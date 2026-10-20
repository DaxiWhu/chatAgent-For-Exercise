"""前台 agent —— 全链路里**唯一直接面向用户**的 agent。

职责是三层里最窄的：理解需求、按需调用专家、汇总产出。

装配上的三处关键差别（测试会逐条钉住）：

* ``tools=[...]`` 里放 **``as_tool()`` 出来的专家** —— 有界子任务，不接管对话；
* ``handoffs=[literary_agent]`` —— 文学创作这类**整段对话都该换人**的请求，
  用官方 handoff 把控制权交出去（见下）；
* ``review_draft``（评审）**也在 ``tools`` 里** —— 要不要评审由**模型自己决定**。
  2026-10-02 按用户要求从"代码 ``while`` 每轮必跑"改成 ``as_tool``；口径变化见
  ``evaluator.py`` 的模块 docstring（尤其是"代码不能假设评审一定发生过"）。
  ⚠️ 2026-10-19 再补一刀：评审结论**也不参与收尾判定**了（不打分、不转人工，
  结论只回给模型自己）。

⚠️ 2026-10-20：``handoffs`` 回来了，``output_type=FrontlineDecision`` 删了
-----------------------------------------------------------------------
这是对 2026-10-03 那次改动的**回退**，原因不是当时判断错了，而是**前提没了**：

当时（2026-10-03）agent 链路被拆成多个 LangGraph 节点，"移交文学作者"只能做成
图的一条边 —— 因为 SDK 的 handoff 是"在同一个节点内部换人后 ``continue``"，
节点里带 handoff 会让节点边界消失。于是它被换成了
``output_type=FrontlineDecision``：模型每步产出一个 ``{action, content}``，
由 ``route_frontline`` 读 ``action`` 决定跳哪条边。

现在图被去掉了（用户要求整条链路按 OpenAI SDK 来），"节点边界"这个前提不存在了，
那条理由自然失效。于是回到官方形态：

* **移交** = ``handoffs=[literary_agent]``，SDK 换掉当前 agent 后继续跑，
  ``result.last_agent`` 就是"这次运行结束在谁手上"这个唯一事实来源；
* **答复** = 普通文本输出（模型不再需要额外产出一个 JSON，交付内容就是最终答复）；
* **不再有 ``agent_decision``** —— 那是给图的代码看的字段，图没了就没有读取方。

由此得到的硬约束换成反过来的那条：**不要再给本 agent 加 ``output_type``**。
一旦加了，``result.final_output`` 就变成结构化对象，而它会被当成"给用户的答复"
一路交付出去（pydantic 的 repr：字段名 + 括号）—— 用户在聊天框里看到的是
``action='reply' content='...'``。守卫见 ``tests/test_agents_pipeline.py``。

⚠️ 入口护栏挂在 agent 级（不是 ``RunConfig``）
--------------------------------------------
官方那两个池子是**相加**（``starting_agent.input_guardrails + run_config.input_guardrails``），
两处都挂会让每次运行跑两遍（指标翻倍 + 重复扫描）。挂在 agent 上还顺带得到一个
正确语义：handoff 之后 SDK **不会**再跑输入护栏（它只扫起始 agent 的输入），
所以同一句用户输入在文学专家那一程不会被重扫一遍 —— 这正是想要的。

⚠️ 没有回程边：**不要加** ``literary_agent.handoffs`` 指回前台。
下一轮用户说话自然回到前台（每次 ``Runner.run`` 的起始 agent 都是前台）。
加回程边会让文学专家在本轮内自己判断"这不是我的活"（多一次模型调用，
失败方式是硬撑不转），而且官方 handoff 文档明确指出应避免循环转交。
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
    literary_agent,
    researcher_agent,
    slides_agent,
    visual_agent,
)
from .steps import tool_error_handler
from .tools import deliver, save_text_asset

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
    "4. 要不要自查由**你**判断：交付正式产出（说明文 / 大纲 / 作品）时，"
    "想确认质量就调用 review_draft；闲聊与追问不必。"
    "⚠️ 一旦调用，就按它给出的改进项改完再交付 —— 它指出问题而你决定不改时，"
    "要么说明理由，要么先改。它**不是**一道必须通过的关卡，只是一份给你自己的第二意见。\n"
    "5. 交付：正式产出（说明文 / 大纲 / 作品，或任何你要作为最终答复的正文）"
    "调用 deliver，把给用户看的正文放进 content。\n"
    "   - 题材敏感、结论影响面大、或你自己拿不准质量时，把 needs_review 设为 true —— "
    "整个运行会**停在这里**等人工过目；人打回时你会收到他写的修改意见，改完再交一次。\n"
    "   - 普通产出保持 needs_review: false。**不要**为了保险一律设为 true："
    "每一次 true 都要有人真的去看，否则这条对话就一直停着，用户等不到答复。\n"
    "\n"
    "# 输出（不要包 JSON）\n"
    "调了 deliver：以它交出去的正文为准，之后的输出只写一句简短说明（例如「已交付」），"
    "**不要**把正文再重复一遍。\n"
    "没调 deliver 的闲聊与追问：把给用户看的正文本身作为答复输出。\n"
    "动手前心里过一遍上面这几条（该取证取证、该转交转交），但**不要**把这份清单"
    "或你的判断过程写进答复里，也不要写成 ``action=... content=...`` 这种字段形式。\n"
    "\n"
    "# 边界（这几条是职责的另一半）\n"
    "- 不自己编事实：凡是要给的数字、配置项、接口路径，必须来自 research 的返回。\n"
    "  包括**本服务自身**的规则（限流怎么算、某个开关在哪）—— 一样去 research 查，"
    "不凭印象回答。\n"
    "- 文学创作（小说/散文/诗歌）**不要自己做**：交给 literary_writer。"
    "你只负责把这次要写什么讲清楚（题材、篇幅、基调、已有约束）。\n"
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
        # ⚠️ ``failure_error_function`` 四处（这里三个 + evaluator 那个）都要给：
        # 专家整体失败（例如结构化产出解析不出来）时，成败同样要能看见 —— 只覆盖
        # 专家**内部**的工具不够，那一层失败会静默消失在"专家被调了几次"里。
        researcher_agent.as_tool(
            "research",
            "多步检索取证。输入一个待查证的问题，返回带出处编号的结论列表。"
            "用于回答需要事实依据的问题；不要用它来做文学创作或版式设计。",
            hooks=AGENT_HOOKS,
            custom_output_extractor=extract_research,
            failure_error_function=tool_error_handler("research"),
        ),
        slides_agent.as_tool(
            "build_slide_deck",
            "把材料或需求整理成结构化 PPT 大纲（标题 / 要点 / 备注）。"
            "需要一份演示稿时用它；它不写字幕式正文。",
            hooks=AGENT_HOOKS,
            custom_output_extractor=extract_slide_deck,
            failure_error_function=tool_error_handler("build_slide_deck"),
        ),
        visual_agent.as_tool(
            "make_illustration",
            "生成一张插图并返回资产路径。需要配图时用它；一次只出一张。",
            hooks=AGENT_HOOKS,
            custom_output_extractor=extract_illustration,
            failure_error_function=tool_error_handler("make_illustration"),
        ),
        # -- 评审：要不要调由模型自己决定（见 evaluator.py 的口径说明）--
        evaluate_draft_tool,
        # -- 确定性步骤：普通函数工具，不是 agent --
        save_text_asset,
        # -- 交付：把正文交出去（顺带承载"要不要人先过目"这个声明）--
        # ⚠️ 它是"按内容决定要不要人审"的**唯一入口**（图只剩一个打标记的
        # ``request_review`` 节点，跑偏/护栏/低分那几档才走它）：
        # 只有这次调用带上 ``needs_review=True`` 时运行才会停下来等人，
        # 判定与代价见 ``approval.delivery_needs_approval``。
        deliver,
    ],
    # 整段对话都该换人的请求（文学创作）⇒ 官方 handoff，控制权交给对方。
    # ⚠️ 刻意**不写** ``output_type``：加了它 ``result.final_output`` 就变成结构化
    # 对象，而它会被当成给用户的答复一路交付出去（见模块 docstring 的硬约束）。
    handoffs=[literary_agent],
    # 入口护栏挂在**这里**：本 agent 是每轮的第一个 agent 节点（结构保证），
    # 不是"碰巧第一个"。理由见模块 docstring 的"入口护栏"一节。
    input_guardrails=[prompt_injection_guardrail],
)
