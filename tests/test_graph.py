"""整图集成测试：覆盖 RAG 注入、人工审核回路、直接结束、节点降级不崩。"""
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from src.graph import build_graph, ReviewResult


def _make_model(score=50, side_effect=None):
    m = MagicMock()
    m.invoke.return_value = AIMessage(content="这是草稿内容。")
    struct = m.with_structured_output.return_value
    if side_effect is not None:
        struct.invoke.side_effect = side_effect
    else:
        struct.invoke.return_value = ReviewResult(
            is_need_evaluate=True, score=score, feedback="建议修改"
        )
    return m


class _HitRetriever:
    def search(self, query, top_k=3):
        return [({"source": "intro.txt", "text": "梧州特产有六堡茶。"}, 0.9)]


class _BoomRetriever:
    def search(self, query, top_k=3):
        raise RuntimeError("index broken")


def test_full_graph_reaches_human_then_accept(graph):
    """mock model 返回 score=50 → 进 human_review → interrupt → resume accept → END。"""
    cfg = {"configurable": {"thread_id": "t_human"}}
    try:
        graph.invoke({"messages": [AIMessage(content="写一段文案")]}, config=cfg)
    except GraphInterrupt:
        pass  # 进入 human_review 后暂停，符合预期

    result = graph.invoke(
        Command(resume={"action": "accept", "feedback": None}), config=cfg
    )
    assert result["human_action"] == "accept"


def test_full_graph_high_score_ends(mock_model, fake_retriever):
    """score=90 >= HIGH → route_review 直接 end，不进人工回路。"""
    m = _make_model(score=90)
    g = build_graph(model=m, retriever=fake_retriever)
    cfg = {"configurable": {"thread_id": "t_high"}}
    result = g.invoke({"messages": [AIMessage(content="写文章")]}, config=cfg)
    assert result["score"] == 90


def test_full_graph_degrade_no_crash(mock_model, fake_retriever):
    """evaluate 彻底失败 → force_exit → route_review 返回 end，整图不崩。"""
    m = _make_model(side_effect=Exception("boom"))
    g = build_graph(model=m, retriever=fake_retriever)
    cfg = {"configurable": {"thread_id": "t_degrade"}}
    result = g.invoke({"messages": [AIMessage(content="写文章")]}, config=cfg)
    assert result["force_exit"] is True
    assert result["output"] == "服务出现问题请稍后重试"


def test_full_graph_retrieve_writes_context(mock_model):
    """retrieve 命中时，参考资料应写进 state["context"]。"""
    g = build_graph(model=_make_model(score=90), retriever=_HitRetriever())
    cfg = {"configurable": {"thread_id": "t_ctx"}}
    result = g.invoke({"messages": [AIMessage(content="梧州有什么特产")]}, config=cfg)
    assert "六堡茶" in result["context"]


def test_full_graph_no_docs_gives_empty_context(mock_model, fake_retriever):
    """知识库未配置 / 无召回 → context 为空，主流程照常走完。"""
    g = build_graph(model=_make_model(score=90), retriever=fake_retriever)
    cfg = {"configurable": {"thread_id": "t_nodoc"}}
    result = g.invoke({"messages": [AIMessage(content="随便问")]}, config=cfg)
    assert result["context"] == ""
    assert result["score"] == 90


def test_full_graph_retrieve_error_does_not_crash(mock_model):
    """检索器抛异常 → 图仍应完整跑完（降级为空上下文）。"""
    g = build_graph(model=_make_model(score=90), retriever=_BoomRetriever())
    cfg = {"configurable": {"thread_id": "t_boom"}}
    result = g.invoke({"messages": [AIMessage(content="随便问")]}, config=cfg)
    assert result["context"] == ""
    assert result["score"] == 90
