"""专家 agent —— 注意它们**没有**共同的接线方式。

```
专家              产出最终给用户看？      接线                谁按选择键
-----------------------------------------------------------------------------
literary_writer   是（作品就是交付物）    图节点之间的边      模型给判定、图跳节点
slides_maker      否（结构化大纲）        as_tool             模型
visual_artist     否（资产路径）          as_tool             模型
researcher        否（取证材料）          as_tool             模型
evaluator         否（给创作方看的意见）  as_tool             模型
-----------------------------------------------------------------------------
```

判据是官方 ``multi_agent`` 页那一句：专家"helps with a bounded subtask but
**should not take over the user-facing conversation**"就用 ``as_tool``；
"routing itself is part of the workflow"、专家要**接管对话**就**另起一个图节点**。

⚠️ ``literary_writer`` 这一行是 2026-10-03 改的：它原来是 ``handoff``，
现改成**图里的一个独立节点**（由前台的结构化判定路由过去）。原因是 agent 链路
拆成了多个节点之后，节点边界就是续跑边界，而 SDK 的 handoff 会在同一个节点内部
把人换完（``run.py`` 里换人后是 ``continue``）—— 两者不能共存。取舍见
``DESIGN_agent_architecture.md``，装配见 ``frontline.py`` 的模块 docstring。

⚠️ ``evaluator`` 这一行是 2026-10-02 改的：它原来排在**代码 ``while``** 里
（照抄官方 ``llm_as_a_judge.py``，"跑不跑评审"由代码定）。用户要求改为
**由模型决定要不要评审**，于是它变成挂在产出方身上的 ``as_tool``。
随之而来的口径变化见 ``evaluator.py`` 的模块 docstring。

三个 ``as_tool`` 专家的共同点：instructions 都是短句级、产出都结构化、用户永远
看不到它们说了什么。这与官方 ``agents_as_tools.py`` 里那三个"instructions 一行、
零工具"的翻译 agent 是同一件事 —— **子 agent 的成立条件不是"有工具"，而是
"一次独立的模型调用能做完一件事"**。

⚠️ ``as_tool(..., hooks=AGENT_HOOKS)`` 不能省：嵌套 run **不会**自动继承父 run 的
hooks（SDK 源码里这是两个独立形参）。漏掉的表现是"专家的模型调用次数静默少算"，
而模型调用次数正是成本与能力指标的分母。

⚠️ 与上面同类的一个坑：``handoff_description`` 也有"看着有用、其实没有读取方"的版本。
官方读它的地方**只有一处** —— ``Handoff.default_tool_description``
（``handoffs/__init__.py``：``f"Handoff to the {agent.name} agent... {agent.handoff_description or ''}"``）；
``as_tool`` 用的是 ``tool_description``，**不回退**到这个字段
（``agent.py``：``tool_description_resolved = tool_description or ""``）。
⇒ 2026-10-03 起本项目**一处 handoff 都不剩**，所以这个字段**全部删净**：
留着它就是"写了没人读"的死配置。三个 ``as_tool`` 专家的职责说明写在
``frontline.py`` 的 ``as_tool(..., description=...)`` 里，那才是模型真正看得到的那句；
``literary_agent`` 的职责说明写在**结构化判定**的字段描述里
（``types.FrontlineDecision``），那也是模型真正看得到的那句。
"""

from __future__ import annotations

from agents import Agent, RunResult, RunResultStreaming

from .context import TaskContext
from .evaluator import evaluate_draft_tool
from .tools import generate_image, kb_search
from .types import ImageBrief, ResearchDigest, SlideDeck

__all__ = [
    "extract_illustration",
    "extract_research",
    "extract_slide_deck",
    "literary_agent",
    "researcher_agent",
    "slides_agent",
    "visual_agent",
]


# --------------------------------------------------------------------------- #
# 回答类：检索取证（as_tool）
# --------------------------------------------------------------------------- #
researcher_agent = Agent[TaskContext](
    name="researcher",
    instructions=(
        "你是检索取证专家。你的产出永远**给主 agent 看**，不是给用户的答复。\n"
        "\n"
        "# Routine\n"
        "1. 先把问题拆成 1~3 个检索式，逐条调用 kb_search。\n"
        "2. 只依据检索结果写结论；检索不到就直说检索不到，不要用常识补齐。\n"
        "3. 每条结论都必须带出处编号（形如 [1]），编号必须来自 kb_search 的返回。\n"
        "\n"
        "# 边界（不负责什么）\n"
        "- 不做文学化表达，不替用户做决定，不给最终答复。\n"
        "- 不臆造 kb_search 未返回的事实、数字或路径。"
    ),
    tools=[kb_search],
    output_type=ResearchDigest,
)


async def extract_research(result: RunResult | RunResultStreaming) -> str:
    """``as_tool`` 的输出提取器。

    默认提取器只取"最后一条消息"，结构化产出必须自己序列化。这里顺带把结果登记进
    共享上下文 —— 出口护栏（引用可追溯）随后要读它。

    这段是**代码**，所以它做的登记模型改不掉。
    """
    digest = result.final_output_as(ResearchDigest, raise_if_incorrect_type=True)
    ctx = result.context_wrapper.context
    if isinstance(ctx, TaskContext):
        ctx.record("research", digest)
        ctx.notes.append(f"researcher 回传 {len(digest.findings)} 条结论")
    return digest.model_dump_json(indent=2)


# --------------------------------------------------------------------------- #
# 帮手 A：PPT 大纲（as_tool，产出结构化）
# --------------------------------------------------------------------------- #
slides_agent = Agent[TaskContext](
    name="slides_maker",
    instructions=(
        "你是 PPT 结构设计师。你把材料整理成一份**结构化大纲**，不写解说词。\n"
        "\n"
        "# Routine\n"
        "1. 先判断这份 PPT 讲给谁听、讲几页。\n"
        "2. 每页一个论点；要点不超过 6 条、每条不超过 20 字。\n"
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
# 帮手 B：插图（as_tool，产出资产）
# --------------------------------------------------------------------------- #
visual_agent = Agent[TaskContext](
    name="visual_artist",
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
            ctx.stats.assets += 1
    return brief.model_dump_json(indent=2)


# --------------------------------------------------------------------------- #
# 帮手 C：文学（图里的一个独立节点，由前台的结构化判定路由过去）
# --------------------------------------------------------------------------- #
literary_agent = Agent[TaskContext](
    name="literary_writer",
    instructions=(
        "你是文学作者，已经接下这次对话。用户接下来直接跟你说话。\n"
        "\n"
        "# Routine\n"
        "1. 先确认题材、篇幅、基调；信息够了就直接写，不要反复追问。\n"
        "2. 一次给出完整作品，再根据用户意见修改。\n"
        "3. 交给用户之前调用 review_draft 自查一次，按它给出的改进项改完再交付。\n"
        "\n"
        "# 边界\n"
        "- 你只做文学创作。用户要的是别的（查资料 / 做 PPT / 插图）时如实说明，"
        "不要硬做 —— 下一轮由前台接手。\n"
        "- 不写技术文档、不做资料检索（那是 researcher 的活）。\n"
        "- 不输出 Markdown 表格、不输出 JSON —— 你交付的是作品本身。"
    ),
    # 作品是交付物 ⇒ 它同样要能自查（评审挂"产出交付物的一方"）。
    tools=[evaluate_draft_tool],
    # 刻意**不写** handoff_description：2026-10-03 起本项目一处 handoff 都不剩，
    # 而这个字段唯一的读取方就是 Handoff.default_tool_description（见模块 docstring）。
    # 模型看到的职责说明来自 types.FrontlineDecision 的字段描述（前台据此决定转不转）。
)
