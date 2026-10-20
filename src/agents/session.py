"""跨轮对话历史 —— 用官方 ``Session`` 抽象，后端是 **PostgreSQL**。

为什么后端从 Redis / 本地文件换成了 PG（2026-10-03）
--------------------------------------------------
换的**不是**"哪个更快"，是**这份数据的寿命由谁说了算**：

* 本地 SQLite 文件的后端**没有任何过期概念**，而且多容器部署下每个副本各写各的文件
  ⇒ 用户按请求落在哪个副本就**随机失忆**（与 ``AUTH_CONSTRAINTS`` 里那条"多副本
  必须共享 Redis"是同一类问题）。
* Redis 后端能共享、也带滑动过期，但会话历史是**全量追加**（每次 ``add_items`` 都是
  往列表尾部 push，没有任何裁剪），于是它是内存里增长最快的一份数据；和鉴权白名单、
  限流计数挤在同一个实例里时，内存压力下会**把登录凭证一起淘汰掉**（表现为用户随机掉线）。
* PG 天然多副本共享，且历史是**长期数据**而不是缓存 —— 它应该落在磁盘上、和检查点
  用同一个库。**历史不再有过期时间**（见下）。

历史的寿命
----------
**不设过期、也不清理。** 官方这个 ``SQLAlchemySession`` 后端**没有任何 TTL 参数** ——
这是刻意的选择：用户隔多久回来都应该还能接上上一轮。

**2026-10-03 产品口径已拍板：对话历史永久保留。** 落地方式是三处一致，改之前先对齐：
``RETENTION_DAYS=0``（默认值即"未配置"，``scripts/retention_sweep.py`` 在 0 时**拒绝执行**）、
清理脚本的 ``AGENT_HISTORY_TABLES`` 里**刻意不含** ``agent_sessions`` / ``agent_messages``、
以及 ``PRODUCTION_CONFIG.md`` 的 ``RETENTION_DAYS`` 条目。

⚠️ 由此产生的一处**故意的不对称**：即便有人显式 ``--days N`` 启用清理（本意是清检查点
这类中间态、省空间），这两张表**也不会被删**。检查点丢了只影响续跑，历史丢了是
**用户可见的内容消失且不可恢复** —— 两者生命周期不同，不该共用一个开关。
真要让某轮对话整段消失，正确入口是 ``DELETE /v1/sessions/{id}``
（四类一起删、且由用户主动发起，见 ``app/main.py`` 的 ``_delete_agent_session``）。

要改这个口径只有两条路：给表加清理（并同步改上面三处）或换后端；两条都会牵动
"用户数据留多久"这个产品承诺，不是随手能改的开关。

历史怎么进模型（**"取多少"不在这里**）
------------------------------------
本模块只负责"取历史 + 并上本轮增量 + 写回"。**取多少 / 送多少**由
``src/agents/history_budget.py`` 按 token 裁（官方 ``RunConfig.call_model_input_filter``）。
所以这里**刻意不设条数上限**（``SessionSettings`` 保持默认的"全取"）：
先把全量历史取出来，再由预算那一层决定送多少 —— 两处都设上限的话，条数那份会**先**
把历史切掉，token 那层就永远看不到真实的总量。

⚠️ 代价要知情：全量取回意味着**单次读的行数随对话长度线性增长**，而预算那一层会在
**每次模型调用**前把整份输入重新数一遍 token（一次 run 内可能调用多次模型）。

为什么这里"不"每请求新建引擎
--------------------------
``create_async_engine`` 每次调用都会建一个**新的连接池**，且不会自己关。每条请求建一个
= 连接数随 QPS 线性上涨，直到戳穿 PG 的 ``max_connections``。本模块持有**进程内唯一**
的异步引擎，所有会话对象共用它（官方文档写的也是"pass a pre-configured engine your
application already manages"）。

⚠️ 本模块导入 ``agents``（SDK）。因此只能被 ``src/agents/*`` 里同样是惰性加载的模块
引用（``runner``），**不要**在包入口 eager import 它 —— 否则"缺 SDK"的报错会变成
import 期的堆栈，而不是启动守卫那句点名该装什么的 ``missing_sdk``
（见 ``src/agents/__init__.py`` 的说明）。

⚠️ **不在导入期连库**：``create_async_engine`` 只构造对象、不建连（真正的连接推迟到
第一条命令）。所以"地址写错 / PG 没起"要到用时才暴露 —— 这正是
``ping_session_backend()`` 存在的理由（启动期那次自检会打一行"跨轮对话历史就绪 /
不可用"的结论）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict
from typing import Any

from .. import observability
from ..envutil import env_int

__all__ = [
    "clear_session",
    "dispose_session_backend",
    "ping_session_backend",
    "reset_sessions",
    "session_backend_intent",
    "session_backend_ok",
    "session_dsn",
    "session_for",
    "to_async_dsn",
]

#: 进程内缓存的会话对象上限。会话对象很轻（一个 id + 引擎引用），
#: 但有界是硬要求：无界意味着"构造一个陌生 thread_id 就能吃内存"，那是 DoS 面。
_CACHE_MAX = env_int("AGENT_SESSION_CACHE_MAX", 256, lo=1)

_sessions: OrderedDict[str, Any] = OrderedDict()
#: 进程内唯一的异步引擎。``None`` = 还没建过（**不代表连不上**，见模块 docstring）。
_engine: Any = None
#: 后端可用性。``None`` = 还没探过（启动自检之前）。
_backend_ok: bool | None = None
#: 首次失败已经打过完整日志了吗（防日志放大：后端起不来时每条请求都会走到这里）。
_failure_logged = False


def _db_url() -> str:
    """PG 连接串（``DB_URL``）。空 ⇒ 没有会话后端。

    ⚠️ 链路层（``src/agents/*``）直接读环境变量、不走 ``app/config.py``：理由与
    ``DOCS_DIR`` 完全同构 —— 链路层在**请求期**就要拿到这个值，走 config 会形成
    ``src/ → app/`` 的反向依赖。两边各读一次，``app/config.py`` 那一份的用途是
    **启动自检回显**。
    """
    return (os.getenv("DB_URL", "") or "").strip()


def to_async_dsn(url: str) -> str:
    """把任意 PG 连接串补上异步驱动名（**纯函数**，迁移脚本也用它）。

    ⚠️ **必须带驱动名**：``postgresql://`` 是同步写法，直接喂给
    ``create_async_engine`` 会报 "The asyncio extension requires an async driver"。
    本仓库装的是 psycopg 3（``psycopg==3.3.3``，它自带异步支持），所以前缀写
    ``postgresql+psycopg://``；**不是** ``+asyncpg``（那个包没装，也没必要装第二套
    驱动 —— 检查点那条路用的就是 psycopg）。

    已经是 ``xxx+driver://`` 形态时原样返回，不重复加。做成纯函数是因为它有两个
    调用方（本模块的引擎、``app/migrations.py`` 的建表迁移），而"连接串怎么拼"
    只能有一处 —— 两边各写一遍必然漂。
    """
    url = (url or "").strip()
    if not url:
        return ""
    scheme, sep, rest = url.partition("://")
    if not sep or "+" in scheme:
        return url
    return f"{scheme}+psycopg{sep}{rest}"


def session_dsn() -> str:
    """当前配置（``DB_URL``）对应的异步连接串。"""
    return to_async_dsn(_db_url())


def _ping_timeout() -> float:
    """启动自检的连通性探测超时（秒）。

    为什么必须给超时：不设的话，PG 主机**不可达**（而不是"拒绝连接"）时，psycopg 会
    按操作系统的连接超时慢慢等 —— 启动会被拖住几十秒到几分钟，看起来像"服务起不来了"。
    """
    raw = (os.getenv("AGENT_SESSION_PING_TIMEOUT", "") or "").strip()
    if not raw:
        return 5.0
    try:
        return max(0.5, float(raw))
    except ValueError:
        logging.warning("AGENT_SESSION_PING_TIMEOUT 不是数字（= %r），改用默认 5s", raw)
        return 5.0


def session_backend_intent() -> str:
    """**打算**用哪个后端（纯配置，不做任何 I/O）。

    与 ``session_backend_ok()`` 配对使用，理由同 ``rate_limit`` 的
    "shared_configured vs backend"：配了 ``DB_URL`` **不等于**后端真的可用
    （PG 没起、库不存在、驱动没装都会失败）。只回显前者会谎报
    "多副本下历史是共享的"，而那正是最危险的那类错误 —— 它会表现成"同一段对话
    偶尔失忆"，看起来像模型的毛病。
    """
    return "postgres" if _db_url() else "none"


def session_backend_ok() -> bool | None:
    """后端可用性：``True`` / ``False`` / ``None``（还没探过）。"""
    return _backend_ok


def _engine_or_raise() -> Any:
    """进程内唯一的异步引擎（懒建，**不在此处建连**）。"""
    global _engine
    if _engine is None:
        dsn = session_dsn()
        if not dsn:
            # 显式抛而不是让 `create_async_engine("")` 抛它的内部错误：那句话
            # （ArgumentError / 空 URL）读起来像是"驱动装错了"，会把排查带偏。
            raise RuntimeError("未配置 DB_URL：跨轮对话历史没有后端")
        from sqlalchemy.ext.asyncio import create_async_engine

        _engine = create_async_engine(
            dsn,
            # 中间设备/防火墙静默掐断连接后，池里会留着"看起来还在"的死连接 ——
            # 不 pre_ping 的话它要到真正执行 SQL 时才报错，且报在下游随机一处。
            pool_pre_ping=True,
            # 连接最长活 30 分钟，配合 PG/代理侧的 idle 超时，避免拿到已关闭的连接。
            pool_recycle=1800,
        )
    return _engine


def _build(session_id: str) -> Any:
    """按当前配置构造一个会话对象（不缓存、不自检）。

    刻意**不传** ``session_settings``（保持官方默认的"全取"）—— "取多少"由
    ``history_budget`` 按 token 决定，见模块 docstring。

    ``create_tables`` 保持官方的生产默认（``False``）：建表属于迁移的职责
    （``app/migrations.py`` 的 ``agent_sessions`` / ``agent_messages``），
    不在这里顺手建 —— 那样多副本同时启动会互相抢 DDL。

    ★ 2026-10-08：外面套一层 ``CompactingSession``（见 ``compaction.py``）。
    **只在这里套一次**，全进程拿到的都是同一个被包住的实例 —— 因为
    ``session_for`` 有进程内缓存，"同一个会话被两处拿到不同对象"本来就不会发生。
    包在缓存**里面**（而不是 `session_for` 的返回点上）是必须的：压缩入口与
    "本轮的会话写入"要共用**同一把锁**，那把锁挂在装饰器上；两处各包一次就变成
    两把锁，等于没锁。

    ⚠️ 装饰器是**透明**的（四个协议方法原样透传），所以"取历史 / 写回 / 清空"
    的语义一个都没变 —— 换掉的只在"允许主动重写历史"这一件事上。
    """
    from agents.extensions.memory import SQLAlchemySession

    from .compaction import CompactingSession

    inner = SQLAlchemySession(session_id, engine=_engine_or_raise())
    return CompactingSession(inner)


def _note_failure(exc: BaseException) -> None:
    """记一次会话后端故障。

    为什么"只打第一条日志、其余只计数"：后端起不来时**每条请求**都会走到这里，
    全量打日志就是一次自我制造的日志放大（几百 QPS 下能把日志盘打满，
    而排查所需信息第一条就有了）。计数去重报，日志去首条。
    """
    global _failure_logged
    observability.counter("agent_session_errors_total", kind=type(exc).__name__)
    if _failure_logged:
        logging.debug("会话后端仍不可用：%s", exc)
        return
    _failure_logged = True
    logging.error(
        "跨轮对话历史不可用（%s），本轮起**每轮只有用户新说的这一句**，模型看不到上文 —— "
        "用户会表现为「接着改上一轮的东西」时说不知道在指什么。后端=%s；"
        "检查 DB_URL 是否配了、PG 是否可达、库与表是否已迁移"
        "（`python -m app.migrations status`）、sqlalchemy 是否装上。原因：%s",
        type(exc).__name__,
        session_backend_intent(),
        exc,
    )


def session_for(session_id: str) -> Any:
    """取（并缓存）某个会话的历史存储；**任何失败都返回 ``None``**。

    session_id: 会话编号。本项目直接用 ``thread_id`` —— "对话线程"与
        "检查点线程"本来就是同一个概念，多一个 id 只会多一处对不上的机会。

    返回 ``None`` 有明确含义：**本轮没有跨轮历史**。调用方必须据此退回
    "只把本轮输入交给模型"，而不是抛出去 —— 丢历史是降级，不是失败。
    """
    global _backend_ok
    sid = (session_id or "").strip()
    if not sid:
        return None
    if not _db_url():
        # 提前判：没有 DB_URL 就没有后端。不提前判的话会走 `_engine_or_raise` 抛错再
        # 被兜住 —— 结果一样，但**当引擎已经被建过**（另一个 DSN）时，那一步会拿到
        # 一个缓存的引擎并"成功"返回一个连不上任何库的会话对象，降级发生在更晚、
        # 更难解释的地方。这条分支在测试里就抓到过一次。
        return None

    hit = _sessions.get(sid)
    if hit is not None:
        _sessions.move_to_end(sid)
        return hit

    try:
        built = _build(sid)
    except Exception as e:  # noqa: BLE001  sqlalchemy 缺失 / 连接串非法 / 引擎建不起来
        _backend_ok = False
        _note_failure(e)
        return None

    _backend_ok = True
    _sessions[sid] = built
    while len(_sessions) > _CACHE_MAX:
        _sessions.popitem(last=False)
    return built


async def load_items(session_id: str) -> list[Any]:
    """读某个会话的历史条目（官方 ``Session.get_items``）；失败返回空列表。

    为什么需要它：``GET /v1/sessions/{id}`` 要回答"这条会话说过什么"。这份历史
    现在**只有官方会话存着** —— 图退役之后没有第二份了。不读它，断流重连时
    客户端就只能拿到自己本地缓存的那半段。

    降级口径与 ``session_for`` 一致：没有后端 / 读失败都返回空列表，不上抛。
    "拿不到历史"不该让一个只读端点变成 500。
    """
    sid = (session_id or "").strip()
    if not sid:
        return []
    session = session_for(sid)
    if session is None:
        return []
    try:
        items = await session.get_items()
    except Exception as e:  # noqa: BLE001  后端不可用 ⇒ 按"没有历史"处理
        _note_failure(e)
        return []
    return list(items or [])


async def clear_session(session_id: str) -> bool:
    """清空某个会话的**跨轮历史**。返回是否真的清了（没有后端 / 后端不可用 = False）。

    为什么这个函数必须存在
    --------------------
    这份历史与 LangGraph 的检查点是**两份独立的数据**（见模块 docstring 与
    ``DESIGN_agent_architecture.md``）。``DELETE /v1/sessions/{thread_id}`` 只删检查点
    那几张表时，这里的消息**一条不动** —— 于是接口 docstring 里那句"缺一类都算不上
    删干净"是假的，而它在合规上承诺的是"删除请求"。

    为什么顺手把缓存里的会话对象丢掉：官方后端每次调用都读库（不在内存里缓存
    items），所以清完不丢也是对的；但"清完之后还有人拿着一份旧的"这种失效方式
    **没有任何症状**（下一轮照常接上已经承诺删除的上下文），丢一个引用换掉这个
    风险是划算的。

    调用方（``app/main.py`` 的删除端点）**不把失败当致命**：删除是幂等的，
    明细里如实告诉调用方每一项到底删掉没有，比抛出去让整个请求失败更有用。
    """
    sid = (session_id or "").strip()
    if not sid:
        return False
    session = session_for(sid)
    if session is None:
        return False
    try:
        await session.clear_session()
    except Exception as e:  # noqa: BLE001  后端不可用时按"没清掉"记
        _note_failure(e)
        return False
    _sessions.pop(sid, None)
    return True


async def ping_session_backend() -> bool:
    """启动自检：**真的连一次**。返回能否用，**不抛异常**。

    为什么必须主动探：``create_async_engine`` 是**懒连接** —— 地址写错、PG 没起、
    库不存在都不会在构造期报错，要到第一次 ``get_items`` 才炸。而那一步在
    ``Runner.run`` **内部**，会被 ``run_agent`` 的宽 ``except`` 接住 ⇒
    ``exit_reason="error"``。于是"DB_URL 配错"表现成"agent 链路动不动答不出来"，
    排查起点完全错。
    """
    global _backend_ok
    if not _db_url():
        _backend_ok = False
        return False

    async def _probe() -> None:
        from sqlalchemy import text

        async with _engine_or_raise().connect() as conn:
            await conn.execute(text("SELECT 1"))

    try:
        await asyncio.wait_for(_probe(), timeout=_ping_timeout())
    except Exception as e:  # noqa: BLE001  连不上 / 超时 / 权限不足
        _backend_ok = False
        _note_failure(e)
        return False
    _backend_ok = True
    return True


def reset_sessions() -> None:
    """清空进程内的会话缓存、**引擎引用**与自检结论（**测试用**）。

    ⚠️ 它**不 await 释放连接池**（那是 ``dispose_session_backend()`` 的事，异步）。
    所以只适合测试收尾：那里引擎通常一次都没真正连过库，丢弃引用就够了。
    生产关闭路径**必须**走 ``dispose_session_backend()`` —— 丢掉引用而不关连接，
    PG 侧会一直挂着这些连接直到自己超时。

    为什么连引擎引用一起清：不清的话，"改了 ``DB_URL`` 再取会话"会**复用上一个
    环境变量建出来的引擎**（测试里就是上一个用例的），于是"没配 ``DB_URL`` 应该
    降级"这类断言会被一个陈旧引擎骗过去。
    """
    global _engine, _backend_ok, _failure_logged
    _engine = None
    _sessions.clear()
    _backend_ok = None
    _failure_logged = False


async def dispose_session_backend() -> None:
    """释放进程内引擎并复位全部状态（关闭时调用；测试收尾也用它）。

    为什么必须显式释放：uvicorn 的关闭不碰第三方连接池，PG 侧会一直挂着这些连接
    直到它自己超时 —— 滚动发布时几个副本叠起来就是"连接数莫名逼近上限"。
    """
    global _engine, _backend_ok, _failure_logged
    engine, _engine = _engine, None
    _sessions.clear()
    _backend_ok = None
    _failure_logged = False
    if engine is None:
        return
    try:
        await engine.dispose()
    except Exception as e:  # noqa: BLE001  关闭路径上的失败只记不抛
        logging.warning("会话引擎释放失败（不影响关闭）：%s", e)
