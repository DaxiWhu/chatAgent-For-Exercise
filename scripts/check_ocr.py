"""OCR 链路自检：现场造"图片版 PDF"（无文字层），验证 OCR 真能把文字捞回来。

为什么需要它：

  1. **RapidOCR 权重是懒加载的**——缺了不会在 import 或建 converter 时报错，
     只在真跑到扫描件那一刻才炸。只拿电子版 PDF 测，永远发现不了这个问题。
  2. 手上没有真实扫描件语料时，这是唯一能独立验证 OCR 权重可用性的手段。
  3. 反过来也是保护：`DOCLING_OCR=auto` 的判据是"文字层是否够厚"，
     本脚本顺带确认这个判据在真实的无文字层文件上确实会开 OCR。
  4. **中文必须单独验**（--sample cn）：中文语料是主场景，而 docling 的
     `RapidOcrOptions.lang` 默认值是 ch 只是"碰巧"——本脚本把这件事变成可验证的事实，
     而不是一条注释。中文样件同时带一张三行表格，用于看清"扫描件上的表格能否抽出结构"。

造件手法：用 PIL 把文字画成位图，再整体存成 PDF。PDF 里只有一张图、没有文字对象，
与扫描件的结构等价，且不需要引入 PyMuPDF/poppler 这类额外依赖。

用法：
    D:/miniConda/envs/langchain1.2/python.exe scripts/check_ocr.py              # 英文样本
    D:/miniConda/envs/langchain1.2/python.exe scripts/check_ocr.py --sample cn  # 中文样本（+表格）

退出码非 0 表示 OCR 没捞回预期文字（权重缺失、语言不匹配或渲染太糊）。
"""
import argparse
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

# --- 中文样本 ---
CN_TITLE = "季度销售报告"
CN_HEADER = ["地区", "销售额"]
CN_ROWS = [["华东", "1200"], ["华南", "980"]]
CN_KEYWORDS = ("季度", "销售", "华东", "1200")

_FONT_CANDIDATES = ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf", "calibri.ttf")
# 中文必须用带 CJK 字形的字体，否则画出来是豆腐块，测的就不是 OCR
_CN_FONT_CANDIDATES = ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc",
                       "Deng.ttf", "NotoSansCJK-Regular.ttc")


def _font(size: int, chinese: bool = False):
    """尽量取一个 TrueType 字体。

    PIL 的默认位图字体只有 11px，渲染出来笔画粘连，OCR 几乎必错——
    那样测出来的是字体问题而不是 OCR 问题，所以优先找真字体。
    """
    from PIL import ImageFont

    for name in (_CN_FONT_CANDIDATES if chinese else ()) + _FONT_CANDIDATES:
        for cand in (name, os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)):
            try:
                return ImageFont.truetype(cand, size)
            except Exception:
                continue
    if chinese:
        raise RuntimeError("找不到中文字体（msyh/simhei/simsun 都没有），无法造中文样本")
    print("  [警告] 没找到 TrueType 字体，退回 PIL 内置小字体（OCR 可能识别不准）")
    return ImageFont.load_default()


def build_scanned_pdf(path: str, lines=None, font_size: int = 48) -> str:
    """生成一份"图片版 PDF"：文字全在位图里，PDF 不含任何文字对象。"""
    from PIL import Image, ImageDraw

    lines = lines or SAMPLE_LINES
    img = Image.new("RGB", (1400, 240 + 90 * len(lines)), "white")
    draw = ImageDraw.Draw(img)
    font = _font(font_size)
    y = 60
    for ln in lines:
        draw.text((60, y), ln, fill="black", font=font)
        y += 90
    img.save(path, "PDF", resolution=150)
    return path


def build_chinese_pdf(path: str) -> str:
    """生成一份中文"图片版"对账单：标题 + 表头 + 两行数据（含一根竖线分隔）。

    为什么要带表格：批次 7 的验收标准是"扫描件能抽出表格且数值核对通过"。
    表格是本项目最难的一类（TableFormer 要有真实框线/对齐线索才认得出结构），
    所以把结构线索画进去（表头加粗感、行列对齐、列分隔线），
    再如实报告"抽成了表格还是只抽到文字"——**用数据判断要不要上 MinerU**。
    """
    from PIL import Image, ImageDraw

    W, H = 1000, 560
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    tf, hf, rf = _font(40, True), _font(30, True), _font(30, True)

    d.text((60, 40), CN_TITLE, fill="black", font=tf)

    x0, y0 = 60, 140
    col_w, row_h = [260, 220], [60, 60, 60]
    grid = [CN_HEADER] + CN_ROWS
    total_h = sum(row_h)
    d.rectangle([x0, y0, x0 + sum(col_w), y0 + total_h], outline="black", width=3)
    # 竖线：给表格结构一个明确的框线线索
    d.line([(x0 + col_w[0], y0), (x0 + col_w[0], y0 + total_h)], fill="black", width=3)
    y = y0
    for r, row in enumerate(grid):
        d.line([(x0, y), (x0 + sum(col_w), y)], fill="black", width=3 if r == 0 else 2)
        x = x0
        for c, cell in enumerate(row):
            d.text((x + 24, y + 16), cell, fill="black", font=hf if r == 0 else rf)
            x += col_w[c]
        y += row_h[r]
    img.save(path, "PDF", resolution=150)
    return path


def _report(text: str, keywords, label: str) -> bool:
    low = text.lower()
    hit = [k for k in keywords if k.lower() in low]
    print(f"  命中关键词：{hit or '（无）'}  期望至少 1 个来自 {keywords}")
    if not hit:
        print(f"\n[失败] {label} OCR 没捞回预期文字。排查方向：")
        print("  - RapidOCR 权重是否齐：<artifacts>/RapidOcr/ 下应有 det/cls/rec 三个 onnx")
        print(f"  - 语言是否匹配：当前 DOCLING_OCR_LANG={docling_loader.DOCLING_OCR_LANG}，"
              "预取与运行时都必须一致（默认 onnxruntime:ch）")
        print("  - 若用 --skip-rapidocr 下过模型，重跑 scripts/fetch_docling_models.py")
        print("  - 合成件的字体是否真的带 CJK 字形（方块渲染出来 OCR 必然读不出）")
        return False
    return True


def run_sample(kind: str, tmpdir: str) -> bool:
    """跑一个样本，返回是否通过。"""
    if kind == "cn":
        pdf = build_chinese_pdf(os.path.join(tmpdir, "scanned_cn.pdf"))
        keywords, label = CN_KEYWORDS, "中文"
    else:
        pdf = build_scanned_pdf(os.path.join(tmpdir, "scanned.pdf"))
        keywords, label = KEYWORDS, "英文"
    print(f"\n=== {label}样本：{pdf}（{os.path.getsize(pdf) / 1024:.1f} KB）===")

    # --- 前置检查：确认它真的没有文字层，否则下面测的还是老链路 ---
    from src.pdf_parser import extract_pdf_text
    raw = (extract_pdf_text(pdf) or "").strip()
    print(f"文字层探测：pdfminer 抽出 {len(raw)} 字符 —— "
          f"{'OK 无文字层（等价扫描件）' if not raw else '[失败] 仍有文字层，测不出 OCR'}")
    if raw:
        return False

    want = docling_loader._want_ocr(pdf, "pdf")
    print(f"DOCLING_OCR={docling_loader.DOCLING_OCR} → want_ocr={want}"
          f"（lang={docling_loader.DOCLING_OCR_LANG} mode={docling_loader.DOCLING_OCR_MODE}）")
    if not want:
        print("[失败] auto 模式下无文字层文件却不开 OCR，扫描件会解析成空。")
        return False

    print("走 parse_document 完整链路（CPU 上 1 页 OCR 约数十秒）...")
    t0 = time.perf_counter()
    out = docling_loader.parse_document(pdf, os.path.basename(pdf), kind="pdf")
    elapsed = time.perf_counter() - t0
    print(f"  status={out.status}｜pages={out.page_total}｜chunks={len(out.chunks)}"
          f"｜耗时 {elapsed:.1f}s")
    if out.reason:
        print(f"  reason={out.reason}")

    text = " ".join((c.retrieval_text or "") for c in out.chunks)
    print(f"  OCR 捞回的文字（{len(text)} 字符）：")
    for line in text.splitlines()[:10]:
        print(f"    {line[:160]}")

    ok = _report(text, keywords, label)

    if kind == "cn":
        # 关键诊断：扫描件上的表格到底是"抽成表格"还是"抽成一行文字"。
        # 这个数字就是"要不要换 MinerU"的判据（方案 §4.2）。
        tables = [c for c in out.chunks if c.kind == "table"]
        print(f"  【表格判定】kind=table 的块：{len(tables)} 个"
              f"（0 个说明 TableFormer 没认出结构，只拿到文字）")
        for t in tables:
            print(f"    section_path={t.section_path!r}｜needs_vlm={t.needs_vlm}")
            print(f"    table_md={t.table_md!r}")
            # 标题在不在：不在 section_path 也不在正文里，说明 OCR 漏了它 ——
            # 这种"少了一行"的问题只看关键词命中是发现不了的（关键词恰好都在表里）。
            blob = f"{t.section_path}{t.content}{t.retrieval_text}"
            if CN_TITLE not in blob:
                print(f"    ⚠️ 标题 {CN_TITLE!r} 未出现在该块中（OCR 或版面分类漏了标题区）")
        if not tables:
            print("  → 注意：OCR 文字拿到了但表格结构没重建。若真实语料也如此，"
                  "再考虑 MinerU（方案 §4.2），不要凭一个合成件就换后端。")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", choices=("en", "cn"), default="en",
                    help="英文样本（默认）或中文样本（中文语料务必跑一次）")
    args = ap.parse_args()

    if not docling_loader.docling_available():
        print("[失败] docling 未安装")
        return 1

    tmpdir = tempfile.mkdtemp(prefix="check_ocr_")
    ok = run_sample(args.sample, tmpdir)
    if ok and args.sample == "en":
        print("\n提示：中文语料请再跑一次 --sample cn（验证 lang 配置与中文识别）。")

    if not ok:
        return 1
    print("\nOCR 链路自检通过。")
    print(f"测试文件保留在：{tmpdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

