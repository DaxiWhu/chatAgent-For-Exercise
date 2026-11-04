"""跨轮历史「压缩 + 裁剪」**两层拼在一起**的守卫（2026-11-01 新增）。

为什么单开一份
==============
这两层各自都有一整套测试：

* ``test_history_compaction.py`` —— 压缩单独拿出来调 ``compact_for_window``；
* ``test_agent_history_budget.py`` —— 裁剪单独拿出来看 ``trim_model_input`` 的输出。

但真正送进模型的是**串起来的结果**，而"串起来"这件事有自己的失效方式，两种都不报错：

1. **白压**：压缩把老的那段换成摘要，紧接着硬截断又把摘要当"最老的一条"丢掉
   ⇒ 花了一次模型调用，模型看到的东西与"压根没压缩"**一模一样**。
   单层测试发现不了：压缩那条会说"摘要生成了"，裁剪那条会说"裁到预算内了"。
2. **顺序反了**：先裁再压 ⇒ 被裁掉的那段根本没进摘要，信息**真的丢了**，
   而摘要看起来"生成成功"。

判据因此必须落在**最终那一份 items 的内容**上，不能停在"函数被调用了"。

⚠️ 关于"压缩后仍超预算"（2026-11-02 起这件事不会发生）
------------------------------------------------------------------
旧算法按"保留固定 16%"反推，压完是「摘要 + 16% 预算」，可能仍贴着甚至越过裁剪预算
（100% 预算），所以那时真有"压完还得裁"的一档。

现在窗口压缩的目标水位是 40%：替换结果是「摘要 + 保留段」且**按定义 ≤ 0.4 × 预算**，
而裁剪预算是 1.0 × 预算 —— 所以压完永远不可能再超裁剪预算。那条断言因此改成直接
构造一份带 system 摘要的输入，去守裁剪侧的锚点（``_trim_sync`` 的锚点 4），不再借
窗口压缩绕一圈。

另外守住一条分层边界：压缩是**可选优化**，它失败只该退化成"这轮没压"，
不能让裁剪跟着失效 —— 否则一个摘要超时就能让输入超窗、整轮答不出来。
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("agents", reason="openai-agents 未安装 ⇒ agent 链路会静默降级")

from agents.run_config import CallModelData, ModelInputData  # noqa: E402

from agents import Agent  # noqa: E402
from src.agents import compaction as C  # noqa: E402
from src.agents import history_budget as hb  # noqa: E402

#: 埋在被压段里的特征串。它是否活到最终输入，是"压缩有没有白做"的唯一标志。
FEATURE = "【特征串-只有被压掉的段里才有】"


def _rounds(n: int, *, size: int = 500) -> list[dict]:
    """n 轮 SDK 形态的历史，每轮 = 用户一条 + 助手一条，**每轮约 2000 token**。

    ⚠️ 单条为什么必须这么大：水位只有预算的 40%，而压缩要"留下最新一轮 + 一条摘要"
    才能成立。单条太小会让"一轮 + 摘要"直接顶穿水位 ⇒ 压缩判定"压不到"而一条不压，
    用例全部走不到压缩那条路径，而断言照样绿 —— 测了个寂寞。
    """
    out: list[dict] = []
    for i in range(n):
        out.append({"role": "user", "content": f"r{i}-问 " + "甲" * size})
        out.append({"role": "assistant", "content": f"r{i}-答 " + "乙" * size})
    return out


def _cd(items, instructions=None) -> CallModelData:
    """按官方形状构造过滤器入参（``CallModelData``，不是 ``ModelInputData``）。"""
    return CallModelData(
        model_data=ModelInputData(input=list(items), instructions=instructions),
        agent=Agent(name="probe"),
        context=None,
    )


def _trim(data):
    """跑一次**完整**的两层过滤器（压缩 → 裁剪），返回最终送模型的 items。"""
    return list(asyncio.run(hb.trim_model_input(data)).input or [])


def _text(item) -> str:
    return item.get("content", "") if isinstance(item, dict) else ""


def _joined(items) -> str:
    return "\n".join(_text(x) for x in items)


@pytest.fixture
def compact_on(monkeypatch):
    """打开压缩；保留轮数各用例自己设（它直接决定"有没有可压的段"）。"""
    monkeypatch.setenv("AGENT_HISTORY_COMPACT", "1")
    C.reset_compaction_state()
    hb.reset_history_budget_state()
    yield
    C.reset_compaction_state()
    hb.reset_history_budget_state()


@pytest.fixture
def stub_summary(monkeypatch):
    """替身摘要：只带被压段每条内容的**开头**，不抄全文。

    为什么要带开头：否则断言不了"交给摘要的确实是那一段"（常量摘要会让切错也看起来
    正常）。为什么不抄全文：摘要自己就会变成几千 token，两层的行为全被这一个数主导，
    测的就不再是分层了。
    """

    def _stub(segment, **kw):
        heads = [str(_text(x))[:16] for x in segment]
        return "【摘要】覆盖：" + " | ".join(heads)

    monkeypatch.setattr(C, "summarize_items", _stub)
    # 水位算法按 `_summary_max_chars`（被压段字数的 1/5，夹 600~6000）**预估**摘要
    # 会占多少。真实摘要确实可能到那个上限，但这里的桩只有两三百 token —— 不把估算
    # 拉到同一刻度，算出来的水位永远装不下"一轮 + 摘要"，压缩压根不会发生，而断言
    # 会误把裁剪钉住的 r0 原文当成摘要（"摘要里没有 r4"就是这么来的）。
    monkeypatch.setattr(C, "_summary_max_chars", lambda chars: 300)
    # 预算 6000 → 7000（2026-11-04）：水位 = 预算 × 0.40 = 2400，而"2 轮 × ~1000 +
    # 摘要估算 300"原本正好 2300 —— 加法里再算上注入抬头（~110 token）就 2410 > 2400，
    # 压缩判定"压不到水位"直接放弃，三个用例一起退化成"根本没压"。
    # ⚠️ 这里**不把抬头打桩为空**（不像 ``tiny_budget``）：这几条测的是最终送进模型的
    # 内容，抬头本来就该在里面 —— 打掉它等于把"摘要连抬头一起活下来"这件事也一起测掉。
    return _stub


# --------------------------------------------------------------------------- #
# ① 白压：压缩之后仍超预算，摘要必须活到最终输入
# --------------------------------------------------------------------------- #
def test_summary_survives_even_when_only_anchors_remain(monkeypatch, compact_on):
    """★ 硬截断不许把第一条 system 摘要当"最老的一条"丢掉。

    2026-11-02 起这条不再借窗口压缩来构造：窗口压缩的目标水位是 0.40，**压完必然
    小于预算**（保留段 + 摘要 ≤ 0.4 × 预算 < 预算），所以"压完仍超裁剪预算"这个
    场景在新算法下不可能出现。摘要真正的来源是别处 —— 历史压缩的投影、以及压缩
    被关掉/失败时留在历史里的那一条 —— 所以这里直接构造一份带摘要的输入。
    """
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "800")
    items = [{"role": "system", "content": "【摘要】覆盖：r0 | r1 | r2"}] + _rounds(4)

    final = _trim(_cd(items))

    assert final, "过滤器不该把输入清空"
    assert final[0].get("role") == "system", (
        f"摘要被裁掉了：最终第一条是 {final[0].get('role')!r}。"
        "模型看到的与没压缩时一样，而日志上写着'压缩成功'。"
    )
    assert "【摘要】" in _text(final[0])
    assert items[-1] in final, "最新一轮的助手回答必须原样在"


def test_compaction_replaces_the_old_segment_and_keeps_the_newest_round(
    monkeypatch, compact_on, stub_summary
):
    """正常比例下：老段被**一条摘要**替换，最新一轮原文仍在。

    这条盯"压缩真的发生了"，而不是"压缩 + 裁剪互相抵消、又回到了原文"。
    判据是原先的 ``r0-问`` 那条**不再逐字出现**（它只以摘要的形式存在）。
    """
    monkeypatch.setenv("AGENT_HISTORY_KEEP_ROUNDS", "2")
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "7000")
    items = _rounds(8)

    final = _trim(_cd(items))

    assert final[0].get("role") == "system"
    assert _text(final[0]).startswith(C._SUMMARY_PREAMBLE), (
        "抬头没活到最终输入：模型看到的是一段没有来源的摘要，会把几轮以前的待办"
        "当成这轮的要求去做（不报错）"
    )
    assert items[-1] in final and items[-2] in final
    assert items[0] not in final, "最早那轮原文应当已被摘要替换掉"
    assert "r0-问" in _text(final[0]) or "r0-" in _text(final[0]), (
        "最早那轮既没被摘要覆盖、也不在原文里 ⇒ 它凭空消失了"
    )


def test_nothing_is_lost_in_the_gap_between_the_two_layers(
    monkeypatch, compact_on, stub_summary
):
    """★ 特征串不能"两头都不在"：既不在摘要里、也不在保留段里 = 信息真的丢了。

    这是"白压"与"顺序反了"共同的结果，所以用同一条判据兜：无论它在摘要里还是在原文
    里都算活着，唯独不能彻底消失。
    """
    monkeypatch.setenv("AGENT_HISTORY_KEEP_ROUNDS", "2")
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "7000")
    items = _rounds(8)
    # 埋在第 5 轮 —— 在保留窗之外，必然被压。
    items[10]["content"] = FEATURE + items[10]["content"]

    final = _trim(_cd(items))

    assert FEATURE in _joined(final), (
        "特征串在最终输入里彻底消失：它被压进了摘要，而摘要没活下来。"
    )


# --------------------------------------------------------------------------- #
# ② 顺序：先压后裁
# --------------------------------------------------------------------------- #
def test_the_whole_old_segment_reaches_the_summary_before_trimming(
    monkeypatch, compact_on, stub_summary
):
    """★ 顺序判据：摘要必须覆盖**全部**被压段，而不只是"裁剩下的那点"。

    反过来（先裁再压）的表现是：最老那几轮在裁剪时就被丢了，根本轮不到进摘要，
    于是摘要里只剩靠近保留窗的一两轮 —— 中间几轮**既没进摘要、也不在最终输入里**，
    而每一步看起来都成功了。
    """
    monkeypatch.setenv("AGENT_HISTORY_KEEP_ROUNDS", "2")
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "7000")
    items = _rounds(8)

    final = _trim(_cd(items))
    summary = _text(final[0])

    assert "r0-问" in summary, f"最早那一轮没进摘要（摘要只覆盖了 {summary[:60]!r}）"
    assert "r4-问" in summary, "紧挨保留窗的那一轮也没进摘要 —— 被压段被截短了"


# --------------------------------------------------------------------------- #
# ③ 分层边界：压缩失败不许连累裁剪
# --------------------------------------------------------------------------- #
def test_summary_failure_still_lets_the_trim_do_its_job(monkeypatch, compact_on):
    """★ 摘要抛异常 ⇒ 只退化成"这轮没压"，裁剪照常生效。

    压缩挂在输入过滤器**里面**：这里如果不吞异常，一次摘要超时就会让整轮请求失败，
    而用户要的只是答案。
    """
    monkeypatch.setenv("AGENT_HISTORY_KEEP_ROUNDS", "2")
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "2000")

    def _boom(segment, **kw):
        raise RuntimeError("摘要服务挂了")

    monkeypatch.setattr(C, "summarize_items", _boom)
    items = _rounds(8)

    final = _trim(_cd(items))

    assert len(final) < len(items), "压缩失败后必须继续裁剪，否则输入照样超窗"
    assert items[-1] in final, "裁剪该保留的锚点不受压缩失败影响"


def test_empty_summary_falls_back_to_plain_trimming(monkeypatch, compact_on):
    """摘要返回空串（没配 Key / 模型返回空）⇒ 不做替换，直接走裁剪。

    "空摘要却照做替换"是这套机制最不能出的错：那等于把老历史删掉，且不可恢复。
    """
    monkeypatch.setenv("AGENT_HISTORY_KEEP_ROUNDS", "2")
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "2000")
    monkeypatch.setattr(C, "summarize_items", lambda segment, **kw: "")
    items = _rounds(8)

    final = _trim(_cd(items))

    assert all(x.get("role") != "system" for x in final), "空摘要不该产出 system 条目"
    assert len(final) < len(items)
    assert items[-1] in final
