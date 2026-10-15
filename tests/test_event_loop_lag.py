"""事件循环阻塞探针（`app/loopwatch.py`）的守卫。

它守的是什么
------------
这条指标的存在意义是"把**卡住**与**慢**分开"（见 `app/loopwatch.py` 的模块说明）。
所以守卫要证明三件事，缺一条它就退化成装饰品：

1. **真的抓得到阻塞** —— 起一个探针，在同一个事件循环上做一次同步阻塞，
   指标必须记下来（这是"能看见"）；
2. **不记录抖动** —— 记下来的样本必须**全部** ≥ 阈值。少了这条，Windows 上
   每次心跳（约 15ms 的定时器量化）都会被记一次 ⇒ 指标变成噪声 ⇒ 被人忽略 ⇒
   真卡住 800ms 那次也淹没在里面（"指标最怕的不是漏报，是噪声"）；
3. **异常不会让它死掉** —— 观测组件成为故障源是最讽刺的失败；探针一旦静默死掉，
   "没问题"与"根本没在检查"就分不出来了。

⚠️ 第 2 条**刻意不写成"环境一定没有抖动"**（那是脆的：CI 是共享机器）。
它断言的是不变量："**低于阈值的桶累计为 0**" —— 这在任何环境下都成立。
"""

from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path

from app import loopwatch, metrics

ROOT = Path(__file__).resolve().parent.parent
METRIC = "event_loop_lag_seconds"


def test_probe_records_a_real_block():
    """在同一条事件循环上做一次同步阻塞 ⇒ 必须被记下来。

    阻塞 0.2s 而心跳间隔 0.1s ⇒ **必然**跨越一个到期点（这是间隔取 0.1s 的理由，
    实测依据见 `app/loopwatch.py`：间隔 1.0s 时同样的阻塞完全没被记录）。
    """

    async def main() -> int:
        metrics.reset()
        try:
            async with loopwatch.watching():
                await asyncio.sleep(0.05)   # 先让心跳跑起来
                time.sleep(0.2)             # 同步阻塞，堵住整个事件循环
                await asyncio.sleep(0.3)    # 等心跳观察到它
            return metrics.histogram_count(METRIC)
        finally:
            metrics.reset()

    assert asyncio.run(main()) >= 1, (
        "200ms 的同步阻塞没有被记录下来 —— 探针失效了（阈值太高？间隔太长？）"
    )


def test_probe_never_records_below_the_threshold():
    """低于阈值的样本一个都不能进来（否则指标会变成噪声源）。

    断言的是**桶**而不是"这轮没发生抖动"：所有低于阈值的桶累计必须为 0。
    """

    async def main() -> list[int]:
        metrics.reset()
        try:
            async with loopwatch.watching():
                await asyncio.sleep(0.6)
            bounds = metrics.bucket_bounds()
            cumulative = metrics.histogram_values(METRIC)
            below = bounds.index(loopwatch._THRESHOLD)
            return cumulative[:below]
        finally:
            metrics.reset()

    below_counts = asyncio.run(main())
    assert not any(below_counts), (
        f"有样本落在低于阈值（{loopwatch._THRESHOLD}s）的桶里：{below_counts} —— "
        "阈值没有生效，指标会把调度抖动（Windows 上空载约 15ms）也记进来"
    )


def test_probe_is_cancelled_when_the_context_exits():
    """退出上下文后不能留下悬挂任务（否则每次滚动发布留一个）。"""

    async def main() -> list[str]:
        async with loopwatch.watching():
            await asyncio.sleep(0.05)
        await asyncio.sleep(0)
        return [t.get_name() for t in asyncio.all_tasks()
                if t is not asyncio.current_task()]

    leftovers = asyncio.run(main())
    assert "event-loop-lag-probe" not in leftovers, (
        f"事件循环阻塞探针在退出后仍在运行：{leftovers}"
    )


def test_probe_survives_an_exception_in_the_sink(monkeypatch):
    """单次心跳里的异常不能让它死掉 —— 死了就再也看不到阻塞。

    验证方式：让 sink 抛异常，然后做**两次**阻塞。若探针在第一次异常后就退出，
    第二次不会被观察到 ⇒ 调用次数只有 1。
    """
    calls = {"n": 0}

    def boom(name, value, **labels):
        calls["n"] += 1
        raise RuntimeError("sink 炸了")

    monkeypatch.setattr(loopwatch.metrics, "observe", boom)

    async def main() -> int:
        async with loopwatch.watching():
            await asyncio.sleep(0.05)
            time.sleep(0.2)
            await asyncio.sleep(0.15)
            time.sleep(0.2)
            await asyncio.sleep(0.3)
        return calls["n"]

    assert asyncio.run(main()) >= 2, (
        "探针在第一次心跳异常之后就停止工作了 —— 观测组件成了故障源"
    )


def test_probe_registration_is_awaited():
    """探针必须**真的被挂进 lifespan**，且那个调用必须 `await`。

    ⚠️ 为什么不能用字符串断言（2026-10-15 实际踩到）
    ----------------------------------------------
    第一版写的是 `assert "stack.enter_async_context(loopwatch.watching())" in src`。
    而 `AsyncExitStack.enter_async_context` 是 **async 方法** —— 少了 `await` 之后：

      * 那行**仍然包含**这段子串 ⇒ 断言照样绿（**假绿**）；
      * 实际后果只是一条 RuntimeWarning（"coroutine was never awaited"），
        而探针**从未运行**；
      * 服务照常启动、面板照常空白 ⇒ "没在检查"与"没问题"又一次分不出来。

    所以判据换成 **AST**：`enter_async_context` 的调用必须落在 `await` 表达式里。
    这条规则本身是通用的（它永远是协程方法，永远要 await），不只为这一个调用点服务。
    """
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "loopwatch.watching()" in src, (
        "lifespan 里没有挂事件循环阻塞探针 —— 指标将永远不存在，"
        "EventLoopBlocked 告警也就永远不会响"
    )

    tree = ast.parse(src)
    awaited: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Await):
            for inner in ast.walk(node.value):
                awaited.add(id(inner))

    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "enter_async_context"]
    assert calls, (
        "main.py 里找不到 `enter_async_context` 的调用 —— 挂载点被删了？"
    )
    for call in calls:
        assert id(call) in awaited, (
            f"main.py:{call.lineno} 的 `enter_async_context(...)` 没有 `await` —— "
            "它是 async 方法，漏掉 await 只报一条 RuntimeWarning，被注册的上下文"
            "**根本不会启动**（服务照常起来，指标永远为空）"
        )
