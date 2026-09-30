#!/usr/bin/env python
"""灰度判据：用 Prometheus 数据判定"这一版灰度能不能放量"（能不能自动回滚）。

为什么需要它（现状的洞）
----------------------
`deploy/app/nginx.conf` 把流量按 9:1 分给 stable / canary，`canary.env` 支持改模型
与开关。**但"灰度好不好"此前完全靠人盯** —— 打开 Prometheus 面板，看两眼，觉得
没问题就把权重调大。这在三种情况下必然出事：

  1. 灰度只承载 10% 流量 ⇒ 它的**绝对**错误数天然很小。"看起来只有 3 个错误"
     经常是"错误率是稳定版的 5 倍"；
  2. 灰度是**低频**路径：一小时可能只有几十个请求，肉眼看不出趋势；
  3. 最要命的一种 —— **灰度根本没流量**（nginx 配置写错、灰度容器起不来），
     面板上一片干净，于是被判成"灰度没问题"，然后把一个**从未被验证过**的版本
     放量到 100%。

所以这个脚本做三件事：**统一用 rate 比较（消除流量比例的影响）→ 样本不足时
明确报"无法判定"而不是"通过" → 用退出码驱动回滚。**

退出码（刻意与 `pg_backup.py` / `qdrant_backup.py` 保持同一套约定）
----------------------------------------------------------------
    0 = PASS   可以放量（判据全部绿灯）
    1 = FAIL   **应回滚**（任一判据红灯）
    2 = 无法判定（Prometheus 不可达 / 窗口内灰度样本不足）

    ⚠️ FAIL 与"无法判定"必须分开：两者都"不是通过"，但处置完全不同 ——
    前者要立刻回滚，后者要先解决"看不到数据"的问题（去看 nginx 权重、
    灰度容器日志、抓取目标是否 UP）。混在一起会让人以为"数据没采到"就是"失败"，
    于是直接回滚一个其实没问题的版本。

判据为什么是这几条、以及每条的局限
--------------------------------
    ① 5xx 比例   —— 相对劣化（含噪声地板）**或** 绝对超标
    ② P95 延迟   —— 同上（灰度最典型的问题是"变慢"而不是"报错"）
    ③ 流式错误率 —— `chat_stream_errors_total` 在正常时恒为 0（本项目主接口是 SSE，
                   流中途挂掉不会体现在 HTTP 状态码上，只看 5xx 会漏掉）
    ④ 每请求 token —— 成本与"上下文膨胀"的代理指标：新版 prompt 变长、
                   检索多召回了几块，都会体现在这里，而它在 5xx/延迟上完全看不出来

    关于 ① 的"两个条件都看"：只看相对会被坏基线带偏（stable 自己已经 5% 时，
    canary 5.2% 会被误判成"灰度引入的"），只看绝对会放过"低基数下的成倍劣化"。
    两个都看、任一成立即 FAIL —— 代价是**全局故障时会一并报 FAIL**，
    所以输出里同时打印 stable 的值，用于区分"是灰度的问题"还是"整体在坏"。

    关于 ④ 的局限：它用的是"全部请求"作分母的粗粒度代理，不是按接口细分。
    它能发现"整体成本/上下文体积异常"，但**不能**告诉你具体是哪个接口。
    要细查请按 path 标签下钻。

用法
----
    python scripts/canary_check.py                       # 判定并打印明细
    python scripts/canary_check.py --json                # 机器可读（给 CI/发布脚本）
    python scripts/canary_check.py --window 30m --min-requests 100
    python scripts/canary_check.py --max-p95 1.0 --prometheus-url http://localhost:9090

在发布流程里的位置：**改完 tag → 观察一个窗口 → 跑这个脚本 → 退出码非 0 就回滚**
（完整流程见 `deploy/CANARY.md`）。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

RC_PASS, RC_FAIL, RC_UNDECIDABLE = 0, 1, 2

# 视为"本机"的主机名：对这些地址必须绕过代理，理由见 Prom.__init__。
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

# 只匹配业务两个抓取 job（chatagent-stable / chatagent-canary）。
# 刻意用 `.` 而不是 `.`* 通配到 blackbox job（它叫 blackbox-chatagent，开头就不是
# chatagent-，因此天然被排除）。
_SEL = 'job=~"chatagent-.+"'

_WINDOW_RE = re.compile(r"^\d+[smhd]$")

# Prometheus 会把"没有数据"编码成这些字符串，而不是报错。
_NON_FINITE = {"NaN", "+Inf", "-Inf", "Inf"}


class PromUnreachable(RuntimeError):
    """Prometheus 不可达 / 返回了错误 —— 与"判据不通过"是两回事。"""


class Prom:
    """Prometheus HTTP API 的最小客户端（只用到 instant query）。"""

    def __init__(self, base: str, timeout: int = 15):
        self.base = (base or "").rstrip("/")
        self.timeout = timeout

        # ⚠️ 本机地址必须绕过代理：Prometheus 通常就跑在 localhost:9090，而
        # http_proxy 在开发机/企业网里很常见。urllib 只在配了 NO_PROXY 时才豁免
        # 本机，没配时的表现极具误导性 —— 请求被送进代理、代理回连本机失败并
        # 回 502，于是报错看起来像"Prometheus 返回了错误"，实际根本没连上它。
        host = urllib.parse.urlsplit(self.base).hostname or ""
        if host.lower() in _LOCAL_HOSTS:
            self._opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}))
        else:
            self._opener = urllib.request.build_opener()

    def _get(self, path: str, params: dict) -> dict:
        if not self.base:
            raise PromUnreachable("未配置 Prometheus 地址（--prometheus-url 或 PROMETHEUS_URL）")
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        try:
            with self._opener.open(url, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise PromUnreachable(f"HTTP {e.code} {path}") from None
        except urllib.error.URLError as e:
            raise PromUnreachable(f"无法连接 {self.base}：{e.reason}") from None
        except Exception as e:  # noqa: BLE001
            raise PromUnreachable(f"{type(e).__name__}: {e}") from None
        if payload.get("status") != "success":
            raise PromUnreachable(f"查询失败：{payload.get('error') or payload}")
        return payload.get("data") or {}

    @staticmethod
    def _to_float(raw) -> float | None:
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None
        # NaN / Inf 统一视为"没有数据"，否则后续比较会全部为 False，
        # 表现成"判据通过"——这正是最危险的假绿形态。
        return v if math.isfinite(v) else None

    def by_release(self, expr: str) -> dict[str, float | None]:
        """执行查询并返回 {release 标签值: 数值}。缺失的 release 不会出现在结果里。"""
        data = self._get("/api/v1/query", {"query": expr})
        out: dict[str, float | None] = {}
        for item in data.get("result") or []:
            rel = (item.get("metric") or {}).get("release")
            if rel is None:
                continue
            value = item.get("value") or []
            out[rel] = self._to_float(value[1] if len(value) > 1 else None)
        return out


# ---------------------------------------------------------------------------
# 判据
# ---------------------------------------------------------------------------
class Judgement:
    def __init__(self, name: str, unit: str):
        self.name = name
        self.unit = unit
        self.status = "PASS"          # PASS | FAIL | UNDECIDABLE
        self.canary: float | None = None
        self.stable: float | None = None
        self.reason = ""

    def fmt(self, v: float | None) -> str:
        if v is None:
            return "无数据"
        return f"{v:.4f}{self.unit}" if self.unit else f"{v:.4f}"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "canary": self.canary,
            "stable": self.stable,
            "reason": self.reason,
        }


def _judge_regression(name: str, unit: str, values: dict,
                      rel_tol: float, floor: float,
                      abs_max: float | None) -> Judgement:
    """通用判据：相对劣化（含噪声地板）**或** 绝对超标 ⇒ FAIL。

    floor 是**噪声地板**：stable=0.0001、canary=0.0003 这种"三倍"在低基数下毫无
    意义，只按倍率判会让门禁频繁误报，而误报的终点是"没人再看这个门禁"。
    所以相对判据写成 `canary > stable * rel_tol + floor`。
    """
    j = Judgement(name, unit)
    j.canary = values.get("canary")
    j.stable = values.get("stable")

    if j.canary is None:
        j.status = "UNDECIDABLE"
        j.reason = "窗口内灰度侧没有任何数据 —— 无法判定（不要当成通过）"
        return j

    reasons: list[str] = []
    if j.stable is not None:
        if j.canary > j.stable * rel_tol + floor:
            reasons.append(
                f"相对劣化：{j.fmt(j.canary)} vs 稳定版 {j.fmt(j.stable)}"
                f"（容差 {rel_tol}x + 地板 {floor}{unit}）"
            )
    else:
        reasons.append("稳定版侧无数据，只能做绝对判定")

    if abs_max is not None and j.canary > abs_max:
        reasons.append(f"绝对超标：{j.fmt(j.canary)} > 上限 {abs_max}{unit}")

    if reasons:
        j.status = "FAIL"
        # 同时打印稳定版的值，用于区分"灰度引入的"还是"整体在坏"
        j.reason = "；".join(reasons)
        if j.stable is not None and j.stable > (abs_max or float("inf")):
            j.reason += "（注意：稳定版同样超标 ⇒ 更像整体故障，而非本版引入）"
    else:
        j.reason = "在容差内"
    return j


def evaluate(prom: Prom, window: str, opts) -> tuple[int, list[Judgement], dict]:
    """执行全部判据，返回 (退出码, 判据列表, 汇总数据)。"""
    jobs: list[Judgement] = []

    # ---- 样本量闸门：先判"看不看得见"，再判"好不好" ----------------------
    # 这一条排在所有判据之前，因为"灰度没流量"带来的假绿比任何判据都危险：
    # 它会让你把一个从未被验证过的版本放量到 100%。
    counts = prom.by_release(
        f"sum by (release) (increase(http_requests_total{{{_SEL}}}[{window}]))")
    canary_n = counts.get("canary")
    stable_n = counts.get("stable")

    sample = Judgement("灰度样本量", "")
    sample.canary = canary_n
    sample.stable = stable_n
    if canary_n is None:
        sample.status = "UNDECIDABLE"
        sample.reason = f"窗口 {window} 内灰度请求数为 0 或取不到 —— 无法判定"
    elif canary_n < opts.min_requests:
        sample.status = "UNDECIDABLE"
        sample.reason = (f"窗口 {window} 内灰度仅 {canary_n:.0f} 个请求 "
                         f"（门槛 {opts.min_requests}）—— 样本太少，任何结论都不可靠")
    else:
        sample.status = "PASS"
        sample.reason = f"窗口 {window} 内灰度 {canary_n:.0f} 个请求"
    jobs.append(sample)

    if sample.status != "PASS":
        # 样本量不足时**不继续**跑其它判据：那些数字同样不可信，而且容易
        # 产生"某一条刚好是绿的"这种似是而非的结论。
        return RC_UNDECIDABLE, jobs, {"window": window, "requests": counts}

    # ---- ① 5xx 比例 --------------------------------------------------------
    # 用 http_requests_total{status=~"5.."} 而不是 http_errors_total ——
    # 后者不含 503（网关/中间件层返回的），会系统性低估错误率（见 deploy/monitoring/SLO.md）。
    err = prom.by_release(
        f'sum by (release) (rate(http_requests_total{{{_SEL},status=~"5.."}}[{window}]))'
        f" / "
        f"sum by (release) (rate(http_requests_total{{{_SEL}}}[{window}]))")
    jobs.append(_judge_regression("5xx 比例", "", err,
                                  opts.max_error_ratio, opts.error_floor,
                                  opts.max_error_abs))

    # ---- ② P95 延迟 --------------------------------------------------------
    # histogram_quantile 直接对 `_bucket` 求和，不手写 le 值 ——
    # 手写时必须写成 le="1.0" 而非 le="1"（桶边界是字符串全匹配，写错则
    # 永不匹配且没有任何报错，见 SLO.md 里记的那个坑）。
    p95 = prom.by_release(
        "histogram_quantile(0.95, sum by (release, le) "
        f"(rate(http_request_duration_seconds_bucket{{{_SEL}}}[{window}])))")
    jobs.append(_judge_regression("P95 延迟", "s", p95,
                                  opts.max_p95_ratio, opts.p95_floor,
                                  opts.max_p95))

    # ---- ③ 流式错误率 ------------------------------------------------------
    # 正常应为 0。SSE 主接口的失败**不体现在 HTTP 状态码上**（响应头早就发出去了），
    # 所以只看 5xx 会完全漏掉"新版本把流写坏了"。
    stream = prom.by_release(
        f"sum by (release) (rate(chat_stream_errors_total{{{_SEL}}}[{window}]))")
    jobs.append(_judge_regression("流式错误率", "/s", stream,
                                  opts.max_stream_ratio, opts.stream_floor,
                                  opts.max_stream_abs))

    # ---- ④ 每请求 token（成本 / 上下文膨胀的代理指标）----------------------
    tokens = prom.by_release(
        f"sum by (release) (rate(llm_tokens_total{{{_SEL}}}[{window}]))"
        f" / clamp_min("
        f"sum by (release) (rate(http_requests_total{{{_SEL}}}[{window}])), 0.001)")
    jobs.append(_judge_regression("每请求 token", "", tokens,
                                  opts.max_token_ratio, opts.token_floor, None))

    # 结论聚合：三种状态都要认。
    # ⚠️ 只判断 FAIL 是个真实的坑：某条判据因为**没有数据**而 UNDECIDABLE 时，
    # 会被当成"不是 FAIL"从而整体判成 PASS —— 指标明明在报"我没数据"，
    # 脚本却给了绿灯，然后据此放量。所以 PASS 必须是"全部 PASS"才成立。
    if any(j.status == "FAIL" for j in jobs):
        verdict = RC_FAIL
    elif any(j.status == "UNDECIDABLE" for j in jobs):
        verdict = RC_UNDECIDABLE
    else:
        verdict = RC_PASS
    return verdict, jobs, {"window": window, "requests": counts}


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def _print_report(jobs: list[Judgement], window: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps(
            {"window": window, "verdict": [j.to_dict() for j in jobs]},
            ensure_ascii=False, indent=2))
        return

    print(f"灰度判据（窗口 {window}）")
    print("-" * 74)
    for j in jobs:
        mark = {"PASS": "✅", "FAIL": "❌", "UNDECIDABLE": "⚠️ "}[j.status]
        line = f"{mark} {j.name:<12} canary={j.fmt(j.canary):>14}  stable={j.fmt(j.stable):>14}"
        print(line)
        print(f"     {j.reason}")
    print("-" * 74)

    if any(j.status == "FAIL" for j in jobs):
        print("结论：FAIL —— 应回滚灰度（见 deploy/CANARY.md 的回滚步骤）")
    elif any(j.status == "UNDECIDABLE" for j in jobs):
        print("结论：无法判定 —— 先解决「看不到数据」，不要当成通过，也不要回滚")
    else:
        print("结论：PASS —— 可以逐步放量（每次调整后重新观察一个窗口）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="用 Prometheus 数据判定灰度是否可以放量（退出码驱动回滚）")
    ap.add_argument("--prometheus-url", default=os.environ.get("PROMETHEUS_URL", ""),
                    help="Prometheus 地址（默认取 PROMETHEUS_URL；本机填 http://localhost:9090）")
    ap.add_argument("--window", default="10m",
                    help="判定窗口，如 10m/30m（必须匹配 ^\\d+[smhd]$）")
    ap.add_argument("--min-requests", type=int, default=30,
                    help="窗口内灰度最少请求数；低于它一律报「无法判定」")
    ap.add_argument("--max-error-ratio", type=float, default=1.5,
                    help="灰度 5xx 比例相对稳定版的容差倍数")
    ap.add_argument("--error-floor", type=float, default=0.002,
                    help="5xx 比例的噪声地板（绝对差值，低于它不判失败）")
    ap.add_argument("--max-error-abs", type=float, default=0.02,
                    help="灰度 5xx 比例的绝对上限（超过直接失败）")
    ap.add_argument("--max-p95-ratio", type=float, default=1.3,
                    help="P95 延迟相对稳定版的容差倍数")
    ap.add_argument("--p95-floor", type=float, default=0.05,
                    help="P95 的噪声地板（秒）")
    ap.add_argument("--max-p95", type=float, default=1.0,
                    help="P95 绝对上限（秒）")
    ap.add_argument("--max-stream-ratio", type=float, default=3.0,
                    help="流式错误率相对稳定版的容差倍数")
    ap.add_argument("--stream-floor", type=float, default=0.02,
                    help="流式错误率的噪声地板（次/秒）")
    ap.add_argument("--max-stream-abs", type=float, default=0.2,
                    help="流式错误率绝对上限（次/秒）")
    ap.add_argument("--max-token-ratio", type=float, default=1.5,
                    help="每请求 token 相对稳定版的容差倍数")
    ap.add_argument("--token-floor", type=float, default=0.0,
                    help="每请求 token 的噪声地板")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（给发布脚本用）")
    args = ap.parse_args(argv)

    if not _WINDOW_RE.match(args.window):
        print(f"窗口格式不对：{args.window!r}（应为 10m / 30m / 1h / 2d 这类）",
              file=sys.stderr)
        return RC_UNDECIDABLE

    prom = Prom(args.prometheus_url)
    try:
        rc, jobs, _meta = evaluate(prom, args.window, args)
    except PromUnreachable as e:
        # 与"判据不通过"严格区分：这是"看不到"，不是"不好"
        print(f"无法判定：Prometheus 不可用 —— {e}", file=sys.stderr)
        print("请先确认 Prometheus 可达、且两个抓取 job 都为 UP。", file=sys.stderr)
        return RC_UNDECIDABLE

    _print_report(jobs, args.window, args.json)
    return rc


if __name__ == "__main__":
    sys.exit(main())
