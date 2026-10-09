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
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
NGINX_CONF = DEPLOY / "app" / "nginx.conf"
NGINX_TLS_CONF = DEPLOY / "app" / "nginx.tls.conf"
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


def test_weight_script_writes_every_entry_conf(weight_mod, tmp_path, monkeypatch):
    """`set` 必须同时改**两份**入口配置（http 版与 tls 版）。

    2026-10-06 修：脚本原先只改 `nginx.conf`，而公网部署挂的是 `nginx.tls.conf`
    （`docker-compose.tls.yml` 用 `!override` 挂载）⇒ TLS 部署下 release.sh 的
    逐档放量一次都没生效，且 `show` 还会回显那份**没被容器读**的文件，
    给出"已切换"的假象。灰度是发布安全的最后一道闸门，静默失效最不该有。
    """
    monkeypatch.delenv("NGINX_CONF", raising=False)
    a = tmp_path / "nginx.conf"
    b = tmp_path / "nginx.tls.conf"
    shutil.copyfile(NGINX_CONF, a)
    shutil.copyfile(NGINX_TLS_CONF, b)
    monkeypatch.setattr(weight_mod, "CONF_CANDIDATES", (str(a), str(b)))

    assert _rc(weight_mod.main, ["set", "--stable", "95", "--canary", "5"]) == 0
    assert weight_mod.read_weights(str(a)) == {"stable": 95, "canary": 5}, "第一份没改到"
    assert weight_mod.read_weights(str(b)) == {"stable": 95, "canary": 5}, "第二份没改到"


def test_show_flags_inconsistent_entry_confs(weight_mod, tmp_path, monkeypatch):
    """两份配置权重不一致时，`show` 必须报错（3）而不是随便报一个读数。

    不一致时"当前权重是多少"这个问题**没有答案** —— 实际值取决于部署时挂了哪一份。
    给一个看似确定的读数，值班的人会据此判断"已经是 9:1 了"，而真实流量可能是 1:1。
    """
    monkeypatch.delenv("NGINX_CONF", raising=False)
    a = tmp_path / "nginx.conf"
    b = tmp_path / "nginx.tls.conf"
    shutil.copyfile(NGINX_CONF, a)
    shutil.copyfile(NGINX_TLS_CONF, b)
    monkeypatch.setattr(weight_mod, "CONF_CANDIDATES", (str(a), str(b)))

    weight_mod.write_weights(str(b), {"stable": 50, "canary": 50})
    assert _rc(weight_mod.main, ["show"]) == 3, "两份不一致却没有报错"


def test_display_falls_back_to_absolute_on_another_drive(weight_mod):
    """`_display` 的契约：同盘 ⇒ 相对路径；跨盘 ⇒ 原样返回，**不抛异常**。

    这条是上面那条的"单元版"，跑在哪个盘上都成立（把 `ROOT` 换成一个不存在的盘符
    即可，不需要真的去挂一个盘）。
    """
    if os.name != "nt":
        pytest.skip("跨盘符 relpath 只存在于 Windows 语义下")
    other = "Z:" + os.sep + "somewhere" + os.sep + "nginx.conf"
    assert weight_mod._display(other) == other, (
        "跨盘符路径没有退回绝对形式 —— os.path.relpath 会在 Windows 上抛 ValueError"
    )
    inside = os.path.join(weight_mod.ROOT, "deploy", "app", "nginx.conf")
    assert weight_mod._display(inside) == os.path.join("deploy", "app", "nginx.conf"), (
        "同盘的路径没有被缩写成相对形式（打印会变得很难读）"
    )


def test_weight_script_survives_a_conf_outside_the_repo_drive(weight_mod, conf_copy,
                                                              monkeypatch):
    """`--conf` 指到仓库所在盘之外时，命令**不能崩**（最容易崩的是"打印路径"那一步）。

    `os.path.relpath` 在 Windows 上对不同盘符会抛 `ValueError`，而它只用来打印
    "改动了哪几份配置"。一旦这里崩掉，**权重其实已经写好**了，调用方却收到非零
    退出码，会误判成"没生效"然后重试 —— 属于"成功却报失败"。

    2026-10-06 实际踩到：`pytest.ini` 打开 `testpaths` 之后，裸 `pytest` 的 basetemp
    落在 `C:\\Users\\...\\Temp`（仓库在 `D:`），于是 release 工具链的三条用例变红。
    """
    if os.name != "nt":
        pytest.skip("跨盘符 relpath 只存在于 Windows 语义下")
    monkeypatch.setattr(weight_mod, "ROOT", "Z:" + os.sep + "not-the-repo")

    assert _rc(weight_mod.main, ["--conf", str(conf_copy), "show"]) == 0, (
        "`show` 在跨盘符路径上崩了（它要把每份配置的路径打印出来）"
    )
    assert _rc(weight_mod.main,
               ["--conf", str(conf_copy), "set", "--stable", "95", "--canary", "5"]) == 0, (
        "`set` 在跨盘符路径上崩了（权重已经写好了，却报了非零退出码）"
    )
    assert weight_mod.read_weights(str(conf_copy)) == {"stable": 95, "canary": 5}


def test_repo_entry_confs_agree_on_weights(weight_mod, monkeypatch):
    """仓库里的两份入口配置，权重必须一致（否则上面那条守卫迟早会在发布时踩到）。"""
    monkeypatch.delenv("NGINX_CONF", raising=False)
    assert weight_mod.read_weights(str(NGINX_CONF)) == \
        weight_mod.read_weights(str(NGINX_TLS_CONF)), (
            "deploy/app/nginx.conf 与 nginx.tls.conf 的灰度权重不一致 —— "
            "实际生效的取决于挂载了哪一份，请用 `python scripts/nginx_weight.py set` "
            "统一（该命令默认同时改两份）"
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


# ===========================================================================
# 回滚通知：必须覆盖**所有**退出路径，且通知失败不改回滚的退出码
# ===========================================================================
def _rollback_sandbox(tmp_path: Path, *, curl_body: str,
                      notify_rc: int) -> tuple[Path, Path]:
    """搭一个能跑到底的 rollback.sh 环境：外部命令全部用 shim 顶替。

    为什么值得这么麻烦：下面三条要验证的都是**进程真实退出时**才成立的性质
    （`trap` 有没有生效、退出码有没有被通知改写）—— 读脚本文本断言不出来。
    """
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    shutil.copy2(ROLLBACK, repo / "deploy" / "rollback.sh")
    (repo / ".env").write_text("ALERT_SINK_ROUTES=oncall:wecom\n", encoding="utf-8")
    (repo / "VERSION").write_text("9.9.9-test\n", encoding="utf-8")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = (tmp_path / "calls.log").as_posix()

    (bindir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "docker %s\\n" "$*" >> "{log}"\n'
        'case "$*" in\n'
        f"  *alert_sink.py*--notify*) exit {notify_rc} ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (bindir / "python").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "python %s\\n" "$*" >> "{log}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    (bindir / "curl").write_text(
        f"#!/usr/bin/env bash\nprintf '%s' '{curl_body}'\n",
        encoding="utf-8",
    )
    for f in bindir.iterdir():
        f.chmod(0o755)
    return repo, tmp_path / "calls.log"


def _run_rollback(repo: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = f"{(tmp_path / 'bin').as_posix()}{os.pathsep}{env.get('PATH', '')}"
    env["ROLLBACK_ENTRY"] = "http://127.0.0.1:9"   # curl 是 shim，这个地址不会被真的访问
    return subprocess.run([BASH, str(repo / "deploy" / "rollback.sh")],
                          capture_output=True, text=True, env=env, cwd=str(repo))


@pytest.mark.skipif(not BASH, reason="本机没有 bash（CI 上有）")
def test_rollback_notifies_on_the_success_path(tmp_path):
    repo, log = _rollback_sandbox(tmp_path, curl_body='{"release":"stable"}', notify_rc=0)
    r = _run_rollback(repo, tmp_path)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "--notify" in log.read_text(encoding="utf-8"), "成功路径没有发出回滚通知"
    assert "已发出回滚通知" in r.stdout


@pytest.mark.skipif(not BASH, reason="本机没有 bash（CI 上有）")
def test_rollback_notifies_even_when_verification_fails(tmp_path):
    """"回滚了但验证没通过"（退出码 2）也必须通知。

    它恰恰是最需要叫醒人的情形：权重改了、流量可能还有一部分留在灰度。
    只在成功分支里发通知，等于把最该通知的那一类整批漏掉。
    """
    repo, log = _rollback_sandbox(tmp_path, curl_body='{"release":"canary"}', notify_rc=0)
    r = _run_rollback(repo, tmp_path)
    assert r.returncode == 2, (
        f"期望验证失败的退出码 2，实际 {r.returncode}\n{r.stdout}\n{r.stderr}"
    )
    assert "--notify" in log.read_text(encoding="utf-8"), (
        "验证失败路径**没有**发出回滚通知 —— trap 没有覆盖这个出口"
    )


@pytest.mark.skipif(not BASH, reason="本机没有 bash（CI 上有）")
def test_rollback_exit_code_survives_notify_failure(tmp_path):
    """alert-sink 没在跑 / 通道没配 ⇒ 通知失败，但回滚本身仍然算成功。

    回滚是**止血动作**。让"通知没发出去"把退出码从 0 改成非 0，会让调用它的
    release.sh 与人以为回滚失败，进而重复回滚或转人工 ——
    代价远大于漏一条消息，而消息本身还可以手工补。
    """
    repo, log = _rollback_sandbox(tmp_path, curl_body='{"release":"stable"}', notify_rc=1)
    r = _run_rollback(repo, tmp_path)
    assert r.returncode == 0, (
        f"通知失败把回滚退出码改了（实际 {r.returncode}）—— 通知不该影响止血\n"
        f"{r.stdout}\n{r.stderr}"
    )
    assert "没能发出" in (r.stdout + r.stderr), "通知失败时没有明确告知运维"
