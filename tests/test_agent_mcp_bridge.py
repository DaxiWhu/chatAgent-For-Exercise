"""MCP 接入与**内容转换层**的守卫。

这里守的是三件容易"看起来能用、实际上没防住"的事：

1. **返回长度**：外部服务器返回多少由对方决定。不封上界，一次调用就能把上下文
   顶爆，随后 ``history_budget`` 从最老开始丢 —— 丢的是用户原话。
2. **业务失败可见性**：MCP 把"工具内部报错"表达成 ``isError=True`` 的**正常响应**，
   官方照此处理 ⇒ 在 Agent 侧它是一次**成功**的调用（步骤清单记 ok、失败计数不增、
   到线劝退永不触发）。所以这里断言它被转成了异常。
3. **工具名撞车**：MCP 里一个叫 ``deliver`` 的工具能顶掉本项目自己的 ``deliver``，
   模型看不出来。所以断言公开名带服务器前缀。

端到端那两条**真的**起子进程（``tests/fixtures/mcp_test_server.py``）：拿手搓的
``CallToolResult`` 测转换层只能证明"我以为返回长这样"，证明不了官方客户端真的
把服务器的返回原样交过来。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from src.agents import mcp_bridge
from src.agents.mcp_bridge import MCPToolError, convert_result

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "tests" / "fixtures" / "mcp_test_server.py"

_ENV_KEYS = (
    "AGENT_MCP_SERVERS",
    "AGENT_MCP_RESULT_MAX_CHARS",
    "AGENT_MCP_IMAGES",
    "AGENT_MCP_TIMEOUT",
    "AGENT_MCP_APPROVE",
    "AGENT_MCP_TOOLS_WARN",
    "AGENT_MCP_CACHE_TOOLS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每条用例都从"什么都没配"开始 —— 否则一条用例设的 env 会漏给下一条。"""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _result(*blocks, is_error: bool = False, structured=None) -> CallToolResult:
    return CallToolResult(
        content=list(blocks), isError=is_error, structuredContent=structured
    )


def _text(value: str) -> TextContent:
    return TextContent(type="text", text=value)


def _image(data: str = "AAAA", mime: str = "image/png") -> ImageContent:
    return ImageContent(type="image", data=data, mimeType=mime)


# ---------------------------------------------------------------------------
# 一、内容转换：长度上界
# ---------------------------------------------------------------------------
def test_short_text_passes_through_unchanged():
    out = convert_result(_result(_text("很短")), server="s", tool="t")
    assert len(out.content) == 1
    assert out.content[0].text == "很短"


def test_long_text_is_clipped_to_the_limit_exactly(monkeypatch):
    """截断后必须**正好**不超过上界，且带"原文多长"。

    ⚠️ 第一版这里漏了：截断说明自身占字符、而说明里又嵌着 kept 的数字（位数会变），
    结果"截到 limit"实际是 limit+3，随后被调用方判成"放不下"整块丢弃 ——
    5000 字符的返回最后只剩一句"另有 1 块未展示"。所以这里断言的是**上限**
    而不只是"变短了"。
    """
    monkeypatch.setenv("AGENT_MCP_RESULT_MAX_CHARS", "200")
    out = convert_result(_result(_text("甲" * 5000)), server="s", tool="t")
    body = out.content[0].text
    assert len(body) <= 200, f"截断后仍超上界：{len(body)} > 200"
    assert "原文 5000 字符" in body, "没有告诉模型原文多长，它会拿半段当全部"
    assert "不要拿这半段当作全部" in body


def test_total_budget_is_shared_across_blocks(monkeypatch):
    """预算是**合计**的，不是每块各自一份 —— 否则十块各 7000 字符照样顶爆。

    预算不够的那一块**整块丢弃并说明**，而不是切成半句：切出来的是
    「…原文 150 字符，这里只给了前 0 字」，比不给更误导。
    """
    monkeypatch.setenv("AGENT_MCP_RESULT_MAX_CHARS", "200")
    out = convert_result(
        _result(_text("甲" * 150), _text("乙" * 150)), server="s", tool="t"
    )
    texts = [b.text for b in out.content if isinstance(b, TextContent)]
    assert any("另有 1 块内容因超出长度上界未展示" in t for t in texts), (
        "被丢掉的那块必须说出来，否则模型以为服务器只给了这些"
    )
    kept_text = [t for t in texts if "另有" not in t]
    assert len(kept_text) == 1 and kept_text[0] == "甲" * 150


def test_zero_limit_disables_clipping(monkeypatch):
    monkeypatch.setenv("AGENT_MCP_RESULT_MAX_CHARS", "0")
    body = "甲" * 5000
    out = convert_result(_result(_text(body)), server="s", tool="t")
    assert out.content[0].text == body


def test_empty_content_is_returned_as_is():
    original = _result()
    assert convert_result(original, server="s", tool="t") is original


# ---------------------------------------------------------------------------
# 二、内容转换：图片
# ---------------------------------------------------------------------------
def test_image_downgraded_to_text_by_default():
    """默认降级：base64 直塞是几十万字符起步，而且多数接口对单图大小有限制。"""
    out = convert_result(_result(_image()), server="s", tool="t")
    assert isinstance(out.content[0], TextContent), "图片没有被降级"
    assert "已降级为文字说明" in out.content[0].text
    assert "image/png" in out.content[0].text, "降级说明里要交代原来是什么图"


def test_image_kept_when_configured(monkeypatch):
    monkeypatch.setenv("AGENT_MCP_IMAGES", "keep")
    out = convert_result(_result(_image()), server="s", tool="t")
    assert isinstance(out.content[0], ImageContent)
    assert out.content[0].data == "AAAA", "keep 模式不能改动图片内容"


def test_oversized_image_is_downgraded_even_in_keep_mode(monkeypatch):
    """``keep`` 不等于"无上界"：预算不够时仍然降级。

    截断 base64 会得到一张坏图（比不给更糟），所以这里的行为是**整块降级**，
    不是"截一半"。

    ⚠️ 断言必须落到"降级说明"这句文案上，不能只判 ``isinstance(TextContent)``：
    图片被**整块丢弃**时，追加的那句"另有 N 块未展示"同样是 ``TextContent`` ——
    第一版就是这样，把"丢弃"和"降级"混成了一种表现，变异实测没红。
    """
    monkeypatch.setenv("AGENT_MCP_IMAGES", "keep")
    monkeypatch.setenv("AGENT_MCP_RESULT_MAX_CHARS", "100")
    out = convert_result(_result(_image("A" * 5000)), server="s", tool="t")
    assert isinstance(out.content[0], TextContent)
    assert "已降级为文字说明" in out.content[0].text, (
        "超预算的图片应当降级成说明，而不是静默丢掉"
    )


def test_remaining_budget_clips_instead_of_dropping(monkeypatch):
    """剩余预算够截一部分时，要**截断**而不是整块丢。

    这是"按剩余预算分配"与"每块各自一份"的唯一行为差异：前者让模型至少看到
    第二块的开头，后者会把它整块扔掉。没有这条用例，把 ``remaining = limit``
    改回去也不会红（最终还有一道 ``cost > limit - used`` 兜着，只是兜成了丢弃）。
    """
    monkeypatch.setenv("AGENT_MCP_RESULT_MAX_CHARS", "300")
    out = convert_result(
        _result(_text("甲" * 100), _text("乙" * 1000)), server="s", tool="t"
    )
    texts = [b.text for b in out.content if isinstance(b, TextContent)]
    assert len(texts) == 2, f"第二块被整块丢了，而不是按剩余预算截断：{texts}"
    assert texts[0] == "甲" * 100
    assert texts[1].startswith("乙") and "原文 1000 字符" in texts[1]
    assert sum(len(t) for t in texts) <= 300


# ---------------------------------------------------------------------------
# 三、内容转换：语义不能被改动
# ---------------------------------------------------------------------------
def test_error_flag_is_preserved_by_conversion(monkeypatch):
    """转换层只管长度，不管语义 —— 把错误改成非错误会让上游失败判定整条失效。"""
    monkeypatch.setenv("AGENT_MCP_RESULT_MAX_CHARS", "50")
    out = convert_result(
        _result(_text("错" * 500), is_error=True), server="s", tool="t"
    )
    assert out.isError is True


def test_oversized_structured_content_is_cleared(monkeypatch):
    """结构化返回不能成为绕过裁剪的第二条路。

    默认路径不读它，但一旦有人开了 ``use_structured_content``，官方会把它
    ``json.dumps`` 之后直接当内容塞给模型。
    """
    monkeypatch.setenv("AGENT_MCP_RESULT_MAX_CHARS", "100")
    out = convert_result(
        _result(_text("短"), structured={"blob": "甲" * 1000}), server="s", tool="t"
    )
    assert out.structuredContent is None


def test_small_structured_content_is_kept(monkeypatch):
    monkeypatch.setenv("AGENT_MCP_RESULT_MAX_CHARS", "1000")
    out = convert_result(_result(_text("短"), structured={"a": 1}), server="s", tool="t")
    assert out.structuredContent == {"a": 1}


# ---------------------------------------------------------------------------
# 四、配置解析
# ---------------------------------------------------------------------------
def test_empty_config_means_no_servers():
    assert mcp_bridge.server_specs() == []
    assert mcp_bridge.configured() is False


def test_invalid_json_raises_instead_of_silently_skipping(monkeypatch):
    """格式错误**必须抛**：静默跳过会让一次手误表现成"功能突然没了"。"""
    monkeypatch.setenv("AGENT_MCP_SERVERS", "{不是数组")
    with pytest.raises(ValueError, match="不是合法 JSON"):
        mcp_bridge.server_specs()


def test_non_array_raises(monkeypatch):
    monkeypatch.setenv("AGENT_MCP_SERVERS", '{"command": "x"}')
    with pytest.raises(ValueError, match="必须是 JSON 数组"):
        mcp_bridge.server_specs()


def test_missing_command_raises(monkeypatch):
    monkeypatch.setenv("AGENT_MCP_SERVERS", '[{"name": "没有命令"}]')
    with pytest.raises(ValueError, match="缺少 command"):
        mcp_bridge.server_specs()


def test_env_ref_is_expanded(monkeypatch):
    """``${VAR}`` 让密钥留在 .env，不抄进 JSON 配置（否则会进日志、进备份）。

    断言展开函数的**实际输出**：只验"构造没报错"是空断言 —— 展开和不展开都能过。
    """
    monkeypatch.setenv("MY_TOKEN", "s3cret")
    assert mcp_bridge._expand_ref("Bearer ${MY_TOKEN}", where="t") == "Bearer s3cret"


def test_missing_env_ref_raises(monkeypatch):
    """引用不存在的变量必须报错，不能替换成空串。

    子进程拿到空令牌的典型表现是"启动成功但所有调用 401"，那个信号离真正的原因很远。
    """
    monkeypatch.setenv(
        "AGENT_MCP_SERVERS",
        '[{"name": "demo", "command": "python", "env": {"TOKEN": "${NO_SUCH_VAR_XYZ}"}}]',
    )
    with pytest.raises(ValueError, match="未设置的环境变量"):
        mcp_bridge.build_servers()


def test_bad_env_ref_name_raises(monkeypatch):
    """变量名不合法也必须报错。

    ⚠️ 第一版正则只匹配 ``[A-Za-z_]`` 开头，``${中文}`` 匹配不上就**原样透传** ——
    配置里写错一个名字，子进程收到的是字面量 ``${中文}``，而启动日志一切正常。
    """
    with pytest.raises(ValueError, match="变量名不合法"):
        mcp_bridge._expand_ref("${中文}", where="t")


def test_unclosed_env_ref_raises():
    with pytest.raises(ValueError, match="缺少右括号"):
        mcp_bridge._expand_ref("${VAR", where="t")


def test_approve_names_accepts_both_forms(monkeypatch):
    monkeypatch.setenv("AGENT_MCP_APPROVE", "write_file, fs.delete_file ,")
    assert mcp_bridge.approve_names() == {"write_file", "fs.delete_file"}


def test_approval_policy_scopes_namespaced_names(monkeypatch):
    """``fs.write`` 只对 fs 这个服务器生效；裸名对所有服务器生效。"""
    monkeypatch.setenv("AGENT_MCP_APPROVE", "write_file,fs.delete_file")
    policy = mcp_bridge._approval_policy({}, "fs")
    assert policy == {"write_file": True, "delete_file": True}
    policy_other = mcp_bridge._approval_policy({}, "db")
    assert policy_other == {"write_file": True}, "带服务器前缀的规则不该漏到别的服务器"


def test_tool_filter_allow_and_deny():
    allow = mcp_bridge._tool_filter({"tools_allow": ["read"]})

    class _T:
        def __init__(self, name):
            self.name = name

    assert allow(None, _T("read")) is True
    assert allow(None, _T("write")) is False
    deny = mcp_bridge._tool_filter({"tools_deny": ["rm"]})
    assert deny(None, _T("rm")) is False
    assert deny(None, _T("read")) is True
    assert mcp_bridge._tool_filter({}) is None


# ---------------------------------------------------------------------------
# 五、装配：前缀与 strict 必须真的设上
# ---------------------------------------------------------------------------
class _FakeAgent:
    def __init__(self):
        self.mcp_servers: list = []
        self.mcp_config: dict = {}


def test_attach_turns_on_prefix_and_strict():
    """三个开关少一个都是静默故障：

    * 不加前缀 ⇒ 一个叫 ``deliver`` 的 MCP 工具顶掉本项目自己的 ``deliver``；
    * 不开 strict ⇒ 模型多给的字段原样转给服务器（唯一一道参数约束没了）；
    * 不挂失败处理 ⇒ MCP 失败不进步骤清单。
    """
    agent = _FakeAgent()
    mcp_bridge.attach(agent, ["a", "b"])
    assert agent.mcp_servers == ["a", "b"]
    assert agent.mcp_config["include_server_in_tool_names"] is True
    assert agent.mcp_config["convert_schemas_to_strict"] is True
    assert callable(agent.mcp_config["failure_error_function"])


# ---------------------------------------------------------------------------
# 六、端到端：真的起子进程
# ---------------------------------------------------------------------------
def _server(**kwargs):
    return mcp_bridge.GuardedStdioServer(
        params={"command": sys.executable, "args": [str(SERVER)]},
        name="demo",
        cache_tools_list=True,
        client_session_timeout_seconds=30,
        **kwargs,
    )


def test_end_to_end_conversion_through_a_real_server(monkeypatch):
    """一次性跑完四条真实路径：短文本、超长、图片、业务错误。"""
    monkeypatch.setenv("AGENT_MCP_RESULT_MAX_CHARS", "300")

    async def _run():
        server = _server()
        async with server:
            tools = await server.list_tools()
            names = sorted(t.name for t in tools)
            short = await server.call_tool("echo", {"text": "hello"})
            long_ = await server.call_tool("big_text", {"n": 5000})
            pic = await server.call_tool("picture", {})
            error: BaseException | None = None
            try:
                await server.call_tool("boom", {})
            except BaseException as e:  # noqa: BLE001
                error = e
            return names, short, long_, pic, error

    names, short, long_, pic, error = asyncio.run(_run())

    assert names == ["big_text", "boom", "echo", "picture", "slow"], names
    assert short.content[0].text == "hello"
    assert len(long_.content[0].text) <= 300, "真实返回没有被截断"
    assert "原文 5000 字符" in long_.content[0].text
    assert isinstance(pic.content[0], TextContent), "真实图片没有被降级"
    assert isinstance(error, MCPToolError), (
        "服务器标了 isError，却没有变成异常 —— 这次失败会在步骤清单里记成成功"
    )
    assert "测试用的故意失败" in str(error), "异常消息里要带上服务器给的原文"


def test_end_to_end_tool_names_are_prefixed():
    """公开名必须带 ``mcp_{server}__`` 前缀 —— 走**真实的装配函数**。

    ⚠️ 第一版这里自己 ``mcp_config.update({"include_server_in_tool_names": True})``，
    于是它测的是"官方前缀机制可用"，而不是"我们的装配打开了它"：把 ``attach`` 里
    那个开关改成 False，这条照样绿。变异实测抓出来的。
    """
    from agents import Agent, RunContextWrapper
    from src.agents import mcp_bridge as bridge

    async def _names():
        server = _server()
        async with server:
            agent = Agent(name="t")
            bridge.attach(agent, [server])
            ctx = RunContextWrapper(context=None)
            tools = await agent.get_all_tools(ctx)
            return [t.name for t in tools]

    names = asyncio.run(_names())
    assert names, "没有拿到任何 MCP 工具"
    assert all(n.startswith("mcp_demo__") for n in names), names


# ---------------------------------------------------------------------------
# 七、启动钩子：配置 → 连接 → 挂到入口 agent
# ---------------------------------------------------------------------------
def _spec_json() -> str:
    import json

    return json.dumps(
        [{"name": "demo", "command": sys.executable, "args": [str(SERVER)]}]
    )


def test_startup_hook_attaches_connected_servers(monkeypatch):
    """这条测的是"整条链"，不是转换层。

    前面所有用例都可以在"根本没接上"的情况下通过 —— 只有这里能证明：
    配置写了 ⇒ 真的起了子进程 ⇒ 真的连上了 ⇒ 真的挂到了入口 agent 上。
    """
    import asyncio as _asyncio
    from contextlib import AsyncExitStack

    from app.main import _install_mcp_servers
    from src.agents.runner import ENTRY_AGENT

    monkeypatch.setenv("AGENT_MCP_SERVERS", _spec_json())
    before = list(ENTRY_AGENT.mcp_servers)
    before_cfg = dict(ENTRY_AGENT.mcp_config)
    try:
        async def _run():
            async with AsyncExitStack() as stack:
                await _install_mcp_servers(stack)
                return [s.name for s in ENTRY_AGENT.mcp_servers]

        assert _asyncio.run(_run()) == ["demo"]
        assert ENTRY_AGENT.mcp_config["include_server_in_tool_names"] is True
        assert ENTRY_AGENT.mcp_config["convert_schemas_to_strict"] is True
    finally:
        # 全局单例必须复原：它会被同进程里的其它用例看见。
        ENTRY_AGENT.mcp_servers[:] = before
        ENTRY_AGENT.mcp_config.clear()
        ENTRY_AGENT.mcp_config.update(before_cfg)


def test_startup_hook_is_a_noop_when_not_configured(monkeypatch):
    """空配置 ⇒ 什么都不做。这条守着"接 MCP 之前的行为逐字节不变"。"""
    import asyncio as _asyncio
    from contextlib import AsyncExitStack

    from app.main import _install_mcp_servers
    from src.agents.runner import ENTRY_AGENT

    before = list(ENTRY_AGENT.mcp_servers)

    async def _run():
        async with AsyncExitStack() as stack:
            await _install_mcp_servers(stack)

    _asyncio.run(_run())
    assert ENTRY_AGENT.mcp_servers == before


def test_startup_hook_survives_bad_config(monkeypatch):
    """配置写坏 ⇒ 一个都不挂，但**不阻止启动**（与其它自检同一口径）。"""
    import asyncio as _asyncio
    from contextlib import AsyncExitStack

    from app.main import _install_mcp_servers
    from src.agents.runner import ENTRY_AGENT

    monkeypatch.setenv("AGENT_MCP_SERVERS", "[这不是 JSON")
    before = list(ENTRY_AGENT.mcp_servers)

    async def _run():
        async with AsyncExitStack() as stack:
            await _install_mcp_servers(stack)

    _asyncio.run(_run())
    assert ENTRY_AGENT.mcp_servers == before, "配置坏了却挂上了半个 —— 必须一个都不挂"
