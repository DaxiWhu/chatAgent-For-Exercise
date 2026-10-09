"""混合检索 / RRF 融合 / 结构感知切分 / rerank 回退 的离线测试。

全部用替身，不调任何外部服务。
"""
import pytest

import src.rag.retrieval as R
from src.rag.retrieval import (
    HybridRetriever,
    RerankClient,
    build_retriever,
    rrf_fuse,
    split_by_headings,
)


def _chunk(source, text, section=""):
    return {"source": source, "section": section, "text": text}


# ---------------- RRF 融合 ----------------
def test_rrf_prefers_item_ranked_high_by_both():
    """两路都排前面的条目，融合后应当胜出。"""
    a, b, c = _chunk("s", "a"), _chunk("s", "b"), _chunk("s", "c")
    list1 = [(b, 0.9), (a, 0.8)]      # b 第一
    list2 = [(b, 0.7), (c, 0.6)]      # b 又第一
    fused = rrf_fuse([list1, list2])
    assert fused[0][0]["text"] == "b"


def test_rrf_rewards_items_found_by_both_paths():
    """只在单路出现的条目，应低于两路都出现的条目。"""
    both = _chunk("s", "both")
    only1 = _chunk("s", "only1")
    only2 = _chunk("s", "only2")
    fused = rrf_fuse([
        [(only1, 1.0), (both, 0.5)],
        [(only2, 1.0), (both, 0.5)],
    ])
    assert fused[0][0]["text"] == "both"


def test_rrf_dedupes_same_chunk():
    same = _chunk("s", "x")
    fused = rrf_fuse([[(same, 0.9)], [(same, 0.8)]])
    assert len(fused) == 1


def test_rrf_scores_are_positive_and_descending():
    fused = rrf_fuse([[( _chunk("s", "a"), 1.0), (_chunk("s", "b"), 0.5)]])
    scores = [s for _, s in fused]
    assert all(s > 0 for s in scores)
    assert scores == sorted(scores, reverse=True)


# ---------------- 结构感知切分 ----------------
MARKDOWN = """# 梧州特产

## 六堡茶
六堡茶是黑茶，有祛湿功效。

## 龟苓膏
龟苓膏可以清热解毒。

# 梧州景点

## 骑楼城
骑楼城是中国规模最大的骑楼建筑群之一。
"""


def test_split_by_headings_keeps_section_path():
    pieces = split_by_headings(MARKDOWN, chunk_size=200, chunk_overlap=20)
    sections = [p["section"] for p in pieces]
    assert "梧州特产 > 六堡茶" in sections
    assert "梧州特产 > 龟苓膏" in sections
    assert "梧州景点 > 骑楼城" in sections


def test_split_by_headings_puts_path_into_text():
    """标题路径要写进正文，否则正文没重复主题词时检索不到。"""
    pieces = split_by_headings(MARKDOWN, chunk_size=200, chunk_overlap=20)
    tea = next(p for p in pieces if p["section"].endswith("六堡茶"))
    assert tea["text"].startswith("【梧州特产 > 六堡茶】")
    assert "祛湿" in tea["text"]


def test_split_by_headings_splits_long_section():
    long_body = "梧州特产很多。" * 60
    text = f"# 标题\n\n## 长章节\n{long_body}\n"
    pieces = split_by_headings(text, chunk_size=100, chunk_overlap=20)
    assert len(pieces) > 1
    assert all(p["section"] == "标题 > 长章节" for p in pieces)
    assert all(p["text"].startswith("【标题 > 长章节】") for p in pieces)


def test_split_by_headings_handles_no_heading():
    pieces = split_by_headings("就是一段没有标题的正文。", chunk_size=100, chunk_overlap=10)
    assert pieces and pieces[0]["section"] == ""
    assert pieces[0]["text"] == "就是一段没有标题的正文。"


def test_split_by_headings_empty():
    assert split_by_headings("", chunk_size=100, chunk_overlap=10) == []
    assert split_by_headings("# 只有标题\n", chunk_size=100, chunk_overlap=10) == []


def test_chunk_strategy_recursive_has_empty_section(monkeypatch):
    monkeypatch.setattr(R, "CHUNK_STRATEGY", "recursive")
    pieces = R._split_text("一段正文。" * 50, 100, 20)
    assert pieces and all(p["section"] == "" for p in pieces)


# ---------------- HybridRetriever ----------------
class _FakeSparse:
    def __init__(self, hits):
        self.hits = hits

    def search(self, query, top_k=3):
        return self.hits[:top_k]


class _FakeDense:
    def __init__(self, hits):
        self.hits = hits

    def search(self, query, top_k=3):
        return self.hits[:top_k]


class _FakeReranker:
    def __init__(self, out=None, boom=False, exc=None):
        self.out = out
        self.boom = boom
        #: 指定要抛的异常。用来区分"上游报错"与"被熔断挡下"——
        #: 两者都回退，但**计数器标签不同**（处置方向也不同）。
        self.exc = exc
        self.calls = 0

    def rerank(self, query, chunks, top_k):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        if self.boom:
            raise RuntimeError("rerank service down")
        return self.out


def test_hybrid_merges_both_paths():
    a, b, c = _chunk("s1", "a"), _chunk("s2", "b"), _chunk("s3", "c")
    r = HybridRetriever(
        [a, b, c],
        sparse=_FakeSparse([(a, 0.9), (b, 0.5)]),
        dense=_FakeDense([(b, 0.8), (c, 0.7)]),
    )
    hits = r.search("任意", top_k=3)
    texts = [h[0]["text"] for h in hits]
    assert texts[0] == "b"          # 两路都命中 → 融合后第一
    assert set(texts) == {"a", "b", "c"}


def test_hybrid_reranker_result_used_when_ok():
    a, b = _chunk("s1", "a"), _chunk("s2", "b")
    rr = _FakeReranker(out=[(b, 0.99), (a, 0.01)])
    r = HybridRetriever([a, b],
                        sparse=_FakeSparse([(a, 0.9)]),
                        dense=_FakeDense([(a, 0.9)]),
                        reranker=rr)
    hits = r.search("任意", top_k=2)
    assert hits[0][0]["text"] == "b"
    assert r.last_rerank_ok is True


def test_hybrid_falls_back_when_reranker_fails():
    """rerank 挂掉绝不能影响检索：应回退到融合顺序。"""
    a, b = _chunk("s1", "a"), _chunk("s2", "b")
    rr = _FakeReranker(boom=True)
    r = HybridRetriever([a, b],
                        sparse=_FakeSparse([(a, 0.9), (b, 0.4)]),
                        dense=_FakeDense([(a, 0.8)]),
                        reranker=rr)
    hits = r.search("任意", top_k=2)
    assert [h[0]["text"] for h in hits][0] == "a"
    assert r.last_rerank_ok is False
    assert rr.calls == 1


def test_hybrid_falls_back_when_reranker_returns_empty():
    a = _chunk("s1", "a")
    rr = _FakeReranker(out=None)
    r = HybridRetriever([a], sparse=_FakeSparse([(a, 0.9)]),
                        dense=_FakeDense([(a, 0.9)]), reranker=rr)
    assert r.search("任意", top_k=1)
    assert r.last_rerank_ok is False


# ---------------- RerankClient ----------------
def test_rerank_client_builds_url_without_double_slash():
    c = RerankClient("https://api.siliconflow.cn/v1/", "k", "m")
    assert c.url == "https://api.siliconflow.cn/v1/rerank"


def test_rerank_client_propagates_error_for_caller_to_handle(monkeypatch):
    """client 本身不吞异常（由 HybridRetriever 负责回退），方便上层统一处理。"""
    import httpx

    def boom(*a, **kw):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "post", boom)
    c = RerankClient("https://x/v1", "k", "m")
    # 断言**具体**异常，不用 `Exception`：`RerankClient.rerank` 的 docstring
    # 承诺"失败会抛异常（httpx 网络错误 / raise_for_status 的 4xx-5xx）"，
    # 写 `Exception` 等于把这条承诺测没了 —— 将来它改成"吞掉并返回 None"，
    # 这个用例照样绿（那正是降级路径与失败路径被混淆的经典形态）。
    with pytest.raises(httpx.ConnectError):
        c.rerank("q", [_chunk("s", "t")], 1)


# ---------------- build_retriever 选择逻辑 ----------------
def test_build_retriever_hybrid_without_embedding_falls_back(monkeypatch):
    monkeypatch.setattr(R, "EMBEDDING_BACKEND", "hybrid")
    monkeypatch.setattr(R, "EMBEDDING_API_KEY", "")
    monkeypatch.setattr(R, "EMBEDDING_MODEL", "")
    chunks = [_chunk("s", "梧州特产有六堡茶。")]
    assert isinstance(build_retriever(chunks), R.BM25Retriever)


def test_build_retriever_selects_hybrid(monkeypatch):
    class _FakeEmb:
        def __init__(self, chunks):
            self.chunks = chunks

        def search(self, query, top_k=3):
            return []

    monkeypatch.setattr(R, "EMBEDDING_BACKEND", "hybrid")
    monkeypatch.setattr(R, "EMBEDDING_API_KEY", "fake-key")
    monkeypatch.setattr(R, "EMBEDDING_MODEL", "fake-model")
    monkeypatch.setattr(R, "RERANK_MODEL", "")
    monkeypatch.setattr(R, "EmbeddingRetriever", _FakeEmb)

    r = build_retriever([_chunk("s", "梧州特产有六堡茶。")])
    assert isinstance(r, HybridRetriever)
    assert r.reranker is None      # 未配 rerank 模型时不挂精排


def test_build_retriever_attaches_reranker_when_configured(monkeypatch):
    class _FakeEmb:
        def __init__(self, chunks):
            self.chunks = chunks

        def search(self, query, top_k=3):
            return []

    monkeypatch.setattr(R, "EMBEDDING_BACKEND", "hybrid")
    monkeypatch.setattr(R, "EMBEDDING_API_KEY", "fake-key")
    monkeypatch.setattr(R, "EMBEDDING_MODEL", "fake-model")
    monkeypatch.setattr(R, "RERANK_MODEL", "fake-reranker")
    monkeypatch.setattr(R, "RERANK_ENABLED", True)
    monkeypatch.setattr(R, "EmbeddingRetriever", _FakeEmb)

    r = build_retriever([_chunk("s", "梧州特产有六堡茶。")])
    assert isinstance(r.reranker, RerankClient)


def test_build_retriever_skips_reranker_when_disabled(monkeypatch):
    class _FakeEmb:
        def __init__(self, chunks):
            self.chunks = chunks

        def search(self, query, top_k=3):
            return []

    monkeypatch.setattr(R, "EMBEDDING_BACKEND", "hybrid")
    monkeypatch.setattr(R, "EMBEDDING_API_KEY", "fake-key")
    monkeypatch.setattr(R, "EMBEDDING_MODEL", "fake-model")
    monkeypatch.setattr(R, "RERANK_MODEL", "fake-reranker")
    monkeypatch.setattr(R, "RERANK_ENABLED", False)
    monkeypatch.setattr(R, "EmbeddingRetriever", _FakeEmb)

    r = build_retriever([_chunk("s", "梧州特产有六堡茶。")])
    assert r.reranker is None


# ---------------- 精排降级必须被计数 ----------------
class _CountingSink:
    """收下观测出口的调用（`src/observability` 的注入点）。"""

    def __init__(self):
        self.counters = {}

    def counter(self, name, amount=1, **labels):
        key = (name, tuple(sorted(labels.items())))
        self.counters[key] = self.counters.get(key, 0) + amount

    def histogram(self, name, value, **labels):   # pragma: no cover - 本文件不用
        pass

    def gauge(self, name, value, **labels):       # pragma: no cover - 本文件不用
        pass


@pytest.fixture()
def obs_sink():
    """注入一个可断言的观测出口；退出时**还原原来那一个**。

    ⚠️ 必须是"存下来再放回去"，**不能**用 `reset_sink()`（2026-10-06 踩过）：

    `app.main` 在导入时（conftest 的 autouse fixture）已经装了 `_MetricsSink`，
    `reset_sink()` 会把它清成 `None`，而**没有任何人**会重新装上 ⇒ 之后所有
    依赖这个出口的用例都静默收不到指标。症状是"另一个文件的一批用例红"
    （那次是 `tests/test_runtime_metrics.py` 里 11 个），与本文件毫无关系，
    单跑本文件还全绿 —— 排查方向会被完全带偏。

    `tests/test_agents_route.py` 里早就是这个写法（`saved` + 还原），这里跟它一致。
    """
    from src import observability

    saved = observability.get_sink()
    sink = _CountingSink()
    observability.set_sink(sink)
    yield sink
    observability.set_sink(saved)


def _fallback_count(sink, reason):
    return sink.counters.get(("rerank_fallback_total", (("reason", reason),)), 0)


def test_rerank_fallback_is_counted_for_every_cause(obs_sink):
    """★ 每一条精排降级路径都必须留下计数。

    ⚠️ 为什么这条不能省：此前这条路径**只有一条 WARNING 日志**，于是
    `RERANK_TIMEOUT=0`（超时被写成 0 ⇒ 请求根本发不出去）、`RERANK_MODEL` 拼错、
    上游 429 这三种**完全不同**的故障，在面板上长得一模一样 —— 都是"什么都没发生"。
    检索静默退回"不精排"，答案质量下降，而没有任何可告警的信号。

    三种成因的处置方向不同（改本机配置 / 查上游健康 / 查配额），所以标签也必须
    分开：混成一个数只能看出"在降级"，看不出该动哪里。
    """
    from src.rag.resilience import CircuitOpenError

    a, b = _chunk("s1", "a"), _chunk("s2", "b")
    args = {"sparse": _FakeSparse([(a, 0.9), (b, 0.4)]),
            "dense": _FakeDense([(a, 0.8)])}

    # ① 上游抛异常
    HybridRetriever([a, b], reranker=_FakeReranker(boom=True), **args).search("任意", top_k=2)
    assert _fallback_count(obs_sink, "error") == 1

    # ② 上游 200 但结果为空（模型名写错 / 配额耗尽 / 网关降级都会长这样）
    HybridRetriever([a, b], reranker=_FakeReranker(out=None), **args).search("任意", top_k=2)
    assert _fallback_count(obs_sink, "empty") == 1

    # ③ 被熔断挡下（本机**没有发出请求**）—— 与①②分开计数
    HybridRetriever([a, b],
                    reranker=_FakeReranker(exc=CircuitOpenError("rerank 熔断中")),
                    **args).search("任意", top_k=2)
    assert _fallback_count(obs_sink, "circuit_open") == 1

    # 精排成功时**不能**再增加降级计数（否则这个指标就失去意义）
    # 注意必须是"快照前后不变"而不是"集合为空" —— 上面三种成因的计数还在里面。
    before = {k: v for k, v in obs_sink.counters.items()
              if k[0] == "rerank_fallback_total"}
    r = HybridRetriever([a, b], reranker=_FakeReranker(out=[(b, 0.9)]), **args)
    r.search("任意", top_k=2)
    assert r.last_rerank_ok is True
    after = {k: v for k, v in obs_sink.counters.items()
             if k[0] == "rerank_fallback_total"}
    assert after == before, "精排成功却也记了一笔降级"


# ---------------- 切分参数：重叠 ≥ 块长 ----------------
def test_overlap_larger_than_chunk_size_is_clamped_not_fatal(monkeypatch, caplog):
    """★ 重叠 ≥ 块长时必须**夹取**，而不是让整个灌库失败。

    LangChain 的 `RecursiveCharacterTextSplitter` 对 `overlap >= chunk_size`
    直接抛 `ValueError: Got a larger chunk overlap ... than chunk size`，而灌库
    链路的宽 `except` 会把它记成"这份文档解析失败" ⇒ 现象是**所有文档都灌不进去**，
    日志里只有一句看不出因果的报错（报错指向切分器，真因在 `.env`）。

    关键点：这两个值**各自**都是合法整数（`RAG_CHUNK_SIZE=300`、
    `RAG_CHUNK_OVERLAP=500`），"重叠 ≥ 块长"是它们之间的**相对**约束 ——
    单看任一变量永远判不出来，所以只能在造切分器的唯一漏斗 `_make_splitter` 里夹。
    """
    import logging

    monkeypatch.setattr(R, "CHUNK_SIZE", 100)
    monkeypatch.setattr(R, "CHUNK_OVERLAP", 500)

    with caplog.at_level(logging.WARNING):
        chunks = R.split_documents([("a.md", "字" * 500)])

    assert chunks, "夹取之后必须仍然切得出块（而不是抛错中断灌库）"
    assert all(len(c["text"]) <= 100 for c in chunks), "块长上限没有生效"
    assert any("已夹到" in r.getMessage() for r in caplog.records), \
        "夹取必须留一条 WARNING —— 静默把 500 改成 99 也是'行为变了没人知道'"
