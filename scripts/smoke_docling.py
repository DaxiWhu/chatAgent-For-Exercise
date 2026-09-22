"""Docling 烟测：现场造一份含表格的 PDF，跑通完整解析链路，打印统计与耗时。

为什么需要它：Docling 首次运行要下载模型（数百 MB），且当前环境是 CPU 版 torch，
每页有秒级推理开销。灌库前先用它预热并实测耗时，避免第一次跑在正式语料上才发现问题。
另外它顺带验证表格识别是否真的工作——手写测试 PDF 里画了真实线条，TableFormer 应当认出来。

用法：
    D:/miniConda/envs/langchain1.2/python.exe scripts/smoke_docling.py [PDF路径]

不给路径时自动生成一份测试 PDF（英文文本 + 线条表格）。首次运行请留出下载模型的时间。
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import docling_loader  # noqa: E402


def build_test_pdf(path: str) -> str:
    """手搓一份最小 PDF：一段说明文字 + 一个 3 行 2 列的真实线条表格。

    只用 Helvetica ASCII 文本——中文需要嵌入 CJK 字体，手搓成本高；
    验证表格识别用英文足够，中文交给真实语料测。
    """
    lines = [
        "BT /F1 14 Tf 72 740 Td (Sales Report 2023) Tj ET",
        "BT /F1 11 Tf 72 712 Td (The table below shows revenue by region.) Tj ET",
    ]
    # 3 行 x 2 列的格子（re 的 y 是底边）
    for row_y in (640, 612, 584):
        for col_x in (100, 250):
            lines.append(f"1 w {col_x} {row_y} 150 28 re S")
    cells = [
        (110, 650, "Region"), (260, 650, "Revenue"),
        (110, 622, "East"), (260, 622, "1200"),
        (110, 594, "West"), (260, 594, "980"),
    ]
    for x, y, text in cells:
        lines.append(f"BT /F1 11 Tf {x} {y} Td ({text}) Tj ET")

    stream = ("\n".join(lines) + "\n").encode("latin-1")

    objs = [
        b"<</Type /Catalog /Pages 2 0 R>>",
        b"<</Type /Pages /Kids [3 0 R] /Count 1>>",
        b"<</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources <</Font <</F1 5 0 R>>>>>>",
        b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"endstream",
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


def main() -> int:
    if len(sys.argv) > 1:
        pdf = sys.argv[1]
        tmpdir = None
    else:
        tmpdir = tempfile.mkdtemp(prefix="smoke_docling_")
        pdf = build_test_pdf(os.path.join(tmpdir, "smoke.pdf"))
        print(f"已生成测试 PDF：{pdf}")

    print(f"\n可用性: {docling_loader.docling_available()}")
    print(f"配置: device={docling_loader.DOCLING_DEVICE} ocr={docling_loader.DOCLING_OCR} "
          f"tokens={docling_loader.DOC_CHUNK_TOKENS} tokenizer={docling_loader.TOKENIZER_MODE}")

    # --- 第一步：底层统计（首次运行会在此处下载模型）---
    print("\n[1/3] 构建 converter 并转换（首次会下载模型，请耐心等）...")
    t0 = time.perf_counter()
    try:
        conv = docling_loader.get_converter(ocr=False)
        result = conv.convert(pdf, raises_on_error=False)
    except Exception as exc:
        print(f"  [失败] {type(exc).__name__}: {exc}")
        return 1
    t_convert = time.perf_counter() - t0

    doc = getattr(result, "document", None)
    status = getattr(getattr(result, "status", None), "value", "?")
    if doc is None:
        print(f"  [失败] 无 document，status={status}")
        return 1

    pages = getattr(doc, "pages", {}) or {}
    print(f"  耗时 {t_convert:.1f}s｜status={status}｜页数={len(pages)}")
    print(f"  ★ len(doc.tables) = {len(doc.tables)}   ← 最关键：已知有 1 张表，应为 1")
    print(f"  len(doc.pictures) = {len(doc.pictures)}")

    if doc.tables:
        md = doc.tables[0].export_to_markdown(doc=doc)
        print("  第一张表的 Markdown：")
        for line in md.splitlines():
            print(f"    {line}")

    # --- 第二步：tokenizer ---
    print("\n[2/3] 构建切分 tokenizer ...")
    t0 = time.perf_counter()
    tok = docling_loader.build_tokenizer()
    print(f"  {type(tok).__name__}｜max_tokens={tok.get_max_tokens()}｜"
          f"耗时 {time.perf_counter() - t0:.2f}s")

    # --- 第三步：完整链路 ---
    print("\n[3/3] 走 parse_document 完整链路 ...")
    t0 = time.perf_counter()
    out = docling_loader.parse_document(pdf, os.path.basename(pdf), kind="pdf")
    t_parse = time.perf_counter() - t0
    print(f"  status={out.status}｜pages={out.page_total}｜chunks={len(out.chunks)}"
          f"｜耗时 {t_parse:.1f}s")
    if out.reason:
        print(f"  reason={out.reason}")

    from src.chunk_writer import format_summary, to_legacy
    print(f"  汇总：{format_summary(_as_summary(out.chunks))}")

    for i, kc in enumerate(out.chunks, start=1):
        text = (kc.retrieval_text or "").replace("\n", " ⏎ ")
        print(f"\n  [{i}] kind={kc.kind} page={kc.page} needs_vlm={kc.needs_vlm} "
              f"parser={kc.parser}")
        print(f"      section={kc.section_path!r}")
        print(f"      bbox={kc.bbox}")
        print(f"      text={text[:160]}")
        if kc.table_md:
            print(f"      table_md={kc.table_md.splitlines()[0][:70]}...")

    # 兼容层契约自检
    bad = [d for d in (to_legacy(c) for c in out.chunks) if not (d.get("source") and "text" in d)]
    print(f"\n  to_legacy 契约：{'OK' if not bad else f'违规 {len(bad)} 条'}")

    if tmpdir:
        print(f"\n测试 PDF 保留在：{pdf}")
    print("\n烟测结束。")
    return 0


def _as_summary(chunks) -> dict:
    from src.chunk_writer import summarize
    return summarize(chunks)


if __name__ == "__main__":
    raise SystemExit(main())
