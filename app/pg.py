"""同步 PG 连接池：`sessions` / `accounts` / `review_queue` 三个存储类共用。

为什么需要池
============
这三个类原先每次操作都 `psycopg.connect(self.dsn, ...)`：一次 TCP 握手 + 认证 +
后端进程分配。一次 `/v1/chat` 要经过 2~4 个这样的连接（认领会话、查账号、
入审核队列…），于是：

  - **延迟**：每次操作多出一次建连往返（同机房约 1~3 ms，跨可用区十几 ms）；
  - **连接数**：并发请求数 = PostgreSQL 后端进程数，`max_connections`（默认 100）
    一满，故障形态是"连不上"而不是"排队"—— 一个副本几十并发就能把整库打满，
    多副本时更早；
  - **churn**：每个连接都触发一次 fork/认证，PG 侧 CPU 明显。

这正是 PgBouncer 存在的理由。本项目只有这三个调用方，且它们都在同一进程内，
所以一个**进程内池**就够，不必再引一个独立组件（那是多副本统一控制连接数时
才划算的额外一跳）。

为什么是**同步**池
==================
这三个类的调用点都在工作线程里（`def` 端点由 anyio 线程池接管，真异步端点走
`app/main.py` 的 `_off`），所以既不需要也不应该用 `AsyncConnectionPool`。
好处是语义**逐字对应**：`ConnectionPool.connection()` 本身就是
"借用 → `with conn:`（事务块）→ 归还"，与原先的
`with psycopg.connect(...) as conn:` 完全一致，调用点一行都不用改。

⚠️ 顺带修掉一个隐蔽缺陷：psycopg 的 `with conn:` 只**提交/回滚事务，不关连接**。
原写法靠 CPython 引用计数在对象离开作用域时兜底关闭；改成池之后，"归还"是
确定发生的。

用法
====
    with connect(dsn) as conn:          # 借用 + 事务块 + 归还
        ...

未配 `dsn` 时抛 `PoolNotConfigured`，由调用方降级到内存后端（与原行为一致）。
"""

from __future__ import annotations

import logging
import re
import threading

from . import config

#: dsn -> ConnectionPool。**按 dsn 共享**，不是每个存储类一个池 ——
#: 否则三个类各自 min_size 条连接，总连接数凭空 ×3。
_POOLS: dict[str, object] = {}
_LOCK = threading.Lock()


class PoolNotConfigured(RuntimeError):
    """未配置 `DB_URL`：没有池可用（调用方应降级到内存后端）。"""


class PoolUnavailable(RuntimeError):
    """池建不起来或已经关了。与 `psycopg` 的连接错误同类，调用方按"存储不可用"处理。"""


def _make_pool(dsn: str):
    """真正建池。延迟导入 `psycopg_pool`，未装则抛 ImportError 由调用方降级。"""
    from psycopg_pool import ConnectionPool

    # `open=False` 之后由本模块显式 `open(wait=True)`：
    # 目的是让"连不上"在**建池这一刻**就以异常暴露，而不是变成一个后台线程
    # 反复重连、请求侧只看到含义模糊的 PoolTimeout。诊断信息的可读性值这个代价。
    #
    # ⚠️ max_size 是**每副本**的上限。多副本部署时，总连接数 =
    # max_size × 副本数，必须与 PG 的 max_connections 一起核算（见 deploy/CAPACITY.md）。
    return ConnectionPool(
        conninfo=dsn,
        min_size=config.PG_POOL_MIN,
        max_size=config.PG_POOL_MAX,
        timeout=config.PG_POOL_TIMEOUT,
        # autocommit 与原实现一致：既有代码依赖"每条语句立即生效 + `with conn:`
        # 只是形式上的事务块"。保持它，改造才可能是零语义变更。
        kwargs={"autocommit": True},
        open=False,
        name=f"chatagent-pg-{abs(hash(dsn)) % 100000}",
    )


def _pool_for(dsn: str):
    dsn = (dsn or "").strip()
    if not dsn:
        raise PoolNotConfigured("未配置 DB_URL")

    pool = _POOLS.get(dsn)
    if pool is not None:
        return pool

    with _LOCK:
        pool = _POOLS.get(dsn)      # 双检：可能已被另一个线程建好
        if pool is not None:
            return pool

        pool = _make_pool(dsn)
        try:
            pool.open(wait=True, timeout=config.PG_POOL_TIMEOUT)
        except Exception as e:      # noqa: BLE001
            # 建不起来就别缓存半个池（缓存了会让后续请求一直等 PoolTimeout）。
            pool.close()
            raise PoolUnavailable(
                f"PG 连接池初始化失败（DB_URL 不可达或认证失败）：{e}") from e
        _POOLS[dsn] = pool
        logging.info("PG 连接池已建立：min=%d max=%d timeout=%ss",
                     config.PG_POOL_MIN, config.PG_POOL_MAX, config.PG_POOL_TIMEOUT)
        return pool


def connect(dsn: str):
    """返回一个上下文管理器：进入时借连接并开启事务块，退出时归还。

    用法与 `psycopg.connect(...)` 在 `with` 语句里**一模一样**，这也是本次改造
    零改动调用点的原因：

        with pg.connect(self.dsn) as conn:
            ...
    """
    pool = _pool_for(dsn)
    try:
        return pool.connection()
    except Exception as e:          # noqa: BLE001
        raise PoolUnavailable(f"PG 连接池不可用：{e}") from e


def close_all() -> None:
    """关闭所有池（进程退出时调用）。幂等。"""
    with _LOCK:
        pools = list(_POOLS.values())
        _POOLS.clear()
    for pool in pools:
        try:
            pool.close()
        except Exception as e:      # noqa: BLE001
            logging.warning("关闭 PG 连接池失败（忽略，进程即将退出）：%s", e)


def stats() -> dict[str, dict]:
    """各池的连接统计，供 `/health/ready` 与指标暴露（只读）。"""
    out: dict[str, dict] = {}
    with _LOCK:
        items = list(_POOLS.items())
    for dsn, pool in items:
        try:
            s = pool.get_stats()
        except Exception:           # noqa: BLE001
            continue
        out[_redact_dsn(dsn)] = {
            "size": s.get("pool_size"),
            "available": s.get("pool_available"),
            "waiting": s.get("requests_waiting"),
        }
    return out


def _redact_dsn(dsn: str) -> str:
    """DSN 里含口令 ⇒ 进日志/指标前必须脱敏，只留主机与库名。"""
    return re.sub(r"://[^@/]*@", "://***@", dsn)
