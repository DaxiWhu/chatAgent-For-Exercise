"""SSE 解析器的单元测试（被测对象：`tests/test_e2e_stack.py::_read_sse_events`）。

为什么单独一份
--------------
`test_e2e_stack.py` 整份在 `E2E_BASE_URL` 为空时**跳过**（它要打真实运行中的栈），
所以放在那里测解析器等于永远不跑。而解析器本身是纯字符串逻辑，不需要栈。

它恰好是最容易写错的地方：服务端的 `done` 是 `yield {"event": "done"}`，**不带 data**。
按"有 data 才记一帧"的直觉写法，`done` 会被静默丢掉 —— 于是
`events[-1][0] == "done"` 这条判据永远为假，而报错信息会指向"流没正常收尾"，
把注意力全引到服务端。
"""

from __future__ import annotations

import importlib
import io

from tests import test_e2e_stack as e2e


def _stream(text: str) -> io.BytesIO:
    """把 SSE 文本包成"有 readline 的对象"，替掉真实 HTTPResponse。"""
    return io.BytesIO(text.encode("utf-8"))


# --------------------------------------------------------------------------
# ① 无 data 的事件不能丢
# --------------------------------------------------------------------------
def test_keeps_events_that_carry_no_data():
    """`done` 就是这种帧：只有 `event:` 行，没有 `data:` 行。"""
    events = e2e._read_sse_events(_stream("event: done\r\n\r\n"), timeout=1)

    assert events == [("done", "")], (
        f"解析结果 {events} —— 不带 data 的帧被丢了。"
        f"服务端的 `done` 正是这种形状，丢掉它会让『流正常收尾』永远判为假"
    )


def test_keeps_a_data_only_frame_under_the_default_event_name():
    """没有 `event:` 行时按 SSE 规范算 `message` —— 不能因为"没名字"就扔。"""
    events = e2e._read_sse_events(_stream("data: hello\r\n\r\n"), timeout=1)

    assert events == [("message", "hello")], f"实得 {events}"


# --------------------------------------------------------------------------
# ② 多行 data 要拼回来
# --------------------------------------------------------------------------
def test_reassembles_multiline_data():
    """一个 token 里带换行时，SSE 会拆成多条 `data:` 行 —— 拼回来才是原文。"""
    raw = "event: token\r\ndata: 第一行\r\ndata: 第二行\r\n\r\n"

    events = e2e._read_sse_events(_stream(raw), timeout=1)

    assert events == [("token", "第一行\n第二行")], f"实得 {events}"


# --------------------------------------------------------------------------
# ③ 心跳注释行不构成事件
# --------------------------------------------------------------------------
def test_comment_lines_do_not_become_events():
    """服务端在首 token 之前会发 `: ping` 保住连接，它不该被当成一帧。"""
    raw = ": ping\r\n\r\nevent: token\r\ndata: hi\r\n\r\n"

    events = e2e._read_sse_events(_stream(raw), timeout=1)

    assert [name for name, _ in events] == ["token"], f"实得 {events}"


# --------------------------------------------------------------------------
# ④ 一条完整流的读法：token 拼接 + 末帧是 done
# --------------------------------------------------------------------------
def test_a_normal_stream_ends_with_done_and_yields_text():
    raw = (
        "event: token\r\ndata: 收\r\n\r\n"
        "event: token\r\ndata: 到\r\n\r\n"
        "event: done\r\n\r\n"
    )

    events = e2e._read_sse_events(_stream(raw), timeout=1)

    assert "".join(d for n, d in events if n == "token") == "收到"
    assert events[-1][0] == "done", f"末帧是 {events[-1]!r}"


# --------------------------------------------------------------------------
# ⑤ 错误帧必须能被分辨出来（这是"假绿"的根源）
# --------------------------------------------------------------------------
def test_error_frames_are_distinguishable_from_normal_frames():
    """`error` 也是合法 SSE 帧 —— 判据必须能把它挑出来，否则"调用失败"就等于"通过"。"""
    raw = (
        "event: error\r\ndata: RuntimeError: relation \"agent_runs\" does not exist\r\n\r\n"
    )

    events = e2e._read_sse_events(_stream(raw), timeout=1)

    assert [name for name, _ in events] == ["error"]
    assert "agent_runs" in events[0][1], (
        "错误内容没被保留 —— 排查时就看不出是哪张表/哪个依赖出的问题"
    )


# --------------------------------------------------------------------------
# ⑥ 默认严格：放宽必须是显式动作
# --------------------------------------------------------------------------
def test_expect_llm_defaults_to_strict():
    """没设 `E2E_EXPECT_LLM` 时必须要求真模型。

    反过来说：如果默认是 0，那"链路跑通"这条判据在本地也永远不执行，
    改坏了也没人知道 —— 而它恰恰是这份 e2e 最该守住的东西。
    """
    assert e2e.EXPECT_LLM is True, (
        "默认值变成了『不要求真模型』—— 放宽必须是显式设置 E2E_EXPECT_LLM=0，"
        "不能靠默认值把用例悄悄废掉"
    )


def test_expect_llm_can_be_switched_off_explicitly(monkeypatch):
    monkeypatch.setenv("E2E_EXPECT_LLM", "0")
    try:
        importlib.reload(e2e)
        assert e2e.EXPECT_LLM is False, "显式设 0 之后仍然要求真模型 —— 这个开关没生效"
    finally:
        monkeypatch.delenv("E2E_EXPECT_LLM", raising=False)
        importlib.reload(e2e)  # 复原，别把 False 漏给后续用例


def test_the_llm_case_is_marked_skippable():
    """那条用例必须带 `skipif(not EXPECT_LLM)`。

    没有它时，无 Key 的栈上会直接**红**（error 帧）而不是 skip —— 于是会出现
    "为了让 CI 绿就把断言删掉"的压力，那比 skip 更糟。
    """
    marks = getattr(e2e.test_chat_run_completes_with_token_and_done, "pytestmark", [])
    names = {getattr(m, "name", "") for m in marks}
    assert "skipif" in names, (
        f"这条用例的 marker 是 {names} —— 缺 skipif 时无 Key 的栈会红，"
        f"而不是显示成 skipped"
    )
