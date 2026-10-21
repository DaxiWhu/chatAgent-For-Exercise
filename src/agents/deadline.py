"""一次运行的**墙钟上限** —— 跑太久就停下来，不管它是在调模型还是在等工具。

为什么需要它
------------
``AGENT_MAX_TURNS``（默认 12）限的是**圈数**，不是**时间**。两者不是一回事：
一圈可能 200ms（闲聊回一句），也可能 50 秒（模型想很久 + 工具慢慢查）。于是
"12 圈"的实际时长在两次请求之间能差两个数量级，而代码里**没有任何地方回答过**
"这次运行最多跑多久"。

现在实际生效的上界是**隐式**的，由三个各自独立的值相乘得到：

    12 圈 × (单次模型调用 30s + 单步工具 20s) ≈ 10 分钟

它只存在于"把这三项乘起来"之后，所以：既配不出来（想给某类任务封 3 分钟做不到），
也说不清楚（运维看到"一次请求跑了 8 分钟"，得自己反推是哪一项放行的）。

与相邻两项的分工（三者对象不同，不要合并）
----------------------------------------
* ``AGENT_TIMEOUT_SECONDS``    —— **一次模型调用**（HTTP 客户端超时）；
* ``AGENT_TOOL_TIMEOUT_SECONDS`` —— **一次工具执行**（线程池等待上界）；
* ``AGENT_RUN_TIMEOUT_SECONDS``  —— **一次运行的合计**（本模块），从 ``Runner.run``
  开始算到收尾，中间的排队、重试、模型思考全算在内。

判据放在注入口的理由与 token 预算相同（``budget`` 模块 docstring）：钩子没有返回值、
后台任务抛异常没人接，唯一握有控制权、且**已经验证过能中断本次运行**的时刻就是
``call_model_input_filter``。

⚠️ 它拦不住"卡在某一次工具调用里" —— 那种情况由工具单步超时管。两者是串联关系：
单步超时保证"每一小段都有上界"，本模块保证"所有这些小段加起来也有上界"。

⚠️ 默认 **不限**（``0``），与 ``AGENT_RUN_TOKEN_BUDGET`` / ``REQUEST_TIMEOUT_SECONDS``
同一口径：给一个"看起来合理"的默认值会**静默截断**长但合法的任务，而症状是
"模型最近答得敷衍"，最难排查。生产必须显式配置。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .. import observability
from ..envutil import env_float

__all__ = [
    "AgentStoppedByDeadline",
    "check",
    "elapsed",
    "mark_start",
    "run_deadline_seconds",
]


class AgentStoppedByDeadline(Exception):
    """到点之后从**注入口**抛出，用来中断本次运行。

    与 ``reflection.AgentStoppedByGuard`` / ``budget.AgentStoppedByBudget`` 同一手法、
    也必须如此：``runner.run_agent`` 末尾有一个 ``except Exception`` 兜底（上游抖动、
    协议错都落进它）。拿普通异常去中断会被它吞成 ``exit_reason="error"`` ——
    表现是"停是停了，理由变成崩溃"，于是运维去查一个不存在的故障，而真正要做的
    "调时限 / 查为什么这么慢"没有人做。
    """

    def __init__(self, elapsed_seconds: float, limit_seconds: float) -> None:
        super().__init__(f"本轮已跑 {elapsed_seconds:.0f}s，超过上限 {limit_seconds:.0f}s")
        self.elapsed_seconds = elapsed_seconds
        self.limit_seconds = limit_seconds


def run_deadline_seconds() -> float:
    """本次运行的合计时限（秒；``0`` = 不限）。

    每次现读环境变量，不缓存：与 ``budget.run_budget`` 同一考虑 —— 测试要能逐例改，
    运维要能不重启就改。
    """
    return env_float("AGENT_RUN_TIMEOUT_SECONDS", 0.0, lo=0.0)


def mark_start(context: Any) -> None:
    """在运行开始时记下起点（``runner.run_agent`` 调一次）。

    用 ``time.monotonic()`` 而不是 ``time.time()``：墙上时钟会被 NTP 校时或用户改
    系统时间拨动，而"拨回去"在闸门里的表现是"跑了很久却判成还没到线" —— 一个
    只有真出事那天才会发现的静默失效。
    """
    try:
        context.started_at = time.monotonic()
    except Exception:  # noqa: BLE001  上下文形态不认识 ⇒ 这一侧不生效，绝不带崩主线
        return


def elapsed(context: Any) -> float:
    """已经跑了多少秒；没记过起点时返回 ``0``。

    ⚠️ 没记过起点（``started_at == 0``）**按 0 处理**、而不是"从现在开始算"：
    后者会让第一次判据变成"刚跑了 0 秒"，看起来一样；但前者在"上下文被复用、
    忘了 mark_start"时是"永远不触发"（方向安全），后者是"每次调用都从 0 起算"
    （永远不触发，且更难发现）。两者都安全，选语义更诚实的那个。
    """
    started = float(getattr(context, "started_at", 0.0) or 0.0)
    if started <= 0:
        return 0.0
    return max(0.0, time.monotonic() - started)


def check(context: Any) -> None:
    """到点就抛（**每次模型调用之前**由 ``runner.model_input_filter`` 调）。

    ⚠️ 判据是 ``>=`` 而不是 ``>``：与预算同一口径（正好到点就该停）。

    ⚠️ 为什么在**调用之前**判而不是之后：之后判等于"这一圈已经跑完了才说超时"，
    而一整圈正是最贵的那一格（它可能又花 50 秒、又调好几次工具）。之前判能把
    "已经超了"的那一圈整个省掉。
    """
    limit = run_deadline_seconds()
    if limit <= 0:
        return
    used = elapsed(context)
    if used < limit:
        return
    observability.counter("agent_run_deadline_stop_total")
    # 日志带具体数字：只写"超时"无法回答"超了多少、上限配在哪一档"，
    # 而调这个配置的人第一眼就要看这两个数。
    logging.warning(
        "本轮已跑 %.0fs，超过上限 %.0fs ⇒ 停止继续调用模型"
        "（这一轮不会形成答复正文；流式下客户端已收到的增量保留，但不进会话历史）",
        used,
        limit,
    )
    raise AgentStoppedByDeadline(used, limit)
