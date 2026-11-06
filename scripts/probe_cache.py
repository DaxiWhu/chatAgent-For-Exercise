"""前缀缓存探针 —— 问清"命中量看得见吗、这个装配的命中率是多少"。

为什么需要它
------------
上游的前缀缓存（DeepSeek 的硬盘上下文缓存）**默认开启，不需要改代码**：只要请求
前缀完整匹配一个 cache prefix unit，命中部分就按缓存价计费。所以"省钱"的关键不是
写缓存，而是两件别的事：

* **能不能看见**命中量 —— 这取决于网关回不回 usage 里的字段；
* **前缀有没有被打断** —— 只要 ``instructions`` 里插了时间戳/uuid，命中率立刻归零。

这两件事的失效都是**零报错**的：账单比预期高，日志指标干干净净。2026-11-01 之前
本项目就处在这个状态（只累加 input/output，缓存命中量一个都没记）。

两个模式
--------
``--raw``（默认）
    连发两次**同一段长前缀**的最小请求，把两次的 ``usage`` 原样打出来。
    要回答的是"字段在不在"：第 1 次必然 0（冷启动），第 2 次非零才算看得见。
    两次都是 0 时才需要怀疑网关，别此前先怀疑缓存。

``--agent N``
    用**真实装配**（前台 + 专家 + 工具 + 真模型）跑 N 轮同一会话，逐轮打命中率。
    要回答的是"这个部署实际省到什么程度"。⚠️ 会话用进程内实现，不用 PG ——
    本机库没起时 ``get_items`` 会超时，那个异常发生在 ``Runner.run`` 内部 ⇒ 整轮
    变成 ``exit_reason="error"``，命中率一行都测不到。

用法
----
    python scripts/probe_cache.py                 # 字段在不在（花几厘钱）
    python scripts/probe_cache.py --agent 3       # 真实链路三轮的命中率

⚠️ 会**真的调用上游模型**（``--agent`` 那一模式是几轮真对话）。用例刻意压到最小。
⚠️ 只读：不改任何配置，也不写任何状态。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


#: 一段够长的固定前缀。太短的话 cache prefix unit 根本不落盘，探针会得出
#: "网关不回字段"的错误结论 —— 那是探针自己在制造假阴性。
PREFIX = "以下是一段用于测试前缀缓存的固定说明文字，请只回一个字。" * 40

PROMPTS = [
    "用一句话说明什么是限流。",
    "那它对用户体验有什么影响？",
    "再说一句该怎么缓解。",
    "最后一句：这件事最容易被忽略什么？",
]


def _cfg() -> tuple[str, str, str]:
    base = os.environ.get("LLM_API_BASE", "").strip() or "https://api.deepseek.com"
    key = os.environ.get("LLM_API_KEY", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip() or "deepseek-chat"
    return base, key, model


def _hit_of(usage: Any) -> int:
    """从一次性响应的 usage 里取命中数（两处字段都要看，便于证明它们同源）。"""
    details = getattr(usage, "prompt_tokens_details", None)
    nested = getattr(details, "cached_tokens", 0) if details is not None else 0
    extra = getattr(usage, "model_extra", None) or {}
    return int(extra.get("prompt_cache_hit_tokens", 0) or nested or 0)


async def probe_raw() -> int:
    """连发两次同一前缀，打印原始 usage。返回 0 = 看得见，1 = 两次都是 0。"""
    from openai import AsyncOpenAI

    base, key, model = _cfg()
    if not key:
        print("✗ LLM_API_KEY 为空，先配 .env")
        return 1
    print(f"网关={base} 模型={model}")
    hits = []
    client = AsyncOpenAI(base_url=base, api_key=key, timeout=60)
    try:
        for i in range(2):
            r = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": PREFIX},
                    {"role": "user", "content": "回一个字：好"},
                ],
                max_tokens=8,
                temperature=0,
            )
            usage = r.usage
            if usage is None:
                print(f"第{i + 1}次：网关没回 usage")
                return 1
            payload = json.dumps(usage.model_dump(), ensure_ascii=False)
            print(f"第{i + 1}次 usage = {payload}")
            hits.append(_hit_of(usage))
    finally:
        await client.close()

    if not any(hits):
        print(
            "✗ 两次都没命中：可能是网关不回字段，也可能是这段前缀没落盘。"
            "先确认网关文档里的字段名，再怀疑缓存本身。"
        )
        return 1
    print(f"✓ 第 2 次命中 {hits[1]} token —— 命中量看得见")
    return 0


class _MemorySession:
    """进程内会话（本模式要量的是前缀，不是持久化）。"""

    def __init__(self) -> None:
        self._items: list[Any] = []

    async def get_items(self, limit: int | None = None) -> list[Any]:
        items = list(self._items)
        return items[-limit:] if limit else items

    async def add_items(self, items: list[Any]) -> None:
        self._items.extend(items)

    async def pop_item(self) -> Any | None:
        return self._items.pop() if self._items else None

    async def clear_session(self) -> None:
        self._items.clear()


async def probe_agent(rounds: int) -> int:
    """真实装配跑 N 轮，逐轮打命中率。返回非 0 = 链路没就绪。"""
    from src.agents import ENTRY_AGENT, TaskContext, is_ready, runtime
    from src.agents import runner as runner_mod
    from src.agents.runner import run_agent

    base, key, model = _cfg()
    runtime.configure(model_name=model, api_key=key, base_url=base, timeout=60.0)
    if not is_ready():
        print(f"✗ agent 链路未就绪：{runtime.ready_reason()}")
        return 1

    shared = _MemorySession()
    # 打在 ``runner`` 的命名空间上。
    # （2026-11-06 起两种都生效：``runner.session_for`` 是个惰性函数，每次调用现取
    #  ``session_module.session_for``，所以改源模块或改本模块都能被看到。
    #  这里保持改本模块，只是历史写法，不影响结果。）
    runner_mod.session_for = lambda _sid: shared

    ctx = TaskContext()
    print(f"{'轮':<4}{'模型调用':>8}{'输入':>8}{'命中':>8}{'命中率':>9}{'输出':>7}  退出原因")
    prev = (0, 0, 0)
    for i in range(min(rounds, len(PROMPTS))):
        out = await run_agent(
            ENTRY_AGENT,
            prompt=PROMPTS[i],
            context=ctx,
            session_id="cache-probe",
            max_turns=3,
        )
        s = ctx.stats
        used_in = s.input_tokens - prev[0]
        used_hit = s.cached_input_tokens - prev[1]
        used_out = s.output_tokens - prev[2]
        prev = (s.input_tokens, s.cached_input_tokens, s.output_tokens)
        rate = used_hit / used_in if used_in else 0.0
        print(
            f"{i + 1:<4}{s.model_calls:>8}{used_in:>8}{used_hit:>8}"
            f"{rate:>8.0%}{used_out:>7}  {out.exit_reason}"
        )
    print(
        "\n口径：命中量 / 输入量，都是**本次运行切片**。第 1 轮冷启动也可能是非零"
        "（命中的是别的请求留下的 unit）；新部署的第一次请求必然是 0。"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="前缀缓存探针（默认 --raw）")
    ap.add_argument(
        "--agent", type=int, metavar="N", default=0,
        help="改用真实装配跑 N 轮，量命中率",
    )
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:  # pragma: no cover - 依赖一定有，缺了也能靠环境变量跑
        pass

    if args.agent:
        # ⚠️ Windows 默认 ProactorEventLoop，psycopg 异步不支持它。本模式虽然用内存
        # 会话，但保持与正式入口同一个 loop，避免"探针能跑、线上不行"的错觉。
        import selectors

        return asyncio.run(
            probe_agent(args.agent),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    return asyncio.run(probe_raw())


if __name__ == "__main__":
    raise SystemExit(main())
