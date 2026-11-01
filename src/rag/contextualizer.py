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
  CONTEXTUAL_MODEL    默认 deepseek-chat（只做"定位与主题"，不需要强模型）
  RAG_CTX_MAX_CHUNKS  单次运行上限（默认 500）
  RAG_CTX_DOC_CHARS   给模型的文档全文上限（默认 20000，超出只取头部+命中块附近）
  RAG_CTX_CACHE_DIR   缓存目录（默认 data/.ctx_cache）
  RAG_CTX_TIMEOUT     单次出站调用超时秒数（默认 60）
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv

    # 项目根 = 上溯 3 层（src/rag/x.py → src/rag → src → 根）。少一层会指向
    # src/.env（并不存在）而让加载静默失效；下面用 `_ROOT` 解析相对缓存目录，
    # 若算错会把 data/.ctx_cache 落到 src/data/.ctx_cache 里（2026-09-28 迁入 src/rag/ 时踩过）。
    _ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    load_dotenv(dotenv_path=os.path.join(_ROOT, ".env"))
except Exception:  # noqa: BLE001
    pass


def _env_int(name: str, default: int) -> int:
    """读环境变量并转 int，转不动就用默认值（本模块内多处配置共用）。

    name: 环境变量名。
    default: 未设置或值不是合法整数时的回退值。
    """
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


CONTEXTUAL_MODEL = os.getenv("CONTEXTUAL_MODEL", "").strip() or "deepseek-chat"
# ⚠️ 默认值必须是**在当前凭证下真能跑通**的那个模型，不是"看起来该用的"那个。
#    历史（2026-10，旧网关）：当时的 flash 档在本账号直接 429（免费档限流），一次都
#    调不通，只能退到 pro 档。2026-11-01 起主网关换成 DeepSeek，该限制不再适用 ——
#    但**这条经验的形状有效**：换网关后要先用最小请求验一次，再把默认值改过去。
CTX_API_BASE = os.getenv("LLM_API_BASE", "https://api.deepseek.com").strip()
CTX_API_KEY = os.getenv("LLM_API_KEY", "").strip()
CTX_MAX_CHUNKS = max(1, _env_int("RAG_CTX_MAX_CHUNKS", 500))
CTX_DOC_CHARS = max(2000, _env_int("RAG_CTX_DOC_CHARS", 20000))
CTX_CACHE_DIR = os.getenv("RAG_CTX_CACHE_DIR", "data/.ctx_cache").strip() or "data/.ctx_cache"
#: 单次出站调用超时（秒）。**2026-10-05 从硬编码 60 改为可配**。
#:
#: 为什么统一到环境变量：本目录其余每一个出站点（embedding 的 EMBEDDING_TIMEOUT、
#: 向量库的 QDRANT_TIMEOUT、重排的 RERANK_TIMEOUT、图片描述的 VLM_TIMEOUT）都
#: 已可配，只有这一处写死在 `OpenAI(..., timeout=60)`。硬编码的后果不是"超时不对"，
#: 而是**换模型/换网关时无法按需收紧**（推理型模型的前缀生成本来就慢，60 秒可能
#: 宽裕；换成一个排队严重的网关时又可能想调大），只能改代码重新发版。
#: 这里还**不设 `max_retries`**（SDK 默认为 2）：切分是离线批量任务，
#: 失败重试比"立刻放弃"更划算，且它不在请求热路径上，重试不会拖慢任何用户请求。
CTX_TIMEOUT = max(5, _env_int("RAG_CTX_TIMEOUT", 60))
# 输出上限。**实测教训（两次，当时的模型是推理型）**：推理型模型先花推理
# token 再出正文。
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
# 限流告警里最多点名几个来源文件。`limited` 的 append 不去重（同一份文件的多个块
# 并发命中会各写一条），所以先 set 去重；大语料下来源可能上百，全打出来会把一行日志撑爆，
# 故只列前 N 个并附上总数。
CTX_LIMITED_SHOWN = max(1, _env_int("RAG_CTX_LIMITED_SHOWN", 5))

_PROMPT = """<文档>
{doc}
</文档>

<片段>
{chunk}
</片段>

请用**一句中文**说明这个片段在整篇文档中的位置与主题，用于改进检索。
要求：只写这一句话（不超过 {limit} 字，越短越好）；提到所属章节/文件的主题；不要复述片段里没有的信息；不要输出解释或标点以外的格式。"""


def enabled() -> tuple[bool, str]:
    """总开关体检：返回 (是否启用, 原因文案)，原因会直接进日志。

    无参数。判两件事：retrieval 模块的 CONTEXTUAL 开关（环境变量 RAG_CONTEXTUAL）
    与 CTX_API_KEY 是否配置。任一条不满足都返回 False —— 调用方据此跳过，
    并把这个原因原样打进日志，避免"为什么没生效"要靠翻环境变量。
    """
    from . import retrieval

    if not getattr(retrieval, "CONTEXTUAL", False):
        return False, "RAG_CONTEXTUAL=off（默认关闭：每块一次 LLM 调用，先算量再开）"
    if not CTX_API_KEY:
        return False, "RAG_CONTEXTUAL=on 但未配置 LLM_API_KEY"
    return True, "RAG_CONTEXTUAL=on"


def _doc_text(chunks: list, source: str, focus: int) -> str:
    """给模型的"整篇文档"参考文本。

    chunks: 候选块列表（list[dict]，读 "text" 与 "source" 两个键）。
    source: 目标块的来源文件名 —— 只取同 source 的块拼成"整篇文档"，
        所以多文件语料下不会串台。
    focus: 命中块在 chunks 中的索引。用于超长时定位"附近"，取不到时该段留空。

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
    """缓存文件的绝对路径。

    source: 来源文件名（参与键）。
    text: 块正文（参与键）—— 用内容而不是块 id，保证"同一段文字换个文件切"也能复用。
        ⚠️ **读、写两侧必须传同口径的 text**（本模块在 `apply` 里两处都已 strip）。任一侧
        少了 strip，带首尾空白的块就会"写进去一个键、读的时候算另一个键"——缓存静默永久失效。
        另注：source 也参与键，所以**跨文件并不共享**，同一段文字出现在两个文件里仍要付两次。

    缓存键 = 模型 + 源文件 + 块文本 + **前缀长度上限**。

    为什么要把长度上限也算进键里：它就是 prompt 的一部分（"不超过 N 字"）。
    改了这个参数却复用旧前缀，会得到一个"看起来测了新参数、其实用的是旧结果"的假结论 ——
    这类静默错配比多花一次调用贵得多。
    """
    key = hashlib.sha1((f"{CONTEXTUAL_MODEL}\x00{CTX_PREFIX_MAX}\x00{source}\x00{text}")
                       .encode()).hexdigest()
    root = CTX_CACHE_DIR
    if not os.path.isabs(root):
        root = os.path.join(_ROOT, root)
    return os.path.join(root, f"{key}.txt")


def _read_cache(source: str, text: str) -> str | None:
    """读缓存的前缀，未命中或读取失败返回 None。

    source: 来源文件名，透传 _cache_path 参与算键。
    text: 块正文，同上。

    读盘异常（权限/编码）只记 warning 并按未命中处理 —— 缓存是省钱手段，
    不能因为它坏了就阻断灌库。
    """
    p = _cache_path(source, text)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            v = fh.read().strip()
        return v or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("上下文缓存读取失败，忽略：%s（%s）", p, exc)
        return None


def _write_cache(source: str, text: str, value: str) -> None:
    """把生成好的前缀写进缓存文件（自动建目录）。

    source: 来源文件名，透传 _cache_path 参与算键。
    text: 块正文，同上。
    value: 要缓存的前缀文本。

    写盘失败只记 warning：本次结果仍会被 _attach 用上，只是下次要重新付费。
    """
    try:
        p = _cache_path(source, text)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(value)
    except Exception as exc:  # noqa: BLE001
        logger.warning("上下文缓存写入失败（不影响结果）：%s", exc)


def _call_api(prompt: str) -> str:
    """默认的前缀生成实现（OpenAI 兼容 chat.completions，温度 0.1）。

    prompt: 已填好 doc / chunk / limit 的完整 prompt（见 _PROMPT）。

    返回 strip 后的正文。**空正文会主动抛 RuntimeError**，并把 finish_reason 与
    usage 带进消息 —— 因为"模型返回空"若被上层记成普通失败，最难的排查就此开始
    （实测：推理型模型把 max_tokens 全用在推理上时正文为空且不报错）。
    调用方 apply 支持注入 call 替换本函数，测试不必真连 API。
    """
    from openai import OpenAI

    client = OpenAI(api_key=CTX_API_KEY, base_url=CTX_API_BASE, timeout=CTX_TIMEOUT)
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


def _clean(text: str, limit: int | None = None) -> str:
    """把模型输出收拾成"一行前缀"。

    text: 模型原始输出。None / 空串都会走到空串返回。
    limit: 截断长度上限。None 时取模块级 CTX_PREFIX_MAX —— 显式传值主要用于
        测试与调参；注意它不参与缓存键（键里固定用 CTX_PREFIX_MAX），
        所以改这个参数做实验时请改成环境变量。

    模型偶尔会加引号、换行、"这句话是："之类的前后缀 —— 这些进入嵌入文本只会是噪声，
    所以统一剥掉。剥完为空则返回空串（调用方据此视为失败，不写前缀）。
    """
    limit = CTX_PREFIX_MAX if limit is None else limit
    t = (text or "").strip().strip('"“”\'`').replace("\n", " ").strip()
    for prefix in ("上下文：", "上下文:", "这句话是：", "说明："):
        if t.startswith(prefix):
            t = t[len(prefix):].strip()         #prompt里面对应写了上限了，所以这里一般来说不会意外切断
    return t[:limit].strip()


def apply(chunks: list, *, call: Callable[[str], str] | None = None,
          max_chunks: int | None = None, workers: int | None = None) -> dict:
    """就地为每块加 `ctx_prefix`，并把前缀拼到 `text` 前面（嵌入输入即带上下文）。

    chunks: 待处理的块列表（list[dict]，**就地改写**，不是返回值）。实现读
        "text" / "source" / "ctx_prefix" 三个键。
    call: 可注入的前缀生成函数，签名 (prompt) -> str。None 时用 _call_api。
        测试与离线场景传假实现即可，不必真连 API。
    max_chunks: 本次最多生成的块数（超出的计入 skipped_limit）。None 时取
        环境变量 CTX_MAX_CHUNKS（默认 500）—— 防止误开时把整库刷完。
    workers: 并发线程数。None 时取 CTX_WORKERS（默认 4）；传 1 则走串行分支
        （不建线程池，便于调试与对比）。

    返回统计 dict：total / prefixed / cached / skipped_empty / skipped_limit / failed。
    调用方用 format_stats 打日志。

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
        """线程池里的单块任务，返回 (索引, 源, 前缀, 错误信息)。

        item: (索引, 源, prompt) 三元组，由上面 todo 构造。

        不抛异常：任何失败都折叠成第 4 位的错误字符串。若是限流则顺带
        置位 stop 事件（让尚未开跑的 worker 立刻放弃）并记下来源，
        供 apply 末尾统一告警。
        """
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
        # 与读侧同口径：第一趟取 text 时先 strip 再查缓存（见上面的 text = ...strip()），
        # 这里若传未 strip 的原文，带首尾空白的块写进去的键与下次读取的键就不同 ——
        # 缓存静默永久失效（不报错、不告警，只是每次都重新付费）。
        _write_cache(source, (chunks[idx].get("text") or "").strip(), prefix)
        _attach(chunks[idx], prefix)
        stats["prefixed"] += 1

    if limited:
        # 去重后再展示：同一份文件的多个块会各自 append 一次（append 不做去重），
        # 直接打出来会有大量重复项；超出上限的只报总数，避免大语料把一行日志撑爆。
        # 点名来源是必需的 —— 否则多文件灌库时只看到一个总块数，查不出是哪份文档撞的配额。
        srcs = sorted(set(limited))
        shown = "、".join(srcs[:CTX_LIMITED_SHOWN])
        if len(srcs) > CTX_LIMITED_SHOWN:
            shown += f" 等 {len(srcs)} 个文件"
        logger.warning(
            "Contextual Retrieval 被限流打断（%d 块未做，来源：%s）。已成功的前缀**已写入缓存**，"
            "补齐后重跑即可只跑缺的那些。建议：调小 RAG_CTX_WORKERS（当前 %d）分多次跑，"
            "或升级 API 套餐 / 换一个不受限的 OpenAI 兼容服务（CONTEXTUAL_MODEL 与 "
            "LLM_API_BASE 可分别指定）。", stats["failed"], shown, n_workers)
    return stats


def _is_rate_limited(msg: str) -> bool:
    """限流判定。各家的措辞不同（429 / rate limit / too many requests / quota），
    所以按关键词匹配而不是认某个异常类 —— 换服务商时不必改代码。

    msg: 异常的类型名 + 描述拼成的字符串（_work 里构造）。
        空串 / None 返回 False（不会误判为限流）。
    """
    low = (msg or "").lower()
    return any(k in low for k in ("429", "rate limit", "ratelimit",
                                  "too many requests", "quota"))


def _attach(chunk: dict, prefix: str) -> None:
    """前缀拼到 text **最前面**：嵌入时它离查询词最近，影响力最大。

    chunk: 单个块 dict，**就地改写** —— 写 ctx_prefix，并把 text 改成 "前缀\\n原文"。
    prefix: 前缀文本。为空串时仍会写入（text 变成 "\\n原文"），
        但调用方 apply 已在写回前过滤掉空前缀。
    """
    chunk["ctx_prefix"] = prefix
    chunk["text"] = f"{prefix}\n{chunk.get('text') or ''}"


def format_stats(stats: dict) -> str:
    """把 apply 的统计 dict 排成一行日志。

    stats: apply() 的返回值，六个键**都硬取**（缺任一个会 KeyError）——
        也就是说只能传 apply 的原样返回，不能手工拼一个残缺 dict。
    """
    return (f"已加前缀 {stats['prefixed']}/{stats['total']} 块"
            f"（缓存命中 {stats['cached']}）；未做：空块 {stats['skipped_empty']}"
            f" / 超上限 {stats['skipped_limit']} / 失败 {stats['failed']}")
