"""结构化输出类型 —— 官方 ``Agent(output_type=...)`` 的载荷。

为什么这里的每个"子 agent"都要有 ``output_type``（文学创作那个除外）：

* 结构化输出是**代码可判**的前提。官方 ``llm_as_a_judge.py`` 的评审之所以能驱动
  ``while`` 循环，就是因为它输出的是 ``EvaluationFeedback{fedback, score}``，
  而不是一段自然语言 —— 否则"要不要再来一轮"只能靠读散文判断；
* 只要是"产出会被**代码**消费"的环节（汇总、渲染、评估、校验），就必须结构化。
  反之，产出**直接给用户看**的环节（文学创作）不该结构化 —— 结构化会把作品
  压成字段，反而限制它。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

__all__ = [
    "EvaluationFeedback",
    "Finding",
    "ImageBrief",
    "ResearchDigest",
    "Slide",
    "SlideDeck",
]


class Finding(BaseModel):
    """一条可核对的结论。"""

    claim: str = Field(description="结论本身，一句话")
    evidence: str = Field(description="支撑该结论的原文片段")
    source: str = Field(description="出处编号，形如 S1")


class ResearchDigest(BaseModel):
    """检索取证的结果 —— 给主 agent 看的材料，不是给用户看的答复。"""

    question: str
    findings: list[Finding]
    citations: list[str] = Field(default_factory=list, description="本轮真正检索到的出处编号")
    confidence: Literal["high", "medium", "low"] = "medium"


class Slide(BaseModel):
    title: str
    bullets: list[str] = Field(default_factory=list)
    speaker_notes: str | None = None


class SlideDeck(BaseModel):
    """PPT 的结构化大纲。渲染成文件是**确定性步骤**，因此不在这里做。"""

    title: str
    subtitle: str = ""
    slides: list[Slide] = Field(default_factory=list)


class ImageBrief(BaseModel):
    """一张插图的生成参数与落地路径。"""

    prompt: str
    style: str = "flat"
    asset_path: str = ""


class EvaluationFeedback(BaseModel):
    """评审结论。字段刻意与官方 ``llm_as_a_judge.py`` 对齐。"""

    feedback: str = Field(description="可执行的改进项：指出哪一处、改成什么")
    score: Literal["pass", "needs_improvement", "fail"]
