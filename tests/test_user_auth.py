"""第二档用户鉴权：端到端行为与安全红线。

覆盖目标（每条都对应一个"改错了不会有运行时症状"的点）：
  1. 未启用时带 Bearer 必须被明确拒绝，而不是静默退回 API Key 路径；
  2. 多设备并存 —— 同用户的多个会话同时有效，登出只影响自己那一台；
  3. refresh 单次使用 + 轮换（被截获的 refresh 不能长期复用），且**不新增会话**；
  4. 资源级授权 —— 审核需要角色，且 403 必须**先于**业务校验发生；
  5. **同租户内跨用户**读不到别人的会话（隔离下沉到用户）；
  6. 公开算法（alg=none）与错密钥伪造必须被拒；
  7. 已删除的两条兜底（共享密钥 `X-API-Key`、匿名放行）**真的没了** ——
     这一节是可执行的"删除记录"，防止它在某次重构里被顺手加回来。

配合 `tests/test_bm25.py` 的做法：这里只断言"线格式"（状态码 + detail 里的 code），
不调私有辅助函数 —— 后者改了实现也照样绿。
"""

import base64
import inspect
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import accounts, config
from app import auth as A
from app import main as M
from app.sessions import SessionRegistry

# 测试用的 PBKDF2 迭代数：最小值（100k）。再低会被 `verify_password` 的
# 安全下限拒掉 —— 那个下限本身有专门的用例（见 test_auth_module.py）。
_TEST_ITER = 100_000


@pytest.fixture
def env(monkeypatch):
    """启用用户鉴权：两个同租户用户（一个有 reviewer 角色，一个没有）。

    ⚠️ 用户现在住在**账号表**里（`app/accounts.py` 的 `AccountStore`），不再是
    `.env` 的 `AUTH_USERS` —— 后者已降级为"只在库为空时建出第一个管理员"的种子。
    所以这里造一个**内存后端**的 `AccountStore` 注入 `_AUTH` 与 `_ACCOUNTS`：
    走的是与生产完全相同的代码路径（`AuthService.login_with_password`），
    只是存储落在进程内存。
    """
    store = accounts.AccountStore(dsn="")
    # 租户行要先建出来：`create_user` **不校验**租户是否存在（那是管理接口的职责），
    # 但管理接口里的每一步（改名 / 删租户 / 把用户移进某租户）都要求它存在。
    store.create_tenant("acme")
    store.create_user("alice", A.hash_password("pw-alice", iterations=_TEST_ITER),
                      tenant="acme", roles=("reviewer",))
    store.create_user("bob", A.hash_password("pw-bob", iterations=_TEST_ITER),
                      tenant="acme", roles=())

    monkeypatch.setattr(config, "AUTH_USERS", {})        # 种子留空：用户全在账号表里
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    monkeypatch.setattr(config, "AUTH_SESSION_SECRET", "t" * 48)
    monkeypatch.setattr(config, "AUTH_REVIEWER_ROLES", ("reviewer",))
    monkeypatch.setattr(config, "AUTH_ADMIN_ROLES", ("admin",))

    service = A.AuthService(A.SessionStore(enabled=True), accounts=store)
    monkeypatch.setattr(M, "_AUTH", service)
    monkeypatch.setattr(M, "_ACCOUNTS", store)
    registry = SessionRegistry(enabled=True)
    monkeypatch.setattr(M, "_SESSIONS", registry)
    return {"service": service, "registry": registry,
            "store": store, "accounts": store}


@pytest.fixture
def client(env):
    # 走完整 lifespan：验证"装配路径本身"也是可用的（不只测函数）。
    with TestClient(M.app) as c:
        yield c


def _login(client, username, password):
    r = client.post("/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. 未启用用户鉴权
# ---------------------------------------------------------------------------
def test_bearer_rejected_when_user_auth_disabled(monkeypatch):
    """未配置 AUTH_* 时带 Bearer 必须被**明确拒绝**。

    不能"忽略它、当成没带凭证"：那会让客户端以为用户身份生效了，而服务端其实
    处于一个谁也进不来的状态 —— 症状从"明确的 401 且写明原因"退化成
    "401 但分不清是没登录还是服务没配"。
    """
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    with TestClient(M.app) as c:
        r = c.get("/v1/auth/me", headers=_bearer("whatever"))
    assert r.status_code == 401
    assert "未启用用户鉴权" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 2. 登录与身份回显
# ---------------------------------------------------------------------------
def test_login_then_me(client):
    tok = _login(client, "alice", "pw-alice")
    r = client.get("/v1/auth/me", headers=_bearer(tok["access_token"]))
    assert r.status_code == 200
    body = r.json()
    assert body["user"] == "alice"
    assert body["tenant"] == "acme"
    # ⚠️ 反向锁：身份回显里不该再有"认证方式"字段 —— 它只剩会话令牌一种取值，
    # 留着会让客户端以为还存在别的凭证形态。
    assert "via" not in body, "身份回显又出现了 via 字段（认证方式已只剩一种）"
    assert body["roles"] == ["reviewer"]
    # ⚠️ 反向锁：`session_bound` 同样是"恒为定值的死字段"（验签要求 `jti` ⇒ 能过鉴权的
    # 身份必然挂着会话），2026-10-05 一并删掉。它比 `via` 更值得钉：它是**对外响应字段**，
    # 一旦有人为了"向后兼容"把它加回来，客户端就会重新以为"存在没有会话的已认证身份"。
    assert "session_bound" not in body, (
        "身份回显又出现了 session_bound 字段（它恒为 true，且暗示存在无会话的已认证身份）"
    )


def test_login_response_does_not_leak_password_hash(client):
    tok = _login(client, "alice", "pw-alice")
    assert "pbkdf2" not in json.dumps(tok)
    assert "password" not in json.dumps(tok)


def test_wrong_password_and_unknown_user_are_indistinguishable(client):
    """用户名是否存在不能从响应里读出来（否则可枚举有效账号）。"""
    r1 = client.post("/v1/auth/login", json={"username": "alice", "password": "nope"})
    r2 = client.post("/v1/auth/login", json={"username": "ghost", "password": "nope"})
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["detail"] == r2.json()["detail"]


def test_claimed_tenant_must_match_token(client):
    """请求头只能"确认"租户，不能"声明" —— 与 API Key 路径同口径。"""
    tok = _login(client, "alice", "pw-alice")
    h = _bearer(tok["access_token"])
    h["X-Tenant-Id"] = "globex"
    assert client.get("/v1/auth/me", headers=h).status_code == 403


# ---------------------------------------------------------------------------
# 3. 多设备并存（2026-10-01 起**不再**互斥）
# ---------------------------------------------------------------------------
def test_multiple_devices_can_be_logged_in_at_once(client):
    """同一用户在多台设备上同时登录 —— 两个会话**都有效**。

    ⚠️ 这条钉住的是**语义反转**：此前同一用户第二次登录会顶掉第一次
    （单会话互斥），改动后必须两个都在。谁要是把覆盖写回去，这条立刻红。
    """
    first = _login(client, "alice", "pw-alice")
    second = _login(client, "alice", "pw-alice")
    assert first["access_token"] != second["access_token"]

    h1 = _bearer(first["access_token"])
    h2 = _bearer(second["access_token"])
    assert client.get("/v1/auth/me", headers=h1).status_code == 200, \
        "第二次登录把第一次顶掉了 —— 多设备并存失效"
    assert client.get("/v1/auth/me", headers=h2).status_code == 200


def test_logout_only_affects_current_device(client):
    """登出只移除**这一台**设备，同用户在其它设备上的会话不受影响。"""
    first = _login(client, "alice", "pw-alice")
    second = _login(client, "alice", "pw-alice")
    h1 = _bearer(first["access_token"])
    h2 = _bearer(second["access_token"])

    r = client.post("/v1/auth/logout", headers=h1)
    assert r.status_code == 200 and r.json()["revoked"] is True

    assert client.get("/v1/auth/me", headers=h1).status_code == 401
    assert client.get("/v1/auth/me", headers=h2).status_code == 200, \
        "登出设备1 把设备2 一起踢掉了"


def test_logout_all_devices(client):
    """`?all=true` 清空该用户的**全部**设备（互斥被移除后的补偿能力）。"""
    headers = [_bearer(_login(client, "alice", "pw-alice")["access_token"])
               for _ in range(3)]
    bob = _bearer(_login(client, "bob", "pw-bob")["access_token"])

    r = client.post("/v1/auth/logout?all=true", headers=headers[0])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revoked"] is True
    assert body["revoked_count"] == 3, f"应清除 3 个会话，实际 {body['revoked_count']}"

    for i, h in enumerate(headers, 1):
        assert client.get("/v1/auth/me", headers=h).status_code == 401, f"设备{i} 未失效"

    # ⚠️ 作用域必须只到当前用户 —— 否则它就成了一个人人可用的"任意踢人"接口
    assert client.get("/v1/auth/me", headers=bob).status_code == 200, \
        "logout?all=true 影响到了其他用户"


def test_repeated_logout_cannot_touch_other_sessions(client):
    """拿一个**已失效**的旧 token 反复调登出，不能打掉同用户的其他设备。

    这是原 CAS 设计要防的那件事（旧 token 反复登出 ⇒ 持续打掉受害者的新会话）。
    现在防它的机制变了：不是 CAS，而是"失效凭证连鉴权都过不去"。
    **保护效果必须一样**，所以这条用例保留下来。
    """
    first = _login(client, "alice", "pw-alice")
    second = _login(client, "alice", "pw-alice")
    h_old = _bearer(first["access_token"])
    h_new = _bearer(second["access_token"])

    client.post("/v1/auth/logout", headers=h_old)
    for _ in range(3):
        r = client.post("/v1/auth/logout", headers=h_old)
        assert r.status_code == 401, "已失效的凭证竟然还能执行登出"
    assert client.get("/v1/auth/me", headers=h_new).status_code == 200, \
        "反复登出把同用户的其他会话打掉了"


def test_sessions_are_isolated_across_users(client):
    """同租户下的不同用户，会话互不干扰（归属粒度是 `(tenant, user)`）。"""
    alice = _login(client, "alice", "pw-alice")
    bob = _login(client, "bob", "pw-bob")
    ha = _bearer(alice["access_token"])
    hb = _bearer(bob["access_token"])

    client.post("/v1/auth/logout?all=true", headers=ha)      # alice 清场
    assert client.get("/v1/auth/me", headers=ha).status_code == 401
    assert client.get("/v1/auth/me", headers=hb).status_code == 200, \
        "alice 的清场动作影响到了 bob"


# ---------------------------------------------------------------------------
# 4. refresh：轮换 + 单次使用 + 不改变会话身份
# ---------------------------------------------------------------------------
def test_refresh_rotates_and_old_refresh_dies(client):
    tok = _login(client, "alice", "pw-alice")
    r = client.post("/v1/auth/refresh", json={"refresh_token": tok["refresh_token"]})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["refresh_token"] != tok["refresh_token"]

    # 旧 refresh 必须失效（单次使用）
    r2 = client.post("/v1/auth/refresh", json={"refresh_token": tok["refresh_token"]})
    assert r2.status_code == 401, "旧 refresh token 可以重复使用 —— 轮换形同虚设"

    # ⚠️ 与上一版不同：刷新**不再**换 sid ⇒ 刷新前后两个 access 属于同一个会话，
    # 于是旧的 access 依然可用。（旧版"刷新即新登录、旧 access 立刻失效"的
    # 断言必须删掉，否则它会锁死一个我们**刻意不要**的语义。）
    assert client.get("/v1/auth/me",
                      headers=_bearer(tok["access_token"])).status_code == 200
    assert client.get("/v1/auth/me",
                      headers=_bearer(new["access_token"])).status_code == 200


def test_refresh_keeps_the_same_session(client, env):
    """刷新**不得**新建会话 —— 否则白名单会随刷新次数**无界增长**。

    一个长期在线的客户端每小时刷新一次，几天就能堆出上千条永不回收的会话键。
    这与本模块当初拒绝"撤销列表"是同一个理由 —— 所以必须有一条用例钉死它。
    """
    store = env["service"].store

    def sessions():
        return sum(1 for k in store.backend._data if k.startswith("auth:sess:"))

    tok = _login(client, "alice", "pw-alice")
    before = sessions()
    assert before == 1

    for _ in range(5):
        r = client.post("/v1/auth/refresh", json={"refresh_token": tok["refresh_token"]})
        assert r.status_code == 200, r.text
        tok = r.json()

    assert sessions() == before, f"5 次刷新后会话数由 {before} 变成了 {sessions()}"


def test_logged_out_session_cannot_refresh_back_to_life(client):
    """登出之后，手里的 refresh 不能把会话复活。

    旧 refresh 会随会话一起失效 —— 不存在"已经登出了，但攻击者还能拿截获的
    refresh 换出新 token"这条路。
    """
    tok = _login(client, "alice", "pw-alice")
    h = _bearer(tok["access_token"])
    client.post("/v1/auth/logout", headers=h)

    r = client.post("/v1/auth/refresh", json={"refresh_token": tok["refresh_token"]})
    assert r.status_code == 401, "登出后仍能用 refresh 换到新 token"
    assert "session_revoked" in r.json()["detail"]


def test_refresh_single_use_is_independent_of_session_state(client):
    """**会话全程有效**的前提下，旧 refresh 也必须失效（单次使用独立成立）。

    为什么要单独一条（2026-10-01 变异验证的教训）：
    "旧 refresh 不能再用"这个**现象**会被另一个机制顺带拦下 —— 只要刷新会改动
    会话状态，第二次使用旧 refresh 就会先撞上那个机制，用例于是全绿，而 refresh
    自己的"消费即删除"**根本没被验证**。实测佐证：把 `consume_refresh` 的 `pop`
    改成 `get`（不再删除）后，只断言"被拒"的用例照样通过。

    所以这条必须先**排除**会话被撤销这条路径：期间不登出、会话始终有效，
    于是那个 401 只可能来自"refresh 已被消费"—— 用 `code` 断言钉死。
    """
    tok = _login(client, "alice", "pw-alice")
    r = client.post("/v1/auth/refresh", json={"refresh_token": tok["refresh_token"]})
    assert r.status_code == 200

    r2 = client.post("/v1/auth/refresh", json={"refresh_token": tok["refresh_token"]})
    assert r2.status_code == 401, "旧 refresh token 可以重复使用 —— 轮换形同虚设"
    assert "invalid_refresh_token" in r2.json()["detail"], (
        f"被拒的原因不是「refresh 已用过」，而是别的机制：{r2.json()['detail']}"
    )


def test_garbage_refresh_token_is_rejected(client):
    r = client.post("/v1/auth/refresh", json={"refresh_token": "not-a-real-token"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 5. 资源级授权：审核需要角色
# ---------------------------------------------------------------------------
def test_review_requires_role_and_check_precedes_business_logic(client):
    alice = _login(client, "alice", "pw-alice")      # 有 reviewer
    bob = _login(client, "bob", "pw-bob")            # 没有

    body = {"thread_id": "not-exist", "action": "accept"}

    r = client.post("/v1/review", json=body, headers=_bearer(bob["access_token"]))
    assert r.status_code == 403, "没有审核角色的人竟然进到了业务逻辑"
    assert "审核权限" in r.json()["detail"]

    # 有角色的人**不能**因为角色被拒 —— 这条同样是必需的：
    # 只断言"bob 403"的话，一个"把所有人都 403"的实现也能过。
    r2 = client.post("/v1/review", json=body, headers=_bearer(alice["access_token"]))
    assert r2.status_code != 403, f"有审核角色却仍被拒：{r2.status_code} {r2.text}"


def test_review_role_check_is_enforced_for_every_identity_kind(client):
    """角色检查对所有身份一视同仁 —— 曾经的"非 JWT 身份直接放行"豁免已删除。

    此前有一条刻意豁免：持共享密钥（`X-API-Key`）的调用方跳过角色检查，理由是
    "脚本通行证是服务端到服务端的凭证，对它强制角色会让压测脚本立刻 403"。
    共享密钥路径整体删除后，被豁免的对象不存在了，豁免也随之消失。

    所以这条用例从**反面**钉住它：`X-API-Key` 已经不是一个可用的凭证，
    带它请求只会得到 401。一个"以为自己还持有通行证"的调用方必须立刻失败 ——
    而不是拿到某种"半开"状态（既没登录成功、又进了业务逻辑，那是最危险的）。
    """
    r = client.post("/v1/review",
                    json={"thread_id": "not-exist", "action": "accept"},
                    headers={"X-API-Key": "k" * 24})
    assert r.status_code == 401, "X-API-Key 已不是凭证，带它不该有任何通道"


def test_reviews_list_requires_reviewer_role(client):
    """**只读**的待审列表同样要查角色 —— 它与写侧的强度此前不一致。

    `/v1/review`（写）一直查角色，而 `/v1/reviews`（读）此前只验签、不查角色。
    于是"看到全部待审草稿正文、评审意见与评分"变成任何登录账号都能做的事。

    读比写门槛低是最容易被忽略的一类漏洞：它不产生副作用，所以没人觉得它
    需要授权；但待审草稿里躺着的是**别人还没发布的内容**，读到就是泄露。
    """
    bob = _login(client, "bob", "pw-bob")            # 无角色
    r = client.get("/v1/reviews", headers=_bearer(bob["access_token"]))
    assert r.status_code == 403, f"无审核角色的人读到了待审列表：{r.status_code}"
    assert "审核权限" in r.json()["detail"]

    # 有角色的人不能被一起挡掉 —— 否则"把所有人都 403"也能让上面那句变绿
    alice = _login(client, "alice", "pw-alice")
    r2 = client.get("/v1/reviews", headers=_bearer(alice["access_token"]))
    assert r2.status_code == 200, r2.text
    assert "items" in r2.json()


def test_reviews_list_scopes_to_identity_tenant(client, monkeypatch):
    """待审列表必须只返回**身份自己租户**的记录。

    这里钉的是本轮修掉的跨租户泄露口：列表端点曾把身份的原始租户直接交给队列，
    而队列对空串的语义是"不加过滤" ⇒ 任何还没有租户的账号（`tenant=""` 是正常
    运营中必然存在的状态）能列出**全部租户**的待审草稿。

    用例刻意用**两个都有内容的租户**：只放一条记录的话，一个"什么都返回"的
    实现也能过。
    """
    from app.review_queue import ReviewQueue

    q = ReviewQueue(dsn="", enabled=True)
    q.add("t-acme", draft="acme 的草稿", score=30, feedback="", tenant="acme")
    q.add("t-globex", draft="globex 的草稿", score=30, feedback="", tenant="globex")
    q.add("t-empty", draft="无租户的草稿", score=30, feedback="", tenant="")
    monkeypatch.setattr(M, "_REVIEW_QUEUE", q)

    alice = _login(client, "alice", "pw-alice")      # 租户 acme
    r = client.get("/v1/reviews", headers=_bearer(alice["access_token"]))
    assert r.status_code == 200, r.text
    tenants = {i["tenant"] for i in r.json()["items"]}
    assert tenants == {"acme"}, (
        f"待审列表返回了不属于本租户的记录：{tenants}\n"
        "空租户尤其危险 —— 它曾被当成「不加过滤」，等于把全库草稿交出去。"
    )


# ---------------------------------------------------------------------------
# 6. 隔离下沉：同租户内的两个用户之间
# ---------------------------------------------------------------------------
def test_session_not_readable_across_users_in_same_tenant(client, env):
    """同租户、不同用户 → 403。只测跨租户是不够的（那种用例在只比 tenant 时也能过）。"""
    env["registry"].claim("t-shared", "acme", "alice")
    alice = _login(client, "alice", "pw-alice")
    bob = _login(client, "bob", "pw-bob")

    r = client.get("/v1/sessions/t-shared", headers=_bearer(bob["access_token"]))
    assert r.status_code == 403, "同租户的另一个用户读到了别人的会话"

    r2 = client.get("/v1/sessions/t-shared", headers=_bearer(alice["access_token"]))
    assert r2.status_code != 403, "会话属主本人被自己的会话拒绝了"


def test_delete_session_not_allowed_across_users(client, env):
    env["registry"].claim("t-del", "acme", "alice")
    bob = _login(client, "bob", "pw-bob")
    r = client.delete("/v1/sessions/t-del", headers=_bearer(bob["access_token"]))
    assert r.status_code == 403
    assert env["registry"].owner_of("t-del") == ("acme", "alice"), \
        "无权限的请求竟然改变了归属"


# ---------------------------------------------------------------------------
# 7. 算法与伪造
# ---------------------------------------------------------------------------
def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_alg_none_token_is_rejected(client):
    """`alg=none` 是最古老的 JWT 绕过手法，必须被算法白名单挡住。"""
    head = _b64(json.dumps({"alg": "none", "typ": "JWT", "kid": "k1"}).encode())
    body = _b64(json.dumps({"sub": "alice", "tenant": "acme",
                            "iss": config.AUTH_ISSUER,
                            "exp": int(time.time()) + 3600}).encode())
    r = client.get("/v1/auth/me", headers=_bearer(f"{head}.{body}."))
    assert r.status_code == 401


def test_forged_hs256_with_wrong_secret_is_rejected(client):
    """用错误的密钥签 HS256（模拟攻击者猜测会话密钥）→ 401。"""
    import hashlib
    import hmac

    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps({"sub": "alice", "tenant": "acme",
                            "iss": config.AUTH_ISSUER, "jti": "x",
                            "exp": int(time.time()) + 3600}).encode())
    signing = f"{head}.{body}".encode()
    sig = _b64(hmac.new(b"wrong-secret", signing, hashlib.sha256).digest())
    r = client.get("/v1/auth/me", headers=_bearer(f"{head}.{body}.{sig}"))
    assert r.status_code == 401


def test_expired_session_token_is_rejected(client, env, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ACCESS_TTL", 1)
    tok = _login(client, "alice", "pw-alice")
    time.sleep(1.2)
    r = client.get("/v1/auth/me", headers=_bearer(tok["access_token"]))
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 8. 已删除的两条兜底：共享密钥（X-API-Key）与匿名放行
# ---------------------------------------------------------------------------
# 2026-10-05 之前，服务端除 JWT 外还有两条"兜底"：
#   · 共享密钥脚本通行证（`X-API-Key` ↔ `SERVE_API_KEY`）；
#   · `REQUIRE_AUTH=0` 时的匿名放行。
# 两条都已**物理删除**（配置项一并删掉），现在业务接口只认一种身份来源。
#
# 下面这几条不是"测旧功能"，而是把"删除"这件事变成可执行断言：删掉一个鉴权
# 入口后最容易发生的回归，不是它坏掉，而是它在某次重构里被"顺手加回来"。
def test_api_key_header_is_not_a_credential_anymore(client):
    """`X-API-Key` 不再是任何形式的凭证 ⇒ 401（即便值"看起来像"一把密钥）。

    为什么这条必须存在：删掉一条鉴权路径后，最危险的状态不是"它报错"，
    而是"它还能用一点点" —— 例如某处仍残留一次密钥比较、失败后**继续往下走**。
    那会表现为"未登录也能调接口"，而且没有任何症状。
    """
    r = client.get("/v1/auth/me", headers={"X-API-Key": "k" * 24})
    assert r.status_code == 401, "共享密钥路径被重新引入了"


def test_tenant_header_alone_cannot_claim_a_tenant(client):
    """只有 `X-Tenant-Id`、没有令牌 ⇒ 401：租户**不能**由客户端自报。

    这正是共享密钥路径最危险的性质 —— 它有租户概念，而租户取自请求头。
    改成 JWT 之后 `X-Tenant-Id` 只能**确认**（不符即 403，见
    `test_claimed_tenant_must_match_token`）。本用例钉住"单独一个 X-Tenant-Id
    什么都换不到"，否则它可能在下次重构里又变回"租户的来源"。
    """
    r = client.get("/v1/auth/me", headers={"X-Tenant-Id": "acme"})
    assert r.status_code == 401


def test_authenticate_takes_no_shared_key_parameter():
    """签名绊线：`_authenticate` 一旦重新收下 `x_api_key`，这条立刻红。

    为什么用签名而不是行为来钉：行为上"配了密钥才生效"，不配时完全看不见；
    而签名是**结构性**的 —— 函数收下一个密钥参数，就等于承认"存在第二种凭证
    入口"，接下来必然有人去配它。这条把"不许有第二种入口"钉在类型层面。
    """
    params = set(inspect.signature(M._authenticate).parameters)
    assert params == {"x_tenant_id", "authorization"}, (
        f"_authenticate 的入参变成了 {sorted(params)} —— "
        "任何与凭证有关的额外参数都意味着第二种身份来源被重新引入"
    )


# ---------------------------------------------------------------------------
# 9. 账号管理：注册 / 改密码 / 加入申请 / 管理员 CRUD
# ---------------------------------------------------------------------------
def _admin(env, client, username="root", tenant="acme"):
    """造一个管理员并返回它的 Bearer 头。

    fixture 里只建了 alice（reviewer）与 bob（无角色），都够不到管理接口 ——
    管理是 fail-closed 的（`AUTH_ADMIN_ROLES=("admin",)`），所以这里单独建一个。
    """
    env["store"].create_user(username,
                             A.hash_password("pw-root", iterations=_TEST_ITER),
                             tenant=tenant, roles=("admin",))
    tok = _login(client, username, "pw-root")
    return _bearer(tok["access_token"])


def test_self_register_is_closed_by_default(client):
    """默认**关闭**自助注册 —— 因为注册出来的账号无租户，无租户按当前口径能读全库。"""
    r = client.post("/v1/auth/register", json={"username": "eve", "password": "pw-eve01"})
    assert r.status_code == 403
    assert "未开启" in r.json()["detail"]


def test_self_register_creates_a_tenantless_user_without_token(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ALLOW_SELF_REGISTER", True)
    r = client.post("/v1/auth/register", json={"username": "eve", "password": "pw-eve01"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant"] == "" and body["roles"] == []
    assert "access_token" not in body, "注册接口不该签发令牌（少一条囤积会话的路径）"

    tok = _login(client, "eve", "pw-eve01")         # 新账号自己去换令牌
    me = client.get("/v1/auth/me", headers=_bearer(tok["access_token"]))
    assert me.status_code == 200
    assert me.json()["tenant"] == "", "自助注册的账号不应带租户"


def test_changing_password_requires_the_old_one(client):
    tok = _login(client, "alice", "pw-alice")
    r = client.post("/v1/auth/password",
                    json={"old_password": "wrong", "new_password": "pw-new01"},
                    headers=_bearer(tok["access_token"]))
    assert r.status_code == 401
    assert "当前密码不正确" in r.json()["detail"]


def test_changing_password_revokes_sessions_and_new_password_works(client):
    tok = _login(client, "alice", "pw-alice")
    h = _bearer(tok["access_token"])
    r = client.post("/v1/auth/password",
                    json={"old_password": "pw-alice", "new_password": "pw-new01"},
                    headers=h)
    assert r.status_code == 200, r.text

    assert client.get("/v1/auth/me", headers=h).status_code == 401, \
        "改密码后旧会话仍可用 —— 那改密码就失去了赶走入侵者的意义"
    _login(client, "alice", "pw-new01")             # 新密码可登录
    r2 = client.post("/v1/auth/login",
                     json={"username": "alice", "password": "pw-alice"})
    assert r2.status_code == 401, "旧密码竟然还能登录"


def test_admin_tenant_crud(client, env):
    h = _admin(env, client)
    r = client.post("/v1/admin/tenants", json={"name": "globex", "note": "试点"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "globex"

    # 重名 => 409（不是 500，也不是静默成功）
    assert client.post("/v1/admin/tenants", json={"name": "globex"},
                       headers=h).status_code == 409

    got = client.get("/v1/admin/tenants/globex", headers=h)
    assert got.status_code == 200 and got.json()["user_count"] == 0

    lst = client.get("/v1/admin/tenants", headers=h)
    assert any(t["name"] == "globex" for t in lst.json()["tenants"])

    ren = client.patch("/v1/admin/tenants/globex", json={"new_name": "globex2"}, headers=h)
    assert ren.status_code == 200 and ren.json()["name"] == "globex2"

    assert client.delete("/v1/admin/tenants/globex2", headers=h).json()["deleted"] is True
    assert client.get("/v1/admin/tenants/globex2", headers=h).status_code == 404


def test_admin_rename_tenant_moves_attached_users(client, env):
    """改名必须**同批改写挂靠用户的租户字段** —— 否则他们指向一个不存在的租户。

    那个状态不报错，只表现为"他们突然搜不到东西了"（检索按租户过滤，过滤值
    指向一个没人往里灌过数据的桶）。
    """
    h = _admin(env, client)
    r = client.patch("/v1/admin/tenants/acme", json={"new_name": "acme2"}, headers=h)
    assert r.status_code == 200, r.text
    assert env["store"].get_user("alice")["tenant"] == "acme2"
    assert env["store"].get_user("bob")["tenant"] == "acme2"


def test_admin_delete_tenant_refuses_while_users_attached(client, env):
    h = _admin(env, client)                          # root 也在 acme
    r = client.delete("/v1/admin/tenants/acme", headers=h)
    assert r.status_code == 409
    assert "仍有" in r.json()["detail"]

    # 显式 force 才连带把人移出 —— 且这个副作用必须是**看得见**的
    r2 = client.delete("/v1/admin/tenants/acme?force=true", headers=h)
    assert r2.status_code == 200, r2.text
    assert r2.json()["deleted"] is True
    assert "alice" in r2.json()["orphaned_users"], "被移出租户的人必须被回报出来"
    assert env["store"].get_user("alice")["tenant"] == ""


def test_admin_user_crud_and_tenant_change_revokes_sessions(client, env):
    h = _admin(env, client)
    client.post("/v1/admin/tenants", json={"name": "globex"}, headers=h)
    r = client.post("/v1/admin/users",
                    json={"username": "carol", "password": "pw-carol",
                          "tenant": "globex", "roles": []}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["tenant"] == "globex"

    # 不存在的租户 => 404。否则她会落进一个没数据、也没人能管的桶，
    # 而那种状态从响应上看和成功**一模一样**。
    assert client.post("/v1/admin/users",
                       json={"username": "dave", "password": "pw-dave01",
                             "tenant": "nope"}, headers=h).status_code == 404

    tok = _login(client, "carol", "pw-carol")
    hc = _bearer(tok["access_token"])
    assert client.get("/v1/auth/me", headers=hc).json()["tenant"] == "globex"

    # 换租户 => 立刻断掉她现有的会话（JWT 里是**签发时快照**）
    r2 = client.patch("/v1/admin/users/carol", json={"tenant": "acme"}, headers=h)
    assert r2.status_code == 200, r2.text
    assert r2.json()["tenant"] == "acme"
    assert r2.json()["sessions_revoked"] >= 1
    assert client.get("/v1/auth/me", headers=hc).status_code == 401, \
        "改租户后旧令牌仍可用 —— 它仍带着旧租户，最长能再撑一个 access TTL"

    tok2 = _login(client, "carol", "pw-carol")
    assert client.get("/v1/auth/me",
                      headers=_bearer(tok2["access_token"])).json()["tenant"] == "acme"

    # 停用 => 登录被拒（与"密码错误"响应完全一致，防枚举）
    assert client.patch("/v1/admin/users/carol", json={"status": "disabled"},
                        headers=h).status_code == 200
    assert client.post("/v1/auth/login",
                       json={"username": "carol", "password": "pw-carol"}
                       ).status_code == 401

    assert client.delete("/v1/admin/users/carol", headers=h).json()["deleted"] is True
    assert client.get("/v1/admin/users/carol", headers=h).status_code == 404


def test_admin_cannot_delete_or_disable_self(client, env):
    """管理员误删/误停自己会把自己锁在外面，而恢复需要**另一个**管理员 —— 可能不存在。"""
    h = _admin(env, client)
    assert client.delete("/v1/admin/users/root", headers=h).status_code == 409
    r = client.patch("/v1/admin/users/root", json={"status": "disabled"}, headers=h)
    assert r.status_code == 409


def test_admin_endpoints_require_user_identity_and_role(client, env):
    # 没有任何凭证 ⇒ **401**（不是 403）。顺序是刻意的：先要"证明你是谁"，
    # 才谈得上"你有没有权限"。若匿名也走角色检查而返回 403，客户端会以为
    # "只要登录就有戏"，把"服务根本没开门"误判成"我权限不够"。
    assert client.get("/v1/admin/users").status_code == 401

    # 普通用户（无 admin 角色）=> 403
    bob = _login(client, "bob", "pw-bob")
    r = client.get("/v1/admin/users", headers=_bearer(bob["access_token"]))
    assert r.status_code == 403
    assert "没有管理权限" in r.json()["detail"]


def test_join_request_flow_approve_changes_tenant(client, env):
    h = _admin(env, client)
    client.post("/v1/admin/tenants", json={"name": "globex"}, headers=h)

    client.post("/v1/admin/users", json={"username": "frank", "password": "pw-frank"},
                headers=h)                            # 无租户用户
    tok = _login(client, "frank", "pw-frank")
    hf = _bearer(tok["access_token"])
    assert client.get("/v1/auth/me", headers=hf).json()["tenant"] == ""

    r = client.post("/v1/join-requests", json={"tenant": "globex"}, headers=hf)
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    assert r.json()["status"] == "pending"

    # 同一租户重复申请 => 409（否则申请队列可被刷）
    assert client.post("/v1/join-requests", json={"tenant": "globex"},
                       headers=hf).status_code == 409

    mine = client.get("/v1/join-requests/me", headers=hf).json()["requests"]
    assert [m["id"] for m in mine] == [rid], "自己只能看到自己的申请"

    pend = client.get("/v1/admin/join-requests?status=pending", headers=h).json()
    assert any(p["id"] == rid for p in pend["requests"])

    ok = client.post(f"/v1/admin/join-requests/{rid}", json={"approve": True}, headers=h)
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "approved"
    assert ok.json()["reviewer"] == "root"
    assert env["store"].get_user("frank")["tenant"] == "globex", \
        "批准了却没把人加进租户 —— 他会一直停在无租户（= 能读全库）"

    # 已处理过的申请不能重复处理，否则"先驳回、再批准"能绕过审批记录
    assert client.post(f"/v1/admin/join-requests/{rid}",
                       json={"approve": False}, headers=h).status_code == 409

    tok2 = _login(client, "frank", "pw-frank")
    assert client.get("/v1/auth/me",
                      headers=_bearer(tok2["access_token"])).json()["tenant"] == "globex"


def test_user_already_in_a_tenant_cannot_apply_elsewhere(client, env):
    """一人只能属于一个租户 —— 已在租户里的人不能再申请别的（换租户要走管理员）。"""
    h = _admin(env, client)
    client.post("/v1/admin/tenants", json={"name": "globex"}, headers=h)
    tok = _login(client, "alice", "pw-alice")          # alice 已在 acme
    r = client.post("/v1/join-requests", json={"tenant": "globex"},
                    headers=_bearer(tok["access_token"]))
    assert r.status_code == 409
    assert "already_in_tenant" in r.json()["detail"]


def test_join_request_endpoints_reject_anonymous(client):
    """入租申请必须来自**已登录的用户** —— 匿名一律 401，没有例外。

    这些接口的全部意义是"某个人申请进入某个租户"，所以它必须以"人"为前提。
    共享密钥身份（无法归因到人）此前被 403 挡在这里；那条路径删除后，挡在
    门口的就只剩 401 这一道，本用例把它钉住。
    """
    assert client.post("/v1/join-requests", json={"tenant": "acme"}).status_code == 401
    assert client.get("/v1/join-requests/me").status_code == 401



# ---------------------------------------------------------------------------
# 9b. 密码下限（2026-10-04 补）：只约束"新设的密码"，不波及登录
# ---------------------------------------------------------------------------
def test_register_rejects_a_password_below_the_floor(client, monkeypatch):
    """注册密码短于下限 ⇒ 422，且错误里写明下限值。

    此前只有上限（512），能设出 `"a"` 这种密码 —— 防住了 PBKDF2 对超长输入的开销，
    却没防弱密码。下限默认 8（NIST SP 800-63B 与 OWASP 的起点）。
    """
    monkeypatch.setattr(config, "AUTH_ALLOW_SELF_REGISTER", True)
    r = client.post("/v1/auth/register", json={"username": "eve", "password": "pw-1234"})
    assert r.status_code == 422
    assert f"至少需要 {config.MIN_PASSWORD_CHARS} 字符" in r.text


def test_change_password_rejects_a_short_new_password(client):
    """改密码时**只**拦新密码：旧密码刻意不受下限约束。

    这条不对称是刻意的 —— 下限的含义是"不许设新的弱密码"，不是"不许用已有的短密码"。
    若连旧密码也拦，早期设的短密码会被反锁在门外：连改成合规密码这一步都做不了。
    """
    tok = _login(client, "alice", "pw-alice")
    r = client.post("/v1/auth/password",
                    json={"old_password": "pw-alice", "new_password": "pw-1234"},
                    headers=_bearer(tok["access_token"]))
    assert r.status_code == 422
    assert "new_password" in r.text


def test_admin_create_user_rejects_a_short_password(client, env):
    """管理员建的账号也要过下限 —— 否则管理接口就是绕开这条策略的后门。"""
    h = _admin(env, client)
    r = client.post("/v1/admin/users",
                    json={"username": "shorty", "password": "pw-1234"}, headers=h)
    assert r.status_code == 422


def test_login_still_accepts_a_password_below_the_floor(client, env):
    """**回归钉子**：登录不受下限约束。

    下限写在请求体校验器里，很容易被"顺手"复制到 `LoginReq` —— 那会让所有历史
    短密码账号突然登不进来，而他们连改密码都需要先登录。这条把不对称钉住。
    """
    env["store"].create_user("legacy", A.hash_password("pw", iterations=_TEST_ITER),
                             tenant="acme", roles=())
    r = client.post("/v1/auth/login", json={"username": "legacy", "password": "pw"})
    assert r.status_code == 200, r.text


def test_password_floor_is_read_from_config_not_hardcoded(client, monkeypatch):
    """下限必须在**调用时**读 config，而不是把常量冻进 `Field(min_length=…)`。

    冻进去的后果：① 运维无法按合规要求调高；② 测试改不动它，边界就永远验不了
    （与 `_bounded` 那条注释同一个理由）。
    """
    monkeypatch.setattr(config, "MIN_PASSWORD_CHARS", 12)
    monkeypatch.setattr(config, "AUTH_ALLOW_SELF_REGISTER", True)
    r = client.post("/v1/auth/register", json={"username": "eve", "password": "pw-eve01"})
    assert r.status_code == 422, "8 字符在 12 位下限下应当被拒"
    assert "12" in r.text


# ---------------------------------------------------------------------------
# 9c. 重登提示：权限在服务端变了、而令牌还是旧的
#     批准入租是**唯一**能走到这个状态的正常路径（其它变更都会清会话，旧令牌直接 401）
# ---------------------------------------------------------------------------
def _join_and_approve(client, env, username="frank", password="pw-frank01"):
    """走完整流程：建无租户用户 → 拿令牌 → 申请入租 → 管理员批准。返回申请人的**旧**令牌。"""
    h = _admin(env, client)
    client.post("/v1/admin/tenants", json={"name": "globex"}, headers=h)
    client.post("/v1/admin/users",
                json={"username": username, "password": password}, headers=h)
    tok = _login(client, username, password)
    hf = _bearer(tok["access_token"])
    rid = client.post("/v1/join-requests", json={"tenant": "globex"},
                      headers=hf).json()["id"]
    ok = client.post(f"/v1/admin/join-requests/{rid}", json={"approve": True}, headers=h)
    assert ok.status_code == 200, ok.text
    return tok


def test_approved_join_makes_the_old_token_ask_for_relogin(client, env):
    """批准入租**不断会话**（用户口径）⇒ 旧令牌仍可用，但必须明确说"你该重登了"。

    没有这个信号的话，用户只能自己发现"申请通过了却搜不到东西"，而按当前口径
    无租户恰好能搜全库 —— 他会以为审批没生效，而这恰好是最难自查的一种错。
    """
    tok = _join_and_approve(client, env)
    me = client.get("/v1/auth/me", headers=_bearer(tok["access_token"]))
    assert me.status_code == 200, "批准入租刻意不清会话 —— 旧令牌应当还能用"
    body = me.json()
    assert body["tenant"] == "", "旧令牌里仍应是旧租户（这正是要提示重登的原因）"
    assert body["relogin_required"] is True
    assert "globex" in body["relogin_reason"]

    mine = client.get("/v1/join-requests/me", headers=_bearer(tok["access_token"])).json()
    assert mine["relogin_required"] is True, "整体标记没置上"
    assert mine["requests"][0]["requires_relogin"] is True, (
        "逐条标记没置上 —— 客户端就没法只对「刚批准的那条」给提示，"
        "只能看到一个说不清来由的全局提示"
    )


def test_relogin_flag_clears_after_relogin(client, env):
    """重登之后标记必须消失 —— 否则这个提示会变成一个关不掉的弹窗。"""
    _join_and_approve(client, env)
    tok2 = _login(client, "frank", "pw-frank01")
    body = client.get("/v1/auth/me", headers=_bearer(tok2["access_token"])).json()
    assert body["tenant"] == "globex"
    assert body["relogin_required"] is False and body["relogin_reason"] == ""


def test_relogin_flag_covers_role_changes_made_outside_the_admin_api(client, env):
    """角色在服务端变了（这里直接改账号表）也要报。

    为什么不走管理端点来构造：`PATCH /admin/users` 改角色会**顺手清会话**，旧令牌
    直接 401 —— 那条路根本走不到"还能用、但权限已经不对"的状态。而另一个副本、
    或运维脚本直接改账号表时不会清会话，客户端仍然需要一个信号。
    """
    tok = _login(client, "bob", "pw-bob")
    env["store"].update_user("bob", roles=("reviewer",))
    body = client.get("/v1/auth/me", headers=_bearer(tok["access_token"])).json()
    assert body["relogin_required"] is True
    assert "角色" in body["relogin_reason"]


def test_relogin_flag_reports_a_disabled_account(client, env):
    """账号被停用（同样绕过管理端点）⇒ 提示必须点明"停用"，不是含糊的"有变更"。"""
    tok = _login(client, "bob", "pw-bob")
    env["store"].update_user("bob", status="disabled")
    body = client.get("/v1/auth/me", headers=_bearer(tok["access_token"])).json()
    assert body["relogin_required"] is True
    assert "停用" in body["relogin_reason"]


def test_relogin_flag_is_false_for_a_fresh_login(client, env):
    """什么都没变过的会话 ⇒ 标记必须是 False。

    这条是**反噪声**的：若实现改成"只要 token 里带租户就提示重登"，或干脆恒
    为 True，用户会看到一个永远关不掉的提示 —— 而真出事的那一次反而被淹掉。
    所以"平常必须 False"与"变更时必须 True"是同等重要的两半。
    """
    tok = _login(client, "bob", "pw-bob")
    body = client.get("/v1/auth/me", headers=_bearer(tok["access_token"])).json()
    assert body["relogin_required"] is False and body["relogin_reason"] == ""