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
import os
from types import SimpleNamespace

import pytest

os.environ["DB_URL"] = ""          # 与 test_api_v1.py 同口径：不要连真库

pytest.importorskip("fastapi.testclient", reason="TestClient 需要 httpx")

from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

import app.main as M  # noqa: E402
from app import (
    accounts,  # noqa: E402
    metrics,  # noqa: E402
)
from app import auth as userauth  # noqa: E402
from app.ratelimit import RateLimiter  # noqa: E402
from app.sessions import SessionRegistry  # noqa: E402
from tests.auth_util import auth_headers  # noqa: E402

client = TestClient(M.app, headers=auth_headers())

#: 测试用的 PBKDF2 迭代数（最小值，再低会被 `verify_password` 的安全下限拒掉）。
#: 用最小值是因为这些用例**不测口令强度**，只需一条能走通的真实验证路径。
_TEST_ITER = 100_000


@pytest.fixture(autouse=True)
def _loose_gates(monkeypatch):
    """放开限流与配额，避免用例之间互相挤占（本文件不测这两个闸门）。"""
    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=100000))
    metrics.reset()


@pytest.fixture(autouse=True)
def _jwt_env(monkeypatch):
    """装一套**内存后端**的账号体系，让本文件的用例能拿到真实的会话令牌。

    为什么要造这一套：本文件里几条用例考察的是「**可信**租户下的会话归属判定」。
    2026-10-04 之前，"可信租户"是靠 `monkeypatch.setattr(config, "TENANT_KEYS",
    {"k_acme": "acme"})` + `X-API-Key: k_acme` 构造的；那张表整体删除后，
    可信租户只剩**一个**来源：已验签的 JWT 载荷。所以这里造 `alice@acme` /
    `bob@beta` 两个账号，并走与生产完全相同的签发路径
    （`login_with_password` → `login_principal`）拿到 access token —— 而不是手搓 JWT。

    ⚠️ 为什么是 autouse 而不是"谁需要谁声明"：`AUTH_ENABLED` 会改变带 Bearer 请求
    的判定，但**不带** Authorization 头的用例仍走匿名路径、行为不变（本文件的其余
    用例正是如此）。做成 autouse 就不会出现"漏声明一个 fixture → 那条用例退化成
    匿名身份却依然绿"这种最难发现的假绿。
    """
    store = accounts.AccountStore(dsn="")
    # ⚠️ alice 带 `reviewer` 角色：本文件有一条用例要走通"快照 → 人审 → 收尾"整条链，
    # 而审稿是**资源级授权**（`_assert_review_role` 默认 fail-closed 到 `reviewer`）。
    # 她原本没有角色也能过 —— 那时匿名档被刻意豁免了角色检查，而那条豁免
    # 已随共享密钥路径一起删除。
    store.create_user("alice",
                      userauth.hash_password("pw-alice", iterations=_TEST_ITER),
                      tenant="acme", roles=("reviewer",))
    store.create_user("bob",
                      userauth.hash_password("pw-bob", iterations=_TEST_ITER),
                      tenant="beta")

    # ⚠️ 顺序要紧：`build_auth_service` 在**调用时**读 config.AUTH_ENABLED 决定
    # 会话白名单是否启用。先改配置再装配，否则签出来的令牌没进白名单、一律 401。
    #
    # ⚠️ 这里**刻意不改** `AUTH_SESSION_SECRET`：它是 `tests/conftest.py` 注入的
    # 那一个值，而本文件模块级的 `client` 默认头就是在**收集期**用它签的。
    # 一旦这里换成别的值，验签当场失败（`Signature verification failed`），
    # 表现是本文件所有"用默认身份"的用例一起 401 —— 看起来像鉴权坏了，
    # 实际只是两处密钥不一致。
    monkeypatch.setattr(M.config, "AUTH_ENABLED", True)
    service = userauth.build_auth_service(accounts=store)
    monkeypatch.setattr(M, "_AUTH", service)
    monkeypatch.setattr(M, "_ACCOUNTS", store)
    _JWT["service"] = service

    # 模块级的默认头是在**收集期**用当时的 `_AUTH` 签的，它的会话登记在**旧**服务
    # 的白名单里；上面刚把 `_AUTH` 换成了新服务（白名单是空的）⇒ 那份默认头验签能过、
    # 但会话查不到，仍会 401。所以这里用**当轮装配好的**服务重新签一份默认头
    # （`monkeypatch.setitem` 会在用例结束后自动还原）。
    alice = service.login_with_password("alice", "pw-alice")
    access, _refresh, _sid = service.login_principal(alice)
    monkeypatch.setitem(client.headers, "Authorization", f"Bearer {access}")

    yield
    _JWT["service"] = None


#: 供 `_bearer()` 取用当轮装配好的 AuthService（fixture 与模块级辅助函数之间的桥）。
_JWT: dict = {"service": None}


def _bearer(username: str = "alice", password: str = "pw-alice") -> dict:
    """签一份该用户的 access token，返回可直接传给 `client.get(headers=...)` 的字典。"""
    service = _JWT["service"]
    assert service is not None, "`_jwt_env` 没生效：本用例拿不到可信身份"
    principal = service.login_with_password(username, password)
    access, _refresh, _sid = service.login_principal(principal)
    return {"Authorization": f"Bearer {access}"}


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


@pytest.fixture
def owned(monkeypatch):
    """把会话登记表换成"内存 + 已启用"的一份，并返回 `own(thread_id)`。

    为什么需要它：`AUTH_ENABLED` 在本测试进程里是**开**的（服务只剩 JWT 一条鉴权
    路径，关掉它所有业务请求都会 401），而 `_SESSIONS.enabled` 正是由它推导出来的
    ⇒ 读会话时会走"查不到属主一律 **404**（fail-closed）"那条分支，快照用例连
    body 都拿不到。

    生产上这不构成问题 —— 每条会话都先经过 `/chat` 的 `_preflight` 认领过。
    本文件这些用例要测的是**快照的字段**，所以先把属主登记好，让"归属"这条正交的
    关注点由上面那三条专门用例去覆盖。

    用**每个用例一份**的登记表（而不是模块级那份）：否则一个用例登记的 id 会留到
    下一个用例，而"属主是谁"恰好是本文件多条用例的判据。
    """
    reg = SessionRegistry(enabled=True)
    monkeypatch.setattr(M, "_SESSIONS", reg)

    def own(thread_id: str, tenant: str = "acme", user: str = "alice") -> str:
        """登记属主。默认取 `alice@acme` —— 与 `_jwt_env` 装好的默认身份一致。"""
        reg.claim(thread_id, tenant, user)
        return thread_id

    return own


# ---------------------------------------------------------------------------
# 状态快照
# ---------------------------------------------------------------------------
def test_snapshot_404_for_unknown_thread(monkeypatch):
    """从未开始的会话 → 404（而不是一个字段全空的 200，那会让客户端以为"进行中"）。"""
    monkeypatch.setattr(M, "graph", _fake_graph({}, ()))
    r = client.get("/v1/sessions/never")
    assert r.status_code == 404


def test_snapshot_completed_state(monkeypatch, owned):
    """已完成：next 为空 → status=completed、resumable=false，历史与引用都在。"""
    owned("done-1")
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


def test_snapshot_incomplete_is_resumable(monkeypatch, owned):
    """中途断掉：next 非空且不含 human_review → status=incomplete、resumable=true。"""
    owned("cut-1")
    monkeypatch.setattr(M, "graph", _fake_graph(
        {"messages": [HumanMessage(content="写一段介绍")], "iteration": 0},
        next_nodes=("generate",)))
    body = client.get("/v1/sessions/cut-1").json()
    assert body["status"] == "incomplete"
    assert body["resumable"] is True
    assert body["pending_review"] is None


def test_snapshot_awaiting_review_exposes_interrupt_payload(monkeypatch, owned):
    """待审：必须把 interrupt 的原始载荷透出来（审核工作台靠它渲染草稿与评分）。

    这里刻意从 `tasks[].interrupts[].value` 取，而不是从 values 拼 —— 因为
    真正驱动人审界面的是 interrupt 载荷（含 `error`：上一次非法输入时的提示）。
    """
    owned("wait-1")
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


def test_snapshot_falls_back_when_interrupt_payload_unavailable(monkeypatch, owned):
    """拿不到 interrupt 载荷时用 state 字段兜底（信息量相同，来源不同）。"""
    owned("wait-2")
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
    # 属主是 **beta 租户的别人**（`_jwt_env` fixture 里建过 `bob@beta`）。
    # ⚠️ 存的是 **(tenant, user) 元组**；用户那一维要写全，否则属主判定永远为假。
    reg._mem["owned-thread"] = ("beta", "bob")
    monkeypatch.setattr(M, "_SESSIONS", reg)
    monkeypatch.setattr(M, "graph", _fake_graph({"messages": [], "output": "不该被读到"}))

    # 用 `alice@acme` 的真实会话令牌读 —— 属主是**别的租户** ⇒ 403。
    r = client.get("/v1/sessions/owned-thread", headers=_bearer())
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
    monkeypatch.setattr(M, "graph", _fake_graph({"messages": [], "output": "不该被读到"}))
    r = client.get("/v1/sessions/not-registered", headers=_bearer())
    assert r.status_code == 404


def test_snapshot_does_not_claim_session(monkeypatch):
    """读操作**绝不能**认领会话 —— 否则"先读一次"就能把别人的 id 占为己有。"""
    class _ExplodingRegistry(SessionRegistry):
        def claim(self, thread_id, tenant):
            raise AssertionError("只读端点调用了 claim()：会把别人的会话占成自己的")

    reg = _ExplodingRegistry(enabled=True)
    # 直接塞进内存表，绕开 claim。⚠️ 存的是 **(tenant, user) 元组** ——
    # 会话归属在 2026-10-01 下沉到用户维度，写成裸字符串会让
    # `owner_of` 把 "acme" 当元组解包成 ('a','c')，于是属主判定永远为假。
    # ⚠️ 用户那一维必须**写全**：只写 `("acme", "")` 时，属主是"租户 acme 的
    #    空用户"，而下面请求的身份是 `alice@acme` ⇒ 属主判定为假 ⇒ 403（实测踩到）。
    reg._mem["existing"] = ("acme", "alice")
    monkeypatch.setattr(M, "_SESSIONS", reg)
    monkeypatch.setattr(M, "graph", _fake_graph({"messages": [], "output": "ok"}))

    # 用 `alice@acme` 的真实会话令牌读 —— 属主与她自己的 (租户, 用户) 一致 ⇒ 200。
    assert client.get("/v1/sessions/existing",
                      headers=_bearer()).status_code == 200


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


def test_snapshot_matches_real_interrupt_state(monkeypatch, mock_model, fake_retriever,
                                              owned):
    """真图跑到 interrupt → 快照必须准确反映"等人工审核"及其草稿/评分。"""
    owned("real-wait")
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


def test_real_review_resume_closes_the_session(monkeypatch, mock_model, fake_retriever,
                                               owned):
    """从真实 interrupt 状态调 /v1/review accept → 会话收尾（next 变空）。

    这条把"快照 → 人审 → 收尾"整条链走通，证明快照暴露的字段足以驱动后续动作。
    """
    owned("real-close")
    g = _real_graph(mock_model, fake_retriever)
    cfg = {"configurable": {"thread_id": "real-close"}}
    _run(g.ainvoke({"messages": [HumanMessage(content="给我写段介绍")]}, config=cfg))
    monkeypatch.setattr(M, "graph", g)

    r = client.post("/v1/review", json={"thread_id": "real-close", "action": "accept"})
    assert r.status_code == 200

    body = client.get("/v1/sessions/real-close").json()
    assert body["status"] == "completed"
    assert body["resumable"] is False


def test_real_cancelled_run_is_resumable(monkeypatch, mock_model, fake_retriever,
                                         owned):
    """真实取消（模拟断连止损）后 → 快照显示 incomplete 且可续跑。

    这是断点续传要解决的**原始场景**：流被中断，检查点停在未完成的节点之前。
    """
    owned("real-cut")
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
