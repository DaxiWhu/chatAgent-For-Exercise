"""停止任务：运行登记、停止请求、以及"停"真的会收流。

三条要守的：

1. **按 run 停，不按会话停** —— 按会话会让"任务刚跑完、停止请求才到"变成
   "下一轮一开始就被停"，而这件事没有任何线索可查；
2. **登记行必须随运行结束删掉** —— 残留行会让同一个会话的下一次运行"看起来在跑"；
3. **停下来的流要有 `stopped` 事件**（终止性），不能静默结束 ——
   客户端否则分不清"被停了"与"网断了"，而这两者该不该重试完全相反。
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

os.environ["DB_URL"] = ""          # 内存后端；本文件不测 PG 那条路

from app.run_control import RunControl  # noqa: E402


@pytest.fixture
def control():
    c = RunControl(dsn="")
    c.reset()
    return c


# --------------------------------------------------------------------------- #
# 一、登记与停止的语义
# --------------------------------------------------------------------------- #
def test_stop_returns_the_run_id_of_the_latest_running_run(control):
    control.begin("r1", "T1", "acme")
    control.begin("r2", "T1", "acme")

    assert control.request_stop("T1", "alice") == "r2"
    assert control.stop_requested("r2") is True
    assert control.stop_requested("r1") is False


def test_stop_after_finish_returns_none(control):
    """运行结束后登记行消失 ⇒ 停止请求明确"没有在跑的任务"（接口据此回 409）。

    这一条同时钉住那个竞态：**不能**把"停止"留在库里等下一轮 —— 那会让用户
    刚发出去的新任务立刻被打断。
    """
    control.begin("r1", "T1")
    control.finish("r1")

    assert control.request_stop("T1", "alice") is None


def test_stop_is_idempotent_while_the_run_is_still_alive(control):
    """同一次运行**还在停的过程中**再点一次，返回同一个 run_id（不是 409）。"""
    control.begin("r1", "T1")

    assert control.request_stop("T1", "alice") == "r1"
    assert control.request_stop("T1", "alice") == "r1"
    assert control.active_count() == 1


def test_stop_only_affects_the_requested_thread(control):
    control.begin("r1", "T1")
    control.begin("r2", "T2")

    assert control.request_stop("T1", "alice") == "r1"
    assert control.stop_requested("r2") is False


def test_finish_is_idempotent_and_safe(control):
    control.finish("never-started")
    assert control.active_count() == 0


# --------------------------------------------------------------------------- #
# 二、接口
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch):
    try:
        from fastapi.testclient import TestClient
    except ImportError:  # pragma: no cover
        pytest.skip("TestClient 需要 httpx")
    import app.main as M
    from app import metrics
    from app.ratelimit import RateLimiter
    from tests.auth_util import auth_headers

    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=100000))
    monkeypatch.setattr(M, "_RUNS", RunControl(dsn=""))
    metrics.reset()
    return TestClient(M.app, headers=auth_headers())


def _claim(thread_id: str) -> None:
    import app.main as M
    M._SESSIONS.claim(thread_id, "", "tester")


def test_stop_endpoint_409_when_nothing_is_running(client):
    _claim("T-stop-none")

    r = client.post("/v1/sessions/T-stop-none/stop")

    assert r.status_code == 409


def test_stop_endpoint_marks_the_running_run(client):
    import app.main as M

    _claim("T-stop-live")
    M._RUNS.begin("r-live", "T-stop-live")

    r = client.post("/v1/sessions/T-stop-live/stop")

    assert r.status_code == 200, r.text
    assert r.json()["run_id"] == "r-live"
    assert M._RUNS.stop_requested("r-live") is True


def test_stop_endpoint_requires_auth():
    try:
        from fastapi.testclient import TestClient
    except ImportError:  # pragma: no cover
        pytest.skip("TestClient 需要 httpx")
    import app.main as M

    anon = TestClient(M.app)
    r = anon.post("/v1/sessions/T-stop-anon/stop")

    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# 三、真的收流
# --------------------------------------------------------------------------- #
class _EndlessTurn:
    """每 50ms 吐一个字，直到被取消。用来复现"正在跑的时候被停"。"""

    def __init__(self, total: int = 200):
        self.total = total
        self.cancelled = False

    async def __call__(self, req, *, context, on_text=None):
        from src.agents.runner import TaskOutcome

        try:
            for i in range(self.total):
                await asyncio.sleep(0.05)
                if on_text is not None:
                    await on_text(f"字{i}", "c1")
            return TaskOutcome(answer="", attempts=1, final_agent="frontline_agent",
                               passed=True, exit_reason="passed")
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def test_stream_emits_stopped_and_stops_consuming(monkeypatch):
    """★ 停 ⇒ 收流 + 一个 `stopped` 事件。

    为什么这条最重要：`stopped` 缺失时，客户端只能把"流没了"当成网络故障，
    于是自动重试 —— 而用户点停止的全部意义就是"别再跑了"。
    """
    import app.main as M

    turn = _EndlessTurn()
    monkeypatch.setattr(M, "run_turn", turn)
    monkeypatch.setattr(M, "_RUNS", RunControl(dsn=""))

    async def _scenario():
        gen = M._stream_run(
            M.TurnRequest(prompt="hi", thread_id="T-stream"),
            "chat", tenant="", thread_id="T-stream", run_id="r-stream")
        events = []
        async for ev in gen:
            events.append(ev)
            if len(events) == 3:
                # 模拟另一个请求（另一个连接）点了停止
                await asyncio.to_thread(M._RUNS.request_stop, "T-stream", "tester")
        return events

    started = time.perf_counter()
    events = asyncio.run(asyncio.wait_for(_scenario(), timeout=20))
    elapsed = time.perf_counter() - started

    assert events[-1]["event"] == "stopped", events[-3:]
    # 停下来要**快**：那一刻上游还有近两百个字没吐（约 10 秒的量）
    assert elapsed < 5, f"停止没有及时生效（{elapsed:.1f}s）"
    assert turn.cancelled is True, "停了却没有取消上游运行（它会继续跑到底、继续计费）"


def test_stream_without_run_id_cannot_be_stopped(monkeypatch):
    """没有 run_id（测试/CLI/未登记）时行为与改造前完全一致：跑到底，不发 stopped。"""
    import app.main as M

    monkeypatch.setattr(M, "run_turn", _EndlessTurn(total=4))
    monkeypatch.setattr(M, "_RUNS", RunControl(dsn=""))

    async def _scenario():
        gen = M._stream_run(
            M.TurnRequest(prompt="hi", thread_id="T-nostop"),
            "chat", tenant="", thread_id="T-nostop")
        return [ev async for ev in gen]

    events = asyncio.run(asyncio.wait_for(_scenario(), timeout=20))

    assert not any(ev.get("event") == "stopped" for ev in events)
