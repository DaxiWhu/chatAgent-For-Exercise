"""Qdrant 向量存储层：**稠密 + 稀疏（BM25）双向量同库**，租户按 payload 过滤。

与 pgvector（`vector_store.py`）的定位差别
-----------------------------------------
`PgVectorStore` 只承载**稠密**向量，稀疏路（关键词）留在进程内由
`retrieval.BM25Retriever` 现算。Qdrant 则把**两路都收进一个 collection**：

- 命名向量 `dense`：embedding 向量，HNSW + 余弦；
- 命名向量 `sparse`：本地 BM25 编码出的稀疏向量，倒排索引；
- 检索时分别走 `search_dense` / `search_sparse`，两路结果交回上层
  `retrieval.HybridRetriever`，由现有的 `rrf_fuse` 做融合 —— **融合与 rerank 逻辑一行不改**。

多租户隔离
----------
pg 靠 `tenant` 列 + `WHERE tenant = %s` 做**物理**隔离；Qdrant 里等价物是
payload 里的 `tenant` keyword 字段 + 查询时的 `query_filter`。隔离强度相当
（都在存储层过滤，不可被上层绕过），但**前提是过滤值本身可信** ——
绝不能拿客户端可伪造的 `X-Tenant-Id` 直接当过滤值（那是"假隔离"，见
`app/main.py:_retrieval_tenant`）。本模块只负责"拿到 tenant 就过滤"，
"tenant 是否可信"由调用方在归一化时决定。

point id 的确定性
-----------------
主键 = `uuid5(NAMESPACE_URL, chunk_fingerprint)`。**刻意不用 `hash()`**：
Python 的 `hash()` 每个进程带随机盐，重启后同一个块会算出另一个 id ⇒ 增量判定
失效、每次启动全量重灌（embedding 白烧钱）。uuid5 是纯函数，跨进程/跨机器可复现。

依赖隔离
--------
`qdrant_client` **全部在方法内延迟导入** —— 没装或没配时只影响 Qdrant 这一条
后端，import 本模块不会让 `retrieval.py` 起不来。
"""

import hashlib
import logging
import math
import re
import uuid

from .index_identity import META_FLAG
from .kb_inventory import source_key

# Qdrant 集合名允许的字符集（官方约定：字母数字 + 下划线/短横线，≤255）。
# 与 pg 的表名一样，非法值会静默写到"另一个集合"这种极难排查的问题，所以白名单校验 + 告警回退。
_COLLECTION_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")

DEFAULT_COLLECTION = "rag_chunks"
DENSE_VECTOR = "dense"
SPARSE_VECTOR = "sparse"


def _safe_collection(name, default=DEFAULT_COLLECTION):
    """把配置里的集合名收敛成安全值；非法值打 WARNING 再回退。"""
    n = (name or "").strip()
    if _COLLECTION_RE.match(n):
        return n
    if n:
        logging.warning("QDRANT_COLLECTION=%r 不是合法集合名，回退为 %s", name, default)
    return default


def point_id(fingerprint) -> str:
    """块指纹 → Qdrant point id（uuid5 字符串，跨进程稳定）。

    fingerprint: `vector_store.chunk_fingerprint` 的结果（sha1 十六进制串）。
        传非字符串会被 str() 兜底，容忍测试里传 int。
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, str(fingerprint)))


def _jsonable(obj):
    """把块 dict 递归转成可安全 json 序列化的结构（payload 存储用）。

    与 `vector_store._jsonable` 同语义：tuple/set 这类类型会让 Qdrant 的序列化
    直接抛错，统一降级成 list / str，保证入库不会因为一个奇怪字段整批失败。
    这里**自带一份而不 import pg 那份**：qdrant 与 pg 是两条互斥后端，不该因为
    新后端而去依赖 pg 模块（它也延迟导入 psycopg）。
    """
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


class Bm25SparseEncoder:
    """本地 BM25 稀疏编码器：text → `(indices, values)`（喂给 Qdrant 稀疏向量）。

    打分口径对齐 Okapi BM25（与 `retrieval.BM25Retriever` 同一套数学），
    但为适配"稀疏向量 + 点积检索"做了重新分解：

        doc 权重(d, t) = tf(t,d) * (k1 + 1) / (tf(t,d) + k1 * (1 - b + b * dl_d / avgdl))
        query 权重(t)  = idf(t) * qtf(t)
        score          = Σ_t  query权重(t) * doc权重(d, t)     # 即稀疏向量的点积

    于是 Qdrant 的一次稀疏点积检索，返回的就是 BM25 相关性排序。

    分词口径：**字符 n-gram**（默认 2~4），与 `BM25_ANALYZER=char_wb` 的思路一致
    （中文无空格，词级分词会把整句当一个 token 导致零命中）。这里是它的近似
    （不做词边界填充）—— **但编码器对 query 与 doc 用的是同一套 `_tokenize`**，
    所以自洽；本后端不要求与进程内 `BM25Retriever` 的分词逐字一致。

    维度用**哈希映射**（token → [0, dim)）而不是全局词表：
      - 省掉一个随语料线性增长的 id↔term 字典；
      - 天然适配 Qdrant 稀疏向量的 uint32 索引。
    代价是哈希碰撞会带来极小噪声分数 —— dim 取 2^20 时碰撞率可忽略。
    """

    def __init__(self, texts, dim=1 << 20, k1=1.2, b=0.75, ngram=(2, 4)):
        """建语料统计量（idf / avgdl）。构造即算完，无增量接口。

        texts: 语料文本列表（每个元素是一块的检索文本）。
        dim: 稀疏向量维度（哈希空间大小）。
        k1 / b: BM25 参数（默认 Lucene 口径 1.2 / 0.75）。
        ngram: 字符 n-gram 的 (下界, 上界) 闭区间。

        空语料（或全为空串）时 idf 为空、avgdl 兜底成 1.0 —— 后续检索恒返回空，
        不会抛错（与 `NullRetriever` 的口径一致）。
        """
        self.dim = max(1, int(dim))
        self.k1 = float(k1)
        self.b = float(b)
        self.ngram = tuple(ngram)

        docs = [self._tokenize(t) for t in (texts or [])]
        n_docs = len(docs)
        self.n_docs = n_docs

        # df[t] = 出现在多少篇文档里；构造期一次算完。
        df: dict[str, int] = {}
        total_tokens = 0
        for toks in docs:
            total_tokens += len(toks)
            for t in set(toks):          # 同一篇里只计一次
                df[t] = df.get(t, 0) + 1

        avgdl = (total_tokens / n_docs) if n_docs else 0.0
        # avgdl 兜底 1.0 而非 0.0：avgdl=0 会让 dl/avgdl 变成 nan，污染所有分数。
        self.avgdl = avgdl if avgdl > 0 else 1.0

        # Lucene 口径的 IDF（log(1 + …) 保证恒正，极常见词不会算出负分）。
        self.idf = {
            t: math.log(1.0 + (n_docs - d + 0.5) / (d + 0.5))
            for t, d in df.items()
        }

    def _tokenize(self, text):
        """把一段文本切成字符 n-gram token 列表（n ∈ ngram 闭区间）。"""
        t = text or ""
        if not t:
            return []
        lo, hi = self.ngram
        n = len(t)
        out = []
        for k in range(lo, hi + 1):
            if n < k:
                continue
            out.extend(t[i:i + k] for i in range(n - k + 1))
        return out

    def _index(self, token):
        """token → 稀疏向量下标（blake2b 稳定哈希后取模）。

        **不用内置 hash()**：它带进程随机盐，跨进程不一致（与 point_id 同一条理由）。
        """
        h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(h, "big") % self.dim

    @staticmethod
    def _pack(pairs):
        """把 {index: value} 打包成 Qdrant 要求的 (indices 升序, values)。

        过滤非正值：BM25 权重恒 > 0，出现非正只可能是哈希抵消，直接丢弃更干净。
        Qdrant 要求 indices 严格升序且无重复 —— 用 dict 已天然去重，排序即可。
        """
        items = sorted((i, v) for i, v in pairs.items() if v > 0)
        return [i for i, _ in items], [v for _, v in items]

    def encode_document(self, text):
        """文档 → (indices, values)。返回 ([], []) 表示无有效 term。"""
        toks = self._tokenize(text)
        if not toks:
            return [], []
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        dl = len(toks)
        norm = self.k1 * (1.0 - self.b + self.b * dl / self.avgdl)
        pairs: dict[int, float] = {}
        for t, f in tf.items():
            idf = self.idf.get(t, 0.0)
            if idf <= 0:
                continue
            w = idf * f * (self.k1 + 1.0) / (f + norm)
            idx = self._index(t)
            pairs[idx] = pairs.get(idx, 0.0) + w      # 哈希碰撞累加
        return self._pack(pairs)

    def encode_query(self, text):
        """查询 → (indices, values)。**不含** EMBEDDING_QUERY_PREFIX（那是稠密路的事）。

        查询里未出现在语料词表里的 token 直接跳过 —— 它们不可能命中任何文档
        （除极小概率哈希碰撞），留着只是徒增稀疏向量体积。
        """
        toks = self._tokenize(text)
        if not toks:
            return [], []
        qtf: dict[str, int] = {}
        for t in toks:
            qtf[t] = qtf.get(t, 0) + 1
        pairs: dict[int, float] = {}
        for t, q in qtf.items():
            idf = self.idf.get(t)
            if idf is None or idf <= 0:
                continue
            idx = self._index(t)
            pairs[idx] = pairs.get(idx, 0.0) + idf * q
        return self._pack(pairs)


class QdrantStore:
    """Qdrant 集合的读写封装：建集合 / 增量判定 / upsert / 稠密检索 / 稀疏检索。

    只负责"和 Qdrant 打交道"，不负责算 embedding（那是 `retrieval.QdrantRetriever` 的事）。
    """

    def __init__(self, url, api_key="", collection=DEFAULT_COLLECTION, tenant="",
                 timeout=10, hnsw_m=16, hnsw_ef_construct=100, on_disk=False):
        """记住连接与集合配置，构造时**不连服务**（与 PgVectorStore 同一约定）。

        url: Qdrant 地址（QDRANT_URL）。
        api_key: 可选密钥（QDRANT_API_KEY）。空 = 无鉴权（本地/内网）。
        collection: 集合名（会做字符白名单校验）。
        tenant: 租户 id。非空时**读写都锁定在该租户内**（payload 过滤）；空串 = 不隔离。
        timeout: 单次请求超时（秒）。
        hnsw_m / hnsw_ef_construct: 稠密 HNSW 建索引参数（只影响首次建集合）。
        on_disk: 向量是否落盘（大数据量省内存，代价是检索变慢）。
        """
        self.url = (url or "").strip()
        self.api_key = (api_key or "").strip() or None
        self.collection = _safe_collection(collection)
        self.tenant = (tenant or "").strip()
        self.timeout = timeout
        self.hnsw_m = hnsw_m
        self.hnsw_ef_construct = hnsw_ef_construct
        self.on_disk = bool(on_disk)
        self._client = None

    # ---------- 连接 ----------
    def _connect(self):
        """创建 QdrantClient（**首次调用才建**，并复用）。

        延迟导入 `qdrant_client`：没装时抛 ImportError，由调用方（build_retriever）
        捕获后回退。QdrantClient 构造本身是懒连接、不建连 —— 真正的探活放在
        `ensure_schema` 里（见下），这样"地址不通"能在构造后端时立刻暴露。
        """
        if self._client is not None:
            return self._client
        if not self.url:
            raise RuntimeError("未配置 QDRANT_URL，无法使用 Qdrant 后端")
        from qdrant_client import QdrantClient

        self._client = QdrantClient(url=self.url, api_key=self.api_key,
                                    timeout=self.timeout)
        return self._client

    # ---------- 建集合 ----------
    def ensure_schema(self, dim):
        """确保集合、双命名向量与租户 payload 索引存在。幂等，可重复调用。

        dim: 稠密向量维度（bge-m3 = 1024）。只影响"首次建集合"；集合已存在时
            不改配置，但会**校验维度一致**（不一致显式报错，见下）。

        首次会**显式探活**（`get_collections`）—— QdrantClient 构造是懒连接，
        没有这一步，"地址填错"会一直拖到第一次检索才炸，而那时已经在请求路径上。
        """
        dim = int(dim)
        if dim <= 0:
            raise ValueError("向量维度必须为正整数")
        from qdrant_client import models as qm

        client = self._connect()
        existing = {c.name for c in client.get_collections().collections}

        if self.collection not in existing:
            client.create_collection(
                collection_name=self.collection,
                vectors_config={
                    DENSE_VECTOR: qm.VectorParams(
                        size=dim,
                        distance=qm.Distance.COSINE,
                        hnsw_config=qm.HnswConfigDiff(
                            m=self.hnsw_m, ef_construct=self.hnsw_ef_construct),
                        on_disk=self.on_disk,
                    )
                },
                sparse_vectors_config={
                    SPARSE_VECTOR: qm.SparseVectorParams(
                        index=qm.SparseIndexParams(on_disk=self.on_disk))
                },
            )
            logging.info("已创建 Qdrant 集合 %s（dense=%d 维 + sparse）", self.collection, dim)
        else:
            # ---- 维度校验：集合已存在且维度不符时必须**显式报错** ----
            # 同 pgvector：换过 embedding 模型后，旧向量"看起来还在、其实已过期"，
            # 且之后每次 upsert 都因维度不符失败 —— 报错点离真因（换了模型）很远。
            actual = self._dense_dim(client)
            if actual and actual != dim:
                raise RuntimeError(
                    f"Qdrant 集合 {self.collection} 的 dense 是 {actual} 维，"
                    f"但本次要写入 {dim} 维。这通常意味着换过 embedding 模型。"
                    "改维度无法原地完成（既有向量无法猜测新维度含义），"
                    f"请把 QDRANT_COLLECTION 换成新名（如 {self.collection}_d{dim}）重新灌库。"
                )

        # 给 tenant 建 keyword payload 索引：多租户过滤走索引而非全扫。
        # 已存在时 Qdrant 返回错误，属正常，降级为 debug 日志。
        try:
            client.create_payload_index(
                collection_name=self.collection,
                field_name="tenant",
                field_schema=qm.PayloadSchemaType.KEYWORD,
            )
        except Exception as e:  # noqa: BLE001  （索引多半已存在）
            logging.debug("tenant payload 索引未新建（多半已存在）：%s", e)

    def _dense_dim(self, client):
        """读集合里 dense 命名向量的维度；读不到返回 0。"""
        try:
            info = client.get_collection(self.collection)
            vectors = info.config.params.vectors
            if isinstance(vectors, dict):
                dv = vectors.get(DENSE_VECTOR)
                return int(getattr(dv, "size", 0) or 0)
            return int(getattr(vectors, "size", 0) or 0)
        except Exception:  # noqa: BLE001
            return 0

    # ---------- 增量判定 ----------
    def existing_ids(self):
        """返回集合里**该租户**已有的全部 point id（set[str]）。

        设了 tenant 时只返回该租户的 —— 否则会把别的租户的块判成"已存在"从而漏灌。
        集合不存在（首次灌库）时返回空集 —— 让所有块都被判为缺失、走全量写入。
        """
        client = self._connect()
        flt = self._tenant_filter()
        ids: set[str] = set()
        offset = None
        try:
            while True:
                points, offset = client.scroll(
                    collection_name=self.collection,
                    scroll_filter=flt,
                    limit=512,
                    offset=offset,
                    with_payload=False,
                    with_vectors=False,
                )
                for p in points:
                    ids.add(str(p.id))
                if offset is None:
                    break
        except Exception as e:  # noqa: BLE001  （集合不存在等 → 视为空，走首灌路径）
            logging.debug("Qdrant 读取已有 id 失败（按空集处理）：%s", e)
            return set()
        return ids

    # ---------- 写入 ----------
    def upsert(self, rows):
        """按 point id upsert 一批块，返回写入条数。

        rows: 可迭代，每项是 `(pid, chunk_dict, text, dense_vector, sparse_indices, sparse_values)`：
            - pid: `point_id(chunk_fingerprint)` 的结果；
            - chunk_dict: **完整块 dict**，落 payload 的 "chunk" 键，检索后原样回读；
            - text: 检索文本（落独立 payload 字段，便于调试与排查）；
            - dense_vector: 稠密向量（list[float] / numpy 数组）；
            - sparse_indices / sparse_values: BM25 稀疏向量（等长列表，可为空表示无稀疏）。
        """
        rows = list(rows)
        if not rows:
            return 0
        from qdrant_client import models as qm

        client = self._connect()
        points = []
        for pid, meta, text, dense, s_idx, s_val in rows:
            vectors = {DENSE_VECTOR: list(dense)}
            if s_idx:
                vectors[SPARSE_VECTOR] = qm.SparseVector(indices=list(s_idx),
                                                         values=list(s_val))
            payload = {
                "tenant": self.tenant,
                "source": str(meta.get("source", "") or ""),
                # source_key / content_hash 是**级联删除的判定依据**（见 kb_inventory）。
                # 顶层单列一份（而不是只藏在 chunk 里）：Qdrant 的过滤只能打到
                # 顶层 payload 字段，要按源删点就必须能直接 match 到它。
                "source_key": source_key(meta),
                "content_hash": str(meta.get("content_hash", "") or ""),
                "section": str(meta.get("section", "") or ""),
                "chunk_id": str(meta.get("chunk_id", "") or meta.get("id", "") or ""),
                "kind": str(meta.get("kind", "") or ""),
                "text": text,
                "chunk": _jsonable(meta),
            }
            points.append(qm.PointStruct(id=pid, vector=vectors, payload=payload))
        client.upsert(collection_name=self.collection, points=points, wait=True)
        return len(points)

    # ---------- 检索 ----------
    def search_dense(self, vector, top_k=3, sources=None):
        """稠密近邻检索，返回 `[(chunk_dict, score), ...]`（score = 余弦相似度，越大越相关）。"""
        client = self._connect()
        hits = self._query(client, query=list(vector), using=DENSE_VECTOR,
                           top_k=top_k, flt=self._source_filter(sources))
        return self._to_chunks(hits)

    def search_sparse(self, indices, values, top_k=3, sources=None):
        """稀疏（BM25）检索，返回 `[(chunk_dict, score), ...]`（score = 点积，越大越相关）。"""
        from qdrant_client import models as qm

        client = self._connect()
        q = qm.SparseVector(indices=list(indices), values=list(values))
        hits = self._query(client, query=q, using=SPARSE_VECTOR,
                           top_k=top_k, flt=self._source_filter(sources))
        return self._to_chunks(hits)

    def _query(self, client, query, using, top_k, flt):
        """统一走 query_points（qdrant-client ≥1.10 的现代 API）。"""
        resp = client.query_points(
            collection_name=self.collection,
            query=query,
            using=using,
            limit=int(top_k),
            query_filter=flt,
            with_payload=True,
            with_vectors=False,
        )
        return list(resp.points)

    @staticmethod
    def _to_chunks(hits):
        """命中点 → [(chunk_dict, score)]，跳过没有 chunk payload 的脏点。"""
        out = []
        for p in hits:
            payload = p.payload or {}
            chunk = payload.get("chunk")
            if chunk is None:
                continue
            out.append((chunk, float(p.score)))
        return out

    # ---------- 过滤器 ----------
    def _tenant_filter(self):
        """仅租户过滤（给 existing_ids 用）。无 tenant 时返回 None。"""
        from qdrant_client import models as qm

        if not self.tenant:
            return None
        return qm.Filter(must=[
            qm.FieldCondition(key="tenant", match=qm.MatchValue(value=self.tenant))
        ])

    def _source_filter(self, sources=None):
        """租户 + 可选来源过滤（给检索用）。都为空的组合返回 None。"""
        from qdrant_client import models as qm

        must = []
        if self.tenant:
            must.append(qm.FieldCondition(key="tenant",
                                          match=qm.MatchValue(value=self.tenant)))
        if sources:
            must.append(qm.FieldCondition(key="source",
                                          match=qm.MatchAny(any=list(sources))))
        return qm.Filter(must=must) if must else None

    # ---------- 维护 ----------
    def count(self):
        """返回集合里该租户的点数（近似，来自集合统计）。

        ⚠️ **未设租户时会多算索引身份的元数据点（+1）**：meta 点的 payload 里
        **没有 `tenant` 字段**，所以走 tenant 过滤的精确计数天然数不到它；
        而不带过滤的 `points_count` 是集合级统计，会把它算进去。
        这是刻意接受的：`count` 的用途只是"库里有多少块"的量级判断，±1 不影响任何
        决策；而为了减掉它去付一次 `exact=True` 全扫，在百万级集合上是得不偿失的。
        """
        client = self._connect()
        try:
            info = client.get_collection(self.collection)
            if self.tenant:
                res = client.count(collection_name=self.collection,
                                   count_filter=self._tenant_filter(), exact=True)
                return int(res.count)
            return int(info.points_count or 0)
        except Exception:  # noqa: BLE001
            return 0

    # ---------- 索引身份（embedding 指纹） ----------
    def index_dim(self):
        """读集合里 dense 命名向量的实际维度；集合不存在时返回 0。

        **只读**：不建集合、不写点、不算 embedding —— 启动自检的入口不能有副作用。
        复用 `ensure_schema` 用的 `_dense_dim`（同一套取维度的逻辑）。
        """
        return self._dense_dim(self._connect())

    @staticmethod
    def _meta_point_id(key):
        """元数据点的 id：在 id 空间里**与真实块刻意隔离**。

        真实块的 id 是 `point_id(chunk_fingerprint)`（sha1 十六进制串）；
        这里先拼上 `META_FLAG:` 前缀再 uuid5，两者不可能撞 ——
        "元数据点被当成某个块"是本设计里最坏的一类事故（会污染增量判定与检索）。
        """
        return point_id(f"{META_FLAG}:{key}")

    def get_meta(self, key):
        """读一条元数据；集合/点不存在都返回 None（= 没记录过，不是错误）。"""
        client = self._connect()
        try:
            got = client.retrieve(
                collection_name=self.collection,
                ids=[self._meta_point_id(key)],
                with_payload=True,
                with_vectors=False,
            )
        except Exception as e:  # noqa: BLE001  （集合不存在 → 视为没记录）
            logging.debug("Qdrant 读取索引身份失败（按未记录处理）：%s", e)
            return None
        if not got:
            return None
        value = (got[0].payload or {}).get("value")
        return None if value is None else str(value)

    def set_meta(self, key, value):
        """写一条元数据点（**不带向量**）。

        Qdrant 的命名向量集合允许点不带向量（`vector={}`）。这类点**永远不出现在
        向量检索结果里**（没有向量可比），所以不会污染召回；代价是"滚一遍集合"的
        维护类接口会看到它 —— 因此凡是遍历 payload 的地方都必须识别并跳过
        （见 `_scroll_rows`），否则它会变成一条 `("", "", "")` 的脏行，
        进而把"某个源是孤儿"判错。
        """
        from qdrant_client import models as qm

        client = self._connect()
        client.upsert(
            collection_name=self.collection,
            points=[qm.PointStruct(
                id=self._meta_point_id(key),
                vector={},
                payload={META_FLAG: True, "key": str(key), "value": str(value)},
            )],
            wait=True,
        )

    # ---------- 按源维护（级联删除） ----------
    def _scroll_rows(self, flt=None):
        """滚一遍集合，产出 `(tenant, source_key, content_hash)` 的原始三元组。

        flt: 额外的过滤条件（None = 全集合）。
        集合不存在等异常按"空集合"处理（与 `existing_ids` 同口径），
        并且**只 debug 记录** —— 维护脚本不该因为集合还没建就报错。
        """
        client = self._connect()
        out = set()
        offset = None
        try:
            while True:
                points, offset = client.scroll(
                    collection_name=self.collection,
                    scroll_filter=flt,
                    limit=512,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for p in points:
                    pl = p.payload or {}
                    if pl.get(META_FLAG):
                        # 索引身份的元数据点：不是块，**必须**跳过。留着它会被
                        # `plan_orphans` 看成一条 source 为空的脏行，级联删除据此
                        # 报出的孤儿清单就不可信了（这条链路只用来做删除决策，
                        # 一个假输入就足以让人误删）。
                        continue
                    # ⚠️ 回落顺序必须与 `kb_inventory.source_key` 一致：
                    # 老点没有顶层 source_key（本次改造前灌的），此时用 source。
                    key = str(pl.get("source_key") or pl.get("source") or "")
                    out.add((str(pl.get("tenant") or ""),
                             key.replace("\\", "/"),
                             str(pl.get("content_hash") or "")))
                if offset is None:
                    break
        except Exception as e:  # noqa: BLE001  （集合不存在 → 视为空）
            logging.debug("Qdrant 读取源清单失败（按空处理）：%s", e)
            return set()
        return out

    def list_rows(self):
        """列出该租户（未设租户 = 全集合）的 `(tenant, 源标识, 内容指纹)`，去重。"""
        return self._scroll_rows(self._tenant_filter())

    def list_tenants(self):
        """列出集合里出现过的 tenant（升序）。

        Qdrant 没有 `SELECT DISTINCT`，只能滚一遍 payload 收集 —— 这是维护动作，
        不是请求路径，可接受。
        """
        return sorted({t for t, _, _ in self._scroll_rows(None)})

    def delete_keys(self, keys):
        """按源标识删除点，返回**实际删除的点数**。

        keys: 源标识的可迭代。空清单直接返回 0（不发请求）。

        过滤用 `should`（OR）匹配两个字段，而不是只匹配 `source_key`：
        本次改造之前灌的点**没有** `source_key` 这个 payload 字段，
        只按它筛会对老数据**一条也删不掉**（预演说有孤儿、真删是 0）。
        """
        keys = sorted({str(k).strip() for k in (keys or []) if str(k).strip()})
        if not keys:
            return 0
        from qdrant_client import models as qm

        must = []
        if self.tenant:
            must.append(qm.FieldCondition(key="tenant",
                                          match=qm.MatchValue(value=self.tenant)))
        should = [
            qm.FieldCondition(key=field, match=qm.MatchAny(any=list(keys)))
            for field in ("source_key", "source")
        ]
        flt = qm.Filter(must=must, should=should)
        client = self._connect()
        # Qdrant 的 delete 不回"删了几条"（只有 operation_id），所以先按同一个过滤条件
        # 精确 count 一次 —— 删的就是它数到的这批点。不回数会让人无法核对
        # "预演说有 N 条、真删了多少"，而那正是这类脚本唯一的安全感来源。
        try:
            matched = int(client.count(collection_name=self.collection,
                                       count_filter=flt, exact=True).count)
        except Exception as e:  # noqa: BLE001  （集合不存在等 → 没有可删的）
            logging.debug("Qdrant 按源计数失败（按 0 处理）：%s", e)
            return 0
        if not matched:
            return 0
        try:
            client.delete(collection_name=self.collection,
                          points_selector=flt, wait=True)
        except Exception as e:  # noqa: BLE001
            logging.warning("Qdrant 按源删除失败：%s", e)
            return 0
        return matched
