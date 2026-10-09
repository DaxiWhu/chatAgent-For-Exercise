"""入口可用性探测与降级 —— "走不了 agent 链路"的三种情形必须各自可辨。

⚠️ 2026-10-02 返工：入口**不再做内容分诊**。
此前这里会额外调一次模型判断"这次要一份作品、还是一段说明"，据此把"说明类"请求
推给原问答链路。那不是官方形态 —— 官方 ``examples/customer_service`` 的入口 agent
**自己就是分诊者**（同时带 tools 与 handoffs），代码里不存在第二层内容判定。
现在改为：**所有请求都进 agent 链路**，由前台自己决定要不要叫帮手、叫哪一个；
原问答链路只保留为"agent 不可用"时的**兜底**。

于是本文件守着三件事：

1. **三种"用不了"的情形各自可辨**，且都不抛异常（静默降级）；
2. **跨文件字符串契约**：``src/graph.py`` **产生** reason，``app/main.py`` **匹配**
   reason，中间没有任何东西保证两边一致 —— 任一边改了字面量，指标就**静默消失**；
3. **惰性导入与观测出口的老契约**：没装 SDK 也不能让 ``src.graph`` 导入失败。

``dispatch`` 自本轮起是**同步**的（不再 await 任何东西）。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from src import graph as G
from src.agents import runtime as agruntime
from tests._agents_testkit import ScriptedModel


def _state(text: str) -> dict:
    return {"messages": [HumanMessage(content=text)]}


@pytest.fixture
def runtime_guard():
    """保存/恢复 runtime 的全局状态。

    `agruntime` 是**进程级单例**（模型槽位），而 monkeypatch 只能撤销
    `setattr`，撤不掉"调过 set_model/reset"这件事。少了这个 fixture，
    本文件里某个用例的 `reset()` 会污染同一会话里后面的用例 ——
    而症状是"单独跑绿、全量跑红"这种最难查的一类。
    """
    saved_model = agruntime.get_model()
    saved_ready = agruntime.is_ready()
    yield
    if saved_model is not None:
        agruntime.set_model(saved_model)
    elif not saved_ready:
        agruntime.reset()


# --------------------------------------------------------------------------- #
# 降级：三种"走不了"的原因必须各自可辨
# --------------------------------------------------------------------------- #
def test_dispatch_is_rag_when_switch_off(monkeypatch):
    """主动关掉开关 ⇒ 走原问答链路，reason=disabled。

    这是运维**意图**，不是故障 —— 服务层刻意不把它计进可用性指标。
    """
    monkeypatch.setattr(G, "AGENTS_ENABLED", False)
    assert G.dispatch(_state("写一篇关于灯塔的短篇小说")) == {
        "route": "rag", "route_reason": "disabled",
    }


def test_dispatch_degrades_when_module_unavailable(monkeypatch, runtime_guard):
    """运行时报告缺 SDK ⇒ 单独一档 ``module_unavailable``。

    这一档的处置是**装包**，与"改配置"不是同一类动作。压进 ``runtime_not_ready``
    会让值班的人去翻 .env，而真正该做的是 pip install。
    """
    monkeypatch.setattr(G, "AGENTS_ENABLED", True)
    G._AGENT_MODULES.clear()
    monkeypatch.setattr(agruntime, "is_ready", lambda: False)
    monkeypatch.setattr(
        agruntime, "ready_reason", lambda: "missing_sdk: No module named 'agents'"
    )

    got = G.dispatch(_state("写一篇关于灯塔的短篇小说"))
    assert got == {"route": "rag", "route_reason": "module_unavailable"}


def test_dispatch_degrades_when_runtime_not_ready(monkeypatch, runtime_guard):
    """装了 SDK 但没装配成功（没配 Key 等）⇒ 回落问答链路，而不是报错。"""
    monkeypatch.setattr(G, "AGENTS_ENABLED", True)
    G._AGENT_MODULES.clear()
    agruntime.reset()

    got = G.dispatch(_state("写一篇关于灯塔的短篇小说"))
    assert got == {"route": "rag", "route_reason": "runtime_not_ready"}


# --------------------------------------------------------------------------- #
# 就绪：**任何**输入都进 agent 链路 —— 入口不再替用户分类
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    ["写一篇关于灯塔的短篇小说", "限流是怎么算的", "用 Python 写一个快速排序", "你好"],
)
def test_dispatch_sends_every_request_to_the_agent_link(text, monkeypatch, runtime_guard):
    """★ 本次改造的落点：入口只回答"链路能不能用"，不回答"这句话该走哪条路"。

    四种输入（创作 / 问原理 / 写代码 / 打招呼）得到**同一个结论**。
    空脚本同时证明 **dispatch 一次模型调用都不花** —— 一旦它偷偷调模型，
    ``ScriptedModel`` 会因为脚本用尽而断言失败。
    """
    monkeypatch.setattr(G, "AGENTS_ENABLED", True)
    G._AGENT_MODULES.clear()
    agruntime.set_model(ScriptedModel([]))

    assert G.dispatch(_state(text)) == {"route": "agent", "route_reason": "ready"}


# --------------------------------------------------------------------------- #
# 跨文件契约：reason 的字面量两边必须一致
# --------------------------------------------------------------------------- #
def test_dispatch_reasons_match_the_service_layer_vocabulary(monkeypatch, runtime_guard):
    """★ 跨文件**字符串契约**：`dispatch` 报的 reason，服务层必须认得出。

    `src/graph.py` **产生** reason，`app/main.py` **匹配** reason，中间没有任何东西
    保证两边一致 —— 任一边改了字面量，指标就**静默消失**：降级真的发生了、日志一行
    没有、`AgentLinkDegraded` 告警永远不响。而"降级面不可观测"正是这条链路的头号故障。

    所以这里**真跑 `dispatch`**（而不是抄一份字面量），把产出的 reason 原样喂回服务层，
    再断言指标落地。两边同时写错也照样会被这条抓住。
    """
    from app import main as app_main
    from app import metrics

    produced: dict[str, str] = {}

    monkeypatch.setattr(G, "AGENTS_ENABLED", False)
    produced["disabled"] = G.dispatch(_state("写小说"))["route_reason"]

    monkeypatch.setattr(G, "AGENTS_ENABLED", True)
    G._AGENT_MODULES.clear()

    monkeypatch.setattr(agruntime, "is_ready", lambda: False)
    monkeypatch.setattr(agruntime, "ready_reason", lambda: "missing_sdk: boom")
    produced["module_unavailable"] = G.dispatch(_state("写小说"))["route_reason"]

    monkeypatch.setattr(agruntime, "ready_reason", lambda: "not_configured")
    produced["runtime_not_ready"] = G.dispatch(_state("写小说"))["route_reason"]

    # ① 字面量必须与服务层的契约常量一致（不是"看起来像"，是**相等**）
    for key in ("module_unavailable", "runtime_not_ready"):
        assert produced[key] in app_main._AGENT_UNAVAILABLE_REASONS, (
            f"{key} 的 reason 文字与服务层白名单不一致 —— 该指标会静默消失"
        )
    # 主动关开关是运维意图，**不能**进故障指标
    assert produced["disabled"] not in app_main._AGENT_UNAVAILABLE_REASONS

    # ② 端到端：真实产出的 reason 喂进去，一个都不许"落空"
    metrics.reset()
    for reason in produced.values():
        app_main._record_agent_route(reason)

    assert metrics.value("agent_unavailable_total",
                         reason=produced["module_unavailable"]) == 1
    assert metrics.value("agent_unavailable_total",
                         reason=produced["runtime_not_ready"]) == 1
    assert metrics.value("agent_unavailable_total", reason=produced["disabled"]) == 0


def test_classify_failed_is_gone():
    """`classify_failed` 与 `agent_classify_errors_total` 已随内容分诊一起删除。

    留着其中一个（或哪天被重新引入）会让值班的人按告警去查一个**不存在的失败模式**：
    入口已经没有"判定要作品还是要说明"这次模型调用了，自然不存在"判定失败"。
    """
    from app import main as app_main

    assert not hasattr(app_main, "_AGENT_CLASSIFY_FAILED")
    assert "classify_failed" not in app_main._AGENT_UNAVAILABLE_REASONS


# --------------------------------------------------------------------------- #
# 状态机分支
# --------------------------------------------------------------------------- #
def test_route_dispatch_maps_both_ways():
    """agent 分支的落点是**前台节点**，不是被拆掉的那个旧节点名。

    ⚠️ 这条断言不是"改个名字"：`route_dispatch` 返回的字符串是**图的节点名**，
    写错一个字母的后果是 `add_conditional_edges` 找不到目标 —— 报错还算好，
    若哪天有人把不存在的名字配成 `END` 兜底，就变成"所有创作请求静默走进 RAG 链路"。
    """
    assert G.route_dispatch({"route": "agent"}) == "frontline"
    assert G.route_dispatch({"route": "rag"}) == "retrieve"
    # 缺字段（老检查点）必须退到原链路，不能抛异常
    assert G.route_dispatch({}) == "retrieve"


@pytest.mark.parametrize(
    "state,expect",
    [
        ({"force_exit": True, "is_need_evaluate": True, "score": 99}, "end"),
        ({"force_exit": False, "is_need_evaluate": False, "score": 99}, "end"),
        ({"force_exit": False, "is_need_evaluate": True, "score": G.HIGH}, "end"),
        ({"force_exit": False, "is_need_evaluate": True, "score": G.HIGH - 1}, "human"),
    ],
)
def test_route_agent_review_never_regenerates(state, expect):
    """agent 链路的收尾分支**不产生 regenerate**。

    评审（``review_draft``）已经在同一次 run 里由**模型自己**调过了；再回到 RAG 的
    generate 等于用另一条链路的生成器去改一份 agent 产出的作品 —— 上下文对不上。
    """
    assert G.route_agent_review(state) == expect
    assert G.route_agent_review(state) in ("end", "human")


def test_route_revise_follows_the_turn_route():
    """被打回的意见必须回到**本轮的执行者**，两条链路不能互相串。

    ⚠️ 打回**只回前台**，不回文学节点。理由：人工给的是"要改成什么样"这种
    面向最终交付的意见，只有前台手里有完整的会话与交付上下文；回文学节点等于
    让一段既拿不到意见全貌、又看不见本轮对话的创作直接重写。
    """
    assert G.route_revise({"route": "agent"}) == "frontline"
    assert G.route_revise({"route": "rag"}) == "generate"
    assert G.route_revise({}) == "generate"


def test_init_clears_per_turn_review_fields():
    """`init` 必须清掉上一轮的人工打回痕迹。

    不清的后果很具体：agent 链路靠 `feedback_source == "human"` 判断"本轮被人工
    打回了"，上一轮打过回时这个值会一直留着 ⇒ **本轮第一次生成就莫名其妙地带上
    上一轮的意见**。与 iteration/force_exit 是同一类跨轮次污染。
    """
    out = G.init({})
    for key in ("iteration", "force_exit", "feedback", "feedback_source",
                "human_action", "agent_guardrail"):
        assert key in out
    assert out["feedback"] == "" and out["feedback_source"] == ""
    # current_agent **不能**被清：它记录"上一轮结束在哪个 agent"，正是要跨轮次保留的
    assert "current_agent" not in out


# --------------------------------------------------------------------------- #
# 惰性导入：没装 SDK 也不能让 src.graph 导入失败
# --------------------------------------------------------------------------- #
def test_agents_package_is_importable_without_the_sdk():
    """`src.agents` 的公开名字必须是**惰性**的。

    若 `__init__.py` 里 eager import runner/tools，那么没装 `openai-agents` 时
    `from .agents import frontline_agent` 会连带把 SDK 拉进来 → `src.graph` 导入
    失败 → **服务起不来**。而这本是一个可选能力。
    """
    import src.agents as pkg

    assert pkg.TaskContext.__name__ == "TaskContext"    # 惰性取到真对象
    assert pkg.is_ready() in (True, False)              # 无 SDK 时也只是 False
    with pytest.raises(AttributeError):
        _ = pkg.this_name_does_not_exist


# --------------------------------------------------------------------------- #
# 观测出口
# --------------------------------------------------------------------------- #
def test_observability_is_a_noop_without_a_sink():
    """没注入 sink 时链路层的上报必须是空操作，且**绝不抛异常**。

    指标是"观察故障"的组件。让它成为故障源（甚至把业务请求带崩）是最讽刺的一种
    失败 —— 所以这里连"没注入"这个最普通的情形也要钉住。
    """
    from src import observability

    saved = observability.get_sink()
    observability.reset_sink()
    try:
        observability.counter("x_total")
        observability.histogram("y_seconds", 1.0)
        observability.gauge("z", 1)
        assert observability.get_sink() is None
    finally:
        observability.set_sink(saved)


def test_observability_swallows_sink_errors():
    """sink 自己抛异常时也不能穿透到业务代码。"""
    from src import observability

    class _Boom:
        def counter(self, *a, **k):
            raise RuntimeError("sink down")

        def histogram(self, *a, **k):
            raise RuntimeError("sink down")

        def gauge(self, *a, **k):
            raise RuntimeError("sink down")

    saved = observability.get_sink()
    observability.set_sink(_Boom())
    try:
        observability.counter("x_total")      # 不该抛
    finally:
        observability.set_sink(saved)
