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

#: 源标识的 SQL 表达式：**优先 `chunk.source_path`（相对路径），回落 `source`（basename）**。
#: 与 `kb_inventory.source_key` 的口径必须**逐字一致** —— 两处不一致时，列出与删除
#: 会判在不同的键上（表现为"预演说有 N 条孤儿，真删却是 0 条"）。守卫见
#: `tests/test_kb_prune.py::test_pg_and_python_agree_on_source_key`。
#: `NULLIF(...,'')` 不能省：老数据的 jsonb 里可能**有 `source_path` 这个键但值为空串**
#: （例如从别处导入的块），此时 COALESCE 不会回落，直接得到空串 ⇒ 判成脏行。
SOURCE_KEY_SQL = "COALESCE(NULLIF(chunk->>'source_path', ''), source)"


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
                 tenant=None):
        """记住连接与表配置，构造时**不连库**。

        dsn: 连接串（DB_URL）。
        table: 表名（会做标识符白名单校验）。
        index: 向量索引类型：hnsw（默认，召回好）/ ivfflat（省内存）/ none（不建）。
        connect_timeout: 单次连接超时（秒）。
        tenant: 租户 id。**语义于 2026-10-06 反转**：
            - `None` = 显式声明"**不过滤**"（离线工具、迁移、单租户兼容）；
            - **任何字符串（含空串）= 一个真实的租户值**，读写都锁定在它上面。

            ⚠️ 此前是 `if self.tenant:` 判断 ⇒ 空串同样等于"不过滤"。修它的理由
            不是洁癖：`resolve_tenant()` 当时会返回空串（"显式要求不隔离"），
            于是 `RAG_TENANT` 留空这一个配置就把**整个库**暴露出去 —— 而文档
            写的恰好相反（"留空 = 什么都读不到"）。**空串不再是一个"关闭隔离"
            的暗号**：想去掉过滤，只有一个显式得多的写法 `tenant=None`。
        """
        self.dsn = (dsn or "").strip()
        self.table = _safe_ident(table)
        self.index = (index or "hnsw").strip().lower()
        self.connect_timeout = connect_timeout
        # ⚠️ 保留 None 与 "" 的区别（过滤判定靠 `is not None`）；但**写入**一律落
        # 字符串（`self.tenant or ""`）—— 那既避免了 NULL 与 '' 两种"空"并存
        # （过滤时 `tenant = ''` 匹配不到 NULL，是个静默的空集合），也让
        # `tenant=None` 建的表与历史数据完全一致（历史写入的就是 ''）。
        self.tenant = None if tenant is None else str(tenant).strip()

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

        无参数。返回 set[str]。`tenant` 不为 None 时**只返回该租户的** —— 否则会把
        别的租户的块判成"已存在"从而漏灌（反向也一样：别的租户的块被算进来，
        本租户该灌的反而漏掉）。

        表不存在时返回空集（而不是抛错）—— 这是"首次灌库"的正常路径：
        空集 ⇒ 所有块都被判为缺失 ⇒ 全量写入并顺带建表。
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)", (self.table,))
                if cur.fetchone()[0] is None:
                    return set()
                if self.tenant is not None:
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
                self.tenant or "",
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

        `tenant` 不为 None 时**只在该租户的数据里检索**（多租户隔离的最后一道闸）。

        ⚠️ 这里的判据是 `is not None`，不是"非空"（2026-10-06 修）：空串曾等于
        "拼不出条件 ⇒ 不过滤"，而 `resolve_tenant()` 当时正会给空串 ——
        `RAG_TENANT` 留空即把整个库暴露给任何一个请求。
        """
        from pgvector import Vector

        params = [Vector(vector)]
        conds = []
        if self.tenant is not None:
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
        """返回表内行数（`tenant` 不为 None 则只数该租户）；表不存在返回 0。"""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)", (self.table,))
                if cur.fetchone()[0] is None:
                    return 0
                if self.tenant is not None:
                    cur.execute(f"SELECT count(*) FROM {self.table} WHERE tenant = %s",
                                (self.tenant,))
                else:
                    cur.execute(f"SELECT count(*) FROM {self.table}")
                return int(cur.fetchone()[0])

    # ---------- 索引身份（embedding 指纹） ----------
    @property
    def meta_table(self):
        """存索引身份的元数据表名：**跟随主表**（`<表>_meta`）。

        跟随主表而不是共用一张固定的元数据表：换 embedding 模型时的推荐做法是
        换 `PGVECTOR_TABLE`（如 `rag_chunks_d1024`），两张表的身份记录必须各存一份，
        否则"新表写进去的身份被旧表读出来"，一次正常的换表会被判成不匹配。

        不额外做长度截断：PG 标识符上限 63 字节，超出会被**静默截断**；但表名已过
        `_safe_ident` 的字符白名单，实际长度远低于该上限（`rag_chunks_meta` = 15）。
        """
        return f"{self.table}_meta"

    def index_dim(self):
        """读主表 `embedding` 列的实际维度；表/列不存在时返回 0。

        **只读**：不建表、不写数据、不算 embedding —— 它是启动自检的入口，
        自检绝不能有副作用（否则"检查一下"本身会改库）。

        复用模块级 `embedding_dim`（"维度探测的唯一实现"）：两处各写一遍 SQL 时，
        很容易一处改了另一处没改，于是"自检说 1024、建表说 768"这种自相矛盾的
        状态能长期存在。
        """
        with self._connect() as conn:
            return embedding_dim(conn, self.table)

    def get_meta(self, key):
        """读一条元数据；表不存在或键不存在都返回 None（= 没记录过，不是错误）。

        "表不存在"必须走 `to_regclass` 显式判掉，而不是靠捕获异常：首次灌库之前
        这张表本来就不存在，如果让它抛 UndefinedTable 再由上层吞掉，日志里会留下
        一条看起来像故障的 WARNING。
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)", (self.meta_table,))
                if cur.fetchone()[0] is None:
                    return None
                cur.execute(f"SELECT value FROM {self.meta_table} WHERE key = %s",
                            (str(key),))
                row = cur.fetchone()
                return None if not row else str(row[0])

    def set_meta(self, key, value):
        """写一条元数据（表不存在则建）。

        单独一张表而不是塞进主表：主表每行都带 `embedding` 列，往里面插一行
        "只有元数据、没有向量"的记录，会让所有 `count(*)` 与 HNSW 索引都多出一个
        假块（而假块的向量是 NULL，检索时没有任何排序依据，行为不可预期）。
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE TABLE IF NOT EXISTS {self.meta_table} ("
                    "  key        text PRIMARY KEY,"
                    "  value      text NOT NULL,"
                    "  updated_at timestamptz NOT NULL DEFAULT now()"
                    ")"
                )
                cur.execute(
                    f"INSERT INTO {self.meta_table} (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                    "updated_at = now()",
                    (str(key), str(value)),
                )
            conn.commit()

    # ---------- 按源维护（级联删除） ----------
    def list_rows(self):
        """列出库里每个"源"的 (tenant, 源标识, 内容指纹) 三元组，去重。

        返回 set 化之前的 list；表不存在时返回 []（与 `existing_ids` 同口径：
        "首次灌库"不是错误）。

        设了 tenant 时**只列该租户的** —— 否则会把别的租户的块算进孤儿清单，
        接着被 `delete_keys` 删掉（跨租户误删，是最不该发生的一类事故）。

        行数在此处可能大于"块数"（一份文件几十个块只贡献一行 distinct），
        返回前用 set 去重；指纹同键不同值时保留两条，判定侧按"有一条对得上
        就算活着"处理（见 `kb_inventory.plan_orphans`）。
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)", (self.table,))
                if cur.fetchone()[0] is None:
                    return []
                sql = (
                    f"SELECT DISTINCT tenant, {SOURCE_KEY_SQL} AS k, "
                    "COALESCE(chunk->>'content_hash', '') AS h "
                    f"FROM {self.table}"
                )
                params = []
                if self.tenant is not None:
                    sql += " WHERE tenant = %s"
                    params.append(self.tenant)
                cur.execute(sql, params)
                return {
                    (str(r[0] or ""), str(r[1] or "").replace("\\", "/"), str(r[2] or ""))
                    for r in cur.fetchall()
                }

    def list_tenants(self):
        """列出表里出现过的 tenant（升序）；表不存在返回 []。

        给运维脚本用：多租户部署下要逐个租户清理，且**必须**逐个来 ——
        一条不限定租户的 DELETE 会把所有租户的孤儿一起删掉，那是不可逆的。
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)", (self.table,))
                if cur.fetchone()[0] is None:
                    return []
                cur.execute(f"SELECT DISTINCT tenant FROM {self.table} ORDER BY tenant")
                return [str(r[0] or "") for r in cur.fetchall()]

    def delete_keys(self, keys):
        """删除源标识命中 keys 的行，返回**实际删除的行数**（不是份数）。

        keys: 源标识的可迭代（`source_key` 口径）。空/全空串时直接返回 0，
        **不执行任何 SQL** —— `= ANY('{}')` 虽然天然匹配不到行，但"没给清单
        却打了一条 DELETE"这件事本身在审计里说不清楚。

        设了 tenant 时删除限定在该租户内（同 `list_rows`）。
        """
        keys = sorted({str(k).strip() for k in (keys or []) if str(k).strip()})
        if not keys:
            return 0
        conds = [f"{SOURCE_KEY_SQL} = ANY(%s)"]
        params = [keys]
        if self.tenant is not None:
            conds.append("tenant = %s")
            params.append(self.tenant)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {self.table} WHERE " + " AND ".join(conds), params
                )
                deleted = cur.rowcount
            conn.commit()
        return int(deleted or 0)

    def delete_tenant(self):
        """删除本租户的**全部**行，返回删除行数。

        ⚠️ ``tenant is None``（= 显式声明"不过滤"）时**拒绝执行**并返回 0。
        那个组合等价的 SQL 是没有 WHERE 的 ``DELETE FROM`` —— 一个参数漏传就把
        整张表清空，而调用方拿到的返回值还是"成功"。这类操作宁可多一道硬拦截。
        """
        if self.tenant is None:
            return 0
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self.table} WHERE tenant = %s",
                            (self.tenant,))
                deleted = cur.rowcount
            conn.commit()
        return int(deleted or 0)
