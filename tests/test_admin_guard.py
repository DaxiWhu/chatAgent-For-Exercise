"""管理员锁死护栏：自降权 + "至少剩一个可用管理员"（2026-10-06）。

为什么这两条要单独守
------------------
一旦库里**一个管理员都不剩**，就再也没有任何接口能把它加回来：

  · 管理接口要求管理员角色 ⇒ 自锁；
  · 初始管理员（`AUTH_USERS` 种子）只在**用户表为空**时创建 ⇒ 表里还有人，
    种子永远不再生效（这是刻意的，见 `AccountStore.seed_if_empty`：两份权威比没有
    管理员更难查）；
  · 于是唯一恢复手段是**直接连数据库改**，而那时线上已经没人能管用户/租户/加入了。

两条护栏分工不同，缺一不可：

  1. **接口层**（`PATCH /v1/admin/users/{me}`）：拦住"自己摘掉自己的管理员角色 /
     停用自己"。自降权尤其隐蔽 —— 请求会**成功**，直到下一次调用管理接口才 403。
  2. **存储层**（`keep_active_admins`）：拦住"最后一个管理员被降权/停用/删除"，
     包括**两个管理员并发互降**这种接口层根本看不见的情形（各自都数到"还有别人"，
     然后一起成功）。这也是为什么它必须在同一个事务里带锁判定。

接口层的护栏**替代不了**存储层：它只认识"改自己"，而 `admin A 降 admin B` 与
并发互降都从它眼皮底下过。
"""
import ast
import pathlib
import threading
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app import accounts, config
from app import auth as A
from app import main as M
from app.accounts import AccountError
from app.sessions import SessionRegistry

_TEST_ITER = 100_000
ADMIN_ROLES = ("admin",)


def _store(admin_names=("root",), plain=("bob",)):
    """造一个内存账号库；`admin_names` 里的用户带管理员角色。"""
    st = accounts.AccountStore(dsn="")
    st.create_tenant("acme")
    for name in plain:
        st.create_user(name, A.hash_password(f"pw-{name}", iterations=_TEST_ITER),
                       tenant="acme", roles=())
    for name in admin_names:
        st.create_user(name, A.hash_password(f"pw-{name}", iterations=_TEST_ITER),
                       tenant="acme", roles=ADMIN_ROLES)
    return st


@pytest.fixture
def env(monkeypatch):
    """两个管理员（root / ops）+ 一个普通用户（bob），内存账号库。"""
    store = _store(admin_names=("root", "ops"))
    monkeypatch.setattr(config, "AUTH_USERS", {})
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    monkeypatch.setattr(config, "AUTH_SESSION_SECRET", "t" * 48)
    monkeypatch.setattr(config, "AUTH_REVIEWER_ROLES", ("reviewer",))
    monkeypatch.setattr(config, "AUTH_ADMIN_ROLES", ADMIN_ROLES)
    service = A.AuthService(A.SessionStore(enabled=True), accounts=store)
    monkeypatch.setattr(M, "_AUTH", service)
    monkeypatch.setattr(M, "_ACCOUNTS", store)
    monkeypatch.setattr(M, "_SESSIONS", SessionRegistry(enabled=True))
    return {"store": store, "service": service}


@pytest.fixture
def client(env):
    with TestClient(M.app) as c:
        yield c


def _login(client, username):
    r = client.post("/v1/auth/login",
                    json={"username": username, "password": f"pw-{username}"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _active_admins(store):
    return sorted(u["username"] for u in store.list_users()
                  if u["status"] == accounts.STATUS_ACTIVE and "admin" in u["roles"])


# ---------------------------------------------------------------------------
# 接口层：自伤护栏
# ---------------------------------------------------------------------------
def test_cannot_demote_yourself(client):
    """自降权被拒（409），且**角色没有真的被改掉**。

    只断言状态码是不够的：如果护栏写在 update 之后，请求会返回 409 而改动已经落库 ——
    "报了错但事情已经发生"是最糟的一种。所以这里回读一遍角色。
    """
    h = _login(client, "root")
    r = client.patch("/v1/admin/users/root", json={"roles": ["reviewer"]}, headers=h)
    assert r.status_code == 409, r.text
    assert "管理员角色" in r.json()["detail"]
    assert _active_admins(M._ACCOUNTS) == ["ops", "root"], "报了错但角色已经改掉"


def test_cannot_disable_yourself(client):
    """停用自己仍被拒 —— 这是原有护栏，与自降权合并成一条判断后必须还在。"""
    h = _login(client, "root")
    r = client.patch("/v1/admin/users/root", json={"status": "disabled"}, headers=h)
    assert r.status_code == 409, r.text
    assert M._ACCOUNTS.get_user("root")["status"] == accounts.STATUS_ACTIVE


def test_demoting_another_admin_is_still_allowed(client):
    """**反向锁**：还有一个管理员在，降权别人必须照常放行。

    没有这条，"加了个护栏"很容易变成"管理接口不能改角色了" —— 而那是个更严重的
    故障（正常运维被挡住），且很容易被当成"安全加固的代价"接受下来。
    """
    h = _login(client, "root")
    r = client.patch("/v1/admin/users/ops", json={"roles": ["reviewer"]}, headers=h)
    assert r.status_code == 200, r.text
    assert "admin" not in r.json()["roles"]
    assert _active_admins(M._ACCOUNTS) == ["root"]


def test_removing_an_admin_role_from_a_plain_user_is_fine(client):
    """给普通用户改角色不该被误判成"最后一个管理员"（判据必须先看目标当前状态）。"""
    h = _login(client, "root")
    r = client.patch("/v1/admin/users/bob", json={"roles": []}, headers=h)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 存储层：最后一个管理员（接口层够不着的那一半）
# ---------------------------------------------------------------------------
def test_storage_refuses_to_demote_the_last_admin():
    st = _store(admin_names=("root",))
    with pytest.raises(AccountError) as ei:
        st.update_user("root", roles=("reviewer",), keep_active_admins=True,
                       admin_roles=ADMIN_ROLES)
    assert ei.value.code == "last_admin" and ei.value.status_code == 409
    assert st.get_user("root")["roles"] == ADMIN_ROLES, "报错了但角色已经改掉"


def test_storage_refuses_to_disable_the_last_admin():
    """降权与停用是**两条**路，漏一条等于没守。"""
    st = _store(admin_names=("root",))
    with pytest.raises(AccountError) as ei:
        st.update_user("root", status=accounts.STATUS_DISABLED, keep_active_admins=True,
                       admin_roles=ADMIN_ROLES)
    assert ei.value.code == "last_admin"


def test_storage_refuses_to_delete_the_last_admin():
    st = _store(admin_names=("root",))
    with pytest.raises(AccountError) as ei:
        st.delete_user("root", keep_active_admins=True, admin_roles=ADMIN_ROLES)
    assert ei.value.code == "last_admin"
    assert st.get_user("root") is not None, "报错了但用户已经删掉"


def test_storage_allows_it_when_another_admin_remains():
    """**反向锁**：还有别的管理员时，降权/删除必须照常放行。

    没有这条，"加了个守卫"很容易变成"改不了角色、删不了人" —— 那是更严重的故障，
    而且很容易被当成"安全加固的代价"接受下来。
    """
    st = _store(admin_names=("root", "ops"))
    st.update_user("ops", roles=("reviewer",), keep_active_admins=True,
                   admin_roles=ADMIN_ROLES)
    assert _active_admins(st) == ["root"]
    # 把 ops 提回管理员再删 root：受理者从 root 换成 ops，守卫应放行
    st.update_user("ops", roles=list(ADMIN_ROLES), keep_active_admins=True,
                   admin_roles=ADMIN_ROLES)
    st.delete_user("root", keep_active_admins=True, admin_roles=ADMIN_ROLES)
    assert st.get_user("root") is None
    assert _active_admins(st) == ["ops"]


def test_guard_is_off_by_default():
    """默认**关闭**：既有调用方（脚本、测试、迁移）行为一字不改。

    这不是"留后门"：守卫的开关是**显式**的、开在管理接口那一处唯一的人手入口上，
    而脚本改账号本来就是特权操作（能跑脚本的人本来就能直接改库）。
    """
    st = _store(admin_names=("root",))
    row = st.update_user("root", roles=("reviewer",))
    assert row["roles"] == ("reviewer",)


def test_no_admin_roles_configured_means_no_guard():
    """没配管理员角色时守卫不介入：那时谁都不是管理员（接口全 403），
    强行拦会把"给普通用户改角色"这类正常操作也挡下来。"""
    st = _store(admin_names=())
    st.update_user("bob", roles=("reviewer",), keep_active_admins=True, admin_roles=())
    assert st.get_user("bob")["roles"] == ("reviewer",)


def test_a_plain_user_is_never_the_last_admin():
    """判据先看"目标现在是不是管理员"。

    否则"给普通用户降权"会被判成"删掉最后一个管理员" —— 库里本来就没有管理员时
    （有人直接改过库），这条 409 会把排查方向引到完全错误的地方。
    """
    st = _store(admin_names=())
    st.update_user("bob", roles=(), keep_active_admins=True, admin_roles=ADMIN_ROLES)
    assert st.delete_user("bob", keep_active_admins=True, admin_roles=ADMIN_ROLES) is True


def test_concurrent_mutual_demotion_cannot_leave_zero_admins():
    """⚠️ 核心：两个管理员**并发**互降，必须恰好成功一个。

    这是接口层拦不住、而现实里最可能发生的一种（"把对方降权"是两个人的自然操作）。
    没有锁内判定的话，两个请求会各自读到"还有另一个管理员"然后一起成功 ⇒ 零管理员。
    断言在**任何**交错顺序下都成立：逐个执行时第二个必然失败，真并发时锁把两者串行化，
    结果同样是"一个成功、一个 last_admin"。
    """
    st = _store(admin_names=("root", "ops"))
    outcome = []
    barrier = threading.Barrier(2)

    def demote(target):
        barrier.wait()                  # 尽量让两个线程真正同时进入
        try:
            st.update_user(target, roles=("reviewer",), keep_active_admins=True,
                           admin_roles=ADMIN_ROLES)
            outcome.append("ok")
        except AccountError as e:
            outcome.append(e.code)

    threads = [threading.Thread(target=demote, args=(t,)) for t in ("ops", "root")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(outcome) == ["last_admin", "ok"], outcome
    assert len(_active_admins(st)) == 1, f"管理员一个都不剩了：{_active_admins(st)}"


# ---------------------------------------------------------------------------
# 接线：[管理端点] 必须把守卫打开
# ---------------------------------------------------------------------------
def test_admin_endpoints_enable_the_guard():
    """静态守卫：`app/main.py` 里每一次改用户 / 删用户都必须打开守卫。

    为什么值得一条静态断言：漏传是**静默**的（守卫默认关，功能看起来完全正常），
    而"最后一个管理员没了"这件事只在事后、且不可逆地暴露出来。

    ⚠️ 判据是"**所有** `update_user` / `delete_user` 调用"，不是"带 roles 关键字的
    那些"：PATCH 端点是用 `**fields` 展开传参的，按关键字筛会把唯一那个调用点漏掉
    （这正是本条守卫的第一版写法，实测它的 `seen` 是空的）。
    改密码走 `set_password`（只写 `password_hash`，动不了角色），因此不受此约束。

    ⚠️ 两种调用形态都要扫（2026-10-22 补）：`store.delete_user(...)` 与
    `_off(store.delete_user, ...)`。后者是"在事件循环里跑同步 DB 调用"的正确写法，
    但它把 `delete_user` 从 `Call.func` 变成了 `Call.args[0]` —— 只认前者的话，
    这个守卫会**静默失效**，而失效后测试仍是绿的（看起来守卫还在）。
    """
    src = pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    seen = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        target = None
        if isinstance(fn, ast.Attribute) and fn.attr in ("update_user", "delete_user"):
            target = fn.attr
        elif (isinstance(fn, ast.Name) and fn.id == "_off" and node.args
              and isinstance(node.args[0], ast.Attribute)
              and node.args[0].attr in ("update_user", "delete_user")):
            target = node.args[0].attr
        if target is None:
            continue
        kwargs = {k.arg: ast.unparse(k.value) for k in node.keywords}
        seen.append((node.lineno, target))
        assert kwargs.get("keep_active_admins") == "True", (
            f"第 {node.lineno} 行的 {target} 没打开最后管理员守卫：{kwargs}")
        assert kwargs.get("admin_roles") == "config.AUTH_ADMIN_ROLES", (
            f"第 {node.lineno} 行的 {target} 没把管理员角色集合传下去：{kwargs}")
    assert sorted(a for _l, a in seen) == ["delete_user", "update_user"], seen


# ===========================================================================
# PG 路径：守卫必须落在**显式事务**里（2026-10-06 修）
# ===========================================================================
class _TxTrackingCursor:
    """假游标：任何 SQL 都必须在事务块内执行，否则当场断言失败。"""

    def __init__(self, conn):
        self._conn = conn
        self._rows: list = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if not self._conn.in_tx:
            raise AssertionError(
                f"在事务块**外**执行了 SQL：{str(sql)[:70]!r}\n"
                "autocommit 连接下事务级咨询锁会当场释放 ⇒ 守卫的串行区间不存在"
            )
        self._conn.statements.append(str(sql))
        self._rows = list(self._conn.rows) if "FROM users" in str(sql) else []
        self.rowcount = 1

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _TxTrackingConn:
    """假连接：把"当前是否在事务里"变成可断言的量。

    为什么用假连接而不是真 PG：真库路径要一个跑着的 PostgreSQL（CI 有、本机常常没有），
    而这条要验证的性质是"**语句落在显式事务里**" —— 那是连接层的属性，
    记录 `transaction()` 进入/退出状态就足够，且零外部依赖、毫秒级。
    """

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.in_tx = False
        self.statements: list[str] = []

    @contextmanager
    def transaction(self):
        assert not self.in_tx, "本假连接只支持单层事务"
        self.in_tx = True
        try:
            yield self
        finally:
            self.in_tx = False

    # 模拟真连接在 autocommit 下的 `with conn:`：**不发 BEGIN**，只是形式上的块。
    # 有了它，"把 `with conn.transaction()` 改回 `with conn:`"这个变异会以
    # **正确的理由**变红（在事务外执行 SQL），而不是以 AttributeError 变红。
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _TxTrackingCursor(self)


class _ConnCtx:
    """`with self._connect() as conn:` 的替身。"""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        return False


def _pg_store(monkeypatch, conn):
    """造一个走 PG 分支、但底层连接是假对象的 AccountStore。"""
    st = accounts.AccountStore(dsn="postgresql://u:p@localhost:5432/db")
    monkeypatch.setattr(st, "_connect", lambda: _ConnCtx(conn))
    monkeypatch.setattr(st, "_ensure_schema", lambda c: None)
    monkeypatch.setattr(st, "get_user", lambda u: {"username": u})
    return st


def test_pg_guard_runs_inside_an_explicit_transaction(monkeypatch):
    """PG 路径的守卫与写操作必须落在**显式事务**里。

    2026-10-06 修。原实现是 `with conn:`，而连接池以 `autocommit=True` 建立 ——
    此时 `with conn:` **不发 BEGIN**，每条语句自成一个事务。事务级咨询锁
    （`pg_advisory_xact_lock`）与 `SELECT ... FOR UPDATE` 都在语句结束时立刻释放，
    于是"锁定 → 数管理员 → 写"之间没有任何互斥：两个管理员并发互降会各自读到
    "还有别人"、然后**一起成功**，库里一个管理员不剩且不可逆。

    ⚠️ 上一轮的并发用例走的是**内存后端**（`dsn=""`，那用的是全局锁），所以这条
    PG 路径的缺陷从未被覆盖 —— 这正是本条存在的理由。
    """
    conn = _TxTrackingConn(rows=[("root", "admin", "active"), ("ops", "admin", "active")])
    st = _pg_store(monkeypatch, conn)

    st.update_user("ops", roles=("reviewer",), keep_active_admins=True,
                   admin_roles=ADMIN_ROLES)

    assert any("pg_advisory_xact_lock" in s for s in conn.statements), "守卫没有取咨询锁"
    assert any(s.startswith("UPDATE users") for s in conn.statements), "没有执行 UPDATE"


def test_pg_delete_guard_is_also_inside_a_transaction(monkeypatch):
    """删除路径同样要显式事务块 —— 两处入口各修一次，别只修一处。"""
    conn = _TxTrackingConn(rows=[("root", "admin", "active"), ("ops", "admin", "active")])
    st = _pg_store(monkeypatch, conn)

    assert st.delete_user("ops", keep_active_admins=True, admin_roles=ADMIN_ROLES) is True
    assert any("pg_advisory_xact_lock" in s for s in conn.statements)


def test_write_entries_wrap_the_guard_in_a_transaction_block():
    """静态钉子：两个写入口里的 guard 调用必须在 `with <x>.transaction():` 之内。

    与上面两条行为断言互补：行为断言证明"现在是对的"，这一条防"重构时把它挪出去"
    —— 挪出去之后 guard 依然能被调用、依然返回正常，只是**再也不互斥**，
    而这种缺陷在单进程测试里完全看不出来。
    """
    src = pathlib.Path(__file__).resolve().parents[1] / "app" / "accounts.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    funcs = [f for c in tree.body if isinstance(c, ast.ClassDef)
             for f in c.body if isinstance(f, ast.FunctionDef)]
    found = {"update_user": False, "delete_user": False}

    for fn in (f for f in funcs if f.name in found):
        for node in ast.walk(fn):
            if not isinstance(node, ast.With):
                continue
            ctx = node.items[0].context_expr if node.items else None
            if not (isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Attribute)
                    and ctx.func.attr == "transaction"):
                continue
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "_guard_last_admin"):
                    found[fn.name] = True

    assert found == {"update_user": True, "delete_user": True}, (
        f"以下入口的 `_guard_last_admin` 不在 `with conn.transaction():` 块内：{found}\n"
        "连接池是 autocommit 建的，那时事务级咨询锁会**当场释放** —— 守卫等于不存在"
    )
