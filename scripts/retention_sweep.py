#!/usr/bin/env python
"""数据保留清理：删除超过保留期的会话数据。

清理什么
--------
三类数据此前**永久驻留**，没有任何回收机制：

  1. **检查点**（`checkpoints` / `checkpoint_blobs` / `checkpoint_writes`，
     由 langgraph 的 `AsyncPostgresSaver.setup()` 建）——
     存的是完整消息历史、中间草稿、评分与引用。数据主体；
  2. **`review_queue`** —— 待审/已审的草稿正文与审核意见；
  3. **`session_owners`** —— 会话归属登记行。

为什么按「最后活动时间」而不是「创建时间」
--------------------------------------
`session_owners.updated_at` 在每次 `claim()`（即该租户对这条会话发起请求）时刷新。
若按 `created_at` 清，一个创建于 100 天前、今天还在用的会话会被删掉 ——
那是不可接受的破坏（对话还在继续），而且用户会看到"历史凭空消失"。

⚠️ 因此本脚本**依赖 `updated_at` 列**。旧库由 `SessionRegistry._ensure_schema`
的 ALTER 自动补列；若你的库还没跑过一次 `claim()`，先跑迁移
（`python -m app.migrations`）或直接跑一次服务。

为什么默认 dry-run
----------------
删除**不可逆**。默认只打印"将要删除什么"，确认无误后再加 `--apply`。
这与 `DELETE /v1/sessions/{id}` 的取向一致：破坏性动作必须显式表达意图。

用法
----
    python scripts/retention_sweep.py                 # 看会删什么（不删）
    python scripts/retention_sweep.py --days 30       # 用 30 天口径预演
    python scripts/retention_sweep.py --days 30 --apply   # 真删
    python scripts/retention_sweep.py --json          # 机器可读（便于接监控）

定时执行（例如每日 03:00）：
    0 3 * * *  cd /app && python scripts/retention_sweep.py --days 30 --apply

为什么不做成 HTTP 端点
-------------------
删除是运维动作。挂成端点会多出一个"谁能触发全量清理"的权限问题，
而清理本身没有任何需要在线交互的部分。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.chdir(ROOT)

from app import config  # noqa: E402

# langgraph 的 pg saver 建的三张表。表名是**内部常量白名单**（不是外部输入），
# 因此下面的 f-string 拼表名是安全的；但仍然只从这份白名单里取值。
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")

#: 单批删除的 thread_id 个数。一次删几十万行会长时间持锁、把库拖慢，
#: 而清理是后台动作 —— 宁可多跑几批。
BATCH = 500


def _connect(dsn):
    import psycopg

    return psycopg.connect(dsn, connect_timeout=10, autocommit=True)


def _existing_tables(cur, names):
    """返回 names 里真实存在的表（在 public schema 下）。"""
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = ANY(%s)",
        (list(names),),
    )
    return {r[0] for r in cur.fetchall()}


def _has_column(cur, table, column) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
        (table, column),
    )
    return cur.fetchone() is not None


def plan(cur, cutoff) -> dict:
    """算出"打算删什么"（dry-run 用）。"""
    report = {
        "cutoff": cutoff.isoformat(),
        "threads": 0,          # 超期会话条数（= 待删的归属登记行数）
        "review_queue": 0,
        "warnings": [],
    }

    if _has_column(cur, "session_owners", "updated_at"):
        cur.execute("SELECT count(*) FROM session_owners WHERE updated_at < %s", (cutoff,))
        report["threads"] = int(cur.fetchone()[0])
    else:
        # 没有 updated_at 就无法安全判断"最后活动时间"。**不退回 created_at** ——
        # 那会删掉仍在使用中的会话；明确报警让人去跑迁移。
        report["warnings"].append(
            "session_owners 缺 updated_at 列，无法判断最后活动时间 —— "
            "请先跑 `python -m app.migrations`（或启动一次服务）补列"
        )

    if _has_column(cur, "review_queue", "updated_at"):
        cur.execute("SELECT count(*) FROM review_queue WHERE updated_at < %s", (cutoff,))
    else:
        cur.execute("SELECT count(*) FROM review_queue WHERE created_at < %s", (cutoff,))
    report["review_queue"] = int(cur.fetchone()[0])
    return report


def sweep(cur, cutoff) -> dict:
    """执行清理。返回实际删除的行数。"""
    result = {"checkpoints": {}, "review_queue": 0, "session_owners": 0, "threads": []}

    # 先用**归属表**确定超期会话清单（它是"最后活动时间"的权威来源），
    # 再用这份清单去删检查点 —— 检查点表本身没有可靠的"最后更新时间"列。
    cur.execute("SELECT thread_id FROM session_owners WHERE updated_at < %s", (cutoff,))
    threads = [r[0] for r in cur.fetchall()]
    result["threads"] = threads

    tables = _existing_tables(cur, CHECKPOINT_TABLES) & set(CHECKPOINT_TABLES)
    # ⚠️ `& set(CHECKPOINT_TABLES)` 不是多余的：上面那句 SQL 已经按名单过滤过了，
    # 但**表名最终是拼进 DELETE 语句的**，所以白名单必须在 Python 侧再收一次口。
    # 只靠 SQL 的 `= ANY(%s)` 保证，一旦哪天有人改了那句查询（或传错参数），
    # 后面的 `DELETE FROM {t}` 就会照着查询结果删 —— 而它不会报错，只会删多。
    # （这条是测试先发现的：假游标不按参数过滤时，脚本试图 DELETE FROM review_queue
    #   并用它去索引 checkpoints 计数，直接 KeyError。）
    for t in CHECKPOINT_TABLES:
        result["checkpoints"][t] = 0
    for i in range(0, len(threads), BATCH):
        chunk = threads[i:i + BATCH]
        for t in sorted(tables):
            cur.execute(f"DELETE FROM {t} WHERE thread_id = ANY(%s)", (chunk,))
            result["checkpoints"][t] += cur.rowcount

    # 审核队列独立清理（不依赖归属表）：它有自己的时间戳，
    # 且即使归属表未启用（单租户），队列数据也照样需要回收。
    if _has_column(cur, "review_queue", "updated_at"):
        cur.execute("DELETE FROM review_queue WHERE updated_at < %s", (cutoff,))
    else:
        cur.execute("DELETE FROM review_queue WHERE created_at < %s", (cutoff,))
    result["review_queue"] = cur.rowcount

    if threads:
        for i in range(0, len(threads), BATCH):
            chunk = threads[i:i + BATCH]
            cur.execute("DELETE FROM session_owners WHERE thread_id = ANY(%s)", (chunk,))
            result["session_owners"] += cur.rowcount

    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=None,
                    help="保留天数（默认取 RETENTION_DAYS；0 表示未配置）")
    ap.add_argument("--apply", action="store_true",
                    help="真的执行删除（不加则只预演）")
    ap.add_argument("--dsn", default=None, help="数据库连接串（默认取 DB_URL）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（便于接监控）")
    args = ap.parse_args()

    days = args.days if args.days is not None else config.RETENTION_DAYS
    if not days or days <= 0:
        print("保留期未设置（RETENTION_DAYS=0 或 --days 未给）—— "
              "这是**刻意的安全默认**：不给就会一次删掉所有超期数据，"
              "所以这里拒绝执行。请显式指定 --days。", file=sys.stderr)
        return 2

    dsn = args.dsn if args.dsn is not None else config.DB_URL
    if not dsn:
        print("未配置 DB_URL（或 --dsn）—— 内存后端没有可清理的持久数据。",
              file=sys.stderr)
        return 2

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    try:
        with _connect(dsn) as conn:
            with conn.cursor() as cur:
                preview = plan(cur, cutoff)
                if not args.apply:
                    out = {"mode": "dry-run", "days": days, **preview}
                    if args.json:
                        print(json.dumps(out, ensure_ascii=False, indent=2))
                    else:
                        print(f"[dry-run] 保留 {days} 天，早于 {preview['cutoff']} 的数据：")
                        print(f"  超期会话       {preview['threads']}"
                              "（含其检查点与归属登记行）")
                        print(f"  审核队列记录   {preview['review_queue']}")
                        print("（检查点按超期会话清单删除，行数需实际执行才能确定）")
                        for w in preview["warnings"]:
                            print(f"  ⚠️ {w}", file=sys.stderr)
                        print("\n加 --apply 执行删除。")
                    return 0

                done = sweep(cur, cutoff)
                out = {"mode": "apply", "days": days, "cutoff": cutoff.isoformat(), **done}
                logging.info("数据保留清理完成：%s", out)
                if args.json:
                    print(json.dumps(out, ensure_ascii=False, indent=2))
                else:
                    print(f"[apply] 保留 {days} 天，已删除：")
                    print(f"  超期会话       {len(done['threads'])}")
                    for t, n in done["checkpoints"].items():
                        print(f"    {t}: {n}")
                    print(f"  审核队列记录   {done['review_queue']}")
                    print(f"  归属登记行     {done['session_owners']}")
                return 0
    except Exception as e:  # noqa: BLE001
        print(f"清理失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
