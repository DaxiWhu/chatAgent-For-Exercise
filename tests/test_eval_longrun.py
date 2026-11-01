"""长任务基线的**口径**必须被守住 —— 它错了会把人引去改错的地方。

为什么值得单独守
----------------
这份基线是"改 instructions / 换模型之后是不是变差了"的唯一依据。它的三处口径
都改过、也都读错过：

* 完成率不把 ``transfer`` 算进来 ⇒ 转交成功的那条被判"没完成"，而低完成率会
  让人去改提示词（真正的问题是统计）；
* 问题率的分母用"用例数" ⇒ ``--repeat 3`` 时"3 次里 1 次报过"被读成
  "这条 100% 偏离"；
* ``problems``（次数）与 ``problems_runs``（几次运行报过）混用 ⇒ 一次运行里
  ``stuck`` 报 3 次会被算成 3 次运行。

另有一处纯观感的：中文按 ``len()`` 补宽会让后面的列整体右移，实测出现
``loop×1`` 与下一列的 ``18`` 粘成 ``loop×118`` —— 读表的人会以为打转了 118 次。

本文件只测**纯函数**（不跑模型、不连库、不花钱）。
"""

from __future__ import annotations

import scripts.eval_longrun as L


def _row(**kw):
    """一条读数的字段齐全版（缺字段会让合并逻辑悄悄取到 None 而不报错）。"""
    row = {
        "name": "用例",
        "outcome": "delivered",
        "reason": "none",
        "exit_reason": "unreviewed",
        "error": "",
        "settled": True,
        "steps": 5,
        "unfinished": 0,
        "oscillation_cycles": 0,
        "problems": {},
        "model_calls": 4,
        "tool_calls": 5,
        "tokens": 1000,
        "tokens_main": 1000,
        "tokens_bypass": 0,
        "cached": 800,
        "answer_chars": 200,
        "seconds": 3.0,
        "min_steps": 0,
    }
    row.update(kw)
    return row


# --------------------------------------------------------------------------- #
# 一、重复取样的合成
# --------------------------------------------------------------------------- #
def test_median_not_mean_so_one_runaway_does_not_drag_it():
    """★ 取中位数而不是均值。

    实测 token 是 9516 / 21306 / 76310 —— 均值会被 76310 那次拉到 35710，
    而中位数 21306 才是"典型一次"的样子。用均值的话，一次跑飞就会让整条
    基线看起来"成本涨了 2 倍"，而实际上什么都没变。
    """
    merged = L._merge_samples(
        {"name": "x"},
        [_row(tokens=9516, tokens_main=9516), _row(tokens=21306, tokens_main=21306),
         _row(tokens=76310, tokens_main=76310)],
    )

    assert merged["tokens"] == 21306
    assert merged["token_range"] == [9516, 76310]


def test_three_different_outcomes_are_marked_contested():
    """★★ 三次全不同时，"取多数"退化成"取第一次"，而那是随机的。

    不标出来，读的人会把"第一次恰好是 error"读成"这条用例崩了"。实测用例 1
    就是 ``manual(guardrail)`` / ``failed(guard_stop)`` / ``partial(stuck)`` 各一次。
    """
    merged = L._merge_samples(
        {"name": "x"},
        [
            _row(outcome="manual", reason="guardrail", exit_reason="output_guardrail"),
            _row(outcome="failed", reason="guard_stop", exit_reason="guard_stop"),
            _row(outcome="partial", reason="stuck", exit_reason="unreviewed"),
        ],
    )

    assert merged["contested"] is True
    assert len(merged["outcome_mix"]) == 3


def test_two_of_three_is_a_majority_so_not_contested():
    """2/3 是同一种 ⇒ 有明确多数，不该打问号（否则每条都会被标成不稳定）。"""
    merged = L._merge_samples(
        {"name": "x"},
        [
            _row(outcome="delivered"),
            _row(outcome="delivered"),
            _row(outcome="partial", reason="stuck"),
        ],
    )

    assert merged["contested"] is False
    assert merged["outcome"] == "delivered"


def test_errors_from_every_sample_are_kept():
    """★ 结局取多数时可能取到**成功**的那次，而 error 如果只取第一个样本就丢了。

    实测用例 2 的三次是 `partial` / `manual` / `failed(error)` —— 结局取多数得到
    partial，于是"其中一次崩了"必须靠 error 字段留着，否则这条用例看起来完全正常。
    """
    merged = L._merge_samples(
        {"name": "x"},
        [
            _row(outcome="partial", error=""),
            _row(outcome="manual", error=""),
            _row(outcome="failed", exit_reason="error", error="ConnectError: boom"),
        ],
    )

    assert merged["outcome"] == "partial", "多数是 partial"
    assert "ConnectError" in merged["error"], "崩过的那次不能被多数掩盖"


def test_no_error_stays_empty():
    merged = L._merge_samples({"name": "x"}, [_row(), _row(), _row()])

    assert merged["error"] == ""


def test_single_sample_is_never_contested():
    merged = L._merge_samples({"name": "x"}, [_row()])

    assert merged["runs"] == 1
    assert merged["contested"] is False
    assert merged["outcome_mix"] == {}


# --------------------------------------------------------------------------- #
# 二、两个"次数"必须分开
# --------------------------------------------------------------------------- #
def test_problem_counts_and_problem_runs_are_different_things():
    """``stuck`` 在一次运行里报了 3 次，只算**一次运行**出过问题。

    混用会让"3 次里 1 次报过 3 回"被读成"9 次运行都出问题"。
    """
    merged = L._merge_samples(
        {"name": "x"},
        [
            _row(problems={"stuck": 3}),
            _row(problems={}),
            _row(problems={}),
        ],
    )

    assert merged["problems"]["stuck"] == 3, "次数要保留（能看出陷了多深）"
    assert merged["problems_runs"]["stuck"] == 1, "运行数只能算 1（只有一次运行报过）"


def test_problem_rate_divides_by_runs_not_by_cases():
    """★ 分母是运行次数。用用例数会把"3 次里 1 次"读成"这条 100% 偏离"。"""
    rows = [
        L._merge_samples({"name": "a"}, [_row(problems={"off_track": 1}), _row(), _row()]),
        L._merge_samples({"name": "b"}, [_row(), _row(), _row()]),
    ]

    # 6 次运行里只有 1 次报过 off_track
    assert L._problem_rate(rows, "off_track") == 1 / 6


def test_problem_rate_on_single_runs_still_works():
    """``--repeat 1`` 时 row 上没有 ``problems_runs``（那是合并时才生成的）。

    不回退到 ``problems`` 的话，``_problem_rate`` 会**恒返回 0** —— 一个
    "看板上什么都不报"的假象，比读数偏高难发现得多。
    """
    rows = [_row(problems={"loop": 1}), _row()]

    assert L._problem_rate(rows, "loop") == 0.5


# --------------------------------------------------------------------------- #
# 三、交付判据
# --------------------------------------------------------------------------- #
def test_transfer_with_an_answer_counts_as_delivered():
    """★ 转交给专家后由它写完，这一次运行**有**产出。

    只数 ``outcome == delivered`` 会把它判成没完成 —— 而 ``outcome`` 为空是因为
    transfer 不走兜底判定，不是因为它没交东西。

    这里测的是 ``_is_settled`` 本身，不是构造好的 row：判据原来内联在
    ``_run_case`` 里，而那个函数要真模型才能跑，于是"把后半截删掉"这种变异
    没有任何测试会发现（实测第一版就是漏的）。
    """
    assert L._is_settled("transfer", "", "一篇小说") is True
    assert L._is_settled("transfer", "", "") is False, "转交了但一个字都没有，不算交付"
    assert L._is_settled("unreviewed", "delivered", "答复") is True
    assert L._is_settled("unreviewed", "partial", "半截") is False
    assert L._is_settled("unreviewed", "manual", "草稿") is False


def test_settled_rate_counts_those_rows():
    rows = [
        _row(outcome="", reason="", exit_reason="transfer", answer_chars=346, settled=True),
        _row(outcome="", reason="", exit_reason="transfer", answer_chars=0, settled=False),
    ]

    assert L._settled_rate(rows) == 0.5, "只有交了东西的那次算"


def test_no_rows_means_zero_not_crash():
    assert L._settled_rate([]) == 0.0
    assert L._problem_rate([], "stuck") == 0.0
    assert L._summary([]) == {}


# --------------------------------------------------------------------------- #
# 四、表格按显示宽度对齐
# --------------------------------------------------------------------------- #
def _shown(text: str) -> int:
    """终端里实际占几列（中文/全角算 2）—— 断言要对齐就得按这个算。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def test_wide_characters_take_two_columns():
    """中文占两列。按 ``len()`` 补宽会让后面每一列右移，数字粘成一串。"""
    assert _shown(L._pad("步", 5)) == 5, "『步』显示占 2 列，再补 3 个空格"
    assert L._pad("abc", 5) == "abc  "
    assert L._pad("abcde", 5) == "abcde", "正好够宽时一个空格都不补"


def test_never_shorter_than_the_text():
    """超宽时不能截断成空 —— 宁可那一行歪掉，也不能把信息丢掉。"""
    assert len(L._pad("很长的中文内容", 3)) >= 7
