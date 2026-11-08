"""基线对比必须能分辨「真退化」与「模型抖了一个用例」。

为什么值得单独守
----------------
把真模型评测接进每晚定时之后，它唯一的用途就是**发现变差**。这件事有两个
相反的失效方式，而且都很安静：

  ① **太紧**：18 个判定轮次里掉 1 轮（5.6 个百分点）就报红。模型本来就有抖动，
     于是每晚都红 —— 人开始忽略它，最后这条守护被关掉。这比没有更糟：它让人
     以为"有人在看质量"。
  ② **太松 / 比错了对象**：基线在写输出**之后**才读，于是"基线"就是刚跑完的
     自己 ⇒ 每一项都持平，永远绿。这种门禁看起来最健康。

所以这里钉三件事：容差是**一个轮次**的量级；基线必须在写盘之前读；分母为 0
的一侧不参与判定（"这次没测"不等于"退化成 0"）。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json

from scripts import agent_tool_eval as E


def _sum(ok: int, total: int, **extra) -> dict:
    """构造一份最小 summary；没显式给的层分母为 0（= 这一批没测这一层）。"""
    base = {
        "turns_total": total,
        "turns_passed": ok,
        "tool_total": 0,
        "tool_ok": 0,
        "result_total": 0,
        "result_ok": 0,
        "soft_total": 0,
        "soft_ok": 0,
        "calls_total": 0,
        "calls_ok": 0,
        "loop_turns": 0,
        "dead_turns": 0,
        "tool_calls": 0,
        "seconds": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    base.update(extra)
    return base


def _layer(diffs: list[dict], key: str) -> dict:
    return next(d for d in diffs if d["key"] == key)


def test_a_real_drop_is_reported_as_degraded():
    """掉 4 个轮次（22 个百分点）必须算真退化 —— 这是这道门的全部意义。"""
    diffs = E._diff_against_baseline(
        {"summary": _sum(18, 18, tool_total=18, tool_ok=18)},
        _sum(14, 18, tool_total=18, tool_ok=14),
    )
    assert _layer(diffs, "tool")["degraded"] is True
    assert any(d["degraded"] for d in diffs)


def test_one_turn_of_jitter_is_not_degraded():
    """★ 掉 1 轮（5.6 个百分点）在容差内。

    太紧的代价不是"更安全"：每晚都红 ⇒ 没人再看 ⇒ 守护被关掉。容差刻意取
    "一个判定轮次的量级"，让单条用例的抖动不至于变成噪音。
    """
    diffs = E._diff_against_baseline(
        {"summary": _sum(18, 18, tool_total=18, tool_ok=18)},
        _sum(17, 18, tool_total=18, tool_ok=17),
    )
    assert _layer(diffs, "tool")["degraded"] is False
    # 但它**不能**被算成"改善" —— 5.6 个百分点的下降就是下降
    assert _layer(diffs, "tool")["delta"] < 0


def test_improvement_is_never_degraded():
    diffs = E._diff_against_baseline(
        {"summary": _sum(14, 18, tool_total=18, tool_ok=14)},
        _sum(18, 18, tool_total=18, tool_ok=18),
    )
    assert _layer(diffs, "tool")["degraded"] is False
    assert _layer(diffs, "tool")["delta"] > 0


def test_a_layer_the_baseline_never_measured_is_skipped_not_failed():
    """旧基线没有"答案结果"这一层（10-30 那版只有选择与执行）。

    这时如果按"基线 0% → 现在 89%"算，会报出一条**不存在的巨大改善**；
    反过来若按"现在没测"算成 0，则会报假退化。两种都不行 ⇒ 必须显式跳过，
    而且屏幕要说明"未对比"（静默跳过等于"这项没退化"）。
    """
    diffs = E._diff_against_baseline(
        {"summary": _sum(12, 12, tool_total=12, tool_ok=12)},
        _sum(18, 18, tool_total=18, tool_ok=18, result_total=9, result_ok=9),
    )
    skipped = _layer(diffs, "result")
    assert skipped["skipped"] is True
    assert skipped["degraded"] is False
    assert skipped["before"] is None and skipped["after"] == 1.0


def test_dead_turns_have_no_tolerance():
    """空转是"有动作、无一成功"的轮次：多一轮就多一轮白烧的模型调用。

    它不像通过率那样有抽样抖动 —— 从 0 到 1 就是从"没有"到"有"。
    """
    grew = E._diff_against_baseline(
        {"summary": _sum(18, 18, dead_turns=0)}, _sum(18, 18, dead_turns=1)
    )
    assert _layer(grew, "dead")["degraded"] is True
    same = E._diff_against_baseline(
        {"summary": _sum(18, 18, dead_turns=2)}, _sum(18, 18, dead_turns=2)
    )
    assert _layer(same, "dead")["degraded"] is False


def test_baseline_is_read_before_the_json_is_written():
    """★ 结构守卫：先读基线、后写输出。

    `--baseline 上次.json --json 上次.json` 是最自然的用法（"与上次比并覆盖"）。
    顺序反过来时，读到的"基线"就是刚落盘的自己 ⇒ 每一项都算持平，门禁永远绿，
    而屏幕上还会认真打印一行"持平"。这种失效不会报任何错。
    """
    src = inspect.getsource(E.main_async)
    read_at = src.index('with open(args.baseline, encoding="utf-8") as fh:')
    write_at = src.index("Path(args.json).write_text")
    assert read_at < write_at, "基线必须在写输出之前读，否则就是自愈门禁"


def test_json_and_screen_share_one_summary():
    """汇总只能有一个入口：屏幕、落盘、对比都取同一份 `summary`。

    两处各算一次时，屏幕上 12/12、落盘的却是另一个数 —— 而看出来的人只会去
    调阈值。这里钉住"落盘不再内联重算"。
    """
    src = inspect.getsource(E.main_async)
    assert '"summary": summary' in src
    assert '"tool_total": len(tool_rows)' not in src


def test_missing_baseline_file_fails_loudly(tmp_path):
    """基线文件不存在 ⇒ 退出码 2，不许"没有基线就直接跑完算绿"。"""
    args = argparse.Namespace(baseline=str(tmp_path / "nope.json"))
    assert asyncio.run(E.main_async(args)) == 2


def test_broken_baseline_file_fails_loudly(tmp_path):
    """基线文件是坏 JSON ⇒ 同样 2；把"读不了"当成"没有基线"会让门禁静默失效。"""
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    args = argparse.Namespace(baseline=str(bad))
    assert asyncio.run(E.main_async(args)) == 2


def test_baseline_file_written_by_this_script_can_be_read_back(tmp_path):
    """自己写的 `--json` 要能被自己当基线读回来（字段名不能漂）。

    这条是给"手改基线"兜底的：字段名漂了之后，对比会静默跳过所有层，
    屏幕上打满"未对比"，而退出码是 0。
    """
    path = tmp_path / "baseline.json"
    summary = _sum(18, 18, tool_total=18, tool_ok=18)
    path.write_text(
        json.dumps({"summary": summary, "cases": []}, ensure_ascii=False), encoding="utf-8"
    )
    loaded = json.loads(path.read_text(encoding="utf-8"))
    diffs = E._diff_against_baseline(loaded, summary)
    measured = [d for d in diffs if not d["skipped"]]
    assert measured, "字段名漂了：一条可对比的层都没有"
    assert all(d["degraded"] is False for d in measured)


def test_baseline_mode_only_fails_on_degradation():
    """★ 带基线时，"基线里本来就有的失败"不许让定时的 job 变红。

    基线里带着一条已知失败（跨轮记忆那条第一轮调了 `remember`）。如果退出码
    仍按"全过才 0"，这条件从接进去的第一天起每晚都红 —— 而每晚都红的通知
    等于没有通知：几周后连真退化也一起被忽略。
    """
    known_failure = _sum(17, 18)
    assert E._exit_code(known_failure, dead=[], degraded=False, has_baseline=True) == 0
    assert E._exit_code(known_failure, dead=[], degraded=True, has_baseline=True) == 1


def test_manual_mode_still_fails_on_any_miss():
    """手动跑（无基线）的语义不变：发现失败，不是发现变化。"""
    assert E._exit_code(_sum(17, 18), dead=[], degraded=False, has_baseline=False) == 1
    assert E._exit_code(_sum(18, 18), dead=[{}], degraded=False, has_baseline=False) == 1
    assert E._exit_code(_sum(18, 18), dead=[], degraded=False, has_baseline=False) == 0


def test_a_subset_run_is_not_comparable_with_a_full_baseline():
    """★ `--only` 跑 1 条得到 100%，不能对着 18 轮的基线报"改善 5.6 个百分点"。

    分母不同就是不可比。假改善比假退化更危险：看到"改善"的人不会去复核。
    """
    full = {"cases": [{"id": f"c{i}"} for i in range(18)]}
    one = [{"id": "c0"}]
    assert E._case_sets_match(full, [{"id": f"c{i}"} for i in range(18)]) is True
    assert E._case_sets_match(full, one) is False


def test_a_summary_only_baseline_is_treated_as_comparable():
    """手写的汇总基线没有 `cases`：只能假定口径一致，让用户自己负责。

    这条防的是"反向过度保护"：基线永远被判不可比 ⇒ 对比功能等于不存在。
    """
    assert E._case_sets_match({"summary": _sum(18, 18)}, [{"id": "c0"}]) is True
