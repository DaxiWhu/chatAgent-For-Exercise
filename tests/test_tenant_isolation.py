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
  1. 租户确实从**请求**经 `build_context` 传到了 `get_retriever`；
  2. 不同租户拿到的是**不同**的检索器实例（而不是共享第一个请求的那个）；
  3. 只有"租户由 Key 反查"时才允许用它做隔离 —— 自报租户模式下必须退回默认值，
     否则等于给了攻击者一个"改成别人的租户就读到别人资料"的开关。
"""

from __future__ import annotations

import pytest

import src.rag.pool as P
from src import turn
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


def test_resolve_tenant_strips_whitespace(monkeypatch):
    """请求头可能带空格，'acme ' 与 'acme' 必须是同一个租户（否则各建一个分片）。"""
    monkeypatch.setattr(R, "RAG_TENANT", "")
    assert R.resolve_tenant("  acme  ") == "acme"


def test_resolve_tenant_never_returns_an_empty_string(monkeypatch):
    """⚠️ **口径反转**（2026-10-06）：空串不再是一个"关掉隔离"的暗号。

    修前这里断言的是 `resolve_tenant("") == ""`，而向量库把空租户读成
    **"不拼过滤条件"** ⇒ `RAG_TENANT` 留空这一个配置就把整个库暴露出去。
    更糟的是它与公开口径**相反**：`PRODUCTION_CONFIG.md` / `README.md` 都写着
    "RAG_TENANT 留空 = 落到 `NO_TENANT_SENTINEL` = 什么都读不到"，而 app 层
    (`_retrieval_tenant`) 确实那么做了 —— 只有 src 层这一侧静默放行。

    三种输入都必须落到"读不到东西"，任何一种返回空串都会把隔离关掉。
    """
    monkeypatch.setattr(R, "RAG_TENANT", "")
    assert R.resolve_tenant(None) == R.NO_TENANT_SENTINEL, \
        "RAG_TENANT 留空时没有 fail-closed"
    assert R.resolve_tenant("") == R.NO_TENANT_SENTINEL, \
        "空串把隔离关掉了（空串在过滤条件里 = 不加过滤）"
    assert R.resolve_tenant("   ") == R.NO_TENANT_SENTINEL, \
        "纯空格同样必须 fail-closed"


def test_resolve_tenant_treats_blank_and_none_alike(monkeypatch):
    """★ 空串与 `None` **同义**：都表示"这个调用方没有租户信息"。

    为什么必须同义：app 层的口径是 `ident.tenant or RAG_TENANT or 哨兵`，而
    `_retrieval_tenant` 里空租户**先落公共租户**。src 层若把空串另判一条
    （旧实现："空串 = 显式要求不隔离"），同一个"没有租户"在两层会得到**相反**
    结论 —— 那正是 2026-10-05 那场"RAG_TENANT 留空 = 全库可见"事故的成因。

    额外收益：`chunk_fingerprint(c, t)` 里 `t` 的取值由此唯一，不会出现
    "写库用空串、增量判定用公共租户"这种**每次启动都全量重算 embedding** 的错配。
    """
    monkeypatch.setattr(R, "RAG_TENANT", "public")
    assert R.resolve_tenant(None) == "public"
    assert R.resolve_tenant("") == "public"
    assert R.resolve_tenant("   ") == "public"
    assert R.resolve_tenant("") == R.resolve_tenant(None)


def test_resolve_tenant_falls_back_when_process_default_is_blank(monkeypatch):
    """`RAG_TENANT` 留空 + 调用方不传租户 ⇒ 哨兵值，而不是空串。"""
    monkeypatch.setattr(R, "RAG_TENANT", "   ")
    assert R.resolve_tenant(None) == R.NO_TENANT_SENTINEL
    # 但配置非空时**原样保留** —— 生产上 RAG_TENANT 是有具名值（本项目 public）的。
    monkeypatch.setattr(R, "RAG_TENANT", "public")
    assert R.resolve_tenant(None) == "public"


def test_the_src_layer_sentinel_equals_the_app_layer_one():
    """哨兵值的**两份字面量**必须相等（否则"无租户"会有两个分区名）。

    `src/` 不能反向 import `app/`，所以 `src/rag/retrieval.py` 里有一份重复的
    字面量。这条守卫把"只改一边"变成红灯 —— 否则 app 层传 A、src 层兜底用 B，
    谁都不报错，只是各自查到不同的空集合。
    """
    from app import config as app_config

    assert R.NO_TENANT_SENTINEL == app_config.NO_TENANT_SENTINEL


def test_tenant_label_renders_the_sentinel_readably():
    """日志文案：无租户要渲染成 `<无租户>`，而不是把 `%__no_tenant__%` 打出来。

    旧的 `resolve_tenant(x) or "<不隔离>"` 在反转后**恒为假**（永不触发），
    留在日志里会让人以为"空租户 = 不隔离"仍存在。
    """
    assert R.tenant_label("") == "<无租户>"
    assert R.tenant_label("acme") == "acme"


# ---------------------------------------------------------------------------
# get_retriever：按租户分片
# ---------------------------------------------------------------------------
@pytest.fixture
def isolated_retriever_state(monkeypatch):
    """把 get_retriever 的模块级状态清空，并让 build_retriever 变成可观测的假实现。

    ⚠️ patch 的目标**必须是函数真正读状态的那个模块**（这里是 ``src.rag.pool``）。
    在别的模块上 setattr 一个同名属性只会创建一个**只属于那个模块**的新变量，
    函数体根本读不到它 —— 症状是用例照常通过，断言的是一个与生产无关的假对象。
    """
    monkeypatch.setattr(P, "_RETRIEVER", None)
    monkeypatch.setattr(P, "_CHUNKS", [{"text": "块"}])
    monkeypatch.setattr(P, "_TENANT_RETRIEVERS", {})
    built: list[str | None] = []

    def fake_build(chunks, tenant=None):
        built.append(tenant)
        return {"tenant": tenant, "chunks": chunks}

    monkeypatch.setattr(P, "build_retriever", fake_build)
    return built


def test_retrievers_are_distinct_per_tenant(isolated_retriever_state):
    """核心断言：两个租户不能拿到同一个检索器实例。

    退回"单例缓存"的实现时，第二次调用会返回第一次的对象 ⇒ 本用例红。
    """
    built = isolated_retriever_state
    a = P.get_retriever("acme")
    b = P.get_retriever("globex")
    assert a is not b, "不同租户共用了同一个检索器（隔离失效）"
    assert a["tenant"] == "acme"
    assert b["tenant"] == "globex"
    assert built == ["acme", "globex"]


def test_same_tenant_hits_cache(isolated_retriever_state):
    """同一租户第二次必须走缓存 —— 否则每个请求都重建索引（pg 后端还会重连库）。"""
    built = isolated_retriever_state
    first = P.get_retriever("acme")
    again = P.get_retriever("acme")
    assert first is again
    assert built == ["acme"], "同一租户被重复构建"


def test_no_tenant_uses_process_default_and_shares_one_instance(isolated_retriever_state):
    """tenant=None（CLI/脚本/老调用点）回到"进程内只建一次"的原有语义。"""
    built = isolated_retriever_state
    r1 = P.get_retriever()
    r2 = P.get_retriever()
    assert r1 is r2
    assert built == [None], "无租户语境下不应重复构建"


def test_injected_retriever_takes_priority_and_ignores_tenant(monkeypatch):
    """测试/自定义注入的检索器优先，且不分片（注入即"调用方自负隔离"）。"""
    sentinel = object()
    monkeypatch.setattr(P, "_RETRIEVER", sentinel)
    assert P.get_retriever("acme") is sentinel
    assert P.get_retriever() is sentinel


def test_tenant_cache_is_bounded(monkeypatch, isolated_retriever_state):
    """缓存必须有上界。

    租户字符串来自请求；若某天有人在自报租户模式下用它分片，无上限的缓存
    等于"发一串不同的租户名就能把内存打满"。这里钉住"超限会淘汰"。
    """
    monkeypatch.setattr(P, "_TENANT_CACHE_MAX", 2)
    for t in ("t1", "t2", "t3"):
        P.get_retriever(t)
    assert len(P._TENANT_RETRIEVERS) <= 2, "分片缓存没有上界，存在内存膨胀风险"
    # 最新的那个必须还在（淘汰的是最老的，不是刚建的）
    assert "t3" in P._TENANT_RETRIEVERS


def test_eviction_does_not_lose_the_just_built_retriever(monkeypatch,
                                                         isolated_retriever_state):
    """淘汰顺序：丢最老的、保留刚建的。丢错方向会让"每次请求都重建"。"""
    monkeypatch.setattr(P, "_TENANT_CACHE_MAX", 1)
    r1 = P.get_retriever("t1")
    r2 = P.get_retriever("t2")
    assert P.get_retriever("t2") is r2, "刚构建的分片被淘汰了，导致反复重建"
    assert P.get_retriever("t1") is not r1


# ---------------------------------------------------------------------------
# build_context：租户从请求传到检索器工厂
# ---------------------------------------------------------------------------
def test_context_carries_request_tenant_and_builds_retriever_lazily(monkeypatch):
    """租户走「请求 → 上下文 → 检索器工厂」，且**构造上下文时不建检索器**。

    为什么这条必须钉住：租户在某一层被丢掉时，`get_retriever(None)` 会静默退回
    进程默认租户 —— 全部租户共用一份索引，且没有任何报错。
    """
    seen: list[str | None] = []

    def fake_get(tenant=None):
        seen.append(tenant)
        return {"tenant": tenant}

    monkeypatch.setattr(turn, "get_retriever", fake_get)
    ctx = turn.build_context(tenant="acme")

    assert ctx.tenant == "acme"
    assert seen == [], "构造上下文就建检索器 = 把「可能用不上」的构建提前做掉"
    ctx.retriever_factory()
    assert seen == ["acme"], "租户没传到检索器"


def test_blank_tenant_is_normalized_to_none(monkeypatch):
    """空串必须归一成 ``None``（= 进程默认租户），不能原样传下去。

    空串在检索层是一个**独立的租户键**（不是一个"不隔离"的开关），
    所以原样传下去等于查了一个谁都没有文档的租户 —— 结果为空、且不报错。
    """
    seen: list[str | None] = []
    monkeypatch.setattr(turn, "get_retriever",
                        lambda tenant=None: (seen.append(tenant), {})[1])

    for raw in ("", "   "):
        ctx = turn.build_context(tenant=raw)
        assert ctx.tenant == ""
        ctx.retriever_factory()
    assert seen == [None, None]


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
# 存储层：空租户**不再**等于"不过滤"（2026-10-06）
# ---------------------------------------------------------------------------
# 这一节是"第二层防护"：即便某条路径把空串递到了存储层（绕过 `resolve_tenant`），
# 它也必须**照样过滤**，而不是把过滤条件整个丢掉。修前 `if self.tenant:` 的判据
# 让空串落进 `else` 分支 = 不加条件 = 全库可见，且没有任何日志。
class _RecordingCursor:
    """只记录 SQL 的假游标（不连库）。"""

    def __init__(self, log):
        self._log = log
        self.rowcount = 3

    def execute(self, sql, params=None):
        self._log.append((" ".join(str(sql).split()), params))
        return self

    def fetchone(self):
        return (1,)          # to_regclass 非 None ⇒ "表存在"，别提前 return

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingConn:
    def __init__(self, log):
        self._log = log

    def cursor(self):
        return _RecordingCursor(self._log)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _sql_log(store, call):
    """跑一次 store 的某个方法，返回它实际执行过的 (SQL, 参数) 列表。"""
    log = []
    store._connect = lambda: _RecordingConn(log)
    call(store)
    return log


def _pg_store(tenant):
    from src.rag.vector_store import PgVectorStore
    return PgVectorStore("postgresql://u:p@h/db", "rag_chunks", tenant=tenant)


def test_pg_search_with_an_empty_tenant_still_filters():
    """空租户必须照样拼 `tenant = %s`（修前它等于"不过滤"，即全库可见）。"""
    log = _sql_log(_pg_store(""), lambda s: s.search([0.1, 0.2, 0.3], top_k=3))
    sql = log[-1][0]
    assert "tenant = %s" in sql, f"空租户把过滤条件丢了（= 读全部租户）：{sql}"
    assert log[-1][1][1] == "", "过滤值的绑定参数不对"


def test_pg_search_without_a_tenant_skips_the_filter():
    """显式的 `tenant=None` 才是"不过滤" —— 离线工具/迁移的唯一入口。"""
    log = _sql_log(_pg_store(None), lambda s: s.search([0.1, 0.2, 0.3], top_k=3))
    assert "tenant = %s" not in log[-1][0], "tenant=None 时不该拼租户条件"


def test_pg_existing_ids_and_delete_keys_respect_the_empty_tenant():
    """读路径与**删除**路径都要锁住：`delete_keys` 写错就是跨租户删数据。"""
    log = _sql_log(_pg_store(""), lambda s: s.existing_ids())
    assert any("WHERE tenant = %s" in sql for sql, _ in log), \
        f"existing_ids 在空租户下不过滤（会把别人的块当成已知）：{log}"

    log = _sql_log(_pg_store(""), lambda s: s.delete_keys(["a.md"]))
    sql, params = log[-1]
    assert "tenant = %s" in sql, f"DELETE 少了租户条件（跨租户删数据）：{sql}"
    assert params[-1] == ""


def test_qdrant_filters_treat_an_empty_tenant_as_a_real_value(monkeypatch):
    """Qdrant 侧同款：空租户 ⇒ 真的过滤 `tenant == ""`，不是"不过滤"。

    `qdrant_client` 是可选依赖（本机 CI 环境未必装），所以这里塞一个最小的
    `qdrant_client.models` 替身进 `sys.modules` —— 过滤条件是纯数据构造，
    不需要真客户端，而"空串是否落进 must 里"这个判据必须被真的执行到。
    """
    import sys
    import types

    if "qdrant_client" not in sys.modules:
        pkg = types.ModuleType("qdrant_client")
        models = types.ModuleType("qdrant_client.models")

        class MatchValue:
            def __init__(self, value):
                self.value = value

        class MatchAny:
            def __init__(self, any):
                self.any = any

        class FieldCondition:
            def __init__(self, key, match):
                self.key, self.match = key, match

        class Filter:
            def __init__(self, must=None, should=None):
                self.must, self.should = list(must or []), list(should or [])

        models.MatchValue, models.MatchAny = MatchValue, MatchAny
        models.FieldCondition, models.Filter = FieldCondition, Filter
        pkg.models = models
        monkeypatch.setitem(sys.modules, "qdrant_client", pkg)
        monkeypatch.setitem(sys.modules, "qdrant_client.models", models)

    from src.rag.qdrant_store import QdrantStore

    flt = QdrantStore("http://q:6333", tenant="")._tenant_filter()
    assert flt is not None, "空租户返回 None ⇒ 完全不拼过滤条件 ⇒ 全库可见"
    assert flt.must[0].match.value == ""

    assert QdrantStore("http://q:6333", tenant=None)._tenant_filter() is None, \
        "tenant=None 才是显式的'不过滤'"

    flt = QdrantStore("http://q:6333", tenant="")._source_filter()
    assert any(c.key == "tenant" for c in flt.must), "检索用的过滤器同样漏了租户"

    assert QdrantStore("http://q:6333", tenant=None)._source_filter() is None



# ---------------------------------------------------------------------------
# app 层：租户的来源与可信度（2026-10-04 口径）
# ---------------------------------------------------------------------------
def _identity(tenant: str):
    from app.main import Identity
    return Identity(tenant)


def test_retrieval_tenant_trusts_jwt_tenant(monkeypatch):
    """令牌载荷里的租户是**唯一可信来源** ⇒ 用于隔离（外部身份源已删除）。"""
    from app import main as M
    monkeypatch.setattr(M.config, "DEFAULT_TENANT", "default")
    assert M._retrieval_tenant(_identity("acme")) == "acme"


# 「无租户时怎么检索」的口径在 2026-10-05 反转了，本节随之重写
# ---------------------------------------------------------------------------
# 旧口径：`RAG_TENANT` 空串 ⇒ `_retrieval_tenant` **原样返回空串** ⇒ 检索层
#   "不加租户过滤" ⇒ 能召回**全部租户**的文档。
# 新口径（用户拍板）：「无租户可以查我们提供的公共租户文档，别的租户的不允许」。
#   ⇒ 无租户的主体落到**公共租户**；**没配**公共租户则落到哨兵值，什么都检不到。
#
# 为什么必须重写而不是"调个期望值"：旧口径与新口径的安全后果是相反的
# （全库可见 vs 什么都看不到）。留着旧用例等于把反转后的口径又钉回去。
def test_retrieval_tenant_falls_back_to_the_public_tenant(monkeypatch):
    """无租户的主体 ⇒ 落到**公共租户**：能读公共文档，读不到别人的。"""
    from app import main as M
    monkeypatch.setattr(M.config, "DEFAULT_TENANT", "public")
    assert M._retrieval_tenant(_identity("")) == "public"


def test_retrieval_tenant_is_fail_closed_without_a_public_tenant(monkeypatch):
    """没配公共租户 ⇒ 落到哨兵值，**检不到任何内容**；空串绝不能传出去。

    真正要防的是这一个：空串在检索层的语义是"**不加租户过滤**"。若把"看不见
    任何东西"的意图用空串表达，会恰好得到最坏的结果 —— 全都能看到。
    所以断言分两半：① 必须是哨兵；② 必须**非空**。
    哨兵之所以不可能与真实租户名碰撞：`%` 被 `AccountStore._check_name` 禁止。
    """
    from app import main as M
    monkeypatch.setattr(M.config, "DEFAULT_TENANT", "")
    got = M._retrieval_tenant(_identity(""))
    assert got == M.config.NO_TENANT_SENTINEL
    assert got != "", "空串会被检索层理解成「不加过滤」⇒ 反而能读全库"


def test_retrieval_tenant_ignores_any_self_reported_value(monkeypatch):
    """自报的租户**不再被采信**：租户只来自已验签的载荷。

    这里删除的是一条被拍板的例外 —— 脚本通行证（`X-API-Key`）的租户取自
    `X-Tenant-Id` 并被采信，理由是"压测要能针对某个租户"。**路径已删除**，
    例外随之消失。

    钉住它是因为"自报被采信"这件事**没有症状**：压测脚本照常跑、指标照常好看，
    只是所有数据悄悄落到了别人的桶里（或读到了别人的桶）。
    """
    from app import main as M
    monkeypatch.setattr(M.config, "DEFAULT_TENANT", "public")
    # 空租户不会"顺手采信"任何外部值，只会落到公共租户。
    assert M._retrieval_tenant(_identity("")) == "public"
    assert M._retrieval_tenant(_identity("acme")) == "acme"


# ---------------------------------------------------------------------------
# tracing 与租户（2026-10-20 整节重写）
# ---------------------------------------------------------------------------
# 这一节原本有三条用例在测 `tracing.build_run_config(...)` 把 `rag_tenant` 写进
# `configurable` 段。那个键**没有任何读者**：官方 `Runner.run` 不认 `configurable`，
# 检索租户实际是从 `TurnRequest.tenant` 进 `TaskContext` 的。三条用例守的是一个
# 死行为，随函数一起删掉了。
#
# 现在真正要钉的是下一件事：**归一后的租户有没有被交给运行**。
# （`_retrieval_tenant` 自己的判据在上面那几条，两条一起才完整 —— 函数算对了，
# 但没接上，接口层照样漏。）
def test_chat_hands_the_normalised_tenant_to_the_run(monkeypatch):
    """`/v1/chat` 交给运行的必须是**归一后**的检索租户，不是身份里的原始值。

    为什么要这一条（2026-10-20）
    ---------------------------
    上面几条测的是 `_retrieval_tenant` **函数自己**算得对不对。但"函数算对了"
    和"这个值真的被交给了检索层"是两件事，而中间断掉过一次：端点当时把
    `ident.tenant`（可能是空串）原样塞进 `TurnRequest`，归一后的值只被塞进一个
    **没人读的运行配置键**。后果不是报错，而是**分叉** —— 空串从这条路走进检索层
    的"进程级默认值"，于是 `_retrieval_tenant` 那套 fail-closed（无租户⇒公共租户
    ⇒什么都看不到）在这条路上只剩文档上的。三条函数级用例全绿，接口层照样漏。

    判据：把 `ident.tenant` 设成空串、公共租户配成 `public`，跑一次 `/v1/chat`，
    看运行收到的 tenant 是 `public` 还是 `""`。后者说明归一被绕过了。
    """
    from fastapi.testclient import TestClient

    from app import main as M
    from app.quota import QuotaManager
    from app.ratelimit import RateLimiter
    from src.agents.runner import TaskOutcome
    from tests.auth_util import auth_headers

    monkeypatch.setattr(M.config, "DEFAULT_TENANT", "public")
    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=1000))
    monkeypatch.setattr(M, "_QUOTA", QuotaManager(daily_limit=10 ** 6, request_limit=500))
    # 身份里**没有租户**（自助注册未获批入租的那类账号）
    monkeypatch.setattr(M, "_authenticate", lambda *a, **k: M.Identity(""))

    seen: dict[str, str] = {}

    async def _capture(req, *, context, on_text=None, trace_metadata=None):
        seen["tenant"] = req.tenant
        return TaskOutcome(answer="好", attempts=1, final_agent="frontline",
                           passed=True, exit_reason="passed")

    monkeypatch.setattr(M, "run_turn", _capture)
    r = TestClient(M.app, headers=auth_headers()).post(
        "/v1/chat", json={"thread_id": "norm-1", "message": "hi"})
    assert r.status_code == 200
    assert seen["tenant"] == "public", (
        f"运行收到的租户是 {seen['tenant']!r}。空串说明 `ident.tenant` 被原样传下去了，"
        "`_retrieval_tenant` 的 fail-closed 在这条路上不生效（它只是「函数对」）。"
    )

