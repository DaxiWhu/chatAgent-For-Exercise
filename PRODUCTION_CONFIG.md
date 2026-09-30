# 生产化配置清单（需要你填的都在这）

> 本文件只回答一个问题：**哪些环境变量还需要你填值**，以及**不填的后果**。
> 其余配置都有"安全且不改变既有行为"的默认值，不填即可跑。
>
> 状态列是 **2026-09-29 对 `.env` 的实测复核结果**（用解析后的值判定，
> 不是 `grep` 猜的 —— `.env` 是 CRLF 行尾，用 shell `[ -z "$v" ]` 判空会把
> 空值行读成 `"\r"` 而误判为"已填"，我上一轮就是这么得出错误结论的）。
>
> ⚠️ **这批状态与 9-28 那版有实质差异**：成本刹车、单请求总时限、`SERVE_API_KEY`
> 都已按下面的建议值落盘（见 `git diff .env.example` 与 `.env.bak`）。
> 仍为 ⚠️ 的只剩真正需要你做决定的那几项 —— 其中**告警通道 webhook 是最紧的一个**：
> 不填，前面所有告警配置都只是面板上的灯，事件永远送不到人。

图例：**⚠️必填** / ○可选（不填有安全默认）/ ✅已有值（沿用）

---

## 一、必须处理（当前状态就有真实风险）

| 变量 | 当前状态 | 不填的后果 | 建议值 |
|---|---|---|---|
| `SERVE_API_KEY` **或** `TENANT_KEYS` | ✅ `SERVE_API_KEY` 已生成（`secrets.token_hex(32)`）；`TENANT_KEYS` 仍未设置 | 原状态是"两个都没有 ⇒ `_authenticate` 落到 `anonymous`：**接口对任何人开放**"。现已补上全局 Key。**只做单租户时到此为止**；要做多租户隔离仍需配 `TENANT_KEYS`（只有它才让"租户由 Key 反查"成立，否则 `X-Tenant-Id` 只是客户端自报，检索层会退回默认租户） | 多租户：`TENANT_KEYS=acme:<key>,globex:<key>`（租户绑定到 Key，推荐）<br>单租户：已生成，无需再动 |
| `APP_ENV` | ○ **`.env` 里仍未设置（刻意）**；容器部署由 compose 注入 `production` | 本机直跑 `python -m app.serve` 时是 `development`（`/docs` 可见、不强制鉴权）—— 这是**有意保留**的，否则本地调试会被鉴权挡住。两条防线：① `deploy/app/docker-compose.yml` 里 `APP_ENV: ${APP_ENV:-production}`；② `deploy/app/docker-compose.staging.yml` 里**写死** `production`（写 `staging` 会静默关掉鉴权，见 `deploy/CANARY.md`）。**用非 compose 方式部署时，必须自己设 `APP_ENV=production`** | `production`（部署时） |
| `LLM_MAX_TOKENS_PER_REQUEST` | ✅ `8000` | 单请求 token 上限已装。原来 `0` = 不限，一次超长输入 / 模型跑飞就会**无限烧钱** | `4000`~`8000` |
| `USER_DAILY_TOKEN_QUOTA` | ✅ `200000` | 每租户日额度已装。这是**按估计**定的 —— 有了真实用量后应据 `llm_tokens_per_request` 的分布回填 | 按成本预算定，如 `200000` |
| `REQUEST_TIMEOUT_SECONDS` | ✅ `180` | 单请求**总时限**已装。注意 `_MODEL` 上的 `timeout=30` 只管**单次调用**，而一次请求可以跑多轮（自评审不通过会再来一轮）⇒ 没有它总时长无上界，而且 **`GRACEFUL_SHUTDOWN_TIMEOUT` 的排空上界会形同虚设**（它等的是在途请求，而在途请求自己不会结束）。超时会下发结构化 `timeout` 事件后收流，**已产出的正文照常交付** | `120`（保守可给 `180`） |
| `TRUST_PROXY_HEADERS` | ✅ `0` | 直连场景下 `0` 是**正确**的。但**一旦部署到 Nginx/网关后面**必须改 `1`：否则限流按**代理 IP** 统计，全体用户共用一个计数桶 ⇒ 要么集体误伤、要么被单人吃满。compose 里已为两个 app 服务注入 `TRUST_PROXY_HEADERS: "1"`（它们确实在 nginx 后面） | 反代后设 `1`；直连保持 `0` |
| `ALERT_SINK_WECOM_WEBHOOK` 等三个通道 | ⚠️ **全部为空**（当前**最紧的一个待办**） | 告警转发的出口。**不填 ⇒ `alert-sink` 只把事件记进日志**（这是刻意设计：没配通道不阻塞、不报错），于是 Prometheus 里的 FIRING 永远变不成一条发到人手里的消息。34 条规则 + 五级路由全都写好了，差的只有这一步。<br>⚠️ **两个坑**：① 填完必须用 `docker compose --env-file .env ... config \| grep -i webhook` 自证值真的传进去了（**漏 `--env-file` 时它静默不生效**，见本文档开头的说明）；② `alert_sink.py --selftest` 只能证明网关进程活着，**必须再真触发一次告警**（停 `app-canary` 让 `ChatAgentDown` 响）才算闭环 | 填企业微信/飞书机器人 URL；自建网关填 `ALERT_SINK_GENERIC_WEBHOOK` |
| `ALERT_SINK_SHARED_SECRET` | ⚠️ 为空 —— **刻意保持为空，别单独填它** | 网关的来源校验：非空时要求每个 POST 带 `X-Alert-Sink-Secret` 头且匹配，否则回 **403**。⚠️ **陷阱就在这里**：`alertmanager.yml` 的四个 receiver **没有** `http_config`，**根本不会送这个头** ⇒ 你填了这个变量，等于**把全部告警打成 403**（Alertmanager 重试几次后放弃，表现成"配置全对但没人收到"，网关日志里只有一行 403）。要用它必须**两处一起改**：给四个 receiver 补 `http_config: http_headers: X-Alert-Sink-Secret: [...]` —— 而 Alertmanager **不支持 `${VAR}` 插值**，密钥只能写进配置文件，又回到"凭据进版本库"（这正是本网关要避免的事）。<br>**当前实际暴露面本就有限**：宿主机端口是回环绑定（`127.0.0.1:8081:8080`），仅剩"同 compose 网络内其它容器可伪造告警"，那更适合用部署侧网络隔离解决 | **留空**。确实要开 ⇒ 两处一起改，并接受密钥进配置文件 |
| `REDIS_PASSWORD` / `POSTGRES_PASSWORD` / `GRAFANA_PASSWORD` | ✅ `REDIS_PASSWORD` 已生成；另两个走 compose 默认值 | 编排层口令（由 compose 从 `.env` 插值，`app` 不读）。**`POSTGRES_PASSWORD` 仍是默认 `123456`** —— 上生产前必须换，且注意它**只在数据卷首次初始化时生效**（详见 `deploy/SECRETS.md` §4.3）。密钥管理的整体方案见 `deploy/SECRETS.md` | 见 `deploy/SECRETS.md` |

> ⚠️ `TRUST_PROXY_HEADERS` 两个方向都是真实故障：**直连却设 1**，客户端加一个
> `X-Forwarded-For: 1.2.3.4` 就换一个限流桶，限流形同虚设（该头客户端可任意伪造）。

## 二、规模化才需要（单实例可留空）

| 变量 | 当前状态 | 说明 | 建议值 |
|---|---|---|---|
| `REDIS_URL` | ○ `.env` 里仍空；**容器部署已由 compose 注入** | 原风险是"多副本时限流/配额是**进程内**计数，2 个副本 = 实际额度 ×2"。`deploy/app/docker-compose.yml` 已为 `app-stable` / `app-canary` 注入 `redis://:${REDIS_PASSWORD}@redis:6379/0`，并加了 `redis` 服务（带口令、`appendonly`、资源上限）。**本机直跑时仍是进程内计数**（单进程，行为正确）。⚠️ 后端不可用时 app 退到进程内计数（**不是放行**），且启动期 ping 不通就整程不复位——所以 compose 里用 `service_healthy` 等它就绪 | 容器内无需手工填；本机要模拟多副本再填 |
| `APP_WORKERS` / `WEB_CONCURRENCY` | 由 compose 注入 | worker 数，默认 1。**刻意默认 1**：多 worker 会各自持有检索器与限流计数，没配 `REDIS_URL` 时额度会翻倍。compose 里 `app-stable` 给 `2`、`app-canary` 给 `1`（因为 Redis 已接上）。两者都认，前者优先（容器/PaaS 常注入后者） | 生产 `2`~`4`，**且必须先确认 `REDIS_URL` 生效** |
| ~~`DISCONNECT_POLL_INTERVAL`~~ | — | **已废弃（2026-09-29）**：断连检测改为 sse_starlette 的 `client_close_handler_callable` 回调，不再有轮询间隔。旧值留着无害，会被忽略 | 删除该行 |
| `HEALTH_READY_TIMEOUT` | 未设置 | 就绪探针里探依赖的超时（秒），默认 `3`。网络慢的跨机房环境可加大 | 默认即可 |
| `RETENTION_DAYS` | ✅ `90`（2026-09-30 落盘） | 数据保留清理的判定口径。**新增了 `retention-sweep` 服务每天执行** —— 在此之前清理只活在脚本 docstring 的 cron 注释里，等于没有任何东西在跑，三张表（检查点 / 审核队列 / 归属登记）**只增不减**。判定依据是「最后活动时间」，所以创建得早、今天还在用的会话不会被删。设 0 时容器照常运行但不删任何东西并每轮播报"未配置" | `90`（按合规要求定） |
| `BACKUP_INTERVAL_HOURS` / `BACKUP_KEEP` / `QDRANT_BACKUP_KEEP` | ✅ `6` / `56` / `28`（2026-09-30 落盘） | 备份节奏。**间隔 × 份数 = 可回溯窗口**，改一个必须改另一个：原来 `24h × 14 = 14 天`；压到 6h 而份数不动会**静默缩到 3.5 天**。现在 6h × 56 = 14 天（pg）、6h × 28 = 7 天（qdrant）。改间隔还要**同步改** `alerts.yml` 里 `BackupLoopFailing` 的 54000s（15h）阈值 | `6 / 56 / 28`；RPO 要求更严就继续压间隔 |
| `CANARY_IMAGE` | 由 `deploy/release.sh` 注入 | 灰度实例的镜像引用。**不要手工 export 它**：发布/回滚都走脚本（`deploy/release.sh` / `deploy/rollback.sh`），否则容易在"改完忘记 `--env-file`"和"忘记 reload"这两处出错 | `ghcr.io/<org>/chatagent:sha-<短哈希>` |

## 三、可观测（强烈建议）

| 变量 | 当前状态 | 说明 |
|---|---|---|
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` | 未设置 | Langfuse 链路。**三填二**（host 可留空用官方云）；与 LangSmith 二选一 |
| `LANGSMITH_API_KEY` / `LANGCHAIN_PROJECT` | 未设置 | LangSmith 链路，LangGraph 原生支持 |
| `METRICS_ENABLED` | ✅ `1` | `/metrics` 已开 |
| `LOG_FORMAT` | ✅ `json` | 一行一 JSON，便于日志系统检索；本地想看人眼友好改 `text` |

> 两组 tracing 都留空时行为与"没有这层代码"完全一致（只记一条 `tracing 未配置` 日志）。

**告警要真正接上**：`deploy/monitoring/alerts.yml` 里有 **5 组 34 条**规则
（可用性 / SLO / 成本 / 安全 / **平台自身**）。其中两条是"任何一次即 critical"：
`TenantMismatchDetected`（有人在冒充租户）、`SessionOwnershipViolation`（有人在猜
`thread_id` 串读会话）—— 这两条一旦触发就是**正在被攻击**，不能只当普通告警。

最后那一组（`platform`，2026-09-30 新增 6 条）回答的是**"谁来发现监控坏了"**：
`Watchdog`（恒 firing 的心跳）、`MonitoringStackDown`、`AlertDeliveryFailing`、
`BackupLoopFailing`、`BackupMetricsMissing`、`DiskSpaceLow`。
它存在的理由很具体 —— 上面四组监控的都是业务，**没有任何一条监控监控栈自己**，
于是"整条告警链路安静地失效"和"真的没故障"在收件箱里长得一模一样。

**但"规则写好了"离"告警送到人"还有四环**（完整链路与断开时的现象见
`deploy/RUNBOOK.md` §1.1）：

```
应用 /metrics → Prometheus 判定 → Alertmanager 路由 → alert-sink 转格式 → 机器人
```

当前状态：四环都已落盘并可跑（`deploy/app/docker-compose.yml` 里的
`prometheus` / `alertmanager` / `alert-sink` 三个服务 + 三份配置），
**最后一环缺凭据** —— `ALERT_SINK_*_WEBHOOK` 为空时 `alert-sink` 只记日志。
填完之后**必须真触发一次告警**确认收到（`deploy/RUNBOOK.md` §1.2 的第 ③ 步：
停掉 `app-canary` 让 `ChatAgentDown` 响一次）。只看 Prometheus 页面上的 FIRING
**不能**证明通知送达 —— 那是最常见的一种误判。

> 一个必须知道的结构性局限：`MonitoringStackDown` 由 Prometheus 自己求值，
> 所以 **Prometheus 挂了它不会响**；Alertmanager 挂了则"响也送不出去"。
> 穿透这两个死角的只有 `Watchdog` —— 约定**每 24h 应收到一次心跳**，
> 收不到就按 `RUNBOOK.md` §3.5 逐环排查。

**错误预算政策**：`deploy/monitoring/ERROR_BUDGET_POLICY.md` 定义了"预算烧掉之后
到底会发生什么"（冻结发布 / 只允许修复类变更 / 何时可带债发布）。
没有这份政策，错误预算就只是仪表盘上的数字。

**SLO 与错误预算**：`deploy/monitoring/SLO.md` 是 SLO 的**定义**（唯一事实源），
`alerts.yml` 的 `chatagent-slo` 组是它的实现，两者由
`tests/test_monitoring_config.py::test_slo_doc_matches_alert_expressions` 逐行对齐 ——
只改一处会立刻测红。两个 SLI：

| SLI | 目标（30 天） | 口径 |
|---|---|---|
| `availability` | 99.5% | 非 5xx 请求占比（**不要**用 `http_errors_total` 当分子，它只统计未捕获异常） |
| `latency` | 99% ≤ 1s | 仅**非流式**路由（`/v1/chat.*` 是 SSE 长连接，算进来会恒不达标） |

⚠️ 部署前必查一项：`alerts.yml` 里 `le="1.0"` 的**书写格式必须与指标实际渲染一致**。
PromQL 是全串匹配，写成 `le="1"` 不会报错，只会让该规则**永不触发** —— 已由
`test_alert_le_labels_match_exposed_format` 钉住。

## 四、审核通知（human-in-the-loop 的闭环）

| 变量 | 当前状态 | 说明 |
|---|---|---|
| `REVIEW_NOTIFY_WEBHOOK` | ⚠️ 空 | 有待审内容时 POST 的地址。**留空 = 只落队列、不发通知** ⇒ 那一刻没人开客户端就没人知道有内容待审，"人工审核"这条闭环在事实上断掉 |
| `REVIEW_NOTIFY_FORMAT` | ✅ `generic` | `generic`（自建网关，JSON）/ `wecom`（企业微信机器人）/ `feishu`（飞书机器人） |

配套：`/metrics` 里 `review_pending` 是 **Gauge**（不是计数器）—— 积压久了本身就是故障，
告警应盯着"长时间 > 0"而不是"增速"。

## 五、策略开关（默认已安全，按需收紧）

| 变量 | 当前 | 说明 |
|---|---|---|
| `CORS_ALLOW_ORIGINS` | 未设置（=`*`） | `allow_credentials` 恒为 false（鉴权走 `X-API-Key` 头而非 Cookie），所以 `*` 在浏览器侧合法，不构成漏洞；但生产建议给白名单 |
| `RAG_GUARD_ACTION` | ✅ `warn` | 提示词注入防护动作：`warn`（只告警）/ `strip`（剥离可疑句）/ `block`（丢弃可疑块）。**结构隔离已默认生效**（`<资料>` 边界 + 声明不执行其中指令）。语料来源不可控时建议提到 `strip` |
| `RAG_TENANT` | ⚠️ 空 | 兜底租户，仅在**单租户模式**下生效。留空 = 不隔离。多租户请改用 `TENANT_KEYS`，别靠这个 |
| `RAG_PARSE_CACHE` | `1`（默认） | 文档级解析结果落盘缓存。语料没变时跳过"重新解析 + 切分"（docling/OCR 是分钟级开销）；多 worker 收益翻倍 |
| `RAG_BREAKER` / `CIRCUIT_FAIL_THRESHOLD` / `CIRCUIT_RESET_SECONDS` | `1` / `5` / `30` | 出站调用（rerank / embedding）熔断参数 |
| `RATE_LIMIT_PER_MINUTE` / `RATE_LIMIT_SESSION_PER_MINUTE` | `120` / `120` | 按 IP、按会话两个维度的每分钟上限（跨端点累计） |
| `RATE_LIMIT_ENABLED` / `RATE_LIMIT_BY_SESSION` | `1` / `1` | 上面两个维度各自的开关 |
| `QUOTA_ENABLED` | `1` | 配额总开关（限额为 0 时仍等于不限） |
| `MAX_CONCURRENT_REQUESTS` | 未设置（`0` = 不限） | **单进程**在途请求上限（背压）。限流限的是"每分钟新请求数"，管不住**长连接驻留**：主接口是 SSE，一个请求几十秒，按限流上限开若干连接并保持不关就能把实例占满而所有指标正常。超限返回 `503 + Retry-After`。⚠️ `0` 时饱和率指标**不存在**，告警不会触发 —— 面板显示 0 是"没有闸门"不是"很空闲"。⚠️ 多 worker 时实例上限 = worker 数 × 该值 | `100`~`300`（按 `CAPACITY.md` 的并发配额定）；**先确认内存与上游配额** |

## 六、已有值（复核无误，沿用即可）✅

| 变量 | 当前值 | 说明 |
|---|---|---|
| `AGNES_API_KEY` / `AGNES_API_BASE` / `AGNES_MODEL` | 已配 / `apihub.agnes-ai.com/v1` / `agnes-2.0-flash` | 主模型（生成） |
| `EMBEDDING_API_BASE` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` | 已配 / SiliconFlow / `BAAI/bge-m3` | 检索用 embedding，与 LLM 分开配 |
| `EMBEDDING_QUERY_PREFIX` | 已配（英文指令） | BGE 系靠它拉开区分度（实测 Top1 38% → 62%） |
| `DB_URL` | 已配（指向容器内 PG） | 会话检查点 **+** pgvector **+** 审核队列 **+** 会话归属 共用 |
| `RAG_VECTOR_STORE` / `RAG_EMBEDDING` | **`qdrant`** / `hybrid` | 稀疏（BM25 哈希稀疏向量）+ 稠密（BGE-M3）**同库同集合**，双路 RRF 融合。⚠️ 2026-09-29 从 `pg` 迁到 `qdrant` —— 本文档 §六 此前仍写着 `pg`，属文档漂移，已修正；同类漂移由 `tests/test_doc_drift.py` 钉住 |
| `QDRANT_URL` | 本机 `http://localhost:6333`；容器内由 compose 覆盖为 `http://qdrant:6333` | 容器里**必须**用服务名 —— `localhost` 指容器自身，连不上会**静默回退 memory**（检索仍可用，但向量库等于没接上） |
| `DOCLING_ARTIFACTS_PATH` / `DOCLING_DEVICE` / `DOCLING_OCR` / `HF_ENDPOINT` | `./models/docling` / `cpu` / `auto` / `hf-mirror.com` | 离线解析链路 |

> 另外 10 个键在 `.env` 里存在但**值为空**（`SERVE_API_KEY`、`REDIS_URL`、
> `REVIEW_NOTIFY_WEBHOOK`、`RAG_TENANT` 等）—— 空值在这套代码里的语义是"未启用"，
> 不是"用默认值"。这正是上面哪些项标 ⚠️ 的依据。

---

## 怎么确认"我填的真的生效了"

配置错配最典型的表现是**静默不生效**（配了但没读、读了但被覆盖）。两个可靠的核对点：

```bash
# ① 启动日志里有一行结构化回显（不含任何密钥，只回显布尔/条数）
python -m app.serve 2>&1 | grep 服务启动配置
#   'env': 'production', 'auth': {'required': True, 'global_key': False, 'tenant_keys': 2},
#   'docs': False, 'proxy': {'trust_forwarded_for': True},
#   'tenant': {'mode': 'key-bound'}, 'rate_limit': {...}, 'quota': {...}
#   ↑ mode 是 key-bound 才说明租户不可伪造；tenant_keys 是条数（不泄露密钥）

# ② 就绪探针真探依赖，四项各自带耗时
curl -s localhost:8000/health/ready | python -m json.tool
```

> 配置回显**刻意不挂 HTTP 端点**：模型名、目录、开关名属于部署细节，而 `/health/*`
> 在反代下是公网可达的。

## 启动方式（重要变更）

```bash
# 生产 / 本地统一入口
python -m app.serve

# 等价手写形式
uvicorn app.main:app --loop app.serve:loop_factory

# 容器
docker compose --env-file .env -f deploy/app/docker-compose.yml up -d --build
```

**为什么不能再用裸 `uvicorn app.main:app`**：检查点持久化用 `AsyncPostgresSaver`，
底层 psycopg 的**异步**连接**不支持 Windows ProactorEventLoop**，而 uvicorn 在 win32
上恰好默认用它。裸启动时服务照样能起，但会**静默降级为内存检查点（重启丢状态）**。
`app/serve.py` 注入平台自适应的事件循环工厂解决这件事。

**怎么确认持久化真的接上了**（这是唯一可靠信号）：

```bash
curl -s localhost:8000/health/ready | python -m json.tool
```

看 `checks.checkpointer`：
- `AsyncPostgresSaver（已持久化）` ← 正常
- `InMemorySaver（内存，重启丢状态！）` ← 降级了，回看启动日志里的 warning（会点名原因）

## 七、告警邮件通道（可选，**默认关闭**）

邮件**不是** `alert-sink` 的通道之一（那个网关只认 wecom / feishu / generic 三种，
见 `deploy/monitoring/alert_sink.py` 里定义通道类型的那一行）。所以**邮箱地址不填在 `.env` 里**，
走的是 **Alertmanager 原生 `email_configs`**：配置在 `deploy/monitoring/alertmanager.yml`，
**启用步骤（5 步）与四个必踩的坑写在该文件末尾**那一节（`片段①/②/③`），照着做即可。

这里只重复三条最关键的：

| 要点 | 说明 |
|---|---|
| **密码不进版本库** | 用 `smtp_auth_password_file`（v0.27 支持，与 `smtp_auth_password` **互斥**）。密码文件放 `deploy/monitoring/secrets/`，该目录已进 `.gitignore`，并由 `tests/test_monitoring_config.py::test_smtp_password_can_never_be_committed` 用 **`git check-ignore` 实测**钉住。之所以能做到这点：Alertmanager 虽然**不支持 `${VAR}` 插值**，但它给了 `_file` 这条路 |
| **为什么默认是关的** | ① 邮件发不出去会让 `AlertDeliveryFailing`（**critical**）长鸣 —— 长期不消失的 critical 等于没有告警；② `_file` 是**装配期**读取：密码文件不存在时 Alertmanager **拒绝启动**。默认打开 = "不用邮件的人起不来 + 永久假告警" |
| **收不到「已恢复」** | `email_configs.send_resolved` 默认 **false**，必须显式写 `true`（现有 `webhook_configs` 都写了，别漏这一处） |

⚠️ 启用后自检**两步都要做**：`logs alertmanager \| grep "Completed loading"` 只证明
**配置装载成功**；必须再**真触发一次告警**（`stop app-canary`）确认邮箱收到 ——
因为邮件是唯一不经 `alert-sink` 的通道，`alert_sink.py --selftest` 全绿证明不了它能到。

## 上线前自检清单

**配置与鉴权**

1. [ ] `APP_ENV=production`（并为 `ENABLE_DOCS` 的推导结果负责）
2. [ ] `TENANT_KEYS` 或 `SERVE_API_KEY` 已填，且用错误的 key 调 `/v1/chat` **能拿到 401**
3. [ ] 多租户下：用 A 租户的 Key 配 B 租户的 `X-Tenant-Id` **能拿到 403**
4. [ ] `GET /docs` 返回 **404**
5. [ ] 反代场景 `TRUST_PROXY_HEADERS=1`，并用不同来源 IP 验证限流**分别**计数
6. [ ] `LLM_MAX_TOKENS_PER_REQUEST` / `USER_DAILY_TOKEN_QUOTA` 已设非 0

**依赖与数据**

7. [ ] `/health/ready` 四项全 ok（llm / db / vector_store / checkpointer）
8. [ ] `checks.checkpointer` 显示 `AsyncPostgresSaver（已持久化）`
9. [ ] `python -m app.migrations --status` 退出码 0（缺迁移 / 维度不符都会非 0）
10. [ ] `python -m scripts.pg_backup drill` 跑通（**没恢复过的备份不算备份**）。
       它会把结果写进 `backups/drill-pg.prom`；**这一点必须核对** —— 没写出来就说明
       指标没发布，`BackupDrillMissing` / `BackupDrillOverdue` / `BackupDrillFailed`
       三条告警全成了哑弹（没触发 ≠ 没问题）

**部署产物（compose 里的默认值是"本机开发用"的，不是生产值）**

11. [ ] `POSTGRES_PASSWORD` 已改成强口令（两处 compose 都从这一个变量取，改一处即可）；
       ⚠️ 它**只在数据卷首次初始化时生效** —— 卷已存在时改它没效果，需 `ALTER USER` 或删卷
12. [ ] 数据库端口仍是 `127.0.0.1:5433:5432` 这个**回环绑定**（不要改成 `5433:5432`，
       那等于把库发布到 0.0.0.0"）
13. [ ] 对外入口走 `nginx.tls.conf`（443 + HSTS），而不是只开 8080 明文
       ⚠️ 同时把 `deploy/monitoring/prometheus.yml` 里 `blackbox-tls` 的
       `https://REPLACE-WITH-YOUR-DOMAIN/health/ready` 换成**同一个真实域名**。
       它是 PromQL 里的普通字符串，**写错不会报任何错**，只会让该 target 的
       `probe_success` 停在 0 ⇒ `ExternalProbeFailing` 恒响（狼来了，久了就没人看）；
       更糟的一种是探针自身没产出序列时那条规则**根本不触发**（见 `RUNBOOK.md`
       §3 那张表："用没有序列表达故障"）。两种都要靠这一步避免。
14. [ ] **`docker compose ... config` 自证插值生效**（2026-09-30 新增，最容易被跳过的一条）：
       ```bash
       docker compose --env-file .env -f deploy/app/docker-compose.yml config | grep -iE 'webhook|POSTGRES_PASSWORD'
       ```
       它打印的是**插值之后**的最终配置。若这里看不到你填的值，说明 `--env-file` 漏了
       （compose 只在**第一个 `-f` 文件所在目录**找 `.env`，也就是 `deploy/app/`，
       仓库根的 `.env` 不会自动生效）—— 而**没有任何报错**，只有默认值悄悄生效。

**可观测与闭环**

15. [ ] `/metrics` 能抓到 `http_requests_total`、`llm_tokens_total`、`review_pending`
16. [ ] `deploy/monitoring/alerts.yml` 已加载（**5 组 34 条**），且 `review_pending` 有"长时间 > 0"的告警
17. [ ] `chatagent-slo` 组已加载（面板上能看到 5 条燃尽率规则），
       且 `SLO.md` 的燃尽率表与表达式一致（`pytest tests/test_monitoring_config.py` 会钉住）
18. [ ] **`Watchdog` 心跳真的到达了**：等到下一个 `repeat_interval`（24h），确认通道上收到它。
       这是唯一能证明"整条链路（采集→判定→路由→送达）是活的"的方式 ——
       没有它，"今天没有告警"分不清是"没故障"还是"链路早断了"。
       同时确认 `MonitoringStackDown` 的四个 job 在 `/targets` 里都是 UP。
19. [ ] `REVIEW_NOTIFY_WEBHOOK` 已配，走一次低分对话**真的收到通知**
20. [ ] `GET /v1/reviews` 能列出待审记录
21. [ ] 多副本时 `REDIS_URL` 已配，且额度在副本间共享

**平台自身（2026-09-30 新增）**

22. [ ] `curl -s localhost:9100/metrics | grep -E 'node_filesystem_avail_bytes|backup_'` 有数据
       （前者证明 node-exporter 读到了**宿主机**文件系统；后者证明备份循环的指标链路是通的）
23. [ ] 各服务的 `logging` 轮转生效：`docker system df -v` 的 LOGS 一列不应超过
       「服务数 × 30MB」太多。**没有它 + 没有磁盘告警 = 一条完整的宕机路径**：
       日志撑满盘 → PG 写不进去 → 表现为随机 500（而不是"磁盘满"）
24. [ ] `retention-sweep` 在跑且 `RETENTION_DAYS` 已设非 0
       （`docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=20 retention-sweep`）
25. [ ] `python scripts/backup_manifest.py` 退出码为 0（pg 与 qdrant 的最新备份成对）
26. [ ] `deploy/release.sh <镜像> --dry-run` 能跑通（发布流程的"空跑"，
       顺便验证 `scripts/nginx_weight.py` 找得到 `nginx.conf` 里的权重行）

**成本护栏**

27. [ ] `scripts/load_test.py --endpoint chat --yes-spend --disconnect-after 3` 跑一次，
       `/metrics` 的 `client_disconnected_total` 有增长、`llm_tokens_total` 明显小于"不打断"的对照

**CI 门禁（不是"建议"，是"部署产物被验证过"的前提）**

28. [ ] `image` job 全绿 —— 它验证的是**镜像**：能构建、容器能起来并通过
       `/health/live`、漏洞扫描无 HIGH/CRITICAL。三类问题只有"真构建 + 真启动"才暴露：
       lock 钉了不存在的版本、`COPY` 少拷目录、缺系统库 / `CMD` 写错
29. [ ] `.trivyignore` 里没有**无到期日**的忽略项。没到期日的忽略 = 永久忽略 =
       悄悄把这条规则删掉（CI 已开 `ignore-unfixed`，无补丁的 CVE 本来就不卡 CI，
       所以这里只该放"可修但暂不升"的项）

**告警邮件（可选通道，只在启用它之后才需要勾）**

30. [ ] `docker compose ... logs alertmanager` 里能看到 `Completed loading`，
       且 `片段①/②/③` **要么三处都取消注释、要么一处都没有**（只放开一半的表现是
       `failed notifications` 里出现 `integration=email`，而你看不出少了哪一半）
31. [ ] `deploy/monitoring/secrets/smtp_password` 存在、权限 `600`，且
       `git check-ignore deploy/monitoring/secrets/smtp_password` **返回 0**
       （返回 1 就说明 `git add -A` 会把 SMTP 密码提交进版本库）
32. [ ] **邮箱里真的收到过一封告警**，并且触发它的那次故障恢复后**也收到了「已恢复」**
       （后者专门验证 `send_resolved: true` 没漏）

**知识库生命周期（只有启动过一次灌库才需要勾）**

33. [ ] `python scripts/tasks.py kb-prune` **退出码不是 2**。退出码 2 = 语料目录扫不到
       任何受支持的文件（几乎总是"语料没挂进容器"或 `DOCS_DIR` 指错）——
       此时全库都会被判成孤儿，所以脚本拒绝执行。**别把它当成"清理失败"去修**：
       它掩盖的是"检索恒空"这个更严重的问题。
       顺带确认输出里的 `无法判定 N 份` 是 0（非 0 说明库里还有本次改造前灌的、
       没有内容指纹的老数据 —— 它们永远不会被清理，重新灌一次库即可补上）

**背压（只有配了 `MAX_CONCURRENT_REQUESTS` 才需要勾）**

34. [ ] **按下述三步之一验证闸门真的在拦**（不验等于没配 —— 它平时完全静默）：
       ① 把 `MAX_CONCURRENT_REQUESTS` 临时设成 `1` 并重启；
       ② 同时开两个 SSE 请求（`curl -N .../v1/chat`），第二个应当**立刻**收到
       `503` 且响应头带 `Retry-After`；
       ③ 在此期间 `curl /health/live` 必须**仍然是 200**（豁免生效），
       随后把限值改回生产值。
       顺带核对启动日志里 `limits.max_concurrent_requests` 就是你填的值
       （**多 worker 时实例实际上限 = worker 数 × 该值**）。

**向量索引身份（用 pg / qdrant 后端时必须勾）**

35. [ ] 启动日志里有 **`索引身份自检：status=...`** 这一行。
       - `status=recorded` —— **首次**记录（本次改造之前灌的库都走这里）。
         ⚠️ 它**只记录当下**，证明不了库里既有向量是同一个模型灌的：
         存量库请人工核对一次当前 `EMBEDDING_MODEL` 与当初入库时是否一致。
       - `status=ok` —— 库里的向量与当前模型一致，之后每次启动都会重判。
       - `status=mismatch` —— ⚠️ **库里的向量是另一个模型灌的**（换过模型但维度
         恰好没变时最隐蔽：检索质量当场崩掉，而 QPS / 延迟 / 错误率全部正常）。
         处置见 `deploy/RUNBOOK.md` §3.1 `IndexIdentityMismatch`。
       - `status=unknown` —— 表/集合还没建（首次灌库之前属正常）。
       - `status=error` —— 自检读不到向量库（按日志里的异常修连接参数）。
       另外确认 `/metrics` 里**有** `rag_index_identity_ok`（`1`=一致、`0`=不一致）：
       它**缺失**说明自检没跑成 —— 那对应告警 `IndexIdentityNeverReported`，
       别把"没检查"读成"没问题"。
