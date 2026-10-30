"""接口层测试（P0/P2 新增能力）：版本化路由、探针、指标、限流、鉴权、租户、审核队列。

全部离线：不触发真实检索与模型调用（只打"进不了图"的路径）。
"""
import os

import pytest

# 与 test_api.py 同口径：清掉 DB_URL，避免测试去连真库
os.environ["DB_URL"] = ""

pytest.importorskip("fastapi.testclient", reason="TestClient 需要 httpx")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as M  # noqa: E402
from app.ratelimit import RateLimiter  # noqa: E402
from tests.auth_util import TEST_USER, auth_headers  # noqa: E402

client = TestClient(M.app, headers=auth_headers())


def _park_review(thread_id: str, *, draft="草稿", score=None, feedback="意见"):
    """在**待审队列**里造一条"停在审核"的记录，并返回那个队列。

    ⚠️ 2026-10-20 起 `POST /v1/review` 的 409 判据是"有没有待审记录或运行现场"
    （见端点里的 `record` / `held`）。伪造执行图的 `next` 已经骗不过它了 ——
    图本身已经不存在。``manual`` 那类（跑偏 / 护栏）没有运行现场，队列就是它的
    全部状态，所以这里造队列记录就能覆盖端点的全部分支。
    """
    from app.review_queue import ReviewQueue

    q = ReviewQueue(dsn="")
    q.add(thread_id, draft=draft, score=score, feedback=feedback, tenant="")
    return q


def _fake_turn(answer="改好的草稿", *, review_reason=""):
    """替身运行：`/v1/review` 的每个分支最后都会跑一轮，这里只需吐一段正文。"""
    from src.agents.runner import TaskOutcome

    async def _run(req, *, context, on_text=None, trace_metadata=None):
        if on_text is not None and answer:
            await on_text(answer, "call-1")
        return TaskOutcome(
            answer=answer,
            attempts=1,
            final_agent="frontline",
            passed=True,
            exit_reason="passed",
            review_reason=review_reason,
        )

    return _run


# ---------------- 版本化路由 ----------------
def test_v1_review_validates_action():
    r = client.post("/v1/review", json={"thread_id": "t", "action": "nope"})
    assert r.status_code == 400


def test_v1_review_unknown_thread_returns_409():
    r = client.post("/v1/review", json={"thread_id": "no-such", "action": "accept",
                                       "feedback": ""})
    assert r.status_code == 409


def test_legacy_and_v1_paths_both_work():
    """旧路径是别名，行为必须与 /v1 一致（兼容既有客户端）。"""
    a = client.post("/review", json={"thread_id": "t", "action": "nope"})
    b = client.post("/v1/review", json={"thread_id": "t", "action": "nope"})
    assert a.status_code == b.status_code == 400


# ---------------- 探针 ----------------
def test_health_compat_is_unchanged():
    assert client.get("/health").json() == {"status": "ok"}


def test_health_live_reports_release():
    body = client.get("/health/live").json()
    assert body["status"] == "ok"
    assert "version" in body and "release" in body


def test_health_ready_503_when_degraded(monkeypatch):
    monkeypatch.setattr(M.health, "readiness",
                        lambda: (False, {"status": "degraded", "checks": {}}))
    r = client.get("/health/ready")
    assert r.status_code == 503


def test_health_ready_200_when_ok(monkeypatch):
    monkeypatch.setattr(M.health, "readiness",
                        lambda: (True, {"status": "ok", "checks": {}}))
    assert client.get("/health/ready").status_code == 200


# ---------------- 指标 ----------------
def test_metrics_endpoint_returns_prometheus_text():
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "process_uptime_seconds" in r.text


# ---------------- 鉴权 ----------------
def test_unauthenticated_request_is_rejected():
    """不带凭证的业务请求必须 401。

    ⚠️ 这里刻意**新建**一个不带默认头的客户端：模块级 `client` 带着测试身份
    （见 tests/auth_util.py），拿它测不出"裸请求会被拒"。
    """
    bare = TestClient(M.app)
    r = bare.post("/v1/review", json={"thread_id": "t", "action": "accept"})
    assert r.status_code == 401


def test_shared_api_key_header_is_not_accepted_anymore():
    """`X-API-Key` 这条路径已整体删除 —— 带着它也一样 401。

    这条守卫的意义：删掉共享密钥档之后，"客户端还在发这个头"是一个**必须**
    表现为失败的状态。若哪天有人把它加回来（哪怕只是"兼容一下"），本条会红。
    """
    bare = TestClient(M.app)
    r = bare.post("/v1/review", json={"thread_id": "t", "action": "accept"},
                  headers={"X-API-Key": "sekret"})
    assert r.status_code == 401


def test_bearer_token_passes_auth():
    """带上真签的令牌即可通过鉴权：409（不在审核态）说明已经进到业务逻辑。"""
    r = client.post("/v1/review", json={"thread_id": "t", "action": "accept"})
    assert r.status_code != 401


# ---------------- 限流 ----------------
def test_rate_limit_returns_429_with_retry_after(monkeypatch):
    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=1, by_session=False))
    body = {"thread_id": "rl-thread", "action": "accept", "feedback": ""}
    first = client.post("/v1/review", json=body)
    second = client.post("/v1/review", json=body)
    assert first.status_code != 429
    assert second.status_code == 429
    assert "Retry-After" in second.headers


def test_quota_exhausted_returns_429(monkeypatch):
    """额度用尽时 /review 返回 429（而不是先被 409 拦掉）。

    必须把图打成"停在审核态"：配额预占排在业务校验**之后**（为什么不排前面见
    `_sse` 的说明），所以不造这个前提，先返回的是 409 —— 用例照样"绿"，但测的
    根本不是配额。
    """

    from app.quota import QuotaManager

    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=1000))
    monkeypatch.setattr(M, "_QUOTA", QuotaManager(daily_limit=1))
    monkeypatch.setattr(M, "run_turn", _fake_turn())
    # 前提：队列里真躺着一条待审记录（否则先返回的是 409，测的就不是配额了）。
    monkeypatch.setattr(M, "_REVIEW_QUEUE", _park_review("q-thread"))
    M._QUOTA.add(M.config.DEFAULT_TENANT, 5)     # 把默认租户额度用光

    r = client.post("/v1/review", json={"thread_id": "q-thread", "action": "accept"})
    assert r.status_code == 429


def test_review_409_does_not_leak_quota(monkeypatch):
    """409（不在审核态）这条路径**不能**留下配额预占。

    预占若放在业务校验之前，这条路径会带着一份预占直接返回；而退还在
    `_stream_run` 的 finally 里 —— 那个 finally 永远不会执行。表现是"配额莫名
    少了一截"，没有任何异常、也没有日志可查，只能靠对账发现。
    """
    from app.quota import QuotaManager

    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=1000))
    monkeypatch.setattr(M, "_QUOTA", QuotaManager(daily_limit=10 ** 6, request_limit=500))

    r = client.post("/v1/review", json={"thread_id": "leak-src", "action": "accept"})
    assert r.status_code == 409
    assert M._QUOTA.check(M.config.DEFAULT_TENANT).used == 0, "409 路径泄漏了配额预占"


def test_chat_settles_quota_with_actual_tokens(monkeypatch):
    """端到端：预占必须被**实际消耗**替换，而不是留在计数里。

    这是 claim→settle 成对与否唯一能被观察到的地方：请求结束后用量应等于实际产出的
    token，而不是 request_limit。留成 request_limit 就是预占泄漏 —— 每次请求都多算
    一份预占，额度会以远快于真实消耗的速度耗尽。
    """

    from app.quota import QuotaManager, estimate_tokens

    draft = "六堡茶是梧州特产。"

    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=1000))
    monkeypatch.setattr(M, "_QUOTA", QuotaManager(daily_limit=10 ** 6, request_limit=500))
    monkeypatch.setattr(M, "run_turn", _fake_turn(answer=draft))

    r = client.post("/v1/chat", json={"thread_id": "settle-1", "message": "hi"})
    assert r.status_code == 200

    used = M._QUOTA.check(M.config.DEFAULT_TENANT).used
    want = estimate_tokens(draft)
    assert used == want, (
        f"结算后用量应为实际产出 {want}，实际 {used}"
        "（等于预占 500 说明 settle 没被调用）"
    )


# ---------------- 审核动作的审计留痕 ----------------
def test_review_action_records_who_did_it(monkeypatch):
    """端到端：`/v1/review` 必须把**操作者与认证方式**写进队列记录。

    只断言"状态变成 accepted"是不够的 —— 那是改造前就有的行为。这条用例要钉的是
    `_REVIEW_QUEUE.resolve(... actor=..., note=...)` 这串关键字参数真的从端点传下去了
    （漏传时 `resolve` 照样成功返回 True，队列照样出队，只有审计字段是空的）。
    """

    from app.review_queue import ReviewQueue

    q = ReviewQueue(dsn="")
    q.add("audit-1", draft="草稿", score=None, feedback="太短", tenant="acme")
    monkeypatch.setattr(M, "_REVIEW_QUEUE", q)
    monkeypatch.setattr(M, "run_turn", _fake_turn())
    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=1000))

    r = client.post("/v1/review", json={"thread_id": "audit-1", "action": "revise",
                                        "feedback": "请补上来源"})
    assert r.status_code == 200

    rec = q.record("audit-1")
    assert rec["status"] == "revised"
    # 操作者记的是**用户名**（不是租户）：共享密钥档删除后不可能再有"没有用户概念"
    # 的身份，于是"是谁批的"永远答得出来。
    assert rec["resolved_by"] == TEST_USER, "审核动作没记操作者"
    # ⚠️ 这一列现在是**固定值**（只剩会话令牌一种凭证），断言保留是为了钉住"写库没被
    # 顺手删掉" —— 它仍是"这条记录出自哪个认证体系"的历史留痕列。
    assert rec["resolved_via"] == "session", "留痕列没写"
    assert rec["resolution_note"] == "请补上来源", "人工反馈没落进审计记录"
    assert rec["resolved_at"], "审核动作没记处理时刻"


# ---------------- 租户 ----------------
def test_retrieval_tenant_prefers_own_then_public(monkeypatch):
    """有租户用自己的；没租户落到**公共租户**（而不是"不加过滤"）。"""
    monkeypatch.setattr(M.config, "DEFAULT_TENANT", "public")
    assert M._retrieval_tenant(M.Identity(tenant="acme")) == "acme"
    assert M._retrieval_tenant(M.Identity(tenant="")) == "public"


def test_retrieval_tenant_is_fail_closed_without_public_tenant(monkeypatch):
    """公共租户没配 ⇒ 无租户的账号**什么都查不到**，绝不能退化成空串。

    空串在检索层的语义是"不加过滤"（能召回全部租户的内容），所以这里必须是一个
    **不可能与真实租户相等**的哨兵值。把返回值改回 `""` 时本条变红。
    """
    monkeypatch.setattr(M.config, "DEFAULT_TENANT", "")
    got = M._retrieval_tenant(M.Identity(tenant=""))
    assert got == M.config.NO_TENANT_SENTINEL
    assert got != "", "空串 = 不加过滤 ⇒ 无租户账号能看到所有租户的文档"


# ---------------- 审核队列端点 ----------------
def test_list_reviews_returns_pending(monkeypatch):
    from app.review_queue import ReviewQueue

    q = ReviewQueue(dsn="")
    q.add("pending-1", draft="草稿", score=30, feedback="改", tenant="")
    monkeypatch.setattr(M, "_REVIEW_QUEUE", q)

    body = client.get("/v1/reviews").json()
    assert body["count"] == 1
    assert body["items"][0]["thread_id"] == "pending-1"


# ---------------- thread_id 非空 ----------------
@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
def test_thread_id_must_not_be_blank(bad):
    """空 / 纯空白的 `thread_id` 必须在入口被拒（422）。

    检查点按 thread_id 落库、会话归属表也按它登记。空 id 让所有客户端**共用
    同一个空 key 的会话空间** —— 互相能读到对方的上下文；而归属校验对空 id
    是"直接短路放行"，所以这条隔离在最该生效的时候恰好不起作用。

    这是"**合法输入导致隔离失效**"：此前它只判长度上限，空串是合法值，
    于是没有任何一处会报错。三个空白变体都要覆盖 —— 只测 `""` 的话，
    一个"只判 `if not v` 但不去空白"的实现照样能过。
    """
    r = client.post("/v1/chat", json={"thread_id": bad, "message": "hi"})
    assert r.status_code == 422, f"thread_id={bad!r} 竟然被接受：{r.status_code}"
    assert "thread_id" in r.text


# ---------------- 未处理异常 ----------------
def test_unhandled_exception_returns_500_with_request_id():
    """未处理异常必须回**脱敏 500 且带 `X-Request-ID`**。

    此前全仓没有任何异常处理器，500 由最外层的 `ServerErrorMiddleware` 生成 ——
    它走的是最初的 `send`，不经过写 rid 的那层中间件。于是 401/403/409/429 都带
    rid，**唯独最需要 rid 的 500 不带**：用户报"500 了"，响应头和响应体里都没有
    任何可检索的标识，运维无从定位。

    ⚠️ 断言落在**响应头**上：只断言"body 里有 rid"是不够的 —— body 里的 rid 是
    `_client_error` 拼进去的，与"响应头可检索"是两件事，而网关与日志系统按的是头。
    """
    class _Boom:
        def pending(self, **_kw):
            raise RuntimeError("连不上 db.internal:5432（/srv/secret/path）")

    original = M._REVIEW_QUEUE
    M._REVIEW_QUEUE = _Boom()
    try:
        c = TestClient(M.app, headers=auth_headers(), raise_server_exceptions=False)
        r = c.get("/v1/reviews")
    finally:
        M._REVIEW_QUEUE = original

    assert r.status_code == 500
    assert r.headers.get("x-request-id"), (
        "500 没有 X-Request-ID —— 这条响应头是运维按 rid 定位的唯一入口"
    )
    if M.config.IS_PRODUCTION:
        assert "db.internal" not in r.text and "secret" not in r.text, (
            f"生产环境的 500 把内部细节回给客户端了：{r.text}"
        )
        assert "request_id=" in r.text, "生产文案必须带上 rid 供用户报障"


# ---------------- SSE 事件：预算事件与既有事件并存 ----------------
def test_budget_exceeded_event_emitted_when_limit_hit(monkeypatch):
    """单请求预算耗尽 → 下发 budget_exceeded 并停止继续生成。"""
    from app.quota import RequestBudget

    monkeypatch.setattr(M, "run_turn", _fake_turn(answer="很长的回答" * 20))
    events = _collect(M._stream_run(
        M.TurnRequest(prompt="hi", thread_id="budget-1"),
        "chat", budget=RequestBudget(1)))
    names = [e["event"] for e in events]
    # 超预算 → 下发 budget_exceeded（此时不再产出 token），随后照常收尾 done，
    # 让 SSE 客户端能正常结束流（而不是悬着等不到结束标记）
    assert names[0] == "budget_exceeded"
    assert "token" not in names
    assert names[-1] == "done"


def _collect(gen):
    import asyncio

    async def run():
        return [ev async for ev in gen]

    return asyncio.run(run())


# ---------------------------------------------------------------------------
# SSE **线格式**：结构化事件的 data 必须是 JSON，不能是 Python repr
# ---------------------------------------------------------------------------
# 这是 2026-09-28 实测发现的真实接口缺陷，此前完全没被测到：
#
#   服务端 yield {"event": "citations", "data": cites}，而 sse_starlette 的
#   `ServerSentEvent.encode` 对 data 用的是 `str(self.data)`（不是 json.dumps），
#   于是线上字节是 **Python repr**：
#
#       data: [{'n': 1, 'source': 'intro.txt', 'score': 0.62}]
#
#   单引号 + True/None —— 只有 Python 的 ast.literal_eval 读得懂，JS/Go/Java
#   客户端一律失败。而前端按 JSON.parse 处理是标准做法，静默失败的表现是
#   "引用区永远空白"，服务端日志毫无异常（契约错了，但不报错）。
#
# 为什么必须**走 HTTP 层**断言：`_stream_run` 产出的是 dict，序列化发生在
# sse_starlette 里。旧测试只断言 dict（进程内、序列化之前），所以这种 bug
# 无论怎么改都测不出来 —— 这正是"假绿"的典型形态。
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


class _CitedTurn:
    """替身运行：产出一段正文 + 一组**带真实类型**的引用。"""

    TOKEN = '梧州位于广西东部，特产有"六堡茶"。'
    CITES = [{"n": 1, "source": "intro.txt", "score": 0.6213,
              "chunk_id": "abcd1234", "page": 3, "pages": [3, 4],
              "section": "概览", "snippet": "梧州市……\n特产：六堡茶",
              "truncated": False, "table_md": None}]

    async def __call__(self, req, *, context, on_text=None, trace_metadata=None):
        from src.agents.runner import TaskOutcome

        if on_text is not None:
            await on_text(self.TOKEN, "call-1")
        return TaskOutcome(
            answer=self.TOKEN, attempts=1, final_agent="frontline",
            passed=True, exit_reason="passed", citations=self.CITES,
        )


def test_citations_payload_is_json_on_the_wire(monkeypatch):
    """citations 的 data 必须能被 `json.loads` 解析（repr 实现下会红）。"""
    import json

    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=1000))
    monkeypatch.setattr(M, "run_turn", _CitedTurn())

    r = client.post("/v1/chat", json={"thread_id": "wire-1", "message": "hi"})
    assert r.status_code == 200
    frames = dict(_parse_sse(r.text))

    assert "citations" in frames, f"没收到 citations 事件：{frames.keys()}"
    # 核心断言：JSON 可解析 + 内容一致
    parsed = json.loads(frames["citations"])
    assert isinstance(parsed, list) and len(parsed) == 1
    assert parsed[0]["chunk_id"] == "abcd1234"
    assert parsed[0]["pages"] == [3, 4]
    assert parsed[0]["snippet"] == "梧州市……\n特产：六堡茶"   # 含真实换行，验证多行 data 拼接
    assert parsed[0]["table_md"] is None                       # repr 下这里是 None 而非 null


def test_token_payload_stays_raw_text(monkeypatch):
    """token 的 data 必须是**裸正文**，不能被 JSON 化。

    否则客户端拿到的会是 `"……引用\\"……"`，得再剥一层引号才能用；
    正文里的引号还会被转义，展示出来就是错的。
    """
    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=1000))
    monkeypatch.setattr(M, "run_turn", _CitedTurn())

    r = client.post("/v1/chat", json={"thread_id": "wire-2", "message": "hi"})
    frames = dict(_parse_sse(r.text))

    assert frames.get("token") == _CitedTurn.TOKEN, \
        f"token 被加工过了：{frames.get('token')!r}"


def test_need_review_payload_is_json_on_the_wire(monkeypatch):
    """need_review 同样是结构化载荷，必须走 JSON。"""
    import json

    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=1000))
    monkeypatch.setattr(M, "run_turn",
                        _fake_turn(answer="草稿内容", review_reason="manual"))

    r = client.post("/v1/chat", json={"thread_id": "wire-3", "message": "hi"})
    frames = dict(_parse_sse(r.text))

    assert "need_review" in frames, f"没收到 need_review：{frames.keys()}"
    parsed = json.loads(frames["need_review"])
    # ⚠️ 这两个字段**刻意**是 None / 空串：agent 链路不产 0-100 分，
    # 评审意见也不再作为"交给审核人的建议"落库。给 0 会让审核台显示"AI 判了 0 分"，
    # 而真相是"没有人评过"。
    assert parsed["score"] is None
    assert parsed["feedback"] == ""
    assert parsed["draft"] == "草稿内容"


def test_budget_exceeded_payload_is_json_on_the_wire(monkeypatch):
    """budget_exceeded 也是结构化载荷（数字类型要能跨语言识别）。

    HTTP 路径的预算是 `RequestBudget(_QUOTA.request_limit)`，所以要靠配额管理器
    把单请求预算压到 1 —— 直接改 config.LLM_MAX_TOKENS_PER_REQUEST 是无效的
    （端点不读它，读的是配额的 request_limit）。
    """
    import json

    from app.quota import QuotaManager

    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=1000))
    monkeypatch.setattr(M, "_QUOTA", QuotaManager(daily_limit=0, request_limit=1))
    monkeypatch.setattr(M, "run_turn", _fake_turn(answer="很长的回答" * 20))

    r = client.post("/v1/chat", json={"thread_id": "wire-4", "message": "hi"})
    frames = dict(_parse_sse(r.text))
    assert "budget_exceeded" in frames, f"事件列表：{list(frames)}"
    parsed = json.loads(frames["budget_exceeded"])
    assert parsed["limit"] == 1
    assert isinstance(parsed["used"], int)

