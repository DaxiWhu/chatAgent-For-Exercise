"""结构化输出的**传输层降档** —— 改什么、以及为什么它不改变语义。

背景
----
三个 ``as_tool`` 专家（``researcher`` / ``slides_maker`` / ``visual_artist``）带
``output_type=<Pydantic 模型>``，SDK 因此发
``response_format={"type":"json_schema", ...}``。DeepSeek 这类 OpenAI 兼容网关回
``400 This response_format type is unavailable now`` —— 三个专家**静默全废**：
主链路照样"成功"，模型会道歉并给一个看起来合理的替代方案。

修法刻意只落在 Model 这一层（``AGENT_STRUCTURED_OUTPUT``），
**不动 ``output_type`` 这个官方原语**。依据是 SDK 的结构化解析路径：它拿响应文本去
``output_schema.validate_json(text)``（``run_internal/turn_resolution.py``），
与请求体里发的是哪种 ``response_format`` 无关。本文件最后那条端到端用例
（``test_structured_output_survives_without_response_format``）就是这个结论的证据 ——
它是整个修法成立的根基，删掉它等于让"降档仍然结构化"变成一句无根据的断言。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from agents.agent_output import AgentOutputSchema
from agents.models.interface import ModelTracing
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from agents import Agent, ModelSettings, RunConfig, Runner
from src.agents import model as AM
from src.agents.types import ResearchDigest

#: 一个合法的结构化产出（``ResearchDigest`` 的最小实例）。
_DIGEST = '{"question":"q","findings":[],"citations":[],"confidence":"low"}'


def _chat_body(content: str) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _model_with_capture(mode: str, seen: list[dict]) -> AM.TimedChatCompletionsModel:
    """造一个真模型，只把 HTTP 传输换成内存桩（其余全是官方实现）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_body(_DIGEST))

    client = AsyncOpenAI(
        api_key="sk-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return AM.TimedChatCompletionsModel(
        model="test-model", openai_client=client, structured_output=mode
    )


def _call(m: AM.TimedChatCompletionsModel, *, schema, system: str = "SYS") -> None:
    asyncio.run(
        m.get_response(
            system_instructions=system,
            input=[{"role": "user", "content": "hi"}],
            model_settings=ModelSettings(),
            tools=[],
            output_schema=schema,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )
    )


def _system_text(body: dict) -> str:
    for message in body["messages"]:
        if message["role"] == "system":
            return message["content"]
    return ""


# --------------------------------------------------------------------------- #
# 1. 默认档 = 官方原样（零回归）
# --------------------------------------------------------------------------- #
def test_default_mode_keeps_official_json_schema():
    """不配 ``AGENT_STRUCTURED_OUTPUT`` 时，请求体必须**逐字**是官方形态。

    这条守的是"修一个 provider 兼容问题，不要顺手改掉默认行为"：默认档由
    provider 强制结构，比提示词可靠；支持 json_schema 的网关（生产用的 Agnes）
    必须继续走这一档。
    """
    seen: list[dict] = []
    _call(_model_with_capture("json_schema", seen), schema=AgentOutputSchema(ResearchDigest))

    body = seen[0]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"]["properties"].keys() >= {
        "question",
        "findings",
    }
    # 默认档不得夹带 schema 说明：多一段提示词就是纯 token 开销，且会与
    # response_format 重复要求，可能诱发"先吐 JSON 不调工具"。
    assert "findings" not in _system_text(body)
    assert _system_text(body) == "SYS"


# --------------------------------------------------------------------------- #
# 2. json_object 档：换格式 + 补 schema
# --------------------------------------------------------------------------- #
def test_json_object_mode_downgrades_and_injects_schema():
    """``json_object`` 档：只声明"是 JSON"，字段要求靠 instructions 传达。

    两件事必须同时成立 —— 少了任一件，模型要么被 400 拒，要么吐出缺字段的 JSON
    （SDK 侧校验失败 → ``ModelBehaviorError``，表现为专家工具"调用失败"）。
    """
    seen: list[dict] = []
    _call(_model_with_capture("json_object", seen), schema=AgentOutputSchema(ResearchDigest))

    body = seen[0]
    assert body["response_format"] == {"type": "json_object"}
    system = _system_text(body)
    assert system.startswith("SYS")  # 原 instructions 一字不丢
    assert "findings" in system and "confidence" in system
    assert "JSON" in system


def test_none_mode_sends_no_response_format_key():
    """``none`` 档：请求体里**不出现** ``response_format`` 这个键。

    判据用"键不存在"而不是"值为 null"：`openai` SDK 对 ``omit`` 会剔除键，
    而某些网关对显式的 ``null`` 反而报 400 —— 两者看起来都是"没设"。
    """
    seen: list[dict] = []
    _call(_model_with_capture("none", seen), schema=AgentOutputSchema(ResearchDigest))

    body = seen[0]
    assert "response_format" not in body
    assert "findings" in _system_text(body)


def test_plain_text_agent_is_untouched_in_every_mode():
    """没有结构化产出的 agent（绝大多数调用）在三种档位下请求体一致。

    降档逻辑的触发条件是"这一次请求带结构化产出"。若按"配了哪个档"盲目注入，
    每轮对话都会被塞一段 JSON 说明 —— 那是最贵、也最容易让模型跑偏的回归。

    两种"没有结构化产出"的形态都要覆盖：``output_type`` 缺省（``None``）与
    ``output_type=str``（SDK 里 ``is_plain_text()`` 为真，同样走 ``omit``）。
    """
    for schema in (None, AgentOutputSchema(str)):
        bodies = []
        for mode in AM.STRUCTURED_OUTPUT_MODES:
            seen: list[dict] = []
            _call(_model_with_capture(mode, seen), schema=schema)
            bodies.append(seen[0])

        for body in bodies:
            assert "response_format" not in body
            assert _system_text(body) == "SYS"


# --------------------------------------------------------------------------- #
# 3. 根基：结构化解析不依赖 response_format
# --------------------------------------------------------------------------- #
def test_structured_output_survives_without_response_format():
    """``none`` 档下走完整 ``Runner``，``final_output`` 仍是 Pydantic 实例。

    这是整个修法成立的**唯一依据**：SDK 拿到文本后自己 ``validate_json``，
    所以"换一种告诉模型的编码方式"不会让结构化失效。若哪天 SDK 改成"只有
    response_format 是 json_schema 才解析"，这条会先红，提示修法失效。
    """
    seen: list[dict] = []
    model = _model_with_capture("none", seen)
    agent = Agent(name="probe", instructions="i", output_type=ResearchDigest, model=model)

    result = asyncio.run(
        Runner.run(agent, "hi", run_config=RunConfig(tracing_disabled=True))
    )

    assert isinstance(result.final_output, ResearchDigest)
    assert result.final_output.question == "q"
    assert "response_format" not in seen[0]


# --------------------------------------------------------------------------- #
# 4. 提示词必须拦住"一上来就吐 JSON"
# --------------------------------------------------------------------------- #
def test_format_note_keeps_tool_calling_turns_working():
    """schema 说明必须写明"只在不再调用工具时"生效。

    带 ``output_type`` 的专家通常第一轮就要调工具（researcher 先 ``kb_search``）。
    若提示词只说"输出 JSON"，模型会在工具轮直接吐一个空壳 JSON —— 请求成功、
    结构化成功、但**一次检索都没做**。那种失败比 400 隐蔽得多：看板上是绿的。
    """
    note = AM._output_format_note(AgentOutputSchema(ResearchDigest))
    assert "不再调用工具" in note
    # ``json_object`` 模式的官方要求：提示词里必须提到 JSON 字样。
    assert "JSON" in note


# --------------------------------------------------------------------------- #
# 5. 配置读取与签名守卫
# --------------------------------------------------------------------------- #
def test_unknown_mode_fails_loudly(monkeypatch):
    """写错档位名要**报错**，不能静默回落到默认档。

    静默回落的表现是"配置改了、专家还是 400" —— 排查的人会去查 provider，
    而不是查这一行。抛出去会被 ``runtime.configure`` 收成 ``build_failed``，
    启动即失败，原因写在启动报错里。
    """
    monkeypatch.setenv("AGENT_STRUCTURED_OUTPUT", "json_objcet")
    with pytest.raises(ValueError, match="AGENT_STRUCTURED_OUTPUT"):
        AM.structured_output_mode()
    with pytest.raises(ValueError):
        AM.build_model(model_name="m", api_key="k")


def test_mode_reads_env_and_explicit_arg_wins(monkeypatch):
    monkeypatch.setenv("AGENT_STRUCTURED_OUTPUT", "json_object")
    assert AM.structured_output_mode() == "json_object"
    assert AM.build_model(model_name="m", api_key="k").structured_output == "json_object"
    # 脚本/测试可以显式覆盖环境（评测脚本按 provider 选档就靠这个）。
    assert (
        AM.build_model(model_name="m", api_key="k", structured_output="none").structured_output
        == "none"
    )


def test_rebind_relies_on_official_parameter_order():
    """``_rebind_structured_output`` 按**位置下标**回填，下标必须与官方签名一致。

    为什么要用下标：SDK 流式路径是**位置传参**（``run_loop.py`` 的
    ``model.stream_response(filtered.instructions, filtered.input, ...)``），
    非流式是全关键字。只按关键字改会漏掉流式，表现是"非流式好了、流式照旧 400"。
    官方一旦调整参数顺序，这条立刻红，提示去看新签名。
    """
    for name in ("get_response", "stream_response"):
        params = list(getattr(OpenAIChatCompletionsModel, name).__code__.co_varnames)
        params = [p for p in params if p != "self"]
        assert params[0] == "system_instructions"
        assert params[2] == "model_settings"
        assert params[4] == "output_schema"


def test_stream_path_also_downgrades():
    """流式路径（位置传参）同样要被降档 —— 这是最容易漏的一半。"""

    def handler(request: httpx.Request) -> httpx.Response:
        capture.append(json.loads(request.content))
        events = [
            {
                "id": "x",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "m",
                "choices": [
                    {"index": 0, "delta": {"role": "assistant", "content": _DIGEST},
                     "finish_reason": None}
                ],
            },
            {
                "id": "x",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "m",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]
        payload = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
        return httpx.Response(
            200, content=payload.encode(), headers={"content-type": "text/event-stream"}
        )

    capture: list[dict] = []
    client = AsyncOpenAI(
        api_key="sk-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    m = AM.TimedChatCompletionsModel(
        model="m", openai_client=client, structured_output="json_object"
    )

    async def drain() -> None:
        # 前七个按官方流式路径**位置**传，模拟 Runner 的真实调用形态。
        async for _ in m.stream_response(
            "SYS",
            [{"role": "user", "content": "hi"}],
            ModelSettings(),
            [],
            AgentOutputSchema(ResearchDigest),
            [],
            ModelTracing.DISABLED,
        ):
            pass

    asyncio.run(drain())
    assert capture[0]["response_format"] == {"type": "json_object"}
    assert "findings" in _system_text(capture[0])
