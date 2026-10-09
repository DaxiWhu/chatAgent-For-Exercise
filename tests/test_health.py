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
    assert set(body["checks"]) == {"llm", "db", "vector_store", "checkpointer", "pg_pool"}
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


class _FakeGraph:
    """只为承载 checkpointer 属性的最小替身。"""

    def __init__(self, checkpointer):
        self.checkpointer = checkpointer


def test_checkpointer_check_flags_memory_saver(monkeypatch):
    """配了 DB_URL 却退化成内存 saver 时必须**可见**。

    这是"以为接了 PG、其实静默降级，重启后会话全丢且日志查不到"的唯一防线。
    判定仍是 ok=True（降级≠不可用，不该被摘流量），但 detail 必须点名。
    """
    from langgraph.checkpoint.memory import InMemorySaver

    import app.main as M

    monkeypatch.setattr(config, "DB_URL", "postgresql://x")
    monkeypatch.setattr(M, "graph", _FakeGraph(InMemorySaver()))

    ok, detail = health._check_checkpointer()
    assert ok is True
    assert "内存" in detail and "重启丢状态" in detail


def test_checkpointer_check_reports_persistent_backend(monkeypatch):
    import app.main as M

    class _AsyncSaverStub:
        pass

    monkeypatch.setattr(M, "graph", _FakeGraph(_AsyncSaverStub()))
    ok, detail = health._check_checkpointer()
    assert ok and "_AsyncSaverStub" in detail and "已持久化" in detail


def test_checkpointer_check_fails_open(monkeypatch):
    """读不到图时不能把探针炸掉 —— 健康检查本身绝不能成为不健康的原因。"""
    import app.main as M

    class _BoomGraph:
        @property
        def checkpointer(self):
            raise RuntimeError("no graph")

    monkeypatch.setattr(M, "graph", _BoomGraph())
    ok, detail = health._check_checkpointer()
    assert ok is True and "未知" in detail

