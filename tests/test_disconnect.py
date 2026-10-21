"""断连止损的**端到端**测试：真实 ASGI 栈 + 真实 `http.disconnect`。

为什么单独一个文件、为什么不用 TestClient
----------------------------------------
`starlette.testclient.TestClient` 把请求-响应当成一次性完整往返：客户端"提前
关掉响应"时它**不会**下发 `http.disconnect`。于是"客户端断连"这条路在它下面
永远走不到 —— 而 2026-09-29 排查发现，这正是本项目断连止损指标与日志长期是
**死代码**的成因：单元用例喂的是"会返回 True 的假 Request"，绕过了真实交互，
一直是**假绿**（生成确实被 sse_starlette 取消了，但 `client_disconnected_total`
恒为 0、日志一行都没有）。

这里自己构造 ASGI 的 `receive` 通道，完整复刻真实服务器的行为：
  ① 先投递 `http.request`（请求体，由 FastAPI 解析）；
  ② 客户端"读到第 N 个 token 后拔线"时投递 `http.disconnect`。
与 uvicorn 在 TCP 断开时做的事完全一致，区别只是我们掌握了时序。

**single-consumer 语义是关键**：`receive()` 是单消费者通道，谁先调谁拿到
`http.disconnect`。sse_starlette 的 `_listen_for_disconnect` 在响应开始时就
阻塞在那儿，所以只有它的 `client_close_handler_callable` 拿得到这条消息 ——
外部轮询 `request.is_disconnected()` 永远晚一步（这也是本文件要钉住的契约）。
"""
import asyncio
import json
import time

import pytest

import app.main as M  # noqa: E402
from app import metrics  # noqa: E402
from app.quota import QuotaManager  # noqa: E402
from app.ratelimit import RateLimiter  # noqa: E402
from tests.auth_util import auth_headers  # noqa: E402

#: 本文件手工构造 ASGI 请求，所以要自己带上凭证。
#:
#: 为什么必须有它：2026-10-05 起服务只剩 JWT 一条鉴权路径，**没有凭证的请求一律 401**
#: （此前能跑通是因为还有匿名档）。用真签的 token 而不是 monkeypatch 鉴权层，
#: 是为了让这些用例仍跑在**真实**的鉴权上下文里 —— 断连止损是横切行为，若鉴权层被
#: 换掉，"401 短路"与"断连处理"谁先发生就测不出来了。
_AUTH_HEADER = auth_headers()["Authorization"]


# ---------------------------------------------------------------------------
# 辅助：原始 ASGI 驱动 + 假图
# ---------------------------------------------------------------------------
class _RawReceive:
    """手工 ASGI `receive` 通道：先给请求体，择机给 `http.disconnect`。"""

    def __init__(self, body: bytes):
        self._q: asyncio.Queue = asyncio.Queue()
        self._q.put_nowait({"type": "http.request", "body": body, "more_body": False})

    async def __call__(self):
        return await self._q.get()

    def disconnect(self):
        """投递断连消息（等价于客户端拔掉 TCP 连接）。"""
        self._q.put_nowait({"type": "http.disconnect"})


class _SlowTurn:
    """替身运行：慢速吐字，并记录"有没有被取消、有没有收尾"。

    慢速是必须的：纯 CPU 循环会在断连消息投递之前就跑完，测不到"断开后停止产出"。

    `cancelled` 才是"取消真的传导到上游"的证据 —— `closed` 在正常完成时也会置位，
    单看它会让"断连后上游仍在后台跑"这种失效照样绿。
    """

    def __init__(self, total=40, delay=0.02):
        self.left = total
        self.delay = delay
        self.closed = False
        self.cancelled = False

    async def __call__(self, req, *, context, on_text=None):
        from src.agents.runner import TaskOutcome

        try:
            while self.left > 0:
                self.left -= 1
                await asyncio.sleep(self.delay)
                if on_text is not None:
                    await on_text("片段", "call-1")
            return TaskOutcome(answer="片段", attempts=1,
                               final_agent="frontline_agent", passed=True,
                               exit_reason="passed")
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.closed = True

def _scope(path, body: bytes, method="POST") -> dict:
    """够用的 HTTP scope（真实服务器会填的字段这里都填上）。"""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"authorization", _AUTH_HEADER.encode()),
        ],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 45678),
    }


async def _drive(app, scope, receive, *, after_tokens=None, timeout=10) -> bytes:
    """把请求打进 ASGI 应用，收到第 `after_tokens` 个 token 后投递断连。

    返回服务端发出的全部 SSE 字节。应用未在 `timeout` 内结束即判失败 ——
    "把自己挂死"是断连处理最典型的失败模式，必须让用例红而不是卡住。
    """
    sent: list[bytes] = []

    async def send(message):
        if message["type"] == "http.response.body":
            sent.append(message.get("body") or b"")

    task = asyncio.create_task(app(scope, receive, send))
    if after_tokens is not None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.01)
            if b"".join(sent).count(b"event: token") >= after_tokens:
                break
        receive.disconnect()

    try:
        await asyncio.wait_for(task, timeout)
    except TimeoutError:  # pragma: no cover - 只在回归时命中
        task.cancel()
        raise AssertionError("断连后应用未结束（连接很可能被挂死）") from None
    return b"".join(sent)


@pytest.fixture
def offline(monkeypatch):
    """把横切组件换成"绝不拦路"的实例，让用例只考察断连这一件事。"""
    # 这个 fixture 此前还关掉过两个横切开关，现在都随着鉴权收口一起没有了：
    #   · `M.config.TENANT_KEYS = {}`（"租户 ↔ API Key"对照表，2026-10-04 删）；
    #   · `M.SERVE_API_KEY = ""`（共享密钥通行证，2026-10-05 删）。
    # 它们的存在意义是"让请求不必出示 X-API-Key"，而**现在所有请求都必须出示
    # JWT** —— 这个 fixture 只管断连，所以它不再需要动鉴权：本文件的用例都直接
    # 调图/端点内部，不经过 `_authenticate`。
    # 剩下两件事才是真正的"绝不拦路"：
    #   - 限流/配额放到不可能触发的量级；
    #   - 指标清零，免得上一轮用例的计数混进来。
    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=10 ** 6))
    monkeypatch.setattr(M, "_QUOTA", QuotaManager(daily_limit=0, request_limit=0))
    metrics.reset()
    yield
    metrics.reset()


# ---------------------------------------------------------------------------
# 核心：断连必须被"真的"观察到
# ---------------------------------------------------------------------------
def test_disconnect_is_detected_counted_and_stops_generation(offline, monkeypatch):
    """客户端读到第 3 个 token 后拔线 —— 必须停止生成、且**被在线观察到**。

    这是本文件存在的理由。修复前（轮询 `request.is_disconnected()`）：
    生成会被 sse_starlette 取消，但 `client_disconnected_total` 恒为 0、
    日志一行都没有。「取消」是库给的，「观测」才是我们的 —— 少了观测，
    线上就无法回答"到底止损了多少次"。

    把 `_sse()` 里的 `client_close_handler_callable=` 去掉，本用例立刻变红。
    """
    g = _SlowTurn(total=40, delay=0.02)
    monkeypatch.setattr(M, "run_turn", g)

    body = json.dumps({"thread_id": "t-disc", "message": "hi"}).encode()
    sse = asyncio.run(_drive(M.app, _scope("/v1/chat", body), _RawReceive(body),
                             after_tokens=3))

    assert metrics.value("client_disconnected_total", kind="chat") == 1, \
        "断连未被计数 —— 止损观测要么没接上，要么是死代码"
    assert sse.count(b"event: token") >= 3, f"断开得太早，没产出内容：{sse[:200]!r}"
    assert sse.count(b"event: token") <= 6, f"断开后仍在继续产出：{sse.count(b'event: token')} 个 token"
    assert b"event: done" not in sse, "已断开却仍下发了收尾事件"
    assert g.cancelled is True, "取消没有传导到上游运行 —— 它会在后台跑到底（边断边计费）"
    assert g.closed is True, "上游运行没有收尾"


def test_review_endpoint_is_wired_for_disconnect_too(offline, monkeypatch):
    """/v1/review 是另一个端点，必须同样接上断连观测。

    分开测的理由：接线的写法很容易只在 /v1/chat 上改对，另外两个端点照抄旧写法
    （这正是 `_sse()` 收敛成一处的原因，但收敛本身也需要被钉住）。
    指标标签应为 `kind="review"`。
    """
    g = _SlowTurn(total=40, delay=0.02)
    monkeypatch.setattr(M, "run_turn", g)
    # 前提：审核队列里真躺着一条待审记录。2026-10-20 起 `/v1/review` 的 409 判据是
    # "这条会话**当前**有没有待审记录"（`needs_review` 那类看运行现场，`manual`
    # 那类只有队列），不造这个前提先返回的是 409 —— 用例照样"绿"，
    # 但测的根本不是断连。
    from app.review_queue import ReviewQueue

    q = ReviewQueue(dsn="")
    q.add(thread_id="t-review", draft="草稿", feedback="", tenant="")
    monkeypatch.setattr(M, "_REVIEW_QUEUE", q)

    body = json.dumps({"thread_id": "t-review", "action": "accept",
                       "feedback": ""}).encode()
    sse = asyncio.run(_drive(M.app, _scope("/v1/review", body), _RawReceive(body),
                             after_tokens=2))

    assert metrics.value("client_disconnected_total", kind="review") == 1
    assert b"event: done" not in sse
    assert g.cancelled is True


def test_normal_completion_is_not_counted_as_disconnect(offline, monkeypatch):
    """反向用例：客户端全程在线跑完，**不能**被误记成断连。

    少了这条，把 `client_disconnected_total` 写在生成器出口（而不是断连回调里）
    这种错误永远不会被发现 —— 指标会恒等于"请求数"，彻底失去意义。
    """
    g = _SlowTurn(total=3, delay=0.01)
    monkeypatch.setattr(M, "run_turn", g)

    body = json.dumps({"thread_id": "t-ok", "message": "hi"}).encode()
    sse = asyncio.run(_drive(M.app, _scope("/v1/chat", body), _RawReceive(body)))

    assert b"event: done" in sse
    assert sse.count(b"event: token") == 3
    assert metrics.value("client_disconnected_total", kind="chat") == 0
    assert g.closed is True, "正常跑完也必须收尾（上游协程不能留成孤儿）"
    assert g.cancelled is False, "正常完成的请求被当成被取消的（会误导断连排查）"


# ---------------------------------------------------------------------------
# 结构性契约：钩子本身的形式
# ---------------------------------------------------------------------------
def test_close_handler_is_async_and_wired():
    """`EventSourceResponse` 对回调是 `await handler(message)` ⇒ 必须 async。

    写成同步函数不会在导入期暴露，而是**第一个断连请求**时才炸成
    `TypeError: object NoneType can't be used in 'await' expression` ——
    且炸在库的清理路径上，现场噪声很大。所以在这里钉住。
    """
    resp = M._sse({"messages": []}, "chat",
                  tenant="t", thread_id="x")

    handler = resp.client_close_handler_callable
    assert handler is not None, "断连钩子没接上（库拿到的永远是 None）"
    assert asyncio.iscoroutinefunction(handler), "回调必须是 async 函数"


def test_the_stream_carries_a_heartbeat(monkeypatch):
    """★ 心跳必须**显式**传，且真的取配置值。

    为什么钉这条：首 token 之前那段静默期（模型思考 + 工具执行）里 TCP 上一个
    字节都没有，长连接只能靠这个注释帧保住。它原来来自 sse_starlette 的默认值
    （3.3.4 是 15s，``ping=None`` 时才用它）—— 第三方默认值升版会变，改成 0 或
    关掉**都不会报错**，症状只是"长回答偶发中断"，而且服务日志里干干净净。

    ⚠️ 必须**改配置值**再断言，不能只比 `ping_interval > 0`：库的默认值（15）与
    我们的默认值（15）恰好相同，删掉 `ping=` 之后比出来的结果一模一样 ——
    变异验证实测过这条，原写法在"删掉显式传参"时**仍然全绿**。
    """
    from app import config as C

    assert C.SSE_PING_SECONDS > 0, "默认配置把心跳关掉了（长连接会在静默期被反代掐断）"

    monkeypatch.setattr(C, "SSE_PING_SECONDS", 7)
    resp = M._sse({"messages": []}, "chat", tenant="t", thread_id="x")

    assert resp.ping_interval == 7, (
        "`_sse` 没有把配置的心跳间隔传给库（吃的是第三方默认值，升版会静默变）"
    )


def test_signal_is_idempotent(monkeypatch):
    """重复置位只计数一次：库的监听循环理论上可能多次进入，重复计数会让指标虚高。"""
    metrics.reset()
    signal = M._DisconnectSignal("chat")
    asyncio.run(signal.mark_closed({"type": "http.disconnect"}))
    asyncio.run(signal.mark_closed({"type": "http.disconnect"}))

    assert metrics.value("client_disconnected_total", kind="chat") == 1
    assert signal.closed is True
