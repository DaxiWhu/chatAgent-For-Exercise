"""人工审核队列 + 通知：让"需要人看"的会话不再只存在于某个正在断开的连接里。

解决什么问题
-----------
现在的流程是：图跑到 `human_review` 就 interrupt 挂起，`/chat` 的 SSE 流下发一个
`need_review` 事件。**如果那一刻没有客户端在线**（用户关了页面、请求超时断开），
这条待审内容就只留在 checkpoint 里，**没有任何人知道它需要处理** ——
在真实业务里等于"工单丢了"。

这一层做两件事：
1. **落队列**：有待审内容时写一条 pending 记录（PG 表 `review_queue`，无库则内存），
   带租户、评分、草稿摘要、时间。可以被列举、被统计、被巡检。
2. **发通知**：把待审内容推给 `REVIEW_NOTIFY_WEBHOOK`（企微/飞书机器人/自建网关），
   失败只记日志、**不阻塞也不影响对话流**（best-effort）。

落库还是内存
----------
配了 `DB_URL` 就落 PG（跨重启、多实例可见）；否则内存（单实例、重启丢）。
两种实现暴露同一组方法，调用方无感。**写库失败自动降级为内存**并告警 ——
通知能力比存储介质重要。

审计：谁在什么时候做了什么（2026-09-29 补）
----------------------------------------
`resolve()` 原先只写状态与 `updated_at`。但 human-in-the-loop 里**唯一需要回答
"谁批的"的就是审核动作**，而 `updated_at` 在 `add()` 的冲突更新里也会被刷新，
连"什么时候被人处理掉"都答不出来。现在每次 `resolve` 都写
`resolved_by`（租户）/ `resolved_via`（固定为 `session`）/ `resolution_note`（人工反馈）/
`resolved_at`，并打一条 INFO 审计日志。读取走 `record(thread_id)`。

`resolved_via` 记录这条决定是经**哪种凭证**作出的。⚠️ 2026-10-05 起这个区分**已经没有
第二个可能值**：外部身份源（`oidc`）随 OIDC 整体删除而消失，原 `script-key` /
`anonymous` 两个取值更早随共享密钥与匿名兜底一起删除 ⇒ 唯一剩下的就是本服务会话
`session`。列保留、新记录固定写 `_RESOLVED_VIA`。之所以不再有"租户是客户端自报的、
需要额外打折扣"的情况，正是这些非可信凭证档消失的直接结果。
"""

import json
import logging
import queue
import threading
import time
from datetime import datetime

from . import config, metrics


def _now_iso() -> str:
    """当前本地时间 ISO 串（落库/通知都用它，便于人读）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _waited_seconds(created_at) -> float | None:
    """从 `_now_iso()` 写下的串算到现在的秒数；解析不了就返回 None（不上报）。

    与 PG 分支的 `EXTRACT(EPOCH FROM now() - created_at)` 是**同一个量**的两种
    取法：配了 DSN 时由数据库算（权威、不受应用侧时钟影响），内存回退路径只能
    在这里自己算。两边都不该因为"算不出来"而上报一个 0 —— 那会把"没测到"
    混进"等待时间为零"，正好污染最该被看见的长尾。
    """
    try:
        t0 = datetime.strptime(str(created_at), "%Y-%m-%dT%H:%M:%S")
    except Exception:  # noqa: BLE001
        return None
    return max(0.0, (datetime.now() - t0).total_seconds())


def _clip(text, n=500) -> str:
    """把草稿截断到 n 字符（通知内容不宜太长，队列里也只存摘要）。"""
    s = (text or "").strip()
    return s if len(s) <= n else s[:n] + "…"


#: 审核留痕里 `resolved_via` 列写什么。
#: 这一列曾经记录"操作者是从哪种凭证上来的"（会话令牌 / 外部 IdP 令牌）。外部身份源
#: 2026-10-05 删除后只剩会话令牌一种可能，所以新记录**固定**写这个值。列本身不删：
#: 历史留痕要仍然可读，而删列要走迁移、丢掉的旧数据补不回来。
_RESOLVED_VIA = "session"


class ReviewQueue:
    """待审队列：add / resolve / record / pending / count。"""

    def __init__(self, dsn="", enabled=True, webhook="", notify_timeout=5,
                 notify_format="generic", notify_workers=2, notify_queue_max=64):
        """构造。

        dsn: DB_URL；空则用内存存储。
        enabled: 关掉时 add/resolve/pending 全部空转（不影响主流程）。
        webhook: 通知地址；空则不通知。
        notify_timeout: 通知请求超时（秒）。
        notify_format: 通知体格式 generic | wecom | feishu。
        notify_workers: 通知线程数上限（见 `_ensure_notify_workers`）。
        notify_queue_max: 通知积压上限，满了就丢弃并计数（见 `_notify_async`）。
        """
        self.enabled = bool(enabled)
        self.dsn = (dsn or "").strip()
        self.webhook = (webhook or "").strip()
        self.notify_timeout = max(1, int(notify_timeout))
        self.notify_format = (notify_format or "generic").lower()
        self.notify_workers = max(1, int(notify_workers))
        self.notify_queue_max = max(1, int(notify_queue_max))
        self._mem: dict[str, dict] = {}
        self._lock = threading.Lock()
        # ⚠️ 建表标志必须与一把锁配套：见 `_ensure_schema` 的说明
        self._schema_ready = False
        self._schema_lock = threading.Lock()
        self.backend = "memory"
        # 通知通道（延迟创建；不配 webhook 就一个线程都不起）
        self._notify_q: queue.Queue | None = None
        self._notify_threads: list[threading.Thread] = []
        self._notify_lock = threading.Lock()
        self.notify_dropped = 0

    # ---------- 存储 ----------
    def _connect(self):
        """借一条**池化** PG 连接（借用 → 事务块 → 归还），见 `app/pg.py`。

        入队发生在生成结束的收尾里（`_stream_graph` 的 finally），是热路径；
        每次新建连接会让"审核落库"这件事跟着 PG 建连延迟走。
        """
        from . import pg

        return pg.connect(self.dsn)

    def _ensure_schema(self, conn):
        """建队列表（幂等）。只在首次写入时建，避免每次请求都发 DDL。

        审计列（2026-09-29 新增）用 `ADD COLUMN IF NOT EXISTS` 补
        ----------------------------------------------------
        队列表在老部署里**已经存在**，而 `CREATE TABLE IF NOT EXISTS` 对已存在的表
        是**静默无操作** —— 只改 CREATE、不加这几条 ALTER，线上会在 `resolve()`
        时报"列不存在"，且报错点离原因很远（表现成"审核动作 500"，而不是
        "你少跑了一次迁移"）。

        ⚠️ 上面的 `CREATE TABLE` 与下面的 `ALTER` 列表**必须同步改**：只改一处，
        新库（走 CREATE）与老库（走 ALTER）就有一条路径少列，而这两条路径的
        差异只在特定部署形态下才暴露。

        ⚠️ 为什么整段 DDL 必须在一把锁里（2026-10-06 补）
        ----------------------------------------------
        `_schema_ready` 是个普通 bool：冷启动时**多个并发请求会同时读到 False**，
        于是同时执行 `CREATE TABLE IF NOT EXISTS`。PG 在这个语句上有一条真实竞态：
        两个事务同时通过 `IF NOT EXISTS` 的存在性检查、再同时插入 `pg_type`/`pg_class`
        元组，后者会撞唯一索引，报
        `duplicate key value violates unique constraint "pg_type_typname_nsp_index"`
        —— 报错点看起来像"表建坏了"，实际是并发建表。

        这个窗口以前很窄（每次操作都新建连接，天然被建连延迟错开）；换成连接池之后
        多个工作线程可以同时进来，窗口反而变宽了。所以标志与锁必须配套：
        双重检查 + 整段 DDL 持锁。
        """
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:      # 双检：等锁期间可能已被别人建好
                return
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS review_queue ("
                    "  thread_id  text PRIMARY KEY,"
                    "  tenant     text NOT NULL DEFAULT '',"
                    "  draft      text,"
                    "  score      integer,"
                    "  feedback   text,"
                    "  status     text NOT NULL DEFAULT 'pending',"
                    "  resolved_by      text NOT NULL DEFAULT '',"
                    "  resolved_via     text NOT NULL DEFAULT '',"
                    "  resolution_note  text,"
                    "  resolved_at      timestamptz,"
                    "  created_at timestamptz NOT NULL DEFAULT now(),"
                    "  updated_at timestamptz NOT NULL DEFAULT now()"
                    ")"
                )
                # 老表补列。列名/类型是本地字面量常量，不来自外部输入。
                for col, typ in (
                    ("resolved_by", "text NOT NULL DEFAULT ''"),
                    ("resolved_via", "text NOT NULL DEFAULT ''"),
                    ("resolution_note", "text"),
                    ("resolved_at", "timestamptz"),
                ):
                    cur.execute(
                        f"ALTER TABLE review_queue ADD COLUMN IF NOT EXISTS {col} {typ}"
                    )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS review_queue_status_idx "
                    "ON review_queue (status, tenant)"
                )
            # 标志必须在**持锁**时置位：放到锁外，另一个线程可能已经在锁内
            # 判过"还没建好"并排上队了。
            self._schema_ready = True
            self.backend = "postgres"

    def ensure_schema(self):
        """确保队列表存在（幂等）。供部署脚本/迁移一次性建齐所有表。

        与 `add()` 首次写入时的自动建表复用**同一份 DDL**（`_ensure_schema`），
        所以不存在"两处表定义漂移"的问题。
        无 dsn（内存后端）时不做事，返回 False。
        """
        if not self.dsn:
            return False
        with self._connect() as conn:
            self._ensure_schema(conn)
        return True

    def add(self, thread_id, draft="", score=None, feedback="", tenant=""):
        """写入/更新一条待审记录，并异步发通知。

        thread_id: 会话 id（主键，同一会话重复触发只更新不新增）。
        draft: 草稿全文（通知里会截断展示，库里存全文）。
        score: 评审分数（可能为 None）。
        feedback: 评审意见。
        tenant: 租户标识。

        返回 True 表示落库/落内存成功。**任何异常都吞掉并返回 False** ——
        队列是旁路，绝不能因为它失败影响正在进行的对话流。
        """
        if not self.enabled or not thread_id:
            return False
        item = {
            "thread_id": thread_id, "tenant": tenant or "",
            "draft": draft or "", "score": score, "feedback": feedback or "",
            "status": "pending", "created_at": _now_iso(),
            # 审计字段**建记录时就占位**（而不是等 resolve 时才出现）：内存后端与
            # PG 后端的记录形状必须一致，否则调用方要按后端分支取值 —— 那正是
            # "两种实现暴露同一组方法、调用方无感"这个约定被破坏的开始。
            "resolved_by": "", "resolved_via": "", "resolution_note": "",
            "resolved_at": None,
        }
        ok = False
        if self.dsn:
            try:
                with self._connect() as conn:
                    self._ensure_schema(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO review_queue "
                            "(thread_id, tenant, draft, score, feedback, status) "
                            "VALUES (%s,%s,%s,%s,%s,'pending') "
                            "ON CONFLICT (thread_id) DO UPDATE SET "
                            "  tenant=EXCLUDED.tenant, draft=EXCLUDED.draft, "
                            "  score=EXCLUDED.score, feedback=EXCLUDED.feedback, "
                            "  status='pending', updated_at=now()",
                            (thread_id, item["tenant"], item["draft"],
                             None if score is None else int(score), item["feedback"]),
                        )
                ok = True
            except Exception as e:  # noqa: BLE001  （降级内存，不影响对话）
                logging.warning("审核队列写库失败，降级内存：%s", e)
        if not ok:
            with self._lock:
                self._mem[thread_id] = item
            ok = True

        self._notify_async(item)
        return ok

    def resolve(self, thread_id, status="accepted", *, actor="", note=""):
        """把一条记录标记为已处理（accepted / revised / dismissed），并**留痕**。

        thread_id: 会话 id。
        status: 新状态。
        actor: **做出这个决定的主体**（正常情况下是**用户名**；只有用户名取不到时
            才退回租户 —— 防御性兜底，已验签的会话身份一定有 user）。
        note: 人工反馈（revise 时就是"要改成什么样"）。

        为什么必须记 actor（这是本次补审计的全部理由）
        ------------------------------------------------
        `status` 只回答了"发生了什么"，回答不了"**是谁做的**" —— 而 human-in-the-loop
        里唯一需要回答"谁批的"的就是审核动作本身。少了操作者的记录，事故复盘时
        等于没有记录：只看到"这条被 accept 了"，无法判断是正常放行、越权操作还是
        误点，也无法按人追责。

        ⚠️ 表里的 `resolved_via` 列**保留但不再是入参**：它曾经记录"这个 actor 是从
        哪种凭证上得到的"，而那种区分随外部身份源的删除一起消失（只剩会话令牌），
        所以新记录固定写 `_RESOLVED_VIA`。列不删是为了历史留痕仍可读。

        顺带记 `resolved_at`：原先只有 `updated_at`，而它在 `add()` 的冲突更新里
        也会被刷新 ⇒ 无法回答"这条是什么时候被人处理掉的"。

        **只记租户名，不记密钥**；日志里也只记 note 的**长度**不记正文
        （反馈里常带用户内容，而日志会被转发到第三方系统）。

        返回 True/False（找不到或关闭时 False）。
        """
        if not self.enabled or not thread_id:
            return False
        ok = False
        failover = False
        waited = None
        if self.dsn:
            try:
                with self._connect() as conn:
                    self._ensure_schema(conn)
                    with conn.cursor() as cur:
                        # `RETURNING` 顺带取回"入队到此刻"的时长。为什么值得多这一句：
                        # human-in-the-loop 里用户实际承受的延迟主体就是**这段等待**
                        # （生成只占一小段），而此前唯一的队列指标 `review_pending`
                        # 只答得了"还压着多少条"，答不了"这一条等了多久" ——
                        # 积压 20 条各等 10 分钟，与积压 2 条各等 3 天，
                        # 在 `review_pending` 上完全一样。
                        cur.execute(
                            "UPDATE review_queue SET status=%s, resolved_by=%s, "
                            "resolved_via=%s, resolution_note=%s, resolved_at=now(), "
                            "updated_at=now() WHERE thread_id=%s "
                            "RETURNING EXTRACT(EPOCH FROM (now() - created_at))",
                            (status, actor or "", _RESOLVED_VIA, note or "", thread_id),
                        )
                        got = cur.fetchone()
                        ok = cur.rowcount > 0
                        if got and got[0] is not None:
                            waited = float(got[0])
            except Exception as e:  # noqa: BLE001
                logging.warning("审核队列更新失败：%s", e)
                # 写库失败才退回内存（与 `add()` 的降级口径一致）。注意不能无条件
                # 走内存：配了 dsn 时内存里通常没有这条记录，查了也是白查。
                failover = True
        if not self.dsn or failover:
            with self._lock:
                row = self._mem.get(thread_id)
                if row is not None:
                    waited = _waited_seconds(row.get("created_at"))
                    row.update(status=status, resolved_by=actor or "",
                               resolved_via=_RESOLVED_VIA, resolution_note=note or "",
                               resolved_at=_now_iso())
                    ok = True
        if ok:
            if waited is not None:
                # action 取有限值（accepted / revised / dismissed），可安全当标签。
                # 它让"被打回重做的那些是不是等得更久"变成可回答的问题。
                metrics.observe("review_wait_seconds", waited, action=status)
            logging.info(
                "审核动作：thread_id=%s status=%s actor=%s note_chars=%d",
                thread_id, status, actor or "-", len(note or ""),
            )
        return ok

    def remove(self, thread_id) -> bool:
        """把一条记录**从队列里删掉**（履行删除请求时调用）。

        与 `resolve()` 的区别是语义，不是程度：`resolve` 表示"人处理完了"
        （记录**必须保留**，它是审计证据），而 `remove` 表示"这条数据不该存在"
        （用户要求删除）。用 `resolve(..., "dismissed")` 冒充删除是错的 ——
        草稿正文仍在库里，而"已删除"的承诺已经做出去了。

        返回是否真的删掉了一条。存储异常不上抛：删除流程应尽力而为，
        调用方据返回值决定是否告警（见 `scripts/retention_sweep.py` 与 DELETE 端点）。
        """
        if not self.enabled or not thread_id:
            return False
        if self.dsn:
            try:
                with self._connect() as conn:
                    self._ensure_schema(conn)
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM review_queue WHERE thread_id=%s",
                                    (thread_id,))
                        return cur.rowcount > 0
            except Exception as e:  # noqa: BLE001
                logging.warning("审核队列删除失败：%s", e)
        with self._lock:
            return self._mem.pop(thread_id, None) is not None

    def record(self, thread_id):
        """读一条记录的**全量**内容（含审计字段）；不存在返回 None。

        `pending()` 只列"还没人处理"的，而审计要回答的恰恰是"这条**已经**被谁在
        什么时候、以什么方式处理了"。没有读取路径的审计字段等于没记 ——
        只能靠人手动去库里 SELECT，巡检与复盘都用不上。
        """
        if not self.enabled or not thread_id:
            return None
        if self.dsn:
            try:
                with self._connect() as conn:
                    self._ensure_schema(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT thread_id, tenant, draft, score, feedback, status, "
                            "resolved_by, resolved_via, resolution_note, "
                            "created_at, resolved_at FROM review_queue "
                            "WHERE thread_id=%s",
                            (thread_id,),
                        )
                        r = cur.fetchone()
                if r is None:
                    return None
                return {
                    "thread_id": r[0], "tenant": r[1], "draft": r[2], "score": r[3],
                    "feedback": r[4], "status": r[5],
                    "resolved_by": r[6], "resolved_via": r[7], "resolution_note": r[8],
                    "created_at": str(r[9]),
                    "resolved_at": str(r[10]) if r[10] else None,
                }
            except Exception as e:  # noqa: BLE001
                logging.warning("审核记录查询失败，回退内存：%s", e)
        with self._lock:
            row = self._mem.get(thread_id)
            return dict(row) if row is not None else None

    def pending(self, limit=50, tenant=None):
        """列出待审记录（按创建时间倒序）。

        limit: 最多返回多少条。
        tenant: **要看的租户**。字符串（**包括空串**）一律按该租户过滤；
                只有显式传 `None`（或不传）才是"不加租户过滤"。

        ⚠️ 为什么把"不过滤"从空串改成一个显式哨兵（2026-10-05）
        ----------------------------------------------------
        此前签名是 `tenant=""`，判据是 `if tenant:` —— 于是**空串等于不过滤**。
        这个等价关系很隐蔽：调用方看着"我明明把 tenant 传进去了"，实际语义却是
        "我不关心租户，给我看全部"。`GET /v1/reviews` 正好踩中 ——
        它把身份的原始租户（还没有租户时就是空串）直接传进来，于是那个账号
        列出了**全部租户**的待审草稿。

        让"空串"与"不过滤"共用同一个值，等于给"忘了传租户"和"故意要全量"
        留了同一种写法。改成显式哨兵后两种意图在调用点一眼可辨，
        且**空串不再有"放大成全部"的副作用**。

        ⚠️ 生产端点**必须**显式传身份自己的租户；不传（= 全量）只该被运维脚本用。
        """
        if not self.enabled:
            return []
        if self.dsn:
            try:
                with self._connect() as conn:
                    self._ensure_schema(conn)
                    with conn.cursor() as cur:
                        if tenant is None:
                            cur.execute(
                                "SELECT thread_id, tenant, score, feedback, draft, created_at "
                                "FROM review_queue WHERE status='pending' "
                                "ORDER BY created_at DESC LIMIT %s",
                                (int(limit),),
                            )
                        else:
                            cur.execute(
                                "SELECT thread_id, tenant, score, feedback, draft, created_at "
                                "FROM review_queue WHERE status='pending' AND tenant=%s "
                                "ORDER BY created_at DESC LIMIT %s",
                                (tenant, int(limit)),
                            )
                        rows = cur.fetchall()
                return [{"thread_id": r[0], "tenant": r[1], "score": r[2],
                         "feedback": r[3], "draft": r[4],
                         "created_at": str(r[5])} for r in rows]
            except Exception as e:  # noqa: BLE001
                logging.warning("审核队列查询失败，回退内存：%s", e)
        with self._lock:
            items = [dict(v) for v in self._mem.values()
                     if v.get("status") == "pending"
                     and (tenant is None or v.get("tenant") == tenant)]
        items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return items[: int(limit)]

    def count_pending(self, tenant=None) -> int:
        """待审条数。`tenant` 语义与 `pending` 一致：None = 全量，字符串 = 该租户。

        ⚠️ 必须走 `count(*)`，不能写成 `len(self.pending(limit=10**9))`（2026-10-06 修）。
        后者为了"数个数"把**全部待审行的 `draft` 全文**拉进内存再丢掉；而这个函数
        被 `/metrics` 的**每次抓取**调用（Prometheus 默认 30s 一次）⇒ 积压越长、
        草稿越大，抓取越慢、内存峰值越高 —— **观察组件自己变成故障源**，
        而且没有任何指标会反映它（`review_pending` 增长本身是正常的）。
        """
        if not self.enabled:
            return 0
        if self.dsn:
            try:
                with self._connect() as conn:
                    self._ensure_schema(conn)
                    with conn.cursor() as cur:
                        if tenant is None:
                            cur.execute("SELECT count(*) FROM review_queue "
                                        "WHERE status='pending'")
                        else:
                            cur.execute("SELECT count(*) FROM review_queue "
                                        "WHERE status='pending' AND tenant=%s", (tenant,))
                        row = cur.fetchone()
                return int(row[0]) if row else 0
            except Exception as e:  # noqa: BLE001
                logging.warning("审核队列计数失败，回退内存：%s", e)
        with self._lock:
            return sum(1 for v in self._mem.values()
                       if v.get("status") == "pending"
                       and (tenant is None or v.get("tenant") == tenant))

    # ---------- 通知 ----------
    def _build_notify_body(self, item) -> dict:
        """按目标平台拼通知体。

        item: add() 组装的记录。

        企微/飞书都要求特定结构（`msgtype.text.content` / `msg_type.content.text`），
        额外字段可能被拒；generic 则是 `{event, ...}` 供自建网关解析。
        """
        line = (f"[待人工审核] 会话 {item['thread_id']}｜评分 {item.get('score')}\n"
                f"意见：{item.get('feedback') or '（无）'}\n"
                f"草稿：{_clip(item.get('draft'), 300)}")
        if self.notify_format == "wecom":
            return {"msgtype": "text", "text": {"content": line}}
        if self.notify_format == "feishu":
            return {"msg_type": "text", "content": {"text": line}}
        return {"event": "review_needed", "thread_id": item["thread_id"],
                "tenant": item.get("tenant", ""), "score": item.get("score"),
                "feedback": item.get("feedback"), "draft": _clip(item.get("draft"), 300),
                "text": line}

    def notify(self, item) -> bool:
        """同步发一次通知，返回是否成功（供测试直接调用）。"""
        if not self.webhook:
            return False
        try:
            import httpx

            resp = httpx.post(self.webhook, json=self._build_notify_body(item),
                              timeout=self.notify_timeout)
            resp.raise_for_status()
            return True
        except Exception as e:  # noqa: BLE001  （通知失败不该冒泡）
            logging.warning("审核通知发送失败：%s", e)
            return False

    def _ensure_notify_workers(self) -> None:
        """懒起通知工作线程（不配 webhook 就一个都不起）。"""
        if not self.webhook or self._notify_q is not None:
            return
        with self._notify_lock:
            if self._notify_q is not None:
                return
            self._notify_q = queue.Queue(maxsize=self.notify_queue_max)
            q = self._notify_q
            for i in range(self.notify_workers):
                t = threading.Thread(target=self._notify_loop, args=(q,),
                                     name=f"review-notify-{i}", daemon=True)
                self._notify_threads.append(t)
                t.start()
            logging.info("审核通知通道已就绪：%d 个线程，积压上限 %d",
                         self.notify_workers, self.notify_queue_max)

    def _notify_loop(self, q: "queue.Queue") -> None:
        """工作线程主循环：串行取队列里的通知发送；收到哨兵即退出。"""
        while True:
            item = q.get()
            try:
                if item is None:        # 哨兵 = 关闭
                    return
                self.notify(item)
            finally:
                q.task_done()

    def _notify_async(self, item):
        """把通知投进**有界**队列，由固定几个后台线程发送。

        为什么不是"每条通知起一个线程"（原实现）
        ----------------------------------------
        原实现 `threading.Thread(...).start()` 是**无界**的：webhook 变慢时，
        每条待审通知都新起一个线程，每个都持有最长 `notify_timeout` 秒。
        审批评审高峰（或 webhook 半死、每请求都超时）会让线程数随待审量线性增长，
        直到把内存与线程上限吃掉 —— 而症状是"进程越来越慢"，看不出跟通知有关。

        现在：线程数固定，积压有上限；队列满说明通知端持续慢/挂，
        **丢弃并计数**（`notify_dropped`）而不是继续排队 ——
        积压的通知价值随时间衰减，而内存是有限资源。审核记录本身已经在库里，
        通知只是"提醒"，丢掉通知不会丢工单。
        """
        if not self.webhook:
            return
        self._ensure_notify_workers()
        q = self._notify_q
        if q is None:
            return
        try:
            q.put_nowait(item)
        except queue.Full:
            self.notify_dropped += 1
            logging.warning(
                "审核通知积压已满（上限 %d，累计丢弃 %d）—— 通知端可能持续慢或不可达；"
                "待审记录本身已入库，不影响工单", self.notify_queue_max, self.notify_dropped)

    def close(self, timeout: float = 5.0) -> None:
        """停止通知线程，并**尽力把已投递的通知发完**（关机路径调用；幂等）。

        为什么必须真的"等"（而不是投个哨兵就返回）
        ----------------------------------------
        哨兵是 `put` 到队尾的，所以工作线程本来就会先处理完队列里的条目 ——
        看起来"不用等"。但 `close()` 返回后不久**进程就退出了**：
        若此刻还有条目在途（正在发、或还没轮到），它们会随进程一起消失。
        所以这里必须亲手把"在途 + 排队"等到归零，而不是依赖 `join()` 那点固定预算。

        等待有上限（默认 5 秒）：webhook 整个挂掉时，每条都要耗满
        `notify_timeout`，不能让发布被它卡住。超时就放弃并**明确记一条日志** ——
        "丢了几条"必须可查，否则表现为"审核台没收到提醒"，无从判断是没触发还是丢了。

        timeout: 排空等待上限（秒）。
        """
        with self._notify_lock:
            q, threads = self._notify_q, list(self._notify_threads)
            self._notify_q = None
            self._notify_threads = []
        if q is None or not threads:
            return

        # `unfinished_tasks` 是 Queue 的**内部计数**（每 put +1、每 task_done -1），
        # 它同时覆盖"排队中"与"正在处理"，正是这里要的口径；
        # `qsize()`/`empty()` 都看不到在途的那条。轮询而不是 `q.join()`：
        # join 没有超时参数，而"不能无限等"是这条路径的硬要求。
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline and q.unfinished_tasks > 0:
            time.sleep(0.05)

        if q.unfinished_tasks > 0:
            logging.warning("审核通知仍有 %d 条未发出（等待超时 %.1fs），随进程退出丢弃",
                            q.unfinished_tasks, timeout)

        for _ in threads:
            try:
                q.put_nowait(None)      # 每个工作线程一个哨兵
            except queue.Full:
                break                   # 队列满 ⇒ 线程已在消费，哨兵可以少发
        for t in threads:
            t.join(timeout=0.5)


def build_review_queue() -> ReviewQueue:
    """按 app.config 造审核队列（main.py 启动时调用一次）。"""
    return ReviewQueue(
        dsn=config.DB_URL,
        enabled=config.REVIEW_QUEUE_ENABLED,
        webhook=config.REVIEW_NOTIFY_WEBHOOK,
        notify_timeout=config.REVIEW_NOTIFY_TIMEOUT,
        notify_format=config.REVIEW_NOTIFY_FORMAT,
        notify_workers=config.REVIEW_NOTIFY_WORKERS,
        notify_queue_max=config.REVIEW_NOTIFY_QUEUE_MAX,
    )


def _safe_json(obj) -> str:
    """把对象序列化成 JSON 串（日志用；失败退回 str）。"""
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)
