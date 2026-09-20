# -*- coding: utf-8 -*-
"""快速检索评测：一个进程内跑完「旧切分 vs 新切分」× 多后端 × 有无前缀。

与 eval_retrieval.py 的区别：查询向量一次性批量算好，不再每条查询发一次 HTTP，
所以整体只要几次请求，秒级返回。
"""
import json
import os
import shutil
import sys
import tempfile
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import numpy as np
import src.retrieval as R
from langchain_openai import OpenAIEmbeddings
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

EN = "Represent this sentence for searching relevant passages: "
SET = json.load(open(os.path.join("scripts", "eval_set.json"), encoding="utf-8"))
QUERIES = SET["queries"]


def norm(x):
    return x / (np.linalg.norm(x, axis=-1, keepdims=True) + 1e-9)


def rrf(lists, k=60):
    sc = {}
    for lst in lists:
        for rank, i in enumerate(lst, 1):
            sc[i] = sc.get(i, 0.0) + 1.0 / (k + rank)
    return [i for i, _ in sorted(sc.items(), key=lambda kv: kv[1], reverse=True)]


def report(name, ranks, chunks):
    g = defaultdict(lambda: {"n": 0, "t1": 0, "t3": 0, "rr": 0.0})
    for q, idxs in zip(QUERIES, ranks):
        expect = set(q["expect"])
        qtype = q.get("type", "literal")
        srcs = [chunks[i]["source"] for i in idxs[:3]]
        hit1 = bool(srcs) and srcs[0] in expect
        rank = next((p for p, s in enumerate(srcs, 1) if s in expect), 0)
        for key in ("all", qtype):
            gg = g[key]
            gg["n"] += 1
            gg["t1"] += int(hit1)
            gg["t3"] += int(rank > 0)
            if rank:
                gg["rr"] += 1.0 / rank
    a, lit, par = g["all"], g["literal"], g["paraphrase"]
    print(f"{name:22s} | Top1 {a['t1']/a['n']:5.0%}  Top3 {a['t3']/a['n']:5.0%}  "
          f"MRR {a['rr']/a['n']:.3f} | 照抄 {lit['t1']/lit['n']:4.0%} | "
          f"改写 {par['t1']/par['n']:4.0%} / Top3 {par['t3']/par['n']:4.0%}")


def write_corpus(tmp):
    for d in SET["documents"]:
        p = os.path.join(tmp, d["filename"])
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(d["content"])


emb = OpenAIEmbeddings(
    model=R.EMBEDDING_MODEL, api_key=R.EMBEDDING_API_KEY,
    base_url=R.EMBEDDING_API_BASE or None,
    chunk_size=R.EMBEDDING_BATCH, max_retries=3,
)

print("=" * 104)
print(f"embedding={R.EMBEDDING_MODEL}  查询={len(QUERIES)} 条"
      f"（照抄 {sum(1 for q in QUERIES if q.get('type') != 'paraphrase')} / "
      f"改写 {sum(1 for q in QUERIES if q.get('type') == 'paraphrase')}）")
print("=" * 104)

for strategy, csize in [("recursive", 500), ("headings", 300)]:
    R.CHUNK_STRATEGY = strategy
    R.CHUNK_SIZE = csize
    R.CHUNK_OVERLAP = 50

    tmp = tempfile.mkdtemp(prefix="eval_")
    write_corpus(tmp)
    chunks = R.load_chunks(tmp)
    shutil.rmtree(tmp, ignore_errors=True)

    texts = [c["text"] for c in chunks]
    tv = TfidfVectorizer(analyzer=R.TFIDF_ANALYZER, ngram_range=R.TFIDF_NGRAM_RANGE)
    M = tv.fit_transform(texts)
    D = norm(np.array(emb.embed_documents(texts)))

    print(f"\n### 切分策略 = {strategy} / chunk={csize} → {len(chunks)} 个片段")
    print("-" * 104)

    for pname, prefix in [("(无前缀)", ""), ("英文指令", EN)]:
        Q = norm(np.array(emb.embed_documents([prefix + q["q"] for q in QUERIES])))
        tf, de, hy = [], [], []
        for i, q in enumerate(QUERIES):
            tsim = cosine_similarity(tv.transform([q["q"]]), M)[0]
            dsim = D @ Q[i]
            tr = list(np.argsort(tsim)[::-1][:10])
            dr = list(np.argsort(dsim)[::-1][:10])
            tf.append(tr[:3])
            de.append(dr[:3])
            hy.append(rrf([tr, dr])[:3])
        report(f"tfidf {pname}", tf, chunks)
        report(f"semantic {pname}", de, chunks)
        report(f"hybrid {pname}", hy, chunks)
        print()

print("=" * 104)
print("照抄 = 查询直接复用原文词（关键词的强项）；改写 = 换种说法提问（语义的强项）")
