"""pgvector 存储层与 PgVectorRetriever 的离线测试。

**全部用替身，不连真实 PostgreSQL、不调外部服务。**

覆盖点：
- chunk_fingerprint 的稳定性与敏感性；
- _safe_ident 的标识符白名单；
- PgVectorStore.ensure_schema / existing_ids 的 SQL 行为（假连接）；
- PgVectorRetriever 的**增量同步**（首次全建 / 二次全跳过 / 部分缺失）；
- _make_dense_retriever 在 pg 失败时回退内存后端。
"""

import pytest

import src.rag.retrieval as R
from src.rag.vector_store import PgVectorStore, _safe_ident, chunk_fingerprint


def _chunk(source, text, section=""):
    return {"source": source, "section": section, "text": text}


# ---------------- 指纹 ----------------
def test_fingerprint_is_stable_for_same_content():
    a = _chunk("s", "梧州特产有六堡茶。", "特产")
    assert chunk_fingerprint(a) == chunk_fingerprint(dict(a))


def test_fingerprint_changes_when_any_component_changes():
    a = _chunk("s", "t", "sec")
    assert chunk_fingerprint(a) != chunk_fingerprint(_chunk("s2", "t", "sec"))
    assert chunk_fingerprint(a) != chunk_fingerprint(_chunk("s", "t", "sec2"))
    assert chunk_fingerprint(a) != chunk_fingerprint(_chunk("s", "t2", "sec"))


def test_fingerprint_missing_field_equals_empty_field():
    """缺字段与空串要算出同一个指纹，否则同一块会在库里存成两条。"""
    assert chunk_fingerprint({"text": "t"}) == chunk_fingerprint(
        {"source": "", "section": "", "text": "t"}
    )


# ---------------- 标识符白名单 ----------------
def test_safe_ident_accepts_normal_name():
    assert _safe_ident("rag_chunks") == "rag_chunks"
    assert _safe_ident("RagChunks2") == "RagChunks2"


def test_safe_ident_rejects_injection_and_empty():
    assert _safe_ident("rag_chunks; DROP TABLE x") == "rag_chunks"
    assert _safe_ident("") == "rag_chunks"
    assert _safe_ident("1bad") == "rag_chunks"


# ---------------- 假连接 ----------------
class _FakeCursor:
    def __init__(self, fetchone_value=(None,)):
        self.sqls = []
        self._fetchone_value = fetchone_value

    def execute(self, sql, params=None):
        self.sqls.append((sql, params))

    def fetchone(self):
        return self._fetchone_value

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_ensure_schema_creates_table_and_hnsw_index(monkeypatch):
    store = PgVectorStore("postgresql://u:p@localhost:5432/db", "rag_chunks", "hnsw")
    cur = _FakeCursor()
    monkeypatch.setattr(store, "_connect", lambda **kw: _FakeConn(cur))

    store.ensure_schema(1024)

    joined = " ".join(sql for sql, _ in cur.sqls)
    assert "CREATE EXTENSION IF NOT EXISTS vector" in joined
    assert "CREATE TABLE IF NOT EXISTS rag_chunks" in joined
    assert "vector(1024)" in joined
    assert "USING hnsw" in joined


def test_ensure_schema_bootstraps_the_extension_before_registering_the_type(monkeypatch):
    """全新库上 `register_vector` 会先失败 ⇒ 建扩展必须走**不注册类型**的连接。

    真实报错（2026-11-08 e2e 第一次真跑迁移，v1 就死在这里）：

        psycopg.ProgrammingError: vector type not found in the database
          File "/app/src/rag/vector_store.py", line 213, in _connect
            register_vector(conn)

    原因是 `_connect()` 里的 `register_vector` 要查 vector 类型的 OID，而
    `CREATE EXTENSION` 本来写在它的**下游**（同一个 with 块的建表语句之前）——
    于是"自动建扩展"在全新库上从未兑现过：只有管理员手动装过扩展的库才走得到建表。

    这里的假连接**按真实行为**模拟：扩展没装时，注册类型那一步直接抛。
    """
    state = {"extension": False}

    class _Cur(_FakeCursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if "CREATE EXTENSION" in sql:
                state["extension"] = True

    cur = _Cur()

    def fake_connect(*, register=True):
        if register and not state["extension"]:
            raise RuntimeError("vector type not found in the database")
        return _FakeConn(cur)

    store = PgVectorStore("postgresql://u:p@localhost:5432/db")
    monkeypatch.setattr(store, "_connect", fake_connect)

    store.ensure_schema(1024)          # 修复前：这里就抛 RuntimeError

    assert state["extension"] is True, "扩展没被创建 —— 后面的建表语句都在它下游"
    joined = " ".join(sql for sql, _ in cur.sqls)
    assert "CREATE TABLE IF NOT EXISTS rag_chunks" in joined, "扩展建好之后仍要建表"


def test_ensure_schema_rejects_bad_dim():
    store = PgVectorStore("postgresql://u:p@localhost:5432/db")
    with pytest.raises(ValueError):
        store.ensure_schema(0)


def test_existing_ids_returns_empty_when_table_absent(monkeypatch):
    """表不存在（to_regclass 返回 NULL）时应返回空集，而不是抛错。"""
    store = PgVectorStore("postgresql://u:p@localhost:5432/db")
    cur = _FakeCursor(fetchone_value=(None,))
    monkeypatch.setattr(store, "_connect", lambda **kw: _FakeConn(cur))

    assert store.existing_ids() == set()


def test_no_dsn_raises_on_connect():
    """未配 DB_URL 时连接要明确报错（由 build_retriever 捕获后回退）。"""
    store = PgVectorStore("")
    with pytest.raises(RuntimeError):
        store._connect()


# ---------------- PgVectorRetriever 增量 ----------------
class _FakeStore:
    """记录交互的假 store：existing_ids / ensure_schema / upsert。"""

    def __init__(self, existing=None):
        self.existing = set(existing or [])
        self.ensured_dim = None
        self.upserted = []

    def existing_ids(self):
        return set(self.existing)

    def ensure_schema(self, dim):
        self.ensured_dim = dim

    def upsert(self, rows):
        rows = list(rows)
        self.upserted.extend(rows)
        return len(rows)

    def search(self, vector, top_k=3, sources=None):
        return [({"source": "s", "text": "hit"}, 0.9)][:top_k]


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
    return _FakeEmb


def test_pg_retriever_first_sync_writes_all(fake_emb):
    chunks = [_chunk("s1", "a"), _chunk("s2", "b")]
    store = _FakeStore(existing=set())
    r = R.PgVectorRetriever(chunks, store=store)

    assert len(store.upserted) == 2
    assert store.ensured_dim == 3
    assert fake_emb.calls == 1
    assert len(r.by_id) == 2


def test_pg_retriever_second_sync_skips_all(fake_emb):
    """库里已全部存在时，必须零 embedding 调用（这是切 pgvector 的核心收益）。"""
    chunks = [_chunk("s1", "a"), _chunk("s2", "b")]
    existing = {chunk_fingerprint(c, R.resolve_tenant(None)) for c in chunks}
    store = _FakeStore(existing=existing)

    R.PgVectorRetriever(chunks, store=store)

    assert store.upserted == []
    assert store.ensured_dim is None
    assert fake_emb.calls == 0


def test_pg_retriever_syncs_only_missing(fake_emb):
    have = _chunk("s1", "a")
    missing = _chunk("s2", "b")
    store = _FakeStore(existing={chunk_fingerprint(have, R.resolve_tenant(None))})

    R.PgVectorRetriever([have, missing], store=store)

    assert len(store.upserted) == 1
    assert store.upserted[0][0] == chunk_fingerprint(missing, R.resolve_tenant(None))


def test_pg_retriever_search_delegates_to_store(fake_emb):
    chunks = [_chunk("s1", "a")]
    store = _FakeStore(existing={chunk_fingerprint(chunks[0], R.resolve_tenant(None))})
    r = R.PgVectorRetriever(chunks, store=store)

    hits = r.search("任意问题", top_k=1)
    assert hits and hits[0][0]["text"] == "hit"
    assert hits[0][1] == pytest.approx(0.9)


# ---------------- _make_dense_retriever 选路 ----------------
def test_make_dense_defaults_to_memory(monkeypatch):
    monkeypatch.setattr(R, "VECTOR_STORE", "memory")

    class _Emb:
        def __init__(self, chunks):
            self.chunks = chunks

    monkeypatch.setattr(R, "EmbeddingRetriever", _Emb)
    assert isinstance(R._make_dense_retriever([_chunk("s", "t")]), _Emb)


def test_make_dense_uses_pg_when_ok(monkeypatch):
    monkeypatch.setattr(R, "VECTOR_STORE", "pg")

    class _Pg:
        # 签名必须跟上 `_make_dense_retriever` 的 tenant 透传 —— 少了它这个
        # 用例会因为 TypeError 被 `except Exception` 吞掉、退化成"回退成功"，
        # 于是**看起来通过、实际测的是另一件事**（典型的假绿）。
        def __init__(self, chunks, tenant=None):
            self.chunks = chunks
            self.tenant = tenant

    monkeypatch.setattr(R, "PgVectorRetriever", _Pg)
    assert isinstance(R._make_dense_retriever([_chunk("s", "t")]), _Pg)


def test_make_dense_passes_tenant_to_pg(monkeypatch):
    """租户必须真的传到 pg 后端 —— 这是多租户隔离的入口。"""
    monkeypatch.setattr(R, "VECTOR_STORE", "pg")

    class _Pg:
        def __init__(self, chunks, tenant=None):
            self.tenant = tenant

    monkeypatch.setattr(R, "PgVectorRetriever", _Pg)
    assert R._make_dense_retriever([_chunk("s", "t")], "acme").tenant == "acme"
    # None 时不覆盖：交给 PgVectorRetriever 内部退回 RAG_TENANT
    assert R._make_dense_retriever([_chunk("s", "t")]).tenant is None


def test_make_dense_falls_back_when_pg_fails(monkeypatch):
    """pg 构造抛错（连不上/无扩展）时必须回退内存后端，绝不冒泡。"""
    monkeypatch.setattr(R, "VECTOR_STORE", "pg")

    def _boom(chunks, tenant=None):
        raise RuntimeError("no pgvector extension")

    monkeypatch.setattr(R, "PgVectorRetriever", _boom)

    class _Emb:
        def __init__(self, chunks):
            self.chunks = chunks

    monkeypatch.setattr(R, "EmbeddingRetriever", _Emb)
    assert isinstance(R._make_dense_retriever([_chunk("s", "t")]), _Emb)


def test_build_retriever_pg_backend_falls_back_without_env(monkeypatch):
    """端到端：RAG_EMBEDDING=semantic + VECTOR_STORE=pg 但库里没扩展 → 回退不崩。"""
    monkeypatch.setattr(R, "EMBEDDING_BACKEND", "semantic")
    monkeypatch.setattr(R, "EMBEDDING_API_KEY", "fake-key")
    monkeypatch.setattr(R, "EMBEDDING_MODEL", "fake-model")
    monkeypatch.setattr(R, "VECTOR_STORE", "pg")

    def _boom(chunks, tenant=None):
        raise RuntimeError("no pgvector extension")

    monkeypatch.setattr(R, "PgVectorRetriever", _boom)

    class _Emb:
        def __init__(self, chunks):
            self.chunks = chunks

    monkeypatch.setattr(R, "EmbeddingRetriever", _Emb)

    r = R.build_retriever([_chunk("s", "梧州特产有六堡茶。")])
    assert isinstance(r, _Emb)
