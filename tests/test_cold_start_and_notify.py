"""冷启动竞态与通知通道的守卫。

这两类问题都只在**特定时序**下出现，功能上完全正常：

  - **冷启动并发建表**：多个请求同时第一次用到表 ⇒ 同时执行
    `CREATE TABLE IF NOT EXISTS`。PG 在这条语句上有真实竞态（并发插入
    `pg_type` 元组撞唯一索引），报错看起来像"表坏了"。平时不出现，
    只在"流量打进来时刚好没建过表"出现 —— 也就是**每次发布之后**。
  - **通知无界线程 / 关机丢通知**：webhook 慢时线程数随待审量增长（症状是
    "进程越来越慢"，看不出跟通知有关）；关机不排空则丢掉发布前那批待审提醒
    （工单还在库里，只是没人被通知到）。
"""

from __future__ import annotations

import ast
import queue
import re
import threading
import time
from pathlib import Path

import pytest

from app.accounts import AccountStore
from app.review_queue import ReviewQueue
from app.sessions import SessionRegistry

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 一、冷启动：建表只允许被真正执行一次
# ---------------------------------------------------------------------------

class _Recorder:
    """假游标：只记录执行过哪些语句（第一条关键字），不做任何 IO。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.sql: list[str] = []

    def execute(self, sql, *args):
        head = " ".join(str(sql).split()[:3]).upper()
        with self.lock:
            self.sql.append(head)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def count(self, prefix: str) -> int:
        with self.lock:
            return sum(1 for s in self.sql if s.startswith(prefix))


class _FakeConn:
    def __init__(self, rec: _Recorder):
        self._rec = rec

    def cursor(self):
        return self._rec


def _race(fn, n: int = 8) -> list[BaseException]:
    """让 n 个线程尽量**同时**进入同一段代码，返回捕获到的异常。"""
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _run():
        try:
            barrier.wait(timeout=5)
            fn()
        except BaseException as e:          # noqa: BLE001  测试要看到全部
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=_run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    return errors


def test_session_schema_bootstrap_runs_once_under_concurrency():
    """`SessionRegistry` 的建表语句只允许被执行一次。

    并发下重复执行 `CREATE TABLE IF NOT EXISTS` 会撞 PG 的元组唯一索引，
    报错看起来像"表建坏了"；而这个窗口在改用连接池之后更宽（多个工作线程可同时进来）。
    """
    rec = _Recorder()
    reg = SessionRegistry(dsn="postgresql://fake", enabled=True)
    errors = _race(lambda: reg._ensure_schema(_FakeConn(rec)))

    assert not errors, f"并发建表报错了：{errors}"
    assert rec.count("CREATE TABLE") == 1, (
        f"建表被执行了 {rec.count('CREATE TABLE')} 次（应为 1）：{rec.sql}")


def test_review_queue_schema_bootstrap_runs_once_under_concurrency():
    rec = _Recorder()
    rq = ReviewQueue(dsn="postgresql://fake", enabled=True)
    errors = _race(lambda: rq._ensure_schema(_FakeConn(rec)))

    assert not errors, f"并发建表报错了：{errors}"
    assert rec.count("CREATE TABLE") == 1, (
        f"建表被执行了 {rec.count('CREATE TABLE')} 次（应为 1）：{rec.sql}")


def test_accounts_schema_bootstrap_runs_once_under_concurrency():
    """账号表更要紧：建表失败会让登录直接 500。"""
    rec = _Recorder()
    store = AccountStore(dsn="postgresql://fake")
    errors = _race(lambda: store._ensure_schema(_FakeConn(rec)))

    assert not errors, f"并发建表报错了：{errors}"
    expected = len(store.schema_statements())
    assert len(rec.sql) == expected, (
        f"DDL 被执行了 {len(rec.sql)} 条（应为 {expected} 条，即只跑一轮）：{rec.sql}")


def test_schema_ready_flag_is_set_under_the_lock():
    """反向锁：三个存储类都必须**同时**持有 `_schema_lock` 与 `_schema_ready`。

    只加锁不改判断、或只加标志不加锁，守卫测试都会因为"并发下恰好只跑一次"而
    偶然通过（时序不保证）。所以这里直接钉住结构：
      * 类里存在 `_schema_lock`；
      * `_ensure_schema` 体内出现 `with self._schema_lock` 与两次 `_schema_ready` 判断
        （双检），且置位语句在 `with` 块**内部**。
    """
    targets = {
        "app/sessions.py": "SessionRegistry",
        "app/accounts.py": "AccountStore",
        "app/review_queue.py": "ReviewQueue",
    }
    for rel, cls in targets.items():
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)
        klass = next((n for n in ast.walk(tree)
                      if isinstance(n, ast.ClassDef) and n.name == cls), None)
        assert klass is not None, f"{rel} 里找不到类 {cls}"

        init = next((n for n in klass.body
                     if isinstance(n, ast.FunctionDef) and n.name == "__init__"), None)
        assert init is not None
        init_attrs = {t.attr for n in ast.walk(init) if isinstance(n, ast.Assign)
                      for t in n.targets if isinstance(t, ast.Attribute)}
        assert "_schema_lock" in init_attrs, (
            f"{cls}.__init__ 没有创建 `_schema_lock` —— 冷启动并发建表会撞 PG 元组唯一索引")

        fn = next((n for n in klass.body
                   if isinstance(n, ast.FunctionDef) and n.name == "_ensure_schema"), None)
        assert fn is not None
        src = ast.get_source_segment((ROOT / rel).read_text(encoding="utf-8"), fn) or ""
        assert "with self._schema_lock" in src, (
            f"{cls}._ensure_schema 没有持 `_schema_lock`")
        assert src.count("if self._schema_ready") >= 2, (
            f"{cls}._ensure_schema 缺少双重检查（等锁期间可能已被别人建好）")
        # 置位必须在 with 块里：放在锁外意味着另一个线程已判过 False 并排上队
        after_lock = src.split("with self._schema_lock", 1)[1]
        assert "self._schema_ready = True" in after_lock.split("def ", 1)[0], (
            f"{cls}._ensure_schema 的 `_schema_ready = True` 不在锁内")


# ---------------------------------------------------------------------------
# 二、通知通道：线程有界、积压有上限、关机排空
# ---------------------------------------------------------------------------

def test_no_webhook_means_no_thread_at_all():
    rq = ReviewQueue(webhook="")
    rq._notify_async({"thread_id": "t-1"})
    assert rq._notify_threads == [], "没配 webhook 却起了通知线程"


def test_notify_thread_count_is_fixed_not_per_item():
    """**核心回归**：线程数固定，不随通知条数增长（原实现是每条一个线程）。

    两条断言分工不同，缺一条都会漏：
      * `_notify_threads`：钉住**自己登记**的工作线程数；
      * 进程线程总数增长：钉住"**没有**另外偷偷起线程" ——
        只看前者的话，把投递改回 `Thread(...).start()` 而保留登记表，
        前者依然通过，而线程已经无界了。
    """
    gate = threading.Event()
    rq = ReviewQueue(webhook="http://notify.invalid", notify_workers=2)
    rq.notify = lambda item: gate.wait(timeout=5)      # 全部挂住，便于数线程

    before = threading.active_count()
    for i in range(10):
        rq._notify_async({"thread_id": f"t-{i}"})

    deadline = time.monotonic() + 2
    while (threading.active_count() - before < rq.notify_workers
           and time.monotonic() < deadline):
        time.sleep(0.01)
    time.sleep(0.1)                                    # 给"每条一线程"的实现足够时间暴露
    growth = threading.active_count() - before
    registered = len(rq._notify_threads)               # ⚠️ 必须在 close() 之前读：
    gate.set()                                        #    close 会把登记表清空
    rq.close(timeout=2)

    assert registered == rq.notify_workers, (
        f"登记的通知线程数是 {registered}（应为 {rq.notify_workers}）")
    assert growth <= rq.notify_workers, (
        f"10 条通知让进程多出 {growth} 个线程（上限应为 {rq.notify_workers}）—— "
        "webhook 慢时会把线程/内存吃光，而症状只是「进程越来越慢」")


def test_notify_queue_drops_instead_of_growing_unboundedly():
    """队列满 ⇒ 丢弃并计数，而不是无限排队。

    ⚠️ 这里预置一个**没有工作线程**的队列，让行为完全确定：
    否则 worker 会边取边走，队列是否满取决于调度，用例会间歇性变红。
    """
    rq = ReviewQueue(webhook="http://notify.invalid", notify_queue_max=2)
    rq._notify_q = queue.Queue(maxsize=2)       # 预置：无 worker 消费

    rq._notify_async({"thread_id": "a"})
    rq._notify_async({"thread_id": "b"})
    assert rq.notify_dropped == 0
    rq._notify_async({"thread_id": "c"})        # 满 ⇒ 丢弃
    assert rq.notify_dropped == 1, "队列满时没有丢弃 —— 内存会被积压的通知吃掉"


def test_close_waits_for_pending_notifications():
    """`close()` 返回时，已投递的通知必须已经发完。

    ⚠️ 用例形态是被变异验证逼出来的：最初写成"gate 一放、3 条秒发完"，
    结果**删掉排空逻辑也照样通过** —— 因为哨兵排在队尾，工作线程顺手就处理完了。
    真正要覆盖的是"在途总量 > 工作线程收尾时的固定 join 预算"：
    这里 4 条 × 0.3s = 1.2s，明显超过收尾 join 的 0.5s，
    只有真的把"在途归零"等到了才会全发出去。
    """
    sent: list[str] = []
    rq = ReviewQueue(webhook="http://notify.invalid", notify_workers=1)

    def _slow(item):
        time.sleep(0.3)
        sent.append(item["thread_id"])
        return True

    rq.notify = _slow
    for i in range(4):
        rq._notify_async({"thread_id": f"t-{i}"})
    rq.close(timeout=10)

    assert sorted(sent) == ["t-0", "t-1", "t-2", "t-3"], (
        f"close() 返回时还有通知没发出去（只发出 {sent}）—— "
        "发布时排队中的待审提醒会被静默丢弃")


def test_close_is_idempotent_and_safe_without_webhook():
    rq = ReviewQueue(webhook="")
    rq.close()
    rq.close()
    assert rq._notify_q is None


def test_close_gives_up_instead_of_hanging_when_the_webhook_is_dead():
    """排空要有**上限**：webhook 死了不能把关机拖住（发布会被卡住）。"""
    rq = ReviewQueue(webhook="http://notify.invalid", notify_workers=1)
    gate = threading.Event()
    rq.notify = lambda item: gate.wait(timeout=10)      # 永不返回

    rq._notify_async({"thread_id": "t-0"})
    rq._notify_async({"thread_id": "t-1"})
    t0 = time.monotonic()
    rq.close(timeout=0.5)
    elapsed = time.monotonic() - t0

    gate.set()
    assert elapsed < 5, f"关机会被卡住的 webhook 拖住 {elapsed:.1f}s"


# ---------------------------------------------------------------------------
# 三、反向锁：关机路径必须真的调用 close
# ---------------------------------------------------------------------------

def test_lifespan_drains_the_review_notify_queue():
    """关机路径必须调用 `_REVIEW_QUEUE.close`。

    "少一行调用"不会有任何报错，只会表现为"发布时偶发丢几条待审提醒" ——
    审核台看不到，工单却真的没被通知到。所以只能静态钉住。
    """
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename="app/main.py")
    lifespan = next((n for n in ast.walk(tree)
                     if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan"), None)
    assert lifespan is not None, "main.py 里找不到 lifespan"

    # ⚠️ 形状必须是"属性引用"而不是"调用"：关机路径上它长这样
    #     `await _off(_REVIEW_QUEUE.close, 5.0)` —— 传的是**可调用对象**
    #     （同步 IO 必须挪出事件循环，见 tests/test_event_loop_offload.py）。
    #     找 `Call` 会一无所获，得到一条永远为红的守卫。
    refs = [
        node for node in ast.walk(lifespan)
        if isinstance(node, ast.Attribute) and node.attr == "close"
        and isinstance(node.value, ast.Name) and node.value.id == "_REVIEW_QUEUE"
    ]
    assert refs, (
        "lifespan 的收尾里没有引用 `_REVIEW_QUEUE.close` —— "
        "关机时排队中的待审通知会被静默丢弃"
    )


@pytest.mark.parametrize("key", ["REVIEW_NOTIFY_WORKERS", "REVIEW_NOTIFY_QUEUE_MAX"])
def test_notify_knobs_are_documented_in_env_example(key):
    """新增的通知开关必须出现在 `.env.example`（否则运维无从发现）。"""
    assert re.search(rf"^#?\s*{key}=", (ROOT / ".env.example").read_text(encoding="utf-8"),
                     flags=re.M), f".env.example 里没有 {key}"
