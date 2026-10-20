"""会话状态查询（``GET /v1/sessions/{id}``）与续跑（``POST /v1/chat/resume``）。

这两条端点回答的是同一个问题："这条会话现在进行到哪了？"

答案现在由**三份数据**拼出来，各回答一半，缺一份就会给出错误结论：

* 官方会话（``session.load_items``）—— 这条会话说过什么（对话历史的唯一来源）；
* 运行现场（``run_store``，官方 ``RunState``）—— 有没有"跑到一半停下来等审批"
  的那一轮，以及能不能接着跑；
* 待审队列 —— ``manual`` 那类（跑偏 / 护栏拦下 / 没产出）运行**已经结束**，
  只有这份记录，没有现场。

⚠️ 图退役之后不再有"检查点"这层状态。所以 ``status`` 里没有 ``incomplete``：
官方只在 interruption 时提供可恢复的现场，客户端断流导致的"跑了一半"没有现场，
``/v1/chat/resume`` 会如实回 409 而不是假装能续（假装的做法是**再跑一轮**，
会把已经发出去的 token 重发一遍、把已经落盘的资产再做一遍）。
"""
import os

import pytest

os.environ["DB_URL"] = ""          # 不要连真库：这些用例只测判定与状态映射

pytest.importorskip("fastapi.testclient", reason="TestClient 需要 httpx")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as M  # noqa: E402
from app import metrics  # noqa: E402
from app.ratelimit import RateLimiter  # noqa: E402
from app.review_queue import ReviewQueue  # noqa: E402
from app.runs import RunStore  # noqa: E402
from tests.auth_util import auth_headers  # noqa: E402

client = TestClient(M.app, headers=auth_headers())


@pytest.fixture(autouse=True)
def _loose_gates(monkeypatch):
    """放开限流与队列，避免用例之间互相挤占（本文件不测这两个闸门）。"""
    monkeypatch.setattr(M, "_LIMITER", RateLimiter(per_minute=100000))
    monkeypatch.setattr(M, "run_store", RunStore(dsn=""))
    monkeypatch.setattr(M, "_REVIEW_QUEUE", ReviewQueue(dsn=""))
    metrics.reset()


# ---------------------------------------------------------------------------
# 纯函数：三种状态
# ---------------------------------------------------------------------------
def test_view_unknown_when_nothing_found():
    """什么都没有 = 从未开始（或已被清理）。"""
    v = M._session_view("t", held=None, record=None, messages=[])
    assert v["status"] == "unknown"
    assert v["resumable"] is False
    assert v["pending_review"] is None


def test_view_completed_when_history_exists():
    """有历史 = 这一轮已经收尾（不需要人做任何事）。"""
    v = M._session_view("t", messages=[{"role": "user", "content": "hi"}])
    assert v["status"] == "completed"
    assert v["messages"] == [{"role": "user", "content": "hi"}]


def test_view_awaiting_review_from_queue_record():
    """``manual`` 那类：运行已结束，队列是它唯一的状态来源。"""
    rec = {"status": "pending", "draft": "草稿", "feedback": "太短"}
    v = M._session_view("t", record=rec)
    assert v["status"] == "awaiting_review"
    assert v["pending_review"]["draft"] == "草稿"
    assert v["pending_review"]["feedback"] == "太短"
    # ⚠️ agent 链路不产 0-100 分 ⇒ None。给 0 会让审核台显示"AI 判了 0 分"，
    # 而真相是"没有人评过"。
    assert v["pending_review"]["score"] is None
    # 没有现场 ⇒ 谈不上"接着跑"（续跑等于新起一轮，不是这个端点的语义）
    assert v["resumable"] is False


def test_view_awaiting_review_from_held_run_state():
    """模型在交付时声明要人审：运行停在工具审批上，现场在 ``run_store``。"""
    held = {"snapshot": "{}", "draft": "待批正文"}
    v = M._session_view("t", held=held)
    assert v["status"] == "awaiting_review"
    assert v["resumable"] is True, "有现场就必须告诉客户端'可以接着跑'"
    assert v["pending_review"]["draft"] == "待批正文"


def test_view_prefers_held_draft_over_queue_draft():
    """两份草稿不一致时取**现场**那份：它是被打断的那次运行真正要交的正文。"""
    v = M._session_view("t", held={"snapshot": "{}", "draft": "现场里的"},
                        record={"status": "pending", "draft": "队列里的"})
    assert v["pending_review"]["draft"] == "现场里的"


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
def test_snapshot_unknown_thread_404():
    r = client.get("/v1/sessions/no-such-session-xyz")
    assert r.status_code == 404


def test_snapshot_shows_queue_draft_as_awaiting_review(monkeypatch):
    # ⚠️ 先把属主登记上。`GET /v1/sessions/{id}` 走的是 `_assert_session_readable`
    # （只读、不写登记表，查不到属主一律 404），而生产链路上任何一条会话都先经过
    # `/v1/chat` 的 `_preflight` 认领过 —— 待审记录只可能在认领之后产生。
    # 测试若跳过这一步，得到的是"属主未知"的 404，而不是本用例要验的状态映射，
    # 两条 404 长得一样、原因完全不同。
    M._SESSIONS.claim("view-pending", "", "tester")

    q = ReviewQueue(dsn="")
    q.add("view-pending", draft="待审草稿", feedback="补来源", tenant="")
    monkeypatch.setattr(M, "_REVIEW_QUEUE", q)

    r = client.get("/v1/sessions/view-pending")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "awaiting_review"
    assert body["pending_review"]["draft"] == "待审草稿"


def test_resume_409_when_nothing_to_resume():
    """没有现场 ⇒ 如实回 409，**不**假装能续。

    假装能续的做法是再跑一轮：已经发出去的 token 会重发一遍、已经落盘的资产会
    再做一遍，而且客户端看到的是一段"重来"的正文。
    """
    r = client.post("/v1/chat/resume", json={"thread_id": "resume-none"})
    assert r.status_code == 409
    assert "Idempotency-Key" in r.text, "409 必须给出可行的替代方案"


def test_resume_409_points_to_review_when_held(monkeypatch):
    """有现场 ⇒ 那是**等人的决定**，不是"可以自己继续"，要指向 /v1/review。"""
    store = RunStore(dsn="")
    store.save("resume-held", snapshot="{}", draft="草稿")
    monkeypatch.setattr(M, "run_store", store)

    r = client.post("/v1/chat/resume", json={"thread_id": "resume-held"})
    assert r.status_code == 409
    assert "v1/review" in r.text
