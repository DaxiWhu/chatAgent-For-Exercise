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
    完成率      有可交付内容的运行 / 有效运行                     （越高越好）
                判据 = ``outcome == delivered`` **或** （``exit_reason == transfer``
                且答案非空）。转交给专家后由它写完，这一次运行**有**产出，只是
                不走兜底判定（``outcome`` 是空）—— 只数 delivered 会把转交判成
                没完成，而低完成率会把人引去改提示词。
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

token 上限（``--max-tokens``，默认 10_000_000）
-----------------------------------------------
这条闸门是**保险丝**，不是优化。它管的是"整个脚本累计烧掉多少 token"，因为长任务
一旦陷入反复检索，单轮烧掉几十万 token 是可能的，而脚本自己不会停下来。

为什么要在**两个地方**同时设：

* 用例之间 —— 每跑完一条累计一次，到线就停止派发后续用例；
* 用例**内部** —— 把"剩余全局预算"塞给 ``AGENT_RUN_TOKEN_BUDGET``，让 ``budget.check``
  在模型调用之间就掐断。只做前者的话，单条用例自己就能把预算烧穿：闸门只在
  用例结束后才看得见它花了多少。

⚠️ 超支是**一跳**，不是按比例。闸门只能在"下一次模型调用之前"看到已经花了多少，
而那一跳会花多少事先不可知。实测上限压到 5000 时实际用了 8139（超 62.8%）、
压到 4000 时用了 4800（超 20%）—— 绝对值都是一次调用的量，不是"每次都超 60%"。
上限放到 10M 时，同样的绝对值占比 0.02%。真跑三次共 25 万 token（2.5%）。

⚠️ 预算触发时**不写** ``--out``：残缺的基线比没有基线更坏 —— 下一轮的 ``--baseline``
会拿它判退化，而缺的那几条会被读成"没有退化"。此时退出码 3。

取样次数（``--repeat``，默认 1）
-------------------------------
**单次读数不可用。** 同一个 prompt 连跑三次（同一个模型、同一份配置），用例 1 的
结局在 ``manual(guardrail)`` / ``failed(guard_stop)`` / ``partial(stuck)`` 之间跳，
token 是 31341 / 11676 / 14738，步数 23 / 11 / 9。也就是说"跑一次存成基线"存下来的
是一次随机抽样，下一轮拿它对比，``--baseline`` 会频繁误报退化。

所以基线请用 ``--repeat 3`` 生成（约 25 万 token）：数值取中位数、结局取多数，
每次的原始读数留在 ``outcome_mix`` / ``token_range`` / ``steps_range`` 里。

会话后端（``--session auto|memory|off``）
---------------------------------------
``auto``（默认）用项目配置的 PG。**PG 不可达时它会挂住**（实测 200 秒不返回），
因为引擎没有设连接超时 —— 这不是本脚本能修的，所以开跑前先做一次自检，
连不上就明说并建议改用 ``--session memory``。

``memory`` 用进程内会话：跨轮行为与真后端一致（SDK 的 Session 接口是同一个），
只是**不验持久化**。本地没起 PG 时，用它才能量到长任务能力。

用法
----
    python scripts/eval_longrun.py --session memory --repeat 3 \
        --out scripts/longrun_baseline.json          # 生成基线（本地没起 PG）
    python scripts/eval_longrun.py --baseline scripts/longrun_baseline.json
    python scripts/eval_longrun.py --only 2 --no-kb  # 只跑第 2 条、不接检索
    python scripts/eval_longrun.py --max-tokens 5000 # 验证闸门（一条就会被掐断）

退出码：0 正常；1 与基线相比退化；2 前置条件不满足（没配模型 / 基线不存在 /
会话后端不可用）；3 被 token 上限中断。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
import unicodedata
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
DEFAULT_MAX_TOKENS = 10_000_000
"""整个脚本累计的 token 上限（输入 + 输出，主链路 + 旁路）。

10M 是**保险丝**量级，不是预期消耗：5 条用例按现有规模一次约几万 token，
差着两个数量级。它存在的意义只有一个 —— 万一某条用例陷进"检索→重试→再检索"，
不至于让一次手动跑的账单失控。
"""


class _MemorySession:
    """进程内会话：跨轮行为与真后端一致，只差持久化。

    为什么需要它：``--session auto`` 在 PG 不可达时会**挂满 200 秒**（引擎没设
    连接超时），5 条用例加起来是十几分钟的纯等待，且全部落成 ``exit_reason=error``
    —— 一行有信息量的读数都拿不到。跨轮能力测的是"上一轮说过的话下一轮还在不在"，
    与"存在哪个库"无关，所以这里换成内存实现不改变用例的含义。

    ⚠️ 它**不能**替代"持久化正确性"的验证：进程重启后历史还在，只能靠真后端。
    """

    def __init__(self) -> None:
        self._items: list = []

    async def get_items(self, limit=None):  # noqa: ANN001
        items = list(self._items)
        return items[-limit:] if limit else items

    async def add_items(self, items) -> None:  # noqa: ANN001
        self._items.extend(items)

    async def pop_item(self):  # noqa: ANN201
        return self._items.pop() if self._items else None

    async def clear_session(self) -> None:
        self._items.clear()


_MEMORY_SESSIONS: dict[str, _MemorySession] = {}


def _install_memory_sessions() -> None:
    """把 ``runner`` 用的会话工厂换成内存实现。

    ⚠️ 必须打在 ``runner`` 的命名空间上：那里写的是 ``from .session import
    session_for``，改 ``session`` 模块的属性对**已绑定的名字**无效 —— 表现是
    "patch 了却没生效"，一个很容易白查半小时的坑。
    """
    from src.agents import runner as runner_mod

    def _factory(session_id: str):
        sid = (session_id or "").strip()
        if not sid:
            return None
        return _MEMORY_SESSIONS.setdefault(sid, _MemorySession())

    runner_mod.session_for = _factory
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


def _is_settled(exit_reason: str, outcome: str, answer: str) -> bool:
    """这一次运行有没有**可交付内容**。

    判据 = ``outcome == delivered`` **或** （``exit_reason == transfer`` 且答案非空）。
    后半截必须写出来：转交给专家后由它写完，``outcome`` 是空的（transfer 不走兜底
    判定），但这一次运行**有**产出。只数 delivered 会把转交判成"没完成"，而低
    完成率会把人引去改提示词。

    抽成纯函数是为了能被测试守住 —— 它原来内联在 ``_run_case`` 里，而那个函数要
    真模型才能跑，于是"把后半截删掉"这种变异**没有任何测试会发现**。
    """
    return outcome == "delivered" or (exit_reason == "transfer" and bool(answer))


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
    main_tokens = ctx.stats.input_tokens + ctx.stats.output_tokens
    bypass_tokens = ctx.stats.bypass_input_tokens + ctx.stats.bypass_output_tokens
    answer = (outcome.answer or "").strip()
    return {
        "name": case["name"],
        "exit_reason": outcome.exit_reason,
        # `exit_reason=="error"` 只说"崩了"，不说"崩在哪"。本机代理不通（ConnectError）
        # 与代码抛异常在读数上完全一样，而处置完全不同 —— 前者重跑就行。
        "error": str(getattr(outcome, "error", "") or ""),
        "outcome": getattr(verdict, "outcome", "") if verdict is not None else "",
        "reason": getattr(verdict, "reason", "") if verdict is not None else "",
        "answer_chars": len(answer),
        # 基线只存数字的话，"五条约束有没有同时满足"这种判据无法复查 ——
        # 而下次想确认"是不是它换了说法"就只能重跑一次（再花一次钱）。
        # 截到 1200 字：够覆盖这几条用例的完整产出，又不至于让基线文件膨胀。
        "answer_head": answer[:1200].replace("\n", " "),
        "steps": len(steps),
        "unfinished": sum(1 for s in steps if getattr(s, "status", "") != "ok"),
        "oscillation_cycles": int(getattr(ctx.steps, "oscillation_cycles", 0) or 0),
        "problems": kinds,
        "model_calls": ctx.stats.model_calls,
        "tool_calls": ctx.stats.tool_calls,
        # 闸门与汇总都用**合计**：旁路（压缩 / 反思）也是出站调用，也要花钱。
        # 只看主链路会系统性低估，而低估的方向是"预算还没到线、账单先到了"。
        "tokens": main_tokens + bypass_tokens,
        "tokens_main": main_tokens,
        "tokens_bypass": bypass_tokens,
        "cached": ctx.stats.cached_input_tokens,
        # 交付判据：转交给专家后由它写完，`outcome` 是空的（不走兜底判定），
        # 但这一次运行**有**可交付内容。只数 `outcome=="delivered"` 会把转交
        # 判成"没完成"，完成率随之虚低 —— 而低完成率会让人去改提示词。
        "settled": _is_settled(
            outcome.exit_reason, getattr(verdict, "outcome", ""), answer
        ),
        "seconds": round(wall, 1),
        "min_steps": case.get("min_steps", 0),
    }


def _rate(rows: list[dict], key: str, value: str) -> float:
    return sum(1 for r in rows if r.get(key) == value) / len(rows) if rows else 0.0


def _settled_rate(rows: list[dict]) -> float:
    """有可交付内容的比例（``settled`` 见 ``_run_case`` 的说明）。"""
    return sum(1 for r in rows if r.get("settled")) / len(rows) if rows else 0.0


def _problem_rate(rows: list[dict], problem: str) -> float:
    """出过这个问题的**运行次数** / 总运行次数。

    分母是运行次数而不是用例数：``--repeat 3`` 时一条用例背后是 3 次真实运行，
    拿用例数当分母会把"3 次里 1 次报过"算成"这条用例 100% 偏离"。
    """
    total = sum(max(1, r.get("runs", 1)) for r in rows)
    if not total:
        return 0.0
    hits = 0
    for r in rows:
        # 单次运行的 row 没有 problems_runs（那是合并时才生成的），回退到 problems：
        # 没有它的话 `--repeat 1` 会恒返回 0 —— 一个"看起来一切正常"的假象。
        counts = r.get("problems_runs")
        if counts is None:
            counts = {k: 1 for k, v in (r.get("problems") or {}).items() if v}
        hits += counts.get(problem, 0)
    return hits / total


def _summary(rows: list[dict]) -> dict:
    """把逐条读数汇成一页（口径见模块 docstring，**只在这里算一次**）。"""
    if not rows:
        return {}
    return {
        "cases": len(rows),
        # 完成率用 settled：转交给专家写完的那一条也算交付，否则完成率虚低。
        "delivered_rate": round(_settled_rate(rows), 4),
        "partial_rate": round(_rate(rows, "outcome", "partial"), 4),
        "fallback_rate": round(
            _rate(rows, "outcome", "manual") + _rate(rows, "outcome", "failed"), 4
        ),
        "transfer_rate": round(_rate(rows, "exit_reason", "transfer"), 4),
        "drifting_rate": round(_problem_rate(rows, "off_track"), 4),
        "looping_rate": round(_problem_rate(rows, "loop"), 4),
        "stuck_rate": round(_problem_rate(rows, "stuck"), 4),
        "avg_steps": round(sum(r["steps"] for r in rows) / len(rows), 2),
        "avg_unfinished": round(sum(r["unfinished"] for r in rows) / len(rows), 2),
        "avg_tokens": round(sum(r["tokens"] for r in rows) / len(rows), 1),
        "avg_seconds": round(sum(r["seconds"] for r in rows) / len(rows), 1),
        "short_cases": [r["name"] for r in rows if r["steps"] < r["min_steps"]],
    }


def _pad(text: str, width: int) -> str:
    """按**显示宽度**右补空格。

    中文与 ``×`` 在终端占两列，用 ``len()`` 补宽会让后面每一列整体右移 ——
    实测出现过 ``loop×1`` 与下一列的 ``18`` 粘成 ``loop×118``，读表的人会
    以为"打转了 118 次"。一个只影响观感的函数，错起来是影响判断的。
    """
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - shown)


def _sum_problems(samples: list[dict]) -> dict:
    """把各次的 `problems`（问题 → 次数）逐键相加。"""
    from collections import Counter

    totals: Counter = Counter()
    for s in samples:
        for key, value in (s.get("problems") or {}).items():
            totals[key] += int(value or 0)
    return dict(totals.most_common())


def _merge_samples(case: dict, samples: list[dict]) -> dict:
    """把同一条用例的 N 次重复合成一行。

    为什么要重复：同一个 prompt 连跑三次，用例 1 的结局在
    ``manual(guardrail)`` / ``failed(guard_stop)`` / ``partial(stuck)`` 之间跳，
    token 是 31341 / 11676 / 14738，步数 23 / 11 / 9。单次读数就是一次随机抽样，
    拿它当基线，``--baseline`` 会频繁误报"退化"。

    合成口径：
    * 数值（步数 / token / 耗时）取**中位数** —— 均值会被一次跑飞的那次拉偏；
    * 结局取**出现最多的那个**（并列时取先出现的），同时把分布留在 ``outcome_mix``；
    * ``settled`` 取多数。
    """
    import statistics
    from collections import Counter

    if len(samples) == 1:
        merged = dict(samples[0])
        merged["runs"] = 1
        merged["outcome_mix"] = {}
        merged["contested"] = False
        merged["problems_runs"] = dict.fromkeys(samples[0]["problems"], 1)
        return merged

    def med(key: str) -> float:
        return statistics.median([s[key] for s in samples])

    mix = Counter((s["outcome"] or "-", s["reason"] or "-", s["exit_reason"]) for s in samples)
    top, top_count = mix.most_common(1)[0]
    merged = dict(samples[0])
    merged.update(
        {
            "runs": len(samples),
            # "取多数"在**三次全不同**时会退化成"取第一次"，而那个结果是随机的。
            # 不显式标出来，读的人会把"第一次恰好是 error"当成"这条用例崩了"。
            "contested": top_count * 2 <= len(samples),
            "outcome": "" if top[0] == "-" else top[0],
            "reason": "" if top[1] == "-" else top[1],
            "exit_reason": top[2],
            "steps": int(med("steps")),
            "unfinished": int(med("unfinished")),
            "model_calls": int(med("model_calls")),
            "tool_calls": int(med("tool_calls")),
            "tokens": int(med("tokens")),
            "tokens_main": int(med("tokens_main")),
            "tokens_bypass": int(med("tokens_bypass")),
            "cached": int(med("cached")),
            "answer_chars": int(med("answer_chars")),
            "seconds": round(med("seconds"), 1),
            "settled": sum(1 for s in samples if s["settled"]) * 2 > len(samples),
            # 错误必须**全部**留下：结局取多数时可能取到成功的那次，而第一个样本
            # 里没有 error —— 那样"其中一次崩了"就彻底消失了。
            "error": " | ".join(sorted({s["error"] for s in samples if s.get("error")})),
            # 次数要**相加**（一次运行里 stuck 报 3 次就是 3），不是"这个键出现过几回"。
            # 用 Counter(k for s in samples for k in s["problems"]) 会让它恒等于
            # "报过的运行数"，于是与 problems_runs 变成同一个值 —— 两个都失去意义。
            "problems": _sum_problems(samples),
            # 折叠口径必须与"次数"分开存：一次运行里 stuck 可能报 3 次，
            # 用次数当分母会让"3 次里 1 次报过"被读成"报了很多次"。
            # 偏离率这种"有多少运行出问题"的指标要的是**运行次数**。
            "problems_runs": dict(
                Counter(k for s in samples for k in s["problems"] if s["problems"].get(k))
            ),
            "outcome_mix": {f"{a}/{b}/{c}": n for (a, b, c), n in mix.items()},
            "token_range": [min(s["tokens"] for s in samples),
                            max(s["tokens"] for s in samples)],
            "steps_range": [min(s["steps"] for s in samples),
                            max(s["steps"] for s in samples)],
        }
    )
    return merged


def _print_rows(rows: list[dict]) -> None:
    # "问题"放最后一列、且不设宽度上限：它长短不定，放在中间一定会把后面的
    # 数字挤过去（实测 `off_track×1` 吃掉宽度后与 `11` 粘成 `off_track×111`）。
    cols = [
        ("#", 3), ("交付", 5), ("结局", 10), ("原因", 16), ("步", 4), ("未完成", 7),
        ("往复", 5), ("模型", 5), ("工具", 5), ("token", 8), ("命中", 7),
        ("字数", 6), ("秒", 6),
    ]
    head = "".join(_pad(name, width) for name, width in cols) + "问题"
    print(head)
    print("-" * len(head))
    for index, row in enumerate(rows, start=1):
        problems = ",".join(f"{k}×{v}" for k, v in row["problems"].items()) or "-"
        cells = [
            str(index),
            "是" if row.get("settled") else "否",
            # 先补 "-" 再加问号：顺序反了的话，outcome 为空的用例会显示成孤零零一个 "?"
            (row["outcome"] or "-") + ("?" if row.get("contested") else ""),
            row["reason"] or "-",
            str(row["steps"]),
            str(row["unfinished"]),
            str(row["oscillation_cycles"]),
            str(row["model_calls"]),
            str(row["tool_calls"]),
            str(row["tokens"]),
            str(row["cached"]),
            str(row["answer_chars"]),
            str(row["seconds"]),
        ]
        line = "".join(_pad(cell, width) for cell, (_, width) in zip(cells, cols, strict=True))
        print(line + problems)
    if any(r.get("contested") for r in rows):
        print("\n? 后缀 = 这条的各次结局互不相同（无多数），结局列只是其中一个样本，看 outcome_mix")


def _print_summary(summary: dict) -> None:
    print(f"\n{'=' * 78}")
    print("长任务基线")
    print(f"{'=' * 78}")
    if not summary:
        print("没有有效运行（全部异常/限流）")
        return
    print(f"用例数        : {summary['cases']}")
    print(f"完成率        : {summary['delivered_rate']:.0%}   （有可交付内容，含转交）")
    print(f"部分结果率    : {summary['partial_rate']:.0%}   （被打断但交了东西）")
    print(f"兜底率        : {summary['fallback_rate']:.0%}   （manual + failed）")
    print(f"转交率        : {summary['transfer_rate']:.0%}   （exit_reason=transfer）")
    print(f"偏离率        : {summary['drifting_rate']:.0%}   （反思报过 off_track）")
    print(f"打转率        : {summary['looping_rate']:.0%}   / 卡住率 {summary['stuck_rate']:.0%}")
    print(f"平均步数      : {summary['avg_steps']}  （平均未完成 {summary['avg_unfinished']} 步）")
    print(f"平均 token    : {summary['avg_tokens']}   平均耗时 {summary['avg_seconds']}s")
    if summary.get("repeat", 1) > 1:
        print(f"取样          : 每条 {summary['repeat']} 次（数值取中位数、结局取多数）")
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
    ap.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help=f"累计 token 上限（默认 {DEFAULT_MAX_TOKENS}；0=不限，**不推荐**）",
    )
    ap.add_argument(
        "--session",
        choices=("auto", "memory", "off"),
        default="auto",
        help="auto=项目配置的 PG；memory=进程内（本地没起 PG 时用）；off=不带跨轮历史",
    )
    ap.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="每条用例重复跑几次（≥2 时数值取中位数、结局按多数，见 _merge_samples）",
    )
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
    cap = args.max_tokens
    print(f"token 上限: {cap if cap else '不限'}"
          f"（累计、含旁路；到线立刻停，且不写基线）")
    budget_env = os.getenv("AGENT_RUN_TOKEN_BUDGET", "0") or "0"
    if budget_env not in ("0", ""):
        print(f"⚠️ .env 里已有单次运行预算 AGENT_RUN_TOKEN_BUDGET={budget_env}；"
              f"本轮会按剩余全局预算覆盖它（每次运行前重设）")
    if args.no_kb:
        print("检索      : 已按 --no-kb 关闭（research 会返回'检索不可用'）")

    session_mode = args.session
    if session_mode == "auto":
        # 开跑之前探一次：PG 不可达时 get_items 会**挂住**（引擎没设连接超时），
        # 盲跑的结果是十几分钟纯等待 + 全成 error 行，一行有效读数都没有。
        from src.agents.session import ping_session_backend

        if not await ping_session_backend():
            print("❌ 会话后端不可用（DB_URL 指向的 PG 连不上）。")
            print("   直接用 --session memory 重跑：跨轮行为一致，只是不验持久化。")
            return 2
        print("会话后端  : PG 连通")
    elif session_mode == "memory":
        _install_memory_sessions()
        print("会话后端  : 进程内（跨轮行为一致，**不验持久化**）")
    else:
        print("会话后端  : 关闭（不带跨轮历史；第 4 条跨轮用例必然失败）")
        if args.only in (0, 4):
            print("⚠️ 第 4 条用例考的就是跨轮记忆，--session off 下它的结论没有意义")

    selected = (
        [(args.only, CASES[args.only - 1])] if args.only else list(enumerate(CASES, start=1))
    )

    rows: list[dict] = []
    errors: list[dict] = []
    skipped: list[str] = []
    spent = 0  # 累计 token（这条闸门只看钱，不看别的）
    truncated = False
    for position, (index, case) in enumerate(selected):
        remaining = cap - spent if cap else 0
        if cap and remaining <= 0:
            truncated = True
            skipped = [c["name"] for _, c in selected[position:]]
            print(f"\n⛔ token 上限已用尽（累计 {spent} / {cap}），跳过剩余 {len(skipped)} 条")
            break
        print(f"\n{'=' * 78}")
        print(f"[{index}] {case['name']}")
        print(f"{'=' * 78}")
        print(f"关注点    : {case['watch']}")
        samples: list[dict] = []
        for attempt in range(max(1, args.repeat)):
            if cap and spent >= cap:
                truncated = True
                break
            os.environ["AGENT_RUN_TOKEN_BUDGET"] = (
                str(max(1, cap - spent)) if cap else "0"
            )
            if args.repeat > 1:
                print(f"\n--- 第 {attempt + 1}/{args.repeat} 次 ---")
            # 每次重复用**独立** session：共用的话第二遍会带着第一遍的历史，
            # 用例 4（跨轮约束）尤其会被污染 —— 它本来考的就是"上一句记不记得"。
            sid = (
                ""
                if session_mode == "off"
                else f"longrun-{index}-{attempt}-{int(time.time())}"
            )
            try:
                row = await _run_case(case, use_kb=not args.no_kb, session_id=sid)
            except Exception as e:  # noqa: BLE001  探测脚本要把失败本身报出来
                kind = type(e).__name__
                limited = "RateLimit" in kind or "429" in str(e)
                tb = traceback.format_exc()
                print(f"{'⏳' if limited else '❌'} "
                      f"{'上游限流' if limited else '执行异常'}: {kind}: {e}")
                # 只留一句 message 的话，"哪个模块把它序列化的"完全看不出来 ——
                # 而这类 TypeError 往往藏在某个 `json.dumps` 里，光看类型名无从下手。
                print("   调用栈末尾：")
                for line in tb.strip().splitlines()[-8:]:
                    print(f"   {line}")
                errors.append(
                    {
                        "name": case["name"],
                        "error": f"{kind}: {e}",
                        "rate_limited": limited,
                        "traceback": tb,
                    }
                )
                continue
            samples.append(row)
            spent += row["tokens"]
            print(f"结局      : outcome={row['outcome'] or '-'} reason={row['reason'] or '-'} "
                  f"exit_reason={row['exit_reason']}"
                  + (f" error={row['error']}" if row.get("error") else ""))
            print(f"过程      : 步数={row['steps']} 未完成={row['unfinished']} "
                  f"往复={row['oscillation_cycles']} 问题={row['problems'] or '无'}")
            print(f"成本      : 模型={row['model_calls']} 工具={row['tool_calls']} "
                  f"token={row['tokens']}（主 {row['tokens_main']} / 旁 {row['tokens_bypass']}，"
                  f"命中 {row['cached']}）耗时={row['seconds']}s")
            if row["answer_head"]:
                print(f"产出开头  : {row['answer_head'][:120]}")
            print(f"累计 token: {spent}{f' / {cap}' if cap else ''}")
            if attempt < args.repeat - 1:
                await asyncio.sleep(max(0.0, args.gap))

        if not samples:
            continue
        row = _merge_samples(case, samples)
        rows.append(row)
        if args.repeat > 1:
            print(f"\n★ {args.repeat} 次合成：结局={row['outcome'] or '-'}/{row['reason'] or '-'}"
                  f" 步数={row['steps']}（{row['steps_range'][0]}~{row['steps_range'][1]}）"
                  f" token={row['tokens']}（{row['token_range'][0]}~{row['token_range'][1]}）")
            print(f"  结局分布：{row['outcome_mix']}")
        if position < len(selected) - 1:
            await asyncio.sleep(max(0.0, args.gap))

    print(f"\n{'=' * 78}")
    _print_rows(rows)
    summary = _summary(rows)
    if summary:
        # 记**实际**样本数的最小值，不是 `--repeat`：某条用例中途抛异常时它只有
        # 2 个样本，标成 3 会让读的人以为所有条都跑了 3 次。
        runs = [r.get("runs", 1) for r in rows]
        summary["repeat"] = max(1, min(runs)) if runs else max(1, args.repeat)
    _print_summary(summary)
    if cap:
        pct = (spent / cap) if cap else 0
        print(f"累计 token    : {spent} / {cap}（{pct:.1%}）")

    if errors:
        limited = sum(1 for e in errors if e["rate_limited"])
        print(f"\n未计入统计 : {len(errors)} 条（其中上游限流 {limited} 条）")
        for e in errors:
            print(f"  - {e['name']}: {e['error'][:90]}")

    if truncated:
        # 残缺基线比没有基线更坏：下一轮 --baseline 会拿它判退化，缺的几条会被读成
        # "没有退化"。所以宁可这一次不写，也不要留一份会误导人的存档。
        print(f"\n⛔ 被 token 上限中断（累计 {spent} / {cap}）：本次**不写** --out。")
        print("   已跳过的用例：")
        for name in skipped:
            print(f"   - {name}")
        print("   若确认是某条用例失控，先单独跑它看步数；若只是上限压得太低，"
              "调大 --max-tokens 再来。")
        return 3

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
        # 环境指纹必须一起存：模型、会话后端、有没有检索，任何一项不同，
        # 两份数字就不可比。只存指标的话，以后换模型再对比会把"模型变强"
        # 看成"提示词改好了"，或者反过来。
        payload = dict(summary)
        # 逐条读数必须一起存：只有汇总时，下一轮"完成率掉了 20 个点"无法回答
        # "是哪条掉的、掉成什么样" —— 而重新确认要再花一次钱。
        payload["cases_detail"] = [
            {
                "name": r["name"],
                "outcome": r["outcome"],
                "reason": r["reason"],
                "exit_reason": r["exit_reason"],
                "error": r.get("error", ""),
                "settled": r["settled"],
                "steps": r["steps"],
                "steps_range": r.get("steps_range"),
                "token": r["tokens"],
                "token_range": r.get("token_range"),
                "cached": r["cached"],
                "answer_chars": r["answer_chars"],
                "problems": r["problems"],
                "outcome_mix": r.get("outcome_mix"),
                "answer_head": r["answer_head"][:300],
            }
            for r in rows
        ]
        payload["env"] = {
            "model": model_name,
            "api_base": base_url,
            "session": session_mode,
            "kb": not args.no_kb,
            "max_tokens": cap,
            "guard": "on",
            "spent_tokens": spent,
        }
        # 异常也进基线：不加进统计（它是"没跑成"，不是"跑得差"），但必须留在
        # 档案里 —— 否则"上次那条 TypeError"在下一次跑之后就没有任何痕迹了。
        if errors:
            payload["errors"] = [
                {"name": e["name"], "error": e["error"],
                 "rate_limited": e["rate_limited"],
                 "traceback_tail": "\n".join(e.get("traceback", "").strip().splitlines()[-6:])}
                for e in errors
            ]
        # newline="\n" 是必须的：Windows 上 write_text 默认会把 `\n` 转成 CRLF，
        # 而仓库要求 LF —— 不显式指定的话，这个文件一提交就是满屏换行差异。
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"\n汇总已写入: {out_path}")

    return code


if __name__ == "__main__":
    # ⚠️ Windows 默认是 ProactorEventLoop，psycopg 的异步模式**不支持**它 ——
    # 不换循环的话，只要用了真会话后端，第一轮就会抛，整轮变成 exit_reason="error"，
    # 与"网关挂了"长得一模一样。正式入口用的是 app/serve.py 的 loop_factory。
    import selectors

    raise SystemExit(
        asyncio.run(
            main(),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    )
