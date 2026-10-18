"""运行控制：登记"当前有哪些任务在跑"，并承接"停下"的请求。

为什么需要一张表，而不是一个进程内字典
------------------------------------
停下是**跨副本**的：用户的 `/stop` 请求可能落在 A 副本，而任务跑在 B 副本。
进程内字典在那种情况下会返回"没有正在运行的任务"，而它其实正在跑 —— 这是最难
排查的一类错误（用户说"点了没反应"，日志里一切正常）。

所以：**登记与停止请求都落库**，执行侧轮询。单副本部署下 `dsn` 为空，退回内存表，
行为一致（只是跨副本不生效，而单副本本来就没有跨副本）。

为什么按 ``run_id`` 而不是按 ``thread_id`` 判定"能不能停"
----------------------------------------------------
按 thread 会带一个无法消除的竞态：任务刚跑完、停止请求才到 ⇒ 标记留在库里
⇒ **下一轮一开始就被停**。用户看到的是"刚发出去就被打断"，而且没有任何线索。
``run_id`` 把"停哪一次"钉死：run 结束即删行，请求到达时找不到 ⇒ 明确回
"当前没有正在运行的任务"。

为什么不复用检查点 / 审核队列
--------------------------
那两个回答的是"内容进行到哪了"，本模块回答的是"这次执行还在不在"。生命周期
也完全不同：run 结束就要消失（否则就是垃圾），而检查点是长期数据。
"""

from __future__ import annotations

import logging
import threading

from . import metrics

__all__ = ["RunControl", "build_run_control"]

#: 表名（迁移与本模块必须一致）。
TABLE = "agent_runs"


class RunControl:
    """运行登记与停止请求。``dsn`` 为空 ⇒ 内存表（单副本/测试）。"""

    def __init__(self, dsn: str = "", enabled: bool = True):
        self.dsn = (dsn or "").strip()
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        #: run_id -> {"thread_id", "tenant", "stopped", "by"}
        self._runs: dict[str, dict] = {}

    # -- 后端信息 ---------------------------------------------------------- #
    @property
    def backend(self) -> str:
        """``postgres`` / ``memory`` / ``disabled``。

        ⚠️ 与限流、会话历史同一条口径：**配了 DSN 不等于后端可用**。这里只回显
        "打算用哪个"，真正的可用性由第一次调用暴露（并在日志里说明）。
        """
        if not self.enabled:
            return "disabled"
        return "postgres" if self.dsn else "memory"

    # -- DDL（迁移用）------------------------------------------------------ #
    def schema_statements(self) -> list[str]:
        """建表语句（幂等）。迁移模块与 ``ensure_schema`` 共用这一份。"""
        return [
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
              run_id TEXT PRIMARY KEY,
              thread_id TEXT NOT NULL,
              tenant TEXT NOT NULL DEFAULT '',
              started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              stop_requested_at TIMESTAMPTZ,
              stop_requested_by TEXT NOT NULL DEFAULT ''
            )
            """,
            f"""
            CREATE INDEX IF NOT EXISTS {TABLE}_thread_running
              ON {TABLE} (thread_id)
              WHERE stop_requested_at IS NULL
            """,
        ]

    def ensure_schema(self) -> None:
        """建表（幂等，只在配了 DSN 时有意义）。"""
        if not self.dsn:
            return
        from . import pg

        with pg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                for statement in self.schema_statements():
                    cur.execute(statement)

    # -- 登记 -------------------------------------------------------------- #
    def begin(self, run_id: str, thread_id: str, tenant: str = "") -> None:
        """登记一次运行开始。失败**只记日志不抛** —— 登记不上不该让请求失败。

        为什么降级而不是抛：登记只影响"能不能被停"，不影响"能不能跑"。为了一个
        可选能力把整个对话打死，是把内部故障升级成用户可见的失败。
        """
        if not self.enabled or not run_id:
            return
        if not self.dsn:
            with self._lock:
                self._runs[run_id] = {"thread_id": thread_id, "tenant": tenant,
                                      "stopped": False, "by": ""}
            return
        try:
            from . import pg

            with pg.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO {TABLE} (run_id, thread_id, tenant) "
                        "VALUES (%s, %s, %s) ON CONFLICT (run_id) DO NOTHING",
                        (run_id, thread_id, tenant),
                    )
        except Exception:  # noqa: BLE001
            metrics.inc("run_control_errors_total", op="begin")
            logging.warning("运行登记失败（本次运行将无法被主动停止）：run=%s", run_id,
                            exc_info=True)

    def finish(self, run_id: str) -> None:
        """运行结束：删掉登记行。

        ⚠️ 必须删而不是标记 —— 留下的行会让同一个 thread 的下一次运行"看起来
        已经在跑"。删了之后 `/stop` 找不到 ⇒ 明确回 409，而不是误停下一轮。
        """
        if not self.enabled or not run_id:
            return
        if not self.dsn:
            with self._lock:
                self._runs.pop(run_id, None)
            return
        try:
            from . import pg

            with pg.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {TABLE} WHERE run_id = %s", (run_id,))
        except Exception:  # noqa: BLE001
            metrics.inc("run_control_errors_total", op="finish")
            logging.warning("运行登记清理失败（该行会残留到手动清理）：run=%s", run_id,
                            exc_info=True)

    # -- 停止 -------------------------------------------------------------- #
    def request_stop(self, thread_id: str, actor: str = "") -> str | None:
        """给该会话**最新那次仍在跑**的运行打一个停止标记，返回 run_id。

        返回 ``None`` = 没有正在运行的任务（调用方应回 409）。这个结论必须来自
        **存储**而不是"本地有没有登记"：跨副本时本地什么都没有。

        ⚠️ 重复请求**幂等**：已经请求过停止、但还没结束的那次运行，仍然返回同一个
        ``run_id``（而不是 409/None）。否则客户端重复点一次会以为"这次没停成"，
        于是再弹一个重试/取消的对话框 —— 而它其实早就在停了。
        """
        if not self.enabled:
            return None
        tid = (thread_id or "").strip()
        if not tid:
            return None
        if not self.dsn:
            with self._lock:
                candidates = [(rid, r) for rid, r in self._runs.items()
                              if r["thread_id"] == tid]
                if not candidates:
                    return None
                # dict 保持插入顺序 ⇒ 最后一个就是最新的那次
                rid, row = candidates[-1]
                # 已经请求过停止就**原样返回同一个 run_id**：重复点"停止"是幂等的，
                # 回 409 会让客户端以为"这次没停成"，于是弹出重试/取消的选项。
                if not row["stopped"]:
                    row["stopped"] = True
                    row["by"] = actor
                return rid
        try:
            from . import pg

            with pg.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE {TABLE} SET stop_requested_at = now(), "
                        "stop_requested_by = %s WHERE run_id = ("
                        f"  SELECT run_id FROM {TABLE} "
                        "   WHERE thread_id = %s AND stop_requested_at IS NULL "
                        "   ORDER BY started_at DESC LIMIT 1"
                        ") RETURNING run_id",
                        (actor, tid),
                    )
                    row = cur.fetchone()
                    if row:
                        return row[0]
                    # 没找到"还没停过的" ⇒ 可能是重复请求。只要这个 thread 还有
                    # 登记行，就说明它仍在跑，返回那一行让接口保持幂等。
                    cur.execute(
                        f"SELECT run_id FROM {TABLE} WHERE thread_id = %s "
                        "ORDER BY started_at DESC LIMIT 1",
                        (tid,),
                    )
                    row = cur.fetchone()
                    return row[0] if row else None
        except Exception:  # noqa: BLE001
            metrics.inc("run_control_errors_total", op="request_stop")
            logging.warning("停止请求写入失败：session=%s", tid, exc_info=True)
            return None

    def stop_requested(self, run_id: str) -> bool:
        """这次运行是否已被要求停止。**执行侧高频调用**（跨副本时才会走库）。"""
        if not self.enabled or not run_id:
            return False
        if not self.dsn:
            with self._lock:
                row = self._runs.get(run_id)
                return bool(row and row["stopped"])
        try:
            from . import pg

            with pg.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT stop_requested_at IS NOT NULL FROM {TABLE} WHERE run_id = %s",
                        (run_id,),
                    )
                    row = cur.fetchone()
                    # 查不到 = 这一行已经不在了（运行刚结束）⇒ 不该再停
                    return bool(row and row[0])
        except Exception:  # noqa: BLE001  查询失败时不误停
            metrics.inc("run_control_errors_total", op="stop_requested")
            logging.debug("停止状态查询失败（按未停止处理）：run=%s", run_id, exc_info=True)
            return False

    def active_count(self) -> int:
        """当前登记在跑的运行数（健康检查/测试用，不参与业务判定）。"""
        if not self.enabled:
            return 0
        if not self.dsn:
            with self._lock:
                return len(self._runs)
        try:
            from . import pg

            with pg.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT count(*) FROM {TABLE}")
                    row = cur.fetchone()
                    return int(row[0]) if row else 0
        except Exception:  # noqa: BLE001
            return 0

    # -- 测试用 ------------------------------------------------------------ #
    def reset(self) -> None:
        """清空内存表（**测试用**）。"""
        with self._lock:
            self._runs.clear()


def build_run_control() -> RunControl:
    """按配置装配（与 `build_review_queue` 同一条口径：读的是环境，不是全局单例）。"""
    from . import config

    return RunControl(dsn=config.DB_URL, enabled=True)
