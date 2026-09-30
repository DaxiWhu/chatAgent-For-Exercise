"""把「恢复演练」的结果发布成 node-exporter 的 textfile 指标。

为什么必须落指标
----------------
`drill` 的价值是"证明备份能被恢复"，但它**只在你跑它的那一刻才存在**。
不落指标的话，"上次演练是什么时候、有没有通过"就只活在某个人的记忆里 ——
而 RUNBOOK 要求每季度做一次，靠人记着的事一定会在最忙的那一季度被跳过。

落成指标之后：
- 面板 `chatAgent · 平台自身` 能看到"距上次成功演练多久"；
- 告警 `BackupDrillMissing` / `BackupDrillOverdue` 会在"从没演练过"和
  "太久没演练过"时叫人 —— 而不是等真的需要恢复时才发现从没人验证过。

为什么不做成"定时自动演练"
-------------------------
`pg_backup.py` 要调 `pg_dump` / `pg_restore`，它的执行通道是「`docker exec` 进
PG 容器」或「宿主机 PATH 里的客户端」—— **容器里两条都不成立**（容器内没有
docker，app 镜像里也没有 PG 客户端）。硬要自动化就得往镜像里塞客户端，
而镜像里的客户端版本还得与服务端严格匹配。

与其做一个"看起来在自动演练、实际跑不通"的容器，不如让**缺失本身可见**：
告警会在你从没演练过的时候就开始响。这就是本模块的作用。

⚠️ 原子替换，不能原地覆写
------------------------
node-exporter 是**另一个进程**在读这个文件。直接覆写会出现"读到一半"的半截内容，
那会被 textfile collector 判为语法错误，并**让整批指标一起消失**
（表现是"所有备份指标都不见了"，而根因跟备份毫无关系）。

⚠️ 失败时**不更新** `last_success`
--------------------------------
失败意味着"上一次成功的演练仍然是上一次成功的演练"。抹掉它、或把它改成当前时间，
都会让"多久没成功演练过"这个判断失真 —— 而那正是这个指标存在的唯一理由。
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

# 指标名 —— 与 deploy/monitoring/alerts.yml 里的表达式必须一致
# （tests/test_drill_closure.py 会逐条核对，防改名后告警静默失效）
M_ATTEMPT = "backup_drill_last_attempt_timestamp_seconds"
M_SUCCESS = "backup_drill_last_success_timestamp_seconds"
M_RESULT = "backup_drill_result"

_SUCCESS_RE = re.compile(r"^" + M_SUCCESS + r"\{[^}]*\}\s+(\d+(?:\.\d+)?)\s*$", re.M)


def metrics_dir() -> Path:
    """演练指标的落地目录。

    ⚠️ **必须与 node-exporter 的 textfile 挂载是同一个目录**，否则文件写出来了
    但没人采集 —— 这是最难发现的一类问题：文件在、时间戳在更新、看起来一切正常，
    而 Prometheus 里什么都没有，于是三条告警全成哑弹。

    默认 `backups`（对应 compose 里 node-exporter 挂的 `../../backups`）。

    ⚠️ 为什么不直接复用调用方的 `BACKUP_DIR`：qdrant 的备份目录是 `backups/qdrant`
    （**子目录**），而 textfile collector **不递归子目录** —— 传进去就等于白写。
    所以指标目录与备份目录是两个概念，这里分开。
    """
    return Path(os.getenv("DRILL_METRICS_DIR") or "backups")


def prom_path(target: str, directory=None) -> Path:
    return Path(directory if directory is not None else metrics_dir()) / f"drill-{target}.prom"


def _prev_success(path: Path) -> str | None:
    try:
        m = _SUCCESS_RE.search(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    return m.group(1) if m else None


def publish(target: str, ok: bool, directory=None) -> Path:
    """发布一次演练结果（`target` 取 `pg` / `qdrant`）。返回写入的文件路径。

    刻意**不抛异常**：指标发布失败不该让"演练本身"（更重要的事）看起来失败。
    但会在 stderr 上说明，避免完全静默。
    """
    path = prom_path(target, directory)
    now = f"{time.time():.0f}"
    lines = [
        "# 由 scripts/pg_backup.py 或 scripts/qdrant_backup.py 的 drill 发布；",
        "# node-exporter 的 textfile collector 读取。不要手工编辑 —— 下次 drill 会覆盖。",
        f'{M_ATTEMPT}{{target="{target}"}} {now}',
        f'{M_RESULT}{{target="{target}"}} {1 if ok else 0}',
    ]
    success = now if ok else _prev_success(path)
    if success:
        lines.append(f'{M_SUCCESS}{{target="{target}"}} {success}')

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, path)          # 原子：node-exporter 永远读不到半截文件
        print(f"[drill-metrics] 已发布 {path}（result={'1（通过）' if ok else '0（未通过）'}）")
    except OSError as exc:
        print(f"[drill-metrics] ⚠️ 指标发布失败（不影响演练结论本身）：{exc}", file=sys.stderr)
    return path
