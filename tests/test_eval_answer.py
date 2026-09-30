"""答案质量评测本身的契约。

为什么需要这个测试
------------------
评测脚本最容易出的一类问题是**它自己悄悄失效**，而失效的表现是"全绿"：

  1. **空评测给满分**：用例集被清空 / JSON 键名写错 → 循环不执行 →
     覆盖率算成 0/0。若不显式拦，它会报"不达标"（看起来像质量问题），
     或者更糟：被写成 `or 1.0` 之后直接满分通过。
  2. **语料为空**：`load_chunks` 对不存在的目录返回 `[]`（刻意如此，服务不该
     因路径写错就起不来）。此时覆盖率必然 0，但**归因会指向检索**，
     而真问题是语料 —— 必须单独拦住并说清楚。
  3. **评测集写错术语**：`key_points` 必须用语料里真实出现的措辞。写近义词
     不会报错，只会让覆盖率悄悄低于真实能力，于是维护者"调低阈值"了事。
     （这不是假设 —— 本文件成稿前，"进程内"就被写成了"本地"，覆盖率因此
     少了 4.5 个点。）

以上三条都不是"脚本写错了"，而是"脚本没有拒绝无效输入"。所以这里逐条钉住。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("DB_URL", "")

ROOT = Path(__file__).resolve().parent.parent

pytest.importorskip("sklearn", reason="评测脚本依赖 scikit-learn（BM25 后端的词频矩阵）")


@pytest.fixture()
def ea():
    """加载评测脚本。

    ⚠️ 该模块在导入时执行 `os.chdir(ROOT)`（与 `eval_retrieval.py` 同款写法，
    为的是让 `docs_dir: "data"` 这类相对路径在任何 cwd 下都成立）。
    导入会**改变测试进程的 cwd**，所以必须在 fixture 里懒加载并还原 ——
    否则它会静默影响同一次 pytest 会话里其它依赖相对路径的用例。
    """
    cwd = os.getcwd()
    try:
        import scripts.eval_answer as mod
        yield mod
    finally:
        os.chdir(cwd)


def _write_set(path: Path, docs_dir: str, cases: list) -> str:
    path.write_text(json.dumps({"docs_dir": docs_dir, "cases": cases},
                               ensure_ascii=False), encoding="utf-8")
    return str(path)


def _run(ea, monkeypatch, set_path: str, extra=()) -> int:
    monkeypatch.setattr(sys, "argv",
                        ["eval_answer.py", "--set", set_path, *extra])
    return ea.main()


# ---------------- 无效输入必须被拒绝（而不是给满分） ----------------

def test_empty_case_list_is_rejected(ea, monkeypatch, tmp_path, capsys):
    """空用例集**必须**返回码非 0 —— 否则它是一个永远绿的门禁。"""
    p = _write_set(tmp_path / "empty.json", "data", [])
    rc = _run(ea, monkeypatch, p)
    assert rc == 2, "空用例集被当成通过 —— 这条门禁从此不再具有任何意义"
    assert "cases" in capsys.readouterr().err


def test_missing_corpus_is_rejected_with_a_clear_reason(ea, monkeypatch, tmp_path, capsys):
    """语料目录不存在时必须单独报错，而不是报"覆盖率不达标"。

    两者都会让 CI 变红，但**归因完全不同**：前者要去看挂载与路径，
    后者会让人去调检索参数 —— 修错方向比不修更耗时。
    """
    p = _write_set(tmp_path / "cases.json", str(tmp_path / "no-such-dir"),
                   [{"q": "任意", "expect_sources": [], "key_points": []}])
    rc = _run(ea, monkeypatch, p)
    assert rc == 2
    err = capsys.readouterr().err
    assert "0 个片段" in err and "评测无意义" in err


def test_missing_eval_set_file_is_rejected(ea, monkeypatch, tmp_path, capsys):
    rc = _run(ea, monkeypatch, str(tmp_path / "nope.json"))
    assert rc == 2
    assert "不存在" in capsys.readouterr().err


# ---------------- 门禁真的会失败 ----------------

def test_degraded_corpus_fails_the_gate(ea, monkeypatch, tmp_path, capsys):
    """把语料换成不相关的内容后，门禁必须**变红**。

    这条是本文件的核心：它验证 `--fail-under` 是一条真的门禁，而不是
    一个恒真的装饰。退化方式是模拟"知识库被替换/被清空成无关内容"——
    检索仍然正常返回结果，只是回答不了问题。
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "unrelated.md").write_text("# 无关文档\n\n今天天气不错，适合出门散步。\n",
                                       encoding="utf-8")
    p = _write_set(tmp_path / "cases.json", str(docs), [
        {"q": "限流的判定值是怎么计算的",
         "expect_sources": ["04-限流与配额.md"],
         "key_points": ["加权", "上一窗口"]},
    ])
    rc = _run(ea, monkeypatch, p)
    assert rc == 1, "语料被换成无关内容后门禁仍然通过 —— 这条门禁没有守住任何东西"
    err = capsys.readouterr().err
    assert "未达门禁" in err


def test_gate_reports_which_metric_failed(ea, monkeypatch, tmp_path, capsys):
    """失败信息必须指出**是哪一项**没达标，否则排障要从头猜。

    构造方式：来源命中、关键点不命中 —— 于是报错里**只应**出现关键点那一项。
    若两项都报，说明代码把"任一不达标"粗暴地全列出来，归因就模糊了。
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    # 语料必须与查询有词面重叠，否则 BM25 直接返回空 —— 那时三项全 0，
    # 报错里自然什么都有，测不到"归因是否精确"这件事（本用例第一次就是这么红的）。
    (docs / "limit.md").write_text("# 限流\n\n限流使用加权滑动窗口，判定值由加权计算。\n",
                                   encoding="utf-8")
    p = _write_set(tmp_path / "cases.json", str(docs), [
        {"q": "限流的加权判定值是怎么算的",
         "expect_sources": ["limit.md"],
         "key_points": ["这个词在语料里根本不存在"]},
    ])
    rc = _run(ea, monkeypatch, p)
    assert rc == 1
    err = capsys.readouterr().err
    assert "关键点覆盖" in err, "没报出未达标的那一项，排障无从下手"
    assert "来源覆盖" not in err, (
        "来源命中了、只有关键点没命中，报错里不该出现来源覆盖 —— 归因必须精确"
    )


# ---------------- 指标语义 ----------------

class _FakeRetriever:
    def __init__(self, hits):
        self._hits = hits

    def search(self, q, top_k=3):
        return self._hits


def test_citations_without_chunk_id_break_traceability(ea):
    """缺 `chunk_id` 的引用必须被判为不可回溯。

    "有引用"本身没有价值 —— 用户点开要能回到原文件。没有 id 的引用
    在界面上看起来完全正常，只是点了没反应。
    """
    chunk = {"source": "a.md", "text": "正文"}
    details, summary = ea.evaluate(
        [{"q": "x", "expect_sources": ["a.md"], "key_points": []}],
        _FakeRetriever([(chunk, 0.9)]),
        [chunk],                      # 语料里也没有带 id 的块
    )
    assert details[0]["traceable"] is False
    assert details[0]["bad_cites"] == 1
    assert summary["traceable_rate"] == 0.0


def test_source_coverage_counts_subset_hits(ea):
    """多个来源共同支撑一个答案时，命中其中一部分就要按比例计分。

    若写成"必须全中才算命中"，跨文档的问题会全部记 0 —— 指标会失真成
    "知识库不行"，而实际只是要求过严。
    """
    hits = [({"source": "a.md", "chunk_id": "1", "text": "甲"}, 0.9)]
    _details, summary = ea.evaluate(
        [{"q": "x", "expect_sources": ["a.md", "b.md"], "key_points": []}],
        _FakeRetriever(hits),
        [{"source": "a.md", "chunk_id": "1", "text": "甲"}],
    )
    assert summary["source_coverage"] == pytest.approx(0.5)


def test_key_points_are_matched_against_the_online_context_shape(ea):
    """关键点要在**线上同款**的上下文拼装结果里匹配。

    评测必须复用 `context_block`：自造一份"评测专用"的拼接方式，
    测的就是一条线上不存在的链路 —— 覆盖率再漂亮也没有意义。
    """
    chunk = {"source": "a.md", "chunk_id": "1", "text": "加权滑动窗口"}
    _details, summary = ea.evaluate(
        [{"q": "x", "expect_sources": ["a.md"], "key_points": ["加权"]}],
        _FakeRetriever([(chunk, 0.9)]),
        [chunk],
    )
    assert summary["keypoint_coverage"] == 1.0


# ---------------- 真实评测集的自我一致性 ----------------

def test_shipped_eval_set_references_existing_sources(ea):
    """评测集里写的来源文件必须真的在语料目录里。

    文件名打错（或语料文件被改名）不会报错，只会让该条用例**永远未命中** ——
    表现是覆盖率长期偏低，而没人会去怀疑评测集本身。
    """
    spec = json.loads((ROOT / "scripts" / "eval_answer_set.json")
                      .read_text(encoding="utf-8"))
    from src.rag import retrieval

    chunks = retrieval.load_chunks(spec.get("docs_dir", "data"))
    assert chunks, "评测集指向的语料目录加载不到任何片段"
    available = {c.get("source") for c in chunks}

    missing = sorted({s for case in spec["cases"]
                      for s in (case.get("expect_sources") or [])
                      } - available)
    assert not missing, (
        f"评测集里的这些来源在语料中不存在：{missing}\n"
        "它们对应的用例会永远未命中 —— 是评测集/语料被改了，不是检索变差了。"
    )


def test_every_key_point_appears_somewhere_in_the_corpus(ea):
    """关键点必须能在语料全文里找到（不要求落在 Top-K 内）。

    这条防的是"把近义词写进 key_points"：那样覆盖率会**永久性**低于真实能力，
    并诱导维护者去调低阈值。子串匹配的代价是同义改写会漏判，
    所以选点必须是语料里的原词 —— 这条断言把该约定变可执行。
    """
    spec = json.loads((ROOT / "scripts" / "eval_answer_set.json")
                      .read_text(encoding="utf-8"))
    from src.rag import retrieval

    chunks = retrieval.load_chunks(spec.get("docs_dir", "data"))
    corpus = "\n".join((c.get("content") or c.get("text") or "") for c in chunks)

    bad = []
    for case in spec["cases"]:
        for pt in (case.get("key_points") or []):
            if pt not in corpus:
                bad.append((case["q"], pt))
    assert not bad, (
        "以下关键点在语料全文里都找不到，属于「写成了近义词」：" + repr(bad) +
        "\n请改用语料中真实出现的措辞，否则覆盖率会永久偏低并诱导调低阈值。"
    )


def test_shipped_eval_set_passes_its_own_gate(ea, monkeypatch, capsys):
    """随仓库分发的评测集必须能通过门禁（否则 CI 一上来就是红的）。"""
    rc = _run(ea, monkeypatch, str(ROOT / "scripts" / "eval_answer_set.json"))
    out = capsys.readouterr()
    assert rc == 0, f"内置评测集未通过门禁：\n{out.out}\n{out.err}"


def test_ci_runs_the_answer_gate():
    """结构性断言：CI 里真的调了这个脚本。

    写好了门禁但没接进 CI，等于没写 —— 而"没接"不会有任何症状。
    """
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/eval_answer.py" in ci, (
        "CI 里没有调用答案质量评测 —— 它不会在任何一次改动中生效"
    )
