#!/usr/bin/env python
"""把仓库根 `VERSION` 文件的版本号同步到所有"副本"上。

为什么需要这个脚本
------------------
版本号曾经同时写在三个地方：`app/config.py` 的默认值、`.env`、compose 的镜像 tag。
改一处漏两处时，**"服务自报的版本"与"实际跑的镜像 tag"会不一致，而且没有任何信号**
—— `/health` 照常回 200，只是回了一个错的版本号；排查"线上到底是哪一版"时会得出
错误结论。这比明确报"不知道"更糟。

现在唯一真源是仓库根的 `VERSION`，其余位置一律当**副本**看：

    VERSION                                  ← 唯一真源（改这里）
      ├── deploy/app/docker-compose.yml      镜像 tag 的默认值   （本脚本同步）
      └── .env.example                       `APP_VERSION=` 示例值（本脚本同步）

`app/config.py` 不存副本，它**直接读** `VERSION` 文件（环境变量优先）。
`tests/test_version_source.py` 在 CI 里断言"副本 == 真源"，所以忘了跑本脚本不会漏到
线上 —— 但跑一下更省事、也能立刻看到哪几处会被改。

用法
----
    python scripts/sync_version.py            # 写入
    python scripts/sync_version.py --check    # 只检查（CI / 测试用），有漂移则退出 1
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"

# 退出码：0 一致/已同步，1 有漂移（--check 时）或副本数量不符，2 VERSION 文件本身不合法
#
# ⚠️ 「数量不符」也归到 1（而不是新开一个码）：对调用方（`make check`、发布脚本、
#    CI）来说，这件事的含义就是"版本号副本的状态不可信，停下来"，与"有漂移"同级。
#    另立一个码只会让每个调用点都多一条 if，而漏掉任何一条就等于这个守卫不存在。
RC_OK = 0
RC_DRIFT = 1
RC_BAD_VERSION = 2

# 语义化版本（允许 -rc1 / +build 之类的后缀）。这里刻意**不接受** `latest`、`dev`
# 这种流动标签 —— 它们让"线上跑的是哪一版"重新变成不可回答的问题。
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")

# compose 里**镜像行**上的 `${APP_VERSION:-0.2.0}`。
#
# ⚠️ 必须锚定 `image:` 行首 —— 文件里还有解释性注释写着 `${APP_VERSION:-<版本>}`
#    作为示例；不锚定的话那个占位符会被当成"一处待同步的副本"（实测：会报
#    "匹配到 5 处，预期 4 处"，并试图把 `<版本>` 同步成真版本号）。
#
# `\S*?` 非贪婪且不含换行，所以能正确穿过嵌套写法
# `image: ${CANARY_IMAGE:-chatagent:${APP_VERSION:-0.2.0}}`，只匹配到内层那个。
# group(1) = 需要原样保留的前缀，group(2) = 当前的版本值。
COMPOSE_RE = re.compile(
    r"(?m)^([ \t]*image:[ \t]*\S*?)\$\{APP_VERSION:-([^}]*)\}"
)
# `.env` / `.env.example` 里的赋值行。同样两个组（前缀 / 值），与上面统一，
# 这样"取副本当前值"和"替换"两处逻辑都能共用同一段代码。
# `[^\r\n]*` 是必须的：用 `.*` 在 `(?m)` 下会把 CRLF 的 `\r` 吃进值里，
# 于是"看起来一样"的两份文件被判成不一致（或更糟，把 `\r` 写进值里）。这是踩过的坑。
DOTENV_RE = re.compile(r"(?m)^(APP_VERSION=)([^\r\n]*)")

# (路径, 定位正则, 渲染函数, 预期副本数)
# 预期副本数是**故意的**：文件结构变了而副本数没跟着变，说明要么有人绕过真源手写了
# 一个版本号，要么本脚本该更新了 —— 两种情况都该被看见，而不是静默放过。
TARGETS = [
    (
        ROOT / "deploy" / "app" / "docker-compose.yml",
        COMPOSE_RE,
        lambda m, ver: m.group(1) + "${APP_VERSION:-" + ver + "}",
        # 5 处 = app-stable / app-canary / alert-sink / retention-sweep / pg-stats。
        # 后两个是 `1a0c360` 部署落地时加的，当时这个数字还停在 3 个服务时代的 4。
        # ⚠️ 加**任何**带 `image:` + `${APP_VERSION:-...}` 的服务都要同步改这里，
        #    否则 `--check` 会红 —— 这是有意的：宁可让人确认一次，
        #    也不要让某个服务的镜像 tag 永久停在旧版本号上且零报错。
        #    守卫：`tests/test_version_source.py::test_compose_copy_count_matches_declaration`
        5,
    ),
    (
        ROOT / ".env.example",
        DOTENV_RE,
        lambda m, ver: m.group(1) + ver,
        1,
    ),
]


def copy_values(pattern: re.Pattern, text: str) -> list[str]:
    """取所有副本的**当前值**（正则的最后一个捕获组）。守卫直接用它做断言。"""
    return [m[-1] for m in pattern.findall(text)]


def read_version() -> str:
    """读并校验唯一真源。不合法就**立刻退出**，不猜一个能跑的值。"""
    try:
        raw = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        print(f"[FATAL] 读不到唯一真源 {VERSION_FILE}：{exc}")
        sys.exit(RC_BAD_VERSION)
    if not SEMVER_RE.match(raw):
        print(
            f"[FATAL] {VERSION_FILE.name} 的内容不是合法版本号：{raw!r}；"
            f"应形如 1.2.3 或 1.2.3-rc1"
        )
        sys.exit(RC_BAD_VERSION)
    return raw


def sync_file(path: Path, pattern: re.Pattern, render, expected: int, ver: str, check: bool):
    """把 `path` 里所有副本改成 `ver`。返回 (是否改动, 旧值集合, 匹配数)。

    ⚠️ `expected` 是**有约束力的参数**，不是提示：数量不符 ⇒ 返回 0 以外的信号
       （`main` 据此判定失败）。此前它只用来 print 一句话，于是"正则与文件写法脱节、
       某一处副本再也不被匹配"这种失效**三道守卫全绿**照样溜过去 —— 那处副本会
       永久停在旧版本号，且零报错。
    """
    rel = path.relative_to(ROOT)
    try:
        raw = path.read_bytes().decode("utf-8")
    except OSError as exc:
        print(f"[FATAL] 读不到 {rel}：{exc}")
        sys.exit(RC_BAD_VERSION)

    old_values = copy_values(pattern, raw)
    new_raw, n = pattern.subn(lambda m: render(m, ver), raw)

    if n != expected:
        print(
            f"[WARN] {rel}：匹配到 {n} 处副本，预期 {expected} 处 —— "
            f"文件结构变了？请同步更新 {Path(__file__).name} 的 TARGETS"
        )

    changed = new_raw != raw
    if changed and not check:
        # 按字节写回：decode/encode 不会碰 `\r\n`，行尾原样保留
        path.write_bytes(new_raw.encode("utf-8"))
    return changed, set(old_values), n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="把 VERSION 同步到 compose 镜像 tag 与 .env.example",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--check", action="store_true", help="只检查，不写文件（有漂移退出 1）")
    args = ap.parse_args(argv)

    ver = read_version()
    drifted: list[str] = []
    # 副本数量不符的文件（正则与文件写法可能已脱节）。这是**独立于"值是否一致"**
    # 的一类失效：数量少了 ⇒ 少掉的那处永不同步，而剩下的值全对 ⇒ 值比对看不出问题。
    miscounted: list[str] = []

    for path, pattern, render, expected in TARGETS:
        changed, old_values, n = sync_file(path, pattern, render, expected, ver, args.check)
        rel = path.relative_to(ROOT)
        if n != expected:
            miscounted.append(f"{rel}（实际 {n} 处，预期 {expected} 处）")
        if changed:
            stale = sorted(v for v in old_values if v != ver)
            drifted.append(f"{rel}（旧值 {stale}）")
            verb = "有漂移" if args.check else "已同步"
            print(f"[{'ERROR' if args.check else ' OK '}] {verb}：{rel} → {ver}")

    if args.check and (drifted or miscounted):
        if drifted:
            print(f"\n[FAIL] {len(drifted)} 处副本与 VERSION({ver}) 不一致：")
            for d in drifted:
                print("  - " + d)
        if miscounted:
            print(
                f"\n[FAIL] {len(miscounted)} 个文件的副本数量与预期不符 —— "
                f"正则可能已与文件写法脱节，少掉的那处不会被同步："
            )
            for d in miscounted:
                print("  - " + d)
            print(
                "\n修法：确认 compose / .env.example 的结构变化是**有意的**，"
                "然后同步更新 scripts/sync_version.py 的 TARGETS 里的预期数量。"
            )
        if drifted:
            print("\n修法：python scripts/sync_version.py")
        return RC_DRIFT

    if not drifted and not miscounted:
        print(f"[ OK ] 版本一致：{ver}（检查了 {len(TARGETS)} 个副本文件，无需改动）")
    elif drifted:
        print(f"[ OK ] 已同步 {len(drifted)} 个文件到 {ver}")
    else:
        # 写模式 + 只有数量问题：值已同步好，但结构仍需人工确认，所以不算失败
        # （写模式的职责只是把值改对；数量是给人看的告警）。
        print("[ OK ] 已同步；但副本数量与预期不符，见上方 WARN（写入模式不判失败）")
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
