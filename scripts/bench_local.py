#!/usr/bin/env python
"""本地微基准：量**不依赖外部服务**的热路径开销。

它测什么、不测什么
----------------
测：请求进入服务后、调用模型之前的那一段 —— 限流判定、配额预占与结算、
指标埋点、检索、上下文拼装。这些都在**每个请求**上执行，且是纯本机计算，
所以数字稳定、可复现、不需要密钥。

**不测**：端到端吞吐与延迟。那取决于模型供应商的响应速度与并发配额，
本机测不出来（也测不准）。端到端必须用 `scripts/load_test.py` 打真实实例，
并把结果留档到 `deploy/CAPACITY.md`。

为什么这个区分重要
----------------
把"微基准"当成"容量基线"是最容易犯的错：本机热路径 5 万 QPS，
就以为服务能扛 5 万 QPS —— 而真实瓶颈在**上游模型的并发与延迟**上，
差三到四个数量级。反过来也一样：热路径若被判为瓶颈（比如限流器每请求
O(活跃键数)），那才是真问题，且与模型无关。

它能回答的具体问题
----------------
  - 限流器是不是瓶颈？（滑动窗口是否做到了 O(1)/键）
  - 指标直方图的内存是否与请求量无关？（固定桶的意义就在这里）
  - 每个请求的"框架内"开销占总延迟的比例是多少？

用法
----
    python scripts/bench_local.py                 # 默认规模
    python scripts/bench_local.py --n 50000       # 调大样本
    python scripts/bench_local.py --json          # 机器可读（便于写进留档）
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.chdir(ROOT)

os.environ.setdefault("DB_URL", "")


def _timed(fn, n: int) -> dict:
    """跑 n 次，返回总计/单次耗时与吞吐。"""
    t0 = time.perf_counter()
    fn(n)
    dt = time.perf_counter() - t0
    return {
        "n": n,
        "total_s": dt,
        "us_per_op": (dt / n) * 1e6 if n else 0.0,
        "ops_per_s": (n / dt) if dt > 0 else float("inf"),
    }


def bench_ratelimit(n: int) -> dict:
    """限流判定（加权滑动窗口 + 内存后端）。

    关注点是**每键 O(1)**：若实现里出现"每次请求扫全表"，高基数 IP 会让
    吞吐随客户端数量下降 —— 那才是真瓶颈。这里用**不同 IP**逼近最坏情况
    （每个请求一个新键，逼出过期清理的代价）。
    """
    from app.ratelimit import RateLimiter

    rl = RateLimiter(per_minute=10 ** 9)      # 放开限值，只量开销
    ip = iter(range(10 ** 9))

    def run(k):
        for _ in range(k):
            rl.check(f"10.0.0.{next(ip) % 255}", "sess-fixed")

    return _timed(run, n)


def bench_quota(n: int) -> dict:
    """配额预占 + 结算（两阶段协议，内存后端）。

    两个动作都要量：`claim` 在请求开头，`settle` 在 finally ——
    它们的开销都落在请求路径上，只量一个会低估。
    """
    from app.quota import QuotaManager

    q = QuotaManager(daily_limit=10 ** 12, request_limit=500)
    tenant = "bench"

    def run(k):
        for _ in range(k):
            st = q.claim(tenant)
            q.settle(tenant, st.reserved, 42)

    return _timed(run, n)


def bench_metrics(n: int) -> dict:
    """指标埋点：计数器 + 直方图观测。

    直方图改固定桶之后，`observe` 是"定位一格并 +1"，与观测次数无关 ——
    这条基准应当表现为稳定的常数时间；若它随 n 变慢，说明有东西在累积。
    """
    from app import metrics

    def run(k):
        for i in range(k):
            metrics.inc("bench_counter_total", kind="chat")
            metrics.observe("bench_duration_seconds", (i % 300) / 100.0,
                            path="/v1/chat")

    out = _timed(run, n)
    # ⚠️ 标签必须与 `observe` 时**完全一致**：指标是按键（名字+标签）分片的，
    # 少传一个标签查的是另一个键，会读到 0 而不是"没数据"的报错。
    out["histogram_slots"] = metrics.histogram_slots(
        "bench_duration_seconds", path="/v1/chat")
    return out


def bench_retrieval(n: int) -> dict:
    """检索（BM25）+ 上下文拼装。

    这是"每次 /v1/chat 都真的会跑"的一段（只要有语料）。它的单次耗时
    直接叠加到首 token 延迟上，所以是热路径里最值得盯的一项。
    """
    from src.rag import retrieval

    chunks = retrieval.load_chunks("data")
    if not chunks:
        return {"skipped": "语料为空（data/ 加载到 0 个片段）", "n": 0}
    r = retrieval.build_retriever(chunks)
    q = "限流的判定值是怎么计算的"

    def run(k):
        for _ in range(k):
            hits = r.search(q, top_k=3)
            "".join(retrieval.context_block(c) for c, _ in hits)
            retrieval.build_citations(hits)

    out = _timed(run, n)
    out["corpus_chunks"] = len(chunks)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=20000, help="每个基准的迭代次数")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = ap.parse_args()

    from app import metrics

    results = {
        "host": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu": platform.processor() or "unknown",
            "cores": os.cpu_count(),
        },
        "n": args.n,
        "bench": {},
    }
    # 每个基准前重置指标，避免上一个基准的标签影响下一个的读数。
    # `scale` 用于给慢基准降样本量：检索单次 0.7ms、比其余项贵两个数量级，
    # 用它跑满样本会让整轮基准耗时被它主导（而它在统计上早已收敛）。
    for name, fn, scale in (("ratelimit", bench_ratelimit, 1.0),
                            ("quota", bench_quota, 1.0),
                            ("metrics", bench_metrics, 1.0),
                            ("retrieval", bench_retrieval, 0.05)):
        metrics.reset()
        nn = max(200, int(args.n * scale))
        try:
            results["bench"][name] = fn(nn)
        except Exception as e:  # noqa: BLE001
            results["bench"][name] = {"error": f"{type(e).__name__}: {e}"}

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    print(f"本机：{results['host']['platform']}｜Python {results['host']['python']}"
          f"｜{results['host']['cores']} 核")
    print(f"样本：每项 {args.n} 次\n")
    print(f"{'基准':<12}{'单次(μs)':>12}{'吞吐(次/秒)':>16}   备注")
    for name, r in results["bench"].items():
        if "error" in r:
            print(f"{name:<12}{'—':>12}{'—':>16}   失败：{r['error']}")
            continue
        if r.get("skipped"):
            print(f"{name:<12}{'—':>12}{'—':>16}   跳过：{r['skipped']}")
            continue
        note = ""
        if name == "metrics":
            note = f"直方图格数={r['histogram_slots']}（应恒定）"
        if name == "retrieval":
            note = f"语料 {r['corpus_chunks']} 片段"
        print(f"{name:<12}{r['us_per_op']:>12.2f}{r['ops_per_s']:>16,.0f}   {note}")

    print("\n注意：这是**本机热路径微基准**，不是容量基线。")
    print("端到端吞吐/延迟取决于上游模型的并发与响应速度，必须用")
    print("`python scripts/load_test.py` 打真实实例并把结果留档到 deploy/CAPACITY.md。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
