"""OCR 链路自检：现场造一份"图片版 PDF"（无文字层），验证 OCR 真能把文字捞回来。

为什么需要它：

  1. **RapidOCR 权重是懒加载的**——缺了不会在 import 或建 converter 时报错，
     只在真跑到扫描件那一刻才炸。只拿电子版 PDF 测，永远发现不了这个问题。
  2. 手上没有真实扫描件语料时，这是唯一能独立验证 OCR 权重可用性的手段。
  3. 反过来也是保护：`DOCLING_OCR=auto` 的判据是"文字层是否够厚"，
     本脚本顺带确认这个判据在真实的无文字层文件上确实会开 OCR。

造件手法：用 PIL 把文字画成位图，再整体存成 PDF。PDF 里只有一张图、没有文字对象，
与扫描件的结构等价，且不需要引入 PyMuPDF/poppler 这类额外依赖。

用法：
    D:/miniConda/envs/langchain1.2/python.exe scripts/check_ocr.py

退出码非 0 表示 OCR 没捞回预期文字（权重缺失、语言不匹配或渲染太糊）。
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import docling_loader  # noqa: E402

# OCR 应当能认出来的样本文本。用英文短句：手头没有可直接嵌入的 CJK 字体时，
# 画中文会变成方块，测的就不是 OCR 而是字体缺失了。
SAMPLE_LINES = ["Hello Docling World", "Invoice Number 2024"]
# 判定成功的关键词：取自样本，且 OCR 输出大小写可能不稳，故统一小写比对
KEYWORDS = ("docling", "invoice", "hello")

_FONT_CANDIDATES = ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf", "calibri.ttf")


def _load_font(size: int):
    """尽量取一个 TrueType 字体。

    PIL 的默认位图字体只有 11px，渲染出来笔画粘连，OCR 几乎必错——
    那样测出来的是字体问题而不是 OCR 问题，所以优先找真字体。
    """
    from PIL import ImageFont

    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    print("  [警告] 没找到 TrueType 字体，退回 PIL 内置小字体（OCR 可能识别不准）")
    return ImageFont.load_default()


def build_scanned_pdf(path: str, lines=None, font_size: int = 48) -> str:
    """生成一份"图片版 PDF"：文字全在位图里，PDF 不含任何文字对象。"""
    from PIL import Image, ImageDraw

    lines = lines or SAMPLE_LINES
    img = Image.new("RGB", (1400, 240 + 90 * len(lines)), "white")
    draw = ImageDraw.Draw(img)
    font = _load_font(font_size)
    y = 60
    for ln in lines:
        draw.text((60, y), ln, fill="black", font=font)
        y += 90
    img.save(path, "PDF", resolution=150)
    return path


def main() -> int:
    if not docling_loader.docling_available():
        print("[失败] docling 未安装")
        return 1

    tmpdir = tempfile.mkdtemp(prefix="check_ocr_")
    pdf = build_scanned_pdf(os.path.join(tmpdir, "scanned.pdf"))
    size_kb = os.path.getsize(pdf) / 1024
    print(f"已生成图片版 PDF：{pdf}（{size_kb:.1f} KB）")

    # --- 前置检查：确认它真的没有文字层，否则下面测的还是老链路 ---
    from src.pdf_parser import extract_pdf_text
    raw = (extract_pdf_text(pdf) or "").strip()
    print(f"\n[1/3] 文字层探测：pdfminer 抽出 {len(raw)} 字符"
          f"（{raw[:40]!r}{'...' if len(raw) > 40 else ''}）")
    if raw:
        print("  [失败] 这份文件仍有文字层，测不出 OCR。请检查造件逻辑。")
        return 1
    print("  OK —— 无文字层，等价于扫描件")

    # --- auto 判据应当开 OCR ---
    want = docling_loader._want_ocr(pdf, "pdf")
    print(f"\n[2/3] DOCLING_OCR={docling_loader.DOCLING_OCR} → 判据结果 want_ocr={want}")
    if not want:
        print("  [失败] auto 模式下无文字层文件却不开 OCR，扫描件会解析成空。")
        return 1

    # --- 真跑一遍 ---
    print("\n[3/3] 走 parse_document 完整链路（CPU 上 1 页 OCR 约数秒）...")
    t0 = time.perf_counter()
    out = docling_loader.parse_document(pdf, "scanned.pdf", kind="pdf")
    elapsed = time.perf_counter() - t0
    print(f"  status={out.status}｜pages={out.page_total}｜chunks={len(out.chunks)}"
          f"｜耗时 {elapsed:.1f}s")
    if out.reason:
        print(f"  reason={out.reason}")

    text = " ".join((c.retrieval_text or "") for c in out.chunks)
    print(f"\n  OCR 捞回的文字（{len(text)} 字符）：")
    for line in text.splitlines()[:12]:
        print(f"    {line[:160]}")

    low = text.lower()
    hit = [k for k in KEYWORDS if k in low]
    print(f"\n  命中关键词：{hit or '（无）'}  期望至少 1 个来自 {KEYWORDS}")
    if not hit:
        print("\n[失败] OCR 没捞回预期文字。排查方向：")
        print("  - RapidOCR 权重是否齐：<artifacts>/RapidOcr/ 下应有 det/cls/rec 三个 onnx")
        print("  - 语言是否匹配：预取与运行时都应为 onnxruntime:ch")
        print("  - 若用 --skip-rapidocr 下过模型，重跑 scripts/fetch_docling_models.py")
        return 1

    print("\nOCR 链路自检通过。")
    print(f"测试文件保留在：{pdf}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
