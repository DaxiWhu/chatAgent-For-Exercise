"""索引身份（`src/rag/index_identity.py`）的契约。

要解决的问题：`EMBEDDING_MODEL` 是环境变量，而 Qdrant 的 collection 名 / pg 的表名
是**固定的**。于是换模型时有两种结局：维度变了会被 `ensure_schema` 显式拦住（好）；
**维度没变、语义空间变了**则完全静默 —— 旧向量还在库里、新查询用新模型编码，
两者的向量空间对不上，检索质量当场崩掉，而所有指标一切正常。

本文件钉住的都是"错了也不会报错、只是悄悄失效"的部分：

  1. **校验必须发生在 `_sync` 的早退之前** —— 换完模型重启恰好走的就是"库已最新、
     零 embedding 调用"那条路径；校验若挂在写入之后，最该报的这一次永远不会报；
  2. **维度必须从库里读，不能从配置推** —— pg 侧压根没有维度配置项，而且从库里读
     才不需要任何 embedding 调用；
  3. **`dim == 0`（库还是空的）时既不能记录也不能报警** —— 否则"还没建库"会被
     永久记成"已核对"，真正的不匹配从此再也不报；
  4. **读身份失败时绝不能覆盖已有记录** —— 覆盖掉之后这条链路永远是绿的；
  5. **索引身份不一致不能让就绪探针失败** —— 那是数据问题不是实例问题，
     摘流量/重启解决不了，只会额外制造一次停机；但它必须**可见**；
  6. **`src/rag/` 不得反向依赖 `app/`** —— 指标由 app 层读 `last_status()` 后上报。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import src.rag.retrieval as R
from src.rag import index_identity as ii
from src.rag.qdrant_store import QdrantStore, point_id
from src.rag.vector_store import PgVectorStore, chunk_fingerprint

ROOT = Path(__file__).resolve().parents[1]


def _chunk(source, text, section=""):
    return {"source": source, "section": section, "text": text}


@pytest.fixture(autouse=True)
def _clean_identity_state():
    """每个用例前后都清掉进程内缓存 —— 它是模块级单例，最容易造成跨用例污染。"""
    ii.reset_status()
    yield
    ii.reset_status()


@pytest.fixture
def fake_emb(monkeypatch):
    """假 embedding（固定 3 维），避免任何真实 API 调用。"""
    import langchain_openai

    class _Emb:
        def __init__(self, **kw):
            pass

        def embed_documents(self, texts):
            return [[0.1, 0.2, 0.3] for _ in texts]

        def embed_query(self, query):
            return [0.1, 0.2, 0.3]

    monkeypatch.setattr(langchain_openai, "OpenAIEmbeddings", _Emb)
    return _Emb


@pytest.fixture
def fake_qdrant(monkeypatch):
    """没有 `qdrant_client` 时也能测 Qdrant 侧逻辑：注入一个最小的 `models` 替身。

    为什么用注入而不是 `importorskip`：下面两条守卫（元数据点不带向量 / 遍历时跳过
    元数据点）在任何环境里都必须生效。跳过它们等于"本地绿、线上红" ——
    而它们守的是"元数据点混进块数据"，那正是最容易误删的链路。
    """
    import sys
    import types

    pkg = types.ModuleType("qdrant_client")
    models = types.ModuleType("qdrant_client.models")

    class PointStruct:
        def __init__(self, id=None, vector=None, payload=None):
            self.id = id
            self.vector = vector
            self.payload = payload

    models.PointStruct = PointStruct
    pkg.models = models
    monkeypatch.setitem(sys.modules, "qdrant_client", pkg)
    monkeypatch.setitem(sys.modules, "qdrant_client.models", models)
    return models


# ===========================================================================
# 1. 指纹本身
# ===========================================================================
def test_fingerprint_is_stable_and_hex():
    fp = ii.embedding_fingerprint("BAAI/bge-m3", 1024)
    assert fp == ii.embedding_fingerprint("BAAI/bge-m3", 1024)
    assert len(fp) == ii.FINGERPRINT_CHARS
    assert all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_changes_with_model_or_dim():
    """★ 这条是整条链路存在的理由：模型换了、维度没换，指纹也必须变。"""
    base = ii.embedding_fingerprint("model-a", 1024)
    assert ii.embedding_fingerprint("model-b", 1024) != base
    assert ii.embedding_fingerprint("model-a", 768) != base


def test_fingerprint_ignores_query_prefix():
    """★ `EMBEDDING_QUERY_PREFIX` 只作用于查询侧（文档向量不变），不该进指纹。

    进了的话，"调了下查询指令"会被判成"必须重灌库" —— 那是错的建议，
    而错误的建议会让人做出真正有害的操作（白烧一次全量 embedding）。
    """
    assert ii.embedding_fingerprint("m", 1024) == ii.embedding_fingerprint("m", 1024)
    d1 = ii.describe("m", 1024, query_prefix="为这个查询生成向量：")
    d2 = ii.describe("m", 1024, query_prefix="")
    assert d1["fingerprint"] == d2["fingerprint"]
    # 但"有没有配"必须记下来，供人工核对
    assert d1["query_prefix_set"] is True
    assert d2["query_prefix_set"] is False


def test_fingerprint_has_no_pairing_ambiguity():
    """★ 拼接要用分隔符：`("m", 12)` 与 `("m1", 2)` 不能撞成同一个指纹。"""
    assert ii.embedding_fingerprint("m", 12) != ii.embedding_fingerprint("m1", 2)


def test_describe_never_records_prefix_body():
    """指令正文可能是一整段提示词，没必要进库 —— 只记"配没配"。"""
    got = ii.describe("m", 8, query_prefix="x" * 500)
    assert all(len(str(v)) < 100 for v in got.values())


# ===========================================================================
# 2. verify 的五种结论
# ===========================================================================
class _MetaStore:
    """只实现索引身份需要的三个方法的假 store，并记录写入。"""

    def __init__(self, dim=1024, recorded=None, fail_read=False, fail_write=False,
                 bad_json=False):
        self._dim = dim
        self._recorded = recorded
        self.fail_read = fail_read
        self.fail_write = fail_write
        self.bad_json = bad_json
        self.index_dim_calls = 0
        self.written = {}

    def index_dim(self):
        self.index_dim_calls += 1
        return self._dim

    def get_meta(self, key):
        if self.fail_read:
            raise RuntimeError("boom-read")
        if self.bad_json:
            return "{不是合法 JSON"
        return self._recorded

    def set_meta(self, key, value):
        if self.fail_write:
            raise RuntimeError("boom-write")
        self.written[key] = value
        self._recorded = value          # 模拟持久化：第二次校验应判成 ok


def test_verify_records_on_first_run():
    s = _MetaStore()
    got = ii.verify(s, model="BAAI/bge-m3", dim=1024, backend="pgvector")
    assert got["status"] == "recorded"
    assert got["ok"] is True
    saved = json.loads(s.written[ii.REMEMBER_KEY])
    assert saved["model"] == "BAAI/bge-m3"
    assert saved["dim"] == 1024
    assert saved["fingerprint"] == got["fingerprint"]


def test_verify_is_ok_on_second_run():
    s = _MetaStore()
    ii.verify(s, model="m", dim=1024)
    got = ii.verify(s, model="m", dim=1024)
    assert got["status"] == "ok"
    assert got["ok"] is True


def test_verify_detects_model_swap_at_same_dim():
    """★ 本改动要防的那个场景：维度一样、语义空间不同，一切指标正常。"""
    s = _MetaStore()
    ii.verify(s, model="BAAI/bge-m3", dim=1024)
    got = ii.verify(s, model="someone-else/same-dim-model", dim=1024)
    assert got["status"] == "mismatch"
    assert got["ok"] is False
    assert got["recorded"]["model"] == "BAAI/bge-m3"


def test_verify_detects_dim_change():
    s = _MetaStore()
    ii.verify(s, model="m", dim=768)
    assert ii.verify(s, model="m", dim=1024)["status"] == "mismatch"


def test_verify_does_not_stamp_when_dimension_unknown():
    """★ 库还是空的（dim=0）时**不能写记录、也不能报警**。

    写了就等于"用当前配置盖了个章"，而这个章证明不了库里任何东西 ——
    以后真换模型时它会给出"身份一致"的假结论。
    """
    s = _MetaStore()
    got = ii.verify(s, model="m", dim=0, backend="pgvector")
    assert got["status"] == "unknown"
    assert got["ok"] is None
    assert s.written == {}


def test_verify_reports_error_when_write_fails():
    s = _MetaStore(fail_write=True)
    got = ii.verify(s, model="m", dim=1024)
    assert got["status"] == "error"
    assert got["ok"] is None


def test_verify_does_not_overwrite_when_read_fails():
    """★ 读身份失败时**不能**把旧记录覆盖成当前身份。

    覆盖掉之后这条链路永远显示"一致"，从此外观全绿而检索质量已经崩了 ——
    是最坏的一种失效。所以读失败必须与"库里确实没记过"分开处理。
    """
    s = _MetaStore(fail_read=True)
    got = ii.verify(s, model="m", dim=1024)
    assert got["status"] == "error"
    assert got["ok"] is None
    assert s.written == {}


def test_verify_does_not_overwrite_when_record_is_corrupt():
    """记录存在但解析不了 = 现场坏了，同样不能覆盖（那会把证据抹掉）。"""
    s = _MetaStore(bad_json=True)
    got = ii.verify(s, model="m", dim=1024)
    assert got["status"] == "error"
    assert s.written == {}


def test_last_status_is_a_copy():
    """调用方（就绪探针）改到返回值不该污染内部状态。"""
    ii.verify(_MetaStore(), model="m", dim=1024)
    got = ii.last_status()
    got["status"] = "篡改"
    assert ii.last_status()["status"] == "recorded"


def test_reset_status_clears():
    ii.verify(_MetaStore(), model="m", dim=1024)
    ii.reset_status()
    assert ii.last_status() == {}


# ===========================================================================
# 3. ★ 早退路径上仍然校验（本改动最容易做漏的地方）
# ===========================================================================
class _SyncStore:
    """假 store：够 `PgVectorRetriever._sync` / `QdrantRetriever._sync` 跑完，
    并记录"读了几次维度"（= 校验了几次）。"""

    def __init__(self, existing=(), dim=1024):
        self._existing = set(existing)
        self._dim = dim
        self._recorded = None
        self.dim_calls = 0
        self.written = {}
        self.upserted = []
        self.ensured_dim = None

    def existing_ids(self):
        return set(self._existing)

    def ensure_schema(self, dim):
        self.ensured_dim = dim
        # 建表之后维度才第一次可知 —— 这正是"要再判一次"的理由
        self._dim = int(dim)

    def upsert(self, rows):
        rows = list(rows)
        self.upserted.extend(rows)
        return len(rows)

    def search(self, vector, top_k=3, sources=None):
        return []

    def index_dim(self):
        self.dim_calls += 1
        return self._dim

    def get_meta(self, key):
        return self._recorded

    def set_meta(self, key, value):
        self.written[key] = value
        self._recorded = value


def test_pg_sync_verifies_identity_even_when_index_is_up_to_date(fake_emb):
    """★ 库里已是最新（零 embedding 调用）时**仍然**要校验身份。

    换完模型重启走的就是这条路径：库里每个块都"已存在"，一个新块都不写。
    把校验挂在写入之后的实现，最该报的这一次恰恰永远不会报 ——
    而且所有测试都是绿的（"没报警"与"没检查"长得一模一样）。
    """
    chunks = [_chunk("s", "t")]
    store = _SyncStore(existing={chunk_fingerprint(chunks[0], R.resolve_tenant(""))}, dim=1024)

    R.PgVectorRetriever(chunks, store=store, tenant="")

    assert store.upserted == []                       # 确实走了早退路径
    assert store.dim_calls == 1                       # 但身份校验照做
    assert ii.REMEMBER_KEY in store.written           # 且把身份记下来了
    assert ii.last_status()["status"] == "recorded"


def test_qdrant_sync_verifies_identity_even_when_index_is_up_to_date(fake_emb):
    """同 pg：Qdrant 侧的早退路径也必须先校验。"""
    chunks = [_chunk("s", "t")]
    store = _SyncStore(existing={point_id(chunk_fingerprint(chunks[0], R.resolve_tenant("")))}, dim=1024)

    R.QdrantRetriever(chunks, store=store, tenant="")

    assert store.upserted == []
    assert store.dim_calls == 1
    assert ii.REMEMBER_KEY in store.written


def test_pg_sync_records_identity_once_dimension_becomes_known(fake_emb):
    """首次灌库时入口那次读不到维度（unknown），建表后才落定身份。"""
    store = _SyncStore(existing=set(), dim=0)

    R.PgVectorRetriever([_chunk("s", "t")], store=store, tenant="")

    assert store.upserted, "首次灌库应当真的写入"
    assert store.dim_calls == 2, "入口 + 建表后各判一次"
    assert ii.last_status()["status"] == "recorded"
    assert ii.last_status()["dim"] == 3, "记的是**库里实际的维度**（假 embedding 是 3 维）"


def test_pg_sync_survives_store_without_identity_support(fake_emb):
    """★ 老式 store（没有 index_dim/get_meta/set_meta）不能把灌库带崩。

    这是"降级不崩"的口径：身份校验是**观测**，观测失败不该影响主流程。
    """

    class _Bare:
        def existing_ids(self):
            return set()

        def ensure_schema(self, dim):
            pass

        def upsert(self, rows):
            return len(list(rows))

    R.PgVectorRetriever([_chunk("s", "t")], store=_Bare(), tenant="")   # 不抛即通过


# ===========================================================================
# 4. open_vector_store / selfcheck_index_identity
# ===========================================================================
def test_open_vector_store_returns_none_for_memory_backend(monkeypatch):
    monkeypatch.setattr(R, "VECTOR_STORE", "memory")
    store, backend = R.open_vector_store()
    assert store is None
    assert backend == "memory"


def test_open_vector_store_builds_pg_store_without_side_effects(monkeypatch):
    """★ 自检用的 store 只"打开"，不建表、不灌库、不算 embedding。"""
    monkeypatch.setattr(R, "VECTOR_STORE", "pg")
    monkeypatch.setattr(R, "DB_URL", "postgresql://u:p@h/db")
    monkeypatch.setattr(R, "PGVECTOR_TABLE", "t1")
    store, backend = R.open_vector_store("")
    assert backend == "pgvector"
    assert isinstance(store, PgVectorStore)
    assert store.table == "t1"


def test_selfcheck_is_skipped_for_memory_backend(monkeypatch):
    monkeypatch.setattr(R, "VECTOR_STORE", "memory")
    got = R.selfcheck_index_identity()
    assert got["status"] == "skipped"
    assert got["ok"] is None


def test_selfcheck_reads_dimension_from_the_store(monkeypatch):
    """★ 维度来自库里的实际值，不是配置 —— pg 侧根本没有维度配置项。"""
    store = _SyncStore(dim=768)
    monkeypatch.setattr(R, "open_vector_store", lambda tenant=None: (store, "pgvector"))
    got = R.selfcheck_index_identity()
    assert got["dim"] == 768
    assert store.dim_calls == 1


def test_selfcheck_never_raises_when_store_unreachable(monkeypatch):
    """★ 自检读不到库要返回 error，而不是抛异常 —— 它不该拦住启动。"""

    class _Broken:
        def index_dim(self):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(R, "open_vector_store", lambda tenant=None: (_Broken(), "pgvector"))
    got = R.selfcheck_index_identity()
    assert got["status"] == "error"
    assert got["ok"] is None


# ===========================================================================
# 5. store 层：元数据的存放位置与"不能污染块数据"
# ===========================================================================
def test_pg_meta_table_follows_main_table():
    """★ 换表是换模型的推荐做法，身份记录必须跟着表走，否则新旧表会串味。"""
    assert PgVectorStore("dsn", "rag_chunks").meta_table == "rag_chunks_meta"
    assert (PgVectorStore("dsn", "rag_chunks_d1024").meta_table
            == "rag_chunks_d1024_meta")


class _ScriptedCursor:
    """按顺序吐出预设结果的假游标，并记录执行过的 SQL。"""

    def __init__(self, results=()):
        self.sqls = []
        self._results = list(results)

    def execute(self, sql, params=None):
        self.sqls.append((sql, params))

    def fetchone(self):
        return self._results.pop(0) if self._results else None

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


def test_pg_get_meta_returns_none_when_meta_table_absent(monkeypatch):
    """首次灌库前 `_meta` 表不存在 —— 这是正常路径，不该抛错也不该留 WARNING。"""
    store = PgVectorStore("postgresql://u:p@h/db", "rag_chunks")
    cur = _ScriptedCursor([(None,)])
    monkeypatch.setattr(store, "_connect", lambda: _FakeConn(cur))

    assert store.get_meta("k") is None
    assert "to_regclass" in cur.sqls[0][0]


def test_pg_set_meta_creates_meta_table_and_upserts(monkeypatch):
    store = PgVectorStore("postgresql://u:p@h/db", "rag_chunks")
    cur = _ScriptedCursor()
    monkeypatch.setattr(store, "_connect", lambda: _FakeConn(cur))

    store.set_meta("k", "v")

    joined = " ".join(sql for sql, _ in cur.sqls)
    assert "CREATE TABLE IF NOT EXISTS rag_chunks_meta" in joined
    assert "ON CONFLICT (key) DO UPDATE" in joined


class _FakeQClient:
    """假 Qdrant 客户端：够测 meta 点的读写与"滚动时跳过 meta 点"。"""

    def __init__(self, points=()):
        self._points = list(points)
        self.structs = []
        self.stored = {}

    def get_collections(self):
        """`_scroll_rows` 现在先问"集合到底存不存在"（区分首灌与读失败）。

        替身必须跟着提供这一问，否则假客户端会在**新的**第一步就 AttributeError ——
        而那与"读失败"在测试里长得一样，会掩盖真正的回归。
        """
        return SimpleNamespace(collections=[SimpleNamespace(name="rag_chunks")])

    def scroll(self, collection_name, scroll_filter=None, limit=512, offset=None,
               with_payload=True, with_vectors=False):
        return list(self._points), None

    def retrieve(self, collection_name, ids, with_payload=True, with_vectors=False):
        out = []
        for i in ids:
            payload = self.stored.get(str(i))
            if payload is not None:
                out.append(SimpleNamespace(id=i, payload=payload))
        return out

    def upsert(self, collection_name, points, wait=True):
        self.structs.extend(points)
        for p in points:
            self.stored[str(p.id)] = p.payload


def test_qdrant_set_meta_writes_point_without_vector(monkeypatch, fake_qdrant):
    """★ 元数据点必须**不带向量**：带向量就会出现在检索结果里，变成一个假命中。"""
    store = QdrantStore("http://q:6333", "rag_chunks")
    client = _FakeQClient()
    monkeypatch.setattr(store, "_connect", lambda: client)

    store.set_meta("k", "v")

    struct = client.structs[0]
    assert str(struct.id) == QdrantStore._meta_point_id("k")
    assert not struct.vector, "元数据点不能带向量"
    assert struct.payload[ii.META_FLAG] is True
    assert struct.payload["value"] == "v"


def test_qdrant_meta_roundtrip(monkeypatch, fake_qdrant):
    """写入后能原样读回 —— 读不回来的话每次启动都会重新"记录"，等于没记住。"""
    store = QdrantStore("http://q:6333", "rag_chunks")
    client = _FakeQClient()                      # 必须复用同一个实例，否则写进 A 读 B
    monkeypatch.setattr(store, "_connect", lambda: client)

    assert store.get_meta("k") is None
    store.set_meta("k", "v")
    assert store.get_meta("k") == "v"


def test_qdrant_list_rows_skips_index_metadata_point(monkeypatch, fake_qdrant):
    """★ 遍历 payload 的维护接口必须跳过元数据点。

    不跳的话它会变成一条 `("", "", "")` 的脏行，被 `plan_orphans` 当成一个源 ——
    而这条链路的唯一用途是**决定删什么**，一个假输入就足以让人误删。
    """
    store = QdrantStore("http://q:6333", "rag_chunks")
    meta = SimpleNamespace(id="m", payload={ii.META_FLAG: True, "key": "k", "value": "v"})
    real = SimpleNamespace(
        id="r",
        payload={"tenant": "", "source_key": "a/b.md", "content_hash": "h"},
    )
    monkeypatch.setattr(store, "_connect", lambda: _FakeQClient([meta, real]))

    assert store.list_rows() == {("", "a/b.md", "h")}


def test_qdrant_meta_point_id_is_deterministic_and_distinct():
    assert (QdrantStore._meta_point_id("k1")
            == QdrantStore._meta_point_id("k1"))
    assert QdrantStore._meta_point_id("k1") != QdrantStore._meta_point_id("k2")


def test_qdrant_meta_point_id_does_not_alias_a_chunk_point():
    """★ 元数据点的 id 不能落在块 id 的空间里。

    块的 id 是 `point_id(chunk_fingerprint)`。若元数据点直接用 `point_id(key)`
    不加前缀，那么"某个块的指纹恰好等于这个 key"时两者就是**同一个点** ——
    那个块会被写成一条没有向量的元数据（检索永远命不中它），
    而"块被静默吞掉"是事后查不出来的。
    """
    assert (QdrantStore._meta_point_id(ii.REMEMBER_KEY)
            != point_id(ii.REMEMBER_KEY))


# ===========================================================================
# 6. app 层：就绪探针回显（可见但不判失败）+ 启动自检上报指标
# ===========================================================================
def _with_qdrant_client(monkeypatch):
    """让 `_check_vector_store` 的 qdrant 分支认为客户端库"已安装"。

    为什么需要：2026-10-30 起该分支会真的 `import qdrant_client`（那个库在
    `src/rag/qdrant_store.py` 里是方法内延迟导入的，只看配置看不出装没装）。
    本节用例要验的是**索引身份回显**，不是依赖是否装好 —— 不塞这个替身的话，
    没装 qdrant 的机器上它们会因为"缺客户端库"而红，验的东西就跑偏了。
    """
    monkeypatch.setitem(__import__("sys").modules, "qdrant_client",
                        ModuleType("qdrant_client"))


def test_readiness_reports_identity_mismatch_without_failing(monkeypatch):
    """★ 不一致必须**可见**，但**不能**让就绪探针失败。

    检索质量差是**数据问题**（库里灌的是旧模型的向量），不是这个实例的问题 ——
    摘流量、重启都解决不了它，只会额外制造一次停机。
    """
    from app import health

    monkeypatch.setattr(R, "VECTOR_STORE", "qdrant")
    monkeypatch.setattr(R, "QDRANT_URL", "http://q:6333")
    monkeypatch.setattr(R, "QDRANT_COLLECTION", "rag_chunks")
    _with_qdrant_client(monkeypatch)

    s = _MetaStore()
    ii.verify(s, model="old-model", dim=1024)
    ii.verify(s, model="new-model", dim=1024)
    assert ii.last_status()["status"] == "mismatch"

    ok, detail = health._check_vector_store()
    assert ok is True, "数据问题不该把实例摘出负载均衡"
    assert "索引身份不一致" in detail
    assert "old-model" in detail and "new-model" in detail


def test_readiness_says_nothing_when_never_checked(monkeypatch):
    """没校验过就不要凭空写一句结论 —— 空缓存不等于"身份没问题"。"""
    from app import health

    monkeypatch.setattr(R, "VECTOR_STORE", "qdrant")
    monkeypatch.setattr(R, "QDRANT_URL", "http://q:6333")
    _with_qdrant_client(monkeypatch)
    ok, detail = health._check_vector_store()
    assert ok is True
    assert "索引身份" not in detail


def test_readiness_flags_missing_qdrant_url(monkeypatch):
    """顺手补上前一个缺口：qdrant 分支此前**完全没有任何检查**。"""
    from app import health

    monkeypatch.setattr(R, "VECTOR_STORE", "qdrant")
    monkeypatch.setattr(R, "QDRANT_URL", "")
    ok, detail = health._check_vector_store()
    assert ok is False
    assert "QDRANT_URL" in detail


def test_startup_report_writes_identity_metric(monkeypatch):
    from app import main, metrics

    metrics.reset()
    monkeypatch.setattr(R, "selfcheck_index_identity", lambda tenant=None: {
        "status": "mismatch", "ok": False, "backend": "qdrant", "detail": "d",
    })
    main._report_index_identity()

    assert metrics.gauge("rag_index_identity_ok") == 0.0
    assert metrics.value("rag_index_identity_mismatch_total") == 1.0


def test_startup_report_skips_metric_when_undecidable(monkeypatch):
    """★ `ok is None`（内存后端 / 库还是空的 / 读取失败）时**不上报**。

    上报 0 会把"还没建库"记成一次真实的"索引不可用" —— 从此告警常亮，
    真出事时反而没人看。
    """
    from app import main, metrics

    metrics.reset()
    monkeypatch.setattr(R, "selfcheck_index_identity", lambda tenant=None: {
        "status": "skipped", "ok": None, "backend": "memory", "detail": "d",
    })
    main._report_index_identity()

    assert "rag_index_identity_ok" not in metrics.render()


def test_startup_report_never_raises(monkeypatch):
    """自检炸了也不能拦住启动（服务本身仍可用）。"""
    from app import main, metrics

    metrics.reset()

    def _boom(tenant=None):
        raise RuntimeError("store down")

    monkeypatch.setattr(R, "selfcheck_index_identity", _boom)
    main._report_index_identity()          # 不抛即通过


# ===========================================================================
# 7. ★ 依赖方向守卫
# ===========================================================================
def _imported_top_level_modules(tree):
    """列出源码里出现的**绝对**导入的顶层模块名。"""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and not node.level:
            out.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name.split(".")[0])
    return out


def test_rag_layer_never_imports_app_layer():
    """★ `src/rag/` 是纯库层（CLI 也直接跑它），不得反向依赖 `app/`。

    这条守卫是刻意加的：指标上报需要 `app.metrics`，最省事的写法就是在 RAG 侧直接
    `from app import metrics` —— 那会让 `python -m src.rag.retrieval` 在 app 有
    循环导入时直接炸，也会把分层彻底打乱。
    正确做法：结论缓存在 `index_identity.last_status()`，由 app 层读后上报。
    """
    offenders = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if "app" in _imported_top_level_modules(tree):
            offenders.append(str(path.relative_to(ROOT)).replace("\\", "/"))
    assert offenders == [], f"src/ 下出现了对 app/ 的反向依赖：{offenders}"
