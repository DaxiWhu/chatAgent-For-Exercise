"""四个专家 agent —— 注意它们**没有**共同的接线方式。

```
专家                 产出最终给用户看？      接线           谁按选择键
--------------------------------------------------------------- -------
literary_writer      是（作品就是交付物）    handoff         模型
slides_maker         否（结构化大纲）        as_tool         模型
visual_artist        否（资产路径）          as_tool         模型
researcher           否（取证材料）          as_tool         模型
evaluator            否（给代码看的分数）    代码 while      **代码**
--------------------------------------------------------------- -------
```

判据是官方 ``multi_agent`` 页那一句：专家"helps with a bounded subtask but **should not take
over the user-facing conversation**"就用 ``as_tool``；"routing itself is part of the
workflow"、专家要**接管对话**就用 ``handoff``。

三个 ``as_tool`` 专家的共同点：instructions 都是一句话级别、产出都结构化、
用户永远看不到它们说了什么。这与官方 ``agents_as_tools.py`` 里那三个"instructions 一行、
零工具"的翻译 agent 是同一件事。
"""

from __future__ import annotations

import json

from agents import Agent, RunContextWrapper, RunResult, RunResultStreaming, handoff

from .context import TaskContext
from .tools import generate_image, kb_search
from .types import ImageBrief, ResearchDigest, SlideDeck

__all__ = [
    "extract_illustration",
    "extract_research",
    "extract_slide_deck",
    "literary_agent",
    "on_literary_handoff",
    "researcher_agent",
    "slides_agent",
    "visual_agent",
]


# --------------------------------------------------------------------------- #
# 回答类：检索取证（as_tool）
# --------------------------------------------------------------------------- #
researcher_agent = Agent[TaskContext](
    name="researcher",
    handoff_description="多步检索取证：把一个问题拆成若干次检索，汇总成带出处的结论。",
    instructions=(
        "你是检索取证专家。你的产出永远**给主 agent 看**，不是给用户的答复。\n"
        "\n"
        "# Routine\n"
        "1. 先把问题拆成 1~3 个检索式，逐条调用 kb_search。\n"
        "2. 只依据检索结果写结论；检索不到就直说检索不到。\n"
        "3. 每条结论都必须带出处编号。\n"
        "\n"
        "# 边界（不负责什么）\n"
        "- 不做文学化表达，不替用户做决定，不给最终答复。\n"
        "- 不臆造 kb_search 未返回的事实。"
    ),
    tools=[kb_search],
    output_type=ResearchDigest,
)


async def extract_research(result: RunResult | RunResultStreaming) -> str:
    """``as_tool`` 的默认输出提取器取"最后一条消息"；结构化产出必须自己序列化。

    同时在这里把结果登记进共享上下文 —— 出口护栏（引用可追溯）随后要读它。
    这段是**代码**，所以它做的登记模型改不掉。
    """
    digest = result.final_output_as(ResearchDigest, raise_if_incorrect_type=True)
    ctx = result.context_wrapper.context
    if isinstance(ctx, TaskContext):
        ctx.record("research", digest)
        ctx.notes.append(f"researcher 回传 {len(digest.findings)} 条结论")
    return digest.model_dump_json(indent=2)


# --------------------------------------------------------------------------- #
# 创作类 A：PPT（as_tool，产出结构化）
# --------------------------------------------------------------------------- #
slides_agent = Agent[TaskContext](
    name="slides_maker",
    handoff_description="把材料/需求整理成结构化 PPT 大纲（标题 + 要点 + 备注）。",
    instructions=(
        "你是 PPT 结构设计师。你把材料整理成一份**结构化大纲**，不写解说词。\n"
        "\n"
        "# Routine\n"
        "1. 判断这份 PPT 要讲给谁听、讲几页。\n"
        "2. 每页一个论点，要点不超过 6 条、每条不超过 20 字。\n"
        "3. 需要补资料时调用 kb_search，但**不要**把检索过程写进大纲。\n"
        "\n"
        "# 边界\n"
        "- 不渲染文件（渲染是确定性步骤，由调用方完成）。\n"
        "- 不直接对用户说话。"
    ),
    tools=[kb_search],
    output_type=SlideDeck,
)


async def extract_slide_deck(result: RunResult | RunResultStreaming) -> str:
    deck = result.final_output_as(SlideDeck, raise_if_incorrect_type=True)
    ctx = result.context_wrapper.context
    if isinstance(ctx, TaskContext):
        ctx.record("slides", deck)
        ctx.notes.append(f"slides_maker 回传 {len(deck.slides)} 页大纲")
    return deck.model_dump_json(indent=2)


# --------------------------------------------------------------------------- #
# 创作类 B：艺术/图像（as_tool，产出资产）
# --------------------------------------------------------------------------- #
visual_agent = Agent[TaskContext](
    name="visual_artist",
    handoff_description="按文字描述生成插图资产，返回图片路径与生成参数。",
    instructions=(
        "你是插图师。给定一段画面需求，产出一张图的生成参数与落地路径。\n"
        "\n"
        "# Routine\n"
        "1. 把需求改写成一句具体的画面描述（主体 / 构图 / 光线）。\n"
        "2. 调用 generate_image 生成资产。\n"
        "3. 回报生成参数与资产路径。\n"
        "\n"
        "# 边界\n"
        "- 不写正文，不做版式设计，不直接对用户说话。"
    ),
    tools=[generate_image],
    output_type=ImageBrief,
)


async def extract_illustration(result: RunResult | RunResultStreaming) -> str:
    brief = result.final_output_as(ImageBrief, raise_if_incorrect_type=True)
    ctx = result.context_wrapper.context
    if isinstance(ctx, TaskContext):
        ctx.record("illustration", brief)
        if brief.asset_path:
            ctx.assets.append(brief.asset_path)
    return brief.model_dump_json(indent=2)


# --------------------------------------------------------------------------- #
# 创作类 C：文学（handoff，产出直接给用户看）
# --------------------------------------------------------------------------- #
literary_agent = Agent[TaskContext](
    name="literary_writer",
    handoff_description="文学创作（小说 / 散文 / 诗歌）。接管对话，与用户来回打磨作品。",
    instructions=(
        "你是文学作者，已经接下这次对话。用户接下来直接跟你说话。\n"
        "\n"
        "# Routine\n"
        "1. 先确认题材、篇幅、基调；信息够了就直接写，不要反复追问。\n"
        "2. 一次给出完整作品，再根据用户意见修改。\n"
        "3. 用户想换题目以外的任务时，转回 frontline_agent。\n"
        "\n"
        "# 边界\n"
        "- 不写技术文档、不做资料检索（那是 researcher 的活）。\n"
        "- 不输出 Markdown 表格、不输出 JSON —— 你交付的是作品本身。"
    ),
    tools=[],
)


async def on_literary_handoff(context: RunContextWrapper[TaskContext]) -> None:
    """``on_handoff`` 回调：**只改上下文，不传结果**。

    对照官方 airline 里 ``on_seat_booking_handoff`` 只写一个 ``flight_number``。
    这里写的是"当前频道"，下游（例如渲染、日志、计费）据此知道该按哪种形态处理产出。
    """
    context.context.channel = "literary"
    context.context.notes.append("handoff → literary_writer，对话控制权已转移")


def literary_handoff():  # pragma: no cover - 纯粹的装配糖，便于 selftest 复用
    return handoff(
        agent=literary_agent,
        on_handoff=on_literary_handoff,
        tool_name_override="transfer_to_literary_writer",
    )
