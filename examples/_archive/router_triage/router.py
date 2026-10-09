"""入口分流 —— 官方 deterministic 模式的形态（**由模型判定，不由规则判定**）。

    dispatch ──await classify(prompt)──> RouteDecision(task_types)
                                              │
                         ┌────────────────────┴────────────────────┐
                    kind = agent                              kind = rag
                         ↓                                        ↓
                    synthesize                               retrieve

与官方示例的对应关系
--------------------
官方 ``examples/agent_patterns/routing.py`` 把路由交给**模型**：

    triage_agent = Agent(
        name="triage_agent",
        instructions="Handoff to the appropriate agent based on the language of the request.",
        handoffs=[french_agent, spanish_agent, english_agent],
    )

它三个出口全是 Agent，所以可以直接 ``handoffs``。本项目不同：**「问答」那一支是
graph 里的 ``retrieve → generate`` 节点，不是 Agent**，``handoff`` 指不到它。

因此这里采用官方另一份示例（``deterministic.py``）的形态：一个 Agent 用
``output_type`` 输出**结构化判定**，代码按判定分支 ——

    outline_checker_agent = Agent(..., output_type=OutlineCheckerOutput)
    ...
    if not outline_checker_result.final_output.good_quality:
        exit(0)                       # 代码按模型的结构化输出做门禁

判据在**模型**手里（读 instructions、看用户原话），代码只负责"拿结论去分支"。

⚠️ 2026-10-02 的返工记录
------------------------
本模块此前是**三条正则规则**（``_CREATIVE_RULES`` / ``_ENGLISH_RULES`` /
``_OPENING_QUESTION``）拼"创作动作 + 作品名词"。那不是官方做法 —— 官方把路由
交给模型，规则是我自己发明的方案。用户指出后已改为模型判定。
**不要再把正则加回来**：分流判据一旦回到词表，就等于把官方原语换成了自定义规则。

⚠️ 本模块**依赖 ``openai-agents`` SDK**（要定义 Agent）。这与返工前的设计相反
（那时这里是"刻意不依赖 SDK"的纯正则模块）。"SDK 没装"仍由
``graph._agent_module("router")`` 返回 None 表达，降级路径不变。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from agents import Agent, RunConfig, Runner
from pydantic import BaseModel, Field

__all__ = ["CREATIVE_KINDS", "CreativeKind", "RouteDecision", "classify", "triage_agent"]

#: 走 agent 链路的作品种类（也是 ``task_types`` 的取值集合，有限）。
CREATIVE_KINDS = ("literary", "slides", "illustration")

CreativeKind = Literal["literary", "slides", "illustration"]


class RouteDecision(BaseModel):
    """分流结论 —— 官方 ``Agent(output_type=...)`` 的载荷。

    **只让模型填 ``task_types``**，``kind`` 与 ``reason`` 由它**派生**：

    * 派生的理由：三个字段都能填的话，模型可能给出自相矛盾的组合
      （``kind="rag"`` 却 ``task_types=["slides"]``），代码还要再写一层纠错。
      少一个可填字段就少一类不一致。
    * ``reason`` 是**有界**取值（直接进指标标签），派生保证它永远与 ``task_types``
      自洽 —— 这正是"标签基数可控"与"标签与分析口径一致"两条要求同时满足的写法。
    """

    task_types: list[CreativeKind] = Field(
        default_factory=list,
        description=(
            "本次要交付的作品种类，可多选；交付物不是作品时留空数组。"
            "literary=小说/散文/诗歌/剧本/歌词，slides=PPT/演示文稿/汇报大纲，"
            "illustration=插图/海报/封面/示意图"
        ),
    )

    @property
    def kind(self) -> str:
        """``agent``（走 agent 链路）或 ``rag``（走原问答链路）。"""
        return "agent" if self.task_types else "rag"

    @property
    def reason(self) -> str:
        """命中原因。取值有限，直接当指标标签用。"""
        if not self.task_types:
            return "no_creative_cue"
        if len(self.task_types) > 1:
            # 命中多种作品（"做个 PPT 再配张图"）：单独一档。多交付物的失败率
            # 明显高于单作品，混在一起会看不出来 —— 它是"能力评估"最该盯的一档。
            return "creative_mix"
        return f"creative_{self.task_types[0]}"


_INSTRUCTIONS = (
    "你是入口分流器。判断用户这次要的是「一份作品」还是「一段说明」，只输出结构化结论。\n"
    "\n"
    "# 要一份作品（填 task_types）\n"
    "- literary：小说 / 散文 / 诗歌 / 剧本 / 歌词 —— 作品本身就是交付物。\n"
    "- slides：PPT / 演示文稿 / 汇报大纲 —— 交付物是结构化大纲。\n"
    "- illustration：插图 / 配图 / 海报 / 封面 / 示意图 —— 交付物是一张图。\n"
    "同时要多种作品时全部填上（「做个 PPT 再配一张插图」⇒ slides + illustration）。\n"
    "\n"
    "# 是一段说明（task_types 留空数组）\n"
    "- 问方法、问原理、问配置：交付物是一段说明，不是一份作品。\n"
    "  「怎么做一份 PPT」「如何写小说」「限流是怎么算的」「系统里限流配置在哪」。\n"
    "- 让你写代码 / 脚本 / SQL：交付物是能跑的东西，不是文学作品。\n"
    "- 闲聊、追问、澄清、以及其他一切不知道归哪类的输入。\n"
    "\n"
    "# 边界（容易判反的两组）\n"
    "- 「写一篇介绍小说叙事技巧的文章」→ 交付物是一篇说明文 ⇒ 留空。\n"
    "- 「写一篇关于灯塔的短篇小说」→ 是作品 ⇒ literary。\n"
    "- 拿不准时留空。"
)

triage_agent: Agent[Any] = Agent(
    name="triage_agent",
    instructions=_INSTRUCTIONS,
    output_type=RouteDecision,
)

#: 分流只做一次判定，不需要工具。
CLASSIFY_MAX_TURNS = 2


async def classify(prompt: str, *, model: Any | None = None) -> RouteDecision:
    """让 triage agent 判定这次请求走哪条链路。

    prompt: 用户本轮的输入。
    model: 模型覆盖；None 时取 ``runtime`` 里装配好的那一个。

    空输入**短路**（不花一次模型调用就得出"不是作品"的结论）—— 这不是分流规则，
    只是"没有内容可判"这个平凡情形的省流处理。

    ⚠️ 调用方（``graph.dispatch``）必须处理异常：分流失败不能把请求带崩，
    要降级回原问答链路（见那里的 ``classify_failed``）。
    """
    if not (prompt or "").strip():
        return RouteDecision(task_types=[])

    bound = model
    if bound is None:
        from .runtime import get_model

        bound = get_model()
    if bound is None:
        raise RuntimeError("agent 链路未就绪（未装配模型）")

    result = await Runner.run(
        triage_agent,
        (prompt or "").strip(),
        # 只带模型：triage 的产出是**给代码看的判定**，不是交付物，
        # 所以不挂出口护栏（那套护栏只对"产出最终输出的 agent"有意义）。
        run_config=RunConfig(model=bound),
        max_turns=CLASSIFY_MAX_TURNS,
    )
    decision = result.final_output_as(RouteDecision, raise_if_incorrect_type=True)
    logging.debug("分流判定：%s（%s）", decision.kind, decision.reason)
    return decision
