"""KnowledgeChunk：Docling 产物 → 本项目检索单元 的转换层（本项目核心，Docling 不管这一层）。

设计要点是**双轨产物**：

    content        —— 保真事实（表格 Markdown / LaTeX / 原文），喂给 LLM 用
    retrieval_text —— 面向检索的自然语言，用于向量化

为什么必须分开：二维表格语法在向量空间里离自然语言问题很远——用户不会用
"| 年龄 | 给付比例 |" 去检索"18 到 40 岁赔多少"。所以表格要额外做一次
**行列文字化**（linearize_table），把它改写成接近句子的形式。

出边界时 retrieval_text 会写进旧的 text 字段（见 src/chunk_writer.py）——
检索层只认 c["text"]，这条契约不能破。
"""
from __future__ import annotations

import copy
import hashlib
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# kind 取值：text | table | chart | diagram | formula | unprocessable
KIND_TEXT = "text"
KIND_TABLE = "table"
KIND_CHART = "chart"
KIND_DIAGRAM = "diagram"
KIND_FORMULA = "formula"
KIND_UNPROCESSABLE = "unprocessable"

# Docling 图片分类 → 我们的 kind（class 名来自 do_picture_classification）
_CHART_CLASSES = {
    "line_chart": KIND_CHART, "bar_chart": KIND_CHART, "pie_chart": KIND_CHART,
    "flow_chart": KIND_DIAGRAM, "scatter_chart": KIND_CHART, "stacked_bar_chart": KIND_CHART,
    "heatmap": KIND_CHART, "box_plot": KIND_CHART, "histogram": KIND_CHART,
    "schematic_diagram": KIND_DIAGRAM, "geographical_map": KIND_DIAGRAM,
    "boxplot": KIND_CHART, "dot_plot": KIND_CHART, "area_chart": KIND_CHART,
}

# 面积占比低于此值的无标注图片按装饰丢弃（Docling 已把页眉页脚归入 furniture，
# 这里只兜底那些混在正文里的分隔线、小图标）
_MIN_AREA_RATIO = 0.03


@dataclass
class KnowledgeChunk:
    """统一检索单元。字段含义见 PLAN_doc_parsing_v3.md §10。"""

    chunk_id: str = ""
    source: str = ""
    page: Optional[int] = None
    kind: str = KIND_TEXT
    section_path: str = ""
    bbox: list = field(default_factory=list)          # [[l, t, r, b], ...]，可跨页
    # docx 是流式格式，没有页码与 bbox，位置只能这样表达：
    #   para_index  —— 文档流第几段（精确）
    #   anchor_text —— 该元素所在段落文字，为空则回退到上文最近的非空段落（给检索用的上下文）
    para_index: Optional[int] = None
    anchor_text: str = ""
    content: str = ""                                  # 保真事实
    retrieval_text: str = ""                           # 出边界时写入旧 text 字段
    tags: list = field(default_factory=list)
    page_total: Optional[int] = None
    parser: str = ""                                   # docling | docling_partial | pdfminer
    needs_vlm: bool = False
    file_checksum: str = ""
    table_md: str = ""                                 # 表格保真表示（Markdown 网格）
    alt_text: str = ""                                 # 作者手写的替代文字（docx 图片/图表）
    vlm_desc: Optional[dict] = None                    # 批次6 填
    source_image: str = ""                             # 裁剪出的插图/媒体原件路径（批次6 填）
    page_image: str = ""                               # 原页截图（或按 bbox 裁出的区域），供用户回溯核对
    origin: str = ""                                   # docling_pdf | docling_docx | docx_zip | pdfminer
    parent_id: str = ""                                # 批次8 父子块预留
    unprocessable_reason: Optional[str] = None         # 拒绝要显式，不静默

    def to_dict(self) -> dict:
        return asdict(self)


def make_chunk_id(source: str, anchor: str) -> str:
    """内容寻址的稳定 id：源不变 → id 不变，便于增量与去重。"""
    raw = f"{source}|{anchor}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 表格
# ---------------------------------------------------------------------------
def _split_md_row(line: str) -> list[str]:
    """拆一行 Markdown 表格（处理首尾竖线与 \\| 转义）。"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]


def _is_separator_row(cells: list[str]) -> bool:
    """GFM 表格的分隔行：|---|---| 或 |:--:|--| 这类。"""
    real = [c for c in cells if c]
    return bool(real) and all(set(c) <= set("-: ") and "-" in c for c in real)


def linearize_table(table_md: str, caption: str = "", max_rows: int = 200) -> str:
    """Markdown 表格 → "列=值；列=值" 的自然语言行。

    这是本项目自己的设计：Docling 只给结构化网格，检索能不能召回要靠这里改写。
    """
    lines = [l for l in (table_md or "").splitlines() if l.strip()]
    if not lines:
        return ""

    header = _split_md_row(lines[0])
    if _is_separator_row(header):
        header = []              # 首行就是分隔行 → 该表没有表头，改用位置键
    out: list[str] = []
    for line in lines[1:]:
        cells = _split_md_row(line)
        if _is_separator_row(cells):
            continue
        pairs = []
        for i, val in enumerate(cells):
            if not val:
                continue
            key = header[i] if i < len(header) and header[i] else f"列{i + 1}"
            pairs.append(f"{key}={val}")
        if pairs:
            out.append("；".join(pairs))
        if len(out) >= max_rows:
            break

    if not out:
        return ""
    body = "\n".join(out)
    return f"{caption}：\n{body}" if caption else body


def table_to_markdown(table_item: Any, doc: Any = None) -> str:
    """Docling TableItem → Markdown。取不到就返回空串，不抛。"""
    fn = getattr(table_item, "export_to_markdown", None)
    if fn is None:
        return ""
    for kwargs in ({"doc": doc}, {}):
        try:
            return fn(**kwargs) or ""
        except TypeError:
            continue
        except Exception:
            return ""
    return ""


# ---------------------------------------------------------------------------
# 元素判定（用 label 字符串而非 isinstance，避免 knowledge_chunk 依赖 docling）
# ---------------------------------------------------------------------------
def _label_of(item: Any) -> str:
    lab = getattr(item, "label", None)
    if lab is None:
        return ""
    return str(getattr(lab, "value", lab)).lower()


# 给别的模块用（docling_loader 的兜底路径要按同一套 label 判定走）
label_of = _label_of


def item_text(item: Any, doc: Any = None) -> str:
    """取单个 Docling item 的文本表示，取不到返回空串。

    ⚠️ 实测踩坑：docling 的 item 上**没有** export_to_text。它只有
    export_to_doctags / export_to_document_tokens 这类标记流，混着坐标与结构标记，
    直接当正文入库会把噪声喂给向量模型。正文实际存在各 item 的 `.text` 上；
    表格、图片没有 `.text`，才需要走 export_to_markdown（复用 table_to_markdown 的容错）。
    """
    txt = getattr(item, "text", None)
    if isinstance(txt, str) and txt.strip():
        return txt.strip()
    return table_to_markdown(item, doc)


def _predicted_class(item: Any) -> str:
    """从 PictureItem.annotations 里取图片分类（do_picture_classification 产出）。"""
    for ann in getattr(item, "annotations", None) or []:
        cls = getattr(ann, "predicted_class", None)
        if cls:
            return str(cls).lower()
    return ""


def _image_path_of(item: Any, image_paths: Optional[dict]) -> str:
    """取该图片元素落盘后的本地路径（见 docling_loader._save_pictures）。

    找不到就返回空串 —— 上游"这张图没落盘"与"这张图不该落盘"是两件事，
    由下游（批次6 的 VLM 步骤）显式记成"无图可取"，绝不能在这里假装有图。
    """
    if not image_paths or item is None:
        return ""
    ref = getattr(item, "self_ref", None)
    if isinstance(ref, str) and ref in image_paths:
        return image_paths[ref] or ""
    return ""


def _kind_for_items(items: list[Any]) -> str:
    """按优先级判定块类型：表格 > 图片 > 公式 > 文本。"""
    labels = {_label_of(i) for i in items}
    if "table" in labels:
        return KIND_TABLE
    if "picture" in labels:
        return KIND_DIAGRAM
    if "formula" in labels or "equation" in labels:
        return KIND_FORMULA
    return KIND_TEXT


def _sibling_texts(items: list[Any], exclude=("picture", "table")) -> str:
    """取同块内**除图片/表格外**的正文文本，用 "；" 连接。

    为什么需要它：短文档（以及整篇只有几个元素的 docx）会被 HybridChunker 把
    正文和图片**并进同一个块**。此时块类型被判成"图片"，如果只说图片的事，
    块里的正文就会从检索文本里彻底消失。

    实测后果很严重：一份 docx 的整篇正文（四季段落 + 图表说明）挤在一个
    kind=diagram 的块里，retrieval_text 只剩一句"图表"，正文全部失去检索入口，
    而 content 里明明躺着完整的文字——属于"看起来正常、实际搜不到"的静默丢失。
    """
    out = []
    for it in items:
        if _label_of(it) in exclude:
            continue
        t = getattr(it, "text", None)
        if isinstance(t, str) and t.strip():
            out.append(t.strip())
    return "；".join(out)


def picture_kind(item: Any, area_ratio: Optional[float] = None) -> tuple[str, bool]:
    """图片 → (kind, 是否值得送 VLM)。

    判定顺序与 PLAN_doc_parsing_v3.md §6.4 一致：有 caption 或有分类 → 有价值；
    面积过小且无标注 → 装饰，丢弃。
    """
    cls = _predicted_class(item)
    if cls in _CHART_CLASSES:
        return _CHART_CLASSES[cls], False
    if area_ratio is not None and area_ratio < _MIN_AREA_RATIO:
        return KIND_DIAGRAM, False                      # 装饰
    return KIND_DIAGRAM, True                           # 位图，本轮只记元信息


def _collect_prov(items: list[Any]) -> tuple[list[int], list[list[float]]]:
    """收集页码与 bbox，供引用溯源与"渲染该页截图"使用。"""
    pages: list[int] = []
    boxes: list[list[float]] = []
    for it in items:
        for prov in getattr(it, "prov", None) or []:
            pno = getattr(prov, "page_no", None)
            if pno is not None and pno not in pages:
                pages.append(int(pno))
            bb = getattr(prov, "bbox", None)
            if bb is None:
                continue
            try:
                boxes.append([float(bb.l), float(bb.t), float(bb.r), float(bb.b)])
            except Exception:
                continue
    return sorted(pages), boxes


# ---------------------------------------------------------------------------
# 元素解析：chunk.meta.doc_items 里装的可能是**轻量引用**，不是完整元素
# ---------------------------------------------------------------------------
def _ref_index(doc: Any) -> dict:
    """`self_ref` → 完整元素。用于把 chunk 里的引用还原成带正文的对象。"""
    idx: dict[str, Any] = {}
    if doc is None:
        return idx
    for attr in ("texts", "tables", "pictures", "key_value_items", "form_items",
                 "field_items", "groups"):
        try:
            seq = getattr(doc, attr, None) or []
        except Exception:  # noqa: BLE001
            continue
        for it in seq:
            ref = getattr(it, "self_ref", None)
            if isinstance(ref, str) and ref:
                idx.setdefault(ref, it)
    return idx


def _resolve_items(items: list[Any], doc: Any) -> list[Any]:
    """把 chunk 里的元素引用还原成完整元素。

    ⚠️ 这是实测踩出来的关键差异，不修会让整条转换链路静默走空分支：

    对 PDF 与 docx 都验证过，`chunk.meta.doc_items` 里装的其实是 **`DocItem` 轻量引用**
    ——只有 `self_ref` / `label` / `prov`，**没有 `.text`，没有 `export_to_markdown`，
    也没有 `annotations`**。于是：
      - `table_to_markdown()` 在引用上取不到方法 → 返回空串 → `table_md` 永远为空，
        `linearize_table()`（本项目核心设计）拿到空串，**从未真正执行**；
      - `_predicted_class()` 读到空的 annotations → 图表/示意图的分类**永远失效**。

    为什么一直没暴露：docling 自己的 `chunk.text` 恰好把表格序列化成
    "East, Revenue = 1200. West, Revenue = 980" 这种准自然语言，检索看着还行，
    "内容不为空"就把"整条链路在走回退分支"掩盖了。

    `prov` 在引用上是有的（所以页码/bbox 一直是对的），因此这里只替换、不合并。
    已经是完整元素的（有 `.text` 或 `export_to_markdown`）原样保留，
    解析不到引用时也原样保留 —— 宁可退化成旧行为，也不能把元素弄丢。
    """
    if doc is None:
        return list(items)

    idx = None
    out = []
    for it in items:
        if hasattr(it, "text") or hasattr(it, "export_to_markdown"):
            out.append(it)
            continue
        ref = getattr(it, "self_ref", None)
        if idx is None:
            idx = _ref_index(doc)
        full = idx.get(ref) if isinstance(ref, str) else None
        if full is None:
            out.append(it)
            continue
        # 位置信息以"有"为准：引用上带着 prov 而完整元素没有时，替换会把页码/bbox 弄丢。
        # 复制一份再补，避免直接改库对象（同一个 doc 可能在别处被复用）。
        if not getattr(full, "prov", None) and getattr(it, "prov", None):
            try:
                full = copy.copy(full)
                full.prov = list(it.prov)
            except Exception as exc:  # noqa: BLE001
                logger.debug("补 prov 失败，用完整元素原样：%s", exc)
        out.append(full)
    return out


# ---------------------------------------------------------------------------
# 转换入口
# ---------------------------------------------------------------------------
def from_pdfminer_pieces(pieces: list[str], source: str,
                         page_total: Optional[int] = None) -> list[KnowledgeChunk]:
    """降级路径：把 pdf_parser.pdf_to_chunks 的 str 列表包成 KnowledgeChunk。

    产出与 Docling 路径同构，下游不必区分来源；origin 字段保留线索，
    便于排查"这份文件其实走的是降级链路"。
    """
    out = []
    for p in pieces or []:
        if not p or not p.strip():
            continue
        out.append(KnowledgeChunk(
            source=source, kind=KIND_TEXT, content=p, retrieval_text=p,
            parser="pdfminer", origin="pdfminer", page_total=page_total,
            chunk_id=make_chunk_id(source, f"pdfminer|{p[:64]}"),
        ))
    return out


def unprocessable(source: str, reason: str, parser: str = "",
                  page_total: Optional[int] = None) -> KnowledgeChunk:
    """显式失败：解析不出内容时不能静默丢弃，要留下可查询的记录。

    retrieval_text 刻意给一句可检索的说明而不是留空——留空等于这条失败记录
    彻底消失，而"哪些文件没解析成功"本身是运维必须能查到的信息。
    """
    return KnowledgeChunk(
        source=source, kind=KIND_UNPROCESSABLE, parser=parser,
        page_total=page_total, unprocessable_reason=reason,
        content="",
        retrieval_text=f"[未能解析] {source}：{reason}",
        tags=["未解析"],
        chunk_id=make_chunk_id(source, f"unprocessable|{reason}"),
    )


def from_docling_chunk(chunk: Any, doc: Any, *, chunker: Any = None,
                       source: str = "", page_total: Optional[int] = None,
                       parser: str = "docling", origin: str = "docling_pdf",
                       file_checksum: str = "",
                       image_paths: Optional[dict] = None) -> KnowledgeChunk:
    """Docling DocChunk → KnowledgeChunk。

    用 duck typing 读取 chunk.meta（headings / doc_items），不 import docling，
    这样未装 docling 时本模块仍可被导入与测试。

    image_paths：`{self_ref: 本地图片路径}`（见 docling_loader._save_pictures）。
    为什么要单独传进来：docling 的 `generate_picture_images=True` 只把图放在内存里，
    **不落盘**；而下游（VLM 描述、随回答返回图片）都需要一个能读的文件。
    """
    meta = getattr(chunk, "meta", None)
    headings = list(getattr(meta, "headings", None) or [])
    # 必须先解析引用：见 _resolve_items —— 引用上没有 .text / export_to_markdown / annotations
    items = _resolve_items(list(getattr(meta, "doc_items", None) or []), doc)
    section_path = " > ".join(h for h in headings if h)

    kind = _kind_for_items(items)
    pages, boxes = _collect_prov(items)
    text = (getattr(chunk, "text", "") or "").strip()
    anchor = f"{pages}|{boxes}|{text[:64]}"

    kc = KnowledgeChunk(
        source=source, page=pages[0] if pages else None, kind=kind,
        section_path=section_path, bbox=boxes, page_total=page_total,
        parser=parser, origin=origin, file_checksum=file_checksum,
        chunk_id=make_chunk_id(source, anchor),
    )

    if kind == KIND_TABLE:
        table_item = next((i for i in items if _label_of(i) == "table"), None)
        md = table_to_markdown(table_item, doc) if table_item is not None else ""
        caption = ""
        if table_item is not None:
            try:
                caption = table_item.caption_text(doc) or ""
            except Exception:
                caption = ""
        kc.table_md = md
        # 混排块里除表格外的正文：既进检索文本，也进 content（喂 LLM 时上下文不丢）
        sibling = _sibling_texts(items)
        kc.content = "\n".join(p for p in (sibling, md) if p) or text
        kc.retrieval_text = linearize_table(md, caption) or text
        if sibling:
            kc.retrieval_text = " ".join(p for p in (kc.retrieval_text, sibling) if p)
        # 章节路径**追加在末尾**：表格数值是强信号，必须留在最前面（嵌入对开头更敏感），
        # 但"这张表属于哪一节"对"第三章的出口数据是多少"这类问题是必需的召回线索。
        # 其它分支（text/formula）都把 section_path 写进了检索文本，表格分支漏了 —— 补齐口径。
        if section_path:
            kc.retrieval_text = f"{kc.retrieval_text}（章节：{section_path}）"
        kc.tags = ["表格"]

    elif kind == KIND_DIAGRAM:
        pic = next((i for i in items if _label_of(i) == "picture"), None)
        caption = ""
        if pic is not None:
            try:
                caption = pic.caption_text(doc) or ""
            except Exception:
                caption = ""
        cls = _predicted_class(pic) if pic is not None else ""
        kc.kind, kc.needs_vlm = picture_kind(pic) if pic is not None else (KIND_DIAGRAM, True)
        kc.source_image = _image_path_of(pic, image_paths)
        sibling = _sibling_texts(items)
        if sibling:
            # 图文混排：块里的正文才是可检索的主体，不能因为"含图片"就丢掉
            kc.content = "\n".join(p for p in (caption, sibling) if p)
        else:
            kc.content = caption or text
        # 描述留空：批次6 由 VLM 补；本轮先用 caption + 分类 + 章节路径保证可召回
        parts = [p for p in (section_path, caption, cls.replace("_", " "), sibling) if p]
        kc.retrieval_text = " ".join(parts) or "图表"
        kc.tags = [t for t in (cls, "图表") if t]
        if not caption:
            kc.needs_vlm = True

    elif kind == KIND_FORMULA:
        kc.content = text
        # LaTeX 离自然语言问题很远，先原样入索引；批次6 可补一句自然语言描述
        kc.retrieval_text = f"{section_path}：{text}" if section_path else text
        kc.tags = ["公式"]

    else:
        kc.content = text
        ctx = None
        if chunker is not None:
            try:
                ctx = chunker.contextualize(chunk)      # 标题路径 + 正文
            except Exception:
                ctx = None
        kc.retrieval_text = (ctx or (f"{section_path}：{text}" if section_path else text)).strip()

    return kc


# ---------------------------------------------------------------------------
# 兜底：两种 chunker 都产出 0 块时，按 item 逐条切
# ---------------------------------------------------------------------------
class _ItemAsChunk:
    """把单个 Docling item 包装成 DocChunk 的形状，只为复用 from_docling_chunk 的字段映射。

    它只在兜底路径内部流转，**绝不交给 chunker.contextualize**——那需要真正的
    DocChunk（serialize 之类的方法它没有）。所以调 from_docling_chunk 时必须不传 chunker。
    """

    class _Meta:
        __slots__ = ("headings", "doc_items")

        def __init__(self, headings: list, doc_items: list):
            self.headings = headings
            self.doc_items = doc_items

    def __init__(self, item: Any, headings: list, text: str):
        self.meta = _ItemAsChunk._Meta(headings, [item])
        self.text = text


def from_blocks(blocks: list, source: str, *, parser: str = "loader",
                origin: str = "file_loader", page_total: Optional[int] = None,
                file_checksum: str = "",
                splitter: Optional[Any] = None) -> list[KnowledgeChunk]:
    """`loaders.Block` → list[KnowledgeChunk]（duck typing 读属性，本模块不依赖 loaders）。

    为什么其它格式需要一个专门的入口：csv/xlsx/pptx/json/md 这些格式**各自**能说清
    一部分结构（表头、工作表、页序、标题路径、记录边界），但那部分结构只存在于
    loaders 的 Block 里。走 `load_text` 把它们拍成一段字符串，结构就永久丢了 ——
    "这张表属于哪个工作表""这一页讲什么"全都找不回来。

    `splitter`：给超大块（例如一整个工作表）准备的二次切分回调。
    有它就会把一个块切成多块，**并保持 section/page 不变** ——
    切开的只是长度，归属不该变。
    """
    out: list[KnowledgeChunk] = []
    if not blocks:
        return out

    for i, b in enumerate(blocks):
        text = (getattr(b, "text", "") or "").strip()
        section = (getattr(b, "section", "") or "").strip()
        page = getattr(b, "page", None)
        tags = list(getattr(b, "tags", None) or [])
        kind = getattr(b, "kind", "") or KIND_TEXT
        img = getattr(b, "source_image", "") or ""
        if not text and not img:
            continue

        pieces = [text]
        if splitter is not None and text:
            try:
                parts = splitter(text)
                pieces = [p for p in parts if (p or "").strip()] or [text]
            except Exception as exc:  # noqa: BLE001  切分失败就整块留着，绝不丢
                logger.warning("块二次切分失败，按整块保留：%s", exc)

        for j, piece in enumerate(pieces):
            retrieval = _with_section(piece, section)
            kc = KnowledgeChunk(
                source=source, page=page, kind=kind, section_path=section,
                page_total=page_total, parser=parser, origin=origin,
                file_checksum=file_checksum, content=piece, retrieval_text=retrieval,
                tags=tags, source_image=img,
                needs_vlm=bool(getattr(b, "needs_vlm", False)),
                chunk_id=make_chunk_id(source, f"block|{i}|{j}|{page}|{piece[:64]}"),
            )
            out.append(kc)
    return out


def _with_section(text: str, section: str) -> str:
    """把标题路径并进检索文本 —— 但**只在它还没出现时**。

    md / xlsx / pptx 的正文块里往往已经带了标题行或工作表名（我们刻意这么写的，
    因为检索只嵌入 `text`）。无条件再拼一次会让同一句话在向量里出现两遍，
    既浪费 token 又会让相似度虚高。
    """
    if not text:
        return section
    if not section:
        return text
    leaf = section.split(" > ")[-1].strip()
    if leaf and leaf in text:
        return text
    return f"{section}；{text}"


def from_docling_items(doc: Any, *, source: str, parser: str = "docling",
                       origin: str = "docling_pdf",
                       page_total: Optional[int] = None,
                       file_checksum: str = "",
                       image_paths: Optional[dict] = None) -> list[KnowledgeChunk]:
    """按 item 逐条兜底切块。仅在两种 chunker 都产出 0 块时使用。

    为什么必须有这条：HybridChunker 对"通篇只有标题、没有正文 item"的文档会返回
    **0 块且不报错**——标题负责开章节，章节里没有 body 就不产出任何块。实测扫描件
    被 OCR 成 section_header 时正好命中：docling 明明抽到了文字，切块返回 0，
    调用方若据此判定"解析不出内容"，就把已有内容丢掉换成 [未能解析]，
    属于**静默数据丢失**。

    这里刻意复用 from_docling_chunk 而不是另写一份映射：kind 判定、bbox 收集、
    表格文字化、图片价值判定都住在那个函数里，另写一份迟早长歪，
    而兜底路径恰恰是最没人测的地方。注意不传 chunker（见 _ItemAsChunk 的说明）。
    """
    out: list[KnowledgeChunk] = []
    headings: list[str] = []
    for entry in doc.iterate_items():
        item = entry[0] if isinstance(entry, tuple) else entry
        try:
            text = item_text(item, doc)
        except Exception as exc:
            logger.warning("取 item 文本失败，跳过：%s", exc)
            continue

        if _label_of(item) in ("title", "section_header"):
            if text:
                # level 是章节层级（1 起）；拿不到就按"更深一层"处理
                depth = max(int(getattr(item, "level", None) or len(headings) + 1) - 1, 0)
                headings = headings[:depth] + [text]
            continue                       # 标题本身不成块，只进后续块的 section_path

        if not text.strip():
            continue
        try:
            out.append(from_docling_chunk(
                _ItemAsChunk(item, [h for h in headings if h], text), doc,
                source=source, parser=parser, origin=origin,
                page_total=page_total, file_checksum=file_checksum,
                image_paths=image_paths,
            ))
        except Exception as exc:
            logger.warning("兜底切块：单个 item 转换失败，跳过：%s", exc)
    return out


def from_docling_markdown(doc: Any, *, source: str, parser: str = "docling",
                          origin: str = "docling_pdf",
                          page_total: Optional[int] = None,
                          file_checksum: str = "") -> list[KnowledgeChunk]:
    """最后兜底：整份文档的 Markdown 作为**单块**。

    它保证一个不变式：**文档里有内容 → 一定有块产出**。

    为什么还需要这一级：前三级都会"挑食"。实测扫描件被 OCR 成单个 section_header 时，
    HybridChunker 返回 0 块（标题开的章节没有正文）、HierarchicalChunker 同样跳过，
    连逐条兜底也把标题当 heading 跳过——三条路都空，而已抽到的文字就在文档里。
    这时宁可给一个粗粒度大块，也不能丢内容：粗块的检索质量可以再优化，
    内容丢了就是永久丢失且无人知晓。

    代价说清楚：这条路放弃了粒度，整份文档挤进一个块，长文档会超出向量模型上限。
    因此它只在前面几级全空时触发，正常路径永远走不到。
    """
    text = ""
    fn = getattr(doc, "export_to_markdown", None)
    if fn is not None:
        try:
            text = (fn() or "").strip()
        except Exception as exc:
            logger.warning("导出整文档 Markdown 失败：%s", exc)
    if not text:
        return []

    pages: list[int] = []
    for attr in ("texts", "tables", "pictures"):
        for it in (getattr(doc, attr, None) or []):
            for prov in getattr(it, "prov", None) or []:
                pno = getattr(prov, "page_no", None)
                if pno is not None and int(pno) not in pages:
                    pages.append(int(pno))
    pages.sort()

    return [KnowledgeChunk(
        source=source, page=pages[0] if pages else None, kind=KIND_TEXT,
        # 整文档块没有"一个"bbox（它跨页跨区域），留空比给个假的更诚实
        bbox=[], page_total=page_total, parser=parser, origin=origin,
        file_checksum=file_checksum, content=text, retrieval_text=text,
        tags=["整文档兜底"],
        chunk_id=make_chunk_id(source, f"whole|{text[:64]}"),
    )]


# ---------------------------------------------------------------------------
# docx 路线 C 产物：图片 / OLE / 图表（Docling 拿不到的那一半）
# ---------------------------------------------------------------------------
# 这些扩展名的内层原件本身就是"表格"，按表格处理能让检索更容易命中数值
_TABULAR_INNER = (".xlsx", ".xlsm", ".csv", ".tsv")


def _anchor_prefix(anchor: str) -> str:
    """把上下文锚点整理成可拼接的前缀。"""
    a = (anchor or "").strip()
    return a


def _safe(obj: Any, name: str, default: Any = "") -> Any:
    """读属性，任何异常都退化为默认值。

    为什么不能直接 getattr：本层是 duck typing 读外部对象的字段，**连异常处理本身**
    都必须不会抛 —— 实测踩过：日志里写 `getattr(a, "part", a)`，而那个对象的
    part 属性恰好会抛异常，于是"处理异常的代码"自己把整份文档带崩了。
    """
    try:
        v = getattr(obj, name, default)
    except Exception:  # noqa: BLE001
        return default
    return default if v is None else v


def from_docx_media(assets: list, source: str, *, page_total: Optional[int] = None,
                    file_checksum: str = "") -> list[KnowledgeChunk]:
    """docx 媒体资产（src/docx_media.py 产出）→ list[KnowledgeChunk]。

    核心原则贯穿每一类：**先用确定性来源，确定性拿不到才标 needs_vlm 交给 VLM**。
      - 图表 → 数据就在 chart XML 里，直接写死成文字（needs_vlm=False）
      - 图片 → 作者写的替代文字（alt）是确定性的；没有才需要 VLM
      - OLE  → 内层原件用 loaders 解析；解析不了才只留元信息

    与 from_docling_* 系列不同，这里的输入不是 DoclingDocument 而是我们自己的
    DocxAsset（duck typing 读属性），所以本模块依旧不依赖 docling。
    产出与正文块同构，出边界后走同一个 to_legacy。
    """
    out: list[KnowledgeChunk] = []
    previews = 0
    for a in assets or []:
        # OLE 对象的预览位图不成块：它是同一个对象的渲染表示，内容已由内层原件
        # （解析得干干净净的文字）表达，再入一块就是同一份数据的第二次出现，
        # 且预览多为 EMF/WMF 这类 VLM 读不了的矢量格式。标记见 docx_media._mark_ole_previews。
        if _safe(a, "is_ole_preview", False):
            previews += 1
            logger.debug("跳过 OLE 预览图（内容已由同段落 OLE 原件表达）：%s",
                         _safe(a, "part", "?"))
            continue
        try:
            out.append(_docx_asset_to_chunk(a, source, page_total, file_checksum))
        except Exception as exc:  # noqa: BLE001  单个资产出问题不该毁掉整份文档
            logger.warning("docx 媒体转块失败，跳过：%s（%s）",
                           _safe(a, "part", "?"), exc)
    if previews:
        logger.info("docx 媒体：%d 个 OLE 预览图未单独成块（同一对象的原件已入库）", previews)
    return out


def _docx_asset_to_chunk(asset: Any, source: str, page_total: Optional[int],
                         file_checksum: str) -> KnowledgeChunk:
    from .docx_media import PROG_ID_CN, chart_to_table, chart_to_text

    kind = _safe(asset, "kind")
    part = _safe(asset, "part")
    anchor = _anchor_prefix(_safe(asset, "anchor_text"))
    alt = (_safe(asset, "alt_text") or "").strip()
    para_index = _safe(asset, "para_index", None)
    error = _safe(asset, "error")
    saved = _safe(asset, "saved_path")

    kc = KnowledgeChunk(
        source=source, kind=KIND_TEXT, section_path="", page=None,
        para_index=para_index, anchor_text=anchor, page_total=page_total,
        parser="docx_zip", origin="docx_zip", file_checksum=file_checksum,
        source_image=saved,
        chunk_id=make_chunk_id(source, f"docxzip|{kind}|{part}|{para_index}"),
    )

    # ⚠️ 这个分支必须排在所有 kind 分派之前。
    # 否则"部件没取出来"会被下面的 image 分支接走，变成一条"待 VLM 描述的图片"——
    # 表面正常、实际内容全没了，是典型的静默丢失。
    # 图表例外：它的 error 语义是"XML 里没数据"，那是图表自己的事，由下面处理。
    if error and kind != "chart":
        kc.kind = KIND_TEXT
        kc.content = (f"[媒体未取出] {kind or '未知类型'}：{part or '(无部件)'}"
                      f" —— {error}")
        kc.retrieval_text = kc.content
        kc.tags = ["媒体未取出"]
        return kc

    if kind == "chart":
        chart = _safe(asset, "chart", None) or {}
        md = chart_to_table(chart)
        natural = chart_to_text(chart)
        kc.kind = KIND_CHART
        kc.alt_text = alt
        if md and natural:
            # 数据来自 chart XML，是**确定性事实**，不需要也不应该送 VLM：
            # 生成式模型看图会引入误差（会"编辑"而非仅转写），而答案本来就在 XML 里。
            kc.content = f"{alt or ''}\n\n{md}".strip()
            kc.table_md = md
            kc.retrieval_text = "；".join(p for p in (anchor, alt, natural) if p)
            kc.tags = ["图表", "来自XML"]
            kc.needs_vlm = False
        else:
            # XML 里真的没数据（图表被裁剪、序列被删、或本身就是张位图）→ 才轮到 VLM
            kc.content = (f"{alt}\n\n[图表数据未从 XML 读出：{error or '无系列数据'}]").strip()
            kc.retrieval_text = "；".join(
                p for p in (anchor, alt, "图表（数据待 VLM 描述）") if p) or "图表"
            kc.tags = ["图表", "待VLM"]
            kc.needs_vlm = True
        return kc

    if kind == "ole":
        prog = _safe(asset, "prog_id")
        desc = PROG_ID_CN.get(prog.lower(), f"嵌入对象（{prog}）" if prog else "嵌入对象")
        inner = (_safe(asset, "ole_text") or "").strip()
        if inner:
            kc.kind = KIND_TABLE if (_safe(asset, "inner_ext")).lower() in _TABULAR_INNER else KIND_TEXT
            kc.content = f"{desc}\n\n{inner}"
            kc.retrieval_text = "；".join(p for p in (anchor, desc, inner[:1500]) if p)
            kc.tags = ["OLE", "已解析原件"]
        else:
            # 拿不到内层正文：至少把"这里有个嵌入对象、原件在哪"记清楚，
            # 而不是让这个元素在索引里凭空消失。
            kc.kind = KIND_TEXT
            kc.content = f"{desc}（原件已导出：{saved or '导出失败'}）"
            kc.retrieval_text = "；".join(p for p in (anchor, desc) if p) or desc
            kc.tags = ["OLE", "仅元信息"]
        kc.needs_vlm = False
        return kc

    if kind in ("image", "other"):
        kc.kind = KIND_DIAGRAM
        kc.content = alt or ""
        kc.alt_text = alt
        # 有作者写的替代文字就用它 —— 那是确定性描述，比 VLM 生成的可信，
        # 也没必要为它花钱；只有"什么说明都没有的图"才值得送 VLM。
        if alt:
            kc.needs_vlm = False
            kc.retrieval_text = "；".join(p for p in (anchor, alt) if p) or alt
            kc.tags = ["图片", "有替代文字"]
        else:
            kc.needs_vlm = True
            kc.retrieval_text = "；".join(
                p for p in (anchor, "图片（无替代文字，待 VLM 描述）") if p) or "图片"
            kc.tags = ["图片", "待VLM"]
        return kc

    # 认不出的类型 / rels 指向缺失部件：显式记录，不静默丢弃
    kc.kind = KIND_TEXT
    kc.content = f"[媒体未取出] {kind or '未知类型'}：{part or '(无部件)'} —— {error or '未知原因'}"
    kc.retrieval_text = kc.content
    kc.tags = ["媒体未取出"]
    return kc
