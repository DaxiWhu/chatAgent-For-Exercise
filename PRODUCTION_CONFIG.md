# 生产化配置清单（需要你填的都在这）

> 本文件只回答一个问题：**哪些环境变量还需要你填值**，以及**不填的后果**。
> 其余配置都有"安全且不改变既有行为"的默认值，不填即可跑。
>
> 状态列是 **2026-09-29 对 `.env` 的实测复核结果**（用解析后的值判定，
> 不是 `grep` 猜的 —— `.env` 是 CRLF 行尾，用 shell `[ -z "$v" ]` 判空会把
> 空值行读成 `"\r"` 而误判为"已填"，我上一轮就是这么得出错误结论的）。
>
> ⚠️ **这批状态与 9-28 那版有实质差异**：成本刹车与单请求总时限已按下面的建议值落盘
> （见 `git diff .env.example` 与 `.env.bak`）。
> **另：`SERVE_API_KEY` / `REQUIRE_AUTH` 两行已于 2026-10-05 从 `.env` 里删除**
> （对应的代码路径已整体删除，留着它们会让服务**拒绝启动**）—— 详见下面第一节。
> 仍为 ⚠️ 的只剩真正需要你做决定的那几项 —— 其中**告警通道 webhook 是最紧的一个**：
> 不填，前面所有告警配置都只是面板上的灯，事件永远送不到人。

图例：**⚠️必填** / ○可选（不填有安全默认）/ ✅已有值（沿用）

---

## 一、必须处理（当前状态就有真实风险）

| 变量 | 当前状态 | 不填的后果 | 建议值 |
|---|---|---|---|
| `SERVE_API_KEY`（共享密钥） | ✅ **已删除**（2026-10-05；`.env` 里那一行已去掉） | 不再是"配置项"，是**禁止出现的变量**：环境里存在它（**留空也算**）会让服务**拒绝启动**（`app/main.py::_assert_no_legacy_auth_knobs`）。它被删而不是被"改成默认关闭"，是因为三条缺陷都是**结构性**的：**分不出身份**（审计与审核留痕里 `user` 恒为空）、**撤不掉**（单值、无过期、无重叠轮换窗口）、**带不了租户**（租户由 `X-Tenant-Id` 自报且被采信 ⇒ 换一个头就切检索分区、并绕过按租户分桶的配额）。脚本与 CI 一律走「账号密码 → `POST /v1/auth/login` → Bearer 令牌」（`scripts/script_auth.py`） | **整行删除**（不是留空） |
| `REQUIRE_AUTH` | ✅ **已删除**（2026-10-05） | 它存在的唯一意义是"关掉鉴权要求"，而匿名兜底已经删除 ⇒ 鉴权要求现在是**结构性**的。同 `SERVE_API_KEY`：环境里存在它（留空也算）会让服务**拒绝启动** | **整行删除** |
| `AUTH_SESSION_SECRET` + 账号表 | ⚠️ 未配（= 未启用用户鉴权） | **这是现在**唯一**的"服务鉴权"**：不配它就没有任何人类用户可以登录，生产环境（`APP_ENV=production`）会**拒绝启动**（见 `app/main.py::_assert_auth_policy`）。⚠️ **没有任何环境变量能关掉这条检查** —— 曾经能"绕开鉴权要求"的两个开关（`SERVE_API_KEY` / `REQUIRE_AUTH`）都已删除。要启用需要两件事：① `AUTH_SESSION_SECRET`（`openssl rand -hex 32`）；② 一个可用的账号存储 —— 配了 `DB_URL` 就是 PG 的 `users`/`tenants` 表，没配则落**进程内存**（启动期告警，且多副本下"A 副本建的用户 B 副本不认"） | `AUTH_SESSION_SECRET` 必填；`DB_URL` 必填（否则账号只在单进程内存里） |
| `AUTH_USERS`（种子） | ⚠️ 未配 | ⚠️ **语义已变**：用户与租户住在**账号表**里，这个变量不再是"用户表"，唯一作用是**解鸡生蛋** —— 仅在账号表为空时用它建出第一个管理员。不配它就没有第一个管理员（除非开自助注册）。**改它不会改动已有用户** | 只在首次初始化时配一条管理员；之后增删用户走 `POST /v1/admin/users` |
| `AUTH_ADMIN_ROLES` | 默认 `admin` | 谁能管用户与租户（增删改查 + 审批加入申请）。默认值是 **fail-closed** 的：留空 = 没人能管。✅ 2026-10-05 起 `AUTH_REVIEWER_ROLES` 也统一为 fail-closed（默认 `reviewer`），两者不再刻意相反 | 保持默认；放权给别的角色名时显式列出 |
| `AUTH_REVIEWER_ROLES` | 默认 `reviewer` | 谁能审稿。⚠️ **2026-10-05 改**：此前默认空 = **不限制**（任何登录用户都能审，包括还没有租户的新账号）；现在不配 = 只有带 `reviewer` 角色的人能审，要放开必须**显式写空**。**对每一种身份都生效**：共享密钥路径那条"跳过角色检查"的豁免已随该路径一起删除 | 保持默认；放权给别的角色名时显式列出 |
| `AUTH_ALLOW_SELF_REGISTER` | 默认 `false` | 打开后任何人可 `POST /v1/auth/register` 建账号。⚠️ 注册出来的账号**没有租户**，而无租户只能读到**公共租户**（`RAG_TENANT`）的文档。所以这个开关的实质是"要不要把公共分区开放给外部自助用户" | 默认关闭；开放前先决定 `RAG_TENANT` 里放什么 |
| `APP_ENV` | ○ **`.env` 里仍未设置（刻意）**；容器部署由 compose 注入 `production` | 本机直跑 `python -m app.serve` 时是 `development`（`/docs` 可见）。⚠️ **它只影响文档端点与启动期自检的严苛程度** —— 鉴权要求**不由它推导**（"没有凭证 ⇒ 401"在任何环境下都是结构性的）。⚠️ **判据 2026-10-05 已改为「黑名单认开发」**：只有 `development`/`dev`/`local`/`test`/`testing` 算非生产，**其余任何取值一律视为生产** —— 于是 `prd`/`live`/`staging` 这类"认不出来的值"会落到生产（关 /docs、启动期严格自检），不再有静默降级。两条防线：① `deploy/app/docker-compose.yml` 的 `APP_ENV: ${APP_ENV:-production}`；② `deploy/app/docker-compose.staging.yml` 里**写死** `production`（不用插值，避免被部署机上的环境变量带偏）。**用非 compose 方式部署时不必再记特定字面值，但显式写 `production` 最清楚** | `production`（部署时） |
| `LLM_MAX_TOKENS_PER_REQUEST` | ✅ `8000` | 单请求 token 上限已装。原来 `0` = 不限，一次超长输入 / 模型跑飞就会**无限烧钱** | `4000`~`8000` |
| `USER_DAILY_TOKEN_QUOTA` | ✅ `200000` | 每租户日额度已装。这是**按估计**定的 —— 有了真实用量后应据 `llm_tokens_per_request` 的分布回填 | 按成本预算定，如 `200000` |
| `REQUEST_TIMEOUT_SECONDS` | ✅ `180` | 单请求**总时限**已装。注意 `_MODEL` 上的 `timeout=30` 只管**单次调用**，而一次请求可以跑多轮（自评审不通过会再来一轮）⇒ 没有它总时长无上界，而且 **`GRACEFUL_SHUTDOWN_TIMEOUT` 的排空上界会形同虚设**（它等的是在途请求，而在途请求自己不会结束）。超时会下发结构化 `timeout` 事件后收流，**已产出的正文照常交付**。<br>⚠️ 它同时是**发布时能放完多久请求**的准数：排空上界默认 = 本值 + 10s（见下一行），容器宽限期必须大于排空上界 | `120`（保守可给 `180`） |
| `GRACEFUL_SHUTDOWN_TIMEOUT` | 未设（默认 = `REQUEST_TIMEOUT_SECONDS + 10`） | uvicorn 等在途请求的**排空上界**。默认值**跟随请求总时限**，不再写死 30s —— 写死 30 时，一次发布会把所有跑过 30 秒的请求取消，而它们本来还被允许再跑 150 秒。那 10s 不是"再跑一会儿"，是**收尾动作**（下发 timeout 事件、结算配额、写待审队列、关上游）。<br>⚠️ 三值联动：**请求总时限 ≤ 排空上界 < 容器宽限期**（当前 `180 ≤ 190 < 210`，宽限期在 `deploy/app/docker-compose.yml`）。显式把它配得比总时限小**会照用但打一条警告**（启动日志搜「断在半句」）。<br>⚠️ 总时限为 `0`（不限）时默认退回 30s —— 那种配置下没有任何取值是对的，排空上界只是"早死晚死" | 不设（跟随总时限）；确实要设就设成 ≥ 总时限 + 10，并同步调大容器宽限期 |
| `TRUST_PROXY_HEADERS` | ✅ `0` | 直连场景下 `0` 是**正确**的。但**一旦部署到 Nginx/网关后面**必须改 `1`：否则限流按**代理 IP** 统计，全体用户共用一个计数桶 ⇒ 要么集体误伤、要么被单人吃满。compose 里已为两个 app 服务注入 `TRUST_PROXY_HEADERS: "1"`（它们确实在 nginx 后面）。<br>⚠️ **设 `1` 只是必要条件**：代理还必须用**覆盖式**写入 `X-Forwarded-For`（`$remote_addr`）。若代理用 nginx 的 `$proxy_add_x_forwarded_for`（**追加式**，客户端那段留在最左），应用取到的第一段仍是**客户端伪造的值**，限流照样被绕过（2026-10-04 修，守卫见 `tests/test_deploy_topology.py`） | 反代后设 `1`；直连保持 `0` |
| `ALERT_SINK_WECOM_WEBHOOK` 等三个通道 | ⚠️ **全部为空**（当前**最紧的一个待办**） | 告警转发的出口。**不填 ⇒ `alert-sink` 只把事件记进日志**（这是刻意设计：没配通道不阻塞、不报错），于是 Prometheus 里的 FIRING 永远变不成一条发到人手里的消息。**6 组 67 条**规则 + 六级路由全都写好了，差的只有这一步。<br>⚠️ **两个坑**：① 填完必须用 `docker compose --env-file .env -f deploy/app/docker-compose.yml config \| grep -i webhook` 自证值真的传进去了（**漏 `--env-file` 时它静默不生效**，见本文档开头的说明）；② `alert_sink.py --selftest` 只能证明网关进程活着，**必须再真触发一次告警**（停 `app-canary` 让 `ChatAgentDown` 响）才算闭环 | 填企业微信/飞书机器人 URL；自建网关填 `ALERT_SINK_GENERIC_WEBHOOK` |
| `ALERT_SINK_SHARED_SECRET` | ⚠️ 为空 —— **刻意保持为空，别单独填它** | 网关的来源校验：非空时要求每个 POST 带 `X-Alert-Sink-Secret` 头且匹配，否则回 **403**。⚠️ **陷阱就在这里**：`alertmanager.yml` 的四个 receiver **没有** `http_config`，**根本不会送这个头** ⇒ 你填了这个变量，等于**把全部告警打成 403**（Alertmanager 重试几次后放弃，表现成"配置全对但没人收到"，网关日志里只有一行 403）。要用它必须**两处一起改**：给四个 receiver 补 `http_config: http_headers: X-Alert-Sink-Secret: [...]` —— 而 Alertmanager **不支持 `${VAR}` 插值**，密钥只能写进配置文件，又回到"凭据进版本库"（这正是本网关要避免的事）。<br>**当前实际暴露面本就有限**：宿主机端口是回环绑定（`127.0.0.1:8081:8080`），仅剩"同 compose 网络内其它容器可伪造告警"，那更适合用部署侧网络隔离解决 | **留空**。确实要开 ⇒ 两处一起改，并接受密钥进配置文件 |
| `REDIS_PASSWORD` / `POSTGRES_PASSWORD` / `GRAFANA_PASSWORD` | ✅ `REDIS_PASSWORD` 已生成；另两个走 compose 默认值 | 编排层口令（由 compose 从 `.env` 插值，`app` 不读）。**`POSTGRES_PASSWORD` 仍是默认 `123456`** —— 上生产前必须换，且注意它**只在数据卷首次初始化时生效**（详见 `deploy/SECRETS.md` §4.3）。密钥管理的整体方案见 `deploy/SECRETS.md` | 见 `deploy/SECRETS.md` |

> ⚠️ `TRUST_PROXY_HEADERS` 两个方向都是真实故障：**直连却设 1**，客户端加一个
> `X-Forwarded-For: 1.2.3.4` 就换一个限流桶，限流形同虚设（该头客户端可任意伪造）。
> ⚠️ 2026-10-04 补：**反代场景同样会中这一枪**，而且更隐蔽 —— 设了 `1`（配置看起来完全
> 正确）、限流器照常工作、日志照常打，但只要代理用**追加式**写这个头
> （`$proxy_add_x_forwarded_for`：客户端自带的那段原样留在最左），应用取到的"第一段"
> 就仍是客户端写的值 ⇒ 换个伪造值换一个桶。
> **判断办法**：看代理配置里这个头是由 `$remote_addr`（覆盖，对）还是由
> `$proxy_add_x_forwarded_for`（追加，错）赋值的。本仓库两份 nginx 配置都已是覆盖式。
>
> ✅ 2026-10-06 补：**配错这件事现在会在运行时自己喊出来**。限流器会记住"每个
> (对端地址, 路由) 背后出现过几个不同用户"，攒到 4 个就判定这是**共享出口**，并报：
> · `TRUST_PROXY_HEADERS=0` ⇒ **ERROR**（这是配置错，修法是打开它）+
>   `ratelimit_shared_egress_total{trusted="no"}` + 告警
>   `RateLimitSharedEgressWithoutTrustedProxy`（见 `deploy/RUNBOOK.md`）；
> · `TRUST_PROXY_HEADERS=1` ⇒ **WARNING**（那是 NAT/公司出口，改配置解决不了）。
> 判定共享之后，**已验证身份**的请求会改按主体分桶 —— 同出口的同事不再互相抢额度；
> 未验证的自报主体（登录/注册）**不参与**分桶（否则换个名字就换个桶）。
> 另有一道 nginx 兜底：认证端点单独一个按真实 IP 的硬帽子（20r/m + burst 10，
> 见两份 `nginx*.conf` 的 `chatagent_auth` zone），保证**单个**客户端占不满应用侧那个
> 桶 —— 但它治不了"全站共用一个桶"，只是让故障不至于变成"一个人堵住所有人"。

### 一之二、用户鉴权与账号体系（2026-10-04 新增）

**统一口径：所有人类用户都用 JWT 登录鉴权，用户身份与租户身份都在令牌载荷里** ——
`sub`（用户名）、`tenant`（租户名，**没有租户就是空串**）、`roles`。此外没有第二条
人类身份路径：旧的"租户 ↔ API Key 反查表"（TENANT_KEYS）已删除。

数据模型是**多对一**：一个用户最多属于一个租户（`users.tenant` 单列，可空）。

| 能力 | 接口 | 谁可以调 |
|---|---|---|
| 自助注册（默认关闭） | `POST /v1/auth/register` | 无需凭证（靠限流防刷） |
| 改自己的密码 | `POST /v1/auth/password` | 本人（需验旧密码，改完清掉该用户全部会话） |
| 申请加入租户 / 查自己的申请 | `POST /v1/join-requests`、`GET /v1/join-requests/me` | 本人 |
| 租户增删改查 | `POST/GET /v1/admin/tenants`、`GET/PATCH/DELETE /v1/admin/tenants/{name}` | 管理员角色 |
| 用户增删改查 | `POST/GET /v1/admin/users`、`GET/PATCH/DELETE /v1/admin/users/{username}` | 管理员角色 |
| 审批加入申请 | `GET /v1/admin/join-requests`、`POST /v1/admin/join-requests/{req_id}` | 管理员角色 |

四条必须知道的语义：

1. **管理接口只认 JWT** —— 而且现在**所有接口都只认 JWT**：共享密钥路径与匿名兜底
   都已删除，服务只有一种身份来源。（历史上审稿接口对"非 JWT 身份"有一处刻意豁免，
   因为当时脚本通行证是服务端到服务的凭证；那条路径删除后豁免也随之消失。）
2. **权限变更即刻生效**：改租户 / 改角色 / 停用 / 改密码，任一变更都会清掉该用户的
   全部会话。原因：JWT 里的租户与角色是**签发时的快照**，不清会话就改不动。
3. **无租户 = 只能读公共租户**（见 `RAG_TENANT` 那条）。所以自助注册 + 空档期的组合
   等价于"注册完即可读公共分区" —— 不是全库。公共分区里放什么由 `RAG_TENANT` 决定，
   未配置时那个账号什么都搜不到。
4. **删租户默认拒绝**："还有用户挂靠"时返回 409，要删必须显式 `force=true`，
   那时会把那些用户的租户清空（不是连带删用户）。

## 二、规模化才需要（单实例可留空）

| 变量 | 当前状态 | 说明 | 建议值 |
|---|---|---|---|
| `REDIS_URL` | ○ `.env` 里仍空；**容器部署已由 compose 注入** | 原风险是"多副本时限流/配额是**进程内**计数，2 个副本 = 实际额度 ×2"。`deploy/app/docker-compose.yml` 已为 `app-stable` / `app-canary` 注入 `redis://:${REDIS_PASSWORD}@redis:6379/0`，并加了 `redis` 服务（带口令、`appendonly`、资源上限）。**本机直跑时仍是进程内计数**（单进程，行为正确）。⚠️ 后端不可用时 app 退到进程内计数（**不是放行**），且启动期 ping 不通就整程不复位——所以 compose 里用 `service_healthy` 等它就绪 | 容器内无需手工填；本机要模拟多副本再填 |
| `APP_WORKERS` / `WEB_CONCURRENCY` | 由 compose 注入 | worker 数，默认 1。**刻意默认 1**：多 worker 会各自持有检索器与限流计数，没配 `REDIS_URL` 时额度会翻倍。compose 里 `app-stable` 给 `2`、`app-canary` 给 `1`（因为 Redis 已接上）。两者都认，前者优先（容器/PaaS 常注入后者） | 生产 `2`~`4`，**且必须先确认 `REDIS_URL` 生效** |
| ~~`DISCONNECT_POLL_INTERVAL`~~ | — | **已废弃（2026-09-29）**：断连检测改为 sse_starlette 的 `client_close_handler_callable` 回调，不再有轮询间隔。旧值留着无害，会被忽略 | 删除该行 |
| `HEALTH_READY_TIMEOUT` | 未设置 | 就绪探针里探依赖的超时（秒），默认 `3`。网络慢的跨机房环境可加大 | 默认即可 |
| `RETENTION_DAYS` | ✅ `0`（2026-10-03 起**永久保留**） | 数据保留清理的判定口径。**2026-10-03 起默认 `0` = 不清理** —— 对话历史改为**永久保留**（产品口径：用户隔多久回来都应该还能接上上一轮）。`retention-sweep` 服务仍保留运行，但在 `0` 时不删任何东西、每轮播报"未配置"；⚠️ `0` 在脚本里是**拒绝执行的哨兵**（安全默认），不是"清 0 天"。⇒ 五组表（`agent_messages` / `agent_sessions` / `agent_run_state` / `review_queue` / `session_owners`）从此**只增不减**，因此**必须**配套容量观测（见 `pg-stats` 与 `deploy/CAPACITY.md` 第四节）。要恢复清理就同时改这里与 `scripts/retention_sweep.py` | `0`（永久保留）；按合规要求定期删则设 `90` 之类 |
| `BACKUP_INTERVAL_HOURS` / `BACKUP_KEEP` / `QDRANT_BACKUP_KEEP` | ✅ `6` / `56` / `28`（2026-09-30 落盘） | 备份节奏。**间隔 × 份数 = 可回溯窗口**，改一个必须改另一个：原来 `24h × 14 = 14 天`；压到 6h 而份数不动会**静默缩到 3.5 天**。现在 6h × 56 = 14 天（pg）、6h × 28 = 7 天（qdrant）。改间隔还要**同步改** `alerts.yml` 里 `BackupLoopFailing` 的 54000s（15h）阈值 | `6 / 56 / 28`；RPO 要求更严就继续压间隔 |
| `CANARY_IMAGE` | 由 `deploy/release.sh` 注入 | 灰度实例的镜像引用。**不要手工 export 它**：发布/回滚都走脚本（`deploy/release.sh` / `deploy/rollback.sh`），否则容易在"改完忘记 `--env-file`"和"忘记 reload"这两处出错 | `ghcr.io/<org>/chatagent:sha-<短哈希>` |

## 三、可观测（强烈建议）

| 变量 | 当前状态 | 说明 |
|---|---|---|
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` | 未设置 | Langfuse 链路。**三填二**（host 可留空用官方云）；与 LangSmith 二选一。⚠️ **还需要额外装两个包**：`langfuse` + `openinference-instrumentation-openai-agents`（不在生产 `requirements.txt` 里，见 §3.1）。装了才真的上报；没装时启动日志会明确写"缺依赖"，`tracing` 后端回落 `none` —— **不会**出现"日志说接上了、面板里一条都没有"。装上之后，每条 trace 上带 `session_id`（会话）与 `metadata.request_id`（响应头回给客户端的那个值）⇒ **用户报 `X-Request-ID` 时能直接搜到那条链路**，不是只能搜日志 |
| `LANGSMITH_API_KEY` / `LANGCHAIN_PROJECT` | 未设置 | LangSmith 链路。**2026-10-30 起已接入 agent 链路**：除写 LangChain 侧认的三个环境变量（开启开关 / API Key / 项目名，LangChain 只认环境变量）之外，还会用官方 `OpenAIAgentsTracingProcessor` **替换** SDK 的处理器列表并**显式解禁** tracing —— 两件事缺一不可，少做替换默认处理器仍会把 prompt 与回复发往 OpenAI 后端，少做解禁则一条 span 都不产生（且零报错）。⚠️ 与 Langfuse 占**同一个位置、二者互斥**；两套 key 都填时 **LangSmith 优先**，Langfuse 分支不执行。⚠️ 想看到"会话聚合"与"按 `X-Request-ID` 反查"，运行配置里必须带 `group_id` 与 `trace_metadata`（`src/agents/runner.py::build_run_config`，由 `app/main.py` 传入）—— 只配 key 不传这两个字段，trace 是有的，但多轮串不起来、按 rid 搜不到。接入或改动前用 `python scripts/trace_probe.py` 离线验证转换层（零成本、不发网络） |
| `METRICS_ENABLED` | ✅ `1` | `/metrics` 已开 |
| `LOG_FORMAT` | ✅ `json` | 一行一 JSON，便于日志系统检索；本地想看人眼友好改 `text` |

> 两组 tracing 都留空时**官方 SDK 的 tracing 会被显式关掉**（`set_tracing_disabled(True)`），
> 行为与"没有这层代码"完全一致 —— 注意这**不等于什么都不做**：SDK 的 trace provider
> 在第一次访问时会自动注册一个往 OpenAI 后端上报的处理器，不关的话每一轮的 prompt
> 与回复全文都会尝试发往 `api.openai.com`（用环境里可能残留的 OpenAI 平台密钥 ——
> **本项目不读那个变量**，所以它要么用错 key、要么失败被吞），
> 失败被吞、不报错、不体现在任何指标上。守卫见
> `tests/test_observability.py::test_no_configured_backend_means_no_trace_at_all`。

**§3.1 Langfuse 怎么接**（2026-10-20 重写，照官方文档
`langfuse.com/integrations/frameworks/openai-agents`）：

```bash
pip install langfuse openinference-instrumentation-openai-agents
```

接法是 OpenInference 的 OTel 插桩，**不是**自己写 `TracingProcessor`，也**不是**
把 LangChain 的 callback handler 挂到运行配置里（旧实现就是后者：那个键官方
`Runner.run` 根本不读，于是 trace 一条都没出去，而代码看起来是接上的）。
插桩内部调 `agents.set_trace_processors([...])` **替换**默认处理器，所以默认的
OpenAI 后台上报会被一并顶掉 —— 这也是为什么配了 Langfuse 时**不能**再关 tracing
（关了连 span 都不产生，插桩收不到东西）。

**告警要真正接上**：`deploy/monitoring/alerts.yml` 里有 **6 组 67 条**规则
（可用性 / SLO / 成本 / 安全 / **平台自身** / **agent 能力链路**）。其中两条是
"任何一次即 critical"：`TenantMismatchDetected`（有人在冒充租户）、
`SessionOwnershipViolation`（有人在猜 `thread_id` 串读会话）—— 这两条一旦触发
就是**正在被攻击**，不能只当普通告警。

> ⚠️ 告警条数**不等于规则文件数**：`deploy/monitoring/recording.yml` 里还有一组
> **录音规则**（SLO 的预算余量，供 `SLOBudget*` 两条告警读）。它刻意不放在
> `alerts.yml`：那里每一条都必须是告警（要 category / severity / runbook），
> 混进录音规则会让那套纪律被稀释。加载它的是 `prometheus.yml` 的 `rule_files`。

`agent` 组（2026-10-02 新增 6 条，同日补跨轮历史后为 8 条）回答的是**"请求到底有没有被正确地跑完 agent 链路"**：
`AgentHistoryDegraded`（**跨轮历史不可用，模型看不到上文**）、
`AgentHistoryBudgetExhausted`（历史取到了、但裁到只剩本句仍超预算）、
`AgentHistoryBudgetErrors`（预算算不出来 ⇒ 本轮整段不裁剪）、
`AgentRunFailureRateHigh`（异常收尾率）、`AgentReworkRateHigh`（返工率）、
`AgentModelCallsPerRunHigh`（单任务模型调用数）、
`AgentGuardrailTripsFrequent`（护栏频繁拦截）、`AgentRetrievalEmptyHigh`（检索空召回率）。
⚠️ 2026-10-20 删掉了这里的第 9 条 `AgentLinkDegraded`（连同它唯一的触发序列
`agent_unavailable_total`）：它盯的是"想走 agent 却走不了 ⇒ 静默退回问答链路"，
而**降级路径本身已经不存在** —— agent 链路现在是唯一链路，SDK 缺失或模型没配时
进程**直接起不来**（守卫 `tests/test_startup_guard.py`），"服务起不来"由 `up == 0`
与容器重启计数覆盖。看到这里少一条不要以为是被误删。
它与另外五组的分工很清晰：那几组管"服务活没活 / 花了多少 token"，
这一组管"这个能力有没有跑起来、跑得好不好"。
⚠️ 这些序列**只在真的走了 agent 链路时才存在**（不是恒为 0）—— 一个从没走过
agent 链路的服务（比如主动把开关关掉），它们全都不存在，那正是"没有这个能力"与
"能力失灵"的区别。

本轮（2026-10-02）在 availability 组补了 8 条、cost 组补了 1 条，都是**"服务一切正常、
而某个东西已经坏了"**那一类：`SharedBackendDegraded`（Redis 挂 ⇒ 限流/配额静默退化）、
`RequestTimeoutFrequent`（总时限被顶掉，**不产生 5xx**）、`RagRetrievalDegraded`
（检索失败降级为无上下文）、`RetrievalCircuitOpen`（熔断打开，**只有这个 gauge 能反映**）、
`FirstTokenTooSlow`（首字节，用户耐心相关的第一指标）、`LlmCallErrorsHigh`（模型调用自身失败率，
不被重试与兜底话术掩盖）、`CheckpointErrorsHigh`（持久化读写失败）、
`OnlineHallucinationDetected`（线上采样到的可证伪编造）、
`ReviewWaitTooLong`（人工审核**等待时长**，与 `ReviewBacklog` 的"积压条数"不可互替）。
判据是一条：**这个故障在它缺席时能不能被别的信号发现**？不能的才配一条告警，
能发现的（`rag_stage_duration_seconds`、`agent_tool_calls` 这类下钻量）只进面板与 runbook。

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
| `CORS_ALLOW_ORIGINS` | 未设置（=`*`） | `allow_credentials` 恒为 false（鉴权走 `Authorization: Bearer` 请求头而非 Cookie），所以 `*` 在浏览器侧合法，不构成漏洞；但生产建议给白名单 |
| `RAG_GUARD_ACTION` | ✅ `warn` | 提示词注入防护动作：`warn`（只告警）/ `strip`（剥离可疑句）/ `block`（丢弃可疑块）。**结构隔离已默认生效**（`<资料>` 边界 + 声明不执行其中指令）。语料来源不可控时建议提到 `strip` |
| `RAG_TENANT` | ⚠️ 空（**建议填**） | **公共租户**：没有租户的账号（自助注册的、加入申请还没批的）能读到的**唯一**分区。它同时是 `python -m scripts.ingest` 未显式指定租户时的灌库默认值。⚠️ **口径 2026-10-05 反转**：此前空串 = 不加租户过滤 = **能看到全部租户的内容**；现在空串 = 落到 `NO_TENANT_SENTINEL` = **什么都读不到**（用户拍板："无租户可以查我们提供的公共租户文档，别的租户的不允许"）。⇒ 生产上应给它一个具名值（本项目用 `public`），并把允许所有人看的内容灌进那个分区。租户由 **JWT 载荷**承载，不存在"租户 ↔ 密钥"对照表 | 给一个具名值（如 `public`）；留空等于让新账号什么都搜不到 |
| `RAG_PARSE_CACHE` | `1`（默认） | 文档级解析结果落盘缓存。语料没变时跳过"重新解析 + 切分"（docling/OCR 是分钟级开销）；多 worker 收益翻倍 |
| `RAG_BREAKER` / `CIRCUIT_FAIL_THRESHOLD` / `CIRCUIT_RESET_SECONDS` | `1` / `5` / `30` | 出站调用（rerank / embedding）熔断参数 |
| `RAG_ONLINE_QUALITY_SAMPLE` | ⚠️ `0`（**关**；建议 `0.05`） | **在线答案质量采样率**。为什么需要它：**可用性 / 延迟两类指标看不见"答得变差"** —— 模型变傻、检索召回下降时 QPS / P95 / 5xx 全部正常，只能等用户投诉。开了之后按比例抽真实回答做**规则判定**（零模型调用、毫秒级：标识符硬判 / 数字只记不判 / 拒答静音）。⚠️ **告警 `OnlineHallucinationDetected` 依赖它**：`0` 时那两条序列**不存在**（不是 0），规则永远挂 inactive，而这一点从告警界面上看不出来。取值区间 0.02 ~ 0.1（0.05 推荐）；抽太少会让比率抖动 —— 它是**趋势**指标，不需要全量 |
| `RATE_LIMIT_PER_MINUTE` / `RATE_LIMIT_SESSION_PER_MINUTE` | `120` / `120` | 按 IP、按会话两个维度的每分钟上限（跨端点累计） |
| `RATE_LIMIT_ENABLED` / `RATE_LIMIT_BY_SESSION` | `1` / `1` | 上面两个维度各自的开关 |
| `QUOTA_ENABLED` | `1` | 配额总开关（限额为 0 时仍等于不限） |
| `MAX_CONCURRENT_REQUESTS` | 未设置（`0` = 不限） | **单进程**在途请求上限（背压）。限流限的是"每分钟新请求数"，管不住**长连接驻留**：主接口是 SSE，一个请求几十秒，按限流上限开若干连接并保持不关就能把实例占满而所有指标正常。超限返回 `503 + Retry-After`。⚠️ `0` 时饱和率指标**不存在**，告警不会触发 —— 面板显示 0 是"没有闸门"不是"很空闲"。⚠️ 多 worker 时实例上限 = worker 数 × 该值 | `100`~`300`（按 `CAPACITY.md` 的并发配额定）；**先确认内存与上游配额** |
| `SSE_PING_SECONDS` | `15` | 长连接的**保活帧**间隔（SSE 注释行，`0` = 关）。首 token 之前可能静默几十秒（模型思考 + 工具执行），这段时间 TCP 上一个字节都没有，反代的空闲超时会把连接当死连接掐掉。⚠️ 它**是注释行不是业务事件**，`EventSource` 不会交给 `onmessage` ⇒ 客户端无需改动。⚠️ 必须**小于**反代的 `proxy_read_timeout`（当前 300s）。写入是显式的，不依赖 `sse_starlette` 的默认值 —— 第三方默认值升版会变，而症状（长回答偶发中断）没有任何报错 | 保持 `15`；只有反代收紧读超时时才需要一起调 |

### 五之二、agent 能力链路（`src/agents`，2026-10-02 新增）

> ⚠️ **这里没有"开关"**：AGENTS_ENABLED 已于 2026-10-20 删除（守卫
> `tests/test_startup_guard.py::test_agents_enabled_knob_is_gone`）。它的语义是
> "关掉 agent、回落另一条链路"，而回落目标已经随图一起删掉 —— 留一个只能取 true
> 的开关，会让人以为可以关；更糟的是关掉之后进程照常启动、却答不出任何东西。
> 让 agent 链路"不工作"的唯一办法是不配 `LLM_API_KEY` / 不装 `openai-agents`，
> 而那会让进程**拒绝启动**（`app.main._assert_agents_ready`）。

| 变量 | 当前 | 说明 |
|---|---|---|
| `AGENT_MAX_ATTEMPTS` | `3` | ⚠️ **当前无代码读取（死配置）**。它原本是"评审回灌的轮数上限"；2026-10-02 评审改成模型可选的 `as_tool` 之后就没有回灌循环了，2026-10-19 删评分闸后连"按评审结论重跑"这条路也不存在 —— 现在只有 `app/config.py` 把它读进来回显，`src/agents` 一处都不读。留着是为了不破坏既有 `.env` 与启动自检，**不要按它去调行为** |
| `AGENT_MAX_TURNS` | `12` | 单次 `Runner.run` 内允许的 **LLM 调用次数**（官方语义，**不是对话轮次**）。设成对话轮次的量级（比如 3）会让正常的"查两次资料"直接抛 `MaxTurnsExceeded` |
| `AGENT_TIMEOUT_SECONDS` | `30` | 单次模型调用的超时（OpenAI 客户端级） |
| `AGENT_STRUCTURED_OUTPUT` | `json_schema` | **结构化产出怎么在请求体里表达**（2026-10-31 新增）。三个 `as_tool` 专家带 `output_type`，SDK 因此发 `response_format={"type":"json_schema", ...}`；不支持它的网关（实测 DeepSeek）回 `400 This response_format type is unavailable now`，三个专家**静默全废** —— 主链路照样"成功"（模型道歉并给替代方案），而评测里"工具选择 12/12 通过"配"工具执行 0/3（空转）。三档：`json_schema`（官方原样，支持它的网关用这档）/ `json_object`（只声明"输出是 JSON"，schema 写进 instructions）/ `none`（连声明都不发）。⚠️ **只改传输层编码**：`output_type`、`final_output_as`、SDK 侧的 JSON 校验全部原样 —— SDK 是对**返回文本**做 `validate_json`，与请求体里发哪种 response_format 无关。⚠️ 写错取值**拒绝启动**（装配期抛错 ⇒ `build_failed`），不静默回落默认档：静默回落的表现是"配置改了、专家照旧 400"，排查的人会去查网关。⚠️ 降档后结构不再由 provider 强制，缺字段只能靠 SDK 校验发现 ⇒ 失败形态从 400 变成 `bad_args`，别把"换档就好了"当成根治 |
| `AGENT_RUN_TOKEN_BUDGET` | `0`（不限） | **一次运行的 token 预算**（输入 + 输出，2026-10-21 新增）。`AGENT_MAX_TURNS` 限的是**圈数**，不是成本：一圈可能几百 token，也可能十几万（一次工具返回塞进整份文档）⇒ "12 圈"在长任务里可以是几十万 token。到线在**下一次模型调用之前**停下（`exit_reason="token_budget"`）。⚠️ 这一轮**不会形成答复正文**（它在注入口抛出 ⇒ `result` 为 `None` ⇒ `answer` 是空串；流式下客户端手里的增量留在客户端，但**不进会话历史**），判定表收成 `failed` + 原因 `token_budget`。⚠️ **这个预算含旁路调用**（反思闸门 + 窗口压缩）：它们不经过 `Runner` 的 usage，但压缩要把被压掉的那段历史整段读一遍，是长任务里最贵的一笔。旁路单独上报在 `agent_bypass_tokens_total{source}`，两条曲线相加才是总额。⚠️ 默认不限，与 `REQUEST_TIMEOUT_SECONDS` 同口径：默认值配小会大面积截断正常长任务，症状是"模型最近答得敷衍"。⚠️ **上游不回 usage 时它不生效**（累计恒 0）—— 唯一静默失效方式，靠 `agent_run_tokens_unmeasured_total` + 日志「一个 token 都没量到」暴露，看到那条先查网关的 `usage` 字段，**别去调预算值** |
| `AGENT_RUN_TIMEOUT_SECONDS` | `0`（不限） | **一次运行的墙钟上限**（秒，2026-10-21 新增）。`AGENT_MAX_TURNS` 限的是**圈数**不是**时间**：一圈可能 200ms 也可能 50 秒。没有它时实际时长上界是**隐式**的 —— 12 圈 ×（模型 30s + 工具 20s）≈ 10 分钟，而且谁也算不出它是怎么来的。到线位置与后果同 token 预算（`exit_reason="run_timeout"`、这一轮没有答复正文），且**排在预算之前判**：两者常常同时超线，而"等太久"是用户直接感知的那个，也常常是花钱多的原因 |
| `AGENT_TOOL_RESULT_MAX_CHARS` | `8000` | **单次工具返回**的字符上界（`0` = 不限，2026-10-21 新增）。`kb_search` 只限条数（`top_k` ≤ 10）不限字符 —— 一页表格、一段长 PDF 单条就能上万字，于是"10 条"可能是 2k 也可能是 20 万字符。⚠️ 没有它时唯一的约束是事后裁剪，而裁剪**从最老的丢**：被顶掉的是用户原话 ⇒ 症状是"模型忘了自己在做什么"。截断会附一句"原文 N / 只给了前 M / 想多看就缩小范围"。发生次数见 `agent_tool_result_truncated_total` |
| `AGENT_TOOL_FAIL_LIMIT` | `3` | 同一个工具失败几次之后，回给模型的文案改成"不要再调用它"（`0` = 不启用，2026-10-21 新增）。⚠️ 判据是**工具名**而不是「工具名 + 参数」：重复计数与震荡检测的指纹含参数，于是"换着参数试同一个坏工具"在它们眼里是八个不同指纹、各一次、都到不了阈值 —— **完全隐形**。到线只改文案，不摘工具、不阻断运行。刚到线那一次计 `agent_tool_give_up_total` |
| `AGENT_TOOL_TIMEOUT_SECONDS` | `20` | **单个工具执行**的等待上界（`0` = 不限）。⚠️ 与上一行不是一回事：一个管一次模型调用，一个管一次工具调用，独立可调。超时**不会**让这一轮失败 —— 它被转成一句给模型的话（换做法/缩小范围/先做完其余部分），模型自己绕。所以它是"悄无声息地少一段依据 + 变慢"，必须靠 `AgentToolStepTimeoutFrequent` 发现。⚠️ 唯一的兜底是请求级总时限（默认 `0` = 不限）⇒ 不配这项等于「工具卡住，请求永不结束」 |
| `AGENT_TOOL_WORKERS` | `0`（按 CPU 推导，`min(32, cpu+4)`） | 工具执行的**独立**线程池容量。为什么必须独立：官方对同步工具用 `asyncio.to_thread`，那是**事件循环的默认池**，与服务层 `_off()`（限流、会话归属、落库、结算）共用 ⇒ 工具卡住会拖累全进程。隔离后这个池是**可牺牲的**：打满时表现为"工具排队/超时"，而不是"服务不可用" |
| `AGENT_EVALUATOR_TRIES` | `2` | ⚠️ **当前无代码读取（死配置）**。原意是"评审调用的重试次数"；评审改成 `as_tool` 之后，嵌套 run 的重试由 SDK 自己管，这个值没人用。与 `AGENT_MAX_ATTEMPTS` 同一条：留配置、别指望它改变行为 |
| `AGENT_KB_TOP_K` | `3` | agent 链路单轮检索返回的片段数上限（与检索层默认的 `retrieval.TOP_K`（3）同量级：链路可以多查几轮，单轮不必更大） |
| `AGENT_MAX_ANSWER_CHARS` | `8000` | 交付物字符上限，即出口护栏 `length_guardrail` 的阈值。纯确定性、零模型调用 |
| `AGENT_ASSET_DIR` | `var/agent_assets` | PPT 大纲 / 插图资产的落盘目录。⚠️ 容器里应挂成卷，否则重启即丢（客户端拿到过的路径会失效） |
| `AGENT_TOOL_STEPS` | ✅ `1`（**默认开**） | **工具步骤记录**（2026-10-09 新增，2026-10-19 补两点）。记下每次运行里每一步调了哪个工具、参数指纹、成没成、花了多久 ⇒ 播 `agent_tool_result_total{tool,status,reason}`（工具正确率，**失败分两档**：`bad_args` = 模型给错参数 ⇒ 改描述/参数结构；`error` = 工具跑起来之后失败 ⇒ 查实现或依赖）、`agent_tool_duration_seconds{tool}`（哪个工具慢）、**同一动作做第二次起**的 `agent_action_repeats_total{tool}`（**"原地打转"的唯一判据** —— 打转时总工具次数不高、重量级动作也不多，只有"同一动作反复做"能把它暴露出来），以及 `agent_tool_exposed_total{tool}`（**每次 agent 开始运行时，给它挂的每个工具各记一次**）。最后这条补的是一个看板盲区：结果计数只在**被调用时**产生时间序列，所以"挂了但从没被选过"与"根本没挂这个工具"在 `/metrics` 上同样是空白 —— 有了曝光，"零选中"才是可查的数（曝光涨、结果没有）。⚠️ **默认开是"新能力默认关"这条惯例的例外**，且是有意的：它是**纯观察** —— 回给模型的错误文本仍由官方实现产出、指标走旁路、记录失败只降级为日志 ⇒ "关掉时与今天一致"天然满足；而默认关掉等于这些数**永远没有数据**，那正是它要补的缺口。曝光与失败分档都**跟随本开关**，没有独立开关 |
| `AGENT_MULTISTEP_GUARD` | `0`（关） | **多步防护总开关**（2026-10-09 新增，第三阶段）。打开后：一次运行内部计数够了就起一个后台任务去问**旁路模型**"是不是打转 / 卡住 / 跑偏"，结论在下一次模型调用**之前**追加一条 `system` 消息。⚠️ **默认关，这次的理由与 `AGENT_TOOL_STEPS` 相反且是本质的**：那个只记录，这个**真会改变模型看到的输入**（同一类问题到第 3 次会**删掉**出问题那一步及其之后的内容）⇒ 打开后出问题的退回代价不再是零。⚠️ **2026-10-10 起还多一件事**：确定"没救"时（该退却退不了，或撤了也没用）它会**中断本次运行**（第 3 级）—— 但**停止 ≠ 不交东西**：那次运行已产出的内容照交并标明"部分结果"，确实没产出才是 `failed`；被护栏拦下的内容连草稿一起转人工。⚠️ 2026-10-21 起证据里带**本轮原始目标**（人工打回轮是意见），提醒/撤回文案会回读它一次 —— 之前没有目标，`off_track` 只能靠工具名猜。⚠️ 反思走旁路 ⇒ **不计入 `AGENT_MAX_TURNS`、也不进 SDK tracing** ⇒ 超时由 `AGENT_REFLECT_TIMEOUT` **单独保证**。⚠️ **2026-10-22 起按"时间点"另有两个无条件检查点**（第 2 个动作之后、交付那一刻）：交付那次会把**即将交付的正文**一起给反思模型看（不给就只能靠步骤清单猜产出）；判出**偏离**或执行**撤回**时，会**作废本轮已登记的那份交付**并要求模型重交（播 `agent_delivery_discarded_total{reason}`）—— 不作废的话 `runner` 照旧用被否掉的旧稿答复用户，这次检查就白做了，而且指标上它是"成功"的。⚠️ 作废与"提醒一定注入"同生共死：额度拿不到时那条提醒不会发出去，那时清掉交付 = 用户什么都收不到、模型也不知道，比不作废更糟。⚠️ 没配 `LLM_API_KEY`（或网关不可用）时它整体降级成"什么都不做"，行为与关闭一致 |
| `AGENT_REFLECT_AFTER_TOOLS` | `8` | 工具调用次数阈值。**阈值同时承担两个含义 —— "什么时候开始看"与"多久再看一眼"**：配 8 ⇒ 在第 8、16、24 次各触发一次。⚠️ 写成"过了阈值就每次都发起"的后果不是"慢"：每次反思 = **一次真实模型调用**，长任务里它会比主线还贵；写成"每个原因只触发一次"的后果是第四阶段的"没纠正就升级"失去依据（没有第二次反思就判不出"同类问题又出现了"） |
| `AGENT_REFLECT_AFTER_HEAVY` | `3` | **重量级动作**次数阈值。重量级 = **叫专家（三个 `as_tool`）+ 落盘产出**（`save_text_asset` / `generate_image`）。⚠️ `kb_search` 与 `review_draft` **刻意不算**：前者本来就该多查几轮（算进来会让任何一次认真查资料都逼近阈值），后者是只读的流程步骤（"产出→评审→改"本来就要调几次，算进来会把正常流程误报成跑偏） |
| `AGENT_REFLECT_AFTER_HOPS` | `2` | **跨 agent 节点换人**次数阈值，取自 `stats.handoffs`（唯一一个**跨运行**累计的计数）。⚠️ 当前拓扑里「前台 → 文学」最多发生**一次** ⇒ 配 `2` 时它**永远不会触发**；刻意保留，因为拓扑一变（加第三个节点）它立刻有用，而"忘了加这个计数"是读代码看不出来的 |
| `AGENT_REFLECT_ON_REPEAT` | `3` | **同一动作重复几次**触发（判据是"工具名 + 规范化参数"这个指纹，不是工具名）。**下限 2** —— 配 1 等于"做一次就算重复"，那是常态 |
| `AGENT_REFLECT_AFTER_STARTED` | `2` | **跑到第几个动作时无条件检查一次**（`0` = 不启用）。2026-10-22 新增。⚠️ 必要性：其余五个原因全是**计数型**，于是一个"动作很快、没重复、没失败、但第 2 步方向就错了"的模型能一路顺畅做完整件错事，而计数型判据**全程沉默** —— 最贵的失败反而最不会被检查。它只触发**一次**（无条件 ≠ 每圈：每圈都查会让反思比主线还贵）；`0` 只关掉这一个，**"交付那一刻"那个检查点不受它控制**（那个没有 0 这个选项）。⚠️ 它判出偏离时会**作废本轮已登记交付**并让模型重交（原因见 `AGENT_MULTISTEP_GUARD` 那行） |
| `AGENT_REFLECT_MAX_LEVEL1` | `2` | 第 1 级（注入反馈）上限：一次运行里最多注入几次结论。⚠️ 第 3 步时它**同时是**发起上限，第 4 步把发起上限拆成 `AGENT_REFLECT_MAX_FIRES` —— 用注入上限去卡发起次数会让第 2 级的触发线（= 本值 + 1）永远到不了 |
| `AGENT_REFLECT_MAX_FIRES` | `5` | 一次运行里最多**发起**几次反思。⚠️ **必须 ≥ `AGENT_REFLECT_MAX_LEVEL1` + `AGENT_REFLECT_MAX_LEVEL2` + 2（两个无条件检查点）**，否则第 2 级**永远轮不到**、或者"交付前那一眼"被计数型判据先占光。默认 `5 = 2 + 1 + 2`。⚠️ 这个组合配错**不报错、不影响交付**（反思照常触发、提醒照常注入、指标照常在涨，只有某些介入从不发生，而"很少见"与"不可能发生"在看板上长得一样），所以有一条配置自检把它打成 warning（只打一次 —— 它在每次工具结束都会被调到）；强制点被额度吃掉时另记 `agent_reflection_skipped_total{reason="cap"}`，让这次退化**看得见** |
| `AGENT_REFLECT_MAX_LEVEL2` | `1` | 第 2 级（退回）上限：一次运行里最多**撤回**几次。`0` = 只提醒、绝不动上下文（**不是另一个开关**：总开关仍然只有 `AGENT_MULTISTEP_GUARD`）。⚠️ 触发线是**第 1 级上限 + 1**（默认第 3 次同类问题）——"先提醒 N 次、还不行才撤"；两个数是推导关系，不各配各的 |
| `AGENT_REFLECT_TIMEOUT` | `20` | 反思自己那次模型调用的超时（秒）。⚠️ **必须设**（走旁路，不受 `AGENT_MAX_TURNS` 与主线超时约束）。⚠️ 与 `AGENT_HISTORY_COMPACT_TIMEOUT` **独立**：配置来源共用（同 `LLM_MODEL` / `LLM_API_KEY` / `LLM_API_BASE`），但 client 分开建 —— 超时是构造期参数，没法共用一个实例 |
| `AGENT_REFLECT_WAIT_SECONDS` | `2` | 每次模型调用前最多等几秒（等还没跑完的反思把结论交上来），`0` = 不等。⚠️ 它**不是"只在收官前等"**：唯一能改模型输入的口子只有"模型调用之前"，在那个位置**看不到"这次是不是最后一次调用"**；阈值定得早时绝大多数情况等 0 秒，真正会等到的只有快收尾那几次。没赶上的会计进 `agent_reflection_late_total`（它在涨 ⇒ 阈值或超时配得不合适） |
| `AGENT_TOOL_APPROVAL` | `0`（关） | **工具审批总开关**（2026-10-14 新增，加固阶段三）。打开后：模型要调**会留下东西**的工具（保存文件 / 生成图片）时，这一轮运行**就地停下来**等用户点头；批准后从断点接着跑（前面做过的检索/评审**不重做**），拒绝则模型收到"被拒"后自己想办法。⚠️ **默认关**，且打开是**对外可见的行为变化**：用户不点头就什么都做不成。⚠️ 新增一档结局 `awaiting`（**不是失败、也不算兜底**、不进 `agent_fallback_total`）—— 光给工具加审批而不接这一档的话，一次落盘请求会变成一条**错误报告**（探针实测）。⚠️ 审批是**逐个工具**的：同一条响应里没标审批的工具**照常执行完** ⇒ **标一个不保护一批** |
| `AGENT_APPROVAL_TIMEOUT_SECONDS` | `600` | 等用户点头的上限（秒）。超时 ⇒ **终止这次任务**，界面展示"用户授权超时"，已产出部分照交并说明哪个动作没执行。⚠️ 这个时限**是自己管的**：SDK 没有审批超时原语，中断之后那次运行会一直挂着；实现是"待批项进队列时记下时刻 + 请求路径上惰性扫描"，不用后台定时任务（多副本下会重复触发） |

**五之三、跨轮对话历史（2026-10-03 改为 PG + token 预算）**

| 变量 | 当前 | 说明 |
|---|---|---|
| `AGENT_HISTORY_TOKEN_LIMIT` | `100000` | 送进模型的**输入预算**（token，`0` = 不限）。⚠️ 这是**输入**预算、不是整个窗口：工具清单与输出长度不在那个口子的视野里（官方 `RunConfig.call_model_input_filter` 只看"指令 + 输入条目"），必须留余量。⚠️ 2026-10-03 起取代了 `AGENT_HISTORY_LIMIT`（**条数**口径，已删除）—— 条数与 token 是**互不知道对方**的两套判据，两处都设上限时条数那份会先切，所以只保留 token 一个口径 |
| `AGENT_HISTORY_COMPACT` | `0`（关） | **跨轮历史压缩总开关**（2026-10-08 新增）。不设即为关，关着时今天的行为逐字节不变。打开后：老历史超过"保留轮数"就压成一段摘要（**摘要里带步骤骨架**：第几步、做了什么、关键结果、成没成，失败的与未完成的必须逐条保留 —— 否则接手者会把走不通的路再试一遍；压的是**库里那份会话历史**，`agent_sessions` 那张表），送进模型的输入超过**预算 × 0.85** 时另做一次窗口压缩（只改这一次调用、不写回） |
| `AGENT_HISTORY_KEEP_ROUNDS` | `5` | 压缩时**保留最近多少轮原样不动**。⚠️ 下限是 **1**（配 `0`/负数会被夹到 1，不是"全部压掉"）：人工打回那一轮靠"原话已经躺在会话里"决定不重传原话，压成 0 会让第二遍只看到一句"人工审核意见：…"、**不知道在改什么**，而且不报错 |
| `AGENT_HISTORY_COMPACT_RATIO` | `0.85` | **窗口压缩的触发比例**（`0.1`~`1.0`）。触发线 = `AGENT_HISTORY_TOKEN_LIMIT × 这个值` = 85,000。做成配置是为了"网关换成更大窗口的模型时只改配置" |
| `AGENT_HISTORY_COMPACT_TIMEOUT` | `30` | 写摘要那次模型调用的超时（秒）。它是**额外**的一次调用，独立可调 |
| `AGENT_HISTORY_COMPACT_MAX_CHARS` | `200000` | 交给摘要模型的正文上限（字符），超出部分**从最老的开始丢**。不设上限的后果是"开关刚打开、积了很长一段"时那次调用注定失败，而失败降级成"这轮没压" ⇒ **永远压不动**，看起来像功能没生效 |
| `AGENT_SESSION_CACHE_MAX` | `256` | 进程内缓存的会话对象数上限（不写这个键就用默认值）。防"构造陌生 `thread_id` 吃内存"这个 DoS 面 |
| `AGENT_SESSION_PING_TIMEOUT` | `5` | 启动自检探测 PG 连通性的超时（秒）。不设的话 PG 主机**不可达**时启动会被拖住几十秒到几分钟 |

> ⚠️ **跨轮历史只有一个后端：`DB_URL` 指的那个 PostgreSQL**（与审核停机现场同一张库，
> 表名 `agent_sessions` / `agent_messages`）。**没有 Redis 分支、也没有本地文件回退** ——
> 2026-10-03 把原来"配了 `REDIS_URL` 走 Redis、否则落本地 sqlite 文件"那套整段删掉了。
>
> - **多副本天然共享**：不再需要"所有副本必须指向同一个 Redis"那条约束。
> - **历史不过期、不清理**：用户隔多久回来都应该还能接上上一轮。这是**产品口径**，
>   不是技术参数 —— 要改就得同时动 `RETENTION_DAYS` 与 `scripts/retention_sweep.py`。
> - ⚠️ **没配 `DB_URL` 就没有历史**（不只是"重启丢状态"）：启动期有一条 warning 点明，
>   lifespan 里另有一条 `跨轮对话历史就绪：后端=postgres 输入预算=... token ...` 结论行
>   （连不通则变成 ERROR）。
> - ⚠️ 表要**先迁移**才有：`python -m app.migrations apply`（生产**不**靠运行时自动建表）。

> **关于压缩（2026-10-08）**：打开 `AGENT_HISTORY_COMPACT` 后，压缩**不是删除** ——
> 被压掉的那段变成**一条 `system` 摘要**，仍然永久留在库里；它只是不再逐条重复出现在
> 每一轮的输入里（这正是"平方增长"的来源：轮数翻一倍、送进模型的条目总量翻四倍）。
> 两个入口都**有阈值守卫**，没到门槛一个字节都不动：历史压缩的"门槛"是老历史超过保留
> 轮数，窗口压缩的"门槛"是输入超过预算的 85%。**不按请求类型额外设门**（人工打回轮
> 也照常判、照常压），因为门槛已经设在"该不该压"上了。
> 打开后要盯的两条：`agent_history_compacted_total{scope=...}`（压了多少次）与
> `agent_history_compaction_errors_total{stage=...}`（没压成的原因）。后者在涨表示
> **这一轮按原样用了历史** —— 功能面不受影响，只是增长没被压住。
> ⚠️ 摘要写成"对话中间夹一条 `system` 消息"这一条**未经真实模型验证**（本机不接真模型），
> 首次上线前用一次真实调用确认网关接受、且模型把它当背景而非新指令。

### 五之四、幂等键（客户端重试保护，2026-10-03 按 IETF 草案实现）

语义**逐条对着** `draft-ietf-httpapi-idempotency-key-header-07`：首次正常执行、
**已完成则重放上一次的响应**、在途回 409、同键换载荷回 422、缺键（仅在要求时）回 400。
规范正文（对客户端的承诺）发布在 **`deploy/IDEMPOTENCY.md`** —— 标准 2.5.2 要求
资源方**必须**发布它，改这里就要改那里。

| 变量 | 当前 | 说明 |
|---|---|---|
| `IDEMPOTENCY_ENABLED` | ✅ `1`（默认开） | 总开关。**关掉之后客户端带键也完全不生效**，重复计费会回来。不带键的请求无论开关都行为不变 |
| `IDEMPOTENCY_TTL_SECONDS` | `600`（10 分钟） | 键的保留时长，**同时**是"重放结果能活多久"（标准 2.3 的过期策略）。必须**明显长于**客户端的重试窗口：键一过期重试就不再被拦，而**没有任何报错** |
| `IDEMPOTENCY_MAX_ENTRIES` | `10000` | 进程内实现的条目上限。键由客户端提供 ⇒ 无界缓存是一个可被利用的内存放大面。溢出后**不再登记新键**（退化为 best-effort，不阻断服务），日志里有一条 warning |
| `IDEMPOTENCY_REQUIRE_KEY` | 未设置（`0`） | 是否**强制**要求该请求头，开启时缺失/非法回 400（标准 2.7 第一条）。⚠️ 默认关是因为该头本就是可选的（2.5.1 的 MAY）；开启前必须确认**所有**客户端都已带键，否则它们会当场全部 400。启动日志的 `idempotency.require_key` 会回显它 |
| `IDEMPOTENCY_MAX_KEY_CHARS` | `128` | 键的最大长度。键只能由**可打印 ASCII** 组成且不含引号/反斜杠（标准 §5 的注入/泄漏两条：键会进缓存键与日志，控制字符能伪造租户前缀或污染日志） |
| `IDEMPOTENCY_MAX_BODY_BYTES` | `262144` | 单个键最多缓存多少字节的重放内容。**内存换重放能力**的取舍：超过它，那次请求完成后重试会回 409 指路而不是重放（见 `idempotency_truncated_total`） |
| `IDEMPOTENCY_DOC_URL` | ⚠️ 空 | 规范文档 URL。400/422 的响应体要带一个指向文档的链接（标准 2.7 的 SHOULD）—— 留空则不带 `Link` 头。**生产应填对外文档地址** |

> ⚠️ **多副本**：不配 `REDIS_URL` 时键表是**进程内**的，重试落到另一个副本仍会
> 重复执行。启动日志的 `idempotency.backend` 会如实回显 `redis` / `memory`。

> ⚠️ **失败不占用键**：请求失败/断连/被配额拒绝时键会被撤销，客户端可以用
> **同一个键**重试。这一点是刻意的、也是标准允许的偏离（标准 2.6 说重放"成功或错误"，
> 本项目不缓存错误结果）—— 理由写在 `deploy/IDEMPOTENCY.md`。

## 六、已有值（复核无误，沿用即可）✅

| 变量 | 当前值 | 说明 |
|---|---|---|
| `LLM_API_KEY` / `LLM_API_BASE` / `LLM_MODEL` | 已配 / `api.deepseek.com` / `deepseek-chat` | 主模型（生成 / 压缩 / 反思 / 上下文补全）。变量名是中性的，换网关只改这三行 |
| `EMBEDDING_API_BASE` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` | 已配 / SiliconFlow / `BAAI/bge-m3` | 检索用 embedding，与 LLM 分开配 |
| `EMBEDDING_QUERY_PREFIX` | 已配（英文指令） | BGE 系靠它拉开区分度（实测 Top1 38% → 62%） |
| `DB_URL` | 已配（指向容器内 PG） | 跨轮对话历史 **+** 被打断的运行现场（`agent_run_state`） **+** pgvector **+** 审核队列 **+** 会话归属 共用 |
| `RAG_VECTOR_STORE` / `RAG_EMBEDDING` | **`qdrant`** / `hybrid` | 稀疏（BM25 哈希稀疏向量）+ 稠密（BGE-M3）**同库同集合**，双路 RRF 融合。⚠️ 2026-09-29 从 `pg` 迁到 `qdrant` —— 本文档 §六 此前仍写着 `pg`，属文档漂移，已修正；同类漂移由 `tests/test_doc_drift.py` 钉住 |
| `QDRANT_URL` | 本机 `http://localhost:6333`；容器内由 compose 覆盖为 `http://qdrant:6333` | 容器里**必须**用服务名 —— `localhost` 指容器自身，连不上会**静默回退 memory**（检索仍可用，但向量库等于没接上） |
| `DOCLING_ARTIFACTS_PATH` / `DOCLING_DEVICE` / `DOCLING_OCR` / `HF_ENDPOINT` | `./models/docling` / `cpu` / `auto` / `hf-mirror.com` | 离线解析链路 |

> 另外若干键在 `.env` 里存在但**值为空**（`REDIS_URL`、`REVIEW_NOTIFY_WEBHOOK`
> 等）—— 空值在这套代码里的语义是"未启用"，不是"用默认值"。
> ⚠️ **`RAG_TENANT` 不在此列，它的空值语义更硬**：留空 = "没有租户的账号**什么都读不到**"
> （检索层会落到 `NO_TENANT_SENTINEL`）。2026-10-05 之前留空的含义恰好相反
> （"不加过滤" ⇒ 能读全部租户的文档），生产上**建议填一个具名值**（本项目用 `public`），
> 把允许所有人看的内容灌进那个分区。
> ⚠️ **`SERVE_API_KEY` 已不在这份 `.env` 里**（整行删除，见文首表的 ⚠️）——
> 不是"值为空"，是"这个键不存在"。任何部署配置里出现它都会让服务拒绝启动。

---

## 怎么确认"我填的真的生效了"

配置错配最典型的表现是**静默不生效**（配了但没读、读了但被覆盖）。两个可靠的核对点：

```bash
# ① 启动日志里有一行结构化回显（不含任何密钥，只回显布尔/条数）
python -m app.serve 2>&1 | grep 服务启动配置
#   'env': 'production',
#   'auth': {'required': True,
#            'user_auth': {'enabled': True, 'session_secret': True,
#                          'seed_users': 1, 'reviewer_roles': ['reviewer'],
#                          'admin_roles': ['admin'], 'self_register': False,
#                          'accounts_backend': 'postgres', 'session_backend': 'redis'}},
#   'docs': False, 'proxy': {'trust_forwarded_for': True},
#   'tenant': {'mode': 'jwt', 'public': 'public', 'no_tenant_sees_nothing': False},
#   'rate_limit': {...}, 'quota': {...}
#   ↑ auth.required 恒为 True，且这里**已没有** `script_key` / `oidc` 这两个键
#     （共享密钥档整体删除；回显一个恒为 false 的字段会让人以为"这项还能开"）
#   ↑ tenant.mode 恒为 jwt（租户只来自令牌）；public 是"无租户者能读到的公共分区"
#   ↑ no_tenant_sees_nothing=True 表示 RAG_TENANT 留空 ⇒ 无租户者什么都读不到

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

**为什么仍然建议用 `python -m app.serve` 而不是裸 `uvicorn app.main:app`**：`app/serve.py`
注入了平台自适应的事件循环工厂（有 uvloop 用 uvloop，win32 上退到保守选择）。
⚠️ 它**原来的**那个理由已经消失（2026-10-20）：当时是 psycopg 的**异步**连接不支持
Windows ProactorEventLoop，而异步检查点已经随图一起删掉了。现在不裸启动只剩
"与容器 / CI 用同一个入口"这一个理由 —— 要删掉这层，得一起改 Dockerfile / compose /
CI / RUNBOOK，别只删 `serve.py`。

**怎么确认"停机现场"真的落库了**（这是唯一可靠信号）：

```bash
curl -s localhost:8000/health/ready | python -m json.tool
```

看 `checks.run_state`：
- `postgres（已持久化，被打断的运行可跨重启恢复）` ← 正常
- `postgres（表尚未建立：首次写入时建）` ← 正常（还没运行被打断过；第一次写入建表）
- `memory（进程内存，重启丢现场！）（未配 DB_URL，属预期）` ← 没接上，回看启动日志
- `disabled（运行现场不落库，停机一律不可恢复）` ← 明确关了
- `⚠️ 配了 DB_URL 但数据库当前连不上 ⇒ 运行现场实际写不进…` ← **不正常**，且这一项
  会是 `ok=false`（同时 `checks.db.ok` 也已经是 `false`）

⚠️ 这一项**默认不判 not-ready**（内存后端只是"重启丢现场"，服务本身还能用；判失败会让
实例被摘流量，反而更糟）。**唯一的例外是"配了 `DB_URL` 但库探测失败"** —— 那一档宣称的
持久化能力并不存在，2026-10-30 起明确判 `ok=false`。

⚠️ 这里原来只读进程内属性（`RunStore.backend` / `dsn`），而 `backend` 要等首次写入
建表成功才会变成 `postgres` ⇒ **库连不上时它也报 `ok:true` + "表尚未建立"**，
与同一份响应里 `db.ok=false` 自相矛盾（实测过）。现在 `readiness()` 会把 db 的探测结论
传给这一项，这个假绿灯已消除。**别把两处探针改回各自为政** —— `tests/test_health.py`
里有一条联动用例守着。

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
2. [ ] `AUTH_SESSION_SECRET` 非空，启动日志里 `user_auth.enabled=True`。
       ⚠️ **未启用用户鉴权时生产会拒绝启动** —— 这是刻意的；而且**没有任何开关能
       绕过它**（曾经能"关掉鉴权要求"的两个变量已删除）
2b. [ ] `.env` / compose / CI 变量里**没有** `SERVE_API_KEY` 与 `REQUIRE_AUTH`
       （**留空也不行**，必须整行删除 —— 存在即拒绝启动）
3. [ ] 启动日志里 `accounts.backend=postgres`（是 `memory` 则账号只在单进程内存里，
       多副本会"这边建的用户那边不认"）
4. [ ] 第一个管理员已存在（`AUTH_USERS` 种子已用上，或已走 `POST /v1/admin/users` 建出）
5. [ ] `AUTH_ADMIN_ROLES` 与 `AUTH_REVIEWER_ROLES` 是你想要的值（默认 `admin` / `reviewer`，**都是 fail-closed**）；确认**不是**意外地显式写空
6. [ ] `AUTH_ALLOW_SELF_REGISTER` 是你要的值（默认 `false`）；若开了，同时确认
       `RAG_TENANT` 已填具名值且那个分区里放的是可公开的内容（无租户只能读公共租户）
7. [ ] 用错误的密码调 `/v1/auth/login` **能拿到 401**；正确的密码能拿到 token，
       且 `GET /v1/auth/me` 回显的 `tenant` 与预期一致
8. [ ] 裸请求（不带 `Authorization`）调任意业务接口 **能拿到 401**；
       带 `X-API-Key` 调 **也是 401**（共享密钥已删除，它不再是凭证）；
       用普通账号（无 admin 角色）调 `/v1/admin/users` **能拿到 403**
9. [ ] 跨租户：用 A 租户用户的令牌读 B 租户的会话 id **能拿到 403**
10. [ ] `GET /docs` 返回 **404**
11. [ ] 反代场景 `TRUST_PROXY_HEADERS=1`，并用不同来源 IP 验证限流**分别**计数。
       ⚠️ **只验"不同 IP 分别计数"是不够的** —— 那种验法在**追加式**写入下照样通过
       （它测的是真实来源不同时桶不同，而不是伪造的头会不会被采信）。必须再验一条：
       **同一个来源连发两次，第二次带一个手写的 `X-Forwarded-For: 1.2.3.4`** ——
       若两次落进**不同**的桶，说明代理是追加式写入（错），任何客户端都能绕过限流
       （2026-10-04 加；改法与守卫见 `deploy/app/nginx.conf` 顶部与
       `tests/test_deploy_topology.py`）
12. [ ] `LLM_MAX_TOKENS_PER_REQUEST` / `USER_DAILY_TOKEN_QUOTA` 已设非 0

**依赖与数据**

7. [ ] `/health/ready` 五项都在（llm / db / vector_store / run_state / pg_pool），前三项 ok
8. [ ] `checks.run_state` 显示 `postgres（已持久化，被打断的运行可跨重启恢复）`
       （`memory（进程内存，重启丢现场！）` ⇒ 停机现场没落库，重启即丢；
       ⚠️ 内存降级这一档**刻意不判 ok=false**，探针不会替你发现它，只能人看；
       而"配了 DB_URL 却连不上"这一档 2026-10-30 起**会**判 ok=false）
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
13. [ ] 对外入口走 **TLS 覆盖文件**（443 + HSTS），而不是只开明文入口。
       **本机/内网默认形态是"明文只绑回环"**，对公网提供服务是一个显式动作：
       ```bash
       docker compose --env-file .env \
         -f deploy/app/docker-compose.yml -f deploy/app/docker-compose.tls.yml up -d
       ```
       ⚠️ 前置：证书（自签 / Let's Encrypt / LB 卸载，三选一见 `deploy/app/nginx.tls.conf`
       文件头）。证书目录 `deploy/app/certs/` 已在 `.gitignore` 里 —— **私钥别进仓库**。
       ⚠️ 若前面还有一层 LB 卸载 TLS，记得 `TRUST_PROXY_HEADERS=1`；否则按 IP 的限流
       （含登录防爆破）会把所有人算成同一个桶，且**没有任何报错**。
       ⚠️ 同时把 `deploy/monitoring/prometheus.yml` 里 `blackbox-tls` 的
       `https://REPLACE-WITH-YOUR-DOMAIN/health/ready` 换成**同一个真实域名**。
       它是 PromQL 里的普通字符串，**写错不会报任何错**，只会让该 target 的
       `probe_success` 停在 0 ⇒ `ExternalProbeFailing` 恒响（狼来了，久了就没人看）；
       更糟的一种是探针自身没产出序列时那条规则**根本不触发**（见 `RUNBOOK.md`
       §3 那张表："用没有序列表达故障"）。两种都要靠这一步避免。
14. [ ] **`docker compose --env-file .env -f deploy/app/docker-compose.yml config` 自证插值生效**（2026-09-30 新增，最容易被跳过的一条）：
       ```bash
       docker compose --env-file .env -f deploy/app/docker-compose.yml config | grep -iE 'webhook|POSTGRES_PASSWORD'
       ```
       它打印的是**插值之后**的最终配置。若这里看不到你填的值，说明 `--env-file` 漏了
       （compose 只在**第一个 `-f` 文件所在目录**找 `.env`，也就是 `deploy/app/`，
       仓库根的 `.env` 不会自动生效）—— 而**没有任何报错**，只有默认值悄悄生效。

**可观测与闭环**

15. [ ] `/metrics` 能抓到 `http_requests_total`、`llm_tokens_total`、`review_pending`。
       ⚠️ `event_loop_lag_seconds` **不会马上出现，也不会一直出现** —— 它是直方图，
       只在真的发生 >50ms 阻塞时才产生第一个样本。所以"序列不存在"是**正常**的
       （含义是"这段时间没被阻塞过"），别去找它；反过来说，它一旦出现就说明
       确实有过同步调用堵住事件循环（处置见 `RUNBOOK.md` 的 `EventLoopBlocked`）。
       ✅ **与它相反，进程级四条从第一次抓取起就一直在**：`process_resident_memory_bytes`、
       `process_cpu_seconds_total`、`python_gc_collections_total`、
       `python_gc_collected_objects_total`。它们**缺席才是问题** ——
       说明该平台的 RSS 读取分支没命中（只在 Windows / Linux 上实现），
       而不是"暂时没有数据"。
       ⚠️ 同理，`llm_inter_chunk_seconds` **要求一次回答至少有两个 chunk** 才会出现
       第一个样本。所有请求都"一句话答完"时它不会出现，这是正常的；
       反过来，它在有长回答的时段仍然为空，才说明埋点没走到。
16. [ ] `deploy/monitoring/alerts.yml` 已加载（**6 组 67 条**），且 `review_pending` 有"长时间 > 0"的告警
17. [ ] `chatagent-slo` 组已加载（面板上能看到 **7 条**燃尽率规则 + 2 条预算余量规则；
       7 = availability 3 条 + latency 2 条 + stream_latency 2 条），
       且 `SLO.md` 的两张表与表达式一致（`pytest tests/test_monitoring_config.py` 会钉住）。
       ⚠️ 还要确认**录音规则真的在跑**（它是 `SLOBudget*` 唯一的数据来源，也是**哑弹重灾区**）：
       `prometheus.yml` 的 `rule_files` 含 `recording.yml` 且它被挂进容器，
       并在 `/rules` 页确认 `slo:budget_remaining_ratio` 的 `Health=ok`、`Last Evaluation` 在动。
       没加载、或序列名写错时的表现是 `SLOBudget*` **永远不触发**——看起来像"预算一直很健康"；
       另外 Prometheus 的保留期必须是 `40d`（默认 15d 会让预算静默只算半个月）
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
24. [ ] `retention-sweep` 的行为符合预期：默认 `RETENTION_DAYS=0` 时它应**保持运行并每轮播报"未配置"**
       （不是崩溃，也不是静默什么都不打）
       （`docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=20 retention-sweep`）
25. [ ] `pg-stats` 在跑，且容量指标真的进了 Prometheus：
       ```bash
       curl -s localhost:9100/metrics | grep -E 'pg_table_size_bytes|pg_database_size_bytes'
       docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=5 pg-stats
       ```
       （前者有数据 = 采集→textfile→exporter 这条链通了；后者应有"PG 容量已采集：N 张表…"）
       ⚠️ **没有它 = 永久保留下的库增长完全不可观测**：`DiskSpaceLow` 是唯一兜底，
       而它只在可用空间低于 15% 时才响 —— 那时余量往往只剩几小时，也不说是哪张表在吃
26. [ ] `python scripts/backup_manifest.py` 退出码为 0（pg 与 qdrant 的最新备份成对）
27. [ ] `deploy/release.sh <镜像> --dry-run` 能跑通（发布流程的"空跑"，
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

30. [ ] `docker compose --env-file .env -f deploy/app/docker-compose.yml logs alertmanager` 里能看到 `Completed loading`，
       且 `片段①/②/③` **要么三处都取消注释、要么一处都没有**（只放开一半的表现是
       `failed notifications` 里出现 `integration=email`，而你看不出少了哪一半）
31. [ ] `deploy/monitoring/secrets/smtp_password` 存在、权限 `600`，且
       `git check-ignore deploy/monitoring/secrets/smtp_password` **返回 0**
       （返回 1 就说明 `git add -A` 会把 SMTP 密码提交进版本库）
32. [ ] **邮箱里真的收到过一封告警**，并且触发它的那次故障恢复后**也收到了「已恢复」**
       （后者专门验证 `send_resolved: true` 没漏）

**幂等键（客户端重试保护）**

33. [ ] 带 `Idempotency-Key` 连打两次**同一个**请求，第二次必须**重放**：
       响应体与首次逐字节一致、带 `Idempotency-Replayed: true`、且**没有再花一次 token**。
       ```bash
       curl -s -N -X POST localhost:8000/v1/chat -H 'Content-Type: application/json' \
         -H 'Idempotency-Key: acceptance-1' \
         -d '{"thread_id":"acceptance-1","message":"你好"}' | head -20
       # 同一条命令再打一次（同一个键、同一个 body）
       ```
       ⚠️ 判据只有两条，缺一不可：
       - 第二次拿到 **409** ⇒ 重放正文没缓存住（看 `/metrics` 的
         `idempotency_truncated_total`，必要时调大 `IDEMPOTENCY_MAX_BODY_BYTES`）；
       - 第二次**又花了一次 token**（对照 `llm_tokens_total`）⇒ 幂等根本没生效，
         先核对启动日志的 `idempotency.enabled` 与 `backend`。
       另外记一句：不配 `REDIS_URL` 时它只在**单副本**内有效。

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

**引用依据校验（`RAG_CITATION_CHECK`，默认开）**

36. [ ] 有引用时，SSE 流末尾能收到 **`citation_check` 事件**（带 `status`）。
       - `ok` —— 引用编号可追溯、来源文件在、与答案有重叠。
       - `no_citations` —— 本轮没检索到内容（不是错误，**不拦**）。
       - `weak` —— 与引用文本重叠偏低（**启发式判据，不拦** —— 改写幅度大而已）。
       - `degraded` —— 被引用的来源文件在语料目录下 stat 不到（多半是文件被删/改名，
         该由 `kb-prune` 清孤儿，**不拦**）。
       - `unsupported` —— ⚠️ **答案里标了本轮命中里不存在的编号**（编造引用）。
         这是唯一 `block_worthy=True` 的状态，对应告警 `CitationUnsupportedFrequent`。
       - `no_answer` / `checked=false` —— 空答案、或 `DOCS_DIR` 不存在（**无从判断**，
         刻意不判：容器里没挂语料时判下去就是"所有引用都坏了"的纯误报）。
       `/metrics` 里有 `citation_check_total{status=...}`，编造引用另计
       `citation_check_unsupported_total`。
       ⚠️ `answer_prompt` **目前不要求**模型标编号 ⇒ 多数部署的 `status` 就是
       `ok` / `no_citations`，**这是刻意的**（改提示词要 bump `PROMPT_VERSION`
       并重跑评测）。但**一旦模型标了编号，标错就必须能看见**。

37. [ ] （**只有配了 `RAG_CITATION_CHECK_ACTION=block` 才需要勾**）验证"拦"真的生效：
       让答案带一个越界编号（例如本轮只命中 1 条却写 `[9]`），确认
       ① SSE 里**没有 `citations` 事件**；② 仍有 `citation_check`（`unsupported`）；
       ③ 日志里有一行 `引用校验未通过（unsupported），按策略 block：不下发引用`；
       ④ **答案照常下发** —— block 只吞引用、不吞答案。核完把 action 改回 `warn`。

**agent 能力链路（唯一作答路径，**没有开关**）**

38. [ ] 自检 JSON 的 `agents` 段里**只有行为参数**（`asset_dir` / `max_turns` /
       `history`），**没有 `enabled` / `ready` / `reason` 三个键** —— 这是刻意的：
       有那三个键就说明代码里还留着"能不能走 agent"的判定，而那条判定连同它的
       降级目标已经一起删了（2026-10-20）。判据变成**进程起没起来**：
       - 起来了 ⇒ 启动日志有一行 `agent 链路就绪：请求走 src/agents（...）`，
         此时**所有**请求都走这条链路，没有第二条。
       - 起不来 ⇒ 日志里有 `agent 链路不可用（<reason>）⇒ 拒绝启动`，`reason` 只有三档：
         `missing_sdk`（镜像里没装 `openai-agents`）、`missing_api_key`（没配
         `LLM_API_KEY`）、`build_failed`（装配期抛异常，具体见上一条 warning）。
         ⚠️ **不要再去找"静默降级"**：没有降级了，起不来就是起不来。
       ⚠️ **装包还要看版本**：`openai-agents` 受 `langchain-openai` 的 `openai<3`
       约束牵制（本项目钉 `0.20.0`），装高了会在**导入期**与 `openai` 冲突 ——
       那时的现象是 `build_failed`，报错文本看着跟版本无关。

39. [ ] **发一条请求，确认它真的走进 agent 链路**（这条只能真跑，看配置不算）：
       ① 发 `写一篇关于灯塔的短篇小说`，SSE 里应收到一个 `token` 事件
          —— agent 链路是**整段**产出的，不产生逐 token 事件，`main._sse` 会把它
          补成一个 `token`；**若收到"零 token + done"**，客户端会表现成"服务没回答"
          （最容易被误判成前端 bug 的一种）；
       ② `/metrics` 里应出现 `agent_runs_total{exit_reason="passed"}`、
          `agent_participants_total{agent="frontline_agent"}`，以及（这一单会写小说）
          `agent_handoff_total{src="frontline_agent", dst="literary_writer"}` 与
          `agent_participants_total{agent="literary_writer"}`；
       ③ ⚠️ **再发一条问答请求**（`限流是怎么算的`），它**同样应该走 agent 链路** ——
          入口不做内容分诊，问答也是前台的职责。表现为 `agent_runs_total`
          **继续增长**，而 `agent_handoff_total` **不涨**（问答只有前台参与）。
          ⚠️ 这里**只有一条链路**：`agent_runs_total` 不涨就说明请求根本没进来
          （看 `http_requests_total` 与 `up`），不存在"退回另一条链路"这回事。

40. [ ] **跨轮记忆**（只能真跑；这一条失灵时服务**照常出答案**，只是记不住上文，
       所以它是所有验收项里最容易被漏掉的一个）：
       ① 启动日志里应有 `跨轮对话历史就绪：后端=postgres 输入预算=100000 token 存储=不过期不清理`。
          - 显示 `后端=none` ⇒ 没配 `DB_URL`，跨轮历史整体关闭（`AGENT_HISTORY_TOKEN_LIMIT`
            此时是空转的：没有历史可取，预算这一层自然也不会动）；
          - 变成 `跨轮对话历史**不可用**`（ERROR）⇒ 后端连不上，按下一行提示查；
       ② 同一个 `thread_id` 连发两句：先 `我叫张工，请记住这个名字`，
          再 `我叫什么名字？`，第二句应当**答得出张工**。
          更省事的方式：`python scripts/agent_capability_probe.py --cross-turn`
          （多花 2 次模型调用，脚本会打印"记得 / 不记得"）；
       ③ `/metrics` 里 `agent_session_errors_total` 应当**不存在或为 0**。它在涨说明
          历史一直没存上 —— 此时上面第 ② 步会失败，但服务本身毫无异常表现。
       ④ （可选，验证有界）把 `AGENT_HISTORY_TOKEN_LIMIT` 调到 `200`，连发三轮，确认
          模型仍答得出**最近**一轮的内容、但看不到最早那轮，且 `/metrics` 里
          `agent_history_trimmed_total` 在涨。核完改回来。

41. [ ] **在线质量采样真的在跑**（不勾这一条时，`OnlineHallucinationDetected` 会
      永远静默 —— 它不报错，只是在等一个永远不会出现的序列）：
      ① `.env` 里设 `RAG_ONLINE_QUALITY_SAMPLE=0.05`（默认 `0` = 关）并重启；
      ② 连发十来条会触发检索的问题，然后 `curl .../metrics | grep online_answer_judged_total`
         —— 应当**出现**该序列且 `verdict` 三档都在（`pass` / `fail` / `unknown`）。
         一条都没有 ⇒ 采样没生效（先确认重启过、变量名没拼错）；
      ③ 看总览看板 panel 23「在线答案质量」—— `unknown` 占比若长期很高，说明多数回答
         没有可核的标识符，此时**幻觉率本身参考价值有限**（先看样本量再看比率）。
