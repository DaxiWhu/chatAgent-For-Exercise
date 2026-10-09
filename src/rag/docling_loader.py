"""Docling 解析层封装：**本项目唯一 import docling 的地方**。

分层原则：解析后端可换，产物模型不换。将来若改用 MinerU，只需替换本文件
（以及 knowledge_chunk 的字段映射），检索层与 graph 不受影响。

实测环境（docling 2.129.0 / docling-core 2.98.0）与网上多数示例有出入，代码按实测写：
  - docx 的 format option 类名是 **WordFormatOption**（不是 DocxFormatOption）
  - PdfPipelineOptions **没有** do_cell_matching / enable_parallel_processing 字段
  - HybridChunker.chunk() 的参数名是 **dl_doc**
  - HybridChunker.__init__ **不收** max_tokens，token 预算在 tokenizer 里设
因此本文件统一"先探测再使用"：升级 docling 不必改代码。
scripts/check_docling_env.py 可核对以上假设。

所有 docling import 都写在函数内部——未安装时本模块仍可导入，
docling_available() 返回 False，调用方走 pdfminer 降级。
"""
from __future__ import annotations

import hashlib
import inspect
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 自己加载 .env，不依赖"调用方先 import 了 retrieval"。
#
# 为什么必须在这写一遍：下面的配置是在 **import 时**就读成模块常量的，而 .env 的加载
# 原先只发生在 src/retrieval.py 里。于是配置正确与否取决于 import 顺序——
# 实测过：scripts/smoke_docling.py 只 import 本模块，DOCLING_ARTIFACTS_PATH 为空，
# docling 便转去联网下模型，撞上代理直接 502，而跑 pytest（conftest 经 graph 走
# retrieval）却是好的。同一个模块两种行为，排查成本极高。
# load_dotenv 默认不覆盖已存在的环境变量，重复调用无副作用。
try:
    from dotenv import load_dotenv

    # 项目根 = 上溯 3 层（src/rag/x.py → src/rag → src → 根）。少一层会指向
    # src/.env（并不存在）——上面那段注释描述的 502 事故就会**原样复发**，
    # 而且是静默复发：本模块的加载是空操作，只有恰好先 import 到别的模块才碰巧好看。
    _ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    load_dotenv(dotenv_path=os.path.join(_ROOT, ".env"))
except Exception:  # noqa: BLE001  （dotenv 是可选依赖）
    pass

from ..envutil import env_float, env_int  # noqa: E402  （必须在 load_dotenv 之后）

# ---------------------------------------------------------------------------
# 配置（与 retrieval.py 的 RAG_* 风格保持一致）
# ---------------------------------------------------------------------------
DOC_PARSER = os.getenv("RAG_DOC_PARSER", "docling").strip().lower()
DOC_CHUNK_TOKENS = env_int("RAG_DOC_CHUNK_TOKENS", 512, lo=1)
DOCLING_OCR = os.getenv("DOCLING_OCR", "auto").strip().lower()      # auto | on | off
DOCLING_DEVICE = os.getenv("DOCLING_DEVICE", "cpu").strip()
DOCLING_ARTIFACTS = os.getenv("DOCLING_ARTIFACTS_PATH", "").strip()
TOKENIZER_MODE = os.getenv("BGE_TOKENIZER", "hf").strip().lower()    # hf | tiktoken | char
# 与 embedding 用同一个模型，保证 chunk 的 token 预算和向量模型的上限对齐
TOKENIZER_MODEL = os.getenv("EMBEDDING_MODEL", "").strip() or "BAAI/bge-m3"
IMAGES_SCALE = env_float("DOCLING_IMAGES_SCALE", 2.0, lo=0.1)

# ---- 原页截图（供用户回溯核对）----
# auto（默认，PDF 开）| on | off
# 要求：**PDF 要把"从哪儿看到的"连同原图一起返回**；纯文字块不必（没必要，可靠性优先），
# 但扫描件必须给原图 —— 扫描件上"原文"就是那张图，OCR 文字只是它的近似转写。
# ⚠️ 代价：整页位图常驻内存（每页一张）。大文件批量灌库内存吃紧时设 off，
# 此时引用里仍有 页码 + 坐标 + 保真文本，只是没有图。
DOCLING_PAGE_IMAGES = os.getenv("DOCLING_PAGE_IMAGES", "auto").strip().lower()
# 裁剪时向外扩的边距（PDF 点）。太小会把贴着 bbox 的表格边框切掉，
# 让人对不上"这到底是原文里的哪一块"。
CITE_IMAGE_MARGIN = env_float("RAG_CITE_IMAGE_MARGIN", 8.0, lo=0.0)
# 值得返回原图的块类型；纯文字块不返回。
CITE_IMAGE_KINDS = {"table", "chart", "diagram", "formula"}
# 原始 bbox 的最小可信面积（pt²）：低于它认为这个元素的位置不可信，宁可不给图
_MIN_BBOX_AREA_PT = 4.0

# ---- OCR（批次7：中文扫描件）----
# ⚠️ **必须显式配**，不能依赖库的默认值：
# docling 2.129 的 RapidOcrOptions.lang 默认恰好是 ['ch']，中文能"碰巧跑对"；
# 但这个默认值不是契约，上游一旦改成 ['en']（不少 OCR 库就是英文优先），
# 中文扫描件会**静默**退化成乱码——不会报错，只会识别出无意义的字符。
# 另：lang/backend 必须与预取权重时一致（见 scripts/fetch_docling_models.py），
# 否则运行时找不到对应 onnx 文件名，直接报 artifacts not found。
_DEFAULT_OCR_LANG = "ch"
DOCLING_OCR_LANG = [s.strip() for s in
                    os.getenv("DOCLING_OCR_LANG", _DEFAULT_OCR_LANG)
                    .replace(",", " ").split() if s.strip()] or [_DEFAULT_OCR_LANG]
DOCLING_OCR_BACKEND = os.getenv("DOCLING_OCR_BACKEND", "onnxruntime").strip() or "onnxruntime"
# OCR 区域选择：default(PDF_AWARE_LAYOUT_REGIONS) | layout | full | pdf_aware
# 实测：合成扫描件在三种模式下都能捞回文字；但**版面模型漏检的坏件**
# （歪斜、低对比、手写表格）只有 full 能兜住 —— 这是遇到真实坏件的第一个旋钮。
DOCLING_OCR_MODE = os.getenv("DOCLING_OCR_MODE", "default").strip().lower()
# 检测前放大倍数。RapidOCR 自身默认 3.0，对小字号有利但更慢；留空则用库默认。
OCR_SCALE = os.getenv("DOCLING_OCR_SCALE", "").strip()
DOCLING_OCR_SCALE = float(OCR_SCALE) if OCR_SCALE else None


def _ocr_mode() -> Any:
    """字符串 → `OcrMode` 枚举（按名字匹配，认不出就退回 DEFAULT）。"""
    from docling.datamodel.pipeline_options import OcrMode

    table = {
        "default": OcrMode.DEFAULT,
        "pdf_aware": OcrMode.PDF_AWARE_LAYOUT_REGIONS,
        "layout": OcrMode.LAYOUT_REGIONS,
        "layout_regions": OcrMode.LAYOUT_REGIONS,
        "full": OcrMode.FULL_PAGE,
        "full_page": OcrMode.FULL_PAGE,
    }
    mode = table.get(DOCLING_OCR_MODE)
    if mode is None:
        logger.warning("未知的 DOCLING_OCR_MODE=%r，退回 default", DOCLING_OCR_MODE)
        return OcrMode.DEFAULT
    return mode

# 判定"**某一页**有文字层"的最小字符数：低于此值视为该页是图像页（扫描 / 整页插图）。
# 考量是文字层残缺/损坏：用低质量软件"伪 OCR"过的、或嵌入了子集字体导致 pdfminer 解不出字形、
# 或只有标题/页眉被抽出来的文件。这种页能抽出几个到几十个字，但不是真正可用的正文，
# 不开 OCR 读出来全是乱文。
#
# 阈值粒度 2026-09-25 从"整份文档"改为"单页"：混合文档（部分页扫描）用整份判据时
# 总字符数会被电子页淹掉 → 扫描页拿不到 OCR → 那几页的文字永久抽不出来，而整份
# 非空、chunks 非空、status=ok，一条 warning 都不会有。正常一页正文至少几百字，
# 50 对单页同样是安全线。
_TEXT_LAYER_MIN_CHARS = 50


@dataclass
class ParseOutcome:
    """解析结果 + 状态。调用方据此决定是否降级到 pdfminer。"""

    chunks: list = field(default_factory=list)
    status: str = "ok"       # ok | partial | unprocessable | unavailable | failed
    reason: str = ""
    page_total: int | None = None


# ---------------------------------------------------------------------------
# 可用性探测
# ---------------------------------------------------------------------------
def docling_available() -> bool:
    """轻量探测：不加载模型，只确认能 import。降级判断的第一道闸。"""
    try:
        import docling  # noqa: F401
        return True
    except Exception:
        return False


def _text_layer_pages(path: str) -> dict[int, int]:
    """每页的文字字符数（1 基页码 → 字符数）。探测失败返回 `{}`。

    用 pdfminer 探而非 Docling：前者毫秒级且不加载任何模型。
    与 `extract_pdf_text` 共用同一次 `extract_pages` 遍历，但只计数不拼接。

    探不出时返回空 dict，调用方据此按"每页都有文字层"处理 —— 与旧的文档级探针
    失败返回 True 是同一个取向：不给正常文件白加 OCR 开销。

    path: PDF 文件路径。**只探测不解析**（毫秒级、不加载模型）；探测失败返回 {}。
    """
    try:
        from .pdf_parser import pdf_page_text_chars
        return pdf_page_text_chars(path) or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("按页文字层探测失败，按每页都有文字层处理：%s（%s）", path, exc)
        return {}


def _want_ocr(chars_by_page: dict[int, int]) -> bool:
    """按 DOCLING_OCR 决定是否启用 OCR。auto = **存在无文字层的页**就开。

    只服务 PDF：唯一的调用者是 `parse_pdf`（docx 走 `parse_docx`，markup 格式不需要 OCR），
    所以不按 kind 判断。

    auto 的口径是"任一页无文字层就整份开"，不是"整份文本量够就不开"：混合文档里
    只要有一页是扫描，不开 OCR 就意味着那几页的文字永久抽不出来 —— 而整份文档非空、
    chunks 非空、status=ok，一条 warning 都不会有。代价是纯电子版里夹一页整页插图
    （或封面扫描图）时会白跑一次 OCR，只是变慢，结果不受影响。

    探测失败（`chars_by_page` 为空）→ False：按"都有文字层"处理，不白加开销。
    """
    if DOCLING_OCR in ("on", "true", "1", "yes"):
        return True
    if DOCLING_OCR in ("off", "false", "0", "no"):
        return False
    return any(n < _TEXT_LAYER_MIN_CHARS for n in chars_by_page.values())       #只要有扫描件就ocr，图片取整页


def _scanned_pages(chars_by_page: dict[int, int]) -> set:
    """无文字层的页集合 = 只能靠 OCR 读的页。给图判定按这个集合走（块级）。

    chars_by_page: {1 基页码: 该页字符数}，来自 _text_layer_pages。低于
        _TEXT_LAYER_MIN_CHARS（50）的页视为图像页。传空 dict 得空集合
        （等价于"没有扫描页"，不误伤正常文件）。
    """
    return {p for p, n in chars_by_page.items() if n < _TEXT_LAYER_MIN_CHARS}


# ---------------------------------------------------------------------------
# tokenizer 与切分器
# ---------------------------------------------------------------------------
class _CharTokenizer:
    """最后兜底：按字符数估 token。

    中文里"一个字约等于一个 token"是相当好的近似（bge 系分词器对常用汉字基本如此），
    英文会低估、导致英文块偏大——但这条路径只在 HF 与 tiktoken 都不可用时才走到。
    必须实现 BaseTokenizer 的三个抽象方法：count_tokens / get_max_tokens / get_tokenizer。
    """

    def __init__(self, max_tokens: int):
        """按给定的 token 预算创建字符计数器。

        max_tokens: 单块 token 上限（来自 RAG_DOC_CHUNK_TOKENS）。存成 int 供
            get_max_tokens 返回。
        """
        self._max = int(max_tokens)

    def count_tokens(self, text: str) -> int:
        """按字符数估 token（中文近似、英文低估）。

        text: 待估算文本；None 视为空串（返回 0）。
        """
        return len(text or "")

    def get_max_tokens(self) -> int:
        return self._max

    def get_tokenizer(self) -> Any:
        return None


def _unprocessable_with_page(doc: Any, source: str, why: str,
                             page_total: int | None, path: str):
    """解析不出内容的**显式失败记录**，并且尽量带上原页图。

    为什么这类记录也要给图：解析不出内容最常见的原因就是**扫描件 OCR 读不出来**
    （歪斜、低对比、手写、纯图无文字）。这恰恰是最需要人打开原图看一眼的时候 ——
    只回一句"未取到任何内容"，用户既不知道文件是不是坏的，也无从核对。

    doc: DoclingDocument；用来找首页位图（取不到就只返回记录本身）。
    source: 逻辑文件名，写进产物用于回溯。
    why: 判为 unprocessable 的原因文字（进产物，供人排查）。
    page_total: 文档总页数；未知传 None。
    path: 源文件路径；用于推导媒体目录以落盘首页图（经 docx_media.media_dir_for）。
    """
    from .knowledge_chunk import unprocessable

    kc = unprocessable(source, why, parser="docling", page_total=page_total)
    if not _want_page_images():
        return kc
    pages = getattr(doc, "pages", None) or {}
    try:
        page_no = sorted(pages.keys())[0]
        pil = _page_pil(pages[page_no])
        if pil is None:
            return kc
        from .docx_media import media_dir_for
        dest = os.path.join(media_dir_for(path), f"p{page_no:03d}_cite01.png")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        pil.save(dest, "PNG")
        kc.page = page_no
        kc.page_image = dest
        logger.info("未能解析出内容，但已附上原页图供人工核对：%s → %s", source, dest)
    except Exception as exc:  # noqa: BLE001
        logger.debug("失败记录附原页图未成功：%s（%s）", source, exc)
    return kc


def _want_page_images() -> bool:
    """是否要生成整页位图（引用里返回原图的前提）。auto/on → 开。"""
    return DOCLING_PAGE_IMAGES not in ("off", "false", "0", "no")


def _attach_cite_images(doc: Any, chunks: list, path: str, scanned_pages: set) -> int:
    """给需要"看原图"的块裁出原页截图，写进 `page_image`。返回产出数量。

    规则（对应需求的三种情况）：
      - **块所在的页是扫描页**（无文字层，作者传进来的 `scanned_pages`）：整页原图。
        扫描页上原文**就是那张图**，OCR 文字只是它的近似转写；按 bbox 裁一行
        OCR 文本反而更难核对。
      - **表格 / 图表 / 图片 / 公式块**：按 bbox 并集裁出该区域（含边距）。
      - **电子版 PDF 的纯文字块**：**不返回**（用户明确说没必要）。

    判据刻意是**页级**而不是"整份是不是扫描件"：混合文档（部分页电子、部分页扫描）
    里同一份文件两种规则都要用 —— 电子页的表格该裁区域，扫描页的纯文字该给整页。
    用整份一个布尔，二者必有一边错。判的页取 `kc.page`（块的起始页），
    与实际被裁的那一页保持一致，否则会出现"按扫描页的规则去裁电子页"的错配。

    裁剪做错最典型的形态是**上下颠倒**：docling 的 bbox 是**左下原点**（PDF 惯例），
    而 PIL 裁剪用的是左上原点。这里显式做一次换算，不靠"看起来对"。

    doc: DoclingDocument；从中取 `pages`（页号 → PageItem）以拿整页位图。
    chunks: 待处理的 KnowledgeChunk 列表；命中者会被**就地**写上 page_image 路径。
    path: 源文件路径；用于推导截图落盘目录（docx_media.media_dir_for）。
    scanned_pages: 无文字层的页码集合（来自 _scanned_pages）。命中该集合的页给整页图，
        其余页只给"非纯文字"块按 bbox 裁的区域图。返回成功落盘的截图数量。
    """
    if not chunks or not _want_page_images():
        return 0

    pages = getattr(doc, "pages", None) or {}           #dict[int, PageItem]	页号 → 页对象。本项目最核心的一个（page_total、"这页有没有位图"都靠它）
    if not pages:
        return 0

    try:
        from .docx_media import media_dir_for
        out_dir = media_dir_for(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("原页截图目录不可用，跳过：%s（%s）", path, exc)
        return 0

    saved = 0
    n_on_page: dict[int, int] = {}
    for kc in chunks:
        page_no = getattr(kc, "page", None)
        if not page_no or not _needs_cite_image(kc, scanned_pages):     #判断一下是不是在前面筛出来的扫描件集合
            continue
        page = pages.get(page_no)       #
        if page is None:
            continue
        pil = _page_pil(page)
        if pil is None:
            logger.debug("页面无位图（未开 generate_page_images？）：%s p%s", path, page_no)
            continue
        img = _full_page(pil) if page_no in scanned_pages else _crop_region(pil, kc, page)          #扫描件就整页，不是就按照bbox来
        if img is None:
            continue
        n_on_page[page_no] = n_on_page.get(page_no, 0) + 1          #同一页内第几张引用图"的计数器
        ##落盘
        dest = os.path.join(out_dir, f"p{page_no:03d}_cite{n_on_page[page_no]:02d}.png")
        try:
            os.makedirs(out_dir, exist_ok=True)
            img.save(dest, "PNG")
        except Exception as exc:  # noqa: BLE001
            logger.warning("原页截图写盘失败（%s）：%s", dest, exc)
            continue
        kc.page_image = dest
        saved += 1

    if saved:
        logger.info("原页截图已生成：%s（%d 张，其中扫描页 %d 页 → %s）",
                    os.path.basename(path), saved, len(scanned_pages), out_dir)
    return saved


def _needs_cite_image(kc: Any, scanned_pages: set) -> bool:
    """这个块要不要给原图：**块所在的页是扫描页**就全给；其余只给"非纯文字"的块。

    判据是页级（见 `_attach_cite_images` 的说明）：混合文档里同一个布尔无法同时
    照顾电子页和扫描页。

    kc: 一个 KnowledgeChunk；按它的 page 与 kind 判定。
    scanned_pages: 无文字层的页码集合（来自 _scanned_pages）。返回 True/False。
    """
    page_no = getattr(kc, "page", None)
    if page_no and page_no in scanned_pages:
        return True
    return (getattr(kc, "kind", "") or "") in CITE_IMAGE_KINDS


def _page_pil(page: Any) -> Any:
    """从 docling 的 PageItem 里取出这一页的整页位图（PIL 对象）。

    page: docling 的 PageItem。返回 `page.image.pil_image`；未开
        generate_page_images（或该页本就无位图）时返回 None。
    """
    img = getattr(page, "image", None)
    return getattr(img, "pil_image", None) if img is not None else None


def _full_page(pil: Any) -> Any:
    """扫描页用的整页图：返回一份副本。

    pil: 整页位图（PIL Image）。返回 copy 而非原对象——调用方会把它落盘，
        不共享引用以免后续处理改到 docling 缓存的位图。
    """
    return pil.copy()


def _crop_region(pil: Any, kc: Any, page: Any) -> Any:
    """按该块的 bbox 并集裁图；bbox 不可用/退化时返回 None（宁可没有图，也不给错图）。

    pil: 该页整页位图（PIL Image）。
    kc: 目标 KnowledgeChunk；读它的 bbox 列表（每项 l/t/r/b，PDF 点）。
    page: 同一页的 PageItem；用它的 size.height 拿页面高度做坐标换算。
        返回裁剪后的 PIL 图，或在 bbox 缺失 / 退化 / 裁剪区过小时返回 None。
    """
    boxes = [b for b in (getattr(kc, "bbox", None) or []) if len(b) == 4]
    if not boxes:
        return None
    page_h = _page_height_pt(page, pil)
    scale = pil.height / page_h if page_h else IMAGES_SCALE

    xs0, ys0, xs1, ys1 = [], [], [], []
    for left, t, r, b in boxes:
        if t >= b:
            # docling/PDF 惯例：左下原点，y 越大越靠上 → 换算成左上原点
            top_y, bot_y = page_h - t, page_h - b
        else:
            # 已经是左上原点（t 是上边、b 是下边），不换算
            top_y, bot_y = t, b
        xs0.append(min(left, r))
        xs1.append(max(left, r))
        ys0.append(top_y)
        ys1.append(bot_y)

    m = CITE_IMAGE_MARGIN
    # 可信度判据看**原始 bbox 的面积**，不看加完边距的结果：
    # 一个 0.5pt 的退化 bbox 加上 8pt 边距后会"看起来"有面积，但那块位置本身就不可信
    # （docling 对某些行内元素会给零面积框）。宁可没图，也不能给错图。
    raw_w = max(xs1) - min(xs0)
    raw_h = max(ys1) - min(ys0)
    if raw_w * raw_h < _MIN_BBOX_AREA_PT:
        logger.debug("bbox 退化（原始面积 %.2f pt²），跳过原页截图：%s",
                     raw_w * raw_h, getattr(kc, "chunk_id", ""))
        return None

    box = (max(0.0, min(xs0) - m) * scale, max(0.0, min(ys0) - m) * scale,
           min(float(pil.width), (max(xs1) + m) * scale),
           min(float(pil.height), (max(ys1) + m) * scale))
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return None
    return pil.crop(tuple(int(round(v)) for v in box))


def _page_height_pt(page: Any, pil: Any) -> float:
    """页面高度（PDF 点）。拿不到就用位图高度反推（位图是按 images_scale 渲染的）。

    page: docling 的 PageItem；优先取 `page.size.height`。
    pil: 该页位图；仅在取不到 page 尺寸时用来反推（height / IMAGES_SCALE）。
        返回浮点高度（PDF 点）。
    """
    size = getattr(page, "size", None)
    h = getattr(size, "height", None)
    try:
        if h:
            return float(h)
    except Exception:  # noqa: BLE001
        pass
    return pil.height / IMAGES_SCALE if IMAGES_SCALE else float(pil.height)


def _save_pictures(doc: Any, path: str) -> dict:
    """把 PDF 里裁剪出的图片落盘，返回 {self_ref: 本地路径}。

    为什么必须做这一步：docling 的 `generate_picture_images=True` 只是把裁剪结果
    **放在内存**里（`PictureItem.get_image(doc)` 现取现用），**不落盘**。
    而下游有两件事都需要一个能读的文件：
      - 批次 6 的 VLM 描述（要读图字节）；
      - "随回答返回图片"（要给出可引用的路径）。
    没有这一步，"开关打开了"和"图片可用"之间就隔着一条看不见的断链 ——
    产物里 `source_image` 永远是空的，而不会报任何错。

    命名带页码与序号（`p003_fig02.png`），便于人工核对时快速定位到原文位置。
    PDF 裁剪出来的图是位图（PIL Image），不存在 EMF/WMF 这类 VLM 读不了的矢量格式。

    doc: DoclingDocument；遍历它的 `pictures`，逐个取裁剪图落盘。
    path: 源文件路径；用于推导图片落盘目录（docx_media.media_dir_for）。
        返回 {self_ref: 本地路径}；目录不可用、无图或全部写盘失败时返回 {}。
    """
    out: dict[str, str] = {}
    pictures = list(getattr(doc, "pictures", None) or [])
    if not pictures:
        return out

    try:
        from .docx_media import media_dir_for
        out_dir = media_dir_for(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("图片落盘目录不可用，跳过图片导出：%s（%s）", path, exc)
        return out

    saved = 0
    for i, pic in enumerate(pictures, start=1):
        ref = getattr(pic, "self_ref", None)
        if not isinstance(ref, str) or not ref:
            continue
        try:
            img = pic.get_image(doc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("取图片失败（%s）：%s", ref, exc)
            continue
        if img is None:
            # 正常情况：没开 generate_picture_images，或该图只有坐标没有像素
            logger.debug("图片无像素数据（未开 generate_picture_images？）：%s", ref)
            continue
        page = _picture_page(pic)
        name = f"p{page:03d}_fig{i:02d}.png" if page else f"fig{i:02d}.png"
        dest = os.path.join(out_dir, name)
        try:
            os.makedirs(out_dir, exist_ok=True)
            # PNG 无损：图片是给模型看的，不做有损压缩以免小字号/细线丢失
            img.save(dest, "PNG")
        except Exception as exc:  # noqa: BLE001
            logger.warning("图片写盘失败（%s）：%s", dest, exc)
            continue
        out[ref] = dest
        saved += 1

    if saved:
        logger.info("PDF 图片已落盘：%s（%d 张 → %s）",
                    os.path.basename(path), saved, out_dir)
    return out


def _picture_page(pic: Any) -> int:
    """取图片所在页码（用于文件名）；取不到返回 0。

    pic: docling 的 PictureItem；读它的 `prov[0].page_no`。取不到时返回 0，
        调用方据此退化成"不带页码"的文件名（而不是丢掉这张图）。
    """
    try:
        prov = getattr(pic, "prov", None) or []
        if prov:
            return int(getattr(prov[0], "page_no", 0) or 0)
    except Exception:  # noqa: BLE001
        pass
    return 0


def build_tokenizer() -> Any:
    """HybridChunker 必需的 token 计数器。三级回退，保证零外部依赖也能跑。"""
    max_tokens = DOC_CHUNK_TOKENS

    if TOKENIZER_MODE in ("hf", "auto"):
        try:
            from docling_core.transforms.chunker.tokenizer.huggingface import (
                HuggingFaceTokenizer,
            )
            from transformers import AutoTokenizer
            tok = AutoTokenizer.from_pretrained(TOKENIZER_MODEL)
            logger.info("切分 tokenizer：HuggingFace %s（与 embedding 模型对齐）", TOKENIZER_MODEL)
            return HuggingFaceTokenizer(tokenizer=tok, max_tokens=max_tokens)
        except Exception as exc:
            logger.warning("HuggingFace tokenizer 不可用（%s），尝试 tiktoken", exc)

    try:
        import tiktoken
        from docling_core.transforms.chunker.tokenizer.openai import OpenAITokenizer
        enc = tiktoken.get_encoding("cl100k_base")
        logger.info("切分 tokenizer：tiktoken cl100k_base（离线近似）")
        return OpenAITokenizer(tokenizer=enc, max_tokens=max_tokens)
    except Exception as exc:
        logger.warning("tiktoken 不可用（%s），回退字符计数器", exc)

    logger.warning("使用字符计数器作为切分 tokenizer（零外部依赖兜底）")
    return _CharTokenizer(max_tokens)


_CHUNKER: Any = None
_CHUNKER_LOCK = threading.Lock()


def get_chunker() -> Any:
    """HybridChunker 单例。

    它按 DoclingDocument 的**元素边界**分块：表格/图片/公式各自是独立 item，
    不会被字符级硬切；超长表仅按行拆并重复表头（repeat_table_header 默认开）。
    """
    global _CHUNKER
    if _CHUNKER is not None:
        return _CHUNKER
    with _CHUNKER_LOCK:
        if _CHUNKER is None:
            from docling.chunking import HybridChunker
            _CHUNKER = HybridChunker(tokenizer=build_tokenizer())
    return _CHUNKER


def _chunk_kwarg() -> str:
    """HybridChunker.chunk() 的数据参数名在版本间变过：旧 doc= / 新 dl_doc=。"""
    try:
        params = inspect.signature(get_chunker().chunk).parameters
        return "dl_doc" if "dl_doc" in params else "doc"
    except Exception:
        return "dl_doc"


_FALLBACK_CHUNKER: Any = None
_FALLBACK_LOCK = threading.Lock()


def get_fallback_chunker() -> Any:
    """HierarchicalChunker：只按文档结构切，不做 token 预算合并。

    与 HybridChunker 是互补关系，不是替代：它对"孤立标题"不敏感，
    而 HybridChunker 会把没有正文的标题整段跳过（见 to_knowledge_chunks 的阶梯说明）。
    只在第一级产出 0 块时才启用，正常路径不会碰它。
    """
    global _FALLBACK_CHUNKER
    if _FALLBACK_CHUNKER is not None:
        return _FALLBACK_CHUNKER
    with _FALLBACK_LOCK:
        if _FALLBACK_CHUNKER is None:
            from docling.chunking import HierarchicalChunker
            _FALLBACK_CHUNKER = HierarchicalChunker()
    return _FALLBACK_CHUNKER


# ---------------------------------------------------------------------------
# converter（重量级对象，按 OCR 开关缓存复用）
# `_want_ocr(chars_by_page)` 返回的**文档级**布尔 → get_converter(ocr) 取对应实例：
# 只要有一页无文字层（含混合文档）就走"带 OCR"的那个。
# ---------------------------------------------------------------------------
_CONVERTERS: dict[bool, Any] = {}
_CONVERTER_LOCK = threading.Lock()

# 构建失败的原因。模型缺失时不必每次重试——每次重试都要重新扫一遍 artifacts 目录，
# 在"没装模型"的环境里会让每份 PDF 都白等一次。
_CONVERTER_FAILURES: dict[bool, str] = {}


def _set(obj: Any, name: str, value: Any) -> bool:
    """只在目标确有该字段时才设值。

    docling 版本间字段会增删——例如 do_cell_matching 与 enable_parallel_processing
    在 2.129 已不存在，硬设会直接抛错。这里静默跳过并留 debug 日志。

    obj: 待设置属性的对象（如 PdfPipelineOptions 实例）。
    name: 属性名（字段在 docling 版本间会增删，不存在时跳过）。
    value: 要写入的值；**为 None 时直接跳过**（"未配置"与"显式设成 None"在此等价）。
        返回布尔值表示是否真的写入了。
    """
    if value is None:
        return False
    if hasattr(obj, name):
        setattr(obj, name, value)
        return True
    logger.debug("pipeline options 无 %s 字段，跳过（docling 版本差异）", name)
    return False


def _build_converter(ocr: bool) -> Any:
    """按是否启用 OCR 构建一个 Docling DocumentConverter（含 PDF 与 DOCX 两条 format option）。

    ocr: **文档级**开关（来自 _want_ocr）——docling 的 converter 只能整份装/不装 OCR，
        无法按页开关，这也是"给不给整页图"必须另设页级判据的原因。
        构建失败会向上抛，由 get_converter 记入失败缓存。
    """
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
    from docling.document_converter import DocumentConverter, PdfFormatOption, WordFormatOption

    opts = PdfPipelineOptions()

    _set(opts, "do_ocr", ocr)
    _set(opts, "do_table_structure", True)
    _set(opts, "do_picture_classification", True)   # 产出 predicted_class，图片价值判定依赖它
    _set(opts, "generate_picture_images", True)     # 不显式打开就导不出裁剪图
    _set(opts, "images_scale", IMAGES_SCALE)
    # 原页截图：打开后每页会多一张位图（内存开销），换来"引用能带原图"
    _set(opts, "generate_page_images", _want_page_images())
    _set(opts, "artifacts_path", DOCLING_ARTIFACTS or None)   # 预下载模型 → 可离线
    # 公式转 LaTeX：每页跑公式模型，代价明显，默认关（需要时用 DOCLING_FORMULA=1）
    _set(opts, "do_formula_enrichment",
         os.getenv("DOCLING_FORMULA", "0").strip().lower() in ("1", "true", "on", "yes"))

    if ocr:
        mode = _ocr_mode()      #OCR 在页面的哪些区域做识别，就是 _ocr_mode 管的事
        try:
            from docling.datamodel.pipeline_options import RapidOcrOptions
            opts.ocr_options = RapidOcrOptions(
                lang=DOCLING_OCR_LANG,          # ★ 必须显式：中文语料不能依赖库的默认值
                backend=DOCLING_OCR_BACKEND,
                mode=mode,
                **({"scale": DOCLING_OCR_SCALE} if DOCLING_OCR_SCALE else {}),
            )
            logger.info("OCR 显式配置：lang=%s backend=%s mode=%s scale=%s "
                        "（lang 必须与预取权重时一致，否则文件名对不上）",
                        DOCLING_OCR_LANG, DOCLING_OCR_BACKEND, mode, DOCLING_OCR_SCALE)
            if DOCLING_OCR_LANG != [_DEFAULT_OCR_LANG]:
                # 这不是错误，但极容易踩：预取脚本下的是 onnxruntime:ch 的权重，
                # 运行时换语言（如 en）会去找另一套 onnx，报 artifacts not found。
                logger.warning("OCR 语言为 %s，而权重是按 %r 预取的 —— 若报 artifacts not found，"
                               "请用同一语言重跑 scripts/fetch_docling_models.py",
                               DOCLING_OCR_LANG, _DEFAULT_OCR_LANG)
        except Exception as exc:  # noqa: BLE001
            logger.warning("显式配置 OCR 选项失败，退回 docling 默认：%s", exc)

    try:
        opts.table_structure_options.mode = TableFormerMode.ACCURATE   # 合并单元格更准
    except Exception as exc:
        logger.debug("设置 TableFormer ACCURATE 失败：%s", exc)

    try:
        from docling.datamodel.pipeline_options import AcceleratorOptions
        opts.accelerator_options = AcceleratorOptions(
            device=DOCLING_DEVICE, num_threads=os.cpu_count() or 4
        )
    except Exception as exc:
        logger.debug("设置 accelerator 失败：%s", exc)

    fmt: dict = {InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    try:
        fmt[InputFormat.DOCX] = WordFormatOption()
    except Exception as exc:
        logger.debug("DOCX format option 不可用：%s", exc)

    logger.info("构建 Docling converter（device=%s, ocr=%s, table=ACCURATE）", DOCLING_DEVICE, ocr)
    return DocumentConverter(format_options=fmt)


def get_converter(ocr: bool = False) -> Any:
    """converter 单例（按 OCR 开关分桶缓存）。模型只加载一次，务必复用。

    ocr: 是否要带 OCR 的 converter；**它同时是缓存桶的键**，故 True/False 各缓存一份。
        命中的直接返回；先前构建失败过的会抛 RuntimeError（不重试，避免每次白扫
        artifacts 目录）。线程安全（内部加锁）。
    """
    if ocr in _CONVERTERS:
        return _CONVERTERS[ocr]
    if ocr in _CONVERTER_FAILURES:
        raise RuntimeError(_CONVERTER_FAILURES[ocr])
    with _CONVERTER_LOCK:
        if ocr not in _CONVERTERS:
            if ocr in _CONVERTER_FAILURES:
                raise RuntimeError(_CONVERTER_FAILURES[ocr])
            try:
                _CONVERTERS[ocr] = _build_converter(ocr)
            except Exception as exc:
                _CONVERTER_FAILURES[ocr] = f"converter 构建失败：{exc}"
                raise
    return _CONVERTERS[ocr]


def reset_converter_cache() -> None:
    """清掉 converter 与失败缓存。仅在测试或补齐模型后需要重新尝试时使用。"""
    with _CONVERTER_LOCK:
        _CONVERTERS.clear()
        _CONVERTER_FAILURES.clear()


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def _error_text(result: Any) -> str:
    """把 docling ConversionResult 上的错误列表拼成一行文本。

    result: docling 的转换结果对象；读它的 `errors`（每项取 `error_message`）。
        没有错误时返回 "无详细信息"——用作 partial / failed 的原因文字。
    """
    errs = getattr(result, "errors", None) or []
    txt = "; ".join(str(getattr(e, "error_message", e)) for e in errs)
    return txt or "无详细信息"


def file_checksum(path: str) -> str:
    """文件内容指纹：源不变 → chunk_id 不变 → 可增量、可缓存。

    path: 源文件路径。按 1MB 分块流式 sha1，返回前 16 位十六进制；文件读不到时
        返回 ""（调用方按"无指纹"处理，不阻断解析）。
    """
    h = hashlib.sha1()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()[:16]
    except Exception:
        return ""


def _doc_has_content(doc: Any) -> bool:
    """DoclingDocument 里到底有没有抽到东西。

    这是"unprocessable 还是我们自己的 bug"的分水岭：文档没内容 = 真的解析不出；
    文档有内容而切块为空 = 实现问题，必须兜底，绝不能当解析失败丢掉。

    doc: DoclingDocument；只要 `texts` / `tables` / `pictures` 三者任一非空就返回 True
        （`groups` 与表单类字段对 PDF 报表恒空，不参与判定）。
    """
    for attr in ("texts", "tables", "pictures"):
        try:
            if len(getattr(doc, attr, None) or []):
                return True
        except Exception:
            continue
    return False


def _chunk_once(doc: Any, chunker: Any, kwarg: str) -> list:
    """跑一次切块；失败只告警不抛，交给调用方的阶梯继续兜底。

    doc: 待切分的 DoclingDocument。
    chunker: 切分器实例（HybridChunker 或 HierarchicalChunker）。
    kwarg: chunk() 的数据参数名（"dl_doc" 或 "doc"，由 _chunk_kwarg 探测得出）。
        返回 DocChunk 列表；异常时返回 []（供调用方决定是否换下一级阶梯）。
    """
    try:
        return list(chunker.chunk(**{kwarg: doc}))
    except Exception as exc:
        logger.warning("%s 切块失败（%s）：%s",
                       type(chunker).__name__, type(exc).__name__, exc)
        return []


def to_knowledge_chunks(doc: Any, source: str, *, kind: str = "pdf",
                        page_total: int | None = None,
                        checksum: str = "",
                        image_paths: dict | None = None) -> list:
    """DoclingDocument → list[KnowledgeChunk]，四级阶梯，确保抽到的内容不被丢掉。

    阶梯设计的原因
      1. **HybridChunker**（默认）—— token 预算 + 结构感知，正常情况都用它。
      2. **HierarchicalChunker** —— 纯结构切，不做 token 合并。当文档只有标题没有正文时，
         第一级会返回 **0 块且不抛异常**（标题开章节，章节里没有 body 就不产出）。
      3. **按 item 逐条兜底** —— 直接遍历文档元素一件一件成块，粒度最好。
      4. **整文档 Markdown 单块** —— 前三级都"挑食"时（实测：全文只有一个 section_header，
         三级全都把标题跳过）保证不变式「文档有内容 → 一定有块」。

    doc: 待转换的 DoclingDocument。
    source: 逻辑文件名，写进每个 KnowledgeChunk（**参与 chunk_id，必须稳定**）。
    kind: "pdf" 或 "docx"；只用来决定 origin 标记（docling_pdf / docling_docx）。
    page_total: 文档总页数（未知传 None），透传给产物。
    checksum: 文件内容指纹（file_checksum 的产物），透传给产物供增量判断。
    image_paths: {self_ref: 本地图片路径}（_save_pictures 的产物），让图片块能挂上
        source_image；None 视为无图。返回 list[KnowledgeChunk]。

    此函数pdf正常走，每一个地方都能处理到东西
    但是docx不行，有些地方docling解析出来的字段是没有的，所以会走一些兜底策略
    """
    from .knowledge_chunk import (
        from_docling_chunk_all,
        from_docling_items,
        from_docling_markdown,
    )

    origin = "docling_pdf" if kind == "pdf" else "docling_docx"
    kwarg = _chunk_kwarg()

    for chunker in (get_chunker(), get_fallback_chunker()):
        raw = _chunk_once(doc, chunker, kwarg)      #list[DocChunk]。如果是docx切出来没有图片和公式只有表格和文本
        if not raw:
            continue
        out = []
        for ch in raw:
            try:
                # 用 *_all 而不是单块版：一个块里可能有多张表 / 多张图，
                # 旧的"取第一个代表元素"会让其余静默丢失（见 from_docling_chunk_all）。
                # extend 而非 append —— 一个 DocChunk 现在可能拆出多个 KnowledgeChunk。
                # 这里传入的是docchunk
                out.extend(from_docling_chunk_all(
                    ch, doc, chunker=chunker, source=source, parser="docling",
                    origin=origin, page_total=page_total, file_checksum=checksum,
                    image_paths=image_paths,
                ))
            except Exception as exc:
                logger.warning("单个 chunk 转换失败，跳过该块：%s", exc)
        if out:
            if type(chunker).__name__ != "HybridChunker":
                logger.info("HybridChunker 未产出块，已由 %s 接管：%s",
                            type(chunker).__name__, source)
            return out
        logger.warning("%s 产出了 %d 块但全部转换失败：%s",
                       type(chunker).__name__, len(raw), source)

    if not _doc_has_content(doc):
        return []

    logger.warning("两种切分器均未产出块，改按 item 逐条兜底：%s", source)
    out = from_docling_items(doc, source=source, parser="docling", origin=origin,
                             page_total=page_total, file_checksum=checksum,
                             image_paths=image_paths)
    if out:
        return out

    logger.warning("逐条兜底也未产出块，改用整文档 Markdown 单块：%s", source)
    return from_docling_markdown(doc, source=source, parser="docling", origin=origin,
                                 page_total=page_total, file_checksum=checksum)


@dataclass
class _Converted:
    """转换中间结果。把"转换并判状态"这段抽出来，是为了让 pdf / docx 两条入口
    共用同一份状态语义——各写一遍迟早会不一致。"""

    doc: Any = None
    partial: bool = False
    partial_reason: str = ""
    error: str = ""          # 非空表示失败，调用方据此降级


def _convert_document(path: str, ocr: bool) -> _Converted:
    """跑一次 Docling 转换并把结果归一成 _Converted（"转换 + 判状态"只此一份实现）。

    path: 待转换的文件路径。
    ocr: 是否使用带 OCR 的 converter（决定取哪个缓存桶）。
        返回 _Converted：error 非空表示失败（调用方据此降级），否则 doc 为
        DoclingDocument、partial 标记"部分成功"。**本函数不抛**——所有异常都转成 error。
    """
    try:
        conv = get_converter(ocr)
    except Exception as exc:  # noqa: BLE001
        return _Converted(error=f"converter 初始化失败：{exc}")

    try:
        result = conv.convert(path, raises_on_error=False)
    except Exception as exc:  # noqa: BLE001
        return _Converted(error=f"转换异常：{exc}")

    status = str(getattr(getattr(result, "status", None), "value",
                         getattr(result, "status", ""))).lower()
    if "failure" in status or "skipped" in status:
        return _Converted(error=f"转换失败（{status}）：{_error_text(result)}")

    doc = getattr(result, "document", None)
    if doc is None:
        return _Converted(error="转换结果中没有 document")

    return _Converted(doc=doc, partial=("partial" in status),
                      partial_reason=_error_text(result) if "partial" in status else "")


def parse_pdf(path: str, source: str) -> ParseOutcome:
    """解析一份 PDF → ParseOutcome。

    **只管 PDF**。docx 不经过这里：`retrieval._parse_docx` 直接调用后文的
    `parse_docx`（它走的是另一套「Docling docx 后端 + 解 zip 取媒体」链路）。

    2026-09-26 起由 `parse_document` 改名而来，同时删掉了原来那个 `kind` 参数与
    `kind="docx"` 转发分支 —— 那条分支全库零调用点（docx 的真实入口一直在
    `parse_docx`），却让本函数看起来像"所有格式的统一入口"。改名是为了让签名
    自己说清职责。

    status 语义：
      ok            —— 成功，采用 chunks
      partial       —— 部分成功，仍采用，但已记 warning
      unprocessable —— 解析不出内容（多为扫描件且 OCR 未开），产出显式失败记录
      unavailable   —— docling 未装或被配置关闭，调用方应降级
      failed        —— 转换失败，调用方应降级

    path: 待解析的 PDF 文件路径。
    source: 逻辑文件名（参与 chunk_id，必须稳定；日志与产物都用它）。
        返回 ParseOutcome。
    """
    if DOC_PARSER != "docling":
        return ParseOutcome(status="unavailable", reason=f"RAG_DOC_PARSER={DOC_PARSER}")
    if not docling_available():     #检查docling是否可import
        return ParseOutcome(status="unavailable", reason="docling 未安装")

    # 一次按页探测，两种粒度消费：OCR 是**文档级**开关（converter 只能整份装/不装），
    # "给不给整页图"是**页级**（混合文档里两种规则要同时用，见 _attach_cite_images）。
    chars_by_page = _text_layer_pages(path)         #这里拿到一个字典，key是页数，value是字数
    scanned_pages = _scanned_pages(chars_by_page)   #拿到大概率是扫描件的集合
    ocr = _want_ocr(chars_by_page)
    if scanned_pages and not ocr:
        # 有图像页却不开 OCR（DOCLING_OCR=off，或用户显式关掉）：那几页的文字会丢，
        # 必须留痕 —— 否则整份文档 status=ok，没人知道少了几页。
        logger.warning("检测到 %d 个无文字层的页，但 OCR 未启用（DOCLING_OCR=%s），"
                       "这些页的文字可能取不到：%s（页 %s）",
                       len(scanned_pages), DOCLING_OCR, source,
                       sorted(scanned_pages)[:10])
    conv = _convert_document(path, ocr)
    #conv：
    # doc: Any = None  # ← 这里装的才是"解析出来的东西"
    # partial: bool = False
    # partial_reason: str = ""
    # error: str = ""  # 非空表示失败
    # doc:
            #schema_name	Literal["DoclingDocument"]	类型标识，跨进程/跨存储传输时自证"我是什么对象"	不用
            # version	str（版本模式校验）	schema 版本。1.0.0 → 1.1.0 的迁移靠它触发（见下面 furniture）	不用
            # name	str	文档工作名，不含扩展名（来自文件名，或 "Untitled 1"）	不用（我们用 source）
            # origin	DocumentOrigin | None	来源信息：mimetype / binary_hash / filename / uri	未消费
            #texts	list[TitleItem | SectionHeaderItem | ListItem | CodeItem | FormulaItem | FieldHeadingItem | FieldValueItem | TextItem]	按类型分桶的平铺实体全集，元素自带 text 全文	★ 核心
            # tables	list[TableItem]	表格实体，自带 data（单元格网格）	★ 核心
            # pictures	list[PictureItem]	图片实体，自带 image（PIL）	★ 核心
            # groups	list[ListGroup | InlineGroup | GroupItem]	分组容器（列表组、内联组、章节组），表达"这几项属于一组"	不用
            # body	GroupItem	正文阅读顺序树的根（#/body），children 是引用链	间接依赖（切块沿它走）
            # furniture	GroupItem（deprecated）
            #pages	dict[int, PageItem]	页号 → 页对象。本项目最核心的一个（page_total、"这页有没有位图"都靠它）
            #key_value_items: list[KeyValueItem]	键值对抽取（票据、表单类文档）
            # form_items: list[FormItem]	表单结构（控件、字段区域）
            # field_regions / field_items	2.98 新增的字段区域/字段。空列表时连序列化都省掉（类里有个 model_serializer 专门删这两个空字段）


    #下游字段
            #NodeItem（所有节点）
                #   self_ref: str	JSON pointer（#/texts/3），全文档唯一地址，_resolve_items 的钥匙
                # parent / children: list[RefItem]	树结构的上下连接（只有 GroupItem 的 children 有实义）
                # content_layer: ContentLayer	BODY / FURNITURE —— 判"这是不是页眉页脚"要看这个
                # meta: BaseMeta | None	插件产物挂载点：图片分类结果就在 meta.classification.predictions（_predicted_class 读的新路径）
            # DocItem（承载内容）
                # label: DocItemLabel	元素类型：text / section_header / list_item / table / picture / formula …（_BODY_EXCLUDE 用的就是它）
                # prov: list[ProvenanceItem]	位置溯源，pages / bbox 的唯一来源
                # ProvenanceItem	page_no: int、bbox: BoundingBox	页码 + 框（l/t/r/b，左下原点 —— _crop_region 换算的就是它）
                # charspan: CharSpan	该元素在原文里的字符区间。本项目没用（用了本可做更精确的文本回溯）
            # FloatingItem（表/图基类）
                # captions: list[RefItem]	题注是引用不是文本，所以 caption_text(doc) 必须 resolve(doc) 才能拿到字——这就是"caption 为什么可能是空"的第一层成因
                # references / footnotes	正文里的交叉引用 / 脚注（本项目未用）
                # image: ImageRef | None	该元素自己的裁剪图
             # TableItem
                # data: TableData	单元格网格（table_cells / num_rows / num_cols），每个 cell 带 text / bbox / row_span / col_span —— Markdown 线性化的原料，也是 JSON 往返会丢的那个 data



    if conv.error:
        return ParseOutcome(status="failed", reason=conv.error)

    doc = conv.doc
    pages = getattr(doc, "pages", None) or {}
    page_total = len(pages) or None
    checksum = file_checksum(path)
    image_paths = _save_pictures(doc, path)
    chunks = to_knowledge_chunks(doc, source, kind="pdf", page_total=page_total,
                                 checksum=checksum, image_paths=image_paths)
    # 原页截图：让引用能带"从哪儿看到的"那张图（扫描页给整页，表格/图给区域，纯文字不给）
    try:
        _attach_cite_images(doc, chunks, path, scanned_pages=scanned_pages)
    except Exception as exc:  # noqa: BLE001  截图失败不该影响已解析出的内容
        logger.warning("原页截图生成失败（不影响解析）：%s（%s: %s）",
                       source, type(exc).__name__, exc)

    if not chunks:
        # 区分"文档真的没抽到东西"与"抽到了但切块没产出"——后者是我们自己的问题，
        # 原因写成"疑似扫描件"会把排查方向带偏（实测就被这样误导过一次）。
        if _doc_has_content(doc):
            why = ("文档已抽到内容但切块未产出任何块（切分器异常，属实现问题，请查日志）")
            logger.error("Docling 抽到内容却切不出块：%s（ocr=%s）", source, ocr)
        else:
            why = "未取到任何内容（疑似扫描件且未启用 OCR，或版面识别失败）"
            logger.warning("Docling 未产出内容，标记 unprocessable：%s（ocr=%s, %s）",
                           source, ocr, why)
        return ParseOutcome(chunks=[_unprocessable_with_page(doc, source, why,
                                                             page_total, path)],
                            status="unprocessable", reason=why, page_total=page_total)

    if conv.partial:
        logger.warning("Docling 部分成功，已采用其输出：%s（%s）", source, conv.partial_reason)
        return ParseOutcome(chunks=chunks, status="partial",
                            reason=conv.partial_reason, page_total=page_total)

    logger.info("Docling 解析完成：%s（%d 页 → %d 块，ocr=%s）",
                source, page_total or 0, len(chunks), ocr)
    return ParseOutcome(chunks=chunks, status="ok", page_total=page_total)


# ---------------------------------------------------------------------------
# docx：A（Docling docx 后端）+ C（解 zip 取媒体）
# ---------------------------------------------------------------------------
def _anchor_key(chunk: Any) -> str:
    """取用于位置对齐的检索文本（正文块与媒体块都走这里，保证两边口径一致）。

    chunk: 一个 KnowledgeChunk。返回 `retrieval_text + "\\n" + content`——
        两路都取，是为了让"锚点段落被切分器改写"时仍有机会命中（见 _find_anchor_index）。
    """
    return f"{getattr(chunk, 'retrieval_text', '') or ''}\n{getattr(chunk, 'content', '') or ''}"


def _find_anchor_index(texts: list, anchor: str, start: int) -> int | None:
    """在 texts[start:] 里找第一个包含 anchor 的块，找不到返回 None。

    先用整段、再退到前 32/16 字：锚点段落可能被切分器切开，或两端空白不同，
    整段匹配未必命中。**这是尽力而为的对齐**，找不到就由调用方追加到末尾并告警——
    位置不准比"内容丢失"轻得多，绝不能为了对齐把块丢掉。

    texts: 已对齐好的正文块的锚点文本列表（每项来自 _anchor_key）。
    anchor: 待插入媒体块的 anchor_text（docx_media 的 DocxAsset.anchor_text）。
    start: 从下标 start 开始往后找（单调游标，避免后出现的媒体被插到更前面）。
        返回命中的下标；找不到返回 None。
    """
    a = (anchor or "").strip()
    if not a:
        return None
    probes = [p for p in (a, a[:32].strip(), a[:16].strip()) if p]
    for i in range(max(start, 0), len(texts)):
        t = texts[i] or ""
        if any(p in t for p in probes):
            return i
    return None


def _splice_media(body: list, media: list) -> list:
    """把媒体块按上下文锚点插回正文序列，还原"它在第几段之后"。

    为什么值得做：docx 是流式格式，没有页码与 bbox，位置只能靠"落在哪一段"表达。
    媒体块插到它的锚点段之后，下游做上下文拼装时就能自然拿到前后文；
    全堆在文档末尾虽然也不丢数据，但失去了这一层信息。

    单调扫描（cursor 只前进）而不是每块独立查找：媒体块本身就是文档流顺序，
    单向前进可以避免后出现的媒体被插到更靠前的位置（锚点重复时最容易发生）。

    顺带继承 `section_path`：媒体块来自解 zip，本身不知道自己在哪个章节标题下，
    而它插入位置的前一个正文块知道。不继承的话，图片/图表就丢掉了"属于哪一节"
    这个对检索很关键的线索。

    body: 正文 KnowledgeChunk 列表（A 路线产物，可能已经过 _reconcile_docling_pictures 处理）。
    media: 媒体 KnowledgeChunk 列表（C 路线产物，顺序即文档流顺序）。
        返回合并后的新列表（不修改入参列表本身）；两侧都为空时返回空列表。
    """
    if not media:
        return list(body)
    if not body:
        return list(media)

    out = list(body)
    texts = [_anchor_key(c) for c in out]
    cursor = 0
    appended = 0

    for mc in media:
        idx = _find_anchor_index(texts, getattr(mc, "anchor_text", ""), cursor)
        if idx is None:
            _inherit_section(mc, out[-1] if out else None)
            out.append(mc)                       # 对不上就追加，绝不丢
            texts.append("")
            appended += 1
            continue
        _inherit_section(mc, out[idx])
        out.insert(idx + 1, mc)
        texts.insert(idx + 1, "")
        cursor = idx + 1

    if appended:
        logger.info("docx 媒体块：%d 个已对齐插入，%d 个锚点对不上已追加到末尾",
                    len(media) - appended, appended)
    return out


def _inherit_section(chunk: Any, donor: Any) -> None:
    """媒体块没有 section_path 时，从它前一个正文块继承（标题路径）。

    chunk: 目标媒体块；**就地**写它的 section_path（自身已有则保持不动）。
    donor: 提供 section_path 的前一个正文块；为 None 或它自己也没有时什么都不做。
        无返回值。
    """
    if donor is None or getattr(chunk, "section_path", ""):
        return
    sp = getattr(donor, "section_path", "") or ""
    if sp:
        chunk.section_path = sp


def _reconcile_docling_pictures(body: list, media: list) -> tuple[list, int, int]:
    """docx 的图片以 C 路线（解 zip）为准，处理 Docling 产出的图片类块。

    为什么会重复：Docling 的 docx 后端确实能"看到"图表/图片，但它给不出图像字节、
    给不出位置、也给不出 caption。同一张图，C 路线给的是"文件 + 替代文字 + 第几段"，
    A 路线给的是"这里有张图"。两条都入库，无信息的那条只会拖低精度。

    分两种情况处理：
      - **空块**（content 为空，retrieval_text 只有一句"图表"）→ 直接丢弃；
      - **混排块**（图文并进同一块，正文才是主体）→ 降级为文本块：
        保留 content / retrieval_text（正文是 A 路线独有的价值），
        但清掉 needs_vlm —— 图片部分已由 C 路线索引，块里也没有图像字节，
        留着 True 会让批次6 去描述一个无图可取的块。

    但**不能无条件处理**：万一解 zip 只覆盖了一部分（rels 类型认不出、part 缺失），
    丢掉 Docling 的块就等于把"这里曾有张图"的信号也抹掉。
    所以加一条覆盖数保护 —— C 的图片块数不少于受影响的 Docling 块数时才动手，
    否则原样保留并告警。返回 (块列表, 丢弃数, 降级数)。

    body: A 路线产出的正文块列表（含可能的 Docling 图片类块）。
    media: C 路线产出的媒体块列表；只用来数"覆盖了几个图片类块"。
        返回 (过滤/降级后的块列表, 丢弃的空块数, 降级为文本的块数)；受影响的块会被
        **就地**改 kind / needs_vlm / tags。
    """
    from .knowledge_chunk import KIND_CHART, KIND_DIAGRAM, KIND_TEXT

    pics = [c for c in body
            if getattr(c, "origin", "") == "docling_docx"
            and getattr(c, "kind", "") in (KIND_CHART, KIND_DIAGRAM)]
    if not pics:
        return body, 0, 0

    covered = sum(1 for m in media if getattr(m, "kind", "") in (KIND_CHART, KIND_DIAGRAM))
    if covered < len(pics):
        logger.warning("docx：Docling 有 %d 个图片类块，但解 zip 只覆盖到 %d 个，"
                       "保守起见全部保留（宁可重复也不丢信号）", len(pics), covered)
        return body, 0, 0

    drop_ids, downgraded = set(), 0
    for c in pics:
        if (getattr(c, "content", "") or "").strip():
            c.kind = KIND_TEXT
            c.needs_vlm = False
            # 降级成文本块后，"这是个图表元素"和"元素自身没内容"两个状态都不再成立，
            # 所以要一起剔掉（`待VLM` 同理由：这个块已经不需要 VLM 了）。
            c.tags = [t for t in (c.tags or [])
                      if t not in ("图表", "待VLM", "自身无内容")]
            if "含图" not in c.tags:
                c.tags.append("含图（图片由解zip索引）")
            downgraded += 1
        else:
            drop_ids.add(id(c))

    kept = [c for c in body if id(c) not in drop_ids]
    logger.info("docx：Docling 图片类块处理完毕 —— 丢弃空块 %d 个、降级为文本块 %d 个"
                "（解 zip 覆盖 %d 个图片）", len(drop_ids), downgraded, covered)
    return kept, len(drop_ids), downgraded


def parse_docx(path: str, source: str) -> ParseOutcome:
    """docx 双路线解析 → ParseOutcome。

    A（Docling docx 后端）：正文、标题层级、普通表格 —— 结构最好的来源；
      且 docx 的表格本身是结构化的，不需要 TableFormer，比"转 PDF 再识别"更准。
    C（src/docx_media.py 解 zip）：图片原件、OLE 原件、**图表原始数据**、段落位置 ——
      这几类 Docling 在 docx 上根本拿不到（实测：OLE 完全忽略、图表无渲染图、图片时有时无）。

    两条路线互补而不是二选一：A 的 body 树顺序 + C 的段落序号，用锚点文本对齐，
    媒体就能挂回它所属的上下文。

    降级语义与 PDF 一致：docling 不可用 → unavailable（调用方走 python-docx 老链路）；
    转换失败 → failed。媒体提取失败**不算失败** —— 正文仍由 A 提供，
    只把媒体那一部分记成显式记录（见 docx_media / knowledge_chunk 的媒体未取出分支）。

    path: 待解析的 .docx 文件路径。
    source: 逻辑文件名（参与 chunk_id，必须稳定）。返回 ParseOutcome。
    """
    if DOC_PARSER != "docling":
        return ParseOutcome(status="unavailable", reason=f"RAG_DOC_PARSER={DOC_PARSER}")
    if not docling_available():
        return ParseOutcome(status="unavailable", reason="docling 未安装")

    conv = _convert_document(path, ocr=False)     # docx 是 markup 格式，永远不需要 OCR
    if conv.error:
        return ParseOutcome(status="failed", reason=conv.error)

    from .docx_media import extract as extract_media
    from .knowledge_chunk import from_docx_media, unprocessable

    checksum = file_checksum(path)
    body = to_knowledge_chunks(conv.doc, source, kind="docx", checksum=checksum)

    media: list = []
    try:
        assets = extract_media(path)
        media = from_docx_media(assets, source, file_checksum=checksum)
    except Exception as exc:  # noqa: BLE001  媒体提取失败不该让整份 docx 挂掉
        logger.warning("docx 媒体提取失败（正文仍保留）：%s（%s: %s）",
                       source, type(exc).__name__, exc)

    # 先去重/降级再对位：Docling 的图片类块若留着，会成为媒体块的"上下文捐赠者"，
    # 把一个没有信息的块当成章节来源 —— 顺序反了会得到看似合理实则空洞的 section。
    body, _dropped, _downgraded = _reconcile_docling_pictures(body, media)
    chunks = _splice_media(body, media)

    if not chunks:
        why = "docx 未抽出任何正文与媒体（空文档、文件损坏或格式不受支持）"
        logger.warning("Docling 未产出内容，标记 unprocessable：%s（%s）", source, why)
        return ParseOutcome(chunks=[unprocessable(source, why, parser="docling")],
                            status="unprocessable", reason=why)

    if conv.partial:
        logger.warning("Docling 部分成功，已采用其输出：%s（%s）", source, conv.partial_reason)
        return ParseOutcome(chunks=chunks, status="partial", reason=conv.partial_reason)

    logger.info("docx 解析完成：%s（正文 %d 块 + 媒体 %d 块，共 %d 块）",
                source, len(body), len(media), len(chunks))
    return ParseOutcome(chunks=chunks, status="ok")
