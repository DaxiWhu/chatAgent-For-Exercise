"""工具单步执行的**隔离**与**单步超时**。

为什么需要它
------------
官方对**同步**工具走 ``asyncio.to_thread``（``agents/tool.py``），而 ``to_thread``
用的是事件循环的**默认线程池** —— 与服务层 ``_off()`` 是同一个池（限流、会话归属、
落库、结算全在它上面）。于是一个卡住的检索不只卡住这一条请求：池满之后连"进门"
那几步都开始排队，症状是**整个进程变慢**，而指标上到处都慢、看不出源头在工具。

所以这里做两件事，缺一不可：

1. **独立线程池** —— 工具再慢也只污染自己这个池。这个池是**可牺牲的**：它被打满时
   的表现是"工具调用普遍变慢/超时"，而不是"服务不可用"。
2. **单步超时** —— 到点就放弃等待，把这一圈还给模型。没有它的时候，唯一的上界是
   请求级总时限（``REQUEST_TIMEOUT_SECONDS``，默认 **0 = 不限**）⇒ 上游一卡，
   这条请求永远不结束，而循环体一次都不转（它停在 await 上）。

⚠️ 超时**停不掉正在跑的那个线程**（Python 的硬限制）。它能做到的是：排队中的任务
被取消、正在跑的**结果被丢弃**。所以"超时"的语义是**放弃等待**，不是"撤销执行"——
对有副作用的动作（落盘）必须让模型知道这一点，见 ``steps.tool_error_handler`` 里
专门为超时写的那句文案。

超时之后走哪条路（这是与"自己另立一套兜底"的关键区别）
--------------------------------------------------------
这里只负责**抛** ``TimeoutError``，不自己决定怎么收场。抛出去之后：

* 官方把它交给工具构造期挂的 ``failure_error_function``（``tool_error_handler``）⇒
  步骤清单记一条 ``failed``（档位 ``timeout``）+ 回给模型一句可操作的话；
* 模型拿着这句话自己决定：换一种查法、缩小范围、还是如实说这条做不了 ——
  这就是"注入错误上下文、让 agent 自我修复"；
* 若同一个动作反复超时，指纹重复计数（``StepLog.max_repeat``）上升 ⇒ 反思闸门
  按既有阈值介入 ⇒ 还不行才踩刹车。

也就是说：**这里不新增熔断逻辑**。重复 N 次即熔断、部分结果 + 未完成步骤，都已经
在步骤清单与收尾定档里，多一套判据只会多一处漂移。

关掉它（``AGENT_TOOL_TIMEOUT_SECONDS=0``）
------------------------------------------
退化成"无限等待"，但**仍然走独立池** —— 因为隔离是安全边界，不是优化。这个组合的
行为与改造前一致（同样是丢进线程池、同样没有单步上界），只是不再和服务层抢线程。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .. import observability
from ..envutil import env_float, env_int

__all__ = [
    "default_workers",
    "dispose",
    "run_tool_step",
    "step_timeout_seconds",
    "workers",
]


def default_workers() -> int:
    """池容量：与 CPython 默认池同口径（``min(32, cpu+4)``）。

    为什么不去猜一个更小的值：**隔离前**工具本来就和整个服务层共享这一个量级的
    线程数；隔离后再给一个更小的上限，等于把"工具变慢"换成"工具排队"，而后者在
    看板上更难看懂（耗时上升、但每一步都成功）。
    """
    return min(32, (os.cpu_count() or 4) + 4)


def workers() -> int:
    """实际池容量。``AGENT_TOOL_WORKERS=0``（默认）= 按 CPU 推导。"""
    return env_int("AGENT_TOOL_WORKERS", 0, lo=0) or default_workers()


def step_timeout_seconds() -> float:
    """单步工具执行的等待上界（秒）。``0`` = 不限（行为与改造前一致）。

    默认 20s：本项目挂给模型的工具全是**本地**动作（检索、落盘、交付），正常都在
    毫秒到秒级；20s 已经够宽，再宽就等于没有这个闸门。它是**独立于**模型调用超时
    （``AGENT_TIMEOUT_SECONDS``）的一个值 —— 两者的对象不同（一次 HTTP 调用 /
    一次工具执行），调一个不该牵动另一个。
    """
    return env_float("AGENT_TOOL_TIMEOUT_SECONDS", 20.0, lo=0.0)


_POOL: ThreadPoolExecutor | None = None


def _pool() -> ThreadPoolExecutor:
    """懒建、进程内唯一。懒建的理由：配置是**运行期**读的（测试可改 env）。"""
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(
            max_workers=workers(), thread_name_prefix="agent-tool"
        )
        logging.info("工具执行线程池已建立：%d 个线程（与服务层线程池隔离）", workers())
    return _POOL


async def run_tool_step(
    fn: Callable[..., Any], /, *args: Any, tool: str = "", **kwargs: Any
) -> Any:
    """在线程池里跑一次工具实现，最多等 ``step_timeout_seconds()`` 秒。

    fn: 工具的**同步**实现（本项目的工具都写成同步的，异步外壳在这里）。
    tool: 工具名，只用于超时指标与日志（回调侧拿不到工具名，见 ``steps`` 的说明）。

    超时抛 ``TimeoutError`` —— 调用方（官方工具运行时）会把它交给
    ``failure_error_function``，不要在这里自己吞掉换成一句字符串：吞掉就等于
    "这一步失败了但清单里是成功的"，而那正是这张清单要防的事。
    """
    loop = asyncio.get_running_loop()
    call = functools.partial(fn, *args, **kwargs)
    future = loop.run_in_executor(_pool(), call)

    timeout = step_timeout_seconds()
    if timeout <= 0:
        return await future

    try:
        return await asyncio.wait_for(future, timeout)
    except TimeoutError:
        # `wait_for` 已经 cancel 过它；再 cancel 一次是为了覆盖"排队中还没开始"的
        # 那一档 —— 那种情况下线程池会直接跳过它，动作**不会发生**。
        # 已经在跑的那一档取消不掉（线程不可中断），它的结果会被丢弃。
        future.cancel()
        observability.counter("agent_tool_step_timeout_total", tool=tool or "?")
        logging.warning(
            "工具单步超时：%s 超过 %.1fs 未返回 ⇒ 放弃等待（线程仍在跑，结果丢弃）",
            tool or "?", timeout,
        )
        raise


def dispose(*, wait: bool = False) -> None:
    """进程收尾：丢下池引用，取消还在排队的任务。

    ``wait=False`` 是刻意的：一个卡住的工具会让 ``shutdown(wait=True)`` 停在那里，
    把"关服务"变成"等一根卡死的线程"。而 Docker 侧还有 ``stop_grace_period``
    兜底，没必要在应用里再赌一次。

    ⚠️ 线程池的线程是**非 daemon** 的，CPython 在解释器退出时会 join 它们 ——
    所以"卡住的工具"仍然可能让进程退出多花一点时间。这是超时改不掉的部分
    （线程不可中断），如实记在这里。
    """
    global _POOL
    pool, _POOL = _POOL, None
    if pool is None:
        return
    try:
        pool.shutdown(wait=wait, cancel_futures=True)
    except Exception as e:  # noqa: BLE001  关闭路径上的失败不该改变退出码
        logging.warning("工具线程池释放失败（不影响关闭）：%s", e)
