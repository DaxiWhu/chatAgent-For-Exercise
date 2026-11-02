"""数据库结构版本管理 + 部署前预检（doctor）。

为什么需要它
-----------
当前所有表都是各模块用 `CREATE TABLE IF NOT EXISTS` 幂等建出来的。首次部署没问题，
但在**线上迭代**时会静默地什么都不做：

- 加列：靠 `ALTER ... ADD COLUMN IF NOT EXISTS` 勉强能跟；
- **改向量维度**（换 embedding 模型，例如 bge-m3 的 1024 换成 768）：表已存在 ⇒
  列定义不会被改 ⇒ 之后每一次 upsert 都因维度不匹配失败，而**报错点离原因很远**
  （报的是"插入失败"，不是"你换了模型"）；
- 加表、加索引、回填数据：完全没有路径。

本模块提供三件事：

1. **结构预检**（`status`）：不写数据，只报告"当前库 vs 代码期望"的差异 ——
   这是部署前的 checklist，而不是等运行时炸。
2. **版本记录**（`schema_migrations` 表）+ **一键 bootstrap**（`apply_pending`）。
3. **维度不一致的显式报错**：见 `src/rag/vector_store.py` 的 `ensure_schema`。

为什么放在 app/ 而不是 src/rag/
-----------------------------
它管的表横跨两层：`rag_chunks`（数据链路）与 `review_queue` / `session_owners`
（服务层）。放在 src/ 会反向依赖 app/，而 app/ 本来就是组合层（已 import src）。

DDL 的单一出处
-------------
迁移**不重复书写 DDL**，而是调用各模块自己的幂等建表方法：

    rag_chunks      → PgVectorStore.ensure_schema
    review_queue    → ReviewQueue.ensure_schema
    session_owners  → SessionRegistry.ensure_schema
    tenants / users / tenant_join_requests → AccountStore.ensure_schema

否则同一张表会有两份定义，迟早漂移。

命令行
------
    python -m app.migrations --status                   # 只看，不改
    python -m app.migrations --apply                    # 应用待应用的迁移
    python -m app.migrations --status --dim 768         # 按"打算换成的维度"预检
    python -m app.migrations --apply --dry-run          # 只列出会做什么
"""

import argparse
import logging
import os
import sys
from dataclasses import dataclass

#: 当前代码期望的结构版本（新增迁移时同步 +1）
#: ⚠️ 这里忘了改不会报错：``--status`` 会显示 pending 为空（"可以部署"），
#: 而老库里 v10/v11 的两张表永远建不出来 —— 上线后第一次压缩/第一次写记忆才炸。
#: 守卫：``tests/test_retention.py::test_schema_version_matches_the_latest_migration``。
SCHEMA_VERSION = 11

#: 迁移元表。**这份 DDL 必须内联**：它是"记录其他迁移的工具"，不能被别的迁移依赖。
_MIGRATIONS_TABLE = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "  version    integer PRIMARY KEY,"
    "  name       text NOT NULL,"
    "  applied_at timestamptz NOT NULL DEFAULT now()"
    ")"
)


def default_dim() -> int:
    """代码期望的向量维度：`RAG_EMBED_DIM` → 兜底 1024（bge-m3）。

    为什么不从真实 embedding 探测：那要调一次外部 API，不该出现在"结构预检"里
    （预检必须离线、快、可重复）。所以这里只做保守推断，并在报告里**明确显示**
    用的是哪个数字，让人有机会发现不对。
    """
    try:
        v = int(os.getenv("RAG_EMBED_DIM", "") or 1024)
        return v if v > 0 else 1024
    except (TypeError, ValueError):
        return 1024


@dataclass(frozen=True)
class Migration:
    """一条迁移：版本号 + 名称 + 执行函数（签名 `apply(dsn, ctx)`）。"""

    version: int
    name: str
    apply: object


# ---------------------------------------------------------------------------
# 迁移动作：全部委托给模块自己的幂等建表，本文件不重写 DDL
# ---------------------------------------------------------------------------
def _m1_rag_chunks(dsn, ctx):
    """向量表（含 HNSW 索引与 tenant 列）。DDL 出处：`PgVectorStore.ensure_schema`。"""
    from src.rag.vector_store import PgVectorStore

    PgVectorStore(dsn, table=ctx["table"]).ensure_schema(ctx["dim"])


def _m2_review_queue(dsn, ctx):
    """审核队列表。DDL 出处：`ReviewQueue._ensure_schema`。"""
    from .review_queue import ReviewQueue

    ReviewQueue(dsn=dsn).ensure_schema()


def _m3_session_owners(dsn, ctx):
    """会话归属表。DDL 出处：`SessionRegistry._ensure_schema`。"""
    from .sessions import SessionRegistry

    SessionRegistry(dsn=dsn, enabled=True).ensure_schema()


def _m4_review_queue_audit(dsn, ctx):
    """审核队列的**审计列**（操作者 / 认证方式 / 决定备注 / 处理时刻）。

    DDL 出处仍是 `ReviewQueue._ensure_schema`（它现在同时负责给老表
    `ADD COLUMN IF NOT EXISTS` 补齐这几列）。

    为什么要单独占一个版本号：v2 在老部署里**已记录为已应用**，`apply_pending`
    会按版本跳过它 ⇒ 只改 `_ensure_schema` 而不加版本号的话，老库既永远不会通过
    `--apply` 补上这几列，`--status` 也报不出来（`pending` 为空 ⇒ 预检显示
    "可以部署"），只剩"运行时第一次 add 顺带补"这条隐式路径。而这条路径在
    "服务先于迁移启动"的正常部署顺序下确实能兜住，但它**不可观测** ——
    这正是本模块存在的意义（把静默变成显式）。
    """
    from .review_queue import ReviewQueue

    ReviewQueue(dsn=dsn).ensure_schema()


def _m5_session_owners_activity(dsn, ctx):
    """会话归属表的**最后活动时间**列（数据保留清理依赖它）。

    DDL 出处仍是 `SessionRegistry._ensure_schema`（它负责
    `ADD COLUMN IF NOT EXISTS updated_at`，并把默认值设为 `now()`）。

    为什么单独占一个版本号：理由与 m4 相同 —— v3 在老部署里已记录为"已应用"，
    `apply_pending` 会按版本号跳过它。只改 `_ensure_schema` 而不加版本号，
    老库既不会通过 `--apply` 补列，`--status` 也报不出来（预检显示"可以部署"），
    只剩"运行时第一次 claim 顺带补"这条**不可观测**的隐式路径。

    而这一列缺失的后果不只是"清理脚本报错"：脚本在缺列时会**拒绝清理整表**
    （不退回 `created_at`，那会删掉仍在使用的会话）—— 于是数据继续无界增长，
    而没有任何地方会提示"保留策略其实一直没生效"。
    """
    from .sessions import SessionRegistry

    SessionRegistry(dsn=dsn, enabled=True).ensure_schema()


def _m6_session_owners_user(dsn, ctx):
    """会话归属表的**用户维度**列（把同租户内的隔离下沉到人）。

    DDL 出处仍是 `SessionRegistry._ensure_schema`（负责
    `ADD COLUMN IF NOT EXISTS owner_user text NOT NULL DEFAULT ''`）。

    为什么单独占一个版本号：与 m4/m5 同理 —— v5 在老部署里已记录为"已应用"，
    `apply_pending` 按版本号跳过它。只改 `_ensure_schema` 而不加版本号，
    老库既不会通过 `--apply` 补列，`--status` 也报不出来（预检显示"可以部署"）。

    这一列缺失的后果是**隔离静默失效**：代码会按 `owner_user` 过滤，而列不存在
    时查询直接抛错 → 上层 catch 后**放行**（归属校验是旁路保护，刻意不阻断服务）
    ⇒ 表现为"同租户内任何人都能读到别人的会话"，而日志里只有一行 warning。
    列存在但值为空串时，语义退化为"属于该租户的机器调用方"，与升级前一致，
    不会误伤存量数据。
    """
    from .sessions import SessionRegistry

    SessionRegistry(dsn=dsn, enabled=True).ensure_schema()


def agent_session_ddl(dsn: str) -> list[str]:
    """生成"跨轮对话历史表"的建表语句（**纯函数**，不连库；供迁移与守卫测试共用）。

    为什么要拆出来：`_m7` 里剩下的部分需要真连一次 PG，而 CI 里没有 PG —— 拆开后
    "**我们到底会执行哪几条 SQL**"就能在离线环境里被断言（见
    `tests/test_agent_history_budget.py`）。不拆的话这段逻辑只能靠"上了生产才知道"。
    """
    from agents.extensions.memory import SQLAlchemySession
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.schema import CreateIndex, CreateTable

    from src.agents.session import to_async_dsn

    # 只借它把 Table 定义交出来。`create_async_engine` 是**懒连接** —— 不会碰数据库。
    probe = SQLAlchemySession(
        "__migration__", engine=create_async_engine(to_async_dsn(dsn))
    )
    dialect = postgresql.dialect()
    statements: list[str] = []
    # 顺序不能换：`agent_messages` 有指向 `agent_sessions` 的外键。
    for table in (probe._sessions, probe._messages):
        statements.append(
            str(CreateTable(table, if_not_exists=True).compile(dialect=dialect))
        )
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            statements.append(
                str(CreateIndex(index, if_not_exists=True).compile(dialect=dialect))
            )
    return statements


def _m7_agent_session_tables(dsn, ctx):
    """跨轮对话历史的两张表：`agent_sessions` / `agent_messages`（2026-10-03）。

    为什么 DDL **不是**"委托给某个模块的 `ensure_schema`"（本文件的一贯做法）：
    这两张表**不归本仓库任何模块所有** —— 它们是官方 `openai-agents` 的
    `SQLAlchemySession` 直接读写的数据表，所以没有一个"表的主人"可以委托。

    为什么也**不手抄一份 DDL**：手抄的那份在 SDK 升版时会**静默漂移**，而漂的后果
    是运行期才炸（列名对不上 → 每次取历史都失败 → `session_for` 降级 ⇒ 每轮没有历史，
    而服务照常跑、指标全绿）。所以这里让 **SDK 自己把 Table 定义交出来**
    （见 `agent_session_ddl`），本函数只负责执行。

    ⚠️ 它用到了 SDK 的两个私有属性（`_sessions` / `_messages`）。这是刻意的取舍：
    依赖私有属性会随 SDK 升版断掉，但那**是迁移/启动期炸**（看得见、能修），
    比运行期静默降级好。守卫见 `tests/test_agent_history_budget.py`。

    幂等：`CREATE TABLE / CREATE INDEX ... IF NOT EXISTS`。
    """
    import psycopg

    statements = agent_session_ddl(dsn)
    with psycopg.connect(dsn, connect_timeout=10, autocommit=True) as conn:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)


def _m8_account_tables(dsn, ctx):
    """账号体系的三张表：`tenants` / `users` / `tenant_join_requests`（2026-10-04）。

    DDL 出处：`AccountStore.schema_statements`（本文件不重写 DDL，与 m1~m6 同口径）。

    为什么单独占一个版本号：与 m4~m6 同理 —— 老库的 `schema_migrations` 里没有
    这条记录，`--apply` 才会执行它。只改代码不加版本号的话，`--status` 会报
    "无待应用迁移、可以部署"，于是新表在老库上**永远不会被建出来**，直到某个
    管理接口第一次被调用才炸 —— 而那时报的是 SQL 错误，离"忘了跑迁移"很远。

    为什么不像 m1~m6 那样挂在别的模块上：这三张表**只归本仓库所有**（不像
    `agent_sessions` 属于 SDK），所以委托给它们的"主人" `AccountStore` 是正解。
    """
    from .accounts import AccountStore

    AccountStore(dsn=dsn).ensure_schema()


def _m9_run_control_table(dsn, ctx):
    """运行登记表：`agent_runs`（2026-10-18）。

    DDL 出处：`RunControl.schema_statements`（本文件不重写 DDL，与 m1~m6 同口径）。

    为什么单独占一个版本号：与 m4~m8 同理 —— 老库的 `schema_migrations` 里没有这条，
    `--apply` 才会执行它。只改代码不加版本号的话，`--status` 会报"无待应用迁移"，
    于是新表在老库上永远不会被建出来，直到第一次点"停止"才炸 —— 而那时报的是
    SQL 错误，离"忘了跑迁移"很远。

    为什么这张表**可以清空**（与 `agent_sessions` 刻意相反）：它存的是"此刻谁在跑"，
    不是用户内容。进程崩溃留下的残行没有任何价值，反而会让下一次 `/stop` 打到一条
    不存在的运行上。
    """
    from .run_control import RunControl

    RunControl(dsn=dsn).ensure_schema()


def _m10_compaction_records(dsn, ctx):
    """压缩记录表：`agent_history_compactions`（2026-11-02）。

    DDL 出处：`src.agents.compaction_store.SCHEMA_STATEMENTS`（本文件不重写）。

    为什么要有这张表：历史压缩从"清空会话再写回摘要"改成"日志只追加 + 读取时投影"，
    压缩产物必须有个地方落 —— 落在会话表里就等于又改了一次库。
    老库上这张表不存在时，`_latest_record()` 会退化成"从来没压过" ⇒ 历史全量返回，
    功能退级但不报错，正是那种**要跑很久才会被发现**的错。所以单独占一个版本号。

    幂等：全部 `IF NOT EXISTS`。
    """
    import psycopg

    from src.agents.compaction_store import SCHEMA_STATEMENTS

    with psycopg.connect(dsn, connect_timeout=10, autocommit=True) as conn:
        with conn.cursor() as cur:
            for statement in SCHEMA_STATEMENTS:
                cur.execute(statement)


def _m11_long_term_memories(dsn, ctx):
    """长期记忆表：`agent_memories`（2026-11-02）。

    DDL 出处：`src.agents.memory_store.SCHEMA_STATEMENTS`（本文件不重写）。

    ⚠️ 那个部分唯一索引（``WHERE invalid_at IS NULL``）是这张表的**语义核心**，
    不是性能优化：全表唯一会挡住"把设置改回原样"（新行哈希与已失效的旧行相同，
    插入直接失败），而且只在用户真的改回去时才炸 —— 测不到、也想不到。

    幂等：全部 `IF NOT EXISTS`。
    """
    import psycopg

    from src.agents.memory_store import SCHEMA_STATEMENTS

    with psycopg.connect(dsn, connect_timeout=10, autocommit=True) as conn:
        with conn.cursor() as cur:
            for statement in SCHEMA_STATEMENTS:
                cur.execute(statement)


MIGRATIONS: tuple = (
    Migration(1, "rag_chunks 向量表", _m1_rag_chunks),
    Migration(2, "review_queue 审核队列表", _m2_review_queue),
    Migration(3, "session_owners 会话归属表", _m3_session_owners),
    Migration(4, "review_queue 审计列（操作者/时间/决定备注）", _m4_review_queue_audit),
    Migration(5, "session_owners 最后活动时间列", _m5_session_owners_activity),
    Migration(6, "session_owners 用户维度列（同租户内隔离到人）", _m6_session_owners_user),
    Migration(7, "agent_sessions / agent_messages 跨轮对话历史表", _m7_agent_session_tables),
    Migration(8, "tenants / users / tenant_join_requests 账号体系表", _m8_account_tables),
    Migration(9, "agent_runs 运行登记表（停止任务）", _m9_run_control_table),
    Migration(10, "agent_history_compactions 压缩记录表", _m10_compaction_records),
    Migration(11, "agent_memories 长期记忆表", _m11_long_term_memories),
)


# ---------------------------------------------------------------------------
# 结构探测（全部只读）
# ---------------------------------------------------------------------------
def embedding_dim(conn, table) -> int:
    """读某表现有 `embedding` 列的实际维度；表或列不存在返回 0。

    实现放在数据层（`src.rag.vector_store.embedding_dim`）—— **维度探测只有那一份**。
    这里只做转发，让服务层调用方不必关心它在哪一层。
    """
    from src.rag.vector_store import embedding_dim as _impl

    return _impl(conn, table)


def _table_rows(conn, table) -> int:
    """读表行数；表不存在返回 -1（以区别于"存在但是空表"）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (table,))
        if cur.fetchone()[0] is None:
            return -1
        cur.execute(f"SELECT count(*) FROM {table}")
        return int(cur.fetchone()[0])


def _applied_versions(conn) -> set:
    """读已应用的迁移版本集合（迁移表不存在时返回空集）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('schema_migrations')")
        if cur.fetchone()[0] is None:
            return set()
        cur.execute("SELECT version FROM schema_migrations")
        return {int(r[0]) for r in cur.fetchall()}


def status(dsn, table="rag_chunks", dim=None) -> dict:
    """部署前预检：**只读**，不改任何结构。

    dsn: 连接串。
    table: 向量表名。
    dim: 代码期望的向量维度（None → `default_dim()`）。

    返回 dict：ok / pg_version / vector_version / tables / dim / migrations / problems。
    连接异常被捕获成 `ok=False` + problems 而不是抛出 —— 它是给人看的诊断工具，
    不该因为探不通就崩掉。
    """
    dim = default_dim() if dim is None else int(dim)
    out = {
        "ok": True,
        "dsn_configured": bool(dsn),
        "pg_version": "",
        "vector_version": "",
        "tables": {},
        "dim": {"expected": dim, "actual": 0, "match": None},
        "migrations": {"applied": [], "pending": []},
        "problems": [],
    }
    if not dsn:
        out["ok"] = False
        out["problems"].append("DB_URL 未配置：向量存储与会话检查点都会降级为内存（重启丢数据）")
        return out

    try:
        import psycopg

        conn = psycopg.connect(dsn, connect_timeout=8)
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["problems"].append(f"连不上数据库：{type(e).__name__}: {e}")
        return out

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            out["pg_version"] = str(cur.fetchone()[0]).split(" on ")[0]
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            r = cur.fetchone()
            out["vector_version"] = r[0] if r else ""

        if not out["vector_version"]:
            out["ok"] = False
            out["problems"].append(
                "vector 扩展未安装：请用 pgvector 镜像，或在库里执行 "
                "`CREATE EXTENSION vector`（需超级用户权限）"
            )

        for t in (table, "review_queue", "session_owners",
                  "tenants", "users", "tenant_join_requests"):
            out["tables"][t] = _table_rows(conn, t)

        actual = embedding_dim(conn, table)
        out["dim"]["actual"] = actual
        # 表不存在（actual=0）不算 mismatch —— 那是"待首次创建"，由迁移负责
        out["dim"]["match"] = (None if actual == 0 else actual == dim)
        if actual and actual != dim:
            out["ok"] = False
            out["problems"].append(
                f"向量维度不一致：库里是 vector({actual})，代码期望 vector({dim})。"
                "这通常意味着换过 embedding 模型。**改列类型无法原地完成**，"
                f"请把 PGVECTOR_TABLE 换成新表名（如 {table}_d{dim}）后重新灌库，"
                "旧表保留以便回滚比对。"
            )
        if out["tables"].get(table, -1) == -1:
            out["problems"].append(
                f"表 {table} 不存在：执行 `python -m app.migrations --apply` 创建"
                "（注意同时用 --dim 指定真实向量维度）"
            )

        applied = _applied_versions(conn)
        out["migrations"]["applied"] = sorted(applied)
        pending = sorted(m.version for m in MIGRATIONS if m.version not in applied)
        out["migrations"]["pending"] = pending
        if pending:
            # 待应用 = 结构还没到代码期望的版本。算作"问题"是对的：
            # 部署前的预检就该把"库是旧的"当成需要处理的项。
            out["ok"] = False
            out["problems"].append(
                f"有 {len(pending)} 条迁移待应用：{pending}。"
                "执行 `python -m app.migrations --apply`（幂等，可安全重复执行）"
            )
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["problems"].append(f"结构探测过程中出错：{type(e).__name__}: {e}")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    return out


# ---------------------------------------------------------------------------
# 应用迁移
# ---------------------------------------------------------------------------
def apply_pending(dsn, table="rag_chunks", dim=None, dry_run=False) -> list:
    """按版本序应用尚未应用的迁移，返回本次应用的版本列表。

    dsn: 连接串。空则抛 RuntimeError —— 静默跳过迁移比报错危险得多。
    table / dim: 传给迁移动作的参数（dim=None → `default_dim()`）。
    dry_run: True 时只列出"会做什么"，不执行也不记账。

    幂等：已记录在 `schema_migrations` 的版本会跳过，可反复执行。
    """
    if not dsn:
        raise RuntimeError("未配置 DB_URL，无法应用迁移")

    import psycopg

    ctx = {"table": table, "dim": default_dim() if dim is None else int(dim)}
    done = []
    with psycopg.connect(dsn, connect_timeout=10, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(_MIGRATIONS_TABLE)
        applied = _applied_versions(conn)

        for m in MIGRATIONS:
            if m.version in applied:
                continue
            if dry_run:
                done.append(m.version)
                continue
            logging.info("应用迁移 v%d：%s", m.version, m.name)
            m.apply(dsn, ctx)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (%s, %s) "
                    "ON CONFLICT (version) DO NOTHING",
                    (m.version, m.name),
                )
            done.append(m.version)
    return done


def current_version(dsn) -> int:
    """读当前库的结构版本（无迁移表 = 0）。"""
    if not dsn:
        return 0
    import psycopg

    with psycopg.connect(dsn, connect_timeout=8) as conn:
        applied = _applied_versions(conn)
    return max(applied) if applied else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _format_status(st: dict) -> str:
    """把 status() 的结果排成人眼可读的文本。"""
    lines = [
        f"数据库：{st['pg_version'] or '（未连接）'}   pgvector：{st['vector_version'] or '未安装'}",
        "",
        "表：",
    ]
    for name, rows in st["tables"].items():
        lines.append(f"  {name:<18} {'不存在' if rows < 0 else f'{rows} 行'}")

    d = st["dim"]
    if d["actual"] == 0:
        dim_line = f"期望 vector({d['expected']})，库里尚未建表"
    elif d["match"]:
        dim_line = f"vector({d['actual']}) —— 与期望一致"
    else:
        dim_line = f"库里 vector({d['actual']}) ✗ 与期望 vector({d['expected']}) 不一致"

    mg = st["migrations"]
    lines += [
        "",
        f"向量维度：{dim_line}",
        "",
        f"已应用迁移：{mg['applied'] or '（无）'}",
        f"待应用迁移：{mg['pending'] or '（无）'}",
        "",
    ]
    if st["problems"]:
        lines.append("发现的问题：")
        lines += [f"  · {p}" for p in st["problems"]]
    else:
        lines.append("未发现问题。")
    lines += ["", "结论：" + ("可以部署" if st["ok"] else "**先处理上面的问题再部署**")]
    return "\n".join(lines)


def main(argv=None) -> int:
    """命令行入口：默认只做预检（只读），`--apply` 才会改结构。

    退出码：0 = 一切就绪；1 = 有未解决的问题（可直接用在部署脚本的 `&&` 链里）。
    """
    from dotenv import load_dotenv

    load_dotenv()

    p = argparse.ArgumentParser(
        prog="python -m app.migrations",
        description="数据库结构预检与迁移（rag_chunks / review_queue / session_owners）",
    )
    p.add_argument("--status", action="store_true", help="只做预检并报告（默认行为）")
    p.add_argument("--apply", action="store_true", help="应用待应用的迁移")
    p.add_argument("--dry-run", action="store_true", help="配合 --apply：只列出会做什么")
    p.add_argument("--dim", type=int, default=None,
                   help="向量维度（默认取 RAG_EMBED_DIM，再兜底 1024）")
    p.add_argument("--table", default=os.getenv("PGVECTOR_TABLE", "rag_chunks"),
                   help="向量表名（默认 PGVECTOR_TABLE 或 rag_chunks）")
    args = p.parse_args(argv)

    dim = default_dim() if args.dim is None else args.dim
    dsn = os.getenv("DB_URL", "")
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.apply:
        if args.dim is None:
            print(f"注意：未指定 --dim，按 {dim} 建表。若 embedding 模型不是这个维度，"
                  f"请改用 --dim <真实维度>。")
        done = apply_pending(dsn, table=args.table, dim=dim, dry_run=args.dry_run)
        print(f"{'将应用' if args.dry_run else '已应用'}迁移：{done or '（无待应用）'}")
        if args.dry_run:
            return 0

    st = status(dsn, table=args.table, dim=dim)
    print(_format_status(st))
    return 0 if st["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
