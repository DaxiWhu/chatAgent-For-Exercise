"""
简单版 RAG（检索增强生成）
================================
- 检索：用 sklearn 的 TF-IDF 做向量召回（无需向量库，开箱即跑）
- 生成：复用 chatAgent 里的 ChatOpenAI + agnes 后端
- 切分：langchain 的 RecursiveCharacterTextSplitter

运行（在项目根目录执行）：
    python -m src.retrieval --docs data --question "你的提问"
    python -m src.retrieval --make-sample        # 先生成一份示例文档
    python -m src.retrieval --docs data          # 不传 question 会用内置示例问题

说明：
- 默认用 TF-IDF 关键词检索，足够简单场景。若要更强的语义检索，
  把下方 EMBEDDING_BACKEND 改成 "openai" 并确保 agnes 支持 /v1/embeddings。
- 本文件原名 rag.py，为消除与教程目录 rag/ 的同名冲突而迁入 src/ 并改名。
"""

import os
import glob
import argparse

from langchain_openai import ChatOpenAI
from langchain_text_splitters import RecursiveCharacterTextSplitter

# ---------- 配置（和你的 notebook 保持一致，从环境变量读）----------
MODEL_NAME = os.getenv("AGNES_MODEL", "agnes-2.0-flash")
API_KEY = os.getenv("AGNES_API_KEY", "")
BASE_URL = os.getenv("AGNES_API_BASE", "https://apihub.agnes-ai.com/v1")

# 检索后端： "tfidf"（默认，无需 embedding 接口）或 "openai"（语义更强，需 agnes 支持 embeddings）
EMBEDDING_BACKEND = os.getenv("RAG_EMBEDDING", "tfidf")
TOP_K = 3


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
def split_documents(docs, chunk_size=500, chunk_overlap=50):
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap
    )
    chunks = []
    for name, text in docs:
        for piece in splitter.split_text(text):
            chunks.append({"source": name, "text": piece})
    return chunks


# ---------- 3. 检索 ----------
class TfidfRetriever:
    """基于 sklearn TF-IDF 的轻量检索器"""

    def __init__(self, chunks):
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        self.chunks = chunks
        self.cosine = cosine_similarity
        self.vectorizer = TfidfVectorizer()
        self.matrix = self.vectorizer.fit_transform([c["text"] for c in chunks])

    def search(self, query, top_k=TOP_K):
        q = self.vectorizer.transform([query])
        sims = self.cosine(q, self.matrix)[0]
        idx = sims.argsort()[::-1][:top_k]
        return [(self.chunks[i], float(sims[i])) for i in idx if sims[i] > 0]


class OpenAIRetriever:
    """语义检索（可选）：用 OpenAI 兼容 embeddings，需 agnes 支持 /v1/embeddings"""

    def __init__(self, chunks):
        from langchain_openai import OpenAIEmbeddings
        import numpy as np

        self.chunks = chunks
        self.np = np
        self.embedder = OpenAIEmbeddings(
            model=os.getenv("AGNES_EMBED_MODEL", "text-embedding-3-small"),
            api_key=API_KEY, base_url=BASE_URL,
        )
        self.vectors = self.embedder.embed_documents([c["text"] for c in chunks])

    def search(self, query, top_k=TOP_K):
        q = self.embedder.embed_query(query)
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


def build_retriever(chunks):
    if not chunks:
        return NullRetriever()
    if EMBEDDING_BACKEND == "openai":
        return OpenAIRetriever(chunks)
    return TfidfRetriever(chunks)


# ---------- 4. 生成 ----------
def build_prompt(question, contexts):
    ctx = "\n\n".join(
        f"[来源: {src}]\n{txt}" for (src, txt), _ in contexts
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
    parser = argparse.ArgumentParser(description="简单 RAG 示例")
    parser.add_argument("--docs", default="data", help="文档目录")
    parser.add_argument("--question", default=None, help="提问（不填用内置示例）")
    parser.add_argument("--make-sample", action="store_true", help="生成示例文档")
    args = parser.parse_args()

    if args.make_sample:
        p = make_sample_docs(args.docs)
        print(f"已生成示例文档：{p}")

    if not API_KEY:
        raise SystemExit("请先设置环境变量 AGNES_API_KEY")

    docs = load_documents(args.docs)
    if not docs:
        print(f"{args.docs} 下没有 .txt/.md 文档，正在生成示例...")
        make_sample_docs(args.docs)
        docs = load_documents(args.docs)

    chunks = split_documents(docs)
    print(f"已加载 {len(docs)} 篇文档，切成 {len(chunks)} 个片段。"
          f"（检索后端：{EMBEDDING_BACKEND}）\n")

    model = ChatOpenAI(model=MODEL_NAME, api_key=API_KEY, base_url=BASE_URL)
    retriever = build_retriever(chunks)

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
