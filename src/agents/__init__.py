"""生产版 agent 链路（严格按 OpenAI Agents SDK 官方原语装配）。

    graph.dispatch ──[agent 链路可用]──> graph.frontline ──[转交判定]──> graph.literary
                                              │                              │
      ┌───────────────────────────────────────┘                              │
      ├── frontline_agent（**每轮入口固定是它**；唯一面向用户）               │
      │     ├── tools        : researcher / slides_maker / visual_artist （as_tool）
      │     │                  + review_draft                           （as_tool，评审）
      │     └── output_type  : FrontlineDecision（这一步是答复还是转交）—— 不再是 handoff
    ├── literary_writer（图里**独立的一个节点**，由上面那个判定路由过去；同样挂 review_draft）
    ├── session（跨轮历史，官方 Session 抽象 + 官方 SQLAlchemySession 后端，落 PostgreSQL）
      ├── run_config 上的出口护栏：citation_traceability + length
      │     （入口护栏 prompt_injection 挂在 frontline_agent 上，见 runner.build_run_config）
      └── 一次请求 = 每个节点各跑**一次** agent（转交那一轮两次）

⚠️ **本包在导入期会 import ``agents``（openai-agents SDK）。**

因此：

* ``src/graph.py`` **不在模块级** import 本包的任何子模块 —— 它走
  ``_agent_module()`` 惰性取，取不到就降级走原问答链路。
* 本文件也**不做任何 eager import** —— 上面那些公开名字是**惰性**的（PEP 562）。
  否则 ``from .agents import router`` 会顺带把 SDK 拉进来，SDK 一没装，
  ``src/graph.py`` 整个模块就导入失败，服务直接起不来。

这是"能力开关默认开、但**开着且没配**时行为必须与关一致"那条原则的落地方式：
没装 SDK ⇒ 全部请求走原问答链路，行为与改造前逐字节一致，无需运维额外配开关。
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "AGENT_HOOKS",
    "AgentStats",
    "EvaluationFeedback",
    "FrontlineDecision",
    "ImageBrief",
    "ResearchDigest",
    "SlideDeck",
    "TaskContext",
    "TaskOutcome",
    "build_model",
    "ENTRY_AGENT",
    "LITERARY_AGENT",
    "frontline_agent",
    "is_ready",
    "run_agent",
]

#: 公开名 → (子模块, 属性名)。惰性取值，避免导入本包就拉起整个 SDK。
_LAZY: dict[str, tuple[str, str]] = {
    "AGENT_HOOKS": ("hooks", "AGENT_HOOKS"),
    "AgentStats": ("context", "AgentStats"),
    "TaskContext": ("context", "TaskContext"),
    "is_ready": ("runtime", "is_ready"),
    "build_model": ("model", "build_model"),
    "run_agent": ("runner", "run_agent"),
    "ENTRY_AGENT": ("runner", "ENTRY_AGENT"),
    "LITERARY_AGENT": ("runner", "LITERARY_AGENT"),
    "TaskOutcome": ("runner", "TaskOutcome"),
    "frontline_agent": ("frontline", "frontline_agent"),
    "EvaluationFeedback": ("types", "EvaluationFeedback"),
    "FrontlineDecision": ("types", "FrontlineDecision"),
    "ImageBrief": ("types", "ImageBrief"),
    "ResearchDigest": ("types", "ResearchDigest"),
    "SlideDeck": ("types", "SlideDeck"),
}


def __getattr__(name: str) -> Any:  # pragma: no cover - 惰性取值，逻辑直白
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f".{target[0]}", __name__)
    return getattr(module, target[1])


def __dir__() -> list[str]:  # pragma: no cover
    return sorted(__all__)
