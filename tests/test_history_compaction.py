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

    def _stub(segment, **kw):
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
    """保留段必须**逐条一字不变**，而且库里那份一个字都不许动。

    改造后"保留"有两层含义，都要断言：库里是原件（压缩只追加记录），模型看到的
    那一份由投影算出、尾部与原件完全相等。
    """
    items = _rounds(4)
    keep_from = C._plan_keep_from(items, keep=2)
    assert keep_from == 4
    expected_tail = [dict(x) for x in items[keep_from:]]

    fake = FakeSession(items)
    wrapped = C.CompactingSession(fake)

    async def _go():
        await C.compact_session_history("s-1", session=wrapped)
        return await wrapped.get_items()

    view = asyncio.run(_go())
    assert fake.items == items, "★ 会话日志必须一个字节都不动"
    assert view[1:] == expected_tail, "投影的尾部必须与原件完全相等"


# --------------------------------------------------------------------------- #
# ③ 会话侧：记录 + 投影、空摘要、读失败、锁、写记录失败
# --------------------------------------------------------------------------- #
def test_old_history_becomes_a_projected_summary(on, fake_summary):
    """老历史在**视图**里换成一条 system 摘要 + 最近 N 轮；库里仍是全量。

    为什么摘要必须是 ``system``：它**天然不算作"一轮"**（轮按用户条目切），所以下一次
    压缩不会把上一次的摘要当成新的一轮再压一遍；换成 user/assistant 就会越压越少。
    """
    items = _rounds(4)
    fake = FakeSession(items)
    wrapped = C.CompactingSession(fake)

    async def _go():
        dropped = await C.compact_session_history("s-1", session=wrapped)
        return dropped, await wrapped.get_items()

    dropped, view = asyncio.run(_go())
    assert dropped == 4, "4 轮里该新覆盖前 2 轮（保留 2 轮）"
    assert view[0]["role"] == "system", "摘要必须是 system 角色"
    assert view[0]["content"] == C._SUMMARY_PREAMBLE + fake_summary(items[:4]), \
        "抬头 + 摘要正文；抬头不能替代正文，正文一个字都不许丢"
    assert view[1:] == items[4:]
    assert len(fake.items) == len(items), "★ 库里的条数不许变"
    assert "clear" not in fake.calls, "★ 不许再出现清空会话"
    assert "add_items" not in fake.calls, "★ 不许再往会话里写摘要"


def test_empty_summary_means_no_replacement(on, monkeypatch):
    """摘要为空 ⇒ **一个字节都不改**。

    这是整套机制里最危险的一条分支：摘要为空却照做替换，等于**把老历史删掉**
    （比不压严重得多，而且不可恢复）。所以判据是"库里原样"，不是"函数返回了 0"。
    """
    monkeypatch.setattr(C, "summarize_items", lambda segment, **kw: "")
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

    def _slow(segment, **kw):
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

    def _slow(segment, **kw):
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


def test_record_failure_leaves_the_log_untouched(on, fake_summary):
    """写压缩记录失败 ⇒ 这轮没压。**没有"回滚"这回事**，因为压根没动过日志。

    这条取代了原来的"替换失败要恢复到已清空之前"：改造后不存在中间态，
    所以判据从"能恢复"升级成"根本没发生"。
    """
    items = _rounds(4)
    fake = FakeSession(items)

    class _BoomStore:
        async def latest_active(self, thread_id, scope="history"):
            return None

        async def record(self, *a, **k):
            raise RuntimeError("记录表写失败")

    wrapped = C.CompactingSession(fake, store=_BoomStore())

    async def _go():
        return await C.compact_session_history("s-1", session=wrapped)

    assert asyncio.run(_go()) == 0
    assert fake.items == items, "写记录失败时库里必须原样不动"
    assert "clear" not in fake.calls and "add_items" not in fake.calls


def test_cancellation_while_recording_leaves_the_log_untouched(on, fake_summary):
    """**取消**同样只影响"这一轮压没压成"，不影响库里的内容。

    旧路线这里要处理"清空之后被取消"（会留下空会话），所以有整整一套回滚；
    现在取消了最坏就是"记录没写进去、下一轮再压"，历史一直是完整的。
    """
    items = _rounds(4)
    fake = FakeSession(items)

    class _CancelStore:
        async def latest_active(self, thread_id, scope="history"):
            return None

        async def record(self, *a, **k):
            raise asyncio.CancelledError()

    wrapped = C.CompactingSession(fake, store=_CancelStore())

    async def _go():
        task = asyncio.create_task(C.compact_session_history("s-1", session=wrapped))
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return task.cancelled()

    assert asyncio.run(_go()) is True, "取消必须原样传播出去"
    assert fake.items == items, "取消之后库里必须原样不动"


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
    """把预算调到很小，并让每个条目的 token 数固定 ⇒ 判定确定，不依赖分词器。

    预算 100 / 触发线 100（``COMPACT_RATIO=1.0``）/ 水位 40，每条 10 token。
    摘要成本与最小净收益也压到 2 token：``_MIN_SUMMARY_CHARS=600`` 那条下限是
    "真实字号"下才成立的常量，在几十 token 的尺度上会直接把水位撑爆 —— 换成同一
    刻度才测得到算法本身，而不是在测那个下限。
    """
    monkeypatch.setenv("AGENT_HISTORY_COMPACT", "1")
    monkeypatch.setenv("AGENT_HISTORY_KEEP_ROUNDS", "5")
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "100")
    monkeypatch.setenv("AGENT_HISTORY_COMPACT_RATIO", "1.0")
    monkeypatch.setattr(hb, "_item_tokens", lambda item: 10)
    monkeypatch.setattr(C, "summarize_items", lambda segment, **kw: "摘要")

    async def _fake_summarize(segment, *, on_usage=None, goal=""):
        return "摘要"

    # ⚠️ 必须连协程入口一起打桩：只桩同步版的话，``compact_for_window`` 仍会去建
    # client 发一次真请求（conftest 给了假 Key），失败返回空串 ⇒ 每个用例都从
    # "摘要为空"那条分支退出，**看起来通过，其实一条断言都没走**。
    monkeypatch.setattr(C, "_summarize_async", _fake_summarize)
    monkeypatch.setattr(C, "_summary_max_chars", lambda chars: 2)
    monkeypatch.setattr(C, "_MIN_WINDOW_GAIN", 1)
    # 抬头也压掉：真实抬头有几十字（≈几十 token），在这个 40 token 的水位上它自己
    # 就能把水位顶爆 ⇒ 所有窗口用例一律返回 None。这里测的是"留几轮"的算法，抬头
    # 的注入另有专项用例（``test_the_summary_reaches_the_model_with_a_static_preamble``）。
    monkeypatch.setattr(C, "_SUMMARY_PREAMBLE", "")
    C.reset_compaction_state()
    yield
    C.reset_compaction_state()


def test_window_compaction_fires_only_above_the_trigger(tiny_budget):
    """低于触发线 ⇒ 一个字都不改（返回 ``None``，调用方原样送模型）。"""
    items = _rounds(6)  # 12 条 × 10 = 120 > 触发线 100

    async def _go():
        return await C.compact_for_window(items)

    assert asyncio.run(_go()) is not None, "120 > 100，应当触发"

    under = _rounds(1)  # 2 条 × 10 = 20，远低于触发线

    async def _under():
        return await C.compact_for_window(under)

    assert asyncio.run(_under()) is None, "没超触发线就不该压"


def test_window_compaction_always_keeps_the_newest_round(tiny_budget):
    """触发时，**最新那一轮无条件保留**。

    丢掉它等于模型连用户刚说的话都看不到，而请求照常 200（``history_budget`` 的口径 1
    同源）。水位 40 只装得下一轮（20 + 摘要 2 = 22），所以这里就是那个边界。
    """
    items = _rounds(6)

    async def _go():
        return await C.compact_for_window(items)

    replacement = asyncio.run(_go())
    assert replacement is not None
    assert replacement[0]["role"] == "system"
    assert replacement[1:] == items[-2:], "最新那一轮必须原样在"


def test_a_big_old_segment_still_gets_compacted(tiny_budget, monkeypatch):
    """★★ 老段很大、最近几轮很小时，**仍然要压**。

    2026-11-02 之前这里返回 ``None``（"最近 keep_rounds 轮本身装得下 ⇒ 老段不归
    我管"），于是那段老历史一路交给硬截断一条条丢掉 —— **连摘要都没有**。而它恰恰
    是唯一能压、也该压的东西：最近几轮再小，也不能让它们替老段承担全部压缩量。
    """
    monkeypatch.setattr(hb, "_item_tokens",
                        lambda item: 10 if "大" in item["content"] else 1)
    items: list[dict] = []
    for i in range(10):
        items.append({"role": "user", "content": f"大{i}"})
        items.append({"role": "assistant", "content": f"回大{i}"})
    items += _rounds(2)  # 最近 2 轮很小（每轮 2 token）
    # 共 204 token > 触发线 100；水位 40。
    # 逐轮算：留 4 轮 = 44 + 摘要 2 > 40；留 3 轮 = 24 + 2 = 26 ≤ 40 ⇒ 应留 3 轮。

    async def _go():
        return await C.compact_for_window(items)

    replacement = asyncio.run(_go())

    assert replacement is not None, "老段必须被压，不能因为最近几轮装得下就放弃"
    assert len(replacement) - 1 == 6, "应留 3 轮（2 小 + 1 大）：按水位尽量多留"
    assert replacement[-2:] == items[-2:], "最新那一轮原样保留"


def test_it_gives_up_when_one_round_alone_exceeds_the_target(tiny_budget, monkeypatch):
    """唯一该放弃的情形：**最新那一轮自己就超水位**。

    这时压它等于让模型看不到要处理的东西（本轮用户那句 / 最后一次工具结果），
    所以交给硬截断 —— 那是确定性兜底，不会失效。
    """
    monkeypatch.setattr(hb, "_item_tokens", lambda item: 100)  # 一条就超水位 40
    items = _rounds(3)

    async def _go():
        return await C.compact_for_window(items)

    assert asyncio.run(_go()) is None


def test_it_skips_when_the_gain_is_not_worth_a_model_call(tiny_budget, monkeypatch):
    """净收益太小就不压：省下的空间下一轮就长回来，而这次调用要付钱还要等。

    构造"差一点点就够"的位置：总量 120，水位 119 ⇒ 挪走 1 轮（20 token）、摘要占
    15，净收益只剩 5，低于最小收益 10。真实配置里水位（0.40）远低于触发线，走不到
    这个位置；这里把两者贴近，是为了把那条判断单独逼出来。
    """
    monkeypatch.setattr(C, "window_target", lambda: 119)
    monkeypatch.setattr(C, "_summary_max_chars", lambda chars: 15)
    monkeypatch.setattr(C, "_MIN_WINDOW_GAIN", 10)
    items = _rounds(6)  # 12 条 × 10 = 120 token（必须**大于**触发线 100）

    async def _go():
        return await C.compact_for_window(items)

    assert asyncio.run(_go()) is None, "净收益不足就不该多调一次模型"


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


# --------------------------------------------------------------------------- #
# 摘要必须带**步骤骨架**（2026-10-21，对照 `agent/心得.md` 的「多处理」一节）
#
# 心得原文："在上下文进行压缩的时候保留结构性摘要（即保留步骤编号，动作，关键结果和
# 是否成功）"。原来那条提示词只要求"需求与约束 / 已结论 / 进行到哪一步"——压完一段
# 二十步的任务，接手者知道"进行到哪一步"，却不知道前十九步做过什么、哪几步失败了。
# 后果很具体：摘要里没有"这条路走不通"的信息，模型只能把那条路再试一遍才知道。
#
# ⚠️ 为什么用"读提示词"这种结构断言：这条要求**只存在于提示词里**，没有任何运行时
# 行为能证明它 —— 摘要内容由模型自由生成，用例造不出稳定断言。所以钉住"指令里必须
# 有这四要素"，而不是钉住某一次摘要长什么样。
# --------------------------------------------------------------------------- #
def test_the_summary_prompt_demands_a_step_skeleton():
    prompt = C._SUMMARY_PROMPT

    # 2026-11-02 从"第 N 步"改成"动作 → 关键结果"：编号本身不承载信息，
    # 接手者要的是"做过什么、成没成"。八个小节里这一条落在「错误与修复」下。
    assert "动作 → 关键结果" in prompt, "没要求动作与结果成对 —— 摘要会退化成自由文本"
    assert "动作" in prompt
    assert "关键结果" in prompt
    assert "失败" in prompt and "未完成" in prompt, (
        "没要求保留失败/未完成的步骤 ⇒ 接手者会把走不通的路再试一遍"
    )


def test_the_summary_prompt_keeps_its_two_anti_fabrication_rules():
    """结构要求不能把原来两条硬约束挤掉：不编造、不评价。

    第一条尤其要紧 —— 摘要会被当成"很久以前确实发生过的事"塞进模型输入，编造一条
    步骤的后果是模型据此推断"这一步已经做过了"，然后跳过真正该做的事。
    """
    prompt = C._SUMMARY_PROMPT

    assert "不许编造" in prompt
    assert "结果未明" in prompt, "看不出成败时要允许它说不知道，而不是硬编一个成功"
    assert "不写建议" in prompt


def test_the_summary_prompt_still_caps_its_length():
    """上限必须留着：摘要是"更省"，不是"再存一份全文"。

    ⚠️ 2026-11-02 从固定 600 字改成**按被压段比例**（夹在 600~6000）。固定值必然
    在两头各错一次：被压段很短时 600 字是一条比原文还长的"摘要"（会被"必须更小"
    那条校验拒掉，白花一次调用），被压段很长时 600 字只能写成结论。
    """
    assert "{limit}" in C._SUMMARY_PROMPT, "提示词自己不许写死数字"
    assert C._summary_max_chars(100) == C._MIN_SUMMARY_CHARS, "短段要有下限"
    assert C._summary_max_chars(10_000) == 2_000, "按 1/5 取"
    assert C._summary_max_chars(10**7) == C._MAX_SUMMARY_CHARS, "长段要有上限"

    rendered = C._summary_system_prompt("", 1234)
    assert "{limit}" not in rendered and "1234" in rendered, "占位符必须被替换掉"
