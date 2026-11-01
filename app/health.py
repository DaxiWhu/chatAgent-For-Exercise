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

from src import prompts

from . import config


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
        return False, "LLM_API_KEY 未配置"
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
    """探向量后端（pg 查 vector 扩展 / qdrant 查依赖与配置），并回显索引身份校验结论。

    只读探测：**绝不建表、绝不连 Qdrant** —— 连上就是 IO，而就绪探针每几秒一次，
    那会把它变成一个稳定的外部负载。所以 qdrant 分支查的是"地址配没配"与
    "客户端库装没装"（导入检查，零 IO）。

    ⚠️ **为什么要查 qdrant 客户端是否可导入**（2026-10-30 实机修）：此前该分支
    只看 `QDRANT_URL`，于是"配了 qdrant 但没装 `qdrant_client`"的实例报
    `ok:true, backend=qdrant`，而同一份启动日志里躺着
    `索引身份自检失败：No module named 'qdrant_client'`。这条链路**每次检索都会
    失败**，就绪探针却一直是绿的 —— 与 `run_state` 那处属于同一类"配了但用不了"。
    `qdrant_client` 在 `src/rag/qdrant_store.py` 里是**方法内延迟导入**的，所以
    装没装只有真去 import 才知道，读配置读不出来。

    ⚠️ **索引身份不一致不判失败**：那是**数据问题**（库里灌的是旧模型的向量），
    不是这个实例的问题 —— 摘流量、重启都解决不了它，只会额外制造一次停机。
    但它必须**可见**（这就是回显的目的），否则这条链路等于没有告警。
    """
    from src.rag import retrieval as R
    from src.rag import vector_store as vs

    ident = _index_identity_note()
    backend = (R.VECTOR_STORE or "memory").strip().lower()
    if backend == "qdrant":
        if not R.QDRANT_URL:
            return False, f"RAG_VECTOR_STORE=qdrant 但 QDRANT_URL 未配置{ident}"
        try:
            import qdrant_client  # noqa: F401  只验可导入，不建客户端、不连
        except Exception as e:  # noqa: BLE001  缺库/装坏了都算不可用
            return False, (f"RAG_VECTOR_STORE=qdrant 但客户端库不可用"
                           f"（{type(e).__name__}: {e}）⇒ 每次检索都会失败{ident}")
        return True, f"backend=qdrant（集合={R.QDRANT_COLLECTION}）{ident}"
    if backend != "pg":
        return True, f"backend={R.VECTOR_STORE or 'memory'}（无外部依赖）{ident}"
    if not R.DB_URL:
        return False, f"RAG_VECTOR_STORE=pg 但 DB_URL 未配置{ident}"
    ok = vs.vector_available(R.DB_URL, connect_timeout=config.HEALTH_READY_TIMEOUT)
    return (ok, ("vector 扩展可用" if ok else "vector 扩展不可用/连不上") + ident)


def _check_run_store(db_reachable: bool | None = None) -> tuple[bool, str]:
    """回显**被打断的运行现场（官方 ``RunState``）存在哪**，用于核对"持久化接上没有"。

    为什么这项取代了原来的 `checkpointer`：编排原语只剩 `Agent` + `Runner`，官方
    没有任何检查点概念；"跑到一半停下来等人批"的现场由官方 ``RunState`` 序列化后
    存在 `agent_run_state` 表里（见 `app/runs.py`）。所以要核对的东西没变
    （状态到底落库了没有），换了承载物。

    db_reachable: `readiness()` 里 db 探测的结论。**必须传进来**，原因见下面那条
    ⚠️。为 None 表示调用方没提供（单测直接调本函数时的默认），此时不做可达性推断。

    ⚠️ **为什么需要 db 的结论**（2026-10-30 实机修）：`RunStore.backend` 只在
    `_ensure_schema()` **成功执行之后**才变成 `"postgres"`，在这之前它是 `"memory"`
    而 `dsn` 有值 —— 原来这条分支无条件回 `"postgres（表尚未建立：首次写入时建）"`，
    于是"配了 DB_URL 但库连不上"的实例得到 `ok:true` + 一句断言持久化已生效的话。
    实机证据：`/health/ready` 里 `db.ok=false`（connection timeout）与
    `run_state.ok=true`（"表尚未建立"）同时出现。**这是假绿灯**：现场根本写不进去。

    为什么不判失败（除了上面那种）：内存后端只是"重启丢现场"，服务本身仍可用；
    判成 not-ready 会让实例被摘流量、反而更糟（Windows 本地开发就属于这种情况）。
    但"配了 dsn 且库探测失败"是另一回事 —— 它宣称的持久化能力并不存在，所以那一档
    明确 `ok=False`。
    """
    try:
        import app.main as M  # 延迟导入：避免与 main→health 形成循环导入
        store = getattr(M, "run_store", None)
        if store is None:
            return True, "none（无现场仓库）"
        # ⚠️ 下面三行**直接读属性**（不用 getattr 带默认值）：读取本身也要被这个
        # except 兜住。否则一个"属性访问会抛"的对象会被 getattr 的默认值吞成
        # "disabled"，探针就报了一个**假结论**（真因是读失败，不是被关掉）。
        enabled = store.enabled
        backend = store.backend
        dsn = store.dsn
    except Exception as e:  # noqa: BLE001
        return True, f"未知（读取失败：{type(e).__name__}）"
    if not enabled:
        return True, "disabled（运行现场不落库，停机一律不可恢复）"
    if dsn and db_reachable is False:
        # 配了 DSN 但数据库这一轮探测失败：无论表建没建，现场都写不进去。
        # 绝不能在这里回"已持久化"或"表尚未建立" —— 那两句都在断言一件假事。
        return False, (
            "⚠️ 配了 DB_URL 但数据库当前连不上 ⇒ 运行现场实际写不进，"
            "被打断的运行重启即丢（先看 db 项）"
        )
    if backend == "postgres":
        return True, "postgres（已持久化，被打断的运行可跨重启恢复）"
    if dsn:
        # 配了 DSN 但表还没建出来：**第一次写入时才建表**，所以在没有运行被打断过
        # 的实例上这是正常状态，不能据此判失败。
        return True, "postgres（表尚未建立：首次写入时建）"
    note = "" if config.DB_URL else "（未配 DB_URL，属预期）"
    return True, f"memory（进程内存，重启丢现场！）{note}"


def _check_pg_pool() -> tuple[bool, str]:
    """回显同步 PG 连接池的占用情况（**只读进程内计数，零 IO**）。

    为什么必须可见：连接池打满的表现是"延迟上升、吞吐不再增长"，**没有任何错误**。
    没有这一项，排障时会一路往模型、向量库上找，而真正的原因是本进程自己的 8 条
    连接全被占用、后续请求在排队。

    为什么**不判失败**：池是"配了 DB_URL 才建"的，没建只是"尚未用到"；
    而占用高（available=0 且有 waiting）说明在**排队**而不是故障 —— 摘掉流量
    只会让剩下的副本排得更久。所以它是可见性，不是健康判据。
    """
    from . import pg

    try:
        st = pg.stats()
    except Exception as e:  # noqa: BLE001  （探针不该因它失败）
        return True, f"未知（读取失败：{type(e).__name__}）"
    if not st:
        return True, ("未建立（尚未用到 PG）" if config.DB_URL
                      else "未配置 DB_URL（跳过）")
    parts = []
    for dsn, v in st.items():
        line = f"{dsn}: available={v.get('available')}/{v.get('size')}"
        if v.get("waiting"):
            line += f"，waiting={v['waiting']}（连接池已打满，请求在排队）"
        parts.append(line)
    return True, "；".join(parts)


def readiness() -> tuple[bool, dict]:
    """就绪探针：真探关键依赖，返回 (是否就绪, 明细)。

    明细里每项含 ok / detail / ms，便于"页面提示哪一项不健康"与排障。
    整体就绪 = 所有必查项都通过。
    """
    checks: dict[str, dict] = {}
    overall = True

    #: db 的结论要喂给 run_state（见 `_check_run_store` 的 ⚠️ 段）：
    #: `RunStore.backend` 在首次写入前恒为 `"memory"`，光读它区分不出
    #: "表还没建"与"库根本连不上" —— 这两种情况属性长得一模一样，只有 db 探测
    #: 能分辨。所以这里按**顺序**求值并把它传下去，而不是各探各的。
    db_ok: bool | None = None

    for name, fn in (
        ("llm", _check_llm),
        ("db", _check_db),
        ("vector_store", _check_vector_store),
        ("run_state", _check_run_store),
        ("pg_pool", _check_pg_pool),
    ):
        ok, detail, ms = _timed(fn, db_ok) if name == "run_state" else _timed(fn)
        checks[name] = {"ok": ok, "detail": detail, "ms": ms}
        if not ok:
            overall = False
        if name == "db":
            db_ok = ok

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
