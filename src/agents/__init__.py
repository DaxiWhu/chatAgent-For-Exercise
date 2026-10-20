"""生产版 agent 链路（严格按 OpenAI Agents SDK 官方原语装配）。

    Runner.run(frontline_agent, ..., session=...)   ← 一次运行，一个主循环
        │
        ├── frontline_agent（**每次运行的起始 agent**；唯一面向用户）
        │     ├── tools    : researcher / slides_maker / visual_artist （as_tool）
        │     │              + review_draft                       （as_tool，评审）
        │     └── handoffs : literary_writer  ← 官方 handoff，同一次运行内换人
        ├── literary_writer（接手后写完作品；同样挂 review_draft）
        ├── session（跨轮历史，官方 Session 抽象 + 官方 SQLAlchemySession 后端，落 PostgreSQL）
        └── run_config 上的出口护栏：citation_traceability + length
              （入口护栏 prompt_injection 挂在 frontline_agent 上，见 runner.build_run_config）

⚠️ 2026-10-20：不再有 LangGraph 节点。此前前台与文学专家是两个图节点、靠结构化
判定连边；现在整条链路就是**一次** ``Runner.run``，交接走官方 handoff，
``result.last_agent`` 回答"这次运行结束在谁手上"。理由见 ``frontline.py`` 的
模块 docstring。

⚠️ **本包在导入期会 import ``agents``（openai-agents SDK）。**

因此：

* 本文件**不做任何 eager import** —— 上面那些公开名字是**惰性**的（PEP 562）。
  这不是为了省导入耗时，而是为了让"缺 SDK"这件事**由一个地方报清楚**：
  `app.main._assert_agents_ready` 会在启动期调 `src.agents.runtime.configure()`，
  由它给出 `missing_sdk` / `missing_api_key` / `build_failed` 三档之一并**拒绝启动**。
  若这里 eager import，缺 SDK 时最先炸的会是 ``import src.agents`` 本身，
  报一个 ModuleNotFoundError 堆栈 —— 看不出"该装什么"。

⚠️ 2026-10-20 起这条链路是**唯一**的作答路径，**没有**"没装 SDK 就走原问答链路"
这种降级：SDK 缺失 = 进程起不来（守卫 `tests/test_startup_guard.py`）。
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "AGENT_HOOKS",
    "AgentStats",
    "EvaluationFeedback",
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
