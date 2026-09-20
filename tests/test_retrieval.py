"""RAG 检索层测试：全部离线，不调用任何外部服务。"""
import src.retrieval as R
from src.retrieval import (
    TfidfRetriever,
    NullRetriever,
    build_retriever,
    build_prompt,
    embedding_configured,
    load_documents,
    split_documents,
)

CHUNKS = [
    {"source": "intro.txt", "text": "梧州是广西壮族自治区的一个地级市，地处广西东部，靠近广东。"},
    {"source": "intro.txt", "text": "梧州特产有龟苓膏、冰泉豆浆、六堡茶。六堡茶是黑茶的一种，有祛湿功效。"},
    {"source": "intro.txt", "text": "梧州代表性景点有骑楼城、龙母庙、白云山。"},
]


# ---------------- TF-IDF 中文检索（回归）----------------
def test_tfidf_chinese_retrieval_works():
    """回归：默认 word 分词在中文上实测 0 命中；改用字符 n-gram 后必须能召回。"""
    r = TfidfRetriever(CHUNKS)
    for q in ["梧州有什么特产？", "六堡茶是什么茶？", "骑楼城", "梧州的气候怎么样"]:
        hits = r.search(q, top_k=1)
        assert hits, f"查询 {q!r} 未召回任何片段"
        assert hits[0][1] > 0


def test_tfidf_picks_most_relevant_chunk():
    r = TfidfRetriever(CHUNKS)
    hits = r.search("六堡茶", top_k=1)
    assert hits
    assert "六堡茶" in hits[0][0]["text"]
    # 返回结构必须是 [(chunk_dict, score)]，与 retrieve 节点约定一致
    chunk, score = hits[0]
    assert set(chunk) >= {"source", "text"}
    assert isinstance(score, float)


# ---------------- 加载 / 切分 ----------------
def test_load_documents_missing_dir_returns_empty():
    assert load_documents("no/such/dir") == []
    assert load_documents("") == []
    assert load_documents(None) == []


def test_load_and_split_roundtrip(tmp_path):
    (tmp_path / "a.txt").write_text("梧州特产有六堡茶。", encoding="utf-8")
    docs = load_documents(str(tmp_path))
    assert len(docs) == 1 and docs[0][0] == "a.txt"
    chunks = split_documents(docs)
    assert chunks and chunks[0]["source"] == "a.txt"
    assert "六堡茶" in chunks[0]["text"]


# ---------------- 后端选择 ----------------
def test_build_retriever_empty_chunks_returns_null():
    assert isinstance(build_retriever([]), NullRetriever)


def test_build_retriever_default_is_tfidf(monkeypatch):
    monkeypatch.setattr(R, "EMBEDDING_BACKEND", "tfidf")
    assert isinstance(build_retriever(CHUNKS), TfidfRetriever)


def test_build_retriever_semantic_without_config_falls_back(monkeypatch):
    """选了 semantic 但 EMBEDDING_* 没填 → 自动回退 TF-IDF，不抛异常。"""
    monkeypatch.setattr(R, "EMBEDDING_BACKEND", "semantic")
    monkeypatch.setattr(R, "EMBEDDING_API_KEY", "")
    monkeypatch.setattr(R, "EMBEDDING_MODEL", "")
    assert isinstance(build_retriever(CHUNKS), TfidfRetriever)


def test_build_retriever_semantic_with_config_uses_embedding(monkeypatch):
    """配置齐全时应选择 embedding 检索器（此处替身，避免真调外部服务）。"""

    class FakeEmbeddingRetriever:
        def __init__(self, chunks):
            self.chunks = chunks

        def search(self, query, top_k=3):
            return []

    monkeypatch.setattr(R, "EMBEDDING_BACKEND", "semantic")
    monkeypatch.setattr(R, "EMBEDDING_API_KEY", "fake-key")
    monkeypatch.setattr(R, "EMBEDDING_MODEL", "fake-model")
    monkeypatch.setattr(R, "EmbeddingRetriever", FakeEmbeddingRetriever)

    r = build_retriever(CHUNKS)
    assert isinstance(r, FakeEmbeddingRetriever)


def test_embedding_configured_requires_key_and_model(monkeypatch):
    monkeypatch.setattr(R, "EMBEDDING_API_KEY", "")
    monkeypatch.setattr(R, "EMBEDDING_MODEL", "")
    assert embedding_configured() is False

    monkeypatch.setattr(R, "EMBEDDING_API_KEY", "k")
    assert embedding_configured() is False  # 只有 key 还不够

    monkeypatch.setattr(R, "EMBEDDING_MODEL", "m")
    assert embedding_configured() is True  # base 可留空


# ---------------- 提示词拼装（回归）----------------
def test_build_prompt_uses_chunk_fields_not_dict_keys():
    """回归：曾经写成 `for (src, txt), _ in contexts`，把 dict 解包成键名，
    导致提示词里出现字面量 '[来源: source] text' 而不是真实内容。"""
    prompt = build_prompt("梧州特产？", [(CHUNKS[1], 0.9)])
    assert "intro.txt" in prompt
    assert "六堡茶" in prompt
    assert "[来源: source]" not in prompt


# ---------------- embedding 查询前缀 ----------------
class _FakeEmbedder:
    """替身：记录被传入的文本，返回固定向量，避免真实调用 embedding 服务。"""

    def __init__(self):
        self.queries: list[str] = []
        self.documents: list[str] = []

    def embed_documents(self, texts):
        self.documents = list(texts)
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text):
        self.queries.append(text)
        return [1.0, 0.0]


def _patch_embedder(monkeypatch, fake):
    import langchain_openai

    monkeypatch.setattr(langchain_openai, "OpenAIEmbeddings", lambda **kw: fake)
    monkeypatch.setattr(R, "EMBEDDING_MODEL", "fake-model")
    monkeypatch.setattr(R, "EMBEDDING_API_KEY", "fake-key")


def test_query_prefix_applied_only_to_query(monkeypatch):
    """指令前缀只能加在查询侧，文档侧不能加。"""
    fake = _FakeEmbedder()
    _patch_embedder(monkeypatch, fake)
    monkeypatch.setattr(R, "EMBEDDING_QUERY_PREFIX", "PFX ")

    r = R.EmbeddingRetriever(CHUNKS)
    r.search("六堡茶")

    assert fake.queries == ["PFX 六堡茶"]
    assert all(not t.startswith("PFX") for t in fake.documents)


def test_no_prefix_by_default(monkeypatch):
    fake = _FakeEmbedder()
    _patch_embedder(monkeypatch, fake)
    monkeypatch.setattr(R, "EMBEDDING_QUERY_PREFIX", "")

    r = R.EmbeddingRetriever(CHUNKS)
    r.search("六堡茶")

    assert fake.queries == ["六堡茶"]


def test_embedding_retriever_returns_expected_shape(monkeypatch):
    fake = _FakeEmbedder()
    _patch_embedder(monkeypatch, fake)
    monkeypatch.setattr(R, "EMBEDDING_QUERY_PREFIX", "")

    r = R.EmbeddingRetriever(CHUNKS)
    hits = r.search("任意", top_k=2)
    assert len(hits) == 2
    chunk, score = hits[0]
    assert set(chunk) >= {"source", "text"}
    assert isinstance(score, float)


def test_embedding_base_url_normalized():
    """容错：把 …/v1/embeddings 整条填进来也要能用（客户端会再拼一次）。"""
    for raw, expected in [
        ("https://api.siliconflow.cn/v1/embeddings", "https://api.siliconflow.cn/v1"),
        ("https://api.siliconflow.cn/v1/", "https://api.siliconflow.cn/v1"),
        ("https://api.siliconflow.cn/v1", "https://api.siliconflow.cn/v1"),
        ("", ""),
    ]:
        assert R._normalize_api_base(raw) == expected
