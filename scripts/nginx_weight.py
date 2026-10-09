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

作用范围（默认改**全部**存在的入口配置）
--------------------------------------
本仓库有两份入口配置，各写了一份 upstream 定义：
`deploy/app/nginx.conf`（HTTP/内网）与 `deploy/app/nginx.tls.conf`（公网 TLS）。
实际生效的是部署时挂载的那一份（`docker-compose.yml` 挂前者，
`docker-compose.tls.yml` 以 `!override` 挂后者）。

所以默认**同时**读写这两份（存在的都算），用 `--conf` 或环境变量 `NGINX_CONF`
可以只操作其中一份。这条默认值是 2026-10-06 的一次事故换来的，详见
`_resolve_confs` 的说明。

退出码
------
    0  正常
    1  参数不合法（负数、两个都为 0 等）
    2  文件里找不到预期的 server 行（配置被改过？）
    3  写入后读回校验不一致（磁盘/权限问题），或**多份配置之间权重不一致**
"""

from __future__ import annotations

import argparse
import os
import re
import sys

RC_OK, RC_BAD_ARGS, RC_NOT_FOUND, RC_VERIFY_FAILED = 0, 1, 2, 3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 默认要操作的入口配置候选（存在的都会被读写）。
CONF_CANDIDATES = (
    os.path.join(ROOT, "deploy", "app", "nginx.conf"),
    os.path.join(ROOT, "deploy", "app", "nginx.tls.conf"),
)


def _resolve_confs(explicit: str | None) -> list[str]:
    """决定本次读写哪些配置文件。

    explicit 非空（`--conf` 或环境变量 `NGINX_CONF`）⇒ 只用它（兼容单文件用法）。
    否则 ⇒ **所有存在的**入口配置。

    为什么默认是"全部"而不是"第一份"（2026-10-06 修）
    -----------------------------------------------
    本脚本原先只改 `nginx.conf`。而生产对公网用的是 `nginx.tls.conf`
    （`docker-compose.tls.yml` 以 `!override` 把它挂进 conf.d）—— 于是 TLS 部署下
    `release.sh` 的逐档放量（1/10/50/100%）**一次都没有生效过**，nginx 始终停在
    9:1；更糟的是 `show` 会回显那个"被改了、但容器根本没读"的文件，给出
    **"已切换"的假象**。灰度是发布安全的最后一道闸门，它静默失效属于本仓库
    最不该有的失败模式。

    改成"两份都改"之后，配置值只有一个事实来源，也不需要运维记得传对参数。
    """
    if explicit:
        return [explicit]
    found = [p for p in CONF_CANDIDATES if os.path.exists(p)]
    if not found:
        print("找不到任何 nginx 配置文件，已找：\n  " + "\n  ".join(CONF_CANDIDATES),
              file=sys.stderr)
        raise SystemExit(RC_NOT_FOUND)
    return found


def _display(path: str) -> str:
    """把路径显示成相对仓库根的短形式 —— 但**跨盘符时退回绝对路径**。

    `os.path.relpath` 在 Windows 上遇到"两个路径不在同一盘符"会抛
    `ValueError: path is on mount 'C:', start on mount 'D:'`。
    这个函数只用来打印"改动了哪几份配置"，可它在仓库所在盘之外的路径上
    （`--conf` 指向别处的一份拷贝、或测试给的是系统临时目录里的 `tmp_path`）
    **会把整个命令崩掉** —— 而权重其实**已经写好**了。调用方看到非零退出码，
    会误判成"没生效"然后重试，属于"成功却报失败"。

    2026-10-06 实际踩到：`pytest.ini` 打开 `testpaths` 后，裸 `pytest` 的
    basetemp 落在 `C:\\Users\\...\\Temp`，三条 release 工具链用例因此变红。
    """
    try:
        return os.path.relpath(path, ROOT)
    except ValueError:      # 不同盘符（Windows 独有）
        return path


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
        print(f"[dry-run] 不会写盘。{os.path.basename(conf)} 变更后的 upstream 段：")
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
    ap.add_argument("--conf", default=os.environ.get("NGINX_CONF") or None,
                    help="只操作这一份配置；默认读写 deploy/app 下**所有存在的** nginx 配置"
                         "（http 版与 tls 版各有一份 upstream 定义，见模块 docstring）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show", help="打印当前权重")

    s = sub.add_parser("set", help="设置权重")
    s.add_argument("--stable", type=int, default=None, help="app-stable 的权重")
    s.add_argument("--canary", type=int, default=None, help="app-canary 的权重")
    s.add_argument("--dry-run", action="store_true", help="只打印不写盘")

    args = ap.parse_args(argv)
    confs = _resolve_confs(args.conf)

    if args.cmd == "show":
        seen: set[tuple[int, int]] = set()
        rc = RC_OK
        for conf in confs:
            w = read_weights(conf)
            label = _display(conf)
            total = w["stable"] + w["canary"]
            if total <= 0:
                print(f"{label}: stable={w['stable']} canary={w['canary']}  ⚠️ 总权重为 0："
                      "nginx 会把所有请求判为不可用（500），这不可能是你想要的状态")
                rc = RC_BAD_ARGS
            else:
                pct = 100.0 * w["canary"] / total
                print(f"{label}: stable={w['stable']}  canary={w['canary']}"
                      f"  → canary 约占 {pct:.1f}%")
            seen.add((w["stable"], w["canary"]))
        if len(seen) > 1:
            # 不一致时不能给一个"当前权重是 X"的结论 —— 实际值取决于挂了哪份配置
            print("⚠️ 多份入口配置的权重**不一致**：实际生效的取决于部署时挂载了哪一份，"
                  "此时上面的读数**不可信**。请用 `set` 统一它们（默认就会同时改这两份）。",
                  file=sys.stderr)
            return RC_VERIFY_FAILED
        return rc

    if args.stable is None and args.canary is None:
        ap.error("至少要给 --stable 或 --canary 之一")

    cur_list = [(conf, read_weights(conf)) for conf in confs]

    # 没显式给的那一侧必须**各文件一致**：否则"保持不变"本身就是歧义的
    # （A 文件里是 9、B 文件里是 50，那"保持 stable 不变"到底保持哪个？）
    for key, given in (("stable", args.stable), ("canary", args.canary)):
        if given is None:
            vals = {w[key] for _, w in cur_list}
            if len(vals) > 1:
                print(f"--{key} 未指定，而各配置当前值不一致：{sorted(vals)}。\n"
                      f"请显式给出 --{key}（或先跑一次 set 把它们统一）。", file=sys.stderr)
                return RC_VERIFY_FAILED

    base = cur_list[0][1]
    want = {
        "stable": base["stable"] if args.stable is None else args.stable,
        "canary": base["canary"] if args.canary is None else args.canary,
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

    for conf in confs:
        write_weights(conf, want, dry_run=args.dry_run)
    if not args.dry_run:
        total = want["stable"] + want["canary"]
        print(f"权重已更新：stable={want['stable']} canary={want['canary']}"
              f"（canary 约 {100.0 * want['canary'] / total:.1f}%）"
              f"；已覆盖 {len(confs)} 份配置："
              f"{'、'.join(_display(c) for c in confs)}")
        print("⚠️ 还要让 nginx 重新加载才会生效：")
        print("   docker compose --env-file .env -f deploy/app/docker-compose.yml "
              "exec nginx nginx -s reload")
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
