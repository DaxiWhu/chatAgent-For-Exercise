"""运维脚本的出站 HTTP 客户端构造：**本地目标必须绕过系统代理**。

为什么需要这个
--------------
httpx 默认读环境里的 `HTTP_PROXY`/`ALL_PROXY` 等变量（trust_env=True），
而**它不会自动豁免 loopback 地址**（除非环境里恰好设了 `NO_PROXY`）。

2026-09-28 实测踩到：本机设了 `HTTP_PROXY=http://127.0.0.1:12611` 且**没有** NO_PROXY，
于是脚本请求 `http://127.0.0.1:8010` 时被送去代理，代理连不上就返回：

    502 upstream connect failed: 由于目标计算机积极拒绝，无法连接。(os error 10061)

表现极具误导性：像是"服务没起来"，实际上请求**根本没到服务**（服务端访问日志为空）。
对压测脚本更糟——它会让**测出来的数字是经过代理的**，而不是服务真实的吞吐，
这种"看起来正常但口径错了"的结果比直接失败更危险。

策略
----
- 目标是 loopback（localhost / 127.0.0.0/8 / ::1）→ `trust_env=False`，强制直连；
- 其它目标 → 保持默认，让企业代理正常生效（连远端确实可能需要代理）。
"""
import ipaddress
from urllib.parse import urlparse

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain"}


def is_loopback(url: str) -> bool:
    """判断 URL 的主机是否指向本机。

    url: 完整 URL（如 `http://127.0.0.1:8000`）。解析失败时返回 False（按远端点处理）。
    """
    host = (urlparse(url).hostname or "").strip().lower()
    if not host:
        return False
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def trust_env_for(url: str) -> bool:
    """返回该目标应否信任环境代理设置：本机目标 False（直连），远端 True。"""
    return not is_loopback(url)
