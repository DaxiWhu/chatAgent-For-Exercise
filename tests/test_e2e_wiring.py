"""E2E 骨架的守卫。

为什么需要这一份
----------------
E2E 文件本身**默认不跑**（没设 `E2E_BASE_URL` 就整份跳过），所以它坏掉的时候
不会有任何信号 —— 这正是最危险的一类"看起来有保障其实没有"：

- 打的路径写错了 ⇒ 等到某天有人真去跑才发现，而那时通常正在排查别的问题；
- `tasks.py` 里写的服务名不是 compose 里的服务名 ⇒ `up -d` 报 "no such service"；
- 默认地址的端口与 compose 里 nginx 的映射不一致 ⇒ 永远连不上，看上去像"栈没起来"；
- E2E 用例把 `-m e2e` 忘在某个用例上 ⇒ 它会混进默认运行，于是**所有人**的
  `pytest tests/` 都开始红（接着这份文件会被删掉，而不是被修好）。

这里把上面每一条都静态钉住，剩下"真起栈跑一遍"的部分交给 CI 的手动/定时 job
（`ci.yml` 的 `e2e`）。
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import yaml

from scripts import tasks

ROOT = Path(__file__).resolve().parents[1]
E2E_FILE = ROOT / "tests" / "test_e2e_stack.py"
COMPOSE = ROOT / "deploy" / "app" / "docker-compose.yml"
SNAPSHOT = ROOT / "tests" / "snapshots" / "openapi.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
PYTEST_INI = ROOT / "pytest.ini"


def _load_e2e_module():
    """把 E2E 文件当普通模块加载 —— **同进程**，检查真实的 marker 对象。

    ⚠️ 这里刻意**不**用"起一个 pytest 子进程跑一遍"的做法（那是最直观的行为断言）。
    实测：在本机的运行环境里，**pytest 自己报告的耗时是 0.07~0.08s，而进程退出要
    23 秒** —— 那是外层沙箱对"由测试启动的子进程"做事后审计造成的（`--collect-only`
    也一样慢，与 tmp 目录、`--basetemp` 都无关）。一条守卫不该给每次全量测试平白
    加 24 秒，否则它迟早被注释掉。

    同进程加载检查的是**真实对象**（`pytestmark` 里的 MarkDecorator），不是源码文本：
    "把模块级 skipif 删掉"或"把条件写反"都会被下面两条抓住。
    """
    spec = importlib.util.spec_from_file_location("_e2e_stack_probe", E2E_FILE)
    assert spec and spec.loader, f"无法加载 {E2E_FILE}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# ① 默认必须跳过（否则它会污染每个人的日常测试）
# --------------------------------------------------------------------------
def test_module_level_skipif_is_wired_to_the_base_url(monkeypatch):
    """没设 `E2E_BASE_URL` 时，模块级 skip 条件必须求值为真。

    这是**行为级**断言：真的在无该变量的环境下加载模块，再看 skip 条件算出来是什么。
    要防的是"某天有人把 skipif 去掉 / 条件写反 / 引用成别的变量"——那样默认的
    `pytest tests/` 就会去连一个不存在的栈，于是所有人的本地测试都开始红
    （接着这份文件会被删掉，而不是被修好）。
    """
    monkeypatch.delenv("E2E_BASE_URL", raising=False)
    mod = _load_e2e_module()

    skipifs = [m for m in mod.pytestmark if getattr(m, "name", "") == "skipif"]
    assert skipifs, (
        "E2E 文件没有模块级 skipif —— 默认的 `pytest tests/` 会去连一个不存在的栈"
    )
    cond = skipifs[0].args[0]
    assert cond is True, (
        f"没设 E2E_BASE_URL 时 skip 条件应当是 True，实际是 {cond!r} —— "
        f"条件写反或引用了别的变量，都会让 E2E 在默认运行里真的去连栈"
    )
    reason = str(skipifs[0].kwargs.get("reason", ""))
    assert "E2E_BASE_URL" in reason, (
        f"skip 理由里没有告诉人怎么跑（{reason!r}）—— "
        f"被跳过的东西如果不出手指引，就等于不存在"
    )


def test_every_case_is_a_plain_top_level_function():
    """用例必须是顶层 `def test_*`：类里的方法不会被模块级 `pytestmark` 覆盖
    （类要走类上的 marker），漏一个就会混进默认运行。"""
    mod = _load_e2e_module()
    cases = [n for n, v in vars(mod).items() if n.startswith("test_") and callable(v)]
    assert cases, "没解析出任何用例 —— 守卫自身失效了"

    classes = [n for n, v in vars(mod).items() if isinstance(v, type) and n.startswith("Test")]
    assert not classes, (
        f"E2E 文件里出现了测试类 {classes} —— 模块级 pytestmark 覆盖不到它们的方法"
    )


def test_every_e2e_case_carries_the_marker():
    text = E2E_FILE.read_text(encoding="utf-8")
    assert "pytest.mark.e2e" in text, "E2E 文件没有用 e2e marker —— `-m e2e` 会筛不出东西"
    assert "pytestmark" in text, (
        "marker 没有**模块级**应用（pytestmark）—— 靠逐个用例手写迟早漏一个"
    )


def test_pytest_ini_registers_the_e2e_marker():
    assert PYTEST_INI.is_file(), "没有 pytest.ini —— e2e marker 未注册（会有 UnknownMark 警告）"
    text = PYTEST_INI.read_text(encoding="utf-8")
    assert re.search(r"^\s*e2e:", text, re.M), "pytest.ini 里没有注册 e2e marker"


# --------------------------------------------------------------------------
# ② 打的端点、起的服务、连的地址：都必须和真实部署对得上
# --------------------------------------------------------------------------
def _e2e_paths() -> set[str]:
    text = E2E_FILE.read_text(encoding="utf-8")
    return {m for m in re.findall(r'"(/[A-Za-z0-9/_.\-]*)"', text) if len(m) > 1}


def test_e2e_hits_only_paths_that_exist():
    """E2E 打的每个路径都必须真实存在（用契约快照当事实源）。

    写错一个字母的路径，E2E 会**在真起栈之后**才报 404 —— 而那时你已经花了几分钟
    起容器，且第一反应是"服务坏了"。
    """
    paths = _e2e_paths()
    assert "/health/live" in paths, f"没从 E2E 文件里提取到路径（守卫自身失效）：{paths}"

    known = set(json.loads(SNAPSHOT.read_text(encoding="utf-8"))["paths"])
    unknown = sorted(p for p in paths if p not in known)
    assert not unknown, (
        f"E2E 打的这些路径不在接口契约里：{unknown}\n"
        f"契约里现有：{sorted(known)}"
    )


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_e2e_services_exist_in_compose():
    """`E2E_SERVICES` 里每个名字都得是 compose 里真实的服务名。

    写错时 `docker compose up -d` 直接报 "no such service"，而错误信息里
    只会点名那一个 —— 起栈失败看着像"栈坏了"。
    """
    services = _compose()["services"]
    missing = [s for s in tasks.E2E_SERVICES.split() if s not in services]
    assert not missing, (
        f"E2E_SERVICES 里这些不是 compose 的服务名：{missing}\n"
        f"compose 现有：{sorted(services)}"
    )


def test_e2e_default_base_url_matches_the_nginx_port_mapping():
    """默认地址的端口必须与 compose 里 nginx 的宿主机端口一致。

    不一致时表现为"永远连不上"，而 E2E 的失败信息会把人引向"栈没起来"这个错误方向。
    """
    ports = _compose()["services"]["nginx"]["ports"]
    host_port = None
    for entry in ports:
        m = re.match(r"^\s*(\d+):(\d+)\s*$", str(entry))
        if m:
            host_port = m.group(1)
            break
    assert host_port, f"nginx 没有 `宿主机:容器` 形式的端口映射，实际是 {ports}"

    assert f":{host_port}" in tasks.E2E_DEFAULT_BASE, (
        f"compose 把 nginx 映射到宿主机的 {host_port}，"
        f"而 E2E 的默认地址是 {tasks.E2E_DEFAULT_BASE}"
    )


# --------------------------------------------------------------------------
# ③ CI 里必须有一条能跑到它的路径
# --------------------------------------------------------------------------
def test_ci_has_an_e2e_job_that_runs_the_same_entry_point():
    cfg = yaml.safe_load(CI.read_text(encoding="utf-8"))
    jobs = cfg["jobs"]
    assert "e2e" in jobs, (
        "CI 里没有 e2e job —— 这份骨架就永远没被端到端跑过一次，"
        "它会在第一次真需要时才发现是坏的"
    )
    blob = "\n".join(str(s.get("run", "")) for s in jobs["e2e"]["steps"])
    assert "tasks.py e2e" in blob or "test_e2e_stack.py" in blob, (
        f"e2e job 没有跑这份检查，它的步骤是：\n{blob}"
    )


def test_ci_e2e_job_is_manual_or_scheduled_not_per_push():
    """E2E 刻意**不**放在每次 push 上。

    起全套容器要几分钟，而它防的是"部署面悄悄漂了"（按天发现足够），不是
    "这次改动错了"（那是 test job 的活）。若被改成每次 push 都跑，
    整条 CI 的反馈周期会被它拖长，接着它会被很自然地"临时注释掉"。
    """
    cfg = yaml.safe_load(CI.read_text(encoding="utf-8"))
    cond = cfg["jobs"]["e2e"].get("if", "")
    assert "schedule" in cond or "workflow_dispatch" in cond, (
        f"e2e job 的 if 是 {cond!r} —— 它应该在手动触发或定时里跑"
    )

    triggers = cfg.get("on") or cfg.get(True) or {}  # YAML 1.1 会把裸 `on:` 读成 True
    assert "workflow_dispatch" in triggers, (
        "workflow 没有声明 workflow_dispatch —— e2e 就只剩定时这一条触发路径，"
        "改了 nginx / compose 之后想立刻验证一下都做不到"
    )
