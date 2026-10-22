"""结构化输出类型 —— 官方 ``Agent(output_type=...)`` 的载荷。

判据只有一条：**这段产出会被"代码"消费吗？**

* 会被代码消费（汇总、渲染、评审、校验）⇒ **必须结构化**。结构化是"代码可判"
  的前提：官方 ``llm_as_a_judge.py`` 的评审之所以能驱动 ``while`` 循环，就是因为它
  输出 ``EvaluationFeedback{feedback, score}`` 而不是一段散文 —— 否则"要不要再来
  一轮"只能靠读自然语言判断，判据会随措辞漂移。
* 直接给用户看（文学创作、前台答复）⇒ **不要结构化**。把内容压成字段只会限制它，
  而且用户要的是正文，不是 JSON。

⚠️ 2026-10-20：``FrontlineDecision`` 已删。它存在的唯一理由是"图要按结构化字段
决定下一条边"；图没了，handoff 由 SDK 在运行内部完成，路由判据变成
``result.last_agent``（既成事实，不是模型的自述）⇒ 那个类型失去了全部读取方。
**不要再给前台 agent 加 ``output_type``** —— 结构化对象会被当成给用户的答复交付出去。

字段级约束（2026-10-22 补）
--------------------------
"结构合法"不等于"内容可用"。只声明 ``str`` 时，``source="来源一"``、``title=""``、
``slides=[]`` 全都算合法 —— 于是：

* 出处写成"来源一" ⇒ 出口护栏判不出它指哪一条，结论无法回溯，**但整轮是成功的**；
* ``slides=[]`` ⇒ 渲染出一份空 PPT，一直走到用户打开才发现。

所以给"取值空间很小"的字段加 ``min_length`` / ``pattern``：**把这类失败从"用户
打开时才看到"改到"模型输出时就被拒"**。pydantic 校验失败会变成一次明确的解析错误，
模型（或它上一层的重试）当场就能改。

⚠️ 但约束**只加在"合法取值空间确实很小"的地方**，不要为了"更严格"到处加：
``ResearchDigest.findings`` 刻意**不加** ``min_length`` —— "什么都没检索到"是合法的
结果，强制至少一条会逼模型编一条出来，那比空列表坏得多。
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

_CITATION_RE = r"^\s*[\[\d]"
"""出处编号的**开头**判据：只允许以数字或 ``[`` 起头。

为什么只判开头、不判完整格式：完整正则（``^\\[\\d+\\]$``）会把 "1,2"、"见 [1]"
这类合理写法一起拒掉，而它们对下游仍然可用（护栏按编号查得到）。真正要拦的是
**以文字开头**的那种（``"来源一"``）—— 那种一定解析不出编号，而它看起来完全正常。

pattern 语义是 ``search``（不是 ``fullmatch``），所以必须带 ``^`` 锚定，否则
``"来源[1]"`` 也会通过。
"""


class Finding(BaseModel):
    """一条可核对的结论。"""

    claim: str = Field(min_length=1, description="结论本身，一句话")
    evidence: str = Field(min_length=1, description="支撑该结论的原文片段（照抄，不要改写）")
    source: str = Field(
        min_length=1,
        pattern=_CITATION_RE,
        description="出处编号**只填编号本身**，形如 [1]；多条写 [1][2]。不要写「见…」或文字说明",
    )


class ResearchDigest(BaseModel):
    """检索取证的结果 —— 给主 agent 看的材料，**不是**给用户看的答复。"""

    question: str = Field(min_length=1, description="被查证的问题")
    findings: list[Finding] = Field(
        default_factory=list,
        description="逐条结论。什么都没查到时留空数组 —— 不要为了凑数编结论",
    )
    citations: list[str] = Field(default_factory=list, description="本轮真正检索到的出处编号")
    confidence: Literal["high", "medium", "low"] = "medium"


class Slide(BaseModel):
    """一页大纲。"""

    title: str = Field(min_length=1)
    bullets: list[str] = Field(default_factory=list)
    speaker_notes: str | None = None


class SlideDeck(BaseModel):
    """PPT 的结构化大纲。

    渲染成文件是**确定性步骤**，因此刻意不在这里做 —— 没有任何模型需要"决定要不要
    渲染"。渲染由 ``runner`` 在代码里调用 ``render_deck`` 完成。
    """

    title: str = Field(min_length=1)
    subtitle: str = ""
    slides: list[Slide] = Field(
        default_factory=list,
        min_length=1,
        description="至少一页。空数组会渲染出一份空 PPT，所以这里直接拒",
    )


class ImageBrief(BaseModel):
    """一张插图的生成参数与落地路径。"""

    prompt: str = Field(min_length=1, description="改写后的具体画面描述")
    style: str = "flat"
    asset_path: str = ""


class EvaluationFeedback(BaseModel):
    """评审结论。字段刻意与官方 ``llm_as_a_judge.py`` 对齐。"""

    feedback: str = Field(description="可执行的改进项：指出哪一处、改成什么")
    score: Literal["pass", "needs_improvement", "fail"] = Field(
        description="是否可交付：pass / needs_improvement / fail"
    )
