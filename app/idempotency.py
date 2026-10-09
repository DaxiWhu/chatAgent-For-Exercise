"""幂等键：客户端重试不该重复生成、重复计费。

依据
----
语义**逐条**对着 IETF 草案 `draft-ietf-httpapi-idempotency-key-header-07` 实现
（该草案已 expired，但它仍是这件事唯一的正式文本）。规范正文发布在
`deploy/IDEMPOTENCY.md` —— 标准 2.5.2 要求资源方**必须**发布它。

问题在哪
--------
客户端超时重试是常态（弱网、网关超时、用户切后台）。但 `/v1/chat` 是 SSE ——
重试的代价不是"多读一次"，而是**重复生成一整轮回答**：

  - 重复计费：token 真的被消耗了，账单上是实打实的两倍；
  - 同一 `thread_id` 被追加两条相同用户消息，图多跑一轮，历史里出现重复；
  - 若结果停在审核态，`review_queue` 里可能出现两条内容相同的待审项，
    人工要处理两遍（而人无法判断这是"两次不同的请求"还是"一次请求被重试"）。

状态机（对应标准 2.6 / 2.7）
---------------------------
    （无键）           → 行为完全不变，不做任何去重
    NEW                → 首次见到（键与指纹都没见过）⇒ 正常执行（2.6）
    DONE               → 上一次**已完成** ⇒ **重放上一次的结果**（2.6 Retry）
    IN_PROGRESS        → 上一次**还在跑** ⇒ 409（2.6 Concurrent Request）
    MISMATCH           → 同一个键换了载荷 ⇒ 422（2.7）

"重放"而不是"回 409 指路"（2026-10-03 起）
----------------------------------------
旧实现的语义是"同键一律 409 + 指路 `GET /v1/sessions/{thread_id}`"，理由是
"SSE 是已经流出去的流，无法回放"。**后半句是对的，前半句因此不成立** ——
流没法"重新流一遍"，但**事件的副本可以留下来**：`app/main.py` 在下发每个事件
之前把 `(事件名, 数据)` 登记进这里，重试时照着登记表再放一遍即可。
标准 2.6 对"已完成后的重试"要求的就是这个（SHOULD respond with the result of
the previously completed operation），所以现在照做。

代价必须说清楚：每个键要缓存整段回答（默认上限 `IDEMPOTENCY_MAX_BODY_BYTES`，
256 KiB）。超过上限时只记"已完成"、不留正文 —— 那种情况下重试回 409 并指路
（标准用的是 SHOULD，此处是**已公布的例外**，见 `deploy/IDEMPOTENCY.md`）。

请求指纹（标准 2.4）
------------------
"同一个键必须对应同一个请求载荷"（2.2 的 MUST NOT）。判断依据是 **SHA-256
指纹**，由请求体规范化 JSON 后算出（标准 2.4 列举的第一种做法：整个请求载荷的
校验和）。没有它，客户端拿同一个键问两个不同的问题时，第二个会被静默当成
重复拒掉 —— 那是功能损坏，比重复计费更糟。

⚠️ 键的语义是「**这一次请求**」，不是「这个问题」：用户确实想再问一遍同一个
问题时，客户端应当换一个新键。所以去重**只能**靠客户端给的键 + 载荷指纹，
绝不用"消息内容像不像"去推断。

失败与未完成**不占用**键
----------------------
只有**成功**的请求才 `finish`（标记已完成 + 留下正文）。失败、断连、被配额
拒绝时一律 `forget`，客户端用**同一个键**重试即可。不分这两种情况的话，
一次偶发失败会让那个键在 TTL 内彻底不可用 —— 客户端的重试逻辑完全正确，
却始终收到 409。

（标准 2.6 说重试也可以重放"上一次的错误"，本项目**刻意不这么做**：上游偶发
失败的语义是"这次没做成"，把它缓存下来会让客户端在 TTL 内再也拿不到结果。
这条偏离已写进 `deploy/IDEMPOTENCY.md`。）

键的保留时长必须**明显长于**客户端的重试窗口：键一过期，重试就不再被拦，
保护凭空消失（且不会有任何报错）。这也是 `IDEMPOTENCY_TTL_SECONDS` 默认取
10 分钟的原因 —— 它要盖住"网关 30s 超时 + 客户端指数退避重试几次"的整个过程。

多副本
------
配了 `REDIS_URL` 时用 Redis 的 `SET NX EX` 做原子抢占（跨副本有效）；
否则退化为进程内 —— **多副本下各自独立，重试落到另一个副本仍会重复执行**。
启动日志的 `idempotency.backend` 会如实回显是 redis 还是 memory。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from typing import Any, NamedTuple

from . import config

#: `begin()` 的四种状态
NEW = "new"
IN_PROGRESS = "in_progress"
MISMATCH = "mismatch"
DONE = "done"

#: 键的合法字符：可打印 ASCII（0x21–0x7E），**且不含引号与反斜杠**。
#: 为什么这么定（标准 §5 的 "Injection attacks" / "Data leaks" 两条）：
#:   - 键会被拼进缓存键（本项目用 `\x1f` 做租户分隔符）⇒ 必须排除控制字符，
#:     否则客户端塞一个 `\x1f` 就能伪造出别的租户的键；
#:   - 键会进日志 ⇒ 排除换行/控制字符，防日志注入；
#:   - 引号/反斜杠是 RFC 8941 字符串的转义字符，本项目不做反转义 ⇒ 直接拒。
_KEY_RE = re.compile(r"^[!#-\[\]-~]+$")


class InvalidKey(ValueError):
    """`Idempotency-Key` 不符合公布的格式 ⇒ 调用方应回 400。"""


class BeginResult(NamedTuple):
    """`begin()` 的返回值。`payload` 只在 `DONE` 且缓存了正文时非空。"""

    status: str
    payload: str | None = None


def normalize_key(raw: str, *, max_chars: int | None = None) -> str:
    """把请求头里的原始值规范化成内部键；**空串表示"客户端没带"**。

    为什么要剥一层引号：标准 2.1 规定 `Idempotency-Key` 是 Structured Header
    （RFC 8941），其 String 类型**必须带引号**（草案 §6 的示例就是
    `Idempotency-Key: "8e03978e-..."`）。但现实里绝大多数客户端不带引号发，
    两种都要收。这里只处理"整体被一对引号包住"这一种情形。

    非法时抛 `InvalidKey`（调用方转 400），而不是"悄悄当成没带"——
    后者会让一个拼错的键静默失去全部保护。
    """
    v = (raw or "").strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1]
    if not v:
        return ""
    limit = config.IDEMPOTENCY_MAX_KEY_CHARS if max_chars is None else max_chars
    if len(v) > limit:
        raise InvalidKey(f"键超过 {limit} 字符")
    if not _KEY_RE.match(v):
        raise InvalidKey("键只能由可打印 ASCII 组成（不含引号与反斜杠）")
    return v


def fingerprint(payload: Any) -> str:
    """按标准 2.4 生成请求指纹：规范化 JSON 的 SHA-256。

    `sort_keys=True` 是**必须的**：客户端重试时字段顺序可能变（不同语言的
    JSON 序列化顺序不同），顺序一变指纹就变 ⇒ 会被误判成"同键不同载荷"而回 422。
    """
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class _MemoryStore:
    """进程内实现：有界字典 + TTL。

    值 = `(状态, 指纹, 正文, 过期时刻)`。
    """

    def __init__(self, max_entries: int, sweep_interval: float = 60.0):
        self._mem: dict[str, tuple[str, str, str | None, float]] = {}
        self._max = max_entries
        self._lock = threading.Lock()
        self._next_sweep = 0.0
        self._sweep_interval = sweep_interval
        self._overflow_warned = False

    def _sweep_locked(self, now: float) -> None:
        """摊还清理：最多每 `_sweep_interval` 秒扫一次。

        每个请求都全表扫是 O(活跃键数)，而键由客户端提供（可被刷）——
        那会把幂等检查本身变成放大面。与限流器同款取舍。
        """
        if now < self._next_sweep:
            return
        self._next_sweep = now + self._sweep_interval
        dead = [k for k, (_, _, _, exp) in self._mem.items() if exp <= now]
        for k in dead:
            self._mem.pop(k, None)

    def claim(self, key: str, fp: str, ttl: int) -> tuple[str, str | None]:
        now = time.time()
        with self._lock:
            self._sweep_locked(now)
            ent = self._mem.get(key)
            if ent is not None and ent[3] > now:
                state, fp0, payload, _ = ent
                if fp0 != fp:
                    return MISMATCH, None
                return (DONE, payload) if state == DONE else (IN_PROGRESS, None)
            if len(self._mem) >= self._max:
                # 满了就**不再登记新键**（与 SessionRegistry 同策略）：
                # 此时退化为"best-effort 去重"，但不阻断服务。
                if not self._overflow_warned:
                    self._overflow_warned = True
                    logging.warning(
                        "幂等键表已达上限 %d，停止登记新键（已登记的仍生效）；"
                        "多副本或大批量场景请配置 REDIS_URL",
                        self._max,
                    )
                return NEW, None
            self._mem[key] = (IN_PROGRESS, fp, None, now + ttl)
            return NEW, None

    def mark_done(self, key: str, fp: str, payload: str | None, ttl: int) -> bool:
        """标记已完成。返回"是否写成功"。

        ⚠️ **只在条目还在时才写**（不新建）：上限那一条路径"拒绝登记新键"，
        若这里无条件新建，键表就会在每次溢出请求后悄悄长大 —— 上限形同虚设。
        代价是"生成耗时超过 TTL"时不会留下完成记录（那种情况下重试重跑，
        是可接受的退化，且**偏安全**：宁可多跑一次，也不要有界缓存失效）。
        """
        with self._lock:
            ent = self._mem.get(key)
            if ent is None or ent[3] <= time.time():
                return False
            self._mem[key] = (DONE, fp, payload, time.time() + ttl)
            return True

    def forget(self, key: str) -> None:
        with self._lock:
            self._mem.pop(key, None)

    def count(self) -> int:
        with self._lock:
            return len(self._mem)


class _RedisStore:
    """Redis 实现：`SET NX EX` 是原子的"抢占"，天然表达首次/重复。"""

    def __init__(self, url: str):
        import redis  # 延迟导入：未装则抛 ImportError 由调用方降级

        # ⚠️ `from_url` 是**懒连接**，不建连 —— 不 ping 就不知道地址是否可达，
        # 于是故障会在第一次真实请求上才暴露（本项目的既有教训）。
        self._c = redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        self._c.ping()

    @staticmethod
    def _decode(raw) -> dict | None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        if not raw:
            return None
        try:
            rec = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return rec if isinstance(rec, dict) else None

    def claim(self, key: str, fp: str, ttl: int) -> tuple[str, str | None]:
        initial = json.dumps({"s": IN_PROGRESS, "fp": fp}, separators=(",", ":"))
        # 两次：第一次抢不到、再读又发现键刚好过期时，重试一次抢占；
        # 仍失败才放行（宁可多跑一次，也不要把正常请求误判成重复）。
        for _ in range(2):
            if self._c.set(key, initial, nx=True, ex=ttl):
                return NEW, None
            rec = self._decode(self._c.get(key))
            if rec is None:
                continue
            if rec.get("fp") != fp:
                return MISMATCH, None
            if rec.get("s") == DONE:
                body = rec.get("p")
                return DONE, body if isinstance(body, str) else None
            return IN_PROGRESS, None
        return NEW, None

    def mark_done(self, key: str, fp: str, payload: str | None, ttl: int) -> bool:
        rec = {"s": DONE, "fp": fp}
        if payload is not None:
            rec["p"] = payload
        self._c.set(key, json.dumps(rec, separators=(",", ":")), ex=ttl)
        return True

    def forget(self, key: str) -> None:
        self._c.delete(key)


class IdempotencyStore:
    """幂等键登记表。`begin()` / `finish()` / `forget()` 三者成对使用。"""

    def __init__(self, *, ttl=600, max_entries=10000, dsn="", enabled=True,
                 max_body_bytes=262144):
        self.ttl = max(1, int(ttl))
        self.enabled = bool(enabled)
        self.max_body_bytes = max(1024, int(max_body_bytes))
        self._local = _MemoryStore(max_entries)
        self._shared = None
        self.backend = "memory"
        if (dsn or "").strip():
            try:
                self._shared = _RedisStore(dsn.strip())
                self.backend = "redis"
            except Exception as e:  # noqa: BLE001
                # 构造失败退到内存并在启动期可见（`backend` 会被回显进启动日志）。
                logging.warning("幂等键共享后端不可用，退化为进程内（多副本下会重复执行）：%s", e)
                self._shared = None

    def _key(self, tenant: str, key: str, subject: str = "") -> str:
        """键里必须带租户**和主体** —— 只带租户会漏掉同租户内成员之间的干扰。

        为什么只带租户不够（2026-10-06 修）
        ----------------------------------
        幂等键是**客户端自己生成**的一串字符，服务端不做归属校验。同租户里的
        B 只要猜中/照抄 A 用过的键，就有两条不需要任何越权凭证的路径：

          · A 的请求还在跑时，B 抢先登记 ⇒ A 拿到 `409 正在处理中`
            （同租户里的人可以按住另一个人的请求）；
          · A 已完成时，B 用**同键同载荷**重试 ⇒ 命中 `DONE` ⇒ B 拿到 A 那一次的
            回答正文（`Idempotency-Replayed: true`）。这是**跨用户的响应体泄露**，
            而载荷里只有 thread_id + message，猜中的门槛并不高。

        这正是标准 §5 建议的 "composite key"（键 + 只有资源方知道的客户端属性）。
        这里取「租户 + 用户 + 键」，与配额分桶、会话归属的口径保持一致 ——
        那两处早就收紧到 (tenant, user)，幂等键是当时漏掉的一处（同租户成员互相
        干扰对方，隔离边界不齐）。

        ⚠️ `subject` 为空串时**保持历史键形态**（只带租户），而不是补一个空段。
        这是刻意的：历史键与带主体的键落在**不同命名空间**、互不命中 ——
        若让空主体与"主体恰好是空串的调用方"共用一把键，等于把刚收回来的隔离
        又还回去。也是这条规则让 `tests/test_idempotency.py` 里那批三参数的
        存储层断言保持原样有效。
        """
        if subject:
            return f"idem:{tenant or '-'}\x1f{subject}\x1f{key}"
        return f"idem:{tenant or '-'}\x1f{key}"

    def begin(self, tenant: str, key: str, fp: str, *, subject: str = "") -> BeginResult:
        """登记/查询。返回 `BeginResult(status, payload)`。

        存储故障一律返回 `NEW`（放行）：幂等是"尽力而为"的额外保护，
        不该因为 Redis 抖动把正常请求打死 —— 与限流/配额的降级取向一致。

        subject: 用户主体（见 `_key()`）。⚠️ `begin` / `finish` / `forget` 三处
        必须传**同一个** subject，否则抢占落在一个命名空间、收尾落在另一个 ——
        键永远停在"在途"，客户端重试被自己的 409 挡到 TTL 到期（假死）。
        """
        if not self.enabled or not key:
            return BeginResult(NEW, None)
        k = self._key(tenant, key, subject)
        try:
            if self._shared is not None:
                status, payload = self._shared.claim(k, fp, self.ttl)
            else:
                status, payload = self._local.claim(k, fp, self.ttl)
        except Exception as e:  # noqa: BLE001
            logging.warning("幂等键查询失败，本次放行：%s", e)
            return BeginResult(NEW, None)
        return BeginResult(status, payload)

    def finish(self, tenant: str, key: str, fp: str, payload: str | None, *,
               subject: str = "") -> bool:
        """把键标记为已完成，并尽力留下可重放的正文。

        返回"正文是否真的被缓存下来了" —— 没缓存（超限或写入失败）时调用方
        要记一条 `idempotency_truncated_total`，因为此后同键重试会回 409 指路
        而不是重放（**对客户端可见的行为差异**，不能只在日志里）。

        ⚠️ 不能在这里删键：删掉之后重试会被当成首次请求重新执行，
        而重试往往正好发生在首次完成之后 —— 那是最需要拦住的那一刻。
        """
        if not self.enabled or not key:
            return False
        stored = payload
        if payload is not None and len(payload.encode("utf-8")) > self.max_body_bytes:
            logging.warning("幂等重放正文超过 %d 字节，只记完成、不留正文",
                            self.max_body_bytes)
            stored = None
        k = self._key(tenant, key, subject)
        try:
            if self._shared is not None:
                ok = self._shared.mark_done(k, fp, stored, self.ttl)
            else:
                ok = self._local.mark_done(k, fp, stored, self.ttl)
        except Exception as e:  # noqa: BLE001
            logging.warning("幂等键置完成失败（键会按 TTL 自然过期）：%s", e)
            return False
        if not ok:
            logging.info("幂等键已完成但条目已过期，未登记：%s", key)
        return bool(ok and stored is not None)

    def forget(self, tenant: str, key: str, *, subject: str = "") -> None:
        """撤销键，允许客户端用**同一个键**重试。

        什么时候必须撤销：请求**失败**或**未完成**（上游异常、客户端中途断开、
        被配额挡下）。不撤销的话，键会一直停在 `IN_PROGRESS` 直到 TTL 到期，
        客户端拿着正确的重试逻辑却始终被 409 挡住 —— 一次偶发失败
        就变成了"十分钟内这个键彻底不可用"。

        换句话说：**只有成功的请求才"占用"这个键**。

        subject: 必须与 `begin()` / `finish()` 用同一个值，否则会**撤销不掉** ——
        查到的是另一个命名空间里的键（不存在），于是原键留在"在途"直到 TTL。
        """
        if not self.enabled or not key:
            return
        k = self._key(tenant, key, subject)
        try:
            if self._shared is not None:
                self._shared.forget(k)
            else:
                self._local.forget(k)
        except Exception as e:  # noqa: BLE001
            logging.warning("幂等键撤销失败（键会按 TTL 自然过期）：%s", e)

    def count(self) -> int:
        """进程内已登记的键数（排障用；Redis 后端不适用）。"""
        return self._local.count()


def build_idempotency_store() -> IdempotencyStore:
    """按 app.config 造幂等键表。"""
    return IdempotencyStore(
        ttl=config.IDEMPOTENCY_TTL_SECONDS,
        max_entries=config.IDEMPOTENCY_MAX_ENTRIES,
        dsn=config.REDIS_URL,
        enabled=config.IDEMPOTENCY_ENABLED,
        max_body_bytes=config.IDEMPOTENCY_MAX_BODY_BYTES,
    )
