"""身份与出口收紧的测试：鉴权口径、会话归属、启动策略、文档开关、断连止损。

这一组测试针对的是 2026-09-28 审计出的真实越权/漏钱路径，所以每条都
**刻意断言到"删掉实现就会红"的粒度**：
  - 缺凭证 → 必须 401（不是"忽略了就算"）
  - 断连 → 必须不再下发 done、不再查状态、上游必须被 aclose
  - 环境里残留已删除的鉴权开关 → 必须拒绝启动

⚠️ 口径收敛（2026-10-04 → 2026-10-05）：租户绑定到 API Key（`TENANT_KEYS`）的
整条路径先被移除，随后**共享密钥脚本通行证（`SERVE_API_KEY` / `X-API-Key`）与
"匿名放行"开关（`REQUIRE_AUTH`）也一并删除**。现在服务端只有一种身份来源：
账号密码 / 外部 IdP → JWT。下面 `test_legacy_auth_knobs_refuse_startup` 与
"`app.main` 不许再出现 `SERVE_API_KEY`"那两条，钉的就是"删除"本身。

全部离线，不打真实模型/数据库。
"""
import asyncio
import os
import subprocess
import sys
import time

import pytest

os.environ["DB_URL"] = ""

pytest.importorskip("fastapi.testclient", reason="TestClient 需要 httpx")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as M  # noqa: E402
from app.sessions import SessionError, SessionRegistry  # noqa: E402
from tests.auth_util import auth_headers  # noqa: E402

client = TestClient(M.app, headers=auth_headers())


# ---------------------------------------------------------------------------
# 辅助：假对象
# ---------------------------------------------------------------------------
class _FakeTurn:
    """替身运行：`chunks` 逐段交给 `on_text`，可加延迟 / 卡住 / 直接抛错。

    替换的是 ``app.main.run_turn``。`closed` 记录"上游那次运行有没有被收掉"——
    它在 `finally` 里置位，所以"取消真的传导下去了"这件事才可断言。
    """

    def __init__(self, chunks=(), delay=0.0, *, stall=False, boom=None):
        self.chunks = list(chunks)
        self.delay = delay
        self.stall = stall
        self.boom = boom
        self.closed = False
        self.cancelled = False

    async def __call__(self, req, *, context, on_text=None):
        from src.agents.runner import TaskOutcome

        try:
            if self.boom is not None:
                raise self.boom
            if self.stall:
                # 上游卡住：一直等，直到被取消（真实场景是限流重试 / 排队）。
                await asyncio.sleep(3600)
            for chunk in self.chunks:
                if self.delay:
                    await asyncio.sleep(self.delay)
                if on_text is not None:
                    await on_text(getattr(chunk, "content", chunk), "call-1")
            return TaskOutcome(answer="", attempts=1, final_agent="frontline_agent",
                               passed=True, exit_reason="passed")
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.closed = True


class _TurnRaisesOnShutdown(_FakeTurn):
    """取消时在 `finally` 里抛异常 —— 模拟"上游运行在收尾期报错"。

    真实形态：底层 HTTP 流被 cancel 时，传输层先在收尾里抛一句
    "connection closed"，然后才轮到取消本身。它**不该**污染这一轮的结论
    （正文已经发出去、配额已经该结算），也不该冒出一个 error 事件。

    分段产出（而不是只产一段就睡）：断连判定发生在**每次迭代的开头**，
    上游若只产一段就卡住，主循环会停在"等下一段"上，断连根本没机会生效。
    """

    async def __call__(self, req, *, context, on_text=None):
        try:
            for i in range(50):
                await asyncio.sleep(0.02)
                await on_text(f"段{i}", "call-1")
        finally:
            self.closed = True
            raise RuntimeError("shutdown boom")


def _stream(turn, monkeypatch, *, tenant="t1", thread_id="t-1", disconnect=None,
            timeout=0):
    """构造一次 `_stream_run`：把替身运行装上，返回它的 SSE 事件生成器。"""
    monkeypatch.setattr(M, "run_turn", turn)
    return M._stream_run(M.TurnRequest(prompt="hi", thread_id=thread_id),
                         "chat", tenant=tenant, thread_id=thread_id,
                         disconnect=disconnect, timeout=timeout)


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
# 鉴权入口：**只有一条路**（已验签的 JWT），没有任何兜底档
# ---------------------------------------------------------------------------
# 2026-10-05 删除了两档兜底：
#   · 共享密钥 `SERVE_API_KEY` / `X-API-Key`（分不出身份、撤不掉、租户可自报）
#   · `REQUIRE_AUTH=0` 时的匿名放行（完全不带凭证也能调所有业务接口）
# 下面这组用例钉住"它们真的没了"，而不是"改成了别的写法"：
# 用**签名断言**与**行为断言**两种方式，因为前者能拦住"参数位又被人加回去"这种
# 静默复活。
def test_no_credential_is_rejected_with_401(monkeypatch):
    """没有任何凭证 ⇒ 401（不是 503，也不是匿名放行）。

    为什么不是 503：这是**客户端**没登录，不是服务端故障。503 会被计入服务端错误、
    污染 SLO 错误预算 —— "用户忘了登录"在监控上看起来像"服务挂了"。
    """
    with pytest.raises(HTTPException) as ei:
        M._authenticate("")
    assert ei.value.status_code == 401
    assert "缺少凭证" in ei.value.detail


def test_authenticate_has_no_shared_key_parameter():
    """`_authenticate` 的第一个参数是 `X-Tenant-Id`，**不是** `X-API-Key`。

    为什么用签名断言而不是行为断言：行为上"删掉共享密钥档"与"把比较改成恒 False"
    无法区分，而后者等于代码里还留着一条能被人重新打开的路。签名一旦变了，
    本用例立刻红 —— 这是绊线，不是形式。
    """
    import inspect

    params = list(inspect.signature(M._authenticate).parameters)
    assert params == ["x_tenant_id", "authorization"], (
        f"_authenticate 的签名变成了 {params} —— 若第一个参数又是 x_api_key，"
        "说明共享密钥档被加回来了"
    )
    assert not hasattr(M, "SERVE_API_KEY"), "共享密钥常量又回到了 app.main"


def test_tenant_header_can_only_confirm_not_declare(monkeypatch):
    """`X-Tenant-Id` **只能"确认"**：与令牌里的租户不符 ⇒ 403。

    它不再是"声明" —— 租户的唯一来源是已验签的载荷。这条把"自报被采信"这个
    曾经的例外彻底钉死（那个例外的代价是：持共享密钥者换一个请求头就能切检索分区）。
    """
    monkeypatch.setattr(M.config, "AUTH_ENABLED", True)

    def fake_user(bearer):
        assert bearer == "TOK"                  # 凭证确实被传了进来
        return M.Identity(tenant="token-tenant", user="alice")

    monkeypatch.setattr(M, "_authenticate_user", fake_user)
    with pytest.raises(HTTPException) as ei:
        M._authenticate("claimed-tenant", authorization="Bearer TOK")
    assert ei.value.status_code == 403
    assert "不匹配" in ei.value.detail


def test_tenant_header_absent_is_fine(monkeypatch):
    """不带 `X-Tenant-Id` 也能正常调用（它只是可选的一致性核对）。"""
    monkeypatch.setattr(M.config, "AUTH_ENABLED", True)
    monkeypatch.setattr(
        M, "_authenticate_user",
        lambda bearer: M.Identity(tenant="", user="alice"))
    ident = M._authenticate("", authorization="Bearer TOK")
    assert ident.tenant == ""


def test_bearer_rejected_when_user_auth_disabled(monkeypatch):
    """带了 Bearer 但服务没启用用户鉴权 ⇒ 明确 401。

    不能"忽略它、放行请求"：那会让客户端以为认证生效了，而实际没有任何身份。
    """
    monkeypatch.setattr(M.config, "AUTH_ENABLED", False)
    with pytest.raises(HTTPException) as ei:
        M._authenticate("", authorization="Bearer TOK")
    assert ei.value.status_code == 401
    assert "未启用用户鉴权" in ei.value.detail


def test_chat_endpoint_requires_credentials():
    """走完整 HTTP 栈确认端点真的接上了鉴权（而不是只测了函数）。"""
    from fastapi.testclient import TestClient as _TC

    r = _TC(M.app).post("/v1/chat", json={"thread_id": "t-noauth", "message": "hi"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 启动期策略：生产必须有用户鉴权；残留的旧开关必须拦住
# ---------------------------------------------------------------------------
def test_production_without_user_auth_refuses_to_start(monkeypatch):
    """生产环境没配用户鉴权 ⇒ **拒绝启动**。

    现象会是"服务活着、日志祥和、但没有任何人能用" —— 比起不来更糟，
    因为起不来至少会有人去查。
    """
    monkeypatch.setattr(M.config, "IS_PRODUCTION", True)
    monkeypatch.setattr(M.config, "AUTH_ENABLED", False)
    with pytest.raises(RuntimeError) as ei:
        M._assert_auth_policy()
    assert "用户鉴权" in str(ei.value)


def test_non_production_without_user_auth_starts_but_says_it_is_unusable(monkeypatch, caplog):
    """非生产、没配鉴权 ⇒ 允许启动（否则连导入 app 的工具脚本都跑不了），
    但必须用 ERROR 说明"所有业务请求都会 401"。

    这是删掉匿名兜底之后新增的一条"说清楚"：本地不配账号的服务**谁也服务不了**，
    沉默地起一个这样的进程是最容易浪费半小时的情形。
    """
    monkeypatch.setattr(M.config, "IS_PRODUCTION", False)
    monkeypatch.setattr(M.config, "AUTH_ENABLED", False)
    with caplog.at_level("ERROR"):
        M._assert_auth_policy()
    assert any("401" in r.message for r in caplog.records), (
        "未配鉴权时必须明确说'所有业务请求都会 401'，不能沉默"
    )


def test_auth_policy_is_quiet_when_user_auth_enabled(monkeypatch, caplog):
    """启用了用户鉴权 ⇒ 不该出现"服务不可用"那类 ERROR。"""
    monkeypatch.setattr(M.config, "AUTH_ENABLED", True)
    with caplog.at_level("ERROR"):
        M._assert_auth_policy()
    assert not [r for r in caplog.records if "401" in r.message]


def test_legacy_auth_knobs_refuse_startup(monkeypatch):
    """`.env` / 环境里残留 `SERVE_API_KEY` 或 `REQUIRE_AUTH` ⇒ **拒绝启动**。

    为什么不静默忽略（这是本组用例存在的理由）：这两个开关**曾经真的能改变鉴权
    结果**。运维看到变量还在、服务又照常起来，会合理地认为"这道门还开着"
    —— 而代码已经不读它了。这类"以为有、其实没有"的安全配置是最危险的形态：
    它不会以任何方式表现出来，直到有人从那个不存在的门走进来。
    """
    monkeypatch.setenv("SERVE_API_KEY", "leftover-from-old-env")
    monkeypatch.delenv("REQUIRE_AUTH", raising=False)
    with pytest.raises(RuntimeError) as ei:
        M._assert_no_legacy_auth_knobs()
    assert "SERVE_API_KEY" in str(ei.value)

    monkeypatch.delenv("SERVE_API_KEY")
    monkeypatch.setenv("REQUIRE_AUTH", "0")
    with pytest.raises(RuntimeError) as ei2:
        M._assert_no_legacy_auth_knobs()
    assert "REQUIRE_AUTH" in str(ei2.value)


def test_legacy_auth_knobs_guard_is_quiet_when_absent(monkeypatch):
    """两个开关都不在时，守卫必须**静默通过**（否则正常的服务起不来）。"""
    monkeypatch.delenv("SERVE_API_KEY", raising=False)
    monkeypatch.delenv("REQUIRE_AUTH", raising=False)
    M._assert_no_legacy_auth_knobs()


def test_oidc_knobs_refuse_startup_even_when_blank(monkeypatch):
    """环境里残留**任何** `AUTH_OIDC_` 开头的键（**留空也算**）⇒ **拒绝启动**。

    为什么留空也算：`AUTH_OIDC_JWKS_URI=` 与填了地址一样坏 —— 看到它的人同样会
    以为"这个外部身份源是配过的、这道门还开着"。这类"以为有、其实没有"的安全
    配置不会以任何方式表现出来，所以判据取"**键存在**"而不是"值非空"。

    为什么用**前缀族**而不是逐条列举：这一族有 10 个键（地址 / 受众 / 算法 /
    缓存 TTL / 时钟偏移 …）。逐条列举的守卫有个隐蔽失效模式 —— 漏掉的那一条
    没人会去数，于是"漏一个键"等价于"留一个口子"，而且没有任何症状。
    """
    monkeypatch.delenv("SERVE_API_KEY", raising=False)
    monkeypatch.delenv("REQUIRE_AUTH", raising=False)
    monkeypatch.setenv("AUTH_OIDC_JWKS_URI", "")
    with pytest.raises(RuntimeError) as ei:
        M._assert_no_legacy_auth_knobs()
    msg = str(ei.value)
    assert "AUTH_OIDC_JWKS_URI" in msg, "报错必须点名是哪个键残留了"
    # 文案必须点出"这一族"，否则运维删掉这一个键就以为完事了，而族里还有九个。
    assert "AUTH_OIDC_" in msg


@pytest.mark.parametrize("name", [
    "AUTH_OIDC_ISSUER",
    "AUTH_OIDC_AUDIENCE",
    "AUTH_OIDC_ALGORITHMS",
    "AUTH_OIDC_JWKS_CACHE_TTL",
    "AUTH_OIDC_LEEWAY",
])
def test_oidc_knob_guard_covers_the_whole_family(monkeypatch, name):
    """族里**任意**一个键都要被拦住，不能只认最眼熟的那个（`parametrize` 逐个试）。"""
    monkeypatch.delenv("SERVE_API_KEY", raising=False)
    monkeypatch.delenv("REQUIRE_AUTH", raising=False)
    monkeypatch.setenv(name, "leftover-from-old-env")
    with pytest.raises(RuntimeError) as ei:
        M._assert_no_legacy_auth_knobs()
    assert name in str(ei.value)


# ---------------------------------------------------------------------------
# 会话归属：thread_id 不能跨租户复用
# ---------------------------------------------------------------------------
def test_session_registry_blocks_cross_tenant_reuse():
    reg = SessionRegistry(dsn="", enabled=True)
    assert reg.claim("s1", "acme") == (True, ("acme", ""))
    ok, owner = reg.claim("s1", "globex")
    assert ok is False and owner == ("acme", "")


def test_session_registry_same_tenant_reuse_is_fine():
    reg = SessionRegistry(dsn="", enabled=True)
    reg.claim("s1", "acme")
    assert reg.claim("s1", "acme") == (True, ("acme", ""))


def test_session_registry_blocks_cross_user_reuse_within_same_tenant():
    """**同租户内的两个用户之间也必须隔离。**

    归属只比到 tenant 时，A 只要知道 B 的 thread_id 就能读到完整对话、
    删掉它、甚至替 B 接受待审草稿 —— 隔离边界只画在"组织之间"，
    而多用户场景下最需要隔离的恰恰是"同一个组织里的两个人"。
    """
    reg = SessionRegistry(dsn="", enabled=True)
    assert reg.claim("s1", "acme", "alice") == (True, ("acme", "alice"))
    ok, owner = reg.claim("s1", "acme", "bob")
    assert ok is False and owner == ("acme", "alice")
    # 同租户 + 同用户仍可复用（不能矫枉过正）
    assert reg.claim("s1", "acme", "alice")[0] is True


def test_session_registry_key_and_user_sessions_do_not_cross():
    """Key 会话（user 为空）与用户会话**互不继承** —— 它们是两类主体。

    刻意如此的严格语义：让它们互通等于给"机器凭证"开一条访问用户数据的后门。
    """
    reg = SessionRegistry(dsn="", enabled=True)
    reg.claim("k1", "acme")                       # API Key 建的
    assert reg.claim("k1", "acme", "alice")[0] is False
    reg.claim("u1", "acme", "alice")              # 用户凭证建的
    assert reg.claim("u1", "acme")[0] is False


def test_session_registry_disabled_passes_everything():
    """未启用用户鉴权时不做区分 —— 否则会平白误伤（那时所有请求同属主）。"""
    reg = SessionRegistry(dsn="", enabled=False)
    assert reg.claim("s1", "acme")[0] is True
    assert reg.claim("s1", "globex")[0] is True


def test_session_registry_memory_is_bounded_and_fails_closed():
    """内存模式必须有界；**超限后拒绝登记新会话**（而不是放行）。

    ⚠️ 2026-10-05 改。此前超限时 `return True, me`（放行且不登记），docstring 写的
    理由是"安全降级而非可用性降级"。**那个理由是错的**：放行意味着归属校验被
    整个跳过 —— 表满之后任何人都能拿别人的 thread_id 直接开跑，而且**零症状**。
    判不了就必须拒绝；代价（新会话起不来）用 503 + 可重试 来表达，
    而不是用"静默放行"来掩盖。

    ⚠️ 同时钉住"不能矫枉过正"：拒绝的是**新**会话，已登记的仍要照常校验
    （本主通过、他人被拒）。少了后半条，一个把入口焊死的实现也能让本用例变绿。
    """
    reg = SessionRegistry(enabled=True)
    reg.MAX_ENTRIES = 3
    for i in range(3):
        assert reg.claim(f"s{i}", "acme")[0] is True

    with pytest.raises(SessionError) as ei:
        reg.claim("s3", "acme")
    assert ei.value.status_code == 503, "判不了必须是 503（可重试），不是 403（结论明确）"
    assert ei.value.code == "session_registry_full"

    assert reg.claim("s0", "acme")[0] is True      # 已登记 + 本主 ⇒ 仍然放行
    assert reg.claim("s0", "globex")[0] is False   # 已登记 + 他人 ⇒ 仍然被拒
    assert reg.count() == 3


def test_session_registry_storage_error_denies_instead_of_allowing(monkeypatch):
    """存储异常时必须**拒绝**，且是 503 而不是 403。

    这是本次修的核心：此前抛异常后 `return True, me`，也就是"数据库抖动 ⇒
    归属校验整体失效"。而抖动恰恰是这类旁路保护最该生效的时刻。
    """
    from app import pg

    def _boom(*a, **kw):
        raise RuntimeError("connection refused")

    # 打桩点在**连接池**这一层（`app.pg.connect`）而不是 `psycopg.connect`：
    # 三个同步存储类已改为从共享池借连接，不再自己调 psycopg。
    monkeypatch.setattr(pg, "connect", _boom)
    reg = SessionRegistry(dsn="postgresql://fake", enabled=True)

    with pytest.raises(SessionError) as ei:
        reg.claim("t-1", "acme")
    assert ei.value.status_code == 503
    assert ei.value.code == "session_store_unavailable"

    # 读路径同样不能把"读不出来"降级成"不存在"（那会把排障引向"会话被删了"）
    with pytest.raises(SessionError) as ei2:
        reg.owner_of("t-1")
    assert ei2.value.status_code == 503


def test_session_registry_rejects_empty_thread_id():
    """空 thread_id 必须拒绝：所有客户端会共用同一个空 key 的会话空间。

    请求层已有非空校验（`ChatReq._v_thread_id`），这里是**纵深防御** ——
    存储层不该对"空 id"这种明显无法定位的记录做任何放行决定。
    """
    reg = SessionRegistry(enabled=True)
    with pytest.raises(SessionError) as ei:
        reg.claim("   ", "acme")
    assert ei.value.status_code == 400
    assert ei.value.code == "empty_thread_id"


def test_preflight_rejects_cross_tenant_session(monkeypatch):
    reg = SessionRegistry(enabled=True)
    reg.claim("shared-thread", "acme")
    monkeypatch.setattr(M, "_SESSIONS", reg)
    with pytest.raises(HTTPException) as ei:
        M._preflight(_FakeHTTPRequest(), session="shared-thread", tenant="globex")
    assert ei.value.status_code == 403


def test_preflight_rejects_cross_user_session_in_same_tenant(monkeypatch):
    """同租户、不同用户 → 403。这是"隔离下沉到用户"的直接断言。

    只测"跨租户被拒"是不够的：那个用例在归属只比到 tenant 时**照样能过**，
    所以它钉不住用户维度。
    """
    reg = SessionRegistry(enabled=True)
    reg.claim("shared-thread", "acme", "alice")
    monkeypatch.setattr(M, "_SESSIONS", reg)
    with pytest.raises(HTTPException) as ei:
        M._preflight(_FakeHTTPRequest(), session="shared-thread",
                     tenant="acme", user="bob")
    assert ei.value.status_code == 403
    # 属主本人必须仍然能过（否则就是"拒绝得对"掩盖了"所有人都被拒"）
    M._preflight(_FakeHTTPRequest(), session="shared-thread",
                 tenant="acme", user="alice")


# ---------------------------------------------------------------------------
# 出口收紧：恒定时间比较 + 异常文案脱敏
# ---------------------------------------------------------------------------
def test_password_verify_is_constant_time(monkeypatch):
    """密码校验必须走 `hmac.compare_digest`，不能用 `==`。

    为什么不写"行为断言"：`==` 与 `compare_digest` 的**返回值完全一致**，
    任何黑盒用例都分不出它们 —— 所以这里退而求其次，断言"确实调用了它"。
    这是一条**绊线**：把它改回 `==` 时本用例会红。

    为什么值得钉：`==` 在第一个不同的字节就返回，攻击者可逐字节试错、靠响应时间
    反推正确前缀（timing oracle）。网络抖动让它很难利用，但修它只要一行。

    ⚠️ 这条此前钉的是**共享密钥**的比较（`app/main.py` 里那句
    `hmac.compare_digest(x_api_key, SERVE_API_KEY)`，随共享密钥路径一起删除）。
    服务里还剩**这一处**需要恒定时间比较的地方 —— 密码哈希比对，所以覆盖面没有
    变窄，只是换了对象。
    """
    import hmac as _hmac

    from app import auth as A

    calls = []
    real = _hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(A.hmac, "compare_digest", spy)
    digest = A.hash_password("pw-correct", iterations=100_000)

    assert A.verify_password("pw-correct", digest) is True
    assert calls, "密码校验没走 compare_digest —— 又退回了 `==` 比较"

    calls.clear()
    assert A.verify_password("pw-wrong", digest) is False
    assert calls, "错误分支必须走同一条比较路径（短路径会泄漏信息）"


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
    from app.logging_setup import reset_request_id, set_request_id

    monkeypatch.setattr(M.config, "IS_PRODUCTION", True)
    token = set_request_id("rid-abc123")
    try:
        assert "rid-abc123" in M._client_error(RuntimeError("内部细节"))
    finally:
        reset_request_id(token)


def test_stream_error_event_is_sanitized_in_production(monkeypatch):
    """**接线测试**：`_stream_run` 下发的 error 事件必须用脱敏文案。

    上面三条只调 `_client_error()` —— 它们测的是"零件"，不是"零件有没有被装上"。
    实测教训：把 `_stream_run` 里的 `_client_error(e)` 改回 `str(e)` 时，
    那三条**照样全绿**（断言根本没走到调用点）。所以必须有一条从异常源头
    走到 SSE 事件的用例，否则这层保护可以被静默摘掉。
    """
    boom = RuntimeError(
        'connection to server at "db.internal" (10.0.0.5), port 5432 failed')
    monkeypatch.setattr(M.config, "IS_PRODUCTION", True)
    monkeypatch.setattr(M, "run_turn", _FakeTurn(boom=boom))

    events = _collect(M._stream_run(M.TurnRequest(prompt="hi", thread_id="t1"), "chat", tenant="t1"))
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
    """TRUST_PROXY_HEADERS=1 时取 XFF **第一段**（最左段）。

    ⚠️ 2026-10-04 订正 docstring：原文写的是"代理链里最左才是真实来源"—— 那句话是
    **错的**。最左段只有在代理**覆盖式**写入该头时才是真实客户端地址；若代理用
    nginx 的 `$proxy_add_x_forwarded_for`（追加式：客户端那段留在左），最左段恰是
    **客户端可控**的那一段，限流可被伪造绕过。

    所以本条只钉"实现取的是哪一段"（这是事实，不变）；
    "代理必须以覆盖式写入"由 `tests/test_deploy_topology.py` 的守卫负责。
    两条一起才构成完整防护。
    """
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
    # ⚠️ 必须给一个**用户鉴权**来源：生产环境"只配脚本通行证"会**拒绝启动**
    # （见 `test_auth_policy_blocks_production_without_user_auth`），
    # 那样这条用例根本测不到文档开关。
    env = {**os.environ, "APP_ENV": "production", "AUTH_SESSION_SECRET": "s",
           "DB_URL": ""}
    code = "import app.main as M; print(M.app.docs_url, M.app.openapi_url, M.config.ENABLE_DOCS)"
    out = subprocess.run([sys.executable, "-c", code], cwd=os.getcwd(), env=env,
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    # 启动日志也写 stdout，所以只看最后一行（print 在 import 之后执行）
    assert out.stdout.strip().splitlines()[-1] == "None None False"


# ---------------------------------------------------------------------------
# 断连止损：客户端走了就别再烧 token
# ---------------------------------------------------------------------------
# 断连止损：客户端走了就别再烧 token
# ---------------------------------------------------------------------------
def test_client_disconnect_stops_generation(monkeypatch):
    """断开后必须：① 停止产出；② 不再下发 done；③ 取消真的传导到上游运行。

    破坏 `_watch_disconnect` 与 `_DisconnectSignal` 之间的接线时这条会红。
    """
    from app import metrics

    metrics.reset()
    chunks = [f"段{i}" for i in range(40)]
    g = _FakeTurn(chunks, delay=0.02)          # 每段 20ms，模拟慢速生成

    events = _collect_with_disconnect(
        lambda sig: _stream(g, monkeypatch, disconnect=sig),
        trip_after=0.05)                        # 约 2~3 段后断开
    names = [e["event"] for e in events]

    assert names.count("token") < len(chunks), f"断开后仍把全部内容产出了：{names}"
    assert "done" not in names, f"已断开不该下发收尾事件：{names}"
    assert g.cancelled is True, "取消没有传导到上游运行，它会在后台跑到底（边断边计费）"
    # 指标是"止损真的发生了"的唯一线上证据；修复前它是死代码（恒为 0）
    assert metrics.value("client_disconnected_total", kind="chat") == 1


def test_client_disconnect_settles_the_upstream_run(monkeypatch):
    """断开后上游那次运行必须**收尾结束**，不能留一个没人取结果的协程。

    只断言"少发了几个 token"是不够的：那可以靠"不再 yield"伪造出来，而上游
    仍在后台跑（=仍在付钱）。`closed` 在替身运行的 `finally` 里置位，
    所以它只在上游**真的**退出时才为 True。
    """
    g = _FakeTurn([f"段{i}" for i in range(10)], delay=0.02)

    _collect_with_disconnect(
        lambda sig: _stream(g, monkeypatch, disconnect=sig),
        trip_after=0.05)
    assert g.closed is True, "上游运行没有收尾，取消没有传导下去"


def test_a_stalled_upstream_is_bounded_by_the_deadline(monkeypatch):
    """**回归测试**：上游长时间不产出内容时，不能无限等 —— 兜底是**总时限**。

    为什么这条不再声称"watcher 能救卡住的上游"
    ----------------------------------------
    watcher 置位后会去 `aclose()` 上游流，而此刻生成器正卡在"等下一段"里
    （`ag_running` 为真），asyncio 会以 "asynchronous generator is already
    running" 拒绝它 —— 这是 `_DisconnectSignal` 文档里写死的三条约束之一，
    以前那条用例只是没走到这里。

    生产链路上卡住的上游是被 sse_starlette 的 `cancel_scope.cancel()` 收掉的
    （断连即取消整个 task group，见 `tests/test_disconnect.py` 的真实 ASGI 栈）；
    单元级没有那层取消，所以这里钉住的是**最后一道兜底**：有总时限时必须按时收流
    并取消上游，而不是挂在那里。原来那条"断连能救卡住的上游"是假前提 ——
    它靠 15 秒超时失败，而不是靠任何真实机制。

    2026-09-28 的事故背景仍然成立：上游 429 重试了 36 秒，主循环一次都没转，
    客户端早走了服务端还在等、token 照付。当时的修法是"独立 watcher"，
    但真正把等待打断的是这里的 `deadline`（以及库的取消）。
    """
    g = _FakeTurn(stall=True)

    started = time.time()
    events = _collect(_stream(g, monkeypatch, timeout=0.5))
    elapsed = time.time() - started

    assert elapsed < 5, f"上游卡住时没有按时收流（耗时 {elapsed:.1f}s）"
    assert [e["event"] for e in events] == ["timeout"], events
    assert g.cancelled is True, "卡住的上游没有被取消，它会一直等到自己结束"


def test_disconnect_takes_effect_at_the_next_wait_boundary(monkeypatch):
    """断连在下一次迭代开头生效：不再继续产完，也绝不补一个没人收的 done。

    能漏出去的只有**断连那一刻正在途中的那一段**（上游已经把它交出来了）。
    剩下的必须停：删掉主循环开头那道 `disconnect.closed` 判定时，这条会看到
    上游把内容全产完再发 done —— 客户端早就走了，token 照付。
    """
    g = _FakeTurn([f"段{i}" for i in range(10)], delay=0.3)   # 每段 300ms

    events = _collect_with_disconnect(
        lambda sig: _stream(g, monkeypatch, disconnect=sig),
        trip_after=0.05)                        # 断连时第一段还在路上
    names = [e["event"] for e in events]

    assert names == ["token"], f"已断开却继续下发事件：{names}"
    assert g.cancelled is True


def test_disconnect_still_settles_quota(monkeypatch):
    """断连不等于免费：已经产出的部分仍要计入租户用量。

    ⚠️ 必须给一个**开着额度**的层（`daily_limit > 0`）：日额度改成租户+每人两级之后，
    两级都为 0 表示"不限" —— 此时根本不会建桶，`settle` 是空转的，
    断言会失败在一个与"断连记账"完全无关的原因上（历史版本无条件记账，
    所以那时 `daily_limit=0` 也能攒出数）。
    """
    from app import quota as Q

    q = Q.QuotaManager(daily_limit=10 ** 6, request_limit=1000)
    monkeypatch.setattr(M, "_QUOTA", q)

    g = _FakeTurn(["已经产出的内容"] * 20, delay=0.02)

    _collect_with_disconnect(
        lambda sig: _stream(g, monkeypatch, tenant="tenant-x", disconnect=sig),
        trip_after=0.1)                         # 已产出若干段后才断
    assert q.store.get(Q._tenant_bucket("tenant-x", Q._today()), Q._today()) > 0


def test_normal_run_without_disconnect_is_unchanged(monkeypatch):
    """不传 disconnect 时保持旧行为（CLI/历史调用方）—— 必须有 done 收尾。"""
    g = _FakeTurn(["正常回答"])

    events = _collect(_stream(g, monkeypatch))
    names = [e["event"] for e in events]
    assert names == ["token", "done"]
    assert g.closed is True and g.cancelled is False   # 正常结束也要收尾，且不是被取消的


def test_upstream_shutdown_error_does_not_break_accounting(monkeypatch):
    """上游在收尾期抛异常，不该污染这一轮的结论，也不该吞掉计费。

    真实形态：底层 HTTP 流被 cancel 时，传输层的收尾先抛一句 "connection closed"。
    它既不是"流故障"（不该冒 error 事件），也不该让 `finally` 里的结算被跳过。
    """
    from app import quota as Q

    q = Q.QuotaManager(daily_limit=10 ** 6, request_limit=1000)
    monkeypatch.setattr(M, "_QUOTA", q)
    g = _TurnRaisesOnShutdown()

    events = _collect_with_disconnect(
        lambda sig: _stream(g, monkeypatch, tenant="t-bad", disconnect=sig),
        trip_after=0.05)

    names = [e["event"] for e in events]
    # 只允许 token，且既没有 done（没收尾成功）、也没有 error（关闭期异常被当故障）
    assert names and set(names) == {"token"}, events
    assert q.store.get(Q._tenant_bucket("t-bad", Q._today()), Q._today()) > 0
