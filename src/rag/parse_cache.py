"""文档解析结果落盘缓存：让冷启动不再重复解析全部语料。

问题
----
`load_chunks` 每次调用都会**重新解析 + 重新切分全部语料**（docling 版式检测、
表格识别、OCR、图片 VLM 描述…）。pgvector 只省掉了"重复算 embedding"，
**解析这一大段完全没有缓存**：语料涨到几百篇 PDF 后，冷启动会从秒级变成分钟级，
而且多 worker 部署会**乘以 worker 数**（每个进程各解析一遍）。

做法
----
缓存键 = sha1(缓存格式版本 + 语料目录绝对路径 + 影响解析的入参/环境变量 +
**全部文件的 (相对路径, 大小, mtime_ns)**)。

- 文件集合、大小、修改时间任一变化 → 键变化 → 自动重算（不会读到过期结果）；
- 语料没变 → 直接反序列化上一次的 chunks（含 page/bbox/table_md/parent_text 等
  全部字段），跳过整条解析链。

缓存目录默认放在**语料目录下的 `.parse_cache`**：`glob` 的 `*` 不匹配点号开头的
目录，所以缓存文件不会被当成语料读进去（`.media`/`.ctx_cache` 同理）。

写入用"临时文件 + os.replace"，保证读到的一定是完整 JSON（进程被 kill 也不会
留下半截文件被下次读到）。
"""

import hashlib
import json
import logging
import os

from .retrieval import load_chunks

# 缓存格式版本：**任何会改变 chunks 结构的逻辑改动都要 +1**，否则老缓存会被当成
# 有效结果喂给新代码，产生极难排查的字段缺失。
CACHE_VERSION = 1

# 影响解析结果的环境变量。任一变化都要重算（例如把 OCR 从 auto 改成 on）。
_ENV_KEYS = (
    "RAG_CHUNK_STRATEGY", "RAG_CHUNK_SIZE", "RAG_CHUNK_OVERLAP",
    "RAG_PDF_CHUNK_SIZE", "RAG_PDF_CHUNK_OVERLAP", "RAG_DOC_CHUNK_TOKENS",
    "RAG_DOC_PARSER", "RAG_CONTEXTUAL", "RAG_PARENT_CHILD", "RAG_PARENT_MAX_CHARS",
    "RAG_CTX_CACHE_DIR", "RAG_MEDIA_DIR", "RAG_VLM_CACHE_DIR",
    "DOCLING_OCR", "DOCLING_OCR_MODE", "DOCLING_OCR_LANG", "DOCLING_OCR_BACKEND",
    "DOCLING_ARTIFACTS_PATH", "DOCLING_DEVICE", "DOCLING_FORMULA",
    "VLM_MODEL", "VLM_MAX_IMAGES", "VLM_MAX_SIDE", "RAG_DOCX_MAX_ASSETS",
    "RAG_CITE_IMAGE_MARGIN", "DOCLING_IMAGES_SCALE", "DOCS_DIR",
)


def cache_enabled() -> bool:
    """缓存是否开启（RAG_PARSE_CACHE，默认开）。"""
    return (os.getenv("RAG_PARSE_CACHE", "1").strip().lower()
            not in ("0", "off", "false", "no"))


def cache_dir_for(doc_folder: str, override: str = "") -> str:
    """算缓存目录：环境变量/入参优先，否则用 `<doc_folder>/.parse_cache`。

    doc_folder: 语料根目录。
    override: 显式指定目录（参数 > 环境变量 > 默认）。
    """
    d = (override or os.getenv("RAG_PARSE_CACHE_DIR", "") or "").strip()
    if d:
        return d
    return os.path.join(doc_folder or ".", ".parse_cache")


def _iter_files(doc_folder):
    """列出语料文件（相对路径, 大小, mtime_ns），跳过点号开头的隐藏目录/文件。

    与 `load_chunks` 里的 glob 口径保持一致（glob 的 * 不匹配 `.` 开头），
    所以 `.parse_cache` / `.media` 不会进指纹、也不会因缓存自身变化触发重算。
    """
    out = []
    for root, dirs, files in os.walk(doc_folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in files:
            if fn.startswith("."):
                continue
            p = os.path.join(root, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append((os.path.relpath(p, doc_folder), st.st_size, st.st_mtime_ns))
    out.sort()
    return out


def cache_key(doc_folder, chunk_size=None, chunk_overlap=None,
              pdf_chunk_size=None, pdf_overlap=None) -> str:
    """算缓存键（sha1 十六进制）。

    doc_folder: 语料根目录。
    chunk_size/chunk_overlap/pdf_chunk_size/pdf_overlap: 切分入参（None 表示"用默认"，
        与 load_chunks 的语义一致 —— 这里不解析成具体数字，因为默认值变化会体现在
        环境变量指纹里）。

    指纹包含：缓存版本、绝对路径、四个切分入参、相关环境变量、全部文件的
    (相对路径, 大小, mtime_ns)。
    """
    h = hashlib.sha1()
    h.update(f"v{CACHE_VERSION}".encode())
    h.update(b"\x00")
    h.update(os.path.abspath(doc_folder).encode("utf-8"))
    h.update(b"\x00")
    for v in (chunk_size, chunk_overlap, pdf_chunk_size, pdf_overlap):
        h.update(str(v).encode())
        h.update(b"\x00")
    for k in _ENV_KEYS:
        h.update(f"{k}={os.getenv(k, '')}".encode("utf-8"))
        h.update(b"\x00")
    for rel, size, mtime in _iter_files(doc_folder):
        h.update(f"{rel}|{size}|{mtime}".encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def load_chunks_cached(doc_folder, chunk_size=None, chunk_overlap=None,
                       pdf_chunk_size=None, pdf_overlap=None,
                       cache_dir=None, enabled=None):
    """带落盘缓存的 `load_chunks`。

    doc_folder: 语料根目录。
    chunk_size/chunk_overlap/pdf_chunk_size/pdf_overlap: 透传给 load_chunks。
    cache_dir: 缓存目录覆盖（默认 `<doc_folder>/.parse_cache`）。
    enabled: 覆盖开关（默认读 RAG_PARSE_CACHE）。

    **任何缓存层的异常都会退化为直接调用 load_chunks** —— 缓存是加速手段，
    绝不能因为它（磁盘满/权限/坏 JSON）导致灌库失败。
    """
    on = cache_enabled() if enabled is None else bool(enabled)
    if not on or not doc_folder or not os.path.isdir(doc_folder):
        return load_chunks(doc_folder, chunk_size, chunk_overlap,
                           pdf_chunk_size, pdf_overlap)

    try:
        key = cache_key(doc_folder, chunk_size, chunk_overlap,
                        pdf_chunk_size, pdf_overlap)
        path = os.path.join(cache_dir_for(doc_folder, cache_dir or ""), key + ".json")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if payload.get("version") == CACHE_VERSION and isinstance(payload.get("chunks"), list):
                logging.info("语料解析缓存命中：%s（%d 块，跳过解析）",
                             os.path.basename(path), len(payload["chunks"]))
                return payload["chunks"]
            logging.warning("解析缓存版本不符，忽略并重算：%s", path)
    except Exception as e:  # noqa: BLE001
        logging.warning("读取解析缓存失败，改为重新解析：%s", e)
        return load_chunks(doc_folder, chunk_size, chunk_overlap,
                           pdf_chunk_size, pdf_overlap)

    chunks = load_chunks(doc_folder, chunk_size, chunk_overlap,
                         pdf_chunk_size, pdf_overlap)

    # 只有解析出内容才缓存：空结果（漏配目录、文件全损坏）缓存下来会让
    # "修好文件后仍读到空"这种问题极难发现。
    if not chunks:
        return chunks
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": CACHE_VERSION, "chunks": chunks}, fh, ensure_ascii=False)
        os.replace(tmp, path)            # 原子替换：读者永远看不到半截文件
        logging.info("语料解析结果已缓存：%s（%d 块）", path, len(chunks))
    except Exception as e:  # noqa: BLE001
        logging.warning("写入解析缓存失败（不影响本次灌库）：%s", e)
    return chunks


def clear(doc_folder, cache_dir=None) -> int:
    """清掉某语料目录的所有缓存文件，返回删除数量（运维/调试用）。

    doc_folder: 语料根目录。
    cache_dir: 覆盖缓存目录。
    """
    d = cache_dir_for(doc_folder, cache_dir or "")
    n = 0
    if not os.path.isdir(d):
        return 0
    for fn in os.listdir(d):
        if fn.endswith(".json") or fn.endswith(".tmp"):
            try:
                os.remove(os.path.join(d, fn))
                n += 1
            except OSError:
                pass
    return n
