"""校验原语 —— **校验是 guardrail，不是 agent**。

这是本示例与"什么都做成子 agent"最本质的分歧点，也是官方**给了独立原语**的地方：

* ``@input_guardrail``：在链上**第一个** agent 之前跑，默认与模型**并行**（乐观执行）；
  触发 tripwire 会立刻中止整个 run；
* ``@output_guardrail``：在产出**最终输出的那个** agent 之后跑，必定**串行**；
  被拒的输出不会写进 session。

由此推出的两条工程结论：

1. **校验不该做成子 agent。** 做成 agent 就变成"主 agent 的一个工具"，
   于是"要不要校验"落到了被校验者手里 —— 它会在对自己不利时跳过。
   官方用独立原语把这件事放在模型的选择范围之外。
2. **判据要分级。** 能用确定性规则判的（正则、编号可追溯、长度、格式）一律用纯函数，
   不花一次模型调用；只有"这句话有没有被资料支撑"这类**需要判断力**的，
   才按官方 ``output_guardrails.py`` 的做法在里面套一个轻量 agent。

本文件的四个判据全部是确定性的（因此离线可跑、不占用 ScriptedModel 的调用序列）。
"""

from __future__ import annotations

import re
from typing import Any

from agents import (
    Agent,
    GuardrailFunctionOutput,
    RunContextWrapper,
    TResponseInputItem,
    input_guardrail,
    output_guardrail,
)

from .context import TaskContext

__all__ = [
    "citation_traceability_guardrail",
    "length_guardrail",
    "prompt_injection_guardrail",
]

_INJECTION = re.compile(
    r"忽略(以上|之前|上面|前面)的?(所有)?(指令|规则|设定)"
    r"|ignore\s+(all\s+)?(previous|above|prior)\s+instructions"
    r"|you\s+are\s+now\s+(a|an)\s+",
    re.IGNORECASE,
)
_CITATION = re.compile(r"[\[【［](\d+)[\]】］]")
_MAX_ANSWER_CHARS = 8000


def _flatten(payload: str | list[TResponseInputItem]) -> str:
    if isinstance(payload, str):
        return payload
    chunks: list[str] = []
    for item in payload:
        if isinstance(item, dict):
            content = item.get("content")
            if isinstance(content, str):
                chunks.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        chunks.append(part["text"])
    return "\n".join(chunks)


@input_guardrail
def prompt_injection_guardrail(
    context: RunContextWrapper[TaskContext],
    agent: Agent[Any],
    agent_input: str | list[TResponseInputItem],
) -> GuardrailFunctionOutput:
    """入口校验：拦截试图覆盖系统指令的输入。

    只对链上第一个 agent 生效 —— handoff 之后的专家**不会**再跑一遍这条护栏，
    所以"入口护栏"的语义是"这条链路入口可信"，而不是"每一次模型调用都可信"。
    """
    text = _flatten(agent_input)
    match = _INJECTION.search(text)
    return GuardrailFunctionOutput(
        output_info={"matched": match.group(0) if match else None, "chars": len(text)},
        tripwire_triggered=match is not None,
    )


@output_guardrail
def citation_traceability_guardrail(
    context: RunContextWrapper[TaskContext],
    agent: Agent[Any],
    agent_output: Any,
) -> GuardrailFunctionOutput:
    """出口校验：答复里出现的引用编号必须在**本轮真的检索到过**。

    "没检索过却带引用"是典型的编造引用（幻觉），且用确定性规则就能判 —— 不值得花一次模型调用。

    ⚠️ 判据的边界：这里只能证明"编号在本轮检索结果里出现过"，
    证明不了"那句话真被该片段支撑"。后者需要判断力，应另建一条 LLM 出口护栏
    （官方 ``output_guardrails.py`` 的形态），不要混在这一条里假装都覆盖了。
    """
    text = agent_output if isinstance(agent_output, str) else str(agent_output or "")
    cited = {f"[{num}]" for num in _CITATION.findall(text)}

    digest = context.context.recall("research") if context.context else None
    known: set[str] = set()
    if digest is not None:
        for index in range(1, len(getattr(digest, "findings", []) or []) + 1):
            known.add(f"[{index}]")

    dangling = sorted(cited - known)
    return GuardrailFunctionOutput(
        output_info={"cited": sorted(cited), "known": sorted(known), "dangling": dangling},
        tripwire_triggered=bool(dangling),
    )


@output_guardrail
def length_guardrail(
    context: RunContextWrapper[TaskContext],
    agent: Agent[Any],
    agent_output: Any,
) -> GuardrailFunctionOutput:
    """出口校验：答复长度上限。纯确定性，用来演示"护栏可以是廉价的"。"""
    text = agent_output if isinstance(agent_output, str) else str(agent_output or "")
    return GuardrailFunctionOutput(
        output_info={"chars": len(text), "limit": _MAX_ANSWER_CHARS},
        tripwire_triggered=len(text) > _MAX_ANSWER_CHARS,
    )
