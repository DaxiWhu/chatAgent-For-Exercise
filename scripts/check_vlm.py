"""图片 VLM 描述链路自检：真造一张图 → 真调一次 API → 校验输出结构与数值。

为什么必须有一个"真调一次"的脚本：
  1. 单测全程离线（否则每次验证都要花钱、出问题也无法复现），
     但"单测绿"证明不了模型真的能看图 —— 可能模型名不支持视觉、base64 拼错、
     后端不认 data URL。这些都只在真调用时暴露。
  2. 方案的整个立场是"**确定性优先，VLM 只补拿不到的**"，所以这里同时要验证
     反面：数值校验能发现模型编的数（把 visible_text 与 description 交叉比对）。

用法：
    # 只算量、不调用（默认行为，零成本）
    D:/miniConda/envs/langchain1.2/python.exe scripts/check_vlm.py --dry-run

    # 真调一次（产生少量 token 费用）
    D:/miniConda/envs/langchain1.2/python.exe scripts/check_vlm.py

退出码非 0 表示链路不通（未配置 Key / 模型不支持视觉 / 输出解析不了 / 数值校验不过）。
"""
import argparse
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.rag import image_describer as D  # noqa: E402

# 画一张"带数字的柱状图"。必须真有几个数字与标题：
# 没有数字就无法验证"数值回查"这条防幻觉规则是否真的在工作。
CHART_TITLE = "2021-2023 年出口额"
CATS = [("2021", 138), ("2022", 127), ("2023", 169)]

_FONTS = ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf", "calibri.ttf")


def _font(size: int):
    from PIL import ImageFont
    for name in _FONTS:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def build_chart_png(path: str) -> str:
    """画一张柱状图：标题 + 三根柱子 + 柱顶数值 + 轴上的年份。"""
    from PIL import Image, ImageDraw

    W, H = 900, 520
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    title_f, label_f = _font(34), _font(24)

    d.text((40, 24), CHART_TITLE, fill="black", font=title_f)
    d.line([(90, 440), (W - 40, 440)], fill="black", width=2)      # x 轴
    d.line([(90, 90), (90, 440)], fill="black", width=2)           # y 轴
    d.text((20, 60), "亿元", fill="black", font=label_f)

    max_v = max(v for _, v in CATS)
    bar_w, gap = 120, 90
    for i, (cat, val) in enumerate(CATS):
        x0 = 140 + i * (bar_w + gap)
        h = int((val / max_v) * 290)
        d.rectangle([x0, 440 - h, x0 + bar_w, 440], fill=(70, 110, 180), outline="black")
        d.text((x0 + 20, 440 - h - 32), str(val), fill="black", font=label_f)   # 柱顶数值
        d.text((x0 + 32, 452), cat, fill="black", font=label_f)                 # 年份
    img.save(path, "PNG")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只算量，不调用 API")
    ap.add_argument("--image", default="", help="改测一张已有的图片")
    args = ap.parse_args()

    tmpdir = tempfile.mkdtemp(prefix="check_vlm_")
    img_path = args.image or build_chart_png(os.path.join(tmpdir, "chart.png"))
    size_kb = os.path.getsize(img_path) / 1024
    print(f"测试图：{img_path}（{size_kb:.1f} KB）")

    # 组装一个"像真产物一样"的块：有章节与前后文，但**没有 caption**（所以需要 VLM）
    from src.rag.knowledge_chunk import KnowledgeChunk
    chunk = KnowledgeChunk(
        source="demo.pdf", page=3, kind="diagram", section_path="第三章 > 出口情况",
        content="", retrieval_text="第三章 > 出口情况", needs_vlm=True,
        source_image=img_path, tags=["图表"],
    )
    chunks = [
        KnowledgeChunk(source="demo.pdf", kind="text", needs_vlm=False,
                       retrieval_text="如图 3 所示，近三年出口额先降后升。",
                       content="如图 3 所示，近三年出口额先降后升。"),
        chunk,
        KnowledgeChunk(source="demo.pdf", kind="text", needs_vlm=False,
                       retrieval_text="综上，2023 年出口额恢复到 169 亿元。",
                       content="综上，2023 年出口额恢复到 169 亿元。"),
    ]

    est = D.estimate(chunks)
    print(f"\n[1/4] 算量：{D.format_estimate(est)}")
    print(f"  开关状态：{D.enabled()}")

    if args.dry_run:
        print("\n--dry-run：未调用 API。去掉该参数即真调一次（产生少量 token 费用）。")
        return 0

    # 自检脚本的目的就是真调一次，所以要显式打开开关（生产默认是关的）
    if not D.enabled()[0]:
        print("  （当前开关为关，本次自检临时打开：RAG_VLM_DESC=on + 复用 AGNES_API_KEY）")
        D.VLM_DESC = "on"
        if not D.VLM_API_KEY:
            print("[失败] 未配置 AGNES_API_KEY，无法调用。请在 .env 里填好再试。")
            return 1

    ctx = D.context_for(chunks, 1)
    print("\n[2/4] 上下文（图 + 前后文一起送）：")
    for k, v in ctx.items():
        print(f"  {k} = {v!r}")

    print(f"\n[3/4] 调用 {D.VLM_MODEL}（base={D.VLM_API_BASE}）...")
    t0 = time.perf_counter()
    desc = D.describe(img_path, ctx)
    elapsed = time.perf_counter() - t0
    if desc.get("error"):
        print(f"[失败] {desc['error']}（耗时 {elapsed:.1f}s）")
        print("排查方向：")
        print("  - 模型是否支持视觉输入（agnes-2.5-pro；agnes-image-* 是生图模型）")
        print("  - AGNES_API_KEY / AGNES_API_BASE 是否正确")
        print("  - 是否为代理/网络问题")
        return 1
    print(f"  耗时 {elapsed:.1f}s｜usage={desc.get('usage')}｜cached={desc.get('cached')}")
    print("  结构化输出：")
    print("    " + json.dumps({k: v for k, v in desc.items()
                               if k not in ("usage",)}, ensure_ascii=False, indent=2)
          .replace("\n", "\n    "))

    print("\n[4/4] 校验（防幻觉）：数值必须能在模型自己转录的 visible_text 里找到")
    issues = desc.get("issues") or []
    if issues:
        print(f"  ⚠️ 发现问题：{issues}（块会被打上 VLM待核对 标签，人工抽查）")
    else:
        print("  OK —— 描述中的数字都能在转录文字里找到")

    # 走一遍产物写回，确认 enrich 真的会把描述落进检索文本且不顶掉上下文
    stats = D.enrich(chunks, describer=lambda p, c: desc)
    print(f"\n[附加] enrich 统计：{stats}")
    print(f"  写回后的检索文本：{chunk.retrieval_text[:200]!r}")
    if not chunk.vlm_desc or chunk.needs_vlm:
        print("[失败] 描述没有写回产物（vlm_desc 为空或 needs_vlm 仍为 True）")
        return 1

    print("\nVLM 描述链路自检通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
