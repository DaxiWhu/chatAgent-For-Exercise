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
    # ⚠️ 下面两项**已从 config 里删除**（2026-10-05），但仍必须清理：
    #    它们是刚被删掉的两个后门开关（共享密钥档、匿名放行开关），
    #    残留会让 `app/main.py::_assert_no_legacy_auth_knobs` 直接拒绝启动。
    #    它们由 conftest 的 `_DEAD_AUTH_KNOBS` 显式加入清理清单（因为解析不到了）。
    "SERVE_API_KEY",      # 曾 ⇒ 所有请求要带 X-API-Key（就是那次 35 个 401 的来源）
    # 用户鉴权侧（2026-10-04 起是**唯一**的身份口径）。非空 ⇒ 测试进程凭空多出
    # "可登录 / 可自助注册 / 管理闸门已打开"三种状态，而这些状态会让一批用例
    # 的**前置条件**被开发机的 `.env` 悄悄改掉（与那次 35 个 401 同一类事故）。
    "AUTH_SESSION_SECRET",       # 非空 ⇒ 能签发会话（AUTH_ENABLED 随之变真）
    "AUTH_USERS",                # 非空 ⇒ 启动时会用种子建出第一个管理员
    "AUTH_ADMIN_ROLES",          # 非空 ⇒ 管理接口的角色闸门被打开
    "AUTH_ALLOW_SELF_REGISTER",  # true ⇒ 任何人都能注册账号
    # production ⇒ REQUIRE_AUTH/ENABLE_DOCS 两个默认值翻转。
    # ⚠️ 2026-10-05 起判据是"**黑名单认开发**"：任何非 development/dev/local/test
    #    的取值都算生产，所以开发机上误 export 一个 `APP_ENV=prd` 同样会让整套测试
    #    跑在生产口径下（而多数用例察觉不到）—— 这条必须照旧清理。
    "APP_ENV",
    "REQUIRE_AUTH",
    "ENABLE_DOCS",
    "TRUST_PROXY_HEADERS",        # 限流的判据来源（信任与否都是真实故障）
    "REQUEST_TIMEOUT_SECONDS",
    "LLM_MAX_TOKENS_PER_REQUEST",
    "USER_DAILY_TOKEN_QUOTA",
    "DAILY_TOKEN_QUOTA_PER_USER",
    "RATE_LIMIT_PER_MINUTE",
    "RATE_LIMIT_ENABLED",
    "APP_VERSION",        # 空串会让 FastAPI(version="") 直接断言失败
    "DB_URL",             # 非空 ⇒ 测试会去连真库
    "LLM_API_KEY",      # 非空 ⇒ 测试会真调模型
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


def test_dead_auth_knobs_are_not_read_by_config():
    """结构守卫：`SERVE_API_KEY` / `REQUIRE_AUTH` **不许**再被 `app/config.py` 读取。

    它们是 2026-10-05 删掉的两个后门开关（共享密钥进门 / 关掉鉴权要求）。若有人
    出于"向后兼容"把它们加回 `app/config.py`，本用例会红 —— 因为只要它们还是
    配置项，就意味着对应的能力还在：
      · `SERVE_API_KEY` 是配置项 ⇒ 存在"凭一把共享密钥进门"的路径；
      · `REQUIRE_AUTH` 是配置项 ⇒ 存在"把鉴权要求关掉"的开关。
    清理清单仍会清理它们（见 `_DEAD_AUTH_KNOBS`），但那是为了防"残留导致启动被拒"，
    不是为了让它们复活。
    """
    resurrected = [n for n in CONF._DEAD_AUTH_KNOBS if n in CONF.behavior_env_names()]
    assert not resurrected, (
        f"这些已删除的后门开关又被 config 读回来了：{resurrected} —— "
        "本服务只认已验签的 JWT，没有第二种凭证来源，也没有'关掉鉴权'的开关。"
    )


def test_dead_auth_prefix_family_is_scrubbed(monkeypatch):
    """`AUTH_OIDC_` 前缀族的残留也必须被清理掉（按**前缀**匹配，不按逐条列举）。

    它们与 `_DEAD_AUTH_KNOBS` 同样会让启动守卫拒绝启动，但名字**不在**
    `behavior_env_names()` 里（`config` 已不读）⇒ 只能靠前缀族清理兜住。
    不钉这条的后果：开发机上残留一个 `AUTH_OIDC_ISSUER`，所有导入 `app.main`
    的用例一起炸，而报错信息看起来与当时的改动**毫无关系**。
    """
    monkeypatch.setenv("AUTH_OIDC_ISSUER", "leftover")
    CONF.scrub_behavior_env()
    assert "AUTH_OIDC_ISSUER" not in os.environ


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
        "AUTH_SESSION_SECRET": "attacker-secret",
        "AUTH_USERS": "admin:pbkdf2_sha256$100000$ff$ff:acme:admin",
        "AUTH_ADMIN_ROLES": "attacker",
        "AUTH_ALLOW_SELF_REGISTER": "1",
        "APP_VERSION": "",
        "DB_URL": "postgresql://u:p@127.0.0.1:1/none",
        "LLM_API_KEY": "sk-attacker",
    }
    code = (
        "import os, tests.conftest, app.main as M, app.config as c, json;"
        "print(json.dumps({"
        # ⚠️ `SERVE_API_KEY` / `REQUIRE_AUTH` 现在**必须为未设置** —— 它们是已删除的
        #    后门开关，残留会让 app.main 的启动守卫直接拒绝启动（本探针导入了 main）。
        "'serve_key': os.environ.get('SERVE_API_KEY'),"
        "'require_auth': os.environ.get('REQUIRE_AUTH'),"
        "'env': c.APP_ENV,"
        "'trust_xff': c.TRUST_PROXY_HEADERS, 'timeout': c.REQUEST_TIMEOUT_SECONDS,"
        "'per_min': c.RATE_LIMIT_PER_MINUTE, 'quota': c.USER_DAILY_TOKEN_QUOTA,"
        "'budget': c.LLM_MAX_TOKENS_PER_REQUEST,"
        "'auth_on': c.AUTH_ENABLED, 'secret': c.AUTH_SESSION_SECRET,"
        "'seed_users': len(c.AUTH_USERS), 'admins': list(c.AUTH_ADMIN_ROLES),"
        "'self_reg': c.AUTH_ALLOW_SELF_REGISTER,"
        "'version': c.APP_VERSION, 'db': c.DB_URL, 'main_key': c.API_KEY,"
        "'app_version': M.app.version}))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=CONF.ROOT, env={**os.environ, **hostile},
        capture_output=True, text=True, timeout=300,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])

    # 最重要的一条：`SERVE_API_KEY` 曾经让**每一个**请求都要带 X-API-Key
    # （那次 35 个 401）。它现在已从代码里删除 —— 敌意值既不能进入环境，也不能复活。
    assert got["serve_key"] is None, "隔离失效：敌意 SERVE_API_KEY 留在了环境里"
    assert got["require_auth"] is None, "隔离失效：敌意 REQUIRE_AUTH 留在了环境里"
    assert got["env"] == "development"
    assert got["trust_xff"] is False
    assert got["timeout"] == 0
    assert got["per_min"] == 120
    assert got["quota"] == 0
    assert got["budget"] == 0
    # 用户鉴权：这里断言的是**测试身份**而不是"关闭"。
    # 2026-10-05 起测试进程必须有一个可用的鉴权配置（否则 /v1/* 全 401，见
    # `tests/conftest.py` 的「测试专用身份」一节）。所以这条守卫证明的是：
    #   · 敌意值**没有**进来（secret 是测试值，不是 attacker-secret）；
    #   · 敌意 AUTH_USERS / AUTH_ALLOW_SELF_REGISTER 也没进来。
    assert got["auth_on"] is True
    assert got["secret"] == CONF._TEST_AUTH_ENV["AUTH_SESSION_SECRET"], (
        "隔离失效：AUTH_SESSION_SECRET 落在了敌意值上（应为测试专用值）"
    )
    assert got["secret"] != hostile["AUTH_SESSION_SECRET"]
    assert got["seed_users"] == 0, "隔离失效：敌意 AUTH_USERS 建出了种子用户"
    assert got["self_reg"] is False
    # AUTH_ADMIN_ROLES 的默认值是 fail-closed 的 ("admin",) —— 与"敌意值"刻意不同，
    # 这样这条断言才能同时证明两件事：敌意值没进来，且默认确实是收紧的那个。
    assert got["admins"] == ["admin"]
    assert got["db"] == ""
    # 模型 Key 与 AUTH_SESSION_SECRET 同一类：测试进程**必须**有一个可用值
    # （agent 链路是唯一作答路径，没配 Key 时 `app.main` 拒绝启动 ⇒
    # `tests/conftest.py` 注入了一个假 Key）。所以这里断言的是"敌意值没进来"，
    # 而不是"是空串" —— 后者在"必须给 Key"之后就不再成立。
    assert got["main_key"] == CONF._TEST_AUTH_ENV["LLM_API_KEY"], (
        "隔离失效：LLM_API_KEY 落在了敌意值上（应为测试专用值）"
    )
    assert got["main_key"] != "sk-attacker"
    # 版本号只要求"非空"：它的默认值会随发布调整，但**绝不能是空串**
    # （FastAPI 的 version 不接受空值，会在建 app 时直接 AssertionError）
    assert got["version"], "APP_VERSION 落到了空串 —— FastAPI 会因此起不来"
    assert got["app_version"] == got["version"]


# ---------------------------------------------------------------------------
# env 文件自身的守卫：同一个键不许出现两次
# ---------------------------------------------------------------------------
def _duplicate_keys(text: str) -> dict:
    """统计非注释行里出现 >1 次的键。"""
    counts: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, _ = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        counts[key] = counts.get(key, 0) + 1
    return {k: n for k, n in counts.items() if n > 1}


def test_env_files_do_not_define_the_same_key_twice():
    """`.env` / `.env.example` 里同一个键**不许**出现两次。

    为什么这不是风格问题（2026-10-06）：`dotenv` 取**最后**出现的那一份，compose
    对 `.env` 的插值同理 —— 两者都**不报错**。于是"改上面那一份"看起来改了、
    实际不生效；更麻烦的是两个值可以在不同时期被改成不同值，而"到底生效了哪个"
    取决于读的人是否完整 load 过这个文件。本仓库的 `.env` 里
    `BACKUP_INTERVAL_HOURS` / `BACKUP_KEEP` 就曾各出现两次 —— 值恰好相同，
    所以这个陷阱从没暴露过，直到有人只改一处。
    """
    for path in (Path(CONF.ROOT) / ".env.example", Path(CONF.ROOT) / ".env"):
        if not path.exists():
            # `.env` 是本机文件（已被 .gitignore 忽略），CI 上没有 —— 跳过它，
            # 但不跳过守卫本身。
            continue
        dups = _duplicate_keys(path.read_text(encoding="utf-8"))
        assert not dups, (
            f"{path.name} 里有重复定义的键：{dups}\n"
            "dotenv / compose 只认**最后**一份且不报错 ⇒ 改前面那份等于没改。"
        )
