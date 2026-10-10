"""兜底出口（第 5 步）的守卫：把 `PLAN_step5_fallback.md` §4 那张判定表**逐行**钉住。

为什么逐行钉而不是"抽几条代表性的"：这张表的语义就是**顺序**（先命中先算）。
只要有一行的位置被挪动，结论就会在某些组合下翻转 —— 而那种翻转在真实流量里
表现为"偶尔有内容该拦没拦"，没有任何东西会报错。逐行 + 两条优先级用例，
才让"谁排在谁前面"这件事本身可测。

另外两处单独说明：

* **单次工具失败不算问题信号**（决策 5：能继续跑就不管它）。这条很容易被"顺手加进去"，
  一旦加进去，一次网络抖动就把一次完全正常的交付标成"部分结果"，用户很快不再相信它。
* **`report` 必须带 agent 标签**：一次请求 = N 次运行，缺标签会让"运行数"被当成"请求数"。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src import observability
from src.agents import fallback as F
from src.agents import runner as R
from src.agents.context import TaskContext
from src.agents.steps import ToolStep
from tests._agents_testkit import ScriptedModel, call, decide, make_ctx


class _Sink:
    """最小录制口（与 `test_agent_reflection.py` 里那个同形）。"""

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


def _ctx(*, steps=(), kinds=None, stopped="") -> TaskContext:
    ctx = TaskContext()
    ctx.steps.steps.extend(steps)
    ctx.reflection.kinds.update(kinds or {})
    ctx.reflection.stopped = stopped
    return ctx


def _ok(seq: int, tool: str = "kb_search") -> ToolStep:
    return ToolStep(seq=seq, tool=tool, status="ok")


def _bad(seq: int, tool: str = "kb_search") -> ToolStep:
    return ToolStep(seq=seq, tool=tool, status="failed")


def _decide(ctx, exit_reason: str, answer: str = ""):
    return F.decide(ctx, exit_reason=exit_reason, answer=answer)


# --------------------------------------------------------------------------- #
# §4 判定表：7 行，逐行
# --------------------------------------------------------------------------- #
def test_row1_a_crash_is_failed_and_system():
    """① 崩溃 / 限流 / 协议错 ⇒ `failed` + 系统类（要改代码）。

    排在最前是有意的：运行根本没跑成，谈不上"内容对错"；而且这条路径没有草稿可审，
    送进审核队列只会多出一条"原因不明"的待审项。
    """
    v = _decide(_ctx(), "error")
    assert (v.outcome, v.reason, v.category) == (F.FAILED, "error", F.SYSTEM)


def test_row2_off_track_is_manual_and_business():
    """② 跑偏 ⇒ 转人工（业务类）。方向做错的内容比不交更糟，哪怕它写得再漂亮。"""
    v = _decide(_ctx(kinds={"off_track": 1}), "passed", answer="一段漂亮但答偏了的内容")
    assert (v.outcome, v.reason, v.category) == (F.MANUAL, "off_track", F.BUSINESS)


def test_row3_review_failed_is_manual_and_business():
    """③ 评审不过 ⇒ 转人工（今天已经是这样，这条把它接进同一张表）。"""
    v = _decide(_ctx(), "review_failed", answer="草稿")
    assert (v.outcome, v.reason, v.category) == (F.MANUAL, "review_failed", F.BUSINESS)


@pytest.mark.parametrize("reason", ["input_guardrail", "output_guardrail"])
def test_row4_guardrails_are_manual_and_business(reason):
    """④ 护栏拦下 ⇒ 转人工（业务类）。草稿由 graph 侧补（见本文件后半段）。"""
    v = _decide(_ctx(), reason)
    assert (v.outcome, v.reason, v.category) == (F.MANUAL, "guardrail", F.BUSINESS)


@pytest.mark.parametrize(
    ("exit_reason", "stopped", "want"),
    [
        ("guard_stop", "capped", "guard_stop"),
        ("max_turns", "", "max_turns"),
        ("passed", "", "no_output"),
    ],
)
def test_row5_no_answer_is_failed_and_keeps_the_real_reason(exit_reason, stopped, want):
    """⑤ 没有可交付的答复 ⇒ `failed` + 系统类，且**带上真原因**。

    三种情形各自要能区分：否则"兜底老在兜同一类事"与"偶发一次"在图上看不出来。
    """
    v = _decide(_ctx(stopped=stopped), exit_reason)
    assert (v.outcome, v.reason, v.category) == (F.FAILED, want, F.SYSTEM)


@pytest.mark.parametrize(
    ("exit_reason", "kinds", "stopped", "want"),
    [
        ("passed", {"loop": 3}, "", "loop"),
        ("passed", {"stuck": 2}, "", "stuck"),
        ("max_turns", {}, "", "max_turns"),
        ("guard_stop", {}, "capped", "guard_stop"),
    ],
)
def test_row6_produced_with_a_problem_signal_is_partial(
    exit_reason, kinds, stopped, want
):
    """⑥ 有答复 + 出过问题信号 ⇒ **照常交付** + 标明部分结果（停止 ≠ 不交东西）。"""
    v = _decide(
        _ctx(kinds=kinds, stopped=stopped), exit_reason, answer="做完的那部分"
    )
    assert (v.outcome, v.reason, v.category) == (F.PARTIAL, want, F.SYSTEM)


def test_a_loop_outranks_a_stuck_when_both_were_reported():
    """⑥ 里多个信号同时成立时**只报第一个**（顺序 = 打转 → 卡住 → 停过 → 轮次烧完）。

    同一个理由与反思的触发原因一致：同时报多个会让"到底为什么"这一栏失去意义。
    顺序也不能反 —— 反了以后"打转"就永远看不到（它总是先被"卡住"盖住），
    而这两个词对应的是两种完全不同的处置。
    """
    v = _decide(
        _ctx(kinds={"loop": 2, "stuck": 5}, stopped="capped"),
        "guard_stop",
        answer="答复",
    )

    assert (v.outcome, v.reason) == (F.PARTIAL, "loop")


def test_row7_a_clean_run_is_delivered():
    """⑦ 其余 ⇒ 正常交付，而且**不带任何兜底字段**。"""
    v = _decide(_ctx(steps=[_ok(1)]), "passed", answer="答复")
    assert (v.outcome, v.reason, v.category, v.unfinished) == (
        F.DELIVERED, "none", "", []
    )


# --------------------------------------------------------------------------- #
# 优先级：**顺序即策略**
# --------------------------------------------------------------------------- #
def test_a_crash_outranks_off_track():
    """① 压过 ②：崩溃时没有草稿可审，转人工只会多一条"原因不明"的待审项。"""
    v = _decide(_ctx(kinds={"off_track": 1}), "error")
    assert v.outcome == F.FAILED


def test_off_track_outranks_review_failed():
    """② 压过 ③：两条都是转人工，但"跑偏"才是那份内容不能交付的真原因。"""
    v = _decide(_ctx(kinds={"off_track": 1}), "review_failed", answer="草稿")
    assert v.reason == "off_track"


def test_a_guardrail_outranks_the_missing_answer():
    """④ 压过 ⑤：护栏那两条本来就没有产出，不能因为"没产出"就丢掉"被拦下"这个原因。"""
    v = _decide(_ctx(), "output_guardrail")
    assert v.reason == "guardrail"


# --------------------------------------------------------------------------- #
# 一条容易写错的边界：单次工具失败**不**构成问题信号
# --------------------------------------------------------------------------- #
def test_a_single_failed_step_alone_is_not_a_signal():
    """★★ 有过失败步骤、但运行继续跑完了、也没报问题 ⇒ **正常交付**。

    决策 5 的原话是"能继续跑就不管它"。把失败步骤也算成问题信号的后果很具体：
    一次网络抖动就会把一次完全正常的交付标成"部分结果"，用户很快就不再相信这个提示。

    ⚠️ 失败的步骤**仍然进清单**（要如实告知哪几步没做成）—— 不参与的是"**触发**"这一档，
    不是"记录"。见下面那条用例。
    """
    v = _decide(_ctx(steps=[_ok(1), _bad(2), _ok(3)]), "passed", answer="答复")
    assert v.outcome == F.DELIVERED
    assert v.unfinished == [], "没触发兜底时不该带清单（前端少一种分支）"


def test_the_failed_step_is_still_listed_when_something_else_triggered_the_fallback():
    """触发是由别的原因来的 ⇒ 那条失败步骤照样要出现在清单里。"""
    v = _decide(
        _ctx(steps=[_ok(1), _bad(2)], kinds={"loop": 3}), "passed", answer="答复"
    )
    assert v.outcome == F.PARTIAL
    assert v.unfinished == ["第 2 步 kb_search（failed）"]


def test_the_unfinished_list_is_capped():
    """★ 清单**有上限**：它自己不能变成新的膨胀源（要进审核队列与结构化返回）。"""
    steps = [_bad(i) for i in range(1, F.MAX_UNFINISHED + 4)]
    v = _decide(_ctx(steps=steps, kinds={"loop": 3}), "passed", answer="答复")
    assert len(v.unfinished) == F.MAX_UNFINISHED + 1
    assert v.unfinished[-1] == f"…还有 {3} 项"


def test_a_step_with_an_unexpected_status_still_lands_in_the_list():
    """★ 清单口径是「**不等于** ok」而不是「等于 failed」。

    今天 `ToolStep.status` 只有 `ok` / `failed` 两种取值，两种写法等价；但将来多出一种
    （比如 `skipped` / `blocked`）时，写成 `== "failed"` 的那版会**静默漏报**一条确实
    没做成的动作 —— 而"没做成的步骤"正是用户要看到的那部分。宁可多报一条。
    """
    steps = [_ok(1), ToolStep(seq=2, tool="kb_search", status="skipped")]
    v = _decide(_ctx(steps=steps, kinds={"loop": 3}), "passed", answer="答复")

    assert v.unfinished == ["第 2 步 kb_search（skipped）"]


# --------------------------------------------------------------------------- #
# 翻译成 state（`src/graph.py::_verdict_state`）
# --------------------------------------------------------------------------- #
def _outcome(verdict, *, exit_reason="passed", answer="草稿", draft=""):
    return SimpleNamespace(
        fallback=verdict, exit_reason=exit_reason, answer=answer, fallback_draft=draft
    )


def _state_of(verdict, **kw):
    from src import graph as G

    return G._verdict_state(_outcome(verdict, **kw), {}, TaskContext())


def test_delivered_falls_through_to_the_normal_delivery():
    """`delivered` / `partial` ⇒ 返回 `None`（照常交付），由调用方那段公共块处理。"""
    assert _state_of(_decide(_ctx(), "passed", "答复")) is None
    assert _state_of(_decide(_ctx(kinds={"loop": 1}), "passed", "答复")) is None


def test_manual_scores_below_the_threshold_so_the_existing_branch_sends_it_to_human():
    """★★ 转人工**不改条件边**，靠"分数低于阈值"这一个既有杠杆表达。

    这条钉的是"判据只有一处"：一旦有人给 `route_agent_review` 加一个"兜底"分支，
    同一个"该不该人看"就有了两处判据，改一处必漏另一处。
    """
    from src.graph import HIGH

    verdict = _decide(_ctx(kinds={"off_track": 1}), "passed", answer="跑偏的内容")
    state = _state_of(verdict, answer="跑偏的内容")

    assert state["score"] < HIGH
    assert state["is_need_evaluate"] is True
    assert "force_exit" not in state, "转人工不是强制收尾，否则永远到不了 human_review"
    assert state["output"] == "跑偏的内容", "草稿要带给审核人"
    assert state["task_outcome"] == F.MANUAL
    assert state["fallback_reason"] == "off_track"


def test_manual_feedback_tells_the_reviewer_why_and_what_is_left():
    """决策 13：原因 + 未完成清单一起带给审核人（走已有的 `feedback`，不加列）。"""
    ctx = _ctx(steps=[_bad(2)], kinds={"off_track": 1})
    from src import graph as G

    state = G._verdict_state(
        _outcome(_decide(ctx, "passed", "跑偏的内容")), {}, ctx
    )
    assert state["feedback"].startswith("[系统] 兜底转人工：")
    assert "偏离了用户的要求" in state["feedback"]
    assert "未完成：第 2 步 kb_search（failed）。" in state["feedback"]


def test_manual_keeps_feedback_source_ai():
    """★★ `feedback_source` 必须是 `ai`。

    写成 `human` 的后果：下一轮 `_agent_context` 会把这段**系统原因**当成
    "人工打回的意见"送进模型 —— 模型于是收到一句不是人说的话。
    """
    state = _state_of(_decide(_ctx(kinds={"off_track": 1}), "passed", "草稿"))
    assert state["feedback_source"] == "ai"


def test_the_draft_for_a_guarded_run_is_the_guarded_content():
    """护栏那两条**没有产出** ⇒ 草稿要用"被拦下的那段内容"（决策 8 的实现）。"""
    state = _state_of(
        _decide(_ctx(), "input_guardrail"), exit_reason="input_guardrail", draft="用户那句请求"
    )
    assert state["output"] == "用户那句请求"


def test_failed_keeps_a_visible_ending_and_forces_the_exit():
    """`failed` ⇒ 不交本次产出（说明里没有它）、但要给用户一个明确结局。

    留空答复的后果写在 `app/main.py` 的注释里：客户端收到"零 token + done"，
    与"模型什么都没说"分不开。
    """
    from src.graph import ERROR_REPORT

    state = _state_of(_decide(_ctx(), "error"), exit_reason="error")
    assert state["output"] == ERROR_REPORT
    assert state["force_exit"] is True
    assert state["is_need_evaluate"] is False


def test_the_partial_notice_goes_into_the_body_only():
    """轻档：正文多一句提醒；清单走结构化字段（正文不铺开）。"""
    from src import graph as G

    text = G._with_partial_notice("做完的那部分")
    assert text.startswith("做完的那部分")
    assert "部分结果" in text
    assert len(text.splitlines()) == 3, "只加一句，不铺开清单"


def test_delivered_carries_no_fallback_fields():
    """正常交付不在 state 上留下"有过兜底"的痕迹（否则前端每轮都要多判一次）。"""
    from src import graph as G

    assert G._verdict_fields(_decide(_ctx(), "passed", "答复")) == {}


# --------------------------------------------------------------------------- #
# 指标（唯一上报点）
# --------------------------------------------------------------------------- #
def test_report_counts_the_outcome_and_carries_the_agent_label(sink):
    """★ 每次运行都要有 outcome，而且**必须带 agent 标签**（一次请求 = N 次运行）。"""
    F.report(_decide(_ctx(), "passed", "答复"), exit_reason="passed", agent="frontline_agent")

    assert sink.names("agent_task_outcome_total") == [
        {"outcome": F.DELIVERED, "agent": "frontline_agent"}
    ]
    assert sink.names("agent_fallback_total") == [], "正常交付不是兜底"


def test_report_only_counts_a_fallback_when_there_is_one(sink):
    """兜底才计 `agent_fallback_total`，且 reason 是有限枚举（可做标签）。"""
    F.report(
        _decide(_ctx(), "error"), exit_reason="error", agent="frontline_agent"
    )
    assert sink.names("agent_fallback_total") == [
        {"reason": "error", "agent": "frontline_agent"}
    ]


# --------------------------------------------------------------------------- #
# 真跑一次：接线（不只是纯函数）
# --------------------------------------------------------------------------- #
def test_a_real_run_reaches_the_table(tmp_path):
    """★ 真跑一次 `run_agent`：`fallback` 必须被填上（否则图那两处收尾读不到结论）。"""
    ctx = make_ctx(tmp_path)
    out = asyncio.run(
        R.run_agent(
            R.ENTRY_AGENT,
            prompt="限流怎么算？",
            context=ctx,
            model=ScriptedModel([decide("答复。")]),
        )
    )
    assert out.fallback is not None
    assert out.fallback.outcome == F.DELIVERED


def test_a_real_run_skips_the_table_for_a_handoff(tmp_path):
    """转交那一步**不进表**：它没有交付物，进表会让每次正常转交都落进"没产出"。"""
    ctx = make_ctx(tmp_path)
    out = asyncio.run(
        R.run_agent(
            R.ENTRY_AGENT,
            prompt="写个开场",
            context=ctx,
            model=ScriptedModel([decide("", action="transfer")]),
        )
    )
    assert out.exit_reason == "transfer"
    assert out.fallback is None


def test_the_third_level_stop_becomes_guard_stop_and_is_not_a_crash(tmp_path, monkeypatch):
    """★★ 第 3 级从注入口抛出 ⇒ `exit_reason="guard_stop"`，**不是** `error`。

    这条同时钉住三件事：
    ① 异常能穿过 SDK 冒到 `run_agent`（`tmp_probe/probe_guard_stop.py` 的实测）；
    ② 它被**专用捕获**接住，没被 `except Exception` 吞成"崩溃"（那会让运维去查
       一个不存在的故障）；
    ③ 模型**少跑一轮**（抛出即中断，不是"跑完再说"）。
    """
    real = R.reflection.inject_reflections
    seen = {"n": 0}

    async def stop_on_second(data, *, context=None):  # noqa: ANN001
        seen["n"] += 1
        if seen["n"] == 2:
            if getattr(context, "reflection", None) is not None:
                context.reflection.stopped = "capped"
            raise R.reflection.AgentStoppedByGuard("capped")
        return await real(data, context=context)

    monkeypatch.setattr(R.reflection, "inject_reflections", stop_on_second)

    ctx = make_ctx(tmp_path)
    model = ScriptedModel(
        [
            call("save_text_asset", {"name": "稿", "content": "第一版。"}, "c1"),
            decide("答复。"),
        ]
    )
    out = asyncio.run(
        R.run_agent(R.ENTRY_AGENT, prompt="写个开场", context=ctx, model=model)
    )

    assert out.exit_reason == "guard_stop"
    assert out.error == "", "被当成崩溃了（专用捕获没排在 except Exception 之前）"
    assert len(model.calls) == 1, "抛出没有拦住后续的模型调用"
    assert out.fallback is not None
    assert (out.fallback.outcome, out.fallback.reason) == (F.FAILED, "guard_stop")
