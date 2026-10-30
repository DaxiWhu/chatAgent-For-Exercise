"""用**真实模型**跑「工具选择」评测集 —— 回答"模型到底会不会用这些工具"。

为什么需要它（与 ``agent_capability_probe.py`` 的分工）
----------------------------------------------------
那个脚本看的是**链路能力**（会不会转交、评审几轮过、护栏误不误伤）；
这个脚本只看一件事：**给定一个请求，模型选对了工具没有**。

两者不能合并，因为失败原因完全不同：链路能力差是 prompt 与编排的问题，
工具选择差是**工具描述与参数 schema** 的问题 —— 而后者只有"看它选了哪个"
才能定位。举例：`calculate` 从没被选中过时，答案里的数字照样完全合理
（模型心算），从产出上根本看不出来。

三条判据（与 ``agent_tool_eval_set.json`` 的字段一一对应）
--------------------------------------------------------
* ``expect_tools``   —— 必须**出现**（子集判定：多查一次不算错）；
* ``forbid_tools``   —— 一个都**不许**出现（跑错方向比跑少一步更贵）；
* ``expect_no_tools``—— 闲聊：一步动作都不该有；
* ``expect_text_any``—— **软判定**，只用于"该承认做不到"那一类（没有对应工具，
  硬判工具名判不出来）。汇总里单列，不与硬判定混算通过率。

数据从哪来
----------
工具序列直接读 ``TaskContext.steps``（本项目自己的动作清单），而不是从模型响应里
解析 —— 那正是"步骤清单"存在的理由，两处分别解析必然漂，而漂的表现是
"评测说调了、日志里没调"。

⚠️ 多轮用的是**内存会话替身**（官方没有内存版 ``Session``，见 ``session.py``）。
所以本脚本验证的是"跨轮能不能拿到历史"，不验证 PG 后端本身。
⚠️ 会**真的调用上游模型**（花钱）。默认 12 条用例、单条最多 3 轮。

用法
----
    python scripts/agent_tool_eval.py                    # 用 .env 里的 AGNES 模型
    python scripts/agent_tool_eval.py --model deepseek   # 换 DeepSeek（同一套 OpenAI 兼容协议）
    python scripts/agent_tool_eval.py --only time-today
    python scripts/agent_tool_eval.py --json tmp_probe/tool_eval.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # 与 app/main.py 同一手法：脚本自己把 .env 读进来
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001  .env 缺失时照常跑（用真实环境变量）
    pass

# ⚠️ 必须在 import src.agents 之前设：``steps.steps_enabled()`` 在模块导入期就被
# 读进常量（各工具的 ``_isolated`` 装饰器在导入时构造）。设晚了就是"清单永远空"，
# 而评测会表现为"模型一个工具都没调"—— 一个完全错误的结论。
os.environ.setdefault("AGENT_TOOL_STEPS", "1")

DEFAULT_SET = ROOT / "scripts" / "agent_tool_eval_set.json"


class MemorySession:
    """内存会话（官方 ``SessionABC`` 的四个方法）。

    只为本脚本服务：官方没有内存版后端，而评测需要"多轮之间真的有历史"。
    语义与官方一致 —— 追加、按序取回、弹出最后一条、清空。
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.items: list[Any] = []
        self.session_settings = None

    async def get_items(self, limit: int | None = None) -> list[Any]:
        if limit is None or limit <= 0:
            return [dict(i) if isinstance(i, dict) else i for i in self.items]
        return [dict(i) if isinstance(i, dict) else i for i in self.items[-limit:]]

    async def add_items(self, items: list[Any]) -> None:
        self.items.extend(items)

    async def pop_item(self) -> Any:
        return self.items.pop() if self.items else None

    async def clear_session(self) -> None:
        self.items.clear()


_SESSIONS: dict[str, MemorySession] = {}


def memory_session_for(session_id: str) -> Any:
    """替掉 ``runner.session_for``：同一个会话编号返回同一个内存对象。"""
    sid = (session_id or "").strip()
    if not sid:
        return None
    session = _SESSIONS.get(sid)
    if session is None:
        session = MemorySession(sid)
        _SESSIONS[sid] = session
    return session


def _build_model(which: str) -> Any:
    """按名字装配模型（两条都是 OpenAI 兼容协议，走同一个官方实现）。"""
    from src.agents.model import build_model

    if which == "deepseek":
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            raise SystemExit("缺少 DEEPSEEK_API_KEY（.env 里没有配）")
        return build_model(
            model_name=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
            api_key=key,
            base_url=os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com"),
            timeout=60.0,
        )

    key = os.environ.get("AGNES_API_KEY", "")
    if not key:
        raise SystemExit("缺少 AGNES_API_KEY（.env 里没有配）")
    return build_model(
        model_name=os.environ.get("AGNES_MODEL", "agnes-2.5-flash"),
        api_key=key,
        base_url=os.environ.get("AGNES_API_BASE", ""),
        timeout=60.0,
    )


def _turns_of(case: dict) -> list[dict]:
    """把单轮 / 多轮两种写法归一成轮次列表。"""
    if "turns" in case:
        return list(case["turns"])
    return [case]


def _judge(turn: dict, called: list[str], answer: str) -> tuple[bool, str, bool]:
    """判一轮。返回 ``(通过, 失败原因, 是不是软判定)``。"""
    seen = set(called)

    if turn.get("expect_no_tools"):
        if called:
            return False, f"不该调工具却调了：{called}", False
        return True, "", False

    missing = [t for t in turn.get("expect_tools", []) if t not in seen]
    if missing:
        return False, f"漏了 {missing}（实际：{called or '无'}）", False

    hit_forbidden = [t for t in turn.get("forbid_tools", []) if t in seen]
    if hit_forbidden:
        return False, f"调了不该调的 {hit_forbidden}（实际：{called}）", False

    wanted = turn.get("expect_text_any") or []
    if wanted and not any(word in (answer or "") for word in wanted):
        return False, f"答案里没有承认做不到（期望含 {wanted} 之一）", True

    return True, "", bool(wanted)


async def _run_turn(
    case_id: str,
    index: int,
    turn: dict,
    *,
    model: Any,
    asset_dir: Path,
    docs_dir: str,
    max_turns: int,
) -> dict:
    """跑一轮，返回这一轮的观测结果。每轮新建上下文（与真实服务一致：一轮一请求）。"""
    from src.agents.context import TaskContext
    from src.agents.runner import ENTRY_AGENT, run_agent

    ctx = TaskContext(docs_dir=docs_dir, asset_dir=asset_dir, retriever_factory=None)
    started = time.perf_counter()
    try:
        outcome = await asyncio.wait_for(
            run_agent(
                ENTRY_AGENT,
                prompt=turn["prompt"],
                context=ctx,
                model=model,
                session_id=f"eval::{case_id}",
                max_turns=max_turns,
                trace_metadata={"eval": case_id, "turn": str(index)},
            ),
            timeout=240,
        )
        answer = outcome.answer
        exit_reason = outcome.exit_reason
        error = outcome.error
    except TimeoutError:
        answer, exit_reason, error = "", "eval_timeout", "单轮超过 240s"
    except Exception as e:  # noqa: BLE001  评测脚本不该因为一条用例挂掉整个批次
        answer, exit_reason, error = "", "eval_error", f"{type(e).__name__}: {e}"

    called = [s.tool for s in ctx.steps.steps]
    # ★ "选中"与"能用"是两件事，必须分开报（2026-10-30 实测教训）：
    # 第一次跑这份评测时硬判定 12/12 全过，而日志里三个 ``as_tool`` 专家**每次都失败**
    # （上游返回 ``response_format type is unavailable``）。只看"选没选"的话，
    # 整个评测是假绿 —— 它证明的只是"模型点了那个按钮"，不证明按钮连着东西。
    failed = [s.tool for s in ctx.steps.steps if s.status == "failed"]
    succeeded = [s.tool for s in ctx.steps.steps if s.status == "ok"]
    ok, reason, soft = _judge(turn, called, answer)
    return {
        "prompt": turn["prompt"],
        "tools": called,
        "status": [s.status for s in ctx.steps.steps],
        "failed_tools": failed,
        # 空转 = 有动作、但一个都没成。它不是"选错工具"，也不是"模型不会用"，
        # 而是**工具这一侧坏了** —— 处置完全不同（改工具 / 换模型 / 查上游）。
        "dead": bool(called) and not succeeded,
        "passed": ok,
        "reason": reason,
        "soft": soft,
        "exit_reason": exit_reason,
        "error": error,
        "answer": (answer or "")[:400],
        "seconds": round(time.perf_counter() - started, 2),
        "model_calls": ctx.stats.model_calls,
        "input_tokens": ctx.stats.input_tokens,
        "output_tokens": ctx.stats.output_tokens,
    }


async def main_async(args: argparse.Namespace) -> int:
    data = json.loads(Path(args.set).read_text(encoding="utf-8"))
    cases = data["cases"]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]
        if not cases:
            raise SystemExit(f"没有匹配的用例：{args.only}")

    # 会话替身必须在 import runner 之后打补丁（runner 里是 from ... import）。
    import src.agents.runner as runner

    runner.session_for = memory_session_for

    model = _build_model(args.model)
    # 每次评测一个独立子目录：``list_assets`` 那条用例判的是"当前有哪些文件"，
    # 与上一批跑剩的文件混在一起时，它会列出几十个 —— 用例仍然通过，但输出没法看，
    # 而"没法看的输出"最后就没人看。
    asset_dir = Path(args.assets) / time.strftime("%Y%m%d-%H%M%S")
    asset_dir.mkdir(parents=True, exist_ok=True)

    print(f"模型：{args.model}｜用例：{len(cases)} 条｜资产目录：{asset_dir}")
    print("-" * 78)

    reports: list[dict] = []
    for case in cases:
        turns = _turns_of(case)
        rows = []
        for index, turn in enumerate(turns, start=1):
            row = await _run_turn(
                case["id"],
                index,
                turn,
                model=model,
                asset_dir=asset_dir,
                docs_dir=args.docs_dir,
                max_turns=args.max_turns,
            )
            rows.append(row)
            mark = "PASS" if row["passed"] else "FAIL"
            tag = " [soft]" if row["soft"] else ""
            dead = " ⚠️空转" if row["dead"] else ""
            print(
                f"[{mark}] {case['id']} 第{index}轮{tag}  "
                f"工具={row['tools'] or '无'}  {row['seconds']}s  "
                f"{row['model_calls']}次模型调用{dead}"
            )
            if row["failed_tools"]:
                print(f"       ↳ 调用失败：{row['failed_tools']}")
            if not row["passed"]:
                print(f"       ↳ {row['reason']}")
                if row["exit_reason"] not in ("passed", "unreviewed", "review_failed"):
                    print(f"       ↳ exit_reason={row['exit_reason']} {row['error'][:160]}")
        reports.append({"id": case["id"], "note": case.get("note", ""), "turns": rows})

    all_rows = [r for c in reports for r in c["turns"]]
    hard = [r for r in all_rows if not r["soft"]]
    soft = [r for r in all_rows if r["soft"]]
    passed = [r for r in hard if r["passed"]]
    tools = [t for r in all_rows for t in r["tools"]]
    seconds = sum(r["seconds"] for r in all_rows)
    tokens_in = sum(r["input_tokens"] for r in all_rows)
    tokens_out = sum(r["output_tokens"] for r in all_rows)
    calls_ok = sum(1 for r in all_rows for s in r["status"] if s == "ok")
    calls_total = sum(len(r["status"]) for r in all_rows)
    dead = [r for r in all_rows if r["dead"]]

    print("-" * 78)
    print(
        f"工具选择：{len(passed)}/{len(hard)} 通过"
        f"（{len(passed) / len(hard) * 100:.0f}%）"
        f"｜软判定：{sum(1 for r in soft if r['passed'])}/{len(soft)}"
    )
    if calls_total:
        print(
            f"工具执行：{calls_ok}/{calls_total} 次成功"
            f"（{calls_ok / calls_total * 100:.0f}%）"
        )
    if dead:
        # 这一行是**这份评测最重要的输出**：选择全对、执行全失败，只看通过率是满分。
        print(f"⚠️ 空转（有动作、无一成功）：{len(dead)} 轮 -> " + "、".join(
            f"{r['prompt'][:12]}…" for r in dead
        ))
    print(f"工具调用合计 {len(tools)} 次｜耗时 {seconds:.1f}s｜token 入 {tokens_in} / 出 {tokens_out}")
    if tools:
        from collections import Counter

        fails: dict[str, int] = {}
        for r in all_rows:
            for name, status in zip(r["tools"], r["status"], strict=True):
                if status == "failed":
                    fails[name] = fails.get(name, 0) + 1
        print(
            "工具分布："
            + "、".join(
                f"{k}×{v}" + (f"（失败{fails[k]}）" if fails.get(k) else "")
                for k, v in Counter(tools).most_common()
            )
        )

    if args.json:
        out = {
            "model": args.model,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "summary": {
                "hard_total": len(hard),
                "hard_passed": len(passed),
                "soft_total": len(soft),
                "soft_passed": sum(1 for r in soft if r["passed"]),
                "calls_total": calls_total,
                "calls_ok": calls_ok,
                "dead_turns": len(dead),
                "tool_calls": len(tools),
                "seconds": round(seconds, 1),
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
            },
            "cases": reports,
        }
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"明细已写入 {args.json}")

    # 退出码把"空转"也算作失败：选择全对、执行全崩时，只看通过率是满分 ——
    # 而那正是最需要有人看一眼的状态。
    return 0 if (len(passed) == len(hard) and not dead) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="agent 工具选择评测")
    parser.add_argument("--model", default="agnes", choices=["agnes", "deepseek"])
    parser.add_argument("--set", default=str(DEFAULT_SET))
    parser.add_argument("--only", default="", help="只跑这些用例 id（逗号分隔）")
    parser.add_argument("--json", default="", help="把明细写到这个路径")
    parser.add_argument("--assets", default="var/eval_assets")
    parser.add_argument("--docs-dir", default="", help="语料目录（给了才会做引用校验）")
    parser.add_argument("--max-turns", type=int, default=8)
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
