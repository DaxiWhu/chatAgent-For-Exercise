"""
简单版 RAG（检索增强生成）
================================
- 检索：默认 sklearn TF-IDF 字符 n-gram（纯本地）；可切换为 embedding 语义检索
- 语料：支持 .txt / .md（递归切分）与 .pdf（教程链路：pdfminer 解析 + 句子级交叠切块）
- 生成：复用 chatAgent 的 ChatOpenAI + agnes 后端

运行（在项目根目录执行）：
    python -m src.retrieval --docs data --question "你的提问"
    python -m src.retrieval --make-sample        # 生成一份示例文档

说明：
- 检索后端由环境变量 RAG_EMBEDDING 决定：
    tfidf    （默认）纯本地关键词检索，零外部依赖
    semantic 语义检索，需要 EMBEDDING_* 三件套且服务方支持 /v1/embeddings
- **embedding 与 LLM 分开配置**：LLM 走 AGNES_*，embedding 走 EMBEDDING_*。
  （agnes 后端目前不提供 embeddings 接口，所以必须单独指一个服务）
- EMBEDDING_* 未填写时，即使 RAG_EMBEDDING=semantic 也会自动回退 TF-IDF，只告警不崩。
- PDF 解析在 src/pdf_parser.py（从教程 examples/pdf_split_*.py 收编并清理）。
- 本文件原名 rag.py，为消除与教程目录 rag/ 的同名冲突而迁入 src/ 并改名。
"""

import os
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

# ---------- LLM 配置（生成用，走 agnes）----------
MODEL_NAME = os.getenv("AGNES_MODEL", "agnes-2.0-flash")
API_KEY = os.getenv("AGNES_API_KEY", "")
BASE_URL = os.getenv("AGNES_API_BASE", "https://apihub.agnes-ai.com/v1")

# ---------- Embedding 配置（检索用，与 LLM 解耦，由使用者填写）----------
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
try:
    EMBEDDING_BATCH = max(1, int(os.getenv("EMBEDDING_BATCH", "10")))
except ValueError:
    EMBEDDING_BATCH = 10

# 查询侧指令前缀：BGE 系模型的检索效果对前缀敏感（文档侧不加，只加在 query 上）。
# 实测（bge-m3，8 条中文查询 / 6 篇语料）Top1 准确率：
#   无前缀 38% ｜ 中文指令 25% ｜ 英文指令 62% ｜ "检索：" 38%
# 因此默认留空不改行为，建议按所用模型实测后开启。
EMBEDDING_QUERY_PREFIX = os.getenv("EMBEDDING_QUERY_PREFIX", "")

# 检索后端： tfidf（默认，纯本地） | semantic（embedding 语义检索）
EMBEDDING_BACKEND = os.getenv("RAG_EMBEDDING", "tfidf")
TOP_K = 3

# TF-IDF 分词参数：中文没有空格，默认的 word 分词会把整句当成一个 token，
# 导致查询词与语料词表完全匹配不上（实测全部 0 命中）。改用字符 n-gram，中英文都友好。
TFIDF_ANALYZER = "char_wb"
TFIDF_NGRAM_RANGE = (2, 4)


# ---------- 1. 加载文档 ----------
def load_documents(doc_folder: str):
    """读取目录下所有 .txt / .md 文件，返回 [(文件名, 文本), ...]

    目录不存在或未配置时返回空列表（不抛异常），由调用方决定降级策略。
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


# ---------- 2. 切分 ----------
def _make_splitter(chunk_size, chunk_overlap):
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap
    )


def split_documents(docs, chunk_size=500, chunk_overlap=50):
    """纯文本切分：[(name, text)] -> [{"source","text"}]（不含 PDF 处理）"""
    splitter = _make_splitter(chunk_size, chunk_overlap)
    chunks = []
    for name, text in docs:
        for piece in splitter.split_text(text):
            chunks.append({"source": name, "text": piece})
    return chunks


def load_chunks(doc_folder, chunk_size=500, chunk_overlap=50, pdf_chunk_size=800,
                pdf_overlap=200):
    """语料读取总入口：目录 → [{"source","text"}]，按扩展名选解析/切分策略。

    支持格式（详见 src/loaders.py）：
      txt/md/log/rst、csv/tsv、json/jsonl、html/htm、docx、xlsx/xlsm、pptx、pdf
    切分策略：
      - .pdf       : 教程链路（pdfminer 提文本 → 短行断段 → 句子级交叠切块）
      - 其余格式    : 递归字符切分（chunk_size / chunk_overlap）

    单个文件解析失败只跳过并告警，不影响其余语料。
    """
    if not doc_folder or not os.path.isdir(doc_folder):
        return []

    from .pdf_parser import pdf_to_chunks
    from .loaders import load_text, supported_extensions

    exts = supported_extensions()
    splitter = _make_splitter(chunk_size, chunk_overlap)
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
                chunks += [{"source": name, "text": p} for p in pieces]
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
        chunks += [{"source": name, "text": p} for p in splitter.split_text(text)]

    if skipped:
        logging.warning(
            "以下扩展名暂不支持，已跳过：%s",
            ", ".join(f"{e or '(无扩展名)'}×{n}" for e, n in sorted(skipped.items())),
        )

    return chunks


# ---------- 3. 检索 ----------
class TfidfRetriever:
    """基于 sklearn TF-IDF 字符 n-gram 的轻量检索器（纯本地，无外部依赖）"""

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
    """语义检索：走独立的 EMBEDDING_* 配置（OpenAI 兼容 /v1/embeddings）。

    与 LLM 解耦——agnes 只管生成，embedding 由 EMBEDDING_API_BASE / KEY / MODEL 指定。
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
        q = self.embedder.embed_query(EMBEDDING_QUERY_PREFIX + query)
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


def embedding_configured() -> bool:
    """embedding 配置是否已填写（API base 可留空，走服务方默认地址）。"""
    return bool(EMBEDDING_API_KEY and EMBEDDING_MODEL)


def build_retriever(chunks):
    """按 RAG_EMBEDDING 选择检索后端；embedding 没配好则自动回退 TF-IDF。"""
    if not chunks:
        logging.info("RAG 语料为空，使用 NullRetriever（检索将返回空结果）")
        return NullRetriever()

    if EMBEDDING_BACKEND in ("semantic", "openai"):
        if embedding_configured():
            logging.info("RAG 检索后端：semantic（embedding 模型=%s）", EMBEDDING_MODEL)
            return EmbeddingRetriever(chunks)
        logging.warning(
            "RAG_EMBEDDING=%s 但 EMBEDDING_API_KEY / EMBEDDING_MODEL 尚未填写，"
            "本次自动回退 TF-IDF。在 .env 填好 EMBEDDING_* 即可启用语义检索。",
            EMBEDDING_BACKEND,
        )

    return TfidfRetriever(chunks)


# ---------- 4. 生成 ----------
def build_prompt(question, contexts):
    """contexts: [({"source":..., "text":...}, score), ...]"""
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


# ---------- 示例 ----------
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
        print(f"{args.docs} 下没有可用文档（.txt/.md/.pdf），正在生成示例...")
        make_sample_docs(args.docs)
        chunks = load_chunks(args.docs)

    print(f"已加载语料，共 {len(chunks)} 个片段。")

    model = ChatOpenAI(model=MODEL_NAME, api_key=API_KEY, base_url=BASE_URL)
    retriever = build_retriever(chunks)
    print(f"检索后端：{type(retriever).__name__}（RAG_EMBEDDING={EMBEDDING_BACKEND}，"
          f"embedding 已配置={embedding_configured()}）\n")

    question = args.question or "梧州有什么特产？六堡茶是什么茶？"
    print(f"问题：{question}\n")
    ans, ctx = answer(question, retriever, model)
    print("回答：")
    print(ans)
    print("\n（引用片段来源）")
    for (c, score) in ctx:
        print(f"  - {c['source']}  相似度={score:.3f}")


if __name__ == "__main__":
    main()
