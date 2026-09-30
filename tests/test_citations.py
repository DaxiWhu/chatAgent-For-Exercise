"""引用（citations）层测试：**检索结果里的元数据必须真的能交到用户手上**。

这一层存在的唯一理由就是"可回溯"：用户看到答案后要能确认它出自哪份文档、
第几页、哪一节、原文长什么样、表格原网格是什么、配图在哪。
所以测试的重点不是"有没有 citations 字段"，而是**保真数据有没有被加工品顶掉**。
"""
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage

from src.graph import retrieve
from src.rag.retrieval import build_citations, context_block

# 一个"像真产物一样"的命中：保真 content 与加工过的 text 明显不同
_GRID = "| 地区 | 销售额 |\n|---|---|\n| 华东 | 1200 |\n| 华南 | 980 |"

# 一个"像真产物一样"的命中：保真 content 与加工过的 text 明显不同。
# 注意 content 与 table_md 一致 —— 真实的纯表格块就是这样（content 由 md 拼成）。
_TABLE_HIT = {
    "source": "年报.pdf", "page": 3, "kind": "table", "parser": "docling",
    "origin": "docling_pdf", "chunk_id": "abc123", "section": "第三章 > 出口情况",
    "bbox": [[1.0, 2.0, 3.0, 4.0]], "para_index": None, "anchor_text": "",
    "table_md": _GRID,
    "content": _GRID,
    "text": "第三章 > 出口情况；地区=华东；销售额=1200 地区=华南；销售额=980",
    "tags": ["表格"], "needs_vlm": False,
}


def _cites(hits, **kw):
    return build_citations([(h, s) for h, s in hits], **kw)


# ---------------------------------------------------------------------------
# 保真优先：这是整层的关键判据
# ---------------------------------------------------------------------------
def test_snippet_comes_from_content_not_from_processed_text():
    """回显的"原文"必须取 content（保真网格），不能取 text（文字化后的加工品）。

    取错的表现很隐蔽：引用看起来"有内容"，但用户拿它去核对原文会发现对不上 ——
    原文是网格，回显的是 `地区=华东；销售额=1200`。
    """
    c = _cites([(_TABLE_HIT, 0.87)])[0]
    assert "| 地区 | 销售额 |" in c["snippet"]
    assert "地区=华东" not in c["snippet"]


def test_citation_carries_position_and_identity():
    """定位三件套（来源 / 页码 / 章节）+ 幂等 id 一个都不能少，否则没法回溯。"""
    c = _cites([(_TABLE_HIT, 0.87)])[0]
    assert c["n"] == 1 and c["source"] == "年报.pdf" and c["page"] == 3
    assert c["section"] == "第三章 > 出口情况"
    assert c["chunk_id"] == "abc123" and c["kind"] == "table"
    assert c["parser"] == "docling" and c["origin"] == "docling_pdf"
    assert c["bbox"] == [[1.0, 2.0, 3.0, 4.0]], "PDF 的坐标要留给'裁原页截图'用"


def test_where_shows_the_full_page_span():
    """跨页块要报出真实范围，而不是只报起始页。

    之前只有 `page` 一个字段，跨页正文会显示成"第 3 页"，去核对的人在那一页
    只看得到一半 —— 而且不知道还有后半段。
    """
    hit = dict(_TABLE_HIT, page=3, pages=[3, 4, 5])
    c = _cites([(hit, 0.5)])[0]
    assert c["pages"] == [3, 4, 5]
    assert "第 3–5 页" in c["where"]


def test_where_lists_pages_when_the_span_is_not_contiguous():
    """跳页不能写成区间 —— 那会让人以为中间那页也有内容。"""
    hit = dict(_TABLE_HIT, page=3, pages=[3, 5])
    assert "第 3、5 页" in _cites([(hit, 0.5)])[0]["where"]


def test_where_falls_back_to_single_page_without_pages_field():
    """旧数据（没有 pages）行为不变 —— 新增字段不能让存量数据先迁移才能用。"""
    c = _cites([(_TABLE_HIT, 0.5)])[0]
    assert "第 3 页" in c["where"]


def test_where_keeps_sheet_and_slide_wording_across_pages():
    """xlsx / pptx 的单位说法要跟着范围一起走，不能一律说成"页"。"""
    xlsx = dict(_TABLE_HIT, source="台账.xlsx", parser="xlsx", page=2, pages=[2, 3])
    assert "第 2–3 个工作表" in _cites([(xlsx, 0.5)])[0]["where"]
    pptx = dict(_TABLE_HIT, source="汇报.pptx", parser="pptx", page=4, pages=[4, 5])
    assert "第 4–5 页幻灯片" in _cites([(pptx, 0.5)])[0]["where"]


def test_table_md_is_passed_through_verbatim():
    """表格网格原样带出：这是"原版数据"里最容易被加工掉、又最需要核对的部分。"""
    c = _cites([(_TABLE_HIT, 0.5)])[0]
    assert c["table_md"] == _TABLE_HIT["table_md"]
    assert "华南" in c["table_md"]


def test_image_path_and_alt_are_included_when_present():
    hit = dict(_TABLE_HIT, kind="diagram", table_md="",
               source_image="data/.media/年报_x/p003_fig01.png", alt_text="组织架构示意图")
    c = _cites([(hit, 0.4)])[0]
    assert c["source_image"].endswith("p003_fig01.png")
    assert c["alt_text"] == "组织架构示意图"


def test_empty_attachment_fields_are_omitted():
    """没有的字段不要带出去：一堆空值会把引用撑胖，还会让人以为"这里本该有图"。"""
    c = _cites([(_TABLE_HIT, 0.5)], snippet_chars=10_000)[0]
    assert "source_image" not in c and "alt_text" not in c
    assert "table_md" in c                      # 这个确实有


def test_snippet_is_truncated_and_flagged():
    """节选要截断并**明确标记**：状态会被 checkpointer 持久化，不能塞整块原文。"""
    hit = dict(_TABLE_HIT, content="甲" * 5000)
    c = _cites([(hit, 0.5)], snippet_chars=200)[0]
    assert len(c["snippet"]) == 201 and c["snippet"].endswith("…")
    assert c["truncated"] is True


def test_short_content_is_not_flagged_as_truncated():
    c = _cites([(_TABLE_HIT, 0.5)], snippet_chars=10_000)[0]
    assert c["truncated"] is False and not c["snippet"].endswith("…")


def test_no_hits_yields_no_citations():
    assert build_citations([]) == []


def test_multiple_hits_are_numbered_in_rank_order():
    """n 要与排序一致 —— 前端拿 n 做角标，错位会让引用和正文对不上。"""
    a = dict(_TABLE_HIT, source="a.pdf", chunk_id="a")
    b = dict(_TABLE_HIT, source="b.pdf", chunk_id="b")
    cites = _cites([(a, 0.9), (b, 0.7)])
    assert [(c["n"], c["source"]) for c in cites] == [(1, "a.pdf"), (2, "b.pdf")]
    assert cites[0]["score"] > cites[1]["score"]


def test_snippet_falls_back_to_text_when_content_missing():
    """没有 content 的旧数据也要能用（不能因为字段缺失就给出空引用）。"""
    hit = {"source": "old.txt", "text": "旧格式只有 text 字段"}
    c = _cites([(hit, 0.3)])[0]
    assert "旧格式只有 text 字段" in c["snippet"]
    assert c["kind"] == "" and c["page"] is None


# ---------------------------------------------------------------------------
# 送模型的文本：父窗 > 保真 > 检索文本
# ---------------------------------------------------------------------------
def test_context_block_prefers_parent_window():
    hit = dict(_TABLE_HIT, parent_text="上一段。命中段。下一段。")
    assert "上一段。" in context_block(hit)


def test_context_block_prefers_content_over_processed_text():
    """没有父窗时用保真内容，而不是被文字化过的 text。"""
    block = context_block(_TABLE_HIT)
    assert "| 地区 | 销售额 |" in block
    assert "地区=华东" not in block


def test_context_block_always_carries_the_source_label():
    assert context_block(_TABLE_HIT).startswith("[来源: 年报.pdf]")


# ---------------------------------------------------------------------------
# 接进图：retrieve 节点必须把引用放进 state
# ---------------------------------------------------------------------------
class _FakeRetriever:
    def __init__(self, hits):
        self.hits = hits

    def search(self, query, top_k=3):
        return self.hits


def _state(text="华东区销售额是多少"):
    from src.graph import OverAllState
    return OverAllState(messages=[AIMessage(content=text)])


def test_retrieve_puts_citations_into_state(monkeypatch):
    import src.graph as g

    monkeypatch.setattr(g, "_RETRIEVER", _FakeRetriever([(_TABLE_HIT, 0.9)]))
    out = retrieve(_state())
    assert out["citations"] and out["citations"][0]["source"] == "年报.pdf"
    assert out["citations"][0]["page"] == 3
    # 送给模型的上下文用保真内容（网格），而不是文字化结果
    assert "| 地区 | 销售额 |" in out["context"]


def test_retrieve_citations_empty_on_failure(monkeypatch):
    """检索失败时引用也要清空 —— 残留上一轮的引用比没有引用更危险。"""
    import src.graph as g

    class _Boom:
        def search(self, query, top_k=3):
            raise RuntimeError("index broken")

    monkeypatch.setattr(g, "_RETRIEVER", _Boom())
    out = retrieve(_state())
    assert out["context"] == "" and out["citations"] == []
