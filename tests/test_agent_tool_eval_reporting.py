"""评测报告里"重复动作"这一列必须真的在数 —— 它是最容易写成永远为空的那种输出。

为什么单独守它
--------------
``slide-outline`` 一轮出现 14 次模型调用时，只有两种解释：模型在**分治**
（不同检索式、不同页面）或者在**绕圈**（同一个动作反复做）。两者的处置完全相反，
而"工具名列表"这一列对二者长得一模一样（都是 ``kb_search`` 连着好几条）。
所以报告里加了"同一动作重复 N 次"，判据复用 ``StepLog.repeat_counts``（按指纹）。

如果这列因为接错字段而永远输出空，"没打转"和"没检测到打转"就再也分不清了 ——
这正是需要一条测试钉住的地方。
"""

from __future__ import annotations

from scripts.agent_tool_eval import _repeat_summary
from src.agents.steps import StepLog, ToolStep


def _step(seq: int, tool: str, fingerprint: str, *, settled: bool = True) -> ToolStep:
    return ToolStep(seq=seq, tool=tool, fingerprint=fingerprint, status="ok", settled=settled)


def test_same_fingerprint_is_reported_as_repeat():
    log = StepLog(
        steps=[
            _step(1, "research", "fp-research"),
            _step(2, "research", "fp-research"),
            _step(3, "kb_search", "fp-search-1"),
        ]
    )
    assert _repeat_summary(log) == [{"tool": "research", "count": 2}]


def test_different_arguments_are_not_a_repeat():
    """同一个工具、不同参数 = 正常查多次，不能报成打转。

    这是判据的核心：按工具名数会把"查得细"误判成"卡住了"，而看板上二者同形。
    """
    log = StepLog(
        steps=[
            _step(1, "kb_search", "fp-a"),
            _step(2, "kb_search", "fp-b"),
            _step(3, "kb_search", "fp-c"),
        ]
    )
    assert _repeat_summary(log) == []


def test_unsettled_steps_do_not_count():
    """没定格的步骤（还在跑 / 已被放弃）不算"做过一次"。

    口径必须与指标播报同源（``StepLog.repeat_counts``）；这里若自己放宽，
    报告上的"重复"会比反思闸门看到的更多，两处结论打架且无从对账。
    """
    log = StepLog(
        steps=[
            _step(1, "research", "fp-research"),
            _step(2, "research", "fp-research", settled=False),
        ]
    )
    assert _repeat_summary(log) == []


def test_empty_log_reports_nothing():
    assert _repeat_summary(StepLog()) == []
