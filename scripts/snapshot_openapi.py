"""OpenAPI 契约快照的生成与校验。

解决什么问题
------------
接口是**对外承诺**，而改动它的代价不在服务端代码里，在调用方：删一个字段、
把参数从 query 挪到 body、改一个枚举取值、把响应包一层 —— 服务端都能编译通过，
多数测试照样绿（只要没有用例恰好断言到那个字段），而客户端在线上直接炸。
这类事故的特点是"我们的测试全绿，但对方挂了"。

本脚本把"当前接口长什么样"固化成 `tests/snapshots/openapi.json`，
`tests/test_openapi_contract.py` 每次跑都重新生成一份并逐字节比对 ——
**任何契约变化都会以 diff 的形式出现在 CI 里**，而不是等到对接方报错。

用法
----
    python scripts/snapshot_openapi.py            # 校验（CI 门禁）
    python scripts/snapshot_openapi.py --write    # 确认变更后重新生成

⚠️ `--write` 之前**先看 diff 到底改了什么**。快照这类门禁最大的失败模式是
"测红了就顺手 --write" —— 那等于把门禁悄悄删掉，而且看起来还很像"修好了"。
因此断言不止一份：`tests/test_openapi_contract.py` 里另有一份**手写**的期望路由
清单，专门用来防止"重新生成一遍把新增的对外端点也一起洗白"。

为什么归一化逻辑放在这里而不是测试里
------------------------------------
"生成快照"与"校验快照"必须是**同一份实现**，否则两者会用不同口径比对 ——
那种不一致的表现是"校验永远失败，而 --write 永远修不好"。测试直接 import 本模块。
"""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "tests" / "snapshots" / "openapi.json"

# 直接 `python scripts/snapshot_openapi.py` 时 sys.path[0] 是 scripts/，
# 仓库根不在路径里 ⇒ `import app.main` 会失败。这里显式补上（test 里已被 conftest 补过）。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 每次发版都会变、但与"契约"无关的字段。留着它只会让快照在每次改版本号时飘。
#: 注意：**只排除这些**。像 `description` 这种虽然也常改，但它是对外可见的文档，
#: 改了就应该出现在 diff 里被人看一眼。
VOLATILE_PATHS: tuple[tuple[str, ...], ...] = (
    ("info", "version"),
)

_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


def load_schema(app=None) -> dict:
    """取 FastAPI 应用的 OpenAPI 结构。

    app: 可注入替身（测试用）。默认导入 `app.main` 里的真实应用 ——
    **必须是真实应用**，否则快照锁的不是"线上跑的那份契约"。
    """
    if app is None:
        import app.main as M  # 延迟导入：避免只跑 --help 也拉起整个服务

        app = M.app
    return app.openapi()


def normalize(schema: dict) -> dict:
    """剔除与契约无关的易变字段。返回**新对象**，不改入参。"""
    out = copy.deepcopy(schema)
    for path in VOLATILE_PATHS:
        node = out
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict):
            node.pop(path[-1], None)
    return out


def dumps(schema: dict) -> str:
    """归一化 + 稳定序列化。

    `sort_keys=True` 是刻意的：字典顺序不该影响"契约有没有变"的判断 ——
    否则重构一下路由注册顺序就会产生一份毫无意义的 diff。
    列表（`required`、`parameters` 等）的顺序**保留**，因为那里顺序是有意义的。
    """
    return json.dumps(normalize(schema), ensure_ascii=False,
                      indent=2, sort_keys=True) + "\n"


def routes_of(schema: dict) -> set[str]:
    """`{"POST /v1/chat", ...}` —— 用于给人看的变更摘要。"""
    out: set[str] = set()
    for path, item in (schema.get("paths") or {}).items():
        for method in item:
            if method in _METHODS:
                out.add(f"{method.upper()} {path}")
    return out


def _model_names(schema: dict) -> set[str]:
    return set((schema.get("components") or {}).get("schemas") or {})


def describe_change(old: dict, new: dict) -> str:
    """把变更讲成"人关心的那几件事"，而不是一堆 JSON 行。

    先给"路由/模型"层面的增删（一眼能看出有没有新增对外端点），
    再让逐行 diff 去说明字段级的变化。
    """
    lines: list[str] = []
    for label, a, b in (("路由", routes_of(old), routes_of(new)),
                        ("模型", _model_names(old), _model_names(new))):
        for x in sorted(b - a):
            lines.append(f"  + {label}新增：{x}")
        for x in sorted(a - b):
            lines.append(f"  - {label}删除：{x}")
    if not lines:
        lines.append("  路由与模型集合没变 ⇒ 变化在字段/参数/响应结构里（见下方 diff）")
    return "\n".join(lines)


def unified_diff(old: str, new: str, limit: int = 200) -> str:
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                     "snapshot", "current", lineterm="", n=2))
    if len(diff) > limit:
        diff = diff[:limit] + [f"… 还有 {len(diff) - limit} 行，"
                               "请在本地跑 --write 后用 git diff 查看"]
    return "\n".join(diff)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OpenAPI 契约快照：生成 / 校验")
    ap.add_argument("--write", action="store_true",
                    help="确认契约变更后重新生成快照")
    ap.add_argument("--quiet", action="store_true",
                    help="只输出结论（测试里用，避免刷屏）")
    args = ap.parse_args(argv)

    text = dumps(load_schema())
    rel = SNAPSHOT.relative_to(ROOT)

    if args.write:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        old = SNAPSHOT.read_text(encoding="utf-8") if SNAPSHOT.exists() else ""
        SNAPSHOT.write_text(text, encoding="utf-8")
        if not args.quiet:
            print(f"已写入 {rel}")
            if old and old != text:
                print(unified_diff(old, text))
        return 0

    if not SNAPSHOT.exists():
        print(f"[FAIL] 快照不存在：{rel}\n"
              f"       先跑：python scripts/snapshot_openapi.py --write",
              file=sys.stderr)
        return 1

    old = SNAPSHOT.read_text(encoding="utf-8")
    if old == text:
        if not args.quiet:
            print(f"[OK] OpenAPI 契约与快照一致（{rel}）")
        return 0

    print("[FAIL] OpenAPI 契约已变化：", file=sys.stderr)
    print(describe_change(json.loads(old), json.loads(text)), file=sys.stderr)
    if not args.quiet:
        print(unified_diff(old, text), file=sys.stderr)
    print("\n若这次变化是**有意**的：python scripts/snapshot_openapi.py --write\n"
          "（改完记得对照文档确认，客户端需要同步升级）\n"
          "若这次变化是**无意**的 —— 那正是这个门禁要拦的事，请去改代码。",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
