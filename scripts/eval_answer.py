#!/usr/bin/env python
"""答案质量评测：引用覆盖 / 关键点覆盖 / 可回溯性。

它补的是哪一块
--------------
`eval_retrieval.py` 测的是 **Top1 召回率** —— "找对了文档吗"。
但检索对 ≠ 答案对：检索回来的块可能**不含**回答该问题需要的关键事实，
于是模型只能漏掉它、或者编一个出来。这一层此前完全没有度量。

本脚本评的是**喂给模型的材料**（检索 + 上下文拼装的产物），而不是模型输出。
这个取舍是刻意的：

  - **确定性、离线**：纯 BM25（纯本地），不需要任何密钥与网络，因此可以进 CI 卡门禁；
  - **它是答案质量的上界**：上下文里没有的信息，模型不可能忠实作答。
    上界掉了，答案质量一定掉 —— 这条断言不需要 LLM 也成立。

模型侧的**忠实度**（有没有编造、有没有答非所问）需要 LLM-as-judge，
那是另一条链路，不能替代这一条：judge 本身有噪声、要密钥、结果不可复现，
拿它当门禁会引入"随机红"。

用法
----
    python scripts/eval_answer.py                       # 默认评测集 + 默认阈值
    python scripts/eval_answer.py --show-miss           # 打印未命中的问题与原因
    python scripts/eval_answer.py --fail-under 0.9      # 覆盖门禁阈值
    python scripts/eval_answer.py --set my_set.json

评测集格式（`scripts/eval_answer_set.json`）：
    {
      "docs_dir": "data",
      "cases": [
        {
          "q": "限流的判定值是怎么计算的",
          "expect_sources": ["04-限流与配额.md"],
          "key_points": ["上一窗口", "加权"]
        }
      ]
    }

`key_points` 用**子串**匹配（而非语义匹配）是刻意的：确定性、可复现、
零依赖。代价是同义改写会漏判 —— 所以选点必须是**语料里明确出现的术语**，
不要写近义词。这条约束反过来也有好处：它逼评测集贴着知识库的真实措辞。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.chdir(ROOT)

import src.rag.retrieval as R  # noqa: E402
from scripts.eval_backend import local_eval  # noqa: E402

DEFAULT_SET = os.path.join("scripts", "eval_answer_set.json")

# 门禁阈值。**不是拍脑袋定的** —— 先跑基线再留出退化空间：
#
#   2026-09-29 基线（BM25 检索 + Top-3，语料 data/ 共 136 片段，22 条用例）：
#     来源覆盖   0.955
#     关键点覆盖 0.955
#     引用可回溯 1.000
#
#   同语料、同口径下换 BM25 **之前**的 TF-IDF 基线（留档，便于对照）：
#     来源覆盖   0.932 / 关键点覆盖 0.909 / 可回溯 1.000
#     -- 换打分函数后来源覆盖 +2.3 点、关键点覆盖 +4.6 点；漏掉的来源从
#        2 个（02-部署与配置.md、04-限流与配额.md）减到 1 个。
#
# 余量按"能容下正常的语料增删、但抓得住真实退化"取：来源留 ~8 点、关键点留 ~9 点。
# 留得太紧的后果不是"更安全"，而是维护者一遇到正常波动就去调低阈值 ——
# 那才是最彻底的失效（阈值成了摆设）。上界则由"引用必须 100% 可回溯"把住：
# 那是可回溯性的定义，不是统计量，没有"差不多"。
#
# ⚠️ 门禁防的是"答案质量整体退化"，**不负责识别检索实现被换掉** ——
#    TF-IDF 的 0.932/0.909 同样高于下面这两个阈值。想守住"用的必须是 BM25"，
#    靠的是 tests/test_bm25.py（它把绝对分值钉到 1e-12 量级），统计门禁做不到。
#
# 已知损耗（**不是 bug，别为此改语料**）：字符 n-gram 在分词上的局限会让少数
# 语义相近的查询把别的文档排到前面（例如"配额为什么要先预占再结算"会先命中
# 05-可观测与SLO.md）。这类损耗靠 embedding 后端解决，属 `eval_retrieval.py`
# 的优化面；本脚本只负责"别让它变得更差"。
DEFAULT_MIN_SOURCE_COVERAGE = 0.87
DEFAULT_MIN_KEYPOINT_COVERAGE = 0.86


def load_set(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def evaluate(cases, retriever, chunks, top_k=3):
    """返回 (明细列表, 汇总指标)。

    三项指标的含义：
      - source_coverage: 期望来源被 Top-K 覆盖的比例（按问题平均）。
        "文档找对了没有" —— 与 eval_retrieval 同源，但这里按**子集**算，
        允许多个来源共同支撑一个答案。
      - key_point_coverage: 关键点出现在**检索上下文**里的比例。
        "材料够不够支撑一个完整回答"。
      - traceable: 每条引用都带 source + chunk_id，且 chunk_id 在语料里存在。
        "点开引用能不能回到原文" —— 引用不可回溯时，"有引用"毫无价值。
    """
    known_ids = {c.get("chunk_id") for c in chunks}
    details = []

    for case in cases:
        q = case["q"]
        expect = set(case.get("expect_sources") or [])
        points = list(case.get("key_points") or [])

        hits = retriever.search(q, top_k=top_k)
        got_sources = {c.get("source") for c, _ in hits}
        # 上下文按**线上同款**拼装（context_block），避免"评测用的拼法与
        # 线上不一致"这种最隐蔽的失真：测的是一个不存在的链路。
        context = "\n\n".join(R.context_block(c) for c, _ in hits)

        src_hit = expect & got_sources if expect else set()
        src_cov = (len(src_hit) / len(expect)) if expect else 1.0

        missing_points = [p for p in points if p not in context]
        kp_cov = ((len(points) - len(missing_points)) / len(points)) if points else 1.0

        cites = R.build_citations(hits)
        bad_cites = [c for c in cites
                     if not (c.get("source") or "").strip()
                     or not (c.get("chunk_id") or "").strip()
                     or c.get("chunk_id") not in known_ids]
        traceable = bool(cites) and not bad_cites

        details.append({
            "q": q,
            "source_coverage": src_cov,
            "keypoint_coverage": kp_cov,
            "traceable": traceable,
            "missed_sources": sorted(expect - got_sources),
            "missed_points": missing_points,
            "bad_cites": len(bad_cites),
            "top1": (hits[0][0].get("source") if hits else None),
        })

    n = len(details) or 1
    summary = {
        "cases": len(details),
        "source_coverage": sum(d["source_coverage"] for d in details) / n,
        "keypoint_coverage": sum(d["keypoint_coverage"] for d in details) / n,
        "traceable_rate": sum(1 for d in details if d["traceable"]) / n,
    }
    return details, summary


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", default=DEFAULT_SET, help="评测集路径")
    ap.add_argument("--top-k", type=int, default=3, help="检索条数")
    ap.add_argument("--fail-under", type=float, default=None,
                    help="两项覆盖率门禁阈值（默认取脚本内置值）")
    ap.add_argument("--show-miss", action="store_true", help="打印未命中的明细")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出汇总（便于留档）")
    args = ap.parse_args()

    if not os.path.exists(args.set):
        print(f"评测集不存在：{args.set}", file=sys.stderr)
        return 2

    spec = load_set(args.set)
    docs_dir = spec.get("docs_dir") or "data"
    cases = spec.get("cases") or []
    if not cases:
        print("评测集里没有 cases —— 空评测会给出满分，必须当成失败", file=sys.stderr)
        return 2

    # ⚠️ 整段评测罩在**锁定的本地口径**下（见 `scripts/eval_backend.py`）。不锁的话
    #    数字随开发机 `.env` 走 hybrid、CI 走 bm25 —— 同一份代码两个结论且两边都不报错，
    #    而阈值只按 bm25 校准过。锁必须罩住**检索调用**（查询侧前缀是查询时读的模块常量），
    #    所以开在装配之前、关在评测之后。
    with local_eval():
        return _evaluate_and_report(args, docs_dir, cases)


def _evaluate_and_report(args, docs_dir, cases):
    chunks = R.load_chunks(docs_dir)
    if not chunks:
        # 语料为空是**静默失败**：检索恒无结果，但如果不显式拦住，
        # 覆盖率会算成 0 并报"不达标" —— 那时人会去查检索，而真正的问题是语料。
        print(f"语料目录 {docs_dir} 加载到 0 个片段 —— "
              "评测无意义（线上此时也会检索恒空）", file=sys.stderr)
        return 2

    retriever = R.build_retriever(chunks)
    details, summary = evaluate(cases, retriever, chunks, top_k=args.top_k)

    if args.json:
        print(json.dumps({"summary": summary, "details": details},
                         ensure_ascii=False, indent=2))
    else:
        print(f"语料：{docs_dir}（{len(chunks)} 个片段）｜用例：{summary['cases']} 条"
              f"｜口径：{R.EMBEDDING_BACKEND}（钉死，与 CI 一致）")
        print(f"  来源覆盖   {summary['source_coverage']:.3f}")
        print(f"  关键点覆盖 {summary['keypoint_coverage']:.3f}")
        print(f"  引用可回溯 {summary['traceable_rate']:.3f}")

    if args.show_miss:
        print("\n未命中明细：")
        for d in details:
            if d["missed_sources"] or d["missed_points"] or not d["traceable"]:
                print(f"  · {d['q']}")
                if d["missed_sources"]:
                    print(f"      缺来源：{d['missed_sources']}（Top1={d['top1']}）")
                if d["missed_points"]:
                    print(f"      上下文未覆盖：{d['missed_points']}")
                if not d["traceable"]:
                    print(f"      不可回溯的引用：{d['bad_cites']} 条")

    lo_src = (args.fail_under if args.fail_under is not None
              else DEFAULT_MIN_SOURCE_COVERAGE)
    lo_kp = (args.fail_under if args.fail_under is not None
             else DEFAULT_MIN_KEYPOINT_COVERAGE)

    problems = []
    if summary["source_coverage"] < lo_src:
        problems.append(f"来源覆盖 {summary['source_coverage']:.3f} < {lo_src}")
    if summary["keypoint_coverage"] < lo_kp:
        problems.append(f"关键点覆盖 {summary['keypoint_coverage']:.3f} < {lo_kp}")
    if summary["traceable_rate"] < 1.0:
        problems.append(f"引用可回溯率 {summary['traceable_rate']:.3f} < 1.0")

    if problems:
        print("\n未达门禁：" + "；".join(problems), file=sys.stderr)
        return 1
    print("\n门禁通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
