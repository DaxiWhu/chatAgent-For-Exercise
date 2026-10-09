"""环境变量读取点的**统一采集**：两份守卫共用一份正则。

为什么必须共用一份
----------------
"代码里读了哪些环境变量"这个事实有两个消费者，而它们各自维护过一份正则：

* `tests/conftest.py::behavior_env_names()` —— 决定测试进程要**清理**哪些变量
  （清理是为了让"开发机 `.env`"影响不到测试结论）；
* `tests/test_doc_drift.py::_code_keys()` —— 决定文档里提到的键算不算"真实存在"。

两份正则漂移过一次，后果是**静默**的（2026-10-06）：
`src/envutil.py` 引入了不带下划线前缀的 `env_int("X")` 读法，一边补了新写法、
另一边没补 ⇒ `RAG_RRF_K` 从清理清单里悄然消失 —— 它既不再被清理，也不报任何错，
只会在某台 `.env` 里恰好设了它的机器上让用例红（"本地红、CI 绿"那一类事故）。

采集面的**范围**仍由各调用方自己决定（conftest 只扫配置与 RAG 链路，
文档守卫要扫全仓），这里统一的只是"什么写法算一次读取"这件事。
"""

from __future__ import annotations

import re

#: 认得**两**类写法：
#:   * `os.getenv("X")` / `os.environ.get("X")` / `os.environ["X"]`
#:     —— `app/` 与 `src/` 的直读写法；
#:   * `env_int("X")` / `_env_str("X")` / …
#:     —— 配置层（`app/config.py`）与 `src/envutil.py` 的兜底读法。
#:
#: ⚠️ `_env_int(` 与 `env_int(` 必须**都**能匹配：后者是 `src/envutil.py`
#: 引入的无下划线前缀写法，而 `env_int(` 这个子串**并不包含** `_env_int(`，
#: 少写一条就会让一批真实存在的键从采集结果里消失（症状见模块 docstring）。
#:
#: 只认**字面量**：变量名拼接的读法（`os.getenv(f"{prefix}_URL")`）静态确认不了，
#: 这是刻意的能力边界，不是遗漏。
_ENV_READ = re.compile(
    r'(?:os\.getenv|os\.environ\.get|env_(?:str|bool|int|float))\(\s*"([A-Za-z0-9_]+)"'
    r'|os\.environ\[\s*"([A-Za-z0-9_]+)"\s*\]'
)


def env_names_in(text: str) -> set[str]:
    """从一段源码文本里取出所有**字面量**环境变量名。"""
    return {next(g for g in m.groups() if g) for m in _ENV_READ.finditer(text)}


def env_names_in_paths(paths) -> set[str]:
    """逐个文件采集并合并（不存在的路径直接跳过）。"""
    names: set[str] = set()
    for path in paths:
        try:
            names |= env_names_in(path.read_text(encoding="utf-8"))
        except OSError:      # 文件不存在 / 读不到：跳过，由调用方自己决定是否该报错
            continue
    return names
