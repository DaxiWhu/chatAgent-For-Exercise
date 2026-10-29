"""LangSmith 转换层探针的守卫。

守的是什么
----------
`scripts/trace_probe.py` 不是生产代码，但它的**判据**必须可靠：它是"接上 LangSmith
之后到底能看见什么"的唯一验证手段。判据本身错了，接完会在 UI 里读出一棵错树却
毫无察觉 —— 这比不接更糟。

所以这里守三层：
  1. 树的重建（乱序到达的 run 能不能正确归到父上、孤儿能不能被认出来）；
  2. 判据本身（well-formed 的输入必须全过；缺 thread_id / 多根 / 没工具必须各自红）；
  3. 端到端（假模型真跑一次 SDK，确认官方处理器确实把 span 收下来了）。

第 3 条需要 `langsmith`（含 openai-agents 集成），未装则跳过 —— 与"可选依赖"的
口径一致：没装不是失败，是这一层能力不存在。
"""

from __future__ import annotations

import pytest

from scripts.trace_probe import build_tree, check, render_tree

try:
    import langsmith  # noqa: F401
    from langsmith.integrations.openai_agents_sdk import (
        OpenAIAgentsTracingProcessor,  # noqa: F401
    )

    HAS_LANGSMITH = True
except Exception:  # noqa: BLE001  没装 / 版本太旧，都算"这一层不存在"
    HAS_LANGSMITH = False


def _run(rid: str, parent: str | None = None, *, name: str = "node",
         rtype: str = "chain", thread: str | None = "t-1") -> dict:
    """造一条 create_run 的入参（只保留判据要读的字段）。"""
    metadata: dict = {}
    if thread is not None:
        metadata["thread_id"] = thread
    return {"id": rid, "parent_run_id": parent, "name": name, "run_type": rtype,
            "extra": {"metadata": metadata}}


def _ok(results: list[tuple[str, bool, str]]) -> dict[str, bool]:
    return {name: ok for name, ok, _ in results}


# --------------------------------------------------------------------------- #
# 一、树的重建
# --------------------------------------------------------------------------- #
def test_build_tree_links_children_regardless_of_arrival_order():
    """根 run 是**最后**才上报的（处理器要等 inputs 齐了才发），所以不能按顺序拼。"""
    runs = [
        _run("child", "root", name="frontline_agent"),
        _run("leaf", "child", name="kb_search", rtype="tool"),
        _run("root", None, name="chatagent_probe"),
    ]
    roots, orphans, nodes = build_tree(runs)

    assert [r["id"] for r in roots] == ["root"]
    assert orphans == []
    assert [c["id"] for c in roots[0]["children"]] == ["child"]
    assert [c["id"] for c in roots[0]["children"][0]["children"]] == ["leaf"]
    assert len(nodes) == 3


def test_build_tree_reports_orphans_instead_of_silently_dropping():
    """父 run 不在记录里 ⇒ 必须报出来。悄悄丢掉会让"残缺的树"看起来正常。"""
    runs = [_run("root", None), _run("lost", "missing-parent")]
    roots, orphans, _ = build_tree(runs)

    assert [r["id"] for r in roots] == ["root"]
    assert [o["id"] for o in orphans] == ["lost"]
    assert any("孤儿" in line for line in render_tree(roots, orphans))


# --------------------------------------------------------------------------- #
# 二、判据本身
# --------------------------------------------------------------------------- #
def test_check_passes_on_wellformed_runs():
    runs = [
        _run("root", None, name="wf"),
        _run("agent", "root", name="frontline_agent"),
        _run("tool", "agent", name="kb_search", rtype="tool"),
        _run("llm", "agent", name="Response", rtype="llm"),
    ]
    assert all(_ok(check(runs, workflow_name="wf", group_id="t-1",
                         expect_tool="kb_search", expect_llm=True)).values())


def test_check_fails_when_thread_id_missing():
    """`group_id` 没传 ⇒ 每条 run 都不带 thread_id ⇒ 多轮聚合失效。

    这是接 LangSmith 时最容易漏的一步：树照样出得来，只是散成一堆单轮记录。
    """
    runs = [_run("root", None, name="wf", thread=None),
            _run("tool", "root", name="kb_search", rtype="tool", thread=None)]
    results = _ok(check(runs, workflow_name="wf", group_id="t-1"))
    assert results["每条 run 都带 thread_id（多轮聚合靠它）"] is False


def test_check_fails_when_more_than_one_root():
    runs = [_run("root-a", None, name="wf"), _run("root-b", None, name="wf")]
    results = _ok(check(runs, workflow_name="wf", group_id="t-1"))
    assert results["有且只有一个根 run"] is False


def test_check_fails_when_root_name_is_not_workflow():
    runs = [_run("root", None, name="别的名字")]
    results = _ok(check(runs, workflow_name="wf", group_id="t-1"))
    assert results["根 run 用工作流名"] is False


def test_check_fails_when_tool_run_absent():
    """工具调用被翻成 chain（而不是 tool）时，UI 里就没有工具那一栏了。"""
    runs = [_run("root", None, name="wf"),
            _run("agent", "root", name="frontline_agent")]
    results = _ok(check(runs, workflow_name="wf", group_id="t-1",
                        expect_tool="kb_search"))
    assert results["工具节点被识别为 tool（kb_search）"] is False


def test_check_fails_when_llm_run_absent_only_when_required():
    """假模型不产生 generation span —— 所以 `expect_llm` 关着时不该要求它。"""
    runs = [_run("root", None, name="wf")]
    loose = _ok(check(runs, workflow_name="wf", group_id="t-1", expect_llm=False))
    strict = _ok(check(runs, workflow_name="wf", group_id="t-1", expect_llm=True))
    assert "模型调用节点被识别为 llm" not in loose
    assert strict["模型调用节点被识别为 llm"] is False


# --------------------------------------------------------------------------- #
# 三、端到端：官方处理器真的把 span 收下来了
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not HAS_LANGSMITH, reason="未安装 langsmith[openai-agents]")
def test_end_to_end_processor_collects_real_run_tree():
    """假模型真跑一次 SDK：这不花钱，但走的是完整的 span 产生与翻译路径。"""
    import asyncio

    from scripts.trace_probe import run_self_test

    try:
        payload = asyncio.run(run_self_test(group_id="g-1", workflow_name="wf"))
    finally:
        # 全局处理器是**替换**语义、全局禁用是**进程级**开关，留着都会污染同进程里
        # 后面的 agent 运行。恢复成"没配后端"时的默认态（与 app/tracing.py 一致）。
        try:
            from agents import set_trace_processors, set_tracing_disabled

            set_trace_processors([])
            set_tracing_disabled(True)
        except Exception:  # noqa: BLE001
            pass

    runs = payload["runs"]
    assert runs, "处理器一条 run 都没收到：说明没挂上，或 SDK 根本没产生 span"

    roots, orphans, nodes = build_tree(runs)
    assert len(roots) == 1, f"根 run 数量不对：{[r['name'] for r in roots]}"
    assert orphans == [], "有孤儿节点，树断了"
    assert any(n["type"] == "tool" and n["name"] == "kb_search" for n in nodes), \
        "工具节点没被翻成 run_type=tool"
    assert all(n["thread"] == "g-1" for n in nodes), \
        "有 run 没带 thread_id：group_id 没传下去"


def test_send_without_key_fails_loudly(monkeypatch):
    """没有密钥时 `--send` 必须当场失败，不能静默丢弃再报"全部通过"。"""
    from scripts.trace_probe import _real_client

    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        _real_client()
    assert excinfo.value.code == 2
