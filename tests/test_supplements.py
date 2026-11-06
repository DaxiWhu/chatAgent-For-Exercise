"""任务执行中补充：进程内暂存、模型输入注入、写入会话历史。

三条要守的东西，各自挡一类**读代码看不出来**的错：

1. **水位必须在 run 开始时定格**（``supplements.cursor``）。现取的话，注入口每次
   模型调用都会把"调用之前的所有补充"当成新的 ⇒ 同一条要求被反复拼进去；
2. **注入排在裁剪与反思之后**。反过来会让补充在下一轮被当成"超预算的老历史"裁掉，
   而症状只是"补了、模型没照做"；
3. **写入会话历史与进程内暂存是两份，缺一份就少一档有效期**：
   只写进程内 ⇒ 重启/换副本丢；只写历史 ⇒ 当次运行不生效。

⚠️ 端点那几条**不连真 PG**（``DB_URL=""``）：会话后端用假的，
本文件测的是"接线与语义"，不是 SQLAlchemy 本身。
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

os.environ["DB_URL"] = ""          # 与 test_api_v1.py 同口径：不要连真库

pytest.importorskip("agents", reason="openai-agents 未安装 ⇒ agent 链路无从验证")

from src.agents import supplements  # noqa: E402
from src.agents.context import TaskContext  # noqa: E402
from tests._agents_testkit import ScriptedModel  # noqa: E402

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None


@pytest.fixture(autouse=True)
def _clean():
    """每条用例都从空状态开始：本模块的状态是**进程级**的，不隔离就会互相串。"""
    supplements.reset()
    yield
    supplements.reset()


# --------------------------------------------------------------------------- #
# 一、暂存本身
# --------------------------------------------------------------------------- #
def test_note_returns_increasing_seq_and_since_filters_by_cursor():
    """序号单调递增；``since`` 只给水位**之后**的，且按登记顺序。"""
    a = supplements.note("T1", "第一句")
    b = supplements.note("T1", "第二句")

    assert b > a > 0
    assert supplements.cursor("T1") == b
    assert supplements.since("T1", 0) == ["第一句", "第二句"]
    assert supplements.since("T1", a) == ["第二句"]
    assert supplements.since("T1", b) == []


def test_note_ignores_empty_thread_or_text():
    """空会话号 / 空白正文不登记 —— 否则模型会收到一条空的用户消息。"""
    assert supplements.note("", "有内容") == 0
    assert supplements.note("T1", "   ") == 0
    assert supplements.pending_count("T1") == 0


def test_pending_is_bounded_per_thread():
    """单会话有界（只留最新那一批）—— 这个字典按 thread_id 增长，无界就是内存入口。"""
    for i in range(supplements.MAX_PER_THREAD + 5):
        supplements.note("T1", f"第{i}条")

    kept = supplements.since("T1", 0)
    assert len(kept) == supplements.MAX_PER_THREAD
    assert kept[-1] == f"第{supplements.MAX_PER_THREAD + 4}条"


def test_cursor_is_per_thread_not_global():
    """水位按会话取 —— 拿别的会话的序号当自己的水位会漏掉自己的补充。"""
    supplements.note("A", "a1")
    supplements.note("B", "b1")

    assert supplements.cursor("A") == 1
    assert supplements.cursor("B") == 2
    assert supplements.since("A", supplements.cursor("A")) == []
    # 用 B 的水位去读 A ⇒ 什么都读不到（A 的最后一条序号是 1，不在 2 之后）
    assert supplements.since("A", 2) == []
    assert supplements.since("B", 1) == ["b1"]


def test_clear_removes_only_that_thread():
    supplements.note("A", "a1")
    supplements.note("B", "b1")

    supplements.clear("A")

    assert supplements.pending_count("A") == 0
    assert supplements.pending_count("B") == 1


# --------------------------------------------------------------------------- #
# 二、注入模型输入
# --------------------------------------------------------------------------- #
def _data(*items):
    return SimpleNamespace(input=list(items), instructions=None)


def test_inject_appends_after_cursor_with_prefix():
    """水位之后登记的补充被拼到**末尾**，并带统一前缀（模型据此区分"补充"与"新请求"）。"""
    from src.agents import runner

    supplements.note("T1", "别写长文，三句话")
    data = _data({"role": "user", "content": "写一篇长文"})

    runner._inject_supplements(data, TaskContext(thread_id="T1", supplement_cursor=0))

    assert len(data.input) == 2
    assert data.input[-1]["content"] == f"{supplements.PREFIX}别写长文，三句话"
    assert data.input[0]["content"] == "写一篇长文"      # 原输入不动


def test_inject_skips_what_was_already_at_the_cursor():
    """★ run 开始时水位已经覆盖的补充**不再注入**。

    那条补充在 run 开始前就写进了会话历史，会自动随历史进模型输入；再拼一次就是
    **同一句话出现两遍** —— 症状是模型莫名重复强调某个要求。
    """
    from src.agents import runner

    seq = supplements.note("T1", "已经写进历史的那句")
    data = _data({"role": "user", "content": "写一篇长文"})

    runner._inject_supplements(data, TaskContext(thread_id="T1", supplement_cursor=seq))

    assert len(data.input) == 1


def test_inject_is_idempotent_within_one_call():
    """同一次调用里重复跑不会叠加（末尾去重）—— 注入口可能被 SDK 调用多次。"""
    from src.agents import runner

    supplements.note("T1", "三句话")
    data = _data({"role": "user", "content": "写长文"})

    runner._inject_supplements(data, TaskContext(thread_id="T1", supplement_cursor=0))
    runner._inject_supplements(data, TaskContext(thread_id="T1", supplement_cursor=0))

    assert len(data.input) == 2


def test_inject_does_nothing_without_session():
    """没有会话号就没有"当次运行"可言 —— 直接原样返回，不报错。"""
    from src.agents import runner

    supplements.note("T1", "不该出现")
    data = _data({"role": "user", "content": "hi"})

    runner._inject_supplements(data, TaskContext())

    assert len(data.input) == 1


# --------------------------------------------------------------------------- #
# 三、run_agent 的接线：水位在 run 开始前定格
# --------------------------------------------------------------------------- #
def test_run_agent_freezes_cursor_before_the_run(tmp_path, monkeypatch):
    """★ 跑完一次 run 之后，上下文里留着"开始时的水位"，不是事后的。

    这是"同一句补充为什么不会在每一轮被重复注入"的全部依据。
    """
    from src.agents import runner
    from src.agents import session as agent_session
    from tests._agents_testkit import FakeSession, make_ctx, say

    fake = FakeSession()
    monkeypatch.setattr(agent_session, "session_for", lambda sid: fake)

    seq = supplements.note("T1", "run 之前就写进去的")
    ctx = make_ctx(tmp_path)
    model = ScriptedModel([say("好")])
    asyncio.run(runner.run_agent(
        runner.LITERARY_AGENT, prompt="写首诗", context=ctx,
        session_id="T1", model=model))

    assert ctx.thread_id == "T1"
    assert ctx.supplement_cursor == seq
    # 水位覆盖了它 ⇒ 当次运行不重复注入（它已随会话历史进入输入）
    assert not any("run 之前就写进去的" in str(i) for i in model.calls[0].input)


def _probe_agent():
    """一个**只为本文件存在**的极简 agent：一个无参工具，没有结构化输出。

    为什么不复用前台 / 文学那两个：它们带 ``output_type``，脚本里必须造出合法的
    结构化 JSON；带工具的专家又各有各的 schema。这里要验的是"注入口在两次模型调用
    之间发生了什么"，与哪个 agent 无关 —— 用最小的那个，失败时看得清是哪里坏了。
    """
    from agents import Agent, function_tool

    @function_tool
    def noop() -> str:
        """无参工具（只为制造第二次模型调用）。"""
        return "ok"

    return Agent(name="supplement_probe", instructions="测试用", tools=[noop])


class _NotingModel(ScriptedModel):
    """第一次模型调用时"用户补了一句"，用来模拟运行中写入。"""

    def __init__(self, script, on_first=None):
        super().__init__(script)
        self._on_first = on_first

    async def get_response(self, *args, **kwargs):
        if self._on_first is not None and not self.calls:
            self._on_first()
        return await super().get_response(*args, **kwargs)


def test_supplement_written_during_run_reaches_the_next_call(tmp_path, monkeypatch):
    """★ 运行中补的话，**下一次模型调用**就出现在输入末尾。

    这是整个功能存在的理由：这一次运行的历史快照已经读过了，不注入就只能等下一个
    节点；而用户期望的是"它下一步就照做"。
    """
    from src.agents import runner
    from src.agents import session as agent_session
    from tests._agents_testkit import FakeSession, call, make_ctx, say

    fake = FakeSession()
    monkeypatch.setattr(agent_session, "session_for", lambda sid: fake)

    model = _NotingModel(
        [call("noop", {}, "c1"), say("好，三句话。")],
        on_first=lambda: supplements.note("T1", "别写长文，三句话"),
    )
    ctx = make_ctx(tmp_path)
    out = asyncio.run(runner.run_agent(
        _probe_agent(), prompt="写一段说明", context=ctx,
        session_id="T1", model=model))

    assert out.answer
    assert len(model.calls) >= 2, "需要至少两次模型调用才能看出注入时机"
    first = " ".join(str(i) for i in model.calls[0].input)
    second = " ".join(str(i) for i in model.calls[1].input)
    assert "别写长文" not in first          # 第一次调用发生在补充之前
    assert "别写长文" in second             # 下一次调用必须带上
    assert supplements.PREFIX in second


# --------------------------------------------------------------------------- #
# 四、接口：写入会话历史 + 登记暂存
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch):
    """装配一个**不连库**的 TestClient：放开限流、给一个假会话后端。"""
    if TestClient is None:  # pragma: no cover
        pytest.skip("TestClient 需要 httpx")
    import app.main as M
    from app import metrics
    from app.ratelimit import RateLimiter
    from tests.auth_util import auth_headers

    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=100000))
    metrics.reset()
    return TestClient(M.app, headers=auth_headers())


class _FakeSession:
    def __init__(self):
        self.items = []

    async def add_items(self, items):
        self.items.extend(items)


def _patch_session(monkeypatch, session, thread_id: str):
    """换掉会话后端，并**先认领**这条会话。

    为什么要认领：补充是写操作，端点走的是 ``_assert_session_readable``
    （fail-closed：查不到属主 404）。不认领的话用例会停在 404，看起来像"端点坏了"。
    """
    import app.main as M
    from src.agents import session as agent_session

    M._SESSIONS.claim(thread_id, "", "tester")
    monkeypatch.setattr(agent_session, "session_for", lambda sid: session)


def test_endpoint_writes_history_and_registers_pending(client, monkeypatch):
    """成功路径：**两份**都写 —— 会话历史那份是持久的，进程内那份是当次运行加速。"""
    fake = _FakeSession()
    _patch_session(monkeypatch, fake, "T-sup-api")

    r = client.post("/v1/sessions/T-sup-api/messages", json={"message": "改成三百字"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] is True and body["seq"] > 0
    assert len(fake.items) == 1
    assert fake.items[0]["role"] == "user"
    assert fake.items[0]["content"] == f"{supplements.PREFIX}改成三百字"
    assert supplements.since("T-sup-api", 0) == ["改成三百字"]


def test_endpoint_returns_503_without_session_backend(client, monkeypatch):
    """没有会话后端时**明确失败**，而不是"补上了"的假象。

    只登记进程内暂存是不够的：它随时会因重启/换副本消失，而调用方会以为这条补充
    已经落地。会话历史是这个功能唯一持久的那一份。
    """
    _patch_session(monkeypatch, None, "T-sup-nodb")

    r = client.post("/v1/sessions/T-sup-nodb/messages", json={"message": "补一句"})

    assert r.status_code == 503
    assert supplements.pending_count("T-sup-nodb") == 0


@pytest.mark.parametrize("payload", [{"message": ""}, {"message": "   "}])
def test_endpoint_rejects_empty_message(client, payload, monkeypatch):
    """空正文 400（与 ``/v1/chat`` 的空 message 同一口径）。"""
    _patch_session(monkeypatch, _FakeSession(), "T-sup-empty")

    r = client.post("/v1/sessions/T-sup-empty/messages", json=payload)

    assert r.status_code == 400


def test_endpoint_requires_auth():
    """不带凭证 401 —— 补充是写操作，不能让匿名请求往别人的会话里塞话。"""
    if TestClient is None:  # pragma: no cover
        pytest.skip("TestClient 需要 httpx")
    import app.main as M

    anon = TestClient(M.app)
    r = anon.post("/v1/sessions/T-sup-anon/messages", json={"message": "hi"})

    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# 五、幂等键：重试不该把同一句话写两遍
# --------------------------------------------------------------------------- #
@pytest.fixture
def idem(monkeypatch):
    """每个用例一张干净的键表。

    不能共用模块级单例：用例之间只要出现"同键 + 同载荷"，后一个就会拿到**前一个
    留下的重放**，表现为"怎么没写进历史"。（与 `tests/test_idempotency.py` 同口径。）
    """
    import app.main as M
    from app.idempotency import IdempotencyStore

    store = IdempotencyStore(ttl=600, max_entries=1000, dsn="")
    monkeypatch.setattr(M, "_IDEMPOTENCY", store)
    return store


def test_the_same_key_writes_once_and_replays_the_same_body(client, idem, monkeypatch):
    """**这条是幂等在这个端点上的全部价值。**
    只验状态码等于什么都没验：重新写一遍同样返回 200，而历史里多了一条，
    模型会以为用户说了两遍、于是把同一句要求当两次强调去执行。
    """
    fake = _FakeSession()
    _patch_session(monkeypatch, fake, "T-sup-idem")
    h = {"Idempotency-Key": "k-sup-1"}
    payload = {"message": "再短一点"}

    r1 = client.post("/v1/sessions/T-sup-idem/messages", json=payload, headers=h)
    assert r1.status_code == 200, r1.text
    r2 = client.post("/v1/sessions/T-sup-idem/messages", json=payload, headers=h)

    assert r2.status_code == 200, r2.text
    assert r1.headers.get("idempotency-replayed") is None, "首次请求不该被标成重放"
    assert r2.headers.get("idempotency-replayed") == "true"

    assert r2.json() == r1.json(), "重放正文必须与首次逐字一致（含同一个 seq）"
    assert len(fake.items) == 1, "重试又写了一条历史"
    assert supplements.pending_count("T-sup-idem") == 1


def test_same_key_with_a_different_message_is_rejected(client, idem, monkeypatch):
    """同一个键换了正文 ⇒ 422（草案 2.2 的 MUST NOT）。

    静默当成重复拒掉是更糟的结果：用户确实想说第二句，而服务端一个字都不回。
    """
    _patch_session(monkeypatch, _FakeSession(), "T-sup-mm")
    h = {"Idempotency-Key": "k-sup-mm"}

    first = client.post("/v1/sessions/T-sup-mm/messages",
                        json={"message": "第一句"}, headers=h)
    assert first.status_code == 200

    second = client.post("/v1/sessions/T-sup-mm/messages",
                         json={"message": "第二句"}, headers=h)
    assert second.status_code == 422
    assert "不同" in second.json()["detail"]


def test_same_key_across_threads_is_rejected(client, idem, monkeypatch):
    """**同一个键、两个会话、同样一句话**必须 422，不能重放。

    这条防的是一个**完全静默**的错：`thread_id` 在路径里而不在请求体里，
    只要指纹漏掉它，第二个会话就会拿到第一个会话那次写入的重放 ——
    客户端以为写进了 B，实际只有 A 有。没有任何报错，只能事后翻历史发现。

    （两个会话都要先认领，否则会先撞上 fail-closed 的 404，测不到这里。）
    """
    _patch_session(monkeypatch, _FakeSession(), "T-sup-x1")
    _patch_session(monkeypatch, _FakeSession(), "T-sup-x2")
    h = {"Idempotency-Key": "k-sup-cross"}
    payload = {"message": "同一句"}

    r1 = client.post("/v1/sessions/T-sup-x1/messages", json=payload, headers=h)
    assert r1.status_code == 200, r1.text

    r2 = client.post("/v1/sessions/T-sup-x2/messages", json=payload, headers=h)
    assert r2.status_code == 422, "换了一个会话却被当成同一次请求（指纹漏了 thread_id）"


class _FlakySession:
    """第一次写入抛错，之后成功 —— 模拟"PG 抖动一次"。"""

    def __init__(self):
        self.items = []
        self.calls = 0

    async def add_items(self, items):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("pg 抖了一下")
        self.items.extend(items)


def test_a_failed_write_does_not_burn_the_key(client, idem, monkeypatch):
    """写库失败 ⇒ 503，且键**被撤销** ⇒ 同一个键重试能真的再写一次。

    不撤销的后果：一次偶发失败让这个键在 TTL 内彻底不可用，客户端拿着完全正确的
    重试逻辑，却一直收到 409「正在处理中」，而实际上没有任何东西在处理它。
    """
    flaky = _FlakySession()
    _patch_session(monkeypatch, flaky, "T-sup-flaky")
    h = {"Idempotency-Key": "k-sup-flaky"}
    payload = {"message": "别丢掉"}

    bad = client.post("/v1/sessions/T-sup-flaky/messages", json=payload, headers=h)
    assert bad.status_code == 503

    good = client.post("/v1/sessions/T-sup-flaky/messages", json=payload, headers=h)
    assert good.status_code == 200, good.text
    assert good.headers.get("idempotency-replayed") is None, "这是重试成功，不是重放"
    assert len(flaky.items) == 1


def test_without_a_key_nothing_is_deduped(client, idem, monkeypatch):
    """不带键 = 不去重，行为与这个能力上线之前完全一致。

    草案 2.5.1：带不带键是客户端的自由。默认不要求，所以不带就必须照旧工作 ——
    否则这次改动会把所有既有客户端打挂。
    """
    fake = _FakeSession()
    _patch_session(monkeypatch, fake, "T-sup-nokey")
    payload = {"message": "同一句"}

    for _ in range(2):
        r = client.post("/v1/sessions/T-sup-nokey/messages", json=payload)
        assert r.status_code == 200

    assert len(fake.items) == 2, "没带键却被去重了（= 用户的第二句被静默吞掉）"


# --------------------------------------------------------------------------- #
# 四、会话后端必须真的能被替换（否则"接线"测试全是假的）
# --------------------------------------------------------------------------- #
def test_session_for_is_replaceable_by_module_attribute(monkeypatch):
    """打 `session` 模块上的 `session_for`，`runner` 必须真的用上替身。

    这条守的是什么
    -------------
    原实现是 `from .session import session_for` —— 导入期绑定，本模块里拿着一份
    固化引用。于是 `monkeypatch.setattr(session_module, "session_for", 替身)`
    **完全不起作用**：测试以为自己换成了内存版会话，实际仍在连真 PostgreSQL。

    它造成的真实故障（2026-11-06 全量跑红）
    ------------------------------------
    本文件有两条用例只 patch 了 `session` 模块。单跑全绿、全量跑红 —— 因为别的
    测试把 `DB_URL` 留在了环境里，真后端去连库，在 Windows 的 `asyncio.run()`
    （ProactorEventLoop）下抛
    `psycopg.InterfaceError: cannot use the 'ProactorEventLoop'`
    ⇒ `run_agent` 整轮 `exit_reason='error'`、`model.calls` 为空
    ⇒ 断言 `model.calls[0]` 报 IndexError。

    报错信息指向"补充提醒没有生效"，与真正原因（测试压根没换掉后端）无关 ——
    这类"守卫自己失效"比被测代码出错更难发现，因为它平时是绿的。
    """
    from src.agents import runner
    from src.agents import session as agent_session

    sentinel = object()
    monkeypatch.setattr(agent_session, "session_for", lambda sid: sentinel)

    assert runner.session_for("T1") is sentinel, (
        "patch `session.session_for` 之后 `runner.session_for` 没有跟着变 —— "
        "说明 runner 里还固化着一份导入期引用，所有'换掉会话后端'的测试都是假守卫"
    )


def test_session_for_is_also_replaceable_on_the_runner_module(monkeypatch):
    """另一种 patch 方式（打在本模块上）也必须生效 —— 两种都要能用。

    `tests/test_agents_pipeline.py` 的 fixture 习惯直接打 `runner.session_for`，
    那是历史写法。两种写法都支持，才不会因为"用哪种 patch"而出现
    "一半用例真换、一半没换"的混乱。
    """
    from src.agents import runner

    sentinel = object()
    monkeypatch.setattr(runner, "session_for", lambda sid: sentinel)

    assert runner.session_for("T1") is sentinel
