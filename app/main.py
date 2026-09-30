"""FastAPI 服务化入口：流式对话 + 人工审核 resume + 生产化横切能力。

运行（在项目根目录执行）：
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
（开发热重载加 --reload）

端点
----
    POST /v1/chat              SSE 流式对话（低分时下发 need_review）
    POST /v1/chat/resume       续跑中途断掉的会话（SSE；从检查点继续）
    POST /v1/review            人工审核 resume（accept / revise）
    GET  /v1/reviews           待审队列（分页列举，含草稿摘要与评分）
    GET  /v1/sessions/{id}     会话状态快照（断点续传的基础；纯读、幂等）
    GET  /health/live          存活探针（只反映本进程，探针失败应重启容器）
    GET  /health/ready         就绪探针（真探 LLM 配置 / 数据库 / 向量后端）
    GET  /metrics              Prometheus 文本指标
    GET  /health               **兼容**旧路径，等价于 /health/live

为兼容既有客户端与测试，`/chat`、`/review` 也保留（等价于 `/v1/` 版本，标记 deprecated）。

本文件原名 serve.py，按目录归类迁入 app/。
"""
import asyncio
import hmac
import json
import logging
import os
import sys
import time
import warnings

# 1) 日志最先配置：结构化 JSON + request_id，让"一次请求的所有日志"可串起来
from .logging_setup import setup_logging, get_request_id

setup_logging()
warnings.filterwarnings("ignore", message=".*PydanticSerializationUnexpectedValue.*")

# 2) 让 .env 自动生效（可选依赖，装在则启用）
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # noqa: BLE001
    pass

import psycopg  # 显式声明依赖：让"缺 psycopg"在导入期就暴露，而不是等检查点装配时才炸

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, field_validator
from sse_starlette.sse import EventSourceResponse
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from src.graph import build_graph
from src.prompts import PROMPT_VERSION

from . import config, health, metrics, middleware, tracing
from .idempotency import NEW as IDEM_NEW, build_idempotency_store
from .middleware import (BodySizeLimitMiddleware, ConcurrencyLimitMiddleware,
                         RequestContextMiddleware)
from .quota import RequestBudget, build_quota_manager, estimate_tokens
from .ratelimit import build_rate_limiter
from .review_queue import build_review_queue
from .sessions import build_session_registry


# ---------------------------------------------------------------------------
# 配置（全部来自环境变量；集中读一次，模块级名字保持与旧版一致以免破坏引用）
# ---------------------------------------------------------------------------
AGNES_API_KEY = config.API_KEY
AGNES_API_BASE = config.API_BASE
AGNES_MODEL = config.MODEL_NAME
# 与 chatAgent.ipynb / .env 统一使用 DB_URL（原先此处叫 PG_CHECKPOINT_URI，导致 .env 取不到值、静默回退内存）
DB_URL = config.DB_URL
SERVE_API_KEY = config.SERVE_API_KEY      # 非空则所有请求需带 X-API-Key

# 横切组件（进程内单例）：限流 / 配额 / 审核队列 / 会话归属
_LIMITER = build_rate_limiter()
_QUOTA = build_quota_manager()
_REVIEW_QUEUE = build_review_queue()
_SESSIONS = build_session_registry()
#: 幂等键表：客户端重试的拦截点（见 `app/idempotency.py` 的语义说明）。
_IDEMPOTENCY = build_idempotency_store()
_TRACING_BACKEND = tracing.init_tracing()


if not AGNES_API_KEY:
    logging.warning("AGNES_API_KEY 未设置，运行时模型调用会失败")

from langchain_openai import ChatOpenAI  # noqa: E402

_MODEL = ChatOpenAI(
    model=AGNES_MODEL, api_key=AGNES_API_KEY,
    base_url=AGNES_API_BASE, timeout=30, max_retries=3,
)


# ---------------------------------------------------------------------------
# 检查点持久化：**必须用异步 saver**，在 lifespan 里装配
# ---------------------------------------------------------------------------
# 为什么不能用同步 PostgresSaver
# -----------------------------
# 本服务的每个端点都走 `await graph.astream(...)` / `await graph.aget_state(...)`
# （asyncio 事件循环）。langgraph 的**同步** `PostgresSaver` 没有实现
# `aget_tuple`，异步调用会直接 `NotImplementedError` —— 而且它是在流的中间炸的，
# 表现成"SSE 连上了、一个 token 都没吐就被 error 事件收尾"，极难往检查点上想。
#
# 所以持久化必须用 `AsyncPostgresSaver` + `AsyncConnectionPool`，而建池要 await，
# 不能在模块导入时做 ⇒ 放进 lifespan。这也顺带给了我们**优雅关闭**：
# 进程收到 SIGTERM 时 lifespan 收尾会释放连接池，不再掐断在途请求。
#
# ⚠️ 但"不掐断在途请求"这件事**不是 lifespan 做的**，别把功劳记错（2026-09-29 实测）
# ---------------------------------------------------------------------------
# 真正保证在途请求跑完的是 **uvicorn**：`Server.shutdown()` 先 `server.close()`
# 停止接受新连接，再对每个连接调 `connection.shutdown()` —— 而对**响应尚未结束**
# 的连接，它只设 `keep_alive = False`（见 `uvicorn/protocols/http/h11_impl.py`
# 的 `RequestResponseCycle.shutdown`），然后 `_wait_tasks_to_complete()` 等它写完。
# 也就是说：**流式响应会被完整放完**，lifespan 的收尾发生在这一切**之后**。
#
# 那真实风险在哪（这才是要防的）：
#   ① uvicorn 的 `timeout_graceful_shutdown` 默认是 **None = 永远等**。一个卡住的
#      请求（上游不吐字、客户端不主动断）能把关闭无限期拖住；
#   ② 拖住的后果是**容器运行时按自己的宽限期 SIGKILL**（`docker stop` 默认 10s）。
#      被 SIGKILL 才是真的"掐断在途流"，而且不留任何清理机会。
#   ⇒ 所以命令在 `app/serve.py` 里显式设了排空上界，`docker-compose.yml` 里把
#      `stop_grace_period` 设得**大于**它。两处必须一起改，只改一处等于没改。
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _async_checkpointer(dsn):
    """按 DSN 装配异步 checkpointer（异步上下文管理器）。

    dsn: 连接串。**不要**在这里做异常处理，交给调用方决定降级策略。
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(dsn) as cp:
        await cp.setup()          # 建表（幂等）
        yield cp


def _report_index_identity():
    """启动自检：核对"库里向量的身份"与当前 `EMBEDDING_MODEL`，并上报指标。

    为什么必须在**启动时主动跑一次**，而不是等就绪探针去读：
      - 就绪探针读的是 `index_identity.last_status()` 这份**进程内缓存**，
        没跑过校验的进程读出来是空的 —— 于是"检查过、没问题"与"根本没检查"
        在探针里长得一模一样，而那正是这条链路最该防的失败模式；
      - 启动日志是唯一能在事后回答"这个实例起来时索引身份对不对"的地方。

    为什么只在这里写指标：`src/rag/` 是纯库层（CLI 也直接跑它），不该反向依赖
    `app/metrics`。RAG 侧把结论缓存在 `last_status()`，由这一处读后上报。

    为什么不报 `ok is None` 的情况（内存后端 / 库里还没有向量 / 读库失败）：
    那些都**无从判断**，上报 0 会把"还没建库"记成一次真实的"索引不可用"。
    """
    try:
        from src.rag import retrieval as R
        got = R.selfcheck_index_identity()
    except Exception as e:  # noqa: BLE001  （自检失败不该拦住启动）
        logging.warning("索引身份自检未能执行（不影响启动）：%s", e)
        return
    logging.info("索引身份自检：status=%s backend=%s %s",
                 got.get("status"), got.get("backend"), got.get("detail"))
    if got.get("ok") is None:
        return
    ok = bool(got["ok"])
    metrics.set_gauge("rag_index_identity_ok", 1 if ok else 0)
    if not ok:
        metrics.inc("rag_index_identity_mismatch_total")


@asynccontextmanager
async def lifespan(_app):
    """应用生命周期：启动时装异步检查点，关闭时释放资源。

    装配失败一律**降级为内存 checkpointer 并记 warning**，不阻止服务启动 ——
    "能起来"优先于"状态持久化"。但降级后重启会丢会话状态，所以日志必须点名原因。
    """
    global graph
    stack = AsyncExitStack()
    # 向量索引身份自检放在最前：它与检查点无关，且无论 DB 通不通都要在启动日志里
    # 留下一条明确结论（"没检出问题"与"根本没检查"必须能区分开）。
    _report_index_identity()
    dsn = os.getenv("DB_URL", DB_URL)
    if not dsn:
        logging.info("未设置 DB_URL，使用内存 checkpointer（重启丢状态）")
    else:
        try:
            cp = await stack.enter_async_context(_async_checkpointer(dsn))
            graph = build_graph(model=_MODEL, checkpointer=cp)
            logging.info("AsyncPostgresSaver 就绪：检查点异步持久化（astream/aget_state 可用）")
        except Exception as e:  # noqa: BLE001
            await stack.aclose()
            stack = AsyncExitStack()
            logging.warning("异步检查点装配失败，回退内存 checkpointer（重启丢状态）：%s", e)
            if sys.platform == "win32":
                logging.warning(
                    "提示：Windows 上 psycopg 的异步模式**不支持 ProactorEventLoop**，"
                    "而 uvicorn 默认就用它。请用 `python -m app.serve` 启动（会先切到 "
                    "SelectorEventLoop），或改用 Linux/Docker 部署（那边默认就是 Selector）。"
                )
    try:
        yield
    finally:
        # 走到这里说明 uvicorn 已经放过在途请求了（见文件头部的顺序说明）。
        # 把数量记下来 —— 这是回答"这次发布有没有打断正在进行的回答"的唯一依据。
        # 大于 0 不一定是坏事（可能是刚进来的短请求），但如果长期 >0 就该查了。
        left = middleware.in_flight_count()
        if left:
            logging.warning("关闭时仍有 %d 个请求在途（uvicorn 已等待其完成）", left)
        else:
            logging.info("关闭时无在途请求")
        await stack.aclose()
        logging.info("服务关闭：检查点连接池已释放")


# API 文档按环境开关：生产默认关闭（`/docs`、`/redoc`、`/openapi.json` 全部 404）。
# 为什么关闭：这三个端点会把接口清单、参数结构与错误模型完整画出来，
# 等于给攻击者一份免探索的接口地图。开发/内网保留以便联调。
def _docs_kwargs(enabled: bool) -> dict:
    """按是否开放文档返回 FastAPI 的 URL 开关（enabled=False 时三者全部 404）。

    抽成独立函数是为了**可测**："生产真的关掉了文档"必须能被断言，
    而不是只检查"配置读对了" —— 后者是典型的假绿。
    """
    return {} if enabled else {"docs_url": None, "redoc_url": None, "openapi_url": None}


app = FastAPI(
    title="chatAgent API",
    version=config.APP_VERSION,
    description="RAG 问答服务：检索 → 生成 → 自评审 → 人工审核（human-in-the-loop）",
    lifespan=lifespan,
    **_docs_kwargs(config.ENABLE_DOCS),
)

# 中间件嵌套顺序（外层 → 内层）：
#   ServerError > CORS > RequestContext > ConcurrencyLimit > BodySizeLimit > 路由
#
# ⚠️ `add_middleware` 是 `insert(0, ...)` —— **后添加的在更外层**，与"读代码时
# 从上往下"的直觉相反。所以下面的调用顺序是 BodySizeLimit → ConcurrencyLimit →
# RequestContext → CORS，才能得到上面那个嵌套（实测打印过 middleware_stack 确认，
# 不要凭直觉调换）。
#
# 为什么 BodySizeLimit 必须在 RequestContext **之内**：
#   它发出的 413 需要经过 RequestContext 的 send 包装，才会带上 X-Request-ID 并计进
#   `http_requests_total{status=413}`。反了的话，"被拒的大包"在指标里完全看不见 ——
#   而这恰是容量问题的第一信号。（这不是推测：第一版就是反的，测试直接抓到 413
#   没有 X-Request-ID。）
# 为什么 ConcurrencyLimit 也在 RequestContext **之内**、BodySizeLimit **之外**：
#   ① 在内层就没法带上 request_id、不进 `http_requests_total` —— 同 413 的理由；
#   ② 在外层则**先拒后读**：过载时连请求体都不用读，省下的是最紧的资源。
#      反过来的话，一个正在发大包的客户端会先把内存吃掉，然后才被并发闸门挡住。
# 为什么 CORS 在最外：
#   让所有响应（含 413、503、500、限流 429）都带跨域头。CORS 要是在内层，浏览器对
#   413 只会报"缺少 CORS 头"，前端永远看不到真正的原因。
#
# allow_credentials 恒为 False：鉴权走 X-API-Key 请求头而不是 Cookie，
# 因此不需要凭据模式，也就避免了"`*` + credentials"这个浏览器非法组合。
app.add_middleware(BodySizeLimitMiddleware, max_bytes=config.MAX_REQUEST_BYTES,
                   enabled=True)
app.add_middleware(ConcurrencyLimitMiddleware,
                   limit=config.MAX_CONCURRENT_REQUESTS, enabled=True)
app.add_middleware(RequestContextMiddleware, enabled=config.METRICS_ENABLED)
app.add_middleware(CORSMiddleware,
                   allow_origins=config.CORS_ALLOW_ORIGINS,
                   allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"])

# 运行期才确定的"共享后端到底有没有生效"：构造完成后回显进启动日志。
# 只回显 REDIS_URL 是否存在会谎报（见 config.as_dict 的说明）。
logging.info("服务启动配置：%s", config.as_dict(runtime={
    "rate_limit_backend": _LIMITER.backend_name,
    "quota_backend": _QUOTA.backend_name,
}))
logging.info("tracing 后端：%s", _TRACING_BACKEND)
# 提示词版本单独回显（不进 as_dict —— 它是**代码事实**而非可配置项，
# 混进"启动配置"会让人误以为可以设置）。线上答得不对时，
# 第一件要核对的事就是"跑的是哪一版提示词"。
logging.info("提示词版本：%s", PROMPT_VERSION)

# 模块级先用内存 checkpointer 起图，lifespan 启动时再换成异步持久化版本。
# 刻意不在导入期连库：那会在 import 时就开一个永不释放的连接，且测试导入本模块
# 会平白打一次真实 PG。
graph = build_graph(model=_MODEL, checkpointer=None)



# ---------------------------------------------------------------------------
# 鉴权与身份：**租户只能由 API Key 推导，不能由请求头决定**
# ---------------------------------------------------------------------------
# 修复的问题（2026-09-28）：原先只有"一个全局 SERVE_API_KEY + 请求头 X-Tenant-Id"，
# 而 X-Tenant-Id 是客户端随便填的 ⇒ 任何持 Key 者改个 header 就是"另一个租户"。
# 后果不是显示问题而是真实越权：
#   - 日配额按租户分桶 ⇒ 换个 header 额度重新计算，配额形同虚设；
#   - 审核队列记录 tenant ⇒ 归属是假的，按租户巡检会看错对象；
#   - 配合可猜的 thread_id ⇒ 可串读别人的会话（由 app/sessions.py 一并封堵）。
#
# 身份模型（三档，按配置自动选择）：
#   ① TENANT_KEYS 已配（推荐，生产多租户）：Key 与租户一对一，租户由**反查**得出，
#      请求头仅用于一致性核对，不一致直接 403。
#   ② 只有 SERVE_API_KEY（单租户/兼容）：租户仍来自请求头 ⇒ 启动时明确告警。
#   ③ 两者都没配：仅限本地开发（REQUIRE_AUTH=1 时该状态**拒绝启动**）。
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Identity:
    """一次请求的身份。

    tenant: 生效租户 —— 决定配额分桶、审核归属、会话属主。
    via: 认证方式 —— tenant-key（Key 反查）| global-key（全局单 Key）| anonymous（未配鉴权）。
    """

    tenant: str
    via: str


def _authenticate(x_api_key: str, x_tenant_id: str) -> Identity:
    """认证请求并确定租户；失败抛 401/403。

    x_api_key: 请求头 X-API-Key。
    x_tenant_id: 请求头 X-Tenant-Id，**只用于核对**（多租户模式下不参与身份判定）。

    三种模式见上方注释块。返回 Identity。
    """
    claimed = (x_tenant_id or "").strip()

    # ① Key ↔ 租户一对一（生产多租户）：租户来自反查，请求头只能"确认"不能"声明"
    #
    # 这里用 dict 查找而不是逐个 compare_digest：查找键是**密钥本身的哈希**，
    # 不做前缀逐字节比较，因此没有"逐字节试错"的时序信息可用；而逐个比较需要
    # 遍历全部租户，反而把租户数量暴露在耗时上。字典键不落盘、不进日志。
    if config.TENANT_KEYS:
        tenant = config.TENANT_KEYS.get(x_api_key or "")
        if not tenant:
            raise HTTPException(status_code=401, detail="invalid api key")
        if claimed and claimed != tenant:
            metrics.inc("tenant_mismatch_total")
            logging.warning("租户自报与 Key 不符，拒绝：key_tenant=%s claimed=%s",
                            tenant, claimed)
            raise HTTPException(status_code=403, detail="X-Tenant-Id 与 API Key 不匹配")
        return Identity(tenant, "tenant-key")

    # ② 单一全局 Key（兼容）：租户仍由请求头决定 ⇒ 属自报，启动时已告警
    if SERVE_API_KEY:
        # 用**常量时间**比较：`!=` 在第一个不同的字节就返回，逐字节试错能靠响应时间
        # 反推密钥前缀（经典的 timing oracle）。网络抖动让它很难利用，但代价是一行。
        if not hmac.compare_digest(x_api_key or "", SERVE_API_KEY):
            raise HTTPException(status_code=401, detail="invalid api key")
        return Identity(_tenant_of(claimed), "global-key")

    # ③ 未配任何密钥：本地开发免鉴权；生产由 REQUIRE_AUTH 在启动期拦下
    if config.REQUIRE_AUTH:
        raise HTTPException(status_code=503, detail="服务未配置鉴权，按策略拒绝服务")
    return Identity(_tenant_of(claimed), "anonymous")


def _assert_auth_policy() -> None:
    """启动期鉴权策略自检：要么拦住"裸奔上线"，要么把风险讲清楚。

    这是**唯一**能在流量进来之前发现配置缺失的地方，所以宁可在这里 raise
    让进程起不来，也不要带着无鉴权状态接管线上流量。
    """
    has_tenant_keys = bool(config.TENANT_KEYS)
    has_global_key = bool(SERVE_API_KEY)

    if config.REQUIRE_AUTH and not (has_tenant_keys or has_global_key):
        raise RuntimeError(
            "REQUIRE_AUTH=1 但 SERVE_API_KEY / TENANT_KEYS 均未配置："
            "拒绝以无鉴权状态启动。请配置 API Key，或显式设 REQUIRE_AUTH=0（仅限本地）。"
        )
    if not (has_tenant_keys or has_global_key):
        logging.warning(
            "未配置任何 API Key，接口对任何人开放（仅限本地开发）。"
            "生产请配 TENANT_KEYS（多租户）或 SERVE_API_KEY，并设 APP_ENV=production"
        )
    elif has_global_key and not has_tenant_keys:
        logging.warning(
            "仅配置了全局 SERVE_API_KEY：租户仍由请求头 X-Tenant-Id 决定，"
            "**持有该 Key 者可冒充任意租户**（配额可绕过、审核归属不可信）。"
            "多租户场景请改用 TENANT_KEYS=租户:密钥[,...]"
        )
    if not config.ENABLE_DOCS:
        logging.info("API 文档已关闭（/docs、/redoc、/openapi.json 返回 404）")


_assert_auth_policy()


def _assert_shared_backend_policy() -> None:
    """启动期自检：`REDIS_URL` 配了、但共享后端实际没生效 —— 必须点名。

    为什么值得单独一条 ERROR（两个组件的构造期已经各自打过一次）
    --------------------------------------------------------
    这是一次**静默降级**：配置项填好了、服务照常启动、请求全部成功，
    唯一的差别是"额度实际 = 配置值 × 副本数"。这类问题不会触发任何告警，
    只会表现为"某天突然发现额度被超了"。所以除了让组件各自报错，
    这里再给一条**聚合的、可被日志系统按关键词检索**的结论行，
    让"这次部署到底共享了没有"有一个唯一答案。
    """
    if not config.REDIS_URL:
        return
    not_shared = [name for name, backend in
                  (("限流", _LIMITER.backend_name), ("配额", _QUOTA.backend_name))
                  if backend != "redis"]
    if not_shared:
        logging.error(
            "REDIS_URL 已配置，但 %s 的共享后端**未生效**（实际为进程内计数）⇒ "
            "多副本部署下限流/额度会按副本数放大。请确认已 `pip install redis` "
            "且 REDIS_URL 可达（启动日志里的 rate_limit.backend / quota.backend "
            "应显示 redis）。", "、".join(not_shared))


_assert_shared_backend_policy()


def _warn_about_missing_timeout() -> None:
    """生产环境未配请求级总超时 —— 给一条**可检索**的告警。

    为什么用告警而不是拒绝启动
    ------------------------
    与 `REQUIRE_AUTH` 不同，"没有总时限"不会立刻造成数据或资金损失（预算与日配额
    仍然拦着失控消耗），它的代价是**可运维性**：优雅关闭的排空上界失去意义、
    用户长时间无反馈、出问题时没有可对账的上界数字。这种量级的分歧用告警提示即可，
    拦启动反而会逼运维随手填一个未经评估的值。

    为什么要点名"LLM client 的 timeout 不够"
    -------------------------------------
    这是最容易被误认为"已经超时保护"的地方：`_MODEL` 上确实写着 `timeout=30`，
    但它约束的是**单次** HTTP 调用；一次请求可以包含多轮（自评审不通过会再来
    一轮），总时长因此没有任何上界。只看代码很容易得出"已经有超时了"的错误结论。
    """
    if config.IS_PRODUCTION and config.REQUEST_TIMEOUT_SECONDS <= 0:
        logging.warning(
            "生产环境未配置 REQUEST_TIMEOUT_SECONDS：单次请求没有总时限。"
            "注意 LLM client 的 timeout 只约束**单次调用**，多轮 self-review 的总"
            "时长无上界 ⇒ 优雅关闭的排空上界失去意义、异常请求会长期占用连接。"
            "建议设为 120（秒）。"
        )


_warn_about_missing_timeout()


def _client_ip(request: Request) -> str:
    """取客户端 IP 作为限流维度。

    request: FastAPI 请求对象。

    默认用直连地址；若部署在反向代理后面（Nginx/网关），真实来源在
    `X-Forwarded-For` 的第一段 —— 但要 **TRUST_PROXY_HEADERS=1 时才信它**，
    否则客户端可以随意伪造该头绕过限流。

    为什么用 `config.TRUST_PROXY_HEADERS` 而不是在函数里 `os.getenv`
    --------------------------------------------------------------
    它是"必须与部署形态一致"的安全开关，配错时**不报错、只是算错 IP**
    （反代后没开 → 全体共用一个桶；直连时开了 → 可伪造绕过）。
    收进 config 才会出现在启动日志的配置回显里，运维能自证（`/health` 刻意不暴露
    这些细节，它公网可达）。
    """
    if config.TRUST_PROXY_HEADERS:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def _tenant_of(x_tenant_id: str) -> str:
    """解析租户：请求头优先，缺省落到 RAG_TENANT 配置。"""
    return (x_tenant_id or "").strip() or config.DEFAULT_TENANT


def _retrieval_tenant(ident: "Identity") -> str:
    """决定把哪个租户交给**检索层**（向量库过滤）。

    这是隔离能否成立的关键一步，所以单独成一个函数并写清判据。

    为什么不能直接用 ident.tenant
    ----------------------------
    三种认证模式下租户的**可信度不同**：

      - `tenant-key`（配了 TENANT_KEYS）：租户由 API Key **反查**得出，客户端
        无法声明 —— 可信，可以安全地用它做检索隔离。这是唯一支持多租户隔离的模式。
      - `global-key` / `anonymous`：租户来自 `X-Tenant-Id` 请求头，**客户端可任意伪造**。
        此时若拿它去过滤向量库，得到的不是隔离而是**假隔离**：攻击者只要把请求头
        改成别人的租户，就能读到别人的资料。比不做隔离更危险 —— 因为它看起来是安全的。

    所以自报模式下**退回进程级默认租户**（`RAG_TENANT`），即"全实例一个库"，
    与改造前行为一致；同时启动期已由 `_assert_auth_policy` 告警过"租户不可信"。

    可观测租户（写进 trace metadata 的 `tenant`）仍然是 ident.tenant ——
    "这条请求自称是谁"在排查时有用，且不参与任何授权判断。
    """
    if ident.via == "tenant-key":
        return ident.tenant
    return config.DEFAULT_TENANT


def _rate_limit(request: Request, session: str) -> None:
    """限流闸门：按 IP + 会话双维度判，超限抛 429（带 Retry-After）。

    拆成独立函数是为了让**只读**端点（如会话状态快照）能复用限流而
    不触发配额与"会话认领"的副作用。
    """
    rl = _LIMITER.check(ip=_client_ip(request), session=session, route=request.url.path)
    if not rl.allowed:
        metrics.inc("rate_limited_total", scope=rl.scope or "unknown")
        logging.warning("限流拒绝：scope=%s limit=%d session=%s", rl.scope, rl.limit, session)
        raise HTTPException(status_code=429, detail=f"请求过于频繁（{rl.scope} 维度超限）",
                            headers={"Retry-After": str(rl.reset_after)})


def _quota_gate(tenant: str) -> int:
    """配额闸门：**预占**该租户当日额度，不足则抛 429；返回预占量供结算用。

    tenant: 租户（来自鉴权，可信）。

    为什么在这里"预占"而不是只"检查"
    -------------------------------
    旧实现是"这里 check、`_stream_graph` 的 finally 里 add"。判定与记账分在请求
    两端、中间隔着整次生成，并发的 N 个请求就会各自读到同一个"还没超"的旧值
    然后一起放行，最后各自 add —— 日额度被超掉 N 份。这不是理论竞态：它只在
    额度快用完时发生，而那正是额度最该被守住的时候。

    现在换成 `QuotaManager.claim`（先占后判，一次原子操作）。返回的预占量必须
    **原样**交给 `_stream_graph` 去 `settle`，否则预占永远不退、额度会被逐次吃光。
    """
    q = _QUOTA.claim(tenant)
    if not q.allowed:
        metrics.inc("quota_exceeded_total")
        logging.warning("配额拒绝：tenant=%s used=%d limit=%d", tenant, q.used, q.limit)
        raise HTTPException(status_code=429,
                            detail="今日配额已用尽，请明日再试或联系管理员",
                            headers={"Retry-After": str(q.reset_after)})
    return q.reserved


def _claim_session(session: str, tenant: str) -> None:
    """会话归属闸门：首次见到即认领；已被别的租户认领则抛 403。

    ⚠️ 这是**有副作用**的（写登记表），只允许用"要开始/继续一次运行"的端点。
    只读端点必须用 `_assert_session_readable`，否则攻击者只要先读一次就能
    把别人尚未开始的 thread_id 占为己有。
    """
    ok, owner = _SESSIONS.claim(session, tenant)
    if not ok:
        metrics.inc("session_owner_mismatch_total")
        logging.warning("会话归属不符，拒绝：session=%s owner=%s requester=%s",
                        session, owner, tenant)
        raise HTTPException(status_code=403, detail="该会话属于其他租户")


def _preflight(request: Request, session: str, tenant: str) -> None:
    """统一前置闸门：限流 + 会话归属，任一不过直接抛 4xx。

    request: 请求对象（取 IP 与路由名）。
    session: 会话 id（限流用，同时校验归属）。
    tenant: 租户（来自鉴权，可信）。

    顺序：**限流 → 会话归属**。限流最便宜（本地计数），先挡住明显刷量；归属校验
    可能走数据库，所以放后面。

    ⚠️ **归属校验必须排在业务校验之前**：`/review`、`/resume` 在通过闸门后还要读
    检查点状态（不在审核态 → 409 / 无未完成生成 → 409）。如果先做状态校验，攻击者
    就能用别人的 thread_id 区分"存在且在审核态"与"不存在"，把 404/409 变成枚举探针。

    ⚠️ 这里**刻意不做配额预占**：那几个 409 会让请求在进入生成前就结束，而预占的
    额度只有 `_stream_graph` 的 finally 会退还 —— 放在这里必然泄漏额度。配额预占
    因此移到 `_sse`，与结算成对（理由见 `_sse` 的说明）。
    """
    _rate_limit(request, session)
    _claim_session(session, tenant)


# ---------------------------------------------------------------------------
# 会话状态快照（断点续传的基础）
# ---------------------------------------------------------------------------
# 为什么需要它
# ----------
# 好消息是：**状态早就在检查点里了**。`init → retrieve → generate → evaluate →
# human_review` 每个节点边界都会落一次检查点，`aget_state()` 随时能取回完整对话、
# 最新草稿、评分与引用。坏消息是：**没有任何端点把它暴露出去** ——
# 于是 SSE 一断（弱网、切后台、代理超时），客户端手里只剩下"已经打到屏幕上的那半句"，
# 除了从头重问一遍别无选择，而重问会**再花一次 token**（生成 + 评审都可能重跑）。
#
# 所以这一节只做一件事：把检查点里已经存在的事实，按客户端能用的形态暴露出来。
#
# 那个 `next` 元组是全部关键
# ------------------------
# `state.next` 是"还没执行完的节点"，它同时回答了"能不能续"和"该用哪个接口续"：
#   - 含 human_review      → 在等人审，必须走 POST /v1/review（要人的决定，不是续跑）；
#   - 非空但不含 human_review → 中途断了，可 POST /v1/chat/resume 续跑；
#   - 空                    → 本轮已收尾（或从未开始）。
# 这不是推测：实测过"流中途取消"后 `next == ('generate',)` —— **未完成的节点仍留在
# 里面**，所以续跑会重新执行它（重放该节点的产出，而不是接着上次的半个 token 拼）。
_ROLE_MAP = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}


def _serialize_messages(messages) -> list:
    """把 LangChain 消息列表转成 {role, content} 的纯 JSON 结构。

    messages: state["messages"]，元素是 BaseMessage（生产）或 dict（某些反序列化路径）。

    为什么要转换而不是直接返回：BaseMessage 不是 JSON 可序列化对象，直接塞进
    响应体要么报错、要么被 FastAPI 用 repr 糊过去（那正是"接口返回了看不懂的东西"）。
    同时把 `type`（human/ai）映射成通用的 `role`（user/assistant），
    让不熟悉 LangChain 的客户端也能直接渲染。
    """
    out = []
    for m in messages or []:
        if isinstance(m, dict):
            raw_role = m.get("type") or m.get("role") or "unknown"
            content = m.get("content", "")
        else:
            raw_role = getattr(m, "type", None) or "unknown"
            content = getattr(m, "content", "")
        out.append({"role": _ROLE_MAP.get(raw_role, raw_role),
                    "content": content if isinstance(content, str) else str(content)})
    return out


def _interrupt_payload(state):
    """从 StateSnapshot.tasks 里取出 interrupt 的原始载荷（人审界面要用）。

    返回 dict 或 None。`human_review` 节点传给 `interrupt()` 的是
    `{"draft", "score", "feedback", "error"}`，正是审核工作台需要的全部信息。
    """
    for task in (getattr(state, "tasks", None) or ()):
        for it in (getattr(task, "interrupts", None) or ()):
            value = getattr(it, "value", None)
            if isinstance(value, dict):
                return value
    return None


def _session_view(state, thread_id: str) -> dict:
    """把 StateSnapshot 拍成对外的会话视图（纯 JSON 可序列化）。

    state: `graph.aget_state(cfg)` 的返回值。
    thread_id: 会话 id（回显，便于客户端确认自己请求的是哪条）。
    """
    values = getattr(state, "values", None) or {}
    pending = tuple(getattr(state, "next", None) or ())

    if not values and not pending:
        status = "unknown"            # 从未开始（或已被清理）
    elif "human_review" in pending:
        status = "awaiting_review"    # 等人审 → 走 /v1/review
    elif pending:
        status = "incomplete"         # 中途断了 → 走 /v1/chat/resume
    else:
        status = "completed"

    review = None
    if status == "awaiting_review":
        # 优先用图里真正的 interrupt 载荷；取不到时用 state 字段兜底
        # （两条路径的信息量相同，只是来源不同）。
        review = _interrupt_payload(state) or {
            "draft": values.get("output"),
            "score": values.get("score"),
            "feedback": values.get("feedback"),
            "error": None,
        }

    return {
        "thread_id": thread_id,
        "status": status,
        # 客户端可直接据此决定要不要提示"继续生成"按钮
        "resumable": status == "incomplete",
        "iteration": values.get("iteration", 0),
        "score": values.get("score"),
        "feedback": values.get("feedback") or "",
        "draft": values.get("output") or "",
        "citations": values.get("citations") or [],
        "messages": _serialize_messages(values.get("messages")),
        "pending_review": review,
    }


def _assert_session_readable(thread_id: str, tenant: str) -> None:
    """读会话前的归属校验。**不写登记表**（与 `_claim_session` 的语义刻意不同）。

    为什么读路径不能沿用 `_claim_session`：`claim()` 是"首次见到即认领"，
    用在只读端点上会让攻击者**先读一次就把别人还没开始的 thread_id 占住** ——
    一个读操作不该产生归属副作用。

    多租户模式下**查不到属主一律 404（fail-closed）**：正常情况下每条会话都先
    经过 /chat 的 `_preflight` 认领过，查不到属主就意味着"这个 id 不存在"。
    若不这样处理，返回内容（或 403/404 的差异）会变成一个"检查点是否存在"的
    可枚举探针。单租户下所有请求同属主，没有区分能力，直接放行。
    """
    if not _SESSIONS.enabled:
        return
    owner = _SESSIONS.owner_of(thread_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if owner != tenant:
        metrics.inc("session_owner_mismatch_total")
        logging.warning("会话归属不符，拒绝读取：session=%s owner=%s requester=%s",
                        thread_id, owner, tenant)
        raise HTTPException(status_code=403, detail="该会话属于其他租户")


async def _delete_thread_state(thread_id: str) -> bool:
    """删除该会话的检查点（完整消息历史 / 草稿 / 评分 / 引用）。

    两个后端的方法名不同，这里按能力探测而不是按类型判断：
      - PG 的 `AsyncPostgresSaver` 是**异步** saver，只有 `adelete_thread`；
      - 内存 `MemorySaver` 是同步 saver，只有 `delete_thread`。
    用 `isinstance` 分支会在 langgraph 换类名时静默失效（退化成"什么都没删、
    却告诉用户已删除"），按方法名探测则至少会落到 `False` 上被看见。

    返回是否真的执行了删除（无 saver / 无对应方法时为 False）。
    """
    cp = getattr(graph, "checkpointer", None)
    if cp is None:
        return False
    adelete = getattr(cp, "adelete_thread", None)
    if adelete is not None:
        await adelete(thread_id)
        return True
    delete = getattr(cp, "delete_thread", None)
    if delete is not None:
        delete(thread_id)
        return True
    logging.warning("checkpointer 没有删除接口，会话状态未被清除：%s", type(cp).__name__)
    return False


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------
# 字段级上限存在的理由（与 ASGI 层的字节上限是**两层**，见 app/config.py）：
#   - ASGI 层拦的是"体量把内存撑爆"，回 413，粒度粗；
#   - 这里拦的是"单个字段失控"，回 422 并**点名是哪个字段超了多少** ——
#     客户端能直接改，而不是对着 413 猜。
# 校验器在**调用时**读 config（而不是把常量写进 Field(max_length=...)）：
# 后者在模块导入时就冻结了，测试无法通过 monkeypatch 调整阈值来验证边界。
def _bounded(value: str, limit: int, field: str) -> str:
    """字段长度校验：超限抛 ValueError（Pydantic 会转成 422 并带上这条消息）。"""
    if len(value) > limit:
        raise ValueError(f"{field} 超长：{len(value)} > {limit} 字符")
    return value


class ChatReq(BaseModel):
    thread_id: str
    message: str

    @field_validator("thread_id")
    @classmethod
    def _v_thread_id(cls, v: str) -> str:
        return _bounded(v, config.MAX_THREAD_ID_CHARS, "thread_id")

    @field_validator("message")
    @classmethod
    def _v_message(cls, v: str) -> str:
        return _bounded(v, config.MAX_MESSAGE_CHARS, "message")


class ReviewReq(BaseModel):
    thread_id: str
    action: str            # accept | revise
    feedback: str = ""

    @field_validator("thread_id")
    @classmethod
    def _v_thread_id(cls, v: str) -> str:
        return _bounded(v, config.MAX_THREAD_ID_CHARS, "thread_id")

    @field_validator("feedback")
    @classmethod
    def _v_feedback(cls, v: str) -> str:
        return _bounded(v, config.MAX_FEEDBACK_CHARS, "feedback")


class ResumeReq(BaseModel):
    """续跑请求体：只有 thread_id —— 要跑的内容早已在检查点里。"""

    thread_id: str

    @field_validator("thread_id")
    @classmethod
    def _v_thread_id(cls, v: str) -> str:
        return _bounded(v, config.MAX_THREAD_ID_CHARS, "thread_id")


# ---------------------------------------------------------------------------
# SSE 流：把图的流式输出转成事件
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# SSE 事件载荷：结构化数据**必须自己 json.dumps**
# ---------------------------------------------------------------------------
# 实测踩到的坑（2026-09-28）：直接 yield `{"event": "citations", "data": cites}` 时，
# sse_starlette 的 `ServerSentEvent.encode` 对 data 用的是 `str(self.data)`
# （见其 event.py：`for chunk in self._LINE_SEP_EXPR.split(str(self.data))`），
# 于是线上拿到的是 **Python repr**：
#
#     data: [{'n': 1, 'source': 'intro.txt', 'score': 0.62, 'page': 3}]
#
# 单引号、`True`/`None` —— 只有 Python 的 `ast.literal_eval` 读得懂，
# **JS / Go / Java 客户端一律解析失败**。而 `data` 的 content-type 又是
# `text/event-stream`，前端按 JSON.parse 处理是标准做法，静默失败后
# 表现为"引用区永远是空的"，且服务端日志毫无异常（契约错了，但不是报错）。
#
# 库里有 `JSONServerSentEvent` 做这件事，但我们自己 dumps 更稳：
# 不依赖具体版本的行为，且当 data 已是 str 时 `str(s) == s`（幂等）。
# 全部结构化事件都必须走这里；`token` 的正文是**裸文本**（不能 JSON 化，
# 否则客户端还得再剥一层引号，且内容里的引号会被转义）。
def _sse_json(event: str, payload) -> dict:
    """构造 data 为 JSON 字符串的 SSE 事件。

    event: 事件名（citations / need_review / budget_exceeded）。
    payload: 任意可 JSON 序列化的对象。
    """
    return {"event": event, "data": json.dumps(payload, ensure_ascii=False)}


def _client_error(exc: BaseException) -> str:
    """把异常转成**可以发给客户端**的文案。

    为什么不直接 `str(e)`
    --------------------
    上游异常经常把内部细节带在消息里，客户端不特定、也不该看到：
      - `psycopg.OperationalError` 带**主机/端口/用户名**（DSN 的一部分）；
      - `openai.RateLimitError` 带 **API base URL 与请求 id**；
      - 文件类异常带**服务器绝对路径**（部署结构）。
    生产环境一律只回一句通用文案 + `request_id`：用户凭 rid 找运维，
    运维在日志里按 rid 拿到完整堆栈 —— 信息没丢，只是没泄露给外面。

    开发环境保留 `类型: 消息`，否则本地排障要靠翻日志，效率太低。
    """
    if not config.IS_PRODUCTION:
        return f"{type(exc).__name__}: {exc}"
    rid = get_request_id()
    return ("服务内部错误，请稍后重试"
            + (f"（request_id={rid}）" if rid else ""))


class _DisconnectSignal:
    """"客户端已断开"信号：由 sse_starlette 的官方钩子置位，本模块消费。

    为什么不能再用 `request.is_disconnected()` 轮询（原实现，2026-09-29 废弃）
    ------------------------------------------------------------------------
    `EventSourceResponse.__call__` 用 `task_group.start_soon` 常驻了一个
    `_listen_for_disconnect(receive)`，它**独占**消费 ASGI 的单消费者通道
    `receive()`。而 `request.is_disconnected()` 读的是同一个通道 —— 谁先调谁
    拿到 `http.disconnect`，库的任务在**响应开始的那一刻**就阻塞在那儿了，
    0.5s 一轮的轮询永远晚一步；更致命的是库拿到 `http.disconnect` 后立刻
    `cancel_scope.cancel()`，整个 task group（含我们的轮询任务）被取消，
    轮询连下一次 `sleep` 都等不到。

    实测证据（黑洞上游：读完请求后永不回应，客户端 4s 后断开）：
      生成确实被取消了（生成器收到 `CancelledError`）；
      但 `client_disconnected_total` **一次都没计数**、日志**一行都没有**。
    即：止损生效靠的是**库的取消**，而我们的指标与日志是**死代码**。
    轮询还额外有害 —— 万一它先抢到 `http.disconnect`，库的监听任务就会永久
    阻塞在 `receive()` 上，task group 再也不会取消，连接彻底挂死。

    改用库自带的钩子 `EventSourceResponse(client_close_handler_callable=...)`：
    它在 `_listen_for_disconnect` 收到 `http.disconnect` 时被 `await` 调用。

    ⚠️ 由此带来三条硬约束：
      1. 回调**必须是 async**（库是 `await handler(message)`；同步函数返回 None
         会炸成 `TypeError: object NoneType can't be used in 'await' expression`）；
      2. 回调**不得 await 耗时操作** —— 它跑在库的取消路径上，在这里等上游关流
         会把 task group 的取消一起拖住；
      3. 回调**不得在这里 `aclose()` 上游生成器** —— 那个生成器此刻正被
         `_stream_response` 任务驱动着，并发调用会命中 asyncio 的
         "asynchronous generator is already running" 保护而抛 RuntimeError。
         关流交给生成器自己的 `finally`（取消会带着 `CancelledError` 打进生成器
         的挂起点，实测确认）。
    """

    __slots__ = ("_req_kind", "_closed", "_event")

    def __init__(self, req_kind: str):
        self._req_kind = req_kind
        self._closed = False
        self._event = asyncio.Event()

    async def mark_closed(self, _message=None) -> None:
        """由 sse_starlette 在收到 `http.disconnect` 时调用。**幂等、不阻塞**。

        _message: ASGI 的 `{"type": "http.disconnect"}` 消息（库原样透传）。
                  这里用不到 —— 需要的信息都在闭包里，签名只是为了对上库的契约。
        """
        if self._closed:
            # 幂等：库的监听循环理论上可能多次进入，重复计数会让指标虚高
            return
        self._closed = True
        metrics.inc("client_disconnected_total", kind=self._req_kind)
        logging.warning("%s 客户端已断开，提前终止生成（上游即将被取消）", self._req_kind)
        self._event.set()

    async def wait(self) -> None:
        """等"已断开"到来。给可被取消的 `_watch_disconnect` 任务用。"""
        await self._event.wait()

    @property
    def closed(self) -> bool:
        """是否已断开。`_stream_graph` 主循环每轮读它，用来跳过收尾事件。"""
        return self._closed


class _RequestDeadline:
    """请求级**总时限**：给一次请求的每个"可能卡住的等待"套上同一个绝对截止时刻。

    为什么不能只"在循环里比对时间"（这是最容易写错的地方）
    ---------------------------------------------------
    主循环只在"上游产出 chunk"时才转一圈。上游限流重试、排队、连接挂死时，
    `async for` 就**停在 await 上**，循环体一次都不执行 —— 于是"看表"这件事
    根本没机会发生。2026-09-28 实测过一次上游 429 重试 36 秒，期间主循环零次
    迭代（这条证据同时是 `_watch_disconnect` 必须独立成任务的原因）。

    所以每次等待都必须能被**从外部打断**，做法是把等待放进独立的任务里、超时
    就取消它。`run()` 用 `asyncio.wait_for` 做这件事；取消会带着 CancelledError
    打进上游生成器的挂起点，把它连同上层的 LLM 调用一起收掉 —— 顺带解决了
    "超时之后上游还在继续计费"这个副作用。

    与 `budget_exceeded` 的区别：预算管的是"产出多少"，本类管的是"花多久"。
    两者都**不把已产出的正文作废**，但超时是**终止性**的（`timeout` 事件之后
    直接收流，不再有 `done`）—— 因为紧接着要做的事（`aget_state`）本身也是
    网络调用，继续等下去等于没有时限。
    """

    __slots__ = ("seconds", "_at")

    def __init__(self, seconds):
        # 负数/None/0 一律归为"不限"。注意判据是 <= 0 而不是 == 0：配置读歪成
        # 负数时应当退化成"不限"（默认行为），而不是变成"立刻超时"。
        self.seconds = float(seconds or 0)
        self._at = (time.monotonic() + self.seconds) if self.seconds > 0 else None

    @property
    def active(self) -> bool:
        """是否启用了总时限（0/负数 = 不限，行为与改造前完全一致）。"""
        return self._at is not None

    def remaining(self) -> float:
        """距截止还剩多少秒（`inf` = 不限）。"""
        if self._at is None:
            return float("inf")
        return self._at - time.monotonic()

    async def run(self, factory):
        """在剩余时限内执行 `factory()` 返回的**新**协程；超时抛 `asyncio.TimeoutError`。

        factory: 无参可调用对象，**每次调用都要返回一个新协程**（如
            `stream.__anext__`、`lambda: graph.aget_state(cfg)`）。

        为什么传工厂而不是协程对象
        ------------------------
        传协程对象就必须**先创建**它；一旦"已经没有剩余时间"（`remaining <= 0`）
        而没去 await 它，就会留下一个"never awaited"的协程（RuntimeWarning +
        潜在的真实资源泄漏）。传工厂能保证"只有真的要在时限内跑时才创建"。

        不限时限时**退化成一个普通 await**（不额外建任务）：默认配置下
        （`REQUEST_TIMEOUT_SECONDS=0`）路径与改造前逐一字节等价。
        """
        if self._at is None:
            return await factory()
        remaining = self.remaining()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return await asyncio.wait_for(factory(), timeout=remaining)


def _timeout_event(req_kind: str, deadline: "_RequestDeadline", produced) -> dict:
    """总时限耗尽：计数 + 告警 + 生成 SSE 事件（结构化，走 `_sse_json`）。

    req_kind: "chat" / "review" / "resume"，用于日志与指标标签。
    deadline: 用于回显配置的秒数。
    produced: 已产出的文本片段列表（算字符数；**不折算 token** —— 那是配额的事）。
    """
    chars = sum(len(x) for x in produced)
    metrics.inc("request_timeout_total", kind=req_kind)
    logging.warning("%s 请求超过总时限 %ss（已产出 %d 字），终止生成",
                    req_kind, deadline.seconds, chars)
    return _sse_json("timeout",
                     {"seconds": deadline.seconds, "produced_chars": chars})


async def _watch_disconnect(signal, produced, state):
    """独立任务：等"客户端已断开"信号，然后**尽力**关闭上游流。

    为什么必须是**独立任务**，而不是在主循环里顺手检查
    ------------------------------------------------
    主循环只在"上游产出 chunk"时才转一圈。如果模型长时间不吐字（重试、排队、
    上游限流），循环体根本不执行 —— 这时客户端早就走了，服务端还在傻等，
    token 照付。2026-09-28 实测：上游 429 重试了 **36 秒**，期间主循环一次都没转。

    为什么"尽力"而不是"保证"：本任务与库的取消竞态 —— 信号置位后库几乎立刻
    取消整个 task group。真正的关流保证在生成器自己的 `finally` 里（取消会带
    `CancelledError` 打进生成器的挂起点），这里只是"抢在取消之前更早地关一次"，
    并补上"断连前已产出多少"这条日志。

    计数与告警**刻意不在这里**：它们必须同步、无竞态地发生，所以放在
    `_DisconnectSignal.mark_closed` 里；本任务若被取消丢掉的只是那条锦上添花的日志。
    """
    try:
        await signal.wait()
        logging.info("断连中止：已产出 %d 段、共 %d 字",
                     len(produced), sum(len(x) for x in produced))
        stream = state.get("stream")
        if stream is not None:
            try:
                await stream.aclose()
            except Exception:  # noqa: BLE001  关闭期异常已无意义（含"已在运行"竞态）
                logging.debug("断连时关闭上游流出现异常（已忽略）", exc_info=True)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001  监听任务出问题不该影响本次生成
        logging.debug("断连监听任务异常退出", exc_info=True)


async def _stream_graph(incoming, config, req_kind: str, tenant: str = "",
                        thread_id: str = "", budget=None, disconnect=None,
                        reserved: int = 0, timeout: float = 0, idem_key: str = ""):
    """把图的流式输出转成 SSE 事件。

    incoming: 送进图的输入。三种形态：
        - 首轮对话：`{"messages": [HumanMessage(...)]}`；
        - 人工审核 resume：`Command(resume={...})`；
        - **断点续传**：`None` —— langgraph 的"从最近一次检查点继续"约定，
          会重新执行 `state.next` 里那个未完成的节点（实测确认）。
    config: langgraph 运行配置，形如 `{"configurable": {"thread_id": ...}}`。
        **必须带 thread_id**：它决定 checkpoint 落哪条会话，/review 正是靠它
        找回被打断的那次运行。tracing 的 metadata/callbacks 也由调用方并进这里。
    req_kind: 仅用于日志文案（"chat" / "review"），标明是哪个端点触发的流。
    tenant: 租户 id。用于**把本次消耗计入配额**，并写进审核队列。
    thread_id: 会话 id。用于把待审记录与通知关联到会话。
    budget: 可选的 RequestBudget。给了且限额 >0 时，**超预算即停止继续生成**
        （已产出的正文照常返回，不是把整次回答作废）。
    disconnect: 可选 `_DisconnectSignal`。给了就**并行等待"客户端已断开"**，
        一旦置位立即停止生成并跳过后续事件 —— 不做这件事的代价是：用户关掉页面后
        模型照样生成到自然结束，**token 照付**。传 None（测试/CLI）时行为不变。
    idem_key: `Idempotency-Key` 请求头。**只有成功的请求才会把它标记为已完成**；
        失败或未完成时撤销键，让客户端能用同一个键重试（见 finally 的说明）。
        ⚠️ 它**不能**由这里自己造：信号必须在构造 `EventSourceResponse` 之前
        就存在，才能被 `client_close_handler_callable` 接上（见 `_sse`）。
    reserved: `_preflight` 里预占的配额量。请求结束时由 `settle` 把它换成实际消耗；
        不传（0）时退化成"只加实际值"，即预占不会被退。
    timeout: **请求级总时限**（秒），0 = 不限（默认，行为与改造前一致）。
        约束的是"消费上游流"与收尾的 `aget_state` —— 也就是会随上游变慢而失控的
        那部分。超时后下发 `timeout` 事件并**收流**（已产出的正文照常交付）。

    事件类型：
      token            —— 逐段生成的正文
      citations        —— **可回溯的引用**（来源/页码/章节/保真原文节选/表格网格/图片路径）
      need_review      —— 需人工审核（附草稿、评分、意见）；同时入队 + 发通知
      budget_exceeded  —— 单请求 token 预算耗尽，生成被提前终止
      timeout          —— **请求级总时限耗尽**，流被终止（终止性事件，其后没有 done）
      done / error

    citations 放在 token 之后、done 之前下发：它描述的是"上面这段回答依据了什么"，
    顺序上必须在正文之后才有意义；同时它带着 chunk_id 与页码/坐标，
    用户点开就能回到原文件核对，而不是只能相信模型。
    """
    budget = budget if budget is not None else RequestBudget(0)
    deadline = _RequestDeadline(timeout)
    produced: list[str] = []
    stream = None
    watcher = None
    # `state` 用 dict 跨任务共享（生成器写 stream、watcher 读取后关闭）。
    # 断连标志不在这个 dict 里 —— 它在 `disconnect` 上，那个值由库的回调写。
    state = {"stream": None}
    # 本次请求是否**成功完成**。它决定幂等键的收尾方式（标记完成 / 撤销），
    # 而这件事只在收尾那一刻才知道 —— 所以必须在 try 之外可见。
    succeeded = False
    try:
        if disconnect is not None:
            watcher = asyncio.create_task(
                _watch_disconnect(disconnect, produced, state))
        stream = graph.astream(incoming, config=config, stream_mode="messages")
        state["stream"] = stream
        # 手工迭代（而不是 `async for`）：只有显式拿到 `__anext__`，才能把**每一次
        # 等待**都交给 `deadline.run` —— 而"上游一直不返回"正是唯一需要总时限的
        # 场景，那种情况下 `async for` 会永久挂住、循环体一次都不执行（见
        # `_RequestDeadline` 的说明）。`timeout=0` 时退化成普通 await，无额外开销。
        anext = stream.__anext__
        while True:
            # 断连信号已置位：停止消费（这里只是兜底 —— 真实链路上库会同时取消
            # 本生成器，取消会带着 CancelledError 打进下一次挂起点）
            if disconnect is not None and disconnect.closed:
                break
            try:
                msg, _meta = await deadline.run(anext)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                # 总时限耗尽：**终止性**返回（不查状态、不补 done）—— 紧接着的
                # `aget_state` 本身也是网络调用，继续等下去等于没有时限。
                yield _timeout_event(req_kind, deadline, produced)
                return
            if msg.content:
                text = msg.content
                if not budget.add(text):
                    # 预算耗尽：立刻停止继续生成（而不是等模型自己收尾）
                    metrics.inc("llm_budget_exceeded_total")
                    logging.warning("单请求 token 预算耗尽（已用 %d/%d），终止生成",
                                    budget.used, budget.limit)
                    yield _sse_json("budget_exceeded",
                                    {"used": budget.used, "limit": budget.limit})
                    break
                produced.append(text)
                yield {"event": "token", "data": text}

        if disconnect is not None and disconnect.closed:
            # 客户端已经走了：不必再查状态、不必入队、更不必下发事件（没人收）。
            # 直接返回，finally 里的计费照常执行。
            return

        try:
            state_snap = await deadline.run(lambda: graph.aget_state(config))
        except asyncio.TimeoutError:
            # 生成已经跑完、卡在收尾的检查点读取上：同样按总时限收流。
            yield _timeout_event(req_kind, deadline, produced)
            return
        values = state_snap.values

        cites = values.get("citations") or []
        if cites:
            yield _sse_json("citations", cites)

        if state_snap.next and "human_review" in state_snap.next:
            item = {
                "thread_id": thread_id or (config.get("configurable", {}) or {}).get("thread_id", ""),
                "draft": values.get("output") or "",
                "score": values.get("score"),
                "feedback": values.get("feedback") or "",
                "tenant": tenant or "",
            }
            # 落队列 + 异步通知：即使此刻没有客户端在线，待审内容也不会"消失"
            _REVIEW_QUEUE.add(**item)
            metrics.inc("review_needed_total")
            yield _sse_json("need_review", {
                "draft": values.get("output"),
                "score": values.get("score"),
                "feedback": values.get("feedback"),
            })
        else:
            yield {"event": "done"}
        # 走到这里就算成功（无论是正常收尾还是停在审核态）——
        # 两种情况下这次请求都已经**产生了真实消耗与状态**，重试都不该重跑。
        succeeded = True
    except Exception as e:  # noqa: BLE001
        if disconnect is not None and disconnect.closed:
            # 客户端已走：上游被取消是**预期行为**而非真故障；而且此时也没人收
            # error 事件。若照旧记成流异常，会污染错误率指标、掩盖真实故障。
            logging.info("%s 流因客户端断开而中止", req_kind)
        else:
            metrics.inc("chat_stream_errors_total", kind=req_kind)
            # 完整异常（含堆栈）只进日志；给客户端的是脱敏文案，见 _client_error
            logging.exception("%s 流异常", req_kind)
            yield {"event": "error", "data": _client_error(e)}
    finally:
        # 先停监听任务，再关上游 —— 反序会留下竞态（watcher 可能在关闭后又关一次）。
        if watcher is not None:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logging.debug("等待断连监听退出时异常", exc_info=True)

        # 显式关闭上游生成器。`async for` 里 break 只是跳出循环，生成器本身要等
        # GC 才被关闭 —— 那段时间底层 LLM 调用仍在跑（这正是"断连了还在计费"的
        # 另一半原因）。主动 aclose 才能把取消真正传导下去。
        if stream is not None:
            try:
                await stream.aclose()
            except Exception:  # noqa: BLE001  关闭期异常不该覆盖真实结果
                logging.debug("关闭上游流时出现异常（已忽略）", exc_info=True)

        # 计费结算：把请求开始时**预占**的额度换成实际消耗（无论成功/中断/断连都算）。
        # 用 settle 而不是 add：预占与实际之间有差额，不结算就等于把差额永久计入用量
        # （预占只增不减 ⇒ 配额比真实消耗快得多地耗尽，且全程无报错）。
        # 注意 `used == 0` 时也要结算 —— 那种情况正是"预占要全额退还"。
        used = estimate_tokens("".join(produced))
        _QUOTA.settle(tenant, reserved, used)
        if used:
            # 刻意不把 tenant 当标签：租户数可能成百上千，会让指标基数爆炸
            metrics.inc("llm_tokens_total", amount=used)
            metrics.observe("llm_tokens_per_request", used)

        # 幂等键收尾。**只有成功的请求才占用键**：
        #   - 成功 → 标记 DONE（保留到 TTL），此后同键重试被 409 拦住并指路；
        #   - 失败/断开 → 撤销键，客户端可以用**同一个键**重试。
        # 若不分这两种情况（例如一律标记 DONE），一次偶发失败会让那个键在 TTL 内
        # 彻底不可用 —— 客户端的重试逻辑完全正确，却始终收到 409。
        if idem_key:
            if succeeded:
                _IDEMPOTENCY.finish(tenant, idem_key)
            else:
                _IDEMPOTENCY.forget(tenant, idem_key)


# ---------------------------------------------------------------------------
# SSE 响应构造：所有"生命周期必须成对"的东西都收在这里
# （断连信号、配额预占→结算）
# ---------------------------------------------------------------------------
def _sse(graph_input, run_cfg, req_kind: str, *, tenant: str, thread_id: str,
         budget=None) -> EventSourceResponse:
    """构造 SSE 响应：断连信号成对接线 + 配额预占（结算交给生成器）。

    为什么必须成对构造（而不是各端点自己拼）
    ----------------------------------------
    断连信号有两端，缺一不可：
      - **写端**在 sse_starlette 里 —— 只有 `client_close_handler_callable` 能拿到
        真正的 `http.disconnect`（库自己常驻消费 `receive()`，外部读不到）；
      - **读端**在 `_stream_graph` 里 —— 生成器与 watcher 据此收手。
    分散在三个端点里手工接线，就要在 6 处保持一致；而漏接任一端**不会报错**，
    只表现为"线上断了却既不计数、也不减损"（这正是修复前线上发生的事）。
    收进一个函数后，接错只可能错一次。

    配额预占为什么也放在这里
    ----------------------
    **预占与结算必须落在同一条"一定会走到 finally"的路径上**。预占若放在
    `_preflight`（各端点业务校验之前），`/review` 与 `/resume` 的 409 会让请求
    在进入生成前就返回，而退还只发生在 `_stream_graph` 的 finally ⇒ **额度永久
    泄漏**（表现成"配额莫名少了一截"，且没有任何报错）。

    调换顺序的代价是配额排在业务校验之后，但换来两个好处：① 不会为注定 409 的
    请求预占额度；② 预占一旦成功，创建生成器之后的每一步都有 finally 兜底。
    安全性不受影响 —— 跨租户防护由 `_preflight` 的会话归属校验负责，那一步仍然
    排在业务校验**之前**。

    ⚠️ 传给库的回调必须是 async 且不阻塞 —— 理由见 `_DisconnectSignal` 的文档。

    graph_input: 送进图的输入（首轮 dict / resume 的 Command / 续跑的 None）。
    run_cfg: langgraph 运行配置。
    req_kind: "chat" / "review" / "resume"，用于日志与指标标签。
    tenant / thread_id / budget: 透传给 `_stream_graph`（配额计量、入队、预算止血）。

    请求级总时限**在这里读配置**（而不是在各端点）：三个端点都要它，而 `_sse`
    已经是它们共同经过的唯一一处。调用时读（而非模块级常量）才能被测试
    monkeypatch —— 与字段闸门同一个理由（见 `_bounded`）。
    """
    # 配额预占必须**紧贴**生成器创建：成功之后，结算就由 `_stream_graph` 的
    # finally 保证；失败（429）则当场抛出、不留残值。
    reserved = _quota_gate(tenant)
    signal = _DisconnectSignal(req_kind)
    return EventSourceResponse(
        _stream_graph(graph_input, run_cfg, req_kind, tenant=tenant,
                      thread_id=thread_id, budget=budget, disconnect=signal,
                      reserved=reserved,
                      timeout=config.REQUEST_TIMEOUT_SECONDS),
        client_close_handler_callable=signal.mark_closed,
        # 提示词版本随响应头下发（**不改 SSE 事件载荷** —— 那是对外契约）。
        # 颗粒度正好是需要的那个：一条回答 = 一个响应 = 一个版本号，
        # 客户端、网关访问日志、抓包都能记下来。线上答得不好时，
        # "这条是 1.0.0 生成的、那条是 1.0.1 生成的"可被直接核对。
        headers={"X-Prompt-Version": PROMPT_VERSION},
    )


# ---------------------------------------------------------------------------
# 路由（版本化：/v1/*；旧路径保留为别名）
# ---------------------------------------------------------------------------
v1 = APIRouter(prefix="/v1", tags=["v1"])


@v1.post("/chat")
async def chat_v1(req: ChatReq, request: Request,
                  x_api_key: str = Header(default=""),
                  x_tenant_id: str = Header(default="")):
    """POST /v1/chat：启动一轮 SSE 流式对话。

    req: 请求体 ChatReq —— thread_id 串起多轮与后续人工审核，message 是用户输入。
    x_api_key: 请求头 X-API-Key。配了 TENANT_KEYS 时由它**反查租户**；否则校验 SERVE_API_KEY。
    x_tenant_id: 请求头 X-Tenant-Id，多租户模式下**仅用于核对**（与 Key 不符返回 403）。

    内部把 message 包成 HumanMessage 作为图输入；若图停在 human_review，
    流里会下发 need_review 事件（草稿 + 评分 + 意见），由前端调 /review 续跑。
    请求处理前会过"限流 → 配额 → 会话归属"三道闸门。
    """
    ident = _authenticate(x_api_key, x_tenant_id)
    tenant = ident.tenant
    _preflight(request, session=req.thread_id, tenant=tenant)
    run_cfg = tracing.build_run_config(
        {"configurable": {"thread_id": req.thread_id}},
        thread_id=req.thread_id, tenant=tenant,
        rag_tenant=_retrieval_tenant(ident))
    incoming = {"messages": [HumanMessage(content=req.message)]}
    budget = RequestBudget(_QUOTA.request_limit)
    return _sse(incoming, run_cfg, "chat", tenant=tenant,
                thread_id=req.thread_id, budget=budget)


@v1.post("/review")
async def review_v1(req: ReviewReq, request: Request,
                    x_api_key: str = Header(default=""),
                    x_tenant_id: str = Header(default="")):
    """POST /v1/review：人工审核 resume，续跑被 interrupt 挂起的图。

    req: 请求体 ReviewReq。thread_id 用于定位会话；action 只允许 accept / revise；
        feedback 在 revise 时必填（accept 时可留空）。
    x_api_key / x_tenant_id: 同 /v1/chat。

    三道前置校验，任一条不过都不进入图：
      - action 非 accept/revise → 400；
      - revise 但 feedback 全空白 → 400；
      - 该 thread 当前并不停在 human_review（如已完成、或从未触发）→ 409。
    通过后把队列里的记录标记为已处理（accepted / revised）。
    """
    ident = _authenticate(x_api_key, x_tenant_id)
    tenant = ident.tenant
    if req.action not in ("accept", "revise"):
        raise HTTPException(status_code=400, detail="action 必须是 accept 或 revise")
    if req.action == "revise" and not req.feedback.strip():
        raise HTTPException(status_code=400, detail="revise 必须填写 feedback")

    _preflight(request, session=req.thread_id, tenant=tenant)
    cfg = {"configurable": {"thread_id": req.thread_id}}
    state = await graph.aget_state(cfg)
    if not (state.next and "human_review" in state.next):
        raise HTTPException(status_code=409, detail="该会话当前不在人工审核状态")

    # 人已处理 → 出队（审核队列只用来看"还有哪些没人管"）。
    # 同时**留痕**：谁（actor=租户）、以什么身份（via）、做了什么决定、留了什么话。
    # 少了这一步，复盘时只能看到"这条被 accept 了"，无法判断越权/误操作，也追不到人。
    _REVIEW_QUEUE.resolve(
        req.thread_id,
        "accepted" if req.action == "accept" else "revised",
        actor=tenant, via=ident.via, note=req.feedback or "",
    )

    run_cfg = tracing.build_run_config(cfg, thread_id=req.thread_id, tenant=tenant,
                                       rag_tenant=_retrieval_tenant(ident))
    incoming = Command(resume={"action": req.action,
                               "feedback": req.feedback or None})
    return _sse(incoming, run_cfg, "review", tenant=tenant,
                thread_id=req.thread_id, budget=RequestBudget(_QUOTA.request_limit))


@v1.get("/reviews")
def list_reviews(request: Request, limit: int = 50,
                 x_api_key: str = Header(default=""),
                 x_tenant_id: str = Header(default="")):
    """GET /v1/reviews：列出待人工审核的会话（默认 50 条，按时间倒序）。

    给"审核工作台"用：没有它，待审内容只存在于 checkpoint 里，无人知晓。
    """
    ident = _authenticate(x_api_key, x_tenant_id)
    tenant = ident.tenant
    items = _REVIEW_QUEUE.pending(limit=max(1, min(int(limit), 500)), tenant=tenant)
    return {"count": len(items), "items": items}


@v1.get("/sessions/{thread_id}")
async def session_state_v1(thread_id: str, request: Request,
                           x_api_key: str = Header(default=""),
                           x_tenant_id: str = Header(default="")):
    """GET /v1/sessions/{thread_id}：取会话的**持久化状态快照**（断点续传的基础）。

    thread_id: 会话 id（路径参数）。
    x_api_key / x_tenant_id: 同 /v1/chat。

    返回（全部字段都是 JSON 原生类型）：
        status            unknown | awaiting_review | incomplete | completed
        resumable         true 时请调 POST /v1/chat/resume 续跑
        draft             最新草稿全文（断流后"已经生成到哪了"就是它）
        messages          完整对话历史，[{role, content}]，role ∈ user/assistant/system/tool
        citations         上一轮的引用（来源/页码/节选，供回溯核对）
        score / feedback  评审分数与意见
        pending_review    待人工审核时的 {draft, score, feedback, error}，否则 null

    为什么是 GET + 纯读：客户端重连后第一件事就是"我刚才进行到哪了"，
    这个动作必须幂等、无副作用，才能安全重试。
    """
    ident = _authenticate(x_api_key, x_tenant_id)
    # 只限流、**不查配额、不认领会话** —— 看状态不花 token，也不该改变归属。
    _rate_limit(request, thread_id)
    _assert_session_readable(thread_id, ident.tenant)

    cfg = {"configurable": {"thread_id": thread_id}}
    state = await graph.aget_state(cfg)
    view = _session_view(state, thread_id)
    if view["status"] == "unknown":
        raise HTTPException(status_code=404, detail="会话不存在")
    return view


@v1.delete("/sessions/{thread_id}")
async def session_delete_v1(thread_id: str, request: Request,
                            x_api_key: str = Header(default=""),
                            x_tenant_id: str = Header(default="")):
    """DELETE /v1/sessions/{thread_id}：**履行删除请求**（合规要求，此前无处可做）。

    删三类数据，缺一类都算不上"删干净"：

      1. **检查点** —— 完整对话历史、所有中间草稿、评分与引用。数据主体；
      2. **审核队列记录** —— 待审/已审的草稿正文与审核意见。它独立存表，
         只删检查点会留下一份"幽灵草稿"；
      3. **归属登记行** —— 不删会让同一个 thread_id 永久挂在旧租户名下：
         归属表只增不减，且该 id 之后谁也再用不了（新租户来会被判 403）。

    为什么必须做归属校验（而不是"有合法 Key 就能删"）
    ----------------------------------------------
    `thread_id` 是**客户端提供的**。只要能猜到别人的 id 就能删掉那条会话 ——
    这比"读"严重一个量级：读是泄露，删是**不可逆的破坏**。所以这里复用
    `_assert_session_readable` 的 fail-closed 语义：多租户下归属不符 403、
    查不到属主 404（两种情况分开，是因为处置方式不同：前者是攻击信号，
    后者是"id 不存在"）。

    为什么单租户下删不存在的会话也返回成功
    ------------------------------------
    返回 404 会把端点变成"这个 thread_id 存在吗"的探针。删除本身是幂等的，
    `deleted` 明细已经如实告诉调用方每一项到底删掉没有。

    为什么不做成"软删除"
    ------------------
    软删除（打标记）保住的是"万一删错了还能恢复"，但对合规诉求毫无帮助 ——
    数据仍在库里。真要可恢复应该靠备份，而不是让线上库长期留着已承诺删除的内容。
    """
    ident = _authenticate(x_api_key, x_tenant_id)
    _rate_limit(request, thread_id)
    _assert_session_readable(thread_id, ident.tenant)

    deleted = {
        "checkpoints": await _delete_thread_state(thread_id),
        "review": _REVIEW_QUEUE.remove(thread_id),
        "ownership": _SESSIONS.release(thread_id),
    }
    metrics.inc("session_deleted_total")
    logging.info("会话已删除：session=%s tenant=%s deleted=%s",
                 thread_id, ident.tenant, deleted)
    return {"thread_id": thread_id, "deleted": deleted}


@v1.post("/chat/resume")
async def chat_resume_v1(req: ResumeReq, request: Request,
                         x_api_key: str = Header(default=""),
                         x_tenant_id: str = Header(default="")):
    """POST /v1/chat/resume：续跑一条**中途断掉**的会话（SSE，事件与 /v1/chat 相同）。

    req: 请求体 ResumeReq，只有 thread_id —— 要跑的内容早就在检查点里了。
    x_api_key / x_tenant_id: 同 /v1/chat。

    语义（实测确认，不是推测）：检查点停在**当前未完成的那个节点之前**，
    所以续跑是**重新执行该节点**，而不是"接上上次的半个 token"。
    对客户端而言是"从头把这次回答重新流一遍"，拿到的是完整、自洽的正文。
    对账时注意：这会**再花一次**该节点的 token（但省掉了前面已完成的检索与迭代）。

    三种状态各自该走哪条路：
        next 含 human_review  → 409，提示调 POST /v1/review（需要人的决定，不是续跑）；
        next 空               → 409，本轮已收尾，没有可续的内容；
        其余（next 非空）      → 200 + SSE，用 `astream(None, ...)` 从检查点继续。

    ⚠️ 为什么不做成"自动判断后用一条接口搞定一切"：把"要人的决定"和"机器能自己跑完"
    混在一个接口里，客户端就无法区分"我需要弹审核界面"和"我可以直接继续" ——
    而这正是 human-in-the-loop 里唯一必须由人做的分支。宁可让客户端多判断一次。
    """
    ident = _authenticate(x_api_key, x_tenant_id)
    tenant = ident.tenant
    _preflight(request, session=req.thread_id, tenant=tenant)

    cfg = {"configurable": {"thread_id": req.thread_id}}
    state = await graph.aget_state(cfg)
    pending = tuple(getattr(state, "next", None) or ())
    if "human_review" in pending:
        raise HTTPException(status_code=409,
                            detail="该会话正在等待人工审核，请调用 POST /v1/review 续跑")
    if not pending:
        raise HTTPException(status_code=409,
                            detail="该会话没有未完成的生成（可能已完成，或从未开始）")

    run_cfg = tracing.build_run_config(cfg, thread_id=req.thread_id, tenant=tenant,
                                       rag_tenant=_retrieval_tenant(ident))
    # incoming=None 是 langgraph 的"从最近一次检查点继续"约定。
    return _sse(None, run_cfg, "resume", tenant=tenant, thread_id=req.thread_id,
                budget=RequestBudget(_QUOTA.request_limit))


app.include_router(v1)


# 旧路径别名：不改客户端的兼容层（功能与 /v1/* 完全一致，仅少一层前缀）
@app.post("/chat", deprecated=True)
async def chat_legacy(req: ChatReq, request: Request,
                      x_api_key: str = Header(default=""),
                      x_tenant_id: str = Header(default="")):
    """兼容旧路径的 /chat（等价于 /v1/chat）。"""
    return await chat_v1(req, request, x_api_key, x_tenant_id)


@app.post("/review", deprecated=True)
async def review_legacy(req: ReviewReq, request: Request,
                        x_api_key: str = Header(default=""),
                        x_tenant_id: str = Header(default="")):
    """兼容旧路径的 /review（等价于 /v1/review）。"""
    return await review_v1(req, request, x_api_key, x_tenant_id)


# ---------------------------------------------------------------------------
# 探针与指标
# ---------------------------------------------------------------------------
@app.get("/health")
def health_compat():
    """兼容旧路径的健康检查：**只回 `{"status":"ok"}`**（等价于存活探针）。

    刻意不再往这里加东西 —— 已有客户端把它当"进程活着"的信号，
    语义变更会让上游误判。
    """
    return {"status": "ok"}


@app.get("/health/live")
def health_live():
    """存活探针：只反映本进程，不查任何外部依赖（失败才应重启容器）。"""
    return health.liveness()


@app.get("/health/ready")
def health_ready():
    """就绪探针：真探 LLM 配置 / 数据库 / 向量后端；不健康时回 503 摘流量。"""
    ok, body = health.readiness()
    if not ok:
        raise HTTPException(status_code=503, detail=body)
    return body


@app.get("/metrics", response_class=PlainTextResponse)
def metrics_endpoint():
    """Prometheus 文本指标（文本格式，抓取器直接消费）。"""
    if not config.METRICS_ENABLED:
        raise HTTPException(status_code=404, detail="metrics disabled")

    # 抓取时刷新"待审积压"。为什么放在这里而不是入队时 +1：
    # 队列会被人处理（数量下降），也可能长期挂着不动；计数器只能表达"新增过多少"，
    # 表达不了"现在还压着多少"。而"待审内容没人处理"正是 human-in-the-loop
    # 服务最该被立刻发现的故障（等于工单丢了）。
    try:
        metrics.set_gauge("review_pending", _REVIEW_QUEUE.count_pending())
    except Exception:  # noqa: BLE001  指标刷新失败不该影响抓取本身
        logging.debug("刷新 review_pending 失败（不影响指标输出）", exc_info=True)

    return PlainTextResponse(metrics.render(),
                             media_type="text/plain; version=0.0.4; charset=utf-8")
