"""生成侧质量指标：把「答案准不准」拆成可自动判的几件事。

要解决什么
----------
`eval_retrieval.py` 量的是「找对文档了吗」，`eval_answer.py` 量的是「喂给模型的
材料够不够」。两者都停在**模型之前** —— 而「agent 生成的结果是否准确」这件事，
此前一行度量都没有：`citation_check` 每轮都算出结论、评审节点每轮都打分，
但两者都只进 state / 接口，**从不聚合**，所以没有任何数字能回答「这周答案是不是变差了」。

为什么不直接上 LLM-as-judge
--------------------------
忠实度评测（RAGAS 那类）要一次答案配一次 judge 调用：**有噪声、要密钥、不可复现**。
拿它当 CI 门禁会引入「随机红」，而随机红的下场一定是被人关掉
（与 `eval_answer.py` 开头同一段论证）。所以这里先做**确定性**的那一半 ——
它不调任何模型，而且它抓到的是**最确凿的一类错**：编造事实。

三类确定性判据
--------------
1. **证据片段（evidence span）在不在资料里**
   答案里的**标识符**（配置项名、接口路径、反引号内容）与**数字**，是「不该被
   合理改写掉」的东西。模型可以总结、可以换说法，但把 `AUTH_SESSION_SECRET`
   说成别的名字、把阈值 0.87 说成 0.55，那就是编造。
   这是本模块的主力判据，也是唯一能硬判的一类（标识符）。
2. **答案有多少内容能在资料里找到出处**（`grounded_ratio`）
   字符 n-gram 覆盖率。模型会总结改写，所以正常答案这个值本来就不高 ——
   它**不做硬门禁**，只做趋势监控（掉了说明答案越来越脱离资料）。
3. **该拒答时是否拒答**（`refusal_detected`）
   资料里根本没有答案时，正确回答是「资料里没提到」，而不是编一个。
   只有评测集里放了负样本（`answerable: false`）才测得出。

⚠️ 可信度分级：为什么数字不硬判
------------------------------
「15 分钟」与资料里的「900 秒」是同一件事，但字符串对不上 —— 数字的
"对不上"有太多合理来源（换算、口径、四舍五入、单位不同）。所以：

    - **标识符**（`APP_WORKERS`、`/v1/chat`）→ 只可能对或错，**硬判**；
    - **数字** → 可能只是换算或口径不同，**只记指标、不硬判**。

把这条写进代码而不是留给使用者判断，是因为「数字对不上就报错」的指标
上线第一天就会被真实数据打成误报机器，然后被人关掉 —— 那时连标识符那一半
也一起没了。

依赖方向
--------
只 import 同包的 `citation_check`（它自己不 import 任何同包模块，故无环）。
不 import app、不碰网络、不读配置：指标要能被单测直接喂合成样本。
"""

from __future__ import annotations

import re

from . import citation_check

#: 反引号内容：Markdown 里引用配置项/命令/字段名的标准写法（`` `APP_WORKERS` ``）。
#: 上限 60 字符：再长的一般是整句引用，作为"证据片段"太模糊，容易变成误报源。
_BACKTICK_RE = re.compile(r"`([^`\n]{2,60})`")

#: 接口路径：`/v1/chat`、`/health/ready`。前缀用 `(?<![\w/])` 排掉 `a/b` 这类
#: 正常文本里的斜杠（否则"可选/必填"会被当成路径）。
_PATH_RE = re.compile(r"(?<![\w/])/[A-Za-z][\w\-/]{2,60}")

#: 配置项/常量名：至少含一个下划线的全大写串（`APP_WORKERS`、`AUTH_SESSION_SECRET`）。
#: **刻意要求下划线** —— 否则 `API`、`RAG`、`SLO` 这类通用词全会进来，
#: 它们在资料里未必逐字出现，会制造一大片稳定误报。
_CONST_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")

#: 数字：`0.55`、`99.5%`、`900`、`604800`。只用于**统计**，不参与硬判（见文件头）。
_NUM_RE = re.compile(r"\b\d+(?:\.\d+)?%?")

#: 拒答措辞。**刻意保守** —— 只认"明确指出资料里没有"的明确说法，
#: 不认泛泛的"没有"（"没有配置就会走默认路径"是陈述，不是拒答）。
_REFUSAL_RE = re.compile(
    r"资料(中|里)?(未|没有)(提及|提到|说明|包含|找到)"
    r"|未在资料(中|里)?(找到|提及|提到)"
    r"|(资料|上下文)(中|里)?(并)?(未|没有)(提及|提到|包含|给出)"
    r"|无法(从|根据)(上述)?(资料|上下文|文档)(中)?(得|查|找到)"
    r"|(上述)?(资料|上下文|文档)(中|里)?(未|没有)(相关)?(信息|内容)"
)

#: 判定"被支撑"时的最小片段长度。太短的片段（如 `CI`）匹配与不匹配都不说明问题。
MIN_SPAN = 3

#: **拒答时静音**的警告。它们的共同含义都是"答案没有依据资料"，而拒答的定义
#: 恰恰是"资料里没有，所以无从依据"。不排除会让**每一个正确的拒答**都挂上警告，
#: 然后这一列被使用者无视 —— 指标最怕的不是漏报，是噪声让人不再看它。
#: 注意只静音"覆盖率类"，**不动确凿错误**（编造引用编号在拒答里同样是错的）。
_REFUSAL_MUTES = frozenset({"low_grounded", "low_overlap", "no_citations"})

#: `grounded_ratio` 的参照阈值 —— **低于它就说明答案整体脱离资料**。
#: 与 `citation_check.DEFAULT_MIN_OVERLAP` 同源（都是 0.05）而**不共用常量**：
#: 那个是"单条引用 vs 答案"的判据，这个默认要求答案与**整段上下文**的重叠，
#: 口径不同（分母是答案、被比对象是全部命中），将来也可能各自调整。共用常量会让
#: 调其中一个时静默改掉另一个的语义。
DEFAULT_MIN_GROUNDED = 0.05


class _SpanSet:
    """已占用的字符区间，用来防同一段文本被多次抽取成不同 kind 的片段。

    不做去重叠的后果：`` `APP_WORKERS` `` 会同时被反引号规则和常量规则各抓一次，
    于是"1 个未支撑"被数成 2 个 —— 幻觉率凭空翻倍，而明细看起来完全正常。
    """

    __slots__ = ("_ranges",)

    def __init__(self):
        self._ranges: list = []

    def free(self, start, end) -> bool:
        return all(end <= a or start >= b for a, b in self._ranges)

    def take(self, start, end) -> None:
        self._ranges.append((start, end))


def evidence_spans(answer):
    """抽取「应该被资料支撑」的片段。返回 `[(kind, text), ...]`，去重保序。

    kind: `code`（反引号内容）| `path`（接口路径）| `const`（配置项名）| `number`。

    抽取**按可信度从高到低**进行（code → path → const → number），
    已被高可信规则占用的区间不再被低可信规则重复抓取。
    """
    if not isinstance(answer, str) or not answer:
        return []

    out, taken = [], _SpanSet()

    def _add(kind, m, group=0, transform=None):
        s, e = m.start(group), m.end(group)
        text = m.group(group).strip()
        if transform is not None:
            text = transform(text)
        if len(text) < MIN_SPAN or not taken.free(s, e):
            return
        taken.take(s, e)
        out.append((kind, text))

    def _key_of(text):
        """`` `KEY=VALUE` `` 只取 `KEY`。

        指标抓的是"编造**名字**"，而给出一个具体取值（`TRUST_PROXY_HEADERS=1`）
        不构成编造 —— 名字在资料里就够了，取值本身属于合理建议。
        不这么切的话，模型最常用的写法之一（把配置项和取值一起写）会被整条判成
        幻觉，而这类**稳定误报**会让指标失去可信度、进而被关掉。
        """
        return text.split("=", 1)[0].strip()

    for m in _BACKTICK_RE.finditer(answer):
        _add("code", m, 1, _key_of)
    for m in _PATH_RE.finditer(answer):
        _add("path", m)
    for m in _CONST_RE.finditer(answer):
        _add("const", m)
    for m in _NUM_RE.finditer(answer):
        _add("number", m)
    return out


def _norm(text: str) -> str:
    """归一化后比较：去空白、转小写。

    去空白是因为资料里的 `AUTH_SESSION_SECRET` 可能被折行；转小写是因为
    `app.serve` 与 `App.Serve` 在"有没有这个标识符"这个问题上是同一个答案。
    """
    return "".join(ch for ch in (text or "") if not ch.isspace()).lower()


def _supported(span: str, haystack_norm: str) -> bool:
    """片段是否被支撑 —— 归一化子串包含。

    只判**包含**，不判相似度：`AUTH_SESSION_SECRET` 与 `AUTH_SESSION_SECRETS`
    是两回事（后者是编造的名字），模糊匹配会把这类确凿错误放过。
    """
    return _norm(span) in haystack_norm


def unsupported_evidence(answer, context, question="") -> dict:
    """答案里出现、但**资料与问题都没有**的证据片段。

    question 也纳入"允许来源"：用户在问题里提到的词，模型复述它不算幻觉。
    漏掉这一条会让"问什么答什么"被稳定误判 —— 而这类误判会让人不再相信指标。

    返回 `{"total", "identifier_total", "unsupported", "identifiers", "numbers", "checked"}`：
      - `identifiers`：code / path / const 三类，**可硬判**；
      - `numbers`：只记录，调用方不应据此判失败（见文件头）；
      - `checked=False`：资料为空 → 一律不判（没有可比对的依据）。
        ⚠️ 此时 `total` / `identifier_total` **照常统计**（只是不判对错）——
        它们是"答案里有多少可证据化片段"这个**事实**，与有没有资料无关。
        让它们归零会让"无上下文的答案"看起来像"没有可证据化内容"，
        两份不同口径的计数混在一起，下游算比率就从对变成了错。
    """
    spans = evidence_spans(answer)
    ident_total = len([1 for k, _ in spans if k != "number"])
    out = {"total": len(spans), "identifier_total": ident_total,
           "unsupported": [], "identifiers": [], "numbers": [], "checked": False}
    if not _norm(context):
        # 无上下文（检索空/降级）⇒ 没有任何依据可查。此时把全部片段报成"编造"
        # 是纯误报：模型本来就是在没有资料的情况下作答，该判的是"有没有拒答"。
        return out
    out["checked"] = True
    hay = _norm(context) + "\x00" + _norm(question)
    for kind, text in spans:
        out["total"] += 1
        if _supported(text, hay):
            continue
        item = {"kind": kind, "text": text}
        out["unsupported"].append(item)
        (out["numbers"] if kind == "number" else out["identifiers"]).append(item)
    return out


def grounded_ratio(answer, context) -> float | None:
    """答案有多少比例能在**资料**里找到出处（0.0~1.0）。

    返回 None 表示**不判**（答案或资料为空）—— 与 `citation_check.best_overlap`
    的第三个返回值同一套理由：把"算出来是 0"与"根本没法算"混成一个 0.0，
    会让"资料为空、模型如实说不知道"被稳定判成"完全没有依据"。
    """
    if not isinstance(answer, str) or not answer.strip():
        return None
    if not isinstance(context, str) or not context.strip():
        return None
    return citation_check.overlap_ratio(answer, context)


def refusal_detected(answer) -> bool:
    """答案是否明确表达了「资料里没有这个信息」。"""
    return bool(_REFUSAL_RE.search(answer if isinstance(answer, str) else ""))


def score_answer(answer, context, citations=None, *, question="",
                 docs_dir=None, exists=None, min_grounded=None) -> dict:
    """聚合为一条答案质量结论（**不抛异常**，与 `citation_check.verify` 同口径）。

    参数：
      answer     模型产出（final draft）。
      context    **线上同款**拼装的资料（`"\\n\\n".join(context_block(c) ...)`），
                 不是原始 chunk 列表 —— 口径与线上不一致的评测最隐蔽（测了一个不存在的链路）。
      citations  `build_citations` 的产物。
      question   用户问题原文，用于排除"复述问题里的词"。
      docs_dir   语料根目录，透传给 `citation_check.verify` 判"源文件还在不在"。
      min_grounded  `grounded_ratio` 的告警阈值，None 取 `DEFAULT_MIN_GROUNDED`。

    返回键：
      - `ok`：True / False / **None（无从判断，别上报成失败）**
      - `reasons`：导致 `ok=False` 的确凿理由（目前只有编造标识符 / 编造引用编号）
      - `warnings`：不足以失败的疑点（低覆盖、缺源文件、无引用）
      - 其余为可入库的数值指标

    `ok` 的派生规则（**单一真相**，与 `citation_check._finish` 同思路）：
      无答案 → None；有确凿错误 → False；有标识符但无资料可核 → None；否则 True。
    "无资料可核"也算 None 而不是 True：把"没检查"记成"检查通过"，会让聚合的
    `ok_rate` 朝"看起来更好"的方向偏 —— 指标最不该有的偏差。
    """
    ans = answer if isinstance(answer, str) else ""
    ctx = context if isinstance(context, str) else ""
    ev = unsupported_evidence(ans, ctx, question)
    cited = citation_check.verify(ans, citations, docs_dir=docs_dir, exists=exists)
    gr = grounded_ratio(ans, ctx)
    refused = refusal_detected(ans)
    lo = DEFAULT_MIN_GROUNDED if min_grounded is None else float(min_grounded)

    hard = []
    warns = []

    # ---- 确凿错误一：编造标识符 ----
    # 只取 identifiers（code/path/const）。数字不进这里，原因见文件头。
    if ev["identifiers"]:
        hard.append("unsupported_identifier")

    # ---- 确凿错误二：编造引用编号（citation_check 已经判过，这里只做转述） ----
    if cited.get("hard_errors"):
        hard.append("bad_citation")

    # ---- 疑点（不判失败） ----
    # ⚠️ 拒答时**不报**覆盖率类警告：拒答的答案本来就不复述资料
    # （"资料里没提到"这句话里没有一个字来自资料），覆盖率天然是 0。
    # 不排除就会让**每一个正确的拒答**都带上警告 —— 这种稳定噪声
    # 会让人不再看警告，等于把这一列废掉。列表见 `_REFUSAL_MUTES`。
    if gr is not None and gr < lo and not refused:
        warns.append("low_grounded")
    for w in cited.get("warnings") or ():
        if refused and w in _REFUSAL_MUTES:
            continue
        if w not in warns:
            warns.append(w)

    if not ans.strip():
        ok = None
    elif hard:
        ok = False
    elif ev["identifier_total"] and not ev["checked"]:
        # 有标识符本该核对，但**没有资料可核**（检索为空/降级）⇒ 无从判断。
        # 这里返回 True 会把"没检查"记成"检查通过"，而 `ok_rate` 是聚合指标，
        # 一旦混进这种条目，数字会朝"看起来更好"的方向偏 —— 指标最不该有的偏差。
        ok = None
    else:
        ok = True

    ident_total = ev["identifier_total"]
    return {
        "ok": ok,
        "reasons": hard,
        "warnings": warns,
        "answer_chars": len(ans.strip()),
        "grounded_ratio": None if gr is None else round(gr, 4),
        "evidence_total": ev["total"],
        "evidence_checked": ev["checked"],
        "unsupported": ev["unsupported"],
        "unsupported_identifiers": ev["identifiers"],
        "unsupported_numbers": ev["numbers"],
        # 幻觉率的分母是**标识符总数**，不是全部片段：数字不参与硬判，
        # 放进分母会让一条"换算口径不同"的答案把比率抬虚。
        "identifier_total": ident_total,
        "identifier_hallucination_rate": (
            round(len(ev["identifiers"]) / ident_total, 4) if ident_total else 0.0
        ),
        "refused": refused,
        "citation_status": cited.get("status"),
        "citation_detail": cited.get("detail", ""),
    }
