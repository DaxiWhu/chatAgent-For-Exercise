"""
RAG 检索层（检索增强生成）
================================
- 语料：txt/md/log/csv/tsv/json/html/docx/xlsx/pptx（见 src/loaders.py）、pdf（见 src/pdf_parser.py）
- 切分：结构感知（按 Markdown 标题切并把「标题路径」写进块）/ 递归字符切分 二选一
- 检索：bm25（纯本地关键词，Apache Lucene 的 BM25Similarity 口径）| semantic（embedding 向量）| hybrid（双路 RRF 融合 + 可选 rerank）
- 生成：复用 chatAgent 的 ChatOpenAI + agnes 后端

运行（在项目根目录执行）：
    python -m src.rag.retrieval --docs data --question "你的提问"
    python -m src.rag.retrieval --make-sample

配置（全部走环境变量）：
    RAG_EMBEDDING        检索后端：bm25（默认）| semantic | hybrid（历史值 tfidf 仍接受，等价 bm25）
    RAG_BM25_K1 / _B     BM25 参数（默认 1.2 / 0.75，即 Lucene 默认；越界值回退默认并告警）
    RAG_CHUNK_STRATEGY   切分策略：headings（默认，结构感知）| recursive
    RAG_CHUNK_SIZE       块大小（默认 300 字符）
    RAG_CHUNK_OVERLAP    块间重叠（默认 50 字符）
    EMBEDDING_*          embedding 服务（与 LLM 完全分开）
    EMBEDDING_QUERY_PREFIX 查询侧指令前缀（BGE 系模型建议开）
    RERANK_MODEL         rerank 模型（开启后 hybrid 会多一层精排，失败自动回退）
    RAG_VECTOR_STORE     稠密向量存储：memory（默认，numpy 暴力）| pg（pgvector）
                         | qdrant（Qdrant：**稀疏 BM25 + 稠密同库**，见 src/qdrant_store.py）
    QDRANT_URL/_API_KEY/_COLLECTION/_DIM  仅 RAG_VECTOR_STORE=qdrant 需要

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
# 用 `python -m src.rag.retrieval` 跑时找不到项目根的 .env，会误报「未设置 API Key」。
try:
    from dotenv import load_dotenv

    # 项目根 = 上溯 3 层（src/rag/x.py → src/rag → src → 根）。少一层会指向
    # src/.env（并不存在）而让下面的加载**静默失效**（2026-09-28 迁入 src/rag/ 时踩过）。
    _ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    load_dotenv(dotenv_path=os.path.join(_ROOT, ".env"))
except Exception:  # noqa: BLE001  （dotenv 是可选依赖）
    pass

from langchain_openai import ChatOpenAI
from langchain_text_splitters import RecursiveCharacterTextSplitter

from .vector_store import PgVectorStore, chunk_fingerprint
from .qdrant_store import QdrantStore, Bm25SparseEncoder, point_id
from .kb_inventory import stamp_source
from . import index_identity
from .resilience import CircuitBreaker, guarded_call


def _env_int(name, default):
    """读环境变量并转 int，转不动就退回默认值。

    name: 环境变量名。
    default: 未设置或值非法（跑不出 int）时的回退值。
    """
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name, default, lo, hi):
    """读环境变量并转 float，**转不动或越界都退回默认值**（越界时告警）。

    name: 环境变量名。
    default: 未设置或值非法时的回退值。
    lo / hi: 闭区间定义域（含端点）。

    为什么不越界就直接用：BM25 的 k1 / b 都有明确定义域（Lucene 是 k1∈[0,3]、b∈[0,1]），
    越界值不会让程序崩，但会让"我明明调了 b=2"变成一个**没有任何症状**的悬案 ——
    分照算、结果照出，只是不是你以为的那个公式。所以这里明确回退 + 告警。
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        logging.warning("%s=%r 不是数字，回退默认值 %s", name, raw, default)
        return default
    if not (lo <= value <= hi):
        logging.warning("%s=%s 超出定义域 [%s, %s]，回退默认值 %s",
                        name, value, lo, hi, default)
        return default
    return value


# ---------- LLM 配置（生成用，走 agnes）----------
MODEL_NAME = os.getenv("AGNES_MODEL", "agnes-2.0-flash")
API_KEY = os.getenv("AGNES_API_KEY", "")
BASE_URL = os.getenv("AGNES_API_BASE", "https://apihub.agnes-ai.com/v1")


# ---------- Embedding 配置（检索用，与 LLM 解耦）----------
def _normalize_api_base(url: str) -> str:
    """统一 API base 的写法，容忍"把完整端点也填进来"这种常见错误。

    url: 环境变量里填的服务地址，可能带尾斜杠、也可能把 /embeddings 一起写进来。
        空串 / None 返回空串（表示"用服务方默认地址"）。

    容错：有人会把完整端点（…/v1/embeddings）整条填进来，
    而 OpenAI 兼容客户端自己还会再拼一次 /embeddings，结果就是 404。
    这里统一裁掉结尾的 /embeddings 和多余斜杠，两种写法都能用。
    """
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
    """把指令前缀拼到查询前。

    prefix: 指令前缀（环境变量 EMBEDDING_QUERY_PREFIX）。空串时原样返回 query。
    query: 用户查询。

    前缀末尾没写空格时自动补一个，免得写 .env 时漏空格导致 'passages:梧州特产' 这种粘连。
    """
    if not prefix:
        return query
    return prefix + query if prefix[-1].isspace() else prefix + " " + query

# ---------- 检索后端 ----------
# bm25（默认，纯本地关键词） | semantic（embedding 向量） | hybrid（双路 RRF 融合）
# ⚠️ 取值 tfidf 是**兼容别名**：它是换 BM25 之前的历史配置值，仍被接受（等价 bm25），
#    读到时会打一条 WARNING。之所以留别名而不是直接删：这个值写在 .env 里，属于
#    **对外契约**，删掉会让存量部署静默落进"其余值 → 兜底"分支 —— 行为恰好一样、
#    所以更难发现，只是日志语义混乱。
EMBEDDING_BACKEND = os.getenv("RAG_EMBEDDING", "bm25")
TOP_K = 3

# ---------- 向量存储（稠密向量落库；Qdrant 还可同库承载稀疏）----------
# memory（默认，进程内 numpy 暴力余弦） | pg（PostgreSQL + pgvector 扩展）
#   | qdrant（Qdrant：稠密 + 稀疏同库，见 src/qdrant_store.py）
# 默认 memory 是为了让"没配数据库 / 没装扩展"的环境开箱可用，且不改变既有行为；
# 生产环境请在 .env 里显式设 RAG_VECTOR_STORE=pg（或 qdrant）—— 否则每次启动都会重算 embedding。
DB_URL = os.getenv("DB_URL", "").strip()
VECTOR_STORE = os.getenv("RAG_VECTOR_STORE", "memory").strip().lower()
PGVECTOR_TABLE = os.getenv("PGVECTOR_TABLE", "rag_chunks").strip() or "rag_chunks"
# hnsw（默认，召回好）| ivfflat（省内存）| none（不建向量索引，仅小数据量/测试用）
PGVECTOR_INDEX = os.getenv("PGVECTOR_INDEX", "hnsw").strip().lower()
# 多租户：非空时 pgvector 的读写都被锁进该租户（详见 vector_store.PgVectorStore）。
# 空串 = 不隔离，与既有数据/行为完全一致。
RAG_TENANT = os.getenv("RAG_TENANT", "").strip()

# ---------- Qdrant 后端（RAG_VECTOR_STORE=qdrant）----------
# 与 pg 后端的差别：**稀疏（BM25）与稠密（embedding）同库**，一个 collection 两路召回，
# 融合仍由本文件的 rrf_fuse 完成（见 qdrant_store.py 顶部说明）。
QDRANT_URL = os.getenv("QDRANT_URL", "").strip()
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "").strip()
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "rag_chunks").strip() or "rag_chunks"
# 稠密维度（bge-m3 = 1024）。只影响首次建集合；集合已存在时以服务端为准并校验一致。
QDRANT_DIM = max(1, _env_int("QDRANT_DIM", 1024))
# 稀疏向量的哈希维度（默认 2^20）：越大碰撞越少、内存略增。改它会使已有稀疏向量失效，
# 需换集合名重灌（与换 embedding 模型同一条纪律）。
QDRANT_SPARSE_DIM = max(1024, _env_int("QDRANT_SPARSE_DIM", 1 << 20))
QDRANT_HNSW_M = max(4, _env_int("QDRANT_HNSW_M", 16))
QDRANT_HNSW_EF_CONSTRUCT = max(4, _env_int("QDRANT_HNSW_EF_CONSTRUCT", 100))
# 向量是否落盘（省内存、检索变慢）。大语料/内存受限时置 1。
QDRANT_ON_DISK = os.getenv("QDRANT_ON_DISK", "0").strip().lower() in ("1", "on", "true", "yes")
QDRANT_TIMEOUT = max(1, _env_int("QDRANT_TIMEOUT", 10))


def resolve_tenant(tenant=None) -> str:
    """把"请求期租户"与"进程级兜底租户"归一成最终用于检索隔离的值。

    为什么需要这个函数
    -----------------
    改造前只有 `RAG_TENANT` 一个来源，而它是**模块导入时读一次**的常量 ——
    于是"多租户"这件事在检索层事实上不存在：请求 A（租户 acme）与请求 B
    （租户 globex）查到的是**同一批**数据。真正的隔离只发生在会话/审核/配额上，
    向量层是漏的。

    这个函数把两条来源收敛到一处，供 `PgVectorRetriever` / `build_retriever`
    调用 —— 免得"哪一层该用哪个值"在每个调用点各判一次（那种写法迟早分叉）。

    tenant: 请求期租户。None 表示"调用方不关心"（CLI、脚本、原有调用点），
        此时退回模块级 RAG_TENANT，**保证改造前后行为一致**。
        传空串表示"显式要求不隔离"。

    返回：最终租户字符串（已 strip）；可能是空串（=不隔离）。
    """
    if tenant is None:
        return RAG_TENANT
    return (tenant or "").strip()

# ---------- 出站调用熔断（rerank / embedding）----------
# 连续失败达阈值即"快速失败"，隔 reset 秒放一个试探请求。默认开；
# 关掉后行为与加这层之前一致（每次都真的发请求）。
CIRCUIT_ENABLED = os.getenv("RAG_BREAKER", "1").strip().lower() not in ("0", "off", "false", "no")
CIRCUIT_FAIL_THRESHOLD = _env_int("CIRCUIT_FAIL_THRESHOLD", 5)
CIRCUIT_RESET_SECONDS = _env_int("CIRCUIT_RESET_SECONDS", 30)

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

# 递归切分的分隔符优先级。
# LangChain 默认只有 ["\n\n", "\n", " ", ""] —— 中文没有空格，无换行的长段落会从 " "
# 直接掉到 "" 逐字符凑满切断，出现句中硬切。补上中文标点后，在到达字符级之前就有断点可用。
# 注意：本常量服务的是非 PDF 的文本格式（.md/.txt/.html 等）；PDF 走 pdf_parser 的
# 句子级打包链路，不经过这里。
SPLIT_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""]

# ---------- 文档解析（Docling 为主，pdfminer 自动降级）----------
# 解析链路见 src/docling_loader.py。PDF 的正文切分由 Docling 的 HybridChunker 负责，
# 它按 token 计预算，与上面按字符计的 RAG_CHUNK_SIZE 是两套单位，互不覆盖。
DOC_PARSER = os.getenv("RAG_DOC_PARSER", "docling").strip().lower()
PDF_CHUNK_SIZE = _env_int("RAG_PDF_CHUNK_SIZE", 800)
PDF_CHUNK_OVERLAP = _env_int("RAG_PDF_CHUNK_OVERLAP", 200)

# ---------- 父子块 / Contextual Retrieval（批次8）----------
# 检索命中的是**小块**（精度高），但送进模型的应该是它所在的**上下文窗口**，
# 否则答案常因缺上文而断裂。这条不改召回排序，改的是"给模型看什么"。
PARENT_CHILD = os.getenv("RAG_PARENT_CHILD", "on").strip().lower() not in ("0", "off", "false", "no")
PARENT_MAX_CHARS = max(200, _env_int("RAG_PARENT_MAX_CHARS", 1200))

# Contextual Retrieval（Anthropic）：给每块加一句"它在整篇文档里的位置与主题"前缀。
# 这是切分侧唯一**显著花钱**的改进（每块一次 LLM 调用），所以默认关。
CONTEXTUAL = os.getenv("RAG_CONTEXTUAL", "off").strip().lower() in ("1", "on", "true", "yes")

# 引用（citations）里每条的原文节选长度。状态会被 checkpointer 持久化，
# 塞进整块原文会让会话状态线性膨胀，所以默认只给"够定位 + 能核对"的片段。
CITE_SNIPPET_CHARS = max(100, _env_int("RAG_CITE_SNIPPET_CHARS", 1000))

# ---------- 稀疏检索：BM25 分词口径 ----------
# 沿用换公式之前的**字符 n-gram**：中文没有空格，sklearn 默认的 word 分词会把整句
# 当成一个 token，查询词与语料词表完全匹配不上（实测全部 0 命中）。改用字符 n-gram，
# 中英文都友好。⚠️ 换打分公式时**刻意不动分词口径** —— 一次只改一个变量，
# 这样召回率的变化才能唯一归因到 BM25，而不是"分词和打分一起动、说不清是谁的功劳"。
#    （Lucene 对中文的官方做法是 StandardTokenizer 单字切 + CJKBigramFilter 组双字。
#      char_wb 2~4-gram 是它的超集口径：同样不做词级分词、不依赖词典，但把三元四元
#      也纳入，专名如「六堡茶」「1897年」不必依赖分词器就能命中。）
BM25_ANALYZER = "char_wb"
BM25_NGRAM_RANGE = (2, 4)

# k1：词频饱和参数（越大，一个词在同一篇里重复出现还能继续加分）。Lucene 默认 1.2，定义域 [0,3]。
# b：文档长度归一化强度（0=不归一，1=完全按长度惩罚）。Lucene 默认 0.75，定义域 [0,1]。
BM25_K1 = _env_float("RAG_BM25_K1", 1.2, 0.0, 3.0)
BM25_B = _env_float("RAG_BM25_B", 0.75, 0.0, 1.0)


# ---------------------------------------------------------------------------
# 1. 加载与切分
# ---------------------------------------------------------------------------
def load_documents(doc_folder: str):
    """读取目录下所有 .txt / .md 文件，返回 [(文件名, 文本), ...]

    doc_folder: 语料目录。空值或目录不存在时返回 []（不抛异常）。

    仅用于纯文本场景；多格式请用 load_chunks。
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
    """造一个递归字符切分器（分隔符固定用 SPLIT_SEPARATORS）。

    chunk_size: 块长度上限（字符）。
    chunk_overlap: 相邻块的重叠长度（字符）。
    """
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=SPLIT_SEPARATORS,
    )


def split_documents(docs, chunk_size=None, chunk_overlap=None):
    """纯文本递归切分：[(name, text)] -> [{"source","section","text"}]

    docs: load_documents 的产物，元素为 (文件名, 全文) 二元组。
    chunk_size: 块长度上限。None 时取环境变量 RAG_CHUNK_SIZE。
    chunk_overlap: 块间重叠。None 时取环境变量 RAG_CHUNK_OVERLAP。

    产出恒带 "section": ""（纯递归切分没有标题信息）。
    """
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

    text: 单份文档的全文（Markdown 形态，`#` 开头的行为标题）。
    chunk_size: 块长度上限。None 时取 RAG_CHUNK_SIZE；只对超过该值的段再切。
    chunk_overlap: 重叠长度，透传给内层递归切分器。None 时取 RAG_CHUNK_OVERLAP。

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
        """把缓冲区里的正文落成一个或多个块（标题切换与结尾各调一次）。

        body_lines: 当前缓冲区的正文行列表。
        stack_snapshot: 调用时刻的标题栈快照 —— 传快照而不是引用，
            避免后续 pop/append 把已产出块的 section 改掉。
        """
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
    """按配置的策略切分一段文本，返回 [{"section","text"}]。

    text: 待切分的单份文档全文。
    chunk_size: 块长度上限。
    chunk_overlap: 块间重叠。

    策略取模块级 CHUNK_STRATEGY：headings 优先，切不出东西时（如纯文本无标题）
    自动落到递归切分，所以调用方不必自己判策略。
    """
    if CHUNK_STRATEGY == "headings":
        pieces = split_by_headings(text, chunk_size, chunk_overlap)
        if pieces:
            return pieces
    return [{"section": "", "text": p}
            for p in _make_splitter(chunk_size, chunk_overlap).split_text(text)]


def _split_text_texts(text, chunk_size, chunk_overlap):
    """同 _split_text，但只返回文本片段。

    text: 待切分的文本（通常是某个已超长的块自身内容）。
    chunk_size: 块长度上限。
    chunk_overlap: 块间重叠。

    供 `from_blocks` 的二次切分用：那边只关心"这一块太长，拆成几段"，
    section/page 由块自己带着，不该被切分器覆盖掉。
    """
    return [p["text"] for p in _split_text(text, chunk_size, chunk_overlap)]


def _enrich_images(chunks, name):
    """给需要描述的图补 VLM 描述（批次 6）。默认关闭，开启后只处理"确定性来源拿不到"的图。

    chunks: 待完善的块列表（KnowledgeChunk 或 dict 均可，由 image_describer 决定），
        **就地改写**。空列表直接返回。
    name: 文件名，仅用于日志定位是哪份语料触发的。

    为什么放在这里而不是 docling_loader：这是**产物完善**步骤，与解析后端无关
    （PDF 裁剪图与 docx 媒体走同一条），放进解析层会让后端多背一个不该有的依赖。
    任何失败都只记日志，绝不影响已经解析出来的内容。
    """
    if not chunks:
        return
    try:
        from . import image_describer

        est = image_describer.estimate(chunks)
        if est["needs_vlm"]:
            # 先把"待描述总量与预估 token"打出来：按图计费，量要在日志里可见
            logging.info("图片 VLM 待办：%s（%s）", image_describer.format_estimate(est), name)
        image_describer.enrich(chunks)
    except Exception as e:  # noqa: BLE001
        logging.warning("图片 VLM 描述步骤异常（不影响已解析内容）：%s（%s: %s）",
                        name, type(e).__name__, e)


def _parse_pdf(path, name, chunk_size, overlap):
    """PDF 解析：Docling 优先，不可用或失败时自动回退 pdfminer。

    path: PDF 文件的绝对/相对路径。
    name: 文件名（basename），作为产物里的 source。
    chunk_size: **仅降级链路使用** —— 透传给 pdf_parser.pdf_to_chunks 作为字符级块长；
        Docling 路径不读这个参数（它按 token 计预算）。
    overlap: 同上，降级链路的块间重叠。

    两条链路产出同构的 dict（都经 chunk_writer.to_legacy 出边界），检索器与 graph
    不必区分来源；dict 里的 parser / origin 字段保留线索，便于排查
    "这份文件其实走的是降级链路"。
    """
    from .chunk_writer import to_legacy_many

    if DOC_PARSER == "docling":
        out = None
        try:
            from . import docling_loader
            out = docling_loader.parse_pdf(path, name)
        except Exception as e:  # noqa: BLE001  解析层的任何意外都不该让整批语料挂掉
            logging.error("Docling 解析异常，回退 pdfminer：%s（%s: %s）",
                          name, type(e).__name__, e)
        else:
            if out.status in ("unavailable", "failed"):
                logging.warning("Docling 不可用，回退 pdfminer：%s（%s）", name, out.reason)
            else:
                if out.status == "unprocessable":
                    # 「解析不出内容」不等于「解析器坏了」：降级到 pdfminer 只会更差，
                    # 所以不降级，而是保留这条显式失败记录，让它可被检索到。
                    logging.warning("Docling 未解析出内容，保留失败记录：%s（%s）",
                                    name, out.reason)
                _enrich_images(out.chunks, name)
                return to_legacy_many(out.chunks)

    from .knowledge_chunk import from_pdfminer_pieces
    from .pdf_parser import pdf_to_chunks

    pieces = pdf_to_chunks(path, chunk_size=chunk_size, overlap_size=overlap)
    return to_legacy_many(from_pdfminer_pieces(pieces, name))


def _parse_docx(path, name, chunk_size, overlap):
    """docx 解析：Docling 双路线优先，不可用或失败时回退 python-docx。

    path: docx 文件路径。
    name: 文件名（basename），作为产物里的 source。
    chunk_size: 切块长度，只用于**降级链路**（_parse_docx_legacy 的通用切分）。
    overlap: 同上，降级链路的块间重叠。

    与 PDF 那条支路的差别在**降级的代价**：docx 本身是结构化 markup，
    python-docx 提正文并不比 Docling 差多少；真正的差距是表格、图片、原生图表、OLE ——
    那些只有「Docling docx 后端 + 解 zip」组合才拿得到（见 src/docx_media.py）。
    所以这里降级损失的比 PDF 小，但语义保持一致：降级必须留痕（parser 字段 + 日志），
    不能让"这份文件其实走的是老链路"变成一件查不出来的事。
    """
    from .chunk_writer import to_legacy_many

    if DOC_PARSER == "docling":
        out = None
        try:
            from . import docling_loader
            out = docling_loader.parse_docx(path, name)
        except Exception as e:  # noqa: BLE001  解析层的任何意外都不该让整批语料挂掉
            logging.error("Docling docx 解析异常，回退 python-docx：%s（%s: %s）",
                          name, type(e).__name__, e)
        else:
            if out.status in ("unavailable", "failed"):
                logging.warning("Docling 不可用，回退 python-docx：%s（%s）", name, out.reason)
            else:
                if out.status == "unprocessable":
                    # 同 PDF：降级到 python-docx 只会更差，保留显式失败记录让它可被检索到
                    logging.warning("Docling 未解析出 docx 内容，保留失败记录：%s（%s）",
                                    name, out.reason)
                _enrich_images(out.chunks, name)
                return to_legacy_many(out.chunks)

    return _parse_docx_legacy(path, name, chunk_size, overlap)


def _parse_docx_legacy(path, name, chunk_size, overlap):
    """老链路：python-docx 提文本（标题转 Markdown `#`）+ 通用切分。

    path: docx 文件路径，交给 loaders.load_text 读取。
    name: 文件名（basename），写进产物的 source。
    chunk_size: 切块长度上限。
    overlap: 块间重叠。

    提不出文本时返回 []（**该文件会静默消失**；docx 的显式失败记录只在
    Docling 路径的 unprocessable 分支产生）。产物带 parser="python-docx"，
    让 _log_load_summary 能把降级用量打出来。
    """
    from .loaders import load_text

    text = load_text(path) or ""
    if not text.strip():
        return []
    out = []
    for p in _split_text(text, chunk_size, overlap):
        d = {"source": name, "section": p["section"], "text": p["text"]}
        d["parser"] = "python-docx"      # 让 _log_load_summary 能把降级用量打出来
        out.append(d)
    return out


def load_chunks(doc_folder, chunk_size=None, chunk_overlap=None, pdf_chunk_size=None,
                pdf_overlap=None):
    """语料读取总入口：目录 → [{"source","section","text", ...}]。

    doc_folder: 语料根目录。空值或不存在时返回 []（不抛异常）。递归遍历子目录。
    chunk_size: 非 PDF 格式的切块长度上限（字符）。None 取 RAG_CHUNK_SIZE（默认 300）。
    chunk_overlap: 非 PDF 格式的块间重叠。None 取 RAG_CHUNK_OVERLAP（默认 50）。
    pdf_chunk_size: **仅 PDF 降级（pdfminer）链路**的块长。None 取 RAG_PDF_CHUNK_SIZE
        （默认 800）。Docling 路径不读它（那份按 token 计预算）。
    pdf_overlap: PDF 降级链路的块间重叠。None 取 RAG_PDF_CHUNK_OVERLAP（默认 200）。

    支持格式见 src/loaders.py；.pdf 走 Docling（src/docling_loader.py），
    不可用时自动降级到 src/pdf_parser.py 的 pdfminer 链路；
    .docx 同样走 Docling 双路线（正文 + 解 zip 取图片/图表/OLE，见 src/docx_media.py），
    不可用时降级到 python-docx。
    单份文件解析失败只跳过并告警，不影响其余语料；解析不出内容的文件会留下
    kind="unprocessable" 的记录，而不是静默消失（**仅 Docling 路径**；降级路径
    抽不出内容时仍是静默跳过）。

    除 source/section/text 三个既有字段外还带 page/bbox/kind/parser 等元信息，
    旧消费者忽略即可（出参契约见 src/chunk_writer.py）。另有 **source_path**
    （相对语料根目录的 POSIX 路径）与 **content_hash**（文件内容 sha256 前 16 位）
    两个字段，供 `scripts/kb_prune.py` 做级联删除判定 —— 二者**都不参与
    chunk_id**（见 src/kb_inventory.py 模块头）。
    收尾固定三步且顺序有讲究：_apply_contextual → attach_parents → _log_load_summary。
    """
    if not doc_folder or not os.path.isdir(doc_folder):         #检查目录
        return []

    chunk_size = CHUNK_SIZE if chunk_size is None else chunk_size
    chunk_overlap = CHUNK_OVERLAP if chunk_overlap is None else chunk_overlap
    pdf_chunk_size = PDF_CHUNK_SIZE if pdf_chunk_size is None else pdf_chunk_size
    pdf_overlap = PDF_CHUNK_OVERLAP if pdf_overlap is None else pdf_overlap

    from .loaders import load_blocks, supported_extensions

    exts = supported_extensions()       #有对应解析器的格式
    chunks: list[dict] = []
    skipped: dict[str, int] = {}        #按扩展名计数

    files = sorted(glob.glob(os.path.join(doc_folder, "**", "*"), recursive=True))
    #recursive=True 是 ** 生效的前提；缺了它 ** 退化成单层 *，子目录全丢。
    for f in files:
        if not os.path.isfile(f):
            continue
        low = f.lower()
        name = os.path.basename(f)      #取文件名

        # ---- PDF：Docling 为主，pdfminer 自动降级 ----
        if low.endswith(".pdf"):
            try:
                pdf_chunks = _parse_pdf(f, name, pdf_chunk_size, pdf_overlap)
                # 打上 source_path / content_hash（级联删除的判定依据）。
                # **必须逐文件打**：全部解析完再按 basename 回填会在"同名不同目录"
                # 的语料上把路径写错，而写错的后果是清理时删错块。
                chunks += stamp_source(pdf_chunks, f, doc_folder)
                logging.info("PDF 解析完成：%s（%d 个片段）", name, len(pdf_chunks))
            except Exception as e:  # noqa: BLE001
                logging.warning("PDF 解析失败，跳过 %s: %s", name, e)
            continue

        # ---- docx：Docling 双路线（正文 + 媒体）为主，python-docx 自动降级 ----
        # 单独一支而不并入下面的通用分支：docx 有专属解析链路（含图片/图表/OLE 抽取），
        # 走 load_text 就只能拿到正文，等于白丢 Docling 那半边的能力。
        if low.endswith(".docx"):
            try:
                docx_chunks = _parse_docx(f, name, chunk_size, chunk_overlap)
                chunks += stamp_source(docx_chunks, f, doc_folder)
                logging.info("docx 解析完成：%s（%d 个片段）", name, len(docx_chunks))
            except Exception as e:  # noqa: BLE001
                logging.warning("docx 解析失败，跳过 %s: %s", name, e)
            continue

        # ---- 其它格式：结构化解析成块 → 同一套产物 ----
        # 不再走"先拍成一段文本再切"：csv/xlsx/pptx/json/md 各能说清一部分结构
        # （表头、工作表、页序、标题路径、记录边界），拍平之后就永久丢了。
        ext = os.path.splitext(low)[1]
        if ext not in exts:
            skipped[ext] = skipped.get(ext, 0) + 1
            continue

        blocks = load_blocks(f)
        if not blocks:
            continue
        from .chunk_writer import to_legacy_many
        from .knowledge_chunk import from_blocks

        kcs = from_blocks(blocks, name, parser=ext.lstrip(".") or "text",
                          splitter=lambda t: _split_text_texts(t, chunk_size, chunk_overlap))
        _enrich_images(kcs, name)
        chunks += stamp_source(to_legacy_many(kcs), f, doc_folder)

    if skipped:
        logging.warning(
            "以下扩展名暂不支持，已跳过：%s",
            ", ".join(f"{e or '(无扩展名)'}×{n}" for e, n in sorted(skipped.items())),
        )

    # 顺序刻意是「先上下文前缀、后父块」：前缀是喂给嵌入的检索用文本，
    # 父窗是喂给模型的生成用文本。反过来的话，父窗里就没有前缀，
    # 于是"被检索的内容"和"被生成的内容"成了两套文本，出问题很难解释。
    _apply_contextual(chunks)   #给嵌入输入补上下文
    attach_parents(chunks)      #给生成输入挂父窗
    _log_load_summary(chunks)
    return chunks


def _apply_contextual(chunks):
    """Contextual Retrieval（批次8，默认关）。任何失败都只记日志，不影响灌库。

    chunks: 块列表，**就地改写**（contextualizer.apply 会把前缀拼进 text）。
        环境变量 RAG_CONTEXTUAL 关着、或列表为空时直接原样返回。

    返回原列表（便于链式调用），但它和入参是同一个对象 —— 不是副本。
    """
    if not CONTEXTUAL or not chunks:
        return chunks
    try:
        from . import contextualizer
        stats = contextualizer.apply(chunks)
        logging.info("Contextual Retrieval：%s", contextualizer.format_stats(stats))
    except Exception as e:  # noqa: BLE001
        logging.warning("Contextual Retrieval 步骤异常（不影响灌库）：%s（%s: %s）",
                        type(e).__name__, e)
    return chunks


def attach_parents(chunks):
    """给每个块挂上"父块文本"：同一份文档里围绕它的一段连续窗口（约 PARENT_MAX_CHARS 字）。

    chunks: 块列表，**就地改写** —— 写入 parent_id 与 parent_text 两个键。
        受环境变量 RAG_PARENT_CHILD 控制，关掉或空列表时原样返回。

    为什么用**连续窗口**而不是"按章节分组的整节"：PDF 的 section_path 常为空，
    按节分组等于把整份文档当父块，再截断到 1200 字 —— 那就变成"永远是文档开头",
    对定位毫无帮助。窗口是命中块左右的邻居，命中块必然在里面，语境才有意义。

    为什么不建单独的父块索引：本项目语料规模（< 1 万块）下存副本最省事，
    而且"父块到底是什么"变成可直接检视的事实，而不是运行时才算出来的隐式行为。

    只在同一 source 内扩张：跨文档拼上下文会把别的文档的正文当成这段的语境。
    孤立单块（该文档只有一块）不挂父窗 —— 左右都没有邻居，挂了也无意义。
    """
    if not PARENT_CHILD or not chunks:
        return chunks

    n = len(chunks)
    covered = 0
    for i, c in enumerate(chunks):
        src = c.get("source")
        own = c.get("text") or ""
        budget = PARENT_MAX_CHARS - len(own)
        lo = hi = i
        # 左右交替扩张：先保证两侧都有机会，避免一侧的长块把预算吃光
        while budget > 0 and (lo > 0 or hi < n - 1):
            grew = False
            for nxt in (lo - 1, hi + 1):
                if nxt < 0 or nxt >= n or chunks[nxt].get("source") != src:
                    continue
                t = chunks[nxt].get("text") or ""
                if len(t) > budget:
                    continue
                if nxt < lo:
                    lo = nxt
                else:
                    hi = nxt
                budget -= len(t)
                grew = True
            if not grew:
                break
        if hi - lo + 1 < 2:
            continue                      # 孤立单块（该文档只有一块）没什么可"父"的
        c["parent_id"] = f"{src}#{lo}-{hi}"
        c["parent_text"] = "\n".join((chunks[k].get("text") or "") for k in range(lo, hi + 1))
        covered += 1

    if covered:
        logging.info("父子块：%d/%d 块已挂上父窗（≤%d 字，同一文档内扩张）",
                     covered, n, PARENT_MAX_CHARS)
    return chunks


def _log_load_summary(chunks):
    """让「走了降级链路」和「哪些文件没解析出来」在日志里一眼可见。

    chunks: 已装配完毕的块列表（dict 形态，读 parser / kind / source 三个键）。
        空列表只打一条"语料为空"的 warning。

    只看"灌进去多少块"是看不出这两件事的，而它们恰恰是最该被发现的运维信号。
    """
    if not chunks:
        logging.warning("语料为空：没有读到任何内容")
        return

    by_parser = {}
    for c in chunks:
        key = c.get("parser") or "?"
        by_parser[key] = by_parser.get(key, 0) + 1
    logging.info("语料装载完成：共 %d 块（%s）", len(chunks),
                 "，".join(f"{k}={v}" for k, v in sorted(by_parser.items())))

    failed = sorted({c.get("source", "?") for c in chunks
                     if c.get("kind") == "unprocessable"})
    if failed:
        logging.warning("有 %d 个文件未解析出内容（已保留记录）：%s",
                        len(failed), " | ".join(failed))


# ---------------------------------------------------------------------------
# 2. 检索器
# ---------------------------------------------------------------------------
class BM25Retriever:
    """稀疏检索：BM25（Apache Lucene 的 BM25Similarity 口径，纯本地、不依赖外部服务）。

    相比换掉的 TF-IDF 余弦，BM25 多做了三件事，正是它在"短查询 + 长短不齐的文档"上更准的原因：
      1. **词频饱和**：一个词在一篇里出现 10 次不等于"相关性 ×10"（TF-IDF 的余弦里近似如此），
         BM25 用 k1 把它压成一条渐近线，复读词刷不动分；
      2. **文档长度归一化**：长文档天然更容易命中词，b 让"命中密度"而非"命中次数"说话；
      3. **IDF 平滑**：Lucene 用 log(1 + …)，对 df→N 的极常见词不会像教科书版那样算出负分。

    公式严格照 Lucene 9.0.0 的 BM25Similarity（含"权重里没有 (k1+1) 因子"这一条 ——
    那是 Lucene 8.0 起的改动，更早的 Robertson 公式是带 (k1+1) 的）：
        idf(df, N) = log(1 + (N - df + 0.5) / (df + 0.5))
        avgdl      = sumTotalTermFreq / docCount          # 全部 token 数 / 文档数
        norm(dl)   = k1 * (1 - b + b * dl / avgdl)
        score(d)   = Σ_t idf(t) * qtf(t) * freq(t,d) / (freq(t,d) + norm(dl_d))

    与 Lucene 的两处**刻意偏差**（都不改排序语义，只影响分值的绝对值）：
      - Lucene 把 dl 量化成 1 字节（SmallFloat.byte4ToInt）省存储，这里用精确长度；
      - Lucene 的 boost 恒为 1（本项目一次查询只有一个权重），故略去。
    """

    def __init__(self, chunks):
        """建词频矩阵与 BM25 统计量（构造即全量算完，无增量更新接口）。

        chunks: 块列表（dict），读 "text" 键作为文档。空列表（或全部文本为空）会因
            sklearn 抛 "empty vocabulary" ValueError —— 上游 build_retriever 已用
            NullRetriever 拦住这种情况，所以这里不做空保护。

        与查询无关的量（idf / avgdl / norm）全部在构造期算好，查询时只剩一次 O(nnz) 累加。
        改 k1 / b 需要**重建实例** —— 长度归一化分母已按当时的值烘进 self.norm。
        """
        from sklearn.feature_extraction.text import CountVectorizer
        import numpy as np

        self.chunks = chunks
        self.np = np
        self.k1 = BM25_K1
        self.b = BM25_B
        self.vectorizer = CountVectorizer(
            analyzer=BM25_ANALYZER, ngram_range=BM25_NGRAM_RANGE, dtype=np.float64
        )
        counts = self.vectorizer.fit_transform([c["text"] for c in chunks]).tocsr()
        n_docs = counts.shape[0]
        self.n_docs = n_docs

        # dl = 每篇文档的 token 数 = 词频矩阵行和；其均值即 avgdl（Lucene 的 sumTotalTermFreq/docCount）。
        # ⚠️ avgdl 兜底成 1.0 而非 0.0：avgdl=0 会让 dl/avgdl 变成 nan，nan 会污染**所有**分数、
        #    让排序结果随机 —— 一个不抛异常的静默错乱。说清强度：这条防御**正常路径不可达**
        #    （全部文本为空时 sklearn 会先抛 empty vocabulary），它是最后一道，不是主要防线。
        doc_len = np.asarray(counts.sum(axis=1)).ravel()
        avgdl = float(doc_len.mean()) if n_docs else 0.0
        self.avgdl = avgdl if avgdl > 0 else 1.0

        # df = 每个 term 出现在多少篇文档里（按列数非零元素）
        df = np.asarray((counts > 0).sum(axis=0)).ravel()
        self.idf = np.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))

        # 把稀疏矩阵摊平成"每个非零元素"的三条平行数组，查询时可直接做 O(nnz) 的 numpy 运算。
        # 不用 counts[:, cols] 那种列切片：csr 的列切片会触发 SparseEfficiencyWarning，
        # 还得重建一遍索引结构。
        self.nnz_col = counts.indices                                        # 属于哪个 term
        self.nnz_freq = counts.data                                          # 在该文档里的词频
        self.nnz_row = np.repeat(np.arange(n_docs), np.diff(counts.indptr))  # 属于哪个文档

        # 长度归一化分母，与查询无关，一次算好
        self.norm = self.k1 * (1.0 - self.b + self.b * doc_len / self.avgdl)

    def search(self, query, top_k=TOP_K):
        """按 BM25 返回 top_k 命中，格式 [(chunk, score), ...]（分越高越相关）。

        query: 查询文本。**不加** EMBEDDING_QUERY_PREFIX（那是稠密检索的事）。
        top_k: 返回条数上限。

        得分为 0 的块被过滤掉（与查询零词重叠 = 无关），所以实际返回可能少于 top_k，
        极端情况下返回 [] —— 与换掉的 TF-IDF 行为一致，上游据此走"没找到资料"的降级路径。
        """
        np = self.np
        q = self.vectorizer.transform([query])
        if q.nnz == 0:
            return []

        # 查询侧 term → 查询词频。用 searchsorted 配对而不是"开一个 vocab 大小的数组再按列查"：
        # char n-gram 的 vocab 随语料线性增长（几十万很常见），而一次查询真正命中的 term
        # 只有几十个 —— 按 vocab 开数组等于每次查询都白白分配并清零一大片内存。
        q_order = np.argsort(q.indices)
        q_col, q_freq = q.indices[q_order], q.data[q_order]
        pos = np.clip(np.searchsorted(q_col, self.nnz_col), 0, q_col.size - 1)
        hit = q_col[pos] == self.nnz_col
        if not hit.any():
            return []

        freq = self.nnz_freq[hit]
        # Σ idf(t) * qtf(t) * freq / (freq + norm(dl))：一次算完全部命中的非零元素的贡献。
        # 分母恒 > 0（freq ≥ 1，norm ≥ 0），不存在除零。
        contrib = (self.idf[self.nnz_col[hit]] * q_freq[pos[hit]] * freq
                   / (freq + self.norm[self.nnz_row[hit]]))
        scores = np.bincount(self.nnz_row[hit], weights=contrib, minlength=self.n_docs)

        idx = np.argsort(scores)[::-1][:top_k]
        return [(self.chunks[i], float(scores[i])) for i in idx]


class EmbeddingRetriever:
    """稠密检索：走独立的 EMBEDDING_* 配置（OpenAI 兼容 /v1/embeddings）。

    语料不大时直接 numpy 暴力算余弦即可，不需要引入向量数据库。
    """

    def __init__(self, chunks):
        """批量向量化全部块（构造即调 API，按 EMBEDDING_BATCH 分批）。

        chunks: 块列表（dict），读 "text" 键作为文档。每条一次嵌入调用，
            大语料下这里是启动耗时的主要来源（无持久化，每次启动全量重算）。
        """
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
        # 实例级熔断器（不跨用例共享状态）：embedding 网关整体挂了时快速失败，
        # 而不是每个请求都顶着超时重试三次。
        self.breaker = CircuitBreaker("embedding", CIRCUIT_FAIL_THRESHOLD,
                                      CIRCUIT_RESET_SECONDS, CIRCUIT_ENABLED)
        self.vectors = guarded_call(
            self.breaker,
            lambda: self.embedder.embed_documents([c["text"] for c in chunks]),
        )

    def search(self, query, top_k=TOP_K):
        """返回 top_k 命中 [(chunk, score), ...]。

        query: 查询文本。**会加** EMBEDDING_QUERY_PREFIX 指令前缀（文档侧不加）——
        BGE 系模型靠这个拉开区分度，见模块顶部的实测数据。
        top_k: 返回条数上限。与 BM25Retriever 不同，这里**不过滤负分/零分**，
            所以恒返回 min(top_k, 块数) 条。
        """
        # 查询侧加指令前缀（文档侧不加），BGE 系模型靠这个拉开区分度
        q = guarded_call(
            self.breaker,
            lambda: self.embedder.embed_query(
                apply_query_prefix(EMBEDDING_QUERY_PREFIX, query)),
        )
        sims = [self._cos(q, v) for v in self.vectors]
        order = self.np.argsort(sims)[::-1][:top_k]
        return [(self.chunks[i], float(sims[i])) for i in order]

    @staticmethod
    def _cos(a, b):
        """两个向量的余弦相似度。

        a: 查询向量（list / np.ndarray）。
        b: 文档向量（同上）。

        分母加了 1e-9 防止零向量导致除零。
        """
        import numpy as np

        a, b = np.array(a), np.array(b)
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


class PgVectorRetriever:
    """稠密检索（PostgreSQL + pgvector）：向量落库、检索走 SQL。

    与 EmbeddingRetriever **接口完全一致**（`search(query, top_k)`），
    所以能直接替换 hybrid 的稠密路，上游 RRF / rerank 一行都不用改。

    与内存版的关键差别是**增量**：构造时先查库里已有的块指纹，只对缺失的块调
    embedding API；已入库的块直接复用向量。这解决了内存版"每次进程启动都全量
    重算 embedding"的烧钱问题，也是切到 pgvector 的首要收益。

    store 可注入（测试用假实现）；None 时按 DB_URL / PGVECTOR_* 自建 PgVectorStore。
    """

    def __init__(self, chunks, store=None, tenant=None):
        """建索引：算缺失块的向量并 upsert 进 pg。

        chunks: 块列表（dict），取 text 作检索文本、**整块**落 jsonb 供检后回读。
        store: PgVectorStore 或同接口的假实现；None 时自建。
        tenant: 租户 id。None（默认）时退回模块级 RAG_TENANT，**行为与改造前完全一致**；
            显式传入时才覆盖。空串 = 不隔离（单租户/兼容既有数据）。

        构造会连库、首次灌库时还会调 embedding API；表已存在且块都在库里时，
        零 embedding 调用（只发一次 `SELECT id`）。
        """
        from langchain_openai import OpenAIEmbeddings

        t = resolve_tenant(tenant)
        self.tenant = t
        self.chunks = chunks
        # 指纹带上租户：多租户下不同租户的同内容块各自入库，不会互相覆盖
        self.by_id = {chunk_fingerprint(c, t): c for c in chunks}
        self.embedder = OpenAIEmbeddings(
            model=EMBEDDING_MODEL,
            api_key=EMBEDDING_API_KEY,
            base_url=EMBEDDING_API_BASE or None,
            chunk_size=EMBEDDING_BATCH,
            max_retries=3,
        )
        self.breaker = CircuitBreaker("embedding", CIRCUIT_FAIL_THRESHOLD,
                                      CIRCUIT_RESET_SECONDS, CIRCUIT_ENABLED)
        self.store = store if store is not None else PgVectorStore(
            DB_URL, PGVECTOR_TABLE, PGVECTOR_INDEX, tenant=t
        )
        self._sync()

    def _sync(self):
        """增量同步：只为库里没有的块算向量并写入。

        无参数、无返回。缺失集为空时直接返回（零 API 调用）。
        """
        _check_index_identity(self.store, "pgvector")
        existing = self.store.existing_ids()
        missing = [(cid, c) for cid, c in self.by_id.items() if cid not in existing]
        if not missing:
            logging.info("pgvector 已是最新（%d 块），本次零 embedding 调用", len(self.by_id))
            return
        texts = [(c.get("text") or "") for _, c in missing]
        vectors = guarded_call(self.breaker,
                               lambda: self.embedder.embed_documents(texts))
        if not vectors:
            raise RuntimeError("embedding 服务返回空结果，无法写入 pgvector")
        self.store.ensure_schema(len(vectors[0]))
        rows = [
            (cid, c, (c.get("text") or ""), vec)
            for (cid, c), vec in zip(missing, vectors)
        ]
        n = self.store.upsert(rows)
        logging.info("pgvector 增量写入 %d 块（库内原有 %d 块）", n, len(existing))
        # 再判一次：入口那次表可能还不存在（维度未知 → unknown，不记录）。
        # 建表之后维度才第一次可知，此刻才是"把身份落定"的正确时机。
        _check_index_identity(self.store, "pgvector")

    def search(self, query, top_k=TOP_K):
        """返回 top_k 命中 [(chunk, score), ...]。

        query: 查询文本，**会加** EMBEDDING_QUERY_PREFIX（与内存版一致）。
        top_k: 返回条数上限。

        score 是余弦相似度（1 - 距离），与 EmbeddingRetriever 同口径，
        故 RRF 融合 / rerank 无需改动。
        """
        q = guarded_call(
            self.breaker,
            lambda: self.embedder.embed_query(
                apply_query_prefix(EMBEDDING_QUERY_PREFIX, query)),
        )
        return self.store.search(q, top_k)


class QdrantRetriever:
    """Qdrant 后端：**稀疏（BM25）与稠密（embedding）同库**，一个 collection 两路召回。

    与 PgVectorRetriever 的三点差别：
      1. 一个后端同时承载稀疏与稠密两路（pg 只做稠密，稀疏留在进程内 BM25Retriever）；
      2. 稀疏用本地 BM25 哈希稀疏向量（`Bm25SparseEncoder`），检索走 Qdrant 稀疏倒排；
      3. 多租户隔离从 `tenant` 列 + WHERE 平移成 payload 过滤（语义等价，见 qdrant_store）。

    对外暴露 `.sparse` / `.dense` 两个适配器（各自 `search(query, top_k)`），
    以便**原样塞进现有 HybridRetriever 架构** —— RRF 融合与 rerank 一行不改，
    `last_rerank_ok` 语义也照旧。

    构造会连 Qdrant、首次灌库时还会调 embedding API；已入库的块零 embedding 调用
    （与 PgVectorRetriever 同一套增量判定）。
    """

    def __init__(self, chunks, store=None, tenant=None):
        """建索引：算缺失块的稠密 + 稀疏向量并 upsert 进 Qdrant。

        chunks: 块列表（dict），取 text 作检索文本、**整块**落 payload 供检后回读。
        store: QdrantStore 或同接口的假实现；None 时按 QDRANT_* 自建。
        tenant: 租户 id。None（默认）时退回模块级 RAG_TENANT；空串 = 不隔离。
        """
        from langchain_openai import OpenAIEmbeddings

        t = resolve_tenant(tenant)
        self.tenant = t
        self.chunks = chunks
        # 指纹带租户：多租户下不同租户的同内容块各自成点，不互相覆盖（与 pg 一致）
        self.by_id = {chunk_fingerprint(c, t): c for c in chunks}
        self.embedder = OpenAIEmbeddings(
            model=EMBEDDING_MODEL,
            api_key=EMBEDDING_API_KEY,
            base_url=EMBEDDING_API_BASE or None,
            chunk_size=EMBEDDING_BATCH,
            max_retries=3,
        )
        self.breaker = CircuitBreaker("embedding", CIRCUIT_FAIL_THRESHOLD,
                                      CIRCUIT_RESET_SECONDS, CIRCUIT_ENABLED)
        self.store = store if store is not None else QdrantStore(
            QDRANT_URL, QDRANT_API_KEY, QDRANT_COLLECTION, tenant=t,
            timeout=QDRANT_TIMEOUT, hnsw_m=QDRANT_HNSW_M,
            hnsw_ef_construct=QDRANT_HNSW_EF_CONSTRUCT, on_disk=QDRANT_ON_DISK,
        )
        # 稀疏编码器的语料统计（idf/avgdl）只依赖当前这批块，构造期一次算好。
        self.encoder = Bm25SparseEncoder(
            [c.get("text") or "" for c in chunks],
            dim=QDRANT_SPARSE_DIM, k1=BM25_K1, b=BM25_B, ngram=BM25_NGRAM_RANGE,
        )
        self._sync()
        self.sparse = QdrantSparseRetriever(self)
        self.dense = QdrantDenseRetriever(self)

    def _sync(self):
        """增量同步：只为集合里没有的块算向量（稠密 + 稀疏）并写入。"""
        # 同 pgvector：身份校验必须在早退之前（"库已最新"正是换模型后重启的路径）
        _check_index_identity(self.store, "qdrant")
        self.store.ensure_schema(QDRANT_DIM)
        existing = self.store.existing_ids()
        missing = [(fp, c) for fp, c in self.by_id.items()
                   if point_id(fp) not in existing]
        if not missing:
            logging.info("Qdrant 已是最新（%d 块），本次零 embedding 调用", len(self.by_id))
            return
        texts = [(c.get("text") or "") for _, c in missing]
        vectors = guarded_call(self.breaker,
                               lambda: self.embedder.embed_documents(texts))
        if not vectors:
            raise RuntimeError("embedding 服务返回空结果，无法写入 Qdrant")
        got = len(vectors[0])
        if got != QDRANT_DIM:
            # 与 pg 同一条口径：维度不符时**显式报错**，不要让它变成"某次 upsert 莫名失败"。
            raise RuntimeError(
                f"embedding 维度 {got} 与 QDRANT_DIM={QDRANT_DIM} 不一致。"
                "改维度无法原地完成，请换 QDRANT_COLLECTION（如 "
                f"{QDRANT_COLLECTION}_d{got}）重新灌库，并把 QDRANT_DIM 设为 {got}。"
            )
        rows = []
        for (fp, c), vec in zip(missing, vectors):
            s_idx, s_val = self.encoder.encode_document(c.get("text") or "")
            rows.append((point_id(fp), c, (c.get("text") or ""), vec, s_idx, s_val))
        n = self.store.upsert(rows)
        logging.info("Qdrant 增量写入 %d 块（该租户原有 %d 块）", n, len(existing))
        # 集合刚建出来时入口那次读不到维度（unknown），这里补判一次把身份落定
        _check_index_identity(self.store, "qdrant")

    def search_dense(self, query, top_k=TOP_K):
        """稠密检索（供 semantic 单路后端直接用；hybrid 走 self.dense 适配器）。"""
        return self.dense.search(query, top_k)

    def search_sparse(self, query, top_k=TOP_K):
        """稀疏检索（供需要单独走关键词路的调用点）。"""
        return self.sparse.search(query, top_k)


class QdrantSparseRetriever:
    """稀疏路适配器：把 Qdrant 的稀疏（BM25）检索包装成 `.search(query, top_k)`。

    存在的意义：让 Qdrant 稀疏路能**原样替换** `BM25Retriever` 塞进
    `HybridRetriever(sparse=...)`，融合/rerank 逻辑不必知道后端是谁。
    """

    def __init__(self, owner):
        """owner: QdrantRetriever（持有 store / encoder）。"""
        self._o = owner

    def search(self, query, top_k=TOP_K):
        """返回 [(chunk, score), ...]。查询无有效 term 时返回 []（同 BM25Retriever）。"""
        idx, val = self._o.encoder.encode_query(query)
        if not idx:
            return []
        return self._o.store.search_sparse(idx, val, top_k)


class QdrantDenseRetriever:
    """稠密路适配器：query 向量化（加 EMBEDDING_QUERY_PREFIX）后走 Qdrant 稠密 ANN。"""

    def __init__(self, owner):
        """owner: QdrantRetriever（持有 store / embedder / breaker）。"""
        self._o = owner

    def search(self, query, top_k=TOP_K):
        """返回 [(chunk, score), ...]，score = 余弦相似度（与 EmbeddingRetriever 同口径）。"""
        q = guarded_call(
            self._o.breaker,
            lambda: self._o.embedder.embed_query(
                apply_query_prefix(EMBEDDING_QUERY_PREFIX, query)),
        )
        return self._o.store.search_dense(q, top_k)


class NullRetriever:
    """无语料 / 未配置 DOCS_DIR 时的空检索器：永远返回空结果。

    作用是把「没配知识库」变成正常降级，而不是让 TF-IDF 在空语料上抛
    ValueError（empty vocabulary）之后把整张图带崩。
    """

    def search(self, query, top_k=TOP_K):
        """恒定返回 []（两个参数都被忽略，只为了与其余检索器接口一致）。

        query: 查询文本，不使用。
        top_k: 返回条数上限，不使用。
        """
        return []


# ---------------------------------------------------------------------------
# 3. 混合检索（RRF 融合 + 可选 rerank）
# ---------------------------------------------------------------------------
def _chunk_key(chunk):
    """块的去重键：source + section + text 三元组。

    chunk: 单个块 dict，三个键都用 .get 兜底成空串。

    为什么不用 chunk_id：RRF 要对齐"两路召回里的同一个块"，而 chunk_id 可能因
    两条链路（Docling / pdfminer）而不同；source+section+text 是稳定的内容指纹。
    代价是内容完全相同的两个块（如同一文件里重复的段落）会被合并成一个。
    """
    return (chunk.get("source", ""), chunk.get("section", ""), chunk.get("text", ""))


def rrf_fuse(rank_lists, k=None):
    """Reciprocal Rank Fusion：只用排名、不用绝对分。

    rank_lists: 多路召回结果，每路是 [(chunk, score), ...]（score 会被忽略）。
        传空列表会得到 []。
    k: RRF 平滑常数。None 时取环境变量 RAG_RRF_K（默认 60，原论文推荐值）——
        越大越弱化头部排名差异。

    返回 [(chunk, fused_score), ...]，按融合分降序，**不做截断**（截断由调用方做）。

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
        """记住端点与凭证，构造时不发请求。

        base: API base（不含 /rerank，本类会自己拼）。尾部斜杠会被裁掉。
        key: Bearer token。
        model: rerank 模型名。
        timeout: 单次请求超时（秒）。None 时取环境变量 RERANK_TIMEOUT（默认 20）。
        """
        self.url = (base or "").rstrip("/") + "/rerank"
        self.key = key
        self.model = model
        self.timeout = timeout or RERANK_TIMEOUT
        # 实例级熔断器：rerank 网关持续失败时快速失败，由 HybridRetriever 捕获后
        # 回退融合顺序（本类的调用方本来就有降级分支，这里只是不再白等超时）。
        self.breaker = CircuitBreaker("rerank", CIRCUIT_FAIL_THRESHOLD,
                                      CIRCUIT_RESET_SECONDS, CIRCUIT_ENABLED)

    def rerank(self, query, chunks, top_k):
        """按 query 对候选块精排，返回 [(chunk, relevance_score), ...]。

        query: 查询文本。
        chunks: 候选块列表（dict），读 "text" 键作为 documents 送给服务方。
        top_k: 请求方最多返回几条（对应请求体的 top_n）；服务方返回的 index
            若越界会被跳过。

        失败会**抛异常**（httpx 网络错误 / raise_for_status 的 4xx-5xx）——
        由调用方 HybridRetriever.search 捕获并回退融合顺序，这里不做兜底。
        服务方返回空 results 时返回 None（同样视为失败）。
        """
        import httpx

        def _post():
            r = httpx.post(
                self.url,
                headers={"Authorization": f"Bearer {self.key}",
                         "Content-Type": "application/json"},
                json={"model": self.model, "query": query,
                      "documents": [c["text"] for c in chunks], "top_n": top_k},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r

        # 经熔断器发请求：熔断打开时抛 CircuitOpenError（上游 HybridRetriever 会
        # 捕获并回退融合顺序），避免持续故障期间每个请求都白等一个超时。
        resp = guarded_call(self.breaker, _post)
        results = resp.json().get("results") or []
        out = []
        for item in results:
            i = int(item.get("index", -1))
            if 0 <= i < len(chunks):
                out.append((chunks[i], float(item.get("relevance_score", 0.0))))
        return out or None


def rerank_configured() -> bool:
    """rerank 是否具备可用配置（模型名 + 至少一个 key）。

    无参数。key 可复用 EMBEDDING_API_KEY（常见情况是同一个服务商提供两套接口），
    所以只要 RERANK_MODEL 填了且两处 key 有其一就算配好。
    """
    return bool(RERANK_MODEL and (RERANK_API_KEY or EMBEDDING_API_KEY))


class HybridRetriever:
    """稀疏（BM25 关键词）+ 稠密（向量）双路召回 → RRF 融合 → 可选 rerank。

    为什么值得做：中文场景下关键词能抓住精确名称/编号（六堡茶、1897年），
    向量能抓住改写与近义（"怎么去广州" ≈ "高铁耗时"），两者漏掉的样本不一样。
    """

    def __init__(self, chunks, sparse=None, dense=None, reranker=None, pool=None):
        """装配双路检索器；sparse / dense 未传时**就地新建**（会产生嵌入成本）。

        chunks: 块列表，原样存进 self.chunks（供 search 取回）。
        sparse: 稀疏检索器（默认 BM25Retriever(chunks)）。测试可注入假实现。
        dense: 稠密检索器（默认 EmbeddingRetriever(chunks)，**这一步会调 embedding API**）。
        reranker: 可选精排器（RerankClient）。None 表示只做融合，不精排。
        pool: 每路各取多少条候选进融合。None 时取 HYBRID_POOL（默认 10）。
        """
        self.chunks = chunks
        self.sparse = sparse if sparse is not None else BM25Retriever(chunks)
        self.dense = dense if dense is not None else EmbeddingRetriever(chunks)
        self.reranker = reranker
        self.pool = pool or HYBRID_POOL
        self.last_rerank_ok = None

    def search(self, query, top_k=TOP_K):
        """双路召回 → RRF 融合 →（可选）rerank，返回 top_k 命中。

        query: 查询文本，原样传给两路（前缀处理在各路内部自理）。
        top_k: 最终返回条数。rerank 的候选池取 max(top_k*3, 10)，比 top_k 宽，
            给精排留出翻身空间。

        精排**失败自动回退**：异常或结果为空时用融合顺序截断，并把
        self.last_rerank_ok 置为 False —— 该字段是给调用方/测试观察
        "这次到底有没有精排生效"用的（初始为 None 表示从未尝试）。
        """
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
    """embedding 配置是否已填写（API base 可留空，走服务方默认地址）。

    无参数。只查这两项是否非空，**不验证连通性** —— 真连不通会在构造
    EmbeddingRetriever 时报错。
    """
    return bool(EMBEDDING_API_KEY and EMBEDDING_MODEL)


def _check_index_identity(store, backend):
    """核对"库里向量的身份"与当前 `EMBEDDING_MODEL`；只读 + 至多写一条元数据。

    store: 向量库句柄（需有 `index_dim` / `get_meta` / `set_meta`）。
    backend: `pgvector` / `qdrant`，只进日志与状态。

    返回 `index_identity.verify` 的状态 dict；任何异常都吞掉并只记 WARNING ——
    这是**观测**，不该让灌库失败（与项目"降级不崩"的口径一致）。

    为什么维度取自 `store.index_dim()`（库里的**实际**维度）而不是配置：pg 侧根本
    没有"维度"这个配置项，而且从库里读才不需要任何 embedding 调用 —— 这很关键，
    因为 `_sync` 在"库已最新"时会**零调用提前返回**，而换完模型重启走的正是这条
    路径。校验若依赖 embedding 调用，最该报的那一次永远不会报。

    为什么不在这里写指标：`src/rag/` 是纯库层（CLI 也直接跑它），不该反向依赖
    `app/metrics`。结论缓存在 `index_identity.last_status()`，由 app 层读后上报。
    """
    try:
        return index_identity.verify(
            store, model=EMBEDDING_MODEL, dim=store.index_dim(),
            query_prefix=EMBEDDING_QUERY_PREFIX, backend=backend,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("索引身份自检失败（不影响灌库）：%s", e)
        return None


def open_vector_store(tenant=None):
    """按当前配置**只打开**向量库句柄（不建表、不灌库、不算 embedding）。

    tenant: 租户 id（None = 沿用模块级 RAG_TENANT）。
    返回 `(store, backend)`；后端为内存时返回 `(None, "memory")` —— 没有持久化
    向量库就没有"索引身份"可言。

    为什么不复用 `build_retriever`：它会**灌库**（缺块时调 embedding API 并按量
    计费）。启动自检必须快、且绝不能因为"检查一下"就产生副作用（改库 / 烧钱）。
    QdrantClient 与 psycopg 都是懒连接，所以这个函数本身不发任何请求。
    """
    if VECTOR_STORE == "qdrant":
        return QdrantStore(
            QDRANT_URL, QDRANT_API_KEY, QDRANT_COLLECTION,
            tenant=resolve_tenant(tenant), timeout=QDRANT_TIMEOUT,
            hnsw_m=QDRANT_HNSW_M, hnsw_ef_construct=QDRANT_HNSW_EF_CONSTRUCT,
            on_disk=QDRANT_ON_DISK,
        ), "qdrant"
    if VECTOR_STORE == "pg":
        return PgVectorStore(
            DB_URL, PGVECTOR_TABLE, PGVECTOR_INDEX, tenant=resolve_tenant(tenant)
        ), "pgvector"
    return None, "memory"


def selfcheck_index_identity(tenant=None):
    """启动自检：比对"库里向量的身份"与当前 `EMBEDDING_MODEL`，返回状态 dict。

    tenant: 租户 id（None = 沿用模块级 RAG_TENANT）。

    返回 `index_identity.verify` 的状态（`status` / `ok` / `detail` / `backend`…）；
    未配置持久化后端时返回 `status="skipped"`。**不抛异常** —— 自检失败不该拦住
    启动（服务本身仍可用），但必须被大声记下来（`verify` 里 mismatch 走 ERROR 日志）。
    """
    store, backend = open_vector_store(tenant)
    if store is None:
        return {
            "status": "skipped",
            "ok": None,
            "backend": backend,
            "detail": "当前未使用持久化向量后端（memory），没有可校验的索引身份",
        }
    got = _check_index_identity(store, backend)
    if got is None:
        return {
            "status": "error",
            "ok": None,
            "backend": backend,
            "detail": "索引身份自检未完成（读取向量库失败，详见日志）",
        }
    return got


def _make_dense_retriever(chunks, tenant=None):
    """按 RAG_VECTOR_STORE 选稠密检索后端：qdrant / pg（pgvector）/ memory（numpy）。

    chunks: 块列表。
    tenant: 租户 id（None = 用模块级 RAG_TENANT）。**只有 qdrant / pg 后端会真正
        用到它** —— 内存后端（EmbeddingRetriever）的语料就是传进来的这批 chunks，
        本身不含跨租户数据，因此没有可过滤的维度。

    qdrant / pg 后端**失败自动回退**内存后端并告警 —— 与项目其它配置项"降级不崩"
    的口径一致：服务连不上、扩展没装、python 包缺失，都不该让整个服务起不来。

    注意：qdrant 这里只取它的**稠密**适配器（`r.dense`）；hybrid 双路后端会在
    `build_retriever` 里另取 `r.sparse`，此处仅在 semantic 单路模式下被调用。
    """
    if VECTOR_STORE == "qdrant":
        try:
            r = QdrantRetriever(chunks, tenant=tenant)
            logging.info("稠密检索后端：Qdrant（集合=%s，租户=%s）",
                         QDRANT_COLLECTION, resolve_tenant(tenant) or "<不隔离>")
            return r.dense
        except Exception as e:  # noqa: BLE001  （任何失败都回退，不让服务崩）
            logging.warning("Qdrant 后端不可用（%s），回退内存向量检索。", e)
    if VECTOR_STORE == "pg":
        try:
            r = PgVectorRetriever(chunks, tenant=tenant)
            logging.info("稠密检索后端：pgvector（表=%s，租户=%s）",
                         PGVECTOR_TABLE, resolve_tenant(tenant) or "<不隔离>")
            return r
        except Exception as e:  # noqa: BLE001  （任何失败都回退，不让服务崩）
            logging.warning("pgvector 后端不可用（%s），回退内存向量检索。", e)
    return EmbeddingRetriever(chunks)


def build_retriever(chunks, tenant=None):
    """按 RAG_EMBEDDING 选择检索后端；embedding 没配好则自动回退 BM25。

    chunks: 已装配的块列表（load_chunks 的产物）。空列表返回 NullRetriever，
        这是为了拦住稀疏检索在空语料上的 "empty vocabulary" 异常。
    tenant: 租户 id。None（默认）= 沿用模块级 RAG_TENANT，与原调用点行为一致；
        传具体值则用于**本次请求**的检索隔离（只有 qdrant / pg 后端会真正生效）。

    选路规则：
      - backend ∈ {semantic, openai, hybrid} 但 embedding 未配置 → 降级 bm25 并告警；
      - hybrid → HybridRetriever（稀疏路默认走进程内 BM25；当 RAG_VECTOR_STORE=qdrant
        时**稀疏与稠密都改走 Qdrant**，见下），若 RAG_RERANK 开着且
        rerank_configured() 则附带精排器；
      - semantic / openai → 稠密检索器（qdrant / pgvector / 内存，见 _make_dense_retriever）；
      - tfidf → 历史别名，等价 bm25，读到就打 WARNING；
      - 其余（含拼错的值）→ BM25Retriever（兜底，不报错）。
    """
    if not chunks:
        logging.info("RAG 语料为空，使用 NullRetriever（检索将返回空结果）")
        return NullRetriever()

    backend = (EMBEDDING_BACKEND or "bm25").strip().lower()
    if backend == "tfidf":
        # 历史取值：换 BM25 之前，"本地稀疏检索"就叫这个名字。留着别名是为了让存量 .env
        # 不改也能跑出**完全相同**的行为（否则它会落进下面的兜底分支 —— 结果一样，
        # 于是配置写错也没人发现），同时把"这个名字已经不算数了"明确说出来。
        logging.warning(
            "RAG_EMBEDDING=tfidf 是历史取值，现已统一为 BM25（行为等价于 bm25）；"
            "建议把 .env 里的这行改成 RAG_EMBEDDING=bm25。"
        )
        backend = "bm25"

    if backend in ("semantic", "openai", "hybrid") and not embedding_configured():          #如果backend已经选了但是没有进行配置
        logging.warning(
            "RAG_EMBEDDING=%s 需要 embedding，但 EMBEDDING_API_KEY / EMBEDDING_MODEL "
            "尚未填写，本次自动回退 BM25。",
            backend,
        )
        backend = "bm25"

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

        # Qdrant 后端：稀疏（BM25）与稠密（embedding）**同库**，两路都走它。
        # 失败自动回退到「进程内 BM25 + 按 RAG_VECTOR_STORE 的稠密后端」，与其余降级一致。
        if VECTOR_STORE == "qdrant":
            try:
                qr = QdrantRetriever(chunks, tenant=tenant)
                logging.info("hybrid 双路后端：Qdrant（集合=%s，租户=%s）",
                             QDRANT_COLLECTION, resolve_tenant(tenant) or "<不隔离>")
                return HybridRetriever(chunks, sparse=qr.sparse, dense=qr.dense,
                                       reranker=reranker)
            except Exception as e:  # noqa: BLE001
                logging.warning("Qdrant 后端不可用（%s），回退 BM25 + %s 稠密检索。",
                                e, VECTOR_STORE)
        return HybridRetriever(
            chunks, dense=_make_dense_retriever(chunks, tenant), reranker=reranker
        )

    if backend in ("semantic", "openai"):
        logging.info("RAG 检索后端：semantic（embedding 模型=%s）", EMBEDDING_MODEL)
        return _make_dense_retriever(chunks, tenant)

    return BM25Retriever(chunks)


# ---------------------------------------------------------------------------
# 4. 生成
# ---------------------------------------------------------------------------
def build_prompt(question, contexts):
    """把命中的块拼成"只依据资料作答"的提示词。

    question: 用户问题，放在末尾。
    contexts: [(chunk, score), ...] —— 检索命中。score 在这里不被使用。

    内容优先取 `parent_text`（批次8 的 small-to-big）：检索用小块保证精度，
    送模型时换成命中块所在的**上下文窗口**，避免答案因为缺上文而断裂。
    没有父窗的块（如该文档只有一块）自动退回自身 text。判断逻辑在 context_block。

    返回完整 prompt 字符串（已含"资料里没有就说未提及、不要编造"的约束）。
    """
    blocks = [context_block(c) for c, _ in contexts]
    ctx = "\n\n".join(blocks)
    return (
        "你是一个严谨的问答助手。请只根据下面提供的【参考资料】回答用户问题，"
        "如果资料里没有相关信息，就如实说“资料中未提及”。不要编造。\n\n"
        f"【参考资料】\n{ctx}\n\n【用户问题】\n{question}\n\n【回答】"
    )


def context_block(chunk) -> str:
    """一个命中块 → 送进模型的文本块。

    chunk: 单个块 dict。读 parent_text / content / text 三个键（按优先级取第一个非空），
        以及 source 用于标注来源。三者全空时正文为空串，但来源行仍会打出来。

    优先级：**父窗 > 保真内容 > 检索文本**。这个顺序不是随手定的：
      - 父窗（parent_text）自带前后文，是批次8 在生成侧唯一生效的地方；
      - 保真内容（content）是没有被加工过的原文（表格是原网格）；
      - 检索文本（text）是给检索用的加工结果（表格被文字化成 `列=值`、
        可能带上下文前缀），拿它当原文喂模型会引入"这段到底是原文还是加工品"的混淆。
    过去 graph 路径直接取了 `text`，等于把批次8 的父子块完全绕过了。
    """
    body = chunk.get("parent_text") or chunk.get("content") or chunk.get("text") or ""
    return f"[来源: {chunk.get('source', '?')}]\n{body}"


def build_citations(hits, snippet_chars=None) -> list:
    """检索命中 → **可回溯的引用列表**（给接口层/前端用）。

    hits: [(chunk, score), ...] —— search() 的原样返回。
    snippet_chars: 单条引用的原文节选长度上限。None 时取环境变量
        RAG_CITE_SNIPPET_CHARS（默认 1000）；显式传值时下限被钳到 50。

    返回 list[dict]，每条含来源/页码/章节/坐标/chunk_id/保真原文节选/表格网格/
    图片路径/人话位置 where。可选附件字段（table_md / source_image / page_image /
    alt_text / tags）**只在真有值时**才带出去，避免一堆空字段把引用撑胖。

    为什么必须单独做这一步：`search()` 返回的是完整块记录（实测 25 个字段），
    但过去没有任何一层把它交出去 —— `graph.retrieve()` 只取了 `source` 与 `text`
    拼成一段字符串。于是"这段话出自哪份文档的第几页、哪一节、原表格长什么样、
    配图在哪"在接口层就永久丢失了。

    这里的取舍：
      - `snippet` 取 `content`（保真）而不是 `text`（加工过），否则回显出来的"原文"
        其实是文字化之后的形态，会误导人工核对；
      - `parent_text` **不**放进引用（那是生成用的窗口，不是原文）；
      - 内容按 `RAG_CITE_SNIPPET_CHARS` 截断：状态会被 checkpointer 持久化，
        把整块原文都塞进每轮状态里会随会话线性膨胀。要看全文请调大这个值，
        或用 `chunk_id` 回原文件核对。
    """
    limit = CITE_SNIPPET_CHARS if snippet_chars is None else max(50, snippet_chars)
    out = []
    for i, (chunk, score) in enumerate(hits, start=1):
        body = (chunk.get("content") or chunk.get("text") or "").strip()
        cite = {
            "n": i,
            "source": chunk.get("source", ""),
            "score": round(float(score), 4),
            "chunk_id": chunk.get("chunk_id", ""),
            "kind": chunk.get("kind", ""),
            "parser": chunk.get("parser", ""),
            "origin": chunk.get("origin", ""),
            "section": chunk.get("section", ""),
            "page": chunk.get("page"),
            "pages": list(chunk.get("pages") or []),
            "bbox": chunk.get("bbox") or [],
            "para_index": chunk.get("para_index"),
            "anchor_text": chunk.get("anchor_text", ""),
            "snippet": body[:limit] + ("…" if len(body) > limit else ""),
            "truncated": len(body) > limit,
        }
        # 附件类信息只在真有的时候才带出去，避免一堆空字段把引用撑胖
        if chunk.get("table_md"):
            cite["table_md"] = chunk["table_md"]
        if chunk.get("source_image"):
            cite["source_image"] = chunk["source_image"]
        if chunk.get("page_image"):
            cite["page_image"] = chunk["page_image"]
        if chunk.get("alt_text"):
            cite["alt_text"] = chunk["alt_text"]
        if chunk.get("tags"):
            cite["tags"] = list(chunk["tags"])
        cite["where"] = _where(chunk)
        out.append(cite)
    return out


def _page_span(chunk) -> str:
    """页码范围的人话形态：单页 `3`；连续多页 `3–5`；不连续逐页列 `3、5`。

    chunk: 单个块 dict。读 pages（完整范围）与 page（起始页，兜底）两个键。
        两者都空时返回空串。

    为什么要判"连续"：跨页块的页码通常是连着的，但也可能跳页（中间那页只有
    页眉页脚这类家具物、没进本块）。直接写 `3–5` 会让核对的人以为第 4 页也有内容，
    所以不连续时逐页列出来更诚实。
    """
    pages = sorted({int(p) for p in (chunk.get("pages") or []) if p})
    if not pages:
        page = chunk.get("page")
        return str(page) if page else ""
    if len(pages) == 1:
        return str(pages[0])
    if pages == list(range(pages[0], pages[-1] + 1)):
        return f"{pages[0]}–{pages[-1]}"
    return "、".join(str(p) for p in pages)


def _where(chunk) -> str:
    """一句人能读懂的位置说明：文件 + 页/工作表 + 章节。

    chunk: 单个块 dict。读 source / pages / page / para_index / parser / section
        六个键 —— parser 用来判断"页码"该说成页、工作表还是幻灯片。

    存在的理由很实际：`page=3, bbox=[...]` 对机器够用，对"我要去核对一下"的人不够用。
    把"在哪份文件的哪里"直接写成一句话，前端可以直接显示，不必自己拼。

    这里也是 `page` 与 `pages` 的分工所在：`page` 只是起始页（代表值），跨页块的
    真实范围由 `pages` 给出 —— 否则引用里写"第 3 页"而内容到第 5 页，去核对的人
    在那一页只看得到一半。
    """
    parts = [chunk.get("source") or "?"]
    span = _page_span(chunk)
    if span:
        # PDF 说"第 N 页"；xlsx/pptx 的 page 是工作表序号/幻灯片序号，含义不同
        kind = chunk.get("kind") or ""
        parser = (chunk.get("parser") or "").lower()
        if parser in ("xlsx", "xlsm"):
            parts.append(f"第 {span} 个工作表")
        elif parser == "pptx":
            parts.append(f"第 {span} 页幻灯片")
        else:
            parts.append(f"第 {span} 页")
    elif chunk.get("para_index") is not None:
        parts.append(f"第 {chunk['para_index']} 段附近")
    if chunk.get("section"):
        parts.append(chunk["section"])
    return " · ".join(parts)


def answer(question, retriever, model):
    """一次性问答（命令行演示用，不经 graph）：检索 → 拼 prompt → 调模型。

    question: 用户问题。
    retriever: 检索器实例（BM25Retriever / EmbeddingRetriever / HybridRetriever /
        NullRetriever 均可，只需有 search(question) 方法）。
    model: 对话模型实例（需有 invoke(prompt)）。

    返回 (回答文本, contexts)；检索为空时返回固定话术与空列表，
    **不会调模型**（省一次无谓的调用）。
    """
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
    """写一份内置示例语料（intro.txt，内容是梧州简介），返回落盘路径。

    folder: 目标目录，不存在会自动创建。同名文件**会被覆盖**。
    """
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
    """命令行入口：`python -m src.rag.retrieval --docs data --question "..."`。

    无参数（全部走 argparse 的命令行开关）。
    语料为空时会自动生成示例文档并重载；未配 AGNES_API_KEY 直接 SystemExit。
    """
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
