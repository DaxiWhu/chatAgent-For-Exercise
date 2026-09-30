"""出站调用的韧性：熔断器 + 带退避的重试。

为什么需要熔断（而重试不够）
--------------------------
只加重试会**放大故障**：上游 embedding / rerank 网关挂了，每个请求都重试 3 次，
故障期间打到上游的流量变成 3 倍，对方更难恢复，本服务的线程也被拖住。
熔断器在"连续失败达阈值"后**直接快速失败**（不发出请求），隔一段时间放一个
试探请求过去；成功了再恢复。这样既保护上游，也把本服务的失败延迟从"超时"
降到"毫秒级"。

为什么是**实例级**而不是全局单例
------------------------------
本项目已有大量离线测试，全局熔断状态会**跨用例串味**（上个用例把熔断打开了，
下个用例以为服务挂了）。所以每个客户端（RerankClient / 检索器）各自持有一个
breaker，生命周期跟着实例走，测试天然隔离。

重试与熔断的分工
--------------
- **重试**：处理"偶发失败"（网络抖动、上游瞬时 5xx）。
- **熔断**：处理"持续故障"（上游整体不可用）。
两者组合的正确姿势是：**先重试、重试仍失败才记为一次熔断失败**
（否则一次抖动就被算成 N 次失败，阈值瞬间打满）。本模块的 `guarded_call`
就是这个语义。
"""

import logging
import random
import time

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """熔断打开时抛出：表示"没有真的发请求，直接失败"。

    调用方可以据此区分"熔断快速失败"与"真实调用失败"（本项目的检索链路两者
    都走同一个降级分支，但日志里区分开更利于排障）。
    """


class CircuitBreaker:
    """三态熔断器：closed（正常）→ open（直接失败）→ half_open（试探）。"""

    def __init__(self, name, fail_threshold=5, reset_seconds=30, enabled=True):
        """构造。

        name: 熔断器名字（日志用，如 "rerank" / "embedding"）。
        fail_threshold: 连续失败多少次后打开。
        reset_seconds: 打开后隔多久放一个试探请求（进入 half_open）。
        enabled: False 时 allow 恒 True、不计数（等价于关掉熔断）。
        """
        self.name = name
        self.fail_threshold = max(1, int(fail_threshold))
        self.reset_seconds = max(1, int(reset_seconds))
        self.enabled = bool(enabled)
        self.state = CLOSED
        self.failures = 0
        self.opened_at = 0.0
        self.open_count = 0            # 累计打开次数（观测用）

    def allow(self) -> bool:
        """判断本次调用是否可以真的发出去。

        返回 True（放行）或 False（熔断中，调用方应直接失败）。
        open 状态到点后会转为 half_open 并**只放行这一次**试探。
        """
        if not self.enabled:
            return True
        if self.state == CLOSED:
            return True
        if self.state == OPEN:
            if time.time() - self.opened_at >= self.reset_seconds:
                self.state = HALF_OPEN
                logging.info("熔断器 %s 进入半开，放行一个试探请求", self.name)
                return True
            return False
        # HALF_OPEN：放行试探（失败会立刻重新打开）
        return True

    def record_success(self):
        """一次成功：清空失败计数并闭合。"""
        if not self.enabled:
            return
        if self.state != CLOSED:
            logging.info("熔断器 %s 试探成功，恢复闭合", self.name)
        self.state = CLOSED
        self.failures = 0

    def record_failure(self):
        """一次失败：累计；达阈值则打开。half_open 下失败立即重新打开。"""
        if not self.enabled:
            return
        self.failures += 1
        if self.state == HALF_OPEN or self.failures >= self.fail_threshold:
            self.state = OPEN
            self.opened_at = time.time()
            self.open_count += 1
            logging.warning("熔断器 %s 打开（连续失败 %d 次，%ds 后试探）",
                            self.name, self.failures, self.reset_seconds)

    def snapshot(self) -> dict:
        """导出当前状态（给健康检查/日志用）。"""
        return {"name": self.name, "state": self.state, "failures": self.failures,
                "open_count": self.open_count}


def retry_call(fn, attempts=3, base_delay=0.5, max_delay=8.0, jitter=0.2,
               exceptions=(Exception,), on_error=None):
    """带指数退避 + 抖动的重试。

    fn: 无参可调用（用 lambda/partial 包好参数再传）。
    attempts: 最多尝试几次（含首次）。
    base_delay: 首次退避基数（秒），之后按 2 的幂增长。
    max_delay: 单次退避上限。
    jitter: 抖动比例（0~1），避免多个客户端同时重试形成尖峰。
    exceptions: 需要重试的异常类型元组。
    on_error: 可选回调 `on_error(attempt, exc)`，用于日志。

    全部尝试都失败时**抛出最后一次异常**（不吞），由调用方决定降级方式。
    """
    last = None
    for i in range(max(1, int(attempts))):
        try:
            return fn()
        except exceptions as e:  # noqa: PERF203
            last = e
            if on_error:
                on_error(i + 1, e)
            if i == max(1, int(attempts)) - 1:
                break
            delay = min(max_delay, base_delay * (2 ** i))
            delay *= 1 + random.uniform(-jitter, jitter)
            time.sleep(max(0.0, delay))
    raise last


def guarded_call(breaker, fn, *args, retries=1, **kwargs):
    """在熔断器保护下调用 `fn(*args, **kwargs)`。

    breaker: CircuitBreaker。
    fn: 目标可调用。
    retries: 允许的重试次数（默认 1 = 不重试，保持既有行为）。
        传 >1 时**先重试、重试全失败才记一次熔断失败**（见模块 docstring）。

    熔断打开时抛 `CircuitOpenError`；否则原样返回 fn 的结果或抛出其异常。
    """
    if not breaker.allow():
        raise CircuitOpenError(f"{breaker.name} 熔断中，快速失败")
    try:
        if retries and int(retries) > 1:
            result = retry_call(lambda: fn(*args, **kwargs), attempts=int(retries))
        else:
            result = fn(*args, **kwargs)
    except Exception:
        breaker.record_failure()
        raise
    else:
        breaker.record_success()
        return result
