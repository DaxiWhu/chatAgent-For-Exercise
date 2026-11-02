"""把**过长的记忆**压成一句自足的话 —— 写入前的最后一道加工。

为什么需要它
------------
``remember`` 的入参是模型自己写的 ``content``。工具说明里已经要求"必须是一句自足的
话"，但模型经常不照做：把用户原话整段抄进来、或者把三件不相关的事写成一段。
一条 800 字的记忆会吃掉注入预算（``memory_store.MAX_INJECT_CHARS`` 2000 字）的一大半，
而那段预算本该放七八条真正的偏好 —— 表现为"记了很多，但回答时该用的那条没出现"。

为什么只在**超过长度线**时才调模型
--------------------------------
每次提炼都是一次额外的出站请求，直接压在用户等待上。而短记忆（默认 200 字以内）
本身就已经是一句话，再压一遍只会丢细节、加延迟，收益为负。所以门槛刻意放在
"明显在照抄原文"的那一档，而不是"每条都过一遍"。

降级方向：**宁长勿丢**
----------------------
提炼失败（没配 Key / 网关抖动 / 返回空）⇒ **原样存**，不截断、不丢弃。
理由：用户明确要求"记住"的内容，宁可它长，也不能悄悄少半句 —— 而注入侧已经有
总长度上限兜底（``render`` 到 2000 字就停并注明"其余未列出"）。
反过来，按"提炼失败就不记"处理是错的：那等于让一次网关抖动变成"用户说过的话丢了"。

与 ``compaction._client_or_none`` 的关系：**刻意不复用那个单例**
--------------------------------------------------------------
那个 client 的失败是**粘性**的（建不起来就永久返回 ``None``）。共用会让"压缩侧建
client 失败一次"连带把提炼永久废掉，而两者的降级语义本来就不同（压缩失败 = 不压；
提炼失败 = 原样存）。**配置变量共享（都用 ``LLM_*``），client 对象不共享。**
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from .. import observability

__all__ = ["digest_above", "digest", "hard_cap", "reset_digest_state"]

#: 超过这个字数才提炼。默认 200（约一小段）—— 正常的一句自足陈述撞不到它。
_ABOVE_DEFAULT = 200

#: 传给提炼模型的原文上限。一条两万字的记忆不该让这次调用跟着线性变长；
#: 开头那一段几乎总是最要紧的（结论/要求通常先说）。
_MAX_INPUT_CHARS = 8000

#: 提炼结果自身的上限（字）。模型不听话时会返回一整段，这里硬截一次 ——
#: 只用于兜住"提炼本身没起作用"的情形，不是常规路径。
_DIGEST_MAX_CHARS = 400

_client: Any = None
_client_failed = False
_failure_logged = False

#: 模型常见的几种"多余包装"：```text 围栏、``- `` 列表符、引号、编号。
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_BULLET_RE = re.compile(r"^(?:[-*•]|\d+[.、])\s*")
_QUOTES = ("\"", "'", "“", "”", "‘", "’", "「", "」")

_KIND_HINT = {
    "persona": "这个人是谁（身份、职责、背景）",
    "preference": "他怎么要求回答（格式、语气、篇幅、先后顺序）",
    "project": "项目级的事实与结论（技术栈、约定、环境、截止时间）",
    "feedback": "他纠正过的做法，以及为什么",
    "reference": "一个索引：到哪去找什么东西",
}


def reset_digest_state() -> None:
    """测试用：清掉 client 单例与"失败已记过"的标记（与 reflection 同一手法）。

    生产路径不调它。``_client_failed`` 是**粘性**的，跨用例不清会让第二个用例
    拿到"上次建失败"的结论，表现为"单独跑绿、整组跑红"。
    """
    global _client, _client_failed, _failure_logged
    _client = None
    _client_failed = False
    _failure_logged = False


def digest_above() -> int:
    """超过多少字就提炼（``AGENT_MEMORY_DIGEST_ABOVE``，默认 200）。

    配 ``0`` 表示**关闭提炼**（永远原样存）；配负数按 0 处理 —— 两者等价，
    都是"这条线关掉"，不需要第二个开关。
    """
    raw = (os.getenv("AGENT_MEMORY_DIGEST_ABOVE", "") or "").strip()
    if not raw:
        return _ABOVE_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        return _ABOVE_DEFAULT


def hard_cap() -> int:
    """单条记忆的字符硬上限。**不是截断线**，只用于挡住明显失控的输入。"""
    return _DIGEST_MAX_CHARS


def _timeout() -> float:
    raw = (os.getenv("AGENT_MEMORY_DIGEST_TIMEOUT", "") or "").strip()
    try:
        return max(1.0, float(raw)) if raw else 20.0
    except ValueError:
        return 20.0


def _model_config() -> tuple[str, str, str]:
    """(model, api_key, base_url) —— 与主链路**同一组**环境变量。

    刻意不引入第二套配置：同一个服务里两套模型变量是运维事故的常见来源，
    而且提炼用的模型与主链路不同会让"记忆的措辞"和"回答的措辞"不一致。
    """
    model = (os.getenv("LLM_MODEL", "") or "").strip() or "deepseek-chat"
    key = (os.getenv("LLM_API_KEY", "") or "").strip()
    base = (os.getenv("LLM_API_BASE", "") or "").strip()
    return model, key, base


def _client_or_none() -> Any:
    global _client, _client_failed
    if _client is not None:
        return _client
    if _client_failed:
        return None
    _, key, base = _model_config()
    if not key:
        # 没配 Key ⇒ 没有提炼能力。**不是错误**：调用方按"原样存"降级。
        return None
    try:
        from openai import OpenAI

        kwargs: dict[str, Any] = {
            "api_key": key, "timeout": _timeout(), "max_retries": 1,
        }
        if base:
            kwargs["base_url"] = base
        _client = OpenAI(**kwargs)
    except Exception as e:  # noqa: BLE001  没装 openai / client 建不起来
        _client_failed = True
        logging.warning("提炼记忆用的模型客户端建不起来 ⇒ 长记忆将原样保存：%s", e)
        return None
    return _client


def _system_prompt(kind: str) -> str:
    hint = _KIND_HINT.get(kind, _KIND_HINT["project"])
    return (
        "你把一段过长的内容压成**一句**可以长期保存的记忆。\n"
        f"这条记忆要记的是：{hint}。\n"
        "硬性要求：\n"
        "1. 只输出那一句话本身。不要编号、不要解释、不要加引号、不要写\"记忆：\"之类的前缀。\n"
        "2. 必须是陈述句，脱离这段对话也读得懂：用\"用户\"或具体的项目名/文件名做主语，"
        "不要出现\"这样\"\"那个\"\"上面说的\"这类指代。\n"
        "3. 保留条件、范围、例外。路径、命令、数值、日期、报错原文**逐字保留**，"
        "不要改写成概括。\n"
        "4. 丢掉一次性的过程（\"他先试了 X，后来改成 Y\"），只留最终结论。\n"
        "5. 如果原文其实是几件互不相关的事，只留最要紧的一件，其余一律丢弃。\n"
        f"6. 长度控制在 {_DIGEST_MAX_CHARS} 字以内。"
    )


def _clean(text: str) -> str:
    """剥掉模型爱加的那几层包装，取第一行有效内容。"""
    out = _FENCE_RE.sub("", str(text or "")).strip()
    if not out:
        return ""
    for line in out.splitlines():
        line = _BULLET_RE.sub("", line.strip()).strip()
        line = line.strip("".join(_QUOTES)).strip()
        if line:
            return line[:_DIGEST_MAX_CHARS]
    return ""


def digest(content: str, kind: str = "project", *, stats: Any = None) -> str:
    """把一条长记忆压成一句自足的话（**同步**）。压不了就返回空串。

    返回空串的语义是**这次没压成**，调用方必须原样存 —— 不是"这条不要了"。

    ``stats``：传了就把这次调用的 usage 记进旁路（``budget.note_bypass``）。
    理由与压缩、反思一致：它是主链路之外多出来的一次出站请求，不记账的话
    账单里会有一段谁也解释不了的量。
    """
    body = str(content or "").strip()
    if not body:
        return ""
    client = _client_or_none()
    if client is None:
        observability.counter("agent_memory_digest_total", result="no_client")
        return ""
    model, _, _ = _model_config()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _system_prompt(kind)},
                {"role": "user", "content": body[:_MAX_INPUT_CHARS]},
            ],
        )
    except Exception as e:  # noqa: BLE001  上游抖动 / 网关不认 / 超时
        _note_failure(e)
        observability.counter("agent_memory_digest_total", result="error")
        return ""
    # 记账放在取值之前：这次出站调用已经花掉了，不管结果能不能用。
    if stats is not None:
        try:
            from . import budget

            budget.note_bypass(stats, "memory_digest", getattr(resp, "usage", None))
        except Exception as e:  # noqa: BLE001  记账失败不该让写入失败
            logging.debug("提炼用量记账失败（已忽略）：%s", e)
    cleaned = _clean(resp.choices[0].message.content or "")
    if not cleaned:
        logging.warning("提炼记忆返回了空内容 ⇒ 这条按原样保存")
        observability.counter("agent_memory_digest_total", result="empty")
        return ""
    observability.counter("agent_memory_digest_total", result="ok")
    return cleaned


def _note_failure(exc: BaseException) -> None:
    """只打第一条完整日志（防日志放大，与 ``compaction._note_failure`` 同一理由）。"""
    global _failure_logged
    observability.counter("agent_memory_digest_total", result="failed")
    if _failure_logged:
        logging.debug("提炼记忆仍失败：%s", exc)
        return
    _failure_logged = True
    logging.warning(
        "提炼长记忆失败 ⇒ 这条按**原样**保存（不截断、不丢弃）。"
        "后果只是记忆偏长、占注入预算。原因：%s", exc,
    )
