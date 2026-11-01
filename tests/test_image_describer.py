"""图片 VLM 描述层测试（批次 6）：**全程不联网**。

为什么坚持不联网：这条链路的失败模式大多是"没调成功但看起来调了"——
开关没生效、图没落盘、格式不支持、超了上限。这些必须能在离线单测里锁住，
否则每次验证都要花钱，出问题也没法复现。

真正的 API 调用由 scripts/check_vlm.py 单独做（那才是它的职责）。
"""
import json

import pytest

from src.rag import image_describer as D
from src.rag.knowledge_chunk import KIND_DIAGRAM


def _enable_vlm(monkeypatch, model: str = "vision-model") -> None:
    """把三项 ``VLM_*`` 配齐 —— 缺任一项 ``enabled()`` 都会拒绝。

    为什么不复用一个"VLM 已启用"的 fixture：这几条用例要验的正是**开关与统计**，
    把配置藏进 fixture 会让"到底开了什么"看不出来。所以显式写一行。
    """
    monkeypatch.setattr(D, "VLM_API_KEY", "sk-x")
    monkeypatch.setattr(D, "VLM_API_BASE", "https://vlm.example/v1")
    monkeypatch.setattr(D, "VLM_MODEL", model)


@pytest.fixture
def png(tmp_path):
    """一张真位图（VLM 读得了的那种）。"""
    from PIL import Image
    p = tmp_path / "fig.png"
    Image.new("RGB", (200, 120), "white").save(p)
    return str(p)


@pytest.fixture
def emf(tmp_path):
    """矢量图（EMF）：VLM 读不了，必须走显式标记而不是当"调用失败"。"""
    p = tmp_path / "fig.emf"
    p.write_bytes(b"\x01\x00\x00\x00" + b"\x00" * 32)
    return str(p)


def _chunk(**kw):
    from src.rag.knowledge_chunk import KnowledgeChunk
    kw.setdefault("kind", KIND_DIAGRAM)
    kw.setdefault("source", "a.pdf")
    kw.setdefault("needs_vlm", True)
    return KnowledgeChunk(**kw)


def _fake_result(desc="图是一张柱状图，显示 2021 年 138 万元。", **extra):
    """模拟一次成功的 VLM 输出（与真实 schema 同构）。"""
    data = {"chart_type": "柱状图", "title": "", "visible_text": "138 2021",
            "axes": {"x": "年份", "y": "金额（万元）"}, "legend": ["销售额"],
            "key_data": ["2021年138万元"], "trend": "上升", "description": desc}
    data.update(extra)
    return data


# ---------------------------------------------------------------------------
# 一、开关：默认关闭，且"没开"要能被看见
# ---------------------------------------------------------------------------
def test_disabled_by_default(monkeypatch):
    monkeypatch.setattr(D, "VLM_DESC", "off")
    ok, why = D.enabled()
    assert ok is False and "off" in why


def test_auto_requires_all_three_vlm_values(monkeypatch):
    """★ 三项缺**任一**项都必须拒绝启动。

    2026-11-01 起 VLM 不再复用 ``LLM_*``（主网关换成 DeepSeek，它没有视觉模型），
    所以旧口径"配了 key 就算开"必须一起改：只配 key 会让 base 为空，SDK 拼出畸形
    URL，而报错和"模型不支持图片输入"完全不像 —— 排查的人会去改模型名。
    """
    monkeypatch.setattr(D, "VLM_DESC", "auto")
    _enable_vlm(monkeypatch)
    assert D.enabled()[0] is True

    for missing in ("VLM_API_KEY", "VLM_API_BASE", "VLM_MODEL"):
        monkeypatch.setattr(D, missing, "")
        ok, why = D.enabled()
        assert ok is False, f"缺 {missing} 时不该启用"
        assert missing in why, f"原因要点名缺的那一项：{why}"
        monkeypatch.setattr(D, missing, "x")


def test_on_reports_each_missing_item_instead_of_silently_doing_nothing(monkeypatch):
    """RAG_VLM_DESC=on 却缺配置时，原因必须点名缺哪几项 ——
    "以为在跑其实没跑"是这类批处理最贵的坑，而三项分属不同服务，猜错要白跑一轮。"""
    monkeypatch.setattr(D, "VLM_DESC", "on")
    monkeypatch.setattr(D, "VLM_API_KEY", "")
    monkeypatch.setattr(D, "VLM_API_BASE", "")
    monkeypatch.setattr(D, "VLM_MODEL", "")
    ok, why = D.enabled()
    assert ok is False
    for name in ("VLM_API_KEY", "VLM_API_BASE", "VLM_MODEL"):
        assert name in why, why


# ---------------------------------------------------------------------------
# 二、请求构造：图 + 前后文都要进 prompt
# ---------------------------------------------------------------------------
def test_messages_carry_image_and_context():
    msgs = D.build_messages(b"\x89PNG-fake", "image/png", {
        "section_path": "第三章 > 出口情况", "preceding_text": "如图 3 所示，出口持续增长。",
        "following_text": "综上，机电产品是主力。", "caption": "图3 出口额变化",
        "predicted_class": "bar_chart"})
    assert len(msgs) == 1 and msgs[0]["role"] == "user"
    parts = msgs[0]["content"]
    assert parts[0]["type"] == "text" and parts[1]["type"] == "image_url"
    prompt = parts[0]["text"]
    for frag in ("第三章 > 出口情况", "如图 3 所示", "综上", "图3 出口额变化", "bar_chart"):
        assert frag in prompt, f"上下文 {frag!r} 没进 prompt"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_long_context_is_clipped():
    """前后文各截断 200 字：给模型定主题用，多了只是白花 token。"""
    msgs = D.build_messages(b"x", "image/png", {"preceding_text": "很" * 500})
    prompt = msgs[0]["content"][0]["text"]
    assert "很" * 200 in prompt and "很" * 250 not in prompt


# ---------------------------------------------------------------------------
# 三、输出解析与校验：模型会"编"，必须能发现
# ---------------------------------------------------------------------------
def test_parse_response_accepts_fenced_json():
    raw = '好的，结果如下：\n```json\n{"description": "图注"}\n```\n希望有帮助。'
    assert D.parse_response(raw)["description"] == "图注"


def test_parse_response_reports_error_instead_of_raising():
    out = D.parse_response("这不是 JSON")
    assert out["error"] and "raw" in out


def test_validate_flags_numbers_not_visible_in_image():
    """核心防幻觉：总结里的数字必须能在 visible_text 里找到。

    这是最典型的生成式失败——把 1,280,000 写成 1,280,00 且看上去完全正常。
    """
    desc = _fake_result(desc="2021 年销售额约 999 万元。")
    issues = D.validate(desc)
    assert issues and "999" in issues[0]


def test_validate_passes_when_numbers_come_from_transcription():
    assert D.validate(_fake_result()) == []


def test_validate_normalizes_thousand_separators():
    desc = {"visible_text": "1,800", "description": "约 1800 亿元", "key_data": []}
    assert D.validate(desc) == []


def test_validate_flags_fabrication_when_nothing_visible():
    desc = {"visible_text": "", "title": "某图", "description": "x", "key_data": []}
    assert any("编造" in i for i in D.validate(desc))


def test_validate_removes_all_empty_axes():
    """空壳结构比没有结构更危险：下游会以为"这块信息已提取"。"""
    desc = {"visible_text": "x", "axes": {"x": "", "y": ""}, "description": "", "key_data": []}
    D.validate(desc)
    assert "axes" not in desc


# ---------------------------------------------------------------------------
# 四、describe：缓存、错误、格式
# ---------------------------------------------------------------------------
def test_describe_calls_once_then_hits_cache(png, tmp_path, monkeypatch):
    monkeypatch.setattr(D, "VLM_CACHE_DIR", str(tmp_path / "cache"))
    calls = []

    def fake_call(messages):
        calls.append(messages)
        return json.dumps(_fake_result()), {"total_tokens": 1234}

    first = D.describe(png, {}, call=fake_call)
    assert first["description"] and first["cached"] is False
    assert first["usage"]["total_tokens"] == 1234

    second = D.describe(png, {}, call=fake_call)
    assert second["cached"] is True
    assert len(calls) == 1, "第二张相同的图不该再花钱"


def test_describe_survives_api_failure(png):
    def boom(messages):
        raise RuntimeError("502 Bad Gateway")

    out = D.describe(png, {}, call=boom)
    assert "502" in out["error"] and "description" not in out


def test_describe_survives_model_returning_garbage(png):
    out = D.describe(png, {}, call=lambda m: ("嗯……我看不清", {}))
    assert out["error"]


def test_describe_rejects_vector_format(emf):
    """矢量图不是"调用失败"，是**根本读不了** —— 两种原因的处置方式不同。"""
    out = D.describe(emf, {}, call=lambda m: ("", {}))
    assert "矢量" in out["error"]


def test_describe_handles_missing_file(tmp_path):
    out = D.describe(str(tmp_path / "nope.png"), {}, call=lambda m: ("", {}))
    assert "失败" in out["error"]


def test_prepare_image_downscales_long_side(tmp_path, monkeypatch):
    """长边超限要等比缩放：token 才可预期（方案：长边 1600~2000px）。"""
    from PIL import Image
    monkeypatch.setattr(D, "MAX_SIDE", 100)
    p = tmp_path / "big.png"
    Image.new("RGB", (800, 400), "white").save(p)
    data, mime = D.prepare_image(str(p))
    assert mime == "image/png"
    with Image.open(__import__("io").BytesIO(data)) as im:
        assert max(im.size) == 100 and im.size[0] > im.size[1]


# ---------------------------------------------------------------------------
# 五、enrich：接入产物层的行为
# ---------------------------------------------------------------------------
def test_enrich_disabled_still_counts_candidates(monkeypatch):
    monkeypatch.setattr(D, "VLM_DESC", "off")
    chunks = [_chunk(source_image="x.png")]
    stats = D.enrich(chunks)
    assert stats["skipped_disabled"] == 1 and stats["described"] == 0
    assert chunks[0].needs_vlm is True          # 关着就不该改动它的状态


def test_enrich_writes_description_without_overwriting_caption(png, monkeypatch):
    monkeypatch.setattr(D, "VLM_DESC", "auto")
    _enable_vlm(monkeypatch)
    c = _chunk(source_image=png, content="图3 出口额变化",
               retrieval_text="第三章 > 出口情况；图3 出口额变化")
    stats = D.enrich([c], describer=lambda path, ctx: _fake_result())
    assert stats["described"] == 1
    assert c.needs_vlm is False and "VLM描述" in c.tags
    assert c.vlm_desc["chart_type"] == "柱状图"
    # 作者写的图注必须在模型描述**之前**，且没被顶掉
    assert c.retrieval_text.startswith("第三章 > 出口情况；图3 出口额变化")
    assert "柱状图" in c.retrieval_text


def test_enrich_marks_unverified_with_tag(png, monkeypatch):
    monkeypatch.setattr(D, "VLM_DESC", "auto")
    _enable_vlm(monkeypatch)
    c = _chunk(source_image=png)
    D.enrich([c], describer=lambda path, ctx: _fake_result(desc="约 999 万元"))
    assert "VLM待核对" in c.tags


def test_enrich_respects_max_images_and_says_so(png, monkeypatch):
    monkeypatch.setattr(D, "VLM_DESC", "auto")
    _enable_vlm(monkeypatch)
    chunks = [_chunk(source_image=png, source="a.pdf") for _ in range(3)]
    stats = D.enrich(chunks, describer=lambda p, c: _fake_result(), max_images=1)
    assert stats["described"] == 1 and stats["skipped_limit"] == 2
    assert sum(1 for c in chunks if any("超出本次上限" in t for t in c.tags)) == 2


def test_enrich_keeps_needs_vlm_for_unsendable_images(emf, tmp_path, monkeypatch):
    """无图可取 / 矢量图 / 文件丢失：必须留下**可筛选**的标记，
    否则"这张图其实没被描述"永远查不出来。"""
    monkeypatch.setattr(D, "VLM_DESC", "auto")
    _enable_vlm(monkeypatch)
    chunks = [_chunk(source_image=""), _chunk(source_image=emf),
              _chunk(source_image=str(tmp_path / "gone.png"))]
    stats = D.enrich(chunks, describer=lambda p, c: pytest.fail("不该被调用"))
    assert stats["described"] == 0
    assert stats["skipped_no_image"] == 2 and stats["skipped_vector"] == 1
    assert all(c.needs_vlm for c in chunks)
    tags = [t for c in chunks for t in c.tags]
    assert any("矢量图" in t for t in tags)


def test_enrich_counts_failures(png, monkeypatch):
    monkeypatch.setattr(D, "VLM_DESC", "auto")
    _enable_vlm(monkeypatch)
    c = _chunk(source_image=png)
    stats = D.enrich([c], describer=lambda p, ctx: {"error": "限流"})
    assert stats["failed"] == 1 and c.needs_vlm is True
    assert any("限流" in t for t in c.tags)


# ---------------------------------------------------------------------------
# 六、上下文装配：只在同一份文档内取前后文
# ---------------------------------------------------------------------------
def test_context_for_uses_neighbours_within_same_source():
    chunks = [
        _chunk(source="a.pdf", retrieval_text="图前的说明", needs_vlm=False),
        _chunk(source="a.pdf", source_image="x.png"),
        _chunk(source="a.pdf", retrieval_text="图后的结论", needs_vlm=False),
    ]
    ctx = D.context_for(chunks, 1)
    assert ctx["preceding_text"] == "图前的说明"
    assert ctx["following_text"] == "图后的结论"


def test_context_for_never_crosses_documents():
    """跨文档取上下文比不给还糟：会把**别的文档**的正文当成这张图的语境。"""
    chunks = [
        _chunk(source="b.pdf", retrieval_text="另一份文档的正文", needs_vlm=False),
        _chunk(source="a.pdf", source_image="x.png"),
    ]
    ctx = D.context_for(chunks, 1)
    assert ctx["preceding_text"] == "" and ctx["following_text"] == ""


def test_context_for_skips_other_needs_vlm_blocks():
    """别拿"另一张还没描述的图"当前文 —— 那是空壳，会把噪声喂给模型。"""
    chunks = [
        _chunk(source="a.pdf", retrieval_text="有内容的正文", needs_vlm=False),
        _chunk(source="a.pdf", source_image="y.png", retrieval_text="图片（无替代文字）"),
        _chunk(source="a.pdf", source_image="x.png"),
    ]
    assert D.context_for(chunks, 2)["preceding_text"] == "有内容的正文"


# ---------------------------------------------------------------------------
# 七、算量（开开关之前必须先看这个）
# ---------------------------------------------------------------------------
def test_estimate_breaks_down_why_images_are_not_sendable(png, emf, tmp_path):
    chunks = [_chunk(source_image=png), _chunk(source_image=emf),
              _chunk(source_image=""), _chunk(source_image=str(tmp_path / "gone.png")),
              _chunk(source_image=png, needs_vlm=False)]
    est = D.estimate(chunks)
    assert est["needs_vlm"] == 4 and est["sendable"] == 1
    assert est["by_reason"]["矢量图（VLM 读不了，需先转位图）"] == 1
    assert est["by_reason"]["无图可取（source_image 为空）"] == 1
    assert est["est_input_tokens"] > 0
    assert "可送 1 张" in D.format_estimate(est)


def test_estimate_counts_nothing_when_all_have_captions(png):
    est = D.estimate([_chunk(source_image=png, needs_vlm=False)])
    assert est["needs_vlm"] == 0 and est["est_input_tokens"] == 0
