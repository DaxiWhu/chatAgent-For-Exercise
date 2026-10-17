"""跨轮压缩的耗时指标（`agent_history_compaction_seconds`）守卫。

为什么这条缺口值得单独补
----------------------
压缩要**调一次模型做摘要**，这段耗时直接压在首 token 之前。但它在整体 P95 里
几乎看不见：压不压取决于"历史有没有超预算"，一次会话里只在某一轮发生。
于是"首 token 慢"这个现象里，唯一无法归因的就是它 —— 检索有分段耗时、
模型调用有 stage 计时、检查点有 op 计时，唯独这次摘要调用没有。

为什么必须断言"只记一次"
----------------------
本文件刻意不复用 `tests/test_history_compaction.py` 那种"直接替换 `summarize_items`"
的打桩方式 —— 那样整个函数体都不执行，测不到计时有没有接对。这里只替换
**client**，让真实的 `summarize_items` 跑起来。

计时段放在两条返回路径上（成功 / 异常），所以"同一次调用产出两条样本"
是这类写法最容易犯的错：它不会报错，只会让 `rate(..._count)` 悄悄翻倍，
所有拿它当分母的口径静默偏移一半。
"""

import time

import pytest

from app import metrics
from src import observability
from src.agents import compaction as C

SEGMENT = [{"role": "user", "content": "上一轮说过的话"}]


@pytest.fixture(autouse=True)
def wired_metrics():
    """接上真实的指标出口。

    ⚠️ 这一步不能省：`src/observability.py` 只是个转发层，**没注入 sink 时
    `histogram()` 是空操作**。不接就断言，读到的永远是 0 —— 那条断言看起来
    在验"计时有没有接对"，实际验的是"什么都没发生"（永远绿）。

    用真实的 `_MetricsSink`（而不是把 `observability.histogram` 换成一个列表）：
    换掉之后只能证明"函数被调用了"，证不了"指标名/标签真的落进注册表"。
    """
    from app.main import _MetricsSink

    saved = observability.get_sink()
    observability.set_sink(_MetricsSink())
    try:
        yield
    finally:
        observability.set_sink(saved)


class _FakeClient:
    """只实现 `chat.completions.create` 的最小替身。"""

    def __init__(self, content="摘要正文", delay=0.0, exc=None):
        self._content = content
        self._delay = delay
        self._exc = exc
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        if self._delay:
            time.sleep(self._delay)
        if self._exc is not None:
            raise self._exc
        message = type("_Msg", (), {"content": self._content})()
        choice = type("_Choice", (), {"message": message})()
        return type("_Resp", (), {"choices": [choice]})()


def _wire(monkeypatch, client):
    monkeypatch.setattr(C, "_client_or_none", lambda: client)


def test_successful_summary_records_elapsed_time(monkeypatch):
    _wire(monkeypatch, _FakeClient(delay=0.05))
    metrics.reset()

    assert C.summarize_items(SEGMENT) == "摘要正文"

    assert metrics.histogram_count(
        "agent_history_compaction_seconds", result="ok") == 1
    assert metrics.histogram_sum(
        "agent_history_compaction_seconds", result="ok") >= 0.05


def test_failed_summary_also_records_time(monkeypatch):
    """失败也记耗时。

    ⚠️ 只记成功样本会让分布里**恰好缺掉最长的那一截** —— 超时的那次正是
    "首 token 为什么慢"要找的答案。同时 `result=error` 这一档让"压得慢"与
    "压失败后白等一次超时"能分开看。
    """
    _wire(monkeypatch, _FakeClient(exc=TimeoutError("上游超时")))
    metrics.reset()

    assert C.summarize_items(SEGMENT) == ""

    assert metrics.histogram_count(
        "agent_history_compaction_seconds", result="error") == 1
    # 失败次数仍走原来的计数器（两条信号回答不同问题，不能互相替代）
    assert metrics.value(
        "agent_history_compaction_errors_total",
        stage="summarize", kind="TimeoutError") == 1


def test_each_call_produces_exactly_one_sample(monkeypatch):
    """一次调用只能产出一条样本（跨标签求和也必须是 1）。

    这条防的是"成功路径记一次、`finally` 里再记一次"这类写法：不报错、
    只是计数翻倍，而翻倍之后所有比率口径都偏一半 —— 且**没有任何指标能反映它**。
    """
    _wire(monkeypatch, _FakeClient())
    metrics.reset()
    C.summarize_items(SEGMENT)

    total = sum(
        metrics.histogram_count("agent_history_compaction_seconds", result=r)
        for r in ("ok", "error"))
    assert total == 1, f"一次调用产出了 {total} 条样本"


def test_no_client_records_nothing(monkeypatch):
    """没配模型能力时**不记**（不是记一个 0）。

    记 0 会把"这次没压"混进"压缩耗时接近 0"，系统性压低这个分布 ——
    与 `review_wait_seconds` 算不出等待时长时不上报是同一条纪律。
    """
    monkeypatch.setattr(C, "_client_or_none", lambda: None)
    metrics.reset()

    assert C.summarize_items(SEGMENT) == ""
    assert metrics.histogram_count(
        "agent_history_compaction_seconds", result="ok") == 0
    assert metrics.histogram_count(
        "agent_history_compaction_seconds", result="error") == 0


def test_empty_response_still_counts_as_a_completed_call(monkeypatch):
    """模型返回空内容时：耗时照记（调用确实发生了），但**不**当成失败耗时。

    这一档的意义是：`result=ok` 回答"这次出站花了多久"，
    `agent_history_compaction_errors_total{kind="EmptySummary"}` 回答"结果不可用"。
    两件事分开，才看得出"慢"与"坏"是不是同一批请求。
    """
    _wire(monkeypatch, _FakeClient(content="   "))
    metrics.reset()

    assert C.summarize_items(SEGMENT) == ""

    assert metrics.histogram_count(
        "agent_history_compaction_seconds", result="ok") == 1
    assert metrics.value(
        "agent_history_compaction_errors_total",
        stage="summarize", kind="EmptySummary") == 1
