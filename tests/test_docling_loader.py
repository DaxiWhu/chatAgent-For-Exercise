"""Docling 解析链路测试：需要 docling + 模型权重就绪，否则跳过。

这些用例会真实加载模型（CPU 上每页有秒级开销），所以只用最小 PDF 验证链路通不通，
不做语料级精度验证——表格识别要用真实带表文档，见 scripts/smoke_docling.py。

环境未就绪时**跳过**而不是失败，但只对"权重没下齐"这类问题跳过：
其他失败照常变红，否则真实回归会被一句 skip 悄悄掩盖。
"""
import pytest

pytest.importorskip("docling")

from src import docling_loader  # noqa: E402
from src.chunk_writer import to_legacy  # noqa: E402
from tests.test_pdf_parser import make_pdf  # noqa: E402  复用手搓 PDF 的手法

# 只认"权重缺失"。列得越窄越好——放太宽会把真实回归也一起 skip 掉。
_ENV_NOT_READY_MARKERS = (
    "artifacts not found or incomplete",   # RapidOCR 的报错原文
    "does not exist",                      # RapidOCR 的 pinned 路径检查
    "No such file or directory",
    "artifacts_path",
    "Failed to download",
)


def _env_not_ready(reason: str) -> bool:
    r = reason or ""
    return any(m in r for m in _ENV_NOT_READY_MARKERS)


@pytest.fixture(scope="module")
def available():
    """只确认能 import docling。不加载任何模型，给不需要权重的用例用。"""
    if not docling_loader.docling_available():
        pytest.skip("docling 未安装")
    return True


@pytest.fixture(scope="module")
def simple_pdf(tmp_path_factory):
    return make_pdf(tmp_path_factory.mktemp("docling") / "simple.pdf",
                    ["Hello Docling World", "Second line here"])


@pytest.fixture(scope="module")
def parsed(available, simple_pdf):
    """整模块只解析一次，并兼任环境就绪判定。

    为什么要真跑一遍、而不是只 get_converter()：
    docling 的模型在**首次 convert 时才实例化**，converter 建得起来不代表权重齐——
    RapidOCR 就是这样漏掉的（fixture 说就绪，用例却红，排查半天）。
    顺带省掉三个用例各自的模型推理开销。

    注意本样本文字层不足 50 字符，DOCLING_OCR=auto 会判为扫描件而开 OCR，
    因此这条链路天然覆盖了 RapidOCR，权重缺失时会在这里暴露。
    """
    out = docling_loader.parse_document(simple_pdf, "simple.pdf", kind="pdf")
    if out.status == "unavailable":
        pytest.skip(f"docling 不可用：{out.reason}")
    if out.status == "failed" and _env_not_ready(out.reason):
        pytest.skip(f"模型权重未就绪，先跑 scripts/fetch_docling_models.py：{out.reason}")
    return out


def test_docling_available(available):
    assert docling_loader.docling_available() is True


def test_parse_document_succeeds(parsed):
    assert parsed.status in ("ok", "partial"), f"status={parsed.status} reason={parsed.reason}"
    assert parsed.chunks
    assert parsed.page_total == 1


def test_chunks_satisfy_legacy_contract(parsed):
    """出边界的 dict 必须含 source 与 text —— 检索层只认这两个键。"""
    for d in (to_legacy(c) for c in parsed.chunks):
        assert set(d) >= {"source", "text"}
        assert d["source"] == "simple.pdf"
        assert d["text"]


def test_chunk_metadata_populated(parsed):
    kc = parsed.chunks[0]
    assert kc.parser == "docling"
    assert kc.origin == "docling_pdf"
    assert kc.page == 1
    assert kc.bbox, "PDF 链路一定带坐标，否则无法溯源回原文位置"
    assert kc.chunk_id


def test_tokenizer_implements_required_interface(available):
    """HybridChunker 依赖 BaseTokenizer 的三个方法，自研兜底也必须实现。"""
    tok = docling_loader.build_tokenizer()
    assert tok.get_max_tokens() == docling_loader.DOC_CHUNK_TOKENS
    assert tok.count_tokens("中文测试文本") > 0


def test_auto_ocr_decision_follows_text_layer(available, simple_pdf, tmp_path, monkeypatch):
    """auto 模式的判据是"文字层是否够厚"：薄了当扫描件开 OCR，厚了不开。

    这条不打模型，只锁行为——阈值或判据被改坏时显式失败，
    避免"给正常电子版 PDF 白跑 OCR"或反过来"扫描件漏了 OCR"。
    """
    monkeypatch.setattr(docling_loader, "DOCLING_OCR", "auto")
    assert docling_loader._want_ocr(simple_pdf, "pdf") is True, "本样本文字层很薄，应判为需 OCR"

    long_pdf = make_pdf(tmp_path / "long.pdf", [
        "This paragraph is deliberately long enough to exceed the fifty",
        "character threshold used by the text layer probe, so OCR is not",
        "needed at all for this particular electronic document.",
    ])
    assert docling_loader._want_ocr(long_pdf, "pdf") is False


def test_ocr_never_enabled_for_docx(available, monkeypatch):
    """docx 是 markup 格式，文字是结构化的，任何模式下都不该走 OCR。"""
    monkeypatch.setattr(docling_loader, "DOCLING_OCR", "auto")
    assert docling_loader._want_ocr("whatever.docx", "docx") is False


def test_missing_file_reports_failure(available, tmp_path):
    out = docling_loader.parse_document(str(tmp_path / "nope.pdf"), "nope.pdf")
    assert out.status == "failed"
    assert not out.chunks


def test_scanned_pdf_text_is_recovered_by_ocr(available, tmp_path, monkeypatch):
    """造一份"纯图片 PDF"（无文字层），验证 OCR 链路真能把文字捞回来。

    为什么值得单独一条：RapidOCR 的权重是**懒加载**的，缺了不会在 import 或建
    converter 时报错，只在真跑到扫描件那一刻才炸——电子版 PDF 的用例永远暴露不了它。
    另外这里断言的是"文字确实回来了"而不是"status 是 ok"：
    只看状态的话，OCR 静默失效、返回空文本也会绿。

    造件直接复用 scripts/check_ocr.py 的实现（PIL 把文字画成位图再存 PDF）：
    同一份假扫描件只该有一个来源，否则测试和自检脚本会慢慢对不上。
    """
    pytest.importorskip("PIL")
    monkeypatch.setattr(docling_loader, "DOCLING_OCR", "auto")

    from scripts.check_ocr import build_scanned_pdf
    scanned = build_scanned_pdf(str(tmp_path / "scanned.pdf"))

    # 前置断言：确认真的没有文字层了，否则这条用例测的还是老链路
    from src.pdf_parser import extract_pdf_text
    assert not (extract_pdf_text(scanned) or "").strip(), "栅格化后不该残留文字层"
    assert docling_loader._want_ocr(scanned, "pdf") is True

    out = docling_loader.parse_document(scanned, "scanned.pdf", kind="pdf")
    assert out.status in ("ok", "partial"), f"status={out.status} reason={out.reason}"
    text = " ".join(c.retrieval_text for c in out.chunks)
    low = text.lower()
    assert any(k in low for k in ("docling", "invoice", "hello")), f"OCR 没捞回文字：{text!r}"


def test_doc_parser_override_disables_docling(available, simple_pdf, monkeypatch):
    """RAG_DOC_PARSER=pdfminer 时应直接判 unavailable，让调用方走降级链路。"""
    monkeypatch.setattr(docling_loader, "DOC_PARSER", "pdfminer")
    out = docling_loader.parse_document(simple_pdf, "simple.pdf")
    assert out.status == "unavailable"
    assert "pdfminer" in out.reason


# ---------------------------------------------------------------------------
# docx 双路线（A: Docling docx 后端 + C: 解 zip 取媒体）
#
# 素材用手拼包（tests/docx_fixtures.py）：python-docx 写不了原生图表与 OLE，
# 而这两类恰恰是路线 C 存在的理由，也是最容易静默丢失的部分。
# 媒体导出目录改到 tmp：默认会写进仓库的 data/.media/，测试不该留下痕迹。
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def parsed_docx(available, tmp_path_factory):
    pytest.importorskip("openpyxl")            # 夹具里的 OLE 内层是真 xlsx
    from src import docx_media
    from tests.docx_fixtures import build_minimal_docx

    docx_media.MEDIA_DIR = str(tmp_path_factory.mktemp("docx_media"))
    path = build_minimal_docx(str(tmp_path_factory.mktemp("docx") / "report.docx"))
    out = docling_loader.parse_docx(path, "report.docx")
    if out.status == "unavailable":
        pytest.skip(f"docling 不可用：{out.reason}")
    if out.status == "failed" and _env_not_ready(out.reason):
        pytest.skip(f"模型权重未就绪，先跑 scripts/fetch_docling_models.py：{out.reason}")
    return out


def _by_kind(chunks, kind):
    return [c for c in chunks if c.kind == kind]


def test_docx_parse_succeeds(parsed_docx):
    assert parsed_docx.status in ("ok", "partial"), \
        f"status={parsed_docx.status} reason={parsed_docx.reason}"
    assert parsed_docx.chunks


def test_docx_both_routes_contribute(parsed_docx):
    """A 与 C 必须都出力：只有 A 说明解 zip 没接上；只有 C 说明正文链路断了。"""
    origins = {c.origin for c in parsed_docx.chunks}
    assert "docx_zip" in origins, "解 zip 路线没有产出（图片/图表/OLE 会全丢）"
    assert "docling_docx" in origins, "Docling 正文路线没有产出"


def test_docx_chart_values_are_exact(parsed_docx):
    """图表数值必须逐项精确 —— 这是"确定性优先于 VLM"的立足点。

    只断言"有内容"是不够的：把 c:f 公式串（Sheet1!$A$2）当数据读也会产出内容，
    而且不报错。所以这里比对的是造件时写进 chart XML 的确切数值。
    """
    from tests.docx_fixtures import CHART_CATS, CHART_TITLE, CHART_VALS, SERIES_NAME

    charts = _by_kind(parsed_docx.chunks, "chart")
    assert len(charts) == 1
    chart = charts[0]
    assert chart.needs_vlm is False, "数据已在 XML 里，不该再花 VLM 的钱"
    assert "来自XML" in chart.tags
    assert CHART_TITLE in chart.retrieval_text
    assert SERIES_NAME in chart.retrieval_text
    for cat, val in zip(CHART_CATS, CHART_VALS):
        assert f"{cat}={val}" in chart.retrieval_text
        assert cat in chart.table_md and val in chart.table_md


def test_docx_image_alt_text_removes_vlm_need(parsed_docx):
    """作者填的替代文字是确定性描述，有它就不必送 VLM。"""
    from tests.docx_fixtures import ALT_IMAGE

    pics = _by_kind(parsed_docx.chunks, "diagram")
    assert pics, "图片未被抽出"
    assert pics[0].alt_text == ALT_IMAGE
    assert pics[0].needs_vlm is False
    # 原件要真的落盘，否则批次6 想描述也没图可读
    import os
    assert pics[0].source_image and os.path.isfile(pics[0].source_image)


def test_docx_ole_inner_workbook_becomes_searchable_text(parsed_docx):
    """OLE 的价值在于内层原件能被解析成文字 —— 比识别一张截图准得多。"""
    from tests.docx_fixtures import OLE_CELL_TEXT

    tables = _by_kind(parsed_docx.chunks, "table")
    hits = [c for c in tables if "已解析原件" in c.tags]
    assert hits, "OLE 内层没有被解析出来"
    assert OLE_CELL_TEXT in hits[0].retrieval_text
    assert hits[0].needs_vlm is False


def test_docx_ole_preview_is_not_a_second_chunk(parsed_docx):
    """OLE 的 VML 预览位图与原件是同一份数据，不能各占一个块。"""
    emf = [c for c in parsed_docx.chunks if (c.source_image or "").lower().endswith((".emf", ".wmf"))]
    assert not emf, f"OLE 预览位图仍单独成块：{[c.source_image for c in emf]}"


def test_docx_media_keeps_position_and_context(parsed_docx):
    """docx 没有页码，位置只能靠"第几段 + 上下文锚点"表达，两者都不能丢。"""
    from tests.docx_fixtures import PARA_BEFORE_CHART, PARA_BEFORE_IMAGE

    chart = _by_kind(parsed_docx.chunks, "chart")[0]
    pic = _by_kind(parsed_docx.chunks, "diagram")[0]
    assert chart.para_index == 2 and chart.anchor_text == PARA_BEFORE_CHART
    assert pic.para_index == 4 and pic.anchor_text == PARA_BEFORE_IMAGE


def test_docx_chunks_satisfy_legacy_contract(parsed_docx):
    """出边界的 dict 必须含 source 与 text —— 检索层只认这两个键。

    这条对 docx 尤其重要：媒体块最容易只填 content（保真事实）而忘了
    retrieval_text（检索入口），那样它在索引里等于不存在。
    """
    for c in parsed_docx.chunks:
        d = to_legacy(c)
        assert set(d) >= {"source", "text"}
        assert d["source"] == "report.docx"
        assert d["text"].strip(), f"块 {c.chunk_id}（{c.kind}）出边界后 text 为空"
