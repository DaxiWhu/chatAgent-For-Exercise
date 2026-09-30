"""请求级横切中间件：request_id 注入 + 访问指标 + 请求体积上限 + 并发闸门。

本模块提供三个**纯 ASGI** 中间件：
  - `RequestContextMiddleware`：分配/回写 request_id，记访问指标与慢请求。
  - `BodySizeLimitMiddleware`：限制请求体字节数，超限返回 413（应用层自保，
    不依赖 nginx 的 `client_max_body_size`）。
  - `ConcurrencyLimitMiddleware`：在途请求数超上限时返回 503 + `Retry-After`
    （背压；`RATE_LIMIT_*` 限的是速率，管不住长连接驻留）。

为什么都是**纯 ASGI 中间件**而不是 `BaseHTTPMiddleware`
---------------------------------------------------
Starlette 的 `BaseHTTPMiddleware` 会用一个额外的 anyio 任务包装响应，
在 SSE 这种"响应头先返回、响应体持续推送"的场景下**可能引入缓冲**，
表现为"流式回答不是逐字出来，而是攒一会儿一次性吐出"。
本项目的主接口就是 SSE，所以直接写 ASGI 中间件：只包一层 `send`/`receive`，
**不缓冲、不复制响应体**，流式行为与不加中间件时完全一致。

RequestContextMiddleware 的作用
------------------------------
1. 取上游 `X-Request-ID`，没有就生成一个；写进 contextvars（供日志）并回写响应头。
2. 记录 `http_requests_total{method,path,status}` 与耗时直方图。
3. 未捕获异常时打一条带 rid 的 error 日志并计入 `http_errors_total`（再把异常抛出，
   让 FastAPI 的 500 处理链路照常工作）。
"""

import json
import logging
import re
import threading
import time
import uuid

from starlette.datastructures import MutableHeaders

from . import metrics
from .logging_setup import get_request_id, reset_request_id, set_request_id

# ---------------------------------------------------------------------------
# 在途请求计数（用于优雅关闭与容量观察）
# ---------------------------------------------------------------------------
# 为什么需要它
# ----------
# SSE 接口的"一个请求"可能持续几十秒。滚动更新时运维最需要回答的一个问题是
# **"这次发布掐断了几个正在进行的回答"** —— 没有这个计数就只能靠用户投诉。
#
# 另外它是判断"实例是不是卡住了"的直接信号：进程还在、但 in_flight 长期不降，
# 说明有请求挂在某个上游上（而不是"没流量"）。
#
# 用锁而不是依赖"单线程事件循环"：ASGI 应用也可能跑在线程池里（TestClient、
# 某些同步部署形态），一次竞态就让计数永久偏移，之后每次都误报。
_IN_FLIGHT = 0
_IN_FLIGHT_LOCK = threading.Lock()


def in_flight_count() -> int:
    """当前处理中的 HTTP 请求数（含正在流式推送的 SSE）。"""
    with _IN_FLIGHT_LOCK:
        return _IN_FLIGHT


def _in_flight_add(delta: int) -> int:
    """增减在途计数并同步到指标 gauge，返回新值。

    计数与 gauge 一起更新，避免"两处各自维护、慢慢不一致"。
    """
    global _IN_FLIGHT
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT = max(0, _IN_FLIGHT + delta)
        n = _IN_FLIGHT
    metrics.set_gauge("http_requests_in_flight", n)
    return n


# 允许的上游 request_id 形态：长度受限的 URL-safe 字符集。
#
# 为什么必须校验（而不是"拿过来直接用"）：
#   ① 这个值会被写进**该请求的每一行日志**，还会回写到响应头。客户端塞一个
#      100KB 的字符串就能把日志行撑爆；换着花样发就是一个便宜的"日志放大"攻击
#      （不花多少请求就吃掉日志存储与转发带宽）。
#   ② 含 `\r\n` 的值有**响应头注入**的风险（JSON 日志会转义换行，所以日志侧
#      伪造新行这条走不通，但头部这条不该只依赖 ASGI 服务器去拦）。
#   ③ 部署拓扑上常看到"上游网关已经生成 rid"，但网关是我们**不可控**的一环 ——
#      不可信输入的校验必须做在自己这一侧。
# 校验不过**不报错**，只是忽略并自己生成一个：rid 是排障辅助，不该让请求失败。
_RID_OK = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _sanitize_request_id(raw: str) -> str:
    """校验上游传入的 request_id；不合规返回空串（调用方会自行生成）。

    raw: 从 `X-Request-ID` 头解出的字符串。
    返回可安全使用的 rid，或空串。
    """
    return raw if _RID_OK.match(raw or "") else ""


# 请求在**路由之前**被拒时，指标 path 标签用哪个固定串。
#
# 为什么必须固定（不能用原始 path）：这类请求 `scope["route"]` 还没被写进去 ⇒
# 会落到 "unmatched"，与真正的 404 混在一起。而"有人在发超大包"和"实例扛不住并发"
# 是完全不同的故障，必须能各自告警。**更不能改用原始 URL** —— 攻击者随便填路径
# 就能把标签基数打爆，那正是 `_route_template` 要防的事。
#
# 每个闸门写自己的固定串（体积 → `<pre-route>`，并发 → `<shed>`），
# 这样"谁拒的"在指标上就分得开。
_PRE_ROUTE_LABEL_KEY = "chatagent.pre_route_label"


def _route_template(scope) -> str:
    """取路由模板作为指标标签（如 `/v1/review`），而不是原始 URL。

    scope: ASGI scope。

    为什么不用 `scope["path"]`：原始路径会带上 thread_id 之类的变量部分，
    每个不同值都会产生一个新的标签组合 —— 直接导致指标内存爆炸（高基数）。
    路由未匹配到（404）时退回固定串 "unmatched"。

    为什么被闸门拒掉的请求要单独标一个固定串
    --------------------------------------
    体积闸门 / 并发闸门都在**路由之前**返回，那时 `scope["route"]` 还没被写进去 ⇒
    会落到 "unmatched"，与真正的 404 混在同一个标签下。而"有人在发超大包"与
    "实例扛不住并发"是完全不同的故障，必须能单独告警。这里用一个固定的低基数字符串。
    """
    forced = scope.get(_PRE_ROUTE_LABEL_KEY)
    if forced:
        return forced
    route = scope.get("route")
    if route is not None and getattr(route, "path", None):
        return route.path
    return "unmatched"


class RequestContextMiddleware:
    """ASGI 中间件：给每个 HTTP 请求分配 request_id 并记录指标。"""

    def __init__(self, app, enabled: bool = True):
        """记住被包裹的 ASGI app。

        app: 下一个 ASGI 应用（FastAPI 实例）。
        enabled: 关掉时退化为"直接透传"（保留 X-Request-ID 回写的最小行为）。
        """
        self.app = app
        self.enabled = enabled

    async def __call__(self, scope, receive, send):
        """ASGI 入口：只处理 http，其余（lifespan/websocket）原样透传。"""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        incoming_rid = ""
        if b"x-request-id" in headers:
            incoming_rid = _sanitize_request_id(
                headers[b"x-request-id"].decode("latin-1", "ignore").strip())
        rid = incoming_rid or uuid.uuid4().hex[:16]
        token = set_request_id(rid)
        started = time.perf_counter()
        # 在途计数**不受 enabled 影响**：即使指标关闭，优雅关闭也需要知道
        # "现在还有几个请求在跑"。它只是一次加/减，成本可忽略。
        _in_flight_add(1)

        status_holder = {"status": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message.get("status", 500)
                # 回写 request_id：客户端/前端 bug 反馈时带上它，服务端一查就定位
                MutableHeaders(scope=message)["X-Request-ID"] = rid
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            metrics.inc("http_errors_total", path=_route_template(scope))
            logging.exception("请求处理异常（rid=%s path=%s）",
                              rid, _route_template(scope))
            raise
        finally:
            _in_flight_add(-1)
            dur = time.perf_counter() - started
            if self.enabled:
                path = _route_template(scope)
                metrics.inc("http_requests_total", method=scope.get("method", "?"),
                            path=path, status=str(status_holder["status"]))
                metrics.observe("http_request_duration_seconds", dur, path=path)
            # 慢请求留痕：>10s 通常是 LLM 卡住或检索退化，值得单独看
            if dur > 10:
                logging.warning("慢请求 %.1fs（rid=%s path=%s status=%s）",
                                dur, rid, _route_template(scope), status_holder["status"])
            reset_request_id(token)


def current_request_id() -> str:
    """便捷读取当前请求 id（供业务代码打自定义日志用）。"""
    return get_request_id()


# ---------------------------------------------------------------------------
# 请求体积上限（应用层）
# ---------------------------------------------------------------------------
# 为什么不能只靠 nginx 的 `client_max_body_size`
# --------------------------------------------
# 那条配置只在"流量确实经过 nginx"时生效。内部直连、未前置网关的部署形态、
# 以及压测/灰度对比都是绕过它的；而 `ChatReq.message` 原先是一个**无长度约束的
# str**，一个几百 MB 的 JSON 会先被完整读进内存再交给 Pydantic —— 进程直接被打爆，
# 不需要任何漏洞，一个 curl 就够。
#
# 为什么两层都要（头部 + 边读边量）
# -------------------------------
# **只校验 `Content-Length` 是可以被绕过的**：`Transfer-Encoding: chunked` 的请求
# 根本没有这个头，uvicorn 会照读不误。所以：
#   ① Content-Length 已知且超限 → 立刻拒绝，**一个字节都不读**（最省资源）；
#   ② 未知/非法（含 chunked）→ 包装 receive，**边读边累加**，越过阈值就抛信号中断。
# 只做 ① 等于给攻击者留了一条"分块发送"的后门；只做 ② 则每次都要读满才知道超限。
#
# 与字段级校验（Pydantic max_length）的分工见 app/config.py 的说明。
class _BodyTooLarge(Exception):
    """内部信号：请求体超过上限。**只在中间件内部使用，不会外泄**。"""


class BodySizeLimitMiddleware:
    """纯 ASGI 中间件：限制请求体字节数，超限返回 413。

    为什么是纯 ASGI 而不是 `BaseHTTPMiddleware`
    -----------------------------------------
    同 `RequestContextMiddleware`：本项目主接口是 SSE，`BaseHTTPMiddleware`
    会额外包一个 anyio 任务，有引入响应缓冲的风险。这里只包一层 `receive`，
    对响应侧零改动。

    为什么超限不是"截断后继续"
    ------------------------
    被截断的 JSON 一定解析失败，客户端会收到一个语义错误的 422；而且后续还要
    处理"半个请求体"，徒增状态。直接 413 + 明确文案，让调用方立刻知道要改什么。
    """

    def __init__(self, app, max_bytes: int, enabled: bool = True):
        """构造。

        app: 下一个 ASGI 应用。
        max_bytes: 请求体上限（字节）。<=0 视为不启用。
        enabled: 显式开关（测试可关掉）。
        """
        self.app = app
        self.max_bytes = int(max_bytes or 0)
        self.enabled = bool(enabled) and self.max_bytes > 0

    async def __call__(self, scope, receive, send):
        """ASGI 入口：只处理 http，其余原样透传。"""
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return

        # ① 声明长度已经超限：连读都不读
        declared = None
        for name, value in (scope.get("headers") or ()):
            if name == b"content-length":
                try:
                    declared = int(value)
                except (TypeError, ValueError):
                    declared = None
                break
        if declared is not None and declared > self.max_bytes:
            await self._reject(scope, send, declared)
            return

        # ② 未声明（chunked）或声明不可信：边读边计量
        received = 0
        started = {"v": False}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                started["v"] = True
            await send(message)

        async def limited_receive():
            nonlocal received
            msg = await receive()
            if msg.get("type") == "http.request":
                received += len(msg.get("body") or b"")
                if received > self.max_bytes:
                    # 用异常而不是 `http.disconnect` 中断：后者会被 Starlette 解释成
                    # "客户端主动断开"，最终抛 ClientDisconnect 变成 500 —— 语义完全错。
                    raise _BodyTooLarge()
            return msg

        try:
            await self.app(scope, limited_receive, send_wrapper)
        except _BodyTooLarge:
            if started["v"]:
                # 响应已经开始发送（正常流程里不会走到：FastAPI 是先读完 body
                # 再进端点）。此时无法再改成 413，只能记一条并在日志里点名，
                # 否则会表现成"偶发 500 且原因不明"。
                metrics.inc("request_body_rejected_total", phase="after_response")
                logging.error("请求体超限（%d 字节）但响应已开始，无法返回 413", received)
                raise
            await self._reject(scope, send, received)

    async def _reject(self, scope, send, received: int) -> None:
        """返回 413 + 明确文案（并计入指标，便于告警"谁在发大包"）。

        scope: ASGI scope。只为打一个"在路由前被拒"的标记（见 `_route_template`）。
        send: ASGI send 可调用。**显式传参而不是存成实例字段** —— 中间件实例是
            全局单例、同时服务所有请求，把请求级状态挂到 self 上会串请求。
        received: 实际读到/声明的字节数（仅用于日志）。
        """
        scope[_PRE_ROUTE_LABEL_KEY] = "<pre-route>"
        metrics.inc("request_body_rejected_total", phase="rejected")
        logging.warning("请求体超过上限 %d 字节（已读 %d），已拒绝", self.max_bytes, received)
        body = json.dumps(
            {"detail": f"请求体过大：上限 {self.max_bytes} 字节"},
            ensure_ascii=False).encode("utf-8")
        await send({"type": "http.response.start", "status": 413, "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]})
        await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------
# 并发闸门（背压）
# ---------------------------------------------------------------------------
# 为什么需要一个**闸门**，而不是继续只看指标
# ----------------------------------------
# `in_flight_count()` 已经把在途数暴露成 gauge，但**只看不拦等于没有背压**。
# `RATE_LIMIT_*` 限的是**每分钟新请求数**，管不住**并发驻留**：本项目主接口是 SSE，
# 一个请求可能持续几十秒 ⇒ 一个客户端按限流上限开若干连接并**保持不关**，就能把
# 单实例的并发能力占满 —— 而限流指标、错误率、延迟全部正常，没有任何信号提示
# "实例已经满了"。这不是理论：一次点击重试风暴就够了。
#
# 为什么用"计数器 + 判断"而不是 `asyncio.Semaphore`
# ----------------------------------------------
# Semaphore 的行为是**排队等待**，而过载时排队是最坏的选择：队列本身占内存，
# 用户会一直等一个注定超时的回答。这里要的是**快速失败** —— 把"我扛不住了"
# 立刻告诉调用方（背压）。判断与递增之间**没有 `await`**，单事件循环下即原子；
# 但计数仍用锁保护：ASGI 应用也可能跑在线程池里（TestClient、同步部署形态），
# 一次竞态会让计数永久偏移，之后每次判断都错。
#
# 为什么健康检查与 `/metrics` 必须豁免
# ----------------------------------
# 这两条路径恰恰在**过载时最需要**：
#   - `/health/*` 被拒 ⇒ 负载均衡/编排把实例判死并重启，重启风暴只会更糟，
#     而且新实例同样会被打满（病根是流量，不是这个进程）；
#   - `/metrics` 被拒 ⇒ 正好在最该看数据的时候没有数据。
# 豁免 = 既不被拒、也不占用名额（完全不经过计数）。
_ALWAYS_ALLOWED_PREFIXES = ("/health", "/metrics")

_CONCURRENT = 0
_CONCURRENT_LOCK = threading.Lock()

#: 拒绝日志的节流间隔（秒）。过载时日志量本身会变成新的故障源（写盘 + 转发），
#: 而计数有指标兜着 —— 日志只用来给一个"什么时候开始的"时间点。
_SHED_LOG_INTERVAL_SECONDS = 5.0
_last_shed_log = 0.0


def concurrent_count() -> int:
    """当前被并发闸门计入的请求数（**不含**被豁免的 /health、/metrics）。"""
    with _CONCURRENT_LOCK:
        return _CONCURRENT


def _should_log_shed() -> bool:
    """这次拒绝是否该打日志（按时间节流）。"""
    global _last_shed_log
    now = time.monotonic()
    with _CONCURRENT_LOCK:
        if now - _last_shed_log < _SHED_LOG_INTERVAL_SECONDS:
            return False
        _last_shed_log = now
        return True


class ConcurrencyLimitMiddleware:
    """纯 ASGI 中间件：在途请求数超过上限时返回 503 + `Retry-After`。

    为什么是纯 ASGI：同 `RequestContextMiddleware` —— 主接口是 SSE，
    `BaseHTTPMiddleware` 会额外包一个 anyio 任务，有引入响应缓冲的风险。
    """

    def __init__(self, app, limit: int, enabled: bool = True):
        """构造。

        app: 下一个 ASGI 应用。
        limit: 单进程在途请求上限。<=0 视为不启用。
        enabled: 显式开关（测试可关掉）。
        """
        self.app = app
        self.limit = int(limit or 0)
        self.enabled = bool(enabled) and self.limit > 0
        if self.enabled:
            # 上限本身也暴露成指标：只有"实测在途 ÷ 上限"这个比值才能回答
            # "离被打满还有多远"，光看绝对值判断不了。
            metrics.set_gauge("http_concurrency_limit", self.limit)

    async def __call__(self, scope, receive, send):
        """ASGI 入口：只处理 http，其余原样透传。"""
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return
        if (scope.get("path") or "").startswith(_ALWAYS_ALLOWED_PREFIXES):
            await self.app(scope, receive, send)
            return

        global _CONCURRENT
        with _CONCURRENT_LOCK:
            if _CONCURRENT >= self.limit:
                admitted, current = False, _CONCURRENT
            else:
                _CONCURRENT += 1
                admitted, current = True, _CONCURRENT
        if not admitted:
            await self._reject(scope, send, current)
            return

        metrics.set_gauge("http_concurrency_in_flight", current)
        try:
            await self.app(scope, receive, send)
        finally:
            # 放在 finally：上游抛异常（500）也必须归还名额 —— 漏归还的话名额
            # 会随故障次数单调减少，最终"实例明明空闲却一律 503"。
            with _CONCURRENT_LOCK:
                _CONCURRENT = max(0, _CONCURRENT - 1)
                n = _CONCURRENT
            metrics.set_gauge("http_concurrency_in_flight", n)

    async def _reject(self, scope, send, current: int) -> None:
        """返回 503 + `Retry-After`（语义：**实例容量耗尽**，不是身份配额超限）。

        scope: ASGI scope。只为打一个"在路由前被闸门拒掉"的标签（见 `_route_template`）。
        send: ASGI send 可调用。**显式传参而不是存成实例字段** —— 中间件实例是
            全局单例、同时服务所有请求，把请求级状态挂到 self 上会串请求。
        current: 拒绝时刻的在途数（仅用于日志）。
        """
        scope[_PRE_ROUTE_LABEL_KEY] = "<shed>"
        metrics.inc("http_requests_rejected_total", reason="concurrency")
        if _should_log_shed():
            logging.warning("并发已达上限 %d（当前 %d），拒绝新请求（日志按 %.0fs 节流）",
                            self.limit, current, _SHED_LOG_INTERVAL_SECONDS)
        body = json.dumps(
            {"detail": f"服务繁忙：实例并发已达上限（{self.limit}），请稍后重试"},
            ensure_ascii=False).encode("utf-8")
        await send({"type": "http.response.start", "status": 503, "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"retry-after", b"1"),
        ]})
        await send({"type": "http.response.body", "body": body})
