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


def _env_float(name, default):
    """读浮点环境变量，非法值退回默认（同 `_env_int`：一个手滑的 .env 不该让服务起不来）。"""
    try:
        return float(os.getenv(name, str(default)))
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
#
# ⚠️ 判据是**黑名单认开发**（2026-10-05 改），不是白名单认生产：
#   只有明确写着"我在开发/测试"的取值（见 NON_PRODUCTION_ENVS）才算非生产，
#   **其余任何取值一律视为生产**。
#
#   改为这样的理由 —— 原判据 `APP_ENV in ("production", "prod")` 的失效方式是
#   **静默**的：`prd` / `live` / `staging` 这些看起来很像生产的写法都不在白名单里，
#   于是被当成开发环境，一次性翻转三个默认值（放行未鉴权请求、打开 /docs、
#   启动期不拦"生产却无鉴权"），而**全程不报错**。一个把生产环境声明写错的运维，
#   得到的不是"启动失败"，而是"服务以开发姿态接管了流量"。
#
#   与 `TRUST_PROXY_HEADERS` 同一个原则：**默认值必须在"没配错"的方向上偏保守**。
#   让"认不出来的取值"落到生产（要鉴权、关文档、启动期会拦），而不是落到开发。
#   代价（已知并接受）：用自定义值当本地环境名（如 `APP_ENV=mylaptop`）会按生产口径
#   走、因缺鉴权而拒绝启动 —— 本地请显式写 `development`/`dev`/`local` 之一。
APP_ENV = _env_str("APP_ENV", "development").lower()
# 非生产的**闭集**：只有这几种写法才表示"我明确在本地/测试"。
NON_PRODUCTION_ENVS = ("development", "dev", "local", "test", "testing")


def _is_production(app_env: str) -> bool:
    """某个 `APP_ENV` 取值是否算生产。**黑名单认开发** —— 只认 `NON_PRODUCTION_ENVS`，
    其余（含**空串**与任何没见过的写法）一律算生产。

    抽成函数的唯一目的是**让它可以直接被测**：判据此前是模块级的一个 `in` 表达式，
    只能靠 `reload` 或子进程间接验证；而它是"配错即静默降级"的关键点，值得被
    逐值钉住（见 `tests/test_config_env_policy.py`）。

    ⚠️ 空串与"未设置"在此**含义不同**，这是刻意的：
      · 未设置 ⇒ 上面的 `_env_str` 给出默认值 `development` ⇒ 非生产；
      · 显式写空 `APP_ENV=` ⇒ 空串 ⇒ **生产**。
    理由：空串是一个"认不出来的值"，按 fail-closed 应落到安全侧。代价是 `.env` 里
    留了一行空的 `APP_ENV=` 会让服务走到生产口径（并因缺用户鉴权而拒绝启动 ——
    显式失败，不是静默降级）。
    """
    return (app_env or "").strip().lower() not in NON_PRODUCTION_ENVS


IS_PRODUCTION = _is_production(APP_ENV)

# --------------------------------------------------------------------------
# 鉴权（P0-1）
# --------------------------------------------------------------------------
# ⚠️ **共享密钥（`SERVE_API_KEY` / `X-API-Key`）已于 2026-10-05 整体删除。**
#
# 它曾作为"脚本过渡通行证"保留，现在连代码路径一起拔掉了（八层清单见
# `deploy/SECRETS.md` §4.2.1）。删它的理由是三条**结构性**缺陷，都不是
# "密钥不够长"这类可以通过加强密钥修补的问题：
#   1. **分不出是谁**：所有调用方共用同一个值 ⇒ 审计日志里 `user` 恒为空，
#      审核留痕只能记到租户、归不到人；
#   2. **撤不掉**：单值、无过期、无重叠轮换窗口 —— 换值等于改配置 + 重启，
#      而重启会踢掉所有在线用户；
#   3. **带不了租户**：它没有载荷，租户只能由请求头 `X-Tenant-Id` 自报并被采信，
#      于是持它者可以自称任意租户 —— 换一个头就切换检索分区、并绕过按租户分桶的配额。
#
# ⇒ 现在**没有替代变量**：机器调用方（脚本 / CI / 压测）走
#   「账号密码 → `POST /v1/auth/login` → Bearer 令牌」，实现在 `scripts/script_auth.py`。
#   任何"服务端共享密钥"的想法都不该被重新引入 —— 它必然把上面三条一起带回来。
#
# 若 `.env` 里仍留着 `SERVE_API_KEY`，它**不会被静默忽略** —— 启动期有一道守卫会
# 把它**显式报出来并拒绝启动**，避免运维以为"这道门还开着"
# （见 `app/main.py::_assert_no_legacy_auth_knobs`）。
#
# ⚠️ 2026-10-05 同时删除：`REQUIRE_AUTH`。它存在的唯一意义是"允许把鉴权关掉"
# （`REQUIRE_AUTH=0` ⇒ 未带凭证的请求落 anonymous 放行）。匿名兜底删除后，
# 它不再影响任何东西：**未带凭证一律 401**，而"生产环境必须有用户鉴权"由
# `_assert_auth_policy` 看 `IS_PRODUCTION` 判定。留着一个不再生效的开关，
# 只会让人以为"把它设成某个值就能改变行为"，进而误判部署的安全状态。
# 若 `.env` 里还留着它，会被**同一道守卫**拦下（`_assert_no_legacy_auth_knobs`）——
# 它俩属于同一类"看起来还能改变鉴权行为、实际已经不生效"的配置，最危险。

# API 文档（/docs、/redoc、/openapi.json）：生产默认关闭。
# 接口清单+参数结构对公网可见等于把攻击面画成地图；内网/开发保留以便联调。
ENABLE_DOCS = _env_bool("ENABLE_DOCS", not IS_PRODUCTION)

# CORS 允许来源（逗号分隔）。默认 `*` 方便本地联调；生产应给白名单，
# 如 `https://app.example.com,https://admin.example.com`。
# 注意 allow_credentials 恒为 False（鉴权走 Authorization 请求头，不用 Cookie），
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
# 认证字段上限（字段级闸门，与上面同理：给语义明确的 422 而不是笼统的 413）。
# JWT 实际大小通常 1–2KB，8KB 已是宽裕上限；用户名/密码给的是"明显超出即异常"的量级。
MAX_AUTH_TOKEN_CHARS = max(256, _env_int("MAX_AUTH_TOKEN_CHARS", 8192))
MAX_USERNAME_CHARS = max(8, _env_int("MAX_USERNAME_CHARS", 128))
MAX_PASSWORD_CHARS = max(8, _env_int("MAX_PASSWORD_CHARS", 512))
# 密码**下限**（2026-10-04 补）。默认 8 是行业基本线：NIST SP 800-63B 对"仅凭密码"的
# 认证要求至少 8 位，OWASP 也以 8 位为起点（推荐 12）。允许配大，**不允许配小** ——
# `max(8, …)` 让"把弱密码策略写进 .env"这件事在代码层就做不到。
# ⚠️ 作用面：只对**新设的**密码生效。登录与被验的旧密码不设下限，否则早期设的
# 短密码会被反锁在门外 —— 连改成合规密码这一步都做不了（见 main._password）。
MIN_PASSWORD_CHARS = min(max(8, _env_int("MIN_PASSWORD_CHARS", 8)), MAX_PASSWORD_CHARS)

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
# 并发闸门（P1，2026-09-30）
# --------------------------------------------------------------------------
# 为什么需要它
# ----------
# `RATE_LIMIT_*` 限的是**每分钟新请求数**，它管不住**并发驻留**。本服务的主接口是
# SSE：一个请求可能持续几十秒。于是一个客户端只要按限流上限开若干个 SSE 连接并
# **保持不关**，就能把单实例的并发能力占满 —— 而限流指标、错误率、延迟全都正常，
# 没有任何信号提示"实例已经满了"。这不是理论：一次点击重试风暴就够了。
#
# 观测已经有了（`middleware.in_flight_count()` → gauge `http_requests_in_flight`），
# 缺的是**闸门**：只看不拦，等于没有背压。
#
# 为什么是"读-判-增之间不 await"而不是 semaphore
# --------------------------------------------
# `asyncio.Semaphore` 的行为是**排队等待**，不是拒绝。过载时排队恰恰是最坏的：
# 队列本身要占内存，而且用户会一直等一个注定超时的回答。这里要的是**快速失败**
# （背压：把"我扛不住了"立刻告诉调用方），所以用一个整数计数器 + 上限判断。
# 递增与判断之间**没有 await**，在单事件循环里就是原子的（不需要锁）；
# 但计数仍用锁保护 —— ASGI 应用也可能跑在线程池里（TestClient、同步部署形态），
# 一次竞态会让计数永久偏移，之后每次都误判。
#
# 为什么默认 0（不限）
# ----------------
# 沿用默认值原则②："能力开关默认开，但**开着且没配**时行为必须与关闭时一致"。
# 猜一个"看起来合理"的上限会**静默拒绝**正常流量 —— 而这类拒绝在客户端看来
# 和"服务挂了"没有区别。
#
# ⚠️ 它的语义是**单进程**上限（多 worker 时实例上限 = worker 数 × 该值），
# 与限流/配额"多副本共享一个桶"的语义**不同**。这是刻意的：要挡的是
# "这个进程被长连接占满"，而每个 worker 有各自的事件循环与内存，
# 一个全局共享的计数器反而会让某个空闲 worker 替另一个挡枪。
# 配额参考见 `deploy/CAPACITY.md`。
MAX_CONCURRENT_REQUESTS = max(0, _env_int("MAX_CONCURRENT_REQUESTS", 0))

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
#
# ⚠️ 2026-10-04 补记：上面第二条**不只发生在"直连"场景**。就算老老实实部署在反代
# 后面、把本项设成 1（compose 给两个 app 服务注入的就是 "1"），只要代理是用
# `$proxy_add_x_forwarded_for`（**追加式**：客户端自带的段原样保留在左、真实地址
# 追加在右）写这个头，应用取到的"第一段"就仍是**客户端伪造的值** ⇒ 限流同样被绕过。
# 也就是说：**设 1 只是必要条件，代理还必须用覆盖式写入**（`$remote_addr`）。
# 两条合起来才成立 —— 见 `deploy/app/nginx.conf` 顶部"客户端 IP 从哪来"一节，
# 以及 `tests/test_deploy_topology.py` 里钉住"不得用追加式写 XFF"的守卫。
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

#: `/metrics` 是否只允许**私网/回环对端**访问（默认开）。
#:
#: 为什么应用侧还要再拦一道（`deploy/app/nginx.conf` 已经有私网白名单）：
#: nginx 只是**其中一条**入口。容器 `-p 8000:8000` 直连、k8s NodePort、
#: `kubectl port-forward`、Service Mesh 里的 sidecar 旁路 —— 任何一条都能绕开
#: nginx 的 location 判定，而 `/metrics` 会暴露进程内存、错误率、租户用量趋势
#: （对攻击者是"该选什么时机打"的情报）。**门禁不能只挂在代理上。**
#:
#: 判据取**直连对端地址**（`request.client.host`）而**不是** X-Forwarded-For：
#: 后者是客户端可写头（仅在 `TRUST_PROXY_HEADERS=1` 且代理覆盖式写入时才可信），
#: 而这里要的正是"防绕过"—— 用一个可伪造的值去防绕过等于没防。
METRICS_PRIVATE_ONLY = _env_bool("METRICS_PRIVATE_ONLY", True)

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
# 配额（P1-6）：单请求 token 预算 + 日额度（租户级 / 每人级两层）。0 = 不限制
# --------------------------------------------------------------------------
QUOTA_ENABLED = _env_bool("QUOTA_ENABLED", True)
LLM_MAX_TOKENS_PER_REQUEST = max(0, _env_int("LLM_MAX_TOKENS_PER_REQUEST", 0))

#: **租户级**当日 token 总量上限。0 = 不限。
#:
#: ⚠️ 名字里的 `USER_` 是历史包袱，它管的是**一整个租户**当天的总量，不是"每个
#: 用户" —— 语义见 `app/quota.py` 的文件头。**刻意不改名**：改名的收益只是好看，
#: 代价是已部署的 `.env` 里那一行会静默失效（额度变成不限），而这类"静默放宽"
#: 正是本项目一直在消灭的东西。每人级另见 `DAILY_TOKEN_QUOTA_PER_USER`。
USER_DAILY_TOKEN_QUOTA = max(0, _env_int("USER_DAILY_TOKEN_QUOTA", 0))

#: **每人级**当日 token 上限（2026-10-06 新增）。0 = 不限（默认，行为与改造前一致）。
#:
#: 为什么需要它：只有租户级时，额度是**整个租户共用一份** —— 同一个租户里一个人
#: 打满，其余人当天全部 429。更糟的是**无租户**的账号：它们的租户是空串，于是
#: 所有无租户账号共用同一个桶（`quota:<日期>`），彼此互抢。
#:
#: 两级**同时生效、任一越界即拒**：
#:   · 租户级 = 天花板，防"一个租户把全站额度吃光"；
#:   · 每人级 = 防"同租户内一个人吃掉全租户的份额"。
#: 只配租户级、不配每人级仍然是合法配置，但要接受上面那种互抢 —— 启动日志会
#: 提醒（见 `QuotaManager.__init__`）。
#:
#: ⚠️ 有效的每人额度取 `min(每人级, 租户级)`：两级都要过，所以更紧的那一级说了算。
DAILY_TOKEN_QUOTA_PER_USER = max(0, _env_int("DAILY_TOKEN_QUOTA_PER_USER", 0))

# --------------------------------------------------------------------------
# 幂等键（客户端重试保护）
# --------------------------------------------------------------------------
# 客户端超时重试是常态（弱网、网关超时、切后台）。而 `/v1/chat` 是 SSE，
# 重试的代价不是"多读一次" —— 而是**重复生成一整轮回答**：
#   - 重复计费（token 真的被消耗了）；
#   - 同一 thread_id 被追加两条相同用户消息，图多跑一轮，历史里出现重复；
#   - 若停在审核态，`review_queue` 里可能出现重复待审项。
#
# 语义**逐条对着 IETF 草案** `draft-ietf-httpapi-idempotency-key-header-07` 实现：
#   - 首次见到该键（且载荷指纹也新）⇒ 正常执行（2.6 First time request）；
#   - 上一次已完成 ⇒ **返回上一次的完整结果**（2.6 Retry：SHOULD respond with
#     the result of the previously completed operation）；
#   - 上一次还在跑 ⇒ 409 Conflict（2.6 Concurrent Request）；
#   - 同一个键换了载荷 ⇒ 422（2.7：MUST not be reused across different payloads）。
# 规范正文（含过期策略）发布在 `deploy/IDEMPOTENCY.md` —— 标准 2.5.2 要求
# 资源方**必须**发布这份规范。
#
# 默认开，但**不带该头的请求行为完全不变** —— 沿用本模块的默认值原则②
# （"能力开关默认开，但开着且没配时行为必须与关一致"）。
IDEMPOTENCY_ENABLED = _env_bool("IDEMPOTENCY_ENABLED", True)
#: 键的保留时长（秒）。必须**明显长于**客户端的重试窗口，否则"重试时键已过期"
#: 等于没保护；也不能太长，否则会挡住用户"确实想再问一次"（那种情况客户端
#: 应当换一个新键 —— 键的语义是"这一次请求"，不是"这个问题"）。
#: 它同时是**重放结果的保留时长**（标准 2.3 要求公布的过期策略）。
IDEMPOTENCY_TTL_SECONDS = max(1, _env_int("IDEMPOTENCY_TTL_SECONDS", 600))
#: 进程内实现的最大条目数（配了 REDIS_URL 时不适用）。
#: 键由客户端提供 ⇒ 无界缓存是一个可被利用的内存放大面。
IDEMPOTENCY_MAX_ENTRIES = max(100, _env_int("IDEMPOTENCY_MAX_ENTRIES", 10000))
#: 是否**强制**要求 `Idempotency-Key`：开启时该头缺失/非法回 400（标准 2.7 的第一条）。
#: 默认关 —— 该头本就是可选的（标准 2.5.1："Clients MAY choose to send..."），
#: 强制要求会让所有既有客户端（含本项目前端）当场收到 400。
IDEMPOTENCY_REQUIRE_KEY = _env_bool("IDEMPOTENCY_REQUIRE_KEY", False)
#: 键的最大长度。键由客户端提供、会进缓存与日志 ⇒ 必须是有界输入
#: （标准 §5 建议"先按公布的格式校验键，再处理请求"）。
IDEMPOTENCY_MAX_KEY_CHARS = max(8, _env_int("IDEMPOTENCY_MAX_KEY_CHARS", 128))
#: 单个键**最多缓存多少字节的重放内容**。超过则只记"已完成"、不再缓存正文 ——
#: 否则客户端可以用长回答把服务端内存撑爆（键是客户端可控的）。
IDEMPOTENCY_MAX_BODY_BYTES = max(1024, _env_int("IDEMPOTENCY_MAX_BODY_BYTES", 262144))
#: 规范文档的 URL。400/422 的响应体要**带一个指向文档的链接**（标准 2.7）。
#: 留空时响应体不带 Link 头（RFC 9457 的 `type` 回落到 about:blank）。
IDEMPOTENCY_DOC_URL = _env_str("IDEMPOTENCY_DOC_URL", "").strip()

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
#: 通知线程数上限。原实现是"每条通知起一个线程"，无界 —— webhook 变慢时
#: 线程数会随待审量线性增长。固定成小池即可（通知是 IO 密集，2 个足够，
#: 且避免"通知端慢"把整个进程拖垮）。
REVIEW_NOTIFY_WORKERS = max(1, _env_int("REVIEW_NOTIFY_WORKERS", 2))
#: 通知积压上限。满了**丢弃并计数**而不是继续排队：积压通知的价值随时间衰减，
#: 而待审记录本身已入库（通知只是提醒）。见 ReviewQueue._notify_async。
REVIEW_NOTIFY_QUEUE_MAX = max(1, _env_int("REVIEW_NOTIFY_QUEUE_MAX", 64))
# 通知体格式：generic（自建网关，JSON）| wecom（企业微信机器人）| feishu（飞书机器人）
REVIEW_NOTIFY_FORMAT = _env_str("REVIEW_NOTIFY_FORMAT", "generic").lower()

# --------------------------------------------------------------------------
# 多租户（P2）
# --------------------------------------------------------------------------
# 公共租户：**没有租户的账号**（新建未分配、被移出租户）检索时落到这个具名桶。
#
# ⚠️ 2026-10-05 口径翻转（用户拍板）。旧口径是"空串 = 不加过滤 ⇒ 能看到全部租户
# 的文档"，它有一个直接后果：**自助注册的账号在获批入租之前就能读到全库**。
# 现在改成：无租户只能看到这一个公共租户的内容，别的租户一概看不到。
#
# 取值约定：
#   · 配一个具体名字（如 `public`）⇒ 无租户 = 只读这个桶；
#   · 留空 ⇒ 无租户 = **什么都查不到**（fail-closed，见 NO_TENANT_SENTINEL）。
#
# ⚠️ 它同时是**灌库时的默认租户**（`src/` 侧读同一个环境变量）：想让所有人都能
# 查到的资料，就以 tenant=公共租户名 灌进去。
DEFAULT_TENANT = _env_str("RAG_TENANT", "").strip()
TENANT_HEADER = _env_str("TENANT_HEADER", "X-Tenant-Id")

#: 检索层的"没有租户"哨兵值。
#:
#: 为什么不能直接把空串交给检索层：`PgVectorStore` / `QdrantStore` 对空 tenant 的
#: 语义是**"不加过滤"** —— 那会让无租户的账号看到全部租户的内容，正是本次要堵的洞。
#: 所以这里给一个**不可能与真实租户相等**的值，让检索结果为空集：
#: 宁可什么都查不到，也不要越权读到别人的资料。
#:
#: 为什么用 `%` 作为标记字符：真实租户名在存储层被 `AccountStore._check_name`
#: 禁止包含 `%`（与 `:` / `,` / `|` 同一组分隔符），所以这个值不可能被谁"取到"。
NO_TENANT_SENTINEL = "%__no_tenant__%"

# ⚠️ 2026-10-04 移除：`TENANT_KEYS`（"租户 ↔ API Key"一对一的那张表）已整体删除。
#
# 它原本解决的问题是"只有全局 Key 时租户由请求头自报、持有者能冒充任意租户"。
# 现在这条需求由 **JWT 承载**：所有人类用户的租户都从已验签的令牌载荷里取，
# 于是那张表、它的解析函数、以及"租户由密钥反查"这一整条判定路径一并取消。
#
# 若 .env 里还留着 `TENANT_KEYS=...`，它会被**静默忽略**（不再有任何效果）。


# --------------------------------------------------------------------------
# 用户鉴权：本服务会话 token + 服务端会话白名单（可撤销、多设备并存）
# --------------------------------------------------------------------------
# ⚠️ **这是本项目唯一的一条鉴权路径**。此前并存的几档已整体删除：
# `X-API-Key` 共享密钥档、匿名放行兜底，以及**外部身份源直通**
# （OIDC / JWKS，2026-10-05 删除）—— 所以这里的判据不再需要与任何"另一档"比较，
# 认准 `Authorization: Bearer` 即可。
#
# 唯一的主体是**人**：账号密码换本服务会话 token（`POST /v1/auth/login`）。
# 机器调用方（脚本 / CI / 压测）**复用这条路径**（服务账号），而不是另开一条
# "拿共享密钥进门"的路 —— 后者无法归因到人。
#
# 为什么必须自己签发会话 token（而不是接受任何外部签发的 token）
# ---------------------------------------------------------
# 因为"登出要真的生效"这条要求，**与 JWT 无状态本质冲突**：
# 无状态 token 在任何地方都有效，服务端无法单方面作废它。要实现撤销，
# 必须有一份**服务端会话白名单**（记录"这个 sid 现在还作不作数"）。
# 所以流程是本服务自己"发牌与收牌" —— 签发会话 token + 维护白名单。
#
# ⚠️ 外部身份源（OIDC / JWKS）此前扮演"证明你是谁"的角色，2026-10-05 已整体删除：
# 本项目实际的身份来源是自助注册 + 申请加入租户 + 管理员批准，用不上外部 IdP，
# 留着只会多一条要长期维护与测试的身份入口。

# ---- 本服务会话签发（login 换发后使用）----
# 对称密钥（HS256），由运维生成：`openssl rand -hex 32`。
# 留空 = 不签发会话 token ⇒ **没有任何人能登录**（服务等同于不可用）。
AUTH_SESSION_SECRET = _env_str("AUTH_SESSION_SECRET", "")
AUTH_ISSUER = _env_str("AUTH_ISSUER", "chatagent")
# access token 短期（15 分钟）：短寿命是"最小化泄露窗口"的主要手段。
# 撤销**不依赖**它过期 —— 每次请求都会比对会话白名单（app/auth.py 的
# SessionStore），登出即刻生效。
AUTH_ACCESS_TTL = max(60, _env_int("AUTH_ACCESS_TTL", 900))
# refresh token 长期（7 天）：单次使用、轮换，旧的立即失效。
# 会话白名单键的 TTL 取这两者的**较大者**（见 AuthService.session_ttl）。
AUTH_REFRESH_TTL = max(300, _env_int("AUTH_REFRESH_TTL", 604800))
# ⚠️ AUTH_SINGLE_SESSION 已于 2026-10-01 **移除**：改为允许同一用户在多台设备
# 同时登录。旧语义（新登录踢旧登录）及与之绑定的 session_superseded 错误码一并删除。
# 需要清场时改用 `/v1/auth/logout?all=true`（登出该用户全部设备，只作用于自己）。
# 若 .env 里还留着 AUTH_SINGLE_SESSION，它会被**静默忽略** —— 不再有任何效果。

# ---- 3) 初始账号（**只用来建出第一个管理员**，2026-10-04 重新定性）----
# 格式：`user:hash:tenant[:role|role][,user:...]`
# hash 用 PBKDF2-HMAC-SHA256 编码串（见 app/auth.py 的 hash_password）：
#   pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
# 生成命令：python -m scripts.hash_password -u alice -t acme -r admin
#
# ⚠️ 它已**不是**运行时的用户表 —— 用户现在住在数据库的 `users` 表里
# （`app/accounts.py`），管理接口负责增删改查。这份配置的唯一作用是解决
# 鸡生蛋问题：管理接口要求管理员角色，而管理员只能由管理员创建，
# 所以第一个管理员必须来自配置。库里一旦有用户，它就被忽略
# （见 AccountStore.seed_if_empty）。
#
# 想改运行时的人（加人、改租户、停用、删人），用管理接口，不要改这里。
AUTH_USERS_RAW = _env_str("AUTH_USERS", "")
PBKDF2_ITERATIONS = max(100000, _env_int("AUTH_PBKDF2_ITERATIONS", 600000))


def _parse_users(raw):
    """解析 `user:hash:tenant[:role|role][,user:...]` → `{user: {hash, tenant, roles}}`。

    raw: 原始配置串。格式非法的条目**跳过并告警**（与 `_parse_tenant_keys` 同口径：
        一个手滑的逗号不该让服务起不来）。
    返回 dict（空 dict = 未启用本地用户模式）。

    ⚠️ 解析用 `partition(":")` 逐步切分而不是 `split(":")` —— PBKDF2 编码串里
    含 `$` 但不含 `:`，所以按"前三个冒号"切分是安全的；而 `split(":")` 一旦
    未来改了哈希编码格式就会静默错位。
    """
    out = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        user, _, rest = part.partition(":")
        pwhash, _, rest2 = rest.partition(":")
        tenant, _, roles_raw = rest2.partition(":")
        user, tenant = user.strip(), tenant.strip()
        if not user or not pwhash or not tenant:
            logging.warning(
                "AUTH_USERS 条目格式非法（应为 user:hash:tenant[:role|role]），已跳过：%s",
                user or part[:20],
            )
            continue
        if user in out:
            logging.warning("AUTH_USERS 出现重复用户，后者覆盖前者：%s", user)
        out[user] = {
            "hash": pwhash.strip(),
            "tenant": tenant,
            "roles": tuple(r.strip() for r in roles_raw.split("|") if r.strip()),
        }
    return out


AUTH_USERS = _parse_users(AUTH_USERS_RAW)

# ---- 4) 资源级授权 ----
# 谁能审稿。默认 `reviewer`（2026-10-05 改；此前默认空 = 不限制）。
#
# ⚠️ 与 AUTH_ADMIN_ROLES 一样是 **fail-closed**（两者默认原先刻意相反，现统一）：
#   不配此变量 ⇒ 只有带 `reviewer` 角色的人能审；真想要"不做角色校验"（任何登录
#   用户都能审），得**显式写空** `AUTH_REVIEWER_ROLES=` —— 让这个决定看得见。
# 为什么改：原默认"空 = 不限制"是一个**开着**的默认值，而且没有任何提示 ——
#   忘了配就等于把审核权发给每一个登录用户，包括刚注册、还没有租户的账号
#   （后者按当前口径只能读到**公共租户**的文档，见 DEFAULT_TENANT 的说明）。
#   审核是**限权**操作，默认应落在"没人能审"这一侧，再由运维显式放权。
# 代价（已知并接受）：未配此变量、又依赖"任何登录用户都能审"的部署，升级后审核
#   接口会全部 403，需要显式写 `AUTH_REVIEWER_ROLES=` 才能回到旧行为。
AUTH_REVIEWER_ROLES = tuple(
    r.strip() for r in _env_str("AUTH_REVIEWER_ROLES", "reviewer").split(",") if r.strip()
)

# 谁能管理用户与租户（增删改查、审批加入申请）。默认 `admin`。
#
# ⚠️ 与 AUTH_REVIEWER_ROLES 一样是 **fail-closed**（2026-10-05 起两者统一）。
#   此前审稿默认"空 = 不限制"、与本项**故意相反**，理由是"向后兼容既有部署"
#   —— 那是一次历史妥协，现在按"**默认值必须在没配错的方向上偏保守**"统一了。
#   两项留空都等于**没人能做**。
# 理由：管理接口能创建账号、改别人的租户归属、删除租户 —— 它是"权限的源头"。
#   把它默认成"不限制"等于上线即对人开放，而它的破坏面比审稿大得多。
# 真想要"不做角色校验"，得显式写 `AUTH_ADMIN_ROLES=`（空值）—— 让这个决定看得见。
AUTH_ADMIN_ROLES = tuple(
    r.strip() for r in _env_str("AUTH_ADMIN_ROLES", "admin").split(",") if r.strip()
)

# 是否开放自助注册（`POST /v1/auth/register`）。默认**关**。
#
# 默认关的理由是它与租户口径组合后的**空档**：注册出来的账号没有租户，
# 而按本项目当前设计，无租户 = **只能读公共租户**（`RAG_TENANT`）的知识库内容
# （见 DEFAULT_TENANT 的说明）；`RAG_TENANT` 也没配时他**什么都搜不到**。
# 也就是说开放注册的后果分两档：配了公共租户 ⇒ 任何能访问本服务的人都能读到
# 那一份公共文档；没配 ⇒ 新账号进门就是"检索恒为空"。前者是内容泄露面，
# 后者会被当成"服务坏了"来报障。所以它必须是一次**显式开启**的决定。
#   ⚠️ 2026-10-05 口径反转前，这条理由是"无租户能搜到全部租户的语料" ——
#   现在危害小了一档（只到公共租户），但"默认关"这个决定不变。
AUTH_ALLOW_SELF_REGISTER = _env_bool("AUTH_ALLOW_SELF_REGISTER", False)

# ---- 派生：本段是否启用 ----
# 用户鉴权总开关：有本地用户表（种子）或 有会话密钥（能签发/校验会话）。
AUTH_ENABLED = bool(AUTH_USERS or AUTH_SESSION_SECRET)

# --------------------------------------------------------------------------
# 提示词注入防护（P2）
# --------------------------------------------------------------------------
GUARD_ENABLED = _env_bool("RAG_GUARD", True)
# warn（只告警，不改内容）| strip（剥离可疑句）| block（丢弃可疑块）
GUARD_ACTION = _env_str("RAG_GUARD_ACTION", "warn").lower()

# --------------------------------------------------------------------------
# 引用校验（答案的引用是否可追溯 / 有依据）
# --------------------------------------------------------------------------
# 能力开关默认开（开着但判据不足时只标注，不改变交付内容）；判定默认只告警 ——
# 判据里含启发式成分（答案与引用的文本重叠率），默认就 block 会把正常的改写
# 当成"没依据"拦下来，而那种误伤比不拦更糟。
CITATION_CHECK = _env_bool("RAG_CITATION_CHECK", True)
# warn（记日志 + 指标 + 明细随响应带出去）| block（**只对确凿错误生效**：
# 答案引用了不存在的编号 —— 那是可以用代码证明的编造，而重叠率低只是可疑）
CITATION_CHECK_ACTION = _env_str("RAG_CITATION_CHECK_ACTION", "warn").lower()
# 答案与引用文本的最低 n-gram 重叠率。**定得很低**：模型本来就会总结与改写，
# 这一层要抓的是"整段凭空生成"，不是"改写幅度大"。
CITATION_MIN_OVERLAP = _env_float("RAG_CITATION_MIN_OVERLAP", 0.05)

# 在线答案质量采样率（0 = 关闭）。>0 时对**被采样的响应**跑一遍离线判据
# （`src/rag/answer_quality`：正则 + 集合运算 + 与引用编号比对，**零模型调用**）。
#
# 它与离线评测回答的是两个不同的问题，互不替代：
#   离线（进 CI）——"这份代码在固定题集上还准不准"（确定性、可回归、防退化）；
#   在线（本开关）——"今天的真实流量里，有多少条在编造标识符"。
# 真实流量的措辞与题集完全不同，这个数只能从线上取。
#
# 默认 0（关闭）：它是**趋势**指标，不需要全量；而每个被采样的请求要多为它
# 花几毫秒。建议 0.02 ~ 0.1 —— 够画出曲线，又几乎无感。
ONLINE_QUALITY_SAMPLE = _env_float("RAG_ONLINE_QUALITY_SAMPLE", 0.0)

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

# --------------------------------------------------------------------------
# PG 连接池（供 app/pg.py 的三个存储类共用）
# --------------------------------------------------------------------------
# 为什么要有池：原先每次操作都新建一条 PG 连接，一次 /v1/chat 要经 2~4 个，
# 于是"并发请求数 = PG 后端进程数"，max_connections 一满就是"连不上"。详见 app/pg.py。
#
# ⚠️ PG_POOL_MAX 是**每副本**的上限：多副本部署时总连接数 = MAX × 副本数，
# 必须与 PG 的 max_connections 一起核算（见 deploy/CAPACITY.md）。
PG_POOL_MIN = max(0, _env_int("PG_POOL_MIN", 1))
#: 下限至少 1 才能让池"预热"（避免首个请求承担建连延迟）。
PG_POOL_MAX = max(1, _env_int("PG_POOL_MAX", 8))
if PG_POOL_MIN > PG_POOL_MAX:
    PG_POOL_MIN = PG_POOL_MAX
#: 取不到连接时的等待上限（秒）。超时抛错、走 fail-closed 的 503，
#: 不允许"无限等"—— 那会把上游超时变成自己挂死。
PG_POOL_TIMEOUT = max(1, _env_int("PG_POOL_TIMEOUT", 5))

# --------------------------------------------------------------------------
# agent 链路（请求默认全部走这里；实现见 src/agents/）
# --------------------------------------------------------------------------
# 为什么这段配置集中在这里、而链路层（src/graph.py、src/agents/*）直接读环境变量
# ------------------------------------------------------------------------------
# 与 `DOCS_DIR` 完全同构：链路层要在**导入期/请求期**就拿到这些值，走 config 会形成
# `src/ → app/` 的反向依赖（见 src/observability.py 的说明）。所以两边各读一次，
# 这里这一份的用途是**启动自检回显** —— 运维能从启动日志确认"agent 链路到底开没开、
# 用哪个目录落资产"，而不是只能靠猜。
#
# 取值约定沿用本模块的两条原则：能力开关默认**开**；但只要"开着且实际不可用"
# （没装 `openai-agents` / 没配 API Key），行为必须与关闭时**一致** ——
# 全部请求回落原问答链路。见 `src/agents/runtime.py` 与 `src/graph.dispatch`。
AGENTS_ENABLED = _env_bool("AGENTS_ENABLED", True)

#: 资产落盘目录（PPT 大纲渲染、插图占位文件）。
#: ⚠️ 默认**不是** `data/` —— 那是 RAG 语料目录，往里写文件会被灌库当成资料。
AGENT_ASSET_DIR = _env_str("AGENT_ASSET_DIR", "var/agent_assets")

#: 单次模型调用超时。刻意不复用主链路的 30s 常量：agent 链路一次请求会调多次模型，
#: 总时长是"调用次数 × 单次"，两者需要独立可调（否则调总时长只能改单次超时）。
AGENT_TIMEOUT_SECONDS = _env_float("AGENT_TIMEOUT_SECONDS", 30.0)

#: 评审回灌的最大轮数 / 单次 run 内允许的 LLM 调用次数（官方 max_turns 语义）。
AGENT_MAX_ATTEMPTS = max(1, _env_int("AGENT_MAX_ATTEMPTS", 3))
AGENT_MAX_TURNS = max(2, _env_int("AGENT_MAX_TURNS", 12))

#: 评审调用的重试次数（结构化输出解析抖动）。
AGENT_EVALUATOR_TRIES = max(1, _env_int("AGENT_EVALUATOR_TRIES", 2))

#: 检索工具单次召回的片段数。
AGENT_KB_TOP_K = max(1, _env_int("AGENT_KB_TOP_K", 3))

#: 单条交付物的字符上限（出口护栏之一，纯确定性、零模型调用）。
AGENT_MAX_ANSWER_CHARS = max(1000, _env_int("AGENT_MAX_ANSWER_CHARS", 8000))

#: 跨轮对话历史：**送进模型的输入预算**（token）；``None`` = **不限制**。
#:
#: ⚠️ 2026-10-03 起取代了原来的"最近条数"口径 —— 条数与 token 是**互不知道对方**的
#: 两套判据（80 条可能只有 5k token，也可能 120k），而窗口是按 token 计的，所以只留
#: 这一个口径。实现走官方口子 `RunConfig.call_model_input_filter`。
#:
#: ⚠️ 必须与 `src/agents/history_budget.py` 的 `history_token_limit()` **同口径** ——
#: 那边读**同一个**环境变量、也把 `0` / 负数解释成"不限"。两处各自独立解析是分层的
#: 结果（`src/` 不依赖 `app/`），所以一致性靠约定 + 守卫
#: （`test_zero_and_negative_mean_unlimited_in_both_places`）。
#:
#: 这里刻意用 ``None`` 而不是 ``0`` 表示"不限"：启动日志与配置回显里，
#: `0` 会被读成"一个 token 都不给"，而那与"不限"是**完全相反**的两件事。
#:
#: ⚠️ 这是**输入**预算、不是整个窗口：工具清单与输出长度不在那个口子的视野里，
#: 配这个值时必须给它们留余量。口径与取舍见 `src/agents/history_budget.py`。
_history_token_raw = _env_int("AGENT_HISTORY_TOKEN_LIMIT", 100_000)
AGENT_HISTORY_TOKEN_LIMIT = _history_token_raw if _history_token_raw > 0 else None

#: 跨轮历史的**压缩**（2026-10-08 新增）。**默认关闭** —— 新链路先可关，
#: 出问题退回的代价是零（行为与今天**逐字节一致**）。
#:
#: 两个固定入口共用一个开关（实现见 `src/agents/compaction.py`）：
#:   1. **历史压缩** —— 老历史超过保留轮数就换成一段摘要，**写回库**：
#:      会话侧在请求开头起后台任务（下一轮生效），图状态侧是图里的一个节点（本轮生效）；
#:   2. **窗口压缩** —— 将要送进模型的输入超过预算的 85% 就先把老的那段换成摘要，
#:      **只改这一次调用**（不写回）。
#: 两者合起来把"库里存的量随轮数**平方**增长"压成线性（实测：不压时轮数每翻一倍、
#: 落盘量正好翻四倍）。
AGENT_HISTORY_COMPACT = _env_bool("AGENT_HISTORY_COMPACT", False)

#: 压缩时**原样保留**最近多少轮。
#:
#: ⚠️ **下限 1 不是随手定的**：人工打回那一轮（`/v1/review` 的 revise）靠
#: "**原话已经躺在会话里**"来决定不再重传原话（`src/agents/runner._build_input_items`），
#: 压成 0 会让第二遍只看到一句"人工审核意见：…"、**不知道在改什么**，而且**不报错**。
#: 打回永远发生在"刚刚跑完的那一轮"上（它是最新的），所以 1 就够；5 只是给日常留余量。
AGENT_HISTORY_KEEP_ROUNDS = max(1, _env_int("AGENT_HISTORY_KEEP_ROUNDS", 5))

#: **窗口压缩**的触发线 = `AGENT_HISTORY_TOKEN_LIMIT` × 这个比例（默认 85%）。
#: 做成配置（而不是写死 0.85）：将来网关换成更大窗口的模型时只改配置、不改代码。
AGENT_HISTORY_COMPACT_RATIO = _env_float("AGENT_HISTORY_COMPACT_RATIO", 0.85)



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
            # 恒为 True：**任何**业务请求都必须带一个已验签的 JWT，没有例外档，
            # 也没有任何环境变量能关掉它（匿名兜底、共享密钥档、外部身份源都已删除）。
            "required": True,
            # 唯一的一条鉴权路径：账号密码换本服务会话 token，租户从令牌载荷里取。
            # ⚠️ 这里**刻意不再有** `script_key` / `oidc` 这些键 —— 对应路径已整体
            # 删除，回显一个恒为 false 的字段会让人以为"这项还能开"。
            "user_auth": {
                "enabled": AUTH_ENABLED,
                # 没有独立会话密钥 ⇒ 不能签发/校验本服务会话 token，
                # 也就**没有任何人能登录**（服务等同于不可用）。必须可见。
                "session_secret": bool(AUTH_SESSION_SECRET),
                # ⚠️ 这是**种子**条目数，不是运行时用户数。运行时用户住在数据库的
                # `users` 表里（管理接口负责增删改查），种子只在库为空时用来
                # 建出第一个管理员。两者混起来会让人误判"账号体系没生效"。
                "seed_users": len(AUTH_USERS),
                "reviewer_roles": list(AUTH_REVIEWER_ROLES),
                "admin_roles": list(AUTH_ADMIN_ROLES),
                "self_register": AUTH_ALLOW_SELF_REGISTER,
                # 账号数据落在哪。postgres 才有"跨重启、多副本一致"；
                # memory 是本地开发用的，多副本下每个进程各持一份互不可见的账号表。
                "accounts_backend": runtime.get(
                    "accounts_backend", "postgres" if DB_URL else "memory"
                ),
                # 同 rate_limit：configured 是"打算用"，backend 是"实际在用"。
                # ⚠️ 会话校验是**无条件**的 ⇒ 多副本下若落在 memory，副本 A 签发的
                # 会话在副本 B 查不到，用户会**随机收到 401**（登录成功、隔几次请求
                # 又要重登）。启动期有对应告警，这里也必须能看出来。
                "session_backend": runtime.get(
                    "auth_session_backend", "redis" if REDIS_URL else "memory"
                ),
            },
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
            # 0 = 不限。**单进程**上限（多 worker 时实例上限 = worker 数 × 该值）
            "max_concurrent_requests": MAX_CONCURRENT_REQUESTS,
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
            # 两级日额度都必须回显：只配了第一级时，"同租户互相抢额度"这件事
            # 从配置值本身看不出来，得两个值一起看才知道第二级没装。
            "tenant_daily_token_quota": USER_DAILY_TOKEN_QUOTA,
            "per_user_daily_token_quota": DAILY_TOKEN_QUOTA_PER_USER,
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
        # 两者都必须能从启动日志看出来（改了键前缀/部署形态时尤其重要）。
        # `require_key` 也要回显：开了它之后缺头的请求会当场 400，
        # 这是"上线后客户端全挂"这类事故的第一个怀疑对象。
        "idempotency": {
            "enabled": IDEMPOTENCY_ENABLED,
            "ttl_seconds": IDEMPOTENCY_TTL_SECONDS,
            "require_key": IDEMPOTENCY_REQUIRE_KEY,
            "max_body_bytes": IDEMPOTENCY_MAX_BODY_BYTES,
            "doc_url": IDEMPOTENCY_DOC_URL or None,
            "shared_configured": bool(REDIS_URL),
            "backend": runtime.get("idempotency_backend",
                                   "redis" if REDIS_URL else "memory"),
        },
        "tenant": {
            # 租户的唯一来源是**已验签的 JWT 载荷**（2026-10-04 起）；
            # 请求头 `X-Tenant-Id` 只能"确认"不能"声明"，且共享密钥档已删除 ⇒
            # 代码里**不存在**任何"租户由客户端自报"的路径。
            "mode": "jwt",
            # 无租户时落到哪个桶。None = 未配 ⇒ 无租户者查不到任何内容（fail-closed）。
            "public": DEFAULT_TENANT or None,
            # ⚠️ 回显出来是为了避免"以为配了就隔离了"：留空时无租户的账号**什么都
            # 搜不到**（而不是能搜到全部）—— 这与本项的历史语义相反，值得一眼可见。
            "no_tenant_sees_nothing": not DEFAULT_TENANT,
        },
        "guard": {"enabled": GUARD_ENABLED, "action": GUARD_ACTION},
        # 回显出来才回答得了"线上到底有没有在校验引用、低重叠会不会被拦"
        "citation_check": {"enabled": CITATION_CHECK,
                           "action": CITATION_CHECK_ACTION,
                           "min_overlap": CITATION_MIN_OVERLAP},
        "parse_cache": PARSE_CACHE_ENABLED,
        "circuit": {"enabled": CIRCUIT_ENABLED,
                    "fail_threshold": CIRCUIT_FAIL_THRESHOLD,
                    "reset_seconds": CIRCUIT_RESET_SECONDS},
        "tracing": {"langfuse": bool(LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY),
                    "langsmith": bool(LANGSMITH_API_KEY)},
        "model": MODEL_NAME,
        "db": bool(DB_URL),
        "docs_dir": DOCS_DIR,
        # agent 链路：`ready` 由调用方在装配后填入（与 rate_limit 的
        # "configured vs backend" 同一个理由）—— 配了 AGENTS_ENABLED=true 不等于
        # 真的能用（没装 SDK / 没配 Key 都会静默降级为"全部走问答链路"）。
        "agents": {
            "enabled": AGENTS_ENABLED,
            "ready": bool(runtime.get("agents_ready")),
            "reason": runtime.get("agents_reason", ""),
            "asset_dir": AGENT_ASSET_DIR,
            "max_attempts": AGENT_MAX_ATTEMPTS,
            "max_turns": AGENT_MAX_TURNS,
            # 跨轮对话历史。⚠️ 这里**刻意不写 "backend" 这个键**：实际用哪个后端
            # 要异步连一次才知道（`create_async_engine` 是懒连接，配错地址不会
            # 当场报错），而本函数在**导入期**就被调用，那时还没有事件循环。
            # 所以这里只回显"打算怎么配"，实际结论由 lifespan 单独打一行日志
            # （`app.main` 的跨轮历史自检）—— 宁可少一个键，也不要谎报一个后端名。
            "history": {
                # 历史与检查点同库（PG）⇒ 多副本天然共享，不再需要"所有副本指向
                # 同一个 Redis"那条约束。
                "shared_configured": bool(DB_URL),
                # 送进模型的**输入预算**（token），None = 不限。
                "token_limit": AGENT_HISTORY_TOKEN_LIMIT,
                # 对话历史**不设保留期**（0 = 不清）。这是产品口径、不是技术参数。
                "retention_days": 0,
                # 压缩（2026-10-08）：开关 + 保留轮数 + 窗口触发比例。
                # ⚠️ 回显出来才回答得了"线上到底在不在压、压得对不对"—— 关着时
                # 库里那份就是**平方**增长，而它在指标上完全看不出来。
                "compact": AGENT_HISTORY_COMPACT,
                "keep_rounds": AGENT_HISTORY_KEEP_ROUNDS,
                "compact_ratio": AGENT_HISTORY_COMPACT_RATIO,
            },
        },
    }
