"""普通 function tools —— **确定性能力不该被包成 agent**。

这是本示例最容易做错的一处。官方 ``research_bot`` 的 ``ResearchManager``
是一个普通 Python 类（不是 ``Agent``），检索、落盘、渲染这类步骤全部由代码完成。
判据：**需要模型「拿主意」的才配 agent，其余都是函数。**

这里的实现全部离线、确定性（不打网络、不调外部服务），
好让整条链路的断言可复现。
"""

from __future__ import annotations

import json
from pathlib import Path

from agents import function_tool

__all__ = [
    "ASSET_DIR",
    "generate_image",
    "image_asset_path",
    "kb_search",
    "render_slide_deck",
    "save_text_asset",
]

ASSET_DIR = Path(__file__).resolve().parent / "_assets"

_KB: dict[str, list[tuple[str, str]]] = {
    "rate_limit": [
        (
            "S1",
            "限流采用加权滑动窗口：判定值 = 上一窗口计数 × 剩余比例 + 当前窗口计数。",
        ),
        (
            "S2",
            "Redis 键的 TTL 必须覆盖两个窗口（2*WINDOW+1），否则上一窗口会被读成 0。",
        ),
        (
            "S3",
            "被限流时返回 429，并在 Retry-After 里给出建议等待秒数。",
        ),
    ],
    "chunk": [
        ("S4", "分块口径统一为「来源去重后的字符数」，不按 token 计。"),
        ("S5", "chunk 元数据必须回填 chunk_id / source / tenant，否则引用无法回溯。"),
    ],
    "guard": [
        ("S6", "入口护栏只对链上第一个 agent 生效，且默认与模型并行执行。"),
        ("S7", "出口护栏只对产出最终输出的 agent 生效，且必定串行。"),
    ],
    "ppt": [
        ("S8", "PPT 大纲建议每页不超过 6 条要点，每条不超过 20 字。"),
    ],
}


@function_tool
def kb_search(query: str, top_k: int = 3) -> str:
    """在本地知识库里检索与 query 相关的片段，返回带出处编号的原文。

    Args:
        query: 检索问题，可以是关键词也可以是自然语言。
        top_k: 最多返回几条片段。
    """
    hits: list[tuple[str, str]] = []
    lowered = query.lower()
    for topic, rows in _KB.items():
        if topic in lowered or any(part and part in lowered for part in topic.split("_")):
            hits.extend(rows)
    if not hits:
        hits = [row for rows in _KB.values() for row in rows][:1]
    picked = hits[: max(1, top_k)]
    return json.dumps(
        [{"source": src, "text": text} for src, text in picked],
        ensure_ascii=False,
        indent=2,
    )


@function_tool
def save_text_asset(name: str, content: str) -> str:
    """把一段文本落盘成资产，返回资产路径。

    Args:
        name: 文件名（不含扩展名）。
        content: 文本正文。
    """
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name) or "asset"
    path = ASSET_DIR / f"{safe}.txt"
    path.write_text(content, encoding="utf-8", newline="")
    return str(path)


def image_asset_path(prompt: str) -> str:
    """确定性推导图片资产路径（示例占位实现用；真实实现不需要这个函数）。"""
    slug = "".join(ch if ch.isalnum() else "_" for ch in prompt)[:32] or "image"
    return str(ASSET_DIR / f"{slug}.png.txt")


@function_tool
def generate_image(prompt: str, style: str = "flat") -> str:
    """按描述生成一张图片，返回资产路径。

    示例里用确定性占位实现（只写一个描述文件，不调用外部图像服务），
    真实实现应替换为图像生成能力。

    Args:
        prompt: 画面描述。
        style: 风格关键词。
    """
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    path = Path(image_asset_path(prompt))
    path.write_text(f"[placeholder image]\nprompt: {prompt}\nstyle: {style}\n", encoding="utf-8", newline="")
    return str(path)


def _render_slide_deck(deck_json: str) -> str:
    """把结构化大纲渲染成文件（确定性步骤，在代码里调用，不是 agent 也不是工具）。"""
    deck = json.loads(deck_json)
    lines = [f"# {deck.get('title', '')}", ""]
    if deck.get("subtitle"):
        lines += [f"_{deck['subtitle']}_", ""]
    for index, slide in enumerate(deck.get("slides", []), start=1):
        lines.append(f"## {index}. {slide.get('title', '')}")
        lines += [f"- {item}" for item in slide.get("bullets", [])]
        if slide.get("speaker_notes"):
            lines.append(f"  > 备注：{slide['speaker_notes']}")
        lines.append("")
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    path = ASSET_DIR / "deck.md"
    path.write_text("\n".join(lines), encoding="utf-8", newline="")
    return str(path)


render_slide_deck = _render_slide_deck
"""渲染步骤刻意**不做成 ``@function_tool``**：没有任何模型需要"决定要不要渲染"。
把它暴露成工具，只会给模型多一个可以忘掉的选项。"""
