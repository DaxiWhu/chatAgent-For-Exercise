"""会话白名单的 **Redis 后端**（`app/auth.py`）—— 此前从未被任何测试执行过。

为什么单开一个文件
------------------
`tests/test_shared_backend.py` 已经用 fakeredis 覆盖了限流 / 配额的共享后端，但
**会话白名单那条路没被覆盖**：`_RedisSessionBackend` 只在 `REDIS_URL` 非空时才实例化，
而开发机与 CI 都没有真实 Redis，于是这三个方法**一行都没跑过** ——
`touch`（Lua 条件续期）、`pop`（Lua GETDEL）、`delete_prefix`（SCAN 前缀删除）。

它们恰好全是"写错了也不会红"的地方，每一个的失效后果都在别处被记录过：

| 方法 | 写错的后果 |
|---|---|
| `delete_prefix` | 前缀匹配跨用户 ⇒ "登出我所有设备"把别人的会话一起删（越权） |
| `touch` | 无条件续期 ⇒ 已登出的会话被 refresh **复活** |
| `pop` | 退化成 `get` ⇒ refresh token **可重复使用**（轮换形同虚设） |

怎么在没有真实 Redis 的情况下测
-------------------------------
fakeredis + **两个"副本"共享同一个 FakeServer**。共享与否正是这个后端存在的全部理由：
若每个副本各用一个独立 server，测试会全绿但什么都没证明。

⚠️ fakeredis 执行 Lua 需要 `lupa`（已声明在 requirements 的测试段）。缺了它，
`EVAL` 会返回 `ResponseError: unknown command 'eval'`，看起来像环境噪声而不是缺失依赖，
所以下面用 `_HAS_LUA` 显式判一次并给可检索的 skip 原因。

键布局契约（本文件最核心的断言）
--------------------------------
`auth:sess:{user_id}:{sid}` —— **user id 就在 Redis 键里**，这就是"按用户撤销、
按用户枚举"的全部依据；键的值是一个登录时间戳，TTL 由调用方传入。
refresh token 则另存 `auth:refresh:{sha256(token)}`，**键里不含原始 token**。
"""
import hashlib
from types import SimpleNamespace

import pytest

redis = pytest.importorskip("redis", reason="需要 redis 客户端")
fakeredis = pytest.importorskip("fakeredis", reason="用内存假 Redis 覆盖该代码路径")

from app.auth import SessionStore  # noqa: E402

_SESSION_PREFIX = "auth:sess:"
_REFRESH_PREFIX = "auth:refresh:"


def _lua_available():
    """fakeredis 只有装了 lupa 才能跑 EVAL —— 否则那条路径无法在此环境断言。"""
    try:
        fakeredis.FakeStrictRedis().eval("return 1", 0)
        return True
    except Exception:  # noqa: BLE001
        return False


_HAS_LUA = _lua_available()
_needs_lua = pytest.mark.skipif(not _HAS_LUA, reason="fakeredis 缺少 lupa，EVAL 不可用")


@pytest.fixture
def redis_env(monkeypatch):
    """把 `redis.Redis.from_url` 指到 fakeredis；多个客户端共享同一个 FakeServer。

    返回 SimpleNamespace(server, inspector)：inspector 是另一个指向同一 server 的
    客户端，用来直接查看键 / TTL（被测对象内部那个客户端不该被测试拿去断言）。
    """
    server = fakeredis.FakeServer()

    def _from_url(url, **kwargs):  # noqa: ARG001
        # 真实客户端支持 socket_timeout 等参数；这里只关心"连同一个地方"
        return fakeredis.FakeStrictRedis(server=server)

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(_from_url))
    return SimpleNamespace(server=server,
                           inspector=fakeredis.FakeStrictRedis(server=server))


def _store(url="redis://anywhere/0"):
    """构造一个**走 Redis 后端**的会话存储。"""
    return SessionStore(redis_url=url, enabled=True)


def _keys(inspector, pattern="*"):
    return sorted(k.decode("utf-8") if isinstance(k, bytes) else k
                  for k in inspector.scan_iter(match=pattern))


# ---------------------------------------------------------------------------
# 后端选择与键布局
# ---------------------------------------------------------------------------
def test_session_store_uses_redis_when_reachable(redis_env):
    assert _store().backend_name == "redis"


def test_user_id_and_sid_become_the_redis_key(redis_env):
    """核心契约：**user id 落在键里**，不是落在值里、也不是共用一个大键。"""
    store = _store()
    store.open_session("alice", "s-phone", 900)

    assert _keys(redis_env.inspector) == [f"{_SESSION_PREFIX}alice:s-phone"]
    raw = redis_env.inspector.get(f"{_SESSION_PREFIX}alice:s-phone")
    assert (raw.decode() if isinstance(raw, bytes) else raw).isdigit()
    assert redis_env.inspector.ttl(f"{_SESSION_PREFIX}alice:s-phone") > 0


def test_has_session_is_false_when_key_is_absent(redis_env):
    assert _store().has_session("alice", "never-issued") is False


# ---------------------------------------------------------------------------
# 多设备并存 —— 本次改造的核心语义
# ---------------------------------------------------------------------------
def test_two_devices_coexist_in_redis(redis_env):
    """同一用户两个会话**同时有效**：谁要是把覆盖写回去（旧单槽位语义），这条立刻红。"""
    store = _store()
    store.open_session("alice", "s-phone", 900)
    store.open_session("alice", "s-laptop", 900)

    assert _keys(redis_env.inspector, f"{_SESSION_PREFIX}alice:*") == [
        f"{_SESSION_PREFIX}alice:s-laptop", f"{_SESSION_PREFIX}alice:s-phone"]
    assert store.has_session("alice", "s-phone") is True
    assert store.has_session("alice", "s-laptop") is True


def test_logout_removes_only_that_device(redis_env):
    """单设备登出：另一个会话**必须仍然可用**（且键确实从 Redis 消失）。"""
    store = _store()
    store.open_session("alice", "s-phone", 900)
    store.open_session("alice", "s-laptop", 900)

    assert store.close_session("alice", "s-phone") is True
    assert store.has_session("alice", "s-phone") is False
    assert store.has_session("alice", "s-laptop") is True
    assert _keys(redis_env.inspector, f"{_SESSION_PREFIX}alice:*") == [
        f"{_SESSION_PREFIX}alice:s-laptop"]


def test_close_all_sessions_only_scans_that_users_prefix(redis_env):
    """`?all=true` 的作用域：只清自己，别的用户一个键都不许少。"""
    store = _store()
    store.open_session("alice", "s-phone", 900)
    store.open_session("alice", "s-laptop", 900)
    store.open_session("bob", "s-bob", 900)

    assert store.close_all_sessions("alice") == 2
    assert store.has_session("alice", "s-phone") is False
    assert store.has_session("bob", "s-bob") is True
    assert _keys(redis_env.inspector) == [f"{_SESSION_PREFIX}bob:s-bob"]


def test_close_all_sessions_on_unknown_user_removes_nothing(redis_env):
    store = _store()
    store.open_session("alice", "s-phone", 900)
    assert store.close_all_sessions("nobody") == 0
    assert store.has_session("alice", "s-phone") is True


def test_colon_in_username_cannot_cross_delete(redis_env):
    """`_key_escape` 的存在理由（只在特定用户名下复现）。

    键格式是 `auth:sess:{user}:{sid}` 而清场靠前缀匹配：若 user 不转义，
    `user="a"` 的前缀 `auth:sess:a:` 会命中 `user="a:b"` 的键 ——
    "登出我的所有设备"把**另一个用户**的会话一起删了。
    """
    store = _store()
    store.open_session("a", "s-1", 900)
    store.open_session("a:b", "s-2", 900)

    assert store.close_all_sessions("a") == 1
    assert store.has_session("a", "s-1") is False
    assert store.has_session("a:b", "s-2") is True


# ---------------------------------------------------------------------------
# 跨副本可见 —— 这就是"多进程必须先配 REDIS_URL"的全部理由
# ---------------------------------------------------------------------------
def test_session_is_visible_across_replicas(redis_env):
    """副本 A 签发的会话，副本 B 必须查得到。

    内存后端下这条是**不成立**的，而会话校验是无条件的 ⇒ 表现为"刚登录成功、
    隔几次请求又要重新登录"的随机 401。两个 store 实例共享同一 FakeServer，
    正是为了把这个语义钉住。
    """
    replica_a, replica_b = _store(), _store()
    assert (replica_a.backend_name, replica_b.backend_name) == ("redis", "redis")

    replica_a.open_session("alice", "s-phone", 900)
    assert replica_b.has_session("alice", "s-phone") is True

    # 在 B 上登出，A 也必须立刻看到失效
    replica_b.close_session("alice", "s-phone")
    assert replica_a.has_session("alice", "s-phone") is False


# ---------------------------------------------------------------------------
# Lua：条件续期（touch）
# ---------------------------------------------------------------------------
@_needs_lua
def test_touch_extends_only_when_key_exists(redis_env):
    store = _store()
    store.open_session("alice", "s-phone", 100)
    before = redis_env.inspector.ttl(f"{_SESSION_PREFIX}alice:s-phone")

    assert store.touch_session("alice", "s-phone", 900) is True
    assert redis_env.inspector.ttl(f"{_SESSION_PREFIX}alice:s-phone") > before


@_needs_lua
def test_touch_does_not_resurrect_a_closed_session(redis_env):
    """登出后 touch 必须返回 False，且**不能把键创建回来**。

    若写成无条件 `EXPIRE`（或 `SET`），一个已登出的会话就能被 refresh 悄悄复活。
    """
    store = _store()
    store.open_session("alice", "s-phone", 900)
    store.close_session("alice", "s-phone")

    assert store.touch_session("alice", "s-phone", 900) is False
    assert _keys(redis_env.inspector) == []
    assert store.has_session("alice", "s-phone") is False


# ---------------------------------------------------------------------------
# Lua：refresh token 的单次使用与哈希存储
# ---------------------------------------------------------------------------
@_needs_lua
def test_refresh_token_is_single_use(redis_env):
    """消费一次即作废（GETDEL 原子语义）。退化成 get 会让 token 可重复使用。"""
    store = _store()
    store.save_refresh("tok-1", {"user": "alice", "tenant": "acme"}, 600)

    assert store.consume_refresh("tok-1") == {"user": "alice", "tenant": "acme"}
    assert store.consume_refresh("tok-1") is None
    assert _keys(redis_env.inspector, f"{_REFRESH_PREFIX}*") == []


def test_refresh_token_is_stored_hashed(redis_env):
    """键里是 SHA-256，**不是**可直接使用的原凭证（快照/备份外泄时不可直接用）。"""
    store = _store()
    store.save_refresh("tok-1", {"user": "alice"}, 600)

    expected = _REFRESH_PREFIX + hashlib.sha256(b"tok-1").hexdigest()
    assert _keys(redis_env.inspector) == [expected]
    assert "tok-1" not in expected


def test_consume_refresh_returns_none_for_unknown_token(redis_env):
    assert _store().consume_refresh("never-issued") is None


# ---------------------------------------------------------------------------
# 故障：不静默降级
# ---------------------------------------------------------------------------
def test_redis_failure_is_not_silently_degraded(redis_env, monkeypatch):
    """运行期 Redis 挂掉 ⇒ 抛异常，**不是**返回"会话不存在"。

    这是刻意的 fail-loud：若降级成 `False`，会因为"内存里查不到那个 sid"把**全体用户**
    判成 `session_revoked`（等于全员被登出），比 5xx 更难排查。
    对照组是 `app/idempotency.py` 的 Redis 后端 —— 那里逐调用降级是对的，
    因为幂等键是"防重复"的优化，而会话白名单是**鉴权依据**，不能降级。
    """
    class _Down(fakeredis.FakeStrictRedis):
        def get(self, *args, **kwargs):
            raise redis.exceptions.ConnectionError("redis is down")

    monkeypatch.setattr(redis.Redis, "from_url",
                        staticmethod(lambda url, **kwargs: _Down(server=redis_env.server)))
    store = _store()
    assert store.backend_name == "redis"

    with pytest.raises(redis.exceptions.ConnectionError):
        store.has_session("alice", "s-phone")
