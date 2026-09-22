"""各格式的结构化解析测试：全部离线，文件由测试现场生成。

为什么每个格式都要单独测自己的"结构"：
Word/PDF 的结构由 Docling 负责，我们只消费；而 csv/xlsx/pptx/json/html/md 这一批是
**我们自己写的解析器**，每个格式能说清的结构都不一样（表头、工作表、页序、备注、
记录边界、代码块……）。这些结构一旦没保住，产物看上去仍然"有内容"——
只是检索永远差一点点，而没有任何报错。
"""
import json

import pytest

from src.loaders import load_blocks, load_text
from src.retrieval import load_chunks


def _texts(blocks):
    return "\n".join(b.text for b in blocks)


def _by_kind(blocks, kind):
    return [b for b in blocks if b.kind == kind]


# ---------------------------------------------------------------------------
# Excel：本格式特有的四个问题（值的位置即语义 / 合并单元格 / 公式无缓存值 / 一个文件多张表）
# ---------------------------------------------------------------------------
def _xlsx(tmp_path, name="a.xlsx"):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    return openpyxl, wb, tmp_path / name


def test_xlsx_every_row_carries_its_column_names(tmp_path):
    """单元格自己不带列名，值是"第 2 列"才有意义 —— 列名必须写进每一行。"""
    openpyxl, wb, p = _xlsx(tmp_path)
    ws = wb.active
    ws.title = "明细"
    ws.append(["项目", "金额"])
    ws.append(["服务器采购", 50000])
    wb.save(str(p))

    blocks = load_blocks(str(p))
    assert blocks[0].kind == "table"
    assert "表头：项目 | 金额" in blocks[0].text
    assert "项目=服务器采购 | 金额=50000" in blocks[0].text


def test_xlsx_merged_cell_value_propagates_across_columns(tmp_path):
    """合并单元格只在左上角存值（其余是 None）。

    真实场景：`类别` 跨两列写了一次"采购"，下面还有 `项目`/`数量` 两列。
    不填充的话这一行的"类别"只在第一列有值，第二列变成空 —— 逐行写成 列=值 之后，
    那句标签就等于消失了。文件看起来完整、解析也不报错。
    """
    openpyxl, wb, p = _xlsx(tmp_path)
    ws = wb.active
    ws.append(["类别", "项目", "数量"])       # 表头
    ws.append([None, "服务器", 3])            # 数据行（A2:B2 将合并）
    ws.merge_cells("A2:B2")
    ws["A2"] = "采购"
    wb.save(str(p))

    # 前提确认：合并区的非左上角确实是 None（否则这条用例测不到东西）
    vals = [r for r in openpyxl.load_workbook(str(p)).active.iter_rows(values_only=True)]
    assert vals[1][1] is None

    text = _texts(load_blocks(str(p)))
    assert "类别=采购" in text, "合并单元格的值没有铺到该行的其它列"
    assert "项目=采购" in text
    assert "数量=3" in text


def test_xlsx_merged_header_does_not_duplicate_column_names(tmp_path):
    """回归：表头行**不能**横向填充。

    跨列合并的标题（A1:B1="采购明细"）若铺到两列，列名会变成两个一样的，
    整张表的列名全错 —— 实测踩过：`采购明细=服务器 | 采购明细=3`。
    """
    openpyxl, wb, p = _xlsx(tmp_path)
    ws = wb.active
    ws.append([None, None, "说明"])
    ws.merge_cells("A1:B1")
    ws["A1"] = "采购明细"
    ws.append(["服务器", 3, "台"])
    wb.save(str(p))

    text = _texts(load_blocks(str(p)))
    header = text.splitlines()[1]
    assert header.count("采购明细") == 1, f"表头列名被复制了：{header}"


def test_xlsx_without_computed_values_says_so_instead_of_looking_empty(tmp_path):
    """公式的缓存值只在被 Excel/WPS 保存过之后才存在。

    机器生成的 xlsx 里 `data_only=True` 读出来全是 None —— 这时**必须显式说明**，
    否则和"真空白文件"完全无法区分（本格式最容易踩的坑，且不会有任何异常）。
    """
    openpyxl, wb, p = _xlsx(tmp_path)
    ws = wb.active
    ws["A1"] = "=1+1"                        # 只有公式，没有缓存值
    ws["A2"] = "=SUM(1,2)"
    wb.save(str(p))

    blocks = load_blocks(str(p))
    text = _texts(blocks)
    assert "未读到任何单元格值" in text
    assert "Excel/WPS" in text, "要告诉人怎么解决，而不是只说读不到"
    assert blocks[0].tags == ["无可读内容"]


def test_xlsx_multiple_sheets_are_separate_blocks_with_page_and_section(tmp_path):
    """一个文件里有多个独立文档（多工作表）—— Word/PDF 是单一线性流，这里必须自己决定。"""
    openpyxl, wb, p = _xlsx(tmp_path)
    ws1 = wb.active
    ws1.title = "特产表"
    ws1.append(["城市", "特产"])
    ws1.append(["梧州", "六堡茶"])
    ws2 = wb.create_sheet("景点表")
    ws2.append(["景点", "位置"])
    ws2.append(["骑楼城", "万秀区"])
    wb.save(str(p))

    blocks = load_blocks(str(p))
    assert [b.page for b in blocks] == [1, 2]
    assert [b.section for b in blocks] == ["工作表：特产表", "工作表：景点表"]
    # 工作表名也要在文本里：检索只嵌入 text，只放 section 会把"特产表里有什么"漏掉
    assert "## 工作表：景点表" in blocks[1].text
    assert "景点=骑楼城 | 位置=万秀区" in blocks[1].text


def test_xlsx_hidden_sheet_is_not_indexed(tmp_path):
    openpyxl, wb, p = _xlsx(tmp_path)
    ws = wb.active
    ws.title = "可见"
    ws.append(["a"])
    ws.append(["1"])
    hidden = wb.create_sheet("隐藏")
    hidden.append(["secret"])
    hidden.sheet_state = "hidden"
    wb.save(str(p))

    text = _texts(load_blocks(str(p)))
    assert "a=1" in text and "secret" not in text


def test_xlsx_chart_data_comes_from_the_same_chart_xml_as_word(tmp_path):
    """xlsx 的图表数据同样是 DrawingML chart XML —— 与 Word **完全同一种格式**，
    所以读取代码可以直接复用，且读出来的是确定性数值，不需要 VLM。"""
    openpyxl, wb, p = _xlsx(tmp_path)
    from openpyxl.chart import LineChart, Reference

    ws = wb.active
    for row in [["年份", "销售额"], [2019, 120], [2020, 145]]:
        ws.append(row)
    chart = LineChart()
    chart.add_data(Reference(ws, min_col=2, min_row=1, max_row=3), titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=1, min_row=2, max_row=3))
    ws.add_chart(chart, "E2")
    wb.save(str(p))

    blocks = load_blocks(str(p))
    charts = _by_kind(blocks, "chart")
    assert charts, "xlsx 里的图表没有被读出来"
    assert "来自XML" in charts[0].tags
    assert "120" in charts[0].text and "145" in charts[0].text


def test_xlsx_row_truncation_is_explicit(tmp_path, monkeypatch):
    from src import loaders as L

    openpyxl, wb, p = _xlsx(tmp_path)
    ws = wb.active
    ws.append(["i", "v"])
    for i in range(30):
        ws.append([i, i * 2])
    wb.save(str(p))

    monkeypatch.setattr(L, "XLSX_MAX_ROWS", 5)
    text = _texts(load_blocks(str(p)))
    assert "已截断" in text and "5 行" in text


# ---------------------------------------------------------------------------
# PowerPoint：本格式特有的四个问题（顺序不可靠 / 备注 / 页序即位置 / 表格图表图片都是嵌入对象）
# ---------------------------------------------------------------------------
def _pptx(tmp_path, name="a.pptx"):
    pptx = pytest.importorskip("pptx")
    return pptx, pptx.Presentation(), tmp_path / name


def test_pptx_page_number_and_title_section(tmp_path):
    pptx, prs, p = _pptx(tmp_path)
    slide = prs.slides.add_slide(prs.slide_layouts[5])       # 带标题占位符的版式
    slide.shapes.title.text = "梧州特产"
    slide.shapes.add_textbox(0, 100, 200, 50).text_frame.text = "六堡茶很有名"
    prs.save(str(p))

    blocks = load_blocks(str(p))
    assert blocks[0].page == 1
    assert blocks[0].section == "梧州特产"
    assert "## 第 1 页" in blocks[0].text        # 页号也在文本里，答得了"第 1 页讲了什么"
    assert "六堡茶很有名" in blocks[0].text


def test_pptx_speaker_notes_are_extracted(tmp_path):
    """备注里藏着真正的讲解，而正文可能只有几个词。以前完全没读 —— 信息量最大的一块。"""
    pptx, prs, p = _pptx(tmp_path)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_textbox(0, 0, 200, 50).text_frame.text = "2024 年回顾"
    slide.notes_slide.notes_text_frame.text = "强调一下：华东区超额完成 12%。"
    prs.save(str(p))

    blocks = load_blocks(str(p))
    notes = [b for b in blocks if "备注" in b.tags]
    assert notes and "华东区超额完成" in notes[0].text
    assert notes[0].page == 1 and "第 1 页" in notes[0].text


def test_pptx_table_is_header_aware(tmp_path):
    pptx, prs, p = _pptx(tmp_path)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    table = slide.shapes.add_table(2, 2, 0, 0, 300, 100).table
    table.cell(0, 0).text, table.cell(0, 1).text = "地区", "销售额"
    table.cell(1, 0).text, table.cell(1, 1).text = "华东", "1200"
    prs.save(str(p))

    text = _texts(load_blocks(str(p)))
    assert "表头：地区 | 销售额" in text
    assert "地区=华东 | 销售额=1200" in text


def test_pptx_chart_data_is_deterministic(tmp_path):
    pptx, prs, p = _pptx(tmp_path)
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE

    slide = prs.slides.add_slide(prs.slide_layouts[6])
    data = CategoryChartData()
    data.categories = ["2019", "2020"]
    data.add_series("销售额", (120, 145))
    slide.shapes.add_chart(XL_CHART_TYPE.LINE, 0, 0, 400, 300, data)
    prs.save(str(p))

    charts = _by_kind(load_blocks(str(p)), "chart")
    assert charts, "PPT 里的图表没有被读出来"
    assert charts[0].page == 1, "图表要挂到它所在的那一页上，不能丢位置"
    assert "120" in charts[0].text and "销售额" in charts[0].text


def test_pptx_image_alt_text_is_captured_and_file_exported(tmp_path):
    """图片的替代文字是作者写的确定性描述（免 VLM），原件要落盘供 VLM/引用使用。"""
    pptx, prs, p = _pptx(tmp_path)
    from PIL import Image

    img = tmp_path / "pic.png"
    Image.new("RGB", (60, 40), "white").save(img)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    pic = slide.shapes.add_picture(str(img), 0, 0)
    pic._element.nvPicPr.cNvPr.set("descr", "组织架构示意图")
    prs.save(str(p))

    pics = _by_kind(load_blocks(str(p)), "diagram")
    assert pics, "PPT 图片没有被抽出"
    assert pics[0].source_image and pics[0].source_image.endswith(".png")
    assert pics[0].text.strip() == "组织架构示意图"
    assert pics[0].needs_vlm is False
    import os
    assert os.path.isfile(pics[0].source_image)


def test_pptx_slide_order_follows_page_numbers_not_strings(tmp_path):
    """回归：`slide10.xml` 不能排在 `slide2.xml` 前面，否则第 10 页的内容会挂到第 2 页上。"""
    pptx, prs, p = _pptx(tmp_path)
    for i in range(11):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.shapes.add_textbox(0, 0, 200, 50).text_frame.text = f"第{i + 1}张"
    prs.save(str(p))

    blocks = load_blocks(str(p))
    pages = [b.page for b in blocks]
    assert pages == sorted(pages) == list(range(1, 12))
    assert "第10张" in [b.text for b in blocks if b.page == 10][0]


# ---------------------------------------------------------------------------
# CSV：表头必须出现在**每一块**里
# ---------------------------------------------------------------------------
def test_csv_every_block_repeats_the_header(tmp_path, monkeypatch):
    """不重复表头的话，切分后的第 2 块起就只剩数值行，列名永久丢失。"""
    from src import loaders as L

    p = tmp_path / "d.csv"
    p.write_text("城市,特产\n" + "\n".join(f"城市{i},特产{i}" for i in range(5)),
                 encoding="utf-8")
    monkeypatch.setattr(L, "CSV_ROWS_PER_BLOCK", 2)

    blocks = load_blocks(str(p))
    assert len(blocks) == 3
    assert all("表头：城市 | 特产" in b.text for b in blocks)
    assert all(b.kind == "table" for b in blocks)
    assert "城市4=城市4" not in blocks[0].text        # 确实是切开的多块


# ---------------------------------------------------------------------------
# JSON / JSONL：按记录成块
# ---------------------------------------------------------------------------
def test_json_array_of_records_becomes_one_block_per_record(tmp_path):
    """记录是这个格式天然的最小语义单位：命中一块 = 拿到完整的一条。"""
    p = tmp_path / "d.json"
    p.write_text(json.dumps([{"city": "梧州", "tea": "六堡茶"},
                             {"city": "桂林", "tea": "桂花茶"}], ensure_ascii=False),
                 encoding="utf-8")
    blocks = load_blocks(str(p))
    assert len(blocks) == 2
    assert "记录 0" in blocks[0].text and "city: 梧州" in blocks[0].text
    assert "city: 桂林" in blocks[1].text
    assert "梧州" not in blocks[1].text               # 记录之间没有串味


def test_jsonl_keeps_invalid_lines(tmp_path):
    p = tmp_path / "d.jsonl"
    p.write_text('{"a":1}\nnot-json\n{"a":2}\n', encoding="utf-8")
    blocks = load_blocks(str(p))
    assert [b.text.splitlines()[0] for b in blocks] == ["记录 0", "not-json", "记录 2"]
    assert "非法JSON行" in blocks[1].tags


def test_json_record_cap_is_explicit(tmp_path, monkeypatch):
    from src import loaders as L

    p = tmp_path / "d.json"
    p.write_text(json.dumps([{"i": i} for i in range(30)]), encoding="utf-8")
    monkeypatch.setattr(L, "JSON_MAX_RECORDS", 3)
    blocks = load_blocks(str(p))
    assert len(blocks) == 4
    assert "已截断" in blocks[-1].text and "截断" in blocks[-1].tags


# ---------------------------------------------------------------------------
# Markdown：代码块不可切开 + front-matter 是元数据
# ---------------------------------------------------------------------------
def test_markdown_code_fence_is_kept_whole(tmp_path):
    """按字符切分会在 `def f():` 与函数体之间断开，检索回半截代码 —— 而且断点看着很自然。"""
    p = tmp_path / "a.md"
    p.write_text("# 用法\n\n下面这样写：\n\n```python\ndef f():\n    return 1\n```\n\n结束。\n",
                 encoding="utf-8")
    blocks = load_blocks(str(p))
    code = [b for b in blocks if "代码块" in b.tags]
    assert len(code) == 1
    assert "def f():" in code[0].text and "return 1" in code[0].text
    assert code[0].text.startswith("```")


def test_markdown_front_matter_is_separated(tmp_path):
    p = tmp_path / "a.md"
    p.write_text("---\ntitle: 报告\n---\n\n# 正文\n\n内容。\n", encoding="utf-8")
    blocks = load_blocks(str(p))
    assert "front-matter" in blocks[0].tags
    assert "title: 报告" in blocks[0].text
    body = [b for b in blocks if "正文" in b.text]
    assert body and "title:" not in body[-1].text, "元数据不能混进正文"


def test_markdown_headings_produce_section_path(tmp_path):
    p = tmp_path / "a.md"
    p.write_text("# 梧州\n\n## 特产\n\n六堡茶。\n", encoding="utf-8")
    blocks = load_blocks(str(p))
    para = [b for b in blocks if "六堡茶" in b.text][0]
    assert para.section == "梧州 > 特产"


# ---------------------------------------------------------------------------
# HTML：表格按行（修"单元格被拍平"）+ 剔除页面家具
# ---------------------------------------------------------------------------
def test_html_table_rows_keep_the_header(tmp_path):
    """老实现把 td/th 也当成独立块标签 → 一行 5 列被拆成 5 个互不相关的块，
    "城市=梧州"与"特产=六堡茶"天各一方，行关系彻底丢失。"""
    p = tmp_path / "a.html"
    p.write_text("<table><tr><th>城市</th><th>特产</th></tr>"
                 "<tr><td>梧州</td><td>六堡茶</td></tr></table>", encoding="utf-8")
    blocks = load_blocks(str(p))
    tables = _by_kind(blocks, "table")
    assert len(tables) == 1
    assert "表头：城市 | 特产" in tables[0].text
    assert "城市=梧州 | 特产=六堡茶" in tables[0].text


def test_html_drops_navigation_and_footer(tmp_path):
    """导航/页脚的文案会在几十个页面里重复出现，进了索引就把真正的内容挤下去。"""
    p = tmp_path / "a.html"
    p.write_text("<html><body><nav>首页 关于 联系</nav>"
                 "<h1>梧州</h1><p>六堡茶很有名。</p>"
                 "<footer>版权所有</footer></body></html>", encoding="utf-8")
    text = _texts(load_blocks(str(p)))
    assert "六堡茶很有名。" in text
    assert "首页 关于 联系" not in text and "版权所有" not in text


# ---------------------------------------------------------------------------
# 图片文件：无文字层（本格式根本性的不同）
# ---------------------------------------------------------------------------
def test_image_file_becomes_a_needs_vlm_block(tmp_path):
    """图片没有任何文字层，只能靠 VLM/OCR。没开 VLM 时也要留下可筛选的痕迹，
    而不是像以前那样当成"不支持的扩展名"静默跳过。"""
    from PIL import Image

    p = tmp_path / "架构图.png"
    Image.new("RGB", (60, 40), "white").save(p)

    blocks = load_blocks(str(p))
    assert len(blocks) == 1
    assert blocks[0].needs_vlm is True
    assert blocks[0].source_image == str(p)
    assert "待VLM" in blocks[0].tags


def test_image_is_not_silently_skipped_by_load_chunks(tmp_path):
    from PIL import Image

    Image.new("RGB", (60, 40), "white").save(tmp_path / "a.png")
    (tmp_path / "b.md").write_text("# 正文\n\n六堡茶。", encoding="utf-8")

    chunks = load_chunks(str(tmp_path))
    sources = {c["source"] for c in chunks}
    assert sources == {"a.png", "b.md"}, "图片文件不能因为没文字就消失"


# ---------------------------------------------------------------------------
# 接线：结构信息要真的出得了边界
# ---------------------------------------------------------------------------
def test_other_formats_carry_kind_page_and_section_into_chunks(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "明细"
    ws.append(["项目", "金额"])
    ws.append(["服务器采购", 50000])
    wb.save(str(tmp_path / "a.xlsx"))

    chunks = load_chunks(str(tmp_path))
    assert len(chunks) == 1
    c = chunks[0]
    assert c["kind"] == "table"
    assert c["page"] == 1
    assert c["section"] == "工作表：明细"
    assert c["parser"] == "xlsx"
    assert "项目=服务器采购 | 金额=50000" in c["text"]     # 检索层只认 text


def test_oversized_block_is_split_but_keeps_section_and_page(tmp_path, monkeypatch):
    """块太长要二次切分，但**归属不能变**：切开的只是长度，section/page 必须跟着走。"""
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "长表"
    ws.append(["编号", "说明"])
    for i in range(200):
        ws.append([i, "说明文字" * 10])
    wb.save(str(tmp_path / "a.xlsx"))

    chunks = load_chunks(str(tmp_path), chunk_size=200, chunk_overlap=0)
    assert len(chunks) > 1, "超长工作表应被切成多块"
    assert all(c["section"] == "工作表：长表" for c in chunks)
    assert all(c["page"] == 1 for c in chunks)
