"""请求级总超时（`REQUEST_TIMEOUT_SECONDS`）：给一次请求一个真正的上界。

为什么需要这一层（这是本文件存在的理由）
------------------------------------
改造前唯一的超时是 LLM client 的 `timeout=30`，它只约束**单次 HTTP 调用**。
而一次请求里会有**多次**模型调用（模型调工具、再调模型、转交之后再调），
每一次都是独立的 30s 调用 ⇒ **一次合法请求的总时长没有任何上界**。

最需要总时限的场景恰恰是"主循环一次都不转"的那个：上游限流重试 / 排队 /
连接挂死时，`async for` 停在 await 上，循环体不执行 —— 所以"在循环里比对时间"
**永远等不到执行**。本文件的核心用例（`test_hanging_upstream_is_cut_off`）
就钉住这一点：只写"检测时间"而不把等待变成可取消的，那个用例会红。

测试策略
-------
- 单元层直接驱动 `_stream_run`（假运行 + 真 asyncio，必须真睡，否则测不到超时）；
- 有一条例外必须走 **HTTP 层**：`timeout` 事件的 data 是结构化载荷，
  而序列化发生在 sse_starlette 里 —— 只断言进程内的 dict 是典型的假绿
  （见 `test_api_v1.py` 里对 JSONServerSentEvent 的那段说明）。
"""
import asyncio
import json
import logging
import os

import pytest

os.environ["DB_URL"] = ""

pytest.importorskip("fastapi.testclient", reason="TestClient 需要 httpx")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as M  # noqa: E402
from app import metrics  # noqa: E402
from app.quota import QuotaManager  # noqa: E402
from app.ratelimit import RateLimiter  # noqa: E402
from tests.auth_util import auth_headers  # noqa: E402

client = TestClient(M.app, headers=auth_headers())


# ---------------------------------------------------------------------------
# 假图 / 假流
# ---------------------------------------------------------------------------
class _FakeTurn:
    """替身运行：按需吐片段 / 挂住 / 慢吐。

    替换的是 ``app.main.run_turn`` —— 接口层现在唯一依赖的执行入口。刻意保留
    "回调式交正文"的形状（`on_text`），因为总时限要约束的正是**每一次等待**，
    而等待发生在"上游什么时候交出下一段"上。
    """

    def __init__(self, total=0, *, delay=0.0, hang=False, hang_hold=False):
        self.total = total
        self.delay = delay
        self.hang = hang
        #: 吐完正文、**收尾**才挂住（模拟"落待审队列时库端卡死"）。
        self.hang_hold = hang_hold
        self.closed = False
        self.cancelled = False

    async def __call__(self, req, *, context, on_text=None):
        from src.agents.runner import TaskOutcome

        try:
            if self.hang:
                await asyncio.sleep(3600)
            for _ in range(self.total):
                if self.delay:
                    await asyncio.sleep(self.delay)
                if on_text is not None:
                    await on_text("片段", "call-1")
            if self.hang_hold:
                await asyncio.sleep(3600)
            return TaskOutcome(
                answer="", attempts=1, final_agent="frontline_agent",
                passed=True, exit_reason="passed",
                # 这一档会触发"落库"这一步（收尾 IO），用于钉住它也在时限内。
                review_reason="manual" if self.hang_hold else "",
            )
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.closed = True


def _collect(gen, timeout=10):
    """把 SSE 事件生成器跑完，返回事件列表（应用挂住即判失败，而不是用例卡死）。"""
    async def run():
        return [ev async for ev in gen]

    return asyncio.run(asyncio.wait_for(run(), timeout=timeout))


@pytest.fixture
def loose_gates(monkeypatch):
    """把限流/配额换成不拦路的实例，让用例只考察超时这一件事。"""
    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=10 ** 6))
    monkeypatch.setattr(M, "_QUOTA", QuotaManager(daily_limit=0, request_limit=0))
    metrics.reset()
    yield
    metrics.reset()


# ---------------------------------------------------------------------------
# 配置：默认必须"不限"（否则会静默截断长但合法的回答）
# ---------------------------------------------------------------------------
def test_default_is_unlimited_and_echoed_in_startup_config():
    """默认 0 = 不限，且必须出现在启动日志的配置回显里。

    两件事都要钉：
      - 默认值不是 0 ⇒ 会**静默截断**长回答（内容少一半、日志里什么都没有）；
      - `as_dict()` 漏了这一项 ⇒ 运维配了却无法自证（这是本项目对"安全开关"
        的一贯要求：见 `app/config.py` 的模块说明）。

    断言不再用 `if not os.getenv(...)` 包一层：那种写法在"本机 `.env` 配了这个
    变量"时会静默跳过断言（假绿）。隔离层（`tests/conftest.py`）已经保证测试
    进程里这个变量一定不存在，所以直接断言。
    """
    assert M.config.REQUEST_TIMEOUT_SECONDS == 0
    limits = M.config.as_dict()["limits"]
    assert limits["request_timeout_seconds"] == M.config.REQUEST_TIMEOUT_SECONDS


def test_production_without_timeout_warns(caplog, monkeypatch):
    """生产 + 未配 ⇒ 一条可检索的告警，且要点名"LLM client 的 timeout 不够"。"""
    monkeypatch.setattr(M.config, "IS_PRODUCTION", True)
    monkeypatch.setattr(M.config, "REQUEST_TIMEOUT_SECONDS", 0)
    with caplog.at_level(logging.WARNING):
        M._warn_about_missing_timeout()
    assert any("REQUEST_TIMEOUT_SECONDS" in r.message for r in caplog.records), \
        "生产未配总超时却没有告警 —— 这类「配置缺失」只能靠日志被发现"


def test_production_with_timeout_does_not_warn(caplog, monkeypatch):
    """反向用例：配了就不该再告警（常误报的 warning 会被运维当噪音过滤掉）。"""
    monkeypatch.setattr(M.config, "IS_PRODUCTION", True)
    monkeypatch.setattr(M.config, "REQUEST_TIMEOUT_SECONDS", 120)
    with caplog.at_level(logging.WARNING):
        M._warn_about_missing_timeout()
    assert not [r for r in caplog.records if "REQUEST_TIMEOUT_SECONDS" in r.message]


# ---------------------------------------------------------------------------
# `_RequestDeadline` 本身
# ---------------------------------------------------------------------------
def test_deadline_is_inactive_when_unlimited():
    """0 与负数都表示"不限"（负数/None 归到不限，而不是"立刻超时"）。"""
    for v in (0, -1, None):
        d = M._RequestDeadline(v)
        assert d.active is False, f"{v!r} 不该被当成启用总时限"
        assert d.remaining() == float("inf")


def test_deadline_does_not_create_coroutine_when_already_expired():
    """已过期时**不得调用 factory**。

    为什么这里的 factory 刻意写成**普通函数**（返回协程）
    ------------------------------------------------
    写成 `async def` 的版本只能证明"协程体没被执行"，证明不了"协程没被创建"：
    被 `ensure_future` 建出来又立刻取消的任务，其协程体**从未开始运行**，
    于是"记录了调用"这件事永远为假 —— 无论实现有没有提前返回。这正是本文件
    第一次写的版本，变异验证（去掉 `remaining <= 0` 前置检查）时**没变红**，
    才改成现在这个写法。

    要证明的东西：传工厂而不是传协程对象，就是为了不留下"创建了但没人 await"
    的协程（RuntimeWarning + 真实资源泄漏）。
    """
    d = M._RequestDeadline(0.001)
    created = []

    async def inner():
        return "x"

    def factory():
        created.append(1)
        return inner()

    async def run():
        await asyncio.sleep(0.02)          # 让它过期
        with pytest.raises(asyncio.TimeoutError):
            await d.run(factory)

    asyncio.run(asyncio.wait_for(run(), timeout=5))
    assert created == [], "已过期却仍然创建了协程（会留下 never-awaited 警告）"


def test_deadline_passes_through_when_within_limit():
    d = M._RequestDeadline(5)

    async def factory():
        return "ok"

    assert asyncio.run(asyncio.wait_for(d.run(factory), timeout=5)) == "ok"


# ---------------------------------------------------------------------------
# 核心：上游卡死必须被打断（"在循环里比对时间"实现下这条会红）
# ---------------------------------------------------------------------------
def test_hanging_upstream_is_cut_off(loose_gates, monkeypatch):
    """上游永不吐字 ⇒ 总时限到点后必须**收流**，而不是无限等下去。

    这条用例专治"只检测时间、不取消等待"的实现：那种写法下
    `await anext()` 会一直挂住，循环体永远不执行 ⇒ 本用例超时失败。
    """
    turn = _FakeTurn(hang=True)
    monkeypatch.setattr(M, "run_turn", turn)

    events = _collect(M._stream_run(M.TurnRequest(prompt="hi", thread_id="to-1"), "chat", timeout=0.05))
    names = [e["event"] for e in events]

    assert names == ["timeout"], f"事件序列应为单个终止性事件，实际 {names}"
    payload = json.loads(events[0]["data"])
    assert payload["seconds"] == 0.05
    assert payload["produced_chars"] == 0
    # 超时是**终止性**的：不查状态（紧接着的 aget_state 也是网络调用，继续等
    # 等于没有时限），也不补 done。
    assert turn.cancelled is True, "超时后上游没被取消（模型会继续跑到自然结束，照付 token）"
    assert turn.closed is True, "上游运行未被关闭（取消没传导下去）"
    assert metrics.value("request_timeout_total", kind="chat") == 1


def test_partial_output_is_still_delivered(loose_gates, monkeypatch):
    """慢速流跑到总时限 ⇒ 已产出的正文照常交付（与 budget_exceeded 同一取舍）。

    为什么不能把整次回答作废：用户已经看到的那半句话一旦被收回、又没有任何
    正文，客户端只能重问一遍，等于**再花一次**已经花掉的 token。
    """
    monkeypatch.setattr(M, "run_turn", _FakeTurn(total=100, delay=0.02))

    events = _collect(M._stream_run(M.TurnRequest(prompt="hi", thread_id="to-2"), "chat", timeout=0.1))
    toks = [e["data"] for e in events if e["event"] == "token"]
    tail = next(e for e in events if e["event"] == "timeout")

    assert toks, "超时前产出的正文被丢掉了"
    assert json.loads(tail["data"])["produced_chars"] == sum(len(t) for t in toks)
    assert "done" not in [e["event"] for e in events]


def test_epilogue_state_read_is_also_bounded(loose_gates, monkeypatch):
    """生成跑完了、卡在收尾的 `aget_state` 上 —— 同样按总时限收流。

    只给"消费流"加时限是不够的：收尾那几次 `aget_state` 也是网络/DB 调用，
    它挂住时同样没有上界（而这时用户已经拿到正文，只会觉得"怎么还没结束"）。
    """
    monkeypatch.setattr(M, "run_turn", _FakeTurn(total=1, hang_hold=True))

    events = _collect(M._stream_run(M.TurnRequest(prompt="hi", thread_id="to-3"), "chat", timeout=0.05))
    names = [e["event"] for e in events]

    assert names[0] == "token"
    assert names[-1] == "timeout", f"事件序列：{names}"
    assert "done" not in names


# ---------------------------------------------------------------------------
# 反向用例：默认不限时，行为必须与改造前完全一致
# ---------------------------------------------------------------------------
def test_no_timeout_means_no_timeout(loose_gates, monkeypatch):
    """timeout=0 ⇒ 一个跑得比"时限"慢得多的流也照样跑完（默认真的不限）。

    这条是"默认值不改变既有行为"的守门人：少了它，把默认实现成"悄悄给个 30s"
    永远不会被发现 —— 而那正是最难排查的故障（内容少一半、日志无异常）。
    """
    monkeypatch.setattr(M, "run_turn", _FakeTurn(total=3, delay=0.03))

    events = _collect(M._stream_run(M.TurnRequest(prompt="hi", thread_id="to-4"), "chat", timeout=0))
    names = [e["event"] for e in events]

    assert names == ["token", "token", "token", "done"], f"事件序列：{names}"
    assert metrics.value("request_timeout_total", kind="chat") == 0


def test_fast_stream_is_unaffected_by_a_large_timeout(loose_gates, monkeypatch):
    """有总时限但流跑得完 ⇒ 正常收尾，不产生 timeout 事件。"""
    monkeypatch.setattr(M, "run_turn", _FakeTurn(total=2))

    events = _collect(M._stream_run(M.TurnRequest(prompt="hi", thread_id="to-5"), "chat", timeout=60))
    names = [e["event"] for e in events]

    assert names == ["token", "token", "done"], f"事件序列：{names}"
    assert metrics.value("request_timeout_total", kind="chat") == 0


# ---------------------------------------------------------------------------
# HTTP 层：事件必须在线上是可解析的 JSON
# ---------------------------------------------------------------------------
def _parse_sse(body: str):
    """把 SSE 响应体拆成 [(event, data_str), ...]（data 多行用 \\n 拼回）。"""
    frames, event, data = [], None, []
    for line in body.split("\n"):
        line = line.rstrip("\r")
        if line == "":
            if data:
                frames.append((event or "message", "\n".join(data)))
            event, data = None, []
            continue
        field, _, value = line.partition(":")
        value = value.lstrip(" ")
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
    if data:
        frames.append((event or "message", "\n".join(data)))
    return frames


def test_timeout_payload_is_json_on_the_wire(loose_gates, monkeypatch):
    """`timeout` 事件的 data 必须能被 `json.loads` 解析（走 Python repr 会红）。

    必须走 HTTP：`_stream_run` 产出的是 dict，序列化在 sse_starlette 里完成。
    只断言进程内的 dict 是假绿 —— 那正是 2026-09-28 citations 事件线上是
    Python repr 却没被测出来的原因。
    """
    monkeypatch.setattr(M.config, "REQUEST_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(M, "run_turn", _FakeTurn(hang=True))

    r = client.post("/v1/chat", json={"thread_id": "to-wire", "message": "hi"})
    assert r.status_code == 200

    frames = dict(_parse_sse(r.text))
    assert "timeout" in frames, f"没收到 timeout 事件：{list(frames)}"
    parsed = json.loads(frames["timeout"])
    assert parsed["produced_chars"] == 0
    assert isinstance(parsed["seconds"], float)


def test_review_endpoint_is_bounded_too(loose_gates, monkeypatch):
    """/v1/review 也必须受总时限约束（收敛在 `_sse` 里，但收敛本身要被钉住）。

    分开测的理由与断连那组相同：接线很容易只在 /v1/chat 上改对，另两个端点
    照抄旧写法。指标标签应为 kind="review"。
    """
    monkeypatch.setattr(M.config, "REQUEST_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(M, "run_turn", _FakeTurn(hang=True))
    # 前提：队列里真躺着一条待审记录（否则先返回 409 —— 用例照样"绿"，
    # 但测的根本不是总时限）。
    from app.review_queue import ReviewQueue

    q = ReviewQueue(dsn="")
    q.add(thread_id="to-review", draft="草稿", feedback="", tenant="")
    monkeypatch.setattr(M, "_REVIEW_QUEUE", q)

    r = client.post("/v1/review", json={"thread_id": "to-review", "action": "accept",
                                        "feedback": ""})
    assert r.status_code == 200
    frames = dict(_parse_sse(r.text))

    assert "timeout" in frames, f"没收到 timeout 事件：{list(frames)}"
    assert metrics.value("request_timeout_total", kind="review") == 1
