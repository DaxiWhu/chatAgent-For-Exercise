"""长期记忆的**三段卸压**：回收原话 → 压缩长条目 → 按分数剔除。

为什么要有这个模块：记忆表只增不减的话，读取侧有上限、写入侧没有 —— 多出来的
那些**永远读不到、也不会消失**。那不是"没记"，是静默遗忘，比删除更难发现。

三段的目的不同，顺序不能换：

1. **回收已提炼的原话**（``raw_content``）。零信息损失 —— 正文还在，原话只是
   "提炼前长什么样"的备份。先做它是因为不要钱。
2. **逐条压缩最长的正文**。让注入体积真的变小。它排第二是因为它要花一次模型
   调用，而且压出来的是"短一点但仍然是全句"，仍有信息损失（细节被概括）。
3. **按分数物理删除**。不可逆。走到这一步说明前两段都不够了。

容量口径（用户 2026-11-03 口径）：
* **条数 = 所有行**，含改口留下的旧版本、含被"忘掉"的行 —— 它们占着存储。
* **总字数 = 正文 + 原话**，两个都算，因为两者都占存储与备份。

两处容易写成"看起来在跑、其实没做"的地方，都做了守卫：

* 压缩**不产生新原话**。第 1 段刚把所有原话清空，第 2 段若又把原文塞回
  ``raw_content``，总字数等于没降 —— 于是每一轮都"压缩了、还是超、再删"，
  用户看到的是记忆越压越少。容量压力下原话是可以丢的（用户把回收排在第一位，
  就是这个意思）；常规写入时的提炼照样留原话，那是另一条路径。
* 压缩**一次都没成功就绝不进入第 3 段**。压不动通常意味着整条优化链路不可用
  （没配 Key、网关 502），此时按分数删数据是不可逆的伤害。宁可超限，等下一条
  记忆写入时再试。

调用点只有一个：``memory.remember_memory`` 写完新行之后。这个项目没有调度器，
"等有调度器再清理"等于永远不清理。写记忆是低频动作（几百轮才一条），
没超上限时它只发一次计数查询就返回。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from .. import observability
from . import memory_digest, memory_store

__all__ = ["digest_per_round", "over_capacity", "relieve"]

#: 单轮最多压几条。超出的等下一次写入继续压。
#: 为什么要上限：每压一条都是一次同步模型调用，一次写入等十几秒是会被人投诉的。
_DEFAULT_PER_ROUND = 5


def digest_per_round() -> int:
    """单轮压缩条数上限（``AGENT_MEMORY_DIGEST_PER_ROUND``，默认 5，配 0 表示不压）。"""
    raw = (os.getenv("AGENT_MEMORY_DIGEST_PER_ROUND", "") or "").strip()
    try:
        return max(0, int(raw)) if raw else _DEFAULT_PER_ROUND
    except ValueError:
        return _DEFAULT_PER_ROUND


def over_capacity(measured: dict) -> bool:
    """条数或总字数任一超限就算超。两个都要管：条数管不住"两百条巨兽"，
    字数管不住"几百条碎片"，任一单独看都会漏。"""
    return (int(measured.get("rows") or 0) > memory_store.max_live_per_owner()
            or int(measured.get("chars") or 0) > memory_store.max_chars_per_owner())


def relieve(owner_key: str, *, store: Any, digest_fn: Callable | None = None) -> dict:
    """跑一次卸压，返回 ``{"reclaimed": n, "digested": n, "deleted": n}``。

    ``digest_fn`` 只为测试注入；生产用 ``memory_digest.digest``。
    任何一步抛错都不外溢 —— 卸压失败不该让"写记忆"这件事失败。
    """
    out = {"reclaimed": 0, "digested": 0, "deleted": 0}
    if not owner_key:
        return out
    try:
        measured = store.measure(owner_key)
        if not over_capacity(measured):
            return out

        # ---- 第 1 段：回收原话（零信息损失） ----
        out["reclaimed"] = int(store.reclaim_raw(owner_key) or 0)
        if out["reclaimed"]:
            observability.counter("agent_memory_reclaimed_total", amount=out["reclaimed"])
        measured = store.measure(owner_key)
        if not over_capacity(measured):
            return out

        # ---- 第 2 段：逐条压缩最长的那条 ----
        compressed_ok = False
        for _ in range(digest_per_round()):
            row = store.pick_longest(owner_key, min_chars=memory_digest.digest_above())
            if row is None:
                break            # 没有"长到值得压"的条目 ⇒ 压缩不适用，正常进第 3 段
            short = (digest_fn or memory_digest.digest)(
                row.get("content") or "", row.get("kind") or "project")
            if not short or short == row.get("content"):
                break            # 压不动（模型没回 / 回了原文）：别转圈，但**不据此删数据**
            # ⚠️ raw_content 传空：见模块 docstring 第二条。容量压力下原话可丢。
            store.replace_content(owner_key, str(row.get("id") or ""), short)
            compressed_ok = True
            out["digested"] += 1
            measured = store.measure(owner_key)
            if not over_capacity(measured):
                return out

        # ---- 第 3 段：按分数物理删除 ----
        if not compressed_ok and store.pick_longest(
                owner_key, min_chars=memory_digest.digest_above()) is not None:
            # 有该压的、却一条都没压成 ⇒ 优化链路不可用。此时删数据是不可逆的伤害。
            observability.counter("agent_memory_errors_total", stage="digest")
            logging.warning("记忆卸压：压缩未成功，暂不删除（用户=%s）", owner_key)
            return out
        out["deleted"] = int(store.prune(owner_key) or 0)
        if out["deleted"]:
            observability.counter("agent_memory_pruned_total", amount=out["deleted"])
            logging.info("记忆容量卸压：删除 %d 条（用户=%s）", out["deleted"], owner_key)
    except Exception as e:  # noqa: BLE001  卸压失败不该影响写入本身
        observability.counter("agent_memory_errors_total", stage="relieve",
                              kind=type(e).__name__)
        logging.warning("记忆卸压失败（不影响写入）：%s", e)
    return out
