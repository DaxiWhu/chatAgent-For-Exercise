"""高风险动作的事前闸门：工具级审批（加固阶段三 · 停点①）。

为什么必须有这一层
------------------
项目里两个工具**会真的动磁盘**（``save_text_asset`` / ``generate_image``）。它们有两个
性质决定了靠现有机制兜不住：

* **撤销不了** —— 退回白名单（``reflection._REVERTABLE_TOOLS``）里没有它们。理由很直接：
  删掉对话里那一步，磁盘上那个文件不会消失。
* **事后拦不住** —— 现有唯一的人工审核审的是**最终草稿**，等审到时文件早落盘了。

所以"落盘"这件事**事前没有任何闸门**。本模块就是那道闸门：模型要调这两个工具时，
这一轮运行**就地停下来**，把"它打算干什么"交出去等人点头。

三条实测事实（``tmp_probe/probe_tool_approval.py``，动手前跑的）
----------------------------------------------------------------
1. 审批是**逐个工具**的，不是整批的：同一条响应里如果还调了别的没标审批的工具，
   那些工具**照常执行完**（工具函数内部打点证实）。想让"一处要批、整批都停"，
   SDK 不给这个开关，只能自己补 —— 代价另见 ``PLAN_tool_hardening.md`` §4 第一节。
   ⚠️ 由此推出一条必须记住的：**"高风险"要逐个工具标，标一个不保护一批**。
2. "中断"是**正常返回**，不是异常：``result.final_output`` 为 ``None``、
   ``result.interruptions`` 非空。⇒ 不主动看那个列表就完全察觉不到。
3. 项目**当前完全不看** ``interruptions``（全仓零引用）⇒ 那次运行落到兜底判定表第 ⑤ 行
   = 没有可交付的答复 = ``failed`` / 系统类。**用户看到的是一条错误报告，不是"等你授权"**。
   ⇒ 光给工具加 ``needs_approval`` 是**负收益**，必须与本模块、判定表那一档、图与服务层
   的接线**同时**落地。

"批准后接着跑"为什么成立（``tmp_probe/probe_approval_resume.py``）
-------------------------------------------------------------------
要回答的是"批准之后怎么办"，两条路的分水岭是**能不能把运行现场存下来**：

* **甲：存快照，从断点继续** —— 前面做过的步骤不重做、不重复花钱；
* **乙：不存，批准后把这一轮整个重跑** —— 改动少，但有两个具体代价：
  ① 前面已经检索、评审过的都要再来一遍（每次都再花一次模型的钱）；
  ② 模型这次**未必还调那个工具**（同样的问题可能给出不同决定）⇒ **用户点了同意却
     什么都没发生**，看起来像功能坏了。

甲的门槛只有一个：项目那个 ``TaskContext`` 能不能变成 JSON —— 它里面装着函数
（检索器工厂）和几个自定义对象，天生存不下。探针实测的结论：

* ``result.to_state()`` 可用，拿到的是可继续的运行状态；
* 不传自定义序列化口子 ⇒ **失败**（``TypeError: Object of type WindowsPath ...``）；
  传了 ⇒ 成功；
* 反序列化 + 批准 + 再跑 ⇒ **危险工具真的执行了（0 → 1）**，而**安全工具没有重做
  （保持 1，不是 2）**。

最后那条是这个方案成立的**唯一证据**：若安全工具变成 2，说明"接着跑"实际是"重跑一轮"，
甲就不成立。**所以甲成立。**

``TaskContext`` 的字段怎么取舍（探针 P6a 逐字段普查的结果）
-------------------------------------------------------------
* **原样带走**（纯 JSON）：``tenant`` / ``docs_dir`` / ``citations`` / ``citations_text`` /
  ``human_feedback`` / ``assets`` / ``delivered`` / ``notes``；
* **转形式带走**（有损）：``asset_dir``（``Path`` ⇒ 字符串）、``rendered``（``set`` ⇒ 列表）；
* **刻意不带**：``retriever_factory``（函数）、``stats``（单次运行的统计）、
  ``steps``（本次运行的动作清单）、``reflection``（反省状态）—— 前三个恢复时**重建更准**，
  最后一个**根本存不下**（它握着在飞的后台任务这类活对象）。

⚠️ 恢复时实际用的是 ``context_override``（把**当前**的活上下文交回去），而不是拿这份
数据反序列化。这份数据的作用是"让快照自包含、便于排查"，同时是将来跨进程恢复的底子。
两条路都留着，是因为它们的失效方式不同：override 依赖"恢复时能重建出等价上下文"
（图状态里有租户与引用，够用），而反序列化依赖"字段清单不出错"。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from ..envutil import env_int

__all__ = [
    "ApprovalRequest",
    "ResumeRequest",
    "approval_enabled",
    "approval_timeout_seconds",
    "delivery_needs_approval",
    "needs_approval_callback",
    "requests_from",
    "restore",
    "snapshot",
]

DEFAULT_TIMEOUT_SECONDS = 600
"""等多久没反应就终止这次任务（秒）。**十分钟** —— 用户拍板的值。

⚠️ SDK **没有**审批超时原语（中断之后那次运行会一直挂着，没人替它过期）。所以这个
时间是**自己管**的：待批项进审核队列时记下时刻，由服务层在请求路径上惰性扫（见
``app`` 侧）。不用后台定时任务，因为多副本下它会重复触发。
"""


def approval_enabled() -> bool:
    """工具级审批总开关（**默认关**）。

    按项目老规矩"新能力默认关"：打开它会让**落盘动作停下来等人**，这是对外可见的
    行为变化（而且用户不点头就什么都做不成）。关着时行为与改造前**逐字节一致**。
    """
    return bool(env_int("AGENT_TOOL_APPROVAL", 0, lo=0, hi=1))


def approval_timeout_seconds() -> int:
    """等用户点头的上限（秒）。见 ``DEFAULT_TIMEOUT_SECONDS`` 那段说明。"""
    return env_int("AGENT_APPROVAL_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, lo=1)


async def needs_approval_callback(
    run_context: Any, tool_parameters: dict, call_id: str
) -> bool:
    """官方 ``FunctionTool.needs_approval`` 的回调（**按危险程度**判定）。

    签名取自官方：``Callable[[RunContextWrapper, dict, str], Awaitable[bool]]``
    （``agents/tool.py`` 里 ``needs_approval`` 字段的类型声明）。

    现在只有两个落盘工具挂这个回调，所以"危不危险"由**挂不挂**决定，这里只读总开关。
    留成回调而不是写死 ``True``：将来若出现"参数级别的危险"（例如导出全量数据的
    检索），判据可以就写在这里，不用改工具定义。
    """
    del run_context, tool_parameters, call_id  # 目前只按总开关判定
    return approval_enabled()


async def delivery_needs_approval(
    run_context: Any, tool_parameters: dict, call_id: str
) -> bool:
    """交付时要不要停下来等人过目 —— **由模型自己声明**（读工具参数 ``needs_review``）。

    与 ``needs_approval_callback`` 的分工，是两种完全不同的事，别合并：

    * 那边问的是"这个动作危不危险"（落盘、生成图片）—— 由**工具**的属性决定，
      开关 ``AGENT_TOOL_APPROVAL`` 管的是"要不要启用这道闸"。
    * 这里问的是"这次产出要不要人先看一眼" —— 由**模型对这一次交付的判断**决定，
      **刻意不受那个开关约束**。理由：模型没有声明时行为逐字节不变（照常直出），
      所以不存在"默认改变了行为"这回事；而一旦模型声明了，停下来正是它请求的结果。

    ⚠️ 为什么把判断权交给模型（2026-10-20 用户拍板）
    --------------------------------------------------
    原本这道审核挂在图里一个独立节点上，触发条件是运行**结束后**的判定表
    （跑偏 / 护栏拦下）。换成官方 interruption 之后那个条件用不了了：等判定表
    出结论时，运行已经结束，没有"可以停下来的地方"可回退。
    两个时刻对不上，只能把"要不要审"提前到**交付那一刻**由执行者自己说 ——
    它是整条链路上唯一在那一刻知道"我这次交的是什么、该不该让人先看"的角色。

    代价用户已知并接受：模型漏声明时，该审的那次会直接交付，且没有任何报错。
    想收紧的话，正确的位置是 ``frontline`` / ``literary_writer`` 的 instructions
    （告诉它哪几类产出必须声明），**不是**在代码里另加一道事后判据。

    tool_parameters: 官方传的是模型生成的参数（已按工具签名解析）。``needs_review``
        缺省 False —— 老模型没传这个字段时行为不变。字符串形态一并兜住：模型偶尔
        会把布尔值写成 ``"true"``，直接 ``bool("false")`` 会得到 True（反向踩坑）。
    """
    del run_context, call_id  # 只按模型这一次的声明判定
    wanted = (tool_parameters or {}).get("needs_review", False)
    if isinstance(wanted, str):
        return wanted.strip().lower() in ("1", "true", "yes", "y")
    return bool(wanted)


@dataclass(frozen=True)
class ApprovalRequest:
    """一个**等着用户点头**的动作。"""

    tool: str
    """工具名（``save_text_asset`` / ``generate_image``）。"""

    arguments: str
    """调用参数（JSON 串）。⚠️ 这里是**模型想做什么**，直接呈现给审核人。"""

    call_id: str = ""
    """官方给这次调用分配的编号。恢复时要靠它把"批准"对回到具体那一次调用。"""

    @property
    def summary(self) -> str:
        """一行给人看的说明（进日志与待审列表）。"""
        args = self.arguments if len(self.arguments) <= 200 else self.arguments[:200] + "…"
        return f"{self.tool}：{args}"


@dataclass(frozen=True)
class ResumeRequest:
    """「接着跑一次被打断的运行」所需的全部输入。

    单独一个类型、而不是给 ``run_agent`` 加三个散参数：这三样永远一起出现
    （有快照才谈得上批不批、有批才谈得上给不给原因），散着传迟早会有人只传一半。
    """

    snapshot: str
    """被打断那次运行现场（``snapshot()`` 的产物）。"""

    approved: bool
    """用户是同意还是拒绝。拒绝**不等于**终止 —— 见 ``restore`` 的说明。"""

    reason: str = ""
    """拒绝的原因（给模型看；空串由官方给默认措辞）。"""


def requests_from(result: Any) -> list[ApprovalRequest]:
    """从一次运行的结果里取出**待批项**（没有则空列表）。

    ``result.interruptions`` 是官方在中断那一刻填好的（``build_interruption_result``）。
    读它有两个要点：
    * 用 ``getattr`` 兜底 —— 老版本 SDK 没有这个字段；没有字段时返回空列表，
      行为退化回"没接审批"，而不是抛异常把运行带崩；
    * 参数统一成字符串 —— 官方给的可能已是 JSON 串，也可能给的是 dict。
    """
    items = getattr(result, "interruptions", None) or []
    out: list[ApprovalRequest] = []
    for item in items:
        raw_args = getattr(item, "arguments", "") or ""
        if not isinstance(raw_args, str):
            raw_args = json.dumps(raw_args, ensure_ascii=False)
        out.append(
            ApprovalRequest(
                tool=str(getattr(item, "tool_name", "") or ""),
                arguments=raw_args,
                call_id=str(getattr(item, "call_id", "") or ""),
            )
        )
    return out


def snapshot(result: Any) -> str:
    """把运行现场转成 JSON 串（**可落库**）。

    只能对"中断了的"运行调用 —— 官方会在没有当前 agent 时抛错，那是正常的
    （说明这次运行已经结束了，没有现场可存）。
    """
    return result.to_state().to_string(context_serializer=_context_payload)


def _context_payload(ctx: Any) -> dict:
    """挑出"能存得回来"的上下文字段（取舍理由见模块 docstring 末节）。"""
    return {
        "tenant": getattr(ctx, "tenant", "") or "",
        "docs_dir": getattr(ctx, "docs_dir", "") or "",
        "asset_dir": str(getattr(ctx, "asset_dir", "") or ""),
        "citations": list(getattr(ctx, "citations", None) or []),
        "citations_text": getattr(ctx, "citations_text", "") or "",
        "human_feedback": getattr(ctx, "human_feedback", "") or "",
        "assets": list(getattr(ctx, "assets", None) or []),
        "delivered": dict(getattr(ctx, "delivered", None) or {}),
        "notes": list(getattr(ctx, "notes", None) or []),
        "rendered": sorted(getattr(ctx, "rendered", None) or []),
    }


async def restore(
    snapshot_json: str,
    *,
    agent: Any,
    context: Any,
    approved: bool,
    reason: str = "",
) -> Any:
    """从快照恢复运行状态，并把**待批项**按用户的选择处理掉。

    返回官方 ``RunState``，交给 ``Runner.run(agent, state)`` 接着跑。

    agent: 恢复时用哪个 agent。官方拿它建"名字 → agent"的表，所以必须是**定义了
        那些工具的那一个**（否则待批项对不回去）。
    context: 恢复后这次运行要用的**活上下文**。用 ``context_override`` 交回去，
        于是恢复后的步骤继续写在同一份清单上（而不是从零开始）。
    approved: True = 用户点了同意；False = 拒绝。拒绝**不等于**终止 —— 模型会收到
        "这个动作被拒了"，然后自己想办法（换一条路或如实说明做不到）。**超时**才终止，
        那是另一条路（服务层判，不进这里）。
    reason: 拒绝时**原文回给模型**的那句话（官方参数名是 ``rejection_message``）。
        空串则由官方用运行级错误格式化器或默认措辞。

    ⚠️ 恢复**只能一次**：``RunState`` 消费掉批准结果之后就作废了。所以调用方必须
    保证"同一个待批项只恢复一次"（服务层用队列状态里的"已办理"来挡）。
    """
    from agents.run_state import RunState

    from agents import RunContextWrapper

    state = await RunState.from_string(
        agent,
        snapshot_json,
        context_override=RunContextWrapper(context=context),
    )
    pending = state.get_interruptions()
    for item in pending:
        if approved:
            state.approve(item)
        else:
            state.reject(item, rejection_message=reason or None)
    logging.info(
        "审批结论已应用：%s，涉及 %d 个待批动作", "同意" if approved else "拒绝", len(pending)
    )
    return state
