"""压缩产物的存放：**"覆盖了库里前多少条 + 那一段的摘要"**，而不是去改会话表。

为什么要有这一层
----------------
2026-11-02 之前，历史压缩走的是"清空会话、再写回一条摘要 + 尾部"。那一步**真的
把库里的原始消息删了**，三个后果都无法补救：

1. 摘要丢掉的细节永久消失（摘要写得再好也只是有损压缩）；
2. 长期记忆失去素材 —— 它要从对话里提炼稳定事实，而原件已经不在了；
3. 答不出"当时到底是怎么说的"，也无法回滚。

改成"日志只追加 + 读取时投影"之后，原始消息一条都不动，压缩只**追加**一条记录，
说清"哪一段已经被这段摘要取代"。投影（``compaction.CompactingSession.get_items``）
按这条记录算出这一轮该看到什么。

为什么记录也要"只追加"
----------------------
一条记录被新的一条取代时，旧行不删，只把状态改成 ``superseded`` 并写 ``invalid_at``。
理由与原始消息同源：**以后要能回答"某一轮当时看到的是哪一版摘要"**。
删掉就永远只有最新一版，而滚动压缩的行为差异恰恰要靠历代摘要来复现。

``covered_items`` 的前提
------------------------
它是"**库里前多少条**"，不是消息 id。理由：官方表虽然有自增 id，但读它要直接查表、
等于把 SDK 的存储格式当契约；而"前 N 条"只用公开接口就能算 —— 库里只追加，
**前 N 条的身份永远不变**。

⚠️ 这个前提要守住：一旦出现"按单条删除"（现在的保留策略是按会话整删，不冲突），
第 N 条之后的全部位置都会前移，``covered_items`` 立刻失真，投影就会**少显示或多显示**
一段历史。守卫见 ``tests/test_compaction_projection.py``。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

__all__ = [
    "SCHEMA_STATEMENTS",
    "InMemoryCompactionStore",
    "PgCompactionStore",
    "make_store",
]

#: 建表语句。DDL 出处在这里，`app/migrations.py` 只引用不重写（与 `RunControl` 同口径）。
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS agent_history_compactions (
        id               text PRIMARY KEY,
        thread_id        text NOT NULL,
        covered_items    integer NOT NULL,
        summary          text NOT NULL,
        summary_tokens   integer NOT NULL DEFAULT 0,
        shadowed_tokens  integer NOT NULL DEFAULT 0,
        scope            text NOT NULL DEFAULT 'history',
        status           text NOT NULL DEFAULT 'active',
        invalid_at       timestamptz,
        superseded_by    text,
        created_at       timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS agent_history_compactions_thread
        ON agent_history_compactions (thread_id, created_at DESC)
    """,
    # 同一会话同一 scope 同时只能有一条生效记录。做成**部分**索引是必须的：
    # 被取代的历史记录要一直留着，全表唯一会让第二次压缩插不进去。
    """
    CREATE UNIQUE INDEX IF NOT EXISTS agent_history_compactions_active
        ON agent_history_compactions (thread_id, scope) WHERE status = 'active'
    """,
)


class InMemoryCompactionStore:
    """无数据库时的实现（测试、以及没配 ``DB_URL`` 的降级路径）。

    语义与 PG 版一致：只追加、旧行标 superseded、按会话取最新一条 active。
    刻意**不做进程间共享** —— 它存在的意义是"能跑"，不是"多副本一致"。
    """

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    async def latest_active(self, thread_id: str, scope: str = "history") -> dict | None:
        for row in reversed(self._rows):
            if (row["thread_id"] == thread_id and row["scope"] == scope
                    and row["status"] == "active"):
                return dict(row)
        return None

    async def record(self, thread_id: str, *, covered_items: int, summary: str,
                     summary_tokens: int = 0, shadowed_tokens: int = 0,
                     scope: str = "history") -> str:
        new_id = uuid.uuid4().hex
        async with self._lock:
            for row in self._rows:
                if (row["thread_id"] == thread_id and row["scope"] == scope
                        and row["status"] == "active"):
                    row["status"] = "superseded"
                    row["superseded_by"] = new_id
            self._rows.append({
                "id": new_id,
                "thread_id": thread_id,
                "covered_items": int(covered_items),
                "summary": summary,
                "summary_tokens": int(summary_tokens),
                "shadowed_tokens": int(shadowed_tokens),
                "scope": scope,
                "status": "active",
                "superseded_by": None,
            })
        return new_id

    def rows(self) -> list[dict[str, Any]]:
        """全部记录（含已取代的）—— **只给测试与排查用**，不要拿来投影。"""
        return [dict(r) for r in self._rows]

    def reset(self) -> None:
        self._rows.clear()


class PgCompactionStore:
    """PG 实现。复用会话后端那一个 **async engine**，不另建连接池。

    为什么复用：投影在 ``get_items`` 的路径上，每次读历史都要查一条记录。另建一个
    连接池意味着同一份数据两条连接路径、两套超时、两套故障表现 —— 排查时最费时间的
    就是"两个池里一个坏了"。
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def latest_active(self, thread_id: str, scope: str = "history") -> dict | None:
        from sqlalchemy import text

        async with self._engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT id, covered_items, summary, summary_tokens, shadowed_tokens,"
                "       scope, status"
                "  FROM agent_history_compactions"
                " WHERE thread_id = :t AND scope = :s AND status = 'active'"
                " ORDER BY created_at DESC LIMIT 1"
            ), {"t": thread_id, "s": scope})).first()
        if row is None:
            return None
        return {
            "id": row[0], "covered_items": int(row[1]), "summary": row[2] or "",
            "summary_tokens": int(row[3] or 0), "shadowed_tokens": int(row[4] or 0),
            "scope": row[5], "status": row[6],
        }

    async def record(self, thread_id: str, *, covered_items: int, summary: str,
                     summary_tokens: int = 0, shadowed_tokens: int = 0,
                     scope: str = "history") -> str:
        from sqlalchemy import text

        new_id = uuid.uuid4().hex
        # 两条语句**必须同一事务**：先失效旧的、再插新的。分开提交的中间态是
        # "一条 active 都没有" —— 投影这时会退回全量历史，表现成"压缩忽然不生效了"，
        # 而唯一索引也会在那几百毫秒里允许插入第二条 active。
        async with self._engine.begin() as conn:
            await conn.execute(text(
                "UPDATE agent_history_compactions"
                "   SET status = 'superseded', invalid_at = now(), superseded_by = :new"
                " WHERE thread_id = :t AND scope = :s AND status = 'active'"
            ), {"new": new_id, "t": thread_id, "s": scope})
            await conn.execute(text(
                "INSERT INTO agent_history_compactions"
                " (id, thread_id, covered_items, summary, summary_tokens,"
                "  shadowed_tokens, scope, status)"
                " VALUES (:id, :t, :n, :s, :st, :sh, :sc, 'active')"
            ), {
                "id": new_id, "t": thread_id, "n": int(covered_items),
                "s": summary, "st": int(summary_tokens),
                "sh": int(shadowed_tokens), "sc": scope,
            })
        return new_id


def make_store(engine: Any = None) -> Any:
    """按有没有 engine 选实现。没有引擎 = 没有库 = 内存版（与今天的降级同一条路）。"""
    if engine is None:
        return InMemoryCompactionStore()
    try:
        return PgCompactionStore(engine)
    except Exception as e:  # noqa: BLE001
        logging.warning("压缩记录用不了 PG（%s）⇒ 退回进程内存储", e)
        return InMemoryCompactionStore()
