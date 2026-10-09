"""脚本与 CI 的凭证获取：**账号密码换 JWT**。

为什么脚本也走登录（而不是另开一条"机器凭证"的路）
------------------------------------------------
机器调用方曾经有一条专用路径：共享密钥 `X-API-Key`（代码里叫"脚本过渡通行证"）。
它有**三个结构性缺陷**，每一个都落在"持它的人能干什么"上，而不是"它够不够长"：

  - **分不出是谁**：所有调用方共用同一个值 ⇒ 审计日志里 `user` 恒为空，
    审核留痕只能记到"某个租户"，归不到人；
  - **撤不掉**：单值、无重叠窗口、无过期 ⇒ 换值 = 改配置 + 重启 = 踢掉所有在线；
  - **带不了租户**：它没有载荷，租户只能靠请求头 `X-Tenant-Id` **自报**且被采信
    ⇒ 持它者换一个请求头就能切检索分区、并绕过按租户分桶的配额。

⚠️ 2026-10-05：那条路径**已从代码中整体删除**（连"留空备用"都不留）。所以本模块
现在不是"推荐做法"，而是**唯一做法**。改用"服务账号 + 登录换令牌"后，三个缺陷一次
解决：令牌里有 `sub` 与 `tenant`（可归因）、落在服务端会话白名单里（可撤销）、
租户来自**已验签的载荷**（不可自报）。

它的租户与角色是**签发时快照**，所以脚本若长跑，要在收到 `relogin_required` 时重登
（见 `examples/http_client.py whoami`）。

建一个脚本账号
--------------
服务账号与人类用户**共用同一张账号表**（`users`），靠命名区分（建议 `svc-` 前缀）：

    python -m scripts.hash_password -u svc-loadtest -t <租户>
    # 不给 -r 就是"没有角色" —— 服务账号按最小权限走，不需要 reviewer/admin
    # 把打印出的那一行（**含单引号**）粘进 .env 的 AUTH_USERS，重启服务

或者用管理接口 `POST /v1/admin/users` 建（需要管理员令牌），这样不必重启。

凭证从哪来
----------
    账号：`--user` 参数 > 环境变量 `AGENT_SCRIPT_USER`
    密码：环境变量 `AGENT_SCRIPT_PASSWORD`（**刻意不提供命令行参数** ——
          明文密码会进 shell history 与 `ps` 输出，与 `hash_password` 的告警口径一致）

两者都取不到时**直接失败**，不"静默退回共享密钥"：后者会让"脚本已改走登录"这件事
在本地看起来成立、而在真正需要它的环境里其实没生效。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

USER_ENV = "AGENT_SCRIPT_USER"
PASSWORD_ENV = "AGENT_SCRIPT_PASSWORD"

_HOW_TO_CREATE = (
    f"缺少脚本账号凭证。请设置环境变量 {USER_ENV} / {PASSWORD_ENV}"
    "（账号也可用 --user 参数给出；密码刻意只从环境变量读，避免进 shell history）。\n"
    "还没有服务账号时，先建一个（与人类用户共用 users 表，建议用 svc- 前缀命名）：\n"
    "    python -m scripts.hash_password -u svc-loadtest -t <租户>\n"
    "    （不给 -r 就是**没有角色** —— 服务账号按最小权限走，不需要 reviewer/admin）\n"
    "    把打印出的那一行（含单引号）粘进 .env 的 AUTH_USERS 后重启；\n"
    "    或用管理接口 POST /v1/admin/users 建（需管理员令牌，不必重启）。"
)


def credentials(user: str = "", password: str = "") -> tuple[str, str]:
    """取脚本账号凭证：参数优先，其次环境变量。

    `password` 这个参数存在是为了让**测试**能用它自己的变量名（`E2E_PASSWORD`）；
    命令行脚本**不该**用它 —— 明文密码会进 shell history 与 `ps` 输出。
    """
    return (
        (user or os.getenv(USER_ENV, "")).strip(),
        (password or os.getenv(PASSWORD_ENV, "")).strip(),
    )


def require_credentials(user: str = "", password: str = "") -> tuple[str, str]:
    """与 `credentials` 相同，但缺任一项时给出可照做的指引并终止。"""
    u, p = credentials(user, password)
    if not (u and p):
        raise SystemExit(_HOW_TO_CREATE)
    return u, p


def has_credentials(user: str = "", password: str = "") -> bool:
    """只做"配没配"的判断，不报错（给任务入口与测试做分支用）。"""
    u, p = credentials(user, password)
    return bool(u and p)


def _login_hint(status: int, body: str) -> str:
    """把登录失败的几个状态码翻成人话。

    ⚠️ 401 刻意不区分"账号不存在""密码错""账号停用" —— 服务端就是**故意**让三者
    响应一致（防用户名枚举），客户端别自作聪明地猜。
    """
    hint = {
        401: "账号不存在 / 密码不对 / 账号已停用（服务端刻意让三者响应一致，防用户名枚举）",
        403: "账号存在但被禁止登录",
        422: "请求体不合规（例如密码不满足长度下限）",
        429: "触发限流，稍后重试",
        503: "服务端未启用可登录的用户鉴权（缺 AUTH_SESSION_SECRET，或 AUTH_* 整体未配置）",
    }.get(status, "")
    return f"登录失败（HTTP {status}）：{hint}\n{body[:400]}"


def login(base_url: str, user: str, password: str, *, timeout: float = 15.0) -> dict:
    """`POST /v1/auth/login`，返回响应体（含 access_token / tenant / roles）。

    刻意用标准库而不是 httpx：这份 helper 被**脚本与测试**共用，而
    `tests/test_e2e_stack.py` 的文件头写明了"不引测试专属依赖"的理由。
    """
    payload = json.dumps({"username": user, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        (base_url or "").rstrip("/") + "/v1/auth/login",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise SystemExit(_login_hint(exc.code, body)) from None
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"连不上 {base_url}/v1/auth/login：{exc.reason}\n"
            "服务起了吗？本地可直接 `python -m app.serve`。"
        ) from None


def access_token(base_url: str, user: str, password: str, *, timeout: float = 15.0) -> str:
    """登录并取出 `access_token`；拿不到就明确报错（绝不返回空串当"没配"）。"""
    data = login(base_url, user, password, timeout=timeout)
    token = str(data.get("access_token") or "")
    if not token:
        raise SystemExit(f"登录响应里没有 access_token：{data}")
    return token


def auth_headers(base_url: str, user: str = "", *, timeout: float = 15.0) -> dict:
    """一站式：取凭证 → 登录 → 返回可直接用的请求头。

    返回值同时带 `_user` / `_tenant` 两个下划线前缀的元信息（**不是请求头**），
    供调用方打印"以谁的身份在跑"；构造真实请求前请用 `only_headers()` 过滤掉它们。
    """
    u, p = require_credentials(user)
    data = login(base_url, u, p, timeout=timeout)
    token = str(data.get("access_token") or "")
    if not token:
        raise SystemExit(f"登录响应里没有 access_token：{data}")
    return {
        "Authorization": f"Bearer {token}",
        "_user": str(data.get("user") or u),
        "_tenant": str(data.get("tenant") or ""),
    }


def only_headers(headers: dict) -> dict:
    """剥掉 `_` 开头的元信息键，只留下真正要发的请求头。"""
    return {k: v for k, v in headers.items() if not k.startswith("_")}
