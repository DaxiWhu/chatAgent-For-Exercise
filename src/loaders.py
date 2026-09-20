"""多格式文档解析：把各种文件统一转成「带结构的纯文本」，供 RAG 切分与向量化。

设计要点
--------
- 统一出口 `load_text(path) -> str`，按扩展名分派；未知扩展名按纯文本兜底读取。
- 输出尽量保留**结构信息**（标题写成 Markdown `#`、表格保留行列），
  这样后续换成结构感知切分器时可以直接用，不必再改解析层。
- 编码兜底：中文环境常见的 GBK/GB18030 文本文件也能正确读出来。
- 单个文件解析失败只返回空字符串并告警，由调用方（load_chunks）跳过，不中断整批语料。

支持格式
--------
纯文本  .txt .md .markdown .log .rst .text
表格    .csv .tsv
数据    .json .jsonl
网页    .html .htm
Word    .docx
Excel   .xlsx .xlsm
PPT     .pptx
PDF     .pdf（由 src/pdf_parser.py 单独处理，不在此模块）
"""

import os
import csv
import io
import json
import logging
import re

# 各格式的依赖库；缺库时只在真正用到该格式时才报错
_LOADERS = {}


def register(*exts):
    """把函数注册为某些扩展名的解析器。"""

    def deco(fn):
        for e in exts:
            _LOADERS[e.lower()] = fn
        return fn

    return deco


def supported_extensions() -> set:
    return set(_LOADERS.keys())


def load_text(path: str) -> str:
    """任意受支持格式 → 纯文本。失败返回 "" 并告警。"""
    ext = os.path.splitext(path)[1].lower()
    fn = _LOADERS.get(ext)
    try:
        if fn is None:
            # 未知扩展名：按文本兜底（很多 .conf/.yml/.sql 之类其实都是文本）
            return _read_text_file(path)
        return fn(path)
    except Exception as e:  # noqa: BLE001
        logging.warning("解析失败，跳过 %s：%s", os.path.basename(path), e)
        return ""


# ---------------------------------------------------------------------------
# 基础：带编码兜底的文本读取
# ---------------------------------------------------------------------------
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "utf-16", "latin-1")


def _read_text_file(path: str) -> str:
    """按常见编码逐个尝试读取，覆盖中文 Windows 常见的 GBK/GB18030 文件。"""
    raw = open(path, "rb").read()
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="ignore")


def _blocks_to_text(blocks) -> str:
    """块级内容用空行分隔（正好对上递归切分器的第一优先级分隔符 \\n\\n）。"""
    return "\n\n".join(b for b in (x.strip() for x in blocks) if b)


def _row_to_kv(header, row) -> str:
    """一行数据转成 "列=值 | 列=值"，让每个值都能被检索到。"""
    pairs = []
    for i, v in enumerate(row):
        v = (v or "").strip() if isinstance(v, str) else str(v)
        if not v:
            continue
        key = header[i] if i < len(header) and header[i] else f"列{i + 1}"
        pairs.append(f"{key}={v}")
    return " | ".join(pairs)


# ---------------------------------------------------------------------------
# 纯文本
# ---------------------------------------------------------------------------
@register(".txt", ".md", ".markdown", ".log", ".rst", ".text")
def _load_plain(path):
    return _read_text_file(path)


# ---------------------------------------------------------------------------
# CSV / TSV
# ---------------------------------------------------------------------------
@register(".csv", ".tsv")
def _load_csv(path):
    text = _read_text_file(path)
    if not text.strip():
        return ""
    delimiter = "\t" if path.lower().endswith(".tsv") else None
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
        except Exception:
            delimiter = ","

    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return ""

    header = [(c or "").strip() for c in rows[0]]
    lines = [f"表头：{' | '.join(h for h in header if h)}"]
    for r in rows[1:]:
        kv = _row_to_kv(header, r)
        if kv:
            lines.append(kv)
    return _blocks_to_text(["\n".join(lines)])


# ---------------------------------------------------------------------------
# JSON / JSONL
# ---------------------------------------------------------------------------
_MAX_JSON_LINES = 20000


def _flatten_json(obj, prefix="", out=None):
    if out is None:
        out = []
    if len(out) >= _MAX_JSON_LINES:
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


@register(".json")
def _load_json(path):
    data = json.loads(_read_text_file(path))
    return _blocks_to_text(["\n".join(_flatten_json(data))])


@register(".jsonl", ".ndjson")
def _load_jsonl(path):
    blocks = []
    for line in _read_text_file(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            blocks.append("\n".join(_flatten_json(json.loads(line))))
        except Exception:
            blocks.append(line)  # 不是合法 JSON 就按原文保留
    return _blocks_to_text(blocks)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
_HTML_BLOCK_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre",
                    "blockquote", "td", "th"]


@register(".html", ".htm", ".xhtml")
def _load_html(path):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(_read_text_file(path), "lxml")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()

    blocks = []
    title = soup.title.get_text(strip=True) if soup.title else ""
    if title:
        blocks.append(f"# {title}")

    for el in soup.find_all(_HTML_BLOCK_TAGS):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        name = el.name.lower()
        if name.startswith("h") and len(name) == 2 and name[1].isdigit():
            blocks.append("#" * int(name[1]) + " " + text)
        else:
            blocks.append(text)

    if not blocks:  # 没有块级元素时退化为全文
        blocks = [soup.get_text("\n", strip=True)]
    return _blocks_to_text(blocks)


# ---------------------------------------------------------------------------
# Word .docx
# ---------------------------------------------------------------------------
@register(".docx")
def _load_docx(path):
    from docx import Document

    doc = Document(path)
    blocks = []

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
            blocks.append("#" * level + " " + text)   # 标题 → Markdown，保住层级
        else:
            blocks.append(text)

    for i, table in enumerate(doc.tables, start=1):
        rows = []
        for row in table.rows:
            cells = [(c.text or "").strip().replace("\n", " ") for c in row.cells]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            blocks.append(f"[表格 {i}]\n" + "\n".join(rows))

    return _blocks_to_text(blocks)


# ---------------------------------------------------------------------------
# Excel .xlsx
# ---------------------------------------------------------------------------
@register(".xlsx", ".xlsm")
def _load_xlsx(path, max_rows=5000):
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    blocks = []
    try:
        for ws in wb.worksheets:
            lines = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= max_rows:
                    lines.append(f"...（超过 {max_rows} 行，已截断）")
                    break
                cells = [("" if v is None else str(v)).strip() for v in row]
                if any(cells):
                    lines.append(" | ".join(cells))
            if lines:
                blocks.append(f"## 工作表：{ws.title}\n" + "\n".join(lines))
    finally:
        wb.close()
    return _blocks_to_text(blocks)


# ---------------------------------------------------------------------------
# PowerPoint .pptx
# ---------------------------------------------------------------------------
@register(".pptx")
def _load_pptx(path):
    from pptx import Presentation

    prs = Presentation(path)
    blocks = []
    for i, slide in enumerate(prs.slides, start=1):
        texts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t:
                    texts.append(t)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [(c.text or "").strip().replace("\n", " ") for c in row.cells]
                    if any(cells):
                        texts.append(" | ".join(cells))
        if texts:
            blocks.append(f"## 第 {i} 页\n" + "\n".join(texts))
    return _blocks_to_text(blocks)


# ---------------------------------------------------------------------------
# 其它：按纯文本兜底（.yml/.yaml/.sql/.ini/.conf 等）
# ---------------------------------------------------------------------------
for _ext in (".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".sql", ".py", ".js"):
    _LOADERS.setdefault(_ext, _load_plain)
