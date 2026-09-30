"""限流：**加权滑动窗口**计数，IP 与会话两个维度。

为什么从固定窗口改成滑动窗口（2026-09-29）
----------------------------------------
固定窗口（`key + 窗口序号 → 计数`）实现最简单，但有一个**结构性**缺陷：
窗口按整分钟翻转，所以客户端可以在窗口末尾用满 N 次、紧接着在新窗口再用满
N 次 —— **任意 60 秒滑窗内最多 2N 次**。这不是"理论上的边界情况"：只要客户端
有 60 秒对齐的重试循环就会稳定命中。而限流在这里挡的是 LLM 成本，瞬时 2 倍配额
恰好是最不该放行的时候。

改用**加权滑动窗口**（业界常称 sliding window counter）：同时保留"当前窗口"与
"上一窗口"的计数，判定值为

    上一窗口计数 × (当前窗口剩余比例) + 当前窗口计数

上一窗口的贡献随时间线性衰减到 0，于是"末尾用满 + 开头用满"不再放行，边界处的
最大放行量从 2N 收敛到 N 量级。代价是每个键要存两个计数器（而非一个），换来的是
**内存仍然 O(1)/键**，且两个后端能用同一套语义实现。

为什么不用令牌桶 / 滑动窗口日志
------------------------------
- **令牌桶**更平滑，但状态是"上次补充时间 + 当前令牌数"，需要读-改-写；多实例下
  要么上 Redis Lua、要么接受竞态。本项目的后端抽象只要求"一次原子计数"（Redis 用
  INCR 即可），保持这个约束才能让内存/Redis 两个后端语义一致。
- **滑动窗口日志**（记录每次命中的时刻）判定最精确，但每个键的内存/元素数随流量
  线性增长 —— 那是**可被利用的放大面**：攻击者用高频请求把计数结构撑大即等同于
  一次内存型 DoS。加权计数器只有两个整数。

代价与边界
---------
- 判定值会向下取整，因此实际放行量可能比理论值多不到 1 次（可接受：限流不是精确计费）。
- `Retry-After` 给的是**上界**：按公式解出"再放行一次仍在限内"的时刻，客户端在该
  时刻重试一定通过。它和"距窗口结束秒数"没有固定的大小关系 —— 边界处那种
  "上一窗口用满、新窗口刚要开始"的情形会更早恢复，而"在一个窗口里一次性打满
  额度"（`cur` 远超 `limit`）的情形会比窗口翻页更晚。两者都由同一个公式给出。

两个维度为什么都要
----------------
- **按 IP**：挡住单个来源的高频刷（含未登录的匿名流量）。
- **按会话（thread_id）**：一个会话同时在 /chat 与 /review 上刷时，单看 IP
  可能被 NAT 掩盖或反过来误伤同出口的其他用户；按会话限流能把"一个对话"
  的消耗单独框住。
两者取"更严格的结论"：任一超限即拒绝。

共享后端（REDIS_URL）
-------------------
填了就优先用 Redis（多实例共享计数）；**没装 redis 包或连接不可用**时自动回退内存，
但会打一条 **ERROR**（不是 warning）并明确写出"多副本下额度 = 配置值 × 副本数" ——
因为这是一次**静默降级**：配置项填好了、服务照常启动，只有日志能看出共享并没生效。
回退后 `RateLimiter.backend_name` 会如实变成 "memory"，由启动日志回显出来可核对。

运行期后端异常时**退到进程内计数**（而不是放行），见 `_hit` 的说明。
"""

import logging
import math
import threading
import time
from dataclasses import dataclass

from . import metrics

_WINDOW = 60  # 秒。滑窗长度：所有计数按"最近 60 秒"计


@dataclass
class RateLimitResult:
    """一次限流判定的结果。

    allowed: 是否放行。
    limit: 该维度的上限（次/分钟）。
    remaining: 本窗口剩余次数（>=0）。
    reset_after: 还要等多少秒才能恢复（放行时 = 当前窗口剩余秒数，用于 Retry-After）。
    scope: "ip" / "session"，标明是哪一维度触发的（日志与响应体用）。
    """

    allowed: bool
    limit: int
    remaining: int
    reset_after: int
    scope: str = ""


def _window_start(now=None) -> float:
    """当前窗口的起始秒（对齐到整分钟）。

    返回 float 而不是 int：对齐点本身是整数，但下游要用它算"已经过去多少秒"
    （`now - window_start`），统一成浮点可以少一处隐式转换。
    """
    return float(int((now if now is not None else time.time()) // _WINDOW) * _WINDOW)


def _weighted(prev: int, cur: int, window_start: float, now: float) -> float:
    """加权滑窗判定值 = 上一窗口计数 × 剩余比例 + 当前窗口计数。

    prev: 上一窗口的计数。
    cur: 当前窗口的计数（**已包含本次**）。
    window_start: 当前窗口起始秒。
    now: 当前时间。

    剩余比例为 1 → 0（窗口从头走到尾），所以上一窗口的贡献会线性衰减到 0。
    """
    elapsed = max(0.0, min(float(_WINDOW), now - window_start))
    return prev * (_WINDOW - elapsed) / _WINDOW + cur


def _blocked_seconds(prev: int, cur: int, limit: int, window_start: float, now: float) -> int:
    """还要等多少秒才能恢复（Retry-After 用）。

    ⚠️ 关键细节：**重试这一次自己也要占 1 次配额**。所以判定用的是"再放行一次
    之后是否仍在限内"，而不是"当前计数是否回落到限内" —— 后者算出来的时刻会
    早一步，客户端按它重试正好又被拒（重试风暴的成因）。

    分两段解 —— 两个计数器的时间行为不同，不能只算一段：
      ① 本窗口内：`prev` 线性衰减、`cur` 恒定。若剩下的额度已被 `cur` 吃掉
         （`room <= 0`），这一段无解，必须等窗口翻页。
      ② 翻页后：`cur` 变成"上一窗口"开始衰减，新的当前窗口只有重试这一次。
         对任意 `limit >= 1` 这一段都有解。
    """
    budget = max(0, limit - 1)          # 给重试本身留出的额度
    w1 = window_start + _WINDOW
    t: float | None = None

    if prev > 0:
        room = budget - cur             # 现窗口里还能再容纳多少次
        if room > 0:
            # prev * (w1 - t) / _WINDOW <= room
            t = w1 - _WINDOW * room / prev

    if t is None:
        # ② 翻页后：等到 cur 的贡献衰减到只剩 budget
        t = w1 + _WINDOW - _WINDOW * budget / max(1, cur)

    return max(1, int(math.ceil(t - now)))


class _MemoryBackend:
    """进程内计数：`{(bucket_key, 窗口序号): 计数}`，只保留最近两个窗口。

    只留两个窗口就够：判定值只用得上"当前 + 上一"两个窗口。
    """

    def __init__(self):
        self._data: dict[tuple, int] = {}
        self._lock = threading.Lock()
        self._last_sweep = 0.0

    def hit(self, bucket_key: str, limit: int, now: float) -> tuple[int, int]:
        """给 key 记一次并返回 (加权计数, 距恢复的秒数)。

        bucket_key: 维度键（如 "ip:1.2.3.4"）。
        limit: 该维度的上限（用于判定与 reset 计算）。
        now: 当前时间（由调用方传入，便于测试注入假时钟）。
        """
        w = int(_window_start(now))
        k = (bucket_key, w)
        pk = (bucket_key, w - _WINDOW)

        with self._lock:
            # ⚠️ 过期清理**不能每请求全表扫**：那是 O(活跃键数) 的每请求成本，
            # 在"高基数 IP"（扫描/爬虫）下会把限流器自己变成瓶颈 —— 而限流器
            # 恰恰是攻击流量第一个撞上的组件。改成摊还 O(1)：最多每分钟扫一次，
            # 代价是过期键最多多留一个窗口（内存上界仍是"最近两个窗口的键"）。
            if now - self._last_sweep >= _WINDOW:
                self._last_sweep = now
                for key in [key for key in self._data if key[1] < w - _WINDOW]:
                    self._data.pop(key, None)

            prev = self._data.get(pk, 0)
            cur = self._data.get(k, 0) + 1
            self._data[k] = cur

        used = int(math.floor(_weighted(prev, cur, w, now)))
        reset = (max(1, int(math.ceil(w + _WINDOW - now))) if used <= limit
                 else _blocked_seconds(prev, cur, limit, w, now))
        return used, reset


class _RedisBackend:
    """Redis 计数：当前窗口 INCR + 读上一窗口，多实例共享。失败时由上层回退内存。"""

    def __init__(self, url: str):
        import redis  # 延迟导入：没装 redis 包时上层会回退内存

        self._r = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
        # `from_url` 是**懒连接**：只构造客户端对象，不建立连接（实测：指向一个
        # 没人监听的端口也照样不抛异常）。不在这里 ping 的话，"地址写错 / 服务没起 /
        # 网络不通"要等到**第一个真实请求**才暴露，而那时的异常会被 `_hit` 吞掉 ——
        # 结果就是"REDIS_URL 配了、日志里一行 warning、限流实际完全不生效"。
        # 主动 ping 把失败提前到构造期，由上层记成 ERROR 并明确回退。
        self._r.ping()

    def hit(self, bucket_key: str, limit: int, now: float) -> tuple[int, int]:
        """INCR 当前窗口、读上一窗口；返回 (加权计数, 距恢复的秒数)。

        ⚠️ 不要改成 `EXPIREAT`（绝对时间）：那要依赖**应用服务器与 Redis 的时钟一致**，
        应用时钟偏快会导致键被立刻删除（计数清零 = 限流失效），偏慢则键残留。
        `EXPIRE` 用的是 Redis 侧时钟，没有这个问题。

        ⚠️ TTL 必须是 **2 个窗口**（旧实现是 1 个窗口 + 1 秒）：键在"当下一个窗口的
        上一窗口"时**仍然会被读**。TTL 只给一个窗口的话，刚好在下个窗口开始的瞬间
        键已过期 ⇒ 上一窗口的计数读成 0 ⇒ 边界 2N 的漏洞在 Redis 后端**原样复发**
        （而且只在多副本 + 恰好跨窗口时出现，极难复现）。
        """
        w = int(_window_start(now))
        cur_key = f"rl:{bucket_key}:{w}"
        prev_key = f"rl:{bucket_key}:{w - _WINDOW}"
        # pipeline 默认 transaction=True → 包在 MULTI/EXEC 里，三条命令原子生效，
        # 不会出现"计数加了但没设过期"的中间态。
        with self._r.pipeline() as pipe:
            pipe.incr(cur_key)
            pipe.expire(cur_key, 2 * _WINDOW + 1)
            pipe.get(prev_key)
            cur, _, prev = pipe.execute()

        prev, cur = int(prev or 0), int(cur)
        used = int(math.floor(_weighted(prev, cur, w, now)))
        reset = (max(1, int(math.ceil(w + _WINDOW - now))) if used <= limit
                 else _blocked_seconds(prev, cur, limit, w, now))
        return used, reset


class RateLimiter:
    """双维度限流器：`check(ip, session)` 返回最严格的判定结果。"""

    def __init__(self, per_minute=120, session_per_minute=None, enabled=True,
                 redis_url="", by_session=True):
        """构造限流器。

        per_minute: 每个 (IP, 路由) 每分钟上限。
        session_per_minute: 每个会话（跨路由）每分钟上限；None 取 per_minute。
        enabled: False 时 check 恒放行（开关关掉）。
        redis_url: 非空则尝试 Redis 后端，失败回退内存。
        by_session: 是否启用会话维度。
        """
        self.per_minute = max(1, int(per_minute))
        self.session_per_minute = max(1, int(session_per_minute or per_minute))
        self.enabled = bool(enabled)
        self.by_session = bool(by_session)
        # `_local` 是**兜底表**：后端（可能是 Redis）出错时改用它计数。
        # 它与主后端必须是**两个独立实例** —— 共用同一个 _MemoryBackend 时，
        # "回退"其实还是在读主后端的状态，语义就不成立了。
        self._local = _MemoryBackend()
        self.backend = self._local
        self.backend_name = "memory"
        if redis_url:
            try:
                self.backend = _RedisBackend(redis_url)
                self.backend_name = "redis"
                logging.info("限流计数后端：redis（多实例共享）")
            except ImportError as e:
                # 与"连不上"区分开：这是**缺依赖**，运维的修法是装包，不是查网络
                logging.error(
                    "REDIS_URL 已配置，但未安装 redis 客户端 ⇒ 共享计数**未生效**，"
                    "当前用进程内计数：多副本下实际额度 = 配置值 × 副本数。"
                    "请执行 `pip install redis`。原因：%s", e)
            except Exception as e:  # noqa: BLE001
                logging.error(
                    "REDIS_URL 已配置但连接不可用 ⇒ 共享计数**未生效**，当前用进程内计数："
                    "多副本下实际额度 = 配置值 × 副本数。请检查 REDIS_URL 与网络。原因：%s", e)

    def check(self, ip: str = "", session: str = "", route: str = "") -> RateLimitResult:
        """判定是否放行。

        ip: 客户端 IP（可能为空）。
        session: 会话 id（thread_id，可能为空）。
        route: 路由模板，用于把 IP 维度的计数按端点拆开（避免 /chat 与 /review 互相挤占）。

        返回 RateLimitResult。两个维度都查，任一超限即拒绝；返回的是"更严格"的那个。
        两个维度共用同一个 `now` —— 各算各的时钟会出现"IP 维度按旧窗口、会话维度按
        新窗口"的错位判定。
        """
        if not self.enabled:
            return RateLimitResult(True, self.per_minute, self.per_minute, 0, "")

        now = time.time()
        results = []

        if ip:
            key = f"ip:{ip}:{route or '-'}"
            used, reset = self._hit(key, self.per_minute, now)
            results.append(RateLimitResult(used <= self.per_minute, self.per_minute,
                                           max(0, self.per_minute - used), reset, "ip"))

        if self.by_session and session:
            key = f"sess:{session}"          # 跨路由累计：会话维度不带 route
            used, reset = self._hit(key, self.session_per_minute, now)
            results.append(RateLimitResult(used <= self.session_per_minute,
                                           self.session_per_minute,
                                           max(0, self.session_per_minute - used),
                                           reset, "session"))

        if not results:
            return RateLimitResult(True, self.per_minute, self.per_minute, 0, "")

        # 取最严格：先看不放行的，再比剩余次数
        blocked = [r for r in results if not r.allowed]
        if blocked:
            return min(blocked, key=lambda r: (r.remaining, -r.reset_after))
        return min(results, key=lambda r: r.remaining)

    def _hit(self, key, limit, now):
        """给一个维度计数。

        后端异常时**退到进程内计数**，而不是直接放行。
        为什么改掉了原来的"放行"：限流是挡住 LLM 成本的最后一道闸门，
        放行意味着"Redis 抖一下，限流就完全失效" —— 而这恰恰是最可能被
        吃满额度的时刻（故障期流量通常还更高）。退回本地计数虽然不准
        （各副本各算各的），但至少仍有拦截能力，属于"降级"而非"关闭"。
        同时打 ERROR + 计数，让"共享后端在报错"可见（否则只有一行 warning 被冲掉）。
        """
        try:
            return self.backend.hit(key, limit, now)
        except Exception as e:  # noqa: BLE001
            metrics.inc("shared_backend_errors_total",
                        component="ratelimit", backend=self.backend_name)
            logging.error("限流共享后端异常，本次退到进程内计数（多副本下计数不准）：%s", e)
            try:
                return self._local.hit(key, limit, now)
            except Exception:  # noqa: BLE001  兜底也失败才真正放行
                return 0, _WINDOW


def build_rate_limiter() -> RateLimiter:
    """按 app.config 造一个限流器（main.py 启动时调用一次）。

    无参数。把配置读取集中在这里，测试可自行构造 RateLimiter 覆盖。
    """
    from . import config

    return RateLimiter(
        per_minute=config.RATE_LIMIT_PER_MINUTE,
        session_per_minute=config.RATE_LIMIT_SESSION_PER_MINUTE,
        enabled=config.RATE_LIMIT_ENABLED,
        redis_url=config.REDIS_URL,
        by_session=config.RATE_LIMIT_BY_SESSION,
    )
