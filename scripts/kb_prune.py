#!/usr/bin/env python
"""知识库级联清理：删掉"源文件已不存在 / 内容已变"的向量块。

背景
----
灌库是幂等的（主键 = `chunk_fingerprint`，重复灌不产生副本），但**没有反向路径**：
源文件删了或改了之后，库里那批块会永久留下。用户问到已经下线的内容，检索照样命中；
对外声明"已删除"的数据仍可被搜出来。

本脚本消费 `load_chunks` 写在每个块上的 **source_path**（相对语料根的路径）与
**content_hash**（文件内容指纹），把它们和"当前语料目录里真实存在的文件"比对，
得出两类孤儿：

  - **missing**：源文件已不存在（删了/移走/改名）—— 默认清理；
  - **stale**：源文件还在但内容变了。改写文件会让 chunk_id 全变 ⇒ 重新灌库只新增、
    旧块一条不删，于是新旧两版块并存、检索命中哪一版看运气。**默认只报告不清理**，
    加 `--include-stale` 才删（因为"先删旧的、还没灌新的"中间态会让知识库短暂缺内容）。

为什么默认 dry-run
----------------
删除不可逆。默认只打印"将要删什么"，确认无误后再加 `--apply`。与
`scripts/retention_sweep.py` 同一取向。

三个拒绝执行的闸门（都返回 2 —— "无法判定"，**不是"通过"**）
--------------------------------------------------------
1. **语料目录读不到任何受支持的文件**。这是最重要的一条：容器里最常见的事故是
   "语料没挂进去"（见 MEMORY.md 的 DOCS_DIR 条目），此时"磁盘上的文件集合"是空的，
   若照常比对，**全库都会被判成孤儿**。文件数为 0 只可能是挂载/路径配置坏了，
   不是"你把语料删光了"，所以直接拒绝执行。
2. **内存后端**（`RAG_VECTOR_STORE=memory`，默认值）。没有持久数据，没什么可清理。
3. **配额/连接配置缺失**（没配 `DB_URL` / `QDRANT_URL`）。

用法
----
    python scripts/kb_prune.py                       # 预演（不删），逐个租户
    python scripts/kb_prune.py --json                # 机器可读
    python scripts/kb_prune.py --include-stale       # 连"内容已变"的也列出来
    python scripts/kb_prune.py --apply               # 真删 missing
    python scripts/kb_prune.py --apply --include-stale
    python scripts/kb_prune.py --tenant public --apply  # 只清这一个分区
    python scripts/kb_prune.py --all-tenants --apply    # 与不给 --tenant 等价

⚠️ `--tenant` 是**字面值**（不是"身份归一化"）：写 `--tenant ""` 清理的是
"没有租户名"那批老数据所在的分区。不给 `--tenant` 才是"库里所有分区"。

典型顺序（先灌新、再删旧，避免知识库出现空窗）：
    python -m src.rag.retrieval --docs data   # 或服务启动自带的增量灌库
    python scripts/kb_prune.py --include-stale --apply

退出码：0 已执行（含"没有孤儿"）/ 1 执行失败 / 2 无法判定（拒绝执行）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ⚠️ 刻意**不做 `os.chdir(ROOT)`**（`retention_sweep.py` 里有那行）：本模块会被测试
# 用 importlib 加载，chdir 会改掉**整个测试进程**的工作目录，连带影响
# `tests/test_project_root.py` 这类按相对路径找文件的用例。改成把相对路径
# 显式锚到 ROOT，效果相同而无副作用。

from app import config  # noqa: E402
from src.rag.kb_inventory import corpus_index, plan_orphans  # noqa: E402

#: dry-run 时最多列出多少条待删源（避免刷屏；计数仍然是全量）。
SAMPLE = 20


def _anchor(path):
    """把相对路径锚到项目根（绝对路径原样返回）。"""
    path = str(path or "")
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def _make_store(tenant=None):
    """按 `RAG_VECTOR_STORE` 造一个向量库句柄。

    tenant: 租户 id。**None = 不过滤**（`--all-tenants` 的探测句柄用这个）；
        任何字符串（含空串）都是一个真实租户，读写都锁在它上面。
        ⚠️ 2026-10-06 之前空串也等于"不过滤" —— 与向量库同一处口径反转，
        所以这里**不能再拿空串当"全库"的暗号**（见 `PgVectorStore.__init__`）。

    返回 `(store, error)`：成功时 error 为空串；失败时 store 为 None 且 error
    里写明**为什么**（这些原因都要原样打印给运维看，它们直接指向该改哪个变量）。
    """
    from src.rag import retrieval as R
    from src.rag.qdrant_store import QdrantStore
    from src.rag.vector_store import PgVectorStore

    vs = (R.VECTOR_STORE or "memory").strip().lower()
    if vs == "pg":
        if not R.DB_URL:
            return None, "RAG_VECTOR_STORE=pg 但未配置 DB_URL"
        return PgVectorStore(R.DB_URL, R.PGVECTOR_TABLE, R.PGVECTOR_INDEX,
                             tenant=tenant), ""
    if vs == "qdrant":
        if not R.QDRANT_URL:
            return None, "RAG_VECTOR_STORE=qdrant 但未配置 QDRANT_URL"
        return QdrantStore(R.QDRANT_URL, R.QDRANT_API_KEY, R.QDRANT_COLLECTION,
                           tenant=tenant, timeout=R.QDRANT_TIMEOUT), ""
    return None, (f"RAG_VECTOR_STORE={vs} 是内存后端（没有持久数据），"
                  "本脚本只在 pg / qdrant 后端下有意义")


def _refuse(msg):
    print(f"拒绝执行：{msg}", file=sys.stderr)
    return 2


def main(argv=None):
    """执行一次清理，返回退出码（0 已执行 / 2 无法判定）。

    argv: 命令行参数（None = 取 sys.argv）。**显式收参数**而不是直接读 sys.argv，
    这样测试能在不改进程 argv 的前提下驱动它 —— 改 argv 的用例之间会互相污染。

    任何未捕获异常都往外抛，由 `run()` 统一转成退出码 1。
    """
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--docs", default=None,
                    help="语料根目录（默认取 DOCS_DIR）")
    ap.add_argument("--apply", action="store_true",
                    help="真的执行删除（不加则只预演）")
    ap.add_argument("--include-stale", action="store_true",
                    help="连「源文件还在但内容已变」的旧块一起清理（默认只报告）")
    ap.add_argument("--tenant", default=None,
                    help="只清理这一个分区，**按字面值**（如 `--tenant public`；"
                         "老数据在空串分区，写 `--tenant \"\"`）。"
                         "不给则清理库里出现过的每个租户")
    ap.add_argument("--all-tenants", action="store_true",
                    help="对库里出现过的每个租户分别执行（与不给 --tenant 等价）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（便于接监控）")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)

    docs = args.docs if args.docs is not None else config.DOCS_DIR
    docs = _anchor(docs)
    index = corpus_index(docs)

    # ---- 闸门 1：语料目录读不到文件 = 挂载/路径坏了，不能照常比对 ----
    # 这里刻意区分"目录里真的一个受支持文件都没有"与"你把语料删光了"：
    # 后者不可能靠一个脚本分辨，而误判的代价是全库清空。宁可让人去手工确认。
    if index["count"] == 0:
        return _refuse(
            f"语料目录 {docs!r} 下没有扫到任何受支持的文档。"
            "若照常比对，库里全部数据都会被判成孤儿 —— 这几乎总是"
            "「语料没挂进容器 / DOCS_DIR 指错」而不是「语料真的空了」。"
            "请先确认目录内容（`ls`），确认无误后再手工处理。"
        )

    if args.tenant is not None and args.all_tenants:
        return _refuse("--tenant 与 --all-tenants 不能同时给："
                       "前者只清一个分区，后者清每个分区。"
                       "（不给 --tenant 就等于 --all-tenants）")

    if args.tenant is not None:
        # 显式指定 ⇒ **按字面值**用，不过 `resolve_tenant`。
        #
        # ⚠️ 这里刻意不归一化（2026-10-06 修）。`resolve_tenant` 的职责是
        # "把**身份**归一成一个过滤值"（无租户 ⇒ 公共租户 ⇒ 哨兵），它服务的是
        # 请求路径；而本脚本是**运维工具**，操作对象是"库里实际存在的分区名"。
        # 两者混用出过一个很难发现的问题：不给 `--tenant` 时经 `resolve_tenant(None)`
        # 会落到 `RAG_TENANT`（或它留空时的哨兵值），于是
        #   · `RAG_TENANT=public`：只清公共分区，**别的租户的孤儿一条都不碰**；
        #   · `RAG_TENANT` 留空：清理对象变成哨兵分区（库里根本不存在它）⇒
        #     脚本打印"源已消失 0 份"、退出码 0 —— **在什么都没做的情况下报成功**。
        # 一个用来决定删不删东西的工具，最不该有的就是这种假绿。
        tenants = [args.tenant.strip()]
    else:
        # 不给 --tenant（或给了 --all-tenants）⇒ 库里出现过的**每个**租户分别清。
        # 这与口径反转之前的"不过滤 = 整个库"在**数据范围**上等效，
        # 但每次删除都被限定在单个分区内 —— 少一次跨租户误删的机会。
        #
        # ⚠️ 探测句柄必须显式 `tenant=None`（= 不过滤）：新口径下传空串只会去查
        # "空租户那一份"，`list_tenants()` 于是恒为空 ⇒ 静默退化成"什么都没做"。
        probe, err = _make_store(None)
        if probe is None:
            return _refuse(err)
        tenants = probe.list_tenants()
        if not tenants:
            tenants = [None]   # 表/集合存在但还没数据 → 走一遍，得到"无孤儿"

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "docs": docs,
        "corpus_files": index["count"],
        "include_stale": bool(args.include_stale),
        "tenants": {},
    }
    total_missing = total_stale = total_unknown = total_deleted = 0

    for t in tenants:
        store, err = _make_store(t)
        if store is None:
            return _refuse(err)
        plan = plan_orphans(store.list_rows(), index)
        missing_keys = [k for _, k in plan["missing"]]
        stale_keys = [k for _, k in plan["stale"]] if args.include_stale else []
        keys = sorted(set(missing_keys) | set(stale_keys))
        deleted = store.delete_keys(keys) if args.apply else 0

        total_missing += len(missing_keys)
        total_stale += len(plan["stale"])
        total_unknown += len(plan["unknown"])
        total_deleted += deleted
        report["tenants"]["<不隔离>" if t is None else t] = {
            "ok": len(plan["ok"]),
            "missing": len(missing_keys),
            "stale": len(plan["stale"]),
            "unknown": len(plan["unknown"]),
            "deleted_rows": deleted,
            "missing_sample": missing_keys[:SAMPLE],
            "stale_sample": [k for _, k in plan["stale"]][:SAMPLE],
        }

    report["totals"] = {
        "missing": total_missing,
        "stale": total_stale,
        "unknown": total_unknown,
        "deleted_rows": total_deleted,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    tag = "[apply]" if args.apply else "[dry-run]"
    print(f"{tag} 语料目录 {docs}（{index['count']} 个受支持文件）")
    for name, s in report["tenants"].items():
        print(f"  租户 {name}：存活 {s['ok']} 份源、"
              f"源已消失 {s['missing']} 份、内容已变 {s['stale']} 份、"
              f"无法判定 {s['unknown']} 份")
        if s["missing_sample"]:
            print(f"    源已消失示例：{', '.join(s['missing_sample'])}")
        if s["stale_sample"]:
            print(f"    内容已变示例：{', '.join(s['stale_sample'])}")
        if args.apply:
            print(f"    已删除块数：{s['deleted_rows']}")
    if total_unknown:
        print(f"  提示：{total_unknown} 份源缺 content_hash（本次改造前灌的数据），"
              "判不了新旧 ⇒ **不会被删**。重新灌一次库即可补上指纹。")
    if total_stale and not args.include_stale:
        print("  提示：有「内容已变」的旧块未清理。确认新版本已灌进去之后，"
              "加 --include-stale --apply 再跑一次。")
    if not args.apply:
        print("\n加 --apply 执行删除。")
    return 0


def run(argv=None):
    """`__main__` 的壳：把 main 的返回值与异常统一成退出码。

    单拎出来是为了**退出码 1 可被测试**：直接给 `__main__` 块写 try/except，
    测不到它 —— 于是"后端连不上时到底退了几"永远没人验证过。
    """
    try:
        return main(argv)
    except Exception as e:  # noqa: BLE001  （运维脚本：报错必须可见且带退出码）
        print(f"清理失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
