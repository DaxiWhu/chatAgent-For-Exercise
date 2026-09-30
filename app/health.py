"""健康检查：把 liveness 与 readiness 分开。

为什么必须分开
------------
- **liveness（`/health/live`）**：进程还活着吗？探针失败通常意味着要**重启容器**。
  所以它必须**只反映本进程状态、绝不查外部依赖** —— 否则数据库抖一下，
  K8s 会把所有 Pod 全部重启，把一次小故障放大成雪崩。
- **readiness（`/health/ready`）**：现在能对外服务吗？探针失败只会**摘掉流量**、
  不重启。所以它应该真去探关键依赖（数据库、向量扩展、检索器是否就绪）。

旧的 `/health` 保留为 liveness 的别名（返回 `{"status":"ok"}`），
不破坏既有客户端与测试。

探测必须是**快速且无副作用**的：不触发语料解析、不建表、不调 LLM。
超时即判不健康（用 `HEALTH_READY_TIMEOUT` 控制），避免探针把线程池占满。
"""

import logging
import time

from . import config
from src import prompts


def liveness() -> dict:
    """存活探针：只回显进程标识，**不碰任何外部依赖**。

    返回 dict（含版本/发布标识，灰度期用来核对流量落到了哪个实例）。
    """
    return {
        "status": "ok",
        "version": config.APP_VERSION,
        "release": config.APP_RELEASE,
    }


def _timed(fn, *args, **kwargs):
    """跑一个探测函数，返回 (ok, detail, elapsed_ms)；异常一律转成 ok=False。"""
    t0 = time.perf_counter()
    try:
        ok, detail = fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001  （探测失败是正常返回值，不抛）
        ok, detail = False, f"{type(e).__name__}: {e}"
    return ok, detail, round((time.perf_counter() - t0) * 1000, 2)


def _check_llm() -> tuple[bool, str]:
    """检查 LLM 关键配置是否就位（只查配置，不发请求）。"""
    if not config.API_KEY:
        return False, "AGNES_API_KEY 未配置"
    return True, f"model={config.MODEL_NAME}"


def _check_db() -> tuple[bool, str]:
    """探数据库连通性（配了 DB_URL 才探；没配视为"不适用"= 通过）。"""
    if not config.DB_URL:
        return True, "未配置 DB_URL（跳过）"
    import psycopg

    with psycopg.connect(config.DB_URL, connect_timeout=config.HEALTH_READY_TIMEOUT) as c:
        with c.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    return True, "ok"


def _index_identity_note() -> str:
    """把"启动时校验过的索引身份"拼成一句可读明细（没校验过则返回空串）。

    只读**进程内缓存**（`index_identity.last_status()`），**零 IO** —— 就绪探针
    每几秒被调一次，它绝不能去打库（否则健康检查本身变成对一个外部服务的负载）。

    没校验过时返回空串而不是"未知"：空串的意思是"这次没话说"，
    不该被读成一个"身份未知"的结论。
    """
    from src.rag import index_identity as ii

    got = ii.last_status()
    status = got.get("status")
    if not status:
        return ""
    if status == "mismatch":
        rec = got.get("recorded") or {}
        return (
            f"；⚠️ 索引身份不一致：库里向量是 EMBEDDING_MODEL={rec.get('model')!r}"
            f"（{rec.get('dim')} 维）灌的，当前配置是 {got.get('model')!r}"
            f"（{got.get('dim')} 维）—— 检索质量会明显下降而各项指标正常，"
            "处置见 deploy/RUNBOOK.md"
        )
    return f"；索引身份={status}"


def _check_vector_store() -> tuple[bool, str]:
    """探向量后端（pg 查 vector 扩展 / qdrant 查配置），并回显索引身份校验结论。

    只读探测：**绝不建表、绝不连 Qdrant** —— 连上就是 IO，而就绪探针每几秒一次，
    那会把它变成一个稳定的外部负载。所以 qdrant 分支只查"地址配没配"
    （也顺手补上了此前 qdrant 分支完全没有任何检查的缺口）。

    ⚠️ **索引身份不一致不判失败**：那是**数据问题**（库里灌的是旧模型的向量），
    不是这个实例的问题 —— 摘流量、重启都解决不了它，只会额外制造一次停机。
    但它必须**可见**（这就是回显的目的），否则这条链路等于没有告警。
    """
    from src.rag import vector_store as vs
    from src.rag import retrieval as R

    ident = _index_identity_note()
    backend = (R.VECTOR_STORE or "memory").strip().lower()
    if backend == "qdrant":
        if not R.QDRANT_URL:
            return False, f"RAG_VECTOR_STORE=qdrant 但 QDRANT_URL 未配置{ident}"
        return True, f"backend=qdrant（集合={R.QDRANT_COLLECTION}）{ident}"
    if backend != "pg":
        return True, f"backend={R.VECTOR_STORE or 'memory'}（无外部依赖）{ident}"
    if not R.DB_URL:
        return False, f"RAG_VECTOR_STORE=pg 但 DB_URL 未配置{ident}"
    ok = vs.vector_available(R.DB_URL, connect_timeout=config.HEALTH_READY_TIMEOUT)
    return (ok, ("vector 扩展可用" if ok else "vector 扩展不可用/连不上") + ident)


def _check_checkpointer() -> tuple[bool, str]:
    """回显**当前生效的检查点后端**，用于核对"持久化到底有没有接上"。

    为什么不判失败：内存检查点只是"重启丢状态"，服务本身仍可用；把它判成
    not-ready 会让实例被摘流量、反而更糟（Windows 本地开发就属于这种情况）。
    但它必须**可见** —— 否则最典型的翻车是"以为连了 PG，其实静默降级成内存，
    重启后会话全丢且日志里查不到"。

    返回恒为 (True, 明细)；明细里点名具体类型与是否需要关注。
    """
    try:
        import app.main as M          # 延迟导入：避免与 main→health 形成循环导入
        cp = getattr(M.graph, "checkpointer", None)
    except Exception as e:  # noqa: BLE001
        return True, f"未知（读取失败：{type(e).__name__}）"
    if cp is None:
        return True, "none（无检查点，状态不落库）"
    name = type(cp).__name__
    if name in ("InMemorySaver", "MemorySaver"):
        note = "（内存，重启丢状态！）" if config.DB_URL else "（未配 DB_URL，属预期）"
        return True, f"{name}{note}"
    return True, f"{name}（已持久化）"


def readiness() -> tuple[bool, dict]:
    """就绪探针：真探关键依赖，返回 (是否就绪, 明细)。

    明细里每项含 ok / detail / ms，便于"页面提示哪一项不健康"与排障。
    整体就绪 = 所有必查项都通过。
    """
    checks: dict[str, dict] = {}
    overall = True

    for name, fn in (
        ("llm", _check_llm),
        ("db", _check_db),
        ("vector_store", _check_vector_store),
        ("checkpointer", _check_checkpointer),
    ):
        ok, detail, ms = _timed(fn)
        checks[name] = {"ok": ok, "detail": detail, "ms": ms}
        if not ok:
            overall = False

    body = {
        "status": "ok" if overall else "degraded",
        "version": config.APP_VERSION,
        "release": config.APP_RELEASE,
        # 提示词版本与代码版本**并列**：模型行为由两者共同决定，
        # 只报代码版本时，改了提示词却回滚代码会看不出差异。
        "prompt_version": prompts.PROMPT_VERSION,
        "checks": checks,
    }
    if not overall:
        logging.warning("readiness 未通过：%s",
                        {k: v["detail"] for k, v in checks.items() if not v["ok"]})
    return overall, body
