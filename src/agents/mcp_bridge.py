"""MCP 接入与**内容转换层**。

它解决什么问题
--------------
官方对 MCP 的支持已经完整：把服务器对象塞进 ``Agent(mcp_servers=[...])``，SDK 会按
服务器的 ``inputSchema`` 生成 function tool，参数、描述、来源标记（``ToolOrigin``）
全都自动。**这一层不重造那部分**，只补官方刻意没做的三件事：

一、**返回内容没有任何长度上界**（``mcp/util.py`` 的 ``invoke_mcp_tool`` 末尾）：
    文本原样给模型；图片转成 ``data:image/png;base64,...`` 直塞；多块返回列表。
    一张普通截图 base64 之后几十万字符，两次就能把上下文窗口吃满。
    后果链是：顶爆窗口 → ``history_budget`` 从最老开始丢 → 丢的是用户原话 →
    模型"忘了自己在做什么"。而 ``toolpool.clip_result`` 只截 ``str``，这条完全绕过它。

二、**只校验"缺必需参数"，不校验"多给参数"**（``server.py`` 的
    ``_validate_required_parameters``）：多余字段原样转给服务器。
    本项目对自有工具的判据是"多给就抛 ``ToolContractError``、归因 ``bad_args``"，
    这里没有等价物 —— 但官方留了另一条路：``convert_schemas_to_strict``。

三、**单步超时语义不同**：``client_session_timeout_seconds`` 管一次会话读，
    不管"连接 + 重试"的总时长，因此**不能**替代 ``AGENT_TOOL_TIMEOUT_SECONDS``。

为什么插在 ``call_tool`` 而不是工具外壳
------------------------------------
工具调用的最外层就是失败处理本身（SDK 的 ``_FailureHandlingFunctionToolInvoker``）。
在外面再包一层，包装层抛出的异常会**绕过**失败处理、冒到运行层被当成致命错误 ⇒ 整轮崩。
``call_tool`` 是 SDK 自己调用的公开方法，所有工具自动经过它，失败处理链一行不动。

⚠️ 因此本模块的转换**只改内容，不改异常**：``call_tool`` 抛什么就让它抛什么，
失败交给 ``failure_error_function``（见 ``_mcp_error_handler``）。

生命周期：SDK 不会替你连接
--------------------------
``agent.py`` 的字段说明写得很明确：调用方必须自己 ``connect()``，用完 ``cleanup()``。
而且 stdio 的 connect 与 cleanup 必须在**同一个 task** 里 —— 这正是
``MCPServerManager`` 存在的原因（每个服务器一个 worker task）。
所以装配点在服务 lifespan：``async with MCPServerManager(...)``，只把**连接成功**的
那批挂给 agent（``active_servers``）。连不上的服务器如果留在列表里，
``list_tools`` 会在每一轮运行里抛异常 ⇒ 整轮崩，而不是"少一个工具"。

配置（全部走 ``src/`` 侧的直读约定，不经 ``app/config.py``）
-----------------------------------------------------------
= 见 ``.env.example`` 的 ``AGENT_MCP_*`` 一节。空 ``AGENT_MCP_SERVERS`` = 完全不接，
行为与接入前逐字节一致。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from agents.mcp import MCPServer, MCPServerStdio
from mcp.types import (
    AudioContent,
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
)

from .. import observability
from ..envutil import env_float, env_int
from .steps import tool_error_handler

__all__ = [
    "GuardedStdioServer",
    "MCPToolError",
    "attach",
    "build_servers",
    "configured",
    "convert_result",
    "images_mode",
    "result_max_chars",
    "server_specs",
    "timeout_seconds",
]


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def result_max_chars() -> int:
    """一次 MCP 调用**所有内容块合计**的字符上界。``0`` = 不限。

    为什么不复用 ``toolpool.result_max_chars()``：那个管的是"本项目工具返回的
    一个字符串"，坐标是**单值**；MCP 返回的是**块列表**（可能一文本一图片），
    坐标是**合计**。两者默认值相同（8000），但口径必须分开写 —— 合成一个的话，
    调 MCP 的上界会连带改掉检索工具的截断线，而那是两件不相干的事。
    """
    return env_int("AGENT_MCP_RESULT_MAX_CHARS", 8000, lo=0)


def images_mode() -> str:
    """图片怎么处理：``drop``（默认，降级成一句说明）或 ``keep``（原样给多模态模型）。

    ``drop`` 是默认值。理由不是"图片没用"，而是**成本不可预期**：服务器返回的图
    多大由对方决定，而 base64 会让字节数再涨三分之一；``keep`` 一次就可能吃掉
    整个窗口，随后的裁剪会丢掉用户原话（见模块说明的后果链）。
    ``keep`` 仍然受总字符预算约束（见 ``convert_result``），所以打开它不会失控，
    只是把"丢弃"换成"超预算时才丢弃"。
    """
    raw = (os.getenv("AGENT_MCP_IMAGES", "drop") or "").strip().lower()
    return "keep" if raw == "keep" else "drop"


def timeout_seconds() -> float:
    """一次 MCP 调用等待服务器响应的上界（秒）。``0`` = 不限。

    默认 30s：**比官方默认值（5s）宽得多**。理由是 MCP 服务器常是"起一个子进程
    再干活"（文件扫描、代码索引），冷启动本身就可能几秒；5s 会让"第一个请求"
    稳定失败、之后的都成功 —— 这种"只在部署后第一次出现"的故障最难查。
    """
    return env_float("AGENT_MCP_TIMEOUT", 30.0, lo=0.0)


def tools_warn_threshold() -> int:
    """单个服务器暴露的工具数超过这个值就告警。``0`` = 不告警。

    为什么是**告警**而不是**截断**：静默砍掉工具，模型会缺能力且完全不知道
    （它只看见"没有这个工具"），排障时也没有任何线索。工具太多真正伤害的是
    模型的注意力分配，那不是靠砍能解决的，得靠服务器自己收窄 —— 先把事实报出来。
    """
    return env_int("AGENT_MCP_TOOLS_WARN", 24, lo=0)


def cache_tools() -> bool:
    """是否缓存工具清单。默认**开**。

    ``Agent.get_all_tools`` 在**每一轮**运行里都会调 ``list_tools``；不缓存就是
    每一轮都往子进程要一次清单（一次完整往返）。服务器工具清单在进程生命周期内
    基本不变，所以默认缓存；要动态变化就设成 0。
    """
    return (os.getenv("AGENT_MCP_CACHE_TOOLS", "1") or "").strip() not in {
        "0",
        "false",
        "False",
        "no",
    }


def approve_names() -> set[str]:
    """需要人工审批的 MCP 工具名（``AGENT_MCP_APPROVE``，逗号分隔）。

    两种写法都认：裸工具名 ``write_file``，或限定服务器的 ``fs.write_file``。
    裸名会命中**所有**服务器上的同名工具 —— 这是刻意的：宁可多批一次，
    也不要因为"另一个服务器上也有个 write_file"而漏批。
    """
    raw = os.getenv("AGENT_MCP_APPROVE", "") or ""
    return {item.strip() for item in raw.split(",") if item.strip()}


_ENV_REF = re.compile(r"\$\{([^}]*)\}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _expand_ref(value: str, *, where: str) -> str:
    """把 ``${VAR}`` 换成进程环境变量。

    为什么需要它：``AGENT_MCP_SERVERS`` 是一整段 JSON，写密码/令牌进去就等于
    把它们抄进了配置文本（会进日志、进备份、进 git 事故）。用 ``${VAR}`` 让密钥
    留在 ``.env``（由 sops 管），配置里只留引用。

    三种写错都**报错**，不静默放过 —— 子进程拿到空令牌的典型表现是"启动成功但
    所有调用 401"，那个信号离真正的原因很远：
    * 变量没设置 ⇒ 用空串会伪装成"配置对了"；
    * 变量名不合法（例如 ``${中文}``）⇒ 正则第一版是 ``[A-Za-z_]`` 开头，
      匹配不上就**原样透传**，于是 ``${中文}`` 会被当成字面量发给子进程；
    * 缺右括号（``${VAR``）⇒ 同上。
    """
    remainder = _ENV_REF.sub("", value)
    if "${" in remainder:
        raise ValueError(f"{where} 里有缺少右括号的变量引用：{value}")

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if not _ENV_NAME.fullmatch(name):
            raise ValueError(
                f"{where} 里的变量名不合法：${{{name}}}"
                "（只能是字母、数字、下划线，不能以数字开头）"
            )
        got = os.getenv(name)
        if got is None:
            raise ValueError(f"{where} 引用了未设置的环境变量 {name}")
        return got

    return _ENV_REF.sub(_sub, value)


def server_specs() -> list[dict[str, Any]]:
    """解析 ``AGENT_MCP_SERVERS``（JSON 数组）。空/未设置 ⇒ 空列表。

    **格式错误直接抛 ``ValueError``**，不静默跳过。理由：这个配置决定"系统到底
    接没接 MCP"，静默忽略会让一次手误表现成"功能突然没了"，而日志里只有一行
    warning 淹没在启动噪音里。抛出去由 lifespan 捕获 → 明确记一条 error →
    这次部署**一个 MCP 都不挂**（不会半接）。
    """
    raw = (os.getenv("AGENT_MCP_SERVERS", "") or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"AGENT_MCP_SERVERS 不是合法 JSON：{e}") from e
    if not isinstance(data, list):
        raise ValueError(
            f"AGENT_MCP_SERVERS 必须是 JSON 数组，实际是 {type(data).__name__}"
        )
    specs: list[dict[str, Any]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"AGENT_MCP_SERVERS[{index}] 必须是对象")
        command = item.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"AGENT_MCP_SERVERS[{index}] 缺少 command（要启动的可执行文件）")
        specs.append(item)
    return specs


def configured() -> bool:
    """是否配置了至少一个服务器（供启动日志与就绪检查用）。"""
    try:
        return bool(server_specs())
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# 内容转换：MCP 的块列表 → 模型能吃的、长度可控的东西
# --------------------------------------------------------------------------- #
_IMAGE_DROPPED = (
    "〔服务器返回了一张图片（{mime}，约 {kb} KB），已降级为文字说明。"
    "需要看图就让用户改用文件路径，或把图的内容写成文字。〕"
)
_AUDIO_DROPPED = "〔服务器返回了一段音频（{mime}，约 {kb} KB），无法交给模型，已省略。〕"
_DROPPED_NOTE = "\n〔另有 {n} 块内容因超出长度上界未展示。需要就缩小请求范围再来一次。〕"
_TRUNCATED = (
    "\n…（内容过长已截断：原文 {total} 字符，这里只给了前 {kept} 字符。"
    "需要更多就缩小范围再来一次，不要拿这半段当作全部。）"
)


class MCPToolError(RuntimeError):
    """服务器把这次调用标成了失败（``isError=true``）。

    为什么必须抛，而不是像官方那样当正常返回
    ----------------------------------------
    MCP 协议里"工具内部出错"是**正常响应**：``CallToolResult.isError=True``，
    错误文案放在 ``content`` 里。官方照此处理，于是这件事在 Agent 侧的表现是
    **一次成功的工具调用** —— 步骤清单记 ``ok``、失败计数不增加、"到线劝退"
    永远不会触发。

    而本项目的成败判据只有一处：``steps.tool_error_handler``。它只在**异常**路径
    被调用（``on_tool_end`` 在失败路径照样触发，分不出成败，见那个模块的说明）。
    ⇒ 不抛，MCP 的业务失败就是隐形失败：模型可以拿着同一个错误无限重试，
    看板上一切正常。

    消息体里放的就是服务器给的原文（已按上界截断），所以模型看到的诊断信息
    没有减少 —— 变的只是它现在同时被记进了失败清单。
    """


def _kb(size: int) -> int:
    return max(1, round(size / 1024))


def _render_text(text: str, limit: int) -> tuple[TextContent, int] | None:
    """按上界截断一段文本；返回 (块, 成本)，``None`` 表示整块丢弃。成本按**截断后**算。

    ⚠️ 说明本身也占字符，必须从预算里扣掉 —— 否则"截到 limit"实际会超过 limit，
    而超出的那一块会在调用方被判成"放不下"整块丢弃（第一版就是这样：5000 字符
    的返回最后只剩下一句"另有 1 块未展示"）。说明里又嵌着 ``kept`` 这个数字，
    位数会反过来影响说明长度，所以迭代到不动点；数字每轮只减不增，四轮足够。

    ⚠️ 剩余预算**装不下那句说明**时返回 ``None``（整块丢弃），而不是切一半。
    切出来的东西是「…原文 150 字符，这里只给了前 0 字」，比不给更误导 ——
    模型会以为自己看到了一段真实内容。
    """
    if limit <= 0 or len(text) <= limit:
        return TextContent(type="text", text=text), len(text)
    if len(_TRUNCATED.format(total=len(text), kept=0)) >= limit:
        return None
    keep = limit
    for _ in range(4):
        note = _TRUNCATED.format(total=len(text), kept=keep)
        nxt = max(0, limit - len(note))
        if nxt >= keep:
            break
        keep = nxt
    out = text[:keep] + _TRUNCATED.format(total=len(text), kept=keep)
    if len(out) > limit:
        out = out[:limit]
    return TextContent(type="text", text=out), len(out)


def _render_block(
    block: Any, *, mode: str, budget: int, server: str, tool: str
) -> tuple[Any, int] | None:
    """把一个内容块转成"能交给模型"的块。返回 ``None`` 表示整块丢弃。

    ``budget`` 是当前还剩多少字符；对**不可分割**的块（图片），budget 不够就
    降级而不是截断 —— 截断 base64 会得到一个损坏的图（比不给更糟）。
    """
    if isinstance(block, TextContent):
        return _render_text(block.text, budget)

    if isinstance(block, ImageContent):
        if mode == "keep":
            cost = len(block.data)
            if budget <= 0 or cost <= budget:
                # 原样复用对象：ImageContent 还有 annotations / meta 等字段，
                # 重建会静默丢掉它们。
                return block, cost
        observability.counter("agent_mcp_image_dropped_total", server=server, tool=tool)
        text = _IMAGE_DROPPED.format(
            mime=getattr(block, "mimeType", "") or "image",
            kb=_kb(len(block.data) * 3 // 4),  # base64 → 原始字节
        )
        return _render_text(text, budget)

    if isinstance(block, AudioContent):
        observability.counter("agent_mcp_audio_dropped_total", server=server, tool=tool)
        text = _AUDIO_DROPPED.format(
            mime=getattr(block, "mimeType", "") or "audio",
            kb=_kb(len(getattr(block, "data", "") or "") * 3 // 4),
        )
        return _render_text(text, budget)

    if isinstance(block, ResourceLink):
        # 资源链接对模型有意义的是"有这么个东西、在哪"，内容本身要它自己去取。
        text = (
            f"〔外部资源〕{getattr(block, 'name', '') or ''}"
            f" uri={getattr(block, 'uri', '')} "
            f"mime={getattr(block, 'mimeType', '') or '未知'}"
        )
        return _render_text(text, budget)

    if isinstance(block, EmbeddedResource):
        resource = getattr(block, "resource", None)
        text = (
            resource.model_dump_json()
            if hasattr(resource, "model_dump_json")
            else str(resource)
        )
        return _render_text(text, budget)

    # 未知块类型：序列化成 JSON 而不是 ``str()``（后者是 Python repr，模型解析不回来）。
    dump = getattr(block, "model_dump_json", None)
    text = dump() if callable(dump) else str(block)
    return _render_text(text, budget)


def convert_result(result: CallToolResult, *, server: str, tool: str) -> CallToolResult:
    """转换层的唯一入口：把外部返回收进可预期的形状。

    三条规则，顺序不能换：

    1. **先按块渲染**，再**按总量分配预算**。反过来（先算总量再渲染）就必须
       预先知道每块渲染后多长，而那要渲染两次。
    2. **图片不可截断**：预算不够就降级成说明，绝不切 base64（切了就是坏图）。
    3. **``isError`` 与 ``structuredContent`` 原样保留**：这个函数只管长度，
       不管语义 —— 把错误结果改成非错误会让上游的失败判定整条失效。

    ``structuredContent`` 只有在服务器开了 ``use_structured_content`` 时才会被
    官方读走（那时它会 ``json.dumps`` 塞给模型）。所以超预算时也把它清掉，
    否则"另一条路"仍然能把窗口顶爆。
    """
    limit = result_max_chars()
    mode = images_mode()
    blocks = list(result.content or [])

    kept: list[Any] = []
    used = 0
    dropped = 0
    truncated = False

    for block in blocks:
        remaining = (limit - used) if limit > 0 else 0
        rendered = _render_block(
            block, mode=mode, budget=remaining, server=server, tool=tool
        )
        if rendered is None:
            dropped += 1
            continue
        out, cost = rendered
        if limit > 0 and cost > limit - used:
            # 渲染时已按 remaining 截过，走到这里说明"连截断后的最短形态都放不下"。
            dropped += 1
            continue
        kept.append(out)
        used += cost
        if isinstance(block, TextContent) and out.text != block.text:
            truncated = True

    if dropped:
        # ⚠️ 这条说明**不参与预算判断**，可能让总量略超上界（约 40 字符）。
        # 这是刻意的：告诉模型"还有东西被丢了"比"严格不超一个字符"重要得多 ——
        # 不给这句，模型会把手上这一小块当成全部，然后基于残片下结论。
        note = TextContent(type="text", text=_DROPPED_NOTE.format(n=dropped))
        kept.append(note)
        used += len(note.text)
        observability.counter(
            "agent_mcp_result_truncated_total", server=server, tool=tool
        )
    elif truncated:
        observability.counter(
            "agent_mcp_result_truncated_total", server=server, tool=tool
        )

    if used:
        observability.histogram("agent_mcp_result_chars", used, server=server)

    structured = result.structuredContent
    if limit > 0 and structured:
        try:
            size = len(json.dumps(structured))
        except (TypeError, ValueError):
            size = 0
        if size > limit:
            # 默认路径（``use_structured_content=False``）根本不会读这个字段，
            # 所以这里置空**不为省窗口**，而是堵住另一条路：有人日后打开那个开关，
            # 官方会把 json.dumps 的结果直接当内容塞给模型，那时这条就绕过了所有裁剪。
            logging.info(
                "MCP 结构化返回超过上界，已置空：server=%s tool=%s %d > %d",
                server, tool, size, limit,
            )
            observability.counter(
                "agent_mcp_result_truncated_total", server=server, tool=tool
            )
            return result.model_copy(update={"content": kept, "structuredContent": None})

    if kept == blocks:
        return result
    return result.model_copy(update={"content": kept})


def _error_text(result: CallToolResult) -> str:
    """把失败响应里的文本拼成异常消息（已按上界截断）。"""
    parts: list[str] = []
    for block in result.content or []:
        if isinstance(block, TextContent):
            parts.append(block.text)
        else:
            parts.append(f"〔{getattr(block, 'type', '未知块')}〕")
    text = "\n".join(parts).strip() or "（服务器返回了失败，但没有给出任何内容）"
    limit = result_max_chars()
    if limit > 0 and len(text) > limit:
        text = text[:limit]
    return text


# --------------------------------------------------------------------------- #
# 服务器装配
# --------------------------------------------------------------------------- #
def _mcp_error_handler(ctx: Any, error: BaseException) -> str:
    """MCP 工具的失败处理：复用项目那份逻辑，工具名**动态**取。

    为什么不能直接用 ``tool_error_handler("某个名字")``：那个函数在**构造期**用闭包
    捕获工具名，而 MCP 的工具清单要连上服务器才知道。所以这里从上下文取
    （``ToolContext.tool_name`` 就是模型看到的公开名），再委托给同一份实现 ——
    失败定格、到线劝退、超时文案因此与自有工具**逐字节一致**，不另立一套。
    """
    name = getattr(ctx, "tool_name", "") or "mcp"
    return tool_error_handler(name)(ctx, error)


def _tool_filter(spec: dict[str, Any]):
    """静态工具过滤：``tools_allow`` / ``tools_deny``。

    白名单非空时**只**暴露名单里的（名单里写错一个名字，那个工具就不见了 ——
    这是白名单的固有代价，启动日志会把实际暴露的数量打出来，便于核对）。
    """
    allow = [str(x) for x in (spec.get("tools_allow") or [])]
    deny = {str(x) for x in (spec.get("tools_deny") or [])}
    if not allow and not deny:
        return None

    def _filter(_context: Any, tool: Any) -> bool:
        if tool.name in deny:
            return False
        return not allow or tool.name in allow

    return _filter


def _approval_policy(spec: dict[str, Any], server_name: str) -> dict[str, bool] | None:
    """审批策略：把 ``AGENT_MCP_APPROVE`` 与服务器自己的 ``approve`` 合成一张表。

    ⚠️ 形态用**裸映射** ``{工具名: True}``，不用 ``{"always": {...}}`` ——
    后者要求值是含 ``tool_names`` 的对象，写错一个字就抛 ``UserError``，
    而错误信息指向的是 SDK 内部字段名，离真正的原因很远。
    """
    names = set(approve_names())
    for extra in spec.get("approve") or []:
        names.add(str(extra))

    policy: dict[str, bool] = {}
    for item in names:
        if "." in item:
            owner, _, bare = item.partition(".")
            if owner != server_name:
                continue
        else:
            bare = item
        policy[bare] = True
    return policy or None


class _ContentGuarded:
    """把 ``call_tool`` 的返回值过一遍转换层。作为 mixin 放在前面。

    ``super().call_tool(...)`` 落到 ``MCPServerStdio`` 的实现，**传输层**异常原样
    上抛，失败处理链一行不动（见模块说明"为什么插在 call_tool"）。

    只有一件事是这里主动改的：``isError=True`` 转成 ``MCPToolError``。
    理由见 ``MCPToolError`` 的说明 —— 不抛它在 Agent 侧就是"一次成功的调用"。
    """

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
    ) -> CallToolResult:
        result = await super().call_tool(tool_name, arguments, meta=meta)  # type: ignore[misc]
        converted = convert_result(result, server=self.name, tool=tool_name)
        if converted.isError:
            observability.counter(
                "agent_mcp_tool_error_total", server=self.name, tool=tool_name
            )
            raise MCPToolError(_error_text(converted))
        return converted


class GuardedStdioServer(_ContentGuarded, MCPServerStdio):
    """stdio 传输 + 内容转换层。"""


def _build_one(spec: dict[str, Any]) -> MCPServer:
    server_name = str(spec.get("name") or spec["command"])
    params: dict[str, Any] = {
        "command": _expand_ref(str(spec["command"]), where=f"{server_name}.command"),
        "args": [
            _expand_ref(str(a), where=f"{server_name}.args") for a in (spec.get("args") or [])
        ],
    }
    if spec.get("cwd"):
        params["cwd"] = _expand_ref(str(spec["cwd"]), where=f"{server_name}.cwd")
    if spec.get("env"):
        raw_env = spec["env"]
        if not isinstance(raw_env, dict):
            raise ValueError(f"{server_name}.env 必须是对象")
        params["env"] = {
            str(k): _expand_ref(str(v), where=f"{server_name}.env[{k}]")
            for k, v in raw_env.items()
        }

    timeout = timeout_seconds()
    return GuardedStdioServer(
        params=params,  # type: ignore[arg-type]
        name=server_name,
        cache_tools_list=cache_tools(),
        client_session_timeout_seconds=timeout if timeout > 0 else None,
        tool_filter=_tool_filter(spec),
        require_approval=_approval_policy(spec, server_name),
        failure_error_function=_mcp_error_handler,
    )


def build_servers() -> list[MCPServer]:
    """按配置构造服务器对象（**不连接**）。

    构造期的 ``ValueError`` 由调用方（lifespan）捕获 → 记 error、这次不挂任何
    服务器。不做"跳过坏的那一条、继续挂其它"：那会让一次配置手误表现成
    "某个工具时有时无"，而排查时看到的是"配置里有它"。
    """
    servers = []
    for spec in server_specs():
        server = _build_one(spec)
        servers.append(server)
        logging.info(
            "MCP 服务器已装配（尚未连接）：%s（command=%s）",
            server.name,
            spec["command"],
        )
    return servers


def attach(agent: Any, servers: list[MCPServer]) -> None:
    """把**已连接**的那批挂到 agent 上。

    只接受 ``MCPServerManager.active_servers``：把没连上的留在列表里，
    ``list_tools`` 会在每一轮运行里抛异常 ⇒ 整轮崩，而不是"少一个工具"。

    ``mcp_config`` 在这里一并设定，三个开关各有理由：
    * ``include_server_in_tool_names`` —— 不加前缀，MCP 里一个叫 ``deliver``
      的工具就能顶掉本项目自己的 ``deliver``，模型看不出来，行为完全变了；
    * ``convert_schemas_to_strict`` —— 这是"多给参数"唯一的官方等价物
      （strict 模式下模型侧就被约束）。它是 best-effort，转不动会静默回落，
      所以只能当**第一道**，不能当唯一一道；
    * ``failure_error_function`` —— agent 级兜底（服务器级已单独挂，这里防漏）。
    """
    agent.mcp_servers[:] = list(servers)
    agent.mcp_config.update({
        "include_server_in_tool_names": True,
        "convert_schemas_to_strict": True,
        "failure_error_function": _mcp_error_handler,
    })
