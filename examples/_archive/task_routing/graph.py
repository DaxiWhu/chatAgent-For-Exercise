"""图组装：四条支路的最终拓扑。

```
START → init → route_task ─┬─ "direct" ─────────────────────────→ generate ─→ finish → END
                           │
                           ├─ "rag" ──→ retrieve ─→ generate ─→ evaluate ─→ route_review ─┐
                           │                                                             │
                           │                              ┌─评分低且高风险─→ human_review ┤
                           │                              └─评分中间──────→ generate ◄───┘
                           │
                           ├─ "write" → [retrieve?] → generate ─→ critique ─┬─ 无新意见 → finish
                           │                                              └─ 有新意见 → generate
                           │
                           └─ "code" ─→ write_code ─→ execute ─┬─ 测试绿 → finish
                                                               └─ 未通过 → write_code
```

三条设计约束写进结构里
----------------------
1. **direct 复用 `generate`**，不新开节点：`generate` 在 `context == ""` 时本来就
   走"直传 messages"的分支 ⇒ 直答支路 = 无上下文的 generate，只是跳过 retrieve。
   两份实现必然漂移。
2. **所有支路都汇到 `finish`**：出口共享（观测/审核/审计/配额只实现一次），
   但判据在 `nodes.EXIT_JUDGES` 里**按支路分治**。
3. **回边只回"该回的那个节点"**：rag 回 `generate`、write 回 `generate`、
   code 回 `write_code` —— 回错节点会让"重试"变成"从头再来"。
"""
from __future__ import annotations

import logging

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from . import nodes as N
from .state import OverAllState

log = logging.getLogger(__name__)


def build_graph(model=None, retriever=None, sandbox=None, checkpointer=None, tools=None):
    """构建并编译图。

    model:       对话模型（需具备 invoke / with_structured_output）
    retriever:   检索器（需具备 search(query, top_k)）；direct 支路**不会**碰它
    sandbox:     代码执行沙箱；**不传则代码支路不执行**（见 nodes.execute 的安全边界）
    checkpointer: 默认 MemorySaver（本地/测试）；生产传 AsyncPostgresSaver
    tools:       工具列表，默认 []
    """
    N.bind(model=model, retriever=retriever, sandbox=sandbox)
    if checkpointer is None:
        checkpointer = MemorySaver()

    b = StateGraph(state_schema=OverAllState)
    for name, fn in (
        ("init", N.init), ("route_task", N.route_task), ("retrieve", N.retrieve),
        ("generate", N.generate), ("evaluate", N.evaluate), ("critique", N.critique),
        ("write_code", N.write_code), ("execute", N.execute),
        ("tool_node", ToolNode(tools=tools or [])),
        ("human_review", N.human_review), ("finish", N.finish),
    ):
        b.add_node(name, fn)

    # --- 入口 ---
    b.add_edge(START, "init")
    b.add_edge("init", "route_task")
    # route_task 是**唯一的走向决定点**
    b.add_conditional_edges("route_task", N.after_route_task, {
        "retrieve": "retrieve", "generate": "generate", "write_code": "write_code",
    })

    # --- rag / write 共用的检索 → 生成 ---
    b.add_edge("retrieve", "generate")
    b.add_conditional_edges("generate", N.after_generate, {
        "tool_node": "tool_node", "evaluate": "evaluate",
        "critique": "critique", "finish": "finish",
    })
    b.add_edge("tool_node", "generate")     # 工具结果回灌后重新生成

    # --- rag 支路的循环（信号：评审分数）---
    b.add_conditional_edges("evaluate", N.after_review, {
        "finish": "finish", "regenerate": "generate", "human": "human_review",
    })
    b.add_conditional_edges("human_review", N.after_human, {
        "finish": "finish", "revise": "generate",
    })

    # --- write 支路的循环（信号：critic）---
    b.add_conditional_edges("critique", N.after_critique, {
        "revise": "generate", "finish": "finish",
    })

    # --- code 支路的循环（信号：执行结果）---
    b.add_edge("write_code", "execute")
    b.add_conditional_edges("execute", N.after_execute, {
        "revise": "write_code", "finish": "finish",
    })

    # --- 统一出口 ---
    b.add_edge("finish", END)

    compiled = b.compile(checkpointer=checkpointer)
    log.info("图已编译：%d 个节点，四条支路（direct/rag/write/code）", 11)
    return compiled


def ask(graph, query, thread_id="default", tenant=None):
    """便捷入口：跑一轮对话并返回最终 state。

    tenant 走 `config["configurable"]` 而**不是** state —— 租户是运行参数，
    写进 state 会被检查点持久化，续跑时可能带着过期值。
    """
    cfg = {"configurable": {"thread_id": thread_id}}
    if tenant is not None:
        cfg["configurable"]["rag_tenant"] = tenant
    from langchain_core.messages import HumanMessage
    return graph.invoke({"messages": [HumanMessage(content=query)]}, config=cfg)
