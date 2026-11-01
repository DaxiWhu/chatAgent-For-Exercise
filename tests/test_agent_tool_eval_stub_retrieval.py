"""评测里的桩检索器：三条行为必须钉住，否则评测会静默退化成"只测降级路径"。

为什么单独守
------------
``retriever_factory=None`` 时 ``kb_search`` 恒返回"检索不可用"，于是 ``research``
永远拿不到素材，所有靠取证的专家每轮都在走降级路径 —— 通过率看着不差，但
"拿到素材之后会不会用"这段一次也没测到。上一轮 ``slide-outline`` 掉到 17/18
就是被这个环境条件触发的，当时分不清是模型偏好还是环境逼的。

反过来，桩也会**造出假绿**：如果它对任何查询都返回内容，"检索不到时会如实说"
这条行为就再也测不出来。所以这三条缺一不可 ——
排序（相关在前）、空召回（无关返回空）、截断（top_k 生效）。
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.agent_tool_eval import _stub_factory

SET_PATH = Path(__file__).resolve().parent.parent / "scripts" / "agent_tool_eval_set.json"


def _corpus() -> list[dict]:
    # 两条都要与下面的查询有交集：只用一条命中时，排序断言在"逆序变异"下
    # 仍然通过（只剩一条，怎么排都排第一），那个变异点就成了摆设。
    return [
        {"content": "限流是怎么算的：按 IP 与按会话两个维度各自每分钟 120 次。", "source": "a.md"},
        {"content": "限流怎么配置：两个开关都在配置文件里，默认都打开。", "source": "b.md"},
        {"content": "检索先向量化再做相似度召回，取 top_k 片段。", "source": "c.md"},
    ]


def test_relevant_document_ranks_first():
    retriever = _stub_factory(_corpus())()
    hits = retriever.search("限流是怎么算的", top_k=2)
    assert hits, "相关问题必须召回"
    assert hits[0][0]["source"] == "a.md"
    # 分数必须真的参与排序，不是原样倒出
    assert hits[0][1] >= hits[-1][1]


def test_unrelated_query_recalls_nothing():
    """无关查询返回空 —— 这是桩**唯一**能保住的行为。

    少了它，"检索不到就如实说"这类用例永远通过，而模型实际上从没遇到过空召回。
    """
    retriever = _stub_factory(_corpus())()
    assert retriever.search("今天天气怎么样") == []


def test_top_k_is_respected():
    retriever = _stub_factory(_corpus())()
    hits = retriever.search("限流是怎么算的", top_k=1)
    assert len(hits) == 1
    assert hits[0][0]["source"] == "a.md"


def test_empty_corpus_yields_empty_result():
    """空语料不能崩，也不能凭空造内容。"""
    retriever = _stub_factory([])()
    assert retriever.search("任何问题") == []


def test_missing_fields_get_defaults():
    """语料只写 content 时，其余字段要有可用默认值（引用拼装依赖它们）。"""
    retriever = _stub_factory([{"content": "限流每分钟 120 次"}])()
    chunk, _score = retriever.search("限流")[0]
    assert chunk["source"] == "eval_stub.md"
    assert chunk["source_path"] == "eval_stub.md"
    assert chunk["chunk_id"].startswith("stub::")
    assert chunk["text"] == "限流每分钟 120 次"


def test_shipped_corpus_entries_are_complete():
    """评测集里每条语料都要有 content —— 缺了会被静默过滤，检索又变成永远为空。"""
    data = json.loads(SET_PATH.read_text(encoding="utf-8"))
    corpus = data.get("_stub_corpus") or []
    assert corpus, "评测集必须带 _stub_corpus，否则默认召回恒为空"
    for row in corpus:
        assert (row.get("content") or "").strip(), f"语料缺 content：{row}"
        assert row.get("source"), f"语料缺 source：{row}"
