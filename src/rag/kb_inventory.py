"""知识库"源清单"与孤儿块判定（级联删除的纯函数层）。

要解决什么
----------
写入侧是幂等的（`chunk_id` / `chunk_fingerprint` 决定 upsert 主键，重复灌库不产生
副本），但**没有任何反向路径**：源文件被删掉或改写之后，向量库里那些块会**永久
驻留**。后果分两层：

  - **答案层**：用户问到已经下线的内容，检索照样命中、模型照着答；
  - **合规层**：对外声明"已删除"的数据仍然可以被搜出来。

两类孤儿，来源不同：

  1. **missing** —— 源文件在语料目录里已经不存在了（被删 / 被移走 / 改名）；
  2. **stale**   —— 源文件还在，但**内容变了**。改写文件会让 `chunk_id`
     （由 source+section+text 的 sha1 决定）全部变新 ⇒ 重新灌库只**新增**，
     旧块一条不删。于是同一个文件的新旧两版块并存，检索命中哪一版看运气。

为什么要在块上记 `source_path` / `content_hash`
---------------------------------------------
只靠 `source`（文件名 basename）判不了这两件事：
  - basename 无法区分 `a/x.md` 与 `b/x.md`，移文件会被误判成"没变"；
  - 没有内容指纹就无法区分"文件没变"与"文件改了但正好块数一样"。

⚠️ **两个新字段都不参与 `chunk_id`**（anchor 只用 source/section/text，
见 `knowledge_chunk` 的 `_build_*_chunk`）—— 这一点是硬要求：若把它们并进 id，
灌一次库就等于**全库重建**（id 全变）+ 引用失效。守卫见
`tests/test_kb_prune.py::test_source_path_does_not_change_chunk_id`。

判定的保守方向
-------------
"删"是不可逆的，所以本模块的每个不确定分支都**倾向不删**：

  - 老数据（本次改造之前灌的）没有 `source_path` / `content_hash` ⇒ 按 basename
    匹配；匹配上就当作 OK，**绝不因为"看不出新旧"而删**；
  - 重名文件（同名不同目录）无法唯一定位 ⇒ 只要 basename 还在就当作 OK；
  - 语料目录读不到任何文件 ≠ 语料为空 ⇒ 那是**挂载坏了**（见 `kb_prune` 脚本里
    的拒绝执行闸门），不能把全库判成孤儿。
"""

from __future__ import annotations

import glob
import hashlib
import os

#: 内容指纹取 sha256 的**前若干位十六进制**。
#: 只用于"和上次记的值比是否相等"，不用于安全用途，64 bit 的碰撞概率可忽略，
#: 而短串在日志/排查输出里可读性好得多。
HASH_CHARS = 16

#: 读文件算指纹时的块大小。
_CHUNK_BYTES = 1 << 20


def file_digest(path, chars=HASH_CHARS) -> str:
    """算文件内容的指纹（sha256 前 chars 位十六进制）。

    path: 文件路径。
    chars: 保留的十六进制位数（默认 16）。

    读不出来（权限/文件消失/是目录）时返回空串 —— 调用方把空串理解为
    "未知"，并按保守方向处理（不删），而不是当作"内容变了"。
    """
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for blk in iter(lambda: fh.read(_CHUNK_BYTES), b""):
                h.update(blk)
        return h.hexdigest()[:chars]
    except (OSError, ValueError):
        return ""


def rel_posix(path, docs_dir) -> str:
    """把绝对/相对路径转成"相对语料目录的 POSIX 路径"。

    path: 文件路径。
    docs_dir: 语料根目录。

    统一用 `/` 分隔：Windows 上跑出来的清单要能和 Linux 容器里灌库时写进
    `source_path` 的值直接比较，否则同一条规则在两个平台上判定结果不同。
    """
    rel = os.path.relpath(path, docs_dir) if docs_dir else path
    return rel.replace(os.sep, "/")


def source_key(chunk) -> str:
    """块 dict → 源标识：**优先 `source_path`，回落 `source`（basename）**。

    chunk: 块 dict（或任何支持 .get 的映射）。

    回落是给"本次改造之前灌的老数据"用的 —— 它们只有 basename。
    回落值是 basename，因此调用方比对时**也必须同时拿 basename 集合去匹配**，
    否则老数据会被整体判成缺失（那是一次全库误删）。
    """
    sp = str(chunk.get("source_path") or "").strip()
    if sp:
        return sp.replace("\\", "/")
    return str(chunk.get("source") or "").strip()


def stamp_source(chunks, path, docs_dir):
    """给"一份文件产出的块"打上 `source_path` 与 `content_hash`（就地改写）。

    chunks: 该文件解析出的块列表（dict 形态，即 `load_chunks` 里的产物）。
    path: 该文件的路径。
    docs_dir: 语料根目录。

    返回同一个列表（便于链式调用），**不是副本**。

    为什么在**每份文件解析后立刻**打标，而不是全部解析完再按 basename 回填：
    后者在"同名不同目录"的语料上只能取其一，会把另一份的路径写错 —— 而
    `source_path` 写错的后果是"清理时删错文件对应的块"。

    空列表直接返回；算不出指纹时只写 `source_path`，**不写空 `content_hash`**
    （空值表示"未知"，与"内容为空"要区分开）。
    """
    if not chunks:
        return chunks
    rel = rel_posix(path, docs_dir)
    digest = file_digest(path)
    for c in chunks:
        c["source_path"] = rel
        if digest:
            c["content_hash"] = digest
    return chunks


def corpus_index(docs_dir, extensions=None) -> dict:
    """扫描语料目录，返回判定所需的"源清单"。

    docs_dir: 语料根目录。空值 / 不存在 / 不是目录 ⇒ 返回空清单（`count` 为 0）。
    extensions: 受支持的扩展名集合（**小写且带前导点**，与
        `loaders.supported_extensions()` 同口径）。None = 取它。传空集合表示
        "不按扩展名过滤"（用于测试与自造语料）。

    返回::

        {
          "count": int,             # 扫到的受支持文件数
          "files": {rel: digest},   # 相对路径(POSIX) → 内容指纹
          "bases": {basename: rel|None},   # 文件名 → 唯一相对路径；重名时为 None
        }

    遍历方式刻意与 `retrieval.load_chunks` 一致（`glob("**/*")`）—— 它**不匹配
    以点开头的目录**，所以 `data/.parse_cache` 这类隐藏目录天然不入清单，
    两个函数对"语料里有哪些文件"的判断永远同口径。
    """
    out = {"count": 0, "files": {}, "bases": {}}
    if not docs_dir or not os.path.isdir(docs_dir):
        return out

    if extensions is None:
        from .loaders import supported_extensions

        extensions = supported_extensions()

    files = sorted(glob.glob(os.path.join(docs_dir, "**", "*"), recursive=True))
    for f in files:
        if not os.path.isfile(f):
            continue
        ext = os.path.splitext(f.lower())[1]
        if extensions and ext not in extensions:
            continue
        rel = rel_posix(f, docs_dir)
        out["files"][rel] = file_digest(f)
        base = os.path.basename(f)
        if base in out["bases"]:
            out["bases"][base] = None      # 重名：无法唯一定位，判定时按"存在"处理
        else:
            out["bases"][base] = rel
    out["count"] = len(out["files"])
    return out


def classify(key, digest, index) -> str:
    """判定单个源标识的状态：`ok` / `missing` / `stale` / `unknown`。

    key: 库里的源标识（`source_key` 的产物）。
    digest: 库里记的内容指纹（空串 = 未知，老数据）。
    index: `corpus_index()` 的返回值。

    `unknown` 是**刻意的第四种状态**：看得见这个源、但判不了新旧（没有指纹）。
    它既不进 missing 也不进 stale，因此永远不会被删 —— 见模块头的"保守方向"。
    """
    files = index["files"]
    if key in files:
        if not digest:
            return "unknown"
        return "ok" if digest == files[key] else "stale"
    if key in index["bases"]:
        rel = index["bases"][key]
        if rel is None:
            return "ok"            # 重名文件：只要还有同名文件就不敢删
        if not digest:
            return "unknown"
        return "ok" if digest == files[rel] else "stale"
    return "missing"


def plan_orphans(rows, index) -> dict:
    """把库里所有源标识分类，给出"该删哪些"。

    rows: 可迭代，每项 `(tenant, key, digest)` —— `Store.list_rows()` 的产物。
        同一个 key 出现多次（多个块同属一份文件）会被去重；指纹不一致时
        **只要有一条对得上就算 ok**（同一份文件在不同时间灌入的块理应同指纹，
        真的不一致说明这批数据本身混乱，此时按"还有正确版本"处理更安全）。
    index: `corpus_index()` 的返回值。

    返回::

        {
          "ok":      [(tenant, key), ...],
          "missing": [(tenant, key), ...],   # 源文件已不存在 → 默认删
          "stale":   [(tenant, key), ...],   # 源文件内容变了 → 需显式开启才删
          "unknown": [(tenant, key), ...],   # 判不了新旧 → 永不删
        }

    列表按 key 排序，保证同一次输入给出同一份输出（dry-run 可复现）。
    """
    verdict: dict[tuple[str, str], str] = {}
    for tenant, key, digest in rows:
        t = str(tenant or "")
        k = str(key or "").strip()
        if not k:
            continue          # 脏行（连 source 都是空的）：没法定位，交给人工
        state = classify(k, str(digest or ""), index)
        cur = verdict.get((t, k))
        # ok > unknown > stale > missing：只要有一份证据支持"还活着"就不判孤儿
        order = {"ok": 3, "unknown": 2, "stale": 1, "missing": 0}
        if cur is None or order[state] > order[cur]:
            verdict[(t, k)] = state

    out = {s: [] for s in ("ok", "missing", "stale", "unknown")}
    for (t, k), state in verdict.items():
        out[state].append((t, k))
    for v in out.values():
        v.sort()
    return out
