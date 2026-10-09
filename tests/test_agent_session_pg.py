"""跨轮对话历史的**真库**集成测试（2026-10-03 新增）。

为什么这条必须有
----------------
在它之前，"跨轮对话历史"这条链路在**任何自动环境里都没有真正跑过**：

  - **本地**：`tests/_agents_testkit.py` 给的是内存假会话，不碰数据库；
  - **CI 每次提交**：最小依赖清单里没有 `openai-agents` ⇒ 整个 agent 测试块被
    `importorskip` **跳过**（不是失败）；
  - **CI 每日全量**：依赖装齐了，但跑测试前会**显式清空** `DB_URL` ⇒ 仍然不落盘。

也就是说，"写进去了、也读得出来"这件事**一处都没被证明过**。而这一层的失效方式偏偏
是静默的：取不到历史就降级成"本轮没有历史"，服务照常返回 200、错误率与延迟全正常 ——
用户那边只表现为"接着改上一轮的东西"被当成全新话题。

所以本文件补的就是这一格：**真建表、真写入、真换一条连接读回来**。

怎么跑
------
需要一个真实 PG，用 `AGENT_SESSION_TEST_DSN` 指过去；没配就整体跳过（本地不影响）。
CI 里由 `agent-session-pg` job 起一个 service container 提供。

⚠️ 刻意**不复用 `DB_URL`**：万一有人在本地把 `DB_URL` 指向了真实环境，这个测试会**建表并写入**
—— 用一个专用变量把"被测库"与"日常开发库"分开。只有最后那条接线用例会临时改 `DB_URL`，
并在结束时还原。
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

pytest.importorskip("agents", reason="openai-agents 未安装 ⇒ agent 链路会静默降级")

_DSN = (os.getenv("AGENT_SESSION_TEST_DSN") or "").strip()

pytestmark = pytest.mark.skipif(
    not _DSN, reason="未配置 AGENT_SESSION_TEST_DSN ⇒ 跳过真库集成测试"
)


def _sid() -> str:
    return f"it-{uuid.uuid4().hex[:12]}"


def _run(coro):
    return asyncio.run(coro)


async def _create_tables(dsn: str) -> None:
    """按**迁移会执行的那份 DDL** 建表。

    刻意调 `agent_session_ddl()` 而不是 `session.create_tables=True`：
    生产建表归迁移管（`create_tables` 保持官方的 `False`，否则多副本同时启动会互相抢 DDL）。
    这里复用同一个函数，是为了让"本测试建出来的结构"与"生产迁移建出来的结构"
    **是同一份** —— 如果两边各写一份，测试绿而生产缺一个索引/外键是完全可能的。
    """
    import psycopg

    from app.migrations import agent_session_ddl

    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        for stmt in agent_session_ddl(dsn):
            await conn.execute(stmt)
        await conn.commit()


async def _open_session(session_id: str, dsn: str):
    """开一个**独立引擎**的会话。每次调用 = 新的连接池 = 模拟另一次请求 / 另一个副本。"""
    from agents.extensions.memory import SQLAlchemySession
    from sqlalchemy.ext.asyncio import create_async_engine

    from src.agents.session import to_async_dsn

    engine = create_async_engine(to_async_dsn(dsn))
    return SQLAlchemySession(session_id, engine=engine), engine


@pytest.fixture(scope="module", autouse=True)
def _tables():
    """整个模块开跑前把表建出来（幂等：DDL 全是 IF NOT EXISTS）。"""
    _run(_create_tables(_DSN))


# --------------------------------------------------------------------------- #
# 1. 表结构：真的是迁移那份被建出来了
# --------------------------------------------------------------------------- #
def test_migration_ddl_creates_both_tables_in_a_real_database():
    """两张表 + 那条索引真的存在。

    `agent_session_ddl` 是**从 SDK 自己的 Table 对象**编译出来的（不是手写 DDL），
    所以这条同时钉住了两件事：DDL 能在这个 PG 上执行成功，且执行完真的有那两张表。
    SDK 升版改了表名/列名时，这里会先红 —— 而不是等到线上取历史全失败。
    """
    import psycopg

    with psycopg.connect(_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
                " AND tablename IN ('agent_sessions','agent_messages')"
            )
            names = {r[0] for r in cur.fetchall()}
            assert names == {"agent_sessions", "agent_messages"}, (
                f"表没建出来（只有 {names}）—— 线上会表现为每轮都没有历史"
            )

            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname='public'"
                " AND tablename='agent_messages'"
            )
            idx = {r[0] for r in cur.fetchall()}
            assert "idx_agent_messages_session_time" in idx, (
                f"按会话取历史的索引不在（现有 {idx}）—— 历史越长取回越慢"
            )


# --------------------------------------------------------------------------- #
# 2. 跨轮的核心含义：换一条连接还能读到
# --------------------------------------------------------------------------- #
def test_written_items_are_visible_from_a_separate_engine():
    """写进去的条目必须能被**另一个连接池**读出来。

    这是"跨轮"这个词的全部含义：历史要跨请求、跨进程存活。只在本进程内存里对得上
    等于什么都没证明 —— 而"进程内缓存"恰恰是最容易写出来、也最容易在多副本下崩掉的实现
    （每个副本各存一份 ⇒ 同一段对话按请求落在哪个副本就随机失忆，而所有指标正常）。
    """
    sid = _sid()

    async def scenario():
        first, engine_a = await _open_session(sid, _DSN)
        await first.add_items(
            [
                {"role": "user", "content": "第一轮的问题"},
                {"role": "assistant", "content": "第一轮的回答"},
            ]
        )
        await engine_a.dispose()

        # 全新引擎 = 全新连接池：数据只能来自库，不可能来自上一段的内存。
        second, engine_b = await _open_session(sid, _DSN)
        try:
            items = await second.get_items()
        finally:
            try:
                await second.clear_session()
            finally:
                await engine_b.dispose()
        return items

    items = _run(scenario())
    assert [i.get("content") for i in items] == ["第一轮的问题", "第一轮的回答"]


def test_a_second_round_appends_instead_of_replacing():
    """第二轮写入是**追加**，不是覆盖 —— 这是"轮"能累积的前提。

    覆盖式实现（每轮重写整份）在有并发时会把彼此的历史抹掉，而且**看不出来**：
    每次读取都自洽，只是内容会莫名少了前面几轮。
    """
    sid = _sid()

    async def scenario():
        s1, e1 = await _open_session(sid, _DSN)
        await s1.add_items([{"role": "user", "content": "第一轮"}])
        await e1.dispose()

        s2, e2 = await _open_session(sid, _DSN)
        await s2.add_items([{"role": "user", "content": "第二轮"}])
        await e2.dispose()

        s3, e3 = await _open_session(sid, _DSN)
        try:
            items = await s3.get_items()
        finally:
            try:
                await s3.clear_session()
            finally:
                await e3.dispose()
        return items

    items = _run(scenario())
    assert [i.get("content") for i in items] == ["第一轮", "第二轮"]


def test_sessions_do_not_leak_into_each_other():
    """不同会话之间必须完全隔离。

    串了的表现是"答非所问"，而且往往要等到两个会话同时活跃才出现 ——
    在单会话的手工验证里永远看不到。
    """
    a, b = _sid(), _sid()

    async def scenario():
        sa, ea = await _open_session(a, _DSN)
        sb, eb = await _open_session(b, _DSN)
        try:
            await sa.add_items([{"role": "user", "content": "会话 A 的内容"}])
            await sb.add_items([{"role": "user", "content": "会话 B 的内容"}])
            got_a = await sa.get_items()
            got_b = await sb.get_items()
        finally:
            await sa.clear_session()
            await sb.clear_session()
            await ea.dispose()
            await eb.dispose()
        return got_a, got_b

    got_a, got_b = _run(scenario())
    assert [i.get("content") for i in got_a] == ["会话 A 的内容"]
    assert [i.get("content") for i in got_b] == ["会话 B 的内容"]


# --------------------------------------------------------------------------- #
# 3. 接线：走 app 的入口也要真的落库
# --------------------------------------------------------------------------- #
def test_session_for_wires_to_the_real_database(monkeypatch):
    """`session_for()` 拿到的会话必须真连上了库。

    上面几条都绕开了 `src/agents/session.py` 直接构造。这一条走**应用真正的入口**
    —— 它多覆盖了两件单元测试碰不到的事：`to_async_dsn` 的转换在真驱动上成立、
    以及进程内那个**唯一引擎**能被真实使用。

    ⚠️ 临时改 `DB_URL` 并在结束时还原：这个变量是全局的，泄漏出去会影响其它用例。
    """
    from src.agents import session as S

    sid = _sid()
    monkeypatch.setenv("DB_URL", _DSN)
    S.reset_sessions()
    try:
        async def write_then_read():
            session = S.session_for(sid)
            assert session is not None, "配了 DB_URL 却拿不到会话"
            await session.add_items([{"role": "user", "content": "走入口写入"}])

            reader, engine = await _open_session(sid, _DSN)
            try:
                return await reader.get_items()
            finally:
                await reader.clear_session()
                await engine.dispose()

        items = asyncio.run(write_then_read())
    finally:
        S.reset_sessions()

    assert [i.get("content") for i in items] == ["走入口写入"]


# --------------------------------------------------------------------------- #
# 4. 取历史的代价有上界吗（永久保留下这是唯一会变慢的地方）
# --------------------------------------------------------------------------- #
def test_reading_a_long_history_is_linear_but_bounded_by_the_budget_layer():
    """长历史**能读回来**，而且条数是准确的。

    这条不测耗时（CI 上不稳定），只测**一件确定性的结构事实**：
    哪怕会话攒了几百条，`get_items()` 仍然把全部条目按顺序交回来。

    ⚠️ 读回的量是 O(历史)，这是刻意没设条数上限的结果（条数上限会**先**把历史切掉，
    让按 token 的那层看不到真实总量）。真正兜住"送进模型多少"的是
    `src/agents/history_budget.py` 的 token 预算 —— 它的守卫在
    `tests/test_agent_history_budget.py`，不需要真库。
    """
    sid = _sid()
    count = 300

    async def scenario():
        s, e = await _open_session(sid, _DSN)
        await s.add_items(
            [{"role": "user", "content": f"第 {i} 条"} for i in range(count)]
        )
        await e.dispose()

        reader, engine = await _open_session(sid, _DSN)
        try:
            return await reader.get_items()
        finally:
            await reader.clear_session()
            await engine.dispose()

    items = _run(scenario())
    assert len(items) == count
    assert items[0].get("content") == "第 0 条"
    assert items[-1].get("content") == f"第 {count - 1} 条"
