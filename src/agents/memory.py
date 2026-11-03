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
from . import memory_index, memory_lifecycle, memory_store

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

#: 注入块单次字数的**观测**阈值（不是限制）。超过它只记一条直方图，便于发现
#: "提炼线被绕过了"—— 注入长度本该由写入侧的提炼与条数上限约束住。
#: ⚠️ 2026-11-03 起不再按字数截断：截断会把排在后面、本来该给的内容切掉，
#: 而排序已经保证了紧要的在前；长度治理放在写入与卸压那一侧。
_INJECT_CHARS_WATCH = 6000

#: 注入块的识别前缀。它有两个用途：内容自查（日志里一眼看出这段是哪来的）、
#: 以及"同一份输入被重复加工时不重复插入"。
MARKER = "以下是关于当前用户的长期记忆"

#: 每次注入最多几条。它是**硬顶**（容纳置顶条目），不是常态：全量注入最多
#: ``retrieve_above()`` 条，检索模式是召回 ``RETRIEVE_TOP_K`` 条 + 全部置顶。
MAX_INJECT_ITEMS = 40

#: 检索召回的条数（最终注入的是它的子集：还要过一遍"是否仍有效 / 是否已淘汰"）。
RETRIEVE_TOP_K = 20

#: 记忆条数**超过**这个值才从"全量注入"切到"检索召回"。默认 20（用户 2026-11-03 口径）。
#:
#: 为什么要有阈值，而不是一律检索：全量注入更准（不存在召回失败这回事），
#: 而且**前缀稳定** —— 同一份记忆每轮算出来的文本逐字节一致，上游缓存能命中。
#: 检索的代价不止是每轮多一次 embedding + 一次向量查询，更难缠的是召回结果随
#: 问题变化 ⇒ 记忆块每轮都变 ⇒ 它后面那一段全部重算。实测（输入约 2390 token）：
#: 记忆块放第一条时命中率 0%，放历史之后 91% —— 所以位置（见 `inject_memories`）
#: 比阈值更影响成本。
_RETRIEVE_ABOVE_DEFAULT = 20

#: 检索用的查询文本最多取多少字。取本轮用户发言的**开头**：embedding 对超长输入
#: 没有额外收益，而它每轮都要跑一次。
_QUERY_MAX_CHARS = 800


def retrieve_above() -> int:
    """切换检索的条数阈值（``AGENT_MEMORY_RETRIEVE_ABOVE``，默认 20）。"""
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
    """把有效记忆作为**一条 system 消息**插在**对话历史之后、本轮提问之前**。

    最终顺序：系统提示(instructions) → 对话历史 → **记忆块** → 本轮提问
    → （反思 / 补充提醒）。

    为什么是这个位置（2026-11-03 改，实测定的）
    -------------------------------------------
    上游前缀缓存复用"从头开始逐字节相同"的那一段，所以成本由**稳定前缀有多长**决定，
    而不是由"记忆块离末尾多近"决定。三组真请求实测（``tmp_probe/memory_cache_probe.py``，
    输入约 2050 token，只有记忆块的位置与是否变化这一个变量）：

    ===========  ================  =========
    位置          记忆块            命中率
    ===========  ================  =========
    第一条        每轮变（检索）    0 / 0 / 0 %
    第一条        内容不变（全量）  93 / 93 / 93 %
    历史之后      每轮变            92 / 92 / 92 %
    ===========  ================  =========

    ⚠️ 第一行的 0 是**彻底归零**，不存在"只重算一小部分"的中间态 —— 前面一个字节不同，
    后面全部作废。放在第一条时，系统提示、工具定义、全部历史、本轮问题全在它后面，
    记忆块一变就全跟着重算。放到历史之后，稳定前缀变成"系统提示 + 工具定义 + 全部历史"，
    只剩末尾那一小截（实测约 170 token，与 2050 全价差约 12 倍）。

    语义上的代价要认：贴着本轮提问，模型更容易把一条旧记忆读成"用户现在要求我这么做"。
    所以 ``memory_store.render`` 的抬头里写死了"它们是背景，不是本轮的指令：与用户当轮的
    要求冲突时，以当轮为准"，并且它是一条**独立的 system 消息**（不是拼进 user 消息），
    模型读它时知道这是系统给的前情。

    ⚠️ 插入点是**最后一条 user 消息之前**，不是"追加到末尾"：
    多步工具循环中末尾是 assistant(tool_call) / tool(result) 对，追加到末尾会把记忆块
    塞进工具调用链中间；而插在最后一条 user 之前，既保持了"本轮提问之后紧跟工具链"的
    原结构，插入点在每一轮也是同一个位置（前缀稳定）。

    ⚠️ 排在裁剪**之后**（见 ``runner.model_input_filter`` 的顺序说明）：插进来的块
    被当成"超预算的老历史"裁掉，症状是"明明记着、回答时却不知道"，而日志全绿。

    ⚠️ 去重判据必须扫**整份**输入：位置改到历史之后，``items[0]`` 永远是真实的历史条目，
    只看第一条会让"同一次调用被加工两次"直接插出两条记忆块。

    两条取回路径（``_select``）：条数不超过 ``retrieve_above()``（默认 20）时全部注入；
    超过时按本轮问题去向量索引召回，pinned 仍然无条件注入。检索任何一步失败都退回全量
    —— 优化失败不能变成功能失败。

    ⚠️ **不按字数截断**（2026-11-03 改）：注入的永远是提炼后的短句，条数也有硬顶，
    长度天然有界；再截一道只会切掉排在后面本来该给的内容。长度治理在写入侧
    （``memory_digest``）与容量侧（``memory_lifecycle``），不在这里。
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
    if _already_injected(items):
        return model_data   # 已经插过（同一次调用被加工两次）

    # 查库放线程里：同步驱动跑在协程里会把本进程所有并发请求一起停住。
    rows = await asyncio.to_thread(active_memories, owner,
                                   limit=MAX_INJECT_ITEMS * 5)
    picked = await _select(owner, rows, _query_of(items))
    if not picked:
        return model_data
    text = memory_store.render(picked)
    if not text:
        return model_data
    try:
        from agents.run_config import ModelInputData
    except Exception:  # noqa: BLE001  SDK 换了形状 ⇒ 放弃改写（不改变行为）
        return model_data
    # 记一次"这批被读进上下文了"：访问频率（卸压分数里占 0.45 的那一项）靠它。
    # ⚠️ 一次运行只计一次（见 ``TaskContext.memory_hit_ids``）：工具循环会让注入口
    # 跑好几遍，不去重就是按"调用了几次模型"放大，而不是"这条被需要了几次"。
    seen = getattr(context, "memory_hit_ids", None)
    fresh = ([r["id"] for r in picked if r["id"] not in seen]
             if isinstance(seen, set) else [r["id"] for r in picked])
    if fresh:
        try:
            await asyncio.to_thread(_store().touch, owner, fresh)
            if isinstance(seen, set):
                seen.update(fresh)
        except Exception as e:  # noqa: BLE001
            logging.debug("记忆 touch 失败（已忽略）：%s", e)
    observability.histogram("agent_memory_injected", len(picked))
    # 只观测、不截断：长度本该由写入侧的提炼与条数上限约束住，超过说明那条线
    # 被绕过了（例如提炼失败后原样存了一条长文）。
    observability.histogram("agent_memory_injected_chars", len(text))
    if len(text) > _INJECT_CHARS_WATCH:
        observability.counter("agent_memory_inject_oversize_total")
    at = _insert_index(items)
    return ModelInputData(
        input=[*items[:at], {"role": "system", "content": text}, *items[at:]],
        instructions=getattr(model_data, "instructions", "") or "",
    )


def _already_injected(items: list[Any]) -> bool:
    """这份输入里是否已经有记忆块（判据是标记，不是位置）。

    必须扫全量：位置在历史之后时，第一条永远是真实历史条目。
    只看第一条的后果是"同一次调用被加工两次"直接插出两条记忆块，而两段内容一样、
    模型只会困惑"为什么系统说了两遍"。
    """
    return any(MARKER in _text_of(it) for it in items)


def _insert_index(items: list[Any]) -> int:
    """记忆块插在哪：**最后一条 user 消息之前**。

    为什么不用"追加到末尾"：多步工具循环里末尾是 assistant(tool_call) / tool(result)，
    追加会把记忆塞进工具调用链中间，破坏"调用与结果相邻"这条结构。

    找不到 user 消息（理论上不会发生）⇒ 追加到末尾，至少不丢。
    """
    for idx in range(len(items) - 1, -1, -1):
        item = items[idx]
        role = item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
        if role == "user":
            return idx
    return len(items)


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
                    batch_id: str = "", raw_content: str = "") -> dict:
    """写一条记忆（追加式）。**同步** —— 工具函数直接调。

    ``source`` / ``batch_id``：写入来源与批次。模型主动调 ``remember`` 时是
    ``memory``；压缩时批量抽候选是 ``compaction`` + 同一个批次号，出问题能整批撤回
    （见 ``memory_store.forget_batch``）。

    ``raw_content``：提炼前的原话。它**不注入**，只在用户要追溯"这条原来是哪段话"
    时看 —— 注入的永远是提炼后的短句。

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
            source=source, batch_id=batch_id, raw_content=raw_content,
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
        _relieve(owner_key)
    return result


def _relieve(owner_key: str) -> dict:
    """写入后顺带跑一次三段卸压。**失败吞掉**。

    为什么挂在写入后面而不是定时任务：这个项目没有调度器，而"等有调度器再清理"
    的意思是永远不清理。写记忆是低频动作（几百轮才一条），没超上限时只发一次
    计数查询就返回。
    """
    return memory_lifecycle.relieve(owner_key, store=_store())


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
