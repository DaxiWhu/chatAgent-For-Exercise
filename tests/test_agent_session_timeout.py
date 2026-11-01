"""会话后端的**超时、降级与冷却**（2026-11-01 新增）。

为什么这条必须有
----------------
真跑长任务时发现：`DB_URL` 指向一个没人监听的端口，`get_items()` **挂到 20 秒
仍停在 `selectors.select`** —— 既没报错也没超时。原因是 psycopg 的连接失败
不会把 socket 结束掉，而引擎上没给 `connect_timeout`，于是"连不上"这件事
在调用方看来只是"很慢"，而且是**无限慢**。5 条用例就此耗掉十几分钟，全部变成
`exit_reason=error`，看起来跟"网关挂了"一模一样。

本文件守四件事，全部**不需要真库**（真库那条在 `test_agent_session_pg.py`）：

1. 引擎必须带着 `connect_timeout` 建出来；
2. 驱动不返回时，`load_items` / `clear_session` 自己要有上界；
3. 连不上之后 `session_backend_ok()` 必须变 False（否则 `/health/ready` 谎报）；
4. 冷却期内不再尝试连库（否则修完只是把"无限等"变成"每个请求都等十几秒"）。

⚠️ 第 3 条是**原有 bug**，不是本次改动引入的：`session_for` 只构造对象
（懒连接，不碰库）就把 `_backend_ok` 置了 True，真正失败的 `get_items` 那一步
以前只记日志、不改状态。
"""

from __future__ import annotations

import asyncio

import pytest

from src.agents import session as S


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例前后都复位：`_retry_after` 是模块级状态，串味会让后面全绿。"""
    S.reset_sessions()
    yield
    S.reset_sessions()


# --------------------------------------------------------------------------
# 配置读取
# --------------------------------------------------------------------------


def test_connect_timeout_defaults_to_five(monkeypatch):
    monkeypatch.delenv("AGENT_SESSION_CONNECT_TIMEOUT", raising=False)

    assert S._connect_timeout() == 5


def test_connect_timeout_rejects_junk(monkeypatch):
    """乱填要回落默认，而不是让一个非法值流进驱动（那会变成"永远不超时"）。"""
    monkeypatch.setenv("AGENT_SESSION_CONNECT_TIMEOUT", "五秒")

    assert S._connect_timeout() == 5


def test_op_timeout_derives_from_connect_timeout(monkeypatch):
    """操作超时默认是连接超时的 3 倍：连接阶段最坏花 2 倍，再留一倍执行。

    两个数各自独立配置时，一定有人配出"操作超时比连接超时还短"的组合 ——
    那种组合下驱动永远没机会按自己的超时正常失败。
    """
    monkeypatch.setenv("AGENT_SESSION_CONNECT_TIMEOUT", "4")
    monkeypatch.delenv("AGENT_SESSION_OP_TIMEOUT", raising=False)

    assert S._op_timeout() == 12.0


def test_op_timeout_can_be_overridden(monkeypatch):
    monkeypatch.setenv("AGENT_SESSION_CONNECT_TIMEOUT", "4")
    monkeypatch.setenv("AGENT_SESSION_OP_TIMEOUT", "30")

    assert S._op_timeout() == 30.0


# --------------------------------------------------------------------------
# 引擎参数
# --------------------------------------------------------------------------


def test_engine_is_created_with_connect_timeout(monkeypatch):
    """★ 少这一个参数，连接失败就没有上界。

    用假 `create_async_engine` 抓参，不真建引擎也不连库。
    """
    monkeypatch.setenv("DB_URL", "postgresql://u:p@localhost:59999/db")
    monkeypatch.setenv("AGENT_SESSION_CONNECT_TIMEOUT", "7")
    grabbed = {}

    def fake_create(dsn, **kwargs):
        grabbed["dsn"] = dsn
        grabbed["kwargs"] = kwargs
        return object()

    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_create)

    S._engine_or_raise()

    assert grabbed["kwargs"]["connect_args"]["connect_timeout"] == 7
    assert grabbed["dsn"].startswith("postgresql+psycopg://"), "驱动名不能被吃掉"


# --------------------------------------------------------------------------
# 「该不该冷却」的判定
# --------------------------------------------------------------------------


def test_connectivity_errors_are_recognized():
    """连不上 ⇒ 该冷却。判据按类名，不 import psycopg（它是可选的）。"""

    class OperationalError(Exception):
        pass

    assert S._is_connectivity_error(OperationalError("boom"))
    assert S._is_connectivity_error(TimeoutError("slow"))


def test_plain_sql_errors_are_not_connectivity():
    """SQL 写错**不该**冷却：那会把一条坏语句放大成所有人的历史静默丢失。"""
    assert not S._is_connectivity_error(ValueError("column does not exist"))


# --------------------------------------------------------------------------
# 降级：驱动不返回时自己收场
# --------------------------------------------------------------------------


class _HangingSession:
    """永远不返回的会话对象（模拟"连接没建立，socket 也不结束"）。"""

    async def get_items(self):
        await asyncio.Event().wait()

    async def clear_session(self):  # pragma: no cover  只有 clear 用例走到
        await asyncio.Event().wait()


def test_load_items_gives_up_and_falls_back_to_no_history(monkeypatch):
    """★ 驱动不返回时必须有上界，且**降级成"没有历史"**而不是抛给调用方。"""
    monkeypatch.setattr(S, "session_for", lambda sid: _HangingSession())
    monkeypatch.setattr(S, "_op_timeout", lambda: 0.05)

    items = asyncio.run(S.load_items("t-hang"))

    assert items == [], "拿不到历史是降级，不是失败"


def test_timeout_marks_backend_unhealthy(monkeypatch):
    """★ 超时之后 `session_backend_ok()` 必须是 False。

    这一条以前是坏的：`session_for` 只构造对象就把标志置 True，
    真正失败的 `get_items` 不改状态 ⇒ `/health/ready` 一直报"历史就绪"。
    """
    monkeypatch.setattr(S, "session_for", lambda sid: _HangingSession())
    monkeypatch.setattr(S, "_op_timeout", lambda: 0.05)

    asyncio.run(S.load_items("t-hang"))

    assert S.session_backend_ok() is False


def test_clear_session_timeout_reports_failure(monkeypatch):
    monkeypatch.setattr(S, "session_for", lambda sid: _HangingSession())
    monkeypatch.setattr(S, "_op_timeout", lambda: 0.05)

    assert asyncio.run(S.clear_session("t-hang")) is False


# --------------------------------------------------------------------------
# 冷却
# --------------------------------------------------------------------------


def test_cooldown_stops_repeated_connect_attempts(monkeypatch):
    """★ 连不上之后短时间内的请求必须**零等待**降级。

    否则修完只是把"无限等"换成"每个请求白等十几秒" —— 用户看到的还是"很慢"。
    """
    monkeypatch.setenv("DB_URL", "postgresql://u:p@localhost:59999/db")
    monkeypatch.setenv("AGENT_SESSION_RETRY_COOLDOWN", "60")
    built = []

    def fake_build(sid):
        built.append(sid)
        return _HangingSession()

    monkeypatch.setattr(S, "_build", fake_build)
    S._note_failure(TimeoutError("连接超时"))

    assert S.session_for("t-cool") is None, "冷却期内不该返回会话"
    assert built == [], "冷却期内连构造都不该发生（构造后面就是连库）"


def test_cooldown_expires(monkeypatch):
    """冷却到期后必须重新尝试 —— 否则后端恢复了也永远接不上。"""
    monkeypatch.setenv("DB_URL", "postgresql://u:p@localhost:59999/db")
    monkeypatch.setenv("AGENT_SESSION_RETRY_COOLDOWN", "0")

    S._note_failure(TimeoutError("连接超时"))

    assert S.session_for("t-cool-expired") is not None


def test_sql_error_does_not_start_cooldown(monkeypatch):
    """SQL 类错误只记日志，不冷却 —— 否则一条坏语句让所有人的历史静默丢 15 秒。"""
    monkeypatch.setenv("DB_URL", "postgresql://u:p@localhost:59999/db")
    monkeypatch.setenv("AGENT_SESSION_RETRY_COOLDOWN", "60")
    monkeypatch.setattr(S, "_build", lambda sid: _HangingSession())

    S._note_failure(ValueError("column does not exist"))

    assert S.session_for("t-sql") is not None, "SQL 错误不该把整个后端停掉"
