"""任务入口与工程化配套的守卫。

为什么需要这一份
----------------
`Makefile` / `scripts/tasks.py` / `.gitattributes` / `.editorconfig` / `CHANGELOG.md`
这几样东西的共同点是：**它们都不参与运行时**，所以错了也没有任何信号 ——
直到某天有人在另一个平台上踩到，而那时距离引入问题已经过去很久。

具体要防的几类失效：

1. **本地门禁比 CI 松**：`make check` 全绿、CI 却红。反过来（本地更严）没关系。
   这里的判据是"CI 的每一步都必须在本地 check 里"，一旦 CI 加了步骤而 tasks.py
   没跟上，这条守卫就红。
2. **Makefile 与 tasks.py 不同步**：加了目标却忘了加任务（或反过来）。更阴的是
   Makefile 的 recipe 用了空格而不是 TAB —— 这条只在**真正跑 make 时**报
   "missing separator"，而开发主力平台（Windows）根本没有 make，永远发现不了。
3. **行尾规则自相矛盾**：`.gitattributes` 说 LF、`.editorconfig` 说 CRLF，
   于是每次保存都产生一次转换，`git status` 反复显示"已修改"而 diff 是空的。
4. **发版忘了写 CHANGELOG**：版本号改了、日志没补，几个月后没人说得清改了什么。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

from scripts import sync_version, tasks

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
GITATTR = ROOT / ".gitattributes"
EDITORCONFIG = ROOT / ".editorconfig"
CHANGELOG = ROOT / "CHANGELOG.md"
VERSION = ROOT / "VERSION"
CI = ROOT / ".github" / "workflows" / "ci.yml"


# --------------------------------------------------------------------------
# 解析辅助
# --------------------------------------------------------------------------
def _makefile_targets() -> set[str]:
    """Makefile 里定义的目标名。

    只认"行首即标识符、紧跟冒号且不是 `:=`"的形式 —— 这样 `PYTHON ?= python`、
    `TASKS := scripts/tasks.py`、`.PHONY: ...` 都不会被误当成目标；
    recipe 行（TAB 开头）同理不会命中。
    """
    text = MAKEFILE.read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Za-z][A-Za-z0-9_-]*):(?!=)", text, re.M))


def _ini(text: str) -> dict[str, dict[str, str]]:
    """极简 INI / editorconfig 解析（够用即可，不引第三方依赖）。"""
    out: dict[str, dict[str, str]] = {}
    cur: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1]
            out[cur] = {}
        elif cur is not None and "=" in line:
            k, v = line.split("=", 1)
            out[cur][k.strip()] = v.strip()
    return out


def _gitattribute_eol(pattern: str) -> str | None:
    for raw in GITATTR.read_text(encoding="utf-8").splitlines():
        parts = raw.split()
        if not parts or parts[0].startswith("#"):
            continue
        if parts[0] == pattern:
            for attr in parts[1:]:
                if attr.startswith("eol="):
                    return attr[len("eol="):]
    return None


def _ci_test_job() -> dict:
    cfg = yaml.safe_load(CI.read_text(encoding="utf-8"))
    assert "test" in cfg["jobs"], "CI 里没有 test job"
    return cfg["jobs"]["test"]


# --------------------------------------------------------------------------
# ① Makefile ↔ tasks.py
# --------------------------------------------------------------------------
def test_makefile_targets_all_exist_in_tasks_py():
    targets = _makefile_targets()
    assert targets, "没从 Makefile 解析出任何目标 —— 守卫本身失效了"
    unknown = sorted(targets - set(tasks.TASKS))
    assert not unknown, (
        f"Makefile 定义了 scripts/tasks.py 里没有的任务：{unknown}；"
        f"tasks.py 现有的是 {sorted(tasks.TASKS)}"
    )


def test_every_task_has_a_makefile_target():
    """反向：每个任务都要能在 Linux / CI 上用 make 调起来。"""
    missing = sorted(set(tasks.TASKS) - _makefile_targets())
    assert not missing, (
        f"这些任务没有 Makefile 目标：{missing} —— "
        f"CI 或 Linux 上就跑不到（而 Windows 上没 make，也不会有人发现）"
    )


def test_makefile_recipes_use_tabs_not_spaces():
    """recipe 必须 TAB 缩进。

    用空格时 make 报 "missing separator"，**且只在你真的跑 make 时报** ——
    本项目开发主力平台是 Windows（默认没有 make），所以这类错误可以一直潜伏。

    ⚠️ 要排除以 `\\` 结尾的**续行**：那是变量定义的延续，缩进用空格是正常的
    （`.PHONY: a b \\` 换行接着写），把它算进来就是守卫自己假红。
    """
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    bad = [
        i
        for i, line in enumerate(lines, 1)
        if line.startswith(" ")
        and line.strip()
        and not (i >= 2 and lines[i - 2].rstrip().endswith("\\"))
    ]
    assert not bad, (
        f"Makefile 第 {bad} 行以**空格**缩进 —— recipe 必须是 TAB，"
        f"否则 make 报 missing separator"
    )


def _makefile_recipes() -> dict[str, str]:
    """目标 → 它自己的 recipe 文本（TAB 开头的那几行）。

    必须**按目标分块**，不能在整个文件里搜 `$(TASKS) test`：
    `test-fast` 的 recipe 里也含 "test"，那种搜法会让"`test:` 的 recipe 被换成
    echo"这种破坏安然通过（由 `test-fast` 顶包）。变异验证时实测发现过。
    """
    out: dict[str, list[str]] = {}
    cur: str | None = None
    for raw in MAKEFILE.read_text(encoding="utf-8").splitlines():
        if re.match(r"^[A-Za-z][A-Za-z0-9_-]*:(?!=)", raw):
            cur = raw.split(":", 1)[0]
            out[cur] = []
        elif raw.startswith("\t") and cur is not None:
            out[cur].append(raw.strip())
        elif raw.strip() and not raw.startswith("#"):
            cur = None  # 变量定义 / 空行等，结束当前目标
    return {k: "\n".join(v) for k, v in out.items()}


def test_makefile_forwards_each_target_to_the_same_named_task():
    """每个目标都只是转发到同名任务，不在 Makefile 里偷偷写第二份逻辑。"""
    recipes = _makefile_recipes()
    missing = [
        t
        for t in sorted(_makefile_targets())
        if not re.search(rf"\$\(TASKS\)\s+{re.escape(t)}(?=\s|$)", recipes.get(t, ""))
    ]
    assert not missing, (
        f"这些目标自己的 recipe 没有转发到同名任务：{missing} —— "
        f"Makefile 里出现了第二份逻辑，两边迟早不一致"
    )


# --------------------------------------------------------------------------
# ② 本地门禁不得比 CI 松
# --------------------------------------------------------------------------
def _norm(cmd: str) -> str:
    cmd = re.sub(r"^\s*python(?:3)?\s+-m\s+", "", cmd.strip())
    return re.sub(r"\s+", " ", cmd)


def _ci_fingerprints(job: dict) -> set[str]:
    """CI 这一步到底"做了哪些事"：脚本文件 + pytest。

    不做整条命令的字符串比对 —— CI 会带自己的参数（`-p no:cacheprovider` 之类），
    那样比对会在无关紧要的地方假红，假红多了这条守卫就会被删掉。
    """
    out: set[str] = set()
    for step in job["steps"]:
        run = step.get("run") or ""
        if "pip install" in run:
            continue
        out |= set(re.findall(r"scripts/[\w./-]+\.py", run))
        if re.search(r"\bpytest\b", run):
            out.add("pytest tests/")
    return out


def test_local_check_covers_every_step_of_the_ci_test_job():
    """CI 跑的每一件事，本地 `check` 都要跑。

    方向很关键：**本地比 CI 严没问题**（多跑一步），**比 CI 松才是事故** ——
    那意味着"本地全绿、推上去才红"，而 CI 的反馈周期是分钟级。
    """
    local = "\n".join(cmd for _, cmd in tasks.CHECK_STEPS)
    missing = sorted(f for f in _ci_fingerprints(_ci_test_job()) if f not in local)
    assert not missing, (
        f"CI 的 test job 跑了这些，但本地 check 没跑：{missing}\n"
        f"本地 check 当前步骤：{[c for _, c in tasks.CHECK_STEPS]}"
    )


def test_check_and_drill_steps_point_at_files_that_exist():
    """步骤表里引用的脚本必须真实存在。

    脚本改名 / 移动后，任务入口会指向一个不存在的文件 —— 而这同样只在**跑它时**
    才发现，且报错是子进程的 "No such file or directory"，很难一眼看出是入口过期。
    """
    missing: list[str] = []
    for _, cmd in (*tasks.CHECK_STEPS, *tasks.DRILL_STEPS):
        for rel in re.findall(r"(scripts/[\w./-]+\.py)", cmd):
            if not (ROOT / rel).is_file():
                missing.append(f"{rel}（出现在：{cmd}）")
    assert not missing, f"任务入口引用了不存在的脚本：{missing}"


# --------------------------------------------------------------------------
# ③ 任务入口自身的行为
# --------------------------------------------------------------------------
def test_help_lists_every_task(capsys):
    assert tasks.main(["help"]) == 0
    out = capsys.readouterr().out
    missing = sorted(t for t in tasks.TASKS if t not in out)
    assert not missing, f"help 里没有列出：{missing}"


def test_unknown_task_fails_loudly(capsys):
    rc = tasks.main(["definitely-not-a-task"])
    assert rc != 0, "未知任务返回了 0 —— 打错字会被当成成功，脚本里接不住的静默失败"
    assert "definitely-not-a-task" in capsys.readouterr().err


def test_run_uses_the_current_interpreter(monkeypatch):
    """`python` / `pytest` 都必须落到**当前解释器**上。

    裸 `python` 会解析到 PATH 里另一个解释器（"我明明装了依赖却说没有"）；
    `pytest` 是 console script，只 `pip install` 进某个环境时它**不在 PATH 里**
    （"明明装了却找不到命令"）。两种情况都让人以为是环境坏了。
    """
    seen: list[list[str]] = []

    class _Done:
        returncode = 0

    monkeypatch.setattr(tasks.subprocess, "run", lambda argv, cwd=None: seen.append(list(argv)) or _Done())

    tasks.run("python scripts/sync_version.py --check")
    tasks.run("pytest tests/ -q -p no:cacheprovider")
    tasks.run(["docker", "compose", "ps"])

    assert seen[0] == [sys.executable, "scripts/sync_version.py", "--check"]
    assert seen[1][:3] == [sys.executable, "-m", "pytest"], (
        f"pytest 没有走 `python -m pytest`，而是 {seen[1][:3]}"
    )
    assert seen[2] == ["docker", "compose", "ps"], "非 Python 命令不该被改写"


def test_run_reports_failure_without_raising(monkeypatch, capsys):
    """外部命令失败时返回退出码而不是抛异常 —— 调用方要能决定"停下还是继续"。"""
    class _Fail:
        returncode = 3

    monkeypatch.setattr(tasks.subprocess, "run", lambda argv, cwd=None: _Fail())
    assert tasks.run("python -c pass") == 3
    assert "退出码 3" in capsys.readouterr().err


# --------------------------------------------------------------------------
# ④ 行尾规则：两个文件必须说同一件事
# --------------------------------------------------------------------------
def test_gitattributes_pins_everything_to_lf():
    assert _gitattribute_eol("*") == "lf", (
        "`.gitattributes` 的 `*` 规则不是 eol=lf —— 本机 core.autocrlf 是 true，"
        "不钉死的话仓库里的字节会随机器而变"
    )


def test_editorconfig_agrees_with_gitattributes_on_line_endings():
    """两个文件对**同一批文件**必须给出一致的行尾要求。

    不一致的后果很隐蔽：编辑器按 .editorconfig 存 CRLF、git 按 .gitattributes
    入库转 LF，于是工作区与索引永远对不上 —— `git status` 一直显示"已修改"、
    `git diff` 却是空的，或者反过来整个文件被标红。
    """
    ec = _ini(EDITORCONFIG.read_text(encoding="utf-8"))
    assert ec.get("*", {}).get("end_of_line") == _gitattribute_eol("*"), (
        f".editorconfig 的 [*] end_of_line={ec.get('*', {}).get('end_of_line')!r} "
        f"与 .gitattributes 的 {_gitattribute_eol('*')!r} 不一致"
    )

    # Windows 专用脚本两边都要 CRLF
    for pat in ("*.bat", "*.cmd", "*.ps1"):
        want = _gitattribute_eol(pat)
        assert want == "crlf", f".gitattributes 里 {pat} 的 eol 应为 crlf，实际 {want!r}"
    ec_bat = next((v for k, v in ec.items() if "bat" in k or "cmd" in k), {})
    assert ec_bat.get("end_of_line") == "crlf", (
        f".editorconfig 没有把 Windows 脚本段设为 crlf：{[k for k in ec]}"
    )


def test_shell_scripts_are_not_pinned_to_crlf():
    """`.sh` 绝不能被钉成 CRLF。

    容器里的 `/bin/bash` 遇到 CRLF 会报 `bad interpreter: /usr/bin/env bash^M`，
    而这类故障**只在容器里出现**（Window 上用 Git Bash 跑同一份脚本往往正常）。
    """
    eol = _gitattribute_eol("*.sh")
    assert eol != "crlf", "`.sh` 被钉成 CRLF —— 容器里会报 bad interpreter"


# --------------------------------------------------------------------------
# ⑤ CHANGELOG 与版本号
# --------------------------------------------------------------------------
def test_changelog_has_an_entry_for_the_current_version():
    version = VERSION.read_text(encoding="utf-8").strip()
    text = CHANGELOG.read_text(encoding="utf-8")
    assert f"## [{version}]" in text, (
        f"CHANGELOG.md 里没有 `## [{version}]` 这一条 —— "
        f"版本号改了却没写日志，几个月后没人说得清这版改了什么"
    )


def test_changelog_version_headings_are_valid_semver():
    text = CHANGELOG.read_text(encoding="utf-8")
    headings = re.findall(r"^## \[([^\]]+)\]", text, re.M)
    assert headings, "CHANGELOG 里一个版本标题都没有"
    bad = [h for h in headings if h != "Unreleased" and not sync_version.SEMVER_RE.match(h)]
    assert not bad, f"CHANGELOG 里这些标题不是合法语义化版本：{bad}"


def test_changelog_mentions_the_version_file_as_the_single_source():
    """CHANGELOG 要告诉读者"版本号从哪儿改"，否则下一个人会直接改 compose 里的 tag。"""
    text = CHANGELOG.read_text(encoding="utf-8")
    assert "VERSION" in text, "CHANGELOG 没有指向 VERSION 文件（版本号的唯一真源）"
