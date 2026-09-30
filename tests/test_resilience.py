"""熔断器 / 重试 / guarded_call 的离线测试。

熔断状态是**实例级**的，所以每个用例各造一个，互不影响。
"""
import time

import pytest

from src.rag.resilience import (CLOSED, HALF_OPEN, OPEN, CircuitBreaker,
                                CircuitOpenError, guarded_call, retry_call)


def test_breaker_opens_after_threshold_and_fast_fails():
    b = CircuitBreaker("demo", fail_threshold=2, reset_seconds=30)
    assert b.allow()
    b.record_failure()
    assert b.state == CLOSED and b.allow()
    b.record_failure()
    assert b.state == OPEN
    assert not b.allow()                    # 熔断中：不再真的发请求
    assert b.open_count == 1


def test_success_resets_failure_count():
    b = CircuitBreaker("demo", fail_threshold=3)
    b.record_failure()
    b.record_success()
    assert b.state == CLOSED and b.failures == 0


def test_half_open_after_reset_window():
    b = CircuitBreaker("demo", fail_threshold=1, reset_seconds=1)
    b.record_failure()
    assert b.state == OPEN
    b.opened_at = time.time() - 2           # 假装已经过了试探窗口
    assert b.allow()
    assert b.state == HALF_OPEN


def test_half_open_failure_reopens_immediately():
    b = CircuitBreaker("demo", fail_threshold=5, reset_seconds=1)
    b.state = HALF_OPEN
    b.record_failure()
    assert b.state == OPEN


def test_disabled_breaker_never_blocks():
    b = CircuitBreaker("demo", fail_threshold=1, enabled=False)
    for _ in range(5):
        b.record_failure()
    assert b.allow() and b.state == CLOSED


def test_guarded_call_raises_circuit_open_when_tripped():
    b = CircuitBreaker("demo", fail_threshold=1)

    def boom():
        raise RuntimeError("上游挂了")

    with pytest.raises(RuntimeError):
        guarded_call(b, boom)
    # 已达阈值 → 下一次直接快速失败，函数体不再执行
    with pytest.raises(CircuitOpenError):
        guarded_call(b, boom)


def test_guarded_call_returns_value_and_closes_breaker():
    b = CircuitBreaker("demo", fail_threshold=1)

    def ok():
        return 42

    assert guarded_call(b, ok) == 42
    assert b.state == CLOSED


def test_guarded_call_retries_before_counting_one_failure():
    """重试全失败才算一次熔断失败（否则一次抖动就把阈值打满）。"""
    b = CircuitBreaker("demo", fail_threshold=1, reset_seconds=30)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        raise ValueError("x")

    with pytest.raises(ValueError):
        guarded_call(b, flaky, retries=3)
    assert calls["n"] == 3
    assert b.state == OPEN and b.failures == 1


def test_retry_call_returns_first_success():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise ValueError("first fails")
        return "ok"

    assert retry_call(flaky, attempts=3, base_delay=0) == "ok"
    assert calls["n"] == 2


def test_retry_call_raises_last_error():
    def boom():
        raise ValueError("always")

    with pytest.raises(ValueError):
        retry_call(boom, attempts=2, base_delay=0, jitter=0)
