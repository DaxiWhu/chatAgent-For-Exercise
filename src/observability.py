"""轻量观测出口：让 `src/` 能把指标交出去，而**不反向依赖** `app/`。

为什么需要它
------------
`src/` 是**纯库层** —— CLI、脚本、notebook、测试都直接调它。一旦在链路里
`import app.metrics`，服务层（FastAPI / config / 日志装配）就被拖进每一个离线
调用点，测试也不得不先起一个"半个服务"。所以项目里的依赖方向是**单向**的：
`app/` → `src/`，反过来不行。

但有些量只有链路内部知道，而且**每次请求**都要报（不是启动时读一次就够）：

* 检索命中了几个片段、是不是空召回、有没有降级、花了多久；
* `researcher` 打了几次检索；
* 出口护栏拦下了什么。

`index_identity` 那种"链路把结论缓存在进程里、`app` 启动时读一次"的办法对它们
不成立 —— 那是**启动自检**的形状，不是**逐请求指标**的形状。

所以这里放一个**可注入的出口**（与 `graph.set_model()`、`build_graph(model=...)`
是同一手法，项目里已有先例）：

    # 进程启动时（app/main.py，且只在这里）
    from src import observability
    observability.set_sink(_MetricsSink())

    # 链路内部（src/rag/…、src/agents/…）
    observability.counter("rag_hits_total", result="empty")

**没注入 sink 时全部是空操作** —— 库层单独跑、测试单独跑都不受影响，链路里也
不需要写 `try: import app.metrics except ImportError: pass` 这种判断。

为什么不做成"可注册多个回调"
----------------------------
一个进程只需要一个出口（就是 `app.metrics`）。多注册点会让"到底谁在收"变得
不确定，而**指标的语义依赖唯一性**：同一次事件被两个 sink 收下，计数就翻倍，
而且翻倍这件事本身没有任何指标能反映（看起来只是"量变多了"）。

失败语义
--------
`counter/histogram/gauge` **永不抛异常**。指标是**观察**故障的组件，让它成为
故障源（甚至把业务请求带崩）是最讽刺的一种失败。sink 里的异常一律降级为
debug 日志。
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "Sink",
    "counter",
    "gauge",
    "get_sink",
    "histogram",
    "reset_sink",
    "set_sink",
]


@runtime_checkable
class Sink(Protocol):
    """出口协议。`app/metrics.py` 的形状正好满足它（三个方法同名同签名）。"""

    def counter(self, name: str, amount: float = 1, **labels: Any) -> None: ...

    def histogram(self, name: str, value: float, **labels: Any) -> None: ...

    def gauge(self, name: str, value: float, **labels: Any) -> None: ...


_sink: Sink | None = None


def set_sink(sink: Sink | None) -> None:
    """注入出口（进程启动时调用一次）。传 None 等价于"关闭上报"。

    ⚠️ **当场校验形状，不合格直接拒绝**（2026-10-02 加）
    --------------------------------------------------
    最容易犯的错是把 `app.metrics` **模块**传进来 —— 模块上只有
    `inc` / `observe` / `set_gauge`，**没有** `counter` / `histogram` / `gauge`。
    那样注入之后，`_dispatch` 的 `getattr(sink, method)` 抛 AttributeError，
    再被它自己的兜底 `except` 吞掉 ⇒ **全部指标静默变成空操作**：服务正常、
    探针正常、面板全空白，而没有任何一处报错。写本文档的探针就踩了两次。

    "指标一条都不出现"与"这段代码根本没在跑"必须能分开 —— 所以接线错误要在
    **启动期**炸出来，而不是变成运行期的静音。这里用 `runtime_checkable` 的
    `Sink` 协议检查（只查方法名存不存在，不查签名，够用了）。
    """
    global _sink
    if sink is not None and not isinstance(sink, Sink):
        missing = [m for m in ("counter", "histogram", "gauge")
                   if not callable(getattr(sink, m, None))]
        raise TypeError(
            f"观测出口形状不对（缺方法：{missing}）。最常见的成因是传了 "
            f"`app.metrics` **模块**而不是 `app.main._MetricsSink()` 实例 —— "
            f"模块上只有 inc/observe/set_gauge。接错线之后所有指标会静默变成"
            f"空操作，所以这里直接拒绝。")
    _sink = sink


def get_sink() -> Sink | None:
    """取当前出口（测试用）。"""
    return _sink


def reset_sink() -> None:
    """清掉出口（**仅供测试**）。"""
    global _sink
    _sink = None


def _dispatch(method: str, name: str, value: float, labels: dict) -> None:
    """统一分发 + 统一兜异常。三个公开函数只负责摆好参数。"""
    sink = _sink
    if sink is None:
        return
    try:
        getattr(sink, method)(name, value, **labels)
    except Exception:  # noqa: BLE001  观测失败绝不能影响业务
        logging.debug("指标上报失败（已忽略）：%s/%s", method, name, exc_info=True)


def counter(name: str, amount: float = 1, **labels: Any) -> None:
    """计数器 +amount。未注入 sink 时是空操作。"""
    _dispatch("counter", name, amount, labels)


def histogram(name: str, value: float, **labels: Any) -> None:
    """直方图记录一次观测。未注入 sink 时是空操作。"""
    _dispatch("histogram", name, value, labels)


def gauge(name: str, value: float, **labels: Any) -> None:
    """瞬时值覆盖写。未注入 sink 时是空操作。"""
    _dispatch("gauge", name, value, labels)
