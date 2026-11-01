"""模型网关能力探针 —— 一次性问清"这把 key 能用吗、三档结构化输出支持到哪一档"。

为什么需要它
------------
``AGENT_STRUCTURED_OUTPUT`` 有三档（``json_schema`` / ``json_object`` / ``none``），
选错的表现是**静默**的：三个专家工具每次 400，主链路照样"成功"（模型会道歉并给
替代方案），评测里出现"工具选择 12/12 + 工具执行 0/3"这种自相矛盾的画面。
2026-10-31 就是这样丢掉了一整轮数据，两天后才定位。

而这件事**不能靠查文档决定**：同一家网关不同模型的支持面不一样，文档也常落后于
部署。只能发最小请求实测。

三类失败必须分开看（合并会把诊断带偏）
------------------------------------
* **401 / 403** —— 凭证问题。此时问"支持哪一档"**没有意义**，换 key 才能答。
  混进"档位不支持"里，就会得出"三档全废、该换网关"的错误结论。
* **400 + unavailable** —— 网关**明确拒绝**这一档，可以据此降档。
* **连不上 / 超时** —— 网络或地址问题，与档位无关。

一个真踩过的假阴性
------------------
2026-10-31 探 ``json_object`` 收到 400，差点判成"档位不可用"，真实原因是提示词里
没有 "JSON" 字样 —— 那是 ``response_format=json_object`` 的硬要求（官方口径，见
``src/agents/model.py::_output_format_note`` 第 1 条）。所以本脚本的探针提示词
**必须**含 "JSON"，否则它自己在制造假阴性。守卫：
``tests/test_probe_provider.py::test_probe_prompt_mentions_json``。

用法
----
    python scripts/probe_provider.py                    # 探 .env 里配置的所有网关
    python scripts/probe_provider.py --provider main    # 只探主网关
    python scripts/probe_provider.py --json out.json    # 留证（换 key 前后各存一份）

⚠️ 只读：发 4 次以内最小请求（max_tokens=32），不写任何配置。建议值只打印，
是否落到 .env 由人决定 —— 档位影响生产行为，不该由脚本自己改。

⚠️ 默认**不走系统代理**。本机 ``HTTP_PROXY`` 指向一个每次重启都换端口的本地代理，
远端请求经它转一手会让失败原因变得难以区分（是代理挂了还是网关拒了）。需要代理
出网时加 ``--use-proxy``。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # 与 app/main.py 同一手法：脚本自己把 .env 读进来
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001  .env 缺失时照常跑（用真实环境变量）
    pass


MODES = ("json_schema", "json_object", "none")

#: 要探的网关。**默认 base/model 必须与代码里的默认值一致**，否则探出来的结论描述
#: 的是另一个模型：主网关看 ``app/config.py``，评测对照线看 ``scripts/agent_tool_eval.py``。
#:
#: ``main`` 取代了原来的 ``agnes`` 一项：变量名已从 ``LLM_*`` 中性化，网关可以换，
#: 所以这里按"它在系统里的角色"命名，而不是按某一家厂商命名。
_GATEWAYS: dict[str, dict[str, str]] = {
    "main": {"prefix": "LLM", "base": "https://api.deepseek.com", "model": "deepseek-chat"},
    "deepseek": {"prefix": "DEEPSEEK", "base": "https://api.deepseek.com", "model": "deepseek-chat"},
}

#: 探针提示词。两个约束都不能省：含 "JSON"（json_object 的硬要求）、极短（只关心
#: HTTP 状态，不关心内容，也不花钱）。
_PROBE_PROMPT = '只输出一个 JSON 对象：{"ok":"yes"}。不要任何别的字。'

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "string"}},
    "required": ["ok"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# 纯逻辑（可单测、可变异）
# ---------------------------------------------------------------------------
def classify(status: int) -> str:
    """把一次探针的 HTTP 状态归成四类。

    为什么必须分开：四类要采取的动作完全不同 —— 换 key / 降档 / 查网络 / 啥也别动。
    合成一个"成功/失败"布尔值，就把"下一步做什么"这件事丢掉了。

    status: HTTP 状态码；``0`` 表示请求根本没发出去或没拿到响应（连接失败、超时）。

    返回 ``ok`` / ``bad_key`` / ``unreachable`` / ``rejected`` 之一。
    """
    if status == 200:
        return "ok"
    if status in (401, 403):
        return "bad_key"
    if status == 0:
        return "unreachable"
    return "rejected"


def decide_mode(support: dict[str, bool | None]) -> tuple[str | None, str]:
    """按三档实测结果给出建议值。

    单调性是这个函数成立的前提：``json_schema`` 是 ``json_object`` 的**超集要求**，
    支持前者必然支持后者；连 ``json_object`` 都不支持，说明网关对 ``response_format``
    整体不支持。所以三档按强度从高到低取第一个通过的即可，不需要排列组合。

    support: ``{"json_schema": True/False/None, "json_object": ..., "none": ...}``；
        ``None`` 表示**没测到**（key 无效 / 连不上），与 ``False``（网关明确拒绝）
        是两件事，不能混。

    返回 ``(建议写入 .env 的值 或 None, 理由)``。``None`` 表示"别动，没数据支撑"。
    """
    if support.get("json_schema") is True:
        return None, "支持 json_schema —— 用默认档（不设这个变量），schema 由 provider 强制"
    if support.get("json_object") is True:
        return (
            "json_object",
            "只支持 json_object —— schema 改由提示词说明，最终文本仍由 SDK 校验；"
            "失败形态从 400 变成 bad_args",
        )
    if support.get("none") is True:
        return "none", "只支持不带 response_format —— 设 none，结构化全靠提示词 + SDK 校验"
    if any(value is None for value in support.values()):
        return None, "有档位没测到（凭证或网络问题），先修通再定档"
    return None, "三档全被拒 —— 网关不提供结构化输出，或模型名/地址不对，先查这两处"


def build_payload(mode: str, model: str) -> dict[str, Any]:
    """构造某一档的探针请求体。

    ``temperature`` 刻意不传：探针只关心 ``response_format`` 是否被接受，多传一个
    参数就多一个可能被拒的理由，而失败原因会混在一起、分不清是档位还是参数。
    这是"一次只动一个变量"。

    mode: ``json_schema`` / ``json_object`` / ``none``。
    model: 模型名。
    """
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": _PROBE_PROMPT}],
        "max_tokens": 32,
    }
    if mode == "json_schema":
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "probe", "schema": _SCHEMA, "strict": True},
        }
    elif mode == "json_object":
        body["response_format"] = {"type": "json_object"}
    return body


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _post(url: str, key: str, body: dict[str, Any], timeout: float, *,
          use_proxy: bool) -> tuple[int, str]:
    """发一次 JSON POST，**任何失败都折算成 (状态码, 摘要)**，不抛。

    为什么不抛：探针的产出是一张对照表，单点异常不该中断整轮探测 —— 那正是
    "一家 401 就把三家都探不出来"的成因。

    url: 完整端点（已带 ``/chat/completions``）。
    key: Bearer 令牌。
    body: 请求体。
    timeout: 单个请求的秒数上限。
    use_proxy: 是否让系统代理生效（默认 False，理由见模块 docstring）。

    返回 ``(HTTP 状态码 或 0, 响应体前 300 字)``。
    """
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    handlers = [] if use_proxy else [urllib.request.ProxyHandler({})]
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")[:300]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:300]
    except Exception as exc:  # noqa: BLE001  连接失败/超时/DNS 都归这一类
        return 0, f"{type(exc).__name__}: {exc}"


def _endpoint(base: str) -> str:
    """把 base 拼成 chat/completions 端点。

    ``.env`` 里不同网关的 base 写法可能不同（有的带 ``/v1``、有的不带），两者都合法，
    所以这里只补尾部、不猜前缀：已经以 ``/chat/completions`` 结尾就原样返回。

    base: 网关根地址。
    """
    trimmed = (base or "").rstrip("/")
    if not trimmed:
        return ""
    if trimmed.endswith("/chat/completions"):
        return trimmed
    return f"{trimmed}/chat/completions"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _provider_rows(only: str) -> list[dict[str, str]]:
    """按 ``--provider`` 收集要探的网关（只收配置了 key 的）。

    同一地址 + 同一模型只留第一行：主网关（``LLM_*``）与评测对照线（``DEEPSEEK_*``）
    可能指向同一家（2026-11 起就是如此）。探两遍既费额度，又会让表里出现两行相同
    结论 —— 更糟的是名字不同会让人以为"两家都验过了"。
    """
    rows: list[dict[str, str]] = []
    seen: dict[tuple[str, str], str] = {}
    for name, spec in _GATEWAYS.items():
        if only and name != only:
            continue
        prefix = spec["prefix"]
        key = (os.getenv(f"{prefix}_API_KEY", "") or "").strip()
        if not key:
            continue
        base = (os.getenv(f"{prefix}_API_BASE", "") or "").strip() or spec["base"]
        model = (os.getenv(f"{prefix}_MODEL", "") or "").strip() or spec["model"]
        row = {"name": name, "key": key, "base": base, "model": model}
        first = seen.get((base, model))
        if first:
            row["duplicate_of"] = first
        else:
            seen[(base, model)] = name
        rows.append(row)
    return rows


def probe(row: dict[str, str], *, timeout: float, use_proxy: bool) -> dict[str, Any]:
    """探一家网关：先探 key 有效性，有效才继续探三档。

    顺序不能颠倒：key 无效时每一档都会拿到 401，把 401 记成"这一档不支持"就会
    得出"三档全废"的假结论（``classify`` 就是为了让这件事不会发生）。

    row: ``_provider_rows`` 的一项。
    timeout / use_proxy: 透传给 ``_post``。

    返回 ``{"name","base","model","key_status","modes":{mode: True/False/None},"detail"}``。
    """
    url = _endpoint(row["base"])
    result: dict[str, Any] = {
        "name": row["name"],
        "base": row["base"],
        "model": row["model"],
        "key_status": "",
        "key_detail": "",
        "modes": {},
        "detail": {},
    }
    if not url:
        result["key_status"] = "unreachable"
        result["key_detail"] = "base 为空"
        return result

    # 第一步：不带 response_format 的最小请求 —— 只验凭证与连通性。
    status, body = _post(url, row["key"], build_payload("none", row["model"]), timeout,
                         use_proxy=use_proxy)
    kind = classify(status)
    result["key_status"] = kind
    result["key_detail"] = f"HTTP {status} {body}" if status else body
    if kind != "ok":
        # 凭证/网络未通：三档全部记 None（未测到），而不是 False（被拒）。
        result["modes"] = dict.fromkeys(MODES)
        return result

    # 第一步那次请求本身就是 none 档的实测结果，别浪费：留证要能对上每一行。
    result["modes"]["none"] = True
    result["detail"]["none"] = f"HTTP {status} {body}"
    for mode in ("json_schema", "json_object"):
        status, body = _post(url, row["key"], build_payload(mode, row["model"]), timeout,
                             use_proxy=use_proxy)
        kind = classify(status)
        result["modes"][mode] = kind == "ok"
        result["detail"][mode] = f"HTTP {status} {body}" if status else body
    return result


_MARK = {"ok": "[OK]", "bad_key": "[KEY]", "unreachable": "[NET]", "rejected": "[NO]"}


def _print_row(result: dict[str, Any], index: int, total: int) -> None:
    """打印一家网关的探测结果与建议（给人看的那一面）。"""
    print(f"[{index}/{total}] {result['name']}  {result['base']}  {result['model']}")
    kind = result["key_status"]
    print(f"      key 有效性 ........ {_MARK.get(kind, '[??]')} {result['key_detail']}")
    if kind != "ok":
        hint = {
            "bad_key": "凭证失效 —— 换 key 后重跑；此时问\"支持哪一档\"没有意义",
            "unreachable": "连不上 —— 查地址/网络（必要时 --use-proxy）",
            "rejected": "地址通但被拒 —— 多半是模型名或账号状态，看上面的响应体",
        }.get(kind, "未知")
        print(f"      => {hint}")
        print("      建议：AGENT_STRUCTURED_OUTPUT 暂不动 —— 没有数据支撑改它。")
        print()
        return
    for mode in MODES:
        supported = result["modes"].get(mode)
        mark = "[OK]" if supported else ("[--]" if supported is None else "[NO]")
        detail = result["detail"].get(mode, "")
        print(f"      {mode:<19} {mark} {detail}")
    mode, reason = decide_mode(result["modes"])
    print(f"      => 建议：{'（不设，用默认档）' if mode is None else f'AGENT_STRUCTURED_OUTPUT={mode}'}")
    print(f"         （{reason}）")
    print()


def main(argv: list[str] | None = None) -> int:
    """命令行入口。返回 0 = 至少一家探明，1 = 全部未探明（凭证/网络）。"""
    parser = argparse.ArgumentParser(description="探模型网关的 key 有效性与结构化输出档位支持面")
    parser.add_argument("--provider", choices=list(_GATEWAYS), help="只探一家（默认全部）")
    parser.add_argument("--timeout", type=float, default=30.0, help="单请求超时秒数")
    parser.add_argument("--use-proxy", action="store_true", help="让系统代理生效（默认直连）")
    parser.add_argument("--json", metavar="PATH", help="把结果写成 JSON（换 key 前后各存一份便于对比）")
    args = parser.parse_args(argv)

    rows = _provider_rows(args.provider or "")
    if not rows:
        print("没有可探的网关：.env 里没找到 LLM_API_KEY / DEEPSEEK_API_KEY。")
        return 1

    print("模型网关能力探针（只读，每家最多 3 次最小请求，max_tokens=32）")
    print("-" * 78)

    results = []
    for index, row in enumerate(rows, start=1):
        if row.get("duplicate_of"):
            # 同一地址 + 同一模型：再说一遍结论没有信息量，反而让人以为验了两家。
            print(f"[{index}/{len(rows)}] {row['name']}  与 {row['duplicate_of']} 同一地址"
                  f"与模型（{row['base']} / {row['model']}），跳过")
            print()
            continue
        result = probe(row, timeout=args.timeout, use_proxy=args.use_proxy)
        results.append(result)
        _print_row(result, index, len(rows))

    if args.json:
        Path(args.json).write_text(
            json.dumps({"results": results}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"已写入 {args.json}")

    explored = any(item["key_status"] == "ok" for item in results)
    if not explored:
        print("没有任何一家探明 —— 先解决凭证或网络，再回来定档。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
