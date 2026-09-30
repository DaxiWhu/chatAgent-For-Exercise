"""进程内指标注册表 + Prometheus 文本暴露。

为什么自己写而不是上 prometheus-client
------------------------------------
只用到计数器、直方图、Gauge 三类，加起来一百多行；多引一个依赖换不来实质收益，
而且自研版本对"被测试直接读数值"更友好（`value()` / `histogram_count()`）。

为什么需要 Gauge（而不是全靠计数器推）
------------------------------------
有些量**推不出来**，只能测：最典型的是"待人工审核的条数"。队列可能一直挂着
不产生新事件（没人处理，也不再有新内容进来），用 `increase(review_needed_total)`
只能看到"新增了多少"，看不到"还积压着多少"。而 human-in-the-loop 服务里
**积压本身就是故障**（工单丢了），所以必须有 Gauge。

局限（必须写清楚，否则会被误用）
------------------------------
- **只在单进程内有效**：多 worker / 多实例下每个进程各算各的，Prometheus 侧
  必须按 `instance` 维度聚合。要全局准数就得换 prometheus-client + Pushgateway
  或用 Redis 汇总。
- 直方图用**固定桶计数**（每格一个整数），内存 O(桶数)、与请求量无关。
  代价是丢掉分位数精度：只能从桶边界读出"P95 落在 1~2.5 秒之间"，
  拿不到精确的 1.37 秒。换桶边界 = 改口径，历史数据不可比，所以要改就一次改到位。
- 标签值会进入 key，**不要用 user_id / thread_id / 原始 URL** 这类高基数标签
  （会把内存打爆）。用路由模板与状态码。

线程安全：所有读写都在一把锁下，`inc/observe/set_gauge` 是原子的。
"""

import bisect
import threading
import time
from collections import defaultdict

_LOCK = threading.Lock()

# key = (指标名, ((标签名, 标签值), ...)) —— 标签冻结成元组才能当 key
_COUNTERS: dict[tuple, float] = defaultdict(float)
_HISTOGRAMS: dict[tuple, "Histogram"] = {}
_GAUGES: dict[tuple, float] = {}

# 延迟直方图的桶边界（秒）。覆盖"内存检索毫秒级"到"LLM 首字节十几秒"
_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)

_START = time.time()


class Histogram:
    """固定桶直方图状态：每格一个计数 + 总和 + 总数。

    为什么不存原始观测值（2026-09-29 改）
    ------------------------------------
    存原始值（`list.append`）实现最短，但内存**由请求量决定**：一个被刷的接口、
    或一次压测，就能让进程内存随 QPS 线性增长，直到 OOM。而指标是用来**观察**故障的
    组件 —— 它自己变成故障源是最讽刺的一种失败，且没有任何指标能反映它
    （`_count` 增长是正常的，看起来一切正常）。

    固定桶把内存钉死在 `len(_BUCKETS) + 1` 个整数上，与观测次数无关。
    """

    __slots__ = ("buckets", "sum", "count")

    def __init__(self):
        # 最后一格是 +Inf（大于所有边界）—— 与 Prometheus 的 `le="+Inf"` 对应
        self.buckets = [0] * (len(_BUCKETS) + 1)
        self.sum = 0.0
        self.count = 0

    def observe(self, v: float) -> None:
        # bisect_left：第一个 >= v 的边界的下标。v 恰好等于边界时落进该桶，
        # 与 Prometheus 的"le = 小于等于"语义一致；超出所有边界则落进 +Inf 那格。
        self.buckets[bisect.bisect_left(_BUCKETS, v)] += 1
        self.sum += v
        self.count += 1

    def cumulative(self) -> list:
        """累积计数（Prometheus 的 `_bucket` 语义要求逐格递增）。"""
        out, acc = [], 0
        for c in self.buckets:
            acc += c
            out.append(acc)
        return out


def _key(name, labels):
    """把 (指标名, labels dict) 归一成可哈希的 key。labels 为 None 时用空元组。"""
    if not labels:
        return (name, ())
    return (name, tuple(sorted((str(k), str(v)) for k, v in labels.items())))


def inc(name, amount=1, **labels):
    """计数器 +amount（默认 +1）。

    name: 指标名，如 "http_requests_total"。
    labels: 标签，如 status=200、path="/v1/chat"。**避免高基数标签**。
    """
    with _LOCK:
        _COUNTERS[_key(name, labels)] += amount


def observe(name, value, **labels):
    """直方图记录一次观测（如请求耗时，单位秒）。

    name: 指标名，如 "http_request_duration_seconds"。
    value: 观测值（秒）。
    labels: 标签；**避免高基数标签**（每个标签组合一份桶状态）。
    """
    k = _key(name, labels)
    with _LOCK:
        h = _HISTOGRAMS.get(k)
        if h is None:
            h = _HISTOGRAMS[k] = Histogram()
        h.observe(float(value))


def set_gauge(name, value, **labels):
    """设置瞬时值型指标（如待审积压条数）。

    与计数器不同，Gauge 是**覆盖**而不是累加 —— 它回答"此刻是多少"，
    所以每次都要传入当前全量值。

    name: 指标名，如 "review_pending"。
    value: 当前值（会被转成 float）。
    """
    with _LOCK:
        _GAUGES[_key(name, labels)] = float(value)


def gauge(name, **labels):
    """读 Gauge 当前值（测试用；生产从 /metrics 拉）。"""
    with _LOCK:
        return _GAUGES.get(_key(name, labels), 0.0)


def value(name, **labels):
    """读计数器的当前值（测试用；生产从 /metrics 拉）。"""
    with _LOCK:
        return _COUNTERS.get(_key(name, labels), 0.0)


def histogram_values(name, **labels):
    """读直方图的**累积**桶计数（测试用）。

    返回长度 = `len(bucket_bounds()) + 1`，最后一格是 `+Inf`，值逐格递增
    —— 与 Prometheus 暴露出来的 `_bucket` 同语义。

    ⚠️ 旧版这里返回的是原始观测值列表 `[1.0, 2.0]`。改口径是**故意**的：
    "内存有界"这件事没有别的观测出口，只有"格数固定"才能证明没在存原始值。
    """
    with _LOCK:
        h = _HISTOGRAMS.get(_key(name, labels))
        return h.cumulative() if h is not None else [0] * (len(_BUCKETS) + 1)


def histogram_count(name, **labels):
    """读直方图的观测总次数（测试用）。"""
    with _LOCK:
        h = _HISTOGRAMS.get(_key(name, labels))
        return h.count if h is not None else 0


def histogram_sum(name, **labels):
    """读直方图的观测值总和（测试用）。"""
    with _LOCK:
        h = _HISTOGRAMS.get(_key(name, labels))
        return h.sum if h is not None else 0.0


def histogram_slots(name, **labels):
    """该直方图占用的**存储格数**（测试用）：固定桶下恒为 `len(_BUCKETS) + 1`。

    存在的唯一理由是给"内存有界"一个可断言的口子 —— 光看 `histogram_count()`
    一直涨是正常的，只有"格数不随观测次数增长"才能证明没有退回去存原始值。
    """
    with _LOCK:
        h = _HISTOGRAMS.get(_key(name, labels))
        return len(h.buckets) if h is not None else 0


def bucket_bounds():
    """直方图的桶边界（秒）。改它 = 改口径，历史数据不可比。"""
    return _BUCKETS


def reset():
    """清空所有指标（**仅供测试**，生产不要调用）。"""
    with _LOCK:
        _COUNTERS.clear()
        _HISTOGRAMS.clear()
        _GAUGES.clear()


def _fmt_labels(labels):
    """把冻结的标签元组还原成 Prometheus 的 `{k="v"}` 文本（值做最小转义）。"""
    if not labels:
        return ""
    inner = ",".join(f'{k}="{str(v).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
                     for k, v in labels)
    return "{" + inner + "}"


def render():
    """输出 Prometheus 文本格式（text/plain; version=0.0.4）。

    计数器输出 `# TYPE x counter` + 样本行；直方图输出 `_bucket/_sum/_count` 三件套。
    末尾附带 `process_uptime_seconds`，便于看实例存活时长。
    """
    lines = []
    with _LOCK:
        counters = dict(_COUNTERS)
        # 快照成不可变结构后再出锁渲染：渲染期间不该占着锁（它要拼很多字符串）
        histograms = {k: (tuple(h.cumulative()), h.sum, h.count)
                      for k, h in _HISTOGRAMS.items()}
        gauges = dict(_GAUGES)

    for (name, labels), val in sorted(counters.items()):
        lines.append(f"# TYPE {name} counter")
        lines.append(f"{name}{_fmt_labels(labels)} {val}")
        lines.append("")

    for (name, labels), val in sorted(gauges.items()):
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name}{_fmt_labels(labels)} {val}")
        lines.append("")

    for (name, labels), (cum, total_sum, total_count) in sorted(histograms.items()):
        lines.append(f"# TYPE {name} histogram")
        # `_bucket` 是**累积**的（le = 小于等于）：0.3 秒同时落进 le=0.5 及之后每一格
        for b, cnt in zip(_BUCKETS, cum):
            lab = dict(labels)
            lab["le"] = b
            lines.append(f"{name}_bucket{_fmt_labels(tuple(sorted(lab.items())))} {cnt}")
        lab = dict(labels)
        lab["le"] = "+Inf"
        lines.append(f"{name}_bucket{_fmt_labels(tuple(sorted(lab.items())))} {cum[-1]}")
        lines.append(f"{name}_sum{_fmt_labels(labels)} {total_sum}")
        lines.append(f"{name}_count{_fmt_labels(labels)} {total_count}")
        lines.append("")

    lines.append("# TYPE process_uptime_seconds gauge")
    lines.append(f"process_uptime_seconds {time.time() - _START:.3f}")
    return "\n".join(lines) + "\n"
