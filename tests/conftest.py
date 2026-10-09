import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage

# 把项目根目录加入 sys.path，使 `from src.graph import ...` 可用
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 环境变量采集正则的**唯一**来源（必须在 ROOT 入 sys.path 之后导入）。
from tests import _envscan  # noqa: E402

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
#: ⚠️ 采集面**必须覆盖每一个会读环境变量的文件**（2026-10-06 扩）。
#: 原值只有 `("app/config.py", "src/rag/*.py")`，于是 `src/graph.py` 与
#: `src/agents/*` 读的那一批（`DOCS_DIR` / `AGENTS_ENABLED` / `AGENT_MAX_TURNS` /
#: `AGENT_MAX_ANSWER_CHARS` / `AGENT_KB_TOP_K` / `AGENT_SESSION_CACHE_MAX` /
#: `RAG_TENANT_CACHE_MAX`）**从不被清理** —— 而"清不掉"正是本文件开头那段说的
#: 三类恶性后果（本地红 CI 绿 / 有人放宽断言 / 前置条件被悄悄改掉）。
#: 判据不是"这批变量眼下有没有出现在 `.env` 里"，而是"它们会不会改变服务行为"：
#: `DOCS_DIR` 眼下就写着 `./data`（代码默认是 `data/kb`），也就是说同一个测试
#: 在开发机与 CI 上跑的是**不同的语料目录**。
_ENV_SOURCE_GLOBS = ("app/config.py", "src/*.py", "src/rag/*.py", "src/agents/*.py")

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

    ⚠️ "什么写法算一次读取"由 `tests/_envscan.py` **统一**提供，不在这里再写一份
    正则 —— 两份正则各改一半的漂移已经真实发生过一次（见那个模块的 docstring），
    而它的症状是"某个键悄悄从清理清单里消失"，不会报错。
    """
    names = set()
    for pattern in _ENV_SOURCE_GLOBS:
        names |= _envscan.env_names_in_paths(sorted(Path(ROOT).glob(pattern)))
    return sorted(n for n in names if not n.startswith(_KEEP_PREFIXES))


#: 已失效、但**必须**一并清理的鉴权开关（2026-10-05）。
#:
#: 为什么不靠解析得到：`app/config.py` 已经不读它们了 —— 那才是"真的删干净"。
#: 而"残留"有两种具体危害，都不是理论上的：
#:   ① `app/main.py::_assert_no_legacy_auth_knobs` 会**直接拒绝启动**（刻意设计，
#:      见那里的说明：这两个开关曾经真能改变鉴权结果，不能让人以为它们还管用）；
#:   ② 它们正是刚被删掉的两个后门（共享密钥档、匿名放行开关），
#:      留一份在测试环境里会让"本机 vs 测试"的困惑原样回来。
_DEAD_AUTH_KNOBS = ("SERVE_API_KEY", "REQUIRE_AUTH")

#: 同上的**前缀族**（2026-10-05 外部身份源 OIDC 整体删除后新增）。
#:
#: ⚠️ 为什么不并进 `_DEAD_AUTH_KNOBS` 逐条列举：这一族有 10 个键，而清理清单
#: 漏一项是**没有症状**的 —— 症状只会出现在别人的机器上（那次 35 个 401 就是
#: "本机 `.env` 里多了一行"引起的）。前缀匹配不需要有人去数。
_DEAD_AUTH_PREFIXES = ("AUTH_OIDC_",)

BEHAVIOR_ENV_VARS = sorted(set(behavior_env_names()) | set(_DEAD_AUTH_KNOBS))
assert BEHAVIOR_ENV_VARS, "没能从 app/config.py 与 src/rag/* 解析出任何环境变量名：解析规则已失效"


def scrub_behavior_env():
    """把"会改变服务行为"的环境变量从本进程里删除（幂等，可反复调用）。"""
    for name in BEHAVIOR_ENV_VARS:
        os.environ.pop(name, None)
    # 前缀族单独走一遍：它们的名字**不在** `BEHAVIOR_ENV_VARS` 里（`config` 已不读
    # 它们），只能按前缀动态匹配环境里**实际存在**的那些。
    for name in [n for n in os.environ if n.startswith(_DEAD_AUTH_PREFIXES)]:
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


# ===========================================================================
# 测试专用身份：让 `app.config` 在**导入期**就带上鉴权配置
# ===========================================================================
# 背景（2026-10-05）：服务端**只剩一条鉴权路径** —— 账号密码 / 外部 IdP → JWT。
# 匿名兜底与共享密钥档（`X-API-Key`）都已从代码里删除，于是"不带凭证"的请求
# 一律 401。测试要调 `/v1/*` 就必须持有一个**真实签发**的身份。
#
# 为什么用"注入 + 立刻撤销"这种方式，而不是让测试进程长期带着这些变量
# -------------------------------------------------------------------
# 本文件的既定不变量是「**测试进程等价于裸环境**」，它由
# `tests/test_env_isolation.py` 的守卫钉着。如果让 `AUTH_SESSION_SECRET` 一直留在
# 环境里，那条守卫会红，而且"本机配置 vs 测试前置条件"的困惑会重新回来。
#
# 做法：在**导入 `app.config` 那一瞬间**把测试身份放进环境，导入完立刻擦掉。
# `config` 的所有常量是导入期求值的 ⇒ 值已经拿到；而此后环境恢复成裸的 ⇒
# 守卫继续成立、其它用例的前置条件不变。
#
# ⚠️ 顺序不能动：`app.config` 必须在这里被**第一个**导入。之后再有人
# `import app.main`，它内部的 `load_dotenv()` 与 `from . import config` 都只会
# 命中 `sys.modules` 里的这份缓存，不会再重新求值。
_TEST_AUTH_ENV = {
    # 会话签名密钥。**只有测试进程见过它**，与任何真实部署无关。
    "AUTH_SESSION_SECRET": "pytest-only-session-secret-not-a-real-key",
    # 有效期放大到一天：全量回归可能跑十几分钟，不能让令牌在跑的过程中过期
    # （那会表现为"后半段用例随机 401"这种与代码无关的诡异失败）。
    "AUTH_ACCESS_TTL": "86400",
    "AUTH_REFRESH_TTL": "86400",
}


def _install_test_auth_env():
    os.environ.update(_TEST_AUTH_ENV)


_install_test_auth_env()
import app.config  # noqa: E402,F401  （必须在注入之后、且只导入一次）

scrub_behavior_env()


# ===========================================================================
# 重载 `app.config` 的**唯一**正确姿势
# ===========================================================================
# 真实踩过的坑（2026-10-05）：`test_agent_history_budget.py` 里有一条用例要验
# "`AGENT_HISTORY_TOKEN_LIMIT` 被 config 与 history_budget 两处解析后口径一致"，
# 做法是 `importlib.reload(app.config)`。而重载会把**所有**模块级常量重新求值 ——
# 包括上面刚注入的 `AUTH_SESSION_SECRET`。可那时环境已被擦干净，于是重载后
# `config.AUTH_SESSION_SECRET == ""`：
#   · 各文件在**收集期**用旧密钥签好的会话 token 立刻验不过 ⇒ 后面所有用例 401；
#   · 症状出现在**别的文件**里（`test_api` / `test_api_v1` / `test_idempotency` …），
#     报的是"鉴权失败"，根因却在几百个用例之前 —— 单跑那些文件全绿，最难定位的一类。
#     （实测：仅这一处，让全量多红 60 条。）
# 所以需要重载 config 的用例一律走下面这个函数。
def reload_app_config():
    """重载 `app.config`，并保证**测试身份仍在**（用完把环境还原成调用前的样子）。

    ⚠️ 不要在用例里直接写 `importlib.reload(app.config)`。

    为什么是"注入 → 重载 → 还原"而不是"重载后再擦一遍"：调用方常常同时依赖
    别的行为变量（例如上面那条用例依赖 `AGENT_HISTORY_TOKEN_LIMIT` 能被
    `src/agents/history_budget.py` 读到）。用 `scrub_behavior_env()` 会把它们一起
    抹掉，于是"被测量的东西"消失了、断言看起来还通过。所以这里只动自己注入的三个键。

    守卫：`tests/test_config_env_policy.py::test_config_reload_keeps_the_test_identity`
    与下面的 `_guard_test_identity_survives_each_test`（每个用例后自动检查）。
    """
    import importlib

    import app.config as _config

    saved = {k: os.environ.get(k) for k in _TEST_AUTH_ENV}
    os.environ.update(_TEST_AUTH_ENV)
    try:
        return importlib.reload(_config)
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


@pytest.fixture(autouse=True)
def _guard_test_identity_survives_each_test():
    """每个用例结束后：`app.config` 里的**测试身份**必须还在。

    这条守卫的唯一目的，是把上面那类"跨文件静默污染"变成**当场可见的失败**：
    否则排查方向会落在"某个文件的鉴权坏了"上，而真正的原因在几百个用例之前。

    判据刻意只取**会话签名密钥**一个值：它是"先前签发的 token 还能不能验过"的
    唯一决定因素，且它一旦被冲掉，症状（满天 401）与被破坏的东西之间隔了很远。
    """
    yield
    # 先落到小写局部名再比较：直接写 `CONST != 变量` 会被 ruff 的 SIM300
    # 当成 yoda 条件（它把全大写的属性访问视作"常量"，要求常量放右边）。
    secret_now = app.config.AUTH_SESSION_SECRET
    if secret_now != _TEST_AUTH_ENV["AUTH_SESSION_SECRET"]:
        pytest.fail(
            "`app.config` 的会话签名密钥在本用例里被改掉了"
            f"（现在是 {app.config.AUTH_SESSION_SECRET!r}）。"
            "最常见的成因是在用例里直接 `importlib.reload(app.config)` —— 重载会"
            "在**已擦净的环境**上重新求值，密钥变成空串，于是其它文件在收集期签好的 "
            "token 全部验签失败，表现为满天 401 而单跑每个文件都是绿的。"
            "需要重载 config 请改用 `tests.conftest.reload_app_config()`。"
        )

@pytest.fixture(autouse=True)
def _restore_observability_sink_each_test():
    """每个用例结束后：`src/observability` 的出口必须还原。

    与上面那条守卫同一类问题（**跨文件静默污染**），只是被污染的对象是"指标出口"：
    它是**进程级单例**，而 `app.main` 导入时已经装好了一个 `_MetricsSink`。
    某个用例若把它换成自己的替身后忘了放回去（最典型的错法是图省事写
    `reset_sink()`，那会清成 `None` 且**没有任何人**会重新装上），
    之后所有依赖这个出口的用例都收不到任何指标。

    实测（2026-10-06）：`tests/test_hybrid.py` 里一个新加的 fixture 用了
    `reset_sink()`，`tests/test_runtime_metrics.py` 当场红了 **11 个**用例 ——
    而单跑该文件全绿、报错信息里也看不出与 test_hybrid 有任何关系。
    "忘记还原"的代价与出错地点离得太远，所以由框架兜住，不靠自觉。
    """
    from src import observability

    saved = observability.get_sink()
    yield
    observability.set_sink(saved)


from src.graph import ReviewResult, build_graph  # noqa: E402


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
