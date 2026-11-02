"""记忆的向量索引：复用 RAG 那套 pgvector 设施，按用户隔离。

为什么复用而不是另起一套
------------------------
项目里已经有一份能用的 pgvector 封装（``PgVectorStore``：建表、hnsw 索引、
upsert、余弦检索）和一份 embedding 客户端（``_new_embedder``）。
另写一份的代价不是"多几百行"，而是**两个地方会用不同的 query 前缀、不同的
模型、不同的归一化**，于是记忆检索和知识库检索的分数不在一个量纲上 ——
而这种不一致没有任何报错，只会让召回莫名其妙地差。

隔离口径：``tenant`` 列装 ``owner_key``
--------------------------------------
``PgVectorStore`` 的隔离是"某个字符串列做行级过滤"，那是它唯一的隔离机制。
本表里那个列装的是**用户 id**，不是租户 —— 与记忆主表一致：**只按用户隔离，
绝不叠租户**（用户没有租户是合法状态，叠上去会让这类用户召回 0 条且零报错）。

失败一律退回全量
----------------
这个模块的每个入口都不抛异常给调用方：检索是"帮你在很多条里挑几条"，
挑不出来就退回"全都给"。前者是优化，后者是功能 —— 优化失败不能变成功能失败。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

__all__ = [
    "DEFAULT_TABLE",
    "MemoryIndex",
    "index_table",
    "make_index",
    "reset_index",
]

#: 记忆向量表。与知识库的 ``rag_chunks`` 分开：两者的 id 语义、生命周期、
#: 过滤维度都不同，混在一张表里迟早会有人用错过滤条件（然后静默召回别人的东西）。
DEFAULT_TABLE = "agent_memories_vec"

#: 已经确认过表结构的 (dsn, table, dim)。只是省掉每次写入都跑一遍 DDL ——
#: 重启后重新跑一次是幂等的，多副本各自跑一次也无害。
_ENSURED: set[tuple[str, str, int]] = set()
_ENSURED_LOCK = threading.Lock()

_INDEX: Any = None
_INDEX_LOCK = threading.Lock()


def index_table() -> str:
    """向量表名（``AGENT_MEMORY_VEC_TABLE``）。测试可以指到一次性表上。"""
    return (os.getenv("AGENT_MEMORY_VEC_TABLE", "") or "").strip() or DEFAULT_TABLE


def _dsn() -> str:
    return (os.getenv("DB_URL", "") or "").strip()


class MemoryIndex:
    """记忆的向量读写。构造**不连库、不调 API**（第一次真正用到才动）。"""

    def __init__(self, dsn: str, table: str = "", *, store_factory=None,
                 embedder=None) -> None:
        self.dsn = (dsn or "").strip()
        self.table = (table or "").strip() or DEFAULT_TABLE
        self._store_factory = store_factory
        self._embedder = embedder

    # ---------- 依赖（全部延迟构造） ----------
    @property
    def embedder(self):
        if self._embedder is None:
            from ..rag.retrieval import _new_embedder

            self._embedder = _new_embedder()
        return self._embedder

    def _store(self, owner_key: str):
        """按 owner 造一个 store：隔离靠 ``tenant`` 参数，所以实例不能复用。"""
        if self._store_factory is not None:
            return self._store_factory(owner_key)
        return self._default_store(owner_key)

    def _ensure(self, dim: int) -> None:
        key = (self.dsn, self.table, int(dim))
        if key in _ENSURED:
            return
        with _ENSURED_LOCK:
            if key in _ENSURED:
                return
            # 建表与"哪个人"无关，所以 owner 传空串（不是 None —— None 在
            # PgVectorStore 里等于"不过滤"，那是另一个语义）。
            store = (self._store_factory("") if self._store_factory is not None
                     else self._default_store(""))
            store.ensure_schema(int(dim))
            _ENSURED.add(key)

    def _default_store(self, owner_key: str):
        from ..rag.vector_store import PgVectorStore

        return PgVectorStore(self.dsn, self.table, tenant=owner_key)

    # ---------- 写 ----------
    def put(self, owner_key: str, items: list[dict]) -> int:
        """把记忆写进向量表，返回成功条数。**失败返回 0，不抛**。

        items: ``[{"id":..., "content":..., "kind":...}, ...]``。``id`` 是记忆主表
        的主键 —— 检索回来靠它回主表取原文，两边一旦对不上，检索就退化成噪声。
        """
        rows = [i for i in items
                if str(i.get("id") or "").strip() and str(i.get("content") or "").strip()]
        if not self.dsn or not rows:
            return 0
        try:
            vectors = self.embedder.embed_documents([str(i["content"]) for i in rows])
        except Exception as e:  # noqa: BLE001
            self._note("embed", e)
            return 0
        if not vectors or len(vectors) != len(rows):
            self._note("embed_empty", None)
            return 0
        try:
            self._ensure(len(vectors[0]))
            store = self._store(owner_key)
            payload = [
                (str(item["id"]),
                 {"id": str(item["id"]), "kind": str(item.get("kind") or ""),
                  "content": str(item["content"]), "source": "memory"},
                 str(item["content"]),
                 vec)
                for item, vec in zip(rows, vectors, strict=False)
            ]
            return int(store.upsert(payload))
        except Exception as e:  # noqa: BLE001
            self._note("write", e)
            return 0

    def drop_owner(self, owner_key: str) -> int:
        """物理删掉该用户的向量（只给删号用，与主表的 ``forget_owner`` 配对）。"""
        if not self.dsn or not owner_key:
            return 0
        try:
            store = self._store(owner_key)
            return int(store.delete_tenant() or 0)
        except Exception as e:  # noqa: BLE001
            self._note("drop", e)
            return 0

    # ---------- 读 ----------
    def search(self, owner_key: str, query: str, top_k: int = 20) -> list[tuple[str, float]]:
        """返回 ``[(记忆 id, 相似度), ...]``，按相似度降序。**失败返回空**。

        ``top_k`` 是**候选**条数，不是最终注入条数：调用方还要拿这些 id 回主表
        过滤掉已失效 / 已淘汰的，所以这里多取一点（调用方传 top_k × 2）。
        """
        text = str(query or "").strip()
        if not self.dsn or not owner_key or not text:
            return []
        # 冷却期内不试：embedding 网关整体挂了时，每轮都顶着超时重试一次
        # 会把整轮请求拖慢，而这一条记忆本来也不是必需的。
        try:
            from ..rag.retrieval import EMBEDDING_QUERY_PREFIX, apply_query_prefix, embedding_down

            if embedding_down():
                return []
            prefixed = apply_query_prefix(EMBEDDING_QUERY_PREFIX, text)
        except Exception as e:  # noqa: BLE001
            self._note("import", e)
            return []
        try:
            vector = self.embedder.embed_query(prefixed)
            hits = self._store(owner_key).search(vector, top_k=max(1, int(top_k)))
        except Exception as e:  # noqa: BLE001
            self._note("search", e)
            return []
        out: list[tuple[str, float]] = []
        for chunk, score in hits:
            ident = str((chunk or {}).get("id") or "").strip()
            if ident:
                out.append((ident, float(score)))
        return out

    # ---------- 观测 ----------
    @staticmethod
    def _note(stage: str, exc: BaseException | None) -> None:
        try:
            from .. import observability

            observability.counter("agent_memory_index_errors_total", stage=stage)
        except Exception:  # noqa: BLE001
            pass
        if exc is not None:
            logging.warning("记忆向量索引失败（已退化为全量注入，stage=%s）：%s", stage, exc)


def make_index(dsn: str = "", table: str = "") -> MemoryIndex:
    """进程单例。DSN 必须与 ``memory_store`` 同源，否则会索引到两张不同的表上。"""
    global _INDEX
    with _INDEX_LOCK:
        if _INDEX is None:
            _INDEX = MemoryIndex(dsn or _dsn(), table or index_table())
        return _INDEX


def reset_index() -> None:
    """测试用：丢掉单例（换了 DSN / 表名之后必须调，否则仍在写旧表）。"""
    global _INDEX
    with _INDEX_LOCK:
        _INDEX = None
