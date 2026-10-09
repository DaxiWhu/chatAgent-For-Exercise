"""变异验证：证明本样例的守卫**真的会红**。

    python -m examples.task_routing.mutate

方法论（本项目硬要求，来自 2026-09-29 那次教训）：
  1. **先预检**：每个锚点必须在目标文件里**唯一**匹配，任一不中就整体退出
     —— 不预检的话，源码一漂移，脚本会静默地"什么都没变"，然后报"全部通过"。
  2. 逐组串行、每组带超时。
  3. **后自证还原**：结束后比对源码 sha256，必须与开始时一致
     —— 脚本要改源码，一旦被中断，变异就留在工作区（实测发生过：一次被杀的
     变异把 `pop` 改成 `get` 留在源码里，而脚本自己毫不知情）。

⚠️ M3 这一组是**它抓出了作者自己写的假绿**：最初第 5 条守卫断言
"`state["messages"][0]` 还是原文"，而 messages 是 **append-reducer** ——
路由器改写 prompt 时是**追加**，第一条永远是原文 ⇒ 变异后照样绿。
改成断言"模型**实际收到**的输入"之后才真正生效。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))          # examples/task_routing
ROOT = os.path.dirname(os.path.dirname(HERE))               # 项目根
PKG = HERE
PY = sys.executable

# (编号, 文件, 锚点, 替换, 期望变红的断言)
MUTATIONS = [
    ("M1", "plan.py",
     'DEFAULT_FALLBACK = "rag"',
     'DEFAULT_FALLBACK = "direct"',
     "3. fail-safe 方向"),
    ("M2", "nodes.py",
     '    if task == "direct":\n        return "generate"',
     '    if task == "direct":\n        return "retrieve"',
     "1. direct 不发检索"),
    ("M3", "nodes.py",
     '    return {"route": plan["task"], "route_plan": plan, "route_source": plan["source"]}',
     '    return {"route": plan["task"], "route_plan": plan, "route_source": plan["source"],\n'
     '            "messages": [HumanMessage(content=query.strip())]}',
     "5. 路由器只读不写"),
    ("M4", "nodes.py",
     '    if not ex.get("executed"):\n        return "finish"          '
     '# 不可恢复：环境问题，退出而不是空转\n',
     '',
     "8. 不可恢复失败不重试"),
    ("M5", "nodes.py",
     '    if it >= _budget(state):\n        return "budget_exhausted"',
     '    if it >= _budget(state):\n        return "converged"',
     "7. 预算耗尽 ≠ 达标"),
]


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def main():
    targets = sorted({m[1] for m in MUTATIONS})
    paths = {t: os.path.join(PKG, t) for t in targets}

    # --- 预检：锚点唯一 + 记录原始哈希 ---
    originals, before = {}, {}
    for t, p in paths.items():
        originals[t] = open(p, encoding="utf-8").read()
        before[t] = sha(p)
    for name, fname, anchor, _, _ in MUTATIONS:
        n = originals[fname].count(anchor)
        if n != 1:
            print(f"预检失败：{name} 的锚点在 {fname} 里匹配 {n} 次（必须恰好 1 次），整体退出")
            return 2
    print(f"预检通过：{len(MUTATIONS)} 组锚点全部唯一匹配\n")

    results = []
    try:
        for name, fname, anchor, repl, guard in MUTATIONS:
            p = paths[fname]
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(originals[fname].replace(anchor, repl, 1))
            try:
                r = subprocess.run([PY, "-m", "examples.task_routing.selftest"],
                                   cwd=ROOT, capture_output=True, text=True, timeout=180)
                rc, out = r.returncode, (r.stdout or "") + (r.stderr or "")
            except subprocess.TimeoutExpired:
                rc, out = -1, "<timeout>"
            finally:
                with open(p, "w", encoding="utf-8", newline="\n") as f:
                    f.write(originals[fname])
            failed = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("[FAIL]")]
            ok = rc != 0
            results.append((name, guard, ok))
            print(f"{'变红 OK ' if ok else '未变红 !!!'} {name} 期望守：{guard}  (exit={rc})")
            for x in failed[:2]:
                print(f"        {x[:110]}")
    finally:
        bad = [t for t, p in paths.items() if sha(p) != before[t]]
        if bad:
            print(f"\n还原失败：{bad} 与原始字节不一致！")
            return 3
        print("\n还原自证：全部文件 sha256 与开始一致")

    wrong = [r[0] for r in results if not r[2]]
    if wrong:
        print(f"\n{len(wrong)} 组未按预期变红：{wrong}")
        return 1
    print(f"\n变异验证通过：{len(MUTATIONS)}/{len(MUTATIONS)} 全部按预期变红且字节还原")
    return 0


if __name__ == "__main__":
    sys.exit(main())
