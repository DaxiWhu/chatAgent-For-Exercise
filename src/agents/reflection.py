"""反思闸门 —— 在一次 agent 运行的**内部**发现"打转 / 卡住 / 跑偏"并介入。

要解决什么
----------
一个 agent 运行 = 一次完整的运行。这次运行内部模型可以来回最多 ``AGENT_MAX_TURNS``
次、配十几次工具执行，而这一段**没有中途检查点**（官方 `Runner` 只在工具请求审批时
才会停下来）⇒ 中间出了问题（在两个动作之间来回试、反复失败、早就跑偏了）**没有任何
东西会打断它**，只能等它把 ``AGENT_MAX_TURNS`` 烧完。前两步把这一段变成了"看得见"
（步骤记录 + 重复计数），本模块把它变成"能介入"。

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

第 3 级 · 停止重试（2026-10-10 追加）
--------------------------------
撤也不成、或撤了也没用时，模型已经被提醒过、也被退回过，再让它跑只是烧轮次 ⇒
**系统自己踩刹车**（`PLAN_step5_fallback.md` §5）。

**什么时候踩**（两个条件任一命中；两者都只发生在"同类问题已经重复到升级线"**之后**，
所以踩刹车从不抢跑 —— 那正是"先提醒 N 次、还不行才撤"的语义）：

* **到线却没撤成**：``capped``（额度用完）/ ``side_effect``（那一步是落盘类、白名单外）
  —— 这两个在 ``_run`` 里就能判出来；``not_found``（那一步已被 token 裁剪或历史压缩
  吃掉）⚠️ **只有注入口看得到模型输入**，所以只能在 ``inject_reflections`` 里判。
* **撤了也没用**：``not_fixed`` —— 已经成功撤回过，同类问题**又**出现。

**怎么踩**：置 ``ReflectionState.stopped``（带停因），然后在**下一次模型调用之前**
（``inject_reflections``）抛 ``AgentStoppedByGuard`` ⇒ 冒到 ``Runner.run`` 外面 ⇒
``runner`` 把它翻成 ``exit_reason="guard_stop"``。

⚠️ **为什么必须在注入口抛**：保护线里唯一握有控制权的时刻就是它 —— 钩子（``on_tool_end``）
没有返回值，后台任务（``_run``）抛异常没人接。实测（``tmp_probe/probe_guard_stop.py``）：
异常**原样**冒到 ``Runner.run`` 外面（类型没被 SDK 改写），而且**只发生了 1 次模型调用**
⇒ "抛出"确实打断了运行、没有多烧一轮。

⚠️ **一条必须记住的后果**：踩了刹车 ⇒ 这次运行**没有最终产出**（agent 链路的交付物只在
运行末尾成形，而运行是被打断的）⇒ 兜底判定必然落到"没有可交付的答复"那一行。
所以"系统类停止之后、已产出的内容照交"这条适用于**没踩刹车**的情形（反思报过打转/卡住、
但运行自己跑完了 ⇒ 交 + 标明部分结果），踩了刹车就只剩一句通用错误说明。

旁路模型
--------
配置**复用** ``compaction.py`` 那一套（同 ``AGNES_MODEL`` / ``AGNES_API_KEY`` /
``AGNES_API_BASE``，读的是 ``compaction._model_config()``），但**自己建 client**：
超时必须独立（``AGENT_REFLECT_TIMEOUT``），而 client 的超时是构造期参数 ⇒ 没法
和摘要共用同一个实例。走旁路 ⇒ **不计入 ``AGENT_MAX_TURNS``、也不进 SDK tracing**
⇒ 超时必须自己设（见 ``_reflect_sync``）。

⚠️ 但**成本必须计**（2026-10-21 补）：旁路不等于免费。这一次调用的 usage 通过
``budget.note_bypass`` 记进 ``AgentStats.bypass_tokens``，于是它既进
``agent_bypass_tokens_total``（能回答"反思花了多少"），也进 ``budget.used_of``
（token 预算必须管住真实账单，而不是只管 ``Runner`` 那几次调用）。
漏掉这一笔的表现很隐蔽：预算看着从没被碰到，账单却在涨 —— 而且读数完全对得上，
因为"少算的那部分"根本没有出现在任何一条曲线里。
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
from . import budget

__all__ = [
    "AgentStoppedByGuard",
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


def _on_oscillation() -> int:
    """**来回震荡转到第几圈**触发（``A→B→A→B`` 是 2 圈）。

    默认 **2**，比 ``AGENT_REFLECT_ON_REPEAT`` 的 3 更低，这不是随手定的：震荡检测的
    全部价值就在"单个动作还没到重复阈值"的那一段。配成 3 会让它永远晚于重复判据 →
    退化成一条永不独立生效的判据（`A→B→A→B` 时每个动作也已各 3 次，重复那条会先报）。

    下限 2 是它的定义：``1 圈`` 不是震荡。
    """
    return env_int("AGENT_REFLECT_ON_OSCILLATION", 2, lo=2)


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
    * ``oscillate`` —— 尾部同一个短周期已经转了几圈（``StepLog.oscillation_cycles``）。
      ⚠️ 它**不是** ``repeat`` 的变体，补的是 ``repeat`` 的结构性盲区：
      ``A→B→A→B`` 时 A、B 各 2 次，``repeat`` 到不了 3 ⇒ 那条判据对这种打转完全沉默。

    取不到任何一项都按 0 算 —— 这是个观测函数，不该因为形态差异抛异常。
    """
    steps = list(getattr(getattr(context, "steps", None), "steps", []) or [])
    heavy = sum(1 for s in steps if getattr(s, "tool", "") in _HEAVY_TOOLS)
    stats = getattr(context, "stats", None)
    hops = len(list(getattr(stats, "handoffs", []) or []))
    log = getattr(context, "steps", None)
    repeat = int(getattr(log, "max_repeat", 0) or 0)
    # 尾部往复已经转了几整圈（``A→B→A→B`` = 2）。它补的正是 ``repeat`` 的盲区：
    # 两个动作来回时每个都只做了两次，``repeat`` 到不了 3 ⇒ 那条判据完全沉默。
    oscillate = int(getattr(log, "oscillation_cycles", 0) or 0)
    return {
        "tools": len(steps),
        "heavy": heavy,
        "hops": hops,
        "repeat": repeat,
        "oscillate": oscillate,
    }


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

    def as_note(self, *, goal: str = "") -> str:
        """注入模型的文本。

        ⚠️ 两句话必须写进去，缺一句都会改变模型的行为：
        ① **"不是用户的新要求"** —— 注入的是一条 system 消息，不写这句，模型可能
           把它当成新任务去做（"好的，我重新开始"），而它要做的恰恰是**接着干**；
        ② **"也不是让你重新开始"** —— 只提醒、不改变任务。

        ``goal``：原始目标。给了就在末尾**回读一次**。这不是装饰 —— 提醒本身只说
        "你刚才做得可能有问题"，模型修正时唯一能参照的就是它；目标不在眼前时，
        最常见的反应是"换个更保守的做法把这一步糊过去"，而不是"回到要求上"。
        每步都回读原文会把历史翻倍，所以只在**提醒真正发生**时回读（这一轮本来
        也没几步会触发）。
        """
        parts = [f"[自动检查] {self.summary or '系统发现刚才的做法可能有问题'}。"]
        if self.problem in _PROBLEM_TEXT:
            parts.append(f"判定：{_PROBLEM_TEXT[self.problem]}。")
        if self.step:
            parts.append(f"涉及第 {self.step} 步。")
        if self.advice:
            parts.append(f"建议：{self.advice}")
        if goal:
            parts.append(f"回到原始目标看一遍：{goal}")
        parts.append(
            "这**不是**用户的新要求，也不是让你重新开始 —— 继续当前任务，"
            "但请换一种做法。"
        )
        return " ".join(parts)

    def as_revert_note(self, *, goal: str = "") -> str:
        """第 2 级的注入文本：**告诉模型那一步已经被撤掉了**。

        ⚠️ 不写"已撤掉"会得到最坏的一种组合：模型看不到那一步，却收到一段
        "你在第 N 步打转"的提醒 —— 它要么当成幻觉忽略，要么再去把那个动作做一遍
        （而"再做一遍"正是退回要阻止的事）。
        ⚠️ **"不是用户的新要求"这句同样必须留**：撤回说明看着更像一条指令。
        ``goal`` 同样要回读：撤掉一步之后"接下来该干什么"只能由目标回答，
        否则模型会顺手挑一个"看起来更安全"的动作重来。
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
        if goal:
            parts.append(f"回到原始目标看一遍：{goal}")
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

    last_problem: str = ""
    """最近一次判出来的问题类型（``loop`` / ``stuck`` / ``off_track``）。

    只用于指标的 ``problem`` 标签（见 ``_raise_stopped``）。⚠️ 它是"最近一次"而不是
    "最严重的"：指标要回答的是"这次介入是被哪类问题触发的"，而处置永远跟着最近一次
    结论走 —— 用累计里最严重的那个会与实际处置对不上。
    """

    stopped: str = ""
    """第 3 级（停止重试）的**停因**；空串 = 没停。

    取值 = ``capped`` / ``side_effect`` / ``not_found`` / ``not_fixed``（见模块 docstring
    "第 3 级"）。它同时是两个地方的判据：**注入口**（要不要抛）与**兜底判定**
    （"为什么没有产出"）。⚠️ 只置一次 —— 第一个停因就是原因，后面的都是它的后果。
    """

    tasks: set[Any] = field(default_factory=set, repr=False)
    """在飞的后台任务的**强引用** —— 不持有会被 GC 掉（回收时任务可能停在半个动作上）。"""


class AgentStoppedByGuard(Exception):
    """第 3 级踩刹车时从**注入口**抛出去，用来中断本次运行。

    为什么必须是**专有类型**、不能拿 ``RuntimeError`` 顶：``runner.run_agent`` 里有一个
    ``except Exception`` 的兜底（上游抖动、协议错都落进它）。拿普通异常去中断会被它吞成
    ``exit_reason="error"`` —— 表现是"停是停了，但理由变成了'崩溃'"，而运维看到
    "崩溃率上升"会去查一个根本不存在的故障。专有类型让 ``runner`` 能在兜底**之前**
    认出它、翻成 ``exit_reason="guard_stop"``。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"多步防护第 3 级：停止重试（{reason}）")
        self.reason = reason


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


_MAX_GOAL_CHARS = 500
"""喂给反思的原始目标最多多长。

为什么要截：目标就是用户那句话，而入口允许到 10 万字符。整段塞进去会让"判一次
打转"的输入随用户输入线性膨胀 —— 而反思只想知道"要解决什么"，不需要原文的每个字。
截断会标注出来（``（已截断）``），不让模型以为那就是全文。
"""


def _goal_text(context: Any) -> str:
    """清洗 + 截断后的原始目标；没有目标时返回空串。

    ⚠️ 没有目标时必须返回空串、由调用方**整行不写**，而不是写一句"完成用户的任务"：
    一句放之四海皆准的目标会让 ``off_track`` 永远判不出来 —— 任何动作都"还算在完成
    任务"，而那正是这条证据要防的失灵。
    """
    goal = " ".join(str(getattr(context, "goal", "") or "").split())
    if not goal:
        return ""
    if len(goal) > _MAX_GOAL_CHARS:
        goal = goal[:_MAX_GOAL_CHARS] + "…（已截断）"
    return goal


def _goal_line(context: Any) -> str:
    """证据里那行原始目标；没有目标时返回空串。"""
    goal = _goal_text(context)
    if not goal:
        return ""
    return f"原始目标（判断是否偏离时以它为准）：{goal}"


def evidence_of(context: Any) -> str:
    """交给反思模型的**证据**（结构化、只给有迹象的那几条）。

    只给"有迹象的"是刻意的：把全部步骤发过去，输入会随任务长度线性膨胀，而长任务里
    绝大多数步骤是正常的 —— 反思模型要在噪声里找信号，成本还随任务一起涨
    （方案风险清单里那条"反思比主线还贵"）。有迹象的三类：

    * **失败的**（``stuck`` 最直接的证据）；
    * **重复的**（``loop`` 最直接的证据）；
    * **重量级的**（要判"跑偏"就得看到它到底调了几次专家、落了几次盘）。

    再补两样（保证模型能回答"此刻停在哪、在不在绕"）：
    * **最近的动作顺序**（``_MAX_SEQUENCE`` 步的工具名，按发生先后）—— 2026-10-21 加。
      理由：上面那三类是**按指纹聚合**的，``A→B→A→B`` 在聚合视图里只是"A 两次、B 两次"，
      读起来像"查了两遍资料"而不像"来回试"。这个往复是 ``repeat`` 判据的结构性盲区
      （每个动作都没到 3 次），只有顺序能看出来；
    * 最近两条（保证模型知道"现在停在哪一步"）。

    去重后按发生顺序排列、最多 ``_MAX_EVIDENCE`` 条。
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
    lines: list[str] = []
    goal = _goal_line(context)
    if goal:
        lines.append(goal)
    oscillate = counts.get("oscillate", 0)
    tally = (
        f"工具调用共 {counts['tools']} 次（其中重量级 {counts['heavy']} 次），"
        f"换人 {counts['hops']} 次，同一动作最多重复 {counts['repeat']} 次"
    )
    if oscillate >= 2:
        # 只在真的转起来时才写：``0`` / ``1`` 圈是常态，写进去只是噪声。
        tally += f"，最近的动作在来回重复（已 {oscillate} 圈）"
    lines += [tally + "。"]
    # ★ 动作**顺序**必须单独给一行（2026-10-21）：上面那份"涉及的动作"是按指纹聚过的，
    #   ``A→B→A→B`` 在里面读起来就是"A 重复 2 次、B 重复 2 次"，**看不出它们在交替** ——
    #   而那正是"在两个方案之间来回试"的形状。只给工具名（不带参数）控制长度。
    sequence = [getattr(s, "tool", "") for s in steps[-_MAX_SEQUENCE:]]
    sequence = [name for name in sequence if name]
    if len(sequence) >= 3:
        lines.append("最近的动作顺序（看有没有来回重复）：" + " → ".join(sequence) + "。")
    lines.append("涉及的动作（可能不全，只列了有迹象的）：")
    for step in picked:
        detail = (getattr(step, "detail", "") or "").strip()
        lines.append(
            f"- 第 {getattr(step, 'seq', 0)} 步 {getattr(step, 'tool', '')} "
            f"[{getattr(step, 'status', '')}]{(' ' + detail) if detail else ''}"
        )
    return "\n".join(lines)


_MAX_EVIDENCE = 20
"""证据条数上限 —— 反思的输入也要有界（它与主线共用同一个网关配额）。"""

_MAX_SEQUENCE = 10
""""最近的动作顺序"最多列几步。

10 的理由：判断一个往复至少要看两个完整周期（``A B A B``），而周期最长 3
⇒ 6 步是下限；取 10 留出"周期 3 转两圈多一点"的空间。再长就只是把证据变成
一份步骤清单 —— 而它要回答的是"此刻还在不在绕"，不是"从头都干了什么"。
"""


async def _run(context: Any, evidence: str, trigger: str) -> None:
    """后台跑一次反思：调旁路模型、解析结论、放进待注入队列。

    ⚠️ 这个协程**活得比主线久**（发起后不 await）。它结束时主线可能已经交付完了 ——
    那时写进 ``pending`` 也没人再读（"迟到 = 白跑"的一种，属可接受损失）。
    所以它**绝不能抛异常**出去：一个没人 await 的 task 抛异常只会在事件循环的
    异常处理器里留一条记录，而有人的代码以为"反思会自己好"。
    """
    started = time.perf_counter()
    stats = getattr(context, "stats", None)
    try:
        raw = await asyncio.to_thread(
            _reflect_sync,
            evidence,
            on_usage=lambda usage: budget.note_bypass(stats, "reflection", usage),
        )
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
    # 记住"最近一次判出来的问题类型"，只为了给指标的 ``problem`` 标签 —— 第 3 级是在
    # ``_raise_stopped`` 里抛的，那里手里只有 ``state.stopped``（一个停因字符串），
    # 拿不到 finding。不记这一笔，刹车指标就只能回答"停过几次"，
    # 回答不了"因为哪类问题停的" —— 而后者才是决定改提示词还是改配置的依据。
    state.last_problem = finding.problem
    revert, refusal = _revert_decision(state, finding)
    if revert:
        state.reverted += 1
        finding.level = 2
    elif refusal:
        observability.counter("agent_recovery_skipped_total", reason=refusal)
    # 第 3 级：该退却退不了 / 撤了也没用 ⇒ 置停因。⚠️ 这里**只置标记、不抛** —— 这是
    # 后台任务，抛出去没人接。真正的"停"在下一次注入口（那里才握有控制权）。
    state.stopped = state.stopped or _level3_reason(state, finding, refusal)
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


def _level3_reason(state: ReflectionState, finding: ReflectiveFinding, refusal: str) -> str:
    """该不该上第 3 级（停止重试）；返回停因，空串 = 不停。

    顺序与 ``_revert_decision`` 对齐：**先判升级线** —— 没到线的时候既不该撤、也不该停，
    那正是"先提醒 N 次"里的前几次。
    """
    if state.kinds.get(finding.problem, 0) < _level2_line():
        return ""
    if refusal:
        # 「该退却退不了」：额度用完（``capped``）/ 那一步是落盘类不可撤（``side_effect``）。
        return refusal
    if state.reverted > 0 and state.kinds.get(finding.problem, 0) > _level2_line():
        # 「撤了也没用」：已经成功撤过，同类问题**又**出现。
        # ⚠️ 它排在 refusal 之后是有意的：默认配置下（撤回额度只有 1 次）同一个现象会先报
        # ``capped``，而"额度没了"是**更具体**的原因 —— 报它才知道该调哪个配置。
        return "not_fixed"
    return ""


def _raise_stopped(state: ReflectionState) -> None:
    """抛 ``AgentStoppedByGuard``（**唯一**计 ``level=3`` 的地方）。

    为什么在"抛"这一处计数、而不是在判定那一处：判定出来 ≠ 真的停成了 —— 若判定发生时
    运行已经跑完（不再有模型调用），刹车就踩不下去。这个指标要回答的是"**真的中断过几次**"。
    """
    observability.counter(
        "agent_recovery_level_total",
        level="3",
        # 没有 problem 时给 `unknown` 而不是空串：空标签值在这个项目的指标口径里
        # 无法与"标签没设"区分，看板上会悄悄丢进一条看不见的序列。
        problem=state.last_problem or "unknown",
    )
    logging.warning("多步防护第 3 级：停止重试（原因=%s）", state.stopped)
    raise AgentStoppedByGuard(state.stopped)


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
    "你在监督一个正在执行任务的智能体。下面给你它**要做的事**、"
    "**已经做过的动作**（含动作发生的先后顺序）与几个计数。"
    "请判断它是不是出了问题，并**只输出一个 JSON 对象**，字段如下：\n"
    '{"problem": "ok|loop|stuck|off_track", "summary": "一句话说明", '
    '"step": 出问题的步号（没有就给 0）, "advice": "一句可执行的建议"}\n'
    "\n"
    "取值含义：\n"
    "- ok：没有异常，动作推进正常（**拿不准就选它**）。\n"
    "- loop：在原地打转。两种形态都算：**同一个动作被反复做**，"
    "以及**两三个动作来回重复**（A→B→A→B，换了个做法但思路没换）。\n"
    "- stuck：卡住了 —— 反复失败，没有推进。\n"
    "- off_track：偏离了原来的任务。\n"
    "\n"
    "判 off_track 时必须对照第一行的「原始目标」：问自己"
    "「照现在这条路走下去，还能交付目标要的那个东西吗」。"
    "**只有明显答偏（做的是另一件事、或早就跑题）才判它** —— "
    "目标没写全、动作看起来绕着走但仍在为它服务，都算 ok。\n"
    "\n"
    "判断 loop 时以「最近的动作顺序」那一行为准，不要只看计数："
    "两个动作各自只做过两次，也可能已经是一个来回。\n"
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


def _reflect_sync(evidence: str, *, on_usage: Any = None) -> dict | None:
    """跑一次反思（**同步**：由 ``_run`` 丢进线程，理由同 ``compaction`` —— 一次
    同步 HTTP 往返跑在协程里会把本进程所有并发请求一起停住）。

    返回 ``None`` 的含义是"这次没结论"（没配 Key / 调用失败 / 超时 / 解析不出来），
    调用方按"没问题"处理。

    on_usage: 拿到这一次的 ``usage`` 时回调一次（``budget.note_bypass`` 的入口）。
        为什么用回调而不是把 usage 塞进返回值：返回值还带着"有没有结论"这个语义，
        改成二元组会让**每一个**调用点都要拆一次；而 usage 与"有没有解析出结论"
        是正交的两件事（解析失败也照样花了钱，那一笔也必须在账上）。
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
    # 记在**成功返回之后、解析之前**：这一次出站调用已经花掉了，不管后面能不能
    # 解析出结论。放到 return 之后就没机会了，放到前面则失败路径也会错记一笔。
    if on_usage is not None:
        usage = getattr(resp, "usage", None)
        if usage is not None:
            on_usage(usage)
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
    """本口的**全部**动作：持久撤回 + 注入提醒 + 执行第 2 级退回 + 第 3 级刹车。

    四步：等一下 → 滤掉已撤回的 → 处理待注入队列 → 装回输入。
    ⚠️ 其中两个位置会**抛 ``AgentStoppedByGuard``**（第 3 级踩刹车）：进门前发现已经该停，
    以及处理队列时新判出停因（``not_found``）。抛出即"本次运行到此为止"。
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
    if state.stopped:
        # 已经该停了 ⇒ **立刻抛**，连"等在飞的反思"都不做：那几秒买不到任何东西，
        # 运行马上就被中断（理由见模块 docstring "第 3 级"）。
        _raise_stopped(state)
    if state.pending or state.tasks:
        await _wait_for_pending(state)

    original = list(getattr(model_data, "input", None) or [])
    items = _drop_revoked(original, state.revoked) if state.revoked else original
    changed = items is not original

    notes: list[str] = []
    #: 原始目标只取一次 —— 提醒与退回文案都要回读它（见 ``as_note`` 的说明）。
    goal = _goal_text(context)
    for finding in state.pending:
        if finding.level == 2:
            items, dropped = _apply_revert(items, state, finding)
            if not dropped:
                # 「该退却退不了」里**只有这里能判**的一种：那一步已经被 token 裁剪或历史压缩
                # 吃掉，模型输入里根本没有它 ⇒ 定位不到就没法撤。
                # ⇒ 第 3 级：停止重试（该退却退不了 = 系统类，见 `PLAN_step5_fallback.md` §2）。
                # ⚠️ 这一条**取代**了早先"当次不退回、降级成一条提醒"的口径：那时还没有
                # "该退却退不了归系统类"这条分类，而"降级成提醒"等于让它悄悄过去。
                observability.counter("agent_recovery_skipped_total", reason="not_found")
                state.stopped = state.stopped or "not_found"
                continue
            changed = True
            notes.append(finding.as_revert_note(goal=goal))
            observability.counter(
                "agent_recovery_level_total", level="2", problem=finding.problem or "unknown"
            )
            logging.info(
                "第 2 级：撤回第 %d 步（%s，call_id=%s），连带移除 %d 条上下文条目",
                finding.step, finding.tool, finding.call_id, dropped,
            )
            continue
        if _take_level1_slot(state, finding.problem):
            notes.append(finding.as_note(goal=goal))
    state.pending.clear()

    if state.stopped:
        # 队列里刚判出来的停因 ⇒ 就地抛。⚠️ 排在"装回输入"**之前**：这一次模型调用本来也
        # 不会发生了，把输入装好再抛只是白干。
        _raise_stopped(state)

    if not notes and not changed:
        return model_data
    return _rebuild(model_data, items, "\n\n".join(notes))


def _take_level1_slot(state: ReflectionState, problem: str = "") -> bool:
    """占一个第 1 级注入额度；没有额度了返回 False（并计一次"额度不够"）。"""
    if state.injected >= max_level1():
        return False
    state.injected += 1
    observability.counter(
        "agent_recovery_level_total", level="1", problem=problem or "unknown"
    )
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
        # ⚠️ ``oscillate`` 必须排在 ``repeat`` **之前**：两者同时到期时只报第一个，
        # 而"两个动作来回"报成"同一动作重复"会让人去查一个不存在的重复调用
        # （看板上那条动作确实只做了两次，与"重复"这个词对不上）。
        "oscillate": _on_oscillation,
        "repeat": _on_repeat,
    }
)
"""放在文件末尾：阈值函数都在上面定义完了，这里只是把它们登记成"触发原因 → 阈值"。

⚠️ 键的顺序就是判定的优先级（工具 → 重量级 → 换人 → **震荡** → 重复）。多个同时到期时只报
第一个 —— 每条结论都会带上触发原因（指标标签），同时报多个会让"到底为什么触发"
这一栏失去意义。
"""
