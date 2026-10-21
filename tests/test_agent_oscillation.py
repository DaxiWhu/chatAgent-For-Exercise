"""震荡检测：``A→B→A→B`` 这种"换着做但没换思路"的打转。

为什么单独一组：这条判据补的是 ``repeat_counts`` 的**结构性盲区** —— 两个动作
各做两次时，重复判据（阈值 3）完全沉默，而那正是最典型的打转形态之一。它比原样
重试更隐蔽：换了一个工具、参数也不同，看起来像"在想办法"。

所以这里钉三件事：
1. 判据本身认得出"周期 2 / 周期 3 的往复"，且**不**把纯重复（``A A A A``）算进来
   （那是 ``repeat`` 的活，两处都报会让看板上的归因打架）；
2. 只看**尾部**：走出来之后不再报（否则一个曾经绕过的运行会被一直标成"在打转"）；
3. 指标口径是"每多转一整圈记一次"，因为闸门要的是"转了几圈"，不是"有没有转过"。
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "agents", reason="openai-agents 未安装 ⇒ agent 链路会静默降级，本组无从验证"
)

from src import observability  # noqa: E402
from src.agents import reflection  # noqa: E402
from src.agents.context import TaskContext  # noqa: E402
from src.agents.steps import StepLog  # noqa: E402

#: 每个工具名固定一份参数 —— 指纹 = 工具名 + 规范化参数，参数一变就不是同一个动作了。
ARGS = {"a": '{"q": "x"}', "b": '{"q": "y"}', "c": '{"q": "z"}', "d": '{"q": "w"}'}


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


def _run_steps(sequence: list[str], *, blank: set[int] | None = None) -> StepLog:
    """按真实顺序走一遍：响应里登记 → 钩子开始 → 钩子结束。

    ``blank`` 里的下标**不登记**调用信息 —— 用来复现"参数没配上、指纹为空"的步骤。
    """
    log = StepLog()
    for index, name in enumerate(sequence):
        if not blank or index not in blank:
            log.note_calls([(name, ARGS[name], f"c{index}")])
        log.begin(name)
        log.finish(name, "ok")
    return log


# --------------------------------------------------------------------------- #
# 一、判据
# --------------------------------------------------------------------------- #
def test_a_two_step_oscillation_is_seen_even_below_the_repeat_threshold():
    """★★ 核心论点：``A→B→A→B`` 时两个动作**各只有 2 次**。

    ``max_repeat == 2`` 说明重复判据（默认阈值 3）这一刻什么都没看见 —— 而它恰恰
    是一次打转。这条用例同时钉住"为什么需要震荡判据"，是删它还是调阈值之前必须
    先想清楚的一件事：把重复阈值降到 2 会把"同一个检索查两次"也当成打转。
    """
    log = _run_steps(["a", "b", "a", "b"])

    assert log.max_repeat == 2, "前提变了：重复判据现在自己就能看见它，震荡判据不再是补盲区"
    assert log.tail_cycle() == (2, 2)
    assert log.oscillation_cycles == 2


def test_a_three_step_oscillation_is_seen_too():
    """周期 3（``A→B→C→A→B→C``）同样算 —— 三个"不同做法"轮着试也是没换思路。"""
    assert _run_steps(["a", "b", "c", "a", "b", "c"]).tail_cycle() == (3, 2)


def test_a_pure_repeat_is_not_an_oscillation():
    """★ ``A A A A`` 是**纯重复**，不是往复。

    让它也算震荡的后果很具体：``A`` 做四次会被说成"两个动作来回"，而看板上那几步
    长得一模一样，值班的人对不上号 —— 归因打架比漏报更难收拾。纯重复由
    ``repeat_counts`` 负责，那条判据本来就够。
    """
    log = _run_steps(["a", "a", "a", "a"])

    assert log.tail_cycle() == (0, 0)
    assert log.max_repeat == 4, "纯重复必须仍被 repeat 看见（否则两边都不报了）"


def test_only_the_tail_counts():
    """★ 只看尾部：``A B A B C`` 已经走出来了 ⇒ 不再报。

    整段扫描会让"很久以前绕过一次、之后已经正常推进"的运行一直被标成打转，
    进而触发提醒与回退 —— 而那些动作早已不在模型输入里（被裁掉也定位不到），
    最后走到第 3 级刹车。判据要回答的是"此刻还在不在绕"。
    """
    assert _run_steps(["a", "b", "a", "b", "c"]).tail_cycle() == (0, 0)


def test_a_step_without_a_fingerprint_breaks_the_sequence():
    """★ 空指纹当**断点**，不当"跳过"。

    空指纹意味着认不出这一步是不是同一个动作（参数没配上）。跳过它会把
    ``A B C A B``（C 认不出）读成 ``A B A B`` —— 一条假的震荡，而它会把一次
    正常的多步流程标成打转。方向宁可漏报：漏报只是少一次提醒，误报会真的去撤步骤。
    """
    log = _run_steps(["a", "b", "c", "a", "b"], blank={2})

    assert log.tail_cycle() == (0, 0)


def test_unsettled_steps_do_not_break_the_sequence():
    """还没定格的步骤不参与、也不打断 —— 它正是"当前这一拍"，还没有结果。"""
    log = _run_steps(["a", "b", "a", "b"])
    log.note_calls([("a", ARGS["a"], "c9")])
    log.begin("a")  # 只开始，不结束

    assert log.tail_cycle() == (2, 2)


# --------------------------------------------------------------------------- #
# 二、指标口径
# --------------------------------------------------------------------------- #
def test_one_extra_cycle_is_broadcast_once(sink):
    """``A→B→A→B`` ⇒ 记 1 次（此刻转满 2 圈）。"""
    _run_steps(["a", "b", "a", "b"])

    events = sink.counters("agent_action_oscillation_total")
    assert len(events) == 1
    assert events[0][3] == {"period": "2"}


def test_each_additional_cycle_is_broadcast_again(sink):
    """★ 又转一圈就再记一次 —— 这样 ``sum`` 直接是"多转了几圈"。

    只记一次的话，"转了 2 圈"和"转了 6 圈"在指标上完全一样，而后者显然更该介入。
    """
    _run_steps(["a", "b", "a", "b", "a", "b"])

    assert len(sink.counters("agent_action_oscillation_total")) == 2


def test_a_normal_flow_broadcasts_nothing(sink):
    """四个不同动作 ⇒ 一条都不记（否则这条指标会随运行长度线性上涨）。"""
    _run_steps(["a", "b", "c", "d"])

    assert sink.counters("agent_action_oscillation_total") == []


# --------------------------------------------------------------------------- #
# 三、接进反思闸门
# --------------------------------------------------------------------------- #
def _ctx(sequence: list[str]) -> TaskContext:
    ctx = TaskContext()
    ctx.steps = _run_steps(sequence)
    return ctx


def test_the_reflection_counts_oscillation_cycles():
    """反思读得到圈数（``counts_of`` 是它与步骤清单之间唯一的接口）。"""
    counts = reflection.counts_of(_ctx(["a", "b", "a", "b"]))

    assert counts["oscillate"] == 2
    assert counts["repeat"] == 2


def test_oscillation_triggers_the_gate_where_repeat_does_not(monkeypatch):
    """★★ 同一份清单：重复判据不触发，震荡判据触发。

    这就是这条判据存在的全部理由。⚠️ 断言的是"触发原因是 oscillate"，不是
    "触发了"—— ``_due`` 只返回第一个到期的原因，返回 ``repeat`` 说明震荡那条
    被排到了后面（或阈值配错了），而"触发了但原因错"会让看板里的归因错位。
    """
    monkeypatch.delenv("AGENT_REFLECT_ON_OSCILLATION", raising=False)
    monkeypatch.delenv("AGENT_REFLECT_ON_REPEAT", raising=False)
    ctx = _ctx(["a", "b", "a", "b"])

    counts = reflection.counts_of(ctx)
    state = reflection.ReflectionState()

    assert reflection._due({"tools": 0, "heavy": 0, "hops": 0, "oscillate": 0}, state) is None
    assert reflection._due(
        {"tools": 0, "heavy": 0, "hops": 0, "repeat": counts["repeat"], "oscillate": 0},
        state,
    ) is None
    assert reflection._due(counts, state) == "oscillate"


def test_the_oscillation_threshold_is_configurable(monkeypatch):
    """默认 2 圈，可调到 3 圈（配置项存在且被读）。"""
    ctx = _ctx(["a", "b", "a", "b"])
    counts = reflection.counts_of(ctx)

    monkeypatch.delenv("AGENT_REFLECT_ON_OSCILLATION", raising=False)
    assert reflection._due(counts, reflection.ReflectionState()) == "oscillate"

    monkeypatch.setenv("AGENT_REFLECT_ON_OSCILLATION", "3")
    assert reflection._due(counts, reflection.ReflectionState()) is None


def test_the_evidence_shows_the_order_of_actions():
    """★ 证据里必须有**动作顺序**，否则模型只会看到"a 两次、b 两次"。

    ``A→B→A→B`` 在按指纹聚合的视图里读起来像"查了两遍资料"，不像"来回试"。
    顺序行是模型能判出"这是往复"的唯一依据。
    """
    evidence = reflection.evidence_of(_ctx(["a", "b", "a", "b"]))

    assert "最近的动作顺序" in evidence
    assert "a → b → a → b" in evidence


def test_the_evidence_reports_how_many_cycles_were_turned():
    """圈数要写进计数那一行，模型才不用自己数。"""
    evidence = reflection.evidence_of(_ctx(["a", "b", "a", "b"]))

    assert "已 2 圈" in evidence


def test_the_evidence_omits_the_cycle_note_when_there_is_no_oscillation():
    """没在绕的时候不写那一句 —— 它出现在每条证据里就只是噪声。"""
    evidence = reflection.evidence_of(_ctx(["a", "b", "c", "d"]))

    assert "圈" not in evidence
