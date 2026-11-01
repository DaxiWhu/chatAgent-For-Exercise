#!/usr/bin/env python
"""长任务基线：用**项目已配置的模型**真跑一组长任务，量"完成率 / 偏离率"。

为什么需要它
------------
现有的三层评测（检索 / 材料 / 生成）考的都是**一次作答**的质量：给一段材料，
问一句，看答案准不准。它们没有一条能回答长任务的那个问题 ——
**一个需要十来步、几次检索、中途还要回头补漏的任务，最后到底做成了没有。**

这两件事的失败方式完全不同：单轮答错是"内容错"，长任务失败是"跑着跑着偏了、
或者陷在某个子步骤里反复试"。后者在单轮用例上永远测不出来（它需要多步才会发生）。
所以在没有长任务口径的情况下，改动 `instructions` 或反思阈值之后，只能靠**感觉**
判断"最近长任务是不是变差了"。

它量什么（每个指标的口径都写死在这里，不要在别处再算一遍）
----------------------------------------------------------
    完成率      ``outcome == delivered`` 的运行 / 有效运行        （越高越好）
    部分结果率  ``outcome == partial``                          （被打断但交了东西）
    兜底率      ``manual`` + ``failed``                          （要人收拾）
    偏离率      反思报过 ``off_track`` 的运行 / 有效运行            （越低越好）
    打转率      反思报过 ``loop`` 的运行
    卡住率      反思报过 ``stuck`` 的运行
    平均步数    ``len(ctx.steps.steps)``（工具动作数，**不是**模型调用数）
    未完成步数  同一次运行里 ``status != "ok"`` 的动作数
    平均 token  ``stats.input_tokens + stats.output_tokens``（上游不回 usage 时为 0）
    平均耗时    墙钟秒

⚠️ **这一项要真花钱**（调上游模型）。用例刻意压到 5 条、每条都限篇幅。它不是压测，
是"能力画像"：宁可少跑几条，也不要为了数字好看把每条都跑到烧钱。

⚠️ **防护必须打开**（脚本强制 ``AGENT_MULTISTEP_GUARD=1``）：偏离率/打转率/卡住率
全部来自反思闸门，闸门关着时那三行永远是 0 —— 而 0 会被读成"没问题"，实际是"没测"。
这是一个典型的"配了但没生效"陷阱，所以它在脚本里是**硬设定**，不是读环境。

⚠️ **基线必须自己先跑一次生成**（``--out``），不存在"出厂基线"。任何声称"我们预设了
一个合理基线"的说法都不成立：基线取决于你用的模型、提示词版本和语料。所以这里只提供
**对比**与退化判据，不提供数字。

用法
----
    python scripts/eval_longrun.py                       # 跑一遍并打印
    python scripts/eval_longrun.py --out scripts/longrun_baseline.json
    python scripts/eval_longrun.py --baseline scripts/longrun_baseline.json
    python scripts/eval_longrun.py --only 2 --no-kb      # 只跑第 2 条、不接检索

有退化时退出码为 1（可用于发布前的手动门禁，**不要**放进 CI —— 它依赖真实模型）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # 与 app/main.py 同一手法：脚本自己把 .env 读进来
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001  .env 缺失时照常跑（用真实环境变量）
    pass


# --------------------------------------------------------------------------- #
# 用例：每条都要求"多步"，且给得出可判的产出约束（控制成本 + 让失败可归因）
# --------------------------------------------------------------------------- #
CASES = [
    {
        "name": "分段调研后成文（先查再写，不许边查边写）",
        "prompt": (
            "请先用检索分别查清三件事：限流怎么算、超时怎么设、重试怎么退避；"
            "然后基于查到的东西写一段 400 字以内的说明，按「是什么 — 为什么 — 怎么配」"
            "三段组织，每段末尾用 [n] 标出依据。不要在没有检索的情况下直接作答。"
        ),
        "min_steps": 3,
        "watch": "是否真的先检索三次再成文（步数 < 3 说明它跳过了检索直接编）",
    },
    {
        "name": "带硬约束的长产出（五条约束同时成立）",
        "prompt": (
            "写一份 500 字以内的部署检查清单，必须同时满足：① 分五个小节；"
            "② 每节不超过三条；③ 至少三条来自检索到的材料并用 [n] 标注；"
            "④ 明确写出「哪些项不做也不会出事」；⑤ 结尾用一句话给出最该先做的那一项。"
        ),
        "min_steps": 1,
        "watch": "五条约束是否同时满足（漏一条就是没做到，不是'差不多'）",
    },
    {
        "name": "自查后再补漏（典型的多轮自我修正）",
        "prompt": (
            "针对「上线前该检查什么」先写一版 300 字以内的要点；"
            "写完之后**自己检查一遍**有没有漏掉「回滚方案」和「容量评估」这两块，"
            "缺了就补进去（补的时候说明补了什么）。最后只输出补完的版本。"
        ),
        "min_steps": 1,
        "watch": "它有没有真的回头检查（产出里应能看出补充动作），还是一遍写完就交",
    },
    {
        "name": "跨轮：先记约束，下一句按约束改",
        "prompt": (
            "记住三条约束，后面会用到：不要用感叹号、结论放最前面、每段不超过四句。"
            "现在先只回复「记住了」。"
        ),
        "min_steps": 0,
        "follow_up": "按刚才那三条约束，写一段 200 字以内关于「先上线还是先压测」的看法。",
        "watch": "第二句是否真的遵守了三条约束（跨轮记忆 + 约束遵循一起考）",
    },
    {
        "name": "转交类长任务（文学创作）",
        "prompt": (
            "写一篇 300 字以内的短篇小说，主题是「最后一班地铁」，"
            "要求有明确的时间推进和一次转折。"
        ),
        "min_steps": 0,
        "watch": "是否交给文学 agent（exit_reason=transfer）、交接之后是否正常收尾",
    },
]


def _ctx_factory(use_kb: bool):
    from src.agents.context import TaskContext

    retriever_factory = None
    if use_kb:
        from src.rag.pool import get_retriever

        retriever_factory = lambda: get_retriever(None)  # noqa: E731
    return TaskContext(
        tenant="",
        docs_dir=os.getenv("DOCS_DIR", "data"),
        retriever_factory=retriever_factory,
        asset_dir=ROOT / os.getenv("AGENT_ASSET_DIR", "var/agent_assets"),
    )


async def _run_case(case: dict, *, use_kb: bool, session_id: str = "") -> dict:
    """跑一条用例（可能两轮），返回一行原始读数。

    ⚠️ 异常**不吞**：限流、协议错必须作为 ``error`` 行单独报出来，而不是混进
    "完成率下降"里 —— 那会让人去改提示词，而真正的问题是额度。
    """
    from src.agents.runner import ENTRY_AGENT, run_agent

    ctx = _ctx_factory(use_kb)
    started = time.perf_counter()
    outcome = await run_agent(
        ENTRY_AGENT, prompt=case["prompt"], context=ctx, session_id=session_id
    )
    follow = case.get("follow_up")
    if follow:
        outcome = await run_agent(
            ENTRY_AGENT, prompt=follow, context=ctx, session_id=session_id
        )
    wall = time.perf_counter() - started

    kinds = dict(ctx.reflection.kinds)
    steps = list(ctx.steps.steps)
    verdict = outcome.fallback
    return {
        "name": case["name"],
        "exit_reason": outcome.exit_reason,
        "outcome": getattr(verdict, "outcome", "") if verdict is not None else "",
        "reason": getattr(verdict, "reason", "") if verdict is not None else "",
        "answer_chars": len((outcome.answer or "").strip()),
        "steps": len(steps),
        "unfinished": sum(1 for s in steps if getattr(s, "status", "") != "ok"),
        "oscillation_cycles": int(getattr(ctx.steps, "oscillation_cycles", 0) or 0),
        "problems": kinds,
        "model_calls": ctx.stats.model_calls,
        "tool_calls": ctx.stats.tool_calls,
        "tokens": ctx.stats.input_tokens + ctx.stats.output_tokens,
        "seconds": round(wall, 1),
        "min_steps": case.get("min_steps", 0),
    }


def _rate(rows: list[dict], key: str, value: str) -> float:
    return sum(1 for r in rows if r.get(key) == value) / len(rows) if rows else 0.0


def _problem_rate(rows: list[dict], problem: str) -> float:
    return sum(1 for r in rows if r["problems"].get(problem)) / len(rows) if rows else 0.0


def _summary(rows: list[dict]) -> dict:
    """把逐条读数汇成一页（口径见模块 docstring，**只在这里算一次**）。"""
    if not rows:
        return {}
    return {
        "cases": len(rows),
        "delivered_rate": round(_rate(rows, "outcome", "delivered"), 4),
        "partial_rate": round(_rate(rows, "outcome", "partial"), 4),
        "fallback_rate": round(
            _rate(rows, "outcome", "manual") + _rate(rows, "outcome", "failed"), 4
        ),
        "drifting_rate": round(_problem_rate(rows, "off_track"), 4),
        "looping_rate": round(_problem_rate(rows, "loop"), 4),
        "stuck_rate": round(_problem_rate(rows, "stuck"), 4),
        "avg_steps": round(sum(r["steps"] for r in rows) / len(rows), 2),
        "avg_unfinished": round(sum(r["unfinished"] for r in rows) / len(rows), 2),
        "avg_tokens": round(sum(r["tokens"] for r in rows) / len(rows), 1),
        "avg_seconds": round(sum(r["seconds"] for r in rows) / len(rows), 1),
        "short_cases": [r["name"] for r in rows if r["steps"] < r["min_steps"]],
    }


def _print_rows(rows: list[dict]) -> None:
    head = (
        f"{'#':<3}{'结局':<10}{'原因':<16}{'步':<4}{'未完成':<5}"
        f"{'往复':<4}{'问题':<14}{'模型':<5}{'工具':<5}{'token':<8}{'字数':<6}{'秒':<6}"
    )
    print(head)
    print("-" * len(head))
    for index, row in enumerate(rows, start=1):
        problems = ",".join(f"{k}×{v}" for k, v in row["problems"].items()) or "-"
        print(
            f"{index:<3}{row['outcome'] or '-':<10}{row['reason'] or '-':<16}"
            f"{row['steps']:<4}{row['unfinished']:<5}{row['oscillation_cycles']:<4}"
            f"{problems:<14}{row['model_calls']:<5}{row['tool_calls']:<5}"
            f"{row['tokens']:<8}{row['answer_chars']:<6}{row['seconds']:<6}"
        )


def _print_summary(summary: dict) -> None:
    print(f"\n{'=' * 78}")
    print("长任务基线")
    print(f"{'=' * 78}")
    if not summary:
        print("没有有效运行（全部异常/限流）")
        return
    print(f"用例数        : {summary['cases']}")
    print(f"完成率        : {summary['delivered_rate']:.0%}   （delivered）")
    print(f"部分结果率    : {summary['partial_rate']:.0%}   （被打断但交了东西）")
    print(f"兜底率        : {summary['fallback_rate']:.0%}   （manual + failed）")
    print(f"偏离率        : {summary['drifting_rate']:.0%}   （反思报过 off_track）")
    print(f"打转率        : {summary['looping_rate']:.0%}   / 卡住率 {summary['stuck_rate']:.0%}")
    print(f"平均步数      : {summary['avg_steps']}  （平均未完成 {summary['avg_unfinished']} 步）")
    print(f"平均 token    : {summary['avg_tokens']}   平均耗时 {summary['avg_seconds']}s")
    if summary["short_cases"]:
        # 这一行很重要：它说明"用例本身没有跑成长任务"，此时完成率再高也不代表能力。
        print(f"⚠️ 步数低于预期 : {summary['short_cases']}（这些用例没跑到多步，完成率不可比）")


def _regressions(base: dict, cur: dict) -> list[str]:
    """与基线比，哪些指标退化了（阈值都是"能解释的"，不是拍的）。

    * 完成率掉 5 个百分点 —— 5 条用例里掉 1 条就是 20 个点，所以 5 点是"还没到一条"的
      噪声余量，低于它不该触发；
    * 偏离率涨 5 个百分点 —— 同一条道理；
    * 平均 token 涨 30% —— 成本变化 30% 已经不是抖动；这一项单独看，因为它与"完成率"
      可以是反向的（多花 30% 换来完成率不变 = 白花）。
    """
    out: list[str] = []
    if cur.get("delivered_rate", 0) < base.get("delivered_rate", 0) - 0.05:
        out.append(
            f"完成率 {base.get('delivered_rate', 0):.0%} → {cur.get('delivered_rate', 0):.0%}"
        )
    if cur.get("drifting_rate", 0) > base.get("drifting_rate", 0) + 0.05:
        out.append(
            f"偏离率 {base.get('drifting_rate', 0):.0%} → {cur.get('drifting_rate', 0):.0%}"
        )
    tokens = base.get("avg_tokens") or 0
    if tokens and cur.get("avg_tokens", 0) > tokens * 1.3:
        out.append(f"平均 token {tokens} → {cur.get('avg_tokens', 0)}（+30% 以上）")
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description="长任务基线（真实模型）")
    ap.add_argument("--only", type=int, default=0, help="只跑第 N 条（从 1 开始；0=全部）")
    ap.add_argument("--no-kb", action="store_true", help="不接真实检索器")
    ap.add_argument("--gap", type=float, default=3.0, help="用例之间间隔秒数（默认 3）")
    ap.add_argument("--out", default="", help="把汇总写到这个 JSON（首次生成基线用）")
    ap.add_argument("--baseline", default="", help="与这个 JSON 比较，退化时退出码 1")
    args = ap.parse_args()

    from src.agents import runtime

    # ⚠️ 硬设定：偏离率/打转率/卡住率全部来自反思闸门，关着时它们恒为 0。
    # 用 setdefault 会被 .env 里的 0 覆盖，所以这里是**直接赋值**并打印出来。
    os.environ["AGENT_MULTISTEP_GUARD"] = "1"

    model_name = os.getenv("LLM_MODEL", "")
    api_key = os.getenv("LLM_API_KEY", "")
    base_url = os.getenv("LLM_API_BASE", "")
    if not api_key:
        print("❌ LLM_API_KEY 为空：长任务基线需要真实模型。请在 .env 里配置后重试。")
        return 2

    ok = runtime.configure(
        model_name=model_name,
        api_key=api_key,
        base_url=base_url,
        timeout=float(os.getenv("AGENT_TIMEOUT_SECONDS", "30") or 30),
    )
    print(f"模型装配: {'OK' if ok else 'FAILED'} | model={model_name} base={base_url} "
          f"reason={runtime.ready_reason()}")
    if not ok:
        return 2
    print("多步防护: 已强制打开（否则偏离率/打转率恒为 0，不是'没问题'）")
    print(f"token 预算: {os.getenv('AGENT_RUN_TOKEN_BUDGET', '0') or '0'}"
          f"（0=不限；到线会让完成率下降，这是配置而不是能力）")
    if args.no_kb:
        print("检索      : 已按 --no-kb 关闭（research 会返回'检索不可用'）")

    selected = (
        [(args.only, CASES[args.only - 1])] if args.only else list(enumerate(CASES, start=1))
    )

    rows: list[dict] = []
    errors: list[dict] = []
    for position, (index, case) in enumerate(selected):
        print(f"\n{'=' * 78}")
        print(f"[{index}] {case['name']}")
        print(f"{'=' * 78}")
        print(f"关注点    : {case['watch']}")
        try:
            row = await _run_case(
                case, use_kb=not args.no_kb, session_id=f"longrun-{index}-{int(time.time())}"
            )
        except Exception as e:  # noqa: BLE001  探测脚本要把失败本身报出来
            kind = type(e).__name__
            limited = "RateLimit" in kind or "429" in str(e)
            print(f"{'⏳' if limited else '❌'} {'上游限流' if limited else '执行异常'}: {kind}: {e}")
            errors.append({"name": case["name"], "error": f"{kind}: {e}", "rate_limited": limited})
            continue
        rows.append(row)
        print(f"结局      : outcome={row['outcome'] or '-'} reason={row['reason'] or '-'} "
              f"exit_reason={row['exit_reason']}")
        print(f"过程      : 步数={row['steps']} 未完成={row['unfinished']} "
              f"往复={row['oscillation_cycles']} 问题={row['problems'] or '无'}")
        print(f"成本      : 模型={row['model_calls']} 工具={row['tool_calls']} "
              f"token={row['tokens']} 耗时={row['seconds']}s")
        if position < len(selected) - 1:
            await asyncio.sleep(max(0.0, args.gap))

    print(f"\n{'=' * 78}")
    _print_rows(rows)
    summary = _summary(rows)
    _print_summary(summary)

    if errors:
        limited = sum(1 for e in errors if e["rate_limited"])
        print(f"\n未计入统计 : {len(errors)} 条（其中上游限流 {limited} 条）")
        for e in errors:
            print(f"  - {e['name']}: {e['error'][:90]}")

    code = 0
    if args.baseline and summary:
        base_path = Path(args.baseline)
        if not base_path.exists():
            print(f"\n⚠️ 基线文件不存在：{base_path}（先跑一次 --out 生成，不要手工编数字）")
            code = 2
        else:
            base = json.loads(base_path.read_text(encoding="utf-8"))
            regressions = _regressions(base, summary)
            if regressions:
                print("\n❌ 与基线相比退化：")
                for line in regressions:
                    print(f"  - {line}")
                code = 1
            else:
                print("\n✅ 与基线相比没有退化")

    if args.out and summary:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = ROOT / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n汇总已写入: {out_path}")

    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
