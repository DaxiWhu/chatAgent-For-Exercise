"""用量配额：单请求 token 预算 + 日额度（**两级**）。

为什么要有这一层
--------------
限流管的是"请求次数"，管不住"单次请求烧多少 token" —— 一次超长输入 + 多轮
self-review 迭代，可以在一次请求里把当日 LLM 额度打掉一大半。所以生产环境
需要两个独立的闸门：

1. **单请求预算**（`LLM_MAX_TOKENS_PER_REQUEST`）：流式回答过程中累计 token，
   超了就**停止继续生成**（已产出的部分照常返回）。防"一次请求失控"。
2. **日额度**（两级，见下）：请求进来先查，超了直接 429。防"把全站额度吃光"。

日额度为什么是两级（2026-10-06 起）
----------------------------------
只有一级（按租户）时，"吃光额度"这件事有两条没人拦得住的路：

  · **同租户内互抢**：额度是整个租户共用一份，一个人先刷满，同租户其余人当天
    全部 429 —— 而他们什么都没做错。
  · **无租户账号共用一个桶**：账号的租户可以是空串（"还没有租户"是合法状态，
    见 `config.DEFAULT_TENANT`），于是所有无租户账号落进同一个
    `quota::<日期>` 桶，彼此互抢。

所以改成两级，**两级都要过，任一越界即拒**：

  · `USER_DAILY_TOKEN_QUOTA`（租户级）= 天花板，防"一个租户把全站额度吃光"；
  · `DAILY_TOKEN_QUOTA_PER_USER`（每人级）= 防"一个人吃掉同租户的份额"。

有效的每人额度是 `min(每人级, 租户级)` —— 两级都要过，更紧的那一级说了算。
两级都是 `0 = 不限`（默认），所以这一层开着也不会改变既有行为。
⚠️ 只配租户级不配每人级时，"同租户互抢"仍然存在，启动日志会点这一条。

桶键的口径（改它之前先想清楚）
----------------------------
  · 租户级：`quota:{tenant}:{today}` —— **刻意与历史完全一致**。改前缀会让升级
    当天已累计的用量归零（额度被静默放宽）；滚动发布期间新旧副本各用一套键，
    用量还会各算一份。
  · 每人级：`quota@user:{user}:{today}` —— 第 6 个字符与前缀不同，所以它**不可能**
    与任何租户桶撞键（租户名里禁止出现 `:`，见 `accounts._FORBIDDEN_IN_NAME`）。

计数口径
-------
用 token 数而非字数。有 `tiktoken` 就用它的分词器精确数；没有则退化用
`max(1, len(text))` —— **对中文这是偏保守（高估）的估计**，宁可早一点刹住，
也不要因为低估而超支。两种口径都写清楚，便于事后对账。

存储
----
- 日额度：内存 dict（`{桶键: (日期, tokens)}`）；配了 REDIS_URL 则用
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
    used: 今日已用 token（**不含本次预占**）—— 拒绝时是**触发拒绝的那一级**的已用量
        （两级都看时，用租户级那一层会答非所问：额度是被每人级拦下的）。
    limit: 触发判定那一级的上限（0 = 不限）。
    reset_after: 距离配额重置（次日零点）还有多少秒。
    tenant: 租户标识。
    reserved: 本次请求已经**预占**的 token 数（`claim` 成功时 >0，供结算用）。
    subject: 本次请求的**主体**（用户名）；每人级的桶键用它，空串表示没传。
    scope: 触发拒绝的是哪一级 —— "tenant" / "subject"；放行时为空串。
    """

    allowed: bool
    used: int
    limit: int
    reset_after: int
    tenant: str = ""
    reserved: int = 0
    subject: str = ""
    scope: str = ""


def _today() -> str:
    """今天的日期串（本地时区），作为日额度的天然分桶键。"""
    return datetime.date.today().isoformat()


def _tenant_bucket(tenant: str, today: str) -> str:
    """租户级桶键。**格式刻意与历史一致** —— 改它等于把升级当天已累计的用量清零。

    空租户名是合法输入（"还没有租户"），此时键是 `quota::<日期>`。
    """
    return f"quota:{tenant}:{today}"


def _subject_bucket(subject: str, today: str) -> str:
    """每人级桶键。前缀里的 `@` 是**防撞键**用的：

    租户名禁止出现 `:`（`accounts._FORBIDDEN_IN_NAME`），但允许 `@`。所以只要
    "第 6 个字符"与租户键不同，`quota@user:...` 就不可能被任何租户键命中 ——
    否则一个恰好叫 `@user:x` 的租户就能与某个用户的桶互相污染（表现为某人莫名
    被限，且查配置一切正常）。
    """
    return f"quota@user:{subject}:{today}"


class _MemoryStore:
    """进程内日用量：`{桶键: (日期, 计数)}`。

    ⚠️ 键是**已经带日期的桶键**（如 `quota:acme:2026-10-06`）。值里那个日期仍
    留着：结算的退还有可能落到"新的一天"的键上（预占在昨天、结算在今晨），
    靠它把跨天的残留读成 0。
    """

    def __init__(self):
        self._data: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()

    def get(self, bucket: str, today: str) -> int:
        """读今日已用量；跨天自动归零。"""
        with self._lock:
            d, v = self._data.get(bucket, (today, 0))
            return v if d == today else 0

    def add(self, bucket: str, today: str, n: int) -> int:
        """累加今日用量并返回新值。

        `n` 允许为负（结算时要把多预占的部分退回来）。结果**下限截到 0**：
        预占发生在昨天、结算落在今天时，退还会打到今天的键上，不截断的话今天的
        用量会变成负数 —— 表现成"跨天时白送一段额度"。
        """
        with self._lock:
            d, v = self._data.get(bucket, (today, 0))
            v = max(0, (v if d == today else 0) + int(n))
            self._data[bucket] = (today, v)
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

    def get(self, bucket: str, today: str) -> int:
        """读今日已用量（键不存在返回 0）。

        redis-py 默认 `decode_responses=False`，拿到的是 bytes；
        `int(b"12")` 在 Python 3 是合法的（已实测），所以这里不需要 decode。

        **下限截到 0**：结算的退还有可能落到"新的一天"的键上（预占在昨天、结算
        在今晨），不截断的话今天的用量会是负数 = 白送一段额度。
        """
        v = self._r.get(bucket)
        return max(0, int(v or 0))

    def add(self, bucket: str, today: str, n: int) -> int:
        """累加并保证键在当天末过期。

        `n` 允许为负：结算时把多预占的部分退回（`settle` 的说明）。返回值同样
        下限截到 0，理由见 `get`。

        过期用**相对 TTL**（`EXPIRE`）而不是绝对时间戳（`EXPIREAT`）：
        后者的时刻由**应用侧时钟**算出，一旦应用与 Redis 的时钟不一致，
        算出的时刻可能已经过去 ⇒ Redis 立刻删键 ⇒ 每次加完马上清零 ⇒
        **配额永远攒不满，静默失效**。这里用 `max(60, ...)` 再兜一层，
        保证任何情况下键至少活 60 秒。
        """
        key = bucket
        ttl = max(60, _end_of_day_epoch() - int(time.time()))
        with self._r.pipeline() as pipe:
            pipe.incrby(key, int(n))
            pipe.expire(key, ttl)
            return max(0, int(pipe.execute()[0]))


def _end_of_day_epoch() -> int:
    """次日零点的 unix 时间戳（给 Redis EXPIREAT 用）。"""
    tomorrow = datetime.datetime.combine(
        datetime.date.today() + datetime.timedelta(days=1),
        datetime.time.min,
    )
    return int(tomorrow.timestamp())


class QuotaManager:
    """配额管理器：`claim()` 预占并判定、`settle()` 结算、`check()` 只读查。

    日额度分**两级**（租户级 / 每人级），两级都要过。哪一级开了额度
    （limit > 0）就参与判定与记账，两级都配 0 时这一层完全不介入。
    """

    def __init__(self, daily_limit=0, request_limit=0, enabled=True, redis_url="",
                 per_user_limit=0):
        """构造。

        daily_limit: **租户级**每日 token 上限（`USER_DAILY_TOKEN_QUOTA`），0 = 不限。
        request_limit: 单请求 token 上限，0 = 不限（由调用方流式累计后判定）。
        enabled: False 时这一层完全不介入。
        redis_url: 非空则用 Redis 共享日用量，失败回退内存。
        per_user_limit: **每人级**每日 token 上限（`DAILY_TOKEN_QUOTA_PER_USER`），0 = 不限。

        ⚠️ 前四个参数的名字与**顺序**都不能动：测试、脚本、`identity` 里大量用
        `QuotaManager(daily_limit=..., request_limit=...)` 构造（关键字与位置两种都有）。
        新增的每人级额度只能**追加在末尾** —— 第一版把它插在 `request_limit` 前面，
        位置调用一律静默错位（`request_limit` 被当成 `per_user_limit`），
        表现成 20+ 个用例变红，而报错信息完全指不到这里。
        """
        self.daily_limit = max(0, int(daily_limit))
        self.per_user_limit = max(0, int(per_user_limit))
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

        self._warn_about_limit_mismatch()

    def _warn_about_limit_mismatch(self):
        """启动期点出两种"配了但对不上"的额度组合 —— 两者都是**静默**故障。

        ⚠️ 某级额度 ≤ 单请求上限 ⇒ **全站 429**：预占（`reservation()` 取单请求
        上限）连自己那一关都过不去，**每一次**请求都被判超额。配置看起来完全正常，
        排查方向会跑到鉴权/网络上去。

        ⚠️ 只配了租户级（**默认就会命中**，因为每人级默认 0）⇒ **同租户内的人互相
        抢额度**：一个人刷满后同租户其余人当天全部 429；无租户的账号（租户为空串）
        还会共用一个桶。症状要等到某天有人刷满、同一批人集体 429 才出现，而那时
        没人会想到"额度是按租户分的桶"。
        """
        for scope, limit, knob in (
            ("租户级", self.daily_limit, "USER_DAILY_TOKEN_QUOTA"),
            ("每人级", self.per_user_limit, "DAILY_TOKEN_QUOTA_PER_USER"),
        ):
            if limit > 0 and self.request_limit >= limit:
                logging.warning(
                    "单请求上限(%d) 不小于日配额（%s=%d，%s）：任何请求的预占都无法被"
                    "容纳，**所有请求都会被判 429**。请把 LLM_MAX_TOKENS_PER_REQUEST "
                    "调到远小于该额度（经验值是 1/20 上下）。",
                    self.request_limit, knob, limit, scope)

        if self.daily_limit > 0 and self.per_user_limit == 0:
            logging.warning(
                "只配了租户级日额度(%d)、未配每人级 ⇒ **同一个租户里的人会互相抢额度**："
                "一个人先刷满，同租户其余人当天全部 429。无租户的账号（租户为空串）"
                "更是共用一个桶，彼此互抢。配 %s 可消除（每人每天多少 token）。",
                self.daily_limit, "DAILY_TOKEN_QUOTA_PER_USER")

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

    def levels(self, tenant: str, subject: str, today: str) -> list:
        """本次请求要判定的层级：`[(scope, 桶键, 上限), ...]`，只含**开了额度**的层。

        scope 是 "tenant" / "subject"，用于日志与 429 文案定位是哪一级拦下的。
        空 subject 不跳过每人级 —— 它会落到 `quota@user::<日期>` 这个**共享桶**上。
        宁可"身份缺失的人也仍然被限、且与同样缺身份的人共享一份"，也不要
        "身份缺失就没人管"（fail-closed）。
        """
        out = []
        if self.daily_limit > 0:
            out.append(("tenant", _tenant_bucket(tenant, today), self.daily_limit))
        if self.per_user_limit > 0:
            out.append(("subject", _subject_bucket(subject, today), self.per_user_limit))
        return out

    def _bump(self, bucket: str, n: int) -> int:
        """给某个桶加/减一次用量；主后端失败时退到进程内计数并留痕。

        bucket: 已经拼好的桶键（见 `levels`）。
        n: 正数累加、负数退回。兜底也失败时会把异常抛给调用方，
            由调用方决定是"放行"还是"放弃结算"。
        """
        try:
            return self.store.add(bucket, _today(), n)
        except Exception as e:  # noqa: BLE001
            self._fallback(e, "累加" if n >= 0 else "退回")
            return self._local.add(bucket, _today(), n)

    def _release(self, taken: list, reserved: int) -> None:
        """把已经预占的桶逐个退回。

        taken: `[(桶键, 上限, scope, 预占后的计数), ...]`（`claim` 攒下来的）。
        退回失败只记日志不抛：那会让额度偏紧（安全方向），而这里已经在失败路径上，
        再抛一个异常出去只会把"额度不够"变成 500。
        """
        for bucket, _limit, _scope, _after in taken:
            try:
                self._bump(bucket, -reserved)
            except Exception:  # noqa: BLE001
                logging.error("配额预占退回失败（额度会偏紧，需人工核对）：bucket=%s", bucket)

    def claim(self, tenant: str = "", subject: str = "") -> QuotaStatus:
        """**原子地**"预占额度 + 判定"。取代"先 check、请求结束再 add"的两步写法。

        tenant: 租户标识（空串合法 = "这个人还没有租户"）。
        subject: 主体（用户名），每人级的桶键用它。空串会落到一个共享桶（见 `levels`）。

        为什么必须合成一步（这一步就是原来的 TOCTOU）
        --------------------------------------------
        旧写法里 `check` 在请求开头、`add` 在请求结尾，中间隔着整次生成（几秒到
        几十秒）。并发的 N 个请求会**各自读到同一个"还没超"的旧值**然后一起放行，
        最后各自 `add` —— 日额度被超掉 N 份。这不是"理论上的小概率竞态"：它只在
        额度快用完时发生，而那正是额度最需要被守住的时候；并发越高超得越多。

        做法是"先占后判"：把本次请求最多可能消耗的 token（`reservation()`）
        **一次原子加**进去，再看是否越界；越界就把它退回来（被拒的请求不该消耗
        额度）。这样"判定"与"记账"是同一次原子操作，不存在中间窗口。

        两级时逐级预占，**任一级越界就把已经占上都退回**（否则被拒的请求会永久
        吃掉一级的额度，且没有任何症状）。两级之间**不是**一个原子操作（那样要
        上 Lua），但方向仍然是安全的：并发下最坏是"偶发多拒一次"。

        返回的 `used` 是**不含本次预占**的已用量，便于日志与 429 文案对齐直觉；
        `reserved` 字段给出预占量，请求结束时交给 `settle` 换成实际值。

        并发下的一个副作用（方向是安全的）：两个请求同时越过限额时，可能各自
        看到对方预占后的值而**都**被拒，然后各自退回。结果是"偶发多拒一次"，
        而不是"偶发多放一次" —— 计费闸门宁可偏严。
        """
        reset = max(1, _end_of_day_epoch() - int(time.time()))
        empty = QuotaStatus(True, 0, 0, reset, tenant, 0, subject=subject)
        if not self.enabled:
            return empty

        today = _today()
        levels = self.levels(tenant, subject, today)
        if not levels:
            return empty

        reserve = self.reservation()
        taken = []          # 已经预占成功的 (桶键, 上限, scope, 预占后的计数)
        for scope, bucket, limit in levels:
            try:
                after = self._bump(bucket, reserve)
            except Exception:  # noqa: BLE001  兜底也失败才真正放行（计费故障不该打死服务）
                self._release(taken, reserve)
                return empty
            taken.append((bucket, limit, scope, after))
            if after > limit:
                self._release(taken, reserve)
                return QuotaStatus(False, max(0, after - reserve), limit, reset,
                                   tenant, 0, subject=subject, scope=scope)

        # 放行时报告**最紧的那一级**的用量：只报租户级会误导 —— 两级都看的时候，
        # "还剩多少额度"是由离上限更近的那一级决定的（通常是每人级）。
        # 用已经握在手里的 `after` 算，不额外读一次存储。
        scope, limit, after = min(
            ((s, lim, a) for _b, lim, s, a in taken),
            key=lambda row: (row[1] - row[2]) / max(1, row[1]))
        return QuotaStatus(True, max(0, after - reserve), limit, reset, tenant, reserve,
                           subject=subject, scope=scope)

    def _read(self, bucket: str) -> int:
        """读一个桶的当前计数；主后端失败退到进程内计数。读失败返回 0。"""
        try:
            return self.store.get(bucket, _today())
        except Exception as e:  # noqa: BLE001
            self._fallback(e, "读取")
            try:
                return self._local.get(bucket, _today())
            except Exception:  # noqa: BLE001
                return 0

    def settle(self, tenant: str, reserved: int, actual: int, subject: str = "") -> int:
        """请求结束时把"预占"换成"实际消耗"，返回调整后的今日用量。

        tenant: 租户标识。
        reserved: `claim` 返回的 `reserved`（没走预占则传 0）。
        actual: 本次实际消耗的 token。
        subject: 与 `claim` 同一次请求传的同一个主体（缺了会算到错误的桶上）。

        ⚠️ 差额多为**负数**（模型提前结束、断连中止都会让实际小于预占），所以这条路
        必须允许减计数：不允许的话预占永远不退，额度被逐次吃掉 —— 表现成"配额比
        实际消耗快得多地用尽"，而且没有任何报错，只能靠对账发现。

        ⚠️ 结算的层级 = **当前配置下开了额度的层级**，与 `claim` 时天然一致
        （进程内配置不会变）。所以这里不需要把"claim 时占了哪几级"记进返回值 ——
        少一个可以传错的状态。
        """
        delta = int(actual) - int(reserved)
        if not self.enabled or delta == 0:
            return 0
        today = _today()
        last = 0
        for _scope, bucket, _limit in self.levels(tenant, subject, today):
            try:
                last = self._bump(bucket, delta)
            except Exception:  # noqa: BLE001  兜底也失败才放弃记账
                last = 0
        return last

    def check(self, tenant: str = "", subject: str = "") -> QuotaStatus:
        """查今日额度是否还够（只读，不做预占）。

        tenant: 租户标识（空串合法 = "这个人还没有租户"）。
        subject: 主体（用户名）；不传则每人级按共享桶判（见 `levels`）。

        两级额度都为 0（不限）或功能关闭时恒放行。存储异常时退到进程内计数；
        进程内计数也异常才真正放行（计费类故障不该把服务直接打死）。

        ⚠️ 只读端点用它可以，**要开始一次生成必须用 `claim`** —— 拿 check 的结果
        去决定"能不能开工"，等于把 TOCTOU 又请回来。
        """
        reset = max(1, _end_of_day_epoch() - int(time.time()))
        if not self.enabled:
            return QuotaStatus(True, 0, 0, reset, tenant, 0, subject=subject)
        levels = self.levels(tenant, subject, _today())
        if not levels:
            return QuotaStatus(True, 0, 0, reset, tenant, 0, subject=subject)

        rows = [(scope, limit, self._read(bucket)) for scope, bucket, limit in levels]
        scope, limit, used = min(rows, key=lambda r: (r[1] - r[2]) / max(1, r[1]))
        return QuotaStatus(used < limit, used, limit, reset, tenant, 0,
                           subject=subject, scope=scope if used >= limit else "")

    def add(self, tenant: str, tokens: int, subject: str = "") -> int:
        """把一次消耗计入今日用量，返回累计值。

        tenant: 租户标识。
        tokens: 增量；>0 累加、<0 退回（`settle` 用）、==0 直接返回当前值。
        subject: 主体（用户名）；留空则不额外计入每人级。

        ⚠️ 正常路径请用 `settle`（它同时处理两级）。这个方法保留给"没有预占、
        只有一次实际消耗"的场景（测试、脚本回填）。
        """
        if not self.enabled or int(tokens) == 0:
            return 0
        today = _today()
        buckets = [bucket for _scope, bucket, _limit in self.levels(tenant, subject, today)]
        if not buckets:
            return 0
        last = 0
        for bucket in buckets:
            try:
                last = self._bump(bucket, int(tokens))
            except Exception:  # noqa: BLE001  兜底也失败才放弃记账
                last = 0
        return last


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
        per_user_limit=config.DAILY_TOKEN_QUOTA_PER_USER,
        request_limit=config.LLM_MAX_TOKENS_PER_REQUEST,
        enabled=config.QUOTA_ENABLED,
        redis_url=config.REDIS_URL,
    )
