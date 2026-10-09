"""chatAgent HTTP 客户端示例：流式对话 + 引用回溯 + 人工审核闭环。

用途
----
这是**给接入方看的参考实现**，不是生产代码：展示怎么正确消费本服务的 SSE 流。
`src/` 里的东西是服务端内部实现，接入方只需要知道本文件里的三个端点与事件语义。

跑之前
------
服务端要先起来。**唯一**的拿凭证方式是登录换令牌：

    # 服务端（另开一个终端）
    python -m app.serve

    # ① 用账号密码换 JWT
    python examples/http_client.py login --user svc-loadtest
    #    密码从环境变量 AGENT_SCRIPT_PASSWORD 读（避免进 shell history）

    # ② 拿上一步的 access_token 调其它子命令
    python examples/http_client.py whoami --token <access_token>
    python examples/http_client.py chat --token <access_token> --message "你好"
    python examples/http_client.py reviews --token <access_token>
    python examples/http_client.py review --token <access_token> \
        --thread-id <id> --action revise --feedback "第二段要给出数据来源"

⚠️ 2026-10-05：`--api-key` / `--tenant` 两个参数**已删除**。共享密钥（`X-API-Key`）
与自报租户（`X-Tenant-Id`）在服务端已不存在 —— 不带令牌的请求一律 401，
租户一律取自令牌载荷，客户端无法指定。

也可以不用命令行，直接改下面的 `_demo()` 里的参数。

事件语义（服务端 `app/main.py` 的 `_stream_graph` 定义）
--------------------------------------------------------
| event            | data 形态 | 含义 |
|------------------|-----------|------|
| `token`          | **裸字符串** | 增量正文片段，直接拼起来就是回答 |
| `citations`      | JSON 数组 | 可回溯引用（来源/页码/章节/chunk_id/原文节选/表格网格/图片路径） |
| `need_review`    | JSON 对象 | 需人工审核，附草稿/评分/意见；服务端已同时入队并发通知 |
| `budget_exceeded`| JSON 对象 | 单请求 token 预算耗尽，生成被提前终止（已产出的照常下发） |
| `done`           | 无 | 正常结束 |
| `error`          | 字符串 | 异常信息 |

⚠️ **`token` 的 data 是裸文本，不是 JSON** —— 这是最容易踩的坑。如果对所有事件
统一 `json.loads`，遇到内容恰好像 JSON 的片段（比如回答里出现 `123`、`true`、`{}`）
就会解析成 int/bool/dict 而把正文写坏。本示例**按事件名决定是否解析**。

⚠️ **另一个同样隐蔽的坑：系统代理**。httpx 默认读 `HTTP_PROXY` 等环境变量，且
**不会自动豁免 loopback**（除非恰好设了 `NO_PROXY`）。本机开着代理时请求
`http://127.0.0.1:8000` 会被送去代理，代理连不上就返回 502
（`upstream connect failed`）——看起来像"服务没起来"，其实请求**根本没到服务**。
所以 `_client_for()` 对本机地址强制直连。**这段逻辑请照抄进你自己的客户端**。
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
from urllib.parse import urlparse

import httpx


def _client_for(base_url: str, timeout: httpx.Timeout) -> httpx.Client:
    """构造 httpx 客户端：目标是本机就绕过环境代理，否则让代理正常生效。

    base_url: 服务地址。远端地址（企业网内）确实可能需要走代理，所以不一律禁用。
    timeout: 超时配置。**不要用总超时** —— 长回答会持续几分钟，总超时会在中途掐断连接。
    """
    host = (urlparse(base_url).hostname or "").lower()
    loopback = host in ("localhost", "localhost.localdomain")
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False          # 域名/解析失败：按远端处理
    if loopback:
        print(f"[提示] {base_url} 是本机地址，已禁用环境代理（避免请求被送去 HTTP_PROXY）",
              file=sys.stderr)
    return httpx.Client(timeout=timeout, trust_env=not loopback)


# ---------------------------------------------------------------------------
# SSE 解析
# ---------------------------------------------------------------------------
# 为什么不用现成的 SSE 库：这是示例，把"一行行读、按空行分帧、取 event/data"
# 讲清楚比省这几行代码更有价值；接入方无论用什么技术栈都能照着实现。
#
# 两个实际注意点：
#   1. 行尾可能是 \r\n（sse-starlette 在部分平台如此），所以要 rstrip("\r\n")；
#   2. 一帧以**空行**结束，不是以 event: 行结束 —— 忘记这点会把 data 提前切掉。
# ---------------------------------------------------------------------------
def iter_sse(response: httpx.Response):
    """把一个流式响应拆成 (event, data) 序列。

    response: 已 `stream=True` 打开的 httpx 响应。
    产出 (event_name, raw_data)：event_name 缺省为 "message"，raw_data 是未解析的字符串。
    """
    event = None
    data_lines: list[str] = []

    for line in response.iter_lines():
        line = line.rstrip("\r\n")

        if line == "":                       # 空行 = 一帧结束
            if data_lines:
                yield event or "message", "\n".join(data_lines)
            event, data_lines = None, []
            continue

        if line.startswith(":"):             # 注释行（心跳），忽略
            continue

        field, _, value = line.partition(":")
        value = value.lstrip(" ")            # 规范：冒号后最多一个空格
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)
        # id / retry 本服务不用，忽略

    if data_lines:                           # 服务端没补最后一个空行也不丢帧
        yield event or "message", "\n".join(data_lines)


JSON_EVENTS = {"citations", "need_review", "budget_exceeded"}


def decode(event: str, raw: str):
    """按事件名决定是否 JSON 解析。

    **解析失败要显式失败，不能静默降级成字符串。**
    这一点是实测教训：早期实现把失败降级为"原样返回"，于是当服务端
    用 Python repr 序列化（单引号，见下）时，客户端拿到的是一个字符串，
    表现为"引用 434 条"（其实是 434 个字符）、随后遍历字符串取 `.get` 崩掉 ——
    真正的原因（服务端序列化错了）被这层兜底完全掩盖。宁可当场报错说清楚。
    """
    if event not in JSON_EVENTS:
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"事件 {event!r} 的 data 不是合法 JSON，请检查服务端序列化：\n"
            f"  原始数据：{raw[:300]}\n"
            f"  解析错误：{e}\n"
            f"提示：如果看到的是单引号（如 [{{'n': 1}}]），说明服务端把结构化数据\n"
            f"      按 Python repr 发出去了，应改为先 json.dumps 再下发。"
        ) from e


# ---------------------------------------------------------------------------
# 三个端点
# ---------------------------------------------------------------------------
def chat(base_url: str, token: str, thread_id: str, message: str) -> dict:
    """流式对话，把事件打到 stdout，返回汇总（正文 + 引用 + 是否需审核）。"""
    body = {"thread_id": thread_id, "message": message}
    headers = _headers(token)
    answer: list[str] = []
    result = {"answer": "", "citations": [], "need_review": None, "error": None}

    # timeout 只限制"建连 + 两帧之间"的间隔，不限制整个流的总时长 ——
    # 长回答会持续几分钟，设成总超时会在中途把连接掐断。
    timeout = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
    with _client_for(base_url, timeout) as client:
        with client.stream("POST", f"{base_url}/v1/chat", json=body, headers=headers) as resp:
            if resp.status_code != 200:
                # SSE 端点的错误是"还没开始流"就返回的 JSON（401/403/429），
                # 这时用 read() 取完整 body 才有内容可看。
                resp.read()
                _die(resp.status_code, resp.text)

            for event, raw in iter_sse(resp):
                data = decode(event, raw)

                if event == "token":
                    answer.append(data)
                    print(data, end="", flush=True)

                elif event == "citations":
                    result["citations"] = data
                    print(f"\n\n--- 引用 {len(data)} 条 ---")
                    for c in data:
                        # `where` 是服务端算好的人话位置（如 "intro.txt 第 3 页 · 检索"）
                        print(f"  [{c.get('n')}] {c.get('where') or c.get('source')}"
                              f"（score={c.get('score')}, chunk_id={c.get('chunk_id')}）")
                        snippet = (c.get("snippet") or "").replace("\n", " ")
                        print(f"      {snippet[:120]}{'…' if len(snippet) > 120 else ''}")

                elif event == "need_review":
                    result["need_review"] = data
                    print("\n\n--- 需人工审核 ---")
                    print(f"  评分 {data.get('score')}；意见 {data.get('feedback') or '（无）'}")
                    print(f"  草稿 {str(data.get('draft'))[:200]}")
                    print(f"  处理：python examples/http_client.py review "
                          f"--thread-id {thread_id} --action revise --feedback '...'")

                elif event == "budget_exceeded":
                    print(f"\n\n--- 预算耗尽（已用 {data.get('used')}/{data.get('limit')}），"
                          f"生成被提前终止 ---")

                elif event == "error":
                    result["error"] = data
                    print(f"\n\n--- 服务端异常：{data} ---")

                elif event == "done":
                    pass                     # 正常结束，无需打印

    result["answer"] = "".join(answer)
    return result


def review(base_url: str, token: str, thread_id: str,
           action: str, feedback: str) -> None:
    """提交人工审核结论。**返回的同样是 SSE 流**（revise 会续跑生成）。"""
    body = {"thread_id": thread_id, "action": action, "feedback": feedback}
    headers = _headers(token)
    timeout = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)

    with _client_for(base_url, timeout) as client:
        with client.stream("POST", f"{base_url}/v1/review", json=body, headers=headers) as resp:
            if resp.status_code != 200:
                resp.read()
                _die(resp.status_code, resp.text)
            for event, raw in iter_sse(resp):
                data = decode(event, raw)
                if event == "token":
                    print(data, end="", flush=True)
                elif event == "citations":
                    print(f"\n\n--- 引用 {len(data)} 条 ---")
                elif event == "need_review":
                    print(f"\n\n--- 仍需人工审核：{data.get('feedback')} ---")
                elif event == "error":
                    print(f"\n\n--- 服务端异常：{data} ---")


def reviews(base_url: str, token: str, limit: int = 50) -> None:
    """列出待人工审核的会话（普通 JSON 接口，不是 SSE）。"""
    headers = _headers(token)
    with _client_for(base_url, httpx.Timeout(15.0)) as client:
        r = client.get(f"{base_url}/v1/reviews", params={"limit": limit}, headers=headers)
    if r.status_code != 200:
        _die(r.status_code, r.text)
    payload = r.json()
    print(f"待审 {payload['count']} 条：")
    for it in payload["items"]:
        print(f"  - {it.get('thread_id')}  tenant={it.get('tenant')} "
              f"score={it.get('score')} draft={str(it.get('draft'))[:60]}")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _headers(token: str = "") -> dict:
    """构造请求头：只有 `Authorization: Bearer`。

    ⚠️ 2026-10-05：这里原先还会发 `X-API-Key`（共享密钥）与 `X-Tenant-Id`（自报租户），
    两者都已删除 —— 服务端现在**只认已验签的 JWT**，租户也来自令牌载荷。
    调用方不再需要、也不应该自报租户。
    """
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def whoami(base_url: str, token: str) -> None:
    """查当前身份；服务端说"该重新登录了"就把它**弹出来**。

    ⚠️ 这是接入方**必须实现**的一步，不是可选装饰。JWT 里的租户与角色是
    **签发那一刻的快照**：管理员改了它们、或批准了你的入租申请之后，你手上那张
    令牌仍按旧值生效（"批准入租"刻意不断会话）。不实现这个检查的症状是 ——
    用户"申请明明通过了却还是搜不到东西"，然后反复重试、提工单。

    真实客户端在这里弹窗（页面横幅 / 应用内弹层 / 登录页重定向）；
    这里用一段醒目的文本代替。关键是**别默默忽略**。
    """
    with _client_for(base_url, httpx.Timeout(10.0)) as c:
        r = c.get(f"{base_url}/v1/auth/me", headers=_headers(token))
        if r.status_code != 200:
            _die(r.status_code, r.text)
        me = r.json()
    print(json.dumps(me, ensure_ascii=False, indent=2))
    if me.get("relogin_required"):
        print("\n" + "!" * 64)
        print(f"请重新登录：{me.get('relogin_reason')}")
        print("!" * 64)


def login(base_url: str, user: str) -> None:
    """用账号密码换 JWT —— **这是唯一的拿凭证方式**（共享密钥路径已整体删除）。

    令牌比曾经的共享密钥多三样东西，而且都是**载荷带来的**：
      - 身份：`sub` 在令牌里 ⇒ 审计日志与审核留痕能归到人；
      - 可撤销：令牌在服务端会话白名单里 ⇒ 能单独作废，不必"改密钥 + 重启"；
      - 租户不可自报：`tenant` 来自已验签的载荷 ⇒ 请求头改不动它。

    真实客户端要把 `refresh_token` 存起来（长期、单次使用、每次刷新轮换），
    本示例只打印 `access_token`：refresh token 是长期凭证，不该在终端回显。
    """
    password = os.getenv("AGENT_SCRIPT_PASSWORD", "")
    if not user or not password:
        raise SystemExit(
            "缺少账号或密码。用法：AGENT_SCRIPT_PASSWORD='...' \\\n"
            "    python examples/http_client.py login --user <账号>\n"
            "密码刻意只从环境变量读（明文进 shell history 与 ps 输出是最常见的泄漏方式）。\n"
            "还没有服务账号：`python -m scripts.hash_password -u svc-loadtest -t <租户>`"
            "（不给 -r 即**无角色**）。"
        )
    body = {"username": user, "password": password}
    with _client_for(base_url, httpx.Timeout(15.0)) as client:
        r = client.post(f"{base_url}/v1/auth/login", json=body)
    if r.status_code != 200:
        _die(r.status_code, r.text)
    data = r.json()
    print(f"已登录：user={data.get('user')} tenant={data.get('tenant') or '（无）'} "
          f"roles={data.get('roles')} 有效期={data.get('expires_in')}s")
    print()
    print("access_token：")
    print(data.get("access_token", ""))
    print()
    print("接着用它调其它子命令，例如：")
    print(f"    python examples/http_client.py whoami --token {data.get('access_token', '')[:16]}…")


def _die(status: int, body: str) -> None:
    """把 4xx/5xx 讲清楚：这些状态码各自对应一种配置问题，直接报码很难排查。"""
    hint = {
        401: "缺少凭证或凭证无效（没带 --token、令牌已过期、或已被登出/撤销）；"
             "用 login 子命令重新换一张",
        403: "权限不足：需要对应角色（审稿/管理），或该会话属于别的租户或用户",
        404: "路径不对（旧版服务只有 /chat，不带 /v1 前缀）",
        409: "该会话当前不在人工审核状态（已处理过，或从未触发低分）",
        429: "触发限流或当日配额已用尽（响应头 Retry-After 有建议等待秒数）",
        503: "服务端没配会话签名密钥（AUTH_SESSION_SECRET）—— 属于部署问题",
    }.get(status, "")
    print(f"\n[HTTP {status}] {hint}\n{body[:500]}", file=sys.stderr)
    raise SystemExit(1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="chatAgent HTTP 客户端示例")
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--token", default="", help="Bearer 令牌（来自 login 子命令）")
    sub = ap.add_subparsers(dest="cmd")

    lg = sub.add_parser("login", help="用账号密码换 JWT（推荐路径）")
    lg.add_argument("--user", default=os.getenv("AGENT_SCRIPT_USER", ""),
                    help="账号（默认取 AGENT_SCRIPT_USER）")

    c = sub.add_parser("chat", help="流式对话")
    c.add_argument("--thread-id", default="demo-thread")
    c.add_argument("--message", default="请介绍一下这份资料的主要内容")

    v = sub.add_parser("review", help="提交人工审核")
    v.add_argument("--thread-id", required=True)
    v.add_argument("--action", choices=("accept", "revise"), default="accept")
    v.add_argument("--feedback", default="")

    sub.add_parser("reviews", help="列出待审会话")
    sub.add_parser("whoami", help="查当前身份（含「该重新登录了」的提示）")

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 1

    if args.cmd == "login":
        login(args.base_url, args.user)
    elif args.cmd == "chat":
        chat(args.base_url, args.token, args.thread_id, args.message)
    elif args.cmd == "review":
        review(args.base_url, args.token, args.thread_id,
               args.action, args.feedback)
    elif args.cmd == "reviews":
        reviews(args.base_url, args.token)
    elif args.cmd == "whoami":
        whoami(args.base_url, args.token)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
