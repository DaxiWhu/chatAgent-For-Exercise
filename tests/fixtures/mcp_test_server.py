"""测试用的最小 MCP 服务器（stdio 传输）。

它存在只有一个目的：让"转换层"能被**真的**验证一次 —— 不是拿一个假的
``CallToolResult`` 自己造，而是让官方客户端真的起子进程、走一遍协议、把外部
返回原样拿回来，再交给转换层。

五个工具各自对应一条真实会出问题的路径：

==================  ==========================================================
``echo``            正常短文本：确认转换层**不会**动没超限的内容
``big_text``        超长文本：确认按上界截断且带"原文多长"的说明
``picture``         图片：确认 base64 被降级成一句说明（默认）或原样保留（keep）
``boom``            抛异常：确认异常**原样上抛**给失败处理，不被转换层吞掉
``slow``            慢响应：确认会话读超时能真的到点（不是永远等）
==================  ==========================================================

⚠️ 这个文件**不是** pytest 用例（名字不以 ``test_`` 开头），它只作为子进程被
``test_agent_mcp_bridge.py`` 启动。所以别往这里加断言。
"""

from __future__ import annotations

import base64
import time

from mcp.server.fastmcp import FastMCP, Image

mcp = FastMCP("agent-mcp-test")

# 1x1 透明 PNG。要的是"它是一张真图"，不是"它好看"。
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@mcp.tool()
def echo(text: str) -> str:
    """原样返回输入的文本。"""
    return text


@mcp.tool()
def big_text(n: int) -> str:
    """返回 n 个字符的文本，用来验证截断上界。"""
    n = max(0, min(int(n), 2_000_000))
    return "甲" * n


@mcp.tool()
def picture() -> Image:
    """返回一张 1x1 的 PNG，用来验证图片降级策略。"""
    return Image(data=_PNG_1X1, format="png")


@mcp.tool()
def boom() -> str:
    """故意抛异常，用来验证异常没有被转换层吞掉。"""
    raise RuntimeError("测试用的故意失败")


@mcp.tool()
def slow(seconds: float) -> str:
    """睡 seconds 秒再返回，用来验证会话读超时。"""
    time.sleep(max(0.0, min(float(seconds), 30.0)))
    return "醒了"


if __name__ == "__main__":
    mcp.run(transport="stdio")
