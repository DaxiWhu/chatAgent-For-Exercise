"""灰度判据脚本（`scripts/canary_check.py`）的行为测试。

为什么值得测
------------
灰度判据的失败模式**几乎全是"看起来通过了"**，而它的输出直接决定"要不要回滚"：

  - 灰度**没有流量**（nginx 权重写错、灰度容器没起来）⇒ 面板干净 ⇒ 判"通过" ⇒
    把一个**从未被验证过**的版本放量到 100%。这是最贵的一种假绿。
  - Prometheus 返回 `NaN`（窗口内无样本）⇒ 所有 `>` 比较都是 False ⇒ 判"通过"。
  - 灰度只承载 10% 流量 ⇒ 绝对错误数天然小 ⇒ "看起来只有 3 个错误"，其实是稳定版的 5 倍。
  - Prometheus 连不上 ⇒ 若归成"失败"，会去回滚一个其实没问题的版本；
    若归成"通过"，就等于在没有任何数据的情况下放量。两种都错，必须单列一种结论。

所以这里测的是**判定逻辑与退出码**，不是"脚本能跑"。
"""

from __future__ import annotations

import importlib.util
import json
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "canary_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("canary_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CB = _load()


# ---------------------------------------------------------------------------
# 假 Prometheus
# ---------------------------------------------------------------------------
def _classify(expr: str) -> str:
    """按查询内容归类到某个判据。顺序有讲究：先匹配最特征化的片段。"""
    if "histogram_quantile" in expr:
        return "p95"
    if "chat_stream_errors_total" in expr:
        return "stream"
    if "llm_tokens_total" in expr:
        return "tokens"
    if "increase(" in expr:
        return "counts"
    if 'status=~"5.."' in expr:
        return "errors"
    return "reqrate"


# 默认场景：两条腿表现一致（全绿）。
_HEALTHY = {
    "counts": {"canary": 1000, "stable": 9000},
    "errors": {"canary": 0.005, "stable": 0.005},
    "p95": {"canary": 0.40, "stable": 0.40},
    "stream": {"canary": 0.0, "stable": 0.0},
    "tokens": {"canary": 800.0, "stable": 800.0},
    "reqrate": {"canary": 1.6, "stable": 15.0},
}


class _FakeProm(BaseHTTPRequestHandler):
    values: dict = {}
    queries: list = []
    status = "success"

    def log_message(self, *a):  # 静音
        pass

    def do_GET(self):  # noqa: N802
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        expr = (params.get("query") or [""])[0]
        type(self).queries.append(expr)

        if type(self).status != "success":
            body = json.dumps({"status": "error", "error": "bad_data"}).encode()
            self._send(body)
            return

        vals = type(self).values.get(_classify(expr), {})
        result = [{"metric": {"release": k}, "value": [0, str(v)]}
                  for k, v in vals.items()]
        self._send(json.dumps({"status": "success",
                               "data": {"resultType": "vector", "result": result}}).encode())

    def _send(self, raw: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture()
def prom():
    _FakeProm.values = json.loads(json.dumps(_HEALTHY))
    _FakeProm.queries = []
    _FakeProm.status = "success"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeProm)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _run(prom_url: str, capsys, **kw):
    """跑一次 CLI，返回 (退出码, 解析后的 JSON 报告或 None)。"""
    argv = ["--prometheus-url", prom_url, "--window", kw.pop("window", "10m"), "--json"] + kw.pop("extra", [])
    rc = CB.main(argv)
    out = capsys.readouterr().out
    report = None
    if out.strip().startswith("{"):
        report = json.loads(out)
    return rc, report


def _verdict(report, name):
    for item in report["verdict"]:
        if item["name"] == name:
            return item
    raise AssertionError(f"报告里没有判据 {name!r}，实际：{[i['name'] for i in report['verdict']]}")


# ---------------------------------------------------------------------------
# 全绿 / 各类劣化
# ---------------------------------------------------------------------------
def test_healthy_canary_passes(prom, capsys):
    rc, report = _run(prom, capsys)
    assert rc == CB.RC_PASS
    assert all(i["status"] == "PASS" for i in report["verdict"])


def test_error_rate_regression_fails(prom, capsys):
    """5xx 比例成倍劣化必须 FAIL（这是回滚的首要理由）。"""
    _FakeProm.values["errors"] = {"canary": 0.05, "stable": 0.005}
    rc, report = _run(prom, capsys)
    assert rc == CB.RC_FAIL
    assert _verdict(report, "5xx 比例")["status"] == "FAIL"


def test_latency_regression_fails_even_when_errors_are_fine(prom, capsys):
    """延迟劣化必须能单独判失败 —— 灰度最典型的问题是"变慢"而不是"报错"。"""
    _FakeProm.values["p95"] = {"canary": 0.95, "stable": 0.40}
    rc, report = _run(prom, capsys)
    assert rc == CB.RC_FAIL
    assert _verdict(report, "P95 延迟")["status"] == "FAIL"
    assert _verdict(report, "5xx 比例")["status"] == "PASS"


def test_stream_errors_fail_even_though_http_status_is_2xx(prom, capsys):
    """流式错误率必须独立成判据。

    本项目主接口是 SSE：流中途挂掉时**响应头早就发出去了**，HTTP 状态码看着
    完全正常。只看 5xx 会 100% 漏掉"新版本把流写坏了"这一类问题。
    """
    _FakeProm.values["stream"] = {"canary": 0.5, "stable": 0.0}
    rc, report = _run(prom, capsys)
    assert rc == CB.RC_FAIL
    assert _verdict(report, "流式错误率")["status"] == "FAIL"
    assert _verdict(report, "5xx 比例")["status"] == "PASS"


def test_token_inflation_fails(prom, capsys):
    """每请求 token 膨胀必须失败：新版 prompt 变长 / 多召回了几块，
    在 5xx 与延迟上都看不出来，但账单会翻倍。"""
    _FakeProm.values["tokens"] = {"canary": 2000.0, "stable": 800.0}
    rc, report = _run(prom, capsys)
    assert rc == CB.RC_FAIL
    assert _verdict(report, "每请求 token")["status"] == "FAIL"


def test_absolute_breach_fails_even_when_stable_is_equally_bad(prom, capsys):
    """绝对超标要能单独成立。

    只看相对会被坏基线带偏（稳定版自己已经 5% 时，灰度 5.2% 会被当成"没劣化"）。
    代价是这种判定在**整体故障**时也会报 FAIL —— 所以报告里必须同时给出
    稳定版的值，并显式提示"更像整体故障"。这条断言把那个提示也钉住。
    """
    _FakeProm.values["errors"] = {"canary": 0.052, "stable": 0.050}
    rc, report = _run(prom, capsys)
    assert rc == CB.RC_FAIL
    item = _verdict(report, "5xx 比例")
    assert item["status"] == "FAIL"
    assert "整体故障" in item["reason"], "没有提示「稳定版同样超标」，人会被引向回滚灰度"


# ---------------------------------------------------------------------------
# 最危险的一类：假绿
# ---------------------------------------------------------------------------
def test_zero_canary_traffic_is_undecidable_not_pass(prom, capsys):
    """灰度**没有流量**时绝不能判通过。

    这是本脚本存在的首要理由：nginx 权重写错、灰度容器没起来、抓取目标选错，
    都会让灰度"一条请求都没有"。此时所有指标都是干净的，任何"只看指标"的判据
    都会给出通过 —— 然后你把一个从未被验证过的版本放量到 100%。
    """
    _FakeProm.values["counts"] = {"canary": 0, "stable": 9000}
    rc, report = _run(prom, capsys)
    assert rc == CB.RC_UNDECIDABLE
    assert _verdict(report, "灰度样本量")["status"] == "UNDECIDABLE"


def test_insufficient_samples_is_undecidable(prom, capsys):
    """样本量低于门槛时报"无法判定"，且**不再继续**跑其它判据。

    继续跑的风险是"某一条刚好是绿的"，那种似是而非的结论比"没有结论"更糟。
    """
    _FakeProm.values["counts"] = {"canary": 5, "stable": 9000}
    rc, report = _run(prom, capsys, extra=["--min-requests", "30"])
    assert rc == CB.RC_UNDECIDABLE
    assert len(report["verdict"]) == 1, (
        f"样本量不足时仍在跑其它判据：{[i['name'] for i in report['verdict']]}"
    )


def test_nan_is_treated_as_missing_not_as_pass(prom, capsys):
    """Prometheus 的 `NaN` 必须被当成"没有数据"。

    直接 float('nan') 参与比较时**所有 `>` 都是 False** ⇒ 判据全部"通过"。
    这是最隐蔽的一种假绿：指标明明在报"我没数据"，脚本却给出绿灯。
    """
    _FakeProm.values["errors"] = {"canary": "NaN", "stable": 0.005}
    rc, report = _run(prom, capsys)
    assert rc == CB.RC_UNDECIDABLE
    assert _verdict(report, "5xx 比例")["status"] == "UNDECIDABLE"


def test_prometheus_error_status_is_undecidable(prom, capsys):
    _FakeProm.status = "error"
    rc, _report = _run(prom, capsys)
    assert rc == CB.RC_UNDECIDABLE


# ---------------------------------------------------------------------------
# 连不上：既不是"通过"也不是"失败"
# ---------------------------------------------------------------------------
def test_unreachable_prometheus_is_undecidable_not_fail(capsys, monkeypatch):
    """Prometheus 连不上必须是第 3 种结论。

    归成 FAIL ⇒ 会去回滚一个其实没问题的版本；
    归成 PASS ⇒ 等于在没有数据的情况下放量。
    两种都错，所以退出码必须独立（2），让调用方走"先修可见性"的分支。
    """
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    rc = CB.main(["--prometheus-url", "http://127.0.0.1:1", "--json"])
    assert rc == CB.RC_UNDECIDABLE
    assert rc not in (CB.RC_PASS, CB.RC_FAIL)


def test_missing_prometheus_url_is_undecidable(capsys, monkeypatch):
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    rc = CB.main(["--json"])
    assert rc == CB.RC_UNDECIDABLE


# ---------------------------------------------------------------------------
# 查询本身的两个易错点
# ---------------------------------------------------------------------------
def test_p95_query_does_not_hand_write_the_le_label(prom, capsys):
    """P95 查询必须用 `sum by (release, le)`，**不能**手写 `le="..."`。

    `deploy/monitoring/SLO.md` 记着这个坑：桶边界是字符串全匹配，
    写 `le="1"` 而实际输出是 `le="1.0"` 时规则**永不匹配且没有任何报错**。
    交给 `histogram_quantile` 自动取 le，从根上避开它。
    """
    _run(prom, capsys)
    p95_queries = [q for q in _FakeProm.queries if "histogram_quantile" in q]
    assert p95_queries, "没有发出 P95 查询"
    for q in p95_queries:
        assert 'le="' not in q, f"手写了 le 值（字符串全匹配极易写错且不报错）：{q}"
        assert "by (release, le)" in q, f"没有按 release 与 le 分组：{q}"


def test_queries_use_rate_over_absolute_counts(prom, capsys):
    """判据必须基于 rate，不能基于绝对计数。

    灰度只承载 10% 流量 ⇒ 绝对错误数天然是稳定版的十分之一。
    若按绝对值比较，灰度的错误**永远**看起来更少，等于永远判通过。
    """
    _run(prom, capsys)
    joined = "\n".join(_FakeProm.queries)
    assert "rate(" in joined, "判据里没有使用 rate —— 会被 9:1 的流量比例直接带偏"
    assert "$__rate_interval" not in joined


def test_queries_are_scoped_to_the_two_app_jobs(prom, capsys):
    """选择器必须只命中两个业务 job，不能把 blackbox 探针的指标混进来。

    blackbox 的 `probe_success` 与 `http_requests_total` 同名不同义；
    混进来会让分母无端变大、把真实的错误率稀释掉。
    """
    _run(prom, capsys)
    app_queries = [q for q in _FakeProm.queries if "http_requests_total" in q]
    assert app_queries
    for q in app_queries:
        assert 'job=~"chatagent-.+"' in q, f"没有限定业务 job：{q}"


def test_bad_window_is_rejected(capsys):
    """窗口格式错误必须直接报"无法判定"，不能把它拼进 PromQL 让 Prometheus 报语法错。"""
    rc = CB.main(["--prometheus-url", "http://127.0.0.1:1", "--window", "10 minutes"])
    assert rc == CB.RC_UNDECIDABLE


# ---------------------------------------------------------------------------
# 代理：与 qdrant_backup 同一个坑（Prometheus 通常就跑在本机）
# ---------------------------------------------------------------------------
def test_loopback_prometheus_bypasses_http_proxy(monkeypatch):
    """配了 `http_proxy` 而未配 `NO_PROXY` 时，访问本机 Prometheus 不能走代理。

    否则报错会指向"Prometheus 返回了 HTTP 502"（像服务端问题），
    而真实原因是根本没连上它 —— 排查方向会被完全带偏。

    这里断言**构造结果**而不是"请求有没有走代理"：后者在 Windows 上是假绿
    （urllib 还会读注册表里的绕过表，默认含 `<local>`），把实现改坏了也照样绿。
    """
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)

    def proxies_of(client):
        for h in client._opener.handlers:
            if isinstance(h, urllib.request.ProxyHandler):
                return dict(h.proxies)
        return None

    assert proxies_of(CB.Prom("http://localhost:9090")) in (None, {})
    assert proxies_of(CB.Prom("http://prometheus.example.com:9090"))
