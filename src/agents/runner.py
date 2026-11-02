"""代码编排（code-first orchestration）—— 官方 ``llm_as_a_judge.py`` 的形态。

官方把编排分成并列的两种，没有优劣：

* **模型决策式**：agent = instructions + tools + handoffs，模型自己决定下一步；
* **代码式**：在代码里做分类、串联、``while`` + 评审、``asyncio.gather`` 并行。

本文件是第二种。三件**本项目自己拿主意**的事（官方两个多轮示例都不是这么写的，
取舍理由写在下面 —— 不是"看起来更合理"，而是有明确代价对比）：

一、**一次运行跑完整条链**（2026-10-20 定，回到官方形态）
--------------------------------------------------------
``Runner.run(ENTRY_AGENT, ...)`` 一次跑到底：模型调工具、handoff 换人、
接手方接着写，全在同一次运行的 ``while`` 里完成（官方 ``run.py`` 的
``NextStepRunAgain`` / ``NextStepHandoff`` 两个分支都是 ``continue``）。
"这次结束在谁手上"由 ``result.last_agent`` 回答 —— 是既成事实，不是模型的自述。

⚠️ 2026-10-03 ～ 2026-10-20 之间本文件是"一次只跑一个 agent"，因为 agent 链路
当时被拆成了多个 LangGraph 节点（节点边界就是续跑边界，而 SDK 的 handoff 会在
节点内部把整条链跑完 ⇒ 拆了等于没拆）。图去掉之后那个前提消失，于是回到官方形态。

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

⚠️ 2026-10-19 再改一步：**评审结论不再决定交付方式（评分闸已删）**
------------------------------------------------------------------
上一次改动只把"跑不跑评审"交给了模型，但出口仍然拿评审结论办事：
``review_failed`` 被映射成 40 分，图那边按 ``>= HIGH`` 判"交人"——
等于模型评了一次，收尾权就被代码接管。用户要的是「让模型自己决定是否收尾、
是否启用评分机制，而不是在最后硬加一道评分闸」。所以：

* ``score_hint()`` **已删除**（agent 链路不产 0-100 分）；
* ``exit_reason="review_failed"`` **保留**，但语义降级为**纯观测**：
  它回答"运行结束时最后一次评审的结论是什么"，不再改变交付；
* ``passed`` 的判据随之扩大 —— 评了没过也是**正常运行收口**（有产出就交）。

由此 ``exit_reason`` 仍是四档，但**没有一档叫"没通过所以不给用户"**：
``passed``（评过且过）/ ``unreviewed``（模型选择不评）/ ``review_failed``（评了没过，
由模型自己决定是否接受）/ ``transfer``（交给下一个 agent，没有交付物）。
前三者都算正常收口。**评审是过程内反馈，不是出口判定**。

另外，**渲染 PPT / 落盘资产是确定性步骤，放在这里（代码）而不是做成工具**：
没有任何模型需要"决定要不要渲染"。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any

from agents.stream_events import RawResponsesStreamEvent
from openai.types.responses import ResponseTextDeltaEvent

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
from ..rag import citation_check
from . import (
    approval,
    budget,
    compaction,
    deadline,
    fallback,
    memory,
    reflection,
    runtime,
    supplements,
)
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

__all__ = [
    "ENTRY_AGENT",
    "LITERARY_AGENT",
    "MAX_TURNS",
    "TaskOutcome",
    "build_run_config",
    "model_input_filter",
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

    评审改成 ``as_tool`` 后不再有重试循环，所以恒为 1 —— **它不再是"返工几轮"的
    信号**（那个看 ``AgentStats.review_rounds``）。字段留着是因为调用方（能力探针
    脚本、日志）把它当"这一次跑了几回"报出来，删了要让那些输出改格式。
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

    fallback: fallback.FallbackVerdict | None = None
    """兜底判定（第 5 步）。``None`` = 没判（**转交**那一步没有交付物，不进判定表）。

    ⚠️ 它是**收尾口径的唯一来源**：`run_agent` 收尾时判一次、写在这里，服务层只管
    按它下发事件。判两次的结果必然是两套规则 —— 改一处漏一处，而症状是"某类失败
    该转人工却直接报错"，只有对应用例能发现。
    """

    fallback_draft: str = ""
    """兜底转人工时**带给审核人**的那段内容（入口护栏 = 用户那句请求；出口护栏 = 被拒的产出）。

    ⚠️ 它**不是交付物**。官方语义是"被拒的候选输出不落库"，这里有意偏离，
    只为让审核人判断"这是不是误拦" —— 那种判断没有原文根本做不了
    （``PLAN_step5_fallback.md`` §11）。
    """

    approvals: list[approval.ApprovalRequest] = field(default_factory=list)
    """等着用户点头的动作（``exit_reason == "awaiting_approval"`` 时有值）。

    ⚠️ 它**不是**"这次运行产出了什么"，而是"这次运行**停在哪**"：那些动作**还没执行**。
    别把它并进 ``delivered`` / ``assets`` —— 那两样是"已经做成了的事"。
    """

    citations: list[Any] = field(default_factory=list)
    """本轮检索命中的**结构化来源**（页码、章节路径、保真原文、表格网格…）。

    为什么跟着结果一起返回：接口层要把它发给客户端做"这句话出自哪一页"的回溯。
    此前这份数据只活在 LangGraph 的 state 里，于是 runner 无法单独把一轮的结果
    说完整 —— 依赖图的那一层一删，它就跟着丢了。
    """

    citation_check: dict[str, Any] = field(default_factory=dict)
    """引用校验结论。``citations`` 非空只说明"检索命中了东西"，推不出"答案用了它"，
    两者必须分开报（见 ``src/rag/citation_check.py``）。"""

    review_reason: str = ""
    """需要人看的原因（有限枚举）。空串 = 这一轮不需要人看。

    取值：``manual``（兜底判定要求人工介入）/ ``needs_review``（模型在交付时
    自己声明的）。前者是代码判的、后者是模型判的 —— 混成一个值会让"到底是谁
    认为要审"这个问题失去答案，而这两种情形的处置并不相同。
    """

    run_snapshot: str = ""
    """被打断那次运行的现场（JSON 串）。批准或拒绝之后从它接着跑。

    ⚠️ 可能很大（含整段历史），所以只在 ``awaiting_approval`` 时非空。
    ⚠️ 它**只能用一次**：官方那个运行状态消费掉批准结果之后就作废了。所以"同一个待批项
    只恢复一次"这条必须由调用方保证（服务层用待审记录的"已办理"来挡）。
    """

    #: ⚠️ 这里曾有 ``score_hint()``（把运行状态映射成 0-100 分，给图的评分闸用）。
    #: 2026-10-19 **已删除**：agent 链路不产分、也不该为了触发一个分支去编一个量纲。
    #: 收尾判据现在只有一处 —— ``fallback.decide`` 的结论（见模块 docstring）。


def _rejected_output(exc: BaseException) -> str:
    """从出口护栏的 tripwire 里取回**被拒的那段产出**（官方 ``agent_output``）。

    ⚠️ 这是"救回来存档"、不是"拿来交付"：官方语义是"被拒的候选输出不落库"，本服务只把它
    作为**转人工时的草稿**带给审核人（``PLAN_step5_fallback.md`` §11 的决策）。
    取不到就退化成空串 —— 宁可为一条诊断信息少带一点，也不要让收尾崩掉。
    """
    result = getattr(exc, "guardrail_result", None)
    return str(getattr(result, "agent_output", "") or "")


def _guardrail_name(exc: BaseException) -> str | None:
    """从 tripwire 异常里取护栏名。官方把结果挂在 ``exc.guardrail_result`` 上。"""
    result = getattr(exc, "guardrail_result", None)
    guardrail = getattr(result, "guardrail", None)
    if guardrail is None:
        return None
    getter = getattr(guardrail, "get_name", None)
    return getter() if callable(getter) else getattr(guardrail, "name", None)


async def model_input_filter(data: Any) -> Any:
    """``RunConfig.call_model_input_filter`` 的**装配处**：时限 → 预算 → 裁剪 → 注入。

    四步各有归属，这里只负责顺序：

    ⓪ ``deadline.check`` —— 跑太久就抛 ``AgentStoppedByDeadline``（**排在最前**）。
       为什么在预算之前：时间与钱往往同时超线，而**时间是用户直接感知的那个**
       （"我等了二十分钟"），也常常是花钱多的**原因**（圈数多）。
       判成 ``run_timeout`` 之后，成本曲线照旧在，运维仍然看得见"它花了很多钱"；
       反过来判成 ``token_budget``，就把"它慢"这个更根本的原因藏起来了。
    ① ``budget.check`` —— 到线就抛 ``AgentStoppedByBudget``。
       这一处就是"钱马上要花了"的那一拍，判在别处（工具结束、或运行收尾）要么
       判不到这次读入、要么已经是事后记账。
    ② ``history_budget.trim_model_input`` —— 窗口压缩 + 按 token 硬截断；
    ③ ``reflection.inject_reflections`` —— 追加一条 system 消息（反思闸门的结论）；
    ④ ``_inject_supplements`` —— 追加用户在本节点运行期间补说的话。

    ⚠️ 前两道闸门都排在裁剪**之前**：到线时不必先算一遍 token / 先跑一次压缩再扔掉
    结果 —— 裁剪要分词、压缩要**调一次模型**，而超线的运行往往正是历史最长的那些。

    ⚠️ **顺序不能反**：注入排在裁剪**之后**，塞进去的提醒才不会在下一次模型调用时
    被当成"超预算的老历史"裁掉。反过来的症状是"提醒过、但模型没按提醒做"，而日志与
    指标里完全看不出来（提醒确实生成了、也确实进了输入，只是在下一轮被丢了）。
    补充排在反思之后：一条来自用户的补充是**要求**，反思结论是**自检提醒**，
    两者同时出现时，让用户的要求落在最后（模型对末尾内容的注意力更强）。

    ⚠️ 注入**不占**预算：它排在预算判定之后，所以输入可能比 ``AGENT_HISTORY_TOKEN_LIMIT``
    略高一点 —— 那是有意的，一条几十字的提醒远小于"提醒没生效"的代价。
    同样地，**注入也不计入 token 预算的判定**：``budget.check`` 用的是"上一次调用
    已经花掉的量"（``AgentStats`` 的累计，含旁路），不是这次输入的预估值 ——
    它判的是"还该不该继续"，不是"这一条能不能塞下"。
    """
    context = getattr(data, "context", None)
    deadline.check(context)
    budget.check(context)
    trimmed = await trim_model_input(data)
    # 长期记忆排在裁剪**之后**、反思**之前**：它是一条稳定前缀（放最前），
    # 但同样不能早于裁剪 —— 否则会被当成"超预算的老历史"裁掉，
    # 症状是"明明记着、回答时却不知道"，而日志里一切都正常。
    with_memory = await memory.inject_memories(
        trimmed, context=getattr(trimmed, "context", None) or context)
    injected = await reflection.inject_reflections(
        with_memory, context=getattr(with_memory, "context", None) or context)
    return _inject_supplements(injected, getattr(injected, "context", None) or context)


def _item_text(item: Any) -> str:
    """取一条输入项的文本（输入项可能是 dict，也可能是 SDK 的 Item 对象）。"""
    content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
    return content if isinstance(content, str) else ""


def _inject_supplements(data: Any, context: Any) -> Any:
    """把"本次运行开始之后用户补说的话"拼到模型输入末尾。

    context: ``TaskContext``。它带着两个值 —— ``thread_id`` 与
        ``supplement_cursor``（run 开始时定格的水位）。
    data: ``ModelInputData``（有 ``input`` 列表）。

    为什么每次模型调用都拼、而且拼完**不**从暂存里删：注入口的改写是"本次调用
    专用"的（实测：拼进去的内容下一次调用就没了），所以删掉就再也不会出现；
    留着才能让模型在后续每一轮都还知道这条要求。同一次调用内不会重复 ——
    ``data.input`` 是这一轮的快照，不是一个跨调用累积的列表。

    ⚠️ 末尾去重是防御而不是主路径：正常情况下水位已经把"run 开始前就写进会话的
    补充"挡在外面了（那条会随会话历史一起进输入）。这一层只在"某条补充既被写进
    会话、又落在水位之后"这种边界情形下挡住重复。
    """
    thread_id = getattr(context, "thread_id", "") or ""
    if not thread_id:
        return data
    items = getattr(data, "input", None)
    if not isinstance(items, list):
        return data
    try:
        texts = supplements.since(thread_id, int(getattr(context, "supplement_cursor", 0) or 0))
    except Exception:  # noqa: BLE001  补充是增强，任何故障都不该让模型调用失败
        logging.debug("取任务执行中补充失败（已忽略）", exc_info=True)
        return data
    if not texts:
        return data

    tail = {_item_text(i) for i in items[-4:]}
    for text in texts:
        line = f"{supplements.PREFIX}{text}"
        if line in tail:
            continue
        items.append({"role": "user", "content": line})
        tail.add(line)
    return data


def build_run_config(model: Any | None = None, *,
                     group_id: str | None = None,
                     trace_metadata: dict[str, Any] | None = None) -> RunConfig:
    """一处装配、全链路生效。

    ``RunConfig.model`` 的优先级**高于** ``Agent.model``（官方三层优先级里最高的
    一层），而且 ``as_tool()`` 的嵌套 run 在没有显式传 ``run_config`` 时会**继承
    父 run 的**（SDK 源码：``resolved_run_config = run_config or context.run_config``）。
    ⇒ 这里挂一次，前台、专家、嵌套 run 全部用同一个模型。

    出口护栏挂在这里：它只对"产出**最终输出**的那个 agent"生效，而"谁是最后一个
    agent"取决于这一轮有没有发生交接（自己答复 → 前台；交接 → 文学专家）——
    挂在 RunConfig 才与"谁是最后一个"完全无关。每一次运行的最终输出都会被它检查，
    而"这一步没有交付物"的情形由护栏自己跳过，见 ``guards._deliverable``。

    ⚠️ 入口护栏**不在这里**（2026-10-03 从本函数移走）。它现在挂在
    ``frontline_agent.input_guardrails``：每轮的第一个 agent 固定是前台，
    "第一份输入必被扫"从此是**结构保证**的，不需要再靠"挂得离拓扑远一点"来兜。
    两处都挂会让每次运行跑两遍 —— 官方那两个池子是**相加**：
    ``starting_agent.input_guardrails + (run_config.input_guardrails or [])``。

    跨轮历史那个开关也在这里挂（与 ``session_for`` 里的后端选择配套）：

    * ``call_model_input_filter``：**模型输入的最后一道加工**，现在是两步
      （装配见 ``model_input_filter``）：
      ① **按 token 裁"送进模型的那一份"**（2026-10-03 起）—— 官方 docstring 写明
      这个口子的用途就是 "to stay within a token limit"，实现见
      ``src/agents/history_budget.py``（预算 ``AGENT_HISTORY_TOKEN_LIMIT``，默认 10 万）；
      ② **注入反思提醒**（2026-10-09 起）—— 见 ``reflection.py``，排在裁剪**之后**。
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

    可观测的两个标识也在这里挂（2026-10-30 起）—— 它们是**官方字段**，不是自造的：

    * ``group_id``：这是 trace 的会话归组键。LangSmith 的转换层在
      ``on_trace_start`` 里把它写成 ``metadata["thread_id"]``（``trace.export()``
      带 ``group_id``，处理器照抄），平台侧"同一次对话的几轮"就是按它聚起来的。
      不传 ⇒ 每一轮都是独立的一条 trace，**多轮对话永远串不成一条线**，且没有任何
      报错。本项目一轮 = 一个 ``thread_id``，所以这里直接传 ``session_id``。
    * ``trace_metadata``：每轮**不同**的运行级元数据。``request_id`` 只能走这里 ——
      用户报"这次答得不对"时手上只有响应头那个 rid，而平台的搜索框按 metadata 建
      索引，不挂上去就只能按时间点大海捞针。

    ⚠️ 这两个字段**只对 LangSmith 生效**。Langfuse 那条路走的是
    ``app/tracing.py::trace_scope()``（上下文变量，靠节点启动时读取），与这里是
    两套独立机制；两个后端都配着的时候，两处都要有值才不会缺维度。
    ⚠️ 它们是 ``RunConfig`` 的字段，而 ``as_tool()`` 的嵌套 run 会**继承**父 run 的
    配置（与 ``model`` 同一条规则），所以挂一次就覆盖整棵嵌套调用。
    """
    return RunConfig(
        model=model,
        workflow_name="chatagent_sub_agents",
        output_guardrails=[citation_traceability_guardrail, length_guardrail],
        call_model_input_filter=model_input_filter,
        reasoning_item_id_policy="omit",
        # 默认会把对话内容带上追踪。本服务的内容是用户数据，
        # 与 main.py 里"日志不落原文"的口径一致：关掉。
        trace_include_sensitive_data=False,
        # 会话归组键 / 每轮运行级元数据。空值统一归一成 None：
        # 官方字段的默认值就是 None，传空串会让"没有会话"变成"会话 id 是空串"，
        # 平台侧会出现一个所有无会话请求都挤进去的分组。
        group_id=group_id or None,
        trace_metadata=trace_metadata or None,
    )


#: 每轮**第一个**跑的 agent（前台）—— 固定，不读任何跨轮状态。
#:
#: 官方示例把它当**初值**（``current_agent = triage_agent``），之后改成
#: ``result.last_agent``；本项目刻意只保留初值这一半，理由见模块 docstring 的
#: "入口固定为前台"。之所以做成模块级常量而不是写在 ``run_agent`` 里：测试与文档要能
#: 引用"入口是哪个 agent"，而写在函数体里就只能靠读源码。
ENTRY_AGENT = frontline_agent

#: 由前台通过官方 ``handoffs`` 交接过去的那个 agent（文学创作）。
#:
#: ⚠️ 它**不是**"第二个前台"：只有前台能答复用户，本 agent 跑完即收尾
#: （交接之后控制权不回来 —— 官方的 handoff 是"换人接着跑"，没有"再转回前台"这回事），
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
        # `recall` 现在给的是 dict（见 `TaskContext.record`），不再是 pydantic 对象 ——
        # 所以这里用 `json.dumps` 而不是 `model_dump_json()`。
        path = render_slide_deck(json.dumps(deck, ensure_ascii=False), ctx.asset_dir)
        ctx.assets.append(path)
        ctx.stats.assets += 1
        ctx.rendered.add("slides")


def _score_of(context: TaskContext) -> str:
    """本次运行的评审得分；没评审过就是空串。

    与 ``_verdict_of`` 共用一处取法：两处都在从 ``evaluation`` 里取 ``score``，
    各写一遍时"一处改成 dict、另一处还在 getattr"几乎必然发生 ——
    而那种错**不会报错**，只会让得分永远是空串。
    """
    return (context.recall("evaluation") or {}).get("score", "") or ""


def _verdict_of(context: TaskContext) -> str:
    """按"**这次运行**里模型到底评审过没有"定 ``exit_reason``。

    评审是模型**可选择**的工具（``as_tool``），所以三种情形必须分开：

    * 一次都没调 ⇒ ``unreviewed``。**不等于"审过了"** —— 代码不能替模型宣布
      "这份被审过了"；指标上要能看出跳过的比例（这是选择"模型自主"必须付的透明度成本）。
    * 评审且 ``pass`` ⇒ ``passed``
    * 评审但没 ``pass`` ⇒ ``review_failed``。⚠️ 它**只是观测**：2026-10-19 起
      这类运行照样正常交付（评审结论是过程内反馈，模型自己决定接不接受，见模块
      docstring）。保留这个取值是为了让"多少交付是在评审未通过的情况下给出的"
      能在 ``agent_runs_total{exit_reason}`` 上被看见。

    ⚠️ 判据是"**本次运行**"而不是"本轮的累积"：``run_agent`` 在每次运行开始时
    清空 ``context.delivered``（理由见那里），所以这里的 ``recall`` 天然只看得见
    这次运行登记的评审结论。不清的话，前台那次运行的评审结论会**冒充**文学那次
    运行的评审结论（``passed`` 会凭空出现在一次根本没评审的运行上）。
    """
    verdict = context.recall("evaluation")
    if verdict is None:
        return "unreviewed"
    # ⚠️ 是 dict 不是对象。写成 `getattr(verdict, "score", "")` 不会报错 ——
    # 它会**静默**返回空串，于是每一次评审都被判成 `review_failed`，
    # 而指标上看起来只是"评审通过率 0%"，没人会想到是取字段的方式错了。
    return "passed" if _score_of(context) == "pass" else "review_failed"


def _bypass_slice(
    before: dict[str, dict[str, int]], current: dict[str, dict[str, int]]
) -> dict[str, dict[str, int]]:
    """把旁路 token 从"累计"切成"这一次运行的增量"。

    为什么要单独一个函数而不是在 ``replace(...)`` 里内联：``bypass_tokens`` 是
    **嵌套字典**，``dataclasses.replace`` 只会原样搬引用 —— 上一份的字典被下一次
    运行接着改，于是第二次运行报出来的是两次之和。表现是"同一个 agent 的成本在
    请求内递增"，而每一次读数单独看都"有值、不为零"，不会触发任何告警。

    ⚠️ 增量两栏都为 0 的来源**直接丢掉**：留着它，``budget.report`` 里那些
    ``if kind_in:`` 虽然会跳过计数，但字典本身会让"这一次到底动过哪条旁路"这个
    问题在调试时产生误导（看起来像动过，其实一笔都没有）。
    """
    out: dict[str, dict[str, int]] = {}
    for source, entry in (current or {}).items():
        was = before.get(source) or {}
        delta_in = int(entry.get("input", 0) or 0) - int(was.get("input", 0) or 0)
        delta_out = int(entry.get("output", 0) or 0) - int(was.get("output", 0) or 0)
        delta_cached = int(entry.get("cached", 0) or 0) - int(was.get("cached", 0) or 0)
        # ⚠️ ``cached`` 必须跟着切、也必须进这个条件（2026-11-01）：它是**新增键**，
        # 漏切的话 ``out[source]`` 里就没有它，于是旁路的命中量在报告里恒为 0 ——
        # 而"旁路从没命中"和"忘了传"在指标上一模一样。只判 input/output 会漏掉
        # "输入没变但命中变了"这种情形（上游重算 cache 时会出现）。
        if delta_in or delta_out or delta_cached:
            out[source] = {"input": delta_in, "output": delta_out, "cached": delta_cached}
    return out


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
        # 0 不是"评审很快"，是"没评审" —— 两者在分位数上完全不是一回事。
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


def _goal_of(prompt: str, context: TaskContext) -> str:
    """本轮"要解决的问题"的一句话表述（写进 ``context.goal``）。

    三种情形必须分开，否则反思拿到的是一个**错的目标**，比没有目标更糟：

    * **人工打回轮** —— ``prompt`` 还是那句原话（前台是本轮第一个节点），但这一轮真正
      要做的是"按意见改"。写原话会让反思以为一切照旧，而它面前的动作全是"在改稿"⇒
      一路判 ``ok``。所以这里写的是意见。
    * **正常轮** —— 用户那句话。
    * 两者都为空（理论上不会）—— 空串，反思那一侧会跳过目标行（见
      ``reflection.evidence_of``），**不要**在这里编一句"完成用户的任务"顶上：
      一句放之四海皆准的目标会让 ``off_track`` 永远判不出来。
    """
    feedback = (context.human_feedback or "").strip()
    if feedback:
        return f"按人工审核意见修改：{feedback}"
    return (prompt or "").strip()


async def _run_streamed(
    agent: Agent[TaskContext],
    run_input: Any,
    *,
    context: TaskContext,
    run_config: RunConfig | None,
    max_turns: int,
    session: Any,
    on_text: Any,
) -> Any:
    """按官方流式接口跑一次，把正文增量边产边交给 ``on_text``。

    为什么不再用 ``Runner.run`` + 事后切段
    --------------------------------------
    ``Runner.run`` 是**整段返回**：它跑完才拿到 ``final_output``。此前服务层虽然
    一路是 SSE，但主链路（agent）拿到的是一整段文本，只能一次性发出去 ——
    用户看到的是"等完整段生成时间，然后所有字同时出现"，而
    ``llm_first_token_seconds{mode="buffered"}`` 那条指标记录的正是这件事本身。
    换成 ``run_streamed`` 之后，token 真正逐段到达。

    事件怎么取
    ----------
    官方把流分成三类：``RawResponsesStreamEvent``（模型原始事件）、
    ``RunItemStreamEvent``（语义事件：工具调用、消息落库）、
    ``AgentUpdatedStreamEvent``（换人）。正文增量在**第一类**的 data 上：
    只有 ``ResponseTextDeltaEvent`` 才带 ``.delta``，其余原始事件一律跳过。

    ``item_id`` 作为"这一次模型调用"的标记传给回调：服务层用它区分
    "同一段生成的连续 chunk"与"换了一次调用（中间隔了工具执行）"，
    TTFT 与 chunk 间隔两条指标都依赖这个区分。

    ⚠️ **异常在迭代时抛出**，不在 ``run_streamed(...)`` 调用那一刻。所以调用方
    的 ``try`` 必须包住整个迭代（见 ``run_agent``），否则护栏与限流会绕过所有
    收尾逻辑，表现为"运行炸了但调用方以为它成功了"。
    """
    streamed = Runner.run_streamed(
        agent,
        run_input,
        context=context,
        run_config=run_config,
        max_turns=max_turns,
        hooks=AGENT_HOOKS,
        session=session,
    )
    async for event in streamed.stream_events():
        if not isinstance(event, RawResponsesStreamEvent):
            continue
        data = event.data
        if not isinstance(data, ResponseTextDeltaEvent):
            continue
        delta = getattr(data, "delta", "") or ""
        if delta:
            await on_text(delta, getattr(data, "item_id", "") or "")
    return streamed


async def run_agent(
    agent: Agent[TaskContext],
    *,
    prompt: str,
    context: TaskContext,
    model: Any | None = None,
    session_id: str = "",
    max_turns: int = MAX_TURNS,
    append_prompt: bool = True,
    resume: approval.ResumeRequest | None = None,
    on_text: Any | None = None,
    trace_metadata: dict[str, Any] | None = None,
) -> TaskOutcome:
    """跑**一个** agent 一次，返回这次运行的结果。

    agent: 本次运行的**起点** agent。正常一轮固定是 ``ENTRY_AGENT``（前台）；
        换人由它自己的 ``handoffs`` 在运行内部完成，所以这里不需要按"第几个节点"
        分别传参（原来那套 `ENTRY_AGENT` / `LITERARY_AGENT` 双入口是给图的两个节点用的）。
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
    resume: **接着跑**一次被打断的运行所需的输入（``approval.ResumeRequest``）。
        给了它，本次就是"从被打断的地方继续"，而不是"跑一轮新的"：``prompt`` /
        ``session_id`` / ``append_prompt`` 三者都不起作用（历史已经在现场里）。

        "接着跑"为什么成立（而不是"重跑一轮"）、两条路各自的代价，
        见 ``approval`` 模块的模块 docstring —— 那里有实测证据，不是推断。

    on_text: 正文增量回调 ``async (text, call_id) -> None``。给了就走官方
        ``Runner.run_streamed``（见 ``_run_streamed``），不给就走 ``Runner.run``
        （整段返回）。两条分支产出的 ``TaskOutcome`` 完全一致 —— 差别只在
        "什么时候把字交出去"，不在"跑成什么样"。
    trace_metadata: 这次运行的元数据（原样交给 ``RunConfig.trace_metadata``）。
        **本层不解释它的内容** —— ``request_id`` / ``tenant`` 是传输层概念，
        ``src/`` 不认识它们，由 ``app/`` 侧组装后传进来；反过来 ``src/`` 去读
        ``app/`` 的上下文变量会形成反向依赖。空字典与 None 等价。

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

    run_config = build_run_config(bound, group_id=session_id,
                                  trace_metadata=trace_metadata)
    started = time.perf_counter()
    # 墙钟闸门的起点：**在 Runner.run 之前**，因为闸门判的是"这次运行跑了多久"，
    # 排队、恢复现场、拼输入都算在内。放在 try 里面就晚了 —— 异常路径下不记起点，
    # 下次 check 会读到 0 而永不触发（方向安全，但等于闸门失效）。
    deadline.mark_start(context)
    stats = context.stats
    #: 本次运行开始前的累计读数 —— 用来切出"这一次运行"的增量（见 _report_run_metrics）。
    #: token 也切：一次请求可能跑两个节点（前台 + 接手方），用累计那份会让"每次运行的成本"
    #: 把前一个节点的也算进去 —— 看板上的单价直接翻倍。
    before = (
        stats.model_calls,
        stats.tool_calls,
        stats.review_rounds,
        stats.input_tokens,
        stats.output_tokens,
        # ⚠️ 命中量必须一起切（2026-11-01）。``replace()`` 会把没点名的字段**原样搬过去**
        # —— 少切一个，这一次运行报出来的就是**累计**命中量配**本次**输入量，
        # 于是命中率虚高（甚至被 ``cache_hit_rate`` 的 min(1.0) 悄悄截断成 100%）。
        # 这正是"两次 replace 迟早有一处忘了减 before"那个坑的当场重演：零报错、
        # 数字看着还挺漂亮。加新字段时请一并检查这份元组与下面的 slice。
        stats.cached_input_tokens,
    )
    # 旁路（反思 / 压缩）那笔同样要切：它的写入是**异步**的（后台任务、工作线程），
    # 只按"结束时读一次"切片的话，第二个节点会把第一个节点的旁路成本也报进去。
    before_bypass = {k: dict(v) for k, v in stats.bypass_tokens.items()}

    context.delivered.clear()
    if resume is not None:
        # 接着跑：输入是"那次被打断的运行现场"，不是新一轮的用户输入。
        # ⚠️ **不接会话** —— 历史已经在现场里了；再接一次会把同一段历史喂两遍，
        # 而症状是"模型看到的内容莫名翻倍"，从代码上完全看不出来。
        session = None
        run_input: Any = await approval.restore(
            resume.snapshot,
            agent=agent,
            context=context,
            approved=resume.approved,
            reason=resume.reason,
        )
    else:
        session = session_for(session_id)
        run_input = _build_input_items(prompt, context, session, append_prompt)
        # ★ 任务执行中补充：水位必须在 run **开始之前**定格。
        # 现取的话，注入口每次模型调用都会把"调用之前的所有补充"当成新的，
        # 同一条要求会在每一轮被重复拼进去（表现成"模型突然反复强调一句补充"）。
        # ⚠️ 恢复（``resume``）那条分支刻意不设：它的输入是"被打断的运行现场"，
        # 现场里已经含当时的会话历史，再叠一条增量会让"模型看到的内容"与
        # "那次运行实际收到的"对不上，排查时无从复现。
        context.thread_id = session_id
        context.supplement_cursor = supplements.cursor(session_id)
        # 目标在**运行开始时**定格：反思闸门要拿它判"还在不在解题"，而它跑在后台、
        # 只能读上下文 —— 现读的话拿到的是"已经跑了几步之后"的上下文，与判据同源
        # 就没意义了（它必须与"这一步为什么被做"对齐，不是与被判的那一刻对齐）。
        context.goal = _goal_of(prompt, context)

    answer = ""
    exit_reason = "error"
    guardrail: str | None = None
    error_kind = ""
    fallback_draft = ""
    result = None
    #: 本次运行结束时停在哪个 agent。空 = run 没跑到底 ⇒ 调用方保持 state 原值不变。
    ended_on = ""

    try:
        async def _invoke_once() -> Any:
            """跑一次 run。抽成函数是为了让溢出兜底能**原样再跑一次**。"""
            if on_text is None:
                return await Runner.run(
                    agent,
                    run_input,
                    context=context,
                    run_config=run_config,
                    max_turns=max_turns,
                    hooks=AGENT_HOOKS,
                    # 会话负责"取历史 + 与本轮合并 + 写回"；没有它时模型只看得到
                    # input_items。⚠️ 它是**这一个** run 的历史口，不是"本轮用什么
                    # 模型/护栏"那类配置 —— 后者在 run_config 里。
                    session=session,
                )
            # 流式：正文边产边交给回调。异常在迭代里抛（见 _run_streamed），
            # 所以下面的 except 分支同样覆盖它 —— 两种形态的收尾口径必须一致。
            return await _run_streamed(
                agent,
                run_input,
                context=context,
                run_config=run_config,
                max_turns=max_turns,
                session=session,
                on_text=on_text,
            )

        try:
            result = await _invoke_once()
        except Exception as exc:  # noqa: BLE001  只为识别"上下文超了"
            # 溢出兜底（对应 DSH 的 ``maxOverflowRetries``）：模型已经明确拒了，
            # 再拿一个可能同样算错的预算去商量没有意义 —— 直接**只留最后一轮**压一次。
            #
            # ⚠️ 只在**非流式**重试。流式下前面推给客户端的正文已经出去了，
            # 重跑会把同一段再推一遍 —— 用户看到重复内容，比"这一轮失败"更糟。
            # 流式这条缺口是**已知**的，处置是记指标（能看到它真发生过），
            # 而不是假装已经覆盖。
            if (on_text is not None or session is None or not session_id
                    or not compaction.is_context_overflow(exc)):
                raise
            observability.counter("agent_context_overflow_total", action="compact_retry")
            logging.warning("模型报上下文超限 ⇒ 强制压一次历史后重试（只重试一次）")
            await compaction.compact_session_history(
                session_id, session=session, force=True,
                owner_key=str(getattr(context, "owner_key", "") or ""))
            result = await _invoke_once()
    except OutputGuardrailTripwireTriggered as exc:
        # 出口护栏拦下的输出**不会**成为交付物 —— 官方语义：被拒的候选输出不落库。
        # ⚠️ 但那段产出要"救回来存档"：转人工时它作为草稿带给审核人（见 fallback_draft）。
        exit_reason, guardrail = "output_guardrail", _guardrail_name(exc)
        fallback_draft = _rejected_output(exc)
    except InputGuardrailTripwireTriggered as exc:
        exit_reason, guardrail = "input_guardrail", _guardrail_name(exc)
        # 入口护栏拦下的是**用户那句话本身** —— 转人工时要带给审核人的就是它。
        fallback_draft = prompt or ""
    except MaxTurnsExceeded:
        exit_reason = "max_turns"
    except reflection.AgentStoppedByGuard:
        # 第 3 级踩刹车（从注入口抛出来的）。⚠️ **必须排在上面那个 `except Exception` 之前**：
        # 否则会被它吞成 `exit_reason="error"`，表现是"停是停了、理由却变成崩溃"，
        # 而运维看到"崩溃率上升"会去查一个根本不存在的故障。
        # ⚠️ 这里**不冒泡**（与 `error` 那条不同）：主线的收尾要照常走完 ——
        # 判定表会把它收成 `failed`（运行被打断 ⇒ 没有产出物）。
        exit_reason = "guard_stop"
    except budget.AgentStoppedByBudget:
        # token 预算烧穿（同样从注入口抛出来，理由与上一条完全一致：**排在
        # `except Exception` 之前**，否则"预算用完了"会伪装成"上游崩了"）。
        # 判定表把它收成 failed + token_budget（它在注入口抛出 ⇒ 没有产出物）。
        exit_reason = "token_budget"
    except deadline.AgentStoppedByDeadline:
        # 墙钟到点（同样从注入口抛出来，同样必须排在 `except Exception` 之前）。
        # ⚠️ 与预算一样：这一步抛出时 ``result`` 是 None ⇒ 这一轮**没有答复正文**，
        # 流式下只有客户端手里的增量。判定表收成 failed + run_timeout。
        exit_reason = "run_timeout"
    except Exception as e:  # noqa: BLE001  上游抖动 / 协议错误
        logging.exception("agent 链路执行异常")
        exit_reason = "error"
        error_kind = f"{type(e).__name__}: {e}"

    # 工具审批（加固阶段三）：模型请求了一个**高危动作**，这一轮就地停下来等人点头。
    #
    # ⚠️ 三处刻意的顺序，每一处都有具体后果：
    #   · 排在下面所有处理**之前** —— 中断那一刻 `final_output` 是 None，照常规路径走
    #     会被当成"没产出"（判定表第⑤行）⇒ 用户看到的是一条**错误报告**，而不是
    #     "等你授权"。探针 P4 实测过这个后果，所以这一步不能省。
    #   · **不跑** `_flush_deterministic_steps` —— 用户可能拒绝，那时不该已经把大纲
    #     渲染出来、资产收好了（落盘的东西撤不回）。
    #   · `exit_reason` 单独一档 —— 判定表给它一行（**不是** failed、也**不算**兜底）。
    approvals: list[approval.ApprovalRequest] = []
    run_snapshot = ""
    if result is not None:
        approvals = approval.requests_from(result)
        if approvals:
            exit_reason = "awaiting_approval"
            run_snapshot = approval.snapshot(result)
            logging.info(
                "高危动作待用户授权：%s", "；".join(a.summary for a in approvals)
            )

    if result is not None and not approvals:
        # 交付物**优先取 ``deliver`` 交出来的正文**（2026-10-20）：模型可能在交完之后
        # 又补一句"已交付"，那句不是给用户的内容（提示词也要求它别重复正文）。
        # 没调 deliver 时回落 ``final_output`` —— 闲聊与追问走的就是这条路，
        # 与改造前逐字节一致。
        delivered = str(context.delivered.get("delivery") or "")
        answer = delivered or str(result.final_output or "")
        # 观测"这轮谁收的尾"。⚠️ 它**不**再决定下一轮的入口（入口固定为前台），
        # 所以这里不需要 on_handoff 参与、也不会因为漏了回调而把谁永久拉走 ——
        # 取的是"本次 run 结束在哪个 agent"这个唯一的事实来源。
        ended_on = getattr(result.last_agent, "name", "") or ""
        # ★ "转交"的判据是**运行结束在谁手上**，不是模型自己报的一个字段
        # （2026-10-20 起用官方 handoff：SDK 换掉 ``current_agent`` 后接着跑，
        # ``last_agent`` 就是换人的结果）。模型说的不算，跑的算。
        #
        # ⚠️ 必须同时要求"起始 agent 是前台"：直接跑文学专家（例如旧的 ``literary``
        # 图节点、或者测试里单跑它）时 ``last_agent`` 也不是前台，但那不是一次转交 ——
        # 它是"这次运行本来就是它"。漏掉前半句，文学的每一次正常产出都会被记成
        # ``exit_reason="transfer"``，而 transfer 在判据里另有含义。
        transfer = (
            getattr(agent, "name", "") == ENTRY_AGENT.name
            and bool(ended_on)
            and ended_on != ENTRY_AGENT.name
        )
        # 渲染大纲、收资产：确定性步骤，放在代码里而不是做成工具。
        _flush_deterministic_steps(context)
        # 转交：这次运行没有"前台给用户的答复"，产出是接手方写的东西。
        # 用"没评审"给它定档会让 `agent_runs_total{exit_reason="unreviewed"}` 虚高
        # （转交是常态，不是漏审）。
        exit_reason = "transfer" if transfer else _verdict_of(context)

    # 2026-10-19：评审结论**不再决定能不能收口**。``review_failed``（模型评了、但结论
    # 不是 pass）也是**正常收口** —— 它改不改、接不接受这个结论是模型自己的事
    # （见模块 docstring）。所以 ``passed`` 的判据从"评过且过 / 没评"扩到含它。
    #
    # ⚠️ 2026-10-20：``transfer`` 也并进来了。此前它不算，理由是"那一步没有交付物"——
    # 那是**图节点**形态下的结论（前台把对话交给下一个节点，自己什么都没写）。
    # 换成官方 handoff 之后，交接在**同一次运行内**完成：接手方写完作品，``last_agent``
    # 是它，``answer`` 就是作品本身 ⇒ 这一次运行**有**可交付内容。不并进来会怎么样：
    # 文学请求的作品被判成"没有答复"，兜底表走第⑤行 ⇒ 用户收到一条错误报告。
    passed = exit_reason in ("passed", "unreviewed", "review_failed", "transfer")
    duration = time.perf_counter() - started
    stats.attempts = 1
    stats.exit_reason = exit_reason
    stats.guardrail = guardrail or ""
    # 累计而不是覆盖：一次请求跑 N 个节点，state 里那个 duration 要能回答
    # "整轮一共花了多久"。单次运行的耗时另有直方图（见 _report_run_metrics）。
    stats.duration_seconds += duration
    stats.last_score = _score_of(context)

    # 上报"能力"指标：跑成没跑成、跑得多慢、花了多少次模型调用与工具调用。
    # 传**这一次运行**的切片（见 _report_run_metrics 的说明）。
    slice_of_run = replace(
        stats,
        model_calls=stats.model_calls - before[0],
        tool_calls=stats.tool_calls - before[1],
        review_rounds=stats.review_rounds - before[2],
        input_tokens=stats.input_tokens - before[3],
        output_tokens=stats.output_tokens - before[4],
        cached_input_tokens=stats.cached_input_tokens - before[5],
        bypass_tokens=_bypass_slice(before_bypass, stats.bypass_tokens),
        duration_seconds=duration,
    )
    _report_run_metrics(slice_of_run, exit_reason, agent=getattr(agent, "name", ""))
    # token 成本与"预算到底有没有生效"在这里报（口径 = 上面同一份切片）。
    # ⚠️ 必须与 `_report_run_metrics` 用**同一份**切片：分成两次 replace 迟早会有一处
    # 忘了减 before，表现是"某个 agent 的成本偏高"，而两次读数都"看起来正常"。
    budget.report(slice_of_run, agent=getattr(agent, "name", ""))

    # 交付物：``passed`` 已经含 ``review_failed``（评审没过照样交，见上），
    # 这里额外放行的只剩 ``max_turns`` —— 它可能已经写出了完整答复，只是没收敛；
    # 有疑点但不确凿，交给人工复核比丢给用户一句空话好。
    # 被护栏拦下的不给（那是确凿的坏产出）；转交的也不给（那一段内容压根不是给用户看的）。
    #
    # ⚠️ ``token_budget`` **不在这里**（2026-10-21 明确）：它在注入口抛出 ⇒ ``result``
    # 是 None ⇒ ``answer`` 本来就是空串，放行等于写一句永远不生效的代码。它必然落到
    # 判定表第⑤行（failed + token_budget），那是对的：这次运行确实没有收口。
    # 流式下客户端已经收到的增量由接口层按既有口径下发（与超时中断同一条路），
    # 不在这里伪造一份"交付物"。
    deliverable = answer if passed or exit_reason == "max_turns" else ""

    # 兜底判定（第 5 步）：**转交**那一步不进表 —— 它没有交付物，拿它进表会让每一次
    # 正常转交都落进"没有可交付的答复"那一行（转交是常态，不是出问题）。
    verdict = None
    if exit_reason != "transfer":
        verdict = fallback.decide(context, exit_reason=exit_reason, answer=deliverable)
        fallback.report(
            verdict, exit_reason=exit_reason, agent=getattr(agent, "name", "")
        )

    # 引用校验：**只算，不拦**。接口层按 `citation_check` 决定要不要下发引用，
    # 拦截逻辑不在算的这一层（在这里改写文案会让"模型到底产出了什么"不可复现）。
    #
    # ⚠️ 被出口护栏拦下的那一轮，这里的结论是 `status="no_answer"`（而不是
    # `unsupported`）：官方出口护栏一旦 tripwire，`final_output` 就是空串，
    # 到这一步已经**没有文本可校验**了。真正的判据在护栏里算过（`guards.py` 的
    # `citation_traceability_guardrail`），并以
    # `agent_output_check_total{status="unsupported"}` 计数、以 `output_info` 回给 SDK。
    # 所以"引用被拦"要看那条指标，**不要**从 `citation_check` 里找 —— 它答的是
    # "交付出去的那份文本的引用可信吗"。
    check: dict[str, Any] = {}
    if context.citations:
        try:
            check = citation_check.verify(
                deliverable, list(context.citations), docs_dir=context.docs_dir
            )
        except Exception as e:  # noqa: BLE001  观测失败不能影响作答
            observability.counter("citation_check_errors_total")
            logging.warning("引用校验失败（不影响作答）：%s", e)

    # 要不要人看：**两件不同的事**，别合并成一个布尔。
    #   ``needs_review`` —— 模型在交付那一刻自己声明的（deliver 工具挂审批回调，
    #       于是运行停在这里，现场在 ``run_snapshot`` 里，可以接着跑）；
    #   ``manual``       —— 代码在运行**结束后**判出来的（跑偏 / 护栏拦下 / 没产出）。
    # 后者没有可挂起的地方（运行已经结束），它的处置是"进待审队列、打回时新起一轮"。
    review_reason = ""
    if exit_reason == "awaiting_approval":
        if any(a.tool == "deliver" for a in approvals):
            review_reason = "needs_review"
    elif verdict is not None and verdict.outcome == "manual":
        review_reason = "manual"

    return TaskOutcome(
        answer=deliverable,
        attempts=1,
        final_agent=ended_on,
        passed=passed,
        exit_reason=exit_reason,
        assets=list(context.assets),
        delivered=dict(context.delivered),
        guardrail=guardrail,
        error=error_kind,
        fallback=verdict,
        fallback_draft=fallback_draft,
        approvals=list(approvals),
        run_snapshot=run_snapshot,
        citations=list(context.citations),
        citation_check=check,
        review_reason=review_reason,
    )
