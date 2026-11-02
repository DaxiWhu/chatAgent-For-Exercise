"""跨轮历史的压缩 —— 两个固定入口，共用一个内核。

要解决什么
----------
会话历史是**全量追加**的（``session.py`` 的模块 docstring 写了为什么刻意不设条数
上限），而每一轮都要把**整份历史**送进模型。两者叠加的结果是**平方**：第 N 轮要
为前面每一轮的对话各付一次输入 token，聊得越久单位对话越贵。

| 轮数 | 对话条目 | 每轮输入条目（累计） |
|---|---|---|
| 3 | 6 | 6 + 4 + 2 = 12 |
| 12 | 24 | 24 + 22 + … + 2 = 156 |
| 24 | 48 | 48 + 46 + … + 2 = 600 |

压缩切断的就是这条链：把"**前 N 轮之前**"的老历史换成一段摘要，此后每轮送进
模型的都只带「摘要 + 最近 N 轮」，增量从"整段历史"变成"常数" ⇒ **增长阶从平方
降到线性**。

两个固定入口（共用 §"压缩内核"）
------------------------------
======================  ==================================  ==================  ==========
入口                    触发                                压哪一份            落盘
======================  ==================================  ==================  ==========
**历史压缩**            老历史超过"保留轮数"                会话日志（只读）    **写记录**
**窗口压缩**            送进模型的输入 > 预算的 80%         本次调用的那一份     否
======================  ==================================  ==================  ==========

* **历史压缩**每轮各判一次（请求开头的后台任务）。它**不改会话日志**，只往
  `agent_history_compactions` 追加一条"覆盖了前 N 条 + 这段的摘要"；下一轮
  `get_items` 投影时按最新那条记录算出一份短历史。它**晚一轮生效** —— 本轮读到
  的是压缩前的历史，行为与不压完全一致。
* **窗口压缩**判的是**将要送进模型的那一份输入**——它本来就含"本轮新增 + 上一次
  返回的内容"，所以是**提前**判而不是事后发现。它**只改这一次调用**：库里那份由
  历史压缩负责，这里再写一次库既无收益，又要在 SDK 的会话写入之间抢时序。

⚠️ 2026-11-02 之前是"清空会话 + 写回摘要与尾部"，那一步真的删库。改成记录 + 投影
的理由见 `compaction_store.py` 的模块 docstring：摘要丢的细节要能回查，长期记忆也
要有素材可提炼。

⚠️ 两个入口都**默认关闭**（``AGENT_HISTORY_COMPACT``），出问题一键退回今天的行为。

为什么触发点必须自己找（一手依据）
--------------------------------
SDK 里**有一个现成的压缩会话**（``agents/memory/openai_responses_compaction_session.py``），
形状可以照抄，但它的触发**在本项目永远不会被调用**：

* 官方调用点的判据是
  ``if response_id and is_openai_responses_compaction_aware_session(session)``
  （``agents/run_internal/session_persistence.py``）；
* ``response_id`` 只有 **Responses 接口**才会给（``models/openai_responses.py`` 里
  ``response_id=response.id``），而本项目走 **Chat Completions**
  （``src/agents/model.py`` 用 ``OpenAIChatCompletionsModel``，SDK 那一侧写死
  ``response_id=None``）⇒ **恒为空**。

⇒ 就算把模型名骗过构造期校验，那个钩子也只是**安静地什么都不做**。所以形状照抄
官方，**触发点必须自己找** —— 就是本模块的两个入口。

另外两条官方约束照抄、不自己发明：

1. **它刻意不压用户说的话**（``select_compaction_candidate_items`` 把
   ``role == "user"`` 整个排除）。本模块的对应做法是**按轮切分**：一轮 = 一个用户
   条目 + 它之后直到下一个用户条目之前的全部内容，切点永远落在用户条目之前。
2. **原路线的"清空 + 写回当一次替换事务"已废弃**（2026-11-02）。现在不做替换，
   所以也不需要回滚 —— 写一条记录失败就是"这轮没压"，会话日志一个字节都没动过。
   这条比原来的回滚更强：**没有中间态可言**。

为什么窗口压缩不做写回
--------------------
官方那张表里"写回"一栏两边都是"库"，本模块**刻意只让历史压缩写回**，理由是实测
出来的，不是省事：

* 写回要认准"哪一段是会话历史、哪一段是本轮新输入"。SDK 侧的形状是
  ``prepared = converted_history + new_input_list``（``prepare_input_with_session``），
  而 ``call_model_input_filter`` 只拿得到拼好的输入、拿不到那个切点 ⇒ 只能靠
  "``len(input) − len(await session.get_items())``"反推。反推错了的表现是**把本轮
  用户那一句写进会话、SDK 收尾时又写一遍** ⇒ 同一句用户消息在会话里出现两次，
  而它不报错，只表现为"模型莫名重复了一遍用户的话"（``runner._build_input_items``
  的注释点名的就是这个症状）。
* 收益很小：历史压缩每轮都在跑，库里本来就封顶在"保留轮数 + 一条摘要"；
  窗口压缩要处理的只是"这几轮本身就太大"（比如一次超长的工具返回），那是
  **单次调用**要解决的事。

⇒ 想同时省存储与 token 时，**存储那一半已经由历史压缩做了**，不需要在窗口这一侧
再来一遍。这条如果将来要改（比如要求窗口压缩也落库），必须先把上面那个切点问题
解决掉 —— 别直接加上去。

⚠️ 本模块导入 ``agents`` 吗？—— **不**。它只做"按轮切分 + 写摘要 + 替换"，替换
动作对会话侧是鸭子类型（官方靠 ``getattr(session, "run_compaction")`` 识别，不看
类型）。所以它**不**受 ``session.py`` 里那条
"只能被惰性加载的模块引用"的约束，``session.py`` 可以在模块级导入它。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from .. import observability
from ..envutil import env_float, env_int
from . import history_budget
from .compaction_store import InMemoryCompactionStore
from .history_budget import count_tokens, history_token_limit, is_user_item

__all__ = [
    "CompactingSession",
    "compact_session_history",
    "compact_for_window",
    "history_compact_enabled",
    "keep_rounds",
    "project_history",
    "reset_compaction_state",
    "round_starts",
    "summarize_items",
    "window_compact_ratio",
    "window_compact_trigger",
]

# ---------------------------------------------------------------------------
# 配置（与 app/config.py 同一组环境变量名，链路层直读，理由同 session.py）
# ---------------------------------------------------------------------------
#: 保留最近多少轮**原样不动**。下限 **1**，不是随手定的：
#: 人工打回那一轮（``/v1/review`` 的 revise）靠"**原话已经躺在会话里**"来决定
#: 不重传原话（``runner._build_input_items``），压成 0 会让第二遍只看到一句
#: "人工审核意见：…"、**不知道在改什么** —— 且不报错。
#: 打回发生在"刚刚跑完的那一轮"，它永远是最新的，所以 1 就够，5 只是给日常留余量。
_KEEP_ROUNDS_DEFAULT = 5

#: 窗口压缩的触发线 = 预算 × 这个比例。
#: 2026-11-02 从 0.85 改到 0.80：0.85 意味着等到"模型已经开始退化"才动手，
#: 而心得那条易发问题说的是"窗口没满、注意力和任务表现已经下降"。
#: 0.85 实测也不是安全边界 —— 它只比硬截断早 15%，一轮里塞进一篇长文档就跨过去了。
_RATIO_DEFAULT = 0.80

_TRUE = ("1", "on", "true", "yes")
_FALSE = ("0", "off", "false", "no")


def history_compact_enabled() -> bool:
    """总开关。**默认关闭** —— 新链路先可关，出问题退回的代价是零。"""
    raw = (os.getenv("AGENT_HISTORY_COMPACT", "") or "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return False


def keep_rounds() -> int:
    """保留最近多少轮原样不动（下限 1，见 ``_KEEP_ROUNDS_DEFAULT``）。"""
    return env_int("AGENT_HISTORY_KEEP_ROUNDS", _KEEP_ROUNDS_DEFAULT, lo=1)


def window_compact_ratio() -> float:
    """窗口压缩的触发比例（默认 0.85）。做成配置是因为换模型只该改配置。"""
    return env_float("AGENT_HISTORY_COMPACT_RATIO", _RATIO_DEFAULT, lo=0.1, hi=1.0)


def window_compact_trigger() -> int | None:
    """窗口压缩的触发线（token）；``None`` = 不限制（也就无从触发）。"""
    budget = history_token_limit()
    if budget is None:
        return None
    return max(1, int(budget * window_compact_ratio()))


def retain_ratio() -> float:
    """最近那部分**逐字保留**占预算的比例，默认 0.16。

    为什么按预算而不按轮数：一轮可能很长（一次工具返回塞进一篇文档），5 轮未必
    装得下；也可能 2 轮就撑爆。轮数是个与真实成本无关的刻度。
    """
    return env_float("AGENT_HISTORY_COMPACT_RETAIN_RATIO", 0.16, lo=0.0, hi=1.0)


def tail_budget() -> int | None:
    """逐字保留段的 token 预算；``None`` = 没配总预算（退化成按轮数，保持旧口径）。"""
    budget = history_token_limit()
    if budget is None:
        return None
    return max(1, int(budget * retain_ratio()))


#: 模型报"上下文超了"的文案特征。**只看文案，不猜异常类型**：
#: 各网关/各家 SDK 的类型各不相同（openai 的 ``BadRequestError``、httpx 的
#: ``HTTPStatusError``、有的网关直接返 400 + 一句中文），而类型判据一旦漏一种，
#: 兜底就静默失效 —— 症状仍是"长对话忽然报错"，和没有兜底时一模一样。
_OVERFLOW_MARKERS = (
    "context_length_exceeded",
    "maximum context length",
    "context length",
    "context window",
    "too many tokens",
    "reduce the length",
    "prompt is too long",
    "input is too long",
    "exceeds the maximum",
    "超过最大",
    "上下文长度",
)


def is_context_overflow(exc: BaseException) -> bool:
    """这次失败是不是"上下文超了"（而不是上游崩了、限流、参数错）。

    判据宽的代价是"多压一次再失败一次"（多花一次调用）；判据窄的代价是兜底永远
    不触发。这里**选宽**：多花一次调用是可观测的（有指标），兜底不触发是静默的。
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _OVERFLOW_MARKERS)


def _summarize_timeout() -> float:
    """写摘要那次模型调用的超时（秒）。独立可调：它是**额外**的一次调用。"""
    return env_float("AGENT_HISTORY_COMPACT_TIMEOUT", 30.0, lo=1.0)


def _max_segment_chars() -> int:
    """交给摘要模型的正文上限（字符）。超出部分**从最老的开始丢**。

    为什么需要它：摘要本身也被窗口约束。正常路径下每段只有"刚滑出保留窗的那一轮"
    那么大，但开关刚打开时可能积了很长一段 —— 不设上限就是一次注定失败的调用
    （而失败会被降级成"这轮没压"，于是**永远压不动**，且看起来像功能没生效）。
    """
    return env_int("AGENT_HISTORY_COMPACT_MAX_CHARS", 200_000, lo=1_000)


# ---------------------------------------------------------------------------
# 按轮切分
# ---------------------------------------------------------------------------
_is_user_item = is_user_item
"""判"这是不是一轮的起点"的**唯一实现**在 ``history_budget``（本模块导入的方向是
"高层引用低层"，反过来会成环 —— 见 ``history_budget.is_user_item`` 的说明）。

这里保留旧名字只是为了让本模块内部的调用点与既有测试不用改：语义一个字都没变。
"""


def _content_text(content: Any) -> str:
    """把条目正文压成一段纯文本。str / list（多段 content）/ 其它都能吃。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            # 多段 content 的元素有两种形态：dict（服务端返回）与对象（LangChain 的
            # content block）。两种都取 text，取不到就跳过。
            text = (
                (part.get("text") or part.get("content"))
                if isinstance(part, dict)
                else getattr(part, "text", None)
            )
            if isinstance(text, str):
                parts.append(text)
        return " ".join(parts)
    return str(content)


def _item_text(item: Any) -> str:
    """一个条目的可读文本（带角色/类型前缀，写摘要时才分得清谁说了什么）。"""
    if isinstance(item, dict):
        kind = item.get("type")
        if kind == "function_call":
            return f"[工具调用 {item.get('name') or ''}] {_content_text(item.get('arguments'))}"
        if kind == "function_call_output":
            return f"[工具返回] {_content_text(item.get('output'))}"
        role = item.get("role") or kind or "?"
        return f"{role}: {_content_text(item.get('content'))}"
    role = getattr(item, "type", "?")
    return f"{role}: {_content_text(getattr(item, 'content', ''))}"


def round_starts(items: list[Any]) -> list[int]:
    """每条"轮的起点"在列表里的下标。"""
    return [i for i, item in enumerate(items) if _is_user_item(item)]


def _plan_keep_from(items: list[Any], *, keep: int) -> int | None:
    """算出"保留段从哪开始"；``None`` = 不需要压。

    保留段 = **最后 ``keep`` 轮的完整内容**（从某一轮的起点开始，因此切点永远落在
    用户条目之前 —— 绝不会落在"助手说要调工具"和"工具返回"之间，那两个靠调用编号
    配对，切开就散了，回灌会被接口拒）。

    压缩段 = ``items[:keep_from]``，它**天然包含前缀**（第一条用户条目之前的东西）。
    本项目往那个位置放的只有上一次的摘要，所以"把旧摘要折进新摘要"不需要额外代码：
    它本来就在被压缩的那一段里。
    """
    starts = round_starts(items)
    if len(starts) <= keep:
        return None
    return starts[-keep]


def _rounds_that_fit(items: list[Any], *, budget: int, cap: int) -> int | None:
    """在预算内**从最新往回**尽量多留几轮；``None`` = 连 ``cap`` 轮都装得下。

    为什么是"尽量多"而不是固定留 cap 轮：窗口压缩要解决的是"**这几轮本身就
    太大**"，固定留 cap 轮就压缩不了任何东西（老的那段早被历史压缩清空了）。
    所以它按 token 从最新往回累加，装不下就停。

    两条口径：

    * **最新那一轮无条件保留**（``k`` 从 0 起也照样加）—— 丢掉它等于模型连用户
      刚说的话都看不到，而请求照常 200（``history_budget`` 里那条口径 1 同源）。
    * **一旦攒够 ``cap`` 轮就直接返回 None**：说明历史压缩那一侧的目标已经满足，
      这里没有可压的东西（老的那段由历史压缩负责，不归这里）。
    """
    starts = round_starts(items)
    if not starts:
        return None
    total = 0
    # ⚠️ ``taken`` 必须在"真正装下这一轮"之后才更新，且**不能用 ``idx + 1`` 代替**：
    # 从 ``break`` 退出时那一轮没装下，轮数是 ``idx``；正常遍历完才是 ``idx + 1``。
    # 两种退出路径刻度不同，写成一个式子就会在"预算刚好吃紧"时多留一轮
    # （= 压缩覆盖范围推不动，且没有任何报错）。
    taken = 0
    for idx, start in enumerate(reversed(starts)):
        end = len(items) if idx == 0 else starts[len(starts) - idx]
        cost = sum(history_budget._item_tokens(x) for x in items[start:end])
        if idx and total + cost > budget:
            break
        total += cost
        taken = idx + 1
        if taken >= cap:
            return None
    return max(1, taken)


def _keep_from_by_tokens(items: list[Any], *, budget: int, cap: int) -> int | None:
    """按 **token 预算**从最新往回攒保留段，返回它的起点；``None`` = 不需要压。

    与 ``_plan_keep_from``（按轮数）的区别：一轮的长度可能差两个数量级，而模型的
    成本只认 token。"留 5 轮"在正常对话里绰绰有余，在一次超长工具返回之后就毫无
    意义 —— 那时 2 轮就撑爆了。

    两条下限同时满足：

    * **至少留最后一轮**：丢掉它等于模型连用户刚说的话都看不到，而请求照常 200；
    * **不超过 ``cap`` 轮**：配置项仍然作上限，避免"每轮都很短"时留下几十轮
      （那会让摘要的覆盖范围永远推不动，压缩形同虚设）。
    """
    starts = round_starts(items)
    if not starts:
        return None
    total = 0
    kept_from = len(items)
    # 倒序遍历用 enumerate(reversed(...))，与 ``_plan_keep_from`` 保持同一形状 ——
    # 两处算的是同一个"从最新往回攒"的边界，写法一旦分叉就会在某次改动里只改一处。
    for idx, start in enumerate(reversed(starts)):
        end = len(items) if idx == 0 else starts[len(starts) - idx]
        cost = sum(history_budget._item_tokens(x) for x in items[start:end])
        if idx and total + cost > budget:
            break
        total += cost
        kept_from = start
        if idx + 1 >= cap:
            break
    return None if kept_from == 0 else kept_from


def _plan_tail(items: list[Any], *, cap: int, budget: int | None = None) -> int | None:
    """保留段起点：有 token 预算就按预算，没有就退回按轮数（旧口径）。

    ``budget=0`` 是**强压**用的：只留最后一轮。溢出兜底走这条 —— 那一步的判据是
    "模型已经拒了"，不该再拿一个可能同样算错的预算去商量。

    ⚠️ 压缩与"边界外推校验"必须调用**同一个**函数，否则两次算出的边界不可比，
    校验会变成随机通过或随机放弃。
    """
    if budget is None:
        budget = tail_budget()
    if budget is None:
        return _plan_keep_from(items, keep=cap)
    return _keep_from_by_tokens(items, budget=budget, cap=cap)


# ---------------------------------------------------------------------------
# 写摘要
# ---------------------------------------------------------------------------
_SUMMARY_PROMPT = (
    "下面是一段对话的**较早部分**（可能以一段已有摘要开头）。"
    "请把它压缩成一份**交接记录**：读它的是下一轮接着干的人（就是你自己），"
    "唯一标准是「拿着它能不能直接往下做，不用回头查」。\n"
    "必须按下面八个小节写，**一节都不许省**；某节确实没有内容就写「(none)」。\n"
    "一、原始请求与意图：这件事要干什么。目标变过就写清「从 X 改成 Y」，"
    "用户的措辞逐字引用。\n"
    "二、已完成事项：只写**已经确认**的结果，不写「大概做了」「应该可以」。\n"
    "三、待办与下一步：还剩什么；**卡点必须写出来**（卡在哪里、缺什么）。\n"
    "四、错误与修复：凡是带工具调用或明确动作的地方，逐条写成"
    "「动作 → 关键结果（成功 / 失败）」；**失败的、被否掉的、未完成（做了一半）的"
    "必须逐条保留**并写明原因 —— 后面的对话很可能正是为了避开它们，合并或省略会让"
    "接手者再试一遍。重复多次且结果相同的同一动作可以合并成一条并注明次数。\n"
    "五、关键上下文：做过的决策 + 理由 + 之后不能再改的约束；"
    "已经否决的方案也要写，否则会被重新讨论一遍。\n"
    "六、涉及的文件与代码：路径写精确，说明改了什么。\n"
    "七、技术概念与约定：用到的框架、模式、项目里定过的规矩。\n"
    "八、当前进展：压缩这一刻正在做什么。\n"
    "**逐字保留**（不许改写成概括）：文件路径、命令、报错原文、标识符、数值、日期、"
    "函数签名、语法片段，以及用户的原话（尤其是纠正过你的那几句）。"
    "这些东西极易被「总结得通顺」抹掉，而它们改一个字就失效。\n"
    "硬性要求：**不许编造原文里没有的信息**（看不出成败就写「结果未明」）；"
    "不写建议、评价、客套话；不用「我」或「你」称呼；"
    "直接输出八个小节，不要额外的标题或前后缀；"
    "总长控制在 {limit} 字以内。"
)

#: 摘要字数上限的下限。低于这个数，八个小节每节只剩一句，等于把细节压成结论 ——
#: 而"细节被压没"正是这套机制要防的第一件事。
_MIN_SUMMARY_CHARS = 600

#: 上限。再长就不是摘要了，而且它每轮都进上下文。
_MAX_SUMMARY_CHARS = 6000

#: 摘要上限 = 被压段正文的 1/5。为什么按比例而不是固定 6000：被压段可能是一轮
#: 闲聊（600 字足够），也可能是几十步的工具链（6000 字仍嫌少）。固定值必然在
#: 某一头出错，而错的方向分别是"丢细节"和"占位置"。
_SUMMARY_CHAR_RATIO = 0.2


def _summary_max_chars(body_chars: int) -> int:
    """这一次摘要的字数上限（按被压段长度定，夹在上下限之间）。"""
    return max(_MIN_SUMMARY_CHARS,
               min(_MAX_SUMMARY_CHARS, int(body_chars * _SUMMARY_CHAR_RATIO)))

_MAX_GOAL_CHARS = 500
"""传给摘要模型的"最初目标"最多多长。

⚠️ 与 ``reflection._MAX_GOAL_CHARS`` 同一个数、同一个理由：目标就是用户那句话，
入口允许到 10 万字符，整段塞进去会让**每一次压缩**的输入都跟着用户输入线性膨胀，
而摘要只需要知道"要解决什么"。两处都改的时候要一起改 —— 它们回答的是同一个问题
（"目标要精简到什么程度才够判读"），数字不一致只会让排查时多一个变量。
"""


def _summary_system_prompt(goal: str = "", max_chars: int = _MIN_SUMMARY_CHARS) -> str:
    """摘要提示词，并在给出目标时追加一行"这件事一开始要做什么"。

    ⚠️ 目标必须写在 **system** 里、而不是拼进待压缩的正文：
    拼进正文它看起来就像"对话里出现过的一句话"，摘要模型可能把它当成用户说过的
    话再复述一遍（于是摘要里出现一段"用户要求…"的伪引用）；写在 system 里它才是
    一条**要求**。

    ⚠️ 不写这一行的后果不是"摘要差一点"，而是**摘要的第一项靠猜**：
    当被压段的开头已经被上一次压缩换成摘要时，唯一还能代表"最初要干什么"的东西
    就是这里传进来的目标。
    """
    base = _SUMMARY_PROMPT.format(limit=max(1, int(max_chars)))
    text = " ".join(str(goal or "").split())
    if not text:
        return base
    if len(text) > _MAX_GOAL_CHARS:
        text = text[:_MAX_GOAL_CHARS] + "…（已截断）"
    return (
        f"{base}\n\n"
        f"这件事一开始的目标是：{text}\n"
        "第一项「原始请求与意图」必须与它对齐：摘要里描述的任务不能变成另一件事。"
    )

#: 为什么是"八个小节"而不是一句自由文本
#: -------------------------------------
#: 演进有三步，每一步都是因为"上一版漏了东西"：
#:
#: 1. 最早只要求"需求与约束 / 已结论 / 进行到哪一步" ⇒ 压完一段二十步的任务，
#:    接手的人只知道"进行到哪一步"，不知道前十九步做过什么、哪几步失败了 ——
#:    而失败那几步恰恰是它**不该重试**的。
#: 2. 后来扩成"任务与约束 / 已做过的步骤"两节（600 字上限）⇒ 步骤有了，但
#:    "要做到什么程度才算完""涉及哪些文件""定过哪些不能变的约定"仍然没地方写。
#:    600 字的硬上限更是强迫模型把细节压成结论。
#: 3. 2026-11-02 对齐 DSH 的八节，并补上它没有的「已完成事项」—— 不写这一节，
#:    接手者会把已完成的部分重做一遍（心得里那条易发问题）。
#:
#: ⚠️ 为什么不去序列化运行内的步骤清单（``steps.StepLog``）：那份只覆盖**本次
#: 运行**，而压缩处理的是**若干轮之前**的历史 —— 那几轮的步骤记录早就随运行结束
#: 没了。所以结构只能从**对话条目本身**（工具调用与它的返回都在里面）重建。

#: 摘要客户端（懒建、进程内唯一）。**同步** client：调用方既有协程（走
#: ``asyncio.to_thread``）、也有脚本/启动期的同步上下文（见 ``src/agents/reflection.py``
#: 同样这么处理）—— 用同步 client + 到线程里跑，两条路只需要**一份**实现，也不会
#: 出现"异步 client 绑在别的 loop 上"的问题。
_client: Any = None
_client_failed = False
_failure_logged = False


def _model_config() -> tuple[str, str, str]:
    """(model, api_key, base_url) —— 与主链路**同一组**环境变量。

    刻意不引入第二套模型配置：两处用不同模型会让摘要口径漂，而"同一个服务里两个
    模型变量"是运维事故的常见来源（``src/agents/runtime.py`` 里同一条理由）。
    """
    model = (os.getenv("LLM_MODEL", "") or "").strip() or "deepseek-chat"
    key = (os.getenv("LLM_API_KEY", "") or "").strip()
    base = (os.getenv("LLM_API_BASE", "") or "").strip()
    return model, key, base


def _client_or_none() -> Any:
    global _client, _client_failed
    if _client is not None:
        return _client
    if _client_failed:
        return None
    _, key, base = _model_config()
    if not key:
        # 没配 Key ⇒ 没有摘要能力。**不是错误**：压缩整体降级成"什么都没做"，
        # 行为与开关关闭一致（与 runtime 那条"开着且没配 ⇒ 行为与关一致"同一原则）。
        return None
    try:
        from openai import OpenAI

        kwargs: dict[str, Any] = {"api_key": key, "timeout": _summarize_timeout(), "max_retries": 2}
        if base:
            kwargs["base_url"] = base
        _client = OpenAI(**kwargs)
    except Exception as e:  # noqa: BLE001  没装 openai / client 建不起来
        _client_failed = True
        logging.warning("摘要用的模型客户端建不起来 ⇒ 历史压缩不可用：%s", e)
        return None
    return _client


def _note_failure(exc: BaseException, *, stage: str) -> None:
    """记一次压缩失败。只打第一条完整日志（防日志放大，与 session.py 同一理由）。"""
    global _failure_logged
    observability.counter("agent_history_compaction_errors_total",
                          stage=stage, kind=type(exc).__name__)
    if _failure_logged:
        logging.debug("历史压缩仍失败（%s）：%s", stage, exc)
        return
    _failure_logged = True
    logging.error(
        "跨轮历史压缩失败（%s，%s）⇒ **这一轮不压**，历史按原样使用。"
        "后果只是增长没被压住（功能面不受影响）。原因：%s",
        stage, type(exc).__name__, exc,
    )


def _segment_body(segment: list[Any]) -> str:
    """拼出交给摘要模型的正文（太长时**从最老的开始丢**，见 ``_max_segment_chars``）。"""
    lines = [_item_text(x) for x in segment]
    lines = [x for x in lines if x.strip()]
    body = "\n".join(lines)
    limit = _max_segment_chars()
    if len(body) > limit:
        logging.warning("待压缩的历史有 %d 字符，超过上限 %d ⇒ 只把最近的那部分交给摘要",
                        len(body), limit)
        observability.counter("agent_history_compaction_truncated_total")
        body = body[-limit:]
    return body


def summarize_items(
    segment: list[Any], *, on_usage: Any = None, goal: str = ""
) -> str:
    """把一段历史压成摘要（**同步**；没有模型能力时返回空串）。

    返回空串有明确含义：**这次压不了**（没配 Key / 建不起 client / 调用失败 /
    模型返回空）。调用方据此原样返回、不做任何替换 —— 摘要为空却照做替换，等于
    把老历史**删掉**，那正好是这套机制最不能出的错。

    on_usage: 拿到这一次的 ``usage`` 时回调一次。**只有窗口压缩那条路会传**
    （它属于某一次运行，成本要进那一次运行的账）；会话写回那条路是后台任务、
    不属于任何一次运行，传 None。

    goal: 这件事**一开始要干什么**。只有窗口压缩那条路会传（它有 ``context``）；
    会话写回是后台任务、手上只有历史，没有"这一次运行的目标"可传 —— 那时摘要
    模型只能从对话里反推，如实写在这里，不要以为它总能拿到目标。

    ⚠️ 为什么这件事必须做：压缩是**长任务里最贵的一笔**——它要把被压掉的那一整段
    历史（可能几十万 token）重新读一遍。不记账的话，token 预算就是"只管主链路"，
    而真实账单里有相当一部分完全不在这张表上（见 ``budget.note_bypass``）。
    """
    client = _client_or_none()
    if client is None:
        return ""
    body = _segment_body(segment)
    if not body.strip():
        return ""
    model, _, _ = _model_config()
    # 计时段**只包住模型调用**，不包 `_segment_body` 的拼装与写压缩记录 —— 后者只是
    # 一次数据库写，不在这里归因。
    # 只有"摘要这一次出站调用"是新增在首 token 延迟上、却完全没有归因出口的那一段。
    started = time.perf_counter()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system",
                 "content": _summary_system_prompt(goal, _summary_max_chars(len(body)))},
                {"role": "user", "content": body},
            ],
        )
        text = resp.choices[0].message.content or ""
    except Exception as e:  # noqa: BLE001  上游抖动 / 网关不认 / 超时
        # 失败也记耗时：超时型故障的耗时正是"这次为什么慢"的直接证据，
        # 只记成功样本会让分布里恰好缺掉最长的那一截。
        observability.histogram("agent_history_compaction_seconds",
                                time.perf_counter() - started, result="error")
        _note_failure(e, stage="summarize")
        return ""
    observability.histogram("agent_history_compaction_seconds",
                            time.perf_counter() - started, result="ok")
    # 记在成功返回之后：这次出站调用已经花掉了，不管后面摘要能不能用
    # （返回空内容也算花过钱，下面那条"空摘要不替换"的分支照样该记账）。
    if on_usage is not None:
        usage = getattr(resp, "usage", None)
        if usage is not None:
            on_usage(usage)
    text = text.strip()
    if not text:
        logging.warning("摘要模型返回了空内容 ⇒ 这一轮不压（不拿空摘要替换历史）")
        observability.counter("agent_history_compaction_errors_total",
                              stage="summarize", kind="EmptySummary")
    return text


async def _summarize_async(
    segment: list[Any], *, on_usage: Any = None, goal: str = ""
) -> str:
    """协程侧的摘要入口：真正的活在**线程**里干。

    与 ``history_budget.trim_model_input`` 同一手法 —— 一次同步 HTTP 往返跑在协程里
    会把本进程所有并发请求（含正在推的 SSE 流）一起停住。
    """
    return await asyncio.to_thread(
        summarize_items, segment, on_usage=on_usage, goal=goal
    )


# ---------------------------------------------------------------------------
# 压缩记录与投影（**不碰会话日志**）
# ---------------------------------------------------------------------------
async def _latest_record(store: Any, session_id: str, *, stage: str) -> dict | None:
    """取这个会话最新一条生效的压缩记录；**读不到就当成"没压过"**。

    为什么失败要吞：读记录在两条路径上（压缩前判断边界、每次取历史的投影）。
    它坏了最坏的后果是"压缩不生效/历史全量返回"，那是**功能退化**；
    抛出去则是整个会话读不出来 —— 用户表现为"一觉醒来失忆"。
    """
    if not session_id:
        return None
    try:
        return await store.latest_active(session_id, scope="history")
    except Exception as e:  # noqa: BLE001  退化成"没压过"，不是失败
        _note_failure(e, stage=stage)
        return None


def _segment_with_prior(segment: list[Any], prior: dict | None) -> list[Any]:
    """把上一代摘要放在待压段的最前面 —— 滚动压缩靠这一步。

    没有它，第二次压缩只看"上次覆盖点到这次覆盖点"之间的原文，上一代摘要里那些
    更早的信息（尤其"这件事一开始要干什么"）会在换代时整段丢掉，而摘要本身看起来
    完全正常。
    """
    summary = (prior or {}).get("summary") or ""
    if not summary.strip():
        return segment
    return [{"role": "system", "content": summary}, *segment]


def project_history(history: list[Any], record: dict | None) -> list[Any]:
    """按压缩记录算出**模型该看到的那一份**历史：一条摘要 + 未被覆盖的原文。

    三条性质，缺一条都会出事：

    * **只读**：这里一次写库、一次模型调用都不做。它在每次取历史的路径上，
      一个副作用就会变成每轮一次。
    * **幂等**：同一份日志 + 同一条记录，算出的结果必须逐字节一致。不一致的后果
      不是"结果错"，而是**上游前缀缓存命中不了**，每轮全量重算 —— 症状是"变慢变贵，
      但没有任何报错"。
    * **宁可全量，不给错量**：记录说覆盖了 40 条、而库里只剩 30 条时，说明"只追加"
      这个前提被破了（有人按条删了）。这时退回全量历史 —— 摘要里可能含已经不在库里
      的内容，照着它答会比"多给几条原文"错得更远。
    """
    if not record:
        return list(history)
    summary = record.get("summary") or ""
    covered = int(record.get("covered_items") or 0)
    if not summary.strip() or covered <= 0:
        return list(history)
    if covered > len(history):
        logging.warning(
            "压缩记录覆盖 %d 条，但库里只剩 %d 条 ⇒ 退回全量历史。"
            "「会话日志只追加」这个前提被破了（是否有人按条删除过？）",
            covered, len(history),
        )
        observability.counter("agent_history_projection_errors_total", reason="shorter")
        return list(history)
    return [{"role": "system", "content": summary}, *history[covered:]]


def _session_replacement(summary: str, tail: list[Any]) -> list[Any]:
    """**窗口压缩**用的替换列表：一条 system 摘要 + 原样保留的最近几轮。

    会话侧已经不用这个函数了（那边只写记录、不动库）。留着的唯一理由是窗口压缩：
    它替换的是"本次调用送进模型的那一份"，不是库。

    为什么摘要写成 ``role="system"``（三条，缺一不可）：

    1. 它是转换器明确支持的角色 —— 官方判据原文是
       ``if role not in ("user", "assistant", "system", "developer")``
       （``agents/models/chatcmpl_converter.py``），一路走到
       ``ChatCompletionSystemMessageParam``；
    2. 它**天然不被当成"一轮"**（轮按用户条目切）⇒ 下一次压缩不会把上一次的摘要
       当成新的一轮再压一遍；
    3. 语义上它就是"系统补的一段背景"，不是用户说的话、也不是助手的回答。

    投影用的也是同一个形状（见 ``project_history``），所以模型看到的两种来源
    （历史压缩 / 窗口压缩）在结构上一致。
    """
    return [{"role": "system", "content": summary}, *tail]


# ---------------------------------------------------------------------------
# 会话装饰器（照官方形状）
# ---------------------------------------------------------------------------
class CompactingSession:
    """会话装饰器：**读的时候投影**、写的时候透传，外加一把锁与一个压缩入口。

    为什么要装饰而不是子类：官方的识别方式是**鸭子类型**
    （``is_openai_responses_compaction_aware_session`` 只 ``getattr`` 看
    ``run_compaction`` 是不是可调用），不检查类型 ⇒ 不必继承任何基类。

    锁的覆盖面：

    * ``get_items`` **不持锁**（纯读 + 一次记录查询，不写任何东西）；
    * ``add_items`` / ``pop_item`` / ``clear_session`` 持同一把锁；
    * 压缩自己持锁，且"重读 → 写记录"整段在锁内。

    ★ 2026-11-02 起 ``get_items`` **不再原样透传**：它按最新一条压缩记录算出一份
    "摘要 + 未被覆盖的原文"。这是全模块**唯一**的投影点 —— 库里那份日志始终完整，
    压缩只追加记录、从不清空。

    ⇒ "本轮正在往会话里写条目"与"后台算出新的覆盖边界"因此排队，不会出现
    "边界盖住了刚写进来的那一轮，而摘要里没有它"。

    ⚠️ 进程内锁挡不住**跨副本**：两个副本同时压同一条会话，可能各写一条记录。
    后果比以前轻 —— 不再有"清空"这种不可逆动作，最坏是多一条摘要记录（记录表上的
    部分唯一索引会让后写的那条把前一条标成 superseded）。跨副本锁不在本模块范围内。
    """

    def __init__(self, inner: Any, store: Any = None) -> None:
        self._inner = inner
        self._lock = asyncio.Lock()
        # 压缩记录存放在哪。没给 = 进程内（测试与无库降级），给了 = PG。
        self._store = store if store is not None else InMemoryCompactionStore()
        # ⚠️ SDK 会 `getattr(session, "session_settings", None)` 取会话设置
        # （`run_internal/session_persistence.py`），拿不到就**静默**改用默认值；
        # `session_id` 同理（`SessionABC` 的公开属性）。不代理这两个，装饰器就会
        # 悄悄改变会话行为，且没有任何报错。
        self.session_id = getattr(inner, "session_id", "")
        self.session_settings = getattr(inner, "session_settings", None)

    # -- 协议方法 ----------------------------------------------------------
    async def get_items(self, *args: Any, **kwargs: Any) -> Any:
        """读历史 = 库里那份 + 最新压缩记录 ⇒ 投影。

        参数原样透传（官方会传 ``limit``）。没有记录时**完全走原路**，连一次多余的
        全量读都不做 —— 压缩关着或从没压过时，这条路径与改造前逐字节相同。
        """
        record = await _latest_record(self._store, self.session_id, stage="project")
        if record is None:
            return await self._inner.get_items(*args, **kwargs)
        # 有记录 ⇒ 必须拿全量才能按"前 N 条"切。``limit`` 不再透传：投影后的条数
        # 已经不等于库里的条数，把 limit 交给内层会让"摘要 + 尾部"被截在半路。
        history = list(await self._inner.get_items() or [])
        return project_history(history, record)

    async def read_raw(self, start: int = 0, end: int | None = None) -> list[Any]:
        """按**库里的下标**读回原始条目（压缩记录覆盖过的那段也能读）。

        投影是默认视角（``get_items``），这是"摘要里找不到细节时回原文"的唯一入口。
        压缩记录里的 ``covered_items`` 就是这套下标的刻度。

        ⚠️ 不做任何裁剪：调用方要自己控制范围。把整段历史一次性塞回上下文，
        等于把压缩刚省下的 token 又还回去 —— 而且这一轮的成本会高出好几倍。
        """
        history = list(await self._inner.get_items() or [])
        return history[start:end]

    async def add_items(self, *args: Any, **kwargs: Any) -> Any:
        async with self._lock:
            return await self._inner.add_items(*args, **kwargs)

    async def pop_item(self, *args: Any, **kwargs: Any) -> Any:
        async with self._lock:
            return await self._inner.pop_item(*args, **kwargs)

    async def clear_session(self, *args: Any, **kwargs: Any) -> Any:
        async with self._lock:
            return await self._inner.clear_session(*args, **kwargs)

    # -- 压缩入口 ----------------------------------------------------------
    async def run_compaction(self, args: Any = None) -> None:
        """执行一次压缩（**与官方同名**）。

        ⚠️ 名字照抄官方，但**当前不会被 SDK 调用**：SDK 的调用点是
        ``if response_id and is_openai_responses_compaction_aware_session(session)``，
        而本项目走 Chat Completions ⇒ ``response_id`` 恒为空（一手依据见模块
        docstring）。保留这个名字是为了"将来真换到 Responses 接口时形状不用改"。

        本项目的触发由两个固定入口自己做（``compact_session_history`` 与
        ``compact_for_window``），它们都最终走到这里。
        """
        await compact_session_history(self.session_id, session=self)


async def compact_session_history(session_id: str, *, session: Any = None,
                                  force: bool = False) -> int:
    """**入口一（会话侧）**：给某个会话算一次新的覆盖边界。返回这次新覆盖的条数。

    ★ 它**不改会话日志**，只追加一条压缩记录。名字保留是因为调用点与语义没变
    （"把老历史压下去"），但落点从"改写库"换成了"记下压到哪"。

    调用点：``app/main.py`` 的 ``_stream_run`` 开头 ``asyncio.create_task`` ——
    **后台跑，不挡本轮**。所以：

    * 本轮读到的仍是**压缩前**的视图，本轮行为与今天完全一样；
    * **从下一轮开始**读到的才是压过的。

    返回 0 的情形（都不是错误，且都**必须**保持"什么都没做"）：
    开关关闭 / 没有会话 / 会话读不出来 / 需要保留的轮数还没攒够 / 上一代已盖到这里 /
    摘要为空 / 摘要期间历史变了 / **摘要没有比被取代的那段更短**（见 ``_compact_once``）。
    """
    if not history_compact_enabled():
        return 0
    if session is None:
        if not session_id:
            return 0
        try:
            from .session import session_for  # 惰性：避免与 session.py 形成循环导入

            session = session_for(session_id)
        except Exception as e:  # noqa: BLE001  后端不可用 ⇒ 与"没有历史"等价
            _note_failure(e, stage="session_for")
            return 0
    if session is None:
        return 0
    inner = getattr(session, "_inner", session)
    lock = getattr(session, "_lock", None)
    store = getattr(session, "_store", None)
    if lock is None:
        # 没被装饰过的裸会话（测试 / 别的调用方）：自己起一把锁，只为串行化本函数
        # 自己的两次调用，不与 SDK 的写入互斥 —— 那种情况下调用方本就该自己保证。
        lock = asyncio.Lock()
    return await _compact_once(inner, lock, session_id=session_id or "",
                              keep=keep_rounds(), stage="session", store=store,
                              force=force)


async def _compact_once(inner: Any, lock: asyncio.Lock, *, session_id: str,
                        keep: int, stage: str, store: Any = None,
                        force: bool = False) -> int:
    """读日志 → 摘要（锁外）→ 重读 → **写一条压缩记录**，返回这次新覆盖的条目数。

    会话日志**一个字节都不动**。这是 2026-11-02 改造的全部要点：摘要漏掉的细节、
    长期记忆的素材、以及"当时到底是怎么说的"，都还在库里。投影负责把记录变成
    模型看到的那一份。

    为什么摘要在**锁外**：写摘要是一次模型调用（秒级）。放在锁内会让本轮的
    ``add_items`` 一起排队等它 —— 那等于把压缩的成本转嫁到用户身上。

    **变了就放弃本轮**：摘要算完、拿到锁之后要**重新读一次**日志，并要求边界没有
    往外推。否则说明这期间有新轮次落进来 —— 照写不误的后果是"记录的 covered_items
    盖住了新条目、摘要里却没有它"，投影会把刚写的那一轮**藏掉**，而且不报错。

    **摘要必须真的更小**：比被取代的那段（含上一代摘要）还长就放弃。那种替换除了
    丢信息什么也没换来，而且下一轮会反复压同一段、每轮多花一次模型调用。
    """
    if store is None:
        store = InMemoryCompactionStore()
    async with _lock_or_none(lock):
        try:
            history = list(await inner.get_items() or [])
        except Exception as e:  # noqa: BLE001  读不出来 ⇒ 什么都不做
            _note_failure(e, stage=f"{stage}_read")
            return 0
        keep_from = _plan_tail(history, cap=keep,
                               budget=0 if force else tail_budget())
        if keep_from is None:
            return 0
        prior = await _latest_record(store, session_id, stage=f"{stage}_record_read")
        # 上一代已经盖到哪儿。它之前的原文**不再**交给摘要模型：那些内容已经浓缩在
        # 上一代摘要里了，再读一遍等于每代都把全量历史重读一次。
        baseline = int(prior.get("covered_items") or 0) if prior else 0
        if keep_from <= baseline:
            return 0
        summarized_upto = keep_from

    segment = _segment_with_prior(history[baseline:keep_from], prior)
    summary = await _summarize_async(segment)

    async with _lock_or_none(lock):
        if not summary:
            return 0
        try:
            fresh = list(await inner.get_items() or [])
        except Exception as e:  # noqa: BLE001
            _note_failure(e, stage=f"{stage}_reread")
            return 0
        new_keep_from = _plan_tail(fresh, cap=keep,
                                   budget=0 if force else tail_budget())
        if new_keep_from is None or new_keep_from > summarized_upto:
            # 期间有新轮次落进来（边界往外推了）⇒ 这次摘要覆盖不到它，放弃。
            observability.counter("agent_history_compaction_skipped_total", reason="moved")
            logging.info("历史压缩放弃本轮：摘要期间历史变了（边界 %d → %d）",
                         summarized_upto, new_keep_from)
            return 0
        shadowed = sum(history_budget._item_tokens(x)
                       for x in fresh[baseline:new_keep_from])
        if prior:
            # 上一代摘要也在被取代之列。不算它的话，"新旧摘要一样大"会被当成有效
            # 压缩，于是每一轮都白压一次，摘要还会一代代变长。
            shadowed += int(prior.get("summary_tokens") or 0)
        summary_tokens = count_tokens(summary)
        if summary_tokens >= shadowed:
            observability.counter("agent_history_compaction_skipped_total", reason="no_gain")
            logging.warning("摘要没有更短（%d ≥ %d token）⇒ 这轮不压，避免白花一次调用",
                            summary_tokens, shadowed)
            return 0
        try:
            await store.record(
                session_id, covered_items=new_keep_from, summary=summary,
                summary_tokens=summary_tokens, shadowed_tokens=shadowed, scope="history",
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  记录写失败 ⇒ 这轮没压（日志没动过）
            _note_failure(e, stage=f"{stage}_record")
            return 0

    dropped = new_keep_from - baseline
    observability.counter("agent_history_compacted_total", scope=stage)
    observability.histogram("agent_history_compacted_items", dropped)
    logging.info(
        "历史压缩完成（%s）：新覆盖 %d 条（累计覆盖 %d 条，原文还剩 %d 条，会话=%s）",
        stage, dropped, new_keep_from, len(fresh) - new_keep_from, session_id or "-",
    )
    return dropped


class _lock_or_none:
    """``async with`` 一个**可能为 None** 的锁（没有锁时什么也不做）。

    为什么需要它：``asyncio.Lock`` 不是可重入的，而 ``_compact_once`` 里要分两段
    临界区（读 / 重读+替换），中间隔着一次模型调用。直接 ``async with lock``
    两次没问题（不是嵌套），但调用方可能没有锁（裸会话）—— 那时用一个空上下文
    比到处写 ``if lock:`` 清楚。
    """

    __slots__ = ("_lock",)

    def __init__(self, lock: asyncio.Lock | None) -> None:
        self._lock = lock

    async def __aenter__(self):
        if self._lock is not None:
            await self._lock.acquire()
        return self._lock

    async def __aexit__(self, *exc: Any) -> bool:
        if self._lock is not None:
            self._lock.release()
        return False


# ---------------------------------------------------------------------------
# 窗口压缩（入口二）
# ---------------------------------------------------------------------------
def _items_tokens_over(instructions: str, items: list[Any], trigger: int) -> bool:
    """将要送进模型的输入**是否超过触发线**。

    从**最新往老**累加、一旦越过 ``trigger`` 立刻返回 —— 更老的条目量不量都不影响
    结论，量它们纯属浪费（与 ``history_budget`` 的口径 4 同一条：历史是永久保留的，
    库里有多少就编多少 ⇒ 全量编码开销无上界）。所以本函数是 **O(触发线)** 的。
    """
    total = count_tokens(instructions or "")
    if total > trigger:
        return True
    for item in reversed(items):
        total += history_budget._item_tokens(item)
        if total > trigger:
            return True
    return False


async def compact_for_window(
    items: list[Any], *, instructions: str = "", on_usage: Any = None,
    goal: str = "",
) -> list[Any] | None:
    """**入口二**：将要送进模型的那一份输入超过阈值时，压成「摘要 + 能装下的轮」。

    返回**替换用**的条目列表；``None`` = 不用压（没开 / 没超 / 压了也没用）。

    on_usage: 摘要那次出站调用的 usage 回调（成本要进**这一次运行**的账，
        见 ``budget.note_bypass``）。

    goal: 这件事一开始要干什么（``TaskContext.goal``）。传下去是为了让摘要的
        第一项「任务与约束」**有据可依**，而不是让模型从被压掉的那段里反推 ——
        那段可能已经被上一次压缩换成了摘要，越往后的压缩越只能靠这里给。
        ⚠️ 它只影响摘要的**提示词**，不参与 ``_items_tokens_over`` 的判据：
        判据量的是"要送进模型的那份输入"，加一段提示词会让压缩更容易触发，
        而那与"窗口是不是真的快满了"无关。

    与"会话侧写回"的分工见模块 docstring 的"为什么窗口压缩不做写回"：这里只改
    **这一次调用送进模型的内容**，库里那份交给历史压缩。

    两条口径：

    * **装不下的轮从最老的开始被压**，最新那一轮无条件保留（同 ``_rounds_that_fit``）；
    * **压了没用就不压**：如果连"留 1 轮 + 摘要"都还在触发线以上，那这一侧的活
      已经到头了，交给 ``history_budget`` 的硬截断（那是确定性的兜底，不会失效）。
    """
    if not history_compact_enabled():
        return None
    trigger = window_compact_trigger()
    if trigger is None:
        return None
    payload = list(items or [])
    if not payload:
        return None
    if not _items_tokens_over(instructions, payload, trigger):
        return None

    # 保留段按**逐字预算**（默认预算的 16%）算，不是按触发线：触发线决定"压不压"，
    # 保留段决定"留多少"。两者共用一个数的话，触发线一调，保留段跟着变 —— 而它们
    # 回答的是完全不同的问题。
    keep = _rounds_that_fit(payload, budget=tail_budget() or trigger, cap=keep_rounds())
    if keep is None:
        # 最近 keep_rounds 轮本身装得下 ⇒ 老的那段不归这里管（历史压缩负责）。
        return None
    keep_from = _plan_keep_from(payload, keep=keep)
    if keep_from is None:
        return None
    summary = await _summarize_async(payload[:keep_from], on_usage=on_usage, goal=goal)
    if not summary:
        return None
    tail = payload[keep_from:]
    replacement = _session_replacement(summary, tail)
    observability.counter("agent_history_compacted_total", scope="window")
    observability.histogram("agent_history_compacted_items", keep_from)
    logging.info("窗口压缩完成：%d 条 → 1 条摘要 + %d 条（触发线 %d token）",
                 keep_from, len(tail), trigger)
    return replacement


def reset_compaction_state() -> None:
    """清掉进程内状态（**测试用**）：摘要 client 与"已打过失败日志"标记。"""
    global _client, _client_failed, _failure_logged
    _client = None
    _client_failed = False
    _failure_logged = False
