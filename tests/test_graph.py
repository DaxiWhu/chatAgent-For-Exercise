"""整图集成测试：覆盖 RAG 注入、人工审核回路、直接结束、节点降级不崩。

本文件的用例统一走**异步 API**（`ainvoke` / `asyncio.run`），这是**省事**的选择而非
硬性要求：agent 链路拆成 `frontline` / `literary` 两个节点后，这两个节点都是
`async def`，异步入口两条分支都能跑，同步入口只在 `route=rag` 分支上可行。

⚠️ 口径在 2026-10-02 **反转**过，别照旧笔记理解：此前 `graph.dispatch` 是 `async def`
（要 `await` 一次内容分诊），因此"整个图只能用异步 API"；现在入口只做可用性探测、
**不含 await**，改成了同步函数。详见 `tests/test_agents_route.py` 的文件头。

测试环境未装配 agent 运行时（`runtime.is_ready()` 为 False），所以 `dispatch` 走的是
`runtime_not_ready` 降级分支，**不会真的发模型请求**，断言口径与改造前一致。
"""
import asyncio
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from src.graph import ReviewResult, build_graph


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


def test_dispatch_is_a_pure_sync_probe():
    """★ 入口节点必须是**同步**的 —— 语义在这一轮发生了反转。

    改造前：`dispatch` 要 `await` 一次内容分诊（"这次要一份作品、还是一段说明"），
    所以**必须**是 `async def`；当时的守卫写的是"改回 `def` 会静默失效"。

    改造后：入口只回答"agent 链路现在能不能用"，**一次模型调用都不花**
    （`tests/test_agents_route.py::test_dispatch_sends_every_request_to_the_agent_link`
    用一个空脚本证明这一点：偷偷调模型就会因脚本用尽而失败），因此**必须**是 `def`。

    于是这条守卫现在防的是**反向**回归：谁把入口改回 `async def`，几乎一定意味着
    "内容分诊"又被塞了回来 —— 而那是被明确否掉的形态（官方
    `examples/customer_service` 的入口 agent 自己就是分诊者，代码里不存在第二层内容判定）。

    ⚠️ 不要再断言"整图只能走异步 API"：入口变同步后，`route=rag` 分支整条已经能同步
    跑通（实测通过），那种断言会把**正常状态**判红。
    """
    import inspect

    from src import graph as G

    assert not inspect.iscoroutinefunction(G.dispatch), (
        "graph.dispatch 又变成 async 了 —— 入口是不是被重新塞进了「内容分诊」那次模型调用？"
        "所有请求都必须直接进 agent 链路，由前台自己决定叫哪个帮手。"
    )


def test_full_graph_reaches_human_then_accept(graph):
    """mock model 返回 score=50 → 进 human_review → interrupt → resume accept → END。"""
    cfg = {"configurable": {"thread_id": "t_human"}}
    try:
        asyncio.run(
            graph.ainvoke({"messages": [AIMessage(content="写一段文案")]}, config=cfg)
        )
    except GraphInterrupt:
        pass  # 进入 human_review 后暂停，符合预期

    result = asyncio.run(
        graph.ainvoke(Command(resume={"action": "accept", "feedback": None}), config=cfg)
    )
    assert result["human_action"] == "accept"


def test_full_graph_high_score_ends(mock_model, fake_retriever):
    """score=90 >= HIGH → route_review 直接 end，不进人工回路。"""
    m = _make_model(score=90)
    g = build_graph(model=m, retriever=fake_retriever)
    cfg = {"configurable": {"thread_id": "t_high"}}
    result = asyncio.run(g.ainvoke({"messages": [AIMessage(content="写文章")]}, config=cfg))
    assert result["score"] == 90


def test_full_graph_degrade_no_crash(mock_model, fake_retriever):
    """evaluate 彻底失败 → force_exit → route_review 返回 end，整图不崩。"""
    m = _make_model(side_effect=Exception("boom"))
    g = build_graph(model=m, retriever=fake_retriever)
    cfg = {"configurable": {"thread_id": "t_degrade"}}
    result = asyncio.run(g.ainvoke({"messages": [AIMessage(content="写文章")]}, config=cfg))
    assert result["force_exit"] is True
    assert result["output"] == "服务出现问题请稍后重试"


def test_full_graph_retrieve_writes_context(mock_model):
    """retrieve 命中时，参考资料应写进 state["context"]。"""
    g = build_graph(model=_make_model(score=90), retriever=_HitRetriever())
    cfg = {"configurable": {"thread_id": "t_ctx"}}
    result = asyncio.run(
        g.ainvoke({"messages": [AIMessage(content="梧州有什么特产")]}, config=cfg)
    )
    assert "六堡茶" in result["context"]


def test_full_graph_no_docs_gives_empty_context(mock_model, fake_retriever):
    """知识库未配置 / 无召回 → context 为空，主流程照常走完。"""
    g = build_graph(model=_make_model(score=90), retriever=fake_retriever)
    cfg = {"configurable": {"thread_id": "t_nodoc"}}
    result = asyncio.run(g.ainvoke({"messages": [AIMessage(content="随便问")]}, config=cfg))
    assert result["context"] == ""
    assert result["score"] == 90


def test_full_graph_retrieve_error_does_not_crash(mock_model):
    """检索器抛异常 → 图仍应完整跑完（降级为空上下文）。"""
    g = build_graph(model=_make_model(score=90), retriever=_BoomRetriever())
    cfg = {"configurable": {"thread_id": "t_boom"}}
    result = asyncio.run(g.ainvoke({"messages": [AIMessage(content="随便问")]}, config=cfg))
    assert result["context"] == ""
    assert result["score"] == 90


# ---------------------------------------------------------------------------
# 语料可见性：空语料是**静默失败**，必须留下告警
# ---------------------------------------------------------------------------
def test_empty_corpus_is_warned(monkeypatch, caplog):
    """语料为空必须告警。

    这是本条目唯一能防住的一类故障：语料目录没挂进容器 / DOCS_DIR 指错 ⇒
    `load_chunks` 对不存在的目录**返回 [] 而不抛异常** ⇒ 服务照常起、探针照常绿、
    SSE 照常连，只是检索恒无结果、回答里没有任何引用。没有这条告警就只能靠
    用户反馈"答得不对"才发现，排查时又会去怀疑模型而不是部署。
    """
    import logging as _logging

    from src import graph as G

    monkeypatch.setattr(G, "_CHUNKS", None)
    monkeypatch.setattr(G, "load_chunks_cached", lambda d: [])

    with caplog.at_level(_logging.WARNING):
        assert G._load_chunks_once() == []

    assert any("RAG 语料为空" in r.getMessage() for r in caplog.records), (
        "空语料没有告警 —— 漏挂语料目录时会变成'检索恒无结果且无任何信号'"
    )


def test_nonempty_corpus_is_not_warned(monkeypatch, caplog):
    """反向断言：语料正常时不得误报。

    告警的价值取决于它是否稀缺 —— 一条经常误报的 warning 会被运维当噪音过滤掉，
    于是真正出问题那次也一起被过滤。
    """
    import logging as _logging

    from src import graph as G

    monkeypatch.setattr(G, "_CHUNKS", None)
    monkeypatch.setattr(G, "load_chunks_cached", lambda d: [{"text": "块"}])

    with caplog.at_level(_logging.WARNING):
        assert len(G._load_chunks_once()) == 1

    assert not any("RAG 语料为空" in r.getMessage() for r in caplog.records)
