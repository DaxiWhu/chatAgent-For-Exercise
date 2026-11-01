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

⚠️ **旁路的钱也算钱**（2026-10-21 补）。窗口压缩与反思闸门各自 bypass 建 client，
它们的 token 不经过 ``Runner`` 的 usage ⇒ 只数主链路会**系统性低估**：
压缩要把老历史整段读一遍，是长任务里最贵的一笔。所以 ``used_of`` 取
"主链路 + 旁路"之和，而指标仍分两条上报（口径不同，见 ``report``）。
⚠️ 这笔账有**一次调用**的延迟：注入口的顺序是"预算 → 压缩 → 裁剪 → 注入"，
压缩发生在判据**之后**，所以它花的钱要到下一次模型调用前的 ``check`` 才看见。
不把判据挪到最后，是因为那样裁剪也白做了 —— 省钱的顺序不能反。
"""

from __future__ import annotations

import logging
from typing import Any

from .. import observability
from ..envutil import env_int

__all__ = [
    "AgentStoppedByBudget",
    "check",
    "note_bypass",
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
    """这次运行**到目前为止**烧掉的 token（输入 + 输出，**主链路 + 旁路**）。

    取不到一律按 0：这是个观测/闸门函数，不该因为上下文形态差异抛异常。
    ⚠️ 按 0 处理的代价是"这一次不会被拦"，而不是"拦错"—— 方向是安全的
    （宁可少拦一次，也不要在量不到的时候把一次正常长任务掐断）。

    ⚠️ **必须把旁路算进来**（2026-10-21）。只算主链路时，预算是"模型调用的钱"，
    而账单是"所有出站调用的钱"：窗口压缩要把老历史整段读一遍，反思闸门也要发一次
    请求，两者都不经过 ``Runner`` 的 usage ⇒ 只算主链路会**系统性低估**，
    表现是"预算 5 万、实际花了 8 万"，而看板上预算那条线从来没被碰到。
    """
    stats = getattr(context, "stats", None)
    if stats is None:
        return 0
    return (
        int(getattr(stats, "input_tokens", 0) or 0)
        + int(getattr(stats, "output_tokens", 0) or 0)
        + int(getattr(stats, "bypass_input_tokens", 0) or 0)
        + int(getattr(stats, "bypass_output_tokens", 0) or 0)
    )


def note_bypass(stats: Any, source: str, usage: Any) -> None:
    """记一笔**旁路**模型调用的 usage（反思闸门 / 窗口压缩调用它）。

    source: 来源标签，取值只有 ``reflection`` / ``compaction`` —— 它进指标标签，
        所以必须是有界枚举（写成 ``type(exc).__name__`` 那种会把基数打爆）。

    为什么要有这**一个**统一入口，而不是让两个模块各自往 ``stats`` 上写：
    两处各写一份一定会漂（一处漏加、一处加错字段），而漂的表现是"预算偶尔偏小"，
    没有任何东西会报错。累加口径只有一处，改也只改这里。

    ⚠️ 这个函数会被**工作线程**调用（两个旁路都跑在 ``asyncio.to_thread`` 里），
    而主线程同时在读同几个数。Python 的 dict 单键赋值是原子的，但 ``+=`` 不是 ——
    理论上可能丢一次增量。这里**不做锁**：丢一次计数的后果是"预算晚一点触发"，
    而加锁的后果是把一次 HTTP 往返变成跨线程串行。方向安全，取安全的那一侧。

    ⚠️ 拿不到 usage（网关不回）时**什么都不做**，也不补一个估算值 —— 编出来的数
    会让预算的判据失去意义，且看不出是编的。
    """
    if stats is None or usage is None:
        return
    slot = getattr(stats, "bypass_tokens", None)
    if not isinstance(slot, dict):
        return
    entry = slot.setdefault(source, {"input": 0, "output": 0, "cached": 0})
    entry["input"] = int(entry.get("input", 0) or 0) + int(
        getattr(usage, "input_tokens", 0) or 0
    )
    entry["output"] = int(entry.get("output", 0) or 0) + int(
        getattr(usage, "output_tokens", 0) or 0
    )
    # 旁路的缓存命中量同样要记：压缩要把老历史整段读一遍，是长任务里最贵的一笔，
    # 它有没有命中缓存直接决定"压缩贵不贵"。漏记的表现是只看主链路会高估成本、
    # 或者反过来低估缓存收益，两种都只表现为"数字对不上"，不会报错。
    details = getattr(usage, "input_tokens_details", None)
    if details is not None:
        entry["cached"] = int(entry.get("cached", 0) or 0) + max(
            0, int(getattr(details, "cached_tokens", 0) or 0)
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
    #
    # ⚠️ 文案必须与**实际后果**一致（2026-10-21 修）：这里是从注入口抛出去的 ⇒
    # ``Runner.run`` 拿不到结果 ⇒ ``result`` 是 None ⇒ ``answer`` 是空串 ⇒
    # 兜底判定表落到第⑤行（failed + token_budget）。也就是说**这一轮不会形成答复正文**。
    # 之前那句"已产出的内容照常交付"在非流式下是**假的**，在流式下也只是一半真
    # （字已经在客户端手里，但那份增量不进会话历史，刷新或换端就没了）。
    # 一句会让人以为"东西还在"的日志，会让值班的人不去捞已经产出的资产。
    logging.warning(
        "本轮 token 已用 %d，超过预算 %d ⇒ 停止继续调用模型。这一轮不会形成答复正文："
        "非流式下客户端什么都不会收到；流式下已经推出去的增量留在客户端，但不进会话历史；"
        "已落盘的资产照常留在 asset 目录。",
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

    * ``agent_run_tokens_total{kind}`` —— 主链路成本。``sum(rate(...))`` 就是吞吐
      成本曲线，按 ``agent`` 拆能回答"钱花在哪个 agent 上"；
    * ``agent_bypass_tokens_total{kind,source}`` —— **旁路成本**（反思闸门 / 窗口压缩）。
      ⚠️ 它与上面那条**不是**同一件事，加起来才是账单：主链路那条回答"这个 agent
      一圈多贵"，混进旁路就再也回答不了；而"这个部署一天花多少"必须两条相加；
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
    cached_input = int(getattr(stats, "cached_input_tokens", 0) or 0)
    if cached_input:
        # ⚠️ 独立指标，**不能**并进上面那条 `kind="input"`：命中量是输入的子集，
        # 并进去以后 `sum(rate(...))` 就是双倍输入，而那条曲线正是成本口径。
        # 命中率 = 这条 / `agent_run_tokens_total{kind="input"}`。
        observability.counter(
            "agent_cache_hit_tokens_total", cached_input, agent=agent
        )
    if input_tokens:
        # 每次运行一条命中率，用来回答"缓存是不是真的在生效"。只看累计量的比值
        # 会被长短会话混在一起平均掉：一次长冷启动能把几十次热调用盖住。
        #
        # ⚠️ 恒为 0 有两种含义，别急着调缓存：① 网关压根不回命中字段；
        # ② 前缀真的一次没命中。区分办法是看 `agent_cache_hit_tokens_total`
        # 有没有**任何**增量 —— 但从没增长这件事本身也是零报错的，所以配了新网关
        # 之后要用真请求验一次（`tmp_probe/cache_probe.py` 那类探针）。
        observability.histogram(
            "agent_cache_hit_ratio", cached_input / input_tokens, agent=agent
        )
    bypass = getattr(stats, "bypass_tokens", None)
    if isinstance(bypass, dict):
        for source, entry in bypass.items():
            kind_in = int(entry.get("input", 0) or 0)
            kind_out = int(entry.get("output", 0) or 0)
            if kind_in:
                observability.counter(
                    "agent_bypass_tokens_total",
                    kind_in,
                    kind="input",
                    source=source,
                    agent=agent,
                )
            if kind_out:
                observability.counter(
                    "agent_bypass_tokens_total",
                    kind_out,
                    kind="output",
                    source=source,
                    agent=agent,
                )
    if run_budget() > 0 and model_calls > 0 and not (input_tokens or output_tokens):
        observability.counter("agent_run_tokens_unmeasured_total", agent=agent)
        logging.warning(
            "配了 token 预算（%d）但这次运行一个 token 都没量到 —— "
            "上游没返回 usage，预算不会生效。查网关的 usage 字段。",
            run_budget(),
        )
