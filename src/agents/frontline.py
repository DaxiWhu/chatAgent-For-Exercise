"""前台 agent —— 全链路里**唯一直接面向用户**的 agent。

职责是三层里最窄的：理解需求、按需调用专家、汇总产出。

装配上的三处关键差别（测试会逐条钉住）：

* ``tools=[...]`` 里放 **``as_tool()`` 出来的专家** —— 有界子任务，不接管对话；
* ``output_type=FrontlineDecision`` —— 每一步都要给出"答复还是转交"的判定，
  由 ``src/graph.py`` 的条件边消费（见下）；
* ``review_draft``（评审）**也在 ``tools`` 里** —— 要不要评审由**模型自己决定**。
  2026-10-02 按用户要求从"代码 ``while`` 每轮必跑"改成 ``as_tool``；口径变化见
  ``evaluator.py`` 的模块 docstring（尤其是"代码不能假设评审一定发生过"）。

⚠️ 2026-10-03：``handoffs`` 已删，改成 ``output_type``
----------------------------------------------------
"移交文学作者"这件事从 **SDK 的 handoff** 改成了 **图节点之间的一条边**：

    前台节点 --(读 FrontlineDecision.action)--> 文学节点

为什么必须换：agent 链路拆成多个图节点之后，**节点边界就是续跑边界**；而 SDK 的
handoff 是在同一个节点内部把人换完再继续跑（``run.py`` 里换人后是 ``continue``），
节点里一旦带 handoff，节点边界就形同不存在 —— 拆了等于没拆。

由此得到的硬约束：**本文件不能再出现 ``handoffs=``**。加回来就会让 SDK 在这一个
节点里把人都换完、一路跑到终点，而图只会看到"一个节点执行了一次" ——
症状是"文学节点永远不被走到"，且没有任何报错。守卫见
``tests/test_agents_pipeline.py`` 的接线断言。

⚠️ 入口护栏从 ``RunConfig`` 移到了这里（2026-10-03）
--------------------------------------------------
此前它挂在运行配置上，理由是本 agent "会 handoff 出去 ⇒ 不一定是链上第一个"，
于是"第一份输入必被扫"的不变式不能绑在任何一个 agent 身上。

拆成图节点之后这条理由**不成立了**：每轮的第一个 agent 节点**固定**是前台
（图里 ``init → dispatch → frontline`` 是写死的边），前台不再 handoff 出去。
不变式现在是**结构保证**的，挂在 agent 级才是它的正确位置。

⚠️ 同时必须从 ``RunConfig.input_guardrails`` 里**删掉**：官方那两个池子是**相加**
（``starting_agent.input_guardrails + run_config.input_guardrails``），两处都挂会让
每次运行跑两遍（指标翻倍 + 重复扫描）。也正因为改了位置，文学节点那次运行不会再
把同一句用户输入重扫一遍。

回程边（曾经的 ``literary_agent.handoffs.append(...)``）已随 handoff 一起去掉 ——
**不要加回来**：入口已固定为前台，下一轮用户说话自然回到前台，不需要文学专家在
本轮内自己判断"这不是我的活"（多一次模型调用，且失败方式是硬撑不转）。
"""

from __future__ import annotations

from agents import Agent

from .context import TaskContext
from .evaluator import evaluate_draft_tool
from .guards import prompt_injection_guardrail
from .hooks import AGENT_HOOKS
from .specialists import (
    extract_illustration,
    extract_research,
    extract_slide_deck,
    researcher_agent,
    slides_agent,
    visual_agent,
)
from .tools import save_text_asset
from .types import FrontlineDecision

__all__ = ["frontline_agent"]

_INSTRUCTIONS = (
    "你是前台，也是本次对话的**唯一入口**：用户只跟你说话，任何请求都先到你这里。\n"
    "\n"
    "# Routine\n"
    "1. 先判断这次要交付什么：一段说明 / 结构化大纲 / 插图 / 文学作品。\n"
    "   - 闲聊、打招呼、追问上下文：**直接回答**，不要调用任何工具。\n"
    "   - 问方法 / 原理 / 配置，或需要事实依据：先调用 research 取证，"
    "再基于取证结果回答。\n"
    "2. 需要多个交付物时逐个调用对应工具。\n"
    "3. 回答里引用取证结论时**保留出处编号**（形如 [1]），不要改写成自己的话，"
    "也不要编造没有出现过的编号。\n"
    "4. 交付**正式产出**（说明文 / 大纲 / 作品）之前，调用 review_draft 自查一次，"
    "按它给出的改进项改完再交付。闲聊与追问不必自查。\n"
    "\n"
    "# 输出（每一步都要给一个判定）\n"
    "你的产出是一个结构化对象，两个字段：\n"
    "- action：reply = 这一步由你答复用户；transfer = 把对话交给文学创作专家。\n"
    "- content：给用户看的正文。action=reply 时必须是**完整答复**；"
    "action=transfer 时写一句交接说明（这一句用户看不到）。\n"
    "\n"
    "# 边界（这几条是职责的另一半）\n"
    "- 不自己编事实：凡是要给的数字、配置项、接口路径，必须来自 research 的返回。\n"
    "  包括**本服务自身**的规则（限流怎么算、某个开关在哪）—— 一样去 research 查，"
    "不凭印象回答。\n"
    "- 文学创作（小说/散文/诗歌）**不要自己做**：把 action 定为 transfer，"
    "不要用 content 直接把作品写出来。\n"
    "- 不做校验、不做评审的具体规则：那是 review_draft 与出口护栏的事，不由你重写。\n"
    "- 工具返回「检索不到」时，如实说明检索不到，不要用常识补齐。"
)

frontline_agent = Agent[TaskContext](
    name="frontline_agent",
    instructions=_INSTRUCTIONS,
    tools=[
        # -- 有界子任务：产出给「你」看，不给用户看 ⇒ as_tool --
        #
        # ⚠️ ``hooks=AGENT_HOOKS`` 三处都不能省：``as_tool()`` 的嵌套 run 不会自动
        # 继承父 run 的 hooks（SDK 里这是两个独立形参）。漏掉的表现是"专家的模型
        # 调用次数与参与 agent 静默少算"，而模型调用次数正是成本与能力指标的分母 ——
        # 数字看起来完全正常，只是偏小，没有任何告警会响。
        researcher_agent.as_tool(
            "research",
            "多步检索取证。输入一个待查证的问题，返回带出处编号的结论列表。"
            "用于回答需要事实依据的问题；不要用它来做文学创作或版式设计。",
            hooks=AGENT_HOOKS,
            custom_output_extractor=extract_research,
        ),
        slides_agent.as_tool(
            "build_slide_deck",
            "把材料或需求整理成结构化 PPT 大纲（标题 / 要点 / 备注）。"
            "需要一份演示稿时用它；它不写字幕式正文。",
            hooks=AGENT_HOOKS,
            custom_output_extractor=extract_slide_deck,
        ),
        visual_agent.as_tool(
            "make_illustration",
            "生成一张插图并返回资产路径。需要配图时用它；一次只出一张。",
            hooks=AGENT_HOOKS,
            custom_output_extractor=extract_illustration,
        ),
        # -- 评审：要不要调由模型自己决定（见 evaluator.py 的口径说明）--
        evaluate_draft_tool,
        # -- 确定性步骤：普通函数工具，不是 agent --
        save_text_asset,
    ],
    # 每一步的判定。图读它的 action 决定下一条边（见模块 docstring）。
    output_type=FrontlineDecision,
    # 入口护栏挂在**这里**：本 agent 是每轮的第一个 agent 节点（结构保证），
    # 不是"碰巧第一个"。理由见模块 docstring 的"入口护栏"一节。
    input_guardrails=[prompt_injection_guardrail],
)
