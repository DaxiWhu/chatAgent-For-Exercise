"""恢复演练闭环的守卫。

背景
----
`pg_backup.py drill`（备份 → 恢复到临时库 → 逐表比对）是**唯一能证明备份可用**的
操作，但它此前没有任何东西在跑：RUNBOOK 写着"每季度做一次"，而靠人记着的事
一定会在最忙的那一季度被跳过。2026-09-30 补了三层：

    ① 演练结果落 textfile 指标（`backups/drill-*.prom`）→ node-exporter → Prometheus
    ② 三条告警：Missing（从没演练过）/ Overdue（太久没演练）/ Failed（上次没过）
    ③ CI 每月真跑一次（用 service container 起真 PG），验证**脚本本身没坏**

本文件钉住这三层的接线。它们全都是"断了也不报错"的类型：

- 指标名改了而告警没跟着改 ⇒ 告警**永远不触发**。而"没触发"和"没问题"
  在收件箱里长得一模一样；
- 指标写到了 node-exporter 看不见的目录（尤其是**子目录** —— textfile collector
  不递归）⇒ 文件在、时间戳在更新，Prometheus 里却什么都没有；
- CI 的 drill job 被删/改名 ⇒ "脚本没坏"这层保障**静默消失**。
"""

from __future__ import annotations

import os
import re
import types
from pathlib import Path

import pytest
import yaml

from scripts import drill_metrics

ROOT = Path(__file__).resolve().parents[1]
ALERTS = ROOT / "deploy" / "monitoring" / "alerts.yml"
COMPOSE = ROOT / "deploy" / "app" / "docker-compose.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"
PG = ROOT / "scripts" / "pg_backup.py"
QDRANT = ROOT / "scripts" / "qdrant_backup.py"
DASH_JSON_DIR = (
    ROOT / "deploy" / "monitoring" / "grafana" / "provisioning" / "dashboards" / "json"
)

_DRILL_ALERTS = ("BackupDrillMissing", "BackupDrillOverdue", "BackupDrillFailed")

# 发布器声明的全部指标 —— 断言的"全集"从模块常量取，而不是另抄一份字符串
_PUBLISHED = (drill_metrics.M_ATTEMPT, drill_metrics.M_SUCCESS, drill_metrics.M_RESULT)


def _rules() -> dict[str, dict]:
    cfg = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
    return {r["alert"]: r for g in cfg["groups"] for r in g["rules"]}


def _consumer_text() -> str:
    """所有"会去读指标"的地方：告警 + 面板 JSON。"""
    text = ALERTS.read_text(encoding="utf-8")
    for p in sorted(DASH_JSON_DIR.glob("*.json")):
        text += p.read_text(encoding="utf-8")
    return text


# --------------------------------------------------------------------------
# ① 指标 ↔ 告警 的接线
# --------------------------------------------------------------------------
def test_alerts_actually_use_the_metrics_they_depend_on():
    """告警表达式里必须**真的出现**这两个指标名。

    改名而告警没跟着改 ⇒ 那三条告警**永远不触发**，而"没触发"和"没问题"
    在收件箱里长得一模一样。所以这里不核对"告警文件里提到了这个名字"
    （那太松：注释里写一遍就能骗过），而是核对它出现在 expr 里。
    """
    rules = _rules()
    used = " ".join(str(r.get("expr", "")) for r in rules.values())
    for name in (drill_metrics.M_SUCCESS, drill_metrics.M_RESULT):
        assert name in used, (
            f"没有任何告警表达式用到 {name} —— 指标名与告警脱节了，"
            f"那三条告警会永远不触发"
        )


def test_no_published_metric_is_a_dead_end():
    """反向：发布出去的每个指标都得有消费者，否则它只让磁盘更脏。

    这条正是把 `backup_drill_last_attempt_timestamp_seconds` 从死指标里救回来的
    原因 —— 它在告警里没位置（Missing / Overdue / Failed 都不需要它），
    但在面板上「距上次尝试」与「距上次成功」并排，能一眼区分
    「从来没人跑过」与「有人在跑但一直失败」。少了它，
    这两种情况在面板上完全一样。
    """
    text = _consumer_text()
    dead = [n for n in _PUBLISHED if n not in text]
    assert not dead, (
        f"这些指标发布了但没有任何告警或面板在看：{dead} —— "
        f"要么给它找个消费者，要么别发（发了没人看只是噪音，还会让人以为『有监控』）"
    )


def test_consumers_only_reference_metrics_the_publisher_declares():
    """再反向一次：告警/面板里写的 `backup_drill_*` 必须是发布器声明的常量。

    挡住拼写错误 —— 面板里把 `..._last_success_...` 少写一个词，Prometheus
    不报错，那块图只会永远空白（和"最近没演练"看起来一样）。
    """
    declared = set(_PUBLISHED)
    used = set(re.findall(r"\bbackup_drill_[a-z0-9_]*\b", _consumer_text()))
    unknown = sorted(used - declared)
    assert not unknown, (
        f"告警/面板引用了发布器没声明的指标：{unknown}；"
        f"drill_metrics 声明的是 {sorted(declared)}"
    )


def test_the_three_drill_alerts_exist_with_the_expected_shape():
    rules = _rules()
    for name in _DRILL_ALERTS:
        assert name in rules, f"缺少演练告警 {name}"
        r = rules[name]
        assert r["labels"]["category"] == "platform", f"{name} 的 category 不是 platform"
        assert r["annotations"].get("runbook"), f"{name} 没有 runbook 注解（半夜收到它不知道做什么）"

    # `absent()` 必须**单独一条**：把"缺失"塞进数值比较里、用 0 兜底会让它永不触发
    # （"缺失"和"等于 0"是两种完全不同的故障）
    assert "absent(" in rules["BackupDrillMissing"]["expr"], "Missing 没有用 absent()"
    assert "absent(" not in rules["BackupDrillOverdue"]["expr"], (
        "Overdue 里不该出现 absent —— '从来没演练过'由 Missing 负责"
    )
    # Failed 必须盯"结果正好等于 0"，不能用 <1 这类会与"缺失"混淆的写法
    assert rules["BackupDrillFailed"]["expr"].strip() == "backup_drill_result == 0", (
        "Failed 的表达式变了 —— 它必须严格盯 result == 0"
    )


# --------------------------------------------------------------------------
# ② 两个脚本都要真的发布
# --------------------------------------------------------------------------
@pytest.mark.parametrize("path,target", [(PG, "pg"), (QDRANT, "qdrant")], ids=["pg", "qdrant"])
def test_backup_scripts_publish_drill_metrics(path: Path, target: str):
    text = path.read_text(encoding="utf-8")
    assert "drill_metrics" in text, f"{path.name} 没有接入演练指标发布"
    assert re.search(rf'drill_metrics\.publish\(\s*"{target}"', text), (
        f"{path.name} 调用了 publish，但 target 不是 {target!r} —— "
        f"指标 label 会错，告警按 target 过滤时会对不上"
    )


def test_publish_writes_every_declared_metric(tmp_path):
    """声明了就必须真的写进文件。

    模块常量（`M_ATTEMPT` 等）是给面板和告警看的"契约"；如果 `publish` 只是
    声明了却不写，面板上那条线永远空白 —— 而且空白和"还没演练过"长得一样。
    这里直接核**线格式**（`名字{target="pg"} 值`），不调辅助函数。
    """
    text = drill_metrics.publish("pg", True, directory=tmp_path).read_text(encoding="utf-8")
    missing = [n for n in _PUBLISHED if f'{n}{{target="pg"}} ' not in text]
    assert not missing, (
        f"publish 没有写出这些已声明的指标：{missing}\n实际内容：\n{text}"
    )


def test_publish_replaces_atomically(tmp_path, monkeypatch):
    """必须经由「写 `.tmp` → `os.replace`」，不能原地覆写正式文件。

    为什么这条只能从**行为**上钉："读到半截文件"是写入那一瞬间的状态，
    事后读文件永远读不出来（读到的要么是旧的、要么是新的）。而后果很重：
    node-exporter 是另一个进程在读它，半截内容会被 textfile collector 判为
    语法错误，并**让整批指标一起消失**（表现是"所有备份指标都不见了"，
    根因却跟备份毫无关系）。

    所以这里拦在替换动作本身上：实现一旦退回 `path.write_text(...)`，
    `os.replace` 就不会被调用，本条立刻变红。
    """
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src, dst):
        calls.append((str(src), str(dst)))
        assert str(src).endswith(".tmp"), (
            f"被替换的源是 {src} —— 不是先写 .tmp 再替换，中间态会直接落在正式文件上"
        )
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    drill_metrics.publish("pg", True, directory=tmp_path)

    assert len(calls) == 1, (
        f"publish 没有走原子替换（os.replace 被调用 {len(calls)} 次）—— "
        f"node-exporter 可能读到半截文件，那会让整批指标一起消失"
    )


def test_publish_behaviour_on_success_then_failure(tmp_path, monkeypatch):
    """行为级：跑两次（成功 → 失败），核对**线格式**。

    ⚠️ 时钟被钉成两个明显不同的值，这不是洁癖：真实时钟在同一秒内两次调用会返回
    **同一个**时间戳，于是"失败时把 last_success 刷成当前时间"这个变异不会被抓到 ——
    `assert ... == ts` 会因为第二次的 now 恰好还等于 ts 而通过。这种假绿只有把时钟
    控制住才能暴露（2026-09-30 变异验证实测踩到）。
    """
    clock = [1_000_000.0]
    monkeypatch.setattr(drill_metrics, "time", types.SimpleNamespace(time=lambda: clock[0]))

    first = drill_metrics.publish("pg", True, directory=tmp_path).read_text(encoding="utf-8")
    assert f'{drill_metrics.M_RESULT}{{target="pg"}} 1' in first, (
        f"成功的演练没有把 result 置 1：\n{first}"
    )
    assert f'{drill_metrics.M_SUCCESS}{{target="pg"}} 1000000' in first, (
        f"成功的演练没有写出 last_success：\n{first}"
    )

    clock[0] += 3600.0
    second = drill_metrics.publish("pg", False, directory=tmp_path).read_text(encoding="utf-8")
    assert f'{drill_metrics.M_RESULT}{{target="pg"}} 0' in second, "失败没有把 result 置 0"
    assert f'{drill_metrics.M_SUCCESS}{{target="pg"}} 1000000' in second, (
        "失败的演练把 last_success 覆盖或删掉了 —— '多久没成功演练过'会因此失真，"
        "而那正是这个指标存在的唯一理由"
    )
    # attempt 必须随本次调用更新：它不变的话，面板上就分不出
    # 「从来没人跑过」和「有人在跑但一直失败」
    assert f'{drill_metrics.M_ATTEMPT}{{target="pg"}} 1003600' in second, (
        f"失败的演练没有更新『距上次尝试』时间戳：\n{second}"
    )
    assert not list(tmp_path.glob("*.tmp")), (
        "留下了 .tmp 中间文件 —— node-exporter 会把 `*.tmp` 也当成指标文件去解析"
    )


# --------------------------------------------------------------------------
# ③ 指标目录必须真的会被采集
# --------------------------------------------------------------------------
def test_metrics_directory_is_the_one_node_exporter_collects(monkeypatch):
    """写到 node-exporter 看不见的目录 = 写了个没人看的文件。

    这里同时挡住一个具体的坑：textfile collector **不递归子目录**，
    所以 qdrant 的 `backups/qdrant/` 不能直接拿来当指标目录。
    """
    compose = COMPOSE.read_text(encoding="utf-8")
    m = re.search(r"-\s*\.\./\.\./([^:\s]+)\s*:\s*/textfile", compose)
    assert m, (
        "找不到 node-exporter 的 textfile 挂载 —— 演练指标写出来也没人采集"
    )
    mounted = m.group(1).strip().rstrip("/")

    monkeypatch.delenv("DRILL_METRICS_DIR", raising=False)
    default_dir = str(drill_metrics.metrics_dir()).replace("\\", "/").rstrip("/")

    assert default_dir == mounted, (
        f"演练指标默认写进 {default_dir}/，而 node-exporter 采集的是 {mounted}/ —— "
        f"两者不一致时文件写了但采集不到，三条告警全是哑弹"
    )
    # 再确认一遍它确实不是子目录写法（子目录不被递归采集）
    assert "/" not in default_dir, (
        f"指标目录 {default_dir} 看起来是子目录 —— textfile collector 不递归子目录"
    )


# --------------------------------------------------------------------------
# ④ CI 那一层
# --------------------------------------------------------------------------
def test_ci_has_a_drill_job_that_actually_runs_and_checks_the_metric():
    cfg = yaml.safe_load(CI.read_text(encoding="utf-8"))
    jobs = cfg["jobs"]
    assert "drill" in jobs, (
        "CI 里没有 drill job —— '脚本没坏'这层保障消失了，"
        "而恢复路径被改坏只会在真需要恢复的那天暴露"
    )
    job = jobs["drill"]

    # 必须定时跑：否则它跟"没人跑"没区别
    assert "schedule" in job.get("if", ""), (
        f"drill job 的 if 是 {job.get('if')!r} —— 不在定时触发里，等于不会自己跑"
    )
    # 必须起**真** PG：drill 的全部价值就在于"真的恢复一次"
    assert "pg" in (job.get("services") or {}), "drill job 没有 PG service container"
    assert "pgvector/pgvector:pg16" in str(job["services"]["pg"]["image"])

    steps = job["steps"]
    blob = "\n".join(str(s.get("run", "")) for s in steps)
    assert "pg_backup.py drill" in blob, "drill job 没有真的跑 drill"
    # 演练通过还不够：指标必须落下来，否则生产那三条告警是哑弹
    assert "drill-pg.prom" in blob, "drill job 没有校验演练指标文件"
    assert "backup_drill_result" in blob, (
        "drill job 没有断言指标内容 —— 文件存在不等于内容对"
    )
