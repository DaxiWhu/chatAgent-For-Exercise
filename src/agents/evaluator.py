"""评审 agent —— 由**创作方决定**要不要调用的专家（``as_tool``）。

它是 ``as_tool`` 出来的工具，挂在**产出交付物的 agent** 上（``frontline_agent``
与 ``literary_writer``，见各自的 ``tools``）。**要不要评审由模型自己决定** ——
代码不强制、也不假设它一定评审过。

⚠️ 与官方 ``llm_as_a_judge.py`` 的差别（别再改回去、也别再讲反）
--------------------------------------------------------------
官方那份示例把评审放在**代码的 ``while`` 循环**里：

    while True:
        result = await Runner.run(story_outline_generator, input_items)
        evaluator_result = await Runner.run(evaluator, input_items)   # 每轮必跑
        if result.score == "pass":
            break
        input_items.append({"content": f"Feedback: {result.feedback}", "role": "user"})

evaluator **不在** handoffs / tools 里，"跑不跑评审"是代码定的。本项目按用户
要求改成 ``as_tool``：评审成为一个模型可调用的工具，"跑不跑"由模型定。

由此产生的**口径变化**（代码侧必须知道，否则会误判交付质量）：

* 模型可能**一次都不调**评审 ⇒ 代码不能假设 ``delivered["evaluation"]`` 一定有值。
  ``runner.run_agent`` 因此单独给一档 ``exit_reason="unreviewed"``，而不是把它
  当成"评审通过"。
* 反过来，评审结论**不再**驱动重试循环 —— 没有"回灌 Feedback 再来一轮"了。
  模型要改就自己在同一次 run 里改（这是它自己的选择）。

判据分级仍然照旧：能确定性判的（引用可追溯、长度）走 ``guards.py`` 的护栏，
花模型调用的判断（这段产出够不够好）才来这里。
"""

from __future__ import annotations

from agents import Agent, RunResult, RunResultStreaming

from .context import TaskContext
from .hooks import AGENT_HOOKS
from .types import EvaluationFeedback

__all__ = ["evaluate_draft_tool", "evaluator_agent", "extract_evaluation"]

evaluator_agent = Agent[TaskContext](
    name="evaluator",
    instructions=(
        "你是评审。给你一段候选产出与用户要求，你逐条核对是否满足。\n"
        "\n"
        "# 输出\n"
        "只输出结构化结论：一段可执行的改进项 + 一个分数。\n"
        "feedback 必须指出**哪一处**、**改成什么**；不许写「整体不错」「可以更生动」"
        "这类无法执行的评价。\n"
        "\n"
        "# 打分\n"
        "- pass：没有硬伤，可以交付。\n"
        "- needs_improvement：有明确可改之处。\n"
        "- fail：有硬伤。\n"
        "\n"
        "# 硬伤（必须给 fail 或 needs_improvement）\n"
        "- 引用编号在本轮检索结果里不存在（编造引用）。\n"
        "- 用户要 A 交付了 B（例如要 PPT 却只给了正文）。\n"
        "- 事实与检索材料矛盾。\n"
        "- 用户明确要求过的东西（篇幅、风格、必须包含的要点）没有出现。\n"
        "\n"
        "# 不要追求完美\n"
        "措辞与风格上的偏好不构成 needs_improvement —— 反复要求重写只会让交付无限延迟。"
    ),
    output_type=EvaluationFeedback,
)


async def extract_evaluation(result: RunResult | RunResultStreaming) -> str:
    """``as_tool`` 的输出提取器：把评审结论**登记进共享上下文**。

    为什么必须登记：评审是模型自己决定调不调的，代码要能回答"这次到底评审过没有"
    —— ``runner.run_agent`` 靠它决定 ``exit_reason`` 是 ``passed`` 还是
    ``unreviewed``。只把结论当字符串还给创作方，代码侧就彻底看不见评审发生过。

    返回给创作方的是 JSON 文本（与其它 ``as_tool`` 提取器一致）—— 创作方要据此
    决定改不改；登记进 ``delivered["evaluation"]`` 的那份是**代码**看的。
    """
    verdict = result.final_output_as(EvaluationFeedback, raise_if_incorrect_type=True)
    ctx = result.context_wrapper.context
    if isinstance(ctx, TaskContext):
        ctx.record("evaluation", verdict)
        # 评审**被调了几次**（模型可能改完再评一次）。这是 `review_rounds` 在新形态
        # 下的含义 —— 它以前数的是"代码循环跑了几轮"，现在数"模型叫了几次评审"。
        # 不在这里累加的话，这个字段会永远停在 0，`agent_review_rounds` 指标也就死了。
        ctx.stats.review_rounds += 1
        ctx.notes.append(f"evaluator 回传：{verdict.score}")
    return verdict.model_dump_json(indent=2)


evaluate_draft_tool = evaluator_agent.as_tool(
    "review_draft",
    "评审一段候选产出：逐条核对它是否满足用户要求，给出可执行的改进项与分数。"
    "重要交付物在给用户之前调用它；它的产出是给你自己参考的，不是给用户看的。",
    # ⚠️ hooks 三处都不能省：as_tool 的嵌套 run **不会**继承父 run 的 hooks。
    hooks=AGENT_HOOKS,
    custom_output_extractor=extract_evaluation,
)
