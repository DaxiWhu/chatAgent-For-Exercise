"""答案质量指标本身的契约。

为什么需要这个测试
------------------
指标坏了比没有指标更危险：没有指标时人会去看答案，指标坏掉时人会**相信数字**。
而坏掉的指标不会报错，它只会安静地给出好看的分。

所以这里用**人工合成的答案**（而不是真实模型输出）钉住每一条判据 ——
合成样本的正确答案是已知的，指标算错立刻看得出来。用真实输出来测则不行：
你没法断言"这条真实答案的幻觉率应该是 0.25"，于是测试只能断言"跑通了"，
而"跑通了"对指标毫无意义。

与 `tests/test_eval_answer.py` 的分工
------------------------------------
那边钉的是「评测脚本拒绝无效输入」（防流程静默失效）；
本文件钉的是「**判据本身算得对**」（防数字静默失真）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("DB_URL", "")

ROOT = Path(__file__).resolve().parent.parent

pytest.importorskip("sklearn", reason="生成侧评测依赖 scikit-learn（BM25 后端的词频矩阵）")

from src.rag import answer_quality as AQ  # noqa: E402

# 一段与 data/kb 风格一致的"资料"。刻意把 `TRUST_PROXY_HEADERS=1` 带反引号写全，
# 因为 `KEY=VALUE` 是模型最常见的写法，也是本模块最容易误报的地方。
CTX = (
    "worker 数量由 `APP_WORKERS` 决定，未配置时回退 `WEB_CONCURRENCY`，最后才是 1。\n"
    "反代场景下 `TRUST_PROXY_HEADERS=1`，它默认为 `False`。\n"
    "健康检查端点：`/health/live` 与 `/health/ready`。\n"
    "被限流时返回 `429`，响应头带 `Retry-After`。\n"
)
Q = "worker 数量从哪里读取"


def _score(answer, ctx=CTX, cites=None, question=Q, **kw):
    return AQ.score_answer(answer, ctx, cites or [], question=question, **kw)


# ---------------- 判据语义：每条指标都要能对、也能错 ----------------

def test_faithful_answer_passes_with_no_unsupported_spans():
    """照资料写的答案必须判可信、且一个未支撑片段都没有。

    这是**误报的底线**：如果连忠实答案都被判出幻觉，指标会被立刻关掉。
    """
    v = _score("worker 数量由 `APP_WORKERS` 决定，没配就回退 `WEB_CONCURRENCY`。")
    assert v["ok"] is True, f"忠实答案被判不可信：{v}"
    assert v["unsupported_identifiers"] == []
    assert v["identifier_hallucination_rate"] == 0.0


def test_fabricated_identifier_makes_the_answer_untrusted():
    """编造标识符 —— 本模块唯一能硬判的错误，必须判 False 并给出理由。"""
    v = _score("worker 数量由 `WORKER_COUNT` 决定，也可以用 `APP_THREADS` 覆盖。")
    assert v["ok"] is False
    assert "unsupported_identifier" in v["reasons"]
    assert {i["text"] for i in v["unsupported_identifiers"]} == {"WORKER_COUNT", "APP_THREADS"}
    assert v["identifier_hallucination_rate"] == 1.0


def test_fabricated_number_is_recorded_but_not_failed():
    """编造数字只记录、不判失败。

    数字的"对不上"有太多合理来源（换算、口径、四舍五入）。把它做成失败判据，
    会立刻把指标变成误报机器，然后被关掉 —— 那时标识符那一半也一起没了。
    """
    v = _score("worker 数量由 `APP_WORKERS` 决定，单实例并发上限是 512。")
    assert v["ok"] is True, "数字不该参与硬判"
    assert [i["text"] for i in v["unsupported_numbers"]] == ["512"]
    assert v["unsupported_identifiers"] == []


def test_key_value_form_is_not_flagged():
    """`` `KEY=VALUE` `` 只核 KEY。

    模型（和人）写配置项时习惯连值一起写。若把整个 `TRUST_PROXY_HEADERS=1`
    当一个标识符去比，语料里只写了 `TRUST_PROXY_HEADERS` 时就会判成编造 ——
    稳定误报，且恰好落在最高频的写法上。
    """
    v = _score("反代后要设 `TRUST_PROXY_HEADERS=1`，默认是 `False`。")
    assert v["ok"] is True, f"`KEY=VALUE` 被误判：{v['unsupported_identifiers']}"
    assert v["unsupported_identifiers"] == []


def test_refusal_is_detected_and_does_not_collect_coverage_warnings():
    """拒答要能被识别，且**不**因此背上覆盖率警告。

    拒答的答案本来就不复述资料（"资料里没有"这句话没有一个字来自资料），
    覆盖率天然为 0。若不静音，每一个正确的拒答都会挂上警告 ——
    这列随即被无视，而"警告列没人看"等于没有警告。
    """
    v = _score("资料里没有提及部署拓扑相关的配置。")
    assert v["refused"] is True
    assert "low_grounded" not in v["warnings"], f"拒答被误报覆盖不足：{v['warnings']}"
    assert "no_citations" not in v["warnings"], f"拒答被误报无引用：{v['warnings']}"
    assert v["ok"] is True


def test_question_words_are_not_hallucinations():
    """复述问题里的词不算编造。

    漏掉这条会让「问什么答什么」被稳定误判成幻觉 —— 而误判高频出现时，
    整套指标的可信度就没了。
    """
    v = _score("关于 worker 数量：由 `APP_WORKERS` 读取，也就是 `WORKER_COUNT_PLACEHOLDER`。",
               ctx="worker 数量由 `APP_WORKERS` 决定。",
               question="WORKER_COUNT_PLACEHOLDER 是什么")
    assert v["unsupported_identifiers"] == []


def test_unknown_citation_reference_is_a_hard_error():
    """答案标了不存在的引用编号 = 编造引用，必须判 False。"""
    cites = [{"n": 1, "source": "a.md", "chunk_id": "c1", "snippet": "资料正文" * 40}]
    v = _score("根据资料 [3]，worker 由 `APP_WORKERS` 决定。", cites=cites)
    assert v["ok"] is False
    assert "bad_citation" in v["reasons"]


# ---------------- 三态语义：None 必须与 False 分开 ----------------

def test_empty_answer_is_unknown_not_failed():
    """空答案是无从判断，不是"不忠实" —— 混起来会让"没答"看起来像"答错"。"""
    v = _score("")
    assert v["ok"] is None
    assert v["grounded_ratio"] is None


def test_no_context_yields_no_judgement_for_identifiers():
    """有标识符要核、但没有资料可核时，ok 必须是 None。

    若这里返回 True，聚合出来的 `ok_rate` 会把"没检查"记成"检查通过" ——
    偏差方向恰好是"看起来更好"，是指标最不该有的偏差。
    """
    v = _score("worker 数量由 `WORKER_COUNT` 决定。", ctx="")
    assert v["ok"] is None, "无资料可核却给了结论"
    assert v["evidence_checked"] is False
    assert v["unsupported_identifiers"] == []
    assert v["grounded_ratio"] is None


def test_grounded_ratio_is_none_when_either_side_is_empty():
    """`grounded_ratio` 的 None 表示"不判"。算成 0.0 会让"资料为空"被读成"完全没依据"。"""
    assert AQ.grounded_ratio("", CTX) is None
    assert AQ.grounded_ratio("有答案", "") is None
    assert AQ.grounded_ratio("有答案", CTX) is not None


def test_grounded_ratio_drops_when_the_answer_drifts_off_the_sources():
    """脱离资料的回答覆盖率必须掉下来 —— 这是这条指标存在的全部理由。"""
    on_topic = AQ.grounded_ratio("worker 数量由 `APP_WORKERS` 决定，最后才是 1。", CTX)
    off_topic = AQ.grounded_ratio("今天天气不错适合出门散步顺便买杯咖啡回来继续写代码。", CTX)
    assert on_topic > off_topic, f"覆盖率没有区分度：{on_topic} vs {off_topic}"


# ---------------- 片段抽取 ----------------

def test_evidence_spans_do_not_double_count():
    """同一段文本只抽一次。

    不去重叠时 `` `APP_WORKERS` `` 会被反引号规则和常量规则各抽一次，
    幻觉率凭空翻倍 —— 而明细看起来完全正常（只是多了一行）。
    """
    spans = AQ.evidence_spans("由 `APP_WORKERS` 决定")
    assert spans.count(("code", "APP_WORKERS")) == 1
    assert "const" not in [k for k, _ in spans]


def test_evidence_spans_classify_paths_and_numbers():
    """裸路径归 path；反引号里的按 code 记（抽取按可信度从高到低，见模块注释）。"""
    spans = dict(AQ.evidence_spans("端点 /health/ready 可用，限流返回 429。"))
    assert spans.get("path") == "/health/ready"
    assert "429" in spans.values()
    assert dict(AQ.evidence_spans("端点 `/health/ready` 可用")).get("code") == "/health/ready"


def test_short_slash_pairs_are_not_treated_as_paths():
    """"可选/必填"这类正常文本里的斜杠不能被当成接口路径。

    不排除的话，中文文档里的并列词会稳定产出"编造路径"。
    """
    spans = [t for k, t in AQ.evidence_spans("该字段可选/必填，见上文。") if k == "path"]
    assert spans == []


def test_bare_acronyms_are_not_identifiers():
    """`API` / `SLO` / `RAG` 这类通用缩写不算标识符。

    它们的共同点是**在资料里未必逐字出现**（中文文档写的是"接口""服务等级目标"），
    算进来就是一大片稳定误报 —— 而误报比漏报更容易毁掉指标的可信度。
    所以常量规则要求"至少含一个下划线"。
    """
    spans = [t for k, t in AQ.evidence_spans("服务提供 API 与 SLO 指标，走 RAG 链路。")
             if k != "number"]
    assert spans == []


# ---------------- 随仓库分发的评测集：自我一致性 ----------------

@pytest.fixture()
def eg():
    """加载生成侧评测脚本（它导入时会 `os.chdir(ROOT)`，用完必须还原）。"""
    cwd = os.getcwd()
    try:
        import scripts.eval_generation as mod
        yield mod
    finally:
        os.chdir(cwd)


def _load_shipped_set():
    return json.loads((ROOT / "scripts" / "generation_set.json").read_text(encoding="utf-8"))


def test_shipped_set_has_both_answerable_and_unanswerable_cases():
    """两类用例缺一不可。

    全是正样本 ⇒ 测不出"会不会编"；全是负样本 ⇒ 测不出"指标会不会误报"。
    而**缺一类不会报错**，只会让某个比率恒等于 1.0，看起来完美。
    """
    spec = _load_shipped_set()
    ans = [c for c in spec["cases"] if c.get("answerable", True)]
    unans = [c for c in spec["cases"] if not c.get("answerable", True)]
    assert ans, "没有可答用例：拒答率会恒为满分，测不出误报"
    assert unans, "没有应拒答用例：幻觉率会恒为满分，测不出编造"


def test_unanswerable_cases_carry_pure_refusals():
    """负样本的参考答案必须是**纯拒答**。

    一旦它提到具体标识符，这条用例的成败就取决于"那一项有没有恰好被检索召回" ——
    同一个参考答案会随检索波动时红时绿，而它本该只测"拒答"这一件事。
    """
    for c in _load_shipped_set()["cases"]:
        if c.get("answerable", True):
            continue
        spans = AQ.evidence_spans(c.get("reference_answer") or "")
        assert not [s for s in spans if s[0] != "number"], (
            f"负样本参考答案里出现了具体标识符 {spans}：{c['q']}"
        )
        assert AQ.refusal_detected(c["reference_answer"]), (
            f"负样本的参考答案没有被识别为拒答：{c['q']}"
        )


def test_answerable_references_only_use_identifiers_from_their_sources():
    """正样本参考答案里的标识符必须逐字出自它自己声明的来源。

    这条是**跨文件的**一致性：语料改了名/挪了文件，参考答案就从"忠实"变成了
    "编造"，而只看评测脚本本身完全看不出来。
    """
    spec = _load_shipped_set()
    corpus = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (ROOT / "data" / "kb").glob("*")
    )
    for c in spec["cases"]:
        if not c.get("answerable", True):
            continue
        for kind, text in AQ.evidence_spans(c["reference_answer"]):
            if kind == "number":
                continue
            assert text in corpus, (
                f"参考答案里的 {kind} `{text}` 在语料里找不到（问题：{c['q']}）—— "
                "参考答案与语料已经脱节"
            )


def test_shipped_set_passes_its_own_gate(eg, monkeypatch, capsys):
    """随仓库分发的评测集必须能通过门禁（否则 CI 一上来就是红的）。"""
    monkeypatch.setattr(sys, "argv", ["eval_generation.py"])
    rc = eg.main()
    out = capsys.readouterr()
    assert rc == 0, f"内置评测集未通过门禁：\n{out.out}\n{out.err}"


def test_ci_runs_the_generation_gate():
    """结构性断言：CI 里真的调了这个脚本。

    写好了门禁但没接进 CI，等于没写 —— 而"没接"不会有任何症状。
    """
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/eval_generation.py" in ci, (
        "CI 里没有调用生成侧质量评测 —— 它不会在任何一次改动中生效"
    )
