# 变更日志

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)，格式参考
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

> **版本号的唯一真源是仓库根的 `VERSION` 文件。** 改版本的顺序是：
> ① 改 `VERSION` → ② `python scripts/tasks.py sync-version`（同步 compose 与
> `.env.example` 里的副本）→ ③ 在下面补一条。第 ③ 步容易忘，所以有守卫盯着：
> `tests/test_tasks_entrypoint.py` 会核对"这里有没有当前版本的条目"，忘了会红 ——
> 而不是等几个月后有人问"这个版本到底改了什么"。

## [Unreleased]

### 新增

- **知识库级联删除**（`src/rag/kb_inventory.py` + `scripts/kb_prune.py`）。
  灌库是幂等的，但此前**没有反向路径**：源文件删了或改了之后那批块会永久驻留，
  表现为"答案里有已下线内容"与"声明已删除却仍可被搜出来"。现在每个块带上
  `source_path` 与 `content_hash`（**都不参与 `chunk_id`**），由 `tasks kb-prune`
  比对语料目录清理两类孤儿（`missing` 默认清、`stale` 需显式开启）。
  ⚠️ 语料目录扫不到任何文件时**拒绝执行**（退出码 2）—— 那几乎总是"语料没挂进
  容器"，照常比对会清空全库。
- **并发闸门**（`middleware.ConcurrencyLimitMiddleware` + `MAX_CONCURRENT_REQUESTS`）。
  `RATE_LIMIT_*` 限的是每分钟新请求数，管不住**并发驻留**：一个客户端按限流上限
  开若干 SSE 连接并保持不关就能把单实例占满，而所有既有指标都正常。
  超限返回 `503 + Retry-After`；`/health/*` 与 `/metrics` 豁免（被拒会让编排重启
  实例、让 Prometheus 在最该看数据时没有数据）；饱和率与拒绝速率进面板，
  配 `ConcurrencySaturated` / `ConcurrencyShedding` 两条告警。

## [0.2.0] - 2026-09-30

从 notebook 原型到可上线服务的一次性收敛。0.2.0 之前的形态没有版本化记录。

### 新增 — RAG 链路（`src/rag/`）

- **分流解析**：`.pdf` / `.docx` 走 Docling（版式检测 / 表格识别 / OCR），其余格式走
  轻量解析器；任一环节不可用时**自动降级**并继续，而不是把整篇文档丢掉
- **结构感知切分**：每个 chunk 携带 `chunk_id`、来源、页码、层级路径等元数据
- **可回溯**：引用能回到原文位置，包含 docx 内嵌图片与扫描件的整页图
- **检索**：BM25 与稠密向量的 RRF 融合；向量后端可选 pgvector 或 Qdrant
- 引用（citations）与上下文块由检索层统一产出，服务层不再拼装

### 新增 — 服务层（`app/`）

- **鉴权三档**，按配置自动选择：`TENANT_KEYS` 反查（租户绑定到 Key）→
  `SERVE_API_KEY` 自报 → `anonymous`。生产环境且未配任何 Key 时**拒绝启动**
- **配额 `claim` → `settle` 两阶段**；跨副本共享计数（Redis），后端故障时退到进程内
  （不是放行）
- **限流：加权滑动窗口**（`RATE_LIMIT_*`）
- **请求体积两层闸门**：ASGI 字节闸门（413，在路由之前）+ 字段闸门（422）
- **断连止损**与**优雅关闭**（`GRACEFUL_SHUTDOWN_TIMEOUT` 与 compose 的
  `stop_grace_period` 必须联动）
- **会话状态与断点续传**：`GET /v1/sessions/{thread_id}`、`POST /v1/chat/resume`
- **人工审核**：`POST /v1/review`、`GET /v1/reviews`
- **可观测**：`/metrics`（Prometheus 文本，无需额外依赖）、`/health/live`、
  `/health/ready`（就绪探针会真查 LLM 配置 / 数据库 / 向量扩展 / 检查点类型）

### 新增 — 部署与运维（`deploy/`）

- 一套 compose 起全栈：pgvector / Qdrant / Redis / app-stable / app-canary /
  alert-sink / alertmanager / prometheus / blackbox / node-exporter / loki /
  promtail / grafana / nginx / 备份与保留任务
- **nginx 灰度分流**（stable / canary），`scripts/nginx_weight.py` 改权重
- **发布与回滚脚本**：`deploy/release.sh`、`deploy/rollback.sh`。整条流程由
  `scripts/canary_check.py` 的**退出码**驱动决策 —— 0 推进 / 1 回滚 / 2 转人工。
  退出码 2（数据不足）刻意**不回滚**：那会回滚一个其实没问题的版本，还会把
  "看不到数据"这个真问题掩盖掉
- **备份与恢复演练**：PostgreSQL / Qdrant 的备份、校验、清理与保留策略；
  `drill` 是真的"备份 → 恢复到临时库 → 逐表比对"
- **监控**：30 条告警（availability / slo / cost / security / platform）+ 4 个
  Grafana 面板 + SLO 与错误预算策略文档
- **恢复演练闭环**：演练结果落 textfile 指标，由三条告警盯着
  （从没演练过 / 太久没演练 / 上次没通过）。**不跑 drill，告警会一直响** —— 这是刻意的

### 新增 — 工程化

- `VERSION` 作为版本号唯一真源，`scripts/sync_version.py` 同步其余副本
- `requirements.lock` 锁定精确版本（`requirements.txt` 只表达意图）+ 漂移校验
- **OpenAPI 契约快照门禁**：接口的任何变化都会以 diff 形式出现在 CI 里
- 任务入口 `scripts/tasks.py`（跨平台）+ `Makefile`（薄转发）：
  `check` / `test` / `drill` / `up` / `release` …
- CI 七个 job：test / eval-gate / full-deps / drill / image / security / publish

### 变更

- RAG 链路从 `src/` 根收敛到 `src/rag/`（11 个模块），编排层留在 `src/graph.py`
- 行尾统一为 LF（新增 `.gitattributes` / `.editorconfig`）——
  此前只有本机的 `core.autocrlf`，同一份代码在不同机器上行尾不同

### 修复

以下每一条都是"看起来在工作、实际已经失效"的类型：

- **断连止损**：轮询 `is_disconnected()` 永远晚一步（`receive()` 是单消费者通道，
  库的监听任务会先拿到 `http.disconnect`）→ 改用 sse_starlette 的
  `client_close_handler_callable`
- **SSE 结构化载荷**：直接 yield dict 时库走 `str(self.data)`，线上发出去的是
  **Python repr**（单引号 / `True` / `None`），非 Python 客户端全废且服务端无告警
  → 统一自行 `json.dumps`（`token` 事件保持裸文本）
- **限流**：固定窗口在跨窗口时可放行 `2N` → 改为加权滑动窗口；Redis 键的 TTL
  必须覆盖**两个**窗口，否则缺陷在跨窗口瞬间原样复发
- **配额**：原来是"请求头 check、请求尾 add"，并发请求会各自读到同一个"还没超"的
  旧值后一起放行，日额度被超掉 N 份 → 改为 `claim` 原子的"预占 + 判定"
- **指标直方图**：存原始观测值使**内存随 QPS 线性增长**（一个被刷的接口即 OOM），
  而 `_count` 增长看起来完全正常 → 改为固定桶
- **优雅关闭**：容器宽限期默认 10s 会 SIGKILL 掐断在途 SSE，且不留清理机会
  → 显式配置并与 compose 联动（只改一处等于没改）

### 安全

- 全局 Key 比较统一用 `hmac.compare_digest`
- 下发客户端的异常统一收敛（生产环境只给通用文案 + `request_id`）；上游
  `X-Request-ID` 走白名单，防日志放大与头注入
- 检索层租户隔离**只在可信租户（Key 反查得出的）下启用**；走客户端可伪造的
  `X-Tenant-Id` 时退回默认租户 —— 否则改个请求头就能读到别人的知识库（这种
  "假隔离"比不隔离更危险，因为它看起来是安全的）
- 读路径用无副作用的属主查询（`owner_of`），查不到属主回 404 而非 403，避免
  用状态码枚举出"哪些 thread_id 存在"
