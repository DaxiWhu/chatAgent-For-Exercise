"""解析结果落盘缓存：命中/失效/关闭/坏缓存降级。

关键断言用"load_chunks 被调用了几次"来验证 —— 只看返回值相等无法区分
"命中了缓存" 与 "又解析了一遍恰好结果一样"。
"""
import os

import pytest

import src.rag.parse_cache as PC


def _make_corpus(tmp_path):
    d = tmp_path / "corpus"
    d.mkdir()
    (d / "a.md").write_text("# 梧州\n\n六堡茶是黑茶。", encoding="utf-8")
    return d


def test_second_call_hits_cache_without_reparsing(tmp_path, monkeypatch):
    d = _make_corpus(tmp_path)
    calls = {"n": 0}
    real = PC.load_chunks

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(PC, "load_chunks", counting)

    first = PC.load_chunks_cached(str(d), enabled=True, cache_dir=str(tmp_path / "cache"))
    assert calls["n"] == 1 and first

    second = PC.load_chunks_cached(str(d), enabled=True, cache_dir=str(tmp_path / "cache"))
    assert calls["n"] == 1, "第二次应直接命中缓存，不应再解析"
    assert second == first


def test_cache_invalidated_when_file_changes(tmp_path, monkeypatch):
    d = _make_corpus(tmp_path)
    calls = {"n": 0}
    real = PC.load_chunks

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(PC, "load_chunks", counting)
    cache_dir = str(tmp_path / "cache")

    PC.load_chunks_cached(str(d), enabled=True, cache_dir=cache_dir)
    (d / "a.md").write_text("# 梧州\n\n六堡茶是黑茶。\n\n新增一段内容。", encoding="utf-8")
    PC.load_chunks_cached(str(d), enabled=True, cache_dir=cache_dir)

    assert calls["n"] == 2, "文件内容变了必须重算"


def test_cache_disabled_always_parses(tmp_path, monkeypatch):
    d = _make_corpus(tmp_path)
    calls = {"n": 0}
    real = PC.load_chunks

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(PC, "load_chunks", counting)
    PC.load_chunks_cached(str(d), enabled=False)
    PC.load_chunks_cached(str(d), enabled=False)
    assert calls["n"] == 2


def test_corrupt_cache_falls_back_to_parse(tmp_path):
    d = _make_corpus(tmp_path)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    key = PC.cache_key(str(d))
    (cache_dir / f"{key}.json").write_text("{ this is not json", encoding="utf-8")

    chunks = PC.load_chunks_cached(str(d), enabled=True, cache_dir=str(cache_dir))
    assert chunks, "坏缓存不能让灌库失败"


def test_cache_key_changes_with_chunk_params(tmp_path):
    d = _make_corpus(tmp_path)
    assert PC.cache_key(str(d), chunk_size=300) != PC.cache_key(str(d), chunk_size=500)


def test_cache_dir_defaults_under_corpus(tmp_path, monkeypatch):
    monkeypatch.delenv("RAG_PARSE_CACHE_DIR", raising=False)
    d = _make_corpus(tmp_path)
    assert PC.cache_dir_for(str(d)) == os.path.join(str(d), ".parse_cache")


def test_clear_removes_cache_files(tmp_path):
    d = _make_corpus(tmp_path)
    cache_dir = tmp_path / "cache"
    PC.load_chunks_cached(str(d), enabled=True, cache_dir=str(cache_dir))
    assert PC.clear(str(d), cache_dir=str(cache_dir)) == 1
    assert PC.clear(str(d), cache_dir=str(cache_dir)) == 0
