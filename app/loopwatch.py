"""事件循环阻塞探针。

为什么需要它
------------
async 服务里最隐蔽的一种变慢：**一个同步调用跑在事件循环上**。它不报错
（错误率保持 0），也不漏请求 —— 它让**所有**并发请求一起停住，直到那个调用返回。
唯一的表现是"延迟上升"，而延迟上升的原因有几十个 ⇒ 只看 P95 分不出是哪一种。

项目里已有 `tests/test_event_loop_offload.py` 守"已知的那些调用点被移出了事件循环"，
但**测试只覆盖已知调用点**：哪天有人在新增的工具里直接读写文件、或在压缩路径里同步
调模型，测试不会红。所以线上需要一条独立信号，把"卡住了"和"慢"分开。

它怎么量
--------
起一个心跳协程，每 `_INTERVAL` 秒醒一次，比较"实际睡了多久"与"期望睡多久"；
超出的部分就是事件循环被别人占住的时间。**只记录超过 `_THRESHOLD` 的那部分** ——
指标因此表达的是"**明显阻塞事件的时长分布**"，而不是"每秒的调度抖动"。

⚠️ 两条实测结论（`tmp_probe/probe_event_loop_lag.py`，本机量的）
------------------------------------------------------------------
1. **Windows 上 `asyncio.sleep` 有约 15ms 的量化误差**（系统定时器分辨率），
   空载 P99 ≈ 15ms、max ≈ 15ms。阈值取 50ms ⇒ 基线的 3 倍余量。
   Linux 的基线更低（1ms 级），同一个阈值只会更安全。
2. **心跳间隔必须小于要检测的阻塞时长。** 阻塞若完全落在两次心跳之间，
   心跳的定时器仍会按时到期，`lag ≈ 0` —— 探针**什么都看不到**。
   实测：间隔 1.0s 时，一次 **200ms 的同步阻塞完全没被记录**（max 只有 14.9ms）；
   0.1s 时间隔下如实记下 194ms。所以间隔取 0.1s
   （代价：每秒 10 次唤醒，每次几微秒，可忽略）。

⚠️ 它自己绝不能成为故障源
------------------------
任何异常都吞掉并继续下一轮（与 `src/observability.py` 同一条原则）。
探针一旦静默死掉，指标就停在零上 —— "没问题"与"根本没在检查"从此分不出来，
那比没有这个指标更糟（同样的理由见 `app/metrics.py` 里 Gauge 的注释）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from . import metrics

__all__ = ["watching"]

#: 心跳间隔（秒）。**必须小于要检测的阻塞时长**，否则会漏检（见模块 docstring 的实测）。
_INTERVAL = 0.1

#: 超过这个时长（秒）才算"阻塞"。Windows 空载基线的 3 倍余量（见模块 docstring）。
_THRESHOLD = 0.05

#: 指标名 —— 与 `deploy/monitoring/alerts.yml` 的表达式必须一致
#: （`tests/test_runtime_metrics.py` 会核对它被埋点、且被告警引用）。
_METRIC = "event_loop_lag_seconds"


async def _heartbeat() -> None:
    """心跳循环：永不退出（除非被取消）。"""
    loop = asyncio.get_running_loop()
    while True:
        try:
            t0 = loop.time()
            await asyncio.sleep(_INTERVAL)
            lag = (loop.time() - t0) - _INTERVAL
            if lag >= _THRESHOLD:
                metrics.observe(_METRIC, lag)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  单次心跳的任何失败都不该让探针死掉
            logging.debug("事件循环阻塞探针单次心跳异常（已忽略）：%s", e)


@asynccontextmanager
async def watching() -> AsyncIterator[None]:
    """在生命周期内挂着探针，退出时取消它。

    用 `AsyncExitStack.enter_async_context(...)` 压进 lifespan 的 stack，
    这样 `stack.aclose()` 会把它一起收掉 —— 不用在关闭路径上再写一遍取消逻辑
    （漏写的代价是每个副本在滚动发布时留下一个悬挂任务）。
    """
    task = asyncio.create_task(_heartbeat(), name="event-loop-lag-probe")
    logging.info("事件循环阻塞探针已启动：每 %.2fs 一次心跳，>%.0fms 记为一次阻塞",
                 _INTERVAL, _THRESHOLD * 1000)
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
