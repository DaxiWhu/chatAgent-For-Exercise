"""长期记忆的守卫：追加式、按用户隔离、注入的位置与稳定性。

这套守卫防四类"看起来正常"的错：

1. **覆盖而不是追加** —— 用户改口后，旧值被静默替换。后果是答不出"他上个月是
   怎么要求的"，而这正是判断"该按哪条做"的依据。
2. **叠了租户条件** —— 没有租户的用户（``tenant=''``）读到 0 条自己的记忆，
   而且零报错。
3. **注入每轮重排** —— 前缀缓存命中不了，每轮全量重算：变慢变贵，没有任何报错。
4. **删号没清记忆** —— 同名重建的账号直接继承上一位的规矩。
"""

from __future__ import annotations

import asyncio

import pytest

from src.agents import memory
from src.agents.memory_store import (
    MAX_CONTENT_CHARS,
    InMemoryMemoryStore,
    normalize_content,
    render,
)


@pytest.fixture
def store(monkeypatch):
    s = InMemoryMemoryStore()
    monkeypatch.setattr(memory, "_store", lambda: s)
    monkeypatch.setenv("AGENT_MEMORY", "1")
    return s


# --------------------------------------------------------------------------- #
# ① 追加式：改口留痕、改回原样能写进去
# --------------------------------------------------------------------------- #
def test_changing_a_setting_keeps_both_rows(store):
    """改口 ⇒ 两行、只有一行有效。旧行**不删**（要能回答"以前是怎么要求的"）。"""
    first = store.remember("u1", "以后都用中文回答", kind="preference")
    second = store.remember("u1", "以后用英文回答", kind="preference",
                            supersedes=first["id"])

    rows = store.rows()
    assert len(rows) == 2
    assert rows[0]["invalid_at"] is not None, "旧行必须标失效"
    assert rows[0]["superseded_by"] == second["id"], "必须记下被谁取代"
    assert rows[0]["supersede_reason"] == "superseded"
    assert rows[1]["invalid_at"] is None

    active = store.active("u1")
    assert [r["content"] for r in active] == ["以后用英文回答"]


def test_setting_it_back_does_not_hit_the_unique_index(store):
    """把设置改回原样 ⇒ 必须写得进去。

    这条就是"部分唯一索引（只对未失效行生效）"存在的原因：全表唯一时，
    新行的 content_hash 与已失效的旧行相同，插入直接失败 —— 而这只在用户真的
    改回去时才发生，测不到也想不到。
    """
    a = store.remember("u1", "先给结论", kind="preference")
    store.remember("u1", "先给依据", kind="preference", supersedes=a["id"])
    back = store.remember("u1", "先给结论", kind="preference",
                          supersedes="先给依据")

    assert back["ok"] is True
    assert [r["content"] for r in store.active("u1")] == ["先给结论"]
    assert len(store.rows()) == 3


def test_repeating_the_same_thing_refreshes_instead_of_duplicating(store):
    """同一句话再说一遍 ⇒ 刷新使用痕迹，不产生第二条（否则库里全是同义重复）。"""
    a = store.remember("u1", "项目用 PostgreSQL 16")
    b = store.remember("u1", "项目用 PostgreSQL 16")

    assert b["action"] == "refreshed" and b["id"] == a["id"]
    assert len(store.rows()) == 1


def test_forget_marks_instead_of_deleting(store):
    """"不需要了"也是**有时间点的**：标失效，不物理删。"""
    a = store.remember("u1", "用 Jira 跟踪缺陷", kind="reference")
    assert store.forget("u1", memory_id=a["id"]) == 1

    assert store.active("u1") == []
    assert len(store.rows()) == 1, "行必须还在，只是失效了"
    assert store.rows()[0]["supersede_reason"] == "revoked"


# --------------------------------------------------------------------------- #
# ② 隔离：只按用户，不叠租户
# --------------------------------------------------------------------------- #
def test_memories_are_isolated_by_user_only(store):
    """两个用户的记忆互不可见；**同一个用户即使租户不同也看得到自己的**。

    后一半是关键：租户是 RAG 的"内容可见性"口径，不是"个人数据归属"。把租户
    叠进查询条件的后果是没有租户的用户读到 0 条，且零报错 —— 看起来像"它忘了"。
    """
    store.remember("alice", "喜欢先看结论", tenant="acme")
    store.remember("bob", "喜欢先看依据", tenant="acme")

    assert [r["content"] for r in store.active("alice")] == ["喜欢先看结论"]
    assert [r["content"] for r in store.active("bob")] == ["喜欢先看依据"]
    assert store.active("carol") == []


def test_deleting_an_account_physically_purges_its_memories(store):
    """删号必须**真删**：同名重建会继承旧记忆，那是"新用户第一轮就被告知
    你以前要求过……"，比没有记忆糟得多。"""
    store.remember("alice", "做后端")
    store.remember("alice", "用中文")
    store.remember("bob", "做前端")

    assert store.forget_owner("alice") == 2
    assert store.active("alice") == []
    assert [r["content"] for r in store.active("bob")] == ["做前端"]


# --------------------------------------------------------------------------- #
# ③ 注入：位置、稳定性、跳过条件
# --------------------------------------------------------------------------- #
def _input(items, instructions=""):
    from agents.run_config import ModelInputData

    return ModelInputData(input=list(items), instructions=instructions)


def _context(owner="u1", thread="t-1"):
    from src.agents.context import TaskContext

    ctx = TaskContext(thread_id=thread)
    ctx.owner_key = owner
    return ctx


def test_injection_puts_memories_first_and_keeps_the_tail_intact(store):
    """记忆作为**一条 system 消息插在最前**；后面的历史逐条不变。

    为什么在最前：它是"背景"。放最后会和"关于你刚才做法的提醒"抢位置，
    而且前缀每次都不一样 ⇒ 缓存命中不了。
    """
    store.remember("u1", "以后都用中文回答", kind="preference")
    data = _input([{"role": "user", "content": "你好"}])

    out = asyncio.run(memory.inject_memories(data, context=_context()))

    assert out.input[0]["role"] == "system"
    assert "以后都用中文回答" in out.input[0]["content"]
    assert out.input[1:] == [{"role": "user", "content": "你好"}]


def test_injection_is_stable_across_calls(store):
    """同一份记忆 + 同一份输入 ⇒ 两次注入结果逐字相同（前缀缓存靠这个）。"""
    store.remember("u1", "回答先给结论", kind="preference")
    store.remember("u1", "项目用 PostgreSQL 16", kind="project")
    data = _input([{"role": "user", "content": "问一句"}], instructions="SYS")

    a = asyncio.run(memory.inject_memories(data, context=_context()))
    b = asyncio.run(memory.inject_memories(data, context=_context()))

    assert a.input == b.input
    assert a.instructions == b.instructions == "SYS"


def test_second_pass_does_not_duplicate_the_block(store):
    """同一次调用被加工两次时不许插两遍（否则上下文里同一段记忆出现两次）。"""
    store.remember("u1", "用中文回答", kind="preference")
    once = asyncio.run(memory.inject_memories(
        _input([{"role": "user", "content": "x"}]), context=_context()))
    twice = asyncio.run(memory.inject_memories(once, context=_context()))

    assert sum(1 for i in twice.input if memory.MARKER in str(i.get("content"))) == 1


def test_no_owner_means_no_read_and_no_write(store):
    """无法归属（匿名 / 机器调用）⇒ 不读不写，而不是读写一个公共桶。

    公共桶的后果是别人的偏好被当成这个人的，而那是最难发现的一类污染：
    回答变得更"有礼貌"，但每一条都是错的。
    """
    store.remember("u1", "用中文回答", kind="preference")
    data = _input([{"role": "user", "content": "x"}])

    out = asyncio.run(memory.inject_memories(data, context=_context(owner="")))
    assert out.input == data.input

    result = memory.remember_memory(owner_key="", content="随便一条")
    assert result["ok"] is False and result["reason"] == "no_owner"


def test_switch_off_means_no_injection_and_no_write(store, monkeypatch):
    """关掉开关 ⇒ 一次都不注入、一次都不写（退回"没有记忆"的行为）。"""
    store.remember("u1", "用中文回答", kind="preference")
    monkeypatch.setenv("AGENT_MEMORY", "0")

    data = _input([{"role": "user", "content": "x"}])
    assert asyncio.run(memory.inject_memories(data, context=_context())).input == data.input
    assert memory.remember_memory(owner_key="u1", content="新的")["ok"] is False


def test_injection_skips_blank_memory_sets(store):
    """一条有效记忆都没有 ⇒ 不插任何东西（空记忆不该改变输入）。"""
    data = _input([{"role": "user", "content": "x"}])
    out = asyncio.run(memory.inject_memories(data, context=_context()))
    assert out.input == data.input


# --------------------------------------------------------------------------- #
# ④ 渲染与清洗
# --------------------------------------------------------------------------- #
def test_render_caps_the_total_length():
    """条数多时按上限截断，并明确告诉模型"还有没列出的"。

    不截的后果：记得多的用户每轮多付一笔固定成本，而要紧的那几条早就排在最前了。
    """
    rows = [{"kind": "project", "content": "很长的记忆内容" * 20} for _ in range(50)]
    text = render(rows, limit_chars=500)

    assert len(text) <= 600
    assert "因长度上限未列出" in text


def test_content_is_collapsed_but_never_rewritten():
    """空白折叠 + 限长，但不改写内容 —— 路径、命令、报错串一改就失效。"""
    raw = "  命令：python -m app.migrations --apply\n\n  报错：OperationalError  "
    assert normalize_content(raw) == "命令：python -m app.migrations --apply 报错：OperationalError"

    long = "字" * (MAX_CONTENT_CHARS + 100)
    assert normalize_content(long).endswith("…")
    assert len(normalize_content(long)) == MAX_CONTENT_CHARS + 1


# --------------------------------------------------------------------------- #
# ⑧ PG 写入：并发撞上唯一索引时的分支
# --------------------------------------------------------------------------- #
# 为什么不连真库：``langgraph_app`` 没有建 schema 的权限，往 public 建表又等于
# 替用户 apply 迁移。真库版本在 ``tests/test_agent_memory_pg.py``（配了
# ``AGENT_SESSION_TEST_DSN`` 才跑）；这里用假游标盯**分支**，
# 语法本身已在一次性临时表上实测过（ON CONFLICT 能推断到部分唯一索引）。
class _FakeCursor:
    """只记账、只按脚本吐结果的游标。``rows`` 里每项对应一次 ``fetchone()``。"""

    def __init__(self, rows):
        self.rows = list(rows)
        self.sql: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql.append(" ".join(str(sql).split()))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self._cur


def _wire_pg(monkeypatch, rows):
    import psycopg

    cur = _FakeCursor(rows)
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _FakeConn(cur))
    from src.agents.memory_store import MemoryStore

    return MemoryStore("postgresql://fake/db"), cur


def test_pg_insert_leans_on_the_unique_index(monkeypatch):
    """★★ 写入必须写成 ``ON CONFLICT ... DO NOTHING``，不能是"先查后插"。

    先查后插在并发下必然翻车：两个事务都看不到对方未提交的行，后到的那个撞上
    部分唯一索引。2026-11-02 之后这条路**真的会并发**（压缩在后台写、本轮模型也在写）。
    """
    store, cur = _wire_pg(monkeypatch, [None, ("new-id",)])

    result = store.remember("u1", "提交信息用中文")

    assert result["action"] == "added"
    inserts = [s for s in cur.sql if s.startswith("INSERT")]
    assert len(inserts) == 1
    assert "ON CONFLICT (owner_key, content_hash) WHERE invalid_at IS NULL" in inserts[0]
    assert "DO NOTHING" in inserts[0]


def test_pg_losing_the_race_becomes_a_refresh(monkeypatch):
    """★★ 输掉竞争（INSERT 没插进去）⇒ 回查那条、当刷新，**不报成失败**。

    报失败会让调用方丢掉这条候选 —— 可表里明明已经有同一句话在生效，
    对使用者来说"记下了"才是事实。
    """
    store, cur = _wire_pg(monkeypatch, [None, None, ("existing-id",)])

    result = store.remember("u1", "提交信息用中文")

    assert result == {"ok": True, "id": "existing-id", "action": "refreshed"}
    assert any("use_count = use_count + 1" in s for s in cur.sql)


def test_pg_a_vanished_row_is_reported_not_guessed(monkeypatch):
    """回查也空（对方插完又立刻失效它）⇒ 如实回报没收下，不编一个 id。"""
    store, _ = _wire_pg(monkeypatch, [None, None, None])

    result = store.remember("u1", "提交信息用中文")

    assert result == {"ok": False, "reason": "conflict"}
