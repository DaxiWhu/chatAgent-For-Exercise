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

凭据脱敏
--------
本模块还负责**把所有日志记录里的凭据抹掉**（`redact()` / `install_redaction()`），
由 `setup_logging()` 自动装上。原因与做法见下面的长注释 —— 一句话：
DSN 里的口令会随 psycopg 异常被 `logging.error("...%s", e)` 带进日志，
而调用点逐个覆盖不可能不漏。**脱敏是兜底网，不是"可以随便记"的许可证。**
"""

import contextvars
import json
import logging
import os
import re
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


# ---------------------------------------------------------------------------
# 凭据脱敏（兜底网）
# ---------------------------------------------------------------------------
# 为什么要在**日志层**做，而不是在各调用点做
# -----------------------------------------
# 调用点脱敏是"记得就做、忘了就漏"，而本项目里存在一处**必然**泄漏的路径：
#
#     DSN 里带口令（`postgresql://user:pass@host/db`），而 psycopg / psycopg_pool
#     的异常文本会把它带上；这些异常被 `logging.error("...：%s", e)` 记下来，
#     口令就进了日志。
#
# 异常是透传的，"谁在哪一层记下来"事先不知道；第三方库（uvicorn/httpx/openai）
# 自己打的日志更管不到。所以做法是**换掉日志记录工厂**
# （`logging.setLogRecordFactory`）—— 那是唯一一个"所有记录都必然经过"的点。
#
# ⚠️ 为什么不给 handler 加 Filter：Logger 层级上的 filter 对"从子 logger 冒泡上来"
#    的记录**不生效**（`Logger.callHandlers` 只调 handler 自己的 filter），
#    而 uvicorn / psycopg / openai 各有一套 logger 与 handler，逐个挂必漏。
#
# ⚠️ 它**不**能替代"根本不要记录敏感值"：脱敏是兜底网，不是许可证。
#
# 每条模式都写成"锚点明确、量词有界"，因为脱敏跑在**每条日志**上。
_REDACT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 带凭据的连接串。只脱口令那一节，保留 scheme/user/host ——
    # 排障时要能认出"是哪个库连不上"。
    # ⚠️ user 段写成 `*` 而不是 `+`：Redis 的常见写法是 `redis://:pass@host`
    # （用户名留空），要求至少一个字符就会**整条漏掉**。
    (re.compile(r"(?i)\b(postgres(?:ql)?|redis|rediss|amqp|amqps|mongodb(?:\+srv)?)"
                r"://([^:@/\s]*):([^@/\s]+)@"),
     r"\1://\2:***@"),
    # `Authorization: Bearer <token>`（JWT 是长串 base64url，含 `.` 分隔）
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/=]{8,}"),
     r"\1 ***"),
    # `key=value` 形态的凭据。key 必须是明确的凭据名，避免误伤普通业务字段
    # （比如日志里的 `secret_count=3` 这种）。
    (re.compile(r"(?i)\b(access[_-]?token|refresh[_-]?token|api[_-]?key|apikey"
                r"|auth[_-]?token|password|passwd|client[_-]?secret|secret)"
                r"(\"?\s*[=:]\s*\"?)([^\s\"',;&]{4,})"),
     r"\1\2***"),
    # OpenAI 风格密钥。⚠️ 长度下限 16：短值（测试里的 `sk-test`）不该被抹掉，
    # 否则守卫测试与排障都会被无谓地打断。
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
     "sk-***"),
    # 私钥块：只要出现头，就别再往外写内容。
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,4096}?"
                r"-----END [A-Z ]*PRIVATE KEY-----"),
     "-----BEGIN PRIVATE KEY-----***-----END PRIVATE KEY-----"),
]

_FORMATTER = logging.Formatter()
_REDACTION_INSTALLED = False


def redact(text: str) -> str:
    """把文本里已知形态的凭据替换成 `***`。

    纯函数、幂等（`postgresql://u:***@h` 再脱敏一次不变）—— 这样"工厂已脱敏过、
    格式器又脱敏一次"不会互相打架。

    text: 任意待输出文本。
    """
    if not text:
        return text
    out = text
    for pattern, repl in _REDACT_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def _wrap_record(record: logging.LogRecord) -> None:
    """把单条记录上**所有会被输出的文本**都换成脱敏版本。

    覆盖四类出口：`getMessage()`（msg + args）、`extra=` 带的字符串字段、
    异常堆栈（`exc_text`）、`stack_info`。少任何一类都是一个"看起来已脱敏"的假象。
    """
    original_get_message = record.getMessage

    def _redacted_message() -> str:
        return redact(original_get_message())

    record.getMessage = _redacted_message        # type: ignore[method-assign]

    # extra= 传进来的自定义字段：JsonFormatter 会把它们原样拼进 JSON，
    # 所以 `extra={"authorization": "Bearer ..."}` 若不处理就直接落盘了。
    for key, value in list(record.__dict__.items()):
        if key in _STD_ATTRS or key.startswith("_"):
            continue
        if isinstance(value, str):
            record.__dict__[key] = redact(value)

    if record.exc_info:
        try:
            record.exc_text = redact(_FORMATTER.formatException(record.exc_info))
        except Exception:                        # noqa: BLE001  脱敏失败不该影响日志
            pass

    if record.stack_info:
        record.stack_info = redact(record.stack_info)


def install_redaction() -> None:
    """装上凭据脱敏（幂等）。由 `setup_logging()` 调用，不必单独调。"""
    global _REDACTION_INSTALLED
    if _REDACTION_INSTALLED:
        return
    _REDACTION_INSTALLED = True
    base_factory = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = base_factory(*args, **kwargs)
        _wrap_record(record)
        return record

    logging.setLogRecordFactory(factory)


def redaction_installed() -> bool:
    """本进程是否已装上脱敏（供守卫测试断言"启动路径真的装了它"）。"""
    return _REDACTION_INSTALLED



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
            # 显式再脱敏一次：`install_redaction()` 已经在记录工厂里脱过，
            # 但那张网是"进程级"的 —— 单独 import 本模块构造格式器（如测试）时
            # 它可能没装。宁愿多跑一次（幂等、微秒级）也不要留一个假象。
            "msg": redact(record.getMessage()),
        }
        rid = get_request_id()
        if rid:
            payload["request_id"] = rid
        # 调用方自定义字段（extra=...）也带上，但不覆盖保留字段
        for k, v in record.__dict__.items():
            if k not in _STD_ATTRS and k not in payload and not k.startswith("_"):
                try:
                    json.dumps(v)          # 只收可序列化的值，避免 json.dumps 整行失败
                    payload[k] = redact(v) if isinstance(v, str) else v
                except (TypeError, ValueError):
                    payload[k] = redact(str(v))
        if record.exc_info:
            payload["exc"] = _exception_text(record, self)
        return json.dumps(payload, ensure_ascii=False)


def _exception_text(record: logging.LogRecord, formatter: logging.Formatter) -> str:
    """取异常文本并脱敏。

    优先用记录上已有那份（`_wrap_record` 预格式化并脱敏过的），避免重复格式化；
    没有就现场格式化再脱敏。
    """
    if record.exc_text:
        return record.exc_text
    return redact(formatter.formatException(record.exc_info))


class TextFormatter(logging.Formatter):
    """人眼友好的单行格式（本地用）。"""

    def format(self, record: logging.LogRecord) -> str:
        """`时间 LEVEL logger [rid] 消息`，异常另起多行。"""
        rid = get_request_id()
        head = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<5} {record.name}"
        if rid:
            head += f" [{rid}]"
        line = f"{head} {redact(record.getMessage())}"
        if record.exc_info:
            line += "\n" + _exception_text(record, self)
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

    # 脱敏与格式设置绑在一起：**只要配了日志，就一定有脱敏**。
    # 单独放一个"记得调用"的启动步骤，迟早会漏 —— 而漏掉的后果是口令进日志，
    # 且没有任何症状。
    install_redaction()

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
