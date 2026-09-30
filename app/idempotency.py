"""幂等键：客户端重试不该重复生成、重复计费。

问题在哪
--------
客户端超时重试是常态（弱网、网关超时、用户切后台）。但 `/v1/chat` 是 SSE ——
重试的代价不是"多读一次"，而是**重复生成一整轮回答**：

  - 重复计费：token 真的被消耗了，账单上是实打实的两倍；
  - 同一 `thread_id` 被追加两条相同用户消息，图多跑一轮，历史里出现重复；
  - 若结果停在审核态，`review_queue` 里可能出现两条内容相同的待审项，
    人工要处理两遍（而人无法判断这是"两次不同的请求"还是"一次请求被重试"）。

为什么是"拒绝重跑"而不是"返回首次结果"
------------------------------------
SSE 的响应是一条已经流出去的流，**无法回放**。要做到"重试拿到与首次相同的
响应体"，就得把整段输出缓存在服务端再重放一遍 —— 那既不省 token（首轮已经花了），
又引入一份需要 TTL 与并发控制的缓存。

所以这里采用更直接的语义：**同一键的重试不执行，回 409 并指路**。
客户端被告知去 `GET /v1/sessions/{thread_id}` 取既有状态 —— 该端点本来就在
（断点续传用的），它读的是检查点，是"这次请求已经进行到哪了"的权威答案。

状态机
------
    （无键）        → 行为完全不变，不做任何去重
    NEW             → 首次见到，正常执行；结束时置 DONE
    IN_PROGRESS     → 同一键正在处理中 ⇒ 409
    DONE            → 同一键已完成 ⇒ 409（指路 /v1/sessions）

键的保留时长必须**明显长于**客户端的重试窗口：键一过期，重试就不再被拦，
保护凭空消失（且不会有任何报错）。这也是 `IDEMPOTENCY_TTL_SECONDS` 默认取
10 分钟的原因 —— 它要盖住"网关 30s 超时 + 客户端指数退避重试几次"的整个过程。

键的语义是「**这一次请求**」，不是「这个问题」
--------------------------------------------
用户确实想再问一遍同一个问题时，客户端应当换一个新键。若服务端按"消息内容"
去重，就会把这种正常行为误判成重试 —— 那是**功能损坏**，比重复计费更糟。
所以去重只能靠客户端给的键，绝不用内容推断。

多副本
------
配了 `REDIS_URL` 时用 Redis 的 `SET NX EX` 做原子抢占（跨副本有效）；
否则退化为进程内计数 —— **多副本下各自独立，重试落到另一个副本仍会重复执行**。
启动日志的 `idempotency.backend` 会如实回显是 redis 还是 memory。
"""

from __future__ import annotations

import logging
import threading
import time

from . import config

#: `begin()` 的三种返回值
NEW = "new"
IN_PROGRESS = "in_progress"
DONE = "done"


class _MemoryStore:
    """进程内实现：有界字典 + TTL。"""

    def __init__(self, max_entries: int, sweep_interval: float = 60.0):
        self._mem: dict[str, tuple[str, float]] = {}
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
        dead = [k for k, (_, exp) in self._mem.items() if exp <= now]
        for k in dead:
            self._mem.pop(k, None)

    def claim(self, key: str, ttl: int) -> str:
        now = time.time()
        with self._lock:
            self._sweep_locked(now)
            ent = self._mem.get(key)
            if ent is not None and ent[1] > now:
                return DONE if ent[0] == DONE else IN_PROGRESS
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
                return NEW
            self._mem[key] = (IN_PROGRESS, now + ttl)
            return NEW

    def mark_done(self, key: str, ttl: int) -> None:
        with self._lock:
            self._mem[key] = (DONE, time.time() + ttl)

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

    def claim(self, key: str, ttl: int) -> str:
        if self._c.set(key, IN_PROGRESS, nx=True, ex=ttl):
            return NEW
        val = self._c.get(key)
        if isinstance(val, bytes):
            val = val.decode("utf-8", "replace")
        return DONE if val == DONE else IN_PROGRESS

    def mark_done(self, key: str, ttl: int) -> None:
        self._c.set(key, DONE, ex=ttl)

    def forget(self, key: str) -> None:
        self._c.delete(key)


class IdempotencyStore:
    """幂等键登记表。`begin()` / `finish()` 成对使用。"""

    def __init__(self, *, ttl=600, max_entries=10000, dsn=""):
        self.ttl = max(1, int(ttl))
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

    def _key(self, tenant: str, key: str) -> str:
        """键里必须带租户 —— 否则两个租户用同一个客户端生成的键会互相干扰。

        例如都用 "req-1"：A 租户发起后，B 租户的同名请求会被判成"重复"而被拒，
        即一个租户能**阻断另一个租户**的请求。
        """
        return f"idem:{tenant or '-'}\x1f{key}"

    def begin(self, tenant: str, key: str) -> str:
        """登记/查询。返回 `NEW` / `IN_PROGRESS` / `DONE`。

        存储故障一律返回 `NEW`（放行）：幂等是"尽力而为"的额外保护，
        不该因为 Redis 抖动把正常请求打死 —— 与限流/配额的降级取向一致。
        """
        if not key:
            return NEW
        k = self._key(tenant, key)
        if self._shared is not None:
            try:
                return self._shared.claim(k, self.ttl)
            except Exception as e:  # noqa: BLE001
                logging.warning("幂等键查询失败，本次放行：%s", e)
                return NEW
        return self._local.claim(k, self.ttl)

    def finish(self, tenant: str, key: str) -> None:
        """把键标记为已完成（**保留到 TTL 到期**，这样重试能认出"已完成"）。

        ⚠️ 不能在这里删键：删掉之后重试会被当成首次请求重新执行，
        而重试往往正好发生在首次完成之后 —— 那是最需要拦住的那一刻。
        """
        if not key:
            return
        k = self._key(tenant, key)
        try:
            if self._shared is not None:
                self._shared.mark_done(k, self.ttl)
            else:
                self._local.mark_done(k, self.ttl)
        except Exception as e:  # noqa: BLE001
            logging.warning("幂等键置完成失败（键会按 TTL 自然过期）：%s", e)

    def forget(self, tenant: str, key: str) -> None:
        """撤销键，允许客户端用**同一个键**重试。

        什么时候必须撤销：请求**失败**或**未完成**（上游异常、客户端中途断开）。
        此时不撤销的话，键会一直停在 `IN_PROGRESS` 直到 TTL 到期，
        客户端拿着正确的重试逻辑却始终被 409 挡住 —— 一次偶发失败
        就变成了"十分钟内这个键彻底不可用"。

        换句话说：**只有成功的请求才"占用"这个键**。这也是通用的幂等语义
        （失败不缓存结果，允许重试；成功则重试返回同一结果）。
        """
        if not key:
            return
        k = self._key(tenant, key)
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
    )
