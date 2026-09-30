"""可选的可观测接入：Langfuse / LangSmith。两者都留空则完全不介入。

为什么必须"可选且不硬依赖"
------------------------
本项目的可用性优先原则：**没配 tracing 时行为必须与没这层代码完全一致**。
所以这里所有第三方包都在函数内延迟导入，失败只打一条 warning，绝不抛。

两条路线的差别（决定了代码写法）
-----------------------------
- **LangSmith / LangChain tracing**：认环境变量 —— 只要 `LANGCHAIN_TRACING_V2=true`
  + `LANGCHAIN_API_KEY` 在进程环境里，LangChain/LangGraph 自己就上报，**不需要
  显式传 callback**。所以这里只负责"把配置写进 os.environ"。
- **Langfuse**：走 LangChain callback handler，需要把 handler 挂到 `graph.astream`
  的 config 里才会生效。老版本路径是 `langfuse.callback.CallbackHandler`，
  3.x 挪到了 `langfuse.langchain.CallbackHandler` —— 两个都试，谁在就用谁。
"""

import logging
import os

from . import config


def init_tracing() -> str:
    """按配置初始化 tracing，返回生效的后端名（"langsmith"/"langfuse"/"none"）。

    无参数：全部信息来自 app.config（进而来自环境变量）。

    副作用：LangSmith 分支会写 `os.environ`（这是它被 LangChain 识别的唯一方式）。
    Langfuse 分支不写环境变量，只把密钥留在 config 里供 `trace_callbacks()` 使用。
    """
    if config.LANGSMITH_API_KEY:
        # LangChain 官方 tracing 只认这几个环境变量名（LANGCHAIN_* 为旧名，同义）
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGCHAIN_API_KEY"] = config.LANGSMITH_API_KEY
        os.environ["LANGCHAIN_PROJECT"] = config.LANGCHAIN_PROJECT
        logging.info("tracing 已启用：LangSmith（project=%s）", config.LANGCHAIN_PROJECT)
        return "langsmith"

    if config.LANGFUSE_PUBLIC_KEY and config.LANGFUSE_SECRET_KEY:
        # 顺手把密钥塞进环境变量：langfuse SDK 自己也读它们，避免两处配置
        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", config.LANGFUSE_PUBLIC_KEY)
        os.environ.setdefault("LANGFUSE_SECRET_KEY", config.LANGFUSE_SECRET_KEY)
        if config.LANGFUSE_HOST:
            os.environ.setdefault("LANGFUSE_HOST", config.LANGFUSE_HOST)
        try:
            _langfuse_handler()
        except Exception as e:  # noqa: BLE001  （没装包 / 版本不符都不该影响服务）
            logging.warning("Langfuse 已配置但初始化失败，tracing 关闭：%s", e)
            return "none"
        logging.info("tracing 已启用：Langfuse")
        return "langfuse"

    logging.info("tracing 未配置（Langfuse/LangSmith 均未填），本次不上报链路")
    return "none"


def _langfuse_handler():
    """构造 Langfuse 的 LangChain callback handler，兼容 2.x / 3.x 两个导入路径。

    返回 handler 实例；两个路径都导入失败时抛 ImportError（由调用方降级）。
    """
    try:                                   # langfuse >= 3
        from langfuse.langchain import CallbackHandler  # type: ignore
    except Exception:                      # langfuse 2.x
        from langfuse.callback import CallbackHandler  # type: ignore
    return CallbackHandler()


def trace_callbacks() -> list:
    """返回要挂到图运行 config 上的 callback 列表（未配置则空列表）。

    LangSmith 路线返回空 —— 它靠环境变量自动上报，不需要 callback，
    重复挂反而会产生重复 trace。
    """
    if not (config.LANGFUSE_PUBLIC_KEY and config.LANGFUSE_SECRET_KEY):
        return []
    if config.LANGSMITH_API_KEY:
        return []                          # 走 LangSmith 时不同时挂 Langfuse，避免双份上报
    try:
        return [_langfuse_handler()]
    except Exception as e:  # noqa: BLE001
        logging.warning("Langfuse callback 构造失败，本次不挂：%s", e)
        return []


def build_run_config(base_config: dict, thread_id: str = "", tenant: str = "",
                     rag_tenant: str | None = None) -> dict:
    """给 langgraph 的运行 config 补上 tracing 回调、元数据与检索租户。

    base_config: 原始 config，形如 `{"configurable": {"thread_id": ...}}`。
    thread_id: 会话 id，写进 metadata，便于在 tracing 面板按会话过滤。
    tenant: 租户 id，同上（**仅用于可观测**）。
    rag_tenant: 交给检索层的租户。None = 不下发该键，检索节点沿用进程级 RAG_TENANT。
        与 tenant 分开是刻意的：tenant 是"这条 trace 属于谁"（可观测维度），
        rag_tenant 是"去哪个租户的库里查"（行为参数）。二者在 Key 绑定模式下相同，
        但在**自报租户**模式下必须不同 —— 详见 main._retrieval_tenant。

    返回**新的** dict（不就地改入参），带上：
      - callbacks：Langfuse handler（无则省略该键）；
      - metadata：app_version / release / thread_id / tenant —— 线上复盘时
        "这条 trace 属于哪个版本、哪个租户"是最先要看的两个维度；
      - configurable.rag_tenant：检索隔离用（仅当显式传入时）。
    """
    cfg = dict(base_config or {})
    cbs = trace_callbacks()
    if cbs:
        cfg["callbacks"] = list(cfg.get("callbacks") or []) + cbs
    meta = {
        "app_version": config.APP_VERSION,
        "release": config.APP_RELEASE,
        "thread_id": thread_id,
        "tenant": tenant,
    }
    cfg["metadata"] = {**(cfg.get("metadata") or {}), **{k: v for k, v in meta.items() if v}}
    if rag_tenant is not None:
        # 深拷一层 configurable：与 base_config 共享同一个 dict 时，
        # 后续任何写入都会"渗透"回调用方，这类隐式耦合极难排查。
        cfg["configurable"] = {**(cfg.get("configurable") or {}), "rag_tenant": rag_tenant}
    return cfg
