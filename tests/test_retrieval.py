"""RAG 检索层测试：全部离线，不调用任何外部服务。"""
import logging

import src.rag.retrieval as R
from src.rag.retrieval import (
    BM25Retriever,
    NullRetriever,
    build_prompt,
    build_retriever,
    embedding_configured,
    load_documents,
    split_documents,
)

CHUNKS = [
    {"source": "intro.txt", "text": "梧州是广西壮族自治区的一个地级市，地处广西东部，靠近广东。"},
    {"source": "intro.txt", "text": "梧州特产有龟苓膏、冰泉豆浆、六堡茶。六堡茶是黑茶的一种，有祛湿功效。"},
    {"source": "intro.txt", "text": "梧州代表性景点有骑楼城、龙母庙、白云山。"},
]


# ---------------- 稀疏检索（BM25）中文召回（回归）----------------
# 打分公式本身的测试在 tests/test_bm25.py；这里守的是"中文能不能召回"这个端到端前提。
def test_bm25_chinese_retrieval_works():
    """回归：默认 word 分词在中文上实测 0 命中；改用字符 n-gram 后必须能召回。"""
    r = BM25Retriever(CHUNKS)
    for q in ["梧州有什么特产？", "六堡茶是什么茶？", "骑楼城", "梧州的气候怎么样"]:
        hits = r.search(q, top_k=1)
        assert hits, f"查询 {q!r} 未召回任何片段"
        assert hits[0][1] > 0


def test_bm25_picks_most_relevant_chunk():
    r = BM25Retriever(CHUNKS)
    hits = r.search("六堡茶", top_k=1)
    assert hits
    assert "六堡茶" in hits[0][0]["text"]
    # 返回结构必须是 [(chunk_dict, score)]，与 retrieve 节点约定一致
    chunk, score = hits[0]
    assert set(chunk) >= {"source", "text"}
    assert isinstance(score, float)


# ---------------- 加载 / 切分 ----------------
def test_load_documents_missing_dir_returns_empty():
    assert load_documents("no/such/dir") == []
    assert load_documents("") == []
    assert load_documents(None) == []


def test_load_and_split_roundtrip(tmp_path):
    (tmp_path / "a.txt").write_text("梧州特产有六堡茶。", encoding="utf-8")
    docs = load_documents(str(tmp_path))
    assert len(docs) == 1 and docs[0][0] == "a.txt"
    chunks = split_documents(docs)
    assert chunks and chunks[0]["source"] == "a.txt"
    assert "六堡茶" in chunks[0]["text"]


# ---------------- 中文分隔符（递归切分不再句中硬切）----------------
# 单段、无换行、共 4 句，总长 88 字，超过 chunk_size 必须被切开
_LONG_ZH = (
    "梧州是广西壮族自治区的一个地级市，地处广西东部，靠近广东。"
    "梧州特产有龟苓膏、冰泉豆浆、六堡茶。"
    "六堡茶属于黑茶，有祛湿功效，在当地很受欢迎。"
    "梧州代表性景点有骑楼城、龙母庙和白云山。"
)
_SENT_END = "。！？；，"
# 遍历多个长度：避免结论依赖"某个 size 恰好切在标点上"的巧合
_SIZES = (30, 40, 50, 60, 70)


def test_chinese_long_paragraph_has_no_mid_sentence_cut():
    """回归：补上中文分隔符后，无换行的中文长段落只会在标点处切分。

    判据方向说明：LangChain 的 keep_separator=True 把分隔符粘在**下一块的开头**，
    所以「块尾是标点」并不成立；「除首块外每块以标点起头」才是正确判据，
    它等价于「每个切点都落在句子边界上」。
    """
    split_seen = 0
    for size in _SIZES:
        pieces = R._make_splitter(size, 0).split_text(_LONG_ZH)
        assert "".join(pieces) == _LONG_ZH, f"size={size} 时切分有损，拼接不等于原文"
        for i, p in enumerate(pieces[1:], start=1):
            assert p[0] in _SENT_END, (
                f"size={size} 时第 {i} 块切在了句中：{p[:24]!r}"
            )
        split_seen += len(pieces) > 1
    assert split_seen, "没有任何 size 触发切分，本用例没有验证到切分行为"


def test_without_chinese_separators_it_would_cut_mid_sentence():
    """反证：修复前的分隔符配置在同样这批 size 下会出现句中硬切。

    显式复现旧配置而非依赖上游默认值，这样即使 LangChain 改了默认分隔符，本用例依然成立。
    """
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    broken = []
    for size in _SIZES:
        old = RecursiveCharacterTextSplitter(
            chunk_size=size, chunk_overlap=0, separators=["\n\n", "\n", " ", ""]
        )
        pieces = old.split_text(_LONG_ZH)
        broken += [
            (size, p[:24]) for p in pieces[1:] if p and p[0] not in _SENT_END
        ]
    assert broken, (
        "旧配置下本应出现句中截断——若不再出现，说明 SPLIT_SEPARATORS 可能已无必要，请复核"
    )


# ---------------- 后端选择 ----------------
def test_build_retriever_empty_chunks_returns_null():
    assert isinstance(build_retriever([]), NullRetriever)


def test_build_retriever_default_is_bm25(monkeypatch):
    monkeypatch.setattr(R, "EMBEDDING_BACKEND", "bm25")
    assert isinstance(build_retriever(CHUNKS), BM25Retriever)


def test_build_retriever_semantic_without_config_falls_back(monkeypatch):
    """选了 semantic 但 EMBEDDING_* 没填 → 自动回退 BM25，不抛异常。"""
    monkeypatch.setattr(R, "EMBEDDING_BACKEND", "semantic")
    monkeypatch.setattr(R, "EMBEDDING_API_KEY", "")
    monkeypatch.setattr(R, "EMBEDDING_MODEL", "")
    assert isinstance(build_retriever(CHUNKS), BM25Retriever)


def test_build_retriever_accepts_tfidf_as_legacy_alias(monkeypatch, caplog):
    """历史配置值 `RAG_EMBEDDING=tfidf` 仍要能用，**且必须留下告警**。

    契约点：这个值写在存量 .env 里。删掉别名的后果不是报错，而是它悄悄落进"其余值 →
    兜底"分支 —— 行为恰好一样，所以谁也不会发现，只是日志语义从此说不清。
    告警是那条"你该改配置了"的唯一出口。
    """
    monkeypatch.setattr(R, "EMBEDDING_BACKEND", "tfidf")
    with caplog.at_level(logging.WARNING):
        r = build_retriever(CHUNKS)
    assert isinstance(r, BM25Retriever)
    assert any("tfidf" in rec.message for rec in caplog.records), "别名路径必须告警"


def test_build_retriever_semantic_with_config_uses_embedding(monkeypatch):
    """配置齐全时应选择 embedding 检索器（此处替身，避免真调外部服务）。"""

    class FakeEmbeddingRetriever:
        def __init__(self, chunks):
            self.chunks = chunks

        def search(self, query, top_k=3):
            return []

    monkeypatch.setattr(R, "EMBEDDING_BACKEND", "semantic")
    monkeypatch.setattr(R, "EMBEDDING_API_KEY", "fake-key")
    monkeypatch.setattr(R, "EMBEDDING_MODEL", "fake-model")
    monkeypatch.setattr(R, "EmbeddingRetriever", FakeEmbeddingRetriever)

    r = build_retriever(CHUNKS)
    assert isinstance(r, FakeEmbeddingRetriever)


def test_embedding_configured_requires_key_and_model(monkeypatch):
    monkeypatch.setattr(R, "EMBEDDING_API_KEY", "")
    monkeypatch.setattr(R, "EMBEDDING_MODEL", "")
    assert embedding_configured() is False

    monkeypatch.setattr(R, "EMBEDDING_API_KEY", "k")
    assert embedding_configured() is False  # 只有 key 还不够

    monkeypatch.setattr(R, "EMBEDDING_MODEL", "m")
    assert embedding_configured() is True  # base 可留空


# ---------------- 提示词拼装（回归）----------------
def test_build_prompt_uses_chunk_fields_not_dict_keys():
    """回归：曾经写成 `for (src, txt), _ in contexts`，把 dict 解包成键名，
    导致提示词里出现字面量 '[来源: source] text' 而不是真实内容。"""
    prompt = build_prompt("梧州特产？", [(CHUNKS[1], 0.9)])
    assert "intro.txt" in prompt
    assert "六堡茶" in prompt
    assert "[来源: source]" not in prompt


# ---------------- embedding 查询前缀 ----------------
class _FakeEmbedder:
    """替身：记录被传入的文本，返回固定向量，避免真实调用 embedding 服务。"""

    def __init__(self):
        self.queries: list[str] = []
        self.documents: list[str] = []

    def embed_documents(self, texts):
        self.documents = list(texts)
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text):
        self.queries.append(text)
        return [1.0, 0.0]


def _patch_embedder(monkeypatch, fake):
    import langchain_openai

    monkeypatch.setattr(langchain_openai, "OpenAIEmbeddings", lambda **kw: fake)
    monkeypatch.setattr(R, "EMBEDDING_MODEL", "fake-model")
    monkeypatch.setattr(R, "EMBEDDING_API_KEY", "fake-key")


def test_query_prefix_applied_only_to_query(monkeypatch):
    """指令前缀只能加在查询侧，文档侧不能加。"""
    fake = _FakeEmbedder()
    _patch_embedder(monkeypatch, fake)
    monkeypatch.setattr(R, "EMBEDDING_QUERY_PREFIX", "PFX ")

    r = R.EmbeddingRetriever(CHUNKS)
    r.search("六堡茶")

    assert fake.queries == ["PFX 六堡茶"]
    assert all(not t.startswith("PFX") for t in fake.documents)


def test_no_prefix_by_default(monkeypatch):
    fake = _FakeEmbedder()
    _patch_embedder(monkeypatch, fake)
    monkeypatch.setattr(R, "EMBEDDING_QUERY_PREFIX", "")

    r = R.EmbeddingRetriever(CHUNKS)
    r.search("六堡茶")

    assert fake.queries == ["六堡茶"]


def test_embedding_retriever_returns_expected_shape(monkeypatch):
    fake = _FakeEmbedder()
    _patch_embedder(monkeypatch, fake)
    monkeypatch.setattr(R, "EMBEDDING_QUERY_PREFIX", "")

    r = R.EmbeddingRetriever(CHUNKS)
    hits = r.search("任意", top_k=2)
    assert len(hits) == 2
    chunk, score = hits[0]
    assert set(chunk) >= {"source", "text"}
    assert isinstance(score, float)


def test_embedding_base_url_normalized():
    """容错：把 …/v1/embeddings 整条填进来也要能用（客户端会再拼一次）。"""
    for raw, expected in [
        ("https://api.siliconflow.cn/v1/embeddings", "https://api.siliconflow.cn/v1"),
        ("https://api.siliconflow.cn/v1/", "https://api.siliconflow.cn/v1"),
        ("https://api.siliconflow.cn/v1", "https://api.siliconflow.cn/v1"),
        ("", ""),
    ]:
        assert R._normalize_api_base(raw) == expected


# ---------------- docx 专属链路接线（Docling 双路线 / 降级 python-docx）----------------
# 这里一律 monkeypatch 掉真正解析、只验证"分派与降级语义"：
# 真跑 Docling 的代价是分钟级，那是 tests/test_docling_loader.py 的职责（且能被 skip）。
def _write_docx(tmp_path):
    from tests.docx_fixtures import build_minimal_docx
    return build_minimal_docx(str(tmp_path / "report.docx"))


def _kc(text, source="report.docx", parser="docling"):
    from src.rag.knowledge_chunk import KnowledgeChunk
    return KnowledgeChunk(source=source, parser=parser, origin="docling_docx",
                          content=text, retrieval_text=text)


def test_docx_uses_docling_route_by_default(tmp_path, monkeypatch):
    """docx 必须走专属链路 —— 并入通用分支（load_text）就只剩正文，
    图片/原生图表/OLE 会一起丢掉，而它们恰恰是 docx 解析里最值钱的部分。"""
    import src.rag.docling_loader as DL

    _write_docx(tmp_path)
    seen = {}

    def fake(path, source):
        seen["path"] = path
        return DL.ParseOutcome(chunks=[_kc("来自 Docling 双路线的正文")], status="ok")

    monkeypatch.setattr(DL, "parse_docx", fake)
    chunks = R.load_chunks(str(tmp_path))
    assert seen.get("path", "").endswith("report.docx"), "docx 未走 Docling 链路"
    assert len(chunks) == 1
    assert "Docling 双路线" in chunks[0]["text"]
    assert chunks[0]["parser"] == "docling"
    assert chunks[0]["source"] == "report.docx"


def test_docx_falls_back_to_python_docx_when_docling_unavailable(tmp_path, monkeypatch):
    """降级要真的可用，且必须留痕（parser 字段），否则"这份文件其实走的老链路"查不出来。"""
    import src.rag.docling_loader as DL

    _write_docx(tmp_path)
    monkeypatch.setattr(DL, "parse_docx",
                        lambda p, s: DL.ParseOutcome(status="unavailable", reason="docling 未安装"))
    chunks = R.load_chunks(str(tmp_path))
    assert chunks, "降级链路必须仍能读出正文"
    assert all(c["parser"] == "python-docx" for c in chunks)
    assert any("季度销售报告" in c["text"] for c in chunks)


def test_docx_unprocessable_record_is_kept_not_downgraded(tmp_path, monkeypatch):
    """「解析不出内容」不是「解析器坏了」：降级只会更差，
    应保留这条显式失败记录（否则该文件在索引里凭空消失，没人知道它没被索引）。"""
    import src.rag.docling_loader as DL
    from src.rag.knowledge_chunk import unprocessable

    _write_docx(tmp_path)
    # 注意 parser 要显式传：真实生产者（docling_loader.parse_docx）就是带 parser="docling" 造的，
    # 这里照抄它的调法，否则测的是"我的假数据"而不是"真实产物"。
    monkeypatch.setattr(DL, "parse_docx",
                        lambda p, s: DL.ParseOutcome(
                            chunks=[unprocessable(s, "空文档", parser="docling")],
                            status="unprocessable", reason="空文档"))
    chunks = R.load_chunks(str(tmp_path))
    assert len(chunks) == 1
    assert chunks[0]["kind"] == "unprocessable"
    assert chunks[0]["parser"] == "docling", "不该悄悄换成 python-docx 链路"


def test_docx_route_can_be_disabled_by_env(tmp_path, monkeypatch):
    """RAG_DOC_PARSER 非 docling 时必须直接走老链路（这条开关是应急回退用的）。"""
    _write_docx(tmp_path)
    monkeypatch.setattr(R, "DOC_PARSER", "legacy")
    chunks = R.load_chunks(str(tmp_path))
    assert chunks and all(c["parser"] == "python-docx" for c in chunks)


# ---------------- 批次8：父子块（small-to-big）----------------
def _mk(source, text, section=""):
    return {"source": source, "section": section, "text": text}


def test_attach_parents_builds_a_window_around_each_chunk(monkeypatch):
    """父窗必须**围绕命中块**，而不是"取文档开头 1200 字"。

    后者最省事但没用：PDF 的 section 常为空，按节分组会退化成"永远是文档开头"，
    命中块根本不在父窗里，语境等于没给。
    """
    monkeypatch.setattr(R, "PARENT_CHILD", True)
    monkeypatch.setattr(R, "PARENT_MAX_CHARS", 120)
    chunks = [_mk("a.pdf", "第一块" + "甲" * 30), _mk("a.pdf", "第二块" + "乙" * 30),
              _mk("a.pdf", "第三块" + "丙" * 30)]
    R.attach_parents(chunks)
    for i, c in enumerate(chunks):
        assert c["parent_text"]
        assert c["text"] in c["parent_text"], f"第 {i} 块的父窗里居然没有它自己"
        assert c["parent_id"].startswith("a.pdf#")


def test_attach_parents_never_crosses_documents(monkeypatch):
    monkeypatch.setattr(R, "PARENT_CHILD", True)
    monkeypatch.setattr(R, "PARENT_MAX_CHARS", 500)
    chunks = [_mk("a.pdf", "甲" * 20), _mk("b.pdf", "乙" * 20), _mk("b.pdf", "丙" * 20)]
    R.attach_parents(chunks)
    assert "parent_text" not in chunks[0] or "乙" not in chunks[0]["parent_text"]
    assert "甲" not in chunks[2]["parent_text"]


def test_attach_parents_skips_isolated_chunk(monkeypatch):
    """一份文档只有一块时没有"父"可言，不要造一个等于自身的父块。"""
    monkeypatch.setattr(R, "PARENT_CHILD", True)
    chunks = [_mk("solo.txt", "只有一块")]
    R.attach_parents(chunks)
    assert "parent_text" not in chunks[0]


def test_attach_parents_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(R, "PARENT_CHILD", False)
    chunks = [_mk("a.pdf", "甲"), _mk("a.pdf", "乙")]
    R.attach_parents(chunks)
    assert all("parent_text" not in c for c in chunks)


def test_build_prompt_prefers_parent_window():
    """送模型的是父窗（含上下文），不是孤立的小块。"""
    ctx = [(_mk("a.pdf", "命中句", section="二"), 0.9)]
    ctx[0][0]["parent_text"] = "上一段。命中句。下一段。"
    prompt = build_prompt("问", ctx)
    assert "上一段。命中句。下一段。" in prompt


def test_build_prompt_falls_back_to_text_without_parent():
    """没有父窗时行为不变 —— 旧数据/单块文档不受影响。"""
    prompt = build_prompt("问", [(_mk("a.pdf", "只有这一句"), 0.9)])
    assert "只有这一句" in prompt


def test_load_chunks_attaches_parents_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "PARENT_CHILD", True)
    monkeypatch.setattr(R, "PARENT_MAX_CHARS", 200)
    (tmp_path / "a.txt").write_text("梧州特产有六堡茶。梧州还有龟苓膏。", encoding="utf-8")
    (tmp_path / "b.txt").write_text("另一份文档的内容。", encoding="utf-8")
    chunks = R.load_chunks(str(tmp_path), chunk_size=10, chunk_overlap=0)
    assert len(chunks) > 2
    with_parent = [c for c in chunks if c.get("parent_text")]
    assert with_parent, "灌库时应自动挂上父窗"
    for c in with_parent:
        assert c["text"] in c["parent_text"]
    # a.txt 的父窗里不能出现 b.txt 的内容
    a = [c for c in with_parent if c["source"] == "a.txt"]
    assert a and all("另一份文档" not in c["parent_text"] for c in a)
