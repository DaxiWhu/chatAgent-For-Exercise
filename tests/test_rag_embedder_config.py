"""稠密后端构造参数的守卫：`check_embedding_ctx_length` 必须是 False。

为什么这条**必须**用单测锁死，而不是靠端到端评测发现：
langchain_openai 默认 True，会先用 tiktoken（OpenAI 分词器）把文本切 token
id 再 decode 回字符串。中文经这一趟就不再是原文 —— 送出去的是另一段文本，
拿回来的向量属于另一段文本。症状是「检索永远有结果、排序像随机」：

    2026-11-01 实测（bge-m3，21 个汉字）
        默认档         与原文向量余弦 0.306
        关掉之后       与原文向量余弦 1.000000
    评测集 28 条查询（scripts/eval_set.json，Qdrant 新集合）
        语义路 Top1      14% → 89%
        改写 Top1         0% → 92%（语义检索唯一的存在理由）

而**所有用假 embedder 的离线测试都是绿的** —— 假件拿不到向量，这条路径
在单测里全是模拟的。所以守卫只能盯"构造参数"，盯不了"检索效果"。

（真实语义效果的验收放在 `scripts/eval_retrieval.py`，它需要网关可用。）
"""
import ast
from pathlib import Path

import pytest

import src.rag.retrieval as R

RETRIEVAL_PY = Path(R.__file__)


def test_embedder_disables_the_tiktoken_roundtrip(monkeypatch):
    """★ 工厂造出来的客户端必须跳过 tiktoken 预切分。"""
    # openai SDK 构造期就要求 api_key 非空；单测环境里它可能是空串
    monkeypatch.setattr(R, "EMBEDDING_API_KEY", "sk-test-not-used")

    emb = R._new_embedder()

    assert emb.check_embedding_ctx_length is False, (
        "check_embedding_ctx_length 回到 True ⇒ 中文会被 tiktoken 改写成另一段文本，"
        "向量随之错位；检索会「有结果但排序随机」，且离线测试全绿发现不了。"
    )


def test_embedder_reads_the_project_config(monkeypatch):
    """反方向：别为了关掉 tiktoken 顺手把模型/超时写死，配置得继续生效。"""
    monkeypatch.setattr(R, "EMBEDDING_API_KEY", "sk-test-not-used")
    monkeypatch.setattr(R, "EMBEDDING_MODEL", "BAAI/bge-m3-test")
    monkeypatch.setattr(R, "EMBEDDING_TIMEOUT", 7)

    emb = R._new_embedder()

    assert emb.model == "BAAI/bge-m3-test"
    assert emb.request_timeout == 7


def _calls_in(path: Path):
    """返回文件里所有 `OpenAIEmbeddings(...)` 调用的 (所在函数名, 行号)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []

    def walk(node, func=None):
        for child in ast.iter_child_nodes(node):
            name = func
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            if (isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id == "OpenAIEmbeddings"):
                out.append((name, child.lineno))
            walk(child, name)

    walk(tree)
    return out


def test_every_dense_backend_goes_through_the_factory():
    """★ 三个稠密后端（内存 / pgvector / qdrant）都不许自己 new 客户端。

    为什么盯这个而不是盯每个类：新增后端时最容易的就是复制一段
    `OpenAIEmbeddings(...)` 过来，然后忘了带那个参数 —— 那种漏法在这条
    测试面前会直接红，而在「评测分数下降」面前要等到有人看数字才发现。
    """
    calls = _calls_in(RETRIEVAL_PY)

    assert calls, "一个 OpenAIEmbeddings 调用都没找到，说明这个守卫和实现对不上了"
    strangers = [(fn, ln) for fn, ln in calls if fn != "_new_embedder"]
    assert not strangers, (
        f"这些地方绕过了 _new_embedder 自己构造 embedding 客户端：{strangers}。"
        "请改回 `_new_embedder()` —— 否则会漏掉 check_embedding_ctx_length=False。"
    )


@pytest.mark.parametrize("backend", ["memory", "semantic", "semantic_dummy"])
def test_backend_switch_does_not_change_the_constructor(backend, monkeypatch):
    """切换后端名不该影响构造参数（防止有人只在某一条分支上补参数）。"""
    monkeypatch.setattr(R, "EMBEDDING_API_KEY", "sk-test-not-used")
    monkeypatch.setattr(R, "EMBEDDING_BACKEND", backend)

    emb = R._new_embedder()
    assert emb.check_embedding_ctx_length is False
