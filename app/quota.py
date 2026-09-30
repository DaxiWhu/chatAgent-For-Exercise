"""用量配额：单请求 token 预算 + 用户（租户）日配额。

为什么要有这一层
--------------
限流管的是"请求次数"，管不住"单次请求烧多少 token" —— 一次超长输入 + 多轮
self-review 迭代，可以在一次请求里把当日 LLM 额度打掉一大半。所以生产环境
需要两个独立的闸门：

1. **单请求预算**（`LLM_MAX_TOKENS_PER_REQUEST`）：流式回答过程中累计 token，
   超了就**停止继续生成**（已产出的部分照常返回）。防"一次请求失控"。
2. **用户日配额**（`USER_DAILY_TOKEN_QUOTA`）：按租户累计当日用量，请求进来先查，
   超了直接 429。防"一个用户/租户把全站额度吃光"。

计数口径
-------
用 token 数而非字数。有 `tiktoken` 就用它的分词器精确数；没有则退化用
`max(1, len(text))` —— **对中文这是偏保守（高估）的估计**，宁可早一点刹住，
也不要因为低估而超支。两种口径都写清楚，便于事后对账。

存储
----
- 日配额：内存 dict（`{tenant: {date: tokens}}`）；配了 REDIS_URL 则用
  `INCRBY` + 到当天结束过期，多实例共享。
- 单请求预算：天然是请求内的，无需外部存储。

共享后端的两条硬要求（都是"配了却不生效"的静默故障，2026-09-29 补齐）
-------------------------------------------------------------------
1. **构造期必须 ping**：`redis.Redis.from_url` 是懒连接，不 ping 的话
   "地址写错/服务没起"要到第一个真实请求才暴露，而那时异常已被吞掉。
2. **运行期出错退到进程内计数，而不是放行**：见 `_fallback` 的说明。

配额的两阶段协议：claim → settle（2026-09-29）
-------------------------------------------
**判定与记账必须是同一次原子操作**。原来的写法是请求开头 `check()`、请求结束
`add()`，中间隔着整次生成（几秒到几十秒）—— 并发的 N 个请求会各自读到同一个
"还没超"的旧值然后一起放行，最后各自 `add`，日额度被超掉 N 份。这不是小概率
竞态：它只在额度快用完时发生，而那恰是额度最需要被守住的时候。

现在改为"先占后判"：`claim()` 把本次请求**最多可能消耗**的 token 一次性原子
加进去再判越界（越界则退回），`settle()` 在请求结束时把预占换成实际值。详见
`QuotaManager.claim`。

**默认限额为 0 表示不限制**，所以这一层开着也不会改变既有行为。
"""

import datetime
import logging
import threading
import time
from dataclasses import dataclass

from . import metrics

try:                                    # tiktoken 是可选依赖（requirements 里有，但允许缺失）
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:                       # noqa: BLE001
    _ENC = None


def estimate_tokens(text: str) -> int:
    """估算一段文本的 token 数。

    text: 任意文本，None/空串返回 0。

    优先用 tiktoken 精确分词；不可用时用 `len(text)`（中文场景偏保守的高估）。
    """
    if not text:
        return 0
    if _ENC is not None:
        try:
            return len(_ENC.encode(text))
        except Exception:  # noqa: BLE001
            pass
    return max(1, len(text))


@dataclass
class QuotaStatus:
    """配额检查结果。

    allowed: 是否允许本次请求。
    used: 今日已用 token（**不含本次预占**）。
    limit: 日配额上限（0 = 不限）。
    reset_after: 距离配额重置（次日零点）还有多少秒。
    tenant: 租户标识。
    reserved: 本次请求已经**预占**的 token 数（`claim` 成功时 >0，供结算用）。
    """

    allowed: bool
    used: int
    limit: int
    reset_after: int
    tenant: str = ""
    reserved: int = 0


class _MemoryStore:
    """进程内日用量：{tenant: (date, tokens)}。"""

    def __init__(self):
        self._data: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()

    def get(self, tenant: str, today: str) -> int:
        """读今日已用量；跨天自动归零。"""
        with self._lock:
            d, v = self._data.get(tenant, (today, 0))
            return v if d == today else 0

    def add(self, tenant: str, today: str, n: int) -> int:
        """累加今日用量并返回新值。

        `n` 允许为负（结算时要把多预占的部分退回来）。结果**下限截到 0**：
        预占发生在昨天、结算落在今天时，退还会打到今天的键上，不截断的话今天的
        用量会变成负数 —— 表现成"跨天时白送一段额度"。
        """
        with self._lock:
            d, v = self._data.get(tenant, (today, 0))
            v = max(0, (v if d == today else 0) + int(n))
            self._data[tenant] = (today, v)
            return v


class _RedisStore:
    """Redis 日用量：INCRBY + 到当天结束过期。"""

    def __init__(self, url: str):
        import redis

        self._r = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
        # `from_url` 是懒连接（只造对象不建连），不主动 ping 的话"地址写错/服务没起"
        # 要到第一个真实请求才暴露，而那时异常会被 check/add 吞掉 ⇒
        # 表现成"配额配了 Redis 却完全不算数"。提前到构造期，由上层记 ERROR 并回退。
        self._r.ping()

    @staticmethod
    def _key(tenant: str, today: str) -> str:
        return f"quota:{tenant}:{today}"

    def get(self, tenant: str, today: str) -> int:
        """读今日已用量（键不存在返回 0）。

        redis-py 默认 `decode_responses=False`，拿到的是 bytes；
        `int(b"12")` 在 Python 3 是合法的（已实测），所以这里不需要 decode。

        **下限截到 0**：结算的退还有可能落到"新的一天"的键上（预占在昨天、结算
        在今晨），不截断的话今天的用量会是负数 = 白送一段额度。
        """
        v = self._r.get(self._key(tenant, today))
        return max(0, int(v or 0))

    def add(self, tenant: str, today: str, n: int) -> int:
        """累加并保证键在当天末过期。

        `n` 允许为负：结算时把多预占的部分退回（`settle` 的说明）。返回值同样
        下限截到 0，理由见 `get`。

        过期用**相对 TTL**（`EXPIRE`）而不是绝对时间戳（`EXPIREAT`）：
        后者的时刻由**应用侧时钟**算出，一旦应用与 Redis 的时钟不一致，
        算出的时刻可能已经过去 ⇒ Redis 立刻删键 ⇒ 每次加完马上清零 ⇒
        **配额永远攒不满，静默失效**。这里用 `max(60, ...)` 再兜一层，
        保证任何情况下键至少活 60 秒。
        """
        key = self._key(tenant, today)
        ttl = max(60, _end_of_day_epoch() - int(time.time()))
        with self._r.pipeline() as pipe:
            pipe.incrby(key, int(n))
            pipe.expire(key, ttl)
            return max(0, int(pipe.execute()[0]))


def _today() -> str:
    """今天的日期串（本地时区），作为日配额的天然分桶键。"""
    return datetime.date.today().isoformat()


def _end_of_day_epoch() -> int:
    """次日零点的 unix 时间戳（给 Redis EXPIREAT 用）。"""
    tomorrow = datetime.datetime.combine(
        datetime.date.today() + datetime.timedelta(days=1),
        datetime.time.min,
    )
    return int(tomorrow.timestamp())


class QuotaManager:
    """配额管理器：`check()` 判额度、`add()` 记用量。"""

    def __init__(self, daily_limit=0, request_limit=0, enabled=True, redis_url=""):
        """构造。

        daily_limit: 每租户每日 token 上限，0 = 不限。
        request_limit: 单请求 token 上限，0 = 不限（由调用方流式累计后判定）。
        enabled: False 时 check 恒放行、add 不计数。
        redis_url: 非空则用 Redis 共享日用量，失败回退内存。
        """
        self.daily_limit = max(0, int(daily_limit))
        self.request_limit = max(0, int(request_limit))
        self.enabled = bool(enabled)
        # `_local` 是**兜底表**（与主后端是两个独立实例，语义见 _hit/_fallback 的说明）
        self._local = _MemoryStore()
        self.store = self._local
        self.backend_name = "memory"
        if redis_url:
            try:
                self.store = _RedisStore(redis_url)
                self.backend_name = "redis"
                logging.info("配额计数后端：redis（多实例共享）")
            except ImportError as e:
                logging.error(
                    "REDIS_URL 已配置，但未安装 redis 客户端 ⇒ 共享日配额**未生效**，"
                    "当前用进程内计数：多副本下每个副本各有一份额度。"
                    "请执行 `pip install redis`。原因：%s", e)
            except Exception as e:  # noqa: BLE001
                logging.error(
                    "REDIS_URL 已配置但连接不可用 ⇒ 共享日配额**未生效**，当前用进程内计数："
                    "多副本下每个副本各有一份额度。请检查 REDIS_URL 与网络。原因：%s", e)

        # 预占制下的一个致命错配：单请求上限 ≥ 日配额时，**每一次**请求的预占都装不进
        # 日额度 ⇒ 全部请求被判超额、服务整体 429。这种"配置写错 = 全站不可用"必须
        # 在启动日志里点名，否则排查方向会跑到鉴权/网络上去。
        if self.daily_limit > 0 and self.request_limit >= self.daily_limit:
            logging.warning(
                "单请求上限(%d) 不小于日配额(%d)：任何请求的预占都无法被容纳，"
                "所有请求都会被判 429。请把 LLM_MAX_TOKENS_PER_REQUEST 调到远小于 "
                "USER_DAILY_TOKEN_QUOTA（经验值是 1/20 上下）。",
                self.request_limit, self.daily_limit)

    def _fallback(self, exc: Exception, action: str):
        """共享后端出错时切到进程内计数，并把这件事变得可见。

        为什么不再"直接放行"
        ------------------
        原来 `check` 失败返回 allowed=True、`add` 失败返回 0 —— 合起来就是
        "Redis 一抖，配额彻底失效"。而配额是防"一个租户把全站额度吃光"的唯一闸门，
        在最需要它的时刻（上游故障、流量异常）失效，方向是错的。
        退到本地计数虽然不准（各副本各算），但至少仍会累积并拦截。
        """
        metrics.inc("shared_backend_errors_total",
                    component="quota", backend=self.backend_name)
        logging.error("配额共享后端%s失败，改用进程内计数（多副本下不准）：%s", action, exc)

    def reservation(self) -> int:
        """单次请求要预占的 token 数 —— 即"这一请求最多可能消耗多少"。

        优先用单请求上限（`LLM_MAX_TOKENS_PER_REQUEST`）：它是本次请求消耗的**硬上界**，
        预占它才能保证日配额"不被突破"。

        ⚠️ 没配单请求上限时退回 1：此时请求的消耗**没有上界可预留**，日配额虽然仍是
        原子判定的，但"上界"性质已经不成立（一个请求仍可能吃掉任意多）——
        所以"日配额 + 单请求上限"要一起配，只配前者防不住并发超发。
        """
        return self.request_limit if self.request_limit > 0 else 1

    def _bump(self, tenant: str, n: int) -> int:
        """加/减一次用量；主后端失败时退到进程内计数并留痕。

        n: 正数累加、负数退回。兜底也失败时会把异常抛给调用方，
            由调用方决定是"放行"还是"放弃结算"。
        """
        try:
            return self.store.add(tenant, _today(), n)
        except Exception as e:  # noqa: BLE001
            self._fallback(e, "累加" if n >= 0 else "退回")
            return self._local.add(tenant, _today(), n)

    def claim(self, tenant: str = "") -> QuotaStatus:
        """**原子地**"预占额度 + 判定"。取代"先 check、请求结束再 add"的两步写法。

        tenant: 租户标识（空串视为一个匿名租户）。

        为什么必须合成一步（这一步就是原来的 TOCTOU）
        --------------------------------------------
        旧写法里 `check` 在请求开头、`add` 在请求结尾，中间隔着整次生成（几秒到
        几十秒）。并发的 N 个请求会**各自读到同一个"还没超"的旧值**然后一起放行，
        最后各自 `add` —— 日额度被超掉 N 份。这不是"理论上的小概率竞态"：它只在
        额度快用完时发生，而那正是额度最需要被守住的时候；并发越高超得越多。

        做法是"先占后判"：把本次请求最多可能消耗的 token（`reservation()`）
        **一次原子加**进去，再看是否越界；越界就把它退回来（被拒的请求不该消耗
        额度）。这样"判定"与"记账"是同一次原子操作，不存在中间窗口。

        返回的 `used` 是**不含本次预占**的已用量，便于日志与 429 文案对齐直觉；
        `reserved` 字段给出预占量，请求结束时交给 `settle` 换成实际值。

        并发下的一个副作用（方向是安全的）：两个请求同时越过限额时，可能各自
        看到对方预占后的值而**都**被拒，然后各自退回。结果是"偶发多拒一次"，
        而不是"偶发多放一次" —— 计费闸门宁可偏严。
        """
        reset = max(1, _end_of_day_epoch() - int(time.time()))
        if not self.enabled or self.daily_limit <= 0:
            return QuotaStatus(True, 0, self.daily_limit, reset, tenant)

        reserve = self.reservation()
        try:
            after = self._bump(tenant, reserve)
        except Exception:  # noqa: BLE001  兜底也失败才真正放行（计费故障不该打死服务）
            return QuotaStatus(True, 0, self.daily_limit, reset, tenant)

        used = max(0, after - reserve)
        if after <= self.daily_limit:
            return QuotaStatus(True, used, self.daily_limit, reset, tenant, reserve)

        try:
            self._bump(tenant, -reserve)
        except Exception:  # noqa: BLE001  退回失败只会让额度偏紧，不阻塞请求
            logging.error("配额预占退回失败（额度会偏紧，需人工核对）：tenant=%s", tenant)
        return QuotaStatus(False, used, self.daily_limit, reset, tenant, 0)

    def settle(self, tenant: str, reserved: int, actual: int) -> int:
        """请求结束时把"预占"换成"实际消耗"，返回调整后的今日用量。

        tenant: 租户标识。
        reserved: `claim` 返回的 `reserved`（没走预占则传 0）。
        actual: 本次实际消耗的 token。

        ⚠️ 差额多为**负数**（模型提前结束、断连中止都会让实际小于预占），所以这条路
        必须允许减计数：不允许的话预占永远不退，额度被逐次吃掉 —— 表现成"配额比
        实际消耗快得多地用尽"，而且没有任何报错，只能靠对账发现。
        """
        delta = int(actual) - int(reserved)
        if delta == 0:
            return 0
        return self.add(tenant, delta)

    def check(self, tenant: str = "") -> QuotaStatus:
        """查该租户今日额度是否还够（只读，不做预占）。

        tenant: 租户标识（空串视为一个匿名租户）。

        日配额为 0（不限）或功能关闭时恒放行。存储异常时退到进程内计数；
        进程内计数也异常才真正放行（计费类故障不该把服务直接打死）。

        ⚠️ 只读端点用它可以，**要开始一次生成必须用 `claim`** —— 拿 check 的结果
        去决定"能不能开工"，等于把 TOCTOU 又请回来。
        """
        used, reset = 0, max(1, _end_of_day_epoch() - int(time.time()))
        if not self.enabled or self.daily_limit <= 0:
            return QuotaStatus(True, used, self.daily_limit, reset, tenant)
        try:
            used = self.store.get(tenant, _today())
        except Exception as e:  # noqa: BLE001
            self._fallback(e, "读取")
            try:
                used = self._local.get(tenant, _today())
            except Exception:  # noqa: BLE001
                return QuotaStatus(True, used, self.daily_limit, reset, tenant)
        return QuotaStatus(used < self.daily_limit, used, self.daily_limit, reset, tenant)

    def add(self, tenant: str, tokens: int) -> int:
        """把一次消耗计入该租户今日用量，返回累计值。

        tenant: 租户标识。
        tokens: 增量；>0 累加、<0 退回（`settle` 用）、==0 直接返回当前值。
        """
        if not self.enabled or int(tokens) == 0:
            return 0
        try:
            return self._bump(tenant, int(tokens))
        except Exception:  # noqa: BLE001  兜底也失败才放弃记账
            return 0


class RequestBudget:
    """单请求 token 预算：流式累计，超限即建议停止生成。"""

    def __init__(self, limit=0):
        """limit: 单请求 token 上限，0 = 不限。"""
        self.limit = max(0, int(limit))
        self.used = 0

    def add(self, text: str) -> bool:
        """累加文本的 token 数，返回"是否仍在预算内"。

        text: 本次新产出的文本片段。

        返回 True 表示还可以继续；False 表示已超预算，调用方应停止继续生成
        （已产出的内容照常返回，不是把整次回答作废）。
        """
        if self.limit <= 0:
            return True
        self.used += estimate_tokens(text)
        return self.used <= self.limit

    @property
    def exceeded(self) -> bool:
        """是否已超预算。"""
        return self.limit > 0 and self.used > self.limit


def build_quota_manager() -> QuotaManager:
    """按 app.config 造配额管理器（main.py 启动时调用一次）。"""
    from . import config

    return QuotaManager(
        daily_limit=config.USER_DAILY_TOKEN_QUOTA,
        request_limit=config.LLM_MAX_TOKENS_PER_REQUEST,
        enabled=config.QUOTA_ENABLED,
        redis_url=config.REDIS_URL,
    )
