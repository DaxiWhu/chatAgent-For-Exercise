"""工具审批（加固阶段三 · 停点①）的守卫。

这一层要挡的是**唯一事后补不回来的缺口**：两个落盘工具一旦执行，文件就留在磁盘上
（它们不在退回白名单里），而现在唯一的人工审核审的是**最终草稿** —— 等审到时文件
早已落下。所以"事前有人点头"这件事必须自己能站住。

三组断言，各有它守的东西：

1. **开关的两种状态**：默认关时行为与改造前**一致**（文件照落、没有待批项）；
   打开时才拦。新能力默认关是本项目的老规矩，但这里还有第二层理由 ——
   打开它会让"落盘"停下来等人，而用户不点头就什么都做不成。
2. **拦下来的真的没做** + **批准后真的做了**：光断言"出现了待批项"是不够的，
   那可能只是"记了一笔、活照干"。唯一可信的证据是磁盘上有没有那个文件。
3. **接着跑 ≠ 重跑一轮**：这条是整个方案成立与否的**唯一证据**。
   如果恢复之后"安全动作"的计数变成 2，说明批准后实际是把这一轮从头重跑 ——
   那会让前面做过的检索、评审再花一遍钱，而且模型这次未必还调那个危险工具
   （用户点了同意却什么都没发生）。探针实测过它是 1，这里把它钉死。

⚠️ 判定表那一档（``awaiting``）单独说明：它**不是失败、也不算兜底**。
不接这一档时，中断的运行会落到"没有可交付的答复"那一行 ⇒ ``failed`` / 系统类，
用户看到的是一条错误报告 —— 这正是"光给工具加 needs_approval、别的都不做"的后果。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agents import Agent, function_tool
from src import observability
from src.agents import approval as A
from src.agents import fallback as F
from src.agents import runner as R
from src.agents.context import TaskContext
from src.agents.tools import save_text_asset
from tests._agents_testkit import ScriptedModel, call, make_ctx, say

#: 安全探针被真正执行了几次（工具函数内部打点 ＝ 唯一可信证据）。
HITS: dict[str, int] = {"safe": 0}


@function_tool
def safe_probe(text: str) -> str:
    """一个**不落盘**的动作：用来验"批准后前面那些步没有被重做"。"""
    HITS["safe"] += 1
    return f"safe-ok:{text}"


def _agent() -> Agent:
    """带上**真实**的落盘工具 + 一个安全探针。"""
    return Agent(
        name="审批测试",
        instructions="按用户要求调用工具。",
        tools=[safe_probe, save_text_asset],
    )


def _write_call() -> object:
    """模型请求落盘这一次调用。"""
    return call("save_text_asset", {"name": "稿", "content": "正文"}, "c1")


class _Sink:
    """最小录制口（与 ``test_agent_fallback.py`` 里那个同形）。"""

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


# --------------------------------------------------------------------------- #
# 第一组：开关
# --------------------------------------------------------------------------- #
def test_approval_is_off_by_default(monkeypatch):
    """★ 新能力**默认关** —— 关着时落盘动作不受任何影响。"""
    monkeypatch.delenv("AGENT_TOOL_APPROVAL", raising=False)
    assert A.approval_enabled() is False


def test_the_callback_follows_the_switch(monkeypatch):
    """审批回调读的是开关（而不是写死 True）—— 否则关不掉。"""
    monkeypatch.setenv("AGENT_TOOL_APPROVAL", "1")
    assert asyncio.run(A.needs_approval_callback(None, {}, "c1")) is True
    monkeypatch.setenv("AGENT_TOOL_APPROVAL", "0")
    assert asyncio.run(A.needs_approval_callback(None, {}, "c1")) is False


def test_the_wait_defaults_to_ten_minutes(monkeypatch):
    """★ 等用户点头的上限默认十分钟（用户拍板的值）。"""
    monkeypatch.delenv("AGENT_APPROVAL_TIMEOUT_SECONDS", raising=False)
    assert A.approval_timeout_seconds() == 600


def test_off_means_the_write_still_happens(monkeypatch, tmp_path):
    """★ 关着时行为与改造前**一致**：文件照落、没有待批项、没有快照。"""
    monkeypatch.setenv("AGENT_TOOL_APPROVAL", "0")
    ctx = make_ctx(tmp_path)
    model = ScriptedModel([_write_call(), say("存好了")])

    out = asyncio.run(R.run_agent(_agent(), prompt="存一下", context=ctx, model=model))

    assert out.exit_reason != "awaiting_approval"
    assert out.approvals == []
    assert out.run_snapshot == ""
    assert (tmp_path / "assets" / "稿.txt").read_text(encoding="utf-8") == "正文"


# --------------------------------------------------------------------------- #
# 第二组：拦得住（且真的没做）
# --------------------------------------------------------------------------- #
def test_on_means_the_write_is_held_back(monkeypatch, tmp_path):
    """★ 开着时：文件**没有**落盘，运行停在"等授权"，并把现场带回来。"""
    monkeypatch.setenv("AGENT_TOOL_APPROVAL", "1")
    ctx = make_ctx(tmp_path)
    model = ScriptedModel([_write_call(), say("存好了")])

    out = asyncio.run(R.run_agent(_agent(), prompt="存一下", context=ctx, model=model))

    assert out.exit_reason == "awaiting_approval"
    assert [a.tool for a in out.approvals] == ["save_text_asset"]
    assert out.approvals[0].call_id == "c1"
    assert "稿" in out.approvals[0].arguments, "审核人要看见模型打算做什么"
    assert not (tmp_path / "assets" / "稿.txt").exists(), (
        "被拦下来的动作**不该**留下文件 —— 只断言「出现了待批项」是不够的，"
        "那可能只是记了一笔、活照干"
    )
    assert out.run_snapshot, "必须把现场带回来，否则批准之后没法接着跑"


def test_the_snapshot_is_json_and_leaves_out_the_unstorable(monkeypatch, tmp_path):
    """快照必须是纯 JSON，且**不含**存不下的东西（函数 / 活对象）。"""
    monkeypatch.setenv("AGENT_TOOL_APPROVAL", "1")
    ctx = make_ctx(tmp_path)
    model = ScriptedModel([_write_call(), say("存好了")])
    out = asyncio.run(R.run_agent(_agent(), prompt="存一下", context=ctx, model=model))

    payload = json.loads(out.run_snapshot)
    assert isinstance(payload, dict)
    blob = json.dumps(payload)
    assert "retriever_factory" not in blob
    assert "reflection" not in blob


# --------------------------------------------------------------------------- #
# 第三组：批准 / 拒绝之后
# --------------------------------------------------------------------------- #
def test_approving_resumes_and_the_write_happens(monkeypatch, tmp_path):
    """★ 批准 ⇒ 接着跑 ⇒ 那个动作**真的执行了**（磁盘上出现文件）。"""
    monkeypatch.setenv("AGENT_TOOL_APPROVAL", "1")
    ctx = make_ctx(tmp_path)
    first = ScriptedModel([_write_call(), say("存好了")])
    out = asyncio.run(R.run_agent(_agent(), prompt="存一下", context=ctx, model=first))
    assert out.exit_reason == "awaiting_approval"

    resumed = ScriptedModel([say("已按授权落盘")])
    out2 = asyncio.run(
        R.run_agent(
            _agent(),
            prompt="",
            context=ctx,
            model=resumed,
            resume=A.ResumeRequest(snapshot=out.run_snapshot, approved=True),
        )
    )

    assert out2.exit_reason != "awaiting_approval"
    assert (tmp_path / "assets" / "稿.txt").read_text(encoding="utf-8") == "正文"


def test_rejecting_leaves_nothing_behind(monkeypatch, tmp_path):
    """★ 拒绝 ⇒ 那个动作没执行，但这次运行**照常跑完**（拒绝不等于终止）。"""
    monkeypatch.setenv("AGENT_TOOL_APPROVAL", "1")
    ctx = make_ctx(tmp_path)
    first = ScriptedModel([_write_call(), say("存好了")])
    out = asyncio.run(R.run_agent(_agent(), prompt="存一下", context=ctx, model=first))

    resumed = ScriptedModel([say("好，那我不落盘了")])
    out2 = asyncio.run(
        R.run_agent(
            _agent(),
            prompt="",
            context=ctx,
            model=resumed,
            resume=A.ResumeRequest(
                snapshot=out.run_snapshot, approved=False, reason="不要落盘"
            ),
        )
    )

    assert not (tmp_path / "assets" / "稿.txt").exists()
    assert out2.exit_reason != "awaiting_approval", "拒绝之后要接着跑完，不是停在那里"
    assert out2.answer, "模型收到拒绝后照常给出答复"


def test_resuming_does_not_redo_the_steps_before_it(monkeypatch, tmp_path):
    """★★ 这套方案成立与否的**唯一证据**：接着跑 ≠ 重跑一轮。

    安全探针的计数若在恢复之后变成 2，就说明"批准后接着跑"实际是"把这一轮从头重跑"。
    那有两个具体代价：前面做过的检索/评审再花一遍钱；模型这次未必还调那个危险工具
    （用户点了同意却什么都没发生）。
    """
    monkeypatch.setenv("AGENT_TOOL_APPROVAL", "1")
    HITS["safe"] = 0
    ctx = make_ctx(tmp_path)
    both = [
        call("safe_probe", {"text": "甲"}, "s1"),
        call("save_text_asset", {"name": "稿", "content": "正文"}, "c1"),
    ]
    first = ScriptedModel([both, say("都办了"), say("都办了")])
    out = asyncio.run(R.run_agent(_agent(), prompt="两件都办了", context=ctx, model=first))

    assert out.exit_reason == "awaiting_approval"
    assert HITS["safe"] == 1, "安全动作在第一次运行时执行了一次"

    resumed = ScriptedModel([say("落盘完成"), say("落盘完成")])
    asyncio.run(
        R.run_agent(
            _agent(),
            prompt="",
            context=ctx,
            model=resumed,
            resume=A.ResumeRequest(snapshot=out.run_snapshot, approved=True),
        )
    )

    assert HITS["safe"] == 1, (
        "恢复**不该**把前面的安全动作再做一遍 —— 变成 2 就说明这是「重跑一轮」，"
        "而不是「从断点继续」"
    )
    assert (tmp_path / "assets" / "稿.txt").exists()


# --------------------------------------------------------------------------- #
# 第四组：判定表那一档
# --------------------------------------------------------------------------- #
def test_row0_awaiting_is_not_a_failure():
    """★ 不接这一档时，中断的运行会落到"没有可交付的答复"⇒ failed。

    这不是推断：探针 P4 实测过 —— 那时用户看到的是一条错误报告，而不是"等你授权"。
    """
    v = F.decide(TaskContext(), exit_reason="awaiting_approval", answer="")
    assert (v.outcome, v.reason, v.category) == (F.AWAITING, "tool_approval", F.BUSINESS)


def test_an_empty_answer_without_the_awaiting_reason_is_still_a_failure():
    """对照组：同样是"没有答复"，只因 ``exit_reason`` 不是待授权，就该判失败。"""
    v = F.decide(TaskContext(), exit_reason="passed", answer="")
    assert v.outcome == F.FAILED


def test_awaiting_does_not_count_as_a_fallback(sink):
    """★ 待授权是**正常暂停**，不进兜底指标 —— 否则"兜底率"会随用户点得慢而升高。"""
    v = F.decide(TaskContext(), exit_reason="awaiting_approval", answer="")
    assert v.is_fallback is False

    F.report(v, exit_reason="awaiting_approval", agent="前台")

    assert sink.names("agent_task_outcome_total") == [
        {"outcome": "awaiting", "agent": "前台"}
    ]
    assert sink.names("agent_fallback_total") == []


def test_a_real_run_reports_the_awaiting_outcome(monkeypatch, tmp_path, sink):
    """跑一遍真实链路：结局指标上是 ``awaiting``，且**没有**兜底记录。"""
    monkeypatch.setenv("AGENT_TOOL_APPROVAL", "1")
    ctx = make_ctx(tmp_path)
    model = ScriptedModel([_write_call(), say("存好了")])

    out = asyncio.run(R.run_agent(_agent(), prompt="存一下", context=ctx, model=model))

    assert out.fallback is not None
    assert out.fallback.outcome == F.AWAITING
    assert sink.names("agent_fallback_total") == []
    outcomes = sink.names("agent_task_outcome_total")
    assert outcomes and outcomes[0]["outcome"] == "awaiting"
    assert outcomes[0]["agent"] == "审批测试", "每运行一次类指标必须带 agent 标签"
