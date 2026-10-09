"""共享上下文对象 —— 官方 `Agent[TContext]` 里的那个 T。

对照官方 ``examples/customer_service/main.py`` 的 ``AirlineAgentContext``：

* 它是**可变对象**，同一个实例会穿过 handoff / ``as_tool`` / guardrail / 工具函数；
* ``on_handoff`` 回调往它里面写字段，下游工具再读它 —— 官方在 ``update_seat`` 里
  用 ``assert context.context.flight_number is not None``（注释写着
  "Ensure that the flight number has been set by the incoming handoff"）
  来钉死这一点。

⇒ 由此得到一条容易讲错的结论：**handoff 传递的是「上下文变更」，不是「任务结果」**。
任务结果走 messages（``RunResult.new_items``），上下文只承载"接下来该按什么前提办事"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["TaskContext"]


@dataclass
class TaskContext:
    """一次任务 run 的共享状态。"""

    user_id: str = "anonymous"
    tenant: str = ""

    channel: str = ""
    """当前接管对话的频道。由 ``on_handoff`` 写入（对照官方的 ``flight_number``）。"""

    assets: list[str] = field(default_factory=list)
    """已经落盘的资产路径（图片 / 渲染出的文件）。"""

    delivered: dict[str, Any] = field(default_factory=dict)
    """本轮已经产出过的结构化结果，按种类存放：``research`` / ``slides`` / ``illustration`` / ``literary``。"""

    notes: list[str] = field(default_factory=list)
    """跨 agent 的备注。"""

    def record(self, kind: str, value: Any) -> None:
        """登记一项产出。幂等：同种产出只保留最新一次。"""
        self.delivered[kind] = value

    def recall(self, kind: str) -> Any | None:
        return self.delivered.get(kind)
