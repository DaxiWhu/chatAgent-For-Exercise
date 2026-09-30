"""测试套件必须与开发机上的 `.env` 无关（隔离层自身的守卫）。

起因（2026-09-29）
----------------
往 `.env` 里补了一行 `SERVE_API_KEY=…`（给本机跑服务用），全量立刻 `35 failed`——
全是 401。代码没坏：`load_dotenv()` 在 `app/main.py` 与 `src/rag/*` 的**导入期**
把 `.env` 灌进 `os.environ`，而 `app/config.py` 的常量也在导入期取好值，于是
"改本机配置" = "改测试前置条件"。这类问题的三个特征让它特别贵：
  ① 本地红、CI 绿（CI 没有 `.env`），第一反应会去查代码而不是查环境；
  ② 修法很容易退化成"放宽断言"（假绿）；
  ③ 真正要测"未配置时的默认值"的用例被悄悄改掉前置条件，且**测试全绿**。

本文件不测业务，只测隔离层自己：它是否覆盖了全部会改变服务行为的变量、
是否拦得住 `.env` 的回灌、以及是否**恰好**只拦这一批（路径类变量必须留下）。
守卫对象是 `tests/conftest.py` 顶部的两段代码：`scrub_behavior_env()` 与
`load_dotenv` 包装层。删除其中任何一段，本文件都会变红。
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import tests.conftest as CONF

#: 安全/行为相关的"必清项"。这份清单不追求完整（完整由解析保证），
#: 只钉住"少一个就会静默出事"的那些。
_MUST_SCRUB = (
    "SERVE_API_KEY",      # 非空 ⇒ 所有请求要带 X-API-Key（就是那次 401 的来源）
    "TENANT_KEYS",        # 非空 ⇒ 租户改为按 Key 反查，X-Tenant-Id 只能核对
    "APP_ENV",            # production ⇒ REQUIRE_AUTH/ENABLE_DOCS 两个默认值翻转
    "REQUIRE_AUTH",
    "ENABLE_DOCS",
    "TRUST_PROXY_HEADERS",        # 限流的判据来源（信任与否都是真实故障）
    "REQUEST_TIMEOUT_SECONDS",
    "LLM_MAX_TOKENS_PER_REQUEST",
    "USER_DAILY_TOKEN_QUOTA",
    "RATE_LIMIT_PER_MINUTE",
    "RATE_LIMIT_ENABLED",
    "APP_VERSION",        # 空串会让 FastAPI(version="") 直接断言失败
    "DB_URL",             # 非空 ⇒ 测试会去连真库
    "AGNES_API_KEY",      # 非空 ⇒ 测试会真调模型
    # RAG 链路侧（由 `src/rag/*` 读取）：非空会换掉检索后端 / 去连真实向量库，
    # 实测 `RAG_VECTOR_STORE=qdrant` 会改掉 test_eval_answer 的门禁结论。
    "RAG_VECTOR_STORE",
    "RAG_EMBEDDING",
    "QDRANT_URL",
    "QDRANT_API_KEY",
    "EMBEDDING_API_KEY",
    "EMBEDDING_MODEL",
    "RERANK_API_KEY",
    "RAG_HYBRID_POOL",
    "RAG_RRF_K",
)

#: 刻意**不**清理的变量：它们不是服务行为，而是本机模型/工具链的路径与开关。
_KEEP_UNSCRUBBED = ("DOCLING_ARTIFACTS_PATH", "HF_ENDPOINT", "DOCLING_OCR")


@pytest.fixture
def _restore_env():
    """让用例可以安全地直接读写 os.environ（退出时还原）。"""
    saved = {n: os.environ.get(n) for n in _KEEP_UNSCRUBBED}
    yield
    for n, v in saved.items():
        if v is None:
            os.environ.pop(n, None)
        else:
            os.environ[n] = v


# ---------------------------------------------------------------------------
# 清理范围
# ---------------------------------------------------------------------------
def test_scrub_list_is_non_empty_and_covers_the_security_relevant_keys():
    """清理清单必须非空，且覆盖全部"少一个就会静默出事"的变量。

    清单来自 `behavior_env_names()` 对 `app/config.py` 的解析。解析规则一旦失效
    （比如有人把 `_env_str` 改名），清单会静默变空/变小 —— 那条路径上所有隔离
    都会失效而**没有任何症状**，所以必须有本条兜住。
    """
    names = CONF.BEHAVIOR_ENV_VARS
    assert len(names) >= 40, f"清理清单异常地小（{len(names)} 项），解析规则可能已失效"
    missing = [n for n in _MUST_SCRUB if n not in names]
    assert not missing, f"这些会改变服务行为的变量没被纳入清理：{missing}"


def test_rag_layer_reads_env_by_literal_names_only():
    """结构守卫：`src/rag/*.py` 读环境变量必须用**字面量**名字。

    清理清单靠在源码里搜 `os.getenv("名字")` / `_env_int("名字", …)` 得到；一旦
    写成 `os.getenv(name)`（名字来自变量），那个变量就会被漏掉，隔离对它**静默失效**。
    允许两种例外，都在注释里写明理由：
      - 局部 `_env_int(name, default)` 辅助函数里的形参 `name`；
      - `parse_cache.py` 的 `_ENV_KEYS` 列表（那是**缓存指纹**用的名字集合，不是
        "读取配置"；其成员绝大多数本来就已在清理清单里）。
    """
    allowed_vars = {"name", "k"}
    offenders = []
    for path in sorted((Path(CONF.ROOT) / "src" / "rag").glob("*.py")):
        if path.name == "parse_cache.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in re.finditer(r'os\.getenv\(\s*([A-Za-z_][A-Za-z0-9_]*)', line):
                if m.group(1) not in allowed_vars:
                    offenders.append(f"{path.name}:{lineno} → os.getenv({m.group(1)}…)")
    assert not offenders, (
        f"这些地方用变量名读环境变量，会被隔离清单漏掉：{offenders}；"
        "请改成字面量（或把该名字加进 tests/conftest.py 的解析范围）"
    )


def test_toolchain_names_are_deliberately_kept():
    """反向守卫（清单侧）：本机模型/工具链变量**不许**出现在清理清单里。

    清掉它们的代价是可验证的：docling 会转为联网下载模型（实机走代理 502），
    实测多红 8 个用例。把它们加进清理清单时本条变红。
    """
    wrongly_scrubbed = [n for n in _KEEP_UNSCRUBBED if n in CONF.BEHAVIOR_ENV_VARS]
    assert not wrongly_scrubbed, (
        f"这些变量属于本机工具链环境，不该被清理：{wrongly_scrubbed}"
        "（清掉会让 docling 转去联网下载模型 / OCR 用例拿不到本地模型）"
    )


def test_config_reads_env_only_through_the_helpers():
    """结构守卫：`app/config.py` 里不许出现裸 `os.getenv("字面量")`。

    解析规则只认 `_env_str/_env_bool/_env_int("NAME"…)`。若 config.py 里出现
    `os.getenv("NAME")`，那个变量会被解析漏掉 —— 隔离层从此对它失效，且**静默**。
    """
    text = (Path(CONF.ROOT) / "app" / "config.py").read_text(encoding="utf-8")
    offenders = re.findall(r'os\.getenv\(\s*"([A-Za-z0-9_]+)"', text)
    assert not offenders, (
        f"app/config.py 里出现了裸 os.getenv：{offenders} —— 请改用 "
        "_env_str/_env_bool/_env_int，否则 tests/conftest.py 的隔离清单会漏掉它"
    )


# ---------------------------------------------------------------------------
# 隔离是否真的生效
# ---------------------------------------------------------------------------
def test_no_behavior_var_leaks_into_the_test_process():
    """跑测试的进程里，不应存在"有值"的行为变量。

    只断言"有值"而不是"存在"：若干用例会显式 `os.environ["DB_URL"] = ""`
    （在收集期执行），那是用例自己声明的前置条件，不是本机泄漏。
    真正的泄漏一定带值 —— 例如 `.env` 里的 `SERVE_API_KEY`。
    """
    leaked = {n: os.environ[n] for n in CONF.BEHAVIOR_ENV_VARS if os.environ.get(n)}
    assert not leaked, f"本机环境泄漏进了测试进程：{sorted(leaked)}"


def test_scrub_deletes_the_key_instead_of_blanking_it(monkeypatch):
    """`scrub_behavior_env()` 必须**删除**变量，不能置空串。

    置空串有两条实测过的反向后果：`FastAPI(version="")` 直接断言失败；
    `src/rag/parse_cache.py` 的 `os.getenv("RAG_PARSE_CACHE", "1")` 会把 "" 读成
    "缓存关掉"（默认是开）。只有键不存在对所有读者都等于"未配置"。
    把 `pop` 改成 `os.environ[name] = ""` 时本条变红。
    """
    monkeypatch.setenv("SERVE_API_KEY", "sk-from-shell")
    monkeypatch.setenv("APP_ENV", "production")
    CONF.scrub_behavior_env()
    assert "SERVE_API_KEY" not in os.environ
    assert "APP_ENV" not in os.environ


def test_load_dotenv_cannot_reinject_behavior_vars(tmp_path):
    """核心机制：`.env` 加载之后，行为变量必须**仍然**不在环境里。

    这是 `load_dotenv` 包装层存在的唯一理由：`scrub_behavior_env()` 用 `pop`
    删除键，而 `load_dotenv()` 只跳过"已存在"的键 ⇒ 没有包装层时，导入期的
    一次加载就会把它们原样灌回来。
    把包装层去掉（或把 `scrub` 改成置空串）时本条变红。
    """
    import dotenv

    probe = tmp_path / ".env"
    probe.write_text("SERVE_API_KEY=sk-leaked-from-dotenv\nAPP_ENV=production\n"
                     "USER_DAILY_TOKEN_QUOTA=1\n", encoding="utf-8")
    dotenv.load_dotenv(dotenv_path=str(probe))

    assert os.environ.get("SERVE_API_KEY") is None, "行为变量被 .env 回灌了"
    assert os.environ.get("APP_ENV") != "production"
    assert os.environ.get("USER_DAILY_TOKEN_QUOTA") is None


def test_path_like_vars_are_deliberately_not_scrubbed(tmp_path, _restore_env):
    """反向守卫：路径/镜像类变量**必须**还能加载 —— 隔离不要做过头。

    它们不是服务行为，而是"本机工具链环境"。清掉它们的代价很具体：docling 会
    转为**联网下载**模型，本机走代理被 502 挡掉（实测多红 8 个用例）。
    """
    import dotenv

    probe = tmp_path / ".env"
    probe.write_text("DOCLING_ARTIFACTS_PATH=/opt/models\n"
                     "HF_ENDPOINT=https://hf-mirror.example\n", encoding="utf-8")
    for name in _KEEP_UNSCRUBBED:
        os.environ.pop(name, None)          # 先删干净，load_dotenv 只跳过已存在的键

    dotenv.load_dotenv(dotenv_path=str(probe))

    assert os.environ.get("DOCLING_ARTIFACTS_PATH") == "/opt/models"
    assert os.environ.get("HF_ENDPOINT") == "https://hf-mirror.example"


# ---------------------------------------------------------------------------
# 端到端：一个"敌意环境"打不进 config
# ---------------------------------------------------------------------------
def test_hostile_ambient_env_cannot_reach_config():
    """子进程里塞满危险值 → `app.config` 仍必须落在"未配置"的默认值上。

    这是整件事的端到端版本，也是唯一不依赖"本进程此刻恰好干净"的验证：
    把 `.env`、shell `export`、CI 注入的变量统统模拟成危险值，只导入
    `tests.conftest`（隔离层）与 `app.main`（它的**导入期**会 `load_dotenv()`，
    正是把 `.env` 灌进环境、进而让 `app.config` 取到 401 配置的那一步），
    再看拿到什么。去掉隔离层的任一段时本条变红。
    """
    hostile = {
        "SERVE_API_KEY": "sk-attacker",
        "APP_ENV": "production",
        "REQUIRE_AUTH": "0",
        "TRUST_PROXY_HEADERS": "1",
        "REQUEST_TIMEOUT_SECONDS": "1",
        "RATE_LIMIT_PER_MINUTE": "1",
        "USER_DAILY_TOKEN_QUOTA": "1",
        "LLM_MAX_TOKENS_PER_REQUEST": "1",
        "TENANT_KEYS": "acme:sk-acme",
        "APP_VERSION": "",
        "DB_URL": "postgresql://u:p@127.0.0.1:1/none",
        "AGNES_API_KEY": "sk-attacker",
    }
    code = (
        "import tests.conftest, app.main as M, app.config as c, json;"
        "print(json.dumps({"
        "'serve_key': M.SERVE_API_KEY, 'key': c.SERVE_API_KEY, 'env': c.APP_ENV,"
        "'require_auth': c.REQUIRE_AUTH,"
        "'trust_xff': c.TRUST_PROXY_HEADERS, 'timeout': c.REQUEST_TIMEOUT_SECONDS,"
        "'per_min': c.RATE_LIMIT_PER_MINUTE, 'quota': c.USER_DAILY_TOKEN_QUOTA,"
        "'budget': c.LLM_MAX_TOKENS_PER_REQUEST, 'tenants': c.TENANT_KEYS,"
        "'version': c.APP_VERSION, 'db': c.DB_URL, 'agnes': c.API_KEY,"
        "'app_version': M.app.version}))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=CONF.ROOT, env={**os.environ, **hostile},
        capture_output=True, text=True, timeout=300,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])

    # 最重要的一条：非空 serve_key 会让**每一个**请求都要带 X-API-Key（那次 35 个 401）
    assert got["serve_key"] == "", "隔离失效：app.main 拿到了危险值"
    assert got["key"] == ""
    assert got["env"] == "development"
    assert got["require_auth"] is False
    assert got["trust_xff"] is False
    assert got["timeout"] == 0
    assert got["per_min"] == 120
    assert got["quota"] == 0
    assert got["budget"] == 0
    assert got["tenants"] == {}
    assert got["db"] == ""
    assert got["agnes"] == ""
    # 版本号只要求"非空"：它的默认值会随发布调整，但**绝不能是空串**
    # （FastAPI 的 version 不接受空值，会在建 app 时直接 AssertionError）
    assert got["version"], "APP_VERSION 落到了空串 —— FastAPI 会因此起不来"
    assert got["app_version"] == got["version"]
