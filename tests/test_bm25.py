"""BM25 稀疏检索的打分公式测试（严格对齐 Apache Lucene 的 BM25Similarity）。

为什么这个文件必须存在
--------------------
`BM25Retriever` 是**排序的唯一决定者**（hybrid 里稀疏路的排名直接进 RRF），
而它的打分公式有好几个"看起来都能跑、结果却完全不同"的变体：

  - IDF 用 Lucene 的 log(1 + (N-df+0.5)/(df+0.5))，还是教科书的 log((N-df+0.5)/(df+0.5))？
  - 分子带不带 (k1+1)？（Lucene 8.0 起去掉了，更早的 Robertson 公式带着）
  - b 到底有没有真的生效？长度归一化分母是不是 k1*(1-b+b*dl/avgdl)？
  - 查询词频 qtf 参不参与加权？

这些差异**几乎不影响"能不能召回"** —— 所有分数同乘一个常数时，排序一模一样。
于是 `tests/test_retrieval.py` 里那种 `assert hits` 的断言对它们一无所知：
换个公式，端到端测试全绿，只有排名悄悄变了。**本文件的职责就是把绝对分值钉死**，
让任何一个变体都变红。

做法：把 tokenizer 降到**词级 unigram**（monkeypatch `BM25_ANALYZER` / `BM25_NGRAM_RANGE`），
这样文档长度 dl、词频 freq、文档频率 df 全都是手可数的整数，打分公式被单独隔离出来考察。
（不这么做就得手算 char_wb 2~4-gram 的几十个 n-gram 计数 —— 那不是测公式，那是测算术。）
"""
import logging
import math

import pytest

import src.rag.retrieval as R

K1, B = 1.2, 0.75          # Lucene BM25Similarity 的默认值


def _word_level(monkeypatch):
    """把分词降成"空格分词的词级 unigram"，好让 dl / freq / df 都是可手算的整数。

    注意这里改的**只是 tokenizer**，打分路径一行都没绕开 —— 本文件要测的正是后者。
    """
    monkeypatch.setattr(R, "BM25_ANALYZER", "word")
    monkeypatch.setattr(R, "BM25_NGRAM_RANGE", (1, 1))


def _lucene_score(freq, dl, avgdl, df, n_docs, k1=K1, b=B):
    """Lucene BM25Similarity 的参考实现（照 9.0.0 源码写，供断言对照）。

        idf   = log(1 + (N - df + 0.5) / (df + 0.5))
        norm  = k1 * (1 - b + b * dl / avgdl)
        score = idf * freq / (freq + norm)        <- 没有 (k1+1) 因子
    """
    idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
    norm = k1 * (1 - b + b * dl / avgdl)
    return idf * freq / (freq + norm)


# 评测语料：N=2，avgdl=(3+1)/2=2，df(苹果)=2
#   a: dl=3（苹果×2、香蕉×1）
#   b: dl=1（苹果×1）
DOCS = [
    {"source": "a", "text": "苹果 苹果 香蕉"},
    {"source": "b", "text": "苹果"},
]


def _search(monkeypatch, docs, query, **kwargs):
    """在词级分词下建检索器并查询，返回 [(chunk, score), ...]。"""
    _word_level(monkeypatch)
    return R.BM25Retriever(docs).search(query, **kwargs)


# ---------------- 绝对分值：唯一能拦住"换了公式"的断言 ----------------
def test_scores_match_lucene_bm25_exactly(monkeypatch):
    """分值与 Lucene 公式逐项对齐（参考实现 + 硬编码数值双重钉死）。

    硬编码那一组不能省：参考实现和被测实现如果被**同一个误解**改坏（比如都加回
    (k1+1)），只对参考实现的断言会一起变绿。写死的小数是最后一道防线。
    """
    got = {c["source"]: s for c, s in _search(monkeypatch, DOCS, "苹果", top_k=2)}

    assert got["a"] == pytest.approx(_lucene_score(freq=2, dl=3, avgdl=2.0, df=2, n_docs=2))
    assert got["b"] == pytest.approx(_lucene_score(freq=1, dl=1, avgdl=2.0, df=2, n_docs=2))
    # 硬编码基准（由 Lucene 公式手算得出）
    assert got["a"] == pytest.approx(0.09990222290079703, abs=1e-12)
    assert got["b"] == pytest.approx(0.10418374673940263, abs=1e-12)


def test_idf_is_lucene_smoothed_not_textbook(monkeypatch):
    """IDF 必须是 Lucene 的平滑形式，不是教科书的 log((N-df+0.5)/(df+0.5))。

    两者在 df 接近 N 时走向完全不同：教科书版会算出**负数**（本例 -1.609），
    于是"越常见的词分越低"会翻转成"越常见的词倒扣分"，且分数不再是单调的。
    """
    _word_level(monkeypatch)
    r = R.BM25Retriever(DOCS)
    col = r.vectorizer.vocabulary_["苹果"]          # df=2, N=2

    assert r.idf[col] == pytest.approx(math.log(1 + (2 - 2 + 0.5) / (2 + 0.5)))
    assert r.idf[col] > 0, "Lucene 平滑 IDF 对 df=N 的词仍是正数"


def test_k1_plus_one_factor_is_absent(monkeypatch):
    """tf 饱和后分值收敛到 idf，而不是 (k1+1)*idf —— Lucene 8.0 去掉了那个因子。

    这条最容易照抄旧教程时写错，而它**完全不改变排序**（所有分数同乘 2.2），
    除了绝对分值没有任何症状。
    """
    docs = [{"source": "a", "text": " ".join(["苹果"] * 200)}]
    hits = _search(monkeypatch, docs, "苹果", top_k=1)

    idf = math.log(1 + (1 - 1 + 0.5) / (1 + 0.5))   # N=1, df=1
    assert hits[0][1] == pytest.approx(idf, rel=0.01)
    assert hits[0][1] != pytest.approx(idf * (K1 + 1), rel=0.01)


# ---------------- 长度归一化（b 是否真的生效）----------------
def test_b_penalizes_long_documents(monkeypatch):
    """同样只命中一次，长文档得分应低于短文档 —— b 生效的直接证据。"""
    docs = [
        {"source": "short", "text": "苹果"},
        {"source": "long", "text": "苹果 " + " ".join(["填充"] * 50)},
    ]
    got = {c["source"]: s for c, s in _search(monkeypatch, docs, "苹果", top_k=2)}
    assert got["short"] > got["long"]


def test_b_zero_disables_length_normalization(monkeypatch):
    """b=0 时长度的差别必须**完全**消失（不只是变小）—— 这是"b 真的进了分母"的判据。"""
    monkeypatch.setattr(R, "BM25_B", 0.0)
    docs = [
        {"source": "short", "text": "苹果"},
        {"source": "long", "text": "苹果 填充 填充 填充"},
    ]
    got = {c["source"]: s for c, s in _search(monkeypatch, docs, "苹果", top_k=2)}
    assert got["short"] == pytest.approx(got["long"])


def test_k1_saturation_is_sublinear(monkeypatch):
    """词频翻倍不等于分数翻倍：复读同一个词刷不动分（TF 饱和）。"""
    docs = [
        {"source": "once", "text": "苹果"},
        {"source": "many", "text": " ".join(["苹果"] * 10)},
    ]
    got = {c["source"]: s for c, s in _search(monkeypatch, docs, "苹果", top_k=2)}
    assert got["many"] > got["once"], "词频更高仍然应当更相关"
    assert got["many"] < got["once"] * 10, "但绝不能是线性增长"


# ---------------- 查询侧权重 ----------------
def test_query_term_frequency_weights_the_term(monkeypatch):
    """查询里同一个词出现两次 ⇒ 该词贡献翻倍（qtf 参与加权）。"""
    _word_level(monkeypatch)
    r = R.BM25Retriever(DOCS)
    once = {c["source"]: s for c, s in r.search("苹果", top_k=2)}
    twice = {c["source"]: s for c, s in r.search("苹果 苹果", top_k=2)}
    assert twice["a"] == pytest.approx(once["a"] * 2)
    assert twice["b"] == pytest.approx(once["b"] * 2)


# ---------------- 边界与配置 ----------------
def test_empty_query_or_no_overlap_returns_nothing(monkeypatch):
    """空查询、纯空白、零词重叠都返回 []（上游据此走"没找到资料"的降级路径）。"""
    _word_level(monkeypatch)
    r = R.BM25Retriever(DOCS)
    assert r.search("") == []
    assert r.search("   ") == []
    assert r.search("西瓜") == [], "与语料零重叠的词不应得分"


def test_results_are_sorted_by_score_descending(monkeypatch):
    """必须按分降序返回 —— 上层全都假定"第一个最相关"（RRF 取排名、引用取 Top、截断取前 N）。"""
    docs = [
        {"source": "weak", "text": "苹果 香蕉 香蕉 香蕉 香蕉 香蕉 香蕉"},
        {"source": "strong", "text": "苹果"},
    ]
    hits = _search(monkeypatch, docs, "苹果", top_k=2)
    assert [c["source"] for c, _ in hits] == ["strong", "weak"]
    assert hits[0][1] > hits[1][1]


def test_top_k_limits_the_result_count(monkeypatch):
    docs = [{"source": f"s{i}", "text": "苹果"} for i in range(5)]
    assert len(_search(monkeypatch, docs, "苹果", top_k=2)) == 2


def test_zero_avgdl_defence_is_in_place(monkeypatch):
    """avgdl 必须被兜成 > 0（dl/avgdl 出 nan 会污染所有分数、让排序随机且无告警）。

    ⚠️ 说清这条测试的**强度**：它守不住"把兜底删掉"，因为正常路径上 avgdl 不可能为 0 ——
    sklearn 对空文本会先抛 empty vocabulary，这段防御实际不可达。留着它是把
    "这里有一条 nan 防线、以及它为什么不可达"固化进测试，而不是只写进注释。
    打分正确性由上面那组绝对分值用例负责。
    """
    _word_level(monkeypatch)
    r = R.BM25Retriever([{"source": "a", "text": "苹果"}])
    assert r.avgdl > 0


def test_out_of_domain_k1_b_falls_back_with_warning(monkeypatch, caplog):
    """越界的 k1/b 回退默认并告警：越界不会崩，但静默接受会让"我明明调了 b"查不出来。"""
    monkeypatch.setenv("RAG_BM25_K1", "99")         # Lucene 定义域 [0, 3]
    monkeypatch.setenv("RAG_BM25_B", "2")           # Lucene 定义域 [0, 1]
    with caplog.at_level(logging.WARNING):
        assert R._env_float("RAG_BM25_K1", 1.2, 0.0, 3.0) == 1.2
        assert R._env_float("RAG_BM25_B", 0.75, 0.0, 1.0) == 0.75
    assert sum("超出定义域" in rec.message for rec in caplog.records) == 2


def test_non_numeric_and_empty_params_fall_back(monkeypatch, caplog):
    monkeypatch.setenv("RAG_BM25_K1", "abc")
    monkeypatch.setenv("RAG_BM25_B", "   ")
    with caplog.at_level(logging.WARNING):
        assert R._env_float("RAG_BM25_K1", 1.2, 0.0, 3.0) == 1.2
    # 空串 = 没配，走默认值且**不该**告警（否则每次启动都刷一条无意义的警告）
    assert R._env_float("RAG_BM25_B", 0.75, 0.0, 1.0) == 0.75
    assert any("不是数字" in rec.message for rec in caplog.records)


def test_boundary_values_are_accepted(monkeypatch):
    """定义域是闭区间：k1=0、k1=3、b=0、b=1 都应当被接受，而不是被当成越界。"""
    for name, val, lo, hi, default, expect in [
        ("RAG_BM25_K1", "0", 0.0, 3.0, 1.2, 0.0),
        ("RAG_BM25_K1", "3", 0.0, 3.0, 1.2, 3.0),
        ("RAG_BM25_B", "0", 0.0, 1.0, 0.75, 0.0),
        ("RAG_BM25_B", "1", 0.0, 1.0, 0.75, 1.0),
    ]:
        monkeypatch.setenv(name, val)
        assert R._env_float(name, default, lo, hi) == expect
