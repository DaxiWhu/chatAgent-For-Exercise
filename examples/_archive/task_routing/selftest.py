"""样例自检：把「这个设计凭什么是对的」钉成可执行的断言。

    python -m examples.task_routing.selftest

刻意**不用 pytest**、也不放进 `tests/`：`tests/` 是生产守卫的地盘，
样例的自检不该混进去影响全量回归的口径。这里用纯 assert，退出码即结论。

八条断言对应八个"删掉实现就会红"的守卫 —— 一个守卫如果删了实现还绿，
它就没在守任何东西。
"""
from __future__ import annotations

import json
import sys

from langchain_core.messages import HumanMessage

from . import nodes as N
from .graph import ask, build_graph
from .plan import DEFAULT_FALLBACK, make_plan
from .router import DecisionCache, decide
from .demo import _CHUNKS, FakeModel, FakeRetriever

FAILS = []


class RecordingModel(FakeModel):
    """记录模型**实际收到**的输入（第一条消息的内容）。

    存在的唯一理由：证明"路由器没改写要发给模型的 prompt"这件事，
    只能看模型收到了什么 —— state 里的 messages 是 append 语义，看不出改写。
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.seen = []

    def invoke(self, messages):
        if isinstance(messages, list) and messages:
            last = messages[-1]
            self.seen.append(last.get("content", "") if isinstance(last, dict)
                             else getattr(last, "content", ""))
        else:
            self.seen.append(messages if isinstance(messages, str) else "")
        return super().invoke(messages)


def check(name, cond, detail=""):
    if cond:
        print(f"  [OK]   {name}")
    else:
        FAILS.append(name)
        print(f"  [FAIL] {name} {detail}")


def fresh_retriever():
    return FakeRetriever(_CHUNKS)


def main():
    print("== 1. direct 支路：一次检索都不发 ==")
    # ⚠️ 断言的是**替身的调用计数**，不是"context 为空"——
    # 后者在检索失败时同样为真 ⇒ 假绿（检索挂了会伪装成"正确跳过了检索"）。
    r = fresh_retriever()
    g = build_graph(model=FakeModel(), retriever=r)
    s = ask(g, "你好", thread_id="s1")
    check("route == direct", s.get("route") == "direct", s.get("route"))
    check("retriever.calls == 0", r.calls == 0, f"calls={r.calls}")
    check("exit_reason == not_needed", s.get("exit_reason") == "not_needed", s.get("exit_reason"))

    print("== 2. rag 支路：恰好检索一次 ==")
    r = fresh_retriever()
    g = build_graph(model=FakeModel(score=90), retriever=r)
    s = ask(g, "梧州有哪些特产？根据文档回答", thread_id="s2")
    check("route == rag", s.get("route") == "rag", s.get("route"))
    check("retriever.calls == 1", r.calls == 1, f"calls={r.calls}")
    check("citations 非空", len(s.get("citations") or []) > 0)
    check("retrieval_stats 有出口", (s.get("retrieval_stats") or {}).get("hits") == 2)

    print("== 3. 判据未命中 → fail-safe 走 rag，不是 direct ==")
    q_fallback = ("我想了解一下这个系统在长时间运行之后内存占用会不会持续增长"
                  "以及有没有相关的观测手段能看到这一点")
    plan = decide(q_fallback)
    check("task == rag（fail-safe 偏多做）", plan["task"] == "rag", plan["task"])
    check("confidence == 0.0（承认自己不确定）", plan["confidence"] == 0.0, plan["confidence"])
    check("DEFAULT_FALLBACK 本身是 rag", DEFAULT_FALLBACK == "rag")

    print("== 4. plan 必须可 JSON 序列化 ==")
    # 进 state 就要过 checkpointer；不可序列化会在**续跑时**才炸。
    p = make_plan("rag", source="rule")
    try:
        json.dumps(p)
        ok = True
    except TypeError as e:
        ok, p_err = False, e
    check("json.dumps(plan) 不抛", ok, "" if ok else str(p_err))

    print("== 5. 路由器只读不写：不改写要发给模型的 messages ==")
    # 改写 prompt 会破坏 KV-cache 前缀复用（丢掉 60~90% 输入成本削减），
    # 且让"模型实际看到了什么"不可复现。
    #
    # ⚠️ **必须断言"模型实际收到的输入"，不能断言 `state["messages"][0]`。**
    # `MessagesState` 的 messages 是 **append-reducer**：路由器顺手追加一条
    # 改写过的消息时，第一条**仍然**是原文 ⇒ 断言第一条永远是绿的（假绿）。
    # 这个坑是变异 M3 抓出来的。
    r = fresh_retriever()
    m = RecordingModel()
    g = build_graph(model=m, retriever=r)
    raw = "  你好  "          # 前后带空白：若路由器顺手 strip 并注入，这条会红
    ask(g, raw, thread_id="s5")
    check("模型收到的最后一条消息未被改写", m.seen and m.seen[0] == raw, repr(m.seen[:1]))

    print("== 6. 多轮继承：续问继承上一轮支路 ==")
    r = fresh_retriever()
    g = build_graph(model=FakeModel(score=90), retriever=r)
    ask(g, "梧州有哪些特产？", thread_id="s6")
    before = r.calls
    s = ask(g, "再详细点，说说六堡茶的工艺", thread_id="s6")
    check("续问 route == rag", s.get("route") == "rag", s.get("route"))
    check("续问确实又检索了一次", r.calls == before + 1, f"{before} -> {r.calls}")

    print("== 7. 预算耗尽 ≠ 达标：两者必须可区分 ==")
    # score=70 落在 [LOW, HIGH) ⇒ 每轮都 regenerate，直到预算用尽。
    # 混报这两种退出会让调优方向完全错（以为该调 prompt，其实只是预算不够）。
    r = fresh_retriever()
    g = build_graph(model=FakeModel(score=70), retriever=r)
    s = ask(g, "梧州有哪些特产？根据文档回答", thread_id="s7")
    check("exit_reason == budget_exhausted", s.get("exit_reason") == "budget_exhausted",
          s.get("exit_reason"))
    check("iterations 用尽了预算", s.get("iteration") == 3, s.get("iteration"))

    print("== 8. code 支路：未注入沙箱 ⇒ 不执行 ==")
    # 示例代码会被复制到生产，一个"方便的内置执行器"就是定时炸弹。
    N.SANDBOX = None          # 显式清掉前面测试的注入（全局注入会跨用例残留）
    r = fresh_retriever()
    g = build_graph(model=FakeModel(), retriever=r)
    s = graph_invoke_code(g)
    ex = s.get("exec_result") or {}
    check("executed == False", ex.get("executed") is False, ex)
    check("verdict 里点名未执行", "not_executed" in (s.get("exit_verdict") or {}).get("issues", []),
          s.get("exit_verdict"))
    # ⚠️ 不可恢复的失败**不能**重试：否则一个"忘了注入沙箱"会变成 8 轮模型调用空转，
    # 还把真正的错误埋在第 8 次的日志里。
    check("没有无意义的重试 iteration == 1", s.get("iteration") == 1, s.get("iteration"))

    print("== 9. 路由缓存必须有界 ==")
    c = DecisionCache(size=4)
    for i in range(50):
        c.put(f"query-{i}", {"task": "rag", "why": "", "confidence": 1.0})
    check("len(cache) <= size", len(c) <= 4, len(c))

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


def graph_invoke_code(g):
    """单独包一层，便于第 8 条用一个明确的代码类 query。"""
    N.SANDBOX = None
    return ask(g, "这段代码跑一下单元测试看看", thread_id="s8")


if __name__ == "__main__":
    sys.exit(main())
