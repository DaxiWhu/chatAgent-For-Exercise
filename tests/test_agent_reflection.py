"""第三阶段守卫：反思闸门（触发 / 异步发起 / 结构化结论 / 注入）。

守的是什么
----------
一个 agent 节点 = 一次完整的运行，这段内部**图不介入、不落检查点** ⇒ 中间出了问题
（来回试、反复失败、早就跑偏）没有任何东西会打断它。前两步让它"看得见"，本模块让它
"能介入"：**够了就起一个后台任务去问旁路模型，结论在下一次模型调用之前塞进输入**。

本组守四件事，每一件都能被单点改坏且**症状是安静的**：

* **触发**：什么时候该看（阈值整数倍），以及**不该**看的时候别乱看（发起一次就是一次
  真实模型调用）；
* **证据**：反思模型看到的是"有迹象的那几条"，不是全部步骤（否则成本随任务长度涨）；
* **结论**：解析不出来时按「没问题」——绝不能因为"反思自己没读懂"把用户的请求搅了；
* **注入**：一条 system 消息、只注入一次、**在裁剪之后**（排前面会被裁掉）。

两处最容易写成"看起来能跑"的地方
--------------------------------
1. **注入排在裁剪之前**：症状是"提醒过、模型没照做"，而日志与指标里一切正常
   （提醒确实生成了、也进了输入，只是下一轮被当成老历史裁掉了）。
2. **在飞任务没有强引用**：``asyncio.create_task`` 的返回值不持有就会被 GC 掉，
   任务可能停在半个动作上（与 ``app/main.py`` 那条同源）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("agents", reason="openai-agents 未安装 ⇒ agent 链路无从验证")

from agents.run_config import ModelInputData  # noqa: E402

from src import observability  # noqa: E402
from src.agents import reflection as R  # noqa: E402
from src.agents.context import TaskContext  # noqa: E402
from tests._agents_testkit import (  # noqa: E402
    ScriptedModel,
    call,
    decide,
    make_ctx,
)

#: 本模块读的全部环境变量。每个用例开始前一律清掉 —— 否则"上一条用例设过的开关"
#: 会漏到下一条里，而那种失败看起来像"这条用例自己写错了"。
_KNOBS = (
    "AGENT_MULTISTEP_GUARD",
    "AGENT_REFLECT_AFTER_TOOLS",
    "AGENT_REFLECT_AFTER_HEAVY",
    "AGENT_REFLECT_AFTER_HOPS",
    "AGENT_REFLECT_ON_REPEAT",
    "AGENT_REFLECT_MAX_LEVEL1",
    "AGENT_REFLECT_MAX_LEVEL2",
    "AGENT_REFLECT_MAX_FIRES",
    "AGENT_REFLECT_TIMEOUT",
    "AGENT_REFLECT_WAIT_SECONDS",
    "AGENT_HISTORY_TOKEN_LIMIT",
)


class RecordingSink:
    """把指标原样记下来的观测出口替身（与步骤记录那组同一个口径）。"""

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
def _isolate(monkeypatch):
    """每个用例：清环境变量 + **禁掉真网关** + 清进程内状态。

    ⚠️ 禁掉旁路模型是硬要求：反思会 ``asyncio.create_task`` 出去，只要本机（或 CI）
    恰好配了 ``AGNES_API_KEY``，它就会**真的发出一次网络请求** —— 测试不该有这种
    副作用，而它一旦发生还很难看出来（结果只是慢一点）。
    """
    for name in _KNOBS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(R, "_reflect_sync", lambda evidence: {"problem": "ok"})
    R.reset_reflection_state()


def _enable(monkeypatch, **knobs) -> None:
    """打开总开关，并按需覆盖阈值。"""
    monkeypatch.setenv("AGENT_MULTISTEP_GUARD", "1")
    for name, value in knobs.items():
        monkeypatch.setenv(name, str(value))


def _do(ctx: TaskContext, tool: str, args: str, idx: int, *, ok: bool = True) -> None:
    """走一遍真实顺序做一次动作。"""
    ctx.steps.note_calls([(tool, args, f"c{idx}")])
    ctx.steps.begin(tool)
    if ok:
        ctx.steps.finish(tool, f"结果{idx}")
    else:
        ctx.steps.fail(tool, ValueError(f"坏{idx}"))
        ctx.steps.finish(tool, "错误文本")


async def _fire_async(ctx: TaskContext) -> None:
    R.maybe_reflect(ctx)
    tasks = list(ctx.reflection.tasks)
    if tasks:
        await asyncio.gather(*tasks)


def _fire(ctx: TaskContext) -> None:
    """判一次闸门，并**等在飞的任务结束**（否则任务会在用例结束之后才跑）。"""
    asyncio.run(_fire_async(ctx))


async def _run_frontline(prompt, *, ctx, model):
    from src.agents.runner import ENTRY_AGENT, run_agent

    return await run_agent(ENTRY_AGENT, prompt=prompt, context=ctx, model=model)


def _flatten(items) -> str:
    """把模型看到的输入拍成一段文本（条目形态可能是 dict 也可能是对象）。"""
    return json.dumps(items, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- #
# 一、开关与常量
# --------------------------------------------------------------------------- #
def test_guard_is_off_by_default():
    """★ **默认关**。与 ``AGENT_TOOL_STEPS`` 的区别是本质的：那个只记录，
    这个会**往模型输入里塞东西**（第四步还会删上下文）⇒ 按老规矩默认关。"""
    assert R.guard_enabled() is False


def test_guard_can_be_turned_on(monkeypatch):
    _enable(monkeypatch)

    assert R.guard_enabled() is True


def test_heavy_tools_exclude_search_and_review():
    """★ 重量级 = **叫专家 + 落盘产出**；检索与评审不算。

    这条名单值得单钉：把 ``kb_search`` 算进来 ⇒ 任何一次认真查资料都逼近阈值；
    把 ``review_draft`` 算进来 ⇒ 正常的"产出→评审→改"被误报成跑偏。两种都不报错，
    只表现为"这个闸门老是无缘无故介入"，久了就没人看它了。
    """
    heavy = R.heavy_tools()

    assert heavy == {
        "research", "build_slide_deck", "make_illustration",
        "save_text_asset", "generate_image",
    }
    assert "kb_search" not in heavy
    assert "review_draft" not in heavy


def test_thresholds_match_the_documented_defaults():
    """默认阈值与 ``.env.example`` / 方案文档一致（改动必须三处一起改）。"""
    import os

    assert os.environ.get("AGENT_REFLECT_AFTER_TOOLS") is None
    ctx = TaskContext()
    _do(ctx, "kb_search", "{}", 1)
    # 读出来的是"默认值下的判定"：8 次以内不触发。
    for i in range(2, 8):
        _do(ctx, "kb_search", json.dumps({"q": i}), i)
    assert R._due(R.counts_of(ctx), ctx.reflection) is None
    _do(ctx, "kb_search", '{"q": 99}', 99)
    assert R._due(R.counts_of(ctx), ctx.reflection) == "tools"


# --------------------------------------------------------------------------- #
# 二、四个计数（全部从既有数据算出来）
# --------------------------------------------------------------------------- #
def test_counts_tools_and_heavy():
    """``tools`` 数全部动作，``heavy`` 只数名单里的那几个。"""
    ctx = TaskContext()
    for i in range(3):
        _do(ctx, "kb_search", json.dumps({"q": i}), i)
    for i in range(3, 5):
        _do(ctx, "research", json.dumps({"q": i}), i)

    counts = R.counts_of(ctx)
    assert (counts["tools"], counts["heavy"]) == (5, 2)


def test_hops_comes_from_the_cross_node_handoff_field():
    """``hops`` 取自 ``stats.handoffs`` —— ⚠️ 它天然跨节点累计。

    这是唯一一个"跨运行"的计数（其余三个都只属于本次运行）。写在同一个函数里、
    却来自两个不同的地方，正是它容易被写错的原因：从步骤清单里数换人是数不出来的
    （换人发生在两次运行**之间**）。
    """
    ctx = TaskContext()
    assert R.counts_of(ctx)["hops"] == 0

    ctx.stats.handoffs.append(("frontline", "literary"))

    assert R.counts_of(ctx)["hops"] == 1


def test_repeat_comes_from_the_step_log():
    ctx = TaskContext()
    for i in range(1, 4):                     # 同一动作做 3 次
        _do(ctx, "save_text_asset", '{"name": "n", "content": "c"}', i)

    assert R.counts_of(ctx)["repeat"] == 3


def test_counts_tolerate_an_empty_context():
    """空上下文 ⇒ 四个 0，且**不抛异常**（这是个观测函数）。"""
    assert R.counts_of(TaskContext()) == {"tools": 0, "heavy": 0, "hops": 0, "repeat": 0}
    assert R.counts_of(SimpleNamespace()) == {"tools": 0, "heavy": 0, "hops": 0, "repeat": 0}


# --------------------------------------------------------------------------- #
# 三、触发：什么时候该看，以及别乱看
# --------------------------------------------------------------------------- #
def test_nothing_fires_before_the_threshold(monkeypatch):
    _enable(monkeypatch, AGENT_REFLECT_AFTER_TOOLS=3)
    ctx = TaskContext()
    for i in range(1, 3):
        _do(ctx, "kb_search", json.dumps({"q": i}), i)

    _fire(ctx)

    assert ctx.reflection.fired == {}
    assert ctx.reflection.tasks == set()


def test_fires_when_the_threshold_is_reached(monkeypatch):
    _enable(monkeypatch, AGENT_REFLECT_AFTER_TOOLS=3)
    ctx = TaskContext()
    for i in range(1, 4):
        _do(ctx, "kb_search", json.dumps({"q": i}), i)

    _fire(ctx)

    assert ctx.reflection.fired == {"tools": 1}


def test_refires_only_on_the_next_multiple(monkeypatch):
    """★★ 阈值同时承担"什么时候开始看"与"**多久再看一眼**"。

    工具阈值 8 ⇒ 第 8、16 次各触发一次；第 9~15 次**不触发**。
    写成"过了阈值就每次都触发"的后果：长任务里每多一个工具就发起一次反思，
    而每次反思都是一次真实模型调用 ⇒ 反思会比主线还贵（方案风险清单里列过）。
    写成"每个原因只触发一次"的后果：第四步的"没纠正就升级"失去依据。
    """
    _enable(monkeypatch, AGENT_REFLECT_AFTER_TOOLS=8)
    ctx = TaskContext()
    for i in range(1, 9):
        _do(ctx, "kb_search", json.dumps({"q": i}), i)
    _fire(ctx)
    assert ctx.reflection.fired == {"tools": 1}

    for i in range(9, 16):                    # 9~15：不该再触发
        _do(ctx, "kb_search", json.dumps({"q": i}), i)
    assert R._due(R.counts_of(ctx), ctx.reflection) is None

    _do(ctx, "kb_search", json.dumps({"q": 16}), 16)
    assert R._due(R.counts_of(ctx), ctx.reflection) == "tools"


def test_each_reason_keeps_its_own_multiple(monkeypatch):
    """四个原因各按**自己的**阈值整数倍走（互不干扰）。

    ⚠️ 这里刻意用**非重量级**的检索来造"重复"：换成 ``save_text_asset`` 的话
    ``heavy`` 会先到期（它默认阈值只有 3），测的就不是"重复"这一路了。
    """
    _enable(monkeypatch, AGENT_REFLECT_ON_REPEAT=3, AGENT_REFLECT_AFTER_TOOLS=99)
    ctx = TaskContext()
    for i in range(1, 3):
        _do(ctx, "kb_search", '{"q": "same"}', i)

    assert R._due(R.counts_of(ctx), ctx.reflection) is None      # 重复 2 次，还没到 3

    _do(ctx, "kb_search", '{"q": "same"}', 3)
    assert R._due(R.counts_of(ctx), ctx.reflection) == "repeat"

    _fire(ctx)
    assert ctx.reflection.fired == {"repeat": 1}
    # 第 4、5 次不再触发（下一个整数倍是 6）
    for i in range(4, 6):
        _do(ctx, "kb_search", '{"q": "same"}', i)
    assert R._due(R.counts_of(ctx), ctx.reflection) is None


def test_the_first_due_reason_wins(monkeypatch):
    """多个原因同时到期时只报**第一个** —— 优先级 = 工具 → 重量级 → 换人 → 重复。

    同时报多个会让指标里"到底为什么触发"这一栏失去意义（每次都是三个原因，
    等于没有信息）。
    """
    _enable(monkeypatch, AGENT_REFLECT_AFTER_TOOLS=1, AGENT_REFLECT_AFTER_HEAVY=1,
            AGENT_REFLECT_ON_REPEAT=2)
    ctx = TaskContext()
    _do(ctx, "research", '{"q": "x"}', 1)
    _do(ctx, "research", '{"q": "x"}', 2)      # tools=2、heavy=2、repeat=2 三个都到期

    assert R._due(R.counts_of(ctx), ctx.reflection) == "tools"


def test_nothing_fires_when_the_guard_is_off(monkeypatch):
    """开关关着时**一次都不发起**（阈值配得再小也一样）。"""
    monkeypatch.setenv("AGENT_REFLECT_AFTER_TOOLS", "1")
    ctx = TaskContext()
    _do(ctx, "kb_search", '{"q": 1}', 1)

    _fire(ctx)

    assert ctx.reflection.fired == {}
    assert ctx.reflection.tasks == set()


def test_no_more_fires_than_the_fire_cap(monkeypatch, sink):
    """发起次数不超过**发起上限**（``AGENT_REFLECT_MAX_FIRES``）。

    ⚠️ 第 4 步之前这条断言的是"不超过**注入**上限"—— 那时两者是同一个数。第 4 步把发起
    上限拆出来之后，用注入上限去卡发起次数会让第 2 级**永远轮不到**（它的触发线是
    "第 1 级上限 + 1"，本来就发生在注入额度用光之后），所以这里改成钉住发起上限本身生效。

    ⚠️ 它同时钉住"发起"这件事**不会**因为反复调用 ``maybe_reflect`` 而失控：
    钩子每次工具结束都会调它一次，一个 12 轮的运行会调十几次。
    ⚠️ 这里**故意**让发起上限(1) ≠ 注入上限(2)：两个数相等时，"用了错的哪一个"是测不出来的。
    """
    _enable(monkeypatch, AGENT_REFLECT_AFTER_TOOLS=1, AGENT_REFLECT_MAX_FIRES=1)
    ctx = TaskContext()
    for i in range(1, 6):
        _do(ctx, "kb_search", json.dumps({"q": i}), i)
        _fire(ctx)

    assert sum(ctx.reflection.fired.values()) == 1, ctx.reflection.fired
    assert R.max_level1() == 2, "另一个数保持默认，否则这条测不出「用错了哪一个」"


def test_in_flight_tasks_are_held_and_released(monkeypatch):
    """★★ 在飞的反思任务必须被**强引用**持有，跑完要放掉。

    ``asyncio.create_task`` 的返回值不持有就会被 GC 掉 —— 回收时任务可能停在半个动作上
    （与 ``app/main.py::_spawn_history_compaction`` 那条坑同源）。而同一份集合还是
    ``_wait_for_pending`` 的"该等谁"名单 ⇒ 少了它，"防 GC"与"等结论"两件事一起坏，
    且都不报错。
    """
    _enable(monkeypatch, AGENT_REFLECT_AFTER_TOOLS=1)

    async def scenario():
        ctx = TaskContext()
        _do(ctx, "kb_search", '{"q": 1}', 1)
        R.maybe_reflect(ctx)
        held = len(ctx.reflection.tasks)          # 刚 create_task、还没让出控制权
        await asyncio.gather(*list(ctx.reflection.tasks))
        await asyncio.sleep(0)                    # 给 done_callback 一次机会
        return held, len(ctx.reflection.tasks)

    held, left = asyncio.run(scenario())

    assert held == 1, "发起后没持有任务引用（会被 GC 掉）"
    assert left == 0, "任务跑完没放掉引用（等待名单会越积越大）"


# --------------------------------------------------------------------------- #
# 四、证据：只给"有迹象的"
# --------------------------------------------------------------------------- #
def test_evidence_lists_failures_repeats_and_heavy(monkeypatch):
    """证据里必须有：**失败的**、**重复的**、**重量级的**这三类。"""
    ctx = TaskContext()
    _do(ctx, "kb_search", '{"q": "ok"}', 1)                     # 普通、不重复
    _do(ctx, "save_text_asset", '{"name": "a"}', 2, ok=False)    # 失败
    for i in range(3, 6):
        _do(ctx, "generate_image", '{"prompt": "same"}', i)      # 重复 + 重量级

    evidence = R.evidence_of(ctx)

    assert "第 2 步 save_text_asset [failed]" in evidence
    assert "第 3 步 generate_image" in evidence
    assert "第 5 步 generate_image" in evidence


def test_evidence_has_a_counts_header(monkeypatch):
    """表头要把四个计数说清楚 —— 反思模型靠它判断"量级"。"""
    ctx = TaskContext()
    for i in range(1, 4):
        _do(ctx, "research", json.dumps({"q": i}), i)

    evidence = R.evidence_of(ctx)

    assert "工具调用共 3 次" in evidence
    assert "重量级 3 次" in evidence
    assert "换人 0 次" in evidence


def test_evidence_leaves_out_ordinary_steps():
    """★ 普通步骤**不进**证据 —— 只给"有迹象的"。

    全给出去的后果不是"慢"：反思的输入随任务长度线性膨胀，而长任务里绝大多数步骤都是
    正常的 ⇒ 反思模型要在噪声里找信号，成本还跟主线一起涨（"反思比主线还贵"那条）。
    ⚠️ 最后两条**总是**进证据（保证模型知道"现在停在哪一步"），所以"普通步骤"必须放在
    倒数第三条之前 —— 否则它会被这条兜底规则捞回来，用例就白写了。
    """
    ctx = TaskContext()
    _do(ctx, "kb_search", '{"q": "a"}', 1)             # 普通：不失败、不重复、不重量级
    _do(ctx, "kb_search", '{"q": "b"}', 2)             # 同上
    _do(ctx, "save_text_asset", '{"name": "n"}', 3)    # 重量级
    _do(ctx, "save_text_asset", '{"name": "m"}', 4)    # 重量级

    evidence = R.evidence_of(ctx)

    assert "第 3 步" in evidence and "第 4 步" in evidence
    assert "第 1 步" not in evidence, "普通步骤进了证据（输入会随任务长度膨胀）"
    assert "第 2 步" not in evidence


def test_evidence_is_capped():
    """★ 证据条数有上限 —— 反思的输入必须**有界**（它与主线共用同一份网关配额）。

    没有上限的后果不是"慢"，而是长任务里反思的输入随步骤数线性膨胀 ⇒ 它会**比主线
    还贵**，而这件事从任何指标上都看不出来（只看到账单一路上涨）。
    """
    ctx = TaskContext()
    for i in range(1, 60):
        _do(ctx, "generate_image", json.dumps({"prompt": i}), i)   # 全是重量级 ⇒ 全都有迹象

    evidence = R.evidence_of(ctx)

    listed = [line for line in evidence.splitlines() if line.startswith("- 第")]
    assert len(listed) == R._MAX_EVIDENCE


# --------------------------------------------------------------------------- #
# 五、结论：取不出来一律按"没问题"
# --------------------------------------------------------------------------- #
def test_parses_a_plain_json_object():
    raw = R._parse_finding('{"problem": "loop", "summary": "在打转"}')

    assert raw == {"problem": "loop", "summary": "在打转"}


@pytest.mark.parametrize(
    "text",
    [
        '```json\n{"problem": "loop"}\n```',          # 代码块包裹
        '我的判断是：{"problem": "loop"} 就这些。',    # 前后有解释
        '{"problem": "loop", "extra": {"a": 1}}',      # 带嵌套
    ],
)
def test_parses_json_wrapped_in_prose_or_fence(text):
    """★ **宽松解析是有意的**：旁路调用没有 ``response_format`` 的保证（网关不一定
    支持），模型常在 JSON 外面包一层解释或代码块。严格解析（要求整段就是 JSON）的
    后果是"大多数反思都白跑"，而它只表现为"这个功能好像没什么用"。"""
    raw = R._parse_finding(text)

    assert raw is not None and raw.get("problem") == "loop"


@pytest.mark.parametrize(
    "text",
    [
        "",                                  # 空
        "没有 JSON。",                        # 只有解释
        '{"problem": "loop"',                # 断了
        "[1, 2, 3]",                         # 不是对象
    ],
)
def test_unusable_output_means_no_problem(text):
    """解析不出来 ⇒ ``None`` ⇒ 调用方按"没问题"处理。

    ⚠️ 这是**必须**的行为："反思自己没读懂"绝不能变成"把用户请求拦下来"或
    "往上下文里塞一段来路不明的话"。
    """
    assert R._parse_finding(text) is None


def test_unknown_problem_value_means_no_problem():
    """模型写了个没见过的词 ⇒ 当 ``ok``，**不当**问题。

    宁可漏报一次真问题，也不要因为模型多写了个字就塞一段话进用户的上下文。
    """
    finding = R._finding_of({"problem": "WeirdValue", "summary": "?"}, "tools")

    assert finding.problem == "ok"
    assert finding.is_problem is False


def test_step_must_be_a_number():
    """``step`` 非数字 ⇒ 归 0（不抛异常）。"""
    assert R._finding_of({"problem": "loop", "step": "第三"}, "tools").step == 0
    assert R._finding_of({"problem": "loop", "step": -5}, "tools").step == 0


def test_is_problem_is_false_only_for_ok():
    for value in ("loop", "stuck", "off_track"):
        assert R.ReflectiveFinding(problem=value).is_problem is True
    assert R.ReflectiveFinding(problem="ok").is_problem is False


# --------------------------------------------------------------------------- #
# 六、注入
# --------------------------------------------------------------------------- #
def _inputs(*items):
    return ModelInputData(input=list(items), instructions="原样")


def test_injects_one_system_message_at_the_end():
    """★ 注在**末尾**、且是一条**独立的 system 消息**。

    末尾：它是"关于你刚才做法的提醒"，放最前会被后面几十条历史冲淡。
    独立成条：拼进最后一条用户消息会让"用户说过什么"与"系统提醒过什么"在事后
    （包括落库的会话里）分不开。
    """
    ctx = TaskContext()
    ctx.reflection.pending.append(R.ReflectiveFinding(problem="loop", summary="同一动作在做第 4 次"))
    md = _inputs({"role": "user", "content": "帮我写一段"})

    out = asyncio.run(R.inject_reflections(md, context=ctx))

    assert len(out.input) == 2
    assert out.input[-1]["role"] == "system"
    assert "同一动作在做第 4 次" in out.input[-1]["content"]
    assert out.input[0]["content"] == "帮我写一段", "原有输入被改动了"


def test_the_note_warns_that_it_is_not_a_new_instruction():
    """★★ 注入文本必须写明"这不是用户的新要求"。

    不写这句的后果很具体：system 消息在模型眼里就是**指令**，它可能回一句
    "好的，我重新开始"并把已经做完的工作丢掉 —— 而提醒的本意恰恰是"接着干、
    换个做法"。这条在测试里不会自己暴露，只表现为线上偶发的"答非所问"。
    """
    note = R.ReflectiveFinding(problem="loop", summary="在打转").as_note()

    assert "不是" in note and "重新开始" in note
    assert "继续当前任务" in note


def test_instructions_are_passed_through():
    """``ModelInputData.instructions`` 必须**原样**传回。

    服务端给它的意义是"模型的系统提示"；漏传会把它清空 ⇒ 模型失去全部角色设定，
    而症状是"偶发答得不像人"（与形状写错那类坑同源，都是安静地坏）。
    """
    ctx = TaskContext()
    ctx.reflection.pending.append(R.ReflectiveFinding(problem="loop", summary="x"))

    out = asyncio.run(R.inject_reflections(_inputs(), context=ctx))

    assert out.instructions == "原样"


def test_a_note_is_injected_only_once():
    """★ 每个结论只注入一次（注入后清空队列）。

    不清的表现：**每一次**模型调用都往里塞同一段话，上下文被同一个提醒反复污染，
    越到后面越像"系统在复读"，而指标上完全看不出来（注入本来就在发生）。
    """
    ctx = TaskContext()
    ctx.reflection.pending.append(R.ReflectiveFinding(problem="loop", summary="x"))
    md = _inputs()

    first = asyncio.run(R.inject_reflections(md, context=ctx))
    second = asyncio.run(R.inject_reflections(md, context=ctx))

    assert len(first.input) == 1
    assert len(second.input) == 0, "同一个结论被注入了两次"


def test_injection_stops_at_the_cap(monkeypatch):
    """注入次数有上限（``AGENT_REFLECT_MAX_LEVEL1``）。"""
    _enable(monkeypatch, AGENT_REFLECT_MAX_LEVEL1=1)
    ctx = TaskContext()
    md = _inputs()

    ctx.reflection.pending.append(R.ReflectiveFinding(problem="loop", summary="第一次"))
    assert len(asyncio.run(R.inject_reflections(md, context=ctx)).input) == 1

    ctx.reflection.pending.append(R.ReflectiveFinding(problem="stuck", summary="第二次"))
    assert len(asyncio.run(R.inject_reflections(md, context=ctx)).input) == 0


def test_no_pending_changes_nothing():
    """没有待注入的 ⇒ **原样**返回（连拷贝都不做）。"""
    ctx = TaskContext()
    md = _inputs({"role": "user", "content": "x"})

    out = asyncio.run(R.inject_reflections(md, context=ctx))

    assert out is md


def test_injection_without_a_context_is_harmless():
    """拿不到上下文 ⇒ 原样返回，不抛（形态差异不该把请求带崩）。"""
    md = _inputs({"role": "user", "content": "x"})

    assert asyncio.run(R.inject_reflections(md, context=None)) is md
    assert asyncio.run(R.inject_reflections(md, context=SimpleNamespace())) is md


def test_waiting_is_bounded_and_late_is_counted(monkeypatch, sink):
    """★★ 等待有**上限**，没赶上要**计数**（决策 6 的落点）。

    这里同时钉住两件事：
    ① 等不会无限期 —— 拿不到结论就照常往下走（用户不该为一次"可选提醒"付无限延迟）；
    ② 没赶上要留痕（``agent_reflection_late_total``）—— 这个数在涨说明阈值或超时
       配得不合适，而"反思老白跑"如果不计数就只是一种感觉。
    """
    _enable(monkeypatch, AGENT_REFLECT_WAIT_SECONDS=0.05)

    async def scenario():
        ctx = TaskContext()
        never = asyncio.ensure_future(asyncio.sleep(10))
        ctx.reflection.tasks.add(never)
        started = time.perf_counter()
        out = await R.inject_reflections(_inputs({"role": "user", "content": "x"}), context=ctx)
        elapsed = time.perf_counter() - started
        never.cancel()
        await asyncio.sleep(0)
        return out, elapsed

    out, elapsed = asyncio.run(scenario())

    assert elapsed < 2.0, f"等待没有被上限截断（等了 {elapsed:.1f}s）"
    assert len(out.input) == 1, "没拿到结论却改动了输入"
    assert len(sink.counters("agent_reflection_late_total")) == 1


def test_waiting_is_skipped_when_zero(monkeypatch, sink):
    """等待设成 0 ⇒ 立刻返回（**不计量迟到**：那是有意的配置，不是损失）。"""
    _enable(monkeypatch, AGENT_REFLECT_WAIT_SECONDS=0)

    async def scenario():
        ctx = TaskContext()
        never = asyncio.ensure_future(asyncio.sleep(10))
        ctx.reflection.tasks.add(never)
        out = await R.inject_reflections(_inputs(), context=ctx)
        never.cancel()
        await asyncio.sleep(0)
        return out

    out = asyncio.run(scenario())

    assert out.input == []
    assert sink.counters("agent_reflection_late_total") == []


# --------------------------------------------------------------------------- #
# 七、端到端：提醒真的到了模型面前
# --------------------------------------------------------------------------- #
def test_a_note_reaches_the_model_inside_a_real_run(tmp_path):
    """★★ 整条链的最关键一跳：注入的提醒**真的出现在模型看到的输入里**。

    只测到"注入函数返回了正确的对象"是不够的 —— 那个对象还要经过
    ``model_input_filter`` 交给 SDK、再交给模型的每一次调用。这条用真运行走完，
    断言的是"模型**看到**了什么"，而不是"我们**生成了**什么"。
    """
    ctx = make_ctx(tmp_path)
    ctx.reflection.pending.append(
        R.ReflectiveFinding(problem="loop", summary="同一个检索做了第 4 次")
    )
    model = ScriptedModel([
        call("save_text_asset", {"name": "n", "content": "c"}, "c1"),
        decide("答复。"),
    ])

    out = asyncio.run(_run_frontline("写一段文字", ctx=ctx, model=model))
    model.assert_complete()

    assert "同一个检索做了第 4 次" in _flatten(model.calls[0].input)
    assert out.answer == "答复。"


def test_the_note_is_not_reinjected_on_later_model_calls(tmp_path):
    """★ 注入一次之后，后续模型调用**不再**带上它（同上"只注入一次"）。"""
    ctx = make_ctx(tmp_path)
    ctx.reflection.pending.append(
        R.ReflectiveFinding(problem="loop", summary="只该出现一次的提醒")
    )
    model = ScriptedModel([
        call("save_text_asset", {"name": "n", "content": "c"}, "c1"),
        decide("答复。"),
    ])

    asyncio.run(_run_frontline("写一段文字", ctx=ctx, model=model))

    assert "只该出现一次的提醒" in _flatten(model.calls[0].input)
    assert "只该出现一次的提醒" not in _flatten(model.calls[1].input)


def test_injection_survives_and_never_squeezes_out_the_user(tmp_path, monkeypatch):
    """★★ 注入必须发生在**裁剪之后** —— 它既不能被裁掉，也不该把原有的输入挤掉。

    用"预算设成 1"把顺序差异逼出来：

    * 顺序对（先裁后注入）⇒ 结果 = ``[用户消息, 提醒]``：提醒在最后、用户消息也在；
    * 顺序反（先注入后裁）⇒ 裁剪把**提醒**当成"最后一条，无条件保留"，于是它前面的
      用户消息被当成超预算的老历史丢掉 ⇒ 结果只剩 ``[提醒]``。

    后者是很难查的一种坏：模型看不到用户刚说的话，答案自然不对，但请求照常 200，
    而日志里"提醒确实生成了、也确实进了输入"看起来一切正常。
    """
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "1")
    ctx = make_ctx(tmp_path)
    ctx.reflection.pending.append(R.ReflectiveFinding(problem="loop", summary="提醒内容"))
    model = ScriptedModel([decide("答复。")])

    asyncio.run(_run_frontline("这句是用户说的话", ctx=ctx, model=model))

    seen = _flatten(model.calls[0].input)
    assert "提醒内容" in seen, "注入的提醒没到模型面前（被裁掉了）"
    assert "这句是用户说的话" in seen, "注入把本轮的用户输入挤掉了（说明注入排在了裁剪之前）"


def test_an_end_to_end_run_does_not_blow_up_when_the_gate_fires(tmp_path, monkeypatch):
    """★ 闸门真的在运行中被触发时，整条链**照常交付**（反思是可选能力）。

    这里故意把阈值压到 1（第一个工具调用就触发），走的完全是生产路径：
    ``on_tool_end`` → ``maybe_reflect`` → 起任务 → 假旁路模型返回"没问题"。
    要钉的是"它不干扰主线" —— 一个还没跑完就被 GC、或者抛异常出去的后台任务，
    会让这条用例以各种难以归因的方式红（这正是它存在的价值）。
    """
    monkeypatch.setenv("AGENT_MULTISTEP_GUARD", "1")
    monkeypatch.setenv("AGENT_REFLECT_AFTER_TOOLS", "1")
    ctx = make_ctx(tmp_path)
    model = ScriptedModel([
        call("save_text_asset", {"name": "n", "content": "c"}, "c1"),
        decide("答复。"),
    ])

    out = asyncio.run(_run_frontline("写一段文字", ctx=ctx, model=model))
    model.assert_complete()

    assert out.answer == "答复。"
    assert out.exit_reason != "error"
    assert ctx.reflection.fired == {"tools": 1}, "闸门没被触发（这条用例就白跑了）"


# --------------------------------------------------------------------------- #
# 八、第 2 级：退回（撤回那一步）
# --------------------------------------------------------------------------- #
def _loop(problem: str = "loop", step: int = 1):
    """让假旁路模型稳定报"同一类问题、同一步"。"""
    return lambda evidence: {"problem": problem, "summary": "在打转", "step": step}


def _tool_items(call_id: str, tool: str = "kb_search", output: str = "结果") -> list[dict]:
    """一对"工具调用 + 结果"条目 —— 形状取自实测（见 probe_input_call_id.py）。"""
    return [
        {"id": call_id, "type": "function_call", "call_id": call_id,
         "name": tool, "arguments": "{}"},
        {"type": "function_call_output", "call_id": call_id, "output": output},
    ]


def test_fire_cap_defaults_leave_room_for_level_two():
    """★★ 默认的**发起**上限必须装得下两级额度，否则第 2 级永远轮不到。

    第 2 级的触发线是"第 1 级上限 + 1"—— 它**本来就发生在第 1 级额度用光之后**。
    所以发起上限必须 ≥ 第 1 级上限 + 第 2 级上限（默认 2 + 1 = 3）。
    ⚠️ 这个组合配错**不报错、不影响交付**：反思照常触发、提醒照常注入、指标照常在涨，
    只有"退回"从不发生 —— 而"退回很少见"与"退回不可能发生"在看板上长得一模一样。
    """
    assert R._max_fires() >= R.max_level1() + R.max_level2()


def test_a_too_small_fire_cap_warns_once(monkeypatch, caplog):
    """发起上限装不下两级额度 ⇒ 打一条 warning，**而且只打一次**。

    ⚠️ "只一次"是硬要求：本函数在**每次工具结束**都会被调到，一个 12 轮的运行会调十几次，
    每次都打等于把日志变成噪声（同 history_budget 那条"打一次就够"的做法）。
    """
    _enable(monkeypatch, AGENT_REFLECT_MAX_FIRES=1)
    caplog.set_level(logging.WARNING)

    R._warn_once_if_the_cap_is_too_small()
    R._warn_once_if_the_cap_is_too_small()
    R._warn_once_if_the_cap_is_too_small()

    hits = [r for r in caplog.records if "第 2 级" in r.getMessage()]
    assert len(hits) == 1, f"配置自检打了 {len(hits)} 次"


def test_default_fire_cap_says_nothing(caplog):
    """默认配置下**不该**打这条 warning（否则守卫会教人无视它）。"""
    caplog.set_level(logging.WARNING)

    R._warn_once_if_the_cap_is_too_small()

    assert not [r for r in caplog.records if "第 2 级" in r.getMessage()]


def test_revertable_tools_are_read_only():
    """★ 可撤回 = **只读**的动作；落盘类一个都不能进白名单。

    撤回的语义是"让模型重做那一步"，而**已落盘的副作用拿不回来**（文件还在）——
    撤一个落盘动作 = 得到两份产物。⚠️ 用白名单而不是黑名单：黑名单下"新加一个工具"
    默认是**能**被撤回的，而"它有没有副作用"只有当它真被撤回一次、留下两份文件时才暴露。
    """
    revertable = R.revertable_tools()

    assert revertable == {"kb_search", "research", "review_draft"}
    for tool in ("save_text_asset", "generate_image", "build_slide_deck", "make_illustration"):
        assert tool not in revertable, f"{tool} 有副作用，不能被撤回"


def test_revert_decision_requires_a_whitelisted_tool():
    """``_revert_decision`` 的四条分支：正常 / 还没到线 / 额度用完 / 白名单外。

    ⚠️ 顺序是有意的：**先判升级线**。反过来的话，第 1、2 次反思（还没到升级的时候）
    也会被计进 ``capped`` / ``side_effect``，那个指标立刻变成噪声。
    """
    due = {"loop": 3}

    ok, why = R._revert_decision(
        R.ReflectionState(kinds=dict(due)),
        R.ReflectiveFinding(problem="loop", step=1, tool="kb_search"),
    )
    assert (ok, why) == (True, "")

    ok, why = R._revert_decision(
        R.ReflectionState(kinds={"loop": 1}),
        R.ReflectiveFinding(problem="loop", step=1, tool="kb_search"),
    )
    assert (ok, why) == (False, ""), "还没到升级线时不该计任何「异常原因」"

    ok, why = R._revert_decision(
        R.ReflectionState(kinds=dict(due), reverted=1),
        R.ReflectiveFinding(problem="loop", step=1, tool="kb_search"),
    )
    assert (ok, why) == (False, "capped")

    ok, why = R._revert_decision(
        R.ReflectionState(kinds=dict(due)),
        R.ReflectiveFinding(problem="loop", step=1, tool="save_text_asset"),
    )
    assert (ok, why) == (False, "side_effect")

    ok, why = R._revert_decision(
        R.ReflectionState(kinds=dict(due)),
        R.ReflectiveFinding(problem="loop", step=1, tool=""),
    )
    assert (ok, why) == (False, "side_effect"), "认不出工具名时必须 fail-safe（不可撤回）"


def test_attach_step_ignores_a_step_the_model_made_up():
    """★ "第几步"一律从**步骤清单**落定，不信模型报的编号与 ``call_id``。

    撤回要按 ``call_id`` 定位，而模型完全可能报一个不存在（或指到别的动作上）的编号。
    拿模型编的编号去定位，等于**把撤回交给模型决定** —— 而它撤掉的会是哪一条完全不可控。
    """
    ctx = TaskContext()
    _do(ctx, "kb_search", '{"q": 1}', 1)

    lying = R.ReflectiveFinding(problem="loop", step=99, call_id="c1", tool="kb_search")
    R._attach_step(ctx, lying)
    assert (lying.step, lying.call_id, lying.tool) == (0, "", ""), (
        "越界步号必须退化成「不指代」，不能当成第 1 步去撤"
    )

    truth = R.ReflectiveFinding(problem="loop", step=1, call_id="骗人的")
    R._attach_step(ctx, truth)
    assert (truth.step, truth.call_id, truth.tool) == (1, "c1", "kb_search")


def test_attach_step_tolerates_a_missing_or_absent_step():
    """``step`` 缺失 / 非正数 / 上下文里没有步骤 ⇒ 一律归 0，不抛异常。"""
    ctx = TaskContext()
    finding = R.ReflectiveFinding(problem="loop")
    R._attach_step(ctx, finding)

    assert (finding.step, finding.call_id, finding.tool) == (0, "", "")


def test_the_third_same_problem_reverts(monkeypatch):
    """★★ 同类问题出现到**第 3 次**才退回（用户 2026-10-09 拍板）。

    前两次只是提醒（"先提醒 N 次、还不行才撤"），第 3 次才动上下文。
    """
    monkeypatch.setattr(R, "_reflect_sync", _loop())
    _enable(monkeypatch, AGENT_REFLECT_AFTER_TOOLS=1)
    ctx = TaskContext()
    for i in range(1, 4):
        _do(ctx, "kb_search", json.dumps({"q": "same"}), i)
        _fire(ctx)

    assert ctx.reflection.kinds == {"loop": 3}
    assert [f.level for f in ctx.reflection.pending] == [1, 1, 2]
    assert ctx.reflection.reverted == 1
    last = ctx.reflection.pending[-1]
    assert (last.call_id, last.tool) == ("c1", "kb_search"), (
        "撤回的定位没落到步骤清单上（`call_id` 必须来自步骤记录，不是模型报的）"
    )


def test_two_same_problems_stay_at_level_one(monkeypatch):
    """第 2 次同类问题**还不到**升级线 ⇒ 两级都仍是"注入提醒"。"""
    monkeypatch.setattr(R, "_reflect_sync", _loop())
    _enable(monkeypatch, AGENT_REFLECT_AFTER_TOOLS=1)
    ctx = TaskContext()
    for i in range(1, 3):
        _do(ctx, "kb_search", json.dumps({"q": "same"}), i)
        _fire(ctx)

    assert [f.level for f in ctx.reflection.pending] == [1, 1]
    assert ctx.reflection.reverted == 0


def test_the_revert_line_follows_the_level_one_cap(monkeypatch):
    """★ 触发线 = **第 1 级上限 + 1**（把第 1 级额度配小 ⇒ 第 2 次同类问题就退回）。

    这条钉住"两个数是推导关系"而不是各配各的：把触发线写成写死的常量时它会红。
    """
    monkeypatch.setattr(R, "_reflect_sync", _loop())
    _enable(monkeypatch, AGENT_REFLECT_AFTER_TOOLS=1,
            AGENT_REFLECT_MAX_LEVEL1=1, AGENT_REFLECT_MAX_FIRES=2)
    ctx = TaskContext()
    for i in range(1, 3):
        _do(ctx, "kb_search", json.dumps({"q": "same"}), i)
        _fire(ctx)

    assert [f.level for f in ctx.reflection.pending] == [1, 2]


def test_only_the_same_kind_of_problem_counts_towards_the_line(monkeypatch):
    """判"没纠正"的判据是**问题类型相同**，不是"又出问题了"。

    两次 ``loop`` 夹一次 ``stuck`` ⇒ ``loop`` 只累计到 2 ⇒ 不到升级线。
    ⚠️ 反过来（数"任意问题"的总数）会把"先打转、后卡住"这种**换了种毛病**的情况判成
    "没纠正"，于是升级到一个和当前问题无关的处置上。
    """
    scripted = iter([_loop("loop"), _loop("stuck"), _loop("loop")])
    monkeypatch.setattr(R, "_reflect_sync", lambda evidence: next(scripted)(evidence))
    _enable(monkeypatch, AGENT_REFLECT_AFTER_TOOLS=1)
    ctx = TaskContext()
    for i in range(1, 4):
        _do(ctx, "kb_search", json.dumps({"q": "same"}), i)
        _fire(ctx)

    assert ctx.reflection.kinds == {"loop": 2, "stuck": 1}
    assert ctx.reflection.reverted == 0


def test_reverting_something_with_side_effects_is_skipped_and_counted(monkeypatch, sink):
    """★ 落盘类动作**不撤**，但"本来该撤却没撤"必须**计数**。

    不计数的话"退回从没发生过"只能靠人盯着看 —— 而它跟"一直没出问题"长得一模一样。
    """
    monkeypatch.setattr(R, "_reflect_sync", _loop())
    _enable(monkeypatch, AGENT_REFLECT_AFTER_TOOLS=1)
    ctx = TaskContext()
    for i in range(1, 4):
        _do(ctx, "save_text_asset", json.dumps({"name": f"n{i}"}), i)
        _fire(ctx)

    assert ctx.reflection.reverted == 0
    assert [f.level for f in ctx.reflection.pending] == [1, 1, 1]
    assert [e[3] for e in sink.counters("agent_recovery_skipped_total")] == [
        {"reason": "side_effect"}
    ], "只有第 3 次（真到升级线那一次）该计 —— 前两次属正常"
    # ★ 第 3 级：「该退却退不了」归**系统类** ⇒ 停（`PLAN_step5_fallback.md` §2）。
    # 停因就是那个 refusal —— 报 `side_effect` 才知道"要撤回的动作不可撤"，而不是"没额度"。
    assert ctx.reflection.stopped == "side_effect"


def test_reverting_when_the_budget_is_gone_is_counted(monkeypatch, sink):
    """``AGENT_REFLECT_MAX_LEVEL2=0`` ⇒ 只提醒、绝不撤；到线的那些计入 ``capped``。"""
    monkeypatch.setattr(R, "_reflect_sync", _loop())
    _enable(monkeypatch, AGENT_REFLECT_AFTER_TOOLS=1, AGENT_REFLECT_MAX_LEVEL2=0)
    ctx = TaskContext()
    for i in range(1, 4):
        _do(ctx, "kb_search", json.dumps({"q": "same"}), i)
        _fire(ctx)

    assert ctx.reflection.reverted == 0
    assert [e[3] for e in sink.counters("agent_recovery_skipped_total")] == [
        {"reason": "capped"}
    ]
    assert ctx.reflection.stopped == "capped", "额度用完也是「该退却退不了」⇒ 该停"


# --------------------------------------------------------------------------- #
# 第 3 级：该不该停（停因**逐行**钉住）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("kinds", "reverted", "refusal", "want"),
    [
        ({"loop": 2}, 0, "", ""),                        # 还没到线：那正是"先提醒 N 次"
        ({"loop": 3}, 0, "", ""),                        # 到线且撤得动 ⇒ 走第 2 级，不停
        ({"loop": 3}, 0, "capped", "capped"),            # 该退却退不了：没额度了
        ({"loop": 3}, 0, "side_effect", "side_effect"),  # 该退却退不了：那一步不可撤
        ({"loop": 3}, 1, "", ""),                        # 撤过、同类问题**刚**复发 ⇒ 还不能断言"没用"
        ({"loop": 4}, 1, "", "not_fixed"),               # 撤过、同类问题又来 ⇒ 撤了也没用
        ({"loop": 4}, 1, "capped", "capped"),            # 两个都成立时报"没额度"（更具体）
    ],
)
def test_the_level_three_reasons(kinds, reverted, refusal, want):
    """★★ 判定表的**逐行**：哪一行都不许挪。

    与 `fallback.decide` 那张表同一种理由 —— 这张表本身也是规格，而它的每一行都对应一个
    **具体的调参动作**：`capped` ⇒ 调 `AGENT_REFLECT_MAX_LEVEL2`；`side_effect` ⇒ 那个动作
    本来就不该撤；`not_fixed` ⇒ 撤回这个手段已经证伪；`not_found` ⇒ 输入被裁/压缩吃掉。

    报错一个笼统的"停了"，上面四件事就只能靠人翻日志才分得清。顺序也有意：
    `refusal` 排在 `not_fixed` **之前**（默认额度只有 1 次，同一个现象会先报"没额度"）。
    """
    state = R.ReflectionState()
    state.kinds.update(kinds)
    state.reverted = reverted

    got = R._level3_reason(state, R.ReflectiveFinding(problem="loop"), refusal)

    assert got == want


def test_a_revert_that_did_not_help_stops_the_run(monkeypatch):
    """★ 「撤了也没用」在**真实顺序**里确实会置上停因（不只是纯函数算得对）。

    ⚠️ 要走到这条得把撤回额度配到 2：默认只有 1 次，同一个现象会先落到 `capped`
    （"没额度了"比"撤了没用"更具体，见 `_level3_reason` 的顺序）。它顺带说明这两条
    停因在默认配置下是**互斥**的 —— 想看到 `not_fixed` 就必须先把额度调大。
    """
    monkeypatch.setattr(R, "_reflect_sync", _loop())
    _enable(monkeypatch, AGENT_REFLECT_AFTER_TOOLS=1,
            AGENT_REFLECT_MAX_FIRES=4, AGENT_REFLECT_MAX_LEVEL2=2)
    ctx = TaskContext()
    for i in range(1, 5):
        _do(ctx, "kb_search", json.dumps({"q": "same"}), i)
        _fire(ctx)

    assert ctx.reflection.kinds == {"loop": 4}
    assert ctx.reflection.reverted == 2, "第 3、4 次该各撤一次"
    assert ctx.reflection.stopped == "not_fixed"


def test_an_already_stopped_run_raises_without_waiting(monkeypatch, sink):
    """★ 进门前已经该停 ⇒ **立刻抛**，连"等在飞的反思"都不做。

    那几秒买不到任何东西：运行马上就被中断（模块 docstring "第 3 级"）。写成"先等完再判"
    的代价是**每一次停止多付一个 `AGENT_REFLECT_WAIT_SECONDS`**，而默认 2 秒、上限 30 秒，
    这段时间用户那边看起来就是"卡住了"。
    """
    waited = {"n": 0}

    async def _never(state):  # noqa: ANN001
        waited["n"] += 1

    monkeypatch.setattr(R, "_wait_for_pending", _never)
    ctx = TaskContext()
    ctx.reflection.stopped = "capped"
    ctx.reflection.pending.append(
        R.ReflectiveFinding(problem="loop", summary="在打转", step=1,
                            call_id="c1", tool="kb_search", level=1)
    )
    md = _inputs({"role": "user", "content": "问"})

    with pytest.raises(R.AgentStoppedByGuard) as caught:
        asyncio.run(R.inject_reflections(md, context=ctx))

    assert caught.value.reason == "capped"
    assert waited["n"] == 0, "已经该停时还在等反思跑完"
    assert [f.level for f in ctx.reflection.pending] == [1], "停之前还在处理队列"
    assert ctx.reflection.stopped == "capped"
    assert [e[3] for e in sink.counters("agent_recovery_level_total")] == [
        {"level": "3"}
    ]


def test_the_revert_note_tells_the_model_the_step_was_removed():
    """★★ 撤回说明必须写明"那一步已经被撤掉了"。

    不写的后果是最坏的一种组合：模型看不到那一步，却收到一段"你在第 N 步打转"的提醒 ——
    它要么当成幻觉忽略，要么**再把那个动作做一遍**，而"再做一遍"正是退回要阻止的事。
    ⚠️ "不是用户的新要求"这句同样必须留：撤回说明看着比提醒更像一条指令。
    """
    note = R.ReflectiveFinding(
        problem="loop", summary="在打转", step=3, tool="kb_search", advice="换关键词"
    ).as_revert_note()

    assert "已把第 3 步" in note and "kb_search" in note
    assert "撤掉" in note
    assert "不是" in note and "重新开始" in note
    assert "换关键词" in note


def test_revert_removes_the_step_and_everything_after_it():
    """★ 撤回 = **从那一步删到末尾**（让模型回到那一步之前重新决定）。

    只删那一条、把之后的东西留着的话，模型看到的是"我做了 Y，但我没做过它依赖的 X" ——
    一段没有来由的历史。
    """
    ctx = TaskContext()
    ctx.reflection.pending.append(
        R.ReflectiveFinding(problem="loop", summary="在打转", step=1,
                            call_id="c1", tool="kb_search", level=2)
    )
    md = _inputs({"role": "user", "content": "问"}, *_tool_items("c1"), *_tool_items("c2"))

    out = asyncio.run(R.inject_reflections(md, context=ctx))

    assert len(out.input) == 2, f"撤回后应只剩「用户消息 + 撤回说明」，实际 {out.input}"
    assert out.input[0] == {"role": "user", "content": "问"}
    assert out.input[-1]["role"] == "system"
    assert "已把第 1 步" in out.input[-1]["content"]
    assert ctx.reflection.revoked == {"c1", "c2"}, "被撤掉的那一段的 call_id 没记下来"
    assert ctx.reflection.injected == 0, "第 2 级不该占用第 1 级的注入额度"


def test_revert_is_persistent_across_later_model_calls():
    """★★★ 撤回必须**持久** —— 之后每一次模型调用都不再看到那一段。

    这是第 2 级最容易写成"看起来生效"的地方：注入口的改写是**本次调用专用**的
    （官方 ``maybe_filter_model_input`` 只覆盖这一次），运行状态里的条目下一轮又被拼回输入
    （``run_internal/run_loop.py``：``run_state._generated_items = ...``）。
    ⇒ 只删一次的表现是"下一轮那条动作又回来了"，而模型会以为自己没做过、很可能再调一次。
    """
    ctx = TaskContext()
    ctx.reflection.pending.append(
        R.ReflectiveFinding(problem="loop", summary="在打转", step=1,
                            call_id="c1", tool="kb_search", level=2)
    )
    md = _inputs({"role": "user", "content": "问"}, *_tool_items("c1"), *_tool_items("c2"))
    asyncio.run(R.inject_reflections(md, context=ctx))
    assert ctx.reflection.pending == []          # 这一轮已经处理过了

    # 下一轮：官方又会把（它自己那份）条目原样送进来 —— 必须靠 revoked 滤掉
    again = _inputs({"role": "user", "content": "问"}, *_tool_items("c1"), *_tool_items("c2"))
    out = asyncio.run(R.inject_reflections(again, context=ctx))

    assert [i.get("content") for i in out.input] == ["问"], (
        f"被撤回的条目又回到了模型眼前：{out.input}"
    )


def test_revoked_items_never_reach_the_model_in_a_real_run(tmp_path):
    """★★ 同一条不变量，走**真实运行**再验一遍：被撤回的那一步到不了模型面前。

    上面那条测的是"注入口做了什么"，这条测的是"模型**看到**了什么" —— 中间还隔着
    ``runner.model_input_filter`` 与 SDK 自己的一层处理（它会对过滤器返回的条目做
    ``deduplicate_input_items_preferring_latest``）。只测前者是不够的。
    """
    ctx = make_ctx(tmp_path)
    ctx.reflection.revoked.add("c1")
    model = ScriptedModel([
        call("save_text_asset", {"name": "n", "content": "c"}, "c1"),
        decide("答复。"),
    ])

    asyncio.run(_run_frontline("写一段文字", ctx=ctx, model=model))

    assert ctx.reflection.injected == 0          # 没有待注入结论 ⇒ 只是"滤掉"
    seen = _flatten(model.calls[1].input)
    assert "c1" not in seen, "被撤回的那一步仍然到了模型面前"
    assert "写一段文字" in seen


def test_unlocatable_step_stops_the_run_and_is_counted(monkeypatch, sink):
    """★ 那一步已经不在输入里（被裁剪 / 压缩吃掉）⇒ **第 3 级：停止重试**。

    ⚠️ 这条**取代**了早先"当次不退回、降级成一条提醒"的口径。改的理由是分类：
    「该退却退不了」（找不到 / 落盘类不可撤 / 额度用完）归**系统类**，而系统类的处置是
    "停止 + 给开发信号"（`PLAN_step5_fallback.md` §2）。降级成提醒等于**让它悄悄过去**——
    模型照样在同一个地方打转，只是我们不再管它了。

    它仍然是"能力退化必须看得见"的同一条：`agent_recovery_skipped_total{reason=not_found}`
    照旧计数，另外多一个 `agent_recovery_level_total{level=3}`（真的踩了刹车）。
    """
    ctx = TaskContext()
    ctx.reflection.pending.append(
        R.ReflectiveFinding(problem="loop", summary="在打转", step=7,
                            call_id="早被裁掉了", tool="kb_search", level=2)
    )
    md = _inputs({"role": "user", "content": "问"})

    with pytest.raises(R.AgentStoppedByGuard) as caught:
        asyncio.run(R.inject_reflections(md, context=ctx))

    assert caught.value.reason == "not_found", "抛出去的异常要带上停因"
    assert ctx.reflection.stopped == "not_found"
    assert ctx.reflection.revoked == set(), "没撤成却记了撤回"
    assert ctx.reflection.pending == [], "队列要清掉，否则下一轮又读一次同一份结论"
    assert [e[3] for e in sink.counters("agent_recovery_skipped_total")] == [
        {"reason": "not_found"}
    ]
    assert [e[3] for e in sink.counters("agent_recovery_level_total")] == [
        {"level": "3"}
    ]


def test_unlocatable_step_stops_even_when_the_note_budget_is_gone(sink):
    """定位不到 + 第 1 级提醒额度也用光了 ⇒ **照样停**。

    为什么值得单列：容易写成"没有额度了 ⇒ 原样返回"（第 4 步那版就是那样）。
    停只取决于"该撤却撤不成"，与提醒额度**无关** —— 两件事共用一个计数器就会写成这样。
    """
    ctx = TaskContext()
    ctx.reflection.injected = R.max_level1()      # 额度已用光
    ctx.reflection.pending.append(
        R.ReflectiveFinding(problem="loop", summary="在打转", step=7,
                            call_id="早被裁掉了", tool="kb_search", level=2)
    )
    md = _inputs({"role": "user", "content": "问"})

    with pytest.raises(R.AgentStoppedByGuard):
        asyncio.run(R.inject_reflections(md, context=ctx))

    assert ctx.reflection.stopped == "not_found"
    assert ctx.reflection.injected == R.max_level1(), "停止不该动提醒额度"
    assert [e[3] for e in sink.counters("agent_recovery_skipped_total")] == [
        {"reason": "not_found"}
    ]


def test_level_two_ignores_the_injection_budget():
    """★★ 第 2 级**不受**注入上限约束 —— 它本来就发生在注入额度用光之后。

    拿注入上限去卡撤回，退回就永远不会发生（而触发线正是"第 1 级上限 + 1"）。
    这条同时说明两级的"上限"是各自独立的两个数。
    """
    ctx = TaskContext()
    ctx.reflection.injected = R.max_level1()          # 额度已经用光
    ctx.reflection.pending.append(
        R.ReflectiveFinding(problem="loop", summary="在打转", step=1,
                            call_id="c1", tool="kb_search", level=2)
    )
    md = _inputs({"role": "user", "content": "问"}, *_tool_items("c1"))

    out = asyncio.run(R.inject_reflections(md, context=ctx))

    assert ctx.reflection.revoked == {"c1"}
    assert out.input[0] == {"role": "user", "content": "问"}
    assert len(out.input) == 2
    assert ctx.reflection.injected == R.max_level1(), "第 2 级不该动第 1 级的计数"
