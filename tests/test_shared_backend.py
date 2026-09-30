"""共享后端（REDIS_URL）：限流与配额的跨副本计数。

这个文件补的是**一段此前从未被执行过的代码**：`ratelimit._RedisBackend` 与
`quota._RedisStore` 写好了但没有任何测试（真实 Redis 在 CI 里不方便起，而
`redis` 包本身之前也没装），于是它们连"能不能跑通"都没被验证过。

怎么在没有真实 Redis 的情况下测
----------------------------
用 `fakeredis`，并且**两个"副本"共享同一个 FakeServer** —— 这是关键：
共享与否正是这段代码存在的理由。如果每个实例用一个独立 server，
测试会全绿但什么都没证明（见 `test_memory_backends_do_not_share` 这个对照组）。

另外三条针对"静默降级"的用例（缺包 / 连不上 / 运行期报错）是这次加固的重点：
原实现这三种情况都会**悄悄退回进程内计数**，配置项填了却当没填。
"""
import logging
import sys
from types import SimpleNamespace

import pytest

redis = pytest.importorskip("redis", reason="需要 redis 客户端")
fakeredis = pytest.importorskip("fakeredis", reason="用内存假 Redis 覆盖该代码路径")

from app import metrics, ratelimit, quota  # noqa: E402
from app import config                      # noqa: E402
from app.quota import QuotaManager          # noqa: E402
from app.ratelimit import RateLimiter       # noqa: E402


@pytest.fixture(autouse=True)
def _clean_metrics():
    metrics.reset()


@pytest.fixture
def redis_env(monkeypatch):
    """把 `redis.Redis.from_url` 指到 fakeredis；多个客户端共享同一个 FakeServer。

    返回 SimpleNamespace(server, inspector)：inspector 是另一个指向同一 server 的
    客户端，用来直接查看键与 TTL（业务对象内部那个客户端不应被测试拿去 assert）。
    """
    server = fakeredis.FakeServer()

    def _from_url(url, **kwargs):
        # 真实客户端支持 socket_timeout 等参数；这里只关心"连同一个地方"
        return fakeredis.FakeStrictRedis(server=server)

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(_from_url))
    return SimpleNamespace(server=server,
                           inspector=fakeredis.FakeStrictRedis(server=server))


# ---------------------------------------------------------------------------
# 核心语义：跨副本共享
# ---------------------------------------------------------------------------
def test_ratelimit_uses_redis_backend_when_reachable(redis_env):
    rl = RateLimiter(per_minute=5, redis_url="redis://anywhere/0")
    assert rl.backend_name == "redis"


def test_two_replicas_share_the_rate_limit_counter(redis_env):
    """两个副本共用同一个 Redis ⇒ 额度是**全局**的，不是"每个副本各一份"。"""
    a = RateLimiter(per_minute=3, by_session=False, redis_url="redis://anywhere/0")
    b = RateLimiter(per_minute=3, by_session=False, redis_url="redis://anywhere/0")
    assert (a.backend_name, b.backend_name) == ("redis", "redis")

    verdicts = [a.check(ip="1.2.3.4").allowed,
                b.check(ip="1.2.3.4").allowed,
                a.check(ip="1.2.3.4").allowed,
                b.check(ip="1.2.3.4").allowed]
    assert verdicts == [True, True, True, False], verdicts


def test_redis_backend_also_slides_the_window(redis_env, monkeypatch):
    """Redis 后端也必须按滑动窗口判定。

    "内存后端换了滑窗、Redis 后端还是固定窗口"是最坏的一种不一致：本地单副本
    测试全绿，上线多副本才复发，而且只发生在恰好跨窗口的瞬间。两个后端共用同一套
    判定公式，这里钉住 Redis 那一路。

    顺带钉住一处极易漏的细节：键的 TTL 必须覆盖**两个**窗口 —— 只给一个窗口时，
    键会在下个窗口开始的瞬间过期，"上一窗口计数"被读成 0，边界 2N 原样复发。
    """

    class _Clock:
        t = 59.0

        def time(self):
            return self.t

    ck = _Clock()
    # 只替换 ratelimit 模块里的 `time` 名字，不动全局 time.time
    monkeypatch.setattr(ratelimit, "time", ck)

    a = RateLimiter(per_minute=3, by_session=False, redis_url="redis://anywhere/0")
    b = RateLimiter(per_minute=3, by_session=False, redis_url="redis://anywhere/0")
    assert all(a.check(ip="7.7.7.7").allowed for _ in range(3))

    ck.t = 60.0
    assert not b.check(ip="7.7.7.7").allowed, (
        "另一副本在新窗口开头的请求被放行了 —— 边界 2N 缺陷在 Redis 后端复发了"
    )


def test_memory_backends_do_not_share():
    """对照组：不配 Redis 时两个副本各算各的 —— 这**正是**额度会放大的原因。

    没有这条，上面那条"共享"测试可能是假绿（比如两边本来就都从 0 开始数）。
    """
    a = RateLimiter(per_minute=3, by_session=False)
    b = RateLimiter(per_minute=3, by_session=False)
    assert (a.backend_name, b.backend_name) == ("memory", "memory")
    for _ in range(3):
        assert a.check(ip="1.2.3.4").allowed
        assert b.check(ip="1.2.3.4").allowed      # b 完全不知道 a 已经用了 3 次


def test_quota_is_shared_across_replicas(redis_env):
    """日配额同理：A 用掉的量，B 立刻能看到并据此拦截。"""
    a = QuotaManager(daily_limit=100, redis_url="redis://anywhere/0")
    b = QuotaManager(daily_limit=100, redis_url="redis://anywhere/0")

    a.add("acme", 80)
    st = b.check("acme")
    assert (st.used, st.allowed) == (80, True)

    b.add("acme", 30)
    st2 = a.check("acme")
    assert (st2.used, st2.allowed) == (110, False), st2


def test_quota_is_isolated_per_tenant(redis_env):
    """键里带租户：一个租户用光不该影响另一个。"""
    qm = QuotaManager(daily_limit=10, redis_url="redis://anywhere/0")
    qm.add("acme", 10)
    assert qm.check("acme").allowed is False
    assert qm.check("globex").allowed is True


# ---------------------------------------------------------------------------
# 键与 TTL（过期策略错了会导致"限流永不重置"或"配额永远攒不满"）
# ---------------------------------------------------------------------------
def test_ratelimit_key_shape_and_ttl(redis_env):
    """键名必须带窗口序号，且 TTL 必须覆盖**两个**窗口。

    为什么是两个而不是一个：加权滑窗的判定要读"上一窗口"的计数。TTL 只给一个
    窗口时，键恰好在下个窗口开始的瞬间过期 ⇒ 上一窗口被读成 0 ⇒ 边界 2N 放行的
    缺陷在 Redis 后端**原样复发**，而且只在多副本 + 恰好跨窗口时出现，极难复现。
    （这条断言原来写的是"<= 一个窗口"，正是上面那个缺陷的镜像。）

    上限仍要有：没有 TTL 的键永不释放，键数只增不减。
    """
    rl = RateLimiter(per_minute=5, by_session=False, redis_url="redis://anywhere/0")
    rl.check(ip="9.9.9.9")
    keys = list(redis_env.inspector.keys("*"))
    assert len(keys) == 1, keys
    assert keys[0].startswith(b"rl:ip:9.9.9.9:")      # 含窗口序号 → 旧窗口自然过期
    ttl = redis_env.inspector.ttl(keys[0])
    # 必须有**两个窗口**的余量：一秒的抖动只会让 121 掉到 120，不会掉到 119 以下
    assert ttl >= 2 * 60 - 1, f"TTL 只覆盖了一个窗口，上一窗口计数会被读成 0：{ttl}"
    assert ttl <= 2 * 60 + 1, f"TTL 没有上界，键永不释放：{ttl}"


def test_quota_key_has_bounded_ttl(redis_env):
    """配额键必须带 TTL，且不能因为时钟问题被"立刻过期"。

    原实现用 `EXPIREAT(次日零点的时间戳)`：时刻由**应用侧时钟**算出，
    应用时钟偏快就会得到一个已过去的时刻 ⇒ Redis 立刻删键 ⇒ 加完就清零 ⇒
    **配额永远攒不满、静默失效**。现在改用相对 TTL + 60s 下限。
    """
    qm = QuotaManager(daily_limit=100, redis_url="redis://anywhere/0")
    qm.add("acme", 7)
    keys = list(redis_env.inspector.keys("*"))
    assert len(keys) == 1 and keys[0].startswith(b"quota:acme:")
    ttl = redis_env.inspector.ttl(keys[0])
    assert ttl >= 60, f"TTL 过短会被立刻过期：{ttl}"
    assert qm.check("acme").used == 7


# ---------------------------------------------------------------------------
# 三条静默降级：都必须响亮地报错，而不是"悄悄退回内存"
# ---------------------------------------------------------------------------
def test_missing_redis_package_logs_actionable_error(monkeypatch, caplog):
    """配了 REDIS_URL 但没装客户端 → ERROR 且给出可执行的修法。"""
    monkeypatch.setitem(sys.modules, "redis", None)     # 令 `import redis` 抛 ImportError
    with caplog.at_level(logging.ERROR):
        rl = RateLimiter(per_minute=5, redis_url="redis://localhost:6379/0")
    assert rl.backend_name == "memory"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("未生效" in m and "pip install redis" in m for m in msgs), msgs


def test_unreachable_redis_falls_back_to_memory_and_logs_error(caplog):
    """地址写错/服务没起 → 必须报 ERROR。

    为什么这条特别重要：`redis.Redis.from_url` 是**懒连接**，原实现不在构造期
    ping，于是"连不上"要等第一个请求才暴露，而那时异常已被吞掉 —— 配了 Redis
    却完全不生效，且日志里只有一行 warning。
    """
    with caplog.at_level(logging.ERROR):
        rl = RateLimiter(per_minute=5, redis_url="redis://127.0.0.1:1/0")
    assert rl.backend_name == "memory"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("未生效" in m for m in msgs), msgs


def test_quota_unreachable_redis_logs_error(caplog):
    with caplog.at_level(logging.ERROR):
        qm = QuotaManager(daily_limit=10, redis_url="redis://127.0.0.1:1/0")
    assert qm.backend_name == "memory"
    assert any("未生效" in r.getMessage() for r in caplog.records)


def test_backend_error_degrades_to_local_counting_not_open(caplog):
    """运行期后端故障 → 退到进程内计数（**不是放行**）。

    原实现直接返回"放行"，等于"Redis 抖一下，限流全失效"。限流是挡 LLM 成本的
    最后一道闸门，而故障期流量通常还更高 —— 方向是反的。
    """
    rl = RateLimiter(per_minute=2, by_session=False)

    class _Broken:
        def hit(self, *a, **k):
            raise RuntimeError("redis down")

    rl.backend = _Broken()
    rl.backend_name = "redis"

    with caplog.at_level(logging.ERROR):
        verdicts = [rl.check(ip="5.5.5.5").allowed for _ in range(3)]

    assert verdicts == [True, True, False], "后端故障时直接放行了，等于限流失效"
    assert metrics.value("shared_backend_errors_total",
                         component="ratelimit", backend="redis") == 3


def test_quota_backend_error_degrades_to_local_counting(caplog):
    """配额同理：后端故障时若"不算数"，配额就永远攒不满。"""
    qm = QuotaManager(daily_limit=10)

    class _BrokenStore:
        def get(self, *a, **k):
            raise RuntimeError("redis down")

        def add(self, *a, **k):
            raise RuntimeError("redis down")

    qm.store = _BrokenStore()
    qm.backend_name = "redis"

    with caplog.at_level(logging.ERROR):
        qm.add("acme", 6)
        st = qm.check("acme")
        qm.add("acme", 6)
        st2 = qm.check("acme")

    assert (st.used, st.allowed) == (6, True)
    assert (st2.used, st2.allowed) == (12, False), "后端故障时配额失效了"
    assert metrics.value("shared_backend_errors_total",
                         component="quota", backend="redis") >= 4


# ---------------------------------------------------------------------------
# 配置回显：不能谎报"共享已启用"
# ---------------------------------------------------------------------------
def test_as_dict_does_not_claim_shared_backend_is_active(monkeypatch):
    """`shared_configured`（打算用）与 `backend`（实际在用）必须分开回显。

    只回显 `bool(REDIS_URL)` 就是谎报：配置填了、实际退回内存，是这里最危险的
    那种错误（以为额度是全局限的，实际是"配置值 × 副本数"）。
    """
    monkeypatch.setattr(config, "REDIS_URL", "redis://configured")
    d = config.as_dict(runtime={"rate_limit_backend": "memory",
                                "quota_backend": "memory"})
    assert d["rate_limit"]["shared_configured"] is True
    assert d["rate_limit"]["backend"] == "memory"      # 配了但没生效
    assert d["quota"]["backend"] == "memory"

    ok = config.as_dict(runtime={"rate_limit_backend": "redis",
                                 "quota_backend": "redis"})
    assert ok["rate_limit"]["backend"] == "redis"


def test_as_dict_defaults_to_memory_without_redis(monkeypatch):
    monkeypatch.setattr(config, "REDIS_URL", "")
    d = config.as_dict()
    assert d["rate_limit"]["shared_configured"] is False
    assert d["rate_limit"]["backend"] == "memory"


def test_builders_pick_up_redis_url_from_config(monkeypatch, redis_env):
    """接线：`build_*()` 必须把 config.REDIS_URL 传下去（否则配了也不生效）。"""
    monkeypatch.setattr(config, "REDIS_URL", "redis://from-config/0")
    assert ratelimit.build_rate_limiter().backend_name == "redis"
    assert quota.build_quota_manager().backend_name == "redis"


def test_startup_selfcheck_flags_configured_but_inactive(monkeypatch, caplog):
    """启动自检：配了 REDIS_URL 但后端没生效 → 一条可检索的聚合 ERROR。"""
    import app.main as M

    monkeypatch.setattr(M.config, "REDIS_URL", "redis://configured")
    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=10))     # memory
    monkeypatch.setattr(M, "_QUOTA", QuotaManager(daily_limit=0))      # memory

    with caplog.at_level(logging.ERROR):
        M._assert_shared_backend_policy()
    msgs = [r.getMessage() for r in caplog.records]
    assert any("未生效" in m and "限流" in m and "配额" in m for m in msgs), msgs


def test_startup_selfcheck_silent_when_shared_is_active(monkeypatch, caplog, redis_env):
    """真接上共享后端时不该报错（否则就成了"狼来了"的噪音）。"""
    import app.main as M

    monkeypatch.setattr(M.config, "REDIS_URL", "redis://anywhere/0")
    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=10,
                                                   redis_url="redis://anywhere/0"))
    monkeypatch.setattr(M, "_QUOTA", QuotaManager(daily_limit=0,
                                                  redis_url="redis://anywhere/0"))
    with caplog.at_level(logging.ERROR):
        M._assert_shared_backend_policy()
    assert not [r for r in caplog.records if "未生效" in r.getMessage()]
