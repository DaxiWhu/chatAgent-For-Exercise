"""Qdrant 存储层与 QdrantRetriever 的离线测试。

**全部用替身，不连真实 Qdrant、不调外部服务。**

覆盖点：
- Bm25SparseEncoder 的稀疏编码（排序正确 / 索引升序去重 / 空输入）；
- point_id 的确定性与区分度、_safe_collection 白名单；
- QdrantStore 未配 URL 时 `_connect` 明确报错；
- QdrantRetriever 的**增量同步**（首次全建 / 二次全跳过 / 部分缺失，复用点 id 判定）；
- sparse / dense 两个适配器各自委派到 store 的对应检索方法；
- _make_dense_retriever 与 build_retriever 在 qdrant 后端下的选路与失败回退。
"""

import pytest

import src.rag.retrieval as R
from src.rag.qdrant_store import (
    Bm25SparseEncoder,
    QdrantStore,
    _safe_collection,
    point_id,
)
from src.rag.vector_store import chunk_fingerprint


def _chunk(source, text, section=""):
    return {"source": source, "section": section, "text": text}


# ---------------- 纯逻辑：BM25 稀疏编码器 ----------------
def test_bm25_sparse_encoder_orders_by_relevance():
    """同一查询下，命中块的稀疏点积必须高于不相关块（否则排序无意义）。"""
    corpus = ["六堡茶是梧州特产，历史悠久", "高铁从广州到梧州大约两小时", "梧州龟苓膏也很出名"]
    enc = Bm25SparseEncoder(corpus)
    qi, qv = enc.encode_query("六堡茶")
    assert qi, "查询应能编码出稀疏向量"

    def dot(text):
        bi, bv = enc.encode_document(text)
        bd = dict(zip(bi, bv, strict=False))
        return sum(v * bd.get(i, 0.0) for i, v in zip(qi, qv, strict=False))

    assert dot("六堡茶是梧州特产，历史悠久") > dot("高铁从广州到梧州大约两小时")


def test_bm25_sparse_encoder_indices_sorted_and_unique():
    """Qdrant 稀疏向量要求 indices 升序且无重复 —— 编码器必须自带这个保证。"""
    enc = Bm25SparseEncoder(["六堡茶梧州特产历史悠久"])
    idx, val = enc.encode_document("六堡茶梧州特产历史悠久")
    assert idx == sorted(idx)
    assert len(idx) == len(set(idx))
    assert len(idx) == len(val)


def test_bm25_sparse_encoder_empty_inputs():
    """空语料 / 空文本都返回 ([], [])，不抛错（与 NullRetriever 口径一致）。"""
    enc = Bm25SparseEncoder([])
    assert enc.encode_query("任意查询") == ([], [])
    assert enc.encode_document("") == ([], [])
    assert enc.encode_query("") == ([], [])


# ---------------- point_id / 集合名白名单 ----------------
def test_point_id_is_deterministic_and_distinct():
    """同一指纹必须恒定映射到同一个 id（否则增量判定失效、每次启动全量重灌）。"""
    assert point_id("abc") == point_id("abc")
    assert point_id("abc") != point_id("abd")


def test_safe_collection_whitelist():
    assert _safe_collection("rag_chunks") == "rag_chunks"
    assert _safe_collection("Rag-Chunks_2") == "Rag-Chunks_2"
    assert _safe_collection("bad name!") == "rag_chunks"
    assert _safe_collection("") == "rag_chunks"


# ---------------- QdrantStore 连接错误 ----------------
def test_qdrant_store_no_url_raises_on_connect():
    """未配 QDRANT_URL 时连接要明确报错（由 build_retriever 捕获后回退）。"""
    with pytest.raises(RuntimeError):
        QdrantStore("")._connect()


# ---------------- 假 store / 假 embedding ----------------
class _FakeQStore:
    """记录交互的假 store：ensure_schema / existing_ids / upsert / 两路检索。"""

    def __init__(self, existing=None):
        self.existing = set(existing or [])
        self.ensured_dim = None
        self.upserted = []

    def ensure_schema(self, dim):
        self.ensured_dim = dim

    def existing_ids(self):
        return set(self.existing)

    def upsert(self, rows):
        rows = list(rows)
        self.upserted.extend(rows)
        return len(rows)

    def search_dense(self, vector, top_k=3, sources=None):
        return [({"source": "s", "text": "dense-hit"}, 0.9)][:top_k]

    def search_sparse(self, indices, values, top_k=3, sources=None):
        return [({"source": "s", "text": "sparse-hit"}, 2.0)][:top_k]


class _FakeEmb:
    """假 embedding：固定 3 维，记录调用次数。"""

    calls = 0

    def __init__(self, **kw):
        pass

    def embed_documents(self, texts):
        type(self).calls += 1
        return [[0.1, 0.2, 0.3] for _ in texts]

    def embed_query(self, query):
        return [0.1, 0.2, 0.3]


@pytest.fixture
def fake_emb(monkeypatch):
    import langchain_openai

    _FakeEmb.calls = 0
    monkeypatch.setattr(langchain_openai, "OpenAIEmbeddings", _FakeEmb)
    # 假 embedding 是 3 维，必须把维度配置同步成 3，否则 _sync 的维度校验会拦下（这是刻意的）。
    monkeypatch.setattr(R, "QDRANT_DIM", 3)
    return _FakeEmb


# ---------------- QdrantRetriever 增量同步 ----------------
def test_qdrant_retriever_first_sync_writes_all(fake_emb):
    chunks = [_chunk("s1", "a"), _chunk("s2", "b")]
    store = _FakeQStore(existing=set())
    r = R.QdrantRetriever(chunks, store=store, tenant="")

    assert len(store.upserted) == 2
    assert store.ensured_dim == 3
    assert fake_emb.calls == 1
    assert len(r.by_id) == 2
    # 点的 id 必须是 point_id(fingerprint)，这样才能与 existing_ids 对齐
    assert store.upserted[0][0] == point_id(chunk_fingerprint(chunks[0], R.resolve_tenant("")))


def test_qdrant_retriever_second_sync_skips_all(fake_emb):
    """集合内已全部存在时，必须零 embedding 调用（切向量库的核心收益）。

    注意与 PgVectorRetriever 的差别：Qdrant 后端**每次构造都会 ensure_schema**
    （保证集合与 tenant 索引存在，检索才可用），所以这里是 ensured_dim 有值、
    但 upserted 为空、embedding 零调用。
    """
    chunks = [_chunk("s1", "a"), _chunk("s2", "b")]
    existing = {point_id(chunk_fingerprint(c, R.resolve_tenant(""))) for c in chunks}
    store = _FakeQStore(existing=existing)

    R.QdrantRetriever(chunks, store=store, tenant="")

    assert store.ensured_dim == 3      # 集合结构始终被确保
    assert store.upserted == []        # 但没有任何写入
    assert fake_emb.calls == 0         # 且零 embedding 调用


def test_qdrant_retriever_syncs_only_missing(fake_emb):
    have = _chunk("s1", "a")
    missing = _chunk("s2", "b")
    store = _FakeQStore(existing={point_id(chunk_fingerprint(have, R.resolve_tenant("")))})

    R.QdrantRetriever([have, missing], store=store, tenant="")

    assert len(store.upserted) == 1
    assert store.upserted[0][0] == point_id(chunk_fingerprint(missing, R.resolve_tenant("")))


def test_qdrant_retriever_dim_mismatch_rejected(fake_emb, monkeypatch):
    """embedding 维度与 QDRANT_DIM 不一致时必须显式报错，而不是让 upsert 事后莫名失败。"""
    monkeypatch.setattr(R, "QDRANT_DIM", 1024)   # 与假 embedding 的 3 维不符
    chunks = [_chunk("s1", "a")]
    store = _FakeQStore(existing=set())
    with pytest.raises(RuntimeError):
        R.QdrantRetriever(chunks, store=store, tenant="")


# ---------------- 两路适配器委派 ----------------
def test_qdrant_adapters_delegate_to_store(fake_emb):
    chunks = [_chunk("s", "六堡茶是梧州特产")]
    store = _FakeQStore(existing={point_id(chunk_fingerprint(chunks[0], R.resolve_tenant("")))})
    r = R.QdrantRetriever(chunks, store=store, tenant="")

    sp = r.sparse.search("六堡茶", top_k=1)
    de = r.dense.search("六堡茶", top_k=1)

    assert sp[0][0]["text"] == "sparse-hit"
    assert de[0][0]["text"] == "dense-hit"


def test_qdrant_sparse_adapter_empty_query_returns_empty(fake_emb):
    """查询无有效 term（如纯空白）时稀疏路返回 []，不调用 store。"""
    chunks = [_chunk("s", "六堡茶是梧州特产")]
    store = _FakeQStore(existing={point_id(chunk_fingerprint(chunks[0], R.resolve_tenant("")))})
    r = R.QdrantRetriever(chunks, store=store, tenant="")
    assert r.sparse.search("") == []


# ---------------- 选路与回退 ----------------
def test_make_dense_uses_qdrant_when_ok(monkeypatch):
    monkeypatch.setattr(R, "VECTOR_STORE", "qdrant")

    class _Dense:
        pass

    class _Q:
        # 签名必须跟上 _make_dense_retriever 的 tenant 透传；少了它会被 except 吞掉，
        # 退化成"回退成功"，看起来通过、实际测的是另一件事（典型假绿）。
        def __init__(self, chunks, tenant=None):
            self.dense = _Dense()
            self.tenant = tenant

    monkeypatch.setattr(R, "QdrantRetriever", _Q)
    d = R._make_dense_retriever([_chunk("s", "t")], "acme")
    assert isinstance(d, _Dense)


def test_make_dense_falls_back_when_qdrant_fails(monkeypatch):
    """qdrant 构造抛错（连不上/包缺失）时必须回退内存后端，绝不冒泡。"""
    monkeypatch.setattr(R, "VECTOR_STORE", "qdrant")

    def _boom(chunks, tenant=None):
        raise RuntimeError("no qdrant")

    monkeypatch.setattr(R, "QdrantRetriever", _boom)

    class _Emb:
        def __init__(self, chunks):
            self.chunks = chunks

    monkeypatch.setattr(R, "EmbeddingRetriever", _Emb)
    assert isinstance(R._make_dense_retriever([_chunk("s", "t")]), _Emb)


def test_build_retriever_hybrid_qdrant_injects_both_paths(monkeypatch):
    """hybrid + VECTOR_STORE=qdrant：稀疏与稠密都要来自 Qdrant 后端。"""
    monkeypatch.setattr(R, "EMBEDDING_BACKEND", "hybrid")
    monkeypatch.setattr(R, "EMBEDDING_API_KEY", "fake-key")
    monkeypatch.setattr(R, "EMBEDDING_MODEL", "fake-model")
    monkeypatch.setattr(R, "VECTOR_STORE", "qdrant")
    monkeypatch.setattr(R, "RERANK_ENABLED", False)   # 避免构造 RerankClient

    class _Sparse:
        def search(self, q, top_k=3):
            return [(_chunk("s", "kw"), 1.0)]

    class _Dense:
        def search(self, q, top_k=3):
            return [(_chunk("s", "vec"), 0.9)]

    class _Q:
        def __init__(self, chunks, store=None, tenant=None):
            self.sparse = _Sparse()
            self.dense = _Dense()

    monkeypatch.setattr(R, "QdrantRetriever", _Q)

    r = R.build_retriever([_chunk("s", "六堡茶")], tenant="acme")
    assert isinstance(r, R.HybridRetriever)
    assert isinstance(r.sparse, _Sparse)
    assert isinstance(r.dense, _Dense)


def test_build_retriever_hybrid_qdrant_falls_back(monkeypatch):
    """qdrant 后端不可用时，hybrid 回退为「进程内 BM25 + 内存稠密」，不崩。"""
    monkeypatch.setattr(R, "EMBEDDING_BACKEND", "hybrid")
    monkeypatch.setattr(R, "EMBEDDING_API_KEY", "fake-key")
    monkeypatch.setattr(R, "EMBEDDING_MODEL", "fake-model")
    monkeypatch.setattr(R, "VECTOR_STORE", "qdrant")
    monkeypatch.setattr(R, "RERANK_ENABLED", False)

    def _boom(chunks, store=None, tenant=None):
        raise RuntimeError("no qdrant")

    monkeypatch.setattr(R, "QdrantRetriever", _boom)

    class _Emb:
        def __init__(self, chunks):
            self.chunks = chunks

    monkeypatch.setattr(R, "EmbeddingRetriever", _Emb)

    r = R.build_retriever([_chunk("s", "梧州特产有六堡茶。")])
    assert isinstance(r, R.HybridRetriever)
    assert isinstance(r.sparse, R.BM25Retriever)   # 稀疏回退进程内 BM25
    assert isinstance(r.dense, _Emb)               # 稠密回退内存


# ---------------- existing_ids / _scroll_rows：把"读失败"和"真的不存在"分开 ----------------
#
# ⚠️ 这一组守卫针对的是一种**花钱**的事故，不是崩溃：
# `existing_ids` 曾把"Qdrant 抖了一下"和"集合还没建"**都**吞成空集，
# 而调用方（增量同步）据此把**整个知识库**重算一遍 embedding。
# 两种情况的返回值完全一样，只有"集合到底存不存在"这一问能把它们分开。
#
# 同理 `_scroll_rows`：它的出口是 `list_tenants` → 孤儿预演 → **删除决策**，
# 读不到库却返回空集 = 在看不见数据的情况下报告"一切正常"。


class _Point:
    def __init__(self, pid, payload=None):
        self.id = pid
        self.payload = payload or {}


class _Coll:
    def __init__(self, name):
        self.name = name


class _CollList:
    def __init__(self, names):
        self.collections = [_Coll(n) for n in names]


class _FakeClient:
    """只实现 `existing_ids` / `_scroll_rows` 用到的方法。

    names: `get_collections()` 报告的集合名（不含 = 集合还不存在）。
    points: `scroll()` 返回的点（一次返回完，offset=None 结束）。
    list_error: `get_collections()` 要抛的异常（模拟"根本连不上"）。
    scroll_error: `scroll()` 要抛的异常（模拟"连得上但这次读失败"）。
    """

    def __init__(self, names=("rag_chunks",), points=(), list_error=None,
                 scroll_error=None):
        self._names = list(names)
        self._points = list(points)
        self._list_error = list_error
        self._scroll_error = scroll_error
        self.scroll_calls = 0

    def get_collections(self):
        if self._list_error:
            raise self._list_error
        return _CollList(self._names)

    def scroll(self, **_kw):
        self.scroll_calls += 1
        if self._scroll_error:
            raise self._scroll_error
        return list(self._points), None


def _qdrant_store_with(monkeypatch, client, **kw):
    """造一个 QdrantStore，并把它的连接替换成替身（不真连服务）。"""
    store = QdrantStore("http://qdrant:6333", **kw)
    monkeypatch.setattr(store, "_connect", lambda: client)
    return store


def test_existing_ids_treats_a_missing_collection_as_empty(monkeypatch):
    """集合还没建 ⇒ 空集是**对的**（首次灌库本就该走全量写入）。"""
    c = _FakeClient(names=())          # 集合还不存在
    store = _qdrant_store_with(monkeypatch, c)
    assert store.existing_ids() == set()
    assert c.scroll_calls == 0, "集合都不存在还去滚一遍（多余请求）"


def test_existing_ids_never_swallows_a_read_failure(monkeypatch):
    """集合在、但这次读失败 ⇒ **必须抛**，绝不能返回空集。

    返回空集的后果不是报错，而是**账单**：增量同步会把每个块都判成缺失，
    把整个知识库的 embedding 重算一遍并全量 upsert。
    """
    c = _FakeClient(names=("rag_chunks",), scroll_error=RuntimeError("timeout"))
    store = _qdrant_store_with(monkeypatch, c)
    with pytest.raises(RuntimeError):
        store.existing_ids()


def test_existing_ids_never_swallows_a_connectivity_failure(monkeypatch):
    """连 Qdrant 都连不上（`get_collections` 就炸）⇒ 同样必须抛。

    这条挡住的是最恶劣的形态：地址填错 / 容器没起 ⇒ 每次都被当成"首次灌库"
    ⇒ 每次都全量重算 —— 而运维看到的是"灌库成功"。
    """
    c = _FakeClient(list_error=RuntimeError("connection refused"))
    store = _qdrant_store_with(monkeypatch, c)
    with pytest.raises(RuntimeError):
        store.existing_ids()


def test_existing_ids_returns_points_when_healthy(monkeypatch):
    c = _FakeClient(points=[_Point("a"), _Point("b")])
    store = _qdrant_store_with(monkeypatch, c)
    assert store.existing_ids() == {"a", "b"}


def test_scroll_rows_never_reports_clean_when_it_cannot_read(monkeypatch):
    """`_scroll_rows` 的出口是删除决策 ⇒ 读不到时**不能**报"没有孤儿"。"""
    c = _FakeClient(names=("rag_chunks",), scroll_error=RuntimeError("timeout"))
    store = _qdrant_store_with(monkeypatch, c)
    with pytest.raises(RuntimeError):
        store.list_rows()
    with pytest.raises(RuntimeError):
        store.list_tenants()


def test_scroll_rows_treats_a_missing_collection_as_empty(monkeypatch):
    """集合还没建时维护脚本不该报错（确实没东西可维护）。"""
    c = _FakeClient(names=())
    store = _qdrant_store_with(monkeypatch, c)
    assert store.list_rows() == set()
    assert store.list_tenants() == []


# 注："跳过索引身份元数据点"已有专门的用例
# （`tests/test_index_identity.py::test_qdrant_list_rows_skips_index_metadata_point`），
# 这里**刻意不重复** —— 同一件事有两份断言时，改一处漏一处是迟早的事。


def test_retriever_keys_points_by_the_resolved_tenant(fake_emb, monkeypatch):
    """写库用的 id 必须来自 `resolve_tenant()` 的**归一化结果**，不能直接用入参。

    为什么值得单钉一条：`""` 与 `None` 都要落到公共租户（`RAG_TENANT`）。如果
    "写进去的 id"用了空串、而"增量判定查的 id"用了归一化后的值（或反过来），
    两者永远对不上 —— 表现是**每次启动都把整个库重算一遍 embedding**：
    不报错、不告警，只是账单。
    """
    monkeypatch.setattr(R, "RAG_TENANT", "public")
    chunk = _chunk("s1", "a")

    store = _FakeQStore(existing=set())
    R.QdrantRetriever([chunk], store=store, tenant="")

    written = store.upserted[0][0]
    assert written == point_id(chunk_fingerprint(chunk, "public")), \
        "没走 resolve_tenant：写库用的还是空串（增量判定将永远对不上）"
    assert written != point_id(chunk_fingerprint(chunk, "")), \
        "空串与公共租户算出了同一个 id ⇒ 归一化没生效"
