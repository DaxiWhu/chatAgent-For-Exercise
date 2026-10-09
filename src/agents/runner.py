"""代码编排（code-first orchestration）—— 官方 ``llm_as_a_judge.py`` 的形态。

官方把编排分成并列的两种，没有优劣：

* **模型决策式**：agent = instructions + tools + handoffs，模型自己决定下一步；
* **代码式**：在代码里做分类、串联、``while`` + 评审、``asyncio.gather`` 并行。

本文件是第二种。三件**本项目自己拿主意**的事（官方两个多轮示例都不是这么写的，
取舍理由写在下面 —— 不是"看起来更合理"，而是有明确代价对比）：

一、**一次运行只跑一个 agent**（2026-10-03 定，此前是"一次跑完整条链"）
---------------------------------------------------------------------
agent 链路拆成了**图里的多个节点**（见 ``src/graph.py``）：前台一个节点、
文学专家一个节点，"该转给谁"由前台的结构化判定（``types.FrontlineDecision``）
经条件边决定。所以本文件不再拥有整条链，只负责**把指定的那一个 agent 跑完一次**。

为什么不能让 SDK 自己换人（``handoffs=``）必须在节点里消失：SDK 的 handoff 是
"同一个节点内部把人换完再继续跑"（``run.py`` 里换人后是 ``continue`` 而不是
``return``）—— 节点里一旦带 handoff，节点边界就不存在了，而**节点边界正是续跑边界**。
拆了等于没拆。取舍与实测见 ``DESIGN_agent_architecture.md``。

二、**入口固定为前台**（2026-10-02 定）
--------------------------------------
官方两个多轮示例都把"谁接下一轮"交给了**上一轮的结束点**：

    current_agent: Agent[...] = triage_agent     # 第一轮写死
    ...
        current_agent = result.last_agent        # 之后每一轮：接着上轮那个人

（``examples/customer_service/main.py``；``routing.py`` 里写作 ``result.current_agent``，
同一个东西。）本项目**只取前半句**：每轮入口**固定**为 ``frontline_agent``，
不读"上一轮结束在谁那里"。理由是"这轮还归不归我管"该由谁判断：

* 跟着上轮走时，一旦这轮换了话题，**专家要先自己判断"这不是我的活"再转回前台** ——
  那是一次"专才勉强判断不属于自己领域的事"的模型调用，而且失败方式是**硬撑不转**
  （用户会看到一个文学专家在解释限流怎么算）。省下的那一次，只在"用户确实接着聊"
  时才真的省到。
* 固定回前台，判断权就始终在**唯一有全局视野**的那个 agent 手上：它看得到需求、
  手上有全部专家的名片。代价只是"接着聊"时多一次模型调用。

⚠️ 因此 ``entry_agent_for`` / ``_ENTRY_AGENTS`` / ``DEFAULT_ENTRY_AGENT`` 已删，
**别加回来** —— 那等于静默恢复"跟进上一轮结束点"。state 里的 ``current_agent`` 仍在
写，但它已降级为**纯观测字段**（回答"这轮谁收的尾"），**不参与路由**。

三、**跨轮历史交给官方 ``Session``**（2026-10-02 定）
---------------------------------------------------
为什么不是自己攒 ``to_input_list()``、为什么后端这么选，见 ``session.py`` 的模块
docstring。本文件负责"把它接上"：``run_agent(session_id=...)`` 取会话交给 ``Runner.run``，
本轮输入**只放增量**（后续节点尤其要注意，见 ``run_agent`` 里的说明）。

⚠️ ``max_turns`` 的单位是 LLM 调用次数（官方语义），不是对话轮次。
设成对话轮次的量级（比如 3）会让正常的"查两次资料"直接抛 ``MaxTurnsExceeded``。

⚠️ 2026-10-03 起它约束的是**单个节点这一次运行**，不再是整条链 ——
一次请求的总模型调用预算因此变成"各节点各自的上限之和"（转交那一轮 = 前台上限 +
文学上限）。这是拆分带来的口径变化，不是疏漏。

⚠️ 2026-10-02 改动：评审**不再是**这里的代码循环
------------------------------------------------
本文件此前照抄官方 ``llm_as_a_judge.py`` 的 ``while``：每轮跑一次评审、
``score == "pass"`` 才 break、否则把 ``Feedback`` 伪装成 user 消息回灌再来一轮。
用户要求改为**由模型决定要不要评审**，于是评审变成挂在产出方身上的 ``as_tool``
（见 ``evaluator.py``）。现在这里只跑**一次** ``Runner.run``。

由此多出一档 ``exit_reason="unreviewed"``（模型一次都没调评审）。**不要**把它当成
``passed``：代码不能替模型宣布"这份产出被审过了"，指标上也要能看出跳过的比例。
2026-10-03 又多了 ``transfer`` 一档（这一步把对话交给了下一个 agent，没有交付物）。

另外，**渲染 PPT / 落盘资产是确定性步骤，放在这里（代码）而不是做成工具**：
没有任何模型需要"决定要不要渲染"。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any

from agents import (
    Agent,
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    OutputGuardrailTripwireTriggered,
    RunConfig,
    Runner,
)

from .. import observability
from ..envutil import env_int
from . import runtime
from .context import TaskContext
from .frontline import frontline_agent
from .guards import (
    citation_traceability_guardrail,
    length_guardrail,
)
from .history_budget import trim_model_input
from .hooks import AGENT_HOOKS
from .session import session_for
from .specialists import literary_agent
from .tools import render_slide_deck
from .types import FrontlineDecision

__all__ = [
    "ENTRY_AGENT",
    "LITERARY_AGENT",
    "MAX_TURNS",
    "TaskOutcome",
    "build_run_config",
    "run_agent",
]

MAX_TURNS = env_int("AGENT_MAX_TURNS", 12, lo=2)
"""单次 ``Runner.run`` 内允许的 **LLM 调用次数**（官方语义）。

⚠️ 2026-10-03 起它约束的是**单个 agent 节点的一次运行**，不是整条链（见模块 docstring）。
"""


@dataclass
class TaskOutcome:
    """一次 agent 运行的结果。"""

    answer: str
    attempts: int
    """本轮的 ``Runner.run`` 次数。

    评审改成 ``as_tool`` 后不再有重试循环，所以恒为 1；保留字段是为了
    ``graph`` 能照旧写 state 的 ``iteration``。
    """

    final_agent: str
    """本次运行结束时停在哪个 agent，值 = 官方 ``result.last_agent.name``。

    ⚠️ **纯观测**（2026-10-02 起）：入口已固定为前台，"上一轮结束在谁"不再参与
    路由。它现在的用途是回答"**这轮谁收的尾**"（日志与排查），不是决定下一轮谁接。

    **空串有明确含义**：run 没跑到底（异常 / 护栏触发 / 超轮数）⇒ 本轮没有可用的
    "结束 agent"。官方在同款情形下 ``current_agent = result.last_agent`` 那一行根本
    不会执行，所以这里也不该编一个值出来 —— 调用方保持 state 原值不变。
    """

    passed: bool
    exit_reason: str
    assets: list[str] = field(default_factory=list)
    delivered: dict[str, Any] = field(default_factory=dict)
    guardrail: str | None = None
    """被哪条护栏拦下（``exit_reason`` 为 ``*_guardrail`` 时有值）。"""

    error: str = ""
    """异常收尾时的 ``类型: 消息``（``exit_reason`` 为 ``error`` 时有值）。

    为什么值得单列一个字段：``exit_reason="error"`` 把**上游限流（429）**、
    **网络超时**、**协议不兼容**、**代码 bug** 压成了同一桶。而运维要做的判断
    完全不同 —— 限流该退避重试，bug 该改代码。这个字段让调用方（探测脚本、
    接口层日志）不必从 traceback 里猜。

    ⚠️ 它**绝不能进指标标签**（消息里带 request id / 上游原文，基数无界且可能
    含用户数据）。它的去处是**日志**与调用方的一次性判断。
    """

    decision: dict[str, Any] = field(default_factory=dict)
    """结构化判定（``{"action": ..., "content": ...}``）。

    只有跑**带 ``output_type`` 的 agent**（当前就是前台）时才有值，其余是空 dict。
    调用方（``src/graph.py`` 的前台节点）读它的 ``action`` 决定下一条边 ——
    这就是"跨节点转移的信号来源"（handoff 已经不在节点里了，见模块 docstring）。

    ⚠️ 空 dict 与 ``{"action": "reply"}`` 是**两回事**：前者是"这次运行没给出判定"
    （跑文学节点 / 没跑到底），后者是"给出了判定：直接答复"。
    """

    def score_hint(self) -> int:
        """把"是否可交付"映射成主链路沿用的 0-100 分。

        为什么要有这个映射：``src/graph.py`` 的 ``route_review`` 是按分数做决策的
        （>=HIGH 直通 / 中间重生成 / 低分转人工）。把 agent 链路的结论接到同一套
        阈值上，两条链路对"要不要转人工"的口径才一致 —— 否则同一个服务里
        两个标准，运维无法解释。
        """
        if self.passed:
            return 90
        if self.exit_reason == "transfer":
            # 转交这一步本身是**成功**的（模型按约定给出判定，图据此跳文学节点），
            # 只是它没有产出可交付物（所以 `passed` 为假）。给 90 是安全的：
            # 目前没有任何调用方会在转交分支上读分数（`route_frontline` 直接跳节点），
            # 而万一有人读了，把一次成功的步进报成"需要人工介入"会制造假告警。
            return 90
        if self.exit_reason in ("input_guardrail", "output_guardrail"):
            return 30          # 确定性拦截：确凿的坏产出，交人
        # review_failed / max_turns / error：链路没跑完或没过审 → 交人
        return 40


def _guardrail_name(exc: BaseException) -> str | None:
    """从 tripwire 异常里取护栏名。官方把结果挂在 ``exc.guardrail_result`` 上。"""
    result = getattr(exc, "guardrail_result", None)
    guardrail = getattr(result, "guardrail", None)
    if guardrail is None:
        return None
    getter = getattr(guardrail, "get_name", None)
    return getter() if callable(getter) else getattr(guardrail, "name", None)


def build_run_config(model: Any | None = None) -> RunConfig:
    """一处装配、全链路生效。

    ``RunConfig.model`` 的优先级**高于** ``Agent.model``（官方三层优先级里最高的
    一层），而且 ``as_tool()`` 的嵌套 run 在没有显式传 ``run_config`` 时会**继承
    父 run 的**（SDK 源码：``resolved_run_config = run_config or context.run_config``）。
    ⇒ 这里挂一次，前台、专家、嵌套 run 全部用同一个模型。

    出口护栏挂在这里：它只对"产出**最终输出**的那个 agent"生效，而拆成图节点之后
    "谁是最后一个 agent"取决于走到哪个节点（前台答复 → 前台；转交 → 文学专家）——
    挂在 RunConfig 才与"谁是最后一个"完全无关。每一次运行的最终输出都会被它检查，
    而"这一步没有交付物"的情形（转交）由护栏自己跳过，见 ``guards._deliverable``。

    ⚠️ 入口护栏**不在这里**（2026-10-03 从本函数移走）。它现在挂在
    ``frontline_agent.input_guardrails``：每轮的第一个 agent 节点固定是前台，
    "第一份输入必被扫"从此是**结构保证**的，不需要再靠"挂得离拓扑远一点"来兜。
    两处都挂会让每次运行跑两遍 —— 官方那两个池子是**相加**：
    ``starting_agent.input_guardrails + (run_config.input_guardrails or [])``。

    跨轮历史那个开关也在这里挂（与 ``session_for`` 里的后端选择配套）：

    * ``call_model_input_filter``：**按 token 裁"送进模型的那一份"**（2026-10-03 起）。
      官方 docstring 写明这个口子的用途就是 "to stay within a token limit"，实现见
      ``src/agents/history_budget.py``（预算 ``AGENT_HISTORY_TOKEN_LIMIT``，默认 10 万）。
      ⚠️ 它**只动送进模型的那一份，库里一行都不删** —— 裁掉的内容只是不再进模型，
      存储那份仍然完整（所以"历史还在不在"与"模型看得见多少"是两个问题）。
      ⚠️ 它跑在 ``Runner.run`` **内部、每次模型调用之前**，所以形状写错的表现是
      "agent 动不动答不出来"，而不是某个测试红。
    * **刻意不配 ``session_settings.limit``**：条数与 token 是**互不知道对方**的两套判据，
      两处都设上限时条数那份会**先**把历史切掉，token 那层就永远看不到真实总量。
      要限制只留 token 一个口径（官方默认 ``None`` = 全取历史）。
    * ``reasoning_item_id_policy="omit"``：历史里可能带着服务端分配的 ``rs_...``
      编号，**回灌会 404**（官方 ``prepare_input_with_session`` 的注释）。SDK 在读、
      写两侧都要应用这个策略，所以它必须是"运行配置"级、不是某一处的局部处理。
      漏配的表现是"某一天开始，多轮对话突然报错"，而单轮测试全绿。
    """
    return RunConfig(
        model=model,
        workflow_name="chatagent_sub_agents",
        output_guardrails=[citation_traceability_guardrail, length_guardrail],
        call_model_input_filter=trim_model_input,
        reasoning_item_id_policy="omit",
        # 默认会把对话内容带上追踪。本服务的内容是用户数据，
        # 与 main.py 里"日志不落原文"的口径一致：关掉。
        trace_include_sensitive_data=False,
    )


#: 每轮**第一个**跑的 agent（前台）—— 固定，不读任何跨轮状态。
#:
#: 官方示例把它当**初值**（``current_agent = triage_agent``），之后改成
#: ``result.last_agent``；本项目刻意只保留初值这一半，理由见模块 docstring 的
#: "入口固定为前台"。之所以做成模块级常量而不是写在 ``run_agent`` 里：测试与文档要能
#: 引用"入口是哪个 agent"，而写在函数体里就只能靠读源码。
ENTRY_AGENT = frontline_agent

#: 由前台的结构化判定路由过去的那个 agent（文学创作）。
#:
#: ⚠️ 它**不是**"第二个前台"：只有前台能答复用户，本 agent 跑完即收尾
#: （图里 ``literary → route_agent_review``），没有"再转回前台"这条边 ——
#: 入口已经固定为前台，下一轮用户说话自然回到前台。
LITERARY_AGENT = literary_agent


def _flush_deterministic_steps(ctx: TaskContext) -> None:
    """把所有"确定性步骤"在这里一次性做完：渲染大纲、收集资产。

    幂等（``rendered`` 标记存在 ``ctx.delivered`` 之外的一个集合里）是因为它可能被
    调多次（拆分之后每个 agent 节点跑完都会调一次），而渲染没有"多渲染几次更好"
    这回事 —— 只会产生重复资产与重复计数。
    """
    deck = ctx.recall("slides")
    if deck is not None and "slides" not in ctx.rendered:
        path = render_slide_deck(deck.model_dump_json(), ctx.asset_dir)
        ctx.assets.append(path)
        ctx.stats.assets += 1
        ctx.rendered.add("slides")


def _verdict_of(context: TaskContext) -> str:
    """按"**这次运行**里模型到底评审过没有"定 ``exit_reason``。

    评审是模型**可选择**的工具（``as_tool``），所以三种情形必须分开：

    * 一次都没调 ⇒ ``unreviewed``。**不等于通过** —— 代码不能替模型宣布"这份被
      审过了"；指标上要能看出跳过的比例（这是选择"模型自主"必须付的透明度成本）。
    * 评审且 ``pass`` ⇒ ``passed``
    * 评审但没 ``pass`` ⇒ ``review_failed`` ⇒ 交付结论交人复核。

    ⚠️ 判据是"**本次运行**"而不是"本轮的累积"：``run_agent`` 在每次运行开始时
    清空 ``context.delivered``（理由见那里），所以这里的 ``recall`` 天然只看得见
    这次运行登记的评审结论。不清的话，前台那次运行的评审结论会**冒充**文学那次
    运行的评审结论（``passed`` 会凭空出现在一次根本没评审的运行上）。
    """
    verdict = context.recall("evaluation")
    if verdict is None:
        return "unreviewed"
    return "passed" if getattr(verdict, "score", "") == "pass" else "review_failed"


def _report_run_metrics(stats, exit_reason: str, *, agent: str = "") -> None:
    """把**一次 agent 运行**的统计播报成指标。

    为什么单独抽一个函数：`AgentStats` 是普通 dataclass，构造它不需要起 SDK、
    不需要模型 —— 于是"**哪些字段真的被播报出去了**"可以被一条廉价单测钉住。
    这正是此前最容易出错的地方：`tool_calls` 在 `AgentStats` 里累计了三层
    （字段、`hooks` 里的计数、`as_dict` 的输出），却唯独没有交出去，
    日志里一切正常、指标里永远空着。

    四个量必须一起看：跑成没跑成（exit_reason）、跑得多慢、花了多少次模型调用、
    多少次工具调用。"慢"是因为重试、因为单次调用慢、还是因为工具绕圈，
    只有这四者齐全才分得出来 —— 只看模型调用次数**分不出**"模型转得多"与
    "工具调得多"，而这两者的处置完全不同（一个调 prompt，一个查工具实现）。

    ⚠️ 传进来的 ``stats`` 必须是**这一次运行**的数字（调用方用
    ``dataclasses.replace`` 做了切片）。直接用累积的 ``context.stats`` 会让
    一次请求里第二次运行的直方图读数把第一次也算进去 —— 而直方图是分位数统计，
    混进另一个分布的样本不会报错，只会让 P95 变成一个没有含义的中间值。

    ⚠️ 为什么要 ``agent`` 标签：拆分之后**一次请求 = N 次运行**（转交那一轮是 2 次）。
    没有这个标签，所有按 ``exit_reason`` 聚合的看板都会把"运行数"当成"请求数"，
    阈值随之失真；有了它才能回答"是哪一段在空转"。取值有限（就是 agent 名），
    不会推高基数。
    """
    observability.counter("agent_runs_total", exit_reason=exit_reason, agent=agent)
    observability.histogram("agent_run_duration_seconds", stats.duration_seconds,
                            agent=agent)
    observability.histogram("agent_model_calls", stats.model_calls, agent=agent)
    observability.histogram("agent_tool_calls", stats.tool_calls, agent=agent)
    if stats.review_rounds:
        observability.histogram("agent_review_rounds", stats.review_rounds, agent=agent)


def _build_input_items(
    prompt: str,
    context: TaskContext,
    session: Any,
    append_prompt: bool,
) -> list[Any]:
    """组装这一次运行的输入项。

    这里要回答的是**同一个问题**，只是两种情形下判据不同：
    **本轮那句用户原话，会话里到底有没有了？**
    传了会话之后 SDK 会把输入**追加**进会话（``save_result_to_session``；它内部的
    去重只覆盖"同一轮内的重试"，跨"新一轮 run"照写）。所以重复送一次 = 会话里留下
    两条一模一样的用户消息，而它**不报错**，只表现为"模型莫名重复了一遍用户的话"。

    于是：

    * **本轮第一个 agent 节点**（``append_prompt=True``）⇒ 会话里还没有 ⇒ 带上；
    * **本轮后续 agent 节点**（``append_prompt=False``）⇒ 上一个节点已经写进去了 ⇒ 不带；
    * **会话不可用** ⇒ 一律带上 —— 历史没地方存，不带就等于模型什么都看不到，
      而那是一种**没有任何报错**的坏（会答得前言不搭后语，看起来像模型变笨）。
    * ⚠️ **人工打回那一轮是特例**：这一轮**不是**第一次跑这句用户原话 —— 第一遍
      （人工评审之前那次运行）就以 ``append_prompt=True`` 把它写进会话了。所以判据
      不是 ``append_prompt``（它在打回轮仍是 True，因为前台确实是本轮第一个节点），
      而是"**会话在不在**"：会话在 ⇒ 原话已经在里面，只送意见；会话不在 ⇒
      只能自己带上，否则模型只看得到一句"人工审核意见：…"，完全不知道在改什么。

    为什么把打回轮单独拿出来而不是复用 ``carry_turn``：复用会让"本轮第一个节点"
    与"这句原话已进会话"被当成同一件事 —— 两者在打回轮上**结论相反**。实测症状是
    会话条目从 2 条变 5 条、首条用户消息出现两次。
    """
    items: list[Any] = []

    if context.human_feedback:
        # 人工打回那一轮：原话在会话里，这里只把**意见**送进去。
        # 路由上打回只回到前台节点，所以这条分支只会在 ``append_prompt=True`` 时走到。
        if session is None:
            # 没有会话 ⇒ 原话没地方存，只能自己带上。
            items.append({"role": "user", "content": prompt})
        # 官方把评审反馈伪装成用户消息是同一手法（``llm_as_a_judge.py``）。
        items.append(
            {"role": "user", "content": f"人工审核意见：{context.human_feedback}"}
        )
    elif append_prompt or session is None:
        items.append({"role": "user", "content": prompt})
    return items


async def run_agent(
    agent: Agent[TaskContext],
    *,
    prompt: str,
    context: TaskContext,
    model: Any | None = None,
    session_id: str = "",
    max_turns: int = MAX_TURNS,
    append_prompt: bool = True,
) -> TaskOutcome:
    """跑**一个** agent 一次，返回这次运行的结果。

    agent: 本次运行用哪个 agent。图里的每个 agent 节点传各自的那一个
        （``ENTRY_AGENT`` / ``LITERARY_AGENT``，见 ``src/graph.py``）。
    prompt: **本轮新增的用户输入**。正常轮就是用户这句话；人工打回那一轮传原话
        （实际送进去的是意见，见 ``_build_input_items``）。
    context: 共享上下文（租户、检索器工厂、资产目录、统计对象）。
        ``context.human_feedback`` 非空 ⇒ 按"打回轮"处理。
    model: 模型覆盖；None 时取 ``runtime`` 里装配好的那一个。
    session_id: 跨轮历史的会话编号（本项目用 thread_id）。空串 / 建不起来 ⇒
        本轮没有历史，退化成"只看到这次送进去的内容"（降级，不抛）。
    append_prompt: 本轮的这句用户输入**是否要追加进会话**。
        每轮第一个节点用默认的 True；后续节点传 False（会话里已经有了）。
        没有会话时这个参数不起作用 —— 见 ``_build_input_items``。

    ⚠️ 只跑**一次** ``Runner.run``：评审已改成模型可调用的 ``as_tool``，
    这里没有"评审不通过就回灌重来"的循环（见模块 docstring）。

    ⚠️ 每次运行开始时**清空 ``context.delivered``**：它是"这次调用产出了什么"
    （评审结论、大纲、插图…），而 ``context`` 是**整轮共享**的。不清的话，
    上一次运行登记的评审结论会被下一次运行当成自己的 —— 表现为"一次根本没评审的
    运行被判成了 passed"，且没有任何报错。``rendered`` / ``assets`` / ``citations``
    刻意**不清**：前两个是跨运行幂等标记，后者是整轮的引用累积（出口护栏与接口层
    都要看它）。
    """
    bound = model if model is not None else runtime.get_model()
    if bound is None:
        raise RuntimeError("agent 链路未就绪（未装配模型）")

    run_config = build_run_config(bound)
    started = time.perf_counter()
    session = session_for(session_id)
    stats = context.stats
    #: 本次运行开始前的累计读数 —— 用来切出"这一次运行"的增量（见 _report_run_metrics）。
    before = (stats.model_calls, stats.tool_calls, stats.review_rounds)

    context.delivered.clear()
    input_items = _build_input_items(prompt, context, session, append_prompt)

    answer = ""
    exit_reason = "error"
    guardrail: str | None = None
    error_kind = ""
    result = None
    #: 本次运行结束时停在哪个 agent。空 = run 没跑到底 ⇒ 调用方保持 state 原值不变。
    ended_on = ""
    decision: dict[str, Any] = {}

    try:
        result = await Runner.run(
            agent,
            input_items,
            context=context,
            run_config=run_config,
            max_turns=max_turns,
            hooks=AGENT_HOOKS,
            # 会话负责"取历史 + 与本轮合并 + 写回"；没有它时模型只看得到 input_items。
            # ⚠️ 它是**这一个** run 的历史口，不是"本轮用什么模型/护栏"那类配置 ——
            # 后者在 run_config 里（会话对象的构造期参数也在那儿配，见 build_run_config）。
            session=session,
        )
    except OutputGuardrailTripwireTriggered as exc:
        # 出口护栏拦下的输出**不会**成为交付物 —— 官方语义：被拒的候选输出不落库。
        exit_reason, guardrail = "output_guardrail", _guardrail_name(exc)
    except InputGuardrailTripwireTriggered as exc:
        exit_reason, guardrail = "input_guardrail", _guardrail_name(exc)
    except MaxTurnsExceeded:
        exit_reason = "max_turns"
    except Exception as e:  # noqa: BLE001  上游抖动 / 协议错误
        logging.exception("agent 链路执行异常")
        exit_reason = "error"
        error_kind = f"{type(e).__name__}: {e}"

    if result is not None:
        final = result.final_output
        if isinstance(final, FrontlineDecision):
            # 带结构化判定的 agent（前台）：**产出就是判定**，正文在 content 字段里。
            # ⚠️ 不能走下面的 `str(final)` —— 那会拿到 pydantic 的 repr（字段名 + 括号），
            # 而它会被当成"给用户的答复"一路交付出去。
            decision = {"action": final.action, "content": final.content}
            if final.action != "transfer":
                answer = final.content or ""
        else:
            answer = str(final or "")
        # 观测"这轮谁收的尾"。⚠️ 它**不**再决定下一轮的入口（入口固定为前台），
        # 所以这里不需要 on_handoff 参与、也不会因为漏了回调而把谁永久拉走 ——
        # 取的是"本次 run 结束在哪个 agent"这个唯一的事实来源。
        ended_on = getattr(result.last_agent, "name", "") or ""
        # 渲染大纲、收资产：确定性步骤，放在代码里而不是做成工具。
        _flush_deterministic_steps(context)
        # 转交：这一步没有交付物，所以不走评审口径 —— 用"没评审"给它定档会让
        # `agent_runs_total{exit_reason="unreviewed"}` 虚高（转交是常态，不是漏审）。
        exit_reason = "transfer" if decision.get("action") == "transfer" else _verdict_of(context)

    passed = exit_reason in ("passed", "unreviewed")
    duration = time.perf_counter() - started
    stats.attempts = 1
    stats.exit_reason = exit_reason
    stats.guardrail = guardrail or ""
    # 累计而不是覆盖：一次请求跑 N 个节点，state 里那个 duration 要能回答
    # "整轮一共花了多久"。单次运行的耗时另有直方图（见 _report_run_metrics）。
    stats.duration_seconds += duration
    stats.last_score = getattr(context.recall("evaluation"), "score", "") or ""

    # 上报"能力"指标：跑成没跑成、跑得多慢、花了多少次模型调用与工具调用。
    # 传**这一次运行**的切片（见 _report_run_metrics 的说明）。
    _report_run_metrics(
        replace(
            stats,
            model_calls=stats.model_calls - before[0],
            tool_calls=stats.tool_calls - before[1],
            review_rounds=stats.review_rounds - before[2],
            duration_seconds=duration,
        ),
        exit_reason,
        agent=getattr(agent, "name", ""),
    )

    return TaskOutcome(
        # 没过审时**仍返回产出**：与 max_turns 同一考虑 —— 有疑点但不确凿，
        # 交给人工复核比丢给用户一句空话好。被护栏拦下的则不给（那是确凿的坏产出）；
        # 转交的也不给（那一段内容压根不是给用户看的）。
        answer=answer if passed or exit_reason in ("review_failed", "max_turns") else "",
        attempts=1,
        final_agent=ended_on,
        passed=passed,
        exit_reason=exit_reason,
        assets=list(context.assets),
        delivered=dict(context.delivered),
        guardrail=guardrail,
        error=error_kind,
        decision=decision,
    )
