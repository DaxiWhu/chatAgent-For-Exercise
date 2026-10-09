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

import time
from typing import Any

from openai import AsyncOpenAI

from .. import observability

try:  # 官方把实现放在子模块，顶层导出名在不同版本间调整过
    from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
except ImportError:  # pragma: no cover - 兼容老/新版本的顶层导出
    from agents import OpenAIChatCompletionsModel  # type: ignore[attr-defined]

__all__ = ["TimedChatCompletionsModel", "build_model"]


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

    async def get_response(self, *args, **kwargs):
        """非流式调用：包住整次请求。"""
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
) -> Any:
    """构造一个官方 ``Model``（带逐次调用的耗时/失败观测）。

    model_name / api_key / base_url: 与主链路同一份配置（见 ``app/config.py``）。
    timeout: 单次调用超时（秒）。与主链路 ``ChatOpenAI(timeout=30)`` 对齐 ——
        两套超时不一致会导致"换了链路但没人知道超时变了"。
    max_retries: SDK 之下的 HTTP 重试次数。

    base_url 为空时**不传**该参数（让 openai 客户端用官方默认地址），
    而不是传空串 —— 传空串在某些版本里会被当成一个合法的相对地址，报出难懂的错。
    """
    kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout, "max_retries": max_retries}
    if base_url:
        kwargs["base_url"] = base_url
    client = AsyncOpenAI(**kwargs)
    return TimedChatCompletionsModel(model=model_name, openai_client=client)
