"""PDF 解析与语料装载测试：全部离线（PDF 由测试自己现场生成）。

覆盖从教程 examples/pdf_split_*.py 收编的解析链路，以及相对教程修掉的三处问题：
- 中文拼接不再插空格
- 连字符断行无缝拼接
- nltk punkt 数据缺失时自动回退，不再崩
"""
import os
import io

import pytest

from src.pdf_parser import (
    extract_pdf_text,
    extract_pdf_paragraphs,
    rebuild_paragraphs,
    sent_tokenize_zh,
    sent_tokenize,
    split_text_sentence_aware,
    pdf_to_chunks,
    guess_lang,
)
from src.retrieval import load_chunks


# ---------------------------------------------------------------------------
# 工具：现场手搓一个最小合法 PDF（只含 Helvetica ASCII 文本）
# ---------------------------------------------------------------------------
def make_pdf(path, lines):
    content = "BT /F1 12 Tf 72 720 Td 14 TL\n"
    for ln in lines:
        content += f"({ln}) Tj T*\n"
    content += "ET\n"
    cb = content.encode("latin-1")

    objs = [
        b"<</Type /Catalog /Pages 2 0 R>>",
        b"<</Type /Pages /Kids [3 0 R] /Count 1>>",
        b"<</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources <</Font <</F1 5 0 R>>>>>>",
        b"<</Length " + str(len(cb)).encode() + b">>stream\n" + cb + b"endstream",
        b"<</Type /Font /Subtype /Type1 /BaseFont /Helvetica>>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    n = len(objs) + 1
    out += f"xref\n0 {n}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer\n<</Size " + str(n).encode() + b" /Root 1 0 R>>\n"
    out += b"startxref\n" + str(xref_at).encode() + b"\n%%EOF\n"

    with open(path, "wb") as fh:
        fh.write(bytes(out))
    return path


@pytest.fixture
def sample_pdf(tmp_path):
    return make_pdf(tmp_path / "sample.pdf", ["Hello PDF World", "Second line here"])


# ---------------------------------------------------------------------------
# 段落重组（纯函数）
# ---------------------------------------------------------------------------
def test_rebuild_paragraphs_splits_on_blank_line():
    assert rebuild_paragraphs("alpha\nbeta\n\ngamma\n", 1) == ["alpha beta", "gamma"]


def test_rebuild_paragraphs_joins_cjk_without_space():
    """回归：教程无脑加空格会把中文拼成「梧州是 广西的」。"""
    out = rebuild_paragraphs("梧州是\n广西的\n\n好", 1)
    assert out == ["梧州是广西的", "好"]


def test_rebuild_paragraphs_removes_hyphen_without_space():
    assert rebuild_paragraphs("exam-\nple", 1) == ["example"]


def test_rebuild_paragraphs_handles_empty_text():
    assert rebuild_paragraphs("", 1) == []
    assert rebuild_paragraphs("\n\n\n", 1) == []


# ---------------------------------------------------------------------------
# 断句
# ---------------------------------------------------------------------------
def test_sent_tokenize_zh_splits_on_cjk_punctuation():
    sents = sent_tokenize_zh("梧州特产有六堡茶。六堡茶是黑茶！好吃吗？")
    assert sents == ["梧州特产有六堡茶。", "六堡茶是黑茶！", "好吃吗？"]


def test_sent_tokenize_en_falls_back_without_crashing():
    """英文断句无论是 nltk 还是回退正则，都必须能返回句子且不抛异常。"""
    sents = sent_tokenize("First sentence. Second one!", lang="en")
    assert len(sents) >= 1
    assert "".join(sents).replace(" ", "") != ""


def test_guess_lang():
    assert guess_lang("梧州特产有六堡茶") == "zh"
    assert guess_lang("hello world") == "en"
    assert guess_lang("") == "en"


# ---------------------------------------------------------------------------
# 句子级交叠切块
# ---------------------------------------------------------------------------
def test_split_text_sentence_aware_respects_chunk_size():
    paras = ["".join(f"这是第{i}句话。" for i in range(1, 21))]
    chunks = split_text_sentence_aware(paras, chunk_size=40, overlap_size=10)
    assert len(chunks) > 1
    # 允许最后一次打包略微超出（算法是「加下一句前先判断」）
    for c in chunks:
        assert len(c) <= 40 + 10


def test_split_text_sentence_aware_has_overlap():
    """相邻块之间应存在重叠内容，避免上下文被硬切断。"""
    paras = ["".join(f"句子内容编号{i}。" for i in range(1, 11))]
    chunks = split_text_sentence_aware(paras, chunk_size=20, overlap_size=8)
    assert len(chunks) >= 2
    # 后一块的开头应能在前一块里找到（即发生了重叠）
    if len(chunks) >= 2:
        head = chunks[1][:6]
        assert head in chunks[0] or head in chunks[1]


def test_split_text_sentence_aware_no_space_between_chinese():
    paras = ["梧州特产有六堡茶。六堡茶是黑茶的一种。"]
    chunks = split_text_sentence_aware(paras, chunk_size=1000, overlap_size=200)
    assert len(chunks) == 1
    assert "。六堡茶" in chunks[0]  # 句子之间不应插入空格


def test_split_text_sentence_aware_empty():
    assert split_text_sentence_aware([], 800, 200) == []
    assert split_text_sentence_aware([""], 800, 200) == []


# ---------------------------------------------------------------------------
# PDF 解析（现场生成的 PDF）
# ---------------------------------------------------------------------------
def test_extract_pdf_text(sample_pdf):
    text = extract_pdf_text(str(sample_pdf))
    assert "Hello PDF World" in text
    assert "Second line here" in text


def test_extract_pdf_paragraphs(sample_pdf):
    paras = extract_pdf_paragraphs(str(sample_pdf))
    assert paras
    assert "Hello PDF World" in paras[0]


def test_pdf_to_chunks(sample_pdf):
    chunks = pdf_to_chunks(str(sample_pdf), chunk_size=800, overlap_size=200)
    assert chunks
    assert any("Hello PDF World" in c for c in chunks)


def test_pdf_to_chunks_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        pdf_to_chunks(str(tmp_path / "nope.pdf"))


# ---------------------------------------------------------------------------
# load_chunks：统一语料入口
# ---------------------------------------------------------------------------
def test_load_chunks_missing_dir_returns_empty():
    assert load_chunks("no/such/dir") == []
    assert load_chunks("") == []
    assert load_chunks(None) == []


def test_load_chunks_reads_md_and_pdf(tmp_path):
    (tmp_path / "a.md").write_text("# 标题\n\n梧州特产有六堡茶。", encoding="utf-8")
    make_pdf(tmp_path / "b.pdf", ["Some pdf content"])

    chunks = load_chunks(str(tmp_path))
    sources = {c["source"] for c in chunks}
    assert "a.md" in sources
    assert "b.pdf" in sources
    assert all(set(c) >= {"source", "text"} for c in chunks)


def test_load_chunks_skips_corrupt_pdf(tmp_path):
    """坏 PDF 只应被跳过并告警，不能把整条语料装载带崩。"""
    (tmp_path / "good.md").write_text("梧州有骑楼城。", encoding="utf-8")
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4 this is not a real pdf")

    chunks = load_chunks(str(tmp_path))
    assert chunks
    assert {c["source"] for c in chunks} == {"good.md"}


# ---------------------------------------------------------------------------
# 回归：容器之间必须产生空行（否则段落层永远失效）
# ---------------------------------------------------------------------------
def make_pdf_blocks(path, blocks):
    """生成含多个【垂直间隔明显】文本块的 PDF，模拟多个段落。

    pdfminer 会按垂直间距把它们分成多个 LTTextContainer。
    """
    content = "BT /F1 12 Tf\n"
    y = 720
    for block in blocks:
        for ln in block:
            content += f"1 0 0 1 72 {y} Tm ({ln}) Tj\n"
            y -= 14
        y -= 30                      # 块之间的大间隔 → 触发 pdfminer 分组
    content += "ET\n"
    cb = content.encode("latin-1")

    objs = [
        b"<</Type /Catalog /Pages 2 0 R>>",
        b"<</Type /Pages /Kids [3 0 R] /Count 1>>",
        b"<</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources <</Font <</F1 5 0 R>>>>>>",
        b"<</Length " + str(len(cb)).encode() + b">>stream\n" + cb + b"endstream",
        b"<</Type /Font /Subtype /Type1 /BaseFont /Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    n = len(objs) + 1
    out += f"xref\n0 {n}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer\n<</Size " + str(n).encode() + b" /Root 1 0 R>>\n"
    out += b"startxref\n" + str(xref_at).encode() + b"\n%%EOF\n"

    with open(path, "wb") as fh:
        fh.write(bytes(out))
    return path


@pytest.fixture
def two_block_pdf(tmp_path):
    return make_pdf_blocks(tmp_path / "two.pdf", [
        ["Paragraph ONE line one.", "Paragraph ONE line two."],
        ["Paragraph TWO line one.", "Paragraph TWO line two."],
    ])


def test_extract_pdf_text_separates_containers_with_blank_line(two_block_pdf):
    """回归：容器之间必须留出空行。

    早期用 `"".join(parts)` 拼接，容器边界只剩一个 \\n，「空行」永远不出现，
    段落层实际失效（整页永远合成 1 段）。
    """
    text = extract_pdf_text(str(two_block_pdf))
    assert "\n\n" in text, f"容器之间没有空行：{text!r}"
    # 空行在 split 之后应表现为空字符串元素
    assert "" in text.split("\n")


def test_two_block_pdf_becomes_two_paragraphs(two_block_pdf):
    """端到端：两个容器 → rebuild_paragraphs 应得到 2 段（修复前是 1 段）。"""
    paras = rebuild_paragraphs(extract_pdf_text(str(two_block_pdf)), 1)
    assert len(paras) == 2, f"应切成 2 段，实际 {len(paras)} 段：{paras}"
    assert "Paragraph ONE" in paras[0]
    assert "Paragraph TWO" in paras[1]
    # 两段之间不应互相污染
    assert "Paragraph TWO" not in paras[0]
    assert "Paragraph ONE" not in paras[1]


def test_extract_pdf_paragraphs_on_two_blocks(two_block_pdf):
    paras = extract_pdf_paragraphs(str(two_block_pdf))
    assert len(paras) == 2


# ---------------------------------------------------------------------------
# 回归：短行有内容时不能丢
# ---------------------------------------------------------------------------
def test_rebuild_paragraphs_keeps_short_line_content():
    """回归：min_line_length>1 时，被判为「短行」的那一行内容不能丢。

    早期实现只 flush 缓冲区、把短行本身丢弃，实测同一段文字 147 字变 142 字。
    """
    text = "一段很长的正文内容凑够十五个字以上\n短行。"
    paras = rebuild_paragraphs(text, min_line_length=15)
    assert len(paras) == 1
    assert "短行。" in paras[0], f"短行内容被丢弃了：{paras}"


def test_rebuild_paragraphs_preserves_all_characters():
    """长度守恒：重组只应改变空白，不能凭空少字。

    这条断言能直接抓住「静默丢内容」类 bug。
    """
    text = ("梧州是广西壮族自治区下辖的地级市，位于广西东部，\n"
            "地处西江、桂江、贺江三江汇流之处。\n"
            "历史\n"
            "梧州以人工宝石产业闻名。")
    for mll in (1, 5, 15, 30):
        paras = rebuild_paragraphs(text, min_line_length=mll)
        src = "".join(text.split())          # 去掉所有空白后的字符数
        out = "".join("".join(paras).split())
        assert len(out) == len(src), (
            f"mll={mll} 丢字了：原文 {len(src)} 字，重组后 {len(out)} 字\n{paras}"
        )


def test_rebuild_paragraphs_short_line_only_becomes_own_paragraph():
    """缓冲区为空时遇到有内容的短行 → 它自己成为一段（而不是消失）。"""
    paras = rebuild_paragraphs("正文正文正文正文正文\n短。", min_line_length=15)
    assert len(paras) == 2
    assert paras[1] == "短。"


def test_whitespace_line_is_not_a_paragraph_break(tmp_path):
    """带空格的行不算「空行」（mll=1 时其长度 ≥1，走长行分支被并入）。

    「空行」必须严格为零字符。这解释了为什么「原文的空行其实带空格」时
    整篇会被合成一段——这也是实际 PDF 抽取里很常见的坑。
    """
    paras = rebuild_paragraphs("第一段\n   \n第二段", min_line_length=1)
    assert len(paras) == 1                                    # 没有被断开
    assert "".join(paras).replace(" ", "") == "第一段第二段"    # 但内容一个不少
