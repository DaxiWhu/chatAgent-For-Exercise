"""依赖锁定的一致性守护。

为什么需要这个测试
------------------
`requirements.txt` 全用 `>=`（表达意图），`requirements.lock` 钉死版本（保证可复现）。
两处名单一旦漂移，后果是**静默的**：新人加了个包、忘了重新生成 lock，
构建依旧成功，只是那个包**没有受到版本约束** —— 直到某天上游发新版才炸。

CI 的 `test` job 刻意不装 docling/transformers，所以这里校验的是
**静态一致性**（声明与锁定对齐），不是"装没装得上"。后者由 Dockerfile 构建负责。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQ = ROOT / "requirements.txt"
LOCK = ROOT / "requirements.lock"


def _norm(name: str) -> str:
    """PyPI 包名归一化：Pdfminer.six / pdfminer-six / pdfminer.six 是同一个包。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse(path: Path) -> list[tuple[str, str]]:
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]+\])?\s*(.*)$", line)
        if m:
            out.append((m.group(1), (m.group(3) or "").strip()))
    return out


def _lock_pins() -> dict[str, str]:
    pins = {}
    for name, spec in _parse(LOCK):
        if spec.startswith("=="):
            pins[_norm(name)] = spec[2:]
    return pins


def test_lock_file_exists():
    """缺了锁定清单，构建就回到"每次装什么都看运气"的状态。"""
    assert LOCK.exists(), "缺少 requirements.lock；跑 `python scripts/lock_requirements.py --write` 生成"


def test_lock_has_no_extras():
    """lock 里**不能**出现 extras（`psycopg[binary]==3.3.3`）。

    这条不是风格要求，是 pip 的**解析期**硬错误：

        ERROR: Constraints cannot have extras

    退出码 1，且在碰索引之前就失败 —— 它不会表现为"装不上某个包"，
    而是整条 `pip install -r requirements.txt -c requirements.lock` 当场退出，
    报错信息和"哪一行写错了"离得很远。extras 只能写在 `requirements.txt` 里。

    ⚠️ 这条守卫此前**并不存在**：`requirements.lock` 的注释里写着"由
    `tests/test_dependency_hashes.py` 的解析期守卫钉住"，但那个文件从来没有过
    （真实文件叫 `test_dependency_lock.py`）。**一条指向不存在守卫的注释，
    比没有注释更危险** —— 它让人以为已经被保护了。

    ⚠️ 判据必须直接看**原始行**而不能走 `_parse`：那个解析器的正则会把
    `[...]` 当成"可选 extras 段"吃掉，于是 extras 恰好检测不出来。
    """
    bad = [ln.strip() for ln in LOCK.read_text(encoding="utf-8").splitlines()
           if "[" in ln.split("#", 1)[0]]
    assert not bad, (
        "requirements.lock 里出现 extras —— pip 会在解析期直接报 "
        "`ERROR: Constraints cannot have extras` 并退出 1：\n  " + "\n  ".join(bad)
    )


def test_every_declared_package_is_pinned():
    """requirements.txt 里声明的每个包，lock 里都必须有精确版本。"""
    pins = _lock_pins()
    missing = [n for n, _ in _parse(REQ) if _norm(n) not in pins]
    assert not missing, (
        f"以下包在 requirements.txt 里声明了但未锁定版本：{missing}\n"
        "跑 `python scripts/lock_requirements.py --write` 重新生成。"
    )


def test_no_orphans_in_lock():
    """lock 里不该有 requirements.txt 已删除的包 —— 否则长期误导后来人。"""
    declared = {_norm(n) for n, _ in _parse(REQ)}
    orphan = [k for k in _lock_pins() if k not in declared]
    assert not orphan, f"lock 里有 requirements.txt 已不声明的包：{orphan}"


def test_lock_pins_are_exact_versions():
    """锁定项必须是 `==x.y.z`，不能混入 `>=`（那等于没锁）。

    这条防的是"把 lock 写成第二份 requirements.txt"这种退化。
    """
    bad = [(n, s) for n, s in _parse(LOCK) if not s.startswith("==")]
    assert not bad, f"锁定清单里出现非精确版本约束：{bad}"


def test_requirements_do_not_use_exact_pins():
    """反向约束：requirements.txt 应保持范围约束。

    如果两处都写 `==`，就会出现"改版本要改两个文件、漏改一处则静默不一致"。
    职责分离是这套双层结构成立的前提。
    """
    pinned = [(n, s) for n, s in _parse(REQ) if s.startswith("==")]
    assert not pinned, (
        f"requirements.txt 里不该出现精确版本（那是 requirements.lock 的职责）：{pinned}"
    )


def test_dockerfile_uses_the_lock_file():
    """Dockerfile 必须真的引用 lock —— 有文件但不用，等于没锁。"""
    dockerfile = (ROOT / "deploy" / "app" / "Dockerfile").read_text(encoding="utf-8")
    assert "-c requirements.lock" in dockerfile, (
        "Dockerfile 未使用 requirements.lock，构建依然不可复现"
    )
    assert "COPY requirements.txt requirements.lock" in dockerfile, (
        "Dockerfile 未把 lock 文件拷进构建上下文，`-c` 会找不到它"
    )


def test_ci_workflow_runs_the_lock_check():
    """CI 里必须有这道门禁，否则漂移只能靠人自觉。"""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "lock_requirements.py" in ci, "CI 未执行依赖锁定一致性检查"


def test_checker_script_passes_on_current_tree():
    """端到端：直接跑校验脚本，确认它在本仓库现状下是通过的。"""
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "lock_requirements.py")],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert r.returncode == 0, f"锁定校验未通过：\n{r.stdout}\n{r.stderr}"


def test_langsmith_integration_stays_declared():
    """LangSmith 的 agent 侧集成必须**显式声明**，不能只靠传递依赖。

    为什么单独守这一条
    ------------------
    `langsmith` 是 `langchain-core` 的传递依赖，所以"不写这一行也在装"。正因为如此，
    它最容易被当成"写不写都行"删掉 —— 删了构建照样成功，测试照样全绿，直到上游把
    下限降回没有 `openai_agents_sdk` 的版本，`app/tracing.py` 的 import 才会失败，
    然后**静默降级**为不产生 trace（只剩一行 warning，服务照常起）。症状是
    "配了 key、平台没数据"，既没有异常也没有指标。

    判据是三条，缺一条这个风险就回来：
      ① requirements.txt 声明了它，且带下限；
      ② requirements.lock 钉了精确版本；
      ③ 钉的版本不低于清单下限（否则锁了个不合格的版本，等于没锁）。
    """

    def _v(text: str) -> tuple[int, ...]:
        return tuple(int(x) for x in text.split(".") if x.isdigit())

    spec = dict(_parse(REQ)).get("langsmith")
    assert spec, (
        "requirements.txt 没声明 langsmith —— 它只是 langchain-core 的传递依赖，"
        "既不能靠它保证版本，也没法在它缺集成时被构建拦住"
    )
    m = re.match(r">=\s*([0-9][0-9.]*)", spec)
    assert m, f"langsmith 的约束应是 >=x.y.z 的下限形式，现在是 {spec!r}"

    pin = _lock_pins().get("langsmith")
    assert pin, "requirements.lock 没钉 langsmith —— 上游发新版就会改变构建结果"
    assert _v(pin) >= _v(m.group(1)), (
        f"lock 钉的 langsmith {pin} 低于清单下限 {m.group(1)}："
        "这个版本可能没有 openai_agents_sdk 集成，接上去只会静默不产生 trace"
    )


def test_psycopg_is_declared_with_the_binary_extra():
    """`psycopg` 必须带 `[binary]`，否则**镜像里连不上数据库**。

    为什么这条是硬需求而不是优化
    ----------------------------
    不带 extra 时装的是**纯 Python 实现**，它需要一个可用的 libpq；`python:3.13-slim`
    里没有，于是运行时的真实表现是（2026-11-06 首次真跑 e2e 时暴露）：

        /health/ready -> checks.db.ok = false
        ImportError: no pq wrapper available.
        - couldn't import psycopg 'c' implementation: No module named 'psycopg_c'
        - couldn't import psycopg 'binary' implementation: No module named 'psycopg_binary'
        - couldn't import psycopg 'python' implementation: libpq library not found

    ⚠️ 这个故障**自我掩盖得很彻底**：`/health/live` 仍是 200，`/health/ready`
    只是 `degraded`（不是 503 拒绝），服务看起来"起来了"。实际后果是会话状态写不进库
    ——检查点恒为内存，重启丢状态且跨轮历史静默失效。

    ⚠️ 为什么本地/CI 的单元测试发现不了：它们的安装路径是 `pip install
    "psycopg[binary]>=3.2"`（见 ci.yml 的 test job），**只有镜像构建**走
    `requirements.txt`。两份清单不同，就会"测试全绿、容器带病"。
    """
    # ⚠️ 不能用 `_parse()` 的结果判断：它把 extras 放进单独的 group 里，
    #    `spec` 只剩 `>=3.2`，extras 根本不在里面 —— 拿它断言会**永远失败**。
    #    这里直接读原始行，才能看见 `[binary]`。
    lines = [
        raw.split("#", 1)[0].strip()
        for raw in REQ.read_text(encoding="utf-8").splitlines()
    ]
    decl = [ln for ln in lines if re.match(r"^psycopg\[", ln) or re.match(r"^psycopg\s*[><=]", ln)]
    assert decl, "requirements.txt 没声明 psycopg"
    assert any(ln.startswith("psycopg[binary]") for ln in decl), (
        f"psycopg 的声明是 {decl}，缺少 `[binary]` extra —— "
        "镜像里会装成纯 Python 版，连不上 PostgreSQL"
    )

    # 反向：约束文件里**不能**出现 extras（那是 pip 解析期硬错误，见 test_lock_has_no_extras）。
    # 两件事一起钉住，才不会有人"为了修这个"去改 lock。
    lock_spec = dict(_parse(LOCK)).get("psycopg") or ""
    assert not lock_spec.startswith("["), (
        f"requirements.lock 里不该有 extras（现在是 {lock_spec!r}）—— "
        "extras 只写在 requirements.txt，lock 只钉版本"
    )
