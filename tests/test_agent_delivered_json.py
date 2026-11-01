"""产出物（``TaskContext.delivered``）必须是"能过 JSON 的"。

为什么这条必须有
----------------
真跑长任务时约 5% 的检索型运行整轮 `exit_reason=error`，错误是
``Object of type ResearchDigest is not JSON serializable``。追了两次都没抓到栈 ——
因为 ``delivered`` 会被**好几处**各自序列化一次（落库、发事件、trace 导出），
不知道是哪一处先碰上的。

修法是把边界收紧到 ``record()``：存进去之前就转成 dict。本文件守的就是这条边界，
以及它带出的两个**静默**风险：

1. ``getattr(verdict, "score", "")`` 在拿到 dict 之后不会报错，只会**永远返回空串**
   ⇒ 每一次评审都被判成 ``review_failed``，指标上只表现为"通过率 0%"；
2. ``deck.model_dump_json()`` 在拿到 dict 之后会 AttributeError —— 这个会立刻红，
   反而安全。

两者都必须有测试，因为第 1 条在真实使用里**没有任何症状**。
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("agents", reason="openai-agents 未安装 ⇒ agent 链路不参与测试")

from src.agents import context as C  # noqa: E402
from src.agents import runner as R  # noqa: E402
from src.agents.types import (  # noqa: E402
    EvaluationFeedback,
    ImageBrief,
    ResearchDigest,
    Slide,
    SlideDeck,
)


def _ctx() -> C.TaskContext:
    return C.TaskContext()


# --------------------------------------------------------------------------
# 边界：存进去的就是 dict
# --------------------------------------------------------------------------


def test_pydantic_output_is_stored_as_dict():
    ctx = _ctx()
    ctx.record("research", ResearchDigest(question="向量检索怎么切块"))

    assert isinstance(ctx.delivered["research"], dict), "对象不许越过边界"
    assert ctx.delivered["research"]["question"] == "向量检索怎么切块"


def test_all_four_output_kinds_survive_json_dumps():
    """★ 四种产出物一起过一遍真实序列化。

    这条是那次崩溃的最小复现：只要有一个留在 ``delivered`` 里是对象，
    ``json.dumps`` 就会抛 ``TypeError``。
    """
    ctx = _ctx()
    ctx.record("research", ResearchDigest(question="q"))
    ctx.record("slides", SlideDeck(title="标题", slides=[Slide(title="第一页")]))
    ctx.record("illustration", ImageBrief(prompt="一只猫"))
    ctx.record("evaluation", EvaluationFeedback(feedback="再具体些", score="pass"))

    dumped = json.dumps(ctx.delivered, ensure_ascii=False)

    assert "一只猫" in dumped
    assert json.loads(dumped)["evaluation"]["score"] == "pass"


def test_recall_returns_dict_not_object():
    ctx = _ctx()
    ctx.record("research", ResearchDigest(question="q"))

    got = ctx.recall("research")

    assert isinstance(got, dict)
    assert not hasattr(got, "model_dump")


def test_plain_values_pass_through():
    """字符串 / dict 原样通过（``delivery`` 就是直接写字符串的那一类）。"""
    ctx = _ctx()
    ctx.record("delivery", "正文内容")

    assert ctx.delivered["delivery"] == "正文内容"


def test_non_json_native_types_are_converted():
    """★ ``mode="json"`` 不能省：``datetime`` 留在 dict 里只是把崩溃推远一点。

    裸 ``model_dump()`` 会返回 ``datetime`` 对象 —— ``isinstance(..., dict)`` 检查
    照样通过，然后 ``json.dumps`` 在某个更远的地方炸，现场信息更少。
    """
    from datetime import datetime

    from pydantic import BaseModel

    class _Stamped(BaseModel):
        at: datetime

    ctx = _ctx()
    ctx.record("stamped", _Stamped(at=datetime(2026, 11, 1, 12, 0, 0)))

    assert isinstance(ctx.delivered["stamped"]["at"], str)
    json.dumps(ctx.delivered)  # 不抛即通过


# --------------------------------------------------------------------------
# 静默风险：取字段的方式
# --------------------------------------------------------------------------


def test_verdict_pass_is_recognized():
    """★ 这里以前是 ``getattr(verdict, "score", "")`` —— 拿到 dict 后**静默**返回空串。"""
    ctx = _ctx()
    ctx.record("evaluation", EvaluationFeedback(feedback="挺好的", score="pass"))

    assert R._verdict_of(ctx) == "passed"


def test_verdict_needs_improvement_is_not_passed():
    ctx = _ctx()
    ctx.record("evaluation", EvaluationFeedback(feedback="改改", score="needs_improvement"))

    assert R._verdict_of(ctx) == "review_failed"


def test_no_review_means_unreviewed():
    """没评审 ≠ 评审通过。代码不许替模型宣布"这份审过了"。"""
    assert R._verdict_of(_ctx()) == "unreviewed"


def test_score_is_read_from_dict():
    """★ ``last_score`` 与 ``_verdict_of`` 共用同一个取法，一起守。

    两处各写一遍时，"一处改成 dict、另一处还在 getattr" 几乎必然发生 ——
    那种错不报错，只让得分永远是空串。
    """
    ctx = _ctx()
    ctx.record("evaluation", EvaluationFeedback(feedback="改改", score="needs_improvement"))

    assert R._score_of(ctx) == "needs_improvement"


def test_score_is_empty_without_review():
    assert R._score_of(_ctx()) == ""


# --------------------------------------------------------------------------
# 确定性步骤：拿 dict 也要能渲染
# --------------------------------------------------------------------------


def test_slide_deck_renders_from_dict(tmp_path):
    """★ 这条覆盖 ``deck.model_dump_json()`` 的改法。

    如果 ``recall`` 返回 dict 而这里仍调 ``model_dump_json()``，
    会 AttributeError —— 立刻红，属于"好"的失败方式。
    """
    ctx = _ctx()
    ctx.asset_dir = tmp_path
    ctx.record("slides", SlideDeck(title="季度汇报", slides=[Slide(title="第一页")]))

    R._flush_deterministic_steps(ctx)

    assert len(ctx.assets) == 1
    assert "季度汇报" in (tmp_path / "deck.md").read_text(encoding="utf-8")


def test_rendering_is_idempotent(tmp_path):
    """跑两次只该产生一份资产（渲染没有"多渲染几次更好"这回事）。"""
    ctx = _ctx()
    ctx.asset_dir = tmp_path
    ctx.record("slides", SlideDeck(title="标题"))

    R._flush_deterministic_steps(ctx)
    R._flush_deterministic_steps(ctx)

    assert len(ctx.assets) == 1
