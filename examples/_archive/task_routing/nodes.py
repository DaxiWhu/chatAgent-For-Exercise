"""四条支路的节点实现与条件边。

核心口径：**支路的区别不在节点多少，而在「纠错信号从哪来」**
-----------------------------------------------------------
大家直觉上以为支路的差别是"链路长短"，实际决定拓扑的只有一件事：
**这条支路纠错时看什么信号**。信号可靠性决定循环能放开到多宽、停止条件怎么写。

| 支路 | 信号来源 | 可靠性 | 循环能放开到多宽 | 停止条件 |
|---|---|---|---|---|
| direct | **无** | — | **不给循环**（只会换措辞、白烧 token） | 单次即出 |
| rag | **检索相关性** | 中 | 中：分数落 `[LOW, HIGH)` 才重生成 | 分数 ≥ HIGH / 预算尽 |
| write | **critic（内部）** | 自洽但会漂 | 宽 | **连续两轮无新意见**（不是"分数到 90"） |
| code | **执行（客观真值）** | 最高 | 最宽 | 测试绿 / 超时兜底 |

两条容易做错的细节：

⚠️ **write 的 critic 输入必须不同于 generator** —— 只给 rubric + 草稿，**不给检索上下文**；
否则它是在给自己刚说过的话打分，收敛不了还看不出问题。

⚠️ **write 用"连续两轮无新意见"而不是"分数到 90"**：LLM critic 会为了显得有用来
编改进点，分数会一直小幅波动，拿阈值当停止条件等于永远停不下来。

⚠️ **code 的执行是独立安全域**：`guard` 只防 prompt 注入，**防不了生成代码里的
`rm -rf`** —— 沙箱、超时、文件作用域、网络开关缺一不可，那是另一层，不要指望 guard。
本模块因此**不内置任何执行实现**，只接受注入（见 `execute`）。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Literal, Optional

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.exceptions import OutputParserException
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt
from pydantic import BaseModel, Field, ValidationError, ConfigDict

from src.rag import citation_check, guard

from .plan import plan_from_state
from .router import DecisionCache, decide
from .state import OverAllState

# ---------------------------------------------------------------------------
# 常量与注入点
# ---------------------------------------------------------------------------
LOW, HIGH = 60, 80
LLM_TRY_TIME = 5
ERROR_REPORT = "服务出现问题请稍后重试"
DOCS_DIR = os.getenv("DOCS_DIR", "data")
TOP_K = 3

#: 可注入的全局对象（测试/demo 直接覆盖；生产由 `bind()` 或 `graph.build_graph()` 注入）。
MODEL = None       # 对话模型：需具备 invoke / with_structured_output
RETRIEVER = None   # 检索器：需具备 search(query, top_k) -> [(chunk, score), ...]
SANDBOX = None     # 代码执行沙箱：需具备 (code) -> {"passed": bool, "stdout":..., "stderr":...}

#: 进程级路由缓存（L1）。有界，见 router.DecisionCache。
CACHE = DecisionCache(size=int(os.getenv("RAG_ROUTER_CACHE_SIZE", "512") or 512))


def bind(model=None, retriever=None, sandbox=None):
    """注入三件外部依赖。返回 None。

    ⚠️ 沙箱**必须显式注入**才可能执行代码 —— 见 `execute` 的说明。
    """
    global MODEL, RETRIEVER, SANDBOX
    if model is not None:
        MODEL = model
    if retriever is not None:
        RETRIEVER = retriever
    if sandbox is not None:
        SANDBOX = sandbox


# ---------------------------------------------------------------------------
# 提示词（生产里应集中到 `src/prompts.py` 并纳入 PROMPT_VERSION 版本管理）
# ---------------------------------------------------------------------------
def _answer_prompt(context: str, question: str) -> str:
    return f"参考以下资料作答：\n{context}\n\n用户需求：{question}"


def _revise_prompt(previous_draft: str, feedback: str, source: str) -> str:
    # 关键要求是"输出【完整的新版本】"：模型天然倾向只回 diff，而下游把返回内容
    # **整体当作新草稿**使用 —— 只回片段会让最终回答残缺。这句是接口契约，不是客套。
    return (
        f"以下是{source}给出的修改意见，请据此修订你之前的草稿{previous_draft}，"
        f"输出【完整的新版本】，不要只给片段或 diff：\n\n{feedback}"
    )


def _evaluate_prompt(draft: str) -> str:
    return f"判断内容是否需要进行评审，如果需要对内容打分(0-100)并且给建议：\n{draft}"


def _critique_prompt(draft: str, rubric: str) -> str:
    # ⚠️ 刻意**不传检索上下文**：critic 拿到的输入必须与 generator 不同，
    # 否则它是"自己给自己打分"，会收敛到"我写的就是对的"。
    return (
        f"你是严格的审稿人。按以下标准审查草稿，只指出**具体**问题；"
        f"如果没有新问题，has_new_points 必须为 false。\n标准：{rubric}\n\n草稿：{draft}"
    )


def _code_prompt(task: str) -> str:
    return f"请写出能完成该任务的代码，只输出代码本身：\n{task}"


# ---------------------------------------------------------------------------
# 结构化输出模型
# ---------------------------------------------------------------------------
class ReviewResult(BaseModel):
    """评审结果。"""
    model_config = ConfigDict(extra="forbid")
    is_need_evaluate: bool = Field(description="是否需要评审；不需要时无需打分与建议")
    score: int = Field(description="评分 0-100", ge=0, le=100)
    feedback: str = Field(description="针对生成内容的建议")


class CritiqueResult(BaseModel):
    """批评结果（写作支路）。"""
    model_config = ConfigDict(extra="forbid")
    has_new_points: bool = Field(description="相比上一轮是否提出了**新的**问题")
    issues: list[str] = Field(default_factory=list, description="具体问题列表")
    feedback: str = Field(default="", description="给作者的修改意见")


class HumanDecision(BaseModel):
    action: Literal["accept", "revise"]
    feedback: Optional[str] = None


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _last_content(messages) -> str:
    """取最后一条消息的文本，兼容 dict 与 BaseMessage 两种形态。"""
    if not messages:
        return ""
    last = messages[-1]
    if isinstance(last, dict):
        return last.get("content", "") or ""
    return getattr(last, "content", "") or ""


def _budget(state) -> int:
    """读本轮支路的迭代预算。**单一来源**：只有 plan.budget。"""
    plan = state.get("route_plan") or {}
    return int((plan.get("budget") or {}).get("max_iterations", 3) or 0)


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------
def init(state: OverAllState) -> OverAllState:
    """图入口：复位**每轮该归零**的量。

    ⚠️ **刻意不清 `route_plan`** —— plan 要跨轮继承（"再详细点"必须继承上一轮的
    `needs.retrieval`）。langgraph 的返回是合并语义，不清就自然保留。
    """
    return {"iteration": 0, "force_exit": False, "exit_reason": "", "exit_verdict": {},
            "critique_rounds": 0, "critique_converged": False, "code": "", "exec_result": {}}


def route_task(state: OverAllState) -> OverAllState:
    """**入口路由节点**：把 query 编译成 plan，写回 `route` / `route_plan` / `route_source`。

    这是整条链路上唯一决定"往哪走"的地方。它**只读不写** query（见 router 模块头）。

    上一轮的 plan 从 state 里恢复（`plan_from_state`）用于**继承**；恢复失败就
    当作没有上一轮，重新判 —— 不能让一个畸形 checkpoint 把会话打死。
    """
    query = _last_content(state["messages"])
    prev = plan_from_state(state.get("route_plan"))
    plan = decide(query, prev_plan=prev, cache=CACHE)
    logging.info("路由：task=%s source=%s conf=%.2f elapsed=%.2fms why=%s",
                 plan["task"], plan["source"], plan["confidence"],
                 plan.get("elapsed_ms", 0.0), plan.get("why", ""))
    return {"route": plan["task"], "route_plan": plan, "route_source": plan["source"]}


def retrieve(state: OverAllState, config=None) -> OverAllState:
    """检索节点：写 `context` / `citations` / `retrieval_stats`。

    ⚠️ **stats 是本节点存在的第二个理由**。检索是整个链路里最容易"挂掉却看不出来"
    的一环：整条检索挂了时，5xx 不涨、延迟反而**变低**（因为不检索了）、答案照常
    返回（只是完全没有依据）—— 服务外观全绿。所以 `hits` / `empty` / `degraded`
    必须有观测出口，否则这条支路的故障只能靠用户反馈"答得不对"才发现。

    容错：知识库没配 / 语料为空 / 检索抛异常，一律降级为空上下文，绝不带崩整图。
    """
    t0 = time.perf_counter()
    try:
        query = _last_content(state["messages"])
        tenant = None
        if isinstance(config, dict):
            conf = config.get("configurable")
            if isinstance(conf, dict):
                tenant = conf.get("rag_tenant")
        hits = RETRIEVER.search(query, top_k=TOP_K) if RETRIEVER is not None else []
        ctx = "\n\n".join(_context_block(c) for c, _ in hits)
        citations = _build_citations(hits)
        if ctx:
            scan = guard.scan(ctx)
            if scan.hit:
                logging.warning("检索上下文疑似提示词注入（risk=%s）：%s",
                                scan.risk, scan.matches[:3])
        stats = {"hits": len(hits), "empty": not hits, "degraded": False,
                 "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
                 "context_chars": len(ctx), "tenant": tenant or ""}
        if not hits:
            logging.info("RAG 未召回任何片段（空命中）")
        return {"context": ctx, "citations": citations, "retrieval_stats": stats}
    except Exception as e:  # noqa: BLE001
        logging.warning("RAG 检索失败，降级为无上下文: %s", e)
        return {"context": "", "citations": [], "retrieval_stats": {
            "hits": 0, "empty": True, "degraded": True, "degraded_reason": str(e),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2), "context_chars": 0}}


def _context_block(chunk) -> str:
    """命中块 → 送模型的文本。优先级：父窗 > 保真内容 > 检索文本。"""
    body = chunk.get("parent_text") or chunk.get("content") or chunk.get("text") or ""
    return f"[来源: {chunk.get('source', '?')}]\n{body}"


def _build_citations(hits) -> list:
    """命中 → 可回溯引用。生产应直接复用 `src.rag.retrieval.build_citations`。"""
    out = []
    for i, (chunk, score) in enumerate(hits, start=1):
        body = (chunk.get("content") or chunk.get("text") or "").strip()
        out.append({
            "n": i, "source": chunk.get("source", ""),
            "source_path": chunk.get("source_path", ""),
            "score": round(float(score), 4),
            "chunk_id": chunk.get("chunk_id", ""),
            "page": chunk.get("page"),
            "snippet": body[:1000] + ("…" if len(body) > 1000 else ""),
        })
    return out


def _check_citations(state, draft) -> dict:
    """引用校验（**只算不拦**）。异常降级为 `{}` —— 观测失败不能影响作答。"""
    try:
        return citation_check.verify(draft if isinstance(draft, str) else "",
                                     state.get("citations") or [], docs_dir=DOCS_DIR)
    except Exception as e:  # noqa: BLE001
        logging.warning("引用校验失败（不影响作答）：%s", e)
        return {}


def generate(state: OverAllState) -> OverAllState:
    """生成节点：**direct 与 rag 与 write 共用**。

    ⚠️ 这里是最关键的一条发现：本节点在 `iteration == 0` 且 `context == ""` 时
    **本来就**走"直传 messages"的分支。所以：

    > **direct 支路 = 现有的"无上下文 generate"，只是跳过 `retrieve` 这一步。**

    因此分流**不需要**为直答新写一个生成节点 —— 新写会产生两份几乎一样的 prompt
    组装逻辑，然后开始漂移（改了一处忘了另一处）。共用同一个节点，"直答"和"带资料
    作答"就永远只有一处实现。
    """
    iteration = state["iteration"]
    if iteration == 0:
        messages = state["messages"]
        ctx = state.get("context", "")
        safe_ctx = guard.wrap_context(ctx) if ctx else ""
        if safe_ctx:
            res = MODEL.invoke(_answer_prompt(safe_ctx, _last_content(messages)))
        else:
            res = MODEL.invoke(messages)
        return {"messages": res, "iteration": iteration + 1, "output": res.content,
                "citation_check": _check_citations(state, res.content)}

    src = state.get("feedback_source", "ai")
    src_cn = {"ai": "评审模型", "critic": "批评模型", "human": "人工审核"}.get(src, "评审模型")
    instruction = HumanMessage(
        content=_revise_prompt(state["output"], state.get("feedback", ""), src_cn))
    res = MODEL.invoke(state["messages"] + [instruction])
    return {"messages": [instruction, res], "iteration": iteration + 1, "output": res.content,
            "citation_check": _check_citations(state, res.content)}


def evaluate(state: OverAllState) -> OverAllState:
    """评审节点：结构化输出打分。失败重试 `LLM_TRY_TIME` 次后**降级不抛**。"""
    draft = state["output"]
    last_err = None
    for attempt in range(LLM_TRY_TIME):
        try:
            result = (MODEL.with_structured_output(ReviewResult)
                      .invoke(_evaluate_prompt(draft)))
            return {"is_need_evaluate": result.is_need_evaluate, "score": result.score,
                    "feedback": result.feedback, "feedback_source": "ai"}
        except (ValidationError, OutputParserException) as e:
            last_err = e
            logging.warning("evaluate 解析失败(第%d次): %s", attempt + 1, e)
        except Exception as e:  # noqa: BLE001
            last_err = e
            logging.warning("evaluate 调用失败(第%d次): %s", attempt + 1, e)
    # 重试耗尽 → force_exit，交给出口节点走 end，保证异常不穿透到图外
    return {"force_exit": True, "output": ERROR_REPORT, "exit_reason": "forced",
            **({"last_error": str(last_err)} if last_err is not None else {})}


def critique(state: OverAllState) -> OverAllState:
    """批评节点（写作支路）：信号是**内部**的，这是它与其它支路最大的不同。

    停止条件是**"连续两轮无新意见"**，实现方式是把每轮的 issues 累积在
    `state["critiques"]` 里，与前一轮比较集合包含关系。

    ⚠️ 不能用"分数到 90"：LLM critic 会为了显得有用来**编**改进点，分数会一直小幅
    波动，拿阈值当停止条件等于永远停不下来（或者反过来，critic 偷懒给了高分就早停）。
    """
    draft = state.get("output", "")
    rounds = int(state.get("critique_rounds", 0))
    history = list(state.get("critiques") or [])
    try:
        res = (MODEL.with_structured_output(CritiqueResult)
               .invoke(_critique_prompt(draft, "事实准确、结构清晰、无冗余、无未支撑的断言")))
        issues = [str(x) for x in (res.issues or [])]
        has_new = bool(res.has_new_points)
        feedback = res.feedback or ""
    except Exception as e:  # noqa: BLE001
        # 批评失败**不应**变成失败点：当作"没有新意见"收敛，让答案正常交付。
        logging.warning("critique 失败，按收敛处理：%s", e)
        issues, has_new, feedback = [], False, ""

    prev_issues = set(history[-1]) if history else set()
    # 双保险：模型说"有新意见"但内容与上一轮完全重合 ⇒ 按"无新意见"算。
    # 这是"连续两轮无新意见"里"连续"二字的真正实现。
    converged = (not has_new) or (bool(issues) and set(issues) <= prev_issues)
    new_history = history + [issues]
    if converged:
        logging.info("写作支路收敛：已迭代 %d 轮，第 %d 轮无新意见", rounds, rounds + 1)
    return {
        "critique_rounds": rounds + 1,
        "critiques": new_history,
        "critique_converged": converged,
        "feedback": feedback,
        "feedback_source": "critic",
    }


def write_code(state: OverAllState) -> OverAllState:
    """代码支路的生成节点：产出代码。

    与 `generate` 分开，是因为代码支路的产物**不是给用户看的文本**，而是要送进沙箱
    执行的东西 —— 两者的提示词、产物形态、下游消费方式都不同，硬塞进一个节点会让
    两边都变难改。
    """
    prev_code = state.get("code", "")
    err = (state.get("exec_result") or {})
    if prev_code and err and not err.get("passed", False):
        prompt = (f"下面的代码执行失败，请修复后输出**完整**代码，不要只给 diff：\n"
                  f"```\n{prev_code}\n```\n\n错误输出：\n{err.get('stderr') or err.get('stdout') or ''}")
    else:
        prompt = _code_prompt(_last_content(state["messages"]))
    res = MODEL.invoke(prompt)
    return {"code": res.content, "output": res.content, "iteration": state.get("iteration", 0) + 1,
            "feedback_source": "ai"}


def execute(state: OverAllState) -> OverAllState:
    """代码支路的执行节点：**全链路唯一有客观真值的反馈信号**。

    ⚠️⚠️ **安全边界 —— 本节点刻意不内置任何执行实现。**

    `guard` 只防 prompt 注入，**防不了生成代码里的破坏性操作**（`rm -rf`、读环境变量
    回传、外发请求）。沙箱、超时、文件作用域、网络开关是一个**独立的安全域**，
    必须由调用方通过 `bind(sandbox=...)` 注入。

    未注入沙箱时本节点**不执行**、显式标记 `executed=False`，并让出口按
    "未验证"处理 —— **绝不**退化成"用 `subprocess` 跑一下试试"。这在示例代码里
    尤其重要：示例会被复制到生产，一个方便的内置执行器就是一颗定时炸弹。
    """
    code = state.get("code", "")
    if SANDBOX is None:
        logging.warning("代码支路未注入沙箱（SANDBOX=None）：跳过执行，结果标记为未验证")
        return {"exec_result": {"executed": False, "passed": False,
                                "stdout": "", "stderr": "sandbox not configured"}}
    try:
        raw = SANDBOX(code) or {}
        return {"exec_result": {"executed": True,
                                "passed": bool(raw.get("passed")),
                                "stdout": str(raw.get("stdout", ""))[:2000],
                                "stderr": str(raw.get("stderr", ""))[:2000]}}
    except Exception as e:  # noqa: BLE001
        logging.warning("沙箱执行异常：%s", e)
        return {"exec_result": {"executed": True, "passed": False,
                                "stdout": "", "stderr": f"sandbox error: {e}"}}


def human_review(state: OverAllState):
    """人工审核节点：`interrupt` 挂起等人工提交，输入不合法就再次挂起。"""
    error = None
    while True:
        payload = {"draft": state["output"], "score": state.get("score", 0),
                   "feedback": state.get("feedback", ""), "error": error}
        raw = interrupt(payload)
        try:
            decision = HumanDecision.model_validate(raw)
        except ValidationError as e:
            error = f"输入不合法：{e.errors()}"
            continue
        if decision.action == "revise" and not decision.feedback:
            error = "打回必须填写修改意见"
            continue
        return {"human_action": decision.action, "feedback": decision.feedback or "",
                "feedback_source": "human"}


# ---------------------------------------------------------------------------
# 出口：统一 + 判据分治
# ---------------------------------------------------------------------------
def _classify_exit(state) -> str:
    """出口归因。

    ⚠️ **"达标"与"预算耗尽"必须分开报。** 混在一起，调优方向会完全错 ——
    你会以为该去调 prompt，其实只是预算不够（或反过来）。

    ⚠️ 命名原则：**只描述事实，不描述判断**。不要把"好/坏"塞进归因值里，
    否则口径不同的调用方会对同一批数据得出相反结论。
    """
    if state.get("force_exit"):
        return "forced"
    if state.get("human_action") == "revise":
        return "rejected"
    task = state.get("route") or ""
    if task == "direct":
        return "not_needed"          # 单次即出，没有"是否达标"这个概念
    it = int(state.get("iteration", 0) or 0)
    if task == "rag" and not state.get("is_need_evaluate", True):
        return "not_needed"
    if it >= _budget(state):
        return "budget_exhausted"
    if task == "write" and not state.get("critique_converged"):
        return "budget_exhausted"
    if task == "code" and not (state.get("exec_result") or {}).get("passed"):
        return "budget_exhausted"
    return "converged"


def _judge_common(state):
    """所有支路共享的那部分出口判据。"""
    out = (state.get("output") or "").strip()
    issues = []
    if not out:
        issues.append("empty_answer")
    if out in (ERROR_REPORT,):
        issues.append("error_report")
    if out.endswith("...") or out.endswith("…"):
        issues.append("possibly_truncated")
    return issues


def _judge_direct(state):
    """直答：没有 citations 是**正常**的，判据只看"答没答出来"。"""
    issues = _judge_common(state)
    return {"route": "direct", "ok": not issues, "issues": issues}


def _judge_rag(state):
    """检索问答：在通用判据上追加**引用相关**的两条。"""
    issues = _judge_common(state)
    needs = (state.get("route_plan") or {}).get("needs") or {}
    if needs.get("retrieval", True) and not (state.get("citations") or []):
        issues.append("no_citations_despite_retrieval")
    cc = state.get("citation_check") or {}
    if cc.get("block_worthy"):
        issues.append("citation_unsupported")
    stats = state.get("retrieval_stats") or {}
    if stats.get("degraded"):
        issues.append("retrieval_degraded")
    return {"route": "rag", "ok": not issues, "issues": issues}


def _judge_write(state):
    issues = _judge_common(state)
    if not state.get("critique_converged"):
        issues.append("critique_not_converged")
    return {"route": "write", "ok": not issues, "issues": issues}


def _judge_code(state):
    issues = _judge_common(state)
    ex = state.get("exec_result") or {}
    if not ex.get("executed"):
        issues.append("not_executed")
    elif not ex.get("passed"):
        issues.append("tests_failed")
    return {"route": "code", "ok": not issues, "issues": issues}


#: 支路 → 出口判据。**出口共享，判据分治。**
#:
#: ⚠️ 为什么不共用一套判据：direct 支路没有 citations ⇒ `citation_check{status="no_citations"}`
#: 在 direct 上是**正常状态**。不区分支路，direct 流量会把 `no_citations` 灌满，
#: 真正的"该有引用却没有"就被淹没在噪声里了。**每加一条支路，这张表都要重审一遍。**
EXIT_JUDGES = {"direct": _judge_direct, "rag": _judge_rag,
               "write": _judge_write, "code": _judge_code}


def finish(state: OverAllState) -> OverAllState:
    """**统一出口节点**：算 `exit_reason` 与 `exit_verdict`。

    所有支路的终点都汇到这里，所以观测/审核/审计/配额只需要在这里实现一次。
    但**判据按支路分治**（`EXIT_JUDGES`）。
    """
    task = state.get("route") or "rag"
    reason = state.get("exit_reason") or _classify_exit(state)
    judge = EXIT_JUDGES.get(task, _judge_rag)
    verdict = judge(state)
    if not verdict["ok"]:
        logging.warning("出口判据未过（route=%s reason=%s）：%s",
                        task, reason, ",".join(verdict["issues"]))
    else:
        logging.info("出口：route=%s reason=%s iterations=%s", task, reason,
                     state.get("iteration"))
    return {"exit_reason": reason, "exit_verdict": verdict}


# ---------------------------------------------------------------------------
# 条件边
# ---------------------------------------------------------------------------
def after_route_task(state: OverAllState) -> str:
    """入口条件边：plan → 第一条要跑的节点。

    注意 direct 也进 `generate` —— 因为 `generate` 在 `context == ""` 时本来就
    走"直传 messages"的分支。不为它单开一个生成节点（两份实现必然漂移）。
    """
    plan = state.get("route_plan") or {}
    task = plan.get("task") or state.get("route") or "rag"
    if task == "code":
        return "write_code"
    if task == "direct":
        return "generate"
    # rag / write：needs.retrieval 为真才去检索。write 默认不检索，
    # 但"根据资料写一份报告"这类会被路由器把 retrieval 位打开。
    if (plan.get("needs") or {}).get("retrieval", True):
        return "retrieve"
    return "generate"


def after_generate(state: OverAllState) -> str:
    """generate 之后：先判要不要调工具，再按支路走各自的循环。"""
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "tool_node"
    task = state.get("route") or "rag"
    if task == "direct":
        return "finish"          # 无信号 ⇒ 不给循环
    if task == "write":
        return "critique"        # 信号来自 critic
    return "evaluate"            # 信号来自评审/检索相关性


def after_review(state: OverAllState) -> str:
    """evaluate 之后：收尾 / 重生成 / 转人工。**顺序即策略**，调换会改变行为。"""
    if state.get("force_exit"):
        return "finish"
    if not state.get("is_need_evaluate", True):
        return "finish"
    if int(state.get("iteration", 0) or 0) >= _budget(state):
        return "finish"
    if state.get("score", 0) >= HIGH:
        return "finish"
    if state.get("score", 0) >= LOW:
        return "regenerate"
    # ⚠️ 升级人工的条件必须是「低分 **AND** 高风险」同时满足。
    # 只看分数会让"写首诗 55 分"这种低风险内容挤占人工队列，
    # 把审核资源从真正需要人的场景上挤走。
    risk = (state.get("route_plan") or {}).get("risk") or {}
    if any(risk.get(k) for k in ("factual_claim", "side_effect")):
        return "human"
    return "finish"


def after_critique(state: OverAllState) -> str:
    """critique 之后：无新意见 ⇒ 收尾；否则回炉，直到预算用尽。"""
    if state.get("critique_converged"):
        return "finish"
    if int(state.get("critique_rounds", 0) or 0) >= _budget(state):
        return "finish"
    return "revise"


def after_execute(state: OverAllState) -> str:
    """execute 之后：测试绿 ⇒ 收尾；否则带着错误回炉，直到预算用尽。

    ⚠️ **"没执行"与"执行了但没过"必须分开处理。** 沙箱未注入、沙箱自己崩了，
    这类失败是**环境问题，重试不会变好** —— 让它按"可重试"走，只会把一个
    环境配置错误变成 8 轮无意义的模型调用（白烧 token，还把真正的错误埋在
    第 8 次的日志里）。只有"执行了、但测试没过"才是模型能修的。
    """
    ex = state.get("exec_result") or {}
    if ex.get("passed"):
        return "finish"
    if not ex.get("executed"):
        return "finish"          # 不可恢复：环境问题，退出而不是空转
    if int(state.get("iteration", 0) or 0) >= _budget(state):
        return "finish"
    return "revise"


def after_human(state: OverAllState) -> str:
    """human_review 之后：接受 ⇒ 收尾；其它任何值（含空）都偏向继续修订。"""
    return "finish" if state.get("human_action") == "accept" else "revise"


__all__ = [
    "bind", "init", "route_task", "retrieve", "generate", "evaluate", "critique",
    "write_code", "execute", "human_review", "finish",
    "after_route_task", "after_generate", "after_review", "after_critique",
    "after_execute", "after_human", "ToolNode",
]
