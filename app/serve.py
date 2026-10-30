"""生产启动入口：参数解析 + 优雅关闭 + 事件循环工厂。

⚠️ 事件循环工厂的历史（2026-10-20 更新，别按旧理由改它）
------------------------------------------------------
它原本**必须存在**：检查点用的是 `AsyncPostgresSaver`（psycopg 异步），而 psycopg
异步不支持 Windows 的 ProactorEventLoop —— uvicorn 在 win32 上默认就是 Proactor，
且 0.36+ 用 `asyncio.run(coro, loop_factory=...)` 直接指定循环、**不再走事件循环
策略**，所以"导入期 `set_event_loop_policy`"那种常见写法在这里完全无效。

**那个组件已经随编排图一起退役**：现在所有 PG 访问都是同步 psycopg + 工作线程
（见 `app/pg.py` 的「为什么是同步池」），没有任何 psycopg 异步调用。因此
`loop_factory` 不再是硬要求，只剩两层作用：

  1. Linux/macOS 上顺手用上 uvloop（装了就用，性能更好）；
  2. win32 上仍返回 `SelectorEventLoop` —— 保守选择，避免"某个库在 Proactor 上
     行为不同"这类不可预测的差异被引进一次启动路径变更里。

要不要删掉它、改回裸 `uvicorn app.main:app`：**等有人愿意同步改 Dockerfile、
compose、CI 与 RUNBOOK 的启动命令时再一起做**，单独删这一个函数只会让文档与
部署脚本指着一个不存在的入口。

用法
----
    python -m app.serve                       # 推荐：一条命令，已内置工厂
    uvicorn app.main:app --loop app.serve:loop_factory   # 等价的手写形式

可用环境变量：APP_HOST / APP_PORT / APP_WORKERS / APP_LOG_LEVEL / APP_RELOAD /
GRACEFUL_SHUTDOWN_TIMEOUT（命令行参数优先于环境变量）

上述变量与 `AGNES_*` 等**都可以写在 `.env` 里**：本模块在导入期就把 `.env` 灌进
环境（理由见 `import` 之后那段注释），而 argparse 的默认值是在 `main()` 里才读的。

worker 数：`APP_WORKERS` 优先，其次 `WEB_CONCURRENCY`（容器/PaaS 的惯例名，很多平台
会自动注入），默认 1。两个名字都认，是为了避免"Dockerfile 里用 WEB_CONCURRENCY、
文档里写 APP_WORKERS"导致**设了却不生效**——那类静默失效最难排查。

优雅关闭：`GRACEFUL_SHUTDOWN_TIMEOUT`（默认**跟随 `REQUEST_TIMEOUT_SECONDS`**，
即"总时限 + 10s 收尾余量"，未配总时限时 30s）是在途请求的排空上界。
**改动时必须同步 `deploy/app/docker-compose.yml` 的 `stop_grace_period`**（要大于它），
否则容器会在排空完成前被 SIGKILL —— 那正是"发布时个别用户回答断掉"的成因。
反过来也有一条：排空上界若**小于**请求总时限，等于发布时主动取消那些"本来还被允许
继续跑"的请求。两个方向都由 `tests/test_graceful_shutdown.py` 守着。
"""
import argparse
import asyncio
import os
import sys

# ---------------------------------------------------------------------------
# `.env` 必须在**任何 app 模块被导入之前**加载
# ---------------------------------------------------------------------------
# 真实事故（2026-10-30 实测）：只配 `.env` 时，`python -m app.serve` 报
#   RuntimeError: agent 链路不可用（missing_api_key）⇒ 拒绝启动
# 而 `.env` 里 `AGNES_API_KEY` 明明有值。
#
# 成因是**加载顺序**，不是配置写错：
#   1. `main()` 调 `resolve_graceful_timeout()`，它经 `_default_graceful_timeout()`
#      触发 `from app import config` —— 这发生在 `uvicorn.run()` **之前**；
#   2. `app/config.py` 的常量是**模块级** `API_KEY = _env_str(...)`，导入那一刻
#      就求值，此时 `.env` 还没人读 ⇒ 固化成一个空串；
#   3. 之后 uvicorn 才导入 `app.main`，它里面的 `load_dotenv()` 已经太晚 ——
#      `app.config` 在 `sys.modules` 里，不会重新求值。
# 测试与探针走的是 `import app.main`，不经过 `resolve_graceful_timeout`，所以
# `load_dotenv()` 恰好先于 `config` 导入 ⇒ **单测全绿，真实入口必挂**。
#
# 为什么不放到 `app/config.py` 顶部（看起来更"一劳永逸"）
# -----------------------------------------------------
# 那样会破坏 `tests/conftest.py` 的环境隔离机制：它在导入 `app.config` **之前**
# 把测试身份（`AUTH_SESSION_SECRET` / `AGNES_API_KEY`）注入环境，而它包装的
# `load_dotenv` 会在加载后调用 `scrub_behavior_env()` 把**所有**行为变量删掉 ——
# 于是 config 读到的会话密钥变成空串，全量用例集体 401。顺序是那边刻意设计并
# 写进注释的，不能为省一行把它挪走。
#
# 所以放在**进程入口**这一层：只有 `-m app.serve`（以及 `--reload` 的子进程）走
# 这条路，测试进程不受影响。其它入口（`app/migrations`、`scripts/*`）本来各自
# 显式 `load_dotenv()`。
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:                          # noqa: BLE001  dotenv 是可选依赖
    pass


def loop_factory(use_subprocess: bool = False):
    """给 uvicorn 的事件循环工厂，返回一个**已构造**的事件循环实例。

    use_subprocess: 兼容 uvicorn 命名工厂的调用约定；本函数不区分
        （子进程/非子进程都需要 psycopg 兼容的循环）。

    win32 → SelectorEventLoop（psycopg 异步的硬要求，没有例外）；
    其它平台 → 有 uvloop 用 uvloop，否则 SelectorEventLoop。
    """
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    try:
        import uvloop  # 可选依赖；Linux/macOS 上更快
        return uvloop.new_event_loop()
    except Exception:                       # noqa: BLE001
        return asyncio.SelectorEventLoop()


def resolve_workers() -> int:
    """解析 worker 数：`APP_WORKERS` → `WEB_CONCURRENCY` → 1。

    为什么认两个名字：本项目的 Dockerfile / docker-compose 历史上用
    `WEB_CONCURRENCY`（uvicorn/gunicorn 生态惯例），而 `app.serve` 读 `APP_WORKERS`。
    两边名字不一致时，运维设了其中一个会被另一个的默认值**静默覆盖** ——
    表现为"我明明配了 4 个 worker，容器里还是 1 个"，日志里也没有任何痕迹。
    统一在这里解析并回显，两个名字都生效。

    非法值（非正整数）忽略并打印，不抛异常：一个手滑的 `.env` 不该让服务起不来。
    返回 int（至少 1）。
    """
    for name in ("APP_WORKERS", "WEB_CONCURRENCY"):
        raw = (os.getenv(name) or "").strip()
        if not raw:
            continue
        try:
            n = int(raw)
        except ValueError:
            print(f"[serve] {name}={raw!r} 不是整数，已忽略", flush=True)
            continue
        if n < 1:
            print(f"[serve] {name}={n} 小于 1，已忽略", flush=True)
            continue
        return n
    return 1


# 优雅关闭的排空上界（秒）。默认 30。
#
# 为什么必须设一个**上界**
# ---------------------
# uvicorn 的 `timeout_graceful_shutdown` 默认是 `None`，含义是"**永远**等在途请求
# 结束"。对本服务（SSE 长连接）来说这个默认值是危险的：
#   - 一个挂住的请求（上游不吐字、客户端又不断开）能让关闭**无限期**卡住；
#   - 卡住之后，容器运行时按自己的宽限期收场（`docker stop` 默认 10s → SIGKILL）。
#     被 SIGKILL 才是真的"掐断在途流"，而且进程没有任何机会记账或清理 ——
#     表现成"发布时偶尔有几个用户的回答直接断掉"，还查不到日志。
# 所以这里显式封顶，让关闭行为**可预测**：到点就记一条明确的日志再取消。
#
# ⚠️ 必须与 `docker-compose.yml` 的 `stop_grace_period` 一起改：
#    compose 的值要**大于**这个上界（留出排空 + 进程退出的时间）。
#    只改一处等于没改 —— 若 compose 更小，容器会在我们排空完成前就被 SIGKILL。
#
# 取值口径（2026-10-21 修正）：**默认值跟随请求总时限**，不再钉死 30s。
# 原来的 30s 是按"单次模型调用 30s"推的，但一次请求可以跑十几圈（见
# `AGENT_MAX_TURNS`），请求总时限早已配到 180s —— 于是"排空 30s"意味着
# **任何跑过 30 秒的请求都会在发布时被取消**，而它本来还能再跑 150 秒正常完成。
# 用户看到的是"回答断在半句"，日志里只有一条"排空超时"，看起来像是个别请求卡住。
_DRAIN_UNLIMITED_FALLBACK = 30
"""请求总时限**未配置**（0 = 不限）时的排空上界。

这种情况下没有任何取值是对的：一个请求可以跑到永远，排空上界再大也只是早死晚死。
所以这里给一个保守值，并在 `resolve_graceful_timeout` 里明确提醒"生产必须配
`REQUEST_TIMEOUT_SECONDS`" —— 那条配置本来就在 `PRODUCTION_CONFIG.md` 的必配项里。
"""

_DRAIN_MARGIN_SECONDS = 10
"""排空上界比请求总时限多留的那一点。

留的不是"再跑一会儿"，而是**收尾动作**：总时限到点后还要下发 timeout 事件、
结算配额、写待审队列、关上游连接，这些都跑在请求自己的执行栈里。把排空上界卡得
和总时限一样紧，等于每次都在收尾中途把协程取消掉 —— 那正是"额度扣了、记录没落"的成因。
"""


def _default_graceful_timeout() -> int:
    """默认排空上界 = 请求总时限 + 收尾余量（未配总时限时退回 30s）。"""
    try:
        from app import config
    except Exception:                       # noqa: BLE001  配置都导不进来时别挡住启动
        return _DRAIN_UNLIMITED_FALLBACK
    request_timeout = int(getattr(config, "REQUEST_TIMEOUT_SECONDS", 0) or 0)
    if request_timeout <= 0:
        return _DRAIN_UNLIMITED_FALLBACK
    return request_timeout + _DRAIN_MARGIN_SECONDS


def resolve_graceful_timeout() -> int:
    """解析排空上界：`GRACEFUL_SHUTDOWN_TIMEOUT`（秒），默认跟随请求总时限。

    默认值不是常数，见 `_default_graceful_timeout`：`REQUEST_TIMEOUT_SECONDS=180`
    ⇒ 排空上界 190，`stop_grace_period` 必须大于它。

    非法/非正值忽略并打印，取默认 —— 与 `resolve_workers` 同一口径：
    配置错误不该让服务起不来。
    """
    fallback = _default_graceful_timeout()
    raw = (os.getenv("GRACEFUL_SHUTDOWN_TIMEOUT") or "").strip()
    if not raw:
        return fallback
    try:
        n = int(raw)
    except ValueError:
        print(f"[serve] GRACEFUL_SHUTDOWN_TIMEOUT={raw!r} 不是整数，已忽略（用 {fallback}）", flush=True)
        return fallback
    if n < 1:
        print(f"[serve] GRACEFUL_SHUTDOWN_TIMEOUT={n} 小于 1，已忽略（用 {fallback}）", flush=True)
        return fallback
    if n < fallback:
        # 显式配小了：**照用**（配置错误不该让服务起不来），但必须说出来。
        # 这条最容易被当成"发布调快一点"的优化，代价是长回答被截断，且症状不可归因。
        print(
            f"[serve] ⚠️ GRACEFUL_SHUTDOWN_TIMEOUT={n} 小于请求总时限对应的排空上界"
            f"（{fallback}）⇒ 滚动发布时跑过 {n}s 的在途请求会被取消，回答断在半句。"
            "要么调大它，要么调小 REQUEST_TIMEOUT_SECONDS。",
            flush=True,
        )
    return n


def main(argv=None):
    """解析参数并启动 uvicorn。

    argv: 命令行参数列表（None = 取 sys.argv）。返回进程退出码。
    """
    ap = argparse.ArgumentParser(
        prog="python -m app.serve",
        description="chatAgent 生产启动入口（自动适配 Windows 事件循环限制）",
    )
    ap.add_argument("--host", default=os.getenv("APP_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.getenv("APP_PORT", "8000")))
    ap.add_argument("--workers", type=int, default=resolve_workers())
    ap.add_argument("--log-level", default=os.getenv(
        "APP_LOG_LEVEL", os.getenv("LOG_LEVEL", "info")).lower())
    ap.add_argument("--reload", action="store_true",
                    default=os.getenv("APP_RELOAD", "").strip().lower() in
                    ("1", "on", "true", "yes"))
    args = ap.parse_args(argv)

    import uvicorn

    graceful = resolve_graceful_timeout()
    if sys.platform == "win32":
        print(f"[serve] win32：事件循环已固定为 Selector（psycopg 异步要求）；"
              f"workers={args.workers}", flush=True)
    print(f"[serve] 优雅关闭排空上界 {graceful}s"
          f"（容器 stop_grace_period 必须大于它，否则会被 SIGKILL）", flush=True)

    # loop 传"本模块:工厂"而不是内置名，才能覆盖 uvicorn 在 win32 上的 Proactor 默认值。
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        workers=args.workers,
        log_level=args.log_level,
        reload=args.reload,
        loop="app.serve:loop_factory",
        # 显式封顶：不设的话 uvicorn 默认 None = 永远等在途请求，
        # 一个挂住的 SSE 能把关闭无限期拖住，最后被容器运行时 SIGKILL。
        timeout_graceful_shutdown=graceful,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
