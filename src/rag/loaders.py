"""多格式文档解析：把各种文件统一转成「带结构的块」，供 RAG 切分与向量化。

设计要点
--------
- 统一出口 `load_blocks(path) -> list[Block]`，按扩展名分派；未知扩展名按纯文本兜底。
- **每个格式自己决定"结构"怎么表达**：表格类格式（csv/xlsx/pptx 表格）把列名写进每一行，
  版式类格式（pptx）用页码，层次类格式（md/html）用标题路径，记录类格式（json/jsonl）
  按记录成块。这不是"统一的启发式"，而是各格式的确定性事实。
- `load_text(path)` = 块文本拼接，保留给旧调用方（兼容，不改变"能读出来"的语义）。
- 编码兜底：中文环境常见的 GBK/GB18030 文本文件也能正确读出来。
- 单个文件解析失败只返回空列表并告警，由调用方（load_chunks）跳过，不中断整批语料。
- 单个文件最多产出 `RAG_MAX_BLOCKS` 块，超出会显式写一条截断标记（不静默丢）。

支持格式
--------
纯文本  .txt .md .markdown .log .rst .text
表格    .csv .tsv
数据    .json .jsonl .ndjson
网页    .html .htm .xhtml
Word    .docx
Excel   .xlsx .xlsm
PPT     .pptx
图片    .png .jpg .jpeg .webp .gif .bmp .tif .tiff（无文字层，靠 VLM/OCR）
PDF     .pdf（由 src/pdf_parser.py + src/docling_loader.py 单独处理，不在此模块）
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# kind 取值与 knowledge_chunk 的 KIND_* 保持一致（那里是权威定义）。
# 这里不 import 它，是为了让 loaders 保持"零内部依赖"的最底层模块地位 ——
# 它是被 docling_loader / docx_media / 测试共同依赖的地方，不能有反向依赖。
KIND_TEXT = "text"
KIND_TABLE = "table"
KIND_CHART = "chart"
KIND_DIAGRAM = "diagram"


def _env_int(name: str, default: int) -> int:
    """读一个整数型环境变量，读不到或格式不对就返回默认值。

    name: 环境变量名（如 "RAG_MAX_BLOCKS"）。
    default: 缺省值。变量不存在、为空串、或不是合法整数时原样返回它。
        **兜底而非报错**是有意的：一个配错的环境变量不该让整条解析链路起不来。
    """
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# 单份文档最多产出多少块（防止异常文件把内存与索引打爆；超出会写明截断）
MAX_BLOCKS = max(50, _env_int("RAG_MAX_BLOCKS", 5000))
# CSV 每块多少行（每块都会**重复表头**，见 _blocks_csv 的说明）
CSV_ROWS_PER_BLOCK = max(1, _env_int("RAG_CSV_ROWS_PER_BLOCK", 50))
# 单个工作表最多读多少行
XLSX_MAX_ROWS = max(10, _env_int("RAG_XLSX_MAX_ROWS", 5000))
# JSON 数组/JSONL 最多产出多少条记录块
JSON_MAX_RECORDS = max(10, _env_int("RAG_JSON_MAX_RECORDS", 2000))


@dataclass
class Block:
    """一份文档里的一段内容 + 它能确定地说出来的结构信息。

    刻意不做"统一元数据"的抽象：`page` 对 pptx 是幻灯片号、对 xlsx 是工作表序号，
    对 md 没有意义；`section` 对 md/html/pptx 是标题路径，对 csv 空着。
    把"这个格式确实能说清的东西"放进来，比强行凑一套字段更诚实。
    """

    text: str
    section: str = ""
    page: int | None = None
    kind: str = KIND_TEXT
    tags: list = field(default_factory=list)
    source_image: str = ""      # 图片类块：落盘后的文件路径
    needs_vlm: bool = False     # 需要（且允许）送 VLM 描述

    def to_dict(self) -> dict:
        return {"text": self.text, "section": self.section, "page": self.page,
                "kind": self.kind, "tags": list(self.tags),
                "source_image": self.source_image, "needs_vlm": self.needs_vlm}


# 各格式的块解析器；缺库时只在真正用到该格式时才报错
_BLOCK_LOADERS: dict[str, Callable[[str], list[Block]]] = {}


def register(*exts):
    """把函数注册为某些扩展名的块解析器（函数需返回 list[Block]）。

    *exts: 一个或多个扩展名字符串，**必须带前导点**（如 ".md"、"*.txt" 里的
        ".txt"）。内部统一转小写后作为 _BLOCK_LOADERS 的键；同一扩展名被重复
        注册时，后注册的覆盖先注册的（模块级顺序决定最终归属）。
    """

    def deco(fn):
        """把 fn 登记到 *exts 的每个扩展名下，并原样返回它（便于继续叠加装饰）。

        fn: 被装饰的函数，签名约定为 fn(path) -> list[Block]。
        """
        for e in exts:
            _BLOCK_LOADERS[e.lower()] = fn
        return fn

    return deco


def supported_extensions() -> set:
    return set(_BLOCK_LOADERS.keys())


def load_blocks(path: str) -> list[Block]:
    """任意受支持格式 → list[Block]。失败返回 [] 并告警。

    path: 待解析文件的路径。扩展名（小写）决定走哪个块解析器；未注册的扩展名
        一律按纯文本处理（_blocks_plain），**不报错**。整体异常被吞掉并降级为
        空列表，以保证单份文件失败不中断整批语料。
    """
    ext = os.path.splitext(path)[1].lower()
    fn = _BLOCK_LOADERS.get(ext)        #拿到对应的块解析器
    try:
        blocks = fn(path) if fn is not None else _blocks_plain(path)        #没有就当纯文本读取
    except Exception as e:  # noqa: BLE001
        logger.warning("解析失败，跳过 %s：%s", os.path.basename(path), e)
        return []
    blocks = [b for b in blocks if (b.text or "").strip() or b.source_image]
    if len(blocks) > MAX_BLOCKS:
        logger.warning("%s 产出块数超过上限 %d（实际 %d），已截断",
                       os.path.basename(path), MAX_BLOCKS, len(blocks))
        blocks = blocks[:MAX_BLOCKS]
        blocks.append(Block(text=f"[已截断：本文件超过 {MAX_BLOCKS} 块，其余未纳入索引]",
                            kind=KIND_TEXT, tags=["截断"]))
    return blocks


def load_text(path: str) -> str:
    """任意受支持格式 → 纯文本（= 各块文本拼接）。失败返回 "" 并告警。

    与历史行为保持一致：过去每个格式各自产出一段文本，现在统一由块拼出来，
    所以"能读出来什么"不变，只是结构信息也一并留在了 load_blocks 里。

    path: 待读取文件的路径。分派与兜底规则与 load_blocks 完全一致（同一套
        扩展名映射），只是把结果拼成一段字符串返回。
    """
    return _blocks_to_text([b.text for b in load_blocks(path)])


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "utf-16", "latin-1")


def _read_text_file(path: str) -> str:
    """按常见编码逐个尝试读取，覆盖中文 Windows 常见的 GBK/GB18030 文件。

    path: 待读取的文本文件路径。以二进制打开后按 _ENCODINGS 顺序试解，第一个
        不抛 UnicodeDecodeError 的编码胜出；全部失败时用 utf-8 + errors="ignore"
        兜底（宁可丢几个无法解码的字符，也不让整份文件读不出来）。
    """
    with open(path, "rb") as _fh:
        raw = _fh.read()
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="ignore")


def _blocks_to_text(blocks) -> str:
    """块级内容用空行分隔（正好对上递归切分器的第一优先级分隔符 \\n\\n）。

    blocks: 任意可迭代的**字符串**序列（注意不是 Block 对象——调用方传的是
        [b.text ...]）。每个元素先 strip，空串丢弃，再用空行连接；传入空序列返回 ""。
    """
    return "\n\n".join(b for b in (x.strip() for x in blocks) if b)


def _cell(v: Any) -> str:
    """单元格值 → 去空白字符串（表格类解析的统一取数口）。

    v: 任意单元格原始值（None / int / float / datetime / str 皆可）。None 归一成
        空串，其余 str() 后 strip —— 这样"空"在本模块里只有一种表示，下游的
        `if v` / `any(...)` 判断不会因 None 与 "" 混用而出错。
    """
    return ("" if v is None else str(v)).strip()


def _row_to_kv(header: list[str], row: list) -> str:
    """一行数据转成 "列=值 | 列=值"，让每个值都能被检索到。

    为什么所有表格类格式都要走这一步：二维网格里"值"的含义由**列位置**承担，
    而列名通常只出现在首行。切开之后，后面那些块里就只剩 `服务器采购 | 50000` ——
    没人知道 50000 是金额还是数量。把列名写进每一行是这个问题的唯一确定性解法。

    header: 表头字符串列表，第 i 个元素作 row 第 i 列的列名。长度允许短于 row，
        不足处回退成 "列{i+1}"（表头缺列时不至于把值丢掉）。
    row: 一行的原始值序列（未转字符串，内部逐格过 _cell）。空值格会被跳过，
        因此返回串里的列数是"非空列数"而非 row 的长度。
    """
    pairs = []
    for i, v in enumerate(row):
        v = _cell(v)
        if not v:
            continue
        key = header[i] if i < len(header) and header[i] else f"列{i + 1}"
        pairs.append(f"{key}={v}")
    return " | ".join(pairs)


def _looks_like_header(row: list[str], next_row: list[str] | None = None) -> bool:
    """判断某一行是不是表头。

    判据刻意保守：**不能**用"全是文本、下一行有数字"这种启发式 ——
    在中文表格里"城市/特产/气候"与"梧州/六堡茶/亚热带"都是文本，猜错就会把数据行当表头，
    整张表的列名全错且不会报错。所以默认 **第一行即表头**（这是 CSV/XLSX 的通行约定），
    只有在该行为空时才向后找第一行非空。

    row: 待判定的行（已转成字符串列表）。
    next_row: 下一行。**当前实现未使用它**，仅保留签名以承接"将来想用上下文判断"
        的扩展点；不传与被传入真实下一行，返回值完全相同。
    """
    return any(c.strip() for c in row)


# ---------------------------------------------------------------------------
# 纯文本 / Markdown
# ---------------------------------------------------------------------------
@register(".txt", ".log", ".rst", ".text")
def _blocks_plain(path) -> list[Block]:
    """纯文本：整篇一个块，切分完全交给下游的递归切分器。

    为什么不再按空行预分段（2026-09-27 改）：
    `\\n\\n` 本来就是递归切分器的**第一优先级分隔符**（`retrieval.py` 的
    `SPLIT_SEPARATORS[0]`），所以按空行预分段**没有提供切分器不知道的信息**，
    却废掉了它的两件事：① 把相邻小段**合并**到接近 `chunk_size` —— `from_blocks`
    对每个 Block 单独调 splitter，而 splitter 只切不并（`split_by_headings` 是
    `splitter.split_text(content) if len(content) > chunk_size else [content]`）；
    ② 跨块 overlap —— 段与段之间完全没有重叠。

    实测（20 段 / 1400 字符 / chunk_size=300 / overlap=50）：
      预分段          → 20 块，平均 68 字符（预算的 23%）
      整篇一个块      →  5 块，平均 278 字符
      直接调 splitter →  5 块，平均 278 字符（与上一行**逐字一致** → 预分段是纯损耗）

    ⚠️ 本函数只填 `text`，其余 6 个 Block 字段全是默认值 —— 这正是"可以整篇交给
    切分器"的前提（没有结构信息可丢）。`.md`（标题路径）/ `.csv`（重复表头）/
    `.xlsx`（工作表号）/ `.pptx`（幻灯片号）的 Block 带**切分器看不见的结构标注**，
    那里必须保留预分段，**不要把这个结论推广过去**。

    另注：CRLF 文件不需要先做换行归一化 —— `\\r\\n\\r\\n` 虽然匹配不上 `\\n\\n`，
    但会退到 `\\n` 优先级断开，且切分器默认 `strip_whitespace=True` 会把 `\\r` 去掉，
    实测结果与 LF 版**逐字一致**。

    path: 纯文本文件路径。编码由 _read_text_file 兜底（GBK/GB18030 也能正确读）；
        内容全为空白时返回 []。
    """
    text = _read_text_file(path)
    if not text.strip():
        return []
    return [Block(text=text.strip())]


_FENCE = re.compile(r"^\s*(```|~~~)")
_FRONT_MATTER = re.compile(r"^---\s*\n.*?\n---\s*\n", re.S)


@register(".md", ".markdown")
def _blocks_markdown(path) -> list[Block]:
    """Markdown：标题路径 + 代码块不可切分 + front-matter 单独成块。

    两个格式特有的坑：
      1. **代码块必须整块保留**。按字符切分会在 `def f():` 和函数体之间断开，
         检索回来的是半截代码 —— 而且断点处看起来"很自然"，没人会发现。
      2. **front-matter（`---` YAML 头）是元数据不是正文**，混进正文会污染检索；
         单独成块并打标签，需要时可按标签取用。

    path: Markdown 文件路径。编码兜底同纯文本。
    """
    text = _read_text_file(path)
    if not text.strip():
        return []

    blocks: list[Block] = []
    m = _FRONT_MATTER.match(text)
    if m:
        head = m.group(0).strip()
        blocks.append(Block(text=head, kind=KIND_TEXT, tags=["front-matter"]))
        text = text[m.end():]

    headings: list[str] = []
    buf: list[str] = []
    fence: str | None = None

    def flush(kind: str = KIND_TEXT, tags=None):
        """把当前缓冲区收口成一块并清空缓冲（区块切分的统一出口）。

        kind: 该块的 kind；正文与代码块都用 KIND_TEXT，靠 tags 区分。
        tags: 该块的标签列表；传 None 视为空列表。**只在 body 非空时才会落块**，
            空缓冲不产生块（避免空白块进索引）。
        """
        body = "\n".join(buf).strip()
        buf.clear()
        if body:
            blocks.append(Block(text=body, section=" > ".join(headings),
                                kind=kind, tags=list(tags or [])))

    for line in text.splitlines():
        fm = _FENCE.match(line)
        if fm:
            marker = fm.group(1)
            if fence is None:
                flush()                       # 代码块前的正文先收口
                fence = marker
                buf.append(line)              # 保留围栏行：语言标记（```python）是有效信息
            elif line.strip().startswith(fence):
                buf.append(line)              # 收尾围栏也保留，块自解释
                flush(KIND_TEXT, ["代码块"])
                fence = None
            else:
                buf.append(line)
            continue

        if fence is not None:
            buf.append(line)
            continue

        hm = re.match(r"^(#{1,6})\s+(.*)$", line)
        if hm:
            flush()
            level, title = len(hm.group(1)), hm.group(2).strip()
            headings = headings[: level - 1] + [title]
            blocks.append(Block(text="# " + title, section=" > ".join(headings),
                                kind=KIND_TEXT, tags=["标题"]))
            continue
        buf.append(line)

    flush(KIND_TEXT, ["代码块"] if fence else None)
    return blocks


# ---------------------------------------------------------------------------
# CSV / TSV
# ---------------------------------------------------------------------------
@register(".csv", ".tsv")
def _blocks_csv(path) -> list[Block]:
    """CSV/TSV：表头 + 逐行 "列=值"，并且**每一块都重复表头**。

    为什么必须重复表头：切分器按大小切，第一块带着表头，第 5 块就只剩数值行了。
    实测过这个后果——查询"金额是多少"召回的往往是不含列名的那几行，
    命中率直接掉一半。重复表头是零成本且确定性的修法。

    path: CSV/TSV 文件路径。`.tsv` 后缀强制用制表符分隔，其余交给 csv.Sniffer
        在 ",;\t|" 里猜（猜不出回退逗号）。
    """
    text = _read_text_file(path)
    if not text.strip():
        return []
    delimiter = "\t" if path.lower().endswith(".tsv") else None
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
        except Exception:
            delimiter = ","

    rows = [[_cell(c) for c in r] for r in csv.reader(io.StringIO(text), delimiter=delimiter)]
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        return []

    header = rows[0]
    header_line = "表头：" + " | ".join(h for h in header if h)
    body = rows[1:] if _looks_like_header(header, rows[1] if len(rows) > 1 else None) else rows

    blocks: list[Block] = []
    chunk: list[str] = []
    for r in body:
        kv = _row_to_kv(header, r)
        if kv:
            chunk.append(kv)
        if len(chunk) >= CSV_ROWS_PER_BLOCK:
            blocks.append(Block(text=header_line + "\n" + "\n".join(chunk),
                                kind=KIND_TABLE, tags=["表格"]))
            chunk = []
    if chunk:
        blocks.append(Block(text=header_line + "\n" + "\n".join(chunk),
                            kind=KIND_TABLE, tags=["表格"]))
    if not blocks:                                  # 只有表头的文件也要留下
        blocks.append(Block(text=header_line, kind=KIND_TABLE, tags=["表格"]))
    return blocks


# ---------------------------------------------------------------------------
# JSON / JSONL
# ---------------------------------------------------------------------------
def _flatten_json(obj, prefix="", out=None):
    """把嵌套 JSON 递归压成 "路径: 值" 的字符串列表（值的位置不再丢失）。

    obj: 当前层对象（dict / list / 标量）。dict 按键展开、list 按 [i] 展开、
        标量落成一条 "prefix: 值"。
    prefix: 当前层的路径前缀（如 "a.b[0]"），根调用留空；由递归逐层拼装，调用方
        通常不必传。
    out: 累积结果的列表。**传入同一个 list 可实现跨层累计**（递归时靠它传递）；
        不传则新建一个，此时返回值才是唯一入口。长度达到 MAX_BLOCKS 即停止展开。
    """
    if out is None:
        out = []
    if len(out) >= MAX_BLOCKS:          # 扁平化本身也要有上限
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten_json(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _flatten_json(v, f"{prefix}[{i}]", out)
    else:
        out.append(f"{prefix}: {obj}" if prefix else str(obj))
    return out


def _record_block(rec: Any, index: int, tags: list) -> Block:
    """一条记录 → 一块。

    为什么 JSON 要按记录成块（而不是整份扁平化成一大段）：
    记录是这个格式**天然的最小语义单位**——一块里通常正好一条记录，
    检索命中后送进 LLM 的上下文是完整的一条，而不是"上一条的尾巴 + 下一条的开头"。

    rec: 一条记录的原始对象（通常是 dict，也可能是标量）。
    index: 该记录在文件中的序号（**0 起**），会写进文本首行 "记录 N"，便于人工
        定位；**不参与任何 id 计算**，所以改它不影响引用。
    tags: 附到块上的标签列表（如 ["记录"]）。内部 list() 复制一份，避免与调用方
        的可变列表共享引用。记录扁平化后为空时不产生块，返回 text="" 的 Block。
    """
    lines = _flatten_json(rec)
    text = f"记录 {index}\n" + "\n".join(lines) if lines else ""
    return Block(text=text, kind=KIND_TEXT, tags=list(tags))


@register(".json")
def _blocks_json(path) -> list[Block]:
    """JSON → 块：顶层是数组则**一条记录一块**，否则整份扁平化成一块。

    记录数超过 JSON_MAX_RECORDS 会截断，并追加一条显式的截断标记块（不静默丢）。

    path: JSON 文件路径。内容不是合法 JSON 时由外层 load_blocks 捕获异常并返回
        空列表——本函数自身不做兜底。
    """
    data = json.loads(_read_text_file(path))
    if isinstance(data, list) and data:
        blocks = [_record_block(rec, i, ["记录"]) for i, rec in enumerate(data)]
    else:
        blocks = [Block(text="\n".join(_flatten_json(data)), tags=["记录"])]
    if len(blocks) > JSON_MAX_RECORDS:
        logger.warning("%s 记录数超过上限 %d，已截断", os.path.basename(path), JSON_MAX_RECORDS)
        blocks = blocks[:JSON_MAX_RECORDS]
        blocks.append(Block(text=f"[已截断：记录数超过 {JSON_MAX_RECORDS}，其余未纳入索引]",
                            tags=["截断"]))
    return blocks


@register(".jsonl", ".ndjson")
def _blocks_jsonl(path) -> list[Block]:
    """JSONL/NDJSON → 块：逐行解析，一行一条记录。

    某行不是合法 JSON 时**按原文保留**并打 "非法JSON行" 标签（宁可多一块，
    也不静默丢内容）——这是与 _blocks_json 的关键差别。

    path: JSONL 文件路径。空行直接跳过；行序号用**原始行号**（跳过空行不影响编号）。
    """
    blocks = []
    for i, line in enumerate(_read_text_file(path).splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            blocks.append(_record_block(json.loads(line), i, ["记录"]))
        except Exception:
            # 不是合法 JSON 就按原文保留：宁可多一块，也不要静默丢掉一行内容
            blocks.append(Block(text=line, tags=["记录", "非法JSON行"]))
    return blocks


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
_HTML_BLOCK_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre",
                    "blockquote", "table"]
# 页面级"家具"：导航、页脚、侧栏、表单。它们的文字会**大量重复**出现在每个页面里，
# 进了索引就会把真正的内容挤下去（同一句话在几十个文档里都命中）。
_HTML_DROP_TAGS = ["script", "style", "noscript", "template", "nav", "footer",
                   "aside", "header", "form", "iframe", "svg", "button"]


@register(".html", ".htm", ".xhtml")
def _blocks_html(path) -> list[Block]:
    """HTML：标题路径 + 正文段落 + **表格按行成块**（这是本格式最容易做错的地方）。

    过去这里把 `td/th` 也当成独立块标签，结果是：一行 5 列被拆成 5 个互不相关的块，
    "城市=梧州" 与 "特产=六堡茶" 天各一方，行关系彻底丢失。
    表格必须**按行**处理，并把表头写进每一行 —— 与 CSV/XLSX 同一套做法。

    path: HTML 文件路径。用 lxml 解析；先按 _HTML_DROP_TAGS 与 ARIA 角色
        （navigation/banner/contentinfo/search）删掉导航、页脚、脚本等页面级家具，
        再依次抽标题、正文与表格。
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(_read_text_file(path), "lxml")
    for tag in soup(_HTML_DROP_TAGS):
        tag.decompose()
    for tag in soup.find_all(attrs={"role": ["navigation", "banner", "contentinfo", "search"]}):
        tag.decompose()

    blocks: list[Block] = []
    title = soup.title.get_text(strip=True) if soup.title else ""
    if title:
        blocks.append(Block(text=f"# {title}", section=title, tags=["标题"]))

    headings: list[str] = []
    for el in soup.find_all(_HTML_BLOCK_TAGS):
        name = el.name.lower()
        if name == "table":
            blocks.extend(_html_table_blocks(el, " > ".join(headings)))
            continue
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if re.fullmatch(r"h[1-6]", name):
            level = int(name[1])
            headings = headings[: level - 1] + [text]
            blocks.append(Block(text="#" * level + " " + text,
                                section=" > ".join(headings), tags=["标题"]))
        else:
            blocks.append(Block(text=text, section=" > ".join(headings)))
    return blocks


def _html_table_blocks(table_el, section: str) -> list[Block]:
    """一个 <table> → 若干"表头 + 行"的块。

    table_el: 一个 BeautifulSoup 的 <table> 元素对象。
    section: 该表所处的标题路径（" > " 连接），直接写进产出块的 section 字段，
        使表格可回溯到它在文档中的位置。表格为空（无任何非空单元格）时返回 []。
    """
    grid: list[list[str]] = []
    for tr in table_el.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        if any(cells):
            grid.append(cells)
    if not grid:
        return []

    # th 行优先当表头；没有 th 就按"第一行是表头"的约定（与 CSV 一致）
    has_th = table_el.find("tr") is not None and bool(table_el.find("tr").find("th"))
    header, body = (grid[0], grid[1:]) if (has_th or len(grid) > 1) else (grid[0], [])
    header_line = "表头：" + " | ".join(h for h in header if h)
    rows = [f"{_row_to_kv(header, r)}" for r in body if any(r)]
    if not rows:
        return [Block(text=header_line, section=section, kind=KIND_TABLE, tags=["表格"])]
    return [Block(text=header_line + "\n" + "\n".join(rows[:CSV_ROWS_PER_BLOCK]),
                  section=section, kind=KIND_TABLE, tags=["表格"])]


# ---------------------------------------------------------------------------
# Word .docx（降级链路用；主链路是 docling 的 A+C 双路线）
# ---------------------------------------------------------------------------
@register(".docx")
def _blocks_docx(path) -> list[Block]:
    """Word 降级链路：python-docx 读段落（按 Heading 样式建标题路径）+ 表格成块。

    主链路的 docx 走 docling（A+C 双路线），本函数只在 docling 不可用时兜底，
    因此能力刻意做薄：够把正文与表格读出来即可，不追求版式还原。

    path: .docx 文件路径。标题级别从样式名 "Heading N" 解析并夹到 1–6 之间。
    """
    from docx import Document

    doc = Document(path)
    blocks: list[Block] = []
    headings: list[str] = []
    for p in doc.paragraphs:
        text = (p.text or "").strip()
        if not text:
            continue
        style = ""
        try:
            style = p.style.name or ""
        except Exception:
            pass
        m = re.match(r"Heading\s*(\d+)", style, re.IGNORECASE)
        if m:
            level = min(max(int(m.group(1)), 1), 6)
            headings = headings[: level - 1] + [text]
            blocks.append(Block(text="#" * level + " " + text,
                                section=" > ".join(headings), tags=["标题"]))
        else:
            blocks.append(Block(text=text, section=" > ".join(headings)))

    for i, table in enumerate(doc.tables, start=1):
        grid = [[_cell(c.text) for c in row.cells] for row in table.rows]
        grid = [r for r in grid if any(r)]
        if not grid:
            continue
        header_line = "表头：" + " | ".join(h for h in grid[0] if h)
        rows = [_row_to_kv(grid[0], r) for r in grid[1:] if any(r)] or [" ".join(grid[0])]
        blocks.append(Block(text=f"[表格 {i}]\n{header_line}\n" + "\n".join(rows),
                            section=" > ".join(headings), kind=KIND_TABLE, tags=["表格"]))
    return blocks


# ---------------------------------------------------------------------------
# Excel .xlsx
# ---------------------------------------------------------------------------
@register(".xlsx", ".xlsm")
def _blocks_xlsx(path) -> list[Block]:
    """Excel：每个工作表独立成"节"，先填合并单元格，再把每行写成 "列=值"。

    本格式比 Word/PDF 多出来的四个问题（都不是"更多努力"能绕开的，是格式性质）：

      1. **值的位置即语义**。单元格自己不带列名，只有"列位置"知道 50000 是金额还是数量。
         所以必须把首行当表头并写进每一行 —— 与 CSV 同理，但这里还要处理多工作表。
      2. **合并单元格只在左上角存值**，其余为 None。不填充的话，
         "广西壮族自治区" 这类跨列标题在数据行里会凭空消失（数据看着完整，其实缺了）。
      3. **公式的缓存值可能压根不存在**。openpyxl 的 `data_only=True` 读的是**最后一次由
         Excel/WPS 计算并保存**的结果；机器生成、从未被 Office 打开过的 xlsx 里全是 None。
         表现是"文件明明有内容，解析出来是空的"——必须显式告诉人，不能当成空文件。
      4. **一个文件里有多个独立文档**（多工作表），甚至还有隐藏表、图表、图片。
         Word/PDF 是单一线性流，这里必须自己决定"哪些表要，按什么顺序"。

    path: .xlsx / .xlsm 文件路径。用 openpyxl 且 `data_only=True` 读**缓存值**——
        这直接决定了上面第 3 条（未计算过公式的文件读出来是空的）。
    """
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=False, data_only=True)
    blocks: list[Block] = []
    try:
        sheets = [ws for ws in wb.worksheets if ws.sheet_state == "visible"]
        hidden = [ws.title for ws in wb.worksheets if ws.sheet_state != "visible"]
        if hidden:
            logger.info("%s 有 %d 个隐藏工作表未索引：%s",
                        os.path.basename(path), len(hidden), "、".join(hidden))

        for idx, ws in enumerate(sheets, start=1):
            section = f"工作表：{ws.title}"
            rows = _sheet_rows(ws)
            if not rows:
                continue
            header, body = rows[0], rows[1:]
            if not any(header):
                continue
            header_line = "表头：" + " | ".join(h for h in header if h)
            lines = [f"{_row_to_kv(header, r)}" for r in body if any(r)]
            if len(lines) > XLSX_MAX_ROWS:
                logger.warning("%s 的 %s 超过 %d 行，已截断",
                               os.path.basename(path), ws.title, XLSX_MAX_ROWS)
                lines = lines[:XLSX_MAX_ROWS] + [f"[已截断：超过 {XLSX_MAX_ROWS} 行]"]
            # 工作表名既进 section，也**写进文本**：检索只嵌入 text，
            # 只放 section 的话"特产表里有什么"这类指向表名的问题召不回来。
            blocks.append(Block(
                text=f"## 工作表：{ws.title}\n{header_line}"
                     + ("\n" + "\n".join(lines) if lines else ""),
                section=section, page=idx, kind=KIND_TABLE, tags=["表格"]))

        if not blocks:
            # 走到这里通常是"有内容但读不到值"，最常见的原因就是公式没被计算过。
            # 这是本格式独有的陷阱：**不报错、不抛异常，看起来就是一份空文件**。
            blocks.append(Block(
                text=f"[未读到任何单元格值] {os.path.basename(path)} —— "
                     "若该文件含公式，请用 Excel/WPS 打开并保存一次（openpyxl 只能读到已计算的缓存值）；"
                     "也可能是所有工作表都是空的。",
                kind=KIND_TEXT, tags=["无可读内容"]))
    finally:
        wb.close()

    # 图表：xlsx 里的图表数据同样是 chart XML（与 Word 完全同一种格式），
    # 读出来就是确定性数值，不需要也不应该送 VLM。
    blocks.extend(_xlsx_chart_blocks(path, wb))
    return blocks


def _xlsx_chart_blocks(path: str, wb) -> list[Block]:
    """xlsx 图表 → 块。**缓存缺失时回表读引用**。

    本格式独有的坑：`c:numCache`（图表自带的数值缓存）不是必须的 ——
    openpyxl 生成的图表只写 `c:f`（如 `Sheet1!$B$2:$B$3`）而不写缓存。
    此时"图表没数据"是假象：**数据就在工作表里**。
    所以按引用回表取一次，把它变成确定性数值 —— 这比"读不到就算了"或者"送 VLM 看图"都正确。

    path: 源 xlsx 路径（用于日志与定位同目录的 docx_media）。
    wb: **已打开的、与 path 同一份**的 openpyxl workbook 对象；回表取引用数值需要
        它，所以调用方必须在 `wb.close()` 之前调用本函数。取图表失败时返回 []
        并告警，绝不影响正文。
    """
    try:
        from .docx_media import chart_to_table, chart_to_text, extract_ooxml_charts

        blocks = []
        for item in extract_ooxml_charts(path):
            chart = _resolve_chart_refs(item["chart"], wb)
            md, natural = chart_to_table(chart), chart_to_text(chart)
            if not md:
                continue
            blocks.append(Block(text=f"{md}\n{natural}".strip(), kind=KIND_CHART,
                                tags=["图表", "来自XML"]))
        return blocks
    except Exception as exc:  # noqa: BLE001  取不到图表不该影响正文
        logger.warning("图表提取失败（不影响正文）：%s（%s）", os.path.basename(path), exc)
        return []


_RANGE_REF = re.compile(r"^(?:'([^']+)'|([^!]+))!\$?([A-Z]+)\$?(\d+)(?::\$?([A-Z]+)\$?(\d+))?$")


def _resolve_chart_refs(chart: dict, wb) -> dict:
    """给缺缓存值的系列回表取值（引用形如 `'明细'!$B$2:$B$3`）。

    chart: extract_ooxml_charts 产出的 chart dict（含 "series" 列表）。**原地修改
        并返回同一对象**；chart 为空或无 series 时原样返回。回表仍取不到值时，
        会把 chart["error"] 置为说明文字（下游据此决定是否退 VLM）。
    wb: 已打开的 openpyxl workbook，用于按 `Sheet1!$B$2:$B$3` 这类引用读单元格值。
    """
    if not chart or not chart.get("series"):
        return chart
    for s in chart["series"]:
        if not s.get("name") and s.get("name_ref"):
            names = _values_from_ref(wb, s["name_ref"])
            if names:
                s["name"] = names[0]
        if s.get("values"):
            continue
        vals = _values_from_ref(wb, s.get("val_ref") or "")
        cats = _values_from_ref(wb, s.get("cat_ref") or "")
        if vals:
            s["values"] = vals
            if cats:
                s["categories"] = cats
    series = [s for s in chart["series"] if s.get("values") or s.get("name")]
    chart["series"] = series
    if not series:
        chart["error"] = "图表只有引用、且回表也没取到值"
    else:
        for s in series:
            if s.get("categories"):
                chart["categories"] = s["categories"]
                break
        chart["error"] = ""
    return chart


def _values_from_ref(wb, ref: str) -> list[str]:
    """`Sheet1!$B$2:$B$3` → ["120", "145"]。解析不出就返回空。

    wb: 已打开的 openpyxl workbook，按工作表名取 cell 的缓存值。
    ref: Excel 区域引用串。仅支持"单工作表 + 连续区"的形式（A1、A1:B3、'明细'!$A$1），
        解析失败、工作表不存在、或区域内全空时一律返回 []（空列表代表"没取到"，
        调用方据此决定是否保留原引用）。
    """
    m = _RANGE_REF.match((ref or "").strip())
    if not m:
        return []
    sheet_name = m.group(1) or m.group(2) or ""
    try:
        ws = wb[sheet_name]
    except Exception:
        return []
    start_row, end_row = int(m.group(4)), int(m.group(6) or m.group(4))
    cols = _col_span(m.group(3), m.group(5) or m.group(3))
    out = []
    for r in range(start_row, end_row + 1):
        for c in cols:
            v = _cell(ws.cell(row=r, column=c).value)
            if v:
                out.append(v)
    return out


def _col_span(first: str, last: str) -> list[int]:
    """列字母区间 → 1 起的列号列表（闭区间）。

    first: 起始列字母（如 "B"）。
    last: 结束列字母（如 "D"）；与 first 顺序颠倒也能正确处理（内部取 min/max）。

    返回值的列号是 **openpyxl 的 1 起口径**（A=1），与 `ws.cell(column=...)` 对齐。
    """
    def idx(s: str) -> int:
        """列字母 → 1 起的列号（A→1、Z→26、AA→27）。

        s: 列字母串，需为大写；空串返回 0（调用处不会出现该情形）。
        """
        n = 0
        for ch in s:
            n = n * 26 + (ord(ch) - ord("A") + 1)
        return n
    a, b = idx(first), idx(last)
    return list(range(min(a, b), max(a, b) + 1))


def _sheet_rows(ws) -> list[list[str]]:
    """工作表 → 字符串网格，并**填充合并单元格**。

    填充方向很关键，实测踩过一次：
      - **向下填**（跨行合并）：同一个标签适用于下面几行，必须填，否则数据行里标签消失；
      - **只在数据行横向填**（跨列合并）：值铺到该行被合并的各列 —— 这才是"合并"的语义；
      - **表头行不横向填**：否则 `A1:B1` 合并的标题会把列名变成两个一样的
        （实测后果：`采购明细=服务器 | 采购明细=3`，列名全错且不报错）。

    ws: 一个 openpyxl 工作表对象。读值用 `values_only=True`；随后按 merged_cells
        填充合并区、删掉全空行与尾部整列皆空的"幽灵列"。返回的网格每格都是去空白
        字符串（经 _cell）。
    """
    grid: list[list[str]] = [[_cell(v) for v in row]
                             for row in ws.iter_rows(values_only=True)]
    if hasattr(ws, "merged_cells"):
        for rng in list(ws.merged_cells.ranges):
            try:
                top_left = grid[rng.min_row - 1][rng.min_col - 1]
            except Exception:
                continue
            if not top_left:
                continue
            for r in range(rng.min_row - 1, min(rng.max_row, len(grid))):
                for c in range(rng.min_col - 1, min(rng.max_col, len(grid[r]))):
                    # 表头行（grid 第 0 行）横向不填：否则跨列标题会把列名复制成两份。
                    # 注意判断的是"网格首行"，不是"合并区首行" —— 数据行自己跨列合并
                    # （A2:B2="采购"）时该行仍要铺开，否则标签就丢了。
                    if r == 0 and c > rng.min_col - 1:
                        continue
                    if not grid[r][c]:
                        grid[r][c] = top_left
    grid = [r for r in grid if any(r)]
    # 去掉尾部整列皆空的列（Excel 常有"幽灵列"，会让每行多出一堆 列N=）
    while grid and all(not row[-1] for row in grid):
        for row in grid:
            row.pop()
    return grid


# ---------------------------------------------------------------------------
# PowerPoint .pptx
# ---------------------------------------------------------------------------
@register(".pptx")
def _blocks_pptx(path) -> list[Block]:
    """PPT：**一页 = 一块**（页就是本格式的天然切分单位），并把演讲者备注单独成块。

    本格式比 Word/PDF 多出来的四个问题：

      1. **阅读顺序不可靠**。文本框是按形状创建顺序返回的，不是视觉顺序；
         版面复杂的页面里，标题可能排在正文之后。所以"页面级成块"比"逐框成块"更稳：
         一页内的所有文字放在一起，顺序问题就不会割裂语义。
      2. **备注（speaker notes）藏着真正的解释**。正文常常只有几个词，讲解在备注里。
         以前完全没读（python-pptx 的 `notes_slide`），等于把信息量最大的一块丢了。
      3. **没有页码概念但有页序**：页序即 `page`，这是本格式唯一能确定的位置信息。
      4. **表格/图表/图片都是嵌进包里的对象**（本格式也是 zip）：
         表格要按行+表头处理，图表数据在 `ppt/charts/chart*.xml`（与 Word 同一种格式），
         图片在 `ppt/media/`。这些 Docling 一个都给不了。

    path: .pptx 文件路径。页序从 1 起（`page` 字段），图表/图片块通过同包的
        docx_media 提取（取不到时只影响图表，正文照常解析）。
    """
    from pptx import Presentation

    prs = Presentation(path)
    blocks: list[Block] = []
    media_by_page = _pptx_media_index(path)     # {页码: [媒体字典]}，见 docx_media

    for page, slide in enumerate(prs.slides, start=1):
        title = ""
        try:
            if slide.shapes.title is not None:
                title = (slide.shapes.title.text or "").strip()
        except Exception:
            title = ""
        section = title

        texts: list[str] = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                t = (shape.text_frame.text or "").strip()
                if t:
                    texts.append(t)
            if getattr(shape, "has_table", False):
                texts.append(_pptx_table_text(shape.table))
        if texts:
            # 页号同样既进 page、也写进文本：检索只嵌入 text，
            # 少了这行，"第 3 页讲了什么"这类问题就没有落脚点。
            blocks.append(Block(text=f"## 第 {page} 页\n" + "\n".join(texts),
                                section=section, page=page, tags=["幻灯片"]))

        # 备注单独成块：它是"讲解"，不是"页面内容"，混在一起会让检索分不清
        notes = _pptx_notes(slide)
        if notes:
            blocks.append(Block(text=f"[演讲者备注 · 第 {page} 页]\n{notes}",
                                section=section, page=page,
                                kind=KIND_TEXT, tags=["备注"]))

        for asset in media_by_page.get(page, []):
            if asset.get("chart"):
                chart = asset["chart"]
                from .docx_media import chart_to_table, chart_to_text
                md, natural = chart_to_table(chart), chart_to_text(chart)
                blocks.append(Block(
                    text=f"{asset.get('alt_text') or ''}\n\n{md}".strip(),
                    section=section, page=page, kind=KIND_CHART,
                    tags=["图表", "来自XML"]))
                if natural:                     # 让"2021 年销售额多少"这类问句能命中
                    blocks[-1].text = f"{blocks[-1].text}\n{natural}"
            elif asset.get("saved_path"):
                alt = (asset.get("alt_text") or "").strip()
                blocks.append(Block(
                    text=alt or "[幻灯片图片（无替代文字）]",
                    section=section, page=page, kind=KIND_DIAGRAM,
                    tags=["图片", "有替代文字" if alt else "待VLM"],
                    source_image=asset["saved_path"], needs_vlm=not alt))
    return blocks


def _pptx_table_text(table) -> str:
    """PowerPoint 表格 → 表头 + "列=值" 行（与 CSV/Excel 同一套表示）。

    table: python-pptx 的表格对象（即 `shape.table`）。空表返回 ""；首行按约定
        当表头（PPT 表格无 th 概念）。
    """
    grid = []
    for row in table.rows:
        cells = [_cell(c.text) for c in row.cells]
        if any(cells):
            grid.append(cells)
    if not grid:
        return ""
    header_line = "表头：" + " | ".join(h for h in grid[0] if h)
    rows = [_row_to_kv(grid[0], r) for r in grid[1:] if any(r)]
    return "\n".join([header_line] + rows)


def _pptx_notes(slide) -> str:
    """取一张幻灯片的演讲者备注文字；没有备注或对象损坏时返回 ""。

    slide: python-pptx 的 slide 对象。内部对 `has_notes_slide` 与 `notes_text_frame`
        均做判空与异常兜底（损坏的 pptx 常取不到备注），因此本函数不抛异常。
    """
    try:
        if not slide.has_notes_slide:
            return ""
        tf = slide.notes_slide.notes_text_frame
        return (tf.text or "").strip() if tf is not None else ""
    except Exception as exc:  # noqa: BLE001  损坏的 pptx 可能取不到备注
        logger.debug("读取幻灯片备注失败：%s", exc)
        return ""


# ---------------------------------------------------------------------------
# 图片文件（无文字层，必须靠 OCR/VLM —— 这是本格式唯一的、也是根本的不同）
# ---------------------------------------------------------------------------
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")


@register(*IMAGE_EXTS)
def _blocks_image(path) -> list[Block]:
    """独立图片文件 → 一个"有图无文"的块，交给 VLM 环节补描述。

    为什么这样做而不是在这里直接调 VLM：产物结构里 `needs_vlm` + `source_image`
    已经把这层表达清楚了（批次 6），让解析层再去调模型会破坏"解析层不碰模型"的分层，
    也绕过了开关、上限、缓存与失败可见性。
    **没开 VLM 时它会以 needs_vlm=True 的姿态留在索引里**：检索不到内容，但你能按标签
    把它筛出来，知道"这些图还没被描述" —— 这比静默跳过好。

    path: 图片文件路径。会**原样**作为块的 `source_image` 落进产物（下游据此读图 /
        送 VLM），所以传入的应是可再次访问的稳定路径。
    """
    return [Block(text=f"[图片文件] {os.path.basename(path)}",
                  kind=KIND_DIAGRAM, tags=["图片", "待VLM"],
                  source_image=path, needs_vlm=True)]


def _ooxml_chart_blocks(path: str) -> list[Block]:
    """从 xlsx / pptx 包里取图表（`chart XML` 与 Word 是同一种格式）。

    path: xlsx 或 pptx 文件路径（二者都是 zip 包，图表 XML 同格式）。取不到图表时
        返回 [] 并告警，**不影响正文解析**。
    """
    try:
        from .docx_media import chart_to_table, chart_to_text, extract_ooxml_charts

        blocks = []
        for item in extract_ooxml_charts(path):
            chart = item["chart"]
            md, natural = chart_to_table(chart), chart_to_text(chart)
            if not md:
                continue
            text = f"{item.get('alt_text') or ''}\n\n{md}\n{natural}".strip()
            blocks.append(Block(text=text, kind=KIND_CHART,
                                page=item.get("page"), tags=["图表", "来自XML"]))
        return blocks
    except Exception as exc:  # noqa: BLE001  取不到图表不该影响正文
        logger.warning("图表提取失败（不影响正文）：%s（%s）", os.path.basename(path), exc)
        return []


def _pptx_media_index(path: str) -> dict:
    """{幻灯片号: [媒体资产]}。取不到就返回空（正文照常解析）。

    path: .pptx 文件路径。媒体资产由 docx_media.extract_pptx_media 提取；无 `page`
        字段的资产归到键 0（不丢资产，只是无法定位到页）。
    """
    try:
        from .docx_media import extract_pptx_media

        out: dict[int, list] = {}
        for a in extract_pptx_media(path):
            out.setdefault(a.get("page") or 0, []).append(a)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("PPT 媒体提取失败（不影响正文）：%s（%s）", os.path.basename(path), exc)
        return {}
