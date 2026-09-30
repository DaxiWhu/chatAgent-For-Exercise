"""语料目录的结构性契约。

为什么需要这个测试
------------------
`src/rag/parse_cache.py` 的注释里写着一条**关于遍历行为的关键断言**：

> 缓存目录默认放在语料目录下的 `.parse_cache`：`glob` 的 `*` 不匹配点号开头的目录，
> 所以缓存文件不会被当成语料读进去（`.media`/`.ctx_cache` 同理）。

这条断言原本没有任何测试守着。它一旦不成立，故障形态是**静默污染**：

  遍历方式从 `glob` 换成 `os.walk`（或手写递归）之后，`<语料>/.ctx_cache/*.txt`
  会被当成正常语料读进来。而 `.ctx_cache` 存的正是"每个 chunk 的上下文摘要" ——
  于是**知识库开始吃自己的缓存**。表现是：服务照常启动、探针全绿、检索照常返回结果，
  只是语料集会随缓存增长而不断膨胀，引用里出现 `02c71b94a2e4….txt` 这种无法回溯的源名。

  没有任何指标能反映这件事，只能靠约定被验证。

所以这里不是"测 glob 库"，而是把**项目对遍历方式的依赖**钉成可执行的契约。

另外两条同源约束：
  - 语料目录不存在时 `load_chunks` 返回 `[]`（不抛异常）⇒ 服务照常起、检索恒无结果；
  - 语料为空是**静默失败**，已在 `src/graph.py` 用告警让它可见，这里再钉住"不为空"。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
KB_DIR = ROOT / "data" / "kb"

# 随仓库分发的知识库至少要有这么多片段。
# 这个数字是**防"语料又变空了"**的下限，不是对语料内容的断言 ——
# 换语料时同步调它即可，但请别调到 0：那等于把这条防线删掉。
MIN_SHIPPED_CHUNKS = 50


def _load(folder):
    from src.rag import retrieval

    return retrieval.load_chunks(str(folder))


@pytest.fixture()
def fake_corpus(tmp_path):
    """造一份带缓存目录的语料：1 份真语料 + 3 个点号缓存目录。"""
    (tmp_path / "real.md").write_text("# 真语料\n\n这是应当被加载的内容。\n",
                                      encoding="utf-8")
    for cache_name, fname, payload in (
        (".ctx_cache", "deadbeef.txt", "这是上下文摘要，不是语料。"),
        (".parse_cache", "cafebabe.json", '{"cached": true}'),
        (".vlm_cache", "0badf00d.txt", "这是图片描述缓存，不是语料。"),
    ):
        d = tmp_path / cache_name
        d.mkdir()
        (d / fname).write_text(payload, encoding="utf-8")
    # 嵌套子目录里的真语料也要能读到（递归遍历是既有契约）
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.md").write_text("# 嵌套语料\n\n子目录内容也应当被加载。\n",
                                   encoding="utf-8")
    return tmp_path


def test_dot_directories_are_never_read_as_corpus(fake_corpus):
    """点号开头的目录里的文件**不得**出现在语料里。

    这是本文件存在的首要理由。把 `load_chunks` 的
    `glob.glob(..., recursive=True)` 换成 `os.walk` 会让这条立刻变红 ——
    而那正是"知识库开始吃自己缓存"的那一步。
    """
    chunks = _load(fake_corpus)
    sources = {c.get("source") for c in chunks}

    assert sources, "连真语料都没加载到，测试前提不成立"
    leaked = {s for s in sources if any(
        s == name for name in ("deadbeef.txt", "cafebabe.json", "0badf00d.txt"))}
    assert not leaked, (
        f"缓存文件被当成语料读进来了：{sorted(leaked)}\n"
        "缓存与语料同住在 DOCS_DIR 下（.ctx_cache/.parse_cache/.media/.vlm_cache），"
        "遍历一旦不再跳过点号目录，知识库就会开始吃自己的缓存 —— "
        "服务照常绿、检索照常返回结果，语料集却会无限膨胀。"
    )


def test_nested_subdirectories_are_still_recursive(fake_corpus):
    """跳过点号目录**不能**顺手把正常子目录也跳过。

    只在最外层遍历是另一个方向的错：故障表现是"语料少了一大半"，
    而服务依然全绿 —— 加了排除规则之后必须有这条反向断言。
    """
    sources = {c.get("source") for c in _load(fake_corpus)}
    assert "nested.md" in sources, (
        f"子目录里的语料没被加载：{sorted(sources)}；"
        "排除点号目录时把 recursive 一起弄丢了"
    )


def test_missing_corpus_directory_returns_empty_without_raising(tmp_path):
    """目录不存在时返回空列表、不抛异常 —— 但也**不能**假装成功。

    返回空是刻意的（服务不该因为语料目录写错就起不来），
    代价是检索会恒无结果，所以必须能在别处被发现。
    """
    assert _load(tmp_path / "does-not-exist") == []


def test_every_chunk_carries_traceable_metadata():
    """每个片段都要能回溯：source 有值、text 有值、chunk_id 唯一。

    引用（citations）靠这些字段把用户送回原文件。缺了 source，
    "引用"就退化成一句无法核对的话。
    """
    chunks = _load(ROOT / "data")
    assert len(chunks) >= MIN_SHIPPED_CHUNKS, (
        f"随仓库分发的语料只有 {len(chunks)} 个片段（下限 {MIN_SHIPPED_CHUNKS}）。\n"
        "语料为空时服务不会报错：探针全绿、SSE 能连，只是检索恒无结果、"
        "回答里没有任何引用。若确实要缩减语料，请同步下调这个常量。"
    )

    ids = [c.get("chunk_id") for c in chunks]
    assert all(ids), "存在没有 chunk_id 的片段 —— 引用将无法定位"
    assert len(set(ids)) == len(ids), "chunk_id 出现重复 —— 引用会指错位置"

    for c in chunks:
        assert (c.get("source") or "").strip(), f"片段缺少 source：{c.get('chunk_id')}"
        assert (c.get("text") or "").strip(), f"片段正文为空：{c.get('chunk_id')}"
        assert not os.path.isabs(c["source"]), (
            f"source 落成了绝对路径：{c['source']} —— "
            "它会被下发给客户端，暴露服务端目录结构"
        )


def test_shipped_corpus_is_retrievable():
    """随仓库分发的语料必须真的能被检索命中。

    "加载得到" 和 "检索得到" 是两件事：空文本片段、全部落在同一份文件里、
    或者切分把内容切碎了，都能让语料"存在但检索不出来"。
    """
    from src.rag import retrieval

    chunks = _load(ROOT / "data")
    retriever = retrieval.build_retriever(chunks)
    hits = retriever.search("限流的判定值是怎么计算的", top_k=3)

    assert hits, "随仓库分发的知识库检索不到任何结果"
    top = hits[0][0] if isinstance(hits[0], tuple) else hits[0]
    assert "限流" in (top.get("source") or ""), (
        f"问「限流的判定值」却命中了 {top.get('source')!r} —— "
        "语料或切分口径可能被改坏了"
    )
