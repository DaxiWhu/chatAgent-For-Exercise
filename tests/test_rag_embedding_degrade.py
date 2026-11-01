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
