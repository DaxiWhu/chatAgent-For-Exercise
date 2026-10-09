"""共享上下文对象 —— 官方 ``Agent[TContext]`` 里的那个 T。

对照官方 ``examples/customer_service/main.py`` 的 ``AirlineAgentContext``：

* 它是**可变对象**，同一个实例穿过 handoff / ``as_tool`` / guardrail / 工具函数；
* ``on_handoff`` 回调往它里面写字段，下游再读 —— 官方在 ``update_seat`` 里用
  ``assert context.context.flight_number is not None``（注释写着
  "Ensure that the flight number has been set by the **incoming handoff**"）
  来钉死这一点。

⇒ 由此得到一条容易讲错的结论：**handoff 传递的是「上下文变更」，不是「任务结果」**。
任务结果走 messages（``RunResult.new_items``），上下文只承载"接下来该按什么前提办事"。

还有一条与之配套、同样容易混进来的边界：**"上一次是谁在接话"也不属于上下文**。
官方主循环把它放在一个局部变量上（``examples/customer_service/main.py``）——

    current_agent = result.last_agent

赋值来源是**本次 run 的结果**，不经过任何 ``on_handoff`` 回调。本项目为了跨副本/重启，
把它落进 LangGraph state 的 ``current_agent`` 字段（见 ``src/graph.py`` 的 ``frontline``
与 ``literary`` 两个节点，每个节点各写一次），但**仍由 result.last_agent 驱动**。

⚠️ 曾经这里有过一个 ``channel`` 字段，由 ``on_handoff`` 回调写入。它只覆盖了"去程"，
回程没有对等的写入 ⇒ 一旦走过文学 handoff，之后每轮都会被拉回文学专家，回程边等于白接。
**不要再把跨轮状态放回上下文 / 回调** —— 状态只有一个来源：本次 run 结束在哪个 agent。

（2026-10-03 起本项目**一处 handoff 都没有了** —— 跨节点转移改由图的条件边做，
见 ``src/agents/types.FrontlineDecision``。那条教训仍然成立：跨轮状态不进上下文。）

与示例版的差别（生产化必须的）
------------------------------
1. **检索器由外部注入**（``retriever_factory``），不在这里 import 产品代码 ——
   否则上下文对象就把整条 RAG 链路（含 qdrant/pg 客户端）拖进每一个构造点。
2. **引用累积**（``citations``）：``kb_search`` 每次命中都往这里追加，编号全局唯一。
   出口护栏（引用是否可追溯）与 SSE 的 ``citations`` 事件都读它。
3. **上游给的人工意见**（``human_feedback``）：``/v1/review`` 打回时，意见要能
   进到创作 agent 的上下文里；不放进 Context 就得靠改消息序列，那会破坏
   "消息序列 = 模型看到的东西"这条可复现性。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .steps import StepLog

__all__ = ["AgentStats", "CARRY_ACROSS_NODES", "TaskContext", "RUN_LOCAL", "stats_from_dict"]


@dataclass
class AgentStats:
    """一次 agent run 的**可上报统计**。

    为什么单独一个对象、而不是随手打日志：这些量就是"评估 agent 能力"的指标源
    （路由降级原因、评审轮次与结论、返工率、护栏拦截、检索命中率）。日志只能事后
    grep 单条请求，做不出比率。

    ⚠️ 它**不进 LangGraph 的 state** —— state 会被 checkpointer 序列化，
    dataclass 不是 msgpack 可序列化类型。`src/graph.py` 里写进 state 的是
    ``as_dict()`` 的**普通 dict**。
    """

    # -- 路由 --
    route: str = ""
    """最终走的链路：``agent`` / ``rag``。"""
    route_reason: str = ""
    """降级原因（指标标签，取值有限）；没降级时是 ``ready``。"""

    # -- 运行 --
    model_calls: int = 0
    tool_calls: int = 0
    agents: list[str] = field(default_factory=list)
    """参与过本轮调用的 agent 名（按首次出现顺序）。"""
    handoffs: list[tuple[str, str]] = field(default_factory=list)
    """(from, to) —— 每次 handoff 一条。"""
    review_rounds: int = 0
    last_score: str = ""
    attempts: int = 0
    exit_reason: str = ""
    guardrail: str = ""
    duration_seconds: float = 0.0

    # -- 检索（agent 链路上的检索由 researcher 工具发起）--
    retrieval_calls: int = 0
    retrieved_chunks: int = 0
    empty_retrievals: int = 0

    # -- 产出 --
    assets: int = 0

    def as_dict(self) -> dict:
        """转成**普通 dict**（可进 state / 可 JSON 序列化）。"""
        return {
            "route": self.route,
            "route_reason": self.route_reason,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "agents": list(self.agents),
            "handoffs": [list(p) for p in self.handoffs],
            "review_rounds": self.review_rounds,
            "last_score": self.last_score,
            "attempts": self.attempts,
            "exit_reason": self.exit_reason,
            "guardrail": self.guardrail,
            "duration_seconds": round(self.duration_seconds, 6),
            "retrieval_calls": self.retrieval_calls,
            "retrieved_chunks": self.retrieved_chunks,
            "empty_retrievals": self.empty_retrievals,
            "assets": self.assets,
        }


#: 跨 agent 节点要**延续**的统计字段。
#:
#: 2026-10-03 起一次请求可能跑**多个** agent 节点（前台 + 文学），而每个节点各建一份
#: ``TaskContext``（它们不是同一次 run，SDK 里没有共享上下文的办法）。而这些量是
#: **整轮**口径的 —— 模型调用了几次、哪些 agent 参与过、发生过哪些转移、检索了几次。
#: 不延续的表现是**静默少一半**：接口层的 ``agent_participants_total`` 只看见最后那个
#: agent，转交次数永远是 0，而日志里什么都看不出来。
CARRY_ACROSS_NODES = (
    "model_calls",
    "tool_calls",
    "agents",
    "handoffs",
    "review_rounds",
    "retrieval_calls",
    "retrieved_chunks",
    "empty_retrievals",
    "assets",
    "duration_seconds",
)

#: **只属于最近一次运行**的统计字段 —— 由当前节点覆盖写，刻意不延续。
#:
#: 延续它们的具体后果：前台那次运行判为转交（``exit_reason="transfer"``），文学节点
#: 如果延续了这个值，一次正常交付会被报成"转交"；``last_score`` 同理（上一段的评审
#: 结论会冒充这一段的）。
RUN_LOCAL = (
    "route",
    "route_reason",
    "last_score",
    "attempts",
    "exit_reason",
    "guardrail",
)


def stats_from_dict(raw: dict) -> AgentStats:
    """把 state 里的 ``agent_stats``（普通 dict）还原成 ``AgentStats``。

    只还原 ``CARRY_ACROSS_NODES`` 里那些 —— 其余字段留给当前节点自己覆盖写
    （理由见 ``RUN_LOCAL``）。

    ⚠️ 这两个常量合起来必须**恰好覆盖** ``AgentStats`` 的全部字段：
    ``tests/test_agents_pipeline.py`` 有一条断言专门钉它。将来给 ``AgentStats``
    加字段时，如果两边都没加，那条断言会红 —— 这正是它存在的理由（漏掉的后果是
    "某个量在第二个节点之后静默归零"，读代码看不出来）。
    """
    stats = AgentStats()
    for name in CARRY_ACROSS_NODES:
        if name not in raw:
            continue
        value = raw[name]
        if name == "agents":
            stats.agents = [str(x) for x in (value or [])]
        elif name == "handoffs":
            stats.handoffs = [
                (str(p[0]), str(p[1]))
                for p in (value or [])
                if isinstance(p, (list, tuple)) and len(p) == 2
            ]
        elif name == "duration_seconds":
            stats.duration_seconds = float(value or 0.0)
        else:
            setattr(stats, name, int(value or 0))
    return stats


@dataclass
class TaskContext:
    """一次任务 run 的共享状态（官方 ``TContext``）。"""
    tenant: str = ""
    """请求期租户。检索工具据此取对应分片的检索器。"""

    docs_dir: str = ""
    """语料根目录。出口护栏的第 2 层判据（来源文件是否存在）要用。"""

    asset_dir: Path = field(default_factory=lambda: Path("var/agent_assets"))
    """落盘资产的目录（PPT 大纲渲染、插图占位文件）。"""

    retriever_factory: Callable[[], Any] | None = None
    """取检索器的**工厂**（不是实例）。

    刻意传工厂而不是实例：检索器是按租户懒加载 + 有界缓存的（见
    ``graph.get_retriever``），在任务开始时就取等于把"可能用不上"的那次构建
    提前做掉。工厂让"真正调 kb_search 时才构建"这件事保持成立。
    """

    citations: list = field(default_factory=list)
    """本轮**真实检索到**的引用记录（``build_citations`` 的产物，编号全局唯一）。"""

    citations_text: str = field(default="")
    """已检索到的资料文本（累积）。用于可观测与调试，不进 state。"""

    human_feedback: str = ""
    """人工打回时的意见（``/v1/review`` 的 revise）。"""

    assets: list[str] = field(default_factory=list)
    """已经落盘的资产路径。"""

    delivered: dict[str, Any] = field(default_factory=dict)
    """本轮已产出的结构化结果，按种类存放：``research`` / ``slides`` / ``illustration``。"""

    notes: list[str] = field(default_factory=list)
    """跨 agent 的备注（排查用）。"""

    rendered: set[str] = field(default_factory=set)
    """已经做过的**确定性步骤**（幂等标记，如 ``"slides"``）。

    为什么要在上下文里存这个、而不是在调用点用局部变量：渲染大纲这类步骤会
    依据上下文里是否有 ``slides`` 产出决定做不做，而"是否已经渲染过"必须能跨
    多次调用被看到 —— 否则重复调用会产生重复资产与重复计数。
    """

    stats: AgentStats = field(default_factory=AgentStats)

    steps: StepLog = field(default_factory=StepLog)
    """本次运行的动作清单（工具名 / 参数指纹 / 成败 / 耗时）。

    ⚠️ 与 ``stats`` 一样**不进 LangGraph state**（不是可序列化类型），且
    **不跨 agent 节点** —— 一次请求里每个 agent 节点各建一份 ``TaskContext``。
    理由与边界见 ``steps`` 模块的 docstring。
    """

    def record(self, kind: str, value: Any) -> None:
        """登记一项产出。幂等：同种产出只保留最新一次。"""
        self.delivered[kind] = value

    def recall(self, kind: str) -> Any:
        """取回一项产出；没有则返回 None。"""
        return self.delivered.get(kind)
