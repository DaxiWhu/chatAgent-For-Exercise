"""环境变量的读取兜底：**配错不该让服务起不来**。

为什么需要它
-----------
本项目多条链路在**模块导入期**读环境变量（Docling 在 import 时定切分口径、
agent 在 import 时定轮次上限、检索层在 import 时定超时）。凡是写成裸
`int(os.getenv(...))` / `float(os.getenv(...))` 的地方，一个手滑的取值
（`AGENT_MAX_TURNS=很多`、`RAG_TENANT_CACHE_MAX=8个`、`RERANK_TIMEOUT=20s`）
就会在**导入期**抛 `ValueError` ⇒ **进程根本起不来**，而报错栈指向的是 import
那一行 —— 与"你的 .env 里写错了一个字"之间隔着一层，排障时最容易误判成
"代码坏了"。项目其它地方（`app/config.py` 的 `_env_int/_env_float`）一直是
兜底口径，`src/` 侧这一批是漏的。

口径（与 `app/config.py` 保持一致）
--------------------------------
- 未设置、空串、非数字 ⇒ **回落到默认值**，不抛异常；
- 非法值留一条 WARNING 日志。这一行不是装饰：`RERANK_TIMEOUT` 静默变成 20、
  `AGENT_MAX_TURNS` 静默变成 12，都是"行为变了但没人知道"的那种问题，
  而排障时第一件要确认的事就是"线上到底读成了什么值"。

为什么是一对函数而不是一份配置对象
--------------------------------
`src/` 侧**刻意**直读 `os.getenv`（项目约定：走 `app/config.py` 会形成
`src → app` 的反向依赖，而 `app/config.py` 又是全项目配置入口）。
所以这里提供可复用的**读取函数**，而不是一个配置快照 —— 调用点仍然显式写变量名，
`tests/test_doc_drift.py` 的静态扫描（"代码读了哪些键"）因此照常工作。
"""

import logging
import os


def _clamp(value: int, lo, hi, name: str):
    """把值夹到 [lo, hi]（None 表示该侧不设限），被夹时留日志。"""
    out = value
    if lo is not None and out < lo:
        out = lo
    if hi is not None and out > hi:
        out = hi
    if out != value:
        logging.warning("环境变量 %s=%s 超出允许范围，已夹到 %s", name, value, out)
    return out


def env_int(name: str, default: int, *, lo=None, hi=None) -> int:
    """读整数环境变量；非法值回落到 `default`。

    name: 变量名（字面量，便于静态扫描）。
    default: 未设置 / 空串 / 非整数时的回退值。
    lo / hi: 可选的上下界（**含**）。用于"0 或负数会让下游除零/死循环"这类配置，
        例如 `RERANK_TIMEOUT`。

    ⚠️ 之所以把上下界做成**这里的职责**而不是让调用点各写 `max(1, ...)`：
    漏写一次就是"合法的 0"流进下游，而 `max()` 不会告诉你它夹过什么值。
    """
    raw = (os.getenv(name, "") or "").strip()
    value = default
    if raw:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            logging.warning("环境变量 %s=%r 不是整数，回落到默认值 %s",
                            name, raw, default)
            value = default
    return _clamp(int(value), lo, hi, name)


def env_float(name: str, default: float, *, lo=None, hi=None) -> float:
    """读浮点环境变量；非法值回落到 `default`（与 `env_int` 同一口径）。"""
    raw = (os.getenv(name, "") or "").strip()
    value = default
    if raw:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logging.warning("环境变量 %s=%r 不是数字，回落到默认值 %s",
                            name, raw, default)
            value = default
    return _clamp(float(value), lo, hi, name)
