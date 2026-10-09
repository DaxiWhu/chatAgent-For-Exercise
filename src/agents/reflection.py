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

第 2 级 · 退回（2026-10-09 追加）
-----------------------------
"提醒没用"之后要把那一步撤掉。**四条落定的事实**决定了它只能这么写：

1. **唯一能改输入的口子还是注入口** ⇒ 撤回只能在 ``inject_reflections`` 里做。
2. ⚠️ **注入口的修改是"本次调用专用"的**：官方 ``maybe_filter_model_input`` 返回的
   ``ModelInputData`` 只喂给这一次模型调用，运行状态里的条目还在，而且每轮之后又被回写
   （``run_internal/run_loop.py``：``run_state._generated_items =
   list(streamed_result._model_input_items)``）⇒ **撤回必须"持久"**：把撤掉的 ``call_id``
   记进 ``state.revoked``，之后每一轮都按它过滤。只在那一次删掉的表现是"下一轮那条动作
   又回来了"，而模型会以为自己没做过、很可能再调一次 —— 撤回等于没发生。
3. **运行中间的条目只有"工具调用对"**（实测：一次运行里第 3 次模型调用收到的条目是
   ``用户消息 + (function_call, function_call_output) × 2``，中间**没有**助手文本条目）
   ⇒ 按 ``call_id`` 过滤就够了，不会留下"一段没有来由的助手话"。
   ⚠️ 这是**实测**结论不是推断（``tmp_probe/probe_input_call_id.py``）；哪天 SDK 往中间
   塞进助手文本条目，这里就要改成按位置切段。
4. **只对没有副作用的动作退回**：退回 = "让模型重做那一步"，而**已落盘的副作用拿不回来**
   ⇒ 用**白名单**（``_REVERTABLE_TOOLS``）而不是黑名单：新加的工具默认**不可退回**，
   fail-safe。

**多久才退**：拍板结果是"**要第 3 次反思才退回**" ⇒ ``_level2_line() = 第 1 级上限 + 1``
（默认 3），语义是"先提醒 N 次、还不行才撤"。发起上限因此**不能**再等于注入上限，
改用 ``AGENT_REFLECT_MAX_FIRES``（默认 3）。
⚠️ 它必须 ≥ ``第 1 级上限 + 第 2 级上限``，否则第 2 级永远轮不到（额度被第 1 级用光）
—— 而这个组合**不报错**，所以有一条配置自检把它打成 warning（``_warn_once_if_...``）。

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
    "max_level2",
    "maybe_reflect",
    "reset_reflection_state",
    "revertable_tools",
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

#: **可撤回**的动作白名单 —— 只列"重做一次代价为零"的。
#:
#: ⚠️ 为什么是白名单而不是"有副作用的黑名单"：撤回的语义是"让模型重做那一步"，
#: 而**已落盘的副作用拿不回来**（文件还在、库写入还在）。黑名单的问题是新加一个工具时
#: 默认**能**被撤回 —— 而"它有没有副作用"这件事只有当它真的被撤回一次并留下两份产物时
#: 才暴露。白名单把默认值反过来：新工具不写进来就不可撤回，代价只是"少撤回一次"。
#:
#: * ``kb_search`` —— 纯读；
#: * ``research`` —— 专家（``as_tool``），内部只有 ``kb_search``，也是纯读；
#: * ``review_draft`` —— 评审，只读的流程步骤。
#:
#: 刻意**不含**：``save_text_asset`` / ``generate_image``（落盘产物）、
#: ``build_slide_deck`` / ``make_illustration``（内部会落盘）。
_REVERTABLE_TOOLS = frozenset({"kb_search", "research", "review_draft"})

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


def max_level2() -> int:
    """第 2 级（退回）的上限：一次运行里最多**撤回**几次。

    下限 **0** 是留给运维的逃生口 —— ``0`` = 只提醒、绝不动上下文。它不是"另一个开关"：
    开关仍然是总开关那一个，这里只是把第 2 级的额度配成 0。
    """
    return env_int("AGENT_REFLECT_MAX_LEVEL2", 1, lo=0)


def _max_fires() -> int:
    """一次运行里最多**发起**几次反思。

    ⚠️ 它**必须 ≥ ``max_level1() + max_level2()``**，否则第 2 级永远轮不到：
    额度会被第 1 级先用光，而"第 2 级从不触发"这件事**不报错、也不影响交付**
    （见 ``_warn_once_if_the_cap_is_too_small``）。默认 3 = 2 + 1，正好够
    "提醒 2 次 + 撤回 1 次"。
    """
    return env_int("AGENT_REFLECT_MAX_FIRES", 3, lo=1)


def _level2_line() -> int:
    """某种问题出现到**第几次**才退回 = 第 1 级上限 + 1。

    语义是"先提醒 N 次，还不行才撤"：第 1 级上限是 2 ⇒ 第 3 次同类问题才退。
    ⚠️ 用**推导**而不是再开一个配置项：这是个"两个数之间必须成立的关系"，
    写成独立配置只会多出一种配错的组合（而它配错的表现是"第 2 级永远不触发"）。
    """
    return max_level1() + 1


def _reflect_timeout() -> float:
    """反思自己那次模型调用的超时（秒）。**必须设** —— 它走旁路，不受主线约束。"""
    return env_float("AGENT_REFLECT_TIMEOUT", 20.0, lo=1.0)


def _wait_seconds() -> float:
    """每次模型调用前最多等几秒（决策 6 的实现，见 ``_wait_for_pending``）。"""
    return env_float("AGENT_REFLECT_WAIT_SECONDS", 2.0, lo=0.0, hi=30.0)


def heavy_tools() -> frozenset[str]:
    """重量级动作的工具名（给测试与文档引用，避免它们抄一份名单）。"""
    return _HEAVY_TOOLS


def revertable_tools() -> frozenset[str]:
    """可撤回的动作白名单（同上：给测试与文档引用）。"""
    return _REVERTABLE_TOOLS


_cap_warned = False


def _warn_once_if_the_cap_is_too_small() -> None:
    """配置自检：发起上限装不下两级额度 ⇒ 第 2 级永远轮不到。

    ⚠️ 这个组合的危害正是"没有症状"：反思照常触发、提醒照常注入、指标照常在涨，
    只有"退回"从不发生 —— 而"退回很少见"与"退回不可能发生"在看板上长得一模一样。
    所以哪怕只是一个 warning 也要打（同 ``history_budget`` 那条打一次就够的做法：
    本函数在**每次工具结束**都会被调到，不能每次都打）。
    """
    global _cap_warned
    if _cap_warned:
        return
    if _max_fires() >= max_level1() + max_level2():
        return
    _cap_warned = True
    logging.warning(
        "反思闸门配置自检：AGENT_REFLECT_MAX_FIRES=%d < AGENT_REFLECT_MAX_LEVEL1=%d + "
        "AGENT_REFLECT_MAX_LEVEL2=%d ⇒ **第 2 级（退回）永远不会触发**（额度会被第 1 级"
        "先用光）。这不会报错、也不影响交付，只会让退回静默失效。",
        _max_fires(),
        max_level1(),
        max_level2(),
    )


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

    level: int = 1
    """处置级别：``1`` = 注入提醒；``2`` = 撤回那一步。

    ⚠️ 在 ``_run``（后台）里就定下来，但**能不能真的撤**要到注入口才知道
    （那里才看得到模型输入）⇒ 注入口可能把它降级回 1。"""

    tool: str = ""
    """那一步用的是哪个工具（由 ``_attach_step`` 从步骤清单落定，**不用模型报的**）。

    撤回前要按它查白名单：模型自己在输出里写的工具名是不可信的（同 ``step``）。"""

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

    def as_revert_note(self) -> str:
        """第 2 级的注入文本：**告诉模型那一步已经被撤掉了**。

        ⚠️ 不写"已撤掉"会得到最坏的一种组合：模型看不到那一步，却收到一段
        "你在第 N 步打转"的提醒 —— 它要么当成幻觉忽略，要么再去把那个动作做一遍
        （而"再做一遍"正是退回要阻止的事）。
        ⚠️ **"不是用户的新要求"这句同样必须留**：撤回说明看着更像一条指令。
        """
        what = f"第 {self.step} 步" + (f"（{self.tool}）" if self.tool else "")
        parts = [
            f"[自动检查] 系统已把{what}**及其之后的动作**从你看到的上下文里撤掉了。"
        ]
        if self.problem in _PROBLEM_TEXT:
            parts.append(f"原因：{_PROBLEM_TEXT[self.problem]}。")
        if self.summary:
            parts.append(f"当时看到的情况：{self.summary}。")
        if self.advice:
            parts.append(f"建议：{self.advice}")
        parts.append(
            "这**不是**用户的新要求，也不是让你重新开始 —— 请从那里**之前**重新决定，"
            "换一种做法，不要重复同一个动作。"
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

    reverted: int = 0
    """已经撤回几次（上限 ``AGENT_REFLECT_MAX_LEVEL2``）。"""

    kinds: dict[str, int] = field(default_factory=dict)
    """每种问题（``loop`` / ``stuck`` / ``off_track``）**累计出现过几次**。

    ⚠️ 判"没纠正"的判据是**问题类型相同**、不是文本相似：同一类问题连着出现才说明
    "上次那个提醒没起作用"，而两次措辞不同的 ``ok`` 不构成任何信号（它压根不进这里）。
    """

    revoked: set[str] = field(default_factory=set)
    """已撤回的 ``call_id`` —— **持久撤回**的落点。

    ⚠️ 不记它就是"只删这一次调用"：运行状态里的条目下一轮又会被拼回输入（见模块
    docstring 第 2 条），模型看到那条动作又回来了、很可能再调一次。
    """

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
    _warn_once_if_the_cap_is_too_small()
    state = _state_of(context)
    if state is None:
        return
    if _fired_total(state) >= _max_fires():
        # ⚠️ 这里的上限是**发起**上限，不是注入上限（第 3 步时两者相等，第 4 步拆开了）：
        # 第 2 级要在"第 1 级的注入额度已经用光"之后才轮到，用注入上限卡发起次数
        # 会让退回永远没机会。多发的结论仍然没有位置可去（每次注入都会清空待注入队列），
        # 所以它本身也不是越小越好 —— 配成 `AGENT_REFLECT_MAX_LEVEL1 + MAX_LEVEL2` 即可。
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
    if state is None:
        return
    # 把结论里的"第几步"落到**步骤清单**上：模型报的步号与调用号都不可信，
    # 而撤回要按 `call_id` 定位 —— 拿模型编的编号去定位等于把撤回交给模型决定。
    _attach_step(context, finding)
    state.kinds[finding.problem] = state.kinds.get(finding.problem, 0) + 1
    revert, refusal = _revert_decision(state, finding)
    if revert:
        state.reverted += 1
        finding.level = 2
    elif refusal:
        observability.counter("agent_recovery_skipped_total", reason=refusal)
    state.pending.append(finding)


def _attach_step(context: Any, finding: ReflectiveFinding) -> None:
    """把结论的"第几步"钉到步骤清单里的那一条上（顺便取回它的 ``call_id`` / 工具名）。

    越界或非数字 ⇒ 一律归 0（"不指代具体步骤"）：一个指不到任何动作的步号必须退化成
    "泛泛的提醒"，**不能**当成"第 1 步"去撤 —— 那会撤掉一条完全无关的动作，
    而且看起来一切正常。
    """
    steps = list(getattr(getattr(context, "steps", None), "steps", []) or [])
    if finding.step <= 0 or finding.step > len(steps):
        finding.step = 0
        finding.call_id = ""
        finding.tool = ""
        return
    step = steps[finding.step - 1]
    finding.call_id = str(getattr(step, "call_id", "") or "")
    finding.tool = str(getattr(step, "tool", "") or "")


def _revert_decision(state: ReflectionState, finding: ReflectiveFinding) -> tuple[bool, str]:
    """该不该升到第 2 级；不升时给出**要计入指标的原因**（正常走第 1 级的原因不算）。

    顺序是有意的：**先判升级线**，再判额度与白名单。反过来会把"还没到升级的时候就报
    ``capped``/``side_effect``"也计进去，指标立刻变成噪声（每次第 1、2 次反思都在涨）。
    """
    if state.kinds.get(finding.problem, 0) < _level2_line():
        return False, ""
    if state.reverted >= max_level2():
        # 额度用完（`AGENT_REFLECT_MAX_LEVEL2=0` 也落在这里）。它持续在涨说明
        # "问题还在、但已经没得退了" ⇒ 该调大额度，或者该上第 3 级（停止重试）。
        return False, "capped"
    if finding.tool not in _REVERTABLE_TOOLS:
        # 白名单外（落盘类 / 认不出来的工具）⇒ 不撤。⚠️ 这里如果认不出工具名
        # （`finding.tool` 为空）也走这一支 —— fail-safe，宁可少撤一次。
        return False, "side_effect"
    return True, ""


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
    """本口的**全部**动作：持久撤回 + 注入提醒 + 执行第 2 级退回。

    四步：等一下 → 滤掉已撤回的 → 处理待注入队列 → 装回输入。
    ⚠️ 调用方必须保证它跑在**裁剪之后**（``runner.model_input_filter``）：排在前面的会被
    裁掉，而"刚提醒过模型、下一轮又被裁掉"等于这段提醒从来没生效，且完全不报错。

    逐条结论的处置（``finding.level`` 在 ``_run`` 里就定下来了）：

    * **第 1 级** —— 追加提醒，受 ``AGENT_REFLECT_MAX_LEVEL1`` 限制；
    * **第 2 级** —— 撤回那一步。⚠️ 到这一步才能判"能不能撤"：**只有这里看得到模型输入**。
      定位不到（被 token 裁剪或历史压缩吃掉）⇒ **当次不退回**，降级成一条提醒并计
      ``agent_recovery_skipped_total{reason="not_found"}`` —— 能力退化必须是**看得见**的。

    ⚠️ 第 2 级**不受**注入上限约束：它本来就发生在"第 1 级的额度已经用光"之后
    （触发线 = 第 1 级上限 + 1），拿注入上限去卡它，退回就永远不会发生。

    每个结论**只处理一次**（处理完清空队列）：不清的表现是每一轮模型调用都往里塞同一段话，
    上下文被同一个提醒反复污染，越到后面越像"系统在复读"。
    """
    state = _state_of(context) if context is not None else None
    if state is None:
        return model_data
    if state.pending or state.tasks:
        await _wait_for_pending(state)

    original = list(getattr(model_data, "input", None) or [])
    items = _drop_revoked(original, state.revoked) if state.revoked else original
    changed = items is not original

    notes: list[str] = []
    for finding in state.pending:
        if finding.level == 2:
            items, dropped = _apply_revert(items, state, finding)
            if not dropped:
                observability.counter("agent_recovery_skipped_total", reason="not_found")
                if _take_level1_slot(state):
                    notes.append(finding.as_note())
                continue
            changed = True
            notes.append(finding.as_revert_note())
            observability.counter("agent_recovery_level_total", level="2")
            logging.info(
                "第 2 级：撤回第 %d 步（%s，call_id=%s），连带移除 %d 条上下文条目",
                finding.step, finding.tool, finding.call_id, dropped,
            )
            continue
        if _take_level1_slot(state):
            notes.append(finding.as_note())
    state.pending.clear()

    if not notes and not changed:
        return model_data
    return _rebuild(model_data, items, "\n\n".join(notes))


def _take_level1_slot(state: ReflectionState) -> bool:
    """占一个第 1 级注入额度；没有额度了返回 False（并计一次"额度不够"）。"""
    if state.injected >= max_level1():
        return False
    state.injected += 1
    observability.counter("agent_recovery_level_total", level="1")
    return True


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


def _rebuild(model_data: Any, items: list[Any], note: str) -> Any:
    """把（可能改过的）条目 + 末条 system 提醒重新装成 ``ModelInputData``。

    ``note`` 为空 ⇒ 不加末尾那条（撤回之后可能只剩"删条目"这一个动作）。

    ⚠️ 提醒追加在**末尾**、且是一条**独立的** system 消息。位置与历史压缩的摘要
    （放最前）**不同**，这是有意的：摘要是"很久以前发生过什么"（背景），本提示是
    "关于你刚才做法的提醒" —— 放最后才是模型最近看到的内容，也是唯一不会被后面
    几十条历史冲淡的位置。独立成条则是因为拼进最后一条用户消息会让"用户说过什么"
    与"系统提醒过什么"在事后分不开（会话落库的那一份也一样）。
    """
    try:
        from agents.run_config import ModelInputData
    except Exception:  # noqa: BLE001  SDK 换了形状 ⇒ 放弃改写（不改变行为）
        return model_data
    out = list(items)
    if note:
        out.append({"role": "system", "content": note})
    return ModelInputData(
        input=out, instructions=getattr(model_data, "instructions", "") or ""
    )


def _call_id_of(item: Any) -> str:
    """条目对应的 ``call_id``（不是工具条目 ⇒ 空串）。

    兼容两种形态：官方这里给的是 typed dict（实测），但 SDK 换成对象形态时
    ``getattr`` 也能读到 —— 与 ``steps.calls_from_response`` 用属性存在性判据同理。
    """
    value = (
        item.get("call_id")
        if isinstance(item, dict)
        else getattr(item, "call_id", None)
    )
    return str(value) if value else ""


def _drop_revoked(items: list[Any], revoked: set[str]) -> list[Any]:
    """把 ``call_id`` 已被撤回的条目滤掉（**没有可滤的就返回原列表对象**）。

    返回原对象是有意的：调用方靠 ``items is not original`` 判断"到底改没改"，
    从而在没有待注入结论时**原样返回** ``model_data``（连拷贝都不做）。
    """
    if not any(_call_id_of(item) in revoked for item in items):
        return items
    return [item for item in items if _call_id_of(item) not in revoked]


def _locate(items: list[Any], call_id: str) -> int | None:
    """``call_id`` 在输入里的下标（找不到 = 那一步已经不在模型视野里了）。

    定位到的是 ``function_call``（**动作本身**）—— 它与配对的 ``function_call_output``
    共用同一个 ``call_id``，而 ``function_call`` 在时间序上更靠前（实测）。
    """
    if not call_id:
        return None
    for index, item in enumerate(items):
        if _call_id_of(item) == call_id:
            return index
    return None


def _apply_revert(
    items: list[Any], state: ReflectionState, finding: ReflectiveFinding
) -> tuple[list[Any], int]:
    """执行一次撤回：返回（删完之后的条目, 这次连带删掉了几条）。

    "撤那一步" = **从它删到末尾**（让模型回到那一步之前重新决定），并把删掉的那一段里
    的 ``call_id`` 全部记进 ``state.revoked`` ⇒ 之后每一轮都不会再出现（持久撤回，
    理由见模块 docstring 第 2 条）。
    """
    index = _locate(items, finding.call_id)
    if index is None:
        return items, 0
    cut = items[index:]
    for item in cut:
        call_id = _call_id_of(item)
        if call_id:
            state.revoked.add(call_id)
    return items[:index], len(cut)


def reset_reflection_state() -> None:
    """清掉进程内状态（**测试用**）：客户端、"已打过失败日志"与配置自检标记。"""
    global _client, _client_failed, _failure_logged, _cap_warned
    _client = None
    _client_failed = False
    _failure_logged = False
    _cap_warned = False


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
