"""文档漂移守卫：让"文档说 A、代码/配置做 B"变成机器可检的。

为什么需要它
-----------
本项目最典型的一类事故不是"代码写错"，而是**文档与实现悄悄分叉**，而且
通常是文档落后于实现。已发生的两次：

  1. `PRODUCTION_CONFIG.md` §六 写着 `RAG_VECTOR_STORE=pg`，`.env` 与代码
     实际是 `qdrant`（2026-09-29 迁移后没同步）—— 照着文档排查的人会去翻
     pgvector，而真正的向量库是 Qdrant；
  2. `deploy/monitoring/alertmanager.yml` 与 `.env.example` 都写着
     "凭据硬编码在 alertmanager.yml，所以必须过 alert_sink"，而实际连接线
     早就是 `http://alert-sink:8080/alerts/<receiver>`。

两者的共同点是：**没有任何运行时信号**。文档不会报错，代码也不读文档。
所以只能靠断言钉住。这里只做三类**能自动判定**的检查：

  ① 文档里出现的环境变量名必须真实存在（防"讨论一个已删除的配置项"）；
  ② 文档声称的告警数量/分组必须与 `alerts.yml` 一致；
  ③ 文档里疑似写错的告警名（与真实名字只差一两个字符）必须被指出来。

刻意**不**做的一类：断言"文档里写的当前值一定等于 .env"。原因有二：
  - CI 里没有 `.env`（它是 gitignored 的），断言会变成必须 skip 的假守卫；
  - `.env.example` 与 `.env` 本来就该不同（模板给的是安全默认 `memory`，
    本机可能选了 `qdrant`），把它们强行对齐反而会逼着人写错模板。
  只对**已经在代码里枚举出来的取值**做校验（见第 ② 条里的向量库后端）。
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

import pytest
import yaml

from tests import _envscan

ROOT = Path(__file__).resolve().parent.parent
DOCS = [
    ROOT / "PRODUCTION_CONFIG.md",
    ROOT / "deploy" / "RUNBOOK.md",
    ROOT / "deploy" / "CANARY.md",
    ROOT / "deploy" / "SECRETS.md",
    # 幂等键规范（2026-10-03 新增）：它是对客户端的**承诺**，里面点了
    # IDEMPOTENCY_* 一串配置项。一份承诺文档写错变量名，与运维文档写错同样致命。
    ROOT / "deploy" / "IDEMPOTENCY.md",
]
ALERTS = ROOT / "deploy" / "monitoring" / "alerts.yml"


def _env_example_keys() -> set[str]:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r"(?m)^([A-Z_][A-Z0-9_]*)\s*=", text))


def _code_keys() -> set[str]:
    """从**真实代码**里推导"已知配置键"，而不是手工维护白名单。

    采集面必须覆盖**所有读环境变量的地方**，否则白名单会漏，守卫会误报
    （2026-09-30 首次运行就漏了 `serve.py` 与 `src/rag/*`：`APP_WORKERS`、
    `RERANK_API_BASE`、`DOCLING_*` 全被误判成"不存在的键"）。

    覆盖来源：
      - `app/config.py`：统一入口，用 `_env_str/bool/int/float("KEY")` 读；
      - `app/serve.py`：**例外**，argparse 直接读 `APP_WORKERS` /
        `WEB_CONCURRENCY` / `GRACEFUL_SHUTDOWN_TIMEOUT`（有意绕过 config）；
      - `src/rag/*.py`：检索链路模块在导入期 `os.getenv(...)` 读一批
        `DOCS_DIR` / `RERANK_*` / `EMBEDDING_*` / `DOCLING_*`；
      - `src/agents/*.py`：agent 链路模块同样直读（`AGENT_SESSION_CACHE_MAX` /
        `AGENT_SESSION_PING_TIMEOUT`）。**2026-10-03 补** —— 缺这一段时，那两个
        真实存在的键会被误判成"文档在教一个不存在的变量"；
      - `scripts/*.py`：运维脚本直接读 `DB_URL` / `HF_ENDPOINT` 等。

    只认 `os.getenv("X")` / `os.environ.get("X")` / `os.environ["X"]` 与
    `_env_*(` 这两种**字面量**写法 —— 变量名拼接的读法本就无法静态确认，
    这属于刻意的能力边界。
    """
    targets = [ROOT / "app" / "config.py", ROOT / "app" / "serve.py"]
    targets += sorted((ROOT / "src").glob("*.py"))
    targets += sorted((ROOT / "src" / "rag").glob("*.py"))
    targets += sorted((ROOT / "src" / "agents").glob("*.py"))
    targets += sorted((ROOT / "scripts").glob("*.py"))

    # ⚠️ "什么写法算一次读取"由 `tests/_envscan.py` **统一**提供。
    # 这里原先自己写了一份正则，与 `tests/conftest.py::behavior_env_names()`
    # 那一份**各改一半**漂移过一次（2026-10-06 引入 `env_int("X")` 读法时）：
    # `RAG_RRF_K` 从清理清单里静默消失，没有任何报错。两份正则不再各自维护。
    return _envscan.env_names_in_paths(p for p in targets if p.exists())


def _compose_keys() -> set[str]:
    keys: set[str] = set()
    for path in (ROOT / "deploy" / "app").glob("docker-compose*.yml"):
        text = path.read_text(encoding="utf-8")
        keys |= set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", text))
    return keys


def _known_keys() -> set[str]:
    # 少数只在运维脚本/文档里出现、不进 app 配置的键（刻意列成白名单而不是放宽正则：
    # 放宽正则会让"胡乱大写一个词"也通过，守卫就失效了）
    extra = {
        "DB_URL",            # migrations CLI / 备份脚本直接读
        "PG_CONTAINER",      # scripts/pg_backup.py
        "BACKUP_DIR",        # scripts/qdrant_backup.py
        "PROMETHEUS_URL",    # scripts/canary_check.py
        # `ROLLBACK_RECEIVER` 由 **shell** 读（`deploy/rollback.sh` 的
        # `${ROLLBACK_RECEIVER:-oncall}`），而 `_code_keys()` 只扫 `*.py` ——
        # 这是静态分析的能力边界，不是"文档写错"。`deploy/CANARY.md` 提到它是
        # 有意义的：回滚通知发到哪个群是可配置的，运维照着改时得知道变量名。
        "ROLLBACK_RECEIVER",
        "COMPOSE_FILE",      # docker compose 自身
        "PYTHONPATH",
        "GITHUB_OUTPUT",
        "GITHUB_ACTIONS",
        "GITHUB_ENV",
        # 下面两个是**动态读法**，静态扫不到（`serve.py` 里
        # `for name in ("APP_WORKERS", "WEB_CONCURRENCY"): os.getenv(name)`）：
        # 名字在循环变量里，正则拿不到字面量。它们确实是真实配置键，
        # 所以手工补进白名单 —— 这不是"放宽"，只是补上静态分析的能力边界。
        "APP_WORKERS",
        "WEB_CONCURRENCY",
        # `PROMPT_VERSION` **不是环境变量**，而是 `src/prompts.py` 里的模块常量
        # （所以既不在 `.env.example`、也不被 `_code_keys()` 的 os.getenv 正则抓到）。
        # 但运维文档提到它是**有意义的**：模型开始编造引用编号时，第一件要确认的事
        # 就是"最近有没有人 bump 过它"（见 RUNBOOK `CitationUnsupportedFrequent`）。
        # 这里补白名单而不是把文档里的反引号去掉 —— 去掉只是让守卫闭嘴，
        # 而它本来就该被当成一个"真实存在的、会影响线上行为的标识符"。
        "PROMPT_VERSION",
        # `NO_TENANT_SENTINEL` 也**不是环境变量**，而是 `app/config.py` 的模块常量
        # （`"%__no_tenant__%"`）。运维文档提到它是有意义的：`RAG_TENANT` 留空时
        # 无租户账号会落到这个哨兵值上、什么都检索不到 —— 这与"留空 = 不加过滤"
        # 的旧口径**结论正好相反**，写清楚才能避免有人按旧语义留空。
        "NO_TENANT_SENTINEL",
        # 脚本/CI 的凭证变量（2026-10-04 新增）：两类都真实存在，但静态扫不到 ——
        #   · `AGENT_SCRIPT_USER` / `AGENT_SCRIPT_PASSWORD`：读法是
        #     `os.getenv(USER_ENV, "")`，名字在**模块常量**里（`scripts/script_auth.py`
        #     刻意把变量名抽成常量，因为报错文案与文档都要引用它）；
        #   · `E2E_USER` / `E2E_PASSWORD`：读在 `tests/test_e2e_stack.py` 里，
        #     而 `_code_keys()` 只扫 `app/` + `src/` + `scripts/`（**刻意不扫 tests/**
        #     —— 测试会读一堆环境变量，扫进来等于把守卫悄悄放宽）。
        # `deploy/SECRETS.md` 提到它们是有意义的：退役共享密钥的照做清单必须写明
        # "谁该换成什么"，否则读的人只知道方向、不知道动作。
        "AGENT_SCRIPT_USER",
        "AGENT_SCRIPT_PASSWORD",
        "E2E_USER",
        "E2E_PASSWORD",
    }
    return _env_example_keys() | _code_keys() | _compose_keys() | extra


#: 已删除的配置键：文档**允许**提到它们，由此处的清单豁免。
#:
#: 为什么需要这份清单，而不是"逐行加删除线"或"逐行写'已删除'"
#: -------------------------------------------------------------------
#: 删掉一个**功能**（而不是改个拼写）之后，文档里会自然留下一大段"为什么删、
#: 删了什么、怎么核对没删漏"的记录，而那些句子必然要成段引用已删除的键名 ——
#: 逐行加标记会让整段读不成句，而逐行加标记这件事本身也没人愿意做（于是要么
#: 漏掉、要么把守卫的判据改松）。
#:
#: ⚠️ 这**不是**"把三个最危险的键放行"。它与白名单的根本区别是有**反向锁**：
#:    `test_deleted_keys_are_really_gone_from_code` 断言这三个键**真的**不在
#:    代码 / `.env.example` / compose 里。一旦谁把它们加回来，那条立刻红。
#:    ⇒ 语义是"文档可以记录它们已经死了"，而不是"它们可以随便留着"。
_DELETED_KEYS = {
    "SERVE_API_KEY",   # 共享密钥通行证（X-API-Key）：分不出身份、撤不掉、租户自报
    "REQUIRE_AUTH",    # "关掉鉴权要求"的开关（匿名兜底的开关侧）
    "TENANT_KEYS",     # "租户 ↔ API Key"对照表
}

#: `_DELETED_KEYS` 的**前缀族**版本：键名以这些前缀开头即视为"已删除"（2026-10-05 新增）。
#:
#: ⚠️ 为什么需要它 —— 外部身份源（OIDC）那一族有 10 个键。逐条写进 `_DELETED_KEYS`
#: 有两个问题：
#:   ① 冗长且没人愿意维护；
#:   ② 真正危险的是**漏项**：文档引用了一个没被列进清单的 `AUTH_OIDC_*`，守约会红，
#:      而修的人多半会去"补一条白名单"、而不是怀疑"是不是文档在描述一个已删除的
#:      配置" —— 那样守卫就被逐条改松了，且松得看不出来。
#: 前缀把"数漏"从根上消掉。反向锁（下一条用例）同样按前缀判定。
_DELETED_KEY_PREFIXES = ("AUTH_OIDC_",)


def _is_deleted_key(name: str) -> bool:
    """该键是否属于"已删除"（精确清单 或 前缀族）。"""
    return name in _DELETED_KEYS or name.startswith(_DELETED_KEY_PREFIXES)


def _env_key_offenders(text: str) -> list[tuple[str, str]]:
    """抽出"被反引号包着、看起来像环境变量、但实际不存在"的标识符。

    只认"含下划线"的大写串：纯大写无下划线的词（`DEBUG` / `INFO`）与
    路径片段（`ERROR_BUDGET_POLICY.md` —— 结尾的反引号不紧跟在名字后面）
    都会被自然排除。
    """
    known = _known_keys()
    offenders = []
    for line in text.splitlines():
        if "~~" in line or "已废弃" in line or "已删除" in line:
            continue  # 显式标注为废弃的说明是**必须留**的
        for key in re.findall(r"`([A-Z][A-Z0-9_]{3,})`", line):
            if "_" not in key:
                continue
            if key in known or _is_deleted_key(key):
                continue
            offenders.append((key, line.strip()[:100]))
    return offenders


def test_deleted_keys_are_really_gone_from_code():
    """`_DELETED_KEYS` 里的键必须**真的**已经不在代码 / 模板 / compose 里。

    这是上面那份"已删除清单"的**反向锁**，也是它不构成"把守卫改松"的全部理由：
    允许文档提到某个键，前提是它确实已经死了。缺了这条断言，"允许文档提到 X"
    会退化成"X 可以随便留着" —— 守卫从"防文档漂移"变成"防文档漂移但豁免三个
    最危险的键"，而那三个恰好是刚被删掉的两扇后门加一张租户密钥表。

    前缀族（`_DELETED_KEY_PREFIXES`）同样按前缀判定 —— 语义一致：文档可以记录
    "这一族已经死了"，但族里任何一个键都不许在代码 / 模板 / compose 里复活。
    """
    for label, keys in ((".env.example", _env_example_keys()),
                        ("代码（app/ + src/ + scripts/）", _code_keys()),
                        ("compose", _compose_keys())):
        still_there = sorted(k for k in keys if _is_deleted_key(k))
        assert not still_there, (
            f"{label} 里仍然存在已删除的配置键：{still_there}。"
            "要么把它们删干净，要么（如果是有意重新引入）从 `_DELETED_KEYS` / "
            "`_DELETED_KEY_PREFIXES` 里拿掉并补回对应的守卫与文档。"
        )


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_env_keys_mentioned_in_docs_actually_exist(doc):
    """文档里 `反引号` 包起来的全大写标识符必须是真实存在的配置键。

    这条防的是"文档在教你配一个已经不存在的变量" —— 那比没有文档更糟：
    你按它做了、没有任何效果、也没有任何报错。
    （`DISCONNECT_POLL_INTERVAL` 就是这么被删掉的，文档里保留了一行
    带删除线的说明，所以下面要把"明确标注为废弃"的行排除。）
    """
    offenders = _env_key_offenders(doc.read_text(encoding="utf-8"))
    assert not offenders, (
        "以下文档里提到的配置键不存在（拼错？已删除没同步？）：\n"
        + "\n".join(f"  {k}  ←  {ctx}" for k, ctx in offenders)
    )


def test_env_keys_mentioned_in_alert_runbooks_actually_exist():
    """`alerts.yml` 的 runbook 里提到的配置键同样必须真实存在。

    为什么把守卫扩到这份 YAML（2026-10-02 扩）
    ---------------------------------------
    告警的 runbook 是**事故当中唯一会读的那段文字**。它写错一个变量名的代价
    与文档写错完全一样：照着做、没有任何效果、也没有任何报错 —— 而且更容易
    发生，因为它和 `description` / `expr` 混在一份"机器读的文件"里，
    没人会想到去核对里面的标识符是不是真的存在。

    这个错误**真实发生过**：`SharedBackendDegraded` 的 runbook 教人检查
    `RATE_LIMIT_BACKEND` / `QUOTA_BACKEND`，而共享后端一共只有 `REDIS_URL`
    一个键 —— 照着查的人在配置文件里永远找不到那两个名字。
    扩展到本文件后，这条守卫立刻就把它们抓了出来（初次套用时全文件零误报）。
    """
    offenders = _env_key_offenders(ALERTS.read_text(encoding="utf-8"))
    assert not offenders, (
        "以下告警 runbook 里提到的配置键不存在（拼错？已删除没同步？）：\n"
        + "\n".join(f"  {k}  ←  {ctx}" for k, ctx in offenders)
    )


def test_doc_alert_headcount_matches_alerts_yml():
    """文档里写的"几组几条"必须与 `alerts.yml` 实际一致。

    数目本身不影响功能，但它是一个**高频漂移点**：每加一条规则都要改文档，
    而漏改之后，接手的人会拿着错误的规模去判断"这套告警够不够用"。
    """
    cfg = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
    groups = cfg["groups"]
    n_groups = len(groups)
    n_rules = sum(len(g["rules"]) for g in groups)

    text = (ROOT / "PRODUCTION_CONFIG.md").read_text(encoding="utf-8")
    claims = re.findall(r"\*\*(\d+)\s*组\s*(\d+)\s*条\*\*", text)
    assert claims, (
        "PRODUCTION_CONFIG.md 里没找到『N 组 M 条』这个声明 —— "
        "要么格式变了，要么声明被删了；这两种都要人工确认后再改本用例"
    )
    for g, r in claims:
        assert (int(g), int(r)) == (n_groups, n_rules), (
            f"文档声称 {g} 组 {r} 条，alerts.yml 实际是 {n_groups} 组 {n_rules} 条"
        )


def _alert_names() -> set[str]:
    cfg = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
    return {rule["alert"] for g in cfg["groups"] for rule in g["rules"]}


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_docs_do_not_misspell_alert_names(doc):
    """文档里出现的、**疑似写错**的告警名必须被指出来。

    刻意不做"文档里每个 CamelCase 词都必须是告警名"（那会误伤
    `ChatAgent` 这类普通标识符）。这里只抓**与某个真实告警名高度相似
    但并不相等**的词 —— 那几乎一定是笔误，而笔误的后果是值班的人
    按名字去搜却搜不到对应的处置条目。
    """
    names = _alert_names()
    text = doc.read_text(encoding="utf-8")

    suspects = []
    for token in set(re.findall(r"`([A-Z][A-Za-z]{6,})`", text)):
        if token in names:
            continue
        close = difflib.get_close_matches(token, names, n=1, cutoff=0.85)
        if close:
            suspects.append((token, close[0]))

    assert not suspects, (
        f"{doc.name} 里疑似把告警名写错了：{suspects}\n"
        "（左=文档里的写法，右=alerts.yml 里的真实名字）"
    )


def test_vector_store_backends_are_enumerated_in_code():
    """向量库后端的取值必须是**代码里真的分支支持**的那几个。

    这条是给下面那条兜底的：如果哪天有人加了一个后端却没更新这里的枚举，
    本用例会红 —— 而不是让"文档写什么都能通过"。
    """
    text = (ROOT / "src" / "rag" / "retrieval.py").read_text(encoding="utf-8")
    for backend in ("memory", "pg", "qdrant"):
        assert f'"{backend}"' in text, (
            f"代码里找不到向量库后端 {backend!r} 的分支 —— "
            "本用例的枚举需要同步（先确认它是真的被删了，还是改名了）"
        )


def test_production_config_vector_store_is_a_real_backend():
    """`PRODUCTION_CONFIG.md` 声称的向量库后端必须是真实支持的后端。

    这条直接对应已发生的那次漂移（文档写 `pg`、实际用 `qdrant`）。
    只校验"取值是合法的"，不与 `.env` 做等值断言 —— 理由见模块 docstring。
    """
    text = (ROOT / "PRODUCTION_CONFIG.md").read_text(encoding="utf-8")
    m = re.search(r"^-?\s*\|?\s*`RAG_VECTOR_STORE`[^|\n]*\|[^|\n]*\*\*`(\w+)`\*\*", text, re.M)
    if not m:
        m = re.search(r"`RAG_VECTOR_STORE`[^\n]*?`(memory|pg|qdrant)`", text)
    assert m, (
        "PRODUCTION_CONFIG.md 里没解析到 RAG_VECTOR_STORE 的当前取值 —— "
        "要么格式变了，要么这一行被删了；两种都要人工确认后再改本用例"
    )
    assert m.group(1) in {"memory", "pg", "qdrant"}, (
        f"文档声称 RAG_VECTOR_STORE={m.group(1)}，而代码只支持 memory/pg/qdrant"
    )


#: `docker compose` 的子命令。用来把"可执行的调用"与"正文里提到这个词"分开
#: （`（docker compose）专用变量` / `docker compose 会对 `$` 做插值` 这类叙述不该被拦）。
_COMPOSE_SUBCOMMANDS = (
    "up", "down", "ps", "logs", "exec", "run", "stop", "start", "restart",
    "config", "build", "pull", "rm", "cp", "top", "events", "port", "images",
    "kill", "pause", "unpause", "wait", "version", "ls",
)


def _is_compose_invocation(line: str) -> bool:
    """这一行看起来是一次**可执行**的 compose 调用吗？

    判据：`docker compose` 后面紧跟的东西要么是子命令、要么是个选项（`-f` /
    `--env-file`）。正文里的提及（后面跟着 `）`、`**`、`...` 等）一律不算。

    ⚠️ 为什么需要这个判据，而不是简单判 `"docker compose" in line`：
    这两个词在文档里大量出现在**叙述**中（"docker compose 会对 `$` 做插值"），
    一刀切会把它们全判成违规 —— 一个会误报的守卫很快就会被人加白名单加到失效。
    """
    _, _, tail = line.partition("docker compose")
    tokens = tail.split()
    if not tokens:
        return False
    head = tokens[0]
    return head in _COMPOSE_SUBCOMMANDS or head.startswith("-")


def _join_continuations(text: str) -> list[tuple[int, str]]:
    """把 `\\` 续行拼成逻辑行，返回 `(起始行号, 逻辑行文本)`。

    必要性（2026-10-06 修）：`PRODUCTION_CONFIG.md` 里那条两层覆盖文件的启动命令
    把 `-f` 写在了**续行**上 —— 逐行看第一条只有 `--env-file`，于是被判成"缺 -f"。
    这是判据的错，不是文档的错。
    """
    out: list[tuple[int, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        start, line = i + 1, lines[i]
        while line.rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            line = line.rstrip()[:-1] + " " + lines[i]
        out.append((start, line))
        i += 1
    return out


def test_docs_never_show_a_bare_compose_command():
    """文档与配置里**可执行**的 compose 调用必须同时给出 `-f` 与 `--env-file .env`。

    核实过的官方行为：没有 `--env-file` 时，compose 从**项目目录**
    （= 第一个 `-f` 文件所在目录，这里是 `deploy/app/`）加载 `.env`，
    **仓库根的 `.env` 完全不参与插值**。于是"照文档把 webhook 填进 .env"这一步
    事实上没生效，而且没有任何报错。

    ⚠️ 原判据（2026-10-06 修）是 `"deploy/app/" in line and "--env-file" not in line`
    ----------------------------------------------------------------
    它只拦**已经写了 `-f deploy/app/...`** 的行 —— 而"完全不写 `-f`"的短写法
    （`docker compose logs app-stable`）恰好从判据下面穿过去。于是 `RUNBOOK.md`
    里积了 **20 条**这样的命令：从仓库根照抄会直接报
    `no configuration file provided`，而守卫是绿的 —— 给人"这条已经被禁止了"的
    错觉，比没有守卫更糟。

    现在改成"任何像调用的 compose 行都必须自证两件事"，并把 `-f` 与
    `--env-file` 都纳入判据（前者保证找得到文件，后者保证读得到根 `.env`）。
    """
    offenders = []
    targets = list(DOCS) + [
        ROOT / ".env.example",
        ROOT / "deploy" / "app" / "docker-compose.yml",
        ROOT / "deploy" / "app" / "docker-compose.staging.yml",
        ROOT / "deploy" / "app" / "canary.env",
        ROOT / "deploy" / "monitoring" / "alerts.yml",
        ROOT / "deploy" / "monitoring" / "prometheus.yml",
        ROOT / "deploy" / "release.sh",
        ROOT / "deploy" / "rollback.sh",
    ]
    for path in targets:
        if not path.exists():
            continue
        for lineno, line in _join_continuations(path.read_text(encoding="utf-8")):
            if not _is_compose_invocation(line):
                continue
            # `-f` 的长写法是 `--file`；空格式与 `=` 式都要认（换个写法就绕过守卫
            # 是最典型的"守卫教人怎么写文档"，不是它该管的事）。
            has_file = any(t in line for t in (" -f ", " --file ", " -f=", " --file="))
            has_env = any(t in line for t in (" --env-file ", " --env-file="))
            missing = ([("-f" if not has_file else "")] if not has_file else []) + \
                      ([("--env-file" if not has_env else "")] if not has_env else [])
            missing = [m for m in missing if m]
            if missing:
                offenders.append(
                    f"{path.relative_to(ROOT)}:{lineno}: 缺 {'/'.join(missing)}"
                    f" | {line.strip()[:100]}")
    assert not offenders, (
        "以下 compose 调用不完整 —— 从仓库根照抄会报 "
        "`no configuration file provided`，或**读不到仓库根的 .env**（配置静默退回默认值）：\n"
        + "\n".join(offenders)
    )


def test_docs_do_not_pass_the_shared_key_as_a_credential():
    """部署/运维文档**不得**把 `$SERVE_API_KEY` 当作可直接执行的凭证。

    2026-10-04：脚本（压测、端到端、评测）已改用「服务账号 → `POST /v1/auth/login`
    → Bearer 令牌」，共享密钥 `SERVE_API_KEY` 降级为**已废弃**的过渡路径。但
    `deploy/RUNBOOK.md` 与 `deploy/CAPACITY.md` 里各留着一条
    `--api-key "$SERVE_API_KEY"` 式的示例命令 —— 照着做的人会把废弃路径**当成官方
    用法**继续用下去，而"过渡"正是这样永不结束的。

    为什么拿 `$SERVE_API_KEY` 这个**展开形式**做判据：裸变量名（反引号包裹）在
    `deploy/SECRETS.md` 里是**必须**出现的（那一节讲轮换与退役）；只有 `$` 前缀才会
    出现在"把它填进环境、然后当凭证发出去"的命令里 —— 那才是要禁的形态。
    负向验收（`data/kb/06` 的"`X-API-Key` 已不是凭证 ⇒ 401"）不含
    `$SERVE_API_KEY`，不会误报。
    （2026-10-05：该负向验收的期望码从 403 改成 401 —— 请求头不再是凭证之后，
    带它进门连身份都没有，轮不到"无权管账号"这一步。）
    """
    targets = [
        ROOT / "README.md",
        ROOT / "PRODUCTION_CONFIG.md",
        ROOT / "deploy" / "RUNBOOK.md",
        ROOT / "deploy" / "CAPACITY.md",
        ROOT / "deploy" / "SECRETS.md",
        ROOT / "deploy" / "CANARY.md",
        ROOT / "deploy" / "monitoring" / "ERROR_BUDGET_POLICY.md",
    ]
    offenders = []
    for path in targets:
        if not path.exists():
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "$SERVE_API_KEY" in line:
                offenders.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()[:110]}")
    assert not offenders, (
        "以下文档把已废弃的共享密钥 `$SERVE_API_KEY` 当凭证写进了命令 —— "
        "脚本已改走服务账号登录换令牌（`scripts/script_auth.py`），"
        "应示例 `--user <svc-账号>` ＋ `AGENT_SCRIPT_PASSWORD` 或 Bearer 令牌：\n"
        + "\n".join(offenders)
    )
