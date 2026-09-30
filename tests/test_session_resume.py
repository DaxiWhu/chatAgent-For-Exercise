"""断点续传：会话状态快照（GET /v1/sessions/{id}）与续跑（POST /v1/chat/resume）。

为什么要重点测"归属校验走的是哪条路"
--------------------------------
快照端点是**纯读**的。如果它复用了 `/chat` 的 `_preflight`，就会连带触发
`SessionRegistry.claim()` —— 那是"首次见到即认领"，于是攻击者只要**先读一次**
别人还没开始的 thread_id，就把这条会话占成自己的（随后受害者反而被 403）。
这类缺陷不会让任何功能失效，所以必须有测试把它钉住：见
`test_snapshot_does_not_claim_session`。

另外两条测试用**真实 LangGraph**（MemorySaver + mock 模型）跑到 interrupt，
而不是用假 state —— 因为 `status/resumable/pending_review` 的推导完全依赖
`StateSnapshot.next` 与 `tasks[].interrupts` 的真实形态，假件容易把字段名写错
却依然全绿。
"""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

os.environ["DB_URL"] = ""          # 与 test_api_v1.py 同口径：不要连真库

pytest.importorskip("fastapi.testclient", reason="TestClient 需要 httpx")

from fastapi.testclient import TestClient      # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver          # noqa: E402

import app.main as M                            # noqa: E402
from app import metrics                         # noqa: E402
from app.ratelimit import RateLimiter           # noqa: E402
from app.sessions import SessionRegistry        # noqa: E402

client = TestClient(M.app)


@pytest.fixture(autouse=True)
def _loose_gates(monkeypatch):
    """放开限流与配额，避免用例之间互相挤占（本文件不测这两个闸门）。"""
    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=100000))
    metrics.reset()


def _fake_graph(values, next_nodes=(), tasks=(), record=None):
    """构造一个只实现 aget_state/astream 的假图。

    values: 会话值（对应 state.values）。
    next_nodes: 未执行完的节点元组（对应 state.next）。
    tasks: 任务列表（里面带 interrupts），用于验证 pending_review 的提取。
    record: 若给了 dict，astream 会把收到的 incoming 记进 record["incoming"]。
    """

    class _G:
        async def aget_state(self, config):
            return SimpleNamespace(values=values, next=next_nodes, tasks=tasks,
                                   config=config)

        async def astream(self, incoming, config, stream_mode):
            if record is not None:
                record["incoming"] = incoming
            yield SimpleNamespace(content="续跑产出的正文"), {}
            return

    return _G()


# ---------------------------------------------------------------------------
# 状态快照
# ---------------------------------------------------------------------------
def test_snapshot_404_for_unknown_thread(monkeypatch):
    """从未开始的会话 → 404（而不是一个字段全空的 200，那会让客户端以为"进行中"）。"""
    monkeypatch.setattr(M, "graph", _fake_graph({}, ()))
    r = client.get("/v1/sessions/never")
    assert r.status_code == 404


def test_snapshot_completed_state(monkeypatch):
    """已完成：next 为空 → status=completed、resumable=false，历史与引用都在。"""
    monkeypatch.setattr(M, "graph", _fake_graph({
        "messages": [HumanMessage(content="梧州有什么特产？"),
                     AIMessage(content="六堡茶与龟苓膏。")],
        "output": "六堡茶与龟苓膏。",
        "score": 92,
        "citations": [{"n": 1, "source": "intro.txt", "page": 3}],
        "iteration": 1,
    }))
    body = client.get("/v1/sessions/done-1").json()
    assert body["status"] == "completed"
    assert body["resumable"] is False
    assert body["draft"] == "六堡茶与龟苓膏。"
    assert body["score"] == 92
    assert body["citations"][0]["page"] == 3
    # 角色做了通用化映射：LangChain 的 human/ai → user/assistant
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][0]["content"] == "梧州有什么特产？"
    assert body["pending_review"] is None


def test_snapshot_incomplete_is_resumable(monkeypatch):
    """中途断掉：next 非空且不含 human_review → status=incomplete、resumable=true。"""
    monkeypatch.setattr(M, "graph", _fake_graph(
        {"messages": [HumanMessage(content="写一段介绍")], "iteration": 0},
        next_nodes=("generate",)))
    body = client.get("/v1/sessions/cut-1").json()
    assert body["status"] == "incomplete"
    assert body["resumable"] is True
    assert body["pending_review"] is None


def test_snapshot_awaiting_review_exposes_interrupt_payload(monkeypatch):
    """待审：必须把 interrupt 的原始载荷透出来（审核工作台靠它渲染草稿与评分）。

    这里刻意从 `tasks[].interrupts[].value` 取，而不是从 values 拼 —— 因为
    真正驱动人审界面的是 interrupt 载荷（含 `error`：上一次非法输入时的提示）。
    """
    intr = SimpleNamespace(value={"draft": "草稿", "score": 33,
                                  "feedback": "缺少来源", "error": "打回必须填写修改意见"})
    monkeypatch.setattr(M, "graph", _fake_graph(
        {"messages": [], "output": "草稿", "score": 33},
        next_nodes=("human_review",),
        tasks=(SimpleNamespace(interrupts=(intr,)),)))
    body = client.get("/v1/sessions/wait-1").json()
    assert body["status"] == "awaiting_review"
    assert body["resumable"] is False          # 要人的决定，不是"续跑"
    assert body["pending_review"]["error"] == "打回必须填写修改意见"
    assert body["pending_review"]["score"] == 33


def test_snapshot_falls_back_when_interrupt_payload_unavailable(monkeypatch):
    """拿不到 interrupt 载荷时用 state 字段兜底（信息量相同，来源不同）。"""
    monkeypatch.setattr(M, "graph", _fake_graph(
        {"output": "草稿B", "score": 41, "feedback": "再具体些"},
        next_nodes=("human_review",)))
    review = client.get("/v1/sessions/wait-2").json()["pending_review"]
    assert review["draft"] == "草稿B" and review["score"] == 41


# ---------------------------------------------------------------------------
# 归属：读路径必须"只查不认领"
# ---------------------------------------------------------------------------
def test_snapshot_403_for_other_tenant(monkeypatch):
    """多租户下读别人的会话 → 403，且计入 session_owner_mismatch_total（告警依赖它）。"""
    reg = SessionRegistry(enabled=True)
    reg.claim("owned-thread", "acme")
    monkeypatch.setattr(M, "_SESSIONS", reg)
    monkeypatch.setattr(M.config, "TENANT_KEYS", {"k_globex": "globex"})
    monkeypatch.setattr(M, "graph", _fake_graph({"messages": [], "output": "x"}))

    r = client.get("/v1/sessions/owned-thread", headers={"X-API-Key": "k_globex"})
    assert r.status_code == 403
    assert metrics.value("session_owner_mismatch_total") == 1
    # 内容一个字节都不能漏出去
    assert "output" not in r.text


def test_snapshot_404_when_registry_enabled_but_unclaimed(monkeypatch):
    """多租户下、登记表里查不到的 id → 404（fail-closed）。

    为什么不是"放行"：正常流程里每条会话都先经过 /chat 的 _preflight 认领过，
    查不到属主就等于"这个 id 不存在"。放行会把端点变成"检查点是否存在"的枚举探针。
    """
    monkeypatch.setattr(M, "_SESSIONS", SessionRegistry(enabled=True))
    monkeypatch.setattr(M.config, "TENANT_KEYS", {"k_acme": "acme"})
    monkeypatch.setattr(M, "graph", _fake_graph({"messages": [], "output": "不该被读到"}))
    r = client.get("/v1/sessions/not-registered", headers={"X-API-Key": "k_acme"})
    assert r.status_code == 404


def test_snapshot_does_not_claim_session(monkeypatch):
    """读操作**绝不能**认领会话 —— 否则"先读一次"就能把别人的 id 占为己有。"""
    class _ExplodingRegistry(SessionRegistry):
        def claim(self, thread_id, tenant):
            raise AssertionError("只读端点调用了 claim()：会把别人的会话占成自己的")

    reg = _ExplodingRegistry(enabled=True)
    reg._mem["existing"] = "acme"            # 直接塞进内存表，绕开 claim
    monkeypatch.setattr(M, "_SESSIONS", reg)
    monkeypatch.setattr(M.config, "TENANT_KEYS", {"k_acme": "acme"})
    monkeypatch.setattr(M, "graph", _fake_graph({"messages": [], "output": "ok"}))

    assert client.get("/v1/sessions/existing",
                      headers={"X-API-Key": "k_acme"}).status_code == 200


# ---------------------------------------------------------------------------
# 续跑
# ---------------------------------------------------------------------------
def test_resume_409_when_completed(monkeypatch):
    monkeypatch.setattr(M, "graph", _fake_graph({"messages": [], "output": "早就写完了"}, ()))
    r = client.post("/v1/chat/resume", json={"thread_id": "r-done"})
    assert r.status_code == 409


def test_resume_409_when_awaiting_review_points_to_review_endpoint(monkeypatch):
    """待审时要**明确指路**到 /v1/review：这两个分支的客户端动作完全不同。"""
    monkeypatch.setattr(M, "graph", _fake_graph(
        {"output": "草稿", "score": 10}, next_nodes=("human_review",)))
    r = client.post("/v1/chat/resume", json={"thread_id": "r-wait"})
    assert r.status_code == 409
    assert "/v1/review" in r.text


def test_resume_passes_none_as_graph_input(monkeypatch):
    """续跑必须用 `astream(None)` —— langgraph 的"从检查点继续"约定。

    传空 dict 会变成"新一轮输入"，把会话当新对话重跑，症状是上下文丢失。
    """
    rec = {}
    monkeypatch.setattr(M, "graph", _fake_graph(
        {"messages": [HumanMessage(content="原问题")], "iteration": 1},
        next_nodes=("generate",), record=rec))
    r = client.post("/v1/chat/resume", json={"thread_id": "r-cut"})
    assert r.status_code == 200
    assert rec["incoming"] is None, f"续跑传了 {rec['incoming']!r}，应为 None"


def test_resume_streams_frames(monkeypatch):
    """续跑走的是同一条 SSE 通道：token … done。"""
    monkeypatch.setattr(M, "graph", _fake_graph(
        {"messages": [], "iteration": 1}, next_nodes=("generate",)))
    r = client.post("/v1/chat/resume", json={"thread_id": "r-cut-2"})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert "续跑产出的正文" in r.text
    assert "event: done" in r.text


# ---------------------------------------------------------------------------
# 与真实 LangGraph 对接（不用假 state：字段名写错时假件会假绿）
# ---------------------------------------------------------------------------
def _run(coro):
    return asyncio.run(coro)


def _real_graph(mock_model, fake_retriever):
    from src.graph import build_graph
    return build_graph(model=mock_model, retriever=fake_retriever,
                       checkpointer=MemorySaver())


def test_snapshot_matches_real_interrupt_state(monkeypatch, mock_model, fake_retriever):
    """真图跑到 interrupt → 快照必须准确反映"等人工审核"及其草稿/评分。"""
    g = _real_graph(mock_model, fake_retriever)
    cfg = {"configurable": {"thread_id": "real-wait"}}
    _run(g.ainvoke({"messages": [HumanMessage(content="给我写段介绍")]}, config=cfg))

    monkeypatch.setattr(M, "graph", g)
    body = client.get("/v1/sessions/real-wait").json()

    assert body["status"] == "awaiting_review"
    assert body["resumable"] is False
    assert body["draft"] == "这是草稿内容。"        # conftest 的 mock 模型固定产出
    assert body["score"] == 50                      # ReviewResult(score=50) → 落在 [LOW, HIGH)
    assert body["pending_review"]["feedback"] == "建议修改"


def test_real_review_resume_closes_the_session(monkeypatch, mock_model, fake_retriever):
    """从真实 interrupt 状态调 /v1/review accept → 会话收尾（next 变空）。

    这条把"快照 → 人审 → 收尾"整条链走通，证明快照暴露的字段足以驱动后续动作。
    """
    g = _real_graph(mock_model, fake_retriever)
    cfg = {"configurable": {"thread_id": "real-close"}}
    _run(g.ainvoke({"messages": [HumanMessage(content="给我写段介绍")]}, config=cfg))
    monkeypatch.setattr(M, "graph", g)

    r = client.post("/v1/review", json={"thread_id": "real-close", "action": "accept"})
    assert r.status_code == 200

    body = client.get("/v1/sessions/real-close").json()
    assert body["status"] == "completed"
    assert body["resumable"] is False


def test_real_cancelled_run_is_resumable(monkeypatch, mock_model, fake_retriever):
    """真实取消（模拟断连止损）后 → 快照显示 incomplete 且可续跑。

    这是断点续传要解决的**原始场景**：流被中断，检查点停在未完成的节点之前。
    """
    g = _real_graph(mock_model, fake_retriever)
    cfg = {"configurable": {"thread_id": "real-cut"}}

    async def _cut():
        stream = g.astream({"messages": [HumanMessage(content="给我写段介绍")]},
                           config=cfg, stream_mode="updates")
        async for _chunk in stream:
            break                          # 只吃到第一个节点就"断连"
        await stream.aclose()               # 与 app.main 的止损路径一致
        await asyncio.sleep(0.1)

    _run(_cut())
    monkeypatch.setattr(M, "graph", g)
    body = client.get("/v1/sessions/real-cut").json()
    assert body["status"] == "incomplete", body
    assert body["resumable"] is True

    # 真的续一次：跑完后应该到达 human_review（mock 模型给 50 分 → 转人工）
    r = client.post("/v1/chat/resume", json={"thread_id": "real-cut"})
    assert r.status_code == 200
    assert "need_review" in r.text
    assert client.get("/v1/sessions/real-cut").json()["status"] == "awaiting_review"
