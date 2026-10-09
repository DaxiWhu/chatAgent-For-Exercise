#!/usr/bin/env python
"""质量 scorecard：把三层指标汇总成**一页**，并支持与基线对比。

为什么要有它
------------
三层评测各自能跑，但"现在这套系统准不准"这个问题需要**同时**看三处输出。
更重要的是漏看：改一次切分，检索 Top1 可能涨了而材料覆盖率掉了；
换一次 prompt，材料不变但答案开始编 —— 这些**只有并排看才看得见**。

它汇总什么
----------
    检索      Top1 / Top3 / MRR / Recall@k / nDCG@k / Prec@k   （eval_set 的通用语料）
    材料      来源覆盖 / 关键点覆盖 / 引用可回溯              （随仓库分发的 data/）
    生成      答案可信率 / 标识符幻觉率 / 拒答正确率 / 可回溯性（同上）

⚠️ 三层各用各的语料，**不是同一个分母**：检索那层用的是 `eval_set.json` 里内嵌的
通用语料（考的是检索算法本身），后两层用的是 `data/`（考的是随仓库分发的知识库）。
并排读的时候要记得这一点，否则会拿"梧州语料的 61%"去解释"服务知识库的覆盖率"。

用哪种模型
----------
**纯 BM25 / 零外部调用**（与 CI 门禁同口径），并且是**在代码里钉死的**而不是靠
"开发机恰好没配密钥"——见 `scripts/eval_backend.py`。换 embedding 或开 rerank 都会
得到不同的数字，而 scorecard 的用途是**看趋势与退化**，口径必须每次一致。

用法
----
    python scripts/eval_scorecard.py                     # 打印并写出 scorecard.json
    python scripts/eval_scorecard.py --baseline scripts/scorecard.json   # 与上次对比，标出退化
    python scripts/eval_scorecard.py --out -             # 不写文件

⚠️ 基线是**在写输出之前**读的 —— 所以上面第二条命令（`--baseline` 与 `--out` 是同一个
文件）比较的确实是**上一轮**。反过来写（先写后读）会让每一项都算成「持平」，
一个永远说"没退化"的对比比没有对比更危险。有回归时退出码为 1。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.chdir(ROOT)

import src.rag.retrieval as R  # noqa: E402
from scripts.eval_backend import local_eval  # noqa: E402

# 需要监控的指标路径 → 方向。方向决定"变小"是退化还是改善：
#   down  = 越小越好  |  up = 越大越好
TRACKED = {
    "retrieval.top1": "up",
    "retrieval.top3": "up",
    "retrieval.mrr": "up",
    "retrieval.recall": "up",
    "retrieval.ndcg": "up",
    "retrieval.precision": "up",
    "answer.source_coverage": "up",
    "answer.keypoint_coverage": "up",
    "answer.traceable_rate": "up",
    "generation.ok_rate": "up",
    "generation.identifier_hallucination_rate": "down",
    "generation.refusal_accuracy": "up",
    "generation.avg_grounded": "up",
}


def collect_retrieval(top_k=3):
    """检索层：用 `eval_set.json` 里内嵌的通用语料，考检索算法本身。"""
    import scripts.eval_retrieval as ER

    with open(os.path.join(ROOT, "scripts", "eval_set.json"), encoding="utf-8") as _fh:
        spec = json.load(_fh)
    workdir = tempfile.mkdtemp(prefix="scorecard_")
    try:
        docs_dir, _ = ER.prepare_corpus(spec, workdir)
        chunks = R.load_chunks(docs_dir)
        res, _ = ER.evaluate(R.build_retriever(chunks), spec["queries"], top_k=top_k)
        # 保留 `n`（样本量）：只报比率而看不出分母是几条，会把"1 条 100%"
        # 读成"很稳"。`round(int)` 原样返回，不影响数值项。
        return {k: round(v, 4) for k, v in res["all"].items()}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _data_chunks(docs_dir):
    chunks = R.load_chunks(docs_dir)
    if not chunks:
        raise SystemExit(f"语料目录 {docs_dir} 加载到 0 个片段 —— scorecard 无意义")
    return chunks, R.build_retriever(chunks)


def collect_answer():
    """材料层：考随仓库分发的知识库里"喂给模型的材料够不够"。"""
    import scripts.eval_answer as EA

    with open(os.path.join(ROOT, "scripts", "eval_answer_set.json"), encoding="utf-8") as _fh:
        spec = json.load(_fh)
    docs_dir = spec.get("docs_dir") or "data"
    chunks, retriever = _data_chunks(docs_dir)
    _, summary = EA.evaluate(spec["cases"], retriever, chunks, top_k=3)
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in summary.items()}


def collect_generation():
    """生成层：考答案本身（编造 / 拒答 / 可回溯）。"""
    import scripts.eval_generation as EG

    with open(os.path.join(ROOT, "scripts", "generation_set.json"), encoding="utf-8") as _fh:
        spec = json.load(_fh)
    docs_dir = spec.get("docs_dir") or "data"
    chunks, retriever = _data_chunks(docs_dir)
    _, summary = EG.evaluate(spec["cases"], chunks=chunks, retriever=retriever, docs_dir=docs_dir)
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in summary.items()}


def collect():
    """三层**在同一个锁下**跑完。

    锁只在这里上一道（而不是每层各上一道）：`eval_answer` / `eval_generation`
    自己也各有一道，但那是给**单独跑**它们的人用的；汇总时由这里统一罩住，
    免得"哪层忘了钉"再发生一次 —— 那正是修这个问题之前的状态
    （scorecard 钉了 embedding 但没钉 rerank，另两个脚本压根没钉）。
    """
    with local_eval():
        return {
            "retrieval": collect_retrieval(),
            "answer": collect_answer(),
            "generation": collect_generation(),
        }


def _dig(d, path):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def diff_against(baseline, current):
    """逐项与基线比较，返回 [(指标, 基线, 当前, 结论)]。

    `结论` 只有三种：不进反退 / 有改善 / 持平（含阈值内的浮动）。
    浮动的容忍度取 0.005 —— 小于它就是噪声：BM25 下同一份语料的结果是确定的，
    但语料增删会带来真实的小幅变化，把它标成退化会让人对告警脱敏。
    """
    rows = []
    for path, direction in TRACKED.items():
        old, new = _dig(baseline, path), _dig(current, path)
        if old is None or new is None:
            continue
        delta = new - old
        if abs(delta) < 0.005:
            verdict = "持平"
        elif (delta > 0) == (direction == "up"):
            verdict = "有改善"
        else:
            verdict = "**不进反退**"
        rows.append((path, old, new, verdict))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join("scripts", "scorecard.json"),
                    help="scorecard 写出路径（'-' 表示不写文件）")
    ap.add_argument("--baseline", default=None, help="与这份历史 scorecard 对比")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args()

    card = collect()

    # ⚠️ 基线必须在**写文件之前**读进来。
    #    默认 `--out` 是 `scripts/scorecard.json`，而 `--baseline` 最常见的用法指的也是它
    #    （"跟上次比"）—— 先写后读会读到刚落盘的**自己**，于是每一项都算成「持平」。
    #    一个永远说"没退化"的对比，比没有对比更危险：它看起来在监控。
    base = None
    if args.baseline:
        if not os.path.exists(args.baseline):
            # 静默跳过 = 让人以为"对比过了、没问题"，所以这里必须显式拒绝。
            print(f"基线文件不存在：{args.baseline}", file=sys.stderr)
            return 2
        with open(args.baseline, encoding="utf-8") as fh:
            base = json.load(fh)

    if args.json:
        print(json.dumps(card, ensure_ascii=False, indent=2))
    else:
        r, a, g = card["retrieval"], card["answer"], card["generation"]
        print("=" * 88)
        print(f"{'层':<8}{'指标':<34}{'值':>10}   说明")
        print("-" * 88)
        for label, key, desc in [
            ("检索", "top1", "Top-1 命中（第一条对不对）"),
            ("检索", "top3", "Top-3 命中（Hit@3）"),
            ("检索", "mrr", "平均倒数排名"),
            ("检索", "recall", "Recall@k（该找回的找回了吗）"),
            ("检索", "ndcg", "nDCG@k（排序质量）"),
            ("检索", "precision", "Prec@k（Top-k 里有多少是噪音）"),
        ]:
            print(f"{label:<8}{key:<34}{r.get(key, 0):>10.4f}   {desc}")
        print("-" * 88)
        for label, key in [("材料", "source_coverage"), ("材料", "keypoint_coverage"),
                           ("材料", "traceable_rate")]:
            print(f"{label:<8}{key:<34}{a.get(key, 0):>10.4f}")
        print("-" * 88)
        for label, key in [("生成", "ok_rate"), ("生成", "identifier_hallucination_rate"),
                           ("生成", "refusal_accuracy"), ("生成", "avg_grounded")]:
            v = g.get(key)
            print(f"{label:<8}{key:<34}{('不判' if v is None else f'{v:.4f}'):>10}")
        print("=" * 88)
        print(f"用例数：检索 {r.get('n', '-')} 条（内嵌通用语料）｜"
              f"材料 {a.get('cases', '-')} 条｜"
              f"生成 {g.get('cases', '-')} 条（{g.get('answerable', '-')} 正 / "
              f"{g.get('unanswerable', '-')} 负）")
        print("口径：纯 BM25，零外部调用（与 CI 门禁一致）")

    if args.out and args.out != "-":
        # newline="\n" 是**必须**的：Windows 上默认会把 "\n" 翻译成 "\r\n"，
        # 而仓库约定磁盘上一律 LF（`tests/test_tasks_entrypoint.py` 有守卫扫源码）。
        # 这份 JSON 是评测基线，会被反复覆写、也可能被提交进仓库 —— 不写死换行符
        # 就会出现"内容没变但整份 diff 全红"的噪声。
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(card, fh, ensure_ascii=False, indent=2)
        print(f"\n已写入 {args.out}", file=sys.stderr)
        if base is None:
            # 不自动对比是**有意**的：自动对比会让"退化 → 本次红 → 顺手覆写基线 → 下次绿"
            # 变成一个**自愈门禁**，而自愈门禁等于没有门禁。对比必须是一次明确的动作。
            print(f"提示：加上 `--baseline {args.out}` 就能与上一次对比"
                  "（基线在写文件**之前**读取，所以比较的是上一轮，不是这一轮）。",
                  file=sys.stderr)

    if base is not None:
        rows = diff_against(base, card)
        print(f"\n与基线 {args.baseline} 对比：")
        if not rows:
            print("  （没有可比项 —— 基线文件缺字段或口径已变）")
        for path, old, new, verdict in rows:
            arrow = "→"
            print(f"  {path:<46}{old:>9.4f} {arrow} {new:<9.4f} {verdict}")
        worse = [r for r in rows if "不进反退" in r[3]]
        if worse:
            print(f"\n⚠️ {len(worse)} 项退化：" + "、".join(w[0] for w in worse), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
