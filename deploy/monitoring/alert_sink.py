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

  ⚠️ 飞书侧**别开「签名校验」**：`to_feishu()` 只发 `{msg_type, content}`，
  **不带 timestamp/sign**，开了就是每个请求都被拒（`19021`）。飞书官方却在
  **强烈建议**加安全设置 ⇒ 这是最容易顺手踩上的一脚。要收紧只用
  「自定义关键词」，填「告警」（`render_text()` 的第一行两种形态都含它）。
  企微侧对应的那一条在下面「安全」一节。

安全
----
  ALERT_SINK_SHARED_SECRET  配了则要求请求头 `X-Alert-Sink-Secret` 匹配
                            （防同 compose 网络内的其它服务误打、或被当作跳板）。

  ⚠️ 默认**刻意留空**，开之前先确认 `alertmanager.yml` 的四个 receiver 都配了
  `http_config.http_headers` —— 它们默认没有（那几行是注释掉的），所以"顺手加上
  一个密钥"这种看起来更安全的动作，直接后果是把自己的告警**全部打成 403**。
  要用必须两处一起改；只用一处的话，故障现象是"配置全对但没人收到"。

自检（**别省这一步**）
----
  python alert_sink.py --selftest

  未配置的通道**不会报错**、只记一条日志 —— 所以"配置看起来对"和"告警真能到人"
  是两件事。自检会构造一条假告警走完渲染 → 转换 → 发送，并打印每个通道的结果。

自定义通知（回滚等**非 Prometheus 来源**的事件）
----
  python alert_sink.py --notify oncall --summary "回滚已执行：..."

  不是所有要发给人看的事都来自 Prometheus。典型是**回滚已执行**：它是一条
  "动作记录"，不会触发 `alerts.yml` 里的任何规则（回滚本身不是异常指标），
  所以只能主动投递。本模式复用同一条渲染与通道链路 ⇒ 消息形态与真实告警一致，
  值班的人不需要第二套阅读习惯。

  退出码：**0 当且仅当真的送达了某个通道**（1 = 没发出去）。调用方
  （`deploy/rollback.sh`）靠这个区分"已通知"与"没能通知"，别把它放宽成"运行过就算成功"。
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


def diagnose_routing(routes: dict[str, str], hooks: dict[str, str]) -> str:
    """路由表与凭据**错位**时给一句人读的说明（空串 = 没错位）。

    为什么要单独诊断：这是这条链路最常见的失效方式 —— 两边都"配了"，但配的不是
    同一个通道（路由指向企微、地址却填在飞书）。投递侧现在会兜底救回来，但
    **"救了"这件事本身必须被人知道**：否则下次改动时没人知道配置是错的，
    而兜底一旦因为某个通道的地址被清掉而失效，告警就又静默丢了。
    """
    configured = [k for k in CHANNEL_KINDS if (hooks.get(k) or "").strip()]
    if not configured:
        return ""
    if not routes:
        return (f"ALERT_SINK_ROUTES 为空，但有已配置的通道 {configured} "
                f"⇒ 所有 receiver 走兜底投递")
    bad = [r for r, k in routes.items() if not (hooks.get(k) or "").strip()]
    if not bad:
        return ""
    return (f"路由与凭据错位：{len(bad)} 个 receiver（{', '.join(sorted(bad))}）"
            f"被路由到没有地址的通道，将**兜底**改发到 {configured}。"
            f"建议把路由改成指向已配置的通道，或把地址填进当前路由指向的通道。")


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
    """飞书机器人：必须是 `msg_type.content.text`。

    刻意**不发** `timestamp` / `sign` ⇒ 飞书那侧的「签名校验」必须保持关闭，
    开了每个请求都会被拒（`19021`）。见本文件顶部「凭据」一节的说明。
    """
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
    # ⚠️ `Request(...)` 也必须在 try 里：地址写成 `u-feishu`（漏了 scheme）时它抛的是
    # ValueError 而不是 URLError —— 落在外面会一路冒到 HTTP handler，把"某个通道配错
    # 地址"升级成整批告警 500（Alertmanager 随后反复重试同一批）。本函数承诺不抛异常。
    try:
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
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

    def configured_channels(self) -> list[str]:
        """**真有地址**的通道，按 `CHANNEL_KINDS` 的固定顺序。

        顺序固定（不按 dict 插入顺序）是为了让兜底结果可预测 —— 同一个配置
        每次重启都该选到同一个通道，否则"这次发到企微、下次发到飞书"会让
        值班的人以为自己看错了。
        """
        return [k for k in CHANNEL_KINDS if (self.hooks.get(k) or "").strip()]

    def fallback_channel(self) -> str:
        """兜底通道：**路由表里用到过的**优先，其次按固定顺序。

        为什么优先路由表里出现过的：路由是"我们打算用哪些通道"的表达。
        `oncall:wecom,default:feishu` 而 wecom 没填地址时，选 feishu 比选一个
        从没在路由里出现过的通道更贴近配置者的意图。
        """
        available = self.configured_channels()
        if not available:
            return ""
        for kind in self.routes.values():
            if kind in available:
                return kind
        return available[0]

    def deliver(self, receiver: str, payload: dict) -> tuple[list[str], list[str], list[str]]:
        """投递一次，返回 `(已送达, 未发送, 失败)` 三组说明。

        三态而不是布尔：**"未发送"（dry-run / 没配）与"失败"（发了但对方拒了）
        是两种完全不同的故障**，混在一起会让人误以为"重试就能解决"。

        **兜底规则：配置了哪个通道，就发哪个。** 路由表（`ALERT_SINK_ROUTES`）与
        通道地址（`ALERT_SINK_*_WEBHOOK`）是两个独立配置项，很容易出现"路由指向 A、
        地址却填在 B"。旧行为在这里静默跳过（记一条 ERROR、HTTP 仍回 200），
        Alertmanager 收到 200 就认为通知成功、不再重试 ⇒ 现象是**两边看起来都配了、
        告警一条都到不了人**（2026-10-04 实际踩到，见 `--selftest`）。
        所以只要**有任何**通道填了地址，就一定能送出去；一个都没填才判"未发送"。
        """
        label = f"{receiver or '-'}"
        kind = self.channel_for(receiver)
        url = (self.hooks.get(kind) or "").strip() if kind else ""
        note = ""

        # 兜底只在**真要发送**时做：dry-run 的语义是"什么都不发"，
        # 在那里换通道会把它变成"换个通道发"，那是另一件事。
        if not url and not self.dry_run:
            alt = self.fallback_channel()
            if alt:
                note = (f"（路由指向 {kind}，该通道无地址 ⇒ 兜底）" if kind
                        else "（无路由记录 ⇒ 兜底）")
                kind, url = alt, (self.hooks.get(alt) or "").strip()

        if self.dry_run:
            return [], [f"{label}→{kind or '-'}: DRY_RUN（显式开启）"], []
        if not kind:
            return [], [f"{label}: 未在 ALERT_SINK_ROUTES 中配置（连 default 都没有）"], []
        if not url:
            return [], [f"{label}→{kind}: 未配置 ALERT_SINK_{kind.upper()}_WEBHOOK"], []

        text = render_text(payload)
        body = build_body(kind, payload, text)
        ok, msg = post_json(url, body, self.timeout)
        if ok:
            return [f"{label}→{kind}: {msg}{note}"], [], []
        return [], [], [f"{label}→{kind}: {msg}{note}"]


def make_handler(dispatcher: Dispatcher, secret: str):
    """构造 HTTP handler。secret 非空时校验 `X-Alert-Sink-Secret`。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "chatagent-alert-sink"
        protocol_version = "HTTP/1.1"

        #: 单条连接的读写超时（秒）。**必须有**，两个原因：
        #:   ① `_drain_body` 会读走提前返回时残留的请求体，而"声明了
        #:      `Content-Length` 却不发体"的请求会让那次读**永远等下去** ——
        #:      没有超时就是一个线程被一条连接钉死；`ThreadingHTTPServer`
        #:      下开 N 条连接就能把线程占满。
        #:   ② `handle_one_request` 对 keep-alive 连接的空闲等待也吃这个超时，
        #:      于是"空闲连接"会被回收。否则 Alertmanager 侧（Go 客户端默认把
        #:      空闲连接留 90s）每条连接都长时间占着一个线程。
        timeout = 30

        #: 本请求的请求体有没有被读走。**每条连接一个 handler 实例、实例上会连着
        #: 处理多个请求**，所以每个 `do_*` 开头都要重置（见 `_begin`）。
        _body_read = False

        def log_message(self, fmt, *args):  # noqa: A003  覆盖基类，统一走 logging
            logging.info("%s %s", self.address_string(), fmt % args)

        def _begin(self) -> None:
            """每个请求的开头把"体已读"清零。

            必须逐请求重置：一条 keep-alive 连接上 `self` 是同一个对象，
            第一次 POST 把体读掉之后若不清零，后面每次提前返回都会以为
            "体已经读过了" —— 于是**第二条 403 又开始错位**。
            """
            self._body_read = False

        def _drain_body(self) -> None:
            """把没被读走的请求体读掉；读不了就**要求服务器关掉这条连接**。

            为什么必须有这一步（2026-10-06 实测复现）
            -----------------------------------------
            `protocol_version = "HTTP/1.1"` ⇒ 连接默认 keep-alive，而
            `BaseHTTPRequestHandler` 在 `do_POST` 返回后会**继续在同一条连接上
            读下一个请求**。403 / 404 / 413 这些提前返回的分支从没碰过请求体，
            于是留在接收缓冲里的 JSON 字节会被当成**下一条请求的请求行**：

            实测（同一条连接上先发一条无密钥的 POST，再发一条带正确密钥的）：
            第一条得 403，第二条拿到的是 `400 <!DOCTYPE HTML>`（基类那个通用错误页），
            **那条正确的告警被丢掉了**。对告警管道来说这是"上报全对、消息不到人"，
            而且客户端看到的是一次莫名其妙的 400（HTTP 请求错位 / desync）。
            它同时会在 Windows 上引发连接重置（`WinError 10053`），
            让依赖它的测试随机变红 —— 假红的测试会让人对真红的测试脱敏。
            """
            raw = self.headers.get("Content-Length")
            if raw is None:
                # 有 Transfer-Encoding 而没 Content-Length ⇒ chunked。长度未知、
                # 读不干净 ⇒ 唯一安全的处置是标记"这条连接不能再复用"。
                if self.headers.get("Transfer-Encoding"):
                    self.close_connection = True
                return
            try:
                length = int(raw.strip())
            except ValueError:
                self.close_connection = True
                return
            if length <= 0:
                return
            if length > MAX_BODY_BYTES:
                # 刻意**不读**：读了就等于允许对方用任意大的体把内存吃掉。
                # 代价是关连接时会产生一次 RST，但对"超大体"这是标准处置。
                self.close_connection = True
                return
            try:
                self.rfile.read(length)
            except OSError as e:
                # 读超时 / 半途断开（超时也是 OSError 的子类）。体没读完 ⇒
                # 这条连接的字节流已经对不齐了，**不能再复用**。
                logging.warning("请求体没读完（%s），本条连接不再复用", e)
                self.close_connection = True
                return
            self._body_read = True

        def _send(self, code: int, body: dict):
            if not self._body_read:
                # 提前返回（403 / 404 / 413 / 400）也要先把体处理干净 ——
                # 放在这个唯一出口上，以后新增分支不会漏掉（见 `_drain_body`）。
                self._drain_body()
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except OSError as e:
                # 对端已经走了（扫描器、超时后主动断开的客户端）。这不是我们的
                # 故障，但**必须吞掉**：让它冒出去会变成 socketserver 打印的
                # traceback，把真正需要看的日志淹掉。
                logging.info("响应没有发出去（%s），对端可能已断开", e)
                self.close_connection = True

        def do_GET(self):  # noqa: N802  基类命名
            self._begin()
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
            self._begin()
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

            # ⚠️ 先声明"体已经归我处理了"再去读：`rfile.read` 半途失败时
            # `_send` → `_drain_body` 会**再读一次**，那要么阻塞到超时、
            # 要么把下一条请求的字节吃掉。半途失败就直接关连接。
            self._body_read = True
            try:
                raw_body = self.rfile.read(length)
            except Exception as e:  # noqa: BLE001
                self.close_connection = True
                return self._send(400, {"error": f"unreadable body: {e}"})
            try:
                payload = json.loads(raw_body.decode("utf-8"))
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


def build_notify_payload(receiver: str, *, summary: str,
                         severity: str = "critical",
                         alertname: str = "RollbackExecuted") -> dict:
    """构造一条**非 Prometheus 来源**的事件（当前唯一用途：回滚已执行）。

    为什么复用 Alertmanager 的 payload 形状、而不另发明一种：
    渲染（`render_text`）与通道转换（`build_body`）都按这个形状写，
    换一种输入形状就得为"通知"再写一条渲染路径 ——
    而同一件事在两处渲染成不同样子，是迟早会发生的漂移。
    """
    return {
        "version": "4",
        "status": "firing",
        "receiver": receiver,
        "commonLabels": {"alertname": alertname},
        "alerts": [{
            "status": "firing",
            "labels": {
                "alertname": alertname,
                "category": "change",
                "severity": severity,
                "instance": "rollback-script",
            },
            "annotations": {
                "summary": summary,
                "runbook": "deploy/RUNBOOK.md",
            },
            "startsAt": "1970-01-01T00:00:00Z",
            "fingerprint": "rollback",
        }],
    }


def notify(dispatcher: Dispatcher, receiver: str, *, summary: str,
           severity: str = "critical",
           alertname: str = "RollbackExecuted") -> int:
    """发一条自定义通知；返回 **0 当且仅当真的送达了某个通道**。

    返回值刻意不放宽：`deploy/rollback.sh` 靠它决定打印"已发出"还是"没能发出"。
    若把"通道未配置"也算成功，脚本会在**什么都没发出去**时告诉运维"已通知" ——
    这正是这条告警链路一直在犯的那类错（未配置只记日志、接口照样 200）。
    """
    payload = build_notify_payload(receiver, summary=summary,
                                   severity=severity, alertname=alertname)
    sent, skipped, failed = dispatcher.deliver(receiver, payload)

    print("=" * 68)
    print("自定义通知")
    print("=" * 68)
    print(f"receiver : {receiver}")
    print("-" * 68)
    print(render_text(payload))
    print("-" * 68)
    for s in sent:
        print(f"  [已送达] {s}")
    for s in skipped:
        print(f"  [未发送] {s}")
    for s in failed:
        print(f"  [失败]   {s}")

    if not sent:
        print("结论：**没有任何通道真正发出** —— 上面这条消息没有人会看到。")
        return 1
    print("结论：已送达（机器人里是否真出现仍需人工确认）。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Alertmanager 告警通知网关")
    ap.add_argument("--selftest", action="store_true",
                    help="构造一条假告警走完整链路后退出（不监听端口）")
    ap.add_argument("--port", type=int, default=int(_env("ALERT_SINK_PORT", "8080")),
                    help="监听端口（默认 8080，亦可用 ALERT_SINK_PORT）")
    ap.add_argument("--notify", metavar="RECEIVER",
                    help="发一条自定义通知（如『回滚已执行』）后退出，不监听端口")
    ap.add_argument("--summary", default="",
                    help="配合 --notify：这条通知要说什么（人读的一句话）")
    ap.add_argument("--severity", default="critical",
                    help="配合 --notify：severity 标签（默认 critical）")
    ap.add_argument("--alertname", default="RollbackExecuted",
                    help="配合 --notify：告警名（默认 RollbackExecuted）")
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

    # 路由与凭据错位：投递侧会兜底，但这件事必须在启动日志里显形（见 diagnose_routing）。
    mismatch = diagnose_routing(routes, hooks)
    if mismatch:
        logging.warning(mismatch)

    # 启动回显：与项目其它服务同口径 —— 只回显布尔/条数，不回显凭据本身。
    logging.info("告警网关配置：routes=%s channels=%s dry_run=%s",
                 routes or "{}", {k: bool(v) for k, v in hooks.items()}, dry)

    if args.selftest:
        return selftest(dispatcher)

    # --notify 是**本地调用**（`deploy/rollback.sh` 从容器内调自己），
    # 因此刻意**不**参与 SHARED_SECRET 校验 —— 那个校验管的是
    # "谁能从网络另一头往这里投递"，与本地发一条通知无关。
    if args.notify:
        return notify(dispatcher, args.notify,
                      summary=args.summary or "（未填写说明）",
                      severity=args.severity, alertname=args.alertname)

    secret = _env("ALERT_SINK_SHARED_SECRET")
    if not secret:
        # 刻意**不**写"建议配上它"：本服务的调用方（alertmanager.yml 的四个
        # receiver）没有 `http_config`，根本不会送 `X-Alert-Sink-Secret` 头，
        # 于是"配上"的直接后果是把**全部告警**打成 403 —— 一个"看起来更安全"
        # 的加固动作，实际效果是让所有人再也收不到告警。
        # 要用必须两处一起改（见 .env.example / PRODUCTION_CONFIG.md）。
        logging.warning(
            "未设置 ALERT_SINK_SHARED_SECRET：任何能访问该端口的人都可以往里塞告警。"
            "⚠️ 这是**当前推荐**的配置，别顺手填上它 —— 非空时本服务要求 "
            "X-Alert-Sink-Secret 请求头，而 alertmanager.yml 的四个 receiver "
            "不会送这个头 ⇒ 全部告警回 403。")
    else:
        logging.warning(
            "已启用 ALERT_SINK_SHARED_SECRET 校验：确认四个 receiver 都配了 "
            "http_config.http_headers，否则它们的告警会被本服务回 403 丢掉。")

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
