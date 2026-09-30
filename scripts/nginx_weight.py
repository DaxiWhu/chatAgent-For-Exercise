#!/usr/bin/env python
"""读写 `deploy/app/nginx.conf` 里的灰度权重。

为什么单独做成脚本
----------------
权重是"发布"与"回滚"唯一真正要改的东西，而改错的表现**极其安静**：

  - 匹配不到那一行时，`sed` 的默认行为是**打印原文、静默不动** ⇒
    你以为流量切了 100%，实际还是 9:1（只体现在"灰度验证没意义"上）；
  - 写成了非数字（`weight=x`）⇒ nginx 启动/重载**直接失败**，
    而此时你可能正在回滚，压力最大。

所以这里做三件 sed 做不到的事：
  ① **改不到就报错退出**（内置"必须命中两次"的断言）；
  ② 改完**立即校验**（把权重再读回来比对）；
  ③ 用 Python 而不是 sed，跨平台行为一致（同 `scripts/pg_backup.py` 的取向）。

用法
----
    python scripts/nginx_weight.py show
    python scripts/nginx_weight.py set --stable 90 --canary 10
    python scripts/nginx_weight.py set --canary 0          # 等价于收回灰度流量
    python scripts/nginx_weight.py set --stable 99 --canary 1 --dry-run

退出码
------
    0  正常
    1  参数不合法（负数、两个都为 0 等）
    2  文件里找不到预期的 server 行（配置被改过？）
    3  写入后读回校验不一致（磁盘/权限问题）
"""

from __future__ import annotations

import argparse
import os
import re
import sys

RC_OK, RC_BAD_ARGS, RC_NOT_FOUND, RC_VERIFY_FAILED = 0, 1, 2, 3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONF = os.path.join(ROOT, "deploy", "app", "nginx.conf")

#: 只匹配 `server <host>:<port> ... weight=<数字> ...;` 这一行里的 weight 段。
#: 刻意把 host 写进正则：文件里还有其它 upstream（将来可能加），
#: 只按 `weight=` 匹配会在加第二个 upstream 时**改错地方**。
_SERVER_RE = {
    "stable": re.compile(r"(?m)^(\s*server\s+app-stable:\d+\b[^;]*?weight=)(\d+)(\b[^;]*;)$"),
    "canary": re.compile(r"(?m)^(\s*server\s+app-canary:\d+\b[^;]*?weight=)(\d+)(\b[^;]*;)$"),
}


def read_weights(conf: str) -> dict[str, int]:
    """读出当前的 stable / canary 权重。找不到就抛 SystemExit(2)。"""
    text = _read(conf)
    out: dict[str, int] = {}
    for name, rx in _SERVER_RE.items():
        m = rx.search(text)
        if not m:
            print(
                f"在 {conf} 里找不到 app-{name} 的 `weight=` 声明。\n"
                "可能原因：upstream 名字改了、把 weight 写成了别的形式、或这段被删了。\n"
                "**不要在这里猜**：本脚本拒绝在'没命中'的情况下继续 —— 那会让发布流程"
                "以为权重已经改掉，而实际没有任何变化（灰度/回滚全部失去意义）。",
                file=sys.stderr,
            )
            raise SystemExit(RC_NOT_FOUND)
        out[name] = int(m.group(2))
    return out


def write_weights(conf: str, weights: dict[str, int], dry_run: bool = False) -> str:
    text = _read(conf)
    for name, val in weights.items():
        rx = _SERVER_RE[name]
        new_text, n = rx.subn(lambda m, v=val: f"{m.group(1)}{v}{m.group(3)}", text)
        if n != 1:
            print(f"写入 app-{name} 时命中了 {n} 行（预期恰好 1 行）—— 拒绝写入。",
                  file=sys.stderr)
            raise SystemExit(RC_NOT_FOUND)
        text = new_text

    if dry_run:
        print("[dry-run] 不会写盘。变更后的 upstream 段：")
        for line in text.splitlines():
            if "app-stable:" in line or "app-canary:" in line:
                print("   " + line)
        return text

    with open(conf, "w", encoding="utf-8", newline="") as f:
        f.write(text)

    # 读回校验：写完立刻证明"磁盘上真的是这个值"
    got = read_weights(conf)
    if got != weights:
        print(f"写入后校验失败：期望 {weights}，磁盘上是 {got}", file=sys.stderr)
        raise SystemExit(RC_VERIFY_FAILED)
    return text


def _read(conf: str) -> str:
    if not os.path.exists(conf):
        print(f"找不到 nginx 配置：{conf}", file=sys.stderr)
        raise SystemExit(RC_NOT_FOUND)
    with open(conf, encoding="utf-8", newline="") as f:
        return f.read()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--conf", default=os.environ.get("NGINX_CONF") or DEFAULT_CONF,
                    help="nginx 配置文件路径（默认 deploy/app/nginx.conf）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show", help="打印当前权重")

    s = sub.add_parser("set", help="设置权重")
    s.add_argument("--stable", type=int, default=None, help="app-stable 的权重")
    s.add_argument("--canary", type=int, default=None, help="app-canary 的权重")
    s.add_argument("--dry-run", action="store_true", help="只打印不写盘")

    args = ap.parse_args(argv)

    if args.cmd == "show":
        w = read_weights(args.conf)
        total = w["stable"] + w["canary"]
        if total <= 0:
            print(f"stable={w['stable']} canary={w['canary']}  ⚠️ 总权重为 0："
                  "nginx 会把所有请求判为不可用（500），这不可能是你想要的状态")
            return RC_BAD_ARGS
        pct = 100.0 * w["canary"] / total
        print(f"stable={w['stable']}  canary={w['canary']}  → canary 约占 {pct:.1f}%")
        return RC_OK

    if args.stable is None and args.canary is None:
        ap.error("至少要给 --stable 或 --canary 之一")

    cur = read_weights(args.conf)
    want = {
        "stable": cur["stable"] if args.stable is None else args.stable,
        "canary": cur["canary"] if args.canary is None else args.canary,
    }

    for k, v in want.items():
        if v < 0:
            print(f"--{k} 不能为负（给了 {v}）", file=sys.stderr)
            return RC_BAD_ARGS
    if want["stable"] + want["canary"] <= 0:
        print(
            "stable 与 canary 不能同时为 0 —— nginx 会把所有请求判为 'no live upstreams' "
            "并返回 502，等于把一个'改配置'动作变成一次**全站故障**。",
            file=sys.stderr,
        )
        return RC_BAD_ARGS

    write_weights(args.conf, want, dry_run=args.dry_run)
    if not args.dry_run:
        total = want["stable"] + want["canary"]
        print(f"权重已更新：stable={want['stable']} canary={want['canary']}"
              f"（canary 约 {100.0 * want['canary'] / total:.1f}%）")
        print("⚠️ 还要让 nginx 重新加载才会生效：")
        print("   docker compose --env-file .env -f deploy/app/docker-compose.yml "
              "exec nginx nginx -s reload")
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
