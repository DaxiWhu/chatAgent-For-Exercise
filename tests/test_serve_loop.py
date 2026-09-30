"""启动入口的事件循环工厂：这是"异步检查点能不能用"的开关。

背景：`AsyncPostgresSaver` 底层 psycopg 异步**不支持 Windows ProactorEventLoop**，
而 uvicorn 在 win32 无子进程时**故意**选 Proactor。`app.serve.loop_factory` 负责
把它纠正成 Selector。这里钉住"win32 必须给出 Selector"这条硬约束 —— 一旦被
改回 Proactor/默认循环，检查点会**静默**降级为内存（服务照常起，只是重启丢状态）。
"""
import asyncio
import sys

from app import serve


def test_loop_factory_is_selector_on_win32(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    loop = serve.loop_factory()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop), \
            "win32 上必须返回 SelectorEventLoop，否则 psycopg 异步会直接报错"
    finally:
        loop.close()


def test_loop_factory_returns_loop_instance_elsewhere(monkeypatch):
    """非 win32：uvloop 有则用、无则 Selector；无论如何必须是**已构造的实例**。

    必须是实例而不是类 —— uvicorn 对 `--loop <模块:函数>` 这条自定义路径是
    `import_from_string` 后**直接**作为 loop_factory 传给 `asyncio.run`，
    而 asyncio 会 `loop_factory()` 取实例（见 uvicorn 的命名工厂返回的是类，
    两者调用约定不同，这里选择"返回实例"以适配自定义路径）。
    """
    monkeypatch.setattr(sys, "platform", "linux")
    loop = serve.loop_factory()
    try:
        assert isinstance(loop, asyncio.AbstractEventLoop)
    finally:
        loop.close()


def test_loop_factory_tolerates_use_subprocess_kwarg(monkeypatch):
    """兼容 uvicorn 命名工厂的调用约定（带 use_subprocess 关键字）。"""
    monkeypatch.setattr(sys, "platform", "win32")
    loop = serve.loop_factory(use_subprocess=True)
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# worker 数解析：两个环境变量名字都要生效
# ---------------------------------------------------------------------------
# 背景：Dockerfile/compose 用 WEB_CONCURRENCY，文档与代码用 APP_WORKERS。
# 名字不一致时运维设了其中一个会被默认值**静默覆盖**（"配了 4 个实际起 1 个"，
# 日志无痕）。这里钉住优先级，避免"改了其中一个名字"悄悄让配置失效。
# ---------------------------------------------------------------------------
def test_resolve_workers_defaults_to_one(monkeypatch):
    """两个都没设 → 1。

    默认必须是 1 而不是 2：多 worker 各自持有检索器与限流计数，
    没配 REDIS_URL 时**实际额度翻倍**，这是安全默认值问题。
    """
    monkeypatch.delenv("APP_WORKERS", raising=False)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    assert serve.resolve_workers() == 1


def test_resolve_workers_honours_app_workers(monkeypatch):
    monkeypatch.setenv("APP_WORKERS", "4")
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    assert serve.resolve_workers() == 4


def test_resolve_workers_falls_back_to_web_concurrency(monkeypatch):
    """容器/PaaS 惯例名也要生效 —— 这是 Dockerfile/compose 实际用的名字。"""
    monkeypatch.delenv("APP_WORKERS", raising=False)
    monkeypatch.setenv("WEB_CONCURRENCY", "3")
    assert serve.resolve_workers() == 3


def test_resolve_workers_prefers_app_workers_over_web_concurrency(monkeypatch):
    monkeypatch.setenv("APP_WORKERS", "2")
    monkeypatch.setenv("WEB_CONCURRENCY", "8")
    assert serve.resolve_workers() == 2


def test_resolve_workers_ignores_garbage_without_crashing(monkeypatch):
    """非法值不能让服务起不来：忽略并继续往下找。"""
    monkeypatch.setenv("APP_WORKERS", "many")
    monkeypatch.setenv("WEB_CONCURRENCY", "5")
    assert serve.resolve_workers() == 5        # 跳过非法的 APP_WORKERS，用下一个

    monkeypatch.setenv("APP_WORKERS", "0")     # 0/负数同样非法
    assert serve.resolve_workers() == 5
