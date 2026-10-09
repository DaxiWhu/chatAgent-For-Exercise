"""生命周期钩子 → 逐请求统计（官方 ``RunHooks``）。

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
"""

from __future__ import annotations

from typing import Any

from agents import RunHooks

from .context import AgentStats, TaskContext

__all__ = ["AgentRunHooks", "AGENT_HOOKS", "stats_of"]


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


class AgentRunHooks(RunHooks[TaskContext]):
    """把 SDK 的生命周期事件累计进 ``TaskContext.stats``。"""

    async def on_llm_start(self, context, agent, system_prompt, input_items) -> None:
        """每一次 LLM 调用前触发。**这就是 ``max_turns`` 计数的那个单位。**"""
        stats = stats_of(context)
        if stats is not None:
            stats.model_calls += 1

    async def on_agent_start(self, context, agent) -> None:
        """当前 agent 变更时触发（含首次）。"""
        stats = stats_of(context)
        name = getattr(agent, "name", "")
        if stats is not None and name and name not in stats.agents:
            stats.agents.append(name)

    async def on_handoff(self, context, from_agent, to_agent) -> None:
        """handoff 发生时触发。这是拓扑是否真的被走到的**唯一直接证据**。

        ⚠️ 2026-10-03 起本项目**一处 ``handoffs=`` 都不剩**（跨节点转移改由图的条件
        边做，见 ``src/agents/types.FrontlineDecision``），所以这个回调**当前不会
        被触发**。留着它是有意的：它是官方钩子的实现，一旦有人把 handoff 加回来，
        "谁交给了谁"必须立刻重新被计数 —— 而"加了 handoff 却没加钩子"的表现是
        指标静默少一块，读代码看不出来。

        ⇒ 跨节点的转移由 ``src/graph.py`` 的 ``frontline`` 节点**手工补记**一条
        （同一个 ``stats.handoffs`` 字段）。两处写同一个字段不是重复：一处覆盖
        SDK 内部的换人，一处覆盖图节点之间的跳转。
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


AGENT_HOOKS = AgentRunHooks()
"""进程级单例：无状态，靠 context 定位本次请求的统计对象。"""
