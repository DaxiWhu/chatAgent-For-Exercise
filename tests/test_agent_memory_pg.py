"""长期记忆的**真库**集成测试：并发写同一条不该炸。

为什么这条必须有
----------------
2026-11-02 之前，记忆的写入方只有一个（模型调 ``remember``），而且同一轮的 tool call
是串行的 —— "两个人同时插同一条"基本不发生。之后压缩也会写记忆，而且是**后台任务**，
与本轮模型并行。于是这条路径第一次真的会并发。

旧的实现是"先 SELECT 查有没有、没有再 INSERT"。在 READ COMMITTED 下两个事务都查不到
对方未提交的行、都去插，后到的那个撞上部分唯一索引
``(owner_key, content_hash) WHERE invalid_at IS NULL`` 抛 ``UniqueViolation``。
表现是"偶尔丢一条记忆"，日志里一条数据库异常 —— 不会静默，但很容易被当成噪声。

现在交给唯一索引裁决（``ON CONFLICT ... DO NOTHING RETURNING``），没插进去就回查、
当作刷新。本文件证明的正是这个分支：**两个线程同时写，不抛异常、表里只有一行**。

怎么跑
------
需要一个真实 PG，用 ``AGENT_SESSION_TEST_DSN`` 指过去（与会话那边的真库测试共用一个
库）；没配就整体跳过，本地全量测试不受影响。
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest

from src.agents.memory_store import MemoryStore

_DSN = (os.getenv("AGENT_SESSION_TEST_DSN") or "").strip()

pytestmark = pytest.mark.skipif(
    not _DSN, reason="未配置 AGENT_SESSION_TEST_DSN ⇒ 跳过真库集成测试"
)


@pytest.fixture
def store():
    s = MemoryStore(_DSN)
    s.ensure_schema()
    return s


@pytest.fixture
def owner():
    """每个用例一个独立用户名，结束就把自己那些行删干净（不依赖测试顺序）。"""
    name = f"it-{uuid.uuid4().hex[:12]}"
    yield name
    s = MemoryStore(_DSN)
    s.forget_owner(name)


def test_concurrent_writes_of_the_same_memory_do_not_raise(store, owner):
    """★★ 两个线程同时写同一条 ⇒ 都不抛、表里只有一行。

    这条如果退回去用"先查后插"，会稳定复现 UniqueViolation —— 因为它不是竞态的
    偶然，而是两个事务都读不到对方未提交的行这件事本身。
    """
    results: list[dict] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _write() -> None:
        barrier.wait(timeout=10)
        try:
            results.append(store.remember(owner, "日期格式一律用 DD/MM/YYYY"))
        except BaseException as e:  # noqa: BLE001  这里就是要看它抛不抛
            errors.append(e)

    threads = [threading.Thread(target=_write) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert errors == [], f"并发写不该抛异常：{errors}"
    assert len(results) == 2
    assert all(r["ok"] for r in results)

    rows = store.active(owner)
    assert len(rows) == 1, "同一条内容只能有一行生效"
    assert {r["action"] for r in results} == {"added", "refreshed"}


def test_a_revoked_row_does_not_block_re_adding_it(store, owner):
    """改回原样（旧行已标失效）⇒ 能重新插入，不被"部分"唯一索引挡住。

    全表唯一索引会在这里挡住，那正是当初选择部分索引的理由。
    """
    first = store.remember(owner, "提交信息用中文")
    assert store.forget(owner, memory_id=first["id"]) == 1

    again = store.remember(owner, "提交信息用中文")

    assert again["ok"] and again["action"] == "added"
    assert len(store.active(owner)) == 1


def test_prune_on_the_real_table_never_takes_the_correction(store, owner):
    """★ 真表上跑淘汰：纠错记录留着，项目事实先出局。

    一条用例同时证明三件事：卸压第 3 段的物理 ``DELETE`` 在真表上真能删、PG 分支
    与内存版走同一个 ``rank_memories``、**纠错记录靠分数活下来而不是靠白名单**。

    ⚠️ 这条用例的口径跟着 2026-11-03 那轮改过：以前纠错记录是 ``_NEVER_PRUNE``
    整类免死，现在没有类别豁免了，它靠 ``KIND_WEIGHT['feedback'] = 0.8`` 与
    1095 天半衰期在分数上排前面。所以这里既要验"没被删"，也要验"它排在最前"——
    只看"没被删"的话，将来有人把豁免偷偷加回来、或者把分数改歪，用例照样绿。
    """
    import psycopg

    store.remember(owner, "迁移前必须先备份", kind="feedback")
    for i in range(10):
        store.remember(owner, f"事实 {i}", kind="project")

    with psycopg.connect(_DSN, connect_timeout=8) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_memories"
            "   SET created_at = now() - interval '400 days', last_used_at = NULL"
            " WHERE owner_key = %s", (owner,))

    # 同龄（都是 400 天前）时纠错记录必须排在最前 —— 这是"靠分数活"的直接证据。
    # ``active()`` 返回的就是 ``rank_memories`` 排好的顺序，与卸压用的是同一份分数。
    top = store.active(owner, limit=100)[0]
    assert top["content"] == "迁移前必须先备份", "同龄下纠错记录的分数应最高"

    dropped = store.prune(owner, keep=5)

    alive = {r["content"] for r in store.active(owner, limit=100)}
    assert dropped == 6, "11 条 - 5"
    assert "迁移前必须先备份" in alive, "纠错记录不可淘汰"
