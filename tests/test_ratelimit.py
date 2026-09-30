"""限流器离线测试：滑动窗口、双维度、关闭开关、后端异常降级。

时间维度（边界放行量）只能靠**假时钟**验证 —— 真实时钟下"窗口末尾 + 窗口开头"
这两步分属不同测试执行时刻，"2N 放行"这个缺陷无法稳定复现，用例会变成随机绿。

`monkeypatch.setattr(RL, "time", clock)` 替换的是 `app.ratelimit` 模块里的 `time`
名字，**不是**全局 `time.time`：后者会污染并行运行的其他代码（含 pytest 自己的计时）。
"""
import pytest

from app import ratelimit as RL
from app.ratelimit import RateLimiter, _window_start


def test_allows_up_to_limit_then_blocks():
    rl = RateLimiter(per_minute=2, by_session=False)
    assert rl.check(ip="1.1.1.1", route="/v1/chat").allowed
    assert rl.check(ip="1.1.1.1", route="/v1/chat").allowed
    third = rl.check(ip="1.1.1.1", route="/v1/chat")
    assert not third.allowed
    assert third.scope == "ip"
    assert third.reset_after >= 1


def test_ip_buckets_are_per_route():
    """IP 维度按路由分开计数，避免 /chat 与 /review 互相挤占额度。"""
    rl = RateLimiter(per_minute=1, by_session=False)
    assert rl.check(ip="9.9.9.9", route="/v1/chat").allowed
    assert rl.check(ip="9.9.9.9", route="/v1/review").allowed


def test_session_dimension_accumulates_across_routes():
    rl = RateLimiter(per_minute=100, session_per_minute=1, by_session=True)
    assert rl.check(ip="1.1.1.1", session="t1", route="/v1/chat").allowed
    blocked = rl.check(ip="1.1.1.1", session="t1", route="/v1/review")
    assert not blocked.allowed
    assert blocked.scope == "session"


def test_disabled_limiter_always_allows():
    rl = RateLimiter(per_minute=1, enabled=False)
    for _ in range(5):
        assert rl.check(ip="2.2.2.2", session="s", route="/x").allowed


def test_backend_failure_falls_back_to_local_counting():
    """限流后端故障时退到**进程内计数**，不是放行。

    放行意味着"Redis 抖一下限流就完全失效"，而故障期流量通常还更高。
    回退计数不准（各副本各算各的），但仍有拦截能力 —— 是降级，不是关闭。
    """
    rl = RateLimiter(per_minute=1, by_session=False)

    class _Boom:
        def hit(self, *a, **kw):
            raise RuntimeError("redis down")

    rl.backend = _Boom()
    assert rl.check(ip="3.3.3.3", route="/x").allowed        # 兜底表里第 1 次 = 额度内
    assert not rl.check(ip="3.3.3.3", route="/x").allowed    # 第 2 次必须仍被拦


def test_window_start_aligns_to_minute():
    assert _window_start(125.9) % 60 == 0
    assert _window_start(125.9) == 120


# ---------------------------------------------------------------------------
# 假时钟
# ---------------------------------------------------------------------------
class _Clock:
    """可推进的假时钟：只有它暴露时间为参数，才能断言"边界处放行了多少次"。"""

    def __init__(self, t=0.0):
        self.t = float(t)

    def time(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(RL, "time", c)
    return c


# ---------------------------------------------------------------------------
# 滑动窗口：修掉"窗口边界 2N 放行"
# ---------------------------------------------------------------------------
def test_boundary_does_not_allow_double_quota(clock):
    """**核心用例**：窗口末尾用满后，紧接新窗口开头必须仍被拦。

    固定窗口下的行为是"新窗口计数从 0 开始" ⇒ 同一客户端在任意 60 秒滑窗内
    能放行 2N 次。这里把它钉死：t=59 用满 3 次，t=60 的第 4 次必须被拒。
    """
    rl = RateLimiter(per_minute=3, by_session=False)
    clock.t = 59.0
    assert all(rl.check(ip="7.7.7.7", route="/v1/chat").allowed for _ in range(3))

    clock.t = 60.0                       # 跨窗口的瞬间
    r = rl.check(ip="7.7.7.7", route="/v1/chat")
    assert not r.allowed, "跨窗口瞬间又放行了 —— 固定窗口的 2N 缺陷回来了"
    assert r.scope == "ip"


def test_reset_after_points_at_real_recovery(clock):
    """Retry-After 必须是"真的能通过了"的时刻，不是"窗口翻了"的时刻。

    判定标准不是某个具体数字，而是**这个值本身是否兑现了承诺**：客户端按它给的
    秒数重试必须真的通过。算早一步（比如沿用旧的"距窗口结束秒数"口径）会让客户端
    在必定失败的时刻重试，这就是重试风暴的成因。
    """
    rl = RateLimiter(per_minute=3, by_session=False)
    clock.t = 59.0
    for _ in range(3):
        rl.check(ip="8.8.8.8", route="/v1/chat")

    clock.t = 60.0
    r = rl.check(ip="8.8.8.8", route="/v1/chat")
    assert not r.allowed
    assert r.reset_after > 0

    clock.t = 60.0 + r.reset_after
    assert rl.check(ip="8.8.8.8", route="/v1/chat").allowed, (
        f"按 reset_after={r.reset_after} 指明的时刻重试仍被拒 —— 这个值在骗客户端"
    )


def test_reset_after_covers_a_hard_burst(clock):
    """在一个窗口里一次性打满额度的情形，恢复时刻**晚于**窗口翻页。

    这是"加权滑窗"必须付出的一段额外等待：新窗口开始时，上一窗口的计数权重仍是 1，
    所以 `cur` 远超 `limit` 时要把那部分衰减掉才恢复得了。旧口径（窗口剩余秒数）
    在这种情形下必然算早。
    """
    rl = RateLimiter(per_minute=2, by_session=False)
    clock.t = 0.0
    assert rl.check(ip="2.2.2.2", route="/v1/chat").allowed
    assert rl.check(ip="2.2.2.2", route="/v1/chat").allowed
    r = rl.check(ip="2.2.2.2", route="/v1/chat")
    assert not r.allowed
    assert r.reset_after > 60, f"恢复时刻早于窗口翻页：{r.reset_after}"

    clock.t = 0.0 + r.reset_after
    assert rl.check(ip="2.2.2.2", route="/v1/chat").allowed


def test_quota_recovers_after_window_fully_slides(clock):
    """窗口完全滑过之后额度自然恢复（不能变成永久封禁）。"""
    rl = RateLimiter(per_minute=3, by_session=False)
    clock.t = 59.0
    for _ in range(3):
        rl.check(ip="6.6.6.6", route="/v1/chat")
    clock.t = 60.0
    assert not rl.check(ip="6.6.6.6", route="/v1/chat").allowed

    clock.t = 120.0
    assert rl.check(ip="6.6.6.6", route="/v1/chat").allowed


def test_previous_window_decays_linearly(clock):
    """上一窗口的贡献随时间衰减 —— 这是"滑窗"相对"固定窗口"的全部增量。

    在窗口 0 用满 10 次，窗口 1 过半（t=90，已过 30 秒）时上一窗口只剩一半权重：
    加权计数 = 10 × 0.5 + 1 = 6，剩余额度 4。
    """
    rl = RateLimiter(per_minute=10, by_session=False)
    clock.t = 0.0
    for _ in range(10):
        assert rl.check(ip="5.5.5.5", route="/v1/chat").allowed

    clock.t = 90.0
    r = rl.check(ip="5.5.5.5", route="/v1/chat")
    assert r.allowed
    assert r.remaining == 4, f"衰减没生效（remaining={r.remaining}）—— 说明只看当前窗口"


def test_reset_after_is_window_remaining_while_allowed(clock):
    """未超限时 reset_after 仍是窗口剩余秒数（保持旧口径，客户端只在 429 用它）。"""
    rl = RateLimiter(per_minute=10, by_session=False)
    clock.t = 30.0
    assert rl.check(ip="4.4.4.4", route="/v1/chat").reset_after == 30


def test_both_dimensions_share_one_clock(clock):
    """两个维度必须用同一个 `now`。

    各读各的时钟会出现"IP 维度按旧窗口、会话维度按新窗口"的错位判定 ——
    表现为限额时紧时松，且不可复现。
    """
    rl = RateLimiter(per_minute=1, session_per_minute=1, by_session=True)
    clock.t = 59.5
    assert rl.check(ip="1.1.1.1", session="s1", route="/v1/chat").allowed
    clock.t = 60.0
    # 两个维度都在上一窗口用满了：这一次必须被拦（无论判到哪个维度）
    assert not rl.check(ip="1.1.1.1", session="s2", route="/v1/chat").allowed


def test_stale_windows_are_swept(clock):
    """过期窗口要被清掉 —— 否则键数只增不减，长期运行是内存泄漏。

    清理改成"最多每分钟扫一次"（摊还 O(1)）后，代价是过期键最多多留一个窗口，
    所以断言的是"更早的窗口已经没了"，而不是"立刻清空"。
    """
    rl = RateLimiter(per_minute=5, by_session=False)
    clock.t = 0.0
    rl.check(ip="1.2.3.4", route="/x")

    clock.t = 300.0
    rl.check(ip="1.2.3.4", route="/x")
    windows = sorted(w for _, w in rl.backend._data)
    assert windows == [300], f"过期窗口没清干净：{windows}"
