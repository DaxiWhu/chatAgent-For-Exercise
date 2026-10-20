"""被打断的运行现场：官方 ``RunState`` 的持久化。

为什么需要它
------------
官方对"一次运行停在中途"只给一个可序列化对象：``RunState``。它的 docstring 原文是
"the durable pause/resume boundary for human-in-the-loop flows" —— 停机与恢复
**是它**，不是我们自己的检查点。``result.to_state().to_string(...)`` 得到 JSON，
``RunState.from_string(...)`` 取回来，``state.approve(...)`` / ``state.reject(...)``
处理掉待批项，再 ``Runner.run(agent, state)`` 接着跑。

官方**不负责存**：进程重启之后那份现场在哪儿，是应用自己的事（``human_in_the_loop.md``
明确写了生产应用必须自己提供恢复）。所以这里只做一件事：把它落库并取回来。

什么时候会有现场
----------------
只有"运行中产生了 interruption"才有 —— 也就是模型请求了一个需要审批的工具：
交付前的自审（``deliver(needs_review=True)``）与落盘类工具（生成图片、写资产）。
**运行正常跑完的轮次没有现场**，也不该有：那类请求的结论（跑偏 / 护栏拦下）
是运行**结束后**才判出来的，已经没有任何"可以停下来的地方"可回退。

⚠️ 一份现场**只能用一次**：官方那个状态消费掉批准结果之后就作废了。所以
``load()`` 之后调用方必须 ``drop()``，否则同一份现场会被重复恢复 ——
症状是"批了一次，同一个动作执行了两遍"，而且不报错。
"""

from __future__ import annotations

import logging
import threading
import time

from . import config

__all__ = ["RunStore", "build_run_store"]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class RunStore:
    """被打断运行现场的存取。配了 ``DB_URL`` 落 PG，否则内存。

    两种后端暴露同一组方法，调用方无感。写库失败自动降级内存并告警 ——
    "这次能不能接着跑"比"存在哪个介质上"重要。
    """

    def __init__(self, dsn: str = "", enabled: bool = True):
        self.dsn = (dsn or "").strip()
        self.enabled = bool(enabled)
        self._mem: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._schema_ready = False
        self._schema_lock = threading.Lock()
        self.backend = "memory"

    # ---------- 存储 ----------
    def _connect(self):
        from . import pg

        return pg.connect(self.dsn)

    def _ensure_schema(self, conn) -> None:
        """建表（幂等）。只在首次写入时建，避免每次请求都发 DDL。

        ⚠️ 标志与锁必须配套：冷启动时多个并发请求会同时读到 ``False``，
        于是同时执行 ``CREATE TABLE IF NOT EXISTS`` —— PG 在这个语句上有真实竞态
        （两个事务同时通过存在性检查、再同时插元组，后者撞唯一索引）。
        症状看起来像"表建坏了"，实际是并发建表。
        """
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS agent_run_state ("
                    "  thread_id  text PRIMARY KEY,"
                    "  tenant     text NOT NULL DEFAULT '',"
                    "  snapshot   text NOT NULL,"
                    "  draft      text,"
                    "  reason     text NOT NULL DEFAULT '',"
                    "  created_at timestamptz NOT NULL DEFAULT now(),"
                    "  updated_at timestamptz NOT NULL DEFAULT now()"
                    ")"
                )
            self._schema_ready = True
            self.backend = "postgres"

    def ensure_schema(self) -> bool:
        """确保表存在（部署脚本/迁移用）。无 dsn 时返回 False。"""
        if not self.dsn:
            return False
        with self._connect() as conn:
            self._ensure_schema(conn)
        return True

    # ---------- 读写 ----------
    def save(self, thread_id: str, *, snapshot: str, draft: str = "",
             reason: str = "", tenant: str = "") -> bool:
        """存一份现场。同一会话**覆盖**旧的：一条会话同时只可能有一轮等着被恢复。"""
        if not self.enabled or not thread_id or not snapshot:
            return False
        item = {
            "thread_id": thread_id, "tenant": tenant or "",
            "snapshot": snapshot, "draft": draft or "", "reason": reason or "",
            "created_at": _now_iso(),
        }
        if self.dsn:
            try:
                with self._connect() as conn:
                    self._ensure_schema(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO agent_run_state "
                            "(thread_id, tenant, snapshot, draft, reason) "
                            "VALUES (%s,%s,%s,%s,%s) "
                            "ON CONFLICT (thread_id) DO UPDATE SET "
                            "  tenant=EXCLUDED.tenant, snapshot=EXCLUDED.snapshot, "
                            "  draft=EXCLUDED.draft, reason=EXCLUDED.reason, "
                            "  updated_at=now()",
                            (thread_id, item["tenant"], item["snapshot"],
                             item["draft"], item["reason"]),
                        )
                return True
            except Exception as e:  # noqa: BLE001  降级内存，不影响对话
                logging.warning("运行现场写库失败，降级内存：%s", e)
        with self._lock:
            self._mem[thread_id] = item
        return True

    def load(self, thread_id: str) -> dict | None:
        """取一份现场；不存在返回 ``None``。"""
        if not self.enabled or not thread_id:
            return None
        if self.dsn:
            try:
                with self._connect() as conn:
                    self._ensure_schema(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT thread_id, tenant, snapshot, draft, reason, created_at "
                            "FROM agent_run_state WHERE thread_id=%s",
                            (thread_id,),
                        )
                        row = cur.fetchone()
                if row is None:
                    return None
                return {"thread_id": row[0], "tenant": row[1], "snapshot": row[2],
                        "draft": row[3], "reason": row[4],
                        "created_at": str(row[5])}
            except Exception as e:  # noqa: BLE001
                logging.warning("运行现场查询失败，回退内存：%s", e)
        with self._lock:
            row = self._mem.get(thread_id)
            return dict(row) if row is not None else None

    def drop(self, thread_id: str) -> bool:
        """删掉一份现场。**恢复之后必须调用** —— 现场只能用一次（见模块 docstring）。"""
        if not self.enabled or not thread_id:
            return False
        if self.dsn:
            try:
                with self._connect() as conn:
                    self._ensure_schema(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM agent_run_state WHERE thread_id=%s",
                            (thread_id,),
                        )
                        return cur.rowcount > 0
            except Exception as e:  # noqa: BLE001
                logging.warning("运行现场删除失败：%s", e)
        with self._lock:
            return self._mem.pop(thread_id, None) is not None


def build_run_store() -> RunStore:
    """按 ``app.config`` 造一个现场仓库（启动时调用一次）。"""
    return RunStore(dsn=config.DB_URL, enabled=config.REVIEW_QUEUE_ENABLED)
