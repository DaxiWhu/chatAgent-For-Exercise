"""启动期硬校验：agent 链路不可用 ⇒ **进程起不来**。

为什么必须用**子进程**测
-----------------------
这道闸门跑在 `app.main` 的**导入期**（`_assert_agents_ready()`）。而本测试进程在
收集阶段就已经把 `app.main` 导入过了（几十个文件 `import app.main as M`），
`sys.modules` 里一堆模块已经绑定了当时那份 `config` 常量的值。在同一个进程里
reload 出来的是一个**混合体**，它的行为与"部署时真的拉起一个进程"不同 ——
而要验的恰恰是后者。

为什么要屏蔽 `dotenv`（这是本文件唯一一处反直觉的地方）
-----------------------------------------------------
`app.main` 在导入期调 `load_dotenv()`，而 python-dotenv 的 `find_dotenv()` 是
**从调用方那个文件**（`app/main.py`）开始往上找 —— 也就是说它与 cwd 无关，本仓库
根目录的 `.env` 一定会被找到（开发机用它跑服务，里面通常写着 `LLM_API_KEY`）。
不屏蔽的话，无论怎么清环境变量，`.env` 都会把 Key 灌回来，用例变成一条**永远绿的
空断言**（"没配 Key 也起得来"恒成立）。

所以子进程里先把 `dotenv` 换成一个空壳（`load_dotenv` 什么都不做）—— 这正好等价于
"这台机器上根本没有 `.env`"，也就是 CI 的真实情况。`tests/conftest.py` 在进程内
做的是同一件事（它包了一层 `load_dotenv` 再擦环境变量）。

⚠️ 子进程必须带 `APP_ENV=development`：`IS_PRODUCTION` 是**黑名单认开发**
（不写 = 生产），而生产口径还会触发别的启动自检（鉴权策略等），那些与本文件
要验的事情无关，混进来会让失败原因变得不可读。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 子进程的基线环境：干净、可预期，不继承本进程的 pytest 身份注入。
_BASE_ENV = {
    "APP_ENV": "development",
    # 不要连真库：这些用例只验"导入期的那道闸门"，连接类自检越少越干净。
    "DB_URL": "",
    # 模型地址给个占位即可（构造 client 不需要网络）。
    "LLM_API_BASE": "http://127.0.0.1:9/v1",
    "LLM_MODEL": "test-model",
}

#: 屏蔽 `.env` 的前置代码（理由见模块 docstring）。
#:
#: ⚠️ 必须**导入真模块再盖掉一个函数**，不能塞一个 `sys.modules['dotenv']` 空壳：
#: `pydantic_settings` 在导入链上会 `from dotenv import dotenv_values`，
#: 空壳会把它一起打断，报的是"cannot import name 'dotenv_values'"——
#: 那个错误与"没配 Key"毫无关系，会把排查方向带跑。
_NO_DOTENV = "import dotenv; dotenv.load_dotenv = lambda *a, **k: False;"


def _run(code: str, cwd: Path, env_overrides: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, **_BASE_ENV}
    # 先清掉所有可能"救活"这条链路的键，再按用例需要加回去。
    for key in ("LLM_API_KEY", "AGENTS_ENABLED"):
        env.pop(key, None)
    env.update(env_overrides)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-c", _NO_DOTENV + code],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=180,
    )


def test_refuses_to_start_without_api_key(tmp_path):
    """没配 Key ⇒ 起不来，且报错**点名是哪个 Key**。

    为什么这条不能再是"只记一条 warning"：图退役之后本服务只剩 agent 这一条作答
    路径，没有第二套实现。进程起来了但每个请求都必然失败，是一个比"起不来"更
    难查的状态 —— 起不来至少部署流水线当场就知道。
    """
    got = _run("import app.main", tmp_path, {})

    assert got.returncode != 0, "没配 Key 却启动成功了（兜底路径还在？）"
    blob = got.stdout + got.stderr
    assert "拒绝启动" in blob
    assert "LLM_API_KEY" in blob
    assert "missing_api_key" in blob


def test_starts_when_api_key_is_present(tmp_path):
    """配了 Key ⇒ 正常导入。

    这条是上面那条的**反向对照**：少了它，一个"无论配不配 Key 都退出"的实现
    会让上面那条照样绿。
    """
    got = _run("import app.main", tmp_path, {"LLM_API_KEY": "sk-test-not-a-real-key"})

    assert got.returncode == 0, f"配了 Key 仍然起不来：\n{got.stdout}\n{got.stderr}"


def test_agents_enabled_knob_is_gone(tmp_path):
    """`AGENTS_ENABLED` 这个开关必须**不存在**。

    它的语义是"关掉 agent 链路、回落另一条链路"，而回落目标已经随图一起删除。
    留一个只能取 true 的开关，会让人以为可以关；更糟的是关掉之后进程照常启动、
    却答不出任何东西。
    """
    got = _run("import app.config as c; print('HAS=', hasattr(c, 'AGENTS_ENABLED'))",
               tmp_path, {})
    assert got.returncode == 0, got.stderr
    assert "HAS= False" in got.stdout, "`AGENTS_ENABLED` 仍然存在"
