"""跨轮历史的 token 预算 —— 用官方 ``RunConfig.call_model_input_filter`` 裁模型输入。

为什么不是"条数"（2026-10-03 改）
--------------------------------
条数是**不用分词器**的省事近似：80 条可能只有 5k token（全是短句），也可能 120k
（中间夹了几次长工具返回）。模型的上下文窗口是按 token 计的，所以 token 才是唯一
"真实"的判据；条数只是"数数就行"的替代品，**两者互不知道对方** —— 于是
"窗口 100k + 只留 80 条"这种组合不构成任何保证。

**它现在是两层里的第二层**（2026-10-08 起）
-------------------------------------------
同一条口子（``call_model_input_filter``）上现在挂着两步，本模块是第二步：

    ① ``compaction.compact_for_window`` —— 超阈值就把老的那段换成**摘要**
       ↓ 压完还超？
    ② 本模块的硬截断 —— 从最老的开始**丢**
       ↓ 丢到只剩本轮一条还超？
    ③ 既有兜底：照发，由模型侧判定（``agent_history_over_budget_total``）

顺序不能反：压缩**有成本**（要调一次模型），不能在"明明丢几条就够"的时候也去调；
而硬截断是**确定性**的、兜底不会失效。反过来（先丢再压）等于把该被摘要的内容先丢了。
两层都可以各自关掉（``AGENT_HISTORY_COMPACT`` / 预算设成 0），行为分别退化。

官方口子在哪
------------
``RunConfig.call_model_input_filter``。官方 docstring 原文：

    Optional callback that is invoked immediately before calling the model. It receives
    the current agent, context and the model input (instructions and input items), and
    must return a possibly modified ModelInputData to use for the model call.

    This allows you to edit the input sent to the model **e.g. to stay within a token
    limit.**

⇒ "按 token 裁输入"正是官方**设计这个口子的用途本身**，不是本项目发明的做法。

形状（官方这里最容易写错）
------------------------
入参是 ``CallModelData``（``.model_data`` 才是 ``ModelInputData``），**出参是
``ModelInputData``** —— 不是"进什么出什么"。写成 ``filter(data) -> data`` 会在**每一次**
模型调用时炸，而它跑在 ``Runner.run`` **内部**，表现成"agent 动不动答不出来"，
排查起点完全错（与 ``session.py`` 里那条"必须主动 ping"是同一类坑）。

裁的四条口径（都是刻意的）
------------------------
1. **只裁历史，绝不裁本轮新输入**：最后一个条目永远保留。裁掉它 = 模型连用户刚说的
   话都看不到，而请求照常成功 —— 最难发现的一种坏。
2. **预算算的是"指令 + 输入条目"的合计**：``ModelInputData`` 就只有这两部分。
   ⚠️ **工具清单不在里面**（它在 ``tools`` 形参上，本口子看不到）⇒ 这是
   "**输入预算**"，不是"整个窗口的账本"：必须给工具表和输出长度留出余量。
3. **裁完必须清孤儿**：从最老的开始丢，会把 ``function_call`` 和它的
   ``function_call_output`` 拆散，回灌会被接口拒。这一步**委托官方实现**
   （``agents.run_internal.items.drop_orphan_function_calls``），不自己写一套 ——
   它还要连带清掉"紧随被丢调用之前的 reasoning 条目"，那条规则很容易抄漏。
4. **只量到预算为止，不量被丢掉的那一段**（2026-10-03 加，这是**上界**的来源）：
   从最新往老累加，累加值一旦越界就停 —— 更老的条目**必定被丢**，给它们算 token
   是纯浪费。这一点在本项目不是优化而是**必需**：历史是**永久保留**的
   （见 ``app/config.py`` 的 ``retention_days``），库里有多少就编多少 ⇒ 开销无上界。
   实测（10 万 token 的条目集，同一台机器）：全量编码随历史**严格线性** ——
   10 万 token 15.8ms / 50 万 82.3ms / 100 万 157.2ms / 200 万 341.1ms
   （≈ 0.17ms 每千 token）；改成"量到预算为止"后，无论库里堆了多少，
   编码量都封顶在"预算 + 最后一条"上。

为什么是 **async**（2026-10-03 改）
---------------------------------
分词编码是纯 CPU，跑在协程里会**占住整个事件循环** —— 本进程内其它并发请求
（包括正在推的 SSE 流）一起停转。官方那个钩子的类型是
``Callable[..., MaybeAwaitable[ModelInputData]]``，SDK 侧有 ``inspect.isawaitable``
分支（``agents/run_internal/turn_preparation.py``），**async 是官方支持的形状**，
不是本项目发明的写法。所以这里把整个测量+裁剪丢进工作线程。

计数是近似，所以这是"软上限"
--------------------------
本项目模型是第三方网关模型（``agnes-*``），没有公开分词器，用 ``tiktoken`` 的
``cl100k_base`` 近似。口径与 ``app/quota.py`` 一致：有 tiktoken 就用它，没有则退
``max(1, len(text))``（宁可高估、早一点刹住）。**它不等于服务端的计费口径** ——
这里算出"没超"不等于服务端认为没超。

⚠️ 本模块导入 ``agents``（SDK）。因此与 ``session.py`` 同一条约束：只能被
``src/agents/*`` 里同样是惰性加载的模块引用，**不能**出现在 ``src/graph.py`` 的
模块级 import 里。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from .. import observability

__all__ = [
    "count_tokens",
    "history_token_limit",
    "reset_history_budget_state",
    "trim_model_input",
    "warmup_tokenizer",
]

#: 默认预算（token）。**这是输入预算，不是整个窗口** —— 见模块 docstring 第 2 条。
#: 取值必须留出"工具表 + 输出长度"的余量；``0`` / 负数 ⇒ 不限制（不推荐：
#: 输入随轮次线性上涨，且必然在某轮撞上窗口）。
_DEFAULT_TOKEN_LIMIT = 100_000

_encoder: Any = None
_encoder_failed = False
#: 预算计算失败已经打过完整日志了吗。防日志放大：真出问题时**每次模型调用**都会
#: 走到这里，几百 QPS 下全量打日志能把日志盘打满，而排查所需信息第一条就有。
_failure_logged = False


def history_token_limit() -> int | None:
    """送进模型的输入预算（token）；``None`` = 不限制。"""
    raw = (os.getenv("AGENT_HISTORY_TOKEN_LIMIT", "") or "").strip()
    if not raw:
        return _DEFAULT_TOKEN_LIMIT
    try:
        n = int(raw)
    except ValueError:
        logging.warning(
            "AGENT_HISTORY_TOKEN_LIMIT 不是整数（= %r），改用默认值 %d",
            raw,
            _DEFAULT_TOKEN_LIMIT,
        )
        return _DEFAULT_TOKEN_LIMIT
    return n if n > 0 else None


def _encoder_or_none() -> Any:
    global _encoder, _encoder_failed
    if _encoder is None and not _encoder_failed:
        try:
            import tiktoken

            _encoder = tiktoken.get_encoding("cl100k_base")
        except Exception as e:  # noqa: BLE001  没装 / 拉不到词表（离线）
            _encoder_failed = True
            logging.warning(
                "tiktoken 不可用（%s）⇒ 历史预算退化为**字符数**近似（偏保守）。"
                "计数口径与 app/quota.py 一致。",
                e,
            )
    return _encoder


def count_tokens(text: str) -> int:
    """近似 token 数。没有 tiktoken 时退 ``max(1, len(text))``（与 quota 同口径）。"""
    if not text:
        return 0
    enc = _encoder_or_none()
    if enc is None:
        return max(1, len(text))
    return len(enc.encode(text))


def _item_tokens(item: Any) -> int:
    """单个输入条目的 token 近似。

    按 JSON 序列化后计数，而不是只数 ``content`` 字符串：条目是带着 ``type`` /
    ``call_id`` / ``name`` 这些**必发字段**一起上线的，只数正文会**系统性低估**。
    """
    try:
        raw = json.dumps(item, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001  不该发生：输入条目都是 JSON 可序列化的
        raw = str(item)
    return count_tokens(raw)


def _note_failure(exc: BaseException) -> None:
    """记一次预算计算失败。只打第一条日志，其余只计数（理由见 ``_failure_logged``）。"""
    global _failure_logged
    observability.counter("agent_history_budget_errors_total", kind=type(exc).__name__)
    if _failure_logged:
        logging.debug("历史预算计算仍失败：%s", exc)
        return
    _failure_logged = True
    logging.error(
        "跨轮历史的 token 预算算不出来（%s）⇒ **本轮不做裁剪**，原样把历史交给模型。"
        "后果：输入可能超过窗口而被模型侧拒绝（表现为这一轮答不出来）。原因：%s",
        type(exc).__name__,
        exc,
    )


async def _compact_before_trim(data: Any) -> Any:
    """**入口二（agent 链路）**：超阈值就先把老的那段换成摘要，再交给硬截断。

    这是 2026-10-08 加的一层，位置在本模块自己的裁剪**之前**。顺序不能反：

        先压缩（把老的那段换成摘要）
          ↓ 压完还超？
        再跑本模块的硬截断（从最老的丢）
          ↓
        都不行 ⇒ 既有兜底：只剩本轮一条也照发，由模型侧判定

    理由：压缩**有成本**（要调一次模型），不能在"明明丢几条就够"的时候也去调；
    而硬截断是**确定性**的、兜底不会失效。反过来（先丢再压）等于把该被摘要的内容
    先丢了。

    刻意**不做**的两件事：

    * 不写回会话（库里那份由"历史压缩"负责，见 ``compaction.py`` 的
      "为什么窗口压缩不做写回"）；
    * 不在失败时抛出去 —— 压缩是可选的优化，它的失败只该退化成"这轮没压"，
      而**绝不能**让整轮请求挂掉（本函数的返回值直接决定模型看到什么）。
    """
    try:
        from agents.run_config import CallModelData, ModelInputData
    except Exception:  # noqa: BLE001
        return data
    model_data = getattr(data, "model_data", None)
    if not isinstance(model_data, ModelInputData):
        return data
    try:
        # 惰性导入：``compaction`` 在模块级导入本模块（它要用同一把 token 尺子），
        # 这里再模块级导入就成环了。
        from .compaction import compact_for_window

        replacement = await compact_for_window(
            list(model_data.input or []),
            instructions=model_data.instructions or "",
        )
    except Exception as e:  # noqa: BLE001  压缩失败不该影响作答
        logging.warning("窗口压缩失败，本轮按原样送模型：%s", e)
        return data
    if not replacement:
        return data
    return CallModelData(
        model_data=ModelInputData(
            input=replacement, instructions=model_data.instructions
        ),
        agent=getattr(data, "agent", None),
        context=getattr(data, "context", None),
    )


async def trim_model_input(data: Any) -> Any:
    """官方 ``call_model_input_filter``：``CallModelData`` 进，``ModelInputData`` 出。

    2026-10-08 起这是**两层**里的第二层（第一层是``compaction.compact_for_window``，
    见 ``_compact_before_trim``）：压缩先把老的那段换成摘要，压完还超才轮到这里的
    硬截断。

    ⚠️ 是 **async**，且真正的活在线程里干 —— 理由见模块 docstring 末节。
    官方 ``maybe_filter_model_input`` 会对返回值做 ``inspect.isawaitable`` 判定，
    所以这个形状是被支持的第一类写法，不是绕路。
    """
    data = await _compact_before_trim(data)
    return await asyncio.to_thread(_trim_sync, data)


def _trim_sync(data: Any) -> Any:
    """同步实现（测量 + 裁剪）。只由 ``trim_model_input`` 在线程里调用。

    只在**超预算**时才动；没超时原样返回（不做任何拷贝）。
    """
    from agents.run_config import ModelInputData

    model_data = getattr(data, "model_data", None)
    if not isinstance(model_data, ModelInputData):
        # 形状不对 ⇒ 原样放行。这条分支理论上走不到，但它一旦走到（SDK 换了形状），
        # 抛出去会把整轮请求打挂，而"不裁剪"只是退化成旧行为。
        return data

    budget = history_token_limit()
    if budget is None:
        return model_data

    items = list(model_data.input or [])
    if not items:
        return model_data

    try:
        # 从**最新往最老**累加，一旦"再加上这条就超预算"立刻停 —— 更老的条目
        # 必定被丢，量它们纯属浪费（口径 4，也是开销上界的来源）。
        total = count_tokens(model_data.instructions or "")
        i = len(items) - 1
        while i >= 0:
            cost = _item_tokens(items[i])
            # 最后一条是本轮新输入，无条件保留（口径 1）：连它都不留下的结果
            # 是"模型看不到用户刚说的话"，而请求照常 200。
            if i < len(items) - 1 and total + cost > budget:
                break
            total += cost
            i -= 1
        keep_from = i + 1

        if keep_from == 0:
            return model_data  # 没超预算

        dropped = keep_from
        kept = items[keep_from:]

        # 裁完必须清孤儿（口径 3，委托官方）。`output_pruning_indexes` 传全量：
        # 我们丢的是**头部**，留下的第一条很可能就是"调用方已被丢掉"的工具结果；
        # 官方 docstring 说这个参数用于 "unambiguous stored history" —— 而我们读的
        # 正是库里的完整会话历史，语义对得上。
        # ⚠️ 别拿 SDK 内部那两处调用（都不传参数）当反例：它们不裁历史，所以不需要。
        from agents.run_internal.items import drop_orphan_function_calls

        kept = drop_orphan_function_calls(
            kept, output_pruning_indexes=set(range(len(kept)))
        )
    except Exception as e:  # noqa: BLE001  预算算不出来不该让整轮失败
        _note_failure(e)
        return model_data

    observability.counter("agent_history_trimmed_total")
    observability.histogram("agent_history_dropped_items", dropped)
    observability.histogram("agent_history_input_tokens", total)

    if len(kept) <= 1 and total > budget:
        # 只剩本轮新输入一条仍超额：**不再继续裁**（裁掉它等于模型看不见用户刚说的话），
        # 交给模型侧判定。这条告警指向配置：预算偏小，或单条输入过长。
        observability.counter("agent_history_over_budget_total")
        logging.warning(
            "跨轮历史预算不足：已裁到只剩本轮新输入（约 %d token）仍超过预算 %d ⇒ "
            "本轮照常发出，由模型侧判定。若频繁出现，请调大 AGENT_HISTORY_TOKEN_LIMIT "
            "或检查单条输入为何这么长。",
            total,
            budget,
        )

    return ModelInputData(input=kept, instructions=model_data.instructions)


def reset_history_budget_state() -> None:
    """清掉"已打过失败日志"的标记（**测试用**）。"""
    global _failure_logged
    _failure_logged = False


async def warmup_tokenizer() -> None:
    """预热分词器 —— 把词表的冷加载从**第一个用户请求**里挪到启动期。

    为什么需要这一步：``tiktoken`` 的编码器是**首次编码时**才加载词表的，
    实测首次 ``count_tokens`` **277.6ms**、之后 **0.054ms**。不预热的话，这 277ms
    会落在"第一个真正需要裁剪的请求"里；而它跑在工作线程中，表现为
    "服务刚起来时第一条请求莫名多等了两百多毫秒"，随后永远复现不出来。

    失败（没装 tiktoken / 离线拿不到词表）不是错误：``count_tokens`` 会退化成
    字符数近似，那条 warning 由 ``_encoder_or_none`` 自己打。
    """
    await asyncio.to_thread(count_tokens, "预热")
