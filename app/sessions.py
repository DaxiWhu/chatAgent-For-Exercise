"""会话归属登记：防止猜到 thread_id 后串读他人会话。

解决什么问题
-----------
`thread_id` 是**客户端在请求体里给的**，而检查点按它落库。在多租户模式收紧
租户来源之后，这个字段仍是攻击面：只要猜到（或撞到）别人的 thread_id，就能
带着自己的合法 Key 直接 resume 那条会话，读到别人的对话与待审草稿。

所以需要一层"**会话属主**"：第一次见到某个 thread_id 时登记它属于谁，
之后同一 thread_id 若来自**不同的主体**，直接拒绝。

归属粒度：(tenant, user) —— 2026-10-01 起是**二元组**
--------------------------------------------------
在此之前只有 `tenant`，隐含假设是"同一租户内大家是一伙的"。多用户场景下这个
假设不成立：同租户的 A 与 B 之间没有任何隔离，A 只要知道 B 的 thread_id 就能
读走完整对话、删掉它、甚至替 B 接受待审草稿 —— 隔离边界只画在"组织之间"。

⚠️ 严格语义（刻意为之，不是缺陷）：Key 会话（user 为空）与用户会话（user 非空）
**互不继承** —— 用 API Key 建的会话，用户凭证读不到；反过来也一样。
理由是这两类是**不同主体**，让它们互通等于给"机器凭证"开了一条访问用户数据的后门。

什么时候启用
----------
只有**用户鉴权已启用**（`AUTH_ENABLED`）时才需要它 —— 那时每个请求都有已验签的
用户主体，登记表才有区分能力。未启用时（本地开发、纯脚本调用）所有请求都是
同一属主，强制校验只会平白引入状态与失败路径 ⇒ `enabled=False`。

⚠️ 这里原先还有一个条件 `TENANT_KEYS`（租户由密钥反查得出）。该机制已于
2026-10-04 整体移除 —— 租户现在一律来自 JWT 载荷。

存储
----
配了 `DB_URL` 落 PG（跨重启、多实例一致）；否则内存（单实例、重启丢）。
内存实现**有界**：达到 `MAX_ENTRIES` 后拒绝登记新会话（已登记的仍照常校验），
返回 503 并打一条 error 日志 —— 避免一个长期运行的进程把内存吃光。

⚠️ 失败方向一律 **fail-closed**（2026-10-05 修正，此前是 fail-open）
---------------------------------------------------------------
此前这里写的是"存储异常一律放行，归属校验是旁路保护，不该因数据库抖动把对话
打死"，并把超限时的放行称为"安全降级而非可用性降级"。**那个说法是错的**：
放行意味着**归属校验整个被跳过**，也就是说这条保护最需要生效的时刻（存储抖动、
表打满）恰恰是它失效的时刻 —— 那是安全降级，不是安全降级式的可用性权衡。

同一个类的 `owner_of()`（读路径）一直是 fail-closed 的：读不到属主就
返回 None，调用方据此 404。同一个子系统里，读被拒、写被放行，方向相反；
既然读路径已经证明"拿不到结论就拒绝"是可行的，写路径没有理由反过来。

代价是真实存在的：归属表不可用时**新会话起不来**。所以拒绝时要给出**可行动**的
信息（503 + 原因），并且错误码要区分"结论明确"（403，这会话不是你的）与
"结论拿不到"（503，暂时判不了）—— 把后者伪装成 403 会让客户端以为"这个 id
真的不是我的"，于是放弃或换 id，而正确行为是重试。
"""

import logging
import threading

from . import config


class SessionError(Exception):
    """会话归属层的业务错误（结论拿不到 / 入参非法）。

    与 `AccountError`、`AuthError` 并列的第三类：后两者分别是"这次操作不合法"
    与"你的凭证不管用"，而这里是"**归属判定本身**没能得出结论"。
    单独一类是因为客户端对它的正确处理是**重试**，而不是改参数或重新登录。

    status_code 默认 503（暂时不可用）而不是 403/404：调用方若把"判不了"当成
    "不是你的"，会做出与事实相反的决策。确实要表达"确定不是你的"时，
    `claim()` 仍然走它原有的 `(False, owner)` 返回值，不经过这个异常。
    """

    def __init__(self, detail, *, status_code=503, code="session_error"):
        super().__init__(detail)
        self.detail = detail
        self.status_code = int(status_code)
        self.code = code


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
        self._mem: dict[str, tuple] = {}   # thread_id -> (tenant, user)
        self._lock = threading.Lock()
        self._schema_ready = False
        #: 与 `_schema_ready` 配套：冷启动并发建表的竞态由它挡住，见 `_ensure_schema`
        self._schema_lock = threading.Lock()
        self._overflow_warned = False
        self.backend = "memory"

    # ---------- 存储 ----------
    def _connect(self):
        """借一条**池化** PG 连接（返回上下文管理器：借用 → 事务块 → 归还）。

        见 `app/pg.py` 文件头：原先每次操作都 `psycopg.connect` 新建连接，一次
        `/v1/chat` 要经 2~4 个，于是"并发请求数 = PG 后端进程数"。这里换成共享
        连接池，`with self._connect() as conn:` 的写法与语义都保持不变。

        池不可用时抛 `pg.PoolUnavailable`，由 `claim`/`owner_of` 的 except 转成
        `SessionError(503)` —— 即"判不了"而非"不是你的"（fail-closed）。
        """
        from . import pg

        return pg.connect(self.dsn)

    def _ensure_schema(self, conn):
        """建归属表（幂等）。只在首次使用时建。

        ⚠️ 整段 DDL 必须持锁（2026-10-06 补）：`_schema_ready` 是普通 bool，
        冷启动时多个并发请求会同时读到 False、同时执行
        `CREATE TABLE IF NOT EXISTS` —— 而 PG 在这个语句上有真实竞态，
        并发时可能撞 `pg_type_typname_nsp_index` 唯一索引，
        报错点看起来像"表建坏了"。换成连接池后多个工作线程可同时进来，窗口更宽。
        理由与 `review_queue._ensure_schema` 同一份，详见那里。
        """
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:      # 双检：等锁期间可能已被别人建好
                return
            with conn.cursor() as cur:
                # ⚠️ 列名是 `owner_user` 而不是 `user`：`user` 在 PostgreSQL 里是
                # **保留字**（等价于 `CURRENT_USER`），写成裸列名会让 DDL 直接语法报错，
                # 而报错信息不会告诉你"是名字取错了"。加前缀是唯一稳妥的写法。
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS session_owners ("
                    "  thread_id  text PRIMARY KEY,"
                    "  tenant     text NOT NULL DEFAULT '',"
                    "  owner_user text NOT NULL DEFAULT '',"
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
                # 用户维度（第二档）。默认空串 = "该租户的机器调用方"，
                # 与升级前存的存量行语义一致 —— 它们本来就是 API Key 建的，
                # 因此不会因为这次升级凭空变成"别人读不到"。
                cur.execute(
                    "ALTER TABLE session_owners ADD COLUMN IF NOT EXISTS "
                    "owner_user text NOT NULL DEFAULT ''"
                )
            self._schema_ready = True   # 必须在锁内置位
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
    def claim(self, thread_id: str, tenant: str, user: str = "") -> tuple[bool, tuple]:
        """登记/校验会话属主。

        thread_id: 会话 id。
        tenant: 本次请求的租户（来自鉴权，可信）。
        user: 本次请求的用户主体（第二档，来自已验签凭证）。API Key 路径传空串。

        返回 `(是否允许, 实际属主)`，属主是 `(tenant, user)` 元组。
        **首次见到即认领**；已存在且属主不同 → False。

        ⚠️ 三种"拿不到结论"的情况一律抛 `SessionError`（**不放行**）：
        入参 thread_id 为空、存储异常、内存表已满。理由见模块 docstring ——
        放行等于跳过校验，而放行时最危险的地方是"你根本不知道属主是谁"。

        空 `thread_id` 单独拒绝（而不是让它落到存储层）：检查点按 thread_id 落库，
        空 id 意味着所有客户端**共用同一个空 key 的会话空间**，互相能读到对方的
        上下文。请求层已有非空校验，这里再挡一次是纵深防御。
        """
        me = (tenant or "", user or "")
        thread_id = (thread_id or "").strip()
        if not self.enabled:
            return True, me
        if not thread_id:
            raise SessionError(
                "thread_id 不能为空", status_code=400, code="empty_thread_id")

        if self.dsn:
            try:
                with self._connect() as conn:
                    self._ensure_schema(conn)
                    with conn.cursor() as cur:
                        # 先尝试认领（已存在则什么都不做），再读出真实属主 ——
                        # 两步之间不需要事务：并发下两者都会得到同一个"先到者"。
                        cur.execute(
                            "INSERT INTO session_owners (thread_id, tenant, owner_user) "
                            "VALUES (%s, %s, %s) ON CONFLICT (thread_id) DO NOTHING",
                            (thread_id, me[0], me[1]),
                        )
                        # 刷新"最后活动时间"。数据保留脚本按它判超期 ——
                        # 若按 `created_at`，一个创建于 100 天前、今天还在用的会话
                        # 会被清掉，那是不可接受的破坏（对话还在继续）。
                        #
                        # ⚠️ 条件必须是**完整归属**（tenant + owner_user），不能只写
                        # tenant：否则同租户的**另一个用户**就能拿别人的 thread_id
                        # 去刷新时间戳，把这个 id 永久钉在保留期内 ——
                        # 等价于让攻击者掌握清理的开关。
                        cur.execute(
                            "UPDATE session_owners SET updated_at = now() "
                            "WHERE thread_id = %s AND tenant = %s AND owner_user = %s",
                            (thread_id, me[0], me[1]),
                        )
                        cur.execute(
                            "SELECT tenant, owner_user FROM session_owners "
                            "WHERE thread_id = %s",
                            (thread_id,),
                        )
                        row = cur.fetchone()
                owner = (row[0], row[1]) if row else me
                return owner == me, owner
            except SessionError:
                raise
            except Exception as e:  # noqa: BLE001
                # 判不了 ⇒ 拒绝，并且必须是 503（可重试）而不是 403（结论明确）。
                logging.error("会话归属校验不可用（存储异常），拒绝本次请求：%s", e)
                raise SessionError(
                    "会话归属校验暂时不可用，请稍后重试",
                    status_code=503, code="session_store_unavailable") from e

        with self._lock:
            owner = self._mem.get(thread_id)
            if owner is None:
                if len(self._mem) >= self.MAX_ENTRIES:
                    if not self._overflow_warned:
                        self._overflow_warned = True
                        logging.error(
                            "会话归属表已达上限 %d，拒绝登记新会话（已登记的仍校验）；"
                            "多实例/大批量场景请配置 DB_URL 落库",
                            self.MAX_ENTRIES,
                        )
                    raise SessionError(
                        "会话归属表已满，暂时无法开始新会话（请联系运维）",
                        status_code=503, code="session_registry_full")
                self._mem[thread_id] = me
                return True, me
            return owner == me, owner

    def owner_of(self, thread_id: str):
        """读会话属主；**未登记**返回 None（供排障与测试）。

        返回 **(tenant, user)** 元组 —— **不返回拼接好的字符串**。
        拼接（如 `f"{tenant}/{user}"`）会让 `tenant="a/b", user=""` 与
        `tenant="a", user="b"` 得到同一个值，从而把两个不同主体判成同一个 ——
        而这类"用字符串拼接做权限比较"的缺陷不会有任何运行时症状。

        ⚠️ "未登记"（None）与"读不出来"（`SessionError`）**必须分开**：
        前者是结论（这个 id 不存在 ⇒ 调用方 404），后者是**没有结论**。
        早先两者都返回 None，于是存储抖动被伪装成"会话不存在"，把排障引向
        错误方向（去查"这个会话是不是被删了"，而真实原因是数据库连不上）。
        """
        thread_id = (thread_id or "").strip()
        if not self.enabled or not thread_id:
            return None
        if self.dsn:
            try:
                with self._connect() as conn:
                    self._ensure_schema(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT tenant, owner_user FROM session_owners "
                            "WHERE thread_id = %s", (thread_id,))
                        row = cur.fetchone()
                return (row[0], row[1]) if row else None
            except Exception as e:  # noqa: BLE001
                logging.error("会话属主读取失败（无法判断归属）：%s", e)
                raise SessionError(
                    "会话归属校验暂时不可用，请稍后重试",
                    status_code=503, code="session_store_unavailable") from e
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

    启用条件：**用户鉴权已启用**（`AUTH_ENABLED`）—— 那时每个请求都有已验签的
    用户主体，登记表才有区分能力。这同时也是"同一租户里 A 与 B 也要隔离"的前提。

    未启用时（本地开发、纯脚本调用）所有请求同属主，校验没有区分能力，
    开着只会平白引入状态与失败路径。

    ⚠️ 这里原先还有一个条件 `TENANT_KEYS`（租户由密钥反查得出）。该机制已于
    2026-10-04 整体移除，租户现在一律来自 JWT 载荷。
    """
    return SessionRegistry(
        dsn=config.DB_URL,
        enabled=bool(config.AUTH_ENABLED),
    )
