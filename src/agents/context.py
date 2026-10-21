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

赋值来源是**本次 run 的结果**，不经过任何 ``on_handoff`` 回调。本项目**不再**把它
持久化到任何地方（原先落进 LangGraph state 的 ``current_agent`` 字段，随图一起删了）：
一轮 = 一次 ``Runner.run``，每轮入口固定是 ``frontline_agent``，所以"上一轮结束在谁手上"
对下一轮没有意义 —— 它只在**本次运行内部**有意义，而本次运行内部官方已经有
``result.last_agent``。

⚠️ 曾经这里有过一个 ``channel`` 字段，由 ``on_handoff`` 回调写入。它只覆盖了"去程"，
回程没有对等的写入 ⇒ 一旦走过文学 handoff，之后每轮都会被拉回文学专家，回程边等于白接。
**不要再把跨轮状态放回上下文 / 回调** —— 状态只有一个来源：本次 run 结束在哪个 agent。

（2026-10-20：前台 → 文学专家重新走官方 ``handoffs=``，所以这条教训**现在更要紧**：
交接发生在一次运行内部，任何"把当前是谁记进上下文"的做法都会在换人后与
``result.last_agent`` 打架。跨轮状态只有一个来源：本次运行结束在哪个 agent。）

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

from .reflection import ReflectionState
from .steps import StepLog

__all__ = ["AgentStats", "TaskContext"]


@dataclass
class AgentStats:
    """一次 agent run 的**可上报统计**。

    为什么单独一个对象、而不是随手打日志：这些量就是"评估 agent 能力"的指标源
    （评审结论、返工率、护栏拦截、检索命中率）。日志只能事后 grep 单条请求，
    做不出比率。

    ⚠️ 这里**不再有** ``route`` / ``route_reason`` 两个字段：它们是"降级到另一条
    链路"的标签，而兜底链路已经随图一起删除（链路不可用时进程直接拒绝启动）。
    留着它们的后果是**指标永远报 unknown** —— 一个看起来在跑、实际不含任何信息的
    标签。

    ⚠️ ``review_rounds`` **保留**，但它数的**不是**图时代的分级评审轮次：它数的是
    "本轮评审工具被模型叫了几次"（见 ``evaluator.extract_evaluation``）。
    删掉它会让 ``agent_rework_total`` 永远为 0 —— 而"模型自己改了一轮再评"
    是唯一能反映自我返工的信号（``attempts`` 恒为 1，判不出返工）。
    """

    # -- 运行 --
    model_calls: int = 0
    tool_calls: int = 0
    agents: list[str] = field(default_factory=list)
    """参与过本轮调用的 agent 名（按首次出现顺序）。"""
    handoffs: list[tuple[str, str]] = field(default_factory=list)
    """(from, to) —— 每次 handoff 一条。"""
    last_score: str = ""
    """上一轮评审给出的结论档（``pass`` / ``revise`` / …）；没评审时是空串。"""

    review_rounds: int = 0
    """本轮**评审工具被模型叫了几次**。

    ⚠️ 不是"代码循环跑了几轮"（那个循环已经随评审改成 ``as_tool`` 一起删掉）。
    它现在的用途只有一个：``review_rounds > 1`` 说明模型改完又评了一次，
    这才是"自我返工"的唯一可观测信号 —— ``attempts`` 恒为 1，判不出来。
    """

    attempts: int = 0
    exit_reason: str = ""
    guardrail: str = ""
    duration_seconds: float = 0.0

    # -- 成本（2026-10-21）--
    input_tokens: int = 0
    """本次运行**主链路**累计的输入 token（``Runner`` 那几次模型调用相加）。"""
    output_tokens: int = 0
    """本次运行**主链路**累计的输出 token。"""

    bypass_tokens: dict[str, dict[str, int]] = field(default_factory=dict)
    """**旁路**模型调用的 token，按来源分开：``{"reflection": {"input":…, "output":…}}``。

    为什么必须单独记、不能直接累进上面两个字段
    ----------------------------------------
    上面两个字段的口径是"``Runner`` 循环里那几次模型调用"，它是 ``agent_run_tokens_total``
    的来源、也是"这个 agent 一圈多贵"的唯一答案。把旁路混进去，那条曲线就不再回答
    原先那个问题，而且**没有任何地方会显示它被污染了**。

    但"钱"这个口径必须含旁路 —— 反思闸门（``reflection._reflect_sync``）与窗口压缩
    （``compaction.summarize_items``）各自 bypass 建 client，账单上是实打实的。
    所以拆成两个口径、两处上报：

    * ``agent_run_tokens_total{kind}``       —— 主链路（口径不变）；
    * ``agent_bypass_tokens_total{kind,source}`` —— 旁路，按来源拆；
    * ``budget.used_of``                    —— **两者之和**（预算是钱，钱不分主次）。

    ⚠️ 按来源存而不是只存一个总数：压缩是长任务里最贵的一笔（它要把老历史整段读一遍），
    反思只是一小段证据。合成一个数就没法回答"钱到底花在哪一侧"。
    ⚠️ 后台反思可能在本轮**收尾之后**才回来（``_run`` 活得比主线久）⇒ 那一笔会写进
    这个字典，但已经没有地方读它了。属可接受损失（与"迟到 = 白跑"同一条），不要
    因此把它改成同步等待。
    """

    @property
    def bypass_input_tokens(self) -> int:
        """所有旁路调用的输入 token 合计。"""
        return sum(int(v.get("input", 0) or 0) for v in self.bypass_tokens.values())

    @property
    def bypass_output_tokens(self) -> int:
        """所有旁路调用的输出 token 合计。"""
        return sum(int(v.get("output", 0) or 0) for v in self.bypass_tokens.values())

    # -- 检索（agent 链路上的检索由 researcher 工具发起）--
    retrieval_calls: int = 0
    retrieved_chunks: int = 0
    empty_retrievals: int = 0

    # -- 产出 --
    assets: int = 0

    def as_dict(self) -> dict:
        """转成**普通 dict**（可 JSON 序列化、可写日志）。"""
        return {
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "agents": list(self.agents),
            "handoffs": [list(p) for p in self.handoffs],
            "last_score": self.last_score,
            "review_rounds": self.review_rounds,
            "attempts": self.attempts,
            "exit_reason": self.exit_reason,
            "guardrail": self.guardrail,
            "duration_seconds": round(self.duration_seconds, 6),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "bypass_tokens": {k: dict(v) for k, v in self.bypass_tokens.items()},
            "retrieval_calls": self.retrieval_calls,
            "retrieved_chunks": self.retrieved_chunks,
            "empty_retrievals": self.empty_retrievals,
            "assets": self.assets,
        }


@dataclass
class TaskContext:
    """一次任务 run 的共享状态（官方 ``TContext``）。"""
    tenant: str = ""
    """请求期租户。检索工具据此取对应分片的检索器。"""

    thread_id: str = ""
    """会话编号（= 跨轮历史那个 id）。

    只用于"任务执行中补充"这条链路：模型输入的注入口要按会话取增量
    （见 ``supplements``）。⚠️ 它**不是**跨轮状态 —— 跨轮状态只有会话历史一份，
    这里放它只是因为注入口拿不到 ``run_config``。
    """

    supplement_cursor: int = 0
    """本次运行开始时"补充"的水位（``supplements.cursor`` 的返回值）。

    为什么在上下文里、而不是在注入口现取：注入口每次模型调用都会跑一次，
    现取水位等于"这次调用之前的补充全算新的" ⇒ 同一条补充会在每一轮被重复注入。
    水位必须在**run 开始时**定格一次（见 ``runner.run_agent``）。
    """

    docs_dir: str = ""
    """语料根目录。出口护栏的第 2 层判据（来源文件是否存在）要用。"""

    goal: str = ""
    """**本次运行要解决的问题**（用户那句话，人工打回轮则是"按意见修改"）。

    为什么单独一个字段，而不是让反思自己去翻消息序列：旁路模型（``reflection``）拿到
    的是"有迹象的动作清单 + 计数"，**没有原始目标** ⇒ 它判 ``off_track`` 时其实不知道
    "题"是什么，只能靠工具名猜意图；即便判出来了，建议也只能是"换个做法"这种通用话，
    给不出"偏离了哪一句要求"。这不是模型能力问题，是输入里根本没有那个参照。

    ⚠️ 它**不是**跨轮状态：每轮由 ``runner`` 在运行开始时重写（人工打回那轮写的是意见，
    因为原话已经在会话里）。所以不要拿它当"整段对话的主题"，它只回答"这一次在干什么"。
    ⚠️ 也不进任何持久化：会话历史才是跨轮唯一来源，这里再存一份必然与它漂。
    """

    asset_dir: Path = field(default_factory=lambda: Path("var/agent_assets"))
    """落盘资产的目录（PPT 大纲渲染、插图占位文件）。"""

    retriever_factory: Callable[[], Any] | None = None
    """取检索器的**工厂**（不是实例）。

    刻意传工厂而不是实例：检索器是按租户懒加载 + 有界缓存的（见
    ``src/rag/pool.py`` 的 ``get_retriever``），在任务开始时就取等于把"可能用不上"
    的那次构建提前做掉。工厂让"真正调 kb_search 时才构建"这件事保持成立。
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

    started_at: float = 0.0
    """本次运行的**起点**（``time.monotonic()``；``0`` = 还没开始）。

    ``runner.run_agent`` 在 ``Runner.run`` 之前写一次，墙钟闸门
    （``deadline.check``）在注入口读它算"这次跑了多久"。

    ⚠️ 为什么不用 ``time.time()``：它是墙上时钟，NTP 校时或用户改系统时间会让它
    **倒退**，而"倒退"在闸门里的表现是"跑了两小时却判成还没到线" —— 一个静默失效。
    单调时钟只回答"过了多久"，正是这里唯一需要的语义。
    ⚠️ 恢复运行（等用户授权之后接着跑）会**重写**它：闸门的语义是"这一次运行最多
    多久"，不是"这个任务从提出到交付最多多久"（后者会把用户思考的时间也算进去）。
    """

    steps: StepLog = field(default_factory=StepLog)
    """本次运行的动作清单（工具名 / 参数指纹 / 成败 / 耗时）。

    ⚠️ 与 ``stats`` 一样**不进 LangGraph state**（不是可序列化类型），且
    **不跨 agent 节点** —— 一次请求里每个 agent 节点各建一份 ``TaskContext``。
    理由与边界见 ``steps`` 模块的 docstring。
    """

    reflection: ReflectionState = field(default_factory=ReflectionState)
    """反思闸门的运行内状态（待注入的结论、已触发 / 已注入几次、在飞任务的强引用）。

    ⚠️ 与 ``stats`` / ``steps`` 同一条：**不进 LangGraph state、不跨 agent 节点**。
    结论只注入**本节点**后续的模型调用 —— 理由见 ``reflection`` 模块 docstring 的
    边界 1（第四步的"退回某一步"也只能在本节点内做，两者边界一致最不容易出错）。
    """

    def record(self, kind: str, value: Any) -> None:
        """登记一项产出。幂等：同种产出只保留最新一次。"""
        self.delivered[kind] = value

    def recall(self, kind: str) -> Any:
        """取回一项产出；没有则返回 None。"""
        return self.delivered.get(kind)
