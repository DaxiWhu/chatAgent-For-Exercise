"""引用校验（`src/rag/citation_check.py`）的契约。

要解决的问题：`build_citations()` 只做**结构转换**，不校验任何东西。于是接口层
看到 `citations` 非空，只能说明"这一轮检索命中了东西"—— 推不出"答案用了它"，
更推不出"答案有依据"。模型完全可以在命中 3 条之后写出一段与三条都不沾边的话，
还带着 3 条引用；前端如实展示，看起来就像"有出处"。

本文件钉住的都是"错了也不会报错、只是悄悄失效"的部分：

  1. **`block` 只对确凿错误生效** —— 重叠率是启发式判据，拿它吞掉用户的引用，
     误伤成本远大于收益；而"拦错了"不会报错，只会让引用区空着（与"本轮没检索到"
     长得一模一样）；
  2. **"没法算"与"算出来是 0"必须分开** —— 引用太短时比率没有意义，
     混成 0.0 会让"只有一条超短引用"的请求被稳定判成"没有依据"；
  3. **语料目录不存在时整体不判** —— 容器里没挂语料时所有 stat 都失败，
     照常判定就是"所有引用都坏了"这种纯误报（同 `kb_prune` 的拒绝执行闸门）；
  4. **老引用（没有 `source_path`）跳过而非猜** —— 只有 basename 时同名不同目录
     无法唯一定位，猜错的后果是报出一条假的"源文件已删除"；
  5. **带 `..` / 绝对路径一律跳过** —— 不拿用户可控的路径去 stat 语料目录之外；
  6. **重叠率的分母是答案** —— 反过来算会恰好判反（只抄了引用一句话的答案得低分）。
"""

from __future__ import annotations

import pytest

from src.rag import citation_check as cc


def _cite(n=1, snippet="梧州特产有六堡茶，历史悠久，是广西梧州的传统名茶之一。", **kw):
    d = {"n": n, "source": "a.md", "source_path": "", "snippet": snippet}
    d.update(kw)
    return d


# ===========================================================================
# 1. 引用编号抽取
# ===========================================================================
def test_extracts_bracket_styles():
    assert cc.extract_refs("结论如下[1]。另见【2】与［3］。") == [1, 2, 3]


def test_ignores_plain_parentheses():
    """★ 不认 `(3)`：正文里"第(3)条""如图(2)"太常见。

    认了就是稳定误报，而稳定误报的判据最后一定会被人关掉 ——
    那等于这条防线不存在。
    """
    assert cc.extract_refs("依据第(3)条与图(2)可知。") == []


def test_refs_are_deduped_and_ordered():
    assert cc.extract_refs("[2] 先看这个，再看 [1]，再回 [2]") == [2, 1]


def test_refs_tolerate_weird_input():
    assert cc.extract_refs(None) == []
    assert cc.extract_refs(123) == []
    assert cc.extract_refs("") == []


# ===========================================================================
# 2. 重叠率
# ===========================================================================
def test_overlap_is_one_for_identical_text():
    t = "梧州特产有六堡茶，历史悠久。"
    assert cc.overlap_ratio(t, t) == 1.0


def test_overlap_is_zero_for_unrelated_text():
    assert cc.overlap_ratio("今天天气不错适合出门散步", "六堡茶是梧州特产") == 0.0


def test_overlap_denominator_is_the_answer():
    """★ 分母取**答案**的 gram 数。

    取反（以引用为分母）回答的是另一个问题，而且会**恰好判反**：
    只抄了引用一句话的答案会被算成低分。这里用"答案是引用的真子串"把它钉住 ——
    答案的每个 gram 都在引用里，比率必须是 1.0。

    ⚠️ 答案必须**明显短于**引用。变异验证抓到过一条假绿：上一版用 `ref[:40]` 而
    `ref` 只有 27 字，切片后 `answer == ref` ⇒ 两个分母相等 ⇒ 把分母写反也照样通过。
    """
    ref = "梧州特产有六堡茶，历史悠久，是广西梧州的传统名茶之一，茶香馥郁回甘明显。"
    answer = ref[:15]
    assert len(answer) < len(ref) - 20, "答案必须是引用的真子串，否则这条用例抓不到分母写反"
    assert cc.overlap_ratio(answer, ref) == 1.0


def test_overlap_ignores_whitespace():
    assert cc.overlap_ratio("六堡茶 是\n梧州\t特产", "六堡茶是梧州特产") == 1.0


def test_best_overlap_reports_which_citation():
    """要报出"最高的是第几条" —— 没有编号的话，人还得自己一条条比。"""
    answer = "六堡茶是梧州特产，很有名。"
    best, n, usable = cc.best_overlap(
        answer,
        [_cite(1, "完全无关的另一段内容，用来占位凑够长度。" * 3),
         _cite(2, "六堡茶是梧州特产，很有名，历史悠久。" * 3)],
    )
    assert usable is True
    assert n == 2
    # 0.9 而不是 1.0：答案末尾的句号与引用里的逗号不同 ⇒ 最后一个 gram 不命中。
    # 这个数字本身也在说明这条判据是**启发式**的（换一个标点就掉 10 个点）。
    assert best >= 0.9


def test_best_overlap_flags_unusable_samples():
    """★ 引用太短 ⇒ `usable=False`，**不能**把"没法算"当成 0。

    混成 0.0 会让"只有一条超短引用"的请求被稳定判成低重叠，进而稳定误报。
    """
    best, n, usable = cc.best_overlap("随便写点什么内容", [_cite(1, "太短")])
    assert usable is False
    assert n is None
    assert best == 0.0


# ===========================================================================
# 3. 引用来源是否还在
# ===========================================================================
class _Exists:
    """可注入的 stat 替身：只认集合里的绝对路径，并记录被问过哪些。"""

    def __init__(self, present):
        self.present = set(present)
        self.asked = []

    def __call__(self, path):
        self.asked.append(path)
        return path in self.present


def test_missing_sources_is_not_judged_when_docs_dir_absent(tmp_path):
    """★ 语料目录不存在 ⇒ 整体不判（返回 checked=False）。

    容器里语料没挂进去时，所有引用都会 stat 失败 —— 照常判定就是
    "所有引用都坏了"这种纯误报。分不清"数据真没了"与"根本没看到数据"时不下结论。
    """
    probe = _Exists([])
    got = cc.missing_sources([_cite(source_path="a/b.md")],
                             str(tmp_path / "不存在的目录"), exists=probe)
    assert got["checked"] is False
    assert got["missing"] == []
    assert probe.asked == []            # 连 stat 都不该发出去


def test_missing_sources_reports_absent_file(tmp_path):
    probe = _Exists([])
    got = cc.missing_sources(
        [_cite(1, source_path="a/b.md"), _cite(2, source_path="c.md")],
        str(tmp_path), exists=probe,
    )
    assert got["checked"] is True
    assert sorted(got["missing"]) == ["a/b.md", "c.md"]


def test_missing_sources_passes_present_file(tmp_path):
    probe = _Exists([str(tmp_path / "a" / "b.md")])
    got = cc.missing_sources([_cite(1, source_path="a/b.md")], str(tmp_path),
                             exists=probe)
    assert got["missing"] == []
    assert got["unverifiable"] == 0


def test_missing_sources_skips_citations_without_source_path(tmp_path):
    """★ 老数据（本次改造前灌的库）只有 basename，无法唯一定位 ⇒ **跳过而不是猜**。

    猜错的后果是报出一条假的"源文件已删除"，接着有人去 `kb-prune` 删数据。
    """
    probe = _Exists([])
    got = cc.missing_sources([_cite(1, source_path="")], str(tmp_path), exists=probe)
    assert got["missing"] == []
    assert got["unverifiable"] == 1
    assert probe.asked == []


def test_missing_sources_skips_unsafe_paths(tmp_path):
    """★ 带 `..` 或以 `/` 开头的一律跳过：不拿它去 stat 语料目录之外的地方。"""
    probe = _Exists([])
    got = cc.missing_sources(
        [_cite(1, source_path="../../etc/passwd"),
         _cite(2, source_path="/etc/passwd"),
         _cite(3, source_path="a/../../../x.md")],
        str(tmp_path), exists=probe,
    )
    assert got["missing"] == []
    assert got["skipped_unsafe"] == 3
    assert probe.asked == []


def test_missing_sources_normalizes_backslashes(tmp_path):
    """Windows 上灌库写进来的 `a\\b.md` 必须能对上 Linux 的 `a/b.md`。"""
    probe = _Exists([str(tmp_path / "a" / "b.md")])
    got = cc.missing_sources([_cite(1, source_path="a\\b.md")], str(tmp_path),
                             exists=probe)
    assert got["missing"] == []


# ===========================================================================
# 4. verify 的状态机
# ===========================================================================
def test_no_answer_is_not_judged():
    got = cc.verify("", [_cite()])
    assert got["status"] == "no_answer"
    assert got["ok"] is None
    assert got["block_worthy"] is False


def test_refs_without_any_citation_is_a_hard_error():
    """★ 答案标了编号，但本轮一条命中都没有 ⇒ 编造引用（确凿，可拦）。"""
    got = cc.verify("梧州特产是六堡茶 [1]。", [])
    assert got["status"] == "unsupported"
    assert got["ok"] is False
    assert got["block_worthy"] is True
    assert got["refs_checked"] is True


def test_answer_without_refs_and_without_citations_is_soft():
    """★ 没有引用又没标编号 ⇒ **不**算确凿错误（模型可能就是如实说了"未提及"）。

    这条判据一旦写成 hard，每次"资料里没有"都会被拦 —— 而那是最常见的正常情况。
    """
    got = cc.verify("资料中未提及相关内容。", [])
    assert got["status"] == "no_citations"
    assert got["ok"] is False
    assert got["block_worthy"] is False
    assert got["hard_errors"] == []


def test_out_of_range_ref_is_a_hard_error():
    """★ 引用了不存在的编号 = 编造引用，是唯一能让 block 生效的一类。"""
    got = cc.verify("梧州特产是六堡茶 [7]。", [_cite(1)])
    assert got["status"] == "unsupported"
    assert got["hard_errors"] == ["unknown_refs"]
    assert got["block_worthy"] is True


def test_valid_refs_are_accepted():
    got = cc.verify("六堡茶是梧州特产 [1]。", [_cite(1)])
    assert got["hard_errors"] == []
    assert got["refs"] == [1]


def test_missing_source_degrades_but_never_blocks(tmp_path):
    """★ 源文件不存在是**数据**问题（该由 kb-prune 清孤儿），不拦用户的答案。"""
    got = cc.verify("六堡茶是梧州特产。", [_cite(1, source_path="a/b.md")],
                    docs_dir=str(tmp_path), exists=_Exists([]))
    assert got["status"] == "degraded"
    assert got["ok"] is False
    assert got["block_worthy"] is False
    assert got["missing"] == ["a/b.md"]


def test_low_overlap_is_a_warning_not_a_block():
    """★ 重叠低只是可疑（模型改写幅度大而已），永远不触发拦截。"""
    got = cc.verify("完全无关的一句话写在这里当作答案内容。",
                    [_cite(1, "六堡茶是梧州特产，" * 10)])
    assert got["status"] == "weak"
    assert got["ok"] is False
    assert got["block_worthy"] is False
    assert "low_overlap" in got["warnings"]


def test_verify_is_ok_when_everything_lines_up():
    body = "梧州特产有六堡茶，历史悠久，是广西梧州的传统名茶之一。"
    got = cc.verify("梧州特产有六堡茶 [1]。", [_cite(1, body)])
    assert got["status"] == "ok"
    assert got["ok"] is True
    assert got["detail"]


def test_verify_never_raises_on_weird_input():
    """校验跑在请求路径上，任何输入都只能是"判不出来"，不能是"请求崩了"。"""
    for bad in (None, 123, [], {}, object()):
        got = cc.verify(bad, [bad] if not isinstance(bad, list) else bad)
        assert got["status"]


def test_verdict_ok_maps_unknown_states_to_none():
    """`ok=None`（无从判断）必须与 `False`（有问题）分开：指标里混了就分不出
    "答得可疑"与"没答案"。"""
    assert cc.verdict_ok("ok") is True
    assert cc.verdict_ok("no_answer") is None
    assert cc.verdict_ok("weak") is False
    assert cc.verdict_ok("unsupported") is False


# ===========================================================================
# 5. 引用列表带出 source_path（第 2 层判据的输入）
# ===========================================================================
def test_build_citations_carries_source_path():
    """★ 只给 basename 时无法唯一定位文件 ⇒ "引用来源还在不在"根本判不了。"""
    from src.rag.retrieval import build_citations

    chunk = {"source": "b.md", "source_path": "a/b.md", "content": "正文" * 20}
    cite = build_citations([(chunk, 0.9)])[0]
    assert cite["source_path"] == "a/b.md"


# ===========================================================================
# 6. graph 接线：每轮产出都要判（首轮 + 修订轮）
# ===========================================================================
def _stub_model(monkeypatch, content):
    from unittest.mock import MagicMock

    from langchain_core.messages import AIMessage

    from src import graph as G

    m = MagicMock()
    m.invoke.return_value = AIMessage(content=content)
    monkeypatch.setattr(G, "MODEL", m)
    return G


def test_generate_records_citation_check(monkeypatch):
    """generate 必须把校验结论写进 state —— 否则接口层拿不到，这条链路等于没接。"""
    G = _stub_model(monkeypatch, "梧州特产是六堡茶 [1]。")
    got = G.generate({
        "messages": [], "iteration": 0, "context": "资料",
        "citations": [_cite(1)],
    })
    assert got["citation_check"]["status"] == "ok"


def test_generate_flags_fabricated_ref(monkeypatch):
    """★ 越界编号必须在**产出当时**就被抓到（模型原文里就带着它）。"""
    G = _stub_model(monkeypatch, "梧州特产是六堡茶 [7]。")
    got = G.generate({
        "messages": [], "iteration": 0, "context": "资料",
        "citations": [_cite(1)],
    })
    assert got["citation_check"]["block_worthy"] is True


def test_generate_checks_the_revised_draft_too(monkeypatch):
    """★ 修订轮也要判：新草稿可能引入原文里没有的说法（含编造的引用编号）。"""
    G = _stub_model(monkeypatch, "改完了 [9]。")
    got = G.generate({
        "messages": [], "iteration": 1, "context": "资料", "output": "旧草稿",
        "feedback": "再改改", "feedback_source": "ai",
        "citations": [_cite(1)],
    })
    assert got["citation_check"]["status"] == "unsupported"


# ===========================================================================
# 7. app 层交付策略
# ===========================================================================
def test_withhold_only_on_hard_error_and_block_policy(monkeypatch):
    """★ 两道闸门缺一不可 —— 这是本改动里最容易被写松的一处。

    "拦错了"不会报错，只会让引用区空着，看起来与"本轮没检索到"一模一样。
    """
    from app import config, main

    hard = {"block_worthy": True, "status": "unsupported"}
    soft = {"block_worthy": False, "status": "weak"}

    monkeypatch.setattr(config, "CITATION_CHECK", True)
    monkeypatch.setattr(config, "CITATION_CHECK_ACTION", "warn")
    assert main._should_withhold_citations(hard) is False    # 默认策略：不拦
    monkeypatch.setattr(config, "CITATION_CHECK_ACTION", "block")
    assert main._should_withhold_citations(hard) is True     # 确凿错误 + block ⇒ 拦
    assert main._should_withhold_citations(soft) is False    # ★ 启发式判据永不拦
    monkeypatch.setattr(config, "CITATION_CHECK", False)
    assert main._should_withhold_citations(hard) is False    # 开关关掉 ⇒ 全放行
    assert main._should_withhold_citations({}) is False


def test_public_check_whitelists_fields():
    """只下发白名单字段：内部统计字段不进契约（进了就再也撤不掉）。"""
    from app.main import _public_citation_check

    got = _public_citation_check({
        "status": "ok", "ok": True, "detail": "d", "refs": [1],
        "overlap_usable": True, "sources_unverifiable": 3, "内部字段": "不该出去",
    })
    assert set(got) == set(__import__("app.main", fromlist=["x"])._CITATION_CHECK_FIELDS)
    assert "内部字段" not in got


def test_citation_check_settings_are_echoed_in_startup_config(monkeypatch):
    """安全/行为开关必须进 `as_dict()` —— 不进就**不会出现在启动日志回显**里，
    于是"线上到底有没有在校验引用"只能靠翻代码回答。"""
    from app import config

    conf = config.as_dict()["citation_check"]
    assert "action" in conf and "min_overlap" in conf


@pytest.mark.parametrize("action", ["warn", "block", "WARN", "block "])
def test_action_values_are_normalized(monkeypatch, action):
    """大小写与尾随空格不该改变策略语义（.env 里两种写法都很常见）。"""
    from app import config, main

    monkeypatch.setattr(config, "CITATION_CHECK", True)
    monkeypatch.setattr(config, "CITATION_CHECK_ACTION", action)
    expected = action.strip().lower() == "block"
    assert main._citation_blocked() is expected
