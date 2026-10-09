"""scorecard 的「与基线对比」必须**真的在对比**。

为什么需要这个测试
------------------
`main()` 的默认 `--out` 是 `scripts/scorecard.json`，而「跟上次比」这个用法里
`--baseline` 指的**也是它**。代码原本是**先写后读**：

    json.dump(card, open(args.out, "w"))     # ← 先把这一轮落盘
    base = json.load(open(args.baseline))    # ← 再把"基线"读回来……读到的就是刚写下去的自己
    rows = diff_against(base, card)          # ⇒ 每一项都算成「持平」

于是这条对比**恒为「持平」**，永远说"没退化"。这比没有对比更危险：它看起来在监控。

⚠️ 同一族病的判据（本项目反复踩）：**产物看起来对了，不等于它真的干了活**。
所以这里的断言不是"`--baseline` 能被解析"，而是**基线里那个更差的值必须被报成「有改善」** ——
先写后读的实现下，这一句必红。
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("DB_URL", "")

ROOT = Path(__file__).resolve().parent.parent

pytest.importorskip("sklearn", reason="scorecard 依赖 scikit-learn（BM25 后端的词频矩阵）")


@pytest.fixture()
def sc():
    """加载 scorecard（它在导入时会 `os.chdir(ROOT)`，要兜住 cwd）。"""
    cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        yield importlib.import_module("scripts.eval_scorecard")
    finally:
        os.chdir(cwd)


def _card(**overrides):
    """造一张**结构完整**的 scorecard —— 字段缺失会让 `diff_against` 静默跳过该行。"""
    card = {
        "retrieval": {"top1": 0.61, "top3": 0.75, "mrr": 0.673,
                      "recall": 0.75, "ndcg": 0.837, "precision": 0.49},
        "answer": {"source_coverage": 0.955, "keypoint_coverage": 0.955,
                   "traceable_rate": 1.0},
        "generation": {"ok_rate": 1.0, "identifier_hallucination_rate": 0.0,
                       "refusal_accuracy": 1.0, "avg_grounded": 0.592},
    }
    for path, value in overrides.items():
        layer, key = path.split("__")
        card[layer][key] = value
    return card


def _run(sc, monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["eval_scorecard.py", *argv])
    return sc.main()


def test_baseline_is_read_before_the_output_is_written(sc, monkeypatch, tmp_path, capsys):
    """`--baseline` 与 `--out` 是同一个文件时，仍必须与**上一轮**比，而不是与自己比。"""
    same = tmp_path / "card.json"
    same.write_text(json.dumps(_card(retrieval__top1=0.10)), encoding="utf-8")

    monkeypatch.setattr(sc, "collect", lambda: _card(retrieval__top1=0.61))

    rc = _run(sc, monkeypatch, ["--out", str(same), "--baseline", str(same)])
    out = capsys.readouterr().out

    assert rc == 0, "只是检索变好了，不该被判成退化"
    assert "有改善" in out, (
        "基线是 0.10、当前是 0.61，却没有任何一项被判成「有改善」——"
        "说明基线读到的是刚写下去的自己（先写后读），对比恒为「持平」"
    )
    # 写文件这件事仍然要做（它是下一轮的基线）
    assert json.loads(same.read_text(encoding="utf-8"))["retrieval"]["top1"] == 0.61


def test_missing_baseline_is_rejected_not_silently_skipped(sc, monkeypatch, tmp_path, capsys):
    """基线文件不存在时必须**显式失败**；静默跳过等于让人以为"对比过了、没问题"。"""
    monkeypatch.setattr(sc, "collect", lambda: _card())
    rc = _run(sc, monkeypatch, ["--out", "-",
                                "--baseline", str(tmp_path / "nope.json")])
    assert rc == 2
    assert "基线文件不存在" in capsys.readouterr().err


def test_hallucination_rate_going_up_is_a_regression(sc):
    """方向表要真的生效：幻觉率是**越小越好**，涨了必须报「不进反退」而不是「有改善」。"""
    rows = {r[0]: r[3] for r in sc.diff_against(
        _card(generation__identifier_hallucination_rate=0.0),
        _card(generation__identifier_hallucination_rate=0.05))}
    assert rows["generation.identifier_hallucination_rate"] == "**不进反退**"


def test_small_drift_counts_as_flat(sc):
    """0.005 以内算噪声 —— 不然语料正常增删就会天天报退化，人会直接无视它。"""
    rows = {r[0]: r[3] for r in sc.diff_against(
        _card(retrieval__top1=0.61), _card(retrieval__top1=0.612))}
    assert rows["retrieval.top1"] == "持平"
