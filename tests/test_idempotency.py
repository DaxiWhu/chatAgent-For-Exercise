"""幂等键（IETF `Idempotency-Key` 草案）—— 行为守卫。

为什么这组断言值得写
-------------------
这套机制的失效方式是**静默**的，而且两种失效方向都会造成真实损失：

  - **少去重**：客户端重试 → 真的又跑一整轮 → token 花两次、同一会话追加两条
    一样的用户消息、审核队列里出现两条同样内容的待审项。服务端**一个错误都没有**，
    所有指标看起来正常。
  - **多去重**：把"用户真心又问了同一个问题"误判成重试 → 用户拿不到新答案。
    这比重复计费更糟（那是功能损坏）。

所以断言必须钉**行为**（第几次请求发生了什么、图跑了几次、响应体是否逐字节一致、
状态码是什么），而不是"源码里有没有那个分支"——后者在重构后照样绿。

⚠️ 全部离线：假图、不连 PG/Redis、不调模型。`tests/conftest.py` 已经清掉了开发机
`.env` 的影响（否则本机的 `SERVE_API_KEY` 会让这里全变 401）。
"""
from __future__ import annotations

import os

import pytest

os.environ["DB_URL"] = ""

from app import idempotency as idem  # noqa: E402
from app.idempotency import (  # noqa: E402
    DONE,
    IN_PROGRESS,
    MISMATCH,
    NEW,
    IdempotencyStore,
    InvalidKey,
    fingerprint,
    normalize_key,
)

pytest.importorskip("fastapi.testclient", reason="TestClient 需要 httpx")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as M  # noqa: E402
from app import config  # noqa: E402
from src.prompts import PROMPT_VERSION  # noqa: E402
from tests.auth_util import TEST_USER, auth_headers  # noqa: E402

client = TestClient(M.app, headers=auth_headers())

#: 默认租户。身份里没有租户时（匿名/脚本通行证未自报）`DEFAULT_TENANT` 为空 ⇒
#: 端点里的 tenant 是空串。
#: 直接抢占键的用例必须用它，否则会抢到另一个命名空间里（见 `_key()`）。
TENANT = ""
#: 端点侧抢键用的**主体**。命名空间是 (tenant, subject, key)，三个都要对上，
#: 否则"直接抢占"抢到的不是端点要用的那把键（见 `IdempotencyStore._key`）。
SUBJECT = TEST_USER


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch):
    """每个用例一张干净的键表。

    不能共用模块级那个单例：用例之间只要出现"同租户 + 同键 + 同载荷"，后一个用例
    就会拿到**前一个用例留下的重放**，表现为"图没跑、断言莫名其妙失败"。
    """
    monkeypatch.setattr(M, "_IDEMPOTENCY",
                        IdempotencyStore(ttl=600, max_entries=1000, dsn=""))
    return M._IDEMPOTENCY


def _store(**kw) -> IdempotencyStore:
    kw.setdefault("ttl", 600)
    kw.setdefault("max_entries", 100)
    kw.setdefault("dsn", "")
    return IdempotencyStore(**kw)


def _fp(op: str, body) -> str:
    """端点侧算指纹的**同一口径**（`_idempotency_gate` 里就是这么调的）。"""
    return fingerprint({"op": op, "body": body})


def _chat_body(thread_id: str, message: str = "你好") -> dict:
    return {"thread_id": thread_id, "message": message}


# ---------------------------------------------------------------------------
# 1. 存储层：四种状态（标准 2.6 / 2.7 的判定表）
# ---------------------------------------------------------------------------
def test_the_first_request_with_a_key_is_a_new_request():
    s = _store()
    r = s.begin(TENANT, "k1", "fp")
    assert r.status == NEW
    assert r.payload is None


def test_a_retry_while_in_flight_is_a_conflict():
    s = _store()
    assert s.begin(TENANT, "k1", "fp").status == NEW
    r = s.begin(TENANT, "k1", "fp")
    assert r.status == IN_PROGRESS
    assert r.payload is None, "在途的那次没有结果可给，不能瞎编一个返回回去"


def test_a_retry_after_completion_replays_the_previous_result():
    """标准 2.6 的核心那一条：已完成 ⇒ 返回上一次的结果。"""
    s = _store()
    s.begin(TENANT, "k1", "fp")
    assert s.finish(TENANT, "k1", "fp", '{"frames":[["done"]]}') is True
    r = s.begin(TENANT, "k1", "fp")
    assert r.status == DONE
    assert r.payload == '{"frames":[["done"]]}'


def test_the_same_key_with_a_different_payload_is_a_mismatch():
    """标准 2.2 的 MUST NOT：键不能跨不同载荷复用（在途与已完成两种状态都要覆盖）。

    只测一种会漏掉另一半：在途靠的是"抢占时写下的指纹"，已完成靠的是"finish 时
    重写的指纹"，两条路径各自可能写丢 `fp`。
    """
    s = _store()
    s.begin(TENANT, "k1", "fp-A")
    assert s.begin(TENANT, "k1", "fp-B").status == MISMATCH, "在途状态下没比指纹"
    s.finish(TENANT, "k1", "fp-A", "{}")
    assert s.begin(TENANT, "k1", "fp-B").status == MISMATCH, "完成之后没比指纹"
    assert s.begin(TENANT, "k1", "fp-A").status == DONE


def test_a_failed_request_releases_the_key(monkeypatch):
    """失败/未完成必须撤销键，否则一次偶发失败会把这个键锁死一整个 TTL。"""
    s = _store()
    s.begin(TENANT, "k1", "fp")
    s.forget(TENANT, "k1")
    assert s.begin(TENANT, "k1", "fp").status == NEW


def test_keys_are_isolated_per_tenant():
    """两个租户用同一个键名不能互相干扰（否则一个租户能挡住另一个租户的请求）。"""
    s = _store()
    assert s.begin("tenant-a", "same", "fp").status == NEW
    assert s.begin("tenant-b", "same", "fp").status == NEW, "另一个租户被误判成重复"
    assert s.begin("tenant-a", "same", "fp").status == IN_PROGRESS


def test_keys_are_isolated_per_subject():
    """同一个租户里的两个**用户**用同一个键名不能互相干扰（2026-10-06 修）。

    幂等键是客户端自己生成的一串字符，服务端不做归属校验。只按租户分命名空间时，
    同租户的 B 拿到 A 用过的键名就能对 A 造成两类后果，**都不需要越权凭证**：
      · A 还在跑，B 抢先登记 ⇒ A 收到 409"正在处理中"（被邻居按住）；
      · A 已完成，B 同键**同载荷**重试 ⇒ 命中 `DONE` ⇒ B 拿到 A 那次的回答正文
        （`Idempotency-Replayed: true`，跨用户的响应体泄露）。
    后者的门槛并不高：载荷里的 thread_id 是客户端自选的，message 常是同一句提问。

    这也正是"隔离边界已收紧到 (tenant, user)"的补齐 —— 配额分桶与会话归属早就
    是两级口径，幂等键是当时漏掉的一处。
    """
    s = _store()
    assert s.begin(TENANT, "same", "fp", subject="alice").status == NEW
    assert s.begin(TENANT, "same", "fp", subject="bob").status == NEW, \
        "同租户的另一个用户被误判成重复"
    assert s.begin(TENANT, "same", "fp", subject="alice").status == IN_PROGRESS


def test_a_subject_less_key_does_not_collide_with_a_scoped_one():
    """主体为空时**退回历史键形态**（只带租户），而不是补一个空段。

    两种形态必须落在**不同**命名空间：否则"没有主体"的调用方会与"有主体"的调用方
    共用一把键 —— 那等于把刚收窄的隔离又还回去，而且新老部署混跑期间会互相干扰。
    """
    s = _store()
    assert s.begin(TENANT, "same", "fp").status == NEW
    assert s.begin(TENANT, "same", "fp", subject="alice").status == NEW, \
        "无主体的键与带主体的键撞在了一起"


def test_finish_and_forget_must_use_the_same_subject_as_begin():
    """收尾漏传 / 传错 subject 时**不能生效**。

    这不是在测一个显然的行为，而是钉住一个**静默**失效：`_stream_run` 的 finally
    （finish/forget）与端点抢键处各写一遍 subject，两处若不一致，键会停在"在途"
    直到 TTL —— 客户端拿同一个键重试永远拿到 409"正在处理中"，而没有任何日志。
    """
    s = _store()
    s.begin(TENANT, "k1", "fp", subject="alice")

    assert s.finish(TENANT, "k1", "fp", "{}", subject="bob") is False, \
        "finish 在另一个命名空间里却报成功"
    assert s.begin(TENANT, "k1", "fp", subject="alice").status == IN_PROGRESS, \
        "收尾落到了别的命名空间（完成了但登记不上）"

    s.forget(TENANT, "k1", subject="bob")
    assert s.begin(TENANT, "k1", "fp", subject="alice").status == IN_PROGRESS, \
        "撤销落到了别的命名空间（没撤掉）"

    s.forget(TENANT, "k1", subject="alice")
    assert s.begin(TENANT, "k1", "fp", subject="alice").status == NEW


def test_an_expired_key_is_treated_as_first_time(monkeypatch):
    """过期后重试**不再被拦** —— 这是公布的过期策略（标准 2.3）的必然含义。

    断言"变成首次"而不只是"没报错"：过期的正确后果是"保护消失、请求照常执行"，
    若实现把过期条目读成 IN_PROGRESS，客户端会被**永久**挡住。
    """
    clock = {"now": 1000.0}
    monkeypatch.setattr(idem.time, "time", lambda: clock["now"])
    s = _store(ttl=60)
    assert s.begin(TENANT, "k1", "fp").status == NEW
    clock["now"] += 61
    assert s.begin(TENANT, "k1", "fp").status == NEW


def test_the_key_table_never_grows_past_its_limit():
    """溢出后**不再登记新键**，且"完成"这条路径也不能绕过上限。

    第二半是容易漏的：抢占被拒（表满）之后若 `finish` 照旧新建条目，表会在每次
    溢出请求后 +1 —— 上限形同虚设，而"慢慢涨"在任何单次运行里都看不出来。
    """
    s = _store(max_entries=100)
    for i in range(100):
        assert s.begin(TENANT, f"k{i}", "fp").status == NEW
    assert s.count() == 100

    assert s.begin(TENANT, "overflow", "fp").status == NEW, "溢出时不该阻断服务"
    assert s.count() == 100, "溢出后仍在登记新键"

    s.finish(TENANT, "overflow", "fp", '{"x":1}')
    assert s.count() == 100, "finish 绕过上限把表撑大了"


# ---------------------------------------------------------------------------
# 2. 存储层：正文缓存的上限（内存放大面）
# ---------------------------------------------------------------------------
def test_a_body_over_the_cap_is_not_stored_but_the_key_still_completes():
    """超上限时：键**仍然**记成已完成，只是没有可重放的正文。

    这两件事必须分开：把键也一起丢掉的话，客户端重试会**再跑一整轮**（重复计费），
    而它只是回答比较长而已。正确行为是"回 409 指路"，不是"重新执行"。
    """
    s = _store(max_body_bytes=1024)
    s.begin(TENANT, "k1", "fp")
    assert s.finish(TENANT, "k1", "fp", "x" * 2000) is False
    r = s.begin(TENANT, "k1", "fp")
    assert r.status == DONE
    assert r.payload is None


def test_a_body_exactly_at_the_cap_is_still_stored():
    """边界是"超过才丢"，不是"达到就丢"。差一个字节的判据会让长回答白白失去重放。"""
    s = _store(max_body_bytes=1024)
    s.begin(TENANT, "k1", "fp")
    assert s.finish(TENANT, "k1", "fp", "x" * 1024) is True
    assert s.begin(TENANT, "k1", "fp").payload == "x" * 1024


def test_a_disabled_store_is_a_complete_noop():
    """关掉之后行为必须与"没有这套机制"完全一致（本项目默认值原则②）。"""
    s = _store(enabled=False)
    assert s.begin(TENANT, "k1", "fp").status == NEW
    assert s.begin(TENANT, "k1", "fp").status == NEW, "关掉的实现仍在去重"
    assert s.finish(TENANT, "k1", "fp", "{}") is False
    assert s.count() == 0


# ---------------------------------------------------------------------------
# 3. 键格式与请求指纹（标准 2.4 / §5）
# ---------------------------------------------------------------------------
def test_the_key_accepts_both_plain_and_rfc8941_quoted_forms():
    """草案 2.1 规定该头是 Structured Header（String 必须带引号），
    但现实里绝大多数客户端不带引号发 —— 两种都要收。"""
    assert normalize_key("8e03978e-40d5-43e8-bc93-6894a57f9324") == \
        "8e03978e-40d5-43e8-bc93-6894a57f9324"
    assert normalize_key('"8e03978e-40d5-43e8-bc93-6894a57f9324"') == \
        "8e03978e-40d5-43e8-bc93-6894a57f9324"
    assert normalize_key("   spaced-key   ") == "spaced-key"
    assert normalize_key("") == "", "没带键要能与'键非法'分开"


@pytest.mark.parametrize("bad", [
    "has space",
    'has"quote',
    "has\\backslash",
    "has\x1fseparator",       # 服务端用 \x1f 分隔租户 ⇒ 必须挡住（标准 §5 的注入面）
    "has\nnewline",           # 进日志 ⇒ 防日志注入
    "中文键",
    "x" * 200,                # 超过默认 128
])
def test_a_key_outside_the_published_format_is_rejected(bad):
    """非法键要**报错**而不是"悄悄当成没带"—— 后者会让一个拼错的键静默失去保护。"""
    with pytest.raises(InvalidKey):
        normalize_key(bad)


def test_the_fingerprint_ignores_field_order_but_not_values():
    """顺序无关是**必需**的：不同语言的 JSON 序列化顺序不同，顺序一变指纹就变
    ⇒ 客户端换个 SDK 就会莫名收到 422。"""
    assert fingerprint({"op": "chat", "body": {"a": 1, "b": 2}}) == \
        fingerprint({"body": {"b": 2, "a": 1}, "op": "chat"})
    assert fingerprint({"op": "chat", "body": {"a": 1}}) != \
        fingerprint({"op": "chat", "body": {"a": 2}})
    assert fingerprint({"op": "chat", "body": {}}) != \
        fingerprint({"op": "review", "body": {}}), "不同操作必须算不同载荷"


# ---------------------------------------------------------------------------
# 4. 端点层：替身运行（不碰模型 / 不碰数据库）
# ---------------------------------------------------------------------------
class _FakeTurn:
    """替身 `src.turn.run_turn`：数"这一轮真的跑了几次"，按需抛错。

    为什么还要数次数：幂等的价值全在"重试时**没有**再跑一次"上。只看状态码等于
    什么都没验 —— 重新跑一遍同样返回 200，而账单上是两倍。
    """

    def __init__(self, chunks=("甲", "乙"), fail_first=False):
        self.chunks = list(chunks)
        self.fail_first = fail_first
        self.calls = 0

    async def __call__(self, req, *, context, on_text=None, trace_metadata=None):
        from src.agents.runner import TaskOutcome

        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise RuntimeError("upstream 500")
        for chunk in self.chunks:
            if on_text is not None:
                await on_text(chunk, "call-1")
        return TaskOutcome(answer="".join(self.chunks), attempts=1,
                           final_agent="frontline_agent", passed=True,
                           exit_reason="passed")


@pytest.fixture
def fake_turn(monkeypatch):
    g = _FakeTurn()
    monkeypatch.setattr(M, "run_turn", g)
    return g


# ---------------------------------------------------------------------------
# 5. 端点层：四种状态
# ---------------------------------------------------------------------------
def test_a_retried_chat_is_replayed_without_running_the_graph_again(fake_turn):
    """**这一条是整套机制的价值所在。**
    "图跑了几次"必须是 1 —— 只看状态码等于什么都没验：重新执行一遍同样返回 200，
    而账单上是两倍。响应体逐字节相等也是必需的（客户端会把它当成一次正常回答）。
    """
    body = _chat_body("t-replay")
    h = {"Idempotency-Key": "k-replay"}

    r1 = client.post("/v1/chat", json=body, headers=h)
    assert r1.status_code == 200
    assert fake_turn.calls == 1

    r2 = client.post("/v1/chat", json=body, headers=h)
    assert r2.status_code == 200
    assert fake_turn.calls == 1, "重试又跑了一遍图（= 重复计费）"
    assert r2.text == r1.text, "重放出来的流与首次不一致"
    assert r2.headers.get("idempotency-replayed") == "true"
    assert r1.headers.get("idempotency-replayed") is None, \
        "首次请求不该被标成重放"


def test_the_replayed_response_keeps_the_original_prompt_version(fake_turn):
    """重放的是**当初那次**的产出，所以版本号必须是它的，不是"现在这份代码的"。"""
    body = _chat_body("t-pv")
    h = {"Idempotency-Key": "k-pv"}
    client.post("/v1/chat", json=body, headers=h)
    r2 = client.post("/v1/chat", json=body, headers=h)
    assert r2.headers.get("x-prompt-version") == PROMPT_VERSION


def test_a_chat_retried_while_in_flight_is_rejected_with_409(fake_turn):
    """在途 ⇒ 409（标准 2.6）。直接抢占键来构造"正在跑"，比让一个真请求挂住可靠。"""
    body = _chat_body("t-inflight")
    M._IDEMPOTENCY.begin(TENANT, "k-inflight", _fp("chat", body), subject=SUBJECT)

    r = client.post("/v1/chat", json=body, headers={"Idempotency-Key": "k-inflight"})
    assert r.status_code == 409
    assert fake_turn.calls == 0, "被判成在途重复的请求仍然进了图"
    assert r.headers["content-type"].startswith("application/problem+json")


def test_the_same_key_with_a_different_message_is_rejected_with_422(fake_turn):
    """同键换载荷 ⇒ 422（标准 2.7）。**不能**回 409：那会让客户端以为"等会儿再试"，
    而它其实需要换一个键。"""
    client.post("/v1/chat", json=_chat_body("t-422", "第一个问题"),
                headers={"Idempotency-Key": "k-422"})
    r = client.post("/v1/chat", json=_chat_body("t-422", "完全不同的问题"),
                    headers={"Idempotency-Key": "k-422"})
    assert r.status_code == 422
    assert fake_turn.calls == 1, "换了载荷的请求不该被执行"


def test_the_same_message_with_a_new_key_is_a_new_request(fake_turn):
    """键的语义是「这一次请求」，不是「这个问题」。

    用户真心想再问一遍时，客户端换个新键就该真的再跑一次 —— 服务端**绝不能**
    按消息内容去重，那是把正常行为误判成重试（功能损坏）。
    """
    body = _chat_body("t-newkey", "同一个问题")
    client.post("/v1/chat", json=body, headers={"Idempotency-Key": "k-a"})
    client.post("/v1/chat", json=body, headers={"Idempotency-Key": "k-b"})
    assert fake_turn.calls == 2, "服务端按内容去重了（用户再也问不到同一个问题）"


def test_one_users_key_does_not_block_another_user_of_the_same_tenant(fake_turn):
    """**接线守卫**：端点是真把 `ident.user` 传进闸门了吗（2026-10-06 修）。

    存储层按 (tenant, subject) 分命名空间**不等于**这条隔离已经生效 ——
    端点里少传一个 `ident.user`，键就退回"整个租户共用一把"，而**所有既有用例
    照样全绿**（它们只有一个用户）。所以这一条必须走 HTTP 层。

    这里覆盖的是那两类后果里"先到达"的一种：B 的载荷与 A 不同 ⇒ 旧口径下
    B 收到 422「键已被用于不同的请求载荷」。B 什么都没做错，却必须换一个键
    才能继续 —— 而"换哪个键"只有他自己知道，服务端也永远不会告诉他为什么。
    """
    a = auth_headers(user="iso-alice", tenant="t-iso")
    b = auth_headers(user="iso-bob", tenant="t-iso")

    r1 = client.post("/v1/chat", json=_chat_body("t-iso-a"),
                     headers={**a, "Idempotency-Key": "k-iso"})
    assert r1.status_code == 200

    r2 = client.post("/v1/chat", json=_chat_body("t-iso-b"),
                     headers={**b, "Idempotency-Key": "k-iso"})
    assert r2.status_code == 200, \
        f"同租户的另一个用户被自己邻居用过的键挡住了（{r2.status_code}：{r2.text[:120]}）"
    assert r2.headers.get("idempotency-replayed") is None, \
        "B 拿到的是 A 那次结果的**重放**（跨用户的响应体泄露）"
    assert fake_turn.calls == 2, "两次请求各自该真跑一遍"


# ---------------------------------------------------------------------------
# 6. 端点层：不带键 / 缺键 / 非法键
# ---------------------------------------------------------------------------
def test_a_request_without_the_key_is_completely_untouched(fake_turn):
    """**默认行为不变的守卫。** 不带这个头时不去重、不报错、不拦截。"""
    body = _chat_body("t-nokey")
    r1 = client.post("/v1/chat", json=body)
    r2 = client.post("/v1/chat", json=body)
    assert (r1.status_code, r2.status_code) == (200, 200)
    assert fake_turn.calls == 2, "不带键的请求被去重了（默认行为被改了）"


def test_a_missing_key_is_rejected_with_400_only_when_the_service_requires_it(
        fake_turn, monkeypatch):
    """标准 2.7 的第一条只在"该操作**要求**这个头"时适用（2.5.1 说带不带是客户端的自由）。

    两个方向都要断言：默认放行（否则所有既有客户端当场 400），开了才拦。
    """
    body = _chat_body("t-required")

    r = client.post("/v1/chat", json=body)
    assert r.status_code == 200, "默认配置下缺键不该被拒"

    monkeypatch.setattr(config, "IDEMPOTENCY_REQUIRE_KEY", True)
    r = client.post("/v1/chat", json=_chat_body("t-required-2"))
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/problem+json")


def test_an_invalid_key_is_rejected_with_400(fake_turn):
    r = client.post("/v1/chat", json=_chat_body("t-badkey"),
                    headers={"Idempotency-Key": "bad key with spaces"})
    assert r.status_code == 400
    assert fake_turn.calls == 0


def test_the_400_and_422_bodies_carry_the_documented_link(fake_turn, monkeypatch):
    """标准 2.7 要求响应体带一个**指向文档的链接**（用户不知道键该怎么写时，
    唯一能自救的信息就是它）。"""
    monkeypatch.setattr(config, "IDEMPOTENCY_DOC_URL",
                        "https://docs.example/idempotency")
    r = client.post("/v1/chat", json=_chat_body("t-link"),
                    headers={"Idempotency-Key": "bad key"})
    assert r.status_code == 400
    assert r.json()["type"] == "https://docs.example/idempotency"
    assert r.json()["title"], "problem 体里没有 title"
    assert 'rel="describedby"' in r.headers["link"]
    assert "https://docs.example/idempotency" in r.headers["link"]


# ---------------------------------------------------------------------------
# 7. 端点层：失败与配额（"只有成功的请求才占用键"）
# ---------------------------------------------------------------------------
def test_a_failed_run_releases_the_key_so_the_same_key_can_be_retried(monkeypatch):
    """上游偶发失败之后，客户端拿**同一个键**重试必须能真的重跑。

    不释放的后果是：这次失败把那个键锁死一整个 TTL（客户端逻辑全对，却始终 409）。
    所以断言的不只是"第二次是 200"，还有"图真的又跑了一次"。
    """
    g = _FakeTurn(("甲",), fail_first=True)
    monkeypatch.setattr(M, "run_turn", g)
    body = _chat_body("t-fail")
    h = {"Idempotency-Key": "k-fail"}

    r1 = client.post("/v1/chat", json=body, headers=h)
    assert "event: error" in r1.text

    r2 = client.post("/v1/chat", json=body, headers=h)
    assert r2.status_code == 200
    assert "event: error" not in r2.text
    assert g.calls == 2, "失败后键没被释放，重试被 409 挡住了"


def test_a_quota_rejection_releases_the_claimed_key(fake_turn, monkeypatch):
    """配额 429 发生在"抢占键之后、生成器创建之前"—— 必须显式退还。

    不退还的后果很隐蔽：客户端等额度恢复后拿同一个键重试，收到 409"正在处理中"，
    而那个"正在处理"的请求**从来没有存在过**。
    """
    def _deny(_tenant, _subject=""):
        # ⚠️ 形参要与真实签名同步：`_quota_gate(tenant, subject="")` 是**位置+关键字**
        # 调用（`_reserve_quota` 里把 subject 一起传下去）。桩少写一个形参时，
        # 报错是 `TypeError: ... takes 1 positional argument but 2 were given` ——
        # 与本用例要验的"配额拒绝要退还幂等键"毫无关系，极易被当成实现坏了。
        raise HTTPException(status_code=429, detail="quota exceeded")

    monkeypatch.setattr(M, "_quota_gate", _deny)
    body = _chat_body("t-quota")

    r = client.post("/v1/chat", json=body, headers={"Idempotency-Key": "k-quota"})
    assert r.status_code == 429
    assert M._IDEMPOTENCY.begin(TENANT, "k-quota", _fp("chat", body),
                                subject=SUBJECT).status == NEW, \
        "配额拒绝后键仍停在在途状态"


# ---------------------------------------------------------------------------
# 8. 端点层：另外两个端点也要受同一套语义保护
# ---------------------------------------------------------------------------
def test_the_review_endpoint_is_guarded_too():
    """`/v1/review` 同样是一次真实生成（接着跑被打断的运行），重试代价一样大。

    ⚠️ 这道题有个坑：`/v1/review` 自己也有前置校验（这条会话当前不在审核态 ⇒
    409），**两条 409 撞在同一个状态码上**。只看状态码的断言无法区分"被幂等闸门
    挡住了"与"根本没进到那一步"，而后者正是**假绿**的经典形态。
    判据因此只能取响应**媒体类型**：幂等闸门回 `application/problem+json`，
    业务校验回 `application/json` + `{"detail": ...}`。
    （不能改用"body 里有没有 `detail`"来分辨 —— problem+json 的 body 自身
    也带 `detail` 字段，那个判据恒为真，等于没写。）
    """
    body = {"thread_id": "t-review", "action": "accept", "feedback": ""}
    M._IDEMPOTENCY.begin(TENANT, "k-review", _fp("review", body), subject=SUBJECT)
    r = client.post("/v1/review", json=body, headers={"Idempotency-Key": "k-review"})
    assert r.status_code == 409
    # ⚠️ 只能靠媒体类型分辨：problem+json 的 body 自身也带 `detail` 字段，
    # 而业务校验回的是 `application/json` + `{"detail": ...}`。
    assert r.headers["content-type"].startswith("application/problem+json"), \
        f"拿到的 409 不是幂等闸门给的（看 body：{r.text[:120]}）"


def test_the_resume_endpoint_is_guarded_too():
    """`/v1/chat/resume` 现在**只会**回 409（没有可续跑的现场，见端点里的长注释），
    所以这条更要点明：那个 409 必须是**幂等闸门**给的，而不是业务校验给的。

    否则"幂等闸门接线掉了一个端点"会被业务 409 完全掩盖 —— 客户端拿同一个键
    重试，永远收到"没有可续跑的运行现场"，而它上一次的结果其实早就缓存好了。
    """
    body = {"thread_id": "t-resume"}
    M._IDEMPOTENCY.begin(TENANT, "k-resume", _fp("resume", body), subject=SUBJECT)
    r = client.post("/v1/chat/resume", json=body,
                    headers={"Idempotency-Key": "k-resume"})
    assert r.status_code == 409
    # ⚠️ 只能靠媒体类型分辨：problem+json 的 body 自身也带 `detail` 字段，
    # 而业务校验回的是 `application/json` + `{"detail": ...}`。
    assert r.headers["content-type"].startswith("application/problem+json"), \
        f"拿到的 409 不是幂等闸门给的（看 body：{r.text[:120]}）"


def test_the_legacy_chat_alias_inherits_the_same_semantics(fake_turn):
    """旧路径是别名，行为必须一致 —— 只在 `/v1/chat` 上接线的话，走旧路径的
    客户端会静默失去保护（而且没有任何迹象）。"""
    body = _chat_body("t-legacy")
    h = {"Idempotency-Key": "k-legacy"}
    client.post("/chat", json=body, headers=h)
    r2 = client.post("/chat", json=body, headers=h)
    assert r2.headers.get("idempotency-replayed") == "true"
    assert fake_turn.calls == 1
