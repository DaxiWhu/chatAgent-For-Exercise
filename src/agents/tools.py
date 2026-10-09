"""普通 function tools —— **确定性能力不做成 agent**。

判据（官方 ``research_bot`` 就是这么写的）：**需要模型"拿主意"的才配 agent，
其余都是函数。**

* ``kb_search``：模型要决定"查什么、查几次" ⇒ 它是**工具**（不是「检索 agent」）。
  真正的检索动作是确定性的，工具就够了；让它变成 agent 只会多一跳。
* ``render_deck``：没有任何模型需要"决定要不要渲染" ⇒ 它是**普通函数**，
  连工具都不该暴露（暴露了只是给模型多一个可以忘掉的选项）。

与示例版的差别（生产化必须的）
------------------------------
1. **检索接真实知识库**：走 ``TaskContext.retriever_factory``（按租户懒加载的检索器），
   而不是内置的小词表。检索失败 / 空召回 / 耗时全部上报（见 `src/observability.py`）。
2. **注入防护复用 RAG 链路的 ``guard``**：工具返回值直接进模型上下文，是注入的
   真实入口。这里用与 ``graph.generate`` **同一套** ``wrap_context``，两条链路
   的防护口径一致。
3. **引用编号全局唯一**：多次检索的编号要连续累加，否则出口护栏（引用可追溯）
   会把第二条检索的第 [1] 条误判成"没检索过"。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from agents import RunContextWrapper, function_tool

from .. import observability
from ..envutil import env_int
from ..rag import guard
from ..rag.retrieval import build_citations
from .context import TaskContext

__all__ = ["generate_image", "kb_search", "render_deck", "render_slide_deck", "save_text_asset"]

DEFAULT_TOP_K = env_int("AGENT_KB_TOP_K", 3, lo=1)
"""单次检索返回的片段数上限。与 ``graph.TOP_K`` 同量级：agent 链路可以多查几轮，
单轮不必更大。"""

_SLUG_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff_-]+")


def _safe_name(name: str, fallback: str) -> str:
    """把任意字符串变成一个安全的文件名主干（防目录穿越 / 非法字符）。"""
    cleaned = _SLUG_RE.sub("_", (name or "").strip())[:60].strip("_")
    return cleaned or fallback


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------- #
# 检索：真实知识库（按租户分片的检索器由上下文注入）
# --------------------------------------------------------------------------- #
@function_tool
def kb_search(ctx: RunContextWrapper[TaskContext], query: str, top_k: int = DEFAULT_TOP_K) -> str:
    """在知识库里检索与 query 相关的原文片段，返回带出处编号（形如 [1]）的条目。

    这是**唯一**获取事实的途径：凡是要写进结论的数字、配置项、接口路径，
    都必须来自本工具的返回；检索不到就如实说检索不到。

    Args:
        query: 检索问题。关键词或自然语言都可以。
        top_k: 最多返回几条片段。
    """
    context = ctx.context
    if not isinstance(context, TaskContext) or context.retriever_factory is None:
        observability.counter("rag_hits_total", result="unavailable", via="agent")
        return "检索不可用：本实例未配置知识库（DOCS_DIR 为空或检索器装配失败）。"

    limit = max(1, min(int(top_k or DEFAULT_TOP_K), 10))
    context.stats.retrieval_calls += 1
    started = time.perf_counter()
    try:
        hits = context.retriever_factory().search(query, top_k=limit)
    except Exception as e:  # noqa: BLE001  检索失败不该把整条 agent 链路带崩
        observability.counter("rag_hits_total", result="error", via="agent")
        observability.counter("rag_retrieval_degraded_total", stage="agent_search")
        logging.warning("agent 链路检索失败，按检索不到处理：%s", e)
        return "检索失败（知识库暂时不可用），无法提供带出处的结论。"
    finally:
        observability.histogram(
            "rag_retrieval_duration_seconds", time.perf_counter() - started, via="agent"
        )

    if not hits:
        observability.counter("rag_hits_total", result="empty", via="agent")
        context.stats.empty_retrievals += 1
        logging.info("agent 链路检索空召回（query=%.60s）", query)
        return "检索不到与这个问题相关的片段。"

    observability.counter("rag_hits_total", result="hit", via="agent")
    context.stats.retrieved_chunks += len(hits)

    # 引用编号**全局唯一**：多次检索时按已有条数偏移。不偏移的话，每轮检索都从
    # [1] 重新编号，出口护栏会把第二次检索的 [1] 当成"重复编号"或漏判。
    offset = len(context.citations)
    fresh = build_citations(hits)
    for i, cite in enumerate(fresh, start=1):
        cite["n"] = offset + i
    context.citations.extend(fresh)

    body = "\n\n".join(
        f"[{offset + i}] {c.get('source', '?')}"
        + (f"（第 {c['page']} 页）" if c.get("page") else "")
        + f"\n{c.get('snippet', '')}"
        for i, c in enumerate(fresh, start=1)
    )
    # 注入防护：工具返回的文本会**直接进模型上下文**，这是外部内容进入模型的真实
    # 入口。用与 graph.generate 同一套 wrap_context（结构边界 + 按策略处理），
    # 保证两条链路的防护口径一致 —— 只在其中一条上防护等于没防护。
    return guard.wrap_context(body)


# --------------------------------------------------------------------------- #
# 落盘：确定性步骤
# --------------------------------------------------------------------------- #
@function_tool
def save_text_asset(ctx: RunContextWrapper[TaskContext], name: str, content: str) -> str:
    """把一段文本落盘成资产文件，返回资产路径。

    Args:
        name: 文件名主干（不含扩展名）。
        content: 文本正文。
    """
    context = ctx.context
    base = context.asset_dir if isinstance(context, TaskContext) else Path("var/agent_assets")
    path = _ensure_dir(Path(base)) / f"{_safe_name(name, 'asset')}.txt"
    # newline=""：与项目"源码/落盘一律 LF"的纪律一致；Windows 上默认会把 \n 换成 \r\n。
    path.write_text(content, encoding="utf-8", newline="")
    if isinstance(context, TaskContext):
        context.assets.append(str(path))
        context.stats.assets += 1
    observability.counter("agent_assets_total", kind="text")
    return str(path)


@function_tool
def generate_image(ctx: RunContextWrapper[TaskContext], prompt: str, style: str = "flat") -> str:
    """按画面描述生成一张插图，返回资产路径。

    ⚠️ 当前是**占位实现**：只把生成参数写成一份 sidecar 文件（``*.prompt.txt``），
    不调用任何外部图像服务。接入真实图像生成能力时，替换本函数体即可 ——
    契约（入参 prompt/style，出参资产路径，并登记到 ``TaskContext.assets``）不变。

    Args:
        prompt: 画面描述（主体 / 构图 / 光线）。
        style: 风格关键词。
    """
    context = ctx.context
    base = context.asset_dir if isinstance(context, TaskContext) else Path("var/agent_assets")
    path = _ensure_dir(Path(base)) / f"{_safe_name(prompt, 'image')}.prompt.txt"
    path.write_text(
        f"[placeholder image]\nprompt: {prompt}\nstyle: {style}\n",
        encoding="utf-8",
        newline="",
    )
    if isinstance(context, TaskContext):
        context.assets.append(str(path))
        context.stats.assets += 1
    observability.counter("agent_assets_total", kind="image_placeholder")
    return str(path)


def render_slide_deck(deck_json: str, asset_dir: Path | str) -> str:
    """把结构化大纲渲染成 Markdown 文件（确定性步骤）。

    deck_json: ``SlideDeck`` 的 JSON 串（``model_dump_json()``）。
    asset_dir: 落盘目录。

    刻意**不做成 ``@function_tool``**：没有任何模型需要"决定要不要渲染"。
    暴露成工具只会给模型多一个可以忘掉的选项 —— 那正是"交付物缺失"这类
    偶发失败的来源。
    """
    deck = json.loads(deck_json)
    lines = [f"# {deck.get('title', '')}", ""]
    if deck.get("subtitle"):
        lines += [f"_{deck['subtitle']}_", ""]
    for index, slide in enumerate(deck.get("slides", []), start=1):
        lines.append(f"## {index}. {slide.get('title', '')}")
        lines += [f"- {item}" for item in slide.get("bullets", [])]
        if slide.get("speaker_notes"):
            lines.append(f"  > 备注：{slide['speaker_notes']}")
        lines.append("")
    path = _ensure_dir(Path(asset_dir)) / "deck.md"
    path.write_text("\n".join(lines), encoding="utf-8", newline="")
    observability.counter("agent_assets_total", kind="deck")
    return str(path)


#: 兼容别名：示例与文档里用的是 ``render_slide_deck`` 这个名字。
render_deck = render_slide_deck
