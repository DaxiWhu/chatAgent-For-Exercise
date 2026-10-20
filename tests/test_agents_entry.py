"""入口与观测出口：**只有一条作答路径**，没有第二层分诊、也没有运行期降级。

⚠️ 2026-10-20 图退役后，本文件原来的主体（三条 `dispatch` 用例：`disabled` /
`module_unavailable` / `runtime_not_ready`）已整体删除。那三档的处置改成了
**启动期拒绝启动**（见 ``tests/test_startup_guard.py``）—— 运行期不再存在"这条
链路走不了、改走另一条"的分支，也就没有可供断言的降级理由。

留下四条，因为它们在改造后**仍然成立、而且仍然会静默坏掉**：

1. **打回意见必须送到本次运行**。它不能走任何跨轮状态 —— 上一轮的审核意见
   若留在某个共享位置，下一轮的正常对话会莫名其妙地带着它（用户看到的症状是
   "我没说这个啊"）。现在结构上只有一条路：``TurnRequest.human_feedback`` →
   ``TaskContext.human_feedback``，且上下文**每轮新建**。
2. **``src.agents`` 的公开名字必须惰性**：没装 SDK 时导入也不能炸，
   否则连"启动期拒绝启动并给出可执行报错"这件事都做不到。
3. **观测出口绝不能成为故障源**（指标组件把业务带崩是最讽刺的一种失败）。
4. **已删除的分诊 / 降级原语不许悄悄回来** —— 它们回来时，代码看起来在
   处理某种情况，而那种情况早已不存在，读代码的人会据此做出错误判断。
"""

from __future__ import annotations

import asyncio

import pytest


# --------------------------------------------------------------------------- #
# 人工打回的意见：只走本次运行的参数
# --------------------------------------------------------------------------- #
def test_review_feedback_reaches_the_run(monkeypatch):
    """审核人写的字必须进到**本次运行**的上下文里。

    少这一步的症状很具体：审核人写了"开头太慢"，模型完全没看到，照样原样交付，
    而链路上一路正常、没有任何报错。
    """
    from src import turn
    from src.agents import runner

    seen: dict = {}

    async def fake_run(agent, *, prompt, context, session_id, resume=None,
                       on_text=None, **kw):
        seen["feedback"] = context.human_feedback
        seen["prompt"] = prompt
        return runner.TaskOutcome(answer="改好的", attempts=1, final_agent="f",
                                  passed=True, exit_reason="passed")

    monkeypatch.setattr(runner, "run_agent", fake_run)
    asyncio.run(turn.run_turn(
        turn.TurnRequest(prompt="原话", thread_id="t-fb", human_feedback="开头太慢"),
        context=turn.build_context(tenant=""),
    ))

    assert seen["feedback"] == "开头太慢", "打回意见没送到本次运行（审核人的字白写了）"
    assert seen["prompt"] == "原话"


def test_context_is_built_per_request_so_opinions_do_not_leak():
    """上下文必须**每轮新建**：上一轮的意见不能跟着下一轮走。

    它现在是普通 dataclass，看起来"当然不会泄漏"；但一旦有人为了省事把它缓存成
    模块级单例，这条立刻变红 —— 而这正是最容易发生的那种"优化"。
    """
    from src import turn

    a = turn.build_context(tenant="acme")
    a.human_feedback = "上一轮的意见"
    b = turn.build_context(tenant="acme")

    assert b is not a
    assert b.human_feedback == ""


# --------------------------------------------------------------------------- #
# 惰性导入：没装 SDK 也不能让导入炸
# --------------------------------------------------------------------------- #
def test_agents_package_is_importable_without_eager_sdk_imports():
    """``src.agents`` 的公开名字必须是**惰性**的。

    若 ``__init__.py`` 里 eager import runner / tools，没装 ``openai-agents`` 时
    连 `app.main` 的启动期硬校验都跑不到（它在 import 阶段就崩，报的是一个与
    "没装依赖"无关的错）。
    """
    import src.agents as pkg

    assert pkg.TaskContext.__name__ == "TaskContext"    # 惰性取到真对象
    assert pkg.is_ready() in (True, False)              # 无 SDK 时也只是 False
    with pytest.raises(AttributeError):
        _ = pkg.this_name_does_not_exist


# --------------------------------------------------------------------------- #
# 观测出口
# --------------------------------------------------------------------------- #
def test_observability_is_a_noop_without_a_sink():
    """没注入 sink 时链路层的上报必须是空操作，且**绝不抛异常**。"""
    from src import observability

    saved = observability.get_sink()
    observability.reset_sink()
    try:
        observability.counter("x_total")
        observability.histogram("y_seconds", 1.0)
        observability.gauge("z", 1)
        assert observability.get_sink() is None
    finally:
        observability.set_sink(saved)


def test_observability_swallows_sink_errors():
    """sink 自己抛异常时也不能穿透到业务代码。"""
    from src import observability

    class _Boom:
        def counter(self, *a, **k):
            raise RuntimeError("sink down")

        def histogram(self, *a, **k):
            raise RuntimeError("sink down")

        def gauge(self, *a, **k):
            raise RuntimeError("sink down")

    saved = observability.get_sink()
    observability.set_sink(_Boom())
    try:
        observability.counter("x_total")      # 不该抛
    finally:
        observability.set_sink(saved)


# --------------------------------------------------------------------------- #
# 墓碑：已删除的分诊 / 降级原语不许回来
# --------------------------------------------------------------------------- #
def test_removed_routing_primitives_stay_removed():
    """图、分诊、降级开关、评审分级轮次：**一条都不许回来**。

    为什么这条值得留：这些东西回归时不会报错，只会让代码"看起来在处理某种
    情况"—— 而那种情况已经不存在了（没有第二套作答链路，也没有运行期降级）。
    读代码的人会据此以为"服务在缺 Key 时会自己退回另一条路"，从而把部署配错
    当成"降级了而已"。
    """
    import importlib

    import app.main as app_main
    from app import config
    from src.agents.context import AgentStats

    # 图与停机容器模块本身不存在
    for name in ("src.graph", "src.review_gate"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(name)

    # 接口层不再持有图 / 停机容器，也不再有"分诊结果"这种东西
    for attr in ("graph", "review_gate", "_AGENT_UNAVAILABLE_REASONS",
                 "_record_agent_route", "_AGENT_CLASSIFY_FAILED"):
        assert not hasattr(app_main, attr), f"app.main 上又出现了 {attr}"

    # 能力开关：只有"启动期硬校验"一条判据，没有运行期开关
    assert not hasattr(config, "AGENTS_ENABLED")

    # 统计里不再有"走了哪条链路"的两个字段。
    # ⚠️ `review_rounds` **必须留着**：它数的不是"分级评审跑了几轮"（那个循环已经
    # 随评审改成 `as_tool` 一起删掉），而是"评审工具被模型叫了几次" ——
    # `attempts` 恒为 1 判不出返工，`review_rounds > 1` 才是唯一的返工信号。
    # 曾经把它当图时代残留删掉，结果 `test_agents_pipeline.py` 33 条当场全红。
    # 所以这条断言是**反向**的：它必须存在。
    stats = AgentStats()
    assert not hasattr(stats, "route") and not hasattr(stats, "route_reason")
    assert hasattr(stats, "review_rounds")
