"""docx 路线 C（解 zip 取媒体）测试：零 docling 依赖，纯标准库 + 手拼夹具。

为什么要零依赖：这条支路是**降级链路的最后一环** —— 当 docling 不可用时，docx
的图片/图表/OLE 仍然要靠它。如果测试自己也依赖 docling，就等于"没装 docling 的环境
根本测不到它"，而那恰恰是它最需要工作的环境。

夹具见 tests/docx_fixtures.py：一份含原生图表 + 截图 + OLE 的最小 docx，
XML 按真实 Word 输出的形态写（含 c:f 公式串与 wp:docPr@descr）。
"""
import zipfile

import pytest

from src.rag import docx_media
from src.rag.docx_media import (
    DocxAsset,
    chart_to_table,
    chart_to_text,
    extract,
    media_dir_for,
    parse_chart_xml,
)
from tests.docx_fixtures import (
    ALT_CHART,
    ALT_IMAGE,
    ALT_OLE,
    CHART_CATS,
    CHART_CAT_FORMULA,
    CHART_TITLE,
    CHART_VALS,
    OLE_CELL_TEXT,
    OLE_CELL_VALUE,
    OLE_PROG_ID,
    PARA_BEFORE_CHART,
    PARA_BEFORE_IMAGE,
    PARA_BEFORE_OLE,
    PNG_1PX,
    SERIES_NAME,
    build_minimal_docx,
)


@pytest.fixture
def docx_path(tmp_path):
    return build_minimal_docx(str(tmp_path / "report.docx"))


@pytest.fixture
def assets(docx_path, tmp_path):
    """整模块共用的解析结果（一次解析，多个断言）。"""
    return extract(docx_path, media_dir=str(tmp_path / "media"))


def _by_kind(assets, kind):
    return [a for a in assets if a.kind == kind]


# ---------------------------------------------------------------------------
# 一、三类元素都要被找到
# ---------------------------------------------------------------------------
def test_extract_finds_chart_image_and_ole(assets):
    kinds = {a.kind for a in assets}
    assert kinds == {"chart", "image", "ole"}
    assert len(_by_kind(assets, "chart")) == 1
    assert len(_by_kind(assets, "ole")) == 1
    # 两张图：正文插图（a:blip）+ OLE 的图标预览（v:imagedata）——
    # 真实 Word 插入 OLE 时两者都会写，漏掉任一种都算回归
    assert len(_by_kind(assets, "image")) == 2


def test_assets_are_in_document_order(assets):
    assert [a.kind for a in assets] == ["chart", "image", "image", "ole"]


# ---------------------------------------------------------------------------
# 二、图表数据必须是确定性的、精确的
# ---------------------------------------------------------------------------
def test_chart_series_values_and_categories_are_exact(assets):
    chart = _by_kind(assets, "chart")[0].chart
    assert chart["chart_type"] == "lineChart"
    assert chart["type_cn"] == "折线图"
    assert chart["title"] == CHART_TITLE
    assert chart["categories"] == CHART_CATS
    assert [s["name"] for s in chart["series"]] == [SERIES_NAME]
    assert chart["series"][0]["values"] == CHART_VALS
    assert not chart["error"]


def test_series_name_is_not_read_from_formula_reference(assets):
    """回归：系列名的两个来源是两个不同的东西。

    `c:tx` 下既有 `c:strRef/c:f`（公式串 `Sheet1!$B$2:$B$5`）
    又有 `c:strCache`（真名字）。读错了会得到一个毫无意义的名字，
    而且不会报错——这类错误只能靠断言精确值来发现。
    """
    chart = _by_kind(assets, "chart")[0].chart
    name = chart["series"][0]["name"]
    assert name == SERIES_NAME
    assert "!" not in name and "Sheet1" not in name


def test_formula_string_never_leaks_into_derived_output(assets):
    """公式串（Sheet1!$A$2:$A$5）不是数据，任何产物里都不该出现。"""
    chart = _by_kind(assets, "chart")[0].chart
    for produced in (chart_to_table(chart), chart_to_text(chart)):
        assert CHART_CAT_FORMULA not in produced
        assert "Sheet1" not in produced


def test_chart_to_text_writes_category_value_pairs(assets):
    """文字化必须写成 "类目=数值"，否则"2021 年销售额多少"这类问题召不回来。"""
    text = chart_to_text(_by_kind(assets, "chart")[0].chart)
    assert "2021=138" in text
    assert "2019=120" in text
    assert SERIES_NAME in text
    assert CHART_TITLE in text


def test_chart_to_table_is_markdown_with_header(assets):
    md = chart_to_table(_by_kind(assets, "chart")[0].chart)
    lines = md.splitlines()
    assert lines[0].startswith("|") and "类目" in lines[0] and SERIES_NAME in lines[0]
    assert set(lines[1]) <= set("|- ")          # GFM 分隔行
    assert len(lines) == len(CHART_CATS) + 2


def test_parse_chart_xml_on_garbage_reports_error_instead_of_raising():
    info = parse_chart_xml("<c:chartSpace 这不是 xml".encode("utf-8"))
    assert info["series"] == []
    assert info["error"]


def test_parse_chart_xml_without_series_reports_error():
    xml = (b'<?xml version="1.0"?><c:chartSpace '
           b'xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart">'
           b'<c:chart><c:plotArea><c:lineChart/></c:plotArea></c:chart></c:chartSpace>')
    info = parse_chart_xml(xml)
    assert info["chart_type"] == "lineChart"
    assert info["series"] == []
    assert info["error"]


# ---------------------------------------------------------------------------
# 三、位置与上下文：docx 没有页码，位置只能靠"落在第几段 + 上下文锚点"
# ---------------------------------------------------------------------------
def test_paragraph_index_points_at_the_right_paragraph(assets):
    chart = _by_kind(assets, "chart")[0]
    image = [a for a in assets if a.kind == "image" and a.part.endswith("image1.png")][0]
    ole = _by_kind(assets, "ole")[0]
    # 夹具里正文段落是 0,1,2,3,4,5,6 —— 图表在 2、插图在 4、OLE 在 6
    assert (chart.para_index, image.para_index, ole.para_index) == (2, 4, 6)


def test_anchor_text_falls_back_to_previous_paragraph(assets):
    """图片/图表所在的段落本身没有文字，锚点必须回退到上文最近的非空段落，
    否则这些块就彻底失去上下文，检索时无从判断它属于哪一节。"""
    chart = _by_kind(assets, "chart")[0]
    image = [a for a in assets if a.kind == "image" and a.part.endswith("image1.png")][0]
    ole = _by_kind(assets, "ole")[0]
    assert chart.para_text == ""                       # 它自己确实没文字
    assert chart.anchor_text == PARA_BEFORE_CHART
    assert image.anchor_text == PARA_BEFORE_IMAGE
    assert ole.anchor_text == PARA_BEFORE_OLE


def test_author_written_alt_text_is_captured(assets):
    """替代文字是作者手写的确定性描述，比任何生成式描述都可靠，必须拿到。"""
    chart = _by_kind(assets, "chart")[0]
    image = [a for a in assets if a.kind == "image" and a.part.endswith("image1.png")][0]
    assert chart.alt_text == ALT_CHART
    assert image.alt_text == ALT_IMAGE
    # VML 的 alt 写在 shape 上，包着图标预览的 imagedata
    icons = [a for a in assets if a.part.endswith("image2.emf")]
    assert icons and icons[0].alt_text == ALT_OLE


# ---------------------------------------------------------------------------
# 四、原件落盘
# ---------------------------------------------------------------------------
def test_image_bytes_are_exported_verbatim(assets, tmp_path):
    image = [a for a in assets if a.part.endswith("image1.png")][0]
    assert image.saved_path.endswith("image1.png")
    with open(image.saved_path, "rb") as fh:
        assert fh.read() == PNG_1PX          # 导出必须是原件，不能有任何加工
    assert str(tmp_path / "media") in image.saved_path


def test_all_media_files_are_on_disk(assets):
    for a in assets:
        if a.kind != "chart":                # chart 是数据不是文件，不导出
            assert a.saved_path, f"{a.kind}/{a.filename} 未落盘"


def test_media_dir_is_isolated_per_document(tmp_path):
    """两份**同名但不同路径**的文档不能混进同一个目录：
    文件不会互相覆盖，但"这图属于哪份文档"会说不清，人工核对时是灾难。"""
    d1 = media_dir_for(str(tmp_path / "a" / "报告.docx"))
    d2 = media_dir_for(str(tmp_path / "b" / "报告.docx"))
    assert d1 != d2
    assert d1.endswith("报告") or "报告_" in d1


def test_media_dir_is_stable_for_the_same_document(tmp_path):
    """同一份文档重复解析必须落在同一目录 —— 否则每次重建都会堆一批副本。"""
    p = str(tmp_path / "报告.docx")
    assert media_dir_for(p) == media_dir_for(p)
    # 正斜杠/反斜杠差异不应产生两个目录（Windows 上同路径的常见写法差异）
    assert media_dir_for(p) == media_dir_for(p.replace("\\", "/"))


# ---------------------------------------------------------------------------
# 五、OLE：解出内层原件并抽正文
# ---------------------------------------------------------------------------
def test_ole_inner_workbook_is_extracted(assets):
    ole = _by_kind(assets, "ole")[0]
    assert ole.prog_id == OLE_PROG_ID
    assert ole.inner_ext == ".xlsx"
    assert ole.filename.endswith(".xlsx")
    with open(ole.saved_path, "rb") as fh:
        assert fh.read()[:4] == b"PK\x03\x04"     # 内层被原样还原成 zip/xlsx


def test_ole_inner_workbook_text_is_parsed(assets):
    """拿到内层原件的真正价值：直接用 loaders 解析，比识别截图准且确定性。"""
    ole = _by_kind(assets, "ole")[0]
    assert OLE_CELL_TEXT in ole.ole_text
    assert str(OLE_CELL_VALUE) in ole.ole_text


def test_ole_prog_id_is_translated_for_humans():
    assert "Excel" in docx_media.PROG_ID_CN[OLE_PROG_ID.lower()]


def test_ole_icon_preview_is_marked_not_a_standalone_image(assets):
    """OLE 对象的 VML 预览位图与 OLE 在**同一个段落**，是同一个对象的两种表示。

    Word 插入 OLE 时必然同时写 `<v:imagedata>`（预览）和 `<o:OLEObject>`（原件），
    两条引用我们都会扫到。不标出来的话，同一份数据会变成两个块：一个解析得干干净净的
    表格文字，外加一张"图"。标记就是为了让产物层只保留前者。
    """
    ole = _by_kind(assets, "ole")[0]
    preview = [a for a in assets if a.part.endswith("image2.emf")][0]
    assert preview.is_ole_preview is True
    assert preview.form == "imagedata"
    assert preview.para_index == ole.para_index      # 判据是"同段落"，不是靠文件名猜
    # 正文插图走 DrawingML 的 a:blip，不是预览形式，不能被误标
    body_img = [a for a in assets if a.part.endswith("image1.png")][0]
    assert body_img.is_ole_preview is False


def test_preview_is_kept_when_ole_inner_was_not_parsed(tmp_path):
    """原件解不出来时（内层是 Visio 之类我们不认识的格式），预览图可能就是唯一的
    视觉线索 —— 这时**不能**去重。保守判据的全部意义就在这里。"""
    magic_cfb = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512
    path = build_minimal_docx(str(tmp_path / "cfb.docx"), ole_blob=magic_cfb)
    assets = extract(path, media_dir=str(tmp_path / "media"))
    assert _by_kind(assets, "ole")[0].ole_text == ""
    preview = [a for a in assets if a.part.endswith("image2.emf")][0]
    assert preview.is_ole_preview is False


def test_summarize_exposes_previews(assets):
    s = docx_media.summarize(assets)
    assert [a.part for a in s["ole_previews"]] == ["word/media/image2.emf"]


def test_cfb_without_package_stream_degrades_gracefully(tmp_path):
    """真实 Word 的 embeddings 是 CFB 复合文档，内层藏在 Package 流里；
    遇到读不出内层的复合文档（或没有 Package 流），必须优雅降级为导出 .bin，
    绝不能因为一个可选依赖缺失就把整条 docx 解析带崩。"""
    magic_cfb = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512
    path = build_minimal_docx(str(tmp_path / "cfb.docx"), ole_blob=magic_cfb)
    assets = extract(path, media_dir=str(tmp_path / "media"))
    ole = _by_kind(assets, "ole")[0]
    assert ole.inner_ext == ""
    assert ole.ole_text == ""
    assert ole.saved_path.endswith(".bin")
    assert ole.error == ""                    # 降级不是错误，不该报警
    # 另外两类元素不受影响，仍要正常
    assert _by_kind(assets, "chart") and _by_kind(assets, "image")


# ---------------------------------------------------------------------------
# 六、异常与边界：失败要显式，不能静默
# ---------------------------------------------------------------------------
def test_non_zip_file_returns_empty(tmp_path):
    p = tmp_path / "not_a_docx.docx"
    p.write_text("这其实是个文本文件", encoding="utf-8")
    assert extract(str(p)) == []


def test_missing_file_returns_empty(tmp_path):
    assert extract(str(tmp_path / "nope.docx")) == []


def test_dangling_relationship_is_recorded_not_skipped(tmp_path):
    """rels 指向包内不存在的部件（文档被工具改写过时会出现）：
    必须留下一条带 error 的记录，而不是当成"没有媒体"悄悄过去。"""
    path = tmp_path / "broken.docx"
    build_minimal_docx(str(path))
    # 重写 rels，把图片指向一个不存在的 part
    with zipfile.ZipFile(path) as z:
        items = {n: z.read(n) for n in z.namelist()}
    items["word/_rels/document.xml.rels"] = items["word/_rels/document.xml.rels"].replace(
        b"media/image1.png", b"media/ghost.png")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in items.items():
            z.writestr(n, b)

    assets = extract(str(path), media_dir=str(tmp_path / "media"))
    broken = [a for a in assets if a.error]
    assert broken, "包内缺失的部件必须留下记录"
    assert any("不存在" in a.error for a in broken)


def test_max_assets_limit_is_respected(docx_path, tmp_path, monkeypatch):
    monkeypatch.setattr(docx_media, "MAX_ASSETS", 2)
    assert len(extract(docx_path, media_dir=str(tmp_path / "media"))) == 2


def test_summarize_counts_by_kind(assets):
    s = docx_media.summarize(assets)
    assert s["total"] == len(assets)
    assert s["by_kind"]["chart"] == 1
    assert len(s["charts_with_data"]) == 1
    assert s["errors"] == []


def test_asset_to_dict_roundtrip(assets):
    d = _by_kind(assets, "chart")[0].to_dict()
    assert isinstance(d, dict)
    assert d["kind"] == "chart" and d["chart"]["title"] == CHART_TITLE
    assert isinstance(DocxAsset(kind="image"), DocxAsset)     # 默认值可用
