"""凭据脱敏与 `/metrics` 收口的守卫。

这两件事的共同点：**出错时没有任何症状**。
  - 脱敏失效 ⇒ 什么都不报错，只是日志里静静躺着口令；等发现时日志系统里
    已经存了几百 MB，而且早就被转发到别处了，无法回收。
  - `/metrics` 收口失效 ⇒ 指标照常输出，抓取方照常抓到；只是"本不该看到它的
    人也看到了"。没有任何日志会提示这件事。

所以两条都必须靠"把泄漏形态写成用例"来钉，而不是靠人记得。
"""

from __future__ import annotations

import json
import logging

import pytest

from app import logging_setup as ls

# ---------------------------------------------------------------------------
# 一、redact() 纯函数：哪些形态必须被抹掉、哪些必须**不被**误伤
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, secret, keep", [
    # 带凭据的 DSN —— 本项目最真实的泄漏路径（psycopg 异常会带上整条 conninfo）
    ("could not connect to postgresql://langgraph_app:hunter2@db:5432/kb",
     "hunter2", "langgraph_app"),
    ("redis://:topsecret@cache:6379/0 连接失败", "topsecret", "cache:6379"),
    # Authorization 头
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhIn0.abcdEFGH",
     "eyJhbGciOiJIUzI1NiJ9", "Bearer"),
    # key=value 形态
    ("api_key=abcd1234efgh 无效", "abcd1234efgh", "api_key"),
    ('{"password": "p@ssw0rd-long"}', "p@ssw0rd-long", "password"),
    ("client_secret=zZzZzZzZzZz", "zZzZzZzZzZz", "client_secret"),
    # OpenAI 风格密钥（长度 >= 16 才脱）
    ("sk-abcdefghijklmnopqrstuvwxyz012345", "sk-abcdefghij", "sk-***"),
])
def test_known_credential_shapes_are_redacted(raw, secret, keep):
    out = ls.redact(raw)
    assert secret not in out, f"没抹掉凭据：{out!r}"
    assert keep in out, f"把不该抹的也抹了（排障需要它）：{out!r}"


def test_short_values_and_normal_text_are_left_alone():
    """过度脱敏会毁掉排障：短值、普通业务文本必须原样保留。"""
    safe = [
        "sk-test",                      # 测试用短 key
        "模型返回 401 unauthorized",     # 一个"秘密"字都没有
        "secret_count=3",               # 不是凭据赋值
        "password_hash=abc",            # 同上（`_hash` 断了匹配）
        "耗时 120ms，命中 3 个 chunk",
        "token_type=Bearer",            # `token` 不在凭据名单里，不该被抹
    ]
    for text in safe:
        assert ls.redact(text) == text, f"误伤了正常日志：{text!r} → {ls.redact(text)!r}"


def test_redact_is_idempotent():
    """工厂脱一次、格式器又脱一次 —— 两次结果必须一致（否则输出会飘）。"""
    raw = "postgresql://u:pw@h/db api_key=abcd1234 Bearer eyJhbGciOiJIUzI1NiJ9.abc"
    once = ls.redact(raw)
    assert ls.redact(once) == once


def test_private_key_block_is_removed():
    raw = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----")
    out = ls.redact(raw)
    assert "MIIEowIBAAKCAQEA" not in out
    assert "BEGIN" in out and "END" in out, "整段删掉会让排障看不出「这里曾有私钥」"


# ---------------------------------------------------------------------------
# 二、记录级：必须覆盖所有输出出口
# ---------------------------------------------------------------------------

def _capture(caplog, fn):
    with caplog.at_level(logging.INFO):
        fn()
    return "\n".join(r.getMessage() for r in caplog.records)


def _capture_json(logger_name: str, fn) -> str:
    """用 JsonFormatter 真正渲染一遍，拿**最终要写出去的文本**来断言。

    ⚠️ 断言必须落在"渲染结果"上：`record.extra` 字段既不进 `getMessage()`，
    也不进 `caplog.text` 之外的任何地方 —— 只断言 message 的用例会对 extra 泄漏
    **完全无感**（假绿），而那正是最容易漏的一类出口。
    """
    import io

    logger = logging.getLogger(logger_name)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ls.JsonFormatter())
    old_handlers, old_propagate, old_level = logger.handlers, logger.propagate, logger.level
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        fn(logger)
    finally:
        logger.handlers, logger.propagate, logger.level = old_handlers, old_propagate, old_level
    return stream.getvalue()


def test_message_and_args_are_redacted_via_the_record_factory(caplog):
    """最常见的形态：`logging.error("...：%s", e)` —— 异常文本带 DSN 口令。"""
    ls.install_redaction()

    def _log():
        err = RuntimeError("connection failed: postgresql://u:pw@h/db")
        logging.getLogger("t.redact").error("落库失败：%s", err)

    captured = _capture(caplog, _log)
    assert ":pw@" not in captured, f"参数里的口令没被抹掉：{captured!r}"
    assert "postgresql://u:***@h/db" in captured, (
        f"抹掉的位置不对（排障需要 host/db 还在）：{captured!r}")


def test_extra_fields_are_redacted():
    """`extra=` 的字符串字段会被 JsonFormatter 原样拼进 JSON，必须也脱。"""
    ls.install_redaction()

    out = _capture_json("t.redact.extra", lambda log: log.info(
        "鉴权失败", extra={"authorization_header": "Bearer eyJhbGciOiJIUzI1NiJ9.payload"}))

    assert "eyJhbGciOiJIUzI1NiJ9" not in out, f"extra 字段里的令牌泄漏了：{out!r}"
    assert "authorization_header" in out, "字段本身要留着 —— 否则看不出这里曾有凭据"


def test_exception_traceback_is_redacted():
    """堆栈是"最容易漏"的出口 —— 它不经过 `getMessage()`。"""
    ls.install_redaction()

    def _log(logger):
        try:
            raise RuntimeError("bad dsn postgresql://root:leaked-pw@pg:5432/x")
        except RuntimeError:
            logger.exception("装配失败")

    out = _capture_json("t.redact.exc", _log)
    assert "leaked-pw" not in out, f"堆栈里的口令泄漏了：{out!r}"


def test_json_and_text_formatters_redact_even_without_the_global_factory():
    """格式器自身也必须脱敏：单独构造格式器的场景（测试、CLI）没有那层全局网。"""
    for formatter in (ls.JsonFormatter(), ls.TextFormatter()):
        record = logging.LogRecord(
            name="t", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="连不上 postgresql://u:hunter2@h/db", args=(), exc_info=None)
        out = formatter.format(record)
        assert "hunter2" not in out, f"{type(formatter).__name__} 没有脱敏"


def test_json_formatter_extra_field_is_redacted():
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="x", args=(), exc_info=None)
    record.api_credential = "Bearer eyJhbGciOiJIUzI1NiJ9.payload"
    payload = json.loads(ls.JsonFormatter().format(record))
    assert "eyJhbGciOiJIUzI1NiJ9" not in json.dumps(payload)


def test_setup_logging_installs_redaction(monkeypatch):
    """反向锁：脱敏必须由**日志配置**自动装上。

    单独留一个"记得调用 install_redaction()"的启动步骤，迟早会漏 ——
    漏掉的后果是口令进日志，且没有任何症状。

    ⚠️ 必须**先把"已安装"状态复原**再断言：本文件里前面的用例都显式调用过
    `install_redaction()`，模块级标志早已是 True。不复原的话，即便把
    `setup_logging()` 里那行删掉，这条依然会绿 —— **假绿**。
    （这不是假设：变异验证正是这么抓出来的。）
    """
    import io

    monkeypatch.setattr(ls, "_REDACTION_INSTALLED", False)
    logging.setLogRecordFactory(logging.LogRecord)      # 还原成默认工厂

    ls.setup_logging()

    assert ls.redaction_installed() is True, "setup_logging 没有安装脱敏"

    # 不只看标志 —— 真打一条带口令的日志，看它落到输出里是什么样。
    stream = io.StringIO()
    logger = logging.getLogger("t.redact.setup")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ls.JsonFormatter())
    old = (logger.handlers, logger.propagate)
    logger.handlers, logger.propagate = [handler], False
    try:
        logger.warning("连不上 postgresql://u:hunter2@h/db")
    finally:
        logger.handlers, logger.propagate = old
    assert "hunter2" not in stream.getvalue(), (
        f"setup_logging 之后口令仍然落盘：{stream.getvalue()!r}")


# ---------------------------------------------------------------------------
# 三、/metrics 应用侧收口
# ---------------------------------------------------------------------------

class _FakeRequest:
    def __init__(self, host):
        self.client = None if host is None else type("C", (), {"host": host})()


@pytest.mark.parametrize("host, allowed", [
    ("10.1.2.3", True),          # 私网
    ("172.18.0.5", True),        # docker 网段
    ("192.168.1.9", True),
    ("127.0.0.1", True),         # 回环
    ("::1", True),
    ("169.254.1.1", True),       # link-local
    (None, True),                # 无对端信息（unix socket / 进程内）
    ("testclient", True),        # 非 IP 文本（TestClient、回环主机名）
    ("8.8.8.8", False),          # 公网 ⇒ 拒绝
    ("1.1.1.1", False),
    ("2001:4860:4860::8888", False),
])
def test_metrics_peer_allowlist(monkeypatch, host, allowed):
    from app import main as M

    monkeypatch.setattr(M.config, "METRICS_PRIVATE_ONLY", True)
    assert M._metrics_peer_allowed(_FakeRequest(host)) is allowed


def test_metrics_peer_check_can_be_disabled(monkeypatch):
    from app import main as M

    monkeypatch.setattr(M.config, "METRICS_PRIVATE_ONLY", False)
    assert M._metrics_peer_allowed(_FakeRequest("8.8.8.8")) is True


def test_metrics_denial_is_indistinguishable_from_disabled(monkeypatch):
    """被拒时回的 404 与文案必须与"未启用"**完全一致**。

    区分两者等于告诉扫描者"这个端点存在、只是我 IP 不在白名单" ——
    那就是一条有效情报，把"收口"变成了"指路"。
    """
    from fastapi.testclient import TestClient

    from app import main as M

    monkeypatch.setattr(M.config, "METRICS_ENABLED", True)
    monkeypatch.setattr(M, "_metrics_peer_allowed", lambda request: False)
    denied = TestClient(M.app).get("/metrics")

    monkeypatch.setattr(M.config, "METRICS_ENABLED", False)
    disabled = TestClient(M.app).get("/metrics")

    assert denied.status_code == 404
    assert denied.status_code == disabled.status_code
    assert denied.json() == disabled.json()


def test_metrics_allowed_for_private_peer_returns_body(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as M

    monkeypatch.setattr(M.config, "METRICS_ENABLED", True)
    monkeypatch.setattr(M, "_metrics_peer_allowed", lambda request: True)
    r = TestClient(M.app).get("/metrics")
    assert r.status_code == 200
    assert "http_requests_total" in r.text or "# HELP" in r.text


def test_metrics_denial_is_counted(monkeypatch):
    """被拒必须有计数 —— 否则"有人在反复试探 /metrics"完全不可见。"""
    from fastapi.testclient import TestClient

    from app import main as M
    from app import metrics as metrics_mod

    monkeypatch.setattr(M.config, "METRICS_ENABLED", True)
    monkeypatch.setattr(M, "_metrics_peer_allowed", lambda request: False)
    before = metrics_mod.value("metrics_denied_total")
    TestClient(M.app).get("/metrics")
    assert metrics_mod.value("metrics_denied_total") == before + 1
