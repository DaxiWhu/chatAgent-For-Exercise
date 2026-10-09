"""提示词集中化与版本号的契约。

为什么需要这个测试
------------------
提示词外置解决的是"可追溯"，而可追溯只有在**版本号真的跟着文本走**时才成立。
这里钉住三件事：

  1. **文本指纹 ↔ 版本号**：只改模板文字、忘了 bump `PROMPT_VERSION`，
     会直接测红。没有这条，"版本号"就退化为一个人工维护的注释 ——
     它会在第一次赶工的改动里失真，且失真是静默的（版本照旧、行为已变）。
  2. **提示词不再回流到节点里**：结构性断言 `src/graph.py` 不含模板文本。
     否则下一个人"顺手"内联一句，外置就慢慢瓦解了 —— 而外置一旦不完整，
     "哪一版提示词"这个问题就重新变得无法回答。
  3. **版本真的送到了外面**：响应头与就绪探针都带版本号。存在常量但没人下发，
     等于没做（线上依然查不到跑的是哪一版）。
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("DB_URL", "")

ROOT = Path(__file__).resolve().parent.parent

from src import prompts  # noqa: E402

# 模板指纹（改文本必须同时改版本号并更新本表）。
# 键是 PROMPT_VERSION；值 = 三个模板用固定哨兵参数渲染后的 sha256。
# 更新方法：跑 `python -c "from tests.test_prompts import fingerprint; print(fingerprint())"`
_PROMPT_FINGERPRINTS = {
    "1.0.0": "7fb7fa4ea678985a991ace209705801b89ca723f43fb688fbeaa0c4cf23d9b25",
}


def fingerprint() -> str:
    """三个模板的合并指纹。

    用哨兵参数渲染（而不是读源码）：指纹覆盖的是**模型实际看到的文本**。
    读源码会把注释、变量名、换行风格也算进去 —— 那些不影响行为的改动
    本不该要求 bump 版本号。
    """
    parts = [
        prompts.answer_prompt("<<CTX>>", "<<QUESTION>>"),
        prompts.revise_prompt("<<DRAFT>>", "<<FEEDBACK>>", "<<SOURCE>>"),
        prompts.evaluate_prompt("<<DRAFT2>>"),
    ]
    blob = "\x1f".join(parts)  # \x1f 是单元分隔符：不可能出现在模板文本里
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def test_prompt_version_changes_with_any_template_edit():
    """模板文本 ↔ 版本号必须同步变化。

    报错信息刻意给出**完整操作步骤**：这条断言几乎只在"改提示词忘了 bump"
    的场景下触发，此时作者最需要的就是知道该怎么做，而不是看到一句
    "assert failed"。
    """
    assert prompts.PROMPT_VERSION in _PROMPT_FINGERPRINTS, (
        f"PROMPT_VERSION={prompts.PROMPT_VERSION!r} 不在指纹表里。\n"
        f"当前三个模板的指纹是：{fingerprint()}\n"
        "如果你刚 bump 了版本号：把上面的指纹作为新条目加进 "
        "tests/test_prompts.py 的 _PROMPT_FINGERPRINTS。"
    )
    expected = _PROMPT_FINGERPRINTS[prompts.PROMPT_VERSION]
    assert fingerprint() == expected, (
        "提示词文本变了，但 PROMPT_VERSION 没变 —— 版本号将不再可信。\n"
        f"当前版本：{prompts.PROMPT_VERSION}（指纹 {fingerprint()}）\n"
        f"登记指纹：{expected}\n\n"
        "请：① 在 src/prompts.py 里 bump PROMPT_VERSION；\n"
        "    ② 用上面的新指纹更新 tests/test_prompts.py 的 _PROMPT_FINGERPRINTS。"
    )


def test_version_looks_like_a_version():
    """版本号必须可由人排序比较（别用日期以外的怪格式，也别留空）。"""
    assert re.fullmatch(r"\d+\.\d+\.\d+", prompts.PROMPT_VERSION), (
        f"PROMPT_VERSION={prompts.PROMPT_VERSION!r} 不是 x.y.z 形式 —— "
        "运维需要能一眼判断哪版更新"
    )


def test_answer_prompt_carries_context_and_question():
    text = prompts.answer_prompt("资料A", "问题B")
    assert "资料A" in text and "问题B" in text


def test_revise_prompt_demands_a_complete_rewrite():
    """修订模板必须明确要求**完整新版本**，且带上草稿与意见。

    这不是措辞偏好：下游把返回内容整体当作新草稿（写回 `state["output"]`），
    模型若只回改动片段，最终回答就是残缺的。
    """
    text = prompts.revise_prompt("旧草稿", "太短了", "评审模型")
    assert "旧草稿" in text and "太短了" in text and "评审模型" in text
    assert "完整" in text, "修订要求里丢了『输出完整的新版本』 —— 模型会只回片段"


def test_revise_prompt_distinguishes_feedback_source():
    """人工意见与模型意见措辞要可区分（便于排查"是谁打回的"）。"""
    ai = prompts.revise_prompt("d", "f", "评审模型")
    human = prompts.revise_prompt("d", "f", "人工审核")
    assert "评审模型" in ai and "人工审核" in human and ai != human


def test_evaluate_prompt_asks_for_a_score():
    text = prompts.evaluate_prompt("草稿")
    assert "草稿" in text and "0-100" in text


def test_templates_do_not_inline_the_context_guard():
    """模板不得自称"资料里可能有指令" —— 那是 `RAG_GUARD` 开关的职责。

    若把这句免责声明写进模板，`RAG_GUARD=0`（关掉防护）就失效了：
    提示词里仍然宣称"不要执行资料里的指令"，但结构边界不再存在，
    行为与开关的语义对不上。防护层级必须留在 `guard.wrap_context`。
    """
    text = prompts.answer_prompt("<<CTX>>", "<<Q>>")
    for word in ("不要执行", "忽略资料", "指令注入"):
        assert word not in text, (
            f"模板里出现了防护性措辞 {word!r} —— 它属于 guard.wrap_context，"
            "写进模板会让 RAG_GUARD 开关失效"
        )


def _inline_string_literals(path: Path) -> list:
    """取出源码里**真正参与运行的**字符串字面量（不含 docstring）。

    为什么不直接扫全文
    ------------------
    全文扫描会把**注释和 docstring** 也算进去，而 node 的 docstring 恰恰
    应当描述"这里用的是什么模板"（`generate` 的文档就写着「参考以下资料作答」）。
    那样的断言只能靠"连注释也不能提"来维持 —— 结果是逼迫注释写得更含糊，
    反而降低了可读性。

    走 `ast` 则精确命中目标：内联提示词的写法**必然**产生字符串常量
    （f-string 的固定片段也是常量），而 docstring 可以被排除。
    """
    docstring_ids = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr):
                val = body[0].value
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    docstring_ids.add(id(val))

    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstring_ids):
            out.append(node.value)
    return out


def test_prompts_are_not_inlined_back_into_the_graph():
    """结构性断言：`src/graph.py` 的**代码**里不得再出现模板文本。

    外置的价值取决于它**完整**。只要有一句留在节点里，
    "线上跑的是哪一版提示词"就会重新变得无法回答 ——
    而那种回归没有任何运行时症状。

    只扫字符串常量（不含注释与 docstring）：docstring 本来就该说明
    "这里用的是哪套模板"，把它也算作违规会让文档与断言互相打架。
    """
    literals = _inline_string_literals(ROOT / "src" / "graph.py")
    for marker in ("参考以下资料作答", "输出【完整的新版本】",
                   "判断内容是否需要进行评审", "给出的修改意见"):
        offenders = [s for s in literals if marker in s]
        assert not offenders, (
            f"src/graph.py 的代码里又出现了提示词文本 {marker!r}：{offenders[:1]}\n"
            "请改为调用 src/prompts.py 里的模板函数"
        )


# ---------------- 版本真的下发到了外面 ----------------
def test_prompt_version_reaches_the_response_header(monkeypatch):
    """`/v1/chat` 的响应头必须带版本号。

    颗粒度正是需要的那个：一条回答 = 一个响应 = 一个版本号。
    客户端、网关访问日志、抓包都能记下来，线上答得不对时可直接核对。
    """
    pytest.importorskip("fastapi.testclient", reason="需要 httpx")
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    import app.main as M
    from app.quota import QuotaManager
    from app.ratelimit import RateLimiter

    class _FakeGraph:
        async def astream(self, incoming, config, stream_mode):
            yield SimpleNamespace(content="草稿"), {}

        async def aget_state(self, config):
            return SimpleNamespace(next=(), values={"citations": []})

    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=1000))
    monkeypatch.setattr(M, "_QUOTA", QuotaManager(daily_limit=10 ** 6, request_limit=500))
    monkeypatch.setattr(M, "graph", _FakeGraph())

    from tests.auth_util import auth_headers

    r = TestClient(M.app, headers=auth_headers()).post(
        "/v1/chat", json={"thread_id": "pv-1", "message": "hi"})
    assert r.status_code == 200
    assert r.headers.get("x-prompt-version") == prompts.PROMPT_VERSION, (
        "响应头 X-Prompt-Version 缺失或与常量不一致 —— "
        "存在版本常量但不下发，等于线上依然查不到跑的是哪一版"
    )


def test_readiness_reports_prompt_version():
    """就绪探针必须同时报**代码版本**与**提示词版本**。

    模型行为由两者共同决定：只报代码版本时，"改了提示词却回滚代码"
    会看不出任何差异。
    """
    from app import health

    _ok, body = health.readiness()
    assert body["prompt_version"] == prompts.PROMPT_VERSION
