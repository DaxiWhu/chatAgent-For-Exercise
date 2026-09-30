"""KnowledgeChunk → 旧 dict 结构 的兼容层（产物出边界的唯一收口点）。

为什么需要它：检索层（BM25Retriever / EmbeddingRetriever / HybridRetriever）与
graph 的 retrieve 节点只认 c["text"] 与 c["source"]，多个测试还断言
set(chunk) >= {"source", "text"}。引入 Docling 后产物字段变多，但这三个旧字段的
**语义不能变**，否则检索、生成以及 101 个既有测试会一起断。

契约：
  1. 返回值必含 "source" 与 "text"；
  2. "text" 取 retrieval_text —— 这是向量化输入，面向检索的自然语言；
  3. "section" 取 section_path，兼容旧字段名；
  4. 其余字段一律**追加**，旧消费者忽略即可。

注意：不是所有消费方都走这里。pdfminer 降级路径经由 from_pdfminer_pieces 产出
KnowledgeChunk 后同样过本函数，这样两条链路的出参结构完全一致。
"""
from __future__ import annotations

from typing import Iterable

from .knowledge_chunk import KnowledgeChunk, KIND_UNPROCESSABLE

# 旧消费者依赖的最小字段集，测试也断言这个
LEGACY_REQUIRED = ("source", "text")


def to_legacy(kc: KnowledgeChunk) -> dict:
    """KnowledgeChunk → dict。asdict 已带全部 v3 字段，这里只补两个旧别名。

    kc: 待转换的单个块，必须是 KnowledgeChunk 实例（不是 dict）—— 实现依赖它的
        to_dict() / section_path / retrieval_text 三个成员，传 dict 会直接
        AttributeError（此处不做类型兜底，属契约内约定）。
    """
    d = kc.to_dict()
    d["section"] = kc.section_path       # 旧字段名，语义相同（标题路径）
    d["text"] = kc.retrieval_text        # ★ 检索层只认 text
    return d


def to_legacy_many(chunks: Iterable[KnowledgeChunk]) -> list[dict]:
    """批量转换，保持输入顺序（块序即阅读顺序，下游可能依赖）。

    chunks: KnowledgeChunk 的可迭代对象（list / tuple / 生成器均可）。
        空输入返回 []；实现是一次列表推导，不预先取长度，所以传生成器也安全。
        注意**不去重**：chunk_id 重复时保留全部，去重是下游向量库的职责。
    """
    return [to_legacy(c) for c in chunks]


def summarize(chunks: Iterable[KnowledgeChunk]) -> dict:
    """按 parser / kind 统计，供灌库日志。

    存在的理由：降级与失败必须可见。只看"灌进去多少块"看不出"有多少文件其实
    走了 pdfminer 降级链路"或"有多少文件根本没解析出来"——这两件事都要能一眼看到。

    chunks: 待统计的块，**必须是 KnowledgeChunk 而非其 dict 形态** —— 统计读的是
        c.parser / c.kind / c.source / c.unprocessable_reason 四个对象属性，
        传 to_legacy 之后的 dict 会 AttributeError。通常在 to_legacy_many 之前调用。
    """
    by_parser: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    failed: list[str] = []
    for c in chunks:
        by_parser[c.parser or "?"] = by_parser.get(c.parser or "?", 0) + 1
        by_kind[c.kind] = by_kind.get(c.kind, 0) + 1
        if c.kind == KIND_UNPROCESSABLE:
            failed.append(f"{c.source}: {c.unprocessable_reason}")
    return {
        "total": sum(by_parser.values()),
        "by_parser": by_parser,
        "by_kind": by_kind,
        "failed": failed,
    }


def format_summary(summary: dict) -> str:
    """把 summarize 的结果排成一行日志文本。

    summary: summarize() 的返回值，或手工构造的同形 dict（测试与脚本常这么用）。
        by_parser / by_kind / failed 三键用 .get 读取，缺失时对应片段不追加；
        total 是硬取值（summary['total']），缺失会 KeyError。
    """
    parts = [f"共 {summary['total']} 块"]
    if summary.get("by_parser"):
        parts.append("来源 " + ", ".join(f"{k}={v}" for k, v in summary["by_parser"].items()))
    if summary.get("by_kind"):
        parts.append("类型 " + ", ".join(f"{k}={v}" for k, v in summary["by_kind"].items()))
    line = "；".join(parts)
    if summary.get("failed"):
        line += f"；未解析 {len(summary['failed'])} 个：" + " | ".join(summary["failed"])
    return line
