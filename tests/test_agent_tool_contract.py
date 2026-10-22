"""工具契约：模型多给字段、给空内容、拿回空片段（2026-10-22）。

守的是什么
----------
这四种都是**不会报错的失败**，而且共同点是"看起来一切正常"：

1. **多给了字段** —— 官方解析参数用的是没开 ``extra="forbid"`` 的 pydantic 模型 ⇒
   多出来的字段被**静默丢弃**。模型以为加了过滤条件，实际那一刻条件已经没了，
   工具照常返回一大段结果，模型也不知道自己漏了什么。
2. **空内容落盘** —— 写出去一个空文件，返回值仍是"已保存"的路径。失败要到**用户
   打开文件**时才发现，而那一刻已经没有任何东西能补救了。
3. **空交付** —— 用户收到一片空白，而这一轮在看板上是成功的。
4. **命中但没有正文的片段** —— 拼进上下文就是一堆空条目，模型会把它当成"查过了、
   没有结论"继续往下写，最终交出一份没有依据的答复。

三处实现决定（改坏任何一个都会让上面某条重新变成隐形）
----------------------------------------------------
* 判据在**包装层**（``tools._extra_args``）：函数体拿到的 kwargs 已经被过滤干净，
  在函数体里查不到了 —— 这是整件事唯一的落点。
* 违约**抛异常**而不是返回一句"失败"：返回文本在步骤清单里算 ``ok``，
  ``fallback._accomplished`` 会把它列成"做成了"。
* 异常继承 ``ModelBehaviorError``：归因是"模型填错了"（``bad_args``），
  而且官方会把它交给失败处理函数转成文案退回模型 —— **不崩整轮**。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("agents", reason="openai-agents 未安装 ⇒ 工具链路无从验证")

from agents.tool_context import ToolContext  # noqa: E402
from agents.usage import Usage  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from src import observability  # noqa: E402
from src.agents import tools as T  # noqa: E402
from src.agents.context import TaskContext  # noqa: E402
from src.agents.steps import StepLog, ToolContractError, _failure_reason  # noqa: E402
from src.agents.types import Finding, SlideDeck  # noqa: E402


class RecordingSink:
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


@pytest.fixture
def ctx(tmp_path):
    """一个带真实资产目录与步骤清单的任务上下文。"""
    c = TaskContext()
    c.asset_dir = tmp_path
    c.steps = StepLog()
    return c


def _call(tool, ctx: TaskContext, args: dict):
    """走**官方那条路**调一次工具（含 JSON 解析与失败处理），返回给模型的文本。

    ⚠️ 必须走 ``on_invoke_tool`` 而不是直接调 ``_isolated`` 里面的函数：被验证的
    恰恰是"异常会被官方转成文案、且步骤清单定格成 failed"这一整条链。
    """
    raw = json.dumps(args, ensure_ascii=False)
    call_ctx = ToolContext(
        context=ctx,
        usage=Usage(),
        tool_name=getattr(tool, "name", ""),
        tool_call_id="call_1",
        tool_arguments=raw,
    )
    return asyncio.run(tool.on_invoke_tool(call_ctx, raw))


def _settled(ctx: TaskContext) -> list[tuple[str, str, str]]:
    return [(s.tool, s.status, s.reason) for s in ctx.steps.steps]


# --------------------------------------------------------------------------- #
# 一、多给了字段
# --------------------------------------------------------------------------- #
def test_extra_args_are_rejected(ctx, sink):
    """★★ 模型自己编了一个 ``filters`` ⇒ 必须退回，不能静默丢掉。"""
    out = _call(
        T.save_text_asset,
        ctx,
        {"name": "a", "content": "正文", "filters": {"year": 2025}},
    )

    assert "不认识这些参数" in out
    assert "filters" in out, "文案里没说是哪个字段 —— 模型没法一次改对"
    assert "content" in out and "name" in out, "文案里没列出它能填什么"
    assert _settled(ctx) == [("save_text_asset", "failed", "bad_args")]
    assert [e[3] for e in sink.counters("agent_tool_unknown_args_total")] == [
        {"tool": "save_text_asset"}
    ]


def test_extra_args_stop_before_the_body_runs(ctx):
    """★ 检查在**进线程池之前** ⇒ 函数体一次都没跑，磁盘上什么都不该有。"""
    _call(T.save_text_asset, ctx, {"name": "a", "content": "正文", "nope": 1})

    assert list(Path(ctx.asset_dir).iterdir()) == []


def test_argument_order_and_whitespace_do_not_matter(ctx):
    """同一组字段、换个顺序不算"多给" —— 否则会误伤每三次正常调用。"""
    out = _call(T.save_text_asset, ctx, {"content": "正文", "name": "b"})
    assert out.endswith("b.txt")
    assert not ctx.steps.failed, "正常调用被判成了多余字段"


def test_invalid_json_is_left_to_the_sdk(ctx):
    """非法 JSON 不在这里抢答：官方会报"JSON 解析失败"，那才是准确的原因。"""
    ctx.steps.note_calls([("save_text_asset", "{not json", "call_1")])
    call_ctx = ToolContext(
        context=ctx,
        usage=Usage(),
        tool_name="save_text_asset",
        tool_call_id="call_1",
        tool_arguments="{not json",
    )
    out = asyncio.run(T.save_text_asset.on_invoke_tool(call_ctx, "{not json"))
    assert "不认识这些参数" not in out
    assert "JSON" in out


def test_without_raw_arguments_nothing_is_checked():
    """拿不到原始串（形态不认识）⇒ 跳过检查。

    宁可漏抓一次，也不能因为"这个 ctx 形状陌生"把一个正常调用拒掉 —— 那个方向的
    错误会让工具整体不可用。
    """
    assert T._extra_args("x", T.save_text_asset.__wrapped__, object()) == frozenset()


# --------------------------------------------------------------------------- #
# 二、空内容 / 空交付
# --------------------------------------------------------------------------- #
def test_empty_text_content_is_a_contract_error(ctx):
    out = _call(T.save_text_asset, ctx, {"name": "a", "content": "   "})

    assert "content 是空的" in out
    assert _settled(ctx) == [("save_text_asset", "failed", "bad_args")]
    assert list(Path(ctx.asset_dir).iterdir()) == [], "空内容还是把文件写出去了"


def test_empty_image_prompt_is_a_contract_error(ctx):
    out = _call(T.generate_image, ctx, {"prompt": "  "})

    assert "prompt 是空的" in out
    assert _settled(ctx) == [("generate_image", "failed", "bad_args")]


def test_empty_delivery_is_a_contract_error(ctx):
    """★★ 这条是"隐形失败"里代价最高的：用户收到空白，而这一轮是成功的。"""
    out = _call(T.deliver, ctx, {"content": "  "})

    assert "content 是空的" in out
    assert ctx.delivered == {}, "空交付被登记了 ⇒ runner 会把空白发出去"
    assert _settled(ctx) == [("deliver", "failed", "bad_args")]


def test_a_contract_error_is_classified_as_bad_args():
    """归因必须是"模型填错了"，不能报成"工具实现坏了" —— 后者会把人引向翻代码。"""
    assert _failure_reason(ToolContractError("x")) == "bad_args"


# --------------------------------------------------------------------------- #
# 三、检索片段：有出处但没有正文
# --------------------------------------------------------------------------- #
class _Retriever:
    def __init__(self, hits):
        self.hits = hits

    def search(self, query, top_k=3):  # noqa: ANN001, ARG002
        return self.hits


def test_hits_without_text_are_not_a_hit(ctx, sink):
    """★★ "检索到了"不等于"拿到了能用的东西"。

    索引损坏 / 解析退化时，片段会带着出处却没有正文。那种结果拼进上下文就是一堆空
    条目，而模型会把它当成"查过了、没有结论"继续往下写。
    """
    ctx.retriever_factory = lambda: _Retriever([({"source": "a.md", "content": "   "}, 0.9)])

    out = _call(T.kb_search, ctx, {"query": "x"})

    assert "没有正文" in out
    assert ctx.citations == [], "空片段被登记成引用了"
    assert [e[3] for e in sink.counters("rag_hits_total")][-1]["result"] == "unparsable"


def test_hits_with_text_keep_global_numbering(ctx):
    """正常路径不受影响：编号仍在第 1 条，且登记进 citations。"""
    ctx.retriever_factory = lambda: _Retriever(
        [({"source": "b.md", "content": "真正的正文", "page": 2}, 0.8)]
    )

    out = _call(T.kb_search, ctx, {"query": "y"})

    assert "[1]" in out and "真正的正文" in out
    assert [(c["n"], c["source"]) for c in ctx.citations] == [(1, "b.md")]


# --------------------------------------------------------------------------- #
# 四、结构化输出的字段级约束
# --------------------------------------------------------------------------- #
def test_a_textual_source_is_rejected():
    """``source="来源一"`` 解析不出编号 ⇒ 结论无法回溯，但整轮曾经是"成功"的。"""
    with pytest.raises(ValidationError):
        Finding(claim="x", evidence="y", source="来源一")


def test_numeric_sources_are_accepted():
    for value in ("[1]", "1", "[1][2]", "1,2"):
        assert Finding(claim="x", evidence="y", source=value).source == value


def test_an_empty_deck_is_rejected():
    """空 slides 会渲染出一份空 PPT，一路走到用户打开才发现。"""
    with pytest.raises(ValidationError):
        SlideDeck(title="标题", slides=[])
