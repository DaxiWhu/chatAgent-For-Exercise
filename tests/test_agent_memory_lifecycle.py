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


def _age_one(store, content: str, days: int) -> None:
    """只把某一条往前推，用来构造"极老的豁免类 vs 较新的可丢类"。"""
    stamp = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    for row in store._rows:
        if row["content"] == content:
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


# --------------------------------------------------------------------------- #
# ⑤ 淘汰判据：与"这一轮先放哪条"是两份东西
# --------------------------------------------------------------------------- #
def test_a_correction_outlives_a_flood_of_project_facts():
    """★★ 核心：纠错记录即使最老、被几十条项目事实淹没，也不被淘汰。

    为什么这条最要紧：``KIND_WEIGHT`` 里纠错记录只有 0.5，是最低档之一，
    半衰期又只有 60 天。用**取回分数**砍尾巴时它第一批出局 —— 而它恰恰是
    "同一个错误别再犯"的唯一记录。丢一条偏好用户会说第二遍；丢一条纠错记录
    意味着同一个错误再犯一次，而用户会认为"我明明讲过了"。
    """
    store = M.InMemoryMemoryStore()
    store.remember("u1", "迁移前必须先备份", kind="feedback")
    for i in range(40):
        store.remember("u1", f"项目事实 {i}", kind="project")
    _age_all(store, 400)

    dropped = store.prune("u1", keep=20)

    alive = {r["content"] for r in store.active("u1", limit=100)}
    assert "迁移前必须先备份" in alive, "纠错记录不可淘汰"
    assert dropped == 21, "41 条 - 20"


def test_a_persona_is_never_pruned():
    """身份类**十年前的**也留着 —— 这条必须让"老"足够极端才有区分度。

    ⚠️ 第一版写成"全部推到 400 天前"，结果是**假通过**：身份类权重 1.0、
    半衰期 730 天，400 天后分数仍高于项目事实，按任何判据都轮不到它被砍。
    要证明豁免真的在起作用，被保护的必须**在旧判据下会掉到队尾**：
    十年老身份（分数 0.46）vs 十天前的事实（分数 0.70）。
    """
    store = M.InMemoryMemoryStore()
    store.remember("u1", "用户是后端工程师", kind="persona")
    for i in range(30):
        store.remember("u1", f"事实 {i}", kind="project")
    _age_all(store, 10)                       # 事实：10 天，已过保护期
    _age_one(store, "用户是后端工程师", 3650)  # 身份：十年前

    store.prune("u1", keep=10)

    alive = {r["content"] for r in store.active("u1", limit=100)}
    assert "用户是后端工程师" in alive


def test_a_pinned_memory_is_never_pruned():
    """置顶以前只是"排序恰好排最前"，不是规则 —— 动一次排序键就破，且不报错。

    ⚠️ 钉住的那条要**够老**：第一版让它和其他 30 条同龄，变异后靠 id 的随机
    顺序侥幸留在前 10 里 —— 那不是守卫，是抽签。十年老的钉住条目在旧判据下
    必然垫底，才测得出豁免。
    """
    store = M.InMemoryMemoryStore()
    store.remember("u1", "这条钉住", kind="project", pinned=True)
    for i in range(30):
        store.remember("u1", f"事实 {i}", kind="project")
    _age_all(store, 10)
    _age_one(store, "这条钉住", 3650)

    store.prune("u1", keep=10)

    alive = {r["content"] for r in store.active("u1", limit=100)}
    assert "这条钉住" in alive


def test_reference_goes_before_project_goes_before_preference():
    """淘汰顺序按可丢弃性：索引 → 项目事实 → 偏好。

    索引类本来就只是"去哪查"的指针，重建成本最低；项目事实会过期
    （"这版接口已经改成 v2"）；偏好最耐放。
    """
    store = M.InMemoryMemoryStore()
    store.remember("u1", "索引：手册在 docs/", kind="reference")
    store.remember("u1", "项目事实：接口是 v2", kind="project")
    store.remember("u1", "偏好：先给结论", kind="preference")
    _age_all(store, 400)

    store.prune("u1", keep=2)

    alive = {r["content"] for r in store.active("u1", limit=100)}
    assert "索引：手册在 docs/" not in alive
    assert "项目事实：接口是 v2" in alive
    assert "偏好：先给结论" in alive


def test_prune_does_nothing_when_the_protected_ones_already_fill_the_cap():
    """豁免类自己就超上限 ⇒ 一条都不砍，宁可超限。

    照常砍会变成"绕过豁免的后门"：豁免的排在前面占满名额，剩下的照砍，
    于是"不淘汰"在字面上成立、行为上没有。
    """
    store = M.InMemoryMemoryStore()
    for i in range(5):
        store.remember("u1", f"纠错 {i}", kind="feedback")
    for i in range(5):
        store.remember("u1", f"事实 {i}", kind="project")
    _age_all(store, 400)

    assert store.prune("u1", keep=3) == 0
    assert len(store.active("u1", limit=100)) == 10


def test_prune_order_disagrees_with_rank_order_on_feedback():
    """★★ 两份判据在纠错记录上必须给出**相反**结论 —— 这就是它们该分开的证明。

    同一批数据：取回排序把纠错记录垫底（权重 0.5、半衰期 60 天，取回时不优先），
    淘汰排序把它排到最前（不可淘汰）。

    如果哪天有人"顺手把两个函数合并成一个"，合并后只能满足其中一边，
    这条会立刻红 —— 而它红的理由不是数字对不上，是**语义被压平了**。
    """
    now = datetime.now(UTC)
    old = (now - timedelta(days=400)).isoformat()
    rows = [
        {"id": "fb", "kind": "feedback", "content": "迁移前先备份", "pinned": False,
         "use_count": 0, "created_at": old, "last_used_at": old},
        {"id": "pf", "kind": "preference", "content": "先给结论", "pinned": False,
         "use_count": 0, "created_at": old, "last_used_at": old},
    ]

    ranked = [r["id"] for r in M.rank_memories(rows, now=now)]
    pruned = [r["id"] for r in M.prune_order(rows, now=now)]

    assert ranked == ["pf", "fb"], "取回时偏好优先，纠错记录垫底"
    assert pruned == ["fb", "pf"], "淘汰时纠错记录不可淘汰，偏好可淘汰"
    assert M.prune_order(rows, now=now)[0] is rows[0], "必须返回原行对象，不是副本"


def test_the_protected_ones_take_room_away_from_the_others():
    """豁免类**占名额**：3 条纠错 + 5 条事实、上限 5 ⇒ 只砍 3 条事实。

    如果切片写成 ``ordered[limit:]``（忘了先减掉豁免的条数），5 条事实里
    一条都切不出来，总数停在 8 —— 淘汰看起来在跑、实际什么都没做，
    而日志里 ``dropped=0`` 也完全正常。
    """
    store = M.InMemoryMemoryStore()
    for i in range(3):
        store.remember("u1", f"纠错 {i}", kind="feedback")
    for i in range(5):
        store.remember("u1", f"事实 {i}", kind="project")
    _age_all(store, 400)

    dropped = store.prune("u1", keep=5)

    assert dropped == 3
    assert len(store.active("u1", limit=100)) == 5
