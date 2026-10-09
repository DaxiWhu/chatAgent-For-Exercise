"""PG 连接池（`app/pg.py`）的行为守卫。

为什么单独测这一层
==================
池是"三个存储类共用的基础设施"，它错了的表现是**延迟上升而没有任何错误**
（连接不够就排队）。反过来，如果它把"建不起来"缓存成了半个池，症状会变成
"永久 503 且日志里只有超时"。两类都是难查的形态，所以行为必须被钉住：

  1. 未配 `DB_URL` → `PoolNotConfigured`（调用方据此降级到内存后端）；
  2. 建池/取连接失败 → `PoolUnavailable`（fail-closed，不返回半个池）；
  3. 同一个 dsn **共用**一个池（否则三个存储类各开 min_size 条，连接数 ×3）；
  4. `with pg.connect(dsn) as conn:` 必须"借了必还"——归还发生在池这一层，
     写错就会每次请求漏一条连接，比不用池更糟；
  5. `stats()` / 日志里的 DSN **必须脱敏**（DSN 里带口令）。
"""

from __future__ import annotations

import ast
import contextlib
from pathlib import Path

import pytest

from app import config, pg

ROOT = Path(__file__).resolve().parent.parent


class _FakePool:
    """够用的假池：记录借/还/开关，`connection()` 语义与真池一致。"""

    def __init__(self, *, fail_open: bool = False):
        self.opened = False
        self.closed = False
        self.borrowed = 0
        self.returned = 0
        self._fail_open = fail_open

    def open(self, wait: bool = False, timeout=None) -> None:
        if self._fail_open:
            raise RuntimeError("connection refused")
        self.opened = True

    def close(self) -> None:
        self.closed = True

    @contextlib.contextmanager
    def connection(self, timeout=None):
        self.borrowed += 1
        try:
            yield object()
        finally:
            self.returned += 1

    def get_stats(self) -> dict:
        return {"pool_size": 3, "pool_available": 2, "requests_waiting": 1}


@pytest.fixture(autouse=True)
def _clean_pools():
    """每个用例前后都清空池注册表 —— 池是模块级全局状态，会跨用例串味。"""
    pg._POOLS.clear()
    yield
    for p in list(pg._POOLS.values()):
        with contextlib.suppress(Exception):
            p.close()
    pg._POOLS.clear()


def test_connect_without_dsn_raises_not_configured():
    """未配 DB_URL 时必须是"没池可用"，而不是一个含义模糊的空指针/连接错误。"""
    for blank in ("", "   "):
        with pytest.raises(pg.PoolNotConfigured):
            pg.connect(blank)


def test_connect_borrows_and_returns_to_the_pool(monkeypatch):
    fake = _FakePool()
    monkeypatch.setattr(pg, "_make_pool", lambda dsn: fake)

    with pg.connect("postgresql://u:p@h/db") as conn:
        assert conn is not None
        assert fake.borrowed == 1
        assert fake.returned == 0      # 还在用

    assert fake.returned == 1, "连接没有归还 —— 每次请求都会漏一条 PG 连接"
    assert fake.opened, "池没有被 open"


def test_pool_open_failure_fails_closed_and_is_not_cached(monkeypatch):
    """建不起来必须抛错，且**不能**把半个池缓存下来。"""
    fake = _FakePool(fail_open=True)
    monkeypatch.setattr(pg, "_make_pool", lambda dsn: fake)

    with pytest.raises(pg.PoolUnavailable):
        pg.connect("postgresql://u:p@h/db")

    assert fake.closed, "建池失败后没有关掉半成品池，会泄漏后台线程/连接"
    assert pg.stats() == {}, "失败的池被缓存了 —— 后续请求会一直等到 PoolTimeout"


def test_same_dsn_shares_one_pool_different_dsn_gets_its_own(monkeypatch):
    """同一个 dsn 共用池：否则三个存储类各自 min_size 条，总连接数凭空 ×3。"""
    made: list[_FakePool] = []

    def _mk(dsn):
        p = _FakePool()
        made.append(p)
        return p

    monkeypatch.setattr(pg, "_make_pool", _mk)

    with pg.connect("postgresql://u@h/same"):
        pass
    with pg.connect("postgresql://u@h/same"):
        pass
    with pg.connect("postgresql://u@h/other"):
        pass

    assert len(made) == 2, f"池没有按 dsn 复用/隔离，实际建了 {len(made)} 个"


def test_close_all_closes_pools_and_is_idempotent(monkeypatch):
    fake = _FakePool()
    monkeypatch.setattr(pg, "_make_pool", lambda dsn: fake)

    with pg.connect("postgresql://u@h/db"):
        pass

    pg.close_all()
    assert fake.closed
    assert pg.stats() == {}

    pg.close_all()   # 再调一次不能抛（lifespan 的 finally 里可能已经关过）
    assert fake.closed


def test_stats_and_log_never_leak_the_password(monkeypatch):
    """DSN 里带口令 ⇒ 进指标/日志前必须脱敏。"""
    fake = _FakePool()
    monkeypatch.setattr(pg, "_make_pool", lambda dsn: fake)

    with pg.connect("postgresql://langgraph_app:super-secret@db:5432/kb?sslmode=disable"):
        pass

    stats = pg.stats()
    assert len(stats) == 1
    key = next(iter(stats))
    assert "super-secret" not in key, f"指标 key 里带出了口令：{key}"
    assert "***" in key, f"没有做脱敏：{key}"
    assert "db:5432" in key, "脱敏把主机也一起抹掉了 —— 排障时会认不出是哪个库"


def test_stats_surfaces_waiting_when_the_pool_is_saturated(monkeypatch):
    """池打满时 `waiting` 必须能被看到 —— 这是它唯一的可观测信号。"""
    fake = _FakePool()
    monkeypatch.setattr(pg, "_make_pool", lambda dsn: fake)

    with pg.connect("postgresql://u@h/db"):
        pass

    from app import health

    ok, detail = health._check_pg_pool()
    assert ok is True, "池占用高是「排队」不是「故障」，不该判实例不健康"
    assert "waiting=1" in detail
    assert "super" not in detail


def test_health_reports_no_pool_without_db_url(monkeypatch):
    from app import health

    monkeypatch.setattr(config, "DB_URL", "")
    ok, detail = health._check_pg_pool()
    assert ok is True
    assert "DB_URL" in detail


def test_pool_config_bounds_are_sane():
    """配置边界：min 不能大于 max，timeout 不能小于 1（否则等于"不等"）。"""
    assert 0 <= config.PG_POOL_MIN <= config.PG_POOL_MAX
    assert config.PG_POOL_MAX >= 1
    assert config.PG_POOL_TIMEOUT >= 1


def test_stores_connect_through_the_pool_not_psycopg_directly():
    """反向锁：三个存储类必须走池，不能自己 `psycopg.connect`。

    少了这条守卫，某天有人加一个"顺手直连"的分支就能让"并发数 = PG 后端数"
    悄悄回来 —— 而它在功能上完全正常，只是把池白建了。
    """
    targets = ("app/sessions.py", "app/accounts.py", "app/review_queue.py")
    offenders: list[str] = []
    for rel in targets:
        src = (ROOT / rel).read_text(encoding="utf-8")
        tree = ast.parse(src, filename=rel)
        if "pg.connect(" not in src:
            offenders.append(f"{rel}：没有走 `pg.connect(...)`")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if (isinstance(f, ast.Attribute) and f.attr == "connect"
                    and isinstance(f.value, ast.Name) and f.value.id == "psycopg"):
                offenders.append(f"{rel}:{node.lineno} 仍在直接 `psycopg.connect`")
    assert not offenders, (
        "以下位置绕过了共享连接池（会让连接数重新与并发同增）：\n  "
        + "\n  ".join(offenders)
    )
