"""知识库检索器的进程内池：按租户分片、缓存有界、语料只解析一次。

为什么单独一个模块
------------------
这段逻辑此前在 ``src/graph.py`` 与 ``src/rag/answer.py`` 里**各有一份**，两份的
淘汰策略还不一致（一个 FIFO、一个 LRU），上限默认值也不同（8 / 32）。同一批语料
被解析两遍、同一件事调不同租户拿到不同结果，都只会在"某个租户的检索结果忽然变了"
这种无从复现的症状里出现。现在只留这一份，其余调用点全部指向这里。

谁在用
------
* ``src/agents/context.py`` 的 ``retriever_factory`` —— agent 链路里 ``kb_search``
  工具通过它取检索器（生产唯一路径）；
* ``scripts/agent_capability_probe.py``、``scripts/verify_pgvector.py`` 等脚本。

为什么要按租户分片
------------------
``PgVectorStore`` 的租户是**构造期**绑定的（``self.tenant``），而检索器又是单例 ——
合起来是"第一个请求用哪个租户，之后所有请求都跟着用那个租户"。只读环境变量时
看不出来；一旦请求期租户接进来，不分片就等于"谁先来谁定调"，隔离形同虚设。

为什么缓存必须有界
------------------
租户字符串来自请求。走令牌时它是已验签的 claim，但脚本通行证下是调用方自报 ——
后者不受任何约束。所以这里必须自己设上限，不能靠"调用方永远守规矩"：超限时
丢弃一个旧分片而不是无限增长，因为内存涨到 OOM 是比"偶尔重建检索器"严重得多的
故障。丢弃后下次访问会重建（语料已在 ``_CHUNKS`` 里，代价可控）。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..envutil import env_int
from . import retrieval as _retrieval
from .parse_cache import load_chunks_cached
from .retrieval import build_retriever, load_chunks, tenant_label

__all__ = [
    "DOCS_DIR",
    "get_retriever",
    "load_chunks_once",
    "reset_retrievers",
    "set_retriever",
    "tenant_cache_max",
]

#: 语料目录（可用 ``DOCS_DIR`` 覆盖）。容器部署时必须挂进镜像 —— 镜像刻意不打 data/。
DOCS_DIR = os.getenv("DOCS_DIR", "data")

#: 注入点：测试与 ``build_graph(retriever=...)`` 直接覆盖它，此时租户分片不生效
#: （注入的检索器是调用方的责任）。生产链路上保持 ``None``，走下面的缓存逻辑。
_RETRIEVER: Any = None

#: 语料只解析一次：多个租户共用同一份 chunks，只是"入库到各自租户"。
_CHUNKS: list[Any] | None = None

#: 按租户缓存的检索器。**必须有界** —— 理由见模块 docstring。
_TENANT_RETRIEVERS: dict[str, Any] = {}

#: 哪些缓存键是"在 embedding 不可用时建出来的"（见 `_build_tracked`）。
#: 只在缓存命中路径上读一次，用来决定要不要趁冷却过期时重建。
_DEGRADED: set[str] = set()

#: 上限取小值：需要隔离的是**少数租户**，而不是"任意租户字符串"。
_TENANT_CACHE_MAX = env_int("RAG_TENANT_CACHE_MAX", 8, lo=1)


def tenant_cache_max() -> int:
    """当前上限（读模块级，便于测试改完立刻生效）。"""
    return _TENANT_CACHE_MAX


def load_chunks_once() -> list[Any]:
    """加载并缓存语料块。进程内只解析一次，供所有租户分片复用。

    优先走文档解析落盘缓存（``parse_cache``）：语料没变时直接反序列化上次的
    chunks，跳过"重新解析 + 重新切分"这整段（docling/OCR 是分钟级开销）。
    缓存层任何异常都退回直接解析 —— 缓存只是加速，不能成为失败点。
    """
    global _CHUNKS
    if _CHUNKS is None:
        try:
            _CHUNKS = load_chunks_cached(DOCS_DIR)
        except Exception as e:  # noqa: BLE001
            logging.warning("解析缓存不可用，回退直接解析语料：%s", e)
            _CHUNKS = load_chunks(DOCS_DIR)
        logging.info("RAG 语料加载完成：%d 个片段（DOCS_DIR=%s）", len(_CHUNKS), DOCS_DIR)
        if not _CHUNKS:
            # 语料为空是**静默失败**：服务照常起、探针照常绿、SSE 照常连，
            # 只是检索恒无结果、回答里没有任何引用 —— 没有这条告警就只能靠
            # 用户反馈"答得不对"才发现。容器部署最常见的原因是**语料目录
            # 没挂进容器**（镜像刻意不打 data/），其次是目录里没有受支持的文档。
            logging.warning(
                "RAG 语料为空（DOCS_DIR=%s）：检索将恒无结果、回答不会有任何引用。"
                "容器部署优先检查语料目录是否挂载、路径是否与 DOCS_DIR 一致。",
                DOCS_DIR,
            )
    return _CHUNKS


def _build_tracked(key: str, chunks, tenant: str | None):
    """构建检索器，并记下"这一次是在降级状态下建出来的"。

    key: 缓存键（租户串，无租户时是空串）。
    chunks: 已装配的块列表。
    tenant: 传给 `build_retriever` 的租户（None = 进程级默认）。

    为什么要记：**缓存会把降级状态一起钉住**。embedding 欠费时建出来的
    `HybridRetriever` 里，稠密那一路是 `NullRetriever` —— 它被缓存之后，
    即使冷却到期、上游恢复正常，这个进程也**永远**只用关键词检索，
    直到有人重启或调 `reset_retrievers`（生产代码里没有任何地方调它）。
    症状是"充值了、日志里也没报错了，但检索质量没回来"，且没有任何报错。
    """
    r = build_retriever(chunks, tenant=tenant)
    if _retrieval.embedding_down():
        _DEGRADED.add(key)
    else:
        _DEGRADED.discard(key)
    return r


def _needs_revive(key: str) -> bool:
    """缓存的这一份是不是"上次构建时稠密路不可用"，且冷却已经过期。

    冷却未过期时**不重建** —— 那等于把"每次请求都白撞一次上游"搬回来
    （`RAG_EMBED_RETRY_COOLDOWN` 存在的理由）。所以重建频率上限就是冷却窗口：
    一分钟最多试一次，成功就永久恢复，失败就再等一分钟。
    """
    return key in _DEGRADED and not _retrieval.embedding_down()


def get_retriever(tenant: str | None = None):
    """取检索器，按租户分片缓存（懒加载）。

    tenant: 请求期租户。``None`` = 沿用进程级 ``RAG_TENANT``，语义与改造前一致。
        非 ``None`` 时会为该租户单独缓存一个检索器（仅 pg 后端真正按租户过滤）。

    注入优先：``_RETRIEVER`` 非空时直接返回它，并**忽略 tenant** ——
    注入意味着"调用方自己负责隔离"。
    """
    if _RETRIEVER is not None:
        return _RETRIEVER

    chunks = load_chunks_once()

    if tenant is None:
        # 无租户语境（CLI/脚本/原有调用点）：用进程级默认值，且不占分片缓存，
        # 保持"整个进程只构建一次"的原有语义。
        if "" not in _TENANT_RETRIEVERS or _needs_revive(""):
            _TENANT_RETRIEVERS[""] = _build_tracked("", chunks, None)
            logging.info("RAG 检索器构建完成：%d 个片段（DOCS_DIR=%s，租户=%s）",
                         len(chunks), DOCS_DIR, tenant_label(None))
        return _TENANT_RETRIEVERS[""]

    key = tenant.strip()
    hit = _TENANT_RETRIEVERS.get(key)
    if hit is not None and not _needs_revive(key):
        return hit

    if hit is None and len(_TENANT_RETRIEVERS) >= _TENANT_CACHE_MAX:
        # 丢一个最老的（dict 保序，取第一个插入的）。不用 LRU 是有意的：
        # 这里的访问分布是"少数租户长期活跃"，FIFO 足够，而 LRU 要额外维护
        # 访问顺序（并发下还要加锁），复杂度换不来实际收益。
        evicted = next(iter(_TENANT_RETRIEVERS))
        _TENANT_RETRIEVERS.pop(evicted, None)
        logging.warning("检索器租户分片缓存已满（上限 %d），丢弃租户 %s 的分片",
                        _TENANT_CACHE_MAX, evicted or "<默认>")

    r = _build_tracked(key, chunks, key)
    _TENANT_RETRIEVERS[key] = r
    logging.info("RAG 检索器构建完成：%d 个片段（租户=%s）",
                 len(chunks), tenant_label(key))
    return r


def set_retriever(retriever: Any) -> None:
    """注入一个检索器（测试 / 已有实例）。传 ``None`` 表示恢复按租户构建。"""
    global _RETRIEVER
    _RETRIEVER = retriever


def reset_retrievers() -> None:
    """清空语料与全部检索器缓存（**测试用**）。

    为什么测试需要它：语料缓存是模块级全局，一个用例注入过检索器之后，后续用例
    会在不知情的情况下复用它 —— 表现是"断言偶发失败"，而不是报错。
    """
    global _RETRIEVER, _CHUNKS
    _RETRIEVER = None
    _CHUNKS = None
    _TENANT_RETRIEVERS.clear()
    _DEGRADED.clear()
