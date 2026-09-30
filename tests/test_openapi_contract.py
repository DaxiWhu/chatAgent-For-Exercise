"""OpenAPI 契约快照测试。

为什么需要
----------
接口是**对外承诺**，而破坏承诺的代价不在服务端：删一个字段、把参数从 query
挪到 body、改一个枚举取值、给响应结果包一层 —— 服务端都能编译通过，
只要没有用例恰好断言到那个字段，测试就是全绿的，而客户端在线上直接炸。
这类事故的典型特征是"我们这边一切正常，但对方挂了"。

快照把"当前契约长什么样"固定下来，任何变化都会以 diff 形式出现在 CI 里。

快照最大的失败模式：测红了就顺手 `--write`
----------------------------------------
那等于把门禁悄悄删掉，而且看起来特别像"修好了"。所以本文除了快照本身，
还有三份**手写**的断言（路由清单、鉴权头、旧路径的 deprecated 标记）——
重新生成快照**不会**让它们通过。它们逼着人明确回答"这个改动是有意的吗"。

生成逻辑的唯一来源是 `scripts/snapshot_openapi.py`
------------------------------------------------
"生成"与"校验"共用同一份归一化实现，否则两者口径不同 ——
症状是"校验永远失败，而 --write 永远修不好"。
"""

from __future__ import annotations

import json

import pytest

from scripts.snapshot_openapi import (
    SNAPSHOT, describe_change, dumps, load_schema, normalize, routes_of,
    unified_diff,
)

#: **手写**的对外路由清单。加端点必须改这里 —— 这就是它存在的意义。
EXPECTED_ROUTES = {
    # v1：对外正式契约
    "POST /v1/chat",
    "POST /v1/review",
    "GET /v1/reviews",
    "GET /v1/sessions/{thread_id}",
    # 履行删除请求（合规要求）。它是 #87「数据保留与会话删除」加进来的对外端点，
    # 当初漏了同步这份手写清单 —— 于是快照与代码都被 --write 抹平了，只有这里红着。
    "DELETE /v1/sessions/{thread_id}",
    "POST /v1/chat/resume",
    # 旧路径：保留兼容，但已标记 deprecated（见下面的用例）
    "POST /chat",
    "POST /review",
    # 运维面
    "GET /health",
    "GET /health/live",
    "GET /health/ready",
    "GET /metrics",
}

#: 需要鉴权头的路由前缀。
_AUTHED_PREFIXES = ("/v1/", "/chat", "/review")


@pytest.fixture(scope="module")
def snapshot_text() -> str:
    assert SNAPSHOT.exists(), (
        f"缺少契约快照 {SNAPSHOT.relative_to(SNAPSHOT.parents[2])}；"
        "先跑 python scripts/snapshot_openapi.py --write"
    )
    return SNAPSHOT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def snapshot(snapshot_text) -> dict:
    return json.loads(snapshot_text)


# ---------------------------------------------------------------------------
# 核心：契约与快照一致
# ---------------------------------------------------------------------------
def test_openapi_matches_the_committed_snapshot(snapshot_text):
    """实时生成的契约必须与提交的快照逐字节一致。

    失败时的处理方式只有两种，没有第三种：
      1. 这个变化是**有意**的 ⇒ 确认影响面后 `--write` 重新生成；
      2. 这个变化是**无意**的 ⇒ 去改代码。那正是这个门禁要拦的事。
    """
    current = dumps(load_schema())
    if current == snapshot_text:
        return

    old = json.loads(snapshot_text)
    new = json.loads(current)
    pytest.fail(
        "OpenAPI 契约与快照不一致：\n"
        f"{describe_change(old, new)}\n\n"
        f"{unified_diff(snapshot_text, current)}\n\n"
        "确认是有意变更后跑：python scripts/snapshot_openapi.py --write\n"
        "（并同步更新文档与客户端）"
    )


def test_snapshot_routes_match_the_handwritten_contract(snapshot):
    """快照里的路由必须与**手写清单**完全一致。

    这是防"顺手 --write 洗白"的关键：重新生成快照能让上面那条用例通过，
    但改不动这里的 EXPECTED_ROUTES —— 新增/删除对外端点必须有人显式确认。
    """
    actual = routes_of(snapshot)
    assert actual == EXPECTED_ROUTES, (
        "对外路由与手写清单不符：\n"
        f"  快照里多出的：{sorted(actual - EXPECTED_ROUTES)}\n"
        f"  快照里缺失的：{sorted(EXPECTED_ROUTES - actual)}\n"
        "确实要增删对外端点，请同步改本文件的 EXPECTED_ROUTES 并说明原因。"
    )


def test_snapshot_is_not_degenerate(snapshot):
    """快照不能是空壳。

    没有这条，"快照文件被清空成 {}" 也能让一致性用例通过（两边一样空）。
    """
    assert (snapshot.get("paths") or {}), "快照里没有任何路由"
    assert (snapshot.get("components", {}).get("schemas") or {}), "快照里没有任何模型"
    assert snapshot.get("openapi"), "快照缺少 openapi 版本号"


# ---------------------------------------------------------------------------
# 手写的契约细节（无法被 --write 洗白）
# ---------------------------------------------------------------------------
def test_legacy_routes_are_marked_deprecated(snapshot):
    """旧路径必须标记 deprecated —— 这是给客户端看的迁移信号。

    "我们文档里写了"不算数：调用方大多只看 OpenAPI（以及由它生成的 SDK），
    标记不上去，旧路径就会一直被新代码使用，永远删不掉。
    """
    paths = snapshot["paths"]
    for route in ("/chat", "/review"):
        assert paths[route]["post"].get("deprecated") is True, (
            f"{route} 未标记 deprecated —— 客户端不会收到迁移信号"
        )
    for route in ("/v1/chat", "/v1/review"):
        assert not paths[route]["post"].get("deprecated"), (
            f"{route} 是当前的正式契约，不该被标记 deprecated，"
            "否则新接入方会以为它要废弃"
        )


def test_v1_and_legacy_routes_accept_the_auth_headers(snapshot):
    """需要鉴权的路由必须在 OpenAPI 里声明 `X-API-Key` / `X-Tenant-Id`。

    鉴权是自定义头（不是 Bearer），调用方只能从 OpenAPI 得知它的存在与名字。
    漏声明的后果是接入方按文档写完必然 401，然后来问"为什么"。
    """
    for path, item in snapshot["paths"].items():
        if not path.startswith(_AUTHED_PREFIXES):
            continue
        for method, op in item.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            names = {p["name"] for p in (op.get("parameters") or [])}
            assert "x-api-key" in names, (
                f"{method.upper()} {path} 没有声明 X-API-Key 头"
            )
            assert "x-tenant-id" in names, (
                f"{method.upper()} {path} 没有声明 X-Tenant-Id 头"
            )


def test_request_bodies_are_named_components(snapshot):
    """每个请求体都必须 `$ref` 到一个**有名字**的模型。

    内联匿名 schema 也能用，但它没有名字 ⇒ 客户端生成的类型是无名结构，
    且"改了字段"在 diff 里不如"改某个有名模型"易读。
    """
    schemas = snapshot["components"]["schemas"]
    checked = 0
    for path, item in snapshot["paths"].items():
        for method, op in item.items():
            body = (op.get("requestBody") or {}).get("content") or {}
            if not body:
                continue
            checked += 1
            ref = list(body.values())[0].get("schema", {}).get("$ref")
            assert ref, f"{method.upper()} {path} 的请求体不是 $ref 到命名模型"
            assert ref.rsplit("/", 1)[-1] in schemas, (
                f"{method.upper()} {path} 的请求体引用了不存在的模型：{ref}"
            )
    assert checked >= 3, f"只检查到 {checked} 个请求体，是不是路由结构变了？"


def test_schema_has_no_frozen_config_limits(snapshot):
    """模型里不应出现 `maxLength` / `minimum` 这类"从配置冻进来的"约束。

    本项目刻意把字段闸门放在**调用时**读配置（422 时现算），而不是
    `Field(max_length=...)`。冻进模型的后果有两个，都很隐蔽：
      1. 测试没法 monkeypatch 这些限制（它们已经是类属性了）；
      2. 限制值变成了对外契约的一部分 —— 调大 `MAX_MESSAGE_CHARS` 会**静默**
         改变 OpenAPI，客户端 SDK 的校验规则随之失配。
    """
    frozen: list[str] = []
    for name, schema in snapshot["components"]["schemas"].items():
        if name in ("HTTPValidationError", "ValidationError"):  # FastAPI 自带
            continue
        for field, spec in (schema.get("properties") or {}).items():
            for key in ("maxLength", "minLength", "maximum", "minimum"):
                if key in spec:
                    frozen.append(f"{name}.{field}.{key}={spec[key]}")
    assert not frozen, (
        f"以下约束被冻进了 OpenAPI 模型：{frozen}；"
        "应改为在调用时读 config（见 app/main.py 的字段闸门）"
    )


# ---------------------------------------------------------------------------
# 归一化自身
# ---------------------------------------------------------------------------
def test_normalize_drops_only_the_release_version():
    """只剔除 `info.version`，其余一律保留。

    关键是"只"：如果顺手把 `description` / `title` 也剔了，
    契约里"接口叫什么、干什么"这层信息就失去了保护 —— 而那同样是承诺。
    """
    raw = {
        "openapi": "3.1.0",
        "info": {"title": "T", "description": "D", "version": "9.9.9"},
        "paths": {"/x": {"get": {}}},
    }
    out = normalize(raw)
    assert "version" not in out["info"], "info.version 没被剔除，发版会污染快照"
    assert out["info"] == {"title": "T", "description": "D"}
    assert out["paths"] == raw["paths"]


def test_normalize_does_not_mutate_its_input():
    """归一化必须返回新对象。

    就地修改会让"生成快照"与"对比快照"共用同一份被改坏的数据 ——
    表现是第二次调用起结果不一致（测试因此变成顺序相关的）。
    """
    raw = {"info": {"version": "1.0"}, "paths": {}}
    normalize(raw)
    assert raw["info"] == {"version": "1.0"}, "normalize 修改了入参"


def test_dumps_is_stable_across_dict_ordering():
    """字典顺序变化不该产生 diff。

    否则"重构一下路由注册顺序"就会生成一份毫无意义的契约变更。
    """
    a = {"info": {"title": "T"}, "paths": {"/x": {"get": {"a": 1, "b": 2}}}}
    b = {"paths": {"/x": {"get": {"b": 2, "a": 1}}}, "info": {"title": "T"}}
    assert dumps(a) == dumps(b)
