"""动作清单：把一次 agent 运行内部的每一步记下来（工具名 / 参数 / 成败 / 耗时）。

为什么必须有它
--------------
一个 agent 节点 = **一次完整的运行**。这次运行内部，模型可以来回最多
``AGENT_MAX_TURNS``（默认 12）次模型调用，每次可能配一次工具执行。这些来回
**全在节点内部** —— 图不介入、不落检查点。于是从外面只看得到"工具调了几次"，
看不到**调得对不对**。

后果很具体：某个工具三成的调用在报错时，模型会一直重试、答案质量与延迟一起
退化，而看板上呈现的是"工具调用次数正常、模型调用次数偏高" —— 指标会把人引向
提示词，而真凶是工具实现。这张清单就是"看得见中间步骤"的最小形态。

三条边界（都影响怎么用它）
--------------------------
1. **不进 LangGraph state**：挂在 ``TaskContext`` 上（与 ``AgentStats`` 同一条，
   理由见 ``context.AgentStats`` 的 docstring）。它**不需要跨轮延续** —— 每一步
   都属于它自己那一次运行。
   ⚠️ 一次请求会跑**多个** agent 节点（前台 + 文学），而每个节点各建一份
   ``TaskContext`` ⇒ **清单不跨节点**。要"本轮全部步骤"得把几份清单拼起来；
   指标不受影响（它们是全局累加的）。
2. **钩子之间没有共同的关联编号**：``on_tool_start`` / ``on_tool_end`` 都**不带参数**、
   也不带定位编号，参数只能从模型响应那一侧取。所以「参数」与「结果」的配对只能按
   「同名工具 + 先来先配」做；**并发执行同名工具时可能配错**。这对"这个工具失败了
   几次"（按工具名统计）没有影响，但"哪个参数导致了失败"暂时配不准 ——
   已知限制，不要在这个基础上做因果推断。
3. **观测失败绝不影响业务**：这张清单的任何一步出问题，都只降级为"这一步没记上"。

重复计数（第二步接在这张清单上）
--------------------------------
同一个动作（工具名 + 规范化参数）出现第二次时，播一次
``agent_action_repeats_total{tool}`` —— 这是"原地打转"**唯一**能被看见的地方：
打转时**总工具次数不高、重量级动作也不多**（模型在两个动作之间来回试），
只有"同一动作反复做"能把这种情况暴露出来。

清单侧对应 ``StepLog.repeat_counts()`` / ``StepLog.max_repeat``：下一次反思闸门
直接读它判"该不该介入"，不必另建一份重复状态 —— 两处分别维护一定会漂。

震荡（两个动作来回）—— 2026-10-21 补
--------------------------------------
重复计数有一个**结构性的盲区**：模型在两个动作之间来回试（``A→B→A→B``）时，
每个动作各做了两次，两个计数都到不了"三次"的阈值 ⇒ 看板安静，闸门也不介入。
而它恰恰是最典型的打转形态之一 —— 换一个工具再试同一个思路，比原样重试更像
"真的在想办法"，所以更不容易被人肉眼发现。

判据是**尾部周期重复**（``StepLog.tail_cycle``）：取已定格动作的指纹序列，看末尾
是否存在长度 2 或 3 的模式连续出现 ≥2 次。为什么只到 3：更长的周期在 12 圈的运行里
凑不齐两轮，写进去只会是一条永远不触发的判据（"配了但永不生效"比没有更难发现）；
而"相同结果的重试"在实践中几乎都落在 1~3 个动作的循环里。

指标 ``agent_action_oscillation_total{period}`` 的口径是"**每多转一整圈记一次**"，
不是"震荡事件数"：``A→B→A→B`` 记 1，接着又 ``A→B`` 记 2。这样 ``sum`` 直接读出
"多转了几圈"，比只报"发生过一次"更容易定阈值。

按**工具名**的失败计数（2026-10-21 补的第三个盲区）
-------------------------------------------------
前两个判据（重复 / 震荡）都用「工具名 + 规范化参数」的指纹，于是它们都看不见
**"一个工具坏了、模型换着参数试了八次"**：八次不同参数就是八个不同指纹，各一次，
两条判据都到不了阈值。

而它烧掉的时间和"原样重试"一模一样。所以另起一条只看**工具名**的计数
（``StepLog.fail_count``），到 ``AGENT_TOOL_FAIL_LIMIT`` 次就把回给模型的文案换成
"别再用它了"（``_GIVE_UP_MESSAGE``）。三者判据各不相同、互不替代：

* ``repeat_counts``  —— 同一个动作做了几次（原样重试）；
* ``tail_cycle``     —— 两个/三个动作来回（换着试同一个思路）；
* ``fail_count``     —— 这个工具还能不能指望（换参数试同一个坏工具）。

两个"看板上必然看不见"的缺口（2026-10-19 补）
---------------------------------------------
1. **一个从没被选过的工具，在指标里根本不存在。** 计数器只在被调用时产生时间序列，
   所以"挂了但一次没用"这件事在 ``/metrics`` 里是**空白**，而不是 0 —— 空白与
   "这个部署根本没有这个工具"读起来一模一样。补法是另计一条**曝光**：每次 agent
   开始运行，把它挂的每个工具各记一次（``agent_tool_exposed_total{tool}``）。于是
   选中率 = 结果数 / 曝光数，而"一次没选过"表现为**曝光有值、结果没有**。
2. **失败只有一档，但两种失败的处置完全相反。** 模型把参数给错（JSON 不合法、
   缺必填、类型不对）要改的是**工具描述或参数结构**；工具实现抛异常要改的是
   **代码或依赖**。两者都落成 ``status="failed"`` 时，看板只会告诉你"这个工具爱
   报错"，而值班的人无从判断该动哪一边。所以再分一档 ``reason``：

   * ``bad_args`` —— 模型给的参数不合法（``ModelBehaviorError`` / 校验失败）；
   * ``error`` —— 工具跑起来之后失败（实现 bug、外部服务不可用、磁盘问题）。

   ⚠️ 判据必须看**异常类型**，不能看错误文本 —— 文本是官方默认实现的产物，版本一升
   就可能变，而"文本里有没有某个词"这种判据会在升级后静默全判成 ``error``。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .. import observability
from ..envutil import env_int

__all__ = [
    "StepLog",
    "ToolContractError",
    "ToolStep",
    "calls_from_response",
    "fail_limit",
    "steps_enabled",
    "tool_error_handler",
]

try:  # pragma: no cover - SDK 缺失时退化成普通异常（项目整体依赖它，这里只是不添硬依赖）
    from agents.exceptions import ModelBehaviorError as _ModelBehaviorError
except Exception:  # noqa: BLE001
    _ModelBehaviorError = Exception  # type: ignore[assignment,misc]


class ToolContractError(_ModelBehaviorError):  # type: ignore[valid-type,misc]
    """模型给的输入**不满足工具自己的契约**（空内容、多给了不认识的字段……）。

    为什么必须抛，不能 ``return`` 一句"失败"
    ---------------------------------------
    工具的返回值在步骤清单里一律算 ``ok``。于是"落盘被拒"会被记成做成了：
    ``fallback`` 的已完成清单会把它列上去，兜底那边看到的是"这一步成了"。
    失败只发生在**用户打开文件的那一刻** —— 那是这一整套防护要消灭的形态。

    为什么继承 ``ModelBehaviorError``
    -------------------------------
    1. **归因正确**：这是模型填错了，不是实现坏了 ⇒ ``_failure_reason`` 按异常类型
       自动判成 ``bad_args``（它本来就认这个类型，不需要另外登记一档）。
    2. **不崩整轮**：官方把任何 ``Exception`` 都先交给工具的失败处理函数
       （``maybe_invoke_function_tool_failure_error_function``），转成一段文案退回
       模型重试。换成裸 ``Exception`` 也一样能接住，但会归到 ``error`` 档，让看板把
       模型的输入错误报成工具实现坏了。

    ⚠️ 消息里必须写清「哪里不对 + 该怎么改」：官方默认文案会把 ``str(error)`` 原样
    拼进去，那是模型唯一能看到的线索，也是它能一次改对的前提。
    """

_MAX_DETAIL = 200
"""结果 / 错误摘要的长度上限。只记**摘要**，原文不进日志（与项目口径一致）。"""

_CYCLE_PERIODS = (2, 3)
"""判"来回震荡"时检查的模式长度。

只到 3 的理由见模块 docstring：更长的周期在一次最多 ``AGENT_MAX_TURNS``（默认 12）
圈的运行里凑不齐两轮，写上就是一条永不触发的判据 —— 而"配了但不生效"比没有更难发现。
"""

_TIMEOUT_MESSAGE = (
    "工具 {tool} 这一步超时了：已经放弃等待，没有拿到结果。\n"
    "可以换一种做法：把范围缩小（更具体的关键词、更少的条数）、换一个工具，"
    "或者说明这一步做不到，先把其余部分完成。\n"
    "注意：超时只表示「没等到结果」，不表示「没执行」。如果这一步会留下东西"
    "（写文件、生成图片），它可能已经成功了，先确认再决定要不要重做。"
)
"""单步超时时回给模型的话。

为什么**不复用**官方默认文案：默认实现给模型的是一句没有出路的报错，模型拿到之后
最常见的两个反应正好都是坏的 —— 原样重试（打转，重复计数随即上升，最后被刹车
停掉）或者放弃整件事（一个本可绕过的步骤变成整单失败）。这里直接把"可以怎么做"
写进上下文，才是"注入错误上下文、让 agent 自我修复"。

第二段那句「不表示没执行」不是客套：超时停不掉已经在跑的线程，落盘类动作可能在
超时之后**照样完成**。不写这一句，模型会重试一遍，文件就被写两遍。
"""

_MODEL_ERROR_DETAIL_MAX = 300
"""回给模型的错误细节长度上限（字符）。

异常消息本来就该简短；超过这个长度基本只有一种情况 —— 有人在里面塞了一整段
payload 或服务端响应体。截断而不是整段回给模型，既省窗口也降低"把不该给的东西
带出去"的概率。
"""

_MODEL_ERROR_MESSAGE = (
    "工具 {tool} 这次没成功：{detail}\n"
    "按这个原因改一下再试；如果改不了，就换一种做法，或说明这一步做不到。"
)
"""一般工具失败时回给模型的话。

⚠️ 为什么不直接转调官方 ``default_tool_error_function``
------------------------------------------------------
openai-agents **0.23.0** 的 ``#5112 redact default tool failure details`` 把它改成了
"固定一句话、刻意不暴露异常"：

    "An error occurred while running the tool. Please try again."

那对**通用 SDK** 是合理的（它兜的是任意异常，可能带 HTTP 头、token、服务端回显）。
但对本项目，它把模型唯一能拿到的线索抹掉了：模型分不清"除数是零"与"参数名写错"，
只能原样重试 —— 于是 `AGENT_TOOL_FAIL_LIMIT` 的到线劝退与震荡检测被触发，
一个一次就能改对的失败升级成"这轮做不成"。

官方对这条收紧给出的出路就是本函数（PR #5112 原文）：

> "Applications can continue to provide approved error details through a custom
>  ``failure_error_function``."

放行边界见 ``_detail_of``：只放行**我们自己抛的业务异常**的消息，并且过一遍
``redact``。
"""

_GIVE_UP_MESSAGE = (
    "工具 {tool} 在这一轮里已经失败 {count} 次了。**不要再调用它**。\n"
    "换个参数重试同一个已经坏掉/用不上的工具，只会继续烧时间，不会得到新结果 ——"
    "前面那几次失败里没有一次是因为参数写法不同。\n"
    "现在可以：① 换一条路拿到需要的东西（换一个工具，或把范围缩到最小再试一次）；"
    "② 直接说明这一步做不到，用已经拿到的材料把其余部分做完；"
    "③ 如果这一步是整件事的前提，就停在这里并说清卡在哪里。"
)
"""同一个工具失败到上限时回给模型的话（``AGENT_TOOL_FAIL_LIMIT``）。

为什么需要这一档、而重复计数不够
--------------------------------
重复计数与震荡判据的指纹是「工具名 + **规范化参数**」——它们回答的是"同一个动作
做了几次"。而"一个工具坏了，模型换着参数试了八次"在这两个判据下**完全隐形**：
八个不同参数就是八个不同指纹，各出现一次，两个判据都到不了阈值。

这正是 ``toolpool`` 里"不新增熔断"那条决定没有覆盖的那一格：那条决定的理由是
"指纹重复会上升、会被反思接住"，而那个链条只在**原样重试**时成立。

处置上刻意**不做**两件事：

* **不把工具从集合里摘掉** —— 工具集是 ``Agent.tools``（构造期绑定），运行中改它
  等于改模型看到的世界，而且改坏了没人能发现；回一句"别再用它"效果相同、可逆。
* **不阻断运行** —— 这一步失败不等于整件事做不成，硬停会把一个可绕过的问题
  升级成整单失败。真正的"停"留给第 3 级刹车（那里有整套归因与告警）。
"""


def steps_enabled() -> bool:
    """工具步骤记录的总开关（**默认开**）。

    为什么"新能力默认可关"这条惯例在这里不适用：它是**纯观察** —— 不改任何对外
    可见行为。工具失败时回给模型的文本仍由官方默认实现产出（见
    ``tool_error_handler``），指标走旁路、失败只降级为 debug 日志。所以"关掉时与
    今天一致"这条它天然满足，而默认关掉等于"这个指标永远没有数据"。

    仍然**可关**：``AGENT_TOOL_STEPS=0``。要再加一层开关或改默认值，落点就在这里。
    """
    return bool(env_int("AGENT_TOOL_STEPS", 1, lo=0, hi=1))


def fail_limit() -> int:
    """同一个工具失败几次之后，改回一句"别再用它"（``0`` = 不启用）。

    默认 3：一次失败可能是参数写法问题（换一个参数确实能成），两次也还可能是
    偶发（外部服务抖了一下）；到第三次还不行，基本就是"这个工具此刻用不了"。
    再往上就是纯烧时间 —— 而它还不会被重复判据看见（理由见 ``_GIVE_UP_MESSAGE``）。

    ⚠️ 判据是**工具名**，不是"工具名 + 参数"。这一点与重复计数**刻意不同**：
    重复回答"同一个动作做了几次"，这里回答"这个工具还能不能指望"。用指纹会让
    "换参数重试同一个坏工具"重新变得隐形，而那正是这条要治的形态。
    """
    return env_int("AGENT_TOOL_FAIL_LIMIT", 3, lo=0)


def _fingerprint(name: str, arguments: Any) -> str:
    """把「工具名 + 参数」算成一个短指纹（用于发现同一动作被重复调用）。"""
    # 参数可能是**字符串**（模型给的原始 JSON）或已经是 dict —— 两种都要能规范化。
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments or {}, ensure_ascii=False)
    try:
        canonical = json.dumps(
            json.loads(raw or "{}"),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        canonical = (raw or "").strip()      # 参数不是合法 JSON 时按原串规范化
    return hashlib.sha1(f"{name}|{canonical}".encode()).hexdigest()[:12]


def _clip(value: Any) -> str:
    """压成一行并截断（要的是摘要，不是原文）。"""
    return " ".join(str(value).split())[:_MAX_DETAIL]


def _failure_reason(error: Any) -> str:
    """把一次失败分成 ``bad_args`` / ``timeout`` / ``error`` 三档（判据是**异常类型**）。

    为什么非要分开：这几种失败的处置**不同**。``bad_args`` 说明模型照着工具定义
    也填不出合法参数 ⇒ 要改的是**描述或参数结构**（例如一个参数名有歧义、一个本该
    有默认值的字段被 strict 模式变成了必填）；``error`` 说明参数是对的、工具跑起来
    之后才坏 ⇒ 要查的是**实现或依赖**；``timeout`` 说明工具既没报参数错、也没报
    异常，只是**在预算内没返回** ⇒ 要动的是超时预算、检索/落盘的数据量，或者那个
    工具依赖的外部服务。混成一档时看板只说"这个工具爱报错"，值班的人得先去翻日志
    才知道该动哪一边。

    判据用类型而不是文本，有两个具体理由：错误文本是官方默认实现的产物（``clip``
    之后还要截断），而且按关键词匹配会在 SDK 升版后**静默全判成 error** —— 那正好
    让这个分类失去意义，且不会有任何东西报错。

    ⚠️ 已知的粗糙点：``as_tool`` 包出来的专家工具，失败可能是**子 agent 自己的
    结构化输出解析不出来**（同样抛 ``ModelBehaviorError``），这里会归到
    ``bad_args``，而实际要动的是子 agent 的输出契约。判断"参数错 vs 执行错"所需的
    信息（工具函数体有没有开始执行）在错误处理函数这一侧拿不到 —— 如实记在这里，
    不要在这个基础上做"谁的责任"的归因。
    """
    # 超时**优先判**：它是 `toolpool` 抛出来的 ``TimeoutError``，与参数无关，也不
    # 说明实现有 bug。放在最前面是因为它和下面两档都不同源，混进去就等于把
    # "预算不够"报成"代码有问题"——那是两种完全不同的处置。
    if isinstance(error, TimeoutError):
        return "timeout"
    try:
        from agents.exceptions import ModelBehaviorError
    except Exception:  # pragma: no cover  SDK 缺失时按执行错处理
        return "error"
    if isinstance(error, ModelBehaviorError):
        return "bad_args"
    try:
        from pydantic import ValidationError
    except Exception:  # pragma: no cover
        return "error"
    if isinstance(error, ValidationError):
        return "bad_args"
    return "error"


def calls_from_response(response: Any) -> list[tuple[str, str, str]]:
    """从一次模型响应里取出"模型请求了哪些工具" ⇒ ``[(工具名, 参数, 定位编号)]``。

    ⚠️ 判据用**属性存在性**（有名字、且 ``arguments`` 不是 None），刻意不用
    ``isinstance`` 判类型：本项目走的是对话补全接口，转换层给出的形态与 Responses
    接口那一侧不完全一致，按类型判会**静默漏掉全部调用**（表现为"一步都没记上"，
    且没有任何报错）。文本消息项没有 ``arguments``，自然被排除在外。
    """
    out: list[tuple[str, str, str]] = []
    for item in getattr(response, "output", None) or []:
        name = getattr(item, "name", None)
        arguments = getattr(item, "arguments", None)
        if not name or arguments is None:
            continue
        out.append((str(name), str(arguments), str(getattr(item, "call_id", "") or "")))
    return out


def _log_of(ctx: Any) -> StepLog | None:
    """从错误处理函数收到的 ctx 取清单。

    走**鸭子类型**而不是 ``isinstance(ctx.context, TaskContext)``：``steps`` 被
    ``context`` 导入，再反过来导入 ``context`` 就是循环导入。
    """
    log = getattr(getattr(ctx, "context", None), "steps", None)
    return log if isinstance(log, StepLog) else None


def tool_error_handler(tool_name: str):
    """给一个工具造"既保住自愈、又记一条失败"的错误处理函数。

    为什么必须换掉官方默认实现，而不是只接 ``on_tool_end``
    ------------------------------------------------------
    工具抛异常时，官方**先把异常转成一句给模型看的话**，钩子拿到的已经是那句话 ——
    实测：失败路径 ``on_tool_end`` **照样触发**，它的 ``result`` 就是那句错误文本。
    所以从钩子侧**分不出**"真的成功了"与"收到一句错误文本"。

    为什么**不再**转调 ``default_tool_error_function``（2026-11-06 改）
    -----------------------------------------------------------------
    此前这里最后一行的写法是 ``return default_tool_error_function(ctx, error)``，
    理由是"回给模型的文本与官方逐字节一致，自愈能力完全不受影响"。**那个理由在
    openai-agents 0.20 成立，0.23 起不成立了。**

    官方 0.23.0 的 ``core: redact default tool failure details (#5112)`` 把默认实现
    从「拼上 ``str(error)``」改成了「固定一句话、刻意不暴露异常」：

        # 0.20
        f"An error occurred while running the tool. Please try again. Error: {str(error)}"
        # 0.23+（原文注释：Return a fixed error response without exposing the exception）
        "An error occurred while running the tool. Please try again."

    ⇒ 继续转调它，模型收到的就是**一句没有任何信息量的话**：不知道是除数是零、
    参数不认识，还是路径越界。它唯一能做的动作是**重试同一个动作** —— 于是
    ``fail_limit`` 的"到线劝退"与震荡检测会被触发，一个本来一次就能改对的失败
    升级成"这轮做不成"。**这不是变慢，是这条能力没有了。**

    官方给的出路正是这条（PR #5112 原文）：

    > "Applications can continue to provide approved error details through a
    >  custom ``failure_error_function``."

    我们本来就有自定义函数，只是把工作**转交**出去了。现在自己产出。
    「approved」的边界很清楚：这里的异常**全部是本项目工具自己抛的业务异常**
    （``ToolContractError`` / 参数校验 / 路径越界 / 超时），不含任何第三方响应体
    或用户数据 —— 详细说明见 ``_MODEL_ERROR_MESSAGE`` 那条的实现注释。

    ⚠️ 回调签名是 ``(ctx, error)``，**没有工具名形参** ⇒ 工具名必须在**构造期**用
    闭包捕获（``tool_error_handler("kb_search")``），不要试图从回调参数里取。
    """

    def handler(ctx: Any, error: BaseException) -> str:
        log = _log_of(ctx) if steps_enabled() else None
        if log is not None:
            log.fail(tool_name, error)
        # 失败次数到线 ⇒ 改成"别再用它"（判据是**工具名**，理由见 ``_GIVE_UP_MESSAGE``）。
        # ⚠️ 只在**刚好到线**那一次记指标：之后每一次调用都 >= 线，按 >= 记会让这条
        # 曲线等于"坏工具被调了几次"，而它要回答的是"多少个工具被劝退了"。
        limit = fail_limit()
        if log is not None and limit > 0:
            count = log.fail_count(tool_name)
            if count >= limit:
                if count == limit:
                    observability.counter("agent_tool_give_up_total", tool=tool_name)
                    logging.warning(
                        "工具 %s 本轮已失败 %d 次 ⇒ 回给模型的文案改为「不要再用它」",
                        tool_name, count,
                    )
                return _GIVE_UP_MESSAGE.format(tool=tool_name, count=count)
        if isinstance(error, TimeoutError):
            # 超时**不能**直接套官方默认实现：它给模型的是一句没有出路的报错，
            # 而这里要的是「注入错误上下文让模型自己修」—— 说清"可以怎么做"，
            # 以及那件最容易被忽略的事实：超时只是放弃等待，**动作可能已经发生**。
            # 少了后半句，模型会当成"失败了"直接重试，于是同一个文件被写两遍。
            return _TIMEOUT_MESSAGE.format(tool=tool_name)
        return _MODEL_ERROR_MESSAGE.format(tool=tool_name, detail=_detail_of(error))

    return handler


def _detail_of(error: BaseException) -> str:
    """从异常里取一段**能给模型看**的说明。

    ⚠️ 这里是"哪些信息可以放行"的唯一判定点，改动前先读下面两条。

    放行的理由：本项目工具抛出的异常**全部是自己构造的业务异常**，消息内容是我们
    自己写的（"除数是零"、"目录外面"、"不支持这个函数"这类），不含第三方响应体、
    不含用户输入原文、不含凭据。对比一下 openai-agents 收紧的那个场景 —— 那里
    兜的是**任意**异常（可能是 SDK/HTTP 层抛出的，``str(error)`` 里可能带请求头、
    token、服务端回显）。两者的信任边界不同，不能因为名字一样就套同一套规则。

    ⚠️ **这里刻意不做脱敏**（2026-11-06 定）
    --------------------------------------------------
    `app/logging_setup.py::redact` 那套是给**日志落盘**用的，不能拿到这里来：
    ① 架构上 `src/` 不许反向依赖 `app/`（守卫：
       `tests/test_index_identity.py::test_rag_layer_never_imports_app_layer`）；
    ② 语义上也不该做 —— 这条字符串的收件人是**模型**，它本来就是"把内部原因告诉
       模型"的通道。如果某个工具把凭据写进了异常消息，那是**抛出点的 bug**，
       应该在抛出点修；在出口处做一层会掩盖它，还让"到底泄没泄"变得说不清。
        （与 §16 工具契约的口径一致：契约问题在边界上报错，不在出口处静默修正。）

    取不到消息（``str(error)`` 为空）时退回异常**类名** —— 至少让模型知道
    失败类型，比"未知错误"有用。
    """
    raw = str(error).strip()
    if not raw:
        return type(error).__name__
    # 模型吃不下超长文本；异常消息本来就该简短，超了就是有人在里面塞了 payload。
    return raw if len(raw) <= _MODEL_ERROR_DETAIL_MAX else raw[:_MODEL_ERROR_DETAIL_MAX] + "……"


@dataclass
class ToolStep:
    """一个动作：模型请求调一次工具，以及它的结果。"""

    seq: int
    tool: str
    fingerprint: str = ""
    """「工具名 + 规范化参数」的短哈希 —— 重复计数的判据（见 ``StepLog.repeat_counts``）。"""
    call_id: str = ""
    """定位编号。模型请求时给、工具结果带同一个回来 ⇒ 将来"退回第几步"的定位键。"""
    status: str = "running"
    """``running`` / ``ok`` / ``failed``。"""
    reason: str = ""
    """失败档位：``bad_args``（模型给错参数）/ ``error``（工具跑起来之后失败）。

    只在 ``failed`` 时有意义；成功时留空。判据是**异常类型**，不是错误文本
    （理由见模块 docstring 末尾）。
    """
    detail: str = ""
    """结果或错误的**摘要**（截断，不是原文）。"""
    duration: float = 0.0
    started: float = field(default=0.0, repr=False)
    settled: bool = field(default=False, repr=False)
    """是否已定格并播报过（失败路径会先后被两个来源碰到，靠它防重复）。"""

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "tool": self.tool,
            "fingerprint": self.fingerprint,
            "call_id": self.call_id,
            "status": self.status,
            "reason": self.reason,
            "detail": self.detail,
            "duration": round(self.duration, 4),
        }


@dataclass
class StepLog:
    """一次运行的动作清单（挂在 ``TaskContext`` 上）。"""

    steps: list[ToolStep] = field(default_factory=list)
    _pending: list[tuple[str, str, str]] = field(default_factory=list, repr=False)
    """已从模型响应里登记、但**还没执行**的调用（``on_tool_start`` 到来时按名取用）。"""

    # -- 只读 --
    @property
    def failed(self) -> list[ToolStep]:
        """失败的步骤（兜底出口要用它列"未完成清单"，不要另写一份描述）。"""
        return [s for s in self.steps if s.status == "failed"]

    @property
    def max_repeat(self) -> int:
        """同一个动作被做过的最多次数（``0`` / ``1`` 都表示**没有任何动作重复**）。

        判"打转"的现成阈值就是它：反思闸门要的是"有没有一个动作被做过 N 次"。
        """
        return max(self.repeat_counts().values(), default=0)

    def repeat_counts(self) -> dict[str, int]:
        """按指纹统计"同一动作做过几次"，**只列做过两次以上的** ⇒ ``{指纹: 次数}``。

        为什么非要按**指纹**数而不是按工具名：同一个 ``kb_search`` 用不同关键词查十次
        是正常检索，用同一个关键词查十次才是打转。按工具名数只会把"查得多"误报成
        "卡住了"，而看板上它俩长得一模一样。
        ⚠️ 与播报同一口径：只数**已定格**的步骤（有结果才算"做过一次"），于是读出来的
        数与 ``agent_action_repeats_total`` 播报过的次数同源 —— 两处分别维护一定会漂。
        """
        counts: dict[str, int] = {}
        for step in self.steps:
            if step.settled and step.fingerprint:
                counts[step.fingerprint] = counts.get(step.fingerprint, 0) + 1
        return {fp: n for fp, n in counts.items() if n >= 2}

    def tail_cycle(self) -> tuple[int, int]:
        """尾部**周期重复** ⇒ ``(周期, 连续出现几次)``；没有则 ``(0, 0)``。

        为什么要单独一个判据：``repeat_counts`` 按指纹各数各的，``A→B→A→B`` 时
        A、B 各 2 次，都到不了"三次"的阈值 ⇒ 那条判据对这种打转**完全沉默**。
        而"换一个动作再试同一个思路"正是最容易被误认为"在想办法"的形态。

        判据只看**末尾**（不是整段里任意位置），理由是"要不要现在介入"只取决于
        "它此刻还在不在绕"：整段里出现过一次往返、之后已经走出来，那不该再报。

        ⚠️ 只数**已定格**的步骤，与 ``repeat_counts`` 同口径 —— 读出来的量与指标
        播报同源，两处分别维护一定会漂（漂的表现是"指标有数、闸门不动"）。
        ⚠️ 序列**遇到空指纹就停**（不是跳过）：空指纹意味着"认不出这一步是不是同一个
        动作"（参数没配上）。跳过它会让 ``A B C A B``（C 认不出）被读成 ``A B A B`` ——
        一条假的震荡比漏报更难查，因为它会把一条正常的多步流程标成打转。
        """
        tail: list[str] = []
        for step in reversed(self.steps):
            if not step.settled:
                continue
            if not step.fingerprint:
                break
            tail.append(step.fingerprint)
        fingerprints = list(reversed(tail))
        best = (0, 0)
        for period in _CYCLE_PERIODS:
            if len(fingerprints) < period * 2:
                continue
            last = fingerprints[-period:]
            if len(set(last)) < 2:
                # 周期内全是同一个动作（``A A A A``）不是"来回"，是纯重复 ——
                # 那由 ``repeat_counts`` 负责报。这里放过它，否则 A 做四次会被说成
                # "两个动作来回"，而看板上那两步长得一模一样，对不上。
                continue
            if fingerprints[-period * 2 : -period] != last:
                continue
            cycles = 2
            while len(fingerprints) >= period * (cycles + 1):
                previous = fingerprints[-period * (cycles + 1) : -period * cycles]
                if previous != last:
                    break
                cycles += 1
            if cycles > best[1]:
                best = (period, cycles)
        return best

    @property
    def oscillation_cycles(self) -> int:
        """尾部往复已经转了几整圈（``0`` / ``1`` = 没有震荡）。

        反思闸门读它判"该不该介入"（阈值 ``AGENT_REFLECT_ON_OSCILLATION``，默认 2 圈）。
        取圈数而不是布尔：阈值要能配成"转两圈才看"或"转三圈才看"，布尔值只能表达
        "发生过"，那个配置项就没有意义了。
        """
        return self.tail_cycle()[1]

    def fail_count(self, tool: str) -> int:
        """这个工具在本轮**已经失败过几次**（只看已定格的条目）。

        与 ``repeat_counts`` / ``tail_cycle`` 刻意用**不同**的判据：那两条按
        「工具名 + 规范化参数」的指纹数，回答"同一个动作做了几次"；这里只按
        **工具名**数，回答"这个工具还能不能指望"。

        为什么必须两种都有：模型"换参数重试一个坏工具"时，每次参数都不同 ⇒ 每次都是
        新指纹 ⇒ 两个指纹类判据到不了阈值，看板与闸门一起沉默。而它烧掉的时间与
        "原样重试"完全一样。

        ⚠️ 只数 ``failed``，不数 ``ok``：一个工具成功八次、失败一次，不构成"它坏了"。
        """
        return sum(
            1
            for step in self.steps
            if step.settled and step.status == "failed" and step.tool == tool
        )

    def as_list(self) -> list[dict]:
        return [s.as_dict() for s in self.steps]

    # -- 写入（钩子与工具错误处理函数调用）--
    def note_exposed(self, tools: list[str]) -> None:
        """登记"这些工具被摆到模型面前过一次"（每个工具各计一次）。

        为什么需要这条：结果计数器只回答"被选中的那几次"，而**没被选中的工具在指标里
        什么都不留**。于是"某个工具挂了三个月一次没用过"与"这个部署根本没有它"在
        ``/metrics`` 上完全一样 —— 都是空白。有了曝光计数，"一次没选过"才是一个可以
        直接查到的数（曝光有值、结果没有），选中率也才有分母。

        ⚠️ 粒度是"每次 agent 开始运行"，不是"每次模型调用"：同一个 agent 在一次运行里
        连跑几圈，工具集只摆出来一次（``on_agent_start`` 只在 agent 变化时触发）。这是
        想要的 —— 它回答的是"这套工具被交给模型多少回"，不是"模型有多少次机会选它"。

        去重放在**这里**，不放在调用方：这是唯一的播报点，任何调用方（含将来新加的）
        都自动受保护。两处各写一份去重一定会漂 —— 而漂的表现是分母偏大、选中率偏低，
        看板上看起来像"模型不太爱用这些工具"。
        """
        if not steps_enabled():
            return
        seen: set[str] = set()
        for name in tools:
            if not name or name in seen:
                continue
            seen.add(name)
            observability.counter("agent_tool_exposed_total", tool=name)

    def note_calls(self, calls: list[tuple[str, str, str]]) -> None:
        """登记"模型这次请求了哪些工具"（还没执行 ⇒ 先入队）。"""
        if not steps_enabled():
            return
        self._pending.extend(calls)

    def begin(self, tool: str) -> None:
        """某个工具开始执行。"""
        if not steps_enabled():
            return
        fingerprint, call_id = self._take_pending(tool)
        self.steps.append(
            ToolStep(
                seq=len(self.steps) + 1,
                tool=tool,
                fingerprint=fingerprint,
                call_id=call_id,
                started=time.perf_counter(),
            )
        )

    def finish(self, tool: str, result: Any) -> None:
        """某个工具执行结束。

        ⚠️ **失败路径也会走到这里**（官方把异常转成文本后照样触发钩子 —— 实现在
        SDK 的 ``run_internal/tool_execution.py``，实测过），所以成败**不在这里判**。

        判据只能是"**还有没有未定格的同名步骤**"：

        * 正常路径：``begin`` 刚建的那条未定格 ⇒ 在这里定格为 ``ok``；
        * 失败路径：``fail()`` 已经定格过了 ⇒ 这里**什么都不做**。若改成"补记一条"
          就会给一次失败**补出一条假的成功**，而这个清单的全部价值就是让失败可见 ——
          那个写法会自己把失败率抹平，且症状是"指标正常"，没有任何东西会报错；
        * 没有 ``begin`` 的 ``on_tool_end``（理论上不该有；实测 ``on_tool_start`` 总在
          参数解析之前触发）：**宁少记一条 ok，也不错记一条** —— 少一条只影响分母，
          错记一条会污染分子。
        """
        if not steps_enabled():
            return
        step = self._open(tool)
        if step is not None:
            self._settle(step, "ok", result)

    def fail(self, tool: str, error: Any) -> None:
        """某个工具失败（由 ``tool_error_handler`` 调用）。

        没有 ``begin`` 对应的失败（例如参数 JSON 解析失败：工具根本没执行）会**补记
        一条**，而不是丢掉 —— 那正是"调用错了但看不出来"的一类。
        """
        if not steps_enabled():
            return
        self._settle(
            self._open(tool) or self._append(tool), "failed", error, reason=_failure_reason(error)
        )

    # -- 内部 --
    def _append(self, tool: str) -> ToolStep:
        step = ToolStep(seq=len(self.steps) + 1, tool=tool)
        self.steps.append(step)
        return step

    def _open(self, tool: str) -> ToolStep | None:
        """最近一条同名、**还没定格**的步骤。"""
        for step in reversed(self.steps):
            if step.tool == tool and not step.settled:
                return step
        return None

    def _take_pending(self, tool: str) -> tuple[str, str]:
        """按「同名 + 先来先配」取一条待执行的调用信息 ⇒ ``(指纹, 定位编号)``。"""
        for index, (name, _, _) in enumerate(self._pending):
            if name == tool:
                _, arguments, call_id = self._pending.pop(index)
                return _fingerprint(tool, arguments), call_id
        return "", ""

    def _count_settled(self, fingerprint: str) -> int:
        """**已经定格**的同指纹步骤有几条（调用它的那条自己还没定格）。"""
        return sum(1 for s in self.steps if s.settled and s.fingerprint == fingerprint)

    def _settle(self, step: ToolStep, status: str, detail: Any, reason: str = "") -> None:
        """定格一条步骤并**播报一次**（重复调用直接返回，不与业务交互）。"""
        if step.settled:
            return
        # ⚠️ "这条是不是重复"必须在**设 settled 之前**判：此刻数到的同指纹步骤都是
        #    **之前**那几次。放到后面就会把自己算进去 ⇒ 每一次调用都被判成重复，
        #    "打转"指标直接等于总调用数，而症状是"指标看着有数、其实全是噪声"。
        #    空指纹（没配到参数的步骤）不参与：把它们算作"同一动作"会凭空造出重复。
        repeated = bool(step.fingerprint) and self._count_settled(step.fingerprint) > 0
        # 震荡同样必须在设 settled **之前**读一次尾部 —— 定格之后这一步就进了序列，
        # 只有"之前 / 之后"比较才知道这一拍有没有**多转一整圈**。
        cycle_before = self.tail_cycle()
        step.settled = True
        step.status = status
        step.reason = reason
        step.detail = _clip(detail)
        step.duration = max(0.0, time.perf_counter() - step.started) if step.started else 0.0
        # reason 只在失败时有内容，成功时是空串。多一个维度不会让既有查询失效
        # （`status="failed"` 仍然匹配全部失败），但能把"该改描述"与"该改代码"分开。
        observability.counter(
            "agent_tool_result_total", tool=step.tool, status=step.status, reason=step.reason
        )
        if repeated:
            observability.counter("agent_action_repeats_total", tool=step.tool)
        cycle_after = self.tail_cycle()
        if cycle_after[1] > cycle_before[1]:
            # 口径：**每多转一整圈记一次**（不是"震荡事件数"）。A→B→A→B 记 1，
            # 再来一轮 A→B 记 2 ⇒ `sum` 直接就是"多转了几圈"。
            observability.counter(
                "agent_action_oscillation_total", period=str(cycle_after[0])
            )
        if step.duration:
            observability.histogram(
                "agent_tool_duration_seconds", step.duration, tool=step.tool
            )
