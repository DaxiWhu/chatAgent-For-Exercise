"""索引身份：记录"库里的向量是用哪个 embedding 模型灌的"，并在启动时校验。

要解决什么
----------
`EMBEDDING_MODEL` 是环境变量，而 Qdrant 的 collection 名 / pg 的表名是**固定的**。
于是换 embedding 模型时有两种结果：

  - **维度变了** ⇒ 现有代码会**显式报错**（`ensure_schema` 里的维度校验），算好的情况；
  - **维度没变、语义空间变了**（例如 BGE-M3 → 另一个同为 1024 维的模型）⇒
    **静默劣化**：旧向量还在库里，新查询用新模型编码，两者的向量空间对不上，
    检索质量当场崩掉 —— 而所有指标完全正常、没有任何告警。这是本项目里最隐蔽的一条。

本模块做的是**记录 + 校验 + 大声报错**，不自动改数据（原因见下）。

为什么不自动把 collection/表名带上指纹
------------------------------------
那会让"换模型"直接变成"检索恒空"（新名字指向一个空集合）。看似更安全，
实际是把"答案质量差"换成"完全没答案"，而且**旧数据依然存在却被静默弃用** ——
对一个已经上线的部署来说，这个代价比报警更贵。所以：

  - 名字**保持稳定**，指纹只作**记录**；
  - 不匹配时：ERROR 日志（可 grep）+ 指标 + 就绪探针明细，**服务照常跑**。

⚠️ **不匹配时不要让就绪探针失败**：检索质量差是**数据问题**，不是这个实例的问题 ——
把实例摘出负载均衡并重启，既解决不了它，还会额外制造一次停机
（与 `app/health.py::_check_run_store` 同一取舍：内存运行现场也只报不明说失败）。

⚠️ **它的能力边界**：本次改造之前灌的库**没有记录**，第一次跑只会"记录当前身份"
（状态 `recorded`），因此**看不出历史上已经发生的模型切换**。想覆盖这种存量情况，
只能靠人工确认当前 `EMBEDDING_MODEL` 与语料入库时的模型是否一致。

维度从哪里来（这条决定了校验能不能真的生效）
------------------------------------------
指纹算的是 `(model, dim)`，其中 **dim 是"库里实际存着的向量维度"**（由
`store.index_dim()` 读出：pg 读 `pg_attribute.atttypmod`，qdrant 读 collection 的
`size`），**不是**从当前配置推出来的。

为什么非要从库里读：
  1. 配置侧 pg 分支**根本没有维度这个配置项**（维度是首次 upsert 时从 embedding
     结果推的），从配置推不出来；
  2. 更关键 —— `_sync()` 在"库已是最新"时**提前 return**，而 pg 的 `ensure_schema`
     只在有新块要写时才调。换完模型重启，库里所有块都"已存在" ⇒ 走零 embedding
     调用的早退路径。**校验若跟着 embedding 调用走，最该报的这一次恰好不会报。**
     从库里读维度就没有这个依赖，可以在早退之前先判一次。

指标归属
--------
本模块**不写指标**（`src/rag/` 是纯库层，可被 CLI 直接运行，不该反过来依赖
`app/`）。它只把结论缓存进 `last_status()`，由 app 层（启动自检 / 就绪探针）读取
后再写 `rag_index_identity_ok` —— 这样"指标什么时候被写"仍然只有一处。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading

#: 指纹取 sha256 的前若干位十六进制（够短够读，碰撞概率可忽略）。
FINGERPRINT_CHARS = 8

#: 在库里存身份用的键名（pg 是 `<表>_meta` 表的一行；qdrant 是 payload 里的标记字段）。
REMEMBER_KEY = "embedding_fingerprint"

#: Qdrant 里"元数据点"的 payload 标记。这类点**没有向量**，因此不会被检索命中；
#: 但维护类接口（`list_rows` / `count` / `existing_ids`）会扫到它，所以必须能识别并跳过。
META_FLAG = "__chatagent_index_meta__"

#: 最近一次校验的结论（进程内缓存）。就绪探针读它 —— 探针必须是**零 IO** 的，
#: 否则每次探活都要打一次库（健康检查本身成为负载）。
_STATE: dict = {}
_LOCK = threading.Lock()


def embedding_fingerprint(model, dim) -> str:
    """算"向量空间身份"指纹：只由 **模型名 + 维度** 决定。

    model: embedding 模型名（如 `BAAI/bge-m3`）。空值按空串参与计算。
    dim: 向量维度（int）。非数字按 0 处理。

    为什么**不含** `EMBEDDING_QUERY_PREFIX`：它只作用于查询侧（文档向量不变），
    把它并进指纹会让"调了下查询指令"被判成"必须重灌库" —— 那是错的建议。
    它单独记在 `describe()` 里供人工核对。

    为什么**含** dim：维度变了其实会被 `ensure_schema` 拦住，但那是"写入时"才炸
    （且要等有新块才炸）。并进指纹后，**同一个抽屉里**就能把两种不匹配一起报出来。
    """
    raw = "\x00".join([str(model or "").strip(), str(int(dim or 0))])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]


def describe(model, dim, query_prefix="") -> dict:
    """把身份展开成可写入、可人工核对的 dict。"""
    return {
        "fingerprint": embedding_fingerprint(model, dim),
        "model": str(model or "").strip(),
        "dim": int(dim or 0),
        # 只记"有没有配"，不记指令正文：它可能是一长段提示词，没必要进库
        "query_prefix_set": bool((query_prefix or "").strip()),
    }


def last_status() -> dict:
    """最近一次校验的结论（没有校验过时返回空 dict）。

    返回的是**副本**：调用方（就绪探针）不该改到内部状态。
    """
    with _LOCK:
        return dict(_STATE)


def _remember(state: dict) -> None:
    with _LOCK:
        _STATE.clear()
        _STATE.update(state)


def reset_status() -> None:
    """清掉缓存结论（测试与"重新建索引"后用）。"""
    with _LOCK:
        _STATE.clear()


#: `_read_recorded` 的哨兵：表示"**读取动作本身失败了**"，而不是"库里没记过"。
#:
#: 两者绝不能合并。合并的后果是很难发现的一种失效：一次读取故障被当成"从没记录过"，
#: 接着 `set_meta` 把**旧身份覆盖成当前身份** —— 从此这条链路永远显示"一致"，
#: 外观全绿，而库里的向量其实已经不是当前模型灌的了。
#: 正确做法是：不判定、也不覆盖，报 `error` 让人去看日志。
_UNREADABLE = object()


def _read_recorded(store):
    """读库里记的身份。

    返回值有三种，语义各不相同，别合并：
      - `dict`        —— 读到的记录（可能为空 dict，表示"记录内容为空"）；
      - `{}`          —— 库里**确实没记过**（键不存在 / 值为空）；
      - `_UNREADABLE` —— **读取失败**或记录损坏。
    """
    try:
        raw = store.get_meta(REMEMBER_KEY)
    except Exception as e:  # noqa: BLE001
        logging.warning("读取索引身份失败（本次不判定、也不覆盖已有记录）：%s（%s）",
                        e, type(e).__name__)
        return _UNREADABLE
    if not raw:
        return {}
    try:
        got = json.loads(raw)
    except (TypeError, ValueError):
        # 记录存在但不是合法 JSON：现场坏了，同样**不能**当成"没记过" ——
        # 那会用当前身份覆盖它，把一个"库里的东西坏了"的现场抹掉。
        logging.warning("索引身份记录不是合法 JSON（本次不判定、也不覆盖）：%r", raw)
        return _UNREADABLE
    return got if isinstance(got, dict) else _UNREADABLE


def verify(store, *, model, dim, query_prefix="", backend="") -> dict:
    """比对"库里的向量身份"与"当前配置的模型"，并按结论记日志 / 缓存。

    store: 向量库句柄（需有 `get_meta` / `set_meta`）。
    model: 当前 `EMBEDDING_MODEL`。
    dim: **库里现有向量的实际维度**（调用方从 `store.index_dim()` 读，别从配置推）。
        **0 表示库还是空的 / 表还没建** —— 此时不写记录也不报警，只标 `unknown`，
        避免"用一个猜出来的维度盖章"（那会把一次真实的不匹配永久记成"已核对"）。
    query_prefix: 仅用于记录，不参与指纹。
    backend: `pgvector` / `qdrant`，只用于日志与状态（方便定位是哪条后端）。

    返回状态 dict：`status ∈ {recorded, ok, mismatch, unknown, error}`，
    另有 `ok` 字段（True / False / None）供调用方决定是否写指标：
    `None` = 无从判断，**不要**上报（否则会把"还没建库"记成一次失败）。
    """
    now = describe(model, dim, query_prefix)
    now.update({"backend": backend})
    verdict = dict(now)

    if not now["dim"]:
        verdict["status"] = "unknown"
        verdict["ok"] = None
        verdict["detail"] = (
            "库里还没有向量（或表/集合尚未创建），本次不记录也不校验身份"
        )
        logging.info("索引身份未校验：%s（后端=%s）", verdict["detail"], backend)
        _remember(verdict)
        return verdict

    recorded = _read_recorded(store)

    if recorded is _UNREADABLE:
        # 读不到 / 记录坏了：**既不判定也不覆盖**（覆盖掉就永远不再报警了）。
        # 这不会影响灌库，但必须让人看见 —— 恢复之前这条告警是"瞎的"。
        verdict["status"] = "error"
        verdict["ok"] = None
        verdict["detail"] = (
            "读取索引身份失败或记录已损坏：本次既不判定也不覆盖已有记录（详见日志）。"
            "注意：在它恢复之前，索引身份这项检查是不可信的。"
        )
        _remember(verdict)
        return verdict

    if not recorded:
        # 首次（或本次改造之前灌的库）：记录当前身份，并**明确说明它证明不了历史**
        try:
            store.set_meta(REMEMBER_KEY, json.dumps(now, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            verdict["status"] = "error"
            verdict["ok"] = None
            verdict["detail"] = f"写入索引身份失败：{type(e).__name__}: {e}"
            logging.warning("索引身份写入失败（不影响灌库）：%s", verdict["detail"])
            _remember(verdict)
            return verdict
        verdict["status"] = "recorded"
        verdict["ok"] = True
        verdict["detail"] = (
            f"已记录当前索引身份（模型={now['model']} 维度={now['dim']} "
            f"指纹={now['fingerprint']}）。注意：**这只记录当下**，"
            "无法证明库里既有向量是同一模型灌的 —— 存量库请人工核对一次。"
        )
        logging.info("索引身份已记录：%s", verdict["detail"])
        _remember(verdict)
        return verdict

    if recorded.get("fingerprint") == now["fingerprint"]:
        verdict["status"] = "ok"
        verdict["ok"] = True
        verdict["detail"] = f"索引身份一致（模型={now['model']} 维度={now['dim']}）"
        logging.info("索引身份校验通过：%s（后端=%s）", verdict["detail"], backend)
        _remember(verdict)
        return verdict

    verdict["status"] = "mismatch"
    verdict["ok"] = False
    verdict["recorded"] = recorded
    verdict["detail"] = (
        f"⚠️ 索引身份不一致：库里的向量是 模型={recorded.get('model')!r} "
        f"维度={recorded.get('dim')} 指纹={recorded.get('fingerprint')} 灌的，"
        f"而当前配置是 模型={now['model']!r} 维度={now['dim']} "
        f"指纹={now['fingerprint']}。"
    )
    logging.error(
        "%s 旧向量与新查询落在**不同的向量空间**里，检索质量会明显下降而指标一切正常。"
        "处置：把 QDRANT_COLLECTION / PGVECTOR_TABLE 改成一个新名字并重新灌库"
        "（旧集合/表保留以便回滚比对）；若确认是误报（比如只是改了模型名的大小写写法），"
        "把 `%s` 记录改成当前指纹即可。后端=%s",
        verdict["detail"], REMEMBER_KEY, backend,
    )
    _remember(verdict)
    return verdict
