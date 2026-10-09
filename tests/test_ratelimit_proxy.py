"""代理失配（共享出口）加固的四条守卫（2026-10-06）。

背景
----
按 IP 限流的前提是"一个 IP 背后是一个人"。前提不成立时症状**完全一样**：
一个桶被很多人共用，一个人刷满则其余人（含 `POST /v1/auth/login`）全部 429。
两种成因必须分开处置：

  · 应用在反向代理后面但 `TRUST_PROXY_HEADERS` 没打开 —— **配置错**，能修，报 ERROR；
  · 反代后面还有 NAT / 网关 —— 修不了配置，报 WARNING。

识别手段是"同一个 IP 桶背后出现了几个不同主体"，处置手段是"已验证主体改按主体分桶"。
这两件事都**必须以主体可信为前提**，所以本文件里最重要的用例是
`test_unverified_subjects_never_get_their_own_bucket`（反向锁）：如果分桶键取自
客户端自报的名字，那"降级分桶"本身就成了绕过限流的方法 —— 每换一个名字换一个桶。
"""
import ast
import pathlib

from app import metrics as metrics_mod
from app.ratelimit import RateLimiter


def _feed(rl, subjects, ip="10.0.0.1", route="/v1/chat", verified=True):
    """用一串不同主体反复打同一个 IP 桶，返回最后一次判定结果。"""
    out = None
    for s in subjects:
        out = rl.check(ip=ip, route=route, subject=s, subject_verified=verified)
    return out


# ---------------------------------------------------------------------------
# 检测：什么时候算"共享出口"，报什么语气
# ---------------------------------------------------------------------------
def test_shared_egress_is_detected_and_reported_once(caplog):
    """同一个 IP 桶背后攒够多个不同主体 ⇒ 判定共享出口，且**只报一次**。

    为什么要靠"几个不同主体"而不是"IP 是不是私网地址"：NAT 出口地址看起来与普通
    客户端没有区别，唯一能自证的信号就是"它背后的主体不止一个"。
    为什么必须只报一次：这条检测会命中**每一个** NAT 出口的正常流量，
    每次请求都报一遍等于把日志冲掉（告警也会变成噪音）。
    """
    import logging

    rl = RateLimiter(per_minute=1000, by_session=False, proxy_headers_trusted=False)
    before = metrics_mod.value("ratelimit_shared_egress_total", scope="ip", trusted="no")
    with caplog.at_level(logging.ERROR):
        _feed(rl, ["u1", "u2", "u3"])
        assert not [r for r in caplog.records if "共享出口" in r.getMessage()], (
            "还没攒够主体就报了出来 —— 阈值失效，正常用户会被误报")
        _feed(rl, ["u4"])
        first = [r for r in caplog.records if "共享出口" in r.getMessage()]
        _feed(rl, ["u5", "u6"])
        all_reports = [r for r in caplog.records if "共享出口" in r.getMessage()]
    assert len(first) == 1, first
    assert len(all_reports) == 1, f"同一个桶报了 {len(all_reports)} 次，日志会被冲掉"
    # 指标按**增量**断言：计数表是进程级的，跨用例不清零
    after = metrics_mod.value("ratelimit_shared_egress_total", scope="ip", trusted="no")
    assert after - before == 1, (before, after)


def test_untrusted_proxy_headers_logs_error_with_the_fix(caplog):
    """没开 TRUST_PROXY_HEADERS 时必须是 ERROR，并且**写出修复动作**。

    这是配置错，而且是最坏的一类：不报错、不崩溃，只是"限流额度算错了"。
    没有这条日志，唯一能发现它的办法是等到某天有人刷满、所有人集体登不进来。
    """
    import logging

    rl = RateLimiter(per_minute=1000, by_session=False, proxy_headers_trusted=False)
    with caplog.at_level(logging.ERROR):
        _feed(rl, ["u1", "u2", "u3", "u4"])
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("TRUST_PROXY_HEADERS" in m for m in msgs), msgs
    assert any("共享出口" in m for m in msgs), msgs


def test_trusted_proxy_headers_is_only_a_warning(caplog):
    """已经信任转发头 ⇒ 这是 NAT，改配置解决不了 ⇒ WARNING，不要拿 ERROR 喊人。"""
    import logging

    rl = RateLimiter(per_minute=1000, by_session=False, proxy_headers_trusted=True)
    before = metrics_mod.value("ratelimit_shared_egress_total", scope="ip", trusted="yes")
    with caplog.at_level(logging.INFO):
        _feed(rl, ["u1", "u2", "u3", "u4"])
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "NAT 出口被报成 ERROR —— 这类流量天天都有，会把真正的配置错误淹掉")
    assert [r for r in caplog.records if r.levelno == logging.WARNING], "至少要有一条 WARNING"
    after = metrics_mod.value("ratelimit_shared_egress_total", scope="ip", trusted="yes")
    assert after - before == 1, (before, after)


def test_anonymous_requests_never_look_like_a_shared_egress(caplog):
    """不传主体（登录接口、未登录）时**不参与**检测。

    否则每个匿名请求都等于"来了一个主体"，检测立刻全是误报 —— 而登录接口恰恰是
    最需要这道保护的地方。
    """
    import logging

    rl = RateLimiter(per_minute=1000, by_session=False, proxy_headers_trusted=False)
    with caplog.at_level(logging.ERROR):
        for _ in range(50):
            rl.check(ip="10.0.0.9", route="/v1/auth/login")
    assert not [r for r in caplog.records if "共享出口" in r.getMessage()]


# ---------------------------------------------------------------------------
# 降级分桶：核心安全边界（自报主体绝不许换桶）
# ---------------------------------------------------------------------------
def test_unverified_subjects_never_get_their_own_bucket():
    """⚠️ 反向锁：主体没验签时，**换名字换不到新桶**。

    没有这条，`_ip_key` 里那个 `subject_verified` 条件一旦被删掉/写成恒真，
    攻击者只要在一次请求里换个 `username` 就能把 IP 维度的额度无限刷下去 ——
    而那正是"加降级分桶"最容易引入的漏洞。断言方式是"桶没被分开"：
    额度用尽之后，即使一直换主体也必须**继续被拒**。
    """
    rl = RateLimiter(per_minute=2, by_session=False, proxy_headers_trusted=True)
    ip, route = "10.0.0.1", "/v1/auth/login"
    # 先把这条 IP 变成"已判定共享出口"（4 个不同主体，且都用**未验签**身份），
    # 这一步的计数全部落在共享桶上；per_minute=2 ⇒ 共享桶此时已经用超。
    for s in ("a", "b", "c", "d"):
        rl.check(ip=ip, route=route, subject=s, subject_verified=False)
    # 换一个从没出现过的名字再打：如果代码错误地按自报主体分桶，这会拿到一个空桶
    # 从而被放行；正确行为是仍然落在**那个已经超额的共享桶**上 ⇒ 被拒。
    r = rl.check(ip=ip, route=route, subject="e", subject_verified=False)
    assert not r.allowed, "自报主体换了个名字就拿到了新额度 —— 限流可被绕过"


def test_verified_subjects_do_not_starve_each_other_behind_one_ip():
    """已验签主体在共享出口后面**各占一个桶** —— 这是这条加固的目的本身。

    没有它，NAT 后面的一个人刷满，同出口的所有同事一起被拒（而他们什么都没做错）。
    """
    rl = RateLimiter(per_minute=1, by_session=False, proxy_headers_trusted=True)
    ip, route = "10.0.0.1", "/v1/chat"
    for s in ("a", "b", "c", "d"):        # 攒到阈值 ⇒ 这条 IP 判定为共享出口
        rl.check(ip=ip, route=route, subject=s, subject_verified=True)
    assert rl.check(ip=ip, route=route, subject="a", subject_verified=True).allowed
    assert not rl.check(ip=ip, route=route, subject="a", subject_verified=True).allowed, (
        "前置条件不成立：a 的桶还没满，后面那条断言就是假绿")
    assert rl.check(ip=ip, route=route, subject="b", subject_verified=True).allowed, (
        "a 把自己那份用完，却把 b 也挡住了 —— 退化分桶没生效")


def test_different_routes_are_tracked_separately():
    """跟踪与降级都按 (IP, 路由) 分开：登录端点的扇出不代表对话端点也是共享出口。"""
    rl = RateLimiter(per_minute=1000, by_session=False, proxy_headers_trusted=False)
    _feed(rl, ["u1", "u2", "u3", "u4"], route="/v1/auth/login")
    assert rl._note_subject("10.0.0.1", "/v1/chat", "z") is False, (
        "登录端点的扇出把对话端点也标记成共享出口了")


# ---------------------------------------------------------------------------
# 内存有界：检测器不能被攻击者撑大
# ---------------------------------------------------------------------------
def test_tracking_table_is_bounded():
    """跟踪表有上限 —— 否则"记下每个 IP 见过谁"本身就是一个内存放大面。"""
    rl = RateLimiter(per_minute=10 ** 9, by_session=False)
    for i in range(RateLimiter.TRACKED_MAX * 3):
        rl.check(ip=f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}", route="/v1/chat",
                 subject=f"u{i}", subject_verified=True)
    assert len(rl._tracked) <= RateLimiter.TRACKED_MAX, len(rl._tracked)


def test_subjects_per_bucket_is_bounded():
    """一个桶里最多记这么多个主体 —— "一个 IP 上万个账号"不该撑爆内存。"""
    rl = RateLimiter(per_minute=10 ** 9, by_session=False)
    for i in range(RateLimiter.SUBJECTS_MAX * 20):
        rl.check(ip="10.0.0.1", route="/v1/chat", subject=f"u{i}", subject_verified=True)
    assert len(rl._tracked[("10.0.0.1", "/v1/chat")]["subjects"]) <= RateLimiter.SUBJECTS_MAX


# ---------------------------------------------------------------------------
# 与主流程的接线：路由键必须是模板、调用方必须传对可信度
# ---------------------------------------------------------------------------
def test_route_key_uses_the_route_template():
    """限流桶键必须用**路由模板**：原始路径会让每个 thread_id 各占一个桶。

    后果有两层：额度随"客户端能造出多少个 id"放大（每个 id 各 120/分钟），
    以及共享出口跟踪表被撑大（与内存有界那条测试是同一件事）。
    """
    from starlette.requests import Request

    from app.main import _route_key

    class _Route:
        path = "/v1/sessions/{thread_id}"

    scope = {"type": "http", "method": "GET", "path": "/v1/sessions/abc",
             "scheme": "http", "server": ("testserver", 80),
             "headers": [], "query_string": b"", "route": _Route()}
    assert _route_key(Request(scope)) == "/v1/sessions/{thread_id}"
    # 路由未匹配（中间件层 / 404）时回落到原始路径，而不是抛异常
    assert _route_key(Request({**scope, "route": None})) == "/v1/sessions/abc"


def test_rate_limit_callers_declare_trustworthiness():
    """接线的静态守卫：两类主体必须走不同的可信度声明。

    为什么用 AST 而不是"跑一遍看日志"：`subject_verified` 默认 False ⇒ 忘记传是
    **静默**的（退化成"IP 后面的人互相抢额度"，功能看起来完全正常），而漏传的后果
    只在 NAT 场景下才显形，本地永远测不出来。反过来，**多传**（把请求体里的用户名
    声明成已验签）更糟：限流键就交给了攻击者。

    两类判据：
      · 主体来自请求体（`req.xxx`）⇒ 是客户端自报的，**禁止**声明可信；
      · 主体来自 `ident.user` ⇒ 是验签过的，**必须**声明可信。
    其余形态（如 `_preflight` 把形参转发下去）静态分类不了，跳过 —— 那一路的可信度
    由 `_preflight` 的调用方（`_authenticate` 之后）保证。
    """
    src = pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    selfreported = verified = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == "_rate_limit"):
            continue
        kwargs = {k.arg: k.value for k in node.keywords}
        subject = kwargs.get("subject")
        if subject is None:
            continue
        expr = ast.unparse(subject)
        if "req." in expr:
            selfreported += 1
            assert "subject_verified" not in kwargs, (
                f"第 {node.lineno} 行的主体是**客户端自报**的（{expr}），"
                "不能声明为已验证 —— 那等于把限流键交给攻击者")
        elif "ident.user" in expr:
            verified += 1
            assert "subject_verified" in kwargs, (
                f"第 {node.lineno} 行传了已验签身份却没说它可信：{expr}"
                "（漏传 ⇒ NAT 后面的人互相抢额度，且不会有任何报错）")
            assert ast.unparse(kwargs["subject_verified"]) == "bool(ident.user)"
    assert verified >= 5, f"只检查到 {verified} 处已验签调用，守卫可能已经不生效了"
    assert selfreported >= 2, f"只检查到 {selfreported} 处自报主体调用，守卫可能已失效"


# ---------------------------------------------------------------------------
# nginx 兜底：应用层配错时，单个真实客户端也堵不住别人
# ---------------------------------------------------------------------------
def _nginx(name):
    root = pathlib.Path(__file__).resolve().parents[1]
    return (root / "deploy" / "app" / name).read_text(encoding="utf-8")


def test_nginx_has_a_strict_per_ip_zone_for_auth_endpoints():
    """认证端点必须有**按真实客户端 IP** 的更紧一道，且只在 /v1/auth/ 生效。

    为什么需要（应用侧已经按 IP 限流了）：应用侧那个桶只有在 `TRUST_PROXY_HEADERS`
    打开时才是按真实 IP 分的；配错时全站一个桶，而 nginx 的通用 zone 是
    "10r/s + burst 40"（≈600 次/分钟）—— 一个客户端就能把应用侧那 120/分钟刷满。
    这一道把它压到应用额度之下，堵住"一个人挡住所有人"。
    """
    for name in ("nginx.conf", "nginx.tls.conf"):
        text = _nginx(name)
        assert "map $uri $chatagent_auth_key" in text, name
        assert 'default     "";' in text, f"{name}: 空键默认值没了 ⇒ 全站都会被这道限流算一遍"
        assert "~^/v1/auth/ $binary_remote_addr;" in text, name
        assert "limit_req_zone $chatagent_auth_key zone=chatagent_auth:" in text, name
        assert "limit_req zone=chatagent_auth burst=" in text, f"{name}: zone 定义了却没用上"


def test_nginx_auth_zone_cannot_let_one_client_fill_the_app_bucket():
    """数量关系必须成立：**单人**在认证端点上每分钟能打的次数 < 应用侧额度。

    否则这道兜底只是看起来在兜 —— 单人仍能把应用侧那个桶刷满。
    用配置里的真实数值比（而不是把数字抄进断言）：应用额度改了以后这条会自动变红，
    提醒同步调整 nginx 侧。
    """
    import re

    from app import config

    text = _nginx("nginx.conf")
    rate = re.search(r"zone=chatagent_auth:10m rate=(\d+)r/m;", text)
    burst = re.search(r"limit_req zone=chatagent_auth burst=(\d+)", text)
    assert rate and burst, "解析不到认证 zone 的速率/突发值（格式变了？）"
    per_client_per_minute = int(rate.group(1)) + int(burst.group(1))
    assert per_client_per_minute < config.RATE_LIMIT_PER_MINUTE, (
        f"认证端点单人可打 {per_client_per_minute}/分钟，应用侧额度只有 "
        f"{config.RATE_LIMIT_PER_MINUTE}/分钟 —— 一个人仍能把全站登录挡住")
