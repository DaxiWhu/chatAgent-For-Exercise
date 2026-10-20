"""健康检查：liveness 与 readiness 的语义差异与降级行为。

readiness 的依赖探测全部被替换成桩：这里验证的是**编排逻辑**
（哪一项不过就整体不健康、明细结构是否可读），不是真的去连库。
"""
from app import config, health


def test_liveness_never_touches_dependencies(monkeypatch):
    """存活探针必须只反映进程本身 —— 依赖挂了也不能让它失败（否则会触发重启风暴）。"""
    def _explode(*a, **kw):
        raise AssertionError("liveness 不应探测任何外部依赖")

    monkeypatch.setattr(health, "_check_db", _explode)
    monkeypatch.setattr(health, "_check_vector_store", _explode)

    body = health.liveness()
    assert body["status"] == "ok"
    assert "release" in body


def test_readiness_ok_when_all_checks_pass(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "sk-test")
    monkeypatch.setattr(health, "_check_db", lambda: (True, "stub"))
    monkeypatch.setattr(health, "_check_vector_store", lambda: (True, "stub"))
    monkeypatch.setattr(health, "_check_pg_pool", lambda: (True, "未建立"))

    ok, body = health.readiness()
    assert ok and body["status"] == "ok"
    # `pg_pool` 是**可见性**项而非健康判据（占用高只代表在排队），但它必须在明细里 —— 
    # 少了它，连接池打满的唯一症状是"延迟上升、没有任何错误"，排障会一路往模型上找。
    assert set(body["checks"]) == {"llm", "db", "vector_store", "run_state", "pg_pool"}
    assert all(v["ok"] for v in body["checks"].values())


def test_readiness_degraded_when_one_check_fails(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "sk-test")
    monkeypatch.setattr(health, "_check_db", lambda: (False, "连不上"))
    monkeypatch.setattr(health, "_check_vector_store", lambda: (True, "stub"))

    ok, body = health.readiness()
    assert not ok and body["status"] == "degraded"
    assert body["checks"]["db"]["ok"] is False
    assert body["checks"]["db"]["detail"] == "连不上"


def test_readiness_reports_missing_llm_key(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "")
    monkeypatch.setattr(health, "_check_db", lambda: (True, "stub"))
    monkeypatch.setattr(health, "_check_vector_store", lambda: (True, "stub"))

    ok, body = health.readiness()
    assert not ok
    assert "AGNES_API_KEY" in body["checks"]["llm"]["detail"]


def test_db_check_skipped_without_db_url(monkeypatch):
    monkeypatch.setattr(config, "DB_URL", "")
    assert health._check_db() == (True, "未配置 DB_URL（跳过）")


def test_vector_check_ok_for_memory_backend(monkeypatch):
    from src.rag import retrieval as R

    monkeypatch.setattr(R, "VECTOR_STORE", "memory")
    ok, detail = health._check_vector_store()
    assert ok and "memory" in detail


class _FakeStore:
    """只为承载 backend / dsn 属性的最小替身（对应 `app/runs.RunStore`）。"""

    def __init__(self, backend="memory", dsn="", enabled=True):
        self.backend = backend
        self.dsn = dsn
        self.enabled = enabled


def test_run_state_check_flags_memory_store(monkeypatch):
    """配了 DB_URL 却退化成内存现场仓库时必须**可见**。

    这是"以为接了 PG、其实静默降级，重启后待审现场全丢且日志查不到"的唯一防线。
    判定仍是 ok=True（降级≠不可用，不该被摘流量），但 detail 必须点名。
    """
    import app.main as M

    monkeypatch.setattr(config, "DB_URL", "postgresql://x")
    monkeypatch.setattr(M, "run_store", _FakeStore(backend="memory"))

    ok, detail = health._check_run_store()
    assert ok is True
    assert "内存" in detail and "重启丢现场" in detail


def test_run_state_check_reports_persistent_backend(monkeypatch):
    import app.main as M

    monkeypatch.setattr(M, "run_store", _FakeStore(backend="postgres", dsn="postgresql://x"))
    ok, detail = health._check_run_store()
    assert ok and "postgres" in detail and "已持久化" in detail


def test_run_state_check_reports_pending_schema(monkeypatch):
    """配了 DSN 但表还没建：**不能**报成"未持久化"。

    表是首次写入时才建的（`RunStore._ensure_schema`），所以在"从未有运行被打断过"
    的实例上这是正常状态。报成降级会让一个完全健康的实例看起来像丢了持久化。
    """
    import app.main as M

    monkeypatch.setattr(M, "run_store", _FakeStore(backend="memory", dsn="postgresql://x"))
    ok, detail = health._check_run_store()
    assert ok and "表尚未建立" in detail


def test_run_state_check_flags_disabled(monkeypatch):
    import app.main as M

    monkeypatch.setattr(M, "run_store", _FakeStore(enabled=False))
    ok, detail = health._check_run_store()
    assert ok and "disabled" in detail


def test_run_state_check_fails_open(monkeypatch):
    """读不到仓库时不能把探针炸掉 —— 健康检查本身绝不能成为不健康的原因。"""
    import app.main as M

    class _BoomStore:
        @property
        def backend(self):
            raise RuntimeError("no store")

    monkeypatch.setattr(M, "run_store", _BoomStore())
    ok, detail = health._check_run_store()
    assert ok is True and "未知" in detail
