"""docx 路线 C：直接解 docx 包，取 Docling 拿不到的那一半（图片原件 / OLE / 图表原始数据）。

为什么必须有这个模块（PLAN_doc_parsing_v3.md §2.1 实测结论）：
  - Word **原生图表**：本质是 `word/charts/chart*.xml` + 内嵌 xlsx，python-docx 层面
    根本没有渲染图，Docling 也拿不到 → 必须自己读 XML。
  - **OLE 对象**：Docling 完全忽略，输出 JSON 里连引用都没有。
  - **截图/位图**：Docling 在新版 Word 生成的文件上时有时无，不可依赖。
  - **位置**：docx 是流式格式，没有页码/bbox，位置只能用"文档流第几段之后"表达。

设计原则是**确定性优先，绝不猜**：
  图表里"类型 / 标题 / 类目 / 数值序列"本来就有 XML 里的确定答案，
  拿 VLM 看图反而会引入生成式误差（模型会"编辑"而非仅转写）。
  所以顺序永远是「先读 XML → 读不到才标 needs_vlm 交给批次6 的 VLM」。

只依赖标准库（zipfile + ElementTree），不 import docling、不 import python-docx：
本模块在"没装 docling"的环境里也必须能跑，否则降级链路的 docx 支路会一起废掉。

命名空间策略与 docling_loader 一致 —— **先探测再使用**：Strict OOXML 与 Transitional
OOXML 的命名空间 URI 不同，甚至同一台机器上不同工具生成的 docx 也可能不一样，
因此一律按 (命名空间集合, local-name) 双条件匹配，不硬编码单一 URI。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import zipfile
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 自己加载 .env，不依赖调用方先 import 了别的模块（同 docling_loader 的教训：
# 配置只在某个模块里加载，会让"结果取决于 import 顺序"这种问题极难排查）。
try:
    from dotenv import load_dotenv

    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(dotenv_path=os.path.join(_ROOT, ".env"))
except Exception:  # noqa: BLE001  （dotenv 是可选依赖）
    pass

# 媒体导出根目录。默认 data/.media（已在 .gitignore 里）。
MEDIA_DIR = os.getenv("RAG_MEDIA_DIR", "data/.media").strip() or "data/.media"
# 单个 docx 最多导出多少个媒体文件，防止异常文件把磁盘写满
MAX_ASSETS = int(os.getenv("RAG_DOCX_MAX_ASSETS", "300") or 300)
# OLE 内层正文摘要的截断长度（内层可能是几十页的表格，全灌进索引会污染召回）
OLE_TEXT_LIMIT = int(os.getenv("RAG_DOCX_OLE_TEXT_LIMIT", "4000") or 4000)

# ---- 命名空间集合（Transitional + Strict 两种都要认） ----
_W = {
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
}
_R = {
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
}
_A = {
    "http://schemas.openxmlformats.org/drawingml/2006/main",
    "http://purl.oclc.org/ooxml/drawingml/main",
}
_C = {
    "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "http://purl.oclc.org/ooxml/drawingml/chart",
}

_MAIN_DOC = "word/document.xml"
_DOC_RELS = "word/_rels/document.xml.rels"

# 图表类型 → 中文名（同时用于给 VLM 兜底时的提示，以及给人看的日志）
CHART_TYPE_CN = {
    "lineChart": "折线图", "line3DChart": "三维折线图",
    "barChart": "柱状图", "bar3DChart": "三维柱状图",
    "pieChart": "饼图", "pie3DChart": "三维饼图", "ofPieChart": "复合饼图",
    "doughnutChart": "圆环图", "areaChart": "面积图", "area3DChart": "三维面积图",
    "scatterChart": "散点图", "bubbleChart": "气泡图", "radarChart": "雷达图",
    "surfaceChart": "曲面图", "surface3DChart": "三维曲面图",
    "stockChart": "股价图",
}
_PLOT_KINDS = tuple(CHART_TYPE_CN.keys())

# OLE 容器魔数
_MAGIC_ZIP = b"PK\x03\x04"
_MAGIC_CFB = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# OLE 内层文件类型 → 扩展名（拿到原件后用 loaders 能直接解析的那些）
_INNER_EXT = {
    "xl/": ".xlsx",          # 内层是 xlsx 包时的典型首条目
    "ppt/": ".pptx",
    "word/": ".docx",
}

# ProgID → 人话（只列常见的，认不出就原样打印 ProgID）
PROG_ID_CN = {
    "excel.sheet.12": "嵌入的 Excel 工作表",
    "excel.sheet.8": "嵌入的 Excel 工作表（旧版）",
    "excel.chart.12": "嵌入的 Excel 图表",
    "word.document.12": "嵌入的 Word 文档",
    "word.document.8": "嵌入的 Word 文档（旧版）",
    "powerpoint.show.12": "嵌入的 PowerPoint 演示文稿",
    "equation.3": "公式编辑器对象",
    "equation.3.0": "公式编辑器对象",
    "visio.drawing.11": "嵌入的 Visio 图",
    "package": "通用嵌入包",
}


@dataclass
class DocxAsset:
    """docx 包里一个媒体元素的确定性描述。

    位置信息刻意拆成三个字段，因为它们的可靠程度不同：
      para_index   —— 文档流里的第几段（0 起），精确但只在本文档内有意义
      para_text    —— 该元素所在段落自身的文字（图表/图片通常在无文字段落里，常为空）
      anchor_text  —— **上下文锚点**：本段文字，为空则回退到上文最近的非空段落文字。
                      这是给检索用的：图片描述要能"挂在"它前后文上才有召回味。
    """

    kind: str                     # image | ole | chart
    rel_id: str = ""
    part: str = ""                # zip 内的 part 路径，如 word/media/image1.png
    filename: str = ""
    saved_path: str = ""          # 导出到本地的路径（导出失败为空）
    para_index: int = 0
    para_text: str = ""
    anchor_text: str = ""
    alt_text: str = ""            # 作者填的替代文字（docPr@descr，确定性，不用 VLM）
    prog_id: str = ""
    size: int = 0
    chart: Optional[dict] = None  # kind == chart 时的确定性数据（见 parse_chart_xml）
    inner_ext: str = ""           # OLE 解包后内层文件的扩展名（如 .xlsx）
    ole_text: str = ""            # OLE 内层是可解析格式时抽出的正文摘要
    is_ole_preview: bool = False  # 本"图片"其实是同段落 OLE 对象的预览位图（见 _mark_ole_previews）
    form: str = ""                # 引用它的 XML 元素名（blip / imagedata / OLEObject / chart）
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# XML 小工具：一律按 (命名空间集合, local-name) 匹配
# ---------------------------------------------------------------------------
def _split_tag(tag: Any) -> tuple[str, str]:
    if not isinstance(tag, str):
        return "", ""
    if tag.startswith("{"):
        ns, _, local = tag[1:].partition("}")
        return ns, local
    return "", tag


def _local(tag: Any) -> str:
    return _split_tag(tag)[1]


def _in(ns: str, group: set[str]) -> bool:
    return ns in group


def _children(el: Any, local: str) -> list:
    return [c for c in list(el) if _local(c.tag) == local]


def _child(el: Any, local: str):
    for c in list(el):
        if _local(c.tag) == local:
            return c
    return None


def _descendants(el: Any, local: str) -> list:
    return [d for d in el.iter() if _local(d.tag) == local]


def _rel_attr(el: Any, name: str) -> str:
    """取 r: 命名空间下的属性（如 r:embed / r:id）。

    OOXML 里同一个元素上 r: 命名空间的属性前缀写法五花八门，
    按"命名空间 ∈ _R 且 local-name 相等"取，比按前缀取稳。
    """
    for k, v in (getattr(el, "attrib", None) or {}).items():
        ns, local = _split_tag(k)
        if local == name and _in(ns, _R):
            return v or ""
    return ""


def _text_of(el: Any, ns_group: set[str]) -> str:
    """把元素下某命名空间的 t 节点文字拼起来（兼容 rich text 被拆成多个 run）。"""
    if el is None:
        return ""
    parts = []
    for d in el.iter():
        ns, local = _split_tag(d.tag)
        if local == "t" and _in(ns, ns_group) and (d.text or ""):
            parts.append(d.text)
    return "".join(parts).strip()


# ---------------------------------------------------------------------------
# rels：rId → 包内 part
# ---------------------------------------------------------------------------
def _read_rels(zf: zipfile.ZipFile) -> dict[str, dict]:
    """word/_rels/document.xml.rels → {rId: {target, type}}。

    target 一律解析成**包内绝对路径**（如 word/media/image1.png），
    这样后面找 part 不用再关心相对谁。
    """
    out: dict[str, dict] = {}
    if _DOC_RELS not in zf.namelist():
        return out
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(zf.read(_DOC_RELS))
    except Exception as exc:  # noqa: BLE001
        logger.warning("解析 document.xml.rels 失败：%s", exc)
        return out

    for rel in root:
        rid = rel.get("Id") or ""
        if not rid:
            continue
        target = (rel.get("Target") or "").replace("\\", "/")
        rtype = rel.get("Type") or ""
        if target.startswith("/"):
            part = target.lstrip("/")            # 包内绝对
        elif target.startswith("../"):
            part = target[3:]                    # 全部 rels 都在 word/_rels/ 下，上一级即包根
        else:
            part = "word/" + target              # 相对 word/
        out[rid] = {"target": part, "type": rtype.rsplit("/", 1)[-1].lower()}
    return out


# ---------------------------------------------------------------------------
# document.xml：段落顺序 + 每个媒体元素落在第几段
# ---------------------------------------------------------------------------
def _scan_document(zf: zipfile.ZipFile) -> list[dict]:
    """扫正文 → [{"para_index","para_text","rel_id","hint","alt","prog_id"}]，顺序即文档流顺序。

    hint ∈ image | ole | chart，用来在 rels 认不出类型时兜底。

    只认 w: 命名空间的 `p` —— 不能用裸 `iter()` 找 local=='p'：
    DrawingML 里的 `a:p`（图形内的段落）local-name 也是 p，混进来会让段落序号整体错位。

    替代文字（alt）的取法利用了 OOXML 的书写顺序：`<wp:inline><wp:docPr descr="…"/>`
    总是排在它描述的 `<a:blip/>` **之前**，所以按文档流顺序遇到 docPr 就"挂起"，
    遇到下一个媒体元素时认领。这是作者手写的说明文字，**确定性、免 VLM**，
    对图片召回的价值比任何生成式描述都高，不能漏。
    """
    if _MAIN_DOC not in zf.namelist():
        return []
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(zf.read(_MAIN_DOC))
    except Exception as exc:  # noqa: BLE001
        logger.warning("解析 document.xml 失败：%s", exc)
        return []

    found: list[dict] = []
    last_text = ""
    para_index = -1

    for p in root.iter():
        ns, local = _split_tag(p.tag)
        if local != "p" or not _in(ns, _W):
            continue
        para_index += 1
        para_text = _text_of(p, _W)
        if para_text:
            last_text = para_text
        pending_alt = ""
        pending_prog = ""

        for el in p.iter():
            _, name = _split_tag(el.tag)
            attrs = getattr(el, "attrib", None) or {}

            if name == "docPr":                      # 图片/图形的替代文字
                pending_alt = (attrs.get("descr") or attrs.get("title")
                               or attrs.get("name") or "").strip() or pending_alt
                continue
            if name == "shape":                      # VML 老式图形（OLE/图片常走这条）
                pending_alt = (attrs.get("alt") or attrs.get("title")
                               or "").strip() or pending_alt
                continue

            rid = ""
            hint = ""
            if name == "blip":                       # DrawingML 图片
                rid, hint = _rel_attr(el, "embed"), "image"
            elif name == "imagedata":                 # VML 老式图片（w:pict 里）
                rid, hint = _rel_attr(el, "id"), "image"
            elif name == "OLEObject":                 # 嵌入对象
                rid, hint = _rel_attr(el, "id"), "ole"
                pending_prog = (attrs.get("ProgID") or "").strip()
            elif name == "chart":                     # Word 原生图表引用
                rid, hint = _rel_attr(el, "id"), "chart"
            if rid:
                found.append({
                    "para_index": para_index, "para_text": para_text, "rel_id": rid,
                    "hint": hint, "alt": pending_alt, "prog_id": pending_prog,
                    "anchor_text": para_text or last_text, "form": name,
                })
                pending_alt = ""
                pending_prog = ""
    return found


# ---------------------------------------------------------------------------
# 图表 XML → 确定性数据
# ---------------------------------------------------------------------------
def _pt_values(container: Any) -> list[str]:
    """从 strCache / numCache 里按 idx 顺序取数值。

    真实 Word 图表的 cache 结构是：
        <c:cat><c:strRef><c:f>Sheet1!$A$2:$A$5</c:f>
          <c:strCache><c:ptCount val="4"/>
            <c:pt idx="0"><c:v>2019</c:v></c:pt> …
    `c:f` 只是"公式引用串"（如 Sheet1!$A$2），**不是数据**，绝不能拿它当值。
    """
    if container is None:
        return []
    pts = []
    for pt in _descendants(container, "pt"):
        try:
            idx = int(pt.get("idx"))
        except (TypeError, ValueError):
            idx = len(pts)
        v = ""
        for cand in _descendants(pt, "v"):
            if cand.text:
                v = cand.text.strip()
                break
        pts.append((idx, v))
    pts.sort(key=lambda t: t[0])
    return [v for _, v in pts]


def _series_data(ser: Any) -> dict:
    """单个 c:ser → {name, categories, values}。

    系列名有两个来源，顺序不能反：
      1. `c:tx` 下的 cache（`c:strCache/c:pt/c:v`）—— 真实 Word 写的就是这个；
      2. `c:tx` 下的富文本（`a:p/a:r/a:t`）—— 手写/某些工具会走这条。
    ⚠️ 别去读 `c:tx/c:strRef/c:f`，那是公式串（如 `Sheet1!$B$1`）而不是名字。
    """
    tx = _child(ser, "tx")
    name = " ".join(_pt_values(tx)).strip() or _text_of(tx, _A)
    cats = _pt_values(_child(ser, "cat"))
    vals = _pt_values(_child(ser, "val"))
    if not cats or not vals:                     # 散点图用 xVal/yVal 而非 cat/val
        xv = _pt_values(_child(ser, "xVal"))
        yv = _pt_values(_child(ser, "yVal"))
        cats = cats or xv
        vals = vals or yv
    return {"name": name, "categories": cats, "values": vals}


def parse_chart_xml(data: bytes) -> dict:
    """chart*.xml → 确定性结构。解析不出就返回带 error 的空壳，绝不抛。

    返回值可能为空壳，调用方据此决定要不要退回 VLM —— 这是"确定性优先"的落点：
    只有 XML 里真的没有数据时，才轮到生成式模型。
    """
    info: dict = {"chart_type": "", "type_cn": "", "title": "", "series": [],
                  "categories": [], "error": ""}
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(data)
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"chart XML 解析失败：{exc}"
        return info

    plot = None
    for el in root.iter():
        _, local = _split_tag(el.tag)
        if local == "plotArea":
            plot = el
            break

    chart_el = None
    for el in (plot if plot is not None else root).iter():
        _, local = _split_tag(el.tag)
        if local in _PLOT_KINDS:
            chart_el, info["chart_type"] = el, local
            break

    info["type_cn"] = CHART_TYPE_CN.get(info["chart_type"], "")

    for el in root.iter():
        _, local = _split_tag(el.tag)
        if local == "title":
            info["title"] = _text_of(el, _A) or " ".join(_pt_values(el)).strip()
            break

    if chart_el is not None:
        for ser in _children(chart_el, "ser"):
            s = _series_data(ser)
            if s["values"] or s["name"]:
                info["series"].append(s)

    for s in info["series"]:
        if s["categories"]:
            info["categories"] = s["categories"]
            break

    if not info["series"]:
        info["error"] = "chart XML 中没有找到任何系列数据"
    return info


def chart_to_table(chart: dict) -> str:
    """图表数据 → Markdown 表格（保真事实，与正文表格同构，可复用 linearize_table）。

    用 Markdown 而不是自造格式：knowledge_chunk 里表格的保真表示和文字化都
    已经围着 Markdown 建好了，走同一条路能直接复用那套容错。
    """
    if not chart or not chart.get("series"):
        return ""
    series = chart["series"]
    headers = ["类目"] + [(s.get("name") or f"系列{i + 1}") for i, s in enumerate(series)]
    cats = chart.get("categories") or []
    rows = max([len(cats)] + [len(s.get("values") or []) for s in series])
    if rows <= 0:
        return ""

    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join(["---"] * len(headers)) + "|"]
    for i in range(rows):
        cells = [cats[i] if i < len(cats) else f"第{i + 1}项"]
        for s in series:
            vals = s.get("values") or []
            cells.append(vals[i] if i < len(vals) else "")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def chart_to_text(chart: dict) -> str:
    """图表数据 → 自然语言（面向检索）。

    为什么不能只留 Markdown：二维网格在向量空间里离自然语言问题很远，
    用户问"2021 年销售额是多少"不会命中 `| 2021 | 180 |`。
    把类目和数值显式写成"X=Y、X=Y"，才能被召回。
    """
    if not chart or not chart.get("series"):
        return ""
    head = chart.get("type_cn") or "图表"
    title = chart.get("title") or ""
    parts = [f"{head}「{title}」" if title else head]

    cats = chart.get("categories") or []
    for i, s in enumerate(chart["series"], start=1):
        name = s.get("name") or f"系列{i}"
        vals = s.get("values") or []
        pairs = []
        for j, v in enumerate(vals):
            key = cats[j] if j < len(cats) else f"第{j + 1}项"
            pairs.append(f"{key}={v}")
        parts.append(f"系列「{name}」：" + "、".join(pairs) if pairs else f"系列「{name}」无数据")
    return "；".join(parts)


# ---------------------------------------------------------------------------
# OLE：导出原件，并尽力取出内层文件
# ---------------------------------------------------------------------------
def _sniff_ext(blob: bytes) -> str:
    """按文件头猜内层类型。返回扩展名，猜不出返回空串。"""
    if blob.startswith(_MAGIC_ZIP):
        try:
            import io
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                names = z.namelist()
            for prefix, ext in _INNER_EXT.items():
                if any(n.startswith(prefix) for n in names):
                    return ext
            return ".zip"
        except Exception:  # noqa: BLE001
            return ".zip"
    if blob.startswith(b"%PDF"):
        return ".pdf"
    if blob.startswith(b"\xef\xbb\xbf") or blob.startswith(b"<?xml"):
        return ".xml"
    return ""


def extract_ole_payload(blob: bytes) -> tuple[bytes, str]:
    """OLE 复合文档 → (内层原始文件字节, 扩展名)。

    Word 插入 Excel 对象时，`word/embeddings/oleObject1.bin` 是 CFB 复合文档，
    里面有一个名为 `Package` 的流，**那个流才是原始的 .xlsx**。
    直接解析 CFB 太贵，用 olefile 取流即可；olefile 不可用时退回"导出 .bin"，
    绝不让缺一个可选依赖把整条 docx 解析带崩。
    有些工具（如某些转换器）会直接把 xlsx 塞进 embeddings，这时文件头就是 PK，
    所以先探测再解包。
    """
    ext = _sniff_ext(blob)
    if ext:
        return blob, ext
    if not blob.startswith(_MAGIC_CFB):
        return blob, ""
    try:
        import io

        import olefile

        with olefile.OleFileIO(io.BytesIO(blob)) as ole:
            if ole.exists("Package"):
                payload = ole.openstream("Package").read()
                return payload, (_sniff_ext(payload) or ".bin")
    except Exception as exc:  # noqa: BLE001
        logger.debug("OLE Package 流提取失败：%s", exc)
    return blob, ""


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def _safe_name(name: str) -> str:
    """去掉路径成分与危险字符，保留原文件名便于人工核对。"""
    base = os.path.basename((name or "").replace("\\", "/"))
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(". ")
    return base or "asset"


def _write(blob: bytes, dest_dir: str, filename: str) -> str:
    """写文件，重名自动加序号。返回落盘路径，失败返回空串。"""
    try:
        os.makedirs(dest_dir, exist_ok=True)
        stem, ext = os.path.splitext(filename)
        target = os.path.join(dest_dir, filename)
        n = 2
        while os.path.exists(target):
            target = os.path.join(dest_dir, f"{stem}_{n}{ext}")
            n += 1
        with open(target, "wb") as fh:
            fh.write(blob)
        return target
    except Exception as exc:  # noqa: BLE001
        logger.warning("媒体导出失败（%s）：%s", filename, exc)
        return ""


def media_dir_for(path: str, media_dir: Optional[str] = None) -> str:
    """某份 docx 的媒体导出目录：`<根目录>/<文件名词干>_<源路径指纹>/`。

    为什么要带指纹而不能只用文件名：`a/报告.docx` 与 `b/报告.docx` 是两份不同的
    文档，只用词干会混进同一个目录 —— 文件不会互相覆盖（_write 会自动加序号），
    但"这个图属于哪份文档"就再也说不清了，人工核对时是灾难。
    指纹取自**绝对路径**，所以同一份文档重复解析仍落在同一目录，
    增量重建时不会堆出一堆副本。
    """
    root = media_dir or MEDIA_DIR
    if not os.path.isabs(root):
        root = os.path.join(_ROOT, root)
    ap = os.path.abspath(path).replace("\\", "/").lower()   # Windows 路径大小写不敏感
    fingerprint = hashlib.sha1(ap.encode("utf-8")).hexdigest()[:8]
    stem = _safe_name(os.path.splitext(os.path.basename(path))[0])
    return os.path.join(root, f"{stem}_{fingerprint}")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def extract(path: str, *, media_dir: Optional[str] = None) -> list[DocxAsset]:
    """解 docx 包 → [DocxAsset]，顺序即文档流顺序。

    失败（不是 zip、文件损坏）返回空列表并告警：docx 支路整体不因媒体提取失败而中断，
    正文仍由 Docling（路线 A）提供。
    """
    if not os.path.isfile(path):
        logger.warning("docx 媒体提取：文件不存在 %s", path)
        return []
    if not zipfile.is_zipfile(path):
        logger.warning("docx 媒体提取：不是 zip 容器（.doc 等旧格式请先转换）%s", path)
        return []

    out_dir = media_dir_for(path, media_dir)
    assets: list[DocxAsset] = []

    try:
        zf = zipfile.ZipFile(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("docx 媒体提取：打开失败 %s（%s）", path, exc)
        return []

    with zf:
        names = set(zf.namelist())
        rels = _read_rels(zf)
        refs = _scan_document(zf)
        # 记录已导出过的 part → 路径，同一张图被引用多次时不必重复写盘
        exported: dict[str, str] = {}

        for ref in refs:
            if len(assets) >= MAX_ASSETS:
                logger.warning("docx 媒体数超过上限 %d，其余已省略：%s", MAX_ASSETS, path)
                break
            rid, hint = ref["rel_id"], ref["hint"]
            rel = rels.get(rid) or {}
            part = rel.get("target") or ""
            rtype = rel.get("type") or ""
            if not part or part not in names:
                # 引用存在但 part 缺失（文档被工具改写过）——记下来，不要静默跳过
                assets.append(DocxAsset(kind=hint or "unknown", rel_id=rid,
                                        part=part, para_index=ref["para_index"],
                                        para_text=ref["para_text"],
                                        anchor_text=ref["anchor_text"],
                                        alt_text=ref["alt"],
                                        error="rels 引用的部件在包内不存在"))
                continue

            if hint == "chart" or rtype == "chart":
                asset = _build_chart(zf, part, ref)
            elif hint == "ole" or rtype == "oleobject":
                asset = _build_blob(zf, part, ref, out_dir, exported, kind="ole")
            else:
                # image 与 rels 认得出但本次不认识的类型，一律按"导出原件"处理：
                # 保底不丢数据，代价只是多占点磁盘。
                kind = "image" if (hint == "image" or rtype == "image") else "other"
                asset = _build_blob(zf, part, ref, out_dir, exported, kind=kind)
            assets.append(asset)

    _mark_ole_previews(assets)
    _log_summary(path, assets)
    return assets


def _mark_ole_previews(assets: list[DocxAsset]) -> None:
    """标出"OLE 对象的 VML 预览位图"——它不是独立图片，是同一个对象的渲染预览。

    为什么会有这种重复：Word 插入 OLE 对象时会写两份表示，且**在同一个段落里**：
        <w:object>
          <v:shape><v:imagedata r:id="rIdPreview"/></v:shape>   ← 预览位图（常为 EMF/WMF）
          <o:OLEObject r:id="rIdReal" ProgID="Excel.Sheet.12"/> ← 真正的原件
        两条引用我们都会扫到，于是同一份数据会变成两个块：一个是解析得干干净净的
        表格文字（来自内层 xlsx），另一个是一张"图"。
    实测中预览图的替代文字与 OLE 相同，看起来像两份独立内容，实际是同一对象。

    判据刻意保守，两个条件同时成立才标记：
      1. 引用形式是 VML `imagedata`（DrawingML 的 `blip` 不是 OLE 的预览形式）；
      2. **同段落里那个 OLE 的正文确实解析出来了**（ole_text 非空）。
    第 2 条是关键：原件没解出来时（内层是 Visio 等我们不认识的格式），
    预览图可能就是唯一的视觉线索，那时保留它才是对的。
    """
    by_para: dict[int, DocxAsset] = {}
    for a in assets:
        if a.kind == "ole" and (a.ole_text or "").strip():
            by_para.setdefault(a.para_index, a)

    marked = 0
    for a in assets:
        if a.kind != "image" or a.form != "imagedata":
            continue
        ole = by_para.get(a.para_index)
        if ole is None:
            continue
        a.is_ole_preview = True
        a.error = ""
        marked += 1
        logger.debug("OLE 预览图已标记（与 %s 同段落）：%s", ole.part, a.part)

    if marked:
        logger.info("docx 媒体提取：%d 个图片实为同段落 OLE 对象的预览位图，已标记去重", marked)


def _build_chart(zf, part: str, ref: dict) -> DocxAsset:
    asset = DocxAsset(kind="chart", rel_id=ref["rel_id"], part=part,
                      filename=os.path.basename(part),
                      para_index=ref["para_index"], para_text=ref["para_text"],
                      anchor_text=ref["anchor_text"], alt_text=ref["alt"],
                      form=ref.get("form") or "")
    try:
        data = zf.read(part)
    except Exception as exc:  # noqa: BLE001
        asset.error = f"读取 chart 部件失败：{exc}"
        return asset
    asset.size = len(data)
    asset.chart = parse_chart_xml(data)
    if asset.chart.get("error"):
        asset.error = asset.chart["error"]
    return asset


def _build_blob(zf, part: str, ref: dict, out_dir: str, exported: dict,
                *, kind: str) -> DocxAsset:
    asset = DocxAsset(kind=kind, rel_id=ref["rel_id"], part=part,
                      filename=os.path.basename(part),
                      para_index=ref["para_index"], para_text=ref["para_text"],
                      anchor_text=ref["anchor_text"], alt_text=ref["alt"],
                      prog_id=ref.get("prog_id") or "",
                      form=ref.get("form") or "")
    try:
        blob = zf.read(part)
    except Exception as exc:  # noqa: BLE001
        asset.error = f"读取部件失败：{exc}"
        return asset
    asset.size = len(blob)

    if kind == "ole":
        payload, ext = extract_ole_payload(blob)
        asset.inner_ext = ext
        stem = os.path.splitext(os.path.basename(part))[0]
        if ext:
            # 解出了内层原件（xlsx/pptx/pdf…）就按真实扩展名导出，便于后续直接解析，
            # 而不是留一个让人无从下手的 .bin。
            asset.filename = f"{stem}{ext}"
            blob = payload

    if part in exported:                            # 同一部件被引用多次
        asset.saved_path = exported[part]
    else:
        asset.saved_path = _write(blob, out_dir, _safe_name(asset.filename))
        exported[part] = asset.saved_path
    if not asset.saved_path:
        asset.error = asset.error or "导出失败"

    if kind == "ole" and asset.saved_path:
        asset.ole_text = _ole_inner_text(asset.saved_path, asset.inner_ext)
    return asset


def _ole_inner_text(saved_path: str, ext: str) -> str:
    """OLE 内层若本身是可解析格式（xlsx/pptx/csv…），把正文抽出来做摘要。

    为什么值得做：内嵌的 Excel 里往往是**整张数据表**，用 loaders 直接解析
    比识别一张截图准得多，而且是确定性的。截断到 OLE_TEXT_LIMIT，
    避免一份几十页的嵌入表把检索索引冲淡。
    """
    if not ext:
        return ""
    from .loaders import load_text, supported_extensions

    if ext.lower() not in supported_extensions():
        return ""
    try:
        text = (load_text(saved_path) or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("OLE 内层解析失败（%s）：%s", saved_path, exc)
        return ""
    if len(text) > OLE_TEXT_LIMIT:
        text = text[:OLE_TEXT_LIMIT] + f"\n…（已截断，完整内容见 {saved_path}）"
    return text


def _log_summary(path: str, assets: list[DocxAsset]) -> None:
    """让"这份 docx 到底带出什么"在日志里一眼可见。

    只看"解析了多少块"看不出图表/OLE 有没有被漏掉，而这两类恰恰是 Docling
    完全拿不到、丢了也没人报错的部分。
    """
    counts: dict[str, int] = {}
    for a in assets:
        counts[a.kind] = counts.get(a.kind, 0) + 1
    if not counts:
        logger.info("docx 媒体提取：%s 无媒体元素", os.path.basename(path))
        return
    detail = "，".join(f"{k}={v}" for k, v in sorted(counts.items()))
    charts = sum(1 for a in assets if a.kind == "chart" and a.chart and a.chart.get("series"))
    errs = [a for a in assets if a.error]
    previews = sum(1 for a in assets if a.is_ole_preview)
    logger.info("docx 媒体提取：%s 共 %d 个（%s；其中图表数据可直接读出 %d 个，导出 %d 个文件%s）",
                os.path.basename(path), len(assets), detail, charts,
                sum(1 for a in assets if a.saved_path),
                f"，OLE 预览图 {previews} 个" if previews else "")
    if errs:
        logger.warning("docx 媒体提取有 %d 项异常：%s", len(errs),
                       "；".join(f"{a.kind}/{a.filename or a.part}: {a.error}" for a in errs[:5]))


def summarize(assets: list[DocxAsset]) -> dict:
    """供脚本与测试使用：把结果排成可直接打印的结构。"""
    counts: dict[str, int] = {}
    for a in assets:
        counts[a.kind] = counts.get(a.kind, 0) + 1
    return {
        "total": len(assets),
        "by_kind": counts,
        "charts_with_data": [a for a in assets
                            if a.kind == "chart" and a.chart and a.chart.get("series")],
        "charts_no_data": [a for a in assets
                          if a.kind == "chart" and (not a.chart or not a.chart.get("series"))],
        "saved": [a for a in assets if a.saved_path],
        "ole_previews": [a for a in assets if a.is_ole_preview],
        "errors": [a for a in assets if a.error],
    }
