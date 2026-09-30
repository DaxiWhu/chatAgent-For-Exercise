#!/usr/bin/env python
"""备份配对清单：判断"同一时刻的 pg 与 qdrant 快照"是否成对存在。

为什么需要它
-----------
两个备份循环（`backup-pg` / `backup-qdrant`）各自独立产出文件，而恢复时需要的
是**同一时刻**的两份：

    PG（检查点 / 审核队列 / 会话归属）  —— 会话状态、审核记录
    Qdrant（稀疏 BM25 + 稠密 embedding）—— RAG 向量

它们之间有隐含的对应关系：**向量库里的 point 与 PG 里的 chunk 元数据必须来自
同一批语料**。取"最近的 dump"+"最近的快照"是恢复时最常见的做法，而两者可能
相差好几个小时 —— 结果不是报错，而是检索出**引用指向不存在的块**、
或整批增量文档静默消失。这类不一致不会以异常的形式暴露。

本脚本把"能不能配"变成一个可执行的判定，让恢复流程**只认清单，不认时间戳**。

判定口径
--------
只看**最新的那一对**（恢复时真正会用到的那一对），因为"历史上某一次曾经错配"
不影响今天的恢复。其余错配作为警告列出。

退出码
------
    0  最新的一对匹配（窗口内）
    1  最新的一对不匹配 —— **恢复前必须人工确认**
    2  没有备份可判断（两侧至少有一侧为空 / 目录不存在）
       ⚠️ 2 **不是失败也不是通过**：它表示"无法判断"。发布流程里已经踩过
       把"无法判定"当成"通过"的坑（见 deploy/CANARY.md），这里保持同一取向。

用法
----
    python scripts/backup_manifest.py
    python scripts/backup_manifest.py --json
    python scripts/backup_manifest.py --window 900      # 放宽到 15 分钟
    python scripts/backup_manifest.py --backup-dir /backups

⚠️ 时区：容器里的循环用 `date -u`（UTC）命名文件，而手工跑
`scripts/pg_backup.py` 用的是本机本地时间。两者混在同一目录时，本脚本无法
分辨，只能按字面时间比较 —— 所以**别把两种来源的文件混着用来配对**。
（这也是把定时循环与手工执行落在同一个 `backups/` 的代价，值得记一笔。）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

RC_OK, RC_MISMATCH, RC_UNDECIDABLE = 0, 1, 2

#: 文件名里的时间戳：`20260930-120000` 或 `20260930_120000`
_STAMP_RE = re.compile(r"(\d{8})[-_](\d{6})")

DEFAULT_WINDOW = 300.0


class Backup:
    """一个备份文件（解析出时间戳）。"""

    def __init__(self, path: Path, ts: datetime | None, mtime: float):
        self.path = path
        self.ts = ts
        self.mtime = mtime

    @property
    def name(self) -> str:
        return self.path.name

    def sort_key(self):
        """优先按文件名里的时间戳排序；没有时间戳就退回 mtime。

        为什么不能只用 mtime：文件在容器里生成后可能被复制/同步过，
        mtime 会变；而文件名里的时间戳是**备份发生时**的事实。
        """
        return (1 if self.ts else 0, self.ts or datetime.min, self.mtime)

    def to_dict(self) -> dict:
        return {
            "file": self.name,
            "timestamp": self.ts.isoformat() if self.ts else None,
            "size": self.path.stat().st_size if self.path.exists() else None,
        }


def _parse_stamp(name: str) -> datetime | None:
    m = _STAMP_RE.search(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _collect(directory: Path, patterns: tuple[str, ...]) -> list[Backup]:
    if not directory.is_dir():
        return []
    out: list[Backup] = []
    for pat in patterns:
        for p in sorted(directory.glob(pat)):
            if not p.is_file():
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            out.append(Backup(p, _parse_stamp(p.name), mtime))
    out.sort(key=lambda b: b.sort_key())
    return out


def latest_pairs(directory: Path) -> tuple[list[Backup], list[Backup]]:
    """返回 (pg 备份列表, qdrant 备份列表)，均按时间升序。

    qdrant 的快照刻意放在子目录（`<dir>/qdrant/`，见 compose 的 `BACKUP_DIR`）；
    同时也扫顶层，兼容手工把 snapshot 放在外面的情况。
    """
    dumps = _collect(directory, ("*.dump", "*.sql", "*.sql.gz"))
    snaps = _collect(directory / "qdrant", ("*.snapshot",))
    snaps += _collect(directory, ("*.snapshot",))
    return dumps, snaps


def evaluate(dumps: list[Backup], snaps: list[Backup], window: float) -> dict:
    """算出配对结论。

    只有最新的那一对决定退出码；其余错配作为 `warnings` 列出。
    """
    report: dict = {
        "pg_count": len(dumps),
        "qdrant_count": len(snaps),
        "window_seconds": window,
        "latest": None,
        "warnings": [],
        "pairs": [],
    }

    if not dumps or not snaps:
        report["warnings"].append(
            "至少一侧没有任何备份文件 —— 无法判断配对情况"
            f"（pg={len(dumps)}, qdrant={len(snaps)}）"
        )
        return report

    # 贪心配对：对每个 pg 取时间上最接近的**未被占用**的 qdrant
    used: set[int] = set()
    pairs = []
    for d in dumps:
        best_i, best_gap = None, None
        for i, s in enumerate(snaps):
            if i in used:
                continue
            gap = abs((s.sort_key()[1] - d.sort_key()[1]).total_seconds())
            if best_gap is None or gap < best_gap:
                best_i, best_gap = i, gap
        if best_i is None:
            pairs.append({"pg": d.to_dict(), "qdrant": None, "gap_seconds": None})
            continue
        used.add(best_i)
        s = snaps[best_i]
        pairs.append({
            "pg": d.to_dict(),
            "qdrant": s.to_dict(),
            "gap_seconds": round(best_gap or 0.0, 1),
        })
    report["pairs"] = pairs

    newest = pairs[-1]
    report["latest"] = newest

    for p in pairs[:-1]:
        if p["qdrant"] is None:
            report["warnings"].append(f"{p['pg']['file']} 没有可配对的 qdrant 快照")
        elif (p["gap_seconds"] or 0) > window:
            report["warnings"].append(
                f"{p['pg']['file']} ↔ {p['qdrant']['file']} "
                f"相差 {p['gap_seconds']}s（> {window}s）"
            )
    # 未被任何 pg 配上的 qdrant（说明 qdrant 比 pg 多跑了几轮）
    leftover = [s for i, s in enumerate(snaps) if i not in used]
    for s in leftover:
        report["warnings"].append(f"{s.name} 没有可配对的 pg 备份")

    if newest["qdrant"] is None:
        report["verdict"] = "MISMATCH"
        report["reason"] = "最新的 pg 备份找不到时间相近的 qdrant 快照"
    elif (newest["gap_seconds"] or 0) > window:
        report["verdict"] = "MISMATCH"
        report["reason"] = (
            f"最新一对相差 {newest['gap_seconds']}s，超过窗口 {window}s ——"
            "两侧可能不是同一时刻的，恢复会得到不一致的数据"
        )
    else:
        report["verdict"] = "OK"
        report["reason"] = f"最新一对相差 {newest['gap_seconds']}s（窗口 {window}s 内）"
    return report


def _print_human(rep: dict, directory: Path) -> None:
    print(f"备份目录：{directory}")
    print(f"  pg dump       {rep['pg_count']} 份")
    print(f"  qdrant 快照   {rep['qdrant_count']} 份")
    if rep.get("latest"):
        pg = rep["latest"]["pg"]
        qd = rep["latest"]["qdrant"] or {}
        print("\n最新一对（恢复时用的就是它）：")
        print(f"  pg      {pg['file']}   {pg['timestamp']}")
        print(f"  qdrant  {qd.get('file', '（缺失）')}   {qd.get('timestamp')}")
        if rep["latest"]["gap_seconds"] is not None:
            print(f"  相差    {rep['latest']['gap_seconds']}s")
    print()

    if rep["warnings"] and rep.get("verdict"):
        print("⚠️ 历史错配（不影响最新一对，但说明循环曾经不同步）：")
        for w in rep["warnings"]:
            print(f"    - {w}")
        print()

    verdict = rep.get("verdict")
    if verdict == "OK":
        print(f"✅ {rep['reason']}")
        print("   恢复命令（Qdrant 侧是破坏性动作，脚本会先打印步骤再要你确认）：")
        if rep.get("latest"):
            print(f"     python scripts/pg_backup.py restore backups/{rep['latest']['pg']['file']} --yes")
            if rep["latest"].get("qdrant"):
                print(f"     python scripts/qdrant_backup.py verify backups/qdrant/{rep['latest']['qdrant']['file']}")
    elif verdict == "MISMATCH":
        print(f"❌ {rep['reason']}")
        print("   **不要**直接恢复。先确认是不是某个循环停过；")
        print("   宁可退回到更早的一对（脚本上面的列表里找 gap 最小的一对）。")
    else:
        print("⚠️ 无法判定：备份不存在或目录不对。这不是通过。")
        for w in rep["warnings"]:
            print(f"    - {w}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--backup-dir", default="",
                    help="备份目录（默认取 BACKUP_DIR，再默认 ./backups）")
    ap.add_argument("--window", type=float, default=DEFAULT_WINDOW,
                    help=f"允许的两侧时间差（秒），默认 {DEFAULT_WINDOW:g}")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    directory = Path(args.backup_dir or os.getenv("BACKUP_DIR") or "backups")
    dumps, snaps = latest_pairs(directory)
    rep = evaluate(dumps, snaps, args.window)

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        _print_human(rep, directory)

    verdict = rep.get("verdict")
    if verdict is None:
        return RC_UNDECIDABLE
    return RC_OK if verdict == "OK" else RC_MISMATCH


if __name__ == "__main__":
    sys.exit(main())
