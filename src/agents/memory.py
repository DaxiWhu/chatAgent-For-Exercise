"""长期记忆的两个出入口：**每轮注入**（读）与 **工具写入**（写）。

为什么读要挂在注入口
--------------------
官方给的注入口只有 ``RunConfig.call_model_input_filter``（每次模型调用前拿到完整
输入）。没有"运行结束钩子"，所以"跑完自动提炼"只能是自己加的旁路任务 ——
而记忆是**每轮都要生效**的东西，读挂在 filter 上是唯一不依赖自定义调度的位置。

写入为什么以工具为主
--------------------
1. 它是官方原语（``FunctionTool``），不需要发明机制；
2. 用户说"记住……"时**当轮就生效**，可解释；后台抽取是"下一轮才可能生效"；
3. 不额外花模型调用 —— 后台抽取每轮都要多调一次。

代价要认：**模型可能不调**。所以 instructions 里明确写了"用户给出长期偏好/结论时
必须调用"，并配了指标 ``agent_memory_writes_total{result}`` 看每百次对话写了几条。
这个数长期偏低说明指令没生效，不是"用户没有可记的"。

开关
----
``AGENT_MEMORY`` 默认**开**（与压缩相反）。理由：压缩会改写历史（破坏性），
而记忆只新增一张表、一条注入；表为空时注入函数返回空串，**零影响**。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from .. import observability
from . import memory_store

__all__ = [
    "enabled",
    "inject_memories",
    "remember_memory",
    "forget_memory",
    "active_memories",
]

_TRUE = ("1", "on", "true", "yes")
_FALSE = ("0", "off", "false", "no")

#: 注入文本的字符上限。记忆是**每轮**都进的，不设上限等于让"记了很多条的用户"
#: 每轮多付一笔固定成本 —— 而真正要紧的那几条早已由排序放在了最前面。
MAX_INJECT_CHARS = 2000

#: 注入块的识别前缀。它有两个用途：内容自查（日志里一眼看出这段是哪来的）、
#: 以及"同一份输入被重复加工时不重复插入"。
MARKER = "以下是关于当前用户的长期记忆"


def enabled() -> bool:
    raw = (os.getenv("AGENT_MEMORY", "") or "").strip().lower()
    if raw in _FALSE:
        return False
    return raw in _TRUE or raw == ""


def _dsn() -> str:
    """``DB_URL``（与 ``session.py`` 同源）。链路层直读环境变量，理由见那边 docstring。"""
    return (os.getenv("DB_URL", "") or "").strip()


def _store() -> Any:
    return memory_store.make_memory_store(_dsn())


def active_memories(owner_key: str, *, limit: int = 40) -> list[dict]:
    """取某个用户仍然有效的记忆（**同步**，工具与注入两条路共用）。失败返回空。"""
    if not owner_key:
        return []
    try:
        return _store().active(owner_key, limit=limit)
    except Exception as e:  # noqa: BLE001  记忆是增强，读不到不该影响对话
        observability.counter("agent_memory_errors_total", stage="read",
                             kind=type(e).__name__)
        logging.warning("长期记忆读取失败（已忽略，本轮没有记忆）：%s", e)
        return []


async def inject_memories(model_data: Any, *, context: Any = None) -> Any:
    """把有效记忆作为**一条 system 消息**插到输入最前面。

    为什么放最前（与反思提醒放最后相反）：记忆是"背景"，不是"关于你刚才做法的提醒"。
    放最前还带来一个必须保住的性质 —— **内容稳定时前缀就稳定**，上游缓存能命中；
    记忆一旦每轮重排，那一轮就是全量重算（变慢变贵，且没有任何报错）。

    ⚠️ 排在裁剪**之后**（见 ``runner.model_input_filter`` 的顺序说明）：放前面会被
    当成"超预算的老历史"裁掉，症状是"明明记着、回答时却不知道"。
    """
    if not enabled():
        return model_data
    owner = str(getattr(context, "owner_key", "") or "")
    if not owner:
        # 无法归属（匿名 / 机器调用）⇒ **不读也写**。读一个公共桶比不读更糟：
        # 别人的偏好会被当成这个人的。
        return model_data
    items = getattr(model_data, "input", None)
    if not isinstance(items, list):
        return model_data
    if items and MARKER in _text_of(items[0]):
        return model_data   # 已经插过（同一次调用被加工两次）

    # 查库放线程里：同步驱动跑在协程里会把本进程所有并发请求一起停住。
    rows = await asyncio.to_thread(active_memories, owner)
    text = memory_store.render(rows, limit_chars=MAX_INJECT_CHARS)
    if not text:
        return model_data
    try:
        from agents.run_config import ModelInputData
    except Exception:  # noqa: BLE001  SDK 换了形状 ⇒ 放弃改写（不改变行为）
        return model_data
    observability.histogram("agent_memory_injected", len(rows))
    return ModelInputData(
        input=[{"role": "system", "content": text}, *items],
        instructions=getattr(model_data, "instructions", "") or "",
    )


def _text_of(item: Any) -> str:
    content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
    return content if isinstance(content, str) else ""


def remember_memory(*, owner_key: str, content: str, kind: str = "project",
                    tenant: str = "", source_thread: str = "",
                    supersedes: str = "") -> dict:
    """写一条记忆（追加式）。**同步** —— 工具函数直接调。"""
    if not enabled():
        return {"ok": False, "reason": "disabled"}
    if not owner_key:
        return {"ok": False, "reason": "no_owner"}
    try:
        result = _store().remember(
            owner_key, content, kind=kind, tenant=tenant,
            source_thread=source_thread, supersedes=supersedes,
        )
    except Exception as e:  # noqa: BLE001
        observability.counter("agent_memory_errors_total", stage="write",
                             kind=type(e).__name__)
        logging.warning("长期记忆写入失败：%s", e)
        return {"ok": False, "reason": f"{type(e).__name__}"}
    observability.counter("agent_memory_writes_total",
                          result=str(result.get("action") or result.get("reason") or "?"))
    return result


def forget_memory(*, owner_key: str, memory_id: str = "", content: str = "") -> int:
    """把一条记忆标成失效（**不物理删**）。返回受影响条数。"""
    if not enabled() or not owner_key:
        return 0
    try:
        n = _store().forget(owner_key, memory_id=memory_id, content=content)
    except Exception as e:  # noqa: BLE001
        observability.counter("agent_memory_errors_total", stage="forget",
                             kind=type(e).__name__)
        logging.warning("长期记忆失效标记失败：%s", e)
        return 0
    observability.counter("agent_memory_forgets_total", result="ok" if n else "miss")
    return n
