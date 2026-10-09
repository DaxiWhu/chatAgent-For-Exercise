"""代码编排（code-first orchestration）—— 官方 ``llm_as_a_judge.py`` 的形态。

官方把编排分成并列的两种，没有优劣：

* **LLM 决策式**：agent = instructions + tools + handoffs，模型自己决定下一步；
* **代码式**：你在自己的代码里做分类、串联、``while`` + 评审、``asyncio.gather`` 并行。

本文件是第二种。三处必须照抄官方的地方：

1. **``current_agent = result.last_agent``**
   官方 airline 的主循环这么写。含义是 **handoff 跨轮次持续**：用户第二轮说话时，
   接话的是上一次的 active agent，不会自动回到前台。漏掉这一行，
   "handoff 后继续打磨"就会每次都被打回前台。
2. **反馈伪装成 user 消息回灌**
   ``input_items = 生成结果 + [{"role": "user", "content": f"Feedback: {...}"}]``。
   注意取的是**生成侧**的 ``to_input_list()``，不是评审侧的 —— 不把评审的内部推理
   灌进创作上下文。
3. **循环在代码里，不在模型的选择范围内**
   评估 agent 既不在 ``frontline_agent.tools`` 也不在 ``handoffs`` 里。
   这是本设计里唯一能保证"评审一定会跑"的办法。

另外，**渲染 PPT / 落盘资产是确定性步骤，放在这里（代码）而不是做成工具**：
没有任何模型需要"决定要不要渲染"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agents import (
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    OutputGuardrailTripwireTriggered,
    RunConfig,
    Runner,
)

from .context import TaskContext
from .evaluator import evaluator_agent
from .frontline import frontline_agent
from .guards import citation_traceability_guardrail, length_guardrail
from .tools import render_slide_deck
from .types import EvaluationFeedback

__all__ = ["MAX_ATTEMPTS", "MAX_TURNS", "TaskOutcome", "build_run_config", "run_task"]

MAX_ATTEMPTS = 3
"""评估回灌的最大轮数。**必须和 evaluator 的 instructions 里写的轮数一致** ——
停止条件写在 prompt 里（官方做法），代码只是它的上限兜底。"""

MAX_TURNS = 12
"""单次 ``Runner.run`` 内允许的 LLM 调用次数（官方语义：``max_turns`` 单位是 LLM 调用，不是对话轮次）。"""


@dataclass
class TaskOutcome:
    answer: str
    attempts: int
    final_agent: str
    passed: bool
    exit_reason: str
    assets: list[str] = field(default_factory=list)
    delivered: dict[str, Any] = field(default_factory=dict)
    guardrail: str | None = None
    """被哪条护栏拦下（``exit_reason`` 为 ``*_guardrail`` 时有值）。"""


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

    ``RunConfig.model`` 优先级**高于** ``Agent.model``（官方三层优先级里最高的一层），
    而且 ``as_tool()`` 的嵌套 run 在没有显式传 ``run_config`` 时会**继承父 run 的**
    （SDK 源码：``resolved_run_config = run_config or context.run_config``）。
    ⇒ 这里挂一次，前台、专家、嵌套 run 全部用同一个模型。

    ``output_guardrails`` 挂在 RunConfig 上（而不是某个 agent 上）：
    出口护栏只对"产出最终输出的那个 agent"生效，而 handoff 之后产出最终输出的
    是文学专家 —— 挂在 agent 级就会漏掉它。
    """
    return RunConfig(
        model=model,
        workflow_name="sub_agents",
        output_guardrails=[citation_traceability_guardrail, length_guardrail],
    )


def _flush_deterministic_steps(ctx: TaskContext, rendered: set[str]) -> None:
    """把所有"确定性步骤"在这里一次性做完：渲染大纲、收集资产。"""
    deck = ctx.recall("slides")
    if deck is not None and "slides" not in rendered:
        path = render_slide_deck(deck.model_dump_json())
        ctx.assets.append(path)
        rendered.add("slides")


async def run_task(
    task: str,
    *,
    model: Any | None = None,
    session: Any | None = None,
    context: TaskContext | None = None,
) -> TaskOutcome:
    """跑一次完整任务：前台 → （handoff / as_tool）→ 代码评审循环。"""
    ctx = context if context is not None else TaskContext()
    run_config = build_run_config(model)
    rendered: set[str] = set()

    input_items: list[Any] = [{"role": "user", "content": task}]
    current_agent = frontline_agent
    answer = ""
    attempts = 0

    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempts = attempt
        try:
            result = await Runner.run(
                current_agent,
                input_items,
                context=ctx,
                session=session,
                run_config=run_config,
                max_turns=MAX_TURNS,
            )
        except OutputGuardrailTripwireTriggered as exc:
            # 出口护栏拦下的输出**不会**成为 delivered —— 这是官方语义（被拒的候选输出不落库）。
            return TaskOutcome(
                answer="",
                attempts=attempt,
                final_agent=current_agent.name,
                passed=False,
                exit_reason="output_guardrail",
                assets=list(ctx.assets),
                delivered=dict(ctx.delivered),
                guardrail=_guardrail_name(exc),
            )
        except InputGuardrailTripwireTriggered as exc:
            return TaskOutcome(
                answer="",
                attempts=attempt,
                final_agent=current_agent.name,
                passed=False,
                exit_reason="input_guardrail",
                assets=list(ctx.assets),
                delivered=dict(ctx.delivered),
                guardrail=_guardrail_name(exc),
            )
        except MaxTurnsExceeded:
            return TaskOutcome(
                answer="",
                attempts=attempt,
                final_agent=current_agent.name,
                passed=False,
                exit_reason="max_turns",
                assets=list(ctx.assets),
                delivered=dict(ctx.delivered),
            )
        answer = str(result.final_output or "")
        current_agent = result.last_agent  # ← 官方 airline 的做法：handoff 跨轮次持续
        _flush_deterministic_steps(ctx, rendered)
        generated_items = result.to_input_list()

        verdict = await Runner.run(
            evaluator_agent,
            generated_items,
            context=ctx,
            run_config=run_config,
            max_turns=MAX_TURNS,
        )
        feedback = verdict.final_output_as(EvaluationFeedback, raise_if_incorrect_type=True)

        if feedback.score == "pass":
            return TaskOutcome(
                answer=answer,
                attempts=attempt,
                final_agent=current_agent.name,
                passed=True,
                exit_reason="passed",
                assets=list(ctx.assets),
                delivered=dict(ctx.delivered),
            )

        # 官方 llm_as_a_judge：反馈伪装成一条用户消息回灌，创作方无需额外状态
        input_items = generated_items + [
            {"role": "user", "content": f"Feedback: {feedback.feedback}"}
        ]

    return TaskOutcome(
        answer=answer,
        attempts=attempts,
        final_agent=current_agent.name,
        passed=False,
        exit_reason="max_attempts",
        assets=list(ctx.assets),
        delivered=dict(ctx.delivered),
    )
