"""Contextual Retrieval 测试（批次 8）：**不联网、不花钱**。

这条链路是唯一显著花钱的改进项，所以"开关是否真的生效""缓存是否真的命中"
必须能在离线单测里锁死 —— 否则每次验证都要付费，还说不清到底是没开还是没调成功。
"""
import os

import pytest

from src import contextualizer as C


def _chunks(n=3, source="a.pdf"):
    return [{"source": source, "section": "", "text": f"第{i}块的内容" + "字" * 20}
            for i in range(n)]


@pytest.fixture
def enabled(monkeypatch, tmp_path):
    """打开开关并把缓存指到临时目录（避免污染仓库、也避免跨用例互相命中）。"""
    monkeypatch.setattr(C, "CTX_API_KEY", "sk-fake")
    monkeypatch.setattr(C, "CTX_CACHE_DIR", str(tmp_path / "ctx"))
    import src.retrieval as R
    monkeypatch.setattr(R, "CONTEXTUAL", True)
    return True


def test_disabled_by_default(monkeypatch):
    import src.retrieval as R
    monkeypatch.setattr(R, "CONTEXTUAL", False)
    called = []
    stats = C.apply(_chunks(), call=lambda p: called.append(p) or "前缀")
    assert called == [] and stats["prefixed"] == 0


def test_on_without_key_is_explicit(tmp_path, monkeypatch):
    import src.retrieval as R
    monkeypatch.setattr(R, "CONTEXTUAL", True)
    monkeypatch.setattr(C, "CTX_API_KEY", "")
    ok, why = C.enabled()
    assert ok is False and "AGNES_API_KEY" in why


def test_prefix_goes_to_the_front_of_text(enabled, tmp_path):
    """前缀必须进 `text` 且在最前面：检索层只认 text，存成旁路字段等于没做；
    放最前面是因为嵌入对开头更敏感。"""
    chunks = _chunks()
    stats = C.apply(chunks, call=lambda p: "这段出自 2023 年年度报告的出口章节")
    assert stats["prefixed"] == 3
    for c in chunks:
        assert c["ctx_prefix"] == "这段出自 2023 年年度报告的出口章节"
        assert c["text"].startswith(c["ctx_prefix"])
        assert c["text"].endswith("字" * 20)


def test_prompt_contains_document_and_chunk(enabled):
    seen = {}
    def fake(prompt):
        seen["p"] = prompt
        return "前缀"

    C.apply(_chunks(), call=fake)
    assert "<文档>" in seen["p"] and "<片段>" in seen["p"]
    assert "第0块的内容" in seen["p"]          # 片段正文
    assert "第1块的内容" in seen["p"]          # 文档全文（用于判断"在整篇里的位置"）


def test_cache_prevents_paying_twice(enabled):
    calls = []
    def fake(prompt):
        calls.append(prompt)
        return "前缀"

    C.apply(_chunks(), call=fake)
    assert len(calls) == 3
    again = _chunks()
    stats = C.apply(again, call=fake)
    assert len(calls) == 3, "相同内容不该再次调用模型"
    assert stats["cached"] == 3 and all(c["text"].startswith("前缀") for c in again)


def test_cache_key_includes_model(enabled, monkeypatch):
    """换模型必须重算：不同模型产出的上下文不能互相当成事实。"""
    calls = []
    C.apply(_chunks(1), call=lambda p: calls.append(p) or "甲")
    monkeypatch.setattr(C, "CONTEXTUAL_MODEL", "另一个模型")
    C.apply(_chunks(1), call=lambda p: calls.append(p) or "乙")
    assert len(calls) == 2


def test_failure_keeps_corpus_intact(enabled):
    """改进项失败绝不能破坏语料：文本原样保留，只记计数。"""
    chunks = _chunks()
    before = [c["text"] for c in chunks]
    def boom(prompt):
        raise TimeoutError("请求超时")

    stats = C.apply(chunks, call=boom)
    assert stats["failed"] == 3 and stats["prefixed"] == 0
    assert [c["text"] for c in chunks] == before
    assert all("ctx_prefix" not in c for c in chunks)


def test_empty_model_output_is_treated_as_failure(enabled):
    chunks = _chunks(1)
    stats = C.apply(chunks, call=lambda p: "   ")
    assert stats["failed"] == 1 and not chunks[0]["text"].startswith(" ")


def test_max_chunks_limit_is_respected(enabled):
    chunks = _chunks(5)
    stats = C.apply(chunks, call=lambda p: "前缀", max_chunks=2)
    assert stats["prefixed"] == 2 and stats["skipped_limit"] == 3


def test_empty_chunks_are_skipped(enabled):
    chunks = [{"source": "a.pdf", "text": ""}, {"source": "a.pdf", "text": "有内容"}]
    stats = C.apply(chunks, call=lambda p: "前缀")
    assert stats["skipped_empty"] == 1 and stats["prefixed"] == 1


def test_idempotent_when_already_prefixed(enabled):
    """重复灌库不该把前缀叠成两层。"""
    chunks = _chunks(1)
    C.apply(chunks, call=lambda p: "前缀")
    text_once = chunks[0]["text"]
    C.apply(chunks, call=lambda p: "另一个前缀")
    assert chunks[0]["text"] == text_once


# ---------------------------------------------------------------------------
# 文本收拾：模型输出不能带引号/换行/"这句话是："
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ('"这段出自报告第三章。"', "这段出自报告第三章。"),
    ("“这段讲的是出口数据。”", "这段讲的是出口数据。"),
    ("上下文：这段出自附件。", "这段出自附件。"),
    ("第一行\n第二行", "第一行 第二行"),
    ("", ""),
])
def test_clean_strips_noise(raw, expected):
    assert C._clean(raw) == expected


def test_clean_clips_to_configured_limit(monkeypatch):
    """默认按 RAG_CTX_PREFIX_MAX 截断 —— 本项目块短，前缀长了会稀释块自身的词项。"""
    monkeypatch.setattr(C, "CTX_PREFIX_MAX", 60)
    assert len(C._clean("啊" * 500)) == 60
    monkeypatch.setattr(C, "CTX_PREFIX_MAX", 25)
    assert len(C._clean("啊" * 500)) == 25
    assert len(C._clean("啊" * 500, limit=10)) == 10


def test_prefix_max_is_part_of_the_cache_key(monkeypatch, tmp_path):
    """改前缀长度必须重算：它是 prompt 的一部分，复用旧前缀会得出"假的新参数结论"。"""
    monkeypatch.setattr(C, "CTX_CACHE_DIR", str(tmp_path / "ctx"))
    monkeypatch.setattr(C, "CTX_PREFIX_MAX", 60)
    p1 = C._cache_path("a.pdf", "文本")
    monkeypatch.setattr(C, "CTX_PREFIX_MAX", 40)
    assert C._cache_path("a.pdf", "文本") != p1


def test_doc_text_is_capped_and_keeps_focus_chunk(monkeypatch):
    """超长文档只取"头部 + 命中块附近"：全文塞进去光输入 token 就够买几张图了。"""
    monkeypatch.setattr(C, "CTX_DOC_CHARS", 2000)
    chunks = [{"source": "a.pdf", "text": f"段落{i}" + "字" * 200} for i in range(40)]
    doc = C._doc_text(chunks, "a.pdf", 39)
    assert len(doc) < C.CTX_DOC_CHARS * 2
    assert "段落39" in doc            # 命中块附近必须保留
    assert "…（中间省略）…" in doc


def test_doc_text_returns_full_when_short(monkeypatch):
    monkeypatch.setattr(C, "CTX_DOC_CHARS", 20000)
    chunks = [{"source": "a.pdf", "text": "短文档"}]
    assert C._doc_text(chunks, "a.pdf", 0) == "短文档"


def test_format_stats_is_human_readable():
    s = {"total": 10, "prefixed": 7, "cached": 5, "skipped_empty": 1,
         "skipped_limit": 1, "failed": 1}
    line = C.format_stats(s)
    assert "7/10" in line and "缓存命中 5" in line


def test_parallel_workers_isolate_failures(enabled):
    """并发下"某个块失败"不能影响别的块，且前缀必须回写到正确的块上。

    为什么特意测这个：串行改并发最容易引入"结果串位"—— 17 个块里错配一个，
    从指标上根本看不出来，但那一块的检索从此指向别的内容。

    注意块号必须从 `<片段>` 段里取：文档全文里也包含所有块，
    直接全文搜索会永远命中第一块（这个坑是这条用例自己先踩出来的）。
    """
    import re

    def fake(prompt):
        frag = re.search(r"<片段>\s*(.*?)\s*</片段>", prompt, re.S).group(1)
        n = int(re.search(r"标记(\d+)", frag).group(1))
        if n == 2:
            raise RuntimeError("限流")
        return f"前缀{n}"

    chunks = [{"source": "a.pdf", "section": "", "text": f"标记{i}的内容"}
              for i in range(5)]
    stats = C.apply(chunks, call=fake, workers=3)
    assert stats["failed"] == 1 and stats["prefixed"] == 4
    for i, c in enumerate(chunks):
        if i == 2:
            assert "ctx_prefix" not in c, "失败的那块不该被写上任何东西"
            assert c["text"] == "标记2的内容"
        else:
            assert c["ctx_prefix"] == f"前缀{i}", "前缀回写串位了"
            assert c["text"].startswith(f"前缀{i}")


def test_workers_one_still_works(enabled):
    """并发度=1 时走的串行分支同样要正确（也是测试与调试时最常用的路径）。"""
    chunks = _chunks(2)
    stats = C.apply(chunks, call=lambda p: "前缀", workers=1)
    assert stats["prefixed"] == 2


def test_rate_limit_is_detected_across_wording():
    """限流判定按关键词匹配而非认异常类：换服务商时不必改代码。"""
    for msg in ("Error code: 429 - quota exceeded",
                "RateLimitError: rate limit reached",
                "Too Many Requests (HTTP 429)",
                "You've reached the API rate limit for free users"):
        assert C._is_rate_limited(msg), msg
    assert not C._is_rate_limited("ConnectionError: 连接超时")


def test_rate_limit_stops_early_instead_of_burning_quota(enabled):
    """被限流后必须止损：剩下的块直接放弃，不再逐块硬撞（实测踩过）。

    注意失败数仍要如实统计 —— 止损不等于隐藏失败。
    """
    calls = []

    def limited(prompt):
        calls.append(prompt)
        raise RuntimeError("Error code: 429 - rate limit for free users")

    chunks = _chunks(20)
    stats = C.apply(chunks, call=limited, workers=1)
    assert stats["prefixed"] == 0
    assert stats["failed"] >= 1
    assert all("ctx_prefix" not in c for c in chunks)
