"""`app/auth.py` 的单元级安全断言（离线，不联网）。

为什么这些必须单独测、不能只靠端到端：
  - **PBKDF2 迭代数上下限**（哈希串是外部输入，一条超限的串就能打满一个 worker）；
  - **会话白名单的语义**（多设备并存、每会话一键、refresh 单次使用、前缀不跨用户）——
    "登出真的生效"全部靠它，而端到端只覆盖其中一条路径。

⚠️ 这里**没有** JWT 外部验签用例：外部身份源（OIDC / JWKS）2026-10-05 已整体删除，
本服务只验自己用 HS256 签发的会话令牌 —— 那条路径由下面的会话白名单用例覆盖。
"""

import pytest

from app import auth as A
from app import config


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------
def test_password_roundtrip_and_salt_uniqueness():
    h1 = A.hash_password("pw", iterations=100_000)
    h2 = A.hash_password("pw", iterations=100_000)
    assert h1 != h2, "两次哈希相同 ⇒ 没有加盐"
    assert A.verify_password("pw", h1)
    assert not A.verify_password("PW", h1)
    assert not A.verify_password("", h1)


def test_verify_password_never_raises():
    """任何脏串都必须返回 False，而不是抛异常。

    抛异常会把"配置写错"升级成 500，既泄露"该用户存在但配置坏了"，
    又把客户端错误变成服务端错误。
    """
    for bad in ["", "garbage", "pbkdf2_sha256$$$", "md5$a$b$c", None,
                "pbkdf2_sha256$abc$zz$zz"]:
        assert A.verify_password("x", bad) is False


def test_iteration_count_is_bounded_both_ways():
    """迭代数上下限是**安全必需**，不是健壮性：哈希串是外部输入。

    没有上限时，一条 `pbkdf2_sha256$2000000000$...` 就能让单次登录消耗
    CPU 数十秒 —— 知道用户名即可打满一个 worker，无需密码正确。
    """
    assert not A.verify_password("x", "pbkdf2_sha256$2000000000$" + "aa" * 16 + "$" + "bb" * 32)
    assert not A.verify_password("x", "pbkdf2_sha256$1$" + "aa" * 16 + "$" + "bb" * 32)


def test_hash_password_clamps_iterations():
    """即使传入超限的迭代数，产出的哈希也必须在安全区间内。"""
    encoded = A.hash_password("pw", iterations=10)
    iterations = int(encoded.split("$")[1])
    assert iterations >= A._MIN_ITER


# ---------------------------------------------------------------------------
# 会话白名单：多设备并存 / 每会话一键 / 轮换
# ---------------------------------------------------------------------------
@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(config, "AUTH_SESSION_SECRET", "s" * 48)
    monkeypatch.setattr(config, "AUTH_ACCESS_TTL", 900)
    monkeypatch.setattr(config, "AUTH_REFRESH_TTL", 3600)
    return A.AuthService(A.SessionStore(enabled=True))


def test_open_session_does_not_disturb_existing_ones(service):
    """开新会话**不得**影响已有会话 —— 这是"多设备并存"的最低要求。

    ⚠️ 旧版这里断言的是 `bind` 的"原子覆盖"（单槽位），结论恰好相反。
    语义反转了，断言必须一起改 —— 留着旧的会把实现锁死在单槽位上。
    """
    service.store.open_session("u", "S1", 60)
    service.store.open_session("u", "S2", 60)
    assert service.store.has_session("u", "S1") is True, "新会话把旧会话顶掉了"
    assert service.store.has_session("u", "S2") is True


def test_close_session_only_removes_its_own(service):
    """关掉一个会话，同用户的其它会话必须原样保留。"""
    service.store.open_session("u", "S1", 60)
    service.store.open_session("u", "S2", 60)

    assert service.store.close_session("u", "S2") is True
    assert service.store.has_session("u", "S2") is False
    assert service.store.has_session("u", "S1") is True, "登出误伤了其它会话"


def test_close_all_sessions_clears_only_that_user(service):
    """`close_all_sessions` 的作用域必须**严格限于单个用户**。"""
    for sid in ("S1", "S2", "S3"):
        service.store.open_session("u", sid, 60)
    service.store.open_session("v", "S9", 60)

    assert service.store.close_all_sessions("u") == 3
    assert service.store.has_session("u", "S1") is False
    assert service.store.has_session("v", "S9") is True, "清场越界删掉了别人的会话"


def test_session_keys_do_not_cross_users_with_colon_in_name(service):
    """用户名含 `:` 时前缀匹配不能跨用户 —— 否则是**静默的越权删除**。

    键格式是 `auth:sess:{user}:{sid}`，而 close_all_sessions 靠前缀匹配。
    不做转义的话，user="a" 的前缀 `auth:sess:a:` 会命中 user="a:b" 的键。
    只在特定用户名下复现，平时测不出来。
    """
    service.store.open_session("a", "S1", 60)
    service.store.open_session("a:b", "S2", 60)

    service.store.close_all_sessions("a")

    assert service.store.has_session("a", "S1") is False
    assert service.store.has_session("a:b", "S2") is True, "前缀匹配跨用户误删"


def test_touch_does_not_resurrect_a_revoked_session(service):
    """续期**不能创建**已不存在的会话 —— 否则登出会被 refresh 复活。"""
    assert service.store.touch_session("u", "gone", 60) is False
    assert service.store.has_session("u", "gone") is False

    service.store.open_session("u", "S1", 60)
    assert service.store.touch_session("u", "S1", 60) is True


def test_refresh_token_is_single_use(service):
    service.store.open_session("u", "S1", 3600)
    _, refresh, _ = service.issue("u", "acme", (), session_id="S1")
    assert service.store.consume_refresh(refresh)["user"] == "u"
    assert service.store.consume_refresh(refresh) is None, "refresh token 可重复使用"


def test_refresh_token_stored_hashed_not_plaintext(service):
    _, refresh, _ = service.issue("u", "acme", (), session_id="S1")
    backend = service.store.backend
    # 明文 token 不应能在后端里被直接查到（存的是哈希）
    assert backend.get(A._REFRESH_PREFIX + refresh) is None
    assert backend.get(A._REFRESH_PREFIX + A._refresh_hash(refresh)) is not None


def test_login_principal_registers_before_issuing(service):
    """先登记会话、再签发 —— 顺序反了会得到"一用就报会话已失效"的 token。"""
    p = A.Principal(user="u", tenant="acme", roles=())
    access, _, sid = service.login_principal(p)
    assert service.store.has_session("u", sid) is True
    assert service.verify_session_token(access).session_id == sid


def test_verify_session_token_rejects_revoked_session(service):
    """会话被移除后，即使 token 本身完全合法也必须拒绝。"""
    p = A.Principal(user="u", tenant="acme", roles=())
    access, _, sid = service.login_principal(p)
    assert service.verify_session_token(access).user == "u"

    service.store.close_session("u", sid)
    with pytest.raises(A.AuthError) as ei:
        service.verify_session_token(access)
    assert ei.value.code == "session_revoked"


def test_principal_has_role():
    p = A.Principal(user="u", tenant="t", roles=("reviewer",))
    assert p.has_role(("reviewer", "admin")) is True
    assert p.has_role(("admin",)) is False
    assert p.has_role(()) is True, "空白名单表示不限制"


# ---------------------------------------------------------------------------
# 存储不可用 ≠ 凭证错误（2026-10-30）
# ---------------------------------------------------------------------------
# 背景（实机）：数据库不可连时 `POST /v1/auth/login` 回 **500「服务内部错误」**，
# 而这个实例的 `/health/ready` 早就算出 `db.ok=false` 了。500 的意思是"程序有
# bug"，运维会去翻代码；真因是账号库连不上。
#
# 更要紧的是**别报成 401**：那会让所有人同时以为自己密码错了，于是集体去重置
# 密码 —— 一次可恢复的运维故障被放大成账号事故。
class _DownAccounts:
    """账号库连不上（模拟 psycopg 的连接超时）。"""

    def get_user(self, username):
        import psycopg

        raise psycopg.OperationalError("connection timeout expired")


class _NoPoolAccounts:
    """连接池不可用（`app.pg` 自己的异常类型，不是 psycopg 的）。"""

    def get_user(self, username):
        from app import pg

        raise pg.PoolUnavailable("池已关闭")


class _BuggyAccounts:
    """代码 bug（不是存储故障）。用来钉住"别把所有异常都当存储错误"。"""

    def get_user(self, username):
        raise KeyError("password_hash")


def test_login_reports_storage_unavailable_when_db_is_down(service):
    svc = A.AuthService(service.store, accounts=_DownAccounts())
    with pytest.raises(A.StorageUnavailable) as ei:
        svc.login_with_password("u", "p")
    assert ei.value.status_code == 503, "账号库不可用不是客户端的错，不能是 4xx"
    assert ei.value.code == "storage_unavailable"
    assert "invalid_credentials" not in ei.value.code


def test_login_treats_pool_unavailable_as_storage_error(service):
    """`app.pg.PoolUnavailable` 不在 psycopg 的异常树里，必须单独认。"""
    svc = A.AuthService(service.store, accounts=_NoPoolAccounts())
    with pytest.raises(A.StorageUnavailable) as ei:
        svc.login_with_password("u", "p")
    assert ei.value.status_code == 503


def test_login_does_not_disguise_code_bugs_as_storage_errors(service):
    """非存储类异常必须**原样抛出**。

    若这里改成"任何异常都转 503"，一个键名写错就会让登录永久 503、日志里一句
    真话都没有 —— 比现在的 500 更难查。
    """
    svc = A.AuthService(service.store, accounts=_BuggyAccounts())
    with pytest.raises(KeyError):
        svc.login_with_password("u", "p")
