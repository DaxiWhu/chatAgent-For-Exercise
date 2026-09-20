import os
import sys

import pytest
from unittest.mock import MagicMock
from langchain_core.messages import AIMessage

# 把项目根目录加入 sys.path，使 `from src.graph import ...` 可用
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.graph import build_graph, ReviewResult  # noqa: E402


class FakeRetriever:
    """假检索器：可指定返回值，避免测试真的去读语料 / 跑 TF-IDF。"""

    def __init__(self, hits=None):
        self.hits = hits if hits is not None else []

    def search(self, query, top_k=3):
        return self.hits


@pytest.fixture
def fake_retriever():
    """默认不召回任何片段（等价于未配置知识库的降级路径）。"""
    return FakeRetriever()


@pytest.fixture
def mock_model():
    """用 MagicMock 模拟 ChatOpenAI：generate 返回草稿，evaluate 返回合法评分。"""
    m = MagicMock()
    m.invoke.return_value = AIMessage(content="这是草稿内容。")
    struct = m.with_structured_output.return_value
    struct.invoke.return_value = ReviewResult(
        is_need_evaluate=True, score=50, feedback="建议修改"
    )
    return m


@pytest.fixture
def graph(mock_model, fake_retriever):
    return build_graph(model=mock_model, retriever=fake_retriever)
