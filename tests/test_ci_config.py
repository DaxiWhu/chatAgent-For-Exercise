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

import re
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


def test_ci_smoke_gives_the_key_the_service_refuses_to_start_without(ci):
    """探活必须传 `LLM_API_KEY`：缺 Key 时**拒绝启动是刻意设计**，不是缺陷。

    这条守的是"口径"而不是"代码"：`app.main._assert_agents_ready()` 明确写着
    `missing_api_key ⇒ 拒绝启动`（"能起来但每个请求都答不出来"不是可接受的部署
    状态）。所以「不传任何环境变量也能起」这个断言**永远不可能成立** ——
    它从写下那天起就是红的，只是 image job 之前没人真跑过。

    ⇒ 探活必须给最小必需配置；同时"没给 Key 时必须拒绝启动"这条要单独反向断言
    （见 `test_ci_smoke_pins_the_fail_closed_contract`），两者不能合并成一条。
    """
    step = _smoke_step(ci)
    assert step is not None
    run = step["run"]
    # ⚠️ 断言必须落在**命令**上，不能只搜关键词 —— 这一步的注释里也写着
    # `LLM_API_KEY`，只搜关键词的话，把命令删掉、注释留着，守卫照样是绿的。
    assert "-e LLM_API_KEY=" in run, (
        "探活没有把 LLM_API_KEY 传进容器 —— 服务刻意在缺 Key 时拒绝启动"
        "（missing_api_key），这一步会必然失败：现象是 60 秒探活超时，"
        "容器日志里是 RuntimeError"
    )


def test_ci_smoke_pins_the_fail_closed_contract(ci):
    """反向断言：**不给 Key 必须拒绝启动**，且真因必须落在 `missing_api_key`。

    为什么这条不能省：正向探活只证明"给了配置能起来"。如果哪天有人为了"方便
    本地跑"把 `_assert_agents_ready()` 放宽成 warning，正向探活**照样是绿的**,
    而线上就会退化成"服务在，但每次都答不出来"。只钉正向等于没钉这条契约。

    三个细节都是必要的，少一个就失去判据：
      · `timeout` —— 区分"自己退出"与"挂住 60 秒"（rc=124），后者不算拒绝启动；
      · `rc != 0` —— 区分"拒绝启动"与"居然起来了"；
      · `grep missing_api_key` —— 区分"因为缺 Key 退出"与"因为别的原因崩了"
        （镜像坏了也会非 0 退出，那不是 fail-closed 的证据）。
    """
    step = _smoke_step(ci)
    assert step is not None
    run = step["run"]
    assert "timeout 60 docker run" in run, (
        "无 Key 的反向断言没有 timeout —— 分辨不出「自己退出」与「挂住不退出」"
    )
    assert 'grep -q "missing_api_key"' in run, (
        "反向断言只看了退出码，没核对真因 —— 镜像坏了同样会非 0 退出，"
        "那不是 fail-closed 的证据。"
        "（断言落在 grep 命令上：注释里也出现 missing_api_key，只搜关键词抓不住删除）"
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
# 热路径性能门禁
# ---------------------------------------------------------------------------
def _bench_job(ci):
    """跑 `scripts/bench_local.py` 的那个 job。

    按"哪一步真的跑了它"**推导**，不写死 job 名 —— 写死的话，改个 job 名
    就会让下面两条断言指向一个已经不存在的对象（然后它们会以"找不到"的方式
    红，或者更糟：被顺手删掉）。
    """
    for job_name, job in (ci.get("jobs") or {}).items():
        runs = "\n".join(str(s.get("run", "")) for s in (job.get("steps") or []))
        if "bench_local.py" in runs:
            return job_name, job
    return None, None


def test_ci_runs_the_bench_gate_in_a_way_that_can_actually_fail(ci):
    """性能门禁必须真的在跑，且**真的会卡**。

    两种写法都会让它变成一个"永远绿"的空壳，而两者都没有任何症状：

      - **少写 `--check`**：`bench_local.py` 默认只是打印一屏数字然后 **退出 0**
        （`--check` 才是唯一会返回非 0 的模式）⇒ 这个 job 从此与性能无关，
        但它看起来每天都在"跑性能测试"；
      - **`continue-on-error: true`**：脚本返回 1 也只是日志里多一行，job 依旧绿。

    与漏洞扫描那条守卫同一个道理（没设 `exit-code` 的扫描 = 只往日志里打 CVE）。
    这个更隐蔽，因为"跑过了"和"卡住了"在 CI 的绿色对勾上长得一模一样。
    """
    job_name, job = _bench_job(ci)
    assert job_name, "CI 里没有任何 job 跑 scripts/bench_local.py —— 性能门禁不存在"

    gate_steps = [s for s in (job.get("steps") or [])
                  if "bench_local.py" in str(s.get("run", ""))]
    assert gate_steps, f"{job_name} job 里找不到跑 bench_local.py 的步骤"

    missing = [s.get("name") for s in gate_steps
               if "--check" not in str(s.get("run", ""))]
    assert not missing, (
        f"这些步骤跑了 bench_local.py 却没带 `--check`：{missing} —— "
        "不带 `--check` 时脚本只打印一屏数字并退出 0，这个 job 会永远绿"
    )
    non_gating = [s.get("name") for s in gate_steps if s.get("continue-on-error")]
    assert not non_gating, (
        f"这些步骤设了 continue-on-error：{non_gating} —— "
        "门禁失败不会让 CI 变红，等于没有这道门禁"
    )


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
    # ⚠️ 只看**命令行**，不看注释：注释里解释"为什么不再用 --no-git"时当然会出现
    # 这个词，按整段文本断言会把说明文字当成违规 —— 这条断言自己踩过一次。
    commands = "\n".join(
        ln for ln in str(g[0].get("run", "")).splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert "--no-git" not in commands, (
        "密钥扫描又加回了 `--no-git`：它只看**当前工作树**，看不到 git 历史里的密钥。\n"
        "而历史里的泄露恰恰是最该被发现的一种 —— 文件可以从 HEAD 删掉，对象仍在\n"
        "`.git` 里可 `git show` 取出，凭据也没有因此失效（只能轮换）。\n"
        "本仓库曾因此整整漏掉两个真实的 embedding 密钥（rag/vectorSaveTest.py）。\n"
        "实测：加 `--no-git` 时 0 命中，扫全历史时 2 命中。"
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


def test_gitleaks_download_survives_transient_5xx_but_still_fails_when_it_cannot(ci):
    """下载 gitleaks 的 curl 必须带重试，**但这一步仍然不许 continue-on-error**。

    2026-11-08 的 `security` 红在

        curl: (22) The requested URL returned error: 504

    —— 卡了两分钟之后 GitHub 的 release 下载返回 504，全仓库一行代码没改。
    这类红会被当成"网络问题重跑一次"，而真正危险的是它引出的那种"修法"：
    给这一步加 `continue-on-error`（或者把 `set -eu` 去掉）。那样"下载不到"
    与"扫过且干净"在 CI 里长得一模一样 —— 门禁从此可以永远绿着什么都不查。

    正确的取舍：瞬时 5xx 重试；重试仍失败就照旧红。
    """
    steps = _security_job(ci).get("steps") or []
    g = [s for s in steps if "gitleaks" in str(s.get("run", "")).lower()]
    assert g, "security job 里没有密钥扫描步骤"
    step = g[0]
    run = str(step.get("run", ""))
    download = [
        ln for ln in run.splitlines()
        if "releases/download" in ln or "curl" in ln
    ]
    assert download, f"没找到下载 gitleaks 的那条命令：\n{run}"
    joined = "\n".join(download)
    assert "--retry" in joined, (
        "下载 gitleaks 的 curl 没有重试 —— 一次 504 就会让整条密钥门禁变红，"
        "而它与代码毫无关系（2026-11-08 实际发生过）"
    )
    assert not step.get("continue-on-error"), (
        "密钥扫描步骤加了 continue-on-error：下载失败/扫描失败都会变成绿，"
        "而'没扫'与'扫过且干净'必须能被区分 —— 这正是它当初没加这个开关的原因"
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


def test_gitleaks_allowlist_does_not_exempt_the_shipped_template():
    """路径放行不许把**随仓库分发**的文件整类豁免掉，`.env.example` 尤其不行。

    `.env.example` 是被复制出去用的模板 —— 恰恰是最容易被人"顺手试一下新配置项"
    而粘进真密钥的地方。此前那条 `^\\.env(\\..*)?$` 看着只是在忽略"本地 .env"，
    实际把 `.env.example` 一起放过了（`.env` + `.example`）。放行一个随仓库走的
    文件，等于把门禁在这条路径上整个关掉，而且**没有任何提示** —— 那行正则看起来
    人畜无害。

    两个方向都钉：`.env.example` 不许被路径放行；本地 `.env` 仍必须被放行
    （否则本机口令会一路报警，把真泄露淹掉 —— 那也是一种失效）。
    """
    import re
    import tomllib

    cfg = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
    paths = (cfg.get("allowlist") or {}).get("paths") or []

    bad = [p for p in paths if re.match(p, ".env.example")]
    assert not bad, (
        f".env.example 被路径允许清单放行了：{bad}\n"
        "它是随仓库分发的公开模板，必须继续被扫描"
    )
    assert any(re.match(p, ".env") for p in paths), (
        "本地 .env 不再被放行 —— 本机口令会变成常驻假阳性，反而把真泄露淹掉"
    )
    assert (cfg.get("allowlist") or {}).get("regexes"), (
        "允许清单里没有正则项 —— .env.example 的占位值只能靠正则规则挡住，"
        "不能靠路径规则"
    )


def test_gitleaks_path_allowlist_is_cross_platform():
    """路径放行必须同时认 `/` 与 `\\`，否则它只在 CI（Linux）上生效。

    实测（gitleaks 8.18.4 / Windows）：把 `tmp_probe/` 写成只带正斜杠时，
    Windows 传进来的 `tmp_probe\\sops_scratch\\...` **完全不匹配** ⇒ 本地扫工作树
    得到 28 处命中，全部落在本该被放行的目录里。

    危害不在"本地多几条告警"，而在于它会训练出"本地那堆告警都是假的"这个习惯，
    而那条习惯正好会盖掉真泄露。同一个跨平台分隔符的坑，本仓库在 sops 的判据里
    已经踩过一次（那里写的就是 `[/\\\\]`）。
    """
    import tomllib

    cfg = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
    paths = (cfg.get("allowlist") or {}).get("paths") or []
    offenders = [p for p in paths if "/" in p and "[/\\\\]" not in p]
    assert not offenders, (
        "这些路径放行只认正斜杠，在 Windows 上不生效：\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# ⑪ action 引用必须钉在 commit SHA 上 / 发布产物必须验签
# ---------------------------------------------------------------------------
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_USES_RE = re.compile(r"^\s*-?\s*uses:\s*(\S+)(.*)$", re.M)


def _workflow_files() -> list[Path]:
    """所有 workflow（不只 ci.yml）—— 新增一个 workflow 就绕过守卫是最典型的漏。"""
    return sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def test_every_action_is_pinned_to_a_commit_sha():
    """任何 `uses:` 都不允许用可变引用（tag / 分支 / 主版本号）。

    tag 是**可以被上游重指向**的，而 action 的代码拿得到本仓库的 secrets、
    `GITHUB_TOKEN`，`publish` job 还额外有 `packages: write` 与 `id-token: write`。
    上游账号被盗或恶意重打 tag 一次，就等于在我们的流水线里执行任意代码。
    commit SHA 不可变，是唯一能钉住的形态。

    本地 action（`./path`）与 `docker://` 引用不算 —— 前者随仓库一起评审，
    后者是镜像引用（应另按 digest 钉，不属本条范围）。
    """
    offenders: list[str] = []
    for wf in _workflow_files():
        for i, line in enumerate(wf.read_text(encoding="utf-8").splitlines(), 1):
            m = _USES_RE.match(line)
            if not m:
                continue
            ref = m.group(1)
            if ref.startswith("./") or ref.startswith("docker://"):
                continue
            if "@" not in ref:
                offenders.append(f"{wf.name}:{i} 缺少 @ref：{ref}")
                continue
            _, _, version = ref.rpartition("@")
            if not _SHA_RE.match(version):
                offenders.append(f"{wf.name}:{i} 用了可变引用 @{version}：{ref}")
    assert not offenders, (
        "以下 action 没有钉在 40 位 commit SHA 上（上游可重指向该 ref ⇒ 可执行任意代码）：\n  "
        + "\n  ".join(offenders) + "\n"
        "升级请用 `gh api repos/<org>/<repo>/commits/<tag> --jq .sha`，"
        "或交给 .github/dependabot.yml（它理解 SHA 钉法）"
    )


def test_pinned_actions_carry_a_readable_version_comment():
    """钉了 SHA 就必须在后面标注真实版本号。

    只留一串 SHA 的后果：下一次 CVE 响应时，没人能一眼看出用的是哪个版本、
    该不该升；而逐个去 GitHub 反查的成本高到不会有人做 —— 于是这些 pin 会
    长期停在旧版本上，"钉住"变成了"冻住"。
    """
    offenders: list[str] = []
    for wf in _workflow_files():
        for i, line in enumerate(wf.read_text(encoding="utf-8").splitlines(), 1):
            m = _USES_RE.match(line)
            if not m:
                continue
            ref, rest = m.group(1), m.group(2)
            if ref.startswith("./") or ref.startswith("docker://"):
                continue
            _, _, version = ref.rpartition("@")
            if _SHA_RE.match(version) and not re.search(r"#\s*v?\d", rest):
                offenders.append(f"{wf.name}:{i} {ref}")
    assert not offenders, (
        "以下 SHA 钉住的行没有 `# vX.Y.Z` 注释（将来无法一眼看出是哪个版本）：\n  "
        + "\n  ".join(offenders)
    )


def _publish_steps(ci) -> list[dict]:
    """publish job 的全部步骤。

    ⚠️ 必须**限定在 publish job 内**查找，不能用全局的 `_find_step`：
    后者返回 `(job_name, job, step)` 三元组、且按 job 顺序返回**第一个**命中 ——
    任何别的 job 里出现同样的字符串都会让它指到别处去（例如测试脚本里的一句
    `cosign verify`），于是断言检查的根本不是发布链路。
    """
    return list(_publish_job(ci).get("steps") or [])


def _run_commands(step) -> str:
    """step 的 `run` 里**真正会被执行**的行（去掉注释与空行）。

    ⚠️ 不能直接在原始 `run` 文本上搜关键字：注释里出现 `cosign verify`
    （比如解释为什么签完要验）就会被当成"这一步在执行验签"。
    本仓库在 gitleaks 门禁上踩过同一个坑 —— 断言扫到了 ci.yml **注释里**的
    `--no-git`，于是"禁止用 --no-git"的守卫被自己的说明文字撞红。
    """
    return "\n".join(
        ln.strip() for ln in str(step.get("run", "")).splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    )


def _publish_step(ci, needle: str):
    return next((s for s in _publish_steps(ci) if needle in _run_commands(s)), None)


def _publish_step_running(ci, command: str):
    """找 publish job 里**以该命令开头执行**的那一步。

    ⚠️ 为什么不能用"文本里含 `cosign verify`"来定位：发布摘要那一步会用
    `echo "cosign verify \\" ...` 把它打印出来给运维看，于是**提到**它的步骤
    先于/后于**执行**它的步骤出现。用子串匹配会认错步骤 ——
    更糟的是：把真正的验签步骤删掉之后，摘要那一步仍然满足断言（它同样含
    `--certificate-identity-regexp`），守卫会**假装通过**。
    """
    for step in _publish_steps(ci):
        for line in _run_commands(step).splitlines():
            if line.startswith(command):
                return step
    return None


def test_publish_verifies_the_signature_it_produced(ci):
    """发布链路必须**验证**自己签出来的签名，而不是只签。

    只签不验等于没有签名保证：签错对象（tag 而不是 digest）、identity/issuer
    与预期不符、仓库 OIDC 没开 —— 这些情况下 `cosign sign` 仍可能返回 0，
    而**使用方** `cosign verify` 时才失败。那时才会有人发现"这批镜像根本验不过"，
    而它已经在生产里跑了很久。这一步同时充当给运维的命令模板。
    """
    step = _publish_step_running(ci, "cosign verify")
    assert step, "publish job 里没有**真的执行** `cosign verify` 的步骤 —— 签名从未被验证过"

    run = _run_commands(step)
    assert "--certificate-identity-regexp" in run, (
        "验签没校验 identity：任何人都能用**自己仓库**的 workflow 签一个同名镜像，"
        "只验 issuer 等于没验"
    )
    assert "--certificate-oidc-issuer" in run, "验签没校验 OIDC issuer"
    assert "token.actions.githubusercontent.com" in run, (
        "issuer 不是 GitHub Actions 的 OIDC 发行方 —— 等于允许别处签发的证书"
    )
    assert "steps.push.outputs.digest" in run, (
        "验签验的不是本次构建产物的 digest —— 那验的是别的东西"
    )


def test_publish_signs_the_digest_not_the_tag(ci):
    """签名对象必须是 **digest**：tag 可以被重新指向，签 tag 等于签了一个会变的东西。"""
    step = _publish_step_running(ci, "cosign sign")
    assert step, "publish job 里没有执行 `cosign sign` 的步骤"
    run = _run_commands(step)
    assert "steps.push.outputs.digest" in run, (
        "签名没有签在构建产物的 digest 上（签名对象会随 tag 漂移）：\n" + run
    )


def test_publish_depends_on_every_gate(ci):
    """`publish` 的 `needs` 必须覆盖**所有**在 push 事件下会跑的质量门禁 job。

    这条守的洞：门禁 job 红了，镜像照样推送 + 签名发布。
    2026-10-06 修前实测就是 `needs: [image, test, security]`，而
    `lint` / `agent-session-pg` / `eval-gate` 三个门禁全在门外 ——
    其中 `eval-gate`（检索召回 / 答案质量）意味着"改提示词导致质量退化"这件事
    挡不住发布；而同一段注释当时就已经写着"没验证过的东西不许出门"。

    ⚠️ 判据是**推导**而不是写死一串名字：写死的清单在新增 job 时会静默过期
    （新门禁不在 needs 里 → 它红了没人管 → 且不会有任何报错）。这里的规则是
    "凡是没有 schedule/workflow_dispatch 专属 `if` 的 job，都必须在 needs 里"，
    新增 job 会自动被纳入要求。
    """
    jobs = ci.get("jobs") or {}
    publish = jobs.get("publish")
    assert publish is not None, "publish job 不见了"

    needs = publish.get("needs") or []
    if isinstance(needs, str):            # YAML 允许 `needs: foo` 的单值写法
        needs = [needs]
    needs = set(needs)

    assert needs <= set(jobs), (
        f"publish 的 needs 里有无此 job 的名字：{sorted(needs - set(jobs))} —— "
        "GitHub Actions 会因此直接判整个工作流配置无效"
    )

    # 只在定时/手动触发下才跑的 job：push 时它们是 skipped，纳入 needs 会
    # 让 publish **一起被跳过**（skipped 会传染依赖方），所以必须排除。
    scheduled_only = set()
    always_on = set()
    for name, job in jobs.items():
        if name == "publish":
            continue
        cond = str(job.get("if") or "")
        if "schedule" in cond or "workflow_dispatch" in cond:
            scheduled_only.add(name)
        else:
            always_on.add(name)

    missing = sorted(always_on - needs)
    assert not missing, (
        f"publish 的 needs 少了这些门禁：{missing}\n"
        f"（当前 needs={sorted(needs)}；push 时会跑的 job={sorted(always_on)}）\n"
        "这些 job 红了，镜像仍会被推送 + 签名发布 —— 门禁等于不存在。"
    )

    # 反向锁：被排除的那几个必须是"确实只在定时触发"的，否则上面的排除逻辑会变成万能免死牌
    #
    # `agent-quality`（2026-11-08 加）为什么可以不在 publish 的 needs 里：
    #   它跑的是**真模型**的工具三层评测，随上游模型漂移、要密钥，本身就不是
    #   "这次提交对不对"的判据 —— 放进门禁会让一次模型抖动挡住发布。它的用途是
    #   每晚发现"工具链是不是变差了"，红了发通知、不拦合并。所以它只在 schedule /
    #   dispatch 跑，与 full-deps / drill / e2e 同一类。
    assert scheduled_only == {"full-deps", "drill", "e2e", "agent-quality"}, (
        f"被当成『定时专属』排除的 job 集合变了：{sorted(scheduled_only)}。"
        "如果不是它们真的都只在 schedule/dispatch 跑，请同步复核本守卫的排除规则 —— "
        "否则任何 job 只要加一句 if 就能绕过发布门禁。"
    )


# ---------------------------------------------------------------------------
# e2e：`.env` 必须把 compose 的**全部**必填变量都填上
# ---------------------------------------------------------------------------
# 2026-11-06 的真实事故
# ---------------------
# `e2e` job 此前**从没真跑过**（只在 schedule/dispatch 触发）。首次执行时，起栈
# 第一步就失败：
#     error while interpolating services.app-stable.environment.DB_URL:
#     required variable POSTGRES_PASSWORD is missing a value
#
# 起因是两件**各自都正确**的事撞在一起：
#   · `compose` 里口令是 `${VAR:?…}` 必填（`test_password_vars_are_required_not_defaulted`
#     在守它，这是对的 —— 默认值等于把口令公开写在仓库里）；
#   · CI 用 `cp .env.example .env` 起头，而模板里口令**故意留空**
#     （`test_env_example_does_not_ship_weak_passwords` 在守它，也是对的）。
# 于是"复制模板"这一步永远不可能让栈起来，而缺的是哪几个变量、一共几个，
# 全靠一条 compose 报错（它还只报**第一个**）来告诉你。
#
# 这条守卫把那个隐式依赖变成显式的：**compose 里出现的每个 `${VAR:?}`，
# e2e 的准备步骤都必须给它生成一个非空值**。新增必填变量而忘了填 ⇒ 在这里红，
# 而不是等到凌晨两点的定时任务里、藏在一行 compose 报错后面。
_COMPOSE_FOR_E2E = ROOT / "deploy" / "app" / "docker-compose.yml"


def test_e2e_env_step_fills_every_required_compose_variable(ci):
    """e2e 准备 `.env` 的步骤必须覆盖 compose 里所有 `${VAR:?}` 必填变量。"""
    compose_text = _COMPOSE_FOR_E2E.read_text(encoding="utf-8")
    # ⚠️ 必须**剔除注释行**再扫：compose 的说明文字里有 `${VAR:?...}` 这样的
    # **示范写法**（用来解释这个语法的含义），它不是真变量。不剔的话会扫出
    # 一个叫 `VAR` 的"必填变量"，然后永远要求 CI 去填一个不存在的东西。
    compose_code = "\n".join(
        line for line in compose_text.splitlines()
        if not line.lstrip().startswith("#")
    )
    # 只看 `${NAME:?` 这一种写法；`${NAME:-默认}` 是"有默认值"，不属于必填
    required = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*):\?", compose_code))
    assert required, (
        "没能从 compose 里解析出任何 `${VAR:?}` 必填变量 —— "
        "要么写法变了，要么这条守卫的匹配规则已失效"
    )
    assert "VAR" not in required, (
        "扫出了字面量 `VAR` —— 说明注释剔除没生效（compose 的说明里用 "
        "`${VAR:?...}` 作示范写法），这条守卫会去要求一个不存在的变量"
    )

    job = ci["jobs"]["e2e"]
    # 找到"准备 .env"那一步（名字可能微调，按 cp .env.example 认）
    prep = [s for s in job["steps"] if "cp .env.example .env" in str(s.get("run", ""))]
    assert len(prep) == 1, (
        f"e2e 里找不到唯一一个『准备 .env』的步骤（找到 {len(prep)} 个）—— "
        "本守卫依赖它来核对必填变量"
    )
    script = str(prep[0]["run"])

    # 每个必填变量都必须在脚本里被显式赋值/生成
    missing = sorted(v for v in required if v not in script)
    assert not missing, (
        f"compose 里这些变量是 ${{VAR:?}} 必填，但 e2e 的 .env 准备步骤没有填它们：{missing}\n"
        "模板里它们是空值，所以栈会在第一步就报 "
        "'required variable ... is missing a value' 并退出。\n"
        "修法是在该步骤里现生成一个非空值（不要给模板填默认值 —— 那等于把口令写进仓库）。"
    )

    # 光"出现变量名"还不够：注释里写一遍就能骗过。要求它出现在赋值/生成语句里。
    for var in sorted(required):
        assert re.search(
            r"(?:set_pw\s+" + var + r"\b|" + var + r"=[^\n]*(?:openssl|rand|\\$\\())[^\n]*"
            r"|printf[^\n]*" + var + r"=)",
            script,
        ), (
            f"{var} 在 e2e 的准备步骤里出现了，但看不出是一次**赋值/生成**。\n"
            "（本守卫要求它被真的赋值，而不是只在注释里提一句 —— "
            "注释里提到变量名不会让 compose 拿到值。）"
        )

    # 反向自检必须存在：同类问题在起栈阶段的表现是"哪个容器没起来"，
    # 真因藏在 compose 的一行报错里。在准备阶段自己拦一下，排查成本差一个量级。
    assert "是空的" in script or "missing" in script.lower(), (
        "e2e 的准备步骤没有对『必填变量为空』做自检 —— "
        "那么下次再漏填，仍然只会得到一行 compose 报错"
    )


# ---------------------------------------------------------------------------
# 镜像的 slim 变体：不许把"硬依赖"当成 docling 的包袱一起卸掉
# ---------------------------------------------------------------------------
# 2026-11-06 的真实事故（CI `image` job）
# -------------------------------------
# `INSTALL_DOCLING=0` 的卸载清单里带了 `tiktoken`，于是容器启动即
#     ModuleNotFoundError: No module named 'tiktoken'
# `/health/live` 60 秒不通，job 红在"容器没起来"上。
#
# 为什么 `tiktoken` 绝不能进那份清单：
#   它是 `langchain-openai` 的**硬依赖**（`tiktoken<1.0.0,>=0.7.0`），
#   而 `langchain_openai` 在**模块导入期**就 `import tiktoken`。
#   我们的导入链 app.main → … → src.rag.retrieval → `from langchain_openai import ChatOpenAI`
#   是服务启动的必经之路 ⇒ 卸掉它 = 镜像一定起不来。
DOCKERFILE = ROOT / "deploy" / "app" / "Dockerfile"

#: 无论装不装 docling 都必须存在的包（服务启动链上的硬依赖）。
_MUST_SURVIVE_SLIM = ("tiktoken",)


def test_slim_variant_does_not_uninstall_hard_dependencies():
    """`INSTALL_DOCLING=0` 的 uninstall 清单里不许出现启动链的硬依赖。"""
    text = DOCKERFILE.read_text(encoding="utf-8")
    m = re.search(r"pip uninstall -y ([^\n|;&]+)", text)
    assert m, (
        "Dockerfile 里找不到 slim 变体的 `pip uninstall -y ...` —— "
        "要么写法变了，要么瘦身逻辑被删了，这条守卫需要同步更新"
    )
    removed = set(m.group(1).split())

    for pkg in _MUST_SURVIVE_SLIM:
        assert pkg not in removed, (
            f"{pkg} 出现在 slim 变体的卸载清单里：{sorted(removed)}\n"
            f"{pkg} 是 langchain-openai 的硬依赖，而 langchain_openai 在导入期就 import 它 ——"
            " 卸掉它镜像一定起不来（容器 60 秒探活超时，真因藏在 docker logs 里）。"
        )


def test_slim_variant_uninstalls_the_distribution_that_holds_the_code():
    """瘦身清单必须按**分发包名**写，而且要包含 `docling-slim`。

    `docling` 从 2.x 起拆成三块：`docling`（空元包）+ `docling-slim`（真正的代码）
    + `docling-parse`（解析引擎）。`import docling` 这个名字属于 `docling-slim`。
    所以只写 `pip uninstall -y docling` 只会卸掉那个空元包，
    **代码一直在镜像里**，而没有任何一步会报错 ——
    证据是镜像扫描报告里出现 `docling-slim (METADATA) 2.129.0`（2026-11-07）。
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    m = re.search(r"pip uninstall -y ([^\n|;&]+(?:\n\s+[^\n|;&]+)*)", text)
    assert m, "Dockerfile 里找不到 slim 变体的卸载清单"
    removed = set(m.group(1).replace("\\", " ").split())

    assert "docling-slim" in removed, (
        f"slim 变体的卸载清单里没有 docling-slim：{sorted(removed)}\n"
        "`docling` 已经是空元包，真正的代码在 docling-slim 里 —— "
        "只卸元包 = 以为瘦了，其实代码和漏洞都还在"
    )


def test_slim_variant_verifies_the_uninstall_actually_happened():
    """卸完必须**自检**：指名的那几个包真的没了。

    只写 `pip uninstall -y ... || true` 时，上游改名 / 名字写错都**静默通过**
    （上一版就是这么漏掉 `docling-slim` 的：卸载命令返回 0，包还在）。
    自检要覆盖"到底有没有真的卸掉"这一层，而不是只看命令返回码。
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert 'pip show "$pkg"' in text, (
        "slim 变体卸完没有自检 —— `pip uninstall` 的名字写错时它照样返回 0，"
        "镜像会带着本该卸掉的包继续构建（症状是体积和漏洞都没变）"
    )


def test_dockerfile_self_checks_the_startup_import_chain():
    """Dockerfile 必须在构建期验证"服务启动链"能导入。

    为什么值得一条守卫：探活失败的表现是"容器 60 秒没起来"，真因埋在
    `docker logs` 里；构建期报错直接指到缺哪个包，成本差一个量级。

    两个细节都要断言，否则这条自检本身会变成"看起来在验、其实恒过"：
      · 必须导入 `app.main`（真正的启动路径），不能只 `import tiktoken`；
      · 必须在 `COPY src`/`COPY app` **之后** —— 代码没拷进来时导入必然失败，
        那是位置错、不是依赖错，会让人误判。
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "import app.main" in text, (
        "Dockerfile 没有在构建期导入 app.main —— "
        "镜像可能构建成功但一启动就 ImportError"
    )

    copy_pos = text.find("COPY app ./app")
    check_pos = text.find("import app.main")
    assert copy_pos != -1, "Dockerfile 里找不到 `COPY app ./app`"
    assert check_pos > copy_pos, (
        "构建期自检写在了 `COPY app ./app` 之前 —— 那时代码还没进镜像，"
        "`import app.main` 必然失败，报的是位置错而不是依赖错"
    )


# ---------------------------------------------------------------------------
# full-deps：必须先真的把模型下下来，测试步骤才可能绿
# ---------------------------------------------------------------------------
def test_full_deps_prefetches_docling_models_before_testing(ci):
    """`full-deps` 必须在下测试之前预取模型，且测试步骤显式离线。

    `tests/conftest.py` 默认 `HF_HUB_OFFLINE=1`，它写着"模型本来就在本地缓存里"
    —— 前提是开发机跑过预取脚本（`models/` 430MB，**没入库**）。CI runner 是干净
    环境 ⇒ 没有缓存 ⇒ docling 报
        Cannot find an appropriate cached snapshot folder
    于是表格/扫描件那 5 条用例全红，而真因是"没下模型"。

    为什么口径是"预取时联网、测试时离线"而不是"把离线开关关掉"：
    关掉之后测试就依赖网络了 —— 网络一抖就是随机红，这类红会训练人"重跑一次就好"，
    门禁慢慢失效。分开之后，"模型在不在"与"代码对不对"各自独立可解释。
    """
    job = ci["jobs"]["full-deps"]
    steps = job["steps"]
    names = [str(s.get("name") or "") for s in steps]

    # ① 存在预取步骤，且调用的是仓库里那个绕开 HF 缓存的脚本
    prefetch_idx = next(
        (i for i, s in enumerate(steps)
         if "fetch_docling_models.py" in str(s.get("run", ""))),
        None,
    )
    assert prefetch_idx is not None, (
        "full-deps 没有预取 docling 模型 —— CI runner 没有模型缓存，"
        "表格/扫描件用例会因为『没下模型』而红（真因不是解析逻辑坏了）"
    )

    # ② 预取必须在下测试之前
    test_idx = next(
        (i for i, s in enumerate(steps) if "pytest" in str(s.get("run", ""))),
        None,
    )
    assert test_idx is not None, "full-deps 里找不到跑 pytest 的步骤"
    assert prefetch_idx < test_idx, (
        f"预取步骤（#{prefetch_idx} {names[prefetch_idx]!r}）排在测试步骤"
        f"（#{test_idx} {names[test_idx]!r}）**之后** —— 那时测试已经红了"
    )

    # ③ 测试步骤必须显式钉住离线 + 指定 artifacts 目录
    test_step = steps[test_idx]
    env = test_step.get("env") or {}
    assert str(env.get("HF_HUB_OFFLINE")) == "1", (
        "full-deps 的测试步骤没有显式 HF_HUB_OFFLINE=1 —— "
        "模型若缺，用例会卡在 TLS 握手上等几分钟，而不是立刻失败"
    )
    assert env.get("DOCLING_ARTIFACTS_PATH"), (
        "full-deps 的测试步骤没有 DOCLING_ARTIFACTS_PATH —— "
        "docling 会回去找 HF 缓存（symbolic link / .lock 问题在 CI 上同样存在）"
    )


# ---------------------------------------------------------------------------
# e2e：起栈前必须先把镜像单独构建出来
# ---------------------------------------------------------------------------
def test_e2e_builds_the_image_before_starting_the_stack():
    """`tasks.py e2e --build` 必须先单独 build，再 `up`。

    `app-canary` 刻意没有 `build:` 段（它的镜像是发布脚本踢过来的），只有
    `image: ${CANARY_IMAGE:-chatagent:<ver>}`。而 `docker compose up --build`
    的执行顺序是**先并行拉取所有 image，再去构建带 build 段的服务** ——
    于是 canary 在"stable 还没构建出来"的瞬间去找那个 tag，本地没有 ⇒
    转去 Docker Hub 拉 ⇒ `pull access denied for chatagent` ⇒ 整条 up 失败。
    实测时序（CI）：13:38:51 canary 拉取失败 → 13:41:23 stable 才构建完成。
    """
    tasks_py = (ROOT / "scripts" / "tasks.py").read_text(encoding="utf-8")

    # ⚠️ 只看 `task_e2e` 函数体，**不要**在全文件里搜 —— 文件里还有
    # `compose("up -d", args)`（起全栈）与 observability profile 那两处，
    # 全局正则先命中它们，于是"up 里还带 --build"这个变异根本抓不到
    # （实测过：第一版守卫就是这么漏掉的）。
    m = re.search(r"\ndef task_e2e\(.*?(?=\ndef )", tasks_py, re.S)
    assert m, "scripts/tasks.py 里找不到 task_e2e 函数"
    body = m.group(0)

    # 必须存在一次独立的 build（不是只靠 up --build）
    assert re.search(r'compose\(\s*["\']build\s+app-stable["\']', body), (
        "tasks.py 的 e2e 没有先单独 `compose build app-stable` —— "
        "只用 `up --build` 时，app-canary 会在 stable 构建完成前就去远端拉镜像"
    )
    # 且 up 本身不该再带 --build（带了就说明顺序没改，只是加了一条无用语句）
    up_line = re.search(r'compose\(\s*f?["\'][^"\']*up -d[^"\']*["\']', body)
    assert up_line, "task_e2e 里找不到 `up -d` 调用"
    assert "--build" not in up_line.group(0), (
        f"`up` 命令里仍带 --build（{up_line.group(0)!r}）—— "
        "那会让 compose 继续走『先并行 pull 再 build』的旧顺序"
    )


# ---------------------------------------------------------------------------
# e2e job 的「最小依赖」是一份契约
# ---------------------------------------------------------------------------
# 这一节守的不是 YAML 里某一行写没写，而是**这份清单能不能一直保持最小**。
# 2026-11-07 第四次真跑，e2e 的 6 条用例**全部 ERROR**（不是断言失败，是 setup 阶段）：
#
#     ModuleNotFoundError: No module named 'langchain_openai'
#
# 真因不在 e2e，而在 `tests/conftest.py` 的一条 autouse fixture：它 import
# `src.rag.retrieval`，而那个模块**模块级**就 `from langchain_openai import ChatOpenAI`。
# 于是一个"只发 HTTP 请求"的黑盒测试进程，被强行拖进整棵服务端依赖树。
#
# 当时有两条修法：① 让 fixture 在缺依赖时什么都不做；② 往 e2e 的安装清单里加
# `langchain-openai`。选 ① 的理由不是省事，而是**职责**：黑盒进程里根本不存在
# retrieval 的进程级状态（`_EMBED_DOWN_UNTIL`），这条 fixture 对它没有任何意义。
# 选 ② 会让"最小依赖"这条口径开始腐蚀 —— 今天加 langchain-openai，明天 docling、
# 后天 torch，最后 e2e 变成一个装满服务端依赖却只发 HTTP 的进程。
#
# 所以这里钉两件事：清单里不许出现服务端依赖；以及**在那种环境里 conftest 真的能加载**。
def _e2e_install_step(ci_cfg):
    for job_name, _job, step in _iter_steps(ci_cfg):
        if job_name != "e2e":
            continue
        if "pip install" in str(step.get("run", "")):
            return step
    return None


def test_e2e_job_stays_a_black_box_client(ci):
    """e2e 的安装清单里不该出现**服务端**依赖。

    它测的是"容器里的服务能不能用"，本进程只负责发 `urllib` 请求 + 签一个 JWT。
    加进 `langchain-*` / `docling` / `torch` 这类包，等于把 e2e 变成"半个全量测试"，
    而且会掩盖"某条本地 fixture 越界"这类问题（这正是 2026-11-07 那次的原因）。
    """
    step = _e2e_install_step(ci)
    assert step is not None, "e2e job 里找不到安装依赖的步骤"
    run = str(step.get("run", ""))
    for forbidden in ("langchain", "docling", "torch", "transformers", "openai-agents"):
        assert forbidden not in run, (
            f"e2e 的安装清单里出现了服务端依赖 {forbidden!r}：\n{run}\n"
            "它是黑盒客户端（被测服务在容器里），加服务端依赖会让这份清单失去意义。"
            "如果是因为 conftest 里的 fixture 越界，请修 fixture，而不是补依赖。"
        )


def test_conftest_loads_without_server_side_dependencies(tmp_path):
    """**行为**校验：只装 pytest + python-dotenv 的环境里，conftest 必须能加载。

    为什么不写成"断言 conftest.py 的源码里有 try/except"：那种断言在把
    try/except 换成别的写法（比如 `importlib.util.find_spec` 预判）后会误报，
    而它想防的事（"纯黑盒进程里 setup 就炸"）**一次都没被真正验证**。

    这里用子进程复现 e2e 的真实处境：
      · 一个 `meta_path` 拦截器让 `import langchain_openai` 抛 ModuleNotFoundError
        —— 与"这台机器没装它"对 Python 而言是同一件事；
      · `-p tests.conftest` 把仓库的 conftest 当插件装进来（探针文件在 tmp 下，
        不会被自动发现），于是它的 autouse fixture 会真的跑起来；
      · 探针用例本身什么都不做，**它能通过就说明 fixture 没有把服务端依赖拖进来**。

    ⚠️ 变异验证（2026-11-07 实测）：把 fixture 里的 `except ModuleNotFoundError`
    改成 `except ZeroDivisionError`，这条用例立刻红，报的正是 CI 上那句
    `ModuleNotFoundError: No module named 'langchain_openai'`。
    """
    import os
    import subprocess
    import sys

    blocker = tmp_path / "block_langchain.py"
    blocker.write_text(
        "import sys\n"
        "\n"
        "\n"
        "class _Blocker:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'langchain_openai' or name.startswith('langchain_openai.'):\n"
        "            raise ModuleNotFoundError(f'No module named {name!r}', name=name)\n"
        "        return None\n"
        "\n"
        "\n"
        "sys.meta_path.insert(0, _Blocker())\n",
        encoding="utf-8",
    )

    probe = tmp_path / "test_probe.py"
    probe.write_text("def test_probe():\n    assert True\n", encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(tmp_path)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(probe), "-q",
         "-p", "tests.conftest", "-p", "block_langchain",
         "-p", "no:cacheprovider"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, (
        "在没有 langchain_openai 的环境里，conftest 加载失败了 —— 这正是 e2e job "
        "（只装 pytest + python-dotenv）会踩的坑。\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )





# --------------------------------------------------------------------------
# e2e 的真模型：Key 从 secret 来，没有就必须**说出来**
# --------------------------------------------------------------------------
def _e2e_env_step(ci_cfg):
    """e2e 里「准备 .env」那一步（`cp .env.example .env` 所在步骤）。"""
    for job_name, _job, step in _iter_steps(ci_cfg):
        if job_name != "e2e":
            continue
        if ".env.example" in str(step.get("run", "")):
            return step
    return None


def test_e2e_job_takes_the_real_model_key_from_a_secret(ci):
    """e2e 必须能把真模型 Key 注入 `.env`。

    `tests/test_e2e_stack.py::test_chat_run_completes_with_token_and_done` 要真模型，
    而它跑不跑由这里的一个 secret 决定。两件事必须成对：
      · 有 secret、没有那条用例 ⇒ 花了钱还是只验了分帧；
      · 有那条用例、没有 secret ⇒ 永远 skipped，等于没覆盖。
    """
    step = _e2e_env_step(ci)
    assert step is not None, "e2e job 里没找到准备 .env 的步骤"

    env = step.get("env") or {}
    assert "secrets.DEEPSEEK_API_KEY" in str(env), (
        f"准备 .env 的步骤没引用 secrets.DEEPSEEK_API_KEY（现在是 {env}）—— "
        f"没有它『链路跑通』那条用例会一直 skipped"
    )

    run = str(step.get("run", ""))
    assert "LLM_API_KEY=${E2E_LLM_KEY}" in run, (
        "引用了 secret 却没把它写进 .env 的 LLM_API_KEY —— "
        "服务读的是 .env，不是这一步的环境变量"
    )


def test_e2e_job_says_aloud_when_it_has_no_real_model(ci):
    """缺 secret 时必须打印说明，不能悄悄退化成「只验分帧」。

    这不是措辞检查，是**可见性**检查：CI 全绿却没人覆盖 agent 主链路，
    正是 2026-11-07 那类假绿的成因。降级可以，但要留在日志里。
    """
    step = _e2e_env_step(ci)
    assert step is not None, "e2e job 里没找到准备 .env 的步骤"
    run = str(step.get("run", ""))

    assert "else" in run, "只有『有 Key』的分支 —— 缺 secret 时会静默跳过注入"
    assert "DEEPSEEK_API_KEY" in run, "没说缺的是哪个 secret，看日志的人不知道该配什么"
    assert "skip" in run, "没说那条用例会 skip —— 等于把『没覆盖』当成了『通过』"


# ---------------------------------------------------------------------------
# 真模型质量 job（agent-quality）：守"工具三层到底有没有人在看"
# ---------------------------------------------------------------------------
# 这一 job 的失效方式全是静默的：
#   · 进了 PR 路径 ⇒ 随模型漂移随机红 ⇒ 有人 `continue-on-error` 掉 ⇒ 守着等于没有；
#   · 缺 secret 却"跳过" ⇒ 屏幕上绿，而这一层一条断言都没跑；
#   · 只跑不比基线 ⇒ 每天打印三个百分数，没人看得出是变好还是变坏；
#   · `--json` 写成基线文件本身 ⇒ 先写后读自己 ⇒ 永远"持平"（自愈门禁）；
#   · `LLM_API_BASE` 没设 ⇒ 客户端打 api.openai.com ⇒ DeepSeek 的 key 401。
def _agent_quality_job(ci_cfg):
    job = (ci_cfg.get("jobs") or {}).get("agent-quality")
    assert job, "缺少 agent-quality job —— 真模型那一层在 CI 里就完全没人跑了"
    return job


def _agent_quality_run(ci_cfg) -> str:
    job = _agent_quality_job(ci_cfg)
    runs = [str(s.get("run", "")) for s in job.get("steps") or [] if "--baseline" in str(s.get("run", ""))]
    assert runs, "agent-quality 里没有一步带 --baseline —— 它只是在'跑一遍'，看不出变化"
    return "\n".join(runs)


def test_agent_quality_only_runs_on_schedule(ci):
    """不许进 PR 路径。真模型评测要密钥、随模型漂移，随机红的终点是被关掉。"""
    job = _agent_quality_job(ci)
    cond = str(job.get("if") or "")
    assert "schedule" in cond and "workflow_dispatch" in cond
    assert "pull_request" not in cond and "push" not in cond


def test_agent_quality_fails_loudly_without_the_secret(ci):
    """缺 secret 必须红并说清缺什么 —— "跳过"会让这一层静默变空。"""
    text = _agent_quality_run(ci)
    assert "exit 1" in text, "缺 secret 时没有让 job 失败"
    assert "DEEPSEEK_API_KEY" in text, "没说缺的是哪个 secret"
    assert not _agent_quality_job(ci).get("continue-on-error")


def test_agent_quality_compares_against_the_shipped_baseline(ci):
    text = _agent_quality_run(ci)
    assert "scripts/agent_tool_eval_baseline.json" in text


def test_agent_quality_never_overwrites_its_own_baseline(ci):
    """`--json` 不能指向基线文件：写完再读自己，每一项都会算成持平。"""
    text = _agent_quality_run(ci)
    m = re.search(r"--json\s+(\S+)", text)
    assert m, "没把明细落盘：退化时没法看是哪一条坏的"
    assert m.group(1) != "scripts/agent_tool_eval_baseline.json"
    assert ">>" not in text and "sed -i" not in text, "命令行里出现了改写基线的动作"


def test_agent_quality_points_at_the_real_gateway(ci):
    """`LLM_API_BASE` 为空时 openai 客户端会打官方地址，DeepSeek 的 key 在那里 401。

    症状是"任务失败但看不出为什么"—— 它不会说"地址配错了"。
    """
    job = _agent_quality_job(ci)
    env = {}
    for step in job.get("steps") or []:
        env.update(step.get("env") or {})
    assert str(env.get("LLM_API_BASE", "")).startswith("https://api.deepseek.com")
    assert env.get("AGENT_STRUCTURED_OUTPUT") == "json_object", (
        "DeepSeek 不支持 json_schema：三个 as_tool 专家会全部 400，而选择层可能照样满分"
    )


def test_the_tool_eval_baseline_ships_with_the_repo():
    """基线必须是仓库里的文件，且字段齐全。

    指向一个不存在的位置 ⇒ 脚本退出码 2 ⇒ nightly 从第一天起天天红；
    字段缺一层 ⇒ 对比静默跳过那一层（屏幕上写"未对比"，而退出码是 0）。
    """
    import json

    path = ROOT / "scripts" / "agent_tool_eval_baseline.json"
    assert path.is_file(), "基线文件没有随仓库分发"
    data = json.loads(path.read_text(encoding="utf-8"))
    summary = data.get("summary") or {}
    for key in ("turns_total", "turns_passed", "tool_total", "calls_total", "result_total"):
        assert key in summary, f"基线缺 {key}：这一层会被静默跳过"
    assert data.get("cases"), "基线没有 cases：无法判断两次跑的是不是同一批用例"
