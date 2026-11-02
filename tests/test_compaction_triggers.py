"""压缩判据的守卫：八成线、按 token 保留尾部、溢出兜底、强压。

为什么这些数字值得钉住（它们是"什么时候花钱"的全部依据）：

* **触发线**决定"要不要多调一次模型"。定高了等到模型已经退化才动手，定低了每轮都
  白压一次。0.80 与 0.85 的差别不是 5 个百分点，而是"在注意力开始下降之前"与
  "在它之后"。
* **保留段**决定"模型还记得多少最近的事"。按轮数算等于拿一个与成本无关的刻度去
  量 token：一次超长工具返回就让 5 轮撑爆。
* **强压**（溢出兜底）是唯一一条"模型已经拒了还继续"的路径，必须只留一轮、
  且只做一次。
"""

from __future__ import annotations

import asyncio

import pytest

import src.agents.compaction as C
from src.agents import history_budget
from src.agents.compaction_store import InMemoryCompactionStore


class FakeSession:
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
        return self.items.pop() if self.items else None

    async def clear_session(self):
        self.calls.append("clear")
        self.items = []


def _rounds(n: int, *, size: int = 40) -> list[dict]:
    out: list[dict] = []
    for i in range(n):
        out.append({"role": "user", "content": f"u{i}:" + "问" * size})
        out.append({"role": "assistant", "content": f"a{i}:" + "答" * size})
    return out


def _round_tokens(items: list[dict], index: int) -> int:
    return sum(history_budget._item_tokens(x) for x in items[index * 2:(index + 1) * 2])


# --------------------------------------------------------------------------- #
# ① 触发线与保留预算
# --------------------------------------------------------------------------- #
def test_trigger_is_eighty_percent_of_the_budget(monkeypatch):
    """压力线 = 预算 × 0.8（2026-11-02 从 0.85 下调）。"""
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "100000")
    assert C.window_compact_ratio() == pytest.approx(0.80)
    assert C.window_compact_trigger() == 80000


def test_tail_budget_is_sixteen_percent(monkeypatch):
    """逐字保留段 = 预算 × 0.16，与触发线**各算各的**。"""
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "100000")
    assert C.retain_ratio() == pytest.approx(0.16)
    assert C.tail_budget() == 16000


def test_window_target_is_forty_percent_of_the_budget(monkeypatch):
    """窗口压缩压到预算的 40% 为止（2026-11-02 从"固定保留 16%"改成按水位反推）。

    触发线回答"要不要压"，水位回答"压到哪停"。两者必须分开：共用一个数会让压完
    刚好贴着触发线，下一轮立刻再触发一次，每轮白花一次调用。
    """
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "100000")
    assert C.target_ratio() == pytest.approx(0.40)
    assert C.window_target() == 40000
    assert C.window_target() < C.window_compact_trigger(), "水位必须在触发线之下"


def test_the_target_can_be_configured(monkeypatch):
    """换模型只该改配置：水位也是配置项。"""
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "100000")
    monkeypatch.setenv("AGENT_HISTORY_COMPACT_TARGET_RATIO", "0.25")
    assert C.window_target() == 25000


def test_a_fat_round_shrinks_the_verbatim_tail(monkeypatch):
    """一轮很长时，保留段按 **token** 收敛，不按"轮数"硬留。"""
    items = _rounds(4, size=400)
    fat = _round_tokens(items, 3)

    keep_from = C._keep_from_by_tokens(items, budget=max(1, fat - 1), cap=5)

    assert keep_from == len(items) - 2, "最后一轮本身就超预算 ⇒ 只能留它自己"


def test_a_roomy_budget_keeps_everything(monkeypatch):
    """预算装得下全部 ⇒ 返回 None（不需要压），而不是"压一点点"。"""
    items = _rounds(2)
    assert C._keep_from_by_tokens(items, budget=10**6, cap=5) is None


def test_cap_still_wins_when_rounds_are_tiny(monkeypatch):
    """预算很大但轮数上限小 ⇒ 仍然按上限截。

    不设这条上限的后果：每轮都很短时，保留段会越攒越长，覆盖边界永远推不动，
    压缩形同虚设（而且不会有任何报错）。
    """
    items = _rounds(6, size=5)
    keep_from = C._keep_from_by_tokens(items, budget=10**6, cap=2)
    assert keep_from == len(items) - 4, "cap=2 ⇒ 只留最后 2 轮"


# --------------------------------------------------------------------------- #
# ② 强压：只留一轮
# --------------------------------------------------------------------------- #
def test_forced_compaction_keeps_only_the_last_round(monkeypatch):
    """溢出兜底走的强压：预算按 0 算 ⇒ 覆盖边界推到**最后一轮之前**。"""
    monkeypatch.setenv("AGENT_HISTORY_COMPACT", "1")
    monkeypatch.setenv("AGENT_HISTORY_KEEP_ROUNDS", "5")
    monkeypatch.setattr(C, "summarize_items", lambda segment, **kw: "简短摘要。")

    items = _rounds(4)
    fake = FakeSession(items)
    store = InMemoryCompactionStore()
    wrapped = C.CompactingSession(fake, store=store)

    dropped = asyncio.run(C.compact_session_history("s-1", session=wrapped, force=True))

    assert dropped == len(items) - 2, "强压只留最后一轮（2 条）"
    record = asyncio.run(store.latest_active("s-1"))
    assert record["covered_items"] == len(items) - 2
    assert fake.items == items, "强压也不许动日志"


# --------------------------------------------------------------------------- #
# ③ 溢出判据
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("message", [
    "Error code: 400 - {'error': {'code': 'context_length_exceeded'}}",
    "This model's maximum context length is 65536 tokens",
    "prompt is too long: 200000 tokens > 128000 maximum",
    "输入超过最大上下文长度",
])
def test_overflow_messages_are_recognized(message):
    assert C.is_context_overflow(RuntimeError(message)) is True


@pytest.mark.parametrize("message", [
    "429 Too Many Requests",
    "invalid api key",
    "Connection reset by peer",
    "Error code: 400 - unknown parameter",
])
def test_other_failures_are_not_mistaken_for_overflow(message):
    """判据宽是对的，但不能宽到"任何 400 都算溢出" —— 那会把参数错误压成
    "历史太长"，真正的原因被藏起来。"""
    assert C.is_context_overflow(RuntimeError(message)) is False
