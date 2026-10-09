#!/usr/bin/env python
"""PG 表体积与行数采集：把"库在涨"变成一条能画、能告警的指标。

为什么必须有
------------
2026-10-03 起对话历史改为**永久保留**（`RETENTION_DAYS=0`，清理任务整体停用）：
`agent_messages` / `agent_sessions` / 检查点三张表 / `review_queue` / `session_owners`
全部只增不减。这是**有意为之**的设计，但它必须配套一件东西 —— **容量可见性**。

在那之前，整个部署面**没有任何一条指标能回答"库现在多大、涨得多快"**。
唯一相关的是 `DiskSpaceLow`（可用空间低于 15%），而它响的时候：
  ① 余量往往只剩几小时（磁盘不是匀速填满的，WAL 与 autovacuum 会加速）；
  ② 它只说"盘要满了"，不说"是哪个表在吃" —— 而在这个库里，
     "某个表涨得快"与"备份窗口变长、VACUUM 跟不上"是同一条因果链上的前后段。

指标怎么进 Prometheus
--------------------
与备份那边**同一套路**：写 `backups/*.prom`，由 node-exporter 的
**textfile collector** 抓走（compose 里把 `backups/` 只读挂进那个容器）。
容器里的调用方是 `pg-stats` 服务。

为什么行数是**估算**（`reltuples`）而不是精确 count
------------------------------------------------
精确 `count(*)` 在大表上要全表扫，几百万行时是秒级到分钟级，而本脚本每 15 分钟
跑一次 —— 那是拿在线库的性能换一个展示用的数字。
`pg_class.reltuples` 是优化器统计里的估算值，**免费且永远可读**，看趋势完全够。
⚠️ 它是估算：`ANALYZE` 之后才准，刚建的表或长期不写入的表可能明显偏离。
**体积（`pg_total_relation_size`）才是精确值**，也是容量判据 —— 同样是免费的。

失败时**绝不改写已有文件**
------------------------
查询失败（库连不上 / 权限不足）时保留上一版指标文件不动，只打日志并非 0 退出。
理由与备份那边一致：指标消失会让 `delta()` / `deriv()` 这类规则**静默失效**
（没有序列可算 ⇒ 不告警），而"容量看不见"正是本脚本要消灭的东西。

用法
----
    python scripts/pg_table_stats.py            # 采集一次，写指标文件
    python scripts/pg_table_stats.py --json     # 打到 stdout（人工看）
    python scripts/pg_table_stats.py --loop     # 按间隔循环（容器里用）

    # 输出路径默认 backups/pg-tables.prom；间隔用 PG_STATS_INTERVAL_SECONDS（默认 900 秒）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.chdir(ROOT)

from app import config  # noqa: E402

#: 默认输出。放 `backups/` 是因为那里已经被 node-exporter 以只读方式挂载
#: （textfile collector 的目录），不需要再动部署拓扑。
DEFAULT_OUT = os.path.join("backups", "pg-tables.prom")

#: 采集间隔（秒）。15 分钟：体积查询走的是元数据、开销可以忽略，
#: 而告警窗口是 6 小时 —— 间隔必须显著小于窗口，否则 `delta()` 只有一两个样本、
#: 算出来的斜率不可信。可用 PG_STATS_INTERVAL_SECONDS 覆盖。
DEFAULT_INTERVAL = 900

#: 指标名以**常量形式**声明，与 `scripts/drill_metrics.py` 同一个约定。
#: 这不是洁癖：`tests/test_grafana_dashboards.py` 只认这个形式，用它来离线校验
#: "看板里引用的指标名是否真实存在"。写成内联字符串的后果是——
#: 看板里把 `pg_database_size_bytes` 拼错一个字母，守卫看不见，
#: 直到有人在 Grafana 上发现那张图是空的。
M_TABLE_SIZE = "pg_table_size_bytes"
M_TABLE_ROWS = "pg_table_rows_estimate"
M_DB_SIZE = "pg_database_size_bytes"

#: ⚠️ `relkind='r'` 只取普通表：视图/序列/分区子表各有各的体积语义，
#: 混在一起会让"某表涨了多少"这句话失去意义。
_STATS_SQL = """
SELECT c.relname                                   AS name,
       pg_total_relation_size(c.oid)               AS bytes,
       GREATEST(c.reltuples, 0)::bigint            AS rows_est
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public'
   AND c.relkind = 'r'
 ORDER BY bytes DESC, c.relname
"""

_DB_SIZE_SQL = "SELECT pg_database_size(current_database())"


def _escape_label(value: str) -> str:
    """Prometheus 标签值的转义（反斜杠、双引号、换行）。

    表名来自 `pg_class`、理论上不含这些字符，但**转义规则不能靠"理论上"** ——
    漏转义会产出一份**语法错误**的指标文件，而 textfile collector 遇到坏行会
    **丢掉整个文件里的所有指标**（不是只丢那一行）。一个引号就能让容量监控整体瞎掉。
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_prom(tables, db_size: int) -> str:
    """渲染 Prometheus 文本（**纯函数**，便于离线断言）。

    ``tables`` 是 ``[(name, bytes, rows_est), ...]``。
    """
    lines = [
        f"# HELP {M_TABLE_SIZE} 每张表的总占用（含索引与 TOAST），字节",
        f"# TYPE {M_TABLE_SIZE} gauge",
    ]
    for name, size, _rows in tables:
        lines.append(f'{M_TABLE_SIZE}{{table="{_escape_label(str(name))}"}} {int(size)}')

    lines += [
        f"# HELP {M_TABLE_ROWS} 表行数估算（pg_class.reltuples，ANALYZE 之后才准）",
        f"# TYPE {M_TABLE_ROWS} gauge",
    ]
    for name, _size, rows in tables:
        lines.append(f'{M_TABLE_ROWS}{{table="{_escape_label(str(name))}"}} {int(rows)}')

    lines += [
        f"# HELP {M_DB_SIZE} 当前数据库总占用，字节",
        f"# TYPE {M_DB_SIZE} gauge",
        f"{M_DB_SIZE} {int(db_size)}",
    ]
    # 末尾必须有换行：没有的话最后一行的内容会被下一个追加者粘住。
    return "\n".join(lines) + "\n"


def fetch_stats(dsn: str):
    """连库取一次。返回 ``([(name, bytes, rows_est), ...], db_size)``。

    只读、不开事务写、不建表 —— 这个脚本在任何权限下都应该是安全的。
    """
    import psycopg

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(_STATS_SQL)
            tables = [(r[0], int(r[1]), int(r[2])) for r in cur.fetchall()]
            cur.execute(_DB_SIZE_SQL)
            row = cur.fetchone()
            db_size = int(row[0]) if row else 0
    return tables, db_size


def write_prom_atomically(text: str, out_path: str) -> None:
    """原子替换：先写临时文件再 ``os.replace``。

    textfile collector 随时可能在读；半截文件会让它**丢掉整个文件**里的指标。
    ⚠️ ``newline="\\n"``：本脚本在 Windows 上手工跑时，默认的换行转换会把
    每行结尾写成 ``\\r\\n`` —— Prometheus 的文本格式不接受裸 ``\\r``，
    而**在 Linux 容器里跑却完全正常**（那里本来就是 ``\\n``）。
    这种"只在某一边坏"的缺陷最难查，所以显式钉死 LF。
    """
    tmp = f"{out_path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, out_path)


def collect(dsn: str, out_path: str):
    """采集一次并落盘。返回统计结果（供 ``--json`` 用）。"""
    tables, db_size = fetch_stats(dsn)
    write_prom_atomically(render_prom(tables, db_size), out_path)
    return {"tables": len(tables), "db_size_bytes": db_size, "out": out_path}


def _interval() -> int:
    raw = (os.getenv("PG_STATS_INTERVAL_SECONDS", "") or "").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 0
    return n if n > 0 else DEFAULT_INTERVAL


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="采集 PG 表体积与行数，写成 Prometheus 文本")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"输出路径（默认 {DEFAULT_OUT}）")
    ap.add_argument("--json", action="store_true", help="结果打到 stdout，不写文件")
    ap.add_argument("--loop", action="store_true", help="按间隔循环（容器里用）")
    args = ap.parse_args(argv)

    dsn = config.DB_URL
    if not dsn:
        # 与 session.py 同一条理由：显式说清楚，别让下游抛一句读起来像"驱动装错了"的话。
        logging.error("未配置 DB_URL：没有库可采集。")
        return 2

    if args.json:
        tables, db_size = fetch_stats(dsn)
        print(
            json.dumps(
                {
                    "database_size_bytes": db_size,
                    "tables": [
                        {"table": n, "size_bytes": s, "rows_estimate": r}
                        for n, s, r in tables
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    interval = _interval()
    while True:
        try:
            result = collect(dsn, args.out)
            logging.info(
                "PG 容量已采集：%d 张表，库总占用 %.1f MB → %s",
                result["tables"],
                result["db_size_bytes"] / 1024 / 1024,
                result["out"],
            )
        except Exception as e:  # noqa: BLE001
            # **不覆盖上一版文件**（见模块 docstring）：保留旧指标好过让序列消失。
            logging.error("PG 容量采集失败（保留上一版指标文件不动）：%s", e)
            if not args.loop:
                return 1
        if not args.loop:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    raise SystemExit(main(sys.argv[1:]))
