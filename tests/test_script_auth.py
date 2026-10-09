"""`scripts/script_auth.py` 的守卫：脚本凭证改走"登录换令牌"之后的行为契约。

为什么要有这份
--------------
脚本的凭证获取是**最容易悄悄退回旧路径**的地方：只要有一处写回
`os.getenv("SERVE_API_KEY")`，那条"分不出身份、租户自报、撤不掉"的共享密钥路径
就会继续被使用，而**没有任何功能会报错**（它本来就能跑通）。

所以这里钉三件事：
  1. 缺凭证时**必须失败并给出可照做的指引**（不许静默退回共享密钥）；
  2. 登录打的是 `/v1/auth/login`、发的是 `username`/`password`、取的是 `access_token`；
  3. `auth_headers()` 产出的请求头里**只有 Authorization**，不带那些 `_` 前缀的元信息
     （它们若被当请求头发出去，服务端只会当成无关的自定义头，问题很难看出来）。
"""

from __future__ import annotations

import json

import pytest

from scripts import script_auth


class _Resp:
    """最小的 urlopen 返回值替身（够 read() + 上下文管理器用）。"""

    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    """默认把环境变量清干净 —— 否则本机配了 AGENT_SCRIPT_* 就会让用例结果随环境漂。"""
    monkeypatch.delenv(script_auth.USER_ENV, raising=False)
    monkeypatch.delenv(script_auth.PASSWORD_ENV, raising=False)


def test_missing_credentials_fail_with_actionable_guidance():
    """缺凭证时**明确失败**，不静默退回共享密钥。

    静默退回的后果是："脚本已改走登录"这件事在本地看起来成立、
    在真正需要它的环境里其实从没生效 —— 而两边都不报错。
    """
    assert not script_auth.has_credentials()
    with pytest.raises(SystemExit) as ei:
        script_auth.require_credentials()
    msg = str(ei.value)
    assert script_auth.USER_ENV in msg and script_auth.PASSWORD_ENV in msg
    assert "hash_password" in msg, "报错必须给出'怎么建一个服务账号'的可照做指引"


def test_credentials_come_from_environment_then_arguments(monkeypatch):
    """参数优先于环境变量（让测试能用 `E2E_*` 这类自己的变量名）。"""
    monkeypatch.setenv(script_auth.USER_ENV, "from-env")
    monkeypatch.setenv(script_auth.PASSWORD_ENV, "pw-from-env")
    assert script_auth.credentials() == ("from-env", "pw-from-env")
    assert script_auth.credentials("from-arg", "pw-from-arg") == ("from-arg", "pw-from-arg")


def test_login_posts_credentials_to_the_login_endpoint(monkeypatch):
    """登录只打 `/v1/auth/login`，发 `username`/`password`，取 `access_token`。"""
    seen: dict = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp({"access_token": "tok-1", "user": "svc-loadtest", "tenant": "acme"})

    monkeypatch.setattr(script_auth.urllib.request, "urlopen", fake_urlopen)
    token = script_auth.access_token("http://svc:8000/", "svc-loadtest", "pw-12345678")
    assert token == "tok-1"
    assert seen["url"] == "http://svc:8000/v1/auth/login", "结尾的 / 没被剥掉会造成 //"
    assert seen["method"] == "POST"
    assert seen["body"] == {"username": "svc-loadtest", "password": "pw-12345678"}


def test_login_response_without_a_token_is_an_error_not_an_empty_string(monkeypatch):
    """响应里没有令牌 ⇒ 明确报错。返回空串会让调用方"看起来没配凭证"而走别的分支。"""
    monkeypatch.setattr(script_auth.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp({"user": "svc-loadtest"}))
    with pytest.raises(SystemExit) as ei:
        script_auth.access_token("http://svc:8000", "svc-loadtest", "pw-12345678")
    assert "access_token" in str(ei.value)


def test_login_503_explains_that_user_auth_is_not_enabled(monkeypatch):
    """"服务端没启用用户鉴权"要直接说出来 —— 否则现象是"连不上/登录失败"来回猜。"""
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, None)

    monkeypatch.setattr(script_auth.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(SystemExit) as ei:
        script_auth.login("http://svc:8000", "svc-loadtest", "pw-12345678")
    assert "AUTH_SESSION_SECRET" in str(ei.value)


def test_auth_headers_carry_only_the_authorization_header(monkeypatch):
    """`auth_headers()` 的元信息键以 `_` 开头，必须能被 `only_headers()` 剥干净。"""
    monkeypatch.setenv(script_auth.USER_ENV, "svc-loadtest")
    monkeypatch.setenv(script_auth.PASSWORD_ENV, "pw-12345678")
    monkeypatch.setattr(script_auth.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp({"access_token": "tok-1",
                                                         "user": "svc-loadtest",
                                                         "tenant": "acme"}))
    headers = script_auth.auth_headers("http://svc:8000")
    assert headers["Authorization"] == "Bearer tok-1"
    assert headers["_tenant"] == "acme"
    assert script_auth.only_headers(headers) == {"Authorization": "Bearer tok-1"}
