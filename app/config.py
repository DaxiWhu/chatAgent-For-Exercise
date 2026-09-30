"""服务层集中配置：把散落在各处的 `os.getenv` 收到一处，便于审计与文档化。

为什么单独一层
--------------
生产化引入的开关越来越多（限流、日志格式、熔断、配额、tracing、审核通知、
租户、灰度标识…）。如果继续在 `main.py` / `graph.py` / `retrieval.py` 里各写
`os.getenv`，会很快失去"到底有哪些配置项"的全貌，新人改错一个名字还静默生效。

所以约定：**服务层（app/）读环境变量一律走本模块**；RAG 数据链路（src/rag/）
仍读自己的模块级常量（它们与检索行为强耦合，且被大量测试按名 monkeypatch）。

**已登记的例外**（都是刻意的，不是漏网；改这里时别顺手"收编"）：
1. `DB_URL` —— 在 `main.lifespan()` 与 `migrations` CLI 里读。前者需要"运行时仍可变"
   （容器注入 / 测试临时改），后者是独立 CLI，不该为了一个变量把整个 app 拉起来。
2. `APP_HOST/APP_PORT/APP_WORKERS/APP_RELOAD/APP_LOG_LEVEL` —— 在 `serve.py` 的
   argparse 默认值里读。它们只在"进程启动参数"这一层有意义，与请求期行为无关。

（历史例外 `DISCONNECT_POLL_INTERVAL` 已废弃删除：断连检测在 2026-09-29 改为
sse_starlette 的 `client_close_handler_callable` 回调，不再有轮询间隔这个参数。）
所有**影响请求期行为**的开关（尤其安全相关的）都必须进本模块，否则它不会出现在
**启动日志**的回显里 —— 运维配了却无法自证生效。

命名与默认值的两条原则
----------------------
1. **需要外部填写的（密钥、地址、配额）默认留空/为 0**，空即"未启用"，绝不猜一个
   看起来合理的值 —— 猜错会导致"以为配了其实没配"。
2. **能力开关默认开（on）**，但要保证"开着且没配置"时行为与关闭时一致（例如配额
   限额为 0 表示不限制）。这样默认值安全、且不改变既有行为。
"""

import logging
import os
from pathlib import Path


def _env_bool(name, default=False):
    """读布尔环境变量：1/on/true/yes 为真，0/off/false/no 为假，其余取默认。"""
    v = (os.getenv(name) or "").strip().lower()
    if v in ("1", "on", "true", "yes"):
        return True
    if v in ("0", "off", "false", "no"):
        return False
    return default


def _env_int(name, default):
    """读整型环境变量，非法值退回默认（不抛异常，避免一个手滑的 .env 让服务起不来）。"""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_str(name, default=""):
    """读字符串环境变量并 strip（.env 里常见的尾随空格是事故源）。"""
    v = os.getenv(name)
    return default if v is None else v.strip()


# --------------------------------------------------------------------------
# 应用标识（灰度/版本）
# --------------------------------------------------------------------------
def _read_version() -> str:
    """读应用版本号。**唯一真源是仓库根的 `VERSION` 文件。**

    优先级：环境变量 `APP_VERSION`（容器 / CI 按发布标签注入）→ `VERSION` 文件。
    两者都取不到时返回 `0.0.0+unknown` —— 刻意**不猜**一个版本号：`/health` 与
    启动日志回显的版本一旦是编的，"线上跑的是哪一版"就会得出错误结论，
    比明确说"不知道"更糟。

    为什么不在本模块硬编码：版本号曾同时写在这里、`.env`、compose 的镜像 tag
    三处，改一处漏两处时"服务自报的版本"与"镜像 tag"会不一致，且**没有任何信号**。
    现在三处都收敛到 `VERSION`（守卫见 tests/test_version_source.py）。
    """
    v = _env_str("APP_VERSION", "")
    if v:
        return v
    try:
        return (Path(__file__).resolve().parents[1] / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return "0.0.0+unknown"


APP_VERSION = _read_version()
# stable / canary：同名镜像的两个实例用它区分，/health 会回显，便于灰度期核对流量去向
APP_RELEASE = _env_str("APP_RELEASE", "stable")

# --------------------------------------------------------------------------
# 运行环境：决定若干"生产默认收紧"的开关
# --------------------------------------------------------------------------
# development（默认）| production。用 APP_ENV=production 显式声明生产环境，
# 而不是靠"猜部署形态" —— 下面几个开关的默认值都由它推导。
APP_ENV = _env_str("APP_ENV", "development").lower()
IS_PRODUCTION = APP_ENV in ("production", "prod")

# --------------------------------------------------------------------------
# 鉴权（P0-1）
# --------------------------------------------------------------------------
# 空 = 关闭鉴权（仅限本地开发）。生产必须填。
SERVE_API_KEY = _env_str("SERVE_API_KEY", "")

# 强制鉴权：生产默认开。开启时**未配置任何密钥就拒绝启动** ——
# "裸奔上线"是最常见的生产事故，宁可起不来也不要以无鉴权状态接管流量。
REQUIRE_AUTH = _env_bool("REQUIRE_AUTH", IS_PRODUCTION)

# API 文档（/docs、/redoc、/openapi.json）：生产默认关闭。
# 接口清单+参数结构对公网可见等于把攻击面画成地图；内网/开发保留以便联调。
ENABLE_DOCS = _env_bool("ENABLE_DOCS", not IS_PRODUCTION)

# CORS 允许来源（逗号分隔）。默认 `*` 方便本地联调；生产应给白名单，
# 如 `https://app.example.com,https://admin.example.com`。
# 注意 allow_credentials 恒为 False（鉴权走 X-API-Key 请求头，不用 Cookie），
# 所以配 `*` 在浏览器侧是合法的 —— 但仍建议生产收紧。
CORS_ALLOW_ORIGINS = [o.strip() for o in
                      _env_str("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()] or ["*"]

# --------------------------------------------------------------------------
# 请求体积上限（应用层，独立于反向代理）
# --------------------------------------------------------------------------
# 为什么应用层还要再拦一道
# ----------------------
# Nginx 的 `client_max_body_size` 只在"流量确实经过 nginx"时生效。以下场景会**绕过**它：
#   - 内部服务直连（绕过网关做联调、压测、灰度对比）；
#   - 部署形态里没有 nginx（直接 `python -m app.serve` 对外）；
#   - 未来的 Sidecar / Service Mesh 入口未同步该限制。
# 而 `ChatReq.message` 过去是一个**无长度约束的 str**：一个 500MB 的 JSON 会先被
# 整个读进内存（uvicorn 默认无上限）再交给 Pydantic，进程直接被撑爆 —— 这是
# "不需要任何漏洞"就能打出的故障。所以上限必须做在应用自己这一侧。
#
# 两层各管一件事，**不能互相替代**：
#   ① `MAX_REQUEST_BYTES`（字节）：ASGI 中间件在**读取阶段**就截断，拦下超大体量，
#      连解析都不做。它防的是"内存被撑爆"。
#   ② `MAX_MESSAGE_CHARS` 等（字符）：Pydantic 字段级校验，给客户端一个**语义明确**
#      的 422（哪個字段超了多少），而不是笼统的 413。它防的是"合法体积内的失控输入"。
# 顺序上 ① 先于 ②：400KB 的 message 会先撞 ①？不会 —— 400KB < 1MiB 所以穿过 ①，
# 再被 ② 以 422 精确拒绝。两层阈值刻意不同量级，避免"413 掩盖了真正的字段错误"。
MAX_REQUEST_BYTES = max(1024, _env_int("MAX_REQUEST_BYTES", 1048576))   # 1 MiB
MAX_MESSAGE_CHARS = max(1, _env_int("MAX_MESSAGE_CHARS", 100000))
MAX_FEEDBACK_CHARS = max(1, _env_int("MAX_FEEDBACK_CHARS", 20000))
# thread_id 会被用作检查点主键、限流桶键与归属表主键 ⇒ 必须有界
MAX_THREAD_ID_CHARS = max(1, _env_int("MAX_THREAD_ID_CHARS", 128))

# --------------------------------------------------------------------------
# 请求级总超时（P1，2026-09-29）
# --------------------------------------------------------------------------
# 为什么需要它
# ----------
# 在此之前唯一的超时是 LLM client 的 `timeout=30`，它只约束**单次 HTTP 调用**。
# 而一次 `/v1/chat` 的图是「检索 → 生成 → 自评审 → （不满意）再审 → 再生成」，
# 每一轮都是一次独立的 30s 调用 ⇒ **一次合法请求的总时长没有任何上界**。
# 上游慢（限流重试、排队）时更糟：实测过一次 429 重试持续 36 秒，期间客户端
# 早已断开（见 `main._watch_disconnect`）。
#
# 没有总时限的代价不是"慢"，而是三件具体的事：
#   ① 连接与进程内资源被长期占用 —— 优雅关闭的"排空上界"（`GRACEFUL_SHUTDOWN_TIMEOUT`）
#      因此形同虚设：它等的是在途请求，而在途请求自己不会结束；
#   ② 用户在没有任何反馈的情况下等待（没有"这次请求已经跑太久"的信号）；
#   ③ 出故障时拿不出"一次请求最久多久"这个数字去对账、去设告警。
#
# 超时后的行为（不是简单掐断）
# --------------------------
# 下发一个结构化的 `timeout` SSE 事件（`{"seconds":..., "produced_chars":...}`）
# 后收流，**已经产出的正文照常交付**（与 `budget_exceeded` 同一取舍：作废整次回答
# 比截断更亏）。详见 `main._RequestDeadline`。
#
# 为什么默认 0（不限）
# ----------------
# 沿用本模块的默认值原则②："能力开关默认开，但**开着且没配**时行为必须与关闭
# 一致"。给一个"看起来合理"的默认值反而会**静默截断**长但合法的回答 ——
# 那是最难排查的一类故障：内容少了一半、日志里却什么都没有。
# 生产建议显式配置（见 `PRODUCTION_CONFIG.md`）；`main._warn_about_missing_timeout`
# 会在"生产 + 未配"时打一条可检索的告警。
REQUEST_TIMEOUT_SECONDS = max(0, _env_int("REQUEST_TIMEOUT_SECONDS", 0))

# --------------------------------------------------------------------------
# 数据保留期（合规与容量）
# --------------------------------------------------------------------------
# 检查点（完整对话历史）、`review_queue`（草稿与审核意见）、`session_owners` 三类
# 数据此前**永久保留**，没有任何回收机制。两个后果：
#   ① 合规：含 PII 的对话长期驻留，用户要求删除时无从履行；
#   ② 容量：检查点表存**每条消息的完整状态**，表随对话量无界增长 ——
#      它在某个时间点会突然从"慢"变成"不可用"（VACUUM 跟不上、索引膨胀、
#      备份窗口拉长），而在此之前没有任何指标会预警。
#
# 为什么默认 0（不自动清理）
# ------------------------
# 沿用本模块的默认值原则②：**删数据是不可逆的**，给一个"看起来合理"的默认值
# （比如 30 天）意味着一次升级就会静默删掉用户的历史会话。0 = 不清理，
# 由 `scripts/retention_sweep.py` 按显式配置执行；生产必须显式设置。
#
# 单位是天。清理动作由脚本完成（不经 HTTP 触发）：删除是运维动作，
# 不该暴露成一个可被调用的端点 —— 那会多出一个"谁能触发全量清理"的权限问题。
RETENTION_DAYS = max(0, _env_int("RETENTION_DAYS", 0))

# --------------------------------------------------------------------------
# 反向代理（P0-2 限流的判据来源）
# --------------------------------------------------------------------------
# 是否信任 `X-Forwarded-For` 作为客户端 IP。
#
# 这是一个**必须与部署形态一致**的安全开关，两个方向都是真实故障：
#   - 部署在 Nginx/网关后面却设 0：限流按**代理的 IP** 统计 ⇒ 全体用户共用一个
#     计数桶，要么集体被限流误伤、要么额度被一个用户吃满。
#   - 直连公网却设 1：任何人加一个 `X-Forwarded-For: 1.2.3.4` 就换一个限流桶，
#     限流形同虚设（而这个头是客户端完全可以伪造的）。
# 默认 0（不信任）—— 默认值必须在"没配错"的方向上偏保守。
TRUST_PROXY_HEADERS = _env_bool("TRUST_PROXY_HEADERS", False)

# --------------------------------------------------------------------------
# 日志（P0-3）
# --------------------------------------------------------------------------
LOG_LEVEL = _env_str("LOG_LEVEL", "INFO").upper()
# json（生产，便于日志系统解析）| text（本地人眼友好）
LOG_FORMAT = _env_str("LOG_FORMAT", "json").lower()

# --------------------------------------------------------------------------
# 指标（P1-5）
# --------------------------------------------------------------------------
METRICS_ENABLED = _env_bool("METRICS_ENABLED", True)

# --------------------------------------------------------------------------
# 限流（P0-2）：固定窗口计数，IP 与会话两个维度叠加
# --------------------------------------------------------------------------
# 默认限流是开的；但默认额度给得宽松（120/分钟），避免误伤正常联调与测试。
RATE_LIMIT_ENABLED = _env_bool("RATE_LIMIT_ENABLED", True)
RATE_LIMIT_PER_MINUTE = max(1, _env_int("RATE_LIMIT_PER_MINUTE", 120))
# 按会话（thread_id）限流：跨端点累计，防止一个会话同时刷 /chat 与 /review
RATE_LIMIT_BY_SESSION = _env_bool("RATE_LIMIT_BY_SESSION", True)
RATE_LIMIT_SESSION_PER_MINUTE = max(1, _env_int("RATE_LIMIT_SESSION_PER_MINUTE",
                                                RATE_LIMIT_PER_MINUTE))
# 共享后端（多实例部署时必需）。空 = 进程内内存计数（单实例够用）
REDIS_URL = _env_str("REDIS_URL", "")

# --------------------------------------------------------------------------
# 配额（P1-6）：单请求 token 预算 + 用户日配额。0 = 不限制
# --------------------------------------------------------------------------
QUOTA_ENABLED = _env_bool("QUOTA_ENABLED", True)
LLM_MAX_TOKENS_PER_REQUEST = max(0, _env_int("LLM_MAX_TOKENS_PER_REQUEST", 0))
USER_DAILY_TOKEN_QUOTA = max(0, _env_int("USER_DAILY_TOKEN_QUOTA", 0))

# --------------------------------------------------------------------------
# 幂等键（客户端重试保护）
# --------------------------------------------------------------------------
# 客户端超时重试是常态（弱网、网关超时、切后台）。而 `/v1/chat` 是 SSE，
# 重试的代价不是"多读一次" —— 而是**重复生成一整轮回答**：
#   - 重复计费（token 真的被消耗了）；
#   - 同一 thread_id 被追加两条相同用户消息，图多跑一轮，历史里出现重复；
#   - 若停在审核态，`review_queue` 里可能出现重复待审项。
#
# 客户端带 `Idempotency-Key` 头时，服务端据它去重：同一键的重试**不重复执行**，
# 而是回 409 并引导客户端去 `GET /v1/sessions/{thread_id}` 取既有状态
# （SSE 无法"回放"已发出的流，所以拒绝重跑 + 指路，是这里唯一正确的语义）。
#
# 默认开，但**不带该头的请求行为完全不变** —— 沿用本模块的默认值原则②
# （"能力开关默认开，但开着且没配时行为必须与关一致"）。
IDEMPOTENCY_ENABLED = _env_bool("IDEMPOTENCY_ENABLED", True)
#: 键的保留时长（秒）。必须**明显长于**客户端的重试窗口，否则"重试时键已过期"
#: 等于没保护；也不能太长，否则会挡住用户"确实想再问一次"（那种情况客户端
#: 应当换一个新键 —— 键的语义是"这一次请求"，不是"这个问题"）。
IDEMPOTENCY_TTL_SECONDS = max(1, _env_int("IDEMPOTENCY_TTL_SECONDS", 600))
#: 进程内实现的最大条目数（配了 REDIS_URL 时不适用）。
#: 键由客户端提供 ⇒ 无界缓存是一个可被利用的内存放大面。
IDEMPOTENCY_MAX_ENTRIES = max(100, _env_int("IDEMPOTENCY_MAX_ENTRIES", 10000))

# --------------------------------------------------------------------------
# 健康检查（P0-4）
# --------------------------------------------------------------------------
# readiness 里探依赖的超时（秒）。探针要快，超时即判不健康
HEALTH_READY_TIMEOUT = max(1, _env_int("HEALTH_READY_TIMEOUT", 3))

# --------------------------------------------------------------------------
# 可观测接入（P1-5）：两个都留空则自动降级为"不接 tracing"
# --------------------------------------------------------------------------
LANGFUSE_PUBLIC_KEY = _env_str("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = _env_str("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST = _env_str("LANGFUSE_HOST", "")
# LangSmith / LangChain 官方 tracing
LANGSMITH_API_KEY = _env_str("LANGSMITH_API_KEY", "")
LANGCHAIN_PROJECT = _env_str("LANGCHAIN_PROJECT", "chatagent")

# --------------------------------------------------------------------------
# 审核队列与通知（P2）
# --------------------------------------------------------------------------
REVIEW_QUEUE_ENABLED = _env_bool("REVIEW_QUEUE_ENABLED", True)
# 有待审内容时 POST 这个地址（企微/飞书机器人、自建网关均可）。空 = 不通知
REVIEW_NOTIFY_WEBHOOK = _env_str("REVIEW_NOTIFY_WEBHOOK", "")
REVIEW_NOTIFY_TIMEOUT = max(1, _env_int("REVIEW_NOTIFY_TIMEOUT", 5))
# 通知体格式：generic（自建网关，JSON）| wecom（企业微信机器人）| feishu（飞书机器人）
REVIEW_NOTIFY_FORMAT = _env_str("REVIEW_NOTIFY_FORMAT", "generic").lower()

# --------------------------------------------------------------------------
# 多租户（P2）
# --------------------------------------------------------------------------
# 兼容用的兜底租户：仅在"未配置 TENANT_KEYS 的单租户模式"下生效。
DEFAULT_TENANT = _env_str("RAG_TENANT", "")
TENANT_HEADER = _env_str("TENANT_HEADER", "X-Tenant-Id")

# ---- 身份可信方案：把租户绑定到 API Key（生产多租户必须配） ----
# 格式：`tenantA:keyA,tenantB:keyB`（逗号分隔租户，冒号分隔租户与密钥）
#
# 为什么需要它：只有"一个全局 Key + 请求头 X-Tenant-Id"时，**租户是客户端自报的** ——
# 任何持有该 Key 的人改个 header 就能冒充别的租户，于是
#   ① 日配额可按租户切换绕过；② 审核队列的归属是假的；③ 配合 guessable 的
#   thread_id，还能串读到别人的会话历史。
# 配了 TENANT_KEYS 之后：租户由 Key **反查**得出，请求头只能用于一致性核对
# （不一致直接 403），不再参与身份判定。
TENANT_KEYS_RAW = _env_str("TENANT_KEYS", "")


def _parse_tenant_keys(raw):
    """解析 `tenant:key,tenant:key` → `{key: tenant}`。

    raw: 原始配置串。格式非法的条目**跳过并告警**（不抛异常 —— 一个手滑的
        逗号不该让服务起不来），重复 key 时后者覆盖前者并告警。
    返回 dict（空 dict = 未启用该模式）。
    """
    out = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            logging.warning("TENANT_KEYS 条目格式非法（应为 tenant:key），已跳过：%s", part)
            continue
        tenant, _, key = part.partition(":")
        tenant, key = tenant.strip(), key.strip()
        if not tenant or not key:
            logging.warning("TENANT_KEYS 条目租户或密钥为空，已跳过：%s", part)
            continue
        if key in out:
            logging.warning("TENANT_KEYS 出现重复密钥，后者覆盖前者（%s → %s）",
                            out[key], tenant)
        out[key] = tenant
    return out


TENANT_KEYS = _parse_tenant_keys(TENANT_KEYS_RAW)

# --------------------------------------------------------------------------
# 提示词注入防护（P2）
# --------------------------------------------------------------------------
GUARD_ENABLED = _env_bool("RAG_GUARD", True)
# warn（只告警，不改内容）| strip（剥离可疑句）| block（丢弃可疑块）
GUARD_ACTION = _env_str("RAG_GUARD_ACTION", "warn").lower()

# --------------------------------------------------------------------------
# 解析结果落盘缓存（P1-7）
# --------------------------------------------------------------------------
PARSE_CACHE_ENABLED = _env_bool("RAG_PARSE_CACHE", True)
# 空 = 语料目录下的 .parse_cache（与 .media/.ctx_cache 同级；glob 的 * 不匹配点号开头目录，安全）
PARSE_CACHE_DIR = _env_str("RAG_PARSE_CACHE_DIR", "")

# --------------------------------------------------------------------------
# 熔断（P1-6）
# --------------------------------------------------------------------------
CIRCUIT_ENABLED = _env_bool("RAG_BREAKER", True)
CIRCUIT_FAIL_THRESHOLD = max(1, _env_int("CIRCUIT_FAIL_THRESHOLD", 5))
CIRCUIT_RESET_SECONDS = max(1, _env_int("CIRCUIT_RESET_SECONDS", 30))

# --------------------------------------------------------------------------
# 模型与数据库（沿用既有变量名，集中在此以便一处看全）
# --------------------------------------------------------------------------
MODEL_NAME = _env_str("AGNES_MODEL", "agnes-2.0-flash")
API_KEY = _env_str("AGNES_API_KEY", "")
API_BASE = _env_str("AGNES_API_BASE", "https://apihub.agnes-ai.com/v1")
DB_URL = _env_str("DB_URL", "")
DOCS_DIR = _env_str("DOCS_DIR", "data")


def as_dict(runtime=None):
    """把关键配置导出成 dict（不含任何密钥值，只回显"是否已配置"）。

    用途**只有启动日志**（`app.main` 里 `logging.info("服务启动配置：%s", ...)`），
    刻意不挂到任何 HTTP 端点：里面的 `model` / `docs_dir` / 各开关名属于部署细节，
    而 `/health/*` 在反代下是公网可达的。要核对实例配置请看日志（结构化 JSON，
    按 `服务启动配置` 检索）。

    密钥类一律只回显布尔，绝不回显内容 —— 日志常被转发到第三方系统。

    runtime: 可选，运行期才知道的事实（如"共享后端到底是 redis 还是 memory"）。
        为什么要单独传进来：**配置写了 `REDIS_URL` 不等于共享后端真的生效** ——
        redis 包没装、地址连不上、ping 超时，都会静默退回进程内计数。
        只回显 `bool(REDIS_URL)` 会谎报"多副本共享已启用"，而这正是最危险的
        那种错误（以为额度是全局限的，实际是"配置值 × 副本数"）。
        调用方（`app.main`）在组件构造完成后把真实后端名传进来。
    """
    runtime = runtime or {}
    return {
        "app_version": APP_VERSION,
        "release": APP_RELEASE,
        "env": APP_ENV,
        "log_format": LOG_FORMAT,
        "log_level": LOG_LEVEL,
        "metrics": METRICS_ENABLED,
        "auth": {
            "required": REQUIRE_AUTH,
            "global_key": bool(SERVE_API_KEY),
            "tenant_keys": len(TENANT_KEYS),   # 只回显条数，绝不回显密钥
        },
        "docs": ENABLE_DOCS,
        # 限流的"判据来源"：配错不报错，只会静默算错 IP ⇒ 必须回显出来可核对
        "proxy": {"trust_forwarded_for": TRUST_PROXY_HEADERS},
        # 请求体积上限：配错了只会在特定路径上失效（如绕过 nginx 直连），也要可核对
        "limits": {
            "max_request_bytes": MAX_REQUEST_BYTES,
            "max_message_chars": MAX_MESSAGE_CHARS,
            "max_feedback_chars": MAX_FEEDBACK_CHARS,
            "max_thread_id_chars": MAX_THREAD_ID_CHARS,
            # 0 = 不限。回显出来才能回答"这次部署到底有没有总时限"
            "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        },
        "rate_limit": {
            "enabled": RATE_LIMIT_ENABLED,
            "per_minute": RATE_LIMIT_PER_MINUTE,
            "by_session": RATE_LIMIT_BY_SESSION,
            # configured 是"打算用"，backend 是"实际在用" —— 两者不一致就是故障
            "shared_configured": bool(REDIS_URL),
            "backend": runtime.get("rate_limit_backend",
                                   "redis" if REDIS_URL else "memory"),
        },
        "quota": {
            "enabled": QUOTA_ENABLED,
            "max_tokens_per_request": LLM_MAX_TOKENS_PER_REQUEST,
            "daily_token_quota": USER_DAILY_TOKEN_QUOTA,
            "shared_configured": bool(REDIS_URL),
            "backend": runtime.get("quota_backend",
                                   "redis" if REDIS_URL else "memory"),
        },
        "review_queue": { "enabled": REVIEW_QUEUE_ENABLED,
                          "notify": bool(REVIEW_NOTIFY_WEBHOOK)},
        # 0 = 永久保留。回显出来才能回答"这次部署到底会不会自动删数据"
        "retention": {"days": RETENTION_DAYS,
                      "auto_sweep": RETENTION_DAYS > 0},
        # 幂等保护：rewrite 是"关掉了重试保护"，不配 REDIS_URL 则是"只在单副本有效"，
        # 两者都必须能从启动日志看出来（改了键前缀/部署形态时尤其重要）
        "idempotency": {
            "enabled": IDEMPOTENCY_ENABLED,
            "ttl_seconds": IDEMPOTENCY_TTL_SECONDS,
            "shared_configured": bool(REDIS_URL),
            "backend": runtime.get("idempotency_backend",
                                   "redis" if REDIS_URL else "memory"),
        },
        "tenant": {
            # key-bound = 租户由 Key 反查（可信）；header-claimed = 请求头自报（不可信，仅单租户/开发）
            "mode": "key-bound" if TENANT_KEYS else "header-claimed",
            "default": DEFAULT_TENANT or None,
        },
        "guard": {"enabled": GUARD_ENABLED, "action": GUARD_ACTION},
        "parse_cache": PARSE_CACHE_ENABLED,
        "circuit": {"enabled": CIRCUIT_ENABLED,
                    "fail_threshold": CIRCUIT_FAIL_THRESHOLD,
                    "reset_seconds": CIRCUIT_RESET_SECONDS},
        "tracing": {"langfuse": bool(LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY),
                    "langsmith": bool(LANGSMITH_API_KEY)},
        "model": MODEL_NAME,
        "db": bool(DB_URL),
        "docs_dir": DOCS_DIR,
    }
