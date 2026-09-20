"""chatAgent 图逻辑（从 chatAgent.ipynb 提炼，作为唯一真源）。

与 notebook 的区别：
- model / checkpointer / tools 均可注入，便于测试（build_graph 参数）。
- evaluate 的 except 用 `Exception`（覆盖 API/超时/限流 + LangChain 解析异常 +
  pydantic 校验错），符合生产化讨论结论。
- 不在此处连接数据库；checkpointer 由调用方注入（测试用 MemorySaver）。
"""
from typing import Optional, Literal

import logging

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import MessagesState, StateGraph, START, END
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel, Field, ValidationError, ConfigDict

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------
LOW = 60
HIGH = 80
LLM_TRY_TIME = 5
ERROR_REPORT = "服务出现问题请稍后重试"

# ----------------------------------------------------------------------------
# 可注入的全局模型（测试时由 build_graph(model=...) 覆盖）
# ----------------------------------------------------------------------------
MODEL = None
TOOLS: list = []


def set_model(m):
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
    is_need_evaluate: bool = False
    score: int = 0
    feedback: str = ""
    human_action: str = ""
    iteration: int = 0
    feedback_source: str = ""
    force_exit: bool = False


# ----------------------------------------------------------------------------
# 节点
# ----------------------------------------------------------------------------
def init(state: OverAllState) -> OverAllState:
    return {
        "iteration": 0,
        "force_exit": False,
    }


def generate(state: OverAllState) -> OverAllState:
    iteration = state["iteration"]
    if iteration == 0:
        messages = state["messages"]
        res = MODEL.invoke(messages)
        return {
            "messages": res,
            "iteration": iteration + 1,
            "output": res.content,
        }
    src = state.get("feedback_source", "ai")
    src_cn = "评审模型" if src == "ai" else "人工审核"
    instruction = HumanMessage(content=(
        f"以下是{src_cn}给出的修改意见，请据此修订你之前的草稿{state['output']}，"
        f"输出【完整的新版本】，不要只给片段或 diff：\n\n"
        f"{state['feedback']}"
    ))
    res = MODEL.invoke(state["messages"] + [instruction])
    return {
        "messages": [instruction, res],  # 反馈指令 + 新草稿 一并追加进历史
        "iteration": iteration + 1,
        "output": res.content,
    }


def evaluate(state: OverAllState) -> OverAllState:
    draft = state["output"]
    last_err = None
    for attempt in range(LLM_TRY_TIME):
        try:
            result = (MODEL.with_structured_output(ReviewResult)
                      .invoke(f"判断内容是否需要进行评审，如果需要对内容打分(0-100)并且给建议：\n{draft}"))
            return {
                "is_need_evaluate": result.is_need_evaluate,
                "score": result.score,
                "feedback": result.feedback,
                "feedback_source": "ai",
            }
        except Exception as e:  # 覆盖 API/超时/限流 + LangChain 解析异常 + pydantic 校验错
            last_err = e
            logging.warning("evaluate 调用或解析失败(第%d次): %s", attempt + 1, e)
    return {
        "force_exit": True,
        "output": ERROR_REPORT,
        "last_error": str(last_err),
    }


def human_review(state: OverAllState):
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
    if state["messages"][-1].tool_calls:
        return "tool_node"
    return "evaluate"


def route_review(state: OverAllState) -> str:
    if state["force_exit"]:
        return "end"
    if not state["is_need_evaluate"]:
        return "end"
    if state["iteration"] >= 10:
        return "end"
    if state["score"] >= HIGH:
        return "end"
    if state["score"] >= LOW:
        return "regenerate"
    return "human"


def after_human(state: OverAllState) -> str:
    return "accept" if state["human_action"] == "accept" else "revise"


# ----------------------------------------------------------------------------
# 构图
# ----------------------------------------------------------------------------
def build_graph(model=None, checkpointer=None, tools=None):
    """构建并编译图。

    model:        ChatOpenAI 实例（测试可传 MagicMock）
    checkpointer: langgraph checkpointer（测试可用 MemorySaver）
    tools:        工具列表（默认 []，与 notebook 一致）
    """
    global MODEL, TOOLS
    if model is not None:
        MODEL = model
    if tools is not None:
        TOOLS = tools
    if checkpointer is None:
        checkpointer = MemorySaver()  # 默认内存存储：便于测试/本地运行；生产传 PostgresSaver

    b = StateGraph(state_schema=OverAllState)
    b.add_node("init", init)
    b.add_node("generate", generate)
    b.add_node("evaluate", evaluate)
    b.add_node("tool_node", ToolNode(tools=TOOLS))
    b.add_node("human_review", human_review)

    b.add_edge(START, "init")
    b.add_edge("init", "generate")
    b.add_conditional_edges("generate", route_tool,
                            {"tool_node": "tool_node", "evaluate": "evaluate"})
    b.add_edge("tool_node", "generate")
    b.add_conditional_edges("evaluate", route_review,
                            {"end": END, "regenerate": "generate", "human": "human_review"})
    b.add_conditional_edges("human_review", after_human,
                            {"accept": END, "revise": "generate"})

    return b.compile(checkpointer=checkpointer)
