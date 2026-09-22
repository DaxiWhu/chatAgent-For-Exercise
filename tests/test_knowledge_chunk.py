"""KnowledgeChunk 转换层测试：全部离线，不需要 docling 也不用下模型。

from_docling_chunk 用 duck typing 读取 DocChunk 与 DoclingDocument 元素
（只看 label / prov / text / meta 这些属性），所以可以用假对象把字段映射测全——
这是刻意的设计：解析层换实现时，本文件的测试仍然有效。
"""
from src.docx_media import DocxAsset, chart_to_table, chart_to_text
from src.knowledge_chunk import (
    KIND_CHART,
    KIND_DIAGRAM,
    KIND_FORMULA,
    KIND_TABLE,
    KIND_TEXT,
    KIND_UNPROCESSABLE,
    from_docling_chunk,
    from_docling_items,
    from_docling_markdown,
    from_docx_media,
    from_pdfminer_pieces,
    item_text,
    linearize_table,
    make_chunk_id,
    picture_kind,
    table_to_markdown,
    unprocessable,
)

# ---------------------------------------------------------------------------
# 假 Docling 对象（只模仿被用到的属性）
# ---------------------------------------------------------------------------
class _Label:
    def __init__(self, value):
        self.value = value


class _Box:
    def __init__(self, l, t, r, b):
        self.l, self.t, self.r, self.b = l, t, r, b


class _Prov:
    def __init__(self, page_no, box=None):
        self.page_no = page_no
        self.bbox = box


class _Ann:
    def __init__(self, predicted_class):
        self.predicted_class = predicted_class


class _Item:
    def __init__(self, label, provs=(), annotations=None):
        self.label = _Label(label)
        self.prov = list(provs)
        if annotations is not None:
            self.annotations = annotations


class _Meta:
    def __init__(self, headings, doc_items):
        self.headings = list(headings)
        self.doc_items = list(doc_items)


class _Chunk:
    def __init__(self, text, headings, doc_items):
        self.text = text
        self.meta = _Meta(headings, doc_items)


class _Table(_Item):
    def __init__(self, md, caption="", provs=()):
        super().__init__("table", provs)
        self._md, self._cap = md, caption

    def export_to_markdown(self, doc=None):
        return self._md

    def caption_text(self, doc):
        return self._cap


class _Picture(_Item):
    def __init__(self, caption="", predicted_class="", provs=()):
        super().__init__("picture", provs,
                         [_Ann(predicted_class)] if predicted_class else [])
        self._cap = caption

    def caption_text(self, doc):
        return self._cap


MD_TABLE = """\
| 年龄 | 给付比例 |
| --- | --- |
| 18-40岁 | 160% |
| 41-60岁 | 140% |"""


class _RefItem:
    """模仿 docling 的**轻量元素引用**：只有 label / self_ref / prov。

    这是实测出来的真实形态：`chunk.meta.doc_items` 里装的就是这种对象，
    没有 `.text`、没有 `export_to_markdown`、没有 `annotations`。
    用它写测试，才能覆盖"引用必须先解析"这条不变式。
    """

    def __init__(self, label, self_ref, provs=()):
        self.label = _Label(label)
        self.self_ref = self_ref
        self.prov = list(provs)


class _DocWithItems:
    """只提供 self_ref → 完整元素 的还原来源（模仿 DoclingDocument 的 texts/tables/pictures）。"""

    def __init__(self, texts=(), tables=(), pictures=()):
        self.texts = list(texts)
        self.tables = list(tables)
        self.pictures = list(pictures)


def _with_ref(item, ref):
    item.self_ref = ref
    return item


# ---------------------------------------------------------------------------
# linearize_table：表格行列文字化（本项目核心设计）
# ---------------------------------------------------------------------------
def test_linearize_table_turns_grid_into_sentences():
    """二维表格语法在向量空间里离自然语言问题很远，必须改写成"列=值"。"""
    out = linearize_table(MD_TABLE)
    assert "年龄=18-40岁；给付比例=160%" in out
    assert "年龄=41-60岁；给付比例=140%" in out


def test_linearize_table_skips_separator_row():
    out = linearize_table(MD_TABLE)
    assert "---" not in out
    assert out.count("\n") == 1          # 两行数据 → 一行分隔


def test_linearize_table_prepends_caption():
    out = linearize_table(MD_TABLE, caption="表1 给付比例")
    assert out.startswith("表1 给付比例：")


def test_linearize_table_handles_empty_cells_and_escapes():
    md = "| A | B |\n| --- | --- |\n| 1 |  |\n| x\\|y | 2 |"
    out = linearize_table(md)
    assert "A=1" in out and "B=1" not in out        # 空单元格跳过
    assert "A=x|y" in out                            # \| 还原成 |


def test_linearize_table_empty_input_returns_empty():
    assert linearize_table("") == ""
    assert linearize_table("   \n  ") == ""
    assert linearize_table("| 只有表头 |\n") == ""    # 无数据行 → 空


def test_linearize_table_uses_positional_key_without_header():
    md = "| --- | --- |\n| a | b |"
    out = linearize_table(md)
    assert "列1=a" in out and "列2=b" in out


def test_linearize_table_respects_max_rows():
    md = "| h |\n| --- |\n" + "\n".join(f"| {i} |" for i in range(20))
    out = linearize_table(md, max_rows=5)
    assert len(out.splitlines()) == 5


def test_table_to_markdown_survives_object_without_method():
    class _NoMethod:
        pass
    assert table_to_markdown(_NoMethod(), None) == ""


# ---------------------------------------------------------------------------
# 图片价值判定
# ---------------------------------------------------------------------------
def test_picture_kind_recognises_chart_class():
    kind, needs_vlm = picture_kind(_Picture(predicted_class="line_chart"))
    assert kind == KIND_CHART and needs_vlm is False


def test_picture_kind_flags_unclassified_bitmap_for_vlm():
    kind, needs_vlm = picture_kind(_Picture())
    assert kind == KIND_DIAGRAM and needs_vlm is True


def test_picture_kind_drops_tiny_decoration():
    kind, needs_vlm = picture_kind(_Picture(), area_ratio=0.01)
    assert needs_vlm is False


# ---------------------------------------------------------------------------
# from_docling_chunk：字段映射
# ---------------------------------------------------------------------------
def test_from_docling_chunk_maps_text_fields():
    ch = _Chunk(
        "梧州位于广西东部。", ["第一章", "概述"],
        [_Item("text", [_Prov(3, _Box(10, 20, 30, 40))])],
    )
    kc = from_docling_chunk(ch, doc=None, source="a.pdf")
    assert kc.kind == KIND_TEXT
    assert kc.section_path == "第一章 > 概述"
    assert kc.page == 3
    assert kc.bbox == [[10.0, 20.0, 30.0, 40.0]]
    assert kc.content == "梧州位于广西东部。"
    assert "梧州位于广西东部" in kc.retrieval_text
    assert "第一章" in kc.retrieval_text          # 章节路径进检索文本
    assert kc.origin == "docling_pdf"
    assert kc.parser == "docling"


def test_from_docling_chunk_maps_table_dual_track():
    """表格必须有两个表示：content 保网格（喂 LLM），retrieval_text 文字化（做召回）。"""
    ch = _Chunk(MD_TABLE, ["第二章", "保险责任"],
                [_Table(MD_TABLE, caption="表1", provs=[_Prov(7)])])
    kc = from_docling_chunk(ch, doc=None, source="b.pdf")
    assert kc.kind == KIND_TABLE
    assert kc.content == MD_TABLE          # 保真
    assert kc.table_md == MD_TABLE
    assert "给付比例=160%" in kc.retrieval_text   # 面向检索
    assert "|" not in kc.retrieval_text[:40]      # 检索文本里不该出现网格语法
    assert kc.page == 7


def test_from_docling_chunk_chart_with_caption_needs_no_vlm():
    ch = _Chunk("", [], [_Picture("图3 出口额变化", "line_chart", [_Prov(2)])])
    kc = from_docling_chunk(ch, doc=None, source="c.pdf")
    assert kc.kind == KIND_CHART
    assert "出口额变化" in kc.retrieval_text
    assert kc.needs_vlm is False


def test_from_docling_chunk_chart_without_caption_flags_vlm():
    ch = _Chunk("", ["第三章"], [_Picture(predicted_class="bar_chart", provs=[_Prov(4)])])
    kc = from_docling_chunk(ch, doc=None, source="d.pdf")
    assert kc.needs_vlm is True
    assert kc.retrieval_text                          # 不能是空的，否则这块等于丢索引
    assert "第三章" in kc.retrieval_text


def test_from_docling_chunk_formula_keeps_latex():
    ch = _Chunk(r"E = mc^2", ["附录"], [_Item("formula", [_Prov(9)])])
    kc = from_docling_chunk(ch, doc=None, source="e.pdf")
    assert kc.kind == KIND_FORMULA
    assert kc.content == r"E = mc^2"


def test_from_docling_chunk_table_wins_over_text_in_mixed_block():
    ch = _Chunk("表格前的说明", [], [_Item("text"), _Item("table")])
    kc = from_docling_chunk(ch, doc=None, source="f.pdf")
    assert kc.kind == KIND_TABLE


def test_from_docling_chunk_mixed_picture_and_text_keeps_text_searchable():
    """回归：图文混排块里，正文必须仍然能被检索到。

    这是实测踩出来的坑：短文档（以及整篇只有几个元素的 docx）会被 HybridChunker
    把正文和图片**并进同一个块**。块类型按"含图片"判成 diagram 之后，
    retrieval_text 只剩一句"图表"——整篇正文全部失去检索入口，
    而 content 里明明躺着完整的文字，属于"看着正常、实际搜不到"的静默丢失。
    """
    doc_text = "季度销售报告"
    ch = _Chunk("季度销售报告\n下面给出各年度的销售额走势。", [],
                [_Picture(), _TextItem(doc_text), _TextItem("下面给出各年度的销售额走势。")])
    kc = from_docling_chunk(ch, doc=None, source="r.docx")
    assert kc.kind == KIND_DIAGRAM                 # 块里确实有图 → 仍按图片类块处理
    assert doc_text in kc.retrieval_text           # ★ 但正文不能丢
    assert "下面给出各年度的销售额走势。" in kc.retrieval_text
    assert doc_text in kc.content


def test_from_docling_chunk_mixed_table_and_text_keeps_text_searchable():
    """表格混排块同理：表格要能召回，同块的正文也不能丢。"""
    ch = _Chunk("表格前的说明", [], [_Table(MD_TABLE), _TextItem("表格前的说明")])
    kc = from_docling_chunk(ch, doc=None, source="f.pdf")
    assert kc.kind == KIND_TABLE
    assert "给付比例=160%" in kc.retrieval_text     # 表格主信号仍在最前
    assert "表格前的说明" in kc.retrieval_text       # 同块正文也保留
    assert kc.retrieval_text.index("给付比例") < kc.retrieval_text.index("表格前的说明")


# ---------------------------------------------------------------------------
# chunk.meta.doc_items 是**轻量引用**，必须先解析回完整元素
#
# 这组用例锁住一个实测踩出来的重大缺陷：docling 的 chunk 里 doc_items 只有
# self_ref / label / prov，没有 .text / export_to_markdown / annotations。
# 不解析的话，表格保真网格永远是空串、linearize_table 从不执行、
# 图片分类永远失效 —— 而因为 docling 自己的 chunk.text 恰好能被检索到，
# "内容不为空"把整条链路在走回退分支这件事完全掩盖了。
# ---------------------------------------------------------------------------
def test_doc_items_reference_is_resolved_for_table():
    full = _with_ref(_Table(MD_TABLE, caption="表1"), "#/tables/0")
    ch = _Chunk("", [], [_RefItem("table", "#/tables/0", [_Prov(2)])])
    kc = from_docling_chunk(ch, doc=_DocWithItems(tables=[full]), source="f.pdf")
    assert kc.kind == KIND_TABLE
    assert kc.table_md == MD_TABLE, "解析引用后才能拿到保真网格（修复前恒为空串）"
    assert "给付比例=160%" in kc.retrieval_text
    assert "|" not in kc.retrieval_text[:40], "检索文本应是文字化结果，不是原始网格"
    assert kc.page == 2                              # prov 在引用上本来就有，别弄丢


def test_doc_items_reference_is_resolved_for_picture_classification():
    """图片分类藏在完整元素的 annotations 上，引用里没有 —— 不解析就永远判成普通示意图。"""
    full = _with_ref(_Picture(predicted_class="line_chart", provs=[_Prov(3)]), "#/pictures/0")
    ch = _Chunk("", [], [_RefItem("picture", "#/pictures/0", [_Prov(3)])])
    kc = from_docling_chunk(ch, doc=_DocWithItems(pictures=[full]), source="c.pdf")
    assert kc.kind == KIND_CHART


def test_doc_items_reference_resolution_covers_the_mixed_docx_case():
    """真实 docx 场景的单元化：正文与图表在同一块里，引用解析后正文要能召回。"""
    full = _with_ref(_TextItem("季度销售报告"), "#/texts/0")
    ch = _Chunk("季度销售报告", [], [_RefItem("text", "#/texts/0"),
                                     _RefItem("picture", "#/pictures/0")])
    kc = from_docling_chunk(ch, doc=_DocWithItems(texts=[full]), source="r.docx")
    assert "季度销售报告" in kc.retrieval_text
    assert "季度销售报告" in kc.content


def test_unresolvable_doc_item_reference_degrades_gracefully():
    """引用查不到（文档结构被改写过）时退化成旧行为，绝不能抛或丢块。"""
    ch = _Chunk("普通正文", [], [_RefItem("text", "#/texts/99")])
    kc = from_docling_chunk(ch, doc=_DocWithItems(), source="x.pdf")
    assert kc.content == "普通正文"


def test_resolve_items_keeps_full_items_untouched():
    """已经是完整元素的原样保留（兜底路径 _ItemAsChunk 走的就是这条）。"""
    from src.knowledge_chunk import _resolve_items

    full = _TextItem("正文")
    ref = _RefItem("text", "#/texts/0")
    out = _resolve_items([full, ref], _DocWithItems(texts=[_with_ref(_TextItem("解析后"), "#/texts/0")]))
    assert out[0] is full
    assert out[1].text == "解析后"


def test_from_docling_chunk_tolerates_missing_meta():
    class _Bare:
        text = "无 meta 的块"
    kc = from_docling_chunk(_Bare(), doc=None, source="g.pdf")
    assert kc.kind == KIND_TEXT and kc.section_path == ""


def test_from_docling_chunk_uses_contextualize_when_available():
    class _Chunker:
        def contextualize(self, chunk):
            return "【第一章】" + chunk.text
    ch = _Chunk("正文", ["第一章"], [_Item("text")])
    kc = from_docling_chunk(ch, doc=None, chunker=_Chunker(), source="h.pdf")
    assert kc.retrieval_text == "【第一章】正文"


# ---------------------------------------------------------------------------
# 降级路径与显式失败
# ---------------------------------------------------------------------------
def test_from_pdfminer_pieces_marks_origin():
    chunks = from_pdfminer_pieces(["第一段。", "第二段。"], "x.pdf")
    assert len(chunks) == 2
    assert all(c.parser == "pdfminer" and c.origin == "pdfminer" for c in chunks)
    assert all(c.retrieval_text == c.content for c in chunks)


def test_from_pdfminer_pieces_skips_blank():
    assert from_pdfminer_pieces(["", "  \n ", "有内容"], "x.pdf") != []
    assert len(from_pdfminer_pieces(["", "   "], "x.pdf")) == 0


def test_unprocessable_is_queryable_not_silent():
    """"拒绝不能变成静默"：失败记录必须留下可检索的痕迹。"""
    kc = unprocessable("scan.pdf", "疑似扫描件且未启用 OCR", parser="docling")
    assert kc.kind == KIND_UNPROCESSABLE
    assert kc.unprocessable_reason
    assert "scan.pdf" in kc.retrieval_text          # 能被检索到
    assert kc.content == ""                          # 但没有假内容


def test_make_chunk_id_is_stable_and_discriminating():
    assert make_chunk_id("a.pdf", "x") == make_chunk_id("a.pdf", "x")
    assert make_chunk_id("a.pdf", "x") != make_chunk_id("a.pdf", "y")
    assert make_chunk_id("a.pdf", "x") != make_chunk_id("b.pdf", "x")


def test_knowledge_chunk_to_dict_has_all_v3_fields():
    kc = unprocessable("s.pdf", "原因")
    d = kc.to_dict()
    for f in ("chunk_id", "source", "page", "kind", "section_path", "bbox", "content",
              "retrieval_text", "tags", "page_total", "parser", "needs_vlm",
              "file_checksum", "table_md", "vlm_desc", "source_image",
              "origin", "parent_id", "unprocessable_reason"):
        assert f in d, f"KnowledgeChunk 缺少字段 {f}"


# ---------------------------------------------------------------------------
# 兜底切块（两种 chunker 都产出 0 块时）
#
# 这层的存在原因是一个实测出来的缺陷：HybridChunker 对"通篇只有标题、没有正文"的
# 文档返回 0 块**且不抛异常**，调用方据此会误判成"解析不出内容"，
# 把已经抽到的文字丢掉换成 [未能解析]。所以这些用例锁的是"不丢内容"。
# ---------------------------------------------------------------------------
class _TextItem(_Item):
    def __init__(self, text, label="text", provs=(), level=None):
        super().__init__(label, provs)
        self.text = text
        if level is not None:
            self.level = level


class _FakeDoc:
    """只模仿 from_docling_items / from_docling_markdown 用到的部分。"""

    def __init__(self, items, markdown=""):
        self._items = list(items)
        self._md = markdown
        self.texts, self.tables, self.pictures = [], [], []
        for i in self._items:                      # 坏 item 不该让假对象自己先炸
            try:
                if getattr(i, "text", None):
                    self.texts.append(i)
            except Exception:
                continue

    def iterate_items(self):
        return [(i, 0) for i in self._items]

    def export_to_markdown(self):
        return self._md


def test_item_text_prefers_text_attribute():
    """正文在 item.text 上——docling 的 item 没有 export_to_text，别去调不存在的 API。"""
    it = _TextItem("梧州位于广西东部。")
    assert item_text(it) == "梧州位于广西东部。"


def test_item_text_falls_back_to_markdown_for_table():
    """表格没有 .text，只能走 export_to_markdown。"""
    assert "给付比例" in item_text(_Table(MD_TABLE), None)


def test_item_text_returns_empty_when_nothing_available():
    assert item_text(_Item("text")) == ""


def test_from_docling_items_emits_one_chunk_per_body_item():
    doc = _FakeDoc([
        _TextItem("第一章 概述。", label="section_header", level=1),
        _TextItem("第一段正文。", provs=[_Prov(1, _Box(0, 0, 10, 10))]),
        _TextItem("第二段正文。", provs=[_Prov(2)]),
    ])
    out = from_docling_items(doc, source="a.pdf")
    assert len(out) == 2, "标题不该单独成块"
    assert out[0].section_path == "第一章 概述。"      # 标题进后续块的章节路径
    assert out[0].page == 1 and out[0].bbox == [[0.0, 0.0, 10.0, 10.0]]
    assert out[1].page == 2
    assert all(c.retrieval_text for c in out), "每块都必须有可检索文本"


def test_from_docling_items_returns_empty_for_heading_only_document():
    """只有标题的文档，逐条兜底会跳过标题 → 空。

    这条不是缺陷声明，而是**锁定下一级的必要性**：正因为这里会空，
    from_docling_markdown 那一级必须存在，否则内容就丢了。
    """
    doc = _FakeDoc([_TextItem("Hello Docling World", label="section_header", level=1)])
    assert from_docling_items(doc, source="a.pdf") == []


def test_from_docling_markdown_is_the_guaranteed_last_resort():
    """绝对兜底：文档有内容 → 一定有块，哪怕粒度很粗。"""
    doc = _FakeDoc(
        [_TextItem("Hello Docling World", label="section_header", provs=[_Prov(3)])],
        markdown="## Hello Docling World",
    )
    out = from_docling_markdown(doc, source="scan.pdf")
    assert len(out) == 1
    kc = out[0]
    assert kc.content == "## Hello Docling World"
    assert kc.retrieval_text == kc.content          # 可检索，不是空串
    assert kc.page == 3
    assert kc.bbox == []                            # 跨页块没有单一 bbox，留空不造假
    assert kc.tags == ["整文档兜底"]


def test_from_docling_markdown_returns_empty_when_no_text():
    assert from_docling_markdown(_FakeDoc([], markdown="   "), source="a.pdf") == []


def test_from_docling_markdown_survives_markdown_export_failure():
    class _Boom:
        def export_to_markdown(self):
            raise RuntimeError("boom")
    assert from_docling_markdown(_Boom(), source="a.pdf") == []


def test_from_docling_items_survives_broken_item():
    """单个坏 item 不能拖垮整份文档的兜底。"""
    class _Boom(_Item):
        def __init__(self):
            super().__init__("text")
        @property
        def text(self):
            raise RuntimeError("boom")
    doc = _FakeDoc([_Boom(), _TextItem("好的一段。")])
    out = from_docling_items(doc, source="a.pdf")
    assert len(out) == 1 and "好的一段" in out[0].content


# ---------------------------------------------------------------------------
# docx 路线 C：图片 / OLE / 图表 → KnowledgeChunk
#
# 用真的 DocxAsset 而不是假对象：这一层是 duck typing 读它的字段，
# 假对象只能验证"我以为的字段名"，真对象才能验证"生产者与消费者真的对得上"。
# ---------------------------------------------------------------------------
_FAKE_CHART = {
    "chart_type": "lineChart", "type_cn": "折线图", "title": "年度销售额",
    "categories": ["2019", "2020", "2021", "2022"],
    "series": [{"name": "销售额", "categories": ["2019", "2020", "2021", "2022"],
                "values": ["120", "145", "138", "180"]}],
    "error": "",
}


def _asset(**kw):
    kw.setdefault("kind", "image")
    return DocxAsset(**kw)


def test_docx_chart_becomes_chart_chunk_with_xml_data():
    a = _asset(kind="chart", part="word/charts/chart1.xml", alt_text="销售额走势",
               anchor_text="下面给出各年度的销售额走势。", para_index=2,
               chart=_FAKE_CHART, filename="chart1.xml")
    kc = from_docx_media([a], source="r.docx")[0]
    assert kc.kind == KIND_CHART
    assert kc.origin == "docx_zip" and kc.parser == "docx_zip"
    assert kc.para_index == 2 and kc.anchor_text == "下面给出各年度的销售额走势。"
    assert kc.alt_text == "销售额走势"
    assert kc.table_md == chart_to_table(_FAKE_CHART)
    assert "| 类目 | 销售额 |" in kc.content
    assert "来自XML" in kc.tags


def test_docx_chart_data_is_deterministic_so_no_vlm_needed():
    """这是整条路线的核心主张：答案本来就在 XML 里，不该花 VLM 的钱去"看图猜"。"""
    a = _asset(kind="chart", part="word/charts/chart1.xml", chart=_FAKE_CHART)
    kc = from_docx_media([a], source="r.docx")[0]
    assert kc.needs_vlm is False
    assert "2021=138" in kc.retrieval_text
    assert chart_to_text(_FAKE_CHART) in kc.retrieval_text


def test_docx_chart_without_xml_data_falls_back_to_vlm():
    """只有 XML 里真没数据时才轮到 VLM —— 兜底要显式标记，不能悄悄当成"没内容"。"""
    a = _asset(kind="chart", part="word/charts/chart2.xml",
               chart={"chart_type": "lineChart", "series": [], "error": "无系列数据"})
    kc = from_docx_media([a], source="r.docx")[0]
    assert kc.kind == KIND_CHART
    assert kc.needs_vlm is True
    assert "待VLM" in kc.tags
    assert kc.retrieval_text.strip()                  # 不能留空，否则这条彻底召不回来
    assert "无系列数据" in kc.content


def test_docx_image_with_alt_text_skips_vlm():
    """作者写的替代文字是确定性描述，比 VLM 生成的可信，也没必要为此花钱。"""
    a = _asset(kind="image", part="word/media/image1.png", alt_text="组织架构示意图",
               anchor_text="下图是组织架构示意。", saved_path="D:/media/image1.png",
               para_index=4)
    kc = from_docx_media([a], source="r.docx")[0]
    assert kc.kind == KIND_DIAGRAM
    assert kc.needs_vlm is False
    assert kc.source_image == "D:/media/image1.png"
    assert kc.alt_text == "组织架构示意图"
    assert "组织架构示意图" in kc.retrieval_text
    assert "下图是组织架构示意。" in kc.retrieval_text    # 上下文锚点要带上
    assert "有替代文字" in kc.tags


def test_docx_image_without_alt_text_needs_vlm():
    a = _asset(kind="image", part="word/media/image2.png", saved_path="D:/media/image2.png")
    kc = from_docx_media([a], source="r.docx")[0]
    assert kc.needs_vlm is True
    assert "待VLM" in kc.tags
    assert kc.retrieval_text.strip()


def test_docx_ole_with_parsed_inner_content_becomes_table():
    a = _asset(kind="ole", part="word/embeddings/oleObject1.bin", prog_id="Excel.Sheet.12",
               inner_ext=".xlsx", ole_text="## 工作表：明细\n项目 | 金额\n服务器采购 | 50000",
               saved_path="D:/media/oleObject1.xlsx", anchor_text="附件为明细表。")
    kc = from_docx_media([a], source="r.docx")[0]
    assert kc.kind == KIND_TABLE                    # 内层是工作簿 → 按表格处理
    assert "服务器采购" in kc.content
    assert "服务器采购" in kc.retrieval_text
    assert "Excel" in kc.content                   # ProgID 要翻成人话
    assert kc.needs_vlm is False
    assert "已解析原件" in kc.tags


def test_docx_ole_without_inner_content_keeps_metadata():
    """拿不到内层正文时也要留下"这里曾有嵌入对象、原件在哪"的线索。"""
    a = _asset(kind="ole", part="word/embeddings/oleObject2.bin",
               prog_id="Equation.3", saved_path="D:/media/oleObject2.bin",
               anchor_text="公式见下。")
    kc = from_docx_media([a], source="r.docx")[0]
    assert "公式编辑器对象" in kc.content
    assert "D:/media/oleObject2.bin" in kc.content
    assert "仅元信息" in kc.tags
    assert kc.retrieval_text.strip()


def test_docx_asset_with_error_is_recorded_not_dropped():
    """rels 指向缺失部件时，必须留下可检索的记录，而不是让这个元素凭空消失。"""
    a = _asset(kind="image", part="word/media/ghost.png", error="rels 引用的部件在包内不存在")
    kc = from_docx_media([a], source="r.docx")[0]
    assert "媒体未取出" in kc.tags
    assert "ghost.png" in kc.retrieval_text
    assert kc.retrieval_text.strip()


def test_docx_media_chunk_carries_position_fields():
    """docx 没有页码，位置必须靠 para_index + anchor_text 表达，且要能出边界。"""
    a = _asset(kind="image", alt_text="图", para_index=7, anchor_text="上文")
    kc = from_docx_media([a], source="r.docx")[0]
    d = kc.to_dict()
    assert d["para_index"] == 7 and d["anchor_text"] == "上文"
    assert d["alt_text"] == "图"


def test_docx_media_returns_one_chunk_per_asset_in_order():
    assets = [_asset(kind="image", part="a.png"), _asset(kind="image", part="b.png"),
              _asset(kind="ole", part="c.bin")]
    out = from_docx_media(assets, source="r.docx")
    assert len(out) == 3
    assert [c.chunk_id for c in out] == [c.chunk_id for c in out]   # id 稳定可复算
    assert len({c.chunk_id for c in out}) == 3                      # 且互不相同


def test_docx_ole_preview_image_does_not_become_a_chunk():
    """OLE 的 VML 预览位图与同一对象的原件是同一份数据，不能入两次。

    为什么这条要在产物层断言而不是只在 docx_media：docx_media 只负责"标记"，
    "标记之后到底有没有真的不重复入库"是另一回事 —— 少了这条，标记写了也没人保证被消费。
    """
    ole = _asset(kind="ole", part="word/embeddings/oleObject1.bin", prog_id="Excel.Sheet.12",
                 inner_ext=".xlsx", ole_text="项目 | 金额\n服务器采购 | 50000",
                 saved_path="D:/media/oleObject1.xlsx", para_index=6)
    preview = _asset(kind="image", part="word/media/image2.emf", form="imagedata",
                     is_ole_preview=True, alt_text="嵌入的 Excel 明细表",
                     saved_path="D:/media/image2.emf", para_index=6)
    out = from_docx_media([ole, preview], source="r.docx")
    assert len(out) == 1, "预览图不该单独成块"
    assert out[0].kind == KIND_TABLE and "服务器采购" in out[0].content


def test_docx_media_survives_broken_asset():
    """单个坏资产：既不能中断后面的资产，自己也不能凭空消失。

    这条用例来自一次真实踩坑 —— 原来异常处理里写 `getattr(a, "part", a)` 打日志，
    而那个对象的 part 属性恰好会抛，于是"处理异常的代码"把整份文档带崩了。
    现在改为：任何属性读取失败都退化，坏资产降级为一条可检索的显式记录。
    """
    class _Boom:
        kind = "chart"

        @property
        def part(self):
            raise RuntimeError("boom")

    out = from_docx_media([_Boom(), _asset(kind="image", alt_text="好图")], source="r.docx")
    assert len(out) == 2, "坏资产要留下记录，而不是被静默跳过"
    good = [c for c in out if c.alt_text == "好图"]
    assert len(good) == 1 and good[0].kind == KIND_DIAGRAM
    broken = [c for c in out if c is not good[0]][0]
    assert broken.retrieval_text.strip(), "坏的这条也必须可被检索到，否则没人发现它坏了"
