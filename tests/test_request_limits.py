"""请求体积上限：ASGI 层字节闸门（413）+ 字段级长度校验（422）。

为什么要单独测 chunked（无 Content-Length）这条路径
------------------------------------------------
只校验 `Content-Length` 的实现看起来能通过所有 HTTP 层用例，但**可以被绕过**：
`Transfer-Encoding: chunked` 的请求根本没有这个头，uvicorn 照读不误。
所以本文件用原始 ASGI 消息直接驱动中间件（httpx 不方便构造"无 CL 的分块体"），
把这条后门钉住。

两层阈值的关系（默认 1 MiB 字节 / 10 万字符）
------------------------------------------
字节闸门比字段闸门**宽一个量级**，所以二者的边界必须在**各自的层面**验证：
小阈值用直接驱动中间件的方式测（快且精确），字段边界走真实 HTTP 打端点。
"""
import asyncio
import json
import os

import pytest

os.environ["DB_URL"] = ""

pytest.importorskip("fastapi.testclient", reason="TestClient 需要 httpx")

from fastapi.testclient import TestClient      # noqa: E402

import app.main as M                            # noqa: E402
from app import metrics                         # noqa: E402
from app.middleware import BodySizeLimitMiddleware  # noqa: E402
from app.ratelimit import RateLimiter           # noqa: E402

client = TestClient(M.app)
MAX = M.config.MAX_REQUEST_BYTES


@pytest.fixture(autouse=True)
def _loose_gates(monkeypatch):
    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=100000))
    metrics.reset()


def _fake_graph():
    class _G:
        async def astream(self, incoming, config, stream_mode):
            from types import SimpleNamespace
            yield SimpleNamespace(content="ok"), {}

        async def aget_state(self, config):
            from types import SimpleNamespace
            return SimpleNamespace(next=(), values={"citations": []})

    return _G()


# ---------------------------------------------------------------------------
# 原始 ASGI 驱动：可精确控制"分块到达"与"声明长度"
# ---------------------------------------------------------------------------
def _drive(max_bytes, chunks, declared=None, scope_type="http"):
    """用原始 ASGI 消息驱动中间件，返回 (内层 app 是否被调用, 发出的响应消息列表)。

    chunks: http.request 消息列表（模拟分块到达的请求体）。
    declared: 若给了，写进 scope 的 content-length 头。
    """
    scope = {"type": scope_type, "method": "POST", "path": "/v1/chat", "headers": []}
    if declared is not None:
        scope["headers"].append((b"content-length", str(declared).encode("ascii")))

    called = {"v": False}
    sent = []

    async def inner(_scope, receive, send):
        """模拟 FastAPI：把请求体读到底（不读完进不了端点）。"""
        called["v"] = True
        while True:
            msg = await receive()
            if msg["type"] != "http.request" or not msg.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"inner-ok"})

    mw = BodySizeLimitMiddleware(inner, max_bytes=max_bytes)
    inbox = iter(list(chunks) + [{"type": "http.request", "body": b"",
                                  "more_body": False}])

    async def receive():
        return next(inbox, {"type": "http.disconnect"})

    async def send(message):
        sent.append(message)

    asyncio.run(mw(scope, receive, send))
    return called["v"], sent


def _status(sent):
    """取响应状态码（断言前先确认真的发了响应）."""
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert starts, f"中间件没有发出任何响应：{sent}"
    return starts[0]["status"]


# ---------------------------------------------------------------------------
# ① 声明长度超限：立刻拒绝，一个字节都不读
# ---------------------------------------------------------------------------
def test_declared_content_length_over_limit_rejected_with_413():
    r = client.post("/v1/chat", content=b"x" * (MAX + 1),
                    headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert str(MAX) in r.json()["detail"]      # 文案里给出上限，便于调用方调整
    assert metrics.value("request_body_rejected_total", phase="rejected") == 1


def test_declared_over_limit_does_not_call_inner_app():
    """超限时**不能**进入内层 app —— 否则"读都不读"就白说了。"""
    called, sent = _drive(max_bytes=10,
                          chunks=[{"type": "http.request", "body": b"x" * 99,
                                   "more_body": False}],
                          declared=99)
    assert called is False
    assert _status(sent) == 413


def test_body_at_exact_limit_passes_the_byte_gate():
    """边界：正好等于上限必须放行（否则是把 off-by-one 当安全）。"""
    called, sent = _drive(max_bytes=10,
                          chunks=[{"type": "http.request", "body": b"x" * 10,
                                   "more_body": False}],
                          declared=10)
    assert called is True
    assert _status(sent) == 200


# ---------------------------------------------------------------------------
# ② 未声明长度（chunked）：只校验 CL 的实现在这里漏
# ---------------------------------------------------------------------------
def test_chunked_body_over_limit_rejected_without_content_length():
    """无 Content-Length 的分块体超限 → 必须靠"边读边量"拦住（8+4 > 10）。"""
    called, sent = _drive(
        max_bytes=10,
        chunks=[{"type": "http.request", "body": b"12345678", "more_body": True},
                {"type": "http.request", "body": b"9012", "more_body": False}],
        declared=None)
    assert called is True                      # 内层确实开始了读（无法预判）
    assert _status(sent) == 413, "分块请求绕过了体积校验"
    assert metrics.value("request_body_rejected_total", phase="rejected") == 1


def test_chunked_body_under_limit_passes_through():
    """未超限的分块体应正常透传（不能因为"没声明长度"就一律拒绝）。"""
    called, sent = _drive(
        max_bytes=100,
        chunks=[{"type": "http.request", "body": b"12345678", "more_body": True},
                {"type": "http.request", "body": b"90", "more_body": False}],
        declared=None)
    assert called is True
    assert _status(sent) == 200
    assert sent[-1]["body"] == b"inner-ok"


def test_lying_content_length_cannot_bypass():
    """声明一个很小的 Content-Length 却实发大包 → 仍要被挡住。

    否则"先谎报 1 字节"就是一条绕过路径。
    """
    called, sent = _drive(max_bytes=10,
                          chunks=[{"type": "http.request", "body": b"x" * 20,
                                   "more_body": False}],
                          declared=1)
    assert called is True
    assert _status(sent) == 413


def test_non_http_scope_passes_through():
    """lifespan / websocket 必须原样透传（这个中间件只管 http）。"""
    seen = {"v": False}

    async def app(scope, receive, send):
        seen["v"] = True

    asyncio.run(BodySizeLimitMiddleware(app, max_bytes=10)({"type": "lifespan"}, None, None))
    assert seen["v"] is True


def test_disabled_middleware_is_transparent():
    """enabled=False / max_bytes=0 → 完全透传（回退成旧行为的能力）。"""
    called, sent = _drive(max_bytes=0,
                          chunks=[{"type": "http.request", "body": b"x" * 999,
                                   "more_body": False}])
    assert called is True
    assert _status(sent) == 200


# ---------------------------------------------------------------------------
# ③ 端到端：413 在指标与响应头里可见
# ---------------------------------------------------------------------------
def test_413_response_is_observable():
    """413 必须进访问指标并带 request_id。

    如果它绕过了 RequestContext（中间件顺序放反），指标里就完全看不见，
    只剩用户报障 —— 而"被拒的大包"恰是容量问题的第一信号。

    label 是 `<pre-route>` 而不是路由路径：体积闸门在路由**之前**就返回了，
    那时 `scope["route"]` 还不存在。若退回 "unmatched" 会和真正的 404 混在一起，
    看不出"有人在发超大包"。
    """
    r = client.post("/v1/chat", content=b"x" * (MAX + 1),
                    headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert r.headers.get("X-Request-ID"), "413 响应缺少 request_id"
    assert metrics.value("http_requests_total", method="POST",
                         path="<pre-route>", status="413") == 1


def test_413_carries_cors_header():
    """413 必须带跨域头，否则浏览器只报"CORS 错误"，前端永远看不到真正原因。"""
    r = client.post("/v1/chat", content=b"x" * (MAX + 1),
                    headers={"content-type": "application/json",
                             "Origin": "https://app.example.com"})
    assert r.status_code == 413
    assert r.headers.get("access-control-allow-origin"), dict(r.headers)


def test_middleware_nesting_order_is_pinned():
    """把嵌套顺序钉住：CORS 最外、BodySizeLimit 在 RequestContext **之内**，
    并发闸门夹在两者之间。

    为什么值得为"顺序"单独写一条测试（而不是靠注释）：
    `add_middleware` 是 `insert(0, ...)`（后加的在外层），与读代码的直觉相反。
    第一版就写反了 —— 功能全对、测试全绿，只有一个后果：413 不带 request_id、
    不进指标、浏览器看不到跨域头。这类"顺序反了"没有任何报错，只能靠断言拦。

    并发闸门的位置也有实质后果：它必须在 RequestContext **之内**（否则 503 不带
    request_id、不进 `http_requests_total`），且在 BodySizeLimit **之外**
    （过载时先拒后读，连请求体都不读 —— 那是过载时最紧的资源）。
    """
    node = M.app.middleware_stack or M.app.build_middleware_stack()
    names = []
    while node is not None:
        names.append(type(node).__name__)
        node = getattr(node, "app", None)
    assert names.index("CORSMiddleware") < names.index("RequestContextMiddleware") \
        < names.index("ConcurrencyLimitMiddleware") \
        < names.index("BodySizeLimitMiddleware"), names


# ---------------------------------------------------------------------------
# ④ 字段级：语义明确的 422（点名是哪个字段超了多少）
# ---------------------------------------------------------------------------
def test_message_too_long_returns_422_naming_the_field(monkeypatch):
    """注意这是 422 而**不是** 413：体积远小于 1 MiB 字节闸门，
    应由字段闸门给出精确原因（两层各司其职）。"""
    monkeypatch.setattr(M, "graph", _fake_graph())
    r = client.post("/v1/chat", json={"thread_id": "lim-1",
                                      "message": "x" * (M.config.MAX_MESSAGE_CHARS + 1)})
    assert r.status_code == 422
    detail = json.dumps(r.json(), ensure_ascii=False)
    assert "message" in detail and "超长" in detail


def test_thread_id_too_long_returns_422():
    """thread_id 会当检查点主键 / 限流桶键 / 归属表主键 ⇒ 必须有界。"""
    r = client.post("/v1/chat", json={"thread_id": "t" * (M.config.MAX_THREAD_ID_CHARS + 1),
                                      "message": "hi"})
    assert r.status_code == 422
    assert "thread_id" in json.dumps(r.json(), ensure_ascii=False)


def test_feedback_too_long_returns_422():
    r = client.post("/v1/review", json={"thread_id": "t", "action": "revise",
                                        "feedback": "x" * (M.config.MAX_FEEDBACK_CHARS + 1)})
    assert r.status_code == 422
    assert "feedback" in json.dumps(r.json(), ensure_ascii=False)


def test_resume_thread_id_too_long_returns_422():
    r = client.post("/v1/chat/resume",
                    json={"thread_id": "t" * (M.config.MAX_THREAD_ID_CHARS + 1)})
    assert r.status_code == 422


def test_normal_sized_request_is_unaffected(monkeypatch):
    """常规请求不受影响（默认阈值必须对真实用法无感）。"""
    monkeypatch.setattr(M, "graph", _fake_graph())
    r = client.post("/v1/chat", json={"thread_id": "lim-ok", "message": "梧州有什么特产？"})
    assert r.status_code == 200


def test_limits_are_echoed_in_startup_config():
    """上限必须出现在启动配置回显里 —— 否则"配了多少"无处核对。"""
    limits = M.config.as_dict()["limits"]
    assert limits["max_request_bytes"] == MAX
    assert limits["max_message_chars"] == M.config.MAX_MESSAGE_CHARS
    assert limits["max_thread_id_chars"] == M.config.MAX_THREAD_ID_CHARS
