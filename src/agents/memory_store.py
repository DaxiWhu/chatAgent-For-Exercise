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
import math
import os
import re
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "KINDS",
    "KIND_WEIGHT",
    "MAX_CONTENT_CHARS",
    "SCHEMA_STATEMENTS",
    "InMemoryMemoryStore",
    "MemoryStore",
    "make_memory_store",
    "max_live_per_owner",
    "normalize_content",
    "rank_memories",
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
        source           text NOT NULL DEFAULT 'memory',
        batch_id         text NOT NULL DEFAULT '',
        pinned           boolean NOT NULL DEFAULT false,
        use_count        integer NOT NULL DEFAULT 0,
        valid_from       timestamptz NOT NULL DEFAULT now(),
        invalid_at       timestamptz,
        stale_at         timestamptz,
        superseded_by    text,
        supersede_reason text NOT NULL DEFAULT '',
        created_at       timestamptz NOT NULL DEFAULT now(),
        last_used_at     timestamptz
    )
    """,
    # ---- 老库补列（v12）。CREATE TABLE IF NOT EXISTS 不会改已存在的表，缺这三句
    # 老库会一直少列，而报错点是"插入失败：column does not exist"，离原因很远。
    "ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS"
    " source text NOT NULL DEFAULT 'memory'",
    "ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS"
    " batch_id text NOT NULL DEFAULT ''",
    "ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS stale_at timestamptz",
    # 注入只取有效行，所以这个索引是热路径的那一条。
    """
    CREATE INDEX IF NOT EXISTS agent_memories_owner
        ON agent_memories (owner_key, invalid_at, kind)
    """,
    # 淘汰扫描用：只关心"还有效且没被淘汰"的行，部分索引比全表小一个量级。
    """
    CREATE INDEX IF NOT EXISTS agent_memories_live
        ON agent_memories (owner_key, last_used_at)
        WHERE invalid_at IS NULL AND stale_at IS NULL
    """,
    # 整批回滚用（压缩抽出的候选带同一个 batch_id）。
    """
    CREATE INDEX IF NOT EXISTS agent_memories_batch
        ON agent_memories (owner_key, batch_id) WHERE batch_id <> ''
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


# --------------------------------------------------------------------------- #
# 价值排序：取哪几条进上下文
# --------------------------------------------------------------------------- #
#: 类别权重。为什么不是"所有记忆一视同仁"：``persona`` / ``preference`` 说的是
#: "这个人是谁、定过什么规矩"，长期有效；``reference`` / ``feedback`` 多半跟某次
#: 任务相关，过期就废。2026-11-02 之前排序是 ``created_at DESC`` —— 新写的排前面，
#: 于是**写得最早的那些稳定偏好反而最先被挤掉**，表一过 40 条就开始静默遗忘。
KIND_WEIGHT = {
    "persona": 1.0,
    "preference": 0.9,
    "project": 0.7,
    "feedback": 0.5,
    "reference": 0.4,
}

#: 时间衰减的**分类**半衰期（天）。基准是 ``last_used_at``（没被用过就退回
#: ``created_at``），不是创建时间 —— 一条两年前定的规矩，只要上周还被读进上下文，
#: 它就不该衰减。
#:
#: 为什么按类别分而不是统一 90 天：**偏好的有效期本来就比项目事实长**。
#: 统一半衰期实测过一次，结果是 120 天前写下的「日期一律用 YYYY-MM-DD」
#: 被 45 条当天的项目事实挤出了前 40 条 —— 那正是这次要修的病，换个公式又犯一遍。
#: 偏好改口靠 ``invalid_at``，不靠衰减；项目事实会随项目变化而过期，才该衰减。
_HALF_LIFE_DAYS = {
    "persona": 730.0,
    "preference": 730.0,
    "project": 120.0,
    "feedback": 60.0,
    "reference": 60.0,
}
_DEFAULT_HALF_LIFE = 120.0

#: 单用户"仍参与注入"的记忆条数上限。超出的按分数从低到高标 ``stale_at``。
#: 为什么要有：没有它，表只增不减，而读取一侧有 40 条上限 ⇒ 多出来的那些
#: **永远读不到、也不会消失**。那是静默遗忘，比删除更难发现。
_DEFAULT_MAX_LIVE = 200

#: 新写入的保护期（天）。淘汰只清理"长期没在场"的记忆，刚写进来的还没机会被用到 ——
#: 按分数砍尾巴时它会因为分数低（还没被 touch 过）而首当其冲，那是反的。
_PROTECT_DAYS = 7

#: 单次读取的扫描上限（条）。它**不是**注入条数上限（那个是 ``limit`` 参数），
#: 只是"一个用户最多扫多少行进内存排序"的保护值。噪声用户攒到几千条时，
#: 排序成本仍然可控。
_SCAN_LIMIT = 500


def max_live_per_owner() -> int:
    """淘汰上限（``AGENT_MEMORY_MAX_LIVE``，默认 200，下限 20）。

    下限不是随手定的：低于几十条时，"淘汰"会开始删掉仍在用的记忆，
    而它换来的空间本来就不缺。
    """
    raw = (os.getenv("AGENT_MEMORY_MAX_LIVE", "") or "").strip()
    try:
        return max(20, int(raw)) if raw else _DEFAULT_MAX_LIVE
    except ValueError:
        return _DEFAULT_MAX_LIVE


def _as_dt(value: Any) -> datetime | None:
    """把 ``datetime`` / ISO 字符串统一成带时区的 datetime；认不出返回 None。

    两种形态都要支持：PG 驱动给的是 datetime，``InMemoryMemoryStore`` 存的是
    ``isoformat()`` 字符串。
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _score(row: dict, now: datetime) -> float:
    """一条记忆的价值分数（0~1）。权重与理由见 ``KIND_WEIGHT`` 那条注释。"""
    kind = str(row.get("kind") or "")
    weight = KIND_WEIGHT.get(kind, 0.5)
    half_life = _HALF_LIFE_DAYS.get(kind, _DEFAULT_HALF_LIFE)
    stamp = _as_dt(row.get("last_used_at")) or _as_dt(row.get("created_at"))
    if stamp is None:
        recency = 0.5
    else:
        age_days = max(0.0, (now - stamp).total_seconds() / 86400.0)
        recency = 0.5 ** (age_days / half_life)
    # 使用痕迹封顶在 10 次：再往上加已经没有区分度，而它对总数的影响本来就最小。
    usage = min(1.0, math.log1p(row.get("use_count") or 0) / math.log1p(10))
    return 0.45 * weight + 0.40 * recency + 0.15 * usage


def rank_memories(rows: list[dict], *, now: datetime | None = None) -> list[dict]:
    """按价值排序（**纯函数**，便于单测；存储层只负责取和写）。

    排序键：``pinned`` 置顶 → 分数降序 → ``created_at`` 降序 → ``id`` 升序。

    最后一个键不是装饰：前三个键全部相同时（同分、同时间写入），顺序就取决于
    输入顺序 —— 而 PG 不保证 ``SELECT`` 返回顺序稳定。后果是注入出去的前缀
    每轮可能不一样，上游缓存永远命不中，症状是"变慢变贵，但零报错"。
    """
    moment = now or datetime.now(UTC)
    return sorted(
        rows,
        key=lambda r: (
            not r.get("pinned"),
            -_score(r, moment),
            -_as_dt(r.get("created_at")).timestamp()
            if _as_dt(r.get("created_at")) else 0.0,
            str(r.get("id") or ""),
        ),
    )


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
        # stale_at 非空 = 被容量淘汰了：行还在（可查、可恢复），但不再参与注入。
        return [r for r in self._rows
                if r["owner_key"] == owner_key and r["invalid_at"] is None
                and r.get("stale_at") is None]

    def active(self, owner_key: str, *, limit: int = 40) -> list[dict]:
        with self._lock:
            rows = rank_memories(self._active(owner_key))
            return [dict(r) for r in rows[:limit]]

    def remember(self, owner_key: str, content: str, *, kind: str = "project",
                 pinned: bool = False, tenant: str = "", source_thread: str = "",
                 supersedes: str = "", source: str = "memory",
                 batch_id: str = "") -> dict:
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
                "source": source, "batch_id": batch_id,
                "pinned": bool(pinned), "use_count": 0,
                "valid_from": now, "invalid_at": None, "superseded_by": None,
                "stale_at": None,
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

    def touch(self, owner_key: str, ids: list[str]) -> int:
        """记一次"这批被读进上下文了"：只更新 ``last_used_at``。

        ⚠️ 不碰 ``use_count``：那个数是**重复强调的次数**（同一条被再说一遍），
        不是访问频率。混在一起会让"反复改口的一条"和"每轮都要用的一条"变得
        无法区分 —— 而它们该有完全不同的权重。

        为什么只动 ``last_used_at``：它是衰减的基准。一条被注入过的记忆，
        在"还新不新"这个维度上就该回到起点。
        """
        if not ids:
            return 0
        wanted = set(ids)
        now = datetime.now(UTC).isoformat()
        with self._lock:
            n = 0
            for r in self._active(owner_key):
                if r["id"] in wanted:
                    r["last_used_at"] = now
                    n += 1
        return n

    def prune(self, owner_key: str, *, keep: int | None = None) -> int:
        """容量淘汰：把分数最低的标 ``stale_at``，返回条数。

        **不物理删**，和"改口"用同一套语义 —— 被淘汰的记忆仍然可查、可恢复，
        只是不再参与注入。物理删的后果是"用户三个月前提过的规矩"永久消失，
        而没人知道自己丢过什么。

        ``keep`` 默认取 ``max_live_per_owner()``。**新建不足保护期的不淘汰**：
        淘汰不是"按新旧排序砍尾巴"，而是"清理长期没在场的东西"，刚写进来的
        还没机会被用到。
        """
        limit = max(1, int(keep if keep is not None else max_live_per_owner()))
        now = datetime.now(UTC)
        with self._lock:
            live = self._active(owner_key)
            if len(live) <= limit:
                return 0
            ranked = rank_memories(live, now=now)
            n = 0
            for row in ranked[limit:]:
                created = _as_dt(row.get("created_at"))
                if created and (now - created).days < _PROTECT_DAYS:
                    continue
                row["stale_at"] = now.isoformat()
                n += 1
        return n

    def forget_batch(self, owner_key: str, batch_id: str) -> int:
        """整批撤回（一次压缩抽出的候选共用一个批次号）。

        为什么按批而不是按条：污染进来时你不知道它抽了几条、都抽了什么，
        逐条找既慢又容易漏。``batch_id`` 是"这次压缩"的身份证。
        """
        if not batch_id:
            return 0
        now = datetime.now(UTC).isoformat()
        n = 0
        with self._lock:
            for r in self._active(owner_key):
                if r.get("batch_id") == batch_id:
                    r["invalid_at"], r["supersede_reason"] = now, "revoked"
                    n += 1
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

    def _live_rows(self, owner_key: str) -> list[dict]:
        """该用户全部"仍参与注入"的行（有效、未被淘汰、受扫描上限保护）。

        取全量再在 Python 里排，而不是把分数写进 ``ORDER BY``：分数含
        ``0.5 ** (age/半衰期)`` 这类运算，SQL 与本模块各写一份必然分叉，
        而分叉的表现是"库里排出来的和测试里排出来的不一样"，极难发现。
        """
        if not self.enabled or not owner_key:
            return []
        import psycopg

        sql = (
            "SELECT id, kind, content, pinned, use_count, created_at, last_used_at"
            "  FROM agent_memories"
            " WHERE owner_key = %s AND invalid_at IS NULL AND stale_at IS NULL"
            " ORDER BY created_at DESC"
            " LIMIT %s"
        )
        with psycopg.connect(self.dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (owner_key, _SCAN_LIMIT))
                rows = cur.fetchall()
        return [
            {"id": r[0], "kind": r[1], "content": r[2], "pinned": r[3],
             "use_count": r[4], "created_at": r[5], "last_used_at": r[6]}
            for r in rows
        ]

    def active(self, owner_key: str, *, limit: int = 40) -> list[dict]:
        return rank_memories(self._live_rows(owner_key))[:max(1, int(limit))]

    def remember(self, owner_key: str, content: str, *, kind: str = "project",
                 pinned: bool = False, tenant: str = "", source_thread: str = "",
                 supersedes: str = "", source: str = "memory",
                 batch_id: str = "") -> dict:
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
                    "  source_thread, source, batch_id, pinned, superseded_by)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)"
                    # 交给唯一索引裁决，而不是"先查一次没有再插"。
                    # 后者在并发下必然翻车：两个事务都查不到已有行、都去插，
                    # 后到的那个撞上部分唯一索引直接抛 UniqueViolation。
                    # 2026-11-02 之后这条路**真的会并发** —— 压缩在后台顺手写记忆，
                    # 同一轮里模型也可能在写同一条。
                    # ⚠️ 谓词必须与索引定义**逐字一致**（都是 invalid_at IS NULL），
                    # 否则 PG 推断不到那个部分索引，报"没有匹配的约束"。
                    " ON CONFLICT (owner_key, content_hash) WHERE invalid_at IS NULL"
                    " DO NOTHING RETURNING id",
                    (new_id, owner_key, tenant, kind if kind in KINDS else "project",
                     text, h, source_thread, source, batch_id, bool(pinned)))
                inserted = cur.fetchone()
                if inserted is None:
                    # 并发：别的写入方抢先插了同一条。当作"刷新"，别报成失败 ——
                    # 结果一样是一条生效行，只是使用痕迹记在它身上。
                    cur.execute(
                        "SELECT id FROM agent_memories"
                        " WHERE owner_key = %s AND content_hash = %s"
                        "   AND invalid_at IS NULL LIMIT 1", (owner_key, h))
                    row = cur.fetchone()
                    if row is None:
                        # 对方在同一瞬间又把它失效了（改口）。不猜，如实回报。
                        return {"ok": False, "reason": "conflict"}
                    cur.execute(
                        "UPDATE agent_memories"
                        "   SET use_count = use_count + 1, last_used_at = now()"
                        " WHERE id = %s", (row[0],))
                    if replaced:
                        cur.execute(
                            "UPDATE agent_memories SET superseded_by = %s"
                            " WHERE id = ANY(%s)", (row[0], replaced))
                    return {"ok": True, "id": row[0], "action": "refreshed"}
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

    def touch(self, owner_key: str, ids: list[str]) -> int:
        """记一次"这批被读进上下文了"：只更新 ``last_used_at``（语义见内存版）。"""
        if not self.enabled or not owner_key or not ids:
            return 0
        import psycopg

        with psycopg.connect(self.dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE agent_memories SET last_used_at = now()"
                    " WHERE owner_key = %s AND id = ANY(%s) AND invalid_at IS NULL",
                    (owner_key, list(ids)))
                return cur.rowcount or 0

    def prune(self, owner_key: str, *, keep: int | None = None) -> int:
        """容量淘汰（语义见内存版）：标 ``stale_at``，不物理删。"""
        limit = max(1, int(keep if keep is not None else max_live_per_owner()))
        now = datetime.now(UTC)
        rows = self._live_rows(owner_key)
        if len(rows) <= limit:
            return 0
        victims = []
        for row in rank_memories(rows, now=now)[limit:]:
            created = _as_dt(row.get("created_at"))
            if created and (now - created).days < _PROTECT_DAYS:
                continue
            victims.append(row["id"])
        if not victims:
            return 0
        import psycopg

        with psycopg.connect(self.dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE agent_memories SET stale_at = now()"
                    " WHERE owner_key = %s AND id = ANY(%s) AND stale_at IS NULL",
                    (owner_key, victims))
                return cur.rowcount or 0

    def forget_batch(self, owner_key: str, batch_id: str) -> int:
        """整批撤回（一次压缩抽出的候选共用一个批次号）。"""
        if not self.enabled or not owner_key or not batch_id:
            return 0
        import psycopg

        with psycopg.connect(self.dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE agent_memories"
                    "   SET invalid_at = now(), supersede_reason = 'batch_revoked'"
                    " WHERE owner_key = %s AND batch_id = %s AND invalid_at IS NULL",
                    (owner_key, batch_id))
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
