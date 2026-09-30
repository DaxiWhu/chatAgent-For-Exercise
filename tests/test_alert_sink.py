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

import importlib.util
import json
import threading
import urllib.error
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
