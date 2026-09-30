"""身份与出口收紧的测试：租户绑定、会话归属、启动策略、文档开关、断连止损。

这一组测试针对的是 2026-09-28 审计出的真实越权/漏钱路径，所以每条都
**刻意断言到"删掉实现就会红"的粒度**：
  - 租户自报 → 必须 403（不是"忽略了就算"）
  - 断连 → 必须不再下发 done、不再查状态、上游必须被 aclose
  - 无 Key + REQUIRE_AUTH → 必须拒绝启动

全部离线，不打真实模型/数据库。
"""
import asyncio
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

os.environ["DB_URL"] = ""

pytest.importorskip("fastapi.testclient", reason="TestClient 需要 httpx")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as M  # noqa: E402
from app.sessions import SessionRegistry  # noqa: E402

client = TestClient(M.app)


# ---------------------------------------------------------------------------
# 辅助：假对象
# ---------------------------------------------------------------------------
class _FakeStream:
    """模拟 langgraph 的 astream 生成器：可异步迭代、可 aclose、记录是否被关闭。

    delay: 每个 chunk 之间的间隔（秒）。用来模拟"模型在慢慢吐字"，
        让"断连后停止产出"这件事在测试里可观测（否则纯 CPU 循环瞬间跑完，
        断连检测根本没机会赢）。
    """

    def __init__(self, chunks, delay=0.0):
        self._chunks = list(chunks)
        self._delay = delay
        self._i = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._i >= len(self._chunks):
            raise StopAsyncIteration
        item = self._chunks[self._i], {}
        self._i += 1
        return item

    async def aclose(self):
        self.closed = True


class _StallingStream:
    """永不产出 chunk 的流：模拟上游卡住/长时间重试（实测踩过的场景）。

    **关键**：`aclose()` 必须真的让挂起的 `__anext__` 结束 —— 这才是真实
    async generator 的语义（aclose 向挂起点抛 GeneratorExit，迭代方随即收到
    StopAsyncIteration）。如果 aclose 只设个标志而不唤醒等待者，就测不出
    "取消能不能传导下去"，而会让用例卡在超时上。
    """

    def __init__(self):
        self.closed = False
        self._released = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        await self._released.wait()         # 一直挂起，直到被关闭
        raise StopAsyncIteration

    async def aclose(self):
        self.closed = True
        self._released.set()                # 唤醒挂起的迭代方


class _FakeGraph:
    """假图：astream 返回给定 chunk 序列，aget_state 记录是否被访问。"""

    def __init__(self, chunks, delay=0.0):
        self.stream = _FakeStream(chunks, delay=delay)
        self.state_queried = False

    def astream(self, incoming, config, stream_mode):
        return self.stream

    async def aget_state(self, config):
        self.state_queried = True
        return SimpleNamespace(next=(), values={"citations": []})


class _StallGraph:
    """假图：上游永远不产出（用于验证"上游卡住时断连仍能被检测"）。"""

    def __init__(self):
        self.stream = _StallingStream()
        self.state_queried = False

    def astream(self, incoming, config, stream_mode):
        return self.stream

    async def aget_state(self, config):
        self.state_queried = True
        return SimpleNamespace(next=(), values={"citations": []})


class _FakeHTTPRequest:
    """假 Starlette Request，只提供 _client_ip / url.path 需要的字段。

    headers / peer：用来验证"到底按哪个 IP 计限流"——`headers` 放
        `x-forwarded-for`，`peer` 放直连地址，两者可同时给，看实现选了谁。
    """

    class _URL:
        path = "/v1/chat"

    def __init__(self, headers=None, peer="127.0.0.1"):
        self.headers = headers or {}
        self._peer = peer
        self.url = self._URL()

        class _Client:
            host = peer

        self.client = _Client()


async def _drain(gen, timeout=20):
    """把一个异步生成器跑完，收集所有事件。"""
    async def run():
        return [ev async for ev in gen]
    return await asyncio.wait_for(run(), timeout)


def _collect(gen, timeout=20):
    """把一个异步生成器跑完，收集所有事件。

    **带超时**：断连检测失效时应当让用例失败，而不是永远挂住 ——
    "测试卡死"比"测试失败"难排查得多。
    """
    return asyncio.run(_drain(gen, timeout))


async def _trip_after(signal, delay):
    """延迟 `delay` 秒后置位断连信号（等价于"客户端此刻拔线"）。"""
    await asyncio.sleep(delay)
    await signal.mark_closed({"type": "http.disconnect"})


def _collect_with_disconnect(make_gen, trip_after, timeout=20):
    """跑生成器，并在 `trip_after` 秒后触发"客户端已断开"。

    为什么不再用 `request.is_disconnected()` 返回 True 的假对象
    --------------------------------------------------------
    那条路在真实链路上**永远走不到**：sse_starlette 的 `_listen_for_disconnect`
    常驻独占消费 `receive()`，并在拿到 `http.disconnect` 后立刻取消整个 task
    group —— 轮询任务连下一次 sleep 都等不到。用假 request 去测它，
    测的是"我们自己的 mock 会返回 True"，而不是产品行为（**假绿**）。
    现在改走真实的置位路径（`mark_closed`），断连的端到端覆盖见
    `tests/test_disconnect.py`（真实 ASGI 栈 + 真实 `http.disconnect`）。

    make_gen: 接收 signal、返回异步生成器的可调用对象。
    """
    async def main():
        signal = M._DisconnectSignal("chat")
        tripper = asyncio.create_task(_trip_after(signal, trip_after))
        try:
            return await _drain(make_gen(signal), timeout)
        finally:
            tripper.cancel()
            try:
                await tripper
            except asyncio.CancelledError:
                pass
    return asyncio.run(main())


# ---------------------------------------------------------------------------
# 租户绑定：租户只能由 Key 推导
# ---------------------------------------------------------------------------
def test_tenant_keys_resolves_tenant_from_key(monkeypatch):
    monkeypatch.setattr(M.config, "TENANT_KEYS", {"k-a": "acme", "k-b": "globex"})
    assert M._authenticate("k-a", "").tenant == "acme"
    assert M._authenticate("k-b", "").tenant == "globex"


def test_tenant_keys_rejects_unknown_key(monkeypatch):
    monkeypatch.setattr(M.config, "TENANT_KEYS", {"k-a": "acme"})
    with pytest.raises(HTTPException) as ei:
        M._authenticate("not-a-key", "")
    assert ei.value.status_code == 401


def test_tenant_keys_rejects_self_declared_other_tenant(monkeypatch):
    """**核心用例**：拿着 acme 的 Key 自称 globex —— 这就是越权尝试，必须 403。

    改回旧实现（信任请求头）时这条会变成 200/通过 ⇒ 红。
    """
    monkeypatch.setattr(M.config, "TENANT_KEYS", {"k-a": "acme"})
    with pytest.raises(HTTPException) as ei:
        M._authenticate("k-a", "globex")
    assert ei.value.status_code == 403


def test_tenant_keys_accepts_matching_claim(monkeypatch):
    """自报值与自己一致时不该被拒（避免把正常客户端误伤）。"""
    monkeypatch.setattr(M.config, "TENANT_KEYS", {"k-a": "acme"})
    ident = M._authenticate("k-a", "acme")
    assert ident.tenant == "acme" and ident.via == "tenant-key"


def test_compat_mode_still_honors_header(monkeypatch):
    """未配 TENANT_KEYS 时保持旧行为（单租户/本地开发）。

    这是**已知且已告警**的降级路径，不是遗漏 —— 单租户下所有请求本就是同一租户。
    """
    monkeypatch.setattr(M.config, "TENANT_KEYS", {})
    monkeypatch.setattr(M, "SERVE_API_KEY", "global-key")
    ident = M._authenticate("global-key", "any-tenant")
    assert ident.tenant == "any-tenant" and ident.via == "global-key"


def test_chat_endpoint_returns_403_on_tenant_mismatch(monkeypatch):
    """走完整 HTTP 栈确认端点真的接上了新鉴权（而不是只测了函数）。"""
    monkeypatch.setattr(M.config, "TENANT_KEYS", {"k-a": "acme"})
    r = client.post("/v1/chat", json={"thread_id": "t-mismatch", "message": "hi"},
                    headers={"X-API-Key": "k-a", "X-Tenant-Id": "globex"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 启动期策略：无 Key 时拒绝启动
# ---------------------------------------------------------------------------
def test_require_auth_rejects_startup_without_any_key(monkeypatch):
    """REQUIRE_AUTH=1 且没有任何 Key ⇒ 必须 raise（起不来好过裸奔接流量）。"""
    monkeypatch.setattr(M.config, "REQUIRE_AUTH", True)
    monkeypatch.setattr(M.config, "TENANT_KEYS", {})
    monkeypatch.setattr(M, "SERVE_API_KEY", "")
    with pytest.raises(RuntimeError):
        M._assert_auth_policy()


def test_auth_policy_warns_when_only_global_key(monkeypatch, caplog):
    """只配全局 Key 时必须点名"租户可被冒充"，不能静默。"""
    monkeypatch.setattr(M.config, "REQUIRE_AUTH", False)
    monkeypatch.setattr(M.config, "TENANT_KEYS", {})
    monkeypatch.setattr(M, "SERVE_API_KEY", "global")
    with caplog.at_level("WARNING"):
        M._assert_auth_policy()
    assert any("冒充" in r.message for r in caplog.records)


def test_auth_policy_silent_when_tenant_keys_configured(monkeypatch, caplog):
    """配了 TENANT_KEYS 就该安静（没有需要警告的自报路径）。"""
    monkeypatch.setattr(M.config, "REQUIRE_AUTH", True)
    monkeypatch.setattr(M.config, "TENANT_KEYS", {"k-a": "acme"})
    with caplog.at_level("WARNING"):
        M._assert_auth_policy()
    assert not [r for r in caplog.records if "冒充" in r.message or "对任何人开放" in r.message]


# ---------------------------------------------------------------------------
# 会话归属：thread_id 不能跨租户复用
# ---------------------------------------------------------------------------
def test_session_registry_blocks_cross_tenant_reuse():
    reg = SessionRegistry(dsn="", enabled=True)
    assert reg.claim("s1", "acme") == (True, "acme")
    ok, owner = reg.claim("s1", "globex")
    assert ok is False and owner == "acme"


def test_session_registry_same_tenant_reuse_is_fine():
    reg = SessionRegistry(dsn="", enabled=True)
    reg.claim("s1", "acme")
    assert reg.claim("s1", "acme") == (True, "acme")


def test_session_registry_disabled_passes_everything():
    """单租户模式（未配 TENANT_KEYS）不做区分 —— 否则会平白误伤。"""
    reg = SessionRegistry(dsn="", enabled=False)
    assert reg.claim("s1", "acme")[0] is True
    assert reg.claim("s1", "globex")[0] is True


def test_session_registry_memory_is_bounded():
    """内存模式必须有界：超限后停止登记但仍放行（安全降级，不阻断服务）。"""
    reg = SessionRegistry(enabled=True)
    reg.MAX_ENTRIES = 3
    for i in range(6):
        assert reg.claim(f"s{i}", "acme")[0] is True
    assert reg.count() == 3


def test_preflight_rejects_cross_tenant_session(monkeypatch):
    reg = SessionRegistry(enabled=True)
    reg.claim("shared-thread", "acme")
    monkeypatch.setattr(M, "_SESSIONS", reg)
    with pytest.raises(HTTPException) as ei:
        M._preflight(_FakeHTTPRequest(), session="shared-thread", tenant="globex")
    assert ei.value.status_code == 403


# ---------------------------------------------------------------------------
# 出口收紧：恒定时间比较 + 异常文案脱敏
# ---------------------------------------------------------------------------
def test_global_key_compare_is_constant_time(monkeypatch):
    """全局 Key 的比较必须走 `hmac.compare_digest`。

    为什么不写"行为断言"：`!=` 与 `compare_digest` 的**返回值完全一致**，
    任何黑盒用例都无法区分它们 —— 所以这里退而求其次，断言"确实调用了它"。
    这是一条**绊线**：把它改回 `!=` 时本用例会红。

    为什么值得钉：`!=` 在第一个不同的字节就返回，攻击者逐字节试错能靠响应时间
    反推密钥前缀（timing oracle）。网络抖动让它很难利用，但修它只要一行。
    """
    import hmac as _hmac

    calls = []
    real = _hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(M.hmac, "compare_digest", spy)
    monkeypatch.setattr(M, "SERVE_API_KEY", "sekret")
    monkeypatch.setattr(M.config, "TENANT_KEYS", {})

    assert M._authenticate("sekret", "").via == "global-key"
    assert len(calls) == 1 and calls[0] == ("sekret", "sekret")

    with pytest.raises(HTTPException) as ei:
        M._authenticate("wrong", "")
    assert ei.value.status_code == 401
    assert len(calls) == 2         # 错误分支也必须走同一条比较路径


def test_client_error_hides_internals_in_production(monkeypatch):
    """生产环境不能把异常的原始消息发给客户端（里面常有主机名/路径/上游 URL）。"""
    monkeypatch.setattr(M.config, "IS_PRODUCTION", True)
    exc = RuntimeError('connection to server at "db.internal" (10.0.0.5), port 5432 '
                       'failed: password authentication failed for user "app"')
    msg = M._client_error(exc)
    assert "db.internal" not in msg and "password" not in msg and "10.0.0.5" not in msg
    assert "RuntimeError" not in msg


def test_client_error_keeps_detail_in_development(monkeypatch):
    """开发环境保留细节 —— 否则本地排障只能翻日志。"""
    monkeypatch.setattr(M.config, "IS_PRODUCTION", False)
    msg = M._client_error(ValueError("boom"))
    assert msg == "ValueError: boom"


def test_client_error_includes_request_id_in_production(monkeypatch):
    """带上 request_id：信息没丢（运维可凭它查到完整堆栈），只是没泄露给外面。"""
    from app.logging_setup import set_request_id, reset_request_id

    monkeypatch.setattr(M.config, "IS_PRODUCTION", True)
    token = set_request_id("rid-abc123")
    try:
        assert "rid-abc123" in M._client_error(RuntimeError("内部细节"))
    finally:
        reset_request_id(token)


def test_stream_error_event_is_sanitized_in_production(monkeypatch):
    """**接线测试**：`_stream_graph` 下发的 error 事件必须用脱敏文案。

    上面三条只调 `_client_error()` —— 它们测的是"零件"，不是"零件有没有被装上"。
    实测教训：把 `_stream_graph` 里的 `_client_error(e)` 改回 `str(e)` 时，
    那三条**照样全绿**（断言根本没走到调用点）。所以必须有一条从异常源头
    走到 SSE 事件的用例，否则这层保护可以被静默摘掉。
    """
    class _BoomGraph:
        async def astream(self, incoming, config, stream_mode):
            raise RuntimeError(
                'connection to server at "db.internal" (10.0.0.5), port 5432 failed'
            )
            yield  # pragma: no cover  （让它成为异步生成器）

        async def aget_state(self, config):  # pragma: no cover
            raise AssertionError("异常路径不该查状态")

    monkeypatch.setattr(M.config, "IS_PRODUCTION", True)
    monkeypatch.setattr(M, "graph", _BoomGraph())

    events = _collect(M._stream_graph({}, {}, "chat", tenant="t1"))
    assert [e["event"] for e in events] == ["error"], events
    payload = events[0]["data"]
    assert "db.internal" not in payload and "10.0.0.5" not in payload
    assert "RuntimeError" not in payload
    assert "服务内部错误" in payload



# 这个开关两个方向都是真实故障，且配错时**不报错、只是静默算错 IP**：
#   - 反代后设 0 → 全体用户共用代理 IP 一个桶，限流误伤/被单人吃满；
#   - 直连时设 1 → 客户端加一个 X-Forwarded-For 就换桶，限流形同虚设。
# 所以必须钉住"默认不信任"，那是防伪造的那一侧。
# ---------------------------------------------------------------------------
def test_client_ip_ignores_forwarded_for_by_default(monkeypatch):
    """默认（TRUST_PROXY_HEADERS 未开）必须**忽略** X-Forwarded-For。

    这是防伪造的关键方向：改成"默认信任"时本条变红。
    """
    monkeypatch.setattr(M.config, "TRUST_PROXY_HEADERS", False)
    req = _FakeHTTPRequest(headers={"x-forwarded-for": "1.2.3.4"}, peer="10.0.0.9")
    assert M._client_ip(req) == "10.0.0.9"


def test_client_ip_uses_forwarded_for_when_trusted(monkeypatch):
    """TRUST_PROXY_HEADERS=1 时必须取 XFF **第一段**（代理链里最左才是真实来源）。"""
    monkeypatch.setattr(M.config, "TRUST_PROXY_HEADERS", True)
    req = _FakeHTTPRequest(headers={"x-forwarded-for": "1.2.3.4, 10.0.0.9"},
                           peer="10.0.0.9")
    assert M._client_ip(req) == "1.2.3.4"


def test_client_ip_falls_back_when_trusted_but_header_absent(monkeypatch):
    """开了信任但请求没带 XFF（直连探针、健康检查）也要能取到 IP，不能返回空串。

    返回空串会让所有无头请求挤进同一个限流桶。
    """
    monkeypatch.setattr(M.config, "TRUST_PROXY_HEADERS", True)
    req = _FakeHTTPRequest(headers={}, peer="10.0.0.9")
    assert M._client_ip(req) == "10.0.0.9"


def test_config_exposes_proxy_trust_in_health(monkeypatch):
    """该开关必须出现在 as_dict() 里 —— 否则运维配了无法自证（它是静默生效型配置）。"""
    assert "proxy" in M.config.as_dict()
    assert M.config.as_dict()["proxy"]["trust_forwarded_for"] in (True, False)


def test_proxy_trust_defaults_to_off_when_unset():
    """**默认值必须在"不信任"这一侧** —— 默认信任 = 任何客户端可伪造 IP 绕过限流。

    用子进程直读 `app.config`：刻意**不经 app.main**（后者会 load_dotenv()，
    把 .env 里的值混进来），这样得到的才是"环境里没写这个变量时"的真实默认值。
    把默认改成 True 时本条变红。
    """
    env = {k: v for k, v in os.environ.items() if k != "TRUST_PROXY_HEADERS"}
    out = subprocess.run(
        [sys.executable, "-c", "import app.config as c; print(c.TRUST_PROXY_HEADERS)"],
        cwd=os.getcwd(), env=env, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "False"


# ---------------------------------------------------------------------------
# 文档出口：开关真的能关掉三个端点
# ---------------------------------------------------------------------------
def test_docs_kwargs_empty_when_enabled():
    assert M._docs_kwargs(True) == {}


def test_docs_kwargs_disable_all_three_urls():
    kw = M._docs_kwargs(False)
    assert kw == {"docs_url": None, "redoc_url": None, "openapi_url": None}


def test_docs_endpoints_404_when_disabled():
    """确认 FastAPI 在三个 url 都为 None 时确实返回 404（而不是留个空页面）。"""
    from fastapi import FastAPI

    probe = TestClient(FastAPI(**M._docs_kwargs(False)))
    assert probe.get("/docs").status_code == 404
    assert probe.get("/redoc").status_code == 404
    assert probe.get("/openapi.json").status_code == 404


def test_production_env_turns_docs_off_by_default():
    """子进程验证"APP_ENV=production 时默认关文档"这条推导真的成立。"""
    env = {**os.environ, "APP_ENV": "production", "SERVE_API_KEY": "x", "DB_URL": ""}
    code = "import app.main as M; print(M.app.docs_url, M.app.openapi_url, M.config.ENABLE_DOCS)"
    out = subprocess.run([sys.executable, "-c", code], cwd=os.getcwd(), env=env,
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    # 启动日志也写 stdout，所以只看最后一行（print 在 import 之后执行）
    assert out.stdout.strip().splitlines()[-1] == "None None False"


# ---------------------------------------------------------------------------
# 断连止损：客户端走了就别再烧 token
# ---------------------------------------------------------------------------
def test_client_disconnect_stops_generation(monkeypatch):
    """断开后必须：① 停止产出；② 不再下发 done；③ 不再查状态（也就不入队）。

    破坏 `_watch_disconnect` 或 `_DisconnectSignal` 时这条会红。
    """
    from app import metrics

    metrics.reset()
    chunks = [SimpleNamespace(content=f"段{i}") for i in range(40)]
    g = _FakeGraph(chunks, delay=0.02)          # 每 chunk 20ms，模拟慢速生成
    monkeypatch.setattr(M, "graph", g)

    events = _collect_with_disconnect(
        lambda sig: M._stream_graph({}, {}, "chat", tenant="t1", disconnect=sig),
        trip_after=0.05)                        # 约 2~3 个 chunk 后断开
    names = [e["event"] for e in events]

    assert names.count("token") < len(chunks), f"断开后仍把全部 chunk 产出了：{names}"
    assert "done" not in names, f"已断开不该下发收尾事件：{names}"
    assert g.state_queried is False, "已断开却仍去查状态（会白白入队）"
    # 指标是"止损真的发生了"的唯一线上证据；修复前它是死代码（恒为 0）
    assert metrics.value("client_disconnected_total", kind="chat") == 1


def test_client_disconnect_closes_upstream_stream(monkeypatch):
    """必须显式 aclose 上游流：不关的话底层 LLM 调用会一直跑到 GC 为止。

    去掉 `await stream.aclose()` 时这条红。
    """
    g = _FakeGraph([SimpleNamespace(content=f"段{i}") for i in range(10)], delay=0.02)
    monkeypatch.setattr(M, "graph", g)

    _collect_with_disconnect(
        lambda sig: M._stream_graph({}, {}, "chat", tenant="t1", disconnect=sig),
        trip_after=0.05)
    assert g.stream.closed is True, "上游生成器未被关闭，取消没有传导下去"


def test_disconnect_detected_even_when_upstream_stalls(monkeypatch):
    """**回归测试**：上游长时间不产出 chunk 时，断连仍必须被检测到。

    原实现把检测写在 `async for` 循环体内 ⇒ 只在"上游产出 chunk"时才转一圈。
    2026-09-28 实测中上游 429 重试了 36 秒，主循环一次都没转，客户端早走了
    服务端还在等，token 照付。改成独立 watcher 任务后才能覆盖这个场景。

    本用例还额外钉住一件事：**是 watcher 的 aclose 把卡住的流解开的**
    （卡住的 `__anext__` 只有被 aclose 唤醒才会结束），所以它同时也验证了
    "关闭能真正传导到上游"。
    退回"循环体内检测"的实现时，本用例会超时失败。
    """
    g = _StallGraph()
    monkeypatch.setattr(M, "graph", g)

    started = time.time()
    events = _collect_with_disconnect(
        lambda sig: M._stream_graph({}, {}, "chat", tenant="t1", disconnect=sig),
        trip_after=0.05, timeout=15)
    elapsed = time.time() - started

    assert elapsed < 5, f"上游卡住时断连未能及时中止（耗时 {elapsed:.1f}s）"
    assert events == [], f"卡住场景不该产出任何事件：{events}"
    assert g.stream.closed is True, "上游未被关闭，取消没有传导下去"
    assert g.state_queried is False


def test_disconnect_still_settles_quota(monkeypatch):
    """断连不等于免费：已经产出的部分仍要计入租户用量。"""
    from app import quota as Q

    q = Q.QuotaManager(daily_limit=0, request_limit=0)
    monkeypatch.setattr(M, "_QUOTA", q)

    g = _FakeGraph([SimpleNamespace(content="已经产出的内容")] * 20, delay=0.02)
    monkeypatch.setattr(M, "graph", g)

    _collect_with_disconnect(
        lambda sig: M._stream_graph({}, {}, "chat", tenant="tenant-x", disconnect=sig),
        trip_after=0.1)                         # 已产出若干 chunk 后才断
    assert q.store.get("tenant-x", Q._today()) > 0


def test_normal_run_without_disconnect_is_unchanged(monkeypatch):
    """不传 disconnect 时保持旧行为（CLI/历史调用方）—— 必须有 done 收尾。"""
    chunks = [SimpleNamespace(content="正常回答")]
    g = _FakeGraph(chunks)
    monkeypatch.setattr(M, "graph", g)

    events = _collect(M._stream_graph({}, {}, "chat", tenant="t1"))
    names = [e["event"] for e in events]
    assert names == ["token", "done"]
    assert g.stream.closed is True     # 正常结束也要关闭，避免连接泄漏


def test_close_error_does_not_break_accounting(monkeypatch):
    """关闭上游时抛异常，不该覆盖真实结果，也不该吞掉计费。"""
    from app import quota as Q

    class _BadClose(_FakeStream):
        async def aclose(self):
            raise RuntimeError("close boom")

    class _BadGraph(_FakeGraph):
        def __init__(self, chunks):
            super().__init__(chunks)
            self.stream = _BadClose(chunks)

    q = Q.QuotaManager(daily_limit=0, request_limit=0)
    monkeypatch.setattr(M, "_QUOTA", q)
    g = _BadGraph([SimpleNamespace(content="内容")])
    monkeypatch.setattr(M, "graph", g)

    events = _collect(M._stream_graph({}, {}, "chat", tenant="t-bad"))
    assert [e["event"] for e in events] == ["token", "done"]
    assert q.store.get("t-bad", Q._today()) > 0
