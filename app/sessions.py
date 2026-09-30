"""会话归属登记：防止猜到 thread_id 后串读他人会话。

解决什么问题
-----------
`thread_id` 是**客户端在请求体里给的**，而检查点按它落库。在多租户模式收紧
租户来源之后，这个字段仍是攻击面：只要猜到（或撞到）别人的 thread_id，就能
带着自己的合法 Key 直接 resume 那条会话，读到别人的对话与待审草稿。

所以需要一层"**会话属主**"：第一次见到某个 thread_id 时登记它属于哪个租户，
之后同一 thread_id 若来自**不同租户**，直接拒绝。

只在多租户模式下强制
------------------
未配置 `TENANT_KEYS` 时（单租户或本地开发）所有请求都是同一属主，
登记表不具备区分能力，强制校验只会平白引入状态与失败路径 ⇒ `enabled=False`。

存储
----
配了 `DB_URL` 落 PG（跨重启、多实例一致）；否则内存（单实例、重启丢）。
内存实现**有界**：达到 `MAX_ENTRIES` 后不再登记新会话（仍校验已登记的），
并打一条告警 —— 避免一个长期运行的进程把内存吃光。此时退化为
"best-effort 校验"，不阻断服务（安全降级而非可用性降级）。
"""

import logging
import threading

from . import config


class SessionRegistry:
    """会话属主登记表：`claim()` 登记并校验。"""

    #: 内存模式下最多登记多少条会话（PG 模式不受此限）
    MAX_ENTRIES = 50000

    def __init__(self, dsn="", enabled=True):
        """构造。

        dsn: DB_URL；空则用内存存储。
        enabled: False 时 claim 恒放行（不登记）—— 单租户模式用。
        """
        self.enabled = bool(enabled)
        self.dsn = (dsn or "").strip()
        self._mem: dict[str, str] = {}
        self._lock = threading.Lock()
        self._schema_ready = False
        self._overflow_warned = False
        self.backend = "memory"

    # ---------- 存储 ----------
    def _connect(self):
        """建 PG 连接（延迟导入 psycopg，未装则抛 ImportError 由调用方降级）。"""
        import psycopg

        return psycopg.connect(self.dsn, connect_timeout=5, autocommit=True)

    def _ensure_schema(self, conn):
        """建归属表（幂等）。只在首次使用时建。"""
        if self._schema_ready:
            return
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS session_owners ("
                "  thread_id  text PRIMARY KEY,"
                "  tenant     text NOT NULL DEFAULT '',"
                "  created_at timestamptz NOT NULL DEFAULT now(),"
                "  updated_at timestamptz NOT NULL DEFAULT now()"
                ")"
            )
            # 老表补列。**必须有**：数据保留脚本按 `updated_at` 判超期，
            # 少这一列的旧库会让脚本直接报错（或更糟：漏掉整表不清理）。
            # 补列时默认值取 now() —— 让存量行从"此刻"重新计时，
            # 而不是一次性全部变成超期（那会在升级后立刻删掉所有历史会话）。
            cur.execute(
                "ALTER TABLE session_owners ADD COLUMN IF NOT EXISTS "
                "updated_at timestamptz NOT NULL DEFAULT now()"
            )
        self._schema_ready = True
        self.backend = "postgres"

    def ensure_schema(self):
        """确保归属表存在（幂等）。供部署脚本/迁移一次性建齐所有表。

        与 `claim()` 首次使用时自动建表复用**同一份 DDL**（`_ensure_schema`）。
        无 dsn（内存后端）时不做事，返回 False。
        """
        if not self.dsn:
            return False
        with self._connect() as conn:
            self._ensure_schema(conn)
        return True

    # ---------- 对外 ----------
    def claim(self, thread_id: str, tenant: str) -> tuple[bool, str]:
        """登记/校验会话属主。

        thread_id: 会话 id。
        tenant: 本次请求的租户（来自鉴权，可信）。

        返回 (是否允许, 实际属主)。**首次见到即认领**；已存在且属主不同 → False。
        存储异常一律**放行**：归属校验是旁路保护，不该因数据库抖动把对话打死。
        """
        thread_id = (thread_id or "").strip()
        if not self.enabled or not thread_id:
            return True, tenant

        if self.dsn:
            try:
                with self._connect() as conn:
                    self._ensure_schema(conn)
                    with conn.cursor() as cur:
                        # 先尝试认领（已存在则什么都不做），再读出真实属主 ——
                        # 两步之间不需要事务：并发下两者都会得到同一个"先到者"。
                        cur.execute(
                            "INSERT INTO session_owners (thread_id, tenant) VALUES (%s, %s) "
                            "ON CONFLICT (thread_id) DO NOTHING",
                            (thread_id, tenant),
                        )
                        # 刷新"最后活动时间"。数据保留脚本按它判超期 ——
                        # 若按 `created_at`，一个创建于 100 天前、今天还在用的会话
                        # 会被清掉，那是不可接受的破坏（对话还在继续）。
                        #
                        # ⚠️ `AND tenant = %s` 不能省：否则任何人都能用**自己**的
                        # tenant 去"刷新"别人的会话时间戳，把这个 id 永久钉在
                        # 保留期内 —— 等价于让攻击者掌握清理的开关。
                        cur.execute(
                            "UPDATE session_owners SET updated_at = now() "
                            "WHERE thread_id = %s AND tenant = %s",
                            (thread_id, tenant),
                        )
                        cur.execute(
                            "SELECT tenant FROM session_owners WHERE thread_id = %s",
                            (thread_id,),
                        )
                        row = cur.fetchone()
                owner = row[0] if row else tenant
                return owner == tenant, owner
            except Exception as e:  # noqa: BLE001
                logging.warning("会话归属校验失败，本次放行：%s", e)
                return True, tenant

        with self._lock:
            owner = self._mem.get(thread_id)
            if owner is None:
                if len(self._mem) >= self.MAX_ENTRIES:
                    if not self._overflow_warned:
                        self._overflow_warned = True
                        logging.warning(
                            "会话归属表已达上限 %d，停止登记新会话（已登记的仍校验）；"
                            "多实例/大批量场景请配置 DB_URL 落库",
                            self.MAX_ENTRIES,
                        )
                    return True, tenant
                self._mem[thread_id] = tenant
                return True, tenant
            return owner == tenant, owner

    def owner_of(self, thread_id: str):
        """读会话属主；未登记返回 None（供排障与测试）。"""
        thread_id = (thread_id or "").strip()
        if not self.enabled or not thread_id:
            return None
        if self.dsn:
            try:
                with self._connect() as conn:
                    self._ensure_schema(conn)
                    with conn.cursor() as cur:
                        cur.execute("SELECT tenant FROM session_owners WHERE thread_id = %s",
                                    (thread_id,))
                        row = cur.fetchone()
                return row[0] if row else None
            except Exception as e:  # noqa: BLE001
                logging.warning("会话属主读取失败：%s", e)
                return None
        with self._lock:
            return self._mem.get(thread_id)

    def release(self, thread_id: str) -> bool:
        """注销会话归属（会话被删除时调用）。

        ⚠️ 这是**唯一**会让"已登记的 thread_id"重新变成"未登记"的操作，
        存在的理由是履行删除请求。只删检查点而留着归属行会留下两个问题：

          - 归属行永久占位（表只增不减），`owner_of` 还会继续回答"这属于某人"；
          - 同一个 thread_id 之后再也无法被使用 —— 新租户拿来会被判 403，
            而它其实已经不存在了。

        返回是否真的删掉了一条（未登记时返回 False，不视为错误）。
        存储异常**不上抛**：调用方是删除流程，不该因为归属表抖动而整单失败 ——
        真正的数据（检查点）删掉才是关键，归属行是附带清理。
        """
        thread_id = (thread_id or "").strip()
        if not self.enabled or not thread_id:
            return False
        if self.dsn:
            try:
                with self._connect() as conn:
                    self._ensure_schema(conn)
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM session_owners WHERE thread_id = %s",
                                    (thread_id,))
                        return cur.rowcount > 0
            except Exception as e:  # noqa: BLE001
                logging.warning("会话归属注销失败（不影响检查点删除）：%s", e)
                return False
        with self._lock:
            return self._mem.pop(thread_id, None) is not None

    def count(self) -> int:
        """已登记会话数（内存模式；供 /metrics 或排障）。"""
        with self._lock:
            return len(self._mem)


def build_session_registry() -> SessionRegistry:
    """按 app.config 造会话登记表。

    **只在多租户（TENANT_KEYS）模式下启用** —— 单租户下所有请求同属主，
    校验没有区分能力，开着只增加状态。
    """
    return SessionRegistry(
        dsn=config.DB_URL,
        enabled=bool(config.TENANT_KEYS),
    )
