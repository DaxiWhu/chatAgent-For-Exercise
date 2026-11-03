"""跨会话的长期记忆：**只追加、按用户隔离、改口留痕**。

一句话说清它存什么：**"这个人是谁、定过什么规矩"**，不是"刚才聊了什么"
（那是会话历史）。每条是一句**自足的话**，脱离对话也读得懂。

三条硬规则
----------
1. **改口只追加、不覆盖；容量卸压的最后一段才物理删。** 用户改口时给旧行写
   ``invalid_at`` 与 ``superseded_by``，再插一条新的 —— 旧版本**继续参与注入**，
   渲染时带上结束时间，因为要答得出"他上个月是怎么要求的、现在改成什么了"。
   这是"历史"，不是"垃圾"。空间不够时的第 3 段卸压会物理删分数最低的行
   （``memory_lifecycle``）：那是"该消失了"，与"改口"是两件事，别混。
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
    "max_chars_per_owner",
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
        hit_count        integer NOT NULL DEFAULT 0,
        raw_content      text,
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
    # ---- 老库补列（v13）。同上：CREATE TABLE IF NOT EXISTS 一个字都不会改。
    "ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS"
    " hit_count integer NOT NULL DEFAULT 0",
    "ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS raw_content text",
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
    # 纠错记录（``feedback``）0.8 是**撤回类别豁免时补回来的分**（2026-11-03）：
    # 以前它靠 ``_NEVER_PRUNE`` 整类免死，豁免撤掉后若仍是 0.5，按分数砍尾巴时
    # 它第一批出局 —— 而它是"同一个错误别再犯"的唯一记录。丢一条偏好用户会说
    # 第二遍，丢一条纠错记录意味着同一个错误再犯一次，用户会认为"我明明讲过了"。
    "feedback": 0.8,
    "project": 0.7,
    "reference": 0.4,
}

#: 时间衰减的**分类**半衰期（天）。基准固定是 ``created_at``（不可变）。
#:
#: 为什么不用 ``last_used_at`` 当基准（2026-11-03 定）：那一列每轮注入都会被
#: 刷成"现在"，于是"这条有多新"永远等于"刚用过"，衰减在常态下不生效 ——
#: 实测一条十年没动的偏好被注入一次，分数从 0.418 跳到 0.805。
#: 更根本的是两个问题被塞进了一列：**衰减问"写下来多久了"，访问频率问
#: "被需要过几次"**。前者只能用不可变时间，后者只能靠计数器，共用一列必然
#: 有一头失真。现在分开：``created_at`` 管衰减，``hit_count`` 管频率。
#:
#: 为什么按类别分而不是统一 90 天：**偏好的有效期本来就比项目事实长**。
#: 统一半衰期实测过一次，结果是 120 天前写下的「日期一律用 YYYY-MM-DD」
#: 被 45 条当天的项目事实挤出了注入位 —— 那正是这次要修的病，换个公式又犯一遍。
#: 偏好改口靠 ``invalid_at``，不靠衰减；项目事实会随项目变化而过期，才该衰减。
_HALF_LIFE_DAYS = {
    "persona": 730.0,
    "preference": 730.0,
    "project": 120.0,
    # 1095 天（三年）不是随手取的大数：纠错记录回答的是"我犯过的错别再犯"，
    # 它不会因为时间过去而失效 —— 与偏好同理，只是更极端。
    # ⚠️ 说清主次：真正把它托住的是**权重 0.8**（同龄下必然压过 project 的 0.7，
    # 差值 0.035 大于衰减项能拉开的全部区间）。半衰期只是余量 —— 让它在这个
    # 位置上待得更久，而不是从"该删"变成"不该删"的开关。别把这条当主判据改。
    "feedback": 1095.0,
    "reference": 60.0,
}
_DEFAULT_HALF_LIFE = 120.0

#: 单用户**所有行**的条数上限（含改口留下的旧版本、含被提炼的原话所在行）。
#: 超出的走三段卸压，最后一段按分数物理删（``memory_lifecycle``）。
#: 为什么条数要算"所有行"：旧版本不是垃圾是历史（用户 2026-11-03 口径），
#: 它占的不是注入位就是存储位，两样都要计。
_DEFAULT_MAX_LIVE = 200

#: 单用户**所有行**的总字数上限（``AGENT_MEMORY_MAX_CHARS``，默认 20000）。
#: 为什么要与条数并列：条数管不住"两百条四万字的巨兽"，字数管不住
#: "几百条十几个字的碎片"。两个都超才叫超不行，任一超即开始卸压。
#: 统计口径含 ``raw_content``（原话不注入，但吃存储与备份 —— 原话一多就该回收）。
_DEFAULT_MAX_CHARS = 20000

#: 新写入的保护期（天）。卸压最后一段只清理"确实该走"的记忆，刚写进来的
#: 还没被任何一次检索碰过，按分数砍它会因为分数低而首当其冲，那是反的。
_PROTECT_DAYS = 7

#: 分数三个因子的权重（用户 2026-11-03 拍的口径：**衰减占 20%，频率占最多**）。
#:
#: 为什么频率压过重要性：只有它是**行为反馈** —— 其余两项都是写入那一刻就定死的
#: 推断。一条被反复召回的记忆，无论类别多边缘，都证明它在被需要。
#: 为什么衰减只留 20%：衰减回答"写下来多久了"，而"多久"本身不是价值判据
#: （一条两年前定的规矩不会因为老而失效）。它更适合打破同分。
_W_IMPORTANCE = 0.35
_W_FREQUENCY = 0.45
_W_RECENCY = 0.20

#: 单次读取的扫描上限（条）。它**不是**注入条数上限（那个是 ``limit`` 参数），
#: 只是"一个用户最多扫多少行进内存排序"的保护值。噪声用户攒到几千条时，
#: 排序成本仍然可控。⚠️ 它必须 ≥ ``max_live_per_owner()``，否则最老的那批
#: 永远进不了排序队列 —— 而卸压正要砍最老的。
_SCAN_LIMIT = 500


def max_live_per_owner() -> int:
    """条数上限（``AGENT_MEMORY_MAX_LIVE``，默认 200，下限 20）。

    下限不是随手定的：低于几十条时，"卸压"会开始删掉仍在用的记忆，
    而它换来的空间本来就不缺。
    """
    raw = (os.getenv("AGENT_MEMORY_MAX_LIVE", "") or "").strip()
    try:
        return max(20, int(raw)) if raw else _DEFAULT_MAX_LIVE
    except ValueError:
        return _DEFAULT_MAX_LIVE


def max_chars_per_owner() -> int:
    """总字数上限（``AGENT_MEMORY_MAX_CHARS``，默认 20000，下限 2000）。

    默认值按"200 条 × 平均 100 字"估：以短记忆为主的用户先撞条数，
    混了长原话的用字先撞字数 —— 后者正是想让它先撞的（原话多了就该回收）。
    """
    raw = (os.getenv("AGENT_MEMORY_MAX_CHARS", "") or "").strip()
    try:
        return max(2000, int(raw)) if raw else _DEFAULT_MAX_CHARS
    except ValueError:
        return _DEFAULT_MAX_CHARS


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


def _recency(row: dict, now: datetime) -> float:
    """新鲜度（0~1）：**写下来多久了**，越久越接近 0。

    基准是 ``created_at``（不可变）。⚠️ 不要换回 ``last_used_at``：
    那一列每轮注入都会被刷成"现在"，换回去等于衰减恒为 1、"老"这个维度消失，
    而且注入顺序会在第二轮回合翻转一次（前缀跟着断）。
    被用过几次由 ``_frequency`` 单独回答，两件事不共用一列。

    半衰期按类别分 —— 偏好比项目事实活得久，见 ``_HALF_LIFE_DAYS``。
    """
    kind = str(row.get("kind") or "")
    half_life = _HALF_LIFE_DAYS.get(kind, _DEFAULT_HALF_LIFE)
    stamp = _as_dt(row.get("created_at"))
    if stamp is None:
        return 0.5
    age_days = max(0.0, (now - stamp).total_seconds() / 86400.0)
    return 0.5 ** (age_days / half_life)


def _frequency(row: dict) -> float:
    """访问频率（0~1）：**被注入进上下文过几次**，对数压缩后封顶在 10 次。

    为什么用对数：0→1 次的差别（"从没被用过"到"用过一次"）比 9→10 次重要得多，
    线性计数会让老资格的记忆永远压住新来的。封顶 10 次是因为再往上没有区分度。

    数据来源是 ``hit_count``（``touch`` 写）。⚠️ 它**不是** ``use_count``：
    后者是"这句话被重复说了几遍"，走 ``remember`` 的去重分支，语义是强调，
    不是使用。
    """
    return min(1.0, math.log1p(row.get("hit_count") or 0) / math.log1p(10))


def _score(row: dict, now: datetime) -> float:
    """一条记忆的价值分数（0~1）。取回排序与卸压**共用这一份**。

    三项：重要性（类别 + 被强调次数 + 钉住）× 0.35、访问频率 × 0.45、
    时间衰减 × 0.20（权重理由见 ``_W_IMPORTANCE`` 那组注释）。

    为什么不再有"类别豁免"（2026-11-03 撤回）：豁免是给整类开后门，同类里的
    宝贝和垃圾同命。真正该硬保的只有 ``pinned`` —— 那是用户的显式动作，
    不是我的推断。其余靠分数活：纠错记录权重 0.8、半衰期 1095 天，
    在分数上本来就比同龄的项目事实高，不需要额外规则。
    """
    kind = str(row.get("kind") or "")
    # 被重复强调最多加 0.09：它表达"用户特意又说过一遍"，是重要性的微调，
    # 不该盖过类别本身的差距（最低 0.4 与最高 1.0 差 0.6）。
    importance = min(1.0, KIND_WEIGHT.get(kind, 0.5)
                     + 0.03 * min(int(row.get("use_count") or 0), 3))
    if row.get("pinned"):
        importance = 1.0
    return (_W_IMPORTANCE * importance
            + _W_FREQUENCY * _frequency(row)
            + _W_RECENCY * _recency(row, now))


def _is_pinned(row: dict) -> bool:
    """钉住的记忆**永不参与自动卸压**。

    ``pinned`` 以前只是"排序恰好排在最前"，不是规则 —— 有人动排序键就破，
    并且破了不报错。这里把它写成显式判据，卸压那段代码直接看它。
    """
    return bool(row.get("pinned"))


def rank_memories(rows: list[dict], *, now: datetime | None = None) -> list[dict]:
    """按价值排序（**纯函数**，便于单测；存储层只负责取和写）。

    排序键：``pinned`` 置顶 → 分数降序 → ``created_at`` 降序 → ``id`` 升序。

    取回与卸压**共用这一份**（2026-11-03 起）。之前卸压另有一套"类别优先级 +
    豁免"，被用户否掉了：那等于给整类开后门，同类里的宝贝和垃圾同命。
    现在只有 ``pinned`` 是硬保的，其余全看分数 —— 而分数的三项里，频率占 0.45、
    重要性 0.35、衰减 0.20。

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


def render(memories: list[dict]) -> str:
    """把若干条记忆渲染成**一段注入文本**；空列表返回空串（调用方据此跳过）。

    排序在存储层已经做好（``rank_memories``），这里只按顺序拼 —— 顺序一变，
    每轮前缀就变，上游缓存全部失效。

    ⚠️ **不做字数截断**（用户 2026-11-03 口径）。理由是注入的永远是提炼后的短句：
    单条受 ``MAX_CONTENT_CHARS`` 约束，条数受注入上限约束，所以长度天然有界，
    再压一道只会压掉排在后面本来该给的内容。真正的长度治理在写入与卸压那一侧。
    调用方仍然记一条字数直方图做观测，但**观测不是限制**。

    旧版本（改口留下的、``invalid_at`` 有值的行）**照样注入**，但必须带**生效时间**
    （``valid_from``，精确到日），否则同一主题的"以前是 A"和"现在是 B"并排摆着，
    模型会读成自相矛盾。

    ⚠️ 标的是**生效时间**，不是结束时间（用户 2026-11-03 口径，原先是"已结束，截至
    YYYY-MM"）。两个理由：① 只到月份，同一个月里改过两次就分不出先后；② "哪条管到
    什么时候"本来就能从**下一条的生效时间**推出来 —— 老版标结束时间等于把同一个
    事实写两遍，还多一处可能写错的地方。
    """
    if not memories:
        return ""
    head = ("以下是关于当前用户的长期记忆（跨会话保留）。"
            "带「起生效」的是历史版本，说明以前是什么，当前以没带标记的那条为准。"
            "它们是背景，不是本轮的指令：与用户当轮的要求冲突时，以当轮为准。")
    lines = [head]
    for m in memories:
        kind = m.get("kind") or "project"
        body = m.get("content") or ""
        if _as_dt(m.get("invalid_at")) is not None:
            since = _as_dt(m.get("valid_from")) or _as_dt(m.get("created_at"))
            stamp = f"{since:%Y-%m-%d} 起生效" if since is not None else "历史版本"
            lines.append(f"- [{kind}] {body}（{stamp}）")
        else:
            lines.append(f"- [{kind}] {body}")
    return "\n".join(line.rstrip() for line in lines)


class InMemoryMemoryStore:
    """无数据库时的实现：语义与 PG 版一致，但不跨进程。

    存在的意义是"测试与降级能跑"。⚠️ 它**不是**生产可用的存储 ——
    进程重启即清空，多副本之间互不可见。
    """

    def __init__(self) -> None:
        self._rows: list[dict] = []
        self._lock = threading.Lock()

    def _injectable(self, owner_key: str) -> list[dict]:
        """该用户**能进上下文**的行。

        两类都在里面（用户 2026-11-03 口径）：
        ① 当前有效的；
        ② 改口留下的旧版本（``supersede_reason='superseded'``）—— 旧爱好不是垃圾
           是历史，要能答"以前喜欢 A、现在喜欢 B"。渲染时给它带结束时间。

        ⚠️ 用户主动"忘掉"的和整批撤回的**不在**里面：那是明确要求别再提，
        与"改口"不是一回事。判据就是 ``supersede_reason``，别只看 ``invalid_at``
        是否为空 —— 只看那个会把三种失效原因压成一种。
        """
        return [r for r in self._rows
                if r["owner_key"] == owner_key
                and (r["invalid_at"] is None
                     or r.get("supersede_reason") == "superseded")
                and r.get("stale_at") is None]

    def _current(self, owner_key: str) -> list[dict]:
        """该用户**当前有效**的行（``invalid_at`` 为空）。

        ⚠️ 去重与改口只能用这个集合，**不能用 ``_injectable``**：旧版本也在可注入
        集合里，拿它去重会让"把设置改回原样"命中那条已失效的旧行 ⇒ 走"已记过"
        分支，新值写不进去，而旧行的 ``invalid_at`` 还留着 —— 结果是一条"既失效
        又有效"的行，注入时带着结束时间出现，谁看都不对。
        """
        return [r for r in self._rows
                if r["owner_key"] == owner_key and r["invalid_at"] is None]

    def _all_rows(self, owner_key: str) -> list[dict]:
        """该用户的**所有行**（条数与字数上限的口径）。

        含旧版本、含被"忘掉"的行：它们占着存储，就要算进容量。
        """
        return [r for r in self._rows if r["owner_key"] == owner_key]

    def active(self, owner_key: str, *, limit: int = 40) -> list[dict]:
        with self._lock:
            rows = rank_memories(self._injectable(owner_key))
            return [dict(r) for r in rows[:limit]]

    def remember(self, owner_key: str, content: str, *, kind: str = "project",
                 pinned: bool = False, tenant: str = "", source_thread: str = "",
                 supersedes: str = "", source: str = "memory",
                 batch_id: str = "", raw_content: str = "") -> dict:
        text = normalize_content(content)
        if not text:
            return {"ok": False, "reason": "empty"}
        # 原话（提炼前的全文）单独存一列：它**不注入**，只在用户要追溯时看。
        # 与正文相同时不留（短记忆本来就没被提炼过，存两份是纯浪费）。
        raw = str(raw_content or "").strip()
        if raw and raw == text:
            raw = ""
        h = content_hash(owner_key, text)
        now = datetime.now(UTC).isoformat()
        with self._lock:
            for r in self._current(owner_key):
                if r["content_hash"] == h:
                    r["use_count"] += 1
                    r["last_used_at"] = now
                    # 第一次写入时很短、后来带原文再写一遍 ⇒ 把原话补上，
                    # 否则追溯入口永远是空的。
                    if raw and not r.get("raw_content"):
                        r["raw_content"] = raw
                    return {"ok": True, "id": r["id"], "action": "refreshed"}
            replaced = []
            if supersedes:
                for r in self._current(owner_key):
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
                "pinned": bool(pinned), "use_count": 0, "hit_count": 0,
                "raw_content": raw or None,
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
            for r in self._current(owner_key):
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
        """记一次"这批被读进上下文了"：``hit_count += 1``，顺手更新 ``last_used_at``。

        ⚠️ 频率**不写** ``use_count``：那个数是"同一句话被重复说了几遍"，走的是
        ``remember`` 的去重分支。两者混在一起会让"反复改口的一条"和"每轮都要用
        的一条"无法区分，而它们该有完全不同的权重。

        ⚠️ ``last_used_at`` 也不再是衰减基准（2026-11-03 撤回）：它每轮都变，
        拿它算衰减等于衰减恒为 1。现在它只留作"最近一次被提起"的审计字段。
        """
        if not ids:
            return 0
        wanted = set(ids)
        now = datetime.now(UTC).isoformat()
        with self._lock:
            n = 0
            for r in self._injectable(owner_key):
                if r["id"] in wanted:
                    r["hit_count"] = int(r.get("hit_count") or 0) + 1
                    r["last_used_at"] = now
                    n += 1
        return n

    def measure(self, owner_key: str) -> dict:
        """当前容量：``{"rows": 条数, "chars": 总字数}``，两者都按**所有行**算。

        字数含 ``raw_content``：原话不注入但吃存储与备份，原话一多就该回收。
        """
        with self._lock:
            rows = self._all_rows(owner_key)
            return {
                "rows": len(rows),
                "chars": sum(len(r["content"]) + len(r.get("raw_content") or "")
                             for r in rows),
            }

    def reclaim_raw(self, owner_key: str) -> int:
        """第 1 段卸压：清掉**已提炼的原话**，返回条数。

        为什么这一段排第一：它是唯一零信息损失的动作 —— 正文还在，原话只是
        "提炼前长什么样"的备份。用户要追溯时才看，看不到了也不影响任何回答。
        """
        n = 0
        with self._lock:
            for r in self._all_rows(owner_key):
                if r.get("raw_content"):
                    r["raw_content"] = None
                    n += 1
        return n

    def pick_longest(self, owner_key: str, *, min_chars: int) -> dict | None:
        """第 2 段卸压的候选：**当前正文最长、且长到值得压**的那条。

        只挑正文，不看原话 —— 压的是"要注入的那句"，原话压在旁边没意义。
        ``min_chars`` 是"再短就没必要压"的门槛（复用提炼的长度线）。
        """
        with self._lock:
            rows = [r for r in self._injectable(owner_key)
                    if len(r["content"]) >= max(1, int(min_chars))]
            if not rows:
                return None
            return dict(max(rows, key=lambda r: len(r["content"])))

    def replace_content(self, owner_key: str, memory_id: str, content: str, *,
                        raw_content: str = "") -> bool:
        """把一条的正文换成压缩后的版本，原话挪进 ``raw_content``。

        ⚠️ 已有的 ``raw_content`` **不覆盖**：那是更早、更完整的原话，
        压第二次时用第二次的输入去覆盖它，等于把最早的版本弄丢了。
        """
        with self._lock:
            for r in self._all_rows(owner_key):
                if r["id"] != memory_id:
                    continue
                if raw_content and not r.get("raw_content"):
                    r["raw_content"] = raw_content
                r["content"] = content
                return True
        return False

    def prune(self, owner_key: str, *, keep: int | None = None,
              max_chars: int | None = None) -> int:
        """**第 3 段卸压：按分数物理删除**，返回删掉的条数。

        走到这一步说明前两段（回收原话、压缩长记忆）都没把容量降下来，剩下的
        只能丢。判据是 ``rank_memories`` 的尾部 —— 与取回**共用同一份分数**
        （频率 0.45 / 重要性 0.35 / 衰减 0.20），没有类别豁免（用户 2026-11-03
        撤回：豁免是给整类开后门，同类里的宝贝和垃圾同命）。

        两条硬规则：``pinned`` 不删（用户显式钉住的，不是我的推断）；
        7 天内新建的不删（还没被任何一次检索碰过，砍它等于砍"没机会"）。

        为什么不再标 ``stale_at``：标了之后读取侧会跳过，行为上与删掉完全一致，
        而表只增不减、备份越来越大 —— 那是"假装保留"。列留着只为兼容老数据。
        """
        limit = max(1, int(keep if keep is not None else max_live_per_owner()))
        char_limit = int(max_chars if max_chars is not None
                         else max_chars_per_owner())
        now = datetime.now(UTC)
        with self._lock:
            rows = self._all_rows(owner_key)
            total = sum(len(r["content"]) + len(r.get("raw_content") or "") for r in rows)
            if len(rows) <= limit and total <= char_limit:
                return 0
            victims: set[str] = set()
            for row in reversed(rank_memories(rows, now=now)):
                if len(rows) - len(victims) <= limit and total <= char_limit:
                    break
                if _is_pinned(row) or row["id"] in victims:
                    continue
                created = _as_dt(row.get("created_at"))
                if created and (now - created).days < _PROTECT_DAYS:
                    continue
                victims.add(row["id"])
                total -= len(row["content"]) + len(row.get("raw_content") or "")
            if not victims:
                return 0
            self._rows = [r for r in self._rows if r["id"] not in victims]
        return len(victims)

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
            for r in self._current(owner_key):
                if r.get("batch_id") == batch_id:
                    r["invalid_at"], r["supersede_reason"] = now, "batch_revoked"
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

    #: 取行时统一带上的列。**两个查询共用一份**：少了半列的后果是"内存版算得出
    #: 分数、PG 版算出来不一样"，而两边各自看起来都自洽。
    # ⚠️ 新增列一律**追加在末尾**：``_row_from`` 按下标取值，中间插一个会把后面
    # 每一列都错位，而错位后字段类型还能对得上（都是 text/timestamptz），不报错。
    _COLS = ("id, kind, content, raw_content, pinned, use_count, hit_count,"
             " created_at, last_used_at, invalid_at, supersede_reason, valid_from")

    def _rows_where(self, owner_key: str, where: str,
                    params: tuple = ()) -> list[dict]:
        """按条件取行，统一在 Python 里排序（分数不进 SQL，避免两份公式分叉）。"""
        if not self.enabled or not owner_key:
            return []
        import psycopg

        sql = (f"SELECT {self._COLS} FROM agent_memories"
               f" WHERE owner_key = %s AND ({where})"
               " ORDER BY created_at DESC LIMIT %s")
        with psycopg.connect(self.dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (owner_key, *params, _SCAN_LIMIT))
                return [self._row_from(r) for r in cur.fetchall()]

    @staticmethod
    def _row_from(r: tuple) -> dict:
        """按 ``_COLS`` 的**顺序**拼字典 —— 改 ``_COLS`` 就必须同步改这里。"""
        return {"id": r[0], "kind": r[1], "content": r[2], "raw_content": r[3],
                "pinned": r[4], "use_count": r[5], "hit_count": r[6],
                "created_at": r[7], "last_used_at": r[8],
                "invalid_at": r[9], "supersede_reason": r[10], "valid_from": r[11]}

    def _injectable_rows(self, owner_key: str) -> list[dict]:
        """能进上下文的行：当前有效的 + 改口留下的旧版本（口径见内存版）。"""
        return self._rows_where(
            owner_key,
            "stale_at IS NULL AND (invalid_at IS NULL OR supersede_reason = 'superseded')")

    def _all_rows(self, owner_key: str) -> list[dict]:
        """该用户的所有行（条数与字数上限的口径）。"""
        return self._rows_where(owner_key, "true")

    def active(self, owner_key: str, *, limit: int = 40) -> list[dict]:
        return rank_memories(self._injectable_rows(owner_key))[:max(1, int(limit))]

    def remember(self, owner_key: str, content: str, *, kind: str = "project",
                 pinned: bool = False, tenant: str = "", source_thread: str = "",
                 supersedes: str = "", source: str = "memory",
                 batch_id: str = "", raw_content: str = "") -> dict:
        text = normalize_content(content)
        if not text:
            return {"ok": False, "reason": "empty"}
        if not self.enabled or not owner_key:
            return {"ok": False, "reason": "unavailable"}
        import psycopg

        raw = str(raw_content or "").strip()
        if raw and raw == text:
            raw = ""   # 没被提炼过 ⇒ 不留第二份
        h = content_hash(owner_key, text)
        new_id = uuid.uuid4().hex
        with psycopg.connect(self.dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                # 同一件事被重复说一遍 ⇒ 只刷新使用痕迹，不产生第二条。
                cur.execute(
                    "SELECT id, raw_content FROM agent_memories"
                    " WHERE owner_key = %s AND content_hash = %s AND invalid_at IS NULL"
                    " LIMIT 1", (owner_key, h))
                row = cur.fetchone()
                if row:
                    cur.execute(
                        "UPDATE agent_memories"
                        "   SET use_count = use_count + 1, last_used_at = now(),"
                        "       raw_content = COALESCE(raw_content, %s)"
                        " WHERE id = %s", (raw or None, row[0]))
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
                    " (id, owner_key, tenant, kind, content, raw_content, content_hash,"
                    "  source_thread, source, batch_id, pinned, superseded_by)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)"
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
                     text, raw or None, h, source_thread, source, batch_id,
                     bool(pinned)))
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
        """``hit_count += 1`` 并刷新 ``last_used_at``（语义见内存版）。"""
        if not self.enabled or not owner_key or not ids:
            return 0
        import psycopg

        with psycopg.connect(self.dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE agent_memories"
                    "   SET hit_count = hit_count + 1, last_used_at = now()"
                    " WHERE owner_key = %s AND id = ANY(%s)"
                    # 旧版本也会被注入（它们是"以前是什么"的历史），所以频率
                    # 也要给它们记 —— 只认 invalid_at IS NULL 等于把历史版本
                    # 的分数永久钉死在零，它们会先被删，而删掉就再也答不出
                    # "以前喜欢什么"。
                    "   AND (invalid_at IS NULL OR supersede_reason = 'superseded')",
                    (owner_key, list(ids)))
                return cur.rowcount or 0

    def measure(self, owner_key: str) -> dict:
        """当前容量（一条 SQL 拿条数与总字数，含 ``raw_content``）。"""
        if not self.enabled or not owner_key:
            return {"rows": 0, "chars": 0}
        import psycopg

        with psycopg.connect(self.dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*),"
                    "       COALESCE(sum(length(content)"
                    "                    + length(COALESCE(raw_content, ''))), 0)"
                    "  FROM agent_memories WHERE owner_key = %s", (owner_key,))
                rows, chars = cur.fetchone()
        return {"rows": int(rows or 0), "chars": int(chars or 0)}

    def reclaim_raw(self, owner_key: str) -> int:
        """第 1 段卸压：清掉已提炼的原话（语义见内存版）。"""
        if not self.enabled or not owner_key:
            return 0
        import psycopg

        with psycopg.connect(self.dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE agent_memories SET raw_content = NULL"
                    " WHERE owner_key = %s AND raw_content IS NOT NULL", (owner_key,))
                return cur.rowcount or 0

    def pick_longest(self, owner_key: str, *, min_chars: int) -> dict | None:
        """第 2 段的候选：当前正文最长且长到值得压的那条（语义见内存版）。"""
        if not self.enabled or not owner_key:
            return None
        import psycopg

        with psycopg.connect(self.dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {self._COLS} FROM agent_memories"
                    " WHERE owner_key = %s AND invalid_at IS NULL AND stale_at IS NULL"
                    "   AND length(content) >= %s"
                    " ORDER BY length(content) DESC, id LIMIT 1",
                    (owner_key, max(1, int(min_chars))))
                row = cur.fetchone()
        return self._row_from(row) if row else None

    def replace_content(self, owner_key: str, memory_id: str, content: str, *,
                        raw_content: str = "") -> bool:
        """换上压缩后的正文，原话挪进 ``raw_content``（已有原话不覆盖，语义见内存版）。"""
        if not self.enabled or not owner_key or not memory_id:
            return False
        import psycopg

        with psycopg.connect(self.dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE agent_memories"
                    "   SET content = %s,"
                    "       raw_content = COALESCE(raw_content, %s)"
                    " WHERE owner_key = %s AND id = %s",
                    (content, raw_content or None, owner_key, memory_id))
                return (cur.rowcount or 0) > 0

    def prune(self, owner_key: str, *, keep: int | None = None,
              max_chars: int | None = None) -> int:
        """第 3 段卸压：按分数**物理删除**（语义与两条硬规则见内存版）。"""
        limit = max(1, int(keep if keep is not None else max_live_per_owner()))
        char_limit = int(max_chars if max_chars is not None
                         else max_chars_per_owner())
        now = datetime.now(UTC)
        rows = self._all_rows(owner_key)
        total = sum(len(r["content"]) + len(r.get("raw_content") or "") for r in rows)
        if len(rows) <= limit and total <= char_limit:
            return 0
        victims: set[str] = set()
        for row in reversed(rank_memories(rows, now=now)):
            if len(rows) - len(victims) <= limit and total <= char_limit:
                break
            if _is_pinned(row) or row["id"] in victims:
                continue
            created = _as_dt(row.get("created_at"))
            if created and (now - created).days < _PROTECT_DAYS:
                continue
            victims.add(row["id"])
            total -= len(row["content"]) + len(row.get("raw_content") or "")
        if not victims:
            return 0
        import psycopg

        with psycopg.connect(self.dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM agent_memories WHERE owner_key = %s AND id = ANY(%s)",
                    (owner_key, list(victims)))
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
