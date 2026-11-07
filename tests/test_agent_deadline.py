"""一次运行的**墙钟上限**（``src/agents/deadline.py``）。

为什么需要单独一组
----------------
在这个模块之前，"这次运行最多跑多久"是**隐式**的：12 圈 ×（模型 30s + 工具 20s）。
它只存在于"把三个各自独立的值乘起来"之后 —— 既配不出来（想给某类任务封 3 分钟
做不到），也说不清（看到"一次请求跑了 8 分钟"得自己反推是哪一项放行的）。

三条容易写错、且错了都不报错的地方
--------------------------------
1. **起点必须真的被写**。``mark_start`` 少调一次，``elapsed`` 恒为 0 ⇒ 闸门永不触发，
   而所有代码看起来都正常。
2. **判据是 ``>=``**，与预算同一口径：正好到点就该停。
3. **必须排在裁剪之前**。排在后面时，到点的那一次调用会先跑一遍窗口压缩（要调一次
   模型、还是最贵的那一次）再被扔掉 —— 钱花了，事没成。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("agents", reason="openai-agents 未安装 ⇒ agent 链路不存在，本组无从验证")

from agents.items import Usage  # noqa: E402

from src import observability  # noqa: E402
from src.agents import deadline, fallback, runner  # noqa: E402
from src.agents.context import TaskContext  # noqa: E402
from src.agents.runner import ENTRY_AGENT, run_agent  # noqa: E402
from tests._agents_testkit import ScriptedModel, decide, make_ctx  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_session(monkeypatch):
    monkeypatch.setattr(runner, "session_for", lambda _sid: None)


def _ctx_aged(seconds: float) -> TaskContext:
    """一个"已经跑了 N 秒"的上下文（直接写起点，不走 mark_start）。"""
    ctx = TaskContext()
    ctx.started_at = time.monotonic() - seconds
    return ctx


# ---------------------------------------------------------------------------
# 一、判据本身
# ---------------------------------------------------------------------------
def test_no_limit_never_trips(monkeypatch):
    """默认 0 = 不限。配错成"看起来合理的小数字"会静默截断长任务，所以默认必须不限。"""
    monkeypatch.delenv("AGENT_RUN_TIMEOUT_SECONDS", raising=False)
    deadline.check(_ctx_aged(10_000))


def test_under_the_limit_passes(monkeypatch):
    monkeypatch.setenv("AGENT_RUN_TIMEOUT_SECONDS", "60")
    deadline.check(_ctx_aged(59))


def test_at_the_limit_trips(monkeypatch):
    """判据是 ``>=``：正好到点就停，不等下一圈。

    ⚠️ 边界**不能拿真钟去撞**（2026-11-07 远端 CI run 26 实测：这条 1/2 概率红，
    报 ``DID NOT RAISE``，而本机怎么跑都是绿的）。

    原因在浮点，不在判据：``_ctx_aged(60)`` 写的是 ``started_at = monotonic() - 60``，
    这个减法按 ~1e-10 量级舍入；``elapsed()`` 再算一次 ``monotonic() - started_at``。
    只要两次 ``monotonic()`` 落在**同一刻度**（粗粒度时钟、或执行够快），
    结果就是 ``60 ± 舍入误差`` —— 落在 60 下方时判据不触发。
    本机时钟是纳秒级、两次调用差 100ns 量级，差值永远把这点误差盖掉，
    所以本地永远撞不到，只有换了机器才现形。

    ⇒ 这里把 ``elapsed`` 钉成精确的 60.0，只测 ``check`` 的**比较**；
      ``elapsed()`` 自己的算术由 ``test_mark_start_and_elapsed`` 覆盖。
      两件事分开之后，这条判据与机器时钟的粒度无关。
    """
    monkeypatch.setenv("AGENT_RUN_TIMEOUT_SECONDS", "60")
    monkeypatch.setattr(deadline, "elapsed", lambda _ctx: 60.0)
    with pytest.raises(deadline.AgentStoppedByDeadline) as exc:
        deadline.check(TaskContext())
    assert exc.value.limit_seconds == 60
    assert exc.value.elapsed_seconds == 60.0


def test_just_under_the_limit_passes(monkeypatch):
    """边界的另一侧：**差一点**就不停。

    与上一条配对：只测"到点停"的话，把判据写成 ``<=``（差一点也停）同样能过。
    两条一起才钉住 ``>=`` 这**一侧**的语义。
    """
    monkeypatch.setenv("AGENT_RUN_TIMEOUT_SECONDS", "60")
    monkeypatch.setattr(deadline, "elapsed", lambda _ctx: 59.999)
    deadline.check(TaskContext())


def test_the_stop_is_counted(monkeypatch):
    seen: list[str] = []
    saved = observability.get_sink()

    class _Sink:
        def counter(self, name, amount=1, **labels):  # noqa: ANN001, ANN003
            seen.append(name)

        def histogram(self, name, value, **labels):  # noqa: ANN001, ANN003
            pass

        def gauge(self, name, value, **labels):  # noqa: ANN001, ANN003
            pass

    observability.set_sink(_Sink())
    try:
        monkeypatch.setenv("AGENT_RUN_TIMEOUT_SECONDS", "1")
        with pytest.raises(deadline.AgentStoppedByDeadline):
            deadline.check(_ctx_aged(5))
    finally:
        observability.set_sink(saved)
    assert seen == ["agent_run_deadline_stop_total"]


def test_mark_start_and_elapsed():
    """起点真的被写进去、elapsed 真的在走。

    这条看着像废话，但它守的是"整个闸门有没有接上电源"：``mark_start`` 写成空函数
    时，上面所有用例仍然是绿的（它们自己写 started_at），而生产上永不触发。
    """
    ctx = TaskContext()
    assert deadline.elapsed(ctx) == 0.0

    deadline.mark_start(ctx)
    assert ctx.started_at > 0
    assert 0.0 <= deadline.elapsed(ctx) < 1.0


def test_elapsed_without_a_start_is_zero():
    """没记过起点按 0 算 ⇒ 不触发。方向安全：宁可少拦一次，也不要拿错误起点拦。"""
    assert deadline.elapsed(SimpleNamespace()) == 0.0


# ---------------------------------------------------------------------------
# 二、闸门位置（排在裁剪之前）
# ---------------------------------------------------------------------------
def test_the_gate_runs_before_trimming(monkeypatch):
    """★★ 到点时必须**在裁剪/压缩之前**返回，不能先花钱再报超时。

    为什么这条特别值得守：窗口压缩是长任务里最贵的一笔（要把老历史整段读一遍）。
    闸门挪到裁剪之后就变成"每次超时都先压一遍再扔掉"，账单上完全看不出来 ——
    只有"超时的时候成本特别高"这个模糊现象。
    """
    monkeypatch.setenv("AGENT_RUN_TIMEOUT_SECONDS", "1")
    called: list[str] = []

    async def _trim(data):
        called.append("trim")
        return data

    monkeypatch.setattr(runner, "trim_model_input", _trim)
    data = SimpleNamespace(context=_ctx_aged(5), model_data=object())

    with pytest.raises(deadline.AgentStoppedByDeadline):
        asyncio.run(runner.model_input_filter(data))
    assert called == [], "裁剪不该被走到"


# ---------------------------------------------------------------------------
# 三、端到端：它必须是**自己的**停因
# ---------------------------------------------------------------------------
def test_a_deadline_stop_has_its_own_exit_reason(tmp_path, monkeypatch):
    """★★ 到点 ⇒ ``exit_reason="run_timeout"``，不是 ``error``。

    ``run_agent`` 末尾有 ``except Exception`` 兜底。专用异常排在它后面就会被吞成
    "崩溃" ⇒ 运维看到崩溃率上升会去查上游，而真正该做的是查"为什么这么慢"。
    """
    monkeypatch.setenv("AGENT_RUN_TIMEOUT_SECONDS", "1")
    # 起点挪到过去，让第一次模型调用之前就超时（mark_start 会覆盖，所以换掉它）。
    monkeypatch.setattr(
        deadline, "mark_start", lambda ctx: setattr(ctx, "started_at", time.monotonic() - 5)
    )
    model = ScriptedModel([decide("不该走到这里。")], usage=Usage(input_tokens=1, output_tokens=1))
    ctx = make_ctx(tmp_path)

    out = asyncio.run(run_agent(ENTRY_AGENT, prompt="跑很久的任务", context=ctx, model=model))

    assert out.exit_reason == "run_timeout"
    assert out.error == "", "不能被当成异常收尾（那样会丢掉'到点'这个理由）"
    assert out.answer == "", "在注入口抛出 ⇒ 没有 result ⇒ 没有答复正文"
    assert out.fallback is not None
    assert out.fallback.reason == "run_timeout"
    assert out.fallback.outcome == fallback.FAILED


def test_run_agent_marks_the_start(tmp_path):
    """``run_agent`` 必须自己记起点。

    去掉那一行之后，单元用例全绿（它们手写 started_at），而生产上闸门永久失效。
    """
    model = ScriptedModel([decide("答复。")])
    ctx = make_ctx(tmp_path)

    asyncio.run(run_agent(ENTRY_AGENT, prompt="你好", context=ctx, model=model))

    assert ctx.started_at > 0


def test_a_normal_run_is_untouched_by_the_gate(tmp_path, monkeypatch):
    """配了时限、但没到 ⇒ 一切照旧（不能因为"加了个闸门"就让正常路径多一种分支）。"""
    monkeypatch.setenv("AGENT_RUN_TIMEOUT_SECONDS", "3600")
    model = ScriptedModel([decide("答复。")])
    ctx = make_ctx(tmp_path)

    out = asyncio.run(run_agent(ENTRY_AGENT, prompt="你好", context=ctx, model=model))

    # 没调评审工具 ⇒ ``unreviewed``（不是 ``passed``：那一档要真的评过）。
    assert out.exit_reason == "unreviewed"
    assert out.answer == "答复。"
