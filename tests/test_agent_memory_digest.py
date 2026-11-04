"""长记忆的提炼：**只在超过长度线时**、且失败必须原样存。

这一组守的是四个方向，每一个写错都不会报错：

1. **短记忆被无谓地压一遍** —— 多一次出站请求、多几秒等待、还要丢细节。
   长记忆才值得压，这是用户 2026-11-02 定的口径（默认 200 字）。
2. **提炼失败变成了"没记住"** —— 用户明确要求记住的话，因为一次网关 502 就丢了。
   降级方向必须是"宁长勿丢"：原样存，只是偏长。
3. **那次调用的用量没进账** —— 它在主链路之外，不进 ``bypass_tokens`` 就永远
   不在预算与账单上（与压缩、反思同一条规矩）。
4. **模型加的包装没剥掉** —— ``` 围栏、``- `` 列表符、引号、编号，任一漏剥都会
   让注入进去的那一行看起来像格式残留，而不是一句记忆。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("agents", reason="openai-agents 未安装 ⇒ 工具链路不存在，本组无从验证")

from agents.items import Usage  # noqa: E402
from agents.tool_context import ToolContext  # noqa: E402

from src.agents import (
    memory,  # noqa: E402
    tools,  # noqa: E402
)
from src.agents import memory_digest as D  # noqa: E402
from src.agents.memory_store import InMemoryMemoryStore  # noqa: E402

#: 超过默认长度线（200 字）、又**低于存储层硬上限**（``MAX_CONTENT_CHARS`` 300）的一段
#: "照抄式"内容。这个窗口很窄（200~300），刻意不加大：再长就会先被存储层截断，
#: 于是"原样存"的用例会变成在验"截断"，而不是在验"没有提炼"。
_LONG = "他一开始问的是怎么配环境，后来聊到回答格式，" * 11
_CONTRACTED = "用户要求回答先给结论、正文不超过 5 条，且不要列目录。"


class _FakeClient:
    """只实现 ``chat.completions.create`` 的最小替身，**带 usage**。

    带 usage 是重点：第 3 条守的就是"那次出站调用的用量有没有交回来"。
    不带 usage 的替身会让记账用例恒绿 —— 它验的是"什么都没发生"。
    """

    def __init__(self, content: str = _CONTRACTED, usage: Usage | None = None) -> None:
        self._content = content
        self._usage = usage
        self.calls: list[dict] = []
        self.chat = self
        self.completions = self

    def create(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        message = SimpleNamespace(content=self._content)
        choice = SimpleNamespace(message=message)
        return SimpleNamespace(choices=[choice], usage=self._usage)


@pytest.fixture(autouse=True)
def _clean_state():
    D.reset_digest_state()
    yield
    D.reset_digest_state()


@pytest.fixture
def store(monkeypatch):
    s = InMemoryMemoryStore()
    monkeypatch.setattr(memory, "_store", lambda: s)
    monkeypatch.setenv("AGENT_MEMORY", "1")
    monkeypatch.delenv("AGENT_MEMORY_DIGEST_ABOVE", raising=False)
    return s


def _ctx(owner="u1"):
    from src.agents.context import TaskContext

    inner = TaskContext(thread_id="t-1")
    inner.owner_key = owner
    return inner


def _remember(ctx, content, kind="preference"):  # noqa: ANN001
    """走**真实链路**（``on_invoke_tool``）：提炼发生在工具层，不在存储层。

    ⚠️ 不能直接调 ``tools.remember``：``@function_tool`` 之后它已经是
    ``FunctionTool`` 对象。也不该绕过装饰器去调内层函数 —— 那样验不到
    "参数从 JSON 解析进来"这一段，而提炼恰好发生在函数体里。
    """
    payload = json.dumps({"content": content, "kind": kind}, ensure_ascii=False)
    tool_ctx = ToolContext(
        context=ctx, usage=Usage(), tool_name="remember",
        tool_call_id="c1", tool_arguments=payload,
    )
    return asyncio.run(tools.remember.on_invoke_tool(tool_ctx, payload))


# --------------------------------------------------------------------------- #
# ① 短的不压
# --------------------------------------------------------------------------- #
def test_short_content_is_stored_verbatim_without_a_model_call(store, monkeypatch):
    """短线以内 ⇒ 一次模型都不调，原文入库。

    判据是"替身有没有被调用"，不是"结果对不对" —— 后者在多做一次无谓请求时
    同样成立，而那次请求正好压在用户等待上。
    """
    client = _FakeClient()
    monkeypatch.setattr(D, "_client_or_none", lambda: client)
    short = "回答先给结论，正文不超过 5 条。"

    out = _remember(_ctx(), short)

    assert client.calls == []
    assert store.active("u1")[0]["content"] == short
    assert "已记住" in out and "提炼" not in out


def test_zero_turns_digestion_off_entirely(store, monkeypatch):
    """``AGENT_MEMORY_DIGEST_ABOVE=0`` ⇒ 关闭提炼（长内容也原样存）。"""
    monkeypatch.setenv("AGENT_MEMORY_DIGEST_ABOVE", "0")
    client = _FakeClient()
    monkeypatch.setattr(D, "_client_or_none", lambda: client)

    _remember(_ctx(), _LONG)

    assert client.calls == []
    assert store.active("u1")[0]["content"] == _LONG


def test_the_threshold_is_configurable_and_bad_values_fall_back(monkeypatch):
    """阈值可配；配了看不懂的值就退回默认，而不是把开关意外关掉。"""
    monkeypatch.setenv("AGENT_MEMORY_DIGEST_ABOVE", "50")
    assert D.digest_above() == 50
    monkeypatch.setenv("AGENT_MEMORY_DIGEST_ABOVE", "不是数字")
    assert D.digest_above() == 200
    monkeypatch.delenv("AGENT_MEMORY_DIGEST_ABOVE")
    assert D.digest_above() == 200


# --------------------------------------------------------------------------- #
# ② 长的压完再存
# --------------------------------------------------------------------------- #
def test_long_content_is_digested_before_it_is_stored(store, monkeypatch):
    """超过长度线 ⇒ 入库的是**提炼后**那句，不是原文。"""
    client = _FakeClient(_CONTRACTED)
    monkeypatch.setattr(D, "_client_or_none", lambda: client)

    out = _remember(_ctx(), _LONG)

    assert len(client.calls) == 1
    assert store.active("u1")[0]["content"] == _CONTRACTED
    assert "已提炼" in out


def test_the_prompt_carries_the_kind_so_the_wording_matches(store, monkeypatch):
    """类别说明要进提示词 —— 偏好压成"要求"、项目事实压成"事实"，措辞不一样。"""
    client = _FakeClient(_CONTRACTED)
    monkeypatch.setattr(D, "_client_or_none", lambda: client)

    _remember(_ctx(), _LONG, kind="preference")

    system = client.calls[0]["messages"][0]["content"]
    assert "他怎么要求回答" in system


def test_a_huge_memory_is_truncated_before_the_call(monkeypatch):
    """两万字的记忆不该让这次调用跟着线性变长：只送前 8000 字。"""
    client = _FakeClient(_CONTRACTED)
    monkeypatch.setattr(D, "_client_or_none", lambda: client)

    D.digest("甲" * 12000)

    assert len(client.calls[0]["messages"][1]["content"]) == 8000


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("```text\n- “用户要求中文回答。”\n```", "用户要求中文回答。"),
        ("1. 用户要求中文回答", "用户要求中文回答"),
        ("• 用户要求中文回答", "用户要求中文回答"),
        ("\n\n用户要求中文回答\n\n多余的下一段", "用户要求中文回答"),
    ],
)
def test_model_wrapping_is_stripped(raw, expected):
    """围栏 / 列表符 / 引号 / 编号 / 多余段落一律剥掉，只留那一句。"""
    assert D._clean(raw) == expected


# --------------------------------------------------------------------------- #
# ③ 失败 ⇒ 原样存（宁长勿丢）
# --------------------------------------------------------------------------- #
def test_a_failed_digest_keeps_the_original_text(store, monkeypatch):
    """提炼抛异常 ⇒ 原文入库，且如实告诉模型"没压成"。"""
    client = _FakeClient()

    def boom(**kwargs):  # noqa: ANN003, ARG001
        raise RuntimeError("网关 502")

    client.create = boom
    monkeypatch.setattr(D, "_client_or_none", lambda: client)

    out = _remember(_ctx(), _LONG)

    assert store.active("u1")[0]["content"] == _LONG
    assert "原样保存" in out


def test_an_empty_digest_also_keeps_the_original(store, monkeypatch):
    """模型返回空内容 ⇒ 同样原样存（空摘要替换等于把这句话删了）。"""
    monkeypatch.setattr(D, "_client_or_none", lambda: _FakeClient("   "))

    out = _remember(_ctx(), _LONG)

    assert store.active("u1")[0]["content"] == _LONG
    assert "原样保存" in out


def test_no_client_configured_is_a_degradation_not_a_failure(store, monkeypatch):
    """没配 Key ⇒ 没有提炼能力，记忆照常写入（只是偏长）。"""
    monkeypatch.setattr(D, "_client_or_none", lambda: None)

    out = _remember(_ctx(), _LONG)

    assert store.active("u1")[0]["content"] == _LONG
    assert "已记住" in out


# --------------------------------------------------------------------------- #
# ④ 那次调用的钱要进账
# --------------------------------------------------------------------------- #
def test_the_digest_usage_lands_in_the_bypass_slot(store, monkeypatch):
    """提炼的用量写进 ``bypass_tokens["memory_digest"]``。

    不进账的后果不是"少报一点"：它完全不出现在预算与账单上，而所有读数自洽。
    """
    usage = Usage(input_tokens=321, output_tokens=17)
    monkeypatch.setattr(D, "_client_or_none", lambda: _FakeClient(_CONTRACTED, usage))
    ctx = _ctx()

    _remember(ctx, _LONG)

    stats = ctx.stats
    assert stats.bypass_tokens["memory_digest"]["input"] == 321
    assert stats.bypass_tokens["memory_digest"]["output"] == 17
    # 预算口径要把这一笔算进去（旁路的钱也是钱）。
    assert stats.bypass_input_tokens == 321


def test_a_missing_usage_does_not_break_the_write(store, monkeypatch):
    """网关不回 usage ⇒ 不记账，但记忆照常写入（记账不该成为写入的前置条件）。"""
    monkeypatch.setattr(D, "_client_or_none", lambda: _FakeClient(_CONTRACTED, None))

    out = _remember(_ctx(), _LONG)

    assert store.active("u1")[0]["content"] == _CONTRACTED
    assert "已记住" in out


# --------------------------------------------------------------------------- #
# ⑤ 长度：提炼结果的硬上限**必须不大于**存储层的截断线
# --------------------------------------------------------------------------- #
def test_the_hard_cap_never_exceeds_the_store_truncation_line():
    """提炼上限必须 ≤ 存储层的 ``MAX_CONTENT_CHARS``。

    这条不成立的后果不是"太长"：``normalize_content`` 是 ``s[:300] + "…"``，
    一条路径 ``D://miniConda//envs//langchain1.2//python.exe`` 正好可能被切成两半，
    切完读起来仍然像一条正常记忆 —— 用户与模型都不知道它坏在哪。

    2026-11-03 真模型实测踩过：提炼上限当时自己拍的 400（比 300 还大），
    模型当场交出 298 字，再多一个字就截了。
    """
    from src.agents.memory_store import MAX_CONTENT_CHARS

    assert D.hard_cap() <= MAX_CONTENT_CHARS
    # 提示词里的目标还要更低一档：模型对"控制在 N 字以内"普遍会略微超出。
    assert D.hard_cap() > D._TARGET_CHARS


def test_an_overlong_digest_is_cut_at_a_sentence_boundary(monkeypatch):
    """超长时按**句末标点**收口，不硬切 —— 硬切会把路径和日期切成两半。"""
    long_sentence = "用户要求先给结论再给理由，最多五条。" * 30
    monkeypatch.setattr(D, "_client_or_none", lambda: _FakeClient(long_sentence))

    out = D.digest("x" * 300)

    assert len(out) <= D.hard_cap()
    assert out.endswith(("。", "；", "！", "？", ";", "!", "?"))


def test_a_single_endless_sentence_falls_back_to_a_hard_cut():
    """整段没有句末标点（一个超长句）⇒ 只能硬切，但不许超过上限。"""
    assert len(D._fit("甲" * 900)) == D.hard_cap()
