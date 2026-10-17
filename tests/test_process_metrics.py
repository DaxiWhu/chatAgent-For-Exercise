"""进程级资源指标（RSS / CPU / GC）的守卫。

为什么这些断言必须钉**数值行为**而不是"源码里有这行字"
------------------------------------------------------
这类指标最典型的失效不是"代码没写"，而是**读到了一个假值**：
* 读的是缓存/常量 ⇒ 永远不变，"内存泄漏"永远看不见；
* 读错了单位（页数当字节）⇒ 数值差 4096 倍，阈值全废；
* 依赖了 `psutil` ⇒ 开发机能 import（传递依赖）、生产镜像没有 ⇒ 线上**少了三条序列**，
  而本地一切正常。

前两条只有"真分配一块内存、看它跟不跟"能验；第三条只有看依赖形态能验。

全部离线：不连 PG / Redis，不调模型。
"""

import re
import time
from pathlib import Path

import pytest

from app import metrics

ROOT = Path(__file__).resolve().parent.parent


def test_process_metrics_are_exposed_with_correct_types():
    """/metrics 里必须真的出现这四条序列，且 TYPE 声明正确。

    ⚠️ 不读 `metrics.gauge(...)` 而读 `render()` 的文本：读一个不存在的 gauge
    也返回 0.0 —— 只用读函数断言，埋点整个没落地也是"0"，看不出区别。
    """
    metrics.reset()
    text = metrics.render()

    for name, kind in (
        ("process_resident_memory_bytes", "gauge"),
        ("process_cpu_seconds_total", "counter"),
        ("python_gc_collections_total", "counter"),
        ("python_gc_collected_objects_total", "counter"),
    ):
        assert f"# TYPE {name} {kind}" in text, f"{name} 的 TYPE 行不对：\n{text}"

    # 必须有真实数值（不能是 0，也不能缺样本行）
    assert re.search(r"^process_resident_memory_bytes [1-9][0-9.e+]*$", text, re.M), text
    assert re.search(r"^process_cpu_seconds_total [0-9]", text, re.M), text


def test_rss_gauge_follows_a_real_allocation():
    """真分配 96MB 之后 RSS 必须跟着涨 —— 这条是"读的是真值"的唯一证据。

    阈值取得很松（只要求涨过 24MB）：分配器可能复用进程已有的空闲页，
    不同平台的记账时机也不同。这里要证的是"它跟着内存走"，不是"精确到字节"。
    """
    metrics.reset()
    metrics.render()
    before = metrics.gauge("process_resident_memory_bytes")
    assert before > 0, "RSS 读不到 —— 平台分支没覆盖到当前环境"

    blob = bytearray(96 * 1024 * 1024)
    for i in range(0, len(blob), 4096):     # 逐页触碰，逼操作系统给物理页
        blob[i] = 1

    metrics.render()
    after = metrics.gauge("process_resident_memory_bytes")
    assert after - before > 24 * 1024 * 1024, (
        f"分配 96MB 后 RSS 只从 {before} 变到 {after} —— 读到的不是真值")
    del blob


def test_cpu_seconds_grows_with_real_work():
    """CPU 秒数必须随真实计算增长，而**睡眠不该让它涨**。

    后半句才是这条指标存在的理由：它要回答"在算还是在等"。若实现误用
    `time.time()`（墙钟），睡眠期间它照样涨，于是"等上游模型"会被读成"CPU 打满"——
    两个完全不同的处置方向（扩容 vs 查上游），却被同一个数字掩盖。
    """
    metrics.render()
    first = metrics.value("process_cpu_seconds_total")
    assert first > 0

    deadline = time.perf_counter() + 0.2
    total = 0
    while time.perf_counter() < deadline:      # 真烧 200ms CPU
        total += 1

    metrics.render()
    after_work = metrics.value("process_cpu_seconds_total")
    assert after_work > first, f"算了 200ms CPU，读数没涨：{first} → {after_work}"
    assert total > 0

    time.sleep(0.3)                            # 纯等待，不消耗 CPU
    metrics.render()
    after_sleep = metrics.value("process_cpu_seconds_total")
    assert after_sleep - after_work < 0.15, (
        f"睡了 300ms（没干活），CPU 秒数涨了 {after_sleep - after_work:.3f}s "
        "—— 说明读的是墙钟而不是 CPU 时间")


def test_gc_counters_cover_every_generation():
    """三代必须都有 —— 只报第 0 代会把"对象活过两轮回收"这个泄漏信号抹掉。"""
    metrics.reset()
    text = metrics.render()
    for generation in ("0", "1", "2"):
        assert f'python_gc_collections_total{{generation="{generation}"}}' in text
        assert f'python_gc_collected_objects_total{{generation="{generation}"}}' in text


def test_a_broken_reader_does_not_break_the_exposition(monkeypatch):
    """读 RSS 失败时：这条序列缺席，但**其余指标照常输出**。

    指标是观察故障的组件。让它因为一次 `OSError` 把整个 `/metrics` 变成 500，
    等于"监控自己成了最新故障"，而这恰好发生在最需要监控的时候。
    """
    def boom():
        raise OSError("读不到 /proc")

    monkeypatch.setattr(metrics, "_resident_memory_bytes", boom)
    metrics.reset()
    metrics.inc("http_requests_total", status="200")

    text = metrics.render()          # 不能抛

    assert "http_requests_total" in text
    assert "python_gc_collections_total" in text
    assert "process_resident_memory_bytes" not in text


def test_metrics_module_does_not_import_psutil():
    """不许引入 `psutil` —— 它不在 `requirements.txt` 里。

    这条看着像洁癖，实际是"本地绿 ≠ 生产绿"的典型：`psutil` 在开发机上
    是别的包的传递依赖（实测 7.2.2 在），而生产镜像只装 `requirements.txt`。
    一旦依赖它，`/metrics` 在线上要么少三条序列、要么整个 500，
    而开发机与 CI 的离线用例都发现不了。

    用正则匹配**导入语句**而不是 `in src`：模块 docstring 里正好解释了
    "为什么不用 psutil"，那不算依赖。
    """
    src = (ROOT / "app" / "metrics.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(?:import|from)\s+psutil\b", src, re.M), (
        "app/metrics.py 引入了 psutil —— 它不在 requirements.txt 里")


@pytest.mark.parametrize("name", ["process_resident_memory_bytes",
                                  "process_cpu_seconds_total",
                                  "python_gc_collections_total",
                                  "python_gc_collected_objects_total"])
def test_process_metrics_survive_reset(name):
    """`reset()`（测试用）之后仍能重新出现 —— 说明它们**不是**一次性的。

    真实故障长这样：指标只在启动时写一次，之后永远冻结。若 `render()` 不再刷新，
    这条断言就会失败。
    """
    metrics.reset()
    assert name in metrics.render()
    metrics.reset()
    assert name in metrics.render()
