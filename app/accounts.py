"""账号体系存储层：租户表、用户表、加入申请表。

解决什么问题
-----------
在这之前，"用户"是 `.env` 里 `AUTH_USERS` 的一行文本，"租户"只是一个字符串标签
（连实体都不是）。两者的共同点是：**进程启动时定死**。后果有两层 ——

  1. 加一个人 / 加一个租户要改配置，而配置模块是模块级求值，改文件对运行中的
     进程零效果，只能重启；
  2. 重启会把所有在线用户踢下线（会话白名单若在进程内存、且配额计量也在内存，
     当日已用量还会一起清零）。

"运行时增删改查"这件事在配置文件的形态下**做不到**，所以必须有真正的表。

数据模型（2026-10-04 定稿，用户口径）
------------------------------------
    租户 tenants                 name（主键）、status、note
    用户 users                   username（主键）、password_hash、tenant、roles、status
    加入申请 tenant_join_requests username、tenant、status、reviewer

**用户与租户是多对一**：一个用户最多属于一个租户，`users.tenant` 就是那一列。
它不是外键，两个原因：

  - 允许为空 —— "还没有租户"是一个**合法状态**（刚注册、刚被移出），
    而外键要求目标行存在，空串没有对应行；
  - 租户改名的代价 —— 有外键时要 `ON UPDATE CASCADE`，没有外键时由
    `rename_tenant` 显式改写两张表，行为一样但**看得见**（改名这件事本来就
    应该是一次显式操作，而不是一个约束的副作用）。

`username` 而不是 `user`：后者在 PostgreSQL 里是保留字（等价于 `CURRENT_USER`），
裸列名会让 DDL 直接语法报错，而报错不会告诉你"是名字取错了"。

存储
----
配了 `DB_URL` 落 PG（**唯一可用于生产的形态** —— 账号数据必须跨重启、
多副本一致）；否则落进程内存，仅供本地开发与测试。内存模式下每个进程
各有一份**互不可见**的账号表，多副本部署会表现为"A 副本上刚建的用户，
B 副本说不存在" —— 所以启动期会点名告警（见 `main._assert_auth_policy`）。

关于"无租户能读到什么"（2026-10-05 口径**反转**）
----------------------------------------------
**旧口径（2026-10-04 拍板）**：`tenant` 为空串时检索层**不做过滤** ⇒ 无租户的
账号能看到**全部租户**的文档。当时它被接受的理由是"新注册的账号还没分租户"，
代价是自助注册即可读全库。

**现口径（2026-10-05 拍板）**：无租户只能读**公共租户**（`config.DEFAULT_TENANT`，
通常是 `public`）的文档；**没配公共租户则什么都读不到**（落到
`config.NO_TENANT_SENTINEL`）。用户原话：「无租户可以查我们提供的公共租户文档，
别的租户的不允许」。

新口径同样是**刻意的**，且方向是收紧 —— 判据集中在 `main._retrieval_tenant`
（返回值**绝不为空串**，因为空串在检索层意味着"不加过滤"）与
`main._assert_auth_policy`。改动前请先确认这条口径是否又变了。
"""

import logging
import threading
import time

from . import config

#: 用户状态。`disabled` 用于停用账号（保留数据、禁止登录）。
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"
#: 加入申请的状态。
REQ_PENDING = "pending"
REQ_APPROVED = "approved"
REQ_REJECTED = "rejected"

#: 角色与租户名的合法字符集（宽松但排除分隔符）。
#: 为什么要在存储层再查一次：这些值会拼进 JWT claim、也会拼进会话白名单的键
#: （键格式 `auth:sess:{user}:{sid}`，靠 `:` 分隔）。含分隔符的用户名会让
#: "登出我的全部设备"跨用户匹配 —— `app/auth.py::_key_escape` 已经做了转义兜底，
#: 但那属于第二道防线；第一道是**别让这种值进来**。
_FORBIDDEN_IN_NAME = (":", ",", "|", "%")


class AccountError(Exception):
    """账号操作的业务错误（重名、不存在、非法取值）。

    与 `AuthError` 分开是刻意的：前者是"这次操作不合法"（HTTP 409/404/400），
    后者是"你的凭证不管用"（401/403）。混在一起会让 401 与 409 的边界变糊，
    而客户端对这两类的处理完全不同（重新登录 vs 改参数）。
    """

    def __init__(self, detail, *, status_code=400, code="account_error"):
        super().__init__(detail)
        self.detail = detail
        self.status_code = int(status_code)
        self.code = code


def _check_name(label, value, *, allow_empty=False):
    """校验用户名 / 租户名 / 角色名。返回去空白后的值。"""
    value = (value or "").strip()
    if not value and not allow_empty:
        raise AccountError(f"{label}不能为空", status_code=400, code="invalid_name")
    for ch in _FORBIDDEN_IN_NAME:
        if ch in value:
            raise AccountError(f"{label}不能包含字符 {ch!r}", status_code=400,
                               code="invalid_name")
    if len(value) > 128:
        raise AccountError(f"{label}过长（上限 128 字符）", status_code=400,
                           code="invalid_name")
    return value


def _split_roles(raw):
    """`reviewer|admin` → `('reviewer', 'admin')`。"""
    return tuple(r.strip() for r in (raw or "").split("|") if r.strip())


def _join_roles(roles):
    """角色序列 → 存储用的 `|` 分隔串（与 `AUTH_USERS` 的写法保持一致）。"""
    if isinstance(roles, str):
        roles = _split_roles(roles)
    return "|".join(r.strip() for r in (roles or ()) if r and r.strip())


def _has_admin_role(roles_text, admin_roles) -> bool:
    """角色串里有没有管理员角色。按 `|` **分词精确匹配**。

    ⚠️ 不能用子串判断（`"admin" in roles_text`）：那样 `administrator` /
    `superadmin` 都会被算成管理员，而这些角色未必在 `AUTH_ADMIN_ROLES` 里 ——
    判宽了会让下面那道"最后一个管理员"守卫在该拦的时候不拦（而它拦不住的那一刻，
    就是这套权限系统再也回不来的那一刻）。
    """
    return bool(set(_split_roles(roles_text)) & set(admin_roles))


def _leaves_no_active_admin(rows, target, admin_roles, *, new_roles=None,
                            new_status=None, deleting=False) -> bool:
    """这次写操作之后，会不会**一个可用的管理员都不剩**。返回 True = 必须拒绝。

    rows: `[(用户名, 角色串, 状态), ...]`，**改之前**的一致快照。
    target: 被改的那一行的当前值（同形状）。
    new_roles / new_status: 改完之后的值；None = 保持 `target` 里的值。
    deleting: True 表示这一行会被删除。

    为什么条件是"按改完之后的状态算"而不是"看字段有没有变"
    ------------------------------------------------------
    降权（roles 去掉 admin）与停用（status=disabled）是**两条**路，将来还可能有
    第三、第四条（批量脚本、迁移）。只拦字段变化会漏；按"最终还有没有管理员"算，
    任何新路径都自动被覆盖。

    为什么先判"目标现在是不是管理员"
    ----------------------------
    否则"给一个普通用户降权"会被判成"删掉最后一个管理员"而拒绝 —— 库里本来就没有
    管理员时（有人直接改过库），一条普通操作的 409 会把人引向完全错误的方向。
    """
    if not (target[2] == STATUS_ACTIVE and _has_admin_role(target[1], admin_roles)):
        return False                # 他本来就不是可用管理员 ⇒ 减少不了管理员数量
    if not deleting:
        final_roles = target[1] if new_roles is None else _join_roles(new_roles)
        final_status = target[2] if new_status is None else new_status
        if final_status == STATUS_ACTIVE and _has_admin_role(final_roles, admin_roles):
            return False            # 改完他自己还是管理员
    others = [r for r in rows
              if r[0] != target[0]
              and r[2] == STATUS_ACTIVE
              and _has_admin_role(r[1], admin_roles)]
    return not others


def _last_admin_error(admin_roles) -> "AccountError":
    """"最后一个管理员"的拒绝理由（409 `last_admin`）。

    文案必须把"为什么不可逆"写清楚：读日志/看报错的人多半第一反应是"我自己的系统
    我还不能改？"，而真实约束是**没有任何接口能把它加回来** —— 种子账号只在用户表为
    空时才创建，手工改库是唯一的恢复手段。
    """
    return AccountError(
        "不能移除最后一个管理员（降权 / 停用 / 删除都算）：之后没有任何身份还能管理"
        "用户与租户，而初始管理员只在用户表为空时才会被创建 —— 它不会自动补回来，"
        f"唯一恢复手段是直接改数据库。请先把另一个账号设为管理员"
        f"（角色：{'/'.join(sorted(admin_roles))}）再回来操作。",
        status_code=409, code="last_admin")


class AccountStore:
    """租户 / 用户 / 加入申请的读写。

    所有写操作在 PG 模式下都是**单条语句**或"先查后写同一个连接"，不引入
    长事务 —— 账号操作的低频特性不值得为它维护事务边界，而漏掉一次 commit
    造成的"改了但没生效"远比并发写冲突更难查。

    ⚠️ 唯一例外是"最后一个管理员"守卫（`keep_active_admins`）：它**必须**把
    "数管理员 → 写"放进同一段**串行**区间，否则并发的两次降权会各自数到
    "还有别人"然后一起成功（详见 `_guard_last_admin`）。那段区间用事务级咨询锁
    实现，仍然是同一连接、同一事务，不引入跨请求的状态。

    ⚠️ **`with conn:` 不是事务**（2026-10-06 修）
    ------------------------------------------
    连接池是 `autocommit=True` 建的（见 `app/pg.py`），此时 `with conn:` 只是
    形式上的事务块 —— 它**不发 BEGIN**，每条语句自成一个事务。而事务级咨询锁
    （`pg_advisory_xact_lock`）与 `SELECT ... FOR UPDATE` 的语义都依赖"真的在
    事务里"，在 autocommit 下它们**语句一结束就释放** ⇒ 守卫的串行区间是空的：
    两个管理员并发互降会各自读到"还有别人"、然后**一起成功**，库里一个管理员
    都不剩（不可逆）。

    所以这两处写入口必须显式写 `with conn.transaction():` —— psycopg 的
    `Connection.transaction()` 在 autocommit 连接上**也会真的发 BEGIN/COMMIT**
    （官方文档 "you can use a `transaction()` context ... # BEGIN is executed"）。
    守卫：`tests/test_admin_guard.py::test_pg_guard_runs_inside_an_explicit_transaction`。
    """

    #: PG 路径的事务级咨询锁键。取值本身没有含义，只要**全流程用同一个**。
    #: 用它而不是 `LOCK TABLE users`：后者要表级排他锁，会把列表/详情这些读也挡住。
    _ADMIN_GUARD_KEY = 0x61646D31       # "adm1"

    def __init__(self, dsn="", seed_users=None):
        """构造。

        dsn: DB_URL；空则用内存存储（仅限本地开发）。
        seed_users: `{用户名: {hash, tenant, roles}}`，即 `config.AUTH_USERS`。
            只在**库里一个用户都没有**时用来建出第一个管理员。见 `seed_if_empty`。
        """
        self.dsn = (dsn or "").strip()
        self.seed_users = dict(seed_users or {})
        # ⚠️ 一开始就按"配了 DB_URL 就是 postgres"标注：`_ensure_schema` 是**惰性**的
        # （第一次真正用到才建表），而启动自检要读这个值来判断"账号数据落在哪"。
        # 若等到建表才改名，自检会在一个 dsn 明明配好的部署上误报"落在进程内存"。
        self.backend = "postgres" if self.dsn else "memory"
        self._schema_ready = False
        #: 与 `_schema_ready` 配套：冷启动并发建表的竞态由它挡住，见 `_ensure_schema`
        self._schema_lock = threading.Lock()
        self._lock = threading.Lock()
        # 内存后端的三个"表"
        self._tenants: dict[str, dict] = {}
        self._users: dict[str, dict] = {}
        self._requests: dict[int, dict] = {}
        self._next_req_id = 1

    # ------------------------------------------------------------------
    # 连接与建表
    # ------------------------------------------------------------------
    def _connect(self):
        """借一条**池化** PG 连接（借用 → 事务块 → 归还），见 `app/pg.py`。"""
        from . import pg

        return pg.connect(self.dsn)

    @staticmethod
    def schema_statements() -> list:
        """本模块负责的全部 DDL（**纯函数**，不连库）。

        拆出来是为了让迁移（`app/migrations.py` 的 v8）与守卫测试能共用同一份
        语句 —— 手抄第二份迟早漂移，而漂移的表现是"新库对、老库错"。
        """
        return [
            # 租户。`name` 是主键：租户在本项目里就是**一个名字**，
            # 没有独立的数字 id —— 这个值会进 JWT claim、进向量库的过滤字段、
            # 进审计日志，多一层 id 只是多一次映射，不带来任何好处。
            "CREATE TABLE IF NOT EXISTS tenants ("
            "  name       text PRIMARY KEY,"
            "  status     text NOT NULL DEFAULT 'active',"
            "  note       text NOT NULL DEFAULT '',"
            "  created_at timestamptz NOT NULL DEFAULT now(),"
            "  updated_at timestamptz NOT NULL DEFAULT now()"
            ")",
            # 用户。`tenant` 允许空串 = 还没有租户（合法状态，见文件头）。
            "CREATE TABLE IF NOT EXISTS users ("
            "  username      text PRIMARY KEY,"
            "  password_hash text NOT NULL,"
            "  tenant        text NOT NULL DEFAULT '',"
            "  roles         text NOT NULL DEFAULT '',"
            "  status        text NOT NULL DEFAULT 'active',"
            "  created_at    timestamptz NOT NULL DEFAULT now(),"
            "  updated_at    timestamptz NOT NULL DEFAULT now()"
            ")",
            "CREATE INDEX IF NOT EXISTS users_tenant_idx ON users (tenant)",
            # 加入申请。"申请 → 审批"两段式：用户提交，管理员批准或驳回。
            "CREATE TABLE IF NOT EXISTS tenant_join_requests ("
            "  id          bigserial PRIMARY KEY,"
            "  username    text NOT NULL,"
            "  tenant      text NOT NULL,"
            "  status      text NOT NULL DEFAULT 'pending',"
            "  reviewer    text NOT NULL DEFAULT '',"
            "  note        text NOT NULL DEFAULT '',"
            "  created_at  timestamptz NOT NULL DEFAULT now(),"
            "  reviewed_at timestamptz"
            ")",
            # 同一个用户对同一个租户**只允许有一条待审申请**。
            # 用部分唯一索引而不是应用层判重：并发点两次提交请求时，
            # "先查有没有再插入"的两步写法会让两条都进来。
            "CREATE UNIQUE INDEX IF NOT EXISTS tenant_join_pending_uniq "
            "ON tenant_join_requests (username, tenant) WHERE status = 'pending'",
        ]

    def _ensure_schema(self, conn):
        """建三张表（幂等）。

        ⚠️ 整段 DDL 必须持锁（2026-10-06 补）：`_schema_ready` 是普通 bool，
        冷启动时并发请求会同时读到 False、同时执行 `CREATE ... IF NOT EXISTS`，
        而 PG 在这类语句上有真实竞态（撞 `pg_type_typname_nsp_index` 唯一索引）。
        这里建的还是**账号表** —— 建表失败会让登录直接 500，而报错看起来像"库坏了"。
        理由与 `review_queue._ensure_schema` 同一份。
        """
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:      # 双检：等锁期间可能已被别人建好
                return
            with conn.cursor() as cur:
                for statement in self.schema_statements():
                    cur.execute(statement)
            self._schema_ready = True   # 必须在锁内置位
            self.backend = "postgres"

    def ensure_schema(self):
        """确保三张表存在（幂等）。供迁移与部署脚本调用；无 dsn 时返回 False。"""
        if not self.dsn:
            return False
        with self._connect() as conn:
            self._ensure_schema(conn)
        return True

    # ------------------------------------------------------------------
    # 启动种子
    # ------------------------------------------------------------------
    def seed_if_empty(self) -> int:
        """库里没有任何用户时，用 `AUTH_USERS` 里的条目建出初始账号。

        为什么需要它 —— 鸡生蛋问题：管理接口要求管理员角色，而管理员本身
        只能由管理员创建。所以第一个管理员必须来自配置。

        为什么是"仅当用户表为空"：一旦库里有了用户，配置里那张表就**不再参与**
        任何判定。否则会出现两份权威（配置删了人、库里还有；或反过来），
        而"配置里多留了一个人"这件事在运行时完全不可见。

        返回本次建出的用户数（内存模式恒为 0，因为内存库每次都是全新的，
        种子的价值在于"重启后还在"，内存模式下没有这个前提）。
        """
        if not self.seed_users:
            return 0
        if not self.dsn:
            # 内存模式：直接把种子放进去，方便本地开发能登录。
            with self._lock:
                if self._users:
                    return 0
                for name, entry in self.seed_users.items():
                    self._users[name] = self._user_row(
                        name, entry.get("hash", ""), entry.get("tenant", ""),
                        entry.get("roles", ()), STATUS_ACTIVE,
                    )
                return len(self.seed_users)

        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM users")
                if int(cur.fetchone()[0]) > 0:
                    return 0
                created = 0
                for name, entry in self.seed_users.items():
                    try:
                        cur.execute(
                            "INSERT INTO users (username, password_hash, tenant, roles, status) "
                            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (username) DO NOTHING",
                            (name, entry.get("hash", ""), entry.get("tenant", ""),
                             _join_roles(entry.get("roles", ())), STATUS_ACTIVE),
                        )
                        created += int(cur.rowcount or 0)
                    except Exception as e:  # noqa: BLE001
                        logging.warning("初始账号 %s 建库失败，已跳过：%s", name, e)
                if created:
                    logging.info(
                        "已用 AUTH_USERS 建出 %d 个初始账号（此后配置里那张表不再参与判定）",
                        created)
                return created

    # ------------------------------------------------------------------
    # 行构造
    # ------------------------------------------------------------------
    @staticmethod
    def _user_row(username, pwhash, tenant, roles, status,
                  created_at=None, updated_at=None):
        return {
            "username": username,
            "password_hash": pwhash,
            "tenant": tenant or "",
            "roles": _split_roles(roles) if isinstance(roles, str) else tuple(roles or ()),
            "status": status or STATUS_ACTIVE,
            "created_at": created_at,
            "updated_at": updated_at,
        }

    @staticmethod
    def _tenant_row(name, status, note, created_at=None, updated_at=None):
        return {"name": name, "status": status or STATUS_ACTIVE, "note": note or "",
                "created_at": created_at, "updated_at": updated_at}

    # ==================================================================
    # 租户
    # ==================================================================
    def create_tenant(self, name, note="") -> dict:
        """新建租户。重名抛 `AccountError`（409）。"""
        name = _check_name("租户名", name)
        if self.dsn:
            with self._connect() as conn:
                self._ensure_schema(conn)
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM tenants WHERE name = %s", (name,))
                    if cur.fetchone():
                        raise AccountError(f"租户已存在：{name}", status_code=409,
                                           code="tenant_exists")
                    cur.execute(
                        "INSERT INTO tenants (name, note) VALUES (%s, %s)", (name, note or ""))
            return self.get_tenant(name) or self._tenant_row(name, STATUS_ACTIVE, note)
        with self._lock:
            if name in self._tenants:
                raise AccountError(f"租户已存在：{name}", status_code=409,
                                   code="tenant_exists")
            self._tenants[name] = self._tenant_row(name, STATUS_ACTIVE, note,
                                                   time.time(), time.time())
            return dict(self._tenants[name])

    def get_tenant(self, name) -> dict | None:
        name = (name or "").strip()
        if not name:
            return None
        if self.dsn:
            with self._connect() as conn:
                self._ensure_schema(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT name, status, note, created_at, updated_at "
                        "FROM tenants WHERE name = %s", (name,))
                    row = cur.fetchone()
            return self._tenant_row(*row) if row else None
        with self._lock:
            row = self._tenants.get(name)
            return dict(row) if row else None

    def list_tenants(self) -> list:
        """全部租户，按名字排序。"""
        if self.dsn:
            with self._connect() as conn:
                self._ensure_schema(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT name, status, note, created_at, updated_at "
                        "FROM tenants ORDER BY name")
                    rows = cur.fetchall()
            return [self._tenant_row(*r) for r in rows]
        with self._lock:
            return [dict(v) for _, v in sorted(self._tenants.items())]

    def rename_tenant(self, old, new) -> bool:
        """租户改名，并**同时改写挂靠用户的租户字段**。

        为什么必须同批改：`users.tenant` 存的是这一列的名字本身（没有 id 层）。
        只改租户表会让所有挂靠用户指向一个不存在的租户 —— 而那个状态不会报错，
        只会静默表现为"这些人突然搜不到东西了"。
        """
        old = _check_name("原租户名", old)
        new = _check_name("新租户名", new)
        if old == new:
            return True
        if self.dsn:
            with self._connect() as conn:
                self._ensure_schema(conn)
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM tenants WHERE name = %s", (new,))
                    if cur.fetchone():
                        raise AccountError(f"目标租户名已存在：{new}", status_code=409,
                                           code="tenant_exists")
                    cur.execute(
                        "UPDATE tenants SET name = %s, updated_at = now() WHERE name = %s",
                        (new, old))
                    if cur.rowcount == 0:
                        return False
                    cur.execute("UPDATE users SET tenant = %s, updated_at = now() "
                                "WHERE tenant = %s", (new, old))
                    cur.execute("UPDATE tenant_join_requests SET tenant = %s "
                                "WHERE tenant = %s", (new, old))
            return True
        with self._lock:
            if old not in self._tenants:
                return False
            if new in self._tenants:
                raise AccountError(f"目标租户名已存在：{new}", status_code=409,
                                   code="tenant_exists")
            self._tenants[new] = self._tenants.pop(old)
            self._tenants[new]["name"] = new
            for u in self._users.values():
                if u["tenant"] == old:
                    u["tenant"] = new
            for r in self._requests.values():
                if r["tenant"] == old:
                    r["tenant"] = new
            return True

    def delete_tenant(self, name, *, force=False) -> dict:
        """删除租户。返回 `{"deleted": bool, "orphaned_users": [...]}`。

        ⚠️ 默认**不**允许删除还有用户挂靠的租户（`force=False`）—— 因为删掉之后
        那些人会变成"无租户"，而无租户只能读**公共租户**的内容。也就是说
        "删掉一个租户"会顺带把里面的人**降级**成公共分区的读者 —— 那是静默的权限
        变化（对当事用户来说是"我的资料看不见了"，而这没有任何报错）。

        只有显式 `force=True`（管理接口上的一个显式参数）才会连带解除挂靠。
        这不是过度谨慎：它把一个静默的权限扩张变成了一个必须勾选的选项。
        """
        name = _check_name("租户名", name)
        def _orphans(rows):
            return [r["username"] for r in rows if r["tenant"] == name]

        if self.dsn:
            with self._connect() as conn:
                self._ensure_schema(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT username, password_hash, tenant, roles, status, "
                        "created_at, updated_at FROM users WHERE tenant = %s", (name,))
                    attached = [self._user_row(*r) for r in cur.fetchall()]
                    if attached and not force:
                        return {"deleted": False, "orphaned_users": _orphans(attached),
                                "reason": "租户下仍有用户，需先移出或显式 force=true"}
                    cur.execute("DELETE FROM tenants WHERE name = %s", (name,))
                    deleted = cur.rowcount > 0
                    if deleted and force and attached:
                        cur.execute("UPDATE users SET tenant = '', updated_at = now() "
                                    "WHERE tenant = %s", (name,))
                    if deleted:
                        cur.execute("DELETE FROM tenant_join_requests WHERE tenant = %s",
                                    (name,))
            return {"deleted": bool(deleted),
                    "orphaned_users": _orphans(attached) if force else []}
        with self._lock:
            if name not in self._tenants:
                return {"deleted": False, "orphaned_users": []}
            attached = [u for u in self._users.values() if u["tenant"] == name]
            if attached and not force:
                return {"deleted": False, "orphaned_users": _orphans(attached),
                        "reason": "租户下仍有用户，需先移出或显式 force=true"}
            # ⚠️ 名单必须在**改写之前**算出来：`attached` 里装的是用户行本身的引用，
            # 把 tenant 清空之后再调 `_orphans` 就永远得到空列表 —— 于是"强删租户"
            # 这条权限边界事件在内存后端下**不会进日志**，而 PG 后端拿的是新构造的行、
            # 不受影响 ⇒ 两个后端行为分叉，且分叉的那一侧正好是"留痕"。
            moved = _orphans(attached)
            self._tenants.pop(name, None)
            if force:
                for u in attached:
                    u["tenant"] = ""
            self._requests = {k: v for k, v in self._requests.items()
                              if v["tenant"] != name}
            return {"deleted": True, "orphaned_users": moved if force else []}

    def count_tenant_users(self, name) -> int:
        if self.dsn:
            with self._connect() as conn:
                self._ensure_schema(conn)
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM users WHERE tenant = %s", (name,))
                    return int(cur.fetchone()[0])
        with self._lock:
            return sum(1 for u in self._users.values() if u["tenant"] == name)

    # ------------------------------------------------------------------
    # "最后一个管理员"守卫（2026-10-06）
    # ------------------------------------------------------------------
    def _guard_last_admin(self, conn, username, admin_roles, *, new_roles=None,
                          new_status=None, deleting=False) -> None:
        """PG 路径：在**同一事务**里串行地"锁定 → 数管理员 → 判定"，不通过就抛错。

        为什么必须在存储层、而且必须在锁内
        ------------------------------
        · **在存储层**：接口层只能拦"改自己"，拦不住"管理员 A 把管理员 B 降权"
          与"B 同时把 A 降权"这种**两步/两人**的操作；而将来多一条改账号的路
          （批量脚本、迁移）就又漏一个点。放在唯一的那道写入口上，才谈得上完备。
        · **在锁内**："数一遍再写"中间隔着一次读，两个并发请求会各自数到"还有别人"
          然后**一起成功** —— 最后两个管理员同时消失。这不是理论竞态：两个管理员
          互相清理对方权限是个很自然的操作，而它一旦发生就**不可逆**（见
          `_last_admin_error` 的说明）。
        · 咨询锁是**事务级**的（`pg_advisory_xact_lock`）：事务结束自动释放，
          所以异常路径也不会漏解锁；也不需要往 DDL 里加任何东西。
          ⚠️ "事务级"是**前提**：调用方必须已经处于 `with conn.transaction():` 之内。
          在 autocommit 连接上用 `with conn:` 的话根本没有事务，锁会**当场释放**
          —— 本函数原先正是这样被调用的（见类文档里那段"`with conn:` 不是事务"）。

        为什么先取咨询锁再 `FOR UPDATE` 目标行：所有走本守卫的写者都按**同一顺序**
        取锁，因此不可能互相等待成环（死锁）。反过来（先锁行、后取咨询锁）在不同
        行上并发时就可能成环。
        """
        if not admin_roles:
            # 没配管理员角色 ⇒ 谁都不是管理员（管理接口对所有人 403），
            # 不存在"最后一个"这回事。判宽了会把正常操作拦下来。
            return
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (self._ADMIN_GUARD_KEY,))
            cur.execute(
                "SELECT username, roles, status FROM users WHERE username = %s FOR UPDATE",
                (username,))
            target = cur.fetchone()
            if target is None:
                return              # 用户不存在：交给调用方的 None / rowcount 语义
            cur.execute("SELECT username, roles, status FROM users")
            rows = cur.fetchall()
        if _leaves_no_active_admin(rows, target, admin_roles, new_roles=new_roles,
                                   new_status=new_status, deleting=deleting):
            raise _last_admin_error(admin_roles)

    def _guard_last_admin_memory(self, username, admin_roles, *, new_roles=None,
                                 new_status=None, deleting=False) -> None:
        """内存路径的同款守卫。

        ⚠️ 调用方**必须已经持有 `self._lock`** —— 内存后端没有咨询锁，
        "串行区间"就是那把全局锁。在锁外调用等于把上面说的并发缺陷原样搬过来。
        """
        if not admin_roles:
            return
        row = self._users.get(username)
        if row is None:
            return
        rows = [(u["username"], _join_roles(u["roles"]), u["status"])
                for u in self._users.values()]
        target = (row["username"], _join_roles(row["roles"]), row["status"])
        if _leaves_no_active_admin(rows, target, admin_roles, new_roles=new_roles,
                                   new_status=new_status, deleting=deleting):
            raise _last_admin_error(admin_roles)

    # ==================================================================
    # 用户
    # ==================================================================
    def create_user(self, username, password_hash, tenant="", roles=(), status=STATUS_ACTIVE):
        """新建用户。`password_hash` 是 `auth.hash_password()` 的输出（调用方算好）。

        为什么不在这一层做哈希：哈希函数在 `app/auth.py`，而 `auth` 需要本模块
        提供的用户去验证登录 —— 本模块反向 import 它就成环了。让调用方传哈希串
        是最干净的切法，顺便也让"种子用户"（配置里本来就是哈希串）复用它。
        """
        username = _check_name("用户名", username)
        if not password_hash:
            raise AccountError("缺少密码哈希", status_code=400, code="invalid_password")
        tenant = (tenant or "").strip()
        status = status or STATUS_ACTIVE
        if status not in (STATUS_ACTIVE, STATUS_DISABLED):
            raise AccountError(f"状态取值非法：{status}", status_code=400, code="invalid_status")

        if self.dsn:
            with self._connect() as conn:
                self._ensure_schema(conn)
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM users WHERE username = %s", (username,))
                    if cur.fetchone():
                        raise AccountError(f"用户已存在：{username}", status_code=409,
                                           code="user_exists")
                    cur.execute(
                        "INSERT INTO users (username, password_hash, tenant, roles, status) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (username, password_hash, tenant, _join_roles(roles), status))
            return self.get_user(username)
        with self._lock:
            if username in self._users:
                raise AccountError(f"用户已存在：{username}", status_code=409,
                                   code="user_exists")
            self._users[username] = self._user_row(username, password_hash, tenant, roles,
                                                   status, time.time(), time.time())
            return dict(self._users[username])

    def get_user(self, username) -> dict | None:
        username = (username or "").strip()
        if not username:
            return None
        if self.dsn:
            with self._connect() as conn:
                self._ensure_schema(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT username, password_hash, tenant, roles, status, "
                        "created_at, updated_at FROM users WHERE username = %s", (username,))
                    row = cur.fetchone()
            return self._user_row(*row) if row else None
        with self._lock:
            row = self._users.get(username)
            return dict(row) if row else None

    def list_users(self, tenant=None) -> list:
        """全部用户；给了 `tenant` 则只列该租户的。按用户名排序。"""
        if self.dsn:
            with self._connect() as conn:
                self._ensure_schema(conn)
                with conn.cursor() as cur:
                    if tenant is None:
                        cur.execute(
                            "SELECT username, password_hash, tenant, roles, status, "
                            "created_at, updated_at FROM users ORDER BY username")
                    else:
                        cur.execute(
                            "SELECT username, password_hash, tenant, roles, status, "
                            "created_at, updated_at FROM users WHERE tenant = %s "
                            "ORDER BY username", (tenant,))
                    rows = cur.fetchall()
            return [self._user_row(*r) for r in rows]
        with self._lock:
            rows = [dict(u) for u in self._users.values()
                    if tenant is None or u["tenant"] == tenant]
        return sorted(rows, key=lambda u: u["username"])

    def update_user(self, username, *, password_hash=None, tenant=None,
                    roles=None, status=None, keep_active_admins=False,
                    admin_roles=()) -> dict | None:
        """局部更新；只改**显式传入**的字段（None = 不动）。用户不存在返回 None。

        为什么用 `None` 表示"不改"而不是"改成空"：租户可以被改成空串（移出租户），
        这是合法操作。用 `""` 与 `None` 区分"改成空"与"不动"，是个必须写清的约定 ——
        否则"把某人移出租户"这个动作会被实现成"什么都没发生"。

        keep_active_admins / admin_roles: 打开后，若这次改动会让**可用的管理员一个
        都不剩**（降权 / 停用都算），抛 409 `last_admin`。`admin_roles` 由调用方传入
        （本模块刻意不 import `app.config`：配置从上面注入，见文件头对 `seed_users`
        的说明）。默认关闭 ⇒ 既有调用方的行为一字不改。
        """
        username = (username or "").strip()
        if not username:
            return None
        if status is not None and status not in (STATUS_ACTIVE, STATUS_DISABLED):
            raise AccountError(f"状态取值非法：{status}", status_code=400, code="invalid_status")

        if self.dsn:
            sets, vals = [], []
            # `sets` 与 `vals` 必须**同步追加**（占位符与值的顺序要一一对应）。
            # 刻意不写成 `a; b` 一行两语句：那种写法在 diff 与 review 里看不出
            # "哪个值和哪个列配对"，而这里配错一位就是静默写错列。
            if password_hash is not None:
                sets.append("password_hash = %s")
                vals.append(password_hash)
            if tenant is not None:
                sets.append("tenant = %s")
                vals.append((tenant or "").strip())
            if roles is not None:
                sets.append("roles = %s")
                vals.append(_join_roles(roles))
            if status is not None:
                sets.append("status = %s")
                vals.append(status)
            if not sets:
                return self.get_user(username)
            vals.append(username)
            with self._connect() as conn:
                self._ensure_schema(conn)
                # ⚠️ 必须 `conn.transaction()`，不能只写 `with conn:`（2026-10-06 修）。
                # 连接池是 autocommit 建的 ⇒ `with conn:` 不发 BEGIN ⇒ 事务级咨询锁与
                # FOR UPDATE 在语句结束时立刻释放 ⇒ 守卫的串行区间**根本不存在**。
                # psycopg 的 `transaction()` 在 autocommit 连接上会真的发 BEGIN/COMMIT。
                with conn.transaction():
                    # 守卫与这条 UPDATE 必须**同生共死**：放到事务外重开连接的话，
                    # 锁在判定完成的那一刻就没了。
                    if keep_active_admins:
                        self._guard_last_admin(conn, username, admin_roles,
                                               new_roles=roles, new_status=status)
                    with conn.cursor() as cur:
                        cur.execute(
                            f"UPDATE users SET {', '.join(sets)}, updated_at = now() "
                            "WHERE username = %s", tuple(vals))
                        if cur.rowcount == 0:
                            return None
            return self.get_user(username)
        with self._lock:
            row = self._users.get(username)
            if row is None:
                return None
            if keep_active_admins:
                self._guard_last_admin_memory(username, admin_roles, new_roles=roles,
                                              new_status=status)
            if password_hash is not None:
                row["password_hash"] = password_hash
            if tenant is not None:
                row["tenant"] = (tenant or "").strip()
            if roles is not None:
                row["roles"] = _split_roles(roles) if isinstance(roles, str) else tuple(roles)
            if status is not None:
                row["status"] = status
            row["updated_at"] = time.time()
            return dict(row)

    def delete_user(self, username, *, keep_active_admins=False,
                    admin_roles=()) -> bool:
        """删除用户。**同一批清掉他的待审申请**（否则会留下指向不存在用户的申请）。

        keep_active_admins / admin_roles: 打开后，删除**最后一个可用管理员**会抛
        409 `last_admin`（理由同 `update_user`）。
        """
        username = (username or "").strip()
        if not username:
            return False
        if self.dsn:
            with self._connect() as conn:
                self._ensure_schema(conn)
                # 同 update_user：必须显式事务块，否则守卫的咨询锁当场释放
                with conn.transaction():
                    if keep_active_admins:
                        self._guard_last_admin(conn, username, admin_roles, deleting=True)
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM users WHERE username = %s", (username,))
                        deleted = cur.rowcount > 0
                        if deleted:
                            cur.execute(
                                "DELETE FROM tenant_join_requests "
                                "WHERE username = %s AND status = 'pending'", (username,))
            return bool(deleted)
        with self._lock:
            if keep_active_admins:
                self._guard_last_admin_memory(username, admin_roles, deleting=True)
            if self._users.pop(username, None) is None:
                return False
            self._requests = {k: v for k, v in self._requests.items()
                              if not (v["username"] == username
                                      and v["status"] == REQ_PENDING)}
            return True

    def set_password(self, username, password_hash) -> dict | None:
        return self.update_user(username, password_hash=password_hash)

    # ==================================================================
    # 加入租户的申请
    # ==================================================================
    def create_join_request(self, username, tenant, note="") -> dict:
        """提交加入申请。已属于该租户 / 已有待审申请 / 租户不存在 → 抛错。"""
        username = _check_name("用户名", username)
        tenant = _check_name("租户名", tenant)
        if self.get_tenant(tenant) is None:
            raise AccountError(f"租户不存在：{tenant}", status_code=404, code="tenant_not_found")
        user = self.get_user(username)
        if user is None:
            raise AccountError(f"用户不存在：{username}", status_code=404, code="user_not_found")
        if user["tenant"] == tenant:
            raise AccountError(f"你已经是租户 {tenant} 的成员", status_code=409,
                               code="already_member")
        if user["tenant"]:
            raise AccountError(
                f"你已属于租户 {user['tenant']}；一人只能属于一个租户，"
                "换租户需联系管理员", status_code=409, code="already_in_tenant")

        if self.dsn:
            with self._connect() as conn:
                self._ensure_schema(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, username, tenant, status, reviewer, note, "
                        "created_at, reviewed_at FROM tenant_join_requests "
                        "WHERE username = %s AND tenant = %s AND status = 'pending'",
                        (username, tenant))
                    if cur.fetchone():
                        raise AccountError("你对该租户已有一条待审申请", status_code=409,
                                           code="request_pending")
                    cur.execute(
                        "INSERT INTO tenant_join_requests (username, tenant, note) "
                        "VALUES (%s, %s, %s) RETURNING id, username, tenant, status, "
                        "reviewer, note, created_at, reviewed_at",
                        (username, tenant, note or ""))
                    return self._request_row(*cur.fetchone())
        with self._lock:
            for r in self._requests.values():
                if (r["username"] == username and r["tenant"] == tenant
                        and r["status"] == REQ_PENDING):
                    raise AccountError("你对该租户已有一条待审申请", status_code=409,
                                       code="request_pending")
            rid = self._next_req_id
            self._next_req_id += 1
            row = {"id": rid, "username": username, "tenant": tenant,
                   "status": REQ_PENDING, "reviewer": "", "note": note or "",
                   "created_at": time.time(), "reviewed_at": None}
            self._requests[rid] = row
            return dict(row)

    @staticmethod
    def _request_row(rid, username, tenant, status, reviewer, note,
                     created_at=None, reviewed_at=None):
        return {"id": int(rid), "username": username, "tenant": tenant,
                "status": status, "reviewer": reviewer or "", "note": note or "",
                "created_at": created_at, "reviewed_at": reviewed_at}

    def list_join_requests(self, status=None, username=None) -> list:
        """列出申请。`status` 传 `pending` 用来给管理员看待审队列。"""
        if self.dsn:
            where, vals = [], []
            if status:
                where.append("status = %s")
                vals.append(status)
            if username:
                where.append("username = %s")
                vals.append(username)
            clause = (" WHERE " + " AND ".join(where)) if where else ""
            with self._connect() as conn:
                self._ensure_schema(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, username, tenant, status, reviewer, note, "
                        "created_at, reviewed_at FROM tenant_join_requests"
                        + clause + " ORDER BY id", tuple(vals))
                    rows = cur.fetchall()
            return [self._request_row(*r) for r in rows]
        with self._lock:
            rows = [dict(r) for r in self._requests.values()
                    if (not status or r["status"] == status)
                    and (not username or r["username"] == username)]
        return sorted(rows, key=lambda r: r["id"])

    def get_join_request(self, req_id) -> dict | None:
        try:
            req_id = int(req_id)
        except (TypeError, ValueError):
            return None
        if self.dsn:
            with self._connect() as conn:
                self._ensure_schema(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, username, tenant, status, reviewer, note, "
                        "created_at, reviewed_at FROM tenant_join_requests WHERE id = %s",
                        (req_id,))
                    row = cur.fetchone()
            return self._request_row(*row) if row else None
        with self._lock:
            row = self._requests.get(req_id)
            return dict(row) if row else None

    def resolve_join_request(self, req_id, *, approve, reviewer, note="") -> dict:
        """批准或驳回一条申请。

        批准时**顺带把用户的租户改成申请的那个** —— 两件事必须同批完成，
        否则会出现"申请批了、人还在无租户状态"，而无租户只能读公共租户
        的那一档（见文件头）。这正是"审批"这个动作唯一的意义所在。

        已经被处理过的申请不能重复处理（否则驳回之后可以再批准，绕过审批记录）。
        """
        req = self.get_join_request(req_id)
        if req is None:
            raise AccountError("申请不存在", status_code=404, code="request_not_found")
        if req["status"] != REQ_PENDING:
            raise AccountError(f"该申请已处理过（{req['status']}）", status_code=409,
                               code="request_resolved")

        new_status = REQ_APPROVED if approve else REQ_REJECTED
        reviewer = _check_name("审批人", reviewer)

        if self.dsn:
            with self._connect() as conn:
                self._ensure_schema(conn)
                with conn.cursor() as cur:
                    # 带 `status = 'pending'` 的条件更新：并发下两个管理员同时点
                    # "批准"时，只有第一个的 rowcount 会是 1，第二个拿不到行 → 报冲突。
                    # 不用事务也不需要锁 —— 比较并交换的语义由这条 WHERE 提供。
                    cur.execute(
                        "UPDATE tenant_join_requests SET status = %s, reviewer = %s, "
                        "note = %s, reviewed_at = now() "
                        "WHERE id = %s AND status = 'pending'",
                        (new_status, reviewer, note or "", int(req_id)))
                    if cur.rowcount == 0:
                        raise AccountError("该申请已被其他人处理", status_code=409,
                                           code="request_resolved")
                    if approve:
                        cur.execute("UPDATE users SET tenant = %s, updated_at = now() "
                                    "WHERE username = %s", (req["tenant"], req["username"]))
            return self.get_join_request(req_id) or req

        with self._lock:
            row = self._requests.get(req["id"])
            if row is None or row["status"] != REQ_PENDING:
                raise AccountError("该申请已被其他人处理", status_code=409,
                                   code="request_resolved")
            row["status"] = new_status
            row["reviewer"] = reviewer
            row["note"] = note or ""
            row["reviewed_at"] = time.time()
            if approve:
                user = self._users.get(req["username"])
                if user is not None:
                    user["tenant"] = req["tenant"]
                    user["updated_at"] = time.time()
            return dict(row)


def build_account_store() -> AccountStore:
    """按 app.config 造账号存储。

    种子取自 `config.AUTH_USERS`（自部署形态下的首个管理员来源）。它的**唯一作用**
    是建出第一个管理员 —— 库里一旦有用户，这份配置就不再参与任何判定
    （见 `seed_if_empty`）。
    """
    return AccountStore(dsn=config.DB_URL, seed_users=config.AUTH_USERS)
