"""
RAG 检索层（检索增强生成）
================================
- 语料：txt/md/log/csv/tsv/json/html/docx/xlsx/pptx（见 src/loaders.py）、pdf（见 src/pdf_parser.py）
- 切分：结构感知（按 Markdown 标题切并把「标题路径」写进块）/ 递归字符切分 二选一
- 检索：tfidf（纯本地关键词）| semantic（embedding 向量）| hybrid（双路 RRF 融合 + 可选 rerank）
- 生成：复用 chatAgent 的 ChatOpenAI + agnes 后端

运行（在项目根目录执行）：
    python -m src.retrieval --docs data --question "你的提问"
    python -m src.retrieval --make-sample

配置（全部走环境变量）：
    RAG_EMBEDDING        检索后端：tfidf（默认）| semantic | hybrid
    RAG_CHUNK_STRATEGY   切分策略：headings（默认，结构感知）| recursive
    RAG_CHUNK_SIZE       块大小（默认 300 字符）
    RAG_CHUNK_OVERLAP    块间重叠（默认 50 字符）
    EMBEDDING_*          embedding 服务（与 LLM 完全分开）
    EMBEDDING_QUERY_PREFIX 查询侧指令前缀（BGE 系模型建议开）
    RERANK_MODEL         rerank 模型（开启后 hybrid 会多一层精排，失败自动回退）

说明：
- **embedding 与 LLM 分开配置**：LLM 走 AGNES_*，embedding 走 EMBEDDING_*。
  （agnes 后端不提供 embeddings 接口，必须另指一个服务）
- 配置缺失时一律「降级 + 告警」，不让服务崩。
- 本文件原名 rag.py，为消除与教程目录 rag/ 的同名冲突而迁入 src/ 并改名。
"""

import os
import re
import glob
import logging
import argparse

# 让 .env 自动生效。
# 注意必须显式给 dotenv_path：load_dotenv() 无参时是按「调用方脚本所在目录」找 .env，
# 用 `python -m src.retrieval` 跑时找不到项目根的 .env，会误报「未设置 API Key」。
try:
    from dotenv import load_dotenv

    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(dotenv_path=os.path.join(_ROOT, ".env"))
except Exception:  # noqa: BLE001  （dotenv 是可选依赖）
    pass

from langchain_openai import ChatOpenAI
from langchain_text_splitters import RecursiveCharacterTextSplitter


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# ---------- LLM 配置（生成用，走 agnes）----------
MODEL_NAME = os.getenv("AGNES_MODEL", "agnes-2.0-flash")
API_KEY = os.getenv("AGNES_API_KEY", "")
BASE_URL = os.getenv("AGNES_API_BASE", "https://apihub.agnes-ai.com/v1")


# ---------- Embedding 配置（检索用，与 LLM 解耦）----------
def _normalize_api_base(url: str) -> str:
    """容错：有人会把完整端点（…/v1/embeddings）整条填进来，
    而 OpenAI 兼容客户端自己还会再拼一次 /embeddings，结果就是 404。
    这里统一裁掉结尾的 /embeddings 和多余斜杠，两种写法都能用。"""
    u = (url or "").strip().rstrip("/")
    if u.endswith("/embeddings"):
        u = u[: -len("/embeddings")].rstrip("/")
    return u


EMBEDDING_API_BASE = _normalize_api_base(os.getenv("EMBEDDING_API_BASE", ""))
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "").strip()
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "").strip()

# 单次请求送多少条文本。各家上限差别很大（百炼约 10 条，硅基流动可更多），
# 这里保守默认 10，语料大时可调大：EMBEDDING_BATCH=64
EMBEDDING_BATCH = max(1, _env_int("EMBEDDING_BATCH", 10))

# 查询侧指令前缀：BGE 系模型的检索效果对前缀敏感（文档侧不加，只加在 query 上）。
# 实测（bge-m3，8 条中文查询 / 6 篇语料）Top1 准确率：
#   无前缀 38% ｜ 中文指令 25% ｜ 英文指令 62% ｜ "检索：" 38%
EMBEDDING_QUERY_PREFIX = os.getenv("EMBEDDING_QUERY_PREFIX", "")


def apply_query_prefix(prefix: str, query: str) -> str:
    """把指令前缀拼到查询前。前缀末尾没写空格时自动补一个，
    免得写 .env 时漏空格导致 'passages:梧州特产' 这种粘连。"""
    if not prefix:
        return query
    return prefix + query if prefix[-1].isspace() else prefix + " " + query

# ---------- 检索后端 ----------
# tfidf（默认，纯本地） | semantic（embedding 向量） | hybrid（双路 RRF 融合）
EMBEDDING_BACKEND = os.getenv("RAG_EMBEDDING", "tfidf")
TOP_K = 3

# 混合检索：每一路各取多少条候选送进融合
HYBRID_POOL = max(1, _env_int("RAG_HYBRID_POOL", 10))
# RRF 的平滑常数（原论文推荐 60），越大越弱化头部排名差异
RRF_K = _env_int("RAG_RRF_K", 60)

# ---------- rerank（可选，失败自动回退）----------
RERANK_API_BASE = _normalize_api_base(os.getenv("RERANK_API_BASE", ""))
RERANK_API_KEY = os.getenv("RERANK_API_KEY", "").strip()
RERANK_MODEL = os.getenv("RERANK_MODEL", "").strip()
RERANK_ENABLED = os.getenv("RAG_RERANK", "1").strip().lower() not in ("0", "false", "off", "no")
RERANK_TIMEOUT = _env_int("RERANK_TIMEOUT", 20)

# ---------- 切分 ----------
# headings：按 Markdown 标题切，并把标题路径写进块（结构化文件收益明显）
# recursive：纯递归字符切分
CHUNK_STRATEGY = os.getenv("RAG_CHUNK_STRATEGY", "headings").strip().lower()
CHUNK_SIZE = _env_int("RAG_CHUNK_SIZE", 300)
CHUNK_OVERLAP = _env_int("RAG_CHUNK_OVERLAP", 50)

# TF-IDF 分词参数：中文没有空格，默认的 word 分词会把整句当成一个 token，
# 导致查询词与语料词表完全匹配不上（实测全部 0 命中）。改用字符 n-gram，中英文都友好。
TFIDF_ANALYZER = "char_wb"
TFIDF_NGRAM_RANGE = (2, 4)


# ---------------------------------------------------------------------------
# 1. 加载与切分
# ---------------------------------------------------------------------------
def load_documents(doc_folder: str):
    """读取目录下所有 .txt / .md 文件，返回 [(文件名, 文本), ...]

    仅用于纯文本场景；多格式请用 load_chunks。目录不存在时返回空列表。
    """
    if not doc_folder or not os.path.isdir(doc_folder):
        return []
    texts = []
    files = glob.glob(os.path.join(doc_folder, "**", "*"), recursive=True)
    for f in files:
        if f.lower().endswith((".txt", ".md")):
            with open(f, "r", encoding="utf-8") as fh:
                texts.append((os.path.basename(f), fh.read()))
    return texts


def _make_splitter(chunk_size, chunk_overlap):
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap
    )


def split_documents(docs, chunk_size=None, chunk_overlap=None):
    """纯文本递归切分：[(name, text)] -> [{"source","section","text"}]"""
    chunk_size = CHUNK_SIZE if chunk_size is None else chunk_size
    chunk_overlap = CHUNK_OVERLAP if chunk_overlap is None else chunk_overlap
    splitter = _make_splitter(chunk_size, chunk_overlap)
    chunks = []
    for name, text in docs:
        for piece in splitter.split_text(text):
            chunks.append({"source": name, "section": "", "text": piece})
    return chunks


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def split_by_headings(text, chunk_size=None, chunk_overlap=None):
    """结构感知切分：按 Markdown 标题分段，并把「标题路径」写进块内与元数据。

    为什么这么做：整篇文档切成一个大块时，多个主题混在一起会把语义匹配稀释掉
    （实测整个工作表当成一块时排名会错乱）。按标题切之后，每个块聚焦一个主题，
    且块开头带上「梧州特产 > 六堡茶」这样的路径，即使正文没重复主题词也能被检索到。

    返回 [{"section": "父 > 子", "text": "【父 > 子】\\n正文..."}]
    """
    chunk_size = CHUNK_SIZE if chunk_size is None else chunk_size
    chunk_overlap = CHUNK_OVERLAP if chunk_overlap is None else chunk_overlap
    splitter = _make_splitter(chunk_size, chunk_overlap)

    out: list[dict] = []
    stack: list[tuple[int, str]] = []
    body: list[str] = []

    def emit(body_lines, stack_snapshot):
        content = "\n".join(body_lines).strip()
        if not content:
            return
        section = " > ".join(t for _, t in stack_snapshot)
        pieces = splitter.split_text(content) if len(content) > chunk_size else [content]
        for p in pieces:
            out.append({
                "section": section,
                "text": (f"【{section}】\n{p}" if section else p),
            })

    for raw in text.split("\n"):
        m = _HEADING_RE.match(raw.strip())
        if m:
            emit(body, stack)
            body = []
            level, title = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
        else:
            body.append(raw)
    emit(body, stack)
    return out


def _split_text(text, chunk_size, chunk_overlap):
    """按配置的策略切分一段文本，返回 [{"section","text"}]。"""
    if CHUNK_STRATEGY == "headings":
        pieces = split_by_headings(text, chunk_size, chunk_overlap)
        if pieces:
            return pieces
    return [{"section": "", "text": p}
            for p in _make_splitter(chunk_size, chunk_overlap).split_text(text)]


def load_chunks(doc_folder, chunk_size=None, chunk_overlap=None, pdf_chunk_size=800,
                pdf_overlap=200):
    """语料读取总入口：目录 → [{"source","section","text"}]。

    支持格式见 src/loaders.py；.pdf 走 src/pdf_parser.py 的专门链路。
    单个文件解析失败只跳过并告警，不影响其余语料。
    """
    if not doc_folder or not os.path.isdir(doc_folder):
        return []

    chunk_size = CHUNK_SIZE if chunk_size is None else chunk_size
    chunk_overlap = CHUNK_OVERLAP if chunk_overlap is None else chunk_overlap

    from .pdf_parser import pdf_to_chunks
    from .loaders import load_text, supported_extensions

    exts = supported_extensions()
    chunks: list[dict] = []
    skipped: dict[str, int] = {}

    files = sorted(glob.glob(os.path.join(doc_folder, "**", "*"), recursive=True))
    for f in files:
        if not os.path.isfile(f):
            continue
        low = f.lower()
        name = os.path.basename(f)

        # ---- PDF：走教程那条专门链路 ----
        if low.endswith(".pdf"):
            try:
                pieces = pdf_to_chunks(f, chunk_size=pdf_chunk_size, overlap_size=pdf_overlap)
                chunks += [{"source": name, "section": "", "text": p} for p in pieces]
                logging.info("PDF 解析完成：%s（%d 个片段）", name, len(pieces))
            except Exception as e:  # noqa: BLE001
                logging.warning("PDF 解析失败，跳过 %s: %s", name, e)
            continue

        # ---- 其它格式：统一走 loaders ----
        ext = os.path.splitext(low)[1]
        if ext not in exts:
            skipped[ext] = skipped.get(ext, 0) + 1
            continue

        text = load_text(f)
        if not text.strip():
            continue
        for p in _split_text(text, chunk_size, chunk_overlap):
            chunks.append({"source": name, "section": p["section"], "text": p["text"]})

    if skipped:
        logging.warning(
            "以下扩展名暂不支持，已跳过：%s",
            ", ".join(f"{e or '(无扩展名)'}×{n}" for e, n in sorted(skipped.items())),
        )

    return chunks


# ---------------------------------------------------------------------------
# 2. 检索器
# ---------------------------------------------------------------------------
class TfidfRetriever:
    """稀疏检索：sklearn TF-IDF 字符 n-gram（纯本地，无外部依赖）。"""

    def __init__(self, chunks):
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        self.chunks = chunks
        self.cosine = cosine_similarity
        self.vectorizer = TfidfVectorizer(
            analyzer=TFIDF_ANALYZER, ngram_range=TFIDF_NGRAM_RANGE
        )
        self.matrix = self.vectorizer.fit_transform([c["text"] for c in chunks])

    def search(self, query, top_k=TOP_K):
        q = self.vectorizer.transform([query])
        sims = self.cosine(q, self.matrix)[0]
        idx = sims.argsort()[::-1][:top_k]
        return [(self.chunks[i], float(sims[i])) for i in idx if sims[i] > 0]


class EmbeddingRetriever:
    """稠密检索：走独立的 EMBEDDING_* 配置（OpenAI 兼容 /v1/embeddings）。

    语料不大时直接 numpy 暴力算余弦即可，不需要引入向量数据库。
    """

    def __init__(self, chunks):
        from langchain_openai import OpenAIEmbeddings
        import numpy as np

        self.chunks = chunks
        self.np = np
        self.embedder = OpenAIEmbeddings(
            model=EMBEDDING_MODEL,
            api_key=EMBEDDING_API_KEY,
            base_url=EMBEDDING_API_BASE or None,
            chunk_size=EMBEDDING_BATCH,   # 按各家单次输入上限分批，避免 400
            max_retries=3,
        )
        self.vectors = self.embedder.embed_documents([c["text"] for c in chunks])

    def search(self, query, top_k=TOP_K):
        # 查询侧加指令前缀（文档侧不加），BGE 系模型靠这个拉开区分度
        q = self.embedder.embed_query(apply_query_prefix(EMBEDDING_QUERY_PREFIX, query))
        sims = [self._cos(q, v) for v in self.vectors]
        order = self.np.argsort(sims)[::-1][:top_k]
        return [(self.chunks[i], float(sims[i])) for i in order]

    @staticmethod
    def _cos(a, b):
        import numpy as np

        a, b = np.array(a), np.array(b)
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


class NullRetriever:
    """无语料 / 未配置 DOCS_DIR 时的空检索器：永远返回空结果。

    作用是把「没配知识库」变成正常降级，而不是让 TF-IDF 在空语料上抛
    ValueError（empty vocabulary）之后把整张图带崩。
    """

    def search(self, query, top_k=TOP_K):
        return []


# ---------------------------------------------------------------------------
# 3. 混合检索（RRF 融合 + 可选 rerank）
# ---------------------------------------------------------------------------
def _chunk_key(chunk):
    return (chunk.get("source", ""), chunk.get("section", ""), chunk.get("text", ""))


def rrf_fuse(rank_lists, k=None):
    """Reciprocal Rank Fusion：只用排名、不用绝对分。

    score = Σ 1/(k + rank)。好处是天然规避两个问题：
      1) 不同检索器分数量纲不可比（TF-IDF 余弦 vs embedding 余弦）；
      2) bge-m3 的相似度被压缩在 0.5~0.7，绝对阈值根本没法用。
    """
    k = RRF_K if k is None else k
    scores: dict[tuple, float] = {}
    keep: dict[tuple, dict] = {}
    for lst in rank_lists:
        for rank, (chunk, _score) in enumerate(lst, start=1):
            key = _chunk_key(chunk)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            keep.setdefault(key, chunk)
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [(keep[key], value) for key, value in ordered]


class RerankClient:
    """调用服务方的 /rerank 接口（硅基流动、百炼等均提供）。

    注意 rerank 不是 OpenAI 兼容标准的一部分，各家的请求体基本一致但仍有差异，
    因此这里失败一律返回 None，由调用方回退到融合顺序，绝不让检索因精排挂掉。
    """

    def __init__(self, base, key, model, timeout=None):
        self.url = (base or "").rstrip("/") + "/rerank"
        self.key = key
        self.model = model
        self.timeout = timeout or RERANK_TIMEOUT

    def rerank(self, query, chunks, top_k):
        import httpx

        resp = httpx.post(
            self.url,
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json"},
            json={"model": self.model, "query": query,
                  "documents": [c["text"] for c in chunks], "top_n": top_k},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        out = []
        for item in results:
            i = int(item.get("index", -1))
            if 0 <= i < len(chunks):
                out.append((chunks[i], float(item.get("relevance_score", 0.0))))
        return out or None


def rerank_configured() -> bool:
    return bool(RERANK_MODEL and (RERANK_API_KEY or EMBEDDING_API_KEY))


class HybridRetriever:
    """稀疏（关键词）+ 稠密（向量）双路召回 → RRF 融合 → 可选 rerank。

    为什么值得做：中文场景下关键词能抓住精确名称/编号（六堡茶、1897年），
    向量能抓住改写与近义（"怎么去广州" ≈ "高铁耗时"），两者漏掉的样本不一样。
    """

    def __init__(self, chunks, sparse=None, dense=None, reranker=None, pool=None):
        self.chunks = chunks
        self.sparse = sparse if sparse is not None else TfidfRetriever(chunks)
        self.dense = dense if dense is not None else EmbeddingRetriever(chunks)
        self.reranker = reranker
        self.pool = pool or HYBRID_POOL
        self.last_rerank_ok = None

    def search(self, query, top_k=TOP_K):
        sparse_hits = self.sparse.search(query, top_k=self.pool)
        dense_hits = self.dense.search(query, top_k=self.pool)
        fused = rrf_fuse([sparse_hits, dense_hits])

        if self.reranker is not None:
            candidates = [c for c, _ in fused[: max(top_k * 3, 10)]]
            try:
                reranked = self.reranker.rerank(query, candidates, top_k)
                self.last_rerank_ok = bool(reranked)
                if reranked:
                    return reranked
            except Exception as e:  # noqa: BLE001
                self.last_rerank_ok = False
                logging.warning("rerank 失败，回退融合顺序：%s", e)

        return fused[:top_k]


def embedding_configured() -> bool:
    """embedding 配置是否已填写（API base 可留空，走服务方默认地址）。"""
    return bool(EMBEDDING_API_KEY and EMBEDDING_MODEL)


def build_retriever(chunks):
    """按 RAG_EMBEDDING 选择检索后端；embedding 没配好则自动回退 TF-IDF。"""
    if not chunks:
        logging.info("RAG 语料为空，使用 NullRetriever（检索将返回空结果）")
        return NullRetriever()

    backend = (EMBEDDING_BACKEND or "tfidf").strip().lower()
    if backend in ("semantic", "openai", "hybrid") and not embedding_configured():
        logging.warning(
            "RAG_EMBEDDING=%s 需要 embedding，但 EMBEDDING_API_KEY / EMBEDDING_MODEL "
            "尚未填写，本次自动回退 TF-IDF。",
            backend,
        )
        backend = "tfidf"

    if backend == "hybrid":
        reranker = None
        if RERANK_ENABLED and rerank_configured():
            reranker = RerankClient(
                RERANK_API_BASE or EMBEDDING_API_BASE,
                RERANK_API_KEY or EMBEDDING_API_KEY,
                RERANK_MODEL,
            )
            logging.info("RAG 检索后端：hybrid + rerank（%s）", RERANK_MODEL)
        else:
            logging.info("RAG 检索后端：hybrid（无 rerank），池大小=%d", HYBRID_POOL)
        return HybridRetriever(chunks, reranker=reranker)

    if backend in ("semantic", "openai"):
        logging.info("RAG 检索后端：semantic（embedding 模型=%s）", EMBEDDING_MODEL)
        return EmbeddingRetriever(chunks)

    return TfidfRetriever(chunks)


# ---------------------------------------------------------------------------
# 4. 生成
# ---------------------------------------------------------------------------
def build_prompt(question, contexts):
    """contexts: [({"source","section","text"}, score), ...]"""
    ctx = "\n\n".join(
        f"[来源: {c['source']}]\n{c['text']}" for c, _ in contexts
    )
    return (
        "你是一个严谨的问答助手。请只根据下面提供的【参考资料】回答用户问题，"
        "如果资料里没有相关信息，就如实说“资料中未提及”。不要编造。\n\n"
        f"【参考资料】\n{ctx}\n\n【用户问题】\n{question}\n\n【回答】"
    )


def answer(question, retriever, model):
    contexts = retriever.search(question)
    if not contexts:
        return "（未在资料中找到相关内容）", []
    prompt = build_prompt(question, contexts)
    resp = model.invoke(prompt)
    return resp.content, contexts


# ---------------------------------------------------------------------------
# 5. 命令行
# ---------------------------------------------------------------------------
def make_sample_docs(folder="data"):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "intro.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "梧州是广西壮族自治区的一个地级市，地处广西东部，靠近广东。\n"
            "梧州以人工宝石产业闻名，被称为“世界人造宝石之都”。\n"
            "梧州代表性景点有骑楼城、龙母庙、白云山。\n"
            "梧州特产有龟苓膏、冰泉豆浆、六堡茶。六堡茶是黑茶的一种，有祛湿功效。\n"
            "梧州气候属亚热带季风气候，夏季炎热多雨，冬季温和少雨。\n"
        )
    return path


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    parser = argparse.ArgumentParser(description="简单 RAG 示例")
    parser.add_argument("--docs", default="data", help="文档目录")
    parser.add_argument("--question", default=None, help="提问（不填用内置示例）")
    parser.add_argument("--make-sample", action="store_true", help="生成示例文档")
    args = parser.parse_args()

    if args.make_sample:
        p = make_sample_docs(args.docs)
        print(f"已生成示例文档：{p}")

    if not API_KEY:
        raise SystemExit("请先设置环境变量 AGNES_API_KEY（生成用的 LLM Key）")

    chunks = load_chunks(args.docs)
    if not chunks:
        print(f"{args.docs} 下没有可用文档，正在生成示例...")
        make_sample_docs(args.docs)
        chunks = load_chunks(args.docs)

    print(f"已加载语料，共 {len(chunks)} 个片段"
          f"（切分策略={CHUNK_STRATEGY}，块大小={CHUNK_SIZE}）。")

    model = ChatOpenAI(model=MODEL_NAME, api_key=API_KEY, base_url=BASE_URL)
    retriever = build_retriever(chunks)
    print(f"检索后端：{type(retriever).__name__}"
          f"（RAG_EMBEDDING={EMBEDDING_BACKEND}，"
          f"embedding 已配置={embedding_configured()}，"
          f"查询前缀={'有' if EMBEDDING_QUERY_PREFIX else '无'}）\n")

    question = args.question or "梧州有什么特产？六堡茶是什么茶？"
    print(f"问题：{question}\n")
    ans, ctx = answer(question, retriever, model)
    print("回答：")
    print(ans)
    print("\n（引用片段来源）")
    for (c, score) in ctx:
        sec = f"  [{c['section']}]" if c.get("section") else ""
        print(f"  - {c['source']}{sec}  分数={score:.4f}")


if __name__ == "__main__":
    main()
