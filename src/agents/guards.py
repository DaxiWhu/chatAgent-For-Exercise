"""校验原语 —— **校验是 guardrail，不是 agent**。

这是与"什么都做成子 agent"最本质的分歧点，也是官方**专门给了独立原语**的地方：

* ``@input_guardrail``：在链上**第一个** agent 之前跑，默认与模型**并行**（乐观执行）。
  触发 tripwire 立刻中止整个 run。
* ``@output_guardrail``：在产出**最终输出**的那个 agent 之后跑，必定**串行**。
  被拒的输出不会写进 session、不会成为交付物。

由此推出的两条工程结论：

1. **校验不该做成子 agent。** 做成 agent 就变成"主 agent 的一个工具"，于是
   "要不要校验"落到了被校验者手里 —— 它会在对自己不利时跳过。官方用独立原语
   把这件事放在模型的选择范围之外。
2. **判据要分级。** 能用确定性规则判的（编号可追溯、来源是否存在、长度）一律用
   纯函数，不花一次模型调用；只有"这句话有没有被资料支撑"这类**需要判断力**的，
   才按官方 ``output_guardrails.py`` 的形态在里面套一个轻量 agent。

本文件的三条护栏全部是确定性的 ⇒ 离线可跑、不占 ScriptedModel 的调用序列。
第三条（语义支撑）刻意**没有**实现：它需要模型判断，且本项目的
``citation_check`` 已经把"能确定性判的那部分"判掉了；在没有评测集证明它
净收益为正之前不引入（假阴/假阳都会直接改变交付行为）。

与示例版的差别
--------------
* 注入扫描复用 ``src/rag/guard.py``（与 RAG 链路**同一套**规则），而不是另写一份
  正则 —— 两份规则一定会漂移，而"两条链路防护口径不同"等于没有防护。
* 引用校验复用 ``src/rag/citation_check.py``（三层判据），而不是自己数编号。
"""

from __future__ import annotations

import logging
from typing import Any

from agents import (
    Agent,
    GuardrailFunctionOutput,
    RunContextWrapper,
    TResponseInputItem,
    input_guardrail,
    output_guardrail,
)

from .. import observability
from ..envutil import env_int
from ..rag import citation_check, guard
from .context import TaskContext

__all__ = [
    "citation_traceability_guardrail",
    "length_guardrail",
    "prompt_injection_guardrail",
]

MAX_ANSWER_CHARS = env_int("AGENT_MAX_ANSWER_CHARS", 8000, lo=1000)
"""单条交付物的字符上限。纯确定性、零成本 —— 用来演示"护栏可以是廉价的"。"""


def _text_of(item: Any) -> str:
    """把一条消息项的内容拍成文本（``content`` 可能是 str，也可能是分段列表）。"""
    if not isinstance(item, dict):
        return ""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _latest_user_text(payload: str | list[TResponseInputItem]) -> str:
    """取载荷里**最后一条用户消息**的文本（不是整份载荷）。

    ⚠️ 为什么必须区分（2026-10-02，接上跨轮会话之后）
    ------------------------------------------------
    传了会话（``Runner.run(..., session=...)``）之后，SDK 送进护栏的载荷是
    **历史 + 本轮新增**的合并结果 —— 官方 ``run.py`` 里那两行：

        prepared_input, _ = await prepare_input_with_session(raw_input, session, ...)
        original_input_for_state = prepared_input        # ← 非 server-managed 分支

    而护栏拿到的是 ``copy_input_items(original_input)``。于是照**整份**扫会有两个后果：

    * ``warn``（默认档）下，历史里那句命中会被**每一轮重复计数**：
      ``agent_input_scan_total`` 从"命中了几次请求"变成"命中次数 × 剩余轮数"，
      日志也每轮重复一条 —— 指标含义静默改变，而图形看起来只是"变多了"。
    * ``block`` 下后果是**功能级**的：用户第一轮说了句被判为注入的话（被拦下、
      而且那条输入会被写进会话，官方 ``persist_session_items_for_guardrail_trip``），
      于是**之后每一轮都会被历史里那句话拦住** —— 用户被永久锁在拒绝里，
      而他后面说的是完全正常的问题。

    判据本来就该是"**本轮新输入**"，这也是接会话之前的实际语义（那时载荷里只有本轮
    输入，整份拍平恰好等于它）。取最后一条用户消息把它恢复回来，挂载点仍然是官方
    原语（``RunConfig.input_guardrails``），没有自造新的拦截口。

    ⚠️ **接会话后"载荷含历史"不是推测，是实测**（`tmp_probe/probe_guardrail_payload_and_limit.py`）：
    连发两句时，第 2 次护栏调用拿到的载荷是**两条用户消息**（第 1 轮那句 + 本轮）。
    所以**不要**退回"把整份载荷拍平再扫"的写法 —— 那个 `_flatten` 帮手已删，
    加回来就等于把上面两个后果一起加回来。

    顺带说明为什么"扫历史"并不更安全：真正危险的注入来自**检索到的资料**，而它们
    以工具返回值的形式进对话，从来不是用户消息 —— 那道防线在 ``tools.kb_search`` 的
    ``guard.wrap_context``（见本文件末尾的说明）。
    """
    if isinstance(payload, str):
        return payload
    for item in reversed(list(payload or [])):
        if isinstance(item, dict) and (item.get("role") or "") == "user":
            return _text_of(item)
    # 没有用户消息（例如只有工具结果的重放）⇒ 视为"没有新输入"，不扫。
    return ""


def _as_text(agent_output: Any) -> str:
    """出口护栏拿到的不一定是 str（结构化产出是 pydantic 模型）—— 统一成文本。"""
    if isinstance(agent_output, str):
        return agent_output
    return str(agent_output or "")


def _deliverable(agent_output: Any) -> str | None:
    """取出**要交付给用户的那段正文**；这一步没有交付物时返回 ``None``。

    为什么需要这一层（2026-10-03，agent 链路拆成多个图节点之后）
    ----------------------------------------------------------
    出口护栏挂在 ``RunConfig`` 上 ⇒ **每一次 agent 运行**的最终输出都会被它检查。
    而拆分之后，跑终止于"判定为转交"的那次运行**根本没有交付物**：
    它的最终输出是一个结构化判定对象，``content`` 字段只是给下一个 agent 的
    交接说明（用户看不到）。

    不区分的话有两个后果，而且都不会报错：

    * 拿 ``str(判定对象)`` 去判 —— 判的是一个"字段名 + 括号"拼出来的 repr，
      判据含义静默改变；
    * 交接说明里一旦出现形如 ``[9]`` 的字样（而它并没有交付给用户），
      整条产出会被拦下 —— 用户收到的是一个**空答复**，而模型其实什么都没做错。

    ⇒ 判据必须是"**这一步有没有交付物**"，而不是"输出是不是一个字符串"。
    只有"要交给用户看的那段正文"才过护栏，其余一律跳过（**不计数** ——
    没交付物既不是"检查通过"也不是"检查失败"，它在指标里不该出现）。
    """
    if isinstance(agent_output, str):
        return agent_output
    content = getattr(agent_output, "content", None)
    if isinstance(content, str):
        # 结构化判定里的正文。``action == "transfer"`` ⇒ 这一步是"把对话交给下一个
        # agent"，没有交付物（见 ``types.FrontlineDecision``）。
        if getattr(agent_output, "action", "") == "transfer":
            return None
        return content
    # 其它结构化产出（例如 ``ResearchDigest``）：没有 content 字段，按整体拍平。
    return _as_text(agent_output)


@input_guardrail
def prompt_injection_guardrail(
    context: RunContextWrapper[TaskContext],
    agent: Agent[Any],
    agent_input: str | list[TResponseInputItem],
) -> GuardrailFunctionOutput:
    """入口校验：识别试图覆盖系统指令的输入。

    为什么**默认只记不拦**（``RAG_GUARD_ACTION=warn``）
    --------------------------------------------------
    用户输入与检索资料的危险程度不一样：检索资料是**不可信的外部内容**，
    命中即拦截是合理的；而用户自己说"忽略上面的要求，改成写一首诗"是**合法意图**
    （他就是改主意了）。把用户输入按注入话术硬拦，会把正常用法一起拦掉。

    所以这里复用 RAG 链路的同一个策略开关：只有当部署方已经声明
    ``RAG_GUARD_ACTION=block``（"本服务把注入话术一律视为攻击"）时，入口护栏才
    真的 trip。默认 ``warn`` 下它只记一条 warning + 一个指标，**不影响请求**。

    ⚠️ 真正的注入入口是**检索到的资料**，那道防线在 ``tools.kb_search`` 里
    （所有工具返回值都过 ``guard.wrap_context``）。入口护栏不是主力。

    ⚠️ 扫的是**最后一条用户消息**，不是护栏拿到的整份载荷（载荷含跨轮历史）——
    理由见 ``_latest_user_text`` 的说明，那是接上会话之后最容易变形成"用户被永久拦住"
    的一个点。
    """
    text = _latest_user_text(agent_input)
    result = guard.scan(text)
    action = guard.guard_action() if guard.guard_enabled() else "off"
    tripped = bool(result.hit) and action == "block"
    if result.hit:
        observability.counter("agent_input_scan_total", risk=result.risk, action=action)
        logging.warning(
            "用户输入命中注入特征（risk=%s, action=%s, trip=%s）：%s",
            result.risk, action, tripped, result.matches[:3],
        )
    return GuardrailFunctionOutput(
        output_info={
            "matched": result.matches[:3],
            "risk": result.risk,
            "action": action,
            "chars": len(text),
        },
        tripwire_triggered=tripped,
    )


@output_guardrail
def citation_traceability_guardrail(
    context: RunContextWrapper[TaskContext],
    agent: Agent[Any],
    agent_output: Any,
) -> GuardrailFunctionOutput:
    """出口校验：答复里出现的引用编号必须**真的检索到过**。

    "没检索过却带引用"是最典型的编造（幻觉），而且用确定性规则就能判 ——
    不值得花一次模型调用。判据直接复用 ``src/rag/citation_check.py`` 的三层判定，
    只有第 1 层（``unknown_refs``，编号不可追溯）会让 ``block_worthy`` 为真。

    为什么这里比 RAG 链路**更严**（RAG 链路只"扣下引用、保留答案"）
    --------------------------------------------------------------
    因为 RAG 链路是**流式**的：token 已经吐给用户了，收不回来，只能退而求其次
    "把引用列表扣下不下发"。而 agent 链路**不流式**（产出是整段返回的），
    所以护栏能在交付前拦下整条产出 —— 这正是把护栏放在出口的价值。
    两条链路的差别来自"能不能收回"，不是判据松紧不同。

    注意判据的边界：这里只能证明"编号在本轮检索结果里出现过"，证明不了
    "那句话真被该片段支撑"。后者需要判断力，本文件刻意不覆盖（见模块 docstring）。

    ⚠️ 这一步没有交付物时（``_deliverable`` 返回 None）**直接放行、且不计数** ——
    详见 ``_deliverable`` 的说明，最容易踩的是"把交接说明当成交付物来判"。
    """
    text = _deliverable(agent_output)
    if text is None:
        return GuardrailFunctionOutput(
            output_info={"status": "no_deliverable"}, tripwire_triggered=False
        )
    ctx = context.context if isinstance(context.context, TaskContext) else TaskContext()
    verdict = citation_check.verify(text, ctx.citations, docs_dir=ctx.docs_dir or None)
    unknown = [e for e in verdict.get("hard_errors", []) if e == "unknown_refs"]

    if verdict.get("status") not in (None, "no_answer"):
        observability.counter(
            "agent_output_check_total",
            status=str(verdict.get("status") or "unknown"),
            agent=getattr(agent, "name", ""),
        )
    return GuardrailFunctionOutput(
        output_info={
            "status": verdict.get("status"),
            "refs": verdict.get("refs", []),
            "citations": verdict.get("citations", 0),
            "detail": verdict.get("detail", ""),
        },
        tripwire_triggered=bool(unknown) and bool(verdict.get("block_worthy")),
    )


@output_guardrail
def length_guardrail(
    context: RunContextWrapper[TaskContext],
    agent: Agent[Any],
    agent_output: Any,
) -> GuardrailFunctionOutput:
    """出口校验：交付物长度上限。

    存在的意义不只是"防超长"，更是证明**护栏可以廉价**：一条纯函数护栏、
    零模型调用、微秒级，就能把一类明显的坏产出挡在交付之外。如果每条护栏都要
    调一次模型，"三道护栏"就会变成三倍成本 —— 那是设计问题，不是取舍。

    同样要在"这一步没有交付物"时跳过（``_deliverable`` 返回 None）：否则判的是
    结构化判定的 repr，或者把一次**转交**当成一次超长交付。
    """
    text = _deliverable(agent_output)
    if text is None:
        return GuardrailFunctionOutput(
            output_info={"status": "no_deliverable"}, tripwire_triggered=False
        )
    tripped = len(text) > MAX_ANSWER_CHARS
    if tripped:
        observability.counter("agent_output_check_total", status="too_long",
                              agent=getattr(agent, "name", ""))
    return GuardrailFunctionOutput(
        output_info={"chars": len(text), "limit": MAX_ANSWER_CHARS},
        tripwire_triggered=tripped,
    )
