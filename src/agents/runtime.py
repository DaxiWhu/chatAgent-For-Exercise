"""agent 链路的运行时装配点（**注入 seam**）。

为什么需要这一层
----------------
`app/main.py` 要在**模块导入期**就能判断"agent 链路能不能用"，而
`src/agents/` 里的模块（tools / specialists / runner）全部在导入期就
``from agents import ...`` —— 只要 `openai-agents` 没装，导入就炸。

所以本模块**不允许在模块级 import ``agents``**：它只是一组开关与一个模型槽位。
真正的 SDK 导入发生在 ``configure()`` 里（进程启动、只调一次）。

⚠️ 没有"降级"这回事
-----------------
图退役之后本服务**只有这一条**作答路径，所以 ``configure()`` 返回 False 意味着
"这个进程答不了任何请求"。调用方（`app/main.py` 的 `_assert_agents_ready`）
据此**拒绝启动**，不再有任何"回落到另一条链路"的分支。

``configure()`` 本身仍然**不抛异常**（它把失败原因写进 ``_reason`` 再返回 False）：
判断要不要退出是调用方的事，装配层只负责把原因说清楚 —— ``missing_sdk`` 与
``missing_api_key`` 的处置方式不同（前者是镜像问题、后者是配置问题），
让调用方拿到原因才能给出可执行的报错。
"""

from __future__ import annotations

import logging
from typing import Any

__all__ = [
    "configure",
    "get_model",
    "is_ready",
    "ready_reason",
    "reset",
    "set_model",
]

_ready: bool = False
_reason: str = "not_configured"
_model: Any | None = None


def configure(
    *,
    model_name: str,
    api_key: str,
    base_url: str,
    timeout: float = 30.0,
) -> bool:
    """按既有配置装配官方 Model 并置为就绪。

    model_name / api_key / base_url: 与主链路**同一份**配置（``app/config.py`` 的
        ``MODEL_NAME`` / ``API_KEY`` / ``API_BASE``）。刻意不引入第二套模型配置 ——
        "同一个服务里两个模型变量"是运维事故的常见来源。

    timeout: 单次模型调用超时（秒）。

    返回是否就绪。**不抛异常**（原因写进 ``ready_reason()``）：
    判"要不要拒绝启动"是调用方的事，见模块 docstring。
    """
    global _ready, _reason, _model
    if not api_key:
        _ready, _reason = False, "missing_api_key"
        return False
    try:
        from .model import build_model
    except ImportError as e:  # openai-agents 未安装
        _ready, _reason = False, f"missing_sdk: {e}"
        return False
    try:
        _model = build_model(
            model_name=model_name, api_key=api_key, base_url=base_url, timeout=timeout
        )
    except Exception as e:  # noqa: BLE001  装配失败不该拦住启动
        _ready, _reason = False, f"build_failed: {e}"
        logging.warning("agent 链路装配失败，回退原问答链路：%s", e)
        return False
    _ready, _reason = True, "ok"
    return True


def set_model(model: Any) -> None:
    """直接注入一个 Model（**测试用**：可以不联网、不建 client）。"""
    global _model, _ready, _reason
    _model = model
    _ready, _reason = model is not None, "injected" if model is not None else "not_configured"


def get_model() -> Any | None:
    """取当前模型（None = 未就绪）。"""
    return _model


def is_ready() -> bool:
    """agent 链路是否可用（诊断用：启动期由 `app.main` 硬校验，运行期不再分叉）。"""
    return _ready and _model is not None


def ready_reason() -> str:
    """未就绪的原因（进启动报错，便于区分"没装 SDK"与"没配 Key"）。"""
    return _reason


def reset() -> None:
    """清空状态（**仅供测试**）。"""
    global _ready, _reason, _model
    _ready, _reason, _model = False, "not_configured", None
