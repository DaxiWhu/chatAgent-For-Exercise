"""跨轮历史的 token 预算 + PostgreSQL 会话后端 —— 守卫（2026-10-03 新增）。

为什么单独一个文件：这一层替换掉了整条"**条数上限 + Redis/本地文件**"的旧逻辑，
而它两个最危险的失效方式都是**静默**的：

1. 官方 ``RunConfig.call_model_input_filter`` 的**形状**写错 —— 入参是
   ``CallModelData``（``.model_data`` 才装着条目）、出参是 ``ModelInputData``，
   **不是"进什么出什么"**。它跑在 ``Runner.run`` 内部、**每次模型调用之前**，
   所以写错的表现是"agent 动不动答不出来"，而不是某条单轮测试变红。
2. 会话后端换成 PG 之后，**没有 PG 就整段没有历史**（降级不报错、指标全绿）。

两条都必须在**离线**环境（CI 里没有 PG）被断言 —— 所以本文件刻意只碰"纯函数 + 接线"：
真正的 SQL 由官方后端负责，PG 有没有表由迁移负责。
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("agents", reason="openai-agents 未安装 ⇒ agent 链路会静默降级")

from agents.run_config import CallModelData, ModelInputData  # noqa: E402

from agents import Agent  # noqa: E402
from src.agents import history_budget as hb  # noqa: E402
from src.agents import session as S  # noqa: E402


def _cd(items, instructions=None):
    """按官方形状构造过滤器的入参（``CallModelData``，不是 ``ModelInputData``）。"""
    return CallModelData(
        model_data=ModelInputData(input=list(items), instructions=instructions),
        agent=Agent(name="probe"),
        context=None,
    )


def _json_tokens(item) -> int:
    """与实现同一口径：条目按 JSON 序列化后计数。"""
    return hb.count_tokens(json.dumps(item, ensure_ascii=False, default=str))


def _trim(data):
    """跑一次过滤器。

    它现在是 **async**（分词编码丢进工作线程，避免占住事件循环），
    本文件统一用它取同步结果 —— 官方侧同样是 ``await`` 调用
    （``maybe_filter_model_input`` 里有 ``inspect.isawaitable`` 分支）。
    """
    return asyncio.run(hb.trim_model_input(data))


# --------------------------------------------------------------------------- #
# 1. 官方口子的形状
# --------------------------------------------------------------------------- #
def test_filter_returns_model_input_data_not_the_caller_data(monkeypatch):
    """出参必须是 ``ModelInputData`` —— 这是官方这个口子最容易写错的地方。

    写成 ``return data``（把 ``CallModelData`` 原样还回去）会在**每一次模型调用**上
    炸，而异常抛在 ``Runner.run`` 内部、被 ``run_agent`` 的宽 ``except`` 接住 ⇒
    表现为"agent 链路动不动答不出来"（最终以接口报失败收场，客户端只看到"没生成出来"，
    看不到真实原因）。单轮测试还可能是绿的 —— 所以只能靠这条守卫钉住形状。
    """
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "100000")
    out = _trim(_cd([{"role": "user", "content": "你好"}]))
    assert isinstance(out, ModelInputData)
    assert out.input == [{"role": "user", "content": "你好"}]


def test_filter_passes_through_an_unknown_shape():
    """形状不对 ⇒ 原样放行（宁可退化成"不裁"，也不要抛出去把整轮请求打挂）。"""
    sentinel = object()
    assert _trim(sentinel) is sentinel


# --------------------------------------------------------------------------- #
# 2. 裁的口径
# --------------------------------------------------------------------------- #
def test_over_budget_drops_the_oldest_and_keeps_the_current_turn(monkeypatch):
    """超预算时从**最老**的一端丢，且本轮新输入一定留下。

    ⚠️ "最老"有一个例外：**第一条用户条目**被锚点钉住（代表"这件事一开始要做什么"，
    见 ``test_the_first_user_message_survives``）。所以这里断言的是"中间那段被丢"，
    不是"除了末条全丢" —— 后者是锚点上线前的口径。
    """
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "50")
    items = [
        {"role": "user", "content": "老问题" * 200},
        {"role": "assistant", "content": "老回答" * 200},
        {"role": "user", "content": "这一轮"},
    ]
    out = _trim(_cd(items))
    texts = [i["content"] for i in out.input]
    assert "这一轮" in texts, "超预算时必须留下本轮新输入"
    assert "老回答" * 200 not in texts, "中间的旧历史必须从最老的开始丢"


def test_the_last_item_survives_even_when_it_alone_exceeds_the_budget(monkeypatch):
    """只剩本轮这一条还超额 ⇒ 不再继续裁，交给模型侧判定。

    裁掉它就等于模型连用户刚说的话都看不到 —— 而请求会照常成功，是最难发现的坏。
    """
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "5")
    items = [{"role": "user", "content": "很长" * 500}]
    out = _trim(_cd(items))
    assert len(out.input) == 1
    assert out.input[0]["content"] == "很长" * 500


def test_budget_zero_means_no_trimming(monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "0")
    items = [{"role": "user", "content": "很长" * 500} for _ in range(5)]
    out = _trim(_cd(items))
    assert len(out.input) == 5, "预算 0 = 不限，却仍然裁了"


def test_instructions_count_against_the_budget(monkeypatch):
    """预算算的是"指令 + 条目"的合计 —— 系统提示词同样占窗口。

    用**同一组条目**跑两次做差分：只是多了一段指令，就必须多裁掉一条。
    漏算指令的话，"历史看起来没超"而实际已经超了，且这个偏差**只在系统提示词很长时**
    才显现 —— 平时全绿、偶发报错，最难定位的那一类。
    """
    items = [
        # ⚠️ 第一条刻意用 assistant：这里验的是"指令占不占预算"，而 user 打头会触发
        # "第一条用户条目"锚点，让两次运行的差异被锚点抹平（判据就测不出来了）。
        {"role": "assistant", "content": "上一轮的回答" * 200},
        {"role": "user", "content": "这一轮"},
    ]
    budget = _json_tokens(items[0]) + _json_tokens(items[1]) + 5
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", str(budget))

    assert len(_trim(_cd(items)).input) == 2, "没超预算却裁了"

    out = _trim(_cd(items, instructions="指令" * 200))
    assert out.input == [{"role": "user", "content": "这一轮"}], (
        "指令没有计入预算 ⇒ 实际输入已经超窗而代码以为没超"
    )
    assert out.instructions == "指令" * 200, "指令本身不参与裁剪（它是每轮现拼的配置）"


def test_orphan_tool_output_is_dropped_with_the_call_it_belongs_to(monkeypatch):
    """裁完必须清孤儿：留下的头部不能是一段"调用方已被丢掉"的工具结果。

    这是"从最老的开始丢"的**必然副产品** —— 丢的那一段里可能正好有工具调用，
    而它的结果在后一条。回灌这种历史会被接口拒（官方注释原话：
    *"tool call items that do not have corresponding outputs"* 那一族）。
    这一步委托官方 ``drop_orphan_function_calls``，本用例钉的是"它确实被调了"。
    """
    # ⚠️ 开头这条刻意是 assistant：这里要造的是"被丢的那段"里只有普通条目，
    # 不含"第一条用户条目"锚点（否则它会被留下，孤儿场景就构造不出来）。
    pad = {"role": "assistant", "content": "填充" * 300}
    call = {"type": "function_call", "call_id": "c1", "name": "kb_search", "arguments": "{}"}
    output = {"type": "function_call_output", "call_id": "c1", "output": "命中一段"}
    current = {"role": "user", "content": "这一轮"}
    # 预算刚好够"工具结果 + 本轮输入"，不够再带上那个调用 ⇒ 裁完只会留下孤儿的 output。
    monkeypatch.setenv(
        "AGENT_HISTORY_TOKEN_LIMIT", str(_json_tokens(output) + _json_tokens(current) + 5)
    )
    out = _trim(_cd([pad, call, output, current]))
    assert out.input == [current], (
        "调用已被裁掉，它的工具结果却留下来了 —— 回灌会被接口拒"
    )


def test_keeps_the_longest_suffix_that_fits(monkeypatch):
    """留下的必须是**最长**的、总量不超预算的后缀 —— 不多留，也不多丢。

    边界刻意取"刚好等于预算"：判定必须是 `超了才停`，不是 `够了就停`，
    否则每次都会白丢一轮历史。

    ⚠️ 前两条刻意是 assistant：若用 ``user`` 打头，"第一条用户条目"锚点会把被丢那段
    里的第一条捞回来，这条判据（后缀长度）就测不准了 —— 锚点有自己的用例。
    """
    items = [
        {"role": "assistant", "content": "第一步" * 100},
        {"role": "assistant", "content": "第二步" * 100},
        {"role": "user", "content": "第三轮要求" * 100},
    ]
    budget = _json_tokens(items[1]) + _json_tokens(items[2])
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", str(budget))
    assert _trim(_cd(items)).input == items[1:], "刚好装得下后两条，却多丢了"


def test_measurement_stops_at_the_budget_instead_of_scanning_the_whole_history(monkeypatch):
    """编码量必须有上界：越界之后**不再去量**更老的条目，直接丢掉。

    为什么这是硬要求而不是优化：历史在本项目是**永久保留**的，库里堆了多少就有多少。
    每轮都把全部历史编码一遍 ⇒ 开销随会话长度线性无限增长。实测（同一台机器）：
    10 万 token 15.8ms / 50 万 82.3ms / 100 万 157.2ms / 200 万 341.1ms。
    而这段 CPU 跑在**每次模型调用之前**，一轮对话里会发生多次。

    本用例用打点的方式钉住"量了几条"，而不是钉耗时 —— 耗时在 CI 上不稳定，
    而"有没有把全部历史都量一遍"是**确定性的结构事实**。
    """
    monkeypatch.setenv("AGENT_HISTORY_TOKEN_LIMIT", "80")
    measured = []
    real = hb._item_tokens

    def spy(item):
        measured.append(item)
        return real(item)

    monkeypatch.setattr(hb, "_item_tokens", spy)

    items = [{"role": "user", "content": "很老的一轮" * 200} for _ in range(40)]
    items.append({"role": "user", "content": "这一轮"})
    _trim(_cd(items))

    assert len(measured) < len(items), (
        "把全部历史都量了一遍 —— 开销随会话长度无上界（永久保留下会一直涨）"
    )
    assert len(measured) <= 3, "预算很小，应当量两三条就停"


def test_warmup_forces_the_tokenizer_to_load_up_front(monkeypatch):
    """预热必须**真的**把词表加载进来。

    实测首次 ``count_tokens`` 277.6ms、之后 0.054ms —— 那 277ms 是词表冷加载。
    不预热的话它落在"第一个需要裁剪的请求"里，且只在进程刚起来时出现，
    事后完全无法复现（看起来像"偶发的首请求变慢"）。这个用例钉住"预热血了"。
    """
    import tiktoken

    loaded = []
    real = tiktoken.get_encoding

    def spy(name):
        loaded.append(name)
        return real(name)

    monkeypatch.setattr(tiktoken, "get_encoding", spy)
    monkeypatch.setattr(hb, "_encoder", None)
    monkeypatch.setattr(hb, "_encoder_failed", False)

    asyncio.run(hb.warmup_tokenizer())

    assert loaded, "预热没有触发词表加载 —— 那 250ms 仍然会落在第一个用户请求上"
    assert hb._encoder is not None, "预热跑完了，编码器却没被留下"


# --------------------------------------------------------------------------- #
# 3. PG 会话后端：连接串、意图、构造出来的确实是官方后端
# --------------------------------------------------------------------------- #
def test_db_url_becomes_an_async_dsn():
    """``postgresql://`` 必须补成 ``postgresql+psycopg://``。

    少了驱动名，``create_async_engine`` 会直接报 "The asyncio extension requires an
    async driver" —— 而那句话听起来像"驱动装错了"，实际是"连接串写法不对"。
    """
    assert S.to_async_dsn("postgresql://u:p@h:5433/db?sslmode=disable") == (
        "postgresql+psycopg://u:p@h:5433/db?sslmode=disable"
    )
    # 已经带了驱动就不重复加（幂等）
    assert S.to_async_dsn("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert S.to_async_dsn("") == ""


def test_session_backend_intent_follows_db_url(monkeypatch):
    monkeypatch.delenv("DB_URL", raising=False)
    assert S.session_backend_intent() == "none"
    monkeypatch.setenv("DB_URL", "postgresql://u:p@h/db")
    assert S.session_backend_intent() == "postgres"


def test_session_for_builds_the_official_pg_backend(monkeypatch):
    """配了 ``DB_URL`` ⇒ 拿到的必须是**官方后端**（外面可再包一层压缩装饰器）。

    这条**不连库**：``create_async_engine`` 是懒连接，构造对象不建连。所以它能在没有
    PG 的 CI 里证明"接线接对了、而且用的是官方后端而不是自己写的存储"。

    ⚠️ 2026-10-08 起外层多了一个 ``CompactingSession``（历史压缩的会话装饰器）。它
    **不是**"自己写的存储"——四个协议方法原样透传，换掉的只有"允许主动重写历史"
    这一件事。所以判据是"穿过装饰器之后看到的是官方后端"，而不是"顶层就是它"：
    把装饰器去掉会让压缩的锁与 SDK 的写入不在同一把锁上（那就是真的静默丢数据了）。

    ⚠️ 顺带钉住"条数上限保持官方默认（``None`` = 全取）"：条数与 token 是互不知道
    对方的两套判据，两处都设上限时条数那份会**先**把历史切掉。
    """
    from agents.extensions.memory import SQLAlchemySession

    from src.agents.compaction import CompactingSession

    monkeypatch.setenv("DB_URL", "postgresql://u:p@localhost:5433/db")
    S.reset_sessions()
    try:
        obj = S.session_for("t-1")
        assert isinstance(obj, CompactingSession), (
            "会话必须被压缩装饰器包住 —— 压缩的锁要与 SDK 的会话写入共用同一把"
        )
        inner = obj._inner
        assert isinstance(inner, SQLAlchemySession)
        assert obj.session_id == "t-1"
        assert inner.session_id == "t-1"
        # 装饰器必须**代理**这两个属性：SDK 是 `getattr(session, "session_settings")`，
        # 拿不到就静默改用默认值。
        assert obj.session_settings is inner.session_settings
        assert inner.session_settings.limit is None
    finally:
        S.reset_sessions()


def test_missing_db_url_returns_none_instead_of_raising(monkeypatch):
    """没配 ``DB_URL`` ⇒ **返回 ``None``**（"本轮没有历史"），不是抛异常。

    抛出去会被 ``run_agent`` 的宽 ``except`` 接住 ⇒ ``exit_reason="error"`` ⇒
    节点抛 ``AgentRunFailed``、整个请求以失败收场（客户端看到的是"这次答不出来"，
    而不是"只是接不上上文"）。丢历史是降级，不是失败。
    """
    monkeypatch.delenv("DB_URL", raising=False)
    S.reset_sessions()
    try:
        assert S.session_for("t-1") is None
    finally:
        S.reset_sessions()


# --------------------------------------------------------------------------- #
# 3.5 `clear_session` —— 删除端点靠它兑现"已删除"
# --------------------------------------------------------------------------- #
def test_clear_session_is_false_and_silent_when_there_is_no_backend(monkeypatch):
    """没有后端（或会话编号为空）⇒ **返回 False，且绝不抛**。

    为什么"绝不抛"是硬要求：这个函数被删除端点直接 await。抛出去会让一次**幂等的**
    删除请求整体失败 —— 而调用方要的信息只是"这一项删掉没有"，明细里如实写 False
    比 500 有用得多（其余三项本该照删）。
    """
    monkeypatch.delenv("DB_URL", raising=False)
    S.reset_sessions()
    try:
        assert asyncio.run(S.clear_session("")) is False
        assert asyncio.run(S.clear_session("t-1")) is False
    finally:
        S.reset_sessions()


def test_clear_session_swallows_backend_errors(monkeypatch):
    """后端自己炸了也只是 False —— 它是一条**删除**路径上的降级，不是故障源。"""
    class _Boom:
        async def clear_session(self):
            raise RuntimeError("backend down")

    monkeypatch.setattr(S, "session_for", lambda sid: _Boom())
    assert asyncio.run(S.clear_session("t-1")) is False


def test_clear_session_clears_the_backend_and_drops_the_cached_object(monkeypatch):
    """★ 清干净时：**真调后端**，并且把缓存里的会话对象丢掉。

    丢掉引用这一步为什么值得单钉：官方 PG 后端每次调用都读库（不在内存缓存条目），
    所以"不丢"在当下也是对的。但"清完之后还有人拿着一份旧的"这种失效方式
    **没有任何症状** —— 下一轮照常接上已经承诺删除的上下文。丢一个引用换掉这个
    风险是划算的，而便宜的东西最容易在重构时被顺手删掉。
    """
    called: list[str] = []

    class _Recorder:
        async def clear_session(self):
            called.append("cleared")

    fake = _Recorder()
    monkeypatch.setattr(S, "session_for", lambda sid: fake)
    monkeypatch.setattr(S, "_sessions", {"t-1": fake})

    assert asyncio.run(S.clear_session("t-1")) is True
    assert called == ["cleared"]
    assert "t-1" not in S._sessions, "清完之后缓存里还留着旧的会话对象"


# --------------------------------------------------------------------------- #
# 4. 建表迁移：DDL 从 SDK 自己的表定义生成
# --------------------------------------------------------------------------- #
def test_agent_session_ddl_comes_from_the_sdk_table_definitions():
    """两张表的 DDL 必须是"SDK 说的那一份"，且幂等。

    为什么不去断言一份**手写**的期望 DDL：那等于把两份 DDL 一起维护，
    而它们会在 SDK 升版时分叉 —— 分叉的后果是运行期才炸（取历史全失败 ⇒
    ``session_for`` 降级 ⇒ 每轮没有历史，而服务照常跑、指标全绿）。
    所以这里只钉**结构事实**（表名 / 列名 / 索引 / 外键 / 幂等），
    列清单本身由 `agent_session_ddl` 从 SDK 的 Table 对象现取。
    """
    from app.migrations import agent_session_ddl

    sql = "\n".join(agent_session_ddl("postgresql://u:p@localhost:5433/db"))

    assert "CREATE TABLE IF NOT EXISTS agent_sessions" in sql
    assert "CREATE TABLE IF NOT EXISTS agent_messages" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_agent_messages_session_time" in sql
    # 外键带级联：删掉一条会话必须连带它的消息，否则会留下一堆永远读不到的孤儿行。
    assert "REFERENCES agent_sessions (session_id) ON DELETE CASCADE" in sql
    for column in ("session_id", "created_at", "updated_at", "message_data"):
        assert column in sql, f"DDL 缺列 {column}"
    # 幂等：迁移允许被重复执行（人工重跑不该炸）
    assert sql.count("IF NOT EXISTS") >= 3


def test_agent_session_tables_have_a_registered_migration():
    """表必须挂在迁移里 —— 生产**不**靠运行时自动建表（`create_tables=False`）。

    漏了这一步的症状：本地怎么跑都对（本地也是跑迁移），线上取历史全失败 ⇒
    `session_for` 降级 ⇒ 每轮没有历史，而所有指标正常。
    """
    from app.migrations import MIGRATIONS

    versions = [m.version for m in MIGRATIONS]
    assert len(versions) == len(set(versions)), "迁移版本号重复"
    assert 7 in versions, "agent_sessions / agent_messages 的建表迁移没有注册"


# --------------------------------------------------------------------------- #
# 5. 配置口径：回显与实际行为必须是同一个意思
# --------------------------------------------------------------------------- #
def test_zero_and_negative_mean_unlimited_in_both_places():
    """同一个环境变量被**两处独立解析**（`app/config.py` 给启动日志与回显看、
    `src/agents/history_budget.py` 给实际裁剪用）。两处必须对 `0` / 负数给出
    **同一种意思**。

    漂移的后果不是崩，是**日志说谎**：回显报一个数、实际按另一套走，
    运维照着日志调参会调错方向 —— 而且这是分层约束（`src/` 不依赖 `app/`）
    带来的结构性风险，不写守卫就一定会漂。

    `history_token_limit()` 每次调用都读环境变量，所以只有 config 需要重新加载。
    """
    import os

    from app import config as appconfig
    from tests.conftest import reload_app_config

    # ⚠️ 重载 config 必须走 `reload_app_config()`，不能直接 `importlib.reload`：
    # 后者会在"已被 conftest 擦净的环境"上重新求值，把注入的 `AUTH_SESSION_SECRET`
    # 冲成空串 ⇒ 其它文件在收集期签好的会话 token 全部验签失败（满天 401）。
    # 详见 `tests/conftest.py` 里那个函数的说明。
    def probe(raw):
        old = os.environ.get("AGENT_HISTORY_TOKEN_LIMIT")
        os.environ["AGENT_HISTORY_TOKEN_LIMIT"] = raw
        try:
            reload_app_config()
            return appconfig.AGENT_HISTORY_TOKEN_LIMIT, hb.history_token_limit()
        finally:
            if old is None:
                os.environ.pop("AGENT_HISTORY_TOKEN_LIMIT", None)
            else:
                os.environ["AGENT_HISTORY_TOKEN_LIMIT"] = old
            reload_app_config()

    for raw in ("0", "-5"):
        cfg, budget = probe(raw)
        assert cfg is None, (
            f"配置 {raw} = 不限，但回显成了 {cfg!r} —— 日志里的 `0` 会被读成"
            "「一个 token 都不给」，和实际行为正好相反"
        )
        assert budget is None, f"配置 {raw} 应表示不限，实际 {budget!r}"

    cfg, budget = probe("100000")
    assert cfg == 100000 and budget == 100000, "正常值也不该被改写"
