"""动作清单的守卫：步骤记录 / 成败分辨（第一阶段）+ 重复计数（第二阶段）。

守的是什么
----------
一个 agent 节点 = 一次完整的运行。这次运行里模型可以来回最多 ``AGENT_MAX_TURNS``
次，每次都可能在调工具 —— 这些全在节点内部，图不介入、不落检查点。于是从外面只
看得到"工具调了几次"，看不到"调得对不对"、"是不是在原地打转"。本组守的就是
**让它看得见**的那几小段：

* ``calls_from_response``：从模型响应里取"它请求了哪些工具"；
* ``StepLog``：按步展开的状态机（``begin`` / ``finish`` / ``fail``）；
* ``tool_error_handler``：把"失败"在**官方默认错误文本之外**多记一份，且**逐字节**
  不改回给模型的文本（自愈能力不能受影响）；
* 七处接线（三个 ``function_tool`` + 三个专家 ``as_tool`` + 一个评审 ``as_tool``）；
* ``StepLog.repeat_counts`` / ``max_repeat``：同一动作被做第二次起计入"重复"并播
  ``agent_action_repeats_total`` —— **"原地打转"的唯一判据**（见第九组）。

⚠️ 最值得盯的一条：**失败路径会被两个来源碰到**。官方先把异常转成一句给模型看的
话，之后 ``on_tool_end`` 照样触发 —— 所以"已经定格为 failed 的那一条"绝不能被
``finish`` 改写成 ok（那会自己把失败率抹平，且没有任何东西会报错）。

这一组的两条硬约束（写新用例时也适用）
-------------------------------------
1. **不许断言"失败文本长什么样"** —— 那句话是官方 ``default_tool_error_function``
   的产物，版本一升就可能变。这里只断言"与官方逐字节相同"。
2. **不许要求文件系统错误的具体消息** —— ``[WinError 183]`` / ``[Errno 17]`` 是
   平台差异，断言只看"有没有被记成 failed"。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("agents", reason="openai-agents 未安装 ⇒ agent 链路无从验证")

from agents.items import ModelResponse, Usage  # noqa: E402
from agents.tool import FunctionTool, default_tool_error_function  # noqa: E402

from src import observability  # noqa: E402
from src.agents import hooks as H  # noqa: E402
from src.agents.context import TaskContext  # noqa: E402
from src.agents.steps import (  # noqa: E402
    StepLog,
    calls_from_response,
    steps_enabled,
    tool_error_handler,
)
from tests._agents_testkit import (  # noqa: E402
    ScriptedModel,
    call,
    decide,
    make_ctx,
    say,
)

# --------------------------------------------------------------------------- #
# 夹具与替身
# --------------------------------------------------------------------------- #


class RecordingSink:
    """把指标原样记下来的观测出口替身。

    为什么不 monkeypatch ``observability.counter``：那会连"分发有没有被走到"一起
    换掉。本组要验的恰好是"清单播报了几次"，所以替身必须挂在 **sink** 这一层。
    """

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
    """装一个记录用的出口（conftest 的 autouse fixture 负责还原）。"""
    saved = observability.get_sink()
    rec = RecordingSink()
    observability.set_sink(rec)
    yield rec
    observability.set_sink(saved)


@pytest.fixture(autouse=True)
def _no_real_session(monkeypatch):
    """端到端那两条不经真会话后端。

    ``run_agent`` 不传 ``session_id`` 时本就不会取会话，但把 ``session_for`` 换掉
    可以让"以后有人顺手加了个默认会话"不变成"本机没有 PG ⇒ 那条用例炸在
    ``NoneType has no attribute get_items``"这种看不懂的失败。
    """
    from src.agents import runner as agrunner

    monkeypatch.setattr(agrunner, "session_for", lambda session_id: None)
    monkeypatch.delenv("REDIS_URL", raising=False)


def _pipeline() -> tuple[TaskContext, SimpleNamespace]:
    """返回 ``(真正的上下文, 钩子看到的那个包装对象)``。

    钩子/工具收到的都不是 ``TaskContext`` 本身，而是外层包装（``RunContextWrapper``
    / ``ToolContext``），都要通过 ``.context`` 才拿到它 —— 这里如实模拟这一层。
    """
    task = TaskContext()
    return task, SimpleNamespace(context=task)


def _tool(name: str) -> SimpleNamespace:
    """工具对象只需要有 ``name``（钩子只取它）。"""
    return SimpleNamespace(name=name)


def _installed_handler(tool):
    """★ 工具**实际生效**的错误处理函数（走 SDK 的公开解析函数）。

    刻意不读 ``_failure_error_function`` 这个私有字段：它是 SDK 的实现细节
    （pydantic 字段表里也确实只叫这个名字）。``resolve_...`` 是官方给出的解析入口，
    语义正是"这个工具运行期会用哪个格式化函数" —— 没装的话它返回官方默认实现。
    """
    from agents.tool import resolve_function_tool_failure_error_function

    return resolve_function_tool_failure_error_function(tool)


def _tool_context(task: TaskContext, name: str, arguments: str):
    """造一个工具执行时收到的上下文（``on_invoke_tool`` 的第一个参数）。"""
    from agents.tool_context import ToolContext

    return ToolContext(
        context=task,
        usage=Usage(),
        tool_name=name,
        tool_call_id="c1",
        tool_arguments=arguments,
    )


async def _run_frontline(prompt, *, ctx, model):
    """跑**前台那一次**运行（与整链测试里同名的 helper 口径一致）。"""
    from src.agents.runner import ENTRY_AGENT, run_agent

    return await run_agent(ENTRY_AGENT, prompt=prompt, context=ctx, model=model)


def _response(*items) -> ModelResponse:
    return ModelResponse(output=list(items), usage=Usage(), response_id=None)


def _begin(log: StepLog, tool: str, arguments: str = "{}", call_id: str = "c") -> None:
    """走一遍真实顺序：响应里登记 → 钩子开始 → （调用方再 finish / fail）。"""
    log.note_calls([(tool, arguments, call_id)])
    log.begin(tool)


# --------------------------------------------------------------------------- #
# 一、从模型响应里取"它请求了哪些工具"
# --------------------------------------------------------------------------- #
def test_calls_from_response_picks_function_calls_and_skips_text():
    """函数调用项被取出来、纯文本项被跳过 —— 顺序与定位编号都要对。

    定位编号（``call_id``）是将来"退回第几步"的唯一键，它在这一步就必须被留住：
    两个工具钩子都**不带**它，丢了就再也找不回来。
    """
    resp = _response(
        say("我先查一下资料。"),
        call("kb_search", {"query": "限流"}, "c1"),
        call("save_text_asset", {"name": "n", "content": "c"}, "c2"),
    )
    got = calls_from_response(resp)

    assert [g[0] for g in got] == ["kb_search", "save_text_asset"]
    assert [g[2] for g in got] == ["c1", "c2"]
    assert json.loads(got[0][1]) == {"query": "限流"}


def test_calls_from_response_tolerates_missing_output():
    """没有 ``output`` / 是 None / 是空表 ⇒ 空清单，且不抛异常。

    这几形态在真实链路上都会出现（例如一次只回文本的响应）。抛异常的表现是
    "整个 agent 请求崩在观测代码里"，所以宁可返回空。
    """
    assert calls_from_response(SimpleNamespace()) == []
    assert calls_from_response(SimpleNamespace(output=None)) == []
    assert calls_from_response(_response()) == []


def test_calls_from_response_does_not_depend_on_sdk_types():
    """★ 判据是**属性存在**，不是 `isinstance` —— 用假对象也要认得出来。

    为什么必须钉住：本项目走的是对话补全接口，转换层给出的形态与 Responses 接口
    那一侧不完全一致。若按类型判，症状是**静默漏掉全部调用**（"一步都没记上"），
    既不报错也没有指标反映 —— 正是本模块最该避免的那种失败。

    ⚠️ 这里刻意用 ``SimpleNamespace`` 当 response（真 ``ModelResponse`` 的 ``output``
    字段是带类型校验的，塞不进假项）—— 而"只做 getattr"正是被验的那件事。
    """
    fake = SimpleNamespace(name="t", arguments='{"a": 1}', call_id="cx")
    got = calls_from_response(SimpleNamespace(output=[fake]))

    assert got == [("t", '{"a": 1}', "cx")]


def test_calls_from_response_skips_items_without_name_or_arguments():
    """缺名字或缺 ``arguments`` 的项一律跳过（例如纯文本消息项）。"""
    got = calls_from_response(SimpleNamespace(output=[
        SimpleNamespace(name="good", arguments="{}", call_id="c"),
        SimpleNamespace(name="no_arguments"),          # 有名字、没有 arguments
        SimpleNamespace(arguments="{}"),               # 有 arguments、没有名字
        SimpleNamespace(name="", arguments="{}"),      # 名字是空串
    ]))

    assert [g[0] for g in got] == ["good"]


# --------------------------------------------------------------------------- #
# 二、钩子把两条线接上
# --------------------------------------------------------------------------- #
def test_on_llm_end_then_tool_start_keeps_the_arguments():
    """★ 响应 → 待执行 → 步骤：参数与定位编号**真的**落到了那一步上。

    这是两组钩子之间的唯一通道：工具钩子不带参数，参数只能由 ``on_llm_end`` 交给
    清单、再由 ``on_tool_start`` 取走。这条断了的表现是"参数指纹永远是空的"。
    """
    task, wrapper = _pipeline()

    asyncio.run(H.AGENT_HOOKS.on_llm_end(
        wrapper, None, _response(call("kb_search", {"query": "限流"}, "c9"))))
    asyncio.run(H.AGENT_HOOKS.on_tool_start(wrapper, None, _tool("kb_search")))

    assert len(task.steps.steps) == 1
    step = task.steps.steps[0]
    assert step.call_id == "c9"
    assert step.fingerprint, "参数指纹没落上来：on_llm_end → on_tool_start 的通道断了"


def test_on_tool_start_without_a_response_is_harmless():
    """钩子先到、响应后到（例如嵌套 run 的工具）⇒ 记一条空指纹的步骤，不抛异常。

    观测代码绝不能因为"顺序不理想"就把业务请求带崩。
    """
    task, wrapper = _pipeline()

    asyncio.run(H.AGENT_HOOKS.on_tool_start(wrapper, None, _tool("orphan")))

    step = task.steps.steps[0]
    assert (step.tool, step.fingerprint, step.call_id) == ("orphan", "", "")


# --------------------------------------------------------------------------- #
# 三、StepLog 状态机
# --------------------------------------------------------------------------- #
def test_finish_after_begin_is_ok():
    """``begin`` → ``finish`` ⇒ 一条 ``ok``，结果摘要是它的 detail。"""
    log = StepLog()
    _begin(log, "kb_search", '{"query": "限制"}', "c1")
    log.finish("kb_search", "命中 1 条")

    assert len(log.steps) == 1
    step = log.steps[0]
    assert (step.seq, step.tool, step.status) == (1, "kb_search", "ok")
    assert step.detail == "命中 1 条"
    assert step.duration >= 0.0


def test_fail_then_finish_stays_failed(sink):
    """★★ **本阶段最关键的一条**：失败定格之后 ``finish`` 不许把它改写成 ok。

    真实顺序就是这样（探针实测）：``on_tool_start`` → 工具抛异常 →
    ``failure_error_function`` 记一条 failed → 官方把异常转成文本 →
    ``on_tool_end`` **照样触发**。所以 ``finish`` 必然在失败之后再被调用一次。

    写成"``finish`` 总是按成功收尾"的后果：每一次工具失败都会被自己抹平成成功，
    失败率恒为 0 —— 而指标上看不出任何异常（"工具都成功了"）。
    顺带钉住：这一条只播报**一次**。
    """
    log = StepLog()
    _begin(log, "save_text_asset", '{"name": "n"}', "c1")
    log.fail("save_text_asset", ValueError("boom"))
    log.finish("save_text_asset", "An error occurred while running the tool. …")

    assert [s.status for s in log.steps] == ["failed"]
    assert log.steps[0].detail.startswith("boom"), "失败摘要被后来的结果覆盖了"
    assert len(sink.counters("agent_tool_result_total")) == 1, "同一步骤被播报了两次"


def test_a_settled_step_is_not_rewritten_or_duplicated():
    """★ 定格只发生一次：重复 ``finish`` 既不多记一条，也不改写已定的结论。

    真实的重复来源是"同一个工具的 ``on_tool_end`` 触发了不止一次"（SDK 版本差异、
    嵌套 run 都可能造成）。多记一条的表现是清单条数多于模型实际的调用次数，
    于是"平均每次运行调了几个工具"这类数整体偏大，而没有任何东西会报错。
    """
    log = StepLog()
    _begin(log, "t", "{}", "c1")
    log.finish("t", "第一次")
    log.finish("t", "第二次")

    assert len(log.steps) == 1
    assert log.steps[0].detail == "第一次"


def test_a_late_fail_cannot_rewrite_an_ok_step():
    """已定格的 ``ok`` 不会被迟到的 ``fail`` 改写（改写的后果是"成功变失败"）。

    ⚠️ 真实链路上不会出现这一对 —— 失败处理函数总在 ``on_tool_end`` **之前**被调用。
    这里把"定格只发生一次"这条不变量单独演一遍：迟到的那条按 ``fail`` 的补记语义
    **另记一条**，但第一条的结论分毫不动。
    """
    log = StepLog()
    _begin(log, "t", "{}", "c1")
    log.finish("t", "正常结果")
    log.fail("t", ValueError("迟到的失败"))

    assert log.steps[0].status == "ok"
    assert log.steps[0].detail == "正常结果"


def test_fail_without_begin_is_still_recorded():
    """★ 没有 ``begin`` 对应的失败也要留下一条 —— 不许静默丢弃。

    这一类真实存在（工具在"开始执行"之前就失败的路径）。丢弃它的表现是"失败数比
    模型实际遇到的少"，而清单里看不出少过什么。补记的这条同样占据一个 ``seq``。
    """
    log = StepLog()
    log.fail("kb_search", RuntimeError("参数没解析出来"))

    assert [s.tool for s in log.steps] == ["kb_search"]
    assert [s.status for s in log.steps] == ["failed"]
    assert log.steps[0].seq == 1
    assert "参数没解析出来" in log.steps[0].detail


def test_a_failed_step_is_not_finished_into_a_new_ok_step():
    """失败之后再来 ``finish`` ⇒ **不多记一条**（"宁少不错"）。

    这条与上面那条失败定格是一对：失败路径上 ``on_tool_end`` 一定还会触发一次。
    若那时"找不到未定格的步骤就补记一条"（看起来更"完整"的写法），一次失败就会
    变成"一条 failed + 一条 ok" —— 清单条数与失败率同时失真，且症状是"指标正常"。
    """
    log = StepLog()
    _begin(log, "t", "{}", "c1")
    log.fail("t", ValueError("boom"))
    log.finish("t", "错误文本")

    assert [s.status for s in log.steps] == ["failed"]


def test_a_finish_without_begin_is_dropped_not_invented():
    """只有 ``finish``、没有 ``begin`` ⇒ 什么都不记（而不是补一条无起点的 ok）。"""
    log = StepLog()
    log.finish("ghost", "结果")

    assert log.steps == []


def test_failed_property_lists_only_failures():
    """``failed`` 只列失败的步骤（兜底出口要用它列"未完成清单"）。"""
    log = StepLog()
    _begin(log, "a")
    log.finish("a", "好")
    _begin(log, "b")
    log.fail("b", ValueError("坏"))
    _begin(log, "c")
    log.finish("c", "好")

    assert [s.tool for s in log.failed] == ["b"]


def test_steps_are_numbered_in_order():
    """``seq`` 从 1 开始、按发生顺序递增（"第几步"是退回机制的基础）。"""
    log = StepLog()
    for name in ("a", "b", "c"):
        _begin(log, name)
        log.finish(name, "ok")

    assert [s.seq for s in log.steps] == [1, 2, 3]


def test_as_list_is_json_serializable_for_the_fallback_exit():
    """``as_list()`` 必须能直接进 JSON —— 兜底出口要把它塞进响应。

    ``ToolStep`` 是 dataclass（不是可序列化类型），所以导出必须是**普通 dict**；
    这条同时钉住字段名（下游按这些名字取）。
    """
    log = StepLog()
    _begin(log, "kb_search", '{"query": "q"}', "c1")
    log.finish("kb_search", "命中")
    _begin(log, "save_text_asset", '{"name": "n"}', "c2")
    log.fail("save_text_asset", ValueError("坏"))

    rows = log.as_list()
    assert [r["status"] for r in rows] == ["ok", "failed"]
    assert set(rows[0]) == {"seq", "tool", "fingerprint", "call_id",
                            "status", "reason", "detail", "duration"}
    json.dumps(rows)      # 不能抛
    assert rows[0]["call_id"] == "c1"
    assert rows[0]["fingerprint"]
    # 成功那条不带失败档位；失败那条必须带上，否则下游分不出该改描述还是改代码
    assert rows[0]["reason"] == ""
    assert rows[1]["reason"] == "error"


def test_detail_is_clipped_to_one_line():
    """detail 是**摘要**：压成一行并截断（原文不进日志）。"""
    log = StepLog()
    _begin(log, "t")
    log.finish("t", "第一行\n第二行\t带制表符 " + "长" * 500)

    detail = log.steps[0].detail
    assert "\n" not in detail and "\t" not in detail
    assert len(detail) <= 200


# --------------------------------------------------------------------------- #
# 四、参数指纹（"同一动作被反复调用"的判据）
# --------------------------------------------------------------------------- #
def test_same_arguments_in_a_different_key_order_share_a_fingerprint():
    """★ 键序不同、参数相同 ⇒ **同一个**指纹。

    指纹不加规范化就会这样失效：模型每次换个键序，同一动作看起来就是两个不同的
    动作，"重复调用"永远数不出来 —— 而这个数正是下一步（重复计数）的全部输入。
    """
    log = StepLog()
    _begin(log, "kb_search", '{"query": "限流", "top_k": 3}', "c1")
    log.finish("kb_search", "ok")
    _begin(log, "kb_search", '{"top_k": 3, "query": "限流"}', "c2")
    log.finish("kb_search", "ok")

    first, second = (s.fingerprint for s in log.steps)
    assert first and first == second


def test_different_arguments_get_different_fingerprints():
    """参数不同 ⇒ 指纹必须不同（否则"重复"会变成误报）。"""
    log = StepLog()
    _begin(log, "kb_search", '{"query": "限流"}', "c1")
    log.finish("kb_search", "ok")
    _begin(log, "kb_search", '{"query": "配额"}', "c2")
    log.finish("kb_search", "ok")

    first, second = (s.fingerprint for s in log.steps)
    assert first != second


def test_the_same_call_keeps_one_fingerprint_even_with_another_call_id():
    """同参数、不同定位编号 ⇒ 同一个指纹（指纹刻意**不含** ``call_id``）。

    ``call_id`` 每次调用都不同，把它算进指纹等于让"重复检测"永远为 0。
    """
    log = StepLog()
    _begin(log, "save_text_asset", '{"name": "n", "content": "c"}', "c1")
    log.finish("save_text_asset", "ok")
    _begin(log, "save_text_asset", '{"name": "n", "content": "c"}', "c2")
    log.finish("save_text_asset", "ok")

    assert log.steps[0].fingerprint == log.steps[1].fingerprint
    assert log.steps[0].call_id != log.steps[1].call_id


def test_invalid_json_arguments_do_not_raise():
    """★ 参数不是合法 JSON ⇒ 照常记一条，**不抛异常**。

    这一步完全可能发生（模型给出坏参数就是这个样子），而它在观测代码里抛异常的
    表现是"整个请求崩在记录步骤的时候"。
    """
    log = StepLog()
    _begin(log, "save_text_asset", "{not json", "c1")
    log.fail("save_text_asset", ValueError("Invalid JSON input"))
    log.finish("save_text_asset", "错误文本")

    step = log.steps[0]
    assert step.fingerprint and step.call_id == "c1"
    assert step.status == "failed"


def test_pending_calls_are_matched_by_tool_name():
    """待执行的调用按**工具名**配对，不是按入队顺序。

    模型在一轮里同时请求两个不同工具时，谁先执行不由入队顺序决定。按顺序取会让
    参数与步骤**张冠李戴**（"哪个参数导致了失败"从此全是错的），按名字取至少保证
    工具名是对的。同名工具并发的那一档配不准，是模块 docstring 里写明的已知限制。
    """
    log = StepLog()
    log.note_calls([
        ("kb_search", '{"query": "先登记"}', "c1"),
        ("save_text_asset", '{"name": "后登记"}', "c2"),
    ])

    log.begin("save_text_asset")          # 后登记的先执行

    assert log.steps[0].call_id == "c2", "按入队顺序配对了（张冠李戴）"
    log.begin("kb_search")
    assert log.steps[1].call_id == "c1"


# --------------------------------------------------------------------------- #
# 五、错误处理函数：既保住自愈，又多记一条失败
# --------------------------------------------------------------------------- #
def test_error_handler_returns_the_official_text_unchanged():
    """★★ 回给模型的文本与官方默认实现**逐字节相同**。

    为什么这条这么重要：那句话是模型**自愈**的唯一依据（官方专门为 JSON 解析失败
    写了另一句措辞）。这里若自作主张改写（"工具失败了，请停止重试"之类），模型就
    失去了"看错误信息改参数再试一次"的能力 —— 而这件事在测试里不会自己暴露，
    只会表现为"线上工具失败率一高，整条链就答不出来"。

    ⇒ 所以这里不做"要么官方、要么我"的选择，而是**官方之外多记一份**。
    """
    _task, wrapper = _pipeline()
    handler = tool_error_handler("kb_search")

    assert handler(wrapper, ValueError("boom")) == default_tool_error_function(
        wrapper, ValueError("boom"))


def test_error_handler_records_exactly_one_failed_step():
    """同一次调用里记下**恰好一条** failed，摘要里含错误信息。"""
    task, wrapper = _pipeline()

    tool_error_handler("kb_search")(wrapper, ValueError("boom"))

    assert [s.tool for s in task.steps.steps] == ["kb_search"]
    assert [s.status for s in task.steps.steps] == ["failed"]
    assert "boom" in task.steps.steps[0].detail


def test_error_handler_still_talks_to_the_official_function_when_disabled(monkeypatch):
    """★ 关掉记录时：清单为空，**但回给模型的文本分毫不动**。

    这是"开关只影响观测"这条边界的守卫 —— 如果关掉开关会改变错误文本（比如变成
    空串），那这个开关就成了"能改变业务行为的观测开关"，最坏的一种形态。
    """
    monkeypatch.setenv("AGENT_TOOL_STEPS", "0")
    task, wrapper = _pipeline()

    text = tool_error_handler("kb_search")(wrapper, ValueError("boom"))

    assert text == default_tool_error_function(wrapper, ValueError("boom"))
    assert task.steps.steps == []


# --------------------------------------------------------------------------- #
# 六、开关
# --------------------------------------------------------------------------- #
def test_steps_enabled_defaults_to_on(monkeypatch):
    """★ 默认**开**（纯观察能力，默认关等于"这个数永远没有数据"）。"""
    monkeypatch.delenv("AGENT_TOOL_STEPS", raising=False)

    assert steps_enabled() is True


def test_steps_enabled_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_STEPS", "0")

    assert steps_enabled() is False


def test_every_write_is_a_noop_when_disabled(monkeypatch, sink):
    """★ 关掉时**每一个**写入口都不产生步骤 —— 不允许只关一半。

    只关一半的表现最难查：清单里出现"只有开始没有结果"的条目，看板上失败率、
    耗时全是错的，而"开关已经关了"这件事让所有人先去怀疑别的地方。
    """
    monkeypatch.setenv("AGENT_TOOL_STEPS", "0")
    log = StepLog()

    log.note_calls([("t", "{}", "c1")])
    log.begin("t")
    log.finish("t", "结果")
    log.fail("t", ValueError("坏"))

    assert log.steps == [] and log.as_list() == []
    assert sink.counters("agent_tool_result_total") == []


# --------------------------------------------------------------------------- #
# 七、接线：七处都不能漏
# --------------------------------------------------------------------------- #
def test_all_tools_that_can_fail_install_a_handler():
    """★ 八处接线**一个都不能漏**（八个 ``function_tool`` + 四个 ``as_tool``）。

    2026-10-30 补了四个只读工具（get_current_time / calculate / list_assets /
    read_asset），所以 ``function_tool`` 那一侧从四个变成八个；下面第 3 段把"哪些
    工具该装"写成了显式清单，新增工具不会因为这段 docstring 没改而漏过去。

    为什么按清单数而不是抽查：漏掉一处的症状是**那一类失败彻底不可见** ——
    "专家整体失败"（结构化产出解析不出来）与"专家内部某个工具失败"完全是两回事，
    只覆盖后者的话，前者会静默消失在"专家被调了几次"里。

    判据是"**该工具运行期用的不是官方默认格式化函数**"（走 SDK 的公开解析函数），
    而不是"参数传没传"这种看代码的事 —— 后者挡不住"以为装了、其实传的是 None"。
    """
    from src.agents.frontline import frontline_agent
    from src.agents.tools import deliver, generate_image, kb_search, save_text_asset

    def installed(tool) -> bool:
        handler = _installed_handler(tool)
        return handler is not None and handler is not default_tool_error_function

    # 1. 四个普通工具（kb_search 不在前台工具表里，它在 researcher 内部）
    for tool in (kb_search, save_text_asset, generate_image, deliver):
        assert installed(tool), f"{tool.name} 没装错误处理函数"

    # 2. 前台工具表里那四个（三个专家 + 评审）
    on_frontline = {t.name: t for t in frontline_agent.tools}
    expected = {"research", "build_slide_deck", "make_illustration", "review_draft"}
    assert expected <= set(on_frontline), set(on_frontline)
    for name in sorted(expected):
        assert installed(on_frontline[name]), f"{name} 没装错误处理函数"

    # 3. 前台工具表里"装了处理函数"的恰好是这些 —— 将来新增一个能失败的工具
    #    却忘了装，这条立刻红；反过来多装了一个也要有人看一眼。
    #    2026-10-30 加了四个只读小工具（时间 / 算术 / 资产回读）：它们同样可能失败
    #    （非法时区、除零、路径穿越），失败文案同样要能进步骤清单，所以都装了。
    #    2026-11-02 加了长期记忆的 remember / forget：写库会失败（没 DSN、无归属、
    #    内容为空），而那几种失败必须能被"这一步成没成"看见。
    #    2026-11-04 加了 recall_memory：查库 / 查索引都会失败，而且它的失败**不能**
    #    伪装成"没有相关记忆"（那会让模型断言"用户没说过这件事"）—— 正是必须走
    #    错误处理函数的典型。
    assert {n for n, t in on_frontline.items() if installed(t)} == expected | {
        "save_text_asset",
        "deliver",
        "get_current_time",
        "calculate",
        "list_assets",
        "read_asset",
        "remember",
        "forget",
        "recall_memory",
    }


def test_render_slide_deck_is_deliberately_not_a_tool():
    """★ ``render_slide_deck`` 是**纯函数**，不该被包成工具。

    它是确定性步骤（没有任何模型需要"决定要不要渲染"）。包成工具只会给模型多一个
    可以忘掉的选项 —— 那正是"交付物偶发缺失"的来源。所以它没有、
    也不该有 ``failure_error_function``。
    """
    from src.agents.tools import render_slide_deck

    assert callable(render_slide_deck)
    assert not isinstance(render_slide_deck, FunctionTool)


def test_the_handler_knows_which_tool_it_belongs_to():
    """★★ 闭包捕获的工具名必须**等于工具自己的名字**。

    为什么需要单独一条：上面那条只断言"装上了"，而"装上了但名字写错"
    （``tool_error_handler("ks_search")`` 这种手滑）在别处**完全看不出来** ——
    失败照样被记下，只是记到了另一个名字下，于是按工具名聚合的失败率一半对它、
    一半对别人。

    做法是**用工具自己**跑一次真实的失败（参数不是合法 JSON），而不是拿
    ``tool_error_handler(...)`` 另造一个：后者只能证明工厂函数是对的，证明不了
    装在工具上的就是它。顺序照真实链路走：``on_tool_start`` → 工具失败 → ``on_tool_end``。
    """
    from src.agents.tools import kb_search

    task, wrapper = _pipeline()
    tool_ctx = _tool_context(task, "kb_search", "{not json")

    asyncio.run(H.AGENT_HOOKS.on_tool_start(wrapper, None, kb_search))
    text = asyncio.run(kb_search.on_invoke_tool(tool_ctx, "{not json"))
    asyncio.run(H.AGENT_HOOKS.on_tool_end(wrapper, None, kb_search, text))

    assert [s.tool for s in task.steps.steps] == ["kb_search"]
    assert [s.status for s in task.steps.steps] == ["failed"], text


def test_hooks_implement_both_lines():
    """★ 钩子类必须同时实现"统计（按量）"与"动作清单（按步）"两条线的入口。

    漏实现一个的表现是**那个回调静默不触发**：清单里少掉全部参数指纹，或者失败
    永远停在 ``running`` —— 都没有任何报错。
    """
    for name in ("on_llm_start", "on_llm_end", "on_tool_start", "on_tool_end"):
        method = getattr(H.AGENT_HOOKS, name, None)
        assert method is not None, f"钩子缺 {name}"
        assert inspect.iscoroutinefunction(method), f"{name} 必须是 async"


def test_task_context_carries_a_step_log():
    """清单挂在**上下文**上（与 ``stats`` 同一处），且默认就是可用的空清单。"""
    assert isinstance(TaskContext().steps, StepLog)


# --------------------------------------------------------------------------- #
# 八、端到端：一次真实运行之后，清单里到底有什么
# --------------------------------------------------------------------------- #
def test_a_successful_tool_leaves_one_ok_step(tmp_path):
    """一次真实运行里工具成功 ⇒ 一条 ``ok``，摘要就是工具返回值（资产路径）。"""
    ctx = make_ctx(tmp_path)
    model = ScriptedModel([
        call("save_text_asset", {"name": "note", "content": "你好"}, "c1"),
        decide("答复。"),
    ])

    out = asyncio.run(_run_frontline("写一段文字", ctx=ctx, model=model))
    model.assert_complete()

    assert [s.status for s in ctx.steps.steps] == ["ok"]
    assert ctx.steps.steps[0].call_id == "c1"
    assert out.answer == "答复。"


def test_a_failing_tool_leaves_exactly_one_failed_step(tmp_path, sink):
    """★★ 端到端：工具在执行期抛异常 ⇒ 恰好一条 ``failed``，且模型仍能收尾。

    这条把三件事一次性钉住（每一件都是"不做成断言就看不出来"的）：

    1. **失败真的被记下来了** —— 失败路径上 ``on_tool_end`` 照样触发，而它的
       ``result`` 就是那句错误文本；只有"错误处理函数先定格"这一条路能分辨出来；
    2. **没有多出一条假的 ok** —— 条数必须是 1；
    3. **自愈没受影响** —— 模型收到官方那句错误文本后照常给出答复
       （``out.answer`` 不为空、``exit_reason`` 不是 ``error``）。

    制造失败的办法是让落盘目录不可用（``asset_dir`` 指向一个**文件**）。
    刻意**不断言**错误消息的内容：``[WinError 183]`` 与 ``[Errno 17]`` 是平台差异。
    """
    blocked = tmp_path / "blocked"
    blocked.write_text("not a dir", encoding="utf-8")
    ctx = make_ctx(tmp_path)
    ctx.asset_dir = blocked
    model = ScriptedModel([
        call("save_text_asset", {"name": "note", "content": "你好"}, "c1"),
        decide("答复。"),
    ])

    out = asyncio.run(_run_frontline("写一段文字", ctx=ctx, model=model))
    model.assert_complete()

    assert len(ctx.steps.steps) == 1, f"失败被记成了 {len(ctx.steps.steps)} 条：{ctx.steps.as_list()}"
    step = ctx.steps.steps[0]
    assert (step.tool, step.status) == ("save_text_asset", "failed")
    assert step.detail and "\n" not in step.detail
    assert [s.tool for s in ctx.steps.failed] == ["save_text_asset"]
    assert len(sink.counters("agent_tool_result_total")) == 1, "播报次数不是 1"
    assert out.answer == "答复。"
    assert out.exit_reason != "error"


# --------------------------------------------------------------------------- #
# 九、重复计数（"原地打转"的唯一判据）
# --------------------------------------------------------------------------- #
# 为什么盯这一组：打转时**总工具次数不高、重量级动作也不多**（模型在两个动作之间
# 来回试），所以"跑了多久""调了几个重家伙"都看不出异常。只有"同一个动作被做过
# 第二次"能把它暴露出来 —— 这里守的就是这个信号既**不缺席**、也**不误报**。
#
# 一个动作 = **工具名 + 规范化参数**。按工具名数会把"换个关键词多查几轮"误报成
# 打转；按参数数才对。所以下面既验"相同的认出来"，也验"不同的别认成一样"。
def _do(log: StepLog, tool: str, arguments: str, idx: int, *, ok: bool = True) -> None:
    """走一遍真实顺序做一次动作（成功或失败）。"""
    _begin(log, tool, arguments, f"c{idx}")
    if ok:
        log.finish(tool, f"结果{idx}")
    else:
        log.fail(tool, ValueError(f"坏{idx}"))
        log.finish(tool, "错误文本")


def test_no_repeat_when_every_action_differs(sink):
    """每个动作都不一样 ⇒ 没有重复：清单为空、``max_repeat`` 为 0、**一次都不播**。

    这是"不误报"那一侧。播了不该播的，看板上的"打转"就全是噪声，真打转时反而
    被淹掉 —— 而这两个状态（真打转 / 没打转）在指标上长得一模一样。
    """
    log = StepLog()
    _do(log, "kb_search", '{"query": "a"}', 1)
    _do(log, "kb_search", '{"query": "b"}', 2)
    _do(log, "save_text_asset", '{"name": "n"}', 3)

    assert log.repeat_counts() == {}
    assert log.max_repeat == 0
    assert sink.counters("agent_action_repeats_total") == []


def test_a_second_identical_action_counts_and_broadcasts_once(sink):
    """同一动作做第二次 ⇒ 计入重复（``max_repeat == 2``）并**恰好播一次**。"""
    log = StepLog()
    _do(log, "kb_search", '{"query": "同一个"}', 1)
    _do(log, "kb_search", '{"query": "同一个"}', 2)

    assert list(log.repeat_counts().values()) == [2]
    assert log.max_repeat == 2
    assert len(sink.counters("agent_action_repeats_total")) == 1


def test_every_extra_call_broadcasts_again(sink):
    """第三次、第四次接着播（口径：**出现第二次起，每多一次算一次重复**）。

    为什么不"达到阈值才播一次"：指标是累计量，每次多出来的一份都要留下痕迹，
    阈值判断留给读它的人（反思闸门要按 ``AGENT_REFLECT_ON_REPEAT`` 决定何时介入）。
    若只在跨过阈值那一下播，重复数从 3 涨到 8 的过程就完全看不见了。
    """
    log = StepLog()
    for i in range(1, 5):
        _do(log, "kb_search", '{"query": "同一个"}', i)

    assert log.max_repeat == 4
    assert len(sink.counters("agent_action_repeats_total")) == 3


def test_a_different_key_order_still_counts_as_the_same_action(sink):
    """★ 参数相同、**键序不同** ⇒ 仍然是同一个动作，重复要被认出来。

    这是"不缺席"那一侧最实际的失效方式：模型每轮把键序换一下，同一动作看起来
    就是两个动作 —— 打转信号**恒为 0**，而且没有任何东西会报错。指纹里的规范化
    （JSON 键排序）是唯一挡住它的地方。
    """
    log = StepLog()
    _do(log, "kb_search", '{"query": "限流", "top_k": 3}', 1)
    _do(log, "kb_search", '{"top_k": 3, "query": "限流"}', 2)

    assert log.max_repeat == 2
    assert len(sink.counters("agent_action_repeats_total")) == 1


def test_different_arguments_are_not_a_repeat(sink):
    """参数不同 ⇒ **不算**重复（"换个关键词再查一次"是正常的，不是打转）。"""
    log = StepLog()
    _do(log, "kb_search", '{"query": "限流"}', 1)
    _do(log, "kb_search", '{"query": "配额"}', 2)

    assert log.repeat_counts() == {}
    assert sink.counters("agent_action_repeats_total") == []


def test_the_same_arguments_on_a_different_tool_are_not_a_repeat(sink):
    """参数一模一样、但工具不同 ⇒ **不算**重复（指纹里含工具名）。

    反例很现实：``kb_search({"query": "x"})`` 与 ``save_text_asset({"query": "x"})``
    是两件完全不同的事。指纹若不绑工具名，这两条会被算成"同一个动作做了两次"。
    """
    log = StepLog()
    _do(log, "kb_search", '{"q": "same"}', 1)
    _do(log, "save_text_asset", '{"q": "same"}', 2)

    assert log.repeat_counts() == {}
    assert sink.counters("agent_action_repeats_total") == []


def test_a_failed_action_still_counts_as_a_repeat(sink):
    """重复的判据是"**动作**又做了一次"，与它成没成**无关** —— 失败也要计入。

    反而更该计入：同一次调用失败后原样重试，正是打转最常见的形态。若只数成功的，
    最该报警的那种"一直在失败和重试之间撞墙"会恰好被排除在外。
    """
    log = StepLog()
    _do(log, "generate_image", '{"prompt": "p"}', 1)
    _do(log, "generate_image", '{"prompt": "p"}', 2, ok=False)

    assert log.max_repeat == 2
    assert len(sink.counters("agent_action_repeats_total")) == 1


def test_blank_fingerprint_never_counts_as_a_repeat(sink):
    """★ 没有指纹的步骤（没配到参数）**不参与**重复计数。

    空指纹是"这一条没拿到参数"的记号，不是"同一动作"的证据。把它们按同一个值
    归并，三条互不相干的孤立步骤会凭空变成"重复了三次" —— 而这正是最容易被当成
    真信号去查的那种假警报。
    """
    log = StepLog()
    for _ in range(3):
        log.begin("orphan")            # 没有 note_calls ⇒ 指纹为空
        log.finish("orphan", "ok")

    assert [s.fingerprint for s in log.steps] == ["", "", ""]
    assert log.repeat_counts() == {}
    assert log.max_repeat == 0
    assert sink.counters("agent_action_repeats_total") == []


def test_repeat_counts_only_lists_actions_done_twice(sink):
    """``repeat_counts`` 的键只有"做过两次以上"的动作 —— 只做一次的不列。"""
    log = StepLog()
    _do(log, "kb_search", '{"query": "重复的"}', 1)
    _do(log, "kb_search", '{"query": "重复的"}', 2)
    _do(log, "kb_search", '{"query": "只做一次的"}', 3)

    assert list(log.repeat_counts().values()) == [2]
    assert len(log.steps) == 3, "清单要留住全部步骤，只有统计口径筛掉一次的"


def test_broadcasts_agree_with_the_readable_counts(sink):
    """★ 播报的次数与"读出来的重复数"**同源**：播报次数 == Σ(每个动作次数 − 1)。

    为什么不厌其烦对这条账：这两处一旦分家（一处按定格前的清单算、另一处按别的
    口径算），看板上"播了几次"与闸门"读到的重复数"就会对不上 —— 而两边各自都
    "看着正常"，没有任何东西会报错。这条把它们钉成一个数。
    """
    log = StepLog()
    for i in range(1, 4):                       # x 做 3 次 ⇒ 播 2
        _do(log, "kb_search", '{"query": "x"}', i)
    for i in range(4, 6):                       # y 做 2 次 ⇒ 播 1
        _do(log, "kb_search", '{"query": "y"}', i)
    _do(log, "kb_search", '{"query": "z"}', 6)  # z 做 1 次 ⇒ 播 0

    counts = log.repeat_counts()
    assert sorted(counts.values()) == [2, 3]
    assert sum(n - 1 for n in counts.values()) == 3
    assert len(sink.counters("agent_action_repeats_total")) == 3


def test_repeat_is_not_broadcast_when_disabled(monkeypatch, sink):
    """关掉步骤记录 ⇒ 重复计数一起关（**跟随** ``AGENT_TOOL_STEPS``，没有独立开关）。"""
    monkeypatch.setenv("AGENT_TOOL_STEPS", "0")
    log = StepLog()
    _do(log, "kb_search", '{"query": "同一个"}', 1)
    _do(log, "kb_search", '{"query": "同一个"}', 2)

    assert log.steps == []
    assert log.repeat_counts() == {} and log.max_repeat == 0
    assert sink.counters("agent_action_repeats_total") == []


# --------------------------------------------------------------------------- #
# 十、失败分两档：该改描述，还是该改代码
# --------------------------------------------------------------------------- #
# 为什么盯这一组：两种失败的**处置相反**。
#   * 模型照着工具定义也填不出合法参数 ⇒ 要改的是描述或参数结构；
#   * 参数是对的、工具跑起来之后坏 ⇒ 要查实现或依赖。
# 合并成 "failed" 一档时，看板只说"这个工具爱报错"，值班的人得先翻日志才知道该动
# 哪一边 —— 而这个分类的全部价值就是省掉那一步。
def _validation_error() -> Exception:
    """造一个**真实的** pydantic 校验失败（不是手工拼的假对象）。"""
    from pydantic import BaseModel

    class _Args(BaseModel):
        name: str

    try:
        _Args(name=123)          # type: ignore[arg-type]
    except Exception as exc:     # noqa: BLE001  这里要的就是那个异常本身
        return exc
    raise AssertionError("构造不出校验失败")


def test_bad_arguments_are_told_apart_from_execution_failures(sink):
    """★ 参数错与执行错分成两档，且**播报里也带着这一档**。

    只写进清单、不进指标是不行的：清单只活在一次运行内部，运维看不到它；指标里
    没有这个维度，看板就永远只有"failed"。
    """
    from agents.exceptions import ModelBehaviorError

    log = StepLog()
    _begin(log, "kb_search", '{"query": 1}', "c1")
    log.fail("kb_search", ModelBehaviorError("Invalid JSON input for tool kb_search"))
    _begin(log, "save_text_asset", '{"name": 1}', "c2")
    log.fail("save_text_asset", _validation_error())
    _begin(log, "generate_image", '{"prompt": "p"}', "c3")
    log.fail("generate_image", OSError("磁盘满"))

    assert [s.reason for s in log.steps] == ["bad_args", "bad_args", "error"]
    reasons = [e[3].get("reason") for e in sink.counters("agent_tool_result_total")]
    assert reasons == ["bad_args", "bad_args", "error"]


def test_a_successful_call_carries_an_empty_reason(sink):
    """成功那条不带失败档位 —— 否则看板上"成功"会被拆成好几个无意义的序列。"""
    log = StepLog()
    _begin(log, "kb_search", '{"query": "q"}', "c1")
    log.finish("kb_search", "命中")

    assert log.steps[0].reason == ""
    assert sink.counters("agent_tool_result_total")[0][3]["reason"] == ""


def test_failure_reason_looks_at_the_type_not_the_text():
    """★ 判据必须是**异常类型**：按错误文本匹配会在 SDK 升版后静默全判成 error。

    这条专门钉住那个退化写法 —— 下面第二个断言里的文本与第一个完全相同，但类型
    不是 ``ModelBehaviorError``，所以它必须落到 ``error``。写成关键词匹配的版本
    会把它判成 ``bad_args``，而症状是"分类看着有数、其实全靠字符串猜"。
    """
    from agents.exceptions import ModelBehaviorError

    from src.agents.steps import _failure_reason

    assert _failure_reason(ModelBehaviorError("Invalid JSON input")) == "bad_args"
    assert _failure_reason(RuntimeError("Invalid JSON input")) == "error"
    assert _failure_reason(_validation_error()) == "bad_args"


def test_the_failed_reason_survives_a_later_finish(sink):
    """失败定格之后 ``on_tool_end`` 又来了（官方一定会来）⇒ 档位不许被抹成空串。"""
    log = StepLog()
    _begin(log, "save_text_asset", '{"name": "n"}', "c1")
    log.fail("save_text_asset", OSError("坏"))
    log.finish("save_text_asset", "官方转述的错误文本")

    assert log.steps[0].status == "failed"
    assert log.steps[0].reason == "error"
    assert len(sink.counters("agent_tool_result_total")) == 1


# --------------------------------------------------------------------------- #
# 十一、曝光：一次没被选过的工具也必须能在指标里看见
# --------------------------------------------------------------------------- #
# 为什么盯这一组：结果计数器只在**被调用时**产生时间序列。所以"挂了三个月一次没用"
# 与"这个部署根本没有它"在 /metrics 上是同一个样子 —— **空白**。空白不是 0，
# 它没法被查询、也不会出现在任何看板上。曝光计数就是那个分母。
def test_exposure_is_recorded_for_every_tool_on_the_agent(sink):
    """每个挂上的工具各记一次曝光。"""
    log = StepLog()
    log.note_exposed(["research", "build_slide_deck", "evaluate_draft_tool"])

    rows = sink.counters("agent_tool_exposed_total")
    assert [e[3]["tool"] for e in rows] == ["research", "build_slide_deck", "evaluate_draft_tool"]
    assert all(e[2] == 1 for e in rows)


def test_exposure_skips_blank_and_duplicate_names(sink):
    """空名与重复名不许各算一次 —— 重复会把选中率的分母抬高，选中率随之系统性偏低。"""
    log = StepLog()
    log.note_exposed(["kb_search", "", "kb_search"])

    assert len(sink.counters("agent_tool_exposed_total")) == 1


def test_a_tool_that_was_never_selected_still_shows_up(sink):
    """★ 本次改动的目的：曝光有值、结果没有 ⇒ "一次没选过"是一个可查的数。

    挂在 agent 上的两个工具只用了其中一个，另一个必须**仍然在指标里出现**
    （以曝光的形式）。这正是改之前看不见的那一侧。
    """
    log = StepLog()
    log.note_exposed(["research", "make_illustration"])
    _begin(log, "research", '{"question": "q"}', "c1")
    log.finish(log.steps[0].tool, "结论")

    exposed = {e[3]["tool"] for e in sink.counters("agent_tool_exposed_total")}
    selected = {e[3]["tool"] for e in sink.counters("agent_tool_result_total")}
    assert exposed == {"research", "make_illustration"}
    assert selected == {"research"}
    assert "make_illustration" not in selected


def test_on_agent_start_records_exposure(sink):
    """★ 接线：钩子的 agent 开始回调必须真的登记曝光。

    只测 ``note_exposed`` 而不测接线，等于"方法对了但没接上"—— 这种情况全绿，
    而指标永远没有数据。本文件里同类接线（``on_agent_start`` 记 agent 名）也有守卫。
    """
    task, wrapper = _pipeline()
    agent = SimpleNamespace(name="frontline_agent",
                            tools=[_tool("research"), _tool("save_text_asset")])

    asyncio.run(H.AGENT_HOOKS.on_agent_start(wrapper, agent))

    names = [e[3]["tool"] for e in sink.counters("agent_tool_exposed_total")]
    assert names == ["research", "save_text_asset"]


def test_exposure_is_silent_when_disabled(monkeypatch, sink):
    """关掉步骤记录 ⇒ 曝光也一起关（跟随同一个开关，没有独立开关）。"""
    monkeypatch.setenv("AGENT_TOOL_STEPS", "0")
    StepLog().note_exposed(["kb_search"])

    assert sink.counters("agent_tool_exposed_total") == []


# --------------------------------------------------------------------------- #
# 六、单步超时（2026-10-21）
#
# 这一节钉的是**链路**，不是"能不能超时"（那在 `test_toolpool.py`）：
#   超时 ⇒ 记一条 failed（独立档位） ⇒ 回给模型一句可操作的话
#        ⇒ 同一动作反复超时 ⇒ 计入重复 ⇒ 由反思闸门按既有阈值熔断。
# 一段链断在任何一处，超时都会退化成"看不见的失败"或"模型无限重试同一个动作"。
# --------------------------------------------------------------------------- #
def test_a_timeout_is_its_own_failure_reason():
    """★ 超时单独一档：它既不是参数错，也不是实现崩。

    判据必须看**异常类型**：下游处置完全不同 —— ``bad_args`` 去改工具描述或参数
    结构，``error`` 去查实现或依赖，``timeout`` 去看预算、数据量或外部依赖。
    第二条断言同时否掉"按文本判"：一句写着 timed out 的 RuntimeError 不是超时。
    """
    from src.agents.steps import _failure_reason

    assert _failure_reason(TimeoutError()) == "timeout"
    assert _failure_reason(RuntimeError("timed out")) == "error"


def test_a_timeout_is_recorded_as_failed_with_its_own_reason(sink):
    """超时在清单里是 ``failed`` + ``reason=timeout``。

    兜底出口列"未完成步骤"读的就是这份状态。若超时被记成 ok（或根本没记），
    用户拿到的部分结果不会带上"这一步没做成"，而那正是这套清单存在的意义。
    """
    log = StepLog()
    _begin(log, "kb_search", '{"query": "x"}')
    log.fail("kb_search", TimeoutError())

    assert log.steps[0].status == "failed"
    assert log.steps[0].reason == "timeout"
    assert [e[3].get("reason") for e in sink.counters("agent_tool_result_total")] == ["timeout"]


def test_the_timeout_message_tells_the_model_the_action_may_already_have_run():
    """★ 超时回给模型的话必须同时说清两件事，否则模型多半会直接重做。

    ① 可以怎么做（缩小范围 / 换做法 / 放弃这一步先把其余部分做完）；
    ② 超时只表示"没等到结果"，**不表示没执行** —— 落盘类动作可能在超时之后照样
       完成，重做会把文件写两遍（而文件名相同会覆盖，连痕迹都留不下）。
    """
    from src.agents import tools

    handler = _installed_handler(tools.deliver)
    text = handler(_tool_context(TaskContext(), "deliver", "{}"), TimeoutError())

    assert "超时" in text
    assert "不表示「没执行」" in text
    assert "重做" in text


def test_a_non_timeout_failure_keeps_the_official_message():
    """非超时失败仍用官方默认文案。

    换掉它是退步：官方那句经过版本演进，模型对它的先验更稳；而这里只需要补
    "超时"这一种官方没有语义的失败。
    """
    from agents.tool import default_tool_error_function

    from src.agents import tools

    task = TaskContext()
    ctx = _tool_context(task, "deliver", "{}")
    error = RuntimeError("boom")
    handler = _installed_handler(tools.deliver)

    assert handler(ctx, error) == default_tool_error_function(ctx, error)


def test_repeated_timeouts_raise_the_repeat_count():
    """★ 同一动作连续超时 ⇒ ``max_repeat`` 上升。

    这就是"同一子任务重试超过 N 次即熔断"的输入：反思闸门读的就是这个值。
    刻意不在这里钉具体阈值（阈值由 ``AGENT_REFLECT_ON_REPEAT`` 定）—— 这里只钉
    "超时**会计入**重复"。不计的话，超时就成了唯一一种"永远不算打转"的失败，
    模型可以无限重试同一个动作而不触发任何熔断。
    """
    log = StepLog()
    for index in range(3):
        _begin(log, "kb_search", '{"query": "x"}', f"c{index}")
        log.fail("kb_search", TimeoutError())

    assert log.max_repeat == 3
