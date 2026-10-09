"""动作清单：把一次 agent 运行内部的每一步记下来（工具名 / 参数 / 成败 / 耗时）。

为什么必须有它
--------------
一个 agent 节点 = **一次完整的运行**。这次运行内部，模型可以来回最多
``AGENT_MAX_TURNS``（默认 12）次模型调用，每次可能配一次工具执行。这些来回
**全在节点内部** —— 图不介入、不落检查点。于是从外面只看得到"工具调了几次"，
看不到**调得对不对**。

后果很具体：某个工具三成的调用在报错时，模型会一直重试、答案质量与延迟一起
退化，而看板上呈现的是"工具调用次数正常、模型调用次数偏高" —— 指标会把人引向
提示词，而真凶是工具实现。这张清单就是"看得见中间步骤"的最小形态。

三条边界（都影响怎么用它）
--------------------------
1. **不进 LangGraph state**：挂在 ``TaskContext`` 上（与 ``AgentStats`` 同一条，
   理由见 ``context.AgentStats`` 的 docstring）。它**不需要跨轮延续** —— 每一步
   都属于它自己那一次运行。
   ⚠️ 一次请求会跑**多个** agent 节点（前台 + 文学），而每个节点各建一份
   ``TaskContext`` ⇒ **清单不跨节点**。要"本轮全部步骤"得把几份清单拼起来；
   指标不受影响（它们是全局累加的）。
2. **钩子之间没有共同的关联编号**：``on_tool_start`` / ``on_tool_end`` 都**不带参数**、
   也不带定位编号，参数只能从模型响应那一侧取。所以「参数」与「结果」的配对只能按
   「同名工具 + 先来先配」做；**并发执行同名工具时可能配错**。这对"这个工具失败了
   几次"（按工具名统计）没有影响，但"哪个参数导致了失败"暂时配不准 ——
   已知限制，不要在这个基础上做因果推断。
3. **观测失败绝不影响业务**：这张清单的任何一步出问题，都只降级为"这一步没记上"。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from .. import observability
from ..envutil import env_int

__all__ = [
    "StepLog",
    "ToolStep",
    "calls_from_response",
    "steps_enabled",
    "tool_error_handler",
]

_MAX_DETAIL = 200
"""结果 / 错误摘要的长度上限。只记**摘要**，原文不进日志（与项目口径一致）。"""


def steps_enabled() -> bool:
    """工具步骤记录的总开关（**默认开**）。

    为什么"新能力默认可关"这条惯例在这里不适用：它是**纯观察** —— 不改任何对外
    可见行为。工具失败时回给模型的文本仍由官方默认实现产出（见
    ``tool_error_handler``），指标走旁路、失败只降级为 debug 日志。所以"关掉时与
    今天一致"这条它天然满足，而默认关掉等于"这个指标永远没有数据"。

    仍然**可关**：``AGENT_TOOL_STEPS=0``。要再加一层开关或改默认值，落点就在这里。
    """
    return bool(env_int("AGENT_TOOL_STEPS", 1, lo=0, hi=1))


def _fingerprint(name: str, arguments: Any) -> str:
    """把「工具名 + 参数」算成一个短指纹（用于发现同一动作被重复调用）。"""
    # 参数可能是**字符串**（模型给的原始 JSON）或已经是 dict —— 两种都要能规范化。
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments or {}, ensure_ascii=False)
    try:
        canonical = json.dumps(
            json.loads(raw or "{}"),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        canonical = (raw or "").strip()      # 参数不是合法 JSON 时按原串规范化
    return hashlib.sha1(f"{name}|{canonical}".encode()).hexdigest()[:12]


def _clip(value: Any) -> str:
    """压成一行并截断（要的是摘要，不是原文）。"""
    return " ".join(str(value).split())[:_MAX_DETAIL]


def calls_from_response(response: Any) -> list[tuple[str, str, str]]:
    """从一次模型响应里取出"模型请求了哪些工具" ⇒ ``[(工具名, 参数, 定位编号)]``。

    ⚠️ 判据用**属性存在性**（有名字、且 ``arguments`` 不是 None），刻意不用
    ``isinstance`` 判类型：本项目走的是对话补全接口，转换层给出的形态与 Responses
    接口那一侧不完全一致，按类型判会**静默漏掉全部调用**（表现为"一步都没记上"，
    且没有任何报错）。文本消息项没有 ``arguments``，自然被排除在外。
    """
    out: list[tuple[str, str, str]] = []
    for item in getattr(response, "output", None) or []:
        name = getattr(item, "name", None)
        arguments = getattr(item, "arguments", None)
        if not name or arguments is None:
            continue
        out.append((str(name), str(arguments), str(getattr(item, "call_id", "") or "")))
    return out


def _log_of(ctx: Any) -> StepLog | None:
    """从错误处理函数收到的 ctx 取清单。

    走**鸭子类型**而不是 ``isinstance(ctx.context, TaskContext)``：``steps`` 被
    ``context`` 导入，再反过来导入 ``context`` 就是循环导入。
    """
    log = getattr(getattr(ctx, "context", None), "steps", None)
    return log if isinstance(log, StepLog) else None


def tool_error_handler(tool_name: str):
    """给一个工具造"既保住自愈、又记一条失败"的错误处理函数。

    为什么必须换掉官方默认实现，而不是只接 ``on_tool_end``
    ------------------------------------------------------
    工具抛异常时，官方**先把异常转成一句给模型看的话**，钩子拿到的已经是那句话 ——
    实测：失败路径 ``on_tool_end`` **照样触发**，它的 ``result`` 就是那句错误文本。
    所以从钩子侧**分不出**"真的成功了"与"收到一句错误文本"。

    这里在官方实现外面套一层：先记一条 ``failed``，再把默认实现的结果**原样返回**
    ⇒ 回给模型的文本与官方逐字节一致，自愈能力完全不受影响。

    ⚠️ 回调签名是 ``(ctx, error)``，**没有工具名形参** ⇒ 工具名必须在**构造期**用
    闭包捕获（``tool_error_handler("kb_search")``），不要试图从回调参数里取。
    """

    def handler(ctx: Any, error: BaseException) -> str:
        from agents.tool import default_tool_error_function

        if steps_enabled():
            log = _log_of(ctx)
            if log is not None:
                log.fail(tool_name, error)
        return default_tool_error_function(ctx, error)

    return handler


@dataclass
class ToolStep:
    """一个动作：模型请求调一次工具，以及它的结果。"""

    seq: int
    tool: str
    fingerprint: str = ""
    """「工具名 + 规范化参数」的短哈希 —— 用于发现"同一动作被反复调用"。"""
    call_id: str = ""
    """定位编号。模型请求时给、工具结果带同一个回来 ⇒ 将来"退回第几步"的定位键。"""
    status: str = "running"
    """``running`` / ``ok`` / ``failed``。"""
    detail: str = ""
    """结果或错误的**摘要**（截断，不是原文）。"""
    duration: float = 0.0
    started: float = field(default=0.0, repr=False)
    settled: bool = field(default=False, repr=False)
    """是否已定格并播报过（失败路径会先后被两个来源碰到，靠它防重复）。"""

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "tool": self.tool,
            "fingerprint": self.fingerprint,
            "call_id": self.call_id,
            "status": self.status,
            "detail": self.detail,
            "duration": round(self.duration, 4),
        }


@dataclass
class StepLog:
    """一次运行的动作清单（挂在 ``TaskContext`` 上）。"""

    steps: list[ToolStep] = field(default_factory=list)
    _pending: list[tuple[str, str, str]] = field(default_factory=list, repr=False)
    """已从模型响应里登记、但**还没执行**的调用（``on_tool_start`` 到来时按名取用）。"""

    # -- 只读 --
    @property
    def failed(self) -> list[ToolStep]:
        """失败的步骤（兜底出口要用它列"未完成清单"，不要另写一份描述）。"""
        return [s for s in self.steps if s.status == "failed"]

    def as_list(self) -> list[dict]:
        return [s.as_dict() for s in self.steps]

    # -- 写入（钩子与工具错误处理函数调用）--
    def note_calls(self, calls: list[tuple[str, str, str]]) -> None:
        """登记"模型这次请求了哪些工具"（还没执行 ⇒ 先入队）。"""
        if not steps_enabled():
            return
        self._pending.extend(calls)

    def begin(self, tool: str) -> None:
        """某个工具开始执行。"""
        if not steps_enabled():
            return
        fingerprint, call_id = self._take_pending(tool)
        self.steps.append(
            ToolStep(
                seq=len(self.steps) + 1,
                tool=tool,
                fingerprint=fingerprint,
                call_id=call_id,
                started=time.perf_counter(),
            )
        )

    def finish(self, tool: str, result: Any) -> None:
        """某个工具执行结束。

        ⚠️ **失败路径也会走到这里**（官方把异常转成文本后照样触发钩子 —— 实现在
        SDK 的 ``run_internal/tool_execution.py``，实测过），所以成败**不在这里判**。

        判据只能是"**还有没有未定格的同名步骤**"：

        * 正常路径：``begin`` 刚建的那条未定格 ⇒ 在这里定格为 ``ok``；
        * 失败路径：``fail()`` 已经定格过了 ⇒ 这里**什么都不做**。若改成"补记一条"
          就会给一次失败**补出一条假的成功**，而这个清单的全部价值就是让失败可见 ——
          那个写法会自己把失败率抹平，且症状是"指标正常"，没有任何东西会报错；
        * 没有 ``begin`` 的 ``on_tool_end``（理论上不该有；实测 ``on_tool_start`` 总在
          参数解析之前触发）：**宁少记一条 ok，也不错记一条** —— 少一条只影响分母，
          错记一条会污染分子。
        """
        if not steps_enabled():
            return
        step = self._open(tool)
        if step is not None:
            self._settle(step, "ok", result)

    def fail(self, tool: str, error: Any) -> None:
        """某个工具失败（由 ``tool_error_handler`` 调用）。

        没有 ``begin`` 对应的失败（例如参数 JSON 解析失败：工具根本没执行）会**补记
        一条**，而不是丢掉 —— 那正是"调用错了但看不出来"的一类。
        """
        if not steps_enabled():
            return
        self._settle(self._open(tool) or self._append(tool), "failed", error)

    # -- 内部 --
    def _append(self, tool: str) -> ToolStep:
        step = ToolStep(seq=len(self.steps) + 1, tool=tool)
        self.steps.append(step)
        return step

    def _open(self, tool: str) -> ToolStep | None:
        """最近一条同名、**还没定格**的步骤。"""
        for step in reversed(self.steps):
            if step.tool == tool and not step.settled:
                return step
        return None

    def _take_pending(self, tool: str) -> tuple[str, str]:
        """按「同名 + 先来先配」取一条待执行的调用信息 ⇒ ``(指纹, 定位编号)``。"""
        for index, (name, _, _) in enumerate(self._pending):
            if name == tool:
                _, arguments, call_id = self._pending.pop(index)
                return _fingerprint(tool, arguments), call_id
        return "", ""

    def _settle(self, step: ToolStep, status: str, detail: Any) -> None:
        """定格一条步骤并**播报一次**（重复调用直接返回，不与业务交互）。"""
        if step.settled:
            return
        step.settled = True
        step.status = status
        step.detail = _clip(detail)
        step.duration = max(0.0, time.perf_counter() - step.started) if step.started else 0.0
        observability.counter("agent_tool_result_total", tool=step.tool, status=step.status)
        if step.duration:
            observability.histogram(
                "agent_tool_duration_seconds", step.duration, tool=step.tool
            )
