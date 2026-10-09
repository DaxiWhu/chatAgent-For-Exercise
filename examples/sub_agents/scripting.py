"""脚本构造辅助 —— 只为 ``agents.testing.ScriptedModel`` 服务。

官方 ``ScriptedModel`` 的每一步要求 ``output`` 是**序列**（``TResponseOutputItem`` 列表），
而 ``assistant_message()`` / ``function_call()`` 各自只返回一个 item，
所以这里包一层。除了这个包装，没有任何自造机制 —— 走的是官方 testing 原语。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agents.testing import ModelStep, assistant_message, function_call

__all__ = ["call", "say", "say_and_call"]


def say(text: str) -> ModelStep:
    """模型这一步只说话（没有 tool call ⇒ 本轮结束，即官方的 final output）。"""
    return ModelStep(output=[assistant_message(text)])


def call(name: str, arguments: Mapping[str, Any] | str, *, call_id: str) -> ModelStep:
    """模型这一步要求调用一个工具（或 handoff：handoff 底层也是工具）。"""
    return ModelStep(output=[function_call(name, arguments, call_id=call_id)])


def say_and_call(
    text: str,
    name: str,
    arguments: Mapping[str, Any] | str,
    *,
    call_id: str,
) -> ModelStep:
    """同一轮里既说话又要求调用工具 —— 官方明确这是合法形态（两者是同一次输出的两部分）。"""
    return ModelStep(
        output=[
            assistant_message(text),
            function_call(name, arguments, call_id=call_id),
        ]
    )
