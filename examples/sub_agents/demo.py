"""离线演示：四条典型请求分别走四条不同的路径。

    python -m examples.sub_agents.demo

刻意用官方 ``ScriptedModel`` 而不是真模型 —— 演示要展示的是**拓扑**（谁跟谁说话、
数据长什么样），不是模型文笔；用脚本才能让每次运行的轨迹完全一致。
真模型只需把 ``model=ScriptedModel(...)`` 换成 ``model=None``（走 ``OPENAI_DEFAULT_MODEL``）。
"""

from __future__ import annotations

import asyncio
import json

from agents.testing import ScriptedModel

from .context import TaskContext
from .runner import run_task
from .scripting import call, say
from .tools import image_asset_path

__all__ = ["main"]


def _feedback(text: str, score: str):
    return say(json.dumps({"feedback": text, "score": score}, ensure_ascii=False))


def _payload(obj):
    return say(json.dumps(obj, ensure_ascii=False))


async def scene_literary() -> None:
    print("=" * 72)
    print("① 创作类 · 文学  →  handoff（专家接管对话，产出直接给用户看）")
    print("=" * 72)
    ctx = TaskContext()
    model = ScriptedModel(
        [
            call("transfer_to_literary_writer", {}, call_id="h1"),
            say("《第七个冬天》\n灯塔守望者把最后一桶煤油倒进灯里……"),
            _feedback("第一轮先不给 pass：请在第二段补一处海面细节。", "needs_improvement"),
            say("《第七个冬天》\n灯塔守望者把最后一桶煤油倒进灯里……海面结着薄冰，风从东北来。"),
            _feedback("已补海面细节，可以交付。", "pass"),
        ]
    )
    outcome = await run_task("写一篇关于灯塔的短篇小说", model=model, context=ctx)
    print(f"  最终接话的 agent : {outcome.final_agent}")
    print(f"  轮数 / 出口       : {outcome.attempts} / {outcome.exit_reason}")
    print(f"  上下文 channel    : {ctx.channel}   ← on_handoff 写的，不是模型写的")
    print(f"  最终答复          : {outcome.answer[:38]}…")


async def scene_multi_deliverable() -> None:
    print()
    print("=" * 72)
    print("② 创作类 · PPT + 插图  →  两个 as_tool（专家不接管对话，前台负责汇总）")
    print("=" * 72)
    ctx = TaskContext()
    deck = {
        "title": "限流方案",
        "subtitle": "实现评审稿",
        "slides": [
            {
                "title": "判定式",
                "bullets": ["上一窗口×剩余比例", "加当前窗口计数"],
                "speaker_notes": "强调：不是固定窗口",
            }
        ],
    }
    image_prompt = "一座灯塔在暴风雪中"
    model = ScriptedModel(
        [
            call("build_slide_deck", {"input": "把限流方案做成 1 页"}, call_id="s1"),
            _payload(deck),
            call("make_illustration", {"input": "暴风雪中的灯塔"}, call_id="v1"),
            call("generate_image", {"prompt": image_prompt, "style": "flat"}, call_id="g1"),
            _payload({"prompt": image_prompt, "style": "flat", "asset_path": image_asset_path(image_prompt)}),
            say("已交付：1 页大纲 + 1 张插图。"),
            _feedback("可以交付。", "pass"),
        ]
    )
    outcome = await run_task("把限流方案做成 PPT 并配一张插图", model=model, context=ctx)
    print(f"  最终接话的 agent : {outcome.final_agent}   ← 控制权始终没转移")
    print(f"  登记到的产出      : {sorted(outcome.delivered)}")
    print("  落盘资产          :")
    for asset in outcome.assets:
        print(f"    - {asset}")


async def scene_research() -> None:
    print()
    print("=" * 72)
    print("③ 回答类 · 资料问答  →  research(as_tool) + 代码评审循环打回一轮")
    print("=" * 72)
    ctx = TaskContext()
    digest = {
        "question": "限流是怎么算的",
        "findings": [
            {
                "claim": "限流用加权滑动窗口",
                "evidence": "判定值 = 上一窗口计数 × 剩余比例 + 当前窗口计数",
                "source": "S1",
            }
        ],
        "citations": ["S1"],
        "confidence": "high",
    }
    model = ScriptedModel(
        [
            call("research", {"input": "限流是怎么算的"}, call_id="t1"),
            call("kb_search", {"query": "rate_limit", "top_k": 2}, call_id="k1"),
            _payload(digest),
            say("限流用加权滑动窗口，不是固定窗口。"),
            _feedback("回答缺出处编号，请补上 [1]。", "needs_improvement"),
            say("限流用加权滑动窗口，不是固定窗口 [1]。"),
            _feedback("已补出处，可以交付。", "pass"),
        ]
    )
    outcome = await run_task("限流是怎么算的", model=model, context=ctx)
    print(f"  最终接话的 agent : {outcome.final_agent}")
    print(f"  轮数 / 出口       : {outcome.attempts} / {outcome.exit_reason}")
    print(f"  取证材料          : {len(ctx.recall('research').findings)} 条结论")
    print(f"  最终答复          : {outcome.answer}")


async def scene_guardrail() -> None:
    print()
    print("=" * 72)
    print("④ 校验  →  出口护栏（不是 agent）：编造引用被拦下，不进入交付物")
    print("=" * 72)
    ctx = TaskContext()
    digest = {
        "question": "x",
        "findings": [{"claim": "a", "evidence": "b", "source": "S1"}],
        "citations": ["S1"],
        "confidence": "high",
    }
    model = ScriptedModel(
        [
            call("research", {"input": "x"}, call_id="t1"),
            _payload(digest),
            say("结论见 [9]。"),  # 本轮只检索到 1 条，[9] 是编造的
        ]
    )
    outcome = await run_task("查一下", model=model, context=ctx)
    print(f"  出口              : {outcome.exit_reason}")
    print(f"  触发护栏          : {outcome.guardrail}")
    print(f"  交付内容          : {outcome.answer!r}   ← 被拒的输出没有成为交付物")
    print(f"  剩余脚本步        : {model.remaining_steps}   ← 拦下后没有浪费后续模型调用")


async def main() -> None:
    await scene_literary()
    await scene_multi_deliverable()
    await scene_research()
    await scene_guardrail()
    print()
    print("=" * 72)
    print("四条请求 → 四种原语：handoff / as_tool ×2 + 代码渲染 / as_tool + 代码循环 / guardrail")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
