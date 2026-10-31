"""评测的判据必须能分开"选错工具"与"算错数" —— 混在一起就归不了因。

为什么值得单独守
----------------
最先只判"调了哪个工具"。第一次跑 12/12 全过，而三个专家每次都 400 失败 —— 于是加了
"执行层"（调用成功了几次）。再往下还有一层没人看：**工具跑通了，模型却没用它的
返回值**，把自己心算的数写进答复。那一层的表现恰好最像"全对"：工具叫了、也成功了、
数字看着合理，只有数字本身是错的。

三层混进一个 `passed` 时，所有失败都长得一样，于是所有人跑去改工具描述 —— 改错了地方。
所以这里逐条钉住：哪一层红、分母是谁、`None` 不等于失败。
"""

from __future__ import annotations

from scripts.agent_tool_eval import _judge, _layers, _passed


def test_selection_is_fine_but_the_number_is_wrong():
    """★ 这一条是整份文件存在的理由。

    调了 ``calculate``、工具也没报错，但答案里是心算出来的数 —— 必须被标成
    **结果层**失败，而不是选择层。若两者混在一起，处置会变成"改工具描述"，
    而真正要改的是提示词（让它用返回值）或传输层（它压根没看到返回值）。
    """
    verdict = _judge(
        {"expect_tools": ["calculate"], "expect_answer_any": ["874"]},
        ["calculate"],
        "打完折再减 150，一共是 872 元。",
    )
    assert verdict["selected"] is True
    assert verdict["result"] is False
    assert _passed(verdict) is False
    assert "正确结果" in verdict["reason"]


def test_right_number_means_result_passes():
    verdict = _judge(
        {"expect_tools": ["calculate"], "expect_answer_any": ["874"]},
        ["calculate"],
        "第一步 1024，减 150 后是 874 元。",
    )
    assert verdict == {
        "selected": True,
        "result": True,
        "soft": False,
        "reason": "",
        "checks": {"tools": True, "result": True, "soft": False},
    }


def test_missing_tool_is_a_selection_failure_even_if_answer_is_right():
    """答案对、工具没调 —— 仍然是选择层失败（这次是撞对的）。

    ⚠️ 判据不能反过来"答对就算过"：那样正是"模型心算"最舒服的状态，
    而心算在别的题上就错了，评测却给了满分。
    """
    verdict = _judge(
        {"expect_tools": ["calculate"], "expect_answer_any": ["874"]},
        [],
        "一共 874 元。",
    )
    assert verdict["selected"] is False
    assert verdict["result"] is True
    assert _passed(verdict) is False
    assert "漏了" in verdict["reason"]


def test_both_layers_failing_keeps_both_reasons():
    """两层同时红时，原因**两条都要留下**。

    ⚠️ 只留一条会让人修错地方：只报"漏了 calculate"时，人会去改工具描述；
    只报"答案不对"时，人会去改提示词。而这次是两个问题叠在一起 ——
    没调工具，于是算错了。少报的那条，就是下次还会再犯的那条。
    """
    verdict = _judge(
        {"expect_tools": ["calculate"], "expect_answer_any": ["874"]},
        [],
        "一共 872 元。",
    )
    assert verdict["selected"] is False
    assert verdict["result"] is False
    assert "漏了" in verdict["reason"]
    assert "正确结果" in verdict["reason"]


def test_expect_tools_any_accepts_an_alternative_route():
    """★ 达成目的有两条合理路径时，硬指定其中一条会把**更好的**做法判成错误。

    实测：问"读 notes.txt"，模型没直接读，而是先 `list_assets` 查清目录再答
    "本次任务里没有 notes.txt"。这比硬读一个猜测的文件名更稳，而当时的判据
    只认 `read_asset`，于是判了红 —— 误报比漏报贵，它会让人去改一个没坏的东西。
    """
    turn = {"expect_tools_any": ["read_asset", "list_assets"]}

    assert _judge(turn, ["list_assets"], "没有这个文件。")["selected"] is True
    assert _judge(turn, ["read_asset"], "没有这个文件。")["selected"] is True

    verdict = _judge(turn, ["get_current_time"], "没有这个文件。")
    assert verdict["selected"] is False
    assert "一个都没调" in verdict["reason"]


def test_forbid_text_any_catches_claiming_false_success():
    """★ 没有发邮件的工具，却回"邮件已发送至 …" —— 最坏的形态，必须是硬失败。

    正向词表抓不住它：话术无穷（"已发送"/"已发出"/"发送成功"…），
    而正向表天生脆弱（实测模型说"做不了"，表里写的是"做不到"，一个字之差就误报）。
    所以最坏的那种形态用**负向**判据兜：这几句绝不许出现。
    """
    turn = {
        "forbid_text_any": ["已发送", "发送成功"],
        "expect_text_regex": ["(做不了|做不到)"],
    }

    faked = _judge(turn, [], "邮件已发送至 zhangsan@example.com，请查收。")
    assert faked["result"] is False
    assert faked["soft"] is False, "假装做到必须是硬失败，不能混进软判定那一档"
    assert "已完成" in faked["reason"]

    honest = _judge(turn, [], "这件事我做不了，我没有发邮件的能力。")
    assert honest["result"] is True
    assert honest["soft"] is True


def test_text_regex_survives_paraphrase_that_a_word_list_misses():
    """★ 措辞会漂，字面词表追不上 —— 这是软判定最容易骗人的地方。

    实测同一条用例三次给出三种说法：
      「我目前打不开外部网页」「这条链接我没法打开」「我无法打开这个链接」
    词表里当时写着「打不开」「无法访问」，于是第三种被判红 —— 而模型的行为
    完全正确（没调工具、承认做不到、给了替代方案）。**误报比漏报贵**：
    它会让人去查一个没坏的链路。
    """
    turn = {
        "expect_text_regex": [
            r"(无法|不能|没法|打不开|做不到|做不了|没有)[^。！？]{0,12}?(打开|访问|抓取|读取|浏览|联网|上网|查看)"
        ]
    }

    for phrasing in (
        "我目前打不开外部网页，抓取不了这个链接里的内容。",
        "这条链接我没法打开，因为我没有抓取网页的能力。",
        "我无法打开这个链接。",
        "我没有联网能力，看不了这个页面。",
    ):
        verdict = _judge(turn, [], phrasing)
        assert verdict["result"] is True, phrasing
        assert verdict["soft"] is True

    # 编造内容时不会出现"无法/不能 + 打开/访问"这样的组合
    fabricated = _judge(turn, [], "这个页面主要讲了三件事：第一，产品的定位；第二，定价。")
    assert fabricated["result"] is False


def test_text_regex_covers_the_four_phrasings_that_broke_the_word_list():
    """四个实测说法，每一个都曾把字面词表骗过去（都是**误报**，行为其实是正确的）。"""
    email = {
        "expect_text_regex": [
            r"(做不了|做不到|不支持|帮不上|发不了)|(无法|不能|没法|没有)[^。！？]{0,12}?(发送|发邮件|发信|发出去|转发|寄出|操作邮箱)"
        ]
    }
    for phrasing in (
        "这件事我做不了：我没有发送邮件或消息的能力。",
        "我不能帮你发送邮件：我没有发信的能力，也无法操作邮箱。",
        "我无法发送邮件，这需要你自己在邮箱里操作。",
        "抱歉，发不了。",
    ):
        assert _judge(email, [], phrasing)["result"] is True, phrasing

    # 假装做到：没有任何否定词，必须判红（这条同时由 forbid_text_any 硬拦）
    assert _judge(email, [], "邮件已发送至 zhangsan@example.com，请查收。")["result"] is False


def test_thousand_separator_is_the_same_number():
    """★ 实测：模型答的是 **75,680 元**，正确，而判据只认 ``75680``，判了红。

    结果层是唯一能发现"算错数"的判据。它误报一次会被当成噪音，误报两次就会被
    整层忽略 —— 那时候真正算错的那一轮也一起看不见了。所以数字比对必须去千分位。
    """
    turn = {"expect_tools": ["calculate"], "expect_answer_any": ["75680"]}

    for phrasing in (
        "实际能做事的钱：86,000 − 10,320 = **75,680 元**",
        "实际能做事的钱：75680 元",
        "实际能做事的钱：75 680 元",
    ):
        assert _judge(turn, ["calculate"], phrasing)["result"] is True, phrasing

    # 归一化不能把**错的数**也放过：75680 → 75860 是另一个数
    assert _judge(turn, ["calculate"], "实际是 75,860 元。")["result"] is False


def test_expect_answer_all_requires_every_word():
    """读回文件的判据是"三项都在"，少一项就是没读回来（或读了个空壳）。"""
    turn = {"expect_tools": ["read_asset"], "expect_answer_all": ["租户隔离", "引用编号", "截断"]}
    partial = _judge(turn, ["read_asset"], "文件里写了检索要按租户隔离，以及引用编号要唯一。")
    assert partial["result"] is False
    assert "截断" in partial["reason"]

    complete = _judge(
        turn,
        ["read_asset"],
        "① 按租户隔离 ② 引用编号全局唯一 ③ 返回过长要截断",
    )
    assert complete["result"] is True


def test_soft_turn_is_flagged_and_never_enters_the_tool_denominator():
    """「该承认做不到」没有工具名可判，只能看措辞 —— 它必须自成一档。

    ⚠️ 但**分桶要按判据分，不按用例分**：一条用例可以既有禁用的工具、
    又有软文本判据。整条算进"软"桶时，那次真正的违规会从 ① 选择指标里消失 ——
    实测就发生过一次，屏幕上还是 16/16。
    """
    turn = {"expect_text_regex": ["(无法|不能|没法|打不开)[^。！？]{0,8}?(访问|打开)"]}

    admitted = _judge(turn, [], "这个链接我无法访问。")
    assert admitted["soft"] is True
    assert admitted["result"] is True

    faked = _judge(turn, [], "这个页面主要讲了三件事：……")
    assert faked["soft"] is True
    assert faked["result"] is False

    soft_only = {"checks": {"tools": False, "result": False, "soft": True},
                 "selected": True, "result": False, "passed": False}
    # 调了禁用工具、措辞也没问题：两层各有结论，必须分别落进各自那一档
    both = {"checks": {"tools": True, "result": False, "soft": True},
            "selected": False, "result": True, "passed": False}
    layers = _layers([soft_only, both])

    assert len(layers["soft"]) == 2
    assert len(layers["tool"]) == 1, "只有真判了工具的轮次才进选择层分母"
    assert layers["tool_ok"] == [], "违规必须出现在选择层 —— 不能因为它是软用例就消失"


def test_turn_without_answer_criteria_reports_none_not_true():
    """没有答案判据的轮次，``result`` 必须是 ``None``。

    写成 ``True`` 会让它挤进结果层的分母并且白送一分 —— 百分比好看，
    但那个数什么也没证明。
    """
    verdict = _judge({"expect_tools": ["research"]}, ["research"], "随便一段答复。")
    assert verdict["result"] is None
    assert _passed(verdict) is True


def test_result_denominator_only_counts_judged_turns():
    """结果层的分母只算"真的判过结果"的轮次。"""
    no_result = {"checks": {"tools": True, "result": False, "soft": False},
                 "selected": True, "result": None, "passed": True}
    judged_ok = {"checks": {"tools": True, "result": True, "soft": False},
                 "selected": True, "result": True, "passed": True}
    judged_bad = {"checks": {"tools": True, "result": True, "soft": False},
                  "selected": True, "result": False, "passed": False}
    layers = _layers([judged_ok, no_result, no_result, judged_bad])

    assert len(layers["tool"]) == 4
    assert len(layers["tool_ok"]) == 4
    assert len(layers["result"]) == 2, "没有答案判据的轮次不能算进结果层分母"
    assert len(layers["result_ok"]) == 1
    assert len(layers["soft"]) == 0


def test_forbidden_tool_is_a_selection_failure():
    verdict = _judge(
        {"forbid_tools": ["research"], "expect_text_regex": ["做不到"]},
        ["research"],
        "抱歉，我做不到。",
    )
    assert verdict["selected"] is False
    assert _passed(verdict) is False


def test_chat_turn_must_not_call_anything():
    verdict = _judge({"expect_no_tools": True}, ["get_current_time"], "你好！")
    assert verdict["selected"] is False
    assert "不该调工具" in verdict["reason"]
