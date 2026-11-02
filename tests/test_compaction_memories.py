"""压缩时顺手把记忆捞出来：解析契约 + 落库 + 不拖累压缩。

为什么这组必须存在
-----------------
"压缩时抽记忆"利用的是**那一刻原文还在手里**这件事。破掉之后的表现都不是报错：

* 记忆块没被切掉 ⇒ 那几行格式文本每轮都进上下文，白占位置、还教模型模仿;
* 切得太狠（把路径的方括号当类别）⇒ 路径丢了，剩下的半句话进了记忆表，
  以后每轮都被当成事实注入;
* 压缩因为记忆写失败而停下 ⇒ 上下文爆掉、请求 500，比少记几条严重得多。

所以这里把三件事分开守：**怎么切**、**切出来的怎么写进去**、**写不进去时压缩怎么办**。
"""

from __future__ import annotations

import asyncio

import pytest

import src.agents.compaction as C
from src.agents import memory
from src.agents.compaction_store import InMemoryCompactionStore
from src.agents.memory_store import InMemoryMemoryStore


class FakeSession:
    """最小会话后端。``_store`` 带出来是为了断言"记录里存的是纯正文"。"""

    def __init__(self, items=None):
        self.items = list(items or [])
        self.session_id = "s-mem"
        self._store = InMemoryCompactionStore()
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


def _rounds(n: int) -> list[dict]:
    out: list[dict] = []
    for i in range(n):
        out.append({"role": "user", "content": f"第{i}轮的问题，写得长一点好让 token 不为零"})
        out.append({"role": "assistant", "content": f"第{i}轮的回答，同样写长一点用来占位"})
    return out


@pytest.fixture
def memstore(monkeypatch):
    """记忆开关打开，并把存储换成内存实现（不碰真库）。"""
    monkeypatch.setenv("AGENT_MEMORY", "1")
    store = InMemoryMemoryStore()
    monkeypatch.setattr(memory, "_store", lambda: store)
    return store


@pytest.fixture
def plain_compact(monkeypatch):
    """只把压缩打开，记忆保持默认；给"解析/切分"这几条用。"""
    monkeypatch.setenv("AGENT_HISTORY_COMPACT", "1")
    monkeypatch.setenv("AGENT_HISTORY_KEEP_ROUNDS", "2")
    C.reset_compaction_state()
    yield
    C.reset_compaction_state()


def _reply_with(block: str, body: str = "摘要正文，写短一点免得比被压段还长。") -> str:
    return f"{body}\n<memories>\n{block}\n</memories>"


# --------------------------------------------------------------------------- #
# 一、怎么切
# --------------------------------------------------------------------------- #
def test_a_summary_without_a_block_is_used_verbatim(plain_compact):
    """模型没按格式输出 ⇒ 整段当正文。**不许因为格式不合规就把摘要扔掉**。"""
    text = "一、原始请求与意图：（没有记忆块的一段正常摘要）"

    body, candidates = C._split_summary(text)

    assert body == text
    assert candidates == []


def test_the_block_is_stripped_from_the_body(plain_compact):
    """正文里不许残留块 —— 它每轮都进上下文，留着等于白占位置。"""
    body, candidates = C._split_summary(
        _reply_with("- [preference] 日期用 DD/MM/YYYY"))

    assert "<memories>" not in body
    assert "DD/MM/YYYY" not in body
    assert body.startswith("摘要正文")
    assert candidates == [{"kind": "preference", "content": "日期用 DD/MM/YYYY"}]


def test_a_block_in_the_middle_does_not_eat_the_rest(plain_compact):
    """块后面的正文必须还在：模型的输出顺序会飘。"""
    text = "前半段\n<memories>\n- [project] 用 py3.13\n</memories>\n后半段"

    body, candidates = C._split_summary(text)

    assert "前半段" in body and "后半段" in body
    assert len(candidates) == 1


def test_an_unknown_kind_falls_back_to_project(plain_compact):
    """类别不认识就归 project：宁可分类不准，也别为一个词丢掉一条真事实。"""
    _, candidates = C._split_summary(_reply_with("- [nonsense] 这条是真的"))

    assert candidates == [{"kind": "project", "content": "这条是真的"}]


def test_a_line_without_a_kind_is_still_kept(plain_compact):
    """没写类别的行照样收 —— 内容比分类重要。"""
    _, candidates = C._split_summary(_reply_with("- 测试解释器是 langchain1.2"))

    assert candidates == [{"kind": "project", "content": "测试解释器是 langchain1.2"}]


def test_a_path_in_brackets_is_not_mistaken_for_a_kind(plain_compact):
    """★★ ``- [src/agent/loop.py] 改了重试逻辑`` 里的方括号是**路径**，不是类别。

    按"任意方括号"解析会把路径吃掉、只留下半句话 —— 而路径恰恰是记忆里最该留住的
    东西之一。判据是"方括号里是 3~12 个字母"，路径含 ``/`` 与 ``.`` ⇒ 不匹配。
    """
    _, candidates = C._split_summary(_reply_with("- [src/agent/loop.py] 改了重试逻辑"))

    assert candidates == [{"kind": "project", "content": "[src/agent/loop.py] 改了重试逻辑"}]


def test_non_list_lines_inside_the_block_are_skipped(plain_compact):
    """块里混进一句说明文字就跳过：它一旦成了记忆，每轮都会被当成事实注入。"""
    _, candidates = C._split_summary(
        _reply_with("下面是这次对话的要点\n- [project] 真的一条"))

    assert candidates == [{"kind": "project", "content": "真的一条"}]


def test_candidates_are_capped(plain_compact):
    """条数设硬顶：候选是模型顺手写的，"有多少收多少"会直接抬升每轮的固定成本。"""
    block = "\n".join(f"- [project] 第{i}条事实" for i in range(30))

    _, candidates = C._split_summary(_reply_with(block))

    assert len(candidates) == C._MAX_CANDIDATES


def test_a_block_without_a_body_yields_an_empty_body(plain_compact):
    """只剩记忆块 ⇒ 正文为空。调用方据此放弃这次压缩（摘要失败了，候选也不可信）。"""
    body, candidates = C._split_summary("<memories>\n- [project] 孤零零一条\n</memories>")

    assert body == ""
    assert len(candidates) == 1


# --------------------------------------------------------------------------- #
# 二、切出来的怎么写进去
# --------------------------------------------------------------------------- #
def test_candidates_are_written_under_the_owner(memstore, monkeypatch):
    """有归属就写，且写在**这个人**名下。"""
    written = asyncio.run(C._absorb_candidates(
        [{"kind": "preference", "content": "日期用 DD/MM/YYYY"}],
        owner_key="alice", thread_id="t-1",
    ))

    assert written == 1
    rows = memstore.active("alice")
    assert [r["content"] for r in rows] == ["日期用 DD/MM/YYYY"]
    assert rows[0]["kind"] == "preference"
    assert rows[0]["source_thread"] == "t-1"
    assert memstore.active("bob") == []


def test_no_owner_means_no_write(memstore, monkeypatch):
    """★★ 归不到人就不写，**也不能**写进公共桶：别人的偏好会被当成这个人的。

    断言"**一次都没发出写入**"而不是"表里没有"：底层 ``remember_memory`` 自己也有
    一道 ``no_owner`` 检查，只看结果的话把上层这道删掉照样绿 —— 两道守卫互相掩护，
    删掉一道没人知道。
    """
    calls: list[dict] = []

    def _spy(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "id": "x"}

    monkeypatch.setattr(memory, "remember_memory", _spy)

    written = asyncio.run(C._absorb_candidates(
        [{"kind": "project", "content": "随便一条"}], owner_key=""))

    assert written == 0
    assert calls == [], "没有归属时根本不该发出写入"
    assert memstore.active("") == []


def test_memory_switch_off_means_no_write(memstore, monkeypatch):
    """关掉记忆 ⇒ 压缩照常，只是不捞。同样断言"没发出写入"，理由同上。"""
    monkeypatch.setenv("AGENT_MEMORY", "0")
    calls: list[dict] = []

    def _spy(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "id": "x"}

    monkeypatch.setattr(memory, "remember_memory", _spy)

    written = asyncio.run(C._absorb_candidates(
        [{"kind": "project", "content": "随便一条"}], owner_key="alice"))

    assert written == 0
    assert calls == []
    assert memstore.active("alice") == []


def test_a_repeated_candidate_is_refreshed_not_duplicated(memstore):
    """同一段历史被抽两次（边界外推失败重试）不该长出两行。"""
    c = [{"kind": "project", "content": "测试解释器是 langchain1.2"}]

    asyncio.run(C._absorb_candidates(c, owner_key="alice"))
    asyncio.run(C._absorb_candidates(c, owner_key="alice"))

    assert len(memstore.active("alice")) == 1


# --------------------------------------------------------------------------- #
# 三、压缩本身不能因为记忆而停
# --------------------------------------------------------------------------- #
def _wire(monkeypatch, reply: str) -> FakeSession:
    async def _fake(segment, **kwargs):
        return reply

    monkeypatch.setattr(C, "_summarize_async", _fake)
    return FakeSession(_rounds(4))


def test_compaction_records_only_the_body(plain_compact, memstore, monkeypatch):
    """★★ 记录里存的必须是**纯正文**：它会每轮进上下文，块留在里面就是永久污染。

    顺带守"压缩照常发生"—— 一条正文 + 一个块，token 仍然远小于被压的 4 条。
    """
    session = _wire(monkeypatch, _reply_with(
        "- [preference] 日期用 DD/MM/YYYY", body="摘要正文，写短一点。"))

    async def _go():
        dropped = await C.compact_session_history(
            "s-mem", session=session, owner_key="alice")
        # 同一个事件循环里读记录：store 内部可能持有 loop 绑定的锁。
        return dropped, await session._store.latest_active("s-mem")

    dropped, record = asyncio.run(_go())

    assert dropped > 0
    assert record is not None
    assert "<memories>" not in record["summary"]
    assert record["summary"].startswith("摘要正文")
    assert [r["content"] for r in memstore.active("alice")] == ["日期用 DD/MM/YYYY"]


def test_a_failed_memory_write_does_not_stop_the_compaction(plain_compact, memstore, monkeypatch):
    """★★ 记忆写失败 ⇒ 记指标、丢掉这一批，**压缩必须照常完成**。

    判据反过来会怎样：压缩停下来是硬故障（上下文继续涨、请求最后 500），
    而少记几条记忆是软损失。不能拿硬故障去换软损失。
    """
    def _boom(**kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(memory, "remember_memory", _boom)
    session = _wire(monkeypatch, _reply_with("- [project] 一条会被丢掉的记忆"))

    async def _go():
        dropped = await C.compact_session_history(
            "s-mem", session=session, owner_key="alice")
        return dropped, await session._store.latest_active("s-mem")

    dropped, record = asyncio.run(_go())

    assert dropped > 0, "记忆写挂了不该让压缩失败"
    assert record is not None
    assert memstore.active("alice") == []


def test_window_compaction_strips_the_block_but_does_not_absorb(
        plain_compact, memstore, monkeypatch):
    """窗口压缩只剥离、不吸收：它的产物不落库，以后由历史压缩覆盖到那段时再收。

    在这里也吸一遍的后果是同一段每轮重抽一次，还会把那些记忆的"使用次数"抬起来。
    """
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "20")
    monkeypatch.setenv("AGENT_HISTORY_COMPACT_RATIO", "1.0")
    monkeypatch.setattr(C.history_budget, "_item_tokens", lambda item: 10)
    session = _wire(monkeypatch, _reply_with("- [project] 不该在这里落库"))

    replacement = asyncio.run(C.compact_for_window(session.items))

    assert replacement is not None
    assert "<memories>" not in replacement[0]["content"]
    assert memstore.active("alice") == []


# --------------------------------------------------------------------------- #
# 四、提示词侧
# --------------------------------------------------------------------------- #
def test_the_summary_prompt_asks_for_a_memory_block():
    """提示词里必须**明说**块的存在与上限，否则模型不会输出它（整条链路静默失效）。"""
    prompt = C._SUMMARY_PROMPT

    assert "<memories>" in prompt and "</memories>" in prompt
    assert "最多 10 条" in prompt
    assert "离开这段对话" in prompt, "判据必须写死在提示词里，不能让模型自己发明"
