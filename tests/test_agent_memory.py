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
def test_changing_a_setting_keeps_both_rows_and_both_stay_injectable(store):
    """改口 ⇒ 两行都在，**旧版本也照样注入**，但带生效时间。

    为什么旧版本不退出注入（用户 2026-11-03 口径）：它是**历史**，不是垃圾。
    要答得出"他以前喜欢 A、现在喜欢 B"，两条必须同时在上下文里，否则模型只能
    看到最新那条，"以前是什么"永远查不到。渲染侧会给它加「YYYY-MM-DD 起生效」
    （见 ``render``），否则两条并排会变成自相矛盾。
    """
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
    assert [r["content"] for r in active] == ["以后用英文回答", "以后都用中文回答"]
    old = next(r for r in active if r["content"] == "以后都用中文回答")
    assert old["invalid_at"] is not None, "旧版本要带着结束时间进上下文"


def test_a_revoked_memory_leaves_the_context_entirely(store):
    """「忘掉这条」与「改口」**不是一回事**：前者要彻底退出注入。

    判据是 ``supersede_reason``，不是 ``invalid_at`` 是否为空 —— 只看那个会把
    三种失效原因压成一种，于是用户说"别再提这个"，系统却继续每轮带着它。
    """
    a = store.remember("u1", "这条以后别提了", kind="project")
    store.forget("u1", memory_id=a["id"])

    assert store.active("u1") == []
    assert len(store.rows()) == 1, "行还在（可查），只是不再注入"


def test_setting_it_back_does_not_hit_the_unique_index(store):
    """把设置改回原样 ⇒ 必须写得进去，而不是"复活"那条已失效的旧行。

    这条就是"部分唯一索引（只对未失效行生效）"存在的原因：全表唯一时，
    新行的 content_hash 与已失效的旧行相同，插入直接失败 —— 而这只在用户真的
    改回去时才发生，测不到也想不到。

    ⚠️ 另一个坑：去重若扫了**可注入集合**（包含旧版本），写回原样会命中那条
    superseded 的旧行、走"已记过"分支，于是新值没写进去，旧行还带着失效时间 ——
    一条既失效又有效、还被当成本轮内容的行。去重只能对"当前有效"的行做。
    """
    a = store.remember("u1", "先给结论", kind="preference")
    store.remember("u1", "先给依据", kind="preference", supersedes=a["id"])
    back = store.remember("u1", "先给结论", kind="preference",
                          supersedes="先给依据")

    assert back["ok"] is True
    assert back["action"] == "added", "必须是新写一条，不是刷新那条已失效的"
    live = [r for r in store.active("u1") if r["invalid_at"] is None]
    assert [r["content"] for r in live] == ["先给结论"]
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
def test_render_does_not_truncate_by_length():
    """**不按字数截断**（2026-11-03 口径）。

    注入的永远是提炼后的短句、条数也有硬顶，长度天然有界；再截一道只会切掉排在
    后面、本来该给的内容，而排序已经保证了紧要的在前。长度治理放在写入侧
    （``memory_digest``）与容量侧（``memory_lifecycle``），不在这里。
    """
    rows = [{"kind": "project", "content": f"第{i}条"} for i in range(50)]
    text = render(rows)

    assert "第49条" in text, "一条都不许丢"
    assert "未列出" not in text


def test_render_marks_the_old_version_with_its_effective_date():
    """旧版本必须带**生效时间**（精确到日），否则同一主题的两条并排会自相矛盾。

    标的是生效时间、不是结束时间（用户 2026-11-03 口径）。原先标「已结束，截至
    2026-08」有两个毛病：只到月份 ⇒ 同月内改两次分不出先后；而且"管到什么时候"
    本来就能从下一条的生效时间推出来，写两遍只是多一个会写错的地方。
    """
    rows = [
        {"kind": "preference", "content": "喜欢深色主题", "invalid_at": None,
         "valid_from": "2026-08-20T10:00:00+00:00"},
        {"kind": "preference", "content": "喜欢浅色主题",
         "invalid_at": "2026-08-15T10:00:00+00:00",
         "valid_from": "2025-03-07T10:00:00+00:00"},
    ]
    text = render(rows)

    assert "（2025-03-07 起生效）" in text, "旧版本要标生效时间，且是年月日"
    body = text.split("\n")[1:]          # 抬头里也提到「起生效」，只数条目行
    assert sum("起生效" in line for line in body) == 1, "只有旧版本带标记"
    assert "2026-08" not in text, "不许再标结束时间"
    assert "以前是什么" in text, "抬头要解释标记的含义"


def test_render_falls_back_to_creation_time_when_valid_from_is_missing():
    """老数据 / 检索层给的字典可能没有 ``valid_from``，退回 ``created_at``，不许崩。"""
    rows = [{"kind": "project", "content": "旧做法",
             "invalid_at": "2026-08-15T10:00:00+00:00",
             "created_at": "2025-12-01T10:00:00+00:00"}]

    assert "（2025-12-01 起生效）" in render(rows)


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


def test_pg_prune_deletes_by_score_and_never_takes_a_pinned_row(monkeypatch):
    """★ PG 分支：物理 ``DELETE``，且钉住的那条绝不出现在 id 列表里。

    为什么验"列表里没有谁"而不是只验"删了几条"：两版各写一遍淘汰逻辑时，最典型
    的漂移是内存版改了、PG 版忘了 —— 本地开发多半走内存降级，一切正常；上生产才
    丢数据。而 ``pinned`` 现在是**唯一硬保**的东西（用户显式钉住的，不是我们的
    推断），丢了它等于把"这条必须一直记着"这个动作悄悄废掉。

    另一件必验的事：这条 SQL 必须是 ``DELETE``。旧的 ``UPDATE ... SET stale_at``
    会让表只增不减，而读取侧照样跳过它们 —— 那是"假装保留"，比删更难发现。
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

    def row(ident: str, kind: str, *, pinned: bool = False) -> dict:
        return {"id": ident, "kind": kind, "content": ident, "raw_content": None,
                "pinned": pinned, "use_count": 0, "hit_count": 0,
                "created_at": old, "last_used_at": old,
                "invalid_at": None, "supersede_reason": ""}

    rows = [row("pin", "project", pinned=True)] + [
        row(f"p{i}", "project") for i in range(10)]
    store = MemoryStore("postgresql://fake/db")
    monkeypatch.setattr(store, "_all_rows", lambda owner: rows)

    store.prune("u1", keep=5)

    assert "DELETE FROM agent_memories" in captured["sql"], "必须是物理删"
    victims = captured["params"][1]
    assert "pin" not in victims, "钉住的不许被删"
    assert len(victims) == 6, "11 条 - 5，且只从非钉住的那 10 条里取"


# --------------------------------------------------------------------------- #
# ⑦ 阈值 20、频率按"一次运行"去重、原话永不注入
# --------------------------------------------------------------------------- #
def test_the_all_in_band_defaults_to_twenty(monkeypatch):
    """20 条以内全量注入，21 条起走检索（用户 2026-11-03 口径）。

    为什么这个数变小了而不是变大：全量注入的前提是"内容稳定 ⇒ 前缀稳定 ⇒
    上游缓存命中"。条数一多这个前提就不值钱了（谁也不会为 40 条小句子省那点钱
    而让每轮多背几千 token），不如早点交给检索。
    """
    monkeypatch.delenv("AGENT_MEMORY_RETRIEVE_ABOVE", raising=False)
    assert memory.retrieve_above() == 20

    monkeypatch.setenv("AGENT_MEMORY_RETRIEVE_ABOVE", "35")
    assert memory.retrieve_above() == 35


def test_hits_are_counted_once_per_run_not_once_per_model_call(store):
    """★★ 访问频率按**一次运行**去重，不是按"模型调用了几次"。

    一次提问走 5 轮工具循环，注入口就跑 5 次。不去重的话，同一条记忆会被
    ``touch`` 5 次 —— 频率是卸压分数里占 0.45 的那一项，放大 5 倍不是小事，
    而且放大的方向恰好是"工具调用多的话题"。

    判据在 ``TaskContext.memory_hit_ids``：同一份上下文里只计一次。
    """
    store.remember("u1", "回答先给结论", kind="preference")
    single = _input([{"role": "user", "content": "第一个问题"}])

    ctx = _context()
    asyncio.run(memory.inject_memories(single, context=ctx))
    asyncio.run(memory.inject_memories(single, context=ctx))
    asyncio.run(memory.inject_memories(single, context=ctx))

    row = store.rows()[0]
    assert row["hit_count"] == 1, "同一次运行里三次注入只算一次"

    asyncio.run(memory.inject_memories(single, context=_context()))
    assert store.rows()[0]["hit_count"] == 2, "换一次运行就要各算一次"


def test_the_raw_copy_is_never_injected(store):
    """原话只给用户追溯，注入的永远是提炼后的短句。

    注入了原话的后果不是"多占几行"：它会每轮重复一遍长文，用户看到的上下文
    突然变长，而模型还得在"提炼版"和"原话"之间自己判断哪个算数。
    """
    memory.remember_memory(owner_key="u1", content="回答先给结论",
                           kind="preference", raw_content="原话" * 200)
    data = _input([{"role": "user", "content": "你好"}])

    out = asyncio.run(memory.inject_memories(data, context=_context()))

    text = str(out.input[0]["content"])
    assert "回答先给结论" in text
    assert "原话原话" not in text
