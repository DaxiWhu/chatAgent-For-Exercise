"""结构化日志：JSON 输出 + 贯穿一次请求的 request_id。

解决什么问题
-----------
LangGraph 一次问答会跨 `retrieve → generate → evaluate → 可能多次重试`，
日志里现在只有时间戳和消息，**无法回答"线上这次答错的请求，日志里哪几条属于它"**。
做法是给每个 HTTP 请求分配一个 `request_id`（或沿用上游传来的 `X-Request-ID`），
存进 `contextvars` —— 于是同一请求内、任意模块、任意线程（threadpool 里继承上下文）
打的日志都会自动带上这个 id。

为什么用 contextvars 而不是全局变量
---------------------------------
contextvars 是 asyncio 原生的：每个请求任务有独立的副本，天然隔离、无需锁，
也不会像全局变量那样在并发下互相覆盖。

形态（LOG_FORMAT）
-----------------
- json（默认，生产）：一行一个 JSON 对象，字段固定 ts/level/logger/msg[/request_id]，
  异常额外带 exc；日志系统（ELK/Loki）能直接按字段检索与聚合。
- text：`asctime LEVEL logger [rid] msg`，本地开发看着顺眼。
"""

import contextvars
import json
import logging
import os
import sys
import time

# 当前请求 id。默认空串表示"不在请求上下文里"（启动期日志、CLI 等）
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

# logging.LogRecord 自带的属性名 —— 拼 JSON 时要排除，剩下的才是调用方传的 extra
_STD_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
    "message",
}


def set_request_id(rid: str) -> contextvars.Token:
    """把 request_id 写进当前上下文，返回可用于 reset 的 token。

    rid: 请求 id 字符串（空串等价于清除）。
    """
    return _request_id.set(rid or "")


def get_request_id() -> str:
    """读当前上下文的 request_id（不在请求里时返回空串）。"""
    return _request_id.get()


def reset_request_id(token) -> None:
    """还原到 set 之前的状态（必须与 set_request_id 的 token 配套用，防串号）。

    token: set_request_id 的返回值。
    """
    try:
        _request_id.reset(token)
    except (ValueError, LookupError):
        # 上下文已被销毁（极端情况）时忽略：日志少个 rid 比抛异常好
        pass


class JsonFormatter(logging.Formatter):
    """把 LogRecord 渲染成单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        """返回一行 JSON。

        record: 标准 LogRecord。除标准字段外，调用方通过 `extra=` 传的自定义字段
            会被原样收进输出（便于 `logging.info("x", extra={"stage": "retrieve"})`）。
        """
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
                  + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = get_request_id()
        if rid:
            payload["request_id"] = rid
        # 调用方自定义字段（extra=...）也带上，但不覆盖保留字段
        for k, v in record.__dict__.items():
            if k not in _STD_ATTRS and k not in payload and not k.startswith("_"):
                try:
                    json.dumps(v)          # 只收可序列化的值，避免 json.dumps 整行失败
                    payload[k] = v
                except (TypeError, ValueError):
                    payload[k] = str(v)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """人眼友好的单行格式（本地用）。"""

    def format(self, record: logging.LogRecord) -> str:
        """`时间 LEVEL logger [rid] 消息`，异常另起多行。"""
        rid = get_request_id()
        head = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<5} {record.name}"
        if rid:
            head += f" [{rid}]"
        line = f"{head} {record.getMessage()}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def build_formatter(fmt: str) -> logging.Formatter:
    """按 `json` / `text` 造格式化器（未知值回退 json）。

    fmt: LOG_FORMAT 的值。
    """
    return TextFormatter() if (fmt or "").lower() == "text" else JsonFormatter()


def setup_logging(level=None, fmt=None):
    """配置根 logger。**幂等且不夺走别人已装的 handler**。

    level: 日志级别，默认取 LOG_LEVEL（INFO）。
    fmt: 日志格式 json|text，默认取 LOG_FORMAT。

    关键取舍：只在"根 logger 还没有 handler"时才添加自己的 handler。
    pytest 的 caplog、uvicorn 的默认配置都会先装 handler，若我们强行覆盖，
    caplog 会失效、uvicorn 自己的访问日志也会消失 —— 宁可少配也不要抢。
    """
    lvl = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    formatter = build_formatter(fmt or os.getenv("LOG_FORMAT", "json"))

    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(formatter)
        root.addHandler(h)
    else:
        # 已有 handler：仅把它们的格式换成我们的（便于统一 JSON），保留 handler 本身
        for h in root.handlers:
            if getattr(h, "_wb_managed", False):
                h.setFormatter(formatter)

    try:
        root.setLevel(getattr(logging, lvl))
    except AttributeError:
        root.setLevel(logging.INFO)

    # 第三方库降噪（httpx 每次请求都打一行，生产日志会被淹）
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return root
