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
    assert "LLM_API_KEY" in body["checks"]["llm"]["detail"]


def test_db_check_skipped_without_db_url(monkeypatch):
    monkeypatch.setattr(config, "DB_URL", "")
    assert health._check_db() == (True, "未配置 DB_URL（跳过）")


def test_vector_check_ok_for_memory_backend(monkeypatch):
    from src.rag import retrieval as R

    monkeypatch.setattr(R, "VECTOR_STORE", "memory")
    ok, detail = health._check_vector_store()
    assert ok and "memory" in detail


# ---------------------------------------------------------------------------
# qdrant：配了地址不等于能用（客户端库是**延迟导入**的）
# ---------------------------------------------------------------------------
# 背景（2026-10-30 实机）：`RAG_VECTOR_STORE=qdrant` + `QDRANT_URL` 已配，但环境里
# 没装 `qdrant_client` 时，就绪探针回 `ok:true, backend=qdrant`，而同一份启动日志
# 里躺着 `索引身份自检失败：No module named 'qdrant_client'` —— 这条链路**每次
# 检索都会失败**，探针却一直是绿的。
def test_vector_check_fails_when_qdrant_client_missing(monkeypatch):
    import sys

    from src.rag import retrieval as R

    monkeypatch.setattr(R, "VECTOR_STORE", "qdrant")
    monkeypatch.setattr(R, "QDRANT_URL", "http://q:6333")
    # `sys.modules[name] = None` 会让 `import name` 直接抛 ImportError，
    # 与"没装这个包"对被测代码完全等价，且不依赖本机是否真装了它。
    monkeypatch.setitem(sys.modules, "qdrant_client", None)

    ok, detail = health._check_vector_store()
    assert ok is False, "缺客户端库却报了通过 ⇒ 每次检索都会失败而探针是绿的"
    assert "客户端库不可用" in detail and "每次检索都会失败" in detail


def test_vector_check_ok_when_qdrant_client_present(monkeypatch):
    """反向对照：库在就必须报通过，否则上面那条会靠"永远失败"蒙过去。"""
    import sys
    import types

    from src.rag import retrieval as R

    monkeypatch.setattr(R, "VECTOR_STORE", "qdrant")
    monkeypatch.setattr(R, "QDRANT_URL", "http://q:6333")
    monkeypatch.setattr(R, "QDRANT_COLLECTION", "rag_chunks")
    monkeypatch.setitem(sys.modules, "qdrant_client", types.ModuleType("qdrant_client"))

    ok, detail = health._check_vector_store()
    assert ok and "qdrant" in detail


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


# ---------------------------------------------------------------------------
# run_state：「配了但连不上」不许报成「持久化已生效」
# ---------------------------------------------------------------------------
# 背景（2026-10-30 实机）：`db.ok=false`（connection timeout）与
# `run_state.ok=true, detail="postgres（表尚未建立：首次写入时建）"` **同时出现在
# 同一份 /health/ready 里**。后半句在断言一件并不成立的事 —— 库连不上，
# `_ensure_schema` 根本跑不了，现场一个字节也写不进去。
def test_run_state_check_flags_unreachable_db(monkeypatch):
    import app.main as M

    monkeypatch.setattr(M, "run_store", _FakeStore(backend="memory", dsn="postgresql://x"))
    ok, detail = health._check_run_store(False)   # db 探测失败

    assert ok is False, "库连不上时仍报 ok ⇒ 假绿灯"
    assert "连不上" in detail and "写不进" in detail
    assert "表尚未建立" not in detail, "这句话在库连不上时是假的，不能出现"


def test_run_state_check_trusts_successful_schema_when_db_is_ok(monkeypatch):
    """反向对照：库探测通过时，已落成的 postgres 后端仍报持久化生效。"""
    import app.main as M

    monkeypatch.setattr(M, "run_store", _FakeStore(backend="postgres", dsn="postgresql://x"))
    ok, detail = health._check_run_store(True)
    assert ok and "已持久化" in detail


def test_readiness_passes_db_result_into_run_state(monkeypatch):
    """整体联动：db 挂了，run_state 那项必须**跟着说实话**。

    这条守的是"两处探针各探各的"那种实现 —— 单看 `_check_run_store` 是对的，
    但 `readiness()` 没把 db 的结论传下去，就仍然是假绿灯。
    """
    import app.main as M

    monkeypatch.setattr(config, "API_KEY", "sk-test")
    monkeypatch.setattr(health, "_check_db", lambda: (False, "connection timeout"))
    monkeypatch.setattr(health, "_check_vector_store", lambda: (True, "stub"))
    monkeypatch.setattr(health, "_check_pg_pool", lambda: (True, "未建立"))
    monkeypatch.setattr(M, "run_store", _FakeStore(backend="memory", dsn="postgresql://x"))

    ok, body = health.readiness()
    assert not ok and body["status"] == "degraded"
    assert body["checks"]["run_state"]["ok"] is False
    assert "连不上" in body["checks"]["run_state"]["detail"]


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
