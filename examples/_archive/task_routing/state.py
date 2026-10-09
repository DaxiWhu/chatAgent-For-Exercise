"""图状态：`OverAllState` 的字段定义。

与本项目既有 `src/graph.py::OverAllState` 的关系是**超集** —— 新增字段全部围绕路由：

| 字段 | 谁写 | 说明 |
|---|---|---|
| `route` / `route_plan` / `route_source` | `route_task` | 支路、策略对象、判定来源 |
| `retrieval_stats` | `retrieve` | `{hits, empty, degraded, elapsed_ms, context_chars}` |
| `critique_rounds` / `critiques` / `critique_converged` | `critique` | 写作支路的停止条件 |
| `code` / `exec_result` | `write_code` / `execute` | 代码支路 |
| `exit_reason` / `exit_verdict` | `finish` | 出口归因与出口判据结论 |

⚠️ 两条关于 state 的硬约束（本项目已有约定）：

1. **能 JSON 序列化的才准进 state** —— state 会被 checkpointer 持久化，放不可序列化
   对象会在**续跑时**才炸（离写入隔了一次重启，排查成本极高）。
2. **运行参数不进 state** —— 租户、request_id 这类应走 `config["configurable"]`；
   写进 state 会被持久化，续跑时可能带着**过期值**（见 `src/graph.py::retrieve` 的说明）。

⚠️ **`init` 刻意不清 `route_plan`**：plan 要**跨轮继承**（"再详细点"必须继承上一轮的
`needs.retrieval`）。langgraph 的节点返回是**合并**语义，不在 init 里清就自然保留了。
`init` 只复位真正的"每轮该归零"的量：`iteration` / `force_exit` / `exit_reason`。
"""
from __future__ import annotations

from langgraph.graph import MessagesState


class OverAllState(MessagesState):
    # ---- 路由（route_task 写）----
    route: str = ""              # "direct" / "rag" / "write" / "code"
    route_plan: dict = {}        # plan.py 的普通 dict，**必须可 JSON 序列化**
    route_source: str = ""       # "rule" / "cache" / "llm" / "inherit" / "escalate"

    # ---- 检索 ----
    context: str = ""
    citations: list = []
    citation_check: dict = {}
    retrieval_stats: dict = {}

    # ---- 生成 / 评审 ----
    output: str = ""
    iteration: int = 0
    score: int = 0
    feedback: str = ""
    feedback_source: str = ""    # "ai" / "critic" / "human"
    is_need_evaluate: bool = False

    # ---- 写作支路（信号来自 critic）----
    critique_rounds: int = 0
    critiques: list = []         # 每轮的 issues 列表，用于"连续两轮无新意见"
    critique_converged: bool = False

    # ---- 代码支路（信号来自执行）----
    code: str = ""
    exec_result: dict = {}       # {executed, passed, stdout, stderr}

    # ---- 人工 ----
    human_action: str = ""

    # ---- 出口 ----
    exit_reason: str = ""        # converged / budget_exhausted / not_needed / forced / rejected
    exit_verdict: dict = {}
    force_exit: bool = False
    escalated: bool = False
