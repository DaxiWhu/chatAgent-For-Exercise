"""验证 pgvector 后端是否生效。连续跑两次即可看出「增量同步」：

    python scripts/verify_pgvector.py    # 第 1 次：应出现「pgvector 增量写入 N 块」
    python scripts/verify_pgvector.py    # 第 2 次：应出现「pgvector 已是最新（N 块），本次零 embedding 调用」

前置条件：
    .env 中 RAG_VECTOR_STORE=pg，且 DB_URL 指向已装 vector 扩展的 PostgreSQL
    （容器方案见 deploy/pgvector/docker-compose.yml）。

判读要点：
    · 第 2 次「本次零 embedding 调用」= 增量生效，冷启动不再重复花钱重算
    · 库内 embedding 维度应与 EMBEDDING_MODEL 一致（bge-m3 = 1024）
    · 出现 ProgrammingError: vector type not found → 服务端没建扩展（CREATE EXTENSION vector）
"""
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv(".env")
logging.basicConfig(level=logging.INFO, format="   %(levelname)s %(message)s")

import src.rag.retrieval as R  # noqa: E402
from src.graph import DOCS_DIR  # noqa: E402


def db_report(dsn, table):
    """打印库内统计；连不上只提示、不抛。"""
    try:
        import psycopg
    except ImportError:
        print("[库统计] 跳过：未安装 psycopg")
        return
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            cur = conn.cursor()
            cur.execute(f"select count(*) from {table}")
            print(f"[库统计] {table} 行数 = {cur.fetchone()[0]}")
            cur.execute("select extversion from pg_extension where extname='vector'")
            row = cur.fetchone()
            print("[库统计] vector 扩展版本 =", row[0] if row else "未安装 !!")
            cur.execute(
                f"select source, count(*), vector_dims(embedding) from {table} "
                "group by source, vector_dims(embedding) order by source"
            )
            for src, cnt, dim in cur.fetchall():
                print(f"           {src:<24} {cnt:>3d} 块  维度={dim}")
            cur.execute("select indexname from pg_indexes where tablename=%s", (table,))
            print("[库统计] 索引 =", [r[0] for r in cur.fetchall()])
    except Exception as e:  # noqa: BLE001
        print(f"[库统计] 失败：{type(e).__name__}: {e}")


def main():
    print("=" * 68)
    print("VECTOR_STORE =", R.VECTOR_STORE)
    print("DB_URL       =", R.DB_URL)
    print("DOCS_DIR     =", DOCS_DIR)
    print("=" * 68)

    chunks = R.load_chunks(DOCS_DIR)
    print("语料块数 =", len(chunks))

    t0 = time.time()
    retriever = R.build_retriever(chunks)
    print(f"检索器 = {type(retriever).__name__}（构建耗时 {time.time() - t0:.1f}s）")

    if R.DB_URL:
        db_report(R.DB_URL, R.PGVECTOR_TABLE)

    hits = retriever.search("梧州六堡茶有什么特点", 3)
    print(f"检索命中 {len(hits)} 条")
    for i, item in enumerate(hits):
        if isinstance(item, tuple):
            item = item[0]
        if isinstance(item, dict):
            text = (item.get("text") or "").replace("\n", " ")[:46]
            print(f"  [{i}] {item.get('source')} | {text}")
        else:
            print(f"  [{i}] {item!r}")


if __name__ == "__main__":
    main()
