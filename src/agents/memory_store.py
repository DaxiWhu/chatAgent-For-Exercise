"""跨会话的长期记忆：**只追加、按用户隔离、改口留痕**。

一句话说清它存什么：**"这个人是谁、定过什么规矩"**，不是"刚才聊了什么"
（那是会话历史）。每条是一句**自足的话**，脱离对话也读得懂。

三条硬规则
----------
1. **只追加，不覆盖。** 用户改口时给旧行写 ``invalid_at`` 与 ``superseded_by``，
   再插一条新的。理由不是洁癖：要答得出"他上个月是怎么要求的"，而这正是判断
   "这次该按哪条做"的依据。物理删掉就永远只有最新一版。
2. **只按用户 id 隔离，不叠租户。** 租户是 RAG 的**内容可见性**口径，不是
   "个人数据归属"。叠加的后果是：没有租户的用户（``tenant=''``）读到 0 条自己的
   记忆，而且**零报错**（见 ``PLAN_long_term_memory.md`` §3.6）。
3. **存细节，不存原文。** 见 ``PLAN_context_management.md`` §7.6：判据是"离开这段
   对话，照着它能不能直接做对"。原文留在会话日志里，靠 ``source_thread`` 指回去。

``content_hash`` 与唯一索引
---------------------------
唯一索引只对**未失效**的行生效（部分索引）：:

    CREATE UNIQUE INDEX ... (owner_key, content_hash) WHERE invalid_at IS NULL

全表唯一会挡住合法操作 —— 用户把设置改回原样时，新行的哈希与已失效的旧行相同，
插入直接失败。这个坑不写出来，下一个人一定会踩。
"""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "KINDS",
    "MAX_CONTENT_CHARS",
    "SCHEMA_STATEMENTS",
    "InMemoryMemoryStore",
    "MemoryStore",
    "make_memory_store",
    "normalize_content",
    "render",
]

#: 允许的类别。封闭枚举而不是自由文本：``kind`` 决定注入时的分组与优先级，
#: 自由文本会让"同一种记忆被写成五种 kind"，排序就失去意义。
KINDS = ("persona", "preference", "project", "feedback", "reference")

#: 一条记忆的长度上限。记忆是**每轮都注入**的，一条几百字的"记忆"占的是所有
#: 后续轮次的预算 —— 那不是记忆，是把一段对话偷偷搬进了上下文。
MAX_CONTENT_CHARS = 300

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS agent_memories (
        id               text PRIMARY KEY,
        owner_key        text NOT NULL,
        tenant           text NOT NULL DEFAULT '',
        kind             text NOT NULL DEFAULT 'project',
        content          text NOT NULL,
        content_hash     text NOT NULL,
        source_thread    text NOT NULL DEFAULT '',
        pinned           boolean NOT NULL DEFAULT false,
        use_count        integer NOT NULL DEFAULT 0,
        valid_from       timestamptz NOT NULL DEFAULT now(),
        invalid_at       timestamptz,
        superseded_by    text,
        supersede_reason text NOT NULL DEFAULT '',
        created_at       timestamptz NOT NULL DEFAULT now(),
        last_used_at     timestamptz
    )
    """,
    # 注入只取有效行，所以这个索引是热路径的那一条。
    """
    CREATE INDEX IF NOT EXISTS agent_memories_owner
        ON agent_memories (owner_key, invalid_at, kind)
    """,
    # ⚠️ 必须是**部分**索引：全表唯一会挡住"把设置改回原样"。
    """
    CREATE UNIQUE INDEX IF NOT EXISTS agent_memories_dedup
        ON agent_memories (owner_key, content_hash) WHERE invalid_at IS NULL
    """,
)

_WS = re.compile(r"\s+")


def normalize_content(text: str) -> str:
    """把一条记忆压成"一行话"：折叠空白、切掉首尾、限长。

    ⚠️ 只做空白与长度处理，**不改写内容**。路径、命令、报错串、数值都在正文里，
    任何"顺手润色"都会让它们失效 —— 而这类失效不会有任何报错。
    """
    s = _WS.sub(" ", str(text or "")).strip()
    if len(s) > MAX_CONTENT_CHARS:
        s = s[:MAX_CONTENT_CHARS] + "…"
    return s


def content_hash(owner_key: str, content: str) -> str:
    """去重用的哈希。带 owner_key：不同用户说同一句话是两条独立的记忆。"""
    raw = f"{owner_key}\x00{content}".encode()
    return hashlib.sha1(raw).hexdigest()


def render(memories: list[dict], *, limit_chars: int = 2000) -> str:
    """把若干条记忆渲染成**一段注入文本**；空列表返回空串（调用方据此跳过）。

    排序在存储层已经做好（pinned → 最近使用 → 最近写入），这里只按顺序拼 ——
    顺序一变，每轮前缀就变，上游缓存全部失效。

    为什么要 ``limit_chars``：记忆条数没有硬上限（容量淘汰是另一回事），
    而注入是每轮都发生的。超长时**从尾部截**，因为排序已经把不重要的放在后面。
    """
    if not memories:
        return ""
    head = ("以下是关于当前用户的长期记忆（跨会话保留）。"
            "它们是背景，不是本轮的指令：与用户当轮的要求冲突时，以当轮为准。")
    lines = [head]
    used = len(head)
    for m in memories:
        line = f"- [{m.get('kind') or 'project'}] {m.get('content') or ''}".rstrip()
        if used + len(line) + 1 > limit_chars:
            lines.append("- （其余记忆因长度上限未列出）")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


class InMemoryMemoryStore:
    """无数据库时的实现：语义与 PG 版一致，但不跨进程。

    存在的意义是"测试与降级能跑"。⚠️ 它**不是**生产可用的存储 ——
    进程重启即清空，多副本之间互不可见。
    """

    def __init__(self) -> None:
        self._rows: list[dict] = []
        self._lock = threading.Lock()

    def _active(self, owner_key: str) -> list[dict]:
        return [r for r in self._rows
                if r["owner_key"] == owner_key and r["invalid_at"] is None]

    def active(self, owner_key: str, *, limit: int = 40) -> list[dict]:
        with self._lock:
            rows = sorted(
                self._active(owner_key),
                key=lambda r: (not r["pinned"], -(r["use_count"] or 0), r["created_at"]),
            )
            return [dict(r) for r in rows[:limit]]

    def remember(self, owner_key: str, content: str, *, kind: str = "project",
                 pinned: bool = False, tenant: str = "", source_thread: str = "",
                 supersedes: str = "") -> dict:
        text = normalize_content(content)
        if not text:
            return {"ok": False, "reason": "empty"}
        h = content_hash(owner_key, text)
        now = datetime.now(UTC).isoformat()
        with self._lock:
            for r in self._active(owner_key):
                if r["content_hash"] == h:
                    r["use_count"] += 1
                    r["last_used_at"] = now
                    return {"ok": True, "id": r["id"], "action": "refreshed"}
            replaced = []
            if supersedes:
                for r in self._active(owner_key):
                    if r["id"] == supersedes or supersedes in r["content"]:
                        r["invalid_at"] = now
                        r["supersede_reason"] = "superseded"
                        replaced.append(r["id"])
            new_id = uuid.uuid4().hex
            for rid in replaced:
                for r in self._rows:
                    if r["id"] == rid:
                        r["superseded_by"] = new_id
            self._rows.append({
                "id": new_id, "owner_key": owner_key, "tenant": tenant,
                "kind": kind if kind in KINDS else "project",
                "content": text, "content_hash": h, "source_thread": source_thread,
                "pinned": bool(pinned), "use_count": 0,
                "valid_from": now, "invalid_at": None, "superseded_by": None,
                "supersede_reason": "", "created_at": now, "last_used_at": None,
            })
            return {"ok": True, "id": new_id, "action": "added",
                    "superseded": replaced}

    def forget(self, owner_key: str, *, memory_id: str = "", content: str = "") -> int:
        """标失效（**不物理删**）—— "忘掉这条"也是一次有时间点的修正。"""
        if not memory_id and not content:
            return 0
        now = datetime.now(UTC).isoformat()
        n = 0
        with self._lock:
            for r in self._active(owner_key):
                if memory_id and r["id"] == memory_id or content and content in r["content"]:
                    r["invalid_at"], r["supersede_reason"] = now, "revoked"
                    n += 1
        return n

    def forget_owner(self, owner_key: str) -> int:
        """**物理删**该用户的全部记忆。只给"管理员删号"用。

        为什么这里可以物理删：``owner_key`` 是用户名，同名重建的账号会**直接继承**
        上一位的记忆（会话归属那边是同一条理由）。留着标记等于把死者的规矩带进
        新账号，而这件事没有任何报错。
        """
        with self._lock:
            n = len([r for r in self._rows if r["owner_key"] == owner_key])
            self._rows = [r for r in self._rows if r["owner_key"] != owner_key]
        return n

    def rows(self) -> list[dict]:
        return [dict(r) for r in self._rows]

    def reset(self) -> None:
        self._rows.clear()


class MemoryStore:
    """PG 实现。同步 psycopg，与 ``RunControl`` / ``ReviewQueue`` 同一形状。

    为什么不像会话那边用 async engine：调用点是两处 —— 模型的工具函数（同步）与
    注入口（异步但只查一次）。用同步驱动 + ``asyncio.to_thread`` 只需要一份实现，
    也不会出现"异步连接绑在别的 loop 上"的问题（摘要客户端同一条理由）。
    """

    def __init__(self, dsn: str = "") -> None:
        self.dsn = (dsn or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.dsn)

    def ensure_schema(self) -> None:
        if not self.enabled:
            return
        import psycopg

        with psycopg.connect(self.dsn, connect_timeout=10, autocommit=True) as conn:
            with conn.cursor() as cur:
                for statement in SCHEMA_STATEMENTS:
                    cur.execute(statement)

    def active(self, owner_key: str, *, limit: int = 40) -> list[dict]:
        if not self.enabled or not owner_key:
            return []
        import psycopg

        sql = (
            "SELECT id, kind, content, pinned, use_count, created_at"
            "  FROM agent_memories"
            " WHERE owner_key = %s AND invalid_at IS NULL"
            " ORDER BY pinned DESC, use_count DESC, created_at DESC"
            " LIMIT %s"
        )
        with psycopg.connect(self.dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (owner_key, int(limit)))
                rows = cur.fetchall()
        return [
            {"id": r[0], "kind": r[1], "content": r[2], "pinned": r[3],
             "use_count": r[4], "created_at": r[5]}
            for r in rows
        ]

    def remember(self, owner_key: str, content: str, *, kind: str = "project",
                 pinned: bool = False, tenant: str = "", source_thread: str = "",
                 supersedes: str = "") -> dict:
        text = normalize_content(content)
        if not text:
            return {"ok": False, "reason": "empty"}
        if not self.enabled or not owner_key:
            return {"ok": False, "reason": "unavailable"}
        import psycopg

        h = content_hash(owner_key, text)
        new_id = uuid.uuid4().hex
        with psycopg.connect(self.dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                # 同一件事被重复说一遍 ⇒ 只刷新使用痕迹，不产生第二条。
                cur.execute(
                    "SELECT id FROM agent_memories"
                    " WHERE owner_key = %s AND content_hash = %s AND invalid_at IS NULL"
                    " LIMIT 1", (owner_key, h))
                row = cur.fetchone()
                if row:
                    cur.execute(
                        "UPDATE agent_memories"
                        "   SET use_count = use_count + 1, last_used_at = now()"
                        " WHERE id = %s", (row[0],))
                    return {"ok": True, "id": row[0], "action": "refreshed"}

                replaced: list[str] = []
                if supersedes:
                    # 改口：先失效旧的、再插新的，**同一事务**。分两次提交的中间态是
                    # "一条有效记忆都没有"，注入时表现成"它忽然忘了这件事"。
                    cur.execute(
                        "SELECT id FROM agent_memories"
                        " WHERE owner_key = %s AND invalid_at IS NULL"
                        "   AND (id = %s OR content LIKE %s)",
                        (owner_key, supersedes, f"%{supersedes}%"))
                    replaced = [r[0] for r in cur.fetchall()]
                    if replaced:
                        cur.execute(
                            "UPDATE agent_memories"
                            "   SET invalid_at = now(), supersede_reason = 'superseded'"
                            " WHERE id = ANY(%s)", (replaced,))

                cur.execute(
                    "INSERT INTO agent_memories"
                    " (id, owner_key, tenant, kind, content, content_hash,"
                    "  source_thread, pinned, superseded_by)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL)",
                    (new_id, owner_key, tenant, kind if kind in KINDS else "project",
                     text, h, source_thread, bool(pinned)))
                if replaced:
                    cur.execute(
                        "UPDATE agent_memories SET superseded_by = %s WHERE id = ANY(%s)",
                        (new_id, replaced))
        return {"ok": True, "id": new_id, "action": "added", "superseded": replaced}

    def forget(self, owner_key: str, *, memory_id: str = "", content: str = "") -> int:
        if not self.enabled or not owner_key or not (memory_id or content):
            return 0
        import psycopg

        with psycopg.connect(self.dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                if memory_id:
                    cur.execute(
                        "UPDATE agent_memories"
                        "   SET invalid_at = now(), supersede_reason = 'revoked'"
                        " WHERE owner_key = %s AND id = %s AND invalid_at IS NULL",
                        (owner_key, memory_id))
                else:
                    cur.execute(
                        "UPDATE agent_memories"
                        "   SET invalid_at = now(), supersede_reason = 'revoked'"
                        " WHERE owner_key = %s AND content LIKE %s AND invalid_at IS NULL",
                        (owner_key, f"%{content}%"))
                return cur.rowcount or 0

    def forget_owner(self, owner_key: str) -> int:
        if not self.enabled or not owner_key:
            return 0
        import psycopg

        with psycopg.connect(self.dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM agent_memories WHERE owner_key = %s",
                            (owner_key,))
                return cur.rowcount or 0


#: 进程内单例（与 `session.py` 的会话缓存同一口径）。测试用它拿同一个实例。
_MEMORY_STORE: Any = None
_LOCK = threading.Lock()


def make_memory_store(dsn: str = "") -> Any:
    """按有没有 DSN 选实现。**没有配置时返回内存版**，不是 None ——
    "记忆不可用"与"记忆是空的"对调用方是同一件事，少一个分支少一个漏判。"""
    global _MEMORY_STORE
    with _LOCK:
        if _MEMORY_STORE is None:
            _MEMORY_STORE = MemoryStore(dsn) if dsn else InMemoryMemoryStore()
        return _MEMORY_STORE


def reset_memory_store(store: Any = None) -> None:
    """换掉进程内实例（**仅供测试**）。"""
    global _MEMORY_STORE
    with _LOCK:
        _MEMORY_STORE = store
