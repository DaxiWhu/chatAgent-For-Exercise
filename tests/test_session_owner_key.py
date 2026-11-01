"""会话归属判据：只按用户比，不按租户比（2026-10-22 口径）。

改这里之前先读背景
----------------
本 agent **不要求用户有租户**：`users.tenant` 允许空串，且那是合法状态
（`app/accounts.py` 文件头："允许空串 = 还没有租户"，自助注册的账号就落在这里），
用户也可能后来才加入某个租户。

归属若按 `(tenant, user)` **整体**比，同一个人在换租户那一刻，他**所有旧会话都会
变成 403**。而报错只会说"该会话属于其他主体" —— 看起来像被别人占了，实际是被
自己的租户变更踢出去的，且没有任何日志会指向"租户变了"。

但也不能反过来"一律只比 user"：机器调用方（`owner_user` 为空串）全都相等，
于是 A 租户的 Key 能读 B 租户的会话。所以判据是：

    两边 user 都非空 ⇒ 只比 user（租户不参与）
    任一边 user 为空 ⇒ 按 (tenant, user) 整体比（机器仍按租户隔离）

守四件事
--------
1. `same_owner` 的判据本身，含"机器调用方仍按租户"这条边界；
2. 换租户后旧会话**仍可读**（端到端，走真实端点，不是只调函数）；
3. 机器调用方不被放宽 —— 变异成"一律只比 user"必须有用例红；
4. 删用户后同名重建，**不能继承**旧会话（`users.username` 是主键，删了能重建同名，
   而归属键就是它）。
"""
import pytest
from fastapi.testclient import TestClient

from app import accounts, config
from app import auth as A
from app import main as M
from app.sessions import SessionRegistry, same_owner

_TEST_ITER = 100_000
ADMIN_ROLES = ("admin",)


# ---------------------------------------------------------------------------
# 1. 判据本身
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "stored,current,expected,why",
    [
        # 核心：同一个人换租户 ⇒ 同一主体
        (("globex", "alice"), ("acme", "alice"), True, "换租户不该把人锁在门外"),
        (("", "alice"), ("acme", "alice"), True, "从无租户加入租户"),
        (("acme", "alice"), ("", "alice"), True, "从有租户退回无租户"),
        # 不同的人，不管租户
        (("acme", "alice"), ("acme", "bob"), False, "同租户不同人"),
        (("acme", "alice"), ("globex", "bob"), False, "跨租户不同人"),
        # 机器调用方（user 空）⇒ 仍按租户，宁可更严
        (("acme", ""), ("globex", ""), False, "机器主体不能跨租户"),
        (("acme", ""), ("acme", ""), True, "同租户机器主体"),
        # 老数据（owner_user 默认空串）遇到真实用户 ⇒ fail-closed
        (("acme", ""), ("acme", "alice"), False, "无主老行不能被任意用户认领"),
        (("acme", "alice"), ("acme", ""), False, "机器不能读个人的会话"),
        # 边界值容错：None 与 空串等价
        ((None, None), ("", ""), True, "空值容错"),
        ((None, "alice"), ("acme", "alice"), True, "None 租户"),
    ],
)
def test_same_owner_rules(stored, current, expected, why):
    assert same_owner(stored, current) is expected, why


# ---------------------------------------------------------------------------
# 2. 登记表：换租户仍认、机器不跨租户、按用户列/删
# ---------------------------------------------------------------------------
def test_claim_survives_tenant_change():
    """换租户后 `claim` 仍然放行 —— 这是本次改动的正题。

    变异：把 `same_owner` 改成整体比元组，这条必须红。
    """
    reg = SessionRegistry(enabled=True)
    ok, owner = reg.claim("t1", "globex", "alice")
    assert (ok, owner) == (True, ("globex", "alice"))
    ok2, owner2 = reg.claim("t1", "acme", "alice")
    assert ok2 is True, "换租户后旧会话被判成别人的"
    assert owner2 == ("globex", "alice"), "属主应该还是最初那个，不该被改写"


def test_claim_still_rejects_other_user():
    reg = SessionRegistry(enabled=True)
    reg.claim("t1", "acme", "alice")
    assert reg.claim("t1", "acme", "bob")[0] is False


def test_claim_machine_caller_still_isolated_by_tenant():
    """机器调用方（user 空）不能被放开。

    变异：把判据改成"一律只比 user"，这条必须红 —— 而症状是跨租户串会话。
    """
    reg = SessionRegistry(enabled=True)
    reg.claim("t1", "acme", "")
    assert reg.claim("t1", "globex", "")[0] is False


def test_threads_of_owner_only_returns_own_and_empty_user_means_nobody():
    reg = SessionRegistry(enabled=True)
    reg.claim("a1", "acme", "alice")
    reg.claim("a2", "globex", "alice")     # 同一个人，另一个租户
    reg.claim("b1", "acme", "bob")
    reg.claim("m1", "acme", "")            # 机器调用方

    assert sorted(reg.threads_of_owner("alice")) == ["a1", "a2"]
    assert reg.threads_of_owner("bob") == ["b1"]
    assert reg.threads_of_owner("") == [], \
        "空 user 必须返回空列表，不能把机器会话全交出去"
    assert reg.threads_of_owner("nobody") == []


def test_release_owner_does_not_touch_others():
    reg = SessionRegistry(enabled=True)
    reg.claim("a1", "acme", "alice")
    reg.claim("b1", "acme", "bob")
    reg.claim("m1", "acme", "")

    assert reg.release_owner("alice") == 1
    assert reg.owner_of("a1") is None
    assert reg.owner_of("b1") is not None, "删别人的会话"
    assert reg.owner_of("m1") is not None, "空 user 时不能一把全删"
    assert reg.release_owner("") == 0


# ---------------------------------------------------------------------------
# 3. 端到端：换租户后旧会话可读
# ---------------------------------------------------------------------------
def _store():
    st = accounts.AccountStore(dsn="")
    st.create_tenant("acme")
    st.create_tenant("globex")
    for name, tenant, roles in (("root", "acme", ADMIN_ROLES),
                                ("carol", "globex", ()),
                                ("bob", "acme", ())):
        st.create_user(name, A.hash_password(f"pw-{name}", iterations=_TEST_ITER),
                       tenant=tenant, roles=roles)
    return st


@pytest.fixture
def env(monkeypatch):
    store = _store()
    monkeypatch.setattr(config, "AUTH_USERS", {})
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    monkeypatch.setattr(config, "AUTH_SESSION_SECRET", "t" * 48)
    monkeypatch.setattr(config, "AUTH_REVIEWER_ROLES", ("reviewer",))
    monkeypatch.setattr(config, "AUTH_ADMIN_ROLES", ADMIN_ROLES)
    service = A.AuthService(A.SessionStore(enabled=True), accounts=store)
    reg = SessionRegistry(enabled=True)
    monkeypatch.setattr(M, "_AUTH", service)
    monkeypatch.setattr(M, "_ACCOUNTS", store)
    monkeypatch.setattr(M, "_SESSIONS", reg)
    return {"store": store, "service": service, "sessions": reg}


@pytest.fixture
def client(env):
    with TestClient(M.app) as c:
        yield c


def _login(client, username):
    r = client.post("/v1/auth/login",
                    json={"username": username, "password": f"pw-{username}"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_old_session_readable_after_tenant_change(client, env):
    """换租户 + 重新登录之后，旧会话**归属校验必须仍通过**。

    改之前这条是 403：归属按 `(tenant, user)` 整体比，而旧行记的是旧租户。
    注意中间必须重新登录一次 —— 改租户会撤销旧令牌（既有行为，见
    `test_user_auth.test_admin_user_crud_and_tenant_change_revokes_sessions`），
    所以"用旧令牌继续访问"测的不是这件事。

    ⚠️ 为什么用 `DELETE` 而不是 `GET` 断言：`GET /v1/sessions/{id}` 在"有归属
    但没有运行现场、没有待审、没有历史"时返回 404，那个 404 与归属**无关**。
    用它断言会把"归属通过了"测成红色，逼着人去改错地方。`DELETE` 只依赖归属校验。
    """
    h_admin = _login(client, "root")
    thread = "thread-tenant-change"

    # 用她的令牌认领一个会话（真实端点里由 /chat 的 preflight 做，这里直接登记，
    # 避免为了测归属而真的跑一轮模型）
    assert env["sessions"].claim(thread, "globex", "carol")[0] is True
    M._assert_session_readable(thread, "globex", "carol")   # 换租户前：通过

    # 管理员把她挪到另一个租户
    r = client.patch("/v1/admin/users/carol", json={"tenant": "acme"}, headers=h_admin)
    assert r.status_code == 200, r.text

    h_carol2 = _login(client, "carol")
    assert client.get("/v1/auth/me", headers=h_carol2).json()["tenant"] == "acme"

    r2 = client.delete(f"/v1/sessions/{thread}", headers=h_carol2)
    assert r2.status_code == 200, (
        f"换租户后自己的旧会话读不到了（{r2.status_code}）—— 归属判据又回到按租户比了"
    )


def test_other_user_still_forbidden_after_tenant_change(client, env):
    """换租户不能顺带把别人的会话也打开 —— 判据放宽的边界必须在这一侧。"""
    h_admin = _login(client, "root")
    thread = "thread-alices"
    assert env["sessions"].claim(thread, "globex", "carol")[0] is True

    # bob 在 acme：既不是同一用户，也不是同一租户
    h_bob = _login(client, "bob")
    assert client.delete(f"/v1/sessions/{thread}", headers=h_bob).status_code == 403

    # 把 bob 也挪到 globex（同租户）后仍然不许 —— 这条守"只放宽租户、不放宽人"
    assert client.patch("/v1/admin/users/bob", json={"tenant": "globex"},
                        headers=h_admin).status_code == 200
    h_bob2 = _login(client, "bob")
    assert client.delete(f"/v1/sessions/{thread}", headers=h_bob2).status_code == 403


# ---------------------------------------------------------------------------
# 4. 删用户：同名重建不继承
# ---------------------------------------------------------------------------
def test_delete_session_endpoint_clears_ownership(client, env):
    """删**单个会话**时，归属行也必须删掉。

    为什么要单独一条（删用户的用例守不住它）：删用户那条路径上还有
    `release_owner` 兜底，它会把归属行一并清掉 —— 于是"`_purge_thread` 漏删归属"
    这个洞在删用户的用例里**结果相同、看不出来**。只有走"删单个会话"才暴露。

    变异：把 `_purge_thread` 的 `ownership` 改成常量 True，这条必须红。
    """
    h_carol = _login(client, "carol")
    thread = "thread-single-delete"
    assert env["sessions"].claim(thread, "globex", "carol")[0] is True

    r = client.delete(f"/v1/sessions/{thread}", headers=h_carol)
    assert r.status_code == 200, r.text
    assert r.json()["deleted"]["ownership"] is True
    assert env["sessions"].owner_of(thread) is None, \
        "归属行没删 —— 这个 thread_id 会永久占位，谁也再用不了"


def test_deleted_user_sessions_not_inherited_by_same_name(client, env):
    """删号后用同名重建，读不到旧会话。

    为什么这条必须测：`users.username` 是主键，删掉之后能再建一个同名账号，
    而归属键就是用户名。不清归属行的话，新账号**直接继承**旧账号的会话 ——
    静默、无报错，旧对话里可能什么都有。

    变异：把端点里 `release_owner` 那一步删掉，这条必须红。
    """
    h_admin = _login(client, "root")
    thread = "thread-bob-old"

    assert env["sessions"].claim(thread, "acme", "bob")[0] is True
    M._assert_session_readable(thread, "acme", "bob")

    r = client.delete("/v1/admin/users/bob", headers=h_admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] is True
    assert thread in body["purge"], "删用户时必须逐个回报清掉了哪些会话"
    assert body["purge"][thread]["ownership"] is True, "归属行没删 —— 同名重建会继承"

    # 同名重建（密码得换一个：接口有最短长度校验）
    assert client.post("/v1/admin/users",
                       json={"username": "bob", "password": "pw-bob-rebuilt",
                             "tenant": "acme", "roles": []}, headers=h_admin
                       ).status_code == 200
    r3 = client.post("/v1/auth/login",
                     json={"username": "bob", "password": "pw-bob-rebuilt"})
    assert r3.status_code == 200, r3.text
    h_bob2 = {"Authorization": f"Bearer {r3.json()['access_token']}"}
    # 归属被清 ⇒ 这个 thread_id 对她就是"不存在"（多租户 fail-closed 回 404）。
    # 归属还在的话这里会是 200 —— 那正是"同名账号继承死人会话"的样子。
    r2 = client.delete(f"/v1/sessions/{thread}", headers=h_bob2)
    assert r2.status_code == 404, (
        "同名重建的账号读到了旧账号的会话 —— 归属行没被清掉"
    )
    assert env["sessions"].owner_of(thread) is None
