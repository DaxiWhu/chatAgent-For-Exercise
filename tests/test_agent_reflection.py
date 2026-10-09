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


def test_no_more_fires_than_the_injection_cap(monkeypatch, sink):
    """发起次数不超过注入上限 —— 多发的结论没有位置可去（每次注入都会清空队列）。

    ⚠️ 这条同时钉住"发起"这件事**不会**因为反复调用 ``maybe_reflect`` 而失控：
    钩子每次工具结束都会调它一次，一个 12 轮的运行会调十几次。
    """
    _enable(monkeypatch, AGENT_REFLECT_AFTER_TOOLS=1, AGENT_REFLECT_MAX_LEVEL1=2)
    ctx = TaskContext()
    for i in range(1, 6):
        _do(ctx, "kb_search", json.dumps({"q": i}), i)
        _fire(ctx)

    assert sum(ctx.reflection.fired.values()) == 2, ctx.reflection.fired


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
