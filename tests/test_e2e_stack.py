"""真实栈的端到端检查（**默认跳过**）。

为什么必须单独一份、且默认跳过
----------------------------
`tests/` 里其余用例都是**进程内**的（`TestClient` 或直接调函数），有几类属性它们
原理上就覆盖不到 —— 而这几类恰好都是"错了也不报错、只是线上体验变差"的：

1. **nginx 有没有把 SSE 攒住**。漏写 `proxy_buffering off` 时，进程内测试完全看不出来
   （它压根不经过 nginx），表现是"回答卡十几秒然后一次性吐出来"。
2. **灰度权重是不是真的生效**。`nginx_weight.py` 改了文件也 reload 了，但权重可能写进了
   错误的位置，或 upstream 里根本没有 canary —— 只有真打一轮请求、看 `release` 字段的
   分布才知道。**这是唯一能自证"权重真生效"的检查。**
3. **检查点到底落库没有**。`/health/ready` 的 `checkpointer` 明细会显示
   `InMemorySaver（内存，重启丢状态！）` —— 这是"持久化有没有接上"的**唯一可靠信号**
   （见 MEMORY.md），而进程内测试里它恒为内存。

所以这一份必须打真实入口。但它需要 docker 起栈（多个容器 + 真 PG），**不能让默认的
`pytest tests/` 去连一个不存在的栈**（那会让每个人的日常测试都红，接着这份文件就会被
删掉）。于是统一由 `E2E_BASE_URL` 控制：没设就整份跳过，且 skip 理由里写明怎么跑。

怎么跑
------
    python scripts/tasks.py e2e                            # 起栈 → 等就绪 → 跑这份 → 收栈
    E2E_BASE_URL=http://127.0.0.1:8080 pytest -m e2e       # 对已经起好的栈跑

可选环境变量
    E2E_USER / E2E_PASSWORD   脚本服务账号 —— 本文件用它**登录换 JWT**（推荐做法）。
                              没设时回退到 `AGENT_SCRIPT_USER` / `AGENT_SCRIPT_PASSWORD`。
    E2E_API_KEY               【已废弃】共享密钥 `X-API-Key`；只为过渡期保留
    E2E_SAMPLES               分流取样次数（默认 20）
    E2E_CHAT_TIMEOUT          等 SSE 首帧的秒数（默认 90，LLM 首字节可能十几秒）

⚠️ 三种栈配置都能跑，不必改本文件：
    ① 配了**用户鉴权** ⇒ 给 `E2E_USER` / `E2E_PASSWORD`，本文件登录换令牌后带 Bearer；
    ② 只配了**共享密钥** ⇒ 给 `E2E_API_KEY`（此时会打印一条废弃提醒）；
    ③ **完全没配鉴权** ⇒ 什么都不用给，本文件不带任何认证头。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections import Counter

import pytest

from scripts import script_auth

BASE = os.getenv("E2E_BASE_URL", "").rstrip("/")
API_KEY = os.getenv("E2E_API_KEY", "").strip()
USER = (os.getenv("E2E_USER") or os.getenv(script_auth.USER_ENV) or "").strip()
PASSWORD = (os.getenv("E2E_PASSWORD") or os.getenv(script_auth.PASSWORD_ENV) or "").strip()
SAMPLES = int(os.getenv("E2E_SAMPLES", "20"))
CHAT_TIMEOUT = float(os.getenv("E2E_CHAT_TIMEOUT", "90"))

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not BASE,
        reason=(
            "未设置 E2E_BASE_URL —— 这份用例需要真实运行中的栈。"
            "一键跑：`python scripts/tasks.py e2e`（起栈 → 等就绪 → 跑本文件 → 收栈）"
        ),
    ),
]


# --------------------------------------------------------------------------
# 极简 HTTP 辅助（标准库；不引 httpx，免得又多一个"测试专属依赖"）
# --------------------------------------------------------------------------
_TOKEN = ""


def _auth_configured() -> bool:
    """这个栈要求鉴权吗？（有凭证 = 要求）—— 供鉴权用例决定该断言 401 还是 200。"""
    return bool(API_KEY) or script_auth.has_credentials(USER, PASSWORD)


def _bearer() -> str:
    """登录换令牌，进程内只登一次。

    ⚠️ 为什么不在模块级登录：登录要打**运行中的栈**，而这份文件在栈没起来时是整份
    skip 的（`BASE` 为空）。模块级登录会在 collection 阶段就失败，把"跳过"变成"报错"。
    """
    global _TOKEN
    if not _TOKEN:
        user, password = script_auth.require_credentials(USER, PASSWORD)
        _TOKEN = script_auth.access_token(BASE, user, password)
    return _TOKEN


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """按栈的配置挑认证方式，三条路径与 `app/main.py::_authenticate` 的顺序一致。

    ⚠️ `X-API-Key` 那条是**已废弃**的过渡路径（分不出身份、租户自报），
    保留只为让还没切换的栈继续能跑；`Authorization` 才是目标口径。
    """
    h = {"Accept": "application/json"}
    if API_KEY:
        h["X-API-Key"] = API_KEY
    elif script_auth.has_credentials(USER, PASSWORD):
        h["Authorization"] = f"Bearer {_bearer()}"
    h.update(extra or {})
    return h


def _request(path: str, *, method: str = "GET", body=None, timeout: float = 15,
             headers: dict[str, str] | None = None):
    """返回 (status, body_bytes, headers)。

    ⚠️ 4xx/5xx **不抛异常**：这里断言的就是状态码本身，让异常冒出去会丢掉
    body（而 body 里往往写着真正的原因）。

    `headers` 传了就**原样使用**（不叠 `_headers()`）—— "裸请求"必须能构造出来，
    否则"配了鉴权就应当拒绝未授权"这条不变量就没法验（见鉴权用例的说明）。
    """
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    hdrs = dict(headers) if headers is not None else _headers(
        {"Content-Type": "application/json"} if data else None)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers or {})
    except urllib.error.URLError as exc:
        pytest.fail(
            f"连不上 {url}：{exc.reason}\n"
            f"栈起来了吗？一键：`python scripts/tasks.py e2e`；"
            f"或确认 E2E_BASE_URL 指向 nginx 的入口（compose 默认是 http://127.0.0.1:8080）"
        )


def _json(path: str, **kw):
    status, raw, headers = _request(path, **kw)
    try:
        return status, json.loads(raw), headers
    except ValueError:
        return status, {"_raw": raw[:400].decode("utf-8", "replace")}, headers


# --------------------------------------------------------------------------
# ① 探针：经 nginx 打得通，且**真的**探到了依赖
# --------------------------------------------------------------------------
def test_live_reports_a_release_identifier():
    status, body, _ = _json("/health/live")
    assert status == 200, f"/health/live 返回 {status}：{body}"
    assert body.get("status") == "ok"
    assert body.get("release") in ("stable", "canary"), (
        f"release={body.get('release')!r} —— 灰度期这个字段是'请求落到了哪个实例'的唯一凭据"
        f"（rollback.sh 也靠它验证分流），缺失会让回滚验证失去依据"
    )
    assert body.get("version"), "/health/live 没有 version —— 无法核对线上跑的是哪个版本"


def test_ready_is_green_with_a_persistent_checkpointer():
    """就绪探针必须真绿，且检查点必须**落库**。

    ⚠️ 这条专门盯一个静默降级：psycopg 的异步连接不支持 Windows 的 ProactorEventLoop，
    配置稍错就会退回内存检查点 —— 服务照常起来、回答照常工作，**只有重启后丢状态**。
    唯一能看出它的地方就是这里的 `checkpointer` 明细。
    """
    status, body, _ = _json("/health/ready", timeout=30)
    detail = body.get("checks", {})
    assert status == 200, (
        f"/health/ready 返回 {status}（{body.get('status')}）—— 明细："
        f"{ {k: v.get('detail') for k, v in detail.items()} }"
    )
    cp = detail.get("checkpointer", {}).get("detail", "")
    assert "内存" not in cp, (
        f"检查点是 {cp} —— 持久化没有接上（容器重启后会话状态全丢）。"
        f"常见成因：DB_URL 没传进容器 / psycopg 异步连接落到了不支持的事件循环上"
    )
    assert detail.get("db", {}).get("ok") is True, f"数据库探测未通过：{detail.get('db')}"


# --------------------------------------------------------------------------
# ② 鉴权：开关与行为必须一致
# --------------------------------------------------------------------------
def test_protected_endpoint_rejects_unauthenticated_requests():
    """**裸请求**必须有确定的处置：配了鉴权就必须 401，完全没配就必须 200。没有 skip。

    跳过在这里是有害的：一个"被跳过"的鉴权用例看起来和"通过了"一样。

    ⚠️ 这条必须发**裸请求**（不带任何认证头），所以不能复用 `_headers()`。
    它原先借 `E2E_API_KEY` 当"探测凭证"、直接把 `_headers()` 的结果当裸请求用；
    现在 `_headers()` 会带**真实**凭证（登录换来的令牌或那把 Key），
    那样发出去的就不再是裸请求，断言会退化成"我带着合法凭证却被拒" —— 与要验的东西无关。
    """
    status, body, _ = _json("/v1/reviews", headers={"Accept": "application/json"})
    if _auth_configured():
        assert status == 401, (
            f"这个栈配了鉴权（给了 E2E_USER/E2E_PASSWORD 或 E2E_API_KEY），"
            f"但 /v1/reviews 对**不带凭证的请求**返回 {status} —— 鉴权没有生效"
        )
    else:
        assert status == 200, (
            f"/v1/reviews 返回 {status}：{body}\n"
            f"两种可能：① 这个栈配了鉴权但你没给凭证（请设 E2E_USER/E2E_PASSWORD，"
            f"或过渡期的 E2E_API_KEY；本用例会自动改为验证'拒未授权'）；"
            f"② 数据库没接上（看 /health/ready 明细）"
        )


# --------------------------------------------------------------------------
# ③ SSE：经 nginx 打，帧必须**一帧一帧地**到达
# --------------------------------------------------------------------------
def test_sse_frames_reach_the_client_unbuffered():
    """验证 SSE 经 nginx 后仍然分帧到达。

    ⚠️ 这条**不需要 LLM Key**：没配 LLM 时服务会立刻回一个 `error` 事件 ——
    那也是合法的 SSE 帧，同样能证明 nginx 没有破坏分帧、也没有把它攒起来
    （`proxy_buffering off` 漏写时会攒够一块才转发，本用例会超时）。
    配了 Key 时走完整链路（token … done）。
    """
    url = BASE + "/v1/chat"
    body = json.dumps({"thread_id": "e2e-sse-probe", "message": "ping"}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers=_headers({"Content-Type": "application/json", "Accept": "text/event-stream"}),
        method="POST",
    )

    try:
        resp = urllib.request.urlopen(req, timeout=CHAT_TIMEOUT)
    except urllib.error.HTTPError as exc:
        pytest.fail(f"/v1/chat 返回 {exc.code}：{exc.read()[:300]!r}")
    except urllib.error.URLError as exc:
        pytest.fail(f"连不上 {url}：{exc.reason}")

    with resp:
        ctype = resp.headers.get("Content-Type", "")
        assert ctype.startswith("text/event-stream"), (
            f"Content-Type 是 {ctype!r} —— 经 nginx 后 SSE 的类型被改掉了，客户端不会按流处理"
        )
        # ⚠️ 用 read1 而不是 read(512)：read(n) 会一直阻塞到**读满 n 字节或流结束**，
        #    对 SSE 就等于"等整轮回答结束"——那恰恰把要测的"首帧是否及时到达"测没了。
        first = resp.read1(4096)

    assert first, (
        f"连接建立后 {CHAT_TIMEOUT}s 内没收到任何字节 —— 大概率是 nginx 把 SSE 攒住了"
        f"（检查 nginx.conf 的 proxy_buffering off / chunked_transfer_encoding on）"
    )
    text = first.decode("utf-8", "replace")
    assert "event:" in text or "data:" in text, (
        f"首块不是 SSE 帧格式：{text[:200]!r} —— 帧格式坏了客户端会静默丢事件"
    )


# --------------------------------------------------------------------------
# ④ 灰度：权重是不是真的生效
# --------------------------------------------------------------------------
def test_the_edge_splits_traffic_between_releases():
    """打一批请求，看 `release` 的分布 —— **唯一能自证权重生效的检查**。

    nginx 用的是平滑加权轮询（不是随机），9:1 时每 10 个请求里恰好有 1 个进 canary，
    所以 20 次取样足够稳定；真出现"只落一个 release"时，要么权重写错了位置、
    要么 upstream 里没有那个 server、要么那个实例没起来（都会让灰度形同虚设）。
    """
    seen: Counter[str] = Counter()
    for _ in range(SAMPLES):
        status, body, _ = _json("/health/live")
        assert status == 200, f"/health/live 返回 {status}：{body}"
        seen[body.get("release", "<缺失>")] += 1

    assert len(seen) >= 2, (
        f"{SAMPLES} 次取样只落到了 {dict(seen)} —— 灰度没有生效。"
        f"依次检查：upstream 里两个 server 都在吗？权重是不是 0？canary 容器起来了吗？"
    )
    top, _, second = seen.most_common(3)
    assert top[0] == "stable", f"占比最高的是 {top[0]!r}（{dict(seen)}）—— 权重配反了？"
    assert second[0] == "canary", f"除了 stable 还有 {second[0]!r}（{dict(seen)}）—— 不应该有第三个实例"


# --------------------------------------------------------------------------
# ⑤ 指标：要么能抓，要么被白名单挡住 —— 但绝不能是 5xx
# --------------------------------------------------------------------------
def test_metrics_are_either_scrapable_or_blocked_by_nginx():
    """`/metrics` 在 nginx 里有私网白名单。

    从宿主机打，源地址可能是 docker 网桥（172.x，命中白名单 → 200）或别的网段
    （→ 403）。两种都是**正确配置**，所以这里断言的是"不是 5xx"，并在 200 时
    进一步核对内容真的是 Prometheus 文本。
    """
    status, raw, _ = _request("/metrics")
    assert status in (200, 403), (
        f"/metrics 返回 {status} —— 预期 200（白名单放行）或 403（白名单拦截），"
        f"5xx 说明服务本身有问题"
    )
    if status == 200:
        text = raw.decode("utf-8", "replace")
        assert "http_requests_total" in text, (
            "/metrics 返回了 200 但没有业务指标 —— 抓到的可能是 nginx 的错误页"
        )
