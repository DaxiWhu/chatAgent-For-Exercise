"""任务执行中补充的内容 —— 进程内暂存，供**下一次模型调用**注入。

它解决什么
----------
一次 ``/v1/chat`` 里模型可能被调用很多次（一个节点内多轮、一个请求跨多个 agent
节点）。用户在这期间又说了一句话，这句话要"接上"，而不是"另起一轮"。

为什么不靠会话历史解决
--------------------
会话历史是在**每次 run 开始时**被读一次的快照（见 ``runner._build_input_items``
与 ``probe_steering_nextnode.py`` 的实测）。补进去的内容对**下一次 run** 生效，
对**当前这次 run** 无效 —— 而用户最想要的恰恰是当前这次。

为什么不落库
-----------
它不需要持久：真正要持久的那一份**同时被写进了会话历史**（见
``app/main.py`` 的 ``POST /v1/sessions/{id}/messages``）。本模块只是一份
"当前进程里、当前这次运行还没看见过"的增量索引，进程重启丢掉的只是一次加速，
不会丢内容。

⚠️ 由此带来一条必须知情的边界：**多副本下它只对本进程有效**。
补充请求落到 A 副本、任务跑在 B 副本时，B 看不到这份增量 —— 那条补充仍然会
在**下一个 agent 节点**生效（因为节点会重新读会话历史），只是当次节点内不生效。
这不是缺陷，是"不做跨进程广播"这个选择的代价；要跨副本就得加一层 pub/sub，
而在单副本部署里那是纯开销。

判据为什么是自增序号而不是时间
----------------------------
时间要比"运行开始时刻"与"写入时刻"，两个时钟来源（应用进程 / DB）之间必然有偏差，
而偏差的症状是"偶尔漏一条补充"或"重复注入一条"。自增序号单调、无需对时：
run 开始时记下当前水位，之后只取**大于水位**的那些。
"""

from __future__ import annotations

import threading
import time

__all__ = [
    "MAX_PER_THREAD",
    "PREFIX",
    "clear",
    "cursor",
    "note",
    "pending_count",
    "reset",
    "since",
]

#: 注入文本的前缀。**两处共用**（写进会话历史的那份、注入模型输入的那份）——
#: 各写一份必然漂，而漂的后果是"模型对同一条补充看到两种说法"。
#:
#: 为什么必须带前缀：不带的话，模型会把这条补充当成**新一轮的用户请求**，
#: 于是可能抛开手头正在做的事、直接回答这句话。这是"补充"与"新指令"的区别，
#: 只能靠文案说清 —— 没有别的机制能表达它。
PREFIX = "【任务执行中补充】"

#: 单个会话最多暂存多少条。有界是硬要求：这是个按 thread_id 增长的进程内字典，
#: 无界意味着"反复往一个会话里发补充"就能吃内存。
MAX_PER_THREAD = 20

#: 暂存多久（秒）。过期即丢 —— 内容已经在会话历史里了，这里只是"当次运行"的加速。
TTL_SECONDS = 1800.0

_lock = threading.Lock()
#: 全局单调序号。**不按会话分段**：跨会话比较没有意义，但全局单调最省事，
#: 也不会因为某个会话被清掉而让别的水位失效。
_seq = 0
#: thread_id -> [(seq, 写入时刻, 正文), ...]（按 seq 升序）
_pending: dict[str, list[tuple[int, float, str]]] = {}


def _prune(thread_id: str, now: float) -> None:
    """清掉某个会话里过期与超量的条目（惰性，调用方已持锁）。"""
    rows = _pending.get(thread_id)
    if not rows:
        _pending.pop(thread_id, None)
        return
    rows = [r for r in rows if now - r[1] <= TTL_SECONDS]
    if len(rows) > MAX_PER_THREAD:
        rows = rows[-MAX_PER_THREAD:]
    if rows:
        _pending[thread_id] = rows
    else:
        _pending.pop(thread_id, None)


def note(thread_id: str, text: str) -> int:
    """登记一条补充，返回它的序号（调用方可以拿来回显）。

    thread_id: 会话编号（与本项目的会话历史同一个 id）。空串 ⇒ 只返回 0，不登记
        （没有会话就没有"当次运行"可言）。
    text: 正文。空串不登记（否则模型会收到一条空的用户消息）。

    为什么返回序号而不是布尔：接口层要告诉客户端"这条补充排在第几位"，
    多副本/多客户端同时补充时，序号是唯一能说清"谁先谁后"的东西。
    """
    global _seq
    sid = (thread_id or "").strip()
    body = (text or "").strip()
    if not sid or not body:
        return 0
    now = time.monotonic()
    with _lock:
        _seq += 1
        seq = _seq
        _prune(sid, now)
        _pending.setdefault(sid, []).append((seq, now, body))
        _prune(sid, now)
    return seq


def cursor(thread_id: str) -> int:
    """当前水位（= 已登记的最大序号）。run 开始时记一次，之后只取变化的部分。

    没有登记过任何补充时返回 0 —— 于是 ``since(thread_id, 0)`` 会返回全部，
    这正是"水位数不出比 0 更小的序号"这个事实的体现。
    """
    sid = (thread_id or "").strip()
    if not sid:
        return 0
    now = time.monotonic()
    with _lock:
        _prune(sid, now)
        rows = _pending.get(sid) or []
        return rows[-1][0] if rows else 0


def since(thread_id: str, cursor_seq: int) -> list[str]:
    """取水位之后登记的正文（按登记顺序）。

    ⚠️ 它**不清空**：同一批补充会在本次运行的每一次模型调用里各注入一次。
    这是刻意的 —— 注入进模型输入的那份是"本次调用专用"的（实测：下一次调用就
    看不到上一次拼进去的东西），不清空才能让模型在后续每一轮都还知道这条补充。
    重复的判据由调用方按内容去重（见 ``runner._inject_supplements``）。
    """
    sid = (thread_id or "").strip()
    if not sid:
        return []
    now = time.monotonic()
    with _lock:
        _prune(sid, now)
        rows = _pending.get(sid) or []
        return [text for seq, _at, text in rows if seq > cursor_seq]


def pending_count(thread_id: str) -> int:
    """某个会话当前暂存了几条（运维/接口回显用，不参与业务判定）。"""
    sid = (thread_id or "").strip()
    if not sid:
        return 0
    now = time.monotonic()
    with _lock:
        _prune(sid, now)
        return len(_pending.get(sid) or [])


def clear(thread_id: str) -> None:
    """丢掉某个会话的暂存（会话被删除时调用）。"""
    sid = (thread_id or "").strip()
    if not sid:
        return
    with _lock:
        _pending.pop(sid, None)


def reset() -> None:
    """清空全部状态（**测试用**；生产只有关闭路径会走到）。"""
    global _seq
    with _lock:
        _pending.clear()
        _seq = 0
