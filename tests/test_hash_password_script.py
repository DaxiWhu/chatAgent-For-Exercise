"""`scripts/hash_password.py` 的契约测试。

为什么这些断言值得写
--------------------
这个脚本是 `AUTH_USERS` 的**唯一入口**，而它出错的方式全是静默的：

  - 脚本拼出的条目格式 与 `app/config.py` 的解析器 不一致 ⇒ 服务读到结构坏掉的
    记录 ⇒ 一律"密码错误"，而**脚本自己回验是通过的**（它只验自己的哈希，
    不经过解析器）；
  - 输出的值没有加单引号 ⇒ 本地能登、容器里登不进（compose 插值吃掉 `$hex` 段），
    而两边都没有任何报错；
  - `--check` 把"哈希串已损坏"与"密码错了"混为一谈 ⇒ 会让人一直怀疑密码。

所以这里不测"函数返回了一个字符串"，而是钉住三条**对外契约**：
① 脚本产出的条目能被打分器（真实的 `config._parse_users`）解析并验签通过；
② 输出的这一行**必须是单引号包裹的**（compose 安全）；
③ `--check` 遇到损坏的哈希串要给出**可区分的**结论与修复指引。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import auth, config
from scripts import hash_password as hp

ROOT = Path(__file__).resolve().parent.parent


def _entry_from_stdout(out: str) -> str:
    """从输出里取出可粘贴的那一行，并**校验它带着单引号**。"""
    lines = [line for line in out.splitlines() if line.startswith("AUTH_USERS=")]
    assert lines, f"输出里没有 AUTH_USERS 行：\n{out}"
    assert len(lines) == 1, f"输出了多行 AUTH_USERS，粘贴时会互相覆盖：\n{out}"
    value = lines[0].split("=", 1)[1].strip()
    assert value.startswith("'") and value.endswith("'"), (
        "值没有用单引号包起来 —— docker compose 会对未加引号的值做变量插值，"
        f"`$<hex>` 段会被替换成空串、哈希静默报废。实际输出：{value!r}"
    )
    return value[1:-1]


def _args(**kw) -> argparse.Namespace:
    base = {"password": None, "stdin": False}
    base.update(kw)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# ① 脚本 → 解析器 → 验签：整条链路
# ---------------------------------------------------------------------------
def test_generated_entry_is_parsed_and_verified_by_the_real_parser(capsys):
    """生成的条目必须能被**服务端真正的解析器**吃下去并验签通过。

    刻意调用 `config._parse_users` 而不是自己写个 `split(":")`：
    后者会把"脚本与解析器已经不一致"这件事一并复现成"测试也一致"，就测不出来了 ——
    而那个不一致正是本条要防的。
    """
    pw = "S3cret-pw!"
    rc = hp.main(["-u", "alice", "-t", "acme", "-r", "reviewer,admin", "--password", pw])
    assert rc == 0
    entry = _entry_from_stdout(capsys.readouterr().out)

    parsed = config._parse_users(entry)
    assert set(parsed) == {"alice"}, f"条目没被解析出预期用户：{parsed}"
    rec = parsed["alice"]
    assert rec["tenant"] == "acme"
    assert rec["roles"] == ("reviewer", "admin")
    assert auth.verify_password(pw, rec["hash"]) is True
    assert auth.verify_password("not-the-password", rec["hash"]) is False

    # 明文口令绝不能出现在配置值里
    assert pw not in entry


def test_roles_are_optional(capsys):
    """不给 `-r` 时仍然是合法条目（角色是可选字段）。"""
    rc = hp.main(["-u", "bob", "-t", "acme", "--password", "pw-123456"])
    assert rc == 0
    entry = _entry_from_stdout(capsys.readouterr().out)
    rec = config._parse_users(entry)["bob"]
    assert rec["tenant"] == "acme" and rec["roles"] == ()


def test_two_invocations_produce_different_hashes(capsys):
    """同一个口令两次生成必须不同（salt 随机）—— 否则一眼就能看出谁和谁同口令。"""
    hashes = []
    for _ in range(2):
        assert hp.main(["-u", "alice", "-t", "acme", "--password", "same-password"]) == 0
        hashes.append(_entry_from_stdout(capsys.readouterr().out).split(":")[1])
    assert hashes[0] != hashes[1]


# ---------------------------------------------------------------------------
# ② 输入校验：格式炸弹要当场拦下，而不是写进配置等运行时炸
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "user,tenant",
    [
        ("bad:name", "acme"),      # `:` 会切坏字段
        ("bad,name", "acme"),      # `,` 会切出假用户
        ("alice", "ba:d"),         # 租户里也不能有
        ("alice", "ba,d"),
    ],
)
def test_rejects_separators_in_user_or_tenant(user, tenant, capsys):
    """分隔符必须当场拒绝：写进 `.env` 之后错误是**静默**的（解析器只告警并跳过）。"""
    with pytest.raises(SystemExit):
        hp.main(["-u", user, "-t", tenant, "--password", "pw-123456"])


def test_rejects_empty_password(capsys):
    """空口令 = 一条"任何人都能登录"的记录，必须拒绝。"""
    with pytest.raises(SystemExit):
        hp.main(["-u", "alice", "-t", "acme", "--password", ""])


def test_requires_user_and_tenant_together():
    with pytest.raises(SystemExit):
        hp.main(["-u", "alice", "--password", "pw-123456"])


def test_clamped_iterations_are_reported(capsys):
    """迭代数被收敛时必须出声 —— 否则"我指定了 50"与"实际 100000"的差异完全静默。"""
    rc = hp.main(["-i", "50", "-u", "alice", "-t", "acme", "--password", "pw-123456"])
    assert rc == 0
    cap = capsys.readouterr()
    assert "迭代数" in cap.err, f"没有报告迭代数被收敛：{cap.err!r}"
    assert "$100000$" in cap.out, f"没有收敛到下限：{cap.out!r}"


# ---------------------------------------------------------------------------
# ③ `--check`：把"配置坏了"与"密码错了"区分开
# ---------------------------------------------------------------------------
class _Cfg:
    """`check()` 只需要 `AUTH_USERS` 这一个属性。"""

    def __init__(self, users):
        self.AUTH_USERS = users


def test_check_diagnoses_compose_interpolation_damage(capsys):
    """salt/hash 为空 = 被 compose 插值吃掉了，必须**这样指出**而不是说"密码错"。

    这是本项目最贵的一类静默故障：哈希报废后服务只会一律拒登，
    而排查的人会一直怀疑密码。
    """
    cfg = _Cfg({"alice": {"hash": "pbkdf2_sha256$600000$$", "tenant": "acme", "roles": ()}})
    rc = hp.check("alice", _args(password="whatever"), auth, cfg)
    assert rc == 4, "损坏的哈希串应当给出**不同于**密码错误的返回码"
    err = capsys.readouterr().err
    assert "单引号" in err and "compose" in err, f"没有给出可执行的修复指引：{err!r}"


def test_check_passes_on_correct_password(capsys):
    pwhash = auth.hash_password("pw-123456")
    cfg = _Cfg({"alice": {"hash": pwhash, "tenant": "acme", "roles": ("reviewer",)}})
    assert hp.check("alice", _args(password="pw-123456"), auth, cfg) == 0
    assert "通过" in capsys.readouterr().out


def test_check_fails_on_wrong_password(capsys):
    """密码错与结构坏**必须是不同的返回码**（否则排障时无法自动分流）。"""
    pwhash = auth.hash_password("pw-123456")
    cfg = _Cfg({"alice": {"hash": pwhash, "tenant": "acme", "roles": ()}})
    assert hp.check("alice", _args(password="wrong"), auth, cfg) == 1
    assert "结构正常" in capsys.readouterr().err


def test_check_reports_unknown_user(capsys):
    assert hp.check("nobody", _args(password="x"), auth, _Cfg({})) == 3
    assert "未找到用户" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# ④ 模块导入不得有副作用（否则会破坏测试进程的裸环境前提）
# ---------------------------------------------------------------------------
def test_importing_the_script_does_not_load_dotenv():
    """导入本脚本**不得**把开发机 `.env` 灌进进程环境。

    这条守的是一个真实事故的入口：`tests/conftest.py` 刻意让测试进程等价于
    "裸环境"（它删掉了所有会影响服务行为的变量），而模块顶层一旦调
    `load_dotenv()`，**任何 import 它的测试**都会把 `.env` 的值灌回来 ——
    表现是"顺序相关、看起来毫不相干"的成片失败（曾因一行 `.env` 全量 35 条 401）。

    说明：CI 里没有 `.env`，所以这条在 CI 上恒真；它的价值在**开发机**上 ——
    而那正是这个隐患唯一会显现的地方。用子进程测是为了拿到"全新解释器"的干净进程。
    """
    code = (
        "import sys, os\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "before = set(os.environ)\n"
        "from scripts import hash_password  # noqa: F401\n"
        "print('|'.join(sorted(set(os.environ) - before)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
    )
    assert proc.returncode == 0, proc.stderr
    added = proc.stdout.strip()
    assert added == "", (
        f"导入 scripts.hash_password 时新增了环境变量：{added}\n"
        "说明 `load_dotenv()` 被放到了模块顶层 —— 它必须留在 `_load_app_modules()` 里，"
        "在真正执行时才加载。"
    )


def test_load_app_modules_happens_at_call_time(monkeypatch, tmp_path):
    """`.env` 的加载必须发生在**调用时**、且指向 `ROOT`（顺序与 import 隔离都靠它）。"""
    sentinel = "HASH_PASSWORD_SCRIPT_SENTINEL"
    (tmp_path / ".env").write_text(f"{sentinel}=yes\n", encoding="utf-8")
    monkeypatch.setattr(hp, "ROOT", tmp_path)
    # `load_dotenv` 会直接写 `os.environ`（不是通过 monkeypatch），所以自己收尾，
    # 别把这个哨兵变量漏给后面的用例。
    os.environ.pop(sentinel, None)
    try:
        hp._load_app_modules()
        assert os.environ.get(sentinel) == "yes"
    finally:
        os.environ.pop(sentinel, None)
