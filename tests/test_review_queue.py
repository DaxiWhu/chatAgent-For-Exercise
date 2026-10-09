"""审核队列：内存后端的入队/出队/列举 + 通知体构造（不真的发请求）。"""
import pytest

from app.review_queue import ReviewQueue


def _q(**kw):
    kw.setdefault("dsn", "")          # 空 dsn = 内存后端，离线
    return ReviewQueue(**kw)


def test_add_then_pending_lists_item():
    q = _q()
    assert q.add("t1", draft="草稿内容", score=42, feedback="再改改", tenant="acme")
    items = q.pending()
    assert len(items) == 1
    assert items[0]["thread_id"] == "t1"
    assert items[0]["score"] == 42
    assert q.count_pending() == 1


def test_add_is_idempotent_per_thread():
    q = _q()
    q.add("t1", draft="v1", score=10)
    q.add("t1", draft="v2", score=20)
    items = q.pending()
    assert len(items) == 1 and items[0]["score"] == 20


def test_resolve_takes_item_out_of_pending():
    q = _q()
    q.add("t1")
    assert q.resolve("t1", "accepted")
    assert q.pending() == []


def test_pending_filters_by_tenant():
    q = _q()
    q.add("t1", tenant="a")
    q.add("t2", tenant="b")
    assert [i["thread_id"] for i in q.pending(tenant="a")] == ["t1"]


def test_pending_empty_tenant_filters_instead_of_returning_everything():
    """空串 = **空租户那一份队列**，不是"不加过滤"。

    此前判据是 `if tenant:`，于是空串落进 else 分支 ⇒ 不加租户条件 ⇒ 返回全部。
    这个等价关系没有任何症状：调用方看着"我明明传了 tenant"，实际拿到的是全量。
    `GET /v1/reviews` 正好踩中 —— 它传的是身份原始租户，而"还没有租户"
    （`tenant=""`）是正常运营中必然存在的状态，于是那个账号列出了全租户草稿。

    "不过滤"现在必须**显式传 `None`**：两种意图在调用点一眼可辨，
    而且不传参数的默认行为只该被运维脚本用（生产端点必须传身份自己的租户）。
    """
    q = _q()
    q.add("t-empty", tenant="")
    q.add("t-acme", tenant="acme")

    assert [i["thread_id"] for i in q.pending(tenant="")] == ["t-empty"], (
        "空串又把别的租户带出来了 —— 这正是那个跨租户泄露口"
    )
    # 显式 None 才是全量（运维视角），且不能因为上面那条把全量能力也弄丢
    assert len(q.pending()) == 2
    assert len(q.pending(tenant=None)) == 2


def test_disabled_queue_is_noop():
    q = _q(enabled=False)
    assert q.add("t1") is False
    assert q.pending() == []


def test_add_rejects_empty_thread_id():
    assert _q().add("") is False


def test_notify_skipped_without_webhook():
    assert _q().notify({"thread_id": "t"}) is False


@pytest.mark.parametrize("fmt,needle", [
    ("wecom", "msgtype"),
    ("feishu", "msg_type"),
    ("generic", "event"),
])
def test_notify_body_shapes(fmt, needle):
    q = _q(notify_format=fmt)
    body = q._build_notify_body({"thread_id": "t1", "score": 30,
                                 "feedback": "偏题", "draft": "草稿" * 100})
    assert needle in body
    assert "t1" in str(body)
    # 草稿在通知里必须被截断（不能把整篇塞进机器人消息）
    assert len(str(body)) < 3000


def test_notify_posts_to_webhook(monkeypatch):
    sent = {}

    class _Resp:
        def raise_for_status(self):
            pass

    def fake_post(url, json=None, timeout=None):
        sent.update(url=url, json=json, timeout=timeout)
        return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)

    q = _q(webhook="https://example.com/hook")
    assert q.notify({"thread_id": "t9", "draft": "d", "score": 1, "feedback": "f"})
    assert sent["url"] == "https://example.com/hook"
    assert sent["json"]["event"] == "review_needed"


def test_notify_failure_is_swallowed(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("网络不通")

    import httpx
    monkeypatch.setattr(httpx, "post", boom)

    assert _q(webhook="https://example.com/hook").notify({"thread_id": "t"}) is False


# ---------------------------------------------------------------------------
# 审计：谁在什么时候做了什么
# ---------------------------------------------------------------------------
# 背景（2026-09-29）：`resolve()` 原先只写 status 与 updated_at —— 而 human-in-the-loop
# 里**唯一需要回答"谁批的"的就是审核动作**。更要命的是 `updated_at` 在 `add()` 的
# 冲突更新里也会被刷新，连"什么时候被人处理掉"都答不出来。
# 少了操作者，复盘只能看到"这条被 accept 了"：无法判断是正常放行、越权还是误点。
# ---------------------------------------------------------------------------
def test_resolve_records_actor_via_note_and_time():
    """解决一条记录时，操作者 / 决定备注 / 处理时刻**都要落下来**。

    删掉 `resolve` 里任何一个审计字段的写入，本用例立刻变红。
    """
    q = _q()
    q.add("t1", draft="草稿", score=20, tenant="acme")

    assert q.resolve("t1", "revised", actor="acme", note="补上引用来源") is True

    rec = q.record("t1")
    assert rec["status"] == "revised"
    assert rec["resolved_by"] == "acme"
    assert rec["resolved_via"] == "session"
    assert rec["resolution_note"] == "补上引用来源"
    assert rec["resolved_at"], "没记处理时刻 —— updated_at 会被 add 刷新，替代不了它"
    # 出队语义不变：处理完就不再出现在待审列表里
    assert q.pending() == []


def test_record_shape_is_identical_before_and_after_resolve():
    """两种存储后端暴露**同一形状**的记录：审计字段在 add 时就要占位。

    若只有 PG 后端有这几列，内存后端会漏 key ⇒ 调用方被迫按后端分支取值，
    "同一组方法、调用方无感"的约定就此破裂，而且只在内存模式下才崩。
    """
    q = _q()
    q.add("t1", tenant="acme")
    rec = q.record("t1")

    for key in ("resolved_by", "resolved_via", "resolution_note", "resolved_at"):
        assert key in rec, f"add 之后记录里缺少审计字段 {key}"
    assert rec["resolved_by"] == "" and rec["resolved_at"] is None

    # 待审列表里的条目形状也一致（列表页要能直接显示"谁处理的"）
    assert {"resolved_by", "resolved_at"} <= set(q.pending()[0])


def test_record_is_none_for_unknown_thread():
    """不存在的 thread 返回 None（调用方据此区分"没这条"与"有这条但没字段"）。"""
    q = _q()
    assert q.record("nope") is None
    assert q.record("") is None


def test_resolve_keeps_working_without_audit_arguments():
    """向后兼容：只传 thread_id 与 status（旧调用写法）依然可用。"""
    q = _q()
    q.add("t1")
    assert q.resolve("t1", "accepted") is True
    assert q.record("t1")["resolved_by"] == ""


def test_audit_log_names_the_actor_but_not_the_content(caplog):
    """审计日志：**要点名操作者与决定，但不落反馈正文**。

    两个方向都要卡住：
      - 不记 actor ⇒ 日志里查不到"谁批的"（审计日志就没有存在意义）；
      - 记了正文 ⇒ 用户内容被转发进第三方日志系统（反馈里常带原文）。

    所以断言的是"有 actor / 有 note 的**长度**"且"**没有** note 的正文"。
    """
    import logging

    q = _q()
    q.add("t1")
    secret = "把第三段里的客户手机号删掉"
    with caplog.at_level(logging.INFO):
        q.resolve("t1", "revised", actor="acme", note=secret)

    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "t1" in line and "revised" in line
    assert "acme" in line, f"日志里没有操作者：{line!r}"
    assert f"note_chars={len(secret)}" in line, f"日志里没有反馈长度：{line!r}"
    assert secret not in line, "反馈正文被写进日志（会被转发到第三方日志系统）"


def test_no_audit_log_when_nothing_was_resolved(caplog):
    """反向用例：没处理到任何记录时不能打审计日志。

    少了它，"把日志写在函数入口"这种错误永远发现不了 —— 审计日志会退化成
    "每个请求一行"，既淹没有效信息，也会让人误以为记录真的被处理过。
    """
    import logging

    q = _q()
    with caplog.at_level(logging.INFO):
        assert q.resolve("does-not-exist", "accepted", actor="acme") is False

    assert not [r for r in caplog.records if "审核动作" in r.getMessage()]


# ---------------------------------------------------------------------------
# 结构性契约：老库补列
# ---------------------------------------------------------------------------
# 内存后端根本不建表，所以"老部署上 resolve 报列不存在"这一类故障，上面所有用例
# **一条都测不到**；CI 里也没有 PG 可用。因此这里用假 cursor 钉住"发了哪些 DDL"。
# ---------------------------------------------------------------------------
class _FakeCursor:
    """只记录 SQL 的假 cursor（把多行 DDL 压成单行便于断言）。"""

    def __init__(self):
        self.sql = []

    def execute(self, query, params=None):
        self.sql.append(" ".join(str(query).split()))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self):
        self.cur = _FakeCursor()

    def cursor(self):
        return self.cur


def test_schema_creates_and_migrates_the_audit_columns():
    """新建表**与**老表补列必须同时覆盖四个审计列。

    两条路径各自的失败模式都很隐蔽：
      - 只改 `CREATE TABLE`（新库对、老库错）⇒ 老部署上线后 `resolve()` 报
        "列不存在"，表现成"审核动作 500"，报错点离原因很远；
      - 只加 `ALTER`（老库对、新库错）⇒ 恰好反过来，只有全新环境才炸。
    所以断言的是"两处都出现"。
    """
    conn = _FakeConn()
    ReviewQueue(dsn="postgres://placeholder")._ensure_schema(conn)
    sql = "\n".join(conn.cur.sql)

    assert "CREATE TABLE IF NOT EXISTS review_queue" in sql
    for col in ("resolved_by", "resolved_via", "resolution_note", "resolved_at"):
        assert col in sql, f"审计列 {col} 完全没进 DDL"
        assert f"ADD COLUMN IF NOT EXISTS {col}" in sql, \
            f"老表补列缺 {col}（CREATE TABLE IF NOT EXISTS 对已存在的表是静默无操作）"
