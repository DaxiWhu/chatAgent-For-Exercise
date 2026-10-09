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

import logging
import os
import re

# ---------------------------------------------------------------------------
# 字符工具
# ---------------------------------------------------------------------------
_CJK = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")


def _is_cjk(ch: str) -> bool:
    """判断单个字符是否落在 CJK / 全角区（决定拼接时要不要补空格）。

    ch: 单个字符。要求非空 —— 正则 match 对空串返回 None，而本函数的返回值会被
        调用方直接当布尔用，故调用方（_join）已保证传入的是真实字符。
    """
    return bool(_CJK.match(ch))


def _join(prev: str, cur: str) -> str:
    """拼接两段文本：中文之间不加空格，西文之间加空格。

    prev: 已累积的前半段。传空串表示"尚无内容"，此时直接返回 cur（不补前导空格）。
    cur: 待追加的后半段。传空串时原样返回 prev。

    判据只看边界字符：prev 的末字或 cur 的首字任一是 CJK 就不加空格 ——
    这样"梧州"+"是 "+"广西" 能拼回"梧州是广西"，而英文单词之间仍保留空格。
    """
    if not prev:
        return cur
    if not cur:
        return prev
    if _is_cjk(prev[-1]) or _is_cjk(cur[0]):
        return prev + cur
    return prev + " " + cur


def guess_lang(text: str, threshold: float = 0.2) -> str:
    """粗略判断中英文：CJK 字符占比超过阈值即视为中文。

    text: 待判断文本。空串直接返回 "en"（避免除零）。
    threshold: CJK 占比阈值，取值 0~1。默认 0.2 —— 中英混排文档只要有两成汉字
        就按中文断句，因为中文断句正则（。！？；）对夹带的英文句子同样安全。
    """
    if not text:
        return "en"
    cjk = len(_CJK.findall(text))
    return "zh" if cjk > len(text) * threshold else "en"


# ---------------------------------------------------------------------------
# 1. 提取文本
# ---------------------------------------------------------------------------
def extract_pdf_text(path: str, page_numbers=None) -> str:
    """用 pdfminer 提取 PDF 文本（按页顺序拼接）。

    path: PDF 文件路径。内部交给 pdfminer 的 extract_pages，不做存在性检查 ——
        文件不存在会由 pdfminer 自己抛异常。
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


def pdf_page_text_chars(path: str) -> dict[int, int]:
    """每页的文字字符数（键为 1 基页码）。读不出时抛异常，由调用方决定降级。

    path: PDF 文件路径。与 extract_pdf_text 共用同一次 extract_pages 遍历。

    为什么按页统计而不是取 extract_pdf_text 的总长度：**混合文档**（部分页扫描、
    部分页电子）的总字符数会被电子页淹掉 —— 整份看着"文字层很厚"，那几页扫描页
    其实一个字都抽不出来。按页看到它们，才有可能为它们开 OCR。

    与 extract_pdf_text 的另一处差别是**只计数、不拼接**，比抽全文更省；
    也因此不 strip 全文，只对每个容器单独 strip（避免纯空白容器把页眉页脚的空行
    算成文字）。注意页码是 1 基，而 extract_pdf_text 的 page_numbers 是 0 基 ——
    两者刻意不同：本函数的对象是要落进 chunk.page 的页码，而 chunk.page 是 1 基。
    """
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer

    out: dict[int, int] = {}
    for i, page_layout in enumerate(extract_pages(path), start=1):
        #page_layout：
        #LTTextBox（LTTextContainer）	一段文字块，.get_text() 取文字	★ 只筛它
        # LTFigure	内嵌 Form 区域（可递归）	忽略
        # LTImage	位图	忽略
        # LTRect / LTCurve / LTLine	矢量线框（表格框线、下划线）	忽略
        n = 0
        for element in page_layout:
            if isinstance(element, LTTextContainer):
                n += len(element.get_text().strip())
        out[i] = n
    return out


# ---------------------------------------------------------------------------
# 2. 段落重组（纯函数，便于单测）
# ---------------------------------------------------------------------------
def _absorb_line(prev: str, line: str, join_no_space: bool) -> tuple[str, bool]:
    """把一行并入缓冲区，返回 (新缓冲区, 下一行是否要无缝续接)。

    prev: 已累积的缓冲区内容（可能为空）。
    line: 当前这一行文本（不含换行符）。
    join_no_space: True 表示上一行以连字符结尾（断词），当前行必须**直接贴上**：
        走 _join 会补一个空格，把 example 拼成 exam ple。
        该标记由上一次调用在本函数末尾产出（merged.endswith("-") 时置 True）。

    返回值第二项是"下一行是否无缝续接"，调用方须把它透传给下一次调用。
    """
    merged = prev + line if (join_no_space and prev) else _join(prev, line)
    if merged.endswith("-"):
        return merged[:-1], True     # 去掉行尾连字符，并标记下一行无缝续接
    return merged, False


def rebuild_paragraphs(full_text: str, min_line_length: int = 1) -> list[str]:
    """把逐行文本重组成段落。

    full_text: 待重组的整份文本，内部按 "\\n" 切行。段落边界只由【空行】承载，
        所以上游 extract_pdf_text 必须在容器之间插空行，否则这里分不出段落。
    min_line_length: 判定"短行"的长度阈值（字符数）。默认 1，即只有严格空行算短行，
        行为等价于「空行 = 段落分隔」。

    判据只有一条：行的长度是否 >= min_line_length。
      - 长行 → 并入当前缓冲区（排版换行被消除）
      - 短行 → 断段。但要区分两种短行：
          1) 空行 / 纯空白行 = 段落分隔 → 只断段
          2) 段落的最后一行（有内容但短）→ 仍属当前段落 → 先并入再断段

    注意：把 min_line_length 调大则会启用「短行 = 段落结束」启发式，
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
    """中文断句：在 。！？；?! 之后切（沿用教程正则）。

    text: 待断句的段落文本。切分后逐句 strip 并丢掉空串，
        所以输入可以是不规整的原始段落。
    """
    return [s.strip() for s in _ZH_SENT_END.split(text) if s.strip()]


def sent_tokenize_en(text: str) -> list[str]:
    """英文断句：优先 nltk；punkt 数据缺失时回退标点正则，绝不因此崩掉。

    text: 待断句的段落文本。nltk 不可用（未装或 punkt 数据缺失）时只记 warning
        并走 _EN_SENT_END 正则，返回值形态与 nltk 分支一致。
    """
    try:
        from nltk.tokenize import sent_tokenize as _nltk_sent

        return [s.strip() for s in _nltk_sent(text) if s.strip()]
    except Exception as e:  # LookupError: punkt 未下载 / 目录不存在
        logging.warning("nltk 断句不可用（%s），回退标点正则", e)
        return [s.strip() for s in _EN_SENT_END.split(text) if s.strip()]


def sent_tokenize(text: str, lang: str = "auto") -> list[str]:
    """按语言分发到中/英断句实现。

    text: 待断句文本。lang="auto" 时先用 guess_lang 自动判定。
    lang: "zh" / "en" / "auto"。取值不是这两个之一时按英文处理（走 else 分支），
        不会报错。
    """
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

    paragraphs: 段落列表（rebuild_paragraphs 的产物）。空列表 / 全部无有效句子时
        返回 []。
    chunk_size: 每块的目标长度上限（字符数）。按"整句"累加，所以实际长度会略小于
        该值（不会为凑满而截断句子）。
    overlap_size: 每块**向前**额外带回的上下文长度上限（字符数）。实现是收集前置
        句子直到累计超过该值，让跨块的上下文不至于被硬切断。
        取 0 表示不重叠；取 >= chunk_size 时重叠可能吞掉整块内容（见下方实现说明）。
    lang: "zh" / "en" / "auto"，透传给 sent_tokenize。

    重叠是「向前看」实现的：每个块开头会带上前面若干句。
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
    """PDF 文件 → 文本块列表（教程那条完整链路）。

    path: PDF 文件路径。**会做存在性检查**，不存在抛 FileNotFoundError
        （与内部三个纯函数不同，那是留给单测直接调用的）。
    chunk_size: 块长度上限（字符），透传 split_text_sentence_aware。
    overlap_size: 块间重叠长度上限（字符），透传 split_text_sentence_aware。
    page_numbers: 参与解析的页码集合（**0 基**），None 表示全部页，透传 extract_pdf_text。
    min_line_length: 段落重组时的短行阈值，透传 rebuild_paragraphs，默认 1 不建议改。

    返回值是**纯字符串列表**，不含任何元数据（无 source / 页码 / kind）；
    调用方 from_pdfminer_pieces 负责把它们包成 KnowledgeChunk。
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"PDF 不存在: {path}")
    text = extract_pdf_text(path, page_numbers=page_numbers)
    paragraphs = rebuild_paragraphs(text, min_line_length=min_line_length)
    return split_text_sentence_aware(paragraphs, chunk_size, overlap_size)


def extract_pdf_paragraphs(path: str, page_numbers=None, min_line_length: int = 1) -> list[str]:
    """PDF 文件 → 段落列表（只要解析，不切块）。

    path: PDF 文件路径。会做存在性检查，不存在抛 FileNotFoundError。
    page_numbers: 参与解析的页码集合（0 基），None 表示全部页。
    min_line_length: 短行阈值，透传 rebuild_paragraphs。

    用途是"看解析质量"而不是"喂检索"——不切块，段落边界清晰可见。
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"PDF 不存在: {path}")
    return rebuild_paragraphs(extract_pdf_text(path, page_numbers), min_line_length)
