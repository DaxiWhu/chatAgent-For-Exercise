"""结构化输出类型 —— 官方 ``Agent(output_type=...)`` 的载荷。

判据只有一条：**这段产出会被"代码"消费吗？**

* 会被代码消费（汇总、渲染、评审、校验）⇒ **必须结构化**。结构化是"代码可判"
  的前提：官方 ``llm_as_a_judge.py`` 的评审之所以能驱动 ``while`` 循环，就是因为它
  输出 ``EvaluationFeedback{feedback, score}`` 而不是一段散文 —— 否则"要不要再来
  一轮"只能靠读自然语言判断，判据会随措辞漂移。
* 直接给用户看（文学创作）⇒ **不要结构化**。把作品压成字段只会限制它，
  而且用户要的是正文，不是 JSON。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

__all__ = [
    "EvaluationFeedback",
    "Finding",
    "FrontlineDecision",
    "ImageBrief",
    "ResearchDigest",
    "Slide",
    "SlideDeck",
]


class FrontlineDecision(BaseModel):
    """前台这一步的判定 —— 图据它决定下一条边（2026-10-03）。

    为什么必须是结构化产出，而不是"用一句自然语言说明我移交了"
    ----------------------------------------------------------
    路由是**代码**要判的事。判据一旦落在自然语言上就会随措辞漂移 —— 官方
    ``llm_as_a_judge.py`` 的评审之所以能驱动循环，正是因为它输出
    ``{feedback, score}`` 而不是一段散文。

    ⚠️ 为什么不再用官方 ``handoff``（改这条时先读这段）
    --------------------------------------------------
    2026-10-03 起 agent 链路**拆成了多个图节点**，节点边界就是续跑边界。
    而 SDK 的 handoff 是"一个节点内部把人换完再继续跑"（``run.py`` 里换人后是
    ``continue`` 而不是 ``return``）—— 节点里一旦带 handoff，节点边界就不存在了，
    拆了等于没拆。所以"该转给谁"改成由图读这个结构化字段来跳节点。
    取舍与实测见 ``DESIGN_agent_architecture.md``。

    与 ``handoff_description`` 的差别：那个字段是给**模型**看的工具描述
    （由 ``Handoff.default_tool_description`` 拼出来），本类型是给**代码**判的。
    """

    action: Literal["reply", "transfer"] = Field(
        description="reply = 这一步直接答复用户；transfer = 把对话交给文学创作专家"
    )
    content: str = Field(
        description=(
            "给用户的正文。action=reply 时是完整答复（必填）；"
            "action=transfer 时写一句交接说明，这一句**不会**出现在给用户的答复里"
        )
    )


class Finding(BaseModel):
    """一条可核对的结论。"""

    claim: str = Field(description="结论本身，一句话")
    evidence: str = Field(description="支撑该结论的原文片段（照抄，不要改写）")
    source: str = Field(description="出处编号，形如 [1]")


class ResearchDigest(BaseModel):
    """检索取证的结果 —— 给主 agent 看的材料，**不是**给用户看的答复。"""

    question: str = Field(description="被查证的问题")
    findings: list[Finding] = Field(default_factory=list, description="逐条结论")
    citations: list[str] = Field(default_factory=list, description="本轮真正检索到的出处编号")
    confidence: Literal["high", "medium", "low"] = "medium"


class Slide(BaseModel):
    """一页大纲。"""

    title: str
    bullets: list[str] = Field(default_factory=list)
    speaker_notes: str | None = None


class SlideDeck(BaseModel):
    """PPT 的结构化大纲。

    渲染成文件是**确定性步骤**，因此刻意不在这里做 —— 没有任何模型需要"决定要不要
    渲染"。渲染由 ``runner`` 在代码里调用 ``render_deck`` 完成。
    """

    title: str
    subtitle: str = ""
    slides: list[Slide] = Field(default_factory=list)


class ImageBrief(BaseModel):
    """一张插图的生成参数与落地路径。"""

    prompt: str = Field(description="改写后的具体画面描述")
    style: str = "flat"
    asset_path: str = ""


class EvaluationFeedback(BaseModel):
    """评审结论。字段刻意与官方 ``llm_as_a_judge.py`` 对齐。"""

    feedback: str = Field(description="可执行的改进项：指出哪一处、改成什么")
    score: Literal["pass", "needs_improvement", "fail"] = Field(
        description="是否可交付：pass / needs_improvement / fail"
    )
