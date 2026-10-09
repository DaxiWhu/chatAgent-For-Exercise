"""守卫：真异步路径上的同步存储调用必须显式挪出事件循环。

为什么需要这层守卫
==================
`app/main.py` 里的 Redis / PG 客户端是**模块级同步单例**，而 FastAPI 的
`async def` 端点体直接在事件循环上跑。一次同步网络往返落在 `async def` 里，
就等于把这一个请求的等待时间转嫁成**本进程所有并发请求的停转时间**，包
括正在推的 SSE 流。其中最危险的一条 PG 查询没有语句级超时，只有连接超时
⇒ 一次抖动可以让整个进程"活着但什么都不动、重启即恢复"。

这个退化在正常流量下**完全看不出来**（每次只慢几毫秒），只在故障时才现形，
所以不能靠人 review，必须靠静态守卫钉住。

钉的是两条不变量
================
A. **路由处理器里 `async def` 必须真的含 `await`**。
   若一个端点函数体里一个 `await` 都没有，`async` 对它是装饰性的，唯一实际
   效果是把阻塞直接压上事件循环 —— 这种端点应当写 `def`，交给 anyio 线程池。
   （本轮把 21 个这样的端点改回了 `def`。）

B. **`async def` 体内不得直接调用已知的同步存储/闸门**。
   它们必须先被 `_off(...)` 包起来（`asyncio.to_thread`）。注意正确写法
   `_off(_LIMITER.check, ...)` 会把**可调用对象本身**当参数传进去，源码里
   根本不会出现 `_LIMITER.check(...)` 这个调用节点 —— 所以"能扫到一个匹配
   的调用节点"本身就是不合格，不需要再去判父节点。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "app" / "main.py"

# 同步存储 / 闸门的调用面。两个来源：
#   1. 模块级函数（自己读 PG / Redis，或转调它们）
#   2. 模块级单例上的方法（`_QUOTA.claim` 这种）
# 新增读存储的模块级单例时，务必同步扩这两个集合 —— 否则守卫会静默漏掉它。
SYNC_FUNCS = {
    "_authenticate",
    "_authenticate_user",
    "_preflight",
    "_rate_limit",
    "_idempotency_gate",
    "_release_idem_gate",
    "_reserve_quota",
    "_quota_gate",
    "_claim_session",
    "_assert_session_readable",
}
SYNC_SINGLETONS = {
    "_REVIEW_QUEUE",
    "_SESSIONS",
    "_ACCOUNTS",
    "_AUTH",
    "_LIMITER",
    "_QUOTA",
    "_IDEMPOTENCY",
}

# 允许的挪走方式（`_off` 是本项目自己的封装）。
OFFLOADERS = {"_off", "to_thread", "run_in_threadpool"}

# 精确豁免：(所在函数名, 命中标签)。**必须逐条给理由**，不做"整类放行"。
ALLOWED: set[tuple[str, str]] = {
    # lifespan 是**启动期**代码：此刻还没有任何请求在跑，也就不存在"阻塞了别人"
    # 的问题；而且这个分支只在无 DB_URL 的**内存后端**下可达，`seed_if_empty`
    # 走的是纯字典插入、零网络 IO。同一函数的真实 PG 分支（第 378 行）已经用了
    # `asyncio.to_thread` —— 那一条是本项目"同步 IO 必须挪走"的正面样板。
    ("lifespan", "_ACCOUNTS.seed_if_empty"),
}

# HTTP 动词，用来认"这个函数是路由处理器"。
VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "api_route"}


def _main_ast() -> ast.Module:
    return ast.parse(MAIN.read_text(encoding="utf-8"), filename=str(MAIN))


def _sync_label(call: ast.Call) -> str | None:
    """若该调用命中同步存储面，返回可读标签；否则 None。"""
    f = call.func
    if isinstance(f, ast.Name) and f.id in SYNC_FUNCS:
        return f.id
    if (isinstance(f, ast.Attribute)
            and isinstance(f.value, ast.Name)
            and f.value.id in SYNC_SINGLETONS):
        return f"{f.value.id}.{f.attr}"
    return None


def _is_route_handler(node: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    for deco in node.decorator_list:
        target = deco.func if isinstance(deco, ast.Call) else deco
        if isinstance(target, ast.Attribute) and target.attr in VERBS:
            return True
    return False


def _has_await(node: ast.AST) -> bool:
    return any(isinstance(n, (ast.Await, ast.AsyncFor, ast.AsyncWith))
               for n in ast.walk(node))


def _async_defs(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            yield node


def test_route_handlers_declared_async_actually_await() -> None:
    """不变量 A：`async def` 路由体里至少要有一个 `await`，否则应写 `def`。

    反例（本轮修掉的形态）：`async def health(): return {"ok": True}` 里夹着
    一次同步 Redis 读取 —— 它既不并发、也拿不到任何 async 好处，只是把阻塞
    放上了循环。改成 `def` 后由 anyio 线程池接管，语义完全不变而循环不再被压。
    """
    offenders = [f"{n.name}（第 {n.lineno} 行）"
                 for n in _async_defs(_main_ast())
                 if _is_route_handler(n) and not _has_await(n)]

    assert not offenders, (
        "以下路由声明为 `async def` 但函数体里没有任何 `await`，"
        "`async` 对它们只是装饰性的、唯一效果是把同步阻塞压在事件循环上。"
        "请改成 `def`（交给 anyio 线程池）：\n  " + "\n  ".join(offenders)
    )


def test_async_bodies_do_not_call_sync_storage_directly() -> None:
    """不变量 B：`async def` 体内不得出现同步存储/闸门的直接调用。

    正确写法是 `await _off(_LIMITER.check, ...)` —— 传的是**可调用对象**，
    所以源码里不会出现被调用的那个节点。扫到调用节点即不合格。
    """
    offenders: list[str] = []
    for fn in _async_defs(_main_ast()):
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call):
                continue
            # 跳过 `_off(...)` 自身：它的参数是 callable，不是调用。
            f = call.func
            head = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if head in OFFLOADERS:
                continue
            label = _sync_label(call)
            if label is not None and (fn.name, label) not in ALLOWED:
                offenders.append(f"{label}（第 {call.lineno} 行，在 {fn.name} 内）")

    assert not offenders, (
        "以下同步存储/闸门调用直接落在 `async def` 体内，会阻塞事件循环。"
        "请改为 `await _off(<可调用对象>, ...)`：\n  " + "\n  ".join(offenders)
    )


def test_sse_from_async_endpoints_always_passes_pre_reserved_quota() -> None:
    """不变量 C：异步端点调 `_sse` 时必须带 `reserved=`。

    `_sse` 是同步函数，`reserved=None` 时它会**就地**跑同步配额预占（Redis）。
    异步端点（`/v1/review`、`/v1/chat/resume`）必须先用
    `await _off(_reserve_quota, ...)` 预占再把结果传进来 —— 少了这个关键字，
    那一次 Redis 往返就又回到事件循环上了，而它在正常流量下只慢几毫秒、
    看不出来。
    """
    offenders: list[str] = []
    for fn in _async_defs(_main_ast()):
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call):
                continue
            f = call.func
            if not (isinstance(f, ast.Name) and f.id == "_sse"):
                continue
            if "reserved" not in {kw.arg for kw in call.keywords}:
                offenders.append(f"第 {call.lineno} 行，在 {fn.name} 内")

    assert not offenders, (
        "以下异步端点调用 `_sse(...)` 时未传 `reserved=`，会就地跑同步配额预占：\n  "
        + "\n  ".join(offenders)
    )


def test_allowlist_entries_are_all_still_needed() -> None:
    """反向锁：`ALLOWED` 里的豁免条目必须仍然被真实命中。

    豁免是守卫的"洞"。一旦某条豁免对应的代码被改成了规范写法（或那段代码被
    删掉），豁免本身就成了**纯粹的漏洞**：它不再为任何真实情况服务，却继续
    允许同一标签在别处直接调用。所以豁免必须"用得着"才能留着。
    """
    hit: set[tuple[str, str]] = set()
    for fn in _async_defs(_main_ast()):
        for call in ast.walk(fn):
            if isinstance(call, ast.Call):
                label = _sync_label(call)
                if label is not None:
                    hit.add((fn.name, label))

    stale = sorted(ALLOWED - hit)
    assert not stale, (
        "以下豁免已不再被任何真实调用命中（对应代码已改规范或被删除），"
        "请从 ALLOWED 中删除，否则它只是一条无理由的漏洞：\n  "
        + "\n  ".join(f"{fn} / {label}" for fn, label in stale)
    )


def test_forwarding_route_handlers_match_the_asyncness_of_their_target() -> None:
    """不变量 D：转发型路由（兼容别名的 `return xxx_v1(...)`）必须与被转发者同形。

    "旧路径别名"是薄转发层。如果被转发者是 `async def`（本项目里是那 4 个必须
    流式产出的端点），别名也必须是 `async def` 且用 `await` 转发。反过来写成同步
    `def` 时，`return review_v1(...)` 交出的不是响应而是一个**协程对象**，
    FastAPI 会把它交给 JSON 序列化器，报
    `ValueError: ... coroutine was never awaited` —— 报错点离病因很远，
    且只在真正请求该别名时才现形。

    （本轮批量把"函数体无 await 的 async 端点"改成 `def` 时，`review_legacy`
    就是这么被误伤的：它自己没有 `await`，但转发的目标有。）
    """
    tree = _main_ast()
    # 只比对**同为路由处理器**的目标：`return _sse(...)` 这种是就地构造响应对象，
    # 与被转发者的"可等待性必须一致"无关（`_sse` 是同步函数，本来就该被同步调用）。
    top = {n.name: isinstance(n, ast.AsyncFunctionDef)
           for n in tree.body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_route_handler(n)}

    offenders: list[str] = []
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_route_handler(fn):
            continue
        caller_async = isinstance(fn, ast.AsyncFunctionDef)
        for node in ast.walk(fn):
            if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
                continue
            f = node.value.func
            if not (isinstance(f, ast.Name) and f.id in top):
                continue
            callee_async = top[f.id]
            if callee_async != caller_async:
                offenders.append(
                    f"{fn.name}（{'async' if caller_async else 'sync'}，第 {fn.lineno} 行）"
                    f" → {f.id}（{'async' if callee_async else 'sync'}，第 {node.lineno} 行）")

    assert not offenders, (
        "以下转发型路由与它转发目标的可等待性不一致，会在运行时报"
        "「coroutine was never awaited」：\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("name", sorted(SYNC_FUNCS))
def test_named_sync_gates_still_exist(name: str) -> None:
    """反向锁：守卫引用的闸门名必须在源码里真实存在。

    少了这条，某天有人把 `_preflight` 改名/删掉之后，守卫会因为"扫不到任何
    命中"而**装作通过** —— 这正是静态守卫最典型的失效方式。
    """
    tree = _main_ast()
    defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert name in defined, f"守卫引用的 `{name}` 在 app/main.py 里已不存在，请同步更新守卫"


@pytest.mark.parametrize("name", sorted(SYNC_SINGLETONS))
def test_named_sync_singletons_still_exist(name: str) -> None:
    """反向锁：守卫引用的模块级单例名必须真实存在（理由同上）。"""
    tree = _main_ast()
    assigned = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)}
    assert name in assigned, f"守卫引用的单例 `{name}` 在 app/main.py 里已不存在，请同步更新守卫"
