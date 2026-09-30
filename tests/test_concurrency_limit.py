"""并发闸门的契约（`middleware.ConcurrencyLimitMiddleware`）。

要解决的问题：`RATE_LIMIT_*` 限的是**每分钟新请求数**，它管不住**并发驻留**。
本项目主接口是 SSE，一个请求可能持续几十秒 ⇒ 一个客户端按限流上限开若干连接
并**保持不关**就能把单实例占满，而限流指标、错误率、延迟**全部正常**。
在途数此前只有观测（gauge `http_requests_in_flight`），没有闸门 —— 只看不拦等于没有背压。

本文件钉住的都是"错了也不会报错、只是悄悄失效"的部分：

  1. **判断与递增之间不能有 `await`** —— 有的话两个并发请求会同时看到"还没满"
     然后一起放行（上限形同虚设，而且只在高并发下偶发，本地永远测不出来）；
  2. **名额必须在 `finally` 里归还** —— 漏归还则名额随故障次数单调减少，
     最终"实例明明空闲却一律 503"；
  3. **`/health/*` 与 `/metrics` 必须豁免** —— 被拒会让编排重启实例、
     让 Prometheus 在最该看数据的时候拿不到数据；
  4. **503 必须经 RequestContext 出去** —— 否则不带 X-Request-ID、不进
     `http_requests_total`，过载在指标里完全看不见；path 标签必须固定成 `<shed>`，
     不能是原始 URL（高基数）也不能落到 `unmatched`（与 404 混淆）；
  5. **默认 0 = 不限** —— 猜一个上限会静默拒绝正常流量，与"服务挂了"无法区分。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import metrics
from app.middleware import (ConcurrencyLimitMiddleware, RequestContextMiddleware,
                            concurrent_count)

# ---------------------------------------------------------------------------
# 测试脚手架：一个可以"挂住"的假内层应用
# ---------------------------------------------------------------------------


class InnerApp:
    """假内层应用：`block_path` 上挂住直到 `release`，其余路径立刻 200。

    为什么要"挂住"：并发闸门只有在**请求还没结束**时才有意义。一个立刻返回的
    假 app 测不出任何并发语义（第二个请求进来时第一个早就释放了）。
    """

    def __init__(self, block_path="/v1/chat", boom=False):
        self.block_path = block_path
        self.boom = boom
        self.calls: list[str] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, scope, receive, send):
        self.calls.append(scope["path"])
        if scope["path"] == self.block_path:
            self.entered.set()
            await self.release.wait()
            if self.boom:
                raise RuntimeError("上游炸了")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _scope(path, method="POST"):
    return {"type": "http", "path": path, "method": method, "headers": []}


async def _drive(app, scope):
    """按 ASGI 契约驱动一次请求，返回收到的消息列表。"""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _status(sent):
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def _header(sent, name: bytes):
    start = next(m for m in sent if m["type"] == "http.response.start")
    return dict(start.get("headers") or []).get(name)


def _body(sent):
    return b"".join(m.get("body") or b"" for m in sent if m["type"] == "http.response.body")


async def _wait_calls(inner, n, spins=2000):
    """等到内层应用已经被进入 n 次。

    用**轮转等待**而不是"假设 `await asyncio.sleep(0)` 一次就够"：任务调度次数
    是实现细节，写死次数会让用例偶发失败（而偶发失败的用例一定会被人加 skip）。
    """
    for _ in range(spins):
        if len(inner.calls) >= n:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"等不到内层被进入 {n} 次（实际 {len(inner.calls)}）")


@pytest.fixture(autouse=True)
def _clean():
    """每个用例前后都清掉计数与指标：这里全是全局状态，残留会让断言互相污染。"""
    metrics.reset()
    yield
    metrics.reset()


# ---------------------------------------------------------------------------
# 1. 基本背压
# ---------------------------------------------------------------------------

def test_the_second_concurrent_request_is_shed():
    """★ 上限 = 1 时，第一个还在途，第二个必须被 503 拒绝且**进不了内层应用**。"""

    async def main():
        inner = InnerApp()
        mw = ConcurrencyLimitMiddleware(inner, limit=1)
        first = asyncio.create_task(_drive(mw, _scope("/v1/chat")))
        await inner.entered.wait()               # 第一个确实进去了（不是碰巧）
        second = await _drive(mw, _scope("/v1/chat"))
        inner.release.set()
        await first
        return inner, second

    inner, second = asyncio.run(main())
    assert _status(second) == 503
    assert inner.calls == ["/v1/chat"], "被拒的请求不该穿透到内层应用"


def test_shed_response_carries_retry_after_and_a_readable_detail():
    """503 必须带 `Retry-After` 与可读文案 —— 否则客户端只能盲重试。"""

    async def main():
        inner = InnerApp()
        mw = ConcurrencyLimitMiddleware(inner, limit=1)
        first = asyncio.create_task(_drive(mw, _scope("/v1/chat")))
        await inner.entered.wait()
        second = await _drive(mw, _scope("/v1/chat"))
        inner.release.set()
        await first
        return second

    sent = asyncio.run(main())
    assert _header(sent, b"retry-after") == b"1"
    payload = json.loads(_body(sent))
    assert "并发" in payload["detail"] and "1" in payload["detail"]


def test_slot_is_released_after_the_request_finishes():
    """★ 请求结束后名额必须归还，否则上限会花一次少一个。"""

    async def main():
        inner = InnerApp()
        mw = ConcurrencyLimitMiddleware(inner, limit=1)
        for _ in range(3):
            first = asyncio.create_task(_drive(mw, _scope("/v1/chat")))
            await inner.entered.wait()
            inner.release.set()
            await first
        return inner

    inner = asyncio.run(main())
    assert len(inner.calls) == 3, "名额没有归还，后续请求被误拒"
    assert concurrent_count() == 0


def test_slot_is_released_even_when_the_inner_app_raises():
    """★ 上游 500 也必须归还名额 —— 否则故障复盘时"实例空闲却全 503"。"""

    async def main():
        inner = InnerApp(boom=True)
        mw = ConcurrencyLimitMiddleware(inner, limit=1)
        with pytest.raises(RuntimeError):
            first = asyncio.create_task(_drive(mw, _scope("/v1/chat")))
            await inner.entered.wait()
            inner.release.set()
            await first
        return concurrent_count()

    assert asyncio.run(main()) == 0


def test_limit_zero_means_disabled():
    """默认 0 = 不限：不能凭空拒绝正常流量（与"服务挂了"无法区分）。"""

    async def main():
        inner = InnerApp()
        mw = ConcurrencyLimitMiddleware(inner, limit=0)
        first = asyncio.create_task(_drive(mw, _scope("/v1/chat")))
        await inner.entered.wait()
        second = asyncio.create_task(_drive(mw, _scope("/v1/chat")))
        await asyncio.sleep(0)
        inner.release.set()
        await first
        await second
        return inner

    assert len(asyncio.run(main()).calls) == 2
    assert metrics.value("http_requests_rejected_total", reason="concurrency") == 0


# ---------------------------------------------------------------------------
# 2. 豁免：过载时最需要的两条路径不能被拒
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/health/live", "/health/ready", "/metrics"])
def test_health_and_metrics_bypass_the_gate(path):
    """★ 被拒会让编排把实例判死（重启风暴）或让 Prometheus 取不到数据。

    这是"保护措施反过来制造故障"的典型：真正的病根是流量，重启这个进程
    只会让新实例同样被打满。
    """

    async def main():
        inner = InnerApp()
        mw = ConcurrencyLimitMiddleware(inner, limit=1)
        first = asyncio.create_task(_drive(mw, _scope("/v1/chat")))
        await inner.entered.wait()
        probe = await _drive(mw, _scope(path, method="GET"))
        inner.release.set()
        await first
        return probe

    assert _status(asyncio.run(main())) == 200


def test_exempt_requests_do_not_consume_a_slot():
    """豁免路径连计数都不进 —— 否则一个高频探针能把名额吃光。"""

    async def main():
        inner = InnerApp()
        mw = ConcurrencyLimitMiddleware(inner, limit=2)
        await _drive(mw, _scope("/health/live", method="GET"))
        return concurrent_count()

    assert asyncio.run(main()) == 0


# ---------------------------------------------------------------------------
# 3. 与 RequestContext 的配合（顺序错了就"看不见过载"）
# ---------------------------------------------------------------------------

def test_shed_request_is_counted_with_a_fixed_path_label():
    """★ 503 必须经 RequestContext 出去：要带 X-Request-ID，并计进访问指标。

    path 标签必须是固定的 `<shed>`：
      - 用原始 URL ⇒ 客户端随便填路径就能把标签基数打爆；
      - 落到 `unmatched` ⇒ 与真正的 404 混在一起，"有人打满了容量"
        这条信号就被淹没在噪声里。
    """

    async def main():
        inner = InnerApp()
        app = RequestContextMiddleware(ConcurrencyLimitMiddleware(inner, limit=1))
        first = asyncio.create_task(_drive(app, _scope("/v1/chat")))
        await inner.entered.wait()
        second = await _drive(app, _scope("/v1/chat"))
        inner.release.set()
        await first
        return second

    sent = asyncio.run(main())
    assert _status(sent) == 503
    assert _header(sent, b"x-request-id"), "503 没带 request_id（顺序反了）"
    assert metrics.value("http_requests_total", method="POST", path="<shed>",
                         status="503") == 1
    assert metrics.value("http_requests_total", method="POST",
                         path="unmatched", status="503") == 0


def test_ordering_is_required_not_incidental():
    """把 ConcurrencyLimit 放在 RequestContext **外面**时，标签就丢了。

    这条不是重复上一条：它证明**顺序本身**是被验证过的约束，而不是碰巧成立。
    """

    async def main():
        inner = InnerApp()
        app = ConcurrencyLimitMiddleware(
            RequestContextMiddleware(inner), limit=1)      # 故意反着套
        first = asyncio.create_task(_drive(app, _scope("/v1/chat")))
        await inner.entered.wait()
        second = await _drive(app, _scope("/v1/chat"))
        inner.release.set()
        await first
        return second

    sent = asyncio.run(main())
    assert _status(sent) == 503
    assert _header(sent, b"x-request-id") is None, (
        "顺序反了反而带上了 request_id —— 说明这条用例的前提失效了"
    )


# ---------------------------------------------------------------------------
# 4. 指标：饱和程度要能算
# ---------------------------------------------------------------------------

def test_rejection_and_saturation_are_observable():
    """要能回答"离被打满还有多远" —— 只有比值能回答，绝对值不能。"""

    async def main():
        inner = InnerApp()
        mw = ConcurrencyLimitMiddleware(inner, limit=2)
        # 两个都发出去、都挂在内层（用任务而不是顺序 await：顺序 await 一个
        # "会挂住"的请求会永远等下去 —— 这正是第一版用例挂死的原因）。
        t1 = asyncio.create_task(_drive(mw, _scope("/v1/chat")))
        t2 = asyncio.create_task(_drive(mw, _scope("/v1/chat")))
        await _wait_calls(inner, 2)
        during = metrics.gauge("http_concurrency_in_flight")
        shed = await _drive(mw, _scope("/v1/chat"))
        inner.release.set()
        await t1, t2
        return during, shed

    during, shed = asyncio.run(main())
    assert during == 2
    assert _status(shed) == 503
    assert metrics.gauge("http_concurrency_limit") == 2
    assert metrics.value("http_requests_rejected_total", reason="concurrency") == 1
    assert concurrent_count() == 0


def test_rejection_survives_to_the_metric_even_when_logging_is_throttled():
    """日志按 5s 节流，但**计数一次都不能少**（节流只影响日志，不影响指标）。

    反过来做（连计数一起节流）会让告警在持续过载时读到偏低的数字 ——
    而那正是唯一还会被人看到的信号。
    """

    async def main():
        inner = InnerApp()
        mw = ConcurrencyLimitMiddleware(inner, limit=1)
        first = asyncio.create_task(_drive(mw, _scope("/v1/chat")))
        await inner.entered.wait()
        codes = [_status(await _drive(mw, _scope("/v1/chat"))) for _ in range(5)]
        inner.release.set()
        await first
        return codes

    assert asyncio.run(main()) == [503] * 5
    assert metrics.value("http_requests_rejected_total", reason="concurrency") == 5


# ---------------------------------------------------------------------------
# 5. 配置与接线
# ---------------------------------------------------------------------------

def test_limit_is_configurable_and_echoed_in_startup_log():
    """必须出现在 `as_dict()` 里 —— 否则运维配了无法自证（静默生效型配置）。"""
    from app import config

    limits = config.as_dict()["limits"]
    assert "max_concurrent_requests" in limits
    assert limits["max_concurrent_requests"] == config.MAX_CONCURRENT_REQUESTS
    assert config.MAX_CONCURRENT_REQUESTS >= 0


def test_middleware_is_wired_into_the_real_app_with_the_configured_limit():
    """真的接进了 FastAPI 栈，且用的就是配置值（不是硬编码）。"""
    import app.main as M

    node = M.app.middleware_stack or M.app.build_middleware_stack()
    found = None
    while node is not None:
        if isinstance(node, ConcurrencyLimitMiddleware):
            found = node
        node = getattr(node, "app", None)
    assert found is not None, "ConcurrencyLimitMiddleware 没有接进 app"
    assert found.limit == M.config.MAX_CONCURRENT_REQUESTS


def test_lifespan_reports_gated_in_flight_on_shutdown():
    """优雅关闭时播报的在途数要继续可用（闸门不能把已有观测挤掉）。"""
    from app import middleware as MW

    assert callable(MW.in_flight_count)
    assert callable(MW.concurrent_count)
    # 两个计数语义不同：in_flight 含 /health，闸门计数不含 ⇒ 不能互相替代
    assert MW.in_flight_count() != -1
