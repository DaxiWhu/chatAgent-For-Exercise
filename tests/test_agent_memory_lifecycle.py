"""长期记忆的取回与生命周期：排序按价值、容量淘汰、整批回滚。

这一组守的是 2026-11-02 那次改动，原因是原来的排序有一个**静默**的坏结果：

    ORDER BY pinned DESC, use_count DESC, created_at DESC LIMIT 40

``created_at`` 倒序 = 新写的排前面。而「稳定偏好」「用户画像」恰恰是**最早**写入、
之后很少重写的东西 —— 表一过 40 条，它们就被后来几十条临时项目事实挤出去。
行还在库里、查得到，但永远进不了上下文，而且没有任何提示。

所以这里的第一条断言是"很早写的偏好仍在可注入集合里"，它同时也是那条回归的守卫。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.agents import memory_store as M


def _age_all(store, days: int) -> None:
    """把所有行的创建/使用时间往前推，绕开"保护期内不淘汰"。"""
    stamp = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    for row in store._rows:
        row["created_at"] = stamp
        if row["last_used_at"] is not None:
            row["last_used_at"] = stamp


# --------------------------------------------------------------------------- #
# ① 排序：稳定偏好不能被"新写的临时事实"挤掉
# --------------------------------------------------------------------------- #
def test_an_old_preference_survives_a_flood_of_new_facts():
    """★★ 核心回归：45 条新事实 + 1 条 120 天前写的偏好，偏好必须还在前 40 里。

    改回 ``created_at DESC`` 这条就会红，而且红得很安静 —— 它只是"某条记忆
    不见了"，同一份测试不比对具体 id 的话根本看不出来。
    """
    store = M.InMemoryMemoryStore()
    for i in range(45):
        store.remember("u1", f"项目事实 {i}", kind="project")
    kept = store.remember("u1", "日期一律用 YYYY-MM-DD", kind="preference")

    old = (datetime.now(UTC) - timedelta(days=120)).isoformat()
    for row in store._rows:
        if row["id"] == kept["id"]:
            row["created_at"] = row["last_used_at"] = old

    contents = [r["content"] for r in store.active("u1", limit=40)]

    assert "日期一律用 YYYY-MM-DD" in contents, (
        "120 天前写下的稳定偏好被 45 条新事实挤出了前 40 条 —— "
        "它还在库里，但模型永远看不到，且没有任何提示"
    )


def test_pinned_still_wins_over_everything():
    """pinned 由人显式钉住，分数再高也压不过它。"""
    store = M.InMemoryMemoryStore()
    store.remember("u1", "这条要钉住", kind="reference", pinned=True)
    for i in range(5):
        store.remember("u1", f"高频事实 {i}", kind="persona")

    assert store.active("u1", limit=1)[0]["content"] == "这条要钉住"


def test_higher_weight_kind_beats_a_lower_one_of_the_same_age():
    """同一时间写下的两条，类别权重决定先后（persona > preference > project）。"""
    store = M.InMemoryMemoryStore()
    store.remember("u1", "低权重", kind="reference")
    store.remember("u1", "高权重", kind="persona")

    order = [r["content"] for r in store.active("u1", limit=2)]

    assert order == ["高权重", "低权重"]


def test_ranking_is_deterministic_when_scores_tie():
    """同分时顺序必须**确定**，否则注入出去的前缀每轮都在变，上游缓存永远命不中。"""
    rows = [
        {"id": "a", "kind": "project", "content": "x", "pinned": False,
         "use_count": 0, "created_at": "2026-01-01T00:00:00+00:00",
         "last_used_at": None},
        {"id": "b", "kind": "project", "content": "y", "pinned": False,
         "use_count": 0, "created_at": "2026-01-01T00:00:00+00:00",
         "last_used_at": None},
    ]

    first = [r["id"] for r in M.rank_memories(rows, now=datetime.now(UTC))]
    second = [r["id"] for r in M.rank_memories(list(reversed(rows)),
                                              now=datetime.now(UTC))]

    assert first == second, "输入顺序一变结果就变 ⇒ 前缀每轮不一样"


def test_an_old_memory_that_is_still_used_does_not_decay():
    """衰减的基准是"最后一次被用到"，不是"创建时间"。"""
    now = datetime.now(UTC)
    base = {"kind": "project", "content": "x", "pinned": False, "use_count": 0,
            "created_at": (now - timedelta(days=365)).isoformat()}
    stale = dict(base, id="stale", last_used_at=(now - timedelta(days=365)).isoformat())
    fresh = dict(base, id="fresh", last_used_at=(now - timedelta(days=1)).isoformat())

    order = [r["id"] for r in M.rank_memories([stale, fresh], now=now)]

    assert order[0] == "fresh"


# --------------------------------------------------------------------------- #
# ② 淘汰：标记而不删
# --------------------------------------------------------------------------- #
def test_prune_marks_the_tail_stale_instead_of_deleting(monkeypatch):
    """★ 超上限的标 stale_at：不再注入，但**行还在**。

    物理删的后果是"用户半年前定过的一条规矩"永久消失，而没人知道自己丢过什么。
    """
    store = M.InMemoryMemoryStore()
    for i in range(30):
        store.remember("u1", f"事实 {i}", kind="project")
    _age_all(store, 30)

    dropped = store.prune("u1", keep=20)

    assert dropped == 10
    assert len(store.rows()) == 30, "淘汰不是删除"
    assert len(store.active("u1", limit=100)) == 20
    assert sum(1 for r in store.rows() if r["stale_at"]) == 10


def test_prune_is_a_noop_below_the_cap():
    store = M.InMemoryMemoryStore()
    for i in range(5):
        store.remember("u1", f"事实 {i}")

    assert store.prune("u1", keep=20) == 0


def test_fresh_memories_are_protected_from_pruning():
    """★ 刚写进来的还没机会被 touch 过，分数天然低 —— 按分数砍尾巴会先砍它。

    保护期就是为这件事设的：淘汰要清理的是"长期没在场"的东西，
    不是"刚写还不够新的东西"。
    """
    store = M.InMemoryMemoryStore()
    for i in range(30):
        store.remember("u1", f"事实 {i}", kind="project")

    assert store.prune("u1", keep=10) == 0, "7 天内写入的不该被淘汰"
    assert len(store.active("u1", limit=100)) == 30


def test_prune_only_touches_its_own_owner():
    store = M.InMemoryMemoryStore()
    for i in range(30):
        store.remember("u1", f"事实 {i}")
        store.remember("u2", f"事实 {i}")
    _age_all(store, 30)

    store.prune("u1", keep=10)

    assert len(store.active("u1", limit=100)) == 10
    assert len(store.active("u2", limit=100)) == 30


# --------------------------------------------------------------------------- #
# ③ 来源与批次
# --------------------------------------------------------------------------- #
def test_batch_revoke_takes_the_whole_batch():
    """★★ 一次压缩抽出的候选共用一个批次号，出问题能整批撤。

    污染进来时你不知道它抽了几条、都抽了什么；逐条找既慢又容易漏。
    """
    store = M.InMemoryMemoryStore()
    store.remember("u1", "候选一", kind="project", source="compaction", batch_id="b1")
    store.remember("u1", "候选二", kind="project", source="compaction", batch_id="b1")
    store.remember("u1", "模型自己记的", kind="project", source="memory")

    revoked = store.forget_batch("u1", "b1")

    assert revoked == 2
    left = [r["content"] for r in store.active("u1", limit=100)]
    assert left == ["模型自己记的"], "同批次之外的记忆不该被牵连"


def test_batch_revoke_does_not_cross_users():
    store = M.InMemoryMemoryStore()
    store.remember("u1", "甲的候选", batch_id="b1")
    store.remember("u2", "乙的候选", batch_id="b1")

    store.forget_batch("u1", "b1")

    assert [r["content"] for r in store.active("u1", limit=10)] == []
    assert [r["content"] for r in store.active("u2", limit=10)] == ["乙的候选"]


def test_source_is_recorded():
    store = M.InMemoryMemoryStore()
    store.remember("u1", "来自压缩", source="compaction", batch_id="b9")
    store.remember("u1", "来自模型", source="memory")

    by_source = {r["content"]: r["source"] for r in store.rows()}

    assert by_source == {"来自压缩": "compaction", "来自模型": "memory"}


# --------------------------------------------------------------------------- #
# ④ touch：只动 last_used_at，不动 use_count
# --------------------------------------------------------------------------- #
def test_touch_refreshes_recency_but_not_the_emphasis_counter():
    """★★ ``use_count`` 是"被重复强调几次"，不是"被读了几次"。

    混在一起就分不清"反复改口的一条"和"每轮都要用的一条"，而它们该有不同的权重。
    """
    store = M.InMemoryMemoryStore()
    got = store.remember("u1", "提交信息用中文")
    _age_all(store, 100)

    store.touch("u1", [got["id"]])

    row = next(r for r in store.rows() if r["id"] == got["id"])
    assert row["use_count"] == 0, "读一次不等于强调一次"
    assert row["last_used_at"] is not None


def test_touch_ignores_ids_from_other_owners():
    store = M.InMemoryMemoryStore()
    theirs = store.remember("u2", "别人的记忆")

    assert store.touch("u1", [theirs["id"]]) == 0


@pytest.mark.parametrize("raw,expect", [("", M._DEFAULT_MAX_LIVE), ("50", 50),
                                        ("1", 20), ("abc", M._DEFAULT_MAX_LIVE)])
def test_max_live_config(monkeypatch, raw, expect):
    """上限可配，但**下限 20**：再低就开始删仍在用的记忆，而空间本来就不缺。"""
    if raw:
        monkeypatch.setenv("AGENT_MEMORY_MAX_LIVE", raw)
    else:
        monkeypatch.delenv("AGENT_MEMORY_MAX_LIVE", raising=False)

    assert M.max_live_per_owner() == expect
