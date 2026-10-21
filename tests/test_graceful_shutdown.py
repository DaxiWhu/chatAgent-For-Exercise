"""优雅关闭（排空）与容器加固。

先澄清一件事：真正"放完在途请求"的是谁
-----------------------------------
2026-09-29 读了 uvicorn 源码后确认（不要凭印象）：

  `uvicorn.Server.shutdown()` 先 `server.close()` 停止接受新连接，再对每个在途
  连接调 `connection.shutdown()`；而对**响应尚未结束**的连接，它只设
  `keep_alive = False`（`protocols/http/h11_impl.py::RequestResponseCycle.shutdown`），
  然后 `_wait_tasks_to_complete()` 等它写完。**响应不会被切断。**

所以"滚动更新会掐断在途 SSE"这句话，在 uvicorn 这一层是**不成立**的。
真正的风险在两处，本文件针对这两处：

  ① uvicorn 的 `timeout_graceful_shutdown` 默认 `None` = **永远等**。
     一个挂住的请求（上游不吐字、客户端不断开）能把关闭无限期拖住。
  ② 拖住之后，容器运行时按自己的宽限期收场 —— `docker stop` 默认 **10s** 就
     SIGKILL。被 SIGKILL 才是真的掐断，且不给进程任何记账/清理机会。

⇒ 修法是"给排空一个上界" + "让容器宽限期大于它"，两者必须联动。
本文件同时锁住"在途请求可观测"，否则无法回答"这次发布打断了几个人"。
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import pytest
import yaml

from app import middleware as MW

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "deploy" / "app" / "docker-compose.yml"


# ---------------------------------------------------------------------------
# 在途请求计数
# ---------------------------------------------------------------------------
class _HangApp:
    """可注入的 ASGI app：挂到事件被 set 为止，用于模拟长请求。"""

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, scope, receive, send):
        self.started.set()
        await self.release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _scope():
    return {"type": "http", "method": "POST", "path": "/v1/chat", "headers": []}


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def test_in_flight_count_rises_during_request():
    """请求进行中计数必须 >0 —— 这是"这次发布有没有打断回答"的唯一依据。"""
    app = _HangApp()
    mw = MW.RequestContextMiddleware(app, enabled=False)
    sent = []

    async def send(msg):
        sent.append(msg)

    async def run():
        task = asyncio.create_task(mw(_scope(), _receive, send))
        await app.started.wait()
        during = MW.in_flight_count()
        app.release.set()
        await task
        return during, MW.in_flight_count()

    during, after = asyncio.run(run())
    assert during == 1, f"请求进行中计数应为 1，实际 {during}"
    assert after == 0, f"请求结束后计数应归零，实际 {after}"


def test_in_flight_count_returns_to_zero_on_exception():
    """异常路径也必须归零。

    漏掉 decrement 的后果是计数**永久偏移**：之后每次都误报"还有请求在途"，
    而这个误报会让关闭逻辑去等一个根本不存在的请求。
    """

    class _Boom:
        async def __call__(self, scope, receive, send):
            raise RuntimeError("boom")

    mw = MW.RequestContextMiddleware(_Boom(), enabled=False)

    async def send(msg):
        pass

    with pytest.raises(RuntimeError):
        asyncio.run(mw(_scope(), _receive, send))
    assert MW.in_flight_count() == 0, "异常路径没有归零，计数会永久偏移"


def test_in_flight_count_ignores_non_http_scopes():
    """lifespan/websocket 不该被计入 HTTP 在途数。"""
    seen = []

    class _App:
        async def __call__(self, scope, receive, send):
            seen.append(MW.in_flight_count())

    mw = MW.RequestContextMiddleware(_App(), enabled=False)

    async def send(msg):
        pass

    asyncio.run(mw({"type": "lifespan"}, _receive, send))
    assert seen == [0], "非 http scope 不该改动在途计数"


def test_in_flight_count_is_exposed_as_gauge():
    """必须同时暴露成指标 —— 否则只能事后看日志，无法实时观察。"""
    app = _HangApp()
    mw = MW.RequestContextMiddleware(app, enabled=True)

    async def send(msg):
        pass

    async def run():
        task = asyncio.create_task(mw(_scope(), _receive, send))
        await app.started.wait()
        during = MW.metrics.gauge("http_requests_in_flight")
        app.release.set()
        await task
        return during, MW.metrics.gauge("http_requests_in_flight")

    during, after = asyncio.run(run())
    assert during == 1.0
    assert after == 0.0


# ---------------------------------------------------------------------------
# 关闭时的在途播报
# ---------------------------------------------------------------------------
def test_shutdown_logs_in_flight_count(monkeypatch, caplog):
    """关闭时必须把在途数量写进日志（>0 用 warning，便于检索）。"""
    from app import main as M

    monkeypatch.setattr(M.middleware, "in_flight_count", lambda: 3)
    monkeypatch.delenv("DB_URL", raising=False)

    async def run():
        async with M.lifespan(None):
            pass

    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    assert any("仍有 3 个请求在途" in r.getMessage() for r in caplog.records), (
        "关闭时没有播报在途请求数，无法判断发布是否打断了回答"
    )


def test_shutdown_logs_quiet_path_when_idle(monkeypatch, caplog):
    from app import main as M

    monkeypatch.setattr(M.middleware, "in_flight_count", lambda: 0)
    monkeypatch.delenv("DB_URL", raising=False)

    async def run():
        async with M.lifespan(None):
            pass

    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    assert any("关闭时无在途请求" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 排空上界
# ---------------------------------------------------------------------------
def test_graceful_timeout_default(monkeypatch):
    from app import serve as S
    monkeypatch.delenv("GRACEFUL_SHUTDOWN_TIMEOUT", raising=False)
    assert S.resolve_graceful_timeout() == 30


@pytest.mark.parametrize("raw,expected", [("60", 60), ("1", 1), ("0", 30), ("-5", 30),
                                          ("abc", 30), ("", 30)])
def test_graceful_timeout_parsing(monkeypatch, raw, expected):
    """非法值退回默认而不抛 —— 与 resolve_workers 同口径：配置错误不该让服务起不来。"""
    from app import serve as S
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", raw)
    assert S.resolve_graceful_timeout() == expected


def test_uvicorn_gets_bounded_graceful_timeout(monkeypatch):
    """端到端：必须真的把上界传进 uvicorn.run。

    不传时 uvicorn 默认 `None` = **永远等**，一个挂住的 SSE 能把关闭无限期拖住，
    最后被容器运行时 SIGKILL（那才是真的掐断在途流）。
    """
    from app import serve as S
    captured = {}

    class _FakeUvicorn:
        @staticmethod
        def run(app, **kwargs):
            captured.update(kwargs)

    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "17")
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", _FakeUvicorn)

    assert S.main([]) == 0
    assert captured.get("timeout_graceful_shutdown") == 17, (
        "未把排空上界传给 uvicorn，关闭会无限期等待"
    )


# ---------------------------------------------------------------------------
# 容器侧：宽限期必须大于排空上界
# ---------------------------------------------------------------------------
def _parse_seconds(v) -> float:
    """把 compose 的时长写法（45s / 1m30s / 90）解析成秒。"""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    total = 0.0
    for num, unit in re.findall(r"(\d+\.?\d*)\s*([hms]?)", s):
        if not num:
            continue
        total += float(num) * {"h": 3600, "m": 60, "s": 1, "": 1}[unit]
    return total


@pytest.fixture(scope="module")
def compose_cfg():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_compose_stop_grace_period_exceeds_drain_timeout(compose_cfg, monkeypatch):
    """**核心联动断言**：容器宽限期必须 > 应用排空上界。

    只改一处等于没改：若 compose 更小，容器会在排空还没做完时 SIGKILL ——
    这正是"发布时个别用户的回答直接断掉、日志里查不到原因"的成因。
    """
    from app import serve as S
    monkeypatch.delenv("GRACEFUL_SHUTDOWN_TIMEOUT", raising=False)
    drain = S.resolve_graceful_timeout()

    for name in ("app-stable", "app-canary"):
        svc = compose_cfg["services"][name]
        assert "stop_grace_period" in svc, (
            f"{name} 未设置 stop_grace_period，将用 docker 默认的 10s —— "
            "长对话必被 SIGKILL"
        )
        grace = _parse_seconds(svc["stop_grace_period"])
        assert grace > drain, (
            f"{name} 的 stop_grace_period({grace}s) 不大于应用排空上界({drain}s)，"
            "容器会在排空完成前 SIGKILL"
        )


def _parse_cpus(v) -> float:
    """compose 的 cpus 可写成字符串（"2.0"）或数字；解析成 float，非法返回 0。"""
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0


def _find_mount(vols, target: str) -> str | None:
    """在 volumes 列表里找"挂到 target"的那条（跳过 :ro 这类模式后缀）。"""
    for v in vols:
        parts = str(v).split(":")
        if len(parts) >= 2 and parts[1] == target:
            return str(v)
    return None


def test_compose_sets_resource_limits(compose_cfg):
    """资源上限：没有它时一个内存泄漏/超大文档会把宿主机吃满。

    "整台机器都慢"比"容器被限流"难定位得多。三项各管一类宿主机级故障：
    memory（OOM killer，且它挑谁杀不确定）、cpus（跑满 CPU 拖垮同机服务）、
    pids（fork bomb 耗尽 PID 表）。
    """
    for name in ("app-stable", "app-canary"):
        res = compose_cfg["services"][name].get("deploy", {}).get("resources", {})
        limits = res.get("limits", {})
        assert limits.get("memory"), f"{name} 未设内存上限"
        assert _parse_cpus(limits.get("cpus")) > 0, (
            f"{name} 未设 CPU 上限 —— 一个卡死的解析任务会把整机 CPU 占满"
        )


def test_compose_limits_process_count(compose_cfg):
    """pids 上限：防 fork bomb / 线程泄漏。

    进程数无上限时，一个失控的子进程爆炸会先耗尽宿主机的 PID 表，表现成
    **整台机器"莫名其妙起不了新进程"** —— 那时已经定位不到罪魁容器了。
    上限也不能给得形同虚设（几千），否则失去了约束意义。
    """
    for name in ("app-stable", "app-canary"):
        limits = (
            compose_cfg["services"][name].get("deploy", {}).get("resources", {}).get("limits", {})
        )
        pids = limits.get("pids")
        assert pids, f"{name} 未设 pids 上限（fork bomb 会耗尽宿主机 PID 表）"
        assert 0 < int(pids) <= 1024, f"{name} 的 pids 上限({pids})不是有效约束"


def test_canary_gets_no_more_resources_than_stable(compose_cfg):
    """灰度副本不该比稳定版拿更多资源。

    灰度只承载 10% 流量。若它反而更宽，一次灰度事故会连带影响那 90% ——
    而灰度的全部意义就是"把影响面限制在小流量里"。
    """
    def _limits(name):
        return (
            compose_cfg["services"][name].get("deploy", {}).get("resources", {}).get("limits", {})
        )

    stable, canary = _limits("app-stable"), _limits("app-canary")
    assert _parse_cpus(canary.get("cpus")) <= _parse_cpus(stable.get("cpus"))
    assert int(canary.get("pids")) <= int(stable.get("pids"))


def test_compose_mounts_the_corpus(compose_cfg):
    """语料目录必须挂进容器 —— 否则是**静默失败**。

    镜像刻意不打 `data/`。不挂的后果不是报错，而是：服务正常、探针全绿、SSE
    也能连上，**只是检索永远召回 0 条**，回答里一条引用都没有。成因是
    `load_chunks` 对不存在的目录直接返回 `[]`（不抛异常）。
    """
    for name in ("app-stable", "app-canary"):
        vols = compose_cfg["services"][name].get("volumes") or []
        mount = _find_mount(vols, "/app/data")
        assert mount, (
            f"{name} 未把语料目录挂到 /app/data（DOCS_DIR 默认值），检索会恒为空：{vols}"
        )
        assert not mount.endswith(":ro"), (
            f"{name} 的语料挂载是只读的（{mount}），解析缓存 <语料>/.parse_cache 写不进去"
        )


def test_compose_drops_privileges(compose_cfg):
    """最小权限：容器被攻破时的影响面。

    与 Dockerfile 的非 root 用户是同一层思路 —— 提权、改内核参数、raw socket
    这些业务都不需要。
    """
    for name in ("app-stable", "app-canary"):
        svc = compose_cfg["services"][name]
        assert "ALL" in (svc.get("cap_drop") or []), f"{name} 未丢弃 capabilities"
        assert any("no-new-privileges" in s for s in (svc.get("security_opt") or [])), (
            f"{name} 未设 no-new-privileges（setuid 提权路径仍开着）"
        )


def test_compose_keeps_loopback_db_binding(compose_cfg):
    """数据库端口必须绑回环。

    `"5433:5432"` 等价于 `0.0.0.0:5433` —— 云主机上就是"对整个互联网开放一个库"。
    这条容易被"我本地连不上"驱动着改掉，所以钉住。
    """
    ports = compose_cfg["services"]["pgvector"]["ports"]
    assert any(p.startswith("127.0.0.1:") for p in ports), (
        f"pgvector 端口未绑定回环：{ports}"
    )


# ---------------------------------------------------------------------------
# 请求总时限 ⇒ 排空上界 ⇒ 容器宽限期（2026-10-21）
#
# 上面那一组只钉了「③ 容器宽限期 > ② 排空上界」。漏掉的是另一头：
# **② 小于 ①（请求总时限）** 时，一次发布动作会主动取消那些"本来还被允许继续跑"
# 的请求 —— 那一刻没有任何代码出错，用户看到回答断在半句，服务端只有一条
# "排空超时"，看起来像个别请求卡住。所以三个值必须当成一条链来钉。
# ---------------------------------------------------------------------------
def test_the_drain_default_follows_the_request_budget(monkeypatch):
    """★ 排空上界默认**跟随**请求总时限，不是常数 30。

    钉的是"跟随"这个关系，不是具体数字：数字要跟着总时限一起改，关系不能断。
    原来写死 30 是按"单次模型调用 30s"推的，可一次请求能跑十几圈（`AGENT_MAX_TURNS`），
    总时限早已是 180 —— 于是默认配置下"排空 30s"意味着跑过 30 秒的请求在发布时被取消。
    """
    from app import config as C
    from app import serve as S

    monkeypatch.delenv("GRACEFUL_SHUTDOWN_TIMEOUT", raising=False)

    monkeypatch.setattr(C, "REQUEST_TIMEOUT_SECONDS", 180)
    assert S.resolve_graceful_timeout() == 190, "排空上界没有跟着总时限走"

    monkeypatch.setattr(C, "REQUEST_TIMEOUT_SECONDS", 60)
    assert S.resolve_graceful_timeout() == 70


def test_without_a_request_budget_the_drain_falls_back_to_a_constant(monkeypatch):
    """总时限为 0（不限）时退回一个常数。

    这种情况下没有任何取值是对的（一个请求可以跑到永远），所以这里只保证
    别算出 0 或负数 —— uvicorn 拿到 0 会**立刻**掐断所有在途响应，
    比原来的 30s 更糟，而且症状同样是"发布时回答断掉"。
    """
    from app import config as C
    from app import serve as S

    monkeypatch.delenv("GRACEFUL_SHUTDOWN_TIMEOUT", raising=False)
    monkeypatch.setattr(C, "REQUEST_TIMEOUT_SECONDS", 0)
    assert S.resolve_graceful_timeout() == 30


def test_a_drain_shorter_than_the_request_budget_is_announced(monkeypatch, capsys):
    """★ 显式把排空配得比总时限短：**照用，但必须喊出来**。

    照用是既有一致口径（配置错误不该让服务起不来）。喊出来是因为这条最容易被
    当成"让发布快一点"的优化，而它的症状（发布时部分回答断在半句）归因不到配置上：
    日志里只有一条排空超时，没有"这条路本来还剩 150 秒"。
    """
    from app import config as C
    from app import serve as S

    monkeypatch.setattr(C, "REQUEST_TIMEOUT_SECONDS", 180)
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "30")

    assert S.resolve_graceful_timeout() == 30
    out = capsys.readouterr().out
    assert "GRACEFUL_SHUTDOWN_TIMEOUT=30" in out
    assert "断在半句" in out


def _sample_request_budget() -> int:
    """读 `.env.example` 里的请求总时限（运维照抄的那个值）。"""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    m = re.search(r"^REQUEST_TIMEOUT_SECONDS=(\d+)\s*$", text, re.M)
    assert m, ".env.example 里找不到 REQUEST_TIMEOUT_SECONDS（样例文件被改坏了？）"
    return int(m.group(1))


def test_the_sample_request_budget_is_covered_by_the_drain_default(monkeypatch):
    """★ 照文档配出来的总时限，必须被**默认**排空上界覆盖。

    这条把两份文件绑在一起：`REQUEST_TIMEOUT_SECONDS=180` 是生产样例值，
    若默认排空仍是 30s，那么"照文档配好"的部署在发布时照样会截断长回答 ——
    而配置本身看起来完全正确（每一项都配了）。
    """
    from app import config as C
    from app import serve as S

    budget = _sample_request_budget()
    assert budget > 0, "样例里必须给正的总时限（0 = 不限，排空上界就没有意义）"

    monkeypatch.delenv("GRACEFUL_SHUTDOWN_TIMEOUT", raising=False)
    monkeypatch.setattr(C, "REQUEST_TIMEOUT_SECONDS", budget)
    drain = S.resolve_graceful_timeout()
    assert drain >= budget, (
        f"默认排空上界({drain}s) 小于样例总时限({budget}s) —— "
        "发布时会把还在允许时间内的请求取消"
    )


def test_compose_grace_period_covers_the_sample_request_budget(compose_cfg, monkeypatch):
    """★ 全链条最后一段：①≤②<③ 必须同时成立。

    只钉 ③>② 是不够的：一旦 ② 跟随 ① 变成 190，③ 若还是 45 就立刻断裂。
    这里用样例里的 ① 推 ②，再要求 ③ 大于它 —— 改任何一个值都得三个一起过。
    """
    from app import config as C
    from app import serve as S

    budget = _sample_request_budget()
    monkeypatch.delenv("GRACEFUL_SHUTDOWN_TIMEOUT", raising=False)
    monkeypatch.setattr(C, "REQUEST_TIMEOUT_SECONDS", budget)
    drain = S.resolve_graceful_timeout()

    for name in ("app-stable", "app-canary"):
        grace = _parse_seconds(compose_cfg["services"][name]["stop_grace_period"])
        assert grace > drain, (
            f"{name} 的 stop_grace_period({grace}s) 不大于排空上界({drain}s) —— "
            "容器会在排空完成前 SIGKILL（这在 ② 跟随 ① 之后才暴露出来）"
        )
