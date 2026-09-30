import os
import re
import sys
from pathlib import Path

import pytest
from unittest.mock import MagicMock
from langchain_core.messages import AIMessage

# 把项目根目录加入 sys.path，使 `from src.graph import ...` 可用
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ===========================================================================
# 测试环境隔离：开发机上的 .env 不得影响测试结论
# ===========================================================================
# 真实踩过的坑（2026-09-29）：往 `.env` 里补了一行 `SERVE_API_KEY=…`，全量立刻
# `35 failed` —— 全是 401。原因不是代码坏了，而是**测试套件读了开发机的 `.env`**：
# `.env` 是"本机跑服务"用的，它的值（密钥、配额、限流、超时、版本号…）会通过
# `load_dotenv()`（在 `app/main.py` 与 `src/rag/*` 的**导入期**调用）进入
# `os.environ`，而 `app/config.py` 的所有常量都在导入期就从环境里取好了。
# 于是"改本机配置"= "改测试前置条件"，出现三类恶性后果：
#   ① 本地红、CI 绿（CI 没有 `.env`），排查方向全错；
#   ② 有人为了让本地变绿而放宽断言（假绿）；
#   ③ 真正想测"未配置时的默认值"的用例被本机配置悄悄改掉前置条件。
#
# 修法：在**任何 app/src 模块被导入之前**（即本文件顶部），让"会改变服务请求行为"
# 的环境变量在本进程里**不存在**。测试进程由此等价于"裸环境 + 用例里显式
# monkeypatch"，与 CI 得到同一套结论。
#
# 为什么必须是"删除"而不是"置成空串"（都试过）
# -------------------------------------------
# `""` 对不同读者含义不同，于是两头都会出错：
#   - `_env_str` 把它读成"配置了一个空字符串" ⇒ `FastAPI(version="")` 直接断言失败
#     （实测：全量 6 个文件在收集期就 ERROR）；
#   - 裸 `os.getenv(name, "1")` 把它读成 falsy ⇒ `src/rag/parse_cache.py` 会认为
#     解析缓存被**关掉**（默认是开），而 `app/config.py` 那边 `_env_bool` 却读出
#     "默认 True"——同一个变量被两个读者理解成相反的值。
# 只有"键不存在"对**所有**读者都等于"未配置"。所以用 `pop`。
#
# 于是必须同时拦 `load_dotenv`（见下面的包装层）：`pop` 掉之后键是"不存在"的，
# 而 `load_dotenv()` 恰好只跳过"已存在"的键 ⇒ 会在**导入期**把 `.env` 的值原样
# 灌回来（这正是那个 401 的完整机制）。
#
# 为什么不干脆把 dotenv 整个停掉（试过，被否）
# -----------------------------------------
# 全量实测：完全不加载 `.env` 会额外红 8 个 docling/PDF 用例，报
# `转换异常：502 Bad Gateway` —— 因为 `.env` 里的 `DOCLING_ARTIFACTS_PATH` /
# `HF_ENDPOINT` 指向本机已下载的模型，去掉后 docling 转为**联网下载**（本机走代理
# 直接被 502 挡掉）。路径类变量属于"本机工具链环境"，不是"服务行为"，刻意保留
# （见下面的 `_KEEP_PREFIXES`）。

#: 读环境变量的两处"服务行为"位置。`src/rag/*` 也要算进来：2026-09-29 修好
#: `_ROOT`（见 `tests/test_project_root.py`）之后，这些模块在导入期真的会加载
#: `.env`，于是 `RAG_VECTOR_STORE` / `QDRANT_URL` / `EMBEDDING_*` 在测试进程里
#: 也开始"生效"。实测后果：本机 `.env` 写着 `RAG_VECTOR_STORE=qdrant` 时，
#: `test_eval_answer.py` 的门禁结论从"达标"变成"来源覆盖 0.795 < 0.87"——
#: 评测集的达标与否**取决于开发机配置**，这是同一类问题的第二次出现。
_ENV_SOURCE_GLOBS = ("app/config.py", "src/rag/*.py")

#: 前缀白名单：本机模型 / 工具链相关，**刻意不清**。
#: 它们是"这台机器上模型装在哪"的答案，不是服务行为；清掉之后 docling 会转去
#: **联网下载**模型（实机走代理被 502 挡掉，实测多红 8 个用例），而 OCR 开关的
#: 用例也会因为拿不到本地 OCR 模型而失败。判定标准是"清掉它会不会让测试去访问
#: 网络或依赖本机已下载的模型"，不是"名字里有没有 DOCLING"。
_KEEP_PREFIXES = ("DOCLING_", "VLM_", "RAG_VLM_", "HF_")


def behavior_env_names():
    """列出"会改变服务请求行为"的环境变量名（= 测试进程里必须不存在的那些）。

    刻意**从源码解析**而不是手写一份清单：新增一个配置项时自动纳入清理范围，
    不会出现"漏了一个 → 换个机器就红"的漂移。
    前提由 `tests/test_env_isolation.py` 钉住：这些文件里环境变量必须经
    `_env_str/_env_bool/_env_int/os.getenv("字面量")` 读取（出现别的读法就会漏项，
    那条用例会红）；且解析结果必须覆盖若干"少一个就静默出事"的变量。
    """
    names = set()
    for pattern in _ENV_SOURCE_GLOBS:
        for path in sorted(Path(ROOT).glob(pattern)):
            text = path.read_text(encoding="utf-8")
            names |= set(re.findall(
                r'(?:os\.getenv|_env_(?:str|bool|int))\(\s*"([A-Za-z0-9_]+)"', text))
    return sorted(n for n in names if not n.startswith(_KEEP_PREFIXES))


BEHAVIOR_ENV_VARS = behavior_env_names()
assert BEHAVIOR_ENV_VARS, "没能从 app/config.py 与 src/rag/* 解析出任何环境变量名：解析规则已失效"


def scrub_behavior_env():
    """把"会改变服务行为"的环境变量从本进程里删除（幂等，可反复调用）。"""
    for name in BEHAVIOR_ENV_VARS:
        os.environ.pop(name, None)


# 闸门 1：清掉 shell 里已经存在的（`export` 出去的那些；`load_dotenv` 拦不住它们）
scrub_behavior_env()

# 闸门 2：`.env` 照常加载，但**加载完立刻再清一遍**——"行为变量"一条都不许留下。
# 包一层而不是改 `override` 参数：调用方散落在 `app/main.py` 与 5 个 `src/rag/*`
# 模块里，逐个改成 `override=True` 既侵入又容易漏，而漏掉的那一处就会静默把
# 本机配置带进测试。
try:
    import dotenv as _dotenv

    _original_load_dotenv = _dotenv.load_dotenv

    def _load_dotenv_without_behavior_vars(*args, **kwargs):
        loaded = _original_load_dotenv(*args, **kwargs)
        scrub_behavior_env()
        return loaded

    _load_dotenv_without_behavior_vars.__wrapped__ = _original_load_dotenv
    _dotenv.load_dotenv = _load_dotenv_without_behavior_vars
except Exception:  # noqa: BLE001  （dotenv 是可选依赖；没装则无需拦）
    pass

from src.graph import build_graph, ReviewResult  # noqa: E402


class FakeRetriever:
    """假检索器：可指定返回值，避免测试真的去读语料 / 跑 TF-IDF。"""

    def __init__(self, hits=None):
        self.hits = hits if hits is not None else []

    def search(self, query, top_k=3):
        return self.hits


@pytest.fixture
def fake_retriever():
    """默认不召回任何片段（等价于未配置知识库的降级路径）。"""
    return FakeRetriever()


@pytest.fixture
def mock_model():
    """用 MagicMock 模拟 ChatOpenAI：generate 返回草稿，evaluate 返回合法评分。"""
    m = MagicMock()
    m.invoke.return_value = AIMessage(content="这是草稿内容。")
    struct = m.with_structured_output.return_value
    struct.invoke.return_value = ReviewResult(
        is_need_evaluate=True, score=50, feedback="建议修改"
    )
    return m


@pytest.fixture
def graph(mock_model, fake_retriever):
    return build_graph(model=mock_model, retriever=fake_retriever)
