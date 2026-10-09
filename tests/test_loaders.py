"""多格式解析测试：全部离线，各格式文件由测试现场生成。"""

import pytest

from src.rag.loaders import load_text, supported_extensions
from src.rag.retrieval import load_chunks


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
def test_supported_extensions_cover_expected_formats():
    exts = supported_extensions()
    for e in [".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".html", ".htm",
              ".docx", ".xlsx", ".pptx"]:
        assert e in exts, f"缺少扩展名 {e}"


# ---------------------------------------------------------------------------
# 纯文本 + 编码兜底
# ---------------------------------------------------------------------------
def test_plain_text_utf8(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("梧州特产有六堡茶。", encoding="utf-8")
    assert "六堡茶" in load_text(str(p))


def test_plain_text_gbk_fallback(tmp_path):
    """中文 Windows 常见 GBK 文件必须能读出来，否则整篇会变乱码。"""
    p = tmp_path / "gbk.txt"
    p.write_bytes("梧州特产有龟苓膏。".encode("gb18030"))
    text = load_text(str(p))
    assert "龟苓膏" in text
    assert "\ufffd" not in text  # 不应出现替换字符


def test_markdown_and_unknown_extension(tmp_path):
    md = tmp_path / "a.md"
    md.write_text("# 标题\n\n正文", encoding="utf-8")
    assert "标题" in load_text(str(md))

    weird = tmp_path / "a.weird"
    weird.write_text("fallback content", encoding="utf-8")
    assert "fallback" in load_text(str(weird))  # 未知扩展名按文本兜底


# ---------------------------------------------------------------------------
# CSV / TSV
# ---------------------------------------------------------------------------
def test_csv_becomes_header_plus_kv_rows(tmp_path):
    p = tmp_path / "data.csv"
    p.write_text("城市,特产,气候\n梧州,六堡茶,亚热带\n桂林,米粉,亚热带\n", encoding="utf-8")
    text = load_text(str(p))
    assert "表头" in text
    assert "城市=梧州" in text
    assert "特产=六堡茶" in text


def test_tsv(tmp_path):
    p = tmp_path / "data.tsv"
    p.write_text("a\tb\n1\t2\n", encoding="utf-8")
    text = load_text(str(p))
    assert "a=1" in text and "b=2" in text


def test_csv_empty(tmp_path):
    p = tmp_path / "empty.csv"
    p.write_text("", encoding="utf-8")
    assert load_text(str(p)) == ""


# ---------------------------------------------------------------------------
# JSON / JSONL
# ---------------------------------------------------------------------------
def test_json_is_flattened_to_key_value_lines(tmp_path):
    p = tmp_path / "d.json"
    p.write_text('{"city":"梧州","tags":["六堡茶","龟苓膏"],"info":{"pop":300}}',
                 encoding="utf-8")
    text = load_text(str(p))
    assert "city: 梧州" in text
    assert "tags[0]: 六堡茶" in text
    assert "info.pop: 300" in text


def test_jsonl(tmp_path):
    p = tmp_path / "d.jsonl"
    p.write_text('{"a":1}\n{"a":2}\nnot-json-line\n', encoding="utf-8")
    text = load_text(str(p))
    assert "a: 1" in text
    assert "a: 2" in text
    assert "not-json-line" in text  # 非法行按原文保留，不丢内容


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def test_html_to_text_keeps_headings_drops_script(tmp_path):
    p = tmp_path / "a.html"
    p.write_text(
        "<html><head><title>梧州简介</title>"
        "<script>var x=1;</script><style>.a{}</style></head>"
        "<body><h1>梧州</h1><h2>特产</h2><p>六堡茶很有名。</p>"
        "<ul><li>龟苓膏</li></ul></body></html>",
        encoding="utf-8",
    )
    text = load_text(str(p))
    assert "# 梧州简介" in text
    assert "# 梧州" in text
    assert "## 特产" in text
    assert "六堡茶很有名。" in text
    assert "龟苓膏" in text
    assert "var x=1" not in text      # script 必须被剔除
    assert ".a{}" not in text         # style 必须被剔除


# ---------------------------------------------------------------------------
# Word / Excel / PPT
# ---------------------------------------------------------------------------
def test_docx_headings_paragraphs_tables(tmp_path):
    docx = pytest.importorskip("docx")
    p = tmp_path / "a.docx"
    doc = docx.Document()
    doc.add_heading("梧州概况", level=1)
    doc.add_paragraph("梧州位于广西东部。")
    t = doc.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "特产"
    t.cell(0, 1).text = "说明"
    t.cell(1, 0).text = "六堡茶"
    t.cell(1, 1).text = "黑茶的一种"
    doc.save(str(p))

    text = load_text(str(p))
    assert "# 梧州概况" in text        # 标题层级被保住
    assert "梧州位于广西东部。" in text
    assert "[表格 1]" in text
    # 表格改成"表头感知"：每一行都带列名。
    # 旧断言（"六堡茶 | 黑茶的一种"）只能证明值读出来了，
    # 而列名只出现在首行 —— 切开之后那些块里没人知道每列是什么。
    assert "表头：特产 | 说明" in text
    assert "特产=六堡茶 | 说明=黑茶的一种" in text


def test_xlsx_sheets(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    p = tmp_path / "a.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "特产表"
    ws.append(["城市", "特产"])
    ws.append(["梧州", "六堡茶"])
    wb.save(str(p))

    text = load_text(str(p))
    assert "## 工作表：特产表" in text
    # 同理改成表头感知：`城市=梧州` 才知道梧州是城市，`梧州 | 六堡茶` 不知道
    assert "表头：城市 | 特产" in text
    assert "城市=梧州 | 特产=六堡茶" in text


def test_pptx_slides(tmp_path):
    pptx = pytest.importorskip("pptx")
    p = tmp_path / "a.pptx"
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_textbox(0, 0, 100, 100).text_frame.text = "梧州特产：六堡茶"
    prs.save(str(p))

    text = load_text(str(p))
    assert "## 第 1 页" in text
    assert "六堡茶" in text


# ---------------------------------------------------------------------------
# load_chunks 集成
# ---------------------------------------------------------------------------
def test_load_chunks_picks_up_multiple_formats(tmp_path):
    (tmp_path / "a.md").write_text("# 梧州\n\n六堡茶。", encoding="utf-8")
    (tmp_path / "b.csv").write_text("城市,特产\n梧州,龟苓膏\n", encoding="utf-8")
    (tmp_path / "c.json").write_text('{"景点":"骑楼城"}', encoding="utf-8")
    (tmp_path / "d.html").write_text("<h1>龙母庙</h1>", encoding="utf-8")

    chunks = load_chunks(str(tmp_path))
    sources = {c["source"] for c in chunks}
    assert sources == {"a.md", "b.csv", "c.json", "d.html"}
    alltext = "".join(c["text"] for c in chunks)
    assert "龟苓膏" in alltext and "骑楼城" in alltext and "龙母庙" in alltext
    assert all(set(c) >= {"source", "text"} for c in chunks)


def test_load_chunks_skips_unsupported_extension(tmp_path, caplog):
    (tmp_path / "good.md").write_text("梧州有骑楼城。", encoding="utf-8")
    (tmp_path / "bad.exe").write_bytes(b"\x00\x01\x02binary")

    chunks = load_chunks(str(tmp_path))
    assert {c["source"] for c in chunks} == {"good.md"}


def test_load_chunks_skips_empty_file(tmp_path):
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    assert load_chunks(str(tmp_path)) == []
