"""前台 agent —— 全链路里**唯一直接面向用户**的 agent。

它的职责是三层里最窄的：理解需求、按需调用专家、汇总产出。
注意它自己**不负责**检索、不负责写作、不负责校验 —— 那些分别属于
``as_tool`` 专家、``handoff`` 专家、以及代码里的评估循环和护栏。

装配上的三处关键差别（selftest 会逐条钉住）：

* ``tools=[...]`` 里放 **``as_tool()`` 出来的专家** —— 有界子任务，不接管对话；
* ``handoffs=[...]`` 里放 **会接管对话的专家**（文学创作）；
* **评估 agent 两个地方都不出现** —— 它由 ``runner.py`` 的代码循环调用，
  模型无法跳过。官方 ``llm_as_a_judge.py`` 正是这么安排的。
"""

from __future__ import annotations

from agents import Agent, handoff

from .context import TaskContext
from .guards import prompt_injection_guardrail
from .specialists import (
    extract_illustration,
    extract_research,
    extract_slide_deck,
    literary_agent,
    literary_handoff,
    researcher_agent,
    slides_agent,
    visual_agent,
)
from .tools import save_text_asset

__all__ = ["frontline_agent"]

_INSTRUCTIONS = (
    "你是前台。用户只跟你说话，你负责把需求变成交付物。\n"
    "\n"
    "# Routine\n"
    "1. 先判断这次要交付什么：资料性回答 / 结构化大纲 / 插图 / 文学作品。\n"
    "2. 需要事实依据时先调用 research 取证，再基于取证结果回答。\n"
    "3. 需要多个交付物时逐个调用对应工具，最后**合并**成一份答复。\n"
    "4. 回答里引用取证结论时保留出处编号（形如 [1]），不要改写成自己的话。\n"
    "\n"
    "# 边界（这几条是职责的另一半）\n"
    "- 不自己编事实：凡是要给的数字、配置项、接口路径，必须来自 research 的返回。\n"
    "- 文学创作（小说/散文/诗歌）**不要自己做**，移交给 transfer_to_literary_writer。\n"
    "- 不做校验、不做评审：那是流程固定动作，不由你决定做不做。\n"
    "- 工具返回「检索不到」时，如实说明检索不到，不要用常识补齐。"
)

frontline_agent = Agent[TaskContext](
    name="frontline_agent",
    handoff_description="前台：理解需求、调用专家、汇总多来源产出。唯一面向用户的 agent。",
    instructions=_INSTRUCTIONS,
    tools=[
        # -- 有界子任务：产出给「你」看，不给用户看 ⇒ as_tool --
        researcher_agent.as_tool(
            "research",
            "多步检索取证。输入一个待查证的问题，返回带出处编号的结论列表。"
            "用于回答需要事实依据的问题；不要用它来做文学创作或版式设计。",
            custom_output_extractor=extract_research,
        ),
        slides_agent.as_tool(
            "build_slide_deck",
            "把材料或需求整理成结构化 PPT 大纲（标题 / 要点 / 备注）。"
            "需要一份演示稿时用它；它不写字幕式正文。",
            custom_output_extractor=extract_slide_deck,
        ),
        visual_agent.as_tool(
            "make_illustration",
            "生成一张插图并返回资产路径。需要配图时用它；一次只出一张。",
            custom_output_extractor=extract_illustration,
        ),
        # -- 确定性步骤：普通函数工具，不是 agent --
        save_text_asset,
    ],
    handoffs=[
        # -- 会接管对话、且产出直接给用户看 ⇒ handoff --
        literary_handoff(),
    ],
    input_guardrails=[prompt_injection_guardrail],
)

# --------------------------------------------------------------------------- #
# 回程边：handoff 天然是**单向**的，专家要能交回控制权就必须显式注册反向 handoff。
# 官方 airline 示例就是这么做的 —— 先定义两端，再
# ``faq_agent.handoffs.append(handoff(agent=triage_agent, tool_name_override=...))``。
#
# ⚠️ 这一步不能省。文学专家的 instructions 里写着「用户想换题目以外的任务时，转回
# frontline_agent」—— 若不注册这条回程边，那句话就是一句**做不到的承诺**
# （模型要么尝试调用一个不存在的工具，要么硬撑着不转）。
# 这类"提示词里承诺了、拓扑上没接"的错配，只有断言抓得到（见 selftest B8）。
# --------------------------------------------------------------------------- #
literary_agent.handoffs.append(
    handoff(agent=frontline_agent, tool_name_override="transfer_to_frontline_agent")
)
