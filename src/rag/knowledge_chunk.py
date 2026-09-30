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

# 这两个默认值**只在这一处定义**：`from_docling_chunk_all` 里公共字段的取值、
# 以及四个构建器（表 / 图 / 公式 / 纯文本）的签名默认值，都从这里引。
# 为什么在意：下游按 origin 区分「docx 原生图表（有 chart XML 数据源）」与
# 「嵌入位图 / 扫描件」，origin 一旦分叉就会直接改变 needs_vlm 的判定结果；
# parser 同理影响引用回溯的展示口径。同一条默认值各写一份字面量，
# 改一处漏一处就是一次无告警的行为分叉 —— 这个模块已经在防这类事了。
_DEFAULT_PARSER = "docling"
_DEFAULT_ORIGIN = "docling_pdf"

# Docling 图片分类 → 我们的 kind（class 名来自 do_picture_classification）。
#
# ⚠️ 类别名必须与 docling **实际加载的模型**对齐。docling 2.129 默认用的是
# `docling-project/DocumentFigureClassifier-v2.5`（26 类），本表只收录
# "数值有确定性来源"的那几种：命中即 needs_vlm=False，因为 docx 原生图表
# 能从 chart XML 逐值读出（见 from_docx_media），比 VLM 看图猜可靠。
#
# **刻意不收录** flow_chart / geographical_map / topographical_map /
# engineering_drawing / chemistry_structure：它们在 docx/pdf 里几乎都是嵌入位图，
# 没有 XML 数据源。判成 needs_vlm=False 会让图里的信息**永久丢失**（扫描件的数值
# 只存在于像素里），所以一律落到默认分支走 VLM —— 多花一次调用，换不可逆的信息。
#
# 旧表里的 scatter_chart / stacked_bar_chart / heatmap / histogram /
# schematic_diagram / boxplot / dot_plot / area_chart 在 v2.5 中**根本不存在**
# （散点的真实类名是 scatter_plot），属历史残留，已删——留着只会误导后人。
_CHART_CLASSES = {
    "bar_chart": KIND_CHART,
    "line_chart": KIND_CHART,
    "pie_chart": KIND_CHART,
    "scatter_plot": KIND_CHART,
    "box_plot": KIND_CHART,
}

# DocumentFigureClassifier-v2.5 的**全部 26 个类别**（`.lower()` 后的形态）。
#
# 为什么要有这份清单：`tags` 是混装的 list —— 里面既有图片分类名（cls），也有
# 类别名（`图表`）和处理状态标记（`待VLM` / `自身无内容` / `混排块正文`）。
# 读取方想取出"分类名"原本只能靠**排除法**（跳过已知的非分类词），而排除名单
# 一漏就会把状态标记当成类别用：`image_describer.context_for` 曾把 `文本兜底`
# 当成 `predicted_class` 喂给 VLM（cls 为空时触发）。
# 有了正向白名单，"是不是分类名"由成员资格决定，往 tags 里加任何新标记都不会
# 被误读，也不需要在加 tag 时同步任何名单。
_V25_CLASSES = frozenset({
    "logo", "photograph", "icon", "engineering_drawing", "line_chart",
    "bar_chart", "other", "table", "flow_chart", "screenshot_from_computer",
    "signature", "screenshot_from_manual", "geographical_map", "pie_chart",
    "page_thumbnail", "stamp", "music", "calendar", "qr_code", "bar_code",
    "full_page_image", "scatter_plot", "chemistry_structure",
    "topographical_map", "crossword_puzzle", "box_plot",
})

# 元素块（表格/图片各成一块）取"同块正文"时要排除的 label。
#
# 除 picture/table 外**必须排除 caption**：图题/表题在 docling 里是**独立 item**
# （实测 label="caption"，自带 prov、位置紧贴元素），而元素块已经通过
# `item.caption_text(doc)` 把题注带进了 content / retrieval_text。不排除的话
# 同一句题注会在同一个块里出现两次（sibling 一次 + caption 一次）。
#
# 对照：**正文独立块**（from_docling_chunk_all 尾部）刻意**不排除 caption** ——
# 那条线没有"元素"来承载孤儿题注，排掉会让题注失去唯一承载块。
_OWN_TEXT_EXCLUDE = ("picture", "table", "caption", "formula", "equation")

# 正文块排除的结构性元素：与 _OWN_TEXT_EXCLUDE 的差别只在 caption。
# 公式必须排：它已有自己的块，正文块再收一遍就是同一截 LaTeX 占两个召回位。
_BODY_EXCLUDE = ("picture", "table", "formula", "equation")

# 结构性元素：**各自成块**，不进正文块。判据是 label 成员资格，加新类型只需往这里加一项。
_STRUCT_LABELS = ("table", "picture", "formula", "equation")

# 面积占比低于此值的无标注图片按装饰丢弃（Docling 已把页眉页脚归入 furniture，
# 这里只兜底那些混在正文里的分隔线、小图标）
_MIN_AREA_RATIO = 0.03


@dataclass
class KnowledgeChunk:
    """统一检索单元。字段含义见 PLAN_doc_parsing_v3.md §10。

    字段按**读侧用途**分四组（下面的分组标题是阅读辅助）：

      A 身份与定位  谁 + 在哪 —— 回原文核对靠这组
      B 内容双通道  `content`（保真事实）vs `retrieval_text`（召回通道），分工不能混
      C 台账与状态  引用展示 / 灌库日志 / 待办队列
      D 增值产物    表格与图片（批次 5–6），全部可为空

    ⚠️ **物理顺序 = 历史追加顺序**，不是分组顺序：新增字段一律追加到末尾，
    不要为了分组好看而重排（`to_dict()` 走 `asdict`，key 顺序会跟着变；
    按位置构造的旧调用也会错位）。分组标题只是给读者用的。
    """

    # ── A. 身份与定位 ────────────────────────────────────────────────────
    chunk_id: str = ""              # sha1(f"{source}|{anchor}")[:16]，内容寻址：源不变 → id 不变。
                                    # ⚠️ 只能由 make_chunk_id 产出。改各 _build_* 里的 anchor 表达式
                                    #    = 全部既有 id 变化 = 全库重建 + 旧引用回溯失效。
    source: str = ""                # 文件名（不含目录）。id 的稳定性靠这个字面值，不靠文件路径
    page: Optional[int] = None      # **起始页**（1 基），只是给人看的代表值。
                                    # 两个真实用途：① 裁原页截图时"去哪一页裁"；② 引用里显示"第 N 页"。
                                    # ⚠️ xlsx/pptx 下它不是页码而是工作表/幻灯片序号（见 retrieval._where）
    # 完整跨越页码（升序、去重）。`page` 只是它的首元素：给人看的代表值，
    # 这个列表才是"这块内容到底跨了几页"的完整事实。跨页块（正文块、整文档兜底块）
    # 只有它说得清范围，否则引用里显示"第 3 页"而内容到第 5 页，人去核对只看得到一半。
    # ⚠️ 刻意**不进** chunk_id 的 anchor —— 加了会改掉全部既有 id、触发全库重建。
    pages: list = field(default_factory=list)
    kind: str = KIND_TEXT           # text | table | chart | diagram | formula | unprocessable。
                                    # 决定"给不给原页截图"：只有后四种在 CITE_IMAGE_KINDS 里。
                                    # 由各自构建器给出，无块级 kind 分发（别再引入优先级判定）
    section_path: str = ""          # 标题路径，如 "第三章 > 出口 > 主要国家"。
                                    # 出边界时别名成旧字段 section（检索层与前端只认 section）
    bbox: list = field(default_factory=list)          # [[l, t, r, b], ...]，可跨页。
                                    # ⚠️ **左下原点**（PDF 惯例），PIL 裁剪是左上原点，用前必须换算；
                                    #    只有 PDF 有值，docx/其它格式恒空
    # docx 是流式格式，没有页码与 bbox，位置只能这样表达：
    #   para_index  —— 文档流第几段（精确）
    #   anchor_text —— 该元素所在段落文字，为空则回退到上文最近的非空段落（给检索用的上下文）
    para_index: Optional[int] = None
    anchor_text: str = ""

    # ── B. 内容双通道 ────────────────────────────────────────────────────
    content: str = ""               # 保真事实：给 LLM 与引用展示（cite.snippet）看。
                                    # **取不准宁留白**，不许拿"同块别人的文字"来兜底（会张冠李戴）
    retrieval_text: str = ""        # 召回通道：向量化输入，面向检索的自然语言。
                                    # 取不准也要留着 —— **留空 = 这块在检索里彻底不可见**。
                                    # 出边界时写入旧字段 text（检索层只认 text，不认 content）

    # ── C. 台账与状态 ────────────────────────────────────────────────────
    tags: list = field(default_factory=list)
                                    # 混装的 list：图片分类名（bar_chart）+ 类别名（图表）+
                                    # 处理状态标记（待VLM / 自身无内容 / 混排块正文）都往里放。
                                    # ⚠️ 读取侧必须用**正向白名单**（_V25_CLASSES），任何"排除已知
                                    #    非分类词"的黑名单写法都会在新增 tag 时静默失效
    page_total: Optional[int] = None
                                    # 这份文档共几页。⚠️ src/ 里目前**只有写入侧、没有读取侧**
    parser: str = ""                # 实际生效的解析器：docling | pdfminer | docx_zip | python-docx |
                                    # xlsx/pptx/md/txt…（后一组直接取扩展名）。
                                    # "走了降级链路"必须能查，就靠它 + 日志（retrieval._log_load_summary）
                                    # ⚠️ 值与下面的 origin 不同名不同义，别混用
    needs_vlm: bool = False         # 这个块还有一张图没被描述（待办队列的判据）。
                                    # 构造期为 True 的只有图片类块；描述成功由 image_describer._apply
                                    # 置回 False，失败则保持 True → 天然可重试
    file_checksum: str = ""         # 源文件内容哈希。⚠️ 同样只有写入侧，没有读取侧（增量索引用时再接）

    # ── D. 表格 / 图片增值产物（批次 5–6，全部可为空）────────────────────
    table_md: str = ""              # 表格保真表示（Markdown 网格）。表格块的 content 由它拼成；
                                    # 引用里按原样透出（比线性化后的文字更适合人工核对）
    alt_text: str = ""              # 作者手写的替代文字（docx 图片/图表）。
                                    # 也是 VLM 的 caption 首选来源：image_describer.context_for 取它，
                                    # 空则回退到 content
    vlm_desc: Optional[dict] = None # 批次6：VLM 描述**原文**（chart_type/title/key_data/issues/usage/
                                    # model/elapsed/cached 全留）。存全文是为了审计——核 token 账只能从这读
    source_image: str = ""          # 批次6：裁出的插图/媒体原件路径。既是 VLM 的输入，
                                    # 也是引用附件（cite.source_image）
    page_image: str = ""            # 原页截图：扫描件给整页，表格/图/公式给 bbox 区域。
                                    # 三段式消费：cite 里透出给人核对；解析不出内容时给第 1 页整图
    # ── C. 台账与状态（续：位置由历史追加顺序决定）───────────────────────
    origin: str = ""                # 产出通道：docling_pdf | docling_docx | docx_zip | pdfminer | file_loader。
                                    # 与 parser 的分工：parser = "用谁解析"，origin = "从哪条链路出来"
                                    # （真实读取点：docling_loader 按 origin == "docling_docx" 挑图表块）
    parent_id: str = ""             # 父子块预留。⚠️ 实际值由 retrieval.attach_parents 写在**dict 层**
                                    # （c["parent_id"] = "{source}#{lo}-{hi}" + 兄弟字段 parent_text），
                                    # 不写回 dataclass —— 所以这个字段在本对象上恒为空串
    unprocessable_reason: Optional[str] = None
                                    # 非空 = 这是一条"解析失败的显式记录"（kind == unprocessable）。
                                    # 拒绝要显式、不静默：chunk_writer.summarize 按它列出失败文件

    def to_dict(self) -> dict:
        return asdict(self)


def make_chunk_id(source: str, anchor: str) -> str:
    """内容寻址的稳定 id：源不变 → id 不变，便于增量与去重。

    source: 文件名（basename）。改它的字面值等于换一个源，全部 id 都会变。
    anchor: 块在文档内的定位串。各构建器各自拼，**含 pages / boxes / text[:64]**
        （见各 `_build_*`）—— 改动 anchor 表达式 = 全库重建 + 旧引用失效。

    返回 sha1 前 16 位十六进制。注意 sha1 不可逆，"把完整页码/文本藏进 id 再读回来"
    是不可能的；要读就存字段（如 pages）。
    """
    raw = f"{source}|{anchor}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 拼接 / 规范化小工具（只服务 retrieval_text 与 content 的组装）
# ---------------------------------------------------------------------------
def _append_unique(base: str, extra: str, sep: str = " ") -> str:
    """把 extra 追加到 base 末尾；空串、或**已出现过的重复片段**直接跳过。

    base: 已有的文本。为空时直接返回 extra（不加前导分隔符）。
    extra: 待追加的片段。空串 / None / 纯空白时原样返回 base。
    sep: 两者之间的连接符，默认单个空格。只在真的追加时才插入。

    为什么需要：表格块在 md 取不到时会先用 `chunk.text` 兜底，而 chunk.text
    通常已经包含同块的正文，紧接着又把 sibling 拼一遍 —— 同一段文字出现两次。
    不报错、也不影响召回，但白占 token，还会让该段在向量空间里被重复加权。

    判重用 `extra in base` 子串匹配，所以"已包含"比"完全相等"更宽 ——
    代价是 extra 恰为 base 子串时会跳过追加，这是有意的保守选择。
    """
    extra = (extra or "").strip()
    if not extra or extra in base:
        return base
    return f"{base}{sep}{extra}" if base else extra


def _one_line(text: str) -> str:
    """把连续空白（含换行）折叠成单个空格，得到**单段**检索文本。

    表格的 linearize_table 输出是「表题一行 + 每行数据一行」的多行文本，
    再拼上正文就成了「多行 + 尾部空格拼接」的混合形态。检索文本只需一段连续
    文本，折叠空白后下游（按行切分 / 窗口截断 / 前端展示）的行为才是确定的。

    ⚠️ 只用于 retrieval_text：content 必须保留换行，Markdown 网格靠行结构。

    text: 待折叠的原始文本；None 视为空串。返回去首尾空白后的单段文本。
    """
    return re.sub(r"\s+", " ", text or "").strip()


# ---------------------------------------------------------------------------
# 表格
# ---------------------------------------------------------------------------
def _split_md_row(line: str) -> list[str]:
    """拆一行 Markdown 表格（处理首尾竖线与 \\| 转义）。

    line: 表格中的一行，形如 `| 名称 | 数值 |` 或 `名称 | 数值`（首尾竖线可有可无）。

    返回单元格文本列表，每格已 strip、且 `\\|` 已还原成 `|`。
    空串返回 [""]（不是空列表）。
    """
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]


def _is_separator_row(cells: list[str]) -> bool:
    """GFM 表格的分隔行：|---|---| 或 |:--:|--| 这类。

    cells: 已拆分的单元格列表（_split_md_row 的产物）。

    判据：去掉空格后只剩 `-` / `:`，且至少含一个 `-`。空列表或全空格返回 False。
    """
    real = [c for c in cells if c]
    return bool(real) and all(set(c) <= set("-: ") and "-" in c for c in real)


def linearize_table(table_md: str, caption: str = "", max_rows: int = 200) -> str:
    """Markdown 表格 → "列=值；列=值" 的自然语言行。

    table_md: 表格的 Markdown 网格。空串 / 全是空行时返回 ""。
    caption: 表题。非空时会作为首行（`表题：`）拼在结果前面。
    max_rows: 最多输出多少行数据（防止超大表把检索文本撑爆）。默认 200。

    返回多行文本（每行一条记录），**不是单段** —— 需要单段请在调用侧用 _one_line。
    首个数据行被当作表头；若首行本身就是分隔行，则视为无表头，列名退化成"列1/列2"。

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
    """Docling TableItem → Markdown。取不到就返回空串，不抛。

    table_item: docling 的 TableItem（或任何有 export_to_markdown 的对象）。
        没有该方法时直接返回 ""。
    doc: 可选的 DoclingDocument。新版 docling 的 export_to_markdown 需要它；
        传 None 时会退化为不带参数的调用方式（老版 API）。

    先用 `doc=doc` 试、再退到无参调用，两次都失败才返回空串 ——
    这是为兼容 docling 版本差异，不是为了掩盖错误。
    """
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
    """取元素的 label 名（小写字符串），取不到返回空串。

    item: 任意 docling 元素。label 可能是枚举（有 .value）也可能是裸字符串，
        两种都处理；None 返回 ""。

    用 label 字符串而非 isinstance 判类型，是为了让本模块**不依赖 docling**
    （可离线单测，也让 `_resolve_items` 还原后的基类对象仍可判类型）。
    """
    lab = getattr(item, "label", None)
    if lab is None:
        return ""
    return str(getattr(lab, "value", lab)).lower()


# 给别的模块用（docling_loader 的兜底路径要按同一套 label 判定走）
label_of = _label_of


def item_text(item: Any, doc: Any = None) -> str:
    """取单个 Docling item 的文本表示，取不到返回空串。

    item: docling 元素。优先读 `.text`（正文实际存在这里），没有才回退表格导出。
    doc: 透传给 table_to_markdown，仅表格需要。

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
    """取图片分类名（docling 的 do_picture_classification 产出）。

    item: docling 的 PictureItem。为 None 时返回 ""。
        全程 getattr、不 import docling，保持本模块可单测。

    ⚠️ 两个来源、两种字段名，都要看：

      1. **新路径**（docling 2.x 主路径）：`item.meta.classification.predictions[].class_name`
      2. **旧路径**（`_keep_deprecated_annotations` 默认 True 时同时写）：
         `item.annotations[].predicted_classes[].class_name`

    旧实现读的是 `ann.predicted_class`（**单数**），而 `PictureClassificationData`
    上只有 `predicted_classes`（**复数**）—— 实测恒返回空串，于是整条分类链路
    静默失效：`_CHART_CLASSES` 永不命中、`kind` 永远是 diagram、tags 里没有分类名。
    开关是好的，读的人拿错了字段，而且不报错。

    返回小写类名；两条路径都取不到时返回 ""。
    """
    meta = getattr(item, "meta", None)
    classification = getattr(meta, "classification", None) if meta is not None else None
    if classification is not None:
        try:
            pred = classification.get_main_prediction()   # 官方 API：取置信度最高的一条
        except Exception:  # noqa: BLE001  旧版没有此方法 → 落旧路径
            pred = None
        name = getattr(pred, "class_name", None)
        if name:
            return str(name).lower()
    for ann in getattr(item, "annotations", None) or []:
        for pred in getattr(ann, "predicted_classes", None) or []:
            name = getattr(pred, "class_name", None)
            if name:
                return str(name).lower()
    return ""


def _image_path_of(item: Any, image_paths: Optional[dict]) -> str:
    """取该图片元素落盘后的本地路径（见 docling_loader._save_pictures）。

    item: docling 的 PictureItem。用它的 self_ref 去查表；item 为 None 时返回 ""。
    image_paths: {self_ref: 落盘路径} 映射表。None 或空表返回 ""。

    找不到就返回空串 —— 上游"这张图没落盘"与"这张图不该落盘"是两件事，
    由下游（批次6 的 VLM 步骤）显式记成"无图可取"，绝不能在这里假装有图。
    """
    if not image_paths or item is None:
        return ""
    ref = getattr(item, "self_ref", None)
    if isinstance(ref, str) and ref in image_paths:
        return image_paths[ref] or ""
    return ""


def _struct_items(items: list[Any]) -> tuple[list, list, list]:
    """把块内元素按 label 分成 (表格, 图片, 公式) 三组 —— 分块判据的唯一来源。

    items: 块内元素列表（**必须已过 _resolve_items**，否则基类对象没有 label
        之外的子类信息，公式会漏判）。

    返回三个列表，顺序与输入一致。三组全空 = 该块是纯文本块。

    ⚠️ 这取代了旧的"数量判据"（`len(tables) <= 1 and len(pictures) <= 1`）。
    旧判据的后果是**同一个元素因邻居数量不同而得到不同的 kind / 归属**：
    公式单独在一块时 `kind=formula`，与 ≥2 张表同块时只是一截混在正文里的 LaTeX。
    新判据改为语义判定 —— **只要块里有结构性元素就拆**，与数量无关。
    """
    tables = [i for i in items if _label_of(i) == "table"]
    pictures = [i for i in items if _label_of(i) == "picture"]
    formulas = [i for i in items if _label_of(i) in ("formula", "equation")]
    return tables, pictures, formulas


# 这里曾有一个 `_kind_for_items(items)`：按优先级 table > picture > formula > text
# 把整个块判成单一 kind。它已随"一个 chunk 一个语义单元"的合流被删除 ——
# 含结构元素的块现在由 `from_docling_chunk_all` 用 `_struct_items` 分组后**逐元素**成块，
# kind 由各自的构建器（_build_table_chunk / _build_picture_chunk / _build_formula_chunk）
# 直接给出，不再需要"多选一"。而"多选一"恰恰是"1 表 + 1 图 → 图被表格吞掉"那个陷阱的
# 载体：一旦有人新增调用点传进混排块，图就会静默消失。删掉它是为了让它无法回归。
#
# 唯一还需要"判 kind"的地方是 `_build_text_chunk` —— 那是纯文本出口，kind 恒为
# KIND_TEXT，也不构成多选一。


def _sibling_texts(items: list[Any], exclude=("picture", "table")) -> str:
    """取同块内**除图片/表格外**的正文文本，用 "；" 连接。

    items: 块内元素列表（已过 _resolve_items）。
    exclude: 要跳过的 label 元组。默认排图片与表格；
        元素块传 `_OWN_TEXT_EXCLUDE`（额外排 caption 与公式），
        正文块传 `_BODY_EXCLUDE`（排结构性元素但**不排 caption**）。

    返回拼好的正文；没有可用文本时返回空串。

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

    item: docling 的 PictureItem。为 None 时按"无分类的位图"处理
        （返回 (KIND_DIAGRAM, True)）。
    area_ratio: 图片面积占整页的比例（0~1）。传 None 表示"不判装饰" ——
        此时任何无分类的图都会被判为需要 VLM。低于 _MIN_AREA_RATIO（0.03）
        且无分类的图按装饰丢弃（needs_vlm=False，且 kind 记成 diagram）。

    判定顺序与 PLAN_doc_parsing_v3.md §6.4 一致：有 caption 或有分类 → 有价值；
    面积过小且无标注 → 装饰，丢弃。

    ⚠️ 注意本函数**不读 caption**：命中 _CHART_CLASSES 的分类才会让 needs_vlm=False。
    调用方 `_build_picture_chunk` 会在"没有 caption"时把 needs_vlm 再置回 True。
    """
    cls = _predicted_class(item)
    if cls in _CHART_CLASSES:
        return _CHART_CLASSES[cls], False
    if area_ratio is not None and area_ratio < _MIN_AREA_RATIO:
        return KIND_DIAGRAM, False                      # 装饰
    return KIND_DIAGRAM, True                           # 位图，本轮只记元信息


def _collect_prov(items: list[Any]) -> tuple[list[int], list[list[float]]]:
    """收集页码与 bbox，供引用溯源与"渲染该页截图"使用。

    items: 元素列表（**传单个元素的列表可只取该元素自己的位置** ——
        拆分路径就是这么用的，见 from_docling_chunk_all 里的 `_collect_prov([t])`）。

    返回 (pages, boxes)：pages 升序去重；boxes 是 [[l, t, r, b], ...]，
    顺序与遍历顺序一致、**不去重**。坐标是**左下原点**（PDF 惯例），
    与 PIL 的左上原点相反 —— 裁剪前必须换算（见 docling_loader._crop_region）。

    没有 prov 的元素被静默跳过（返回空列表），不抛异常：位置缺失是可接受的降级，
    bbox 为空时引用仍能显示来源与页码（或退化成只有文件名）。
    """
    pages: list[int] = []
    boxes: list[list[float]] = []
    for it in items:
        for prov in getattr(it, "prov", None) or []:    #prov是出处，有页码，bbox（四个参数标记位置），还有一个字符区间
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
#
# 实测（docling 2.129 + HybridChunker）：两种形态并存，按块是否超 token 预算分：
#   未超预算 → JSON 往返（DocChunk(**export_json_dict())）→ 退化成 DocItem 基类
#   超预算   → meta 对象被直接复用      → 保留 TextItem / TableItem / PictureItem
# 默认 512 token 下多数块走前者，所以按引用处理是常态而非例外。
# ---------------------------------------------------------------------------
def _ref_index(doc: Any) -> dict:
    """`self_ref` → 完整元素。用于把 chunk 里的引用还原成带正文的对象。

    doc: DoclingDocument。None 时返回空 dict（调用方据此退化为"不还原"）。

    返回 {self_ref: 元素对象} 的地址簿。遍历七个集合，其中 groups 与
    key_value_items / form_items / field_items 对本项目的 PDF 报表恒空，
    留着是为了代码形态统一、不因格式差异改变索引范围。

    同名 ref 只保留**第一个**（setdefault）—— 理论上一份文档内 ref 唯一，
    真出现重复时保留首次出现比后者覆盖前者更可预期。
    """
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

    items: chunk.meta.doc_items，可能是完整元素、也可能是只有 self_ref 的基类引用，
        两种混装也允许（按块是否被 token 切分而定）。
    doc: DoclingDocument，用来按 self_ref 回捞实体。传 None 时原样返回输入列表
        （不做任何还原，退化成旧行为）。

    ⚠️ 为什么必须有这一步 —— 实测结论（docling 2.129 + HybridChunker）：

    `chunk.meta.doc_items` 里的元素**有两种形态**，取决于该块有没有超 token 预算：

      - **未超预算**（`RAG_DOC_CHUNK_TOKENS` 默认 512，多数块属此列）：
        `HybridChunker._split_using_plain_text` 走
        `DocChunk(**doc_chunk.export_json_dict())` —— 一次 JSON 往返。
        而 `DocMeta.doc_items` 的类型注解是 **`list[DocItem]`（基类）**，
        `model_dump(mode="json")` 按基类序列化，只输出
        `self_ref / label / prov / parent / children / content_layer`，
        **子类字段（`TextItem.text`、`TableItem.data`、`PictureItem.annotations`）
        全部被丢弃**；重建后类型退化成 `DocItem` 基类。
        ——所谓"轻量引用"就是这么来的：它不是 docling 刻意设计的结构，
        而是"基类注解 + JSON 往返"的副作用。
      - **超预算被 token 切分**：`DocChunk(text=s, meta=doc_chunk.meta)` **直接复用
        原 meta 对象**，doc_items 保持为完整实体（实测 40 行大表切成 41 块，
        40 块的 doc_items 是原 TableItem 实体，只有 1 块是 DocItem 引用）。

    不还原的后果全是静默的：
      - `table_to_markdown()` 在引用上取不到方法 → 返回空串 → `table_md` 为空，
        `linearize_table()`（本项目核心设计）拿空串空转；
      - `_predicted_class()` 读不到 `meta.classification` / `annotations`
        → 图表分类失效，`kind` 永远是 diagram、`needs_vlm` 退回看有无 caption；
      - `_sibling_texts()` 取不到 `.text` → 混排块里的正文丢掉检索入口
        （实测：还原前返回 `''`，还原后返回整句正文）。

    为什么一直没暴露：docling 自己的 `chunk.text` 恰好把表格序列化成
    "East, Revenue = 1200. West, Revenue = 980" 这种准自然语言，检索看着还行，
    "内容不为空"就把"链路在走回退分支"掩盖了。

    `prov` 在引用上是有的（所以页码/bbox 一直是对的），因此这里只替换、不合并。
    已经是完整元素的（有 `.text` 或 `export_to_markdown`）原样保留，
    解析不到引用时也原样保留 —— 宁可退化成旧行为，也不能把元素弄丢。
    """
    if doc is None:
        return list(items)

    idx = None
#       idx 是什么：一份"门牌号 → 元素"的地址簿，比如idx == {
#     "#/texts/0":    <TextItem  对象>,
#     "#/texts/1":    <TextItem  对象>,
#     "#/tables/0":   <TableItem 对象>,   # ← 上面例子要查的就是它
#     "#/pictures/0": <PictureItem 对象>,
#     ...
# }
    out = []
    for it in items:
        if hasattr(it, "text") or hasattr(it, "export_to_markdown"):
            out.append(it)
            continue
        ref = getattr(it, "self_ref", None)     #self_ref 是门牌号类似于#/texts/5
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

    pieces: 纯文本块列表（pdf_parser.pdf_to_chunks 的产物）。None / 空列表返回 []；
        其中的空串与纯空白块被静默跳过。
    source: 文件名（basename），写进每个块的 source，也参与 chunk_id 计算。
    page_total: 该 PDF 的总页数。调用点（retrieval._parse_pdf）**没传**，
        所以实际恒为 None —— 降级路径拿不到页数，引用里显示不出页数。

    产出与 Docling 路径同构，下游不必区分来源；origin 字段保留线索，
    便于排查"这份文件其实走的是降级链路"。

    每条块的 anchor 是 `pdfminer|{p[:64]}`（与 Docling 路径的
    `{id_prefix}{pages}|{boxes}|{text[:64]}` 不同源），且 chunk_size / overlap_size
    不参与 anchor —— 改这两个参数后若两块前 64 字相同，会得到同一个 chunk_id。
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

    source: 文件名（basename），写进 source 与 retrieval_text。
    reason: 失败原因（如"Docling 未产出任何内容"）。**参与 chunk_id 计算**，
        所以同一文件因不同原因失败会得到两条不同的记录。
    parser: 实际尝试过的解析器名（docling 等）。留空表示尚未确定用谁。
    page_total: 该文档总页数，可为 None。

    返回单条 **kind=KIND_UNPROCESSABLE** 的块：content 恒为空，tags=["未解析"]。

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


# ---------------------------------------------------------------------------
# 元素级构建：一个元素 → 一个 chunk
#
# 为什么要抽成独立函数：元素块的 content / retrieval_text / kind 各有规则，三条出路
# （表 / 图 / 公式）若各写一份迟早长歪。合流后它们是**唯一的构造入口**：
# 只由 from_docling_chunk_all 在一处按元素逐个调用 ——
# 于是"同一个元素因入口不同而产出不同块"在结构上不再可能发生。
# ---------------------------------------------------------------------------
def _mark_broad_fallback(kc: KnowledgeChunk, *, own: bool, text: str,
                         id_prefix: str, label: str = "") -> None:
    """元素自身取不到内容时，把这件事变成可观测的（tag + 可选 warning）。

    kc: 刚构造好的元素块，**就地改写 tags**（追加 `自身无内容`）。
    own: 元素**自身的内容源**是否取到了。表格看 md/caption/正文、图片看 caption/正文、
        公式看 LaTeX。传 True 时本函数直接返回（什么都不做）。
    text: 整块 text。**必须照传**（即使不用于内容）—— 它既用来判定"要不要标记"，
        也是 chunk_id 的原料（`text[:64]`）。空串时本函数直接返回（无从标记）。
    id_prefix: 元素序号前缀（如 "t1|"）。**只有非空时才发 warning**：
        统一路径恒非空，需要留白/兜底能被发现；逐 item 兜底路径恒空，天然不误报。
    label: 元素类型名（"table" / "picture" / "formula"），仅用于 warning 文案。

    `own=False` 表示元素自身的内容源（表格的 md/caption/正文、图片的 caption/正文、
    公式的 LaTeX）一个都没取到。**吃不吃整块 text 由调用方的 `allow_broad_fallback`
    决定**，判据是 `sole and not body`（见 from_docling_chunk_all）：

      - 允许兜底（块里只有这一个结构元素、也没有别的文本元素）：text 就是本元素的
        序列化结果，调用方照吃它保命；
      - 不允许兜底：留白待补。`text` 只用来判定"要不要标记"和给 warning 提供反查线索 ——
        它是 chunk_id 的原料（`text[:64]`），必须照传，但不再是内容来源。

    两个选项里取折中：静默当成元素自己的内容不好（错归属）；因为怕错就丢掉更糟
    （信息不可逆丢失）。所以保留兜底 / 留白待补，并留一个 tag 让它可筛、留一条 warning
    让它可见。

    tag 用 `自身无内容` 而不是 `文本兜底`："兜底"只描述了 content 的一种可能来源；
    这个 tag 描述的是**元素自身的状态**（没取到内容，等 VLM 补）。
    """
    if own or not text:
        return
    kc.tags = list(kc.tags) + ["自身无内容"]
    if id_prefix:
        logger.warning(
            "元素自身内容为空（%s，tag=自身无内容），整块文本前 20 字=%r：chunk_id=%s",
            label or "?", text[:20], kc.chunk_id)


def _build_table_chunk(item: Any, doc: Any, *, items: list, pages: list, boxes: list,
                       section_path: str, source: str, page_total: Optional[int] = None,
                       parser: str = _DEFAULT_PARSER, origin: str = _DEFAULT_ORIGIN,
                       file_checksum: str = "", text: str = "",
                       id_prefix: str = "", with_sibling: bool = True,
                       allow_broad_fallback: bool = True) -> KnowledgeChunk:
    """单个 TableItem → KnowledgeChunk（表格分支与多元素拆分共用）。

    item: docling TableItem。为 None 时仍产块（md 与 caption 均为空），
        走兜底或留白 —— 宁可产一个空块也不让内容无声消失。
    doc: DoclingDocument，传给 export_to_markdown 与 caption_text。
    items: **块内全部元素**（不是单个表）。用于 `_sibling_texts` 取同块正文。
    pages: 该元素的页码列表（拆分路径传 `_collect_prov([item])[0]`，即元素自己的）。
    boxes: 该元素的 bbox 列表，**左下原点**。
    section_path: 标题路径，末尾会以 `（章节：…）` 追加进 retrieval_text。
    source: 文件名，写进块并参与 chunk_id。
    page_total: 文档总页数，可为 None。
    parser: 解析器名（docling / pdfminer）。
    origin: 产出通道（docling_pdf / docling_docx）。
    file_checksum: 源文件哈希，供增量索引用。
    text: **整块** chunk.text。两个用途：chunk_id 的 anchor 原料（`text[:64]`）
        与 content 的兜底（受 allow_broad_fallback 管）。**必须照传**。
    id_prefix: 元素序号前缀（多表格路径传 `"t1|"`，单表格路径传空串）。

        它**不是**区分同块多元素的唯一手段：anchor 里的 boxes 取元素各自的 prov，
        同一块两个元素的 bbox 本来就不同。真实价值有两条：
          ① 兜底——元素 prov 缺失（boxes 为空）时两个元素会算出同一个 id；
          ② 顺序可读——t0/t1/p0 让人一眼看出拆分次序。

        ⚠️ **订正**：原注释此处曾写"而 chunk_writer 按 id 去重，刚拆出来的块会被
        静默吃掉一个"。2026-09-26 复核全仓库，**没有任何按 chunk_id 去重的代码** ——
        `to_legacy` / `to_legacy_many` 只做字段映射，`load_chunks` 是 `chunks +=`，
        chunk_id 的出现点只有构造、测试断言、cite dict 与注释。撞 id 的真实后果是
        **两条并存的块**，不是静默吞并。理由①保留，但描述改为"便于定位，不影响正确性"。

        留着它，是因为"boxes 必然不同"本身就属于不该被依赖的那类隐含假设。
    with_sibling: 是否把同块正文并入本块的 content / retrieval_text。
        拆分路径传 False —— 那里同块正文会**单独成一块**，再往每个表块里附一份
        就是 N 份重复（检索时同一段文字会挤占多个召回位）。
    allow_broad_fallback: **只管 content，不管 chunk_id**。拆分路径传 False ——
        那时 `text` 是**整块**文本（含同块其它元素的内容），吃它等于张冠李戴。
        注意 `text` 参数本身**照传**：它是 anchor 的原料（`text[:64]`），传空会改掉
        拆分路径所有块的 id、触发全库重建 + 引用失效。
    """
    md = table_to_markdown(item, doc) if item is not None else ""
    caption = ""        #docx 走的是 markup 管线 —— 读 XML 结构，从头到尾没有"版面"这个东西，也就没有关联的依据
    if item is not None:
        try:
            caption = item.caption_text(doc) or ""
            #caption_text(doc) 是表题（如"表 3-1 出口数据"）；
            # docx没有caption
        except Exception:
            caption = ""

    #取同块内**除图片/表格/题注外**的正文文本，用 "；" 连接。也就是同一个docchunk
    # 题注必须排除：表题已由上面的 caption_text 带进来，不排就是同句出现两次。
    sibling = _sibling_texts(items, exclude=_OWN_TEXT_EXCLUDE) if with_sibling else ""


    kc = KnowledgeChunk(
        source=source, page=pages[0] if pages else None, kind=KIND_TABLE,
        pages=list(pages),
        section_path=section_path, bbox=boxes, page_total=page_total,
        parser=parser, origin=origin, file_checksum=file_checksum,
        chunk_id=make_chunk_id(source, f"{id_prefix}{pages}|{boxes}|{(text or '')[:64]}"),
    )
    kc.table_md = md
    # content 也带上表题：图片分支的 content 是 caption + 正文，表格分支原来只有
    # 正文 + 网格，LLM 拿到手不知道"这张表叫什么"。表题里常含关键限定词
    # （如"表3-1 出口数据"），按阅读习惯排在正文之后、网格之前。
    own = "\n".join(p for p in (sibling, caption, md) if p)
    kc.content = own or (text if allow_broad_fallback else "")
    # retrieval_text 的 `or text` **刻意保留**（不挂 allow_broad_fallback）：
    # 它只喂检索（chunk_writer 写进旧 text 字段），是"这张表还能不能被搜到"的
    # 最后一道保险；留空 = 这条块在检索里彻底不可见，比带上可能不精准的文本更糟。
    # 它的触发面也很窄：md 与 caption 同时为空（md 要渲染抛异常才空）。
    kc.retrieval_text = linearize_table(md, caption) or text
    if sibling:
        # 用 _append_unique 而不是裸拼接：md 取不到时上面那行已用 text 兜底，
        # 而 text 通常已含同块正文，再拼一次就是同一段文字出现两遍。
        kc.retrieval_text = _append_unique(kc.retrieval_text, sibling)
    # 章节路径**追加在末尾**：表格数值是强信号，必须留在最前面（嵌入对开头更敏感），
    # 但"这张表属于哪一节"对"第三章的出口数据是多少"这类问题是必需的召回线索。
    # 其它分支（text/formula）都把 section_path 写进了检索文本，表格分支漏了 —— 补齐口径。
    if section_path:
        kc.retrieval_text = f"{kc.retrieval_text}（章节：{section_path}）"
    # 收尾统一成单段：上面拼出来的是「多行（linearize 逐行）+ 尾部空格拼接」的
    # 混合形态，折叠空白后才是一段确定的连续文本。
    kc.retrieval_text = _one_line(kc.retrieval_text)
    kc.tags = ["表格"]
    _mark_broad_fallback(kc, own=bool(own), text=text, id_prefix=id_prefix,
                         label="table")
    return kc


def _build_picture_chunk(item: Any, doc: Any, *, items: list, pages: list, boxes: list,
                         section_path: str, source: str,
                         image_paths: Optional[dict] = None,
                         page_total: Optional[int] = None, parser: str = _DEFAULT_PARSER,
                         origin: str = _DEFAULT_ORIGIN, file_checksum: str = "",
                         text: str = "", id_prefix: str = "",
                         with_sibling: bool = True,
                         allow_broad_fallback: bool = True) -> KnowledgeChunk:
    """单个 PictureItem → KnowledgeChunk（图片分支与多元素拆分共用）。

    item: docling PictureItem。为 None（块里其实没有图片元素）也照样产块，
        并强制 `needs_vlm=True` —— 宁可多送一次 VLM，也不能让一块内容无声消失。
    doc: DoclingDocument，用于取图题（caption_text）。
    items: 块内全部元素，用于 `_sibling_texts` 取图文混排时的那段正文。
    pages: 该元素的页码列表（拆分路径传元素自己的）。
    boxes: 该元素的 bbox 列表，左下原点。
    section_path: 标题路径，进 retrieval_text。
    source: 文件名，写进块并参与 chunk_id。
    image_paths: {self_ref: 落盘路径}，用于填 `source_image`（批次6 的 VLM 输入）。
        None 时该字段为空串 → 下游会显式记为"无图可取"。
    page_total: 文档总页数，可为 None。
    parser: 解析器名。
    origin: 产出通道。
    file_checksum: 源文件哈希。
    text: 整块 chunk.text。既作 anchor 原料（`text[:64]`），也受
        allow_broad_fallback 约束作 content 兜底。**必须照传**。
    id_prefix: 元素序号前缀（拆分路径传 `"p0|"`）。理由与 _build_table_chunk 相同。
    with_sibling: 是否把同块正文并入本块。拆分路径传 False（正文会单独成块）。
    allow_broad_fallback: 同 `_build_table_chunk` —— 只管 content，拆分路径传 False。
        图片**没有** md 这类"自身可渲染内容"，唯一的内容源就是 caption，所以拆分路径下
        "无图题"必然落到 content 为空 + `自身无内容` tag（`needs_vlm=True` 会在批次6 补）。
        注意 `text` 照传：chunk_id 的 anchor 含 `text[:64]`。
    """
    caption = ""
    if item is not None:
        try:
            caption = item.caption_text(doc) or ""
        except Exception:
            caption = ""
    cls = _predicted_class(item) if item is not None else ""
    kind, needs_vlm = picture_kind(item) if item is not None else (KIND_DIAGRAM, True)
    # 题注排除的理由同表格分支：图题是独立 item，会同时被 caption_text 带进来。
    sibling = (_sibling_texts(items, exclude=_OWN_TEXT_EXCLUDE)
               if with_sibling else "")

    kc = KnowledgeChunk(
        source=source, page=pages[0] if pages else None, kind=kind,
        section_path=section_path, bbox=boxes, page_total=page_total,
        parser=parser, origin=origin, file_checksum=file_checksum,
        pages=list(pages), needs_vlm=needs_vlm,
        chunk_id=make_chunk_id(source, f"{id_prefix}{pages}|{boxes}|{(text or '')[:64]}"),
    )
    kc.source_image = _image_path_of(item, image_paths)
    own = bool(caption or sibling)
    if sibling:
        # 图文混排：块里的正文才是可检索的主体，不能因为"含图片"就丢掉
        kc.content = "\n".join(p for p in (caption, sibling) if p)
    else:
        kc.content = caption or (text if allow_broad_fallback else "")
    # 描述留空：批次6 由 VLM 补；本轮先用 caption + 分类 + 章节路径保证可召回
    parts = [p for p in (section_path, caption, cls.replace("_", " "), sibling) if p]
    kc.retrieval_text = " ".join(parts) or "图表"
    kc.tags = [t for t in (cls, "图表") if t]
    if not caption:
        kc.needs_vlm = True
    _mark_broad_fallback(kc, own=own, text=text, id_prefix=id_prefix, label="picture")
    return kc


def _build_formula_chunk(item: Any, doc: Any, *, items: list, pages: list, boxes: list,
                         section_path: str, source: str,
                         page_total: Optional[int] = None, parser: str = _DEFAULT_PARSER,
                         origin: str = _DEFAULT_ORIGIN, file_checksum: str = "",
                         text: str = "", id_prefix: str = "",
                         allow_broad_fallback: bool = True) -> KnowledgeChunk:
    """单个 FormulaItem → KnowledgeChunk（kind=formula）。

    item: docling 的公式元素（label 为 formula / equation）。为 None 时 latex 为空，
        走兜底或留白。自身内容取 `.text`（LaTeX）。
    doc: DoclingDocument。本函数**不用它**（公式没有 caption_text），
        留着是为了与另两个构建器签名一致。
    items: 块内全部元素。本函数**不用它**（公式不并入同类正文），同为签名一致。
    pages: 该元素的页码列表（拆分路径传元素自己的）。
    boxes: 该元素的 bbox 列表，左下原点。
    section_path: 标题路径，以 `路径：LaTeX` 形式进 retrieval_text。
    source: 文件名，写进块并参与 chunk_id。
    page_total: 文档总页数，可为 None。
    parser: 解析器名。
    origin: 产出通道。
    file_checksum: 源文件哈希。
    text: 整块 chunk.text。anchor 原料（`text[:64]`）+ 受 allow_broad_fallback
        约束的 content 兜底。**必须照传**。
    id_prefix: 元素序号前缀（拆分路径传 `"f0|"`）。理由同 _build_table_chunk。
    allow_broad_fallback: 同其它两个构建器 —— 原注释之外的具体含义见上。
        整块 `text` 确实只含这一个元素时才允许兜底（取不到 LaTeX 时保命）。

    为什么公式要单独成块、不能留在正文块里：公式的"自身内容"就是它的 LaTeX，
    和正文混在一个块里有两个后果 ——
      ① 语义丢失：块 kind 只能记成 text，公式再也筛不出来（原来会走
         `CITE_IMAGE_KINDS` 拿原页截图，也会是将来 VLM 批次的候选）；
      ② 召回被稀释：一截 LaTeX 混在整段正文里，"这个公式是什么"与"这段正文讲什么"
         共用同一个向量。

    content 取**元素自身**的 `.text`（LaTeX）—— 与表格/图片统一：元素块的 content
    只含自己的内容。

    公式不做 `contextualize`：LaTeX 离自然语言问题很远，让标题路径进 retrieval_text
    即可；真正的文字化留给批次6。
    """
    latex = ""
    if item is not None:
        t = getattr(item, "text", None)
        if isinstance(t, str):
            latex = t.strip()

    kc = KnowledgeChunk(
        source=source, page=pages[0] if pages else None, kind=KIND_FORMULA,
        pages=list(pages), section_path=section_path, bbox=boxes, page_total=page_total,
        parser=parser, origin=origin, file_checksum=file_checksum,
        chunk_id=make_chunk_id(source, f"{id_prefix}{pages}|{boxes}|{(text or '')[:64]}"),
    )
    kc.content = latex or (text if allow_broad_fallback else "")
    kc.retrieval_text = f"{section_path}：{latex}" if section_path else latex
    kc.tags = ["公式"]
    _mark_broad_fallback(kc, own=bool(latex), text=text, id_prefix=id_prefix,
                         label="formula")
    return kc


def _build_text_chunk(chunk: Any, *, pages: list, boxes: list, section_path: str,
                      text: str, source: str = "", chunker: Any = None,
                      page_total: Optional[int] = None,
                      parser: str = _DEFAULT_PARSER, origin: str = _DEFAULT_ORIGIN,
                      file_checksum: str = "") -> KnowledgeChunk:
    """纯文本块构造器 —— 块内没有表 / 图 / 公式时唯一产出的块（kind=KIND_TEXT）。
    anchor 为 `f"{pages}|{boxes}|{text[:64]}"`

    chunk: 原始 DocChunk。**只用于传给 chunker.contextualize**，为 None 时会跳过
        那一步（retrieval_text 退化为 `section_path：text`）。
    pages: 页码列表，取首元素作 page。
    boxes: bbox 列表（左下原点）。
    section_path: 标题路径。无 chunker 时作为 retrieval_text 的前缀。
    text: 本块正文。**同时是 content 与 anchor 原料**（`text[:64]`）——
        这是唯一"整块 text 就是本块全部内容"的分支，所以可以放心直接当 content。
    source: 文件名。留空时块没有 source（引用里会显示为 "?"）。
    chunker: 可选 chunker，用于 `contextualize(chunk)` 生成 标题路径 + 正文 的
        检索文本（docling 官方接口，内部会去取 chunk.meta 拼富化文本）。
        传 None 或调用异常时退化为手工拼 `section_path：text`。
    page_total: 文档总页数，可为 None。
    parser: 解析器名。
    origin: 产出通道。
    file_checksum: 源文件哈希。
    """
    kc = KnowledgeChunk(
        source=source, page=pages[0] if pages else None, kind=KIND_TEXT,
        section_path=section_path, bbox=boxes, page_total=page_total,
        parser=parser, origin=origin, file_checksum=file_checksum,
        pages=list(pages),
        chunk_id=make_chunk_id(source, f"{pages}|{boxes}|{text[:64]}"),
    )
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
# 多元素拆分：块里有**多个**表格或图片时，按元素 1:1 成块
# ---------------------------------------------------------------------------
def from_docling_chunk_all(chunk: Any, doc: Any, **kwargs) -> list[KnowledgeChunk]:
    """DocChunk → list[KnowledgeChunk]：**唯一分块器** —— 结构性元素各成一块，正文另成一块。

    chunk: docling 的 DocChunk（或 `_ItemAsChunk` 这种鸭子类型替身）。
        读它的 `.text`（整块文本）与 `.meta.doc_items` / `.meta.headings`。
    doc: DoclingDocument，用于 `_resolve_items` 回捞实体。
    **kwargs: 全部可选，键与含义如下（缺省值见函数体内的 common）：
        source        文件名，写进每个块并参与 chunk_id。
        page_total    文档总页数，可为 None。
        parser        解析器名，默认 _DEFAULT_PARSER。
        origin        产出通道，默认 _DEFAULT_ORIGIN。
        file_checksum 源文件哈希。
        chunker       仅传给**纯文本出口**，用于 contextualize（元素块不走它）。
                      兜底路径（`_ItemAsChunk`）**绝不能传** —— 那里没有真 DocChunk。
        image_paths   {self_ref: 落盘路径}，仅图片块消费。

    返回块列表：每个表格 / 图片 / 公式各一块，块内正文（若有）再一块；
    完全没有结构性元素时只返回一个纯文本块。

    ⚠️ 为什么需要这一层：原来的表格/图片分支都用 `next(...)` 取第一个匹配元素 ——
    这等于假设"一个块里最多一张表、最多一张图"。而**这个假设从来没有任何代码
    保证**：块边界由 docling 决定（token 预算、文档有没有 section 结构、库版本），
    项目无法通过"规避用法"消除它。

    假设一旦破裂，两个方向的后果**不对等**：
      - 表格：第二张表的 md 不生成，而 content 的 `... or text` 因 md 非空
        短路掉了 `chunk.text` 兜底 —— 内容 / 检索文本 / 磁盘产物三处全无，**不可挽回**；
      - 图片：第二张图的 source_image 挂不上，但 _save_pictures 已全量落盘
        —— 是"有据可查的孤儿"，可反查补回。

    所以这里把"1 块 = 1 个代表元素"换成"**每个元素各成一块**"：
    每张表一块（KIND_TABLE）、每张图一块（KIND_DIAGRAM / KIND_CHART）、
    块内正文（若有）另成一块（KIND_TEXT）—— 保持"一个 chunk 一个语义单元"。

    正文为什么不附到每个元素块上：N 个元素就会复制 N 份，检索时同一段文字挤占
    多个召回位（项目里已有 `_append_unique`，说明重复是刻意在防的）。所以拆开时
    正文独立成块，元素块只带自己的表题 / 图题 + 章节路径。

    同理，**整块 `chunk.text` 也不作元素块的内容兜底**（`allow_broad_fallback=False`）：
    它是全块所有元素序列化结果的拼接，含同块别人的题注和正文。元素块吃它，内容就与
    元素自己无关 —— 而它的每一部分**都已经有承载块**（表 → 表格块 / 图 → 图片块 /
    其余 → 正文块），所以禁掉兜底不会造成任何不可逆的内容丢失，只把"张冠李戴"
    换成"诚实留白 + `自身无内容` tag + needs_vlm=True 等批次6 补"。

    公式同样单独成块（`_build_formula_chunk`，kind=formula），并从正文块里**排除**。
    混在正文里有两个后果：① 块 kind 只能记成 text，公式再也筛不出来（原本可走
    `CITE_IMAGE_KINDS` 拿原页截图，也是将来 VLM 批次的候选）；② 一截 LaTeX 与
    整段正文共用同一个向量，"这个公式是什么"和"这段正文讲什么"互相稀释。

    **块内没有结构性元素时，走 `_build_text_chunk`（纯文本块）** —— 唯一的例外。
    判据是"有没有"而不是"有几个"：旧的数量判据（`len(tables) <= 1 and len(pictures) <= 1`）
    让同一元素因邻居数量不同而得到不同待遇 —— 公式单独在一块时 kind=formula、
    与 ≥2 张表同块时只是正文串里的一截 LaTeX。现在只要有表 / 图 / 公式就一律按元素拆开。

    docchunk：
    doctext:每个元素的序列化结果,也就是图片和表格和文字的都拼在一起了
    docmeta:
    doc_items	list[DocItem]，必填、min_length=1	分组结构性元素（表/图/公式）、取 caption、算页码/bbox
    headings	list[str] | None	→ section_path
    origin	DocumentOrigin | None	项目暂未消费
    captions	list[str] | None	已标 deprecated，实测恒 None
    schema_name / version	字面量 / "1.0.0"	反序列化时校验（major 相等、minor 不许超前）


    如果是docx
    文档级	doc.pages	{} —— 0 页
    所有元素	prov	[] —— page_no / bbox / charspan 三样全无
    PictureItem	meta	None
    PictureItem	annotations	[]
    PictureItem	captions	[]
    TableItem	captions	[]
    TableItem	prov	[]
    """
    items = _resolve_items(
        list(getattr(getattr(chunk, "meta", None), "doc_items", None) or []), doc)
    #上面把docchunk拆分成一个个item，原来是一个什么格式的都有混在里面的，现在拆分出来
    tables, pictures, formulas = _struct_items(items)

    meta = getattr(chunk, "meta", None)
    headings = list(getattr(meta, "headings", None) or [])
    section_path = " > ".join(h for h in headings if h)
    # `text` 在这里**只剩两个用途**：① chunk_id 的 anchor 原料（`text[:64]`）；
    # ② 判断"元素自身没内容时要不要打 `自身无内容` tag / 告警"。
    # 它**不再作为元素块的内容兜底**（见下面 allow_broad_fallback=False）：
    # 实测这个 text 是**整块**所有元素序列化结果的拼接，含同块其它元素的内容 ——
    # 拆开之后每个元素块吃它，内容就与元素自己无关（实测：无图题的图块 content
    # 里躺着兄弟图块的图题 + 同块正文，一旦被召回，喂给 LLM 的原文和引用片段都错）。
    # 之所以还要照传：anchor 含 `text[:64]`，传空会改掉拆分路径所有块的 id，
    # 触发全库重建 + 引用失效。
    # 纯文本出口不受这条约束 —— 那里块内没有别的元素，text 就是本块全部内容。
    text = (getattr(chunk, "text", "") or "").strip()
    common = dict(
        section_path=section_path, source=kwargs.get("source", ""),
        page_total=kwargs.get("page_total"),
        parser=kwargs.get("parser", _DEFAULT_PARSER),
        origin=kwargs.get("origin", _DEFAULT_ORIGIN),
        file_checksum=kwargs.get("file_checksum", ""),
    )

    if not (tables or pictures or formulas):
        # 纯文本快通道：本函数唯一的非元素出口。
        pages, boxes = _collect_prov(items)
        return [_build_text_chunk(
            chunk, pages=pages, boxes=boxes, text=text,
            chunker=kwargs.get("chunker"), **common,
        )]

    # 正文内容先算出来 —— 它同时决定元素块**能不能吃整块 text**。
    body = _sibling_texts(items, exclude=_BODY_EXCLUDE)
    # "整块 text 只含这一个元素"的判据：块里只有这一个结构元素，且没有别的文本元素。
    # 只有这种情形下 text 才等于元素自身的序列化结果，吃它才没有归属风险。这不是给旧
    # 行为开后门，而是把"禁兜底"说准：禁的是**含同块别人内容的 text**。元素自身内容
    # 取不到时（md 渲染抛异常等）还能靠它保命 —— 否则就是不可逆的内容丢失。
    sole = len(tables) + len(pictures) + len(formulas) == 1
    allow = sole and not body
    #假如只有正文走上面，有正文也有别的就body不为空，为False，
    # 如果没有正文，但是有别的，而且不止一个，也不允许，因为会引入别的item的内容

    out: list[KnowledgeChunk] = []
    for i, t in enumerate(tables):
        # 位置取**该元素自己**的 prov，而不是整块的：拆开之后每块只代表一个元素，
        # 页码/bbox 必须指向它自己，否则引用回溯会高亮到同块别的内容上。
        pages, boxes = _collect_prov([t])
        out.append(_build_table_chunk(
            t, doc, items=items, pages=pages, boxes=boxes, text=text,
            id_prefix=f"t{i}|", with_sibling=False,
            allow_broad_fallback=allow,
            **common,
        ))
    for i, pic in enumerate(pictures):
        pages, boxes = _collect_prov([pic])
        out.append(_build_picture_chunk(
            pic, doc, items=items, pages=pages, boxes=boxes, text=text,
            image_paths=kwargs.get("image_paths"), id_prefix=f"p{i}|",
            with_sibling=False, allow_broad_fallback=allow, **common,
        ))
    for i, fml in enumerate(formulas):
        # 位置同表/图：取元素自己的 prov，引用回溯才不会落到同块别的内容上。
        pages, boxes = _collect_prov([fml])
        out.append(_build_formula_chunk(
            fml, doc, items=items, pages=pages, boxes=boxes, text=text,
            id_prefix=f"f{i}|", allow_broad_fallback=allow, **common,
        ))

    # 正文独立成块（body 已在上面算过）。exclude 用 `_BODY_EXCLUDE`：排掉全部
    # **结构性元素**（表/图/公式，它们都有自己的块），但**不排 caption**：
    # 这条线是"孤儿题注"的唯一承载块 —— 题注对应的元素被丢弃时（如序列化为空的
    # 图被 chunker 的 `if not ser_res.text: continue` 扔掉），题注 item 仍会留在这个
    # 块里；排掉它题注就彻底消失。代价是"元素块 + 正文块"各带一次题注，
    # 但那是两个召回位之间的轻微重复，比丢内容可接受。
    if body:
        pages, _ = _collect_prov(items)
        out.append(KnowledgeChunk(
            source=common["source"], page=pages[0] if pages else None,
            pages=pages,
            kind=KIND_TEXT, section_path=section_path,
            # bbox 留空：这块只承载正文，而表和图的 bbox 不属于它。
            # 给一个"包含其它元素的框"比留空更误导 —— 引用回溯会高亮到别人身上。
            bbox=[],
            page_total=common["page_total"], parser=common["parser"],
            origin=common["origin"], file_checksum=common["file_checksum"],
            content=body,
            retrieval_text=f"{section_path}：{body}" if section_path else body,
            tags=["混排块正文"],
            chunk_id=make_chunk_id(common["source"],
                                   f"body|{section_path}|{pages}|{body[:64]}"),
        ))

    if not out:
        # 控制流上不可达：能走到这里意味着块内至少有一个结构性元素，三个循环必有一个执行过。
        # 之所以不写成 `return out`：将来若有人改动了入口判据，返回空 list 会变成
        # 又一次静默丢块 —— 而这个模块从头到尾在防的就是"静默"。所以宁可回退
        # 纯文本块，并留一条 warning 让这次异常显形。
        #
        # 回退目标从原来的单块构造器换成 `_build_text_chunk`，**结果等价**：
        # `out` 为空的唯一可能是三个 `enumerate` 都没执行 ⟺ `_struct_items` 三组全空
        # ⟺ 合并前那个 `_kind_for_items` 也只会返回 KIND_TEXT。所以这里产出的必然是
        # 纯文本块，不需要再走一轮 kind 分发。
        logger.warning("拆分路径意外产出 0 块，回退纯文本块：%s", kwargs.get("source", ""))
        pages, boxes = _collect_prov(items)
        return [_build_text_chunk(
            chunk, pages=pages, boxes=boxes, text=text,
            chunker=kwargs.get("chunker"), **common,
        )]
    return out


# ---------------------------------------------------------------------------
# 兜底：两种 chunker 都产出 0 块时，按 item 逐条切
# ---------------------------------------------------------------------------
class _ItemAsChunk:
    """把单个 Docling item 包装成 DocChunk 的形状，只为复用唯一分块器的字段映射。

    它只在兜底路径内部流转，**绝不交给 chunker.contextualize**——那需要真正的
    DocChunk（serialize 之类的方法它没有）。所以调 from_docling_chunk_all 时必须不传 chunker。

    每个实例**只包一个 item**（`doc_items=[item]`），所以分组时至多落在
    表 / 图 / 公式 / 纯文本其中之一 —— 兜底路径天然不可能出现混排块。
    """

    class _Meta:
        """替身 meta：只提供唯一分块器真正读的两个属性。

        __slots__ 限定这两个名字，任何笔误（如写成 doc_item）会立刻 AttributeError，
        而不是静默拿到 None —— 兜底路径最不该有"悄悄少了一块"。
        """
        __slots__ = ("headings", "doc_items")

        def __init__(self, headings: list, doc_items: list):
            """headings: 当前累积的标题路径列表（外层循环维护）。
            doc_items: 只放一个元素，`[item]` —— 这是"每实例只包一个"的实现点。"""
            self.headings = headings
            self.doc_items = doc_items

    def __init__(self, item: Any, headings: list, text: str):
        """item: 被包装的单个 docling 元素。
        headings: 该元素所属的标题路径（由 from_docling_items 维护）。
        text: 该元素的文本表示；**同时充当 anchor 原料**（`text[:64]`），
            所以必须传元素的真实文本，不能传空串。"""
        self.meta = _ItemAsChunk._Meta(headings, [item])
        self.text = text


def from_blocks(blocks: list, source: str, *, parser: str = "loader",
                origin: str = "file_loader", page_total: Optional[int] = None,
                file_checksum: str = "",
                splitter: Optional[Any] = None) -> list[KnowledgeChunk]:
    """`loaders.Block` → list[KnowledgeChunk]（duck typing 读属性，本模块不依赖 loaders）。

    blocks: loaders.load_blocks 的产物（Block 对象列表）。空列表返回 []。
        只读六个属性：text / section / page / tags / kind / source_image / needs_vlm
        —— 全部用 getattr 兜底，缺字段不会报错。text 与 source_image 同时为空
        的块被跳过（既无文又无图，没有可检索内容）。
    source: 文件名（basename），写进每个块并参与 chunk_id。
    parser: 解析器名。默认 "loader"；调用方（load_chunks）按扩展名传入，如 "xlsx"。
    origin: 产出通道，默认 "file_loader"。
    page_total: 文档总页数，可为 None。
    file_checksum: 源文件哈希。
    splitter: 可选的二次切分回调，签名 (text) -> list[str]。只对**超长块**有意义：
        传了它就会把一个块切成多块，**并保持 section/page 不变** ——
        切开的只是长度，归属不该变。切分抛异常时回退整块保留（绝不丢内容）。
        None 时每块原样产一条。

    为什么其它格式需要一个专门的入口：csv/xlsx/pptx/json/md 这些格式**各自**能说清
    一部分结构（表头、工作表、页序、标题路径、记录边界），但那部分结构只存在于
    loaders 的 Block 里。走 `load_text` 把它们拍成一段字符串，结构就永久丢了 ——
    "这张表属于哪个工作表""这一页讲什么"全都找不回来。

    每条块的 anchor 是 `block|{i}|{j}|{page}|{piece[:64]}`：i 是源块序号、
    j 是二次切分后的片序号。
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
                pages=[page] if page else [],
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

    text: 块正文。为空时直接返回 section（让块至少带上标题这个可检索信号）。
    section: 标题路径，形如 "父 > 子"。为空时原样返回 text。

    md / xlsx / pptx 的正文块里往往已经带了标题行或工作表名（我们刻意这么写的，
    因为检索只嵌入 `text`）。无条件再拼一次会让同一句话在向量里出现两遍，
    既浪费 token 又会让相似度虚高。

    判重只看**叶子标题**（`section.split(" > ")[-1]`）是否作为子串出现在 text 里 ——
    路径各级往往已在正文中重复出现，用叶子判足够且更宽松。
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

    doc: DoclingDocument。靠 `doc.iterate_items()` 遍历（按阅读顺序）。
    source: 文件名，写进每个块并参与 chunk_id。
    parser: 解析器名，默认 "docling"。
    origin: 产出通道，默认 "docling_pdf"。
    page_total: 文档总页数，可为 None。
    file_checksum: 源文件哈希。
    image_paths: {self_ref: 落盘路径}，透传给图片块填 source_image。

    返回块列表。标题类 item（title / section_header）**不成块**，只用来累加
    headings —— 它们会成为后续块的 section_path。单个 item 转换失败只记 warning
    并跳过，不影响其余 item。

    为什么必须有这条：HybridChunker 对"通篇只有标题、没有正文 item"的文档会返回
    **0 块且不报错**——标题负责开章节，章节里没有 body 就不产出任何块。实测扫描件
    被 OCR 成 section_header 时正好命中：docling 明明抽到了文字，切块返回 0，
    调用方若据此判定"解析不出内容"，就把已有内容丢掉换成 [未能解析]，
    属于**静默数据丢失**。

    这里刻意复用 `from_docling_chunk_all` 而不是另写一份映射：分组判据（`_struct_items`）、
    bbox 收集、表格文字化（`table_to_markdown` / `linearize_table`）、图片价值判定
    （`picture_kind` → kind / needs_vlm）都住在那一层里，另写一份迟早长歪，
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
            # 每个 `_ItemAsChunk` 只包一个 item，所以这里恒产出 1 块；
            # 用 extend 而非 append 是为了与唯一分块器的返回类型（list）对齐，
            # 将来分组逻辑再进化时这里不必跟着改。
            out.extend(from_docling_chunk_all(
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

    doc: DoclingDocument。靠 `doc.export_to_markdown()` 导出；没有该方法或导出失败
        （异常 / 空串）时返回 []。
    source: 文件名，写进块并参与 chunk_id（anchor 为 `whole|{text[:64]}`）。
    parser: 解析器名，默认 "docling"。
    origin: 产出通道，默认 "docling_pdf"。
    page_total: 文档总页数，可为 None。
    file_checksum: 源文件哈希。

    返回 0 或 1 条块（kind=KIND_TEXT，tags=["整文档兜底"]，bbox 恒为空）。
    pages 由遍历 texts / tables / pictures 三处的 prov 汇总而来（升序去重）——
    所以跨页块的页码范围是完整的，引用里能显示"第 3–5 页"。

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
        pages=list(pages),
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
    """把上下文锚点整理成可拼接的前缀。

    anchor: 原始锚点文本（docx 媒体资产所在段落的文字）。None / 空串返回 ""。
        只做 strip，不截断 —— 截断策略由消费方（_docx_asset_to_chunk 的
        retrieval_text 拼装，取 inner[:1500]）各自决定。
    """
    a = (anchor or "").strip()
    return a


def _safe(obj: Any, name: str, default: Any = "") -> Any:
    """读属性，任何异常都退化为默认值。

    obj: 目标对象（duck typing，可能是 DocxAsset 也可能是别的东西）。
    name: 属性名。不存在时返回 default。
    default: 回退值，默认空串。属性值为 None 时同样返回 default。

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

    assets: DocxAsset 对象列表。空 / None 返回 []。
    source: 文件名（basename），写进每个块并参与 chunk_id。
    page_total: 文档总页数（docx 无页码概念，通常为 None）。
    file_checksum: 源文件哈希。

    返回块列表，顺序与 assets 一致。OLE 预览位图（is_ole_preview=True）**不成块**
    —— 它是同一个对象的渲染表示，内容已由内层原件表达；只统计并打一条 info。
    单个资产转换失败只记 warning 并跳过，不影响整份文档。

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
    """单个 DocxAsset → KnowledgeChunk（按 kind 分派：chart / ole / image / 其它）。

    asset: docx_media.DocxAsset（duck typing，全部经 _safe 读属性）。
        只读这些字段：kind / part / anchor_text / alt_text / para_index / error /
        saved_path / chart / prog_id / ole_text / inner_ext。
    source: 文件名（basename），写进块并参与 chunk_id。
    page_total: 文档总页数，可为 None（docx 无页码）。
    file_checksum: 源文件哈希。

    返回一条块。**不抛异常**（属性读取全走 _safe），但调用方 from_docx_media
    仍包了 try —— 因为 chunk_id 计算等步骤理论上仍可能失败。

    分派顺序与理由：
      1. `error` 且 kind != "chart" → 显式"媒体未取出"记录。**这一支必须排在最前**：
         否则"部件没取出来"会被 image 分支接走，变成一条"待 VLM 描述的图片"，
         表面正常、实际内容全没了。
      2. `chart` → 数据来自 chart XML，确定性事实，needs_vlm=False；
         XML 里真没数据时才轮到 VLM。
      3. `ole` → 内层原件有正文就是确定性内容；没有则至少记清"这里有个对象、原件在哪"。
      4. `image` / `other` → 有作者替代文字就用它（确定性、不花钱），没有才标 needs_vlm。
      5. 其余 → 显式"媒体未取出"，不静默丢弃。

    位置信息用 para_index / anchor_text 表达（docx 没有页码与 bbox）。
    """
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
