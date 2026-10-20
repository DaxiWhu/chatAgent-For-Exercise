"""FastAPI 服务化入口：流式对话 + 人工审核 resume + 生产化横切能力。

运行（在项目根目录执行）：
    python -m app.serve              # ⚠️ **必须**是这个入口，不要裸 uvicorn
（开发热重载：`python -m app.serve --reload`）

⚠️ 为什么不能裸 `uvicorn app.main:app`
------------------------------------
本进程在 **win32** 上需要自适应的事件循环工厂（psycopg 的异步连接对 loop 有要求），
而那个工厂是在 `app/serve.py` 里装的。裸 uvicorn 会绕开它 —— 现象是"端点一连库就
报 loop 相关的错"，而启动日志一切正常。

端点
----
    POST /v1/chat              SSE 流式对话（低分时下发 need_review）
    POST /v1/chat/resume       续跑被打断的会话（SSE；从运行现场继续）
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
import contextlib
import ipaddress
import json
import logging
import os
import random
import threading
import time
import uuid
import warnings

# 1) 日志最先配置：结构化 JSON + request_id，让"一次请求的所有日志"可串起来
from .logging_setup import get_request_id, setup_logging

setup_logging()
warnings.filterwarnings("ignore", message=".*PydanticSerializationUnexpectedValue.*")

# 2) 让 .env 自动生效（可选依赖，装在则启用）
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # noqa: BLE001
    pass


from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import NamedTuple

from fastapi import APIRouter, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, field_validator
from sse_starlette.sse import EventSourceResponse

from src import observability
from src.agents.approval import ResumeRequest
from src.prompts import PROMPT_VERSION
from src.turn import TurnRequest, build_context, run_turn

from . import auth as userauth
from . import config, health, loopwatch, metrics, middleware, tracing
from .accounts import REQ_APPROVED, STATUS_DISABLED, AccountError, build_account_store
from .auth import AuthError
from .idempotency import DONE as IDEM_DONE
from .idempotency import IN_PROGRESS as IDEM_IN_PROGRESS
from .idempotency import MISMATCH as IDEM_MISMATCH
from .idempotency import InvalidKey, build_idempotency_store
from .idempotency import fingerprint as idem_fingerprint
from .idempotency import normalize_key as idem_normalize_key
from .middleware import BodySizeLimitMiddleware, ConcurrencyLimitMiddleware, RequestContextMiddleware
from .quota import RequestBudget, build_quota_manager, estimate_tokens
from .ratelimit import build_rate_limiter
from .review_queue import build_review_queue
from .run_control import build_run_control
from .runs import build_run_store
from .sessions import SessionError, build_session_registry

# ---------------------------------------------------------------------------
# 配置（全部来自环境变量；集中读一次，模块级名字保持与旧版一致以免破坏引用）
# ---------------------------------------------------------------------------
AGNES_API_KEY = config.API_KEY
AGNES_API_BASE = config.API_BASE
AGNES_MODEL = config.MODEL_NAME
# 与 chatAgent.ipynb / .env 统一使用 DB_URL（原先此处叫 PG_CHECKPOINT_URI，导致 .env 取不到值、静默回退内存）
DB_URL = config.DB_URL
# ⚠️ 共享密钥（`SERVE_API_KEY` / `X-API-Key`）已于 2026-10-05 整体删除，
# 这里不再有对应常量。任何"再加一个服务端共享密钥"的想法都会把
# "分不出是谁 / 撤不掉 / 租户可自报"三条缺陷一起带回来（见 config.py 的说明）。

# 横切组件（进程内单例）：限流 / 配额 / 审核队列 / 会话归属
_LIMITER = build_rate_limiter()
_QUOTA = build_quota_manager()
_REVIEW_QUEUE = build_review_queue()
_SESSIONS = build_session_registry()
_RUNS = build_run_control()
#: 幂等键表：客户端重试的拦截点（见 `app/idempotency.py` 的语义说明）。
_IDEMPOTENCY = build_idempotency_store()
#: 账号体系：租户 / 用户 / 加入申请。
#: ⚠️ 这是**运行时**的用户权威 —— `.env` 里的 `AUTH_USERS` 只在库里一个用户都没有时
#: 用来建出第一个管理员（见 `AccountStore.seed_if_empty`）。管理接口读写的是它。
_ACCOUNTS = build_account_store()
#: 用户鉴权（**人类用户的唯一口径**）：JWT 验签 + 会话白名单 + 账号表校验。
#: 未配置 `AUTH_*` 时它只是一个空壳 —— `_authenticate` 不会走到它（见 `AUTH_ENABLED`）。
_AUTH = userauth.build_auth_service(accounts=_ACCOUNTS)
_TRACING_BACKEND = tracing.init_tracing()


# ---------------------------------------------------------------------------
# agent 链路装配（**唯一**的作答路径；实现见 src/agents/）
# ---------------------------------------------------------------------------
# **模型用主链路同一份配置**（同一个 base_url / api_key / model 名）。
# 刻意不引入第二套模型变量：同一个服务里两个模型配置是运维事故的常见来源
# （改了 A 没改 B，现象是"某类请求的答案风格突然变了"）。
#
# ⚠️ 装配失败 = **拒绝启动**，不降级、不兜底。
# ---------------------------------------------------
# 图退役之后本服务只剩这一条作答路径（官方 `Agent` + `Runner`）。没有第二套
# "不依赖 SDK 也能答"的实现，所以"没装 SDK / 没配 Key 也照常起"这件事本身就是
# 一个谎：进程起来了，但每个请求都必然失败，而且失败点分散在每次模型调用里
# （客户端只看到"答不出来"，运维在指标里只看到 error 率上升）。
#
# 宁可起不来 —— 起不来有明确的启动日志与退出码，部署流水线当场就知道镜像/配置
# 不对；起来了却答不了，只会在线上变成一个需要反查的现象。
#
# ⚠️ 这段必须排在构造 `ChatOpenAI` **之前**：`openai>=2.5x` 起 `api_key=""` 会在
# 构造期直接抛 `OpenAIError`，那条报错读起来跟"没配 Key"毫无关系（现象是
# "服务起不来，但日志里没有一句提到 Key"）。先判就会给出上面那段明确的原因。
_AGENT_NOT_READY = """\
agent 链路不可用（{reason}）⇒ 拒绝启动。

本服务只有这一条作答路径（官方 openai-agents 的 Agent + Runner），没有备用实现：

  · `missing_sdk`  ⇒ 镜像里没装 `openai-agents`（装：`pip install openai-agents`）；
  · `missing_api_key` ⇒ 没配 `AGNES_API_KEY`；
  · `build_failed` / 其它 ⇒ 见上面那条 warning 的具体异常。

"能起来但每个请求都答不出来"不是一个可接受的部署状态，所以这里直接退出。
"""


def _assert_agents_ready():
    """装配官方 Model 并**硬校验**就绪；未就绪抛 RuntimeError（进程起不来）。

    返回 runtime 模块，供启动日志回显用。
    """
    from src.agents import runtime as _agent_runtime

    ok = _agent_runtime.configure(
        model_name=config.MODEL_NAME,
        api_key=config.API_KEY,
        base_url=config.API_BASE,
        timeout=config.AGENT_TIMEOUT_SECONDS,
    )
    if not ok:
        raise RuntimeError(_AGENT_NOT_READY.format(reason=_agent_runtime.ready_reason()))
    return _agent_runtime


_agent_runtime = _assert_agents_ready()
logging.info(
    "agent 链路就绪：请求走 src/agents（入口 agent 自己决定叫哪个帮手，"
    "模型与主链路同配置）"
)


from langchain_openai import ChatOpenAI  # noqa: E402

# 这里不再有空 Key 的占位值：`_assert_agents_ready()` 已经保证 Key 非空，
# 走不到这一行时 `AGNES_API_KEY` 必然有值。
_MODEL = ChatOpenAI(
    model=AGNES_MODEL, api_key=AGNES_API_KEY,
    base_url=AGNES_API_BASE, timeout=30, max_retries=3,
)


# ---------------------------------------------------------------------------
# src/ 的观测出口：把链路层（检索、agent 编排）的观测值接到本进程的指标注册表
# ---------------------------------------------------------------------------
# 为什么要有这一层：`src/` 是纯库层，不能 `import app.metrics`（那会把整个服务层
# 拖进每一个 CLI / 脚本 / 测试调用点，见 `src/observability.py`）。所以链路层只调
# `observability.counter(...)`，由**这里**注入真实实现。
#
# 不注入时链路层是空操作 —— 库层单独跑、测试单独跑都不受影响，链路里也不需要写
# `try: import app.metrics` 这种判断。
class _MetricsSink:
    """`src/observability.Sink` 的实例：直接转发到本进程的指标注册表。

    刻意**不加任何标签改写**：标签口径由链路层决定（它才知道语义），
    服务层只负责落地。要改口径就改一处，不会出现"两处都在加标签"的重复计数。
    """

    def counter(self, name, amount=1, **labels):
        metrics.inc(name, amount=amount, **labels)

    def histogram(self, name, value, **labels):
        metrics.observe(name, value, **labels)

    def gauge(self, name, value, **labels):
        metrics.set_gauge(name, value, **labels)


observability.set_sink(_MetricsSink())


# ---------------------------------------------------------------------------
# 优雅关闭：在途请求由 **uvicorn** 放完，不是 lifespan
# ---------------------------------------------------------------------------
# 本模块已不装配状态持久化（图退役了；"被打断的运行现场"见 `app/runs.py`）。
# 这段只澄清一个容易记错功劳的地方 —— 它决定 lifespan 收尾时究竟还有什么要善后：
# "不掐断在途请求"**不是 lifespan 做的**（2026-09-29 实测）。
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
    """应用生命周期：启动时做各项自检，关闭时释放资源。

    这里**不再装配状态持久化**：编排原语只剩 `Agent` + `Runner`（官方没有任何
    图/节点/检查点概念），"被打断的运行现场"由 `app/runs.py` 存官方 ``RunState``。
    所以启动期只剩一堆自检与回显 —— 它们回答的都是"这次部署到底接上了什么"，
    接不上时一律**只记日志、不阻止启动**（"能起来"优先），但结论必须留在日志里。
    """
    global run_store
    stack = AsyncExitStack()
    # 向量索引身份自检放在最前：它不依赖 DB，且无论 DB 通不通都要在启动日志里
    # 留下一条明确结论（"没检出问题"与"根本没检查"必须能区分开）。
    _report_index_identity()
    # 跨轮历史同理，只是它要**异步连一次**才能下结论（懒连接意味着构造期不报错）。
    await _report_agent_history_backend()
    # 账号表：确保三张表存在，并在**库为空**时用 `AUTH_USERS` 建出第一个管理员。
    # 为什么放在启动期而不是等第一个管理接口调用：种子必须**早于**任何登录尝试生效，
    # 否则"服务起来了但没人能登录"这段时间里唯一的补救手段是重启。
    # 连不上库时只记 warning 不阻止启动（与运行现场的取舍一致：能起来优先），
    # 但结论必须留在日志里 —— 它决定"这次部署到底有没有人能登录"。
    if _ACCOUNTS.dsn:
        try:
            created = await asyncio.to_thread(_ACCOUNTS.seed_if_empty)
            if created:
                logging.info("账号表已初始化：建出 %d 个初始账号（来自 AUTH_USERS 种子）",
                             created)
        except Exception as e:  # noqa: BLE001
            logging.warning(
                "账号表初始化失败（AUTH_USERS 种子未能写入 ⇒ 若库为空则无人能登录）。"
                "请确认已执行 `python -m app.migrations --apply`。原因：%s", e)
    elif config.AUTH_ENABLED:
        # 内存后端：只在本地开发/测试有意义，种子直接放进内存库。
        created = _ACCOUNTS.seed_if_empty()
        if created:
            logging.warning(
                "账号数据落在**进程内存**（无 DB_URL）：已放入 %d 个种子账号。"
                "重启即丢，多副本下各进程互不可见 —— 仅限本地开发。", created)
    # 被打断的运行现场（官方 ``RunState``）存在哪：配了 DB_URL 就进 PG，
    # 否则进进程内存（单实例、重启丢）。这份数据**只有**"运行中产生了
    # interruption"的轮次才有 —— 正常跑完的轮次没有现场，也不需要。
    dsn = os.getenv("DB_URL", DB_URL)
    run_store = build_run_store()
    if not dsn:
        logging.info("未设置 DB_URL：运行现场（RunState）落在进程内存，重启即丢")
    else:
        logging.info("运行现场（RunState）落 PostgreSQL：被打断的运行可跨重启恢复")
    # 事件循环阻塞探针。**压进 stack**：关闭时由 `stack.aclose()` 一并取消，
    # 不在关闭路径上再写一遍取消逻辑（漏写的代价是每次滚动发布留一个悬挂任务）。
    # 放在这里而不是更早，是因为上面那个 except 分支会 `aclose()` 并**重建** stack ——
    # 压早了会被关掉，而重建后的那个 stack 里没有它（探针静默消失，指标永远为零）。
    # ⚠️ `enter_async_context` 是 **async 方法**，必须 `await` —— 漏掉 `await` 只是
    # RuntimeWarning（"coroutine was never awaited"），服务照样启动、探针**却从未运行**，
    # 指标永远为空而没有任何别的症状。守卫见
    # `tests/test_event_loop_lag.py::test_probe_registration_is_awaited`（AST 判据）。
    await stack.enter_async_context(loopwatch.watching())
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
        # 跨轮历史的引擎也要显式释放：uvicorn 的关闭**不碰**第三方连接池，PG 侧会一直
        # 挂着这些连接直到它自己超时 —— 滚动发布时几个副本叠起来就是"连接数莫名逼近上限"。
        try:
            from src.agents import session as agent_session

            await agent_session.dispose_session_backend()
        except Exception as e:  # noqa: BLE001  关闭路径上的失败不该改变退出码
            logging.warning("跨轮历史引擎释放失败（不影响关闭）：%s", e)
        # 本进程自己的同步 PG 池同理：不关就留下一批 PG 后端进程，
        # 直到服务端 idle 超时。滚动发布时新旧副本的连接会**叠加**。
        try:
            from . import pg

            pg.close_all()
        except Exception as e:  # noqa: BLE001
            logging.warning("PG 连接池释放失败（不影响关闭）：%s", e)
        # 审核通知队列：尽力把已排队的通知发完再退（有上限，不拖住发布）。
        # 不排空的话，发布前那批真实用户触发的待审通知会被静默丢掉 ——
        # 审核台看不到任何提示，而工单其实还在库里，只是没人被提醒。
        try:
            await _off(_REVIEW_QUEUE.close, 5.0)
        except Exception as e:  # noqa: BLE001
            logging.warning("审核通知队列排空失败（不影响关闭）：%s", e)
        logging.info("服务关闭：跨轮历史与 PG 连接池已释放")


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
# allow_credentials 恒为 False：鉴权走 Authorization 请求头而不是 Cookie，
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


# ---------------------------------------------------------------------------
# 未处理异常 → 脱敏 500（**并显式带上 X-Request-ID**）
# ---------------------------------------------------------------------------
# 为什么需要它（2026-10-05 补）：此前全仓没有任何异常处理器，未处理异常由
# Starlette 最外层的 `ServerErrorMiddleware` 生成一个裸 500。它走的是**最初的
# `send`**，不经过 `RequestContextMiddleware` 的 `send_wrapper` —— 也就是那句
# `MutableHeaders(scope=message)["X-Request-ID"] = rid` 根本不会执行。
#
# 结果是个很别扭的组合：401/403/409/429（走 ExceptionMiddleware 的 HTTPException
# 分支，在用户中间件**之内**）都带 rid，唯独**最需要 rid 的那个 500 不带**。
# 用户报"500 了"，响应体与响应头里都没有任何可检索的标识，运维无法按 rid 定位。
#
# ⚠️ 为什么在处理器里**自己写这个头**，而不是指望中间件补：
# 实测读源码确认（fastapi 0.135.1 的 `build_middleware_stack`）——注册给
# `Exception` / `500` 的处理器会被交给 **`ServerErrorMiddleware`**（最外层），
# 而不是 `ExceptionMiddleware`（用户中间件之内）。所以它的响应**不会**再穿过
# `send_wrapper`，头部必须在这里显式带上。凭直觉以为"注册了处理器就会自动带头"
# 是错的，而且错了不会有任何症状 —— 除了最需要 rid 时恰好没有。
#
# 副作用需要知道：异常仍然会**穿过**用户中间件（ServerErrorMiddleware 在它们之外），
# 所以 `RequestContextMiddleware` 的 `logging.exception` 与 `http_errors_total`
# 照旧生效 —— 这里不重复记日志、不重复计数，只负责"给客户端一个能解释的 500"。
@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """未处理异常：生产只回通用文案 + rid，开发保留类型与消息（见 `_client_error`）。

    rid 从 `request.scope["state"]` 取，**不能**用 `get_request_id()`：本处理器
    跑在最外层的 `ServerErrorMiddleware`，而请求 id 是由内层中间件在一个**复制过的
    contextvars 上下文**里设置的，外层读不到。中间件因此额外把 rid 写进了
    `scope`（同一个 dict 对象，传引用）—— 那边有详细的来龙去脉。
    """
    rid = (request.scope.get("state") or {}).get("request_id") or get_request_id()
    headers = {"X-Request-ID": rid} if rid else None
    return JSONResponse({"detail": _client_error(exc, rid=rid)},
                        status_code=500, headers=headers)


# 运行期才确定的"共享后端到底有没有生效"：构造完成后回显进启动日志。
# 只回显 REDIS_URL 是否存在会谎报（见 config.as_dict 的说明）。
logging.info("服务启动配置：%s", config.as_dict(runtime={
    "rate_limit_backend": _LIMITER.backend_name,
    "quota_backend": _QUOTA.backend_name,
    # 会话白名单落在 redis 还是进程内存：会话校验是**无条件**的，所以内存后端
    # 只在单进程成立；多副本下会表现为"刚登录成功、隔几次请求又要重新登录"的随机 401
    "auth_session_backend": _AUTH.store.backend_name,
    # 幂等键的**实际**后端。不传这一项时 `config.as_dict` 只能按 `bool(REDIS_URL)` 猜，
    # 而 `IdempotencyStore` 在共享后端不可用时会**静默退回进程内存**（只打一条 warning）
    # ⇒ 启动日志会谎报 redis，运维据此以为"多副本下的重复执行保护已生效"。
    "idempotency_backend": _IDEMPOTENCY.backend,
}))
logging.info("tracing 后端：%s", _TRACING_BACKEND)
# 提示词版本单独回显（不进 as_dict —— 它是**代码事实**而非可配置项，
# 混进"启动配置"会让人误以为可以设置）。线上答得不对时，
# 第一件要核对的事就是"跑的是哪一版提示词"。
logging.info("提示词版本：%s", PROMPT_VERSION)

# 被打断运行的现场仓库：内存起步，lifespan 启动时按 DB_URL 换成 PG 版。
# 刻意不在导入期连库：那会在 import 时就开一个永不释放的连接，且测试导入本模块
# 会平白打一次真实 PG。
run_store = build_run_store()



# ---------------------------------------------------------------------------
# 鉴权与身份：**唯一的身份来源是已验签的 JWT，租户写在令牌载荷里**
# ---------------------------------------------------------------------------
# 演进：
#   ① 起初是"租户绑定到 API Key"（TENANT_KEYS 反查）＋"全局 Key + 请求头自报"；
#   ② 2026-10-04 收敛成"人类用户一律 JWT，租户写在载荷里"，但**保留了**
#      `SERVE_API_KEY` 作为"脚本过渡通行证"；
#   ③ 2026-10-05（本次）**把那条过渡路径整体删除** —— 代码里不再存在任何
#      "凭一个共享密钥就能进门"的判定。
#
# 为什么连"过渡用"都不留 —— 共享密钥的三个结构性缺陷（不是"不够安全"这种
# 模糊说法，而是在这个系统里必然出问题的三件事）：
#   - **分不出是谁**：所有调用方共用同一个值，出问题无法归因到具体的人，
#     而本服务要留审核痕迹（"谁批的"）；
#   - **撤不掉**：换值＝改配置＋重启，而重启会踢掉所有在线用户；
#   - **带不了租户**：它没有载荷，租户只能靠请求头自报并被采信 ⇒ 持它者可
#     自称任意租户：配额分桶可绕过、审核归属不可信、检索分区可随意切换。
#   只要它还在代码里，就总有人会去配它（因为"配一个密钥"比"建账号"省事），
#   于是上面三条会被重新引入。所以**删代码**，而不是"留空"。
#
# 现在只有一条路径：
#   `Authorization: Bearer <本服务会话 token>` ⇒ 通过；没带 ⇒ 401
#   （**没有任何"匿名放行"、"凭共享密钥放行"或"接受外部签发 token"的兜底**）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Identity:
    """一次请求的身份。

    tenant: 生效租户 —— 决定配额分桶、审核归属、会话属主、检索分区。
        **空串是合法值**，含义是"这个主体还没有租户"。⚠️ 它在**检索层**不再
        意味着"不加过滤"：无租户会落到配置的公共租户，未配置则检不到任何内容
        （判据见 `_retrieval_tenant` 与 `config.NO_TENANT_SENTINEL`）。
    user: 用户主体。审核留痕与配额归因都靠它。
        租户与角色都来自**已验签的会话令牌载荷** ⇒ 可信，不是客户端自报。
    roles: 用户角色。用于资源级授权（审稿要 reviewer、管理要 admin）。
    session_id: 会话 id。供登出与排障使用。

    ⚠️ 这里**刻意没有** via（认证方式）字段：它曾经是"可信度标签"，用来区分
    会话令牌 / 外部身份源令牌 / 共享密钥 / 匿名。后三档已陆续删除，只剩会话
    令牌一种可能 ⇒ 一个恒为定值的字段没有信息量，反而会让读代码的人以为
    还存在别的认证方式（2026-10-05 拍板删除）。
    """

    tenant: str
    user: str = ""
    roles: tuple = ()
    session_id: str = ""


def _bearer_token(authorization: str) -> str:
    """从 `Authorization` 头取出 Bearer token；非 Bearer 方案返回空串。

    为什么只认 `Bearer` 这一种方案，而不是"有值就当 token 用"：将来若支持
    其他方案（如 `Basic`），把它的值当 JWT 解析只会得到一堆"token 格式非法"
    的噪声告警，把真正的问题淹掉。
    """
    raw = (authorization or "").strip()
    if not raw:
        return ""
    scheme, _, token = raw.partition(" ")
    if scheme.strip().lower() != "bearer":
        return ""
    return token.strip()


def _authenticate_user(bearer: str) -> Identity:
    """用户凭证认证：本服务签发的会话 token（HS256）。

    `tenant` 与 `user` 都来自**已验签的凭证**，不是客户端自报 —— 这是本服务
    关于"谁是调用方、属于哪个租户"的**唯一**可信来源。
    """
    try:
        principal = _AUTH.verify_session_token(bearer)
    except AuthError as e:
        # code 一并记日志：401 的原因可能完全不同（token 过期 / 会话被登出 /
        # 密码轮换清场），处置手段也不同。只记"401"等于没记。
        logging.warning("用户凭证认证失败：code=%s detail=%s", e.code, e.detail)
        raise HTTPException(status_code=e.status_code,
                            detail=f"{e.detail}（code={e.code}）") from e

    return Identity(tenant=principal.tenant, user=principal.user,
                    roles=principal.roles, session_id=principal.session_id)


def _authenticate(x_tenant_id: str, authorization: str = "") -> Identity:
    """认证请求并确定身份；失败抛 401/403。

    authorization: 请求头 Authorization —— **唯一的凭证入口**，`Bearer <token>`。
    x_tenant_id: 请求头 X-Tenant-Id。⚠️ 它**不是**租户来源，只做**一致性核对**：
        带令牌时若与令牌里的租户不符则 403；不带令牌时它一点作用都没有
        （没有身份，就没有"要以谁的租户为准"这件事）。取值一律来自已验签的载荷。

    只有两条路径：

      `Authorization: Bearer <token>` 有值 → 用户身份。租户与角色都来自
      **已验签的载荷**；没有租户就是空串（含义见 `_retrieval_tenant`）。
      没带凭证 → **401**。

    ⚠️ 这里**没有兜底档**。此前存在过的几档都已被显式删除：
      · `SERVE_API_KEY` 脚本通行证 —— 用一把共享密钥就能进门，且租户由请求头自报；
      · `REQUIRE_AUTH=0` 时的匿名放行 —— 完全不带凭证也能调所有业务接口；
      · 外部身份源令牌（OIDC）—— 2026-10-05 删除，见 `app/auth.py` 文件头。
      删掉之后，**任何**业务请求都必须携带一个本服务签发的会话令牌。
      这不是"默认更严"这种程度上的变化，而是"**服务只认一种身份来源**"的口径：
      没有"服务端信任的第二种凭证"，也就没有"配错一个变量就变成公开服务"的可能。

    ⚠️ 为什么不再看 `REQUIRE_AUTH`：它存在的唯一意义就是"关掉鉴权要求"，
      而现在鉴权要求是**结构性**的（没配 AUTH_* 的服务在启动期就被拦住），
      没有任何环境变量能把它关掉 —— 该配置项已随之删除。
    """
    claimed = (x_tenant_id or "").strip()

    bearer = _bearer_token(authorization)
    if not bearer:
        # 缺凭证 —— 401（不是 503）。
        # 为什么不是 503：这是**客户端**没登录，不是服务端故障。回 503 会被计入服务端
        # 错误、污染 SLO 错误预算，让"用户忘了登录"在监控上看起来像"服务挂了"。
        # 守卫：`tests/test_identity.py::test_unauthenticated_request_gets_401_...`，
        # 它同时是 CI e2e 的前提（`tests/test_e2e_stack.py` 断言裸请求必须 401）。
        raise HTTPException(
            status_code=401,
            detail="缺少凭证：请先 POST /v1/auth/login 换取令牌，再带 Authorization: Bearer 调用")
    if not config.AUTH_ENABLED:
        # 带了 Bearer 但本服务没配任何用户来源 ⇒ 没人能签发这个 token，验也验不了。
        # 明确拒绝，不要"忽略它、当没看见"—— 后者会让客户端以为认证生效了。
        # （这个组合在生产环境到不了这里：启动期 `_assert_auth_policy` 已拦。）
        raise HTTPException(
            status_code=401,
            detail="服务未启用用户鉴权（未配置 AUTH_*），不接受 Bearer 凭证")

    ident = _authenticate_user(bearer)
    # ⚠️ "确认"必须发生在这里（唯一入口），不能只放在 `_authenticate_user` 里：
    # 核对需要同时看到"令牌里的租户"（ident.tenant）与"请求头自报的值"（claimed），
    # 而只有本函数两个都有。放在唯一入口上还顺带保证了"将来多一个调用点也绕不过去"。
    if claimed and claimed != ident.tenant:
        metrics.inc("tenant_mismatch_total")
        logging.warning("租户自报与用户凭证不符，拒绝：token_tenant=%s claimed=%s",
                        ident.tenant, claimed)
        raise HTTPException(status_code=403, detail="X-Tenant-Id 与用户凭证不匹配")
    return ident


def _assert_review_role(ident: Identity) -> None:
    """资源级授权：谁能审核。

    ⚠️ 2026-10-05：此前还有一条"非 JWT 身份直接放行"的**豁免**（为了让持共享密钥的
    旧调用方不被 403）。那条路径本身已被删除，豁免也就随之消失 —— 现在能到达这里的
    身份**只可能是**已验签的会话令牌，它一定带角色，一律按角色判定。

    ⚠️ `AUTH_REVIEWER_ROLES` 是 **fail-closed** 的：默认 `reviewer`。
    也就是说"不做角色校验"必须**显式写空** `AUTH_REVIEWER_ROLES=` 才生效；
    不配 = 只有带 `reviewer` 角色的人能审。理由见 config 里那一行。
    """
    if not config.AUTH_REVIEWER_ROLES:
        return
    if set(ident.roles) & set(config.AUTH_REVIEWER_ROLES):
        return
    # 单独一个指标名：403 的原因很多（租户不符、会话越权、缺角色），
    # 混在总数里就分不出"是不是有人在用普通账号试探审核接口"。
    metrics.inc("authz_denied_total", resource="review")
    logging.warning("审核被拒：user=%s 缺少角色（需要 %s，实有 %s）",
                    ident.user, "/".join(config.AUTH_REVIEWER_ROLES),
                    "/".join(ident.roles) or "无")
    raise HTTPException(
        status_code=403,
        detail="当前身份没有审核权限（需要角色：" + "/".join(config.AUTH_REVIEWER_ROLES) + "）")


def _assert_admin_role(ident: Identity) -> None:
    """资源级授权：谁能管用户与租户（增删改查、审批加入申请）。

    ⚠️ 与 `_assert_review_role` 是**同一种形状**（都只看角色、都 fail-closed）。
      历史上两者有一处刻意不同：审稿对"非 JWT 身份"直接放行（为了不打断持共享密钥
      的旧调用方），管理则一律 403。共享密钥档删除后那种身份**不再存在**，差异连同
      豁免一起消失。现在两种授权都只做一件事：查角色。

    ⚠️ **留空 = 没人能管**（fail-closed）。理由见 `config.AUTH_ADMIN_ROLES`：
      管理接口是"权限的源头"，默认开放等于上线即敞开。

    ⚠️ 管理身份本身也要经得起审计：这里不记"操作了什么"（那是各端点的事），
    但必须记**是谁被拒了** —— 有人拿普通账号反复试探管理接口，是值得看见的信号。
    """
    if not config.AUTH_ADMIN_ROLES:
        metrics.inc("authz_denied_total", resource="admin")
        raise HTTPException(
            status_code=403,
            detail="未配置 AUTH_ADMIN_ROLES ⇒ 管理接口当前对所有人关闭")
    if set(ident.roles) & set(config.AUTH_ADMIN_ROLES):
        return
    metrics.inc("authz_denied_total", resource="admin")
    logging.warning("管理操作被拒：user=%s 缺少角色（需要 %s，实有 %s）",
                    ident.user, "/".join(config.AUTH_ADMIN_ROLES),
                    "/".join(ident.roles) or "无")
    raise HTTPException(
        status_code=403,
        detail="当前身份没有管理权限（需要角色：" + "/".join(config.AUTH_ADMIN_ROLES) + "）")


def _drops_admin(roles) -> bool:
    """这次 PATCH 传进来的角色集合，是不是**不再包含**任何一个管理员角色。

    roles: `None` = 这次不改角色（⇒ False）。空 tuple/列表 = 把角色清空（⇒ True）。

    为什么单独拎出来：判据是"改完之后还是不是管理员"，而不是"roles 变没变" ——
    传 `("reviewer",)` 也是变化，但那不构成自伤。判错了会把正常操作拦下来。
    未配置 `AUTH_ADMIN_ROLES` 时返回 False：那时谁都不是管理员，`_assert_admin_role`
    已经把所有管理请求挡在 403 上，这里没有"管理员角色"可谈。
    """
    if roles is None or not config.AUTH_ADMIN_ROLES:
        return False
    return not (set(roles) & set(config.AUTH_ADMIN_ROLES))


#: 已失效、但"看起来还能改变鉴权行为"的配置项 ⇒ 留着它们就拒绝启动。
#: 为什么这一组要用"拒绝启动"而不是像其它已废弃变量那样静默忽略
#: --------------------------------------------------------------
#: 因为**它们曾经真的能改变鉴权结果**。运维看到变量还在、服务又照常起来，会非常
#: 自然地得出"这扇门还开着 / 这个开关还管用"的结论 —— 而代码已经不读它了。
#: 这类"以为有、其实没有"的安全配置是所有误判里最危险的一种：它不会以任何方式
#: 表现出来，直到有人从那个不存在的门走进来。
#:
#: 报出来 + 拒绝启动，逼配置与代码对齐：要么删掉这一行，要么明确接受它已不存在。
#: 这与"`.env` 里的 `TENANT_KEYS` / `AUTH_SINGLE_SESSION` 静默忽略"不同 ——
#: 那几个被忽略之后**不会让人误判任何事的成败**，而这一组会。
_DEAD_AUTH_KNOBS = {
    "SERVE_API_KEY": (
        "共享密钥路径已整体删除（它分不出身份、撤不掉、租户可由请求头自报）。"
        "脚本与 CI 的凭证请改走「账号密码 → POST /v1/auth/login → Bearer 令牌」"
        "（见 scripts/script_auth.py）。"
    ),
    "REQUIRE_AUTH": (
        "匿名兜底已删除：未带凭证的请求**一律 401**，不再有任何开关能把它变成放行。"
        "生产环境必须有用户鉴权这条检查看的是 APP_ENV，与任何环境变量无关。"
    ),
}

#: 已失效配置项的**前缀族** —— 整族随功能一起删除。
#: 为什么用前缀而不是逐条列举：OIDC 那一族有 10 个键（地址 / 受众 / 算法 / 缓存 TTL /
#: 超时 / 时钟偏移 / 三个 claim 名），而"一共几个"只是当时的实现细节。逐条列举的话，
#: 漏掉一个就等于给"看起来还能配"留了一个口子 —— 而这类漏项恰恰最难被发现（没人会去数）。
_DEAD_AUTH_PREFIXES = {
    "AUTH_OIDC_": (
        "外部身份源（OIDC / JWKS）已整体删除：本服务只接受**自己签发**的会话令牌，"
        "不再验签任何外部 IdP 的 token。这一族配置项请整行删除。"
    ),
}


def _stale_auth_knobs() -> dict:
    """环境里仍存在（**键存在**即可，留空也算）的已失效鉴权配置项 → 原因。"""
    stale = {}
    for name in os.environ:
        if name in _DEAD_AUTH_KNOBS:
            stale[name] = _DEAD_AUTH_KNOBS[name]
            continue
        for prefix, why in _DEAD_AUTH_PREFIXES.items():
            if name.startswith(prefix):
                stale[name] = why
                break
    return stale


def _assert_no_legacy_auth_knobs() -> None:
    """启动期守卫：环境里还留着已失效的鉴权开关 ⇒ **拒绝启动**。

    ⚠️ 读的是裸 `os.environ` 而不是 `config`：本模块**刻意不再**把这些变量收进配置
    —— 收进去就等于给"它们还是一等配置项"这件事留了余地。

    ⚠️ 判据是"**键存在**"而不是"值非空"：`SERVE_API_KEY=`（留空）同样拒绝启动。
    留空看起来最无害，实际最容易把人带偏 —— 运维看到这一行会认定"密钥这一档
    还配着、只是没填"，于是去填一个值；而代码根本不读它。要的是整行消失。
    """
    stale = _stale_auth_knobs()
    if not stale:
        return
    detail = "\n".join(f"  · {n} —— {stale[n]}" for n in sorted(stale))
    raise RuntimeError(
        "检测到已失效的鉴权配置项仍然存在，但对应的代码路径已整体删除。\n"
        f"{detail}\n"
        "服务不会读它们，也不会因此获得任何鉴权能力 —— 请从 `.env` / compose / "
        "CI 变量里删掉这些项（留空也不行，必须整行删除）。"
    )


def _assert_auth_policy() -> None:
    """启动期鉴权策略自检。

    这是**唯一**能在流量进来之前发现配置缺失的地方，所以宁可在这里 raise
    让进程起不来，也不要带着"谁也登不进来"的状态接管线上流量。

    ⚠️ 2026-10-05：共享密钥档与匿名兜底都已删除，所以它变得很短 —— 只剩两条分支：
      · 生产环境没配用户鉴权 ⇒ **拒绝启动**（连一个能登录的人都没有）；
      · 非生产环境没配 ⇒ 起得来，但**每个业务请求都会 401**，用 ERROR 说明白。
    没有第三条路 —— 不再存在"配了共享密钥也能进门"这种折中状态。
    """
    has_user_auth = config.AUTH_ENABLED

    if not has_user_auth:
        if config.IS_PRODUCTION:
            # 生产环境的硬要求：必须有人能登录。现象是"服务活着、日志祥和、
            # 但没有任何人能用" —— 这比起不来更糟（起不来至少会有人去查）。
            raise RuntimeError(
                "APP_ENV=production 但未启用用户鉴权（AUTH_*）：**没有任何人能登录**，"
                "所有业务请求都会 401，服务等同于不可用。请配置 AUTH_SESSION_SECRET "
                "＋ 至少一个账号（AUTH_USERS 作为初始管理员种子）。"
                "⚠️ 没有任何环境变量能关掉本检查。"
            )
        # 非生产（本地开发 / 测试）：允许启动（否则连导入 app 的工具脚本都跑不了），
        # 但必须**大声**说明"这个进程谁也服务不了" —— 它现在没有匿名兜底了。
        logging.error(
            "未配置任何鉴权（AUTH_* 全空）⇒ **所有业务请求都会返回 401**，服务等同不可用。"
            "这是刻意的 fail-closed：本服务已不再有'匿名放行'的本地兜底。"
            "本地要用，请配置 AUTH_SESSION_SECRET（任意非空值即可）＋ AUTH_USERS 一条种子；"
            "生成哈希：python -m scripts.hash_password -u <账号> -t <租户>。"
        )

    # ---- 用户鉴权（唯一的鉴权口径）自检 ----
    if config.AUTH_ENABLED and not config.AUTH_SESSION_SECRET:
        logging.warning(
            "已启用用户鉴权但未配置 AUTH_SESSION_SECRET ⇒ 无法签发/续期本服务会话，"
            "**没有任何人能登录**（服务等同于不可用）。生成方式：openssl rand -hex 32"
        )
    if config.AUTH_SESSION_SECRET and not config.AUTH_ENABLED:
        logging.warning(
            "配置了 AUTH_SESSION_SECRET 但没有用户来源（AUTH_USERS 种子）"
            "⇒ 任何人都无法登录，会话密钥形同虚设。"
        )
    if config.AUTH_ENABLED and _ACCOUNTS.backend == "memory":
        # ⚠️ 账号数据落进程内存比"会话白名单落内存"更隐蔽：会话是短期状态，
        # 而账号是**长期事实** —— 每个副本各持一份互不可见的账号表，表现为
        # "刚在 A 副本建的用户，在 B 副本说密码错"，用户只会觉得"服务在抽风"。
        logging.warning(
            "用户鉴权已启用，但账号数据落在**进程内存**（未配置 DB_URL）⇒ "
            "重启即丢、多副本下各进程各持一份互不可见的账号表。"
            "请配置 DB_URL 并执行 `python -m app.migrations --apply`。"
        )
    if (config.AUTH_REVIEWER_ROLES or config.AUTH_ADMIN_ROLES) and not has_user_auth:
        # 上面那条 ERROR 已经说明"所有请求都会 401"，这里补的是"角色配置为什么也
        # 白配了"—— 角色只存在于已验签的身份里，没有身份就没有角色可查。
        logging.warning(
            "已配置审核/管理角色（reviewer=%s / admin=%s）但未启用用户鉴权 ⇒ "
            "这两项检查都不会被执行（没有 JWT 身份可查角色）。",
            ",".join(config.AUTH_REVIEWER_ROLES) or "-",
            ",".join(config.AUTH_ADMIN_ROLES) or "-",
        )
    if not config.ENABLE_DOCS:
        logging.info("API 文档已关闭（/docs、/redoc、/openapi.json 返回 404）")


_assert_no_legacy_auth_knobs()
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


def _assert_session_backend_policy() -> None:
    """启动期自检：会话白名单是否落在**共享**后端上。

    为什么这条比限流/配额那条更严重
    ------------------------------
    限流/配额降级到内存的后果是"额度按副本数放大"—— 数字不准，但请求**能用**。
    会话白名单降级到内存的后果是**功能直接错**：校验是**无条件**查存储的，
    副本 A 签发的会话在副本 B 查不到 ⇒ 用户按请求落在哪个副本**随机收到 401**，
    表现为"刚登录成功、隔几次请求又要重新登录"。用户侧看起来像服务在抽风，
    而日志里只有一堆 401，没有任何一条说明原因。

    ⚠️ 检测能力的边界：`APP_WORKERS > 1` 能确定有问题；但"**多个容器各跑 1 个
    worker**"检测不到（每个进程都以为自己独占）。所以末段提示是无条件的 ——
    它覆盖的正是这种自动检测不到、却相当常见的部署形态。
    """
    if not config.AUTH_SESSION_SECRET:
        # 没有会话密钥 ⇒ 不签发本服务会话 ⇒ 白名单根本不会被查。
        # 此时内存后端完全够用，不必打扰运维。
        return

    if _AUTH.store.backend_name == "redis":
        return

    if config.REDIS_URL:
        # 配了 Redis 却没有用上：AuthService 构造期已报过一次 ERROR，
        # 这里补一条结论行，让"这次部署到底共享了没有"有唯一答案。
        logging.error(
            "REDIS_URL 已配置，但会话白名单**落在进程内存** ⇒ 多副本下用户会随机 "
            "收到 401。请确认已 `pip install redis` 且 REDIS_URL 可达"
            "（启动日志里的 auth.user_auth.session_backend 应显示 redis）。"
        )
        return

    workers = 1
    try:
        # 延迟导入：serve 是启动入口，模块级导入它会与"由 serve 启动 main"形成
        # 不必要的耦合。worker 数的解析口径统一在 serve.resolve_workers，
        # 这里刻意复用而不是复制一份 —— 复制就会漂移。
        from .serve import resolve_workers

        workers = resolve_workers()
    except Exception:  # noqa: BLE001
        pass

    if workers > 1:
        logging.error(
            "APP_WORKERS=%s（多 worker）但未配置 REDIS_URL ⇒ 会话白名单**分散在各 "
            "worker 进程内**，同一用户的请求落到不同 worker 会随机 401。"
            "多进程部署必须配置 REDIS_URL 让会话跨进程共享。", workers
        )
    else:
        logging.warning(
            "未配置 REDIS_URL ⇒ 会话白名单保存在**进程内存**（单进程部署可正常工作）。"
            "⚠️ 一旦改成多 worker 或多容器副本，同一用户的会话在不同进程间不共享，"
            "会表现为**随机 401**（登录成功、隔几次请求就要重登）。届时必须先配 REDIS_URL。"
        )


_assert_session_backend_policy()


def _assert_agent_history_backend_policy() -> None:
    """启动期自检：**跨轮对话历史**有没有落在一个能用的后端上。

    为什么必须提醒：历史丢了的后果不是"报错"，而是**同一段对话随机失忆** ——
    用户上一轮让写的小说，这一轮说"把结尾改得更压抑"，接话的前台看不到上文，
    只能反问"改什么的结尾？"。而所有指标都正常（那一轮确实正常跑完了），
    日志里一行错误都没有。用户侧看起来像模型能力不稳。

    2026-10-03 起只有一个后端（PG，与运行现场同库）：配了 `DB_URL` 就天然多副本共享，
    不再有"配了 Redis 但某个副本没配"那类部署形态问题。所以这里只需要拦一种情况 ——
    **根本没配 `DB_URL`**，那时连运行现场也是内存的，跨轮历史整个不存在。
    """
    if config.DB_URL:
        # 配了也可能连不上（PG 没起 / 库不存在），但那个要异步连一次才知道 ——
        # 结论由 lifespan 的 `_report_agent_history_backend()` 给，不在这里猜。
        return
    logging.warning(
        "未配置 DB_URL ⇒ 跨轮对话历史**不可用**（没有后端）：模型每一轮只看得到用户"
        "当轮说的那一句，上一轮说过什么一概不知道（用户会看到「接着改上一轮的东西」时"
        "模型不知道在指什么，而所有指标都正常）。同时运行现场也是内存的，重启即丢。"
        "要接上历史，请配 DB_URL 并执行 `python -m app.migrations apply` 建表。"
    )


_assert_agent_history_backend_policy()


async def _report_agent_history_backend() -> None:
    """启动自检：跨轮历史的后端**真的连一次**（异步，放在 lifespan 里）。

    为什么必须主动探，而不是等第一个请求：`create_async_engine` 是**懒连接** ——
    地址写错、PG 没起、库不存在都不会在构造期报错，要到第一次取历史才炸。而那一步
    在 `Runner.run` **内部**，会被 `run_agent` 的宽 `except` 接住 ⇒ `exit_reason="error"`。
    于是"DB_URL 配错"表现成"agent 链路动不动答不出来"，排查起点完全错。

    （失败详情由 `src.agents.session` 打一条 ERROR；这里只负责给出**结论行**，
    让"这次部署的历史到底接上了没有"有一个可以直接 grep 的答案。）
    """
    try:
        from src.agents import session as agent_session
    except Exception as e:  # noqa: BLE001  自检失败不该拦住启动
        logging.warning("跨轮历史自检未能执行（不影响启动）：%s", e)
        return

    intent = agent_session.session_backend_intent()
    # 连通性探测是 IO、分词器预热是 CPU（跑在工作线程里），两者**并行** ——
    # 串行的话启动会白等一次词表加载（实测约 250ms）。见 `_warmup_history_tokenizer`。
    ok, _ = await asyncio.gather(
        agent_session.ping_session_backend(),
        _warmup_history_tokenizer(),
    )
    if ok:
        logging.info(
            "跨轮对话历史就绪：后端=%s 输入预算=%s token 存储=不过期不清理",
            intent,
            config.AGENT_HISTORY_TOKEN_LIMIT or "不限",
        )
    else:
        logging.error(
            "跨轮对话历史**不可用**（打算用的后端=%s，详见上面那条 ERROR）⇒ "
            "每轮只剩用户新说的这一句，模型看不到上文。服务照常运行，"
            "用户侧表现为「接着改上一轮的东西」时不知道在指什么。", intent,
        )


async def _warmup_history_tokenizer() -> None:
    """预热跨轮历史预算用的分词器（词表冷加载约 250ms，每个进程只做一次）。

    为什么要主动做：`tiktoken` 的词表是**首次编码时才加载**的。不预热，这 250ms 就
    落在"第一个真正需要裁剪的请求"里 —— 而那时它跑在工作线程中，表现为
    "服务刚起来时第一条请求莫名多等两百多毫秒"，之后再也复现不出来。

    预热失败不拦启动：`count_tokens` 会自行退化成字符数近似，并有 warning 说明。
    """
    try:
        from src.agents import history_budget
    except Exception as e:  # noqa: BLE001  没装 SDK 时本函数本就不该被调用
        logging.debug("分词器预热未执行（%s）", e)
        return
    try:
        await history_budget.warmup_tokenizer()
    except Exception as e:  # noqa: BLE001  预热失败只是"没预热"，不是启动错误
        logging.debug("分词器预热失败（%s）⇒ 首次裁剪会稍慢，其余不受影响", e)


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

    默认用直连地址；若部署在反向代理后面（Nginx/网关），真实来源要靠
    `X-Forwarded-For`，但要 **TRUST_PROXY_HEADERS=1 时才信它**。

    ⚠️ 取"第一段"的正确性**完全取决于代理怎么写这个头**（2026-10-04 订正）
    ---------------------------------------------------------------------
    这里取 `split(",")[0]`，即转发头里的**最左段**。它只有在代理**覆盖式**写入时
    才是真实客户端地址 —— 本仓库的 `deploy/app/nginx.conf` 与 `nginx.tls.conf`
    都是 `proxy_set_header X-Forwarded-For $remote_addr;`（覆盖）。
    若代理改用 nginx 的 `$proxy_add_x_forwarded_for`（**追加式**：客户端自带的段
    原样保留在左、真实地址追加在右），最左段就成了**客户端随便写的值** ——
    攻击者每换一个值就换一个限流桶，按 IP 的限流（含 `POST /v1/auth/login` 的
    防爆破闸门）整体失效。

    ⚠️ 别把这条理解成"**代理链里最左才是真实来源**"：那句话是错的，追加式写法下
    最左恰是最不可信的那一段（本仓库曾把这句话写进注释与测试，2026-10-04 订正）。
    改代理配置时请同时看 `tests/test_deploy_topology.py` 里那条
    "nginx 不得用追加式写 X-Forwarded-For"的守卫。

    为什么用 `config.TRUST_PROXY_HEADERS` 而不是在函数里 `os.getenv`
    --------------------------------------------------------------
    它是"必须与部署形态一致"的安全开关，配错时**不报错、只是算错 IP**
    （反代后没开 → 全体共用一个桶；直连时开了 → 信了客户端可伪造的头）。
    收进 config 才会出现在启动日志的配置回显里，运维能自证（`/health` 刻意不暴露
    这些细节，它公网可达）。
    """
    if config.TRUST_PROXY_HEADERS:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def _retrieval_tenant(ident: "Identity") -> str:
    """决定把哪个租户交给**检索层**（向量库过滤）。

    这是隔离能否成立的关键一步，所以单独成一个函数并把判据写下来。

    判据（2026-10-05 收敛后，只有一条）：
        身份自己的租户（来自**已验签的 JWT 载荷**）→ 若为空，落到配置的**公共租户**
        （`RAG_TENANT`）→ 若公共租户也没配，返回 `config.NO_TENANT_SENTINEL`。

    ⚠️ 为什么最后那一档不能退化成空串
    ------------------------------
    历史上这里给的是空串（"没有租户就为空"，2026-10-04 拍板），代价是
    **自助注册的账号在获批入租之前就能读到全库**：那一版的检索层把空租户读成
    "**不加过滤**"，也就是能召回所有租户的内容。

    现在改成：无租户 ⇒ 只看到公共租户；公共租户没配 ⇒ **什么都看不到**。
    两档都不再"不加过滤"，所以"忘了给一个账号分配租户"这个疏忽的后果是
    "他搜不到东西"（可见、可排查），而不是"他能搜到所有人的东西"（不可见）。

    ⚠️ 与之配套，`src/rag/retrieval.py` 的 `resolve_tenant()` 已收成**同一条**规则
    （空串与 None 同义）。两层必须逐字一致 —— 只要有一层把空串读成"不过滤"，
    这一整套 fail-closed 就只是文档上的，而检索层会**静默放行**。

    ⚠️ 与 `tenant`（用于配额分桶、审核归属、会话属主）**刻意不同**：那里空串
    仍是空串（"这个人没有租户"是一个需要如实记录的事实），而这里是"给他看什么"
    —— 两件事，不能共用一个取值。
    """
    return (ident.tenant or "").strip() or config.DEFAULT_TENANT or config.NO_TENANT_SENTINEL


# ---------------------------------------------------------------------------
# 同步 IO 的出口：把阻塞调用挪到工作线程
# ---------------------------------------------------------------------------
# 为什么需要它（2026-10-05 补，#126）
# --------------------------------
# 本服务的事件循环上原本有一整批**同步**网络调用：鉴权查会话白名单（Redis）、
# 限流（Redis）、配额（Redis）、幂等（Redis）、会话归属（PG），全部经由同步客户端
# 在 `async def` 端点里**直接执行** —— 一次 `/v1/chat` 会在这一条循环上串起约 5 次
# 网络往返（1 次 PG + 4 次 Redis）。
#
# 正常时每次只有几毫秒，看不出问题。危险形态是"某一次特别慢"：Redis 抖动、
# PG 建连排队、目标端半开连接。此时**本进程内所有并发请求一起停转**（含正在推的
# SSE 流），而每个调用只受自己的上限约束 —— Redis 是 2 秒，PG 那条查询
# **没有上限**（只有 connect_timeout）⇒ 一次 PG 抖动能让整个事件循环永久冻住，
# 现场是"进程还在、所有请求都不动，重启即恢复"，几乎没人会往"某个调用是同步的"
# 上想。
#
# 处理方式分两类（这是刻意的，不是口径不一致）：
#
#   1. **纯同步端点 → 改回 `def`**。FastAPI 见到 `def`（非 async）端点会把它丢进
#      anyio 的工作线程池，端点体里的同步调用**天然不在事件循环上**。本轮把 21 个
#      "函数体里一个 await 都没有"的端点全部改成 `def` —— 它们本来就不该是 async；
#      async 对它们只是装饰性的，唯一实际效果是把阻塞直接压在循环上。
#
#   2. **真异步的调用点 → `await _off(...)`**。只剩 SSE 那几个端点（必须 async
#      才能流式产出）与 `_stream_run` 的收尾，靠 `def` 解决不了，只能显式挪走。
#
# ⚠️ 为什么用 `asyncio.to_thread` 而不是换成 async 客户端：本项目的 Redis / PG
# 客户端是**跨调用共享的模块级单例**，换成 async 客户端要连带重写整个存储层
# 及其测试；`to_thread` 不动任何存储层、语义完全等价。代价是每个请求多几次
# 线程切换（微秒级），相对一次网络往返可忽略。
#
# ⚠️ 线程池容量：`asyncio.to_thread` 用循环的默认执行器，`def` 端点用 anyio 的
# 线程池（默认 40）。两者都**有界**，因此这层改造顺带把"无限并发压垮上游"变成了
# "排队"—— 这是想要的方向（有界 + 背压，好过无界地冻住循环）。
async def _off(fn, /, *args, **kwargs):
    """在工作线程里执行同步函数并 `await` 它。

    fn: 任意同步可调用对象（如 `_LIMITER.check`、`_SESSIONS.claim`）。
    *args / **kwargs: 原样透传。

    为什么要分 `kwargs` 两种情况：`asyncio.to_thread` 只吃位置参数，
    带关键字的调用得包一层。多数调用点（`_LIMITER.check` 等）用关键字传参，
    所以这个分支是常用路径而不是边角。
    """
    if kwargs:
        return await asyncio.to_thread(lambda: fn(*args, **kwargs))
    return await asyncio.to_thread(fn, *args)


def _route_key(request: Request) -> str:
    """限流用的路由键：优先取**路由模板**（如 `/v1/sessions/{thread_id}`）。

    为什么不用 `request.url.path`（2026-10-06 订正）
    ---------------------------------------------
    原始路径会让**每个资源 id 各占一个桶**：`/v1/sessions/a`、`/v1/sessions/b` ……
    于是额度随"客户端能造出多少个 id"放大（每个 id 各 120/分钟），共享出口跟踪表
    也会被一个个 thread_id 撑大 —— 而 `check()` 的文档里一直写的是"按端点拆开"。
    模板把整类端点收成一个桶，才是那个口径。

    路由未匹配时（中间件里、404）`scope` 里没有 `route`，回落到原始路径。
    ⚠️ 有些测试用的是只实现 `headers` / `url` 的轻量替身（没有 `scope`），
    所以全程用 `getattr` 取值，不假设它是完整的 Starlette Request。
    """
    scope = getattr(request, "scope", None) or {}
    route = scope.get("route") if isinstance(scope, dict) else None
    template = getattr(route, "path", "") if route is not None else ""
    if template:
        return template
    return getattr(getattr(request, "url", None), "path", "") or ""


def _rate_limit(request: Request, session: str, subject: str = "",
                subject_verified: bool = False) -> None:
    """限流闸门：按 IP + 会话双维度判，超限抛 429（带 Retry-After）。

    拆成独立函数是为了让**只读**端点（如会话状态快照）能复用限流而
    不触发配额与"会话认领"的副作用。

    subject / subject_verified: 请求主体与它是否来自**已验签身份**。它们只用于
    "这个 IP 是不是共享出口"的判定与降级分桶（见 `RateLimiter` 的说明）——
    ⚠️ 自报的用户名（登录/注册接口）**必须** subject_verified=False，否则等于把
    限流键交给攻击者。
    """
    rl = _LIMITER.check(ip=_client_ip(request), session=session,
                        route=_route_key(request), subject=subject,
                        subject_verified=subject_verified)
    if not rl.allowed:
        metrics.inc("rate_limited_total", scope=rl.scope or "unknown")
        logging.warning("限流拒绝：scope=%s limit=%d session=%s", rl.scope, rl.limit, session)
        raise HTTPException(status_code=429, detail=f"请求过于频繁（{rl.scope} 维度超限）",
                            headers={"Retry-After": str(rl.reset_after)})


def _quota_gate(tenant: str, subject: str = "") -> int:
    """配额闸门：**预占**当日额度，不足则抛 429；返回预占量供结算用。

    tenant: 租户（来自鉴权，可信）。
    subject: 主体（用户名，来自鉴权，可信）。**必须原样传给结算** —— 每人级的
        桶键按它算，claim 与 settle 传的不是同一个值就会把账记到别人的桶上
        （症状是"某人的额度莫名被吃"）。

    为什么在这里"预占"而不是只"检查"
    -------------------------------
    旧实现是"这里 check、`_stream_run` 的 finally 里 add"。判定与记账分在请求
    两端、中间隔着整次生成，并发的 N 个请求就会各自读到同一个"还没超"的旧值
    然后一起放行，最后各自 add —— 日额度被超掉 N 份。这不是理论竞态：它只在
    额度快用完时发生，而那正是额度最该被守住的时候。

    现在换成 `QuotaManager.claim`（先占后判，一次原子操作）。返回的预占量必须
    **原样**交给 `_stream_run` 去 `settle`，否则预占永远不退、额度会被逐次吃光。

    拒绝时的 `scope` 会带进日志与指标：额度可能是被租户级拦下的，也可能是被
    每人级拦下的 —— 两种情况的处置完全不同（前者要提租户额度，后者要么放宽
    每人额度、要么就是那个人真的刷太多了）。
    """
    q = _QUOTA.claim(tenant, subject=subject)
    if not q.allowed:
        metrics.inc("quota_exceeded_total", scope=q.scope or "unknown")
        logging.warning("配额拒绝：scope=%s tenant=%s subject=%s used=%d limit=%d",
                        q.scope, tenant, subject, q.used, q.limit)
        raise HTTPException(status_code=429,
                            detail="今日配额已用尽，请明日再试或联系管理员",
                            headers={"Retry-After": str(q.reset_after)})
    return q.reserved


def _reserve_quota(tenant: str, subject: str, idem_key: str) -> int:
    """预占配额；若预占失败，**先退还已抢占的幂等键**再抛出。

    tenant / subject: 来自鉴权，可信；两者都要原样传给 `_stream_run` 的结算。
    idem_key: 本次请求已抢占的幂等键（可能为空 = 客户端没带 `Idempotency-Key`）。
        退还必须带上同一个 `subject`（见 `IdempotencyStore._key`）—— 少了它就会
        去撤销另一个命名空间里的键，原键留在"在途"直到 TTL 到期。

    为什么"退还"必须和预占绑在一起
    -----------------------------
    `_IDEMPOTENCY.begin()` 是**先占后判**的原子抢占（见 `_IdemGate` 的说明）：
    键此刻已经是"在途"状态。而归还只发生在 `_stream_run` 的 finally ——
    配额 429 会在这里抛出，生成器根本没被创建，那个 finally 永远不执行。

    后果很隐蔽：客户端拿到 429、等额度恢复后拿**同一个键**重试，却收到
    409"正在处理中" —— 而那个"正在处理"的请求从来没有存在过，要一直等到 TTL
    到期。守卫：`tests/test_idempotency.py::test_a_quota_rejection_releases_the_claimed_key`。

    为什么单独抽成函数（2026-10-05）
    ------------------------------
    这段 `try/except + forget` 原先内联在 `_sse` 里。而 `_sse` 现在是被
    两类调用方共用：`def` 端点在**工作线程**里调它（同步没问题），而
    `/v1/review`、`/v1/chat/resume` 这两个必须 async 的端点只能在外面先
    `await _off(_reserve_quota, ...)` 预占、再把结果传进来。抽出来才能让
    "预占+退还"这条成对逻辑只存在一份，不会被两个调用方各写一遍。
    """
    try:
        return _quota_gate(tenant, subject)
    except Exception:
        if idem_key:
            _IDEMPOTENCY.forget(tenant, idem_key, subject=subject)
        raise


def _session_http_error(e: SessionError) -> HTTPException:
    """把归属层的业务错误转成 HTTP 异常（与 `_account_http_error` 同一形状）。

    ⚠️ 必须**保留** `status_code`，不能统一成 403。两种失败的性质完全不同：
      - 403：**结论明确** —— 这个 thread_id 确实属于别人；
      - 503：**没有结论** —— 归属表读不到/写不进，判不了。
    把 503 伪装成 403 的代价不是"消息不好看"：客户端会据此认为"这个 id 真的不是
    我的"，于是换一个 id 继续试或直接放弃，而正确行为是**重试**。
    归属校验改成 fail-closed 之后，"判不了"会从偶发变成一种常见路径，
    这个区分就从"锦上添花"变成了必需品。
    """
    return HTTPException(status_code=e.status_code, detail=f"{e.detail}（code={e.code}）")


def _claim_session(session: str, tenant: str, user: str = "") -> None:
    """会话归属闸门：首次见到即认领；已被别的主体认领则抛 403。

    ⚠️ 这是**有副作用**的（写登记表），只允许用在"要开始/继续一次运行"的端点。
    只读端点必须用 `_assert_session_readable`，否则攻击者只要先读一次就能
    把别人尚未开始的 thread_id 占为己有。

    归属主体是 **(tenant, user)**：只在租户级比对时，同租户的两个使用者彼此
    完全可见 —— 能互读会话、互删会话、互相替对方接受待审草稿。

    判不了时抛 503 而不是 403（见 `_session_http_error`）。
    """
    try:
        ok, owner = _SESSIONS.claim(session, tenant, user)
    except SessionError as e:
        metrics.inc("session_registry_errors_total", code=e.code)
        raise _session_http_error(e) from e
    if not ok:
        metrics.inc("session_owner_mismatch_total")
        logging.warning("会话归属不符，拒绝：session=%s owner=%s/%s requester=%s/%s",
                        session, owner[0], owner[1], tenant, user)
        raise HTTPException(status_code=403, detail="该会话属于其他主体")


def _preflight(request: Request, session: str, tenant: str, user: str = "") -> None:
    """统一前置闸门：限流 + 会话归属，任一不过直接抛 4xx。

    request: 请求对象（取 IP 与路由名）。
    session: 会话 id（限流用，同时校验归属）。
    tenant: 租户（来自鉴权，可信）。
    user: 用户主体（第二档，来自已验签凭证）。API Key 路径为空串。

    顺序：**限流 → 会话归属**。限流最便宜（本地计数），先挡住明显刷量；归属校验
    可能走数据库，所以放后面。

    ⚠️ **归属校验必须排在业务校验之前**：`/review`、`/resume` 在通过闸门后还要读
    待审记录 / 运行现场（不在审核态 → 409 / 没有现场 → 409）。如果先做状态校验，
    攻击者就能用别人的 thread_id 区分"存在且在审核态"与"不存在"，把 404/409 变成
    枚举探针。

    ⚠️ 这里**刻意不做配额预占**：那几个 409 会让请求在进入生成前就结束，而预占的
    额度只有 `_stream_run` 的 finally 会退还 —— 放在这里必然泄漏额度。配额预占
    因此移到 `_sse`，与结算成对（理由见 `_sse` 的说明）。
    """
    _rate_limit(request, session, subject=user, subject_verified=bool(user))
    _claim_session(session, tenant, user)


# ---------------------------------------------------------------------------
# 会话状态快照（断点续传的基础）
# ---------------------------------------------------------------------------
# 为什么需要它
# ----------
# SSE 一断（弱网、切后台、代理超时），客户端手里只剩下"已经打到屏幕上的那半句"，
# 除了从头重问一遍别无选择，而重问会**再花一次 token**。所以这一节只做一件事：
# 把"这条会话现在进行到哪了"按客户端能用的形态暴露出来。
#
# 状态由**三份数据**拼出来，各回答一半，缺一份就会给出错误结论
# ------------------------------------------------------------
#   · **官方会话**（``session.load_items``）—— 这条会话说过什么。图退役之后它是
#     **唯一**的对话历史来源，没有第二份；
#   · **运行现场**（``run_store``，官方 ``RunState``）—— 有没有"跑到一半停下来等
#     审批"的那一轮。只有它才谈得上"接着跑"；
#   · **待审队列**（``_REVIEW_QUEUE``）—— ``manual`` 那类（跑偏 / 护栏拦下 /
#     没产出）运行**已经结束**，只有这条记录，没有现场。
#
# ⚠️ 官方**只**在"运行中产生了 interruption"时提供可恢复的现场。客户端断流导致的
# "跑了一半"没有现场（崩溃恢复官方刻意不管，``human_in_the_loop.md`` 明确要求生产
# 应用自己做）。所以 ``status`` 里没有 "incomplete" 这一档，``/v1/chat/resume``
# 对这种情况如实回 409 —— 假装能续的做法是**再跑一轮**，会把已经发出去的 token
# 重发一遍、把已经落盘的资产再做一遍。
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


def _session_view(thread_id: str, *, held=None, record=None,
                  messages=None) -> dict:
    """把"这条会话现在是什么状态"拍成对外的视图（纯 JSON 可序列化）。

    held: ``run_store.load()`` 的返回值 —— 运行**中途**停下来等审批的现场
        （官方 ``RunState``）。有它才谈得上"接着跑"。
    record: ``_REVIEW_QUEUE.record()`` 的返回值 —— 待审队列里的那条记录。
        ``manual`` 那类（跑偏 / 护栏拦下 / 没产出）运行已经结束，只有队列、没有现场。
    messages: 官方会话的历史条目（``session.load_items``）。这是**唯一**的对话历史
        来源 —— 图退役之后没有第二份了。

    为什么状态只有这三种
    ------------------
    * ``awaiting_review``：有现场或有待审记录 ⇒ 要人做决定，走 ``/v1/review``；
    * ``completed``：有历史 ⇒ 这一轮已经收尾（或从未需要干预）；
    * ``unknown``：什么都没有 ⇒ 从未开始，或已被清理。
    """
    awaiting = bool(held) or bool(record and record.get("status") == "pending")
    history = _serialize_messages(messages)
    if awaiting:
        status = "awaiting_review"
    elif history:
        status = "completed"
    else:
        status = "unknown"

    draft = (held or {}).get("draft") or (record or {}).get("draft") or ""
    review = None
    if awaiting:
        review = {
            "draft": draft,
            # 这条链路不产 0-100 分 ⇒ ``None``。**不能给 0**：审核台上 0 分是
            # "AI 判了 0 分"，而真相是"没有人评过"。
            "score": None,
            "feedback": (record or {}).get("feedback") or "",
            "error": None,
        }

    return {
        "thread_id": thread_id,
        "status": status,
        # 只有"有现场"才谈得上接着跑；其余情况"续"就等于新起一轮。
        "resumable": bool(held),
        "iteration": 0,
        "score": None,
        "feedback": (record or {}).get("feedback") or "",
        "draft": draft,
        # 兜底结局属于"上一轮的过程结论"，现在不落在任何持久化状态里
        # （它随 SSE 的 `task_outcome` 事件一次性交付）。字段保留是为了不改契约。
        "task_outcome": "",
        "fallback_reason": "",
        "unfinished_steps": [],
        "citations": [],
        "citation_check": {},
        "messages": history,
        "pending_review": review,
    }


def _assert_session_readable(thread_id: str, tenant: str, user: str = "") -> None:
    """读会话前的归属校验。**不写登记表**（与 `_claim_session` 的语义刻意不同）。

    为什么读路径不能沿用 `_claim_session`：`claim()` 是"首次见到即认领"，
    用在只读端点上会让攻击者**先读一次就把别人还没开始的 thread_id 占住** ——
    一个读操作不该产生归属副作用。

    多租户模式下**查不到属主一律 404（fail-closed）**：正常情况下每条会话都先
    经过 /chat 的 `_preflight` 认领过，查不到属主就意味着"这个 id 不存在"。
    若不这样处理，返回内容（或 403/404 的差异）会变成一个"这条会话是否存在"的
    可枚举探针。单租户下所有请求同属主，没有区分能力，直接放行。

    ⚠️ "查不到属主"（404）与"**查不出来**"（503）必须分开：前者是结论，后者是
    没有结论。存储抖动时若也回 404，排障会直奔"这个会话是不是被删了"，
    而真实原因是数据库连不上 —— 一个把人带偏的假结论。
    """
    if not _SESSIONS.enabled:
        return
    try:
        owner = _SESSIONS.owner_of(thread_id)
    except SessionError as e:
        metrics.inc("session_registry_errors_total", code=e.code)
        raise _session_http_error(e) from e
    if owner is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    # ⚠️ 比的是 **(tenant, user) 元组**，不是拼接后的字符串。
    # 拼接会让 (tenant="a/b", user="") 与 (tenant="a", user="b") 相等 ——
    # 一个"用字符串做权限比较"的经典缺陷，且没有任何运行时症状。
    if owner != (tenant, user):
        metrics.inc("session_owner_mismatch_total")
        logging.warning("会话归属不符，拒绝读取：session=%s owner=%s/%s requester=%s/%s",
                        thread_id, owner[0], owner[1], tenant, user)
        raise HTTPException(status_code=403, detail="该会话属于其他主体")


async def _delete_agent_session(thread_id: str) -> bool:
    """删除该会话在 **SDK 侧**的跨轮历史（``agent_sessions`` / ``agent_messages``）。

    为什么必须单独删：这份历史与运行现场、待审记录是**三份数据** ——
    一份是"下一轮的模型会读到的对话"（官方 Session），一份是"被打断的那次运行"，
    一份是"等奖人看的草稿"。删除端点此前只动后两份，于是"删除请求"之后还完整
    留着用户说过的每一句与助手产出过的每一段。接口 docstring 里写着"缺一类都
    算不上删干净"，而它漏的正是这一类。

    ⚠️ 惰性 import：``src/agents/session.py`` 本身不依赖 SDK（官方会话后端是在函数
    内部才 import 的），但这里保持与本文件其它 agent 相关代码同一姿势 ——
    万一将来有人把它改成模块级 import，症状会是"整条服务链接不起来"。

    返回是否真的清了。**取不到模块 / 后端不可用都只是 False**：删除是幂等的，
    明细里如实告诉调用方每一项到底删掉没有，比让整个请求失败更有用。
    """
    try:
        from src.agents import session as agent_session
    except Exception as e:  # noqa: BLE001  没装 SDK / 模块导入失败
        logging.debug("会话历史删除不可用：%s", e)
        return False
    return await agent_session.clear_session(thread_id)


async def _delete_thread_state(thread_id: str) -> bool:
    """删除该会话的**运行现场**（官方 ``RunState`` 快照）。

    这份数据现在只装一件事：某个待审批的运行停在哪。它含整段历史（快照是
    完整现场），所以删除请求必须把它一起清掉 —— 只删会话历史的话，一份"已被
    要求删除"的对话原文仍留在快照里，而接口 docstring 承诺的是删干净。

    返回是否真的删掉了一条。存储异常不上抛：删除是幂等的，如实告诉调用方
    每一项到底删掉没有，比让整个请求失败更有用。
    """
    try:
        return await _off(run_store.drop, thread_id)
    except Exception as e:  # noqa: BLE001
        logging.warning("运行现场删除失败（thread=%s）：%s", thread_id, e)
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


def _thread_id(value: str) -> str:
    """`thread_id` 的校验：**去空白后不得为空**，再判长度上限。

    ⚠️ "非空"这条是 2026-10-05 补的缺口。此前只拦长度上限，空串是**合法输入**；
    而会话历史、运行现场、归属表全部按 thread_id 落库，于是所有提交空 id 的客户端
    **共用同一个空 key 的会话空间** —— 彼此的上下文互相可见；更糟的是归属校验对
    空 id 是"直接短路放行"，所以这条隔离在最需要它的时候（多客户端共用空 id）
    恰好不起作用。这是典型的"**合法输入导致隔离失效**"，必须在入口挡掉，
    不能只靠存储层兜（`SessionRegistry.claim` 里还有一道，那是纵深防御）。

    ⚠️ 返回的是**去空白后**的值，不是原值。此前不去空白、而存储层内部去空白，
    导致 `"a"` 与 `"a "` 在归属表里是同一行、在历史表里却是两个不同 key ——
    同一个会话在两个子系统里落到不同的键上。在入口统一规范化，两边就一致了。
    """
    v = (value or "").strip()
    if not v:
        raise ValueError("thread_id 不能为空")
    return _bounded(v, config.MAX_THREAD_ID_CHARS, "thread_id")


def _password(value: str, field: str) -> str:
    """**新设**密码的校验：先下限、后上限。

    下限（`config.MIN_PASSWORD_CHARS`，默认 8）是 2026-10-04 补的缺口：此前只拦上限，
    能设出 1 个字符的密码 —— 防住了 PBKDF2 对超长输入的开销，却没防弱密码。
    行业基本线取 8（NIST SP 800-63B / OWASP 的起点），理由写在 config 那一行。

    ⚠️ 只用于"新密码"。登录（`LoginReq`）与被验的旧密码（`ChangePasswordReq.old_password`）
    **刻意不走这里** —— 下限是"不许设新的弱密码"，不是"不许用已有的短密码"；
    后者会把早期设的短密码反锁在门外，连改成合规密码都做不到。
    """
    if len(value) < config.MIN_PASSWORD_CHARS:
        raise ValueError(
            f"{field} 太短：{len(value)} 字符，至少需要 {config.MIN_PASSWORD_CHARS} 字符")
    return _bounded(value, config.MAX_PASSWORD_CHARS, field)


class ChatReq(BaseModel):
    thread_id: str
    message: str

    @field_validator("thread_id")
    @classmethod
    def _v_thread_id(cls, v: str) -> str:
        return _thread_id(v)

    @field_validator("message")
    @classmethod
    def _v_message(cls, v: str) -> str:
        return _bounded(v, config.MAX_MESSAGE_CHARS, "message")


class SessionMessageReq(BaseModel):
    """追加到会话的一句补充（**不触发运行**）。

    与 ``ChatReq`` 的区别只有一处，但它在语义上是全部：``/v1/chat`` 是"开始一轮"，
    本请求是"给正在跑的那一轮补一句"。所以它没有 thread_id 字段（在路径里）、
    也不该被当成新任务 —— 见 ``POST /v1/sessions/{id}/messages`` 的说明。
    """

    message: str

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
        return _thread_id(v)

    @field_validator("feedback")
    @classmethod
    def _v_feedback(cls, v: str) -> str:
        return _bounded(v, config.MAX_FEEDBACK_CHARS, "feedback")


class ResumeReq(BaseModel):
    """续跑请求体：只有 thread_id —— 要跑的内容早就存在运行现场里了。"""

    thread_id: str

    @field_validator("thread_id")
    @classmethod
    def _v_thread_id(cls, v: str) -> str:
        return _thread_id(v)


class LoginReq(BaseModel):
    """登录请求体：本地用户名密码。

    ⚠️ `password` 是唯一一个会出现在**请求体**里的敏感字段。它不进任何日志：
    本模块对请求体的记录一律取字段白名单，而登录失败日志只记 `code`。
    """

    username: str = ""
    password: str = ""

    @field_validator("username")
    @classmethod
    def _v_username(cls, v: str) -> str:
        return _bounded(v, config.MAX_USERNAME_CHARS, "username")

    @field_validator("password")
    @classmethod
    def _v_password(cls, v: str) -> str:
        return _bounded(v, config.MAX_PASSWORD_CHARS, "password")


class RefreshReq(BaseModel):
    """刷新会话请求体。"""

    refresh_token: str

    @field_validator("refresh_token")
    @classmethod
    def _v_refresh_token(cls, v: str) -> str:
        return _bounded(v, config.MAX_AUTH_TOKEN_CHARS, "refresh_token")


class RegisterReq(BaseModel):
    """自助注册请求体。

    ⚠️ 注册出来的账号**没有租户**（租户字段为空串）。按本项目当前口径，空租户在
    检索层等于"不加过滤" ⇒ **注册完即可搜到全部租户的文档**。这是刻意的
    （用户 2026-10-04 拍板）。也正因如此，这个端点默认**关闭**
    （需要 `AUTH_ALLOW_SELF_REGISTER=true` 才开放）。

    想加入租户要走 `POST /v1/join-requests` 提交申请，由管理员审批。
    """

    username: str
    password: str

    @field_validator("username")
    @classmethod
    def _v_username(cls, v: str) -> str:
        return _bounded(v, config.MAX_USERNAME_CHARS, "username")

    @field_validator("password")
    @classmethod
    def _v_password(cls, v: str) -> str:
        return _password(v, "password")


class ChangePasswordReq(BaseModel):
    """改自己的密码（需要给出当前密码）。

    为什么要验旧密码：不验的话，一个被借走的**已登录设备**可以静默改密码并锁死
    原主人 —— 而"改密码"正是原主人用来踢掉入侵者的手段。要求旧密码让这条攻击
    至少需要片刻的已知凭据。
    """

    old_password: str
    new_password: str

    @field_validator("old_password")
    @classmethod
    def _v_old(cls, v: str) -> str:
        return _bounded(v, config.MAX_PASSWORD_CHARS, "old_password")

    @field_validator("new_password")
    @classmethod
    def _v_new(cls, v: str) -> str:
        return _password(v, "new_password")


class TenantReq(BaseModel):
    """新建租户。`name` 就是租户的唯一标识（没有额外的 id 层）。"""

    name: str
    note: str = ""


class TenantRenameReq(BaseModel):
    """租户改名。挂靠用户的租户字段会被**同批改写**（见 AccountStore.rename_tenant）。"""

    new_name: str


class UserReq(BaseModel):
    """管理员创建用户。

    tenant 允许留空 = 创建一个"还没有租户"的用户（他会落到与自助注册相同的状态）。
    roles 是纯字符串列表 —— 代码里**不存在**合法角色清单，真正说了算的是配置里的
    `AUTH_REVIEWER_ROLES` / `AUTH_ADMIN_ROLES`：没被列进去的角色只是装饰品。
    """

    username: str
    password: str
    tenant: str = ""
    roles: list = []

    @field_validator("username")
    @classmethod
    def _v_username(cls, v: str) -> str:
        return _bounded(v, config.MAX_USERNAME_CHARS, "username")

    @field_validator("password")
    @classmethod
    def _v_password(cls, v: str) -> str:
        return _password(v, "password")


class UserPatchReq(BaseModel):
    """管理员修改用户。**只改显式提供的字段**（缺省 = 不动）。

    ⚠️ `tenant` 的类型是 `str | None`，默认 `None` 表示"不动"，而 `""` 表示
    "把他移出租户"。这两个必须区分：类型写成 `str = ""` 的话，"移出租户"这个动作
    会被静默实现成"什么都没发生"。
    """

    password: str | None = None
    tenant: str | None = None
    roles: list | None = None
    status: str | None = None

    @field_validator("password")
    @classmethod
    def _v_password(cls, v):
        return None if v is None else _password(v, "password")


class JoinReqReq(BaseModel):
    """提交"加入某个租户"的申请。"""

    tenant: str
    note: str = ""


class JoinResolveReq(BaseModel):
    """审批一条加入申请。

    approve=true 时**顺带把申请人的租户改成申请的那个** —— 两件事必须同批完成，
    否则会出现"申请批了、人还在无租户状态"，而无租户正好是"能看全部内容"的那一档
    （见 AccountStore.resolve_join_request）。
    """

    approve: bool
    note: str = ""


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


# ---------------------------------------------------------------------------
# 引用校验的交付策略（见 src/rag/citation_check.py）
# ---------------------------------------------------------------------------
# 现状：`citations` 非空只说明"这一轮检索命中了东西"，**推不出**"答案用了它"、
# 更推不出"答案有依据"。而模型完全可以在命中 3 条之后写出一段与三条都不沾边的
# 话，还带着 3 条引用 —— 前端如实展示，看起来就像"有出处"。
_CITATION_CHECK_FIELDS = (
    "status", "ok", "detail", "citations", "refs", "missing",
    "best_overlap", "best_overlap_n", "warnings",
)


def _public_citation_check(check: dict) -> dict:
    """把校验结论收敛成**可以下发给客户端**的字段（不下发内部统计字段）。"""
    return {k: check.get(k) for k in _CITATION_CHECK_FIELDS}


def _citation_blocked() -> bool:
    """当前是否处于"拦截不可信引用"的策略（且开关是开的）。"""
    return (config.CITATION_CHECK
            and (config.CITATION_CHECK_ACTION or "").strip().lower() == "block")


def _record_agent_metrics(stats: dict, has_guardrail: str = "") -> None:
    """把 agent 链路的运行统计翻译成指标。

    为什么翻译放在服务层、而不是链路层直接上报
    ------------------------------------------
    ① `src/` 不能依赖 `app/`（见 `src/observability.py`）——链路层只把统计**交出来**；
    ② 标签的**基数与命名**是运维契约（告警规则、Grafana 面板都按这些名字与标签写），
       集中在服务层改一处就够，不会出现"链路加了标签、告警没跟上"。

    标签一律取**有限取值**：退出原因、agent 名、护栏名。
    **绝不**放 thread_id / 用户输入这类高基数值 —— 标签进 key，会把内存打爆。

    stats: ``TaskContext.stats.as_dict()``（普通 dict）。
    has_guardrail: 被哪条出口护栏拦下（空 = 没被拦）。

    ⚠️ 这里报的是**整轮**口径（``agents`` / ``handoffs`` / ``tool_calls``）——
    一次请求里可能有多次 handoff，这正是运维要的"这一轮花了多少"。
    **单次运行**的口径在 ``runner._report_run_metrics`` 的直方图里，那里带
    ``agent`` 标签。两处不能混：把整轮数字塞进单次运行的直方图，分位数会失去含义。
    """
    if not stats:
        return

    for pair in stats.get("handoffs") or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            metrics.inc("agent_handoff_total", src=str(pair[0]), dst=str(pair[1]))
    for name in stats.get("agents") or []:
        metrics.inc("agent_participants_total", agent=str(name))

    attempts = int(stats.get("attempts") or 0)
    review_rounds = int(stats.get("review_rounds") or 0)
    if attempts > 1 or review_rounds > 1:
        # 返工率 = agent_rework_total / 请求总数。
        # 它是"能力"最直接的一个代理指标：一次通过说明规划与执行到位，
        # 反复返工说明 prompt 或产出质量有问题 —— 而这个量只能靠埋点拿到。
        #
        # ⚠️ 判据必须是这两条**合起来**：
        #   · `attempts > 1` —— 代码侧回灌重来的轮次（评审改成 `as_tool` 后恒为 1，
        #     但留着这条是因为"哪天有人加回循环"时它必须继续生效）；
        #   · `review_rounds > 1` —— 模型**自己**改了一轮再评。这是当前形态下唯一
        #     能反映自我返工的信号，只留 `attempts` 会让这个指标永远为 0。
        metrics.inc("agent_rework_total")

    retrieved = int(stats.get("retrieved_chunks") or 0)
    retrieval_calls = int(stats.get("retrieval_calls") or 0)
    if retrieval_calls:
        metrics.observe("agent_retrieval_calls", retrieval_calls)
    if retrieved:
        metrics.observe("agent_retrieved_chunks", retrieved)

    # 工具调用次数：与 model_calls 合起来才能回答"agent 绕圈子时是**模型在想**
    # 还是**工具在往返**"。只有模型调用次数时，这两种成因分不开，
    # 而处置方向完全相反（前者要收紧 prompt，后者要减少工具/合并调用）。
    tool_calls = int(stats.get("tool_calls") or 0)
    if tool_calls:
        metrics.observe("agent_tool_calls", tool_calls)

    if has_guardrail:
        metrics.inc("agent_guardrail_trips_total", guardrail=str(has_guardrail))


def _maybe_sample_quality(text: str, context: str) -> None:
    """按采样率对**本轮产出**跑一遍离线判据，把线上幻觉率变成指标。

    参数是**内容本身**而不是某个状态对象：这条链路此前签名是 `values: dict`，
    因为产出与资料都长在 LangGraph 的 state 里。图去掉之后没有 state 了 ——
    继续要求调用方拼一个假 dict 只会让"这两个字段从哪来"更难追。

    为什么可以直接在请求里跑
    ----------------------
    `src/rag/answer_quality` 是**纯规则**：正则 + 集合运算 + 与引用编号比对，
    零模型调用、零外部依赖、毫秒级。它与 LLM-as-judge 不是一类东西 ——
    后者有噪声、要密钥、不可复现，所以只配活在离线与 CI 里（见 README 的三层评测）。

    为什么默认关（采样率 0）
    --------------------
    这是**趋势**指标，不需要全量；而每个被采样的请求都要多为它花几毫秒。
    打开时建议 0.02 ~ 0.1：够画出曲线，又几乎无感。

    verdict 必须保留三态
    ------------------
    `unknown`（答案里没有可核的标识符 / 没有资料可核）单独一档。把它并进
    `pass` 会让线上 ok_rate 朝"看起来更好"的方向偏 —— 这正是离线口径里
    反复强调过的那个偏差，在线口径不能反过来犯。
    """
    rate = config.ONLINE_QUALITY_SAMPLE
    if rate <= 0 or random.random() >= rate:
        return
    if not (text or "").strip():
        return
    try:
        from src.rag import answer_quality as AQ
        # context 取**线上同款**拼装结果（运行上下文里累积的检索资料），不是原始
        # chunk 列表 —— 口径与线上不一致的评测最隐蔽（测了一个不存在的链路）。
        got = AQ.score_answer(text, context or "")
    except Exception as e:  # noqa: BLE001  观测失败绝不能影响作答
        metrics.inc("online_quality_sampling_errors_total")
        logging.debug("在线质量采样失败（已忽略）：%s", e)
        return
    ok = got.get("ok")
    verdict = "unknown" if ok is None else ("pass" if ok else "fail")
    metrics.inc("online_answer_judged_total", verdict=verdict)
    if verdict == "fail":
        # 与 `ok=False` 同义：出现了**确凿的编造**（标识符或引用编号）。
        # 单独一条无标签计数，是为了让告警能直接写 `> 比值` 而不必去拆 verdict。
        metrics.inc("online_answer_hallucinated_total")


def _should_withhold_citations(check: dict) -> bool:
    """是否**不下发**引用。

    抽成纯函数是为了可测：这一条决定"用户还能不能看到引用"，而它的失效方式
    恰恰是最安静的那种 —— 拦错了（把正常引用吞掉）不会报错，只会让引用区空着，
    看起来和"这一轮没检索到东西"一模一样。

    两道闸门**缺一不可**：
      1. `block_worthy` —— 只有确凿错误（答案引用了不存在的编号）才算。
         重叠率低只是**可疑**：模型改写幅度大一点就会低，拿它吞掉用户的引用，
         误伤成本远大于收益；
      2. 策略真的配成了 `block` —— 默认 `warn`，行为与改造前**完全一致**。
    """
    if not check or not check.get("block_worthy"):
        return False
    return _citation_blocked()


def _client_error(exc: BaseException, rid: str = "") -> str:
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

    rid: 显式传入的请求 id。默认空串 = 自己去 contextvar 取（请求**内部**调用时
    这条路有效）。未处理异常的处理器在最外层，读不到那个 contextvar，必须由调用方
    从 `request.scope` 取好再传进来 —— 否则文案里就没有 rid，用户报障时无从检索。
    """
    if not config.IS_PRODUCTION:
        return f"{type(exc).__name__}: {exc}"
    rid = rid or get_request_id()
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
        """是否已断开。`_stream_run` 主循环每轮读它，用来跳过收尾事件。"""
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
    直接收流，不再有 `done`）—— 收尾那几步（落待审队列、结算配额）也是 IO，
    继续等下去等于没有时限。
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

        factory: 无参可调用对象，**每次调用都要返回一个新协程**（本项目里唯一
            的用法是 `stream.__anext__` —— 也就是"等下一段正文"）。

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
            raise TimeoutError
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


def _stopped_event(req_kind: str, produced) -> dict:
    """用户主动停下：计数 + 日志 + 生成 `stopped` 事件（**终止性**，其后没有 done）。

    与 `timeout` 的区别只有"谁按的"：一个是时限到了，一个是人叫停。两者对客户端
    的语义一样 —— **已产出的正文照常保留**，不要再等一个 `done`。

    为什么必须给一个明确的事件、而不是静默结束流：客户端无法区分"被停了"与
    "网络断了"。前者不该自动重试（用户就是不想让它继续），后者应该 —— 混成一个
    表现，客户端只能二选一，而两种选择都有代价。
    """
    chars = sum(len(x) for x in produced)
    metrics.inc("run_stopped_total", kind=req_kind)
    logging.warning("%s 被用户停止（已产出 %d 字）；已产出的正文保留，本轮不再补 done",
                    req_kind, chars)
    return _sse_json("stopped", {
        "produced_chars": chars,
        # ⚠️ 恒为 False：停止之后**没有**可恢复的现场（官方只在"运行中产生了
        # interruption"时才给 `RunState`，而被停下的一轮是运行被取消，不是工具在
        # 等审批）。所以 `/v1/chat/resume` 对这条会话只会回 409。
        # 以前这里写 True 是因为检查点停在节点边界上——那个前提已经不存在了。
        "resumable": False,
    })


async def _watch_stop(run_id: str, flag, produced, state):
    """独立任务：轮询"这次运行要不要停"，一旦要停就**尽力**关掉上游。

    为什么也必须是独立任务（与 `_watch_disconnect` 同一条理由）
    ------------------------------------------------------
    主循环只在"上游产出 chunk"时才转一圈。上游限流重试、连接挂死时循环体一次都不
    执行 —— 那时"检查停止标志"永远没机会发生，用户点了停止却像没点一样。

    为什么轮询间隔按后端分档
    ----------------------
    内存后端是一次字典读（微秒级），可以密一点；PG 后端是**一次远程查询**，
    0.5s 一次会把连接池当计数器用。跨副本的停止延迟 2s 是刻意的取舍：
    真正在意毫秒级的是同副本的那条路（用户与任务通常落在同一个入口）。

    ⚠️ 关流这一步是"尽力"，不是保证：主循环可能正在驱动这个生成器，
    并发 `aclose()` 会撞上 asyncio 的 "asynchronous generator is already running"。
    这里吞掉该异常 —— 真正的收尾在生成器自己的 `finally` 里。
    """
    interval = 0.5 if not _RUNS.dsn else 2.0
    try:
        while True:
            await asyncio.sleep(interval)
            if flag.is_set():
                return
            if await _off(_RUNS.stop_requested, run_id):
                flag.set()
                logging.warning("收到停止请求：run=%s 已产出 %d 段，正在收手上游",
                                run_id, len(produced))
                stream = state.get("stream")
                if stream is not None:
                    try:
                        await stream.aclose()
                    except Exception:  # noqa: BLE001  含"已在运行"竞态
                        logging.debug("停止时关闭上游流出现异常（已忽略）", exc_info=True)
                return
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001  监听任务出问题不该影响本次生成
        logging.debug("停止监听任务异常退出", exc_info=True)


#: 在途的后台压缩任务。**必须持有引用** —— `asyncio.create_task` 的返回值只被
#: 事件循环弱引用，不存起来的话任务可能在跑完前被 GC 掉（表现为"压缩时有时无"，
#: 而日志里什么都没有）。
_HISTORY_COMPACT_TASKS: set = set()


def _on_compact_done(task) -> None:
    """后台压缩任务的收尾：丢掉引用、把异常记进日志（**不影响任何请求**）。"""
    _HISTORY_COMPACT_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        # 走到这里说明压缩内核自己漏了异常（它内部已尽力兜住）。只记不抛：这个
        # 任务的失败与任何一次请求都无关，抛出去会变成无人接收的异常。
        logging.warning("后台历史压缩任务异常结束（已忽略）：%s", exc)


def _spawn_history_compaction(thread_id: str) -> None:
    """请求开头起一个**后台**压缩任务（入口一·会话侧，见 ``src/agents/compaction.py``）。

    为什么在 ``_stream_run`` 里、而不是端点函数里
    ----------------------------------------------
    * 端点 ``chat_v1`` 是**同步** ``def``，FastAPI 把它丢进线程池 —— 那里没有正在
      运行的事件循环，``asyncio.create_task`` 直接抛 ``RuntimeError``。
    * ``_sse`` 也是同步的（只组装响应对象）。
    * 本函数是 ``async def``（且是异步生成器，第一次迭代时已经有事件循环）。

    **不挡本轮**：任务与本轮的流并行跑，所以

    * 本轮读到的仍是**压缩前**的历史，本轮行为与今天完全一样；
    * **从下一轮开始**读到的才是压过的。

    ⚠️ 刻意**不按 ``req_kind`` 设门**（``/v1/review`` 人工打回那条路照跑）：压缩
    入口自己就是**有条件的**——"老历史超过保留轮数"才压，而保留段最少 1 轮，
    被打回的那一轮永远落在保留段里 ⇒ 打回轮依赖的"原话还在会话里"不会被破坏
    （``src/agents/runner._build_input_items`` 的判据）。门槛设在"该不该压"上，
    不设在"这是哪条路"上。

    任何异常都吞掉：压缩是可选优化，绝不能影响请求。
    """
    if not thread_id:
        return
    try:
        from src.agents import compaction

        if not compaction.history_compact_enabled():
            return
    except Exception:  # noqa: BLE001  压缩内核装不上（不该发生）
        return
    try:
        task = asyncio.create_task(compaction.compact_session_history(thread_id))
    except RuntimeError:  # 没有运行中的事件循环（理论上走不到，见 docstring）
        logging.debug("当前没有事件循环，跳过后台历史压缩")
        return
    _HISTORY_COMPACT_TASKS.add(task)
    task.add_done_callback(_on_compact_done)


async def _turn_events(turn, context, out: dict):
    """跑一轮官方 agent 运行，把正文增量当成"流"逐段吐出。

    为什么用队列桥接
    --------------
    ``Runner.run_streamed`` 是**回调式**的（边跑边把 token 交出来），而调用方需要
    一个"可以逐次 ``await`` 的流" —— 只有那样才能把每一次等待都交给总时限、
    断连、预算这三道闸门。队列是这两种形态之间最薄的一层桥，没有第三套编排。

    结果怎么带回来
    -----------
    异步生成器没法 ``return`` 值，所以结果写进 ``out["outcome"]``；异常写进
    ``out["error"]``，由调用方在流结束后重新抛出 —— 让它和"流读到一半失败"
    走同一条错误路径（否则错误处理会分成两套，而其中一套必然少测）。
    """
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    async def _push(text: str, call_id: str) -> None:
        await queue.put((text, call_id))

    async def _drive() -> None:
        try:
            out["outcome"] = await run_turn(turn, context=context, on_text=_push)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  统一交给调用方
            out["error"] = e
        finally:
            queue.put_nowait(sentinel)

    task = asyncio.create_task(_drive())
    try:
        while True:
            item = await queue.get()
            if item is sentinel:
                return
            yield item
    finally:
        # 客户端断开 / 超时 / 停止：把上游真正取消掉再走，否则模型调用会在
        # 后台继续跑到底 —— 那正是"断了还在计费"的成因。
        if not task.done():
            task.cancel()
        # ⚠️ 必须连 `BaseException` 一起吃掉，不能只 suppress Exception。
        # `task.cancel()` 之后 `await task` 抛的 `CancelledError` 是**我们自己刚
        # 发出去的那次取消的回执**，不是"调用方被取消"。它一旦冒出去，就会从
        # `aclose()` 穿回 `_stream_run` 的 finally —— 那里只 catch Exception，
        # 于是整个生成器以 CancelledError 结束：断连（客户端已经走了、这一轮
        # 该结算）被表现成"请求被取消"，收尾的配额结算与幂等键都无法正常走完。
        # 实测：断连那一刻主循环正在等下一段（生成器在运行中），watcher 的
        # aclose 会命中 asyncio 的 "already running" 保护而失效，唯一真正关掉
        # 上游的就是这条 finally ⇒ 每次断连都会走到这里。
        with contextlib.suppress(BaseException):  # noqa: BLE001
            await task


def _review_draft(outcome) -> str:
    """审核台要看的草稿。

    两处来源，按"答复本身优先"取：
      * 正常产出 / 兜底转人工 ⇒ ``outcome.answer``（兜底那类还可能只有
        ``fallback_draft``，那是被护栏拦下的原文）；
      * 模型声明要人审 ⇒ 运行停在 ``deliver`` 工具上，正文还没变成答复，
        它在待批项的参数里。
    """
    if outcome.answer:
        return outcome.answer
    for item in outcome.approvals or ():
        if getattr(item, "tool", "") != "deliver":
            continue
        try:
            payload = json.loads(getattr(item, "arguments", "") or "{}")
        except (TypeError, ValueError):
            continue
        text = payload.get("content") if isinstance(payload, dict) else None
        if isinstance(text, str) and text.strip():
            return text
    return outcome.fallback_draft or ""


async def _hold_for_review(session_id: str, outcome, tenant: str, *, reason: str) -> bool:
    """把这一轮押进"等人看"，返回是否真的停住了。

    两类触发，处置**不同**（这是这套流程里最容易做错的一处）：

    * ``needs_review`` —— 模型在交付那一刻声明要人看，运行停在工具的审批中断上。
      现场（官方 ``RunState`` 的 JSON）存进 run_store，``/v1/review`` 用它接着跑。
    * ``manual`` —— 代码在运行**结束后**判的（跑偏 / 护栏拦下 / 没产出）。
      它**没有现场可存**：运行已经结束，没有任何"可以停下来的地方"可回退。
      处置是入队；``accept`` 直接交付草稿、``revise`` 新起一轮。

    ⚠️ 失败时返回 False 而**不抛错**：产出已经在手里，吞掉比交出去更糟 ——
    用户拿不到东西，审核人也拿不到（队列里没记录）。但必须可观测，
    否则这条降级路径等于静默失效。
    """
    if reason != "needs_review":
        return True
    if not outcome.run_snapshot:
        logging.error("声明了要人审，但运行没有产生现场（thread=%s）—— 无法恢复", session_id)
        metrics.inc("review_gate_failed_total")
        return False
    try:
        ok = await _off(
            run_store.save, session_id,
            snapshot=outcome.run_snapshot,
            draft=_review_draft(outcome),
            reason=reason,
            tenant=tenant or "",
        )
    except Exception as e:  # noqa: BLE001
        logging.error("存运行现场失败（本轮按正常交付处理）：%s", e)
        metrics.inc("review_gate_failed_total")
        return False
    if not ok:
        logging.warning("运行现场没有落库（thread=%s）—— 按正常交付处理", session_id)
        metrics.inc("review_gate_failed_total")
    return bool(ok)


async def _stream_run(turn, req_kind: str, tenant: str = "",
                      subject: str = "", thread_id: str = "", budget=None,
                      disconnect=None, reserved: int = 0, timeout: float = 0,
                      idem_key: str = "", idem_fp: str = "", run_id: str = ""):
    """跑一轮官方 agent 运行，把它的输出转成 SSE 事件。

    turn: 这一轮的输入（``src.turn.TurnRequest``）：用户原话、会话编号、租户、
        人工打回意见、以及"接着跑一次被打断的运行"的现场。**没有**"图输入"这
        种东西了 —— 官方主循环只接受"用户输入"或"RunState"，两者都装在这里。
        ⚠️ 这里**不再收一个"运行配置"字典**（2026-10-20 删）：那是 LangGraph 的
        `configurable` 形状，官方 `Runner.run` 根本不读它，唯一的读者是下面那句
        "兜底取 thread_id" —— 而 `TurnRequest.thread_id` 本来就是必填字段。
        删掉它同时删掉了 tracing 那条"看起来接上了"的假通路（见 `app/tracing.py`）。
    req_kind: 仅用于日志文案（"chat" / "review"），标明是哪个端点触发的流。
    tenant: 租户 id。用于**把本次消耗计入配额**，并写进审核队列。
    subject: 请求主体（用户名）。同时是两件事的桶键，必须与调用方一致：
        · 配额**每人级**的桶 —— 要与 `_sse` 里 `_reserve_quota(tenant, subject, ...)`
          用的是同一个值，不一致就把结算记到别人的桶上了（表现成"某人的额度莫名
          被吃"，且没有任何报错）；
        · 幂等键的命名空间之一（见 `IdempotencyStore._key`）—— `finish` / `forget`
          必须与端点抢键时用同一个 subject，否则键会**永远收不了尾**。
        ⚠️ 原名 `quota_subject`（2026-10-06 改名）：它早已不只用于配额，改名是为了
        让"这里传的就是那个 subject"这件事在调用点上一眼可见 —— 两个用途用同一个
        值正是这条约束的全部内容。
    thread_id: 会话 id。用于把待审记录与通知关联到会话。
    budget: 可选的 RequestBudget。给了且限额 >0 时，**超预算即停止继续生成**
        （已产出的正文照常返回，不是把整次回答作废）。
    disconnect: 可选 `_DisconnectSignal`。给了就**并行等待"客户端已断开"**，
        一旦置位立即停止生成并跳过后续事件 —— 不做这件事的代价是：用户关掉页面后
        模型照样生成到自然结束，**token 照付**。传 None（测试/CLI）时行为不变。
        ⚠️ 它**不能**由这里自己造：信号必须在构造 `EventSourceResponse` 之前
        就存在，才能被 `client_close_handler_callable` 接上（见 `_sse`）。
    idem_key: 已抢占的 `Idempotency-Key`（由 `_idempotency_gate` 判定为首次请求）。
        成功后把**登记下来的事件序列**交给 `_IDEMPOTENCY.finish`，同键重试据此
        重放（标准 2.6）；失败或未完成时撤销键，让客户端能用同一个键重试。
        **空串 = 不做任何去重**（客户端没带键 / 能力被关 / 无需重放）。
    idem_fp: 该键对应的**请求指纹**（标准 2.4）。收尾时要连它一起写回 ——
        少了它，重放命中就无从判断"这次重试的载荷与上次是否同一个"。
    reserved: `_preflight` 里预占的配额量。请求结束时由 `settle` 把它换成实际消耗；
        不传（0）时退化成"只加实际值"，即预占不会被退。
    timeout: **请求级总时限**（秒），0 = 不限（默认，行为与改造前一致）。
        约束的是"等下一段正文"这一步 —— 也就是会随上游变慢而失控的那部分。
        超时后下发 `timeout` 事件并**收流**（已产出的正文照常交付）。
    run_id: 这次运行的登记编号。非空 ⇒ 可以被 `POST /v1/sessions/{id}/stop` 停下，
        停的时候下发 `stopped` 事件并收流；空串（测试/CLI/未登记）⇒ 完全不可停，
        行为与改造前一致。**必须由调用方生成并在 `_RUNS.begin` 里登记过** ——
        这里只用它查状态与收尾，不负责登记（登记要在生成器创建之前完成，
        否则"刚启动的那一瞬间"是停不了的）。

    事件类型：
      token            —— 逐段生成的正文
      citations        —— **可回溯的引用**（来源/页码/章节/保真原文节选/表格网格/图片路径）
      need_review      —— 需人工审核（附草稿、评分、意见）；同时入队 + 发通知
      budget_exceeded  —— 单请求 token 预算耗尽，生成被提前终止
      timeout          —— **请求级总时限耗尽**，流被终止（终止性事件，其后没有 done）
      stopped          —— **用户主动停下**，流被终止（终止性事件，其后没有 done）
      done / error

    citations 放在 token 之后、done 之前下发：它描述的是"上面这段回答依据了什么"，
    顺序上必须在正文之后才有意义；同时它带着 chunk_id 与页码/坐标，
    用户点开就能回到原文件核对，而不是只能相信模型。
    """
    # ---- 历史压缩（入口一·会话侧）：起个**后台**任务，不挡本轮 ----
    # 位置在函数体最开头：本函数是异步生成器，函数体要等第一次迭代才执行，而
    # `_sse` 创建它之后**立即** `async for` —— 所以到这一行事件循环一定在。
    _spawn_history_compaction(thread_id)
    # 运行登记放在**生成器体内**，而不是端点里：端点先登记的话，配额 429 会让
    # 请求在生成器创建之前就结束，那一行登记永远没人清 —— 于是这个 thread
    # 之后每一次 `/stop` 都会打到一条早已不存在的运行上。
    # 放这里还顺带保证了"登记成功 ⇒ 确实开始跑了"。
    if run_id:
        await _off(_RUNS.begin, run_id, thread_id, tenant)
    # 这一轮的运行上下文（租户、语料目录、检索器工厂）。一次请求一份 ——
    # 不再像图那样"每个节点各建一份、再互相串引用"，因为一次请求**就是**一次运行。
    context = build_context(tenant=turn.tenant)
    budget = budget if budget is not None else RequestBudget(0)
    deadline = _RequestDeadline(timeout)
    produced: list[str] = []
    #: 预算是否被耗尽。耗尽时**不再补发整段产出** —— 那会把刚被判定为"超预算、
    #: 停止生成"的正文原样发出去，等于预算闸只在流式路径上生效、在整段返回的
    #: 路径上失效。
    starved = False
    # chunk 间延迟（TPOT）的游标：上一个**已交付** chunk 的到达时刻，以及它属于
    # 哪一次模型调用。判据必须是"同一次调用"而不能只是"同一个节点" —— 同一个
    # agent 节点里完全可能调用两次模型，中间隔着一次工具执行（几百毫秒到几十秒），
    # 那段等待**不是解码耗时**，混进来会把 TPOT 的 P95 直接抬到秒级。
    # 实测（`tmp_probe/probe_chunk_identity.py`）：同一个节点内接连两次调用，
    # chunk 的 `id` 不同；同一次调用内的各 chunk 共享同一个 `id`。
    last_chunk_at = 0.0
    last_call = None
    # TTFT（首字节延迟）的起点。为什么落在函数体内而不是更外面：本函数是
    # async generator，函数体要等第一次迭代才执行 —— 而 `_sse` 创建它之后
    # **立即** `async for`，两者之间只隔一次事件循环调度（微秒级，可忽略）。
    # 再往前就够不着了：`_preflight` 在构造生成器之前，拿不到它的时刻。
    started = time.perf_counter()
    stream = None
    watcher = None
    # 停止标志：``threading.Event`` 而不是 ``asyncio.Event``，因为置位它的是
    # ``_watch_stop`` 里经线程池返回的结果（跨线程），而读它的既有事件循环里的
    # 主循环、也有它在 finally 里的收尾。threading.Event 两者都安全。
    stop = threading.Event() if run_id else None
    stop_watcher = None
    # `state` 用 dict 跨任务共享（生成器写 stream、watcher 读取后关闭）。
    # 断连标志不在这个 dict 里 —— 它在 `disconnect` 上，那个值由库的回调写。
    state = {"stream": None}
    # 本次请求是否**成功完成**。它决定幂等键的收尾方式（标记完成 / 撤销），
    # 而这件事只在收尾那一刻才知道 —— 所以必须在 try 之外可见。
    succeeded = False
    #: 这一轮的结果与异常。由 `_turn_events` 在流结束时写入 —— 异步生成器没法
    #: 返回值，所以用这个字典把 `TaskOutcome` 带回主循环。
    outcome_box: dict = {}
    # ---- 幂等重放的登记表 ----
    # 只在下发**之前**登记每个事件，且只在带了键时登记（不带键的请求零开销）。
    # 为什么登记"事件"而不是"最终答案"：见 `_replay_from_idem_payload`。
    frames: list[list] = []
    frame_bytes = 0
    truncated = False

    def _record(event: dict) -> dict:
        """登记一个即将下发的事件（供同键重试重放），并原样返回它。"""
        nonlocal frame_bytes, truncated
        if not idem_key or truncated:
            return event
        data = event.get("data")
        size = len(event.get("event", "")) + (len(data) if isinstance(data, str) else 0)
        if frame_bytes + size > config.IDEMPOTENCY_MAX_BODY_BYTES:
            # 上限一到就**整轮停记**，不续记也不截断：记一半的事件序列比不记更糟,
            # 重放出来会是一个残缺的流（客户端以为生成到一半就结束了）。
            truncated = True
            logging.warning(
                "幂等重放正文超过 %d 字节，本轮不缓存（同键重试将回 409 指路）",
                config.IDEMPOTENCY_MAX_BODY_BYTES)
            return event
        frame_bytes += size
        frames.append([event.get("event", ""), data] if data is not None
                      else [event.get("event", "")])
        return event

    try:
        if disconnect is not None:
            watcher = asyncio.create_task(
                _watch_disconnect(disconnect, produced, state))
        if stop is not None:
            stop_watcher = asyncio.create_task(
                _watch_stop(run_id, stop, produced, state))
        stream = _turn_events(turn, context, outcome_box)
        state["stream"] = stream
        # 手工迭代（而不是 `async for`）：只有显式拿到 `__anext__`，才能把**每一次
        # 等待**都交给 `deadline.run` —— 而"上游一直不返回"正是唯一需要总时限的
        # 场景，那种情况下 `async for` 会永久挂住、循环体一次都不执行（见
        # `_RequestDeadline` 的说明）。`timeout=0` 时退化成普通 await，无额外开销。
        anext = stream.__anext__
        while True:
            # 断连 / 停止信号已置位：停止消费（这里只是兜底 —— 真实链路上库会同时
            # 取消本生成器，取消会带着 CancelledError 打进下一次挂起点）
            if disconnect is not None and disconnect.closed:
                break
            if stop is not None and stop.is_set():
                break
            try:
                text, call_id = await deadline.run(anext)
            except StopAsyncIteration:
                break
            except TimeoutError:
                # 总时限耗尽：**终止性**返回（不查状态、不补 done）—— 收尾那几步
                # 同样是 IO（写待审队列、结算配额），继续等下去等于没有时限。
                yield _record(_timeout_event(req_kind, deadline, produced))
                return
            # 到达时刻在**任何处理之前**取：后面还要判预算、拼事件，那些开销
            # 会随实现变化，而 TTFT / TPOT 量的都该是"用户等了多久"。
            arrived = time.perf_counter()
            if text:
                if not budget.add(text):
                    # 预算耗尽：立刻停止继续生成（而不是等模型自己收尾）
                    metrics.inc("llm_budget_exceeded_total")
                    logging.warning("单请求 token 预算耗尽（已用 %d/%d），终止生成",
                                    budget.used, budget.limit)
                    yield _record(_sse_json("budget_exceeded",
                                            {"used": budget.used, "limit": budget.limit}))
                    starved = True
                    break
                # "这一次模型调用"的标记：官方 `ResponseTextDeltaEvent.item_id`。
                # 同一个 output item 内的各 delta 共享它，换一次模型调用就变
                # （中间那次工具执行的时间**不是**解码耗时，要能剔除）。
                call = call_id
                if not produced:
                    # 首个 token = TTFT。放在预算判定**之后**：预算被顶掉时
                    # 用户一个字的正文都没看到，把它算成一次首字节会系统性
                    # 压低 TTFT（最需要它的场景反而被抹平）。
                    metrics.observe("llm_first_token_seconds",
                                    arrived - started,
                                    kind=req_kind, mode="stream")
                elif last_chunk_at and call == last_call:
                    # 第二个及以后的 chunk：记"距上一个 chunk 到达多久"。
                    # 口径同 OpenTelemetry 的 `gen_ai.client.inference.
                    # time_per_output_chunk`（每个首 chunk 之后的 chunk 记一次，
                    # 量的是与上一个 chunk 的间隔），而不是"总时长 ÷ token 数"——
                    # 后者会把一次卡顿平摊到所有 token 上，正好把要抓的东西抹平。
                    #
                    # 换了一次模型调用就**不记**：那中间是工具执行 / 节点调度。
                    metrics.observe("llm_inter_chunk_seconds",
                                    arrived - last_chunk_at, kind=req_kind)
                last_chunk_at, last_call = arrived, call
                produced.append(text)
                yield _record({"event": "token", "data": text})

        if disconnect is not None and disconnect.closed:
            # 客户端已经走了：不必再查状态、不必入队、更不必下发事件（没人收）。
            # 直接返回，finally 里的计费照常执行。
            return

        if stop is not None and stop.is_set():
            # 用户主动停下：**终止性**返回。不查状态、不入队、不补 done ——
            # 那三件事都属于"这一轮要正常收尾"，而停止的含义正好是不收尾。
            # ⚠️ 这里**没有**可恢复的现场：运行是被取消的，官方不会为取消产出
            # `RunState`（只对 interruption 产）。所以 `stopped` 事件里
            # `resumable` 恒为 False，客户端不该把它当成"稍后能接着跑"。
            yield _record(_stopped_event(req_kind, produced))
            return

        # ---- 这一轮的结论 ----
        # `_turn_events` 已经把结果（或异常）放进来了：流结束 ⇒ 那次运行也结束了。
        if "error" in outcome_box:
            # 运行本身失败：交给外层 except 统一处理（记指标 + 下发脱敏错误）。
            # ⚠️ 不能在这里"降级成一句友好话术" —— 那会把"没跑成"伪装成"跑成了"，
            # 观测面与客户端重试逻辑一起失真。
            raise outcome_box["error"]
        outcome = outcome_box.get("outcome")
        if outcome is None:
            raise RuntimeError("运行结束但没有结果")

        # 会话编号：按"离调用方最近"的顺序兜底。
        # ⚠️ 参数优先、`turn.thread_id` 兜底：前者是给"流式收尾也要知道是哪条会话"
        # 的调用方另外传的，后者是这一轮**必然**带着的那个 id。只看参数的话，一旦
        # 有调用方忘了传，待审记录会落到空串上 —— 所有会话挤进同一条记录（互相
        # 覆盖），而且交付前那一步会因为 `session_id` 为空**直接跳过**，表现成
        # "该转人工的轮次悄悄变成了正常交付"。两种症状都不报错。
        #
        # ⚠️ 2026-10-20：这里原来还有一级"从运行配置的 `configurable.thread_id`
        # 兜底"，随 LangGraph 的 config 形状一起删掉了 —— 官方 `Runner.run` 不认
        # 那个键，而它上面的两级已经覆盖全部调用方。
        session_id = thread_id or getattr(turn, "thread_id", "") or ""

        # ---- 本轮要不要人审？----
        # ⚠️ 必须在**补齐产出之前**判。要审的内容**不是给用户的** —— 顺序反了的话，
        # 待审草稿会先以 token 事件发给用户、然后才收到"待审核"，于是"不交半成品"
        # 只写在文档里、实际照样漏出去。
        review_requested = bool(outcome.review_reason)
        awaiting_review = False
        if review_requested and session_id:
            # ⚠️ 这一步同样要在总时限之内：它是一次**落库**（`run_store` 写 PG /
            # `_REVIEW_QUEUE.add` 写表），库端卡住时会无限等 —— 而用户已经拿到正文，
            # 只会觉得"怎么还没结束"。收尾的 IO 不比正文的等待更能超时。
            try:
                awaiting_review = await deadline.run(
                    lambda: _hold_for_review(
                        session_id, outcome, tenant, reason=outcome.review_reason))
            except TimeoutError:
                yield _record(_timeout_event(req_kind, deadline, produced))
                return
        # 审核台要看的草稿：正常产出时就是答复本身；`needs_review` 那一轮运行停在
        # 交付工具上（答复还没产生），正文在待批项的参数里。
        draft = _review_draft(outcome)

        # ---- 非流式链路的产出补齐 ----
        # agent 链路用 `Runner.run` 整段返回，**不产生 token 事件**。
        # 不补这一步，客户端会收到"零 token + done" —— 表现得像服务没回答，
        # 而服务日志里一切正常（最容易误判成前端 bug 的一种）。
        # 放在这里而不是各链路各写一遍：判据统一是"本轮一个 token 都没流过"。
        if not produced and not awaiting_review and not starved and outcome.answer:
            text = outcome.answer
            # 整段返回（`on_text` 为 None 的路径 / 模型一个 token 都没流式发出）：
            # 它的"首字节"就是**整段生成耗时**，与流式差一个数量级。两者混进同一个
            # 分布会让 P95 变成一个没有含义的中间值，所以用 mode 标签分开看。
            metrics.observe("llm_first_token_seconds",
                            time.perf_counter() - started,
                            kind=req_kind, mode="buffered")
            produced.append(text)
            yield _record({"event": "token", "data": text})

        # ---- agent 链路的运行指标 ----
        agent_stats = context.stats.as_dict()
        guardrail = outcome.guardrail or ""
        if agent_stats or guardrail:
            _record_agent_metrics(agent_stats, guardrail)

        # ---- 在线答案质量采样（默认关，见 config.ONLINE_QUALITY_SAMPLE）----
        # 判据统一是"本轮最终产出是什么"与"本轮真实检索到了什么"。
        _maybe_sample_quality(outcome.answer, context.citations_text)

        cites = outcome.citations or []
        check = outcome.citation_check or {}
        blocked = False
        if check:
            # status 是**有限取值**（ok/weak/degraded/unsupported/no_citations/no_answer），
            # 适合做标签；绝不要把 detail 或引用来源放进标签（高基数）。
            metrics.inc("citation_check_total",
                        status=str(check.get("status") or "unknown"))
            if check.get("block_worthy"):
                metrics.inc("citation_check_unsupported_total")
            blocked = _should_withhold_citations(check)
        if cites and not blocked:
            yield _record(_sse_json("citations", cites))
        elif blocked:
            logging.warning("引用校验未通过（%s），按策略 block：不下发引用",
                            check.get("detail"))
        if check:
            # 校验结论**总要下发**（含 block 的那次）：客户端需要知道
            # "这段回答的引用为什么不见了"，否则"没有引用"与"引用被拦了"分不开。
            yield _record(_sse_json("citation_check", _public_citation_check(check)))

        # ---- 出口护栏拦下整条产出：必须给用户一个**明确的**结局 ----
        # agent 链路的产出是整段返回的（不流式），所以护栏能在交付前把它拦掉 ——
        # 被拒的输出 `output` 留空。此时若只发 done，用户看到的是"空回答"，
        # 与"模型什么都没说"分不开；这里补一条可见的解释。
        if guardrail and not outcome.answer:
            logging.warning("agent 出口护栏拦下本次交付：%s", guardrail)
            yield _record(_sse_json("guardrail_blocked", {
                "guardrail": guardrail,
                # 只给有限取值的"原因档位"，不给护栏的内部 output_info
                # （里面有引用编号列表、重叠率等内部判据，属于实现细节）。
                "reason": "citation_or_size",
                "message": "本次产出未通过交付前校验（引用无法溯源或超出长度上限），已拦下。",
            }))

        # ---- 兜底结局（第 5 步）：结构化字段，**不进答复正文、也不进对话历史** ----
        # 正文原样交付（有产出就交，不加任何"部分结果"的措辞），完整清单在这里给前端；
        # failed / manual 也报一次 —— 否则"这一轮为什么没有正文"只能靠翻日志回答。
        # ⚠️ delivered **不发**：它是"没出问题"，发了会让客户端多一种永远不该出现的分支。
        verdict = outcome.fallback
        outcome_kind = (verdict.outcome if verdict is not None else "") or ""
        if outcome_kind and outcome_kind != "delivered":
            yield _record(_sse_json("task_outcome", {
                "outcome": outcome_kind,
                "reason": (verdict.reason if verdict is not None else "") or "",
                "unfinished": list(verdict.unfinished) if verdict is not None else [],
            }))

        if awaiting_review:
            item = {
                "thread_id": session_id,
                "draft": draft,
                # agent 链路不产 0-100 分 ⇒ None。**不能给 0**：审核台上 0 分是
                # "AI 判了 0 分"，而真相是"这条链路根本不产分"。
                "score": None,
                # 评审结论不再进这个字段（它属于"对内容的建议"，现在只交给模型自己看）。
                "feedback": "",
                "tenant": tenant or "",
            }
            # 落队列 + 异步通知：即使此刻没有客户端在线，待审内容也不会"消失"。
            # ⚠️ 挪到工作线程：`add` 是一次 **PG 写**，而 PG 这条路径只有
            # connect_timeout、**没有 statement_timeout** ⇒ 库端一旦卡住就是
            # 无限等待，直接冻住整个事件循环（所有并发请求一起停转）。
            await _off(_REVIEW_QUEUE.add, **item)
            metrics.inc("review_needed_total")
            yield _record(_sse_json("need_review", {
                "draft": draft,
                "score": None,
                "feedback": "",
            }))
        else:
            yield _record({"event": "done"})
        # 走到这里就算成功（无论是正常收尾还是停在审核态）——
        # 两种情况下这次请求都已经**产生了真实消耗与状态**，重试都不该重跑。
        succeeded = True
    except Exception as e:  # noqa: BLE001
        if stop is not None and stop.is_set():
            # 停下时上游是被 `aclose()` 打断的，冒出来的形态很杂（StopAsyncIteration /
            # RuntimeError("already running") / 上游自己的取消异常）。它们全都是
            # **预期内**的：当成流故障记会污染错误率，正好掩盖真实故障。
            yield _record(_stopped_event(req_kind, produced))
            return
        if disconnect is not None and disconnect.closed:
            # 客户端已走：上游被取消是**预期行为**而非真故障；而且此时也没人收
            # error 事件。若照旧记成流异常，会污染错误率指标、掩盖真实故障。
            logging.info("%s 流因客户端断开而中止", req_kind)
        else:
            metrics.inc("chat_stream_errors_total", kind=req_kind)
            # 完整异常（含堆栈）只进日志；给客户端的是脱敏文案，见 _client_error
            logging.exception("%s 流异常", req_kind)
            yield _record({"event": "error", "data": _client_error(e)})
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

        if stop_watcher is not None:
            stop_watcher.cancel()
            try:
                await stop_watcher
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logging.debug("等待停止监听退出时异常", exc_info=True)

        # 登记行必须**无论成败**都删掉：留下一行会让这个 thread 下一次运行
        # "看起来已经在跑"，而 `/stop` 会打到那次新运行上（误停下一轮）。
        if run_id:
            try:
                await _off(_RUNS.finish, run_id)
            except Exception:  # noqa: BLE001  清理失败不该覆盖真实结果
                logging.debug("清理运行登记失败：run=%s", run_id, exc_info=True)

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
        #
        # ⚠️ 这里在 `finally` 里 `await`，安全性是**实测过的**（不是想当然）：
        # 取消（客户端断连）在 `try` 的挂起点就已经投递完毕，进入 `finally` 时
        # 该任务已不再是"待取消"状态，后续 await 会正常执行完
        # （探针：tmp_probe/probe_finally_await_on_cancel.py，三种写法均执行了收尾）。
        # 这正是"把同步调用改成 await 不会丢记账"的依据 —— 少了这条证据，
        # 这个改动就是在拿计费准确性赌博。
        used = estimate_tokens("".join(produced))
        await _off(_QUOTA.settle, tenant, reserved, used, subject=subject)
        if used:
            # 刻意不把 tenant 当标签：租户数可能成百上千，会让指标基数爆炸
            metrics.inc("llm_tokens_total", amount=used)
            metrics.observe("llm_tokens_per_request", used)

        # 幂等键收尾。**只有成功的请求才占用键**：
        #   - 成功 → 标记 DONE 并留下登记的事件序列，此后同键同载荷的重试
        #     直接**重放**这一段（标准 2.6），不再花一次 token；
        #   - 失败/断开 → 撤销键，客户端可以用**同一个键**重试。
        # 若不分这两种情况（例如一律标记 DONE），一次偶发失败会让那个键在 TTL 内
        # 彻底不可用 —— 客户端的重试逻辑完全正确，却始终收到 409。
        #
        # ⚠️ `finish` 返回的是"正文**真的**缓存下来了吗"，不是"写没写成功"。
        # 超上限、条目已过期、Redis 抖动，三种情况都会让它返回 False，
        # 而三种情况下同键重试的行为都是"409 指路"而不是"重放" ——
        # 这是**对客户端可见的差异**，所以必须计出来。
        # ⚠️ 收尾必须带**抢键时用的那个 subject**（见 `IdempotencyStore._key`）：
        # 少了它，finish/forget 会落到"只带租户"的历史命名空间，而键其实登记在
        # "租户+主体"里 ⇒ 标记不上、也撤销不掉 ⇒ 键停在"在途"直到 TTL，
        # 客户端拿同一个键重试永远是 409"正在处理中"（而且**没有任何日志**）。
        if idem_key:
            if succeeded:
                if not await _off(_IDEMPOTENCY.finish, tenant, idem_key, idem_fp,
                                  _render_idem_payload(frames, truncated),
                                  subject=subject):
                    metrics.inc("idempotency_truncated_total")
            else:
                await _off(_IDEMPOTENCY.forget, tenant, idem_key, subject=subject)


# ---------------------------------------------------------------------------
# 幂等闸门（IETF `Idempotency-Key` 草案，规范见 deploy/IDEMPOTENCY.md）
# ---------------------------------------------------------------------------
# 为什么单独抽一层，而不是塞进 `_preflight`
# ----------------------------------------
# `_preflight` 的三道闸门（限流/配额/归属）是**幂等**的：重复执行没有额外代价。
# 而幂等闸门第一次执行就**改变了状态**（抢占键）—— 放在 `_preflight` 里，
# 后面任何一个 4xx（审核态不对、action 非法）都会让请求带着一个已抢占的键退出，
# 于是"客户端重试"被自己的 409 挡住，而它什么都没做错。
# 所以顺序固定为：**业务 4xx 全部判完 → 抢键 → 配额 → 开流**。
#
# 抢到的键由谁收尾：`_stream_run` 的 finally（成功→finish，失败→forget）。
# 唯一"抢了却进不了生成器"的缺口是配额 429 —— 那一步在 `_sse` 里显式退还。
class _IdemGate(NamedTuple):
    """闸门结论：`response` 非空表示"就此返回"，否则用 `key`/`fp` 开流。

    `subject` 是**抢键时的那个主体**（用户名），必须一路带到收尾（`_release_idem_gate`
    与 `_stream_run`）—— 幂等键的命名空间是 (tenant, subject, key)，
    收尾时漏掉 subject 就会去改另一个空间里的键，原键永远收不了尾。
    收在闸门里而不是让调用方各自再传一遍：调用方已经拿到了 gate，
    多一个"记得也把 subject 传下去"的手工约定，就多一个静默漏传的机会。
    """

    response: "JSONResponse | EventSourceResponse | None"
    key: str = ""
    fp: str = ""
    subject: str = ""


def _idempotency_problem(status: int, title: str, detail: str) -> JSONResponse:
    """按 RFC 9457 回一个 problem+json。

    用 problem+json 而不是项目其余地方的 `{"detail": ...}`：草案 2.7 的示例
    就是这个媒体类型，且 400/422 要求"响应体里带一个指向文档的链接" ——
    `type` 与 `Link` 都是 problem 文档的既有位置，不必自创字段。
    """
    doc = config.IDEMPOTENCY_DOC_URL
    body = {
        "type": doc or "about:blank",
        "title": title,
        "status": status,
        "detail": detail + (f"（规范见 {doc}）" if doc else ""),
    }
    headers = {"Link": f'<{doc}>; rel="describedby"'} if doc else None
    return JSONResponse(body, status_code=status,
                        media_type="application/problem+json", headers=headers)


def _render_idem_payload(frames: list, truncated: bool) -> str | None:
    """把登记下来的事件序列成可重放的正文；`truncated` 时返回 None。"""
    if truncated:
        return None
    return json.dumps({"pv": PROMPT_VERSION, "frames": frames},
                      ensure_ascii=False, separators=(",", ":"))


def _replay_from_idem_payload(payload: str | None) -> "EventSourceResponse | None":
    """把缓存的事件序列**原样再放一遍**（标准 2.6：返回上一次已完成的结果）。

    为什么是"重放事件"而不是"把最终答案拼一条 token 发出去"：客户端拿到的
    必须是**与首次同构的流** —— 包括 citations / citation_check / need_review
    这些事件，以及 token 的分段边界。只补一条总 token 的话，前端的分段渲染、
    引用面板、审核弹窗全都会缺一块。

    解析失败返回 None ⇒ 调用方退回 409 指路（有日志，不是静默）。
    """
    if not payload:
        return None
    try:
        rec = json.loads(payload)
        frames = rec["frames"]
    except (TypeError, ValueError, KeyError):
        logging.warning("幂等重放正文无法解析，退回 409 指路")
        return None
    pv = rec.get("pv") or PROMPT_VERSION

    async def _iter():
        for frame in frames:
            event = frame[0]
            data = frame[1] if len(frame) > 1 else None
            yield {"event": event, "data": data} if data is not None else {"event": event}

    # 响应头标出来是重放：客户端据此知道"没有新生成"，运维/网关日志据此
    # 区分"这次请求真的花了 token"与"只是把上次的结果又发了一遍"。
    return EventSourceResponse(_iter(), headers={"X-Prompt-Version": pv,
                                                 "Idempotency-Replayed": "true"})


def _replay_json_from_idem_payload(payload: str | None) -> "JSONResponse | None":
    """把上一次的 **JSON 响应**原样再回一遍（非流式端点的重放形态）。

    为什么不能复用上面那个 SSE 重放器：它的正文是一串 ``(事件名, 数据)`` 帧，
    而这里要回的是一个普通 JSON 对象。**要守住的是同一条语义**（标准 2.6：
    已完成的重试返回上一次的结果），只是"结果长什么样"由端点决定。

    ⚠️ 重放出来的 ``seq`` 是**上一次那个号**，不是新号 —— 这正是对的：它代表
    "同一次逻辑写入"。若这里重新生成一个号，客户端会以为又写了一条。
    """
    if not payload:
        return None
    try:
        body = json.loads(payload)
    except (TypeError, ValueError):
        logging.warning("幂等重放正文无法解析，退回 409 指路")
        return None
    return JSONResponse(body, headers={"Idempotency-Replayed": "true"})


def _idempotency_gate(tenant: str, req_kind: str, raw_key: str, payload,
                      subject: str = "", replay=None) -> _IdemGate:
    """按草案 2.6/2.7 判定这次请求该怎么走。

    payload: 参与**请求指纹**（标准 2.4）的载荷。重试必须同键同载荷；
    同键不同载荷回 422 —— 没有这一条，"客户端拿同一个键问了两个不同的问题"
    会被静默当成重复拒掉（功能损坏，且客户端无从判断自己错在哪）。
    subject: 请求主体（用户名）。它是幂等键命名空间的一部分（见
        `IdempotencyStore._key`）——**不传的话同租户成员会互相干扰**：
        猜中别人的键就能按住别人的请求，或在对方完成后拿到对方那次回答的重放。
        ⚠️ 默认空串只是为了让单测/内部调用不必改造；三个 SSE 端点都必须显式传
        `ident.user`，否则这条隔离等于没做（而且是**静默**没做）。
    replay: "把已完成的正文还原成响应"那个渲染器。默认按 SSE 事件的形态还原
        （`_replay_from_idem_payload`）；非流式端点传自己的那一个。
        ⚠️ 这里只换渲染器、**不换状态机** —— 四种状态（NEW / DONE /
        IN_PROGRESS / MISMATCH）与对应状态码对所有端点必须完全一致，否则
        `deploy/IDEMPOTENCY.md` 那张判定表就得按端点分叉，客户端没法照一份文档写。
    """
    if not config.IDEMPOTENCY_ENABLED:
        return _IdemGate(None)
    try:
        key = idem_normalize_key(raw_key)
    except InvalidKey as e:
        metrics.inc("idempotency_rejected_total", reason="invalid")
        return _IdemGate(_idempotency_problem(
            400, "Idempotency-Key 格式非法",
            f"该键不符合本项目公布的格式：{e}"))
    if not key:
        if not config.IDEMPOTENCY_REQUIRE_KEY:
            # 没带键 = 不要求去重（草案 2.5.1：带不带是客户端的自由）。
            # 行为与"这个能力没上"完全一致 —— 本项目默认值原则②。
            return _IdemGate(None)
        metrics.inc("idempotency_rejected_total", reason="missing")
        return _IdemGate(_idempotency_problem(
            400, "缺少 Idempotency-Key",
            "本服务把该请求头声明为必需（IDEMPOTENCY_REQUIRE_KEY=1）。"))

    fp = idem_fingerprint({"op": req_kind, "body": payload})
    result = _IDEMPOTENCY.begin(tenant, key, fp, subject=subject)
    if result.status == IDEM_DONE:
        replayed = (replay or _replay_from_idem_payload)(result.payload)
        if replayed is not None:
            metrics.inc("idempotency_replay_total")
            return _IdemGate(replayed)
        # 已完成、但正文没能缓存下来（超上限/条目过期/写入失败）——
        # 这是**对客户端可见的行为差异**（重放 vs 指路），必须能被看见。
        metrics.inc("idempotency_conflict_total", reason="not_replayable")
        return _IdemGate(_idempotency_problem(
            409, "该 Idempotency-Key 已完成，但响应不可重放",
            "上一次请求已完成；其响应超出本服务的重放缓存上限。"
            "请改用 GET /v1/sessions/{thread_id} 查看结果。"))
    if result.status == IDEM_IN_PROGRESS:
        metrics.inc("idempotency_conflict_total", reason="in_flight")
        return _IdemGate(_idempotency_problem(
            409, "该 Idempotency-Key 正在处理中",
            "上一次同键请求尚未结束；请在原请求结束后重试。"))
    if result.status == IDEM_MISMATCH:
        metrics.inc("idempotency_mismatch_total")
        return _IdemGate(_idempotency_problem(
            422, "Idempotency-Key 已被用于不同的请求载荷",
            "同一个键不能跨不同载荷复用（草案 2.2 的 MUST NOT）。"
            "若确实是另一次请求，请换一个新的键。"))
    return _IdemGate(None, key, fp, subject)


def _release_idem_gate(gate: _IdemGate, tenant: str) -> None:
    """业务校验没过时，把已经占住的幂等键**还回去**。

    为什么必须有这一步：`_IDEMPOTENCY.begin()` 是**先占后判**（原子抢占，见
    `app/idempotency.py`）。闸门一旦通过，这个键就被登记成"这次请求正在处理"。
    如果紧接着的业务校验把它挡掉了（`/review` 与 `/chat/resume` 的 409），
    键却留在登记表里 ⇒ 客户端拿**同一个键**重试时会撞上
    `409 该 Idempotency-Key 正在处理中` —— 而实际上**没有任何东西在处理它**。
    这个假死会持续到 TTL（默认 10 分钟）为止，而且报错信息（"正在处理中"）
    会把排障引向"是不是有别的请求卡住了"，方向完全错。

    只对**没有返回响应**的闸门做事：`gate.response` 非空表示这次请求已经
    按幂等规则给出了结论（重放 / 冲突 / 格式错），那不属于"占住又放弃"。

    ⚠️ 用 `gate.subject` 而不是调用方另传一个：命名空间是 (tenant, subject, key)，
    退错命名空间等于**没退**（原键继续停在"在途"，而这正是这个函数要消灭的假死）。
    """
    if gate.key:
        _IDEMPOTENCY.forget(tenant, gate.key, subject=gate.subject)


# ---------------------------------------------------------------------------
# SSE 响应构造：所有"生命周期必须成对"的东西都收在这里
# （断连信号、配额预占→结算）
# ---------------------------------------------------------------------------
def _sse(turn, req_kind: str, *, tenant: str, subject: str = "",
         thread_id: str, budget=None, idem_key: str = "", idem_fp: str = "",
         reserved=None, run_id: str = "") -> EventSourceResponse:
    """构造 SSE 响应：断连信号成对接线 + 配额预占（结算交给生成器）。

    为什么必须成对构造（而不是各端点自己拼）
    ----------------------------------------
    断连信号有两端，缺一不可：
      - **写端**在 sse_starlette 里 —— 只有 `client_close_handler_callable` 能拿到
        真正的 `http.disconnect`（库自己常驻消费 `receive()`，外部读不到）；
      - **读端**在 `_stream_run` 里 —— 生成器与 watcher 据此收手。
    分散在三个端点里手工接线，就要在 6 处保持一致；而漏接任一端**不会报错**，
    只表现为"线上断了却既不计数、也不减损"（这正是修复前线上发生的事）。
    收进一个函数后，接错只可能错一次。

    配额预占为什么也放在这里
    ----------------------
    **预占与结算必须落在同一条"一定会走到 finally"的路径上**。预占若放在
    `_preflight`（各端点业务校验之前），`/review` 与 `/resume` 的 409 会让请求
    在进入生成前就返回，而退还只发生在 `_stream_run` 的 finally ⇒ **额度永久
    泄漏**（表现成"配额莫名少了一截"，且没有任何报错）。

    调换顺序的代价是配额排在业务校验之后，但换来两个好处：① 不会为注定 409 的
    请求预占额度；② 预占一旦成功，创建生成器之后的每一步都有 finally 兜底。
    安全性不受影响 —— 跨租户防护由 `_preflight` 的会话归属校验负责，那一步仍然
    排在业务校验**之前**。

    ⚠️ 传给库的回调必须是 async 且不阻塞 —— 理由见 `_DisconnectSignal` 的文档。

    turn: 这一轮的输入（``src.turn.TurnRequest``）：用户原话、会话编号、租户、
        打回意见、以及"接着跑一次被打断的运行"的现场。
    req_kind: "chat" / "review" / "resume"，用于日志与指标标签。
    tenant / subject: 配额计量的两级"算在谁头上"，同时是幂等键命名空间的一部分
        （见 `IdempotencyStore._key`）。**subject 必须跟着 `reserved` 一起传**：
        `reserved` 是 `_reserve_quota(tenant, subject, ...)` 算出来的，settle
        用另一个 subject 就会把结算记到别的桶上（额度偏松或偏紧，且无任何报错）。
        默认空串只对"调用方自己已经预占过"的场景安全 —— 那时 `reserved` 由外部传入。
    thread_id / budget: 透传给 `_stream_run`（入队、预算止血）。
    idem_key / idem_fp: 已抢占的幂等键与其请求指纹，透传给 `_stream_run` 收尾。
    run_id: 已在 `_RUNS.begin` 登记过的运行编号。空串 = 这次运行不可被主动停止。

    请求级总时限**在这里读配置**（而不是在各端点）：三个端点都要它，而 `_sse`
    已经是它们共同经过的唯一一处。调用时读（而非模块级常量）才能被测试
    monkeypatch —— 与字段闸门同一个理由（见 `_bounded`）。
    """
    # 配额预占必须**紧贴**生成器创建：成功之后，结算就由 `_stream_run` 的
    # finally 保证；失败（429）则当场抛出、不留残值。
    #
    # ⚠️ 幂等键必须先**退还**再抛：调用方已经抢到了键（状态 = 在途），而退还只
    # 发生在 `_stream_run` 的 finally —— 429 一抛，生成器根本没被创建，
    # 那个键会一直停在"在途"直到 TTL 到期。表现为：客户端拿到 429、等额度
    # 恢复后拿**同一个键**重试，却收到 409"正在处理中" —— 它什么都没做错。
    #
    # ⚠️ `reserved` 可由调用方**预先算好**再传进来（`None` = 就地预占）。
    # 为什么需要这个口子：`/v1/review` 与 `/v1/chat/resume` 必须保持 async（要流式
    # 产出），而配额预占是同步 Redis 调用 —— 就地执行会阻塞事件循环。
    # 这两个端点因此用 `await _off(_reserve_quota, ...)` 先预占、再传进来；
    # `def` 端点（已在工作线程里）与测试则走默认的 `None`，行为与改造前一致。
    if reserved is None:
        reserved = _reserve_quota(tenant, subject, idem_key)
    signal = _DisconnectSignal(req_kind)
    return EventSourceResponse(
        _stream_run(turn, req_kind, tenant=tenant,
                    subject=subject,
                    thread_id=thread_id, budget=budget, disconnect=signal,
                    reserved=reserved,
                    timeout=config.REQUEST_TIMEOUT_SECONDS,
                    idem_key=idem_key, idem_fp=idem_fp, run_id=run_id),
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


# ---------------------------------------------------------------------------
# 用户认证端点（第二档）
# ---------------------------------------------------------------------------
# 与其余 v1 端点的差异：它们**不调用 `_preflight`** —— 限流 / 配额 / 会话归属
# 这三件事都是围绕"会话"的，而登录时还没有会话。只保留限流一道闸门。
#
# 但限流**必须有**：登录端点是最典型的暴力破解目标（本地用户模式下尤其如此，
# 攻击者可以对着用户名反复试密码）。去掉它等于给爆破开绿灯。
@v1.post("/auth/login")
def auth_login_v1(req: LoginReq, request: Request):
    """POST /v1/auth/login：用账号密码换发**本服务会话 token**。

    登出/撤销能力来自本服务的**会话白名单**（见 `app/auth.py` 文件头）：
    每个 sid 各占一个键，登出删掉自己那把即可，该用户其它设备不受影响。

    返回 access_token（短期，放 `Authorization: Bearer`）与 refresh_token
    （长期、**单次使用**、每次刷新轮换）。
    """
    # subject 用请求体里的用户名：**未验证**，所以只参与"这个 IP 是不是共享出口"的
    # 判定，不参与降级分桶（把限流键交给客户端自报的值等于没限流）。
    _rate_limit(request, "", subject=(req.username or ""))
    if not config.AUTH_ENABLED:
        raise HTTPException(status_code=503, detail="服务未启用用户鉴权（未配置 AUTH_*）")

    try:
        principal = _AUTH.login_with_password(req.username, req.password)
    except AuthError as e:
        # ⚠️ 日志**只记 code**，不记 username / password 的任何片段。
        # 登录失败日志是最容易被"顺手记全"的地方，而它恰好含凭证明文。
        logging.warning("登录失败：code=%s", e.code)
        raise HTTPException(status_code=e.status_code,
                            detail=f"{e.detail}（code={e.code}）") from e

    if not _AUTH.can_issue:
        raise HTTPException(
            status_code=503,
            detail="服务未配置 AUTH_SESSION_SECRET，无法签发会话 token")

    access, refresh, _sid = _AUTH.login_principal(principal)
    # 记 user 是刻意的（审计需要回答"谁在什么时候登录了"）；
    # 但它只进服务端日志，不回显到响应里 —— 响应里只有 token 与角色。
    logging.info("用户登录成功：user=%s tenant=%s", principal.user, principal.tenant)
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": config.AUTH_ACCESS_TTL,
        "user": principal.user,
        "tenant": principal.tenant,
        "roles": list(principal.roles),
    }


@v1.post("/auth/refresh")
def auth_refresh_v1(req: RefreshReq, request: Request):
    """POST /v1/auth/refresh：用 refresh token 换新的会话三元组（**轮换**）。

    旧 refresh token 在被用掉的瞬间失效（单次使用）。这是"被截获的 refresh
    token 不能长期复用"的**全部**保证：攻击者用它一次之后，真正的用户下次刷新
    就会失败从而暴露异常，而不是双方都能无限期静默续期。

    刷新**沿用同一个会话**（不新建 sid）：会话已被撤销时这里抛 `session_revoked`
    —— 被登出的会话不能靠 refresh 续命，否则"登出"就只剩个形式。
    """
    # 这里**不传 subject**：refresh token 在请求体里，没有"人"的名字可用；
    # 硬拿 token 当主体只会让每个 token 各占一个桶（等于把 IP 维度的额度放大）。
    _rate_limit(request, "")
    if not config.AUTH_ENABLED or not _AUTH.can_issue:
        raise HTTPException(status_code=503, detail="服务未启用可换发的用户会话")
    try:
        access, refresh, _sid = _AUTH.refresh(req.refresh_token)
    except AuthError as e:
        logging.warning("刷新会话失败：code=%s", e.code)
        raise HTTPException(status_code=e.status_code,
                            detail=f"{e.detail}（code={e.code}）") from e
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": config.AUTH_ACCESS_TTL,
    }


@v1.post("/auth/logout")
def auth_logout_v1(request: Request,
                         all_devices: bool = Query(default=False, alias="all"),
                         x_tenant_id: str = Header(default=""),
                         authorization: str = Header(default="")):
    """POST /v1/auth/logout：登出**当前这台设备**；`?all=true` 登出该用户全部设备。

    默认只移除当前这一个会话，**不影响该用户在其他设备上的登录** —— 允许多设备
    并存之后，"登出"的语义就应该是"登出这一台"，而不是"把该用户所有端都踢掉"。

    `?all=true` 是**清场**手段（改密码后、凭证泄漏、员工离职）：把该用户的所有会话
    从白名单移除。⚠️ 作用域严格取自**已验签凭证里的 user**，不接受任何指定用户名的
    参数 —— 否则它就成了一个人人可用的"任意踢人"接口。

    ⚠️ 能进到这里的身份**必然是**一个服务端会话：`_authenticate` 只认本服务签发的
    令牌，而验签要求载荷带 `jti`（缺了就是 401）。此前那条"外部 IdP token 直连 ⇒
    无会话可撤、请到 IdP 撤销"的分支，随外部身份源的删除一并消失 —— 现在不存在
    "没有会话记录却仍算已认证"的身份。
    """
    ident = _authenticate(x_tenant_id, authorization)
    _rate_limit(request, "", subject=ident.user, subject_verified=bool(ident.user))

    if all_devices:
        n = _AUTH.logout_all(ident.user)
        logging.info("用户登出全部设备：user=%s 清除会话数=%s", ident.user, n)
        return {"revoked": bool(n), "revoked_count": n}

    ok = _AUTH.logout(ident.user, ident.session_id)
    return {"revoked": bool(ok), "revoked_count": 1 if ok else 0}


@v1.get("/auth/me")
def auth_me_v1(request: Request,
                     x_tenant_id: str = Header(default=""),
                     authorization: str = Header(default="")):
    """GET /v1/auth/me：回显当前身份（客户端据此判断"我是谁、能不能审稿"）。

    纯读、无副作用。**刻意不返回任何 token** —— 它只是一个身份探针。

    ⚠️ 2026-10-05 删掉了 `session_bound` 字段（破坏性契约变更）。它原本表示"这次身份
    是否挂着一条服务端会话记录"，但验签要求载荷带 `jti`（缺了就是 401）⇒ 能到达这里的
    身份**必然**挂着一个会话，该字段恒为 `true`。恒为定值的字段没有信息量，反而会让
    客户端与后来读代码的人以为"还存在没有会话的已认证身份" —— 那是外部身份源直连才有的
    形态，已随 OIDC 整体删除。

    `relogin_required` / `relogin_reason` 是给**客户端弹提示**用的：令牌里的租户与角色
    是签发时的快照，管理员改了它们（或批准了入租申请）之后、旧令牌仍在手里生效。
    为真的含义是"服务端的权限已经不是你这张令牌里的了，重新登录才会拿到新的"。
    判定口径与它为什么只认本服务会话，见 `_relogin_reason`。
    """
    ident = _authenticate(x_tenant_id, authorization)
    reason = _relogin_reason(ident)
    return {
        "user": ident.user,
        "tenant": ident.tenant,
        "roles": list(ident.roles),
        "relogin_required": bool(reason),
        "relogin_reason": reason,
    }


# ---------------------------------------------------------------------------
# 自助端点：注册、改密码、申请加入租户
# ---------------------------------------------------------------------------
# 这一组的共同点：调用方是**终端用户自己**，作用域严格限定在"自己"——
# 用户名一律取自已验签凭证，不接受任何参数指定别人。这不是过度谨慎：
# 一个能指定用户名的"改密码"或"踢下线"接口，就是一个人人可用的越权接口。
@v1.post("/auth/register")
def auth_register_v1(req: RegisterReq, request: Request):
    """POST /v1/auth/register：自助注册一个账号（**默认关闭**）。

    注册出来的账号**没有租户**。按本项目当前口径，无租户的账号在检索层不加过滤
    ⇒ **注册完即可搜到全部租户的文档**。这是刻意拍板的行为（见配置文件里
    DEFAULT_TENANT 的说明），所以这个端点默认是关的：要开放必须显式配置
    `AUTH_ALLOW_SELF_REGISTER=true` —— 让这个决定看得见，而不是默认打开。

    ⚠️ 注册**不返回令牌**：新账号要自己去 `/v1/auth/login` 换。这样注册接口就
    不需要签发能力，也少掉"刷注册接口囤积有效会话"这条路径。

    想加入某个租户，注册后走 `POST /v1/join-requests`。
    """
    _rate_limit(request, "", subject=(req.username or ""))   # 未验证，只用于共享出口判定
    if not config.AUTH_ALLOW_SELF_REGISTER:
        raise HTTPException(
            status_code=403,
            detail="自助注册未开启（需要 AUTH_ALLOW_SELF_REGISTER=true）")
    if not config.AUTH_ENABLED or not _AUTH.can_issue:
        raise HTTPException(
            status_code=503,
            detail="服务未启用可登录的用户鉴权（缺 AUTH_SESSION_SECRET 或账号存储）")

    try:
        user = _ACCOUNTS.create_user(
            req.username, userauth.hash_password(req.password), tenant="", roles=())
    except AccountError as e:
        raise HTTPException(status_code=e.status_code,
                            detail=f"{e.detail}（code={e.code}）") from e

    # 只记用户名，不记密码（任何片段都不记）。
    logging.info("用户自助注册：user=%s（尚无租户）", user["username"])
    return {
        "user": user["username"],
        "tenant": "",
        "roles": [],
        "status": user["status"],
        "detail": "注册成功。当前没有租户；如需访问知识库，请提交加入申请。",
    }


@v1.post("/auth/password")
def auth_change_password_v1(req: ChangePasswordReq, request: Request,
                                  x_tenant_id: str = Header(default=""),
                                  authorization: str = Header(default="")):
    """POST /v1/auth/password：改**自己**的密码（需要验证当前密码）。

    为什么要验旧密码：不验的话，一台被借走的**已登录设备**就能静默改掉密码、
    把原主人锁在门外 —— 而"改密码"恰恰是原主人用来赶走入侵者的手段。

    改完**清掉该用户的全部会话**（含发起这次请求的这一台）：改密码的典型动机就是
    "怀疑密码泄漏"，此时把旧会话留着等于什么都没做。代价是客户端需要重新登录，
    这是刻意的。

    管理员重置**别人**的密码走 `PATCH /v1/admin/users/{username}`。
    """
    ident = _authenticate(x_tenant_id, authorization)
    _rate_limit(request, "", subject=ident.user, subject_verified=bool(ident.user))
    if not ident.user:
        raise HTTPException(status_code=403, detail="该接口只接受用户身份（JWT）")

    try:
        # 用登录校验复用同一条 PBKDF2 路径（含"用户不存在也做等价耗时"的处理）。
        _AUTH.login_with_password(ident.user, req.old_password)
    except AuthError as e:
        # 日志不记密码的任何片段，只记"谁改失败了"。
        logging.warning("改密码失败（当前密码不符）：user=%s", ident.user)
        raise HTTPException(status_code=401, detail="当前密码不正确") from e

    try:
        _ACCOUNTS.set_password(ident.user, userauth.hash_password(req.new_password))
    except AccountError as e:
        raise HTTPException(status_code=e.status_code,
                            detail=f"{e.detail}（code={e.code}）") from e

    n = _AUTH.logout_all(ident.user)
    logging.info("用户改密码：user=%s 清除会话数=%s", ident.user, n)
    return {"changed": True, "sessions_revoked": n,
            "detail": "密码已更新，所有设备需重新登录"}


@v1.post("/join-requests")
def join_request_create_v1(req: JoinReqReq, request: Request,
                                 x_tenant_id: str = Header(default=""),
                                 authorization: str = Header(default="")):
    """POST /v1/join-requests：申请加入某个租户（由管理员审批）。

    ⚠️ 申请期间申请人**仍然处于"无租户"状态**，而按当前口径无租户能搜到全部内容
    —— 也就是说"提交申请"只是走流程，在获批之前就已经读得到所有租户的文档。
    这是 2026-10-04 拍板口径的直接后果（判据在 `_retrieval_tenant`），
    不是本接口的缺陷。想收口的话有两条路：给 `RAG_TENANT` 配一个具体值
    （无租户从此是一个具名隔离桶），或改检索层对空租户的语义。

    一人只能属于一个租户：已经在某个租户里的人**不能**再申请加入别的
    （换租户要走管理员），否则"一人一租户"这条约束就名存实亡。
    """
    ident = _authenticate(x_tenant_id, authorization)
    _rate_limit(request, "", subject=ident.user, subject_verified=bool(ident.user))
    if not ident.user:
        raise HTTPException(status_code=403, detail="该接口只接受用户身份（JWT）")

    try:
        row = _ACCOUNTS.create_join_request(ident.user, req.tenant, req.note)
    except AccountError as e:
        raise HTTPException(status_code=e.status_code,
                            detail=f"{e.detail}（code={e.code}）") from e
    logging.info("提交加入租户申请：user=%s tenant=%s id=%s",
                 ident.user, row["tenant"], row["id"])
    return {"id": row["id"], "tenant": row["tenant"], "status": row["status"],
            "detail": "申请已提交，等待管理员审批"}


@v1.get("/join-requests/me")
def join_request_mine_v1(request: Request,
                               x_tenant_id: str = Header(default=""),
                               authorization: str = Header(default="")):
    """GET /v1/join-requests/me：查看**自己**提交过的申请及其状态。

    刻意不给"按用户名查"的参数 —— 那会变成一个"任何人都能看别人申请记录"的接口。

    `requires_relogin`（逐条）与 `relogin_required`（整体）是给客户端弹提示用的：
    审批通过**只改账号表、不断会话**（用户口径 2026-10-04），所以申请人手上那张令牌
    里还是旧租户（通常为空 = 能读全库）。客户端看到这个标记就可以提示
    "你已加入 X 租户，重新登录后生效"，而不必等用户自己发现搜不到东西。
    """
    ident = _authenticate(x_tenant_id, authorization)
    _rate_limit(request, "", subject=ident.user, subject_verified=bool(ident.user))
    if not ident.user:
        raise HTTPException(status_code=403, detail="该接口只接受用户身份（JWT）")
    rows = _ACCOUNTS.list_join_requests(username=ident.user)
    reason = _relogin_reason(ident)
    out = []
    for r in rows:
        item = _request_public(r)
        item["requires_relogin"] = bool(
            reason
            and r.get("status") == REQ_APPROVED
            and (r.get("tenant") or "") != (ident.tenant or ""))
        out.append(item)
    return {"requests": out,
            "relogin_required": bool(reason),
            "relogin_reason": reason}


# ---------------------------------------------------------------------------
# 管理端点：租户与用户的增删改查、加入申请的审批
# ---------------------------------------------------------------------------
# 统一入口 `_admin_ident`：认证 → 限流 → **管理员授权**。三道缺一不可：
#   - 认证：先确定"你是谁"（否则角色无从谈起）；
#   - 限流：管理接口是低频高价值目标，尤其 `POST /admin/users` 可以拿来刷账号；
#   - 授权：`_assert_admin_role` 只认角色（共享密钥那条身份来源已删除）。
#
# ⚠️ 管理端点**不过配额与会话归属闸门**（`_preflight`）：那两个是围绕"对话会话"的，
# 管理操作没有 thread_id，硬套只会引入无意义的失败路径。限流单独调用。
def _admin_ident(request: Request, x_tenant_id: str,
                 authorization: str) -> Identity:
    """管理端点的统一前置：认证 + 限流 + 管理员角色校验，返回身份。"""
    ident = _authenticate(x_tenant_id, authorization)
    _rate_limit(request, "", subject=ident.user, subject_verified=bool(ident.user))
    _assert_admin_role(ident)
    return ident


def _account_http_error(e: AccountError) -> HTTPException:
    """把存储层的业务错误转成 HTTP 异常。

    为什么要带上 `code`：`409` 的原因可能完全不同（用户重名 / 已属于租户 /
    申请已处理过），客户端需要据此决定是改用户名、还是提示"你已经在这个租户里了"。
    只给一个 409 等于让客户端去猜。
    """
    return HTTPException(status_code=e.status_code, detail=f"{e.detail}（code={e.code}）")


def _ts(value):
    """把时间统一成 ISO 字符串。

    必要：内存后端给的是 `time.time()`（float），PG 给的是 `timestamptz`（datetime）。
    不统一的话同一个接口在两种后端下返回两种格式，客户端得写两套解析。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        import datetime as _dt

        return _dt.datetime.fromtimestamp(value, _dt.UTC).isoformat()
    return str(value)


def _tenant_public(row: dict) -> dict:
    """租户行 → 对外形态。"""
    return {"name": row["name"], "status": row["status"], "note": row.get("note") or "",
            "created_at": _ts(row.get("created_at")),
            "updated_at": _ts(row.get("updated_at"))}


def _relogin_reason(ident: Identity) -> str:
    """令牌里的权限与账号表当前值不一致时，给一句**给用户看**的原因；一致则返回空串。

    为什么需要它：JWT 里的 `tenant` / `roles` 是**签发那一刻的快照**。改密码、改租户、
    改角色都会顺手清会话，但"批准加入租户"刻意**不**清（用户口径 2026-10-04：不断会话，
    改为提示重登）。于是客户端需要一个可判定的信号 —— 否则用户只能自己发现
    "我申请明明通过了，却还什么都搜不到"。

    为什么以**账号表**为准：它是权限的唯一事实来源，令牌只是它的一份快照。

    只对挂着服务端会话的身份判定 —— 令牌里的 tenant/roles 是签发时的快照，
    而账号表才是唯一事实来源。没有用户主体的身份无从比较，返回空串。
    """
    if not ident.user:
        return ""
    row = _ACCOUNTS.get_user(ident.user)
    if row is None:
        return ""
    if (row.get("status") or "") != "active":
        return "账号已被停用 —— 请重新登录（或联系管理员）"
    fresh_tenant = row.get("tenant") or ""
    if fresh_tenant != (ident.tenant or ""):
        return (f"你在服务端的租户已变更为「{fresh_tenant or '无租户'}」，"
                f"当前令牌里仍是「{ident.tenant or '无租户'}」—— 重新登录后生效")
    if tuple(row.get("roles") or ()) != tuple(ident.roles or ()):
        return "你在服务端的角色已变更 —— 重新登录后生效"
    return ""


def _user_public(row: dict) -> dict:
    """用户行 → 对外形态。

    ⚠️ **口令哈希绝不出现在响应里**。这不是"顺手过滤"——它的迭代数与盐值足以
    支撑离线爆破，而管理接口的响应会进浏览器、进日志、进工单截图。所以这里用
    **白名单构造**而不是"复制一份再删掉敏感字段"：新增列时白名单默认不暴露，
    黑名单则默认暴露。
    """
    return {"username": row["username"], "tenant": row.get("tenant") or "",
            "roles": list(row.get("roles") or ()), "status": row.get("status"),
            "created_at": _ts(row.get("created_at")),
            "updated_at": _ts(row.get("updated_at"))}


def _request_public(row: dict) -> dict:
    """加入申请行 → 对外形态。"""
    return {"id": row["id"], "username": row["username"], "tenant": row["tenant"],
            "status": row["status"], "reviewer": row.get("reviewer") or "",
            "note": row.get("note") or "",
            "created_at": _ts(row.get("created_at")),
            "reviewed_at": _ts(row.get("reviewed_at"))}


# ---- 租户 ----
@v1.post("/admin/tenants")
def admin_tenant_create_v1(req: TenantReq, request: Request,
                                 x_tenant_id: str = Header(default=""),
                                 authorization: str = Header(default="")):
    """POST /v1/admin/tenants：新建租户（需要管理员角色）。

    租户在本项目里**就是一个名字**：它会进 JWT 载荷、进向量库的过滤字段、
    进审计日志。没有独立的数字 id —— 多一层映射不带来任何好处，只多一处要同步。
    """
    ident = _admin_ident(request, x_tenant_id, authorization)
    try:
        row = _ACCOUNTS.create_tenant(req.name, req.note)
    except AccountError as e:
        raise _account_http_error(e) from e
    # 建租户是权限边界上的事件，必须留下"谁建的"。
    logging.info("管理员建租户：by=%s tenant=%s", ident.user, row["name"])
    return _tenant_public(row)


@v1.get("/admin/tenants")
def admin_tenant_list_v1(request: Request,
                               x_tenant_id: str = Header(default=""),
                               authorization: str = Header(default="")):
    """GET /v1/admin/tenants：列出全部租户，带各自的用户数。

    带 `user_count` 是有用的：删租户前先看这里就知道会不会踩到"还有人在里面"
    （`DELETE` 默认会因此拒绝）。
    """
    _admin_ident(request, x_tenant_id, authorization)
    items = []
    for row in _ACCOUNTS.list_tenants():
        item = _tenant_public(row)
        item["user_count"] = _ACCOUNTS.count_tenant_users(row["name"])
        items.append(item)
    return {"tenants": items, "count": len(items)}


@v1.get("/admin/tenants/{name}")
def admin_tenant_get_v1(name: str, request: Request,
                              x_tenant_id: str = Header(default=""),
                              authorization: str = Header(default="")):
    """GET /v1/admin/tenants/{name}：租户详情（不存在返回 404）。"""
    _admin_ident(request, x_tenant_id, authorization)
    row = _ACCOUNTS.get_tenant(name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"租户不存在：{name}")
    item = _tenant_public(row)
    item["user_count"] = _ACCOUNTS.count_tenant_users(row["name"])
    item["users"] = [u["username"] for u in _ACCOUNTS.list_users(tenant=row["name"])]
    return item


@v1.patch("/admin/tenants/{name}")
def admin_tenant_rename_v1(name: str, req: TenantRenameReq, request: Request,
                                 x_tenant_id: str = Header(default=""),
                                 authorization: str = Header(default="")):
    """PATCH /v1/admin/tenants/{name}：租户改名。

    ⚠️ 改名会**同批改写挂靠用户的租户字段**（以及历史申请记录里的租户名）。
    只改租户表的话，那些人会指向一个不存在的租户 —— 而那个状态不报错，
    只表现为"他们突然搜不到东西了"。
    """
    ident = _admin_ident(request, x_tenant_id, authorization)
    try:
        ok = _ACCOUNTS.rename_tenant(name, req.new_name)
    except AccountError as e:
        raise _account_http_error(e) from e
    if not ok:
        raise HTTPException(status_code=404, detail=f"租户不存在：{name}")
    logging.info("管理员改租户名：by=%s %s -> %s", ident.user, name, req.new_name)
    return _tenant_public(_ACCOUNTS.get_tenant(req.new_name) or {"name": req.new_name})


@v1.delete("/admin/tenants/{name}")
def admin_tenant_delete_v1(name: str, request: Request,
                                 force: bool = Query(default=False),
                                 x_tenant_id: str = Header(default=""),
                                 authorization: str = Header(default="")):
    """DELETE /v1/admin/tenants/{name}：删除租户。

    ⚠️ 默认**拒绝**删除还有用户挂靠的租户（返回 409 并列出那些人）。必须显式加
    `?force=true` 才会连带把他们移出租户。这不是过度谨慎 —— 按当前口径，
    "移出租户"等于"变成能搜到全部内容"，所以这一步必须是**看得见的**勾选，
    而不是一个顺带发生的副作用。
    """
    ident = _admin_ident(request, x_tenant_id, authorization)
    try:
        result = _ACCOUNTS.delete_tenant(name, force=force)
    except AccountError as e:
        raise _account_http_error(e) from e
    if not result["deleted"]:
        if result.get("orphaned_users"):
            raise HTTPException(
                status_code=409,
                detail=(f"租户 {name} 下仍有 {len(result['orphaned_users'])} 个用户，"
                        f"拒绝删除：{', '.join(result['orphaned_users'])}。"
                        "先把他们移出，或显式加 ?force=true（注意：移出租户后他们"
                        "将失去租户隔离，可搜到全部内容）"))
        raise HTTPException(status_code=404, detail=f"租户不存在：{name}")
    if result["orphaned_users"]:
        # 这是权限边界被放宽的一次操作 —— 必须留下痕迹，且能追溯到人。
        logging.warning("管理员强删租户：by=%s tenant=%s 被移出租户的用户=%s",
                        ident.user, name, ",".join(result["orphaned_users"]))
    else:
        logging.info("管理员删租户：by=%s tenant=%s", ident.user, name)
    return {"deleted": True, "orphaned_users": result["orphaned_users"]}


# ---- 用户 ----
@v1.post("/admin/users")
def admin_user_create_v1(req: UserReq, request: Request,
                               x_tenant_id: str = Header(default=""),
                               authorization: str = Header(default="")):
    """POST /v1/admin/users：管理员创建用户。

    `tenant` 可以留空（创建一个还没有租户的用户）。填了的话**不校验租户是否存在**
    是有意的吗 —— 不，这里会校验：一个不存在的租户名会让这个人落到一个没有数据、
    也没人能管的桶里，而那种状态从响应上看和成功一模一样。
    """
    ident = _admin_ident(request, x_tenant_id, authorization)
    tenant = (req.tenant or "").strip()
    if tenant and _ACCOUNTS.get_tenant(tenant) is None:
        raise HTTPException(status_code=404,
                            detail=f"租户不存在：{tenant}（可先用 POST /v1/admin/tenants 创建）")
    try:
        row = _ACCOUNTS.create_user(req.username, userauth.hash_password(req.password),
                                    tenant=tenant, roles=req.roles)
    except AccountError as e:
        raise _account_http_error(e) from e
    logging.info("管理员建用户：by=%s user=%s tenant=%s roles=%s",
                 ident.user, row["username"], row["tenant"] or "(无)",
                 "/".join(row["roles"]) or "无")
    return _user_public(row)


@v1.get("/admin/users")
def admin_user_list_v1(request: Request,
                             tenant: str = Query(default=""),
                             x_tenant_id: str = Header(default=""),
                             authorization: str = Header(default="")):
    """GET /v1/admin/users：列出用户；`?tenant=` 只列该租户的。

    ⚠️ `tenant=` 传空串与不传是**两个意思**（"只看无租户的人" vs "看全部"），
    所以这里区分得很清楚：不传 = 全部；传了（哪怕空串）= 精确匹配。
    """
    _admin_ident(request, x_tenant_id, authorization)
    rows = _ACCOUNTS.list_users(tenant=tenant if "tenant" in request.query_params else None)
    return {"users": [_user_public(r) for r in rows], "count": len(rows)}


@v1.get("/admin/users/{username}")
def admin_user_get_v1(username: str, request: Request,
                            x_tenant_id: str = Header(default=""),
                            authorization: str = Header(default="")):
    """GET /v1/admin/users/{username}：用户详情（不含口令哈希）。"""
    _admin_ident(request, x_tenant_id, authorization)
    row = _ACCOUNTS.get_user(username)
    if row is None:
        raise HTTPException(status_code=404, detail=f"用户不存在：{username}")
    return _user_public(row)


@v1.patch("/admin/users/{username}")
def admin_user_patch_v1(username: str, req: UserPatchReq, request: Request,
                              x_tenant_id: str = Header(default=""),
                              authorization: str = Header(default="")):
    """PATCH /v1/admin/users/{username}：改用户（改密码 / 换租户 / 改角色 / 停用）。

    ⚠️ 三件事必须知道：

      1. **租户变更会立刻断掉他现有的会话**。JWT 里的租户与角色是**签发时的快照**，
         不清理会话的话，管理员把某人移出租户之后，他手上的令牌仍然是旧租户 ——
         最长能再撑一个 access token 的生命周期（默认 15 分钟）。所以这里对
         "租户 / 角色 / 状态 / 密码"任一变更都执行一次**登出全部设备**，
         让新权限在下一个请求就生效。代价是他需要重新登录。
      2. **`tenant` 传空串 = 移出租户**（不是"不改"）。缺省（不传这个字段）= 不改。
      3. **两条"自伤"护栏**：不能把自己**停用**，也不能**摘掉自己的管理员角色**
         （自降权）。两者都会当场把自己锁在管理接口外面，而恢复只能靠另一个管理员
         —— 系统里可能只有一个。自降权尤其隐蔽：请求本身会**成功**，直到下一次调用
         管理接口才 403，那时的第一反应会是"令牌坏了 / 角色配置丢了"，而不是
         "昨天自己改过角色"。
      4. **不能移除最后一个管理员**（降权 / 停用 / 删除都算，含"两个管理员互相
         降权"这种并发情形）：由存储层在同一个事务里带锁判定，见
         `AccountStore._guard_last_admin`。接口层的护栏只拦得住"改自己"，
         拦不住这一步 —— 两层都需要。
    """
    ident = _admin_ident(request, x_tenant_id, authorization)

    if username == ident.user and (req.status == STATUS_DISABLED
                                   or _drops_admin(req.roles)):
        raise HTTPException(
            status_code=409,
            detail="不能停用或摘掉当前登录管理员的管理员角色（先让另一个管理员操作）")

    tenant = req.tenant
    if tenant is not None:
        tenant = tenant.strip()
        if tenant and _ACCOUNTS.get_tenant(tenant) is None:
            raise HTTPException(status_code=404, detail=f"租户不存在：{tenant}")

    fields = {}
    if req.password is not None:
        fields["password_hash"] = userauth.hash_password(req.password)
    if tenant is not None:
        fields["tenant"] = tenant
    if req.roles is not None:
        fields["roles"] = req.roles
    if req.status is not None:
        fields["status"] = req.status

    try:
        row = _ACCOUNTS.update_user(username, keep_active_admins=True,
                                    admin_roles=config.AUTH_ADMIN_ROLES,
                                    **fields) if fields \
            else _ACCOUNTS.get_user(username)
    except AccountError as e:
        raise _account_http_error(e) from e
    if row is None:
        raise HTTPException(status_code=404, detail=f"用户不存在：{username}")

    revoked = 0
    if fields:
        # 任何权限相关字段变化都清会话：让改动**立刻生效**，而不是等令牌自然过期。
        revoked = _AUTH.logout_all(username)
        logging.info("管理员改用户：by=%s user=%s 字段=%s 清除会话数=%s",
                     ident.user, username, ",".join(sorted(fields)), revoked)
    return {**_user_public(row), "sessions_revoked": revoked}


@v1.delete("/admin/users/{username}")
def admin_user_delete_v1(username: str, request: Request,
                               x_tenant_id: str = Header(default=""),
                               authorization: str = Header(default="")):
    """DELETE /v1/admin/users/{username}：删除用户，并清掉他的全部会话。

    ⚠️ **不能删自己**：把自己删掉之后，如果系统里没有别的管理员，就再也没人能
    管理账号了（只能改配置重启——而那需要能登进服务器的人）。

    ⚠️ **也不能删掉最后一个管理员**（接口层那条只拦得住"删自己"，拦不住
    "管理员 A 删管理员 B"以及两个管理员**并发**互删）：由存储层在同一个事务里
    带锁判定，见 `AccountStore._guard_last_admin`。

    删除会**同批清掉他的会话**。只删用户行的话，他手上那个未过期的 access token
    仍然带着有效的签名与 jti —— 而会话白名单里那条记录还在，于是它继续可用
    直到自然过期。这类"删了用户却还能用"的窗口不报错、不告警，只能靠这里封住。
    """
    ident = _admin_ident(request, x_tenant_id, authorization)
    if username == ident.user:
        raise HTTPException(status_code=409,
                            detail="不能删除当前登录的管理员账号（先让另一个管理员操作）")
    try:
        deleted = _ACCOUNTS.delete_user(username, keep_active_admins=True,
                                        admin_roles=config.AUTH_ADMIN_ROLES)
    except AccountError as e:
        raise _account_http_error(e) from e
    if not deleted:
        raise HTTPException(status_code=404, detail=f"用户不存在：{username}")
    n = _AUTH.logout_all(username)
    logging.info("管理员删用户：by=%s user=%s 清除会话数=%s", ident.user, username, n)
    return {"deleted": True, "sessions_revoked": n}


# ---- 加入申请的审批 ----
@v1.get("/admin/join-requests")
def admin_join_list_v1(request: Request,
                             status: str = Query(default=""),
                             x_tenant_id: str = Header(default=""),
                             authorization: str = Header(default="")):
    """GET /v1/admin/join-requests：列出加入申请（`?status=pending` 只看待审）。

    默认**全部**而不是只列待审：审批之后还能回看"谁批的、什么时候批的"，
    是这套流程存在的意义之一。
    """
    _admin_ident(request, x_tenant_id, authorization)
    rows = _ACCOUNTS.list_join_requests(status=(status or None))
    return {"requests": [_request_public(r) for r in rows], "count": len(rows)}


@v1.post("/admin/join-requests/{req_id}")
def admin_join_resolve_v1(req_id: int, req: JoinResolveReq, request: Request,
                                x_tenant_id: str = Header(default=""),
                                authorization: str = Header(default="")):
    """POST /v1/admin/join-requests/{id}：批准或驳回一条申请。

    批准时**顺带把申请人加入该租户** —— 两件事必须同批完成。只批准不改租户的话，
    申请人会一直停在"无租户"状态，而无租户按当前口径能搜到全部内容：审批动作
    看起来成功了，实际什么也没改变。

    已处理过的申请不能重复处理（409）—— 否则"先驳回、再批准"能绕过审批记录。
    """
    ident = _admin_ident(request, x_tenant_id, authorization)
    try:
        row = _ACCOUNTS.resolve_join_request(
            req_id, approve=req.approve, reviewer=ident.user, note=req.note)
    except AccountError as e:
        raise _account_http_error(e) from e
    logging.info("审批加入申请：by=%s id=%s user=%s tenant=%s -> %s",
                 ident.user, row["id"], row["username"], row["tenant"], row["status"])
    return _request_public(row)


@v1.post("/chat")
def chat_v1(req: ChatReq, request: Request,
                  x_tenant_id: str = Header(default=""),
                  authorization: str = Header(default=""),
                  idempotency_key: str = Header(default="", alias="Idempotency-Key")):
    """POST /v1/chat：启动一轮 SSE 流式对话。

    req: 请求体 ChatReq —— thread_id 串起多轮与后续人工审核，message 是用户输入。
    authorization: 请求头 Authorization: Bearer <JWT>（**必带**）。身份与租户都来自
        令牌载荷：用户/租户/角色是签发时的**快照**。租户为空串表示"这个账号还没有
        租户"，此时检索层只给看**公共租户**的内容（不再"不加过滤"）。
        令牌获取：`POST /v1/auth/login`。
    x_tenant_id: 请求头 X-Tenant-Id。**不是租户来源**，只做一致性核对 ——
        与令牌里的租户不符会被 403。不带它也能正常调用。
    idempotency_key: 可选的 `Idempotency-Key`。带上它，客户端超时重试就不会
        重跑一整轮（规范见 `deploy/IDEMPOTENCY.md`）。

    内部把 message 作为这一轮的输入交给 `run_turn`（一次 `Runner.run`）；若模型
    在交付时自己声明需要人看（`needs_review`），流里会下发 need_review 事件
    （草稿 + 评分 + 意见），同时把这条产出登记进审核队列等审，由前端调 /review
    再审。⚠️ 审核之后**不能"续跑"**：拒绝/通过都是对已产出的那份文本的处置，
    没有可恢复的执行现场（见 `deploy/RUNBOOK.md` 的三态判据）。
    请求处理前会过"限流 → 配额 → 会话归属"三道闸门。

    幂等闸门排在**所有业务 4xx 之后、配额之前**（理由见 `_IdemGate` 的说明）。
    """
    ident = _authenticate(x_tenant_id, authorization)
    tenant = ident.tenant
    _preflight(request, session=req.thread_id, tenant=tenant, user=ident.user)
    gate = _idempotency_gate(tenant, "chat", idempotency_key, req.model_dump(),
                             ident.user)
    if gate.response is not None:
        return gate.response
    incoming = TurnRequest(
        prompt=req.message,
        thread_id=req.thread_id,
        # ⚠️ 这里传**归一后**的检索租户，不是 `ident.tenant`：
        # `TurnRequest.tenant` 的唯一去向是检索层（`build_context` → `get_retriever`），
        # 而"给他看哪些库"的判据只写在 `_retrieval_tenant` 里（无租户⇒公共租户⇒
        # 哨兵，绝不返回空串）。传 `ident.tenant` 的话，空串会从这条路走进检索层的
        # 进程级默认值，而 `_retrieval_tenant` 那套 fail-closed 就只剩文档上的。
        # 与之相对，配额分桶 / 审核归属 / 会话属主仍然用 `tenant`（空串是如实记录）。
        tenant=_retrieval_tenant(ident),
    )
    budget = RequestBudget(_QUOTA.request_limit)
    return _sse(incoming, "chat", tenant=tenant, subject=ident.user,
                thread_id=req.thread_id, budget=budget,
                idem_key=gate.key, idem_fp=gate.fp,
                # 每次运行一个编号：`/stop` 靠它精确命中"正在跑的这一次"，
                # 而不是"这个会话现在还开不开着"。理由见 run_control 模块 docstring。
                run_id=uuid.uuid4().hex)


@v1.post("/review")
async def review_v1(req: ReviewReq, request: Request,
                    x_tenant_id: str = Header(default=""),
                    authorization: str = Header(default=""),
                    idempotency_key: str = Header(default="", alias="Idempotency-Key")):
    """POST /v1/review：人工审核的入口 —— 把审核人的决定送回停机容器。

    req: 请求体 ReviewReq。thread_id 用于定位会话；action 只允许 accept / revise；
        feedback 在 revise 时必填（accept 时可留空）。
    x_tenant_id: 同 /v1/chat。
    idempotency_key: 同 /v1/chat。它同时挡住了"审核 POST 被重试"这类事故。

    三道前置校验，任一条不过都不动容器：
      - action 非 accept/revise → 400；
      - revise 但 feedback 全空白 → 400；
      - 该 thread 当前并没有停在审核（如已完成、或从未触发）→ 409。
    通过后把队列里的记录标记为已处理（accepted / revised）。

    两个动作走**完全不同的路**（2026-10-20 起）：
      - accept → 把现场/队列里的草稿直接**作为答复流出去**，不再跑模型：
        正文早就产出了（有现场时在 ``RunState`` 里，没现场时在队列里），
        再跑一遍就是重复计费、重复落盘。
      - revise → 容器里写回决定，然后**新起一轮运行**，审核意见作为
        `configurable.human_feedback` 交给生成方（不进 state、不进对话历史）。

    ⚠️ 幂等闸门必须排在 `_REVIEW_QUEUE.resolve` **之前**：那条 resolve 是
    **写操作**（出队 + 留痕"谁批的"）。若一条重试先改了状态、再被幂等判成
    重复，队列里就会留下"某人已批"的审计记录，而这次审批根本没执行。
    """
    ident = await _off(_authenticate, x_tenant_id, authorization)
    tenant = ident.tenant
    # 授权检查排在业务校验（action 合法性、会话是否停在审核）**之前**：
    # 先确认"你配不配做这件事"，再看"这件事能不能做"。反过来的话，
    # 一个无权限的调用方能靠 400/409 的差异去探测会话状态。
    _assert_review_role(ident)
    if req.action not in ("accept", "revise"):
        raise HTTPException(status_code=400, detail="action 必须是 accept 或 revise")
    if req.action == "revise" and not req.feedback.strip():
        raise HTTPException(status_code=400, detail="revise 必须填写 feedback")

    await _off(_preflight, request, session=req.thread_id, tenant=tenant, user=ident.user)

    # ⚠️ 幂等闸门必须排在**状态检查之前**（2026-10-05 修正顺序）
    # ------------------------------------------------------
    # 此前是"先查状态（还在不在等审），再走幂等闸门"。那个顺序让**重放永远走不到**：
    # 第一次 /review 成功后那条待审记录就结案了，于是客户端拿同一个 Idempotency-Key
    # 重试时，状态检查先命中、直接 409 —— 而"重放上一次已完成的结果"正是幂等模块
    # docstring 承诺的标准 2.6 行为。承诺与实现对不上，且**没有任何测试覆盖**。
    #
    # 更糟的是它的下游影响：客户端会以为"这次审批没成功"，于是换个新键再点一次 ——
    # 而状态其实早已推进，行为不可预期。而"审核 POST 被重试"恰恰是幂等设计里
    # 点名的那个事故场景。
    #
    # 顺序调过来之后，业务 409 发生在闸门**之后** ⇒ 必须把占住的键还回去
    # （见 `_release_idem_gate`），否则客户端重试会撞上"正在处理中"的假死。
    gate = await _off(_idempotency_gate, tenant, "review", idempotency_key,
                      req.model_dump(), ident.user)
    if gate.response is not None:
        return gate.response

    # 409 的判据：这条会话**当前**有没有一条待审记录。
    # 两个来源，因为两类拦截存的东西不同：
    #   · `needs_review`（模型在交付那一刻声明）⇒ 运行现场在 run_store 里；
    #   · `manual`（跑偏 / 护栏拦下 / 没产出）⇒ 只有待审队列，没有现场
    #     （运行已经结束，没有任何"可以停下来的地方"可回退）。
    # 判据必须是"事实存在"而不是"曾经想审"：入队成功但存现场失败时，
    # 信意图会让端点去恢复一条不存在的运行。
    record = await _off(_REVIEW_QUEUE.record, req.thread_id)
    held = await _off(run_store.load, req.thread_id)
    if not (held or (record and record.get("status") == "pending")):
        await _off(_release_idem_gate, gate, tenant)
        raise HTTPException(status_code=409, detail="该会话当前不在人工审核状态")

    # 人已处理 → 出队（审核队列只用来看"还有哪些没人管"）。
    # 同时**留痕**：谁、做了什么决定、留了什么话。
    # 少了这一步，复盘时只能看到"这条被 accept 了"，无法判断越权/误操作，也追不到人。
    #
    # actor 取**用户主体**（`or tenant` 只是防御性兜底：JWT 身份一定有 user，
    # 历史上那条"没有用户概念"的共享密钥路径已删除）。过去一律记租户，导致审计
    # 只能回答"哪个租户处理的"，而"租户里是哪个人"永远查不到 —— 等于没有归因能力。
    resolved = await _off(
        _REVIEW_QUEUE.resolve,
        req.thread_id,
        "accepted" if req.action == "accept" else "revised",
        actor=ident.user or tenant, note=req.feedback or "",
    )
    if not resolved and _REVIEW_QUEUE.enabled:
        # ⚠️ 返回值**必须**看（2026-10-06 修）。原来直接丢弃 ⇒ 写库失败时
        # "审批推进了、队列里那条却永远停在 pending、也没留下是谁批的"，
        # 而且**没有任何信号** —— 恰恰违背这个模块存在的全部理由（审计归因）。
        #
        # 为什么不在这里抛错阻断审批：审批的**业务**正确性由运行现场（`RunState`）决定，
        # 审核队列只是"待办清单 + 留痕"。为一个可降级的留痕问题去打断已经成立的
        # 审批，等于把内部故障升级成用户可见的失败。
        #
        # 为什么要带 `_REVIEW_QUEUE.enabled` 这个条件：`REVIEW_QUEUE_ENABLED=0` 时
        # `resolve` 本身就返回 False（压根没入队），那是**正常部署**。
        # 不区分就报 ERROR 的话，真正的写库故障会淹没在天天都有的噪音里。
        metrics.inc("review_resolve_missing_total")
        logging.error(
            "审批留痕失败：审核队列里找不到 thread_id=%s（actor=%s）—— "
            "审批已照常推进，但这条记录**没有留下是谁批的**。"
            "请检查审核队列后端（PG 抖动 / 记录被清理 / 队列开关与写入路径不一致）。",
            req.thread_id, ident.user or tenant)

    # ---- 配额预占必须排在**恢复运行之前** ----
    # 反过来的话：预占失败（429）时运行已经被恢复，审批等于"执行了"——
    # 但用户一个字的答复都没拿到，而客户端重试会撞 409（现场已被消费）。
    # 预占在前，429 时现场仍停着，重试完全安全。
    reserved = await _off(_reserve_quota, tenant, ident.user, gate.key)

    # ---- 把决定变成**一轮运行** ----
    # 三种形态，全部落在官方原语上，没有第四套机制：
    #   · 有现场 + accept ⇒ `Runner.run(agent, state)` 接着跑（state 里这个待批项
    #     被批准，交付工具随即执行，答复就产生了）；
    #   · 有现场 + revise ⇒ 同一个入口，只是一次 `reject(意见)`：模型收到拒绝理由，
    #     改完再交一次；
    #   · 没有现场（`manual` 那类，运行早已结束）⇒ accept 直接交付草稿、
    #     revise 新起一轮并把意见作为 `human_feedback` 带进去。
    if held:
        turn = TurnRequest(
            prompt="", thread_id=req.thread_id, tenant=_retrieval_tenant(ident),
            resume=ResumeRequest(
                snapshot=held["snapshot"],
                approved=(req.action == "accept"),
                reason=req.feedback or "",
            ),
        )
        # ⚠️ 现场只能用一次：官方消费掉批准结果之后就作废了。不删的话，
        # 同一个待批项能被恢复两次 ⇒ 同一个动作执行两遍，而且不报错。
        await _off(run_store.drop, req.thread_id)
    elif req.action == "accept":
        draft = (record or {}).get("draft") or ""
        if not draft.strip():
            # 没有正文可交：把已经占住的两样东西还回去，别让客户端重试撞假死。
            await _off(_release_idem_gate, gate, tenant)
            await _off(_QUOTA.settle, tenant, reserved, 0, subject=ident.user)
            raise HTTPException(status_code=409, detail="没有可交付的待审正文")
        turn = TurnRequest(prompt="", thread_id=req.thread_id,
                           tenant=_retrieval_tenant(ident), deliver_text=draft)
    else:
        # `prompt` 给空串是有意的：原话已经在官方会话里，打回只需要意见
        # （见 `runner._build_input_items` 对打回轮的判据）。
        turn = TurnRequest(prompt="", thread_id=req.thread_id,
                           tenant=_retrieval_tenant(ident),
                           human_feedback=req.feedback or "")

    return _sse(turn, "review", tenant=tenant, subject=ident.user,
                thread_id=req.thread_id, budget=RequestBudget(_QUOTA.request_limit),
                reserved=reserved,
                idem_key=gate.key, idem_fp=gate.fp,
                run_id=uuid.uuid4().hex)


@v1.get("/reviews")
def list_reviews(request: Request, limit: int = 50,
                 x_tenant_id: str = Header(default=""),
                 authorization: str = Header(default="")):
    """GET /v1/reviews：列出待人工审核的会话（默认 50 条，按时间倒序）。

    给"审核工作台"用：没有它，待审内容只能靠人记得去某条会话里翻，无人知晓。

    ⚠️ 2026-10-05 修了两处（此前是个可被低权限账号利用的跨租户泄露口）：
    1. **补了资源级授权**。此前只做 `_authenticate`，不查角色 —— 任何登录账号
       都能读到同租户里别人的待审草稿与评审意见；而写侧的 `/v1/review` 一直是
       查角色的。读写两侧的授权强度不一致，等于把"能看"当成了"能审"的弱化版。
    2. **租户口径**。此前把原始 `ident.tenant` 交给队列，而队列对**空串**的语义
       曾是"不过滤" ⇒ 任何一个还没有租户的账号（`UserReq.tenant` 默认就是空串，
       是正常运营中必然存在的状态）会拿到**全部租户**的待审草稿。
       现在空串只表示"空租户那一份队列"，不再等于"不加过滤"（见
       `ReviewQueue.pending`）。这里仍然传**原始租户**而不是 `_retrieval_tenant`：
       "这个人有没有租户"是需要如实记录的事实，与"给他看什么内容"是两件事，
       这一点在 `_retrieval_tenant` 的 docstring 里已经写明，本次不改那条边界。
    """
    ident = _authenticate(x_tenant_id, authorization)
    _assert_review_role(ident)
    tenant = ident.tenant
    items = _REVIEW_QUEUE.pending(limit=max(1, min(int(limit), 500)), tenant=tenant)
    return {"count": len(items), "items": items}


@v1.get("/sessions/{thread_id}")
async def session_state_v1(thread_id: str, request: Request,
                           x_tenant_id: str = Header(default=""),
                           authorization: str = Header(default="")):
    """GET /v1/sessions/{thread_id}：取会话的**持久化状态快照**（断点续传的基础）。

    thread_id: 会话 id（路径参数）。
    x_tenant_id: 同 /v1/chat。

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
    ident = await _off(_authenticate, x_tenant_id, authorization)
    # 只限流、**不查配额、不认领会话** —— 看状态不花 token，也不该改变归属。
    await _off(_rate_limit, request, thread_id)
    await _off(_assert_session_readable, thread_id, ident.tenant, ident.user)

    # 状态有三个来源，各回答一半，缺一半都会给出错误结论：
    #   · 运行现场（RunState）—— 有没有"跑到一半停下来等审批"的那一轮；
    #   · 待审队列 —— `manual` 那类（运行已结束）只有这份记录；
    #   · 官方会话 —— 这条会话说过什么（对话历史的唯一来源）。
    from src.agents import session as agent_session

    held = await _off(run_store.load, thread_id)
    record = await _off(_REVIEW_QUEUE.record, thread_id)
    history = await agent_session.load_items(thread_id)
    view = _session_view(thread_id, held=held, record=record, messages=history)
    if view["status"] == "unknown":
        raise HTTPException(status_code=404, detail="会话不存在")
    return view


@v1.post("/sessions/{thread_id}/messages")
async def session_message_v1(thread_id: str, req: SessionMessageReq, request: Request,
                             x_tenant_id: str = Header(default=""),
                             authorization: str = Header(default=""),
                             idempotency_key: str = Header(default="",
                                                           alias="Idempotency-Key")):
    """POST /v1/sessions/{thread_id}/messages：给**正在进行的任务**补一句话（不触发运行）。

    它与 `POST /v1/chat` 的差别只有一件事，但这件事定义了整个接口：
    `/v1/chat` 是"开始一轮"，这里是"给跑着的那一轮补一句"。所以它
    **不启动任何生成**、不占配额、不产生 SSE —— 只是一次写入。

    补的话什么时候生效（按先后）
    ------------------------------
      1. **本次模型运行的下一次调用** —— 通过 ``supplements`` 的进程内增量注入。
         这是"用户看到 agent 还在跑，补一句，它下一步就照做"的那一档；
      2. **下一个 agent 节点** —— 因为补充这句话被**同时写进了会话历史**
         （官方 Session 后端），而节点之间是独立的 SDK 运行，各自会重新读一次历史；
      3. **下一轮 `/v1/chat`** —— 同一个道理，历史里已经有它。

    ⚠️ **当前这次模型调用看不到**：那一次调用的输入在它开始时就定格了。想改它只能
    打断重来（`POST /v1/sessions/{id}/stop`，见那里的说明）。

    为什么会写两份
    ------------
    会话历史那份是**持久**的（跨节点、跨轮、跨副本、跨重启都靠它）；
    进程内那份是**当次运行**的加速（历史快照已经读过了，不补就只能等下一个节点）。
    只写进程内 ⇒ 重启或换副本就丢了；只写历史 ⇒ 当次运行完全不生效。
    两份都写不是冗余，是两段时间尺度各需要一个载体。

    ⚠️ 边界：进程内那份**只在本副本有效**。补充请求落到 A 副本、任务跑在 B 副本时，
    第 1 档不生效，但第 2/3 档照常生效（它们靠的是库里的历史）。

    为什么正文要带 `【任务执行中补充】` 前缀
    ------------------------------------
    不带的话，模型会把这条当成**新一轮的用户请求**，于是可能丢下手头的活去回答它。
    "补充"与"新指令"的区别没有任何机制能表达，只能靠文案说清楚。前缀在写入历史与
    注入模型输入两处**共用同一个常量**（`supplements.PREFIX`），不允许各写一份。

    为什么写历史时不切断"工具调用 / 工具结果"这一对
    --------------------------------------------
    SDK 是**一轮一次**写入的（`run.py` 里 `save_result_to_session` 每轮 turn 末尾
    调用一次，把该轮所有条目一起交出去），不是逐条追加。所以我们插进去的那条只会
    落在两轮之间 —— 落不到 function_call 与 function_call_output 中间。
    这一点如果哪天 SDK 改成逐条写入，这里就会开始产出**非法历史**（下一轮模型调用
    直接报错），所以它不是风格问题，是依赖。

    为什么这个端点也要 `Idempotency-Key`
    ----------------------------------
    `/v1/chat` 要它，是因为重试会重复生成、重复计费；这里要它，是因为重试会
    **把同一句话写两遍**。性质相同（一次请求变成两次副作用），后果不同：
    那边是账单翻倍，这边是**模型以为用户说了两遍**，于是把同一句要求当成两次强调
    去执行 —— 而用户只看得到一个"已收到"。

    ⚠️ 指纹里必须带 `thread_id`：它在本端点位于**路径**而不在请求体里，
    不显式并进去的话，"同一个键、两个不同会话、同样一句话"会被判成同一次请求
    并**重放** —— 客户端以为写进了 B 会话，而实际只有 A 会话里有。这个错没有任何
    报错，只能靠事后翻历史发现。
    """
    ident = await _off(_authenticate, x_tenant_id, authorization)
    await _off(_rate_limit, request, thread_id, ident.user, True)
    # fail-closed：多租户下归属不符 403、查不到属主 404（见 `_assert_session_readable`）。
    await _off(_assert_session_readable, thread_id, ident.tenant, ident.user)

    text = req.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="message 不能为空")

    # 延迟导入：`src.agents.*` 依赖官方 SDK，没装 SDK 的环境连图都建不起来，
    # 不能出现在模块级 import 里（与其它 agent 相关导入同一条纪律）。
    from src.agents import session as agent_session
    from src.agents import supplements

    session = agent_session.session_for(thread_id)
    if session is None:
        # 没有会话后端就没有第 2/3 档，只剩"当次运行"这一档 —— 而这一档随时会因
        # 进程重启/换副本而丢。与其给一个"补上了"的假象，不如明确告诉调用方存不下。
        raise HTTPException(
            status_code=503,
            detail="会话历史后端不可用（未配置 DB_URL），补充无法保存")

    # 幂等闸门排在**所有业务 4xx 与"没有后端"之后**。理由与 `/review` 那条修正相同：
    # `begin()` 是"先占后判"的原子抢占，让一个必然失败的请求先占住键，客户端拿同一个
    # 键重试就会被自己的 409「正在处理中」挡住 —— 而实际上没有任何东西在处理它。
    gate = await _off(_idempotency_gate, ident.tenant, "session_message",
                      idempotency_key,
                      {"thread_id": thread_id, **req.model_dump()},
                      ident.user, _replay_json_from_idem_payload)
    if gate.response is not None:
        return gate.response

    line = f"{supplements.PREFIX}{text}"
    try:
        # ⚠️ 这是**异步**方法（官方 Session 协议），必须 await —— 漏掉的话拿到的
        # 是一个从未被 await 的协程，写入不会发生，而接口照样返回 accepted。
        await session.add_items([{"role": "user", "content": line}])
    except Exception as e:  # noqa: BLE001  后端抖动 / 表缺失
        # 失败**不占用**键（见 idempotency.py 的"失败与未完成不占用键"）：
        # PG 抖动一次不该让这个键在 TTL 内不可用，而客户端的重试完全正确。
        await _off(_release_idem_gate, gate, ident.tenant)
        metrics.inc("session_message_errors_total")
        logging.exception("补充写入会话历史失败：session=%s", thread_id)
        raise HTTPException(status_code=503,
                            detail=f"写入会话历史失败：{type(e).__name__}") from e

    seq = supplements.note(thread_id, text)
    metrics.inc("session_message_total")
    logging.info("任务执行中补充：session=%s subject=%s seq=%s chars=%d",
                 thread_id, ident.user, seq, len(text))
    body = {
        "thread_id": thread_id,
        "accepted": True,
        # 0 = 没登记（会话号或正文为空，理论上走不到 —— 上面已经挡过）。
        "seq": seq,
        "pending": supplements.pending_count(thread_id),
        "applies_to": ["running_model_call", "next_node", "next_turn"],
    }
    # 登记"已完成 + 可重放的正文"。同键重试据此原样返回**同一个 seq** —— 那代表
    # "同一次逻辑写入"，重新生成一个号会让客户端以为又写了一条。
    await _off(_IDEMPOTENCY.finish, ident.tenant, gate.key, gate.fp,
               json.dumps(body, ensure_ascii=False), subject=gate.subject)
    return body


@v1.post("/sessions/{thread_id}/stop")
async def session_stop_v1(thread_id: str, request: Request,
                          x_tenant_id: str = Header(default=""),
                          authorization: str = Header(default="")):
    """POST /v1/sessions/{thread_id}/stop：**停掉这个会话正在跑的那一次运行**。

    它停的是"**某一次运行**"，不是"这个会话"。差别在于竞态：如果按会话判，
    任务刚跑完、停止请求才到 ⇒ 标记留下 ⇒ 下一轮一开始就被停，用户看到的是
    "刚发出去就被打断"，且没有任何线索可查。按 ``run_id`` 就没有这个窗口 ——
    运行结束即删登记行，请求到达时找不到就是找不到。

    停下之后是什么状态
    ----------------
      · **已经产出的正文照常保留**（不是作废）；
      · SSE 收到一个 **`stopped`** 事件，它是**终止性**的（其后没有 `done`）——
        客户端必须能区分"被停了"与"网络断了"，因为前者不该自动重试；
      · **没有可恢复的现场**：运行是被取消的，而官方只对"工具在等审批"这种
        interruption 产出 `RunState`。所以 `stopped` 事件里 `resumable` 恒为
        False，`POST /v1/chat/resume` 对这条会话只会回 409（它要求重发消息）；
      · 计费按已产出结算（与断连同一条路径）。

    为什么不能只靠"客户端断开"
    ----------------------
    断连的语义是"我不要这个响应了"，而停止的语义是"别再花钱了、也别再改我的
    文件了"。两者的副作用完全不同：断连时服务端仍可能在跑工具、写资产；停止要求
    那些动作尽快收手 —— 而这需要一个**别人也能触发的**信号，不能只由那条连接自己
    消失来触发（用户可能只是关了个页面，也可能是另一个管理员要叫停）。

    为什么返回 409 而不是 200 + "没停成"
    ---------------------------------
    409 是"你的请求与当前状态冲突"，正是"当前没有在跑的任务"这件事。回 200 会让
    客户端以为停成功了，于是不再重试、也不再提示用户。

    ⚠️ 重复调用是**幂等**的：同一次运行还在停的过程中，再调一次仍返回同一个
    ``run_id``。理由见 `RunControl.request_stop` 的说明。
    """
    ident = await _off(_authenticate, x_tenant_id, authorization)
    await _off(_rate_limit, request, thread_id, ident.user, True)
    # fail-closed：多租户下归属不符 403、查不到属主 404。
    await _off(_assert_session_readable, thread_id, ident.tenant, ident.user)

    actor = ident.user or ident.tenant
    run_id = await _off(_RUNS.request_stop, thread_id, actor)
    if not run_id:
        raise HTTPException(status_code=409,
                            detail="该会话当前没有正在运行的任务")

    metrics.inc("run_stop_requested_total")
    logging.warning("收到停止请求：session=%s run=%s by=%s", thread_id, run_id, actor)
    return {"thread_id": thread_id, "stopped": True, "run_id": run_id}


@v1.delete("/sessions/{thread_id}")
async def session_delete_v1(thread_id: str, request: Request,
                            x_tenant_id: str = Header(default=""),
                            authorization: str = Header(default="")):
    """DELETE /v1/sessions/{thread_id}：**履行删除请求**（合规要求，此前无处可做）。

    删四类数据，缺一类都算不上"删干净"：

      1. **运行现场** —— 被打断那次运行的 ``RunState`` 快照（里面含整段历史与
         未交付的正文）。数据主体；
      2. **agent 侧跨轮历史** —— ``agent_sessions`` / ``agent_messages`` 两张表，
         存的是"下一轮的模型会读到的对话"（官方 Session 后端）。⚠️ 它与运行现场是
         **两份独立的数据**：只删 1 会留下一整段用户消息与助手产出，而接口却告诉
         调用方"已删除"；
      3. **审核队列记录** —— 待审/已审的草稿正文与审核意见。它独立存表，
         只删运行现场会留下一份"幽灵草稿"；
      4. **归属登记行** —— 不删会让同一个 thread_id 永久挂在旧租户名下：
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
    ident = await _off(_authenticate, x_tenant_id, authorization)
    await _off(_rate_limit, request, thread_id)
    await _off(_assert_session_readable, thread_id, ident.tenant, ident.user)

    deleted = {
        "run_state": await _delete_thread_state(thread_id),
        # agent 侧那份跨轮历史（另一份数据，见 _delete_agent_session）。
        "agent_history": await _delete_agent_session(thread_id),
        "review": await _off(_REVIEW_QUEUE.remove, thread_id),
        "ownership": await _off(_SESSIONS.release, thread_id),
    }
    # 进程内那份"任务执行中补充"的增量索引也要丢：它不落盘，但不丢的话，
    # 同一个 thread_id 被复用时会带着上一段对话的补充 —— 而那份内容刚刚被承诺
    # 删除过（合规口径里"删干净"包含它，它虽然只是副本，但仍是用户内容）。
    from src.agents import supplements
    supplements.clear(thread_id)
    metrics.inc("session_deleted_total")
    logging.info("会话已删除：session=%s tenant=%s deleted=%s",
                 thread_id, ident.tenant, deleted)
    return {"thread_id": thread_id, "deleted": deleted}


@v1.post("/chat/resume")
async def chat_resume_v1(req: ResumeReq, request: Request,
                         x_tenant_id: str = Header(default=""),
                         authorization: str = Header(default=""),
                         idempotency_key: str = Header(default="", alias="Idempotency-Key")):
    """POST /v1/chat/resume：续跑一条**被打断**的会话（SSE，事件与 /v1/chat 相同）。

    req: 请求体 ResumeReq，只有 thread_id —— 要跑的内容早就存在运行现场里了。
    x_tenant_id: 同 /v1/chat。
    idempotency_key: 同 /v1/chat（续跑同样是一次真实生成，重试代价一样大）。

    语义：现场（官方 ``RunState``）记的是"某个工具在等审批"那一刻，所以续跑是
    **从那一步接着走**，不是"接上上次的半个 token"。对客户端而言是"把这次回答
    继续流完"，拿到的是完整、自洽的正文。

    两种状态各自该走哪条路：
        有现场（等人审）→ 409，提示调 POST /v1/review（要人的决定，不是续跑）；
        没有现场        → 409，说明断流后请重发消息。

    ⚠️ 第二条**不是**"本轮已收尾"的同义词，这是最容易被误读的一处：官方只在
    运行中产生 interruption 时才给现场，**客户端断流 / 请求超时都不产生现场**。
    那类崩溃恢复官方刻意不管（``human_in_the_loop.md`` 明确要求生产应用自己提供
    恢复与副作用对账）。所以这里如实回 409 并指路"重发消息"，而不是假装能续 ——
    假装能续的做法是**再跑一轮**，会把已经发出去的 token 重发一遍、把已经落盘的
    资产再做一遍。

    ⚠️ 为什么不做成"自动判断后用一条接口搞定一切"：把"要人的决定"和"机器能自己跑完"
    混在一个接口里，客户端就无法区分"我需要弹审核界面"和"我可以直接继续" ——
    而这正是 human-in-the-loop 里唯一必须由人做的分支。宁可让客户端多判断一次。
    """
    ident = await _off(_authenticate, x_tenant_id, authorization)
    tenant = ident.tenant
    await _off(_preflight, request, session=req.thread_id, tenant=tenant, user=ident.user)

    # ⚠️ 幂等闸门排在状态检查之前 —— 与 `/review` 同一个修正（理由见那里的长注释）：
    # 续跑成功后现场就被消费掉了，于是"同一个键重试"拿到的会是 409
    # "没有可续跑的运行现场"，而不是上一次结果的**重放**。
    gate = await _off(_idempotency_gate, tenant, "resume", idempotency_key,
                      req.model_dump(), ident.user)
    if gate.response is not None:
        return gate.response

    # 先判"是不是在等人审"，再判"有没有现场" —— 顺序反了会把"等人审"报成
    # "没有可续跑的运行"。
    held = await _off(run_store.load, req.thread_id)
    if held:
        await _off(_release_idem_gate, gate, tenant)
        raise HTTPException(status_code=409,
                            detail="该会话正在等待人工审核，请调用 POST /v1/review")

    # ⚠️ 官方**只**在"运行中产生了 interruption"时提供可恢复的现场（``RunState``）。
    # 客户端断流 / 请求超时导致的"跑到一半"没有现场 —— 那类崩溃恢复官方刻意不管，
    # 明确写明"生产应用必须自己提供恢复与副作用对账"（``human_in_the_loop.md``）。
    # 所以这里如实回 409，而不是假装能续：假装能续的做法是**再跑一轮**，
    # 那会把已经发出去的 token 重发一遍、把已经落盘的资产再做一遍。
    await _off(_release_idem_gate, gate, tenant)
    raise HTTPException(
        status_code=409,
        detail="该会话没有可续跑的运行现场（断流后请重发消息；带同一个 "
               "Idempotency-Key 会重放上次的结果）")


app.include_router(v1)


# 旧路径别名：不改客户端的兼容层（功能与 /v1/* 完全一致，仅少一层前缀）
@app.post("/chat", deprecated=True)
def chat_legacy(req: ChatReq, request: Request,
                      x_tenant_id: str = Header(default=""),
                      authorization: str = Header(default=""),
                      idempotency_key: str = Header(default="", alias="Idempotency-Key")):
    """兼容旧路径的 /chat（等价于 /v1/chat）。"""
    return chat_v1(
            req, request,
            x_tenant_id=x_tenant_id, authorization=authorization,
            idempotency_key=idempotency_key)


# ⚠️ 这个别名**必须**跟着被转发者一起是 `async def`：`review_v1` 保持异步
# （它要流式产出 SSE）。少了协程适配，同步函数 `return review_v1(...)` 交出的是
# 一个**协程对象**，FastAPI 会把它丢给 JSON 序列化器，报
# `ValueError: ... coroutine was never awaited`，而请求体看起来"什么都没发生"。
# 这条约束由 tests/test_event_loop_offload.py 的不变量 D 兜住。
#
# 说明写在注释里而不是 docstring 里：FastAPI 会把 docstring 原样搬进 OpenAPI 的
# `description`，那是给客户端看的对外契约，不该出现"协程没 await"这种内部细节。
@app.post("/review", deprecated=True)
async def review_legacy(req: ReviewReq, request: Request,
                        x_tenant_id: str = Header(default=""),
                        authorization: str = Header(default=""),
                        idempotency_key: str = Header(default="", alias="Idempotency-Key")):
    """兼容旧路径的 /review（等价于 /v1/review）。"""
    return await review_v1(
            req, request,
            x_tenant_id=x_tenant_id, authorization=authorization,
            idempotency_key=idempotency_key)


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


def _metrics_peer_allowed(request: Request) -> bool:
    """抓取方能否读 `/metrics`：私网 / 回环对端，或显式关掉白名单。

    为什么判据是"直连对端"而不是 `X-Forwarded-For`
    ----------------------------------------------
    这条检查的目的就是**防绕过 nginx**。XFF 是客户端可写的头，用它做门禁等于
    用可伪造的值守门（只有在 `TRUST_PROXY_HEADERS=1` 且代理**覆盖式**写入时才可信）。
    `request.client.host` 是 TCP 对端地址，伪造不了。

    为什么"无对端 / 非 IP 文本"要**放行**
    ------------------------------------
    - 无 `client`：unix socket 或进程内 ASGI transport（`TestClient`），
      只可能来自本机自身；
    - 非 IP 文本（`TestClient` 的 `"testclient"`、回环上的主机名）：
      它不可能是公网地址。
    这两类若判 deny，结果是"本地与 unix-socket 部署全都抓不到指标" ——
    而它们本来就在本机，属于误伤而不是收紧。
    """
    if not config.METRICS_PRIVATE_ONLY:
        return True
    peer = request.client.host if request.client else ""
    if not peer:
        return True
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return True
    return ip.is_private or ip.is_loopback or ip.is_link_local


# 为什么应用侧要**独立**再拦一道（不依赖 nginx 的 location 判定）：
# nginx 只是其中一条入口 —— 容器 `-p 8000:8000` 直连、k8s NodePort、
# `kubectl port-forward`、Service Mesh 的 sidecar 旁路，任何一条都能绕开它。
# 而 `/metrics` 会暴露进程内存、错误率、租户用量趋势，对攻击者是"该选什么时机打"
# 的情报。**门禁挂在代理上等于只有一个地方在防。**
#
# 说明写在注释而不是 docstring 里：FastAPI 会把 docstring 搬进 OpenAPI 的
# `description`（对外契约），内部取舍不该出现在那里。
@app.get("/metrics", response_class=PlainTextResponse)
def metrics_endpoint(request: Request):
    """Prometheus 文本指标（文本格式，抓取器直接消费）。"""
    if not config.METRICS_ENABLED:
        raise HTTPException(status_code=404, detail="metrics disabled")
    if not _metrics_peer_allowed(request):
        # 刻意回**同一个 404 与同一句文案**（与上面"未启用"完全一致）：
        # 区分两者等于告诉扫描者"端点存在、只是我 IP 不在白名单" —— 那就是情报。
        metrics.inc("metrics_denied_total")
        logging.warning("拒绝非私网对端抓取 /metrics：peer=%s",
                        request.client.host if request.client else "?")
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
