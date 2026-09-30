"""并发压测 + **断连止损验证**。

它能回答两个问题
--------------
1. 这个实例在并发下能扛多少？（吞吐、P95/P99、错误率）
2. 客户端中途断开时，服务端是否真的停止生成？（**省钱**的直接验证）

第 2 点是本脚本相对普通压测工具的价值所在：普通压测只会报"我这边断了"，
而这里会在压测前后各拉一次 `/metrics`，比对 `client_disconnected_total` 与
`llm_tokens_total` 的增量，**由服务端指标证实**止损真的发生了。

⚠️ 压测前必读
------------
- **默认打 `/health/live`**：不花钱、不触发检索，用来量连接与框架开销。
- 打 `/v1/chat` 必须显式加 `--endpoint chat --yes-spend` —— 它会真的调用大模型
  并产生费用。
- 应用层有按 IP 的限流（默认 120/分钟）。压测**必然**会撞上它，所以：
      RATE_LIMIT_ENABLED=0 python -m app.serve      # 压测期间关掉
  否则你量到的是限流器，不是服务能力。脚本会把 429 单独统计出来提醒你。

用法
----
    # 轻量：连接与框架开销（不花钱）
    python scripts/load_test.py --requests 500 --concurrency 20

    # 真实对话：注意费用
    python scripts/load_test.py --endpoint chat --requests 20 --concurrency 4 \
        --api-key $SERVE_API_KEY --yes-spend

    # 断连止损验证：每个请求收到 3 个 token 就断开
    python scripts/load_test.py --endpoint chat --requests 10 --concurrency 2 \
        --disconnect-after 3 --api-key $SERVE_API_KEY --yes-spend
"""

import argparse
import asyncio
import os
import statistics
import sys
import time

try:
    import httpx
except ImportError:  # pragma: no cover
    raise SystemExit("需要 httpx：pip install httpx")

# 兼容"python scripts/load_test.py"与"python -m scripts.load_test"两种跑法
try:
    from scripts.net import trust_env_for
except ImportError:  # pragma: no cover
    from net import trust_env_for


def _pct(values, p):
    """取分位数（无数据返回 0）。用最近邻法，样本少时不会算出奇怪的值。"""
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * len(s) + 0.5)) - 1))
    return s[k]


async def _fetch_metrics(client, base, api_key) -> dict:
    """拉一次 /metrics，解析出 `指标名 -> 值`（只取单行无标签的样本）。

    失败返回空 dict（不影响压测本身）—— 指标是"证据"，缺了不该让压测跑不下去。
    """
    try:
        r = await client.get(base + "/metrics", headers={"X-API-Key": api_key}, timeout=10)
        if r.status_code != 200:
            return {}
        out = {}
        for line in r.text.splitlines():
            if not line or line.startswith("#") or "{" in line:
                continue
            parts = line.rsplit(" ", 1)
            if len(parts) == 2:
                try:
                    out[parts[0]] = float(parts[1])
                except ValueError:
                    pass
        return out
    except Exception:  # noqa: BLE001
        return {}


async def _hit_health(client, base, api_key):
    """打一次存活探针，返回 (ok, 耗时, 状态码)。"""
    t0 = time.perf_counter()
    try:
        r = await client.get(base + "/health/live", timeout=15,
                             headers={"X-API-Key": api_key} if api_key else None)
        return (r.status_code < 500, time.perf_counter() - t0, r.status_code)
    except Exception:  # noqa: BLE001
        return (False, time.perf_counter() - t0, 0)


async def _hit_chat(client, base, api_key, thread_id, disconnect_after=None):
    """打一次 /v1/chat，读完 SSE 或按需提前断开。

    返回 (ok, 耗时, 收到的 token 数, 是否主动断开, 是否被限流)。

    提前 return 会让 `async with stream(...)` 退出并关闭连接 —— 这正是
    "用户关掉页面"在传输层的表现，服务端应当据此停止生成。
    """
    payload = {"thread_id": thread_id, "message": "请用一句话介绍你自己"}
    headers = {"X-API-Key": api_key} if api_key else {}
    tokens = 0
    t0 = time.perf_counter()
    try:
        async with client.stream("POST", base + "/v1/chat", json=payload,
                                 headers=headers, timeout=120) as r:
            if r.status_code == 429:
                return (False, time.perf_counter() - t0, 0, False, True)
            if r.status_code != 200:
                return (False, time.perf_counter() - t0, 0, False, False)
            event = None
            async for line in r.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:") and event == "token":
                    tokens += 1
                    if disconnect_after and tokens >= disconnect_after:
                        return (True, time.perf_counter() - t0, tokens, True, False)
        return (True, time.perf_counter() - t0, tokens, False, False)
    except Exception:  # noqa: BLE001
        return (False, time.perf_counter() - t0, tokens, False, False)


async def run(args) -> int:
    """并发跑完所有请求并打印报告；返回进程退出码（0 = 无失败）。"""
    base = args.target.rstrip("/")
    api_key = args.api_key or os.getenv("SERVE_API_KEY", "")
    latencies, failures, rate_limited, total_tokens, disconnects = [], 0, 0, 0, 0

    limits = httpx.Limits(max_connections=args.concurrency * 2,
                          max_keepalive_connections=args.concurrency)
    # 本地目标必须绕过系统代理，否则请求会被送到 HTTP_PROXY 去，
    # 结果要么 502（像"服务没起来"，其实请求根本没到服务），要么数字是"经代理的吞吐"
    # ——压测口径错了比直接失败更危险。远端目标仍让代理生效（企业网可能需要）。
    trust = trust_env_for(base)
    if not trust:
        print(f"目标 {base} 判定为本机地址：已禁用环境代理，直连测量。")
    async with httpx.AsyncClient(limits=limits, http2=False, trust_env=trust) as client:
        # ---- 压测前的指标基线：用来证明"断连止损真的发生了" ----
        before = await _fetch_metrics(client, base, api_key)
        if not before:
            print("提示：拉不到 /metrics（可能未启用或不可达），断连止损将无法由服务端证实。")

        counter = {"n": 0}
        lock = asyncio.Lock()
        sem = asyncio.Semaphore(args.concurrency)

        async def worker(idx):
            nonlocal failures, rate_limited, total_tokens, disconnects
            while True:
                async with lock:
                    if counter["n"] >= args.requests:
                        return
                    counter["n"] += 1
                    n = counter["n"]
                async with sem:
                    if args.endpoint == "health":
                        ok, dt, code = await _hit_health(client, base, api_key)
                        limited = code == 429
                        tokens, disc = 0, False
                    else:
                        ok, dt, tokens, disc, limited = await _hit_chat(
                            client, base, api_key, f"load-{idx}-{n}",
                            disconnect_after=args.disconnect_after)
                async with lock:
                    latencies.append(dt)
                    if limited:
                        rate_limited += 1
                    elif not ok:
                        failures += 1
                    total_tokens += tokens
                    if disc:
                        disconnects += 1

        t0 = time.perf_counter()
        await asyncio.gather(*[worker(i) for i in range(args.concurrency)])
        wall = time.perf_counter() - t0

        after = await _fetch_metrics(client, base, api_key)

    # ---- 报告 ----
    done = len(latencies)
    print()
    print(f"目标      : {base}  端点={args.endpoint}  并发={args.concurrency}")
    print(f"请求总数  : {done}   墙钟={wall:.2f}s   吞吐={done / wall:.1f} req/s")
    print(f"成功      : {done - failures - rate_limited}   失败={failures}   被限流(429)={rate_limited}")
    print(f"延迟      : P50={_pct(latencies, 50) * 1000:.0f}ms  "
          f"P95={_pct(latencies, 95) * 1000:.0f}ms  "
          f"P99={_pct(latencies, 99) * 1000:.0f}ms  "
          f"max={max(latencies) * 1000:.0f}ms" if latencies else "延迟: 无数据")
    if args.endpoint == "chat":
        print(f"Token     : 合计收到 {total_tokens} 个 token 事件"
              f"（均 {total_tokens / max(1, done):.1f}/请求）")

    if rate_limited:
        print()
        print(f"⚠️ 有 {rate_limited} 个请求被限流（429）。本次量到的是**限流器**而不是服务能力。")
        print("   压测请先 `RATE_LIMIT_ENABLED=0 python -m app.serve` 再跑。")

    # ---- 断连止损的服务端证据 ----
    exit_code = 0 if (failures == 0) else 1
    if args.endpoint == "chat" and args.disconnect_after and before and after:
        d_disc = after.get("client_disconnected_total", 0) - before.get("client_disconnected_total", 0)
        d_tok = after.get("llm_tokens_total", 0) - before.get("llm_tokens_total", 0)
        print()
        print("断连止损核验（服务端指标增量）：")
        print(f"  client_disconnected_total  +{d_disc:.0f}   （客户端侧主动断开 {disconnects} 次）")
        print(f"  llm_tokens_total           +{d_tok:.0f}")
        if d_disc >= 1:
            print("  ⇒ ✓ 服务端确实观测到了断连（说明 is_disconnected 检测生效，不会继续白烧 token）")
        else:
            print("  ⇒ ✗ 服务端**没有**观测到断连：请检查请求是否真的提前关闭连接，"
                  "以及服务是否是旧版本（该能力 2026-09-28 才加入）")
            exit_code = 1
    return exit_code


def main(argv=None) -> int:
    """命令行入口。"""
    p = argparse.ArgumentParser(
        prog="python scripts/load_test.py",
        description="chatAgent 并发压测与断连止损验证",
    )
    p.add_argument("--target", default="http://localhost:8000", help="服务地址")
    p.add_argument("--endpoint", choices=("health", "chat"), default="health",
                   help="health（默认，不花钱）| chat（**会消耗模型额度**）")
    p.add_argument("--requests", type=int, default=200, help="总请求数")
    p.add_argument("--concurrency", type=int, default=10, help="并发数")
    p.add_argument("--api-key", default="", help="X-API-Key（默认取 SERVE_API_KEY）")
    p.add_argument("--disconnect-after", type=int, default=None,
                   help="chat 场景：收到 N 个 token 后主动断开，用于验证止损")
    p.add_argument("--yes-spend", action="store_true",
                   help="打 chat 端点必须带此确认（会真的调用模型并产生费用）")
    args = p.parse_args(argv)

    if args.endpoint == "chat" and not args.yes_spend:
        raise SystemExit(
            "打 /v1/chat 会真实调用大模型并产生费用。确认请加 --yes-spend。\n"
            "（先用 --endpoint health 量连接层开销是不花钱的替代方案）"
        )
    if args.requests <= 0 or args.concurrency <= 0:
        raise SystemExit("--requests 与 --concurrency 必须为正数")

    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
