"""反思闸门 —— 在一次 agent 运行的**内部**发现"打转 / 卡住 / 跑偏"并介入。

要解决什么
----------
一个 agent 节点 = 一次完整的运行。这次运行内部模型可以来回最多 ``AGENT_MAX_TURNS``
次、配十几次工具执行，而这一段**图不介入、不落检查点** ⇒ 中间出了问题（在两个动作
之间来回试、反复失败、早就跑偏了）**没有任何东西会打断它**，只能等它把
``AGENT_MAX_TURNS`` 烧完。前两步把这一段变成了"看得见"（步骤记录 + 重复计数），
本模块把它变成"能介入"。

怎么介入（四次交接）
------------------
    ① 钩子里判"够了没有"            ``maybe_reflect``
        ↓ 够了
    ② 起一个后台任务（不阻塞主线）    ``_run``
        ↓ 跑完
    ③ 结论放进"待注入"队列           ``ReflectiveFinding``
        ↓ 下一次模型调用**之前**
    ④ 追加成一条 system 消息         ``inject_reflections``

⚠️ 位置是**被逼出来的**，不是随手选的：钩子（``on_tool_end`` 等）**没有返回值**，
只能观察、不能改写送进模型的内容；唯一能改模型输入的口子是
``RunConfig.call_model_input_filter``（官方 docstring 原话就是 "edit the input sent
to the model"）。所以"提醒模型"只能表现为"在下一次模型调用之前塞一段话"。

四条边界（都影响怎么用它）
------------------------
1. **不进 LangGraph state、不跨 agent 节点**（挂 ``TaskContext``，与 ``steps`` /
   ``stats`` 同一条理由）：结论只注入**本节点**后续的模型调用。第四步的"退回某一步"
   本身就只能在本节点内做（跨了节点就退不回去了），两者边界一致最不容易出错。
2. **绝不阻塞主线**：发起用 ``asyncio.create_task``，且**必须持有引用**（不持有会被
   GC 掉 —— 与 ``app/main.py::_spawn_history_compaction`` 那条坑同源）。全程唯一的
   等待是注入前那最多 ``AGENT_REFLECT_WAIT_SECONDS`` 秒（见 ``_wait_for_pending``）。
3. **反思失败不影响业务**：调用失败 / 超时 / 解析不出来 ⇒ 一律按"没问题"处理，
   本轮照常跑完（只有指标留下的痕迹）。
4. **默认关**（``AGENT_MULTISTEP_GUARD=0``）：它**真会改变模型看到的东西**，
   与 ``AGENT_TOOL_STEPS`` 那种纯观察不是一回事，所以按老规矩默认关。

旁路模型
--------
配置**复用** ``compaction.py`` 那一套（同 ``AGNES_MODEL`` / ``AGNES_API_KEY`` /
``AGNES_API_BASE``，读的是 ``compaction._model_config()``），但**自己建 client**：
超时必须独立（``AGENT_REFLECT_TIMEOUT``），而 client 的超时是构造期参数 ⇒ 没法
和摘要共用同一个实例。走旁路 ⇒ **不计入 ``AGENT_MAX_TURNS``、也不进 SDK tracing**
⇒ 超时必须自己设（见 ``_reflect_sync``）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .. import observability
from ..envutil import env_float, env_int

__all__ = [
    "ReflectionState",
    "ReflectiveFinding",
    "counts_of",
    "guard_enabled",
    "heavy_tools",
    "inject_reflections",
    "maybe_reflect",
    "reset_reflection_state",
]


#: 重量级动作 —— **叫专家**（三个 ``as_tool``）与**落盘产出**。
#:
#: 为什么不按"所有工具"数：检索（``kb_search``）本来就该多查几轮，把它算进来会让
#: 任何一次认真查资料都逼近阈值。评审（``review_draft``）刻意**不算** —— 它是只读的
#: 流程步骤、不改变世界，而"产出→评审→改"本来就要调几次，算进来会把正常流程
#: 误报成跑偏。
_HEAVY_TOOLS = frozenset(
    {
        "research",
        "build_slide_deck",
        "make_illustration",
        "save_text_asset",
        "generate_image",
    }
)

#: 触发原因 → 阈值取值函数。
#:
#: ⚠️ 存**函数**而不是数值：阈值必须每次现读环境变量（测试要能逐例改它，运维要能
#: 不重启就改 —— 而模块级算好的常量两样都做不到）。
_THRESHOLDS: dict[str, Any] = {}

#: 问题类型取值（给模型看的中文说明见 ``_PROBLEM_TEXT``）。
_PROBLEMS = ("ok", "loop", "stuck", "off_track")

_PROBLEM_TEXT = {
    "loop": "在原地打转（同一个动作反复做）",
    "stuck": "卡住了（反复失败，没有推进）",
    "off_track": "偏离了原来的任务",
}

_JSON_OBJECT = re.compile(r"\{.*\}", re.S)
"""从模型输出里取最外层的 ``{...}``。

⚠️ **宽松解析是有意的**：旁路调用没有 ``response_format`` 的保证（网关不一定支持），
模型常在 JSON 外面包一层解释或 ``` 代码块。取不出来时返回 None ⇒ 调用方按"没问题"
处理 —— 绝不能因为"反思自己没读懂"就把用户的请求拦下来。
"""


# --------------------------------------------------------------------------- #
# 配置（``src/`` 侧直读环境变量，与 session / compaction 一致：避免反向依赖 app）
# --------------------------------------------------------------------------- #
def guard_enabled() -> bool:
    """多步防护总开关（**默认关**）。

    与 ``AGENT_TOOL_STEPS`` 的区别是本质的：那个只记录、不改任何对外可见行为；
    这个会**往模型输入里塞东西**（而且未来第四步还会删上下文）。所以默认关，
    出问题的退回代价是零。
    """
    return bool(env_int("AGENT_MULTISTEP_GUARD", 0, lo=0, hi=1))


def _after_tools() -> int:
    return env_int("AGENT_REFLECT_AFTER_TOOLS", 8, lo=1)


def _after_heavy() -> int:
    return env_int("AGENT_REFLECT_AFTER_HEAVY", 3, lo=1)


def _after_hops() -> int:
    return env_int("AGENT_REFLECT_AFTER_HOPS", 2, lo=1)


def _on_repeat() -> int:
    """同一动作重复几次触发。下限 **2** —— 1 意味着"做一次就算重复"，那是常态。"""
    return env_int("AGENT_REFLECT_ON_REPEAT", 3, lo=2)


def max_level1() -> int:
    """第 1 级（注入反馈）的上限：一次运行里最多注入几次结论。"""
    return env_int("AGENT_REFLECT_MAX_LEVEL1", 2, lo=1)


def _reflect_timeout() -> float:
    """反思自己那次模型调用的超时（秒）。**必须设** —— 它走旁路，不受主线约束。"""
    return env_float("AGENT_REFLECT_TIMEOUT", 20.0, lo=1.0)


def _wait_seconds() -> float:
    """每次模型调用前最多等几秒（决策 6 的实现，见 ``_wait_for_pending``）。"""
    return env_float("AGENT_REFLECT_WAIT_SECONDS", 2.0, lo=0.0, hi=30.0)


def heavy_tools() -> frozenset[str]:
    """重量级动作的工具名（给测试与文档引用，避免它们抄一份名单）。"""
    return _HEAVY_TOOLS


# --------------------------------------------------------------------------- #
# 四个计数（全部从既有数据算出来，不新增一份要同步维护的状态）
# --------------------------------------------------------------------------- #
def counts_of(context: Any) -> dict[str, int]:
    """本次运行的四个计数 + 跨节点的换人次数。

    * ``tools`` —— 登记过的动作总数（最粗的"跑了多久"）；
    * ``heavy`` —— 其中重量级动作（见 ``_HEAVY_TOOLS``）；
    * ``hops`` —— **跨 agent 节点的换人次数**，取自 ``stats.handoffs``。⚠️ 它天然
      跨节点累计（``CARRY_ACROSS_NODES`` 里有 ``handoffs``），而当前拓扑里
      「前台 → 文学」最多发生 **一次** ⇒ 配 ``AGENT_REFLECT_AFTER_HOPS=2`` 时
      **永远不会触发**。留着它是因为拓扑一变（加第三个节点）它就立刻有用，
      而"忘了加这个计数"是读代码看不出来的；不要因为"现在不会触发"把它删掉。
    * ``repeat`` —— 同一动作被做过的最多次数（``StepLog.max_repeat``）。

    取不到任何一项都按 0 算 —— 这是个观测函数，不该因为形态差异抛异常。
    """
    steps = list(getattr(getattr(context, "steps", None), "steps", []) or [])
    heavy = sum(1 for s in steps if getattr(s, "tool", "") in _HEAVY_TOOLS)
    stats = getattr(context, "stats", None)
    hops = len(list(getattr(stats, "handoffs", []) or []))
    repeat = int(getattr(getattr(context, "steps", None), "max_repeat", 0) or 0)
    return {"tools": len(steps), "heavy": heavy, "hops": hops, "repeat": repeat}


def _fired_total(state: ReflectionState) -> int:
    return sum(state.fired.values())


def _due(counts: dict[str, int], state: ReflectionState) -> str | None:
    """有没有计数**刚跨过**它的下一个阈值整数倍 ⇒ 返回那个原因的名字。

    口径（用户 2026-10-09 拍板）：阈值同时承担两个含义 ——"什么时候开始看"与
    "多久再看一眼"。工具阈值 8 ⇒ 在第 8、16、24 次各触发一次；重复阈值 3 ⇒
    第 3、6、9 次。

    为什么不是"过了阈值就每次都触发"：一旦过了阈值，之后每多一个动作都会发起一次
    反思，而每次反思都是一次**真实模型调用** ⇒ 长任务里反思会比主线还贵
    （方案的风险清单里列过这一条）。也不是"每个原因只触发一次"：那样第四步的
    "没纠正就升级"就失去依据 —— 没有第二次反思，判不出"同类问题又出现了"。
    """
    for name, threshold_of in _THRESHOLDS.items():
        threshold = threshold_of()
        if counts.get(name, 0) >= threshold * (state.fired.get(name, 0) + 1):
            return name
    return None


# --------------------------------------------------------------------------- #
# 状态（挂 TaskContext）
# --------------------------------------------------------------------------- #
@dataclass
class ReflectiveFinding:
    """一次反思的结论。"""

    problem: str = "ok"
    """``ok`` / ``loop`` / ``stuck`` / ``off_track``（取值见 ``_PROBLEMS``）。"""
    summary: str = ""
    """一句话说明（会被注入模型，所以要写成给人看的话）。"""
    step: int = 0
    """涉及第几步（0 = 不指代具体步骤）。第四步"退回那一步"的定位依据。"""
    call_id: str = ""
    """涉及哪一次调用（同上；工具钩子不带它，只能从模型响应那一侧取）。"""
    advice: str = ""
    trigger: str = ""
    """哪个计数触发的（指标标签）。"""

    @property
    def is_problem(self) -> bool:
        return self.problem in _PROBLEMS and self.problem != "ok"

    def as_note(self) -> str:
        """注入模型的文本。

        ⚠️ 两句话必须写进去，缺一句都会改变模型的行为：
        ① **"不是用户的新要求"** —— 注入的是一条 system 消息，不写这句，模型可能
           把它当成新任务去做（"好的，我重新开始"），而它要做的恰恰是**接着干**；
        ② **"也不是让你重新开始"** —— 只提醒、不改变任务。
        """
        parts = [f"[自动检查] {self.summary or '系统发现刚才的做法可能有问题'}。"]
        if self.problem in _PROBLEM_TEXT:
            parts.append(f"判定：{_PROBLEM_TEXT[self.problem]}。")
        if self.step:
            parts.append(f"涉及第 {self.step} 步。")
        if self.advice:
            parts.append(f"建议：{self.advice}")
        parts.append(
            "这**不是**用户的新要求，也不是让你重新开始 —— 继续当前任务，"
            "但请换一种做法。"
        )
        return " ".join(parts)


@dataclass
class ReflectionState:
    """反思闸门的运行内状态（挂 ``TaskContext``，与 ``steps`` 同一条：不进 state）。"""

    fired: dict[str, int] = field(default_factory=dict)
    """每个触发原因**已经触发过几次**（用来算"下一个阈值整数倍"）。"""

    pending: list[ReflectiveFinding] = field(default_factory=list)
    """已拿到、**还没注入**的结论（注入后清空）。"""

    injected: int = 0
    """已经注入过几次（上限 ``AGENT_REFLECT_MAX_LEVEL1``）。"""

    tasks: set[Any] = field(default_factory=set, repr=False)
    """在飞的后台任务的**强引用** —— 不持有会被 GC 掉（回收时任务可能停在半个动作上）。"""


def _state_of(context: Any) -> ReflectionState | None:
    """从上下文取状态（走鸭子类型，避免 ``reflection`` ↔ ``context`` 循环导入）。"""
    state = getattr(context, "reflection", None)
    return state if isinstance(state, ReflectionState) else None


# --------------------------------------------------------------------------- #
# 发起（钩子调用；**不阻塞**）
# --------------------------------------------------------------------------- #
def maybe_reflect(context: Any) -> None:
    """够了就发起一次反思。**必须在有事件循环的异步上下文里调用**（钩子）。"""
    if not guard_enabled():
        return
    state = _state_of(context)
    if state is None:
        return
    if _fired_total(state) >= max_level1():
        # 注入上限就是发起上限：多发的结论没有位置可去（``inject_reflections``
        # 每次注入都会清空队列），只是白花一次模型调用。
        return
    trigger = _due(counts_of(context), state)
    if trigger is None:
        return
    state.fired[trigger] = state.fired.get(trigger, 0) + 1
    evidence = evidence_of(context)
    try:
        task = asyncio.create_task(_run(context, evidence, trigger))
    except RuntimeError:
        # 没有事件循环（同步调用 / 已关闭）⇒ 放弃这一次。反思是可选能力，
        # 绝不能因为"这里没有 loop"把主线带崩。
        state.fired[trigger] -= 1
        return
    state.tasks.add(task)
    task.add_done_callback(state.tasks.discard)


def evidence_of(context: Any) -> str:
    """交给反思模型的**证据**（结构化、只给有迹象的那几条）。

    只给"有迹象的"是刻意的：把全部步骤发过去，输入会随任务长度线性膨胀，而长任务里
    绝大多数步骤是正常的 —— 反思模型要在噪声里找信号，成本还随任务一起涨
    （方案风险清单里那条"反思比主线还贵"）。有迹象的三类：

    * **失败的**（``stuck`` 最直接的证据）；
    * **重复的**（``loop`` 最直接的证据）；
    * **重量级的**（要判"跑偏"就得看到它到底调了几次专家、落了几次盘）。

    再补最近两条（保证模型知道"现在停在哪一步"），去重后按发生顺序排列、最多
    ``_MAX_EVIDENCE`` 条。
    """
    steps = list(getattr(getattr(context, "steps", None), "steps", []) or [])
    repeats = getattr(getattr(context, "steps", None), "repeat_counts", None)
    hot_fingerprints = set(repeats().keys()) if callable(repeats) else set()

    picked: list[Any] = []
    seen: set[int] = set()
    for step in steps:
        fingerprint = getattr(step, "fingerprint", "")
        flagged = (
            getattr(step, "status", "") == "failed"
            or (fingerprint and fingerprint in hot_fingerprints)
            or getattr(step, "tool", "") in _HEAVY_TOOLS
        )
        if flagged and getattr(step, "seq", 0) not in seen:
            picked.append(step)
            seen.add(getattr(step, "seq", 0))
    for step in steps[-2:]:
        if getattr(step, "seq", 0) not in seen:
            picked.append(step)
            seen.add(getattr(step, "seq", 0))
    picked.sort(key=lambda s: getattr(s, "seq", 0))
    picked = picked[-_MAX_EVIDENCE:]

    counts = counts_of(context)
    lines = [
        f"工具调用共 {counts['tools']} 次（其中重量级 {counts['heavy']} 次），"
        f"换人 {counts['hops']} 次，同一动作最多重复 {counts['repeat']} 次。",
        "涉及的动作（可能不全，只列了有迹象的）：",
    ]
    for step in picked:
        detail = (getattr(step, "detail", "") or "").strip()
        lines.append(
            f"- 第 {getattr(step, 'seq', 0)} 步 {getattr(step, 'tool', '')} "
            f"[{getattr(step, 'status', '')}]{(' ' + detail) if detail else ''}"
        )
    return "\n".join(lines)


_MAX_EVIDENCE = 20
"""证据条数上限 —— 反思的输入也要有界（它与主线共用同一个网关配额）。"""


async def _run(context: Any, evidence: str, trigger: str) -> None:
    """后台跑一次反思：调旁路模型、解析结论、放进待注入队列。

    ⚠️ 这个协程**活得比主线久**（发起后不 await）。它结束时主线可能已经交付完了 ——
    那时写进 ``pending`` 也没人再读（"迟到 = 白跑"的一种，属可接受损失）。
    所以它**绝不能抛异常**出去：一个没人 await 的 task 抛异常只会在事件循环的
    异常处理器里留一条记录，而有人的代码以为"反思会自己好"。
    """
    started = time.perf_counter()
    try:
        raw = await asyncio.to_thread(_reflect_sync, evidence)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001  反思失败不该影响业务
        logging.warning("反思执行失败（不影响本轮）：%s", e)
        return
    observability.histogram(
        "agent_reflection_duration_seconds", max(0.0, time.perf_counter() - started)
    )
    finding = _finding_of(raw, trigger)
    observability.counter(
        "agent_reflections_total",
        trigger=trigger,
        verdict="problem" if finding.is_problem else "ok",
    )
    if not finding.is_problem:
        return
    state = _state_of(context)
    if state is not None:
        state.pending.append(finding)


def _finding_of(raw: dict | None, trigger: str) -> ReflectiveFinding:
    """把解析出来的 dict 变成结论。**取不出来一律按"没问题"**。

    这一步刻意不严格：``problem`` 是未知取值时当 ``ok``（而不是当问题）—— 宁可漏报
    一次真问题，也不要因为模型写了个没见过的词就塞一段话进用户的上下文。
    """
    if not isinstance(raw, dict):
        return ReflectiveFinding(problem="ok", trigger=trigger)
    problem = str(raw.get("problem") or "").strip().lower()
    if problem not in _PROBLEMS:
        problem = "ok"
    try:
        step = int(raw.get("step") or 0)
    except (TypeError, ValueError):
        step = 0
    return ReflectiveFinding(
        problem=problem,
        summary=_one_line(raw.get("summary")),
        step=max(0, step),
        call_id=str(raw.get("call_id") or "").strip(),
        advice=_one_line(raw.get("advice")),
        trigger=trigger,
    )


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())[:300]


# --------------------------------------------------------------------------- #
# 反思本身（旁路模型；同步实现，由 ``_run`` 丢进线程）
# --------------------------------------------------------------------------- #
_REFLECT_PROMPT = (
    "你在监督一个正在执行任务的智能体。下面给你它**已经做过的动作**与四个计数。"
    "请判断它是不是出了问题，并**只输出一个 JSON 对象**，字段如下：\n"
    '{"problem": "ok|loop|stuck|off_track", "summary": "一句话说明", '
    '"step": 出问题的步号（没有就给 0）, "advice": "一句可执行的建议"}\n'
    "\n"
    "取值含义：\n"
    "- ok：没有异常，动作推进正常（**拿不准就选它**）。\n"
    "- loop：在原地打转 —— 同一个动作被反复做。\n"
    "- stuck：卡住了 —— 反复失败，没有推进。\n"
    "- off_track：偏离了原来的任务。\n"
    "\n"
    "硬性要求：不要解释、不要客套话、不要输出 JSON 以外的任何内容；"
    "没有把握时给 ok。"
)

_client: Any = None
_client_failed = False
_failure_logged = False


def _client_or_none() -> Any:
    """反思用的模型客户端（懒建、进程内唯一）。

    ⚠️ 与 ``compaction`` 的 client **不共用**：那边超时是
    ``AGENT_HISTORY_COMPACT_TIMEOUT``，这边是 ``AGENT_REFLECT_TIMEOUT``，而 client 的
    超时是构造期参数。配置来源共用（``compaction._model_config()``），实例分开。
    """
    global _client, _client_failed
    if _client is not None:
        return _client
    if _client_failed:
        return None
    try:
        from .compaction import _model_config

        _, key, base = _model_config()
    except Exception as e:  # noqa: BLE001  同包模块导不进来（理论上不会）
        _client_failed = True
        logging.warning("反思取不到模型配置：%s", e)
        return None
    if not key:
        # 没配 Key ⇒ 没有反思能力。**不是错误**：整体降级成"什么都不做"，
        # 行为与开关关闭一致（同 runtime 那条"开着且没配 ⇒ 行为与关一致"）。
        return None
    try:
        from openai import OpenAI

        kwargs: dict[str, Any] = {
            "api_key": key,
            "timeout": _reflect_timeout(),
            "max_retries": 1,
        }
        if base:
            kwargs["base_url"] = base
        _client = OpenAI(**kwargs)
    except Exception as e:  # noqa: BLE001  没装 openai / client 建不起来
        _client_failed = True
        logging.warning("反思用的模型客户端建不起来 ⇒ 反思闸门不可用：%s", e)
        return None
    return _client


def _reflect_sync(evidence: str) -> dict | None:
    """跑一次反思（**同步**：由 ``_run`` 丢进线程，理由同 ``compaction`` —— 一次
    同步 HTTP 往返跑在协程里会把本进程所有并发请求一起停住）。

    返回 ``None`` 的含义是"这次没结论"（没配 Key / 调用失败 / 超时 / 解析不出来），
    调用方按"没问题"处理。
    """
    client = _client_or_none()
    if client is None:
        return None
    try:
        from .compaction import _model_config

        model, _, _ = _model_config()
    except Exception:  # noqa: BLE001
        return None
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _REFLECT_PROMPT},
                {"role": "user", "content": evidence},
            ],
        )
        text = resp.choices[0].message.content or ""
    except Exception as e:  # noqa: BLE001  上游抖动 / 网关不认 / 超时
        _note_failure(e)
        return None
    return _parse_finding(text)


def _note_failure(exc: BaseException) -> None:
    """记一次反思失败。超时单独计数（它指向配置，与"网关抖动"处置不同）。"""
    kind = type(exc).__name__
    if "Timeout" in kind:
        observability.counter("agent_reflection_timeout_total")
        logging.info("反思超时（%.1fs）⇒ 这一次不等它的结论", _reflect_timeout())
        return
    observability.counter("agent_reflections_total", trigger="error", verdict="ok")
    global _failure_logged
    if _failure_logged:
        logging.debug("反思仍失败：%s", exc)
        return
    _failure_logged = True
    logging.warning("反思调用失败（不影响本轮，按「没问题」处理）：%s: %s", kind, exc)


def _parse_finding(text: str) -> dict | None:
    """从模型输出里取结构化结论（宽松解析，理由见 ``_JSON_OBJECT``）。"""
    if not text:
        return None
    match = _JSON_OBJECT.search(text)
    if match is None:
        logging.debug("反思输出里没有 JSON 对象，按「没问题」处理")
        return None
    try:
        raw = json.loads(match.group(0))
    except (TypeError, ValueError):
        logging.debug("反思输出的 JSON 解析不了，按「没问题」处理")
        return None
    return raw if isinstance(raw, dict) else None


# --------------------------------------------------------------------------- #
# 注入（``call_model_input_filter`` 的最后一步）
# --------------------------------------------------------------------------- #
async def inject_reflections(model_data: Any, *, context: Any = None) -> Any:
    """把待注入的结论追加成**一条 system 消息**；没有待注入的就原样返回。

    三步：等一下 → 取队列 → 追加。⚠️ 调用方必须保证它跑在**裁剪之后**
    （``runner._model_input_filter``）：排在前面的会被裁掉，而"刚提醒过模型、
    下一轮又被裁掉"等于这段提醒从来没生效，且完全不报错。

    每个结论**只注入一次**（注入后清空队列）：不清的表现是每一轮模型调用都往里
    塞同一段话，上下文被同一个提醒反复污染，越到后面越像"系统在复读"。
    """
    state = _state_of(context) if context is not None else None
    if state is None:
        return model_data
    if state.pending or state.tasks:
        await _wait_for_pending(state)
    if not state.pending:
        return model_data
    if state.injected >= max_level1():
        state.pending.clear()
        return model_data
    note = "\n\n".join(finding.as_note() for finding in state.pending)
    state.pending.clear()
    state.injected += 1
    return _append_system(model_data, note)


async def _wait_for_pending(state: ReflectionState) -> None:
    """最多等 ``AGENT_REFLECT_WAIT_SECONDS`` 秒，让在飞的反思把结论交上来。

    ⚠️ 这就是决策 6（"收尾前等几秒"）的**实际形态**：唯一能改模型输入的口子只有
    "模型调用之前"，在那个位置**看不到"这是不是最后一次调用"**，所以"等"只能表现为
    "每次模型调用前最多等 X 秒"。阈值定得早时绝大多数情况等 0 秒（发起到这一次调用
    之间已经跑了几轮），真正会等到的只有快收尾那几次。
    ⚠️ 这条要写进配置注释，否则运维会以为它是"只在最后等"。
    """
    wait = _wait_seconds()
    if wait <= 0:
        return
    in_flight = [task for task in state.tasks if not task.done()]
    if not in_flight:
        return
    done, _pending_tasks = await asyncio.wait(in_flight, timeout=wait)
    if not done:
        # 没赶上 ⇒ 白跑一次（结论回来时可能已经没人读它了）。属可接受损失，
        # 但必须可计数：它在涨说明 AGENT_REFLECT_TIMEOUT 或阈值配得不合适。
        observability.counter("agent_reflection_late_total")


def _append_system(model_data: Any, note: str) -> Any:
    """在输入**末尾**追加一条 system 消息。

    ⚠️ 位置与历史压缩的摘要（放最前）**不同**，这是有意的：摘要是"很久以前发生过
    什么"（背景），本提示是"关于你刚才做法的提醒" —— 放在最后是模型最近看到的
    内容，也是唯一不会被后面几十条历史冲淡的位置。
    ⚠️ 追加一条**独立的** system 消息、而不是拼进最后一条用户消息：拼进去会让
    "用户说过什么"与"系统提醒过什么"在事后分不开（会话落库的那一份也一样）。
    """
    try:
        from agents.run_config import ModelInputData
    except Exception:  # noqa: BLE001  SDK 换了形状 ⇒ 放弃注入（不改变行为）
        return model_data
    items = list(getattr(model_data, "input", None) or [])
    items.append({"role": "system", "content": note})
    return ModelInputData(
        input=items, instructions=getattr(model_data, "instructions", "") or ""
    )


def reset_reflection_state() -> None:
    """清掉进程内状态（**测试用**）：客户端与"已打过失败日志"标记。"""
    global _client, _client_failed, _failure_logged
    _client = None
    _client_failed = False
    _failure_logged = False


_THRESHOLDS.update(
    {
        "tools": _after_tools,
        "heavy": _after_heavy,
        "hops": _after_hops,
        "repeat": _on_repeat,
    }
)
"""放在文件末尾：阈值函数都在上面定义完了，这里只是把它们登记成"触发原因 → 阈值"。

⚠️ 键的顺序就是判定的优先级（工具 → 重量级 → 换人 → 重复）。多个同时到期时只报
第一个 —— 每条结论都会带上触发原因（指标标签），同时报多个会让"到底为什么触发"
这一栏失去意义。
"""
