"""一次运行内的 token 预算 —— 烧穿了就把这一轮停下来。

为什么需要它
------------
``AGENT_MAX_TURNS``（默认 12）限的是**圈数**，不是**成本**。两者不是一回事：
一圈可能只花几百 token（闲聊），也可能花十几万（一次工具返回塞进了一大段文档，
或历史本来就长）。于是"12 圈"在一次长任务里可能是几十万 token，而代码里
**没有任何地方累计过它** —— 只有租户的**日**额度会在下一轮把用户挡在门外，
那时账单已经产生，而且看起来是"用户额度不够"，不是"这一轮跑飞了"。

判据放在**每次模型调用之前**（``runner.model_input_filter`` 的入口）。理由很具体：
放在运行结束时只是"事后记账"，钱已经花完；而放在工具结束时判不到"模型这一圈
准备再读一遍多长的历史" —— 那正是下一笔开销。

⚠️ 为什么挂在 ``call_model_input_filter`` 而不是 ``hooks.on_llm_start``：
钩子里抛异常的行为本仓库**没有验证过**（官方在 run 循环里对 hook 的调用有
try/except 的可能），而 input filter 抛异常会**原样冒到 ``Runner.run`` 外面** ——
第 3 级刹车（``reflection.AgentStoppedByGuard``）走的就是这条路，已有生产用例与
守卫。同一个"要中断这次运行"的动作，用一条**验证过的**通路，比用一条"看起来也行"
的通路安全：后者失效时的表现是"预算配了、从来没停过"，没有任何东西会报错。

⚠️ 默认 **不限**（``AGENT_RUN_TOKEN_BUDGET=0``），与 ``REQUEST_TIMEOUT_SECONDS`` 同一
口径：这两项都是"给这一次运行的总量封顶"，默认值配错（偏小）会大面积截断正常长任务，
而症状是"模型最近答得敷衍"。生产建议值见 ``.env.example``。

⚠️ **上游不报 usage 时，这个预算形同虚设**。这是它唯一的静默失效方式：
网关不返回 usage ⇒ 累计恒为 0 ⇒ 永远不触发。所以运行结束时会单独计一条
``agent_run_tokens_unmeasured_total``（配了预算、跑过模型、却一个 token 都没量到）——
没有那个计数，"预算配了但没生效"与"预算很宽松、从来没超"在读板上完全一样。
"""

from __future__ import annotations

import logging
from typing import Any

from .. import observability
from ..envutil import env_int

__all__ = [
    "AgentStoppedByBudget",
    "check",
    "remaining",
    "report",
    "run_budget",
    "used_of",
]


class AgentStoppedByBudget(Exception):
    """超预算时从**注入口**抛出，用来中断本次运行。

    与 ``reflection.AgentStoppedByGuard`` 同一手法、也必须如此：``runner.run_agent``
    末尾有一个 ``except Exception`` 的兜底（上游抖动、协议错都落进它）。拿普通异常
    去中断会被它吞成 ``exit_reason="error"`` —— 表现是"停是停了，理由变成崩溃"，
    于是运维去查一个不存在的故障，而真正要做的"调预算 / 查为什么这么费"没有人做。

    专有类型让 ``runner`` 能在兜底**之前**认出它，翻成 ``exit_reason="token_budget"``。
    """

    def __init__(self, used: int, budget: int) -> None:
        super().__init__(f"本轮已用 {used} token，超过预算 {budget}")
        self.used = used
        self.budget = budget


def run_budget() -> int:
    """本次运行的 token 预算（``0`` = 不限）。

    每次现读环境变量，不缓存：与 ``reflection`` 的阈值同一考虑 —— 测试要能逐例改，
    运维要能不重启就改。
    """
    return env_int("AGENT_RUN_TOKEN_BUDGET", 0, lo=0)


def used_of(context: Any) -> int:
    """这次运行**到目前为止**烧掉的 token（输入 + 输出）。

    取不到一律按 0：这是个观测/闸门函数，不该因为上下文形态差异抛异常。
    ⚠️ 按 0 处理的代价是"这一次不会被拦"，而不是"拦错"—— 方向是安全的
    （宁可少拦一次，也不要在量不到的时候把一次正常长任务掐断）。
    """
    stats = getattr(context, "stats", None)
    if stats is None:
        return 0
    return int(getattr(stats, "input_tokens", 0) or 0) + int(
        getattr(stats, "output_tokens", 0) or 0
    )


def remaining(context: Any) -> int:
    """离预算还差多少 token（``-1`` = 没有预算）。

    只给探针 / 评测脚本读：它们要能报出"这一轮离上限还有多远"，
    而不是只知道"有没有超"。
    """
    budget = run_budget()
    if budget <= 0:
        return -1
    return max(0, budget - used_of(context))


def check(context: Any) -> None:
    """到线就抛（**每次模型调用之前**由 ``runner.model_input_filter`` 调）。

    ⚠️ 判据是 ``>=`` 而不是 ``>``：预算是"最多还能花这么多"，正好花满就该停 ——
    用 ``>`` 会允许再花一整圈，而那一圈正是最贵的一圈（历史最长、工具返回最多）。
    """
    budget = run_budget()
    if budget <= 0:
        return
    used = used_of(context)
    if used < budget:
        return
    observability.counter("agent_run_budget_stop_total")
    # 日志带具体数字：只写"超预算"无法回答"超了多少、预算配在哪一档"，
    # 而调这个配置的人第一眼就要看这两个数。
    logging.warning(
        "本轮 token 已用 %d，超过预算 %d ⇒ 停止继续调用模型（已产出的内容照常交付）",
        used,
        budget,
    )
    raise AgentStoppedByBudget(used, budget)


def report(stats: Any, *, agent: str = "") -> None:
    """运行结束时上报 token 用量（**唯一**一处，按"运行时切片"取数）。

    ``stats``: ``runner`` 传进来的**本次运行切片**（``dataclasses.replace`` 出来的），
    不是跨节点累计的那份 —— 与 ``_report_run_metrics`` 同一口径。用累计那份会让
    一次请求里第二个节点的用量把第一个节点的也算进去，看板上的"每次运行成本"翻倍。

    两条指标的分工：

    * ``agent_run_tokens_total{kind}`` —— 成本。``sum(rate(...))`` 就是吞吐成本曲线，
      按 ``agent`` 拆能回答"钱花在哪个 agent 上"；
    * ``agent_run_tokens_unmeasured_total`` —— **预算是否真的在生效**。只在
      "配了预算 + 跑过模型 + 一个 token 都没量到"时计一条。⚠️ 它涨不等于出错，
      等于"这个部署里预算根本没起作用"（网关没回 usage），此时别去调预算值。
    """
    input_tokens = int(getattr(stats, "input_tokens", 0) or 0)
    output_tokens = int(getattr(stats, "output_tokens", 0) or 0)
    model_calls = int(getattr(stats, "model_calls", 0) or 0)
    if input_tokens:
        # counter 的第二个位置参数是"这一次加多少"（``observability.counter`` 的签名），
        # 不是标签 —— token 是累计量，按 1 加会得到一个毫无意义的值。
        observability.counter(
            "agent_run_tokens_total", input_tokens, kind="input", agent=agent
        )
    if output_tokens:
        observability.counter(
            "agent_run_tokens_total", output_tokens, kind="output", agent=agent
        )
    if run_budget() > 0 and model_calls > 0 and not (input_tokens or output_tokens):
        observability.counter("agent_run_tokens_unmeasured_total", agent=agent)
        logging.warning(
            "配了 token 预算（%d）但这次运行一个 token 都没量到 —— "
            "上游没返回 usage，预算不会生效。查网关的 usage 字段。",
            run_budget(),
        )
