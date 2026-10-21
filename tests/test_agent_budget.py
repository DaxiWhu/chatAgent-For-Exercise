"""一次运行内的 token 预算（``src/agents/budget.py``）。

这一组守的是"预算到底有没有在那一拍上生效"，而不是"有没有一个配置项"。四个
**不报错**的失效形态，每一条都能让预算变成摆设：

1. **判据挂在钩子上** —— 官方对 hook 抛异常的处理没有验证过；真失效时表现是
   "预算配了、从来没停过"，日志与指标干干净净。所以判据必须在 input filter 上
   （第 3 级刹车走的就是那条已验证的通路）。
2. **累计被塞在观察开关后面** —— ``on_llm_end`` 开头有一句 ``if not steps_enabled(): return``。
   把 token 累计写在它下面，``AGENT_TOOL_STEPS=0`` 时成本统计就永远不涨。
3. **边界写成 ``>``** —— 正好花满预算时还允许再跑一圈，而那一圈往往是最贵的
   （历史最长、工具返回最多）。判据必须是 ``>=``。
4. **上游不回 usage** —— 这是它唯一的静默失效方式：累计恒为 0，预算永不触发。
   必须有单独的计数把它暴露出来，否则"预算很宽松"和"预算根本没量到"一模一样。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip(
    "agents", reason="openai-agents 未安装 ⇒ agent 链路会静默降级，本组无从验证"
)

from agents.items import Usage  # noqa: E402

from src import observability  # noqa: E402
from src.agents import budget, runner  # noqa: E402
from src.agents.context import AgentStats, TaskContext  # noqa: E402
from src.agents.hooks import AGENT_HOOKS  # noqa: E402
from src.agents.runner import ENTRY_AGENT, run_agent  # noqa: E402
from tests._agents_testkit import (  # noqa: E402
    ScriptedModel,
    call,
    decide,
    make_ctx,
)


class RecordingSink:
    """与 ``test_agent_tool_steps`` 同形：替身挂在 sink 层，不换掉分发本身。"""

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
def _no_real_session(monkeypatch):
    """端到端用例不经真会话后端（CI 没有 PG）。"""
    monkeypatch.setattr(runner, "session_for", lambda _sid: None)


# --------------------------------------------------------------------------- #
# 一、闸门本身（纯函数）
# --------------------------------------------------------------------------- #
def test_no_budget_means_no_gate(monkeypatch):
    """不配预算（``0``）⇒ 一律放行。

    默认必须是"不限"：默认值配小了会大面积截断正常长任务，而症状是
    "模型最近答得敷衍"—— 那比"账单偏高"更难归因。
    """
    monkeypatch.delenv("AGENT_RUN_TOKEN_BUDGET", raising=False)
    ctx = TaskContext()
    ctx.stats.input_tokens = 10**9
    ctx.stats.output_tokens = 10**9

    budget.check(ctx)  # 不抛

    assert budget.run_budget() == 0
    assert budget.remaining(ctx) == -1, "没预算时应返回 -1，而不是 0（0 会被读成'刚好用完'）"


def test_the_gate_fires_at_the_budget_not_after_it(monkeypatch):
    """★ 边界是 ``>=``：正好花满就停。

    ``>`` 会允许再花一整圈，而那一圈正是最贵的一圈（历史最长、工具返回最多）——
    于是"预算 10 万"的实际开销会稳定超过 10 万，且超出的部分不可控。
    """
    monkeypatch.setenv("AGENT_RUN_TOKEN_BUDGET", "100")
    ctx = TaskContext()
    ctx.stats.input_tokens = 99
    budget.check(ctx)  # 还差 1，放行

    ctx.stats.input_tokens = 60
    ctx.stats.output_tokens = 40  # 合计正好 100
    with pytest.raises(budget.AgentStoppedByBudget) as exc:
        budget.check(ctx)

    assert exc.value.used == 100
    assert exc.value.budget == 100


def test_the_gate_counts_input_and_output_together(monkeypatch):
    """只算输入会漏掉一半成本：输出（尤其推理模型的思考）也是按量计费的。"""
    monkeypatch.setenv("AGENT_RUN_TOKEN_BUDGET", "100")
    ctx = TaskContext()
    ctx.stats.output_tokens = 120

    assert budget.used_of(ctx) == 120
    with pytest.raises(budget.AgentStoppedByBudget):
        budget.check(ctx)


def test_a_stop_is_broadcast_once(sink, monkeypatch):
    """停一次记一条 —— 它上游是"每次运行最多触发一次"（抛出去运行就结束了）。"""
    monkeypatch.setenv("AGENT_RUN_TOKEN_BUDGET", "10")
    ctx = TaskContext()
    ctx.stats.input_tokens = 50

    with pytest.raises(budget.AgentStoppedByBudget):
        budget.check(ctx)

    assert len(sink.counters("agent_run_budget_stop_total")) == 1


# --------------------------------------------------------------------------- #
# 二、位置：必须在裁剪之前
# --------------------------------------------------------------------------- #
def test_the_gate_runs_before_trimming(monkeypatch):
    """★ 超预算时**不该**先分词裁一遍再扔结果。

    这不只是省钱：裁剪本身要调分词器，而超预算的运行往往正是历史最长的那些 ——
    在最贵的请求上多做一次全量分词，恰好是最不该做的。更实际的一点是顺序守卫：
    如果有人把 ``budget.check`` 挪到 ``trim_model_input`` 之后，行为看起来完全一样
    （照样抛），只有"裁剪有没有被调用"能分辨。
    """
    monkeypatch.setenv("AGENT_RUN_TOKEN_BUDGET", "10")
    called: list[str] = []

    async def _trim(data):
        called.append("trim")
        return data

    monkeypatch.setattr(runner, "trim_model_input", _trim)

    ctx = TaskContext()
    ctx.stats.input_tokens = 999
    data = SimpleNamespace(input=[], context=ctx)

    with pytest.raises(budget.AgentStoppedByBudget):
        asyncio.run(runner.model_input_filter(data))

    assert called == [], "超预算时仍然先裁剪了 —— 闸门被挪到了裁剪之后"


# --------------------------------------------------------------------------- #
# 三、累计的接线（最容易漏的那一处）
# --------------------------------------------------------------------------- #
def test_tokens_are_counted_even_when_step_logging_is_off(sink, monkeypatch):
    """★ 关掉动作清单之后，token 仍然要累计。

    ``on_llm_end`` 开头有一句 ``if not steps_enabled(): return``。token 累计写在它
    下面就等于"成本统计挂在一个观察开关后面"—— 关掉步骤记录的人会顺手关掉预算，
    而两件事毫无关系。这条用例是这个位置上唯一的守卫：把那两句换个顺序，
    功能测试全绿，只有这里会红。
    """
    monkeypatch.setenv("AGENT_TOOL_STEPS", "0")
    ctx = TaskContext()
    response = SimpleNamespace(usage=Usage(input_tokens=70, output_tokens=30))

    asyncio.run(AGENT_HOOKS.on_llm_end(SimpleNamespace(context=ctx), None, response))

    assert ctx.stats.input_tokens == 70
    assert ctx.stats.output_tokens == 30
    assert ctx.stats.tool_calls == 0, "步骤记录确实被关掉了（否则这条用例测不到那个顺序）"


def test_a_gateway_without_usage_leaves_the_counter_at_zero(monkeypatch):
    """网关不回 usage ⇒ 累计是 0（不报错、不猜）。

    按 0 处理的代价是"这一次不会被拦"，方向是安全的：宁可少拦一次，也不要在量不到
    的时候把一次正常长任务掐断。但这件事必须**可见** —— 见下一条。
    """
    ctx = TaskContext()
    asyncio.run(AGENT_HOOKS.on_llm_end(SimpleNamespace(context=ctx), None, SimpleNamespace()))

    assert budget.used_of(ctx) == 0


# --------------------------------------------------------------------------- #
# 四、静默失效的可见性
# --------------------------------------------------------------------------- #
def test_unmeasured_tokens_are_reported(sink, monkeypatch):
    """★ 配了预算、跑过模型、却一个 token 都没量到 ⇒ 单独计数。

    没有这条计数，"预算配了但没生效"（网关不回 usage）与"预算很宽松、从来没超"
    在读板上完全一样 —— 而前者意味着这道闸门根本不存在。
    """
    monkeypatch.setenv("AGENT_RUN_TOKEN_BUDGET", "100000")
    stats = AgentStats(model_calls=3, input_tokens=0, output_tokens=0)

    budget.report(stats, agent="frontline_agent")

    assert len(sink.counters("agent_run_tokens_unmeasured_total")) == 1
    assert sink.counters("agent_run_tokens_total") == []


def test_a_measured_run_is_not_reported_as_unmeasured(sink, monkeypatch):
    """量到了就不许报"没量到" —— 否则这条计数会常态为真，失去意义。"""
    monkeypatch.setenv("AGENT_RUN_TOKEN_BUDGET", "100000")
    budget.report(AgentStats(model_calls=1, input_tokens=5, output_tokens=7))

    assert sink.counters("agent_run_tokens_unmeasured_total") == []
    assert [(e[1], e[2], e[3]) for e in sink.counters("agent_run_tokens_total")] == [
        ("agent_run_tokens_total", 5, {"kind": "input", "agent": ""}),
        ("agent_run_tokens_total", 7, {"kind": "output", "agent": ""}),
    ]


# --------------------------------------------------------------------------- #
# 五、端到端：预算烧穿要成为**自己的**停因
# --------------------------------------------------------------------------- #
def test_a_budget_stop_has_its_own_exit_reason(tmp_path, monkeypatch):
    """★★ 预算烧穿 ⇒ ``exit_reason="token_budget"``，不是 ``error``。

    这是整条链路里最重要的一条：``run_agent`` 末尾有 ``except Exception`` 的兜底，
    专用异常只要排在它后面，就会被吞成"崩溃"⇒ 运维看到崩溃率上升会去查上游，
    而真正该做的是调预算、或查出那个把上下文撑大的步骤。

    场景：第一圈调一次工具（花掉 200 token），第二圈之前闸门到线 ⇒ 停。
    """
    monkeypatch.setenv("AGENT_RUN_TOKEN_BUDGET", "50")
    model = ScriptedModel(
        [call("research", {"question": "限流是怎么算的？"}, "c1"), decide("答复。")],
        usage=Usage(input_tokens=120, output_tokens=80),
    )
    ctx = make_ctx(tmp_path)

    out = asyncio.run(run_agent(ENTRY_AGENT, prompt="限流怎么算的？", context=ctx, model=model))

    assert out.exit_reason == "token_budget"
    assert out.error == "", "不能被当成异常收尾（那样会丢掉'预算'这个理由）"
    assert out.fallback is not None
    assert out.fallback.reason == "token_budget"
    assert out.fallback.outcome == "failed", "没有产出 ⇒ 判定表第⑤行"


def test_a_run_under_budget_finishes_normally(tmp_path, monkeypatch):
    """预算没到 ⇒ 与不配预算完全一致（这是"配了不出事"的那一半）。"""
    monkeypatch.setenv("AGENT_RUN_TOKEN_BUDGET", "100000")
    model = ScriptedModel([decide("答复。")], usage=Usage(input_tokens=10, output_tokens=10))
    ctx = make_ctx(tmp_path)

    out = asyncio.run(run_agent(ENTRY_AGENT, prompt="你好", context=ctx, model=model))

    assert out.exit_reason == "unreviewed"
    assert out.answer == "答复。"


def test_each_run_reports_only_its_own_tokens(sink, tmp_path, monkeypatch):
    """★ 上报必须用"本次运行切片"：一次请求可能跑两个节点。

    用累计值上报，第二个节点会把第一个节点的量也算进去 —— 看板上的"每次运行成本"
    随着拓扑变长而翻倍，而两次读数各自都"看起来正常"。
    """
    monkeypatch.setenv("AGENT_RUN_TOKEN_BUDGET", "100000")
    ctx = make_ctx(tmp_path)

    first = ScriptedModel([decide("第一段。")], usage=Usage(input_tokens=10, output_tokens=1))
    asyncio.run(run_agent(ENTRY_AGENT, prompt="一", context=ctx, model=first))
    second = ScriptedModel([decide("第二段。")], usage=Usage(input_tokens=20, output_tokens=2))
    asyncio.run(run_agent(ENTRY_AGENT, prompt="二", context=ctx, model=second))

    amounts = [e[2] for e in sink.counters("agent_run_tokens_total") if e[3]["kind"] == "input"]
    assert amounts == [10, 20], f"切片口径错了（第二个运行报的是累计值）：{amounts}"
