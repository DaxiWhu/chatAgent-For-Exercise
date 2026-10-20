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

import pytest

from src import observability
from src.agents import fallback as F
from src.agents.context import TaskContext
from src.agents.steps import ToolStep


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


def test_review_failed_is_not_in_the_table_at_all():
    """★ 评审结论**不进这张表**（2026-10-19 删掉的那一行）。

    评审是模型自己调的工具，结论是过程内反馈；用户要求「让模型自己决定是否收尾、
    是否启用评分」，所以"评了没过"不能再变成一次转人工。有产出 ⇒ 正常交付，
    指标上靠 ``agent_runs_total{exit_reason="review_failed"}`` 看见比例即可。
    """
    v = _decide(_ctx(), "review_failed", answer="草稿")
    assert (v.outcome, v.reason, v.category) == (F.DELIVERED, "none", "")
    assert v.is_fallback is False


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
# 翻译成 SSE 事件（`app.main._stream_run`）
# --------------------------------------------------------------------------- #
# 判定表本身在 `src/agents/fallback.py`，它只回答"这一轮算哪一档"。**同一份结论
# 怎么让用户感知到**是接口层的事，且只剩一个出口：SSE 事件。所以这一节直接跑
# `_stream_run`，断言客户端**真的收到了什么** —— 而不是某个中间对象长什么样。
def _outcome_for(ctx, *, exit_reason: str, answer: str, review_reason: str = ""):
    """按运行器的口径造一个结果（verdict 由同一张表算出来，不手抄结论）。"""
    from src.agents.runner import TaskOutcome

    verdict = F.decide(ctx, exit_reason=exit_reason, answer=answer)
    return TaskOutcome(
        answer=answer,
        attempts=1,
        final_agent="frontline_agent",
        passed=verdict.outcome in (F.DELIVERED, F.PARTIAL),
        exit_reason=exit_reason,
        fallback=verdict,
        review_reason=review_reason,
    )


def _fake_turn(outcome, *, stream=None):
    """替身运行：`stream` 是"边产边交出去"的那些片段（None = 整段返回）。"""

    async def _run(req, *, context, on_text=None):
        if on_text is not None and stream is not None:
            for chunk in stream:
                await on_text(chunk, "call-1")
        return outcome

    return _run


def _events(monkeypatch, *, outcome, stream=None, thread_id="fb-1"):
    import app.main as M

    monkeypatch.setattr(M, "run_turn", _fake_turn(outcome, stream=stream))
    gen = M._stream_run(M.TurnRequest(prompt="hi", thread_id=thread_id), "chat")

    async def _run():
        return [ev async for ev in gen]

    return asyncio.run(_run())


def _names(events) -> list[str]:
    return [ev["event"] for ev in events]


def test_delivered_emits_no_task_outcome_event(monkeypatch):
    """正常交付**只发正文**，不带任何兜底字段（客户端少一种分支）。

    这也是"delivered 不是一种结局"的落点：它是"没出问题"，不该在事件里出现。
    """
    out = _outcome_for(_ctx(steps=[_ok(1)]), exit_reason="passed", answer="答复")
    events = _events(monkeypatch, outcome=out, stream=["答复"])

    assert _names(events) == ["token", "done"]
    assert "task_outcome" not in _names(events)


def test_failed_emits_task_outcome_and_no_body(monkeypatch):
    """系统类失败 ⇒ 没有正文，但必须给一个**明确的结局**。

    只发 `done` 的话，用户看到的是"零 token + 结束"，与"模型什么都没说"分不开；
    `task_outcome` 里的 reason 才是"为什么这一轮没有正文"的答案。
    """
    out = _outcome_for(_ctx(), exit_reason="error", answer="")
    events = _events(monkeypatch, outcome=out)

    assert "token" not in _names(events), "没产出却发了正文"
    payload = [ev for ev in events if ev["event"] == "task_outcome"][0]["data"]
    assert '"outcome": "failed"' in payload or '"outcome":"failed"' in payload
    assert "error" in payload


def test_partial_delivers_the_body_and_flags_it(monkeypatch):
    """轻档：**正文照常交付**，问题信号走结构化事件（不进正文）。

    停止 ≠ 不交东西。把已经做出来的那部分扣下不给，用户会以为整轮白跑了；
    但也不能不说 —— 否则"只做了一半"与"全做完了"在客户端看来一模一样。
    """
    ctx = _ctx(kinds={"loop": 3})
    out = _outcome_for(ctx, exit_reason="passed", answer="做完的那部分")
    events = _events(monkeypatch, outcome=out, stream=["做完的那部分"])

    assert _names(events)[0] == "token"
    payload = [ev for ev in events if ev["event"] == "task_outcome"][0]["data"]
    assert '"partial"' in payload or "partial" in payload
    assert "loop" in payload


def test_manual_parks_the_draft_instead_of_showing_it(monkeypatch):
    """★★ 转人工的内容**不是给用户的**：一个字都不许以 token 发出去。

    顺序在这里就是语义：先判"要不要人审"，再决定补不补正文。反过来的话，
    待审草稿会先发给用户、然后才收到"待审核" —— "不交半成品"就只写在文档里。
    """
    import app.main as M
    from app.review_queue import ReviewQueue

    q = ReviewQueue(dsn="")
    monkeypatch.setattr(M, "_REVIEW_QUEUE", q)

    out = _outcome_for(_ctx(kinds={"off_track": 1}), exit_reason="passed",
                       answer="跑偏的内容", review_reason="manual")
    events = _events(monkeypatch, outcome=out, thread_id="fb-manual")

    assert "token" not in _names(events), "待审内容被当成正文发出去了"
    need = [ev for ev in events if ev["event"] == "need_review"]
    assert need, "没有下发 need_review —— 客户端永远弹不出审核界面"
    assert "跑偏的内容" in need[0]["data"]
    # 队列里真的躺着这条（即使此刻没有人在线，内容也不能消失）
    assert q.record("fb-manual") is not None
