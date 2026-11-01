"""容量基线的契约。

容量数字本身不可断言（换机器就变），但**产生数字的方式**可以：

  1. 基准必须复用**线上同款**的检索与拼装函数。自造一条"评测专用"的路径，
     测出来的数字与线上无关 —— 这是最隐蔽的失真，因为数字看着很具体；
  2. `load_test.py` 默认必须打**不花钱**的端点。默认打 `/v1/chat` 意味着
     任何人跑一次"连接测试"就产生模型费用，而这件事没有任何提示；
  3. 直方图的**格数不得随观测次数增长** —— 这是"内存有界"唯一的证据，
     也是容量评估里唯一与请求量无关的内存保证。

第 3 条在 `tests/test_observability.py` 已有单元级覆盖，这里在**真实基准**上
再确认一次：基准跑的是生产代码路径，而不是被 mock 过的替身。
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("DB_URL", "")

ROOT = Path(__file__).resolve().parent.parent

pytest.importorskip("sklearn", reason="检索基准依赖 scikit-learn")


@pytest.fixture()
def bench():
    """加载基准脚本（它在导入时 chdir 到仓库根，用完必须还原）。"""
    cwd = os.getcwd()
    try:
        import scripts.bench_local as mod
        yield mod
    finally:
        os.chdir(cwd)


def _literal_strings(path: Path) -> set[str]:
    """源码里所有字符串字面量（含 f-string 的固定片段）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def test_bench_reuses_the_online_retrieval_path(bench):
    """检索基准必须走线上同款的三个函数。

    基准若自己拼上下文（比如只取 `chunk["text"]`），量到的就不是线上那条路径 ——
    而"读起来很具体的数字"最容易被当成结论。线上的拼装口径在
    `src/rag/retrieval.context_block`（父窗 > 保真内容 > 检索文本）。
    """
    src = (ROOT / "scripts" / "bench_local.py").read_text(encoding="utf-8")
    for fn in ("build_retriever", "context_block", "build_citations"):
        assert fn in src, (
            f"检索基准没有用到 {fn} —— 它量的不是线上路径，数字不可用于容量判断"
        )


def test_bench_keeps_a_histogram_slot_count_that_does_not_grow(bench):
    """直方图格数必须与观测次数无关（内存有界的唯一证据）。

    这是容量评估里唯一一条"与请求量无关的内存保证"：若它随请求增长，
    等于内存随 QPS 线性增长 —— 一个被刷的接口就能 OOM，而 `_count`
    一直增长看起来完全正常。
    """
    from app import metrics

    metrics.reset()
    small = bench.bench_metrics(2_000)
    slots_small = small["histogram_slots"]
    metrics.reset()
    big = bench.bench_metrics(50_000)
    slots_big = big["histogram_slots"]

    assert slots_small == slots_big == len(metrics.bucket_bounds()) + 1, (
        f"直方图格数随观测次数变化：{slots_small} → {slots_big}；"
        "说明实现退回了存原始观测值，内存会随 QPS 线性增长"
    )
    assert big["n"] == 50_000, "基准没有真的跑满样本"


def test_bench_reports_the_environment_with_every_run(bench):
    """每次运行都必须带上环境信息。

    没有机器上下文的容量数字**不可比**（换台机器就完全不同），
    而"贴一个数字、没有环境"是留档时最常见的退化方式。
    """
    import json
    import subprocess
    import sys

    env = dict(os.environ)
    env.update({"DB_URL": "", "LLM_API_KEY": "", "EMBEDDING_API_KEY": ""})
    r = subprocess.run([sys.executable, "scripts/bench_local.py", "--n", "500", "--json"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, cwd=str(ROOT))
    payload = json.loads(r.stdout[r.stdout.index("{"):])   # 跳过前面的日志行
    assert payload["host"]["cores"] and payload["host"]["python"], (
        "基准输出缺少环境信息 —— 这样的数字无法与其它机器比较"
    )
    assert payload["n"] == 500


# ---------------- 压测脚本的安全默认 ----------------

def test_load_test_defaults_to_a_free_endpoint():
    """压测脚本默认必须打**不花钱**的端点。

    默认打 `/v1/chat` 意味着任何人跑一次"连接测试"就会产生模型费用，
    而这件事不会有任何提示 —— 破坏性默认值不能靠文档提醒来兜。
    """
    src = (ROOT / "scripts" / "load_test.py").read_text(encoding="utf-8")
    # 注意 `.*?` 而不是 `[^)]*?`：`choices=("health", "chat")` 本身就含 `)`，
    # 用"排除右括号"的写法会匹配不到（本用例第一版就是这样红的）。
    m = re.search(r'"--endpoint".*?default\s*=\s*"([^"]+)"', src, re.S)
    assert m, "找不到 --endpoint 的默认值"
    assert m.group(1) != "chat", (
        f"压测默认打的是 {m.group(1)!r} —— 会花钱。应为 health 这类免费端点"
    )


def test_capacity_doc_uses_valid_endpoint_values():
    """文档里写的 `--endpoint X` 必须是脚本真的接受的取值。

    文档与参数脱节时不会报错，只会在读者执行的那一刻失败 ——
    而此时他正需要这份文档来得出容量结论。
    """
    src = (ROOT / "scripts" / "load_test.py").read_text(encoding="utf-8")
    m = re.search(r'"--endpoint".*?choices=\(([^)]*)\)', src, re.S)
    assert m, "找不到 --endpoint 的可选值"
    allowed = set(re.findall(r'"(\w+)"', m.group(1)))

    doc = (ROOT / "deploy" / "CAPACITY.md").read_text(encoding="utf-8")
    used = set(re.findall(r"--endpoint (\w+)", doc))
    assert used, "CAPACITY.md 里没有给出任何压测命令"
    bad = sorted(used - allowed)
    assert not bad, (
        f"CAPACITY.md 用了脚本不接受的 --endpoint 取值：{bad}；可用值：{sorted(allowed)}"
    )


def test_load_test_requires_an_explicit_opt_in_to_spend():
    """打真实对话必须要求显式的付费开关（`--yes-spend`）与密钥。"""
    src = (ROOT / "scripts" / "load_test.py").read_text(encoding="utf-8")
    assert "--yes-spend" in src, (
        "压测脚本没有付费开关 —— 调用大模型会直接产生费用"
    )
    assert "--api-key" in src, "压测脚本没有要求提供鉴权密钥"


def test_capacity_doc_commands_point_at_existing_scripts():
    """文档里给出的复现命令必须真的存在。

    文档最容易腐化的地方就是命令：脚本改名后文档照旧，读者执行时才发现 ——
    而此时他已经不知道该信哪个数字了。
    """
    doc = (ROOT / "deploy" / "CAPACITY.md").read_text(encoding="utf-8")
    referenced = set(re.findall(r"python (scripts/[\w_]+\.py)", doc))
    assert referenced, "CAPACITY.md 里没有给出任何可复现的命令"
    missing = sorted(p for p in referenced if not (ROOT / p).exists())
    assert not missing, f"文档引用了不存在的脚本：{missing}"


def test_capacity_doc_admits_what_is_not_measured_yet():
    """文档必须明确标注"端到端尚未实测"。

    把本机微基准读成容量结论是最容易犯的错（差三到四个数量级），
    所以这份文件必须先自己声明边界。
    """
    doc = (ROOT / "deploy" / "CAPACITY.md").read_text(encoding="utf-8")
    assert "尚未执行" in doc or "待办" in doc, (
        "CAPACITY.md 没有标注哪些还没实测 —— 读者会把微基准当成容量基线"
    )
