"""用**真实模型**跑「工具选择」评测集 —— 回答"模型到底会不会用这些工具"。

为什么需要它（与 ``agent_capability_probe.py`` 的分工）
----------------------------------------------------
那个脚本看的是**链路能力**（会不会转交、评审几轮过、护栏误不误伤）；
这个脚本只看一件事：**给定一个请求，模型选对了工具没有**。

两者不能合并，因为失败原因完全不同：链路能力差是 prompt 与编排的问题，
工具选择差是**工具描述与参数 schema** 的问题 —— 而后者只有"看它选了哪个"
才能定位。举例：`calculate` 从没被选中过时，答案里的数字照样完全合理
（模型心算），从产出上根本看不出来。

判据（与 ``agent_tool_eval_set.json`` 的字段一一对应）
----------------------------------------------------
前三条判"动作"，后两条判"结果"：

* ``expect_tools``   —— 必须**出现**（子集判定：多查一次不算错）；
* ``expect_tools_any``—— **任一**出现即可。用于"达成目的有不止一条合理路径"的场合：
  例如"读一个不存在的文件"，先 ``list_assets`` 看清目录再答**比**直接读更好，
  硬要求 ``read_asset`` 会把更好的做法判成错误；
* ``forbid_tools``   —— 一个都**不许**出现（跑错方向比跑少一步更贵）；
* ``expect_no_tools``—— 闲聊：一步动作都不该有；
* ``expect_answer_any`` / ``expect_answer_all`` —— **答案层**硬判定：最后给出的数字/正文
  对不对。这条是 2026-10-31 补的，因为前两层全绿也可能全是错的：模型调了
  ``calculate``、工具也确实返回了，但它把**自己心算的**数写进了答复；
* ``expect_text_regex``—— **软判定**，任一正则命中即算过。**不要用字面词表**：
  同一个意思实测出现过「做不到 / 做不了 / 无法访问 / 打不开 / 无法打开 /
  不能发送 / 不能帮你发送」—— 2026-10-31 一天之内被字面词表误报四次，每次都是
  **行为正确、判据判红**。正则描述的是"否定词 + 动作词"这个句法，不是某一个说法。
* ``forbid_text_any``—— 答案里**一个都不许出现**（硬判定）。它治的是"假装做到了"：
  没有发邮件的工具，却回"邮件已发送至 …" —— 提问的人会等一封永远不来的邮件。
  正向关键词表天生脆弱（``做不了`` ≠ ``做不到``），所以最坏的那种形态要用负向判据兜住。

三层指标分开报（``选择`` / ``执行`` / ``结果``），因为它们的分母和处置都不同。
混成一个通过率时，"选错工具""工具跑不通""结果算错"长得一模一样。

数据从哪来
----------
工具序列直接读 ``TaskContext.steps``（本项目自己的动作清单），而不是从模型响应里
解析 —— 那正是"步骤清单"存在的理由，两处分别解析必然漂，而漂的表现是
"评测说调了、日志里没调"。

⚠️ 多轮用的是**内存会话替身**（官方没有内存版 ``Session``，见 ``session.py``）。
所以本脚本验证的是"跨轮能不能拿到历史"，不验证 PG 后端本身。
⚠️ 会**真的调用上游模型**（花钱）。默认 15 条用例 / 18 个判定轮次，单条最多 2 轮。

⚠️ 答案层判据**不许依赖运行日期**。断言"今天是 31 号"这类用例今天过、明天红 ——
那是评测自己过期，不是被测对象退步。所以时间类用例只判"有没有调时间工具"。

用法
----
    python scripts/agent_tool_eval.py                    # 走主网关配置（LLM_*，当前 DeepSeek）
    python scripts/agent_tool_eval.py --model deepseek    # 走评测对照线（DEEPSEEK_*）
    python scripts/agent_tool_eval.py --only time-today
    python scripts/agent_tool_eval.py --json tmp_probe/tool_eval.json

    # 与基线比（CI 的 agent-quality job 每晚就是这么跑的）：只判"比基线差"才非 0
    python scripts/agent_tool_eval.py --baseline scripts/agent_tool_eval_baseline.json

    # 更新基线：**手动**执行，确认新数字合理之后。脚本永不自动覆写基线 ——
    # 写完再读自己 = 自愈门禁（每一项都算持平，永远绿）。
    python scripts/agent_tool_eval.py --json scripts/agent_tool_eval_baseline.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
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


def _bigrams(text: str) -> set[str]:
    """字符二元组（去掉空白）。

    桩检索器要判"这两句话是不是在讲同一件事"，而项目里没有中文分词依赖。
    单字重合不行：「怎」「么」「的」在任意两句中文里都会撞上，等于没有相关性；
    双字组能挡住这类误命中，又不引入新依赖。
    """
    chars = [ch for ch in text if not ch.isspace()]
    if len(chars) < 2:
        return set(chars)
    return {a + b for a, b in zip(chars, chars[1:], strict=False)}


def _stub_factory(corpus: list[dict]) -> Any:
    """把评测集里的固定语料包成检索器工厂（接口同 ``TaskContext.retriever_factory``）。"""

    class StubRetriever:
        """评测专用的**桩检索器**。

        为什么需要它
        ------------
        ``retriever_factory=None`` 时 ``kb_search`` 恒返回"检索不可用"，``research``
        因此拿不到任何素材 ⇒ 所有靠取证的专家（``build_slide_deck`` 等）每次都在走
        **降级路径**，而"拿到素材之后怎么做"这段一次也没被测到。
        上一轮 ``slide-outline`` 掉到 17/18 正是如此：检索为空后模型自己手写大纲，
        评测分不出这是"模型偏好"还是"环境逼的"。

        ⚠️ 它**不是**知识库：不切分、不向量化、不重排，只按字符重合度排序。
        所以报告必须标 ``retrieval=stub`` —— "检索通了"不等于 RAG 链路通了。
        """

        def __init__(self, rows: list[dict]) -> None:
            self._rows = [dict(r) for r in rows if (r.get("content") or "").strip()]

        def search(self, query: str, top_k: int = 4) -> list[tuple[dict, float]]:
            needle = _bigrams(query or "")
            if not needle:
                return []
            # 命中门槛：**两个不同的双字组**（查询本身更短时降为全中）。
            # 只按单字重合判会把"今天天气怎么样"和"限流是怎么算的"连起来 ——
            # 它们共用了"怎""么"两个字。中文的高频字太多，单字重合等于没有相关性；
            # 而桩一旦对任何查询都给素材，"检索不到时会不会如实说"就再也测不出来。
            need = min(2, len(needle))
            scored: list[tuple[dict, float]] = []
            for index, row in enumerate(self._rows):
                hit = len(needle & _bigrams(row["content"]))
                if hit < need:
                    continue
                scored.append((self._fill(row, index), hit / len(needle)))
            scored.sort(key=lambda pair: -pair[1])
            return scored[: max(1, int(top_k or 4))]

        @staticmethod
        def _fill(row: dict, index: int) -> dict:
            return {
                "content": row["content"],
                "text": row.get("text") or row["content"],
                "source": row.get("source") or "eval_stub.md",
                "source_path": row.get("source_path") or "eval_stub.md",
                "section": row.get("section") or "",
                "kind": row.get("kind") or "text",
                "parser": row.get("parser") or "stub",
                "chunk_id": row.get("chunk_id") or f"stub::{index}",
            }

    retriever = StubRetriever(corpus)
    return lambda: retriever


def _build_model(which: str, structured_output: str = "") -> Any:
    """按名字装配模型（两条都是 OpenAI 兼容协议，走同一个官方实现）。

    ``structured_output`` 为空时交给 ``build_model`` 去读环境变量 —— 命令行显式值
    优先，这样同一份 .env 下可以不改配置就跑两个档位做对照。
    """
    from src.agents.model import build_model

    extra = {"structured_output": structured_output} if structured_output else {}

    if which == "deepseek":
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            raise SystemExit("缺少 DEEPSEEK_API_KEY（.env 里没有配）")
        return build_model(
            model_name=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
            api_key=key,
            base_url=os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com"),
            timeout=60.0,
            **extra,
        )

    key = os.environ.get("LLM_API_KEY", "")
    if not key:
        raise SystemExit("缺少 LLM_API_KEY（.env 里没有配）")
    return build_model(
        model_name=os.environ.get("LLM_MODEL", "deepseek-chat"),
        api_key=key,
        base_url=os.environ.get("LLM_API_BASE", ""),
        timeout=60.0,
        **extra,
    )


def _turns_of(case: dict) -> list[dict]:
    """把单轮 / 多轮两种写法归一成轮次列表。"""
    if "turns" in case:
        return list(case["turns"])
    return [case]


def _repeat_summary(step_log: Any) -> list[dict]:
    """把"同一动作做了几次"压成人能读的形态：``[{"tool": ..., "count": n}]``。

    口径直接复用 ``StepLog.repeat_counts()``（按**指纹** = 工具名 + 规范化参数），
    不在这里另数一遍 —— 另数一定与反思闸门/指标播报的判定漂，而漂了的判据比没有更糟。
    指纹是短哈希、反查不出工具名，所以用"指纹 -> 工具名"的映射翻回可读的名字。
    """
    counts = step_log.repeat_counts()
    if not counts:
        return []
    name_by_fp = {s.fingerprint: s.tool for s in step_log.steps if s.fingerprint}
    return [
        {"tool": name_by_fp.get(fp, "?"), "count": n}
        for fp, n in sorted(counts.items(), key=lambda kv: -kv[1])
    ]


def _squash(text: str) -> str:
    """去掉千分位与空白再比 —— ``75,680`` 与 ``75680`` 是同一个数。

    ⚠️ 这条是被实测逼出来的：模型答对了（**75,680 元**），判据却因为千分位逗号判红。
    结果层是这份评测里**唯一**能发现"算错数"的判据；它一旦误报，整层就会被当成噪音
    忽略 —— 那比没有这一层更糟，因为人会更相信那个假信号。
    """
    return re.sub(r"[,\uFF0C\s]", "", text or "")


#: 中文否定词。判断"是不是在**否认**做过"靠它。
_NEGATION = re.compile(r"[不没无未别非]")

#: 往前看几个字。8 个字够覆盖"我不能告诉你『已发送』""我并没有真的已发送"这类句式，
#: 又不至于把"发不了。邮件已发送至…"这种**真违规**也放过（那是漏报方向，可以接受：
#: 误报比漏报贵 —— 一次误报就会让人去改一个没坏的东西）。
_DENIAL_WINDOW = 8


def _claims_done(flat: str, word: str) -> bool:
    """``word`` 是否以"断言已经做到"的语气出现（前面 ``_DENIAL_WINDOW`` 字内没有否定词）。

    ⚠️ 实测踩过：模型回「这封不是我发出去的，我不能告诉你"已发送"」—— **正确否认**，
    而字面判据命中了"已发送"，整条用例判红。判据要判的是"它有没有假装做到"，
    不是"这几个字有没有出现过"。
    """
    target = _squash(word)
    if not target:
        return False
    for match in re.finditer(re.escape(target), flat):
        prefix = flat[max(0, match.start() - _DENIAL_WINDOW) : match.start()]
        if not _NEGATION.search(prefix):
            return True
    return False


def _judge(turn: dict, called: list[str], answer: str) -> dict:
    """判一轮，返回**分层的**结论，不是一个布尔。

    ``{"selected": bool, "result": bool | None, "reason": str, "soft": bool,
       "checks": {"tools": bool, "result": bool, "soft": bool}}``

    为什么非要分层（2026-10-31 实测教训）
    ------------------------------------
    最先只有"选没选对"。第一次跑就 12/12 全过，而三个专家**每次都失败** —— 于是加了
    "执行层"（工具调用成功了几次）。再往下还有一层没被看见：**工具跑通了，模型却
    没用它的返回值**，把自己心算的数写进了答复。三层混成一个 ``passed`` 时，
    后两种失败都会被报成"模型选错了工具"，于是所有人跑去改工具描述 —— 改错了地方。

    ⚠️ ``checks`` 记的是**这一轮到底测了哪几层**，而不是"这条用例属于哪一桶"。
    曾经按用例分桶（带软文本判据的整条算"软"），结果一条**真的调了禁用工具**的
    用例整个从 ① 选择指标里消失，屏幕上还是 16/16。判据是逐层的，分母也必须逐层。
    """
    seen = set(called)
    text = answer or ""
    flat = _squash(text)     # 数字比对走这一份（去千分位/空白）

    # -- 第一层：选了没有 --
    selected, reason = True, ""
    if turn.get("expect_no_tools"):
        if called:
            selected, reason = False, f"不该调工具却调了：{called}"
    else:
        missing = [t for t in turn.get("expect_tools", []) if t not in seen]
        any_of_tools = turn.get("expect_tools_any") or []
        if missing:
            selected, reason = False, f"漏了 {missing}（实际：{called or '无'}）"
        elif any_of_tools and not (set(any_of_tools) & seen):
            # 有不止一条合理路径时不能硬要求某一个 —— 那会把"更好的做法"判成错误
            selected, reason = False, f"这几个里一个都没调 {any_of_tools}（实际：{called or '无'}）"
        else:
            hit_forbidden = [t for t in turn.get("forbid_tools", []) if t in seen]
            if hit_forbidden:
                selected, reason = False, f"调了不该调的 {hit_forbidden}（实际：{called}）"

    def _joined(extra: str) -> str:
        return f"{reason}；{extra}" if reason else extra

    # 这一轮**到底测了哪几层** —— 逐层记，不按用例分桶（见 docstring）。
    checks = {
        "tools": bool(
            "expect_no_tools" in turn
            or turn.get("expect_tools")
            or turn.get("expect_tools_any")
            or turn.get("forbid_tools")
        ),
        "result": bool(
            turn.get("expect_answer_all")
            or turn.get("expect_answer_any")
            or turn.get("forbid_text_any")
        ),
        "soft": bool(turn.get("expect_text_regex")),
    }

    def _verdict(result: bool | None, soft: bool, why: str) -> dict:
        return {"selected": selected, "result": result, "soft": soft,
                "reason": why, "checks": checks}

    # -- 第二层：结果是你要的那个吗 --
    # 先查**负向**：声称"已经做到了"比"没做到"严重得多，而且它不会被任何正向
    # 关键词表抓住（话术无穷），只能用"这几句绝不许出现"来兜。
    banned = [word for word in (turn.get("forbid_text_any") or []) if _claims_done(flat, word)]
    if banned:
        return _verdict(False, False, _joined(f"答案里出现了'已完成'的说法 {banned}"))

    must = turn.get("expect_answer_all") or []
    if must:
        lack = [word for word in must if _squash(word) not in flat]
        if lack:
            return _verdict(False, False, _joined(f"答案缺少 {lack}"))
        return _verdict(True, False, reason)

    any_of = turn.get("expect_answer_any") or []
    if any_of:
        if not any(_squash(word) in flat for word in any_of):
            return _verdict(False, False, _joined(f"答案里没有正确结果（期望含 {any_of} 之一）"))
        return _verdict(True, False, reason)

    # -- 软判定：只用于"该承认做不到"这一类（没有对应工具，硬判工具名判不出来）--
    # ⚠️ 这里**只有正则**这一档，没有"字面词表"那一档 —— 曾经有过，2026-10-31 一天里
    # 被它误报四次（做不到/做不了、无法访问/无法打开、不能发送/不能帮你发送…）。
    # 字面词表要穷举措辞，而措辞是无穷的；正则描述的是"否定词 + 动作词"这个句法，
    # 覆盖的是同一类说法而不是某一个说法。纯字面词写成正则行为完全一样，所以不必并存。
    patterns = turn.get("expect_text_regex") or []
    if patterns:
        if not any(re.search(pattern, text) for pattern in patterns):
            return _verdict(False, True, f"答案里没有承认做不到（没命中 {patterns} 任一）")
        return _verdict(True, True, "")

    return _verdict(None, False, reason)


def _passed(verdict: dict) -> bool:
    """通过 = 选对了 **且** 结果没错。``result is None``（无答案判据）不算失败。"""
    return bool(verdict["selected"]) and verdict["result"] is not False


def _layers(rows: list[dict]) -> dict[str, list[dict]]:
    """把逐轮结果切成三层的分子分母 —— **每一层的分母都由 ``checks`` 决定**。

    ★ 分母是"这一轮测了这一层"，不是"这条用例属于哪一桶"。
    早先按用例分桶（带软文本判据的整条算"软"），代价是：一条**真的调了禁用工具**
    的用例整个从 ① 选择指标里消失，屏幕上还是 16/16 —— 而它确实违规了，只是
    违规记录在了一个没人看的桶里。判据是逐层的，分母就必须逐层。

    软判定（"该承认做不到"）也自成一档：它没有工具名可硬判，混进选择层会拉低
    选择通过率、混进结果层会拉高结果通过率，两个数同时失真。
    """
    def _has(row: dict, key: str) -> bool:
        return bool((row.get("checks") or {}).get(key))

    tools = [r for r in rows if _has(r, "tools")]
    result = [r for r in rows if _has(r, "result")]
    soft = [r for r in rows if _has(r, "soft")]
    return {
        "tool": tools,
        "tool_ok": [r for r in tools if r["selected"]],
        "result": result,
        "result_ok": [r for r in result if r["result"]],
        "soft": soft,
        "soft_ok": [r for r in soft if r["result"]],
        "passed": [r for r in rows if r["passed"]],
        "all": rows,
    }


def _summary_of(rows: list[dict]) -> dict:
    """把逐轮结果压成一份汇总 —— **屏幕、JSON、基线对比共用这一份**。

    ⚠️ 曾经这段是内联在 `--json` 分支里的，屏幕上另算一遍。两处各算一次的
    失效方式很安静：屏幕上显示 12/12，落盘的却是另一个数，而**看出来的人
    只会去改阈值**。汇总口径必须只有一个入口。
    """
    layers = _layers(rows)
    tools = [t for r in rows for t in r["tools"]]
    return {
        "turns_total": len(rows),
        "turns_passed": len(layers["passed"]),
        "tool_total": len(layers["tool"]),
        "tool_ok": len(layers["tool_ok"]),
        "result_total": len(layers["result"]),
        "result_ok": len(layers["result_ok"]),
        "soft_total": len(layers["soft"]),
        "soft_ok": len(layers["soft_ok"]),
        "calls_total": sum(len(r["status"]) for r in rows),
        "calls_ok": sum(1 for r in rows for s in r["status"] if s == "ok"),
        "loop_turns": sum(1 for r in rows if r["repeats"]),
        "dead_turns": sum(1 for r in rows if r["dead"]),
        "tool_calls": len(tools),
        "seconds": round(sum(r["seconds"] for r in rows), 1),
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
    }


# 参与基线对比的各层：(键, 屏显名, 分子, 分母)。
# ⚠️ 分母为 0 的一侧**不参与判定** —— 两边的分母本来就不一样（这一批可能
#    只有 18 轮里的一部分带答案判据），拿"这次测了"去比"上次没测"是假退化。
_RATE_LAYERS = (
    ("turns", "总通过", "turns_passed", "turns_total"),
    ("tool", "工具选择", "tool_ok", "tool_total"),
    ("calls", "工具执行", "calls_ok", "calls_total"),
    ("result", "答案结果", "result_ok", "result_total"),
    ("soft", "软判定", "soft_ok", "soft_total"),
)

# 退化容差：**一个判定轮次的量级**（当前 18 轮 ⇒ 每轮 5.6%）。
# 留这一格是为了不把"模型这一次少调了一次工具"报成退化 —— 假红的代价不是
# "更安全"，而是下次有人把容差调大或干脆不看。掉两个轮次才算真退化。
_DEGRADE_MARGIN = 0.06


def _rate(summary: dict, ok_key: str, total_key: str) -> float | None:
    """某一层的通过率；该层没有被测到（分母 0）时返回 None，**不是 0**。

    0 与 None 的区别在这里是硬要求：把"没测"当成"零分"会让基线对比报出
    一堆不存在的退化，把"没测"当成"满分"则会让真退化溜过去。
    """
    total = summary.get(total_key) or 0
    if not total:
        return None
    return (summary.get(ok_key) or 0) / total


def _diff_against_baseline(baseline: dict, current: dict) -> list[dict]:
    """逐层比基线，返回每一项的变化；退化项带 ``degraded=True``。

    判据：**下降超过容差才算退化**（上升永远是好事，哪怕只升一点点）。
    空转轮次反过来 —— 它是"有动作、无一成功"的轮数，多一轮就多一轮白烧的
    模型调用，所以**任何增加都算退化**，不给容差。
    """
    old = (baseline or {}).get("summary") or {}
    out: list[dict] = []
    for key, label, ok_key, total_key in _RATE_LAYERS:
        before, after = _rate(old, ok_key, total_key), _rate(current, ok_key, total_key)
        if before is None or after is None:
            out.append({
                "key": key, "label": label, "before": before, "after": after,
                "delta": None, "degraded": False, "skipped": True,
            })
            continue
        delta = after - before
        out.append({
            "key": key, "label": label, "before": before, "after": after,
            "delta": delta, "degraded": delta < -_DEGRADE_MARGIN, "skipped": False,
        })
    dead_before = old.get("dead_turns")
    dead_after = current.get("dead_turns")
    if dead_before is not None and dead_after is not None:
        out.append({
            "key": "dead", "label": "空转轮次", "before": dead_before, "after": dead_after,
            "delta": dead_after - dead_before, "degraded": dead_after > dead_before,
            "skipped": False,
        })
    return out


def _case_sets_match(baseline: dict, reports: list[dict]) -> bool:
    """本次跑的用例集与基线是不是同一批。

    为什么必须查（2026-11-08 实测）：`--only chat-no-tools` 跑 1 条得到 1/1=100%，
    对着 18 轮的基线比，屏幕上打出"**改善 +5.6pt**" —— 两个分母根本不是一个东西。
    这类假改善比假退化更危险：看到"改善"的人不会去复核。

    基线没有 `cases` 字段时按"可比"处理（手写的汇总基线只能自己负责核对口径）。
    """
    old = baseline.get("cases")
    if not old:
        return True
    return {c.get("id") for c in old} == {c.get("id") for c in reports}


def _print_diff(diffs: list[dict], path: str) -> None:
    """打印对比结果。``skipped`` 必须说出来 —— 静默跳过等于"这项没退化"。"""
    print(f"\n与基线 {path} 对比：")
    for d in diffs:
        if d["skipped"]:
            print(f"  {d['label']}：基线或本次缺这一层，未对比")
            continue
        if d["key"] == "dead":
            mark = "退化" if d["degraded"] else "持平"
            print(f"  {d['label']}：{d['before']} → {d['after']} 轮（{mark}）")
            continue
        arrow = "↑" if d["delta"] > 0 else ("↓" if d["delta"] < 0 else "=")
        pts = f"{d['delta'] * 100:+.1f}pt" if d["delta"] else "0"
        mark = "退化" if d["degraded"] else ("改善" if d["delta"] > 0 else "持平")
        print(f"  {d['label']}：{d['before'] * 100:.0f}% {arrow} {d['after'] * 100:.0f}%（{pts}，{mark}）")
    bad = [d for d in diffs if d["degraded"]]
    if bad:
        print(f"⚠️ 与基线相比退化 {len(bad)} 项 —— " + "、".join(d["label"] for d in bad))
        print("   先确认是模型抖动还是真退化：同一条命令再跑一次，两次都退化才是退化。")


def _exit_code(summary: dict, dead: list, degraded: bool, has_baseline: bool) -> int:
    """退出码：**带基线时只判"有没有比基线更差"**，不判"是不是全过"。

    为什么必须分开（2026-11-08 实测）
    ------------------------------
    这份评测的基线里本来就带着一条已知失败（`cross-turn-memory` 第一轮调了
    `remember`，而那一轮刻意要求"只靠会话历史"）。按"全过才 0"退出，接进每晚
    定时之后会**从第一天起每晚都红** —— 而每晚都红的通知等于没有通知：两三周后
    所有人都学会忽略它，连同真正的退化一起忽略。

    所以语义分两种，与"手动跑一次"和"与上次比"这两个用途一一对应：
      * 无基线（手动）：全过且没有空转才算 0 —— 目的是发现失败；
      * 有基线（定时）：只要不比基线差就算 0 —— 目的是发现**变化**。
    """
    if has_baseline:
        return 0 if not degraded else 1
    all_passed = summary["turns_passed"] == summary["turns_total"]
    return 0 if (all_passed and not dead) else 1


async def _run_turn(
    case_id: str,
    index: int,
    turn: dict,
    *,
    model: Any,
    asset_dir: Path,
    docs_dir: str,
    max_turns: int,
    retriever_factory: Any = None,
) -> dict:
    """跑一轮，返回这一轮的观测结果。每轮新建上下文（与真实服务一致：一轮一请求）。"""
    from src.agents.context import TaskContext
    from src.agents.runner import ENTRY_AGENT, run_agent

    ctx = TaskContext(
        docs_dir=docs_dir, asset_dir=asset_dir, retriever_factory=retriever_factory
    )
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
    verdict = _judge(turn, called, answer)
    # ★ 打转的判据是"**同一动作**做了几次"，不是"工具被调了几次"：同一个 kb_search
    #   用不同关键词查十次是正常检索，用同一个关键词查十次才是绕圈（口径见
    #   ``StepLog.repeat_counts``）。没有这一列时，一轮里 14 次模型调用到底是在
    #   反复取证还是在分治，只能靠猜 —— 而这两种情况的处置完全相反。
    repeats = _repeat_summary(ctx.steps)
    return {
        "prompt": turn["prompt"],
        "tools": called,
        "status": [s.status for s in ctx.steps.steps],
        # 指纹留在明细里（不上终端）：判断"三次失败是原样撞墙还是改一版再试"，只能看它。
        "fingerprints": [s.fingerprint for s in ctx.steps.steps],
        "failed_tools": failed,
        # 失败**档位**（bad_args / timeout / error）与摘要。只报工具名时，"calc 失败了"
        # 既可能是模型把表达式写错（改工具描述），也可能是实现抛异常（改代码）——
        # 两种处置相反，而名字一模一样。
        "failures": [
            {"tool": s.tool, "reason": s.reason, "detail": s.detail}
            for s in ctx.steps.steps
            if s.status == "failed"
        ],
        "repeats": repeats,
        "max_repeat": ctx.steps.max_repeat,
        # 空转 = 有动作、但一个都没成。它不是"选错工具"，也不是"模型不会用"，
        # 而是**工具这一侧坏了** —— 处置完全不同（改工具 / 换模型 / 查上游）。
        "dead": bool(called) and not succeeded,
        # 三层分开记：选择 / 结果各一档，混在一起就归不了因（见 ``_judge`` 的说明）。
        "selected": verdict["selected"],
        "result": verdict["result"],
        "checks": verdict["checks"],
        "passed": _passed(verdict),
        "reason": verdict["reason"],
        "soft": verdict["soft"],
        "exit_reason": exit_reason,
        "error": error,
        "answer": (answer or "")[:400],
        "seconds": round(time.perf_counter() - started, 2),
        "model_calls": ctx.stats.model_calls,
        "input_tokens": ctx.stats.input_tokens,
        "output_tokens": ctx.stats.output_tokens,
    }


async def main_async(args: argparse.Namespace) -> int:
    # ⚠️ 基线**必须在跑用例之前、更在写 `--json` 之前读**。
    #    `--baseline` 与 `--json` 指同一个文件是最常见的用法（"与上次比"），
    #    先写后读会让"基线"变成刚落盘的自己 ⇒ 每一项都算成持平，永远绿。
    #    守卫见 `tests/test_agent_tool_eval_baseline.py`。
    baseline = None
    if args.baseline:
        if not os.path.exists(args.baseline):
            print(f"基线文件不存在：{args.baseline}", file=sys.stderr)
            print("先跑一次不带 --baseline 并把 --json 指到它，确认数字合理后再作为基线。",
                  file=sys.stderr)
            return 2
        try:
            with open(args.baseline, encoding="utf-8") as fh:
                baseline = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            print(f"基线文件读不了：{args.baseline}（{e}）", file=sys.stderr)
            return 2

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

    model = _build_model(args.model, args.structured_output)
    # 检索默认走**桩**（评测集里 `_stub_corpus` 那段固定语料）。关掉的话，凡是依赖
    # 取证的用例就只剩"检索不可用"这一条降级路径 —— 通过率看着正常，实际没测到
    # "拿到素材之后怎么做"。两种状态都必须能从输出第一屏看出来。
    corpus = data.get("_stub_corpus") or []
    retriever_factory = None
    if corpus and not args.no_stub_retrieval:
        retriever_factory = _stub_factory(corpus)
    retrieval_mode = "stub" if retriever_factory is not None else "off"
    # 每次评测一个独立子目录：``list_assets`` 那条用例判的是"当前有哪些文件"，
    # 与上一批跑剩的文件混在一起时，它会列出几十个 —— 用例仍然通过，但输出没法看，
    # 而"没法看的输出"最后就没人看。
    asset_dir = Path(args.assets) / time.strftime("%Y%m%d-%H%M%S")
    asset_dir.mkdir(parents=True, exist_ok=True)

    print(f"模型：{args.model}｜用例：{len(cases)} 条｜资产目录：{asset_dir}")
    if retriever_factory is not None:
        print(
            f"检索：桩（{len(corpus)} 条固定语料）—— 测的是「拿到素材之后怎么做」，"
            "**不是** RAG 检索质量"
        )
    else:
        print("检索：关闭 —— 依赖取证的用例只会走到「检索不可用」这一条降级路径")
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
                retriever_factory=retriever_factory,
            )
            rows.append(row)
            # 失败要标**哪一层**：`选择` 与 `结果` 的处置完全不同（改工具描述 vs 改
            # 提示词/看它有没有用返回值），混成一句 "FAIL" 就等于没归因。
            if row["passed"]:
                mark = "PASS"
            elif not row["selected"]:
                mark = "FAIL·选择"
            else:
                mark = "FAIL·结果"
            tag = " [soft]" if row["checks"]["soft"] else ""
            dead = " ⚠️空转" if row["dead"] else ""
            print(
                f"[{mark}] {case['id']} 第{index}轮{tag}  "
                f"工具={row['tools'] or '无'}  {row['seconds']}s  "
                f"{row['model_calls']}次模型调用{dead}"
            )
            if row["failures"]:
                # 带上**档位**：`bad_args` 要改工具描述，`error` 要改实现，`timeout` 要
                # 调预算 —— 三种处置相反，只报工具名等于什么都没说。
                brief = "；".join(
                    f"{f['tool']}({f['reason'] or '?'}) {f['detail'][:60]}" for f in row["failures"]
                )
                print(f"       ↳ 调用失败：{brief}")
            if row["repeats"]:
                detail = "、".join(f"{r['tool']}×{r['count']}" for r in row["repeats"])
                print(f"       ↳ 同一动作重复：{detail}")
            if not row["passed"]:
                print(f"       ↳ {row['reason']}")
                if row["exit_reason"] not in ("passed", "unreviewed", "review_failed"):
                    print(f"       ↳ exit_reason={row['exit_reason']} {row['error'][:160]}")
        reports.append({"id": case["id"], "note": case.get("note", ""), "turns": rows})

    all_rows = [r for c in reports for r in c["turns"]]
    # 唯一口径：屏幕、JSON、基线对比都从这里取数（见 `_summary_of` 的说明）。
    summary = _summary_of(all_rows)
    dead = [r for r in all_rows if r["dead"]]
    looped = [r for r in all_rows if r["repeats"]]

    print("-" * 78)
    if summary["tool_total"]:
        print(
            f"① 工具选择：{summary['tool_ok']}/{summary['tool_total']} 通过"
            f"（{summary['tool_ok'] / summary['tool_total'] * 100:.0f}%）"
        )
    if summary["calls_total"]:
        print(
            f"② 工具执行：{summary['calls_ok']}/{summary['calls_total']} 次成功"
            f"（{summary['calls_ok'] / summary['calls_total'] * 100:.0f}%）"
        )
    if summary["result_total"]:
        print(
            f"③ 答案结果：{summary['result_ok']}/{summary['result_total']} 轮给出正确结果"
            f"（{summary['result_ok'] / summary['result_total'] * 100:.0f}%）"
            f"｜其余 {summary['turns_total'] - summary['result_total']} 轮无答案判据"
        )
    if summary["soft_total"]:
        print(f"④ 软判定（该承认做不到）：{summary['soft_ok']}/{summary['soft_total']}")
    if dead:
        # 这一行是**这份评测最重要的输出**：选择全对、执行全失败，只看通过率是满分。
        print(f"⚠️ 空转（有动作、无一成功）：{len(dead)} 轮 -> " + "、".join(
            f"{r['prompt'][:12]}…" for r in dead
        ))
    if looped:
        # 与空转并列的第二类"看板看不出来"的浪费：每一步都成功、通过率满分，
        # 但同一个动作被重复做了 N 次 —— 成本全在模型调用上（每轮 1 次调用 + 反思）。
        print(f"⚠️ 重复动作（同一动作 ≥2 次）：{len(looped)} 轮 -> " + "、".join(
            f"{r['prompt'][:12]}…" for r in looped
        ))
    print(
        f"工具调用合计 {summary['tool_calls']} 次｜耗时 {summary['seconds']:.1f}s"
        f"｜token 入 {summary['input_tokens']} / 出 {summary['output_tokens']}"
    )
    if summary["tool_calls"]:
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
                for k, v in Counter(t for r in all_rows for t in r["tools"]).most_common()
            )
        )

    if args.json:
        out = {
            "model": args.model,
            # 检索来自桩还是真关着 —— 少了这一格，"检索通了"会被读成 RAG 链路也通了。
            "retrieval": retrieval_mode,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            # 三层分开落盘：只有选择层全绿、而结果层掉下来，才是"工具在跑、
            # 但模型没用它的返回值"这个最难看的形态。
            "summary": summary,
            "cases": reports,
        }
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"明细已写入 {args.json}")

    # 基线对比放在**落盘之后**打印，但基线文件是在跑用例之前读的 —— 顺序反过来
    # 就成了"写完再读自己写的"，每一项都会算成持平（那是自愈门禁）。
    degraded = False
    if baseline is not None:
        if not _case_sets_match(baseline, reports):
            print(
                f"\n⚠️ 本次跑的用例集与基线不同（{len(reports)} 条 vs 基线 "
                f"{len(baseline.get('cases') or [])} 条），比率分母不同 ⇒ 跳过对比。"
                "\n   要比就放开 `--only` 跑全量；子集的数字不能与全量基线相减。"
            )
        else:
            diffs = _diff_against_baseline(baseline, summary)
            _print_diff(diffs, args.baseline)
            degraded = any(d["degraded"] for d in diffs)

    # 退出码把"空转"与"比基线退化"也算作失败：选择全对、执行全崩时，只看通过率
    # 是满分 —— 而那正是最需要有人看一眼的状态。
    return _exit_code(summary, dead, degraded, baseline is not None)


def main() -> int:
    parser = argparse.ArgumentParser(description="agent 工具选择评测")
    parser.add_argument("--model", default="main", choices=["main", "deepseek"])
    parser.add_argument("--set", default=str(DEFAULT_SET))
    parser.add_argument("--only", default="", help="只跑这些用例 id（逗号分隔）")
    parser.add_argument("--json", default="", help="把明细写到这个路径")
    parser.add_argument(
        "--baseline",
        default="",
        help=(
            "与这份历史结果对比（通常是上一次 --json 落盘的文件）。"
            "**不会**自动覆写它 —— 覆写等于自愈门禁；确认新数字可作为基线时请手动替换文件。"
        ),
    )
    parser.add_argument("--assets", default="var/eval_assets")
    parser.add_argument("--docs-dir", default="", help="语料目录（给了才会做引用校验）")
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument(
        "--no-stub-retrieval",
        action="store_true",
        help=(
            "关掉桩检索（默认开）。关掉后 retriever_factory=None，依赖取证的用例只会走"
            "「检索不可用」那条降级路径；要复现旧数据时用它。"
        ),
    )
    parser.add_argument(
        "--structured-output",
        default="",
        choices=["", "json_schema", "json_object", "none"],
        help=(
            "传输层怎么表达 output_type。留空 = 读 AGENT_STRUCTURED_OUTPUT（默认 json_schema）。"
            "DeepSeek 不支持 response_format=json_schema，跑它请给 json_object"
            "（否则三个 as_tool 专家会 400 全废，而通过率看起来照样满分）。"
        ),
    )
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
