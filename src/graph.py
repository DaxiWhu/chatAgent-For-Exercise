"""chatAgent 图逻辑（从 chatAgent.ipynb 提炼，作为唯一真源）。

与 notebook 的区别：
- model / checkpointer / tools / retriever 均可注入，便于测试（build_graph 参数）。
- evaluate 的 except 分两层：先接解析/校验类异常，再接 API/超时/限流等通用异常，
  均为「重试 N 次后降级 force_exit」，符合生产化讨论结论。
- retrieve 节点自带容错：知识库未配置或检索失败时降级为空上下文，不让整图崩。
- 不在此处连接数据库；checkpointer 由调用方注入（测试用 MemorySaver）。

两条链路（2026-10-01 起，2026-10-02 定形）
----------------------------------------
``init → dispatch`` 的判定**只剩一件事**：agent 链路现在**能不能用**。

* ``agent``（默认路径）：``frontline → [literary]`` —— 走 ``src/agents``（OpenAI
  Agents SDK 官方原语装配的 agent 链路）。**所有请求都走这里**，问答、写代码、
  打招呼都一样："这句话该由谁处理"由**前台自己**决定（它每一步给一个结构化判定），
  代码里不存在第二层内容判定。
* ``rag``（**兜底**）：``retrieve → generate → evaluate → …`` —— 原有问答链路，
  带 token 级流式、引用校验、评审分级、人工回路，**一字未改**。只在 agent 链路
  走不了（开关关掉 / 缺 SDK / 没装配成功）时启用。

agent 链路的形状（2026-10-03 起）
--------------------------------
**每个会接管对话的 agent 是图里一个独立节点**：

    frontline ──(读 agent_decision.action)──▶ literary
        │                                        │
        └────────────▶（收尾判定）◀──────────────┘

* ``frontline``：每轮的入口，一次运行给出一个 ``types.FrontlineDecision``
  （这一步是答复还是转交）。答复 ⇒ 进收尾判定；转交 ⇒ 跳 ``literary``。
* ``literary``：文学创作专家，一次运行产出作品本身，跑完进同一个收尾判定。

为什么拆、"该转给谁"这个信号从哪来、以及每轮只能落一次输入这三件事（连同实测
数据）都记在 ``DESIGN_agent_architecture.md``。节点内部由 ``src/agents/runner.py``
的 ``run_agent`` 跑**一个** agent 的**一次**运行。

⚠️ 由此得到一条硬约束：**agent 节点内部不能再出现 handoff**。SDK 的 handoff 会在
同一个节点里把人换完再继续跑（``run.py`` 里换人后是 ``continue``），节点边界随之
消失 —— 拆了等于没拆。守卫见 ``tests/test_agents_pipeline.py`` 的接线断言。

⚠️ 另一条：**续跑边界就是节点边界**。实测（``tmp_probe/probe_node_boundary_resume.py``）
崩在文学节点时，检查点停在 ``next=('literary',)``、前台那份产出已在盘上 ——
续跑从文学节点开始，前台不重跑、也不会重新判定一遍。

⚠️ 第三条：**节点里的异常要抛出去**（``AgentRunFailed``），不许在节点里兜成一句"友好
话术"再正常收尾 —— 兜住之后图会照常走到 END，检查点里"待执行节点"为空，查询接口报
``status="completed"``、``resumable=false``，客户端连"继续生成"的按钮都不弹。
代价是这类请求对客户端表现为一次**失败**（刻意换的，理由见 ``AgentRunFailed``）。

⚠️ 第四条：**每个节点各建一份 ``TaskContext``**（SDK 没有跨运行共享上下文），
所以跨节点要延续的东西（``citations`` / ``assets`` / ``agent_stats``）必须从 state
读回来 —— 见 ``_agent_context``。漏了全是静默错：文学那次运行的引用校验会把"本轮真的
检索过"的编号判成编造。

跨轮对话（2026-10-02 定形）
--------------------------
两件事配套，缺一都会让"接着上一轮继续说"变成一句空话：

1. **入口固定为前台**：每轮都由前台接话，它再决定叫哪个帮手、要不要转交。
   刻意**不**跟"上一轮结束在哪个 agent"（官方两个多轮示例都跟，本项目的取舍理由见
   ``src/agents/runner.py`` 的模块 docstring）。state 里的 ``current_agent`` 因此
   降级为**纯观测**。
2. **历史交给官方 ``Session``**：走 ``configurable.thread_id`` 取会话，由 SDK 负责
   取历史、与本轮合并、去重、写完回写。⚠️ 因此**每轮只有第一个 agent 节点**送本轮
   新增的那一句（``append_prompt=True``），后续节点送空 —— 会话里的历史已经在 SDK
   手里，再送一份就是重放（且轮次越多重复越多）。实现与后端选择见
   ``src/agents/session.py``，落点的判断见 ``runner._build_input_items``。

⚠️ 2026-10-02 来回走过两种错法，**都别再重走**：

1. 最初 ``dispatch`` 用**三条正则**判"是不是创作类" —— 把路由降级成自定义词表，
   不是官方做法；
2. 中途改成 ``triage_agent`` 出结构化判定（``RouteDecision(task_types)``）——
   判据确实交给了模型，但**在入口 agent 之外又插了一层"先分类、再分派"**，
   仍不是官方形态（官方入口 agent 自己就是分诊者，不需要别人先分一次类）。

``classify`` / ``RouteDecision`` / ``CREATIVE_KINDS`` / ``classify_failed`` /
``agent_classify_errors_total`` 已随第 2 种错法一起删净，留档在
``examples/_archive/router_triage/``。

⚠️ 由此带出一条硬约束：``dispatch`` **不是** ``async``，也**不许**变回 ``async`` ——
它一次模型调用都不花。谁把它改成 ``async def``，几乎一定意味着"内容判定"又被塞回
了入口。守卫见 ``tests/test_graph.py::test_dispatch_is_a_pure_sync_probe``。

开关是 ``AGENTS_ENABLED``（默认开）。"开着但没配"时（没装 ``openai-agents``
或没配 API Key）``runtime.is_ready()`` 为假 ⇒ **全部请求走 rag**，行为与改造前
逐字节一致 —— 这就是"能力开关默认开、但开着且没配时行为必须与关一致"。

为什么 agent 只拆到"会接管对话的那一级"，而不是把工具调用也拆成节点
------------------------------------------------------------------
会话状态、检查点、中断恢复（``/v1/review``）、配额、幂等、限流全部长在 LangGraph
这一层。agent 节点跑的是"一次 SDK 运行"，这些基础设施原样复用；反过来另起一套
编排就要把它们全部重做一遍。官方也明确支持 Agents SDK 嵌进其他编排框架。

**不把每次工具调用也做成节点**（那样 SDK 的 run loop 就完全不需要了）的判据：
工具级带来的实际收益只有一条 —— 断点能细到"某一次工具调用"，而本项目单轮是秒级
问答，没有"长到会中途断"的任务去消费它；代价却是把工具循环、换人时的历史裁剪、
三级护栏、跨轮会话、``as_tool`` 这些 SDK 已经做好的事全部自己重写一遍。
判据与取舍见 ``DESIGN_agent_architecture.md``。
"""
import logging
import os
import time
from typing import Literal

from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import observability, prompts
from .envutil import env_int
from .rag import citation_check, guard
from .rag.parse_cache import load_chunks_cached
from .rag.retrieval import (
    build_citations,
    build_retriever,
    context_block,
    load_chunks,
    tenant_label,
)

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------
LOW = 60
HIGH = 80
LLM_TRY_TIME = 5
MAX_ITERATION = 10
ERROR_REPORT = "服务出现问题请稍后重试"
GUARDRAIL_REFUSAL = "抱歉，这个请求包含试图覆盖系统指令的内容，我无法处理。"

# RAG：语料目录（可用 DOCS_DIR 覆盖）与召回条数
DOCS_DIR = os.getenv("DOCS_DIR", "data")
TOP_K = 3

# ----------------------------------------------------------------------------
# agent 链路开关（请求默认全部走 src/agents；见模块 docstring）
# ----------------------------------------------------------------------------
# 与 app/config.py 的 AGENTS_ENABLED 是**同一个**环境变量。链路层直接读它，
# 与 DOCS_DIR 的处理方式一致（config.py 那份用于启动自检回显）。
AGENTS_ENABLED = (os.getenv("AGENTS_ENABLED", "1").strip().lower()
                  not in ("0", "off", "false", "no"))
AGENT_ASSET_DIR = os.getenv("AGENT_ASSET_DIR", "var/agent_assets")


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
    feedback: str | None = Field(default=None)  # 打回时必填


class OverAllState(MessagesState):
    output: str = ""
    context: str = ""          # RAG 检索到的参考资料（送模型的文本）
    citations: list = []       # 引用的结构化来源信息（给用户回溯核对用）
    # 引用校验结论（见 src/rag/citation_check.py）：citations 非空只说明"检索命中了
    # 东西"，推不出"答案用了它"。"引用带了"与"有依据"必须分开报。
    citation_check: dict = {}
    is_need_evaluate: bool = False
    score: int = 0
    feedback: str = ""
    human_action: str = ""
    iteration: int = 0
    feedback_source: str = ""
    force_exit: bool = False
    # ---- agent 链路（route == "agent" 时才有值）----
    route: str = ""            # "agent" / "rag"
    route_reason: str = ""     # 降级原因（指标标签）；没降级时是 "ready"
    # 上一轮 run **结束在哪个 agent**（= 官方 `result.last_agent.name`）。
    # ⚠️ 2026-10-02 起这是**纯观测字段**：入口固定为前台，它**不参与路由**。
    # 别再用它决定"下一轮谁先接话"—— 那是本次刻意去掉的语义（理由见
    # src/agents/runner.py 的模块 docstring "入口固定为前台"）。
    current_agent: str = ""
    assets: list = []          # 本轮落盘的资产路径
    agent_stats: dict = {}     # src/agents 的运行统计（普通 dict，可序列化）
    agent_guardrail: str = ""  # 被哪条出口护栏拦下
    # 前台节点那一步的**判定**（2026-10-03 拆成多节点后新增）：
    # ``{"action": "reply"|"transfer", "content": ...}``，由 ``route_frontline`` 消费。
    # ⚠️ 它与 ``current_agent`` 分工不同：那个是"本轮结束在哪个 agent"（观测），
    # 这个是"这一步往哪走"（**调度**）。混用的后果是观测字段悄悄变成了路由输入 ——
    # 而那正是 2026-10-02 刻意去掉的语义。
    agent_decision: dict = {}



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
_TENANT_CACHE_MAX = env_int("RAG_TENANT_CACHE_MAX", 8, lo=1)


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
    租户字符串来自请求。走 JWT 时它是令牌里已验签的 claim，走脚本通行证时是
    调用方自报 —— 后者**不受任何约束**，所以这里必须自己设上限，不能靠
    "调用方永远守规矩"：超限时丢弃一个旧分片而不是无限增长 —— 内存涨到 OOM
    是比"偶尔重建检索器"严重得多的故障。丢弃后下次访问会重建（语料已在
    _CHUNKS 里，代价可控）。
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
                         len(chunks), DOCS_DIR, tenant_label(None))
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
                 len(chunks), tenant_label(key))
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


def _last_user_content(messages) -> str:
    """取**最后一条用户消息**的文本（不是"最后一条消息"）。

    为什么必须区分：人工打回那一轮，``frontline`` / ``literary`` 已经把本轮草稿写回
    messages（一条 ``AIMessage``），"最后一条"取到的是**草稿**。把它当用户输入送进
    agent 链路，等于让模型以为自己刚说的那段话是用户说的 —— 而这个错误**不会报错**，
    只会让打回后的新版本莫名其妙。

    空列表 / 没有用户消息都返回 ""，调用方（两个 agent 节点）据此走"没有新输入"分支。
    """
    for m in reversed(list(messages or [])):
        if isinstance(m, dict):
            if (m.get("role") or "") == "user":
                return m.get("content", "") or ""
            continue
        if isinstance(m, HumanMessage):
            return getattr(m, "content", "") or ""
    return ""


# ----------------------------------------------------------------------------
# 节点
# ----------------------------------------------------------------------------
def init(state: OverAllState) -> OverAllState:
    """图入口节点：把**跨轮次会累积**的字段复位。

    state: 图状态。**只读入参、不读任何字段** —— 它存在是为了显式重置
        iteration / force_exit / 评审反馈 这几样：同一 thread_id 多轮对话时它们会随
        checkpoint 带过来，不清就会让上一轮的失败、迭代次数、甚至**上一轮的人工
        打回意见**污染本轮。

        新增 `feedback` / `feedback_source` / `human_action` 的原因与此完全一致：
        agent 链路要靠 `feedback_source == "human"` 判断"本轮是不是被人打回了"，
        而上一轮打过回时这个值会一直留在 state 里 —— 结果是**本轮第一次生成就
        莫名其妙地带上上一轮的意见**。这与 iteration/force_exit 是同一类 bug。

        刻意**不**重置 `current_agent`：它记录"上一轮 run 结束在哪个 agent"。
        它已经不参与路由（入口固定为前台），保留它是为了能回答"这轮谁收的尾"。

        ⚠️ `messages` 同样刻意**不**重置 —— 它不是"跨轮累积的垃圾"，而是
        **跨轮对话本身**：agent 链路要取"最后一条用户消息"作为本轮新输入
        （见 `_last_user_content`），问答链路的 `retrieve` 也要用它当检索 query。
        把它一起复位掉，会让每轮都从零开始 —— 而那正是本次要修的毛病。

        `agent_stats` / `assets` / `agent_guardrail` 属于**同一条**：它们只在
        agent 链路（``frontline`` / ``literary`` 两个节点）里被写，而**兜底**的问答
        链路整条不碰它们。同一 thread_id 先走一轮 agent 链、第二轮**没走**（链路不可用
        降级 / 主动关了开关），这三样就会**从上一轮残留下来** —— 接口层读
        ``agent_stats`` 会把上一轮的统计当成本轮的报给指标，"没走 agent 的请求不产生
        agent 指标"这条口径随之失效（比率被稀释）。复位成空值是让"本轮没走 agent"
        这件事**可被如实读出**。

        ``agent_decision`` 与它们同一类，但理由更硬：它是**路由输入**（条件边读它决定
        跳文学节点还是收尾）。不复位的话，上一轮那次"转交"会一直留在 state 里 ——
        本轮前台若因为护栏/超轮数提前收尾（没走到写判定的那一步），
        ``route_frontline`` 就会读到**上一轮**的转交，把一个已经结束的回合拐到文学
        节点去。它与 iteration/force_exit 是同一类跨轮污染。

        （2026-10-02 起**所有**请求默认都走 agent 链，但降级路径依然存在，
        所以这条复位仍然吃重 —— 别因为"现在都走 agent 了"就把它删掉。）
    """
    return {
        "iteration": 0,
        "force_exit": False,
        "feedback": "",
        "feedback_source": "",
        "human_action": "",
        "agent_guardrail": "",
        "agent_stats": {},
        "assets": [],
        "agent_decision": {},
    }


# ----------------------------------------------------------------------------
# agent 链路：惰性加载（不装 openai-agents 时本图必须照常工作）
# ----------------------------------------------------------------------------
_AGENT_MODULES: dict = {}


def _agent_module(name: str):
    """惰性导入 ``src.agents`` 下的子模块；任何失败都降级为 None。

    为什么必须惰性：``src/agents/*`` 里除了 ``runtime`` 之外都在导入期
    ``from agents import ...``（openai-agents SDK）。若在模块级 import，SDK 没装的
    环境连"导入 ``src.graph``"都会失败 —— 服务直接起不来，而这本是一个可选能力。

    为什么要缓存失败（把 False 存进去）：否则每条请求都要重试一次注定失败的 import，
    异常开销会挂在热路径上。
    """
    if name not in _AGENT_MODULES:
        try:
            from importlib import import_module
            _AGENT_MODULES[name] = import_module(f".agents.{name}", __package__)
        except Exception as e:  # noqa: BLE001  SDK 缺失 / 版本不符都在这
            logging.warning("agent 链路的 %s 模块不可用（本次进程内不再重试）：%s", name, e)
            _AGENT_MODULES[name] = False
    return _AGENT_MODULES[name] or None


def dispatch(state: OverAllState) -> OverAllState:
    """入口节点：只回答"agent 链路**现在能不能用**"，不回答"这句话该走哪条路"。

    写回 route / route_reason。三种"用不了"的情形各自有独立的 reason：

      - ``disabled``             : ``AGENTS_ENABLED=0``
      - ``module_unavailable``   : 运行时报告缺 SDK（``missing_sdk``）
      - ``runtime_not_ready``    : 装了但没装配成功（没配 API Key / client 建不起来）

    三者都是**持续不可用**（要运维动配置），所以共用一条降级告警；但它们的**处置
    动作不同**（装包 vs 改 .env），因此 reason 必须分开 —— 否则"要不要人工介入、
    介入做什么"就失去了依据。``route_reason`` 在本节点里**只是降级原因**，
    没降级时是 ``ready``。

    ⚠️ 2026-10-02 按用户要求变更：**本节点不再做内容分流。**
    此前这里会额外交给一个模型判断"这次要一份作品、还是一段说明"，并把"说明类"
    请求推给原问答链路。那不是官方形态 —— 官方 ``examples/customer_service`` 的
    入口 agent **自己就是分诊者**（它同时带 tools 与 handoffs），代码里不存在第二层
    内容判定。现在改为：**所有请求都进 agent 链路**，由前台自己决定要不要叫帮手、
    叫哪一个；原问答链路只保留为"agent 不可用"时的**兜底**。

    随之消失的两样东西（别在别处再引用）：
      * ``creative_*`` / ``no_creative_cue`` 这类**命中原因**；
      * ``classify_failed`` —— 已经不剩任何"内容判定"可失败。

    为什么不再 ``async``：本节点不再 await 任何东西。此前是 async 只因为要 await
    那次判定；一个不 await 的 async 节点只会让 langgraph 白走一层协程调度。
    """
    kind, reason = "rag", "disabled"
    if AGENTS_ENABLED:
        runtime = _agent_module("runtime")
        if runtime is None:
            # ``runtime`` 刻意不依赖 SDK，正常导入不会失败；留着这一支是为了
            # "模块在导入期炸掉"这种情形仍然可辨，而不是并进 runtime_not_ready。
            reason = "module_unavailable"
        elif not runtime.is_ready():
            # 细分原因由 runtime 自己给（缺 SDK / 缺 Key / 装配失败）。只有
            # "缺 SDK"单列一档：它的处置是**装包**，与"改配置"不是同一类动作。
            detail = str(runtime.ready_reason() or "")
            reason = (
                "module_unavailable"
                if detail.startswith("missing_sdk")
                else "runtime_not_ready"
            )
        else:
            kind, reason = "agent", "ready"
    return {"route": kind, "route_reason": reason}


def route_dispatch(state: OverAllState) -> str:
    """dispatch 之后的条件分支。返回键必须是 ``add_conditional_edges`` 里登记过的。"""
    return "frontline" if state.get("route") == "agent" else "retrieve"


async def compact_history(state: OverAllState) -> OverAllState:
    """图里的**历史压缩**节点（入口一·图状态侧）：把老对话换成一段摘要。

    为什么需要它
    -----------
    ``messages`` 在图里是**累加**的，而检查点**每走完一个节点就整份落一次盘** ⇒
    库里存的量随轮数**平方**增长（实测数据见 ``src/agents/compaction.py`` 的模块
    docstring）。这个节点把"前 N 轮之前"的老对话换成一条摘要，此后每份快照只带
    「摘要 + 最近 N 轮」，**增长阶从平方降到线性**。

    位置为什么在 ``init`` 与 ``dispatch`` 之间
    ----------------------------------------
    * ``dispatch`` 之后图就分叉，而 **rag 链路一个 agent 节点都不经过** —— 挂在
      agent 节点上的话，rag 链路（它恰恰是"历史全在 ``messages`` 里"的那条）
      永远压不到。
    * ⚠️ **不能塞进 ``dispatch``**：那个节点的 docstring 专门写了"为什么不再
      async"（它不 await 任何东西），而写摘要必须调模型 ⇒ 只能另起一个 async 节点。
    * 放在 ``dispatch`` **之前**，而且**两条链路都经过**，正好一次覆盖完。

    为什么要 ``async`` 而不是放到后台
    -------------------------------
    两条：它必须**影响本轮接下来的节点**（``retrieve`` 拿最后一条当检索词、
    ``generate`` 把它送进模型），所以本轮就得是压完的；而且**改检查点只能由图自己
    写** —— 检查点同时是图自己的账本（"图跑到哪了"就记在里面），后台任务去改它
    等于同一条会话出现两个写者，``parent_checkpoint_id`` 那条链还会被改断。
    （会话表那一份没有这个问题：它只是个仓库，所以那一侧才是后台任务。）

    只动 ``messages``
    ----------------
    其它字段（``output`` / ``citations`` / ``score``…）不是"对话"，压不得。
    返回 ``{}`` = 什么都不改（没开开关 / 轮数没攒够 / 摘要失败），此时**不产生任何
    state 写入**，检查点不会因此多落一份快照。

    与人工打回的关系
    ---------------
    打回那一轮靠"**原话已经躺在会话里**"决定不重传原话（见
    ``src/agents/runner._build_input_items``）。压缩保留**最近 N 轮**（N ≥ 1，由
    ``AGENT_HISTORY_KEEP_ROUNDS`` 兜底），而被打回的那一轮永远是最新的 ⇒ 原话必然
    在保留段里。所以这里**不需要**为打回轮单独设门槛：触发条件到了就压。
    """
    messages = state.get("messages") or []
    if not messages:
        return {}
    try:
        from .agents import compaction
    except Exception as e:  # noqa: BLE001  压缩内核装不上（不该发生）⇒ 按原样继续
        logging.warning("历史压缩内核不可用，本轮不压：%s", e)
        return {}
    try:
        replacement = await compaction.compact_graph_messages(list(messages))
    except Exception as e:  # noqa: BLE001  压缩失败**绝不能**带崩整轮
        logging.warning("历史压缩失败，本轮按原样继续：%s", e)
        return {}
    if not replacement:
        return {}
    return {"messages": replacement}


class AgentRunFailed(RuntimeError):
    """某次 agent 运行以**异常**收尾 —— 故意让它冒到图外。

    为什么不像以前那样"在节点里兜住、写一句错误提示、正常收尾"
    --------------------------------------------------------
    agent 链路内部异常（限流 / 超时 / 协议不兼容 / 代码 bug）被兜住时，节点会照常
    返回、图会照常走到 END ⇒ 检查点里"待执行节点"是空的 ⇒ 查询接口报
    ``status="completed"``、``resumable=false`` —— **客户端连"继续生成"的按钮都不会
    弹**，而这次请求其实什么都没产出。

    冒出来之后：检查点停在**当前节点之前**（实测：三节点图里崩在 b，落盘
    ``next=('b',)``），查询接口因此报 ``incomplete``，``/v1/chat/resume`` 能真正续上。

    代价（知情后接受）：这类请求在客户端不再表现为"一段友好的错误提示"，而是一次
    **失败**（SSE 下是一个脱敏的 ``error`` 事件，见 ``app/main.py`` 的收流分支）。
    这是刻意换的 —— 把"没跑成"伪装成"跑成了"，会让观测面与重试逻辑一起失真。

    ⚠️ **只包异常**。"超轮数"与"护栏拦截"仍然是正常的收尾路径（见 ``_agent_stop``）：
    它们有明确的结论要告诉用户，不是"什么都没发生"。
    """


def _agent_runner():
    """取 agent 链路的运行器模块；不可用时**抛**（不静默产出空答案）。

    这条路**理论上不可达**（``dispatch`` 已经把"链路不可用"封掉了）。留着它是为了
    让"判定与执行用了不同判据"这类未来的改动立即暴露 —— 那时的正确行为是失败，
    而不是让用户收到一句看起来正常的空答复。
    """
    runner = _agent_module("runner")
    if runner is None or _agent_module("runtime") is None:
        raise RuntimeError("dispatch 判为 agent 链路但模块不可用")
    return runner


def _agent_context(state: OverAllState, config: RunnableConfig | None):
    """装配 agent 链路的共享上下文，返回 ``(ctx, session_id)``。

    两个 agent 节点共用这一份装配 —— 各写一份必然漂，而漂的后果是"文学路径少带了
    租户或会话号"，读代码几乎看不出来（两边看起来都很正常）。

    config: langgraph 注入的运行配置。两个值都从 ``configurable`` 取，与 ``retrieve``
        完全一致（都不进 state，见那里的说明）：

        * ``rag_tenant`` —— 检索租户。运行参数，不是会话状态：写进 state 会被检查点
          持久化，续跑时可能带着一个过期的租户值。
        * ``thread_id`` —— **会话编号**，直接交给跨轮历史（见 ``src/agents/session.py``）。
          用 thread_id 而不是另起一个 id：它已经唯一标识"这段对话"，多一个 id
          只会多一处对不上的机会。
    """
    from pathlib import Path

    from .agents.context import TaskContext, stats_from_dict  # 本模块不支持 SDK，可安全导入

    tenant = None
    session_id = ""
    if isinstance(config, dict):
        conf = config.get("configurable")
        if isinstance(conf, dict):
            tenant = conf.get("rag_tenant")
            session_id = conf.get("thread_id") or ""

    ctx = TaskContext(
        tenant=tenant or "",
        docs_dir=DOCS_DIR,
        # 用工厂而不是实例：检索器按租户懒加载 + 有界缓存（见 get_retriever），
        # 在这里就取等于把"可能用不上"的那次构建提前做掉。
        retriever_factory=(lambda t=tenant: get_retriever(t)),
    )
    # 资产目录按需创建（配置里给的是路径，这里不 mkdir —— 真的产出资产时工具会建）
    ctx.asset_dir = Path(AGENT_ASSET_DIR)

    # ★ 把**前一个 agent 节点**留下的东西续上（2026-10-03，拆成多节点后必须做）
    #
    # 每个节点是一次独立的 SDK 运行，各建一份 TaskContext —— SDK 里没有"跨运行共享
    # 上下文"这回事。不续的后果全是静默的：
    #   * ``citations``（引用记录）：出口护栏与接口层都读它，不续 ⇒ 文学节点那次运行的
    #     引用校验会把"本轮真的检索过"的编号判成编造，把正常产出整条拦下；
    #   * ``stats``：接口层的参与者/转移/模型调用次数只统计到最后一个节点；
    #   * ``assets``：文学节点会把前台落的资产从视图里抹掉。
    # 三样都在 state 里有对应字段，所以续的方式就是"从 state 读回来"。
    ctx.citations = list(state.get("citations") or [])
    ctx.assets = list(state.get("assets") or [])
    ctx.stats = stats_from_dict(state.get("agent_stats") or {})

    # 只有"本轮被人工打回"才把意见带给 agent。上一轮留下的 feedback 已被 init 清掉，
    # 所以这里不需要再判 iteration。
    if state.get("feedback_source") == "human" and state.get("human_action") == "revise":
        ctx.human_feedback = state.get("feedback") or ""
    return ctx, session_id


def _agent_base(state: OverAllState, outcome, ctx) -> dict:
    """把一次运行的结果翻成两个 agent 节点**共用**的那部分 state 写入。"""
    stats = ctx.stats.as_dict()
    # 路由结论由 dispatch 决定，runner 不知道（也不该知道）——在这里补进统计，
    # 这样"哪个降级原因产出了哪种结局"在指标里能直接对得上。
    stats["route"] = state.get("route") or "agent"
    stats["route_reason"] = state.get("route_reason") or ""
    return {
        "agent_stats": stats,
        "assets": list(ctx.assets),
        "citations": list(ctx.citations),
        # ★ 官方口径：状态只有一个来源 —— **本次运行结束在哪个 agent**。
        # 每个节点各写一次，最后一次写下的就是"这轮谁收的尾"。run 没跑到底时
        # outcome.final_agent 为空串，此时**不覆盖**，保持原值（官方异常路径也不会
        # 更新 current_agent）。
        "current_agent": outcome.final_agent or (state.get("current_agent") or ""),
        "agent_guardrail": outcome.guardrail or "",
        "iteration": outcome.attempts,
    }


def _agent_stop(outcome, base) -> OverAllState | None:
    """处理"这次运行没有可交付答案"的三种收尾；没命中时返回 ``None``。

    三种都是**确定的结论**，各自要给用户不同的东西：

    * 入口护栏 ⇒ 一句明确的拒绝（而不是空答复）；
    * 出口护栏 ⇒ 空答复 + 由接口层下一个明确的 error 事件（**被拒的候选输出不成为
      交付物**，官方语义 —— 不能拿一句被判定为编造的答复交付）；
    * 超轮数 ⇒ 通用错误提示（模型绕圈没收敛，按错误收尾）。

    ⚠️ 三处都顺手把 ``agent_decision`` 清空：这几条路径**没有**产生判定，
    留着旧值会让条件边读到一个不属于本次的结论。
    """
    if outcome.exit_reason == "input_guardrail":
        return {**base, "agent_decision": {}, "output": GUARDRAIL_REFUSAL,
                "force_exit": True, "is_need_evaluate": False}
    if outcome.exit_reason == "output_guardrail":
        return {**base, "agent_decision": {}, "output": "", "force_exit": True,
                "is_need_evaluate": False}
    if outcome.exit_reason == "max_turns":
        logging.warning("agent 链路未正常收尾（max_turns），按通用错误收尾")
        return {**base, "agent_decision": {}, "output": ERROR_REPORT,
                "force_exit": True, "is_need_evaluate": False}
    return None


async def frontline(state: OverAllState, config: RunnableConfig | None = None) -> OverAllState:
    """前台节点：跑一次前台 agent，产出「答复」或「转交文学作者」的判定。

    state: 图状态。读 messages（取"最后一条用户消息"作为本轮新输入）、
        feedback/feedback_source（人工打回时的意见）。
        ⚠️ **不读 current_agent** —— 入口固定为前台，那个字段只是观测。
    config: 见 ``_agent_context``。

    写回：output / messages / citations / citation_check / score / is_need_evaluate /
    current_agent / assets / agent_stats / agent_guardrail / agent_decision。

    ⚠️ **只把"本轮新增的那一句"送进去**（``append_prompt`` 走默认的 True），
    历史由会话负责。后续节点（文学）才送空 —— 把 messages 整份转成输入会与会话里的
    历史**叠加**，等于每一轮都重放一遍对话。

    ⚠️ 判定为转交时**不写 output、也不追加助手消息**：这一步没有给用户的东西。
    检查点里"转交给了谁"由**下一跳**表达（落盘那一行的 ``next=('literary',)``），
    不是一条独立的记录（实测见 ``tmp_probe/probe_node_boundary_resume.py``）。

    为什么产出不在这里拦、不在这里改：图内部改写文案会让"模型实际产出了什么"变得
    不可复现。拦截口只有两个 —— 出口护栏（在 SDK 里，交付前）与接口层
    （``app/main.py``，按 ``citation_check`` 决定是否下发引用）。
    """
    runner = _agent_runner()
    ctx, session_id = _agent_context(state, config)

    outcome = await runner.run_agent(
        runner.ENTRY_AGENT,
        prompt=_last_user_content(state["messages"]),
        context=ctx,
        session_id=session_id,
    )
    if outcome.decision.get("action") == "transfer":
        # 跨节点转移补记一条"谁交给了谁"：SDK 侧已经没有 handoff 事件了
        # （它不再是 handoff 工具，而是图的一条边），但 `agent_handoff_total`
        # 这个运维契约的语义不变 —— 它回答的是"多少请求被交给了专家"。
        # ⚠️ 必须在 `_agent_base` **之前**记：那一步会把 stats 拍成 dict 写进 state。
        ctx.stats.handoffs.append(
            (runner.ENTRY_AGENT.name, runner.LITERARY_AGENT.name)
        )
    base = _agent_base(state, outcome, ctx)
    if outcome.exit_reason == "error":
        # ★ 冒泡（不是"写句错误提示正常收尾"）：只有冒出去，检查点才会留下
        # 待执行节点、续跑才真的可用。见 AgentRunFailed 的说明。
        raise AgentRunFailed(outcome.error or "agent 运行失败")
    stop = _agent_stop(outcome, base)
    if stop is not None:
        return stop

    decision = dict(outcome.decision)
    if decision.get("action") == "transfer":
        return {**base, "agent_decision": decision}

    check = citation_check.verify(outcome.answer, ctx.citations, docs_dir=DOCS_DIR)
    evaluation = ctx.recall("evaluation")
    return {
        **base,
        "agent_decision": decision,
        "output": outcome.answer,
        "messages": [AIMessage(content=outcome.answer)],
        "citation_check": check,
        # 评审结论接到主链路沿用的一套状态字段上：这样 route_agent_review 与
        # route_review 用的是同一套阈值口径，运维不需要记两套规则。
        "is_need_evaluate": True,
        "score": outcome.score_hint(),
        "feedback": getattr(evaluation, "feedback", "") or "",
        "feedback_source": "ai",
    }


async def literary(state: OverAllState, config: RunnableConfig | None = None) -> OverAllState:
    """文学节点：跑一次文学 agent，产出作品本身。

    与前台节点的差别只有两处，都写在下面（其余写法逐字相同，共用上面的帮手）。

    * ``append_prompt=False`` —— 本轮那句用户输入**在前台节点就已经写进会话了**，
      再传一遍会在同一个 thread 下留下两条一模一样的用户消息（实测：
      ``tmp_probe/probe_session_semantics.py``）。会话不可用时 runner 会回退成
      "自己带上"，否则模型什么都看不到 —— 见 ``runner._build_input_items``。
    * 它产出的是**作品正文**（该 agent 没有 ``output_type``），直接写进 output 与
      messages。

    ⚠️ 它**没有回程边**（不会在本轮里再转回前台）。入口已经固定为前台，下一轮用户
    说话自然回到前台；加回程边会让图出现环，还要额外配一个打转上限 ——
    而那正是 2026-10-03 用户拍板丢掉的能力。
    """
    runner = _agent_runner()
    ctx, session_id = _agent_context(state, config)

    outcome = await runner.run_agent(
        runner.LITERARY_AGENT,
        prompt=_last_user_content(state["messages"]),
        context=ctx,
        session_id=session_id,
        append_prompt=False,
    )
    base = _agent_base(state, outcome, ctx)
    if outcome.exit_reason == "error":
        raise AgentRunFailed(outcome.error or "agent 运行失败")
    stop = _agent_stop(outcome, base)
    if stop is not None:
        return stop

    check = citation_check.verify(outcome.answer, ctx.citations, docs_dir=DOCS_DIR)
    evaluation = ctx.recall("evaluation")
    return {
        **base,
        "output": outcome.answer,
        "messages": [AIMessage(content=outcome.answer)],
        "citation_check": check,
        "is_need_evaluate": True,
        "score": outcome.score_hint(),
        "feedback": getattr(evaluation, "feedback", "") or "",
        "feedback_source": "ai",
    }


def route_frontline(state: OverAllState) -> str:
    """前台节点之后的条件分支：转交文学节点，还是就地收尾 / 转人工。

    为什么"转交"**排在**收尾判定之前：转交这一步**没有交付物**，
    ``route_agent_review`` 会按分数与 ``force_exit`` 判它 —— 而"该交给文学专家"
    根本不是一次"产出不合格"，送去人工复核会让每一次正常转交都卡在审核队列里。

    返回键为 "literary" / "end" / "human"（登记见 ``build_graph``）。

    ⚠️ 判决来自 ``state["agent_decision"]`` —— 它是**前台节点亲手写回来的**结构化
    判定（``types.FrontlineDecision``），不是从模型输出里现读。这与"路由判据必须落在
    图的状态里"是同一条：不写回状态，检查点里就看不见"为什么跳到了下一个节点"。
    """
    if state.get("force_exit"):
        return "end"
    decision = state.get("agent_decision") or {}
    if isinstance(decision, dict) and decision.get("action") == "transfer":
        return "literary"
    return route_agent_review(state)


def route_agent_review(state: OverAllState) -> str:
    """agent 链路收尾时的条件分支：收尾还是转人工。

    挂在**两个出口**上：前台直接答复那条（``route_frontline`` 兜底走到这里）、
    以及文学节点那条（``literary → route_agent_review``）。

    与 ``route_review`` 的差别只有一处：**没有 "regenerate"**。不是省略 —— agent
    链路的产出**没法交给 RAG 的 ``generate`` 节点改**：那是另一个模型、另一套上下文，
    改出来的东西与 agent 自己写的不一致。要改只能由 agent 链路自己改（模型在同一次
    run 里按 ``review_draft`` 的意见改；2026-10-02 起评审就是这个 ``as_tool`` 形态，
    代码侧**不再有**"回灌 Feedback 再来一轮"的循环）。

    所以这里只有两档：达标收尾；不达标交人（与 RAG 链路低分转人工的口径一致）。
    """
    if state["force_exit"]:
        return "end"
    if not state["is_need_evaluate"]:
        return "end"
    if state["score"] >= HIGH:
        return "end"
    return "human"


def refine(state: OverAllState) -> OverAllState:
    """人工打回之后的**路由占位节点**（不做任何事）。

    为什么需要它：``human_review`` 的"打回"该回到哪个执行者，取决于本轮走的是哪条
    链路（rag → ``generate``，agent → ``frontline``）。而 langgraph 的条件边只能
    映射到固定节点，没法在一条边上直接分叉 —— 所以中间垫一个空节点，
    由它的条件边来完成分叉。

    刻意让 ``after_human`` 保持原样（返回 "accept"/"revise"）：它的语义是"人工的
    决定是什么"，与"打回后该交给谁"是两件事，混在一处会让两条链路的策略互相污染。
    """
    return {}



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
        # 检索是本链路里**唯一可能静默失效**的环节：它挂了服务外观全绿
        # （探针绿、SSE 连得上、模型照常作答），只是回答里没有任何引用。
        # 所以这里的耗时与命中结果必须上报 —— 这是"检索成功率"的分母。
        started = time.perf_counter()
        hits = get_retriever(tenant).search(query, top_k=TOP_K)
        observability.histogram("rag_retrieval_duration_seconds",
                                time.perf_counter() - started, via="graph")
        observability.counter("rag_hits_total",
                              result="hit" if hits else "empty", via="graph")
        # 文本块优先用父窗/保真内容（见 context_block 的优先级说明）
        ctx = "\n\n".join(context_block(c) for c, _ in hits)
        citations = build_citations(hits)
        observability.histogram("rag_context_chars", len(ctx), via="graph")
        # 注入扫描（只告警，不改内容）：把"资料里夹带指令"这件事变成一条可见的日志。
        # 真正的结构隔离在 generate 里由 guard.wrap_context 完成 —— 扫描只是提高发现率。
        if ctx:
            scan = guard.scan(ctx)
            if scan.hit:
                logging.warning("检索上下文疑似提示词注入（risk=%s）：%s",
                                scan.risk, scan.matches[:3])
                observability.counter("rag_context_injection_total",
                                      risk=scan.risk, via="graph")
        if not ctx:
            logging.info("RAG 未召回任何片段，按无上下文继续")
        return {"context": ctx, "citations": citations}
    except Exception as e:  # noqa: BLE001
        # 降级要**可计数**：过去只有一行 warning，于是"检索一直失败"这件事
        # 在指标上看不出来（rag_hits_total 里也不会出现，因为压根没走到命中判定）。
        observability.counter("rag_retrieval_degraded_total", stage="graph_retrieve")
        observability.counter("rag_hits_total", result="error", via="graph")
        logging.warning("RAG 检索失败，降级为无上下文: %s", e)
        return {"context": "", "citations": []}


def _check_citations(state: OverAllState, draft) -> dict:
    """对刚产出的草稿做引用校验（**只算，不拦**），返回结论 dict。

    为什么在 generate 里而不是单独开一个节点：langgraph 的图与边是编译期固定的，
    加节点要动图；而校验需要的两样东西（新草稿 + 本轮 citations）在这一步刚好都齐，
    且**每轮都会跑到** —— 首轮与每次修订后的新版本都要判，漏一轮就等于漏掉那个版本。

    为什么不在图里"拦"：图内部改写文案会让"模型实际产出了什么"变得不可复现，
    而排查答案质量时第一件要看到的就是模型原文。拦截放在接口层
    （`app/main.py`，只对 `block_worthy` 的确凿错误生效）。

    任何异常都降级为 `{}`：**观测失败不能影响作答**（与项目其它降级口径一致）。
    """
    try:
        return citation_check.verify(
            draft if isinstance(draft, str) else "",
            state.get("citations") or [],
            docs_dir=DOCS_DIR,
        )
    except Exception as e:  # noqa: BLE001
        # 校验失败降级为"没结论"。**必须计数**：接口层只在 `check` 非空时上报
        # `citation_check_total{status}`，于是"校验一直抛异常"与"没跑校验"在指标上
        # 长得一模一样（都是**一条都没有**）—— 而这两件事的处置完全不同
        # （前者是代码缺陷，后者是没引用）。这个计数就是用来区分它们的。
        observability.counter("citation_check_errors_total")
        logging.warning("引用校验失败（不影响作答）：%s", e)
        return {}


def _timed_model_call(stage: str, call):
    """包住一次模型调用，记录耗时与失败（观测失败绝不能影响作答）。

    为什么不包装 `MODEL` 对象
    ----------------------
    `MODEL` 是可注入的（`set_model()` / `build_graph(model=...)`），测试里常是
    MagicMock —— 把注入对象换成包装器会连带改变注入语义。而调用点只有三处，
    显式包一层的改动面更小，也不容易被将来的重构绕过。

    标签 stage 的取值（有限）：
      "answer"   首轮带上下文作答 —— 失败意味着用户拿不到答案；
      "revise"   按评审/人工意见修订 —— 失败同样是交付失败；
      "evaluate" 自评审打分 —— 失败只丢评审（有 LLM_TRY_TIME 重试兜底）。
    不区分 stage 时，"模型开始变慢"与"评审环节在坏"长得一模一样。
    """
    t0 = time.perf_counter()
    try:
        return call()
    except Exception:
        observability.counter("llm_call_errors_total", stage=stage, via="graph")
        raise
    finally:
        # 失败也记耗时：超时型故障的耗时正是要看的那个数。
        observability.histogram("llm_call_duration_seconds",
                                time.perf_counter() - t0, stage=stage, via="graph")


def _compact_for_window(messages):
    """**入口二（rag 链路）**：将要送进模型的那份历史超阈值时先压一遍。

    只在该分支**真的会把历史整份送进模型**时调用：

    * 首轮"带资料作答"发出去的是一段**拼好的字符串**（资料 + 最后一条用户消息，
      见 ``prompts.answer_prompt``）—— 历史压根不进模型，压它没有意义；
    * 无资料的首轮、以及所有修订轮（``MODEL.invoke(历史 + 指令)``）才需要。

    压缩**只改这一次调用送进去的内容**，不写回 state：库里那份由 ``compact_history``
    节点（每个请求跑一次）负责，见 ``src/agents/compaction.py`` 的
    "为什么窗口压缩不做写回"。任何失败都原样返回 —— 压缩失败只该退化成"这轮没压"，
    不该让作答失败。
    """
    try:
        from .agents import compaction
    except Exception:  # noqa: BLE001  压缩内核装不上（不该发生）
        return messages
    try:
        replacement = compaction.compact_for_window_sync(list(messages or []))
    except Exception as e:  # noqa: BLE001
        logging.warning("窗口压缩失败，按原样送模型：%s", e)
        return messages
    return replacement if replacement else messages


def generate(state: OverAllState) -> OverAllState:
    """生成节点：首轮带 RAG 上下文作答，后续轮按评审/人工意见修订。

    state: 图状态。读 messages / context / iteration / feedback_source /
        feedback / output，写回 messages / iteration+1 / output。
          - iteration == 0：有 context 时用「参考以下资料作答」模板拼 prompt
            单独 invoke；无 context 时直传 messages；
          - iteration > 0：按 feedback_source（"ai" / 其它即人工）决定措辞，
            把意见包成一条 HumanMessage，明确要求输出【完整的新版本】而非 diff，
            与新草稿一并追加进历史。

    ⚠️ 2026-10-08：两处 ``MODEL.invoke`` 之前各加一道**窗口压缩**（``_compact_for_window``）。
    它判的是"将要送进模型的这份输入"的 token 数，超阈值就把老的那段换成摘要再送 ——
    压在 ``history_budget`` 的硬截断**之前**（先摘要、丢不动了再硬丢）。
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
            # 这条路送出去的是一段**拼好的字符串**：历史不进模型 ⇒ 没有可压的东西。
            prompt = prompts.answer_prompt(safe_ctx, _last_content(messages))
            res = _timed_model_call("answer", lambda: MODEL.invoke(prompt))
        else:
            compacted = _compact_for_window(messages)
            res = _timed_model_call("answer", lambda: MODEL.invoke(compacted))
        return {
            "messages": res,
            "iteration": iteration + 1,
            "output": res.content,
            "citation_check": _check_citations(state, res.content),
        }
    src = state.get("feedback_source", "ai")
    src_cn = "评审模型" if src == "ai" else "人工审核"
    instruction = HumanMessage(content=prompts.revise_prompt(
        state["output"], state["feedback"], src_cn))
    # 修订轮的输入 = 整份历史 + 指令（指令里还带着上一版草稿全文）⇒
    # 这是 rag 链路里**最大**的一次输入，也正是窗口压缩最该起作用的地方。
    revised_input = _compact_for_window(state["messages"]) + [instruction]
    res = _timed_model_call("revise", lambda: MODEL.invoke(revised_input))
    return {
        "messages": [instruction, res],  # 反馈指令 + 新草稿 一并追加进历史
        "iteration": iteration + 1,
        "output": res.content,
        # 修订轮同样要判：新草稿可能引入了原文里没有的说法
        "citation_check": _check_citations(state, res.content),
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
            result = _timed_model_call(
                "evaluate",
                lambda: MODEL.with_structured_output(ReviewResult)
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
        （含空串、None）都返回 "revise" 回到 refine。默认偏向"继续修订"。
    """
    return "accept" if state["human_action"] == "accept" else "revise"


def route_revise(state: OverAllState) -> str:
    """refine 之后的条件分支：被打回的意见该交给哪个执行者。

    state: 图状态。只读 route —— 本轮走的是哪条链路，打回就回到哪条链路的执行者。
        让两边互相串（agent 产出被 RAG 生成器改，或反之）会得到一个上下文完全对不上
        的新版本，而且很难从现象上看出原因。

    agent 那条回到 ``frontline``（本轮的入口节点）而不是"上一轮结束在谁那里"：
    入口固定为前台，打回也是一次新的轮次，判断"这活归谁"的仍然是前台。
    """
    return "frontline" if state.get("route") == "agent" else "generate"



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
    # 历史压缩：夹在复位节点与分流节点之间，**两条链路都经过**（rag 链路一个 agent
    # 节点都不走，挂别处就漏掉它）。它是 async 的，所以不能并进 `dispatch`。
    b.add_node("compact_history", compact_history)
    b.add_node("dispatch", dispatch)
    # agent 链路的两个节点：**每个会接管对话的 agent 一个节点**（2026-10-03 拆分）。
    # 拆的判据与代价见模块 docstring 与 DESIGN_agent_architecture.md。
    b.add_node("frontline", frontline)
    b.add_node("literary", literary)
    b.add_node("retrieve", retrieve)
    b.add_node("generate", generate)
    b.add_node("evaluate", evaluate)
    b.add_node("tool_node", ToolNode(tools=TOOLS))
    b.add_node("human_review", human_review)
    b.add_node("refine", refine)

    b.add_edge(START, "init")
    # init →（压缩）→ dispatch：压缩必须**先于**分流，见 compact_history 的 docstring。
    b.add_edge("init", "compact_history")
    b.add_edge("compact_history", "dispatch")
    # 入口判定：agent 链路可用 ⇒ 所有请求走它；不可用 ⇒ 退回问答链路兜底。
    b.add_conditional_edges("dispatch", route_dispatch,
                            {"frontline": "frontline", "retrieve": "retrieve"})
    # agent 链路第一跳：读前台的结构化判定 —— 转交 ⇒ 文学节点；答复 ⇒ 收尾判定。
    b.add_conditional_edges("frontline", route_frontline,
                            {"literary": "literary", "end": END, "human": "human_review"})
    # 文学节点是**终点节点**（没有回程边，见 literary 的 docstring）。
    # 它和"前台直接答复"共用同一个收尾判定：两条路都要回答"达标收尾还是交人"。
    b.add_conditional_edges("literary", route_agent_review,
                            {"end": END, "human": "human_review"})
    b.add_edge("retrieve", "generate")
    b.add_conditional_edges("generate", route_tool,
                            {"tool_node": "tool_node", "evaluate": "evaluate"})
    b.add_edge("tool_node", "generate")
    b.add_conditional_edges("evaluate", route_review,
                            {"end": END, "regenerate": "generate", "human": "human_review"})
    b.add_conditional_edges("human_review", after_human,
                            {"accept": END, "revise": "refine"})
    # 打回后按链路分叉：RAG 回合回 generate，agent 回合回**入口节点** frontline。
    b.add_conditional_edges("refine", route_revise,
                            {"generate": "generate", "frontline": "frontline"})

    return b.compile(checkpointer=checkpointer)
