"""旁路模型调用的 token 记账（反思闸门 / 窗口压缩）。

这一组守的是"**账单上的钱有没有全部进账**"，不是一个新指标有没有名字。

为什么这是个真问题
----------------
``AgentStats.input_tokens`` 只由 ``hooks.on_llm_end`` 写 —— 也就是 ``Runner`` 循环里
那几次模型调用。而反思闸门与窗口压缩各自 **bypass 建 client**（超时要独立、也不想
和摘要共用实例），它们完全不出现在那条 usage 里。

后果不是"少报一点"，而是**整类开销从不进预算**：压缩要把被压掉的那段历史整段读
一遍，是长任务里最贵的一笔；反思每触发一次就是一次完整请求。于是"预算 5 万"实际
可能花出 8 万，而看板上那条预算线从来没被碰到过 —— 且所有读数都自洽，看不出漏了。

四件必须同时成立的事，缺一条这个记账就是假的
----------------------------------------
1. 两个旁路都真的**上报**（不是只加了一个字段没人写）；
2. ``used_of`` 把旁路算进预算（只上报不进判据 = 只是看板好看）；
3. 指标按 ``source`` 分开（合成一个总数就回答不了"钱花在哪一侧"）；
4. 切片按**本次运行**取增量（旁路是异步写入的，用累计值会让第二个节点重复计）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("agents", reason="openai-agents 未安装 ⇒ agent 链路不存在，本组无从验证")

from agents.items import Usage  # noqa: E402

from src import observability  # noqa: E402
from src.agents import budget  # noqa: E402
from src.agents import compaction as C
from src.agents import history_budget as hb
from src.agents import reflection as R
from src.agents.context import AgentStats, TaskContext  # noqa: E402
from src.agents.runner import _bypass_slice  # noqa: E402


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


class _FakeClient:
    """只实现 ``chat.completions.create`` 的最小替身，**带 usage**。

    带 usage 是重点：这一整组要验的就是"那次出站调用的用量有没有被交回来"。
    不带 usage 的替身会让每个用例都恒绿 —— 它验的是"什么都没发生"。
    """

    def __init__(self, content: str = "正文", usage: Usage | None = None) -> None:
        self._content = content
        self._usage = usage
        self.chat = self
        self.completions = self

    def create(self, **kwargs):  # noqa: ANN003, ARG002
        message = SimpleNamespace(content=self._content)
        choice = SimpleNamespace(message=message)
        return SimpleNamespace(choices=[choice], usage=self._usage)


@pytest.fixture(autouse=True)
def _clean_bypass_state():
    R.reset_reflection_state()
    C.reset_compaction_state()
    yield
    R.reset_reflection_state()
    C.reset_compaction_state()


# ---------------------------------------------------------------------------
# 一、两个旁路都真的上报
# ---------------------------------------------------------------------------
def test_reflection_reports_its_usage(monkeypatch):
    """反思这一次出站调用的用量要交回调用方。

    判据是"回调被调了几次、数值对不对"，不是"函数跑完了" —— 后者在漏掉整段
    记账代码时同样成立。
    """
    seen: list[tuple[int, int]] = []
    monkeypatch.setattr(
        R, "_client_or_none",
        lambda: _FakeClient('{"problem":"ok"}', Usage(input_tokens=900, output_tokens=30)),
    )

    assert R._reflect_sync("证据", on_usage=lambda u: seen.append((u.input_tokens, u.output_tokens))) == {"problem": "ok"}
    assert seen == [(900, 30)]


def test_reflection_counts_usage_even_when_the_answer_is_unparseable(monkeypatch):
    """解析不出来也要记账 —— 钱已经花了。

    这是最容易被写成"只在成功时记"的一格：把回调放到 ``_parse_finding`` 之后，
    一条模型胡说八道的回答就白花了，而看板上连痕迹都没有。
    """
    seen: list[int] = []
    monkeypatch.setattr(
        R, "_client_or_none",
        lambda: _FakeClient("这不是 JSON", Usage(input_tokens=700, output_tokens=5)),
    )

    assert R._reflect_sync("证据", on_usage=lambda u: seen.append(u.input_tokens)) is None
    assert seen == [700]


def test_compaction_reports_its_usage(monkeypatch, sink):
    """窗口压缩那一次摘要调用的用量要交回调用方。"""
    seen: list[int] = []
    monkeypatch.setattr(
        C, "_client_or_none",
        lambda: _FakeClient("摘要", Usage(input_tokens=4321, output_tokens=88)),
    )

    out = C.summarize_items(
        [{"role": "user", "content": "旧话"}], on_usage=lambda u: seen.append(u.input_tokens)
    )
    assert out == "摘要"
    assert seen == [4321]


def test_window_compaction_passes_the_sink_down(monkeypatch):
    """``compact_for_window`` 必须把回调传进摘要函数 —— 漏传就是"记了但没生效"。"""
    got: dict = {}

    async def fake_summarize(segment, *, on_usage=None, **kw):
        got["on_usage"] = on_usage
        got["extra"] = kw
        return "摘要"

    monkeypatch.setattr(C, "history_compact_enabled", lambda: True)
    monkeypatch.setattr(C, "window_compact_trigger", lambda: 1)
    monkeypatch.setattr(C, "_items_tokens_over", lambda *a, **k: True)
    monkeypatch.setattr(C, "_rounds_that_fit", lambda *a, **k: 1)
    monkeypatch.setattr(C, "_plan_keep_from", lambda *a, **k: 1)
    monkeypatch.setattr(C, "_summarize_async", fake_summarize)

    rec = lambda u: None  # noqa: E731
    asyncio.run(C.compact_for_window([{"role": "user", "content": "a"}], on_usage=rec))
    assert got["on_usage"] is rec


def test_the_reflection_gate_wires_the_sink(monkeypatch):
    """闸门（``_run``）必须把 ``budget.note_bypass`` **接上**，不能只让函数支持。

    与上面两条的分工：那两条验的是 ``_reflect_sync`` 收到回调会调、且解析失败也调；
    这一条验的是"闸门有没有把回调递下去"。少了它，把 ``on_usage=`` 那一行整个删掉
    不会有任何用例变红 —— 旁路记账退化成"函数支持，但没人用"，而账面上一切正常。
    """
    monkeypatch.setattr(
        R, "_client_or_none",
        lambda: _FakeClient('{"problem":"ok"}', Usage(input_tokens=321, output_tokens=7)),
    )
    ctx = TaskContext()

    asyncio.run(R._run(ctx, "证据", "oscillation"))

    assert ctx.stats.bypass_tokens == {
        "reflection": {"input": 321, "output": 7, "cached": 0}
    }


def test_the_trim_path_wires_compaction_into_the_run_stats(monkeypatch):
    """裁入入口（``_compact_before_trim``）必须把**本次运行**的 stats 接给压缩。

    这一条守的是"成本归到哪一次运行上"。``CallModelData.context`` 是用户自己的
    TContext（官方声明 ``context: TContext | None``），所以能直接取到 ``stats``；
    取错成"每次新建一个 stats"会让预算永远看不见压缩这笔钱，且症状是"预算从没到线"。
    """
    from agents.run_config import CallModelData, ModelInputData

    from agents import Agent

    got: dict = {}

    async def fake_compact(items, *, instructions="", on_usage=None, **kw):  # noqa: ARG001
        got["on_usage"] = on_usage
        got["extra"] = kw
        return None

    monkeypatch.setattr(C, "compact_for_window", fake_compact)
    ctx = TaskContext()
    data = CallModelData(
        model_data=ModelInputData(
            input=[{"role": "user", "content": "问"}], instructions=""
        ),
        agent=Agent(name="probe"),
        context=ctx,
    )

    asyncio.run(hb._compact_before_trim(data))

    assert got["on_usage"] is not None, "压缩那一路没拿到回调 ⇒ 这笔钱不会进预算"
    got["on_usage"](Usage(input_tokens=1200, output_tokens=60))
    assert ctx.stats.bypass_tokens == {
        "compaction": {"input": 1200, "output": 60, "cached": 0}
    }


# ---------------------------------------------------------------------------
# 二、旁路进预算（只上报不进判据 = 白记）
# ---------------------------------------------------------------------------
def test_used_of_includes_bypass():
    """``used_of`` 必须是"主链路 + 旁路"。"""
    ctx = TaskContext()
    ctx.stats.input_tokens = 100
    ctx.stats.output_tokens = 50
    ctx.stats.bypass_tokens = {"compaction": {"input": 4000, "output": 200}}
    assert budget.used_of(ctx) == 4350


def test_budget_trips_on_bypass_alone(monkeypatch):
    """主链路一分没花、旁路花超了，预算也必须停。

    这一格是整组的价值所在：它对应"一次运行里压缩连跑几轮、主链路的 token 还没
    涨到线"这种真实形态。
    """
    monkeypatch.setenv("AGENT_RUN_TOKEN_BUDGET", "5000")
    ctx = TaskContext()
    ctx.stats.bypass_tokens = {"compaction": {"input": 6000, "output": 0}}

    with pytest.raises(budget.AgentStoppedByBudget) as exc:
        budget.check(ctx)
    assert exc.value.used == 6000


def test_bypass_only_stats_are_not_treated_as_unmeasured(monkeypatch, sink):
    """只有旁路的用量、没有主链路用量时**不该**报"没量到"。

    ``agent_run_tokens_unmeasured_total`` 的语义是"网关根本不给 usage"。只按主链路
    判会让它在"这一轮只跑了压缩、还没调主模型"时误报 —— 而误报的代价是有人去查
    一个不存在的网关问题。
    判据刻意只看主链路那两栏：旁路有数不代表主链路量得到，反之亦然。
    """
    monkeypatch.setenv("AGENT_RUN_TOKEN_BUDGET", "1000")
    stats = AgentStats(model_calls=1, bypass_tokens={"reflection": {"input": 50, "output": 5}})
    budget.report(stats, agent="frontline_agent")
    # 主链路仍然一个都没量到 ⇒ 这条计数该有。
    assert sink.counters("agent_run_tokens_unmeasured_total")


# ---------------------------------------------------------------------------
# 三、指标按来源分开
# ---------------------------------------------------------------------------
def test_report_splits_bypass_metric_by_source(sink):
    stats = AgentStats(
        bypass_tokens={
            "reflection": {"input": 100, "output": 10},
            "compaction": {"input": 9000, "output": 200},
        }
    )
    budget.report(stats, agent="frontline_agent")

    rows = {
        (e[3].get("source"), e[3].get("kind")): e[2]
        for e in sink.counters("agent_bypass_tokens_total")
    }
    assert rows == {
        ("reflection", "input"): 100,
        ("reflection", "output"): 10,
        ("compaction", "input"): 9000,
        ("compaction", "output"): 200,
    }
    # 主链路那条**不能**被旁路污染：两者口径不同（见 AgentStats.bypass_tokens）。
    assert sink.counters("agent_run_tokens_total") == []


def test_report_carries_the_agent_label(sink):
    """按"每次运行"类的指标必须带 ``agent`` 标签（项目硬约定）。"""
    stats = AgentStats(bypass_tokens={"compaction": {"input": 10, "output": 0}})
    budget.report(stats, agent="literary_writer")
    assert sink.counters("agent_bypass_tokens_total")[0][3]["agent"] == "literary_writer"


# ---------------------------------------------------------------------------
# 四、切片取增量
# ---------------------------------------------------------------------------
def test_bypass_slice_only_counts_this_run():
    """第二次运行只能报自己的增量 —— 旁路字典是**共享引用**，直接搬会累加。"""
    before = {"compaction": {"input": 1000, "output": 20}}
    current = {"compaction": {"input": 1300, "output": 25}, "reflection": {"input": 80, "output": 4}}
    assert _bypass_slice(before, current) == {
        "compaction": {"input": 300, "output": 5, "cached": 0},
        "reflection": {"input": 80, "output": 4, "cached": 0},
    }


def test_bypass_slice_drops_unchanged_sources():
    """这一轮一笔都没动的来源**不要留空壳**：留着会让"动过哪条旁路"读起来是假的。"""
    before = {"compaction": {"input": 10, "output": 1}}
    assert _bypass_slice(before, {"compaction": {"input": 10, "output": 1}}) == {}


# ---------------------------------------------------------------------------
# 五、拿不到 usage 时不编数
# ---------------------------------------------------------------------------
def test_no_usage_means_nothing_is_recorded():
    """网关不回 usage 时宁可空着，也不要补一个估算值。

    编出来的数会让预算判据失去意义，而且**看不出是编的** —— 那是比"没有数"更坏的
    状态（没有数至少还能从 ``agent_run_tokens_unmeasured_total`` 看出来）。
    """
    stats = AgentStats()
    budget.note_bypass(stats, "reflection", None)
    budget.note_bypass(stats, "compaction", SimpleNamespace(input_tokens=0, output_tokens=0))
    assert stats.bypass_tokens == {"compaction": {"input": 0, "output": 0, "cached": 0}}
    assert stats.bypass_input_tokens == 0


def test_note_bypass_tolerates_a_missing_stats():
    """上下文形态不认识时**不能抛** —— 记账失败不该把一次正常运行带崩。"""
    budget.note_bypass(None, "reflection", Usage(input_tokens=1, output_tokens=1))


def test_note_bypass_accumulates_per_source():
    stats = AgentStats()
    budget.note_bypass(stats, "reflection", Usage(input_tokens=10, output_tokens=2))
    budget.note_bypass(stats, "reflection", Usage(input_tokens=5, output_tokens=1))
    budget.note_bypass(stats, "compaction", Usage(input_tokens=100, output_tokens=9))
    assert stats.bypass_tokens == {
        "reflection": {"input": 15, "output": 3, "cached": 0},
        "compaction": {"input": 100, "output": 9, "cached": 0},
    }
    assert stats.bypass_input_tokens == 115
    assert stats.bypass_output_tokens == 12
