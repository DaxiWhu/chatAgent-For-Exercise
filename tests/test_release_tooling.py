"""发布 / 回滚工具链的行为测试。

覆盖对象
--------
  - `scripts/nginx_weight.py`   —— 读写灰度权重（发布与回滚**唯一真正要改的东西**）
  - `scripts/backup_manifest.py` —— 判断"同一时刻的 pg 与 qdrant 备份"是否成对
  - `deploy/release.sh` / `deploy/rollback.sh` —— 把上面两步固化成一条命令

为什么值得测
------------
这一组脚本的失败模式**全是"看起来成功了"**：

  - `nginx_weight.py` 改不到那一行时若"静默不动"（sed 的传统行为）⇒ 你以为流量
    切了 100%，实际还是 9:1 —— "灰度验证没意义"这件事本身看不出来；
  - 权重写成 0/0 ⇒ nginx 把所有请求判为 `no live upstreams`，把"改配置"变成
    一次**全站 502**；
  - `backup_manifest.py` 把"没有备份可判断"当成通过 ⇒ 你在**没有可用备份**的
    状态下以为可以恢复；
  - `release.sh` 把判据退出码 2（看不到数据）当成不通过 ⇒ 自动回滚一个其实没问题
    的版本，并且把"看不到数据"这个真问题掩盖掉。

这些都不会让脚本崩，只会让它在**最需要它的时刻给出错误结论**。所以这里测的是
判定逻辑与退出码契约，而不是"脚本能跑"。
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
NGINX_CONF = DEPLOY / "app" / "nginx.conf"
RELEASE = DEPLOY / "release.sh"
ROLLBACK = DEPLOY / "rollback.sh"
CANARY_DOC = DEPLOY / "CANARY.md"
WEIGHT_SCRIPT = ROOT / "scripts" / "nginx_weight.py"
MANIFEST_SCRIPT = ROOT / "scripts" / "backup_manifest.py"

BASH = shutil.which("bash")


def _load(path: Path, name: str):
    """按文件路径加载脚本模块（它们不在包内，不能用普通 import）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rc(fn, *args) -> int:
    """把 `main()` 的返回值与 SystemExit 都归一成退出码。

    有的分支是 `return RC`，有的是 `raise SystemExit(RC)`（见 nginx_weight.py 的
    `read_weights`）。只看其中一种会漏掉一半契约。
    """
    try:
        return int(fn(*args))
    except SystemExit as e:  # noqa: PERF203 —— 这里就是要捕获它
        return int(e.code) if e.code is not None else 0


@pytest.fixture()
def weight_mod():
    return _load(WEIGHT_SCRIPT, "nginx_weight_under_test")


@pytest.fixture()
def conf_copy(tmp_path: Path) -> Path:
    """真实 nginx.conf 的副本 —— 测试绝不改仓库里的那一份。

    ⚠️ 必须用 `copyfile` 做**逐字节**复制，不能用 `read_text` + `write_text`：
    后者在 Windows 上会把行尾 `\\n` 翻译成 `\\r\\n`，而 `nginx_weight.py` 的行内
    正则以 `$` 收尾 —— 配上 `\\r\\n` 会**匹配不到任何行**，于是这些用例会以
    "找不到 app-stable 的 weight 声明"这种与真实代码无关的方式失败（踩过）。
    """
    dst = tmp_path / "nginx.conf"
    shutil.copyfile(NGINX_CONF, dst)
    return dst


# ===========================================================================
# scripts/nginx_weight.py
# ===========================================================================
def test_weight_script_refuses_when_it_cannot_find_the_target(weight_mod, tmp_path):
    """命中不到目标行时必须**报错退出（2）**，不能"静默不动"。

    这是 sed 传统写法最危险的失败模式：匹配失败时 sed 打印原文、退出 0。
    发布流程会因此以为权重已经切过去了 —— 而实际没有任何变化，灰度与回滚
    全部失去意义，且没有任何报错。
    """
    empty = tmp_path / "nginx.conf"
    empty.write_text("# 这里没有 upstream 段\n", encoding="utf-8")

    assert _rc(weight_mod.main, ["--conf", str(empty), "show"]) == 2
    assert _rc(weight_mod.main, ["--conf", str(empty), "set", "--canary", "50"]) == 2


def test_weight_script_refuses_both_zero(weight_mod, conf_copy):
    """拒绝 stable 与 canary 同时为 0。

    nginx 在总权重为 0 时会把所有请求判为 `no live upstreams` 并返回 502 ——
    那就把一个"改配置"动作变成了一次**全站故障**。而 `--stable 0 --canary 0`
    看起来很对称，是回滚时最容易手滑的一步。
    """
    before = conf_copy.read_text(encoding="utf-8")
    rc = _rc(weight_mod.main,
             ["--conf", str(conf_copy), "set", "--stable", "0", "--canary", "0"])
    assert rc == 1
    assert conf_copy.read_text(encoding="utf-8") == before, "被拒绝的参数仍然改动了文件"


def test_weight_script_rejects_negative(weight_mod, conf_copy):
    before = conf_copy.read_text(encoding="utf-8")
    assert _rc(weight_mod.main, ["--conf", str(conf_copy), "set", "--canary", "-1"]) == 1
    assert conf_copy.read_text(encoding="utf-8") == before


def test_weight_script_roundtrip_reads_back_what_it_wrote(weight_mod, conf_copy):
    """正常写入后，磁盘上的值必须与请求一致（脚本自带读回校验）。"""
    assert _rc(weight_mod.main,
               ["--conf", str(conf_copy), "set", "--stable", "95", "--canary", "5"]) == 0
    assert weight_mod.read_weights(str(conf_copy)) == {"stable": 95, "canary": 5}

    # 再切一次（模拟回滚：只把 canary 归零）—— 幂等
    assert _rc(weight_mod.main, ["--conf", str(conf_copy), "set", "--canary", "0"]) == 0
    assert weight_mod.read_weights(str(conf_copy)) == {"stable": 95, "canary": 0}


def test_weight_script_dry_run_does_not_write(weight_mod, conf_copy):
    """`--dry-run` 绝不能改动文件 —— 它是"先看清楚要做什么"的保险。"""
    before = conf_copy.read_text(encoding="utf-8")
    assert _rc(weight_mod.main, ["--conf", str(conf_copy), "set",
                                 "--stable", "99", "--canary", "1", "--dry-run"]) == 0
    assert conf_copy.read_text(encoding="utf-8") == before, "dry-run 改动了文件"


def test_weight_script_fails_loudly_when_the_readback_disagrees(weight_mod, conf_copy,
                                                                monkeypatch):
    """写入后读回不一致 ⇒ 退出码 3（**不能**当成成功）。

    这条断言的是"那个校验真的会拦人"。做法是把 `read_weights` 换成返回一个
    **不一致**的值（模拟"写没落盘、磁盘上还是旧值"）—— 这是不依赖坏盘、
    不依赖 Windows 权限语义就能触发该校验的唯一办法。
    少了它，"写完校验"就只是一段没人验证过的代码。
    """
    monkeypatch.setattr(weight_mod, "read_weights",
                        lambda conf: {"stable": 9, "canary": 1})
    rc = _rc(weight_mod.main,
             ["--conf", str(conf_copy), "set", "--stable", "50", "--canary", "50"])
    assert rc == 3, (
        "读回值与请求值不一致时脚本没有报错（期望退出码 3）——"
        "那它会在『其实没写进去』的情况下打印『权重已更新』"
    )


# ===========================================================================
# scripts/backup_manifest.py
# ===========================================================================
@pytest.fixture()
def manifest_mod():
    return _load(MANIFEST_SCRIPT, "backup_manifest_under_test")


def _mk(directory: Path, name: str) -> Path:
    p = directory / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def test_manifest_is_undecidable_when_a_side_is_empty(manifest_mod, tmp_path):
    """两侧至少一侧为空 ⇒ 退出码 2（**无法判定**），而不是 0（通过）。

    把"没有备份可判断"当成通过，是本项目踩过的同一类坑（见 CANARY.md 的
    "不要当成通过"）：你会以为自己有一份可用的配对，而实际上什么都没有。
    """
    _mk(tmp_path, "langgraph_checkpoints-20260930-120000.dump")
    assert _rc(manifest_mod.main, ["--backup-dir", str(tmp_path)]) == 2


def test_manifest_is_undecidable_when_the_directory_is_missing(manifest_mod, tmp_path):
    """目录不存在与"两侧都空"是同一档：无法判定，不是通过。"""
    assert _rc(manifest_mod.main,
               ["--backup-dir", str(tmp_path / "does-not-exist")]) == 2


def test_manifest_reports_ok_for_a_matched_pair(manifest_mod, tmp_path):
    _mk(tmp_path, "langgraph_checkpoints-20260930-120000.dump")
    _mk(tmp_path, "qdrant/rag_chunks-20260930-120030.snapshot")
    assert _rc(manifest_mod.main,
               ["--backup-dir", str(tmp_path), "--window", "300"]) == 0


def test_manifest_reports_mismatch_for_a_stale_pair(manifest_mod, tmp_path):
    """最新一对相差 6 小时 ⇒ 退出码 1（恢复前必须人工确认）。"""
    _mk(tmp_path, "langgraph_checkpoints-20260930-120000.dump")
    _mk(tmp_path, "qdrant/rag_chunks-20260930-180000.snapshot")
    assert _rc(manifest_mod.main,
               ["--backup-dir", str(tmp_path), "--window", "300"]) == 1


def test_manifest_only_the_newest_pair_decides(manifest_mod, tmp_path):
    """只有**最新**那一对决定退出码。

    历史上某一次曾经错配不影响今天的恢复（真正会用到的只有最新一对）。
    把历史错配也算失败会让人对脚本脱敏，进而忽略真正的那次错配。
    """
    _mk(tmp_path, "langgraph_checkpoints-20260930-000000.dump")
    _mk(tmp_path, "qdrant/rag_chunks-20260930-090000.snapshot")   # 旧的、错配
    _mk(tmp_path, "langgraph_checkpoints-20260930-120000.dump")
    _mk(tmp_path, "qdrant/rag_chunks-20260930-120005.snapshot")   # 最新的、匹配
    assert _rc(manifest_mod.main,
               ["--backup-dir", str(tmp_path), "--window", "300"]) == 0


def test_manifest_missing_snapshot_is_a_mismatch_not_a_pass(manifest_mod, tmp_path):
    """最新的 pg 备份找不到配对快照 ⇒ 1（不匹配），不能当成"通过"。

    注意：这里刻意保留一份**更旧**的 qdrant 快照。若两侧里有一侧完全为空，
    脚本给的是 2（无法判定）—— 那是另一条分支。这条要测的是"两侧都有备份，
    但最新的 pg 配不上任何一个快照"。
    """
    _mk(tmp_path, "qdrant/rag_chunks-20260930-110000.snapshot")
    _mk(tmp_path, "langgraph_checkpoints-20260930-120000.dump")
    _mk(tmp_path, "langgraph_checkpoints-20260930-130000.dump")   # 更新的、无配对
    assert _rc(manifest_mod.main,
               ["--backup-dir", str(tmp_path), "--window", "300"]) == 1


# ===========================================================================
# deploy/release.sh / deploy/rollback.sh
# ===========================================================================
@pytest.mark.parametrize("name", ["release.sh", "rollback.sh"])
def test_deploy_scripts_exist(name):
    assert (DEPLOY / name).exists(), f"缺少 {DEPLOY / name}"


@pytest.mark.skipif(not BASH, reason="本机没有 bash（CI 上有）")
@pytest.mark.parametrize("name", ["release.sh", "rollback.sh"])
def test_shell_scripts_are_syntactically_valid(name):
    """`bash -n` 语法检查。

    这两脚本里最容易出的一类错是**引号 / heredoc**：不带引号的 heredoc 或双引号
    字符串里的反引号会被 shell 当成命令替换去执行 —— 它不报错，只会让输出变成
    一堆 "command not found"，或者更糟：静默执行了不该执行的命令。
    语法检查拦不住语义错误，但能保证"脚本至少在语法上可执行"。
    """
    r = subprocess.run([BASH, "-n", str(DEPLOY / name)],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"{name} 语法检查失败：\n{r.stderr}"


def test_release_documents_the_four_exit_codes():
    """`release.sh` 必须写明退出码 0/1/2/3 分别该做什么。

    最关键的是 2 与 3 的区分：2 = 判据不达标 → **已自动回滚**；
    3 = 判据无法判定 → **停在原地**、转人工。把 3 当成失败去回滚，等于回滚一个
    其实没问题的版本，还把"看不到数据"这个真问题掩盖掉。
    """
    text = RELEASE.read_text(encoding="utf-8")
    codes = set(re.findall(r"(?m)^#\s+(\d)\s+\S", text))
    assert {"0", "1", "2", "3"} <= codes, (
        f"release.sh 的头部注释没有写全退出码（已写：{sorted(codes)}）——"
        "值班的人遇到非 0 时要能立刻知道该做什么"
    )
    assert "回滚" in text and "人工" in text


def test_release_uses_env_file_weight_script_and_rollback():
    """发布脚本必须：带 `--env-file`、用 nginx_weight.py 改权重、不达标时调回滚。"""
    text = RELEASE.read_text(encoding="utf-8")
    assert "--env-file .env" in text, (
        "release.sh 的 compose 调用没有带 --env-file —— 它读不到仓库根的 .env，"
        "CANARY_IMAGE 等插值会静默退回默认值（发布跑的还是旧镜像）"
    )
    assert "nginx_weight.py" in text, (
        "release.sh 没有用 nginx_weight.py 改权重 —— 会回到『改了但没生效』的老问题"
    )
    assert "canary_check.py" in text, "release.sh 没有调用判据脚本"
    assert "rollback.sh" in text, "release.sh 在判据不达标时没有调用回滚脚本"


def test_release_distinguishes_check_exit_code_1_from_2():
    """判据的退出码 1 与 2 必须走**不同**分支。

    这是整套流程里最贵的假绿：把 2（看不到数据 / Prometheus 不可达）当成
    "不通过"，就会自动回滚一个其实没问题的版本 —— 而且掩盖了"监控数据缺失"
    这个真问题。所以必须能在脚本里看到两个不同的分支。
    """
    text = RELEASE.read_text(encoding="utf-8")
    assert re.search(r'rc["\']?\s*-eq\s+1', text), "没有针对判据退出码 1（不达标）的分支"
    assert re.search(r'rc["\']?\s*-eq\s+2', text), "没有针对判据退出码 2（无法判定）的分支"
    assert "--dry-run" in text and "--resume" in text, (
        "release.sh 缺少 --dry-run / --resume（前者用于先看清楚要做什么，后者用于中断后续跑）"
    )


def test_rollback_zeros_canary_and_verifies_syntax_before_reload():
    """回滚必须：把 canary 归零 → **先 `nginx -t` 再 `nginx -s reload`**。

    顺序不能反：reload 失败时旧配置仍然生效，而"旧配置生效"的表现与
    "忘了 reload"完全一样 —— 只有 `-t` 能提前拦住语法错。
    """
    text = ROLLBACK.read_text(encoding="utf-8")
    assert "nginx_weight.py" in text and "set --canary 0" in text, (
        "回滚没有通过 nginx_weight.py 把 canary 权重归零"
    )
    m_t = re.search(r"exec -T nginx nginx -t", text)
    m_r = re.search(r"exec -T nginx nginx -s reload", text)
    assert m_t, "回滚里没有 nginx -t（语法校验）"
    assert m_r, "回滚里没有 nginx -s reload（改了权重不 reload 等于没改）"
    assert m_t.start() < m_r.start(), (
        "回滚里 reload 出现在 -t 之前 —— 语法错时 reload 失败但旧配置继续生效，"
        "看起来『没报错』，实则权重没变"
    )


def test_rollback_documents_exit_codes_and_the_keepalive_caveat():
    """回滚脚本必须写明退出码，并解释"命中 canary"的一个无害原因。

    自带端到端验证是它的核心价值（"改了配置"与"流量真的回去了"是两件事）。
    但验证失败时若不解释**连接复用（keepalive）**这个常见无害原因，运维会以为
    回滚失败而去重启实例 —— 那正好抹掉了唯一能定位根因的现场。
    """
    text = ROLLBACK.read_text(encoding="utf-8")
    codes = set(re.findall(r"(?m)^#\s+(\d)\s+\S", text))
    assert {"0", "1", "2"} <= codes, f"rollback.sh 头部没有写全退出码（已写：{sorted(codes)}）"
    assert "keepalive" in text or "连接复用" in text, (
        "回滚没有解释 keepalive / 连接复用 —— 运维会把一次正常现象误判为回滚失败"
    )
    assert "exec -T nginx" in text or "curl" in text, "回滚没有做端到端验证（改完只打印'已回滚'不够）"


def test_canary_doc_points_at_the_scripts():
    """`CANARY.md` 必须引用这两个脚本 —— 否则流程文档与实际操作脱节。"""
    text = CANARY_DOC.read_text(encoding="utf-8")
    for name in ("deploy/release.sh", "deploy/rollback.sh"):
        assert name in text, f"CANARY.md 没有引用 {name}"
