"""配额与单请求预算的离线测试。

分两段：`check/add`（只读查询 + 记账）与 `claim/settle`（生成路径的两阶段协议）。
后者是修掉 TOCTOU 的那一段，也是本文件里断言最"较真"的部分。
"""
from app.quota import QuotaManager, RequestBudget, estimate_tokens


def test_daily_quota_blocks_when_exhausted():
    qm = QuotaManager(daily_limit=10)
    assert qm.check("t1").allowed
    qm.add("t1", 10)
    st = qm.check("t1")
    assert not st.allowed
    assert st.used == 10 and st.limit == 10
    assert st.reset_after >= 1


def test_daily_quota_is_per_tenant():
    qm = QuotaManager(daily_limit=5)
    qm.add("a", 5)
    assert not qm.check("a").allowed
    assert qm.check("b").allowed          # 别的租户不受影响


def test_zero_limit_means_unlimited():
    qm = QuotaManager(daily_limit=0)
    qm.add("t", 10 ** 6)
    assert qm.check("t").allowed


def test_disabled_quota_does_not_count():
    qm = QuotaManager(daily_limit=1, enabled=False)
    qm.add("t", 999)
    assert qm.check("t").allowed


def test_quota_store_failure_fails_open():
    qm = QuotaManager(daily_limit=1)

    class _Boom:
        def get(self, *a, **kw):
            raise RuntimeError("redis down")

        def add(self, *a, **kw):
            raise RuntimeError("redis down")

    qm.store = _Boom()
    assert qm.check("t").allowed


# ---------------------------------------------------------------------------
# claim / settle：把"判定 + 记账"合成一次原子操作（修 TOCTOU）
# ---------------------------------------------------------------------------
def test_claim_counts_the_reservation_immediately():
    """预占必须**当场**进入计数，并且返回的 used 不含自己那份。

    `used` 不含自己的预占是为了让日志/429 文案读起来是"别人已经用了多少"，
    否则每次拒绝都会看到一个凭空多出 request_limit 的数字。
    """
    qm = QuotaManager(daily_limit=1000, request_limit=400)
    st = qm.claim("t")
    assert st.allowed and st.reserved == 400
    assert st.used == 0, f"used 应为预占前的已用量，实际 {st.used}"
    assert qm.check("t").used == 400, "预占没有当场计入 —— 那么它压根没起作用"


def test_concurrent_requests_cannot_all_pass_on_the_last_slot():
    """**核心用例**：额度只够两次时，第三个在途请求必须被拒。

    这就是 TOCTOU 的现场：旧写法（请求头 check、请求尾 add）下三次 check 都会
    读到同一个"已用 0"，于是**三个请求全部放行**，最后各自 add —— 日额度被超掉
    一整份 request_limit。并发越高超得越多。
    """
    qm = QuotaManager(daily_limit=1000, request_limit=400)
    verdicts = [qm.claim("t").allowed for _ in range(3)]
    assert verdicts == [True, True, False], f"并发全被放行，TOCTOU 回来了：{verdicts}"
    assert qm.check("t").used == 800


def test_rejected_claim_does_not_consume_quota():
    """被拒的请求必须**退回**预占。

    不退的话，每次被拒都会永久吃掉一份 request_limit —— 额度只减不增，
    很快连额度充足的租户也会被拒（而且日志上只看到"超额"，看不出是自己吃掉的）。
    """
    qm = QuotaManager(daily_limit=1000, request_limit=400)
    qm.claim("t")
    qm.claim("t")
    before = qm.check("t").used
    assert not qm.claim("t").allowed
    assert qm.check("t").used == before, "被拒的请求把预占留下了"


def test_settle_refunds_the_unused_reservation():
    """结算必须把"预占 - 实际"退回来。

    预占是上界、实际通常更小（模型提前结束、断连中止）。不退还的话每次请求都按
    预占满额计费 —— 配额比真实消耗快几倍地耗尽，而且没有任何报错。
    """
    qm = QuotaManager(daily_limit=1000, request_limit=400)
    st = qm.claim("t")
    assert qm.settle("t", st.reserved, 120) == 120
    assert qm.check("t").used == 120


def test_settle_with_zero_output_refunds_everything():
    """实际消耗为 0（请求在生成前就断了）时，预占要全额退还。"""
    qm = QuotaManager(daily_limit=1000, request_limit=400)
    st = qm.claim("t")
    qm.settle("t", st.reserved, 0)
    assert qm.check("t").used == 0


def test_settle_without_reservation_still_counts():
    """没走预占的路径（如配额刚打开）也要能记账。"""
    qm = QuotaManager(daily_limit=100)
    assert qm.settle("t", 0, 5) == 5
    assert qm.check("t").used == 5


def test_add_accepts_negative_delta():
    """`add` 必须允许负数 —— 结算的退还全走这条路。

    早期写法是 `if tokens <= 0: return 0`，于是退还被静默忽略：`settle` 变成空操作。
    """
    qm = QuotaManager(daily_limit=100)
    qm.add("t", 50)
    assert qm.add("t", -20) == 30
    assert qm.check("t").used == 30


def test_memory_store_never_goes_negative():
    """跨天退还（预占在昨天、结算在今天）会把差额打到今天的键上，必须截到 0。

    不截断的话今天的用量是负数 = 白送一段额度。
    """
    from app.quota import _MemoryStore

    st = _MemoryStore()
    st.add("t", "2026-01-01", 5)
    assert st.add("t", "2026-01-02", -50) == 0


def test_reservation_is_request_limit_or_one():
    """预占用单请求上限（消耗的硬上界）；没配时退回 1。"""
    assert QuotaManager(daily_limit=100, request_limit=30).reservation() == 30
    assert QuotaManager(daily_limit=100, request_limit=0).reservation() == 1


def test_claim_is_unlimited_without_daily_limit():
    """日配额为 0（不限）时 claim 恒放行且不预占。"""
    qm = QuotaManager(daily_limit=0, request_limit=400)
    st = qm.claim("t")
    assert st.allowed and st.reserved == 0


def test_claim_falls_back_to_local_when_store_breaks():
    """共享后端故障时退到进程内预占，而不是放行或不计数。"""
    qm = QuotaManager(daily_limit=10)

    class _Boom:
        def add(self, *a, **kw):
            raise RuntimeError("redis down")

    qm.store = _Boom()
    st = qm.claim("t")
    assert st.allowed and st.reserved == 1


def test_misconfigured_quota_logs_warning(caplog):
    """单请求上限 ≥ 日配额会让**每个**请求都被拒 —— 必须留下告警。

    这种错配的表现是"全站 429"，排查者会先去怀疑鉴权/网络，而不是两个数字的大小关系。
    """
    import logging

    with caplog.at_level(logging.WARNING):
        QuotaManager(daily_limit=1000, request_limit=1000)
    assert any("不小于日配额" in r.getMessage() for r in caplog.records)


def test_request_budget_stops_after_limit():
    # ⚠️ 不要用字符数假设预算口径：装了 tiktoken 时 "abc" 是 1 token，
    #    没装时回退成 len=3。用 estimate_tokens 自算，两种后端都成立。
    unit = estimate_tokens("abc")
    assert unit >= 1
    b = RequestBudget(limit=unit)      # 预算恰好等于第一段
    assert b.add("abc")                # 用光预算（== limit 仍算未超）
    assert not b.exceeded
    assert not b.add("de")             # 再追加即超预算
    assert b.exceeded


def test_request_budget_zero_is_unlimited():
    b = RequestBudget(limit=0)
    for _ in range(100):
        assert b.add("x" * 1000)
    assert b.used == 0 and not b.exceeded


def test_estimate_tokens_handles_empty():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0
    assert estimate_tokens("梧州六堡茶") >= 1
