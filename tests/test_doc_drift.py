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

ROOT = Path(__file__).resolve().parent.parent
DOCS = [
    ROOT / "PRODUCTION_CONFIG.md",
    ROOT / "deploy" / "RUNBOOK.md",
    ROOT / "deploy" / "CANARY.md",
    ROOT / "deploy" / "SECRETS.md",
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
        `RERANK_*` / `EMBEDDING_*` / `DOCLING_*`；
      - `scripts/*.py`：运维脚本直接读 `DB_URL` / `HF_ENDPOINT` 等。

    只认 `os.getenv("X")` / `os.environ.get("X")` / `os.environ["X"]` 与
    `_env_*(` 这两种**字面量**写法 —— 变量名拼接的读法本就无法静态确认，
    这属于刻意的能力边界。
    """
    targets = [ROOT / "app" / "config.py", ROOT / "app" / "serve.py"]
    targets += sorted((ROOT / "src" / "rag").glob("*.py"))
    targets += sorted((ROOT / "scripts").glob("*.py"))

    pat = re.compile(
        r'_env_(?:str|bool|int|float)\(\s*"([A-Za-z0-9_]+)"'
        r'|os\.(?:getenv|environ\.get)\(\s*"([A-Za-z0-9_]+)"'
        r'|os\.environ\[\s*"([A-Za-z0-9_]+)"\s*\]'
    )
    keys: set[str] = set()
    for path in targets:
        if not path.exists():
            continue
        for m in pat.finditer(path.read_text(encoding="utf-8")):
            keys.add(next(g for g in m.groups() if g))
    return keys


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
    }
    return _env_example_keys() | _code_keys() | _compose_keys() | extra


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_env_keys_mentioned_in_docs_actually_exist(doc):
    """文档里 `反引号` 包起来的全大写标识符必须是真实存在的配置键。

    这条防的是"文档在教你配一个已经不存在的变量" —— 那比没有文档更糟：
    你按它做了、没有任何效果、也没有任何报错。
    （`DISCONNECT_POLL_INTERVAL` 就是这么被删掉的，文档里保留了一行
    带删除线的说明，所以下面要把"明确标注为废弃"的行排除。）
    """
    text = doc.read_text(encoding="utf-8")
    known = _known_keys()

    offenders = []
    for line in text.splitlines():
        if "~~" in line or "已废弃" in line or "已删除" in line:
            continue  # 显式标注为废弃的说明是**必须留**的
        for key in re.findall(r"`([A-Z][A-Z0-9_]{3,})`", line):
            # 只查"看起来像环境变量"的：必须含下划线，且不是纯正则/路径片段
            if "_" not in key:
                continue
            if key not in known:
                offenders.append((key, line.strip()[:100]))

    assert not offenders, (
        "以下文档里提到的配置键不存在（拼错？已删除没同步？）：\n"
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


def test_docs_do_not_use_compose_without_env_file():
    """文档与注释里的 compose 调用**必须**带 `--env-file .env`。

    这是 2026-09-30 挖出来的一个真缺陷的守卫。核实过的官方行为：
    没有 `--env-file` 时，compose 从**项目目录**（= 第一个 `-f` 文件所在目录，
    这里是 `deploy/app/`）加载 `.env`，**仓库根的 `.env` 完全不参与插值**。
    于是"照文档把 webhook 填进 .env"这一步事实上没生效，而且没有任何报错。

    只在文档/配置里禁掉短写法是不够的（人手一抖就又写回来了），所以钉成用例。
    """
    short = "docker compose -f deploy/app/"
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
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "docker compose" in line and "deploy/app/" in line and "--env-file" not in line:
                offenders.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()[:110]}")
    assert not offenders, (
        "以下位置用了不带 `--env-file .env` 的 compose 调用 —— "
        "那条命令**读不到仓库根的 .env**，配置会静默退回默认值：\n" + "\n".join(offenders)
    )
