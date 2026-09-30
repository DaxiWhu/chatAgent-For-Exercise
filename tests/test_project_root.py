"""项目根目录的推导：`src/rag/*` 的模块必须指向**项目根**，不是 `src/`。

背景（2026-09-28 目录重构引入、2026-09-29 发现）
--------------------------------------------
RAG 链路从 `src/*.py` 迁进 `src/rag/*.py` 时，5 个模块里的这行忘了多上溯一层：

    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

对 `src/rag/x.py` 来说它只算到 `<项目>/src`（上溯 2 层），而正确值是项目根
（上溯 3 层）。于是两个后果同时发生，且**都没有任何报错**：

  ① `load_dotenv(dotenv_path=<项目>/src/.env)` 指向一个不存在的文件 ⇒ 这 5 个模块
     口中的"自己加载 .env，不让配置是否生效取决于 import 顺序"**全部是空操作**。
     之所以平时看起来正常，是因为 `app/main.py` 会调一次无参 `load_dotenv()`（它按
     调用方文件位置向上找，能找到项目根的 `.env`）——也就是说：配置能否生效取决于
     "有没有别的模块先 import 了 app"，正是这些注释想消灭的那种不确定性。
     `src/rag/docling_loader.py` 的注释里记录过一次因它导致的 502 事故，而它自己
     的"修法"同样是坏的。
  ② `_ROOT` 还被当作**项目根**用来解析相对路径：`data/.media`、`data/.vlm_cache`、
     `data/.ctx_cache` 会被落到 `src/data/...` 下。落到镜像里未挂载的目录 = 媒体文件
     写在容器可写层（重启即丢）或直接因只读挂载而失败。

本文件只做一件事：把这个语义钉住（"是项目根"而不是"是 src/"）。用例会**失败**而
非跳过，因为一旦算错，行为差异是静默的。
"""
import importlib
import os
import re
from pathlib import Path

import pytest

import tests.conftest as CONF

#: 依赖 `_ROOT` 的 5 个模块（都同时用它加载 .env 与解析相对路径）
_RAG_MODULES = ("retrieval", "docling_loader", "docx_media",
                "image_describer", "contextualizer")


@pytest.mark.parametrize("modname", _RAG_MODULES)
def test_rag_module_root_is_the_project_root(modname):
    """`_ROOT` 必须等于项目根 —— 用"项目根才有的目录"判定，而不是比较字符串前缀。

    判定用 `app/` 与 `src/` 同时存在：这两个目录在任何一次 checkout 里都在，
    所以本用例在 CI（没有 `.env`）下同样成立。
    """
    mod = importlib.import_module(f"src.rag.{modname}")
    root = os.path.abspath(mod._ROOT)
    assert root == os.path.abspath(CONF.ROOT), \
        f"src/rag/{modname}.py 的 _ROOT 算到了 {root}，应为项目根 {CONF.ROOT}"
    assert os.path.isdir(os.path.join(root, "app")), \
        f"src/rag/{modname}.py 的 _ROOT 下没有 app/ —— 它不是项目根"
    assert os.path.isdir(os.path.join(root, "src")), \
        f"src/rag/{modname}.py 的 _ROOT 下没有 src/ —— 它不是项目根"


def test_no_rag_module_derives_the_root_from_too_few_levels():
    """结构守卫：`src/rag/*.py` 里从 `__file__` 推导根目录时，必须上溯 ≥3 层。

    写法上禁"深度"而不是禁字符串，是因为 3 层写法**包含** 2 层写法作为子串
    （`dirname(dirname(dirname(x)))` 里就藏着 `dirname(dirname(x))`），
    直接查子串会把正确的写法也判成违规。

    为什么要做这条：行为侧只覆盖"当前存在且被 import"的模块；新加一个
    `src/rag/xxx.py` 并复制粘贴旧写法时，只有本用例能在**它还没被引用**的时候
    就抓住。把任意一个模块改回 2 层（或新增一个复制来的模块）时本条变红。

    作用域刻意限定在 `src/rag/`：同样这行写在 `src/graph.py` 里是**正确**的
    （`src/` 直系文件上溯 2 层就是项目根），所以不能全仓库一刀切。
    """
    offenders = []
    for path in sorted((Path(CONF.ROOT) / "src" / "rag").glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "os.path.abspath(__file__)" not in line or "os.path.dirname(" not in line:
                continue          # 与"由 __file__ 推导根目录"无关的行（如只打日志）不管
            depth = line.count("os.path.dirname(")
            if depth < 3:
                offenders.append(f"{path.name}:{lineno}（只上溯 {depth} 层）")
    assert not offenders, (
        f"这些地方把项目根算成了 src/：{offenders}；正确写法要上溯 3 层 —— "
        "os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))"
    )


@pytest.mark.parametrize("modname,attr,default", [
    ("docx_media", "MEDIA_DIR", "data/.media"),
    ("image_describer", "VLM_CACHE_DIR", "data/.vlm_cache"),
    ("contextualizer", "CTX_CACHE_DIR", "data/.ctx_cache"),
])
def test_relative_default_dirs_land_under_the_project_root(modname, attr, default):
    """相对默认目录必须解析到 `<项目根>/data/...`（而不是 `<项目根>/src/data/...`）。

    用 `RAG_*_DIR` 显式配过（含绝对路径）时跳过：那时用户的意图不属于"默认值语义"，
    本条只钉默认值。
    """
    mod = importlib.import_module(f"src.rag.{modname}")
    value = getattr(mod, attr)
    if value != default:
        pytest.skip(f"{attr} 已被显式配置为 {value!r}，本条只钉未配置时的默认值")
    resolved = os.path.normpath(os.path.join(mod._ROOT, value))
    expected = os.path.normpath(os.path.join(CONF.ROOT, default))
    assert resolved == expected, f"{attr} 解析成了 {resolved}，应为 {expected}"
    # 双保险：路径中间不能多出一段 src/
    assert not re.search(r"[\\/]src[\\/]data[\\/]", resolved + os.sep), \
        f"{attr} 被解析到了 src/data 下：{resolved}"
