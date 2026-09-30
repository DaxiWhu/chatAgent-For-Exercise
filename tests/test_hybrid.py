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
    def __init__(self, out=None, boom=False):
        self.out = out
        self.boom = boom
        self.calls = 0

    def rerank(self, query, chunks, top_k):
        self.calls += 1
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
    with pytest.raises(Exception):
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
