"""CI 工作流的**结构**校验。

为什么需要这个测试
------------------
CI 配置的错误和监控配置一样是**静默的**：YAML 合法、job 显示绿色，
但"本该被验证的东西"其实一次都没被验证。本项目最典型的三类：

  1. **只构建不启动**：`docker build` 成功只证明"层能构建"，
     不证明应用能起来（少 COPY 一个目录、缺 `libgl1`、`CMD` 写错，全都构建成功）。
     这个 job 会一直是绿的，而镜像根本跑不起来 —— 上线才发现。
  2. **扫了但不卡**：漏洞扫描没设 `exit-code`，于是它"永远成功"，
     只是往日志里打一堆 CVE。门禁形同虚设。
  3. **构建了但没 load**：`push: false` 又不 `load: true`，镜像只留在 buildx 缓存里，
     下一步 `docker run` 报 "Unable to find image" —— 报错点看起来像"冒烟测试写错了"。

这些都无法靠"人能记住"来防，所以固化成断言。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CI_FILE = ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def ci():
    assert CI_FILE.exists(), f"缺少 CI 工作流：{CI_FILE}"
    return yaml.safe_load(CI_FILE.read_text(encoding="utf-8"))


def _iter_steps(ci_cfg):
    """遍历全部 (job 名, job, step)。"""
    for job_name, job in (ci_cfg.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            yield job_name, job, step


def _find_step(ci_cfg, pred):
    for job_name, job, step in _iter_steps(ci_cfg):
        if pred(step):
            return job_name, job, step
    return None, None, None


def _build_step(ci_cfg):
    return _find_step(
        ci_cfg, lambda s: "docker/build-push-action" in str(s.get("uses", "")))


# ---------------------------------------------------------------------------
# 镜像构建
# ---------------------------------------------------------------------------
def test_ci_builds_the_service_image(ci):
    """CI 必须真的构建部署产物。

    代码测试全绿而镜像构建失败是完全可能的状态（lock 钉了不存在的版本、
    COPY 路径写错）。镜像才是上线的东西，它必须自己过一遍。
    """
    job_name, _job, step = _build_step(ci)
    assert step is not None, "CI 里没有镜像构建步骤 —— 部署产物从未被验证过"

    with_ = step["with"]
    assert with_.get("file") == "deploy/app/Dockerfile", (
        f"构建用的 Dockerfile 路径与仓库实际不符：{with_.get('file')}"
    )
    assert (ROOT / "deploy" / "app" / "Dockerfile").exists(), \
        "CI 引用的 Dockerfile 不存在"
    assert with_.get("context") == ".", (
        "Dockerfile 里 COPY 的是仓库根下的 src/ 与 app/ ⇒ 构建上下文必须是仓库根目录"
    )


def test_image_build_is_loaded_into_the_local_daemon(ci):
    """构建产物必须 `load` 到本地 docker，否则下一步的 `docker run` 找不到它。

    `push: false` 且不 load 时，镜像只存在于 buildx 的构建缓存里 ——
    报错出现在**下一步**（"Unable to find image"），很容易被误读成
    "冒烟测试的 run 命令写错了"，从而去改错的地方。
    """
    _job_name, _job, step = _build_step(ci)
    assert step is not None
    assert step["with"].get("load") is True, (
        "构建步骤缺 `load: true`（或设为 false）：镜像不会进本地 daemon，"
        "后面的 docker run 会失败"
    )


def test_image_build_uses_cache(ci):
    """几 GB 的 pip 安装层必须走 GHA 缓存，否则这个 job 会被"嫌慢"而绕过。"""
    _job_name, _job, step = _build_step(ci)
    assert step is not None
    assert "type=gha" in str(step["with"].get("cache-from", "")), \
        "构建步骤没用 cache-from: type=gha，每次 push 都要重装依赖"


# ---------------------------------------------------------------------------
# 冒烟启动
# ---------------------------------------------------------------------------
def _smoke_step(ci):
    """定位"把镜像跑起来并探活"的那一步。"""
    _job_name, _job, step = _find_step(
        ci, lambda s: "docker run" in (s.get("run") or "")
        and "/health/" in (s.get("run") or ""))
    return step


def test_ci_smoke_starts_the_built_image(ci):
    """构建成功 ≠ 能启动。必须真的把容器跑起来并探活。

    这一步是"部署产物可用"的唯一证据：少 COPY 一个目录、缺系统库、
    `CMD` 写错，全都**能构建成功**但起不来。
    """
    step = _smoke_step(ci)
    assert step is not None, (
        "CI 只构建镜像、没有把它跑起来探活 —— "
        "构建成功不代表应用能启动，这类问题只会在上线时暴露"
    )
    run = step["run"]
    assert "/health/live" in run, (
        "冒烟测试应探 /health/live（存活探针，不依赖外部服务）；"
        "探就绪探针会因为没配 DB 而失败，测的不是「镜像能不能起」"
    )


def test_ci_smoke_fails_the_job_on_boot_failure(ci):
    """探活失败必须让 job 变红，并打印容器日志。

    只 `docker logs` 不 `exit 1`（或反之）都会让这一步失去意义：
    前者让 job 变绿，后者只报"容器退出了"却不给原因。
    """
    step = _smoke_step(ci)
    assert step is not None
    run = step["run"]
    assert "exit 1" in run, "探活失败却没有 exit 1 —— 这一步永远不会让 CI 变红"
    assert "docker logs" in run, (
        "探活失败时没有打印容器日志 —— 只看到「容器退出」而没有原因，无法定位"
    )


# ---------------------------------------------------------------------------
# 漏洞扫描
# ---------------------------------------------------------------------------
def _scan_step(ci):
    _job_name, _job, step = _find_step(
        ci, lambda s: "trivy" in str(s.get("uses", "")).lower())
    return step


def test_ci_scans_the_image_for_vulnerabilities(ci):
    """必须有镜像漏洞扫描，且**真的会卡住**（exit-code 非 0）。"""
    step = _scan_step(ci)
    assert step is not None, "CI 没有镜像漏洞扫描步骤"
    with_ = step["with"]
    assert str(with_.get("exit-code", "0")) not in ("0", ""), (
        "扫描没设 exit-code —— 它只会往日志里打 CVE，永远不会让 CI 变红，"
        "等于没有这道门禁"
    )
    sev = str(with_.get("severity", ""))
    assert "CRITICAL" in sev and "HIGH" in sev, (
        f"扫描的严重度门限应为 HIGH,CRITICAL，当前是 {sev!r}"
    )
    assert str(with_.get("format")) == "table", (
        "扫描结果用 table 格式，出问题时人在 CI 日志里能直接看到明细"
    )


def test_ci_scan_ignores_unfixable_vulnerabilities(ci):
    """不卡"无可用修复"的 CVE。

    这类 CVE 你无法通过改代码解决，卡住它只会让门禁变成**习惯性失败** ——
    而习惯性失败比没有门禁更糟：真正可修的问题混在里面，没人再看。
    """
    step = _scan_step(ci)
    assert step is not None
    assert step["with"].get("ignore-unfixed") is True, (
        "未开 ignore-unfixed ⇒ 无补丁的 CVE 也会卡 CI，门禁会被绕过"
    )


def test_trivyignore_documents_an_expiry_policy():
    """忽略清单必须写明"每条都要有到期日"。

    没有到期日的忽略 = 永久忽略 = 悄悄把这条规则删掉。
    """
    f = ROOT / ".trivyignore"
    assert f.exists(), "缺少 .trivyignore（即使没有条目，也需要把策略写下来）"
    text = f.read_text(encoding="utf-8")
    assert "exp:" in text, ".trivyignore 没写到期日约定"
    assert "ignore-unfixed" in text, (
        ".trivyignore 应说明 CI 已开 ignore-unfixed，因此这里只放"
        "**可修但暂不升**的项，避免两种忽略混在一起"
    )


def test_trivyignore_entries_have_an_expiry_above_them():
    """任何**生效**的忽略项上方 4 行内必须有 `exp:`。

    这条防的是"随手加一行 CVE 就合入"——而这类改动恰恰最常见。
    """
    lines = (ROOT / ".trivyignore").read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        window = "\n".join(lines[max(0, i - 4):i])
        assert "exp:" in window, (
            f".trivyignore 第 {i + 1} 行是生效的忽略项，但上方没有到期日：{s}"
        )


# ---------------------------------------------------------------------------
# 一致性
# ---------------------------------------------------------------------------
def test_image_job_has_a_timeout(ci):
    """镜像 job 必须设超时。

    构建含几 GB 的 pip 安装，一次卡死会白烧最多 6 小时 runner 时间。
    """
    job_name, job, _step = _build_step(ci)
    assert job_name is not None
    t = job.get("timeout-minutes")
    assert isinstance(t, int) and 0 < t <= 60, (
        f"{job_name} 的 timeout-minutes 不合理：{t!r}（建议 30~60）"
    )


def test_image_job_is_not_gated_to_schedule_only(ci):
    """镜像构建 + 扫描不能只在定时任务里跑。

    只在 schedule 跑 ⇒ 绝大多数 PR 合入前，代码从没被"打包并启动"验证过，
    漏洞扫描也可能几天才跑一次。
    """
    job_name, job, _step = _build_step(ci)
    assert job_name is not None
    cond = str(job.get("if", ""))
    assert "schedule" not in cond, (
        f"{job_name} 被限制成只在 schedule 时运行（if: {cond}）——"
        "部署产物的验证应该跟着每次 push/PR 走"
    )


def test_all_ci_jobs_use_one_python_version(ci):
    """各 job 必须用同一个 Python 版本。

    版本漂移会造成"本地绿、CI 红"（或反过来），而这类差异极难排查 ——
    代码一样、结果不一样，第一反应总是去查代码。
    """
    versions: set[str] = set()
    for _job_name, _job, step in _iter_steps(ci):
        if "actions/setup-python" in str(step.get("uses", "")):
            versions.add(str((step.get("with") or {}).get("python-version")))
    assert len(versions) == 1, (
        f"CI 各 job 用了不同的 Python 版本：{sorted(versions)}"
    )


def test_build_smoke_and_scan_share_one_job_in_order(ci):
    """构建 / 冒烟 / 扫描必须在**同一个 job** 里，且顺序正确。

    两个很容易犯的错，且都不会给出明确的错误提示：

      - **拆到不同 job**：不同 job 跑在不同 runner 上，job A 构建出来的镜像
        在 job B 根本不存在。除非把镜像 push 到 registry 再 pull（本 workflow
        刻意 `push: false`），否则"扫描"扫的是空气、甚至报"找不到镜像"。
      - **顺序颠倒**：扫描排在构建之前，扫的是一个还不存在的镜像。

    顺序靠"步骤在列表中的下标"判定 —— 这正是 GitHub Actions 的执行依据。
    """
    build_job, _bjob, build_step = _build_step(ci)
    smoke_job, _sjob, smoke_step = _find_step(
        ci, lambda s: "docker run" in (s.get("run") or "")
        and "/health/" in (s.get("run") or ""))
    scan_job, _scjob, scan_step = _find_step(
        ci, lambda s: "trivy" in str(s.get("uses", "")).lower())

    assert build_job and smoke_job and scan_job, "构建/冒烟/扫描三步缺了一步"
    assert build_job == smoke_job == scan_job, (
        f"三步分散在不同 job：构建={build_job} 冒烟={smoke_job} 扫描={scan_job}；"
        "不同 job 在不同 runner 上，镜像不会自动可用"
    )

    steps = ci["jobs"][build_job]["steps"]
    idx = {id(s): i for i, s in enumerate(steps)}
    assert idx[id(build_step)] < idx[id(smoke_step)], "冒烟测试排在构建之前"
    assert idx[id(build_step)] < idx[id(scan_step)], "漏洞扫描排在构建之前"


# ---------------------------------------------------------------------------
# 检索质量门禁
# ---------------------------------------------------------------------------
def test_eval_gate_backend_exists_in_the_eval_script(ci):
    """CI 门禁里 `--only` 的后端名，必须在 eval_retrieval.py 的 combos 里真实存在。

    这条是被一次**真实事故**逼出来的：稀疏检索从 TF-IDF 换成 BM25 后，
    `eval_retrieval.py` 的 combos 里 `("tfidf", ...)` 改成了 `("bm25", ...)`，
    而 CI 那行 `--only tfidf` 忘了跟着改。后果分两种，都不好查：

      - `--only` 过滤掉所有组合 ⇒ `if not cand: sys.exit(2)`。CI 红是红了，
        但报的是"没有跑 tfidf 后端的结果" —— 把人指向门禁脚本，而不是那行配置；
      - 若写的值恰好落在 build_retriever 的**兜底分支**上（任何拼错的值都会落到
        BM25Retriever），它照样跑出一个数，只是那个数**不是门禁以为的那个后端**。

    `--gate-backend` 也得跟着看：CI 只传 `--only`、不传 `--gate-backend`，
    后者取脚本里的默认值。两者不一致时同样会 exit(2)。
    """
    import re

    _, _, step = _find_step(ci, lambda s: "eval_retrieval.py" in (s.get("run") or ""))
    assert step is not None, "CI 里找不到跑 eval_retrieval.py 的步骤"
    run = step["run"]

    m = re.search(r"--only\s+(\S+)", run)
    assert m, f"门禁没写 --only，会连需要外部密钥的后端一起跑：{run!r}"
    backend = m.group(1)

    # 后端清单从**脚本源码**里取，而不是在这里再抄一份常量 —— 抄一份就等于
    # 又造了一个会漂移的副本，而漂移正是这条测试要防的东西。
    src = (ROOT / "scripts" / "eval_retrieval.py").read_text(encoding="utf-8")
    assert "combos = [" in src, "eval_retrieval.py 里找不到 combos 定义，本测试需同步"
    block = src.split("combos = [", 1)[1].split("]", 1)[0]
    available = set(re.findall(r'"([a-z0-9_]+)"', block))

    assert backend in available, (
        f"CI 门禁的 --only={backend!r} 不在 eval_retrieval.py 的 combos "
        f"{sorted(available)} 里：门禁会因匹配不到组合而 exit(2)")

    g = re.search(r'--gate-backend",\s*default="(\w+)"', src)
    assert g, "eval_retrieval.py 的 --gate-backend 默认值取不到，本测试需同步"
    assert backend == g.group(1), (
        f"CI 只传 --only={backend!r}，而 --gate-backend 会用脚本默认值 "
        f"{g.group(1)!r}；两者不一致时门禁同样 exit(2)")


# ---------------------------------------------------------------------------
# 发布链路：CI 验证过的镜像 = 唯一发布制品
# ---------------------------------------------------------------------------
def _publish_job(ci):
    job = (ci.get("jobs") or {}).get("publish")
    assert job is not None, (
        "CI 没有 publish job —— 于是「构建过的东西 ≠ 部署过的东西」，"
        "发布靠人在服务器上重新构建，且无法反查线上跑的是哪个 commit"
    )
    return job


def test_publish_only_runs_on_main_or_version_tags(ci):
    """发布不能跟着每次 push / PR 走。

    一旦每个分支都推 registry，镜像仓库会堆满从未上线的构建产物，
    真正上线的那个 digest 也失去意义（"最新"成了噪音）。
    """
    cond = str(_publish_job(ci).get("if", ""))
    assert "refs/heads/main" in cond, (
        f"publish 没有限定 main 分支（if: {cond!r}）——会跟着每个分支的 push 发布"
    )
    assert "refs/tags/" in cond, "publish 没有覆盖版本 tag"
    assert "pull_request" not in cond, "publish 不该在 PR 上运行"


def test_publish_requires_the_verified_image(ci):
    """发布必须**依赖** image job。

    没有 `needs: image` 时，一个**冒烟失败 / 扫出 HIGH 漏洞**的镜像照样能推上
    registry —— 那不是"发布流程"，只是"又一个构建"。这一条是发布链路里
    唯一能阻止"没验证过的东西出门"的机制。
    """
    needs = _publish_job(ci).get("needs")
    needs = [needs] if isinstance(needs, str) else list(needs or [])
    assert "image" in needs, (
        f"publish 不依赖 image job（needs={needs}）——未通过冒烟与漏洞扫描的镜像"
        "也能被发布"
    )


def test_publish_also_waits_for_the_secret_scan(ci):
    """发布也必须等 security job —— 否则密钥扫描只是"装饰"。

    少这一条依赖会出现一个很难察觉的洞：gitleaks 真的拦下了一个提交进仓库的
    密钥、CI 因此变红，**而镜像照样被推上了 registry**。门禁变红但制品已经出门，
    等于门禁不存在。
    """
    needs = _publish_job(ci).get("needs")
    needs = [needs] if isinstance(needs, str) else list(needs or [])
    assert "security" in needs, (
        f"publish 不依赖 security job（needs={needs}）——密钥扫描失败时镜像仍会被发布"
    )


def test_publish_has_registry_and_oidc_permissions(ci):
    """必须显式申请 `packages: write` 与 `id-token: write`。

    这两条缺任意一条，表现为**权限错误**（push 被 403、cosign 拿不到 OIDC token），
    而报错信息不会告诉你"是 workflow 的 permissions 没写"。
    """
    perms = _publish_job(ci).get("permissions") or {}
    assert perms.get("packages") == "write", (
        "缺 permissions.packages: write —— GITHUB_TOKEN 无法推 GHCR"
    )
    assert perms.get("id-token") == "write", (
        "缺 permissions.id-token: write —— cosign keyless 签名拿不到 OIDC token"
    )


def test_publish_skips_instead_of_failing_without_registry(ci):
    """registry 不可用时必须**跳过**发布，而不是失败。

    这是刻意设计：仓库没启用 Packages / 组织策略禁止推送属于**配置缺失**，
    不是**代码坏了**。把它变红只会让每次合入都挂着一条与本次改动无关的红灯，
    很快被习惯性忽略 —— 那时真问题也一起被忽略。

    机制：登录放在一个带 `id` 的预检步骤里，后续推送/签名步骤统一
    `if: steps.<id>.outputs.<key> == '1'`。断言的是这个**结构**，
    因为它是"跳过而非失败"能成立的唯一原因。
    """
    job = _publish_job(ci)
    steps = job.get("steps") or []

    pre = [s for s in steps if s.get("id") and "login" in str(s.get("run", ""))]
    assert pre, "publish 里找不到带 id 的 registry 预检步骤"
    pre_id = pre[0]["id"]
    assert "GITHUB_OUTPUT" in str(pre[0].get("run")), (
        "预检步骤没有把结果写进 $GITHUB_OUTPUT，后面的步骤无法据此跳过"
    )

    push = [s for s in steps
            if "docker/build-push-action" in str(s.get("uses", ""))]
    assert push, "publish 里没有推送镜像的步骤"
    assert f"steps.{pre_id}.outputs" in str(push[0].get("if", "")), (
        f"推送步骤没有用 `if: steps.{pre_id}.outputs...` 门控 —— "
        "registry 不可用时它会真的失败，而不是跳过"
    )


def test_publish_tags_the_image_with_the_commit_sha(ci):
    """镜像标签必须含 commit 短哈希。

    `:main` 是个**会被下一次推送覆盖**的滚动指针，而本项目缺的恰恰是
    "线上跑的到底是哪个 commit"这个可反查性。只有 sha 标签能回答它。
    """
    steps = _publish_job(ci).get("steps") or []
    meta = [s for s in steps if "git rev-parse --short" in str(s.get("run", ""))]
    assert meta, "发布流程没有从 git 取 commit 短哈希来打标签"
    body = str(meta[0]["run"])
    assert "sha-" in body, "镜像标签里没有 sha- 前缀，无法从标签反查 commit"


def test_publish_builds_with_the_same_args_as_ci(ci):
    """发布的镜像必须用**与 CI 验证时相同的构建参数**。

    参数不一致 = "验证的"和"发布的"不是同一个东西 —— 前面所有验证（冒烟、
    漏洞扫描）都随之作废，而且不会有任何报错。
    """
    _j, _jjob, ci_build = _build_step(ci)
    steps = _publish_job(ci).get("steps") or []
    pub_build = [s for s in steps
                 if "docker/build-push-action" in str(s.get("uses", ""))]
    assert pub_build, "publish 里没有构建步骤"
    assert pub_build[0]["with"].get("build-args") == ci_build["with"].get("build-args"), (
        "发布构建与 CI 验证构建的 build-args 不一致："
        f"CI={ci_build['with'].get('build-args')!r} "
        f"发布={pub_build[0]['with'].get('build-args')!r}"
    )


# ---------------------------------------------------------------------------
# 代码层安全：门禁强度是**刻意分级**的
# ---------------------------------------------------------------------------
def _security_job(ci):
    job = (ci.get("jobs") or {}).get("security")
    assert job is not None, "CI 没有 security job（代码层 SAST / 依赖 / 密钥扫描）"
    return job


def test_security_job_scans_for_committed_secrets_and_gates(ci):
    """密钥扫描必须存在，且必须**真的卡住**。

    这是安全组里唯一该卡门禁的一项：泄露的凭据永远可修（删除 + 轮换），
    且代价最高 —— 凭据进了仓库历史之后，改代码是补不回来的。
    """
    steps = _security_job(ci).get("steps") or []
    g = [s for s in steps if "gitleaks" in str(s.get("run", "")).lower()]
    assert g, "security job 里没有密钥扫描步骤"
    assert not g[0].get("continue-on-error"), (
        "密钥扫描设了 continue-on-error —— 它永远不会让 CI 变红，等于没有这道门禁"
    )


def test_security_advisory_steps_stay_non_gating_until_baselined(ci):
    """bandit / pip-audit 目前是**刻意**只报不卡的。

    理由（不是"忘了改"）：两者第一次跑必然命中**存量**问题 ——
    bandit 会命中 src/rag 里 7 处 sha1（那是内容指纹，不是加密用途，其 B324
    规则无法区分）；pip-audit 会命中任何"尚无可用修复版本"的传递依赖。
    直接卡 ⇒ 第一次合入就是红的 ⇒ 接着必然被 continue-on-error 或删掉绕过。

    正确顺序是：先产出基线 → 逐条处置或写进带到期日的忽略清单 →
    **再**把 continue-on-error 去掉。这条断言防的是"有人看到 continue-on-error
    就顺手删掉"，而基线还没建立时那样做只会让门禁被整体绕过。
    """
    steps = _security_job(ci).get("steps") or []
    for tool in ("bandit", "pip-audit"):
        found = [s for s in steps if tool in str(s.get("run", ""))]
        assert found, f"security job 里没有 {tool} 步骤"
        assert found[0].get("continue-on-error") is True, (
            f"{tool} 改成了卡门禁。若已建立基线并逐条处置完毕，"
            "请连同本测试与上面的注释一起更新（并说明忽略清单在哪）"
        )


def test_gitleaks_config_keeps_default_rules(ci):
    """gitleaks 配置必须**沿用内置规则集**。

    `useDefault = false` 且不自带规则的配置会让扫描"通过"——但一条都没扫。
    这比不配更危险：它看起来是有防护的。允许清单只应做**定向**放宽（路径/正则），
    不应整类关掉规则。
    """
    f = ROOT / ".gitleaks.toml"
    assert f.exists(), (
        "缺少 .gitleaks.toml —— 没有允许清单时，.env.example 里的"
        "开发默认口令会被判成泄露，门禁会因假阳性被习惯性忽略"
    )
    import tomllib

    cfg = tomllib.loads(f.read_text(encoding="utf-8"))
    assert (cfg.get("extend") or {}).get("useDefault") is True, (
        "gitleaks 配置没有 extend.useDefault = true —— 内置规则被关掉了，"
        "扫描会「通过」但什么都没查"
    )
    assert (cfg.get("allowlist") or {}).get("paths"), (
        "允许清单里没有任何路径项，无法解释 .env.example 的占位值"
    )


