"""测试专用身份：铸造一张**真签**的本服务会话 token，供 TestClient 默认带上。

为什么需要它
-----------
2026-10-05 起服务只有**一条**鉴权路径 —— 账号密码 / 外部 IdP → JWT。
匿名兜底与共享密钥档（`X-API-Key`）都已从代码里删除，于是"不带凭证"的业务请求
**一律 401**。测试要调 `/v1/*`，就必须持有一个真实签发的身份。

为什么是"真签"，而不是 monkeypatch `_authenticate`
------------------------------------------------
  · monkeypatch 会让**所有**用例都绕过鉴权层，于是"鉴权本身"永远不被执行 ——
    而它恰恰是这次改造里最需要被真实覆盖的一段；
  · 真签的 token 会走完"验签 → 查会话白名单"两道检查，与线上行为一致；
  · 顺带让"每个用例都在真实的鉴权上下文里跑"成为事实，而不是靠自觉。

身份取值为什么是 `tenant=""`
--------------------------
刻意与历史 `anonymous` 档的租户取值一致：这样"本次改造只换了鉴权入口、没有改动
业务口径（配额分桶 / 会话归属 / 审核归属都还是原来的取值）"这一点在回归里保持可比。
需要特定租户的用例自己传 `tenant=`。
"""

from __future__ import annotations

#: 测试用户名。与任何真实账号无关。
TEST_USER = "tester"
#: 测试用户所属租户。空串 = "没有租户"，与历史 anonymous 档一致（见模块文档）。
TEST_TENANT = ""
#: 测试用户的角色：给足两个，避免审核/管理端点用例因为"缺角色"而 403。
#: "缺角色 ⇒ 403"那一条由用例用显式 monkeypatch 覆盖（见 test_config_env_policy）。
TEST_ROLES = ("admin", "reviewer")


def auth_headers(user: str = TEST_USER, tenant: str = TEST_TENANT, roles=TEST_ROLES) -> dict:
    """铸造一张 access token，返回可直接喂给 httpx 的 `Authorization` 头。

    ⚠️ 每次调用都会**新开一个会话**（写入会话白名单）。模块级的
    `client = TestClient(M.app, headers=auth_headers())` 因此是每个测试文件一个会话，
    总条数很少，且内存后端会自行清扫过期项。
    """
    import app.main as M
    from app import auth as userauth

    principal = userauth.Principal(user=user, tenant=tenant, roles=roles)
    access, _refresh, _sid = M._AUTH.login_principal(principal)
    return {"Authorization": f"Bearer {access}"}
