"""监控配置的一致性校验。

为什么需要这个测试
------------------
监控配置的错误是**静默的**：YAML 语法对、Prometheus 能启动，但告警送不到人。
最典型的几种：

  1. 路由里引用了不存在的接收人（写错名字/拼错 `receivers` 关键字）——
     Alertmanager 启动时未必报错，触发时才发现没人收到；
  2. 规则没有可路由的标签 —— 只有 `severity: critical`，导致"安全告警"和
     "依赖故障"进同一个接收人，处置方式完全不同却混在一起；
  3. `prometheus.yml` 没配 `alerting.alertmanagers` —— 规则能 firing，
     但事件从不外发（面板上看着一切正常，这是最误导的一种）；
  4. **SLO 文档与表达式脱节** —— `SLO.md` 写着 14.4 倍燃尽，表达式里却是别的数；
     或表达式里正则是 `le="1"` 而指标实际渲染 `le="1.0"`（全串匹配 ⇒ **永不触发**
     且没有任何报错）。这类错误没有任何运行时信号，只能靠断言钉住。
  5. **接收人方向只查了一半** —— "路由指向不存在的接收人"和"接收人没有被任何路由
     指向"是同一件事的两个方向：前者丢告警，后者是**死出口**（新增了接收人却忘了那条
     路由，配置看起来"该有的都有"）。同理，分流其实有**两处**（Alertmanager 的 route
     选 receiver、网关的 `ALERT_SINK_ROUTES` 选通道），网关对表里没有的 receiver 会
     静默退回 `default` —— 只对了一处，就会得出"我已经分好了"的错误结论。

这些都无法靠"人能记住"来防（改配置的人通常只关心自己那条规则），所以固化成断言。
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
MON = ROOT / "deploy" / "monitoring"


@pytest.fixture(scope="module")
def alerts_cfg():
    return yaml.safe_load((MON / "alerts.yml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def am_cfg():
    return yaml.safe_load((MON / "alertmanager.yml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def prom_cfg():
    return yaml.safe_load((MON / "prometheus.yml").read_text(encoding="utf-8"))


def _all_rules(alerts_cfg):
    for group in alerts_cfg["groups"]:
        for rule in group["rules"]:
            yield group["name"], rule


# ---------------------------------------------------------------------------
# alerts.yml
# ---------------------------------------------------------------------------
def test_every_rule_has_category_and_severity(alerts_cfg):
    """每条规则都必须带 category —— 否则它落不进任何一条路由。

    只有 severity 时，"安全告警"与"可用性告警"会挤进同一个接收人，
    而这两者的处置方式完全不同（一个要追攻击来源、一个要查依赖健康）。
    """
    bad = [rule["alert"] for _, rule in _all_rules(alerts_cfg)
           if not rule.get("labels", {}).get("category")]
    assert not bad, f"以下规则缺少 category 标签，无法路由：{bad}"


def test_every_rule_has_runbook(alerts_cfg):
    """告警最怕的不是响，而是响了没人知道下一步做什么。"""
    bad = [rule["alert"] for _, rule in _all_rules(alerts_cfg)
           if not rule.get("annotations", {}).get("runbook")]
    assert not bad, f"以下规则缺少 runbook：{bad}"


def test_every_rule_has_summary_and_description(alerts_cfg):
    bad = [rule["alert"] for _, rule in _all_rules(alerts_cfg)
           if not rule.get("annotations", {}).get("summary")
           or not rule.get("annotations", {}).get("description")]
    assert not bad, f"以下规则缺少 summary/description：{bad}"


def test_alert_names_are_unique(alerts_cfg):
    names = [rule["alert"] for _, rule in _all_rules(alerts_cfg)]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"告警名重复：{dupes}"


def test_rule_groups_declare_expected_categories(alerts_cfg):
    """组名与内部规则的 category 必须对得上。

    防的是"复制粘贴一条规则到别的组、忘了改 category"——
    那样它会被路由到错误的接收人，且看不出来。
    """
    for group in alerts_cfg["groups"]:
        expected = group["name"].replace("chatagent-", "")
        actual = {r["labels"]["category"] for r in group["rules"]}
        assert actual == {expected}, (
            f"组 {group['name']} 内规则的 category 为 {actual}，与组名不符"
        )


# ---------------------------------------------------------------------------
# alertmanager.yml
# ---------------------------------------------------------------------------
def test_alertmanager_uses_correct_top_level_keys(am_cfg):
    """顶层键拼写必须正确。

    这里防的是一类真实踩过的错：把 `receivers:` 写成 `recievers:`。
    YAML 语法完全合法（只是多了一个未知键），所以**解析不会报错** ——
    但 Alertmanager 读不到任何接收人，路由全部失效。
    """
    allowed = {"global", "route", "receivers", "inhibit_rules",
               "templates", "time_intervals", "alertmanager"}
    unknown = set(am_cfg) - allowed
    assert not unknown, f"alertmanager.yml 出现未知顶层键（拼写错误？）：{unknown}"
    for required in ("route", "receivers"):
        assert required in am_cfg, f"缺少必需顶层键：{required}"


def _receiver_names(am_cfg) -> set[str]:
    return {r["name"] for r in am_cfg["receivers"]}


def _walk_routes(route) -> list[dict]:
    out = [route]
    for child in route.get("routes") or []:
        out.extend(_walk_routes(child))
    return out


def test_every_route_references_an_existing_receiver(am_cfg):
    """路由指向的接收人必须存在。

    引用不存在的接收人是 Alertmanager 最常见的静默故障：
    告警触发 → 路由匹配 → 找不到接收人 → 什么都发不出去。
    """
    names = _receiver_names(am_cfg)
    for route in _walk_routes(am_cfg["route"]):
        assert route["receiver"] in names, (
            f"路由引用了不存在的接收人：{route['receiver']}（已定义：{sorted(names)}）"
        )


def test_default_route_receiver_exists(am_cfg):
    """根路由的兜底接收人必须存在。

    兜底一旦失效，"漏配某条子路由"的表现就是**安静地丢告警** —— 最难发现的一类。
    """
    assert am_cfg["route"]["receiver"] in _receiver_names(am_cfg)


def test_every_declared_receiver_is_reachable(am_cfg):
    """已声明的接收人必须至少被一条路由指向 —— 否则它的出口是死的。

    这是"路由引用了不存在的接收人"（上面那条）的**镜像**，同样静默、方向相反：
    有人新增了一个接收人、打算把某类告警引过去，却忘了（或写错了）那条路由。
    接收人本身合法、Alertmanager 启动正常、配置看起来"该有的都有"，
    而那个出口**永远收不到任何东西**。

    只有把"声明的"与"被路由用到的"两个集合相减才看得出来 ——
    靠人 review 时，多出来一个接收人不会显得可疑（它看起来正是"刚加好的"）。
    """
    declared = _receiver_names(am_cfg)
    routed = {r["receiver"] for r in _walk_routes(am_cfg["route"])}
    orphans = sorted(declared - routed)
    assert not orphans, (
        f"这些接收人已声明，但没有任何路由指向它们：{orphans}\n"
        "它们的出口是死的（永远收不到告警），而配置看起来完全正常。"
    )


def test_all_categories_are_routed(alerts_cfg, am_cfg):
    """alerts.yml 里出现的每个 category，alertmanager.yml 都必须有对应路由。

    漏一个的后果：那类告警全部落进兜底接收人。看起来"也收到了"，
    但值班的人错了 —— 安全告警发给了管预算的人。
    """
    categories = {rule["labels"]["category"] for _, rule in _all_rules(alerts_cfg)}
    routed: set[str] = set()
    for route in _walk_routes(am_cfg["route"]):
        for m in route.get("matchers") or []:
            # 形如 'category = "security"'
            if m.strip().startswith("category"):
                routed.add(m.split("=", 1)[1].strip().strip('"').strip("'"))
    missing = categories - routed
    assert not missing, f"以下 category 没有专属路由，会落进兜底接收人：{missing}"


def test_security_category_is_routed_without_long_wait(am_cfg):
    """安全告警不能有长时间的 group_wait。

    别的告警攒一攒是降噪，安全告警攒一攒是**给攻击者留时间**。
    """
    for route in _walk_routes(am_cfg["route"]):
        if any(m.strip().startswith("category") and "security" in m
               for m in route.get("matchers") or []):
            wait = route.get("group_wait", "0s")
            secs = float(str(wait).rstrip("smh") or 0)
            if str(wait).endswith("m"):
                secs *= 60
            elif str(wait).endswith("h"):
                secs *= 3600
            assert secs <= 60, f"安全告警的 group_wait 过长（{wait}），会延迟响应"
            return
    pytest.fail("没有为 security 类别定义专属路由")


def test_inhibit_rules_reference_known_alerts(alerts_cfg, am_cfg):
    """抑制规则里的 source/target 必须是真实存在的告警名（或合法的通配）。

    写错一个告警名的后果是"抑制不生效"，而抑制不生效平时看不出来 ——
    只在真正出事、告警风暴涌来时暴露。
    """
    known = {rule["alert"] for _, rule in _all_rules(alerts_cfg)}
    for rule in am_cfg.get("inhibit_rules") or []:
        for key in ("source_matchers", "target_matchers"):
            for m in rule.get(key) or []:
                if "alertname" not in m:
                    continue
                value = m.split("=", 1)[1].strip().strip('"').strip("'")
                if not any(ch in value for ch in "*?[]"):
                    assert value in known, (
                        f"抑制规则引用了不存在的告警名：{value}"
                    )


def test_inhibit_rules_scope_by_instance(am_cfg):
    """抑制规则必须声明 `equal` 含 instance，避免跨副本误抑制。

    机制说明（容易写错的地方）：限定副本靠的是 `equal: ["instance"]` ——
    Alertmanager 要求 source 与 target 的该标签**取值相同**才抑制。
    它**不需要**在 source_matchers 里再写一遍 `instance`（写了反而变成
    "只对某个具体实例生效"）。漏掉 equal 的后果是：一台机器挂掉，
    把另一台**独立**出问题的告警一起吃掉 —— 等于把真实故障藏起来。
    """
    rules = am_cfg.get("inhibit_rules") or []
    assert rules, "没有任何抑制规则，故障时会引发告警风暴"
    for rule in rules:
        assert "instance" in (rule.get("equal") or []), (
            f"抑制规则未按 instance 限定（缺 equal: [instance]）：{rule}"
        )


# ---------------------------------------------------------------------------
# prometheus.yml
# ---------------------------------------------------------------------------
def test_alerting_section_points_at_alertmanager(prom_cfg):
    """prometheus.yml 必须把事件发给 Alertmanager。

    缺了 `alerting.alertmanagers` 时规则照样会 firing（面板上看得见），
    但**事件从不外发** —— 这是最容易误判成"已经通知过了"的一种状态。
    """
    targets = [t for am in prom_cfg.get("alerting", {}).get("alertmanagers", [])
               for sc in am.get("static_configs", [])
               for t in sc.get("targets", [])]
    assert targets, "prometheus.yml 未配置 alerting.alertmanagers，告警不会外发"


def test_prometheus_loads_the_alert_rules(prom_cfg):
    files = prom_cfg.get("rule_files") or []
    assert any("alerts.yml" in f for f in files), "prometheus.yml 未加载 alerts.yml"


# 本仓库自带的告警网关服务名（compose 里的 alert-sink）。
ALERT_SINK_HOST = "alert-sink"


def test_receiver_urls_are_placeholder_or_the_bundled_sink(am_cfg):
    """每个 webhook URL 要么指向本仓库自带的网关，要么显式标注为待替换。

    这条断言防的是同一类故障的两种形态：

      1. **指向一个不存在的主机**（填了内网地址但没人部署那个服务）——
         告警触发后 Alertmanager 连接失败、重试、放弃，只有它的日志里有痕迹，
         Prometheus 面板上完全看不出来。这是"以为已经通知过了"的典型来源。
      2. **凭据硬编码** —— Alertmanager 配置**不支持环境变量插值**，所以
         真实 webhook 地址（含 key）一旦写进来就会进版本库。

    因此合法形态只有两种：指向 `alert-sink`（本仓库的网关，凭据在它的
    环境变量里，不进版本库），或明确写成 `REPLACE-ME`（表示这个部署还没配接收端）。
    两者都不满足时，说明有人填了"看起来像真实地址"的东西，要人工确认。
    """
    cfgs = [c for r in am_cfg["receivers"] for c in (r.get("webhook_configs") or [])]
    assert cfgs, "没有定义任何 webhook 接收人"
    for c in cfgs:
        url = c["url"]
        assert ALERT_SINK_HOST in url or "REPLACE-ME" in url, (
            f"接收人 URL 既不是本仓库的告警网关（{ALERT_SINK_HOST}），也没有替换标记：{url}\n"
            "若确实已换成真实地址，请改用 alert-sink 并在其环境变量里配凭据。"
        )


def test_receiver_url_suffix_matches_receiver_name(am_cfg):
    """URL 末段必须等于 receiver 名 —— 这是 alert_sink.py 的路由契约。

    网关按 `URL 末段` 去查 `ALERT_SINK_ROUTES` 决定用哪个通道。写错一个字母的
    后果是"该 receiver 的告警只落进网关日志、不发出去"，**且没有任何报错**
    （未知 key 会落到 default 兜底，或者干脆没有通道）。这类错误只能靠断言钉住。
    """
    for r in am_cfg["receivers"]:
        for c in (r.get("webhook_configs") or []):
            url = c["url"]
            if "REPLACE-ME" in url:
                continue
            suffix = url.rstrip("/").rsplit("/", 1)[-1]
            assert suffix == r["name"], (
                f"receiver {r['name']!r} 的 URL 末段是 {suffix!r}，与 receiver 名不符；"
                "网关会把它路由到错误的通道（或落进 default），且不报错。"
            )


# ---------------------------------------------------------------------------
# 两处分流的第二处：网关的 ALERT_SINK_ROUTES
# ---------------------------------------------------------------------------
SINK_PATH = MON / "alert_sink.py"
ENV_EXAMPLE = ROOT / ".env.example"
SINK_ROUTES_KEY = "ALERT_SINK_ROUTES"


def _sink_routes() -> dict[str, str]:
    """用网关**自己的** `parse_routes` 解析 `.env.example` 里的分流表。

    刻意不在这里另写一份解析：两套解析迟早对不上，而"对不上"的表现恰好就是
    这条守卫要防的静默误投。`.env.example` 是随仓库发布的契约值
    （真实 `.env` 不入库，也不该被测试读 —— 那会让测试结果依赖本机环境）。

    `deploy/` 不是包，所以按路径加载（与 `tests/test_alert_sink.py` 同一手法）。
    """
    spec = importlib.util.spec_from_file_location("alert_sink", SINK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    m = re.search(rf"(?m)^{SINK_ROUTES_KEY}=(.*)$", text)
    assert m, f".env.example 里没有 {SINK_ROUTES_KEY} —— 网关的分流表没有契约来源"
    return mod.parse_routes(m.group(1).strip())


def test_every_sink_receiver_has_an_explicit_channel_route(am_cfg):
    """经网关投递的每个接收人，都必须在 `ALERT_SINK_ROUTES` 里**显式**给出通道。

    分流有**两处**：Alertmanager 的 route（决定"找谁"）与网关的分流表
    （决定"用哪个通道"）。只看前者会得出"我已经按类目分好了"的错误结论 ——
    分流表若漏了某个 receiver，`channel_for()` 会退回 `default` 兜底**且不报错**，
    于是"安全告警"安静地进了 oncall 群，而两份配置看起来都对。

    反方向同样要查：分流表里的键必须都对应真实接收人。否则那是接收人改过名之后的
    残留 —— 改名后的那个接收人现在会静默退回 `default`，同样没有任何报错。
    """
    routes = _sink_routes()
    via_sink = {
        r["name"]
        for r in am_cfg["receivers"]
        for c in (r.get("webhook_configs") or [])
        if ALERT_SINK_HOST in c["url"]
    }
    missing = sorted(via_sink - set(routes))
    assert not missing, (
        f"这些接收人经 {ALERT_SINK_HOST} 投递，但 {SINK_ROUTES_KEY} 里没有它们：{missing}\n"
        "网关会把它们**静默**退回 default 通道 —— 收到告警的人错了，且不会有任何报错。"
    )
    stale = sorted(set(routes) - _receiver_names(am_cfg))
    assert not stale, (
        f"{SINK_ROUTES_KEY} 里有不对应任何接收人的键：{stale}\n"
        "多半是接收人改过名而分流表没跟着改 —— 那个接收人现在会静默退回 default。"
    )


# ---------------------------------------------------------------------------
# SLO / 错误预算（alerts.yml 的 chatagent-slo 组 + SLO.md）
# ---------------------------------------------------------------------------
SLO_DOC = MON / "SLO.md"


@pytest.fixture(scope="module")
def slo_rules(alerts_cfg):
    """所有 `category: slo` 的规则（按告警名索引）。"""
    return {rule["alert"]: rule for _, rule in _all_rules(alerts_cfg)
            if rule.get("labels", {}).get("category") == "slo"}


# SLO 规则分两个**家族**，靠 `kind` 标签区分（`category: slo` 只说明它属于哪个组）：
#   burn   —— 燃尽率（**速率**问法）：形状固定为"长窗口 + 短窗口 + 倍数"，
#             能与 SLO.md 的 8 列表格逐行对齐；
#   budget —— 预算余量（**余量**问法）：读录音序列（`recording.yml`），
#             窗口、阈值、严重度的形状与 burn 完全不同，没法套同一张表。
# 为什么非要这个标签：下面的断言要按家族分开。没有它时，新加的一条规则会从
# **所有**分类断言里漏出去（既不进燃尽率表的对齐，也不进余量表），而在这里
# "漏出去"就等于"没有任何守卫看着它" —— 所以下面有一条守卫专门要求每条都声明 `kind`。
_BURN, _BUDGET = "burn", "budget"

#: 预算余量的录音序列名。**唯一**一处定义，测试与文档都以它为准。
BUDGET_METRIC = "slo:budget_remaining_ratio"


def _of_kind(rules: dict, kind: str) -> dict:
    return {n: r for n, r in rules.items()
            if (r.get("labels") or {}).get("kind") == kind}


def _series_in(expr: str) -> set[str]:
    """表达式里出现的**录音序列名**，按完整的 `slo:` 标识符取。

    ⚠️ 不能用 `name in expr` 判"读了哪条序列"：`slo:budget_remaining_ratio` 是
    `slo:budget_remaining_ratioo` 的**子串** —— 拼错一个字母时 `in` 照样为真，
    于是"序列名写错 ⇒ 匹配空序列 ⇒ 规则永远 inactive"这类缺陷完全扫不到。
    变异验证实测过：用 `in` 时，"把序列名打错一个字母"只有按 token 取的那条守卫会红。
    （同一个坑在 `tests/test_runtime_metrics.py::test_every_new_metric_is_emitted_in_source`
    里已经踩过一次，那里记的是'指标名必须带引号才算数'。）
    """
    return set(re.findall(r"\b(slo:[a-z_0-9:]+)", expr))


BURN_COLS = ["alert", "slo", "target", "burn", "long", "short", "budget", "severity"]
#: ⚠️ 列数**必须与燃尽率表（8 列）不同**：`_doc_table` 是按列数识别表格的，
#: 两张表同列数时会互相污染 —— 燃尽率表的 schema 会跑去解析余量表的行。
BUDGET_COLS = ["alert", "metric", "window", "threshold", "slo", "severity"]


def _secs(dur: str) -> int:
    """把 Prometheus 的窗口字面量（`30s` / `5m` / `6h` / `3d`）换算成秒。"""
    m = re.fullmatch(r"(\d+)([smhd])", str(dur).strip())
    assert m, f"无法解析的时长：{dur!r}"
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return int(m.group(1)) * mult


def _windows(expr: str) -> list[str]:
    """取出表达式里所有窗口字面量（`[1h]` → `1h`）。"""
    return re.findall(r"\[(\d+[smhd])\]", expr)


def _doc_table(text: str, cols: list[str]) -> list[dict]:
    """解析 SLO.md 里**列数等于 `len(cols)`** 的表格。

    为什么靠"列数 + 表头"识别而不是靠标题定位：改标题不该让测试失效，
    但把表格列删一列必须被发现（那意味着某条口径没写下来）。
    """
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|") or not s.endswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) != len(cols):
            continue
        if cells[0] in ("告警", cols[0]) or set(cells[0]) <= set("-: "):
            continue
        out.append(dict(zip(cols, cells, strict=False)))
    return out


def _burn_rows() -> list[dict]:
    return _doc_table(SLO_DOC.read_text(encoding="utf-8"), BURN_COLS)


def _budget_rows() -> list[dict]:
    return _doc_table(SLO_DOC.read_text(encoding="utf-8"), BUDGET_COLS)


def test_slo_doc_exists():
    assert SLO_DOC.exists(), "缺少 SLO.md —— SLO 不该只存在于 YAML 表达式里"


def test_slo_doc_matches_alert_expressions(slo_rules):
    """SLO.md 表格里的每一行，都必须能在 `alerts.yml` 的表达式里对上。

    这是防"文档与实现脱节"的核心断言：只改表达式不改文档（或反过来）立刻测红。
    断言三件事：燃尽率倍数、SLO 目标、长短两个窗口。
    """
    rows = _burn_rows()
    assert rows, "SLO.md 里没有解析到燃尽率表（表头/列数变了？）"
    burn = _of_kind(slo_rules, _BURN)
    assert burn, "alerts.yml 里没有任何 kind: burn 的规则 —— 家族划分被改掉了？"

    for row in rows:
        name = row["alert"]
        assert name in burn, (
            f"SLO.md 的燃尽率表里提到了 {name}，但它不是一条 `kind: burn` 的规则："
            f"{'alerts.yml 里没有这条规则' if name not in slo_rules else '它的 kind 是 ' + repr((slo_rules[name].get('labels') or {}).get('kind'))}"
        )
        expr = burn[name]["expr"]

        want = f"> {row['burn']} * (1 - {row['target']})"
        assert want in expr, (
            f"{name} 的表达式与 SLO.md 不符：文档说燃尽率 {row['burn']}、"
            f"目标 {row['target']}，表达式里却找不到 {want!r}"
        )
        for key in ("long", "short"):
            w = row[key]
            assert f"[{w}]" in expr, (
                f"{name} 的表达式缺少 SLO.md 声明的{key}窗口 [{w}]"
            )


def test_slo_doc_covers_every_slo_alert(slo_rules):
    """反向：`alerts.yml` 里每条 SLO 规则都必须写进 SLO.md。

    少了这条，新增一条燃尽率告警却忘了写文档就会漏过 ——
    而值班的人遇到没文档的告警只能靠猜。

    ⚠️ 两个家族各有自己的表（燃尽率表 8 列、预算余量表 6 列）。
    只查其中一张时，另一族的新规则会**静默漏过** ——
    所以这里要求落在两张表的**并集**里，而不是只对燃尽率表。
    """
    documented = {r["alert"] for r in _burn_rows()} | {r["alert"] for r in _budget_rows()}
    missing = set(slo_rules) - documented
    assert not missing, (
        f"以下 SLO 规则没有写进 SLO.md（既不在燃尽率表也不在预算余量表里）：{sorted(missing)}"
    )


def test_slo_rules_use_multi_window_burn_rate(slo_rules):
    """每条 SLO 规则都必须是"长 + 短"两个窗口，且用 `and` 连接。

    单窗口燃尽率告警是这一族里最常见的写错方式，而它在语法上完全合法：
      - 只留长窗口 ⇒ 已过去的尖峰让告警继续挂很久（恢复慢、误报长）；
      - 只留短窗口 ⇒ 30 秒抖动就能把人叫醒（噪音，很快被忽略）。
    两者都必须能区分开，所以断言"窗口数 >= 2 **且** 表达式里有 and"。
    ⚠️ 只适用于 `kind: burn` 这一族。预算余量规则（`kind: budget`）**本来就只有
    一个窗口** —— 它要回答的正是"这一个月下来还剩多少"，拆成长短两个窗口反而
    回到速率问法上去了（见 SLO.md §4 与 §4b）。
    """
    burn = _of_kind(slo_rules, _BURN)
    assert burn, "没有任何 kind: burn 的规则"
    for name, rule in burn.items():
        wins = _windows(rule["expr"])
        assert len(set(wins)) >= 2, (
            f"{name} 只用了 {set(wins)} 一个窗口 —— 单窗口燃尽率告警不是多窗口多燃尽率"
        )
        assert re.search(r"\band\b", rule["expr"]), (
            f"{name} 没有用 and 把长短两个窗口连接起来（长窗口单独成立不算）"
        )


def test_slo_burn_alerts_do_not_stack_a_long_for(slo_rules):
    """`for` 不得超过短窗口 —— 否则窗口本身的平滑被白等一遍。

    窗口已经承担了"持续多久才算"的职责，再叠一个 >= 短窗口的 `for`
    只会让告警更晚、更钝；而 `for` 为 0 又容易在单次抓取抖动上误报。
    所以要求落在 (0, 短窗口] 这个区间。

    只适用于 `kind: burn`：余量规则的 `for` 与窗口没有这种关系（窗口在录音规则里）。
    """
    for name, rule in _of_kind(slo_rules, _BURN).items():
        wins = sorted({_secs(w) for w in _windows(rule["expr"])})
        short = wins[0]
        hold = _secs(rule.get("for", "0s"))
        assert 0 < hold <= short, (
            f"{name} 的 for={rule.get('for')} 不在 (0, {short}s] 内："
            "要么白等一遍窗口，要么完全靠抖动触发"
        )


def test_slo_numerator_does_not_use_unhandled_exception_counter(slo_rules, recording_cfg):
    """可用性 SLI 的分子不得用 `http_errors_total`。

    它只在**未捕获异常**时自增（见 `app/middleware.py`），503 这类
    "正常返回的失败"不计入 —— 拿它当分子会系统性低估错误率，
    SLO 会**永远显示达标**，这是最危险的一种假绿。

    ⚠️ 必须**两个文件一起扫**：接上录音规则之后，预算那一族的分子搬进了
    `recording.yml`（`slo:bad_rate_5m`）。只扫 alerts.yml 时，
    "有人在那条录音规则里改用 http_errors_total"完全不会被发现 ——
    而这与本条要防的是**同一个错**，只是换了个文件、换了一族。
    """
    bad = [name for name, rule in slo_rules.items()
           if "http_errors_total" in rule["expr"]]
    bad += [f"recording.yml:{r['record']}"
            for g in recording_cfg["groups"] for r in g["rules"]
            if "http_errors_total" in r.get("expr", "")]
    assert not bad, (
        f"以下 SLO 规则用 http_errors_total 当分子：{bad}；"
        "应改用 http_requests_total{status=~\"5..\"}"
    )


# ---------------------------------------------------------------------------
# 录音规则（recording.yml）与「预算余量」这一族
#
# 背景：`ERROR_BUDGET_POLICY.md` 的政策表里有两行是**余量**口径（剩余 < 20% 冻结 /
# 耗尽全面冻结），但 `chatagent-slo` 组里 5 条规则全是**速率**问法，问不出"还剩多少"。
# 2026-10-03 起补上：录音规则算余量、两条告警读它。
#
# 这一节钉的都是"错了也不报错"的类型 —— 录音规则最典型的失败模式是**哑弹**：
# 没被加载 / 序列名写错 / 规则报错，三种都表现为"告警永远不触发"，
# 看起来就像"预算一直很健康"。
# ---------------------------------------------------------------------------
RECORDING = MON / "recording.yml"

#: Prometheus 的 `--query.lookback-delta` 默认值（5m）。录音规则的产物按同一套
#: lookback 规则被查询，所以它的间隔必须远小于这个数（理由见下面那条用例）。
LOOKBACK_SECS = 300


@pytest.fixture(scope="module")
def recording_cfg():
    return yaml.safe_load(RECORDING.read_text(encoding="utf-8"))


def test_every_slo_rule_declares_its_kind(slo_rules):
    """每条 `category: slo` 的规则都必须声明 `kind`（burn / budget）。

    这是**元守卫**：下面所有按家族分开的断言都靠 `kind` 过滤。某条规则若没打这个
    标签，它会从**每一族**里同时漏出去 —— 既不被燃尽率的形状断言看住，
    也不被余量表的文档对齐看住，而"没红"会被读成"没问题"。
    新增规则时忘了打标签，是最自然的疏忽。
    """
    bad = {n: (r.get("labels") or {}).get("kind") for n, r in slo_rules.items()
           if (r.get("labels") or {}).get("kind") not in (_BURN, _BUDGET)}
    assert not bad, (
        f"以下 SLO 规则没有声明 kind（或取值不在 {(_BURN, _BUDGET)} 内）：{bad}\n"
        "kind 决定它归哪一族、被哪组断言看着；缺失等于从所有断言里漏出去。"
    )


def test_recording_rules_live_in_their_own_file(alerts_cfg, recording_cfg):
    """两个文件各装一种规则：alerts.yml 只有告警，recording.yml 只有录音。

    为什么必须钉住：
      * 录音规则混进 alerts.yml ⇒ 本文件里所有按 `rule["alert"]` 遍历的断言直接
        抛 KeyError（报错指向守卫而不是真正的错处），或者为了绕开它给录音规则
        编一套 `alert:`/`severity:`/`runbook:` 的假标签 —— 后者会把
        "每条告警都要写处置办法"这条纪律稀释掉；
      * **告警混进 recording.yml ⇒ 不会有任何报错**：它会正常触发、正常送达，
        只是绕过了告警侧全部的守卫（category 路由、runbook 覆盖、与文档对齐）。
    """
    stray_records = [r.get("record") for g in alerts_cfg["groups"]
                     for r in g["rules"] if "record" in r]
    assert not stray_records, (
        f"alerts.yml 里出现了录音规则：{stray_records}（应放进 recording.yml）"
    )
    stray_alerts = [r.get("alert") for g in recording_cfg["groups"]
                    for r in g["rules"] if "alert" in r]
    assert not stray_alerts, (
        f"recording.yml 里出现了告警规则：{stray_alerts}（应放进 alerts.yml，"
        "否则它会绕过告警侧所有守卫且不报错）"
    )
    for g in recording_cfg["groups"]:
        for r in g["rules"]:
            assert r.get("record") and r.get("expr"), (
                f"recording.yml 的 {g['name']} 里有条目既不是录音也不是告警：{r}"
            )


def test_recording_rules_are_loaded_and_mounted(prom_cfg, recording_cfg):
    """recording.yml 必须**同时**被 prometheus.yml 加载、且被 compose 挂进容器。

    少任何一处，录音序列**永远不存在** ⇒ 读它的两条预算告警**永远 inactive，
    且没有任何报错**（空序列让条件不成立，而不是让规则报错）——
    与"告警表达式引用了不存在的 scrape job"完全同类。
    最坏的地方在于它看起来像"预算一直很健康"。
    """
    files = prom_cfg.get("rule_files") or []
    assert any("recording.yml" in f for f in files), (
        f"prometheus.yml 的 rule_files 没有加载 recording.yml（当前：{files}）"
    )
    text = COMPOSE.read_text(encoding="utf-8")
    for path in files:
        # 挂载目标必须与 rule_files 里的**容器内路径逐字相同**：写岔时 Prometheus
        # 在启动期就失败，而报错只有一句 "no such file"，很难判断是哪一处错了。
        assert re.search(rf"\.\./monitoring/{re.escape(Path(path).name)}\s*:\s*"
                         rf"{re.escape(path)}:ro", text), (
            f"compose 没有把 {path} 按这个路径挂进 prometheus 容器 —— "
            "Prometheus 会在启动期直接失败（只有一个 no such file 的报错）"
        )


def test_recording_interval_is_within_the_lookback_delta(prom_cfg, recording_cfg):
    """录音组的间隔必须**远小于** lookback delta（默认 5m）。

    ⚠️ 这是最不直观、也最容易踩的一条：录音规则的产物就是**普通样本**，
    写进 TSDB 之后按同样的 lookback 规则被查询。间隔若等于/接近 5m，
    告警取数的那一瞬间最近的样本可能"刚好过期" ⇒ 查询返回**空序列** ⇒
    告警从 firing 掉回 inactive、`for` 重新计时。
    表现是"告警每 5 分钟闪一下"，而日志里没有任何异常。
    """
    default_eval = _secs(prom_cfg.get("global", {}).get("evaluation_interval", "1m"))
    for g in recording_cfg["groups"]:
        iv = g.get("interval")
        secs = _secs(iv) if iv else default_eval
        shown = str(iv) if iv else f"继承全局 evaluation_interval（{default_eval}s）"
        assert secs * 2 <= LOOKBACK_SECS, (
            f"录音组 {g['name']} 的间隔（{shown}）太接近 lookback delta（{LOOKBACK_SECS}s）："
            "告警会因为间歇性取不到序列而反复翻转（firing → inactive → 重新计时）"
        )


def test_prometheus_retention_covers_the_longest_budget_window(recording_cfg):
    """Prometheus 的保留期必须**长于**预算窗口（默认 15d 会让预算只算半个月）。

    ⚠️ 这是一条典型的"不报错的错"：保留期不够时 `avg_over_time(...[30d])`
    不会失败，它只是**拿到现有数据上的平均** ⇒ "上周烧掉的那一段"随时间滑出保留期、
    预算会慢慢**自己涨回来**。这比没有监控更糟 —— 它给的是错误的安全感，
    而"冻结发布"这个动作恰恰要靠这个数字。

    窗口字面量直接从 recording.yml 反推（不写死 30），所以
    "把窗口改大、却忘了改保留期"会立刻测红。
    """
    windows = [_secs(w) for g in recording_cfg["groups"] for r in g["rules"]
               for w in _windows(r.get("expr", ""))]
    assert windows, "recording.yml 里没有任何窗口字面量（窗口被搬走了？）"
    need = max(windows)

    text = COMPOSE.read_text(encoding="utf-8")
    m = re.search(r"--storage\.tsdb\.retention\.time=(\d+[smhd])", text)
    assert m, (
        "compose 没有给 prometheus 设 --storage.tsdb.retention.time —— "
        f"默认保留期只有 15 天，而预算规则要读 {need // 86400} 天，"
        "预算会静默只算半个月（且看起来一切正常）"
    )
    got = _secs(m.group(1))
    assert got >= need * 1.2, (
        f"Prometheus 保留期（{m.group(1)}）不足以覆盖预算窗口 {need // 86400} 天："
        f"至少要留 20% 余量（>= {int(need * 1.2)}s）"
    )


def test_budget_alerts_read_the_recorded_series(slo_rules):
    """预算告警必须读录音序列；**不许**把 30 天窗口搬回告警表达式里。

    搬回来的代价不是"算错"，而是**每次评估都要重算整个窗口**：
    `http_requests_total` 那几百条原始序列 × 86400 个点全部进内存 ——
    在 compose 给 Prometheus 的 512MB 上限下是真风险，而这条规则每 30 秒就要跑一次。
    录音规则那条每 60 秒跑一次，却只读两条聚合序列。
    """
    budget = _of_kind(slo_rules, _BUDGET)
    assert budget, "alerts.yml 里没有任何 kind: budget 的规则"
    for name, rule in budget.items():
        expr = rule["expr"]
        assert BUDGET_METRIC in _series_in(expr), (
            f"{name} 没有读录音序列 {BUDGET_METRIC}（它读到的是 {sorted(_series_in(expr))}），"
            f"实际表达式：{expr!r}"
        )
        for forbidden in ("increase(", "avg_over_time(", "http_requests_total",
                          "http_request_duration"):
            assert forbidden not in expr, (
                f"{name} 的表达式里出现了 {forbidden!r} —— 窗口计算属于录音规则"
                "（recording.yml）；搬回告警里会让每次评估都重算一遍整个 30 天窗口"
            )


def test_recorded_series_used_by_alerts_are_declared(alerts_cfg, recording_cfg):
    """alerts.yml 里引用的 `slo:*` 序列，必须在 recording.yml 里真的被 record 出来。

    这是**哑弹**守卫：引用一个不存在的序列名不会报错 —— 表达式匹配到**空序列** ⇒
    规则永远 inactive。与"告警表达式里 `job=` 指向不存在的 scrape job"完全同类，
    只是换了个命名空间。多打一个字母、或录音规则改了名而告警没跟着改，
    都会落到这里。

    只扫 `slo:` 前缀（录音规则自己的命名空间）；其余指标名由
    `tests/test_runtime_metrics.py` 那几条按各自的口径看着。
    """
    declared = {r["record"] for g in recording_cfg["groups"] for r in g["rules"]}
    used: set[str] = set()
    for _, rule in _all_rules(alerts_cfg):
        used |= set(re.findall(r"\b(slo:[a-z_0-9:]+)", rule.get("expr", "")))
    assert used, "没有任何告警引用 slo: 录音序列（预算那两条被删了？）"
    missing = sorted(used - declared)
    assert not missing, (
        f"以下录音序列被告警引用，但 recording.yml 里没有 record 出它们：{missing}\n"
        f"实际声明的：{sorted(declared)}\n"
        "引用不存在的序列 ⇒ 表达式匹配空序列 ⇒ 规则**永远 inactive 且不报错**。"
    )


def test_budget_rules_scope_the_level_one_series_by_sli(recording_cfg):
    """二级录音规则必须用 `{slo="..."}` 限定一级序列。

    一级刻意把两种 SLI 记成**同一个指标名**（这样告警一条表达式就能覆盖两者，
    与政策表的"任一 SLI"对齐），代价是：不带过滤的引用会同时取到两条序列 ⇒
    再叠上静态标签 `slo: ...` 之后两条结果的标签集完全撞车 ⇒ **重复样本** ⇒
    这条规则每次评估都报错、**什么都不产出**。
    告警随之永远不触发，而 Prometheus 只在 /rules 页与日志里说这件事。

    ⚠️ 断言必须落在**每一个**一级引用上，而不是"表达式里出现过 `{slo=...}`"：
    一条二级规则会引用两个一级序列（bad / total），只给其中一个加过滤时，
    "表达式里含 `{slo=...}`"依然成立 —— 变异验证实测：那样改**一条测试都不红**。
    """
    #: 一级序列的引用形式：序列名 + 紧随其后的可选标签过滤。
    ref = re.compile(r"(slo:(?:bad|total)_rate_5m)((?:\{[^}]*\})?)\[")
    for g in recording_cfg["groups"]:
        for r in g["rules"]:
            if r.get("record") != BUDGET_METRIC:
                continue
            slo = (r.get("labels") or {}).get("slo")
            assert slo, f"{BUDGET_METRIC} 的录音规则没有 slo 标签"
            refs = ref.findall(r["expr"])
            assert refs, f"slo={slo} 的预算规则没有引用任何一级序列：{r['expr']!r}"
            for series, matcher in refs:
                assert matcher == f'{{slo="{slo}"}}', (
                    f"slo={slo} 的预算规则里 {series} 的标签过滤是 "
                    f"{matcher or '（没有）'}，应为 {{slo=\"{slo}\"}} —— "
                    "不带过滤（或过滤成另一种 SLI）会同时取到两种 SLI 的序列，"
                    "再叠上静态标签就产生重复样本，规则每次评估都报错"
                )


def test_budget_remaining_is_recorded_for_every_sli(recording_cfg, slo_rules):
    """必须**每个 SLI 各一条**预算录音规则，且告警确实覆盖了全部 SLI。

    两种失败都是静默的：
      * 少了 latency 那条 ⇒ 延迟预算永远不被检查（政策表写的是"任一 SLI"）；
      * 告警表达式里加了 `{slo="availability"}` 过滤 ⇒ 覆盖范围悄悄减半，
        而摘要里的 `{{ $labels.slo }}` 照常打印那个唯一的取值，看不出少了什么。
    """
    slis = {r.get("labels", {}).get("slo")
            for g in recording_cfg["groups"] for r in g["rules"]
            if r.get("record") == BUDGET_METRIC}
    assert slis == {"availability", "latency"}, (
        f"预算录音规则覆盖的 SLI 是 {slis}，应为 availability 与 latency 两种"
    )
    for name, rule in _of_kind(slo_rules, _BUDGET).items():
        assert 'slo="' not in rule["expr"], (
            f"{name} 的表达式带了 SLI 过滤（{rule['expr']!r}）—— 政策要求覆盖**任一 SLI**，"
            "过滤会把另一种 SLI 悄悄排除在外（而摘要里的 {{ $labels.slo }} 照常打印）"
        )


def test_budget_alerts_and_records_agree_with_the_slo_document(slo_rules, recording_cfg):
    """SLO.md 的预算余量表必须与 alerts.yml / recording.yml 逐项对得上。

    断言四件事：告警存在且是 `kind: budget`；表达式读的正是表里写的序列；
    严重度一致；表里声明的 SLI 覆盖 = 录音规则实际产出的 `slo` 标签集合；
    以及文档声明的窗口字面量真的出现在表达式里（否则"30 天"只是文档里的说法）。
    """
    rows = _budget_rows()
    assert rows, "SLO.md 里没有解析到预算余量表（表头/列数变了？）"
    budget = _of_kind(slo_rules, _BUDGET)
    for row in rows:
        name = row["alert"]
        assert name in budget, (
            f"SLO.md 的余量表提到了 {name}，但它不是一条 kind: budget 的规则"
            f"（{'alerts.yml 里没有' if name not in slo_rules else '它的 kind 是 ' + repr((slo_rules[name].get('labels') or {}).get('kind'))}）"
        )
        rule = budget[name]
        labels = rule.get("labels") or {}
        assert row["metric"] in _series_in(rule["expr"]), (
            f"{name} 的表达式没有读文档里写的序列 {row['metric']}"
            f"（它读到的是 {sorted(_series_in(rule['expr']))}）"
        )
        assert row["severity"] == labels.get("severity"), (
            f"{name} 的 severity 与 SLO.md 不符：文档 {row['severity']}、规则 {labels.get('severity')}"
        )
        # 窗口在**录音规则**里（告警只读序列），所以窗口要对录音规则去查 ——
        # 在告警表达式里找 `[30d]` 是找不到的，那会是一条永远修不好的假红。
        sources = [r for g in recording_cfg["groups"] for r in g["rules"]
                   if r.get("record") == row["metric"]]
        assert sources, f"{row['metric']} 在 recording.yml 里没有被 record 出来"
        assert all(f"[{row['window']}]" in r["expr"] for r in sources), (
            f"{name}：文档声明窗口 [{row['window']}]，但 {row['metric']} 的录音规则里没有"
        )
        declared = {s.strip() for s in row["slo"].split("/")}
        recorded = {r.get("labels", {}).get("slo") for r in sources}
        assert declared == recorded, (
            f"{name}：SLO.md 说覆盖 {declared}，而 {row['metric']} 实际只录了 {recorded}"
        )


def test_budget_targets_match_the_slo_document(recording_cfg):
    """预算录音规则里的错误预算（`1 - 目标`）必须与 SLO.md §3 的目标表一致。

    目标现在写在三个地方：SLO.md §3、燃尽率倍数（`14.4 * (1 - 0.995)`）、
    预算分母（`(1 - 0.995)`）。改一处而漏另外两处时**不会报错** ——
    预算照算，只是算的是**另一个 SLO** 的预算，于是"剩余 20%"这个阈值
    与实际承诺脱钩。
    """
    text = SLO_DOC.read_text(encoding="utf-8")
    rows = _doc_table(text, ["sli", "target", "budget", "meaning"])
    assert rows, "SLO.md 里没有解析到 §3 的目标表（4 列表格）"
    targets: dict[str, float] = {}
    for row in rows:
        sli = row["sli"].strip().strip("`")
        # 只认「裸 SLI 名」的行：本文件里别的 4 列表格（若有）不该被当成目标表，
        # 认不出来时宁可跳过也不要误报成"目标列解析不了"。
        if not re.fullmatch(r"[a-z_]+", sli):
            continue
        m = re.match(r"(\d+(?:\.\d+)?)%", row["target"])
        assert m, f"目标表里 {sli} 的目标列解析不了：{row['target']!r}"
        targets[sli] = float(m.group(1)) / 100
    assert {"availability", "latency"} <= set(targets), (
        f"SLO.md §3 的目标表里没有解析到 availability 与 latency（解析到：{sorted(targets)}）"
    )

    win = re.search(r"目标（窗口：(\d+)\s*天）", text)
    assert win, "SLO.md §3 的表头没有声明窗口（形如『目标（窗口：30 天）』）"
    window = f"[{win.group(1)}d]"

    recorded = [r for g in recording_cfg["groups"] for r in g["rules"]
                if r.get("record") == BUDGET_METRIC]
    assert recorded, f"recording.yml 里没有 {BUDGET_METRIC}"
    for r in recorded:
        slo = (r.get("labels") or {}).get("slo")
        assert slo in targets, f"预算规则的 slo={slo} 在 SLO.md §3 的目标表里没有"
        expected = f"(1 - {targets[slo]})"
        assert expected in r["expr"], (
            f"slo={slo}：SLO.md §3 的目标是 {targets[slo]}（错误预算 {1 - targets[slo]:.3g}），"
            f"表达式里应出现 {expected!r}，实际：{r['expr']!r}"
        )
        assert window in r["expr"], (
            f"slo={slo} 的预算规则没有读 SLO.md §3 声明的 {window} 窗口"
        )


def test_budget_route_precedes_the_generic_slo_routes(am_cfg):
    """`kind = "budget"` 的专属路由必须排在通用 slo 路由之前，且 repeat 更长。

    Alertmanager 是**首个匹配生效**：预算告警同时满足 `category = "slo"`
    与一条 severity，排在后面就会继承通用路由的 `repeat_interval`（1h）。
    余量是**慢变量**（耗尽可能持续到月底），每小时重复一次的结果是一周内被静音 ——
    而它恰是"冻结发布"这个动作**唯一**的自动触发器。
    （推理与 `test_watchdog_route_precedes_the_platform_wildcard` 完全同源。）
    """
    routes = am_cfg["route"].get("routes") or []

    def _has(r, needle):
        return any(needle in m for m in (r.get("matchers") or []))

    budget = [i for i, r in enumerate(routes) if _has(r, 'kind = "budget"')]
    generic = [i for i, r in enumerate(routes)
               if _has(r, 'category = "slo"') and not _has(r, 'kind = "budget"')]
    assert budget, "没有为 kind=budget 定义专属路由 —— 它会落进通用 slo 路由（repeat 1h）"
    assert generic, "没有 category=slo 的通用路由（燃尽率告警无处可去）"
    assert min(budget) < min(generic), (
        "预算告警的专属路由排在通用 slo 路由之后 —— 首个匹配生效，"
        "它会继承通用路由的 repeat_interval，预算耗尽的提醒频率不再是 24h"
    )
    hold = routes[min(budget)].get("repeat_interval")
    assert hold, "预算路由没有显式 repeat_interval（'多久提醒一次'这个约定没写下来）"
    assert _secs(hold) >= 6 * 3600, (
        f"预算路由的 repeat_interval（{hold}）太短：余量是月级慢变量，"
        "频繁重复只会让它在被处置之前就被静音"
    )


def test_alert_le_labels_match_exposed_format(alerts_cfg):
    """告警里写的 `le="..."` 必须与 `app/metrics.py` 实际渲染出的完全一致。

    PromQL 的匹配是**全串**的：指标暴露 `le="1.0"` 而规则里写 `le="1"`，
    结果不是报错，而是这条规则**永不触发** —— 面板上永远看不出问题。
    这是本项目最典型的"静默故障"类别之一，所以把格式钉住。

    ⚠️ 只扫 `expr` 字段，不扫整个文件：注释里出现的 `le="1"` 是**说明性**的
    （正是用来举例"写成 1 就错了"），扫全文会把它当成错误。
    """
    from app import metrics

    metrics.reset()
    try:
        metrics.observe("http_request_duration_seconds", 0.3, path="/v1/reviews")
        rendered = metrics.render()
    finally:
        metrics.reset()

    exposed = set(re.findall(r'le="([^"]+)"', rendered))
    assert "1.0" in exposed, (
        "指标渲染出的桶边界里没有 '1.0' —— 桶定义或渲染格式变了，"
        "请同步检查 alerts.yml 里的 le 过滤"
    )

    used: set[str] = set()
    for group in alerts_cfg["groups"]:
        for rule in group["rules"]:
            used |= set(re.findall(r'le="([^"]+)"', rule.get("expr", "")))
    assert used, "没有任何表达式用 le= 过滤（延迟 SLI 是不是漏了边界？）"
    unknown = used - exposed
    assert not unknown, (
        f"表达式里出现了指标不会产生的 le 值：{sorted(unknown)}；"
        f"指标实际暴露：{sorted(exposed)}。全串不匹配 ⇒ 该规则永不触发。"
    )


# ---------------------------------------------------------------------------
# 平台自身（监控栈 + 备份 + 磁盘）—— "谁来发现监控坏了"
# ---------------------------------------------------------------------------
COMPOSE = ROOT / "deploy" / "app" / "docker-compose.yml"

#: 这一组每一条都对应一个"否则完全不可见"的盲区，删掉任意一条都要人工确认。
PLATFORM_ALERTS = {
    "Watchdog",            # 心跳（唯一能穿透"Alertmanager 自己挂了"的信号）
    "MonitoringStackDown",  # 监控栈组件不可达
    "TLSCertProbeDown",    # 证书探活自己挂了（否则 TLSCertExpiringSoon 是哑弹）
    "AlertDeliveryFailing",  # 告警在响但发不出去
    "BackupLoopFailing",   # 备份很久没成功
    "BackupMetricsMissing",  # 备份指标本身不见了（textfile 链路断了）
    "DiskSpaceLow",        # 磁盘（满盘的表现是 PG 随机 500，不是"磁盘满"）
}


def _rules_by_name(alerts_cfg) -> dict:
    return {rule["alert"]: rule for _, rule in _all_rules(alerts_cfg)}


def test_platform_group_covers_the_self_monitoring_blind_spots(alerts_cfg):
    """必须有 chatagent-platform 组，且覆盖平台自身的几条盲区。

    缺了这一组时的最坏状态是：**整条告警链路安静地失效，而你以为一切正常** ——
    规则照样 firing（面板上灯全亮），但一条都送不出去；或备份/磁盘早就出问题，
    而没有任何信号指向它。所以这里把"这几条必须存在"钉住。
    """
    groups = {g["name"]: g for g in alerts_cfg["groups"]}
    assert "chatagent-platform" in groups, (
        "缺少 chatagent-platform 组 —— 监控栈自身的故障将完全不可观测"
    )
    names = {r["alert"] for r in groups["chatagent-platform"]["rules"]}
    missing = PLATFORM_ALERTS - names
    assert not missing, (
        f"chatagent-platform 组缺少这些规则：{sorted(missing)}\n"
        "每一条都对应一个'否则完全不可见'的盲区，删除前请先确认有替代信号。"
    )


def test_watchdog_is_a_constant_true_heartbeat(alerts_cfg):
    """Watchdog 必须是恒真表达式（`vector(1)`）+ severity=none。

    它的价值**不在"响"，而在"持续到达"**：因为它是必须到达的，"没到达"本身
    才成为一个可观测信号 —— 这是唯一能穿透"Alertmanager 自己挂了"这个
    自我指涉死角的机制。写成任何带条件的表达式（如 `up == 1`）都会让它
    变成"有时才响"，于是"没收到心跳"再也分不清是链路断了还是本来就该静默。
    """
    rules = _rules_by_name(alerts_cfg)
    assert "Watchdog" in rules, "缺少 Watchdog 心跳规则"
    wd = rules["Watchdog"]
    assert wd["expr"].strip() == "vector(1)", (
        f"Watchdog 的表达式必须是恒真的 `vector(1)`，实际是 {wd['expr']!r}；"
        "带条件的表达式会让'心跳缺失'不再是一个可判定的信号"
    )
    assert wd["labels"]["severity"] == "none", (
        "Watchdog 必须是 severity=none（它不是故障，是心跳）——"
        "否则会被当成 critical 反复叫人，很快被静音，心跳机制随之失效"
    )


def test_watchdog_route_precedes_the_platform_wildcard(am_cfg):
    """Watchdog 的专属路由必须排在通配的 `category=platform` 之前。

    Alertmanager 的路由是**首个匹配生效**。两条 matchers 都能匹配 Watchdog
    （它既 `category=platform` 又 `alertname=Watchdog`），所以顺序决定成败：
    通配那条在前时，Watchdog 会继承 oncall 的 `repeat_interval`（1h）——
    "每日心跳"变成"每小时骚扰一次"，很快被静音，心跳机制随之失效。
    """
    routes = am_cfg["route"].get("routes") or []

    def _has(r, key, needle):
        return any(key in m and needle in m for m in (r.get("matchers") or []))

    wd_idx = [i for i, r in enumerate(routes)
              if _has(r, "alertname", "Watchdog") and _has(r, "category", "platform")]
    wild_idx = [i for i, r in enumerate(routes)
                if _has(r, "category", "platform")
                and not any("alertname" in m for m in (r.get("matchers") or []))]

    assert wd_idx, "没有为 Watchdog 定义专属路由 —— 它会落进通配的 platform 路由"
    assert wild_idx, "没有 category=platform 的通配路由（其余平台告警无处可去）"
    assert min(wd_idx) < min(wild_idx), (
        "Watchdog 的专属路由排在通配 platform 路由之后 —— Alertmanager 首个匹配生效，"
        "Watchdog 会继承通配路由的 repeat_interval，心跳频率不再是 24h"
    )
    assert routes[min(wd_idx)].get("repeat_interval"), (
        "Watchdog 路由没有显式 repeat_interval —— '多久该收到一次心跳'这个约定"
        "没写下来，会退回根路由的默认值"
    )


def test_alerts_reference_only_existing_scrape_jobs(alerts_cfg, prom_cfg):
    """告警表达式里 `job=...` 引用的作业必须在 prometheus.yml 里真实存在。

    这是"哑弹"的一种：规则语法完全合法，但当它引用的 job 不存在时，表达式
    匹配到**空序列** ⇒ 规则永远 inactive，**且没有任何报错**。
    证书探活（`blackbox-tls`）与外部探活（`blackbox-chatagent`）都踩过这个坑 ——
    `probe_success == 0` 在 exporter 挂掉时不会触发，因为序列**停止产出**而不是变成 0。
    """
    jobs = {sc["job_name"] for sc in prom_cfg["scrape_configs"]}
    assert jobs, "prometheus.yml 里没有定义任何 scrape job"

    exact: set[str] = set()
    prefixes: set[str] = set()
    for _, rule in _all_rules(alerts_cfg):
        expr = rule.get("expr", "")
        exact |= set(re.findall(r'job\s*=\s*"([^"]+)"', expr))
        for rx in re.findall(r'job\s*=~\s*"([^"]+)"', expr):
            for alt in rx.split("|"):
                if alt.endswith(".*"):
                    prefixes.add(alt[:-2])
                elif alt:
                    exact.add(alt)

    unknown = {j for j in exact if j not in jobs}
    unknown |= {f"{p}*" for p in prefixes if not any(j.startswith(p) for j in jobs)}
    assert not unknown, (
        f"告警引用了 prometheus.yml 里不存在的 job：{sorted(unknown)}\n"
        f"实际存在的 job：{sorted(jobs)}\n"
        "引用不存在的 job ⇒ 表达式匹配空序列 ⇒ 规则**永远 inactive 且不报错**。"
    )


#: 这些 scrape job **不需要**单独的 `up` 规则 —— 每一项都必须写清理由。
#: 之所以要写理由而不是静默跳过：静默跳过 == 看不见，而那正是这次要修的毛病本身。
_JOB_UP_NOT_NEEDED = {
    "blackbox-chatagent": (
        "它的 up==0 只可能由 blackbox-exporter 自身不可达引起，已被 MonitoringStackDown "
        "覆盖；而目标 URL 不可达会让 probe_success 变成 0 ⇒ ExternalProbeFailing 覆盖。"
    ),
}


def test_every_scrape_job_has_someone_watching_it(alerts_cfg, prom_cfg):
    """反向的那一半：prometheus.yml 里**每个** scrape job 都必须有规则盯着它的 `up`。

    上面那条防的是"告警指着不存在的 job ⇒ 哑弹"。这里防的是另一侧，同样静默：
    **抓取目标新增了，但没有任何规则看它的抓取存活**。抓取失败是 Prometheus 自己
    产生的 `up == 0`，有规则匹配就会响；没有规则就完全没有信号 —— 面板上那块图是空的，
    而"图是空的"和"一切正常"长得一模一样。

    为什么现在才补（2026-10-06）：`MonitoringStackDown` 的表达式是**逐个列举** job 名的
    （`up{job=~"prometheus|alertmanager|node-exporter|blackbox-exporter"} == 0`）。
    后加的 `blackbox-tls` 不在列举里，于是证书探活挂掉时**没有任何告警**，连带
    `TLSCertExpiringSoon` 一起成了哑弹（它读的序列根本不产出）。列举式判据漏一项没有症状，
    所以这里改成"发现式"：把 prometheus.yml 里的 job 全抓出来，逐个确认有规则覆盖。
    （同一个毛病今天已经在别处栽过两次 —— 见 `test_deploy_topology.py` 的
    `test_every_compose_file_is_registered`。清单必须配一条一致性检查，否则它会慢慢失效。）

    ⚠️ 判据必须是"**读 `up{...}` 的规则**"，不能放宽成"表达式里提到过这个 job"
    ---------------------------------------------------------------------------
    第一版就是这么写的（从所有 expr 里抓 `job="x"` / `job=~"x"`），结果**在修复前就是绿的**：
    `TLSCertExpiringSoon` 的 `probe_ssl_earliest_cert_expiry{job=~"blackbox-.*"}`
    也提到了 `blackbox-tls`，于是它被算成"有人盯"。可那条规则读的是**证书指标**，
    抓取本身停掉它一点办法都没有 —— 把"引用过"当成"盯住了"，正好放走了这次要修的缺陷。
    同理 `=~` 的正则在 Prometheus 里是**全串锚定**的，所以这里用 `re.fullmatch`
    而不是 `re.match`，否则 `chatagent-.*` 会被误判成也能覆盖 `chatagent-typo` 之类。
    """
    jobs = {sc["job_name"] for sc in prom_cfg["scrape_configs"]}
    assert jobs, "prometheus.yml 里没有定义任何 scrape job"

    # 只承认 `up{...}` 选择器里的 job 匹配器 —— `up` 是"这次抓取成功了没有"的唯一来源。
    up_matchers: list = []
    for _, rule in _all_rules(alerts_cfg):
        expr = rule.get("expr", "") or ""
        for selector in re.findall(r"\bup\{([^}]*)\}", expr):
            up_matchers += re.findall(r'job\s*(=~|=)\s*"([^"]*)"', selector)

    assert up_matchers, (
        "没有任何规则读 `up{...}` —— 抓取存活完全不可观测（`MonitoringStackDown` / "
        "`ChatAgentDown` 都不在了？）"
    )

    unwatched = [
        job for job in sorted(jobs)
        if job not in _JOB_UP_NOT_NEEDED
        and not any((job == pat if op == "=" else re.fullmatch(pat, job))
                    for op, pat in up_matchers)
    ]
    assert not unwatched, (
        f"这些 scrape job 没有任何规则盯着它的 `up`：{unwatched}\n"
        "抓取失败不会产生任何可匹配的序列 ⇒ 该目标挂掉时**完全不可观测**。\n"
        '要么加一条 `up{job="..."} == 0` 的规则，要么登记进 _JOB_UP_NOT_NEEDED '
        "并写明它被哪条规则间接覆盖。\n"
        "⚠️ 只在别的表达式里「被提到」（比如读该 job 的某个业务指标）**不算**盯住了 —— "
        "那些规则在抓取停掉时同样匹配不到序列。"
    )

    # 反向：登记表里的 job 必须还在 —— 否则它会慢慢积成死条目，
    # 而它本来是"为什么这个可以不盯"的唯一记录。
    stale = sorted(set(_JOB_UP_NOT_NEEDED) - jobs)
    assert not stale, (
        f"_JOB_UP_NOT_NEEDED 里登记了 prometheus.yml 中已不存在的 job：{stale}\n"
        "（删掉这些条目，或它们对应的 job 被误删了）"
    )


def test_backup_failure_alert_tolerates_a_skipped_backend(alerts_cfg):
    """`BackupLoopFailing` 必须用 `backup_backend_skipped` 排除"后端未启用"。

    Qdrant 未启用时那个备份循环会**永远** rc=2（明确播报"跳过"）。若告警只看
    时间戳，就会得到一个**永不恢复的假告警** —— 而长期存在的假告警等于没有告警
    （所有人都会忽略它）。所以这个显式排除不是装饰。
    """
    rules = _rules_by_name(alerts_cfg)
    assert "BackupLoopFailing" in rules, "缺少'备份很久没成功'的告警"
    expr = rules["BackupLoopFailing"]["expr"]
    assert "backup_backend_skipped" in expr, (
        "BackupLoopFailing 没有排除 backup_backend_skipped —— "
        "Qdrant 未启用时会得到一个永不恢复的假告警"
    )
    assert re.search(r"\band\b", expr), (
        "备份失败告警没有用 and 组合两个条件（'跳过'的判定没接上）"
    )


def test_missing_metrics_alert_uses_absent_not_a_value_comparison(alerts_cfg):
    """'指标不存在'必须用 `absent()`，不能塞进数值比较。

    `absent()` 与"数值超标"是两种不同的故障：前者说明链路断在更前面
    （备份容器没起 / textfile 目录没挂对），后者说明备份确实很久没成功。
    塞进同一条表达式会让人分不清该去查备份还是查 exporter。
    """
    rules = _rules_by_name(alerts_cfg)
    assert "BackupMetricsMissing" in rules, (
        "缺少'备份指标不存在'的告警 —— textfile 链路断了将无人知晓"
    )
    expr = rules["BackupMetricsMissing"]["expr"]
    assert "absent(" in expr, (
        "BackupMetricsMissing 没有用 absent() —— 它要判的是'序列不存在'而非'值超标'"
    )
    assert not re.search(r"[<>]|backup_backend_skipped", expr), (
        "BackupMetricsMissing 里混入了数值比较或跳过判定 —— "
        "absent() 的结果是 0/1，再比大小或加合取会让语义变得无法判断"
    )


def test_disk_alert_excludes_pseudo_filesystems(alerts_cfg):
    """磁盘告警必须过滤掉 tmpfs / overlay 等伪文件系统。

    容器里的 overlay 挂载点常年接近 100% 满。不过滤时，"最小可用比例"
    会被它拉到 0 ⇒ 规则**永久 firing**（又一个假告警）。
    """
    rules = _rules_by_name(alerts_cfg)
    assert "DiskSpaceLow" in rules, (
        "缺少磁盘空间告警 —— 满盘的表现是 PG 随机 500，不是'磁盘满'"
    )
    expr = rules["DiskSpaceLow"]["expr"]
    assert "fstype" in expr, "DiskSpaceLow 没有按 fstype 过滤伪文件系统"
    for fs in ("tmpfs", "overlay"):
        assert fs in expr, (
            f"DiskSpaceLow 的 fstype 过滤里没有 {fs} —— 伪文件系统会把可用比例"
            "拉到 0，导致规则永久 firing"
        )


def _compose_env_default(text: str, key: str) -> str | None:
    m = re.search(rf"\$\{{{re.escape(key)}:-([^}}]+)\}}", text)
    return m.group(1) if m else None


def test_backup_failure_threshold_tracks_the_configured_interval(alerts_cfg):
    """`BackupLoopFailing` 的阈值必须与 `BACKUP_INTERVAL_HOURS` 保持倍数关系。

    阈值在 PromQL 里只能写死（它读不到 compose 的默认值），但必须留出
    **至少两个完整周期**的容错，又不能在间隔调大后变成"漏了好多轮才响"。
    改 `BACKUP_INTERVAL_HOURS` 时忘了改这里 ⇒ 不是稳定误报就是漏报，
    而两者都不会有任何报错。
    """
    rules = _rules_by_name(alerts_cfg)
    expr = rules["BackupLoopFailing"]["expr"]
    m = re.search(r"backup_last_success_timestamp_seconds\s*>\s*(\d+)", expr)
    assert m, "解析不到 BackupLoopFailing 的时间阈值（表达式格式变了？）"
    threshold_h = int(m.group(1)) / 3600

    interval = _compose_env_default(COMPOSE.read_text(encoding="utf-8"),
                                    "BACKUP_INTERVAL_HOURS")
    assert interval, "compose 里没找到 BACKUP_INTERVAL_HOURS 的默认值"
    interval_h = float(interval)

    assert 2 * interval_h <= threshold_h <= 4 * interval_h, (
        f"备份告警阈值 {threshold_h}h 与备份间隔 {interval_h}h 不成比例："
        f"要求落在 [{2 * interval_h}h, {4 * interval_h}h]。"
        "太小 ⇒ 单周期抖动就误报；太大 ⇒ 连续漏掉多轮才响。"
    )


# ---------------------------------------------------------------------------
# 邮件通道（默认关闭）—— 启用说明在 alertmanager.yml 末尾，这里是它的守卫
# ---------------------------------------------------------------------------
# 为什么需要单独一节：邮件配置**默认是注释状态**，所以它不会被 YAML 解析、
# 也就不会被上面任何一条断言覆盖。而它恰恰是最容易写错的部分
# （`send_resolved` 默认 false、密码必须走 `_file`、挂载路径三处必须一致）。
# 把注释里的片段当配置解析，是唯一能让它"坏掉就变红"的办法。

ALERTMANAGER = MON / "alertmanager.yml"

#: 密码文件的**容器内**路径。三处必须一致：alertmanager.yml 的
#: `smtp_auth_password_file`、compose 的挂载目标、以及下面的断言。
#: 不一致时 Alertmanager 在**装配期**读不到文件而拒绝启动 —— fail loud 是好事，
#: 但报错只有一句 "no such file"，很难判断是哪一处写错了。
PASSWORD_IN_CONTAINER = "/etc/alertmanager/smtp_password"

#: 宿主侧路径（相对仓库根），必须被 git 忽略。
PASSWORD_ON_HOST = "deploy/monitoring/secrets/smtp_password"


def _fragment(tag: str) -> str:
    """取出 `# --- 片段X ---` 与 `# --- 片段X结束 ---` 之间被注释掉的 YAML。

    剥掉注释符后交给调用方按 YAML 解析 —— 这样"片段里的字段名写错/缩进错"
    会直接变成解析失败，而不是等到某天有人照抄时才炸。
    """
    text = ALERTMANAGER.read_text(encoding="utf-8")
    start = re.search(rf"(?m)^\s*#\s*---\s*片段{tag}\s*---\s*$", text)
    assert start, f"alertmanager.yml 里找不到『片段{tag}』的起始标记 —— 启用说明被删了？"
    tail = text[start.end():]
    end = re.search(rf"(?m)^\s*#\s*---\s*片段{tag}结束\s*---\s*$", tail)
    assert end, f"『片段{tag}』没有结束标记，它后面的注释会被一起当成配置"

    lines: list[str] = []
    for raw in tail[: end.start()].splitlines():
        if not raw.strip():
            continue
        assert raw.strip().startswith("#"), (
            f"『片段{tag}』里混进了一行**未注释**的内容：{raw!r} —— "
            "它会被 Alertmanager 当成真实配置生效（而这些片段本该是关闭状态）"
        )
        lines.append(re.sub(r"^\s*#\s?", "", raw))
    assert lines, f"『片段{tag}』是空的"
    return textwrap.dedent("\n".join(lines))


def test_email_setup_fragments_are_valid_yaml():
    """三个片段必须是合法 YAML，且结构与其粘贴位置匹配。

    片段②③ 粘进的是**列表**（`receivers:` / `route.routes:`），片段① 是**映射**
    （`global:`）。结构不对时人一照抄就得到一个非法配置 —— 而
    "照抄文档"正是这类片段唯一的用法。
    """
    assert isinstance(yaml.safe_load(_fragment("①")), dict), "片段① 不是映射（要粘进 global:）"
    for tag in ("②", "③"):
        got = yaml.safe_load(_fragment(tag))
        assert isinstance(got, list), f"片段{tag} 不是列表（要粘进 receivers:/routes:）"


def test_email_global_fragment_covers_every_required_smtp_key():
    """`global` 片段必须给全，且密码只能以 `_file` 形式出现。"""
    got = yaml.safe_load(_fragment("①"))
    for key in ("smtp_smarthost", "smtp_from", "smtp_auth_username",
                "smtp_auth_password_file", "smtp_require_tls"):
        assert key in got, f"片段① 缺少 {key}"
    assert got["smtp_auth_password_file"] == PASSWORD_IN_CONTAINER, (
        f"片段① 的 smtp_auth_password_file 是 {got['smtp_auth_password_file']!r}，"
        f"与 compose 挂载的目标 {PASSWORD_IN_CONTAINER!r} 不一致"
    )
    assert "smtp_auth_password" not in got, (
        "片段① 用了 `smtp_auth_password`（内联明文）—— 它会被提交进版本库。"
        "Alertmanager 不支持 ${VAR} 插值，所以只能走 `smtp_auth_password_file`"
    )


def test_email_receiver_fragment_sends_resolved_and_has_a_recipient():
    """两处最容易漏的：`to` 必填、`send_resolved` 默认 false。"""
    (rx,) = yaml.safe_load(_fragment("②"))
    assert rx["name"] == "email", f"片段② 的 receiver 名是 {rx['name']!r}"
    (cfg,) = rx["email_configs"]
    assert cfg.get("to"), "email_configs 的 `to` 是必填项，漏了 Alertmanager 直接拒绝启动"
    assert cfg.get("send_resolved") is True, (
        "email_configs 没有显式 `send_resolved: true` —— 它的默认值是 **false**，"
        "于是你**永远收不到「已恢复」**（而现有 webhook_configs 都显式写了 true）"
    )
    assert "auth_password" not in cfg, "收件人配置里出现了内联 auth_password，密码不该进版本库"


def test_email_route_fragment_uses_continue():
    """旁路路由必须 `continue: true`，否则会把所有分级路由吃掉。"""
    (route,) = yaml.safe_load(_fragment("③"))
    assert route["receiver"] == "email"
    assert route.get("continue") is True, (
        "email 路由没有 `continue: true` —— Alertmanager 是**首个匹配生效**，"
        "这条「匹配所有告警」的旁路会把下面各分级路由全部吃掉，"
        "安全告警再也不会送到能处置攻击的人"
    )


def test_compose_mounts_the_password_file_at_the_same_path():
    """compose 的挂载目标必须与配置里的 `smtp_auth_password_file` 逐字一致。"""
    text = COMPOSE.read_text(encoding="utf-8")
    m = re.search(r"\.\./monitoring/(secrets[/\\]smtp_password)\s*:\s*(\S+?):ro", text)
    assert m, (
        "compose 的 alertmanager 服务里找不到 smtp_password 的挂载行 —— "
        "启用邮件时密码文件没挂进容器，Alertmanager 会在启动阶段失败"
    )
    assert m.group(2) == PASSWORD_IN_CONTAINER, (
        f"compose 把密码挂到 {m.group(2)!r}，而配置读的是 {PASSWORD_IN_CONTAINER!r}"
    )
    assert m.group(1).replace("\\", "/") == "secrets/smtp_password"


def test_smtp_password_can_never_be_committed():
    """密码文件必须**真的**被 git 忽略 —— 用 `git check-ignore` 实测。

    查 `.gitignore` 里有没有那一行是弱断言：忽略语义涉及反选（`!`）、
    上级目录、`.git/info/exclude` 等，读一行文本判断不了。真正要保证的是
    "这个路径不会被 `git add` 进去"，那就直接问 git。
    """
    if not (ROOT / ".git").exists():
        pytest.skip("不在 git 仓库里（源码打包场景），跳过行为级校验")
    proc = subprocess.run(["git", "check-ignore", "-q", PASSWORD_ON_HOST],
                          cwd=ROOT, capture_output=True)
    assert proc.returncode == 0, (
        f"{PASSWORD_ON_HOST} 没有被 git 忽略 —— 一次 `git add -A` 就会把 SMTP "
        "密码提交进版本库。请确认 `.gitignore` 覆盖了 `deploy/monitoring/secrets/`"
    )


