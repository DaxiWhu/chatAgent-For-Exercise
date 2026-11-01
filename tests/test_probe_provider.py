"""探针脚本的判定逻辑必须能分开"凭证坏了"与"网关不支持" —— 混了就会误判方向。

为什么值得单独守
----------------
这个脚本存在的意义是替人做一个决定：``AGENT_STRUCTURED_OUTPUT`` 该写哪一档。
它一旦把 401 记成"这一档不支持"，结论会变成"三档全废、该换网关"—— 而真实问题
只是 key 过期。方向错了，后面每一步都白做。

2026-10-31 还踩过一个更隐蔽的：探 ``json_object`` 收到 400，原因是提示词里没有
"JSON" 字样（那是该档的硬要求），于是差点把"我的探针写错了"读成"网关不支持"。
所以提示词里那个 "JSON" 本身也要被守住。
"""

from __future__ import annotations

import scripts.probe_provider as P


def test_status_is_not_a_boolean():
    """★ 这一条是整份文件存在的理由：401 不是"这一档不支持"。

    四类对应四种动作：换 key / 降档 / 查网络 / 别处有问题。合成布尔值就把
    "下一步做什么"丢了。
    """
    assert P.classify(200) == "ok"
    assert P.classify(401) == "bad_key"
    assert P.classify(403) == "bad_key"      # 权限不足与 key 失效处置相同：换凭证
    assert P.classify(400) == "rejected"     # 网关明确拒绝：可以降档
    assert P.classify(404) == "rejected"     # 地址/模型名不对，同属"被拒"，看响应体
    assert P.classify(0) == "unreachable"    # 根本没发出去，与档位无关


def test_support_ladder_is_monotonic():
    """档位是单调的：支持 json_schema 就该用默认档，而不是"更保险的" json_object。

    这条反着断言很重要 —— 若函数写成"总是建议 json_object"，上面两个断言都会红，
    而不是碰巧都对。
    """
    mode, reason = P.decide_mode({"json_schema": True, "json_object": True, "none": True})
    assert mode is None, "支持 json_schema 时不该改配置"
    assert "默认档" in reason

    mode, _ = P.decide_mode({"json_schema": False, "json_object": True, "none": True})
    assert mode == "json_object"

    mode, _ = P.decide_mode({"json_schema": False, "json_object": False, "none": True})
    assert mode == "none"


def test_untested_is_not_rejected():
    """``None``（没测到）与 ``False``（被拒）必须分流。

    全 None 时不能建议任何档位 —— 提示"先修通"才是对的；把它当 False 会得出
    "三档全废"的假结论。
    """
    mode, reason = P.decide_mode({"json_schema": None, "json_object": None, "none": None})
    assert mode is None
    assert "没测到" in reason

    # 混入：json_schema 明确拒绝、其余没测到 —— 仍然不能定档
    mode, reason = P.decide_mode({"json_schema": False, "json_object": None, "none": None})
    assert mode is None
    assert "没测到" in reason

    # 三档都被明确拒绝：这是别的问题（模型名/地址），不能悄悄给个 none 了事
    mode, reason = P.decide_mode({"json_schema": False, "json_object": False, "none": False})
    assert mode is None
    assert "全被拒" in reason


def test_probe_prompt_mentions_json():
    """★ 防假阴性：``response_format=json_object`` 硬要求提示词里出现 "JSON"。

    少了它，这一档会返回 400，而 400 会被 ``classify`` 记成 ``rejected`` ——
    于是"探针自己写错了"被读成"网关不支持"。这个坑真实发生过。
    """
    assert "JSON" in P._PROBE_PROMPT


def test_payload_only_sends_response_format_when_asked():
    """``none`` 档必须**真的**不带 ``response_format``，否则它验的不是 none 档。"""
    assert "response_format" not in P.build_payload("none", "m")

    obj = P.build_payload("json_object", "m")
    assert obj["response_format"] == {"type": "json_object"}

    schema = P.build_payload("json_schema", "m")
    assert schema["response_format"]["type"] == "json_schema"
    assert schema["response_format"]["json_schema"]["strict"] is True
    assert schema["response_format"]["json_schema"]["schema"]["required"] == ["ok"]


def test_payload_holds_model_and_keeps_it_short():
    """探针只关心 HTTP 状态：模型名要透传，输出上限要小（省钱且不跑偏）。"""
    body = P.build_payload("json_object", "some-model")
    assert body["model"] == "some-model"
    assert body["max_tokens"] <= 64


def test_endpoint_tolerates_both_base_styles():
    """.env 里两家 base 写法不同（agnes 带 /v1、deepseek 不带），两种都合法。"""
    assert P._endpoint("https://a.example/v1") == "https://a.example/v1/chat/completions"
    assert P._endpoint("https://a.example/v1/") == "https://a.example/v1/chat/completions"
    assert P._endpoint("https://a.example") == "https://a.example/chat/completions"
    # 已经写全的不重复追加
    full = "https://a.example/v1/chat/completions"
    assert P._endpoint(full) == full
    assert P._endpoint("") == ""


def test_bad_key_marks_all_modes_untested(monkeypatch):
    """★ 端到端：key 401 时三档必须全是 ``None``，不能是 ``False``。

    这是把 ``classify`` 的分流真正接进主流程的守卫 —— 只测 ``classify`` 不够，
    ``probe`` 里写成 ``kind == "ok"`` 一样能把 None 变成 False。
    """
    calls: list[str] = []

    def fake_post(url, key, body, timeout, *, use_proxy):  # noqa: ARG001
        calls.append(body.get("response_format", {}).get("type", "none"))
        return 401, '{"error":{"message":"Invalid token"}}'

    monkeypatch.setattr(P, "_post", fake_post)
    row = {"name": "x", "key": "k", "base": "https://a.example/v1", "model": "m"}
    result = P.probe(row, timeout=1.0, use_proxy=False)

    assert result["key_status"] == "bad_key"
    assert result["modes"] == {"json_schema": None, "json_object": None, "none": None}
    # key 无效时**只该发一次**请求，不要再去逐档试探（省时间也不给网关添错）
    assert calls == ["none"]


def test_healthy_key_measures_every_mode(monkeypatch):
    """key 有效时才逐档探，且每档的响应要留证（否则对照表没有依据）。"""
    def fake_post(url, key, body, timeout, *, use_proxy):  # noqa: ARG001
        rf = body.get("response_format") or {}
        if rf.get("type") == "json_schema":
            return 400, "This response_format type is unavailable now"
        return 200, '{"choices":[]}'

    monkeypatch.setattr(P, "_post", fake_post)
    row = {"name": "x", "key": "k", "base": "https://a.example", "model": "m"}
    result = P.probe(row, timeout=1.0, use_proxy=False)

    assert result["key_status"] == "ok"
    assert result["modes"] == {"json_schema": False, "json_object": True, "none": True}
    assert "unavailable" in result["detail"]["json_schema"]
    assert result["detail"]["none"].startswith("HTTP 200"), "none 档也要留证"
    assert P.decide_mode(result["modes"])[0] == "json_object"


def test_rows_skip_providers_without_key(monkeypatch):
    """没配 key 的网关不出现在表里 —— 表里每一行都该有可执行的结论。"""
    monkeypatch.setenv("AGNES_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    names = [row["name"] for row in P._provider_rows("")]
    assert names == ["deepseek"]
    assert P._provider_rows("agnes") == []
