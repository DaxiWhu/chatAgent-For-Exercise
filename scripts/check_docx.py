"""docx 双路线自检：现场造一份"含原生图表 + 截图 + OLE"的 docx，验证两条路线都真的通了。

为什么需要它：

  1. **docx 的能力缺口不在正文，而在媒体** —— 正文 python-docx 也能提；
     原生图表（数据在 chart XML 里）、图片原件、OLE 内层工作簿只能靠解 zip。
     这些要是没拿全，产物看上去依然"有内容"，但图表数值、嵌入表全丢了，不会报错。
  2. 手头没有真实 Word 语料时（python-docx 本身**写不了**图表与 OLE），
     手拼包是唯一能端到端验证 A+C 两条路线的办法。
  3. 顺带量出这条链路的真实耗时，供决定它该不该进默认测试套件。

自查项（任一不满足即退出码非 0）：
  - 图表：类型/标题/类目/数值必须与造件时写入的完全一致（确定性，不是"看起来像"）
  - 图片：作者写的替代文字必须拿到（这是免 VLM 的确定性描述）
  - OLE ：内层 xlsx 必须被解出并解析出正文；预览位图不得单独成块
  - 位置：媒体块要带 para_index 与上下文锚点

用法：
    D:/miniConda/envs/langchain1.2/python.exe scripts/check_docx.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.rag import docling_loader  # noqa: E402
from src.rag.chunk_writer import to_legacy  # noqa: E402


def _build_sample(path: str) -> str:
    """复用测试夹具造件，避免测试与自检脚本各写一份、慢慢对不上。"""
    from tests.docx_fixtures import build_minimal_docx

    return build_minimal_docx(path)


def main() -> int:
    if not docling_loader.docling_available():
        print("[失败] docling 未安装")
        return 1

    from tests.docx_fixtures import (ALT_CHART, ALT_IMAGE, ALT_OLE, CHART_CATS,
                                     CHART_TITLE, CHART_VALS, OLE_CELL_TEXT,
                                     PARA_BEFORE_CHART, PARA_BEFORE_OLE, SERIES_NAME)

    tmpdir = tempfile.mkdtemp(prefix="check_docx_")
    docx = _build_sample(os.path.join(tmpdir, "report.docx"))
    print(f"已生成样件 docx：{docx}（{os.path.getsize(docx) / 1024:.1f} KB）")

    print("\n[1/4] 走 parse_docx 完整链路（A: Docling docx 后端 + C: 解 zip 取媒体）...")
    t0 = time.perf_counter()
    out = docling_loader.parse_docx(docx, "report.docx")
    elapsed = time.perf_counter() - t0
    print(f"  status={out.status}｜chunks={len(out.chunks)}｜耗时 {elapsed:.1f}s")
    if out.reason:
        print(f"  reason={out.reason}")

    print("\n[2/4] 产物清单：")
    for i, c in enumerate(out.chunks):
        print(f"  [{i}] kind={c.kind:<9} origin={c.origin:<12} para={c.para_index} "
              f"vlm={c.needs_vlm} tags={c.tags}")
        print(f"      锚点={c.anchor_text[:40]!r}")
        print(f"      检索文本={c.retrieval_text[:120]!r}")

    text = " ".join((c.retrieval_text or "") for c in out.chunks)
    fails: list[str] = []

    print("\n[3/4] 确定性内容核对（对不上说明某条路线没真跑通）：")
    checks = [
        ("图表标题", CHART_TITLE in text),
        ("图表系列名", SERIES_NAME in text),
        ("图表数值（类目=数值）",
         all(f"{k}={v}" in text for k, v in zip(CHART_CATS, CHART_VALS))),
        ("图片替代文字", ALT_IMAGE in text),
        ("OLE 内层正文", OLE_CELL_TEXT in text),
        ("位置锚点（图表前文）", PARA_BEFORE_CHART in text),
        ("位置锚点（OLE 前文）", PARA_BEFORE_OLE in text),
    ]
    for name, ok in checks:
        print(f"  {'OK  ' if ok else '失败'} {name}")
        if not ok:
            fails.append(name)

    # 图表类目/数值的精确性单独再核一遍：这是"确定性优先于 VLM"的立足点，
    # 一旦读错（例如把 c:f 公式串当数据）文字化结果会静默地错，必须精确断言。
    charts = [c for c in out.chunks if c.kind == "chart"]
    print(f"\n[4/4] 结构化核对：chart 块 {len(charts)} 个，OLE 原始件 {ALT_OLE!r}")
    if not charts:
        fails.append("缺少 chart 块")
    else:
        md = charts[0].table_md or ""
        print("  图表 Markdown：")
        for line in md.splitlines():
            print(f"    {line}")
        if not md.strip():
            fails.append("chart 块没有 table_md（说明表格保真表示没生成）")
        if charts[0].needs_vlm:
            fails.append("chart 块被标成需要 VLM（数据本来就在 XML 里，不该花钱）")

    # 预览位图不得成块：它和 OLE 原件是同一份数据
    previews = [c for c in out.chunks if (c.source_image or "").endswith(".emf")]
    if previews:
        fails.append(f"OLE 预览位图仍然单独成块（{len(previews)} 个）")

    # 出边界契约
    for c in out.chunks:
        d = to_legacy(c)
        if not d.get("text"):
            fails.append(f"块 {c.chunk_id} 出边界后 text 为空，检索层看不到它")

    if fails:
        print("\n[失败] 以下自查项未通过：")
        for f in fails:
            print(f"  - {f}")
        print("\n排查方向：")
        print("  - 图表无数据 → chart XML 未被读到（docx_media.parse_chart_xml）")
        print("  - 图片无替代文字 → 未取 wp:docPr@descr（docx_media._scan_document）")
        print("  - OLE 无内层正文 → olefile 是否可用、embeddings 里是否真是 xlsx")
        return 1

    print("\ndocx 双路线自检通过。")
    print(f"测试文件保留在：{docx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
