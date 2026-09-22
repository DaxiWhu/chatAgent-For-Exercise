#!/usr/bin/env python
"""检索质量评测：对比不同检索后端 / 查询前缀 / 切分策略下的命中率。

用法（在项目根目录执行）：
    python scripts/eval_retrieval.py                    # 用内置评测集跑全部组合
    python scripts/eval_retrieval.py --set my_eval.json # 用自己的评测集
    python scripts/eval_retrieval.py --only hybrid --show-miss

评测集 JSON 格式：
    {
      "documents": [ {"filename": "...", "content": "..."} ],   # 或改用 "docs_dir": "data"
      "queries":   [ {"q": "提问", "expect": ["正确来源文件名"], "type": "literal|paraphrase"} ]
    }

指标：
    Top1 / Top3 / MRR。**按 type 分组统计**——literal 是照抄原文词的查询，
    paraphrase 是换了说法的查询，混在一起统计会得出「关键词就够用」的错误结论。
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.chdir(ROOT)

import src.retrieval as R  # noqa: E402

EN_INSTRUCTION = "Represent this sentence for searching relevant passages: "


def prepare_corpus(eval_set, workdir):
    if eval_set.get("docs_dir"):
        return os.path.join(ROOT, eval_set["docs_dir"]), False
    os.makedirs(workdir, exist_ok=True)
    for d in eval_set.get("documents", []):
        path = os.path.join(workdir, d["filename"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(d["content"])
    return workdir, True


def evaluate(retriever, queries, top_k=3):
    """返回 {"all": {...}, "literal": {...}, "paraphrase": {...}} 与未命中明细。"""
    groups = defaultdict(lambda: {"n": 0, "top1": 0, "top3": 0, "rr": 0.0})
    misses = []
    for item in queries:
        q, expect = item["q"], set(item["expect"])
        qtype = item.get("type", "literal")
        hits = retriever.search(q, top_k=top_k)
        sources = [c["source"] for c, _ in hits]

        hit1 = bool(sources) and sources[0] in expect
        rank = next((i for i, s in enumerate(sources, 1) if s in expect), 0)
        hit3 = rank > 0

        for key in ("all", qtype):
            g = groups[key]
            g["n"] += 1
            g["top1"] += int(hit1)
            g["top3"] += int(hit3)
            if rank:
                g["rr"] += 1.0 / rank
        if not hit3:
            misses.append((q, qtype, sources[:3]))

    out = {}
    for key, g in groups.items():
        n = g["n"] or 1
        out[key] = {"n": g["n"], "top1": g["top1"] / n, "top3": g["top3"] / n,
                    "mrr": g["rr"] / n}
    return out, misses


def main():
    ap = argparse.ArgumentParser(description="RAG 检索质量评测")
    ap.add_argument("--set", default=os.path.join("scripts", "eval_set.json"))
    ap.add_argument("--only", default=None,
                    help="只跑指定后端：tfidf / semantic / hybrid")
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--show-miss", action="store_true")
    ap.add_argument("--no-rerank", action="store_true", help="禁用 rerank 组合")
    ap.add_argument("--strategy", default=None,
                    help="切分策略：headings（结构感知）| recursive（纯递归）")
    ap.add_argument("--chunk-size", type=int, default=None, help="块大小（字符）")
    ap.add_argument("--overlap", type=int, default=None, help="块间重叠（字符）")
    ap.add_argument("--contextual", action="store_true",
                    help="开启 Contextual Retrieval（每块一次 LLM 调用，有缓存）后再评测")
    args = ap.parse_args()

    # 允许命令行覆盖切分参数，便于做「改前 vs 改后」对比
    if args.strategy:
        R.CHUNK_STRATEGY = args.strategy
    if args.chunk_size is not None:
        R.CHUNK_SIZE = args.chunk_size
    if args.overlap is not None:
        R.CHUNK_OVERLAP = args.overlap

    with open(args.set, encoding="utf-8") as fh:
        eval_set = json.load(fh)
    queries = eval_set["queries"]

    workdir = tempfile.mkdtemp(prefix="rag_eval_")
    docs_dir, is_temp = prepare_corpus(eval_set, workdir)

    try:
        chunks = R.load_chunks(docs_dir)
        # 批次8 的验收手段：Contextual Retrieval 会改变**召回排序**，所以能用同一套指标量出差别
        # （父子块不会改排序，它改的是"送模型什么"，只能用单测与 prompt 检查验收）。
        if args.contextual:
            from src import contextualizer
            R.CONTEXTUAL = True          # 命令行开关落到配置上，enabled() 的判据保持单一
            stats = contextualizer.apply(chunks)
            print(f"[Contextual] {contextualizer.format_stats(stats)}")
            if not stats["prefixed"]:
                print("  ⚠️ 一块都没加上前缀，检查 RAG_CONTEXTUAL / AGNES_API_KEY；"
                      "下面测的仍是基线。")
        print("=" * 92)
        print(f"评测集 {args.set} | 切分策略={R.CHUNK_STRATEGY} "
              f"块大小={R.CHUNK_SIZE} 重叠={R.CHUNK_OVERLAP}")
        print(f"文档 → {len(chunks)} 个片段 | 查询 {len(queries)} 条"
              f"（literal {sum(1 for q in queries if q.get('type') != 'paraphrase')} / "
              f"paraphrase {sum(1 for q in queries if q.get('type') == 'paraphrase')}）")
        print(f"embedding {R.EMBEDDING_MODEL or '(未配置)'} @ {R.EMBEDDING_API_BASE or '(默认)'}"
              f" | rerank {'开(' + R.RERANK_MODEL + ')' if R.RERANK_ENABLED and R.RERANK_MODEL else '关'}")
        print("=" * 92)
        print("片段分布：" + "  ".join(
            f"{s}×{n}" for s, n in sorted(Counter(c["source"] for c in chunks).items())))

        combos = [
            ("tfidf", "", False),
            ("semantic", "", False),
            ("semantic", EN_INSTRUCTION, False),
            ("hybrid", "", False),
            ("hybrid", EN_INSTRUCTION, False),
        ]
        if R.RERANK_ENABLED and R.RERANK_MODEL and not args.no_rerank:
            combos.append(("hybrid", EN_INSTRUCTION, True))
        if args.only:
            combos = [c for c in combos if c[0] == args.only]

        print()
        print(f"{'后端':9s} {'前缀':7s} {'rerank':7s} | "
              f"{'整体Top1':>8s} {'整体Top3':>8s} {'整体MRR':>8s} | "
              f"{'照抄Top1':>8s} | {'改写Top1':>8s} {'改写Top3':>8s}")
        print("-" * 92)

        results = []
        cache = {}
        for backend, prefix, use_rerank in combos:
            key = (backend, use_rerank)
            if key not in cache:
                R.EMBEDDING_BACKEND = backend
                R.EMBEDDING_QUERY_PREFIX = ""
                R.RERANK_ENABLED = use_rerank
                cache[key] = R.build_retriever(chunks)
            retriever = cache[key]
            R.EMBEDDING_QUERY_PREFIX = prefix

            res, misses = evaluate(retriever, queries, top_k=args.top_k)
            results.append((backend, prefix, use_rerank, res, misses))
            lit = res.get("literal", {})
            par = res.get("paraphrase", {})
            print(f"{backend:9s} {'英文指令' if prefix else '(无)':7s} {str(use_rerank):7s} | "
                  f"{res['all']['top1']:7.0%} {res['all']['top3']:8.0%} {res['all']['mrr']:8.3f} | "
                  f"{lit.get('top1', 0):7.0%} | "
                  f"{par.get('top1', 0):7.0%} {par.get('top3', 0):8.0%}")

        print("-" * 92)
        print("说明：『照抄Top1』= 查询直接复用原文词；『改写Top1』= 换种说法提问。")
        print("      只看整体会低估语义检索的价值，改写列才是它真正解决的问题。")

        if results:
            best_all = max(results, key=lambda r: (r[3]["all"]["top1"], r[3]["all"]["mrr"]))
            best_par = max(results, key=lambda r: (r[3].get("paraphrase", {}).get("top1", 0),
                                                   r[3].get("paraphrase", {}).get("mrr", 0)))
            print(f"\n整体最佳：{best_all[0]}{' + 前缀' if best_all[1] else ''}"
                  f"{' + rerank' if best_all[2] else ''}"
                  f" → Top1 {best_all[3]['all']['top1']:.0%} / MRR {best_all[3]['all']['mrr']:.3f}")
            print(f"改写最佳：{best_par[0]}{' + 前缀' if best_par[1] else ''}"
                  f"{' + rerank' if best_par[2] else ''}"
                  f" → Top1 {best_par[3].get('paraphrase', {}).get('top1', 0):.0%}")

        if args.show_miss:
            for backend, prefix, use_rerank, res, misses in results:
                if not misses:
                    continue
                print(f"\n[{backend}{' + 前缀' if prefix else ''}"
                      f"{' + rerank' if use_rerank else ''}] 未命中：")
                for q, qtype, got in misses:
                    print(f"   ✗ [{qtype:10s}] {q}   实得：{got}")
    finally:
        if is_temp:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
