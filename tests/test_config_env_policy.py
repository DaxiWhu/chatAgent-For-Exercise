"""运行环境判据与授权默认值：两条"默认值必须在没配错的方向上偏保守"的守卫。

为什么单独一个文件
------------------
这两处改动（2026-10-05）都属于同一类风险：**配错时不报错，只是行为悄悄变了**。

  1. `APP_ENV` 判据从"白名单认生产"改为"**黑名单认开发**"。原判据
     `APP_ENV in ("production", "prod")` 下，`prd` / `live` / `staging` 这些
     看起来很像生产的写法都不在白名单里 ⇒ 被当成开发环境 ⇒ 一次性翻转默认值
     （打开 `/docs`、启动期不拦"生产却无鉴权"），而**全程没有任何报错**。
     （当时还包含"放行未鉴权请求" —— 那个开关已随匿名兜底一起删除，
     现在无论什么环境，"没有凭证 ⇒ 401"都是结构性的。）
  2. `AUTH_REVIEWER_ROLES` 默认值从"空 = 不限制"改为"`reviewer`"。
     原默认是一个**开着**的权限默认值，且没有任何提示 —— 忘了配就等于把审核权
     发给每一个登录用户，包括刚注册、还没有租户的账号。

两处都靠"改默认值"修，而**默认值最容易被后人无意改回去**（它只是一个字面量）。
所以这里钉的不是那个字面量本身，而是**行为**：改回去必须变红。


⚠️ 一条刻意保留的不一致：「未设置」与「显式写空」含义不同
------------------------------------------------------
  · **未设置** `APP_ENV` ⇒ 取 `_env_str` 的默认值 `development` ⇒ **非生产**；
  · **显式写空** `APP_ENV=` ⇒ 空串 ⇒ **生产**（空串不在非生产闭集里）。

这个不一致是 fail-closed 的**刻意结果**：一个"认不出来的值"应当落到安全侧，
而不是被默默当作开发。代价是 `.env` 里若留了一行空的 `APP_ENV=`，服务会走到
生产口径并因缺用户鉴权而**拒绝启动** —— 那是显式失败，不是静默降级。
`test_explicitly_empty_app_env_is_treated_as_production` 把它钉住。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.config as C
from app.main import Identity, _assert_review_role

# ---------------------------------------------------------------------------
# ① 生产判据：认不出来的取值一律落到生产
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["development", "dev", "local", "test", "testing"])
def test_explicit_development_values_are_not_production(value):
    """只有明确写着"我在本地/测试"的取值才算非生产。"""
    assert C._is_production(value) is False


@pytest.mark.parametrize(
    "value",
    [
        "production",
        "prod",  # 原本就该认的两个
        "Production",
        "PRODUCTION",  # 大小写不敏感
        "production ",
        " prod ",  # 首尾空格照旧被 strip
        "prd",
        "live",
        "staging",  # ⚠️ 本次改动的**核心**：这三个以前会被当成开发环境
        "mylaptop",  # 任何自定义值
        "",  # 显式写空（见文件头那条刻意的不一致）
    ],
)
def test_unknown_app_env_values_fall_back_to_production(value):
    """⚠️ 核心守卫：认不出来的取值一律落到生产。

    把判据改回 `APP_ENV in ("production", "prod")`，上面 `prd` / `live` /
    `staging` / `mylaptop` / `""` 这几条会立刻变红 —— 这正是这条测试存在的意义。
    """
    assert C._is_production(value) is True


def test_explicitly_empty_app_env_is_treated_as_production():
    """显式写空 = 生产，与"未设置"刻意不同（理由见文件头）。"""
    assert C._is_production("") is True
    # 而未设置时 `_env_str` 会给出 default=development ⇒ 非生产：
    assert C._env_str("APP_ENV", "development") == "development"
    assert C._is_production("development") is False


def test_test_process_defaults_to_development():
    """测试进程里 `APP_ENV` 被隔离层清掉了 ⇒ 落到默认 `development`。

    这条同时钉住两件事：① 默认值仍是 `development`（没有哪次改动把它挪到生产）；
    ② 隔离层确实清掉了这个键 —— 否则开发机上 `export APP_ENV=production`
    会让整个套件在"生产口径"下运行，而多数用例察觉不到。
    """
    assert C.APP_ENV == "development"
    assert C.IS_PRODUCTION is False
    # 派生值跟着走，一并钉住 —— 它们才是判据的实际后果。
    # ⚠️ 这里**不再**断言 `REQUIRE_AUTH`：该配置项随"匿名兜底"一起删除，
    #    它对鉴权已经没有任何影响。现在"非生产"仍然可见的后果只剩文档开关。
    assert C.ENABLE_DOCS is True


# ---------------------------------------------------------------------------
# ② 授权默认值：fail-closed
# ---------------------------------------------------------------------------


def test_reviewer_roles_default_is_fail_closed():
    """⚠️ 核心守卫：不配 `AUTH_REVIEWER_ROLES` 时默认要求 `reviewer`。

    把默认值改回 `""`，`_assert_review_role` 开头那句
    `if not config.AUTH_REVIEWER_ROLES: return` 就会让**任何登录用户**都能审 ——
    这条断言会红。与之配对的是下面那条：显式写空仍必须是合法出口。
    """
    assert C.AUTH_REVIEWER_ROLES == ("reviewer",)


def test_admin_roles_default_is_fail_closed():
    """管理角色的默认值与审核角色**同为** fail-closed（两者此前刻意相反）。"""
    assert C.AUTH_ADMIN_ROLES == ("admin",)


def test_reviewer_roles_default_actually_gates_a_roleless_user():
    """反向证明默认值真的参与判定：没有 `reviewer` 角色的用户被拒 403。

    这条钉住"默认值不只是存在于 config 里，而是真的被用来判权限"。
    """
    with pytest.raises(HTTPException) as ei:
        _assert_review_role(
            Identity(tenant="acme", user="u", roles=("viewer",))
        )
    assert ei.value.status_code == 403


def test_explicitly_empty_reviewer_roles_still_disables_the_check(monkeypatch):
    """显式写空仍然是"不做角色校验"的出口 —— 别把出口一起堵了。

    与上面成对：默认 fail-closed，但**放开的开关必须还在**。否则运维想回到
    "任何登录用户都能审"就只能改代码 —— 改动面更大，且会在下次升级时被覆盖掉。
    """
    monkeypatch.setattr(C, "AUTH_REVIEWER_ROLES", ())
    # 一个没有任何角色的用户身份，在"显式写空"下不该被拒
    _assert_review_role(Identity(tenant="acme", user="u", roles=()))


# ---------------------------------------------------------------------------
# ③ 重载 config 的正确姿势（2026-10-05 事故的守卫）
# ---------------------------------------------------------------------------
def test_config_reload_keeps_the_test_identity():
    """重载 `app.config` 之后，**测试身份必须还在**（会话密钥不能被冲成空串）。

    这条守卫来自一次真实事故：`test_agent_history_budget.py` 里一条用例为了验
    "同一个环境变量被两处独立解析"，直接 `importlib.reload(app.config)`。重载时
    环境已被 conftest 擦净 ⇒ 密钥变成空串 ⇒ 其它文件在**收集期**签好的 token
    全部验签失败，全量多红 60 条，而单跑那些文件条条通过。

    正确的姿势是 `tests.conftest.reload_app_config()`。这里把它钉住，并顺带钉住
    "用完不许把身份留在环境里"—— conftest 的既定不变量是**测试进程等价于裸环境**
    （由 `test_env_isolation.py` 守着）。
    """
    import os

    from tests.conftest import _TEST_AUTH_ENV, reload_app_config

    expected = _TEST_AUTH_ENV["AUTH_SESSION_SECRET"]
    before = C.AUTH_SESSION_SECRET
    assert before == expected, "调用前测试身份就已丢失"
    after = reload_app_config().AUTH_SESSION_SECRET
    assert after == expected, (
        "重载后密钥变了 —— 之前签发的 token 会全部验签失败"
    )
    assert "AUTH_SESSION_SECRET" not in os.environ, (
        "重载后把测试身份留在了环境里 —— 违背『测试进程等价于裸环境』"
    )
