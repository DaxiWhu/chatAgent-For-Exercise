"""原页截图（引用返图）测试。

需求：**PDF 要能连同"从哪儿看到的"原图一起返回**；纯文字块不必；扫描件必须给原图。
这层最容易错的不是"有没有图"，而是**图截错位置**：
  - 坐标原点是左下（PDF 惯例）而 PIL 裁剪用左上 → 不换算就会上下颠倒；
  - bbox 是每个元素的，混排块要取**并集**，否则只裁到半张表。
截图错了不会报错，只会让人对着图找不到那句话 —— 所以必须有像素级的断言。
"""
import os

import pytest

from src.rag import docling_loader as L

pytest.importorskip("docling")

# 需要模型权重的用例统一用"权重缺失才 skip"的判据（与 test_docling_loader 一致）
_ENV_NOT_READY = ("artifacts not found or incomplete", "does not exist",
                  "No such file or directory", "Failed to download")


def _skip_or_fail(out):
    if out.status == "unavailable":
        pytest.skip(f"docling 不可用：{out.reason}")
    if out.status == "failed" and any(m in (out.reason or "") for m in _ENV_NOT_READY):
        pytest.skip(f"模型权重未就绪：{out.reason}")
    return out


# ---------------------------------------------------------------------------
# 方向：像素级断言（不依赖 OCR，也不依赖"看起来对"）
# ---------------------------------------------------------------------------
class _FakePage:
    def __init__(self, pil, height):
        self.image = type("Img", (), {"pil_image": pil})()
        self.size = type("Size", (), {"height": height, "width": height})()   # 100x100 的方形页


class _FakeChunk:
    def __init__(self, bbox, kind="table", page=1):
        self.bbox = bbox
        self.kind = kind
        self.page = page
        self.chunk_id = "c"


def _page_image(half="top"):
    """造一张"上半白、下半黑"的页图：白=上半、黑=下半。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (100, 100), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 50, 100, 100], fill="black")
    return img


def test_crop_region_flips_bottom_left_origin_to_pil_top_left():
    """docling 的 bbox 是**左下原点**（y 越大越靠上），PIL 裁剪用左上原点。

    不换算的后果是图上下颠倒 —— 而"截到的是页面另一半"这种事不会报错，
    只会让人对着图找不到要核对的那句话。
    这里 bbox 声明在**页面顶部**（左下原点 y=80~90），裁出来必须全是白色。
    """
    pil = _page_image()
    page = _FakePage(pil, 100)
    # bbox=[l, t, r, b]；t=90, b=80 → 左下原点下这是页面顶部区域
    crop = L._crop_region(pil, _FakeChunk([[10, 90, 90, 80]]), page)
    assert crop is not None
    colors = {c for _, c in crop.convert("RGB").getcolors(maxcolors=10000)}
    assert colors == {(255, 255, 255)}, f"裁到的不是页面顶部（可能上下颠倒了）：{colors}"


def test_crop_region_handles_top_left_origin_without_flipping():
    """万一是左上原点（t < b），不能再翻一次 —— 翻了同样会跑到页面另一半去。"""
    pil = _page_image()
    page = _FakePage(pil, 100)
    crop = L._crop_region(pil, _FakeChunk([[10, 10, 90, 40]]), page)
    colors = {c for _, c in crop.convert("RGB").getcolors(maxcolors=10000)}
    assert colors == {(255, 255, 255)}


def test_crop_region_uses_the_union_of_all_boxes():
    """混排块（表格 + 图注）有多个 bbox：只取一个会裁到半张表。"""
    pil = _page_image()
    page = _FakePage(pil, 100)
    # 两个框一个在上半、一个在下半 → 并集必然跨越黑白交界
    crop = L._crop_region(pil, _FakeChunk([[0, 95, 100, 85], [0, 20, 100, 10]]), page)
    colors = {c for _, c in crop.convert("RGB").getcolors(maxcolors=10000)}
    assert (255, 255, 255) in colors and (0, 0, 0) in colors, "并集没生效，只裁到了一部分"


def test_crop_region_returns_none_on_degenerate_bbox():
    """bbox 退化（面积≈0）时宁可**不给图**，也不能给一张裁错位置的图。"""
    pil = _page_image()
    page = _FakePage(pil, 100)
    assert L._crop_region(pil, _FakeChunk([[10, 20, 10.5, 20.5]]), page) is None
    assert L._crop_region(pil, _FakeChunk([]), page) is None


def test_crop_region_clips_to_page_bounds():
    """bbox 贴着页面边缘 + 边距外扩，不能越界（PIL 会把越界区域填黑）。"""
    from PIL import Image
    pil = Image.new("RGB", (100, 100), "white")
    page = _FakePage(pil, 100)
    crop = L._crop_region(pil, _FakeChunk([[0, 100, 100, 0]]), page)
    assert crop.size == (100, 100)          # 裁剪结果不超过页面本身


# ---------------------------------------------------------------------------
# 规则：哪些块给图（纯文字不给；扫描页给整页）
# ---------------------------------------------------------------------------
def test_only_non_text_kinds_get_a_crop():
    """电子页（不在 scanned_pages 里）：只有表/图/公式给裁剪图，纯文字不给。"""
    electronic: set = set()
    assert L._needs_cite_image(_FakeChunk([], kind="table"), electronic) is True
    assert L._needs_cite_image(_FakeChunk([], kind="chart"), electronic) is True
    assert L._needs_cite_image(_FakeChunk([], kind="diagram"), electronic) is True
    assert L._needs_cite_image(_FakeChunk([], kind="text"), electronic) is False


def test_scanned_pages_always_get_the_full_page():
    """扫描页上"原文"就是那张图，OCR 文字只是近似转写 —— 按 bbox 裁一行反而更难核对。"""
    assert L._needs_cite_image(_FakeChunk([], kind="text", page=3), {3}) is True


def test_mixed_document_judges_page_by_page():
    """**混合文档**：同一份文件里电子页和扫描页两种规则要同时生效。

    判据必须看块自己那一页，不能用"整份是不是扫描件"一个布尔 —— 那种写法必然
    有一边错：要么扫描页的纯文字块拿不到整页图，要么电子页的每个块都被塞一张整页图。
    """
    scanned = {2, 3}
    # 第 2 页是扫描页：连纯文字块也要整页图
    assert L._needs_cite_image(_FakeChunk([], kind="text", page=2), scanned) is True
    # 第 1 页是电子页：纯文字块不给图，表格块照旧给裁剪
    assert L._needs_cite_image(_FakeChunk([], kind="text", page=1), scanned) is False
    assert L._needs_cite_image(_FakeChunk([], kind="table", page=1), scanned) is True


def test_page_images_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(L, "DOCLING_PAGE_IMAGES", "off")
    assert L._want_page_images() is False
    # 关掉之后连入口都不该进（大文件批量灌库时的内存保护）
    assert L._attach_cite_images(None, [_FakeChunk([[0, 0, 1, 1]])], "x.pdf", set()) == 0


# ---------------------------------------------------------------------------
# 端到端：真 PDF（Docling 跑一遍）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def media_dir(tmp_path_factory, ):
    """媒体目录改到临时目录，别往仓库里写。"""
    from src.rag import docx_media
    d = tmp_path_factory.mktemp("cite_media")
    docx_media.MEDIA_DIR = str(d)
    return str(d)


def _table_pdf(tmp_path):
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "scripts"))
    from smoke_docling import build_test_pdf
    return build_test_pdf(str(tmp_path / "table.pdf"))


def test_table_chunk_gets_a_cropped_image(tmp_path, media_dir):
    """电子版 PDF 的表格块 → 有原页截图，且**不是整页**（是裁出的区域）。"""
    out = _skip_or_fail(L.parse_pdf(_table_pdf(tmp_path), "table.pdf"))
    tables = [c for c in out.chunks if c.kind == "table"]
    assert tables, "没有解析出表格块"
    img = tables[0].page_image
    assert img and os.path.isfile(img), "表格块没有生成原页截图"
    from PIL import Image
    with Image.open(img) as im, Image.open(img) as _:
        crop_size = im.size
    assert crop_size[0] > 0 and crop_size[1] > 0
    # 页面是 612x792pt、scale 2.0 → 整页应约 1224x1584；裁出的区域必须明显小于整页
    assert crop_size[0] < 1200 and crop_size[1] < 1500, f"截图像是整页而不是区域：{crop_size}"


def test_pure_text_chunk_gets_no_image(tmp_path, media_dir):
    """用户明确要求：纯文字块没必要返回原图。"""
    from tests.test_pdf_parser import make_pdf
    p = make_pdf(str(tmp_path / "plain.pdf"),
                 ["Wuzhou exports liubao tea to Guangzhou.", "Plain text page."])
    out = _skip_or_fail(L.parse_pdf(p, "plain.pdf"))
    assert out.chunks
    assert all(c.kind == "text" for c in out.chunks)
    assert all(not c.page_image for c in out.chunks), "纯文字块不该产出原页截图"


def test_citation_carries_image_and_human_readable_where(tmp_path, media_dir):
    """引用里要同时有"图"和"人话位置" —— 光有坐标不足以让人去核对。"""
    from src.rag.retrieval import build_citations

    out = _skip_or_fail(L.parse_pdf(_table_pdf(tmp_path), "table.pdf"))
    hit = next(c for c in out.chunks if c.kind == "table")
    cite = build_citations([(hit.to_dict() | {"text": hit.retrieval_text}, 0.9)])[0]
    assert cite["page"] == 1 and cite["where"].startswith("table.pdf")
    assert "第 1 页" in cite["where"]
    assert cite["page_image"] and os.path.isfile(cite["page_image"])
    assert cite["bbox"], "坐标要留着，便于前端在整页图上高亮"


def test_scanned_pdf_returns_the_whole_page(tmp_path, media_dir):
    """扫描件：原图 = 整页（这是"可靠性"要求的直接体现）。"""
    pytest.importorskip("PIL")
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "scripts"))
    from check_ocr import build_scanned_pdf
    from PIL import Image

    out = _skip_or_fail(L.parse_pdf(build_scanned_pdf(str(tmp_path / "s.pdf")),
                                   "s.pdf"))
    chunks = [c for c in out.chunks if c.page_image]
    assert chunks, "扫描件没有产出原图"
    with Image.open(chunks[0].page_image) as im:
        assert im.size[0] >= 1000, f"整页原图分辨率过低，核对时看不清：{im.size}"


def test_unprocessable_scan_still_returns_the_page_image(tmp_path, media_dir):
    """**解析不出内容时更要给原图**。

    最常见的失败原因是"扫描件 OCR 读不出来"（歪斜/低对比/手写/纯图无文字）。
    这时用户最需要的是打开原图看一眼，而不是只收到一句"未取到任何内容"。
    """
    from PIL import Image, ImageDraw

    pytest.importorskip("PIL")
    # 造一份"有内容但不是文字"的扫描件：OCR 必然读不出东西
    pages = []
    for _ in range(2):
        im = Image.new("RGB", (1240, 1754), "white")
        d = ImageDraw.Draw(im)
        for y in range(120, 1600, 80):
            d.rectangle([100, y, 1140, y + 40], fill="black" if (y // 80) % 2 else (60, 60, 60))
        pages.append(im)
    p = str(tmp_path / "bars.pdf")
    pages[0].save(p, "PDF", save_all=True, append_images=pages[1:], resolution=150)

    out = _skip_or_fail(L.parse_pdf(p, "bars.pdf"))
    assert out.status == "unprocessable", f"预期解析不出内容，实得 {out.status}"
    kc = out.chunks[0]
    assert kc.kind == "unprocessable"
    assert kc.page_image, "解析失败的扫描件也必须给出原页图"
    assert os.path.isfile(kc.page_image)
    assert kc.page == 1, "附了图就要标出是哪一页"
