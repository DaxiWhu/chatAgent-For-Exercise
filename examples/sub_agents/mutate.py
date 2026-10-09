"""变异验证：把实现改坏，确认 selftest 真的会红。

    python -m examples.sub_agents.mutate

**为什么示例也要做这件事**：一组恒绿的断言说明不了任何问题 —— 它可能压根没检查到东西。
把被测实现按已知方向改坏，看断言是否恰好在那一条上失败，才能证明断言有效。

本脚本守着四条纪律（都是本项目踩过坑换来的，缺一条就可能**一边报成功一边把源码改坏**）：

1. **预检唯一匹配**：每个锚点在目标文件里必须恰好出现 1 次，任一不中即整体退出
   —— 否则会"静默少跑一项"却照报 N/N；
2. **收尾 SHA-256 自证还原**：`finally` 不防 SIGTERM，所以除了 finally 之外，
   正常路径结束还要再比对一次全量哈希；
3. **读写一律 ``newline=""``**：Windows 上默认的文本模式会把整个文件的 ``\\n``
   换成 ``\\r\\n``，而哈希自证**抓不住**它（CRLF 往返自洽 ⇒ 照报"已还原"）；
4. **跑动期间不要编辑被变异的文件**：还原是按"每轮开始读到的文本"回写，
   并发编辑会被静默丢弃。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent

__all__ = ["main"]

SELFTEST = "examples.sub_agents.selftest"


@dataclass(frozen=True)
class Mutation:
    mid: str
    filename: str
    old: str
    new: str
    expect_group: str
    why: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        mid="M1",
        filename="runner.py",
        old="        current_agent = result.last_agent  # ← 官方 airline 的做法：handoff 跨轮次持续",
        new="        current_agent = frontline_agent",
        expect_group="B handoff",
        why="丢掉了官方的 `current_agent = result.last_agent` ⇒ handoff 不能跨轮次持续",
    ),
    Mutation(
        mid="M2",
        filename="specialists.py",
        old='    context.context.channel = "literary"',
        new="    context.context.channel = context.context.channel",
        expect_group="B handoff",
        why="on_handoff 不再写共享上下文 ⇒ 对照官方 airline 的 flight_number 失效",
    ),
    Mutation(
        mid="M3",
        filename="frontline.py",
        old=(
            "literary_agent.handoffs.append(\n"
            '    handoff(agent=frontline_agent, tool_name_override="transfer_to_frontline_agent")\n'
            ")"
        ),
        new="pass  # 回程边被摘掉",
        expect_group="B handoff",
        why="撤掉回程 handoff ⇒ 专家 instructions 里「可以转回前台」变成做不到的承诺",
    ),
    Mutation(
        mid="M4",
        filename="runner.py",
        old='        if feedback.score == "pass":',
        new="        if True:",
        expect_group="D 评估循环",
        why="把评审结论当摆设 ⇒ 打回不再触发回灌（评审白跑）",
    ),
    Mutation(
        mid="M5",
        filename="guards.py",
        old="        tripwire_triggered=bool(dangling),",
        new="        tripwire_triggered=False,",
        expect_group="E 护栏",
        why="出口护栏永不触发 ⇒ 编造引用被放行",
    ),
    Mutation(
        mid="M6",
        filename="frontline.py",
        old="        literary_handoff(),",
        new='        literary_agent.as_tool("write_literature", "文学创作"),',
        expect_group="A 拓扑",
        why="把会接管对话的专家塞进 tools ⇒ 它同时出现在两处，产出形态与接线不匹配",
    ),
)


def _read(path: Path) -> str:
    # newline="" ⇒ 不做行尾翻译，读写对称（纪律 3）
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _write(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_selftest() -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", SELFTEST],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _preflight(targets: dict[str, str]) -> None:
    """纪律 1：每个锚点必须唯一匹配，否则整体退出。"""
    problems: list[str] = []
    for mutation in MUTATIONS:
        text = targets[mutation.filename]
        hits = text.count(mutation.old)
        if hits != 1:
            problems.append(
                f"  {mutation.mid}: 在 {mutation.filename} 里匹配到 {hits} 处（应为 1）"
            )
    if problems:
        raise SystemExit("✗ 预检失败，未做任何改动：\n" + "\n".join(problems))


def main() -> int:
    files = sorted({m.filename for m in MUTATIONS})
    paths = {name: HERE / name for name in files}

    originals = {name: _read(path) for name, path in paths.items()}
    baseline_sha = {name: _sha(path) for name, path in paths.items()}

    _preflight(originals)
    print(f"预检通过：{len(MUTATIONS)} 个锚点各自唯一匹配")

    code, output = _run_selftest()
    if code != 0:
        print("✗ 基线 selftest 未通过，先修好再来做变异")
        print(output[-2000:])
        return 1
    print("基线 selftest 通过\n")

    caught = 0
    try:
        for mutation in MUTATIONS:
            path = paths[mutation.filename]
            mutated = originals[mutation.filename].replace(mutation.old, mutation.new, 1)
            _write(path, mutated)
            try:
                code, output = _run_selftest()
            finally:
                _write(path, originals[mutation.filename])

            if _sha(path) != baseline_sha[mutation.filename]:
                print(f"✗ {mutation.mid}: 还原后哈希不一致，立即中止")
                return 1

            failed_groups = [
                line for line in output.splitlines() if line.startswith(("[FAIL]", "[ERROR]"))
            ]
            hit_expected = any(
                line.startswith("[FAIL]") and mutation.expect_group in line
                for line in failed_groups
            )
            if code != 0 and hit_expected:
                caught += 1
                print(f"[抓住] {mutation.mid} → {mutation.expect_group} 变红｜{mutation.why}")
            elif code == 0:
                print(f"[漏网] {mutation.mid}：selftest 全绿 ⇒ 这条断言是假绿｜{mutation.why}")
            else:
                print(
                    f"[错位] {mutation.mid}：红了但不是预期那组"
                    f"（预期 {mutation.expect_group}，实际 {failed_groups}）｜{mutation.why}"
                )
    finally:
        for name, path in paths.items():
            _write(path, originals[name])

    # 纪律 2：正常路径也再自证一次（finally 不防 SIGTERM）
    drift = [name for name, path in paths.items() if _sha(path) != baseline_sha[name]]
    if drift:
        print(f"✗ 源码未还原：{drift}")
        return 1

    print(f"\n变异验证：{caught}/{len(MUTATIONS)} 被抓住；受影响文件全部逐字节还原")
    return 0 if caught == len(MUTATIONS) else 1


if __name__ == "__main__":
    sys.exit(main())
