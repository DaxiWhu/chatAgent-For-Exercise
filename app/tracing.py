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
   全文）。本项目用自建 `AsyncOpenAI` 客户端、配置项叫 `LLM_*`，所以那条上报
   要么失败被吞（白跑线程），要么用错 key 把对话内容发给第三方。

LangSmith 那一支（2026-10-30 起**已接入 agent 链路**）：用官方
`langsmith.integrations.openai_agents_sdk.OpenAIAgentsTracingProcessor`，通过
`set_trace_processors` 挂到 SDK 上。它与 Langfuse 的 OpenInference 插桩占用
**同一个点位**（都是替换处理器列表），二者互斥，`init_tracing()` 里 LangSmith 优先
—— 两套 key 都填的时候 Langfuse 分支不会执行。

同时仍然写 `LANGCHAIN_*` 环境变量：那部分是给还在用 LangChain 的脚本
（`scripts/eval_generation.py` 的 `ChatOpenAI.invoke`），与 agent 链路互不相干。
别把"配了 key"直接读成"agent 链路有 trace"，中间还差"处理器真的挂上了"这一步。

⚠️ 接入后仍然"看不到东西"的两个已知原因，都是**零报错**的：

  - SDK 全局 tracing 处于禁用状态 ⇒ 处理器一个 span 都收不到。所以
    `_enable_langsmith()` 必须显式解禁，理由见该函数说明。
  - 运行配置里没给 `group_id` / `trace_metadata` ⇒ trace 是有的，但同一会话的
    几轮串不起来、拿 request_id 搜不到。这两个字段在
    `src/agents/runner.py::build_run_config`。

接入或改动前先跑 `python scripts/trace_probe.py`（零成本、不发网络）确认转换层正常。

怎么让一条 trace 能被日志里的 request_id 反查到（2026-10-21）
--------------------------------------------------------------
用户报"这次答得不对"时，他手上只有响应头里的 request_id。要能拿它搜到 trace，
就得把 request_id 挂成 trace 上的属性。三条**看起来**都行、实际只有一条行的路
（都读过源码，不是猜的）：

1. `openinference.instrumentation.using_attributes(...)` —— **不行**。
   `OpenAIAgentsInstrumentor` 的 `on_trace_start` 只做一件事：
   `start_span(name=trace.name, attributes={OPENINFERENCE_SPAN_KIND: AGENT})`。
   全程没有读过 OpenInference 的 context 属性，也不转发 `trace.metadata`
   （`_processor.py` 里连 `metadata` 这个词都没出现）。
2. 官方 `RunConfig.tracing = TracingConfig(metadata={...})` —— **不行**。
   SDK 自己确实存了这份 metadata，但上面那条说明它不会被这个插桩导出。
3. Langfuse 的 `propagate_attributes(...)` —— **行**，官方文档在 Agents SDK 那一节
   用的就是它。原因在 `langfuse/_client/span_processor.py`：
   `on_start()` 里读 `_get_propagated_attributes_from_context(context)`，然后
   `_set_span_attributes_within_limit(span, propagated_attributes)` —— 这是挂在
   **TracerProvider 上的 SpanProcessor**，于是**任何**插桩产生的 span（包括
   OpenInference 的）只要在作用域内启动，都会被盖上这批属性。
   ⚠️ 它靠"span 启动时读 context"，所以作用域必须**包住整个运行**，不能事后补
   —— 事后补只影响之后新建的 span。

因此 `trace_scope()` 是唯一入口：包住一轮 `run_turn`，把 request_id / 会话 /
租户挂上去。没配后端时它退化成 `nullcontext()`，一个字节都不写。
"""

import contextlib
import logging
import os

from . import config

#: 生效的后端名（"langfuse" / "langsmith" / "none"），由 `init_tracing()` 设定。
#: `trace_scope()` 只认它 —— 不看环境变量，避免"启动时没启用、请求时又按环境变量
#: 走另一套判断"这种两处真相。
_ACTIVE_BACKEND = "none"


def init_tracing() -> str:
    """按配置初始化 tracing，返回生效的后端名（"langfuse"/"langsmith"/"none"）。

    无参数：全部信息来自 app.config（进而来自环境变量）。

    副作用（三种，互斥）：
      - 没配任何后端 ⇒ `agents.set_tracing_disabled(True)`，**彻底不产生 trace**；
      - 配了 LangSmith ⇒ 写 `LANGCHAIN_*` 环境变量，并用官方转换层处理器替换
        SDK 的处理器列表（agent 链路与仍走 LangChain 的脚本都有 trace）；
      - 配了 Langfuse ⇒ 设置 `LANGFUSE_*` 环境变量并插桩（处理器由插桩自己替换）。

    判定顺序是 LangSmith 在前：两套 key 都填时只有 LangSmith 生效。这是刻意的 ——
    两者占同一个点位（`set_trace_processors`），同时挂会互相覆盖，而"哪一个生效"
    必须可预测，不能取决于环境变量的字典顺序。

    同时把结论记进 `_ACTIVE_BACKEND`，供 `trace_scope()` 使用 —— 重复调用是安全的
    （每次都会重新判定并覆盖，测试里就是这么用的）。
    """
    global _ACTIVE_BACKEND

    if config.LANGSMITH_API_KEY:
        # LangChain 官方 tracing 只认这几个环境变量名（LANGCHAIN_* 为旧名，同义）。
        # 用 setdefault：环境里显式设过（比如 `LANGCHAIN_TRACING_V2=false`）就以
        # 环境为准，不在 import 期把它悄悄翻成 true。
        os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
        os.environ.setdefault("LANGCHAIN_API_KEY", config.LANGSMITH_API_KEY)
        os.environ.setdefault("LANGCHAIN_PROJECT", config.LANGCHAIN_PROJECT)
        if _enable_langsmith():
            _ACTIVE_BACKEND = "langsmith"
            logging.info("tracing 已启用：LangSmith（project=%s，agent 链路已挂处理器）",
                         config.LANGCHAIN_PROJECT)
            return "langsmith"
        logging.warning("LangSmith 已配置但 agent 链路接入失败，本次不产生 agent trace")
        _disable_sdk_tracing()
        _ACTIVE_BACKEND = "none"
        return "none"

    if config.LANGFUSE_PUBLIC_KEY and config.LANGFUSE_SECRET_KEY:
        if _enable_langfuse():
            _ACTIVE_BACKEND = "langfuse"
            logging.info("tracing 已启用：Langfuse（OpenInference OTel 插桩）")
            return "langfuse"
        logging.warning("Langfuse 已配置但初始化失败，本次不产生任何 trace")
        _disable_sdk_tracing()
        _ACTIVE_BACKEND = "none"
        return "none"

    _disable_sdk_tracing()
    _ACTIVE_BACKEND = "none"
    logging.info("tracing 未配置（Langfuse/LangSmith 均未填），本次不产生 trace")
    return "none"


def trace_scope(*, request_id: str = "", thread_id: str = "",
                tenant: str = ""):
    """给"接下来这一整轮运行"挂上可反查的属性（会话 / request_id / 租户）。

    request_id: 响应头里回给客户端的那个值 —— **它是用户唯一报得出来的标识**。
        不挂上去，``grep rid`` 出得来日志、却搜不到任何 trace，"这次答得不对"
        就只能按时间点大海捞针。
    thread_id: 会话 id。Langfuse 侧按 session 聚合，多轮对话才连得起来。
    tenant: 租户。作为 metadata 维度，便于按租户看成本与失败率。

    **必须包住整个运行**（进入作用域之后再调 `Runner.run`）：属性是在 span
    **启动时**由 Langfuse 的 SpanProcessor 读 context 盖上去的，事后补只影响之后
    新建的 span。理由与源码位置见模块 docstring。

    ⚠️ **这套机制只对 Langfuse 有效**，对 LangSmith 是空转（返回 `nullcontext()`）。
    LangSmith 的属性靠在**构造运行配置时**给字段（`group_id` 给会话、
    `trace_metadata` 给 request_id，见 `src/agents/runner.py::build_run_config`），
    与"包一层上下文"是两套独立机制。两套都配着的时候，两处都要有值才不会缺维度；
    只配 LangSmith 时，这个 `with` 块不产生任何效果 —— 也不会报错。

    没配后端 / 没装 `langfuse` 时返回 `nullcontext()` —— 调用点无需分支判断，
    也无需 try。
    """
    if _ACTIVE_BACKEND != "langfuse":
        return contextlib.nullcontext()
    metadata: dict[str, str] = {}
    if request_id:
        metadata["request_id"] = request_id
    if tenant:
        metadata["tenant"] = tenant
    if not (thread_id or metadata):
        return contextlib.nullcontext()
    try:
        from langfuse import propagate_attributes  # type: ignore
    except Exception as e:  # noqa: BLE001  （装了 instrumentor 但没装 langfuse：不产生 trace）
        logging.debug("没有 langfuse.propagate_attributes，本轮不挂 trace 属性：%s", e)
        return contextlib.nullcontext()
    try:
        # `session_id` / `metadata` 都有校验（US-ASCII、≤200 字符），不合规时
        # Langfuse 只打 warning 并丢弃该项，不会抛（`propagate_attributes` 的
        # docstring 明写"Invalid values are logged as warnings and dropped"）。
        # 所以这里不额外做字符集清洗 —— 清了反而与"日志里那个 rid"对不上。
        return propagate_attributes(session_id=thread_id or None,
                                    metadata=metadata or None)
    except Exception as e:  # noqa: BLE001  （tracing 配错不该打断请求）
        logging.debug("挂 trace 属性失败，本轮 trace 没有这些维度：%s", e)
        return contextlib.nullcontext()


def _enable_langsmith() -> bool:
    """把 agent 链路接到 LangSmith：挂官方转换层处理器。

    必须显式做两件事（都核过 SDK 源码，不是推断）：

    1. **替换**处理器列表。`set_trace_processors` 的语义就是"替换"：它会把 SDK
       默认那个 `BatchTraceProcessor(BackendSpanExporter())` 顶掉 —— 那个默认处理器
       会拿环境里的 `OPENAI_API_KEY` 把 prompt 与回复全文发往 `api.openai.com`，
       失败还会被静默吞掉。本项目用自建客户端、没有那个 key，留着它要么白跑线程，
       要么把对话内容送到不该去的地方。
    2. **显式解禁**。`set_trace_processors` **不只是替换、它不解禁**：两个函数
       各自改 provider 上的不同字段。只要本进程里先前走过"没配后端"那条分支
       （`_disable_sdk_tracing()`），SDK 就根本不产生 span —— 处理器挂得再对也
       收不到任何东西，症状只是"平台没有 trace"，零报错。

    顺序是"先替换、再解禁"：反过来的话，从解禁到替换之间默认处理器仍在位。
    这一瞬间没有运行、不会真发出去，但先替换没有代价，也不留窗口。

    与 Langfuse **互斥**：Langfuse 的 OpenInference 插桩内部同样替换这个列表，
    两者占用同一个点位。`init_tracing()` 里 LangSmith 在前，所以两套 key 都填时
    只有 LangSmith 生效。

    返回 True 表示已挂上；缺依赖或挂载失败返回 False（由调用方降级）。
    **绝不抛** —— 可观测配错了不该让服务起不来。
    """
    try:
        from langsmith.integrations.openai_agents_sdk import (  # type: ignore
            OpenAIAgentsTracingProcessor,
        )

        from agents import set_trace_processors, set_tracing_disabled
    except Exception as e:  # noqa: BLE001  （没装包 = 没这层能力，不是故障）
        logging.warning("LangSmith 已配置但缺依赖（装 langsmith[openai-agents]）：%s", e)
        return False
    try:
        # 不传 client：让它自己按 `LANGCHAIN_API_KEY` / `LANGSMITH_API_KEY` 造，
        # 上面那几个 setdefault 已经把值备好了。
        set_trace_processors([
            OpenAIAgentsTracingProcessor(project_name=config.LANGCHAIN_PROJECT),
        ])
        set_tracing_disabled(False)
        return True
    except Exception as e:  # noqa: BLE001
        logging.warning("LangSmith 接入失败，本次不产生 agent trace：%s", e)
        return False


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
