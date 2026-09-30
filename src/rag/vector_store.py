"""pgvector 向量存储层：把块的稠密向量落到 PostgreSQL，并做余弦近邻检索。


本模块把它换成 pgvector：

- 向量与块的元数据**同库同表**存放，检索走 SQL，写入走 `ON CONFLICT` upsert；
- 启动时只对"库里还没有的块"付费算向量（`existing_ids` 增量判定）；
- 向量规模在百万级以内时，pgvector + HNSW 的性能足够，且不用额外运维一个向量服务。

⚠️ **服务端前置条件**：数据库必须已安装 `vector` 扩展（`CREATE EXTENSION vector`）。
本模块会在 `ensure_schema` 里尝试自动创建，但如果连接角色没有超级用户权限，
需要管理员先手动执行一次。

依赖隔离
--------
`psycopg` / `pgvector`（python 客户端）**全部在方法内延迟导入** —— 没装或没配时
只影响 pg 这一条后端，import 本模块不会让 `retrieval.py` 起不来。

约定
----
- 主键 = `chunk_fingerprint(chunk)`（source + section + text 的 sha1）；
- `chunk` 列存**完整块 dict 的 jsonb**，检索后原样回读，保证 `context_block` /
  `build_citations` 那套字段一个都不丢；
- 检索分数字段 = `1 - (embedding <=> query)`，即余弦相似度（越大越相关）。
"""

import hashlib
import logging
import re

# 表名 / 列名这类标识符不能用占位符传参（DDL 不支持），只能字符串拼。
# 为了不让配置里的怪字符变成 SQL 注入，统一白名单校验。
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

DEFAULT_TABLE = "rag_chunks"


def _safe_ident(name, default=DEFAULT_TABLE):
    """把配置里的表名收敛成安全的标识符。

    name: 环境变量 PGVECTOR_TABLE 的值。
    default: 非法或空时的回退表名。

    非法值会打一条 WARNING 再回退 —— 静默改表名会导致"数据写进了另一张表"
    这种极难排查的问题，必须留痕。
    """
    n = (name or "").strip()
    if _IDENT_RE.match(n):
        return n
    if n:
        logging.warning("PGVECTOR_TABLE=%r 不是合法标识符，回退为 %s", name, default)
    return default


def chunk_fingerprint(chunk, tenant="") -> str:
    """块的内容指纹（sha1），作为 pg 表主键与增量判定的依据。

    chunk: 单个块 dict。取 source / section / text 三个字段拼成指纹。
    tenant: 租户 id。**非空时**把它并进指纹，使不同租户的同内容块各自成行
        （多租户隔离）；为空时**保持原有算法不变** —— 单租户场景升级后库里
        已有 id 不变，不会平白触发一次全量重算 embedding。

    为什么用这三元组：与 `retrieval._chunk_key`（RRF 去重用的键）保持同一口径，
    这样"融合去重"和"增量入库"对'同一个块'的认定完全一致。
    代价：内容完全相同的两个块（如同文件里重复段落）会算成同一个 id、互相覆盖 ——
    与 RRF 的取舍一致，可接受。

    注意：**指纹里不含 embedding 模型**。换 embedding 模型后向量会"看起来还在、
    其实已经过期"，需要配合 `PGVECTOR_TABLE` 换表名或手动清理（见 .env.example 说明）。
    """
    parts = [
        str(chunk.get("source", "") or ""),
        str(chunk.get("section", "") or ""),
        str(chunk.get("text", "") or ""),
    ]
    if tenant:
        parts.insert(0, str(tenant))
    return hashlib.sha1("\x00".join(parts).encode("utf-8")).hexdigest()


def _jsonable(obj):
    """把块 dict 递归转成可安全 json 序列化的结构。

    obj: 任意嵌套的值。

    jsonb 落库时 tuple/set 这类类型会让 psycopg 的序列化直接抛错，
    这里统一降级成 list / str，保证入库不会因为一个奇怪字段整批失败。
    """
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def _int_or_none(v):
    """把 page 之类的值收敛成 int 或 None（列类型是 integer）。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def vector_available(dsn, connect_timeout=5) -> bool:
    """探测 dsn 指向的 PG 是否可用且**已安装 vector 扩展**。

    dsn: 数据库连接串（DB_URL）。空串直接返回 False。
    connect_timeout: 连接超时（秒），探测要快，避免启动被慢网络拖住。

    只做"能不能连 + 有没有扩展"两件事，不建表不写数据。
    任何异常都吞掉并返回 False —— 这是给 `build_retriever` 做降级判断用的，
    探测失败应当回退内存后端，而不是把服务带崩。
    """
    if not dsn:
        return False
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=connect_timeout) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                return cur.fetchone() is not None
    except Exception as e:  # noqa: BLE001  （探测失败即视为不可用）
        logging.warning("pgvector 可用性探测失败：%s", e)
        return False


def embedding_dim(conn, table) -> int:
    """读某表现有 `embedding` 列的实际维度；表或列不存在返回 0。

    conn: 已打开的 psycopg 连接。
    table: 表名（经 search_path 解析）。

    pgvector 把维度编码在 `pg_attribute.atttypmod` 里，直接读它即可 ——
    比从 information_schema 里正则解析类型名可靠。**本函数是维度探测的唯一实现**，
    服务层的结构预检（`app.migrations`）也复用它，避免两处 SQL 各写一遍。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.atttypmod FROM pg_attribute a "
            "WHERE a.attrelid = to_regclass(%s) AND a.attname = 'embedding'",
            (table,),
        )
        row = cur.fetchone()
    if not row or row[0] is None:
        return 0
    try:
        return max(0, int(row[0]))
    except (TypeError, ValueError):
        return 0


class PgVectorStore:
    """pgvector 表的读写封装：建表 / 增量判定 / upsert / 余弦检索。

    只负责"和 pg 打交道"，不负责算 embedding（那是 PgVectorRetriever 的事）。
    """

    def __init__(self, dsn, table=DEFAULT_TABLE, index="hnsw", connect_timeout=10,
                 tenant=""):
        """记住连接与表配置，构造时**不连库**。

        dsn: 连接串（DB_URL）。
        table: 表名（会做标识符白名单校验）。
        index: 向量索引类型：hnsw（默认，召回好）/ ivfflat（省内存）/ none（不建）。
        connect_timeout: 单次连接超时（秒）。
        tenant: 租户 id。非空时**读写都锁定在该租户内**（多租户隔离）；
            空串 = 不隔离（单租户/兼容既有数据）。
        """
        self.dsn = (dsn or "").strip()
        self.table = _safe_ident(table)
        self.index = (index or "hnsw").strip().lower()
        self.connect_timeout = connect_timeout
        self.tenant = (tenant or "").strip()

    # ---------- 连接 ----------
    def _connect(self):
        """建立连接并注册 vector 类型适配器。

        返回 psycopg 连接（调用方用 with 管理）。

        延迟导入 psycopg / pgvector：没装时抛 ImportError，由调用方（build_retriever）
        捕获后回退内存后端。`register_vector` 会去查 vector 类型的 OID，
        **扩展没装时这一步就会失败** —— 正好当作"pgvector 不可用"的自然探测点。
        """
        if not self.dsn:
            raise RuntimeError("未配置 DB_URL，无法使用 pgvector 后端")
        import psycopg
        from pgvector.psycopg import register_vector

        conn = psycopg.connect(self.dsn, connect_timeout=self.connect_timeout)
        register_vector(conn)
        return conn

    # ---------- 建表 ----------
    def ensure_schema(self, dim):
        """确保扩展、表与索引存在。幂等，可重复调用。

        dim: 向量维度（必须与实际 embedding 一致，bge-m3 = 1024）。
            维度只影响"首次建表"；表已存在时不会改动列定义（改维度要重建表）。

        建表用 `IF NOT EXISTS`，索引同理；唯一可能失败的是 `CREATE EXTENSION`
        需要超级用户权限，失败会抛错并由上层回退/告警。

        多租户：表里固定有 `tenant` 列（老表用 ALTER 补上，不影响既有数据）。
        单租户场景该列恒为空串，行为与以前完全一致。
        """
        dim = int(dim)
        if dim <= 0:
            raise ValueError("向量维度必须为正整数")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cur.execute(
                    f"CREATE TABLE IF NOT EXISTS {self.table} ("
                    "  id         text PRIMARY KEY,"
                    "  tenant     text NOT NULL DEFAULT '',"
                    "  source     text,"
                    "  section    text,"
                    "  page       integer,"
                    "  pages      text,"
                    "  chunk_id   text,"
                    "  kind       text,"
                    "  text       text NOT NULL,"
                    "  chunk      jsonb NOT NULL,"
                    f"  embedding  vector({dim}),"
                    "  updated_at timestamptz NOT NULL DEFAULT now()"
                    ")"
                )
                # ---- 维度校验：表已存在且维度不符时必须**显式报错** ----
                # 为什么不能静默放过：换过 embedding 模型后，`CREATE TABLE IF NOT
                # EXISTS` 不会改动已有列的定义，于是之后**每一次 upsert 都因维度
                # 不匹配失败**，而报错点是"插入失败"，离真正原因（换了模型）很远。
                actual = embedding_dim(conn, self.table)
                if actual and actual != dim:
                    raise RuntimeError(
                        f"表 {self.table} 的 embedding 列是 vector({actual})，"
                        f"但本次要写入 vector({dim})。这通常意味着换过 embedding 模型。"
                        "改列类型无法原地完成（既有向量无法猜测新维度的含义），"
                        f"请把 PGVECTOR_TABLE 换成新表名（如 {self.table}_d{dim}）"
                        "重新灌库，旧表保留以便回滚比对。"
                    )
                # 老表（本改造之前建的）没有 tenant 列，补上；IF NOT EXISTS 让这句幂等
                cur.execute(
                    f"ALTER TABLE {self.table} "
                    "ADD COLUMN IF NOT EXISTS tenant text NOT NULL DEFAULT ''"
                )
                cur.execute(
                    f"CREATE INDEX IF NOT EXISTS {self.table}_source_idx "
                    f"ON {self.table} (source)"
                )
                cur.execute(
                    f"CREATE INDEX IF NOT EXISTS {self.table}_tenant_idx "
                    f"ON {self.table} (tenant)"
                )
                if self.index in ("hnsw", "ivfflat"):
                    cur.execute(
                        f"CREATE INDEX IF NOT EXISTS {self.table}_embedding_idx "
                        f"ON {self.table} USING {self.index} "
                        f"(embedding vector_cosine_ops)"
                    )
            conn.commit()

    # ---------- 增量判定 ----------
    def existing_ids(self):
        """返回库里已有的全部块指纹。

        无参数。返回 set[str]。设了 tenant 时**只返回该租户的** —— 否则会把
        别的租户的块判成"已存在"从而漏灌。

        表不存在时返回空集（而不是抛错）—— 这是"首次灌库"的正常路径：
        空集 ⇒ 所有块都被判为缺失 ⇒ 全量写入并顺带建表。
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)", (self.table,))
                if cur.fetchone()[0] is None:
                    return set()
                if self.tenant:
                    cur.execute(f"SELECT id FROM {self.table} WHERE tenant = %s",
                                (self.tenant,))
                else:
                    cur.execute(f"SELECT id FROM {self.table}")
                return {row[0] for row in cur.fetchall()}

    # ---------- 写入 ----------
    def upsert(self, rows):
        """按主键 upsert 一批块，返回写入条数。

        rows: 可迭代，每项是 `(chunk_id, chunk_dict, text, vector)`：
            - chunk_id: chunk_fingerprint 的结果（主键）；
            - chunk_dict: **完整块 dict**，落 jsonb，检索后原样回读；
            - text: 检索文本（落独立列，便于调试与按文本过滤）；
            - vector: 向量（list[float] 或 numpy 数组）。

        用 `ON CONFLICT (id) DO UPDATE` 而不是先删后插：内容没变时也会更新
        updated_at，但不会产生 id 空窗；重复灌库是幂等的。
        """
        rows = list(rows)
        if not rows:
            return 0
        from pgvector import Vector
        from psycopg.types.json import Jsonb

        sql = (
            f"INSERT INTO {self.table} "
            "(id, tenant, source, section, page, pages, chunk_id, kind, text, chunk, embedding) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (id) DO UPDATE SET "
            "  tenant=EXCLUDED.tenant, "
            "  source=EXCLUDED.source, section=EXCLUDED.section, page=EXCLUDED.page, "
            "  pages=EXCLUDED.pages, chunk_id=EXCLUDED.chunk_id, kind=EXCLUDED.kind, "
            "  text=EXCLUDED.text, chunk=EXCLUDED.chunk, embedding=EXCLUDED.embedding, "
            "  updated_at=now()"
        )
        params = [
            (
                cid,
                self.tenant,
                str(meta.get("source", "") or ""),
                str(meta.get("section", "") or ""),
                _int_or_none(meta.get("page")),
                str(meta.get("pages", "") or ""),
                str(meta.get("chunk_id", "") or meta.get("id", "") or ""),
                str(meta.get("kind", "") or ""),
                text,
                Jsonb(_jsonable(meta)),
                Vector(vector),
            )
            for cid, meta, text, vector in rows
        ]
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, params)
            conn.commit()
        return len(params)

    # ---------- 检索 ----------
    def search(self, vector, top_k=3, sources=None):
        """余弦近邻检索，返回 `[(chunk_dict, score), ...]`。

        vector: 查询向量（list[float] 或 numpy 数组）。
        top_k: 返回条数上限。
        sources: 可选，限定只在这些来源文件里检索（None = 全库）。
            走的是 source 列上的 B-tree 索引，用于"按文档/权限过滤"这类场景。

        score 是余弦相似度（1 - 距离），越大越相关，与 EmbeddingRetriever.search
        返回的分数口径一致，因此上游 RRF / rerank 无需任何改动。

        设了 tenant 时**只在该租户的数据里检索**（多租户隔离的最后一道闸）。
        """
        from pgvector import Vector

        params = [Vector(vector)]
        conds = []
        if self.tenant:
            conds.append("tenant = %s")
            params.append(self.tenant)
        if sources:
            conds.append("source = ANY(%s)")
            params.append(list(sources))
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        sql = (
            f"SELECT chunk, 1 - (embedding <=> %s) AS score FROM {self.table}"
            f"{where} ORDER BY embedding <=> %s LIMIT %s"
        )
        params += [Vector(vector), int(top_k)]
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return [(row[0], float(row[1])) for row in cur.fetchall()]

    # ---------- 维护 ----------
    def count(self):
        """返回表内行数（设了 tenant 则只数该租户）；表不存在返回 0。"""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)", (self.table,))
                if cur.fetchone()[0] is None:
                    return 0
                if self.tenant:
                    cur.execute(f"SELECT count(*) FROM {self.table} WHERE tenant = %s",
                                (self.tenant,))
                else:
                    cur.execute(f"SELECT count(*) FROM {self.table}")
                return int(cur.fetchone()[0])
