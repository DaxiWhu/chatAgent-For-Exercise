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

CI 门禁守什么、不守什么（2026-10-15 新增）
----------------------------------------
`--check` 只断言**机器无关的不变量**，不比对绝对耗时：

  1. **规模不变量**：限流 / 配额 / 埋点都号称"每键 O(1)"。把样本翻 4 倍，
     单次耗时若跟着涨 ~4 倍，说明实现退化成了 O(n)——那才是真瓶颈
     （高基数 IP 下吞吐会随客户端数下降）。判据是"翻 4 倍规模后单次耗时 ≤ 2 倍"，
     且每个规模**量 3 次取最小**（噪声单向，只有 min 不会被它污染；见 `SCALING_REPEATS`）。
  2. **内存有界**：直方图格数必须恒为 `len(bucket_bounds()) + 1`。退回去存原始观测值
     ⇒ 内存随 QPS 线性增长 ⇒ 一个被刷的接口即可 OOM，而 `_count` 增长看起来完全正常。

**为什么不比对绝对耗时**：CI runner 与开发机的绝对微秒数差 2~3 倍很常见，
拿本机基线去判 CI 会天天红 —— 于是有人把门禁关掉，那比没有门禁更糟。
绝对性能对比是**显式动作**（`--baseline`，本地/发布前人工跑），
判据与纪律照搬 `scripts/eval_scorecard.py`：基线在写文件**之前**读、
基线缺失时显式报错而不是静默跳过、不自动对比（自动对比＝自愈门禁＝没有门禁）。

用法
----
    python scripts/bench_local.py                 # 默认规模，只打印
    python scripts/bench_local.py --n 50000       # 调大样本
    python scripts/bench_local.py --json          # 机器可读（便于写进留档）
    python scripts/bench_local.py --check         # CI 门禁：只断言不变量，失败退出 1
    python scripts/bench_local.py --out scripts/bench_baseline.json    # 留档基线
    python scripts/bench_local.py --baseline scripts/bench_baseline.json  # 与基线比
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

#: 默认基线留档路径（**同机器**对比用；换机器必须重新生成 —— 见模块 docstring）
DEFAULT_BASELINE = os.path.join("scripts", "bench_baseline.json")

#: 绝对对比的容差（倍数）。**留宽的代价比留窄小**：留窄会因正常波动误报，
#: 而误报的终点是有人把门禁关掉；留宽只漏掉小幅退化，而小幅退化本来就不该卡 CI。
DEFAULT_TOLERANCE = 2.5

#: 规模不变量的容差（倍数）。翻 4 倍样本量：O(1) 期望比值 ≈1，O(n) 会是 ≈4。
#: 取 2.0 —— 离 1 有 2 倍余量（够吸收计时噪声），离 4 也留了 2 倍（退化抓得到）。
SCALING_TOLERANCE = 2.0

#: 参与规模不变量检查的基准（检索刻意不在内：它**本来就是** O(语料)，见模块说明）
SCALING_BENCHES = ("ratelimit", "quota", "metrics")

#: 每个规模量几次、**取最小值**。
#:
#: 微基准里的噪声是**单向**的 —— 上下文切换、GC、同 runner 上别的 job 抢 CPU
#: 只会让某一次变慢，绝不会把它变快。所以 min 才是"真实代价"的稳健估计，
#: 单次测量与均值都不是（均值还把噪声算进去了）。
#:
#: 为什么必须这么做：CI runner 是共享的 2 核机器，一次几十毫秒的停顿落在
#: n 或 4n 任意一侧，比值就会被顶过 `SCALING_TOLERANCE` ⇒ **随机红**。
#: 本机 12 轮实测最差比值 1.17（上限 2.0），但那是在 24 核空闲机上 ——
#: CI 上的尾部要长得多。而随机红的终点是"有人把门禁关掉"，比没有门禁更糟。
SCALING_REPEATS = 3


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
    """检索 + 上下文拼装。

    这是"每次 /v1/chat 都真的会跑"的一段（只要有语料）。它的单次耗时
    直接叠加到首 token 延迟上，所以是热路径里最值得盯的一项。

    ⚠️ **必须锁检索口径**（见 `scripts/eval_backend.py`）：不锁的话，开发机 `.env`
    里只要有 `EMBEDDING_API_KEY`，这里量到的就是**外部 embedding 的网络往返**
    （几十毫秒级）而不是本机开销 —— 既与 `CAPACITY.md` 的基线不可比，也随网络抖动。
    与评测链路上那个"同一份代码、两套后端、两边都不报错"是同一个坑。
    """
    from scripts.eval_backend import local_eval
    from src.rag import retrieval

    chunks = retrieval.load_chunks("data")
    if not chunks:
        return {"skipped": "语料为空（data/ 加载到 0 个片段）", "n": 0}
    q = "限流的判定值是怎么计算的"
    with local_eval():
        r = retrieval.build_retriever(chunks)

        def run(k):
            for _ in range(k):
                hits = r.search(q, top_k=3)
                "".join(retrieval.context_block(c) for c, _ in hits)
                retrieval.build_citations(hits)

        out = _timed(run, n)
    out["corpus_chunks"] = len(chunks)
    return out


def _scaling_check(name: str, fn, n: int) -> dict:
    """这个基准的"每键 O(1)"是不是真的：同一样本量翻 4 倍，单次耗时该基本不变。

    每个规模量 `SCALING_REPEATS` 次**取最小值** —— 理由见该常量的说明：
    噪声只会让某一次变慢，min 是唯一不会把噪声算进判据的估计方式。
    """
    best: dict[int, dict] = {}
    for size in (n, n * 4):
        runs = [fn(size) for _ in range(SCALING_REPEATS)]
        best[size] = min(runs, key=lambda r: r.get("us_per_op", float("inf")))
    small, large = best[n], best[n * 4]
    us_small = small.get("us_per_op", 0.0)
    us_large = large.get("us_per_op", 0.0)
    ratio = (us_large / us_small) if us_small > 0 else 0.0
    return {
        "n_small": n,
        "n_large": n * 4,
        "us_small": us_small,
        "us_large": us_large,
        "ratio": ratio,
        "repeats": SCALING_REPEATS,
        "ok": ratio <= SCALING_TOLERANCE,
        "histogram_slots": small.get("histogram_slots"),
    }


def check_invariants(n: int) -> tuple[list[str], dict]:
    """跑一遍**机器无关**的不变量，返回 (问题清单, 规模数据)。问题清单为空 = 通过。

    这两类不变量是 CI 里唯一能稳的判据（绝对耗时跨机器不可比，见模块 docstring）：

    * **规模不变量** —— 号称 O(1)/键的东西，翻 4 倍样本后单次耗时不该跟着涨；
    * **内存有界** —— 直方图格数恒定（退回存原始观测值 ⇒ 内存随 QPS 线性增长）。

    ⚠️ 名字与 `(name, fn)` 表同名，避免"检查的是 A、报告的是 B"。
    """
    from app import metrics

    problems: list[str] = []
    scaling: dict = {}
    for name, fn in (("ratelimit", bench_ratelimit),
                     ("quota", bench_quota),
                     ("metrics", bench_metrics)):
        metrics.reset()
        try:
            r = _scaling_check(name, fn, n)
        except Exception as e:  # noqa: BLE001  基准跑不起来本身就是问题，要报出来
            problems.append(f"{name}: 基准跑不起来（{type(e).__name__}: {e}）")
            continue
        scaling[name] = r
        if not r["ok"]:
            problems.append(
                f"{name}: 样本从 {r['n_small']} 加到 {r['n_large']} 后单次耗时涨了 "
                f"{r['ratio']:.2f} 倍（{r['us_small']:.2f}μs → {r['us_large']:.2f}μs，"
                f"上限 {SCALING_TOLERANCE}） ⇒ 实现可能退化成了 O(n)，不再是每键 O(1)")
        if name == "metrics":
            want = len(metrics.bucket_bounds()) + 1
            got = r["histogram_slots"]
            if got != want:
                problems.append(
                    f"metrics: 直方图格数 = {got}，应为 {want} —— 实现可能退回了"
                    "「存原始观测值」，内存会随 QPS 线性增长（一个被刷的接口即可 OOM，"
                    "而 `_count` 增长看起来完全正常）")
    metrics.reset()
    return problems, scaling


def _diff_against(baseline: dict, current: dict, tolerance: float) -> list[tuple]:
    """逐基准与留档对比，返回 [(名字, 基线, 当前, 倍数, 结论)]。

    ⚠️ 只在**同一台机器**上有意义 —— 调用方要先检查机器指纹（`_host_diff`）。
    """
    rows: list[tuple] = []
    cur_bench = current.get("bench", {})
    old_bench = baseline.get("bench") or {}
    for name, cur in cur_bench.items():
        old = old_bench.get(name) or {}
        a, b = old.get("us_per_op"), cur.get("us_per_op")
        if not a or not b:
            continue
        factor = b / a
        if factor > tolerance:
            verdict = "**退化**"
        elif factor < 1 / tolerance:
            verdict = "有改善"
        else:
            verdict = "持平"
        rows.append((name, a, b, factor, verdict))
    # 反向也要看：留档里有、本次没有的项 —— 那说明基准被删了或改名了，
    # 而"永远不再被对比"这件事本身没有任何症状。
    for name in old_bench:
        if name not in cur_bench:
            rows.append((name, old_bench[name].get("us_per_op", 0.0), None, 0.0,
                         "**本次缺席**"))
    return rows


def _host_diff(baseline: dict, current: dict) -> str | None:
    """机器不同就明说 —— 否则一屏"退化"会让人去查代码，而根因只是换了台机器。"""
    bh, ch = baseline.get("host", {}), current.get("host", {})
    if not bh:
        return None
    if (bh.get("platform"), bh.get("cpu")) != (ch.get("platform"), ch.get("cpu")):
        return (f"⚠️ 基线与本次**不是同一台机器**"
                f"（{bh.get('platform')}／{bh.get('cpu')} vs "
                f"{ch.get('platform')}／{ch.get('cpu')}）：绝对耗时不跨机器可比，"
                f"请在本机重新生成基线")
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=20000, help="每个基准的迭代次数")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    ap.add_argument("--out", default=None,
                    help=f"把本次结果写进留档（生成基线：--out {DEFAULT_BASELINE}）")
    ap.add_argument("--baseline", default=None,
                    help="与这份留档对比（**同一台机器**上才有意义）")
    ap.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE,
                    help=f"绝对对比的容差倍数（默认 {DEFAULT_TOLERANCE}）")
    ap.add_argument("--check", action="store_true",
                    help="CI 门禁：只断言机器无关的不变量（规模 + 内存），失败退出 1")
    args = ap.parse_args()

    # ⚠️ 基线必须在**写文件之前**读进来。
    #    默认 `--out` 与 `--baseline` 常见用法指向同一个文件（"跟上次比"），
    #    先写后读会读到刚落盘的**自己** ⇒ 每一项都算成「持平」。
    #    一个永远说"没退化"的对比比没有对比更危险：它看起来在监控。
    baseline = None
    if args.baseline:
        if not os.path.exists(args.baseline):
            # 静默跳过 = 让人以为"对比过了、没问题"，所以必须显式拒绝。
            print(f"基线文件不存在：{args.baseline}", file=sys.stderr)
            return 2
        with open(args.baseline, encoding="utf-8") as fh:
            baseline = json.load(fh)

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

    # ---- 门禁：机器无关的不变量（CI 用）+ 可选的基线对比 ----
    # 人类可读的报告一律走 stderr：`--json` 时 stdout 必须只有那一段 JSON。
    exit_code = 0

    if args.check:
        problems, scaling = check_invariants(args.n)
        results["scaling"] = scaling
        print("\n机器无关的不变量（--check）：", file=sys.stderr)
        for name, r in scaling.items():
            flag = "OK" if r["ok"] else "退化"
            print(f"  {name:<10} 规模 {r['n_small']}→{r['n_large']}："
                  f"单次 {r['us_small']:.2f}μs → {r['us_large']:.2f}μs "
                  f"（{r['ratio']:.2f} 倍，上限 {SCALING_TOLERANCE}）[{flag}]",
                  file=sys.stderr)
        if problems:
            print("\n⚠️ 不变量被破坏：", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            exit_code = 1
        else:
            print("  ⇒ 全部通过", file=sys.stderr)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
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

    # ---- 写留档（基线已在开头读过，这里落的盘不会污染本次对比） ----
    if args.out:
        # newline="\n" 不是风格问题：Windows 默认写 CRLF，而仓库约定磁盘上一律 LF。
        # 不写死换行符 ⇒ 内容没变而整份 diff 全红，基线文件会被这类噪声淹没。
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=2)
        print(f"\n已写入 {args.out}", file=sys.stderr)
        if baseline is None:
            # 不自动对比是**有意**的：自动对比会让"退化 → 本次红 → 顺手覆写基线 → 下次绿"
            # 变成一个**自愈门禁**，而自愈门禁等于没有门禁。对比必须是一次明确的动作。
            print(f"提示：下次加 `--baseline {args.out}` 就能与这一次对比。", file=sys.stderr)

    if baseline is not None:
        warn = _host_diff(baseline, results)
        if warn:
            print(warn, file=sys.stderr)
        rows = _diff_against(baseline, results, args.tolerance)
        print(f"\n与基线 {args.baseline} 对比（容差 {args.tolerance} 倍）：", file=sys.stderr)
        if not rows:
            print("  （没有可比项 —— 基线缺字段，或口径已变）", file=sys.stderr)
        worse: list[str] = []
        for name, old, new, factor, verdict in rows:
            shown = "  （缺席）" if new is None else f"{new:>9.2f}"
            print(f"  {name:<12}{old:>9.2f} → {shown}  {factor:>6.2f}x  {verdict}",
                  file=sys.stderr)
            if "退化" in verdict or "缺席" in verdict:
                worse.append(name)
        if worse:
            print(f"\n⚠️ 需要人工确认：{'、'.join(worse)}", file=sys.stderr)
            exit_code = 1
        else:
            print("  ⇒ 无退化", file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
