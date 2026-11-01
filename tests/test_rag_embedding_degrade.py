"""embedding 用不了时，检索必须降级到关键词，而不是整条挂掉。

**全部用替身，不联网、不调上游、不读真实语料。**

背景（2026-11-01 真实链路实测）：embedding 网关返回 ``402``（余额不足）时，
``build_retriever`` 直接抛异常 —— 因为 ``embedding_configured()`` 只判
"key/model 填没填"，不看能不能通。后果是 **"embedding 欠费" = "知识库不可用"**，
而同一批语料上的 BM25 明明是好的（实测 Top1 61%）。

更隐蔽的一层在 agent 侧：``kb_search`` 把异常转成"检索失败"之后，模型会换措辞
再试，每次重试又白撞一次上游 —— 一次运行就这样耗掉 23 步 / 31341 token 而产出 0 字。
所以"降级"和"别每次都重撞"是同一件事的两半，本文件两条都守。

判据分三层：
1. **构造期**：稠密路建不起来 → 返回 ``NullRetriever``（不抛），并点亮冷却；
2. **检索期**：单路抛异常 → 用另一路（不抛），两路都失败才抛；
3. **口径**：降级必须发指标（否则"检索质量悄悄下降"没有任何告警信号）。
"""

import pytest

import src.rag.pool as P
import src.rag.retrieval as R
from src import observability


def _chunk(source, text, section=""):
    return {"source": source, "section": section, "text": text}


def _chunks():
    return [
        _chunk("04-限流与配额.md", "限流使用加权滑动窗口，判定值 = 上一窗口计数 × 剩余比例"),
        _chunk("02-部署与配置.md", "部署使用 docker compose，配置文件在 deploy 目录下"),
    ]


class _BoomEmbedding:
    """构造即抛：模拟余额不足 / 配额耗尽 / 网关熔断时的 ``EmbeddingRetriever``。"""

    calls = 0

    def __init__(self, chunks):
        type(self).calls += 1
        raise RuntimeError("Error code: 402 - insufficient balance")


class _Side:
    """一路检索的替身：要么返回给定命中，要么抛给定异常。"""

    def __init__(self, hits=None, exc=None):
        self.hits = hits if hits is not None else []
        self.exc = exc
        self.calls = 0

    def search(self, query, top_k=3):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.hits[:top_k]


class _Sink:
    """最小指标出口：只记 counter 的名字与标签。"""

    def __init__(self):
        self.rows = []

    def counter(self, name, amount=1, **labels):
        self.rows.append((name, labels))

    def histogram(self, name, value, **labels):
        pass

    def gauge(self, name, value, **labels):
        pass

    def stages(self, name):
        return [lab.get("stage") for n, lab in self.rows if n == name]


@pytest.fixture
def sink():
    """接管指标出口。

    用 ``set_sink`` 而不是 patch ``get_sink``：``observability.counter`` 走的是
    模块级 ``_sink``（经 ``_dispatch``），patch 取数函数对它没有任何影响 —— 第一版
    就是这么写的，症状是"降级明明发生了，指标却一条没有"，而代码本身是对的。
    还原由 ``tests/conftest.py`` 的 autouse 守卫负责。
    """
    s = _Sink()
    observability.set_sink(s)
    return s


@pytest.fixture
def boom(monkeypatch):
    """让稠密后端构造必失败，并返回"被尝试了几次"的计数器。

    ⚠️ 必须同时把 ``embedding_configured`` 固定成 True：测试进程里
    ``EMBEDDING_API_KEY`` 往往是空的（conftest 会擦环境），那样 ``build_retriever``
    会走"**没配**"那条分支直接回退 BM25 —— 看起来也是绿的，但测的根本不是
    本文件要说的那件事：**配了、但运行时用不了**。这两条分支的处置不同，
    混淆了就等于没测。
    """
    _BoomEmbedding.calls = 0
    monkeypatch.setattr(R, "VECTOR_STORE", "memory")
    monkeypatch.setattr(R, "EmbeddingRetriever", _BoomEmbedding)
    monkeypatch.setattr(R, "embedding_configured", lambda: True)
    return _BoomEmbedding


# ---------------- 构造期：失败要降级，不能抛 ----------------
def test_dense_failure_returns_null_instead_of_raising(boom):
    """★ 核心判据：构造失败**不能**冒到调用方。

    冒出去的后果不是"这个请求报错"，而是 ``kb_search`` 收到异常后返回
    "检索失败（知识库暂时不可用）" —— 用户会以为知识库没了。
    """
    dense = R._make_dense_retriever(_chunks())

    assert isinstance(dense, R.NullRetriever)
    assert boom.calls == 1, "应该真试过一次才降级"


def test_dense_failure_opens_the_cooldown(boom):
    """失败之后 `embedding_down()` 必须亮起来，否则下一轮还会重撞。"""
    assert R.embedding_down() is False

    R._make_dense_retriever(_chunks())

    assert R.embedding_down() is True


def test_cooldown_stops_the_retry_storm(boom):
    """★ 冷却期内不再尝试上游。

    这条挡的是 agent 侧的"重试风暴"：模型每次换措辞重试工具，
    如果每次都重新构造稠密路，就是每次都白等一个上游往返。
    """
    R._make_dense_retriever(_chunks())
    assert boom.calls == 1

    for _ in range(3):
        dense = R._make_dense_retriever(_chunks())
        assert isinstance(dense, R.NullRetriever)

    assert boom.calls == 1, "冷却期内一次都不该再试"


def test_zero_cooldown_keeps_retrying_every_time(monkeypatch, boom):
    """``RAG_EMBED_RETRY_COOLDOWN=0`` = 关闭冷却，每次照试。

    留着这个开关是为了"上游刚恢复、想立刻重试"的场景；
    断言它真的生效，否则这个配置项等于装饰品。
    """
    monkeypatch.setattr(R, "RAG_EMBED_RETRY_COOLDOWN", 0)

    for _ in range(3):
        R._make_dense_retriever(_chunks())

    assert boom.calls == 3
    assert R.embedding_down() is False, "0 = 不进冷却"


def test_reset_clears_the_cooldown(boom):
    """充值/修好之后要能立刻恢复，不用重启进程。"""
    R._make_dense_retriever(_chunks())
    assert R.embedding_down() is True

    R.reset_embedding_cooldown()

    assert R.embedding_down() is False
    R._make_dense_retriever(_chunks())
    assert boom.calls == 2, "复位后应该重新尝试"


# ---------------- 选路：降级后仍然拿得到东西 ----------------
def test_hybrid_still_works_without_dense(monkeypatch, boom, sink):
    """★ hybrid 在稠密路不可用时**仍然返回 HybridRetriever 且能检索出结果**。

    刻意不退成纯 ``BM25Retriever``：类型一变，外部按 `last_rerank_ok` 之类
    字段观察的地方就要跟着改，而这只是临时降级。
    """
    monkeypatch.setattr(R, "EMBEDDING_BACKEND", "hybrid")
    monkeypatch.setattr(R, "RERANK_ENABLED", False)

    r = R.build_retriever(_chunks())

    assert isinstance(r, R.HybridRetriever)
    hits = r.search("限流的判定值怎么算", top_k=2)
    assert hits, "稀疏路（真 BM25，不是替身）必须给出结果"
    assert hits[0][0]["source"] == "04-限流与配额.md"
    assert "hybrid_sparse_only" in sink.stages("rag_retrieval_degraded_total")


def test_semantic_falls_back_to_bm25(monkeypatch, boom, sink):
    """单路 semantic 没有"另一路"可兜 —— 退成关键词检索，别返回空。

    返回空的表现是"知识库里没有这个内容"，而实际是"embedding 欠费了"，
    这两件事对用户和对运维的含义完全不同。
    """
    monkeypatch.setattr(R, "EMBEDDING_BACKEND", "semantic")

    r = R.build_retriever(_chunks())

    assert isinstance(r, R.BM25Retriever)
    assert r.search("限流", top_k=2)
    assert "semantic_to_bm25" in sink.stages("rag_retrieval_degraded_total")


# ---------------- 检索期：单路失败不扩散 ----------------
def test_search_survives_one_side_failing(sink):
    """稠密路在**检索时**抛（构造成功但第一次查询就挂），也要能出结果。"""
    good = [(_chunks()[0], 0.9)]
    r = R.HybridRetriever(
        _chunks(), sparse=_Side(hits=good), dense=_Side(exc=RuntimeError("402"))
    )

    hits = r.search("限流")

    assert hits == good
    assert "hybrid_dense_failed" in sink.stages("rag_retrieval_degraded_total")


def test_sparse_side_failure_uses_dense(sink):
    """反方向也要成立：稀疏路抛，稠密路照样交付。"""
    good = [(_chunks()[1], 0.8)]
    r = R.HybridRetriever(
        _chunks(), sparse=_Side(exc=RuntimeError("empty vocabulary")),
        dense=_Side(hits=good),
    )

    assert r.search("部署") == good


def test_search_raises_when_both_sides_fail():
    """两路都失败 = 确实拿不到任何东西，让它诚实地失败。

    不要在这里伪造空结果 —— 那会让模型以为"知识库里没有"，
    然后拿自己的记忆编一段没有出处的结论。
    """
    r = R.HybridRetriever(
        _chunks(), sparse=_Side(exc=RuntimeError("a")), dense=_Side(exc=RuntimeError("b"))
    )

    with pytest.raises(RuntimeError):
        r.search("anything")


def test_dense_search_failure_opens_cooldown(sink):
    """检索期失败也要点亮冷却 —— 否则下一轮 `build_retriever` 还会重建它。"""
    r = R.HybridRetriever(
        _chunks(), sparse=_Side(hits=[]), dense=_Side(exc=RuntimeError("402"))
    )

    assert R.embedding_down() is False
    r.search("q")
    assert R.embedding_down() is True
    assert "embedding_down" in sink.stages("rag_retrieval_degraded_total")


def test_null_dense_does_not_count_as_failure(sink):
    """``NullRetriever`` 返回空不是"失败"，不该被记成降级。

    否则冷却期内的每一次检索都会再记一条降级指标，
    `rate(rag_retrieval_degraded_total)` 会以为"一直在坏"。
    """
    r = R.HybridRetriever(
        _chunks(), sparse=_Side(hits=[(_chunks()[0], 0.5)]), dense=R.NullRetriever()
    )

    assert r.search("限流")
    assert sink.stages("rag_retrieval_degraded_total") == []


# ---------------- "库挂了" 与 "embedding 挂了" 不能混 ----------------
def _fault(module, msg="boom"):
    """造一个"看起来来自某个模块"的异常。

    为什么要伪造模块名而不是真去构造 `openai.APIStatusError`：它需要
    message/response/body 一整套参数，而 `_is_embedding_fault` 的判据**只有**
    "异常来自哪个模块"这一条。真构造只会把测试绑死在 SDK 的构造签名上。
    """
    cls = type("Boom", (RuntimeError,), {})
    cls.__module__ = module
    return cls(msg)


class _OkDense:
    """能用的内存稠密后端替身：只为证明"回退继续往下走"。"""

    def __init__(self, chunks):
        self.chunks = chunks

    def search(self, query, top_k=3):
        return [(_chunks()[0], 0.5)][:top_k]


def test_embedding_fault_from_qdrant_stops_the_retries(monkeypatch):
    """★ Qdrant 构造失败若来自 embedding，后面两个分支不许再各撞一次。

    2026-11-01 实测：一次 402 在 hybrid 路径上被撞了**三次**
    （Qdrant 分支 → `_make_dense_retriever` 的 qdrant 分支 → 内存后端），
    首次构建 12.9 秒。接上 Qdrant 之后这条路径才真正跑得到，之前测不出来。
    """
    attempts = []

    class _QdrantBoom:
        def __init__(self, *a, **k):
            attempts.append("qdrant")
            raise _fault("openai", "Error code: 402 - insufficient balance")

    _BoomEmbedding.calls = 0
    monkeypatch.setattr(R, "EMBEDDING_BACKEND", "hybrid")
    monkeypatch.setattr(R, "VECTOR_STORE", "qdrant")
    monkeypatch.setattr(R, "embedding_configured", lambda: True)
    monkeypatch.setattr(R, "QdrantRetriever", _QdrantBoom)
    monkeypatch.setattr(R, "EmbeddingRetriever", _BoomEmbedding)

    r = R.build_retriever(_chunks())

    assert isinstance(r, R.HybridRetriever)
    assert r.search("限流"), "稠密路挂了，稀疏路必须照常出货"
    assert len(attempts) == 1, f"Qdrant 被撞了 {len(attempts)} 次"
    assert _BoomEmbedding.calls == 0, "已经确认 embedding 用不了，不该再试内存后端"
    assert R.embedding_down() is True


def test_vector_store_fault_does_not_open_the_cooldown(monkeypatch):
    """反方向：**库连不上**不等于 embedding 挂了，白冷却 60 秒会连累别人。

    这条防的是"判据写成 `except Exception` 就顺手冷却"——
    症状是 Qdrant 抖一下，整个服务 60 秒内所有检索都失去向量那一路，且零报错。
    """
    monkeypatch.setattr(R, "EMBEDDING_BACKEND", "semantic")
    monkeypatch.setattr(R, "VECTOR_STORE", "qdrant")
    monkeypatch.setattr(R, "embedding_configured", lambda: True)
    monkeypatch.setattr(
        R, "QdrantRetriever",
        type("_QdrantDown", (), {
            "__init__": lambda self, *a, **k: (_ for _ in ()).throw(
                _fault("qdrant_client.http.exceptions", "Connection refused"))
        }),
    )
    monkeypatch.setattr(R, "EmbeddingRetriever", _OkDense)

    dense = R._make_dense_retriever(_chunks())

    assert R.embedding_down() is False
    assert isinstance(dense, _OkDense), "库挂了要回退到内存后端，不能直接判稠密路不可用"


def test_error_module_root_is_the_only_judgement():
    """判据只看模块根，不看 message 文案（文案会随网关变，不能当契约）。"""
    assert R._is_embedding_fault(_fault("openai", "anything")) is True
    assert R._is_embedding_fault(_fault("openai._exceptions", "anything")) is True
    assert R._is_embedding_fault(_fault("qdrant_client.http.exceptions", "402")) is False
    assert R._is_embedding_fault(RuntimeError("Error code: 402")) is False

def test_semantic_qdrant_embedding_fault_also_stops(monkeypatch):
    """单路 semantic 同理：Qdrant 那一步撞在 embedding 上，就别退到内存后端再撞一次。"""
    _BoomEmbedding.calls = 0
    monkeypatch.setattr(R, "EMBEDDING_BACKEND", "semantic")
    monkeypatch.setattr(R, "VECTOR_STORE", "qdrant")
    monkeypatch.setattr(R, "embedding_configured", lambda: True)
    monkeypatch.setattr(
        R, "QdrantRetriever",
        type("_Qdrant402", (), {
            "__init__": lambda self, *a, **k: (_ for _ in ()).throw(
                _fault("openai", "Error code: 402"))
        }),
    )
    monkeypatch.setattr(R, "EmbeddingRetriever", _BoomEmbedding)

    r = R.build_retriever(_chunks())

    assert isinstance(r, R.BM25Retriever), "单路稠密挂了要退成关键词检索"
    assert _BoomEmbedding.calls == 0
    assert R.embedding_down() is True


# ---------------- 缓存会把降级状态一起钉住（pool 侧）----------------
@pytest.fixture
def pool_state(monkeypatch):
    """清空 `src.rag.pool` 的模块级缓存，并让"构建"与"是否冷却中"可控。

    ⚠️ patch 的目标必须是函数真正读状态的那个模块（`src.rag.pool` 的 `build_retriever`），
    理由见 `tests/test_tenant_isolation.py::isolated_retriever_state` 的说明。
    """
    monkeypatch.setattr(P, "_RETRIEVER", None)
    monkeypatch.setattr(P, "_CHUNKS", _chunks())
    monkeypatch.setattr(P, "_TENANT_RETRIEVERS", {})
    monkeypatch.setattr(P, "_DEGRADED", set())
    state = {"down": True, "built": 0}
    monkeypatch.setattr(R, "embedding_down", lambda: state["down"])

    def fake_build(chunks, tenant=None):
        state["built"] += 1
        return {"tenant": tenant, "n": state["built"]}

    monkeypatch.setattr(P, "build_retriever", fake_build)
    return state


def test_degraded_retriever_is_revived_after_the_cooldown(pool_state):
    """★ 冷却过期后必须重建 —— 否则这个进程**永远**只用关键词检索。

    检索器是缓存单例，而 `reset_retrievers` 在生产代码里没有任何调用点。
    不重建的后果是"充值了、日志也不报错了，检索质量却没回来"，
    且没有任何报错可查。
    """
    first = P.get_retriever("acme")
    assert pool_state["built"] == 1
    assert "acme" in P._DEGRADED, "降级时建出来的这一份必须被记下来"

    assert P.get_retriever("acme") is first, "冷却期内必须继续用缓存"
    assert pool_state["built"] == 1, "冷却期内重建 = 把'每次请求白撞上游'搬回来"

    pool_state["down"] = False
    revived = P.get_retriever("acme")
    assert revived is not first, "冷却过期后没有重建"
    assert pool_state["built"] == 2
    assert "acme" not in P._DEGRADED

    assert P.get_retriever("acme") is revived, "恢复之后不该再重建"
    assert pool_state["built"] == 2


def test_no_tenant_path_also_revives(pool_state):
    """无租户语境（CLI/脚本走的那条）用的是另一段分支，同样必须能自愈。"""
    first = P.get_retriever()
    assert pool_state["built"] == 1

    pool_state["down"] = False
    assert P.get_retriever() is not first
    assert pool_state["built"] == 2


def test_healthy_build_is_not_marked_degraded(pool_state):
    """反方向：正常建出来的不该被标记，否则每次读缓存都白重建一次索引。"""
    pool_state["down"] = False
    r = P.get_retriever("acme")

    assert not P._DEGRADED
    assert P.get_retriever("acme") is r
    assert pool_state["built"] == 1
