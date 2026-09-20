"""接口级测试：/health 与 /review 的离线用例。

只覆盖**不需要真实 LLM** 的路径，因此可在 CI 中无网络运行：
- /health 健康检查
- /review 的 400（非法 action / revise 缺 feedback）与 409（会话不在审核态）

需要 `httpx`（FastAPI TestClient 依赖）；缺失时整个模块自动跳过。
"""
import os

import pytest

# 先清掉 DB_URL，避免测试时真的去连 Postgres（自动回退 MemorySaver）
os.environ["DB_URL"] = ""

pytest.importorskip("fastapi.testclient", reason="TestClient 需要 httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_review_unknown_thread_returns_409():
    r = client.post("/review", json={
        "thread_id": "no-such-thread", "action": "accept", "feedback": "",
    })
    assert r.status_code == 409


def test_review_invalid_action_returns_400():
    r = client.post("/review", json={
        "thread_id": "t", "action": "nope", "feedback": "",
    })
    assert r.status_code == 400


def test_review_revise_without_feedback_returns_400():
    r = client.post("/review", json={
        "thread_id": "t", "action": "revise", "feedback": "",
    })
    assert r.status_code == 400
