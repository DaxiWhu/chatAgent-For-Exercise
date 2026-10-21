"""工具执行的隔离与单步超时（``src/agents/toolpool.py``）。

为什么单独一个文件
------------------
这里验的是**执行形态**（跑在哪个线程、等多久），与工具的业务语义无关。混进
``test_agent_tool_steps.py`` 会让那一组从"清单记对没记对"变成"清单 + 并发"，
两类失败的原因完全不同（一个是记账 bug，一个是线程/超时配置）。

判据为什么用**线程名**而不是 executor 对象
----------------------------------------
对象 id 只能证明"两个 executor 不是同一个"；线程名能直接证明"这一次执行真的
落在那个池里"。隔离失效最可能的形态是有人把 ``run_tool_step`` 改回
``asyncio.to_thread`` —— 那时对象断言仍然可能通过（池还是那个池，只是没被用），
而线程名会立刻变成 ``asyncio_0``。
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from src import observability
from src.agents import toolpool


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def counter(self, name, amount=1, **labels):  # noqa: ANN001, ANN003
        self.events.append(("counter", name, amount, labels))

    def histogram(self, name, value, **labels):  # noqa: ANN001, ANN003
        self.events.append(("histogram", name, value, labels))

    def gauge(self, name, value, **labels):  # noqa: ANN001, ANN003
        self.events.append(("gauge", name, value, labels))

    def counters(self, name: str) -> list[tuple]:
        return [e for e in self.events if e[0] == "counter" and e[1] == name]


@pytest.fixture
def sink():
    saved = observability.get_sink()
    rec = RecordingSink()
    observability.set_sink(rec)
    yield rec
    observability.set_sink(saved)


@pytest.fixture(autouse=True)
def _fresh_pool():
    """每个用例一个干净的池。

    池是进程级的，但它的**容量配置**是运行期读的（``AGENT_TOOL_WORKERS``）；
    不复位的话，前一个用例建出来的池会带着旧配置继续服务后面的用例 ——
    症状是"单独跑绿、整组跑红"。
    """
    toolpool.dispose()
    yield
    toolpool.dispose()


def test_a_step_runs_off_the_event_loop():
    """★ 工具跑在**独立**池的线程里。

    独立是安全边界而不是性能优化：这个池被打满时，坏的只是"工具变慢"；
    共用默认池时，坏的是限流、会话归属、落库、结算一起排队（见模块说明）。
    """
    seen: dict[str, str] = {}

    def where() -> int:
        seen["name"] = threading.current_thread().name
        return 7

    async def go() -> int:
        return await toolpool.run_tool_step(where, tool="probe")

    assert asyncio.run(go()) == 7
    assert seen["name"].startswith("agent-tool"), (
        f"工具没有跑在独立池里（线程名 {seen['name']}）—— 它又一次和服务层抢线程了"
    )


def test_the_isolated_pool_is_not_the_event_loop_default_executor():
    """隔离的核心断言：两个池不是同一个对象。

    与线程名那条互补：线程名证明"执行落在哪"，这一条证明"池本身没被复用"。
    只测前者的话，一个把默认池命名成 agent-tool-* 的实现也能骗过。
    """
    async def go():
        await asyncio.to_thread(lambda: None)
        loop = asyncio.get_running_loop()
        return getattr(loop, "_default_executor", None), toolpool._pool()

    default, ours = asyncio.run(go())
    assert default is not None, "默认池没被创建出来 ⇒ 这条断言失去了对照组"
    assert ours is not None
    assert ours is not default


def test_a_slow_step_times_out(monkeypatch):
    """★ 单步超时到点就抛，不等工具自己结束。

    时间断言是必要的：只断言"抛了 TimeoutError"的话，一个"先等工具跑完、再
    假装超时"的实现也能通过 —— 而那种实现等于没有这个闸门（请求该等多久还是
    等多久）。
    """
    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "0.2")

    def slow() -> str:
        time.sleep(0.9)
        return "late"

    async def go() -> str:
        return await toolpool.run_tool_step(slow, tool="slow")

    started = time.perf_counter()
    with pytest.raises(TimeoutError):
        asyncio.run(go())
    assert time.perf_counter() - started < 0.6


def test_a_timeout_is_counted_with_the_tool_name(monkeypatch, sink):
    """超时要单独计数 —— 否则"工具普遍变慢"只能从整体耗时曲线去猜。"""
    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "0.1")

    def slow() -> None:
        time.sleep(0.5)

    async def go() -> None:
        await toolpool.run_tool_step(slow, tool="kb_search")

    with pytest.raises(TimeoutError):
        asyncio.run(go())

    assert [e[3].get("tool") for e in sink.counters("agent_tool_step_timeout_total")] == [
        "kb_search"
    ]


def test_zero_means_unlimited(monkeypatch):
    """``0`` = 不限：**仍然进池**，只是没有单步上界。

    这个组合是有意的 —— 隔离是安全边界，不该跟着"要不要超时"一起被关掉。
    若关掉超时就连池也换回默认池，等于"配置一个开关顺手削弱一处隔离"。
    """
    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "0")

    def slow() -> str:
        time.sleep(0.3)
        return "done"

    async def go() -> str:
        return await toolpool.run_tool_step(slow, tool="slow")

    assert asyncio.run(go()) == "done"


def test_the_fast_path_adds_no_thread_hop_for_nothing(monkeypatch):
    """正常路径的返回值原样透传（含 ``None``）。

    这条看起来平淡，但它挡住一类真实退化：包装层顺手把返回值转成字符串，
    而工具返回值会被原样喂给模型 —— 变成 ``"None"`` 之后模型会当成一句话读。
    """
    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "5")

    async def go():
        return (
            await toolpool.run_tool_step(lambda: None, tool="a"),
            await toolpool.run_tool_step(lambda: "", tool="b"),
            await toolpool.run_tool_step(lambda: {"k": 1}, tool="c"),
        )

    assert asyncio.run(go()) == (None, "", {"k": 1})


def test_every_function_tool_is_wrapped_by_the_isolated_runner():
    """★ 接线守卫：每个 ``@function_tool`` 都必须紧跟 ``@_isolated(...)``。

    为什么必须用 AST 而不是"跑一遍看看"：漏包一个工具**不会**让任何用例变红 ——
    它会安静地退回官方的 ``asyncio.to_thread``，跟着服务层共用默认池。症状是
    "某个工具卡住时，整个进程连进门都开始排队"，而看板上所有工具都正常。

    这正是 ``test_outbound_timeouts`` 记下的同一个教训：三个同类调用点只漏了
    一个，压测看起来全好，只有走到那条支路的请求被挂死。
    """
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent / "src" / "agents" / "tools.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    tools: list[tuple[str, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        names = {
            dec.func.id
            for dec in node.decorator_list
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name)
        }
        if "function_tool" in names:
            tools.append((node.name, "_isolated" in names))

    assert tools, "一个 function_tool 都没解析到 —— 说明 AST 口径已经和代码对不上了"
    missing = [name for name, isolated in tools if not isolated]
    assert not missing, f"这些工具没有走独立线程池（会跟服务层抢线程）：{missing}"
