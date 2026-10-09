"""告警网关（`deploy/monitoring/alert_sink.py`）的行为测试。

为什么这些断言值得写
--------------------
告警网关是"最后一次投递机会"，而它的失败模式**几乎全是静默的**：

  - 通道没配置 → 只记一条日志，接口照样返回 200（服务看起来完全健康）；
  - 企微/飞书在 **HTTP 200** 时于 body 里报错（机器人被移出群、token 失效）→
    只看状态码的实现会判成"已送达"；
  - receiver 名与 URL 末段不一致 → 告警落进 default 兜底或直接消失；
  - 网络异常没被吞掉 → 一条通道挂掉导致整组重试，把已成功的通道重复轰炸。

这些都不会让进程崩，只会让**故障发生时没人被叫醒** —— 而那正是这套系统
存在的唯一理由。所以把判定钉在这里，而不是靠人记得去自检。

（说明：本文件刻意用标准库 `urllib` 起真实 HTTP，而不是 mock 掉传输层 ——
因为这一层的契约就是"线格式"：路径怎么解析、状态码怎么给、请求头怎么校验。
mock 掉之后这些全都不被测试覆盖。`TestClient` 那类假绿的教训见 MEMORY.md。）
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SINK_PATH = ROOT / "deploy" / "monitoring" / "alert_sink.py"


def _load_sink():
    """按路径加载（deploy/ 不是包，不能 import）。"""
    spec = importlib.util.spec_from_file_location("alert_sink", SINK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SINK = _load_sink()


# ---------------------------------------------------------------------------
# 路由表解析：容忍脏输入（配置错不该让整个网关起不来）
# ---------------------------------------------------------------------------
def test_parse_routes_basic():
    assert SINK.parse_routes("oncall:wecom,security-oncall:feishu,default:generic") == {
        "oncall": "wecom", "security-oncall": "feishu", "default": "generic",
    }


def test_parse_routes_tolerates_dirty_input():
    """多余逗号 / 空格 / 空段都不该让路由表崩掉。

    容忍的收益是决定性的：解析失败 ⇒ 网关起不来 ⇒ **所有**告警都送不到，
    比"少了一条路由"严重得多。而环境变量里多一个逗号极其常见。
    """
    assert SINK.parse_routes(" oncall : WeCom ,, default:feishu ,") == {
        "oncall": "wecom", "default": "feishu",
    }
    assert SINK.parse_routes("") == {}
    assert SINK.parse_routes("没有冒号的段") == {}


def test_parse_routes_drops_unknown_kind():
    """通道类型拼错时丢弃该项（并告警），而不是造出一个发不出去的通道。"""
    routes = SINK.parse_routes("oncall:wecom,typo:wecomx")
    assert routes == {"oncall": "wecom"}


# ---------------------------------------------------------------------------
# 消息渲染
# ---------------------------------------------------------------------------
def _payload(status="firing", n=1, name="ChatAgentDown", **labels):
    base = {"alertname": name, "category": "availability", "severity": "critical",
            "instance": "app-stable:8000"}
    base.update(labels)
    return {
        "version": "4", "status": status, "receiver": "oncall",
        "alerts": [{
            "status": status,
            "labels": base,
            "annotations": {"summary": "服务不可用", "description": "探活连续失败",
                            "runbook": "deploy/RUNBOOK.md"},
            "startsAt": "2026-09-29T00:00:00Z",
        } for _ in range(n)],
    }


def test_render_text_includes_actionable_fields():
    """必须带上 alertname / severity / instance / summary / runbook。

    少了 runbook 就等于"响了但没人知道下一步做什么"，这是告警最贵的失败形态。
    """
    text = SINK.render_text(_payload())
    for want in ("ChatAgentDown", "critical", "instance=app-stable:8000",
                 "服务不可用", "deploy/RUNBOOK.md"):
        assert want in text, f"渲染出的消息缺少 {want!r}：\n{text}"


def test_render_text_marks_resolved():
    text = SINK.render_text(_payload(status="resolved"))
    assert "已恢复" in text
    assert "[告警]" not in text


def test_render_text_counts_only_firing_as_firing():
    """混批（部分已恢复）时，标题里的条数只能是 firing 的条数。

    否则恢复通知会和告警通知看起来一样，值班的人会误以为还有故障没处理。
    """
    p = _payload(n=3)
    p["alerts"][0]["status"] = "resolved"
    text = SINK.render_text(p)
    assert "[告警] 2 条" in text
    assert "另有 1 条已恢复" in text


def test_render_text_truncates_long_batches():
    """超长批次必须裁剪并给出"还有多少条"。

    机器人消息过长的结果是**没人看完**，等价于没发。
    """
    text = SINK.render_text(_payload(n=SINK.MAX_ALERTS_IN_TEXT + 5))
    assert "还有 5 条" in text
    assert text.count("\n1.") == 1  # 第一条仍然在


def test_render_text_handles_empty_event():
    """空事件也不能抛异常（Alertmanager 在极少数情况下会发空 alerts）。"""
    assert "空事件" in SINK.render_text({"alerts": []})


# ---------------------------------------------------------------------------
# 消息体形状：必须与 app/review_queue.py 的约定一致
# ---------------------------------------------------------------------------
def test_wecom_body_matches_review_notify_contract():
    assert SINK.to_wecom("hi") == {"msgtype": "text", "text": {"content": "hi"}}


def test_feishu_body_matches_review_notify_contract():
    assert SINK.to_feishu("hi") == {"msg_type": "text", "content": {"text": "hi"}}


def test_feishu_body_carries_no_signature_fields():
    """契约守卫：实现**不支持加签** ⇒ 飞书那侧不能开「签名校验」。

    飞书官方**强烈建议**给自定义机器人加安全设置，而三个选项里"签名校验"是最
    容易顺手勾上的那个。勾上之后每个请求都会被拒（`19021 sign match fail`），
    现象是"配置全对但没人收到" —— 与企微 `ALERT_SINK_SHARED_SECRET` 那条同构。

    哪天真的给 `to_feishu()` 加了 timestamp/sign 支持，这条会红，提醒同步改
    `.env.example` 里那句"不要开签名校验"。
    """
    body = SINK.to_feishu("hi")
    assert set(body) == {"msg_type", "content"}
    assert "timestamp" not in body
    assert "sign" not in body


def test_docs_warn_against_feishu_signature_check():
    """上一条的前提必须写在人能看到的地方 —— 勾错了不会有任何本地报错。"""
    for rel in (".env.example", "deploy/monitoring/alert_sink.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "签名校验" in text, rel
        assert "自定义关键词" in text, rel
        assert "19021" in text, rel


def test_generic_body_keeps_original_payload():
    """自建网关要能拿到原始 alerts[]（它可能要做更复杂的处理）。"""
    p = _payload()
    body = SINK.to_generic(p, "rendered")
    assert body["alerts"] == p["alerts"]
    assert body["rendered_text"] == "rendered"


# ---------------------------------------------------------------------------
# 投递：三态（已送达 / 未发送 / 失败）必须分清
# ---------------------------------------------------------------------------
def test_channel_for_falls_back_to_default():
    d = SINK.Dispatcher({"oncall": "wecom", "default": "feishu"},
                        {"wecom": "u", "feishu": "u"})
    assert d.channel_for("oncall") == "wecom"
    assert d.channel_for("某个没配过的receiver") == "feishu"
    assert d.channel_for("") == "feishu"


def test_deliver_reports_skipped_when_unconfigured():
    """未配置 ⇒ 归入 `skipped`，**不是** `failed`。

    两者的处置完全不同：failed 值得重试，skipped 重试一万次也没用
    （是配置缺失）。混在一起会让人对着一个配置错误反复重试。
    """
    d = SINK.Dispatcher({"oncall": "wecom"}, {"wecom": ""})
    sent, skipped, failed = d.deliver("oncall", _payload())
    assert sent == [] and failed == []
    assert skipped and "未配置" in skipped[0]


def test_deliver_reports_skipped_when_no_channel_configured():
    d = SINK.Dispatcher({}, {})
    sent, skipped, failed = d.deliver("oncall", _payload())
    assert skipped and failed == []


def test_deliver_never_touches_network_in_dry_run(monkeypatch):
    """DRY_RUN 下绝对不能真的发出去。

    否则"我先看看配置对不对"这一步会真的往生产群里推消息。
    """
    def explode(*a, **k):
        raise AssertionError("DRY_RUN 不应该发起网络请求")

    monkeypatch.setattr(SINK.urllib.request, "urlopen", explode)
    d = SINK.Dispatcher({"oncall": "wecom"}, {"wecom": "http://example.invalid/x"},
                        dry_run=True)
    sent, skipped, failed = d.deliver("oncall", _payload())
    assert sent == [] and failed == [] and skipped


def test_deliver_falls_back_to_a_configured_channel(monkeypatch):
    """**路由指向的通道没地址 ⇒ 改发到配了地址的那个**（2026-10-04 用户口径）。

    这是实际踩过的故障：路由四条全指 wecom，而只有 feishu 填了地址 ⇒
    旧行为静默跳过（记 ERROR、HTTP 仍回 200），Alertmanager 收到 200 就认为
    通知成功、不再重试 —— 现象是"两边看起来都配了，告警一条都到不了人"。
    """
    monkeypatch.setattr(SINK.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp('{"errcode": 0}'))
    d = SINK.Dispatcher({"oncall": "wecom", "default": "wecom"},
                        {"wecom": "", "feishu": "http://example.invalid/fs"})
    sent, skipped, failed = d.deliver("oncall", _payload())
    assert failed == [] and skipped == []
    assert sent and "feishu" in sent[0]
    assert "兜底" in sent[0], (
        "走了兜底却没在理由里说明 —— 值班的人会以为配置本来就是对的，"
        "下次清掉那个通道的地址时又会静默丢告警"
    )


def test_fallback_prefers_a_channel_the_routes_already_use():
    """兜底优先选**路由表里出现过**的通道（那是配置意图），而不是固定顺序的第一个。

    `oncall:wecom, default:generic` 而 wecom 没填地址时，generic 才是配置者的意图；
    直接按固定顺序选到 feishu，等于发到一个他从来没打算用的地方。
    """
    d = SINK.Dispatcher({"oncall": "wecom", "default": "generic"},
                        {"wecom": "", "feishu": "u-feishu", "generic": "u-generic"})
    assert d.fallback_channel() == "generic"


def test_fallback_keeps_a_stable_order_when_no_route_matches():
    """路由表里一个可用通道都没有时按固定顺序取 —— 保证每次重启选到同一个。

    不固定的后果很小但很烦人：这次发到企微、下次发到飞书，
    值班的人会以为自己看错了群。
    """
    d = SINK.Dispatcher({"oncall": "wecom"},
                        {"wecom": "", "feishu": "u-feishu", "generic": "u-generic"})
    assert d.fallback_channel() == "feishu"
    assert d.fallback_channel() == "feishu"


def test_fallback_never_happens_in_dry_run(monkeypatch):
    """dry-run 的语义是"什么都不发"；在那里换通道会把它变成"换个通道发"。"""
    def explode(*a, **k):
        raise AssertionError("DRY_RUN 不应该发起网络请求")

    monkeypatch.setattr(SINK.urllib.request, "urlopen", explode)
    d = SINK.Dispatcher({"oncall": "wecom"},
                        {"wecom": "", "feishu": "http://example.invalid/fs"},
                        dry_run=True)
    sent, skipped, failed = d.deliver("oncall", _payload())
    assert sent == [] and failed == [] and skipped


def test_notify_succeeds_via_fallback(monkeypatch):
    """错位配置下回滚通知也要发得出去 —— 否则 rollback.sh 会打印"没能发出"。

    退出码是那个脚本判断"已通知 / 没能通知"的唯一依据（见上一条用例）。
    """
    monkeypatch.setattr(SINK.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp('{"errcode": 0}'))
    d = SINK.Dispatcher({"oncall": "wecom"},
                        {"wecom": "", "feishu": "http://example.invalid/fs"})
    assert SINK.notify(d, "oncall", summary="回滚已执行") == 0


def test_post_json_returns_false_for_a_malformed_url():
    """地址漏了 scheme（写成 `u-feishu`）不能抛异常。

    `Request(...)` 抛的是 ValueError 而不是 URLError —— 只捕后者的话，一个通道的
    地址写错会让整批告警变成 500，Alertmanager 接着反复重试同一批。
    本函数的契约是"网络层任何异常都归为这次没送到"。
    """
    ok, msg = SINK.post_json("u-feishu", {})
    assert ok is False and "ValueError" in msg


def test_diagnose_routing_reports_the_mismatch():
    """错位必须在启动日志里显形：兜底救了它，但"配置是错的"不该被掩盖。"""
    msg = SINK.diagnose_routing({"oncall": "wecom", "default": "wecom"},
                                {"wecom": "", "feishu": "u"})
    assert "错位" in msg and "oncall" in msg and "feishu" in msg


def test_diagnose_routing_is_quiet_when_aligned():
    """没错位时不许刷日志 —— 一条永远出现的 WARNING 等于没有 WARNING。"""
    assert SINK.diagnose_routing({"oncall": "wecom"}, {"wecom": "u"}) == ""
    assert SINK.diagnose_routing({}, {}) == ""


# ---------------------------------------------------------------------------
# 自定义通知：回滚等**非 Prometheus 来源**的事件
# ---------------------------------------------------------------------------
def test_notify_payload_renders_the_summary_and_is_marked_as_a_change():
    """通知复用告警的渲染契约 ⇒ 值班的人不需要第二套阅读习惯。"""
    payload = SINK.build_notify_payload("oncall", summary="回滚已执行：原因 X")
    text = SINK.render_text(payload)
    assert "回滚已执行：原因 X" in text, "渲染出来的文本里没有我们传进去的说明"
    assert payload["alerts"][0]["labels"]["category"] == "change"
    assert payload["alerts"][0]["annotations"]["runbook"], (
        "没有 runbook —— 收到通知的人不知道该翻哪份文档"
    )


def test_notify_returns_zero_only_when_a_channel_actually_sent(monkeypatch):
    """退出码 0 的含义必须是"真的发出去了"。

    它是 `deploy/rollback.sh` 判断"已通知 / 没能通知"的**唯一**依据。
    放宽成"跑过就算成功"，脚本就会在什么都没发出去时告诉运维"已通知" ——
    与这条链路一贯的失败模式（未配置只记日志、接口照样返回 200）完全同构。
    """
    monkeypatch.setattr(SINK.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp('{"errcode": 0}'))
    d = SINK.Dispatcher({"oncall": "wecom"}, {"wecom": "http://example.invalid/hook"})
    assert SINK.notify(d, "oncall", summary="回滚已执行") == 0


def test_notify_returns_failure_when_no_channel_is_configured():
    """通道未配置 ⇒ 非 0。这正是 rollback.sh 要打印"没能发出"的那条分支。"""
    d = SINK.Dispatcher({"oncall": "wecom"}, {"wecom": ""})
    assert SINK.notify(d, "oncall", summary="回滚已执行") == 1


# ---------------------------------------------------------------------------
# post_json 的判定：HTTP 200 不等于送达
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, body: str = "", status: int = 200):
        self._raw = body.encode("utf-8")
        self.status = status

    def read(self, n=None):
        return self._raw if n is None else self._raw[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_post_json_treats_errcode_nonzero_as_failure(monkeypatch):
    """HTTP 200 + body 里 errcode 非 0 = **没送到**。

    企微/飞书在"机器人被移出群""token 失效"时**仍返回 200**，只在 body 里
    给一个非 0 的 errcode。只看状态码的实现会把这种情况记成"已送达"，
    于是告警持续静默丢失 —— 这是本网关最需要防的一种假绿。
    """
    monkeypatch.setattr(
        SINK.urllib.request, "urlopen",
        lambda *a, **k: _FakeResp('{"errcode": 93000, "errmsg": "invalid webhook url"}'))
    ok, msg = SINK.post_json("http://example.invalid/hook", {"a": 1})
    assert ok is False
    assert "93000" in msg and "invalid webhook url" in msg


def test_post_json_accepts_errcode_zero(monkeypatch):
    monkeypatch.setattr(SINK.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp('{"errcode": 0, "errmsg": "ok"}'))
    ok, msg = SINK.post_json("http://example.invalid/hook", {})
    assert ok is True and "HTTP 200" in msg


def test_post_json_accepts_plain_text_response(monkeypatch):
    """自建网关可能返回纯文本 —— 不能因为"不是 JSON"就判失败。"""
    monkeypatch.setattr(SINK.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp("OK"))
    ok, _ = SINK.post_json("http://example.invalid/hook", {})
    assert ok is True


def test_post_json_returns_false_instead_of_raising(monkeypatch):
    """网络异常必须变成 `(False, 说明)`，不能往外抛。

    抛出去的后果：一条通道挂掉 ⇒ 整批告警的响应变 500 ⇒ Alertmanager 重试整组
    ⇒ 把**已经成功的通道**重复轰炸一遍。
    """
    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(SINK.urllib.request, "urlopen", boom)
    ok, msg = SINK.post_json("http://example.invalid/hook", {})
    assert ok is False and "connection refused" in msg


# ---------------------------------------------------------------------------
# HTTP 层：起真实服务测"线格式"
# ---------------------------------------------------------------------------
@pytest.fixture()
def live_sink():
    dispatcher = SINK.Dispatcher({"oncall": "wecom"},
                                 {"wecom": "", "feishu": "", "generic": ""})  # 全未配 ⇒ dry-run
    server = SINK.ThreadingHTTPServer(("127.0.0.1", 0), SINK.make_handler(dispatcher, "s3cr3t"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _request(url, method="GET", payload=None, secret=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if secret:
        headers["X-Alert-Sink-Secret"] = secret
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read().decode("utf-8")
            return r.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        return e.code, (json.loads(raw) if raw.strip() else {})


def test_health_live_is_200(live_sink):
    code, body = _request(f"{live_sink}/health/live")
    assert code == 200 and body["status"] == "ok"


def test_post_requires_secret_when_configured(live_sink):
    """配了 shared secret 就必须校验 —— 否则内网任何服务都能往里塞告警。"""
    code, _ = _request(f"{live_sink}/alerts/oncall", "POST", _payload())
    assert code == 403

    code, body = _request(f"{live_sink}/alerts/oncall", "POST", _payload(), secret="wrong")
    assert code == 403

    code, body = _request(f"{live_sink}/alerts/oncall", "POST", _payload(), secret="s3cr3t")
    assert code == 200


@pytest.mark.parametrize("early_path,early_code", [("/alerts/oncall", 403), ("/nope", 404)])
def test_an_early_rejection_does_not_desync_the_connection(live_sink, early_path,
                                                           early_code):
    """抢先返回（403 / 404）之后，**同一条连接**上的下一条告警不能被错位。

    为什么值得单独钉一条（2026-10-06 实测复现）
    -------------------------------------------
    handler 用的是 `HTTP/1.1`（连接默认 keep-alive），而 403 / 404 这些提前返回
    的分支**从没碰过请求体** ⇒ 残留在接收缓冲里的 JSON 字节会被
    `BaseHTTPRequestHandler` 当成**下一条请求的请求行**。

    实测（一条连接上先发无密钥的、再发带正确密钥的）：
    第一条得 403，**第二条拿到 `400 <!DOCTYPE HTML>`** —— 那条正确的告警被丢掉了。
    Alertmanager 侧用的是 Go 的 keep-alive 客户端，所以这不是测试专属问题；
    它的生产表现是"配置改错一次之后，同一连接上后续**正确**的告警全部静默丢失"。
    它还会在 Windows 上引发连接重置（`WinError 10053`），让本文件随机变红 ——
    而随机变红的测试会让人对真红的测试脱敏。

    用一个真实连接（`http.client`）而不是每请求新开一条，是因为**只有复用连接
    才看得见**这个 bug：`urllib.request` 每次请求都新开连接，恰好绕开了它。
    """
    parts = urllib.parse.urlsplit(live_sink)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    try:
        body = json.dumps(_payload()).encode("utf-8")
        base = {"Content-Type": "application/json", "Content-Length": str(len(body))}

        conn.request("POST", early_path, body=body, headers=base)
        first = conn.getresponse()
        first.read()
        assert first.status == early_code

        conn.request("POST", "/alerts/oncall", body=body,
                     headers={**base, "X-Alert-Sink-Secret": "s3cr3t"})
        second = conn.getresponse()
        raw = second.read()
        assert second.status == 200, (
            f"同一条连接上的下一条请求被错位了（拿到 {second.status}，期望 200）："
            f"{early_code} 分支没有把请求体读走，残留字节被当成了下一条请求的请求行。\n"
            f"{raw[:200]!r}"
        )
    finally:
        conn.close()


def test_post_reports_skipped_channel_with_200(live_sink):
    """dry-run（未配凭据）时返回 200 并把原因放进 `skipped`。

    刻意**不返回 500**：让 Alertmanager 重试一个"配置缺失"只会造成无意义的
    重复投递，真正的解法是跑 --selftest 把它抓出来。
    """
    code, body = _request(f"{live_sink}/alerts/oncall", "POST", _payload(), secret="s3cr3t")
    assert code == 200
    assert body["failed"] == []
    assert body["skipped"] and "未配置" in body["skipped"][0]


def test_post_rejects_oversized_and_bad_body(live_sink):
    req = urllib.request.Request(
        f"{live_sink}/alerts/oncall", data=b"{not json",
        headers={"Content-Type": "application/json", "X-Alert-Sink-Secret": "s3cr3t"},
        method="POST")
    try:
        urllib.request.urlopen(req, timeout=5)
        pytest.fail("非法 JSON 不该被接受")
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_unknown_path_is_404(live_sink):
    code, _ = _request(f"{live_sink}/nope")
    assert code == 404


def test_index_does_not_leak_webhook_urls():
    """`GET /` 只回显"配没配"，**绝不回显 URL 本身**（URL 含 key）。

    这个端点在部署后是内网可达的，回显 URL 等于把凭据泄给任何能访问它的人。
    """
    dispatcher = SINK.Dispatcher(
        {"oncall": "wecom"},
        {"wecom": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SUPERSECRET",
         "feishu": "", "generic": ""})
    server = SINK.ThreadingHTTPServer(("127.0.0.1", 0), SINK.make_handler(dispatcher, ""))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        code, body = _request(f"http://127.0.0.1:{server.server_address[1]}/")
        assert code == 200
        dumped = json.dumps(body, ensure_ascii=False)
        assert "SUPERSECRET" not in dumped
        assert body["channels"]["wecom"] == "configured"
        assert body["channels"]["feishu"] == "missing"
    finally:
        server.shutdown()
        server.server_close()
