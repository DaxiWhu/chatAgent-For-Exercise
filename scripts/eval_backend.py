#!/usr/bin/env python
"""评测用的检索后端锁定 —— 让「评测数字」与开发机的 `.env` 无关。

为什么必须有它
--------------
`src/rag/retrieval.py` 在**导入期**就 `load_dotenv(dotenv_path=仓库根/.env)`，并据此
把 `EMBEDDING_BACKEND` / `RERANK_ENABLED` 定成**模块常量**。于是同一个评测脚本：

    开发机 .env 里有 EMBEDDING_API_KEY  → RAG_EMBEDDING=hybrid（真的调外部 embedding API）
    CI 与全量回归清空了密钥             → 兜底 bm25

**同一份代码、两套检索后端、两套指标，而两边都不会报错。** 实测（`eval_set.json`
内嵌通用语料，8 条查询，Top-3）：

    bm25     Top1 61%  Recall 75%  nDCG 0.837  Prec 49%
    hybrid   Top1 54%  Recall 66%  nDCG 0.607  Prec 33%

后果就是「材料充足」这一层在 `make eval`（读 `.env`）下红、在 CI（清空环境）下绿，
而阈值只按 bm25 校准过 —— 看到两个结论的人无从判断该信哪个。

⚠️ **口径漂移比阈值定错更坏**：阈值定错至少每次结论一致，能从历史对比里看出来；
口径漂移是「看起来在监控」，而每次数字都不可比。这正是"评测进 CI"这条链路最隐蔽的失效。

所以评测脚本一律先 `pin()` 再建检索器，比对与门禁**只认这一个口径**。
想横向比各后端的优劣请用 `scripts/eval_retrieval.py` —— 它本来就逐后端跑成一张表，
那是**探索**，不该和**回归门禁**共用一个数字。

为什么连 rerank 一起钉
---------------------
`build_retriever` 里 rerank 的判据是 `RERANK_ENABLED and rerank_configured()`，
**与后端无关** —— 即 bm25 也会被 rerank 改写排序。只要哪台机器配了 RERANK_MODEL，
"钉了 bm25" 的评测仍会走出两套排序。所以钉后端必须连同 rerank 一起钉，
否则这道锁有个看不见的旁路。

用法
----
    from scripts.eval_backend import local_eval

    with local_eval():                      # 进块即钉死，出块自动还原
        retriever = R.build_retriever(chunks)
        ...

也可以用显式的一对：

    prev = pin()
    try: ...
    finally: restore(prev)
"""

from __future__ import annotations

import contextlib

# 唯一认可的评测口径：本地、确定性、零外部调用。改它 = 改所有历史基线的可比性，
# 必须同时重跑全部三层并重设阈值（见 QUALITY_CONSTRAINTS.md）。
LOCAL_BACKEND = "bm25"

# 一起钉住的模块常量。加新项时问一句"它会不会随 .env 变" —— 会，就加进来。
_PINNED = ("EMBEDDING_BACKEND", "RERANK_ENABLED", "EMBEDDING_QUERY_PREFIX")


def _retrieval():
    import src.rag.retrieval as R

    return R


def pin(retrieval=None, backend: str = LOCAL_BACKEND) -> dict:
    """把检索后端钉成确定性本地配置，返回被覆盖的旧值（供 `restore`）。"""
    R = retrieval or _retrieval()
    prev = {name: getattr(R, name) for name in _PINNED}
    R.EMBEDDING_BACKEND = backend
    R.RERANK_ENABLED = False
    R.EMBEDDING_QUERY_PREFIX = ""
    return prev


def restore(prev: dict, retrieval=None) -> None:
    """还原 `pin()` 返回的旧值。"""
    R = retrieval or _retrieval()
    for name, value in prev.items():
        setattr(R, name, value)


@contextlib.contextmanager
def local_eval(retrieval=None, backend: str = LOCAL_BACKEND):
    """`pin()` / `restore()` 的上下文形式 —— 异常路径也会还原。"""
    prev = pin(retrieval, backend)
    try:
        yield
    finally:
        restore(prev, retrieval)
