"""把"最初要干什么"交给压缩：提示词侧与接线侧。

为什么单独一组
-------------
长任务跑到后半程，"目标"只有两个载体：

1. ``history_budget`` 的**锚点**（还没被压过时，就是最初那条用户条目）；
2. ``compaction`` 生成的**摘要**（压过之后，就是摘要的第一项「原始请求与意图」）。

只做第 1 条不够：第一次压缩一旦发生，最初那句原话就只存在于摘要里，而摘要会被
**逐层再压** —— 靠"摘要提示词请你自己从对话里反推目标"是很软的一条链，尤其当被压
那段的开头已经被上一次摘要占掉时，能反推的原料已经不在了。

所以这里验两件必须同时成立的事：
* 摘要的 system 提示词里**真的带着**目标（不是"函数支持 goal 参数"）；
* 裁剪链路（``_compact_before_trim``）**真的把** ``context.goal`` 传下去了。

⚠️ 缺少第二条时症状最隐蔽：函数签名收 ``goal``、单测直接调它也能过，但生产链路上
永远是空串 —— 摘要照常生成、看起来一切正常，只是目标那一行从来没出现过。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("agents", reason="openai-agents 未安装 ⇒ agent 链路不存在，本组无从验证")

from agents.run_config import CallModelData, ModelInputData  # noqa: E402

from agents import Agent  # noqa: E402
from src.agents import compaction as C  # noqa: E402
from src.agents import history_budget as hb  # noqa: E402
from src.agents.context import TaskContext  # noqa: E402


class _RecordingClient:
    """记下每一次 ``create`` 的入参（这一组要验的正是提示词内容）。"""

    def __init__(self, content: str = "摘要正文") -> None:
        self._content = content
        self.calls: list[dict] = []
        self.chat = self
        self.completions = self

    def create(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        message = SimpleNamespace(content=self._content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _system_of(call: dict) -> str:
    return next(m["content"] for m in call["messages"] if m["role"] == "system")


@pytest.fixture(autouse=True)
def _clean_state():
    C.reset_compaction_state()
    yield
    C.reset_compaction_state()


# --------------------------------------------------------------------------- #
# 一、提示词侧：目标真的进了 system
# --------------------------------------------------------------------------- #
def test_the_goal_reaches_the_summarizer(monkeypatch):
    """★★ 有目标时，摘要的 system 提示词里必须出现它。

    判据是"目标出现"而不是"goal 参数非空"：后者在拼装那一步写错（例如拼进了
    user 正文，或变量名写错取到空串）时同样成立。
    """
    client = _RecordingClient()
    monkeypatch.setattr(C, "_client_or_none", lambda: client)

    C.summarize_items([{"role": "user", "content": "旧话"}], goal="把三份财报对比成一张表")

    system = _system_of(client.calls[0])
    assert "把三份财报对比成一张表" in system
    # 原有要求不能被顶掉：目标那一行是**追加**，不是替换。
    # 锚在第一节的标题上 —— 它正是承载"最初要干什么"的那一格，改提示词小节时
    # 这里必须跟着改，否则本文守卫的"对齐"就无处可对。
    assert "原始请求与意图" in system


def test_without_a_goal_the_prompt_is_unchanged(monkeypatch):
    """拿不到目标 ⇒ 提示词里一个字的目标段都没有。

    这条守的是"别编一个目标":一句放之四海皆准的"完成用户的任务"会让摘要的第一项
    变成废话，而它正是后面判断"还在不在解题"的唯一依据。

    ⚠️ 2026-11-02：提示词从"一段固定文案"改成了带 ``{limit}`` 占位符的模板
    （摘要上限按被压段长度浮动）。所以"原版"不再是常量本身，而是
    **模板按本次上限渲染出来的那一份** —— 这里正文只有两个字，上限取到下限 600。
    """
    client = _RecordingClient()
    monkeypatch.setattr(C, "_client_or_none", lambda: client)

    C.summarize_items([{"role": "user", "content": "旧话"}])

    system = _system_of(client.calls[0])
    assert "这件事一开始的目标是" not in system
    assert system == C._SUMMARY_PROMPT.format(limit=C._MIN_SUMMARY_CHARS)


def test_a_long_goal_is_clipped_and_marked(monkeypatch):
    """目标过长要截断并**标注**——不标注会让模型以为那就是全文。"""
    client = _RecordingClient()
    monkeypatch.setattr(C, "_client_or_none", lambda: client)

    C.summarize_items([{"role": "user", "content": "旧话"}], goal="很长的目标" * 200)

    system = _system_of(client.calls[0])
    assert "已截断" in system
    assert len(system) < len(C._SUMMARY_PROMPT) + C._MAX_GOAL_CHARS + 200


# --------------------------------------------------------------------------- #
# 二、接线侧：链路真的把目标传下去了
# --------------------------------------------------------------------------- #
def test_the_trim_path_passes_the_goal_to_compaction(monkeypatch):
    """★★ ``_compact_before_trim`` 必须把 ``context.goal`` 传给压缩。

    这是"函数支持但没人接"最典型的形态：``summarize_items`` 收 goal、单测直接调它
    也能过，但生产链路上永远是空串 ⇒ 摘要里那一行目标从来没出现过，且**不报错**。
    """
    got: dict = {}

    async def fake_compact(items, *, instructions="", on_usage=None, **kw):  # noqa: ARG001
        got.update(kw)
        return None

    monkeypatch.setattr(C, "compact_for_window", fake_compact)
    ctx = TaskContext()
    ctx.goal = "把三份财报对比成一张表"
    data = CallModelData(
        model_data=ModelInputData(input=[{"role": "user", "content": "问"}], instructions=""),
        agent=Agent(name="probe"),
        context=ctx,
    )

    asyncio.run(hb._compact_before_trim(data))

    assert got.get("goal") == "把三份财报对比成一张表"


def test_a_missing_context_degrades_to_an_empty_goal(monkeypatch):
    """没有 context 时传空串，不许抛（压缩是可选优化，不该让整轮挂掉）。"""
    got: dict = {}

    async def fake_compact(items, *, instructions="", on_usage=None, **kw):  # noqa: ARG001
        got.update(kw)
        return None

    monkeypatch.setattr(C, "compact_for_window", fake_compact)
    data = CallModelData(
        model_data=ModelInputData(input=[{"role": "user", "content": "问"}], instructions=""),
        agent=Agent(name="probe"),
        context=None,
    )

    asyncio.run(hb._compact_before_trim(data))

    assert got.get("goal") == ""
