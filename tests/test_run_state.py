"""运行现场（官方 ``RunState``）的存取：``app/runs.RunStore``。

这份数据回答一个很窄的问题：**"这一轮能不能接着跑？"**
它只有"运行中产生了 interruption"时才有 —— 也就是模型请求了一个需要审批的工具。
正常跑完的轮次没有现场，也不该有。

三个必须钉住的语义：
  · 内存后端与 PG 后端**同一组方法**（调用方无感），否则本地/测试与线上走两条路；
  · 写库失败**降级内存**而不是抛错（"这次能不能接着跑"比"存在哪个介质上"重要）；
  · `drop()` 之后 `load()` 必须回 None —— 现场**只能用一次**（官方那个状态消费掉
    批准结果之后就作废；不删的症状是"批了一次，同一个动作执行了两遍"，且不报错）。
"""
from __future__ import annotations

from app.runs import RunStore, build_run_store


def test_save_load_roundtrip_in_memory():
    store = RunStore(dsn="")

    assert store.save("t1", snapshot='{"x":1}', draft="草稿", reason="needs_review",
                      tenant="acme") is True

    got = store.load("t1")
    assert got is not None
    assert got["snapshot"] == '{"x":1}'
    assert got["draft"] == "草稿"
    assert got["reason"] == "needs_review"
    assert got["tenant"] == "acme"


def test_load_missing_returns_none():
    assert RunStore(dsn="").load("nope") is None


def test_drop_makes_the_site_unusable():
    """现场只能用一次：``drop`` 之后必须取不到，否则同一个动作会被恢复两遍。"""
    store = RunStore(dsn="")
    store.save("t2", snapshot='{"x":1}')

    assert store.drop("t2") is True
    assert store.load("t2") is None
    # 再删一次是幂等的（不是异常）
    assert store.drop("t2") is False


def test_save_overwrites_previous_site():
    """一条会话同时只可能有一轮等着被恢复 ⇒ 后写的覆盖先写的。"""
    store = RunStore(dsn="")
    store.save("t3", snapshot='{"v":1}', draft="旧")
    store.save("t3", snapshot='{"v":2}', draft="新")

    assert store.load("t3")["draft"] == "新"


def test_empty_snapshot_is_not_stored():
    """空快照存下去 = 一个"有现场但恢复不了"的幽灵，之后每次 resume 都会炸。"""
    store = RunStore(dsn="")
    assert store.save("t4", snapshot="") is False
    assert store.load("t4") is None


def test_disabled_store_is_a_black_hole():
    """关掉时不落库也不报错 —— 调用方据此照常交付（judge 见 `_hold_for_review`）。"""
    store = RunStore(dsn="", enabled=False)
    assert store.save("t5", snapshot='{"x":1}') is False
    assert store.load("t5") is None
    assert store.drop("t5") is False


def test_write_failure_falls_back_to_memory(monkeypatch):
    """库挂了也**不能**让这一轮丢掉现场：降级内存 + 告警，仍然返回"存住了"。

    反例的后果：`save` 抛错 ⇒ `_hold_for_review` 走失败分支 ⇒ 用户既拿不到正文
    （它是待审内容），审核台也看不到记录（没入队/没现场）—— 内容凭空消失。
    """
    store = RunStore(dsn="postgresql://boom/db")

    def _boom():
        raise RuntimeError("库连不上")

    monkeypatch.setattr(store, "_connect", _boom)

    assert store.save("t6", snapshot='{"x":1}', draft="草稿") is True
    assert store.load("t6")["draft"] == "草稿"


def test_build_run_store_follows_config(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DB_URL", "postgresql://h/db")
    store = build_run_store()
    assert store.dsn == "postgresql://h/db"
    assert store.backend == "memory", "表还没建时后端名仍是 memory（首次写入才转 postgres）"
