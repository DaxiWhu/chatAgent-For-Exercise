"""节点与路由单元测试：不依赖真实 LLM / 数据库。"""
import src.graph as g
from src.graph import (
    route_review,
    after_human,
    evaluate,
    generate,
    ReviewResult,
)
from unittest.mock import MagicMock
from langchain_core.messages import AIMessage


def make_state(**kw):
    base = {
        "messages": [], "output": "", "is_need_evaluate": False,
        "score": 0, "feedback": "", "human_action": "",
        "iteration": 0, "feedback_source": "", "force_exit": False,
    }
    base.update(kw)
    return base


# ---------------- route_review 分支 ----------------
def test_route_review_force_exit():
    assert route_review(make_state(force_exit=True)) == "end"

def test_route_review_no_eval():
    assert route_review(make_state(is_need_evaluate=False)) == "end"

def test_route_review_iter_cap():
    assert route_review(make_state(is_need_evaluate=True, iteration=10, score=50)) == "end"

def test_route_review_high_score():
    assert route_review(make_state(is_need_evaluate=True, iteration=0, score=90)) == "end"

def test_route_review_regenerate():
    assert route_review(make_state(is_need_evaluate=True, iteration=0, score=70)) == "regenerate"

def test_route_review_human():
    assert route_review(make_state(is_need_evaluate=True, iteration=0, score=50)) == "human"


# ---------------- after_human ----------------
def test_after_human_accept():
    assert after_human(make_state(human_action="accept")) == "accept"

def test_after_human_revise():
    assert after_human(make_state(human_action="revise")) == "revise"


# ---------------- evaluate ----------------
def test_evaluate_success():
    m = MagicMock()
    m.with_structured_output.return_value.invoke.return_value = ReviewResult(
        is_need_evaluate=True, score=85, feedback="好"
    )
    g.MODEL = m
    out = evaluate(make_state(output="草稿"))
    assert out["score"] == 85
    assert out["is_need_evaluate"] is True
    assert "force_exit" not in out  # 成功路径不应带 force_exit

def test_evaluate_degrade_on_error():
    """模型调用/解析彻底失败 → 降级 force_exit，且不再抛异常（图不崩）。"""
    m = MagicMock()
    m.with_structured_output.return_value.invoke.side_effect = Exception("boom")
    g.MODEL = m
    out = evaluate(make_state(output="草稿"))
    assert out["force_exit"] is True
    assert out["output"] == "服务出现问题请稍后重试"


# ---------------- generate ----------------
def test_generate_first_round_writes_output():
    m = MagicMock()
    m.invoke.return_value = AIMessage(content="首轮草稿")
    g.MODEL = m
    out = generate(make_state(messages=[{"role": "user", "content": "写文章"}], iteration=0))
    assert out["output"] == "首轮草稿"
    assert out["iteration"] == 1
