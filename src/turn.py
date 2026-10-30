"""一次对话回合 = 一次官方 ``Runner`` 运行。没有节点，没有图。

为什么不在这里编排
------------------
官方只有一种执行引擎：``Runner`` 的循环（模型 → tool call / handoff → 再调模型
→ 直到最终答复或 interruption）。本模块**不加第二层调度** —— 它只做三件事：
装上下文、把这一轮的输入交给 ``run_agent``、把结果原样交回去。

⚠️ 这里曾经对应 LangGraph 上的 ``init → compact_history → dispatch → frontline``
四个节点。它们没有一个是"一步 agent 工作"，分别是：复位上一轮字段、压缩历史、
判断能力可用性、跑一轮 —— 前三个都是**请求生命周期**，该在接口层或本次运行的
准备阶段做完；第四个就是 ``Runner.run`` 本身。把它们做成节点，等于给官方主循环
套一层它不需要的壳，而且每个节点边界都会落一份检查点（库里按轮数平方增长）。

跨轮状态在哪
------------
只有一处：官方 ``Session``（``thread_id`` 作会话编号）。不再有"图状态"这份
平行的会话记录 —— 两套状态同时存在时，它们迟早不一致，而不一致的症状
（模型看到的历史与客户端看到的对话对不上）没有任何报错。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agents import approval, runner
from .agents.context import TaskContext
from .rag.pool import DOCS_DIR, get_retriever

__all__ = [
    "ENTRY_AGENT",
    "TurnRequest",
    "build_context",
    "run_turn",
]

#: 入口 agent。每轮都由它接话，它再决定叫哪个帮手或把对话交出去。
ENTRY_AGENT = runner.ENTRY_AGENT

#: 正文增量回调：``async (text, call_id) -> None``。
TextSink = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True)
class TurnRequest:
    """一轮对话的输入。字段全与"这一次运行"有关，不含任何跨轮状态。"""

    prompt: str
    """本轮用户原话。打回那一轮可以传原话（实际送进去的是意见，见 runner）。"""

    thread_id: str
    """会话编号。既是官方会话的主键，也是待审记录与通知的关联键。"""

    tenant: str = ""
    """检索租户。空串会被归一成 ``None``（= 走进程级默认租户）。"""

    human_feedback: str = ""
    """人工打回时的意见。非空 ⇒ 这一轮按"打回轮"处理（不重传原话）。"""

    resume: approval.ResumeRequest | None = None
    """接着跑一次被打断的运行（工具审批）。给了它就不再是"新起一轮"。"""

    max_turns: int | None = None
    """覆盖单次运行的 LLM 调用上限；``None`` 用 ``runner.MAX_TURNS``。"""

    deliver_text: str = ""
    """非空 ⇒ **不跑模型**，直接把这段正文交付出去。

    只有一个调用方用它：``POST /v1/review`` 的 accept 分支。那一轮的语义是
    "把审核人刚批过的正文发给用户"，跑模型既没有必要，还会把同一段内容重写一遍。
    """


def build_context(*, tenant: str, asset_dir: str | Path | None = None) -> TaskContext:
    """装配一次运行要用的上下文。

    为什么检索器传**工厂**而不是实例：``kb_search`` 是模型自己决定要不要调的
    工具，很多轮根本不会检索。开工就构建等于把"可能用不上"的那次构建
    （pg 后端还要连库、做增量同步）提前做掉。
    """
    key = (tenant or "").strip()
    ctx = TaskContext(
        tenant=key,
        docs_dir=DOCS_DIR,
        retriever_factory=(lambda: get_retriever(key or None)),
    )
    ctx.asset_dir = Path(asset_dir or os.getenv("AGENT_ASSET_DIR", "var/agent_assets"))
    return ctx


async def run_turn(
    req: TurnRequest,
    *,
    context: TaskContext,
    on_text: TextSink | None = None,
    trace_metadata: dict[str, Any] | None = None,
) -> runner.TaskOutcome:
    """跑这一轮，返回它的结果。

    on_text: 给了就走官方流式（``Runner.run_streamed``），正文边产边交出去；
        不给就整段返回。两种形态的结果完全一致。
    trace_metadata: 这次运行的元数据，原样透传给 ``runner.run_agent``。
        ``request_id`` 属于传输层概念，本模块不认识它 —— 由 ``app/`` 组装好传进来，
        与 ``src/`` 不反向依赖 ``app/`` 的既有约定一致。
    """
    context.human_feedback = req.human_feedback or ""
    if req.deliver_text:
        # 已通过审核的正文：不跑模型、不调工具，直接算这一轮的结果。
        return runner.TaskOutcome(
            answer=req.deliver_text,
            attempts=1,
            final_agent="",
            passed=True,
            exit_reason="passed",
        )
    extra: dict[str, Any] = {}
    if req.max_turns is not None:
        extra["max_turns"] = req.max_turns
    return await runner.run_agent(
        ENTRY_AGENT,
        prompt=req.prompt,
        context=context,
        session_id=req.thread_id,
        resume=req.resume,
        on_text=on_text,
        trace_metadata=trace_metadata,
        **extra,
    )
