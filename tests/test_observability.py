"""可观测层离线测试：结构化日志 / request_id 上下文 / 指标 / 请求中间件。

全部不依赖外部服务。middleware 用最小 FastAPI 应用跑通，避免拉入真实 graph。
"""
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import metrics
from app.logging_setup import JsonFormatter, TextFormatter, get_request_id, reset_request_id, set_request_id
from app.middleware import RequestContextMiddleware


# ---------------- 日志 ----------------
def _record(msg="hello", level=logging.INFO, name="t"):
    return logging.LogRecord(name=name, level=level, pathname=__file__, lineno=1,
                             msg=msg, args=(), exc_info=None)


def test_json_formatter_emits_valid_json_with_core_fields():
    out = JsonFormatter().format(_record("你好"))
    payload = json.loads(out)
    assert payload["msg"] == "你好"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "t"
    assert "ts" in payload


def test_json_formatter_includes_request_id_from_contextvar():
    token = set_request_id("rid-123")
    try:
        payload = json.loads(JsonFormatter().format(_record()))
        assert payload["request_id"] == "rid-123"
    finally:
        reset_request_id(token)
    # 还原后不应再带上 rid
    assert "request_id" not in json.loads(JsonFormatter().format(_record()))


def test_json_formatter_keeps_extra_fields():
    rec = _record()
    rec.stage = "retrieve"
    assert json.loads(JsonFormatter().format(rec))["stage"] == "retrieve"


def test_text_formatter_is_single_line_without_rid():
    line = TextFormatter().format(_record("hi"))
    assert "hi" in line and "\n" not in line


def test_request_id_defaults_to_empty_outside_request():
    assert get_request_id() == ""


# ---------------- 指标 ----------------
def _prometheus_text_violations(text: str) -> list[str]:
    """按 **Prometheus 服务端（Go `expfmt`）** 的规则检查文本格式，返回违规清单。

    ⚠️ 为什么不能拿 `prometheus_client` 的解析器当判据：它是**宽松**实现 ——
    对"同一个指标名出现多行 `# TYPE`"不报错，只静默保留第一条。2026-10-06 实测：
    同一段含 3 行重复 TYPE 的文本，Go 报
    `second TYPE line for metric name "x", or TYPE reported after samples` 并中止解析，
    而 `prometheus_client.parser.text_string_to_metric_families()` **解析成功**。
    用它做断言 = 这条真实缺陷永远显示为绿。
    """
    import re as _re

    declared: dict[str, str] = {}       # 指标名 -> 已声明的类型
    seen_samples: set[str] = set()
    problems: list[str] = []

    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# TYPE "):
            name = line.split()[2]
            if name in declared:
                problems.append(f"第 {n} 行：{name} 出现第二行 # TYPE")
            elif name in seen_samples:
                problems.append(f"第 {n} 行：{name} 的 # TYPE 出现在样本之后")
            declared[name] = line.split()[3]
            continue
        if line.startswith("#"):
            continue
        m = _re.match(r"([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{|[ \t]|$)", line)
        assert m, f"第 {n} 行不是合法的样本行：{line}"
        # 直方图的样本名带 `_bucket`/`_sum`/`_count` 后缀，归并回它所属的 family
        family = _re.sub(r"_(bucket|sum|count)$", "", m.group(1))
        seen_samples.add(family)
        if family not in declared:
            declared[family] = "untyped"
    return problems


def test_render_emits_one_type_line_per_metric_name():
    """每个指标名**恰好一行** `# TYPE` —— 否则整个 /metrics 是非法文本。

    2026-10-06 回归钉子。旧实现按"每个 (指标名, 标签组合)"打一行 TYPE，
    于是 `http_requests_total` 有几个标签组合就有几行 TYPE。Prometheus 服务端
    遇到第二行会报 `second TYPE line for metric name ...` 并**立即中止解析**、
    整批样本丢弃（target 变 `up == 0`）。
    危害量级不是"少一个指标"，而是全部指标 / SLO / 燃尽率 / 告警规则依赖的数据
    根本不存在 —— 而单测若用宽松解析器会误判为正常。
    """
    metrics.reset()
    for status in ("200", "404", "429", "500"):
        metrics.inc("http_requests_total", method="GET", path="/v1/chat", status=status)
    metrics.set_gauge("review_pending", 3)
    metrics.set_gauge("review_pending", 5, tenant="acme")
    metrics.observe("rag_seconds", 0.1, stage="retrieve")
    metrics.observe("rag_seconds", 0.2, stage="rerank")

    text = metrics.render()
    for name in ("http_requests_total", "review_pending", "rag_seconds"):
        assert text.count(f"# TYPE {name} ") == 1, f"{name} 的 TYPE 行不唯一：\n{text}"
    assert _prometheus_text_violations(text) == []


def test_render_puts_every_type_line_before_its_samples():
    """`# TYPE` 必须排在该指标名的**所有样本之前**（规范原文要求）。

    与上一条是两件事：TYPE 唯一但仍排在样本之后，Go 同样报
    "TYPE reported after samples"。旧实现在第 2 个标签组合处就会踩到这条。
    """
    metrics.reset()
    metrics.inc("a_total", x="1")
    metrics.inc("a_total", x="2")
    metrics.set_gauge("b_gauge", 1)
    metrics.observe("c_seconds", 0.1, k="v")

    text = metrics.render()
    assert _prometheus_text_violations(text) == []

    lines = text.splitlines()
    type_at = next(i for i, ln in enumerate(lines) if ln.startswith("# TYPE a_total "))
    first_sample = next(i for i, ln in enumerate(lines) if ln.startswith("a_total{"))
    assert type_at < first_sample, "TYPE 落在了样本之后"

    # 直方图同理：`c_seconds` 的 TYPE 必须在 `c_seconds_bucket` 之前
    h_type = next(i for i, ln in enumerate(lines) if ln.startswith("# TYPE c_seconds "))
    h_sample = next(i for i, ln in enumerate(lines) if ln.startswith("c_seconds_bucket"))
    assert h_type < h_sample


def test_misdeclared_metric_name_does_not_break_the_whole_exposition():
    """同一个名字既是 counter 又是 gauge（编程错误）时，宁可跳过它也不能输出两行 TYPE。

    取舍：这个撞名是**代码 bug**，但它的代价不该由整个 /metrics 承担 ——
    让一次撞名把全部监控打瞎，比少一个指标严重得多。所以 render() 记 ERROR
    并保留先注册的类型。这条钉住的就是"错误被降级为局部"。
    """
    metrics.reset()
    metrics.inc("dup_total")
    metrics.set_gauge("dup_total", 1)

    text = metrics.render()
    assert text.count("# TYPE dup_total ") == 1, text
    assert _prometheus_text_violations(text) == []


def test_counter_and_histogram_render_prometheus_text():
    metrics.reset()
    metrics.inc("demo_total", 2, a="1")
    metrics.inc("demo_total", a="1")
    metrics.observe("demo_seconds", 0.3, path="/x")

    text = metrics.render()
    assert 'demo_total{a="1"} 3.0' in text
    assert "# TYPE demo_seconds histogram" in text
    assert 'demo_seconds_count{path="/x"} 1' in text
    # 桶要累计（0.3 落在 <=0.5 及之后的所有桶里）
    assert 'demo_seconds_bucket{le="0.5",path="/x"} 1' in text
    assert "process_uptime_seconds" in text


def test_histogram_buckets_are_cumulative_and_fixed_size():
    """桶必须**累积**，且格数**固定**。

    累积是 Prometheus 的 `_bucket` 语义（le = 小于等于），客户端算分位数全靠它；
    格数固定则是"内存不随请求量增长"的直接证据 —— 旧实现把每次观测 append 进列表，
    一个被刷的接口或一次压测就能把进程内存顶到 OOM。
    """
    metrics.reset()
    for v in (0.004, 1.0, 2.0, 999.0):
        metrics.observe("h", v, k="v")

    bounds = metrics.bucket_bounds()
    cum = metrics.histogram_values("h", k="v")
    assert len(cum) == len(bounds) + 1          # 最后一格是 +Inf
    assert cum == sorted(cum)                   # 累积 → 单调不减
    assert cum[-1] == 4                         # +Inf 格 = 全部观测
    assert cum[0] == 1                          # 0.004 落进第一格（le=0.005）
    assert cum[bounds.index(1.0)] == 2          # 0.004 + 1.0
    assert cum[bounds.index(2.5)] == 3          # 再加 2.0
    assert cum[-2] == 3                         # 999 超出所有边界，只在 +Inf 格里
    assert metrics.histogram_count("h", k="v") == 4


def test_histogram_storage_is_independent_of_observation_count():
    """两万次观测后存储**不能**变大 —— 这是"内存有界"的直接断言。

    只断言"计数正确"是不够的：计数正确与内存有界是两件事，旧实现对前者也成立。
    """
    metrics.reset()
    for i in range(20000):
        metrics.observe("flood", (i % 997) / 1000.0)

    assert metrics.histogram_count("flood") == 20000
    assert metrics.histogram_slots("flood") == len(metrics.bucket_bounds()) + 1
    assert metrics.histogram_sum("flood") > 0


def test_histogram_boundary_value_lands_in_its_own_bucket():
    """恰好等于边界的值必须落进**该**格（le 语义），不能差一格。

    差一格会让基于桶的分位数估算系统性偏移，而且偏移随桶宽变化 ——
    对不上账时也看不出是这里的问题。
    """
    metrics.reset()
    edge = metrics.bucket_bounds()[0]           # 0.005
    metrics.observe("edge", edge)
    assert metrics.histogram_values("edge")[0] == 1, "等于边界的观测没落进这一格"


def test_histogram_render_and_readers_share_one_definition():
    """渲染出来的 `_bucket` 与 `histogram_values()` 必须同口径（都累积）。

    两处各自算一遍是这种小模块最典型的漂移来源：改一处、另一处静默不一致。
    """
    metrics.reset()
    metrics.observe("r", 0.3)
    metrics.observe("r", 5.0)

    text = metrics.render()
    assert 'r_bucket{le="0.5"} 1' in text
    assert 'r_bucket{le="5.0"} 2' in text
    assert 'r_bucket{le="+Inf"} 2' in text
    assert "r_count 2" in text
    assert metrics.histogram_values("r")[-1] == 2


# ---------------- 中间件 ----------------
def _client():
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"rid": get_request_id()}

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    app.add_middleware(RequestContextMiddleware, enabled=True)
    return TestClient(app, raise_server_exceptions=False)


def test_middleware_generates_and_echoes_request_id():
    metrics.reset()
    r = _client().get("/ping")
    assert r.status_code == 200
    rid = r.headers.get("X-Request-ID")
    assert rid and len(rid) >= 8
    # 上下文里的 rid 与响应头一致（业务代码能打到同一条日志）
    assert r.json()["rid"] == rid


def test_middleware_honours_incoming_request_id():
    r = _client().get("/ping", headers={"X-Request-ID": "abc123"})
    assert r.headers["X-Request-ID"] == "abc123"
    assert r.json()["rid"] == "abc123"


# 上游 request_id 是**不可信输入**（网关不在我们控制内）：它会被写进该请求的
# 每一行日志并回写响应头，所以必须限长 + 限字符集，否则可被用来做日志放大
# （塞 100KB 字符串）或响应头注入（含 \r\n）。校验不过时**忽略并自行生成**，
# 不让请求失败 —— rid 只是排障辅助。
@pytest.mark.parametrize("bad", [
    "x" * 200,                      # 超长 → 日志放大
    "bad\nvalue",                   # 换行 → 日志伪造 / 头注入
    "bad\r\nX-Injected: 1",         # CRLF → 响应头注入
    "has space",                    # 空格不在白名单
    "semi;colon=1",                 # 分隔符/等号不在白名单
    "tab\there",                    # 控制字符
])
def test_middleware_rejects_unsafe_incoming_request_id(bad):
    r = _client().get("/ping", headers={"X-Request-ID": bad})
    rid = r.headers["X-Request-ID"]
    assert rid != bad, "不可信的上游 rid 被原样透传了"
    assert len(rid) <= 64 and "\n" not in rid and "\r" not in rid
    # 仍然给了可用的 rid（不是空串），且上下文与响应头一致
    assert rid and r.json()["rid"] == rid


def test_middleware_rejects_high_byte_request_id():
    """高位字节（latin-1 的 0x80–0xFF）也在拒绝范围内。

    单列一条是因为 httpx **客户端侧**不允许 str 形式发非 ASCII 头，只有 bytes 能发过去；
    而真实链路上（裸 socket / 某些网关）这类字节是发得出来的 —— 服务端不该依赖
    客户端库的约束来保证安全。
    """
    r = _client().get("/ping", headers={"X-Request-ID": b"\xe9abc"})
    rid = r.headers["X-Request-ID"]
    assert rid and rid != "\xe9abc"
    assert all(ch.isascii() for ch in rid), f"回写了非 ASCII 的 rid：{rid!r}"


@pytest.mark.parametrize("good", ["abc123", "req-1.2_3", "A" * 64, "0f3d-9a"])
def test_middleware_keeps_reasonable_incoming_request_id(good):
    """合规的 rid 必须原样保留 —— 否则上游的链路追踪就断了（追溯不到同一次调用）。"""
    r = _client().get("/ping", headers={"X-Request-ID": good})
    assert r.headers["X-Request-ID"] == good


def test_middleware_records_metrics_and_error_counter():
    metrics.reset()
    c = _client()
    c.get("/ping")
    c.get("/boom")

    text = metrics.render()
    assert 'http_requests_total{method="GET",path="/ping",status="200"}' in text
    assert 'http_errors_total{path="/boom"}' in text


# ---------------------------------------------------------------------------
# tracing 接线（2026-10-20 重写：从"往 LangGraph config 里塞 metadata"改成
# "接管官方 SDK 的 trace provider"）
# ---------------------------------------------------------------------------
# ⚠️ 这里原本有两条用例在测 `tracing.build_run_config` 把 `request_id` 写进
# `metadata`（"用户报 X-Request-ID，就能在面板里反查到那条 trace"）。那个字典
# 从来没有读者 —— 官方 `Runner.run` 不认 `configurable` / `metadata` —— 所以那
# 两条用例守的是一个**死行为**，随函数一起删掉了。
#
# request_id → trace 这条链在 2026-10-21 补上了，入口是 `tracing.trace_scope()`
# （Langfuse 的 `propagate_attributes`）。**没有**用 OpenInference 的
# `using_attributes`，也没有用官方的 `TracingConfig.metadata`：这个插桩两样都不读
# （`_processor.py::on_trace_start` 只设 span kind）。详见 `app/tracing.py` 的模块
# docstring，那里贴了三条路的源码依据。
def test_no_configured_backend_means_no_trace_at_all(monkeypatch):
    """没配任何后端 ⇒ 官方 SDK **根本不产生 trace**，而不是"产生但没地方送"。

    ⚠️ 为什么必须钉住：SDK 的 trace provider 在**第一次访问时**会自动注册
    `default_processor()` —— 那是 `BatchTraceProcessor(BackendSpanExporter())`，
    会拿环境里的 `OPENAI_API_KEY` 往 api.openai.com 发 span（**含 prompt 与
    回复全文**）。本项目模型走自建 `AsyncOpenAI` 客户端（配置项叫 `AGNES_*`），
    所以那条上报要么用错 key 把对话发给第三方、要么失败被吞 —— 两种都不报错、
    也不体现在任何指标上。

    判据取自官方 `DefaultTraceProvider.create_trace`：disabled 时返回
    `NoOpTrace`（`trace_id == "no-op"`），而不是"照常建 trace 但没人收"。
    """
    from agents.tracing import get_trace_provider

    from app import tracing

    monkeypatch.setattr(tracing.config, "LANGSMITH_API_KEY", "")
    monkeypatch.setattr(tracing.config, "LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setattr(tracing.config, "LANGFUSE_SECRET_KEY", "")
    assert tracing.init_tracing() == "none"
    assert get_trace_provider().create_trace("probe").trace_id == "no-op", (
        "tracing 没被关掉：SDK 的默认处理器仍活着，会把 prompt 与回复发往 OpenAI 后端"
    )


def test_langfuse_without_its_dependencies_is_not_claimed(monkeypatch):
    """配了 Langfuse 但缺依赖 ⇒ 必须如实返回 "none"。

    这是旧实现的反面教材：它把回调挂在那个没人读的 config 键上，却返回
    "langfuse" —— 面板里一条 trace 都没有，而启动日志说"已启用"。
    这里用 `sys.modules[name] = None` 制造"导入失败"（import 会抛 ImportError），
    与真实环境有没有装包无关，所以将来装了包这条也不会变红。
    """
    import sys

    from app import tracing

    monkeypatch.setattr(tracing.config, "LANGSMITH_API_KEY", "")
    monkeypatch.setattr(tracing.config, "LANGFUSE_PUBLIC_KEY", "pk-lf-x")
    monkeypatch.setattr(tracing.config, "LANGFUSE_SECRET_KEY", "sk-lf-x")
    monkeypatch.setitem(sys.modules, "langfuse", None)
    monkeypatch.setitem(sys.modules, "openinference.instrumentation.openai_agents", None)
    assert tracing.init_tracing() == "none"


def test_langfuse_goes_through_the_official_instrumentor(monkeypatch):
    """配齐密钥且依赖在时，必须走 `OpenAIAgentsInstrumentor` 这条路。

    为什么这条要单独钉：真的 langfuse 没装，所以"配了"这条分支在 CI 里永远不会
    被执行。而**旧实现就是在这一支上骗人的** —— 它返回 "langfuse"，实际只是把
    一个回调塞进没人读的 config 键。用两个替身模块把"到底调了什么"钉住：

      · 必须调用插桩的 `instrument()`（它内部才做
        `agents.set_trace_processors([...])`，把默认的 OpenAI 上报顶掉）；
      · **不能**在这条路上再调 `set_tracing_disabled(True)` —— 那会让 SDK
        根本不产生 span，插桩收不到任何东西（配了等于没配）。
    """
    import sys
    import types

    from agents.tracing import get_trace_provider

    from app import tracing

    calls: dict[str, object] = {}

    class _FakeInstrumentor:
        def instrument(self, **kwargs):
            calls["instrument"] = kwargs

    def _fake_pkg(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        mod.__path__ = []  # 让它像个包，`from a.b import c` 才走得通
        return mod

    oi_agents = types.ModuleType("openinference.instrumentation.openai_agents")
    oi_agents.OpenAIAgentsInstrumentor = _FakeInstrumentor
    lf = types.ModuleType("langfuse")
    lf.get_client = lambda: calls.setdefault("client", object())

    monkeypatch.setitem(sys.modules, "openinference", _fake_pkg("openinference"))
    monkeypatch.setitem(sys.modules, "openinference.instrumentation",
                        _fake_pkg("openinference.instrumentation"))
    monkeypatch.setitem(sys.modules, "openinference.instrumentation.openai_agents", oi_agents)
    monkeypatch.setitem(sys.modules, "langfuse", lf)

    monkeypatch.setattr(tracing.config, "LANGSMITH_API_KEY", "")
    monkeypatch.setattr(tracing.config, "LANGFUSE_PUBLIC_KEY", "pk-lf-x")
    monkeypatch.setattr(tracing.config, "LANGFUSE_SECRET_KEY", "sk-lf-x")
    # 上一个用例可能把 provider 置成了 disabled（`_manual_disabled` 优先于环境变量），
    # 这里复位回"未手工干预"，否则本用例的断言会因为**别的用例**的副作用而假绿。
    provider = get_trace_provider()
    monkeypatch.setattr(provider, "_manual_disabled", None, raising=False)
    provider._refresh_disabled_flag()

    assert tracing.init_tracing() == "langfuse"
    assert "instrument" in calls, "配了 Langfuse 却没调插桩 —— 那就等于什么都没接"
    assert "client" in calls, "没构造 Langfuse 客户端（OTel exporter 没装上）"
    assert provider.create_trace("probe").trace_id != "no-op", (
        "tracing 被关掉了：插桩拿不到 span，配了等于没配"
    )


def test_trace_scope_is_inert_when_no_backend_is_configured(monkeypatch):
    """没配后端 ⇒ `trace_scope` 必须退化成 `nullcontext`，一位都不写。

    为什么不用 try 兜住"导入失败"就算完：请求热路径上每次调用都去 `import langfuse`
    会白白付一次模块查找；更要紧的是 —— 如果这里返回的是真作用域，而底层没有
    TracerProvider，`attach/detach` 照样会跑，出问题时表现为"随机某个请求的 span
    带着别人的属性"，极难查。所以判据是"没启用就什么都不做"。
    """
    import contextlib

    from app import tracing

    monkeypatch.setattr(tracing, "_ACTIVE_BACKEND", "none")
    scope = tracing.trace_scope(request_id="rid-1", thread_id="t-1", tenant="acme")
    assert isinstance(scope, contextlib.nullcontext)
    with scope:      # 进出都不该抛
        pass


def test_trace_scope_hands_the_request_id_and_session_to_langfuse(monkeypatch):
    """配了 Langfuse ⇒ 把 request_id / 会话 / 租户交给 `propagate_attributes`。

    为什么是它、不是 `using_attributes` 或官方的 `TracingConfig.metadata`：
    这个插桩两样都不读（`_processor.py::on_trace_start` 只设 span kind），而
    Langfuse 的 SpanProcessor 会在 `on_start` 里把 context 里的传播属性盖到**任何**
    span 上。依据见 `app/tracing.py` 的模块 docstring。
    """
    import contextlib
    import sys
    import types

    from app import tracing

    seen: dict = {}

    def _fake_propagate(**kwargs):
        seen.update(kwargs)
        return contextlib.nullcontext()

    lf = types.ModuleType("langfuse")
    lf.propagate_attributes = _fake_propagate
    monkeypatch.setitem(sys.modules, "langfuse", lf)
    monkeypatch.setattr(tracing, "_ACTIVE_BACKEND", "langfuse")

    with tracing.trace_scope(request_id="rid-abc", thread_id="t-9", tenant="acme"):
        pass

    assert seen["session_id"] == "t-9", "会话没交给 Langfuse：多条 trace 聚不到同一个 session 上"
    assert seen["metadata"] == {"request_id": "rid-abc", "tenant": "acme"}, (
        "request_id 没进 trace 属性 —— 用户报的 rid 就只能搜到日志、搜不到链路"
    )


def test_the_turn_runs_inside_the_trace_scope(monkeypatch):
    """`_stream_run` 必须**真的**把运行包在作用域里，且顺序是"先进入、再跑"。

    这一条是上一版 tracing 的死因复刻：函数写对了、没人调，代码看起来接上了而一条
    trace 都没有。所以钉两件事：
      · 作用域拿到了 rid / 会话 / 租户（从 `get_request_id()` 与 turn 读，不是空转）；
      · 进入作用域发生在 `run_turn` **之前**、退出在其后 —— 属性是靠 span **启动时**
        读 context 生效的，包错了位置等于没挂。
    """
    import asyncio
    import contextlib

    from app import main as M
    from src.agents.runner import TaskOutcome

    seen: dict = {}

    @contextlib.contextmanager
    def _fake_scope(**kwargs):
        seen["kwargs"] = kwargs
        seen["entered"] = True
        yield
        seen["exited"] = True

    class _Turn:
        async def __call__(self, req, *, context, on_text=None):
            seen["entered_before_run"] = seen.get("entered", False)
            return TaskOutcome(answer="ok", attempts=1,
                               final_agent="frontline_agent",
                               passed=True, exit_reason="passed")

    monkeypatch.setattr(M.tracing, "trace_scope", _fake_scope)
    monkeypatch.setattr(M, "run_turn", _Turn())

    async def _drain() -> list:
        return [ev async for ev in M._stream_run(
            M.TurnRequest(prompt="hi", thread_id="t-1", tenant="t1"), "chat",
            tenant="t1", subject="u1", thread_id="t-1")]

    token = set_request_id("rid-wire")
    try:
        events = asyncio.run(_drain())
    finally:
        reset_request_id(token)

    assert seen["kwargs"] == {"request_id": "rid-wire", "thread_id": "t-1",
                              "tenant": "t1"}, "作用域拿到的是空值 —— 那等于没挂"
    assert seen["entered_before_run"] is True, "作用域在运行开始之后才进 —— span 已经建好了"
    assert seen.get("exited") is True
    assert any(e.get("event") == "done" for e in events)
