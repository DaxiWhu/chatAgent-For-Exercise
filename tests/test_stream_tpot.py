"""chunk 间延迟（TPOT）的守卫。

为什么这条指标值得单独一组测试
----------------------------
它量的东西**在任何别的指标里都看不见**：首 token 很快、第一个字立刻出现，
然后卡住十几秒才继续 —— `llm_first_token_seconds` 只记一次（那时一切正常），
端到端耗时又被"回答有多长"主导。用户感受到的是"卡住了"，而监控上是全绿。

三条边界判据，每条都对应一个**会把数据搞坏**的错法：
  * 首个 chunk 不产生间隔（否则那条间隔其实是 TTFT，"首字等了多久"会被重复计）；
  * 换一次模型调用**必须**断开（否则工具执行的那几百毫秒到几十秒
    会被当成"解码变慢"，把 P95 抬到秒级）；
  * 一个 chunk 只记一次（否则 count 虚高，`histogram_quantile` 仍然能算，
    但"平均"类聚合会偏）。
"""

import asyncio

import pytest

from app import metrics


@pytest.fixture(autouse=True)
def _clean_metrics():
    """指标是进程级单例：不清就会把上一个用例的数字带进来。"""
    metrics.reset()
    yield
    metrics.reset()


class _FakeTurn:
    """按脚本吐字的替身运行（替换 `app.main.run_turn`）。

    chunks 里每一项是 `(content, call_id, delay_before)`：
      * `call_id` —— 官方 `ResponseTextDeltaEvent.item_id`。真实链路里**同一次
        模型调用内恒定、换调用就变**（实测见 `tmp_probe/probe_chunk_identity.py`），
        这也是"这一次模型调用"的唯一标记；
      * `delay_before` —— **交出这一段之前**睡多久，用来模拟解码间隔与工具执行。
    """

    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def __call__(self, req, *, context, on_text=None, trace_metadata=None):
        from src.agents.runner import TaskOutcome

        for content, call_id, delay in self._chunks:
            if delay:
                await asyncio.sleep(delay)      # 模拟"下一段还没来"
            if on_text is not None:
                await on_text(content, call_id)
        return TaskOutcome(answer="".join(c for c, _, _ in self._chunks), attempts=1,
                           final_agent="frontline_agent", passed=True,
                           exit_reason="passed")


def _run(monkeypatch, chunks, kind="chat"):
    """把替身运行装进 `app.main`，收完整个流，返回事件类型列表。"""
    import app.main as M

    monkeypatch.setattr(M, "run_turn", _FakeTurn(chunks))

    async def collect():
        return [ev async for ev in M._stream_run(
            M.TurnRequest(prompt="hi", thread_id="tpot"), kind)]

    return asyncio.run(collect())


def _stats(name="llm_inter_chunk_seconds", **labels):
    return metrics.histogram_count(name, **labels), metrics.histogram_sum(name, **labels)


# ---------------------------------------------------------------------------
# 1. 基本形态：N 个 chunk ⇒ N-1 次间隔
# ---------------------------------------------------------------------------
def test_interval_is_recorded_between_chunks_of_one_model_call(monkeypatch):
    """同一次模型调用的 3 个 chunk ⇒ 恰好 2 次间隔观测。

    为什么断言**次数**而不只断言"有值"：次数是唯一能证明"每个 chunk 各记一次、
    既没漏也没重复"的量。间隔本身被 sleep 的量化误差污染（Windows 上约 15ms），
    所以值只能给一个很松的下限。
    """
    events = _run(monkeypatch, [
        ("第一段", "lc_run--a", 0.0),
        ("第二段", "lc_run--a", 0.02),
        ("第三段", "lc_run--a", 0.02),
    ])
    assert [e["event"] for e in events] == ["token", "token", "token", "done"]

    count, total = _stats(kind="chat")
    assert count == 2, f"3 个 chunk 之间应有 2 次间隔，实际 {count}"
    assert total >= 0.02, f"两段各睡了 20ms，总和却只有 {total:.3f}s"


def test_a_single_chunk_produces_no_interval(monkeypatch):
    """只有一个 chunk（整段返回 / 一句话答完）⇒ 没有间隔可记。

    这条同时守住"不许把 TTFT 复制一份当间隔"这种错法 —— 那样每个请求都会
    在 `llm_inter_chunk_seconds` 里多出一条等于 TTFT 的观测，直方图的 P95
    会被首字等待时间主导。
    """
    _run(monkeypatch, [("只有一段", "lc_run--solo", 0.0)])
    assert _stats(kind="chat")[0] == 0


# ---------------------------------------------------------------------------
# 2. 边界：换一次模型调用必须断开
# ---------------------------------------------------------------------------
def test_a_new_model_call_breaks_the_interval(monkeypatch):
    """★ 核心判据：跨模型调用的那段等待**不是**解码耗时。

    场景就是 agent 链路里最常见的形态：模型 A 说完 ⇒ 执行工具（这里用 0.25s
    的 sleep 代表）⇒ 模型 B 接着说。如果把这段算成一次间隔，P95 直接变成
    "工具耗时"，从此这条指标只会反映工具执行慢，与解码速度无关。
    """
    _run(monkeypatch, [
        ("调用一", "lc_run--1", 0.0),
        ("调用二", "lc_run--2", 0.25),   # ← 中间是工具执行
        ("调用二续", "lc_run--2", 0.02),
    ])
    count, total = _stats(kind="chat")
    assert count == 1, f"只有后两个 chunk 属于同一次调用，应记 1 次，实际 {count}"
    assert total < 0.2, f"把 0.25s 的工具执行算进来了：总和 {total:.3f}s"


def test_a_new_item_id_breaks_the_interval_even_for_the_same_agent(monkeypatch):
    """同一个 agent 内的**两次模型调用**也要断开（`item_id` 变了就算换调用）。

    为什么单独一条：上面那条用"两个不同的 agent"来体现换调用，容易让人以为
    判据是"换了谁在说"。真实判据只有 `item_id` —— 同一个 agent 连续调两次模型
    （先检索再组织语言），中间那段等待同样不是解码耗时。
    """
    _run(monkeypatch, [
        ("先叫我查一下", "lc_run--1", 0.0),
        ("查完了，答案是", "lc_run--2", 0.2),
    ])
    assert _stats(kind="chat")[0] == 0


# ---------------------------------------------------------------------------
# 3. 口径：首字等待不许被算进第一段间隔
# ---------------------------------------------------------------------------
def test_the_first_interval_does_not_include_the_wait_for_the_first_chunk(monkeypatch):
    """首 chunk 等了 0.2s，第二 chunk 只隔 0.02s ⇒ 间隔应≈0.02，不是 0.2。

    这是"从上一个 chunk **到达**开始算"与"从请求开始算"的区别。搞错的实现
    会让每条间隔都拖着一个 TTFT 的尾巴，指标从此只反映"首字多慢"。
    """
    _run(monkeypatch, [
        ("首段", "lc_run--t", 0.2),
        ("次段", "lc_run--t", 0.02),
    ])
    count, total = _stats(kind="chat")
    assert count == 1
    assert total < 0.15, f"第一段间隔里带上了首字等待：{total:.3f}s"


# ---------------------------------------------------------------------------
# 4. 标签：分路径可查
# ---------------------------------------------------------------------------
def test_kind_label_separates_the_two_streaming_paths(monkeypatch):
    """`kind` 必须有：`chat` 与 `review` 的期望完全不同，混在一起没意义。"""
    _run(monkeypatch, [("一", "lc_run--k", 0.0), ("二", "lc_run--k", 0.01)],
         kind="review")
    assert _stats(kind="review")[0] == 1
    assert _stats(kind="chat")[0] == 0, "review 的观测不该落到 chat 上"
