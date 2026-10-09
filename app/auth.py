"""用户鉴权：会话 token 验签 + 服务端会话白名单 + 口令校验。

⚠️ 这条路径是服务**唯一**的鉴权口径。此前并存的几档已全部删除："租户绑定到
API Key"（TENANT_KEYS 反查）、"共享密钥过渡通行证"（`SERVE_API_KEY`）、
**外部身份源直通**（OIDC / JWKS，2026-10-05 删除）与"未配鉴权时匿名放行"的本地兜底 ——
现在**未带凭证的请求一律 401**。脚本 / CI / 压测也走这条路径
（服务账号 + `POST /v1/auth/login`）。

解决什么问题
-----------
在它之前，服务只有"一把共享 API Key"的门禁：粒度到租户、无法归因到人、
无法撤销、无法表达角色。本模块补齐"用户"这个主体。

为什么必须引入服务端状态
----------------------
因为**撤销**。无状态 JWT 在任何地方都有效，服务端无法单方面作废它 —— 用户点了
登出、密码泄漏了、员工离职了，服务端都没有手段让已签发的 token 立刻失效。
所以本服务自己**签发**会话 token（HS256，密钥 `AUTH_SESSION_SECRET`），并维护一张
**会话白名单**回答"你手上这个会话还能不能用"：每个仍然有效的会话占一个键
`auth:sess:{user}:{sid}`（详见下方「会话白名单」一节）。

⚠️ 为什么不再接外部身份源（2026-10-05 用户拍板）
--------------------------------------------
"把认证外包给已有 IdP"只有在**真的有人用那个 IdP** 时才是收益。本项目实际的身份
来源是"自助注册 + 申请加入租户 + 管理员批准"，全是本服务自己的账号体系；OIDC
一天也没被启用过。这种情况下留着它只剩代价：多一条要长期维护与测试的身份入口、
多一份"看起来能接 SSO"的误导。所以整条路径（JWKS 拉取与缓存、kid 轮换、算法
白名单、claim 映射、`id_token` 登录）**物理删除**，不留开关。

⚠️ 历史沿革（2026-10-01）：最初的需求是"同一用户不能同时在两个地方登录"，
因此白名单的前身是**单槽位锚点** `auth:sess:{user} -> sid` —— 登录时原子覆盖，
旧 token 的 jti 与锚点不再相等即为失效。那条约束后来被取消（改为一用户多设备
并存），锚点随之升级成"每会话一键"。**注意：覆盖语义（新登录踢旧登录）已经
不存在了**，需要清场时走 `close_all_sessions`（登出所有设备）。

为什么是白名单而不是撤销列表
--------------------------
常见做法是维护"已作废 jti"的黑名单，但那张表要保存所有历史 token 直到它们过期，
是**无界增长**的；白名单只保存"当前还有效的那些会话"，条数 = 用户数 × 在线设备数，
且有 TTL 自动回收。

两块能力
-------
1. `SessionStore` —— 会话白名单与 refresh token（Redis 优先 / 内存兜底）
2. 口令校验 —— PBKDF2-HMAC-SHA256（标准库实现，零额外依赖）

⚠️ **用户数据不在本模块**：谁存在、属于哪个租户、什么角色，都在数据库的 `users`
表里，由 `app/accounts.py` 读写、由管理接口增删改查。本模块只回答两件事 ——
"这个密码对不对"、"这个令牌是不是本服务签的且会话还在"。
`AccountStore` 由**构造参数注入**（`build_auth_service(accounts=...)`），
避免 `auth ↔ accounts` 互相 import 成环。

⚠️ 未配置 `AUTH_*` 时本模块完全不参与 `main._authenticate`，行为与没有它一致。
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time

from . import config, metrics

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class AuthError(Exception):
    """鉴权失败。

    `code` 是**稳定的机器可读标记**，会随响应体下发（`{"error": ..., "code": ...}`），
    供客户端分支处理 —— 例如收到 `session_revoked` 说明该会话已被撤销（本人登出、
    登出了所有设备、或被清场），应当重新登录；而收到 `invalid_token` 说明 token
    本身有问题（过期/被篡改），应当先尝试刷新。用人话字符串做分支是脆的。

    `status_code` 约定：
      401 —— 凭证缺失/无效/过期（客户端可尝试刷新或重新登录）
      403 —— 凭证有效但无权（换 token 也没用）
      503 —— 鉴权依赖不可用（服务器未配会话密钥 / 会话存储后端不可用）
    """

    def __init__(self, detail, *, status_code=401, code="invalid_credentials"):
        super().__init__(detail)
        self.detail = detail
        self.status_code = int(status_code)
        self.code = code


# ---------------------------------------------------------------------------
# 本地用户密码（PBKDF2-HMAC-SHA256，标准库）
# ---------------------------------------------------------------------------

_HASH_ALGO = "pbkdf2_sha256"

# 迭代数上下限。**这个范围检查是安全必需的**，不是为了健壮性：
# 迭代数写在哈希串里（见 `verify_password`），也就是**由配置串提供**。没有上限时，
# 一条 `pbkdf2_sha256$2000000000$...` 会让单次登录消耗 CPU 数十秒 —— 攻击者
# 只要知道用户名就能用一个请求打满一个 worker（无需密码正确）。
# 下限则防止有人用 1 次迭代生成哈希，使 PBKDF2 退化成一次 SHA-256。
_MIN_ITER = 100_000
_MAX_ITER = 4_000_000


def hash_password(password, *, iterations=None, salt=None):
    """生成 PBKDF2 编码串：`pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>`。

    为什么编码成**单个字符串**而不是"salt 与 hash 各占一列"：本项目的自部署形态
    没有用户表，只有 `.env` —— 单串才能直接写进 `AUTH_USERS`。
    """
    iterations = int(iterations or config.PBKDF2_ITERATIONS)
    iterations = max(_MIN_ITER, min(_MAX_ITER, iterations))
    salt = salt if salt is not None else os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, iterations)
    return f"{_HASH_ALGO}${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password, encoded):
    """校验密码；**任何解析失败一律返回 False**，不抛异常。

    不抛异常的理由：登录路径不该因为一条脏配置返回 500（那既泄露了"该用户名存在
    但配置坏了"，又把一个客户端错误升级成服务端错误）。"异常即失败"与"返回 False"
    在安全上等价，后者口径更统一。
    """
    try:
        algo, iter_s, salt_hex, hash_hex = str(encoded or "").split("$")
        if algo != _HASH_ALGO:
            return False
        iterations = int(iter_s)
        # ⚠️ 迭代数取自**哈希串自身**，不是取自 config.PBKDF2_ITERATIONS。
        # 从配置读会导致：运维哪天调大迭代数 → 所有存量哈希（用旧迭代数生成）
        # 全部校验失败 → 全员锁在门外，而且现象是"密码明明是对的"。
        # 代价就是上面那个范围检查必须做（哈希串是外部输入）。
        if not (_MIN_ITER <= iterations <= _MAX_ITER):
            logging.error("AUTH_USERS 中存在迭代数超出安全范围的哈希，已拒绝该条目")
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, iterations)
    # 常量时间比较：`==` 在第一个不同字节就返回，逐字节试错能靠响应时间反推哈希。
    return hmac.compare_digest(dk, expected)


# ---------------------------------------------------------------------------
# 会话白名单（多设备并存）
# ---------------------------------------------------------------------------
#
# 结构演进（2026-10-01）：原来是**单槽位锚点** `auth:sess:{user} -> session_id`。
# 它的存在理由就是"同一用户不能同时在两处登录"—— 每次登录原子覆盖，旧 token
# 的 jti 与锚点不再相等即为失效。
#
# 现在要求允许多设备并存，单槽位随之不成立，改成**每会话一个键**：
#
#     auth:sess:{user}:{session_id} -> "<登录时间戳>"     每个键独立的 TTL
#
# 为什么不是"把一个用户的会话放进一个 SET / ZSET"，三条理由：
#   1. 校验只需要回答"**这一个** sid 还有效吗" —— 单键 GET 即可，O(1)；
#      用集合则要读回整组成员再判断，开销随该用户设备数增长。
#   2. 每个会话需要**自己的** TTL。集合的过期是整体性的，给新设备续期会顺带
#      把旧设备的过期时间一起推后 —— 于是"某台设备该掉线"这件事永远不发生。
#   3. 键本身就是身份 ⇒ **登出不再需要 CAS**。原来的 CAS（比较并删除）是为了在
#      "一个用户只有一个槽位"时避免误伤；现在每个会话各有自己的键，删自己那把
#      不可能碰到别人。所以 _LUA_CAS_DEL 与比较逻辑一并删除。
#
# ⚠️ 代价：校验变成**无条件**读存储。多副本部署下这要求共享后端 —— 副本 A 签发的
# 会话，副本 B 必须查得到。内存后端 + 多副本会让用户随机 401，所以启动期点名告警
# （main._assert_auth_policy）。

# Redis Lua：取值并删除（refresh 单次使用语义的原子实现）。
_LUA_GETDEL = (
    "local v = redis.call('GET', KEYS[1]) "
    "if v then redis.call('DEL', KEYS[1]) end return v"
)
# Redis Lua：**仅当键存在时**续期。
# ⚠️ 不能写成无条件的 `EXPIRE` —— 那会让一个**已被登出**的会话被 refresh 复活：
# 键已经没了，EXPIRE 对不存在的键返回 0 而不创建它，看似没事；但"先 SET 再
# EXPIRE"或"GET 判空再 SET"的两步写法在并发登出下都有窗口。用脚本一次判定。
_LUA_TOUCH = (
    "if redis.call('GET', KEYS[1]) then "
    "return redis.call('EXPIRE', KEYS[1], ARGV[1]) end return 0"
)

_SESSION_PREFIX = "auth:sess:"
_REFRESH_PREFIX = "auth:refresh:"


def _key_escape(value):
    """转义键里可能出现的分隔符。

    ⚠️ 为什么必须做 —— 这是引入"每会话一个键"时**新出现**的越权删除风险：
    键格式是 `auth:sess:{user}:{sid}`，而 `close_all_sessions` 靠前缀
    `auth:sess:{user}:` 去匹配该用户的所有会话。如果 user_id 本身含 `:`，
    前缀就会**跨用户**匹配：

        user="a"   的会话键   auth:sess:a:S1
        user="a:b" 的会话键   auth:sess:a:b:S2
        close_all_sessions("a") 的前缀 auth:sess:a: 会命中上面**两条**

    于是"登出我的所有设备"把另一个用户的会话一起删了。只在特定用户名下复现，
    平时测不出来 —— 属"不做转义就会静默发生"的缺陷。

    转义只影响含 `%` 或 `:` 的取值：常规用户名（`alice`、`alice@corp.com`）
    保持原样，排障时键仍然一眼能读。
    """
    return (value or "").replace("%", "%25").replace(":", "%3A")


def _session_key(user_id, session_id):
    """单个会话的键。sid 由本服务生成（token_urlsafe，仅 [A-Za-z0-9_-]），
    但一并转义 —— 防御成本为零，且不依赖"上游永不变"这个假设。"""
    return f"{_SESSION_PREFIX}{_key_escape(user_id)}:{_key_escape(session_id)}"


def _session_prefix(user_id):
    """该用户**所有**会话键的公共前缀（供 close_all_sessions 使用）。"""
    return f"{_SESSION_PREFIX}{_key_escape(user_id)}:"


def _refresh_hash(token):
    """refresh token 落库前先哈希。

    理由同密码：**存储里不应出现可直接使用的凭证**。日志转储、备份、Redis 快照
    都可能外泄；存哈希后，拿到快照的人也无法用它换取会话。
    这里用单轮 SHA-256 即可（无需 PBKDF2）—— token 本身是 48 字节高熵随机串，
    不存在"可被字典/暴力破解"的前提，慢哈希只会白白增加每次刷新的延迟。
    """
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


class _MemorySessionBackend:
    """进程内会话存储（单实例 / 未配 REDIS_URL 时）。带惰性过期清理。"""

    #: 触发一次过期扫描的条目阈值
    _SWEEP_AT = 4096

    def __init__(self):
        self._data = {}          # key -> (value, expire_at)
        self._lock = threading.Lock()

    def _sweep(self, now):
        dead = [k for k, (_, exp) in self._data.items() if exp and exp <= now]
        for k in dead:
            self._data.pop(k, None)

    def set(self, key, value, ttl):
        now = time.time()
        with self._lock:
            if len(self._data) >= self._SWEEP_AT:
                self._sweep(now)
            self._data[key] = (value, now + ttl if ttl > 0 else 0)

    def get(self, key):
        now = time.time()
        with self._lock:
            item = self._data.get(key)
        if not item:
            return None
        value, exp = item
        if exp and exp <= now:
            with self._lock:
                self._data.pop(key, None)
            return None
        return value

    def touch(self, key, ttl):
        """**仅当键存在且未过期时**续期；否则返回 False。

        返回 False 是"这个会话已经没了"，调用方据此拒绝续期 —— 不能顺手把它
        创建出来，否则一个被登出的会话会被 refresh 复活。
        """
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if not item:
                return False
            value, exp = item
            if exp and exp <= now:
                self._data.pop(key, None)
                return False
            self._data[key] = (value, now + ttl if ttl > 0 else 0)
            return True

    def delete(self, key):
        with self._lock:
            return self._data.pop(key, None) is not None

    def delete_prefix(self, prefix):
        """删除所有以 prefix 开头的键，返回删除条数。"""
        with self._lock:
            keys = [k for k in self._data if k.startswith(prefix)]
            for k in keys:
                self._data.pop(k, None)
        return len(keys)

    def pop(self, key):
        with self._lock:
            item = self._data.pop(key, None)
        if not item:
            return None
        value, exp = item
        if exp and exp <= time.time():
            return None
        return value


class _RedisSessionBackend:
    """Redis 会话存储（多副本共享 —— 会话白名单只在共享后端下才跨副本成立）。"""

    def __init__(self, url):
        import redis

        self._r = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
        # 同 ratelimit._RedisBackend：`from_url` 是懒连接，不主动 ping 的话
        # "地址写错/服务没起"要等第一个真实请求才暴露，而那时异常被上层吞掉 ——
        # 结果就是"配了 REDIS_URL、日志一行 warning、实际多副本下随机 401"。
        self._r.ping()

    def set(self, key, value, ttl):
        self._r.set(key, value, ex=max(1, int(ttl)))

    def get(self, key):
        v = self._r.get(key)
        return v.decode("utf-8") if isinstance(v, bytes) else v

    def touch(self, key, ttl):
        return bool(self._r.eval(_LUA_TOUCH, 1, key, max(1, int(ttl))))

    def delete(self, key):
        return bool(self._r.delete(key))

    def delete_prefix(self, prefix):
        """删除该前缀下的所有键，返回删除条数。

        ⚠️ 用 `scan_iter` 游标而不是 `KEYS`：`KEYS` 会**阻塞整个 Redis 实例**
        （单线程模型），会话量大时表现为全站卡顿 —— 即"一个人登出所有设备，
        把所有其他人一起卡住"。SCAN 是增量式的，代价是期间新增的键可能扫不到，
        这里可以接受（那些键属于登出之后才建立的会话）。
        """
        removed = 0
        for key in self._r.scan_iter(match=prefix + "*", count=500):
            removed += int(self._r.delete(key) or 0)
        return removed

    def pop(self, key):
        v = self._r.eval(_LUA_GETDEL, 1, key)
        return v.decode("utf-8") if isinstance(v, bytes) else v


class SessionStore:
    """会话白名单 + refresh token。

    白名单语义：**一个键 = 一个仍然有效的会话**（用户 × 设备）。登录新增一个键，
    登出删掉自己的那个键，该用户的其它会话完全不受影响。
    """

    def __init__(self, redis_url="", enabled=True):
        """构造。

        redis_url: 非空则尝试 Redis；失败**回退内存并告警**（不是拒绝启动）。
            ⚠️ 回退内存的影响比上一版**更严重**：会话校验现在是**无条件**读存储，
            副本 A 签发的会话在副本 B 查不到 ⇒ 用户按请求落在哪个副本随机收到 401，
            表现为"刚登录成功、隔几次请求又要重新登录"。这个事实必须可见，
            所以后端名会进启动日志（`auth.user_auth.session_backend`），
            并且"多副本 + 内存后端"会在启动期被点名告警
            （main._assert_session_backend_policy）。
        """
        self.enabled = bool(enabled)
        self._local = _MemorySessionBackend()
        self.backend = self._local
        self.backend_name = "memory"
        if redis_url:
            try:
                self.backend = _RedisSessionBackend(redis_url)
                self.backend_name = "redis"
                logging.info("会话白名单后端：redis（多副本共享，会话跨副本有效）")
            except ImportError as e:
                logging.error("REDIS_URL 已配置但未安装 redis 客户端 ⇒ 会话白名单在进程内，"
                              "多副本下用户会**随机收到 401**。请 `pip install redis`。原因：%s", e)
            except Exception as e:  # noqa: BLE001
                logging.error("REDIS_URL 已配置但连接不可用 ⇒ 会话白名单在进程内，"
                              "多副本下用户会**随机收到 401**。请检查 REDIS_URL 与网络。原因：%s", e)

    # ---------- 会话白名单 ----------
    def open_session(self, user_id, session_id, ttl):
        """登录：登记一个**新**会话。

        ⚠️ 不再覆盖该用户已有的会话 —— 这正是本次改动的目的（多设备并存）。
        "在别处登录会自动踢掉旧的"是上一版单槽位锚点的语义，已随互斥一并移除。
        """
        if not user_id or not session_id:
            return
        self.backend.set(_session_key(user_id, session_id), str(int(time.time())), ttl)

    def has_session(self, user_id, session_id):
        """该会话是否仍在白名单里。

        这是**每一次带会话 token 的请求**都要走的一步，所以刻意选了"单键存在性
        判断"而不是"读回该用户的会话集合再查找" —— 后者开销随设备数增长。
        """
        if not user_id or not session_id:
            return False
        return self.backend.get(_session_key(user_id, session_id)) is not None

    def touch_session(self, user_id, session_id, ttl):
        """续期（滑动窗口）；**会话已不存在时不创建**。

        返回值必须被调用方检查：返回 False 表示这个会话已经没了（登出过了），
        此时不能续期 —— 否则一个被登出的会话会被 refresh 悄悄复活。
        """
        if not user_id or not session_id:
            return False
        return bool(self.backend.touch(_session_key(user_id, session_id), ttl))

    def close_session(self, user_id, session_id):
        """登出**这一台**设备。

        不需要 CAS：键里已经含 session_id，删自己的键不可能碰到别人的会话 ——
        这正是"每会话一键"相比单槽位锚点最直接的收益（原实现必须靠 CAS 才能
        避免"拿旧 token 反复登出打掉别人新会话"的 DoS）。
        """
        if not user_id or not session_id:
            return False
        return bool(self.backend.delete(_session_key(user_id, session_id)))

    def close_all_sessions(self, user_id):
        """登出该用户的**所有**设备，返回清除的会话数。

        它是"互斥被移除"的补偿：过去"在别处登录"会自动顶掉旧会话，现在不会了，
        所以需要一个显式的清场动作（改密码后、发现凭证泄漏时、员工离职）。

        作用域严格限制在**单个 user_id** 上，前缀经 `_key_escape` 转义 ——
        避免含 `:` 的用户名跨用户匹配（见 `_key_escape` 的说明）。
        """
        if not user_id:
            return 0
        return int(self.backend.delete_prefix(_session_prefix(user_id)))

    # ---------- refresh token ----------
    def save_refresh(self, token, payload, ttl):
        """保存 refresh token（存哈希）。"""
        self.backend.set(_REFRESH_PREFIX + _refresh_hash(token), json.dumps(payload), ttl)

    def consume_refresh(self, token):
        """取出并删除（**单次使用**）；不存在/已用过返回 None。

        ⚠️ "取出并删除"必须原子（GETDEL / Lua）。先 GET 判断再 DEL 的两步写法，
        在并发重放下会让**两个请求同时通过** —— 而 refresh token 的轮换语义
        正是靠"旧的一次性作废"保证的；破了它，一个被截获的 refresh token
        就能被攻击者长期复用，轮换形同虚设。
        """
        raw = self.backend.pop(_REFRESH_PREFIX + _refresh_hash(token))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None


# ---------------------------------------------------------------------------
# 组合层
# ---------------------------------------------------------------------------


class Principal:
    """一次已认证的用户身份（`main.Identity` 的用户侧对应物）。

    `tenant` 与 `roles` 同样来自**可信来源**（已验签的 token claim 或本地用户表），
    不是客户端自报 —— 这是与 `X-Tenant-Id` 的根本区别。
    """

    __slots__ = ("user", "tenant", "roles", "session_id", "expires_at")

    def __init__(self, user, tenant, roles=(), session_id="", expires_at=0):
        self.user = user
        self.tenant = tenant
        self.roles = tuple(roles or ())
        self.session_id = session_id
        self.expires_at = expires_at

    def has_role(self, allowed):
        """是否具备 `allowed` 中的任一角色（空 `allowed` 表示不做限制）。"""
        if not allowed:
            return True
        return bool(set(self.roles) & set(allowed))


class AuthService:
    """用户鉴权的组合层：验 token → 查会话白名单 → 产出 `Principal`。

    入口：
      - `verify_session_token(token)` —— 外来请求带的 Bearer token（本服务签发）
      - `login(...)` / `refresh(...)` / `logout(...)` —— 认证端点用
    """

    def __init__(self, store, accounts=None):
        self.store = store
        #: 账号表（`app/accounts.py` 的 `AccountStore`）。
        #: None = 账号数据不可用 ⇒ 密码登录恒失败（也就**没有任何人能登录**）。
        #: 刻意用"构造时传入对象"而不是 import：本模块要读用户，而 `accounts`
        #: 需要知道"用户长什么样"，互相 import 会成环；依赖注进来最干净。
        self.accounts = accounts

    # ---------- 自签会话 token ----------
    @property
    def can_issue(self):
        """能否签发本服务会话 token（需要独立的对称密钥）。"""
        return bool(config.AUTH_SESSION_SECRET)

    @property
    def session_ttl(self):
        """会话白名单条目的存活时间（也是滑动续期的步长）。

        取 access 与 refresh 的**较大者**：白名单条目必须覆盖到 refresh token
        还能用的整段时间 —— 否则会出现"access 已过期、用户拿 refresh 换新 access，
        但白名单条目已自己过期消失了"，续期路径被判成 `session_revoked`，
        表现为"闲一会儿回来就要重新登录"（该会话还有 refresh、却被提前作废）。

        注意它现在是**每会话独立**的起点：多设备并存时每台各自计时、各自续期，
        不存在"某台设备活动顺带延长另一台寿命"的问题（这正是没用集合/哈希的原因）。
        """
        return max(config.AUTH_ACCESS_TTL, config.AUTH_REFRESH_TTL)

    def issue(self, user, tenant, roles, *, session_id=None):
        """签发会话三元组：(access_token, refresh_token, session_id)。

        调用方负责先 `store.open_session(...)` 再调这里 —— 顺序不能反：
        先发 token 再登记会话的话，会存在一个"token 已签发、白名单里却还没有"
        的窗口，该窗口内这个 token 一被使用就会被判为无效（校验就是查白名单的）。
        先登记后签发则不存在这个窗口。
        """
        import jwt

        if not self.can_issue:
            raise AuthError("未配置 AUTH_SESSION_SECRET，无法签发会话 token",
                            status_code=503, code="auth_misconfigured")
        session_id = session_id or secrets.token_urlsafe(32)
        now = int(time.time())

        access = jwt.encode(
            {
                "iss": config.AUTH_ISSUER,
                "sub": user,
                "tenant": tenant,
                "roles": list(roles or ()),
                # jti 就是会话 id：验签后拿它去白名单里做**存在性**判断
                # （不做比对 —— 每个会话各占一个键，不存在"谁覆盖谁"）。
                # 白名单里只存"这个 sid 还作不作数"，完整会话信息不落库。
                "jti": session_id,
                "typ": "access",
                "iat": now,
                "exp": now + config.AUTH_ACCESS_TTL,
            },
            config.AUTH_SESSION_SECRET,
            algorithm="HS256",
        )
        refresh = secrets.token_urlsafe(48)
        self.store.save_refresh(
            refresh,
            {"user": user, "tenant": tenant, "roles": list(roles or ()),
             "sid": session_id},
            config.AUTH_REFRESH_TTL,
        )
        return access, refresh, session_id

    def login_principal(self, principal):
        """把已认证的 `Principal` 变成一次**新会话**，返回 (access, refresh, session_id)。

        ⚠️ 顺序：**先登记会话、再签发 token**。反过来会留下一个窗口 —— token 已经
        发出去了，但白名单里还没有这个 sid，于是它一被使用就报"会话已失效"。
        先登记后签发则不存在这个窗口。

        这个封装存在的另一个理由：调用方（端点）不该自己去拼接
        `open_session + issue` —— 顺序一旦写反就是一个静默的功能缺陷。

        多设备：每次调用新增一个会话，**不影响该用户已在线的其它设备**。
        """
        session_id = secrets.token_urlsafe(32)
        self.store.open_session(principal.user, session_id, self.session_ttl)
        return self.issue(principal.user, principal.tenant, principal.roles,
                          session_id=session_id)

    def verify_session_token(self, token):
        """验本服务签发的 access token（HS256），并校验该会话仍在白名单里。

        白名单校验是这里唯一有状态的一步：`jti` 必须仍对应一个有效会话。
        """
        import jwt

        if not self.can_issue:
            raise AuthError("服务未配置会话密钥", status_code=503, code="auth_misconfigured")
        try:
            claims = jwt.decode(
                token,
                config.AUTH_SESSION_SECRET,
                algorithms=["HS256"],
                issuer=config.AUTH_ISSUER,
                options={"require": ["exp", "iss"]},
            )
        except Exception as e:  # noqa: BLE001
            metrics.inc("auth_token_rejected_total", reason="invalid_token")
            # ⚠️ **不要把 `e` 拼进 detail**（2026-10-06 修）：它是 PyJWT 的原始异常文本
            # （"Signature verification failed" / "The specified alg value is not allowed"
            # 等），而 main.py 会把 detail 原样下发到 401 响应体 ⇒ 等于把**验签失败的具体
            # 判据**告诉调用方（"签名不对"与"算法不被允许"指向完全不同的攻击路径）。
            # 异常细节只进服务端日志，客户端一律一句通用文案。
            logging.warning("会话 token 校验失败（%s）：%s", type(e).__name__, e)
            raise AuthError("会话 token 无效或已过期", code="invalid_token") from e

        user = claims.get("sub") or ""
        sid = claims.get("jti") or ""
        if not user or not sid:
            raise AuthError("会话 token 缺少 sub/jti", code="invalid_token")

        # ⚠️ 这一步是**无条件**的（旧版有 AUTH_SINGLE_SESSION 开关可以跳过）。
        # 跳过它就等于"登出只是客户端行为" —— 而 logout 端点照样返回 200，
        # 造成"看起来撤销了、实际那个 token 一直有效到过期"。
        if not self.store.has_session(user, sid):
            # 这是"会话已被撤销"的唯一可观测出口：token 本身完全合法、签名正确、
            # 未过期，只是已经不在白名单里了。
            # 单独一个指标名，才能把它和"token 过期/被伪造"区分开 —— 否则运维
            # 看到的只是 401 总量上升，无从判断是攻击、过期还是用户自己登出了。
            metrics.inc("auth_session_revoked_total")
            raise AuthError("会话已失效，请重新登录",
                            status_code=401, code="session_revoked")

        return Principal(
            user=user,
            # ⚠️ 刻意**不**做 `or config.DEFAULT_TENANT` 兜底：租户缺失就是**空串**，
            # 含义是"这个人没有租户"。空串在检索层恰好等于"不加过滤"（能看到全部
            # 内容）—— 见 main._retrieval_tenant 的口径说明。用配置兜底会把这个
            # 事实静默改写成一个具体租户名，而两者下游行为完全不同。
            tenant=claims.get("tenant") or "",
            roles=tuple(claims.get("roles") or ()),
            session_id=sid,
            expires_at=int(claims.get("exp") or 0),
        )

    # ---------- 认证端点用 ----------
    def login_with_password(self, username, password):
        """账号表校验 → `Principal`（不含 session_id，由调用方绑定）。

        ⚠️ 用户不存在时也要**做一次哈希运算**再返回失败：否则"用户存在"
        （耗时 ~50ms）与"用户不存在"（耗时 ~0ms）的差异可以枚举出有效用户名。
        用一条固定的假哈希做等价计算。

        ⚠️ 停用的账号（`status=disabled`）**对外与"密码错误"完全一致** ——
        不区分"没这个人""密码错""账号被停用"。区分它们等于白送一个账号枚举器
        （能问出"这个用户名存在吗"），而这一条在登录接口上是最典型的攻击手法。

        ⚠️ `tenant` 为空串是**合法结果**：含义是"这个人还没有租户"，
        不是"读配置兜底"。这是 2026-10-04 定下的口径 —— 没有租户就为空。
        """
        user = self.accounts.get_user(username) if self.accounts else None
        if user is None or user.get("status") != "active":
            verify_password(password, _DUMMY_HASH)
            metrics.inc("auth_login_total", result="failed")
            raise AuthError("用户名或密码错误", code="invalid_credentials")
        if not verify_password(password, user.get("password_hash") or ""):
            metrics.inc("auth_login_total", result="failed")
            raise AuthError("用户名或密码错误", code="invalid_credentials")
        metrics.inc("auth_login_total", result="ok")
        return Principal(user=username, tenant=user.get("tenant") or "",
                         roles=tuple(user.get("roles") or ()))

    def refresh(self, refresh_token):
        """用 refresh token 换新的会话三元组（**轮换**，sid 保持不变）。

        顺序很关键：先消费掉旧的（单次使用），再校验会话、再签发新的。
        反过来（先签发再消费）会让"重放旧 refresh"在并发下也能拿到新 token。
        """
        payload = self.store.consume_refresh(refresh_token)
        if not payload:
            metrics.inc("auth_refresh_total", result="failed")
            raise AuthError("refresh token 无效或已被使用", code="invalid_refresh_token")

        user = payload.get("user") or ""
        sid = payload.get("sid") or ""

        # ⚠️ **刷新沿用同一个 sid，不新建会话。** 这一点是必须的，两个理由：
        #
        #  1. 否则无界增长。每次刷新都新建 sid 的话，白名单条数 = 刷新次数而不是
        #     登录次数 —— 一个长期在线的客户端每小时刷一次，几天就能堆出上千条
        #     永不回收的会话键。这与本模块当初拒绝"撤销列表"是同一个理由。
        #  2. 语义上"刷新"就是同一个会话续期（还是这台设备、还是这次登录），
        #     换 sid 等于把"设备"这个概念打散。
        if not self.store.has_session(user, sid):
            # 会话已被撤销（本人登出、登出所有设备、或密码轮换清场）。
            # 旧 refresh 已在上面被消费掉，所以它一并作废 —— 不存在
            # "登出之后还能拿手里的 refresh 把会话复活"这条路。
            metrics.inc("auth_session_revoked_total")
            raise AuthError("会话已失效，请重新登录",
                            status_code=401, code="session_revoked")

        # 租户与角色**从账号表重新读**，不用 refresh token 里那份签发时的快照。
        # 理由：管理员把某人移出租户、或收回他的角色之后，若沿用快照，那个变更要等
        # refresh token 自然过期（默认 7 天）才生效 —— 而"收回权限"必须是**立刻**的。
        # 顺带让"停用账号"能通过清会话当场生效（见管理接口）。
        fresh = self.accounts.get_user(user) if self.accounts else None
        if fresh is None or fresh.get("status") != "active":
            metrics.inc("auth_session_revoked_total")
            raise AuthError("账号已停用或不存在，请重新登录",
                            status_code=401, code="session_revoked")

        # 滑动续期：只要在活跃使用就延长会话寿命（与 refresh token 的 TTL 同源）。
        # ⚠️ 返回值**必须**检查（`SessionStore.touch_session` 的 docstring 明确要求）：
        # 返回 False 表示这个会话已经不在了。忽略它会让**刚被登出 / 被权限变更清场**的
        # 会话被这次刷新悄悄复活 —— 上面那次 `has_session` 与本行之间隔着一次 IO，
        # 并发登出正好落在这个窗口里。
        if not self.store.touch_session(user, sid, self.session_ttl):
            metrics.inc("auth_session_revoked_total")
            raise AuthError("会话已失效，请重新登录",
                            status_code=401, code="session_revoked")
        metrics.inc("auth_refresh_total", result="ok")
        # 空串租户是合法结果（"这个人还没有租户"），不做 DEFAULT_TENANT 兜底。
        return self.issue(user, fresh.get("tenant") or "",
                          fresh.get("roles") or (), session_id=sid)

    def logout(self, user, session_id):
        """登出**当前这一台**设备（不影响该用户的其它设备）。"""
        ok = self.store.close_session(user, session_id)
        metrics.inc("auth_logout_total", result="ok" if ok else "stale")
        return ok

    def logout_all(self, user):
        """登出该用户的**所有**设备，返回被清除的会话数。

        多设备并存下的清场手段（改密码后、凭证泄漏、员工离职）。这是"互斥被移除"
        之后的补偿能力 —— 过去"在别处登录"会自动顶掉旧会话，现在必须显式触发。

        ⚠️ 作用域只到**单个** user_id。调用方必须传已验签凭证里的 user，
        **绝不能**接受请求参数指定的用户名 —— 那会变成一个任意用户踢人接口。
        """
        n = self.store.close_all_sessions(user)
        metrics.inc("auth_logout_all_total", result="ok" if n else "none")
        return n


#: 用户不存在时用来"等价耗时"的假哈希（内容无意义，只保证走同一条 PBKDF2 路径）。
#: 迭代数取模块默认值，且是**模块导入时**生成一次 —— 每次调用现算会让
#: "不存在"比"存在"更慢，反而泄露信息。
_DUMMY_HASH = hash_password("dummy-password-for-timing-equalization")


def build_auth_service(accounts=None):
    """按 app.config 装配 AuthService（供 main.py 调用）。

    accounts: 账号表（`app/accounts.py` 的 `AccountStore`）。**刻意从参数传入**
        而不是在这里构造：本模块只负责"验凭证"，不负责"账号数据存哪" ——
        存储形态（PG / 内存）是服务层的事（`main._ACCOUNTS`）。
    """
    store = SessionStore(
        redis_url=config.REDIS_URL,
        enabled=config.AUTH_ENABLED,
    )
    return AuthService(store, accounts=accounts)
