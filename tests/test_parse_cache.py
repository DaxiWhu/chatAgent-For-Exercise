"""解析结果落盘缓存：命中/失效/关闭/坏缓存降级。

关键断言用"load_chunks 被调用了几次"来验证 —— 只看返回值相等无法区分
"命中了缓存" 与 "又解析了一遍恰好结果一样"。
"""
import os

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


# ---------------------------------------------------------------------------
# 缓存键的**覆盖面**守卫：解析链路读到的环境变量一个都不能漏
# ---------------------------------------------------------------------------
#: 解析链路（`load_chunks` 会走到的模块）。加新模块时同步加进来 —— 否则它的
#: 环境变量不受本守卫保护，等于漏一个就少一个。
_PARSE_CHAIN_MODULES = (
    "loaders.py", "pdf_parser.py", "docling_loader.py", "docx_media.py",
    "image_describer.py", "contextualizer.py", "knowledge_chunk.py", "guard.py",
)

#: 刻意**不**参与指纹的键（它们是"能不能用/怎么用缓存"与"凭证"，不是产出内容）。
_NOT_IN_FINGERPRINT = {
    "RAG_PARSE_CACHE",       # 用不用缓存
    "RAG_PARSE_CACHE_DIR",   # 缓存放哪
    "AGNES_API_KEY",         # 凭证：不决定产出什么，且不该被哈希进文件名
}


def test_env_keys_cover_everything_the_parsers_read():
    """**配置改了、缓存却命中** ⇒ 返回旧配置的产物，且没有任何报错。

    这就是这份清单必须完整的全部理由。补齐之前实测漏了 23 个键
    （`RAG_VLM_DESC` / `BGE_TOKENIZER` / `CONTEXTUAL_MODEL` / `RAG_CTX_*` /
    `RAG_DOCX_OLE_TEXT_LIMIT` / `RAG_MAX_BLOCKS` …）—— 也就是说"把图片描述打开"
    "换 BGE 分词器""改上下文前缀预算"这些改动**一个都不会让缓存失效**，
    而日志还会写着"缓存命中，跳过解析"。

    判据是**推导**而不是写死清单：扫解析链路源码里所有 `os.getenv("X")` /
    `_env_*(“X”)` 的字面量，凡不在 `_ENV_KEYS` 里就报错。这样新增一个配置项时
    会被自动纳入要求（而不是等人想起来）。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "src" / "rag"
    pat = re.compile(
        r'os\.getenv\(\s*"([A-Za-z0-9_]+)"'
        r'|os\.environ\.get\(\s*"([A-Za-z0-9_]+)"'
        r'|_env_(?:str|bool|int|float)\(\s*"([A-Za-z0-9_]+)"'
    )
    read: dict[str, str] = {}
    for name in _PARSE_CHAIN_MODULES:
        path = root / name
        assert path.exists(), f"解析链路模块不存在（清单过时了）：{name}"
        for m in pat.finditer(path.read_text(encoding="utf-8")):
            key = next(g for g in m.groups() if g)
            read.setdefault(key, name)

    missing = sorted(k for k in read if k not in PC._ENV_KEYS
                     and k not in _NOT_IN_FINGERPRINT)
    assert not missing, (
        "这些环境变量会改变解析产物，却不在缓存键里 ⇒ 改了配置缓存仍命中、"
        f"功能静默不生效：{[(k, read[k]) for k in missing]}\n"
        "把它们加进 `src/rag/parse_cache.py::_ENV_KEYS`；若某个键确实不影响产出，"
        "再加进本用例的 `_NOT_IN_FINGERPRINT` 并写明理由。"
    )


def test_env_fingerprint_actually_moves_the_key(tmp_path, monkeypatch):
    """接线上的一句实证：改一个环境变量真的会让键变。

    上面那条是"清单完整"，这条是"清单里的键真的被用上了" —— 少了它，
    清单再全也可能因为拼写/大小写与 `os.getenv` 不一致而完全不参与计算。
    """
    d = _make_corpus(tmp_path)
    monkeypatch.delenv("RAG_VLM_DESC", raising=False)
    before = PC.cache_key(str(d))
    monkeypatch.setenv("RAG_VLM_DESC", "on")
    assert PC.cache_key(str(d)) != before, "改了 RAG_VLM_DESC 缓存键没变"
