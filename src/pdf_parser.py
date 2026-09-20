"""PDF 解析：从教程 examples/pdf_split_zh.py / pdf_split_en.py 收编并清理。

相对教程版本修掉了三个问题：
1. 教程英文版 `nltk.data.path.append('../other/punkt_tab')` —— 该目录并不存在，
   一跑就崩。这里改为「优先 nltk，缺失自动回退标点正则」，不再依赖外部数据目录。
2. 教程在拼接行时无条件加空格，中文会被拼成「梧州 是 广西」这种断字；
   这里按字符判断，中文之间不加空格。
3. 教程把文件名/页码/语言全写死在模块级、`__main__` 里硬编码 PDF 路径；
   这里全部改成函数参数，可复用、可测试。

流程（与教程一致）：pdfminer 提文本 → 短行断段重组成段落 → 句子级交叠切块。
"""

import os
import re
import logging

# ---------------------------------------------------------------------------
# 字符工具
# ---------------------------------------------------------------------------
_CJK = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")


def _is_cjk(ch: str) -> bool:
    return bool(_CJK.match(ch))


def _join(prev: str, cur: str) -> str:
    """拼接两段文本：中文之间不加空格，西文之间加空格。"""
    if not prev:
        return cur
    if not cur:
        return prev
    if _is_cjk(prev[-1]) or _is_cjk(cur[0]):
        return prev + cur
    return prev + " " + cur


def guess_lang(text: str, threshold: float = 0.2) -> str:
    """粗略判断中英文：CJK 字符占比超过阈值即视为中文。"""
    if not text:
        return "en"
    cjk = len(_CJK.findall(text))
    return "zh" if cjk > len(text) * threshold else "en"


# ---------------------------------------------------------------------------
# 1. 提取文本
# ---------------------------------------------------------------------------
def extract_pdf_text(path: str, page_numbers=None) -> str:
    """用 pdfminer 提取 PDF 文本（按页顺序拼接）。

    page_numbers: 可选的页码集合（0 基），None 表示全部页。
    """
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer

    parts = []
    for i, page_layout in enumerate(extract_pages(path)):
        if page_numbers is not None and i not in page_numbers:
            continue
        for element in page_layout:
            if isinstance(element, LTTextContainer):
                parts.append(element.get_text())
    return "".join(parts)


# ---------------------------------------------------------------------------
# 2. 段落重组（纯函数，便于单测）
# ---------------------------------------------------------------------------
def rebuild_paragraphs(full_text: str, min_line_length: int = 1) -> list[str]:
    """把逐行文本重组成段落。

    启发式（沿用教程思路）：长度 >= min_line_length 的行并入当前缓冲区；
    遇到短行（通常是空行）就把缓冲区吐出一个段落。行尾连字符会被去掉。
    """
    paragraphs: list[str] = []
    buf = ""
    # 上一行以连字符结尾 → 当前行要无缝接上（exam- + ple -> example），不能补空格
    join_no_space = False

    for line in full_text.split("\n"):
        if len(line) >= min_line_length:
            if join_no_space and buf:
                buf = buf + line
                join_no_space = False
            else:
                buf = _join(buf, line)
            if buf.endswith("-"):
                buf = buf[:-1]
                join_no_space = True
        elif buf:
            paragraphs.append(buf)
            buf = ""
            join_no_space = False

    if buf:
        paragraphs.append(buf)
    return paragraphs


# ---------------------------------------------------------------------------
# 3. 断句
# ---------------------------------------------------------------------------
_ZH_SENT_END = re.compile(r"(?<=[。！？；?!])")
_EN_SENT_END = re.compile(r"(?<=[.!?])\s+")


def sent_tokenize_zh(text: str) -> list[str]:
    """中文断句：在 。！？；?! 之后切（沿用教程正则）。"""
    return [s.strip() for s in _ZH_SENT_END.split(text) if s.strip()]


def sent_tokenize_en(text: str) -> list[str]:
    """英文断句：优先 nltk；punkt 数据缺失时回退标点正则，绝不因此崩掉。"""
    try:
        from nltk.tokenize import sent_tokenize as _nltk_sent

        return [s.strip() for s in _nltk_sent(text) if s.strip()]
    except Exception as e:  # LookupError: punkt 未下载 / 目录不存在
        logging.warning("nltk 断句不可用（%s），回退标点正则", e)
        return [s.strip() for s in _EN_SENT_END.split(text) if s.strip()]


def sent_tokenize(text: str, lang: str = "auto") -> list[str]:
    if lang == "auto":
        lang = guess_lang(text)
    return sent_tokenize_zh(text) if lang == "zh" else sent_tokenize_en(text)


# ---------------------------------------------------------------------------
# 4. 句子级交叠切块（沿用教程算法，修掉中文加空格）
# ---------------------------------------------------------------------------
def split_text_sentence_aware(
    paragraphs, chunk_size: int = 800, overlap_size: int = 200, lang: str = "auto"
) -> list[str]:
    """把段落拆成句子，再按 chunk_size 打包成带重叠的块。

    重叠是「向前看」实现的：每个块开头会带上前面若干句，直到累计长度达到
    overlap_size，让跨块的上下文不至于被硬切断。
    """
    sentences: list[str] = []
    for p in paragraphs:
        sentences.extend(sent_tokenize(p, lang))
    if not sentences:
        return []

    chunks: list[str] = []
    i = 0
    while i < len(sentences):
        head = sentences[i]

        # 向前收集重叠部分
        overlap = ""
        prev = i - 1
        while prev >= 0 and len(sentences[prev]) + len(overlap) <= overlap_size:
            overlap = _join(sentences[prev], overlap)
            prev -= 1

        chunk = _join(overlap, head)

        # 向后堆到接近 chunk_size
        nxt = i + 1
        while nxt < len(sentences) and len(sentences[nxt]) + len(chunk) <= chunk_size:
            chunk = _join(chunk, sentences[nxt])
            nxt += 1

        chunks.append(chunk)
        i = nxt
    return chunks


# ---------------------------------------------------------------------------
# 5. 对外主入口
# ---------------------------------------------------------------------------
def pdf_to_chunks(
    path: str,
    chunk_size: int = 800,
    overlap_size: int = 200,
    page_numbers=None,
    min_line_length: int = 1,
) -> list[str]:
    """PDF 文件 → 文本块列表（教程那条完整链路）。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"PDF 不存在: {path}")
    text = extract_pdf_text(path, page_numbers=page_numbers)
    paragraphs = rebuild_paragraphs(text, min_line_length=min_line_length)
    return split_text_sentence_aware(paragraphs, chunk_size, overlap_size)


def extract_pdf_paragraphs(path: str, page_numbers=None, min_line_length: int = 1) -> list[str]:
    """PDF 文件 → 段落列表（只要解析，不切块）。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"PDF 不存在: {path}")
    return rebuild_paragraphs(extract_pdf_text(path, page_numbers), min_line_length)
