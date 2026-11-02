"""记忆的向量检索：什么时候查、查到什么、查挂了怎么办。

这一组守的核心是一句话：**检索是优化，不是功能**。挑不出几条就退回"全都给"，
所以下面有相当一部分断言是在验证"失败时有没有变成少给东西"。
"""

from __future__ import annotations

import asyncio

import pytest

from src.agents import memory, memory_index
from src.agents.memory_store import InMemoryMemoryStore


class _FakeIndex:
    """记录调用、按剧本返回命中。"""

    def __init__(self, hits=None, *, fail=False):
        self.hits = list(hits or [])
        self.fail = fail
        self.searches: list[tuple[str, str]] = []
        self.puts: list[tuple[str, list[dict]]] = []

    def put(self, owner_key, items):
        self.puts.append((owner_key, list(items)))
        return len(items)

    def search(self, owner_key, query, top_k=20):
        self.searches.append((owner_key, query))
        if self.fail:
            raise RuntimeError("向量库连不上")
        return list(self.hits)


@pytest.fixture
def store(monkeypatch):
    s = InMemoryMemoryStore()
    monkeypatch.setattr(memory, "_store", lambda: s)
    monkeypatch.setenv("AGENT_MEMORY", "1")
    return s


@pytest.fixture
def index(monkeypatch):
    def _wire(fake):
        monkeypatch.setattr(memory, "_index", lambda: fake)
        return fake

    return _wire


def _input(items, instructions=""):
    from agents.run_config import ModelInputData

    return ModelInputData(input=list(items), instructions=instructions)


def _context(owner="u1"):
    from src.agents.context import TaskContext

    ctx = TaskContext(thread_id="t-1")
    ctx.owner_key = owner
    return ctx


def _many(store, n, *, kind="project"):
    ids = []
    for i in range(n):
        ids.append(store.remember("u1", f"事实 {i}", kind=kind)["id"])
    return ids


# --------------------------------------------------------------------------- #
# ① 什么时候查
# --------------------------------------------------------------------------- #
def test_below_the_threshold_never_touches_the_index(store, index):
    """★ 条数不多时全量注入，**一次检索都不发**。

    全量更准（不存在召回失败），而且前缀稳定、上游缓存能命中。检索的代价不是
    "多一次查询"，是"每轮前缀都变"。
    """
    fake = index(_FakeIndex())
    _many(store, 5)

    asyncio.run(memory.inject_memories(_input([{"role": "user", "content": "问一句"}]),
                                       context=_context()))

    assert fake.searches == [], "没超过阈值就不该走检索"


def test_above_the_threshold_retrieves(store, index, monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_RETRIEVE_ABOVE", "10")
    hit = store.remember("u1", "这条会被召回", kind="preference")["id"]
    _many(store, 30)
    fake = index(_FakeIndex(hits=[(hit, 0.91)]))

    out = asyncio.run(memory.inject_memories(
        _input([{"role": "user", "content": "日期怎么格式化"}]), context=_context()))

    assert len(fake.searches) == 1
    assert fake.searches[0][1] == "日期怎么格式化", "查询文本应当是本轮用户发言"
    assert "这条会被召回" in out.input[0]["content"]


# --------------------------------------------------------------------------- #
# ② 查到什么：命中串只取有效的那几条
# --------------------------------------------------------------------------- #
def test_hits_pointing_at_dead_rows_are_skipped(store, index, monkeypatch):
    """★★ 向量表是**脏读**的：改口 / 淘汰 / 整批回滚都不会去删向量行。

    命中一个已经不存在的 id 时必须静静跳过 —— 而不是拿它去查主表、查空、
    或者更糟：把一条已失效的内容注入进去。
    """
    monkeypatch.setenv("AGENT_MEMORY_RETRIEVE_ABOVE", "5")
    alive = store.remember("u1", "仍然有效", kind="preference")["id"]
    _many(store, 20)
    index(_FakeIndex(hits=[("已经不存在的-id", 0.99), (alive, 0.80)]))

    out = asyncio.run(memory.inject_memories(
        _input([{"role": "user", "content": "x"}]), context=_context()))
    body = out.input[0]["content"]

    assert "仍然有效" in body
    assert "已经不存在的-id" not in body


def test_pinned_survives_a_retrieval_round(store, index, monkeypatch):
    """★★ pinned 是人显式钉的，相似度回答不了"重不重要"。

    让检索决定 pinned 的去留，等于把"钉住"这个动作悄悄废掉 ——
    而它失效的时候没有任何症状，只是"我钉的那条好像没生效"。
    """
    monkeypatch.setenv("AGENT_MEMORY_RETRIEVE_ABOVE", "5")
    store.remember("u1", "这条被钉住了", kind="reference", pinned=True)
    hit = store.remember("u1", "检索命中这条", kind="project")["id"]
    _many(store, 20)
    index(_FakeIndex(hits=[(hit, 0.7)]))

    out = asyncio.run(memory.inject_memories(
        _input([{"role": "user", "content": "x"}]), context=_context()))
    body = out.input[0]["content"]

    assert "这条被钉住了" in body
    assert "检索命中这条" in body


def test_retrieval_failure_falls_back_to_full_injection(store, index, monkeypatch):
    """★★ 检索挂了不能变成"少给记忆"：退回全量。"""
    monkeypatch.setenv("AGENT_MEMORY_RETRIEVE_ABOVE", "5")
    _many(store, 12)
    index(_FakeIndex(fail=True))

    out = asyncio.run(memory.inject_memories(
        _input([{"role": "user", "content": "x"}]), context=_context()))

    assert out.input[0]["role"] == "system"
    assert "事实 0" in out.input[0]["content"], "检索失败应当退回全量"


def test_empty_hits_also_falls_back(store, index, monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_RETRIEVE_ABOVE", "5")
    _many(store, 12)
    index(_FakeIndex(hits=[]))

    out = asyncio.run(memory.inject_memories(
        _input([{"role": "user", "content": "x"}]), context=_context()))

    assert "事实 0" in out.input[0]["content"]


def test_no_user_turn_means_no_retrieval(store, index, monkeypatch):
    """工具循环里的续跑没有新的用户发言 ⇒ 没有查询文本 ⇒ 全量（不能瞎编一个 query）。"""
    monkeypatch.setenv("AGENT_MEMORY_RETRIEVE_ABOVE", "5")
    _many(store, 12)
    fake = index(_FakeIndex(hits=[]))

    asyncio.run(memory.inject_memories(
        _input([{"role": "assistant", "content": "继续"}]), context=_context()))

    assert fake.searches == []


# --------------------------------------------------------------------------- #
# ③ 写入时同步向量
# --------------------------------------------------------------------------- #
def test_a_new_memory_gets_a_vector(store, index):
    fake = index(_FakeIndex())

    memory.remember_memory(owner_key="u1", content="日期用 YYYY-MM-DD",
                           kind="preference")

    assert len(fake.puts) == 1
    owner, items = fake.puts[0]
    assert owner == "u1"
    assert items[0]["content"] == "日期用 YYYY-MM-DD"
    assert items[0]["kind"] == "preference"


def test_refreshing_the_same_memory_does_not_re_embed(store, index):
    """★ 同一句话再说一遍 ⇒ ``refreshed``，内容没变，重算向量是白花一次调用。"""
    fake = index(_FakeIndex())

    memory.remember_memory(owner_key="u1", content="日期用 YYYY-MM-DD")
    memory.remember_memory(owner_key="u1", content="日期用 YYYY-MM-DD")

    assert len(fake.puts) == 1


def test_vector_failure_does_not_fail_the_write(store, index):
    """★★ 主表才是事实来源。向量写失败只是"检索少一条"，不是"记忆没记上"。"""

    class _Broken(_FakeIndex):
        def put(self, owner_key, items):
            raise RuntimeError("embedding 网关 502")

    index(_Broken())

    result = memory.remember_memory(owner_key="u1", content="照样要记下来")

    assert result["ok"] is True
    assert [r["content"] for r in store.active("u1")] == ["照样要记下来"]


# --------------------------------------------------------------------------- #
# ④ 索引本身：隔离与查询前缀
# --------------------------------------------------------------------------- #
class _FakeStore:
    def __init__(self, owner):
        self.owner = owner
        self.rows = []
        self.dim = 0
        self.search_args = None

    def ensure_schema(self, dim):
        self.dim = dim

    def upsert(self, rows):
        self.rows.extend(rows)
        return len(rows)

    def search(self, vector, top_k=3):
        self.search_args = (vector, top_k)
        return [({"id": "m1"}, 0.93)]

    def delete_tenant(self):
        return len(self.rows)


class _FakeEmbedder:
    def __init__(self):
        self.queries = []

    def embed_documents(self, texts):
        return [[0.1, 0.2] for _ in texts]

    def embed_query(self, text):
        self.queries.append(text)
        return [0.1, 0.2]


def test_search_is_scoped_to_the_owner(monkeypatch):
    """★★ 隔离靠"按 owner 造一个 store"，不是靠查询里加条件。

    这个区别很实在：查询里拼条件，漏一次就把别人的记忆召回给这个人；
    而 owner 是构造参数，漏了就连 store 都造不出来。
    """
    made = []

    def _factory(owner):
        s = _FakeStore(owner)
        made.append(s)
        return s

    idx = memory_index.MemoryIndex("postgresql://x/y", "t",
                                   store_factory=_factory, embedder=_FakeEmbedder())
    monkeypatch.setattr(memory_index, "_ENSURED", set())

    changed = idx.search("alice", "日期格式", top_k=7)

    assert changed == [("m1", pytest.approx(0.93))]
    assert [s.owner for s in made] == ["alice"], "store 必须按 owner 造"


def test_put_writes_the_owner_and_the_id(monkeypatch):
    made = []

    def _factory(owner):
        s = _FakeStore(owner)
        made.append(s)
        return s

    idx = memory_index.MemoryIndex("postgresql://x/y", "t",
                                   store_factory=_factory, embedder=_FakeEmbedder())
    monkeypatch.setattr(memory_index, "_ENSURED", set())

    n = idx.put("bob", [{"id": "m-9", "content": "用中文", "kind": "preference"}])

    assert n == 1
    assert any(s.rows for s in made if s.owner == "bob")
    row = next(r for s in made if s.owner == "bob" for r in s.rows)
    assert row[0] == "m-9"
    assert row[1]["id"] == "m-9", "chunk 里必须带 id，否则检索回来无法回主表"


def test_a_dead_store_returns_empty_instead_of_raising(monkeypatch):
    class _Broken(_FakeStore):
        def search(self, vector, top_k=3):
            raise RuntimeError("向量库挂了")

    idx = memory_index.MemoryIndex("postgresql://x/y", "t",
                                   store_factory=lambda o: _Broken(o),
                                   embedder=_FakeEmbedder())
    monkeypatch.setattr(memory_index, "_ENSURED", set())

    assert idx.search("alice", "随便问") == []


def test_embedding_failure_returns_empty(monkeypatch):
    class _BadEmbed:
        def embed_query(self, text):
            raise RuntimeError("网关超时")

    idx = memory_index.MemoryIndex("postgresql://x/y", "t",
                                   store_factory=lambda o: _FakeStore(o),
                                   embedder=_BadEmbed())
    monkeypatch.setattr(memory_index, "_ENSURED", set())

    assert idx.search("alice", "问一句") == []


def test_drop_owner_refuses_when_the_store_is_not_isolated(monkeypatch):
    """★★ ``tenant=None`` 在 PgVectorStore 里等于"不过滤"，那一下会把整张表删空。

    ``delete_tenant`` 必须硬拦这个组合，而不是"按调用方的意思执行"。
    """
    from src.rag.vector_store import PgVectorStore

    store = PgVectorStore("postgresql://x/y", "t", tenant=None)

    assert store.delete_tenant() == 0
