"""可观测接入：把官方 Agents SDK 的链路交给 Langfuse（可选，不配则不产生任何 trace）。

⚠️ 2026-10-20 重写。旧版把 Langfuse 的 **LangChain** callback handler 挂到"执行图
的运行 config"（`configurable` / `callbacks`）里。删图之后那条路**没有任何读者** ——
官方 `Runner.run` 不认识这两个键，于是 Langfuse 从来没收到过 agent 链路的 trace，
而代码看起来是接上的（三条用例还在守那个死行为）。

现在的接法照官方文档（`langfuse.com/integrations/frameworks/openai-agents`）：
OpenInference 的 OTel 插桩 `OpenAIAgentsInstrumentor`。为什么是它、而不是自己写
一个 `TracingProcessor`——**这一点看过源码，不是猜的**：`_instrument()` 里

    if exclusive_processor:                      # 默认 True
        from agents import set_trace_processors
        set_trace_processors([OpenInferenceTracingProcessor(...)])
    else:
        from agents import add_trace_processor

也就是说它用官方原语**替换**当前处理器列表。由此得到两条硬结论：

1. **装了且配了 Langfuse** ⇒ SDK 默认那个"往 OpenAI 后端上报"的
   `BatchTraceProcessor` 被**替换掉**，不会重复上报、也不会把内容发给 OpenAI。
   我们**不需要**（也**不能**）再调 `set_tracing_disabled(True)` —— 那会让 SDK
   根本不产生 span，插桩收不到任何东西。
2. **没配** ⇒ 这里必须显式 `set_tracing_disabled(True)`。不做的话默认处理器还活着，
   它会拿环境里的 `OPENAI_API_KEY` 往 api.openai.com 发 span（含 prompt 与回复
   全文）。本项目用自建 `AsyncOpenAI` 客户端、配置项叫 `AGNES_*`，所以那条上报
   要么失败被吞（白跑线程），要么用错 key 把对话内容发给第三方。

LangSmith 那一支**只覆盖还在用 LangChain 的脚本**（`scripts/eval_generation.py`
用 `ChatOpenAI.invoke`），agent 链路不进 LangSmith —— 它的 tracing 认的是
LangChain 的回调，与官方 SDK 不是一个体系。别把"配了 LANGCHAIN_API_KEY"读成
"agent 链路也有 trace"。
"""

import logging
import os

from . import config


def init_tracing() -> str:
    """按配置初始化 tracing，返回生效的后端名（"langfuse"/"langsmith"/"none"）。

    无参数：全部信息来自 app.config（进而来自环境变量）。

    副作用（三种，互斥）：
      - 没配任何后端 ⇒ `agents.set_tracing_disabled(True)`，**彻底不产生 trace**；
      - 配了 Langfuse ⇒ 设置 `LANGFUSE_*` 环境变量并插桩（处理器由插桩自己替换）；
      - 配了 LangSmith ⇒ 只写 `LANGCHAIN_*` 环境变量（对 agent 链路无效，见模块说明）。
    """
    if config.LANGSMITH_API_KEY:
        # LangChain 官方 tracing 只认这几个环境变量名（LANGCHAIN_* 为旧名，同义）。
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGCHAIN_API_KEY"] = config.LANGSMITH_API_KEY
        os.environ["LANGCHAIN_PROJECT"] = config.LANGCHAIN_PROJECT
        logging.info("tracing 已启用：LangSmith（project=%s）—— ⚠️ 只覆盖仍用 LangChain "
                     "的脚本，agent 链路不进 LangSmith", config.LANGCHAIN_PROJECT)
        return "langsmith"

    if config.LANGFUSE_PUBLIC_KEY and config.LANGFUSE_SECRET_KEY:
        if _enable_langfuse():
            logging.info("tracing 已启用：Langfuse（OpenInference OTel 插桩）")
            return "langfuse"
        logging.warning("Langfuse 已配置但初始化失败，本次不产生任何 trace")
        _disable_sdk_tracing()
        return "none"

    _disable_sdk_tracing()
    logging.info("tracing 未配置（Langfuse/LangSmith 均未填），本次不产生 trace")
    return "none"


def _enable_langfuse() -> bool:
    """按官方文档接 Langfuse：写环境变量 → OTel 插桩 → 构造客户端。

    ⚠️ **不做启动期连通性校验**（不调 `auth_check()`）：那是一次网络请求，而这里在
    启动路径上。凭据填错或 Langfuse 暂时不可达时，它会把启动拖住、甚至让服务起不来
    —— 可观测配错了绝不该影响可用性。凭据错的症状是 trace 发不出去，SDK 自己会打日志。

    返回 True 表示插桩成功；任何一步失败都返回 False（由调用方降级为"不产生 trace"）。
    **绝不抛** —— tracing 配错了不该让服务起不来。
    """
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", config.LANGFUSE_PUBLIC_KEY)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", config.LANGFUSE_SECRET_KEY)
    if config.LANGFUSE_HOST:
        # ⚠️ 官方文档的变量名是 `LANGFUSE_BASE_URL`；历史版本认 `LANGFUSE_HOST`。
        # 两个都写：前者给新 SDK，后者给还在读旧名的地方 —— 只写一个会造成
        # "配了 host 却仍然发往 cloud.langfuse.com"这种静默错发。
        os.environ.setdefault("LANGFUSE_BASE_URL", config.LANGFUSE_HOST)
        os.environ.setdefault("LANGFUSE_HOST", config.LANGFUSE_HOST)
    try:
        from langfuse import get_client  # type: ignore
        from openinference.instrumentation.openai_agents import (  # type: ignore
            OpenAIAgentsInstrumentor,
        )
    except Exception as e:  # noqa: BLE001  （没装包 = 没这层能力，不是故障）
        logging.warning("Langfuse 已配置但缺依赖（装 langfuse 与 "
                        "openinference-instrumentation-openai-agents）：%s", e)
        return False
    try:
        # 默认 exclusive_processor=True ⇒ 用它自己的 `set_trace_processors` 替换掉
        # SDK 默认的 OpenAI 后台上报（见模块 docstring 贴的那段源码）。
        OpenAIAgentsInstrumentor().instrument()
        # ⚠️ **不做 `get_client().auth_check()`**：那是一次网络请求，而这里在
        # **启动路径**上。凭据填错 / Langfuse 暂时不可达时，它要么把启动拖住几十秒，
        # 要么让服务起不来 —— 而"可观测"配错了绝不该影响服务可用性。凭据错的症状
        # 是 trace 发不出去，Langfuse SDK 自己会打日志。
        get_client()
        return True
    except Exception as e:  # noqa: BLE001
        logging.warning("Langfuse 插桩失败，本次不产生 trace：%s", e)
        return False


def _disable_sdk_tracing() -> None:
    """关掉官方 SDK 的 tracing —— 包括它默认那个往 OpenAI 后端上报的处理器。

    为什么必须有这一步：`get_trace_provider()` 在**第一次访问时**会自动注册
    `default_processor()`（`BatchTraceProcessor(BackendSpanExporter())`）。不关的话，
    每一轮对话的 prompt 与回复全文都会尝试发往 `api.openai.com`，用的还是环境里
    那个 `OPENAI_API_KEY`（可能是别的项目留下的）。失败会被 SDK 吞掉，所以它
    既不报错、也不体现在任何指标上。
    """
    try:
        from agents.tracing import set_tracing_disabled

        set_tracing_disabled(True)
    except Exception as e:  # noqa: BLE001  （没装 SDK 的极端场景：本来就没 trace）
        logging.debug("关闭 SDK tracing 失败（可忽略）：%s", e)
