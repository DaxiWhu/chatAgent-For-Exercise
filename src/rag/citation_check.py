"""引用校验：把"答案带了引用"与"答案有依据"分开。

要解决什么
----------
`build_citations()` 只做**结构转换**：把检索命中拍成可回溯的引用列表。
它不校验任何东西 —— 于是接口层看到 `citations` 非空，只能说明"这一轮检索命中了
东西"，推不出"答案用了它"，更推不出"答案有依据"。而模型完全可以在命中 3 条之后
写出一段与三条都不沾边的话，还带着 3 条引用。

三层判据（都能自动判，都不需要人）
--------------------------------
1. **引用编号可追溯**：答案里若出现 `[3]` 这类编号，它必须落在本轮命中的范围内
   （1..len(citations)）。
   ⚠️ 当前 `answer_prompt` **没有要求**模型标编号 ⇒ 通常一条都抽不到，此时这一层
   标 `refs_checked=False`，**不判**。这是刻意的边界而不是缺陷：改 prompt 会改变
   模型行为（要 bump `PROMPT_VERSION` 并重跑评测），那应该是独立的一次演进，
   不该顺手塞进校验层。但**一旦模型标了编号，它就必须对** ——
   编造编号（`[7]` 而只有 3 条）是最硬的一种错。
2. **引用来源仍存在**：引用的 `source_path` 在语料目录下 stat 得到。
   ⚠️ 三个"不判"的闸门，缺任何一个都会变成纯误报：
     - 语料目录不存在（容器里根本没挂语料）⇒ 全部 stat 失败 ⇒ 看起来"所有引用都坏了"；
     - 引用没有 `source_path`（本次改造之前灌的库只有 basename）⇒ 同名不同目录
       无法唯一定位，**跳过**而不是猜；
     - 路径里带 `..` 或以 `/` 开头 ⇒ 跳过（不拿它去 stat 语料目录之外的地方）。
   ⚠️ 只 stat **被引用的那几个文件**，绝不扫目录 —— 这层要跑在请求路径上。
3. **答案与引用有最低重叠**：答案的字符 n-gram 有多大比例能在引用文本里找到。
   这是**启发式**，所以只在"低到不可能有依据"时才报，并且**永不**触发拦截。

`block` 只在第 1 层生效
----------------------
只有"模型编造了引用编号"是**确凿**的错误。第 2 层是数据状态（源文件被删了，
该由 `kb_prune` 的 `missing` 判定去清理孤儿），第 3 层是启发式（可能只是改写幅度大）。
拿启发式判据去拦用户的答案，误伤成本远大于收益；而编造引用是**可以用代码证明**的错。

依赖方向
--------
纯标准库（不 import app、不 import 同包其它模块）：它要在请求路径上跑，
必须快、无副作用、无循环导入风险。语料目录由调用方传入。
"""

from __future__ import annotations

import os
import re

#: 引用编号写法：`[3]` / `【3】` / `［3］`。
#: 刻意**不认** `(3)`：正文里"第(3)条""如图(2)"太常见，认了就是稳定误报，
#: 而稳定误报的判据最后一定会被人关掉。
_REF_RE = re.compile(r"[\[【［]\s*(\d{1,3})\s*[\]】］]")

#: 字符 n-gram 的 n。4 对中英文都还算稳：中文能抓住词组，英文能抓住词形片段。
#: 调小（2）会让"我们""系统"这类高频片段把覆盖率抬虚；调大（6）对改写过于敏感。
NGRAM = 4

#: 默认最低重叠。**刻意很低**：模型会总结与改写，正常答案的覆盖率本来就不高，
#: 这一层要抓的是"整段凭空生成"，不是"改写幅度大"。
DEFAULT_MIN_OVERLAP = 0.05

#: 引用文本短于这个长度时不判重叠 —— 样本太小，比率没有意义
#: （引用只有 3 个字时，覆盖率非 0 即 1，两种都不说明问题）。
MIN_REFERENCE_CHARS = 40


def extract_refs(answer) -> list:
    """抽取答案里的引用编号（去重、保序）。

    answer: 答案文本。非字符串按空处理。
    """
    out, seen = [], set()
    for m in _REF_RE.finditer(answer if isinstance(answer, str) else ""):
        n = int(m.group(1))
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _grams(text, n=NGRAM) -> set:
    """字符 n-gram 集合（先去掉所有空白：换行与缩进不该影响覆盖率）。"""
    s = "".join(ch for ch in (text or "") if not ch.isspace())
    if len(s) < n:
        return set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def overlap_ratio(answer, text, n=NGRAM) -> float:
    """答案的 n-gram 有多大比例能在 `text` 里找到（0.0 ~ 1.0）。

    分母取**答案**的 gram 数，回答的是"答案里有多少内容是有出处的"。
    反过来（以引用为分母）回答的是"这段引用被用上了多少"，是另一个问题 ——
    而且那个口径下"答案只抄了引用的一句话"会被算成低分，恰好判反。
    """
    a, b = _grams(answer, n), _grams(text, n)
    if not a:
        return 0.0
    return len(a & b) / len(a)


def best_overlap(answer, citations, n=NGRAM):
    """返回 `(最高重叠率, 对应引用编号或 None, 是否可判)`。

    第三条为 False 表示"所有引用都太短" ⇒ **不要**据此下任何结论。
    分开返回是必要的：把"算出来是 0"与"根本没法算"混成一个 0.0，
    会让"只有一条超短引用"的请求被稳定判成"没有依据"。
    """
    best, best_n, usable = 0.0, None, False
    for c in citations or []:
        body = str(c.get("snippet") or "")
        if len(body.strip()) < MIN_REFERENCE_CHARS:
            continue
        usable = True
        r = overlap_ratio(answer, body, n)
        if best_n is None or r > best:
            best, best_n = r, int(c.get("n") or 0)
    return best, best_n, usable


def _unsafe_rel(rel: str) -> bool:
    """相对路径是否越出语料目录（绝对路径 / `..` 都算）。"""
    return rel.startswith("/") or rel == ".." or rel.startswith("../") or "/../" in rel


def missing_sources(citations, docs_dir, exists=None) -> dict:
    """检查引用的源文件是否还在语料目录下。

    citations: `build_citations` 的产物。
    docs_dir: 语料根目录。**空或不是目录 ⇒ 整体不判**（`checked=False`）——
        容器里语料没挂进去时，所有引用都会 stat 失败，照常判定就是
        "所有引用都坏了"这种纯误报（与 `kb_prune` 拒绝执行的闸门同一条理由：
        分不清"数据真没了"与"根本没看到数据"时，**不下结论**）。
    exists: 注入点（测试用），默认 `os.path.exists`。

    返回 `{"checked", "missing", "unverifiable", "skipped_unsafe"}`。
    """
    out = {"checked": False, "missing": [], "unverifiable": 0, "skipped_unsafe": 0}
    if not docs_dir or not os.path.isdir(docs_dir):
        return out
    out["checked"] = True
    probe = exists or os.path.exists
    for c in citations or []:
        rel = str(c.get("source_path") or "").strip().replace("\\", "/")
        if not rel:
            # 本次改造之前灌的库只有 basename（`source`），同名不同目录无法唯一定位
            out["unverifiable"] += 1
            continue
        if _unsafe_rel(rel):
            out["skipped_unsafe"] += 1
            continue
        if not probe(os.path.join(docs_dir, *rel.split("/"))):
            out["missing"].append(rel)
    return out


def verdict_ok(status):
    """状态 → `ok` 三态：True（通过）/ False（有问题）/ None（无从判断）。

    `weak` 归 False 而不是 None：它确实是"有疑点"，只是疑点不足以拦截。
    把"有疑点"和"无从判断"混起来，指标里就再也分不出"答得可疑"与"没答案"。
    """
    if status == "ok":
        return True
    if status in ("no_answer", "skipped"):
        return None
    return False


def _finish(v: dict) -> dict:
    """收尾：`ok` 与 `block_worthy` 一律**派生**出来，不在各分支里各写一遍。

    派生而不是手写，是为了消掉"两处真相"：将来有人往 `hard_errors` 里加了一类
    却忘了同步置 `block_worthy`，表现就是"报了错但没拦" ——
    而两处都"各自看起来对"，测试很难抓到。
    """
    v["block_worthy"] = bool(v["hard_errors"])
    v["ok"] = verdict_ok(v["status"])
    return v


def verify(answer, citations, *, docs_dir=None, min_overlap=None, exists=None) -> dict:
    """三层判据汇总，返回 verdict dict（**不抛异常**）。

    answer: 模型产出的答案文本。
    citations: `build_citations` 的产物（本轮命中的引用）。
    docs_dir: 语料根目录（第 2 层用）。None/不存在 ⇒ 该层标 `sources_checked=False`。
    min_overlap: 第 3 层阈值。None 取 `DEFAULT_MIN_OVERLAP`。
    exists: 第 2 层的 stat 注入点（测试用）。

    返回键（最重要的几个）：
      - `status`：`ok` / `weak` / `degraded` / `unsupported` / `no_citations` / `no_answer`
      - `ok`：True / False / None（None = 无从判断，**别上报成失败**）
      - `hard_errors`：**只有它能让 `block` 生效**（目前只有 `unknown_refs`）
      - `warnings`：`missing_sources` / `low_overlap` / `no_citations`
      - `block_worthy`：= `hard_errors` 非空
      - `detail`：一句人话（进日志 / 接口明细）
    """
    ans = answer if isinstance(answer, str) else ""
    cites = list(citations or [])
    min_ov = DEFAULT_MIN_OVERLAP if min_overlap is None else float(min_overlap)

    # `ok` 与 `block_worthy` 不在上面预置：它们由 `_finish` 从 `status` / `hard_errors`
    # 派生出来，避免出现"手写了一份、实际又按另一份判"的两处真相。
    v = {
        "status": "ok",
        "hard_errors": [],
        "warnings": [],
        "citations": len(cites),
        "refs": [],
        "refs_checked": False,
        "missing": [],
        "sources_checked": False,
        "best_overlap": None,
        "best_overlap_n": None,
        "overlap_usable": False,
        "detail": "",
    }

    if not ans.strip():
        v.update(status="no_answer", detail="没有答案内容，不校验引用")
        return _finish(v)

    if not cites:
        refs = extract_refs(ans)
        v["refs"] = refs
        v["refs_checked"] = bool(refs)
        if refs:
            v.update(status="unsupported")
            v["hard_errors"].append("unknown_refs")
            v["detail"] = (
                f"答案标了引用编号 {refs}，但本轮**一条检索命中都没有** —— 这是编造引用"
            )
        else:
            v.update(status="no_citations")
            v["warnings"].append("no_citations")
            v["detail"] = (
                "答案没有引用任何资料（可能是资料里确实没有、如实说了未提及，"
                "也可能是凭空作答 —— 人工抽查一次）"
            )
        return _finish(v)

    allowed = len(cites)
    refs = extract_refs(ans)
    v["refs"] = refs
    v["refs_checked"] = bool(refs)
    unknown = [n for n in refs if n < 1 or n > allowed]
    if unknown:
        v["hard_errors"].append("unknown_refs")

    src = missing_sources(cites, docs_dir, exists=exists)
    v["sources_checked"] = src["checked"]
    v["missing"] = src["missing"]
    v["sources_unverifiable"] = src["unverifiable"]
    if src["missing"]:
        v["warnings"].append("missing_sources")

    best, best_n, usable = best_overlap(ans, cites)
    v["best_overlap"] = round(best, 4)
    v["best_overlap_n"] = best_n
    v["overlap_usable"] = usable
    if usable and best < min_ov:
        v["warnings"].append("low_overlap")

    if unknown:
        v.update(status="unsupported")
        v["detail"] = (
            f"答案引用了不存在的编号 {unknown}（本轮命中只有 1..{allowed}）"
            " —— 应视为编造引用"
        )
    elif v["missing"]:
        v.update(status="degraded")
        v["detail"] = (
            f"引用指向的源文件已不在语料目录里：{v['missing']}"
            f"（这类孤儿由 `kb-prune` 的 missing 判定覆盖；引用目前不可回溯）"
        )
    elif "low_overlap" in v["warnings"]:
        v.update(status="weak")
        v["detail"] = (
            f"答案与引用文本的重叠率仅 {best:.1%}（阈值 {min_ov:.0%}）"
            " —— 可能只是改写幅度大，也可能没有依据，建议人工抽查"
        )
    else:
        v.update(status="ok")
        v["detail"] = f"引用校验通过（{allowed} 条引用，最高重叠 {best:.1%}）"
    return _finish(v)
