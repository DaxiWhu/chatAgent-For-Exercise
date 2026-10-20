"""提示词集中化与版本号的契约。

为什么需要这个测试
------------------
提示词外置解决的是"可追溯"，而可追溯只有在**版本号真的跟着文本走**时才成立。
这里钉住三件事：

  1. **文本指纹 ↔ 版本号**：只改模板文字、忘了 bump `PROMPT_VERSION`，
     会直接测红。没有这条，"版本号"就退化为一个人工维护的注释 ——
     它会在第一次赶工的改动里失真，且失真是静默的（版本照旧、行为已变）。
  2. **提示词不再回流到链路代码里**：结构性断言 `src/`（除本模块外）不含模板文本。
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
# 键是 PROMPT_VERSION；值 = 模板用固定哨兵参数渲染后的 sha256。
# 更新方法：跑 `python -c "from tests.test_prompts import fingerprint; print(fingerprint())"`
#
# ⚠️ 2026-10-20：删掉 `revise_prompt` / `evaluate_prompt` 后**重算过一次**（那两个
# 模板随旧编排图的节点一起删除，它们从来没有在 agent 链路里被送给模型）。
# 版本号**没有** bump：指纹表回答的是"线上实际送给模型的文本变了没有"，
# 而留下来的 `answer_prompt` 一个字符都没动。别把这次当成"忘了 bump"的先例。
_PROMPT_FINGERPRINTS = {
    "1.0.0": "47993a244e2c6aaaedf260d646ef606ea7520111964ceae481cea43d32d64cba",
}


def fingerprint() -> str:
    """所有模板的合并指纹。

    用哨兵参数渲染（而不是读源码）：指纹覆盖的是**模型实际看到的文本**。
    读源码会把注释、变量名、换行风格也算进去 —— 那些不影响行为的改动
    本不该要求 bump 版本号。
    """
    parts = [
        prompts.answer_prompt("<<CTX>>", "<<QUESTION>>"),
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


def test_the_dead_templates_are_really_gone():
    """旧编排图的两个模板（修订 / 评审）**必须不存在**。

    为什么值得单钉一条：它们删掉之后没有任何生产调用点，所以"被人顺手加回来"
    不会有任何运行时症状 —— 只会让 `PROMPT_VERSION` 的指纹表重新变得含糊
    （表里到底覆盖了哪些模板？），而 agent 链路的评审其实走 evaluator 自己的
    instructions，两套并存的后果是"改了一处、线上没变"。
    """
    for name in ("revise_prompt", "evaluate_prompt"):
        assert not hasattr(prompts, name), f"`{name}` 又被加回来了"


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


def test_prompts_are_not_inlined_back_into_the_code():
    """结构性断言：**除 ``src/prompts.py`` 之外**，链路代码里不得出现模板文本。

    外置的价值取决于它**完整**。只要有一句留在某个 agent 的 instructions 里，
    "线上跑的是哪一版提示词"就会重新变得无法回答 —— 而那种回归没有任何运行时
    症状。

    ⚠️ 扫的是整个 ``src/``（而不是某个具体模块）：模板可能被复制到任何一处
    （specialists / frontline / evaluator / runner），逐个指定文件等于给下一次
    复制留后门。

    只扫字符串常量（不含注释与 docstring）：docstring 本来就该说明"这里用的是
    哪套模板"，把它也算作违规会让文档与断言互相打架。
    """
    offenders: list[tuple[str, str]] = []
    for path in (ROOT / "src").rglob("*.py"):
        if path.name == "prompts.py" or "__pycache__" in path.parts:
            continue
        for lit in _inline_string_literals(path):
            for marker in ("参考以下资料作答",):
                if marker in lit:
                    offenders.append((str(path.relative_to(ROOT)), marker))

    assert not offenders, (
        f"这些文件里又出现了提示词文本（应改为调用 src/prompts.py 的模板）：{offenders[:3]}"
    )


# ---------------- 版本真的下发到了外面 ----------------
def test_prompt_version_reaches_the_response_header(monkeypatch):
    """`/v1/chat` 的响应头必须带版本号。

    颗粒度正是需要的那个：一条回答 = 一个响应 = 一个版本号。
    客户端、网关访问日志、抓包都能记下来，线上答得不对时可直接核对。
    """
    pytest.importorskip("fastapi.testclient", reason="需要 httpx")

    from fastapi.testclient import TestClient

    import app.main as M
    from app.quota import QuotaManager
    from app.ratelimit import RateLimiter
    from src.agents.runner import TaskOutcome

    async def _fake_turn(req, *, context, on_text=None):
        if on_text is not None:
            await on_text("草稿", "call-1")
        return TaskOutcome(answer="草稿", attempts=1, final_agent="frontline_agent",
                           passed=True, exit_reason="passed")

    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=1000))
    monkeypatch.setattr(M, "_QUOTA", QuotaManager(daily_limit=10 ** 6, request_limit=500))
    monkeypatch.setattr(M, "run_turn", _fake_turn)

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
