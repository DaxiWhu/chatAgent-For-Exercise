"""Grafana 面板的守卫。

为什么需要这一份
----------------
`deploy/monitoring/grafana/` 此前**只有 datasources、一个面板都没有**，而 RUNBOOK 的
上线自检第一步写的是"三层面板都看一眼"—— 文档指向了一个不存在的东西。
这类漂移**没有任何运行时信号**：只有值班的人真的去点开，才发现文件夹是空的，
而那时通常正在处理故障。

本文件钉住四件事，每一件都是"错了也不报错、只是悄悄失效"的类型：

1. provisioning 的 `options.path` 与磁盘目录对得上。写错时 Grafana 只记一行 error，
   界面上表现为"文件夹是空的"。
2. 面板引用的数据源 `uid` 在 datasources.yml 里定义。让 Grafana 自动生成 uid 的话，
   每次重建都会变，面板会集体报 `datasource not found`。
3. 面板引用的**指标名真实存在**。写错一个字母的指标，Prometheus 不会报错，
   面板只会**永远空白** —— 看起来和"最近没流量"一模一样。
4. RUNBOOK 里提到的面板标题真实存在（文档不许再指向空文件夹）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
GRAFANA = ROOT / "deploy" / "monitoring" / "grafana"
PROVISIONING = GRAFANA / "provisioning"
DATASOURCES = PROVISIONING / "datasources" / "datasources.yml"
PROVIDER = PROVISIONING / "dashboards" / "chatagent.yml"
DASH_JSON_DIR = PROVISIONING / "dashboards" / "json"
COMPOSE = ROOT / "deploy" / "app" / "docker-compose.yml"
RUNBOOK = ROOT / "deploy" / "RUNBOOK.md"

# provider 的 path 必须是容器内路径；compose 把这个目录挂成了这个前缀
CONTAINER_MOUNT = "/etc/grafana/provisioning"

# PromQL 的函数 / 聚合修饰符 / 运算符 —— 它们会以裸标识符出现在 expr 里，但不是指标
_PROMQL_KEYWORDS = {
    "rate", "irate", "sum", "min", "max", "count", "count_values", "avg",
    "histogram_quantile", "clamp_min", "clamp_max", "increase", "delta",
    "time", "by", "without", "on", "ignoring", "group_left", "group_right",
    "topk", "bottomk", "quantile", "abs", "ceil", "floor", "round", "stddev",
    "vector", "absent", "label_replace", "offset", "bool", "and", "or", "unless",
    "start", "end",
}
# Prometheus 注入 / 约定标签，以及本项目自己用的标签名
_KNOWN_LABELS = {
    "job", "instance", "le", "status", "path", "method", "kind", "scope",
    "phase", "target", "fstype", "mountpoint", "reason",
}

_IDENT_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b")
# 直方图的派生序列后缀
_HIST_SUFFIXES = ("_bucket", "_sum", "_count")

# 应用自产指标的命名前缀（与 app/metrics.py 的调用点一致）。
# 用它把"该由代码证明存在"的名字圈出来 —— 外部 exporter 的指标不适用这条。
_APP_PREFIXES = (
    "http_", "llm_", "chat_stream_", "client_", "review_", "quota_",
    "rag_", "rate_limited", "request_", "session_", "tenant_", "shared_backend_",
)


def _app_metric_names() -> set[str]:
    """扫 app/ 与 src/ 里所有 `inc/observe/set_gauge("名字")` 调用。"""
    names: set[str] = set()
    pat = re.compile(r'(?:inc|observe|set_gauge)\(\s*"([a-z_]+)"')
    for base in ("app", "src"):
        for py in (ROOT / base).rglob("*.py"):
            names |= set(pat.findall(py.read_text(encoding="utf-8")))
    return names


def _external_metric_names() -> set[str]:
    """非应用自产的指标：Prometheus 自身 / blackbox / node-exporter / 备份脚本。

    从 `prometheus.yml` 与 `alerts.yml` 里**实际出现过**的名字里收，而不是维护一份
    硬编码清单 —— 硬编码的清单会在新增 exporter 后过期，那时守卫开始假红，
    接着必然被跳过或删掉。宁可宽一点（漏判），也不要误红。
    """
    text = (ROOT / "deploy" / "monitoring" / "prometheus.yml").read_text(encoding="utf-8")
    text += (ROOT / "deploy" / "monitoring" / "alerts.yml").read_text(encoding="utf-8")
    # 不设长度下限：`up` 只有两个字符，但它是最常用的外部指标；
    # 白名单宽一点只会"漏判"，而下限会让它误红。
    return set(re.findall(r"\b([a-z][a-z0-9_]*)\b", text))


# 脚本自产指标的声明形式：`M_SUCCESS = "backup_drill_last_success_timestamp_seconds"`
_SCRIPT_METRIC_RE = re.compile(r'^\s*M_[A-Z0-9_]+\s*=\s*"([a-z][a-z0-9_]*)"', re.M)


def _script_metric_names() -> set[str]:
    """`scripts/` 里以**常量形式**声明的指标名。

    备份/演练指标既不由 app 产生，也不写在 prometheus.yml / alerts.yml 里，
    而是由 `scripts/drill_metrics.py` 发布 —— 它既不在 app/ 也不在 src/，
    前两个来源都收不到它。

    刻意只收 `M_XXX = "..."` 这个形式，而不是把 scripts 里所有小写标识符
    一股脑倒进来：后者会把 `os`、`path`、`rc` 之类的局部名放进白名单，
    于是面板里拼错的 `backup_drill_typo` 只要恰好是某个变量名就会被放行。
    """
    names: set[str] = set()
    for py in (ROOT / "scripts").glob("*.py"):
        names |= set(_SCRIPT_METRIC_RE.findall(py.read_text(encoding="utf-8")))
    return names


def _dashboards() -> list[dict]:
    out = []
    for path in sorted(DASH_JSON_DIR.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:  # 给一条能直接定位的报错
            raise AssertionError(f"{path.name} 不是合法 JSON：{exc}") from exc
    return out


def _exprs(db: dict):
    for panel in db.get("panels", []):
        for t in panel.get("targets", []):
            if t.get("expr"):
                yield panel, t["expr"]


def _base_metric(ident: str) -> str:
    """把 `xxx_bucket` 还原成 `xxx`（直方图的派生序列不是独立指标）。"""
    for suf in _HIST_SUFFIXES:
        if ident.endswith(suf):
            return ident[: -len(suf)]
    return ident


# --------------------------------------------------------------------------
# 1) provisioning 与挂载点
# --------------------------------------------------------------------------
def test_compose_mounts_the_provisioning_directory():
    """compose 必须把 grafana/provisioning 挂到容器的 CONTAINER_MOUNT。"""
    text = COMPOSE.read_text(encoding="utf-8")
    assert re.search(
        r"\.\./monitoring/grafana/provisioning:\s*" + re.escape(CONTAINER_MOUNT), text
    ), (
        f"compose 没有把 grafana/provisioning 挂到 {CONTAINER_MOUNT} —— "
        f"provider 里写的容器内路径就指向了不存在的位置，面板一个都加载不出来"
    )


def test_provider_path_points_at_a_directory_that_exists():
    provider = yaml.safe_load(PROVIDER.read_text(encoding="utf-8"))
    paths = [p.get("options", {}).get("path") for p in provider.get("providers", [])]
    assert paths and all(paths), f"{PROVIDER.name} 里没有 options.path"

    for p in paths:
        assert p.startswith(CONTAINER_MOUNT + "/"), (
            f"provider 的 path={p!r} 不在 {CONTAINER_MOUNT} 下 —— "
            f"那它指向的是容器里没挂载的位置"
        )
        rel = p[len(CONTAINER_MOUNT) + 1 :]
        assert (PROVISIONING / rel).is_dir(), (
            f"provider 指向 {p}，但仓库里没有 {PROVISIONING.relative_to(ROOT)}/{rel} —— "
            f"Grafana 只会记一行 error，界面上表现为『文件夹是空的』"
        )


# --------------------------------------------------------------------------
# 2) 面板结构
# --------------------------------------------------------------------------
def test_there_are_dashboards_at_all():
    """最少四个 —— RUNBOOK §1.2 的上线自检就是按这四个写的。"""
    dbs = _dashboards()
    assert len(dbs) >= 4, (
        f"只有 {len(dbs)} 个面板 JSON；RUNBOOK 的上线自检要求四个"
        f"（服务总览 / LLM 与成本 / 限流配额与安全 / 平台自身）"
    )


def test_every_dashboard_is_wellformed():
    seen_uids: set[str] = set()
    for db in _dashboards():
        title = db.get("title")
        assert title, f"{db.get('uid')} 缺少 title"
        assert db.get("uid"), f"{title} 缺少 uid"
        assert db["uid"] not in seen_uids, f"面板 uid 重复：{db['uid']}"
        seen_uids.add(db["uid"])
        panels = db.get("panels")
        assert panels, f"{title} 一个 panel 都没有"

        for panel in panels:
            assert panel.get("title"), f"{title} 里有 panel 没有 title"
            exprs = [t.get("expr") for t in panel.get("targets", []) if t.get("expr")]
            assert exprs, f"{title} / {panel['title']} 没有任何查询（会是一块空白）"
            assert all(e.strip() for e in exprs), (
                f"{title} / {panel['title']} 有空查询"
            )


def test_panel_datasource_uids_are_defined():
    defined = {
        d.get("uid")
        for d in yaml.safe_load(DATASOURCES.read_text(encoding="utf-8"))["datasources"]
    }
    assert all(defined), (
        f"datasources.yml 里有数据源没写 uid：{defined} —— "
        f"让 Grafana 自动生成 uid 的话，容器重建后面板会集体报 datasource not found"
    )
    for db in _dashboards():
        for panel in db["panels"]:
            uid = (panel.get("datasource") or {}).get("uid")
            assert uid in defined, (
                f"{db['title']} / {panel['title']} 引用了未定义的数据源 uid={uid!r}；"
                f"datasources.yml 定义的是 {sorted(defined)}"
            )


# --------------------------------------------------------------------------
# 3) 指标必须真实存在（写错不会报错，只会永远空白）
# --------------------------------------------------------------------------
def test_panel_app_metrics_actually_exist():
    """长得像应用自产指标的，必须在代码里真的被 inc / observe 过。"""
    real = _app_metric_names()
    assert real, "没从 app/ 或 src/ 扫到任何指标名 —— 守卫本身失效了"

    bad: list[str] = []
    for db in _dashboards():
        for panel, expr in _exprs(db):
            for ident in set(_IDENT_RE.findall(expr)):
                if not ident.startswith(_APP_PREFIXES):
                    continue
                if _base_metric(ident) not in real:
                    bad.append(f"{db['title']} / {panel['title']}：{ident}")
    assert not bad, (
        "面板引用了代码里不存在的指标（Prometheus 不会报错，面板只会永远空白，"
        "看起来和『最近没流量』一模一样）：\n  " + "\n  ".join(sorted(set(bad)))
    )


def test_panel_identifiers_are_all_known():
    """expr 里的每个标识符要么是指标、要么是 PromQL 关键字、要么是标签名。"""
    allowed = (
        _app_metric_names()
        | _external_metric_names()
        | _script_metric_names()
        | _PROMQL_KEYWORDS
        | _KNOWN_LABELS
    )
    bad: list[str] = []
    for db in _dashboards():
        for panel, expr in _exprs(db):
            for ident in set(_IDENT_RE.findall(expr)):
                if ident in allowed or _base_metric(ident) in allowed:
                    continue
                bad.append(f"{db['title']} / {panel['title']}：{ident}")
    assert not bad, (
        "面板 expr 里出现了无法识别的标识符（多半是拼错的指标名）：\n  "
        + "\n  ".join(sorted(set(bad)))
    )


# --------------------------------------------------------------------------
# 4) 文档不许指向不存在的面板
# --------------------------------------------------------------------------
def test_runbook_mentions_only_existing_dashboards():
    titles = {db["title"] for db in _dashboards()}
    text = RUNBOOK.read_text(encoding="utf-8")
    # 标题里有空格（`chatAgent · LLM 与成本`），所以要取到表格分隔符 / 行尾 / 反引号为止
    mentioned = {
        m.strip() for m in re.findall(r"chatAgent · [^|`\n]+", text)
    }
    assert mentioned, (
        "RUNBOOK 里一个面板标题都没提到 —— 上线自检的第一步就是『看一眼面板』，"
        "不写具体名字等于没写"
    )
    unknown = sorted(m for m in mentioned if m not in titles)
    assert not unknown, (
        f"RUNBOOK 提到了不存在的面板：{unknown}；实际存在的是 {sorted(titles)}"
    )
