"""接口级测试：/health 与 /review 的离线用例。

只覆盖**不需要真实 LLM** 的路径，因此可在 CI 中无网络运行：
- /health 健康检查
- /review 的 400（非法 action / revise 缺 feedback）与 409（会话不在审核态）

需要 `httpx`（FastAPI TestClient 依赖）；缺失时整个模块自动跳过。
"""
import json
import os

import pytest

# 先清掉 DB_URL，避免测试时真的去连 Postgres（自动回退 MemorySaver）
os.environ["DB_URL"] = ""

pytest.importorskip("fastapi.testclient", reason="TestClient 需要 httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_review_unknown_thread_returns_409():
    r = client.post("/review", json={
        "thread_id": "no-such-thread", "action": "accept", "feedback": "",
    })
    assert r.status_code == 409


def test_review_invalid_action_returns_400():
    r = client.post("/review", json={
        "thread_id": "t", "action": "nope", "feedback": "",
    })
    assert r.status_code == 400


def test_review_revise_without_feedback_returns_400():
    r = client.post("/review", json={
        "thread_id": "t", "action": "revise", "feedback": "",
    })
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 引用事件（citations）：答案之后必须下发"依据了什么"，否则用户无法回溯
# ---------------------------------------------------------------------------
class _FakeGraph:
    """替身图：只验证 **SSE 事件编排**，不触发真实检索与模型调用。"""

    def __init__(self, citations, next_nodes=()):
        self._citations = citations
        self._next = next_nodes

    async def astream(self, incoming, config, stream_mode):
        from types import SimpleNamespace
        yield SimpleNamespace(content="华东区 1200。"), {}

    async def aget_state(self, config):
        from types import SimpleNamespace
        return SimpleNamespace(next=self._next,
                               values={"citations": self._citations,
                                       "output": "草稿", "score": 50, "feedback": "x"})


def _collect(gen):
    """把异步生成器收成事件列表。"""
    import asyncio

    async def run():
        return [ev async for ev in gen]

    return asyncio.run(run())


def test_stream_emits_citations_between_tokens_and_done(monkeypatch):
    import app.main as M

    cites = [{"n": 1, "source": "年报.pdf", "page": 3, "snippet": "| 华东 | 1200 |"}]
    monkeypatch.setattr(M, "graph", _FakeGraph(cites))
    events = _collect(M._stream_graph({}, {}, "chat"))
    assert [e["event"] for e in events] == ["token", "citations", "done"]
    # data 必须是**已序列化的 JSON 字符串**，不能是 list 本身：
    # sse_starlette 的 encode 对 data 走 `str(...)`，直接给 list 会发出 Python repr
    # （单引号、None/True），JS/Go 客户端解析不了。详见 app.main._sse_json 的注释。
    # 旧断言 `== cites` 只覆盖到序列化之前，是这类 bug 长期漏测的原因。
    assert isinstance(events[1]["data"], str), "data 应是 JSON 字符串（交给 SSE 层前已 dumps）"
    assert json.loads(events[1]["data"]) == cites


def test_stream_skips_citations_event_when_nothing_recalled(monkeypatch):
    """没有召回就**不要**发空引用事件：前端收到空数组会显示一个空引用区。"""
    import app.main as M

    monkeypatch.setattr(M, "graph", _FakeGraph([]))
    events = _collect(M._stream_graph({}, {}, "chat"))
    assert [e["event"] for e in events] == ["token", "done"]


def test_stream_still_signals_need_review_with_citations(monkeypatch):
    import app.main as M

    monkeypatch.setattr(M, "graph", _FakeGraph([{"n": 1, "source": "a.pdf"}],
                                               next_nodes=("human_review",)))
    events = _collect(M._stream_graph({}, {}, "chat"))
    assert [e["event"] for e in events] == ["token", "citations", "need_review"]
