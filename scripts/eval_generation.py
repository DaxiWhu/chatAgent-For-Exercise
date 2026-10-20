#!/usr/bin/env python
"""生成侧质量评测：**agent 产出的答案本身**准不准。

它补的是哪一块
--------------
到这里为止的三层是：

    检索对不对    → eval_retrieval.py    Top1/Top3/MRR/Recall@k/nDCG@k/Prec@k
    材料够不够    → eval_answer.py       来源覆盖 / 关键点覆盖 / 引用可回溯
    答案准不准    → **本脚本**           ↓ 下面这些

前两层都停在**模型之前**：「找对文档」与「材料够用」都推不出「答案是忠实的」——
模型完全可以拿着含答案的材料，写出一段与材料无关的话。这一层此前一行度量都没有。

评什么（全部确定性、零模型调用）
--------------------------------
对每条答案产出：

    ok                             是否可信（True / False / **None = 无从判断**）
    reasons                        导致 False 的**确凿**理由（编造标识符 / 编造引用编号）
    grounded_ratio                 答案有多少内容能在资料里找到出处
    identifier_hallucination_rate  编造标识符的比例 ← **主力指标**
    refused                        是否如实拒答（资料里没有时这是正确答案）

判据的**可信度分级**写在 `src/rag/answer_quality.py` 的文件头：标识符硬判、
数字只记不判。这里不重复论证，只交代使用上的后果 ——
`identifier_hallucination_rate` 是**唯一**能让门禁失败的质量指标。

两种运行模式
------------
    python scripts/eval_generation.py        # 默认：离线（CI 用，零密钥）
    python scripts/eval_generation.py --live # 现场调模型（需 AGNES_API_KEY）

**默认（离线）**：对评测集里**人工写的 `reference_answer`** 跑指标，context 取
`expect_sources` 对应的语料。它回答的是「**忠实答案会不会被这套指标误报**」——
指标若把常用词当成幻觉，这里立刻红。这是**指标自身的回归测试**：
确定性、零密钥、可进 CI。

**`--live`**：现场检索 + 调模型，量真实答案。它才回答「**模型现在准不准**」，
但它要密钥、结果随模型漂移、有随机性 ⇒ **不进 CI**（与 eval_answer.py 拒绝
LLM-as-judge 当门禁是同一条理由：随机红的下场一定是被人关掉）。

⚠️ 两种模式不能互相替代：离线绿只说明"指标没坏"，不代表"模型准"。
`--live` 的数字才是模型质量的读数，要定期手动跑并用 `--save-json` 留档。

为什么不录一份真实模型输出进 CI
----------------------------
判据会随模型/prompt 漂移，录制文件会**过期**，而"录制过期"在流水线上表现成
随机红 —— 与 judge 当门禁同样的病。人工 `reference_answer` 则不会漂移
（它就是语料的忠实表达），且它失败时的含义单一：**指标坏了**。
两份稳定性不同的东西只留稳的那份。

为什么默认模式用 `expect_sources` 而不是真实检索结果
--------------------------------------------------
因为**检索质量已经有两个门禁在测**（eval_retrieval / eval_answer）。这里再用
检索结果当 context，"检索没召回"的失分会记成"答案编造" —— 一个指标承担两件事，
两件都测不准。分开之后本脚本的失败只有一个含义：**答案不忠实**。
要看真实链路就用 `--live`。

用法
----
    python scripts/eval_generation.py --show-miss       # 看是哪条判据没过
    python scripts/eval_generation.py --json            # 机器可读，便于留档/对比
    python scripts/eval_generation.py --live --save-json baseline.json
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
from src.rag import answer_quality as AQ  # noqa: E402

DEFAULT_SET = os.path.join("scripts", "generation_set.json")

# 门禁阈值。与 eval_answer.py 同思路：**先跑基线再留余量**，留太紧会让人
# 一遇正常波动就去调低阈值 —— 那才是最彻底的失效。
#
#   2026-10-01 基线（语料 data/kb 共 6 篇，11 条用例：8 正 + 3 负）：
#     ok_rate 1.000 / refusal_accuracy 1.000 / identifier_hallucination 0.000
#     avg_grounded 0.592（仅告警线 0.15）｜标识符分母 17
#
# 前两项与幻觉率**不留余量**：它们是"定义"而不是统计量 ——
# "忠实答案被判成编造"、"该拒答却编了"没有"差不多"可言，出现一次即失败。
DEFAULT_MIN_OK_RATE = 1.0
DEFAULT_MIN_REFUSAL = 1.0
DEFAULT_MAX_HALLUCINATION = 0.0

#: `avg_grounded` 只做**告警**不做门禁：模型会总结改写，覆盖率天然低于 1.0，
#: 且它对"改写幅度"敏感、对"编造"不敏感（编造已由上面那条硬指标把住）。
#: 设成硬阈值只会制造"改一次措辞就红一次"的噪音。
DEFAULT_MIN_GROUNDED = 0.15


def _load_set(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _context_from_sources(chunks, sources):
    """按期望来源挑片段，用**线上同款**的 `context_block` 拼装。

    不用 `"\\n".join(text)` 自己拼：线上进模型的是 `context_block` 的产物，
    评测若用另一种拼法，测的就是一条不存在的链路 —— 这类失真最隐蔽，
    数字看着正常，但它对应的是一个假想的系统。
    """
    want = set(sources or ())
    picked = [c for c in chunks if c.get("source") in want]
    return "\n\n".join(R.context_block(c) for c in picked), picked


def _live_context(retriever, q, top_k):
    """真实链路：检索 → 拼上下文 → 引用。返回 (context, hits, citations)。"""
    hits = retriever.search(q, top_k=top_k)
    ctx = "\n\n".join(R.context_block(c) for c, _ in hits)
    return ctx, hits, R.build_citations(hits)


def _generate(model, ctx, q):
    """首轮作答 —— 用**与作答提示词同一套**的上下文包装。

    包括 `guard.wrap_context`：线上进模型的是包了结构边界、声明不执行其中指令的
    上下文。评测这里少包一层，测的就是"没有注入防护"的路径，与线上不是一个系统。
    """
    from src import prompts
    from src.rag import guard

    safe = guard.wrap_context(ctx) if ctx else ""
    if safe:
        return model.invoke(prompts.answer_prompt(safe, q)).content
    return model.invoke(q).content


def _make_model():
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=os.getenv("AGNES_MODEL") or "agnes-2.0-flash",
        api_key=os.getenv("AGNES_API_KEY") or "",
        base_url=os.getenv("AGNES_API_BASE") or None,
        timeout=60, max_retries=2,
    )


def evaluate(cases, *, chunks=None, retriever=None, model=None, top_k=3, docs_dir=None):
    """逐条评测，返回 (明细列表, 汇总)。"""
    details = []
    for case in cases:
        q = case["q"]
        answerable = bool(case.get("answerable", True))

        if model is not None:
            ctx, hits, cites = _live_context(retriever, q, top_k)
            answer = _generate(model, ctx, q)
        elif case.get("expect_sources"):
            ctx, picked = _context_from_sources(chunks, case["expect_sources"])
            if not picked:
                # 期望来源一个片段都找不到时必须**显式失败**：否则 context 为空，
                # 指标全部退回"不判"，11 条用例会齐刷刷显示通过。
                raise SystemExit(
                    f"用例期望的来源在语料里一个片段都没找到：{case.get('expect_sources')}"
                    f"（问题：{q}）—— 语料被挪走或改名了，此时评测无意义")
            cites = R.build_citations([(c, 1.0) for c in picked])
            answer = case.get("reference_answer") or ""
        else:
            # 负样本：没有"期望来源"这回事 —— 它的语义恰恰是
            # 「**检索命中了一堆资料，但里面没有这个信息**」。所以必须用**真实检索**
            # 的结果当 context：自己指定一份 context 就把这个场景测没了，
            # 而"检索到相关资料时会不会编"才是负样本要回答的问题。
            ctx, hits, cites = _live_context(retriever, q, top_k)
            answer = case.get("reference_answer") or ""

        v = AQ.score_answer(answer, ctx, cites, question=q, docs_dir=docs_dir)
        details.append({
            "q": q, "answerable": answerable, "answer": answer,
            "ok": v["ok"], "reasons": v["reasons"], "warnings": v["warnings"],
            "grounded_ratio": v["grounded_ratio"],
            "identifier_total": v["identifier_total"],
            "unsupported_identifiers": [i["text"] for i in v["unsupported_identifiers"]],
            "unsupported_numbers": [i["text"] for i in v["unsupported_numbers"]],
            "refused": v["refused"], "citation_status": v["citation_status"],
        })
    return details, summarize(details)


def summarize(details):
    """汇总。

    每个比率的**样本量跟着一起给**：只报 `1.000` 而实际样本是 1 条，会被读成
    "很稳"，而这正是最该避免的误读。
    """
    ans = [d for d in details if d["answerable"]]
    unans = [d for d in details if not d["answerable"]]
    ident_total = sum(d["identifier_total"] for d in details)
    ident_bad = sum(len(d["unsupported_identifiers"]) for d in details)
    grounded = [d["grounded_ratio"] for d in ans if d["grounded_ratio"] is not None]
    ok_known = [d for d in ans if d["ok"] is not None]
    return {
        "cases": len(details),
        "answerable": len(ans),
        "unanswerable": len(unans),
        "ok_rate": (sum(1 for d in ok_known if d["ok"]) / len(ok_known)) if ok_known else 1.0,
        "ok_unknown": sum(1 for d in ans if d["ok"] is None),
        "identifier_hallucination_rate": (ident_bad / ident_total) if ident_total else 0.0,
        "identifier_total": ident_total,
        "refusal_accuracy": (sum(1 for d in unans if d["refused"]) / len(unans)) if unans else 1.0,
        "avg_grounded": (sum(grounded) / len(grounded)) if grounded else None,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", default=DEFAULT_SET, help="评测集路径")
    ap.add_argument("--top-k", type=int, default=None, help="检索条数（默认取评测集 top_k，--live 用）")
    ap.add_argument("--live", action="store_true",
                    help="现场调模型（需 AGNES_API_KEY）；不传则跑离线指标回归")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（便于留档/对比）")
    ap.add_argument("--save-json", default=None, help="把本次 scorecard 写进文件")
    ap.add_argument("--show-miss", action="store_true", help="打印未通过/有疑点的明细")
    ap.add_argument("--min-grounded", type=float, default=DEFAULT_MIN_GROUNDED,
                    help=f"avg_grounded 的告警线（默认 {DEFAULT_MIN_GROUNDED}，仅告警不失败）")
    args = ap.parse_args()

    if not os.path.exists(args.set):
        print(f"评测集不存在：{args.set}", file=sys.stderr)
        return 2

    spec = _load_set(args.set)
    cases = spec.get("cases") or []
    if not cases:
        # 空评测会给出满分（三个比率全是 1.0），必须当成失败。
        print("评测集里没有 cases —— 空评测会给出满分，必须当成失败", file=sys.stderr)
        return 2
    top_k = args.top_k or spec.get("top_k") or 3
    docs_dir = spec.get("docs_dir") or "data"

    # ⚠️ 整段评测罩在**锁定的本地口径**下（见 `scripts/eval_backend.py`）。负样本要
    #    走**真实检索**，所以数字直接由检索后端决定：不锁的话开发机走 hybrid、CI 走
    #    bm25，负样本召回的东西不同 ⇒ 拒答正确率两边不同，而两边都不报错。
    with local_eval():
        return _evaluate_and_report(args, cases, docs_dir, top_k)


def _evaluate_and_report(args, cases, docs_dir, top_k):
    model = None
    chunks = R.load_chunks(docs_dir)
    if not chunks:
        # 语料加载成空是**静默失败**：所有期望来源都找不到，若放行则覆盖率恒为 0，
        # 而排查的人会去查答案而不是查语料目录。
        print(f"语料目录 {docs_dir} 加载到 0 个片段 —— 评测无意义", file=sys.stderr)
        return 2
    # 检索器两种模式都要：离线模式下正样本用 expect_sources 取上下文，
    # 但**负样本必须走真实检索**（见 evaluate 里的说明）。
    retriever = R.build_retriever(chunks)
    if args.live:
        model = _make_model()

    details, summary = evaluate(cases, chunks=chunks, retriever=retriever, model=model,
                                top_k=top_k, docs_dir=docs_dir)

    payload = {"summary": summary, "details": details}
    if args.save_json:
        with open(args.save_json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"已写入 {args.save_json}", file=sys.stderr)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        mode = "现场（模型）" if args.live else "离线（人工参考答案）"
        print(f"模式：{mode}｜语料：{docs_dir}｜用例：{summary['cases']} 条"
              f"（可答 {summary['answerable']} / 应拒答 {summary['unanswerable']}）"
              f"｜口径：{R.EMBEDDING_BACKEND}（钉死，与 CI 一致）")
        print("-" * 92)
        print(f"{'问题':<34s} {'ok':<6} {'grounded':<9} {'ident':<7} {'refused':<8} 理由")
        for d in details:
            ident = f"{len(d['unsupported_identifiers'])}/{d['identifier_total']}"
            print(f"{d['q'][:32]:<34s} {str(d['ok']):<6} "
                  f"{str(d['grounded_ratio']):<9} {ident:<7} {str(d['refused']):<8} "
                  f"{','.join(d['reasons'])}")
        print("-" * 92)
        print(f"  答案可信率 ok_rate                     {summary['ok_rate']:.3f}"
              f"  （{summary['answerable']} 条可答用例）")
        print(f"  标识符幻觉率 identifier_hallucination   "
              f"{summary['identifier_hallucination_rate']:.3f}"
              f"  （分母 {summary['identifier_total']} 个标识符）")
        print(f"  拒答正确率 refusal_accuracy            {summary['refusal_accuracy']:.3f}"
              f"  （{summary['unanswerable']} 条应拒答用例）")
        ag = summary["avg_grounded"]
        print(f"  平均可回溯性 avg_grounded              {'不判' if ag is None else f'{ag:.3f}'}")
        if summary["ok_unknown"]:
            print(f"  ⚠️ {summary['ok_unknown']} 条答案为空（ok=None，不计入 ok_rate）")

    if args.show_miss:
        print("\n未通过 / 有疑点：")
        for d in details:
            if d["ok"] is False or d["warnings"] or (not d["answerable"] and not d["refused"]):
                print(f"  · {d['q']}")
                if d["unsupported_identifiers"]:
                    print(f"      编造标识符：{d['unsupported_identifiers']}  ← 资料里没有这些名字")
                if d["unsupported_numbers"]:
                    print(f"      数字对不上（仅记录，不判失败）：{d['unsupported_numbers']}")
                if d["warnings"]:
                    print(f"      疑点：{d['warnings']}")
                if not d["answerable"] and not d["refused"]:
                    print("      ⚠️ 资料里没有该信息，但答案没有明确拒答（可能编了）")

    problems = []
    if summary["ok_rate"] < DEFAULT_MIN_OK_RATE:
        problems.append(f"答案可信率 {summary['ok_rate']:.3f} < {DEFAULT_MIN_OK_RATE}")
    if summary["refusal_accuracy"] < DEFAULT_MIN_REFUSAL:
        problems.append(f"拒答正确率 {summary['refusal_accuracy']:.3f} < {DEFAULT_MIN_REFUSAL}")
    if summary["identifier_hallucination_rate"] > DEFAULT_MAX_HALLUCINATION:
        problems.append(f"标识符幻觉率 {summary['identifier_hallucination_rate']:.3f} "
                        f"> {DEFAULT_MAX_HALLUCINATION}")

    if problems:
        print("\n未达门禁：" + "；".join(problems), file=sys.stderr)
        return 1

    if summary["avg_grounded"] is not None and summary["avg_grounded"] < args.min_grounded:
        # 只告警：见 DEFAULT_MIN_GROUNDED 上方那段（对"改写幅度"敏感、对"编造"不敏感）
        print(f"\n⚠️ 平均可回溯性 {summary['avg_grounded']:.3f} 低于告警线 "
              f"{args.min_grounded}（不影响退出码）", file=sys.stderr)

    print("\n门禁通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
