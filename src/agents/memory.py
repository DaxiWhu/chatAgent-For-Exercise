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
from . import memory_index, memory_store

__all__ = [
    "enabled",
    "inject_memories",
    "remember_memory",
    "forget_memory",
    "forget_batch_memories",
    "active_memories",
    "retrieve_above",
]

_TRUE = ("1", "on", "true", "yes")
_FALSE = ("0", "off", "false", "no")

#: 注入文本的字符上限。记忆是**每轮**都进的，不设上限等于让"记了很多条的用户"
#: 每轮多付一笔固定成本 —— 而真正要紧的那几条早已由排序放在了最前面。
MAX_INJECT_CHARS = 2000

#: 注入块的识别前缀。它有两个用途：内容自查（日志里一眼看出这段是哪来的）、
#: 以及"同一份输入被重复加工时不重复插入"。
MARKER = "以下是关于当前用户的长期记忆"

#: 每次注入最多几条。40 条 × 平均 40 字 ≈ 1600 字，与 ``MAX_INJECT_CHARS`` 同量级。
MAX_INJECT_ITEMS = 40

#: 检索召回的条数（最终注入的是它的子集：还要过一遍"是否仍有效 / 是否已淘汰"）。
RETRIEVE_TOP_K = 20

#: 记忆条数**超过**这个值才从"全量注入"切到"检索召回"。默认 40。
#:
#: 为什么要这个阈值，而不是一律检索：全量注入更准（不存在召回失败这回事），
#: 而且**前缀稳定** —— 同一份记忆每轮算出来的文本逐字节一致，上游缓存能命中。
#: 检索的代价不止是每轮多一次 embedding + 一次向量查询，更难缠的是召回结果随
#: 问题变化 ⇒ 前缀每轮都变 ⇒ 缓存必然失效，每轮全量重算。它是"条目多到装不下"
#: 时的必需品，不是越多越好。
_RETRIEVE_ABOVE_DEFAULT = 40

#: 检索用的查询文本最多取多少字。取本轮用户发言的**开头**：embedding 对超长输入
#: 没有额外收益，而它每轮都要跑一次。
_QUERY_MAX_CHARS = 800


def retrieve_above() -> int:
    """切换检索的条数阈值（``AGENT_MEMORY_RETRIEVE_ABOVE``，默认 40）。"""
    raw = (os.getenv("AGENT_MEMORY_RETRIEVE_ABOVE", "") or "").strip()
    try:
        return max(1, int(raw)) if raw else _RETRIEVE_ABOVE_DEFAULT
    except ValueError:
        return _RETRIEVE_ABOVE_DEFAULT


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


def _index() -> Any:
    """向量索引单例。**DSN 与主表同源** —— 否则会索引到另一张表上，
    表现为"检索永远返回空"，而它不报错。"""
    return memory_index.make_index(_dsn())


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
    """把有效记忆作为**一条 system 消息**插在**系统提示之后、对话历史之前**。

    位置是用户 2026-11-02 定的口径，实现上就是 ``input`` 列表的第一条：
    ``instructions`` 是独立字段，SDK 组装时排在 ``input`` 前面 ⇒ 最终顺序是
    系统提示 → 记忆块 → 对话历史 → 本轮提问 → （反思/补充提醒）。

    为什么是这个位置，而不是整份输入的最末尾：
    ① **语义上它是背景**，背景该出现在对话开始之前，不该挤在用户刚说完的话旁边 ——
       贴着本轮提问，模型很容易把一条旧偏好读成"用户现在要求我这么做"；
    ② 它属于 system 层，模型读它时知道这是系统给的前情，不是用户说的话。

    代价要认：这个位置**紧挨稳定前缀**。40 条以内全量注入时内容只由记忆库决定，
    前缀稳定、上游缓存能命中；一旦进入检索模式，召回随本轮问题变化 ⇒ 记忆块每轮
    不同 ⇒ 它后面的系统提示之外的历史全部重算。这是"语义正确优先"的取舍，
    不是疏漏 —— 想省钱就把 ``AGENT_MEMORY_RETRIEVE_ABOVE`` 调大，让更多用户
    走全量注入。

    ⚠️ 排在裁剪**之后**（见 ``runner.model_input_filter`` 的顺序说明）：插进来的块
    被当成"超预算的老历史"裁掉，症状是"明明记着、回答时却不知道"，而日志全绿。

    两条取回路径（``_select``）：条数不超过 ``retrieve_above()`` 时全量按价值排序
    取前 40 条；超过时按本轮问题去向量索引召回，pinned 仍然无条件注入。
    检索任何一步失败都退回全量 —— 优化失败不能变成功能失败。
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
    rows = await asyncio.to_thread(active_memories, owner,
                                   limit=MAX_INJECT_ITEMS * 5)
    picked = await _select(owner, rows, _query_of(items))
    if not picked:
        return model_data
    text = memory_store.render(picked, limit_chars=MAX_INJECT_CHARS)
    if not text:
        return model_data
    try:
        from agents.run_config import ModelInputData
    except Exception:  # noqa: BLE001  SDK 换了形状 ⇒ 放弃改写（不改变行为）
        return model_data
    # 记一次"这批被读进上下文了"：衰减的基准靠它。失败不影响本轮。
    try:
        await asyncio.to_thread(_store().touch, owner, [r["id"] for r in picked])
    except Exception as e:  # noqa: BLE001
        logging.debug("记忆 touch 失败（已忽略）：%s", e)
    observability.histogram("agent_memory_injected", len(picked))
    return ModelInputData(
        input=[{"role": "system", "content": text}, *items],
        instructions=getattr(model_data, "instructions", "") or "",
    )


async def _select(owner: str, rows: list[dict], query: str) -> list[dict]:
    """决定这一轮把哪几条记忆交给模型。

    条数不超过阈值 ⇒ 全量（已按价值排好）。超过 ⇒ 向量召回，``pinned`` 无条件保留。

    为什么 pinned 在检索模式下也要塞：它是人显式钉住的（"这条必须一直记着"），
    而相似度只回答"和当前问题像不像"，回答不了"重不重要"。让检索决定 pinned
    的去留，等于把"钉住"这个动作悄悄废掉。
    """
    if not rows:
        return []
    if len(rows) <= retrieve_above() or not query:
        return rows[:MAX_INJECT_ITEMS]
    try:
        hits = await asyncio.to_thread(
            _index().search, owner, query, RETRIEVE_TOP_K * 2)
    except Exception as e:  # noqa: BLE001
        logging.debug("记忆检索失败，退回全量（已忽略）：%s", e)
        return rows[:MAX_INJECT_ITEMS]
    if not hits:
        observability.counter("agent_memory_retrieve_total", result="empty")
        return rows[:MAX_INJECT_ITEMS]

    by_id = {r["id"]: r for r in rows}
    picked: list[dict] = []
    seen: set[str] = set()
    for ident, _score in hits:
        row = by_id.get(ident)
        if row is None or ident in seen:
            # 命中了已失效 / 已淘汰 / 已超过扫描上限的那条：跳过。
            # 向量表是**脏读**的 —— 改口、淘汰、整批回滚都不会去删它
            # （删要向量的物理行，而"失效"的语义只是不参与注入）。
            continue
        picked.append(row)
        seen.add(ident)
        if len(picked) >= RETRIEVE_TOP_K:
            break
    pinned = [r for r in rows if r.get("pinned") and r["id"] not in seen]
    out = (pinned + picked)[:MAX_INJECT_ITEMS]
    observability.counter("agent_memory_retrieve_total", result="hit")
    observability.histogram("agent_memory_retrieved", len(out))
    return out


def _query_of(items: list[Any]) -> str:
    """本轮提问的文本（从后往前找第一条用户发言）。取不到就空串 ⇒ 走全量。"""
    for item in reversed(items):
        role = item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
        if role != "user":
            continue
        text = _text_of(item)
        if text.strip():
            return text[:_QUERY_MAX_CHARS]
    return ""


def _text_of(item: Any) -> str:
    content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
    return content if isinstance(content, str) else ""


def remember_memory(*, owner_key: str, content: str, kind: str = "project",
                    tenant: str = "", source_thread: str = "",
                    supersedes: str = "", source: str = "memory",
                    batch_id: str = "") -> dict:
    """写一条记忆（追加式）。**同步** —— 工具函数直接调。

    ``source`` / ``batch_id``：写入来源与批次。模型主动调 ``remember`` 时是
    ``memory``；压缩时批量抽候选是 ``compaction`` + 同一个批次号，出问题能整批撤回
    （见 ``memory_store.forget_batch``）。

    写完顺手写一条向量。**向量写失败不算记忆写失败**：主表才是事实来源，
    检索只是"在条目很多时挑几条"的优化 —— 优化失败退化成全量注入，功能仍在。
    """
    if not enabled():
        return {"ok": False, "reason": "disabled"}
    if not owner_key:
        return {"ok": False, "reason": "no_owner"}
    try:
        result = _store().remember(
            owner_key, content, kind=kind, tenant=tenant,
            source_thread=source_thread, supersedes=supersedes,
            source=source, batch_id=batch_id,
        )
    except Exception as e:  # noqa: BLE001
        observability.counter("agent_memory_errors_total", stage="write",
                             kind=type(e).__name__)
        logging.warning("长期记忆写入失败：%s", e)
        return {"ok": False, "reason": f"{type(e).__name__}"}
    observability.counter("agent_memory_writes_total",
                          result=str(result.get("action") or result.get("reason") or "?"))
    if result.get("ok") and result.get("action") == "added":
        _index_put(owner_key, str(result.get("id") or ""), content, kind)
        _prune(owner_key)
    return result


def _prune(owner_key: str) -> int:
    """写入后顺带做一次容量淘汰。**失败吞掉**。

    为什么挂在写入后面而不是定时任务：这个项目没有调度器，而"等有调度器再清理"
    的意思是永远不清理。写记忆是低频动作（几百轮才一条），多一次 SELECT 可以忽略；
    没超上限时它查一次就返回，连 UPDATE 都不发。
    """
    try:
        dropped = _store().prune(owner_key)
    except Exception as e:  # noqa: BLE001
        observability.counter("agent_memory_errors_total", stage="prune",
                             kind=type(e).__name__)
        logging.warning("记忆淘汰失败（不影响写入）：%s", e)
        return 0
    if dropped:
        observability.counter("agent_memory_pruned_total", amount=dropped)
        logging.info("记忆容量淘汰：%d 条标为不再注入（用户=%s）", dropped, owner_key)
    return int(dropped or 0)


def _index_put(owner_key: str, memory_id: str, content: str, kind: str) -> None:
    """给新写的记忆补一条向量。**任何失败都吞掉**（理由见 ``remember_memory``）。

    只在 ``action == "added"`` 时调：``refreshed`` 意味着表里已有同一句话，
    内容没变，重算一遍向量只是白花一次 embedding 调用。
    """
    if not memory_id:
        return
    try:
        _index().put(owner_key, [{
            "id": memory_id,
            "content": memory_store.normalize_content(content),
            "kind": kind,
        }])
    except Exception as e:  # noqa: BLE001
        observability.counter("agent_memory_errors_total", stage="index",
                             kind=type(e).__name__)
        logging.warning("记忆向量写入失败（检索将少这一条，全量注入不受影响）：%s", e)


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


def forget_batch_memories(*, owner_key: str, batch_id: str) -> int:
    """整批撤回（一次压缩抽出的候选共用一个批次号）。

    给"发现记忆里混进了不该有的东西"用的：那条链路是后台自动写入，
    进来时没人看过，所以出去时要有一个**不用逐条找**的撤销手段。
    """
    if not enabled() or not owner_key or not batch_id:
        return 0
    try:
        n = _store().forget_batch(owner_key, batch_id)
    except Exception as e:  # noqa: BLE001
        observability.counter("agent_memory_errors_total", stage="forget_batch",
                             kind=type(e).__name__)
        logging.warning("整批撤回记忆失败：%s", e)
        return 0
    observability.counter("agent_memory_forgets_total",
                          result="batch" if n else "batch_miss")
    return n
