"""普通 function tools —— **确定性能力不做成 agent**。

判据（官方 ``research_bot`` 就是这么写的）：**需要模型"拿主意"的才配 agent，
其余都是函数。**

* ``kb_search``：模型要决定"查什么、查几次" ⇒ 它是**工具**（不是「检索 agent」）。
  真正的检索动作是确定性的，工具就够了；让它变成 agent 只会多一跳。
* ``render_deck``：没有任何模型需要"决定要不要渲染" ⇒ 它是**普通函数**，
  连工具都不该暴露（暴露了只是给模型多一个可以忘掉的选项）。

与示例版的差别（生产化必须的）
------------------------------
1. **检索接真实知识库**：走 ``TaskContext.retriever_factory``（按租户懒加载的检索器），
   而不是内置的小词表。检索失败 / 空召回 / 耗时全部上报（见 `src/observability.py`）。
2. **注入防护复用 RAG 链路的 ``guard``**：工具返回值直接进模型上下文，是注入的
   真实入口。这里用与 ``graph.generate`` **同一套** ``wrap_context``，两条链路
   的防护口径一致。
3. **引用编号全局唯一**：多次检索的编号要连续累加，否则出口护栏（引用可追溯）
   会把第二条检索的第 [1] 条误判成"没检索过"。
"""

from __future__ import annotations

import ast
import functools
import inspect
import json
import logging
import math
import operator
import re
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agents import RunContextWrapper, function_tool

from .. import observability
from ..envutil import env_int
from ..rag import guard
from ..rag.retrieval import build_citations
from . import approval
from .context import TaskContext
from .steps import ToolContractError, tool_error_handler
from .toolpool import run_tool_step

__all__ = [
    "calculate",
    "deliver",
    "generate_image",
    "get_current_time",
    "kb_search",
    "list_assets",
    "read_asset",
    "render_deck",
    "render_slide_deck",
    "save_text_asset",
]

DEFAULT_TOP_K = env_int("AGENT_KB_TOP_K", 3, lo=1)
"""单次检索返回的片段数上限。与 ``graph.TOP_K`` 同量级：agent 链路可以多查几轮，
单轮不必更大。"""

_SLUG_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff_-]+")


def _safe_name(name: str, fallback: str) -> str:
    """把任意字符串变成一个安全的文件名主干（防目录穿越 / 非法字符）。"""
    cleaned = _SLUG_RE.sub("_", (name or "").strip())[:60].strip("_")
    return cleaned or fallback


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


_CONTEXT_PARAMS = frozenset({"ctx", "context"})
"""上下文注入参数的名字 —— 只用于**文案**（不把它们列给模型看）。

⚠️ 判"多给了什么字段"时**不需要**排除它们：那里的判据是
``给的键 - 形参名``，而模型不会给 ``ctx``（schema 里没有它）。把 ``ctx`` 留在
已声明集合里只有好处 —— 万一哪天模型真给了，也不会被误判成多余字段。
"""


def _declared_names(fn: Callable[..., Any]) -> frozenset[str]:
    """工具的形参名集合（取不到签名时返回空集 ⇒ 调用方跳过检查）。

    为什么取不到就跳过：宁可漏抓一次多余字段，也不能因为"这个函数对象形状不认识"
    把一个正常的工具调用拒掉 —— 那个方向的错误会让工具整体不可用。
    """
    try:
        return frozenset(inspect.signature(fn).parameters)
    except (TypeError, ValueError):  # pragma: no cover - 内建函数 / C 扩展
        return frozenset()


def _extra_args(tool: str, fn: Callable[..., Any], ctx: Any) -> frozenset[str]:
    """模型在参数里多给了哪些**工具并不认识**的字段。

    为什么必须在包装层查、函数体里查不到
    ----------------------------------
    官方用 ``params_pydantic_model(**json_data)`` 解析参数，而那个模型**没有**
    ``extra="forbid"`` ⇒ 多给的字段被**静默丢弃**。函数体拿到的 kwargs 里压根没有
    它们，所以"模型自己编了个 ``filters``"这件事在函数体里是不可见的：
    它以为加了过滤条件，实际那一刻条件已经没了，工具照常返回一大段结果，
    没有任何报错，模型也不知道自己漏了什么。

    唯一能拿到原始参数的地方是 ``ToolContext.tool_arguments``（SDK 里那个字段的
    注释就是 "The raw arguments string of the tool call"），而工具的第一个参数正好
    收到的就是它 —— 所以这一层看得到、下面看不到。

    三种情况一律**不判**（返回空集）：拿不到原始串（非 ``ToolContext`` 形态）、
    不是合法 JSON（官方自己会报 JSON 错，这里不抢答）、解析出来不是对象。
    """
    raw = getattr(ctx, "tool_arguments", "") or ""
    if not isinstance(raw, str) or not raw.strip():
        return frozenset()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return frozenset()
    if not isinstance(data, dict):
        return frozenset()
    declared = _declared_names(fn)
    if not declared:
        return frozenset()
    return frozenset(str(key) for key in data if key not in declared)


def _reject_extra_args(tool: str, fn: Callable[..., Any], ctx: Any) -> None:
    """多给了字段 ⇒ 抛 ``ToolContractError``（退回模型重试，且步骤清单定格为失败）。"""
    extra = _extra_args(tool, fn, ctx)
    if not extra:
        return
    observable = _declared_names(fn) - _CONTEXT_PARAMS
    observability.counter("agent_tool_unknown_args_total", tool=tool)
    logging.info(
        "工具 %s 收到 %d 个不认识的参数（%s）⇒ 退回模型重试",
        tool, len(extra), ", ".join(sorted(extra)),
    )
    raise ToolContractError(
        f"工具 {tool} 不认识这些参数：{'、'.join(sorted(extra))}。"
        f"它只接受：{'、'.join(sorted(observable)) or '（无）'}。"
        "**多给的参数会被静默丢掉**，所以你以为加上的条件其实没有生效 —— "
        "请只按工具定义里的参数名填写；有额外条件就并进已有参数（例如把筛选条件写进 query），"
        "然后重新调用。"
    )


def _isolated(tool: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """把一个**同步**工具实现包成 async：进独立线程池跑，带单步超时。

    为什么用装饰器而不是把每个函数体改写成 ``async def`` + ``await``
    --------------------------------------------------------------
    函数体本身没有任何 IO 可等待（检索器与落盘都是同步 API）。改写只会把
    "同步实现"和"线程切换"两件事揉进同一个缩进层级里，让每个 ``return`` 都要多
    想一层。这里让实现保持同步、**执行方式**交给这一层，两件事各自可读。

    为什么必须包（而不是让官方用 ``to_thread``）
    -------------------------------------------
    官方对同步工具用 ``asyncio.to_thread`` ⇒ 事件循环的**默认线程池**，与服务层
    ``_off()`` 共用。一个卡住的工具会把限流/落库/结算的线程一起占掉（见
    ``toolpool`` 的模块说明）。包成 async 之后官方直接 await 本函数，线程由
    ``toolpool`` 的独立池提供。

    ``functools.wraps`` 是必需的：``function_tool`` 靠 ``inspect.signature`` 生成
    给模型看的参数 schema，包装层丢了签名就等于"工具没有参数"。

    另外在这里做**参数契约检查**（2026-10-22）：模型多给的字段会被官方静默丢弃，
    而那件事在函数体里不可见（见 ``_extra_args``）。检查放在进线程池之前 ——
    它不需要 IO，也不该占一个线程。
    """

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def inner(*args: Any, **kwargs: Any) -> Any:
            _reject_extra_args(tool, fn, args[0] if args else None)
            return await run_tool_step(fn, *args, tool=tool, **kwargs)

        return inner

    return wrap


# --------------------------------------------------------------------------- #
# 确定性小工具：时间 / 算术 / 资产回读（2026-10-30 补）
#
# 为什么这四个是**工具**而不是"让模型自己算/自己记"
# ------------------------------------------------
# 判据仍然是本模块开头那条：需要模型"拿主意"的才配 agent，其余都是函数 ——
# 而"确定性的、有唯一正确答案的"动作，连让模型自由发挥都不该有：
#
# * ``get_current_time``：模型的训练数据有截止日期，**它不知道今天几号**。
#   知识库问答里"最近三个月""今年新规"这类问题全要落在具体日期上，让它猜就是
#   编一个看起来很合理的日子 —— 而且答案里没有任何线索能看出那是猜的。
# * ``calculate``：算术是模型最容易"算得像"的能力（过程对、结果错），而错了没人
#   复核。这里用 AST 白名单求值，不用 ``eval``：``eval`` 的输入是**模型生成**的
#   字符串，等于把代码执行权交给一段不可信文本。
# * ``read_asset`` / ``list_assets``：``save_text_asset`` 一直是**单向**的 ——
#   写得出去、读不回来。于是多轮对话里"把上一步的稿子改一下"只能靠模型记住全文，
#   而会话一旦被 ``history_budget`` 裁过，它就真的没有了。写入端与读取端必须成对，
#   否则"落盘"只是一次丢弃。
#
# ⚠️ 刻意**没有**做 ``fetch_url``：抓任意 URL 会同时带进 SSRF（内网地址、云元数据
# 端点）与间接注入（抓回来的正文直接进模型上下文）两个问题，而本项目已有的
# ``guard.wrap_context`` 只处理后者。要做就得先定域名白名单与出网策略 ——
# 那是独立一件事，不该混在"补常见工具"里悄悄上线。
# --------------------------------------------------------------------------- #
def _asset_base(ctx: Any) -> Path:
    """取落盘根目录。拿不到上下文时与既有工具用同一个兜底值。"""
    context = getattr(ctx, "context", None)
    base = context.asset_dir if isinstance(context, TaskContext) else Path("var/agent_assets")
    return Path(base)


@function_tool(failure_error_function=tool_error_handler("get_current_time"))
@_isolated("get_current_time")
def get_current_time(ctx: RunContextWrapper[TaskContext], timezone: str = "Asia/Shanghai") -> str:
    """返回**现在**的日期与时间（含星期与时区）。

    凡是要落到具体日期的地方都用它，不要凭记忆写"今天"是哪天：
    你的知识有截止日期，而用户问的是当下。

    Args:
        timezone: IANA 时区名，默认 ``Asia/Shanghai``（东八区）。
    """
    try:
        zone = ZoneInfo(timezone or "Asia/Shanghai")
    except (ZoneInfoNotFoundError, ValueError, TypeError) as e:
        raise ToolContractError(
            f"不认识的时区名：{timezone!r}。请用 IANA 名称（例如 Asia/Shanghai、"
            "UTC、America/New_York），不要写「北京时间」「东八区」这类说法。"
        ) from e
    now = datetime.now(zone)
    weekday = "一二三四五六日"[now.weekday()]
    return now.strftime(f"%Y-%m-%d %H:%M:%S（{zone.key}，星期{weekday}）")


#: 允许出现的运算符（**白名单**，不是黑名单）。
#:
#: 黑名单在有新语法时必然漏（例如 ``ast.MatMult``、海象表达式），而这里漏一个
#: 就等于把一整类表达式交给未知实现。白名单漏了只会"这个表达式算不了"，方向安全。
_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "floor": math.floor,
    "ceil": math.ceil,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
}
_CONSTS = {"pi": math.pi, "e": math.e}

_MAX_EXPR_CHARS = 300
"""表达式长度上界。防的是"把一整段文字当表达式喂进来"那类误用，不是安全边界。"""

_MAX_POW_EXPONENT = 64
"""指数的绝对值上界。``9**9**9`` 是合法的 Python 表达式，但它是**算不完**的：
求值会占满一个线程，而工具线程池是共享的（见 ``toolpool``）—— 一个表达式就能让
其它工具调用排队到超时。"""

_MAX_RESULT_DIGITS = 40
"""结果规模的第二条闸门（第一条是上面的指数上界）。"""


def _eval_node(node: ast.AST) -> Any:
    """按白名单递归求值。非白名单节点一律抛 ``ToolContractError``（不是 ``ValueError``）。"""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        # ``bool`` 是 ``int`` 的子类，``True + 1`` 会被当成 2 —— 而模型写 "True"
        # 时想要的是逻辑，不是数字。静默接受它只会让一个错误答案看起来正常。
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ToolContractError(
                f"calculate 只支持数字字面量，不支持 {node.value!r}。"
            )
        return node.value
    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise ToolContractError(
                f"calculate 不支持这个运算符：{type(node.op).__name__}。"
                "支持 + - * / // % ** 与括号。"
            )
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_POW_EXPONENT:
            raise ToolContractError(
                f"指数 {right} 超过上界 {_MAX_POW_EXPONENT}：这个幂算不完，已拒绝。"
                "请改用对数或直接给近似值。"
            )
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        op = _UNARYOPS.get(type(node.op))
        if op is None:
            raise ToolContractError(
                f"calculate 不支持这个一元运算符：{type(node.op).__name__}。"
            )
        return op(_eval_node(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            # 用 ``ast.unparse`` 回显被拒的那段调用形态：模型写的是
            # ``(1).bit_length()`` 这种属性调用时，只报"不支持"它不知道自己错在哪，
            # 换个写法会继续撞同一堵墙。
            shown = ast.unparse(node.func) if hasattr(ast, "unparse") else "?"
            raise ToolContractError(
                f"calculate 不支持这个函数：{shown}。"
                f"可用的：{'、'.join(sorted(_FUNCS))}。"
                "也只能调用它们，不能调用对象上的方法。"
            )
        if node.keywords:
            raise ToolContractError("calculate 的函数不接受关键字参数，请按位置传。")
        return _FUNCS[node.func.id](*[_eval_node(a) for a in node.args])
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise ToolContractError(
            f"calculate 不认识名字 {node.id!r}。常量只有 pi 和 e；"
            "要算的东西必须是字面量或上面列出的函数。"
        )
    raise ToolContractError(
        f"calculate 不支持这种写法（{type(node).__name__}）。"
        "请只写算术表达式，例如 (1200-350)*0.8/sqrt(2)。"
    )


def _format_number(value: Any) -> str:
    """把结果写成**给人看的**形式，并把"太大 / 不是有限数"拦在这里。"""
    if isinstance(value, complex):
        raise ToolContractError(
            "计算结果落到了复数范围（例如对负数开方）。请确认输入是否符合实际含义。"
        )
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ToolContractError(
                "计算结果不是有限数（溢出或除以零）。请检查数量级后重新计算。"
            )
        if value.is_integer() and abs(value) < 1e16:
            return str(int(value))
        return f"{value:.10g}"
    if isinstance(value, int):
        if len(str(abs(value))) > _MAX_RESULT_DIGITS:
            raise ToolContractError(
                f"结果有 {len(str(abs(value)))} 位，超过上界 {_MAX_RESULT_DIGITS} 位，已拒绝。"
            )
        return str(value)
    raise ToolContractError(f"计算结果类型不支持：{type(value).__name__}。")


@function_tool(failure_error_function=tool_error_handler("calculate"))
@_isolated("calculate")
def calculate(ctx: RunContextWrapper[TaskContext], expression: str) -> str:
    """计算一个算术表达式，返回精确结果。

    凡是要给出的数字结论（合计、占比、增长率、换算）都用它算一遍，
    不要心算 —— 心算错了从结果上看不出来。

    Args:
        expression: 算术表达式，例如 ``(1200-350)*0.8``。
            支持 ``+ - * / // % **``、括号，以及 abs/round/min/max/sqrt/log/log10/exp/
            floor/ceil/sin/cos/tan 与常量 pi、e。
    """
    text = (expression or "").strip()
    if not text:
        raise ToolContractError("calculate 的 expression 是空的：没有表达式可算，已拒绝。")
    if len(text) > _MAX_EXPR_CHARS:
        raise ToolContractError(
            f"表达式有 {len(text)} 字符，超过上界 {_MAX_EXPR_CHARS}："
            "这看起来不是一道算式。请只传算式本身。"
        )
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as e:
        raise ToolContractError(
            f"这不是一个合法的算术表达式：{e.msg}。请检查括号与运算符。"
        ) from e
    try:
        value = _eval_node(tree)
    except ZeroDivisionError as e:
        raise ToolContractError("除数是零：这道算式没有结果，请检查输入。") from e
    except (OverflowError, ValueError) as e:
        raise ToolContractError(f"计算失败：{e}。请检查输入的数量级或取值范围。") from e
    return f"{text} = {_format_number(value)}"


@function_tool(failure_error_function=tool_error_handler("list_assets"))
@_isolated("list_assets")
def list_assets(ctx: RunContextWrapper[TaskContext]) -> str:
    """列出本次任务已经落盘的文件（名称、大小、修改时间）。

    想确认"手上到底有哪些文件"时用它 —— 不要凭记忆猜文件名：
    文件名是由 ``save_text_asset`` 的参数规范化后生成的，与你写的不一定一致。
    """
    base = _asset_base(ctx)
    if not base.is_dir():
        return "本次任务还没有落盘任何文件。"
    entries = sorted(
        (p for p in base.iterdir() if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not entries:
        return "本次任务还没有落盘任何文件。"
    lines = [f"共 {len(entries)} 个文件（按修改时间倒序，最多列 50 个）："]
    for path in entries[:50]:
        stat = path.stat()
        stamp = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"- {path.name}（{stat.st_size} 字节，{stamp}）")
    if len(entries) > 50:
        lines.append(f"…其余 {len(entries) - 50} 个未列出。")
    return "\n".join(lines)


@function_tool(failure_error_function=tool_error_handler("read_asset"))
@_isolated("read_asset")
def read_asset(ctx: RunContextWrapper[TaskContext], name: str) -> str:
    """读回本次任务已经落盘的文件正文。

    改稿、续写、核对上一步写了什么时用它，而不是靠对话历史回忆 ——
    历史可能已经被裁剪过，而文件是一直在的。

    Args:
        name: 文件名（``list_assets`` 返回的那个名字，含扩展名）。
    """
    wanted = (name or "").strip()
    if not wanted:
        raise ToolContractError(
            "read_asset 的 name 是空的：不知道要读哪个文件，已拒绝。"
            "先调用 list_assets 看有哪些文件。"
        )
    base = _asset_base(ctx).resolve()
    target = (base / wanted).resolve()
    # ⚠️ 必须先 resolve 再比前缀：``../secret.txt`` 这种写法在字符串层面看不出来，
    # 只有解析成真实路径之后才知道它有没有跑出目录。这一步是**唯一**的边界检查，
    # 不要因为"name 来自模型、看起来不像路径"就省掉 —— 模型完全可能写出 ../。
    if not target.is_relative_to(base):
        raise ToolContractError(
            f"read_asset 只能读本次任务的落盘目录，{wanted!r} 指到了目录外面（已拒绝）。"
            "请只传 list_assets 返回的文件名。"
        )
    if not target.is_file():
        available = sorted(p.name for p in base.iterdir() if p.is_file())[:20] if base.is_dir() else []
        hint = "、".join(available) if available else "（目录里还没有文件）"
        return f"没有这个文件：{wanted}。当前可读的：{hint}。"
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"{wanted} 不是 UTF-8 文本（可能是图片或二进制文件），无法当正文读取。"
    except OSError as e:
        return f"读取 {wanted} 失败：{e}"


# --------------------------------------------------------------------------- #
# 检索：真实知识库（按租户分片的检索器由上下文注入）
# --------------------------------------------------------------------------- #
@function_tool(failure_error_function=tool_error_handler("kb_search"))
@_isolated("kb_search")
def kb_search(ctx: RunContextWrapper[TaskContext], query: str, top_k: int = DEFAULT_TOP_K) -> str:
    """在知识库里检索与 query 相关的原文片段，返回带出处编号（形如 [1]）的条目。

    这是**唯一**获取事实的途径：凡是要写进结论的数字、配置项、接口路径，
    都必须来自本工具的返回；检索不到就如实说检索不到。

    Args:
        query: 检索问题。关键词或自然语言都可以。
        top_k: 最多返回几条片段。
    """
    context = ctx.context
    if not isinstance(context, TaskContext) or context.retriever_factory is None:
        observability.counter("rag_hits_total", result="unavailable", via="agent")
        return "检索不可用：本实例未配置知识库（DOCS_DIR 为空或检索器装配失败）。"

    limit = max(1, min(int(top_k or DEFAULT_TOP_K), 10))
    context.stats.retrieval_calls += 1
    started = time.perf_counter()
    try:
        hits = context.retriever_factory().search(query, top_k=limit)
    except Exception as e:  # noqa: BLE001  检索失败不该把整条 agent 链路带崩
        observability.counter("rag_hits_total", result="error", via="agent")
        observability.counter("rag_retrieval_degraded_total", stage="agent_search")
        logging.warning("agent 链路检索失败，按检索不到处理：%s", e)
        return "检索失败（知识库暂时不可用），无法提供带出处的结论。"
    finally:
        observability.histogram(
            "rag_retrieval_duration_seconds", time.perf_counter() - started, via="agent"
        )

    if not hits:
        observability.counter("rag_hits_total", result="empty", via="agent")
        context.stats.empty_retrievals += 1
        logging.info("agent 链路检索空召回（query=%.60s）", query)
        return "检索不到与这个问题相关的片段。"

    observability.counter("rag_hits_total", result="hit", via="agent")
    context.stats.retrieved_chunks += len(hits)

    fresh = build_citations(hits)
    # 契约检查：**「检索到了」不等于「拿到了能用的东西」**。索引损坏 / 解析退化时，
    # 片段会带着出处却没有正文 —— 那种结果拼进上下文就是一堆空条目，而模型会把它
    # 当成"查过了、没有结论"继续往下写，最终交出一份没有依据的答复。
    # 按"检索失败"处理比按"空召回"更准：空召回是知识库里真没有，这是有但没有内容。
    usable = [c for c in fresh if str(c.get("snippet") or "").strip()]
    if not usable:
        observability.counter("rag_hits_total", result="unparsable", via="agent")
        observability.counter("rag_retrieval_degraded_total", stage="agent_empty_snippet")
        logging.warning(
            "agent 链路检索到 %d 条片段但正文全为空（索引可能损坏），按检索失败处理", len(hits)
        )
        return "检索到的片段没有正文（索引可能损坏），无法提供带出处的结论。"

    # 引用编号**全局唯一**：多次检索时按已有条数偏移。不偏移的话，每轮检索都从
    # [1] 重新编号，出口护栏会把第二次检索的 [1] 当成"重复编号"或漏判。
    offset = len(context.citations)
    for i, cite in enumerate(usable, start=1):
        cite["n"] = offset + i
    context.citations.extend(usable)

    body = "\n\n".join(
        f"[{offset + i}] {c.get('source', '?')}"
        + (f"（第 {c['page']} 页）" if c.get("page") else "")
        + f"\n{c.get('snippet', '')}"
        for i, c in enumerate(usable, start=1)
    )
    # 注入防护：工具返回的文本会**直接进模型上下文**，这是外部内容进入模型的真实
    # 入口。用与 graph.generate 同一套 wrap_context（结构边界 + 按策略处理），
    # 保证两条链路的防护口径一致 —— 只在其中一条上防护等于没防护。
    return guard.wrap_context(body)


# --------------------------------------------------------------------------- #
# 落盘：确定性步骤
#
# ⚠️ 两个落盘工具都挂了 ``needs_approval``（加固阶段三）—— 它们**撤销不了**
# （退回白名单里没有它们：删掉对话里那一步，磁盘上的文件不会消失），而且现有的人工
# 审核审的是**最终草稿**，等审到时文件早落盘了。所以事前必须有人点头。
#
# 回调读的是总开关 ``AGENT_TOOL_APPROVAL``（**默认关**）：关着时这两个工具的行为与
# 改造前逐字节一致。开与关的差别、以及"停下来"之后要配套什么，见 ``approval`` 模块。
# --------------------------------------------------------------------------- #
@function_tool(
    failure_error_function=tool_error_handler("save_text_asset"),
    needs_approval=approval.needs_approval_callback,
)
@_isolated("save_text_asset")
def save_text_asset(ctx: RunContextWrapper[TaskContext], name: str, content: str) -> str:
    """把一段文本落盘成资产文件，返回资产路径。

    ⚠️ 这是**会留下东西**的动作：文件一旦写出就撤不回（它不在退回白名单里）。
    所以开启审批时，调用本工具前会先停下来请示用户。

    Args:
        name: 文件名主干（不含扩展名）。
        content: 文本正文。
    """
    context = ctx.context
    # 契约检查：空内容会写出去一个**空文件**，而返回值仍然是"已保存"的路径 ——
    # 失败要到用户打开文件时才发现。抛出去才是当场定格成失败（见 ``ToolContractError``）。
    if not (content or "").strip():
        raise ToolContractError(
            "save_text_asset 的 content 是空的：这只会写出一个空文件，已拒绝落盘。"
            "请把要保存的正文整理好放进去再调一次；如果这一步本来就没有内容要写，"
            "就不要调用本工具。"
        )
    base = context.asset_dir if isinstance(context, TaskContext) else Path("var/agent_assets")
    path = _ensure_dir(Path(base)) / f"{_safe_name(name, 'asset')}.txt"
    # newline=""：与项目"源码/落盘一律 LF"的纪律一致；Windows 上默认会把 \n 换成 \r\n。
    path.write_text(content, encoding="utf-8", newline="")
    if isinstance(context, TaskContext):
        context.assets.append(str(path))
        context.stats.assets += 1
    observability.counter("agent_assets_total", kind="text")
    return str(path)


@function_tool(
    failure_error_function=tool_error_handler("generate_image"),
    needs_approval=approval.needs_approval_callback,
)
@_isolated("generate_image")
def generate_image(ctx: RunContextWrapper[TaskContext], prompt: str, style: str = "flat") -> str:
    """按画面描述生成一张插图，返回资产路径。

    ⚠️ 这也是**会留下东西**的动作（同 ``save_text_asset``）：生成物落盘就撤不回，
    开启审批时会先停下来请示用户。

    ⚠️ 当前是**占位实现**：只把生成参数写成一份 sidecar 文件（``*.prompt.txt``），
    不调用任何外部图像服务。接入真实图像生成能力时，替换本函数体即可 ——
    契约（入参 prompt/style，出参资产路径，并登记到 ``TaskContext.assets``）不变。
    真实服务一旦接入，"停下来等人点头"会更有价值（生成要花钱、且可能不合规）。

    Args:
        prompt: 画面描述（主体 / 构图 / 光线）。
        style: 风格关键词。
    """
    context = ctx.context
    # 同 ``save_text_asset``：空描述生成出来的东西必然没人能用，而返回值仍是"已生成"。
    if not (prompt or "").strip():
        raise ToolContractError(
            "generate_image 的 prompt 是空的：没有画面描述就生成不出可用的图，已拒绝。"
            "请把主体 / 构图 / 光线写清楚再调一次。"
        )
    base = context.asset_dir if isinstance(context, TaskContext) else Path("var/agent_assets")
    path = _ensure_dir(Path(base)) / f"{_safe_name(prompt, 'image')}.prompt.txt"
    path.write_text(
        f"[placeholder image]\nprompt: {prompt}\nstyle: {style}\n",
        encoding="utf-8",
        newline="",
    )
    if isinstance(context, TaskContext):
        context.assets.append(str(path))
        context.stats.assets += 1
    observability.counter("agent_assets_total", kind="image_placeholder")
    return str(path)


# --------------------------------------------------------------------------- #
# 交付：这一次要发给用户的正文（顺带承载"要不要人先过目"这个声明）
#
# 为什么交付要变成一个工具调用，而不是"最终输出就是交付物"（2026-10-20）
# --------------------------------------------------------------------
# 交付前的人审原本挂在图里一个独立节点上（``graph.human_review`` + ``interrupt``）。
# 2026-10-20 把"停下来"搬到官方 ``RunState``（现场存 ``agent_run_state``），
# 那两代实现都随图删掉了。
# 而"按内容决定这一次要不要人看"这件事，官方机制里只有**工具审批**能做
# （模型请求一个需要批准的工具 ⇒ 运行就地停下）。
#
# 于是交付本身必须是一次工具调用：只有"正在交付的那个人"知道这次交的是什么、
# 该不该让人先看。判定时刻也就从"运行结束后的判定表"前移到"交付那一刻"——
# 这两个时刻原本对不上，是这道闸迁过来时唯一真正的约束（见 ``approval`` 模块
# ``delivery_needs_approval`` 的说明）。
#
# ⚠️ 调用它不是强制的：模型不调，``runner`` 照旧用 ``final_output`` 作为答复，
# 行为与改造前逐字节一致。所以"模型忘了调"的后果是**少了一道自选的审核**，
# 不是"用户收不到东西"。
# --------------------------------------------------------------------------- #
@function_tool(
    failure_error_function=tool_error_handler("deliver"),
    needs_approval=approval.delivery_needs_approval,
)
@_isolated("deliver")
def deliver(ctx: RunContextWrapper[TaskContext], content: str, needs_review: bool = False) -> str:
    """把这一次要给用户的正文交付出去。

    正式产出（说明文、大纲、作品、以及任何你要作为最终答复的正文）都用它交付，
    把**要给用户看的正文本身**放进 ``content``。闲聊、打招呼、追问上下文不用调它，
    直接回答即可。

    Args:
        content: 要给用户看的正文本身（不要放"已完成"这类过程说明）。
        needs_review: 这次产出要不要先让人过目再发出去。题材敏感、结论影响面大、
            或者你自己拿不准质量时设为 True；普通答复保持 False。
            设为 True 时整个运行会**停在这里**等人工处理，人同意后继续，人打回时
            你会收到他写的修改意见，改完再交一次即可。
    """
    context = ctx.context
    text = (content or "").strip()
    if not isinstance(context, TaskContext):
        return "交付失败：缺少任务上下文，这次未交付。"
    # 空交付**必须抛**，不能返回一句"失败"：返回文本在步骤清单里算 ``ok``，
    # ``fallback._accomplished`` 会把它列成"做成了这一步"，而用户实际什么都没收到。
    # 这是"隐形失败"里代价最高的一种 —— 看板上这一轮是成功的。
    if not text:
        raise ToolContractError(
            "deliver 的 content 是空的：用户会收到一片空白，已拒绝交付。"
            "请把**要给用户看的正文本身**放进去再交一次（不要放「已完成」这类过程说明；"
            "如果确实没有正文要交，就不要调用本工具）。"
        )
    context.delivered["delivery"] = text
    context.delivered["delivery_reviewed"] = bool(needs_review)
    observability.counter(
        "agent_delivery_total", reviewed="true" if needs_review else "false"
    )
    return "已交付（人工已过目）。" if needs_review else "已交付。"


def render_slide_deck(deck_json: str, asset_dir: Path | str) -> str:
    """把结构化大纲渲染成 Markdown 文件（确定性步骤）。

    deck_json: ``SlideDeck`` 的 JSON 串（``model_dump_json()``）。
    asset_dir: 落盘目录。

    刻意**不做成 ``@function_tool``**：没有任何模型需要"决定要不要渲染"。
    暴露成工具只会给模型多一个可以忘掉的选项 —— 那正是"交付物缺失"这类
    偶发失败的来源。
    """
    deck = json.loads(deck_json)
    lines = [f"# {deck.get('title', '')}", ""]
    if deck.get("subtitle"):
        lines += [f"_{deck['subtitle']}_", ""]
    for index, slide in enumerate(deck.get("slides", []), start=1):
        lines.append(f"## {index}. {slide.get('title', '')}")
        lines += [f"- {item}" for item in slide.get("bullets", [])]
        if slide.get("speaker_notes"):
            lines.append(f"  > 备注：{slide['speaker_notes']}")
        lines.append("")
    path = _ensure_dir(Path(asset_dir)) / "deck.md"
    path.write_text("\n".join(lines), encoding="utf-8", newline="")
    observability.counter("agent_assets_total", kind="deck")
    return str(path)


#: 兼容别名：示例与文档里用的是 ``render_slide_deck`` 这个名字。
render_deck = render_slide_deck
