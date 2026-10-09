"""任务分流：策略对象（plan）—— 能力位 + 预算 + 风险，而不是一个 intent 标签。

为什么不用「意图标签」分流
-------------------------
标签**排他**，能力**可叠加**。一份「技术方案写作」同时需要 `retrieval + reflection`；
「跑一下这段代码再解释结果」需要 `code_exec + retrieval`。用单标签分流，第二种能力
永远进不来，最后只能退化成枚举所有组合（`writing_with_rag`、`coding_with_rag`…），
组合爆炸，且每加一个能力都要改一遍判定逻辑。

正确的输出是一份**能力集合 + 预算**。但图的走向必须是**单个确定值**，所以拆成两层：

- `task`  主支路，**单值**，决定图往哪走（确定性 ⇒ 可复现、可排查、可度量）；
- `needs` 能力位，**可叠加**，供支路内部决定要不要多挂一个阶段。

两者并存不矛盾：**走向要确定性，能力要可叠加**。

⚠️ plan 必须是**普通 dict**，不能是 pydantic 实例 —— 它会进 state，而 state 会被
checkpointer 序列化。放不可序列化对象会在**续跑时**炸在半路，而那时离写入已经隔了
一次重启，排查成本极高（见 `tests/test_router.py::test_plan_is_json_serializable`）。
"""
from __future__ import annotations

#: 四条支路。**顺序即默认表的键顺序**，不要再另开一份列表（两处真相）。
ROUTES = ("direct", "rag", "write", "code")

#: 每条支路的默认预算。
#:
#: ⚠️ 它**取代** `src/graph.py` 的模块常量 `MAX_ITERATION`，不能与之并存：
#: 一旦有支路，「写作允许迭代 6 轮、直答 0 轮」就没法用一个全局常量表达，
#: 而两个来源共存必然出现"改了 plan 忘了改常量"这类静默不一致。
#:
#: `max_iterations=0` 是**有意的**：直答没有可纠错的信号（见 nodes.py 的支路表），
#: 给它循环只会换措辞、白烧 token。
DEFAULT_BUDGET = {
    "direct": {"max_iterations": 0, "max_tokens": 2_000, "deadline_ms": 30_000},
    "rag":    {"max_iterations": 3, "max_tokens": 8_000, "deadline_ms": 90_000},
    "write":  {"max_iterations": 6, "max_tokens": 24_000, "deadline_ms": 300_000},
    "code":   {"max_iterations": 8, "max_tokens": 32_000, "deadline_ms": 600_000},
}

#: 支路 → 默认能力位。
NEEDS_BY_TASK = {
    "direct": {"retrieval": False, "reflection": False, "code_exec": False, "human": False},
    "rag":    {"retrieval": True,  "reflection": False, "code_exec": False, "human": False},
    "write":  {"retrieval": False, "reflection": True,  "code_exec": False, "human": False},
    "code":   {"retrieval": False, "reflection": False, "code_exec": True,  "human": False},
}

#: 路由器也不确定时走哪条。**默认 "rag" 而不是 "direct"**（fail-safe 偏多做）：
#: 两类错误的代价**不对称** ——
#:   该检索却直答 = 事故（无依据作答、可能编造，用户看不出来）；
#:   不该检索却检索 = 只是慢 100~300ms。
#: 方向与限流/鉴权的 fail-closed **相反**，正是因为这个不对称。
DEFAULT_FALLBACK = "rag"

#: `plan["source"]` 的合法取值。**有限取值**才能安全地当指标标签。
SOURCES = ("rule", "cache", "llm", "inherit", "escalate")

#: `plan["risk"]` 的三个位。风险不进路由走向，只影响**出口**（要不要转人工）。
RISK_KEYS = ("factual_claim", "injection_surface", "side_effect")


def _as_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def make_plan(task, *, source, needs=None, budget=None, risk=None,
              confidence=1.0, fallback=DEFAULT_FALLBACK, why=""):
    """构造一份 plan（普通 dict）。

    task:       期望的支路；不在 `ROUTES` 里时按 `fallback` 收敛。
    source:     这次判定来自哪一层（`rule` / `cache` / `llm` / `inherit` / `escalate`）。
                **必须记**：它回答"路由是怎么定的"，是排查路由退化时的第一手线索。
    needs:      能力位覆盖；缺省取该支路的默认表。
    budget:     预算覆盖；缺省取该支路的默认表。
    risk:       风险位覆盖。
    confidence: 路由器**自己**的置信度（不是答案的置信度）。
    fallback:   兜底支路。
    why:        人话的判定理由，进日志与观测。
    """
    if task not in NEEDS_BY_TASK:
        task = fallback if fallback in NEEDS_BY_TASK else DEFAULT_FALLBACK
    merged_needs = dict(NEEDS_BY_TASK[task])
    if isinstance(needs, dict):
        merged_needs.update({k: bool(v) for k, v in needs.items() if k in merged_needs})
    merged_budget = dict(DEFAULT_BUDGET[task])
    if isinstance(budget, dict):
        merged_budget.update({k: v for k, v in budget.items() if k in merged_budget})
    merged_risk = {k: False for k in RISK_KEYS}
    if isinstance(risk, dict):
        merged_risk.update({k: bool(v) for k, v in risk.items() if k in merged_risk})
    return {
        "task": task,
        "source": source if source in SOURCES else "rule",
        "needs": merged_needs,
        "budget": merged_budget,
        "risk": merged_risk,
        "confidence": _as_float(confidence, 0.0),
        "fallback": fallback,
        "why": why,
    }


def plan_from_state(raw, fallback=DEFAULT_FALLBACK):
    """从 checkpoint 里带回的 plan 安全恢复；畸形一律返回 None（交给本轮重新判）。

    为什么必须过这一层：plan 会跨轮继承，而 state 可能来自**旧版本代码**写下的
    checkpoint（滚动升级期间在途的那批会话）。直接信任它 = 让一次版本升级把在途
    会话打死在 `KeyError` 上。
    """
    if not isinstance(raw, dict):
        return None
    task = raw.get("task")
    if task not in NEEDS_BY_TASK:
        return None
    return make_plan(
        task,
        source="inherit",
        needs=raw.get("needs"),
        budget=raw.get("budget"),
        risk=raw.get("risk"),
        confidence=_as_float(raw.get("confidence"), 1.0),
        fallback=raw.get("fallback") if raw.get("fallback") in ROUTES else fallback,
        why="继承上一轮",
    )
