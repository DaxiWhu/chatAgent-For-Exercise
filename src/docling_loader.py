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
from typing import Any, Optional

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

    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(dotenv_path=os.path.join(_ROOT, ".env"))
except Exception:  # noqa: BLE001  （dotenv 是可选依赖）
    pass

# ---------------------------------------------------------------------------
# 配置（与 retrieval.py 的 RAG_* 风格保持一致）
# ---------------------------------------------------------------------------
DOC_PARSER = os.getenv("RAG_DOC_PARSER", "docling").strip().lower()
DOC_CHUNK_TOKENS = int(os.getenv("RAG_DOC_CHUNK_TOKENS", "512") or 512)
DOCLING_OCR = os.getenv("DOCLING_OCR", "auto").strip().lower()      # auto | on | off
DOCLING_DEVICE = os.getenv("DOCLING_DEVICE", "cpu").strip()
DOCLING_ARTIFACTS = os.getenv("DOCLING_ARTIFACTS_PATH", "").strip()
TOKENIZER_MODE = os.getenv("BGE_TOKENIZER", "hf").strip().lower()    # hf | tiktoken | char
# 与 embedding 用同一个模型，保证 chunk 的 token 预算和向量模型的上限对齐
TOKENIZER_MODEL = os.getenv("EMBEDDING_MODEL", "").strip() or "BAAI/bge-m3"
IMAGES_SCALE = float(os.getenv("DOCLING_IMAGES_SCALE", "2.0") or 2.0)

# 判定"有文字层"的最小字符数：低于此值视为扫描件
_TEXT_LAYER_MIN_CHARS = 50


@dataclass
class ParseOutcome:
    """解析结果 + 状态。调用方据此决定是否降级到 pdfminer。"""

    chunks: list = field(default_factory=list)
    status: str = "ok"       # ok | partial | unprocessable | unavailable | failed
    reason: str = ""
    page_total: Optional[int] = None


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


def _has_text_layer(path: str) -> bool:
    """探测 PDF 有无可用文字层：决定 auto 模式要不要开 OCR。

    用 pdfminer 探而非 Docling：前者毫秒级且不加载任何模型。
    探不出时保守返回 True（按"有文字层"处理），避免给正常文件白加 OCR 开销。
    """
    try:
        from .pdf_parser import extract_pdf_text
        return len((extract_pdf_text(path) or "").strip()) >= _TEXT_LAYER_MIN_CHARS
    except Exception as exc:
        logger.debug("文字层探测失败，按有文字层处理：%s（%s）", path, exc)
        return True


def _want_ocr(path: str, kind: str) -> bool:
    """按 DOCLING_OCR 决定是否启用 OCR。auto = 无文字层才开。"""
    if DOCLING_OCR in ("on", "true", "1", "yes"):
        return True
    if DOCLING_OCR in ("off", "false", "0", "no"):
        return False
    if kind == "docx":
        return False                      # docx 是 markup 格式，不需要 OCR
    return not _has_text_layer(path)


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
        self._max = int(max_tokens)

    def count_tokens(self, text: str) -> int:
        return len(text or "")

    def get_max_tokens(self) -> int:
        return self._max

    def get_tokenizer(self) -> Any:
        return None


def build_tokenizer() -> Any:
    """HybridChunker 必需的 token 计数器。三级回退，保证零外部依赖也能跑。"""
    max_tokens = DOC_CHUNK_TOKENS

    if TOKENIZER_MODE in ("hf", "auto"):
        try:
            from transformers import AutoTokenizer
            from docling_core.transforms.chunker.tokenizer.huggingface import (
                HuggingFaceTokenizer,
            )
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
    """
    if value is None:
        return False
    if hasattr(obj, name):
        setattr(obj, name, value)
        return True
    logger.debug("pipeline options 无 %s 字段，跳过（docling 版本差异）", name)
    return False


def _build_converter(ocr: bool) -> Any:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
    from docling.document_converter import DocumentConverter, PdfFormatOption, WordFormatOption

    opts = PdfPipelineOptions()

    _set(opts, "do_ocr", ocr)
    _set(opts, "do_table_structure", True)
    _set(opts, "do_picture_classification", True)   # 产出 predicted_class，图片价值判定依赖它
    _set(opts, "generate_picture_images", True)     # 不显式打开就导不出裁剪图
    _set(opts, "images_scale", IMAGES_SCALE)
    _set(opts, "artifacts_path", DOCLING_ARTIFACTS or None)   # 预下载模型 → 可离线
    # 公式转 LaTeX：每页跑公式模型，代价明显，默认关（需要时用 DOCLING_FORMULA=1）
    _set(opts, "do_formula_enrichment",
         os.getenv("DOCLING_FORMULA", "0").strip().lower() in ("1", "true", "on", "yes"))

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
    """converter 单例（按 OCR 开关分桶缓存）。模型只加载一次，务必复用。"""
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
    errs = getattr(result, "errors", None) or []
    txt = "; ".join(str(getattr(e, "error_message", e)) for e in errs)
    return txt or "无详细信息"


def file_checksum(path: str) -> str:
    """文件内容指纹：源不变 → chunk_id 不变 → 可增量、可缓存。"""
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
    """
    for attr in ("texts", "tables", "pictures"):
        try:
            if len(getattr(doc, attr, None) or []):
                return True
        except Exception:
            continue
    return False


def _chunk_once(doc: Any, chunker: Any, kwarg: str) -> list:
    """跑一次切块；失败只告警不抛，交给调用方的阶梯继续兜底。"""
    try:
        return list(chunker.chunk(**{kwarg: doc}))
    except Exception as exc:
        logger.warning("%s 切块失败（%s）：%s",
                       type(chunker).__name__, type(exc).__name__, exc)
        return []


def to_knowledge_chunks(doc: Any, source: str, *, kind: str = "pdf",
                        page_total: Optional[int] = None,
                        checksum: str = "") -> list:
    """DoclingDocument → list[KnowledgeChunk]，四级阶梯，确保抽到的内容不被丢掉。

    阶梯设计的原因（实测出来的，不是假想）：
      1. **HybridChunker**（默认）—— token 预算 + 结构感知，正常情况都用它。
      2. **HierarchicalChunker** —— 纯结构切，不做 token 合并。当文档只有标题没有正文时，
         第一级会返回 **0 块且不抛异常**（标题开章节，章节里没有 body 就不产出）。
      3. **按 item 逐条兜底** —— 直接遍历文档元素一件一件成块，粒度最好。
      4. **整文档 Markdown 单块** —— 前三级都"挑食"时（实测：全文只有一个 section_header，
         三级全都把标题跳过）保证不变式「文档有内容 → 一定有块」。

    不变式在 parse_document 那边也有对应：只有文档**真的**没抽出东西才判 unprocessable。
    """
    from .knowledge_chunk import (
        from_docling_chunk, from_docling_items, from_docling_markdown,
    )

    origin = "docling_pdf" if kind == "pdf" else "docling_docx"
    kwarg = _chunk_kwarg()

    for chunker in (get_chunker(), get_fallback_chunker()):
        raw = _chunk_once(doc, chunker, kwarg)
        if not raw:
            continue
        out = []
        for ch in raw:
            try:
                out.append(from_docling_chunk(
                    ch, doc, chunker=chunker, source=source, parser="docling",
                    origin=origin, page_total=page_total, file_checksum=checksum,
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
                             page_total=page_total, file_checksum=checksum)
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


def parse_document(path: str, source: str, *, kind: str = "pdf") -> ParseOutcome:
    """解析一份文档 → ParseOutcome。

    status 语义：
      ok            —— 成功，采用 chunks
      partial       —— 部分成功，仍采用，但已记 warning
      unprocessable —— 解析不出内容（多为扫描件且 OCR 未开），产出显式失败记录
      unavailable   —— docling 未装或被配置关闭，调用方应降级
      failed        —— 转换失败，调用方应降级

    kind="docx" 会转给 parse_docx（双路线 A+C）——留这个入口是为了让调用方
    不必记住"哪种格式该调哪个函数"，也避免误用只走 A 的半条链路。
    """
    if kind == "docx":
        return parse_docx(path, source)

    if DOC_PARSER != "docling":
        return ParseOutcome(status="unavailable", reason=f"RAG_DOC_PARSER={DOC_PARSER}")
    if not docling_available():
        return ParseOutcome(status="unavailable", reason="docling 未安装")

    ocr = _want_ocr(path, kind)
    conv = _convert_document(path, ocr)
    if conv.error:
        return ParseOutcome(status="failed", reason=conv.error)

    doc = conv.doc
    pages = getattr(doc, "pages", None) or {}
    page_total = len(pages) or None
    checksum = file_checksum(path)
    chunks = to_knowledge_chunks(doc, source, kind=kind,
                                 page_total=page_total, checksum=checksum)

    if not chunks:
        from .knowledge_chunk import unprocessable
        # 区分"文档真的没抽到东西"与"抽到了但切块没产出"——后者是我们自己的问题，
        # 原因写成"疑似扫描件"会把排查方向带偏（实测就被这样误导过一次）。
        if _doc_has_content(doc):
            why = ("文档已抽到内容但切块未产出任何块（切分器异常，属实现问题，请查日志）")
            logger.error("Docling 抽到内容却切不出块：%s（ocr=%s）", source, ocr)
        else:
            why = ("未取到任何内容（疑似扫描件且未启用 OCR，或版面识别失败）"
                   if kind == "pdf" else "未取到任何内容")
            logger.warning("Docling 未产出内容，标记 unprocessable：%s（ocr=%s, %s）",
                           source, ocr, why)
        return ParseOutcome(chunks=[unprocessable(source, why, parser="docling",
                                                 page_total=page_total)],
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
    """取用于位置对齐的检索文本（正文块与媒体块都走这里，保证两边口径一致）。"""
    return f"{getattr(chunk, 'retrieval_text', '') or ''}\n{getattr(chunk, 'content', '') or ''}"


def _find_anchor_index(texts: list, anchor: str, start: int) -> Optional[int]:
    """在 texts[start:] 里找第一个包含 anchor 的块，找不到返回 None。

    先用整段、再退到前 32/16 字：锚点段落可能被切分器切开，或两端空白不同，
    整段匹配未必命中。**这是尽力而为的对齐**，找不到就由调用方追加到末尾并告警——
    位置不准比"内容丢失"轻得多，绝不能为了对齐把块丢掉。
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
    """媒体块没有 section_path 时，从它前一个正文块继承（标题路径）。"""
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
            c.tags = [t for t in (c.tags or []) if t not in ("图表", "待VLM")]
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
