"""多租户检索隔离：租户从请求一路传导到向量库。

背景（这是本轮修的真实缺陷）
--------------------------
改造前检索层的租户只有一个来源：`retrieval.RAG_TENANT`，而它是**模块导入时读一次**
的常量，`graph.get_retriever()` 又把检索器**单例缓存**。合起来的效果是：

    第一个请求用哪个租户，之后**所有**请求都跟着用那个租户。

更糟的是当 `RAG_TENANT` 为空（默认）时，`PgVectorStore.search` 里的
`if self.tenant:` 为假 ⇒ **完全不拼 tenant 条件** ⇒ 租户 A 的查询能召回租户 B 的文档。
而同一套系统的会话归属、审核队列、配额**都是按租户隔离的** —— 唯独检索是漏的，
这种"大部分隔离 + 一处不隔离"比完全不隔离更危险，因为它看起来是安全的。

本文件锁住三件事：
  1. 租户确实从运行配置传到了 `get_retriever`；
  2. 不同租户拿到的是**不同**的检索器实例（而不是共享第一个请求的那个）；
  3. 只有"租户由 Key 反查"时才允许用它做隔离 —— 自报租户模式下必须退回默认值，
     否则等于给了攻击者一个"改成别人的租户就读到别人资料"的开关。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

import src.graph as g
from src.rag import retrieval as R


# ---------------------------------------------------------------------------
# resolve_tenant：两个来源归一
# ---------------------------------------------------------------------------
def test_resolve_tenant_none_falls_back_to_process_default(monkeypatch):
    """None = 调用方不关心 ⇒ 沿用 RAG_TENANT，保证老调用点行为不变。"""
    monkeypatch.setattr(R, "RAG_TENANT", "process-default")
    assert R.resolve_tenant(None) == "process-default"


def test_resolve_tenant_explicit_value_wins(monkeypatch):
    monkeypatch.setattr(R, "RAG_TENANT", "process-default")
    assert R.resolve_tenant("acme") == "acme"


def test_resolve_tenant_empty_string_means_no_isolation(monkeypatch):
    """空串是**显式**要求不隔离，与 None（未指定）语义不同。"""
    monkeypatch.setattr(R, "RAG_TENANT", "process-default")
    assert R.resolve_tenant("") == ""


def test_resolve_tenant_strips_whitespace(monkeypatch):
    """请求头可能带空格，'acme ' 与 'acme' 必须是同一个租户（否则各建一个分片）。"""
    monkeypatch.setattr(R, "RAG_TENANT", "")
    assert R.resolve_tenant("  acme  ") == "acme"


# ---------------------------------------------------------------------------
# get_retriever：按租户分片
# ---------------------------------------------------------------------------
@pytest.fixture
def isolated_retriever_state(monkeypatch):
    """把 get_retriever 的模块级状态清空，并让 build_retriever 变成可观测的假实现。"""
    monkeypatch.setattr(g, "_RETRIEVER", None)
    monkeypatch.setattr(g, "_CHUNKS", [{"text": "块"}])
    monkeypatch.setattr(g, "_TENANT_RETRIEVERS", {})
    built: list[str | None] = []

    def fake_build(chunks, tenant=None):
        built.append(tenant)
        return {"tenant": tenant, "chunks": chunks}

    monkeypatch.setattr(g, "build_retriever", fake_build)
    return built


def test_retrievers_are_distinct_per_tenant(isolated_retriever_state):
    """核心断言：两个租户不能拿到同一个检索器实例。

    退回"单例缓存"的实现时，第二次调用会返回第一次的对象 ⇒ 本用例红。
    """
    built = isolated_retriever_state
    a = g.get_retriever("acme")
    b = g.get_retriever("globex")
    assert a is not b, "不同租户共用了同一个检索器（隔离失效）"
    assert a["tenant"] == "acme"
    assert b["tenant"] == "globex"
    assert built == ["acme", "globex"]


def test_same_tenant_hits_cache(isolated_retriever_state):
    """同一租户第二次必须走缓存 —— 否则每个请求都重建索引（pg 后端还会重连库）。"""
    built = isolated_retriever_state
    first = g.get_retriever("acme")
    again = g.get_retriever("acme")
    assert first is again
    assert built == ["acme"], "同一租户被重复构建"


def test_no_tenant_uses_process_default_and_shares_one_instance(isolated_retriever_state):
    """tenant=None（CLI/脚本/老调用点）回到"进程内只建一次"的原有语义。"""
    built = isolated_retriever_state
    r1 = g.get_retriever()
    r2 = g.get_retriever()
    assert r1 is r2
    assert built == [None], "无租户语境下不应重复构建"


def test_injected_retriever_takes_priority_and_ignores_tenant(monkeypatch):
    """测试/自定义注入的检索器优先，且不分片（注入即"调用方自负隔离"）。"""
    sentinel = object()
    monkeypatch.setattr(g, "_RETRIEVER", sentinel)
    assert g.get_retriever("acme") is sentinel
    assert g.get_retriever() is sentinel


def test_tenant_cache_is_bounded(monkeypatch, isolated_retriever_state):
    """缓存必须有上界。

    租户字符串来自请求；若某天有人在自报租户模式下用它分片，无上限的缓存
    等于"发一串不同的租户名就能把内存打满"。这里钉住"超限会淘汰"。
    """
    monkeypatch.setattr(g, "_TENANT_CACHE_MAX", 2)
    for t in ("t1", "t2", "t3"):
        g.get_retriever(t)
    assert len(g._TENANT_RETRIEVERS) <= 2, "分片缓存没有上界，存在内存膨胀风险"
    # 最新的那个必须还在（淘汰的是最老的，不是刚建的）
    assert "t3" in g._TENANT_RETRIEVERS


def test_eviction_does_not_lose_the_just_built_retriever(monkeypatch,
                                                         isolated_retriever_state):
    """淘汰顺序：丢最老的、保留刚建的。丢错方向会让"每次请求都重建"。"""
    monkeypatch.setattr(g, "_TENANT_CACHE_MAX", 1)
    r1 = g.get_retriever("t1")
    r2 = g.get_retriever("t2")
    assert g.get_retriever("t2") is r2, "刚构建的分片被淘汰了，导致反复重建"
    assert g.get_retriever("t1") is not r1


# ---------------------------------------------------------------------------
# retrieve 节点：从运行配置取租户
# ---------------------------------------------------------------------------
class _RecordingRetriever:
    def __init__(self):
        self.queries: list[tuple[str, int]] = []

    def search(self, query, top_k=3):
        self.queries.append((query, top_k))
        return []


def test_retrieve_reads_tenant_from_config(monkeypatch):
    """租户走 configurable.rag_tenant —— 这是"运行参数"，不该进 state。

    走 state 的话会被检查点持久化，续跑时可能带着过期的租户值。
    """
    seen: list[str | None] = []
    inner = _RecordingRetriever()

    def fake_get(tenant=None):
        seen.append(tenant)
        return inner

    monkeypatch.setattr(g, "get_retriever", fake_get)
    state = {"messages": [HumanMessage(content="六堡茶是什么")]}
    g.retrieve(state, {"configurable": {"rag_tenant": "acme"}})
    assert seen == ["acme"], "retrieve 没把配置里的租户传给检索器"


def test_retrieve_without_config_uses_default(monkeypatch):
    """没有 config（直接调用节点）时不能炸 —— 退回默认租户。"""
    seen: list[str | None] = []
    monkeypatch.setattr(g, "get_retriever",
                        lambda tenant=None: (seen.append(tenant), _RecordingRetriever())[1])
    g.retrieve({"messages": [HumanMessage(content="q")]})
    assert seen == [None]


def test_retrieve_tolerates_malformed_config(monkeypatch):
    """config 结构异常（不是 dict / configurable 不是 dict）时同样退回默认。"""
    seen: list[str | None] = []
    monkeypatch.setattr(g, "get_retriever",
                        lambda tenant=None: (seen.append(tenant), _RecordingRetriever())[1])
    state = {"messages": [HumanMessage(content="q")]}
    g.retrieve(state, None)
    g.retrieve(state, "not-a-dict")
    g.retrieve(state, {"configurable": "not-a-dict"})
    assert seen == [None, None, None]


# ---------------------------------------------------------------------------
# PgVectorRetriever：tenant 落到 store
# ---------------------------------------------------------------------------
class _FakeStore:
    """最小 PgVectorStore 替身：只记录收到的租户与检索条件。"""

    def __init__(self, existing=None, rows=None):
        self._existing = set(existing or [])
        self._rows = rows or []
        self.searched = []

    def existing_ids(self):
        return set(self._existing)

    def ensure_schema(self, dim):
        pass

    def upsert(self, rows):
        return len(rows)

    def search(self, vector, top_k=3):
        self.searched.append((tuple(vector), top_k))
        return list(self._rows)


@pytest.fixture
def fake_embeddings(monkeypatch):
    """把 OpenAIEmbeddings 换成不发网络请求的替身。

    不加这个的话，构造 PgVectorRetriever 会**真的去打 embedding 接口**
    （`_sync()` 发现有缺失块就调 `embed_documents`）—— 用例会挂在网络上，
    还可能把测试环境的调用额度烧掉。这是"单元测试不该碰网络"的硬约束。
    """
    import langchain_openai

    class _Emb:
        def __init__(self, *args, **kwargs):
            pass

        def embed_documents(self, texts):
            return [[0.1, 0.2, 0.3] for _ in texts]

        def embed_query(self, text):
            return [0.1, 0.2, 0.3]

    monkeypatch.setattr(langchain_openai, "OpenAIEmbeddings", _Emb)
    return _Emb


def test_pg_retriever_binds_tenant_on_store_construction(monkeypatch, fake_embeddings):
    """store 是自建的时候，租户必须写进 PgVectorStore 构造参数。

    这是隔离真正生效的地方：PgVectorStore.search 里 `if self.tenant:` 为假就
    不拼 tenant 条件 ⇒ 等于全库可见。
    """
    captured = {}

    class _StoreSpy:
        def __init__(self, dsn, table, index, tenant=""):
            captured["tenant"] = tenant

        def existing_ids(self):
            return set()

        def ensure_schema(self, dim):
            pass

        def upsert(self, rows):
            return len(rows)

    monkeypatch.setattr(R, "PgVectorStore", _StoreSpy)

    r = R.PgVectorRetriever([{"text": "块", "source": "a"}], tenant="acme")
    assert captured["tenant"] == "acme", "自建 store 时租户没传下去"
    assert r.tenant == "acme"


def test_pg_retriever_tenant_defaults_to_process_value(monkeypatch, fake_embeddings):
    """不传 tenant 时退回模块级 RAG_TENANT —— **老调用点行为不变**。"""
    captured = {}
    monkeypatch.setattr(R, "RAG_TENANT", "env-tenant")

    class _StoreSpy:
        def __init__(self, dsn, table, index, tenant=""):
            captured["tenant"] = tenant

        def existing_ids(self):
            return set()

        def ensure_schema(self, dim):
            pass

        def upsert(self, rows):
            return len(rows)

    monkeypatch.setattr(R, "PgVectorStore", _StoreSpy)

    r = R.PgVectorRetriever([{"text": "块", "source": "a"}])
    assert captured["tenant"] == "env-tenant"
    assert r.tenant == "env-tenant"


def test_pg_retriever_resolved_tenant_visible_when_store_injected(monkeypatch,
                                                                  fake_embeddings):
    """store 由外部注入（测试场景）时，实例上仍要留下解析后的租户，
    便于上层与排查工具判断"这个检索器属于谁"。"""
    monkeypatch.setattr(R, "RAG_TENANT", "env-tenant")
    r = R.PgVectorRetriever([{"text": "块", "source": "a"}], store=_FakeStore())
    assert r.tenant == "env-tenant"

    r2 = R.PgVectorRetriever([{"text": "块", "source": "a"}], store=_FakeStore(),
                             tenant="acme")
    assert r2.tenant == "acme"


def test_cross_tenant_rows_do_not_leak_through_fingerprints():
    """不同租户的同内容块必须算出**不同**指纹 —— 否则会互相覆盖。

    指纹是 upsert 的主键（`ON CONFLICT ... DO UPDATE`），撞了就真的覆盖。
    """
    chunk = {"text": "同一段内容", "source": "a.md", "section": ""}
    fa = R.chunk_fingerprint(chunk, "acme")
    fb = R.chunk_fingerprint(chunk, "globex")
    assert fa != fb, "跨租户同内容块指纹相同 ⇒ 会互相覆盖"
    assert fa == R.chunk_fingerprint(chunk, "acme")



# ---------------------------------------------------------------------------
# app 层：只信"Key 反查"出来的租户
# ---------------------------------------------------------------------------
def _identity(tenant: str, via: str):
    from app.main import Identity
    return Identity(tenant, via)


def test_retrieval_tenant_trusts_key_bound_tenant(monkeypatch):
    """Key 反查得到的租户不可伪造 ⇒ 可以用于隔离。"""
    from app import main as M
    monkeypatch.setattr(M.config, "DEFAULT_TENANT", "default")
    assert M._retrieval_tenant(_identity("acme", "tenant-key")) == "acme"


@pytest.mark.parametrize("via", ["global-key", "anonymous"])
def test_retrieval_tenant_ignores_self_reported(monkeypatch, via):
    """**安全断言**：自报租户不得用于检索隔离。

    改用 ident.tenant 的实现会返回 "globex"（攻击者声明的值）⇒ 本用例红。
    这条防的是"假隔离"：客户端把 X-Tenant-Id 改成别人的就能读到别人的资料。
    """
    from app import main as M
    monkeypatch.setattr(M.config, "DEFAULT_TENANT", "default")
    assert M._retrieval_tenant(_identity("globex", via)) == "default"


# ---------------------------------------------------------------------------
# tracing：租户确实进了 configurable
# ---------------------------------------------------------------------------
def test_build_run_config_writes_rag_tenant():
    from app import tracing as T
    cfg = T.build_run_config({"configurable": {"thread_id": "t1"}},
                             thread_id="t1", tenant="acme", rag_tenant="acme")
    assert cfg["configurable"]["rag_tenant"] == "acme"
    assert cfg["configurable"]["thread_id"] == "t1", "原有配置被覆盖了"


def test_build_run_config_omits_rag_tenant_when_none():
    """不传时不写该键 —— 让检索节点走"进程级默认"这条老路径。"""
    from app import tracing as T
    cfg = T.build_run_config({"configurable": {"thread_id": "t1"}}, thread_id="t1")
    assert "rag_tenant" not in cfg["configurable"]


def test_build_run_config_does_not_mutate_input():
    """返回新 dict，且不就地改入参的 configurable。

    就地改会让调用方的 cfg 被悄悄污染 —— 例如 review 端点复用同一个 cfg
    先查状态、再拼 run_cfg，污染后两者语义就分叉了。
    """
    from app import tracing as T
    base = {"configurable": {"thread_id": "t1"}}
    T.build_run_config(base, thread_id="t1", tenant="acme", rag_tenant="acme")
    assert base["configurable"] == {"thread_id": "t1"}, "入参被就地修改了"
