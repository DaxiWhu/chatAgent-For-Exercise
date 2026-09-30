"""依赖锁定清单的生成与校验。

解决什么问题
------------
`requirements.txt` 全用 `>=`，同一份代码在不同时间构建会装出**不同的依赖树**。
这类事故的特点是：本地与线上行为不一致、但代码完全一样 —— 排查成本极高
（典型症状是"我这跑得好好的，线上就是不行"）。

本脚本做两件事
--------------
1. `--write`：从**当前已安装环境**读出真实版本，写成 `requirements.lock`。
   前提是这个环境已经验证可用（跑通了测试）。
2. `--check`（默认）：校验 `requirements.lock` 与 `requirements.txt` 是否漂移 ——
   即"清单里声明的每个包是否都有锁定版本"。CI 用这个当门禁。

为什么校验的是「漂移」而不是「装没装」
------------------------------------
CI 的 `test` job **刻意不装 docling/transformers**（详见 ci.yml 的说明），
所以"校验每个包都装了"会在 CI 里必然失败。正确的门禁是**静态一致性**：
requirements.txt 里出现的包名，requirements.lock 里必须有对应的精确版本。
至于"这个版本能不能装上"，由 Dockerfile 构建那一步负责验证。

用法
----
    python scripts/lock_requirements.py            # 校验（CI 门禁）
    python scripts/lock_requirements.py --write    # 重新生成
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQ = ROOT / "requirements.txt"
LOCK = ROOT / "requirements.lock"

# 包名归一化：PyPI 上 `Pdfminer.six` / `pdfminer-six` / `pdfminer.six` 是同一个包
_NORM = re.compile(r"[-_.]+")


def _norm(name: str) -> str:
    return _NORM.sub("-", name).lower()


def _parse_requirements(path: Path) -> list[tuple[str, str]]:
    """读出 (包名, 版本约束) 列表，跳过注释与空行。"""
    out: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]+\])?\s*(.*)$", line)
        if not m:
            continue
        out.append((m.group(1), (m.group(3) or "").strip()))
    return out


def _parse_lock(path: Path) -> dict[str, str]:
    """读出 {归一化包名: 精确版本}。"""
    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]+\])?==([^\s;]+)", line)
        if m:
            pins[_norm(m.group(1))] = m.group(3)
    return pins


def check() -> int:
    declared = _parse_requirements(REQ)
    pins = _parse_lock(LOCK)

    missing: list[str] = []
    unpinned: list[str] = []
    for name, spec in declared:
        key = _norm(name)
        if key not in pins:
            missing.append(name)
        elif not spec.startswith("=="):
            # 声明用的是范围约束，但 lock 里有精确版本 —— 这是**期望**的状态
            pass

    # 反向检查：lock 里有、requirements.txt 里没有的，说明清单被删过而 lock 没同步
    declared_keys = {_norm(n) for n, _ in declared}
    orphan = [k for k in pins if k not in declared_keys]

    if missing:
        print("❌ 以下包在 requirements.txt 里声明了，但 requirements.lock 里没有锁定版本：")
        for n in missing:
            print(f"   - {n}")
        print("\n   修法：跑 `python scripts/lock_requirements.py --write` 重新生成，")
        print("   或确认这些包确实不该进生产镜像（如是，请在 requirements.txt 里注释掉并写明原因）。")

    if orphan:
        print("⚠️ 以下包在 requirements.lock 里有，但 requirements.txt 里已不再声明：")
        for n in sorted(orphan):
            print(f"   - {n}")
        print("   这通常意味着删包后忘了同步 lock（无害但会长期误导人）。")

    if missing:
        return 1

    print(f"✅ 依赖锁定一致：声明 {len(declared)} 个包，锁定 {len(pins)} 个精确版本。")
    return 0


def write() -> int:
    """从当前环境读出真实版本并生成 lock 文件。"""
    import importlib.metadata as md

    declared = _parse_requirements(REQ)
    lines = [
        "# 依赖锁定清单（constraints 文件）—— 由 scripts/lock_requirements.py --write 生成",
        "#",
        "# 用法：pip install -r requirements.txt -c requirements.lock",
        "# 改完 requirements.txt 后请重新生成，否则 CI 的一致性门禁会红。",
        "#",
        "# 这里钉的是**直接依赖**（requirements.txt 里列出的那些），不是完整传递闭包。",
        "# 直接依赖的版本漂移是最常见的故障源；完整锁定需 pip-compile 在干净环境里解析。",
        "#",
        f"# 生成时间：{__import__('datetime').date.today().isoformat()}",
        f"# 生成环境：Python {sys.version.split()[0]}",
        "",
    ]
    failed: list[str] = []
    for name, _spec in declared:
        try:
            ver = md.version(name)
        except md.PackageNotFoundError:
            failed.append(name)
            continue
        lines.append(f"{name}=={ver}")

    if failed:
        print("❌ 以下包在当前环境里没装，无法确定版本：")
        for n in failed:
            print(f"   - {n}")
        print("   请先安装它们（或从 requirements.txt 里移除）再重新生成。")
        return 1

    LOCK.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✅ 已写入 {LOCK.relative_to(ROOT)}（{len(declared)} 个包）")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="依赖锁定清单的生成与校验")
    p.add_argument("--write", action="store_true", help="从当前环境重新生成 lock 文件")
    args = p.parse_args(argv)
    if not LOCK.exists() and not args.write:
        print(f"❌ 找不到 {LOCK.name}。请先跑 `python scripts/lock_requirements.py --write` 生成。")
        return 1
    return write() if args.write else check()


if __name__ == "__main__":
    raise SystemExit(main())
