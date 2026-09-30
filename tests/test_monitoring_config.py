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

这些都无法靠"人能记住"来防（改配置的人通常只关心自己那条规则），所以固化成断言。
"""

from __future__ import annotations

import re
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
# SLO / 错误预算（alerts.yml 的 chatagent-slo 组 + SLO.md）
# ---------------------------------------------------------------------------
SLO_DOC = MON / "SLO.md"


@pytest.fixture(scope="module")
def slo_rules(alerts_cfg):
    """所有 `category: slo` 的规则（按告警名索引）。"""
    return {rule["alert"]: rule for _, rule in _all_rules(alerts_cfg)
            if rule.get("labels", {}).get("category") == "slo"}


def _secs(dur: str) -> int:
    """把 Prometheus 的窗口字面量（`30s` / `5m` / `6h` / `3d`）换算成秒。"""
    m = re.fullmatch(r"(\d+)([smhd])", str(dur).strip())
    assert m, f"无法解析的时长：{dur!r}"
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return int(m.group(1)) * mult


def _windows(expr: str) -> list[str]:
    """取出表达式里所有窗口字面量（`[1h]` → `1h`）。"""
    return re.findall(r"\[(\d+[smhd])\]", expr)


def _doc_table(text: str) -> list[dict]:
    """解析 SLO.md 的燃尽率表（唯一一张 8 列的表格）。

    为什么靠"列数 + 表头"识别而不是靠标题定位：改标题不该让测试失效，
    但把表格列删一列必须被发现（那意味着某条口径没写下来）。
    """
    cols = ["alert", "slo", "target", "burn", "long", "short",
            "budget", "severity"]
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
        out.append(dict(zip(cols, cells)))
    return out


def test_slo_doc_exists():
    assert SLO_DOC.exists(), "缺少 SLO.md —— SLO 不该只存在于 YAML 表达式里"


def test_slo_doc_matches_alert_expressions(slo_rules):
    """SLO.md 表格里的每一行，都必须能在 `alerts.yml` 的表达式里对上。

    这是防"文档与实现脱节"的核心断言：只改表达式不改文档（或反过来）立刻测红。
    断言三件事：燃尽率倍数、SLO 目标、长短两个窗口。
    """
    rows = _doc_table(SLO_DOC.read_text(encoding="utf-8"))
    assert rows, "SLO.md 里没有解析到燃尽率表（表头/列数变了？）"

    for row in rows:
        name = row["alert"]
        assert name in slo_rules, f"SLO.md 提到了 {name}，但 alerts.yml 里没有这条规则"
        expr = slo_rules[name]["expr"]

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
    """
    documented = {r["alert"] for r in _doc_table(SLO_DOC.read_text(encoding="utf-8"))}
    missing = set(slo_rules) - documented
    assert not missing, f"以下 SLO 规则没有写进 SLO.md：{sorted(missing)}"


def test_slo_rules_use_multi_window_burn_rate(slo_rules):
    """每条 SLO 规则都必须是"长 + 短"两个窗口，且用 `and` 连接。

    单窗口燃尽率告警是这一族里最常见的写错方式，而它在语法上完全合法：
      - 只留长窗口 ⇒ 已过去的尖峰让告警继续挂很久（恢复慢、误报长）；
      - 只留短窗口 ⇒ 30 秒抖动就能把人叫醒（噪音，很快被忽略）。
    两者都必须能区分开，所以断言"窗口数 >= 2 **且** 表达式里有 and"。
    """
    assert slo_rules, "没有任何 category: slo 的规则"
    for name, rule in slo_rules.items():
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
    """
    for name, rule in slo_rules.items():
        wins = sorted({_secs(w) for w in _windows(rule["expr"])})
        short = wins[0]
        hold = _secs(rule.get("for", "0s"))
        assert 0 < hold <= short, (
            f"{name} 的 for={rule.get('for')} 不在 (0, {short}s] 内："
            "要么白等一遍窗口，要么完全靠抖动触发"
        )


def test_slo_numerator_does_not_use_unhandled_exception_counter(slo_rules):
    """可用性 SLI 的分子不得用 `http_errors_total`。

    它只在**未捕获异常**时自增（见 `app/middleware.py`），503 这类
    "正常返回的失败"不计入 —— 拿它当分子会系统性低估错误率，
    SLO 会**永远显示达标**，这是最危险的一种假绿。
    """
    bad = [name for name, rule in slo_rules.items()
           if "http_errors_total" in rule["expr"]]
    assert not bad, (
        f"以下 SLO 规则用 http_errors_total 当分子：{bad}；"
        "应改用 http_requests_total{status=~\"5..\"}"
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


