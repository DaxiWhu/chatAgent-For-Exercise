"""agent 链路测试的共用夹具：假模型 / 假检索器 / 消息构造。

为什么单独一个文件：入口测试（``test_agents_route.py``）与整链测试
（``test_agents_pipeline.py``）都要用同一个 ``ScriptedModel`` —— 各写一份必然漂移。

文件名以 ``_`` 开头 ⇒ pytest 不会把它当测试文件收集。
"""

from __future__ import annotations

import json
from pathlib import Path

from agents.items import (  # noqa: F401  Usage 供调用方构造响应时使用
    ModelResponse,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
    Usage,
)
from agents.memory.session import SessionABC
from agents.models.interface import Model

from src.agents.context import TaskContext

__all__ = [
    "EmptyRetriever",
    "FakeRetriever",
    "FakeSession",
    "ScriptedModel",
    "call",
    "decide",
    "feedback",
    "handoff_to",
    "make_ctx",
    "say",
]


def say(text: str) -> ResponseOutputMessage:
    """一条纯文本助手消息。"""
    return ResponseOutputMessage(
        id="m1", type="message", role="assistant", status="completed",
        content=[ResponseOutputText(text=text, type="output_text", annotations=[], logprobs=[])],
    )


def call(name: str, arguments: dict, call_id: str) -> ResponseFunctionToolCall:
    """一次函数调用（工具调用与 handoff 在模型眼里是同一形态）。"""
    return ResponseFunctionToolCall(
        id=call_id, call_id=call_id, type="function_call",
        name=name, arguments=json.dumps(arguments, ensure_ascii=False),
    )


def feedback(text: str, score: str) -> ResponseOutputMessage:
    """评审 agent 的结构化产出（``EvaluationFeedback``）。"""
    return say(json.dumps({"feedback": text, "score": score}, ensure_ascii=False))


def decide(content: str) -> ResponseOutputMessage:
    """前台的一次答复。

    ⚠️ 2026-10-20：前台**没有** ``output_type`` 了（``FrontlineDecision`` 已删，
    交接改回官方 handoff），所以这里就是一条普通文本消息 —— 不再包 JSON。
    加回结构化产出会让 ``result.final_output`` 变成 pydantic 对象，而它会被当成
    给用户的答复交付出去（用户看到 ``action='reply' content='...'``）。
    """
    return say(content)


def handoff_to(target: str = "literary_writer") -> ResponseFunctionToolCall:
    """模型请求把对话交给另一个 agent（官方 handoff 在模型眼里就是一次函数调用）。

    工具名规则来自 SDK：``transfer_to_{agent.name}``（``Handoff.default_tool_name``，
    见 ``agents/handoffs/__init__.py``）。参数按官方 handoff 的约定是空对象 ——
    handoff 的载荷来自 ``Handoff.get_transfer_message``，不是模型给的参数。
    """
    return call(f"transfer_to_{target}", {}, f"h-{target}")


class ScriptedModel(Model):
    """按预设脚本逐次返回的模型；记录每次调用收到的**系统提示/工具表/handoff 表**。

    记录这些形参是刻意的：本组里好几条断言的对象不是"模型说了什么"，而是
    "模型**看到**了什么"（例如 handoff 到底以什么名字出现在工具位上）。
    """

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    @property
    def remaining_steps(self) -> int:
        return len(self._script)

    async def get_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        **kwargs,
    ) -> ModelResponse:
        from types import SimpleNamespace

        self.calls.append(SimpleNamespace(
            system_instructions=system_instructions or "",
            tools=list(tools or []),
            handoffs=list(handoffs or []),
            input=input,
        ))
        assert self._script, "脚本已用尽：实际发生的模型调用比预期更多"
        out = self._script.pop(0)
        items = out if isinstance(out, list) else [out]
        return ModelResponse(output=items, usage=Usage(), response_id=None)

    def assert_complete(self) -> None:
        assert not self._script, f"脚本还剩 {len(self._script)} 步没被消费"

    async def stream_response(self, *args, **kwargs):
        """官方 ``Model`` 是抽象基类，``stream_response`` 必须实现。

        本假模型只支持**非流式**路径 —— 这不是偷懒：生产链路用的就是
        ``Runner.run``（见 ``src/agents/runner.py`` 的说明），
        ``run_streamed`` 根本不会被走到。真被走到时立刻抛错，
        比"返回一个空流、让上层静静地拿到空答案"要好得多。
        """
        raise NotImplementedError(
            "测试用假模型只实现非流式路径（get_response）；"
            "生产链路也不使用 run_streamed"
        )
        yield  # pragma: no cover - 让它成为 async generator

    def tool_names(self, index: int = 0) -> set[str]:
        return {getattr(t, "name", "") for t in self.calls[index].tools}

    def handoff_names(self, index: int = 0) -> set[str]:
        return {getattr(h, "tool_name", "") for h in self.calls[index].handoffs}


class FakeRetriever:
    """假检索器：返回一条固定的命中，避免测试真的去跑语料/索引。"""

    CHUNK = {
        "source": "rate_limit.md",
        "source_path": "rate_limit.md",
        "chunk_id": "c1",
        "content": "限流采用加权滑动窗口：判定值 = 上一窗口计数 × 剩余比例 + 当前窗口计数。",
    }

    def search(self, query, top_k=3):
        return [(dict(self.CHUNK), 0.91)]


class EmptyRetriever:
    def search(self, query, top_k=3):
        return []


class FakeSession(SessionABC):
    """内存版跨轮会话，**只在测试里用**。

    为什么需要一个假的：本项目的会话后端是 PostgreSQL（官方 ``SQLAlchemySession``），
    而 CI 里没有 PG；官方也**没有**内存版会话（2026-10-03 核过 `agents/memory/`：
    SQLite / OpenAI 服务端那几种，都不是内存的）。所以按官方 ``SessionABC`` 的四个
    方法写一个 —— 语义与官方后端一致：追加、按序取回、弹出最后一条、清空。

    ⚠️ 它测的是**接线与语义**（"历史真的跨轮到了模型面前"），**不测** SQLAlchemy/PG
    本身 —— 那是官方后端的职责。PG 那侧只留一条"构造出来的确实是官方后端"的守卫。
    """

    def __init__(self, session_id: str = "T") -> None:
        self.session_id = session_id
        self.items: list[dict] = []
        self.session_settings = None

    async def get_items(self, limit: int | None = None):
        if limit is None or limit <= 0:
            return [dict(i) for i in self.items]
        return [dict(i) for i in self.items[-limit:]]

    async def add_items(self, items) -> None:
        self.items.extend(items)

    async def pop_item(self):
        return self.items.pop() if self.items else None

    async def clear_session(self) -> None:
        self.items.clear()


def make_ctx(tmp_path: Path, retriever=None, **kw) -> TaskContext:
    """构造一份上下文（默认接假检索器，引用校验第 2 层因 docs_dir 为空而跳过）。"""
    return TaskContext(
        docs_dir="",
        asset_dir=tmp_path / "assets",
        retriever_factory=(lambda: (retriever or FakeRetriever())),
        **kw,
    )
