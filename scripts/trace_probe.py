"""LangSmith 转换层探针：证明官方处理器能把一次 agent 运行**完整**转成 run 树。

为什么需要它
------------
官方集成（`langsmith[openai-agents]` 的 `OpenAIAgentsTracingProcessor`）用
`set_trace_processors([...])` 挂到 SDK 上，之后每次运行都会被翻译成一棵 run 树发往
LangSmith。但"挂上了"与"翻译对了"是两件事：

* 挂上了、翻译错了 ⇒ 你在 UI 里看到一棵残缺或错父的树，却以为"链路就是这样"；
* 没挂上 ⇒ 一条 trace 都没有，而日志里没有任何报错。

所以本探针**不依赖网络、不依赖密钥、不花钱**地检查那棵树的形状：给处理器换一个
只记录不发送的 client，把它收到的 `create_run` 逐条收下来，再按 `parent_run_id`
重新拼树。拼得出一棵连通、无孤儿、带 thread_id 的树 ⇒ 转换层正常。

三种模式
--------
    python scripts/trace_probe.py              # 自检（假模型，零成本，不发网络）
    python scripts/trace_probe.py --live       # 真模型跑一次，额外验证 llm 节点
    python scripts/trace_probe.py --send       # 真发到 LangSmith（需 LANGSMITH_API_KEY）

⚠️ `--live` 会**真的调用上游模型**（一次运行，成本极低但不为零）。
⚠️ `--send` 会把这次运行发到 LangSmith；内容是否带出，取决于
   `trace_include_sensitive_data` —— 本脚本按项目口径设为 **False**（不带原文）。

为什么自检用假模型而不是真模型
------------------------------
假模型（复用 `tests/_agents_testkit.py` 的 `ScriptedModel`）能产出的 span 是
**agent / function** 两类，验证得了树结构、父子关系、工具节点、thread_id；
但它**不产生 generation span**（那是由真实 model 实现内部创建的），所以 llm 节点
验不出来。这不是缺陷，是分工：零成本的自检守骨架，`--live` 守 llm 节点。

为什么不自己写一份 ScriptedModel：与 `tests/` 各写一份必然漂移，而这里要验证的
正是"官方怎么翻译 SDK 的 span"，假模型必须与测试里那份是同一种形状。

退出码
------
0 = 全部检查项通过；1 = 有检查项失败（会逐条打印原因）；2 = 依赖缺失（没装 langsmith）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # 与其它脚本同一手法：自己把 .env 读进来
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001  .env 缺失时用真实环境变量
    pass


# --------------------------------------------------------------------------- #
# 记录型 client：顶替 langsmith.Client，只收不发
# --------------------------------------------------------------------------- #
class RecordingClient:
    """把 RunTree 的上报调用记下来；`downstream` 非空时**同时**转发给它。

    需要哪几个成员是**照着 RunTree 的实际用法**来的，不是猜的：
    `post()` 调 `create_run`，`end()`/`patch()` 调 `update_run`，
    `force_flush()` 调 `flush`。缺 `flush` 的症状很有欺骗性 ——
    运行本身完全正常，只在收尾时打一行 "Error shutting down trace processor"。

    `downstream` 存在的理由：`--send` 要"既记录、又真的发出去"。
    做法是**一个处理器 + 一个转发 client**，而不是挂两个处理器 ——
    官方只保证 `set_trace_processors` 的"替换"语义，多处理器共存没有文档承诺。
    """

    def __init__(self, project: str = "chatagent-probe", downstream=None) -> None:
        self.runs: list[dict] = []
        self.updates: list[dict] = []
        self.flushes = 0
        self.downstream = downstream
        # 这几个是 RunTree 构造时读的属性（没有就走它在 env 里的兜底）
        self.api_url = "http://recording.invalid"
        self.api_key = "recording-no-network"
        self.session_name = project
        self.tracing_queue = None

    def create_run(self, **kwargs) -> None:
        self.runs.append(kwargs)
        if self.downstream is not None:
            self.downstream.create_run(**kwargs)

    def update_run(self, run_id, **kwargs) -> None:
        self.updates.append({"run_id": run_id, **kwargs})
        if self.downstream is not None:
            self.downstream.update_run(run_id, **kwargs)

    def flush(self) -> None:
        self.flushes += 1
        if self.downstream is not None:
            self.downstream.flush()


# --------------------------------------------------------------------------- #
# 树的形状：从 create_run 的入参里取
# --------------------------------------------------------------------------- #
def _node(run: dict) -> dict:
    extra = run.get("extra") or {}
    meta = extra.get("metadata") or {}
    return {
        "id": run.get("id"),
        "parent": run.get("parent_run_id"),
        "name": run.get("name") or "",
        "type": run.get("run_type") or "",
        "thread": meta.get("thread_id"),
        "agent_type": meta.get("ls_agent_type"),
    }


def build_tree(runs: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """把乱序到达的 run 拼成树。

    返回 (roots, orphans, nodes)。**到达顺序不可依赖**：根 run 是**最后**才上报的
    （处理器要等到第一个 generation/response span 结束、拿到 inputs 之后才 post），
    所以按 parent 归并，不能按列表顺序。
    """
    nodes = [_node(r) for r in runs]
    by_id = {n["id"]: n for n in nodes}
    roots, orphans = [], []
    for n in nodes:
        if not n["parent"]:
            roots.append(n)
        elif n["parent"] not in by_id:
            orphans.append(n)
    children: dict[str, list[dict]] = {}
    for n in nodes:
        if n["parent"] in by_id:
            children.setdefault(n["parent"], []).append(n)
    for n in nodes:
        n["children"] = children.get(n["id"], [])
    return roots, orphans, nodes


def render_tree(roots: list[dict], orphans: list[dict]) -> list[str]:
    """按缩进打印树（深度优先）。孤儿单独列出来 —— 它们就是"看到一半的树"。"""
    lines: list[str] = []

    def walk(node: dict, depth: int) -> None:
        pad = "  " * depth
        thread = node["thread"] or "-"
        lines.append(f"{pad}{node['type']:<6} {node['name'] or '(无名)'}"
                     f"   [thread={thread}]")
        for child in node["children"]:
            walk(child, depth + 1)

    for root in roots:
        walk(root, 0)
    if orphans:
        lines.append("")
        lines.append("孤儿节点（父 run 不在本次记录里）：")
        for node in orphans:
            lines.append(f"  {node['type']:<6} {node['name']}   parent={node['parent']}")
    return lines


# --------------------------------------------------------------------------- #
# 检查项：每一条都是一个"转换层是否正常"的判据
# --------------------------------------------------------------------------- #
def check(runs: list[dict], *, workflow_name: str, group_id: str,
          expect_tool: str = "", expect_llm: bool = False) -> list[tuple[str, bool, str]]:
    """返回 [(判据名, 是否通过, 失败时的说明)]。"""
    roots, orphans, nodes = build_tree(runs)
    results: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, why: str = "") -> None:
        results.append((name, ok, why))

    add("有且只有一个根 run", len(roots) == 1,
        f"实际 {len(roots)} 个根：{[r['name'] for r in roots]}")

    if len(roots) == 1:
        add("根 run 用工作流名", roots[0]["name"] == workflow_name,
            f"期望 {workflow_name!r}，实际 {roots[0]['name']!r}")

    add("没有孤儿节点（父子都能对上）", not orphans,
        f"{len(orphans)} 个节点找不到父 run")

    missing = [n["name"] or n["id"] for n in nodes if n["thread"] != group_id]
    add("每条 run 都带 thread_id（多轮聚合靠它）", not missing,
        f"{len(missing)} 条缺 thread_id 或值不符：{missing[:5]}")

    if expect_tool:
        hits = [n for n in nodes if n["type"] == "tool" and n["name"] == expect_tool]
        add(f"工具节点被识别为 tool（{expect_tool}）", bool(hits),
            "没有 run_type=tool 且名字等于工具名的节点")

    if expect_llm:
        # 只有 --live 才要求：假模型不产生 generation span（见模块说明）
        add("模型调用节点被识别为 llm", any(n["type"] == "llm" for n in nodes),
            "没有 run_type=llm 的节点 —— 真实模型调用没有被翻译成 llm run")

    return results


def print_results(results: list[tuple[str, bool, str]]) -> bool:
    failed = 0
    for name, ok, why in results:
        if ok:
            print(f"  [通过] {name}")
        else:
            failed += 1
            print(f"  [失败] {name} —— {why}")
    return failed == 0


# --------------------------------------------------------------------------- #
# 一次运行的两种搭法
# --------------------------------------------------------------------------- #
def _kb_tool():
    """探针用的最小工具：证明 function span 会被翻成 ``run_type=tool``。

    两种模式共用同一个工具 —— 各写一份的话，"工具节点"这条判据在两处就分别成立，
    等于有两套真相。
    """
    from agents import function_tool

    @function_tool
    def kb_search(query: str) -> str:
        """检索知识库。"""
        return f"命中：{query}"

    return kb_search


def _real_client():
    """真实的 `langsmith.Client`；**没有密钥时直接失败**，不许静默降级。

    为什么必须显式判：缺 key 时上报任务照样进队列、发送失败只留一条日志，
    脚本看起来"全部通过"。那正是最难查的一类失败 —— 你只会以为 UI 还没刷新。
    """
    if not (os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY")):
        print("没有 LANGSMITH_API_KEY（或 LANGCHAIN_API_KEY）：--send 的上报会被静默丢弃，"
              "看起来像成功。先在 .env 里配好密钥再来。")
        raise SystemExit(2)
    from langsmith import Client as LangSmithClient

    return LangSmithClient()


def verify_sent(client, run_id: str, attempts: int = 3, wait: float = 2.0) -> bool:
    """回读刚发的那条根 run —— **这才是"真的发出去了"的证据**。

    只靠 `flush()` 不够：它保证本地队列已送出，不保证服务端接受并建好索引。
    索引有延迟，所以给几次重试；三次都读不到就说明没发成（或项目/权限不对）。
    """
    for attempt in range(attempts):
        try:
            run = client.read_run(run_id)
        except Exception as e:  # noqa: BLE001  读失败与"没发出去"要分开表述
            print(f"  第 {attempt + 1} 次回读失败：{e}")
        else:
            if run is not None:
                print(f"  回读成功：name={run.name}  id={run_id}")
                return True
        if attempt + 1 < attempts:
            time.sleep(wait)
    return False


async def run_self_test(*, group_id: str, workflow_name: str,
                        send: bool = False) -> dict:
    """假模型跑一次真实 SDK 运行：验证翻译链路，不调模型。

    send=True 时**再挂一个真处理器**：两个处理器同时收同一次运行的 span。
    这么挂是探针自己的需要（既要检查形状、又要验证真的发得出去），不代表生产建议 ——
    官方只保证 `set_trace_processors` 的"替换"语义，多处理器共存是机制上成立、
    但没有文档承诺的行为。
    """
    from tests._agents_testkit import ScriptedModel, call, say

    model = ScriptedModel([
        call("kb_search", {"query": "限流"}, "call-1"),
        say("限流按加权滑动窗口计算。"),
    ])
    return await _collect(model, group_id=group_id, workflow_name=workflow_name,
                          send=send)


async def _collect(model, *, group_id: str, workflow_name: str,
                   send: bool = False) -> dict:
    """跑一次运行并收下处理器看到的一切。model 由调用方决定（假模型 / 真模型）。

    两种模式共用这一段：**被判定的东西是"运行产生了什么 span"，不是"模型是谁"**，
    所以除了 model 之外没有任何差别。分开写两份的话，迟早出现"自检查的是 A 组判据、
    真跑查的是 B 组"这种不对称。
    """
    from langsmith.integrations.openai_agents_sdk import OpenAIAgentsTracingProcessor

    from agents import Agent, RunConfig, Runner, set_trace_processors, set_tracing_disabled

    # ⚠️ 必须显式打开这个全局开关。`set_tracing_disabled(True)` 会让 SDK **根本不产生
    # span** —— 处理器挂得再对也收不到任何东西，而症状只是"没有 trace"，没有任何报错。
    # 服务侧 `init_tracing()` 在"两个后端都没配"时正是这么关的（见 `app/tracing.py`），
    # 所以同一进程里先初始化过服务、再跑探针，会一条也收不到。
    set_tracing_disabled(False)

    client = RecordingClient(downstream=_real_client() if send else None)
    processor = OpenAIAgentsTracingProcessor(
        client=client, project_name=client.session_name, name=workflow_name)
    set_trace_processors([processor])

    agent = Agent(name="frontline_agent", instructions="你是前台，回答要短。",
                  model=model, tools=[_kb_tool()])
    result = await Runner.run(
        agent, "限流怎么算？",
        run_config=RunConfig(
            workflow_name=workflow_name,
            group_id=group_id,
            # 与 src/agents/runner.py 同一口径：不带对话原文。
            trace_include_sensitive_data=False,
        ),
    )
    try:
        # 内部会 force_flush → client.flush → downstream.flush
        processor.shutdown()
    except Exception:  # noqa: BLE001  关不掉不影响结论
        pass
    return {"runs": client.runs, "updates": client.updates,
            "flushes": client.flushes, "answer": result.final_output,
            "client": client}


async def run_live(*, group_id: str, workflow_name: str,
                   send: bool = False) -> dict | None:
    """真模型跑一次：额外验证 llm 节点。需要 LLM_* 已配置。

    为什么这一步不能用假模型替代：generation span 是**真实 model 实现内部**创建的
    （`models/openai_chatcompletions.py` 里 `with generation_span(...)`），
    假模型自己返回 `ModelResponse`、根本不开这个 span。所以"llm 节点能不能被翻译
    出来"只有真跑能验 —— 自检那几条全绿也不代表这一项成立。
    """
    from src.agents.model import build_model

    api_key = os.getenv("LLM_API_KEY", "")
    if not api_key:
        print("LLM_API_KEY 为空：--live 需要真实模型。只跑自检的话去掉 --live。")
        return None

    model = build_model(
        model_name=os.getenv("LLM_MODEL", ""),
        api_key=api_key,
        base_url=os.getenv("LLM_API_BASE", ""),
        timeout=float(os.getenv("AGENT_TIMEOUT_SECONDS", "30") or 30),
    )
    return await _collect(model, group_id=group_id, workflow_name=workflow_name,
                          send=send)


# --------------------------------------------------------------------------- #
async def main() -> int:
    parser = argparse.ArgumentParser(description="LangSmith 转换层探针")
    parser.add_argument("--live", action="store_true",
                        help="用真实模型跑一次（会消耗模型额度），额外验证 llm 节点")
    parser.add_argument("--send", action="store_true",
                        help="真的发到 LangSmith（需 LANGSMITH_API_KEY）")
    parser.add_argument("--project", default="chatagent-probe", help="LangSmith 项目名")
    args = parser.parse_args()

    try:
        import langsmith  # noqa: F401
    except ImportError:
        print("没装 langsmith。装法：pip install 'langsmith[openai-agents]>=0.3.15'")
        return 2

    group_id = "probe-thread-0001"
    workflow_name = "chatagent_probe"

    print(f"模式：{'--live（真模型）' if args.live else '自检（假模型，零成本）'}"
          f"{' + --send（真的发往 LangSmith）' if args.send else '（不发网络）'}")
    print()

    runner = run_live if args.live else run_self_test
    payload = await runner(group_id=group_id, workflow_name=workflow_name,
                           send=args.send)
    if payload is None:
        return 2
    runs = payload["runs"]
    print(f"处理器收到 {len(runs)} 次 create_run / {len(payload['updates'])} 次 update_run / "
          f"{payload['flushes']} 次 flush")
    print()
    roots, orphans, _ = build_tree(runs)
    for line in render_tree(roots, orphans):
        print(line)
    print()

    print("检查项：")
    ok = print_results(check(
        runs, workflow_name=workflow_name, group_id=group_id,
        # 真模型不保证一定调工具，这条判据只在自检（脚本写死的那两步）时成立。
        expect_tool="" if args.live else "kb_search",
        expect_llm=args.live,
    ))
    print()
    if args.send:
        print("发送验证（回读刚发的根 run）：")
        ok = (ok and verify_sent(payload["client"].downstream, roots[0]["id"])
              if roots else False)
        print()
    print(f"答案产出：{payload['answer']!r}")
    print("结论：" + ("转换层正常" if ok else "转换层有问题，见上面的失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
