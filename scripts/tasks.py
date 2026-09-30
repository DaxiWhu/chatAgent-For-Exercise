#!/usr/bin/env python
"""任务入口 —— 一条命令做一件事。

为什么不是「只用 Makefile」
--------------------------
`make` 在 Windows 上**默认不存在**（Git for Windows 不带，要额外装），而本项目的
主力开发环境就是 Windows。把任务只写进 Makefile，等于让 Windows 上的开发者靠记忆
敲一长串命令 —— 而"记不住就跳过"，正是所有运维动作衰减的起点（RUNBOOK 里那句
"每季度演练一次"就是这么失效的）。

所以真正的逻辑放在这里：**纯标准库、跨平台**，Windows 上直接
`python scripts/tasks.py test` 就能跑。`Makefile` 只是给 CI / Linux / WSL 用的一层
薄转发（每个目标一行，逻辑不重复）。两边接线是否一致由
`tests/test_tasks_entrypoint.py` 钉住。

用法
----
    python scripts/tasks.py help                     # 列出全部任务
    python scripts/tasks.py test                     # 跑全量测试
    python scripts/tasks.py test -- -k version       # `--` 之后的参数原样透传
    python scripts/tasks.py check                     # 提交前门禁
    python scripts/tasks.py drill                     # 恢复演练
    python scripts/tasks.py up                        # 起本地栈
    python scripts/tasks.py release -- <镜像tag> --dry-run

退出码：0 成功；非 0 即失败（透传被调用命令的退出码，CI 可直接用）。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
COMPOSE_FILE = "deploy/app/docker-compose.yml"

# 用**当前解释器**而不是裸 `python`：调用方可能来自 conda 环境或 venv，
# 裸 `python` 会解析到另一个解释器（"装了依赖却说没装"）。
PY = sys.executable

# 会被 `run()` 替换成真实解释器的占位词
_PY_ALIASES = {"python", "python3", "py"}
# 这些是 console script：**未必在 PATH 里**（比如 pytest 只用 `pip install` 进了某个
# conda 环境，可执行文件在别处）。统一改成 `python -m <name>`，用当前解释器跑，
# 才不会出现"明明装了却说找不到命令"。
_MODULE_ALIASES = {"pytest", "ruff", "mypy", "pip"}


# --------------------------------------------------------------------------
# 与 CI 逐字对齐的步骤表
#
# ⚠️ 刻意做成**数据结构**而不是散落在函数里：`tests/test_tasks_entrypoint.py`
#    会拿它跟 `.github/workflows/ci.yml` 的 test job 逐条比对。"本地门禁"和
#    "CI 门禁"漂移是很隐蔽的失效 —— 本地 `make check` 全绿、CI 却红（或反过
#    来：CI 早就已经不检查某样东西了，而没人发现）。
# --------------------------------------------------------------------------
CHECK_STEPS: tuple[tuple[str, str], ...] = (
    ("版本号副本与 VERSION 一致", "python scripts/sync_version.py --check"),
    ("依赖锁与 requirements.txt 未漂移", "python scripts/lock_requirements.py"),
    ("全量测试（与 CI 的 test job 同款命令）", "pytest tests/ -q -p no:cacheprovider"),
)

DRILL_STEPS: tuple[tuple[str, str], ...] = (
    ("PG：备份 → 恢复到临时库 → 逐表比对", "python scripts/pg_backup.py drill"),
    ("Qdrant：快照 → 校验（无服务时跳过）", "python scripts/qdrant_backup.py drill"),
)

# 端到端检查需要的最小服务集。
# 刻意**不**用 `up -d` 起全部：备份循环与监控栈（prometheus/grafana/alertmanager）
# 对这里的检查没有贡献，却会明显拖慢启动、占用内存，还可能因为缺配置而起不来 ——
# 那会让"E2E 失败"看起来像是业务代码坏了。
E2E_SERVICES = "pgvector qdrant redis app-stable app-canary nginx"
E2E_DEFAULT_BASE = "http://127.0.0.1:8080"


# --------------------------------------------------------------------------
# 执行辅助
# --------------------------------------------------------------------------
def run(cmd: str | list[str], *, env: dict[str, str] | None = None) -> int:
    """打印并执行一条命令，返回退出码。

    ⚠️ 用列表形式传给 subprocess（`shell=False`）：命令里含路径与用户输入，
    拼成字符串再交给 shell 会在含空格/引号的路径上静默拆错参数。
    """
    argv = shlex.split(cmd, posix=True) if isinstance(cmd, str) else list(cmd)
    if argv:
        if argv[0] in _PY_ALIASES:
            argv[0] = PY
        elif argv[0] in _MODULE_ALIASES:
            argv = [PY, "-m", argv[0], *argv[1:]]
    print(f"\n$ {cmd if isinstance(cmd, str) else ' '.join(argv)}", flush=True)
    rc = subprocess.run(argv, cwd=ROOT, env=env).returncode
    if rc != 0:
        print(f"\n✗ 命令失败（退出码 {rc}）", file=sys.stderr, flush=True)
    return rc


def _steps(steps: tuple[tuple[str, str], ...], extra: list[str]) -> int:
    """顺序跑一组步骤，第一个失败就停。"""
    for i, (label, cmd) in enumerate(steps, 1):
        print(f"\n[{i}/{len(steps)}] {label}", flush=True)
        rc = run(cmd + (" " + " ".join(extra) if extra else ""))
        if rc != 0:
            return rc
    print(f"\n✓ {len(steps)} 步全部通过", flush=True)
    return 0


def require(tool: str, hint: str) -> str | None:
    """确认外部命令存在，返回可执行文件路径；不存在时给出**可操作的**提示。"""
    path = shutil.which(tool)
    if not path:
        print(
            f"✗ 找不到 `{tool}`。\n  {hint}",
            file=sys.stderr, flush=True,
        )
    return path


def compose(args: str, extra: list[str], *, profile: str | None = None) -> int:
    """转发给 docker compose。

    ⚠️ 必须显式带 `--env-file .env`：compose 在**没有** `--env-file` 时是从
    「第一个 `-f` 文件所在目录」找 `.env` 的 —— 而这里是 `-f deploy/app/...`，
    于是仓库根的 `.env` **不会**参与插值，compose 会拿空值去解析
    `${SOME_SECRET:?}` 之类的必填项并直接报错（或更糟：用默认值静默起错）。
    """
    if not require("docker", "需要 Docker Desktop（或 WSL 里的 docker），并且 `docker` 在 PATH 里。"):
        return 127
    parts = ["docker", "compose", "--env-file", ".env", "-f", COMPOSE_FILE]
    if profile:
        parts += ["--profile", profile]
    parts += shlex.split(args) + extra
    return run(parts)


# --------------------------------------------------------------------------
# 任务实现
# --------------------------------------------------------------------------
def task_setup(args: list[str]) -> int:
    """装依赖（用 lock 文件约束版本，保证可复现）。"""
    if not (ROOT / ".env").exists():
        print(
            "提示：还没有 .env。先 `cp .env.example .env` 并填好必填项，"
            "否则服务能起但会在运行时才报缺少配置。",
            flush=True,
        )
    return run(f"python -m pip install -r requirements.txt -c requirements.lock")


def task_test(args: list[str]) -> int:
    """跑全量测试（`--` 之后的参数原样透传给 pytest）。"""
    return run("pytest tests/ -q" + (" " + " ".join(args) if args else ""))


def task_test_fast(args: list[str]) -> int:
    """只重跑上次失败的用例（本地快速迭代用；CI 不用它）。"""
    return run("pytest tests/ -q --lf" + (" " + " ".join(args) if args else ""))


def task_check(args: list[str]) -> int:
    """提交前门禁 —— 与 CI 的 test job 等价（见 CHECK_STEPS 的说明）。"""
    return _steps(CHECK_STEPS, args)


def task_version(args: list[str]) -> int:
    """打印当前版本（唯一真源是仓库根的 VERSION 文件）。"""
    try:
        print(VERSION_FILE.read_text(encoding="utf-8").strip())
    except OSError as exc:
        print(f"✗ 读不到 {VERSION_FILE.name}：{exc}", file=sys.stderr)
        return 1
    return 0


def task_sync_version(args: list[str]) -> int:
    """把 VERSION 同步到 compose / .env.example 里的副本。"""
    return run("python scripts/sync_version.py")


def task_openapi(args: list[str]) -> int:
    """重新生成 OpenAPI 契约快照（确认接口变更后用）。"""
    return run("python scripts/snapshot_openapi.py --write")


def task_lock(args: list[str]) -> int:
    """从当前已装环境重新生成依赖锁（确认依赖变更且测试通过后用）。"""
    return run("python scripts/lock_requirements.py --write")


def task_backup_pg(args: list[str]) -> int:
    """备份 PostgreSQL。"""
    return run("python scripts/pg_backup.py backup")


def task_backup_qdrant(args: list[str]) -> int:
    """备份 Qdrant。"""
    return run("python scripts/qdrant_backup.py backup")


def task_drill(args: list[str]) -> int:
    """恢复演练 —— 唯一能证明「备份可用」的操作。

    结果会落进 `backups/drill-<target>.prom`，node-exporter 采集后由
    `BackupDrillMissing` / `BackupDrillOverdue` / `BackupDrillFailed` 三条告警盯着。
    **不跑它，那三条告警就会一直响** —— 这是刻意的。
    """
    return _steps(DRILL_STEPS, args)


def task_canary(args: list[str]) -> int:
    """看灰度判据（退出码即结论：0 推进 / 1 回滚 / 2 数据不足转人工）。"""
    return run("python scripts/canary_check.py" + (" " + " ".join(args) if args else ""))


def task_up(args: list[str]) -> int:
    """起本地栈（app + pg + qdrant + redis + nginx + 告警出口）。"""
    return compose("up -d", args)


def task_up_observability(args: list[str]) -> int:
    """起本地栈**含监控**（Prometheus / Grafana / Loki，属 observability profile）。"""
    return compose("up -d", args, profile="observability")


def task_down(args: list[str]) -> int:
    """停本地栈（保留数据卷）。"""
    return compose("down", args)


def task_ps(args: list[str]) -> int:
    """看本地栈各服务状态。"""
    return compose("ps", args)


def task_logs(args: list[str]) -> int:
    """跟踪本地栈日志（默认跟全部服务；`-- <服务名>` 可只看一个）。"""
    return compose("logs -f --tail=100", args)


def _dotenv_value(key: str) -> str | None:
    """从仓库根的 `.env` 读一个键（只取字面值，不做 `${VAR}` 插值）。"""
    try:
        for raw in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip().strip("'\"")
    except OSError:
        return None
    return None


def _e2e_api_key() -> str | None:
    """推断这个栈用的是哪个 Key，好让 E2E 的鉴权用例自动变成"验证拒未授权"。

    优先 `TENANT_KEYS` 的第一个（形如 `acme:<key>,globex:<key>`），其次 `SERVE_API_KEY`；
    都取不到就返回 None —— 此时那条用例会断言"**应当放行**"（两个方向都有断言，没有 skip）。
    """
    tenant_keys = _dotenv_value("TENANT_KEYS")
    if tenant_keys:
        first = tenant_keys.split(",")[0]
        if ":" in first:
            return first.split(":", 1)[1].strip() or None
    return _dotenv_value("SERVE_API_KEY") or None


def _wait_ready(base: str, timeout: float) -> bool:
    """轮询 `/health/live` 直到 200。

    等**存活**探针而不是就绪探针：刚 `up` 完时 PG 往往还在初始化，就绪探针会失败，
    但"HTTP 服务已经起来了"才是这里要等的；依赖到底通没通，交给 E2E 自己的就绪用例去报
    （那条报错才有明细可看）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base + "/health/live", timeout=3) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(2)
    return False


def task_e2e(args: list[str]) -> int:
    """端到端检查：起真实栈 → 等就绪 → 跑 `tests/test_e2e_stack.py` → 收栈。

    ⚠️ 需要 docker，并会占用 8080（nginx）等端口。跑完**默认收栈**；
    要保留现场排障就加 `--keep`。
    检查内容见 `tests/test_e2e_stack.py` 的模块文档（SSE 分帧 / 灰度权重 / 持久化检查点）。
    """
    if not require(
        "docker",
        "端到端检查要起真实栈，需要 Docker Desktop（或 WSL 里的 docker）且 `docker` 在 PATH 里。",
    ):
        return 127

    keep = "--keep" in args
    build = "--build" in args
    base = os.environ.get("E2E_BASE_URL") or E2E_DEFAULT_BASE
    wait = float(os.environ.get("E2E_WAIT", "240"))

    # `--build` 让"镜像构建失败"与"起栈失败"分开定位 —— CI 里首次跑必然要构建，
    # 若不显式 build，构建错误会混在 `up` 的输出里，看起来像"服务起不来"。
    print(f"[e2e] 起栈：{E2E_SERVICES}{'（先构建镜像）' if build else ''}", flush=True)
    if compose(f"up -d{' --build' if build else ''} {E2E_SERVICES}", []) != 0:
        return 1

    print(f"[e2e] 等待 {base}/health/live 就绪（最多 {wait:.0f}s）…", flush=True)
    if not _wait_ready(base, wait):
        print("[e2e] ✗ 栈没有在预期时间内起来。app-stable 日志尾部：", file=sys.stderr, flush=True)
        compose("logs --tail=60 app-stable", [])
        if not keep:
            compose("down", [])
        return 1

    env = dict(os.environ, E2E_BASE_URL=base)
    key = os.environ.get("E2E_API_KEY") or _e2e_api_key()
    if key:
        env["E2E_API_KEY"] = key
        print("[e2e] 检测到该栈要求鉴权，已自动带上 E2E_API_KEY", flush=True)

    print("[e2e] 跑检查：tests/test_e2e_stack.py", flush=True)
    rc = run(["pytest", "tests/test_e2e_stack.py", "-q", "-m", "e2e"], env=env)

    if rc != 0:
        print("[e2e] 检查未通过。app-stable / nginx 日志尾部：", file=sys.stderr, flush=True)
        compose("logs --tail=60 app-stable nginx", [])

    if keep:
        print(f"[e2e] --keep：栈保留着（{base}）。收栈用 `python scripts/tasks.py down`", flush=True)
    else:
        print("[e2e] 收栈…", flush=True)
        compose("down", [])
    return rc


def _bash_script(rel: str, args: list[str]) -> int:
    if not require("bash", "这两个脚本是 bash 写的，需要 Git Bash 或 WSL 环境。"):
        return 127
    return run(["bash", str(ROOT / rel), *args])


def task_release(args: list[str]) -> int:
    """灰度发布：改权重 → 观察窗 → 按判据决定推进/回滚/转人工（需给镜像 tag）。"""
    return _bash_script("deploy/release.sh", args)


def task_rollback(args: list[str]) -> int:
    """回滚到指定镜像。"""
    return _bash_script("deploy/rollback.sh", args)


def task_clean(args: list[str]) -> int:
    """清理构建缓存（默认只列出；加 `--yes` 才真删）。"""
    targets: list[Path] = []
    for pat in ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"):
        targets += [p for p in ROOT.rglob(pat) if p.is_dir()]
    targets = [p for p in targets if ".workbuddy" not in p.parts]

    if not targets:
        print("没有需要清理的缓存目录。")
        return 0

    total = sum(sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) for p in targets)
    print(f"将清理 {len(targets)} 个缓存目录（约 {total / 1e6:.1f} MB）：")
    for p in sorted(targets):
        print(f"  {p.relative_to(ROOT)}")

    if "--yes" not in args:
        print("\n（这是预演。确认无误后加 `--yes` 真正删除。）")
        return 0

    for p in targets:
        shutil.rmtree(p, ignore_errors=True)
    print(f"\n✓ 已清理 {len(targets)} 个目录")
    return 0


def task_help(args: list[str]) -> int:
    print(__doc__.strip().split("\n\n")[0])
    print("\n任务：")
    width = max(len(n) for n in TASKS)
    for name, (summary, _) in sorted(TASKS.items()):
        print(f"  {name.ljust(width)}  {summary}")
    print(
        "\n`--` 之后的参数会原样透传给被调用的工具，例如：\n"
        "  python scripts/tasks.py test -- -k version\n"
        "  python scripts/tasks.py logs -- app-stable"
    )
    return 0


# 名字 → (一句话说明, 实现)。守卫会核对它和 Makefile 的目标一一对应。
TASKS: dict[str, tuple[str, Callable[[list[str]], int]]] = {
    "help": ("列出全部任务", task_help),
    "setup": ("安装依赖（受 requirements.lock 约束）", task_setup),
    "test": ("跑全量测试", task_test),
    "test-fast": ("只重跑上次失败的用例", task_test_fast),
    "check": ("提交前门禁（与 CI 的 test job 等价）", task_check),
    "version": ("打印当前版本", task_version),
    "sync-version": ("把 VERSION 同步到各副本", task_sync_version),
    "openapi": ("重新生成 OpenAPI 契约快照", task_openapi),
    "lock": ("重新生成依赖锁", task_lock),
    "backup-pg": ("备份 PostgreSQL", task_backup_pg),
    "backup-qdrant": ("备份 Qdrant", task_backup_qdrant),
    "drill": ("恢复演练（结果落指标，供告警消费）", task_drill),
    "canary": ("查看灰度判据（退出码即结论）", task_canary),
    "e2e": ("端到端检查：起真实栈跑 SSE/灰度/持久化验证", task_e2e),
    "up": ("起本地栈", task_up),
    "up-observability": ("起本地栈（含监控面板）", task_up_observability),
    "down": ("停本地栈", task_down),
    "ps": ("看本地栈状态", task_ps),
    "logs": ("跟踪本地栈日志", task_logs),
    "release": ("灰度发布（bash）", task_release),
    "rollback": ("回滚镜像（bash）", task_rollback),
    "clean": ("清理构建缓存（默认预演）", task_clean),
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("help", "-h", "--help"):
        return task_help([])

    name, *extra = argv
    # 允许 `--` 分隔（Makefile 里传 `$(ARGS)` 时用得上），去掉它本身
    if extra and extra[0] == "--":
        extra = extra[1:]

    entry = TASKS.get(name)
    if entry is None:
        print(f"✗ 未知任务 {name!r}\n", file=sys.stderr)
        task_help([])
        return 2

    return entry[1](extra)


if __name__ == "__main__":
    sys.exit(main())
