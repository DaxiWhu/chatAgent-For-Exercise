"""出站调用超时守卫：每一个"会发网络请求"的客户端构造点，都必须显式给超时。

为什么需要这一条（这是本文件存在的理由）
------------------------------------
2026-10-05 的审计里，**同一个子系统内的三个同类调用点，只有一个忘了传超时**：

  - `src/rag/retrieval.py` 的三处 `OpenAIEmbeddings(...)` 全都没传 `timeout`；
  - 而同一条链路上的 `QdrantStore` / rerank 的 `httpx.post` / 通知的 `httpx.post`
    都传了。

这种"漏一个"的失效方式与"全都没写"完全不同：

  - 全都没写 ⇒ 一压测就全线卡死，很容易定位到"少了个超时"；
  - 只漏一个 ⇒ **大部分请求正常**，只有走到那条支路的请求会被挂死。而它恰好
    在**请求热路径**上（每次检索都要 `embed_query`），后果不是"这个请求慢"，
    而是工作线程被占死、并发闸门名额耗尽 —— 现场是"上线后实例陆续卡死、
    重启即恢复"，几乎没人会往"某个调用了忘了传 timeout"上想。

`openai` SDK 的默认超时是 **600 秒**，再叠加 `max_retries` ⇒ 单个忘传超时的
调用最坏能挂住**约 40 分钟**。而 `REQUEST_TIMEOUT_SECONDS` 默认是 0（不设总时限），
所以没有任何一层会兜住它。

所以判据不是"能跑就行"，而是"**每个出站点都必须显式声明它的上界**" —— 显式
优于隐式：httpx 虽有 5 秒默认值，但把超时写在调用点上，改的人一眼能看到，
也强迫新增调用点时做一次"这个上界合理吗"的判断。

为什么用 AST 而不是正则
--------------------
这些调用点普遍是**多行参数列表**（例如 `OpenAIEmbeddings(` 后跟 6 个关键字，
`timeout` 在最后一行）。用正则找"括号内是否含 timeout"要么写得很脆（跨行、
嵌套括号），要么只能退化成"整个文件里出现过 timeout 字样" —— 后者在
`retrieval.py` 这种到处都有 `TIMEOUT` 常量的文件里**永不失败**，是典型的假绿。
AST 能精确拿到"这一次调用"的关键字集合，且 `**kwargs` 展开的情况也能单独处理。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: 需要检查的客户端构造名（`Call.func` 是**裸名字**的那些）。
CLIENT_CALLS = {
    "OpenAI",            # openai 官方 SDK（同步）
    "AsyncOpenAI",       # openai 官方 SDK（异步）
    "ChatOpenAI",        # langchain_openai 的对话模型封装
    "OpenAIEmbeddings",  # langchain_openai 的向量化封装 ← 本次漏传超时的就是它
    "QdrantClient",      # qdrant 官方客户端
    "AsyncQdrantClient",
}

#: `httpx.<attr>` 形式的模块级便捷函数（每次调用新建连接，不复用连接池）。
HTTPX_ATTRS = {"post", "get", "put", "patch", "delete", "request", "stream", "send"}

#: 出站点所在的文件（相对项目根）。
#:
#: 刻意用**显式清单**而不是全仓 glob：全仓扫描会把 `examples/`、`notebooks/`、
#: `scripts/eval_*.py` 里那些"跑一次就完、故意不设超时"的调用也算进来。一旦
#: 出现噪音，人的第一反应是往清单里加豁免 —— 而这个守卫的全部价值就在于
#: "清单里每一条都不能漏"，加了豁免就等于失效。所以只列**服务进程真正会走到**
#: 的出站点，新增出站点时必须同步加进来。
SCANNED_FILES = [
    "app/main.py",
    "app/review_queue.py",
    "src/agents/model.py",
    "src/rag/retrieval.py",
    "src/rag/qdrant_store.py",
    "src/rag/contextualizer.py",
    "src/rag/image_describer.py",
]


def _call_label(node: ast.Call) -> str | None:
    """把一次调用归类成"需要超时的出站点"，否则返回 None。

    只认两类：裸名字的客户端构造（`OpenAI(...)`），以及 `httpx.` 前缀的模块级
    便捷函数（`httpx.post(...)`）。**刻意不认** `XXXStore(...)` 这类自有封装 ——
    它们的超时由封装内部负责，在调用点再要求一遍会变成噪音。
    """
    func = node.func
    if isinstance(func, ast.Name) and func.id in CLIENT_CALLS:
        return func.id
    if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            and func.value.id == "httpx" and func.attr in HTTPX_ATTRS):
        return f"httpx.{func.attr}"
    return None


def _scan(path: Path) -> list[tuple[int, str, ast.Call, str]]:
    """返回 [(行号, 调用名, AST 节点, 所在函数源码)]，每个出站点一条。

    所在函数源码是给 `**kwargs` 那种写法用的（超时写在"上面那个 dict"里，
    静态看不到调用上的关键字），见 `_has_timeout`。
    """
    src = path.read_text(encoding="utf-8")
    hits: list[tuple[int, str, ast.Call, str]] = []

    class _V(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope = src  # 模块顶层：作用域就是整个文件

        def _enter_func(self, node):
            prev = self.scope
            sel = ast.get_source_segment(src, node)
            if sel:
                self.scope = sel
            self.generic_visit(node)
            self.scope = prev

        visit_FunctionDef = _enter_func
        visit_AsyncFunctionDef = _enter_func

        def visit_Call(self, node: ast.Call) -> None:
            label = _call_label(node)
            if label is not None:
                hits.append((node.lineno, label, node, self.scope))
            self.generic_visit(node)

    _V().visit(ast.parse(src, filename=str(path)))
    return hits


def _has_timeout(node: ast.Call, scope_src: str) -> bool:
    """这次调用是否显式声明了超时。"""
    if any(kw.arg == "timeout" for kw in node.keywords):
        return True
    # `**kwargs` 展开：超时在别处（通常是一个 dict 字面量）。退一步要求
    # **同一函数体内**出现 `"timeout"` 这个键 —— 比"整个文件里出现过"紧得多，
    # 也不会被模块级同名常量骗过（常量名是 `TIMEOUT`，不是被引号包起来的键）。
    if any(kw.arg is None for kw in node.keywords):
        return ('"timeout"' in scope_src) or ("'timeout'" in scope_src)
    return False


@pytest.mark.parametrize("rel", SCANNED_FILES)
def test_every_outbound_client_call_sets_a_timeout(rel):
    """清单里每个文件中的每个出站点，都必须显式给超时。

    失败信息里列出"文件 + 调用名 + 行号"，因为漏一个的定位成本恰恰来自
    "它看起来只是众多调用中的一个" —— 不给行号，人会在几百行里翻找。
    """
    path = ROOT / rel
    assert path.is_file(), (
        f"扫描清单里的文件不存在：{rel}。文件改过名？"
        "改名后必须同步 `SCANNED_FILES`，否则这个文件的出站点会**静默不被检查**。"
    )

    offenders = [f"{label}（第 {lineno} 行）"
                 for lineno, label, node, scope in _scan(path)
                 if not _has_timeout(node, scope)]

    assert not offenders, (
        f"{rel} 里有出站点没显式设超时：{', '.join(offenders)}。\n"
        "后果：`openai` SDK 默认 600 秒 × 重试 ⇒ 单个调用最坏挂住约 40 分钟；"
        "而 `REQUEST_TIMEOUT_SECONDS` 默认 0（不设总时限），没有任何一层兜底。"
        "若它落在请求热路径上，表现是「实例陆续卡死、重启即恢复」，极难定位。"
    )


def test_scanner_actually_finds_the_known_call_sites():
    """反锁：扫描面不许空跑。

    这条防的是"守卫本身失效"—— 文件改名、调用被包进新函数、清单写错路径，
    都会让 `_scan` 返回空列表，于是上面那条断言**永远通过**（零个出站点 ⇒
    零个违规）。这正是本项目里最需要提防的假绿形态：守卫还在、名字还在，
    但它已经不检查任何东西了。

    下限取 10 = 当前实际出站点数（见下方注释）。**只增不减**：新增出站点时
    这里不必改（`>=`），但若某次重构把出站点挪出了扫描面，这条会立刻变红。
    """
    # 当前实际出站点：
    #   app/main.py              ChatOpenAI ×1
    #   app/review_queue.py      httpx.post ×1
    #   src/agents/model.py      AsyncOpenAI ×1
    #   src/rag/retrieval.py     OpenAIEmbeddings ×1 + httpx.post ×1 + ChatOpenAI ×1
    #   src/rag/qdrant_store.py  QdrantClient ×1
    #   src/rag/contextualizer.py OpenAI ×1
    #   src/rag/image_describer.py OpenAI ×1
    #
    # 2026-11-01：下限从 10 降到 9，**不是**扫描面缩小 —— 是 retrieval.py 里三处
    # `OpenAIEmbeddings(...)` 收成了 `_new_embedder()` 一处（三个稠密后端共用，
    # 参数漏一处就会有一个后端悄悄返回错向量）。出站点本身确实少了 2 个，
    # 而"每个出站点都显式带 timeout"这条约束反而更稳了：现在只有一处要守。
    total = sum(len(_scan(ROOT / rel)) for rel in SCANNED_FILES)
    assert total >= 9, (
        f"扫描面只找到 {total} 个出站点，少于已知的 9 个 —— "
        "守卫已经空跑了（文件改名 / 调用被挪走 / 清单路径写错）。"
        "请先确认出站点还在，再更新这里。"
    )
