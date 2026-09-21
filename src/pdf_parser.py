"""PDF 解析：从教程 examples/pdf_split_zh.py / pdf_split_en.py 收编并清理。

相对教程版本修掉了五个问题：
1. 教程英文版 `nltk.data.path.append('../other/punkt_tab')` —— 该目录并不存在，
   一跑就崩。这里改为「优先 nltk，缺失自动回退标点正则」，不再依赖外部数据目录。
2. 教程在拼接行时无条件加空格，中文会被拼成「梧州 是 广西」这种断字；
   这里按字符判断，中文之间不加空格。
3. 教程把文件名/页码/语言全写死在模块级、`__main__` 里硬编码 PDF 路径；
   这里全部改成函数参数，可复用、可测试。
4. 原用 `"".join(容器)` 拼接，容器边界只剩一个 \\n，导致「空行」永远不出现，
   段落层实际失效（整页永远 1 段）。改为容器之间插入空行。
5. 短行分支原先只做断段、把该行本身直接丢弃，`min_line_length>1` 时会静默丢正文；
   改为「非空短行先并入再断段」。

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

    容器（LTTextContainer）之间用【空行】分隔——这一点很关键：
    pdfminer 已经按垂直间距把文字切成了「段落级」容器，如果直接用 "" 拼接，
    容器边界只会剩一个 \\n，下游的 rebuild_paragraphs 就再也分不出段落了
    （实测两个视觉上明显分开的段落会被合并成 1 段）。
    """
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer

    parts = []
    for i, page_layout in enumerate(extract_pages(path)):
        if page_numbers is not None and i not in page_numbers:
            continue
        for element in page_layout:
            if isinstance(element, LTTextContainer):
                text = element.get_text()
                if text.strip():                      # 跳过纯空白容器
                    parts.append(text.strip("\n"))    # 只去首尾换行，保留容器内部换行
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 2. 段落重组（纯函数，便于单测）
# ---------------------------------------------------------------------------
def _absorb_line(prev: str, line: str, join_no_space: bool) -> tuple[str, bool]:
    """把一行并入缓冲区，返回 (新缓冲区, 下一行是否要无缝续接)。

    join_no_space=True 表示上一行以连字符结尾（断词），当前行必须直接贴上：
    走 _join 会补一个空格，把 example 拼成 exam ple。
    """
    merged = prev + line if (join_no_space and prev) else _join(prev, line)
    if merged.endswith("-"):
        return merged[:-1], True     # 去掉行尾连字符，并标记下一行无缝续接
    return merged, False


def rebuild_paragraphs(full_text: str, min_line_length: int = 1) -> list[str]:
    """把逐行文本重组成段落。

    判据只有一条：行的长度是否 >= min_line_length。
      - 长行 → 并入当前缓冲区（排版换行被消除）
      - 短行 → 断段。但要区分两种短行：
          1) 空行 / 纯空白行 = 段落分隔 → 只断段
          2) 段落的最后一行（有内容但短）→ 仍属当前段落 → 先并入再断段

    注意：默认 min_line_length=1 时，只有【严格空行】算短行，行为等价于
    「空行 = 段落分隔」；把 min_line_length 调大则会启用「短行 = 段落结束」启发式，
    该启发式会双向出错（段落末行排得满→漏检；表格/标题等短行→误检），故不建议调整。
    """
    paragraphs: list[str] = []
    buf = ""
    join_no_space = False

    for line in full_text.split("\n"):
        if len(line) >= min_line_length:
            buf, join_no_space = _absorb_line(buf, line, join_no_space)
        else:
            # 短行若含实际内容（段落最后一行），必须先并入缓冲区——
            # 早期版本这里只做 flush、直接丢掉了这一行，在 min_line_length>1 时
            # 会静默丢正文（实测同一段文字 147 字变成 142 字）。
            if line.strip():
                buf, join_no_space = _absorb_line(buf, line, join_no_space)
            if buf:
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
