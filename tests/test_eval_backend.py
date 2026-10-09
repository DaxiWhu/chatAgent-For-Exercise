"""评测口径必须**钉死在后端上**，而不是随开发机的 `.env` 漂。

为什么需要这个测试
------------------
`src/rag/retrieval.py` 在**导入期**就 `load_dotenv(dotenv_path=仓库根/.env)`，
把 `RAG_EMBEDDING` / `RAG_RERANK` 读成模块常量。于是同一个评测脚本：

    有 EMBEDDING_API_KEY 的开发机  → hybrid（真的调外部 embedding API）
    CI / 全量回归（清空密钥）       → 兜底 bm25

这份代码在两条路径上跑出**两套指标**，而两边都不报错。实测后果：「材料充足」这层
在 `make eval`（读 `.env`）下红 —— 来源覆盖 0.841 < 阈值 0.87，在 CI 下绿 —— 0.955，
而阈值只按 bm25 校准过。看日志的人无从判断该信哪一个，甚至连"这是口径问题"都看不出来。

⚠️ 这类守卫**最不能写成"源码里有没有那句话"**（见 `MEMORY.md` §2「检查规则≠检查事实」）：
`.gitattributes` 说 LF 全绿、磁盘上照样有 CRLF 文件。所以这里的核心断言是在
`build_retriever` **被调用的那一刻**去读模块常量，断言它确实是锁定的口径 ——
把 `local_eval` 从任何一个脚本里删掉，对应用例必红。
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("DB_URL", "")

ROOT = Path(__file__).resolve().parent.parent

pytest.importorskip("sklearn", reason="评测脚本依赖 scikit-learn（BM25 后端的词频矩阵）")

#: 这些脚本**故意**不加锁 —— 它的工作就是逐后端跑成一张表做对比。
#: 把"探索"和"回归门禁"混成一个数字，才是真正的问题（见 eval_backend 模块文档）。
EXPLORATION_SCRIPTS = {"eval_retrieval.py"}


@pytest.fixture()
def retrieval():
    import src.rag.retrieval as R

    return R


@pytest.fixture()
def polluted(retrieval):
    """把模块常量改成"这台开发机装了密钥"的样子，用完还原。

    ⚠️ 这两个常量是**模块级全局单例**：不还原就会把 hybrid 泄漏给同一会话里
    其它依赖检索的用例（表现为随机红，且看起来像它们自己的问题）。
    """
    saved = (retrieval.EMBEDDING_BACKEND, retrieval.RERANK_ENABLED,
             retrieval.EMBEDDING_QUERY_PREFIX)
    retrieval.EMBEDDING_BACKEND = "hybrid"
    retrieval.RERANK_ENABLED = True
    retrieval.EMBEDDING_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
    try:
        yield
    finally:
        (retrieval.EMBEDDING_BACKEND, retrieval.RERANK_ENABLED,
         retrieval.EMBEDDING_QUERY_PREFIX) = saved


def _spy_build(retrieval, monkeypatch) -> list:
    """拦在 `build_retriever` 上，记录**调用那一刻**的口径。

    为什么不在调用前后各读一次：那样测的是"有没有还原"，而不是"有没有钉住" ——
    一个只还原、不钉住的实现照样能过。
    """
    seen: list = []
    real = retrieval.build_retriever

    def spy(chunks, *args, **kwargs):
        seen.append((retrieval.EMBEDDING_BACKEND, retrieval.RERANK_ENABLED,
                     retrieval.EMBEDDING_QUERY_PREFIX))
        return real(chunks, *args, **kwargs)

    monkeypatch.setattr(retrieval, "build_retriever", spy)
    return seen


def _import_script(name):
    """导入评测脚本（它们导入时会 `os.chdir(ROOT)`，要自己兜住 cwd）。"""
    cwd = os.getcwd()
    os.chdir(ROOT)  # 先切到仓库根，让脚本里的相对路径与真实运行时一致
    try:
        return importlib.import_module(f"scripts.{name}")
    finally:
        os.chdir(cwd)


# ---------------- 锁本身 ----------------

def test_pin_forces_local_backend_and_restores(retrieval, polluted):
    from scripts.eval_backend import LOCAL_BACKEND, local_eval

    assert retrieval.EMBEDDING_BACKEND == "hybrid", "前置：模块常量此时应是污染态"
    with local_eval():
        assert retrieval.EMBEDDING_BACKEND == LOCAL_BACKEND == "bm25"
        assert retrieval.RERANK_ENABLED is False
        assert retrieval.EMBEDDING_QUERY_PREFIX == ""
    assert (retrieval.EMBEDDING_BACKEND, retrieval.RERANK_ENABLED,
            retrieval.EMBEDDING_QUERY_PREFIX) == (
        "hybrid", True, "为这个句子生成表示以用于检索相关文章：")


def test_pin_restores_even_when_the_block_raises(retrieval, polluted):
    """评测中途抛异常（语料加载失败等）不能把后端留在锁定态 —— 那会污染同进程后续调用。"""
    from scripts.eval_backend import local_eval

    with pytest.raises(RuntimeError):
        with local_eval():
            assert retrieval.EMBEDDING_BACKEND == "bm25"
            raise RuntimeError("评测中途炸了")
    assert retrieval.EMBEDDING_BACKEND == "hybrid"


def test_rerank_is_pinned_not_just_the_backend(retrieval, polluted):
    """**只钉后端是不够的**：rerank 的判据与后端无关，bm25 也会被它改写排序。

    谁在 `.env` 里配了 `RERANK_MODEL`，谁就会从"钉了 bm25"的评测里拿到另一套排序。
    """
    from scripts.eval_backend import local_eval

    with local_eval():
        assert retrieval.EMBEDDING_BACKEND == "bm25"
        assert retrieval.RERANK_ENABLED is False


# ---------------- 谁真的用了这把锁（行为层） ----------------

def test_scorecard_builds_every_layer_under_the_lock(monkeypatch, retrieval, polluted):
    """三层**各自**建检索器时都必须是锁定口径，且跑完把常量还回去。"""
    seen = _spy_build(retrieval, monkeypatch)
    SC = _import_script("eval_scorecard")

    card = SC.collect()

    assert len(seen) == 3, f"三层应各建一次检索器，实际 {len(seen)} 次"
    assert set(seen) == {("bm25", False, "")}, f"有层没在锁下跑：{sorted(set(seen))}"
    assert retrieval.EMBEDDING_BACKEND == "hybrid", "collect() 跑完没还原模块常量"
    assert {"retrieval", "answer", "generation"} <= set(card)
    assert card["answer"]["source_coverage"] > 0.8, "锁生效时不应退化到「材料不够」"


@pytest.mark.parametrize("name", ["eval_answer", "eval_generation"])
def test_standalone_eval_scripts_take_the_lock_too(name, monkeypatch, retrieval, polluted):
    """单独跑（`make eval` 的第 2、3 步）也必须锁 —— scorecard 罩不到它们。"""
    seen = _spy_build(retrieval, monkeypatch)
    mod = _import_script(name)

    monkeypatch.setattr(sys, "argv", [f"{name}.py"])
    cwd = os.getcwd()
    try:
        rc = mod.main()
    finally:
        os.chdir(cwd)

    assert seen, "根本没走到 build_retriever —— 用例本身失效了"
    assert set(seen) == {("bm25", False, "")}, f"{name} 没在锁下跑：{sorted(set(seen))}"
    assert rc == 0, f"{name} 在锁定口径下应通过门禁（rc={rc}）"
    assert retrieval.EMBEDDING_BACKEND == "hybrid", "main() 跑完没还原模块常量"


# ---------------- 谁该用这把锁（规则层，只拦"新脚本忘加锁"） ----------------

def test_every_eval_script_that_builds_a_retriever_takes_the_lock():
    """规则层：将来新加的评测脚本若自己建检索器却不加锁，这里会红。

    ⚠️ 这条**不是**主要守卫（行为层见上面三条）—— 它只是让"新写了一个脚本、
    忘了加锁"这种增量失效有个门。单有它是不够的：把三个脚本里的 `local_eval()`
    调用全删掉，只留一句注释也能过。
    """
    offenders = []
    for path in sorted((ROOT / "scripts").glob("eval_*.py")):
        if path.name in EXPLORATION_SCRIPTS:
            continue
        src = path.read_text(encoding="utf-8")
        if "build_retriever" in src and "local_eval" not in src:
            offenders.append(path.name)
    assert offenders == [], (
        f"这些评测脚本自己建检索器但没锁口径：{offenders}；"
        "口径漂移会让同一份代码在不同机器上给出两套结论且都不报错"
    )
