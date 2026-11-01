"""上游前缀缓存的**可见性**（``src/agents/context.py`` / ``hooks.py`` / ``budget.py``）。

这一组守的不是"缓存有没有开" —— 那是网关的事，而且默认就开着（DeepSeek 的硬盘
缓存不用改代码）。守的是**看得见**：命中量有没有被记下来、有没有被记到对的地方。

四个不报错的失效形态，每一条都能让"缓存省钱"变成一句无法核实的话：

1. **记了 input 忘了 cached** —— 两个数在同一个钩子里，拆开写必然漂；漂的表现
   只是命中率偏低，没有任何东西会报错。
2. **把命中量并进成本口径** —— ``agent_run_tokens_total{kind="input"}`` 是成本曲线
   的唯一来源。命中量是它的**子集**，并进去以后 ``sum(rate(...))`` 就是双倍输入，
   而看板上看不出多出来的那一半是什么。
3. **网关不回字段时崩掉** —— 老网关 / 本地模型没有 ``input_tokens_details``。
   这里一旦抛异常，后果不是"少一个指标"，而是**每次模型调用之后的钩子失败**。
4. **旁路漏记** —— 压缩要把老历史整段读一遍，是长任务里最贵的一笔。它有没有命中
   缓存，直接决定"压缩贵不贵"。只记主链路会让成本口径系统性偏移。

⚠️ 与 ``test_agent_budget.py`` 的分工：那边守"预算在不在那一拍上触发"，这边守
"成本量得对不对"。同一份 ``AgentStats``，问的是两个不同的问题。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip(
    "agents", reason="openai-agents 未安装 ⇒ agent 链路会静默降级，本组无从验证"
)

from src import observability  # noqa: E402
from src.agents import budget, runner  # noqa: E402
from src.agents.context import AgentStats, TaskContext  # noqa: E402
from src.agents.hooks import AGENT_HOOKS  # noqa: E402
from src.agents.runner import ENTRY_AGENT, run_agent  # noqa: E402
from tests._agents_testkit import (  # noqa: E402
    ScriptedModel,
    decide,
    make_ctx,
)


class RecordingSink:
    """与 ``test_agent_budget`` 同形：替身挂在 sink 层，不换掉分发本身。"""

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

    def histograms(self, name: str) -> list[tuple]:
        return [e for e in self.events if e[0] == "histogram" and e[1] == name]


@pytest.fixture
def sink():
    s = RecordingSink()
    observability.set_sink(s)
    yield s
    observability.set_sink(None)


def _usage(inp: int, out: int, cached: int | None = None) -> SimpleNamespace:
    """造一个 usage。

    ``cached=None`` ⇒ **不带** ``input_tokens_details``（老网关的形态）。
    ⚠️ 刻意不用官方的 ``Usage``：本组要证明的是"读不到字段也不炸"，而官方
    ``Usage`` 的 ``__post_init__`` 会把缺字段补成 0 —— 用它永远到不了那条分支。
    """
    if cached is None:
        return SimpleNamespace(input_tokens=inp, output_tokens=out)
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        input_tokens_details=SimpleNamespace(cached_tokens=cached),
    )


def _usage_obj(inp: int, out: int, cached: int):
    """造一个**官方** ``Usage``（端到端用例要真走 SDK 的钩子链）。"""
    from agents.items import Usage

    # ⚠️ ``cache_write_tokens`` 是必填（pydantic 定义），漏了直接 ValidationError ——
    # 这个字段本项目不用，但构造对象时必须给。
    return Usage(
        input_tokens=inp,
        output_tokens=out,
        input_tokens_details={"cached_tokens": cached, "cache_write_tokens": 0},
    )


def _end(ctx: TaskContext, usage) -> None:
    """走一次 ``on_llm_end``。

    ⚠️ 钩子的第三个参数是**响应**（``response.usage``），不是 usage 本身。
    直接传 usage 会静默走到"没有 usage"那条分支 —— 三个断言全变成 0，
    看起来像实现错了，其实是测试喂错了。
    """
    asyncio.run(
        AGENT_HOOKS.on_llm_end(SimpleNamespace(context=ctx), None, SimpleNamespace(usage=usage))
    )


# --------------------------------------------------------------------------- #
# 一、钩子侧：记在同一个地方
# --------------------------------------------------------------------------- #
def test_the_hit_count_is_collected_next_to_the_input_count():
    """★ 两个数必须在同一次读取里累加。

    拆开写的漂移不会报错，只会让命中率偏低 —— 而"偏低"与"缓存真的不灵"
    在读板上是同一个样子。
    """
    ctx = TaskContext()
    _end(ctx, _usage(100, 10, cached=60))

    assert ctx.stats.input_tokens == 100
    assert ctx.stats.cached_input_tokens == 60


def test_hits_accumulate_across_model_calls():
    """一次运行里会调好几次模型，每次都要加进去。"""
    ctx = TaskContext()
    _end(ctx, _usage(100, 10, cached=60))
    _end(ctx, _usage(200, 20, cached=150))

    assert ctx.stats.input_tokens == 300
    assert ctx.stats.cached_input_tokens == 210


def test_a_gateway_without_the_field_is_not_an_error():
    """★ 没有 ``input_tokens_details`` 时按 0 算，**不许抛**。

    这里抛异常的后果不是"少一个指标"：钩子是**每次模型调用之后**都会走的，
    失败会让整次运行落到 ``exit_reason="error"``——一个纯观测字段就能把作答打掉。
    """
    ctx = TaskContext()
    _end(ctx, _usage(100, 10))

    assert ctx.stats.input_tokens == 100
    assert ctx.stats.cached_input_tokens == 0


def test_a_null_detail_value_counts_as_zero():
    """字段存在但为 ``null``（openai SDK 的 model_construct 会产生）也按 0。"""
    ctx = TaskContext()
    usage = SimpleNamespace(
        input_tokens=10, output_tokens=1, input_tokens_details=SimpleNamespace(cached_tokens=None)
    )
    _end(ctx, usage)

    assert ctx.stats.cached_input_tokens == 0


def test_a_negative_hit_count_is_dropped():
    """负数按 0 —— 它只会出现在上游写错的时候，但会被算进比率。"""
    ctx = TaskContext()
    _end(ctx, _usage(10, 1, cached=-5))

    assert ctx.stats.cached_input_tokens == 0


# --------------------------------------------------------------------------- #
# 二、比率：分母是 input，且截断到 1
# --------------------------------------------------------------------------- #
def test_the_ratio_uses_input_tokens_as_the_denominator():
    stats = AgentStats(input_tokens=200, cached_input_tokens=50)

    assert stats.cache_hit_rate == pytest.approx(0.25)


def test_the_ratio_is_capped_at_one():
    """命中数不可能超过输入数；上游算错时也不能把比率撑到 1 以上。

    为什么非要截断：这个数会进直方图。1.4 这种值会把分位数整体拉歪，而它只在
    "上游字段自相矛盾"时出现 —— 那种时候更应该表现为"比率看起来怪"，而不是
    "看板数值不可解释"。
    """
    stats = AgentStats(input_tokens=100, cached_input_tokens=140)

    assert stats.cache_hit_rate == 1.0


def test_no_input_means_the_ratio_is_zero():
    """没有输入 token 时比率是 0，不是除零、也不是 1。

    ⚠️ 此时"0"没有意义 —— 读的人应当先看 ``input_tokens`` 是否为 0
    （= 上游压根没回 usage），而不是据此判断缓存失效。
    """
    assert AgentStats().cache_hit_rate == 0.0


def test_the_stats_dict_carries_both_numbers_and_the_ratio():
    """``as_dict`` 进日志 / 评测报告，漏了它等于这两个数只活在内存里。"""
    d = AgentStats(input_tokens=200, cached_input_tokens=50).as_dict()

    assert d["cached_input_tokens"] == 50
    assert d["cache_hit_rate"] == pytest.approx(0.25)


# --------------------------------------------------------------------------- #
# 三、切片：每次运行只报自己的那一份
# --------------------------------------------------------------------------- #
def test_each_run_reports_only_its_own_cache_hits(sink, tmp_path):
    """★★ 命中量必须跟着"本次运行切片"一起减掉 ``before``。

    这是本组最贵的一条：``dataclasses.replace`` 会把**没点名的字段原样搬过去**。
    漏切 ``cached_input_tokens`` 时，第二次运行报出的命中量是**累计值**，而同一份
    切片里的 ``input_tokens`` 是本次增量 —— 命中率于是虚高，且会被
    ``cache_hit_rate`` 的截断悄悄压成 100%。零报错、数字还很好看。

    （这条不是想出来的：加完统计后跑真链路才发现 ``before`` 元组只有 5 项。）
    """
    ctx = make_ctx(tmp_path)

    first = ScriptedModel([decide("第一段。")], usage=_usage_obj(100, 10, 60))
    asyncio.run(run_agent(ENTRY_AGENT, prompt="一", context=ctx, model=first))
    second = ScriptedModel([decide("第二段。")], usage=_usage_obj(200, 20, 150))
    asyncio.run(run_agent(ENTRY_AGENT, prompt="二", context=ctx, model=second))

    hits = [e[2] for e in sink.counters("agent_cache_hit_tokens_total")]
    assert hits == [60, 150], f"第二次报的是累计值而不是本次增量：{hits}"
    ratios = [round(e[2], 4) for e in sink.histograms("agent_cache_hit_ratio")]
    assert ratios == [pytest.approx(0.6), pytest.approx(0.75)]


def test_the_bypass_slice_keeps_the_hit_column():
    """旁路的 ``cached`` 也要跟着切；只切 input/output 会让它恒为 0。"""
    before = {"compaction": {"input": 100, "output": 10, "cached": 80}}
    current = {"compaction": {"input": 300, "output": 30, "cached": 260}}

    sliced = runner._bypass_slice(before, current)

    assert sliced["compaction"] == {"input": 200, "output": 20, "cached": 180}


def test_a_bypass_source_that_only_gained_hits_is_not_dropped():
    """输入没变、命中变了也要留下来 —— 上游重算缓存时会出现这种形态。"""
    before = {"compaction": {"input": 100, "output": 10, "cached": 0}}
    current = {"compaction": {"input": 100, "output": 10, "cached": 90}}

    assert runner._bypass_slice(before, current)["compaction"]["cached"] == 90


# --------------------------------------------------------------------------- #
# 四、上报：独立的指标名
# --------------------------------------------------------------------------- #
def test_the_hit_tokens_get_their_own_counter(sink):
    budget.report(AgentStats(model_calls=1, input_tokens=200, cached_input_tokens=50))

    hits = sink.counters("agent_cache_hit_tokens_total")
    assert [(e[2], e[3]) for e in hits] == [(50, {"agent": ""})]


def test_the_hit_tokens_are_not_folded_into_the_input_counter(sink):
    """★★ 本组最重要的一条：命中量**不许**并进成本口径。

    ``agent_run_tokens_total{kind="input"}`` 的 ``sum(rate(...))`` 就是成本曲线。
    把子集并进去，曲线会变成两倍输入 —— 而且看板上多出来的那一半没有任何标签
    能解释它。这条断言红的时候，别去改断言。
    """
    budget.report(AgentStats(model_calls=1, input_tokens=200, cached_input_tokens=50))

    inputs = [e[2] for e in sink.counters("agent_run_tokens_total") if e[3]["kind"] == "input"]
    assert inputs == [200], "输入成本必须只报一次、且只报总输入，不含命中量"


def test_nothing_is_reported_when_nothing_hit(sink):
    """一次没命中就不发（省掉一条恒为 0 的曲线），但比率照发 —— 0 也要看得见。"""
    budget.report(AgentStats(model_calls=1, input_tokens=200, cached_input_tokens=0))

    assert sink.counters("agent_cache_hit_tokens_total") == []
    assert [e[2] for e in sink.histograms("agent_cache_hit_ratio")] == [0.0]


def test_the_ratio_is_reported_once_per_run(sink):
    """比率按运行记，不是按模型调用记 —— 它是"这一次运行命中得怎么样"。

    按调用记会让长会话里的冷启动被后面的热调用淹没，而冷启动正是成本最高的那一次。
    """
    budget.report(AgentStats(model_calls=5, input_tokens=200, cached_input_tokens=150))

    values = [e[2] for e in sink.histograms("agent_cache_hit_ratio")]
    assert values == [pytest.approx(0.75)]


def test_no_ratio_when_no_tokens_were_measured(sink):
    """一个 token 都没量到时不发比率：0/0 没有意义，发 0.0 会被读成"完全没命中"。"""
    budget.report(AgentStats(model_calls=1, input_tokens=0, cached_input_tokens=0))

    assert sink.histograms("agent_cache_hit_ratio") == []


# --------------------------------------------------------------------------- #
# 四、旁路：记下来，但不进预算
# --------------------------------------------------------------------------- #
def test_bypass_records_its_own_hits():
    """压缩/反思那两次调用各自有命中量，按来源分开记。"""
    stats = AgentStats()
    budget.note_bypass(stats, "compaction", _usage(1000, 100, cached=800))
    budget.note_bypass(stats, "reflection", _usage(200, 20, cached=50))

    assert stats.bypass_tokens["compaction"]["cached"] == 800
    assert stats.bypass_tokens["reflection"]["cached"] == 50


def test_bypass_hits_do_not_change_the_budget_arithmetic():
    """★ 命中量不进 ``used_of``。

    预算是"还能花多少 token"，不是"花了多少钱"。命中量是输入的**子集**，
    加进去等于把同一个 token 数两遍 —— 表现是预算提前烧穿，而账单其实还宽裕。
    """
    used = AgentStats()
    budget.note_bypass(used, "compaction", _usage(1000, 100, cached=800))

    assert budget.used_of(SimpleNamespace(stats=used)) == 1100
    assert used.bypass_input_tokens == 1000


def test_the_main_line_hits_do_not_change_the_budget_either():
    """★ 主链路的命中量同样不进预算。

    上一条只覆盖了旁路，而旁路用例里 ``cached_input_tokens`` 恰好是 0 ——
    于是"把命中量加进 ``used_of``"这个改动在它面前**不会红**（变异验证实测：
    该变异在第一版测试下存活）。这一条把主链路也放进来，那个改动就藏不住了。
    """
    stats = AgentStats(input_tokens=1000, output_tokens=100, cached_input_tokens=800)

    assert budget.used_of(SimpleNamespace(stats=stats)) == 1100, (
        "命中量是输入的**子集**，再加一遍等于同一个 token 数两遍"
    )


def test_an_old_bypass_entry_without_the_cache_key_still_adds_up():
    """老结构（只有 input/output）继续可加 —— 字典里没有 ``cached`` 键不能炸。"""
    stats = AgentStats(bypass_tokens={"reflection": {"input": 10, "output": 2}})
    budget.note_bypass(stats, "reflection", _usage(5, 1, cached=3))

    assert stats.bypass_tokens["reflection"] == {"input": 15, "output": 3, "cached": 3}
