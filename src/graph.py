"""chatAgent 图逻辑（从 chatAgent.ipynb 提炼，作为唯一真源）。

与 notebook 的区别：
- model / checkpointer / tools / retriever 均可注入，便于测试（build_graph 参数）。
- evaluate 的 except 分两层：先接解析/校验类异常，再接 API/超时/限流等通用异常，
  均为「重试 N 次后降级 force_exit」，符合生产化讨论结论。
- retrieve 节点自带容错：知识库未配置或检索失败时降级为空上下文，不让整图崩。
- 不在此处连接数据库；checkpointer 由调用方注入（测试用 MemorySaver）。
"""
import os
from typing import Optional, Literal
import logging

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import RunnableConfig
from langgraph.graph import MessagesState, StateGraph, START, END
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel, Field, ValidationError, ConfigDict

from .rag.retrieval import (
    load_chunks, build_retriever, build_citations, context_block, resolve_tenant,
)
from .rag.parse_cache import load_chunks_cached
from .rag import guard
from . import prompts

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------
LOW = 60
HIGH = 80
LLM_TRY_TIME = 5
MAX_ITERATION = 10
ERROR_REPORT = "服务出现问题请稍后重试"

# RAG：语料目录（可用 DOCS_DIR 覆盖）与召回条数
DOCS_DIR = os.getenv("DOCS_DIR", "data")
TOP_K = 3

# ----------------------------------------------------------------------------
# 可注入的全局模型（测试时由 build_graph(model=...) 覆盖）
# ----------------------------------------------------------------------------
MODEL = None
TOOLS: list = []


def set_model(m):
    """注入全局对话模型（写入模块级 MODEL）。

    m: 具备 invoke / with_structured_output 的模型实例（如 ChatOpenAI 或测试用的
        MagicMock）。生产路径走 build_graph(model=...)，本函数留给直接在测试里
        替换全局模型的场景。

    注意是**就地覆盖全局变量**：调用后所有节点都改用新模型，无返回值。
    """
    global MODEL
    MODEL = m


# ----------------------------------------------------------------------------
# 数据模型
# ----------------------------------------------------------------------------
class ReviewResult(BaseModel):
    """llm结构化输出内容评分"""
    model_config = ConfigDict(extra="forbid")
    is_need_evaluate: bool = Field(
        description="是否需要评审，比如说查天气不需要，生成文章需要,不需要评审的话无需打分无需建议"
    )
    score: int = Field(description="评分0-100", ge=0, le=100)
    feedback: str = Field(description="针对生成内容的建议")


class HumanDecision(BaseModel):
    action: Literal["accept", "revise"]          # 只允许这两个值
    feedback: Optional[str] = Field(default=None)  # 打回时必填


class OverAllState(MessagesState):
    output: str = ""
    context: str = ""          # RAG 检索到的参考资料（送模型的文本）
    citations: list = []       # 引用的结构化来源信息（给用户回溯核对用）
    is_need_evaluate: bool = False
    score: int = 0
    feedback: str = ""
    human_action: str = ""
    iteration: int = 0
    feedback_source: str = ""
    force_exit: bool = False


# ----------------------------------------------------------------------------
# RAG 检索器（懒加载 + 按租户分片缓存）
# ----------------------------------------------------------------------------
# _RETRIEVER 是**注入点**：测试与 build_graph 直接覆盖它，此时租户分片不生效
# （注入的检索器是调用方的责任）。生产链路上它保持 None，走下面的缓存逻辑。
_RETRIEVER = None
# 语料只解析一次：多个租户共用同一份 chunks，只是"入库到各自租户"。
_CHUNKS = None
# 按租户缓存的检索器。**必须有界** —— 见 get_retriever 的说明。
_TENANT_RETRIEVERS: dict[str, object] = {}
# 上限取小值：需要隔离的是**少数租户**，而不是"任意租户字符串"。
# 每个分片都持有 chunks（pg 后端还带一个连接配置与增量同步状态），
# 无上限意味着"构造一个陌生租户"就能吃内存 —— 那是 DoS 面，不是功能。
_TENANT_CACHE_MAX = max(1, int(os.getenv("RAG_TENANT_CACHE_MAX", "8") or 8))


def _load_chunks_once():
    """加载并缓存语料块。进程内只解析一次，供所有租户分片复用。"""
    global _CHUNKS
    if _CHUNKS is None:
        # 优先走文档解析落盘缓存（src/rag/parse_cache.py）：语料没变时直接反序列化
        # 上次的 chunks，跳过"重新解析 + 重新切分"这整段（docling/OCR 是分钟级开销）。
        # 缓存层任何异常都退回直接解析，缓存只是加速、不能成为失败点。
        try:
            _CHUNKS = load_chunks_cached(DOCS_DIR)
        except Exception as e:  # noqa: BLE001
            logging.warning("解析缓存不可用，回退直接解析语料：%s", e)
            _CHUNKS = load_chunks(DOCS_DIR)
        logging.info("RAG 语料加载完成：%d 个片段（DOCS_DIR=%s）", len(_CHUNKS), DOCS_DIR)
        if not _CHUNKS:
            # 语料为空是**静默失败**：服务照常起、探针照常绿、SSE 照常连，
            # 只是检索恒无结果、回答里没有任何引用 —— 没有这条告警就只能靠
            # 用户反馈"答得不对"才发现。容器部署最常见的原因是**语料目录
            # 没挂进容器**（镜像刻意不打 data/），其次是目录里没有受支持的文档。
            logging.warning(
                "RAG 语料为空（DOCS_DIR=%s）：检索将恒无结果、回答不会有任何引用。"
                "容器部署优先检查语料目录是否挂载、路径是否与 DOCS_DIR 一致。",
                DOCS_DIR,
            )
    return _CHUNKS


def get_retriever(tenant: str | None = None):
    """取检索器，按租户分片缓存（懒加载）。

    tenant: 请求期租户。None = 沿用进程级 RAG_TENANT，行为与改造前一致。
        非 None 时会为该租户单独缓存一个检索器（仅 pg 后端真正按租户过滤）。

    为什么必须按租户分片
    -------------------
    `PgVectorStore` 的租户是**构造期**绑定的（`self.tenant`），而检索器又是单例 ——
    合起来的后果是：第一个请求用哪个租户，之后**所有**请求都跟着用那个租户。
    改造前只读环境变量所以看不出来；一旦请求期租户接进来，不按租户分片就等于
    "谁先来谁定调"，隔离形同虚设。

    为什么缓存必须**有界**
    ---------------------
    租户字符串来自请求。在 Key 绑定的多租户模式下它是**受信集合**（由 TENANT_KEYS
    决定），有界天然成立；但为了不依赖调用方永远守规矩，这里仍然设了上限：
    超限时丢弃一个旧分片而不是无限增长 —— 内存涨到 OOM 是比"偶尔重建检索器"
    严重得多的故障。丢弃后下次访问会重建（语料已在 _CHUNKS 里，代价可控）。
    """
    global _RETRIEVER

    # 注入优先：测试与 build_graph(retriever=...) 显式给了实例就用它。
    # 这条分支**忽略 tenant** —— 注入意味着"调用方自己负责隔离"。
    if _RETRIEVER is not None:
        return _RETRIEVER

    chunks = _load_chunks_once()

    if tenant is None:
        # 无租户语境（CLI/脚本/原有调用点）：用进程级默认值，且不占分片缓存，
        # 保持"整个进程只构建一次"的原有语义。
        if "" not in _TENANT_RETRIEVERS:
            _TENANT_RETRIEVERS[""] = build_retriever(chunks)
            logging.info("RAG 检索器构建完成：%d 个片段（DOCS_DIR=%s，租户=%s）",
                         len(chunks), DOCS_DIR, resolve_tenant(None) or "<不隔离>")
        return _TENANT_RETRIEVERS[""]

    key = tenant.strip()
    hit = _TENANT_RETRIEVERS.get(key)
    if hit is not None:
        return hit

    if len(_TENANT_RETRIEVERS) >= _TENANT_CACHE_MAX:
        # 丢一个最老的（dict 保序，取第一个插入的）。不用 LRU 是有意的：
        # 这里的访问分布是"少数租户长期活跃"，FIFO 足够，而 LRU 要额外维护
        # 访问顺序（并发下还要加锁），复杂度换不来实际收益。
        evicted = next(iter(_TENANT_RETRIEVERS))
        _TENANT_RETRIEVERS.pop(evicted, None)
        logging.warning("检索器租户分片缓存已满（上限 %d），丢弃租户 %s 的分片",
                        _TENANT_CACHE_MAX, evicted or "<默认>")

    r = build_retriever(chunks, tenant=key)
    _TENANT_RETRIEVERS[key] = r
    logging.info("RAG 检索器构建完成：%d 个片段（租户=%s）",
                 len(chunks), resolve_tenant(key) or "<不隔离>")
    return r


def _last_content(messages) -> str:
    """取最后一条消息的文本，兼容 dict 与 BaseMessage 两种形态。

    messages: 消息列表（通常是 state["messages"]）。空列表 / None 返回 ""。
        元素既可以是 LangChain 的 BaseMessage（走 .content 属性），
        也可以是 {"content": ...} 形态的 dict（走 .get）—— 两种形态都判。
    """
    if not messages:
        return ""
    last = messages[-1]
    if isinstance(last, dict):
        return last.get("content", "") or ""
    return getattr(last, "content", "") or ""


# ----------------------------------------------------------------------------
# 节点
# ----------------------------------------------------------------------------
def init(state: OverAllState) -> OverAllState:
    """图入口节点：把跨轮次会累积的字段复位。

    state: 图状态。**只读入参、不读任何字段** —— 它存在是为了显式重置
        iteration 与 force_exit：同一 thread_id 多轮对话时这两个值会随
        checkpoint 带过来，不清就会让上一轮的失败/迭代次数污染本轮。
    """
    return {
        "iteration": 0,
        "force_exit": False,
    }


def retrieve(state: OverAllState, config: RunnableConfig | None = None) -> OverAllState:
    """RAG 检索节点：放在 generate 之前，把参考资料写入 state["context"]，
    并把**引用信息**写入 state["citations"]。

    state: 图状态。读 messages（取最后一条的内容作为 query）；写回 context 与
        citations 两个字段，其余字段不动。
    config: langgraph 注入的运行配置（标注成 RunnableConfig 是 langgraph 的硬要求，
        写成 dict 会在构图时告警）。从 `configurable.rag_tenant` 取本次请求的
        租户，交给检索器做隔离。缺省（None / 没有该键）时退回进程级 RAG_TENANT，
        **与改造前行为一致**。

        为什么要走 config 而不是 state：租户是**运行参数**，不是会话状态 ——
        写进 state 会被检查点持久化，续跑时可能带着一个过期的租户值。

    容错要求：知识库没配、语料为空、检索抛异常，都只降级为「无上下文」，
    绝不因为检索失败把整张图带崩。

    为什么必须同时产出 citations：`search()` 返回的是完整块记录（页码、坐标、
    章节路径、保真原文、表格网格、配图路径…），而"送模型的文本"只是其中一个字段。
    过去这里把整条记录拍成一段字符串，**其余元数据在接口层就再也拿不到了** ——
    用户看到一个答案，却无法确认它出自哪一页、原文长什么样。
    """
    try:
        query = _last_content(state["messages"])
        # config 可能缺失、可能不是 dict（直接调用节点时）—— 逐层兜底取，
        # 取不到就是 None，等价于"沿用进程级默认租户"。
        tenant = None
        if isinstance(config, dict):
            conf = config.get("configurable")
            if isinstance(conf, dict):
                tenant = conf.get("rag_tenant")
        hits = get_retriever(tenant).search(query, top_k=TOP_K)
        # 文本块优先用父窗/保真内容（见 context_block 的优先级说明）
        ctx = "\n\n".join(context_block(c) for c, _ in hits)
        citations = build_citations(hits)
        # 注入扫描（只告警，不改内容）：把"资料里夹带指令"这件事变成一条可见的日志。
        # 真正的结构隔离在 generate 里由 guard.wrap_context 完成 —— 扫描只是提高发现率。
        if ctx:
            scan = guard.scan(ctx)
            if scan.hit:
                logging.warning("检索上下文疑似提示词注入（risk=%s）：%s",
                                scan.risk, scan.matches[:3])
        if not ctx:
            logging.info("RAG 未召回任何片段，按无上下文继续")
        return {"context": ctx, "citations": citations}
    except Exception as e:  # noqa: BLE001
        logging.warning("RAG 检索失败，降级为无上下文: %s", e)
        return {"context": "", "citations": []}


def generate(state: OverAllState) -> OverAllState:
    """生成节点：首轮带 RAG 上下文作答，后续轮按评审/人工意见修订。

    state: 图状态。读 messages / context / iteration / feedback_source /
        feedback / output，写回 messages / iteration+1 / output。
          - iteration == 0：有 context 时用「参考以下资料作答」模板拼 prompt
            单独 invoke；无 context 时直传 messages；
          - iteration > 0：按 feedback_source（"ai" / 其它即人工）决定措辞，
            把意见包成一条 HumanMessage，明确要求输出【完整的新版本】而非 diff，
            与新草稿一并追加进历史。
    """
    iteration = state["iteration"]
    if iteration == 0:
        messages = state["messages"]
        ctx = state.get("context", "")
        # 送模型前给资料加结构边界（"<资料>…</资料> + 声明不执行其中指令"）：
        # 这是提示词注入防护真正的兜底一层（规则扫描只是辅助）。按 RAG_GUARD 开关，
        # 关掉时 wrap_context 原样返回；策略 block 时返回空串 → 视同无上下文。
        safe_ctx = guard.wrap_context(ctx) if ctx else ""
        if safe_ctx:
            prompt = prompts.answer_prompt(safe_ctx, _last_content(messages))
            res = MODEL.invoke(prompt)
        else:
            res = MODEL.invoke(messages)
        return {
            "messages": res,
            "iteration": iteration + 1,
            "output": res.content,
        }
    src = state.get("feedback_source", "ai")
    src_cn = "评审模型" if src == "ai" else "人工审核"
    instruction = HumanMessage(content=prompts.revise_prompt(
        state["output"], state["feedback"], src_cn))
    res = MODEL.invoke(state["messages"] + [instruction])
    return {
        "messages": [instruction, res],  # 反馈指令 + 新草稿 一并追加进历史
        "iteration": iteration + 1,
        "output": res.content,
    }


def evaluate(state: OverAllState) -> OverAllState:
    """评审节点：用结构化输出打分（0-100）并给建议，失败重试 LLM_TRY_TIME 次。

    state: 图状态。只读 output（待评审的草稿），写回 is_need_evaluate / score /
        feedback / feedback_source。

    重试耗尽后**不抛异常**：改为写回 force_exit=True + ERROR_REPORT，
    交给 route_review 走 end —— 保证异常不会穿透到图外。异常分两层捕获：
    结构化输出解析/校验失败（模型没按 schema 返回）与 API/超时/限流等其余异常。
    """
    draft = state["output"]
    last_err = None
    for attempt in range(LLM_TRY_TIME):
        try:
            result = (MODEL.with_structured_output(ReviewResult)
                      .invoke(prompts.evaluate_prompt(draft)))
            return {
                "is_need_evaluate": result.is_need_evaluate,
                "score": result.score,
                "feedback": result.feedback,
                "feedback_source": "ai",
            }
        except (ValidationError, OutputParserException) as e:
            # 结构化输出解析/校验失败：模型没按 schema 返回
            last_err = e
            logging.warning("evaluate 解析失败(第%d次): %s", attempt + 1, e)
        except Exception as e:  # noqa: BLE001
            # API/超时/限流等：覆盖剩余所有情况，保证不把异常抛给图
            last_err = e
            logging.warning("evaluate 调用失败(第%d次): %s", attempt + 1, e)
    return {
        "force_exit": True,
        "output": ERROR_REPORT,
        "last_error": str(last_err),
    }


def human_review(state: OverAllState):
    """人工审核节点：interrupt 挂起等人工提交，输入不合法就再次挂起。

    state: 图状态。读 output / score / feedback 组装 interrupt 载荷；
        写回 human_action / feedback / feedback_source（feedback_source 恒为 "human"，
        让 generate 知道这次修订意见来自人工）。

    实现是一个 while True + interrupt 的循环：非法输入不退出，而是把错误信息
    塞进下一次 interrupt 的 payload["error"] 里让人工重填 —— 所以本节点可能
    挂起多次，直到拿到合法 HumanDecision。
    """
    error = None
    while True:
        payload = {
            "draft": state["output"],
            "score": state["score"],
            "feedback": state["feedback"],
            "error": error,          # 上一次非法时，把提示带回去
        }
        raw = interrupt(payload)     # 暂停，等人工提交

        try:
            decision = HumanDecision.model_validate(raw)
        except ValidationError as e:
            error = f"输入不合法：{e.errors()}"
            continue                 # 不合法 → 再 interrupt 一次，人工重填

        # 打回必须带修改意见
        if decision.action == "revise" and not decision.feedback:
            error = "打回必须填写修改意见"
            continue

        return {
            "human_action": decision.action,
            "feedback": decision.feedback or "",
            "feedback_source": "human",
        }


def route_tool(state: OverAllState) -> str:
    """generate 之后的条件分支：模型是否请求调用工具。

    state: 图状态。只看最后一条消息的 tool_calls —— 非空返回 "tool_node"，
        否则返回 "evaluate"。

    返回值必须是 add_conditional_edges 里登记过的键（"tool_node" / "evaluate"），
    否则 langgraph 编译期就会报错。
    """
    if state["messages"][-1].tool_calls:
        return "tool_node"
    return "evaluate"


def route_review(state: OverAllState) -> str:
    """evaluate 之后的条件分支：决定收尾、重生成还是转人工。

    state: 图状态。读 force_exit / is_need_evaluate / iteration / score。

    按顺序短路判断（先命中先返回）：force_exit → 不需要评审 → 迭代超
    MAX_ITERATION → 分数 ≥ HIGH → 分数 ≥ LOW → 兜底转人工。即"高分直通、
    中间分重生成、低分交人"，且四条退出条件都排在转人工之前 ——
    顺序即策略，调换会改变行为。

    返回键为 "end" / "regenerate" / "human"。
    """
    if state["force_exit"]:
        return "end"
    if not state["is_need_evaluate"]:
        return "end"
    if state["iteration"] >= MAX_ITERATION:
        return "end"
    if state["score"] >= HIGH:
        return "end"
    if state["score"] >= LOW:
        return "regenerate"
    return "human"


def after_human(state: OverAllState) -> str:
    """human_review 之后的条件分支：人工接受还是打回。

    state: 图状态。只读 human_action —— 等于 "accept" 返回 "end"，其它任何值
        （含空串、None）都返回 "revise" 回到 generate。默认偏向"继续修订"。
    """
    return "accept" if state["human_action"] == "accept" else "revise"


# ----------------------------------------------------------------------------
# 构图
# ----------------------------------------------------------------------------
def build_graph(model=None, checkpointer=None, tools=None, retriever=None):
    """构建并编译图。

    model:        ChatOpenAI 实例（测试可传 MagicMock）
    checkpointer: langgraph checkpointer（测试可用 MemorySaver）
    tools:        工具列表（默认 []，与 notebook 一致）
    retriever:    可选，注入自定义检索器（测试用）；传 None 则走懒加载单例
    """
    global MODEL, TOOLS, _RETRIEVER
    if model is not None:
        MODEL = model
    if tools is not None:
        TOOLS = tools
    if retriever is not None:
        _RETRIEVER = retriever
    if checkpointer is None:
        checkpointer = MemorySaver()  # 默认内存存储：便于测试/本地运行；生产传 PostgresSaver

    b = StateGraph(state_schema=OverAllState)
    b.add_node("init", init)
    b.add_node("retrieve", retrieve)
    b.add_node("generate", generate)
    b.add_node("evaluate", evaluate)
    b.add_node("tool_node", ToolNode(tools=TOOLS))
    b.add_node("human_review", human_review)

    b.add_edge(START, "init")
    b.add_edge("init", "retrieve")
    b.add_edge("retrieve", "generate")
    b.add_conditional_edges("generate", route_tool,
                            {"tool_node": "tool_node", "evaluate": "evaluate"})
    b.add_edge("tool_node", "generate")
    b.add_conditional_edges("evaluate", route_review,
                            {"end": END, "regenerate": "generate", "human": "human_review"})
    b.add_conditional_edges("human_review", after_human,
                            {"accept": END, "revise": "generate"})

    return b.compile(checkpointer=checkpointer)
