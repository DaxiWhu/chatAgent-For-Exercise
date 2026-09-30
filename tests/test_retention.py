"""数据保留与会话删除的契约。

这三块合起来解决的是同一个问题：**数据只进不出**。
  - `session_owners.updated_at`：让"超期"能按**最后活动时间**判断；
  - `DELETE /v1/sessions/{id}`：履行删除请求；
  - `scripts/retention_sweep.py`：兜底回收，防表无界增长。

这里钉住的都是**失效后不可逆**的那部分：

  1. 删除必须做归属校验 —— 否则猜到 thread_id 就能删掉别人的会话，
     比"读"严重一个量级（读是泄露，删是破坏）；
  2. 清理脚本默认**不删**（dry-run），`--apply` 必须由人显式给出；
  3. 保留期未配置时脚本**拒绝执行** —— 缺省值给一个"看起来合理"的天数，
     意味着一次升级就静默删掉用户的历史；
  4. 迁移版本号与 SCHEMA_VERSION 必须一致 —— 不一致时老库永远不会补列，
     而清理脚本会因此拒绝清理整表（数据继续增长，且没人知道保留策略没生效）。
"""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("DB_URL", "")

pytest.importorskip("fastapi.testclient", reason="需要 httpx")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as M  # noqa: E402
from app.ratelimit import RateLimiter  # noqa: E402
from app.review_queue import ReviewQueue  # noqa: E402
from app.sessions import SessionRegistry  # noqa: E402

client = TestClient(M.app)


# ---------------- 归属登记的注销 ----------------

def test_release_frees_the_thread_id_for_reuse():
    """注销后该 thread_id 必须能被**别的租户**重新认领。

    只删检查点而留着归属行，会让这个 id 永久挂在旧租户名下：
    新租户拿到它会被判 403，而它其实已经不存在了。
    """
    reg = SessionRegistry(enabled=True)
    reg.claim("t-1", "acme")
    assert reg.owner_of("t-1") == "acme"

    assert reg.release("t-1") is True
    assert reg.owner_of("t-1") is None

    ok, owner = reg.claim("t-1", "globex")
    assert ok is True and owner == "globex", "注销后 thread_id 没有被释放"


def test_release_of_unknown_session_is_false_not_an_error():
    """注销未登记的会话返回 False，不是异常 —— 删除流程要能幂等重试。"""
    assert SessionRegistry(enabled=True).release("never-seen") is False


def test_release_is_a_noop_when_disabled():
    """单租户（未启用归属校验）时 release 不做事，也不该抛。"""
    assert SessionRegistry(enabled=False).release("t") is False


def test_claim_refreshes_activity_only_for_the_matching_tenant(monkeypatch):
    """刷新"最后活动时间"的 SQL **必须带租户条件**。

    数据保留脚本按 `updated_at` 判超期。若这句 UPDATE 不限定租户，
    任何人拿自己的租户名请求一次别人的 thread_id（哪怕会被判 403），
    都能把那条会话的时间戳刷新 —— 等于把"什么时候清理"的开关交给了攻击者，
    被盯上的会话可以永久免于清理。

    这里断言的是**发出去的 SQL 文本**：这条约束只存在于语句里，
    没有任何返回值的差异能反映它（`claim` 照常返回 False）。
    """
    executed: list[str] = []

    class _Cur:
        rowcount = 0

        def execute(self, sql, params=None):
            executed.append(" ".join(sql.split()))

        def fetchone(self):
            return ("acme",)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import psycopg

    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: _Conn())

    reg = SessionRegistry(dsn="postgresql://fake", enabled=True)
    reg.claim("t-1", "acme")

    updates = [s for s in executed if s.upper().startswith("UPDATE")]
    assert updates, "claim 没有刷新 updated_at —— 清理会按「创建时间」误判超期"
    assert any("updated_at" in s for s in updates), (
        f"claim 没有刷新最后活动时间，实际执行：{updates}"
    )
    assert all("tenant" in s for s in updates), (
        f"刷新活动时间的语句没有限定租户：{updates}\n"
        "任何人都能借一次被拒的请求给别人的会话续命，"
        "被盯上的会话将永久免于清理。"
    )


# ---------------- 审核队列的删除（区别于"处理完毕"） ----------------

def test_remove_deletes_while_resolve_keeps_the_record():
    """`remove` 是**删除**，`resolve` 是**留痕** —— 两者不能互相代替。

    用 `resolve(..., "dismissed")` 冒充删除是错的：草稿正文仍在库里，
    而"已删除"的承诺已经做出去了。
    """
    q = ReviewQueue(enabled=True)
    q.add("t-keep", "acme", "草稿A", 50, "太短")
    q.add("t-drop", "acme", "草稿B", 40, "太短")

    assert q.resolve("t-keep", "accepted", actor="acme") is True
    assert q.record("t-keep") is not None, "resolve 之后记录必须还在（审计证据）"

    assert q.remove("t-drop") is True
    assert q.record("t-drop") is None, "remove 之后记录必须真的消失"


def test_remove_of_unknown_record_is_false():
    assert ReviewQueue(enabled=True).remove("never-seen") is False


# ---------------- DELETE 端点 ----------------

class _FakeGraph:
    """假图：只记录 `checkpointer.adelete_thread` 是否被调用。

    ⚠️ 删除接口挂在 **`graph.checkpointer`** 上，不是图上 —— 这是
    langgraph 的结构（`graph` 是编译产物，saver 在它的属性里）。
    第一次写本文件时把方法直接放在图上，于是端点拿不到删除接口、
    静默返回 `checkpoints: False`，用例因此变红。
    """

    def __init__(self):
        self.deleted: list[str] = []
        self.checkpointer = self          # 省一层，方法名与真实结构一致

    async def astream(self, incoming, config, stream_mode):   # pragma: no cover
        yield None, {}

    async def aget_state(self, config):                       # pragma: no cover
        raise AssertionError("DELETE 不应读会话状态")

    async def adelete_thread(self, thread_id):
        self.deleted.append(thread_id)


class _SyncOnlyCheckpointer:
    """只有同步 `delete_thread` 的 saver（内存后端就是这样）。"""

    def __init__(self):
        self.deleted: list[str] = []

    def delete_thread(self, thread_id):
        self.deleted.append(thread_id)


@pytest.fixture()
def open_limits(monkeypatch):
    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=1000))


def test_delete_removes_all_three_kinds_of_data(monkeypatch, open_limits):
    """一次删除必须同时清掉检查点、审核队列、归属登记 —— 缺一类都不算删干净。"""
    fake = _FakeGraph()
    q = ReviewQueue(enabled=True)
    reg = SessionRegistry(enabled=True)

    q.add("del-1", "acme", "草稿", 50, "太短")
    reg.claim("del-1", "acme")

    monkeypatch.setattr(M, "graph", fake)
    monkeypatch.setattr(M, "_REVIEW_QUEUE", q)
    monkeypatch.setattr(M, "_SESSIONS", reg)

    r = client.delete("/v1/sessions/del-1", headers={"X-Tenant-Id": "acme"})
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] == {"checkpoints": True, "review": True, "ownership": True}
    assert fake.deleted == ["del-1"]
    assert q.record("del-1") is None
    assert reg.owner_of("del-1") is None


def test_delete_uses_sync_delete_when_async_is_absent(monkeypatch, open_limits):
    """同步 saver 只有 `delete_thread` —— 必须走它，不能"什么都不删还报成功"。

    按方法名探测（而不是 `isinstance`）就是为了这个：内存后端与 PG 后端的
    方法名不同，漏一边的后果是"用户以为删了、数据还在"。
    """
    cp = _SyncOnlyCheckpointer()
    monkeypatch.setattr(M, "graph", type("G", (), {"checkpointer": cp})())
    monkeypatch.setattr(M, "_REVIEW_QUEUE", ReviewQueue(enabled=True))
    monkeypatch.setattr(M, "_SESSIONS", SessionRegistry(enabled=False))

    r = client.delete("/v1/sessions/sync-1")
    assert r.status_code == 200
    assert r.json()["deleted"]["checkpoints"] is True
    assert cp.deleted == ["sync-1"]


def test_delete_is_forbidden_for_another_tenant(monkeypatch, open_limits):
    """别人的会话**删不掉** —— 这是本端点最重要的一条。

    `thread_id` 由客户端提供，缺了归属校验，猜到 id 就能删掉别人的会话；
    读路径的越权最多泄露，删路径的越权是**不可逆的破坏**。
    """
    reg = SessionRegistry(enabled=True)
    reg.claim("other-1", "acme")
    monkeypatch.setattr(M, "_SESSIONS", reg)
    monkeypatch.setattr(M, "graph", _FakeGraph())

    # 以另一个租户的身份发起（单租户头自报模式下 tenant 即请求头值）
    r = client.delete("/v1/sessions/other-1", headers={"X-Tenant-Id": "globex"})
    assert r.status_code in (403, 404), (
        f"越权删除没有被拦住（{r.status_code}）—— 归属校验缺失或失效"
    )


def test_delete_is_idempotent_in_single_tenant_mode(monkeypatch, open_limits):
    """单租户下删不存在的会话返回 200 + 全 False（幂等），而不是 404。

    返回 404 会把端点变成"这个 thread_id 存在吗"的枚举探针。
    删除本身是幂等的，`deleted` 明细已经如实说明每一项删掉没有。
    """
    monkeypatch.setattr(M, "_SESSIONS", SessionRegistry(enabled=False))
    monkeypatch.setattr(M, "_REVIEW_QUEUE", ReviewQueue(enabled=True))
    monkeypatch.setattr(M, "graph", _FakeGraph())

    r = client.delete("/v1/sessions/no-such-session")
    assert r.status_code == 200
    assert r.json()["deleted"] == {"checkpoints": True, "review": False,
                                   "ownership": False}


# ---------------- 清理脚本的安全默认 ----------------

@pytest.fixture()
def sweep():
    cwd = os.getcwd()
    try:
        import scripts.retention_sweep as mod
        yield mod
    finally:
        os.chdir(cwd)


def _run_sweep(mod, monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["retention_sweep.py", *argv])
    return mod.main()


def test_sweep_refuses_without_an_explicit_retention_window(sweep, monkeypatch, capsys):
    """保留期未配置时**拒绝执行**。

    给一个"看起来合理"的默认天数（比如 30）意味着一次升级就静默删掉用户的
    历史会话。0 = 未配置，此时脚本必须停下并要求显式给出 `--days`。
    """
    monkeypatch.setattr(sweep.config, "RETENTION_DAYS", 0)
    rc = _run_sweep(sweep, monkeypatch, ["--dsn", "postgresql://x"])
    assert rc == 2, "未配置保留期却照常执行 —— 会删掉本不该删的数据"
    assert "--days" in capsys.readouterr().err


def test_sweep_refuses_without_a_database(sweep, monkeypatch, capsys):
    """没有 DB_URL 时明确报错，而不是"清理了 0 行"看起来成功。"""
    monkeypatch.setattr(sweep.config, "DB_URL", "")
    rc = _run_sweep(sweep, monkeypatch, ["--days", "30"])
    assert rc == 2
    assert "DB_URL" in capsys.readouterr().err


# ---------------- 清理脚本的 SQL 行为（假 cursor） ----------------

class _FakeCursor:
    """按 SQL 关键字回应的假游标，用来观察"脚本会执行哪些语句"。"""

    def __init__(self, tables=("session_owners", "review_queue", "checkpoints",
                               "checkpoint_blobs", "checkpoint_writes"),
                 has_updated_at=True, counts=7, threads=("t-old",),
                 filter_params=True):
        self.executed: list[tuple[str, object]] = []
        self.rowcount = 0
        self._tables = list(tables)
        self._has_updated = has_updated_at
        self._counts = counts
        self._threads = list(threads)
        self._filter_params = filter_params

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        self.rowcount = 1

    def fetchone(self):
        sql = self.executed[-1][0]
        if "information_schema.columns" in sql:
            return (1,) if self._has_updated else None
        return (self._counts,)

    def fetchall(self):
        sql = self.executed[-1][0]
        if "information_schema.tables" in sql:
            # 必须按**参数**过滤（真实 PG 就是这样）。若忽略参数把全部表都返回，
            # 就测不出"脚本是否会删到白名单之外的表" —— 假游标比真实行为宽松时，
            # 用例会漏掉真实的破坏路径（本文件正是这样才发现代码缺一道 Python 侧收口）。
            # ⚠️ `ANY(%s)` 的参数形态是「包着一个 list 的 tuple」：`([t1, t2],)`。
            # 直接 `set(params)` 会拿 list 当元素 → unhashable。
            params = self.executed[-1][1] or ()
            names = params[0] if params and isinstance(params[0], (list, tuple, set)) else ()
            wanted = set(names) if self._filter_params else set(self._tables)
            return [(t,) for t in self._tables if t in wanted]
        if "SELECT thread_id" in sql:
            return [(t,) for t in self._threads]
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def deletes(self):
        return [sql for sql, _ in self.executed if sql.upper().startswith("DELETE")]


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _with_fake_db(sweep, monkeypatch, cur):
    monkeypatch.setattr(sweep, "_connect", lambda dsn: _FakeConn(cur))


def test_dry_run_executes_no_delete(sweep, monkeypatch, capsys):
    """dry-run **不得**发出任何 DELETE —— 这是本脚本唯一的安全底线。

    默认不删的价值全在这里：确认前它必须一行都不动。
    """
    cur = _FakeCursor()
    _with_fake_db(sweep, monkeypatch, cur)
    monkeypatch.setattr(sweep.config, "DB_URL", "postgresql://x")

    rc = _run_sweep(sweep, monkeypatch, ["--days", "30"])
    assert rc == 0
    assert cur.deletes() == [], f"dry-run 竟然执行了删除：{cur.deletes()}"
    assert "[dry-run]" in capsys.readouterr().out


def test_apply_deletes_only_from_the_table_whitelist(sweep, monkeypatch):
    """真删时只允许碰白名单里的表。

    表名是拼进 SQL 的（`DELETE FROM {t}`），所以必须来自内部常量而不是
    任何外部输入 —— 否则一旦某个参数被拼进来就是注入。
    """
    cur = _FakeCursor()
    _with_fake_db(sweep, monkeypatch, cur)
    monkeypatch.setattr(sweep.config, "DB_URL", "postgresql://x")

    rc = _run_sweep(sweep, monkeypatch, ["--days", "30", "--apply"])
    assert rc == 0
    dels = cur.deletes()
    assert dels, "apply 没有执行任何删除"

    allowed = set(sweep.CHECKPOINT_TABLES) | {"review_queue", "session_owners"}
    for sql in dels:
        table = sql.split()[2]
        assert table in allowed, f"删除了白名单之外的表：{table}"
    # 三张检查点表都必须被真正删到（只删 `checkpoints` 会留下一堆孤儿 blob 行）：
    # langgraph 把状态分散在 checkpoints / checkpoint_blobs / checkpoint_writes 里。
    touched = {sql.split()[2] for sql in dels}
    assert set(sweep.CHECKPOINT_TABLES) <= touched, (
        f"检查点清理没有覆盖全部三张表，实际碰到：{sorted(touched)}"
    )


def test_sweep_stays_inside_the_whitelist_even_if_the_query_over_returns(
        sweep, monkeypatch):
    """即便表清单查询返回了白名单之外的表，也**不得**删它们。

    模拟"那句 `information_schema` 查询被改坏 / 参数传错"：让假游标忽略参数、
    把库里所有表都返回（含 `review_queue`、`session_owners`，以及**诱饵表**
    `rag_chunks`）。表名最终是拼进 `DELETE FROM {t}` 的，所以白名单必须在
    Python 侧再收一次口 —— 只靠 SQL 的 `= ANY(%s)` 保证，出问题时不会报错，
    只会**删多**。

    （这不是假设：本文件第一版就是这样红的 —— 脚本试图 `DELETE FROM review_queue`
    并用它去索引 `checkpoints` 计数，直接 KeyError。测试先于真实事故发现了它。）
    """
    cur = _FakeCursor(
        tables=("checkpoints", "checkpoint_blobs", "checkpoint_writes",
                "rag_chunks", "session_owners", "review_queue"),   # rag_chunks 是诱饵
        filter_params=False)
    _with_fake_db(sweep, monkeypatch, cur)
    monkeypatch.setattr(sweep.config, "DB_URL", "postgresql://x")

    rc = _run_sweep(sweep, monkeypatch, ["--days", "30", "--apply"])
    assert rc == 0
    touched = {sql.split()[2] for sql in cur.deletes()}
    assert "rag_chunks" not in touched, (
        "白名单收口失效：脚本删到了一张与保留策略无关的表（这里是向量表）——\n"
        "删表不会报错，只会让检索数据静默消失。"
    )
    assert touched <= set(sweep.CHECKPOINT_TABLES) | {"review_queue", "session_owners"}


def test_missing_updated_at_column_is_reported_not_silently_skipped(sweep, monkeypatch, capsys):
    """缺 `updated_at` 时必须在预演里**明确警告**。

    静默跳过等于"保留策略从未生效且没人知道" —— 数据继续无界增长，
    而运维以为已经配好了清理任务。
    """
    cur = _FakeCursor(has_updated_at=False)
    _with_fake_db(sweep, monkeypatch, cur)
    monkeypatch.setattr(sweep.config, "DB_URL", "postgresql://x")

    monkeypatch.setattr(sys, "argv",
                        ["retention_sweep.py", "--days", "30"])
    rc = sweep.main()
    err = capsys.readouterr().err
    assert rc == 0
    assert "updated_at" in err, "缺列时没有给出任何提示"


# ---------------- 迁移版本的一致性 ----------------

def test_schema_version_matches_the_latest_migration():
    """`SCHEMA_VERSION` 必须等于最大迁移版本号。

    两者脱节时 `--status` 会显示"可以部署"（pending 为空），
    而老库里缺的列永远不会被补上。这条在加迁移时极易漏改。
    """
    from app import migrations

    latest = max(m.version for m in migrations.MIGRATIONS)
    assert migrations.SCHEMA_VERSION == latest, (
        f"SCHEMA_VERSION={migrations.SCHEMA_VERSION} 与最新迁移 v{latest} 不一致 —— "
        "老库不会补列，而 --status 会谎报可以部署"
    )


def test_migration_versions_are_unique_and_contiguous():
    """版本号必须唯一且连续 —— 跳号会让 `apply_pending` 跳过整整一代变更。"""
    from app import migrations

    versions = sorted(m.version for m in migrations.MIGRATIONS)
    assert versions == list(range(1, len(versions) + 1)), (
        f"迁移版本号不连续或有重复：{versions}"
    )
