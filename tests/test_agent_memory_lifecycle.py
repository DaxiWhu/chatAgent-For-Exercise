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


def test_a_correction_outranks_project_facts_of_the_same_age():
    """★ 纠错记录靠**分数**排在最前，不靠类别白名单（2026-11-03 撤回豁免）。

    撤回豁免时补了两处：``KIND_WEIGHT['feedback']`` 0.5 → 0.8、半衰期 60 → 1095
    天。两处的作用**不对等**，实测过才敢这么写：

    * **刚写下的**（年龄 0，两条衰减项都是 1）⇒ 只剩权重说话。权重退回 0.5 时
      纠错记录当场输给 project（0.375 < 0.445）。**这一档是权重的守卫。**
    * **400 天前写的** ⇒ 权重 0.8 与 0.7 的差值 0.035 仍然大于衰减项能拉开的
      全部区间，所以即使半衰期退回 60 天它照样赢（0.282 > 0.265）。**这一档
      抓不到半衰期的变异**，它守的是另一件事："老记忆不会被后来的事实挤掉"。

    ⚠️ 半衰期那个数目前**没有可观测的行为差异**（对 project 永远被权重压过），
    所以别在这里假装验到它。第一版用例只写了 400 天那一档，变异权重回 0.5 时
    照样全绿 —— 一条断言同时被两个参数解释，是最典型的假通过。
    """
    now = datetime.now(UTC)

    def pair(age_days: int) -> tuple[dict, dict]:
        base = {"pinned": False, "use_count": 0, "hit_count": 0, "last_used_at": None,
                "created_at": (now - timedelta(days=age_days)).isoformat()}
        return (dict(base, id="f", kind="feedback", content="迁移前必须先备份"),
                dict(base, id="p", kind="project", content="事实"))

    for age in (0, 400):
        correction, fact = pair(age)
        assert M._score(correction, now) > M._score(fact, now), \
            f"年龄 {age} 天：纠错记录的分数应高于同龄项目事实"
        assert [r["id"] for r in M.rank_memories([fact, correction], now=now)][0] == "f"


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


def test_recency_follows_creation_time_not_last_use():
    """★★ 衰减只看 ``created_at``（不可变）；``last_used_at`` 一格都不许影响它。

    为什么这条最要紧：``last_used_at`` 每轮注入都会被刷成"现在"，拿它当基准等于
    衰减恒为 1 —— 实测一条十年没动的偏好被注入一次，分数从 0.418 跳到 0.805。
    而且第一轮到第二轮的排序会翻转一次，注入前缀跟着断、上游缓存全废。

    "被用过几次"由 ``hit_count`` 单独回答（见下一组），两件事不共用一列。
    """
    now = datetime.now(UTC)
    base = {"kind": "project", "content": "x", "pinned": False, "use_count": 0,
            "hit_count": 0, "created_at": (now - timedelta(days=365)).isoformat()}
    a = dict(base, id="a", last_used_at=(now - timedelta(days=365)).isoformat())
    b = dict(base, id="b", last_used_at=(now - timedelta(days=1)).isoformat())

    assert M._recency(a, now) == pytest.approx(M._recency(b, now)), \
        "last_used_at 不许影响新鲜度"
    assert M._score(a, now) == pytest.approx(M._score(b, now))


def test_frequency_outweighs_importance():
    """★★ 频率的权重（0.45）压过重要性（0.35）—— 用户 2026-11-03 定的口径。

    一条从没被用过的身份类（importance 1.0）分数 0.55；一条新写但被用过 10 次的
    项目事实（importance 0.7）分数 ≈ 0.895，排在它前面。
    "被反复召回"是唯一的行为反馈，其余两项都是写入那一刻就定死的推断。
    """
    now = datetime.now(UTC)
    fresh = (now - timedelta(days=1)).isoformat()
    persona = {"id": "p", "kind": "persona", "content": "x", "pinned": False,
               "use_count": 0, "hit_count": 0, "created_at": fresh,
               "last_used_at": None}
    used = {"id": "u", "kind": "project", "content": "y", "pinned": False,
            "use_count": 0, "hit_count": 10, "created_at": fresh,
            "last_used_at": fresh}

    order = [r["id"] for r in M.rank_memories([persona, used], now=now)]

    assert order == ["u", "p"]


def test_the_frequency_factor_saturates_at_ten_hits():
    """对数封顶：0→1 次的差别比 9→10 次重要得多，而且不许无限增长。

    线性计数的后果是"老资格"永远压住新来的 —— 一条被注入过 200 次的索引类
    会压过一条刚写的重要偏好，而前者很可能只是当年那个话题聊得久。
    """
    row = {"kind": "project", "content": "x", "pinned": False, "use_count": 0,
           "created_at": datetime.now(UTC).isoformat()}

    def freq(n: int) -> float:
        return M._frequency(dict(row, hit_count=n))

    assert freq(0) == 0.0
    assert freq(10) == pytest.approx(1.0)
    assert freq(200) == pytest.approx(1.0), "封顶后不再增长"
    assert freq(1) - freq(0) > freq(10) - freq(9)


# --------------------------------------------------------------------------- #
# ② 卸压：按分数物理删，不是标记过期
# --------------------------------------------------------------------------- #
def test_prune_deletes_rows_instead_of_marking_them():
    """★★ 第 3 段卸压是**物理删**（用户 2026-11-03 口径：剔除，不是标记过期）。

    旧的 ``UPDATE ... SET stale_at`` 有两个问题：表只增不减、备份越来越大；
    而读取侧照样跳过它们 —— 那是"假装保留"，比删更难发现（谁也不会去查
    "我是不是有一条看不见的记忆"）。

    注意"标记过期"仍然存在于另一条路径：**改口**（``invalid_at``）。两者的区别是
    前者是"该消失了"，后者是"变成了历史，还要留在上下文里"。
    """
    store = M.InMemoryMemoryStore()
    for i in range(12):
        store.remember("u1", f"事实 {i}", kind="project")
    _age_all(store, 400)

    dropped = store.prune("u1", keep=5)

    assert dropped == 7
    assert len(store.rows()) == 5, "行真没了，不是打了个标记"
    assert all(r.get("stale_at") is None for r in store.rows())


def test_prune_is_a_noop_below_both_caps():
    store = M.InMemoryMemoryStore()
    for i in range(5):
        store.remember("u1", f"事实 {i}", kind="project")
    _age_all(store, 400)

    assert store.prune("u1", keep=20) == 0


def test_prune_triggers_on_total_chars_even_when_the_row_count_is_fine():
    """★ 两个上限都要管：条数管不住"几条巨兽"，字数管不住"几百条碎片"。

    这条只让字数超：5 条 × 100 字 = 500 字，条数上限 200 完全够。
    """
    store = M.InMemoryMemoryStore()
    for i in range(5):
        store.remember("u1", f"{i}" + "字" * 99, kind="project")
    _age_all(store, 400)

    dropped = store.prune("u1", keep=200, max_chars=200)

    assert dropped > 0
    assert store.measure("u1")["chars"] <= 200


def test_fresh_memories_are_protected_from_pruning():
    """7 天内写的不删：它还没被任何一次检索碰过，砍它等于砍"没机会"。"""
    store = M.InMemoryMemoryStore()
    for i in range(10):
        store.remember("u1", f"刚写的 {i}", kind="project")

    assert store.prune("u1", keep=2) == 0
    assert len(store.rows()) == 10


def test_pinned_is_never_deleted_even_when_it_exceeds_the_cap():
    """``pinned`` 是**唯一硬保**的东西（用户显式钉住，不是我们的推断）。

    为什么必须让钉住的条数**超过上限**：钉住的条目本来就排在排序最前、分数也最高，
    只放一条的话，"没被删"可能只是"轮不到它"—— 去掉保护也照样绿，是假通过。
    两条钉住 + 上限 1 才有区分度：去掉保护会删掉其中一条，留着保护则宁可超限。

    类别豁免（身份 / 纠错记录）已经撤回：那等于给整类开后门，同类里的宝贝和垃圾
    同命。它们靠分数活（纠错记录权重 0.8、半衰期 1095 天）。
    """
    store = M.InMemoryMemoryStore()
    store.remember("u1", "钉住甲", kind="project", pinned=True)
    store.remember("u1", "钉住乙", kind="project", pinned=True)
    for i in range(8):
        store.remember("u1", f"事实 {i}", kind="project")
    _age_all(store, 400)

    store.prune("u1", keep=1)

    alive = [r["content"] for r in store.rows()]
    assert sorted(alive) == ["钉住乙", "钉住甲"], "钉住的一条都不许删，宁可超限"


def test_prune_only_touches_its_own_owner():
    store = M.InMemoryMemoryStore()
    for i in range(10):
        store.remember("u1", f"甲 {i}", kind="project")
    for i in range(10):
        store.remember("u2", f"乙 {i}", kind="project")
    _age_all(store, 400)

    store.prune("u1", keep=3)

    assert len([r for r in store.rows() if r["owner_key"] == "u2"]) == 10


def test_measure_counts_every_row_including_the_raw_copy():
    """容量口径：**所有行**（含旧版本、含被忘掉的），字数含 ``raw_content``。

    原话不注入但吃存储与备份，不算它就会"原话越囤越多、永远不触发回收"。
    """
    store = M.InMemoryMemoryStore()
    store.remember("u1", "短", kind="project", raw_content="原" * 500)
    a = store.remember("u1", "会被改口的一条", kind="preference")
    store.remember("u1", "新的一条", kind="preference", supersedes=a["id"])
    store.forget("u1", memory_id=store.rows()[0]["id"])

    got = store.measure("u1")

    assert got["rows"] == len(store.rows()) == 3, "失效的、被忘掉的都算"
    assert got["chars"] >= 500, "原话要算进字数"


# --------------------------------------------------------------------------- #
# ③ 三段卸压：回收原话 → 压缩 → 剔除
# --------------------------------------------------------------------------- #
def test_relieve_reclaims_the_raw_copy_before_compressing(monkeypatch):
    """第 1 段：原话先回收，够用就不花模型调用。

    它是唯一零信息损失的动作 —— 正文还在，原话只是"提炼前长什么样"的备份。
    """
    from src.agents import memory_lifecycle as L

    monkeypatch.setenv("AGENT_MEMORY_MAX_LIVE", "20")
    monkeypatch.setenv("AGENT_MEMORY_MAX_CHARS", "2000")
    store = M.InMemoryMemoryStore()
    store.remember("u1", "短句", kind="project", raw_content="原" * 3000)

    calls: list = []
    out = L.relieve("u1", store=store,
                    digest_fn=lambda *a: calls.append(a) or "压过的句子")

    assert out["reclaimed"] == 1
    assert calls == [], "回收够了就不该再调模型"
    assert store.measure("u1")["chars"] <= 2000


def test_relieve_compresses_the_longest_until_it_fits(monkeypatch):
    """第 2 段：字数还超 ⇒ 逐条压最长的，压到不超为止。"""
    from src.agents import memory_lifecycle as L

    monkeypatch.setenv("AGENT_MEMORY_MAX_LIVE", "200")
    monkeypatch.setenv("AGENT_MEMORY_MAX_CHARS", "2000")
    monkeypatch.setenv("AGENT_MEMORY_DIGEST_ABOVE", "50")
    monkeypatch.setenv("AGENT_MEMORY_DIGEST_PER_ROUND", "20")
    store = M.InMemoryMemoryStore()
    for i in range(25):
        store.remember("u1", f"{i}" + "字" * 99, kind="project")

    out = L.relieve("u1", store=store, digest_fn=lambda *a: "短")

    assert out["digested"] == 6, "2500 字压到 ≤2000 需要 6 条"
    assert out["deleted"] == 0, "压缩够用就不许删"
    assert store.measure("u1")["chars"] <= 2000


def test_relieve_deletes_when_nothing_is_long_enough_to_compress(monkeypatch):
    """没有"长到值得压"的条目 ⇒ 压缩不适用，直接进第 3 段。

    关键区别：``pick_longest`` 返回 None 是"压缩不适用"（正常），
    返回了但压不动才是"链路不可用"（见下一条）。
    """
    from src.agents import memory_lifecycle as L

    monkeypatch.setenv("AGENT_MEMORY_MAX_LIVE", "200")
    monkeypatch.setenv("AGENT_MEMORY_MAX_CHARS", "2000")
    monkeypatch.setenv("AGENT_MEMORY_DIGEST_ABOVE", "200")
    store = M.InMemoryMemoryStore()
    for i in range(25):
        store.remember("u1", f"{i}" + "字" * 99, kind="project")
    _age_all(store, 60)          # 绕开 7 天保护期：这里验的是"该删时会删"

    out = L.relieve("u1", store=store, digest_fn=lambda *a: "不该被调用")

    assert out["digested"] == 0
    assert out["deleted"] > 0
    assert store.measure("u1")["chars"] <= 2000


def test_relieve_refuses_to_delete_when_compression_fails(monkeypatch):
    """★★ 安全守卫：有该压的、却一条都没压成 ⇒ **不删除**。

    压不动通常意味着整条优化链路不可用（没配 Key、网关 502）。此时按分数删数据
    是不可逆的伤害，而"超限一会儿"完全可恢复 —— 等下一条记忆写入时再试。
    少了这条判断，一次网关抖动就能把用户攒了半年的记忆按分数清掉一批。
    """
    from src.agents import memory_lifecycle as L

    monkeypatch.setenv("AGENT_MEMORY_MAX_LIVE", "200")
    monkeypatch.setenv("AGENT_MEMORY_MAX_CHARS", "2000")
    monkeypatch.setenv("AGENT_MEMORY_DIGEST_ABOVE", "50")
    store = M.InMemoryMemoryStore()
    for i in range(25):
        store.remember("u1", f"{i}" + "字" * 99, kind="project")
    # ⚠️ 必须把时间推过去：不推的话 7 天保护期会挡住删除，这条用例就变成在验
    # "保护期有效"而不是"压缩失败时不删" —— 变异（去掉守卫）也能通过，是假绿。
    _age_all(store, 60)
    before = len(store.rows())

    out = L.relieve("u1", store=store, digest_fn=lambda *a: "")

    assert out["digested"] == 0
    assert out["deleted"] == 0, "压缩全败 ⇒ 一条都不许删"
    assert len(store.rows()) == before


def test_relieve_does_nothing_below_both_caps(monkeypatch):
    from src.agents import memory_lifecycle as L

    monkeypatch.setenv("AGENT_MEMORY_MAX_LIVE", "200")
    monkeypatch.setenv("AGENT_MEMORY_MAX_CHARS", "20000")
    store = M.InMemoryMemoryStore()
    store.remember("u1", "一条", kind="project")

    out = L.relieve("u1", store=store)

    assert out == {"reclaimed": 0, "digested": 0, "deleted": 0}


# --------------------------------------------------------------------------- #
# ④ touch：只累加访问频率，不碰"被强调几次"
# --------------------------------------------------------------------------- #
def test_touch_counts_the_frequency_but_not_the_emphasis_counter():
    """★★ ``hit_count`` 与 ``use_count`` 是两件事，绝不合并。

    ``use_count``：同一句话被**重复说了几遍**（去重分支里 +1），表达"用户特意强调"。
    ``hit_count``：被**注入进上下文过几次**，表达"这条最近被需要"。

    混在一起的后果：分不清"反复改口的一条"和"每轮都要用的一条"，而它们在
    卸压分数里该有完全不同的分量（频率占 0.45）。
    """
    store = M.InMemoryMemoryStore()
    got = store.remember("u1", "提交信息用中文")
    _age_all(store, 100)

    store.touch("u1", [got["id"]])
    store.touch("u1", [got["id"]])

    row = next(r for r in store.rows() if r["id"] == got["id"])
    assert row["use_count"] == 0, "读一次不等于强调一次"
    assert row["hit_count"] == 2
    assert row["last_used_at"] is not None, "最近提起时间仍然要记（审计用）"


def test_touch_ignores_ids_from_other_owners():
    store = M.InMemoryMemoryStore()
    theirs = store.remember("u2", "别人的记忆")

    assert store.touch("u1", [theirs["id"]]) == 0


# --------------------------------------------------------------------------- #
# ⑤ 配置
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expect", [("", M._DEFAULT_MAX_LIVE), ("50", 50),
                                        ("1", 20), ("abc", M._DEFAULT_MAX_LIVE)])
def test_max_live_config(monkeypatch, raw, expect):
    monkeypatch.setenv("AGENT_MEMORY_MAX_LIVE", raw)
    assert M.max_live_per_owner() == expect


@pytest.mark.parametrize("raw,expect", [("", M._DEFAULT_MAX_CHARS), ("9000", 9000),
                                        ("1", 2000), ("abc", M._DEFAULT_MAX_CHARS)])
def test_max_chars_config(monkeypatch, raw, expect):
    monkeypatch.setenv("AGENT_MEMORY_MAX_CHARS", raw)
    assert M.max_chars_per_owner() == expect


@pytest.mark.parametrize("raw,expect", [("", 5), ("0", 0), ("12", 12), ("x", 5)])
def test_digest_per_round_config(monkeypatch, raw, expect):
    from src.agents import memory_lifecycle as L

    monkeypatch.setenv("AGENT_MEMORY_DIGEST_PER_ROUND", raw)
    assert L.digest_per_round() == expect


# --------------------------------------------------------------------------- #
# ⑥ 批次与来源
# --------------------------------------------------------------------------- #
def test_batch_revoke_takes_the_whole_batch():
    store = M.InMemoryMemoryStore()
    for i in range(3):
        store.remember("u1", f"抽出来的 {i}", source="compaction", batch_id="b1")
    store.remember("u1", "模型自己写的", kind="preference")

    assert store.forget_batch("u1", "b1") == 3
    assert [r["content"] for r in store.active("u1")] == ["模型自己写的"]


def test_batch_revoke_does_not_cross_users():
    store = M.InMemoryMemoryStore()
    store.remember("u1", "甲的", source="compaction", batch_id="b1")
    store.remember("u2", "乙的", source="compaction", batch_id="b1")

    store.forget_batch("u1", "b1")

    assert len(store.active("u2")) == 1


def test_source_is_recorded():
    store = M.InMemoryMemoryStore()
    store.remember("u1", "压缩抽的", source="compaction", batch_id="b9")

    row = store.rows()[0]
    assert row["source"] == "compaction"
    assert row["batch_id"] == "b9"
