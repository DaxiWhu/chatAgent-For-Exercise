#!/usr/bin/env python
"""告警通知网关：Alertmanager → 企业微信 / 飞书 / 自建网关。

为什么必须有这一层
------------------
`alertmanager.yml` 的 `webhook_configs.url` 是**唯一的送达出口**，而它有两个
绕不开的限制：

  1. **Alertmanager 原生不支持环境变量插值**（不像 compose 支持 `${VAR}`）。
     所以真实 webhook 地址（含 key）只能硬编码进 `alertmanager.yml` ——
     等于把凭据提交进版本库，且换一个接收端就要改配置 + 重启 Alertmanager。
  2. **企业微信/飞书机器人的 payload 与 Alertmanager 原生 JSON 不同**。
     直接把机器人 URL 填进 `webhook_configs.url`，对方会返回 400；
     Alertmanager 会重试几次然后放弃 —— 表现成"配置看起来全对，但没人收到"。

这一层把两件事一起解决：凭据只存在于本服务的环境变量里（不进版本库），
格式转换在这里做。Alertmanager 侧只需指向 `http://alert-sink:8080/alerts/<receiver>`。

（背景：`app/review_queue.py` 已经在"审核通知"上做过同样的三种格式转换，
本文件的 `to_wecom` / `to_feishu` 与它保持**一致的消息体形状** —— 同一套机器人
不该有两套 payload 约定。）

端点
----
  POST /alerts/<receiver>   接收 Alertmanager 事件。receiver 取 **URL 末段**，
                            不依赖 payload 的 receiver 字段（少一个出错的自由度）。
  GET  /health/live         存活探针（compose healthcheck 用）。
  GET  /                    当前路由表与通道配置摘要（**只回显"配没配"，不回显密钥**）。

路由
----
  ALERT_SINK_ROUTES=oncall:wecom,security-oncall:wecom,cost-digest:feishu,default:wecom
  左 = Alertmanager 的 receiver 名（= URL 末段），右 = 通道类型 wecom|feishu|generic。
  未在表里出现的 receiver 落到 `default`；连 `default` 都没有 ⇒ 该条只记日志。

凭据（**只在这里**，不进 alertmanager.yml）
----
  ALERT_SINK_WECOM_WEBHOOK / ALERT_SINK_FEISHU_WEBHOOK / ALERT_SINK_GENERIC_WEBHOOK

安全
----
  ALERT_SINK_SHARED_SECRET  配了则要求请求头 `X-Alert-Sink-Secret` 匹配
                            （防内网其它服务误打、或被当作跳板）。

自检（**别省这一步**）
----
  python alert_sink.py --selftest

  未配置的通道**不会报错**、只记一条日志 —— 所以"配置看起来对"和"告警真能到人"
  是两件事。自检会构造一条假告警走完渲染 → 转换 → 发送，并打印每个通道的结果。
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 支持的通道类型。generic = 自建网关（接受 Alertmanager 原生 JSON）。
CHANNEL_KINDS = ("wecom", "feishu", "generic")

# 一条消息里最多展开多少条告警。机器人消息过长的结果是"没人看完"，等于没发。
MAX_ALERTS_IN_TEXT = 8

# 请求体上限：Alertmanager 的 group 再大也不该超过 1MB。
MAX_BODY_BYTES = 1 << 20

HTTP_TIMEOUT = 5

# 只挑这几个人看的标签：全量 labels 会把消息撑爆，且含噪声。
KEY_LABELS = ("category", "severity", "release", "instance", "tenant")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def parse_routes(spec: str) -> dict[str, str]:
    """`oncall:wecom,default:feishu` → `{'oncall': 'wecom', 'default': 'feishu'}`。

    刻意**容忍脏输入**（空段、多余逗号、未知通道类型）：
    环境变量里多一个逗号、或者把 feishu 拼成 feishu h（带空格）是很常见的，
    不该让整个网关起不来 —— 起不来的后果是**所有告警都送不到**，
    比"少了一条路由"严重得多。
    """
    out: dict[str, str] = {}
    for item in (spec or "").split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        name, _, kind = item.partition(":")
        name, kind = name.strip().lower(), kind.strip().lower()
        if name and kind in CHANNEL_KINDS:
            out[name] = kind
        elif name:
            logging.warning("ALERT_SINK_ROUTES 里 %r 的通道类型 %r 未知（" 
                            "有效值：%s），该项被忽略", name, kind, "/".join(CHANNEL_KINDS))
    return out


def render_text(payload: dict) -> str:
    """把 Alertmanager 原生 JSON 渲染成一条**人读的纯文本**。

    只输出处置需要的信息（哪条、多严重、什么现象、怎么办）。
    刻意不把 labels 全量铺开：值班的人需要的是"instance=app-stable"和 runbook，
    不是 20 个 Prometheus 内部标签。
    """
    alerts = payload.get("alerts") or []
    if not alerts:
        return f"[告警] 空事件（receiver={payload.get('receiver') or '-'}）"

    firing = [a for a in alerts if (a.get("status") or "firing") != "resolved"]
    if not firing:
        title = f"[已恢复] {len(alerts)} 条告警已解除"
    elif len(firing) == len(alerts):
        title = f"[告警] {len(firing)} 条"
    else:
        title = f"[告警] {len(firing)} 条（另有 {len(alerts) - len(firing)} 条已恢复）"

    lines = [title]
    for i, a in enumerate(alerts[:MAX_ALERTS_IN_TEXT], start=1):
        labels = a.get("labels") or {}
        ann = a.get("annotations") or {}
        name = labels.get("alertname") or "?"
        sev = labels.get("severity") or ""
        suffix = "（已恢复）" if (a.get("status") or "") == "resolved" else ""
        lines.append(f"{i}. {name}" + (f" [{sev}]" if sev else "") + suffix)

        ctx = " ".join(f"{k}={labels[k]}" for k in KEY_LABELS if k in labels)
        if ctx:
            lines.append(f"   {ctx}")
        if ann.get("summary"):
            lines.append(f"   {ann['summary']}")
        if ann.get("runbook"):
            lines.append(f"   runbook: {ann['runbook']}")

    if len(alerts) > MAX_ALERTS_IN_TEXT:
        lines.append(f"……还有 {len(alerts) - MAX_ALERTS_IN_TEXT} 条，详见 Alertmanager 面板")
    return "\n".join(lines)


def to_wecom(text: str) -> dict:
    """企业微信机器人：必须是 `msgtype.text.content`，多余字段会被拒。"""
    return {"msgtype": "text", "text": {"content": text}}


def to_feishu(text: str) -> dict:
    """飞书机器人：必须是 `msg_type.content.text`。"""
    return {"msg_type": "text", "content": {"text": text}}


def to_generic(payload: dict, text: str) -> dict:
    """自建网关：保留 Alertmanager 原始 payload（含 alerts[]），另附渲染好的文本。"""
    body = dict(payload)
    body["rendered_text"] = text
    return body


def build_body(kind: str, payload: dict, text: str) -> dict:
    if kind == "wecom":
        return to_wecom(text)
    if kind == "feishu":
        return to_feishu(text)
    return to_generic(payload, text)


def post_json(url: str, body: dict, timeout: int = HTTP_TIMEOUT) -> tuple[bool, str]:
    """POST 一个 JSON，返回 `(成功, 说明)`。

    **不抛异常** —— 单个通道挂掉不该中断整批告警的处置；由调用方决定
    日志级别与 HTTP 响应码。

    ⚠️ 关键点：企微/飞书即使在 HTTP 200 时也可能在 **body 里**报错
    （`errcode` / `code` 非 0，例如 token 失效、机器人被移出群）。
    只看 HTTP 状态码会把这类"看起来发出去了、其实没到"当成成功 —— 必须读 body。
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", 200)
            raw = resp.read(600).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read(200).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001  读不出 body 不影响判定
            pass
        return False, f"HTTP {e.code} {detail[:120].strip()}"
    except Exception as e:  # noqa: BLE001  网络层任何异常都归为"这次没送到"
        return False, f"{type(e).__name__}: {e}"

    if not (200 <= code < 300):
        return False, f"HTTP {code} {raw[:120].strip()}"

    try:
        info = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001  非 JSON 响应（自建网关可能返回纯文本）
        info = {}
    if isinstance(info, dict):
        err = info.get("errcode", info.get("code"))
        if err not in (None, 0):
            msg = info.get("errmsg") or info.get("msg") or raw[:80]
            return False, f"业务错误 {err}: {msg}"
    return True, f"HTTP {code}"


class Dispatcher:
    """把 (receiver, payload) 送到对应的真实接收端。"""

    def __init__(self, routes: dict[str, str], hooks: dict[str, str],
                 dry_run: bool = False, timeout: int = HTTP_TIMEOUT):
        self.routes = dict(routes or {})
        self.hooks = dict(hooks or {})
        self.dry_run = bool(dry_run)
        self.timeout = timeout

    def channel_for(self, receiver: str) -> str:
        """URL 末段 → 通道类型；未在路由表里的走 `default` 兜底。"""
        key = (receiver or "").strip().lower()
        return self.routes.get(key) or self.routes.get("default") or ""

    def deliver(self, receiver: str, payload: dict) -> tuple[list[str], list[str], list[str]]:
        """投递一次，返回 `(已送达, 未发送, 失败)` 三组说明。

        三态而不是布尔：**"未发送"（dry-run / 没配）与"失败"（发了但对方拒了）
        是两种完全不同的故障**，混在一起会让人误以为"重试就能解决"。
        """
        label = f"{receiver or '-'}"
        kind = self.channel_for(receiver)
        if not kind:
            return [], [f"{label}: 未在 ALERT_SINK_ROUTES 中配置（连 default 都没有）"], []

        url = (self.hooks.get(kind) or "").strip()
        if self.dry_run or not url:
            why = "DRY_RUN（显式开启）" if self.dry_run else f"未配置 ALERT_SINK_{kind.upper()}_WEBHOOK"
            return [], [f"{label}→{kind}: {why}"], []

        text = render_text(payload)
        body = build_body(kind, payload, text)
        ok, msg = post_json(url, body, self.timeout)
        if ok:
            return [f"{label}→{kind}: {msg}"], [], []
        return [], [], [f"{label}→{kind}: {msg}"]


def make_handler(dispatcher: Dispatcher, secret: str):
    """构造 HTTP handler。secret 非空时校验 `X-Alert-Sink-Secret`。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "chatagent-alert-sink"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # noqa: A003  覆盖基类，统一走 logging
            logging.info("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: dict):
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802  基类命名
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path in ("/health/live", "/health"):
                return self._send(200, {"status": "ok"})
            if path == "/":
                # 只回显"配没配"，**绝不回显 URL 本身**（含 key）——
                # 这个端点在生产是内网可达的，回显等于泄凭据。
                return self._send(200, {
                    "routes": dispatcher.routes,
                    "channels": {k: ("configured" if v else "missing")
                                 for k, v in dispatcher.hooks.items()},
                    "dry_run": dispatcher.dry_run,
                })
            return self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            seg = [p for p in path.split("/") if p]
            if not seg or seg[0] != "alerts":
                return self._send(404, {"error": "not found"})

            if secret:
                got = self.headers.get("X-Alert-Sink-Secret") or ""
                if not hmac.compare_digest(got, secret):
                    return self._send(403, {"error": "forbidden"})

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, {"error": "bad content-length"})
            if length <= 0 or length > MAX_BODY_BYTES:
                return self._send(413, {"error": f"bad body size: {length}"})

            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                return self._send(400, {"error": f"invalid json: {e}"})
            if not isinstance(payload, dict):
                return self._send(400, {"error": "payload must be an object"})

            receiver = seg[1] if len(seg) > 1 else ""
            receiver = receiver or str(payload.get("receiver") or "default")

            sent, skipped, failed = dispatcher.deliver(receiver, payload)
            for s in sent:
                logging.info("已送达 %s", s)
            for s in skipped:
                # 未配置是**配置问题**，不是瞬时故障：用 ERROR 让它显眼，
                # 但要返回 200 —— 让 Alertmanager 重试一个配置错误只会造成
                # 无意义的重复投递，真正的解法是跑 --selftest 把它抓出来。
                logging.error("未发送 %s", s)
            for s in failed:
                logging.warning("发送失败 %s", s)

            # 有失败 ⇒ 500，让 Alertmanager 按它的退避策略重试（可恢复类故障）；
            # 全部成功或全部未配置 ⇒ 200（不值得重试）。
            code = 500 if failed else 200
            return self._send(code, {"receiver": receiver, "sent": sent,
                                     "skipped": skipped, "failed": failed})

    return Handler


def selftest(dispatcher: Dispatcher) -> int:
    """构造一条假告警走**完整**链路（渲染 → 格式转换 → 发送），打印每个通道结果。

    为什么这比"看配置对不对"更有价值：未配置的通道不抛异常、只记日志，
    所以配置文件的正确性与"告警真能到人"之间没有自动关联 —— 必须实跑一次。
    最后一步（机器人里是否真的出现消息）无法自动化，命令会明确提示人工确认。
    """
    payload = {
        "version": "4",
        "status": "firing",
        "receiver": "self-test",
        "commonLabels": {"alertname": "AlertSinkSelfTest"},
        "alerts": [{
            "status": "firing",
            "labels": {
                "alertname": "AlertSinkSelfTest",
                "category": "availability",
                "severity": "warning",
                "instance": "self-test",
            },
            "annotations": {
                "summary": "这是一条自检消息 —— 收到即说明告警通道畅通",
                "description": "由 deploy/monitoring/alert_sink.py --selftest 发出",
                "runbook": "deploy/RUNBOOK.md",
            },
            "startsAt": "1970-01-01T00:00:00Z",
            "fingerprint": "self-test",
        }],
    }

    print("=" * 68)
    print("告警网关自检")
    print("=" * 68)
    print(f"路由表   : {dispatcher.routes or '（空）'}")
    print("凭据     : " + ", ".join(
        f"{k}={'已配置' if v else '未配置'}" for k, v in dispatcher.hooks.items()))
    print(f"模式     : {'DRY_RUN（只打印，不发送）' if dispatcher.dry_run else '实际发送'}")
    print("-" * 68)

    targets = list(dispatcher.routes) or ["default"]
    n_failed = 0
    n_skipped = 0
    for r in targets:
        sent, skipped, failed = dispatcher.deliver(r, payload)
        for s in sent:
            print(f"  [已送达] {s}")
        for s in skipped:
            print(f"  [未发送] {s}")
        for s in failed:
            print(f"  [失败]   {s}")
        n_failed += len(failed)
        n_skipped += len(skipped)

    print("-" * 68)
    print("渲染出的消息体：")
    print("-" * 68)
    print(render_text(payload))
    print("-" * 68)

    if dispatcher.dry_run:
        print("结论：处于 DRY_RUN，**没有任何消息真正发出**。")
        print("     配好 ALERT_SINK_*_WEBHOOK 后重跑本命令；机器人里出现消息才算接通。")
        return 0
    if n_failed:
        print(f"结论：{n_failed} 个通道发送失败，请按上面的原因排查（凭据/网络/机器人是否在群）。")
        return 1
    if n_skipped:
        print(f"结论：{n_skipped} 个通道未发送（配置缺失），这些 receiver 的告警会丢。")
        return 0
    print("结论：所有通道发送成功。")
    print("     ⚠️ 最后一步无法自动化：请确认机器人/网关**真的收到了**上面这条消息。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Alertmanager 告警通知网关")
    ap.add_argument("--selftest", action="store_true",
                    help="构造一条假告警走完整链路后退出（不监听端口）")
    ap.add_argument("--port", type=int, default=int(_env("ALERT_SINK_PORT", "8080")),
                    help="监听端口（默认 8080，亦可用 ALERT_SINK_PORT）")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    routes = parse_routes(_env("ALERT_SINK_ROUTES", "default:wecom"))
    hooks = {k: _env(f"ALERT_SINK_{k.upper()}_WEBHOOK") for k in CHANNEL_KINDS}
    dry = _env("ALERT_SINK_DRY_RUN").lower() not in ("", "0", "false", "no")

    # 一个凭据都没配时**强制** dry-run：否则每来一条告警都要等一次无意义的
    # 网络失败（Alertmanager 侧表现为超时），还会把日志刷满。
    if not any(hooks.values()):
        logging.warning(
            "未配置任何 ALERT_SINK_*_WEBHOOK ⇒ 强制 DRY_RUN："
            "告警只会写进本服务日志，**不会送达任何人**。"
        )
        dry = True

    dispatcher = Dispatcher(routes, hooks, dry_run=dry)

    # 启动回显：与项目其它服务同口径 —— 只回显布尔/条数，不回显凭据本身。
    logging.info("告警网关配置：routes=%s channels=%s dry_run=%s",
                 routes or "{}", {k: bool(v) for k, v in hooks.items()}, dry)

    if args.selftest:
        return selftest(dispatcher)

    secret = _env("ALERT_SINK_SHARED_SECRET")
    if not secret:
        logging.warning("未设置 ALERT_SINK_SHARED_SECRET：任何能访问该端口的人"
                        "都可以往里塞告警（建议内网也配上）。")

    handler = make_handler(dispatcher, secret)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), handler)  # noqa: S104
    logging.info("监听 0.0.0.0:%d —— POST /alerts/<receiver>，GET /health/live", args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("收到中断信号，退出")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
