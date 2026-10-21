"""长任务防护的三处补丁 + 兜底清单。

对应四件事，每一件原先都是**判据看不见**的形态（不是"没做"，是"做了但读不出来"）：

1. **目标锚点**：硬截断从最老开始丢，而压缩摘要与用户原话都在最前面 ⇒ 长任务跑到
   后半程，模型面前只剩最近十几个工具返回，没有一句话说它要做什么。
2. **单次工具返回的字符上界**：``kb_search`` 只限条数不限字符，一条超长片段就能吃掉
   大半个窗口 —— 而被它顶掉的是目标（见上一条）。
3. **按工具名的失败计数**：重复/震荡判据都按"工具名 + 参数"算指纹，于是"换着参数
   试同一个坏工具"完全隐形。
4. **已完成清单**：只报"哪几步没做成"，用户没法判断这份部分结果的边界在哪。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("agents", reason="openai-agents 未安装 ⇒ agent 链路不存在，本组无从验证")

from agents.run_config import CallModelData, ModelInputData  # noqa: E402
from agents.tool import default_tool_error_function  # noqa: E402

from agents import Agent  # noqa: E402
from src import observability  # noqa: E402
from src.agents import fallback, toolpool  # noqa: E402
from src.agents import history_budget as hb
from src.agents import steps as S
from src.agents.context import TaskContext  # noqa: E402


def _cd(items, instructions="系统提示"):
    return CallModelData(
        model_data=ModelInputData(input=list(items), instructions=instructions),
        agent=Agent(name="probe"),
        context=None,
    )


def _trim(items):
    """只跑**硬截断**这一层（``_trim_sync``），绕开压缩 —— 本组验的是裁剪的取舍。"""
    return hb._trim_sync(_cd(items))


def _msg(role: str, text: str) -> dict:
    return {"role": role, "content": text}


# --------------------------------------------------------------------------- #
# 1. 目标锚点
# --------------------------------------------------------------------------- #
def test_the_last_user_message_survives_a_tiny_budget(monkeypatch):
    """★★ 预算小到只剩锚点时，**本轮那句要求**必须还在。

    这是整组最要紧的一条：它对应的不是"少了一条历史"，而是"模型不知道自己要做
    什么了"—— 而那种状态看起来像模型在乱答，不像输入被裁过。
    """
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "1")
    items = [
        _msg("user", "很久以前的要求"),
        _msg("assistant", "很久以前的回答"),
        _msg("user", "本轮要做的事"),
        _msg("assistant", "我先查一下"),
        _msg("assistant", "最近的一次工具返回"),
    ]

    out = _trim(items)

    assert _msg("user", "本轮要做的事") in out.input
    assert out.input[-1] == _msg("assistant", "最近的一次工具返回")


def test_the_leading_summary_survives(monkeypatch):
    """开头的 system 摘要（压缩的产物）也必须留着。

    ⚠️ 它尤其容易被丢：压缩刚花钱把它生成出来放在最前面，而硬截断恰好从最前面
    开始丢 —— 于是"花钱买来的背景"活不过同一拍。
    """
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "1")
    items = [
        _msg("system", "【摘要】前面发生过这些事"),
        _msg("user", "很早的话"),
        _msg("user", "本轮要做的事"),
        _msg("assistant", "最近的返回"),
    ]

    out = _trim(items)

    assert out.input[0] == _msg("system", "【摘要】前面发生过这些事")


def test_middle_items_are_still_dropped(monkeypatch):
    """锚点不是"不裁了"：中间那段该丢还是丢（否则预算就没有意义）。"""
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "1")
    items = [
        _msg("user", "本轮要做的事"),
        _msg("assistant", "中间一"),
        _msg("assistant", "中间二"),
        _msg("assistant", "最近的返回"),
    ]

    out = _trim(items)

    texts = [i.get("content") for i in out.input]
    assert "中间一" not in texts
    assert "中间二" not in texts
    assert "本轮要做的事" in texts


def test_within_budget_nothing_changes(monkeypatch):
    """没超预算 ⇒ 原样返回（连拷贝都不做，行为与改造前一致）。"""
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "100000")
    items = [_msg("user", "你好"), _msg("assistant", "你好呀")]

    out = _trim(items)

    assert out.input == items


def test_no_user_item_is_tolerated(monkeypatch):
    """没有用户条目时（纯工具续跑）不许炸，只保留末条。"""
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "1")
    items = [_msg("system", "背景"), _msg("assistant", "甲"), _msg("assistant", "乙")]

    out = _trim(items)

    assert out.input[-1] == _msg("assistant", "乙")


def test_anchors_are_not_counted_as_a_failure_to_trim(monkeypatch):
    """锚点占满之后仍超额 ⇒ 记一条"预算不足"，而不是无声无息。

    这条告警指向配置（预算太小 / 单条输入太长），是这一格里唯一能让人看出
    "再怎么裁也装不下"的信号。
    """
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "1")
    seen: list[str] = []
    saved = observability.get_sink()

    class _Sink:
        def counter(self, name, amount=1, **labels):  # noqa: ANN001, ANN003
            seen.append(name)

        def histogram(self, name, value, **labels):  # noqa: ANN001, ANN003
            pass

        def gauge(self, name, value, **labels):  # noqa: ANN001, ANN003
            pass

    observability.set_sink(_Sink())
    try:
        _trim([_msg("user", "很长的一句要求" * 50), _msg("assistant", "回答")])
    finally:
        observability.set_sink(saved)

    assert "agent_history_over_budget_total" in seen


# --------------------------------------------------------------------------- #
# 2. 单次工具返回的字符上界
# --------------------------------------------------------------------------- #
def test_a_long_string_result_is_clipped(monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_RESULT_MAX_CHARS", "100")
    out = toolpool.clip_result("字" * 500, tool="kb_search")

    assert out.startswith("字" * 100)
    assert "原文 500 字符" in out
    assert "只给了前 100 字符" in out
    # 必须告诉模型"还能怎么办"，否则它会拿半段资料当全部。
    assert "缩小检索范围" in out


def test_a_short_string_result_is_untouched(monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_RESULT_MAX_CHARS", "100")
    assert toolpool.clip_result("短", tool="kb_search") == "短"


def test_non_string_results_are_never_clipped(monkeypatch):
    """结构化产出按字符截断会变成**非法 JSON** —— 那比截断更糟。"""
    monkeypatch.setenv("AGENT_TOOL_RESULT_MAX_CHARS", "5")
    payload = {"outline": ["第一页", "第二页", "第三页"]}
    assert toolpool.clip_result(payload, tool="build_slide_deck") is payload


def test_zero_disables_the_cap(monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_RESULT_MAX_CHARS", "0")
    text = "字" * 500
    assert toolpool.clip_result(text, tool="kb_search") == text


def test_the_cap_is_applied_at_the_single_exit(monkeypatch):
    """★ 截断必须在 ``run_tool_step`` 这个**出口**上，不能靠每个工具自觉。

    靠自觉的失效方式没有任何症状：新写一个工具时忘了截，直到某天一次检索把窗口
    顶爆。这条用例走的是真实的执行路径（线程池 + 超时分支）。
    """
    monkeypatch.setenv("AGENT_TOOL_RESULT_MAX_CHARS", "10")

    out = asyncio.run(toolpool.run_tool_step(lambda: "长" * 100, tool="kb_search"))
    assert out.startswith("长" * 10)
    assert "已截断" in out


# --------------------------------------------------------------------------- #
# 3. 按工具名的失败计数
# --------------------------------------------------------------------------- #
def _handler_ctx() -> tuple[TaskContext, SimpleNamespace]:
    task = TaskContext()
    return task, SimpleNamespace(context=task)


def test_fail_count_is_by_tool_name_not_by_arguments(monkeypatch):
    """★★ 三个**不同参数**的失败也要数成 3 次。

    这正是重复/震荡判据的盲区：它们的指纹含参数，三次不同参数 = 三个指纹、各出现
    一次、都到不了阈值 ⇒ 两条判据一起沉默。
    """
    task = TaskContext()
    log = task.steps
    for query in ("甲", "乙", "丙"):
        log.note_calls([("kb_search", f'{{"query":"{query}"}}', query)])
        log.begin("kb_search")
        log.fail("kb_search", ValueError("boom"))

    assert log.fail_count("kb_search") == 3
    assert log.max_repeat == 0, "参数不同 ⇒ 重复判据（按指纹）确实看不见"


def test_the_message_changes_after_the_limit(monkeypatch):
    """到线之后回给模型的不是"再试一次"，而是"别再用它"。"""
    monkeypatch.setenv("AGENT_TOOL_FAIL_LIMIT", "2")
    task, wrapper = _handler_ctx()
    handler = S.tool_error_handler("kb_search")

    first = handler(wrapper, ValueError("boom"))
    second = handler(wrapper, ValueError("boom"))

    assert "不要再调用它" not in first
    assert "不要再调用它" in second


def test_below_the_limit_keeps_the_official_text(monkeypatch):
    """没到线 ⇒ 与官方默认实现逐字节一致（自愈能力不受影响）。"""
    monkeypatch.setenv("AGENT_TOOL_FAIL_LIMIT", "3")
    _task, wrapper = _handler_ctx()

    text = S.tool_error_handler("kb_search")(wrapper, ValueError("boom"))

    assert text == default_tool_error_function(wrapper, ValueError("boom"))


def test_the_give_up_is_counted_once(monkeypatch):
    """劝退计数只在**刚好到线**那一次记：按 ``>=`` 记就成了"坏工具被调了几次"。"""
    monkeypatch.setenv("AGENT_TOOL_FAIL_LIMIT", "2")
    seen: list[tuple] = []
    saved = observability.get_sink()

    class _Sink:
        def counter(self, name, amount=1, **labels):  # noqa: ANN001, ANN003
            seen.append((name, labels))

        def histogram(self, name, value, **labels):  # noqa: ANN001, ANN003
            pass

        def gauge(self, name, value, **labels):  # noqa: ANN001, ANN003
            pass

    observability.set_sink(_Sink())
    try:
        _task, wrapper = _handler_ctx()
        handler = S.tool_error_handler("kb_search")
        for _ in range(4):
            handler(wrapper, ValueError("boom"))
    finally:
        observability.set_sink(saved)

    hits = [x for x in seen if x[0] == "agent_tool_give_up_total"]
    assert len(hits) == 1
    assert hits[0][1] == {"tool": "kb_search"}


def test_zero_disables_the_give_up(monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_FAIL_LIMIT", "0")
    _task, wrapper = _handler_ctx()
    handler = S.tool_error_handler("kb_search")

    for _ in range(5):
        text = handler(wrapper, ValueError("boom"))
    assert "不要再调用它" not in text


def test_a_success_does_not_count_as_a_failure(monkeypatch):
    """成功几次不构成"它坏了" —— 只数 ``failed``。"""
    task, wrapper = _handler_ctx()
    log = task.steps
    log.begin("kb_search")
    log.finish("kb_search", "结果")

    S.tool_error_handler("kb_search")(wrapper, ValueError("boom"))

    assert log.fail_count("kb_search") == 1
    assert sum(1 for s in log.steps if s.status == "ok") == 1


# --------------------------------------------------------------------------- #
# 4. 已完成清单
# --------------------------------------------------------------------------- #
def _ctx_with_steps(rows: list[tuple[str, str]]) -> TaskContext:
    """按 ``(工具名, 状态)`` 造一份上下文（状态只有 ok / failed）。"""
    task = TaskContext()
    for tool, status in rows:
        task.steps.begin(tool)
        if status == "ok":
            task.steps.finish(tool, "结果")
        else:
            task.steps.fail(tool, ValueError("boom"))
    return task


def test_accomplished_lists_the_ok_steps():
    """★★ 部分结果要能回答"边界在哪"：做成了哪几步、没做成哪几步。"""
    ctx = _ctx_with_steps([("kb_search", "ok"), ("research", "ok"), ("deliver", "failed")])

    verdict = fallback.decide(ctx, exit_reason="max_turns", answer="")

    assert verdict.outcome == fallback.FAILED
    assert len(verdict.accomplished) == 2
    assert "kb_search" in verdict.accomplished[0]
    assert "research" in verdict.accomplished[1]
    assert "deliver" in verdict.unfinished[0]


def test_a_running_step_is_not_reported_as_accomplished():
    """``running``（还没定格）不许进"做成了" —— 那会把边界说成比实际更靠后。"""
    ctx = TaskContext()
    ctx.steps.begin("kb_search")  # 只 begin，不定格

    verdict = fallback.decide(ctx, exit_reason="max_turns", answer="")

    assert verdict.accomplished == []


def test_a_clean_delivery_carries_no_lists():
    """正常交付两份清单都不带（带一份空清单只会让前端多一种分支）。"""
    ctx = _ctx_with_steps([("kb_search", "ok")])

    verdict = fallback.decide(ctx, exit_reason="passed", answer="答好了")

    assert verdict.outcome == fallback.DELIVERED
    assert verdict.accomplished == []
    assert verdict.unfinished == []


def test_the_accomplished_list_has_a_cap():
    """清单自己不能变成新的膨胀源。"""
    ctx = _ctx_with_steps([("kb_search", "ok")] * (fallback.MAX_DONE + 3))

    verdict = fallback.decide(ctx, exit_reason="max_turns", answer="")

    assert len(verdict.accomplished) == fallback.MAX_DONE + 1, "5 条 + 一行'…还有 N 项'"
    assert verdict.accomplished[-1].startswith("…还有 3 项")
