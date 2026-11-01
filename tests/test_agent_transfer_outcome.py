"""转交（handoff）与兜底表的边界（2026-11-01 新增）。

为什么单开一份
==============
``fallback.decide`` 的 docstring 里写着一条硬约定：**调用方必须先排掉 ``transfer``**，
否则"每一次正常转交都会落进'没有可交付的答复'那一行"。这句话在代码里只体现为一个
``if``（``runner.run_agent`` 收尾处），而在此之前**没有任何测试盯着它**：

* ``test_agents_pipeline.py`` 断言的是 ``exit_reason`` / ``final_agent`` / ``answer``；
* ``test_agent_fallback.py`` 断言的是判定表**内部**各行的分类，从来没喂过 ``transfer``。

于是"删掉那个 ``if``"是一种全程静默的改动：有作品的转交仍然会把作品交出去，
只是判定表会悄悄多出一条 ``delivered`` 事件 —— 指标上表现为"交付量凭空多出一截"，
而没有任何一处报错。

本文件用**真链路**（ScriptedModel + run_agent）钉住三件事：

1. 正常转交 ⇒ 判定表**不被调用**（``fallback is None``，且没有 task_outcome / fallback 事件）；
2. 转交的运行仍然有一条 ``agent_runs_total{exit_reason="transfer"}`` —— 它必须能被数到，
   只是数在另一个指标上（否则"转交了多少次"就无从回答）；
3. 对照：不转交的那一次**有**这些事件 —— 否则 (1) 可能只是"事件根本没接上"。

⚠️ 已知空白（本文件不覆盖，见交付说明）：``transfer`` 且接手方**一个字都没写**时，
判定表同样被跳过 ⇒ 用户拿到空答复、客户端**收不到任何 task_outcome**。
探针实测（2026-11-01）：``exit_reason='transfer' answer='' fallback=None``，零事件。
这条要不要补报，是产品口径问题，等拍板后再动。
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("agents", reason="openai-agents 未安装 ⇒ agent 链路会静默降级")

from src import observability  # noqa: E402
from src.agents import runner as R  # noqa: E402
from tests._agents_testkit import ScriptedModel, decide, handoff_to, make_ctx  # noqa: E402
from tests.test_agents_pipeline import ENTRY_AGENT  # noqa: E402


class _Sink:
    """最小录制口（与 ``test_agent_approval.py`` 里那个同形）。"""

    def __init__(self) -> None:
        self.counters: list[tuple[str, dict]] = []

    def counter(self, name, amount=1, **labels):  # noqa: ANN001, ANN003
        self.counters.append((name, labels))

    def histogram(self, name, value, **labels):  # noqa: ANN001, ANN003
        pass

    def gauge(self, name, value, **labels):  # noqa: ANN001, ANN003
        pass

    def names(self, name: str) -> list[dict]:
        return [labels for got, labels in self.counters if got == name]


@pytest.fixture
def sink():
    saved = observability.get_sink()
    rec = _Sink()
    observability.set_sink(rec)
    yield rec
    observability.set_sink(saved)


def _run(prompt: str, script, tmp_path):
    ctx = make_ctx(tmp_path)
    model = ScriptedModel(script)
    out = asyncio.run(
        R.run_agent(ENTRY_AGENT, prompt=prompt, context=ctx, model=model)
    )
    return out, model


# --------------------------------------------------------------------------- #
# ① 正常转交：判定表不被调用
# --------------------------------------------------------------------------- #
def test_a_normal_transfer_never_enters_the_fallback_table(tmp_path, sink):
    """★ 有作品的转交 ⇒ ``fallback is None``，且**不发** task_outcome / fallback 事件。

    为什么这是对的：转交是**常态**，不是出问题。让它进表只有两种后果，都不对：

    * 落第⑤行（"没有可交付的答复"）⇒ 每一部正常交出去的作品都被记成失败；
    * 落第⑦行（``delivered``）⇒ 指标上凭空多出一批交付，而这一轮根本没有"前台
      给用户的答复"，答复是接手方写的 —— 两件事在 ``agent_runs_total`` 里已经分开了，
      这边再记一次 delivered 就是重复计数。

    所以正确形态是：transfers 只出现在 ``agent_runs_total{exit_reason="transfer"}`` 上。
    """
    out, model = _run("写一篇关于灯塔的短篇小说", [handoff_to(), decide("一稿。")], tmp_path)
    model.assert_complete()

    assert out.exit_reason == "transfer"
    assert out.answer == "一稿。", "作品必须真的交出去（不是空答复）"
    assert out.fallback is None, (
        "转交进了判定表 —— 正常转交被当成一次'产出'来判，"
        "下一次删掉那个 if 时没有任何测试会发现"
    )
    assert sink.names("agent_task_outcome_total") == [], "转交不该发 task_outcome 事件"
    assert sink.names("agent_fallback_total") == [], "转交不该进兜底统计"


def test_a_transfer_is_still_counted_on_the_runs_metric(tmp_path, sink):
    """转交必须能被数到 —— 只是数在 ``agent_runs_total`` 上，不是 task_outcome。

    与上一条配套：上面说"不进表"，这条保证"但也别把它弄丢"。
    两个一起才说明"转交自成一档"，只做前者等于把转交从所有指标里抹掉。
    """
    out, model = _run("写一篇短篇小说", [handoff_to(), decide("一稿。")], tmp_path)
    model.assert_complete()

    runs = sink.names("agent_runs_total")
    assert runs, "必须有运行指标"
    assert runs[0].get("exit_reason") == "transfer", (
        f"转交要自成一档，实际记成了 {runs[0].get('exit_reason')!r}"
    )


# --------------------------------------------------------------------------- #
# ② 对照组：不转交的那一次**有**这些事件
# --------------------------------------------------------------------------- #
def test_a_plain_reply_does_enter_the_table(tmp_path, sink):
    """★ 对照组 —— 没有它，上面两条可能只是因为"指标根本没接上"而通过。

    前台直接答复 ⇒ 判定表**确实**被调用（``delivered``），``agent_runs_total`` 的
    ``exit_reason`` 也不是 transfer。三条一起才构成"转交确实被区别对待"的证据。
    """
    out, model = _run("限流怎么选？", [decide("用加权滑动窗口。")], tmp_path)
    model.assert_complete()

    assert out.exit_reason != "transfer"
    assert out.fallback is not None, "正常交付也要走判定表 —— 它是'这次结果怎么交'的唯一出口"
    assert out.fallback.outcome == "delivered"
    assert sink.names("agent_fallback_total") == [], "delivered 不是兜底"
    # ⚠️ 指标层**是发** delivered 的（``fallback.report`` 无条件发结局计数）；
    # "delivered 不发"说的是 **SSE 事件**那一层（见 ``app/main.py`` 的 task_outcome 分支）。
    # 两件事别混：混了会得出"转交不该发 outcome"的错误结论 —— 转交是**根本不进表**。
    assert sink.names("agent_task_outcome_total") == [
        {"outcome": "delivered", "agent": ENTRY_AGENT.name}
    ]
    runs = sink.names("agent_runs_total")
    assert runs[0].get("exit_reason") != "transfer"
