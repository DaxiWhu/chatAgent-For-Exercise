"""兜底出口的判定（第 5 步）。

第 1 级只会"往模型输入里塞一条提醒"，第 2 级只会"把出问题那一步从模型看到的上下文里撤掉"。
两者都建立在**模型愿意改**这个前提上。模型不改（或根本撤不了）时，由这里决定这次的结果
**怎么交出去**。

为什么单独一个模块、而不写进 ``runner``：这里那张优先级表**本身就是规格**（七行，先命中先算），
它要能被逐行单测、逐行做变异验证； ``runner`` 已经够长，塞进去之后"哪一行被改坏了"根本看不出来。

⚠️ 两条最容易搞混的边界，先说清：

1. **停止 ≠ 不交东西**。系统类的问题（打转 / 卡住 / 轮次烧完…）要"停止"，但那次运行**已经产出的
   内容照交**，只是标明"这是部分结果"。把两件事揉成一个"轻档 / 重档"是错的 —— 它们是两条
   互相垂直的轴：**问题性质**（要人来审 = 业务类 / 要改代码 = 系统类）决定**该让谁来看**；
   **处置**（停止 / 转人工 / 交什么）是另一条，可以叠加。
2. **"没产出"指的是没有可交付的答复**（``answer`` 去空白后为空），不是"没有资产"。
   资产是答复的附件，不是替代品 —— 一段没有正文、只有附件的交付等于什么都没说。

判据的性质归属见 ``_CATEGORY`` 与 ``decide`` 的注释；口径来源见 ``PLAN_step5_fallback.md`` §4。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .. import observability

__all__ = [
    "AWAITING",
    "BUSINESS",
    "DELIVERED",
    "FAILED",
    "MANUAL",
    "MAX_UNFINISHED",
    "PARTIAL",
    "SYSTEM",
    "FallbackVerdict",
    "decide",
    "report",
]

MAX_UNFINISHED = 5
"""未完成清单最多列几条。

清单自己**不能变成新的膨胀源**：它要进审核队列、也可能进结构化返回。超出部分只报条数。
"""

DELIVERED = "delivered"
PARTIAL = "partial"
MANUAL = "manual"
FAILED = "failed"
AWAITING = "awaiting"
"""五档结局，与指标 ``agent_task_outcome_total{outcome}`` 的标签**同名同值**。

同名是有意的：看板上"任务完成率掉了"要能直接对到代码里的哪一个判定，中间不再过一次翻译。

``awaiting`` 是唯一**不是终态**的一档：这次运行停在"等用户授权某个高危动作"，
用户点头或拒绝之后还会**接着跑**（加固阶段三）。因此它**不算兜底**（见 ``is_fallback``）——
兜底是"机制没兜住"，而待授权是**正常运行被有意暂停**。
"""

BUSINESS = "business"
"""业务类：**要人来审**（内容、危险操作…）⇒ 转人工。"""

SYSTEM = "system"
"""系统类：**要改代码**（程序或环境坏了）⇒ 停止 + 给开发信号，不转人工。"""


@dataclass(frozen=True)
class FallbackVerdict:
    """一次 agent 运行的兜底结论。"""

    outcome: str
    """``delivered`` / ``partial`` / ``manual`` / ``failed`` / ``awaiting``。"""

    reason: str
    """为什么落到这一档（**有限枚举，做指标标签用**）。

    取值：``none`` / ``error`` / ``off_track`` / ``review_failed`` / ``guardrail`` /
    ``no_output`` / ``loop`` / ``stuck`` / ``max_turns`` / ``token_budget`` /
    ``guard_stop`` / ``tool_approval``。

    ⚠️ 必须保持有界。把它做成"把异常原文塞进去"会让指标基数爆炸
    （异常原文带 request id、上游返回体、可能含用户数据）—— 那是 ``TaskOutcome.error``
    的去处，它只进日志。
    """

    category: str
    """``business``（要人审）/ ``system``（要改代码）/ 空串（没有问题时）。

    它决定"该让谁来看"，**不**决定"交不交东西" —— 交付方式由 ``outcome`` 表达。
    """

    unfinished: list[str] = field(default_factory=list)
    """未完成清单（步骤清单里 ``ok`` 之外的条目），**有上限**。"""

    @property
    def is_fallback(self) -> bool:
        """有没有发生兜底（**正常交付**与**待授权**都不算）。

        ``awaiting`` 刻意排除在外：兜底意味着"机制没兜住、要人来收拾"，而待授权是
        正常运行被**有意**暂停。把它算进兜底，看板上的"兜底率"会随着用户点得慢而升高 ——
        那个数字就不再指示任何东西了。
        """
        return self.outcome not in (DELIVERED, AWAITING)

    def unfinished_text(self) -> str:
        """清单的一行文本（转人工与日志共用；空清单返回空串）。"""
        return "；".join(self.unfinished)


def decide(context: Any, *, exit_reason: str, answer: str) -> FallbackVerdict:
    """§4 那张优先级表的**唯一实现**（先命中先算，顺序即策略）。

    context: 本次运行的上下文（鸭子类型读 ``steps`` / ``reflection``，与 ``reflection``
        模块同一手法 —— 避免与 ``context`` 互相导入）。
    exit_reason: ``TaskOutcome.exit_reason``。
    answer: 这次运行产出的答复正文（``TaskOutcome.answer``）。

    ⚠️ 调用方必须先排掉"转交"那条路（``exit_reason == "transfer"``）：那一步**没有交付物**，
    它压根不是一次"产出"，拿它进表只会让每一次正常转交都落进兜底。

    ``awaiting_approval``（第 ⓪ 行）**进表**、且排在最前：待授权不是问题、也不是终态，
    但"这次运行是什么结局"必须只有一处回答 —— 表外再单判一次就是两套规则，改一处漏一处。
    """
    steps = _steps_of(context)
    state = getattr(context, "reflection", None)
    kinds = _kinds_of(state)
    stopped = getattr(state, "stopped", None)
    produced = bool((answer or "").strip())
    unfinished = _unfinished(steps)

    # ⓪ 待用户授权（加固阶段三）：**不是问题、也不是终态**。模型请求了一个高危动作
    # （落盘 / 生成图片），这一轮就地停下来等人点头；用户点头或拒绝之后还会接着跑。
    #
    # ⚠️ 排在最前是有意的：其他各行都在回答"这次结果**怎么交**"，只有它在回答
    # "这次**停在哪**、接下来还要接着跑"。若让它落到第⑤行（没有可交付的答复）——
    # 那正是"不接这一档"会走到的位置 —— 用户看到的是一条**错误报告**而不是"等你授权"。
    # 这不是推断：探针 P4 实测过（``tmp_probe/probe_tool_approval.py``）。
    if exit_reason == "awaiting_approval":
        return FallbackVerdict(AWAITING, "tool_approval", BUSINESS, unfinished)

    # ① 崩溃 / 限流 / 协议错：运行**根本没跑成**，谈不上"内容对错"。
    # 排在最前是有意的：这条路径没有草稿可审（产出为空），送进审核队列只会多出一条
    # "原因不明"的待审项。开发信号照发（见 ``run_agent`` 收尾处的 WARNING 与指标）。
    if exit_reason == "error":
        return FallbackVerdict(FAILED, "error", SYSTEM, unfinished)

    # ② 跑偏：**业务类**。方向做错的内容比不交更糟 —— 哪怕它写得再漂亮也不给用户，
    # 交给人工判断。⚠️ 判据用"报过跑偏"（累计）而不是"这次报的是跑偏"：跑偏过就必须有人看。
    if kinds.get("off_track"):
        return FallbackVerdict(MANUAL, "off_track", BUSINESS, unfinished)

    # ③ 评审结论**不进这张表**（2026-10-19 删掉的那一行：``review_failed`` ⇒ 转人工）。
    #
    # 为什么删：评审是模型自己调用的工具（``review_draft``，``as_tool``），它的结论
    # 是**过程内反馈** —— 工具把改进项返回给模型，模型在同一次运行里接着改。它改不改、
    # 改到什么时候收，由模型自己决定。把"评审没过"接进出口判定，等于模型只要评一次，
    # 收尾权就被代码接管；而用户明确要求的是「让模型自己决定是否收尾、是否启用评分」。
    #
    # ⚠️ ``exit_reason="review_failed"`` 这个值**仍然保留**：它描述"运行结束前最后一次
    # 评审的结论"，在 ``agent_runs_total{exit_reason}`` 上还能看出"多少比例是评了没过
    # 还照交的"。但它**不再改变交付方式** —— 有产出就正常交付，没产出才落第⑤行。
    # 想收紧这一点，正确的位置是模型自己的 instructions，不是这里。

    # ④ 护栏拦下的：**业务类** ⇒ 转人工。这里**有意偏离**官方"被拒的输出不落库"的语义 ——
    # 被拦下的那段内容会作为草稿存进审核队列，因为"这是不是误拦"没有原文根本判不了。
    # 草稿由 ``run_agent`` 收尾时写（入口护栏 = 用户那句请求；出口护栏 = 被拒的产出）。
    if exit_reason in ("input_guardrail", "output_guardrail"):
        return FallbackVerdict(MANUAL, "guardrail", BUSINESS, unfinished)

    # ⑤ 没有可交付的答复：**系统类**（停止 + 开发信号）。它包含三种情形，各自带上真原因，
    # 否则"兜底老在兜同一类事"与"偶发一次"在图上看不出来：
    #   · 第 3 级踩过刹车（`guard_stop`）—— 运行被打断，产出物根本来不及成形；
    #   · 轮次烧完（`max_turns`）—— ⚠️ 实测那一刻步骤清单可能**全是 ok**（模型在正常调工具、
    #     只是没收敛），所以这一路只能靠 `exit_reason` 判，不能只看清单；
    #   · token 预算烧穿（`token_budget`，2026-10-21）—— 同上：它是"别再花了"，
    #     不是"内容不对"，判据也只能靠 `exit_reason`；
    #   · 其余（跑完了、没报错、就是什么都没说出来）。
    if not produced:
        if stopped:
            return FallbackVerdict(FAILED, "guard_stop", SYSTEM, unfinished)
        if exit_reason == "max_turns":
            return FallbackVerdict(FAILED, "max_turns", SYSTEM, unfinished)
        if exit_reason == "token_budget":
            return FallbackVerdict(FAILED, "token_budget", SYSTEM, unfinished)
        return FallbackVerdict(FAILED, "no_output", SYSTEM, unfinished)

    # ⑥ 有答复，但**出现过问题信号** ⇒ 照常交付 + 标明"部分结果"。
    # "问题信号"只算两类：反思报过打转 / 卡住，或运行是被停下来的。
    # ⚠️ **单次工具失败不算**（决策 5：能继续跑就不管它）—— 否则一次网络抖动就把
    # 一次完全正常的交付标成"部分结果"，用户很快就不再相信这个提示。
    # 失败的步骤仍然进 `unfinished` 清单（要如实告知哪几步没做成），只是它不**触发**这一档。
    signal = _signal(kinds, stopped, exit_reason)
    if signal:
        return FallbackVerdict(PARTIAL, signal, SYSTEM, unfinished)

    # ⑦ 其余：正常交付。清单不带（没有要申报的东西，带一份空清单只会让前端多一种分支）。
    return FallbackVerdict(DELIVERED, "none", "", [])


def report(verdict: FallbackVerdict, *, exit_reason: str, agent: str) -> None:
    """上报结局与"给开发人员的信号"（**唯一**一处）。

    ``agent``: 本次运行在哪个 agent 节点上（``run_agent`` 传进来）。
    ⚠️ **必须带这个标签**：一次请求 = N 次运行（转交那一轮是前台 + 文学 2 次），
    少了它，按 ``outcome`` 聚合的看板会把"运行数"当"请求数"，阈值随之失真 ——
    而没有任何东西会变红。这是项目里对"每运行一次"类指标的硬约定
    （见 ``tests/test_agents_pipeline.py::test_each_run_reports_its_own_agent_label``）。

    为什么单独一个函数、由 ``runner`` 在判定后立刻调一次：上报点必须跟判定点一样只有一个。
    散成"``run_agent`` 里报一次、接口层再报一次"，第一个后果就是同一次运行被记两遍，
    看板上的完成率立刻失真。

    决策 10（"没产出、也没异常信号时开发人员需要知道"）落成：**一条 WARNING 日志
    + 一个有限标签的指标**。告警规则本身属第 7 步（收口），这里只落指标与日志。
    """
    observability.counter(
        "agent_task_outcome_total", outcome=verdict.outcome, agent=agent
    )
    if not verdict.is_fallback:
        return
    observability.counter("agent_fallback_total", reason=verdict.reason, agent=agent)
    tail = f"未完成：{verdict.unfinished_text()}" if verdict.unfinished else ""
    if verdict.category == SYSTEM:
        # 系统类的兜底意味着"要改代码"（机制没兜住模型）⇒ 必须让开发侧看到。
        logging.warning(
            "agent 兜底（系统类，需改代码）：原因=%s，exit_reason=%s。%s",
            verdict.reason, exit_reason, tail,
        )
        return
    # 业务类转人工是**正常业务**（等一个人来看），不该按告警处理，但要留痕：
    # "转人工特别多"本身是一个要看的信号（阈值定得太严、或者内容真的在变差）。
    logging.info(
        "agent 兜底（业务类，转人工）：原因=%s，exit_reason=%s。%s",
        verdict.reason, exit_reason, tail,
    )


def _signal(kinds: dict[str, int], stopped: str | None, exit_reason: str) -> str:
    """第 ⑥ 行的"问题信号"（返回空串 = 没有信号）。

    顺序即优先级：打转 → 卡住 → 第 3 级停过 → 预算烧穿 → 轮次烧完。多个同时成立时只报第一个
    （与反思的触发原因同一考虑：同时报多个会让"到底为什么"这一栏失去意义）。
    """
    if kinds.get("loop"):
        return "loop"
    if kinds.get("stuck"):
        return "stuck"
    if stopped:
        return "guard_stop"
    if exit_reason == "token_budget":
        return "token_budget"
    if exit_reason == "max_turns":
        return "max_turns"
    return ""


def _steps_of(context: Any) -> list[Any]:
    return list(getattr(getattr(context, "steps", None), "steps", []) or [])


def _kinds_of(state: Any) -> dict[str, int]:
    kinds = getattr(state, "kinds", None)
    return kinds if isinstance(kinds, dict) else {}


def _unfinished(steps: list[Any]) -> list[str]:
    """步骤清单里"没做成"的那些，渲染成给人看的一行行文本（**有上限**）。

    口径 = ``status != "ok"``：清单里只有 ``ok`` 与 ``failed`` 两种取值（见 ``steps`` 模块），
    但这里用"不等于 ok"而不是"等于 failed" —— 将来多出一种状态时，默认进清单（
    fail-safe：宁可多报一条，也不要悄悄漏掉一条没做成的动作）。
    """
    broken = [step for step in steps if getattr(step, "status", "") != "ok"]
    lines = [
        f"第 {getattr(step, 'seq', 0)} 步 {getattr(step, 'tool', '')}"
        f"（{getattr(step, 'status', '')}）"
        for step in broken[:MAX_UNFINISHED]
    ]
    rest = len(broken) - len(lines)
    if rest > 0:
        lines.append(f"…还有 {rest} 项")
    return lines
