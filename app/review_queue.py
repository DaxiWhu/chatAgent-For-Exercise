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
`resolved_by`（租户）/ `resolved_via`（认证方式）/ `resolution_note`（人工反馈）/
`resolved_at`，并打一条 INFO 审计日志。读取走 `record(thread_id)`。

`resolved_via` 不是可有可无的装饰：三种认证模式下租户的可信度不同
（`tenant-key` 由 Key 反查、`global-key`/`anonymous` 是客户端自报），
复盘时必须能分辨这条归属是"可信"还是"自报"，否则会追责到错的人身上。
"""

import json
import logging
import threading
import time

from . import config


def _now_iso() -> str:
    """当前本地时间 ISO 串（落库/通知都用它，便于人读）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _clip(text, n=500) -> str:
    """把草稿截断到 n 字符（通知内容不宜太长，队列里也只存摘要）。"""
    s = (text or "").strip()
    return s if len(s) <= n else s[:n] + "…"


class ReviewQueue:
    """待审队列：add / resolve / record / pending / count。"""

    def __init__(self, dsn="", enabled=True, webhook="", notify_timeout=5,
                 notify_format="generic"):
        """构造。

        dsn: DB_URL；空则用内存存储。
        enabled: 关掉时 add/resolve/pending 全部空转（不影响主流程）。
        webhook: 通知地址；空则不通知。
        notify_timeout: 通知请求超时（秒）。
        notify_format: 通知体格式 generic | wecom | feishu。
        """
        self.enabled = bool(enabled)
        self.dsn = (dsn or "").strip()
        self.webhook = (webhook or "").strip()
        self.notify_timeout = max(1, int(notify_timeout))
        self.notify_format = (notify_format or "generic").lower()
        self._mem: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._schema_ready = False
        self.backend = "memory"

    # ---------- 存储 ----------
    def _connect(self):
        """建 PG 连接（延迟导入 psycopg，未装则抛 ImportError 由调用方降级）。"""
        import psycopg

        return psycopg.connect(self.dsn, connect_timeout=5, autocommit=True)

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
        """
        if self._schema_ready:
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

    def resolve(self, thread_id, status="accepted", *, actor="", via="", note=""):
        """把一条记录标记为已处理（accepted / revised / dismissed），并**留痕**。

        thread_id: 会话 id。
        status: 新状态。
        actor: **做出这个决定的主体**（本服务里就是租户标识）。
        via: 该主体的认证方式（`tenant-key` / `global-key` / `anonymous`）。
        note: 人工反馈（revise 时就是"要改成什么样"）。

        为什么必须记 actor / via（这是本次补审计的全部理由）
        ------------------------------------------------
        `status` 只回答了"发生了什么"，回答不了"**是谁做的**" —— 而 human-in-the-loop
        里唯一需要回答"谁批的"的就是审核动作本身。少了操作者的记录，事故复盘时
        等于没有记录：只看到"这条被 accept 了"，无法判断是正常放行、越权操作还是
        误点，也无法按人追责。

        `via` 一起记，是因为三种认证模式下 `actor` 的**可信度完全不同**：`tenant-key`
        下租户由 Key 反查（可信），`global-key` / `anonymous` 下租户来自客户端可伪造的
        `X-Tenant-Id`。复盘时必须能分辨"这是个可信归属"还是"这是自报的" ——
        否则一条伪造的 actor 会让追责指向错误的人。

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
        if self.dsn:
            try:
                with self._connect() as conn:
                    self._ensure_schema(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE review_queue SET status=%s, resolved_by=%s, "
                            "resolved_via=%s, resolution_note=%s, resolved_at=now(), "
                            "updated_at=now() WHERE thread_id=%s",
                            (status, actor or "", via or "", note or "", thread_id),
                        )
                        ok = cur.rowcount > 0
            except Exception as e:  # noqa: BLE001
                logging.warning("审核队列更新失败：%s", e)
                # 写库失败才退回内存（与 `add()` 的降级口径一致）。注意不能无条件
                # 走内存：配了 dsn 时内存里通常没有这条记录，查了也是白查。
                failover = True
        if not self.dsn or failover:
            with self._lock:
                row = self._mem.get(thread_id)
                if row is not None:
                    row.update(status=status, resolved_by=actor or "",
                               resolved_via=via or "", resolution_note=note or "",
                               resolved_at=_now_iso())
                    ok = True
        if ok:
            logging.info(
                "审核动作：thread_id=%s status=%s actor=%s via=%s note_chars=%d",
                thread_id, status, actor or "-", via or "-", len(note or ""),
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

    def pending(self, limit=50, tenant=""):
        """列出待审记录（按创建时间倒序）。

        limit: 最多返回多少条。
        tenant: 非空则只返回该租户的。
        """
        if not self.enabled:
            return []
        if self.dsn:
            try:
                with self._connect() as conn:
                    self._ensure_schema(conn)
                    with conn.cursor() as cur:
                        if tenant:
                            cur.execute(
                                "SELECT thread_id, tenant, score, feedback, draft, created_at "
                                "FROM review_queue WHERE status='pending' AND tenant=%s "
                                "ORDER BY created_at DESC LIMIT %s",
                                (tenant, int(limit)),
                            )
                        else:
                            cur.execute(
                                "SELECT thread_id, tenant, score, feedback, draft, created_at "
                                "FROM review_queue WHERE status='pending' "
                                "ORDER BY created_at DESC LIMIT %s",
                                (int(limit),),
                            )
                        rows = cur.fetchall()
                return [{"thread_id": r[0], "tenant": r[1], "score": r[2],
                         "feedback": r[3], "draft": r[4],
                         "created_at": str(r[5])} for r in rows]
            except Exception as e:  # noqa: BLE001
                logging.warning("审核队列查询失败，回退内存：%s", e)
        with self._lock:
            items = [dict(v) for v in self._mem.values()
                     if v.get("status") == "pending" and (not tenant or v.get("tenant") == tenant)]
        items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return items[: int(limit)]

    def count_pending(self, tenant="") -> int:
        """待审条数（无参数时全量；tenant 非空则限租户）。"""
        return len(self.pending(limit=10 ** 9, tenant=tenant))

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

    def _notify_async(self, item):
        """后台线程发通知：**不阻塞请求**（webhook 慢或挂掉都不该拖慢对话）。"""
        if not self.webhook:
            return
        threading.Thread(target=self.notify, args=(item,), daemon=True).start()


def build_review_queue() -> ReviewQueue:
    """按 app.config 造审核队列（main.py 启动时调用一次）。"""
    return ReviewQueue(
        dsn=config.DB_URL,
        enabled=config.REVIEW_QUEUE_ENABLED,
        webhook=config.REVIEW_NOTIFY_WEBHOOK,
        notify_timeout=config.REVIEW_NOTIFY_TIMEOUT,
        notify_format=config.REVIEW_NOTIFY_FORMAT,
    )


def _safe_json(obj) -> str:
    """把对象序列化成 JSON 串（日志用；失败退回 str）。"""
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)
