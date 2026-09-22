"""Contextual Retrieval（批次 8，可选）：给每块加一句"它在整篇文档里的位置与主题"。

Anthropic 的实测结论（`contextual-retrieval`）：仅加**上下文前缀**，top-20 检索失败率
5.7% → 3.7%；前缀 + BM25 混合检索 → 2.9%。原理很直白：一个孤立的片段
（"该季度营收环比增长 12%"）在向量空间里没有主语，加一句"这段出自 XX 年报的
经营讨论章节"之后才可被正确召回。

**为什么默认关闭**：这是切分侧唯一显著花钱的改进 —— 每块一次 LLM 调用。
（父子块不花钱；前几批的改进也都不花钱。）所以：
  - 默认 `RAG_CONTEXTUAL=off`；
  - 打开后每块结果**按内容缓存**，重跑同一语料不再付费；
  - 有单次上限，防止误开时把几千块的语料一次刷完。

配置
  RAG_CONTEXTUAL      off（默认）| on
  CONTEXTUAL_MODEL    默认 agnes-2.0-flash（只做"定位与主题"，不需要强模型）
  RAG_CTX_MAX_CHUNKS  单次运行上限（默认 500）
  RAG_CTX_DOC_CHARS   给模型的文档全文上限（默认 20000，超出只取头部+命中块附近）
  RAG_CTX_CACHE_DIR   缓存目录（默认 data/.ctx_cache）
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv

    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(dotenv_path=os.path.join(_ROOT, ".env"))
except Exception:  # noqa: BLE001
    pass


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


CONTEXTUAL_MODEL = os.getenv("CONTEXTUAL_MODEL", "").strip() or "agnes-2.5-pro"
# ⚠️ 为什么不用更便宜的 flash：实测本账号的 `agnes-2.0-flash` 直接 429
# （"reached the API rate limit for free users"），一次都调不通；
# 而 `agnes-2.5-pro` 可用。默认值必须是**在当前凭证下真能跑通**的那个。
CTX_API_BASE = os.getenv("AGNES_API_BASE", "https://apihub.agnes-ai.com/v1").strip()
CTX_API_KEY = os.getenv("AGNES_API_KEY", "").strip()
CTX_MAX_CHUNKS = max(1, _env_int("RAG_CTX_MAX_CHUNKS", 500))
CTX_DOC_CHARS = max(2000, _env_int("RAG_CTX_DOC_CHARS", 20000))
CTX_CACHE_DIR = os.getenv("RAG_CTX_CACHE_DIR", "data/.ctx_cache").strip() or "data/.ctx_cache"
# 输出上限。**实测教训（两次）**：agnes-2.5-pro 是推理型模型，先花推理 token 再出正文。
#   一次简单请求：46 推理 + 16 正文；
#   一次带"文档全文"的真实请求：**404 推理 + 0 正文**（finish_reason=length）→ 只得到空串且不报错。
# 所以上限必须按"推理开销"而不是"一句话有多长"来给。1200 足够，代价是每题约 400~900
# 推理 token（有缓存，每块只付一次）。要省钱就换一个**非推理型**的 OpenAI 兼容模型。
CTX_MAX_TOKENS = max(200, _env_int("RAG_CTX_MAX_TOKENS", 1200))
# 前缀长度上限。**实测教训**：本项目切出来的块只有 100~300 字，而模型给的前缀
# 平均 40~60 字 —— 占了嵌入文本的 30% 左右，把块自身词项的权重稀释掉了：
# 实测 Top1 下降、Top3 上升（正确的块更容易进候选，但首位排名被搞乱）。
# 所以默认收紧到 60 字，只留"章节 + 主题"。
CTX_PREFIX_MAX = max(20, _env_int("RAG_CTX_PREFIX_MAX", 60))
# 并发度。**实测教训**：串行时 17 个块跑了 1 分多钟才出 1 个前缀，
# 按这个速率 1000 块要十几小时 —— 这个功能等于不可用。
# 每个块的前缀只依赖它自己的文本，天然可并行，所以用受控并发（默认 4，保守防限流）。
CTX_WORKERS = max(1, _env_int("RAG_CTX_WORKERS", 4))

_PROMPT = """<文档>
{doc}
</文档>

<片段>
{chunk}
</片段>

请用**一句中文**说明这个片段在整篇文档中的位置与主题，用于改进检索。
要求：只写这一句话（不超过 {limit} 字，越短越好）；提到所属章节/文件的主题；不要复述片段里没有的信息；不要输出解释或标点以外的格式。"""


def enabled() -> tuple[bool, str]:
    from . import retrieval

    if not getattr(retrieval, "CONTEXTUAL", False):
        return False, "RAG_CONTEXTUAL=off（默认关闭：每块一次 LLM 调用，先算量再开）"
    if not CTX_API_KEY:
        return False, "RAG_CONTEXTUAL=on 但未配置 AGNES_API_KEY"
    return True, "RAG_CONTEXTUAL=on"


def _doc_text(chunks: list, source: str, focus: int) -> str:
    """给模型的"整篇文档"参考文本。

    为什么要限长：一份几百页的文档全文塞进去，光输入 token 就够买好几张 VLM 描述。
    超出上限时取「文档头部 + 命中块附近」——头部给文档主题，附近给局部位置，
    这两块信息才是生成"位置与主题"所需的。
    """
    texts = [(c.get("text") or "") for c in chunks if c.get("source") == source]
    full = "\n".join(texts)
    if len(full) <= CTX_DOC_CHARS:
        return full
    head = full[: CTX_DOC_CHARS // 2]
    cur = chunks[focus].get("text") or ""
    idx = full.find(cur)
    around = full[max(0, idx - CTX_DOC_CHARS // 4): idx + CTX_DOC_CHARS // 4] if idx >= 0 else ""
    return f"{head}\n…（中间省略）…\n{around}"


def _cache_path(source: str, text: str) -> str:
    """缓存键 = 模型 + 源文件 + 块文本 + **前缀长度上限**。

    为什么要把长度上限也算进键里：它就是 prompt 的一部分（"不超过 N 字"）。
    改了这个参数却复用旧前缀，会得到一个"看起来测了新参数、其实用的是旧结果"的假结论 ——
    这类静默错配比多花一次调用贵得多。
    """
    key = hashlib.sha1((f"{CONTEXTUAL_MODEL}\x00{CTX_PREFIX_MAX}\x00{source}\x00{text}")
                       .encode("utf-8")).hexdigest()
    root = CTX_CACHE_DIR
    if not os.path.isabs(root):
        root = os.path.join(_ROOT, root)
    return os.path.join(root, f"{key}.txt")


def _read_cache(source: str, text: str) -> Optional[str]:
    p = _cache_path(source, text)
    if not os.path.isfile(p):
        return None
    try:
        with io.open(p, "r", encoding="utf-8") as fh:
            v = fh.read().strip()
        return v or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("上下文缓存读取失败，忽略：%s（%s）", p, exc)
        return None


def _write_cache(source: str, text: str, value: str) -> None:
    try:
        p = _cache_path(source, text)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with io.open(p, "w", encoding="utf-8") as fh:
            fh.write(value)
    except Exception as exc:  # noqa: BLE001
        logger.warning("上下文缓存写入失败（不影响结果）：%s", exc)


def _call_api(prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=CTX_API_KEY, base_url=CTX_API_BASE, timeout=60)
    resp = client.chat.completions.create(
        model=CONTEXTUAL_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=CTX_MAX_TOKENS,   # 推理型模型要先花推理 token，给小了会返回空正文
    )
    text = (resp.choices[0].message.content or "")
    if not text.strip():
        # 空正文不报错最难查：把它变成一条能指向原因的异常，而不是让上层记成"模型返回空"
        finish = getattr(resp.choices[0], "finish_reason", "?")
        usage = getattr(resp, "usage", None)
        raise RuntimeError(
            f"模型返回空正文（finish_reason={finish}, usage={usage}）"
            f"—— 推理型模型可能把 max_tokens({CTX_MAX_TOKENS}) 用在推理上了")
    return text.strip()


def _clean(text: str, limit: Optional[int] = None) -> str:
    """把模型输出收拾成"一行前缀"。

    模型偶尔会加引号、换行、"这句话是："之类的前后缀 —— 这些进入嵌入文本只会是噪声，
    所以统一剥掉。剥完为空则返回空串（调用方据此视为失败，不写前缀）。
    """
    limit = CTX_PREFIX_MAX if limit is None else limit
    t = (text or "").strip().strip('"“”\'`').replace("\n", " ").strip()
    for prefix in ("上下文：", "上下文:", "这句话是：", "说明："):
        if t.startswith(prefix):
            t = t[len(prefix):].strip()
    return t[:limit].strip()


def apply(chunks: list, *, call: Optional[Callable[[str], str]] = None,
          max_chunks: Optional[int] = None, workers: Optional[int] = None) -> dict:
    """就地为每块加 `ctx_prefix`，并把前缀拼到 `text` 前面（嵌入输入即带上下文）。

    为什么拼进 `text` 而不是只存字段：检索层只认 `text`（见 chunk_writer 的契约），
    前缀只有真的进了 `text` 才会被向量化 —— 存成旁路字段等于没做。

    失败（网络/鉴权/空输出）一律**保持原样**并计数，绝不因为一个改进项失败而破坏语料。

    并发：每个块的前缀只依赖自身文本（文档全文只读），所以用受控并发；
    实测串行调用慢到不可用（见 CTX_WORKERS 的说明）。结果按块索引写回，与顺序无关。
    """
    stats = {"total": len(chunks), "prefixed": 0, "cached": 0,
             "skipped_empty": 0, "skipped_limit": 0, "failed": 0}
    if not chunks:
        return stats

    ok, why = enabled()
    if not ok:
        logger.info("跳过 Contextual Retrieval：%s", why)
        return stats

    limit = CTX_MAX_CHUNKS if max_chunks is None else max_chunks
    fn = call or _call_api

    # 第一趟（主线程）：缓存命中与各种"不做"的情况先处理掉，并为其余块备好 prompt
    todo: list[tuple[int, str, str]] = []          # (索引, 源, prompt)
    for i, c in enumerate(chunks):
        text = (c.get("text") or "").strip()
        if not text:
            stats["skipped_empty"] += 1
            continue
        if c.get("ctx_prefix"):
            continue
        source = c.get("source") or "?"
        hit = _read_cache(source, text)
        if hit:
            _attach(c, hit)
            stats["prefixed"] += 1
            stats["cached"] += 1
            continue
        if len(todo) >= limit:
            stats["skipped_limit"] += 1
            continue
        todo.append((i, source, _PROMPT.format(
            doc=_doc_text(chunks, source, i), chunk=text, limit=CTX_PREFIX_MAX)))

    if not todo:
        return stats

    n_workers = CTX_WORKERS if workers is None else max(1, workers)
    logger.info("Contextual Retrieval：%d 块需要生成前缀（并发 %d，模型 %s）",
                len(todo), n_workers, CONTEXTUAL_MODEL)

    # 限流止损：实测遇到 429（免费额度）时，若继续硬撞，剩下每块都要等一次失败往返。
    # 一旦判定被限流，立刻把事件立起来，还没开跑的 worker 直接放弃 ——
    # 这不是"隐藏失败"：失败数照常统计，只是不再浪费时间和配额。
    stop = threading.Event()
    limited: list = []

    def _work(item):
        idx, source, prompt = item
        if stop.is_set():
            return idx, source, "", "限流后跳过"
        try:
            return idx, source, _clean(fn(prompt)), ""
        except Exception as exc:  # noqa: BLE001  限流/超时都可能
            msg = f"{type(exc).__name__}: {exc}"
            if _is_rate_limited(msg):
                limited.append(source)
                stop.set()
            return idx, source, "", msg

    if n_workers == 1:
        results = [_work(it) for it in todo]
    else:
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            results = list(pool.map(_work, todo))

    for idx, source, prefix, err in results:
        if err or not prefix:
            stats["failed"] += 1
            logger.warning("上下文生成失败（%s）：%s", source, err or "模型返回空")
            continue
        _write_cache(source, chunks[idx].get("text") or "", prefix)
        _attach(chunks[idx], prefix)
        stats["prefixed"] += 1

    if limited:
        logger.warning(
            "Contextual Retrieval 被限流打断（%d 块未做）。已成功的前缀**已写入缓存**，"
            "补齐后重跑即可只跑缺的那些。建议：调小 RAG_CTX_WORKERS（当前 %d）分多次跑，"
            "或升级 API 套餐 / 换一个不受限的 OpenAI 兼容服务（CONTEXTUAL_MODEL 与 "
            "AGNES_API_BASE 可分别指定）。", stats["failed"], n_workers)
    return stats


def _is_rate_limited(msg: str) -> bool:
    """限流判定。各家的措辞不同（429 / rate limit / too many requests / quota），
    所以按关键词匹配而不是认某个异常类 —— 换服务商时不必改代码。"""
    low = (msg or "").lower()
    return any(k in low for k in ("429", "rate limit", "ratelimit",
                                  "too many requests", "quota"))


def _attach(chunk: dict, prefix: str) -> None:
    """前缀拼到 text **最前面**：嵌入时它离查询词最近，影响力最大。"""
    chunk["ctx_prefix"] = prefix
    chunk["text"] = f"{prefix}\n{chunk.get('text') or ''}"


def format_stats(stats: dict) -> str:
    return (f"已加前缀 {stats['prefixed']}/{stats['total']} 块"
            f"（缓存命中 {stats['cached']}）；未做：空块 {stats['skipped_empty']}"
            f" / 超上限 {stats['skipped_limit']} / 失败 {stats['failed']}")
