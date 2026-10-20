"""跨轮历史压缩的守卫（``src/agents/compaction.py`` + 四个调用点）。

为什么需要这一份
================
压缩是**唯一一处会主动重写历史**的代码。它出错的形态全都**不报错**：

* 切点落在"助手说要调工具"和"工具返回"之间 ⇒ 回灌被接口拒（这一条会报，但报在
  下一轮、离现场很远）；
* 摘要为空却照做替换 ⇒ **老历史整段消失**（比不压严重得多，而且不可恢复）；
* 摘要在**锁外**算完之后不重读就替换 ⇒ 把期间新写入的轮次顶掉（内容静默丢失）；
* 替换失败不回滚 ⇒ 会话停在"已清空"上（用户下一轮失忆）；
* 压掉了打回轮依赖的"原话" ⇒ 模型只看到一句"人工审核意见：…"，答得莫名其妙；
* 保留轮数配成 0 ⇒ 同上，而且看起来"配置生效了"。

所以这一份按"**每一条都必须能被变异验证抓住**"来写：判据都落在**内容**上
（保留段逐条比对不对、压缩段里有没有用户原话、替换失败后库里的内容是不是原样），
不停留在"函数被调用了"这种层次。
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.agents import compaction as C
from src.agents import history_budget as hb

#: 让"在线程里写摘要"的替身能回到事件循环上（``_slow_summary`` 用）。
_LOOP: dict = {"loop": None}


# --------------------------------------------------------------------------- #
# 替身：一个最小的会话后端（官方只按鸭子类型识别，不必继承任何类）
# --------------------------------------------------------------------------- #
class FakeSession:
    """内存版会话（四个协议方法 + 可控的失败注入）。"""

    def __init__(self, items=None):
        self.items = list(items or [])
        self.session_id = "s-1"
        self.session_settings = "SETTINGS"
        self.calls: list[str] = []
        #: 注入用：``(异常, 次数)``。次数用尽后恢复正常（回滚才有机会成功）。
        self.fail_add: BaseException | None = None
        self.fail_add_times = 0
        self.fail_get: BaseException | None = None

    async def get_items(self, limit=None):
        self.calls.append("get_items")
        if self.fail_get is not None:
            raise self.fail_get
        return list(self.items)

    async def add_items(self, items):
        self.calls.append("add_items")
        if self.fail_add is not None and self.fail_add_times > 0:
            self.fail_add_times -= 1
            raise self.fail_add
        self.items.extend(items)

    async def pop_item(self):
        self.calls.append("pop_item")
        return self.items.pop() if self.items else None

    async def clear_session(self):
        self.calls.append("clear")
        self.items = []


def _rounds(n: int, *, label: str = "r") -> list[dict]:
    """n 轮 SDK 形态的历史（每轮 = 一条用户 + 一条助手）。"""
    out: list[dict] = []
    for i in range(n):
        out.append({"role": "user", "content": f"{label}{i}-问"})
        out.append({"role": "assistant", "content": f"{label}{i}-答"})
    return out


def _messages(n: int, *, prefix: bool = False) -> list:
    """n 轮 LangChain 形态的历史（可选带一条"旧摘要"前缀）。"""
    out: list = [SystemMessage(content="[旧摘要]")] if prefix else []
    for i in range(n):
        out.append(HumanMessage(content=f"u{i}"))
        out.append(AIMessage(content=f"a{i}"))
    return out


def _text(item) -> str:
    return item.get("content") if isinstance(item, dict) else getattr(item, "content", "")


@pytest.fixture
def on(monkeypatch):
    """打开压缩；保留轮数设成 2（小于用例给的历史轮数），保证"有东西可压"。"""
    monkeypatch.setenv("AGENT_HISTORY_COMPACT", "1")
    monkeypatch.setenv("AGENT_HISTORY_KEEP_ROUNDS", "2")
    C.reset_compaction_state()
    yield
    C.reset_compaction_state()


@pytest.fixture
def fake_summary(monkeypatch):
    """把摘要换成**由被压缩段拼出来**的固定串。

    为什么不是返回常量：这样才能断言"交给摘要的**恰好**是那一段" —— 常量摘要会让
    "切分切错了"看起来一切正常（摘要总是一样的）。
    """

    def _stub(segment):
        return "【摘要】" + "|".join(str(_text(x)) for x in segment)

    monkeypatch.setattr(C, "summarize_items", _stub)
    return _stub


# --------------------------------------------------------------------------- #
# ① 配置：默认关、保留轮数下限 1
# --------------------------------------------------------------------------- #
def test_compaction_is_off_by_default(monkeypatch):
    """默认**关闭**：新链路先可关，出问题退回的代价是零。

    这条同时是"今天的行为逐字节不变"的判据 —— 关着时两个入口都必须**一次都不动**。
    """
    monkeypatch.delenv("AGENT_HISTORY_COMPACT", raising=False)
    assert C.history_compact_enabled() is False
    for raw in ("", "0", "off", "false", "no", "随便写点什么"):
        monkeypatch.setenv("AGENT_HISTORY_COMPACT", raw)
        assert C.history_compact_enabled() is False, f"{raw!r} 不该被当成打开"
    for raw in ("1", "on", "TRUE", "yes"):
        monkeypatch.setenv("AGENT_HISTORY_COMPACT", raw)
        assert C.history_compact_enabled() is True, f"{raw!r} 该被当成打开"


def test_keep_rounds_can_never_drop_below_one(monkeypatch):
    """保留轮数**下限 1** —— 配 0 会被夹到 1，不是"全部压掉"。

    为什么这条是硬约束：人工打回那一轮靠"**原话已经躺在会话里**"决定不重传原话
    （``runner._build_input_items``）。保留 0 轮 ⇒ 第二遍只看到一句"人工审核意见：…"，
    **不知道在改什么**，而且**不报错**。配错的表现必须只是"多留一轮"，不是"信息没了"。
    """
    monkeypatch.setenv("AGENT_HISTORY_KEEP_ROUNDS", "0")
    assert C.keep_rounds() == 1
    monkeypatch.setenv("AGENT_HISTORY_KEEP_ROUNDS", "-5")
    assert C.keep_rounds() == 1
    monkeypatch.setenv("AGENT_HISTORY_KEEP_ROUNDS", "不是数字")
    assert C.keep_rounds() == 5  # 回落默认值，不是 0


# --------------------------------------------------------------------------- #
# ② 切分：切点必须落在用户条目之前（不许切开工具调用与它的结果）
# --------------------------------------------------------------------------- #
def test_cut_point_never_splits_a_tool_call_from_its_output():
    """切点只能落在"轮的起点"（用户条目）上。

    工具调用与它的结果靠 ``call_id`` 配对，切开就散了、回灌会被接口拒。这条**不能**
    靠"看起来对"—— 这里把工具调用摆在保留边界**紧邻前面**，切错一格就会露出来。
    """
    items = [
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "u1"},
        {"type": "function_call", "name": "kb_search", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "结果"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    assert C._plan_keep_from(items, keep=1) == 6, "保留段必须从 u2 这一轮的起点开始"

    for keep in (1, 2):
        kf = C._plan_keep_from(items, keep=keep)
        assert kf is not None and C._is_user_item(items[kf]), (
            f"keep={keep} 的切点落在了非用户条目上：{items[kf]}"
        )
        seg = items[:kf]
        calls = {x.get("call_id") for x in seg if x.get("type") == "function_call"}
        outs = {x.get("call_id") for x in seg if x.get("type") == "function_call_output"}
        assert calls == outs, f"keep={keep} 把工具调用与其结果切散了：{calls} vs {outs}"


def test_kept_rounds_come_back_byte_identical(on, fake_summary):
    """保留段必须**逐条一字不变**（这是"压缩不篡改近期对话"的全部内容）。"""
    items = _rounds(4)
    keep_from = C._plan_keep_from(items, keep=2)
    assert keep_from == 4
    expected_tail = [dict(x) for x in items[keep_from:]]

    fake = FakeSession(items)
    wrapped = C.CompactingSession(fake)

    async def _go():
        return await C.compact_session_history("s-1", session=wrapped)

    asyncio.run(_go())
    assert fake.items[0] == {"role": "system", "content": fake_summary(items[:4])}
    assert fake.items[1:] == expected_tail, "保留段必须与原始条目完全相等"


# --------------------------------------------------------------------------- #
# ③ 会话侧：替换、空摘要、读失败、锁、失败回滚
# --------------------------------------------------------------------------- #
def test_old_history_is_replaced_by_one_system_summary(on, fake_summary):
    """老历史换成**一条 system 摘要 + 最近 N 轮**；摘要必须是 system 角色。

    为什么必须是 ``system``：它**天然不算作"一轮"**（轮按用户条目切），所以下一次
    压缩不会把上一次的摘要当成新的一轮再压一遍；换成 user/assistant 就会越压越少。
    """
    items = _rounds(4)
    fake = FakeSession(items)
    wrapped = C.CompactingSession(fake)

    async def _go():
        return await C.compact_session_history("s-1", session=wrapped)

    dropped = asyncio.run(_go())
    assert dropped == 4, "4 轮里该压掉前 2 轮（保留 2 轮）"
    assert fake.items[0]["role"] == "system", "摘要必须是 system 角色"
    assert fake.items[1:] == items[4:]
    assert fake.calls.count("clear") == 1, "替换必须是「清空 + 写回」一次，不能分两次"


def test_empty_summary_means_no_replacement(on, monkeypatch):
    """摘要为空 ⇒ **一个字节都不改**。

    这是整套机制里最危险的一条分支：摘要为空却照做替换，等于**把老历史删掉**
    （比不压严重得多，而且不可恢复）。所以判据是"库里原样"，不是"函数返回了 0"。
    """
    monkeypatch.setattr(C, "summarize_items", lambda segment: "")
    items = _rounds(4)
    fake = FakeSession(items)
    wrapped = C.CompactingSession(fake)

    async def _go():
        return await C.compact_session_history("s-1", session=wrapped)

    assert asyncio.run(_go()) == 0
    assert fake.items == items, "摘要为空时库里必须原样不动"
    assert "clear" not in fake.calls, "摘要为空时连清空都不该发生"


def test_history_read_failure_does_nothing(on):
    """读不出历史 ⇒ 什么都不做（不算错误、不改库、不抛）。"""
    fake = FakeSession(_rounds(4))
    fake.fail_get = RuntimeError("PG 挂了")
    wrapped = C.CompactingSession(fake)

    async def _go():
        return await C.compact_session_history("s-1", session=wrapped)

    assert asyncio.run(_go()) == 0
    assert "clear" not in fake.calls and "add_items" not in fake.calls


def test_summarizing_does_not_hold_the_lock(on, monkeypatch):
    """写摘要**不许占着锁** —— 期间本轮的 ``add_items`` 必须能照常进行。

    为什么这条值得单列：把"写摘要"挪进锁内是最容易想到的"简化"（也最像对的写法），
    代价是把一次秒级的模型调用摊到用户的会话写入上 —— 用户会看到"说话后半天没反应"。
    官方也是这个形状（``responses.compact`` 在锁外，只有重读 + 替换在锁内）。
    """
    entered = asyncio.Event()
    release = {"done": False}

    def _slow(segment):
        loop = _LOOP["loop"]
        loop.call_soon_threadsafe(entered.set)
        while not release["done"]:
            time.sleep(0.01)
        return "摘要"

    monkeypatch.setattr(C, "summarize_items", _slow)
    fake = FakeSession(_rounds(4))
    wrapped = C.CompactingSession(fake)

    async def _go():
        _LOOP["loop"] = asyncio.get_running_loop()
        task = asyncio.create_task(C.compact_session_history("s-1", session=wrapped))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            # 摘要还在飞 —— 此时写会话必须立刻成功，不能被压缩挡住
            await asyncio.wait_for(
                wrapped.add_items([{"role": "user", "content": "本轮"}]), timeout=2
            )
        finally:
            # ⚠️ 这一句是**给变异验证用的**，不是修饰：把摘要挪进锁内时上面那句会超时，
            # 此时若不给替身放行，那个跑在**线程**里的 `_slow` 会一直转，而
            # `asyncio.run` 收尾会 `shutdown_default_executor()` **永久等待**它 ——
            # 红灯就变成"卡死"，比不报还糟（变异验证读到的是超时，不是失败）。
            release["done"] = True
        return await asyncio.wait_for(task, timeout=5)

    asyncio.run(_go())
    assert "本轮" in [_text(x) for x in fake.items]


def test_concurrent_write_during_summary_aborts_this_round(on, monkeypatch):
    """摘要期间历史变了（有新轮次落进来）⇒ **放弃本轮**，不改库。

    为什么必须放弃而不是照写：摘要只覆盖了"旧的那一段"，此时落进来的新轮次不在
    摘要里 —— 照写就会**用一条不含它的摘要把它顶掉**（内容静默丢失）。
    放弃的代价只是"偶尔漏压一次"，下一轮会再来。
    """
    entered = asyncio.Event()

    def _slow(segment):
        _LOOP["loop"].call_soon_threadsafe(entered.set)
        time.sleep(0.3)
        return "摘要"

    monkeypatch.setattr(C, "summarize_items", _slow)
    fake = FakeSession(_rounds(4))
    wrapped = C.CompactingSession(fake)

    async def _go():
        _LOOP["loop"] = asyncio.get_running_loop()
        task = asyncio.create_task(C.compact_session_history("s-1", session=wrapped))
        await asyncio.wait_for(entered.wait(), timeout=5)
        # 期间"本轮"又写进了一轮（保留边界整体往外推）
        await wrapped.add_items(
            [{"role": "user", "content": "新问题"}, {"role": "assistant", "content": "新回答"}]
        )
        return await asyncio.wait_for(task, timeout=5)

    assert asyncio.run(_go()) == 0
    assert "clear" not in fake.calls, "边界变了却不放弃 ⇒ 会把新写入的轮次顶掉"
    assert [_text(x) for x in fake.items][-2:] == ["新问题", "新回答"]


def test_replacement_failure_restores_previous_history():
    """替换失败（写回那一步炸了）⇒ 历史**退回原样**，不能停在"已清空"上。"""
    items = _rounds(4)
    fake = FakeSession(items)
    fake.fail_add = RuntimeError("PG 写失败")
    fake.fail_add_times = 1

    async def _go():
        with pytest.raises(RuntimeError):
            await C._replace_items(fake, summary="摘要", tail=items[4:], previous=items)

    asyncio.run(_go())
    assert fake.items == items, "替换失败后库里必须退回原历史（不是空会话）"


def test_cancellation_during_replacement_also_restores():
    """**取消**同样要回滚 —— 取消不是"可以不清场"的理由。

    官方把 ``Exception`` 与 ``CancelledError`` 分开写、两种都回滚，理由写在注释里：
    否则一次取消就会留下一个空会话（用户下一轮失忆）。这里连"回滚本身再被打断"
    都要扛住（官方 ``_await_restore_despite_cancellation``）。
    """
    items = _rounds(4)
    fake = FakeSession(items)
    fake.fail_add = asyncio.CancelledError()
    fake.fail_add_times = 1

    async def _go():
        task = asyncio.create_task(
            C._replace_items(fake, summary="摘要", tail=items[4:], previous=items)
        )
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return task.cancelled()

    assert asyncio.run(_go()) is True, "取消必须原样传播出去"
    assert fake.items == items, "取消之后库里必须退回原历史"


def test_decorator_proxies_the_protocol_without_changing_it():
    """装饰器必须**透明**：四个方法原样透传，另加两个被 SDK 读的属性。

    ``session_settings`` / ``session_id`` 不代理的后果：SDK 是
    ``getattr(session, "session_settings", None)``（``session_persistence.py``），
    拿不到就**静默**改用默认值 —— 没有报错，只是行为悄悄变了。
    """
    fake = FakeSession([{"role": "user", "content": "x"}])
    wrapped = C.CompactingSession(fake)
    assert wrapped.session_id == "s-1"
    assert wrapped.session_settings is fake.session_settings

    async def _go():
        assert await wrapped.get_items() == [{"role": "user", "content": "x"}]
        await wrapped.add_items([{"role": "assistant", "content": "y"}])
        assert await wrapped.pop_item() == {"role": "assistant", "content": "y"}
        await wrapped.clear_session()
        return await wrapped.get_items()

    assert asyncio.run(_go()) == []


def test_the_entry_uses_the_official_duck_typing_contract():
    """``run_compaction`` 这个名字是**官方契约**（官方靠 ``getattr`` 识别，不看类型）。

    ⚠️ 同时钉住另一半：SDK **现在不会**调用它 —— 它的调用点判据是
    ``if response_id and ...``，而本项目走 Chat Completions、那条路上
    ``response_id`` 恒为空（``openai_chatcompletions.py`` 写死 ``response_id=None``）。
    所以名字保留（将来真换到 Responses 接口形状不用改）、触发由我们自己的入口做。
    """
    from agents.memory.session import is_openai_responses_compaction_aware_session

    wrapped = C.CompactingSession(FakeSession())
    assert is_openai_responses_compaction_aware_session(wrapped) is True
    assert is_openai_responses_compaction_aware_session(FakeSession()) is False


# --------------------------------------------------------------------------- #
# ⑤ 窗口压缩：触发线、提前判、最新一轮无条件保留、压了没用就不压
# --------------------------------------------------------------------------- #
@pytest.fixture
def tiny_budget(monkeypatch):
    """把预算调到很小，并让每个条目的 token 数固定 ⇒ 判定确定，不依赖分词器。"""
    monkeypatch.setenv("AGENT_HISTORY_COMPACT", "1")
    monkeypatch.setenv("AGENT_HISTORY_KEEP_ROUNDS", "5")
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "20")
    monkeypatch.setenv("AGENT_HISTORY_COMPACT_RATIO", "1.0")
    monkeypatch.setattr(hb, "_item_tokens", lambda item: 10)
    monkeypatch.setattr(C, "summarize_items", lambda segment: "摘要")
    C.reset_compaction_state()
    yield
    C.reset_compaction_state()


def test_window_compaction_fires_only_above_the_trigger(tiny_budget):
    """低于触发线 ⇒ 一个字都不改（返回 ``None``，调用方原样送模型）。"""
    items = _rounds(2)  # 4 条 × 10 = 40 > 触发线 20

    async def _go():
        return await C.compact_for_window(items)

    assert asyncio.run(_go()) is not None, "40 > 20，应当触发"

    under = items[:2]  # 2 条 × 10 = 20，恰好**不超**（判据是严格大于）

    async def _under():
        return await C.compact_for_window(under)

    assert asyncio.run(_under()) is None, "没超触发线就不该压"


def test_window_compaction_always_keeps_the_newest_round(tiny_budget):
    """触发时，**最新那一轮无条件保留**。

    丢掉它等于模型连用户刚说的话都看不到，而请求照常 200（``history_budget`` 的口径 1
    同源）。这里把预算调到"最新一轮自己都不够"的极端，看它还在不在。
    """
    items = _rounds(4)  # 8 条，触发线 20 ⇒ 只装得下 1 轮

    async def _go():
        return await C.compact_for_window(items)

    replacement = asyncio.run(_go())
    assert replacement is not None
    assert replacement[0]["role"] == "system"
    assert replacement[1:] == items[-2:], "最新那一轮必须原样在"


def test_window_compaction_is_a_noop_when_everything_already_fits(tiny_budget, monkeypatch):
    """最近 ``keep_rounds`` 轮装得下 ⇒ 不压（老的那段由**历史压缩**负责）。

    两条入口分工不能混：窗口压缩解决的是"这几轮本身就太大"，不是"历史太长"。
    混在一起的话，窗口压缩会把历史压缩该保留的轮次也压掉。
    """
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "10000")
    items = _rounds(4)

    async def _go():
        return await C.compact_for_window(items)

    assert asyncio.run(_go()) is None


# --------------------------------------------------------------------------- #
# ⑥ 关掉时：两个入口都必须"一次都不动"
# --------------------------------------------------------------------------- #
def test_both_entries_are_noops_when_disabled(monkeypatch, fake_summary):
    """开关关着时，两个入口都**一次都不动**（这是"默认值 = 今天的行为"的判据）。"""
    monkeypatch.delenv("AGENT_HISTORY_COMPACT", raising=False)
    C.reset_compaction_state()
    items = _rounds(6)
    fake = FakeSession(items)
    wrapped = C.CompactingSession(fake)

    async def _session():
        return await C.compact_session_history("s-1", session=wrapped)

    async def _window():
        return await C.compact_for_window(items)

    assert asyncio.run(_session()) == 0
    assert asyncio.run(_window()) is None
    assert fake.items == items
    assert fake.calls == [], "关着的时候连读都不该发生"


def test_config_exposes_compaction_for_startup_echo():
    """配置必须能回答"线上到底在不在压" —— 不回显就只能猜。"""
    from app import config

    hist = config.as_dict()["agents"]["history"]
    assert hist["compact"] is False  # 默认关
    assert hist["keep_rounds"] >= 1
    assert 0 < hist["compact_ratio"] <= 1
