"""生命周期钩子 → 逐请求统计 + 动作清单（官方 ``RunHooks``）。

为什么必须有它
--------------
"agent 能力怎么样"这个问题的**所有**答案都要从钩子里取：调了几次模型、经过了哪些
agent、发生了哪几次 handoff、工具被调了几次。这些量只有 SDK 内部知道，
事后从日志里 grep 拼不出来（尤其是比率：返工率 = 未一次通过的轮次 / 总轮次）。

为什么钩子实例是**模块级单例**、而不是每个请求 new 一个
------------------------------------------------------
钩子的每个方法都收到 ``RunContextWrapper``，其中 ``.context`` 就是**本次请求的**
``TaskContext`` —— 于是"单例钩子 + 按 context 写统计"就得到了天然的并发安全：
没有共享可变状态，钩子自己无状态。反过来，如果每请求 new 一个钩子，就得把它
一路传进 ``as_tool()``（那是构造期绑定，agent 是模块级单例）—— 传不进去。

⚠️ 一个容易漏的事实：``agent.as_tool()`` 的嵌套 run **不会**自动继承父 run 的
hooks（SDK 源码里 ``as_tool`` 有独立的 ``hooks=`` 形参，默认 None）。所以必须显式
``as_tool(..., hooks=AGENT_HOOKS)``，否则 expert 的模型调用次数会**静默少算**。

两条线：统计（按量）与动作清单（按步）
------------------------------------
* 统计（``AgentStats``）：**按量**聚合 —— 跑了几次模型、几次工具。
* 动作清单（``steps.StepLog``）：**按步**展开 —— 每一步调了哪个工具、什么参数、
  成没成、花了多久。前者回答"多不多"，后者回答"对不对"。

后者为什么光有钩子不够：工具抛异常时官方**先把异常转成一句给模型看的话**，
``on_tool_end`` 拿到的已经是那句话（实测：失败路径照样触发这个钩子）⇒ 从钩子侧
分不出"真的成功了"与"收到一句错误文本"。所以成败由**工具自己的错误处理函数**
先定格，见 ``steps.tool_error_handler``。

另外，``on_tool_end`` 还是**反思闸门的判决口**：每次动作定格后判一次"够了没有"
（见 ``reflection.maybe_reflect``）。闸门数的几个量全是"定格"的产物，所以判点必须
落在定格**之后** —— 早一步会让重复数系统性偏小，而"指标偏小"没有任何东西会报错。
"""

from __future__ import annotations

from typing import Any

from agents import RunHooks

from . import reflection
from .context import AgentStats, TaskContext
from .steps import StepLog, calls_from_response, steps_enabled

__all__ = ["AgentRunHooks", "AGENT_HOOKS", "stats_of", "steps_of"]


def stats_of(context: Any) -> AgentStats | None:
    """从钩子收到的 context 里取 ``AgentStats``。

    context 在 SDK 里有两三种形态（``RunContextWrapper`` / ``AgentHookContext`` /
    ``ToolContext``），但都通过 ``.context`` 指向用户自己的 TContext。这里逐层兜底，
    取不到就返回 None（钩子不该因为形态差异抛异常）。
    """
    inner = getattr(context, "context", None)
    if isinstance(inner, TaskContext):
        return inner.stats
    return None


def steps_of(context: Any) -> StepLog | None:
    """从钩子收到的 context 里取动作清单（与 ``stats_of`` 同一套兜底口径）。"""
    inner = getattr(context, "context", None)
    if isinstance(inner, TaskContext):
        return inner.steps
    return None


def _tool_names(agent: Any) -> list[str]:
    """这个 agent 挂了哪些工具（按名字取，跳过空名）。

    口径必须与 ``on_tool_start`` 一致 —— 那里记的也是 ``tool.name``。若这里改用类名
    或对象 id，曝光侧与结果侧就再也对不上，而症状是"曝光有值、选中率恒为 0"，
    看板上看起来像"模型从来不用工具"。

    这里**不去重**：去重只在播报点做（``StepLog.note_exposed``），两处各写一份一定
    会漂，而漂的表现是分母偏大、选中率偏低。
    """
    out: list[str] = []
    for tool in getattr(agent, "tools", None) or []:
        name = getattr(tool, "name", "") or ""
        if name:
            out.append(name)
    return out


def _cached_input_tokens(usage: Any) -> int:
    """这次调用里命中**上游前缀缓存**的输入 token 数；读不到就是 0。

    字段位置：官方 ``Usage.input_tokens_details.cached_tokens``
    （``agents/usage.py`` 的 ``InputTokensDetails``）。OpenAI 系网关把它放在
    ``usage.prompt_tokens_details.cached_tokens``，SDK 的
    ``openai_chatcompletions`` 会转过来（源码 ``_normalize_input_tokens_details``）。

    ⚠️ DeepSeek 是两个字段都给（2026-11-01 实测：
    ``prompt_tokens_details.cached_tokens`` 与顶层的
    ``prompt_cache_hit_tokens`` 同为 512），所以**不需要**自己解原始 payload、
    也不需要打开 ``preserve_raw_usage``。这一点与"顶层字段只有 DeepSeek 认识"
    的猜测相反，别照猜测改。

    ⚠️ 读不到时返回 0 而**不估**：估出来的命中数会让"缓存省了多少钱"变成编的，
    且看不出是编的。0 与"真没命中"分不开 —— 这正是要同时看 ``input_tokens``
    的原因（后者也为 0 ⇒ 上游压根没回 usage）。
    """
    details = getattr(usage, "input_tokens_details", None)
    try:
        # 这里**不**先判 ``details is None``：``getattr(None, "cached_tokens", 0)``
        # 本来就走默认值。多写一层判空只会让人以为"没有它就会炸"，而变异验证
        # 证明它删掉以后行为完全一样（2026-11-01 实测：判空版被删，测试全绿）。
        # 真正必要的是下面那个 except：上游把字段写成字符串时会抛。
        return max(0, int(getattr(details, "cached_tokens", 0) or 0))
    except (TypeError, ValueError):
        return 0


class AgentRunHooks(RunHooks[TaskContext]):
    """把 SDK 的生命周期事件累计进 ``TaskContext``（统计 + 动作清单）。"""

    async def on_llm_start(self, context, agent, system_prompt, input_items) -> None:
        """每一次 LLM 调用前触发。**这就是 ``max_turns`` 计数的那个单位。**"""
        stats = stats_of(context)
        if stats is not None:
            stats.model_calls += 1

    async def on_llm_end(self, context, agent, response) -> None:
        """每一次 LLM 调用**之后**触发（拿到这一次的响应）。

        这里取的是"模型请求了哪些工具" —— 工具的两个钩子都**不带参数、也不带定位
        编号**，所以工具入参只能从响应这一侧取（从工具钩子那一侧取不到）。

        ⚠️ **token 累计必须放在 ``steps_enabled()`` 那句之前**（2026-10-21）。它读的是
        ``AGENT_TOOL_STEPS``，而那是"动作清单"的开关；把成本统计挡在一个观察开关后面，
        表现为"关掉步骤记录之后，token 预算再也不触发"—— 一个配了预算、看着有配置项、
        实际永远不生效的失效，且没有任何东西会报错。
        """
        stats = stats_of(context)
        usage = getattr(response, "usage", None)
        if stats is not None and usage is not None:
            # 官方 ``Usage`` 的字段名（``agents/usage.py``）：input_tokens / output_tokens。
            # 用 getattr + or 0：不是每个网关/模型都会回全这些字段（本地模型常见），
            # 缺字段时按 0 算是安全的 —— 少算只会"晚一点触发预算"，不会拦错。
            stats.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            stats.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
            # 命中量必须在**同一处**累加：拆到别的地方写就会出现"input 记了、
            # cached 忘记"，而那种漂移的表现只是命中率偏低，没有任何东西报错。
            stats.cached_input_tokens += _cached_input_tokens(usage)
        if not steps_enabled():
            return
        log = steps_of(context)
        if log is not None:
            log.note_calls(calls_from_response(response))

    async def on_agent_start(self, context, agent) -> None:
        """当前 agent 变更时触发（含首次）。

        除了记名字，这里还要**登记一次曝光**：这个 agent 挂的工具从此摆到模型面前了。
        漏掉这一句的后果很窄但很隐蔽 —— 只要某个工具真的被用过，选中率照样算得出来；
        只有当它**一次都没被用过**时，指标里才什么都不留，而"从没用过"与"根本没挂"
        在 ``/metrics`` 上是同一个样子。所以这一句的价值全在"零调用"那一侧。
        """
        stats = stats_of(context)
        name = getattr(agent, "name", "")
        if stats is not None and name and name not in stats.agents:
            stats.agents.append(name)
        log = steps_of(context)
        if log is not None:
            log.note_exposed(_tool_names(agent))

    async def on_handoff(self, context, from_agent, to_agent) -> None:
        """handoff 发生时触发。这是拓扑是否真的被走到的**唯一直接证据**。

        ⚠️ 2026-10-20：前台 → 文学专家重新改回官方 ``handoffs=``（此前是图的条件边），
        所以这个回调**又开始真的被触发了**。它记的 ``stats.handoffs`` 就是
        ``agent_handoff_total`` 的来源 —— 这条指标回答"多少请求被交给了专家"。
        改拓扑时如果忘了它，指标会静默归零，而代码看起来完全正常。
        """
        stats = stats_of(context)
        if stats is not None:
            stats.handoffs.append(
                (getattr(from_agent, "name", ""), getattr(to_agent, "name", ""))
            )

    async def on_tool_start(self, context, agent, tool) -> None:
        """本地工具开始执行。"""
        stats = stats_of(context)
        if stats is not None:
            stats.tool_calls += 1
        log = steps_of(context)
        if log is not None:
            log.begin(getattr(tool, "name", "") or "")

    async def on_tool_end(self, context, agent, tool, result) -> None:
        """本地工具执行结束。

        ⚠️ **失败路径也会走到这里** —— 官方把异常转成一句给模型看的话之后，照样
        触发这个钩子（实现在 SDK 的 ``run_internal/tool_execution.py``）。所以
        成败**不在这里判**：失败由工具自己的错误处理函数先定格（见
        ``steps.tool_error_handler``），这里只做收尾；对已定格的条目不会重复播报。

        收尾之后另做一件事：**判一次反思闸门**（见 ``reflection.maybe_reflect``）。
        位置选在这里是因为闸门数的几个量（工具总数、重复次数）都是"定格"的产物 ——
        早一步判会读到还没定格的清单，重复数会系统性偏小。

        ⚠️ 工具名**必须传进去**（2026-10-22）：闸门里有一个无条件的检查点是
        "刚交付"（判产出是否为目标要的东西）。不传名字，那个检查点永远不会触发 ——
        而"从没在交付前检查过"与"检查了都没问题"在看板上完全一样。
        """
        log = steps_of(context)
        name = getattr(tool, "name", "") or ""
        if log is not None:
            log.finish(name, result)
        inner = getattr(context, "context", None)
        if isinstance(inner, TaskContext):
            reflection.maybe_reflect(inner, tool=name)


AGENT_HOOKS = AgentRunHooks()
"""进程级单例：无状态，靠 context 定位本次请求的统计对象。"""
