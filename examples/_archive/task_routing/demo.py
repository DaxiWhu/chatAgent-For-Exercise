"""可运行的最终形态演示：四条支路各跑一遍（**不需要真实模型与语料**）。

    python -m examples.task_routing.demo

替身模型与替身检索器只用来证明**图结构与路由判据**是对的 ——
它们被刻意做成"每次调用都计数"，因为本样例要证明的第一件事是：

> direct 支路**一次**检索都不发（`retriever.calls` 不增加）。

这条用"断言 context 为空"是证明不了的：检索失败时 context 也为空 ⇒ 假绿。
"""
from __future__ import annotations

from langchain_core.messages import AIMessage

from . import nodes as N
from .graph import ask, build_graph

# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------
_CHUNKS = [
    {"source": "梧州特产.md", "source_path": "data/梧州特产.md", "chunk_id": "c1",
     "content": "梧州特产有六堡茶、龟苓膏与砂糖橘。六堡茶属黑茶类，产于苍梧县六堡镇。"},
    {"source": "六堡茶工艺.md", "source_path": "data/六堡茶工艺.md", "chunk_id": "c2",
     "content": "六堡茶采用渥堆发酵，成品具有红浓陈醇的特点，越陈越香。"},
]


class FakeRetriever:
    """计数替身：`calls` 是"这条支路到底有没有检索"的唯一证据。"""

    def __init__(self, chunks):
        self.chunks = chunks
        self.calls = 0

    def search(self, query, top_k=3):
        self.calls += 1
        return [(c, round(0.95 - i * 0.1, 4)) for i, c in enumerate(self.chunks[:top_k])]


class _Struct:
    def __init__(self, result):
        self._r = result

    def invoke(self, *a, **k):
        return self._r


class FakeModel:
    """按 prompt 内容分派返回值的替身模型。"""

    def __init__(self, *, answer="梧州的特产包括六堡茶、龟苓膏与砂糖橘。",
                 direct_answer="好的，我来回答这个问题。",
                 revised="（修订后的回答）",
                 code="def solve():\n    return 42\n", score=90, need_evaluate=True,
                 critique_new_rounds=1):
        self.answer = answer
        self.direct_answer = direct_answer
        self.revised = revised
        self.code = code
        self.score = score
        self.need_evaluate = need_evaluate
        self.critique_new_rounds = critique_new_rounds
        self.critique_calls = 0

    @staticmethod
    def _text(messages):
        if isinstance(messages, str):
            return messages
        if isinstance(messages, list):
            parts = []
            for m in messages:
                if isinstance(m, dict):
                    parts.append(m.get("content", "") or "")
                else:
                    parts.append(getattr(m, "content", "") or "")
            return " ".join(parts)
        return ""

    def invoke(self, messages):
        text = self._text(messages)
        if "只输出代码本身" in text:
            return AIMessage(content=self.code)
        if "输出【完整的新版本】" in text:
            return AIMessage(content=self.revised)
        if "参考以下资料作答" in text:
            return AIMessage(content=self.answer)      # rag 支路：带资料
        return AIMessage(content=self.direct_answer)   # direct 支路：无资料

    def with_structured_output(self, schema):
        name = getattr(schema, "__name__", "")
        if name == "ReviewResult":
            return _Struct(N.ReviewResult(is_need_evaluate=self.need_evaluate,
                                          score=self.score, feedback="建议更具体一些"))
        if name == "CritiqueResult":
            self.critique_calls += 1
            if self.critique_calls <= self.critique_new_rounds:
                return _Struct(N.CritiqueResult(has_new_points=True,
                                                issues=[f"第{self.critique_calls}轮发现的问题"],
                                                feedback="请补充具体论据"))
            return _Struct(N.CritiqueResult(has_new_points=False, issues=[], feedback=""))
        raise ValueError(f"未支持的结构化输出：{name}")


# ---------------------------------------------------------------------------
# 演示
# ---------------------------------------------------------------------------
def _show(title, state, retriever):
    plan = state.get("route_plan") or {}
    verdict = state.get("exit_verdict") or {}
    stats = state.get("retrieval_stats") or {}
    print(f"\n{'=' * 74}\n{title}")
    print(f"  route     : {state.get('route'):<7} source={state.get('route_source')} "
          f"conf={plan.get('confidence')} elapsed={plan.get('elapsed_ms')}ms")
    print(f"  why       : {plan.get('why')}")
    print(f"  needs     : {plan.get('needs')}  budget={plan.get('budget', {}).get('max_iterations')}")
    needs = plan.get("needs") or {}
    if state.get("route") == "direct":
        print(f"  检索       : 未发生（retriever.calls 累计 {retriever.calls}）")
    elif not needs.get("retrieval", True):
        print(f"  检索       : 未发生（该支路 needs.retrieval=False，"
              f"retriever.calls 累计 {retriever.calls}）")
    else:
        print(f"  检索       : hits={stats.get('hits')} empty={stats.get('empty')} "
              f"degraded={stats.get('degraded')} ctx_chars={stats.get('context_chars')} "
              f"retriever.calls 累计 {retriever.calls}")
    print(f"  citations : {len(state.get('citations') or [])} 条")
    print(f"  exit      : reason={state.get('exit_reason')} iterations={state.get('iteration')}")
    print(f"  verdict   : ok={verdict.get('ok')} issues={verdict.get('issues')}")
    out = (state.get("output") or "").strip().replace("\n", " ")
    print(f"  output    : {out[:70]}{'…' if len(out) > 70 else ''}")


def main():
    retriever = FakeRetriever(_CHUNKS)
    model = FakeModel(answer="梧州的特产包括六堡茶、龟苓膏与砂糖橘。", score=90)
    sandbox = lambda code: {"passed": True, "stdout": "1 passed", "stderr": ""}  # noqa: E731

    g = build_graph(model=model, retriever=retriever, sandbox=sandbox)

    print("路由样例：四条支路，同一个图。")
    _show("① 简单寒暄 → direct", ask(g, "你好", thread_id="d1"), retriever)
    _show("② 专业问题 → rag", ask(g, "梧州有哪些特产？根据文档回答", thread_id="d2"), retriever)
    _show("③ 写作类 → write（reflection 循环）",
          ask(g, "帮我写一篇介绍六堡茶的短文", thread_id="d3"), retriever)
    _show("④ 写代码 → code（执行反馈）",
          ask(g, "运行一下这段代码，跑个单元测试看有没有问题", thread_id="d4"), retriever)

    # ⑤ fail-safe：任何规则都没命中 ⇒ 走 rag，而不是 direct
    _show("⑤ 判据未命中 → fail-safe 到 rag",
          ask(g, "我想了解一下这个系统在长时间运行之后内存占用会不会持续增长"
                 "以及有没有相关的观测手段能看到这一点", thread_id="d5"), retriever)

    # ⑥ 多轮继承：单看"再详细点"是短句，但必须继承上一轮的 rag
    ask(g, "梧州有哪些特产？", thread_id="d6")
    _show("⑥ 续问继承上一轮 needs",
          ask(g, "再详细点，说说六堡茶的工艺", thread_id="d6"), retriever)


if __name__ == "__main__":
    main()
