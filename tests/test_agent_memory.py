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


def test_injection_sits_between_history_and_the_turn_question(store):
    """记忆作为**一条 system 消息插在对话历史之后、本轮提问之前**。

    为什么不在最前（2026-11-03 实测改）：上游缓存复用"从头逐字节相同"的那一段，
    记忆块放第一条时，它后面还有系统提示、工具定义、全部历史、本轮提问 ——
    记忆块一变（检索模式下**每轮都变**）这些全部重算，实测命中率直接归零。
    放到历史之后，稳定前缀变成"系统提示 + 工具 + 全部历史"，只剩末尾一小截重算。

    ⚠️ 这条断言必须用**多轮历史**：只有一条 user 消息时，"插在最后一条 user 之前"
    与"插在最前"是同一个位置，测不出区别（第一版就是这么假通过的）。
    """
    store.remember("u1", "以后都用中文回答", kind="preference")
    history = [
        {"role": "user", "content": "早上好"},
        {"role": "assistant", "content": "早"},
        {"role": "user", "content": "现在几点了"},
        {"role": "assistant", "content": "九点"},
        {"role": "user", "content": "你好"},
    ]
    data = _input(history)

    out = asyncio.run(memory.inject_memories(data, context=_context()))

    at = next(i for i, msg in enumerate(out.input) if memory.MARKER in str(msg["content"]))
    assert out.input[at]["role"] == "system"
    assert "以后都用中文回答" in out.input[at]["content"]
    # 前面是原样不动的历史，后面只剩本轮提问。
    assert out.input[:at] == history[:-1]
    assert out.input[at + 1:] == history[-1:]
    # 历史必须**完整**保留（插一条，不改一条）。
    assert len(out.input) == len(history) + 1


def test_injection_goes_before_the_question_even_mid_tool_loop(store):
    """多步循环中末尾是 assistant(tool_call) / tool(result) ⇒ 记忆块仍插在本轮提问之前。

    为什么不"追加到末尾"：那会把记忆块塞进工具调用链中间，破坏"调用与结果相邻"。
    """
    store.remember("u1", "日期用 YYYY-MM-DD", kind="preference")
    tool_loop = [
        {"role": "user", "content": "上一轮问的"},
        {"role": "assistant", "content": "上一轮答的"},
        {"role": "user", "content": "查一下昨天的数据"},
        {"role": "assistant", "content": "好的", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "content": "查询结果：12 条", "tool_call_id": "c1"},
    ]
    data = _input(tool_loop)

    out = asyncio.run(memory.inject_memories(data, context=_context()))

    at = next(i for i, msg in enumerate(out.input) if memory.MARKER in str(msg["content"]))
    # 位置 = 本轮提问（索引 2）之前；工具调用与结果仍然相邻。
    assert at == 2
    assert out.input[at + 1] == tool_loop[2]
    assert out.input[at + 2] == tool_loop[3]
    assert out.input[at + 3] == tool_loop[4]
    # 前面那段历史逐条不变 —— 它是稳定前缀，缓存靠它。
    assert out.input[:at] == tool_loop[:2]


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
    """同一次调用被加工两次时不许插两遍（否则上下文里同一段记忆出现两次）。

    ⚠️ 必须用**带历史**的输入：位置在历史之后时，插好的记忆块**不在第一条**，
    只看 ``items[0]`` 的去重判据会漏掉它 —— 单条 user 的用例测不出这件事
    （第一版就是这么漏过去的，变异存活）。
    """
    store.remember("u1", "用中文回答", kind="preference")
    history = [
        {"role": "user", "content": "上一轮问的"},
        {"role": "assistant", "content": "上一轮答的"},
        {"role": "user", "content": "x"},
    ]
    once = asyncio.run(memory.inject_memories(_input(history), context=_context()))
    twice = asyncio.run(memory.inject_memories(once, context=_context()))

    assert sum(1 for i in twice.input if memory.MARKER in str(i.get("content"))) == 1
    assert twice.input == once.input


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


def test_pg_prune_never_lists_a_protected_row_as_a_victim(monkeypatch):
    """★ PG 分支的受害名单必须与内存版同源（都来自 ``prune_order``）。

    两版各写一遍淘汰逻辑时，最典型的漂移是"内存版加了豁免、PG 版忘了"——
    本地开发多半走内存降级，一切正常；上生产才丢数据，而且丢的正好是
    纠错记录那类最贵的。所以这里不验"淘汰了几条"，验的是**UPDATE 的 id 列表里
    有没有豁免的那条**。
    """
    from datetime import UTC, datetime, timedelta

    from src.agents.memory_store import MemoryStore

    captured: dict = {}

    class _Cur:
        rowcount = 6

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            captured["sql"] = " ".join(str(sql).split())
            captured["params"] = params

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cur()

    import psycopg

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _Conn())

    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()

    def row(ident: str, kind: str) -> dict:
        return {"id": ident, "kind": kind, "content": ident, "pinned": False,
                "use_count": 0, "created_at": old, "last_used_at": old}

    live = [row("fb", "feedback")] + [row(f"p{i}", "project") for i in range(10)]
    store = MemoryStore("postgresql://fake/db")
    monkeypatch.setattr(store, "_live_rows", lambda owner: live)

    store.prune("u1", keep=5)

    victims = captured["params"][1]
    assert "fb" not in victims, "纠错记录不许出现在 UPDATE 里"
    assert len(victims) == 6, "11 条 - 5（1 条豁免 + 4 条事实）"
