"""配额与单请求预算的离线测试。

分两段：`check/add`（只读查询 + 记账）与 `claim/settle`（生成路径的两阶段协议）。
后者是修掉 TOCTOU 的那一段，也是本文件里断言最"较真"的部分。
"""
from app.quota import QuotaManager, RequestBudget, _subject_bucket, _tenant_bucket, _today, estimate_tokens


def _bucket_of(qm, tenant, subject, scope):
    """取某一级的桶键 —— 两级口径下断言必须指明"看的是哪一级"。

    「租户级已用多少」与「每人级已用多少」是两个不同的数，只写一个桶名会让
    用例在改动后**静默地看错一级**（而错误的桶读出来永远是 0，断言就变成假绿）。
    """
    for sc, bucket, _limit in qm.levels(tenant, subject, _today()):
        if sc == scope:
            return bucket
    raise AssertionError(f"这一级没有开额度，取不到桶键：{scope}")


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


# ---------------------------------------------------------------------------
# 两级日额度（租户级 + 每人级）：修"同租户内的人互相抢额度"
# ---------------------------------------------------------------------------
def test_a_member_cannot_drain_the_whole_tenant_quota():
    """**核心用例**：租户级额度很大、每人级很小时，一个人打满不能把同租户别人挡在门外。

    改两级之前，额度是整个租户共用一份：一个人先刷满，同租户其余人当天全部 429
    —— 而他们什么都没做错。这是审计里点名的那条（"同租户互抢"）。
    """
    qm = QuotaManager(daily_limit=10 ** 9, per_user_limit=100, request_limit=10)

    for _ in range(10):                      # alice 用光自己的 100
        assert qm.claim("acme", subject="alice").allowed
    assert not qm.claim("acme", subject="alice").allowed, "alice 没有自己的上限"

    # bob 是同一个租户的另一个人：租户总额度还多得是，他必须能继续
    assert qm.claim("acme", subject="bob").allowed, "同租户的别人被 alice 连累了"


def test_tenant_level_still_caps_the_total():
    """每人级很大时，租户级仍然是天花板（防一个租户把全站额度吃光）。"""
    qm = QuotaManager(daily_limit=100, per_user_limit=10 ** 9, request_limit=10)

    assert qm.claim("acme", subject="alice").allowed     # 10
    assert qm.claim("acme", subject="bob").allowed       # 20
    for _ in range(8):                                   # 到 100
        assert qm.claim("acme", subject="carol").allowed
    st = qm.claim("acme", subject="dave")
    assert not st.allowed and st.scope == "tenant", "租户级封顶失效"


def test_tenantless_accounts_do_not_share_one_bucket():
    """**审计点名的那条**：无租户的账号（`tenant == ""`）曾经共用同一个桶，彼此互抢。

    两级之后，每人级把"无租户"的人彼此分开：一个人打满，另一个无租户账号照常。
    """
    qm = QuotaManager(daily_limit=10 ** 9, per_user_limit=30, request_limit=10)

    for _ in range(3):
        assert qm.claim("", subject="u1").allowed
    assert not qm.claim("", subject="u1").allowed
    assert qm.claim("", subject="u2").allowed, "无租户的账号仍然在互相抢额度"


def test_rejected_claim_releases_every_level():
    """被拒时**每一级**都要退回预占。

    只退触发拒绝的那一级，前面那级就永久吃掉一份预占 —— 额度只减不增，
    症状是"额度比真实消耗快得多地用尽"，而且全程没有任何报错。
    """
    # 租户级宽、每人级窄 ⇒ 拒绝一定发生在第二级（每人级）
    qm = QuotaManager(daily_limit=10 ** 9, per_user_limit=30, request_limit=10)
    for _ in range(3):
        qm.claim("acme", subject="alice")
    assert not qm.claim("acme", subject="alice").allowed

    tenant_used = qm.store.get(_bucket_of(qm, "acme", "alice", "tenant"), _today())
    assert tenant_used == 30, (
        f"被拒的请求在租户级留下了预占（已用 {tenant_used}，应为 30）—— 拒绝路径没退全")


def test_settle_touches_both_levels():
    """结算必须把**两级**都改成实际值。

    只改一级的话，另一级的账永远停在"预占"上（那一级会被预占吃满而实际消耗从未
    发生），表现成"配额怎么用都用不完"或"莫名其妙用光了"。
    """
    qm = QuotaManager(daily_limit=10 ** 6, per_user_limit=10 ** 6, request_limit=100)
    st = qm.claim("acme", subject="alice")
    assert st.reserved == 100
    qm.settle("acme", st.reserved, 7, subject="alice")
    assert qm.store.get(_bucket_of(qm, "acme", "alice", "tenant"), _today()) == 7
    assert qm.store.get(_bucket_of(qm, "acme", "alice", "subject"), _today()) == 7


def test_check_reports_which_level_would_block():
    """只读检查也要两级都看，并说清是**哪一级**会拦 —— 两级口径下"还剩多少"由更紧的那级决定。"""
    qm = QuotaManager(daily_limit=1000, per_user_limit=20)
    qm.add("acme", 15, subject="alice")
    st = qm.check("acme", subject="alice")
    assert st.allowed and st.limit == 20, f"没按每人级报告（limit={st.limit}）"
    qm.add("acme", 5, subject="alice")
    st = qm.check("acme", subject="alice")
    assert not st.allowed and st.scope == "subject"


def test_bucket_keys_cannot_collide_with_each_other():
    """两级的桶键必须**永不相撞**。

    撞键的后果不是报错，而是"某人的额度被别人的用量吃掉"（查配置一切正常）——
    这正是最难查的一类故障。不撞的依据是**两个前缀的第 6 个字符不同**
    （租户级 `quota:`、每人级 `quota@user:`），与租户名/用户名长什么样无关。
    所以这里把该依据本身也钉住：它依赖"租户名与用户名都禁用 `:`"。
    """
    from app.accounts import _FORBIDDEN_IN_NAME

    assert ":" in _FORBIDDEN_IN_NAME, (
        "名字里不再禁用 `:` ⇒ 桶键的两段式/三段式结构可能被撑成任意形状，"
        "『两级桶键不可能相撞』这条论证失效")

    today = "2026-01-01"
    # 拿"最像用户桶前缀"的合法租户名试：`user` 会让租户桶变成 `quota:user:<日期>`，
    # 而用户桶是 `quota@user:<用户>:<日期>` —— 第 6 个字符不同，不可能相等。
    for tenant in ("user", "user@alice", "acme", "", "quota"):
        for subject in ("alice", "bob", ""):
            assert _tenant_bucket(tenant, today) != _subject_bucket(subject, today), (
                f"桶键相撞：tenant={tenant!r} subject={subject!r}")

    # 形状反向锁：租户键必须是三段（`quota:<租户>:<日期>`）—— 它**刻意沿用改造前
    # 的键名**，改了就等于升级当天把已累计的用量清零（额度被静默放宽）。
    assert _tenant_bucket("acme", today) == f"quota:acme:{today}"
    assert _subject_bucket("alice", today) == f"quota@user:alice:{today}"


def test_the_per_user_bucket_omits_the_tenant_on_purpose():
    """每人级桶键**刻意不带租户名**，理由必须仍然成立：用户名是主键 ⇒ 全局唯一。

    带租户名的话，"管理员把某人移出租户再移回来"会把他当天的用量清零 ——
    等于给了"找管理员刷额度"这条路。不带租户名则额度跟着人走，刷不出来。
    这条不变量依赖"用户名在 users 表里是主键"，所以顺带把它钉住。
    """
    from app.accounts import AccountStore
    from app.quota import _subject_bucket

    assert _subject_bucket("alice", "2026-01-01") == _subject_bucket("alice", "2026-01-01")
    ddl = "\n".join(AccountStore.schema_statements())
    users_ddl = ddl.split("CREATE TABLE IF NOT EXISTS users (")[1].split(")")[0]
    assert "username" in users_ddl and "PRIMARY KEY" in users_ddl, (
        "users 表的主键结构变了 —— 每人级桶键不带租户的前提（用户名全局唯一）不再成立")


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
