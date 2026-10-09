"""KnowledgeChunk 转换层测试：全部离线，不需要 docling 也不用下模型。

`from_docling_chunk_all` 是**唯一分块器**，它用 duck typing 读取 DocChunk 与
DoclingDocument 元素（只看 label / prov / text / meta 这些属性），所以可以用假对象
把字段映射测全 —— 这是刻意的设计：解析层换实现时，本文件的测试仍然有效。
"""
import logging

from src.rag.docx_media import DocxAsset, chart_to_table, chart_to_text
from src.rag.knowledge_chunk import (
    _CHART_CLASSES,
    _V25_CLASSES,
    KIND_CHART,
    KIND_DIAGRAM,
    KIND_FORMULA,
    KIND_TABLE,
    KIND_TEXT,
    KIND_UNPROCESSABLE,
    _struct_items,
    from_docling_chunk_all,
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
    def __init__(self, left, top, right, bottom):
        # ⚠️ **属性名必须保持 l/t/r/b**：这个假对象是在模仿 Docling 的
        #    BoundingBox，被测代码读的就是 `.l/.t/.r/.b`。E741 只管参数名，
        #    把属性一起改名会让它不再像真对象，测试会变成假绿。
        self.l, self.t, self.r, self.b = left, top, right, bottom


class _Prov:
    def __init__(self, page_no, box=None):
        self.page_no = page_no
        self.bbox = box


class _Pred:
    """模拟 PictureClassificationClass：分类名在 `class_name` 上。"""

    def __init__(self, class_name):
        self.class_name = class_name


class _Ann:
    """模拟 PictureClassificationData —— **旧路径**容器。

    ⚠️ 字段名是 `predicted_classes`（**复数**）。修复前的实现读的是
    `predicted_class`（单数），而这个类上根本没有那个字段 —— 所以真实环境里
    分类恒为空串。假对象当初照着错误的字段名写，等于把 bug 一起"测绿"了，
    这正是它长期没被发现的原因之一。
    """

    def __init__(self, class_name):
        self.predicted_classes = [_Pred(class_name)] if class_name else []


class _Classification:
    """模拟 PictureClassificationMetaField —— **新路径**容器（docling 2.x 主路径）。"""

    def __init__(self, class_name):
        self._pred = _Pred(class_name)

    def get_main_prediction(self):
        return self._pred


class _PicMeta:
    """承载 PictureItem.meta，本测试只用得到 classification。"""

    def __init__(self, class_name):
        self.classification = _Classification(class_name)


class _Item:
    def __init__(self, label, provs=(), annotations=None, meta=None):
        self.label = _Label(label)
        self.prov = list(provs)
        if annotations is not None:
            self.annotations = annotations
        if meta is not None:
            self.meta = meta


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
    def __init__(self, caption="", predicted_class="", provs=(), via="meta"):
        """分类挂在哪个容器上：via = "meta"（新路径，默认）| "annotations"（旧路径）| "both"。

        默认走新路径，与 docling 2.x 的主路径一致。两条路径的字段名不同
        （`meta.classification.predictions[].class_name` vs
        `annotations[].predicted_classes[].class_name`），所以都要有用例覆盖。
        """
        anns = meta = None
        if predicted_class:
            if via in ("meta", "both"):
                meta = _PicMeta(predicted_class)
            if via in ("annotations", "both"):
                anns = [_Ann(predicted_class)]
        super().__init__("picture", provs, anns, meta)
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


def test_picture_kind_reads_new_meta_path():
    """docling 2.x 主路径：分类写在 meta.classification 上。"""
    kind, needs_vlm = picture_kind(_Picture(predicted_class="bar_chart", via="meta"))
    assert kind == KIND_CHART and needs_vlm is False


def test_picture_kind_reads_legacy_annotations_path():
    """兼容路径：annotations[].predicted_classes[]（**复数**）。

    修复前实现读的是 `ann.predicted_class`（单数）—— 该字段在
    PictureClassificationData 上不存在，所以两条路径都读不到、分类恒为空串。
    这个用例专门锁住复数写法，防止再退回去。
    """
    kind, needs_vlm = picture_kind(_Picture(predicted_class="pie_chart", via="annotations"))
    assert kind == KIND_CHART and needs_vlm is False


def test_picture_kind_recognises_v25_scatter_plot_name():
    """v2.5 的真实类名是 `scatter_plot`；旧表里写的 `scatter_chart` 命不中。"""
    kind, needs_vlm = picture_kind(_Picture(predicted_class="scatter_plot"))
    assert kind == KIND_CHART and needs_vlm is False


def test_picture_kind_sends_flow_chart_to_vlm():
    """流程图在 pdf/docx 里通常是嵌入位图，没有 XML 数据源 —— 必须送 VLM。

    `_CHART_CLASSES` 刻意不收录 flow_chart / geographical_map 这类"看起来像图表、
    实际没有确定性数据源"的类别：判成 needs_vlm=False 会让图里的信息永久丢失。
    """
    kind, needs_vlm = picture_kind(_Picture(predicted_class="flow_chart"))
    assert kind == KIND_DIAGRAM and needs_vlm is True


# DocumentFigureClassifier-v2.5 的真实类别清单（26 个）。在测试里独立写一份，
# 而不是引用源码常量 —— 这样"源码常量漂移了"才会被测出来，否则就是自证自洽。
V25_CLASSES = {
    "logo", "photograph", "icon", "engineering_drawing", "line_chart",
    "bar_chart", "other", "table", "flow_chart", "screenshot_from_computer",
    "signature", "screenshot_from_manual", "geographical_map", "pie_chart",
    "page_thumbnail", "stamp", "music", "calendar", "qr_code", "bar_code",
    "full_page_image", "scatter_plot", "chemistry_structure",
    "topographical_map", "crossword_puzzle", "box_plot",
}


def test_chart_class_keys_all_exist_in_v25_model():
    """映射表的键必须都是 DocumentFigureClassifier-v2.5 的真实类别名。

    旧表照更早的模型写，14 个键里有 8 个在 v2.5 中根本不存在 —— 即使修好读取
    也永远命不中。这个用例把"键名必须对得上模型"变成可执行的约束。
    """
    unknown = set(_CHART_CLASSES) - V25_CLASSES
    assert not unknown, f"这些类名不在 v2.5 模型输出里，永远不会命中：{sorted(unknown)}"


def test_v25_class_list_matches_model():
    """源码里的 26 类白名单必须与模型真实类别**完全一致**。

    它是"tags 里哪个词才是图片分类名"的唯一判据（`image_describer.context_for`
    靠它取 VLM 上下文的 `predicted_class`）。多一个 → 状态标记可能被误判成类别；
    少一个 → 真实分类名被忽略、VLM 丢掉这条提示。所以要求集合相等，不是包含。
    """
    assert _V25_CLASSES == V25_CLASSES, (
        f"源码白名单与 v2.5 不一致：多={sorted(_V25_CLASSES - V25_CLASSES)} "
        f"少={sorted(V25_CLASSES - _V25_CLASSES)}")


# ---------------------------------------------------------------------------
# 唯一分块器 from_docling_chunk_all：字段映射
# ---------------------------------------------------------------------------
def test_from_docling_chunk_maps_text_fields():
    ch = _Chunk(
        "梧州位于广西东部。", ["第一章", "概述"],
        [_Item("text", [_Prov(3, _Box(10, 20, 30, 40))])],
    )
    kc = from_docling_chunk_all(ch, doc=None, source="a.pdf")[0]
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
    kc = from_docling_chunk_all(ch, doc=None, source="b.pdf")[0]
    assert kc.kind == KIND_TABLE
    assert MD_TABLE in kc.content          # 保真网格完整保留
    assert kc.content.startswith("表1")    # 表题也进 content（LLM 要知道表名）
    assert kc.table_md == MD_TABLE
    assert "给付比例=160%" in kc.retrieval_text   # 面向检索
    assert "|" not in kc.retrieval_text[:40]      # 检索文本里不该出现网格语法
    assert kc.page == 7


def test_split_table_block_does_not_repeat_sibling_in_retrieval_text():
    """表格块不与同块正文重复拼接检索文本。

    md 取不到时 `retrieval_text` 已由整块 `chunk.text` 兜底
    （`linearize_table(...) or text`）；若无条件再追加一次 sibling，同一段文字会
    出现两遍 —— 不报错，但白占 token，还让该段在向量空间里被重复加权。

    合流后同块正文有了**自己的块**，所以"不重复"这件事从"同一块内去重"变成了
    "分属两块、各出现一次"。
    """
    ch = _Chunk("表格前的说明", [], [_Item("table"), _TextItem("表格前的说明")])
    out = from_docling_chunk_all(ch, doc=None, source="b2.pdf")
    table_kc = next(k for k in out if k.kind == KIND_TABLE)
    assert table_kc.retrieval_text.count("表格前的说明") == 1
    body_kc = next(k for k in out if k.tags == ["混排块正文"])
    assert body_kc.content == "表格前的说明"


def test_from_docling_chunk_table_retrieval_text_is_single_line():
    """检索文本要折叠成单段，不留「多行 + 尾部空格拼接」的混合形态。"""
    ch = _Chunk(MD_TABLE, ["第二章"], [_Table(MD_TABLE, caption="表1")])
    kc = from_docling_chunk_all(ch, doc=None, source="b3.pdf")[0]
    assert "\n" not in kc.retrieval_text
    assert "给付比例=160%" in kc.retrieval_text
    assert "表1" in kc.retrieval_text


def test_from_docling_chunk_chart_with_caption_needs_no_vlm():
    ch = _Chunk("", [], [_Picture("图3 出口额变化", "line_chart", [_Prov(2)])])
    kc = from_docling_chunk_all(ch, doc=None, source="c.pdf")[0]
    assert kc.kind == KIND_CHART
    assert "出口额变化" in kc.retrieval_text
    assert kc.needs_vlm is False


def test_from_docling_chunk_chart_without_caption_flags_vlm():
    ch = _Chunk("", ["第三章"], [_Picture(predicted_class="bar_chart", provs=[_Prov(4)])])
    kc = from_docling_chunk_all(ch, doc=None, source="d.pdf")[0]
    assert kc.needs_vlm is True
    assert kc.retrieval_text                          # 不能是空的，否则这块等于丢索引
    assert "第三章" in kc.retrieval_text


def test_from_docling_chunk_formula_keeps_latex():
    """公式块的内容取**元素自身**的 LaTeX（不再是整块序列化文本）。"""
    ch = _Chunk("", ["附录"], [_TextItem(r"E = mc^2", label="formula", provs=[_Prov(9)])])
    kc = from_docling_chunk_all(ch, doc=None, source="e.pdf")[0]
    assert kc.kind == KIND_FORMULA
    assert kc.content == r"E = mc^2"
    assert kc.tags == ["公式"]
    assert "附录" in kc.retrieval_text


def test_mixed_table_and_text_block_splits_instead_of_picking_one():
    """混排块不再"表胜出、其余归表" —— 表与正文各成一块。

    旧行为是 `_kind_for_items` 按优先级 table > picture > formula > text 选出**一个**
    代表 kind，块内其余内容只能被吞或挤进同一个块。那条判据已随合流删除 ——
    它正是"1 表 + 1 图 → 图被表格吞掉"这个陷阱的载体。
    """
    ch = _Chunk("表格前的说明", [], [_TextItem("表格前的说明"), _Item("table")])
    out = from_docling_chunk_all(ch, doc=None, source="f.pdf")
    assert sorted(k.kind for k in out) == sorted([KIND_TABLE, KIND_TEXT])
    assert any(k.tags == ["混排块正文"] for k in out), "正文要有自己的块，不能被表格吞掉"


def test_mixed_picture_and_text_block_keeps_text_searchable():
    """回归：图文混排块里，正文必须仍然能被检索到。

    这是实测踩出来的坑：短文档（以及整篇只有几个元素的 docx）会被 HybridChunker
    把正文和图片**并进同一个块**。合流前块类型按"含图片"判成 diagram，图片分支
    只让 retrieval_text 留一句"图表"——整篇正文全部失去检索入口，而 content 里明明
    躺着完整的文字，属于"看着正常、实际搜不到"的静默丢失。

    合流后正文有了自己的块，这条不变式从"图表块里塞进正文"升级为"正文块独立存在"
    —— 结果更强：正文不再依附于图片块的存亡。
    """
    doc_text = "季度销售报告"
    ch = _Chunk("季度销售报告\n下面给出各年度的销售额走势。", [],
                [_Picture(), _TextItem(doc_text), _TextItem("下面给出各年度的销售额走势。")])
    out = from_docling_chunk_all(ch, doc=None, source="r.docx")
    pic_kc = next(k for k in out if k.kind in (KIND_DIAGRAM, KIND_CHART))
    body_kc = next(k for k in out if k.tags == ["混排块正文"])
    assert pic_kc.kind == KIND_DIAGRAM              # 图仍按图片类块处理
    assert doc_text in body_kc.retrieval_text       # ★ 正文不能丢，且有自己的召回位
    assert "下面给出各年度的销售额走势。" in body_kc.content


def test_mixed_table_and_text_block_keeps_text_searchable():
    """表格混排块同理：表格要能召回，同块的正文也不能丢 —— 现在是两块各召回一次。"""
    ch = _Chunk("表格前的说明", [], [_Table(MD_TABLE), _TextItem("表格前的说明")])
    out = from_docling_chunk_all(ch, doc=None, source="f.pdf")
    table_kc = next(k for k in out if k.kind == KIND_TABLE)
    body_kc = next(k for k in out if k.tags == ["混排块正文"])
    assert "给付比例=160%" in table_kc.retrieval_text    # 表格主信号
    assert body_kc.content == "表格前的说明"              # 同块正文有自己的块
    assert "表格前的说明" in body_kc.retrieval_text


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
    kc = from_docling_chunk_all(ch, doc=_DocWithItems(tables=[full]), source="f.pdf")[0]
    assert kc.kind == KIND_TABLE
    assert kc.table_md == MD_TABLE, "解析引用后才能拿到保真网格（修复前恒为空串）"
    assert "给付比例=160%" in kc.retrieval_text
    assert "|" not in kc.retrieval_text[:40], "检索文本应是文字化结果，不是原始网格"
    assert kc.page == 2                              # prov 在引用上本来就有，别弄丢


def test_doc_items_reference_is_resolved_for_picture_classification():
    """图片分类藏在完整元素的 annotations 上，引用里没有 —— 不解析就永远判成普通示意图。"""
    full = _with_ref(_Picture(predicted_class="line_chart", provs=[_Prov(3)]), "#/pictures/0")
    ch = _Chunk("", [], [_RefItem("picture", "#/pictures/0", [_Prov(3)])])
    kc = from_docling_chunk_all(ch, doc=_DocWithItems(pictures=[full]), source="c.pdf")[0]
    assert kc.kind == KIND_CHART


def test_doc_items_reference_resolution_covers_the_mixed_docx_case():
    """真实 docx 场景的单元化：正文与图表在同一块里，引用解析后正文要能召回。

    注意第二个引用 `#/pictures/0` 在 `_DocWithItems` 里**没有对应元素**，解析后
    退化成引用本身（label=picture）—— 它仍会被分成独立一块，只是内容取不到。
    正文块不受它影响，这正是"正文不依附于图表"的意义。
    """
    full = _with_ref(_TextItem("季度销售报告"), "#/texts/0")
    ch = _Chunk("季度销售报告", [], [_RefItem("text", "#/texts/0"),
                                     _RefItem("picture", "#/pictures/0")])
    out = from_docling_chunk_all(ch, doc=_DocWithItems(texts=[full]), source="r.docx")
    body_kc = next(k for k in out if k.tags == ["混排块正文"])
    assert "季度销售报告" in body_kc.retrieval_text
    assert "季度销售报告" in body_kc.content


def test_unresolvable_doc_item_reference_degrades_gracefully():
    """引用查不到（文档结构被改写过）时退化成旧行为，绝不能抛或丢块。"""
    ch = _Chunk("普通正文", [], [_RefItem("text", "#/texts/99")])
    kc = from_docling_chunk_all(ch, doc=_DocWithItems(), source="x.pdf")[0]
    assert kc.content == "普通正文"


def test_resolve_items_keeps_full_items_untouched():
    """已经是完整元素的原样保留（兜底路径 _ItemAsChunk 走的就是这条）。"""
    from src.rag.knowledge_chunk import _resolve_items

    full = _TextItem("正文")
    ref = _RefItem("text", "#/texts/0")
    out = _resolve_items([full, ref], _DocWithItems(texts=[_with_ref(_TextItem("解析后"), "#/texts/0")]))
    assert out[0] is full
    assert out[1].text == "解析后"


def test_from_docling_chunk_tolerates_missing_meta():
    class _Bare:
        text = "无 meta 的块"
    kc = from_docling_chunk_all(_Bare(), doc=None, source="g.pdf")[0]
    assert kc.kind == KIND_TEXT and kc.section_path == ""


def test_from_docling_chunk_uses_contextualize_when_available():
    class _Chunker:
        def contextualize(self, chunk):
            return "【第一章】" + chunk.text
    ch = _Chunk("正文", ["第一章"], [_Item("text")])
    kc = from_docling_chunk_all(ch, doc=None, chunker=_Chunker(), source="h.pdf")[0]
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
    for f in ("chunk_id", "source", "page", "pages", "kind", "section_path", "bbox",
              "content", "retrieval_text", "tags", "page_total", "parser", "needs_vlm",
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


def test_from_docling_items_still_builds_table_chunk():
    """兜底路径遇到表格 item 仍产出 kind=table 的块 —— "结构分支必须活着"的实证。

    为什么能走到：`item_text(表格)` 会回退到 `table_to_markdown`（表格没有 `.text`），
    所以它不会在 `if not text.strip(): continue` 那里被跳过。合流前这条路由
    `from_docling_chunk` 的 `KIND_TABLE` 分支承担，现在由唯一分块器接住 ——
    这也解释了为什么那个函数体内的结构分支不能简单地当"死代码"删掉。
    """
    md = "| 年龄 | 给付比例 |\n| --- | --- |\n| 18-40岁 | 160% |"
    doc = _FakeDoc([_TextItem("正文甲", provs=[_Prov(1)]),
                    _Table(md, caption="表1", provs=[_Prov(3)])])
    out = from_docling_items(doc, source="fb.pdf")
    assert [k.kind for k in out] == [KIND_TEXT, KIND_TABLE]
    table_kc = out[1]
    assert table_kc.table_md == md
    assert table_kc.content.startswith("表1")
    assert table_kc.pages == [3]
    expected = make_chunk_id("fb.pdf",
                             f"t0|{table_kc.pages}|{table_kc.bbox}|{md[:64]}")
    assert table_kc.chunk_id == expected, "兜底路径的元素块同样带 t0| 前缀"


def test_from_docling_items_skips_picture_items_without_text():
    """图片 item 在兜底路径被跳过 —— 既有行为，合流没有改变它。

    `item_text(图片)` 回退到 `table_to_markdown`，而图片上没有表格 → 空串 →
    撞上 `if not text.strip(): continue`。所以兜底路径**产不出图片块**，
    `KIND_DIAGRAM` 那条分支在这里实际到不了（保留它是为了 `table_to_markdown`
    将来对图片返回非空的情形）。

    为什么要显式锁住：这一层是"文档里有内容却解析不出来"的最后防线之一，
    它的覆盖面边界必须写下来，否则"兜底没兜住图"会被误当成 bug 去查。
    """
    doc = _FakeDoc([_Picture("图1", "bar_chart", [_Prov(5)]),
                    _TextItem("正文甲", provs=[_Prov(1)])])
    out = from_docling_items(doc, source="fb.pdf")
    assert [k.kind for k in out] == [KIND_TEXT], "图片 item 因无文本被跳过，不产块"


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


# ---------------------------------------------------------------------------
# 多元素块：一块里有**多个**表格 / 图片，必须逐个成块
#
# 旧实现用 `next(...)` 取第一个匹配元素，等于假设"一个块最多一张表、最多一张图"。
# 这个假设从来没有任何代码保证（块边界由 docling 决定），而假设破裂时代价不对等：
#   - 表格：第二张表的 md 不生成，content 的 `... or text` 因 md 非空短路掉
#     chunk.text 兜底 —— 内容 / 检索文本 / 磁盘产物三处全无，**不可挽回**；
#   - 图片：第二张图的 source_image 挂不上，但文件已落盘，是**可反查的孤儿**。
# ---------------------------------------------------------------------------
MD_TABLE_2 = """\
| 年份 | 出口额 |
| --- | --- |
| 2021 | 1200 |
| 2022 | 1380 |"""


def test_multi_table_block_splits_one_chunk_per_table():
    """两张表必须各成一块 —— 谁也不能吞掉谁。"""
    ch = _Chunk("", ["第三章"], [_Table(MD_TABLE, caption="表1"),
                                 _Table(MD_TABLE_2, caption="表2")])
    out = from_docling_chunk_all(ch, doc=None, source="m.pdf")
    assert len(out) == 2
    assert all(k.kind == KIND_TABLE for k in out)
    assert {k.table_md for k in out} == {MD_TABLE, MD_TABLE_2}, "两张表的保真网格都要在"
    assert any("给付比例=160%" in k.retrieval_text for k in out)
    assert any("出口额=1380" in k.retrieval_text for k in out), "第二张表要能被检索到"
    assert all("第三章" in k.retrieval_text for k in out)


def test_multi_table_block_produces_distinct_chunk_ids():
    """同页、同章节、内容相似的两张表，id 不掺元素序号就会被 chunk_writer 去重吃掉一个。"""
    ch = _Chunk("", [], [_Table(MD_TABLE, provs=[_Prov(3)]),
                         _Table(MD_TABLE_2, provs=[_Prov(3)])])
    out = from_docling_chunk_all(ch, doc=None, source="m.pdf")
    assert len(out) == 2
    assert len({k.chunk_id for k in out}) == 2, "拆出来的块不能撞 id，否则等于白拆"


def test_multi_table_block_keeps_body_text_as_its_own_chunk():
    """同块正文要保留，但**只留一份** —— 复制 N 份会让同一段文字挤占多个召回位。"""
    ch = _Chunk("表格前的说明", [], [_TextItem("表格前的说明"),
                                     _Table(MD_TABLE), _Table(MD_TABLE_2)])
    out = from_docling_chunk_all(ch, doc=None, source="m.pdf")
    assert len(out) == 3, "2 张表 + 1 份正文 = 3 块"
    bodies = [k for k in out if k.kind == KIND_TEXT]
    assert len(bodies) == 1 and "表格前的说明" in bodies[0].content
    assert sum(k.retrieval_text.count("表格前的说明") for k in out) == 1


# ---------------------------------------------------------------------------
# 完整页码（pages）—— `page` 只是起始页，跨页范围得靠它说话
#
# 这个字段存在的唯一理由是"跨页块"。丢了它的后果很具体：引用里写"第 2 页"，
# 而这段正文延续到第 3 页，去核对的人在第 2 页只看得到一半，且完全意识不到
# 还有后半段。所以这几条锁的是"范围不许被截成起点"。
# ---------------------------------------------------------------------------
def test_cross_page_body_chunk_keeps_the_full_page_span():
    """跨页正文块要留下完整页码：`page` 取起始页，`pages` 说清范围。"""
    ch = _Chunk("正文甲", [], [_TextItem("正文甲", provs=[_Prov(2)]),
                               _Table(MD_TABLE, provs=[_Prov(3)]),
                               _Table(MD_TABLE_2, provs=[_Prov(3)])])
    out = from_docling_chunk_all(ch, doc=None, source="m.pdf")
    bodies = [k for k in out if k.kind == KIND_TEXT]
    assert len(bodies) == 1
    assert bodies[0].page == 2, "代表值取起始页"
    assert bodies[0].pages == [2, 3], "跨页范围不能丢"


def test_element_chunk_pages_mirror_its_own_prov():
    """元素块的 `pages` 只指向自己那一页，不受同块其它元素影响。"""
    ch = _Chunk("", [], [_Table(MD_TABLE, provs=[_Prov(4)]),
                         _Table(MD_TABLE_2, provs=[_Prov(4)])])
    out = from_docling_chunk_all(ch, doc=None, source="m.pdf")
    assert all(k.pages == [4] for k in out)


def test_single_chunk_path_records_the_span_too():
    """单元素路径也要带上完整页码 —— 两条路径口径必须一致。"""
    ch = _Chunk("", [], [_TextItem("甲", provs=[_Prov(1)]),
                         _TextItem("乙", provs=[_Prov(2)])])
    kc = from_docling_chunk_all(ch, doc=None, source="s.pdf")[0]
    assert kc.page == 1 and kc.pages == [1, 2]


def test_whole_document_fallback_records_every_page():
    """整文档兜底块跨页最多，页码列表正是为它准备的。"""
    doc = _FakeDoc([_TextItem("甲", provs=[_Prov(1)]),
                    _TextItem("乙", provs=[_Prov(5)])], markdown="甲乙")
    out = from_docling_markdown(doc, source="w.pdf")
    assert out[0].pages == [1, 5] and out[0].page == 1


def test_pages_stay_out_of_chunk_id():
    """`pages` 是元数据，**绝不能进** chunk_id 的 anchor —— 进了会改掉全部既有 id。"""
    a = from_docling_markdown(_FakeDoc([_TextItem("甲", provs=[_Prov(1)])],
                                       markdown="甲"), source="w.pdf")[0]
    b = from_docling_markdown(_FakeDoc([_TextItem("甲", provs=[_Prov(9)])],
                                       markdown="甲"), source="w.pdf")[0]
    assert a.pages != b.pages, "页码确实不同"
    assert a.chunk_id == b.chunk_id, "但 id 只由 source + 文本决定"


def test_multi_picture_block_splits_one_chunk_per_picture():
    ch = _Chunk("", ["第二章"], [_Picture("图1 走势", "line_chart", [_Prov(2)]),
                                 _Picture("图2 占比", "pie_chart", [_Prov(3)])])
    out = from_docling_chunk_all(ch, doc=None, source="c.pdf")
    assert len(out) == 2
    assert all(k.kind == KIND_CHART for k in out)
    assert any("图1 走势" in k.retrieval_text for k in out)
    assert any("图2 占比" in k.retrieval_text for k in out), "第二张图要能被检索到"
    assert len({k.chunk_id for k in out}) == 2


def test_multi_picture_block_hangs_each_own_source_image():
    """每张图都要挂上**自己**的落盘路径 —— 这是图能不能被 VLM 拿到的唯一入口。"""
    p0 = _with_ref(_Picture("图1", "bar_chart"), "#/pictures/0")
    p1 = _with_ref(_Picture("图2", "bar_chart"), "#/pictures/1")
    ch = _Chunk("", [], [p0, p1])
    out = from_docling_chunk_all(
        ch, doc=None, source="c.pdf",
        image_paths={"#/pictures/0": "/m/p0.png", "#/pictures/1": "/m/p1.png"})
    assert {k.source_image for k in out} == {"/m/p0.png", "/m/p1.png"}


def test_multi_element_split_takes_position_from_each_element():
    """拆开后每块的页码取**元素自己**的 prov —— 用整块的会让引用回溯高亮到别人身上。"""
    ch = _Chunk("", [], [_Table(MD_TABLE, provs=[_Prov(2)]),
                         _Table(MD_TABLE_2, provs=[_Prov(7)])])
    out = from_docling_chunk_all(ch, doc=None, source="m.pdf")
    assert {k.page for k in out} == {2, 7}


def test_table_and_picture_in_one_block_each_get_a_chunk():
    """1 表 + 1 图 也拆开：图片不再被表格块吞掉。

    改造前这条锁的是"仍是一块、图被吞"（旧的数量判据 `len(tables)<=1 and
    len(pictures)<=1`）。那是已知缺陷，只因当时不动才被当现状锁住 —— 判据改成
    语义判定（有结构元素就拆）之后，图有自己的一块，表格块也不再登记别人的图题。
    """
    ch = _Chunk("", [], [_Table(MD_TABLE, caption="表1"), _Picture("图1", "bar_chart")])
    out = from_docling_chunk_all(ch, doc=None, source="x.pdf")
    assert len(out) == 2
    assert sorted(k.kind for k in out) == sorted([KIND_TABLE, KIND_CHART])
    pic = [k for k in out if k.kind == KIND_CHART][0]
    assert pic.content == "图1", "图题是这张图自己的内容，不该被表格块吃掉"


def test_single_element_block_now_goes_through_unified_path():
    """单个表格也走统一路径 —— 口径不再随"块里有几个元素"漂移。

    代价：id 多了一段元素前缀（`t0|`），因此与旧的单块路径**不再同 id** ——
    这正是需要重建索引的原因，不是 bug。收益：content 口径与多元素块完全一致
    （不吃整块 text、不附带同块正文），并且不再有"1 表 + 1 图 → 图被吞"这种特例。

    合流后"旧单块路径"已不存在，所以这里改为**直接算一次期望的 anchor** 把前缀钉住：
    `t0|` 就是与旧 id 分道扬镳的那个字符，它要是哪天被摘掉，这条会立刻红。
    """
    ch = _Chunk(MD_TABLE, ["第二章"], [_Table(MD_TABLE, caption="表1")])
    out = from_docling_chunk_all(ch, doc=None, source="b3.pdf")
    assert len(out) == 1
    assert out[0].kind == KIND_TABLE
    assert out[0].table_md == MD_TABLE
    expected = make_chunk_id("b3.pdf", f"t0|{out[0].pages}|{out[0].bbox}|{MD_TABLE[:64]}")
    assert out[0].chunk_id == expected, "元素前缀 t0| 必须留在 anchor 里"


def test_split_path_never_swallows_whole_block_text(caplog):
    """拆分路径下元素自身没内容时：**不吃整块 chunk.text**，只留白 + 标记。

    那个 text 是**整块**所有元素序列化结果的拼接 —— 含同块别人的题注和正文。
    改前它会成为元素块的 content，一旦被召回，喂给 LLM 的原文和给用户看的引用
    片段都与该元素无关（张冠李戴，用户无从察觉）。改后留空，靠 `自身无内容`
    tag + needs_vlm 等批次6 补；而 text 的每一部分本来都有自己的承载块
    （表 → 表格块 / 图 → 图片块 / 其余 → 正文块），所以没有内容被丢掉。
    """
    ch = _Chunk("正文甲。图1 说明。正文乙。", [],
                [_TextItem("正文甲。"), _Picture("图1 说明。", "bar_chart"),
                 _TextItem("正文乙。"), _Picture("", "line_chart")])
    with caplog.at_level(logging.WARNING):
        out = from_docling_chunk_all(ch, doc=None, source="mix.pdf")
    assert len(out) == 3
    bare = [k for k in out if "自身无内容" in k.tags]
    assert len(bare) == 1, "只有自身内容为空的那一块该被打标"
    assert bare[0].content == "", "拆分路径不吃整块 text：那是同块别人的内容"
    assert bare[0].needs_vlm is True, "留白之后必须留着 VLM 这条路，否则信息永久缺失"
    assert "图1 说明。" not in bare[0].content, "兄弟元素的图题不能挂到这一块身上"
    assert len(caplog.records) == 1, "拆分路径下这一块留了白，必须留下 warning 可反查"


def test_split_path_chunk_ids_unchanged_after_fallback_ban():
    """禁用兜底**不得**改动 chunk_id：`text` 仍是 anchor 的原料（`text[:64]`）。

    这是"顺手把 text 传成空串"这个错误实现的报警器 —— 那样 anchor 会变，
    拆分路径所有块的 id 全变：引用回溯失效、增量缓存把旧块全判成"新块"、
    需要全库重建。下面的值是改造**前**实测的基线，必须逐字节一致。
    """
    ch = _Chunk("正文甲。图1 说明。正文乙。", [],
                [_TextItem("正文甲。"), _Picture("图1 说明。", "bar_chart"),
                 _TextItem("正文乙。"), _Picture("", "line_chart")])
    out = from_docling_chunk_all(ch, doc=None, source="mix.pdf")
    assert [k.chunk_id for k in out] == [
        "68115aacabdcf65b", "0b47fc7038c54213", "8c3568c836d0cbff"]

    ch2 = _Chunk("整块序列化文本：年份=2021 出口额=1200", [],
                 [_Table("", caption=""), _Table(MD_TABLE_2, caption="表2")])
    out2 = from_docling_chunk_all(ch2, doc=None, source="fb.pdf")
    assert [k.chunk_id for k in out2] == ["31ad2b1af20baabc", "2a59a5ebab355611"]


def test_element_chunk_does_not_take_caption_twice():
    """题注是**独立 item**，元素块已经用 `caption_text()` 带过它一次。

    不排除 caption 的话 `_sibling_texts` 会把它当"正文"再收一次，同一句图题
    在同一个块里出现两遍（content 与 retrieval_text 都重复，等于两个召回位）。
    """
    cap = _TextItem("图1 说明。", label="caption")
    ch = _Chunk("正文甲。图1 说明。", ["第三章"],
                [_TextItem("正文甲。"), _Picture("图1 说明。", "bar_chart"), cap])
    out = from_docling_chunk_all(ch, doc=None, source="dup.pdf")
    assert len(out) == 2
    pic = [k for k in out if k.kind != KIND_TEXT][0]
    body = [k for k in out if k.kind == KIND_TEXT][0]
    assert pic.content.count("图1 说明。") == 1, f"元素块内题注重复：{pic.content!r}"
    assert pic.retrieval_text.count("图1 说明。") == 1
    assert "图1 说明。" in body.content, "正文块不排 caption：它是孤儿题注的唯一承载块"


def test_bare_element_falls_back_to_whole_text_and_warns(caplog):
    """元素自身没内容、且整块 text 就是它自己的序列化结果 → 兜底照吃，但**要告警**。

    允许兜底的判据是 `sole and not body`（块里只有这一个结构元素、也没有别的文本），
    此时 text 不含别人的内容，吃它没有归属风险 —— 反而是防不可逆丢失的最后一道保险。
    告警与 `自身无内容` tag 保留：这是"元素自身没取到内容"，必须可见。
    """
    ch = _Chunk("兜底文本", [], [_Table("", caption="")])
    with caplog.at_level(logging.WARNING):
        out = from_docling_chunk_all(ch, doc=None, source="fb2.pdf")
    assert len(out) == 1
    assert out[0].content == "兜底文本"
    assert "自身无内容" in out[0].tags
    assert len(caplog.records) == 1, "统一路径下元素自身无内容必须留下 warning"


def test_block_with_text_never_lets_element_eat_whole_text():
    """块里还有别的文本时不兜底：那时 text 含别人的内容，吃它就是张冠李戴。"""
    ch = _Chunk("表格网格\n表格前的说明", [],
                [_Table("", caption=""), _TextItem("表格前的说明")])
    out = from_docling_chunk_all(ch, doc=None, source="fb4.pdf")
    tb = [k for k in out if k.kind == KIND_TABLE][0]
    body = [k for k in out if k.kind == KIND_TEXT][0]
    assert tb.content == "", "有独立正文块时，表块不能吃整块 text"
    assert body.content == "表格前的说明"


def test_formula_and_table_in_one_block_each_get_a_chunk():
    """公式必须独立成块：不能被表格吞掉、也不能混进正文。

    改造前公式的 kind 取决于它跟几个表同块：单独在一块时 kind=formula，
    与 ≥2 张表同块时只是正文串里的一截 LaTeX —— 正是本轮要消除的口径漂移。
    """
    fml = _TextItem(r"E = mc^2", label="formula", provs=[_Prov(3)])
    ch = _Chunk("", ["附录"],
                [_Table(MD_TABLE, caption="表1", provs=[_Prov(3)]), fml])
    out = from_docling_chunk_all(ch, doc=None, source="fx.pdf")
    assert sorted(k.kind for k in out) == sorted([KIND_TABLE, KIND_FORMULA])
    fx = [k for k in out if k.kind == KIND_FORMULA][0]
    assert fx.content == r"E = mc^2"
    assert fx.tags == ["公式"]
    tb = [k for k in out if k.kind == KIND_TABLE][0]
    assert "mc^2" not in tb.content, "公式不能落进表格块的 content"


def test_struct_items_groups_elements_by_label():
    """分块判据的唯一来源：按 label 分成三组，label 变体（equation）也算公式。"""
    items = [_TextItem("正文"), _Table(MD_TABLE), _Picture(),
             _TextItem("x", label="formula"), _TextItem("y", label="equation"),
             _TextItem("题注", label="caption")]
    tables, pictures, formulas = _struct_items(items)
    assert len(tables) == 1
    assert len(pictures) == 1
    assert len(formulas) == 2, "formula 与 equation 都要认"


def test_formula_is_kept_out_of_the_body_chunk():
    """正文块不得收录公式：同一截 LaTeX 不能占两个召回位。"""
    fml = _TextItem(r"\int_0^1 x dx", label="formula", provs=[_Prov(5)])
    ch = _Chunk("正文甲。", ["附录"], [_TextItem("正文甲。"), fml])
    out = from_docling_chunk_all(ch, doc=None, source="fb3.pdf")
    assert len(out) == 2
    body = [k for k in out if k.kind == KIND_TEXT][0]
    assert body.content == "正文甲。"
    fx = [k for k in out if k.kind == KIND_FORMULA][0]
    assert fx.content == r"\int_0^1 x dx"
