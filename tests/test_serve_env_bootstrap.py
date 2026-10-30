"""启动入口的 `.env` 加载顺序：必须**先于** `app.config` 的导入期求值。

这条守的是一个"单测全绿、真实入口必挂"的缺陷（2026-10-30 实机复现）
------------------------------------------------------------------
只写 `.env`（不 export 任何变量）时，`python -m app.serve` 报

    RuntimeError: agent 链路不可用（missing_api_key）⇒ 拒绝启动

而 `.env` 里 `AGNES_API_KEY` 是有值的。成因不是配置写错，是顺序：

  1. `main()` 里 `resolve_graceful_timeout()` → `_default_graceful_timeout()`
     → `from app import config`，这一步在 `uvicorn.run()` **之前**；
  2. `app/config.py` 的常量是模块级求值，导入瞬间就把空 Key 固化了；
  3. 之后 uvicorn 导入 `app.main`、其中的 `load_dotenv()` 再执行 —— 已经晚了，
     `app.config` 在 `sys.modules` 里不会重新求值。

为什么测试没抓到：测试与探针走的是 `import app.main`（`load_dotenv()` 恰好排在
`from . import config` 之前），根本不经过 `resolve_graceful_timeout`。
**"测试路径与启动路径不同源"** —— 所以这里必须用**子进程**跑真正的入口模块，
在进程内 monkeypatch 验不出来。

为什么用探针换掉 `load_dotenv` 而不是造一个真 `.env`
----------------------------------------------------
`load_dotenv()` 的 `find_dotenv()` 是从**调用方文件**（`app/serve.py`）向上找，
与 cwd 无关 ⇒ 开发机根目录那份真 `.env` 一定会被找到，用例结果就取决于"这台
机器上有没有 `.env` 里的 Key"。那会变成一条在 CI 上红、在本机绿的空断言。
换成探针后：既精确记录"被调用时 `app.config` 进来了没有"，又由探针自己往环境里
写一个哨兵值 —— 断言的是**顺序**，不是这台机器的配置。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_SUBPROCESS_CODE = r"""
import os
import runpy
import sys

sys.argv = ["app.serve", "--port", "1"]

# --- 探针：替换 load_dotenv ------------------------------------------------
# 记录"被调用的那一刻 app.config 是否已进 sys.modules"，同时写一个哨兵 Key。
# 真正的 .env 内容一概不读（这正是本用例与环境无关的原因）。
import dotenv

_seen = {}


def _probe(*args, **kwargs):
    _seen["before_config"] = "app.config" not in sys.modules
    os.environ["AGNES_API_KEY"] = "sk-sentinel-from-dotenv-probe"
    return True


dotenv.load_dotenv = _probe

# --- 拦住 uvicorn：本用例只验"启动之前的配置解析"，不需要真的监听端口 ------
import uvicorn

_started = {}
uvicorn.run = lambda *a, **k: _started.setdefault("called", True)

try:
    runpy.run_module("app.serve", run_name="__main__")
except SystemExit:
    pass

import app.config as config

print("BEFORE_CONFIG=", _seen.get("before_config"))
print("API_KEY=", config.API_KEY or "")
print("UVICORN_CALLED=", bool(_started))
"""


def _run(tmp_path: Path) -> subprocess.CompletedProcess:
    env = {**os.environ}
    # 本文件验的是"`.env` 里的值能不能到达 config"，所以进程里**不能**预先有它，
    # 否则哨兵值不生效，用例就变成"环境里本来就有 Key"的假绿。
    for key in ("AGNES_API_KEY", "AGENTS_ENABLED"):
        env.pop(key, None)
    env["APP_ENV"] = "development"
    env["DB_URL"] = ""
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_CODE],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=180,
    )


def test_dotenv_is_loaded_before_config_is_read(tmp_path):
    """`.env` 必须在 `app.config` 求值之前灌进环境。

    失败时的样子：`API_KEY=` 为空 —— 也就是部署时那条 `missing_api_key` 拒绝启动。
    """
    got = _run(tmp_path)
    assert got.returncode == 0, f"入口模块跑不起来：\n{got.stdout}\n{got.stderr}"
    assert "BEFORE_CONFIG= True" in got.stdout, (
        "`load_dotenv` 被调用时 `app.config` 已经导入过了 —— 它的模块级常量"
        f"（含 API_KEY）已经按空环境固化，`.env` 再也回填不进去。\n{got.stdout}"
    )
    assert "API_KEY= sk-sentinel-from-dotenv-probe" in got.stdout, (
        "`.env` 加载没有发生在 config 求值之前，Key 没有到达 `app.config`。\n"
        f"{got.stdout}"
    )


def test_entry_reaches_uvicorn(tmp_path):
    """反向对照：顺序对了还不够，`main()` 必须真的走到 `uvicorn.run`。

    少了这条，一个"加载了 .env 但 `main()` 中途 return"的实现也会让上面那条绿。
    """
    got = _run(tmp_path)
    assert "UVICORN_CALLED= True" in got.stdout, f"{got.stdout}\n{got.stderr}"
