"""版本号单一来源的守卫。

背景（真实踩过的坑）
--------------------
版本号曾同时写在三处：`app/config.py` 的默认值、`.env`、compose 的镜像 tag。
改一处漏两处时，**"服务 `/health` 自报的版本"与"实际跑的镜像 tag"会不一致，
而且没有任何信号** —— 接口照常回 200，只是回了一个错的版本号。于是排查
"线上到底是哪一版"时会得出错误结论，这比明确报"不知道"更糟。

现在唯一真源是仓库根的 `VERSION`：

    VERSION                              ← 唯一真源（只改这里）
      ├── app/config.py                  直接**读**它（环境变量优先，不存副本）
      ├── deploy/app/docker-compose.yml  镜像 tag 的默认值（副本）
      └── .env.example                   `APP_VERSION=` 示例值（副本）

副本由 `python scripts/sync_version.py` 同步。本文件断言"副本 == 真源"，
以及"没有人重新把版本号写死回去"。
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts import sync_version

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
COMPOSE = ROOT / "deploy" / "app" / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"
CONFIG_PY = ROOT / "app" / "config.py"

# `0.0.0+unknown` 是 config.py 里那个"明确表示不知道"的兜底值，**不算**硬编码版本
_FALLBACK_PREFIX = "0.0.0"


def _version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def test_version_file_exists_and_is_a_real_version():
    """唯一真源必须存在，且是"可追溯"的版本号，而不是 latest 这种流动标签。"""
    assert VERSION_FILE.is_file(), (
        "缺少 VERSION 文件 —— 版本号又退回'三处各写各的'了"
    )
    v = _version()
    assert sync_version.SEMVER_RE.match(v), (
        f"VERSION 的内容 {v!r} 不是合法版本号（应形如 1.2.3 或 1.2.3-rc1）。"
        f"latest / dev 这类流动标签会让'线上跑的是哪一版'重新变得不可回答。"
    )


def test_compose_image_tags_are_not_hardcoded():
    """compose 的镜像 tag 不许再写死版本号 —— 写死了改 VERSION 就不会影响它。"""
    text = COMPOSE.read_text(encoding="utf-8")
    offenders = re.findall(
        r"^\s*image:\s*(chatagent(?:-alert-sink)?):\d[\w.\-]*", text, re.M
    )
    assert not offenders, (
        f"compose 里有硬编码版本号的镜像 tag：{sorted(set(offenders))}。"
        f"应写成 ${{APP_VERSION:-<版本>}}，让 VERSION 成为唯一真源。"
    )


def test_compose_defaults_stay_in_sync_with_version_file():
    """副本必须等于真源 —— 这是"改一处漏一处"唯一的兜底信号。"""
    ver = _version()
    values = sync_version.copy_values(
        sync_version.COMPOSE_RE, COMPOSE.read_text(encoding="utf-8")
    )
    assert values, (
        "compose 里一个 ${APP_VERSION:-...} 都没有 —— 要么镜像 tag 被写死了，"
        "要么本守卫的正则已与实现脱节（两种都会让守卫变成假绿）"
    )
    stale = sorted({v for v in values if v != ver})
    assert not stale, (
        f"compose 的镜像 tag 默认值 {stale} 与 VERSION({ver}) 不一致；"
        f"跑 `python scripts/sync_version.py` 同步"
    )


def test_env_example_version_stays_in_sync():
    ver = _version()
    values = sync_version.copy_values(
        sync_version.DOTENV_RE, ENV_EXAMPLE.read_text(encoding="utf-8")
    )
    assert values, ".env.example 里找不到 `APP_VERSION=` 这一项（配置清单不完整？）"
    stale = sorted({v for v in values if v != ver})
    assert not stale, (
        f".env.example 的 APP_VERSION={stale} 与 VERSION({ver}) 不一致；"
        f"跑 `python scripts/sync_version.py` 同步"
    )


def test_sync_script_reports_clean():
    """同步脚本自己跑一遍 `--check`：CI 走的就是这条路径，不能只有函数级断言。"""
    assert sync_version.main(["--check"]) == sync_version.RC_OK


def test_config_reads_version_from_the_file(monkeypatch):
    """`app/config.py` 的版本号来自 VERSION 文件，而不是模块里写死的字面量。"""
    from app import config

    monkeypatch.delenv("APP_VERSION", raising=False)
    assert config._read_version() == _version()


def test_config_env_var_wins(monkeypatch):
    """容器 / CI 可以按发布标签注入 —— 环境变量优先级必须高于文件。"""
    from app import config

    monkeypatch.setenv("APP_VERSION", "9.9.9-rc1")
    assert config._read_version() == "9.9.9-rc1"


def test_config_falls_back_to_unknown_rather_than_guessing(monkeypatch):
    """读不到 VERSION 时必须回 `0.0.0+unknown`，**不能猜**一个"看起来对"的版本号。"""
    from app import config

    def _boom(*_a, **_k):
        raise OSError("模拟 VERSION 文件不可读")

    monkeypatch.delenv("APP_VERSION", raising=False)
    monkeypatch.setattr(config.Path, "read_text", _boom)
    assert config._read_version() == "0.0.0+unknown", (
        "读不到真源时返回了某个具体版本号 —— 那会让 /health 说谎，"
        "比明确表示'不知道'更危险"
    )


def test_config_py_carries_no_hardcoded_version():
    """模块里不许再出现版本号字面量（`0.0.0+unknown` 兜底除外）。"""
    found = [
        v
        for v in re.findall(
            r"""["'](\d+\.\d+\.\d+(?:[-+][^"']*)?)["']""",
            CONFIG_PY.read_text(encoding="utf-8"),
        )
        if not v.startswith(_FALLBACK_PREFIX)
    ]
    assert not found, (
        f"app/config.py 里出现了硬编码版本号 {found} —— 版本号又有了第二个真源"
    )


def test_compose_copy_count_matches_declaration():
    """**副本数量**必须等于 TARGETS 里声明的预期值。

    为什么要单独一条（而不是只靠 `test_sync_script_reports_clean`）
    ----------------------------------------------------------------
    此前 `sync_file` 的 `expected` 参数只用来 print 一句 WARN，`--check`
    在数量不符时**照样返回 RC_OK**，于是那条"CI 走的就是这条路径"的测试
    假绿了整整一段时间（实际 5 处、预期 4 处）。

    这类失效的形态很隐蔽：`COMPOSE_RE` 若与 compose 的写法脱节（比如某个服务
    改用了 `image: chatagent:${APP_VERSION}` 的新写法），那一行不再被匹配 ⇒
    它**永不同步**；而剩下几处的值全都正确 ⇒ 值比对看不出任何问题。
    最终某个服务的镜像 tag 永久停在旧版本号，且零报错。

    本用例直接比对数量，`expected` 一旦与文件现实脱节就立刻红。
    """
    text = COMPOSE.read_text(encoding="utf-8")
    actual = len(sync_version.COMPOSE_RE.findall(text))
    declared = next(
        exp
        for path, _pat, _render, exp in sync_version.TARGETS
        if path == COMPOSE
    )
    assert actual == declared, (
        f"compose 里带 ${{APP_VERSION:-...}} 的 image 行实际 {actual} 处，"
        f"但 TARGETS 声明 {declared} 处。\n"
        f"如果新增/删除了服务，这是**有意的结构变化** —— 请同步更新 "
        f"scripts/sync_version.py 的 TARGETS 预期数量。\n"
        f"如果不是有意的，说明 COMPOSE_RE 已与文件写法脱节，"
        f"那么少掉的那处副本将永远不被同步。"
    )


def test_check_mode_fails_when_copy_count_drifts(monkeypatch):
    """`--check` 必须在**数量不符**时返回非零，而不是只打印一句 WARN。

    这是上一条的"行为侧"：断言 `main()` 的返回值真的变了。只测数量相等
    （上一条）测的是"当前状态对不对"，测不出"将来数量变了会不会被拦住"。
    """
    real_targets = sync_version.TARGETS
    # 把 compose 的预期数量故意改错一处，模拟"加了服务但没同步 TARGETS"。
    monkeypatch.setattr(
        sync_version,
        "TARGETS",
        [
            (path, pat, render, (exp + 1 if path == COMPOSE else exp))
            for path, pat, render, exp in real_targets
        ],
    )
    rc = sync_version.main(["--check"])
    assert rc == sync_version.RC_DRIFT, (
        "副本数量与预期不符时 `--check` 返回了 RC_OK —— "
        "这意味着 `make check` 与 CI 都会放过它，"
        "而某一处副本可能正永久停留在旧版本号上且零报错"
    )


def test_write_mode_does_not_fail_on_count_drift(monkeypatch):
    """写模式（不带 `--check`）的职责只是把值改对，数量问题不判失败。

    反过来钉一下，防止把上面那条的判定顺手加到写模式上 —— 那会让
    `python scripts/sync_version.py` 在版本号已经同步好的情况下"报失败"，
    发布脚本会因此中断，而实际上什么都没有错。
    """
    real_targets = sync_version.TARGETS
    monkeypatch.setattr(
        sync_version,
        "TARGETS",
        [
            (path, pat, render, (exp + 1 if path == COMPOSE else exp))
            for path, pat, render, exp in real_targets
        ],
    )
    assert sync_version.main([]) == sync_version.RC_OK
