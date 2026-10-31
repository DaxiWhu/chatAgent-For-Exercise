"""模型装配 —— 把**既有配置**装成官方 ``Model``。

为什么用 ``OpenAIChatCompletionsModel`` 而不是自己写一个 Model 适配器
------------------------------------------------------------------
主链路的模型是 LangChain 的 ``ChatOpenAI``（``app/main.py`` 的 ``_MODEL``）。
要把它塞进 Agents SDK，理论上可以写一个 ``Model`` 子类去桥接两边的消息格式
（``TResponseInputItem`` ↔ LangChain ``BaseMessage``），代价是：

* 要自己处理 tool_calls 的双向映射、流式事件、usage 统计 —— 每一处都是差异来源；
* 网关换一个（OpenAI 兼容 → 别的协议）就得再改一遍。

而本项目的主链路本来就走 **OpenAI 兼容网关**（``AGNES_API_BASE``），
``OpenAIChatCompletionsModel`` 正是为这个协议写的官方实现。所以这里读**同一份**
配置（同一个 base_url / api_key / model 名），走**官方**的请求构造 —— 两个链路
发出去的请求体一致，行为差异只可能来自编排，不会来自协议适配。

⇒ 结论：不写适配器。**同一份配置 + 官方实现** 比"自己桥接"可靠得多。

为什么还要套一层 ``TimedChatCompletionsModel``
-------------------------------------------
只为了可观测：给模型的**每一次**调用记耗时与失败。理由见该类的文档。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import replace
from typing import Any

from openai import AsyncOpenAI

from .. import observability

try:  # 官方把实现放在子模块，顶层导出名在不同版本间调整过
    from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
except ImportError:  # pragma: no cover - 兼容老/新版本的顶层导出
    from agents import OpenAIChatCompletionsModel  # type: ignore[attr-defined]

__all__ = [
    "DEFAULT_STRUCTURED_OUTPUT",
    "STRUCTURED_OUTPUT_MODES",
    "TimedChatCompletionsModel",
    "build_model",
    "structured_output_mode",
]


#: 传输层表达 ``output_type`` 的三种方式。
#:
#: * ``json_schema`` —— 官方原样：请求体带 ``response_format={"type":"json_schema",...}``。
#: * ``json_object`` —— 只声明"输出是 JSON 对象"，schema 改由 instructions 说明。
#: * ``none`` —— 什么都不声明，schema 同样只写在 instructions 里。
#:
#: **只改传输层，不改编排**：``output_type`` / ``final_output_as`` / SDK 侧的 JSON
#: 校验一律照旧（见 ``_degrade_structured_output`` 的说明）。
STRUCTURED_OUTPUT_MODES = ("json_schema", "json_object", "none")

DEFAULT_STRUCTURED_OUTPUT = "json_schema"


def structured_output_mode() -> str:
    """读 ``AGENT_STRUCTURED_OUTPUT``（缺省 = 官方原样 ``json_schema``）。

    为什么非法值要**抛**而不是回落
    -----------------------------
    回落的表现是"配置写错了，但服务照旧启动、专家照旧 400" —— 排查的人会盯着
    provider 看，而不是盯着这一行配置。抛出去则被 ``runtime.configure`` 收成
    ``build_failed``，启动时就把原因说清楚（见 ``app/main.py`` 的启动校验）。
    """
    raw = (os.getenv("AGENT_STRUCTURED_OUTPUT", "") or "").strip().lower()
    if not raw:
        return DEFAULT_STRUCTURED_OUTPUT
    if raw not in STRUCTURED_OUTPUT_MODES:
        raise ValueError(
            f"AGENT_STRUCTURED_OUTPUT={raw!r} 不认识；"
            f"可选：{' / '.join(STRUCTURED_OUTPUT_MODES)}"
        )
    return raw


def _output_format_note(output_schema: Any) -> str:
    """把 JSON Schema 写成一段 instructions。

    ``json_object`` / ``none`` 两档里，provider 拿不到 schema —— 模型只能从提示词
    知道要什么结构。三个要点缺一不可：

    1. **出现 "JSON" 字样**：``response_format={"type":"json_object"}`` 的官方要求
       是提示词里必须提到 JSON，否则部分实现直接 400。
    2. **"不再调用工具时"这个前提**：带 ``output_type`` 的 agent 第一轮往往要先调
       工具（如 ``researcher`` 先 ``kb_search``）。不写这句，模型会被这段格式要求
       诱导成"一上来就吐 JSON"，工具永远不调 —— 那比 400 更隐蔽。
    3. **禁止代码块围栏**：``json_object`` 保证"是 JSON 对象"，不保证"没有 ```json
       包裹"；而 SDK 侧是 ``json.loads(整段文本)``，多一层围栏就解析失败。
    """
    schema = json.dumps(output_schema.json_schema(), ensure_ascii=False, indent=2)
    return (
        "# 输出格式（只在给出最终结果时生效）\n"
        "不再调用工具、要给出最终结果时，**直接输出一个 JSON 对象**：\n"
        "- 不要 Markdown 代码块围栏（不要 ```json），不要任何解释性文字；\n"
        "- 字段与类型必须严格符合下面的 JSON Schema，缺字段或多字段都算错。\n"
        "\n"
        f"```json\n{schema}\n```\n"
    )


def _rebind_structured_output(args: tuple, kwargs: dict, *, instructions: Any, schema: Any,
                              settings: Any) -> tuple[tuple, dict]:
    """把改好的三个入参回填到调用参数里（位置/关键字两种传法都要接）。

    为什么按**位置下标**回填而不是改成显式签名
    ----------------------------------------
    SDK 内部两种传法并存：非流式全关键字（``run_loop.py`` 的 ``get_response``），
    流式前七个是**位置**参数（同一个文件里的 ``stream_response``）。显式签名会把
    位置传法钉死，SDK 加参数时子类直接 TypeError；``*args/**kwargs`` 则不会。
    代价是下标与官方签名顺序绑定 —— 由 ``tests/test_agent_structured_output.py``
    的签名守卫钉住（顺序一变立刻红）。
    """
    args = list(args)
    if args:
        args[0] = instructions
    if len(args) >= 3:
        args[2] = settings
    if len(args) >= 5:
        args[4] = schema
    for key, value in (
        ("system_instructions", instructions),
        ("output_schema", schema),
        ("model_settings", settings),
    ):
        if key in kwargs:
            kwargs[key] = value
    return tuple(args), kwargs


class TimedChatCompletionsModel(OpenAIChatCompletionsModel):
    """给官方模型的每次调用计时、计失败（其余行为逐一继承）。

    为什么必须包在 Model 这一层
    -------------------------
    ``Runner.run`` 在一次运行内会调用模型**多次**（每次工具调用一轮）。在
    ``runner`` 层只能拿到"整轮耗时"，而那个已经有了
    （``agent_run_duration_seconds``）—— 缺的正是"**单次**模型调用"这个粒度，
    它才是区分"模型慢"与"工具慢"的依据。官方 SDK 没有暴露逐次调用的 hook
    （``AgentHooks`` 只有 run / agent / tool 三级），Model 是唯一能拿到该粒度的
    扩展点。

    为什么是**继承**而不是代理
    ------------------------
    官方在多处做 ``isinstance(model, Model)`` 判定（``agents/agent.py`` 的
    ``_validate_model``、``agents/run_internal/turn_preparation.py``）——
    代理对象过不了这层判定，会**静默走 fallback 分支**（表现成"指标时有时无"，
    且没有任何报错）。继承还顺带保住了 ``isinstance(m, OpenAIChatCompletionsModel)``
    这类断言。

    两个方法与父类**保持同一调用语义**：``get_response`` 是协程、
    ``stream_response`` 是异步生成器。改语义（比如把 stream 变成返回 coroutine）
    会让 ``Runner`` 里的 ``async for`` 拿到一个 coroutine，报出与本意无关的错。
    """

    def __init__(
        self,
        *args: Any,
        structured_output: str = DEFAULT_STRUCTURED_OUTPUT,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if structured_output not in STRUCTURED_OUTPUT_MODES:
            raise ValueError(
                f"structured_output={structured_output!r} 不认识；"
                f"可选：{' / '.join(STRUCTURED_OUTPUT_MODES)}"
            )
        self.structured_output = structured_output
        if structured_output != DEFAULT_STRUCTURED_OUTPUT:
            # 降档是**能力妥协**，不是优化：它把"结构由 provider 强制"换成
            # "结构靠提示词 + SDK 侧校验兜"。启动时留一行日志，别让它只活在 .env 里。
            logging.warning(
                "结构化输出已降档：AGENT_STRUCTURED_OUTPUT=%s"
                "（provider 不支持 response_format=json_schema 时才该这么配）",
                structured_output,
            )

    def _degrade_structured_output(self, args: tuple, kwargs: dict) -> tuple[tuple, dict]:
        """按 ``structured_output`` 改写**这一次请求**的三个入参。

        为什么只改这一层就够（不碰 ``output_type``）
        ------------------------------------------
        SDK 拿到响应后，结构化解析走的是 ``turn_resolution.py`` 里的
        ``output_schema.validate_json(potential_final_output_text)`` —— 也就是
        **对文本做 JSON 解析 + pydantic 校验**，与请求体里发的是哪种
        ``response_format`` 毫无关系（源码已核对，见测试
        ``test_structured_output_survives_without_response_format``）。
        所以"换成 json_object"只是换一种把要求告诉模型的**编码方式**：
        ``output_type``、``final_output_as``、后续的校验与重试全部原样。

        剩下的唯一风险是"模型不知道 schema"，由 ``_output_format_note`` 补上。
        """
        if self.structured_output == DEFAULT_STRUCTURED_OUTPUT:
            return args, kwargs

        instructions = kwargs.get("system_instructions", args[0] if args else None)
        settings = kwargs.get("model_settings", args[2] if len(args) >= 3 else None)
        schema = kwargs.get("output_schema", args[4] if len(args) >= 5 else None)
        # 没有结构化产出（纯文本 agent / 工具调用轮）就什么都不用做。
        if schema is None or schema.is_plain_text() or settings is None:
            return args, kwargs

        note = _output_format_note(schema)
        merged = f"{instructions}\n\n{note}" if instructions else note
        new_settings = settings
        if self.structured_output == "json_object":
            extra = dict(getattr(settings, "extra_args", None) or {})
            extra["response_format"] = {"type": "json_object"}
            new_settings = replace(settings, extra_args=extra)
        return _rebind_structured_output(
            args, kwargs, instructions=merged, schema=None, settings=new_settings
        )

    async def get_response(self, *args, **kwargs):
        """非流式调用：包住整次请求。"""
        args, kwargs = self._degrade_structured_output(args, kwargs)
        t0 = time.perf_counter()
        try:
            return await super().get_response(*args, **kwargs)
        except Exception:
            observability.counter("llm_call_errors_total",
                                  stage="step_get", via="agent")
            raise
        finally:
            # 失败也记耗时：超时型故障的耗时正是要看的那个数。
            observability.histogram("llm_call_duration_seconds",
                                    time.perf_counter() - t0,
                                    stage="step_get", via="agent")

    async def stream_response(self, *args, **kwargs):
        """流式调用：计时起点在**进入**时，而不是第一次 yield 时。

        建连与首包都发生在 ``super()`` 的迭代里 —— 把 t0 放在第一次 yield 附近
        会把"上游迟迟不给第一个包"这段（最该被看见的那段）排除在耗时之外。
        """
        args, kwargs = self._degrade_structured_output(args, kwargs)
        t0 = time.perf_counter()
        try:
            async for event in super().stream_response(*args, **kwargs):
                yield event
        except Exception:
            observability.counter("llm_call_errors_total",
                                  stage="step_stream", via="agent")
            raise
        finally:
            observability.histogram("llm_call_duration_seconds",
                                    time.perf_counter() - t0,
                                    stage="step_stream", via="agent")


def build_model(
    *,
    model_name: str,
    api_key: str,
    base_url: str = "",
    timeout: float = 30.0,
    max_retries: int = 3,
    structured_output: str | None = None,
) -> Any:
    """构造一个官方 ``Model``（带逐次调用的耗时/失败观测）。

    model_name / api_key / base_url: 与主链路同一份配置（见 ``app/config.py``）。
    timeout: 单次调用超时（秒）。与主链路 ``ChatOpenAI(timeout=30)`` 对齐 ——
        两套超时不一致会导致"换了链路但没人知道超时变了"。
    max_retries: SDK 之下的 HTTP 重试次数。
    structured_output: 传输层怎么表达 ``output_type``（``json_schema`` /
        ``json_object`` / ``none``，见 ``STRUCTURED_OUTPUT_MODES``）。默认读
        ``AGENT_STRUCTURED_OUTPUT``；显式传参优先（测试与独立脚本用）。

    base_url 为空时**不传**该参数（让 openai 客户端用官方默认地址），
    而不是传空串 —— 传空串在某些版本里会被当成一个合法的相对地址，报出难懂的错。
    """
    kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout, "max_retries": max_retries}
    if base_url:
        kwargs["base_url"] = base_url
    client = AsyncOpenAI(**kwargs)
    return TimedChatCompletionsModel(
        model=model_name,
        openai_client=client,
        structured_output=structured_output or structured_output_mode(),
    )
