"""评估 agent —— 官方 ``llm_as_a_judge.py`` 形态里的"第二个模型"。

三条与官方逐字对齐的设计（都影响能不能落地，不是风格问题）：

1. **输出结构化** ``EvaluationFeedback{feedback, score}``
   ⇒ 官方原话是让评审给出 ``Literal["pass", "needs_improvement", "fail"]``。
   理由：代码要据此决定"停还是再来一轮"，读自然语言做不到稳定判定。
2. **反馈靠"伪装成用户消息"回灌**，不给创作方加任何额外状态。
   官方做法：``input_items.append({..., "role": "user", "content": f"Feedback: {...}"})``。
   本示例把这一步放在 ``runner.py`` 里（那里是代码编排的位置）。
3. **停止条件写在 instructions 里，不用分数阈值。**
   官方原文：「Never give it a pass on the first try. After 5 attempts, you can give it a
   pass if … — **do not go for perfection**」。
   理由：LLM 评委会持续给出小幅改进建议，用"分数 >= 90 才停"等于永远停不下来。

⚠️ 为什么评审**不能**接成 handoff / ``as_tool``（本示例刻意不做）：
接 handoff ⇒ 评审成为新的 active agent，**它会直接对用户说话**（评审语不是给用户读的），
且 handoff 单向；接 ``as_tool`` ⇒ "要不要评审"落到创作者手里，它会在对自己不利时跳过。
官方选的正是第三种：**把循环放在代码里**。
"""

from __future__ import annotations

from agents import Agent

from .context import TaskContext
from .types import EvaluationFeedback

__all__ = ["evaluator_agent"]

evaluator_agent = Agent[TaskContext](
    name="evaluator",
    instructions=(
        "你是评审。给你一段候选产出与用户要求，你逐条核对是否满足。\n"
        "\n"
        "# 输出\n"
        "只输出结构化结论：一段可执行的改进项 + 一个分数。\n"
        "feedback 必须指出**哪一处**、**改成什么**；不许写「整体不错」「可以更生动」这类评价。\n"
        "\n"
        "# 打分规则\n"
        "- 第一轮不要给 pass，至少指出一条可改进项。\n"
        "- 最多评 3 轮；到第 3 轮时只要没有硬伤（事实错误、答非所问、缺交付物）就给 pass。\n"
        "- 不要追求完美：措辞与风格上的偏好不构成 needs_improvement。\n"
        "\n"
        "# 硬伤（必须给 fail 或 needs_improvement）\n"
        "- 引用编号在本轮检索结果里不存在（编造引用）。\n"
        "- 用户要 A 交付了 B（例如要 PPT 却只给了正文）。\n"
        "- 事实与检索材料矛盾。"
    ),
    output_type=EvaluationFeedback,
)
