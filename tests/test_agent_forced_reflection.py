"""强制检查点 + 交付作废（2026-10-22）。

守的是什么
----------
其余五个触发原因全是**计数型**（工具 8 次、重量级 3 次、换人 2 次、重复 3 次、
震荡 2 圈）。一个动作很快、一次都没重复、也没失败的模型，可以在第 2 步就把方向做错，
然后一路顺畅地做完整件错事 —— 计数型判据全程沉默。这句话的推论很难看：

    **最贵的失败，反而最不会被检查。**

所以补两个按"时间点"而不是"计数"触发的检查：交付那一刻、第 2 个动作之后。
本模块守四件事，每一件改坏之后都是**安静的**：

* 两个检查点**各自只触发一次**（无条件 ≠ 每圈 —— 每圈都查会让反思比主线还贵）；
* 交付那一刻那一刻**不能被配置关掉**（它是这个功能的全部价值）；
* 额度不够时**看得见**（"交付前没检查"与"检查了没问题"在看板上长得一模一样）；
* 判出偏离/撤回之后，**已交付的那份必须作废**，否则用户收到的还是被否掉的旧稿。
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("agents", reason="openai-agents 未安装 ⇒ agent 链路无从验证")

from agents.run_config import ModelInputData  # noqa: E402

from src import observability  # noqa: E402
from src.agents import reflection as R  # noqa: E402
from src.agents.context import TaskContext  # noqa: E402

_KNOBS = (
    "AGENT_MULTISTEP_GUARD",
    "AGENT_REFLECT_AFTER_TOOLS",
    "AGENT_REFLECT_AFTER_HEAVY",
    "AGENT_REFLECT_AFTER_HOPS",
    "AGENT_REFLECT_ON_REPEAT",
    "AGENT_REFLECT_AFTER_STARTED",
    "AGENT_REFLECT_MAX_LEVEL1",
    "AGENT_REFLECT_MAX_LEVEL2",
    "AGENT_REFLECT_MAX_FIRES",
    "AGENT_REFLECT_TIMEOUT",
    "AGENT_REFLECT_WAIT_SECONDS",
)


class RecordingSink:
    """把指标原样记下来的观测出口替身。"""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def counter(self, name, amount=1, **labels):  # noqa: ANN001, ANN003
        self.events.append(("counter", name, amount, labels))

    def histogram(self, name, value, **labels):  # noqa: ANN001, ANN003
        self.events.append(("histogram", name, value, labels))

    def gauge(self, name, value, **labels):  # noqa: ANN001, ANN003
        self.events.append(("gauge", name, value, labels))

    def counters(self, name: str) -> list[tuple]:
        return [e for e in self.events if e[0] == "counter" and e[1] == name]


@pytest.fixture
def sink():
    saved = observability.get_sink()
    rec = RecordingSink()
    observability.set_sink(rec)
    yield rec
    observability.set_sink(saved)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for name in _KNOBS:
        monkeypatch.delenv(name, raising=False)
    # 禁掉真网关：反思会 create_task 出去，不能让它真的发网络请求。
    monkeypatch.setattr(R, "_reflect_sync", lambda evidence, **kw: {"problem": "ok"})
    R.reset_reflection_state()


def _enable(monkeypatch, **knobs) -> None:
    monkeypatch.setenv("AGENT_MULTISTEP_GUARD", "1")
    # 关掉计数型判据（阈值拉到很大），让本模块只测"时间点"那两条 ——
    # 不关的话，第 8 个动作的计数到期会混进来，断言就不再只指向被测判据。
    monkeypatch.setenv("AGENT_REFLECT_AFTER_TOOLS", "999")
    monkeypatch.setenv("AGENT_REFLECT_AFTER_HEAVY", "999")
    monkeypatch.setenv("AGENT_REFLECT_AFTER_HOPS", "999")
    monkeypatch.setenv("AGENT_REFLECT_ON_REPEAT", "999")
    for name, value in knobs.items():
        monkeypatch.setenv(name, str(value))


def _do(ctx: TaskContext, tool: str, idx: int) -> None:
    ctx.steps.note_calls([(tool, json.dumps({"q": idx}), f"c{idx}")])
    ctx.steps.begin(tool)
    ctx.steps.finish(tool, f"结果{idx}")


async def _fire_async(ctx: TaskContext, tool: str = "") -> None:
    R.maybe_reflect(ctx, tool=tool)
    tasks = list(ctx.reflection.tasks)
    if tasks:
        await asyncio.gather(*tasks)


def _fire(ctx: TaskContext, tool: str = "") -> None:
    asyncio.run(_fire_async(ctx, tool))


def _inputs(*items):
    return ModelInputData(input=list(items), instructions="原样")


# --------------------------------------------------------------------------- #
# 一、第 2 个动作之后
# --------------------------------------------------------------------------- #
def test_one_action_is_not_enough(monkeypatch):
    """第 1 次动作之后还看不出方向（那只是"开始了"），不该花钱。"""
    _enable(monkeypatch)
    ctx = TaskContext()
    _do(ctx, "kb_search", 1)
    _fire(ctx)
    assert ctx.reflection.fired == {}


def test_the_early_check_fires_after_the_second_action(monkeypatch):
    _enable(monkeypatch)
    ctx = TaskContext()
    for i in (1, 2):
        _do(ctx, "kb_search", i)
    _fire(ctx)
    assert ctx.reflection.fired == {"early": 1}
    assert "early" in ctx.reflection.forced


def test_the_early_check_fires_only_once(monkeypatch):
    """★ 无条件 ≠ 每圈。

    第 3、4 个动作之后**不该**再触发：每圈都查 = 每圈一次真实模型调用，
    长任务里反思会比主线还贵，而"第二次看同一个方向"几乎没有新信息。
    """
    _enable(monkeypatch)
    ctx = TaskContext()
    for i in range(1, 5):
        _do(ctx, "kb_search", i)
        _fire(ctx)
    assert ctx.reflection.fired.get("early") == 1


def test_the_early_threshold_is_configurable(monkeypatch):
    _enable(monkeypatch, AGENT_REFLECT_AFTER_STARTED=3)
    ctx = TaskContext()
    for i in (1, 2):
        _do(ctx, "kb_search", i)
    _fire(ctx)
    assert ctx.reflection.fired == {}, "阈值 3 时第 2 个动作不该触发"

    _do(ctx, "kb_search", 3)
    _fire(ctx)
    assert ctx.reflection.fired == {"early": 1}


# --------------------------------------------------------------------------- #
# 二、交付那一刻
# --------------------------------------------------------------------------- #
def test_delivery_fires_the_check(monkeypatch):
    _enable(monkeypatch)
    ctx = TaskContext()
    _do(ctx, "deliver", 1)
    _fire(ctx, tool="deliver")
    assert ctx.reflection.fired == {"delivery": 1}
    assert ctx.reflection.forced == {"delivery"}


def test_delivery_wins_when_both_are_due(monkeypatch):
    """两个都满足时报更具体的那个 —— 否则"交了个错东西"会被记成"早期检查"。"""
    _enable(monkeypatch)
    ctx = TaskContext()
    _do(ctx, "kb_search", 1)
    _do(ctx, "deliver", 2)
    _fire(ctx, tool="deliver")
    assert ctx.reflection.fired == {"delivery": 1}
    assert "early" not in ctx.reflection.forced


def test_the_delivery_check_cannot_be_switched_off(monkeypatch):
    """★★ ``AGENT_REFLECT_AFTER_STARTED=0`` 只关早期那个，交付那个不受它控制。

    把它也关掉等于"这个功能可以配成完全不存在"，而配成 0 的那次改动**不会有任何症状**。
    """
    _enable(monkeypatch, AGENT_REFLECT_AFTER_STARTED=0)
    ctx = TaskContext()
    _do(ctx, "deliver", 1)
    _fire(ctx, tool="deliver")
    assert ctx.reflection.fired == {"delivery": 1}


def test_a_short_cap_makes_the_skip_visible(monkeypatch, sink):
    """★ 强制检查点被额度吃掉 ⇒ 必须看得见。

    "交付前没检查"与"检查了、没问题"在看板上长得一模一样，所以这里单独计一条
    ``agent_reflection_skipped_total{reason="cap"}``，把判据钉在"该触发但没触发"上。
    """
    _enable(monkeypatch, AGENT_REFLECT_MAX_FIRES=1)
    ctx = TaskContext()
    for i in (1, 2):
        _do(ctx, "kb_search", i)
    _fire(ctx)                                  # early 用掉唯一一格
    assert ctx.reflection.fired == {"early": 1}

    _do(ctx, "deliver", 3)
    _fire(ctx, tool="deliver")

    assert "delivery" not in ctx.reflection.fired
    labels = [e[3] for e in sink.counters("agent_reflection_skipped_total")]
    assert {"reason": "cap", "trigger": "delivery"} in labels


# --------------------------------------------------------------------------- #
# 三、交付前那次检查必须看得见正文
# --------------------------------------------------------------------------- #
def test_delivery_evidence_contains_the_delivered_text():
    """判"只做了一半就宣布完成"必须有正文可看 —— 否则这次检查只能靠步骤清单猜。"""
    ctx = TaskContext()
    ctx.goal = "写一份三项对比"
    ctx.delivered["delivery"] = "只写了第一项"

    text = R.evidence_of(ctx, trigger="delivery")

    assert "交付前" in text
    assert "只写了第一项" in text


def test_other_triggers_do_not_carry_the_delivery_text():
    """其余触发原因不带正文：那是每次反思都要付的 token，只有交付前那一次值得。"""
    ctx = TaskContext()
    ctx.delivered["delivery"] = "一份很长的正文"
    assert "一份很长的正文" not in R.evidence_of(ctx, trigger="tools")


def test_a_huge_delivery_is_truncated_and_marked():
    ctx = TaskContext()
    ctx.delivered["delivery"] = "字" * (R._MAX_DELIVERY_CHARS + 100)
    text = R.evidence_of(ctx, trigger="delivery")
    assert "（已截断）" in text
    assert len(text) < R._MAX_DELIVERY_CHARS + 400


# --------------------------------------------------------------------------- #
# 四、判出偏离 / 撤回之后：已交付的那份必须作废
# --------------------------------------------------------------------------- #
def _pending(problem: str, *, level: int = 1, call_id: str = "") -> R.ReflectiveFinding:
    return R.ReflectiveFinding(
        problem=problem, summary="看起来不对", level=level, step=0, call_id=call_id
    )


def test_off_track_discards_the_delivered_text(sink):
    """★★ 不作废的后果：用户收到的还是被否掉的那份旧稿，而这次检查"看起来成功了"。

    ``runner`` 取答复的规则是"有已登记的交付就用它" ⇒ 不清掉它，判出偏离也白判。
    """
    ctx = TaskContext()
    ctx.delivered["delivery"] = "旧稿"
    ctx.delivered["delivery_reviewed"] = False
    ctx.reflection.pending.append(_pending("off_track"))

    out = asyncio.run(R.inject_reflections(_inputs({"role": "user", "content": "目标"}), context=ctx))

    assert ctx.delivered == {}, "判出偏离后交付没被作废"
    assert ctx.reflection.discarded == 1
    note = out.input[-1]["content"]
    assert "作废" in note and "重新交付" in note, "没告诉模型要重新交付"
    assert [e[3] for e in sink.counters("agent_delivery_discarded_total")] == [
        {"reason": "off_track"}
    ]


def test_loop_does_not_discard_the_delivered_text():
    """★★ 只有偏离才作废。

    打转/卡住说的是"过程不顺"，不代表这份产出不对；把它们也作废，等于用一次过程问题
    把一份可能正确的成品删掉 —— 而模型重做的那份通常更差（它已经因为打转被提醒过两次）。
    """
    ctx = TaskContext()
    ctx.delivered["delivery"] = "成品"
    ctx.reflection.pending.append(_pending("loop"))

    asyncio.run(R.inject_reflections(_inputs({"role": "user", "content": "目标"}), context=ctx))

    assert ctx.delivered["delivery"] == "成品"
    assert ctx.reflection.discarded == 0


def test_discard_is_skipped_when_the_reminder_has_no_slot(monkeypatch):
    """★★ 不作废与"提醒一定注入"必须同生同死。

    额度用光时那条提醒不会发出去。此时若清掉交付 ⇒ 模型以为东西已经交了、不再交，
    ``runner`` 取不到交付物，用户最终收到一段没人审过的 ``final_output`` ——
    比不作废更糟。
    """
    _enable(monkeypatch, AGENT_REFLECT_MAX_LEVEL1=1)
    ctx = TaskContext()
    ctx.delivered["delivery"] = "成品"
    ctx.reflection.injected = 1                 # 模拟额度已被前面的提醒用光
    ctx.reflection.pending.append(_pending("off_track"))

    out = asyncio.run(R.inject_reflections(_inputs({"role": "user", "content": "目标"}), context=ctx))

    assert ctx.delivered["delivery"] == "成品", "提醒没注入却把交付作废了"
    assert ctx.reflection.discarded == 0
    assert len(out.input) == 1, "额度用光时不该注入任何东西"


def test_a_revert_also_discards_the_delivered_text(sink):
    """撤回的语义是"从这一步到末尾全部重来" ⇒ 基于那些动作做出的交付一并作废。"""
    ctx = TaskContext()
    ctx.delivered["delivery"] = "基于第 2 步做出的结论"
    ctx.reflection.pending.append(_pending("loop", level=2, call_id="c1"))
    md = _inputs(
        {"role": "user", "content": "目标"},
        {"type": "function_call", "call_id": "c1", "name": "kb_search"},
        {"type": "function_call_output", "call_id": "c1", "output": "结果"},
    )

    out = asyncio.run(R.inject_reflections(md, context=ctx))

    assert ctx.delivered == {}
    assert ctx.reflection.discarded == 1
    assert "作废" in out.input[-1]["content"]
    assert [e[3] for e in sink.counters("agent_delivery_discarded_total")] == [
        {"reason": "revert"}
    ]


def test_nothing_to_discard_is_not_counted():
    """没有交付时不该说"已作废" —— 模型会去找一份根本不存在的东西重交。"""
    ctx = TaskContext()
    ctx.reflection.pending.append(_pending("off_track"))

    out = asyncio.run(R.inject_reflections(_inputs({"role": "user", "content": "目标"}), context=ctx))

    assert ctx.reflection.discarded == 0
    assert "作废" not in out.input[-1]["content"]
