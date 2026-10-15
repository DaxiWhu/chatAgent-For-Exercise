"""本地微基准的门禁与留档（`scripts/bench_local.py`）守卫。

它守的是什么
------------
`--check` 是 CI 里唯一的性能门禁，而它的**判据全是"不会报错的错"**：

* 判据写反（`<=` 写成 `>`）⇒ 永远绿，而且没人会发现；
* 对比读到刚落盘的**自己** ⇒ 每一项都算「持平」⇒ 一个**永远说没退化**的对比 ——
  它比没有对比更危险，因为它看起来在监控（与 `eval_scorecard.py` 同一个坑，
  那边有 `test_eval_scorecard.py` 钉住，这边照搬同一条思路）；
* 基线缺失时静默跳过 ⇒ 让人以为"对比过了、没问题"。

这三条在正常使用中都不会报错，所以只能靠断言钉住。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from scripts import bench_local as B

ROOT = Path(__file__).resolve().parents[1]


def _on_benchmark(n: int) -> dict:
    """一个**退化成 O(n)** 的假基准：每次"操作"内部都要扫 n 个元素。

    这正是 `--check` 要抓的形状（限流器若退回"每次请求扫全表"，就是这个曲线）。
    """

    def run(k: int) -> None:
        for _ in range(k):
            acc = 0
            for j in range(n):
                acc += j

    t0 = time.perf_counter()
    run(n)
    dt = time.perf_counter() - t0
    return {"n": n, "total_s": dt, "us_per_op": dt / n * 1e6}


def _constant_benchmark(n: int) -> dict:
    """一个 O(1)/次 的假基准：单次耗时与样本量无关。"""

    def run(k: int) -> None:
        acc = 0
        for i in range(k):
            acc = (acc * 31 + i) % 1_000_003

    t0 = time.perf_counter()
    run(n)
    dt = time.perf_counter() - t0
    return {"n": n, "total_s": dt, "us_per_op": dt / n * 1e6}


def test_scaling_check_catches_a_linear_benchmark():
    """退化成 O(n) 必须被抓住 —— 这是门禁存在的唯一理由。"""
    r = B._scaling_check("fake", _on_benchmark, 200)
    assert r["ratio"] > B.SCALING_TOLERANCE, (
        f"O(n) 的基准在样本翻 4 倍后单次耗时只涨了 {r['ratio']:.2f} 倍，"
        f"但判据上限是 {B.SCALING_TOLERANCE} —— 门禁抓不到线性退化"
    )
    assert r["ok"] is False


def test_scaling_check_accepts_a_constant_benchmark():
    """O(1) 必须放行 —— 判据写反（把 `<=` 写成 `>`）会让它这里红。

    ⚠️ 判据写反的症状是"**所有**真实基准都红"，而那时人的第一反应是调大
    `SCALING_TOLERANCE`，最后把门禁调成摆设。所以这条要钉住。
    """
    r = B._scaling_check("fake", _constant_benchmark, 5_000)
    assert r["ratio"] <= B.SCALING_TOLERANCE, (
        f"O(1) 的基准被判成退化（比值 {r['ratio']:.2f} > {B.SCALING_TOLERANCE}）—— "
        "判据写反了"
    )
    assert r["ok"] is True


def _spiky_constant_benchmark(base_n: int):
    """O(1) 的基准，但**第一次量"大样本"时读数被一次固定停顿污染**。

    模拟 CI runner 上的一次 GC / 邻居抢占 —— 噪声是**单向**的（只会让某一次变慢）。
    刻意不用随机数：这条断言必须是确定性的，否则它自己就会变成随机红。

    ⚠️ 停顿必须落在**被计时的区间内**（这里直接加到 `total_s` 上）。写成
    "计时之前 sleep 一下"是不行的 —— 那样它压根进不了读数，这条守卫会变成
    一个恒绿的空壳（第一版就是这么写错的，变异验证时才发现）。
    """
    seen: dict[int, int] = {}

    def bench(n: int) -> dict:
        seen[n] = seen.get(n, 0) + 1
        out = _constant_benchmark(n)
        if n != base_n and seen[n] == 1:
            out["total_s"] += 0.4
            out["us_per_op"] = out["total_s"] / n * 1e6
        return out

    return bench


def test_one_noise_spike_does_not_turn_o1_into_a_false_red():
    """★ 一次噪声尖峰不该把 O(1) 判成退化。

    CI runner 是共享机器：一次 GC 或邻居抢占落在 n 与 4n 的**任意一侧**，单次
    测量出的比值就会被顶过上限 ⇒ **随机红**。而随机红的终点是有人把门禁关掉
    （本项目对"习惯性失败"的处置写在 ci.yml 的 security job 注释里：它比没有
    门禁更糟，因为它给了"有防护"的错觉）。

    `_scaling_check` 每个规模量 3 次取最小 ⇒ 这一次停顿必须被吸收掉。
    变异验证：把取最小换成"只量一次"，比值会到几百倍 ⇒ 本测试红。
    """
    r = B._scaling_check("fake", _spiky_constant_benchmark(20_000), 20_000)
    assert r["ok"] is True, (
        f"一次固定停顿就把 O(1) 判成了退化（比值 {r['ratio']:.2f}）—— "
        f"「每规模量 {B.SCALING_REPEATS} 次取最小」这件事没生效"
    )
    assert r["repeats"] == B.SCALING_REPEATS, (
        f"判据里记录的重复次数是 {r['repeats']!r}，与常量 "
        f"{B.SCALING_REPEATS} 不一致 —— 报告的是 A、量的是 B"
    )


def test_check_invariants_is_green_on_the_real_benchmarks():
    """真实基准本身必须过（否则 CI 天天红，门禁会被关掉）。"""
    problems, scaling = B.check_invariants(2_000)
    assert not problems, f"真实基准没通过不变量检查：{problems}"
    assert set(scaling) == {"ratelimit", "quota", "metrics"}


def test_check_invariants_reports_a_broken_histogram(monkeypatch):
    """直方图格数变了必须报出来（内存有界被破坏）。"""

    def fake_metrics(n: int) -> dict:
        out = _constant_benchmark(n)
        out["histogram_slots"] = 99  # 恒定值被破坏
        return out

    monkeypatch.setattr(B, "bench_metrics", fake_metrics)
    problems, _ = B.check_invariants(2_000)
    assert any("格数" in p for p in problems), (
        f"直方图格数不对却没报出来：{problems}"
    )


def test_diff_verdicts_cover_all_four_outcomes():
    """退化 / 持平 / 有改善 / 本次缺席 —— 四种结论都要分得出来。"""
    baseline = {"bench": {"a": {"us_per_op": 10.0},
                          "b": {"us_per_op": 10.0},
                          "c": {"us_per_op": 10.0},
                          "gone": {"us_per_op": 10.0}}}
    current = {"bench": {"a": {"us_per_op": 100.0},   # 10 倍 ⇒ 退化
                         "b": {"us_per_op": 10.0},    # 持平
                         "c": {"us_per_op": 1.0}}}    # 1/10 ⇒ 有改善
    rows = {r[0]: r[4] for r in B._diff_against(baseline, current, 2.5)}
    assert "退化" in rows["a"], rows
    assert rows["b"] == "持平", rows
    assert rows["c"] == "有改善", rows
    assert "缺席" in rows["gone"], (
        "基线里有、本次没有的基准没被报出来 —— 它从此再也不会被对比，"
        "而这件事没有任何症状"
    )


def test_host_diff_flags_a_different_machine():
    """换了机器必须明说 —— 否则一屏"退化"会让人去查代码，而根因只是换了台机器。"""
    base = {"host": {"platform": "Linux-x", "cpu": "Xeon"}}
    same = {"host": {"platform": "Linux-x", "cpu": "Xeon"}}
    other = {"host": {"platform": "Windows", "cpu": "Ryzen"}}
    assert B._host_diff(base, same) is None
    assert B._host_diff(base, other) is not None


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "bench_local.py"), *argv],
        cwd=ROOT, capture_output=True, text=True, timeout=180)


def test_missing_baseline_fails_loudly(tmp_path):
    """基线不存在时必须显式失败 —— 静默跳过等于谎报"对比过了"。"""
    proc = _run("--baseline", str(tmp_path / "nope.json"), "--n", "500")
    assert proc.returncode == 2, (
        f"基线文件不存在时退出码是 {proc.returncode}，应当是 2（显式拒绝）"
    )
    assert "不存在" in proc.stderr


def test_baseline_is_read_before_the_output_is_written(tmp_path):
    """★ 对比的对象必须是**上一轮**的留档，不是刚落盘的自己。

    ⚠️ 这条判据来自 `eval_scorecard.py` 踩过的同一个坑：默认 `--out` 与
    `--baseline` 常指向同一个文件，先写后读 ⇒ 每一项都算成「持平」⇒
    一个**永远说没退化**的对比。它比没有对比更危险：它看起来在监控。

    构造：预置一份"比真实值快 10^6 倍"的假基线 ⇒ 本次必然被判退化（退出码 1）。
    若实现是"先写后读"，基线会被本次结果覆盖 ⇒ 全部持平 ⇒ 退出码 0 ⇒ 本测试红。
    """
    target = tmp_path / "bench.json"
    real = json.loads(
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "bench_local.py"),
             "--n", "500", "--json"],
            cwd=ROOT, capture_output=True, text=True, timeout=180).stdout)
    fake = {"host": real["host"], "n": 500,
            "bench": {name: {"us_per_op": 1e-6} for name in real["bench"]}}
    target.write_text(json.dumps(fake), encoding="utf-8")

    proc = _run("--n", "500", "--out", str(target), "--baseline", str(target))
    assert proc.returncode == 1, (
        f"预置的基线明显更快，却没有报退化（退出码 {proc.returncode}）—— "
        "最可能的原因是**先写输出再读基线**，于是对比的是刚落盘的自己。\n"
        f"stderr：{proc.stderr[-800:]}"
    )
    assert "退化" in proc.stderr
