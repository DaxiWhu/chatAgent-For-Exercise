"""运行时监控指标的语义守卫（本轮补齐的 8 组埋点 + 9 条告警）。

为什么这些断言值得写
------------------
指标的错误是**静默**的：埋点漏了、标签写错、口径混了，服务照常跑、日志照常有，
只是"要看的那个数永远不存在"，或者"看起来一直正常"。所以每条断言都钉**行为**
（指标被不被记录、值是多少、标签是什么），而不是"源码里有没有那行字"——
后者在重构后照样绿，挡不住真正的退化。

全部离线：不连 PG / Redis，不调上游模型。
"""
import asyncio
import os
import time
from pathlib import Path

import pytest
import yaml

os.environ["DB_URL"] = ""

import app.main as M  # noqa: E402
from app import config, metrics  # noqa: E402
from src.rag.resilience import CLOSED, HALF_OPEN, OPEN, CircuitBreaker, CircuitOpenError  # noqa: E402


# ---------------------------------------------------------------------------
# 驱动 `_stream_run` 的最小假运行（不碰模型、不碰数据库）
# ---------------------------------------------------------------------------
class _FakeTurn:
    """替身运行：`chunks` 逐段交给 `on_text`；`answer` 是整段返回的正文。

    `chunks=()` 且 `answer` 非空 = **整段返回**（模型一个 token 都没流式发出），
    这正是 `mode="buffered"` 那一档的判据来源。
    """

    def __init__(self, chunks=(), answer=""):
        self.chunks = list(chunks)
        self.answer = answer

    async def __call__(self, req, *, context, on_text=None, trace_metadata=None):
        from src.agents.runner import TaskOutcome

        if on_text is not None:
            for chunk in self.chunks:
                await on_text(chunk, "call-1")
        return TaskOutcome(answer=self.answer, attempts=1,
                           final_agent="frontline_agent", passed=True,
                           exit_reason="passed")


def _run(monkeypatch, turn, *, budget=None, thread_id="m-1"):
    import app.main as M

    monkeypatch.setattr(M, "run_turn", turn)
    gen = M._stream_run(M.TurnRequest(prompt="hi", thread_id=thread_id), "chat",
                        budget=budget)

    async def _go():
        return [ev async for ev in gen]

    return asyncio.run(_go())


def _collect(agen):
    async def run():
        return [e async for e in agen]
    return asyncio.run(run())


def _count_total(name: str) -> float:
    """从 /metrics 文本里跨**所有标签组合**求计数总和。

    为什么要绕这一圈（否则会写出空转的守卫）
    --------------------------------------
    `metrics.value(name)` / `metrics.histogram_count(name)` **不带标签**时读的是
    "无标签序列"，而本项目的指标几乎都带标签写入 ⇒ 那个读法恒为 0，
    `assert ... == 0` 于是永远成立 —— 埋点整个漏掉它也照样绿。
    "缺了它也不会红"的断言不是守卫，是装饰。
    """
    total = 0.0
    for line in metrics.render().splitlines():
        parts = line.rsplit(" ", 1)
        if len(parts) != 2:
            continue
        head = parts[0].split("{")[0]
        if head in (name, f"{name}_count"):
            total += float(parts[1])
    return total


# ---------------------------------------------------------------------------
# 1. 首字节延迟（TTFT）
# ---------------------------------------------------------------------------
def test_first_token_latency_recorded_once_per_stream(monkeypatch):
    """三个 token 只记**一次**首字节。

    记成三次会把分布变成"平均每个 token 的间隔"，而那个量已经由
    `llm_tokens_per_request` + 端到端延迟间接反映 —— 重复记等于把
    "用户等了多久才看见第一个字"这个唯一含义抹掉。
    """
    metrics.reset()
    _run(monkeypatch, _FakeTurn(["甲", "乙", "丙"]))

    assert metrics.histogram_count(
        "llm_first_token_seconds", kind="chat", mode="stream") == 1
    # 跨标签总数也是 1：证明没有一个 token 溜进别的 kind/mode 档（那会让
    # "只记一次"变成"记在别处"，而上面那条断言看不出这种事）。
    assert _count_total("llm_first_token_seconds") == 1


def test_buffered_chain_is_recorded_under_a_separate_mode(monkeypatch):
    """agent 链路整段返回：它的"首字节"其实是**整段生成耗时**。

    与流式混进同一个分位数，P95 会变成一个没有含义的中间值 —— 所以两者必须
    分档。这条断言钉住"分档"这件事，而不只是"记了"。
    """
    metrics.reset()
    _run(monkeypatch, _FakeTurn(answer="整段产出"))

    assert metrics.histogram_count(
        "llm_first_token_seconds", kind="chat", mode="buffered") == 1
    assert metrics.histogram_count(
        "llm_first_token_seconds", kind="chat", mode="stream") == 0


def test_no_output_means_no_first_token_sample(monkeypatch):
    """一个字都没产出时不该记首字节。

    典型场景：出口护栏把整条产出拦下（`output` 留空）。用户没看见任何东西，
    把它算成一次"很快的首字节"会系统性压低这个指标 —— 最需要它的场景被抹平。
    """
    metrics.reset()
    _run(monkeypatch, _FakeTurn())

    # 用跨标签求和的读法：不带标签的 `histogram_count(name)` 恒为 0，
    # 拿它断言等于什么都没验（埋点整个漏掉也是 0）。
    assert _count_total("llm_first_token_seconds") == 0


def test_budget_exhaustion_does_not_count_as_first_token(monkeypatch):
    """预算被顶掉 ⇒ 用户一个字都没看到 ⇒ 不记首字节。

    这是本指标最容易被写反的地方：把 `observe` 放在预算判定**之前**，
    代价不是"多记一次"，而是**最需要它的那类场景被系统性抹平** ——
    高负载下大量请求被预算砍掉，统计上却呈现出"首字节最快"，因为被砍掉
    的请求几乎立刻就结束了。
    """
    class _Rejecting:
        """第一次 add 就拒绝：模拟预算已耗尽。"""
        used, limit = 10, 10

        def add(self, text):
            return False

    metrics.reset()
    _run(monkeypatch, _FakeTurn(["甲", "乙"]), budget=_Rejecting())

    assert metrics.value("llm_budget_exceeded_total") == 1
    assert _count_total("llm_first_token_seconds") == 0


# ---------------------------------------------------------------------------
# 3. 模型调用耗时与失败（agent 链路）
# ---------------------------------------------------------------------------
def test_agent_model_wrapper_survives_sdk_isinstance_checks():
    """包装后必须仍被 SDK 的 `isinstance(model, Model)` 认出。

    官方在 `agent.py` 与 `turn_preparation.py` 里做了这类判定 —— 用代理对象
    包装会过不了这层，被当成"没配模型"而**静默走 fallback 分支**（表现成
    "指标时有时无"且没有报错）。这条断言就是钉住"不能用代理"。
    """
    from agents.models.interface import Model
    from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

    from src.agents import model as AM

    m = AM.build_model(model_name="test-model", api_key="sk-test")
    assert isinstance(m, Model)
    assert isinstance(m, OpenAIChatCompletionsModel)


def test_agent_model_wrapper_times_get_response(monkeypatch):
    from src.agents import model as AM

    m = AM.build_model(model_name="test-model", api_key="sk-test")

    async def fake_get(self, *a, **kw):
        return "resp"

    monkeypatch.setattr(AM.OpenAIChatCompletionsModel, "get_response", fake_get)
    metrics.reset()
    assert asyncio.run(m.get_response()) == "resp"
    assert metrics.histogram_count(
        "llm_call_duration_seconds", stage="step_get", via="agent") == 1

    async def fake_fail(self, *a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(AM.OpenAIChatCompletionsModel, "get_response", fake_fail)
    with pytest.raises(RuntimeError):
        asyncio.run(m.get_response())
    assert metrics.value(
        "llm_call_errors_total", stage="step_get", via="agent") == 1


def test_agent_model_wrapper_stream_stays_an_async_iterator(monkeypatch):
    """`stream_response` 必须保持**异步生成器**语义。

    改语义（比如把 `async for` 换成 `return`）会让 `Runner` 里的 `async for`
    拿到一个 coroutine，报出与本意无关的错 —— 而那种错在测试里很少被覆盖到。
    """
    from src.agents import model as AM

    m = AM.build_model(model_name="test-model", api_key="sk-test")

    async def fake_stream(self, *a, **kw):
        yield "e1"
        yield "e2"

    monkeypatch.setattr(AM.OpenAIChatCompletionsModel, "stream_response", fake_stream)
    metrics.reset()

    async def drain():
        return [e async for e in m.stream_response()]

    assert asyncio.run(drain()) == ["e1", "e2"]
    assert metrics.histogram_count(
        "llm_call_duration_seconds", stage="step_stream", via="agent") == 1


# ---------------------------------------------------------------------------
# 5. agent 的工具调用次数
# ---------------------------------------------------------------------------
def test_agent_run_metrics_report_tool_calls(monkeypatch):
    """`tool_calls` 必须被播报出去，且必须带 `agent` 标签。

    它此前在 `AgentStats` 里累计了三层（字段、hooks 计数、`as_dict`），
    却唯独没有交出去 —— 于是"agent 是在绕圈调工具还是模型本身慢"这个问题
    在指标上无法回答，而排查 `max_turns` 时第一个要看的正是它。

    ⚠️ `agent` 标签是**拆分之后才有的必要性**：一次请求 = N 次运行（转交那一轮
    是前台 + 文学 2 次）。标签一旦漏掉，按 `exit_reason` 聚合的看板会把"运行数"
    当"请求数"，阈值全失真 —— 所以这里不只断言"播报了"，还断言标签**真的传到位**。

    ⚠️ **但这条只覆盖了一半**：它是**直接调** `_report_run_metrics`，所以只证明
    "函数会把调用方给的 `agent=` 原样播报"。**证明不了** `run_agent` 真的传了
    `agent.name` —— 变异验证实测过：把调用处改成 `agent=""`，这条照样绿。
    另一半由 `tests/test_agents_pipeline.py::test_each_run_reports_its_own_agent_label`
    钉住（跑一次真实运行，看指标出口收到了什么）。**两条都要在**。
    """
    from src.agents import runner as R
    from src.agents.context import AgentStats

    seen = []

    def _hist(name, value, **labels):
        seen.append(("hist", name, value, labels))

    def _count(name, amount=1, **labels):
        seen.append(("count", name, amount, labels))

    monkeypatch.setattr(R.observability, "histogram", _hist)
    monkeypatch.setattr(R.observability, "counter", _count)

    R._report_run_metrics(
        AgentStats(model_calls=3, tool_calls=7, duration_seconds=1.5,
                   review_rounds=1),
        "passed",
        agent="frontline_agent",
    )
    names = [s[1] for s in seen]
    for name in ("agent_runs_total", "agent_run_duration_seconds",
                 "agent_model_calls", "agent_tool_calls", "agent_review_rounds"):
        assert name in names, f"{name} 没有被播报"
    assert any(s[1] == "agent_tool_calls" and s[2] == 7 for s in seen)
    assert any(s[1] == "agent_model_calls" and s[2] == 3 for s in seen)
    # 标签必须真的跟着走（漏传标签不会报错，只会让看板口径静默错位）
    assert all(s[3].get("agent") == "frontline_agent" for s in seen), seen


def test_agent_run_metrics_skip_review_rounds_when_zero(monkeypatch):
    """没走评审（0 轮）时**不记** —— 0 不是"评审很快"，是"没评审"。

    ⚠️ 顺带钉住 `tool_calls=0` **仍要记**：它与 `review_rounds` 恰好相反 ——
    "0 次工具调用"是一个合法且有意义的取值（说明这轮没用到工具），
    而"0 轮评审"表达的是"没发生这件事"。两类 0 不能混为一谈，
    所以这条用例在变异验证里也会被"删掉 tool_calls 上报"变红（预期内）。
    """
    from src.agents import runner as R
    from src.agents.context import AgentStats

    seen = []
    monkeypatch.setattr(R.observability, "histogram",
                        lambda name, value, **kw: seen.append(name))
    monkeypatch.setattr(R.observability, "counter",
                        lambda name, amount=1, **kw: None)

    R._report_run_metrics(AgentStats(model_calls=1, tool_calls=0), "passed")
    assert "agent_review_rounds" not in seen
    assert "agent_tool_calls" in seen


# ---------------------------------------------------------------------------
# 6. 熔断器状态
# ---------------------------------------------------------------------------
def test_circuit_breaker_publishes_open_state_and_counters():
    """熔断打开时**没有任何报错**（调用方走降级分支），只有这个 gauge 能反映它。

    ⚠️ 断言用的是 `render()`（/metrics 的实际文本）而不只是 `gauge()` 的读数。
    原因：读一个不存在的 gauge 也返回 0.0 —— 只断言"读到 0"的话，**埋点整个没
    落地**（正是加 `name=` 标签时的真实事故）这条测试照样绿。
    断言线上真的出现了这一行，才把"上报链路通了"钉住。标签用 `breaker` 同理：
    它是保留名的替代（见 `src/rag/resilience.py::_publish_state` 的说明）。
    """
    b = CircuitBreaker("rerank", fail_threshold=2, reset_seconds=30, enabled=True)
    metrics.reset()

    assert b.allow() is True
    assert metrics.gauge("circuit_open", breaker="rerank") == 0.0

    b.record_failure()
    # 未达阈值：仍是闭合。指标不该提前报警。
    assert metrics.gauge("circuit_open", breaker="rerank") == 0.0

    b.record_failure()
    assert b.state == OPEN
    assert metrics.gauge("circuit_open", breaker="rerank") == 1.0
    assert metrics.value("circuit_opened_total", breaker="rerank") == 1
    # 线上确实有这条序列（值 + 标签都按预期渲染）
    assert 'circuit_open{breaker="rerank"} 1.0' in metrics.render()

    assert b.allow() is False
    assert metrics.value("circuit_rejected_total", breaker="rerank") == 1
    assert 'circuit_rejected_total{breaker="rerank"} 1.0' in metrics.render()


def test_circuit_breaker_half_open_still_counts_as_unclosed():
    """半开也算"未闭合"。

    只把 open 报成 1 的话，"上游始终没真正恢复、一直卡在 half_open 反复试探"
    这段就完全不告警 —— 而它对用户的影响与 open 完全相同。
    """
    b = CircuitBreaker("embedding", fail_threshold=1, reset_seconds=30, enabled=True)
    metrics.reset()
    b.record_failure()
    assert b.state == OPEN

    b.opened_at = time.time() - 31          # 模拟已过重置窗口
    assert b.allow() is True
    assert b.state == HALF_OPEN
    assert metrics.gauge("circuit_open", breaker="embedding") == 1.0

    b.record_success()
    assert b.state == CLOSED
    assert metrics.gauge("circuit_open", breaker="embedding") == 0.0
    # 恢复后是**覆盖写 0**，而不是把序列删掉 —— 否则"已恢复"会退化成"没有这个指标"
    assert 'circuit_open{breaker="embedding"} 0.0' in metrics.render()


def test_disabled_circuit_breaker_publishes_nothing():
    """关掉熔断时不该留下任何状态序列（"没这个能力"与"能力失灵"要能分开）。"""
    b = CircuitBreaker("rerank", fail_threshold=1, reset_seconds=30, enabled=False)
    metrics.reset()
    b.record_failure()
    b.record_success()
    assert metrics.gauge("circuit_open", breaker="rerank") == 0.0
    assert metrics.value("circuit_opened_total", breaker="rerank") == 0
    # 关键断言：一条序列都不该出现（而不是"值为 0"）
    text = metrics.render()
    assert "circuit_open" not in text
    assert "circuit_opened_total" not in text


# ---------------------------------------------------------------------------
# 7. 检索分段耗时
# ---------------------------------------------------------------------------
def test_retrieval_stage_timer_records_even_on_failure():
    """失败也记耗时：超时型故障的耗时正压着上限，是"卡在哪一段"最直接的证据。"""
    from src.rag import retrieval as R

    metrics.reset()
    assert R._timed("embed", lambda: 7) == 7
    assert metrics.histogram_count("rag_stage_duration_seconds", stage="embed") == 1

    with pytest.raises(RuntimeError):
        R._timed("vector", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert metrics.histogram_count("rag_stage_duration_seconds", stage="vector") == 1


def test_bm25_leaf_records_sparse_stage():
    """稀疏路的计时挂在**叶子实现**上（`BM25Retriever.search`），不是挂在
    `HybridRetriever` 上。

    为什么这条要单独测：BM25 是纯本地计算（微秒级），敢真跑；而它一旦变慢
    就说明实际走的是 Qdrant 稀疏后端（网络）—— 这个判断依赖"稀疏路确实有计时"。
    """
    from src.rag import retrieval as R

    metrics.reset()
    r = R.BM25Retriever([{"text": "六堡茶 1897 年"}, {"text": "广州 高铁"}])
    r.search("六堡茶", top_k=2)
    assert metrics.histogram_count("rag_stage_duration_seconds", stage="sparse") == 1


def test_hybrid_surfaces_leaf_timing_without_double_counting():
    """混合检索要能把「叶子那一层记的分段耗时」透出来，且**只记一次**。

    为什么"只记一次"是硬要求，而不只是洁癖
    ------------------------------------
    如果把计时段包在 `HybridRetriever.search` 里（叶子已经包过一层），同一次
    检索会产出**两条**样本。`rate(..._count)` 从此不再等于"每秒多少次稀疏检索"，
    所有拿它当分母的比率口径静默偏掉一半 —— 而"记录翻倍"这件事**没有任何指标
    能反映它**（看起来只是量变多了）。所以计时段只放在叶子，这里断言它不多不少。

    注入假 dense 而不是真跑：真跑要调 embedding API，与"计时有没有接上"无关。
    """
    from src.rag import retrieval as R

    class _Dense:
        def search(self, query, top_k):
            return [({"text": "b"}, 1.0)]

    metrics.reset()
    h = R.HybridRetriever([{"text": "六堡茶 1897 年"}], dense=_Dense(),
                          reranker=None)
    h.search("六堡茶", top_k=1)
    assert metrics.histogram_count("rag_stage_duration_seconds", stage="sparse") == 1


def test_rerank_client_records_rerank_stage(monkeypatch):
    """精排段的计时挂在 `RerankClient.rerank`（真实出站调用那一层）。

    出站网络用 `httpx.post` 打桩：这里要验的是"计时是否包住了真实调用"，
    而不是网络本身。
    """
    import httpx

    from src.rag import retrieval as R

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"index": 0, "relevance_score": 0.9}]}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    client = R.RerankClient("http://stub", "k", "m")

    metrics.reset()
    out = client.rerank("q", [{"text": "a"}], 1)
    assert out and out[0][1] == 0.9
    assert metrics.histogram_count("rag_stage_duration_seconds", stage="rerank") == 1


def test_rerank_rejected_by_breaker_records_counter_and_zero_duration_stage(
        monkeypatch):
    """被熔断挡下时：既记 `circuit_rejected_total`，也记一段≈0 的 rerank 耗时。

    两者回答**不同**问题，缺一不可：
      circuit_rejected_total —— 省下了多少次白等；
      rag_stage_duration_seconds{stage="rerank"} —— 省下了多少等待时间。
    只留前者的话，"精排那一段"的耗时分布会只剩成功样本，看起来一切正常。
    """
    import httpx

    from src.rag import retrieval as R

    def _boom(*a, **k):
        raise RuntimeError("上游挂了")

    monkeypatch.setattr(httpx, "post", _boom)
    client = R.RerankClient("http://stub", "k", "m")
    # 不依赖环境变量：显式把阈值与开关钉死，否则 `CIRCUIT_ENABLED=0` 的部署
    # （或某个用例改了 config）会让这条测试变成"验证不了任何东西"的空转。
    client.breaker.fail_threshold = 1
    client.breaker.enabled = True

    metrics.reset()
    with pytest.raises(RuntimeError):
        client.rerank("q", [{"text": "a"}], 1)      # 第 1 次：真实失败 ⇒ 熔断打开
    assert client.breaker.state == OPEN
    assert 'circuit_open{breaker="rerank"} 1.0' in metrics.render()

    with pytest.raises(CircuitOpenError):
        client.rerank("q", [{"text": "a"}], 1)      # 第 2 次：被挡下，没发请求

    assert metrics.value("circuit_rejected_total", breaker="rerank") == 1
    assert metrics.histogram_count("rag_stage_duration_seconds", stage="rerank") == 2
    assert metrics.histogram_sum("rag_stage_duration_seconds", stage="rerank") < 1.0


# ---------------------------------------------------------------------------
# 8. 人工审核等待时长
# ---------------------------------------------------------------------------
def test_review_wait_seconds_recorded_on_resolve():
    """`review_pending`（还压着多少条）答不了"这一条等了多久" —— 两者都要有。"""
    from app.review_queue import ReviewQueue

    q = ReviewQueue(dsn="", enabled=True)
    metrics.reset()
    assert q.add(thread_id="t-wait", draft="草稿", score=1, feedback="f",
                 tenant="acme") is True
    # 把入队时间往前挪 2 小时（等价于"这条一直没人处理"）
    q._mem["t-wait"]["created_at"] = time.strftime(
        "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 7200))

    assert q.resolve("t-wait", "accepted", actor="u1") is True
    assert metrics.histogram_count("review_wait_seconds", action="accepted") == 1
    assert metrics.histogram_sum("review_wait_seconds", action="accepted") > 7000


def test_review_wait_seconds_absent_when_created_at_unparsable():
    """算不出等待时长时**不上报**，而不是上报一个 0。

    上报 0 会把"没测到"混进"等待时间为零"，正好污染最该被看见的长尾。
    """
    from app.review_queue import ReviewQueue, _waited_seconds

    assert _waited_seconds("not-a-timestamp") is None
    assert _waited_seconds(None) is None

    q = ReviewQueue(dsn="", enabled=True)
    metrics.reset()
    q.add(thread_id="t-bad", draft="d", score=None, feedback="", tenant="")
    q._mem["t-bad"]["created_at"] = "garbage"
    assert q.resolve("t-bad", "accepted") is True
    # 跨标签求和：不带标签的读法恒为 0，验不出"上报了一个 0"这种错。
    assert _count_total("review_wait_seconds") == 0


# ---------------------------------------------------------------------------
# 9. 在线答案质量采样
# ---------------------------------------------------------------------------
def test_online_quality_sampling_is_off_when_rate_is_zero(monkeypatch):
    """采样率写 0 = 关：它是趋势指标，不需要为它给每个请求加几毫秒。"""
    monkeypatch.setattr(config, "ONLINE_QUALITY_SAMPLE", 0.0)
    metrics.reset()
    M._maybe_sample_quality("配置项 `APP_WORKERS` 控制并发", "`APP_WORKERS` 默认 1")
    assert metrics.value("online_answer_judged_total", verdict="pass") == 0
    assert _count_total("online_answer_judged_total") == 0
    assert _count_total("online_answer_hallucinated_total") == 0


def test_online_quality_sampling_defaults_to_one_percent():
    """★ 默认值必须 > 0 —— 这是"线上到底有没有质量数字"的总开关。

    默认 0 时 `online_answer_judged_total` / `online_answer_hallucinated_total`
    **序列不存在（不是 0）**，告警 `OnlineHallucinationDetected` 永远 inactive。
    于是"没在检查"与"质量没问题"在告警界面上完全一样 —— 可用性、延迟、5xx
    全部正常而答案变差的场景，只能等用户投诉。

    所以这条守两处（配置默认值与部署模板），改回 0 必须是一次**有意的**动作：
    删掉这两个断言可以，但要同时接受"线上重新变瞎"。
    """
    root = Path(__file__).resolve().parent.parent
    cfg = (root / "app" / "config.py").read_text(encoding="utf-8")
    assert '_env_float("RAG_ONLINE_QUALITY_SAMPLE", 0.01)' in cfg, (
        "配置默认值被改回 0 了：线上会失去唯一的质量信号"
    )
    example = (root / ".env.example").read_text(encoding="utf-8")
    assert "RAG_ONLINE_QUALITY_SAMPLE=0.01" in example, (
        "部署模板与配置默认值不一致：照模板部署的实例与代码默认行为会给出两套数字"
    )


def test_online_quality_sampling_keeps_three_way_verdict(monkeypatch):
    """三态必须各自成档。

    把"无从判断"并进"通过"会让线上 ok_rate 朝"看起来更好"偏 —— 这正是离线
    口径里反复强调的偏差，在线口径不能反过来犯。
    """
    monkeypatch.setattr(config, "ONLINE_QUALITY_SAMPLE", 1.0)
    metrics.reset()

    # 通过：答案里的标识符在资料里出现过
    M._maybe_sample_quality("配置项 `APP_WORKERS` 控制并发", "`APP_WORKERS` 默认 1")
    assert metrics.value("online_answer_judged_total", verdict="pass") == 1

    # 失败：标识符在资料里不存在 ⇒ 确凿编造
    M._maybe_sample_quality("配置项 `NOPE_KEY` 控制并发", "`APP_WORKERS` 默认 1")
    assert metrics.value("online_answer_judged_total", verdict="fail") == 1
    assert metrics.value("online_answer_hallucinated_total") == 1

    # 无从判断：有标识符但**没有资料可核**（检索为空/降级）
    M._maybe_sample_quality("配置项 `APP_WORKERS` 控制并发", "")
    assert metrics.value("online_answer_judged_total", verdict="unknown") == 1
    # unknown 不该污染幻觉计数
    assert metrics.value("online_answer_hallucinated_total") == 1


def test_online_quality_sampling_skips_empty_answer(monkeypatch):
    monkeypatch.setattr(config, "ONLINE_QUALITY_SAMPLE", 1.0)
    metrics.reset()
    M._maybe_sample_quality("   ", "x")
    assert _count_total("online_answer_judged_total") == 0


# ---------------------------------------------------------------------------
# 10. 埋点与告警必须成对存在
# ---------------------------------------------------------------------------
# 为什么分成两组，而不是"每个新指标都必须有告警"
# --------------------------------------------------
# "值得监控"的完整含义是"它对应的故障能被发现并**通知到人**"，但"发现"与
# "通知"不必是同一件事：
#   ALERTED    —— 故障**只能**从这里看出来 ⇒ 必须有告警，
#                 否则只剩"等用户投诉"这一条路径；
#   DRILL_ONLY —— 故障已经由别的告警兜住了，这些量是**下钻定位**用的：
#                 进面板、进 runbook，不进告警表达式。
# 把第二类也硬塞一条告警只会制造噪音 —— 而长期存在的噪音告警等于没有告警
# （所有人都会开始忽略它）。但第二类仍必须**在 alerts.yml 里被提到**：
# 一个谁都不知道去看的量，和没埋这个点没有区别。
ALERTED = (
    # 事件循环被同步调用占住 ⇒ 所有并发请求一起停住，而错误率为 0。
    # 这条指标是"卡住了"与"慢"之间唯一的区分手段（见 app/loopwatch.py）。
    "event_loop_lag_seconds",
    "llm_call_duration_seconds",
    "llm_call_errors_total",
    "llm_first_token_seconds",
    "circuit_open",
    "review_wait_seconds",
    "online_answer_judged_total",
    "online_answer_hallucinated_total",
    "shared_backend_errors_total",
    "request_timeout_total",
    "rag_retrieval_degraded_total",
    # 客户端在拿同一个 Idempotency-Key 反复提交 —— 挡住的是重复计费，
    # 而**挡不住**的那部分（客户端没带键 / 键过期）在服务端没有任何错误信号。
    "idempotency_conflict_total",
    # 任务执行中的人机交互两条失败面（2026-10-18）：
    #   · run_control   —— 登记/停止失败 ⇒ "停不下来"，而它正是成本与安全的止损阀；
    #   · session_message —— 补充写不进历史 ⇒ "用户说了没听见"，且只有 503 这一个信号。
    # 两者都不影响"任务能不能跑"，所以不会被任何错误率/延迟告警顺带发现。
    "run_control_errors_total",
    "session_message_errors_total",
    # 该停下来等人审、却没能存下现场 —— 表现是"没审就交付"或"审核台 409"，
    # 两者都不报错（见 alerts.yml 的 RunStatePersistFailed）。
    "review_gate_failed_total",
)

DRILL_ONLY = (
    "rag_stage_duration_seconds",    # 整体慢由 rag_retrieval_duration 兜住
    "circuit_opened_total",          # 状态由 circuit_open 兜住，这里是次数
    "circuit_rejected_total",        # 同上：省下了多少次白等
    "agent_tool_calls",              # 异常收尾由 agent_runs_total{exit_reason} 兜住
    # 工具层两点：一个工具是不是**从没被选过**、失败是**参数错**还是**实现错**。
    # 它们都不该单独告警（没有一个"正常值"可断言），但都是 `agent_tool_calls`
    # 那条 runbook 在分诊时真正要落到的那一层 —— 所以必须在 alerts.yml 里被指路。
    "agent_tool_exposed_total",
    "agent_tool_result_total",
    # 跨轮压缩那一次摘要调用的耗时：它压在首 token 之前，但**不是每一次请求**都会
    # 触发（只有历史超过预算时才压），所以不适合单独设阈值告警 —— 它是
    # `FirstTokenTooSlow` 的分诊入口（runbook 指路）。
    "agent_history_compaction_seconds",
    # 进程级资源三条：故障已由 OOM / 容器重启 / EventLoopBlocked 兜住，
    # 这三条用来回答"是内存涨了还是 GC 在抖还是 CPU 打满"。
    "process_resident_memory_bytes",
    "process_cpu_seconds_total",
    "python_gc_collections_total",
    "python_gc_collected_objects_total",
    # 首 token 之后的"顺不顺"：每收到一个 chunk 记一次间隔（口径同 OpenTelemetry
    # 的 `gen_ai.client.inference.time_per_output_chunk`）。
    # 为什么只做下钻、不单独设阈值：**没有生产基线**，阈值只能拍；
    # 而"用户体验已经坏了"这件事有更粗的信号兜着（`client_disconnected_total`
    # 与端到端延迟）。拍一个阈值出来只会制造长期噪音 —— 噪音告警等于没有告警。
    # 真正的用法是：`FirstTokenTooSlow` 之外，用户还抱怨"答到一半卡住"时，
    # 用它区分"上游解码慢"与"我们自己的事件循环被堵"（后者看 `event_loop_lag_seconds`）。
    "llm_inter_chunk_seconds",
    # 幂等键的另外四个分档都只在 `IdempotencyConflictsHigh` 的 runbook 里被指路：
    # 它们回答的是"冲突是哪种冲突"，属于**下钻**而不是独立的故障信号。
    "idempotency_replay_total",      # 被正确吸收的重试次数（机制的价值体现）
    "idempotency_mismatch_total",    # 同键换载荷 = 客户端 bug（非服务端故障）
    "idempotency_rejected_total",    # 键缺失/格式非法 = 接入事故
    "idempotency_truncated_total",   # 响应太大没能缓存 ⇒ 重放退化成 409 指路
    # 补充与停止的**次数**是正常行为，不是故障信号。它们只在上面两条失败告警的
    # description 里被指路 —— 用途是回答"这是普遍现象还是个例"。
    "session_message_total",
    "run_stop_requested_total",
    "run_stopped_total",
)

ALL_NEW_METRICS = ALERTED + DRILL_ONLY


def _source_text() -> str:
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    return "".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in list((root / "app").rglob("*.py"))
        + list((root / "src").rglob("*.py"))
    )


def _alerts_text() -> str:
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    return (root / "deploy" / "monitoring" / "alerts.yml").read_text(encoding="utf-8")


def test_every_new_metric_is_emitted_in_source():
    """每个新指标名都要以**字符串字面量**的形式出现在源码里。

    ⚠️ 为什么必须带上两侧引号，而不是 `m in text`
    -------------------------------------------
    变异验证实测：把 `"circuit_opened_total"` 改成 `"circuit_opened_totals"`
    之后，`"circuit_opened_total" in text` **仍然为真**（前者是后者的子串）
    ⇒ "指标名拼错 ⇒ 序列永远不存在"这一类缺陷**完全扫不到**，
    而这正是本测试存在的理由。带上引号之后，"多打一个字母"与
    "只写在注释里"都不再算数。
    后果的严重性与告警里的 `le="1"` 同类：**永不触发，且没有任何报错**。
    """
    text = _source_text()
    missing = [m for m in ALL_NEW_METRICS if f'"{m}"' not in text]
    assert not missing, (
        f"以下指标名在源码里没有以字符串字面量出现（拼错了？）：{missing}")


def test_alerted_metrics_are_referenced_by_alerts():
    """必须告警的那批，每一条都要真的出现在某个告警表达式里。

    ⚠️ 只在 YAML 的 `runbook` 里提到**不算** —— 那样告警永远不会响，
    只是"文档里写了要看它"。
    """
    cfg = yaml.safe_load(_alerts_text())
    exprs = "\n".join(r.get("expr", "")
                      for g in cfg["groups"] for r in g["rules"])
    missing = [m for m in ALERTED if m not in exprs]
    assert not missing, f"以下指标没有任何告警表达式引用它们：{missing}"


def test_drill_only_metrics_are_documented_in_alerts():
    """仅下钻的量可以不进告警表达式，但必须**在 alerts.yml 里被指路**。

    `agent_tool_calls` 最初就漏在这条上：埋好了、也在 /metrics 里，但没有任何
    runbook 提到它 —— 值班的人遇到"agent 在绕圈子"时不会想到去看它，
    于是这条埋点的实际价值是 0。
    """
    text = _alerts_text()
    missing = [m for m in DRILL_ONLY if m not in text]
    assert not missing, (
        "以下仅下钻的指标在 alerts.yml 里没有任何指路（runbook / description）："
        f"{missing}")


# ---------------------------------------------------------------------------
# 11. 标签名不能撞 metrics 的形参名（会让指标静默丢失）
# ---------------------------------------------------------------------------
import inspect  # noqa: E402

from src import observability  # noqa: E402

_METRIC_CALL_OWNERS = {"observability", "metrics"}
_METRIC_CALL_FUNCS = {"counter", "histogram", "gauge", "inc", "observe",
                      "set_gauge", "set_counter"}

# 每个调用点最终把标签 dict **splat 进**哪个函数。
# `src/observability.py` 只是转发，`app/main.py` 的 `_MetricsSink` 也只是转发 ——
# 真正吃掉标签的是 `app/metrics.py` 的这三个写函数（`metrics.gauge` 是读函数，
# 标签直接进 key，所以它自己就是终端）。
_TERMINAL = {
    ("observability", "counter"): metrics.inc,
    ("observability", "histogram"): metrics.observe,
    ("observability", "gauge"): metrics.set_gauge,
    ("metrics", "inc"): metrics.inc,
    ("metrics", "observe"): metrics.observe,
    ("metrics", "set_gauge"): metrics.set_gauge,
    ("metrics", "set_counter"): metrics.set_counter,
    ("metrics", "gauge"): metrics.gauge,
}


def _param_names(fn) -> list:
    """函数的位置形参名（不含 *args/**kwargs）。用签名派生，不硬编码。"""
    return [n for n, p in inspect.signature(fn).parameters.items()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]


def _iter_metric_call_kwargs():
    """遍历 app/ 与 src/ 下所有"指标调用"的**关键字**实参。

    只认**指标名写成字面量**的调用点（如 `observability.counter("x", k="v")`）：
    这样能自动排除 sink 适配器里的位置转发
    （`metrics.inc(name, amount=amount, **labels)` —— 首参是变量而不是字面量），
    那两处的 `amount` 是形参而非标签，误报会让守卫变成噪音。

    ⚠️ 局限（必须写明白，否则会误以为"守住了"）：指标名若不是字符串字面量
    （f-string、变量、`getattr` 拼的），这里**扫不到**。本项目所有指标名都是
    字面量，所以这是可接受的取舍；新增 API 函数时要同步 `_METRIC_CALL_FUNCS`，
    由下面 `test_guard_is_not_vacuous` 的"扫到的调用点数"兜住这次遗漏。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    files = list((root / "app").rglob("*.py")) + list((root / "src").rglob("*.py"))
    for p in files:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:      # pragma: no cover - 语法错由编译检查负责
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not isinstance(fn, ast.Attribute) or fn.attr not in _METRIC_CALL_FUNCS:
                continue
            owner = fn.value
            owner_name = (owner.id if isinstance(owner, ast.Name)
                          else getattr(owner, "attr", ""))
            if owner_name not in _METRIC_CALL_OWNERS:
                continue
            first = node.args[0] if node.args else None
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            yield p, node.lineno, owner_name, fn.attr, node


def _label_key_problems(path, lineno, owner, func, node) -> list:
    """判断一个调用点的标签里有没有"会被吞掉"的键，返回问题描述列表。

    判定规则（三条，按顺序）
    ----------------------
    1. 该关键字**已由位置实参绑定**（首参必是指标名，第二个是计数/观测值）
       ⇒ 调用点当场 `TypeError`（例：`observability.gauge("x", 0, name="y")`）。
    2. 该关键字是**被调用函数自己的形参** ⇒ 它是"参数"不是标签，放行
       （例：`metrics.inc("tokens", amount=5)` 的 `amount`）。
    3. 否则它是标签 ⇒ 不能与**终端写函数的形参**重名，因为终端是按位置吃
       前两个实参的（`inc(name, amount=1, **labels)`），标签再叫 `name`/`amount`
       会撞成 `TypeError` 并被 `observability._dispatch` 的兜底 `except` 吞掉。
    """
    mod = observability if owner == "observability" else metrics
    called = getattr(mod, func)
    bound_positionally = set(_param_names(called)[: len(node.args)])
    declared = set(_param_names(called))
    terminal_slots = set(_param_names(_TERMINAL[(owner, func)]))

    out = []
    for kw in node.keywords:
        if kw.arg is None:                       # `**labels` 展开，静态看不出来
            continue
        if kw.arg in bound_positionally:
            out.append(f"{path}:{lineno} {owner}.{func}(...) 关键字 {kw.arg!r} "
                       f"与位置实参冲突（调用点就会 TypeError）")
        elif kw.arg in declared:
            continue                             # 是形参，不是标签
        elif kw.arg in terminal_slots:
            out.append(f"{path}:{lineno} {owner}.{func}(...) 标签 {kw.arg!r} "
                       f"与 {_TERMINAL[(owner, func)].__qualname__} 的形参重名"
                       f"（会被 _dispatch 静默吞掉，序列根本不存在）")
    return out


_ALL_METRIC_CALLS = list(_iter_metric_call_kwargs())


def test_guard_is_not_vacuous():
    """守卫本身不能空转：源码里应当扫到几十个指标调用点。

    没有这条，"AST 抽取悄悄失效"（比如指标名改成变量）会让下面那条守卫
    在**零个样本**上通过 —— 一个永远绿的守卫比没有守卫更糟。
    """
    assert len(_ALL_METRIC_CALLS) >= 40, (
        f"只扫到 {len(_ALL_METRIC_CALLS)} 个指标调用点，AST 抽取可能失效了")


def test_metric_labels_never_shadow_metrics_parameter_names():
    """标签键不能与观察函数的形参名重名（实测事故，不是洁癖）。

    事故经过
    -------
    熔断器最初把熔断器名字写成标签 `name=`：

        observability.gauge("circuit_open", 0 if ... else 1, name=self.name)

    `observability.gauge(name, value, **labels)` 的第一个形参就叫 `name`，
    已被位置实参占掉 ⇒ **在调用点当场抛**
    `TypeError: gauge() got multiple values for argument 'name'`。

    ⚠️ 这里最容易写错的一句是"被兜底 except 吞掉、只是指标没了"。
    2026-10-02 用探针确认过：异常抛在 `observability.gauge(...)` **这一行本身**，
    `_dispatch` 的兜底 `except` **根本轮不到**。后果比"没指标"严重：
    `CircuitBreaker.record_success()` 在**每次成功调用**后都会走到它 ⇒ 异常穿透
    `guarded_call` ⇒ 被检索调用方的宽 `except` 接住 ⇒ **整条检索永久降级为
    "无上下文"**，而 `circuit_open` / `circuit_opened_total` /
    `circuit_rejected_total` 三条序列一条都不存在。

    为什么守卫扫**源码**而不是某次运行的读数：`record_success` 那条路径在离线
    测试里很少被走到，而且"标签写错"这件事在任何一次具体运行里都只是"没报错"
    —— 纯运行时断言看不见它。
    """
    bad = [msg for p, ln, owner, func, node in _ALL_METRIC_CALLS
           for msg in _label_key_problems(p, ln, owner, func, node)]
    assert not bad, "这些标签会让指标调用当场抛异常：\n" + "\n".join(bad)


def test_set_sink_rejects_an_object_of_the_wrong_shape():
    """接错线的出口要在**注入时**就被拒绝。

    最容易犯的错：把 `app.metrics` **模块**当 sink 传进来。模块上有
    `inc` / `observe` / `set_gauge`，但**没有** `counter` / `histogram` / `gauge`
    ⇒ `_dispatch` 的 `getattr` 抛 AttributeError ⇒ 被兜底 `except` 吞掉
    ⇒ **全部指标静默变成空操作**：服务正常、探针正常、/metrics 空白。
    （本文件作者写探针时就连踩两次。）
    """
    saved = observability.get_sink()
    try:
        with pytest.raises(TypeError):
            observability.set_sink(metrics)         # 模块：形状不对
        with pytest.raises(TypeError):
            observability.set_sink(object())        # 什么都没有
        observability.set_sink(None)                # None 是合法值（=关闭上报）
        assert observability.get_sink() is None
        observability.set_sink(M._MetricsSink())    # 正确形状
        assert observability.get_sink() is not None
    finally:
        observability.set_sink(saved)


def test_new_alert_names_are_registered():
    """新增的告警必须真的在 alerts.yml 里（防止"改了本地副本没落到文件"）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    cfg = yaml.safe_load(
        (root / "deploy" / "monitoring" / "alerts.yml").read_text(encoding="utf-8"))
    names = {r["alert"] for g in cfg["groups"] for r in g["rules"]}
    expected = {
        "SharedBackendDegraded",
        "RequestTimeoutFrequent",
        "RagRetrievalDegraded",
        "RetrievalCircuitOpen",
        "FirstTokenTooSlow",
        "LlmCallErrorsHigh",
        "RunStatePersistFailed",
        "OnlineHallucinationDetected",
        "ReviewWaitTooLong",
        "IdempotencyConflictsHigh",
    }
    missing = expected - names
    assert not missing, f"以下告警没有写进 alerts.yml：{sorted(missing)}"
