"""压缩改成"日志只追加 + 读取时投影"后的守卫。

这套守卫回答三件事，任何一条破了都会表现成"看起来正常、实际丢了内容"：

1. **库不动**：压缩前后会话日志的条数与内容逐条相等。这是整次改造的立身之本，
   破了就等于退回旧路线（删掉的东西再也拿不回来）。
2. **投影确定**：同一份日志 + 同一条记录，两次算出的视图必须完全一致。不一致的
   后果不是"结果错"，而是上游前缀缓存命中不了 —— 每轮全量重算，变慢变贵且不报错。
3. **宁可全量，不给错量**：记录说覆盖 N 条、库里却少于 N 条时退回全量。这个分支
   平时不会走到，一旦走到就说明"只追加"的前提被破坏了，必须能从日志里看出来。
"""

from __future__ import annotations

import asyncio

import pytest

import src.agents.compaction as C
from src.agents.compaction_store import InMemoryCompactionStore


class FakeSession:
    """最小会话后端。``calls`` 用来断言"某个方法一次都没被调用"。"""

    def __init__(self, items=None):
        self.items = list(items or [])
        self.session_id = "s-1"
        self.session_settings = "SETTINGS"
        self.calls: list[str] = []

    async def get_items(self, limit=None):
        self.calls.append("get_items")
        return list(self.items)

    async def add_items(self, items):
        self.calls.append("add_items")
        self.items.extend(items)

    async def pop_item(self):
        self.calls.append("pop_item")
        return self.items.pop() if self.items else None

    async def clear_session(self):
        self.calls.append("clear")
        self.items = []


def _rounds(n: int, *, start: int = 0) -> list[dict]:
    out: list[dict] = []
    for i in range(start, start + n):
        out.append({"role": "user", "content": f"第{i}轮的问题，写得稍长一点好让 token 数不为零"})
        out.append({"role": "assistant", "content": f"第{i}轮的回答，同样写得稍长一点用来占位"})
    return out


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_COMPACT", "1")
    monkeypatch.setenv("AGENT_HISTORY_KEEP_ROUNDS", "2")
    C.reset_compaction_state()
    yield
    C.reset_compaction_state()


@pytest.fixture
def store():
    return InMemoryCompactionStore()


@pytest.fixture
def short_summary(monkeypatch):
    """摘要固定成一句很短的话 —— "必须比原文短"那条校验要能通过。"""
    monkeypatch.setattr(C, "summarize_items", lambda segment, **kw: "简短摘要。")
    return "简短摘要。"


def _compact(session, **kw):
    return asyncio.run(C.compact_session_history("s-1", session=session, **kw))


# --------------------------------------------------------------------------- #
# ① 库不动
# --------------------------------------------------------------------------- #
def test_compaction_never_touches_the_log(on, store, short_summary):
    """★ 压缩只写记录：会话日志的条数与内容逐条不变，且没有 clear / add_items。"""
    items = _rounds(4)
    fake = FakeSession(items)
    wrapped = C.CompactingSession(fake, store=store)

    assert _compact(wrapped) == 4

    assert fake.items == items, "会话日志被改动了 —— 这正是这次改造要消灭的事"
    assert wrapped._inner.calls.count("clear") == 0
    assert "add_items" not in fake.calls


def test_three_generations_still_keep_the_first_round(on, store, short_summary):
    """压三代之后，**第一轮的原文仍然在库里**（投影之外可按 covered 回读）。"""
    items = _rounds(3)
    fake = FakeSession(items)
    wrapped = C.CompactingSession(fake, store=store)

    assert _compact(wrapped) == 2
    fake.items.extend(_rounds(2, start=3))
    assert _compact(wrapped) > 0
    fake.items.extend(_rounds(2, start=5))
    assert _compact(wrapped) > 0

    assert fake.items[: len(items)] == items, "第一代之前的原文必须仍在库里"
    assert len(store.rows()) == 3, "三代压缩 = 三条记录（旧的两条标 superseded，不删）"
    assert len([r for r in store.rows() if r["status"] == "active"]) == 1


# --------------------------------------------------------------------------- #
# ② 投影：位置、幂等、口子
# --------------------------------------------------------------------------- #
def test_projection_is_byte_identical_across_two_reads(on, store, short_summary):
    """同一份日志 + 同一条记录，两次投影必须完全一致（前缀缓存靠这个）。"""
    fake = FakeSession(_rounds(4))
    wrapped = C.CompactingSession(fake, store=store)
    _compact(wrapped)

    async def _go():
        return await wrapped.get_items(), await wrapped.get_items()

    a, b = asyncio.run(_go())
    assert a == b
    assert a[0] == {"role": "system", "content": C._SUMMARY_PREAMBLE + "简短摘要。"}
    assert len(a) == 5, "摘要 + 保留的 2 轮（4 条）"


def test_the_summary_reaches_the_model_with_a_static_preamble(on, store, short_summary):
    """★ 摘要注入时必须带抬头，且抬头**写死**、且**不入库**。

    抬头回答的是"这段文本是谁说的、算不算本轮指令"。摘要正文里有「待办」、有逐字
    引用的用户原话，没有这句话时模型会把几轮以前的待办当成现在的要求去做 —— 症状是
    很听话地做了不该做的事，而日志全绿。

    三条一起验，少一条都会退化：
    * **两条注入路径一模一样**（历史压缩 / 窗口压缩）—— 不一致时同一段摘要在两个入口
      下的判读会变，而两边各自的用例都是绿的；
    * **不含任何数字** —— 带轮次/日期/条数就等于每轮都变，上游前缀缓存从这条消息起
      全部作废（实测：内容每轮变 ⇒ 命中 0%）；
    * **库里没有抬头** —— 存进去的话每代摘要都多一段自述，还会被下一代压缩当成正文
      再压一遍，摘要一代代变长。
    """
    fake = FakeSession(_rounds(4))
    wrapped = C.CompactingSession(fake, store=store)
    _compact(wrapped)

    head = asyncio.run(wrapped.get_items())[0]["content"]

    assert head.startswith(C._SUMMARY_PREAMBLE), "历史压缩这条路径没带抬头"
    assert "简短摘要。" in head, "抬头不能顶掉正文"
    assert C._session_replacement("正文", [])[0]["content"] == C._SUMMARY_PREAMBLE + "正文", \
        "窗口压缩这条路径的抬头必须逐字节一致"
    assert not any(ch.isdigit() for ch in C._SUMMARY_PREAMBLE), \
        "抬头里出现数字（轮次/日期/条数）⇒ 每轮都变 ⇒ 前缀缓存全废"
    assert all(C._SUMMARY_PREAMBLE not in (r.get("summary") or "") for r in store.rows()), \
        "抬头只该在注入时拼，不该写进库"


def test_projection_keeps_everything_before_the_first_record(on, store):
    """没压过 ⇒ **一次记录查询之外什么都不做**，历史原样返回。"""
    fake = FakeSession(_rounds(2))
    wrapped = C.CompactingSession(fake, store=store)

    view = asyncio.run(wrapped.get_items())
    assert view == fake.items


def test_projection_falls_back_when_the_record_is_unreadable(short_summary):
    """读不到记录 ⇒ 退回全量历史，**不是**退回空。

    表现差别的量级：全量是"多花 token"，空是"用户一觉醒来失忆"。所以这里必须是
    fail-open，而且日志里要留下痕迹。
    """
    class _BrokenStore:
        async def latest_active(self, thread_id, scope="history"):
            raise RuntimeError("记录表读不了")

        async def record(self, *a, **k):  # pragma: no cover - 不该被调用
            raise AssertionError("这条用例不该写记录")

    fake = FakeSession(_rounds(2))
    wrapped = C.CompactingSession(fake, store=_BrokenStore())

    assert asyncio.run(wrapped.get_items()) == fake.items


def test_projection_refuses_a_record_that_covers_more_than_the_log(monkeypatch):
    """记录说覆盖 100 条、库里只有 4 条 ⇒ 退回全量并计数。

    这个分支只在"承诺被破坏"时出现（有人按条删过历史）。宁可多给原文，也不给
    一份和库对不上的投影 —— 后者会让模型按不存在的上文答话。
    """
    from src import observability

    events: list = []

    class _Sink:
        def counter(self, name, amount=1, **tags):
            events.append((name, tags))

        def histogram(self, name, value, **tags):
            pass

        def gauge(self, name, value, **tags):
            pass

    # 用 set_sink 而不是直接改 `_sink`：`_dispatch` 读的是模块全局，但注入点
    # 只有 set_sink 会做形状校验 —— 直接改属性时"形状不对"不会被发现，
    # 排查起来会以为是"指标没上报"。
    previous = observability.get_sink()
    observability.set_sink(_Sink())
    try:
        view = C.project_history(_rounds(2), {"covered_items": 100, "summary": "摘要"})
    finally:
        observability.set_sink(previous)

    assert view == _rounds(2)
    assert ("agent_history_projection_errors_total", {"reason": "shorter"}) in events


# --------------------------------------------------------------------------- #
# ③ 滚动压缩：上一代摘要要折进下一代
# --------------------------------------------------------------------------- #
def test_second_round_feeds_the_previous_summary_back(on, store, monkeypatch):
    """第二次压缩交给模型的段 = **上一代摘要** + 新滑出的原文。

    不这么做的话，上一代摘要里那些更早的信息（尤其"最初要干什么"）会在换代时
    整段丢掉，而新摘要看起来完全正常。
    """
    seen: list[list[str]] = []

    def _stub(segment, **kw):
        seen.append([str(x.get("content")) for x in segment])
        return "简短摘要。"

    monkeypatch.setattr(C, "summarize_items", _stub)

    fake = FakeSession(_rounds(3))
    wrapped = C.CompactingSession(fake, store=store)
    assert _compact(wrapped) == 2          # 3 轮留 2 轮 ⇒ 覆盖前 1 轮（2 条）

    fake.items.extend(_rounds(3, start=3))
    assert _compact(wrapped) > 0

    second = seen[-1]
    assert second[0] == "简短摘要。", f"上一代摘要必须排在段首，实际是 {second!r}"
    assert not any("第0轮" in x for x in second), "已经被覆盖的原文不该再读一遍"


def test_nothing_new_to_cover_means_zero_calls(on, store, monkeypatch):
    """上一代已经盖到边界 ⇒ 直接返回 0，**连摘要都不调**。"""
    calls = []

    def _stub(segment, **kw):
        calls.append(segment)
        return "简短摘要。"

    monkeypatch.setattr(C, "summarize_items", _stub)

    fake = FakeSession(_rounds(3))
    wrapped = C.CompactingSession(fake, store=store)
    assert _compact(wrapped) == 2
    before = len(calls)

    assert _compact(wrapped) == 0
    assert len(calls) == before, "没有新段可压却调了一次摘要 = 白花钱"


def test_a_summary_that_is_not_shorter_is_rejected(on, store, monkeypatch):
    """摘要不比被取代的那段短 ⇒ **不写记录**。

    不校验的后果是每轮都压同一段：多花一次模型调用、摘要一代代变长，
    而日志里什么都看不出来。
    """
    monkeypatch.setattr(C, "summarize_items", lambda segment, **kw: "很长的摘要。" * 400)

    fake = FakeSession(_rounds(3))
    wrapped = C.CompactingSession(fake, store=store)

    assert _compact(wrapped) == 0
    assert store.rows() == [], "没换来更小的东西就不该留下记录"


# --------------------------------------------------------------------------- #
# ④ 存储本身：单条 active、旧行不删
# --------------------------------------------------------------------------- #
def test_store_keeps_one_active_record_and_never_deletes():
    store = InMemoryCompactionStore()

    async def _go():
        await store.record("t", covered_items=2, summary="第一代")
        await store.record("t", covered_items=4, summary="第二代")
        return await store.latest_active("t")

    latest = asyncio.run(_go())
    rows = store.rows()
    assert len(rows) == 2, "旧记录必须留着（要能查某一轮当时看到的是哪一版）"
    assert [r["status"] for r in rows] == ["superseded", "active"]
    assert rows[0]["superseded_by"] == rows[1]["id"]
    assert latest["summary"] == "第二代"
    assert latest["covered_items"] == 4


def test_store_isolates_threads():
    """记录按会话隔离：A 会话的覆盖边界绝不能作用到 B 上。"""
    store = InMemoryCompactionStore()

    async def _go():
        await store.record("a", covered_items=10, summary="A 的摘要")
        return await store.latest_active("b")

    assert asyncio.run(_go()) is None
