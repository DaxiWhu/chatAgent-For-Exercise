"""知识库级联删除的契约（`src/rag/kb_inventory.py` + `scripts/kb_prune.py`）。

灌库是幂等的，但**没有反向路径**：源文件删了/改了之后，向量库里那批块会永久驻留。
后果分两层 —— 用户问到已下线的内容仍会被检索到并照答（答案层），以及对外声明
"已删除"的数据仍可被搜出来（合规层）。

这里钉住的都是**失效后不可逆或静默**的部分：

  1. `source_path` / `content_hash` **绝不能进 `chunk_id`** —— 进去就等于灌一次库
     全库重建 + 全部引用失效；
  2. 语料目录扫不到文件时**拒绝执行**（那是"语料没挂进容器"，不是"你把语料删光了"）
     —— 照常比对会把全库判成孤儿；
  3. 判不了新旧的老数据（无指纹 / 重名文件）**永不删**；
  4. "内容已变"的旧块默认**只报告不清理**（先删后灌会造成知识库空窗）；
  5. pg 的 SQL 源键表达式与 Python 的 `source_key` 必须同口径 —— 不一致时会出现
     "预演说有 N 条孤儿、真删是 0 条"这种查不出原因的偏差。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PRUNE_SCRIPT = ROOT / "scripts" / "kb_prune.py"


def _load(path: Path, name: str):
    """按路径加载一个不属任何包的模块（scripts/ 不是包）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


import src.rag.kb_inventory as kb  # noqa: E402
from src.rag.qdrant_store import QdrantStore  # noqa: E402
from src.rag.vector_store import SOURCE_KEY_SQL, PgVectorStore, chunk_fingerprint  # noqa: E402

# ---------------------------------------------------------------------------
# 1. 源清单扫描
# ---------------------------------------------------------------------------

def _make_corpus(root: Path) -> None:
    """造一棵有代表性的语料树：子目录、隐藏目录、不受支持的扩展名。"""
    (root / "sub").mkdir(parents=True, exist_ok=True)
    (root / ".parse_cache").mkdir(parents=True, exist_ok=True)
    (root / "sub" / "a.md").write_text("# A\n内容甲\n", encoding="utf-8")
    (root / "b.md").write_text("# B\n内容乙\n", encoding="utf-8")
    # 隐藏目录里的文件**不入清单**：`glob("**/*")` 不匹配点开头的目录，
    # 而 load_chunks 用的是同一句 glob ⇒ 两边对"语料里有哪些文件"永远同口径。
    (root / ".parse_cache" / "a.md.json").write_text("{}", encoding="utf-8")
    (root / "c.bin").write_bytes(b"\x00\x01")


def test_corpus_index_uses_posix_relative_paths(tmp_path):
    """清单里的键必须是**相对 POSIX 路径**（不是 basename、不是反斜杠）。"""
    _make_corpus(tmp_path)
    idx = kb.corpus_index(tmp_path, extensions={".md"})
    assert set(idx["files"]) == {"sub/a.md", "b.md"}
    assert idx["count"] == 2
    assert "\\" not in "".join(idx["files"])


def test_corpus_index_skips_hidden_dirs_like_load_chunks(tmp_path):
    """`data/.parse_cache` 这类隐藏目录不能进清单 —— 否则解析缓存会被当成语料。"""
    _make_corpus(tmp_path)
    idx = kb.corpus_index(tmp_path, extensions={".md"})
    assert not any(".parse_cache" in k for k in idx["files"])


def test_corpus_index_filters_by_extension_but_records_basenames(tmp_path):
    """不受支持的扩展名不入清单（它压根没被灌过库）。"""
    _make_corpus(tmp_path)
    idx = kb.corpus_index(tmp_path, extensions={".md"})
    assert "c.bin" not in idx["files"]
    assert set(idx["bases"]) == {"a.md", "b.md"}


def test_corpus_index_of_missing_dir_is_empty_not_an_error(tmp_path):
    """目录不存在 → 空清单（调用方据此拒绝执行，不该在这里抛）。"""
    idx = kb.corpus_index(tmp_path / "nope", extensions={".md"})
    assert idx == {"count": 0, "files": {}, "bases": {}}


def test_corpus_index_marks_duplicate_basenames_as_ambiguous(tmp_path):
    """同名不同目录 ⇒ `bases[名字] is None`（无法唯一定位，判定时按"存在"处理）。"""
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    (tmp_path / "x" / "same.md").write_text("1", encoding="utf-8")
    (tmp_path / "y" / "same.md").write_text("2", encoding="utf-8")
    idx = kb.corpus_index(tmp_path, extensions={".md"})
    assert idx["bases"]["same.md"] is None


def test_corpus_index_uses_real_supported_extensions_by_default(tmp_path):
    """不传 extensions 时走 `loaders.supported_extensions()`（与灌库同一份白名单）。"""
    from src.rag.loaders import supported_extensions

    exts = supported_extensions()
    assert exts, "块解析器注册表为空，语料白名单无从谈起"
    ext = sorted(exts)[0]
    (tmp_path / f"f{ext}").write_text("x", encoding="utf-8")
    (tmp_path / "f.unknown-ext").write_text("x", encoding="utf-8")
    idx = kb.corpus_index(tmp_path)
    assert set(idx["files"]) == {f"f{ext}"}


def test_file_digest_is_content_addressed_and_fails_open(tmp_path):
    """同内容同指纹、改内容变指纹、读不到时返回空串（= "未知"，不是"变了"）。"""
    p = tmp_path / "f.md"
    p.write_text("abc", encoding="utf-8")
    d1 = kb.file_digest(p)
    assert d1 and d1 == kb.file_digest(p)
    p.write_text("abd", encoding="utf-8")
    assert kb.file_digest(p) != d1
    assert kb.file_digest(tmp_path / "missing.md") == ""
    assert len(d1) == kb.HASH_CHARS


# ---------------------------------------------------------------------------
# 2. 源标识（Python 与 SQL 必须同口径）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("chunk,expected", [
    ({"source_path": "sub/a.md", "source": "a.md"}, "sub/a.md"),
    # 空串必须**回落** basename：等价于 SQL 里的 `NULLIF(...,'')`。
    ({"source_path": "", "source": "a.md"}, "a.md"),
    ({"source_path": None, "source": "a.md"}, "a.md"),
    ({"source": "a.md"}, "a.md"),
    ({"source_path": "sub\\a.md"}, "sub/a.md"),
    ({}, ""),
])
def test_source_key_semantics(chunk, expected):
    """`source_path` 优先、空串回落、反斜杠归一 —— 与 SQL 表达式逐条对应。"""
    assert kb.source_key(chunk) == expected


def test_pg_sql_source_key_matches_python_fallback():
    """pg 的源键表达式必须与 Python 同口径；`NULLIF` 掉了就会把空串当成有效路径。"""
    sql = " ".join(SOURCE_KEY_SQL.split())
    assert "NULLIF(chunk->>'source_path', '')" in sql, (
        "去掉 NULLIF 后，jsonb 里存在但为空串的 source_path 会被 COALESCE 当成有效值，"
        "于是判成脏行既不删也不报"
    )
    assert sql.startswith("COALESCE(") and sql.endswith(")"), sql
    assert sql[len("COALESCE("):-1].endswith(", source"), (
        "必须回落到 source（basename），否则老数据全判成缺失"
    )


def test_source_path_and_hash_do_not_change_chunk_id():
    """★ 硬约束：两个新字段**不参与** chunk_id。

    参与进去 = 灌一次库全库重建（id 全变）+ 全部引用失效（sha1 不可逆）。
    """
    base = {"source": "a.md", "section": "一", "text": "正文"}
    stamped = dict(base, source_path="sub/a.md", content_hash="deadbeef" * 2)
    assert chunk_fingerprint(base) == chunk_fingerprint(stamped)


# ---------------------------------------------------------------------------
# 3. 孤儿判定
# ---------------------------------------------------------------------------

def _index(**files):
    """手搓一份清单（files: rel=digest）。"""
    return {"count": len(files), "files": dict(files),
            "bases": {k.split("/")[-1]: k for k in files}}


def test_plan_orphans_classifies_four_states():
    idx = _index(**{"a.md": "h1", "b.md": "h2"})
    plan = kb.plan_orphans([
        ("", "a.md", "h1"),          # ok：路径在、指纹对
        ("", "b.md", "old"),         # stale：文件在、内容变了
        ("", "gone.md", "h3"),       # missing：文件没了
        ("", "c.md", ""),            # missing（且无指纹）—— 但键不在磁盘上
    ], idx)
    assert [k for _, k in plan["ok"]] == ["a.md"]
    assert [k for _, k in plan["stale"]] == ["b.md"]
    assert [k for _, k in plan["missing"]] == ["c.md", "gone.md"]
    assert plan["unknown"] == []


def test_plan_orphans_never_deletes_legacy_rows():
    """★ 老数据（无 content_hash）判不了新旧 ⇒ 进 unknown，**永不删**。"""
    idx = _index(**{"a.md": "h1"})
    plan = kb.plan_orphans([("", "a.md", "")], idx)
    assert plan["stale"] == [] and plan["missing"] == []
    assert [k for _, k in plan["unknown"]] == ["a.md"]


def test_plan_orphans_keeps_rows_matched_by_basename_only():
    """老数据只有 basename：语料在子目录里也**不能**被判成缺失。"""
    idx = _index(**{"sub/a.md": "h1"})     # bases: {"a.md": "sub/a.md"}
    plan = kb.plan_orphans([("", "a.md", "")], idx)
    assert plan["missing"] == []
    assert [k for _, k in plan["unknown"]] == ["a.md"]


def test_plan_orphans_treats_duplicate_basenames_as_alive():
    """重名文件（同名不同目录）无法唯一定位 ⇒ 只要还有同名文件就不敢删。"""
    idx = {"count": 2, "files": {"x/s.md": "h1", "y/s.md": "h2"},
           "bases": {"s.md": None}}
    plan = kb.plan_orphans([("", "s.md", "whatever")], idx)
    assert [k for _, k in plan["ok"]] == ["s.md"]


def test_plan_orphans_prefers_the_most_alive_verdict_per_key():
    """同一个键有多条记录时，"有一条活着"就按活着算（避免误删）。"""
    idx = _index(**{"a.md": "h1"})
    plan = kb.plan_orphans([("", "a.md", "old"), ("", "a.md", "h1")], idx)
    assert [k for _, k in plan["ok"]] == ["a.md"]
    assert plan["stale"] == []


def test_plan_orphans_keeps_tenants_apart_and_sorts():
    """键按 (租户, 源) 去重并排序 —— dry-run 的输出必须可复现。"""
    idx = _index(**{"a.md": "h1"})
    plan = kb.plan_orphans([("t2", "z.md", "h"), ("t1", "y.md", "h"),
                            ("t1", "y.md", "h")], idx)
    assert plan["missing"] == [("t1", "y.md"), ("t2", "z.md")]


def test_plan_orphans_ignores_rows_without_any_source():
    """连 source 都是空的脏行不能进任何桶 —— 它没法定位，也不该被删。"""
    plan = kb.plan_orphans([("", "", "")], _index(**{"a.md": "h1"}))
    assert all(not plan[s] for s in ("ok", "missing", "stale", "unknown"))


# ---------------------------------------------------------------------------
# 4. 打标
# ---------------------------------------------------------------------------

def test_stamp_source_writes_relative_path_and_digest(tmp_path):
    (tmp_path / "sub").mkdir()
    f = tmp_path / "sub" / "a.md"
    f.write_text("hello", encoding="utf-8")
    chunks = [{"source": "a.md", "text": "x"}, {"source": "a.md", "text": "y"}]
    out = kb.stamp_source(chunks, f, tmp_path)
    assert out is chunks                       # 就地改写，同一个对象
    assert all(c["source_path"] == "sub/a.md" for c in chunks)
    assert all(c["content_hash"] == kb.file_digest(f) for c in chunks)
    assert all(c["source"] == "a.md" for c in chunks), "source 语义不能变（basename）"


def test_stamp_source_omits_hash_when_unreadable(tmp_path):
    """算不出指纹时只写路径，**不写空 content_hash** —— 空值表示"未知"。"""
    chunks = [{"source": "a.md"}]
    kb.stamp_source(chunks, tmp_path / "gone.md", tmp_path)
    assert chunks[0]["source_path"] == "gone.md"
    assert "content_hash" not in chunks[0]


def test_stamp_source_on_empty_list_is_a_noop(tmp_path):
    assert kb.stamp_source([], tmp_path / "a.md", tmp_path) == []


def test_load_chunks_stamps_every_chunk(tmp_path):
    """端到端：`load_chunks` 的产物必须带上两个判定字段。"""
    from src.rag.loaders import supported_extensions

    if ".md" not in supported_extensions():
        pytest.skip(".md 无对应解析器")
    (tmp_path / "kb").mkdir()
    (tmp_path / "kb" / "doc.md").write_text("# 标题\n\n正文一段。\n", encoding="utf-8")

    from src.rag.retrieval import load_chunks

    chunks = load_chunks(str(tmp_path))
    assert chunks, "语料没解析出内容，用例前提不成立"
    for c in chunks:
        assert c["source_path"] == "kb/doc.md"
        assert c["content_hash"] == kb.file_digest(tmp_path / "kb" / "doc.md")


# ---------------------------------------------------------------------------
# 5. 后端必须成对（只在 pg 上加、忘了 qdrant = 换个后端就静默不清理）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls", [PgVectorStore, QdrantStore])
def test_both_backends_expose_source_maintenance_api(cls):
    for name in ("list_rows", "delete_keys", "list_tenants"):
        assert callable(getattr(cls, name, None)), f"{cls.__name__} 缺 {name}"


@pytest.mark.parametrize("store", [
    PgVectorStore.__new__(PgVectorStore),
    QdrantStore.__new__(QdrantStore),
])
def test_delete_keys_with_empty_list_never_connects(store):
    """空清单必须在**建立连接之前**返回 —— 否则一条没有目标清单的 DELETE 会打出去。

    这里连 `_connect` 都不该被调用：两个后端的连接方法在未配地址时都会抛，
    用例能通过本身就证明短路发生了。
    """
    store.tenant = ""
    store.table = "rag_chunks"
    store.collection = "rag_chunks"
    assert store.delete_keys([]) == 0
    assert store.delete_keys([""]) == 0
    assert store.delete_keys(None) == 0


def test_qdrant_delete_matches_legacy_points_that_lack_source_key():
    """老点没有 `source_key` payload ⇒ 删除过滤必须同时匹配 `source`。

    只按 `source_key` 筛的话，对本次改造之前灌的数据**一条也删不掉**，
    而预演（走 Python 侧的 basename 回落）却会报出孤儿 —— 两边不一致。
    """
    src = (ROOT / "src" / "rag" / "qdrant_store.py").read_text(encoding="utf-8")
    block = src.split("def delete_keys", 1)[1].split("def ", 1)[0]
    assert '"source_key", "source"' in block, "删除过滤漏了老数据的 source 字段"


# ---------------------------------------------------------------------------
# 6. 脚本：闸门与退出码
# ---------------------------------------------------------------------------

class FakeStore:
    """假向量库：只实现维护接口，用来验证脚本的编排与退出码。"""

    def __init__(self, rows, tenants=None):
        self.rows = list(rows)
        # `list_tenants()` 的默认返回值 = "库里只有一个没有租户名的分区"。
        # 它对应**真实库里那批老数据**（payload 里没写 `tenant`），而脚本拿到
        # 这个清单后会再用一个 `tenant=None`（不过滤）的句柄去扫它 —— 所以
        # 下面 `holder` 的默认键也是 `None`，两者必须对得上。
        self.tenants = list(tenants if tenants is not None else [None])
        self.deleted = []

    def list_rows(self):
        return set(self.rows)

    def list_tenants(self):
        return self.tenants

    def delete_keys(self, keys):
        self.deleted.append(sorted(keys))
        return len(keys)


@pytest.fixture()
def prune(monkeypatch):
    """加载脚本并把 `_make_store` 换成可注入的假后端。

    默认（没往 holder 里塞东西时）模拟内存后端 —— 那正是 `RAG_VECTOR_STORE` 的
    默认值，也是"什么都没配就跑"时脚本会遇到的情况。
    """
    mod = _load(PRUNE_SCRIPT, "kb_prune_under_test")
    holder = {}

    def _fake(tenant=None):
        """与真实 `_make_store` **同一个默认值**（`None` = 不过滤）。

        ⚠️ 默认值写 `""` 会让"探测句柄"悄悄取到一个真实租户的句柄 —— 而探测与
        遍历用同一个键时，`tenants=["t1","t2"]` 那种用例会静默退化成"只清了一个".
        """
        store = holder.get(tenant)
        if store is not None:
            return store, ""
        return None, ("RAG_VECTOR_STORE=memory 是内存后端（没有持久数据），"
                      "本脚本只在 pg / qdrant 后端下有意义")

    monkeypatch.setattr(mod, "_make_store", _fake)
    mod.holder = holder
    return mod


def _run(mod, argv, capsys):
    """驱动脚本并把 (退出码, stdout, stderr) 一起交回。"""
    code = mod.run(argv)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_script_refuses_when_corpus_is_empty(tmp_path, prune, capsys):
    """★ 语料目录扫不到文件时**拒绝执行**（退出码 2）—— 照常比对会清空全库。"""
    prune.holder[None] = FakeStore([("", "a.md", "h")])
    code, _out, err = _run(prune, ["--docs", str(tmp_path), "--apply"], capsys)
    assert code == 2
    assert "拒绝执行" in err
    assert "挂" in err, "提示必须指向「语料没挂进容器」这个最常见成因"
    assert prune.holder[None].deleted == [], "拒绝执行时绝不允许发出任何删除"


def test_script_refuses_missing_corpus_dir(tmp_path, prune, capsys):
    code, _out, err = _run(prune, ["--docs", str(tmp_path / "nope")], capsys)
    assert code == 2 and "拒绝执行" in err


def test_script_refuses_memory_backend(tmp_path, prune, capsys):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    code, _out, err = _run(prune, ["--docs", str(tmp_path)], capsys)
    assert code == 2
    assert "内存后端" in err


def test_script_dry_run_reports_without_deleting(tmp_path, prune, capsys):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    store = FakeStore([("", "a.md", kb.file_digest(tmp_path / "a.md")),
                       ("", "gone.md", "h")])
    prune.holder[None] = store
    code, out, _err = _run(prune, ["--docs", str(tmp_path)], capsys)
    assert code == 0
    assert "源已消失 1 份" in out and "[dry-run]" in out
    assert store.deleted == []


def test_script_apply_deletes_missing_but_keeps_stale(tmp_path, prune, capsys):
    """★ 默认只删「源已消失」；「内容已变」必须先灌新再删（避免知识库空窗）。"""
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    store = FakeStore([("", "a.md", "stale-hash"), ("", "gone.md", "h")])
    prune.holder[None] = store
    code, out, _err = _run(prune, ["--docs", str(tmp_path), "--apply"], capsys)
    assert code == 0
    assert store.deleted == [["gone.md"]]
    assert "内容已变 1 份" in out
    assert "--include-stale" in out, "要提示运维还有一批没清"


def test_script_include_stale_deletes_both(tmp_path, prune, capsys):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    store = FakeStore([("", "a.md", "stale-hash"), ("", "gone.md", "h")])
    prune.holder[None] = store
    code, _out, _err = _run(prune, ["--docs", str(tmp_path), "--apply",
                                    "--include-stale"], capsys)
    assert code == 0
    assert store.deleted == [["a.md", "gone.md"]]


def test_script_never_deletes_rows_without_hash(tmp_path, prune, capsys):
    """老数据（无指纹）永远不进删除清单 —— 端到端再确认一次。"""
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    store = FakeStore([("", "a.md", "")])
    prune.holder[None] = store
    code, out, _err = _run(prune, ["--docs", str(tmp_path), "--apply",
                                   "--include-stale"], capsys)
    assert code == 0
    assert store.deleted == [[]], "只允许发出一次空清单的调用，不得真删"
    assert "无法判定 1 份" in out


def test_script_json_output_is_machine_readable(tmp_path, prune, capsys):
    import json as _json

    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    prune.holder[None] = FakeStore([("", "gone.md", "h")])
    code, out, _err = _run(prune, ["--docs", str(tmp_path), "--json"], capsys)
    assert code == 0
    data = _json.loads(out)
    assert data["mode"] == "dry-run"
    assert data["corpus_files"] == 1
    assert data["totals"]["missing"] == 1
    assert data["totals"]["deleted_rows"] == 0


def test_script_all_tenants_walks_every_tenant(tmp_path, prune, capsys):
    """多租户必须逐个租户清理 —— 一条不限租户的删除会跨租户误删。"""
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    prune.holder[None] = FakeStore([], tenants=["t1", "t2"])
    prune.holder["t1"] = FakeStore([("t1", "gone1.md", "h")])
    prune.holder["t2"] = FakeStore([("t2", "gone2.md", "h")])
    code, out, _err = _run(prune, ["--docs", str(tmp_path), "--apply",
                                   "--all-tenants"], capsys)
    assert code == 0
    assert prune.holder["t1"].deleted == [["gone1.md"]]
    assert prune.holder["t2"].deleted == [["gone2.md"]]
    assert "租户 t1" in out and "租户 t2" in out


def test_script_all_tenants_with_no_data_still_succeeds(tmp_path, prune, capsys):
    """表存在但还没数据：走一遍、报「源已消失 0 份」，退出码仍是 0（不是错误）。"""
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    prune.holder[None] = FakeStore([], tenants=[])
    code, out, _err = _run(prune, ["--docs", str(tmp_path), "--all-tenants"], capsys)
    assert code == 0 and "源已消失 0 份" in out


def test_script_exit_1_when_store_explodes(tmp_path, prune, capsys):
    """后端报错 → 退出码 1（执行失败），与「无法判定」的 2 区分开。

    两种退出码混同的代价很实在：CI / 巡检把 2 当成 1 就会去"修"一个其实
    只是缺配置的东西，而把 1 当成 2 会让真正的清理失败被忽略。
    """
    (tmp_path / "a.md").write_text("x", encoding="utf-8")

    class Boom(FakeStore):
        def list_rows(self):
            raise RuntimeError("connection refused")

    prune.holder[None] = Boom([])
    code, _out, err = _run(prune, ["--docs", str(tmp_path)], capsys)
    assert code == 1
    assert "清理失败" in err and "connection refused" in err


def test_script_without_tenant_cleans_every_partition(tmp_path, prune, capsys):
    """★ 不给 `--tenant` 时清理对象是**库里出现过的每个租户**，不是某一个分区。

    为什么单钉一条：脚本的"默认范围"曾经是"整个库"（那时空串等于不过滤）。
    口径反转后如果不显式处理，默认值会经身份归一化落到 `RAG_TENANT` —— 表现是
    **只清公共分区、别的租户的孤儿一条都不碰**，而输出看起来完全正常。
    """
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    prune.holder[None] = FakeStore([], tenants=["t1", "t2"])
    prune.holder["t1"] = FakeStore([("t1", "gone1.md", "h")])
    prune.holder["t2"] = FakeStore([("t2", "gone2.md", "h")])

    code, _out, _err = _run(prune, ["--docs", str(tmp_path), "--apply"], capsys)

    assert code == 0
    assert prune.holder["t1"].deleted == [["gone1.md"]]
    assert prune.holder["t2"].deleted == [["gone2.md"]], \
        "不给 --tenant 时漏掉了别的分区（默认范围被收窄了）"


def test_script_tenant_flag_is_a_literal_partition_name(tmp_path, prune, capsys):
    """★ `--tenant` 是**字面**分区名，不做身份归一化。

    `--tenant ""` 要清的正是"没有租户名"那批老数据所在的分区。若这里改走
    `resolve_tenant`，空串会被归一成公共租户 / 哨兵值 ⇒ 脚本去清一个空分区，
    打印"源已消失 0 份"、退出码 0 —— **在什么都没做的情况下报成功**。
    """
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    prune.holder[""] = FakeStore([("", "gone.md", "h")])

    code, _out, _err = _run(
        prune, ["--docs", str(tmp_path), "--apply", "--tenant", ""], capsys)

    assert code == 0
    assert prune.holder[""].deleted == [["gone.md"]], \
        "`--tenant \"\"` 没落到空串分区（说明被归一化成了别的分区名）"


def test_script_refuses_conflicting_scope_flags(tmp_path, prune, capsys):
    """`--tenant` 与 `--all-tenants` 同时给出时必须拒绝 —— 两者语义互斥。"""
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    code, _out, err = _run(
        prune, ["--docs", str(tmp_path), "--tenant", "public", "--all-tenants"],
        capsys)
    assert code == 2
    assert "不能同时给" in err
