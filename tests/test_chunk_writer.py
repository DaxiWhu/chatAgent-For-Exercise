"""chunk_writer 兼容层测试：全部离线。

这里守的是**兼容红线**——检索层（BM25Retriever / EmbeddingRetriever / HybridRetriever）
与 graph 的 retrieve 节点只认 c["text"]、c["source"]，多个既有测试也断言
set(chunk) >= {"source","text"}。引入 Docling 后产物字段变多，但旧字段语义不能变。
"""
from src.rag.chunk_writer import (
    LEGACY_REQUIRED,
    format_summary,
    summarize,
    to_legacy,
    to_legacy_many,
)
from src.rag.knowledge_chunk import (
    KIND_TABLE,
    KIND_TEXT,
    KnowledgeChunk,
    from_pdfminer_pieces,
    unprocessable,
)


def test_to_legacy_always_carries_legacy_required_fields():
    kc = KnowledgeChunk(source="a.pdf", section_path="第一章", retrieval_text="检索文本")
    assert set(to_legacy(kc)) >= set(LEGACY_REQUIRED)


def test_to_legacy_maps_retrieval_text_not_content():
    """text 必须取 retrieval_text（面向检索），不能取 content（保真事实）。"""
    kc = KnowledgeChunk(source="a.pdf", content="| A | B |\n|---|---|", retrieval_text="A=1；B=2")
    d = to_legacy(kc)
    assert d["text"] == "A=1；B=2"
    assert d["content"] == "| A | B |\n|---|---|"


def test_to_legacy_keeps_section_alias_for_old_consumers():
    kc = KnowledgeChunk(source="a.pdf", section_path="第一章 > 概述")
    d = to_legacy(kc)
    assert d["section"] == "第一章 > 概述"
    assert d["section_path"] == "第一章 > 概述"


def test_to_legacy_passes_through_v3_fields():
    kc = KnowledgeChunk(
        source="a.pdf", page=7, kind=KIND_TABLE, bbox=[[1.0, 2.0, 3.0, 4.0]],
        table_md="| h |", parser="docling", origin="docling_pdf", needs_vlm=True,
    )
    d = to_legacy(kc)
    for f in ("page", "kind", "bbox", "table_md", "parser", "origin", "needs_vlm"):
        assert f in d, f"兼容层丢了字段 {f}"
    assert d["kind"] == KIND_TABLE and d["page"] == 7


def test_to_legacy_many_matches_single():
    kcs = from_pdfminer_pieces(["甲", "乙"], "x.pdf")
    many = to_legacy_many(kcs)
    assert len(many) == 2
    assert all(d["source"] == "x.pdf" and d["text"] for d in many)


def test_legacy_dict_survives_retriever_style_access():
    """模拟检索器的实际访问方式：只碰 source / text / get("section")。"""
    d = to_legacy(KnowledgeChunk(source="s.pdf", section_path="S", retrieval_text="内容"))
    assert d["source"] == "s.pdf"
    assert d["text"] == "内容"
    assert d.get("section") == "S"


# ---------------------------------------------------------------------------
# summarize / format_summary：让降级与失败可见
# ---------------------------------------------------------------------------
def test_summarize_counts_by_parser_and_kind():
    chunks = (
        from_pdfminer_pieces(["甲", "乙"], "a.pdf")
        + [KnowledgeChunk(source="b.pdf", kind=KIND_TABLE, parser="docling")]
    )
    s = summarize(chunks)
    assert s["total"] == 3
    assert s["by_parser"] == {"pdfminer": 2, "docling": 1}
    assert s["by_kind"][KIND_TEXT] == 2 and s["by_kind"][KIND_TABLE] == 1


def test_summarize_lists_unprocessable_files():
    """拒绝不能静默：汇总里必须能一眼看到哪些文件没解析出来。"""
    chunks = from_pdfminer_pieces(["正常内容"], "ok.pdf") + [
        unprocessable("bad.pdf", "疑似扫描件且未启用 OCR", parser="docling")
    ]
    s = summarize(chunks)
    assert len(s["failed"]) == 1
    assert "bad.pdf" in s["failed"][0]
    assert "疑似扫描件" in s["failed"][0]


def test_summarize_empty():
    s = summarize([])
    assert s["total"] == 0 and s["failed"] == []
    assert "共 0 块" in format_summary(s)


def test_format_summary_renders_parser_and_failures():
    chunks = from_pdfminer_pieces(["甲"], "a.pdf") + [unprocessable("z.pdf", "原因X")]
    line = format_summary(summarize(chunks))
    assert "pdfminer=1" in line
    assert "未解析 1 个" in line and "z.pdf" in line
