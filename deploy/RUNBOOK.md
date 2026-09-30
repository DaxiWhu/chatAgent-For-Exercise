# 值班手册（RUNBOOK）

> **本文与 `monitoring/alerts.yml` 一一对应。** 新增一条告警却不在本文写处置，
> 会被 `tests/test_deploy_topology.py::test_runbook_covers_every_alert` 测红 ——
> 理由是"告警最怕的不是响，而是响了没人知道下一步做什么"。
>
> 适用范围：生产环境（`APP_ENV=production`）。staging 的告警是**关闭**的
> （见 `deploy/app/docker-compose.staging.yml`）—— 告警必须只来自生产，
> 否则"这条是 staging 的"会变成忽略真告警的借口。

---

## 一、先搞清三件事

### 1.1 告警是怎么送到我手上的

```
应用 /metrics → Prometheus 判定（alerts.yml）→ Alertmanager 路由（按 category）
              → alert-sink 转成机器人消息 → 企业微信 / 飞书 / 自建通道
```

**每一环都可能断，而且断开的表现各不相同：**

| 断在哪 | 现象 | 怎么确认 |
|---|---|---|
| Prometheus 没抓到 | `http://localhost:9090/targets` 里 target 是 DOWN | 查网络、服务名、`/metrics` 是否被 nginx 限制（只允许内网段） |
| 规则不触发 | `/alerts` 页面里规则长期 inactive，连 pending 都没有 | 表达式里的标签值格式写错了（如 `le="1"` vs `le="1.0"`）—— **不报错，只是永不匹配** |
| Alertmanager 收不到 | `/status` 页里 Alertmanagers 一节是 DOWN | 服务名/网络不对；`prometheus.yml` 少了 `alerting.alertmanagers` |
| 送到了但没转 | Alertmanager 里能看到 FIRING，机器人没消息 | `docker compose logs alert-sink` 看是否有转换失败 |
| 通道没配 | alert-sink 日志里只有"未配置通道，仅记日志" | `.env` 里 `ALERT_SINK_*_WEBHOOK` 为空 |
| 邮件没收到（仅在启用邮件后） | 群/机器人正常，邮箱一封都没有 | 看 `logs alertmanager` 里的投递失败；重点查 `smtp_smarthost` 的**端口写法**（465 需配 `force_implicit_tls`）与密码文件**是否真的挂进容器** —— 见 §3.5 |
| **链路整体死了** | **该来的 `Watchdog` 心跳没来** | 见 §3.5 —— 这是唯一能穿透"Alertmanager 自己挂了"的信号 |

> ⚠️ 最常见的误判：**在 Prometheus 页面看到 FIRING 就以为"已经通知过了"**。
> FIRING 只证明"规则判定成立"，不证明有人收到。
>
> ⚠️ 第二个误判：**"今天没有告警，说明系统很健康"**。没有告警有两种成因，
> 而且从收件箱里**看不出区别**：真的没故障，或者链路早就断了。
> 区分它们的唯一手段是 `Watchdog` —— 它恒 firing，所以"**必须一直在响**"，
> 于是"**不响**"本身就成了可观测信号。默认每 24h 到达一次。

### 1.2 上线后必须做的一次验证（别省）

```bash
# ① 三层面板都看一眼
open http://localhost:3000             # Grafana：四个面板（见下表）
open http://localhost:9090/targets     # 两个 chatagent job + blackbox 都是 UP
open http://localhost:9090/status      # Alertmanagers 一节是 UP
open http://localhost:9093/#/status    # 无配置解析错误

# ② alert-sink 自检（未配置的通道不会报错，只会记日志）
docker compose --env-file .env -f deploy/app/docker-compose.yml run --rm alert-sink \
  python alert_sink.py --selftest

# ③ **真正把一条告警发出来**（这一步才是"闭环验证"）
#    停一个非关键实例让 ChatAgentDown 触发，确认机器人真的收到消息。
docker compose --env-file .env -f deploy/app/docker-compose.yml stop app-canary
#    收到后立刻恢复：
docker compose --env-file .env -f deploy/app/docker-compose.yml start app-canary

# ④ 端到端自检（**改过 nginx / compose / 依赖之后必做**）
python scripts/tasks.py e2e             # 起栈 → 等就绪 → 检查 → 收栈；加 --keep 保留现场

# ⑤ 邮件通道（**只在启用了它之后**做；启用步骤见 alertmanager.yml 末尾）
docker compose --env-file .env -f deploy/app/docker-compose.yml logs alertmanager \
  | grep -i "Completed loading"          # 配置装载成功（含 SMTP 段语法）
#    然后**真收一封**：把 app-canary 停一下让 ChatAgentDown 走完全链路
#    收到后注意 `AlertDeliveryFailing` 应停止增长（它统计的是所有通道的投递失败）
```

第 ③ 步是唯一能证明"告警送得到人"的方法。上面两步只能证明链路"看起来接好了"。

第 ④ 步检查的是**进程内测试原理上覆盖不到**的三件事，而它们全都是"告警不响、
只是体验悄悄变差"的类型：

- **SSE 经 nginx 之后是否仍然一帧一帧地到达** —— 漏写 `proxy_buffering off` 时，
  流式回答会变成"卡十几秒然后一次性吐出来"，而进程内测试压根不经过 nginx；
- **灰度权重是否真的生效** —— 靠 `/health/live` 的 `release` 字段分布判断。
  这是唯一能自证"那 10% 流量真的进了新版本"的检查；
- **检查点是否真的落了库** —— 退回内存时服务照常工作、回答照常出，**只在容器重启后
  丢状态**（`/health/ready` 的 `checkpointer` 明细是唯一能看出它的地方）。

第 ⑤ 步只在启用了邮件时才有意义，但那时它**不能省**：邮件是唯一**不经 alert-sink** 的
通道（走 Alertmanager 原生 `email_configs`），两条链路在 Alertmanager 里就分叉了 ——
所以 `alert_sink.py --selftest` 全绿**证明不了邮件能送达**。

它要起一整套容器（几分钟），所以刻意不放进每次 CI 的 push 上，而是手动 / 每晚定时
（`ci.yml` 的 `e2e` job）。

Grafana 要在 `chatAgent` 文件夹下看到这四个面板（它们随仓库走，不是手工建的；
`--profile observability` 才会起）：

| 面板 | 什么时候看 |
|---|---|
| chatAgent · 服务总览 | 值班第一眼：现在有没有在出错 |
| chatAgent · LLM 与成本 | 钱花在哪、回答有没有在退化 |
| chatAgent · 限流、配额与安全 | 被拒绝的请求 —— 它们不产生 5xx，所以不会在总览里露头 |
| chatAgent · 平台自身 | "谁来发现监控坏了" |

一个都加载不出来时：先看 Grafana 日志里的 provisioning 报错，再核
`deploy/monitoring/grafana/provisioning/dashboards/chatagent.yml` 的 `options.path`
—— 它必须是**容器内**路径。写错时 Grafana 只记一行 error，界面上就是"文件夹是空的"。


### 1.3 事件分级

| 级别 | 判定标准 | 响应 | 通知 |
|---|---|---|---|
| **P0** | 用户在报障 / `ExternalProbeFailing` / `ChatAgentDown` 两个实例都挂 / 安全组告警（`TenantMismatchDetected`、`SessionOwnershipViolation`） | 立即（15 分钟内动手） | 值班 + 负责人 |
| **P1** | `HighErrorRate` / `StreamErrorsHigh` / `SLOAvailabilityBurnFast` | 1 小时内 | 值班 |
| **P2** | `SLO*BurnMedium` / `HighLatencyP95` / 成本组告警 / 平台组 warning（`BackupLoopFailing`、`BackupMetricsMissing`） | 当天 | 值班（可异步） |
| **P3** | `SLO*BurnSlow` / `TLSCertExpiringSoon` | 排期（一周内） | 记录，不必叫人 |

平台组的 critical 单独记：`MonitoringStackDown` / `AlertDeliveryFailing` /
`DiskSpaceLow` 都按 **P1** 处理（1 小时内），但有一个例外 ——
**`DiskSpaceLow` 如果同时伴随 5xx 升高，直接按 P0**：满盘时 PG 会先转只读，
接下来是随机 500，而根因在日志里几乎看不出来（这是本栈最典型的"慢刀子"）。

`Watchdog` 不属于任何级别：它**永远 firing**，只在"**该来而没来**"时才有意义。

安全组的两条**任何一次都按 P0 处理**：`tenant_mismatch_total` 与
`session_owner_mismatch_total` 在正常情况下恒为 0，非 0 只可能是有人在探测越权。

---

## 二、处置流程（通用骨架）

1. **先止血，再定位。** 典型止血动作按成本从低到高：
   - 回滚最近一次发布（`deploy/CANARY.md` 的"回滚"一节，秒级生效）；
   - 收回灰度流量（`nginx.conf` 里 canary `weight=0` → `nginx -s reload`）；
   - 调紧限流（`RATE_LIMIT_*`）挡住刷量；
   - 临时调低 `LLM_MAX_TOKENS_PER_REQUEST` / `USER_DAILY_TOKEN_QUOTA` 阻断成本失血。
2. **拿到 request_id 再钻。** 结构化日志（`LOG_FORMAT=json`）里每条都带
   `request_id`，用它把一次请求的完整链路串起来：
   ```bash
   docker compose --env-file .env -f deploy/app/docker-compose.yml logs app-stable \
     | grep '"request_id": "REPLACE-WITH-ID"' | python -m json.tool --json-lines
   ```
3. **区分 stable / canary。** 指标的 `release` 标签就是为这件事存在的：
   只有 canary 坏 ⇒ 是这一版引入的；两边一起坏 ⇒ 环境/依赖/上游的问题。
   这也是灰度存在的意义 —— 别把它当成"少影响 10% 用户"的机制。
4. **记下证据再恢复。** 回滚前把告警值、日志片段、时间点保存下来。
   不记录等于白灰一场，同一个问题下次还会再来一遍。

---

## 三、逐条处置（对应 `alerts.yml`）

### 3.1 可用性（category: availability）

#### `ChatAgentDown`（critical，2m）
Prometheus 抓不到 `/metrics`，最可能是进程没了。

```bash
docker compose --env-file .env -f deploy/app/docker-compose.yml ps
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=200 app-stable
curl -sS localhost:8080/health/live | python -m json.tool
```
- 进程死了 ⇒ 看日志里的最后一屏。**特别注意启动期的拒绝启动**：
  `APP_ENV=production` 且没有任何密钥时，服务**故意**拒绝启动（这是特性不是 bug）。
- 进程活着但探针失败 ⇒ 看 `/health/ready` 的 `checks` 明细，定位是哪个依赖。
- 只是 `/metrics` 被拒 ⇒ nginx 的 `location = /metrics` 只允许内网段；
  Prometheus 若在宿主机上跑，源 IP 可能不在白名单里。

#### `ChatAgentReadinessDegraded`（warning，5m）
readiness 一直 503 ⇒ 实例被摘掉流量，等于"没在服务"。

```bash
curl -sS localhost:8000/health/ready | python -m json.tool
```
逐项看 `llm / db / vector_store / checkpointer` 哪个 `ok=false`。

> ⚠️ **`checkpointer` 这一项是最容易搞错的**：检查点后端若降级成内存实现，
> 它的文案会明确写 `InMemorySaver（内存，重启丢状态！）`。
> 这是判断"持久化到底有没有接上"的**唯一可靠信号** —— 服务看起来一切正常，
> 但所有会话状态在重启后会丢。

#### `HighErrorRate`（critical，5m，5xx > 5%）
```bash
# 按路由看错误分布
curl -s 'localhost:9090/api/v1/query?query=sum by (path) (rate(http_requests_total{status=~"5.."}[5m]))' \
  | python -m json.tool
```
拿 `path` 定位路由后用 `request_id` 串日志。若刚发过版，**先回滚再排查**
（回滚成本是秒级，继续排障的成本是每分钟都在伤用户）。

#### `HighLatencyP95`（warning，10m，P95 > 10s）
注意这个阈值（10s）是给"非流式"看的；SSE 长连接天然拉高尾部。

```bash
# 是不是模型侧变慢了？token 产出速率与每请求 token 一起看
curl -s 'localhost:9090/api/v1/query?query=rate(llm_tokens_total[5m])' | python -m json.tool
```
- 同时 `llm_tokens_per_request` 上升 ⇒ 模型产出变长/变慢；
- 只有延迟高 ⇒ 查 PG 与检索：rerank 熔断是否一直打开
  （日志里搜 `rerank` 与 `熔断`）、向量库是否变慢。
- **换过向量库的版本要额外注意**：Qdrant 的 `QDRANT_ON_DISK=1` 会显著增加检索延迟，
  它是有意的空间换时间，但会体现在这里。

#### `ConcurrencyShedding`（warning，5m）/ `ConcurrencySaturated`（warning，10m）
两条是**容量**信号，不是"挂了"：进程活着、依赖健康、5xx 也不一定高，
只是单实例的**在途并发**被占满了（`MAX_CONCURRENT_REQUESTS`）。后者是前者的
提前量 —— 还没开始拒人，但余量已不足。

```bash
# 1) 饱和率与拒绝速率（面板 `chatAgent · 服务总览` 最下面两块同源）
curl -s 'localhost:9090/api/v1/query?query=100*http_concurrency_in_flight/clamp_min(http_concurrency_limit,1)' | python -m json.tool
curl -s 'localhost:9090/api/v1/query?query=rate(http_requests_rejected_total{reason="concurrency"}[5m])' | python -m json.tool
# 2) 是不是被刷？看"拒绝"有没有特定的来源集中
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=200 app-stable | grep 并发已达上限
```

处置顺序（**先分清是"被刷"还是"真增长"**）：

1. **被刷**（同 IP / 同 Key 的请求速率异常，或大量 SSE 长连接开而不关）
   ⇒ 临时收紧 `RATE_LIMIT_*`；已确认是攻击时**不要急着调大上限** ——
   那只会把上游一起拖垮（背压的意义就是在这里把损失限制在自己这一层）。
2. **真增长** ⇒ 加副本（首选，见 `CAPACITY.md`），或调大 `MAX_CONCURRENT_REQUESTS`，
   **前提是先确认内存与上游配额扛得住**。
3. 加了上限却仍不生效 ⇒ 检查是否**多 worker**：该值是**单进程**上限，
   实例实际上限 = worker 数 × 该值（启动日志的 `limits.max_concurrent_requests` 可核对）。

> ⚠️ 上限为 0（默认）时 `http_concurrency_in_flight` / `http_concurrency_limit`
> 这两个指标**根本不存在**，两条规则都**不会触发** —— 面板上显示 0 是"没有闸门"
> 而不是"很空闲"。要区分只能看启动日志里回显的 `max_concurrent_requests`。
> ⚠️ `/health/*` 与 `/metrics` 刻意豁免本闸门：它们被拒会让编排重启实例、
> 让 Prometheus 在最该看数据的时候没有数据。


#### `StreamErrorsHigh`（critical，5m）
SSE 流中途抛异常。典型成因：检查点后端故障、模型超时、配额被拒。

```bash
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=500 app-stable | grep '流异常'
```
重点看 `DB_URL` 连通性（检查点写失败会让流在输出中途断掉）与上游模型是否可用。

> 这类问题**不会**在 HTTP 状态码上体现（响应头早就发出去了），
> 所以只看 `HighErrorRate` 会完全漏掉它 —— 这就是这条规则独立存在的原因。

#### `ExternalProbeFailing`（critical，2m）
**用户那条路断了**，但容器内部可能一切正常。

```bash
# ① 外部视角
curl -i http://<入口地址>/health/ready
# ② 内部视角（直接打容器端口，绕过入口）
docker compose --env-file .env -f deploy/app/docker-compose.yml exec app-stable \
  python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready').status)"
```
- 外部失败 + 内部正常 ⇒ **一定是入口/网络层**：nginx 配置、端口映射、安全组、DNS。
- 两边都失败 ⇒ 回到 `ChatAgentDown` 的处置。
- 顺手看 nginx 的错误日志：`docker compose logs --tail=100 nginx`。

#### `TLSCertExpiringSoon`（warning，1h）
证书过期是唯一"必然发生且能提前几十天发现"的故障。

```bash
openssl s_client -connect <域名>:443 </dev/null 2>/dev/null | openssl x509 -noout -dates
```
自动续期（Let's Encrypt）时先查**续期任务本身**是否失败 —— 这比证书本身常见得多。

#### `IndexIdentityMismatch`（warning，5m）
库里存着的向量与当前 `EMBEDDING_MODEL` **不是同一个模型灌的**。
最隐蔽的是"换了模型而维度恰好没变"（1024 → 1024）：旧向量还在库里、新查询用
新模型编码，两者落在**不同的向量空间**里 —— 检索命中当场变形、答案开始答非所问，
而 **QPS / 延迟 / 错误率全都正常**。这是本项目里唯一一条"答案质量掉了、
却没有任何别的信号"的故障，所以它必须自己报出来。

```bash
# 1) 库里的身份 vs 当前的 —— 启动日志那一行会同时打印两边的模型名与维度
docker compose --env-file .env -f deploy/app/docker-compose.yml logs app-stable | grep '索引身份自检'
# 2) 就绪探针的明细里也回显（零 IO，读的是启动时算出来的结论）
curl -s localhost:8000/health/ready | python -m json.tool
# 3) 指标：1=一致、0=不一致
curl -s 'localhost:9090/api/v1/query?query=rag_index_identity_ok' | python -m json.tool
```

处置顺序（**先分清"真换过模型"还是"只是名字写法变了"**）：

1. **真换过模型** ⇒ 这是**数据**问题，不是实例问题（摘流量、重启都解决不了）：
   把 `QDRANT_COLLECTION` / `PGVECTOR_TABLE` 改成新名字（如 `<原名>_d1024`）后
   重新灌库；**旧集合/表先保留**，用于比对与回滚。灌完确认 `rag_index_identity_ok` 变回 `1`。
   > ⚠️ 不要"就地清空再灌"：中途失败时你既没有新数据、也丢掉了旧数据。
2. **只是写法变了**（大小写、组织前缀）⇒ 把库里 `embedding_fingerprint` 那条记录
   改成当前指纹即可。
   > ⚠️ **不要为了让告警消失而直接删掉那条记录** —— 删掉等于把这项检查永久关掉，
   > 下一次真换模型时它不会再响。
3. 仍不放心就人工核对一遍：`EMBEDDING_MODEL` 与语料当初入库时用的是不是同一个
   （本次改造之前灌的库只有 `recorded`、证明不了历史，见 PRODUCTION_CONFIG 第 35 项）。

#### `IndexIdentityNeverReported`（warning，15m）
"没查出问题"与"根本没检查"长得一模一样，这条专门用来区分它们。
用持久化后端（pg / qdrant）时，启动自检**一定会**写下 `rag_index_identity_ok`；
它缺席 ⇒ 那次自检没跑成，于是 `IndexIdentityMismatch` 永远不会响，而外观全绿。

```bash
docker compose --env-file .env -f deploy/app/docker-compose.yml logs app-stable | grep '索引身份'
```

1. **日志里完全没有这一行** ⇒ `app/main.py::_report_index_identity` 没被调用或抛了异常
   （它会打 WARNING，一并搜）；确认启动走的确实是 `python -m app.serve` 那条路径。
2. **有这一行但 `status=error`** ⇒ 自检读不到向量库（连接参数错 / 权限不足），
   按日志里的异常先把连接修好。
3. **`status=unknown`** ⇒ 表/集合还没建（首次灌库之前属正常，灌完会变成 `recorded`）。
4. 确认 `RAG_VECTOR_STORE` 不是 `memory` —— 内存后端天然没有索引身份，
   "没有这个指标"是**预期**；生产不该用内存后端。

### 3.2 SLO 与错误预算（category: slo）

先读 `deploy/monitoring/SLO.md` 的处置表，这里只给补充：

| 告警 | 动作 |
|---|---|
| `SLOAvailabilityBurnFast`（critical） | 按 P1 处理。**25% 的 30 天预算在 1 小时内烧掉** —— 这是最紧急的一类 |
| `SLOAvailabilityBurnMedium`（critical） | 通常是长期错误源。看 6 小时错误按 `path` 是否集中在单一路由 |
| `SLOAvailabilityBurnSlow`（warning） | 排期。若确认是常态，**正式**下调 SLO（改 `SLO.md` + 表达式两处），别让它长期挂着 |
| `SLOLatencyBurnFast`（warning） | 按 `path` 定位：`/v1/reviews` 慢 ⇒ 审核队列表与 PG；`/health/*` 慢 ⇒ 探针在等外部依赖 |
| `SLOLatencyBurnSlow`（warning） | 排期优化（典型是 PG 查询缺索引） |

> **错误预算烧到什么程度要冻结发布**：见 `deploy/monitoring/ERROR_BUDGET_POLICY.md`。
> 一句话版：`Fast` 触发即冻结发布并优先回滚；`Slow` 触发则本周期只允许修复类变更。

### 3.3 成本与配额（category: cost）

#### `RateLimitSurging`（warning，10m）
看 `scope`：`ip` ⇒ 单来源刷量，保持限流并考虑在 nginx 层收紧；
`session` ⇒ 某客户端多开或循环重试。**是误伤就调阈值**，不要长期容忍正常用户被拒。

#### `QuotaExhausted`（warning，1h）
```bash
docker compose logs --tail=200 app-stable | grep '配额拒绝'
```
- 少数几个租户集中 ⇒ 业务增长，该加额度；
- 大量不同租户零散出现 ⇒ 怀疑 `TENANT_KEYS` 泄漏（走 `deploy/SECRETS.md` 的轮换流程）。

#### `AbnormalTokenBurn`（warning，15m）
```bash
curl -s 'localhost:9090/api/v1/query?query=sum(rate(llm_tokens_total[15m]))' | python -m json.tool
```
结合 `llm_budget_exceeded_total` 判断：单请求失控（收紧预算）还是请求量正常增长。

#### `BudgetExceededFrequent`（warning，30m）
`LLM_MAX_TOKENS_PER_REQUEST` 配得偏紧 ⇒ 回答经常被截断。
看 `llm_tokens_per_request` 的分位数，把预算调到覆盖 P95。

#### `ReviewBacklog`（warning，30m）
```bash
curl -sS -H "X-API-Key: $SERVE_API_KEY" localhost:8080/v1/reviews | python -m json.tool
```
**先查通知通道**（`REVIEW_NOTIFY_WEBHOOK`）——积压最常见的成因是通知没送到人手里，
而不是没人愿意审。

#### `FrequentClientDisconnects`（warning，10m）
服务侧已及时止损（不再计费），但用户没拿到回答。通常是首字节太慢或前端超时太短。
配合 `HighLatencyP95` 一起看。

### 3.4 安全（category: security）—— 任何一次都是 P0

#### `TenantMismatchDetected`（critical，单条即报）
```bash
docker compose logs --tail=500 app-stable | grep '租户自报与 Key 不符'
```
- 来自内部服务 ⇒ 配置写错（改配置即可）；
- 来自外部 ⇒ **按密钥泄漏处置**：立即轮换对应的 `TENANT_KEYS` 条目，
  并检查该租户的用量（`llm_tokens_total` 无租户标签，需查日志）。

#### `SessionOwnershipViolation`（critical，单条即报）
有人在猜/撞 `thread_id`。确认 id 是否可枚举：
```bash
grep -rn "def _new_thread_id\|thread_id =" app/ src/ | head
```
若是自增或时间戳，**改成带随机段**（可枚举的 id 让这条告警变成"迟早会被撞上"）。

#### `ClientDisconnectsSurging`（warning，5m）
"连上就断"也是一种低成本 DoS 试探。结合 `rate_limited_total{scope='ip'}` 看是否同源，
必要时收紧 nginx 的 `limit_req`。

### 3.5 平台自身（category: platform）—— "谁来发现监控坏了"

这一组回答的是前面四组**回答不了**的问题：**监控链路本身是不是还活着**。
它对应的场景是"**没有告警**"—— 而那既可能是真没故障，也可能是整个链路早就断了。

> ⚠️ 先记住一个结构性事实：`MonitoringStackDown` 与 `AlertDeliveryFailing`
> 这两条**自己也走同一条投递链路**，所以：
> - **Prometheus 挂了** ⇒ 没有求值者 ⇒ 这两条都不会响；
> - **Alertmanager 挂了** ⇒ 它们会 firing，但要经过 Alertmanager 才能送达 ⇒ 送不出去。
>
> 能穿透这两个死角的只有 `Watchdog`（靠"**该来的心跳没来**"）。所以下面的
> 处置顺序里，**心跳缺失是第一优先的入口**。

#### `Watchdog`（恒 firing，每 24h 到达一次）
**这条不需要处置**，它的用途是"反向"的：

- **收到了** ⇒ 采集 → 判定 → 路由 → 送达 四环全通，此时其它告警是否响起都可信；
- **到了该来的时间却没收到** ⇒ 立刻按下面的顺序逐环排查（别跳过任何一环）：
  ```bash
  # ① Prometheus 在不在、抓得到吗
  curl -s --noproxy '*' localhost:9090/-/healthy
  curl -s --noproxy '*' localhost:9090/api/v1/targets \
    | python -c "import json,sys;d=json.load(sys.stdin);print([(t['labels']['job'],t['health']) for t in d['data']['activeTargets']])"
  # ② 规则本身是不是 firing（Watchdog 用的是 vector(1)，它必须恒 firing）
  curl -s --noproxy '*' 'localhost:9090/api/v1/query?query=ALERTS{alertname="Watchdog"}'
  # ③ Alertmanager 在不在、有没有解析错误
  curl -s --noproxy '*' localhost:9093/-/healthy
  curl -s --noproxy '*' localhost:9093/api/v2/status | python -m json.tool | head -30
  # ④ 投递有没有失败（含"通道未配置"这种静默丢消息）
  docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=200 alertmanager | grep -i "fail\|error"
  docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=200 alert-sink  | grep -i "未配置\|失败"
  ```
  `--noproxy '*'` 不是可选项：本机若设了 `http_proxy`，curl 会把这些请求送进代理
  （本项目已经踩过两次，表现成"服务像是没起来"）。用 `curl` 而不是浏览器，
  是为了排除"浏览器走了系统代理/缓存"这类干扰。

#### `MonitoringStackDown`（critical，5m）
按 `job` 标签分辨，处置完全不同：

| job | 后果 | 第一件事 |
|---|---|---|
| `alertmanager` | **告警从此发不出去**（Watchdog 也会停） | `docker compose ... logs --tail=200 alertmanager`；先恢复，再补发消息 |
| `blackbox-exporter` | `probe_success` **停止产出**（不是变 0）⇒ `ExternalProbeFailing` 也**不会触发** | 恢复它，然后**回看这段时间入口是否真的断过** |
| `node-exporter` | 磁盘指标与备份指标一起消失 | 恢复它；期间 `df -h` 要人工看 |
| `prometheus` | 所有指标停止更新（但这条规则本身也靠它求值 ⇒ 通常看不到它响） | 以 Watchdog 缺失为准 |

> `blackbox-exporter` 那一行是本项目最隐蔽的一种失效：**用"没有序列"表达故障，
> 而规则写的是 `== 0`，于是静默失效**。这正是 `MonitoringStackDown` 存在的理由。

#### `AlertDeliveryFailing`（critical，10m）
`Alertmanager` 在喊"我有通知发不出去"。最常见的原因**不是**网络，而是**通道没配**：
`alert-sink` 在通道为空时的设计是"只记一条日志、接口照回 200" —— 从 Alertmanager
的视角看投递是**成功**的。所以两种成因要分开看：

```bash
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=200 alert-sink
# 见到 "未配置通道，仅记日志" ⇒ 是配置问题，不是网络问题
python deploy/monitoring/alert_sink.py --selftest     # 真发一条，看对方返回体
```
原始错误在 `http://localhost:9093/#/status` 的 **failed notifications** 一节。

**若 `failed notifications` 里的 `integration` 是 `email`**（即启用了邮件通道），
按这三条查 —— 都是"配置看着对、但没人收到"：

| 现象 | 根因 | 确认方式 |
|---|---|---|
| `no such host` / 连接超时 | `smtp_smarthost` 写错端口：**465 是 implicit TLS**，需要额外 `smtp_force_implicit_tls: true`；587 才是 STARTTLS | `openssl s_client -connect <host>:465 </dev/null \| head -1` |
| 认证失败 | 密码文件没挂进容器，或 `smtp_from` 与 `smtp_auth_username` 不一致（多数邮件服务要求两者相同） | `docker compose ... exec alertmanager cat /etc/alertmanager/smtp_password`（**只在排障时**，别把输出粘进工单） |
| 对方拒收 | 收件人被机器人/网关列入拒收名单、或进了垃圾箱 | 看 `failed notifications` 里 SMTP 的原始错误码 |

> 邮件**默认是关闭的**（启用步骤与四个坑写在 `alertmanager.yml` 末尾）。如果你从没启用过
> 却看到 `integration=email` 在失败，说明有人放开了一半：检查 `片段①/②/③` 是否只取消注释了
> 一部分，以及 compose 里那行 `smtp_password` 的 volume 是否也放开了。

#### `BackupLoopFailing`（warning，15h 未成功）
```bash
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=100 backup-pg backup-qdrant
ls -lt backups/ | head
python backups/../scripts/pg_backup.py backup     # 手工补一次（会同时刷新指标文件）
```
`target=qdrant` 且日志里是"跳过：Qdrant 不可达" ⇒ 说明该后端**本来就没启用**，
这条告警会被 `backup_backend_skipped` 自动排除；若仍收到，说明那个 gauge 没写出去
（回头查 `backups/backup-qdrant.prom` 是否存在）。

**恢复后必须做一次**（这是"备份存在"与"备份可用"的分界）：
```bash
python scripts/pg_backup.py drill
python scripts/qdrant_backup.py drill
```

#### `BackupMetricsMissing`（warning，30m）
指标**根本不存在**，比上一条更往前一步。三处依次查，别跳：
```bash
docker compose --env-file .env -f deploy/app/docker-compose.yml ps        # ① 三个容器在不在
ls -la backups/*.prom                                     # ② 循环写没写出来
curl -s localhost:9100/metrics | grep backup_             # ③ exporter 读没读到
```
第 ③ 步是关键：它区分『没写』（①②③ 都空）与『没读到』（②有、③空 ⇒
`--collector.textfile.directory` 或挂载路径不对）。

#### `DiskSpaceLow`（critical，可用 < 15%）
先定位再动手 —— 这条告警最常见的成因是**容器日志**，而不是业务数据：
```bash
df -h
docker system df -v          # 看 LOGS 一列（各容器 json-file 日志占用）
du -sh backups/ promdata/ 2>/dev/null
```
- **日志**：确认 `deploy/app/docker-compose.yml` 里每个服务的 `logging` 轮转生效
  （顶部有 `x-logging` 锚点；新增服务时漏挂是唯一容易出错的地方）；
- **`backups/`**：`BACKUP_KEEP` 是否与 `BACKUP_INTERVAL_HOURS` 匹配
  （两者相乘 = 可回溯窗口，改动一个必须改另一个）；
- **`promdata`**：Prometheus TSDB 保留期；**不要**为了省空间把它删掉 ——
  那会同时抹掉 SLO 的 30 天窗口与全部燃尽率历史。

清理完成后确认告警**自动恢复**（receiver 上应收到 resolved 通知）；
并回头查这段时间是否有**被掩盖的写入失败**（磁盘满时 PG 会先转只读）。

---

## 四、日常巡检（不需要告警也要做）

### 4.1 备份巡检（最容易被遗忘的一项）

> **"容器在运行"不等于"备份在发生"。** 现在有三层信号，从弱到强：
> ① 容器在跑（最弱，几乎无意义）；② `backup_last_success_timestamp_seconds` 在推进；
> ③ **备份文件的时间戳是今天**（唯一直接证据）。

```bash
# ③ 最直接：第一行必须是今天（或上一个备份周期内）的
ls -lt backups/ | head -5
# ② 指标侧（比翻文件更快，也是告警依赖的那份数据）
curl -s localhost:9100/metrics | grep '^backup_'
```

**两个备份循环现在是"同一槽位对齐"的**：每轮结束时会计算"距离下一个
`BACKUP_INTERVAL_HOURS` 整数倍时刻还有多少秒"并 sleep 到那一刻，所以
pg 与 qdrant 的快照**在同一分钟甚至同一秒内开始**，而不是各自按启动时间漂移。

> **为什么要对齐**：它们各自独立时，恢复会拿到**两个不同时刻**的快照 ——
> 向量库里的 point 与 PG 里的 chunk 元数据可能对不上（少一批或多一批），
> 而这种不一致**不会报错**，只会在检索结果里表现为"引用指向不存在的块"。
> 对齐之后两者的一致性窗口是"两个备份的耗时差"（秒级）。

**配对检查（恢复前必须先做）**：

```bash
python scripts/backup_manifest.py           # 列出成对的备份；退出码 0=全配对，1=有错配
python scripts/backup_manifest.py --json    # 机器可读
```

> **恢复只认清单，不认时间戳**：从"最近的 pg dump"和"最近的 qdrant 快照"
> 各自取最新，是恢复时最常见的错误做法 —— 它们很可能差了好几个小时。

**恢复演练（每季度，必须真做一次）**：

```bash
python scripts/pg_backup.py drill       # 备份 → 恢复到临时库 → 逐表比对
python scripts/qdrant_backup.py drill   # 建快照 → 校验结构 → 打印恢复步骤
```

> Qdrant 的恢复**刻意不自动执行**：它会覆盖当前集合，属破坏性动作，必须人工确认
> （步骤由 `drill` 打印）。Qdrant 的数据可以从语料重建，但重建要花 embedding 钱
> 与几小时，所以快照的定位是"降低 RTO"，不是"唯一真相来源"。

**演练不再"靠人记着"**。每个 `drill` 结束都会把结果写进 `backups/drill-<target>.prom`
（node-exporter 的 textfile 采集），于是有三条告警在盯这件事：

| 告警 | 含义 | 响应 |
|---|---|---|
| `BackupDrillMissing` | **从来没成功演练过** —— 首次部署后它就会响，这是刻意的 | 跑一次 drill（见上） |
| `BackupDrillOverdue` | 超过 100 天没成功演练过 | 排到本周 |
| `BackupDrillFailed` | 上次演练**没通过** | **最急**：说明备份此刻很可能恢复不了 |

面板 `chatAgent · 平台自身` 最下面一格就是"距上次成功演练多久"。

> **为什么不做成定时自动演练**：`pg_backup.py` 要调 `pg_dump` / `pg_restore`，
> 执行通道是「`docker exec` 进 PG 容器」或「宿主机 PATH 里有客户端」——
> **容器里两条都不成立**（容器内没有 docker，app 镜像里也没有 PG 客户端）。
> 做一个跑不通的定时容器，比让人手动跑一次更糟：它会给人"已经在自动演练了"的错觉。

不可自动化的是"**你的真实数据**能不能恢复"；可自动化的那一半已经补上了：
**CI 每月真跑一次 `drill`**（`.github/workflows/ci.yml` 的 `drill` job，用 service
container 起真 PG，跑完整条备份→恢复→比对链）。它证明**脚本没坏** ——
restore 路径、临时库的建与删、行数比对，这些一旦被改坏，只有在真需要恢复的那天才会暴露。

**保留窗口是算出来的，不是猜的**：`BACKUP_INTERVAL_HOURS × BACKUP_KEEP` 才是
可回溯天数。默认 `6h × 56 = 14 天`（pg）、`6h × 28 = 7 天`（qdrant）。
**只改间隔不改份数**，窗口会静默缩短（这是最容易犯的一次错）。

### 4.2 磁盘与资源

```bash
df -h                                        # 备份目录、Prometheus TSDB、Loki 都会长
docker system df -v                          # 悬空镜像与**各容器的 json-file 日志占用**
curl -s localhost:9100/metrics | grep node_filesystem_avail_bytes
```
`DiskSpaceLow` 告警只在**低于 15%** 时响，所以巡检要更早看趋势。
容器日志现在有轮转（每个服务 ≤ 30MB），若 `docker system df` 的 LOGS 一列
明显超出"服务数 × 30MB"，说明有服务漏挂 `logging`（新增服务时的常见疏漏）。

`promdata` 不设 retention 会一直长（见 `prometheus.yml` 的说明）。

### 4.3 故障演练（每季度挑一条做）

代码里的降级路径（熔断、配额、限流、断连止损）**不演练就等于不存在** ——
它们平时不生效，出事时才发现写错了，那时最贵。可演练的清单：

| 演练 | 怎么造 | 期望结果 |
|---|---|---|
| embedding 熔断 | 把 `EMBEDDING_API_BASE` 指到一个黑洞地址 | 检索走 BM25 回退，服务不崩，日志有熔断告警 |
| rerank 熔断 | 把 `RERANK_API_BASE` 指到黑洞 | `HybridRetriever.last_rerank_ok is False`，结果退回 RRF 融合顺序 |
| Redis 不可用 | `docker compose stop redis` | 退到进程内计数（**不是放行**），启动日志有明确提示 |
| PG 不可用 | `docker compose stop pgvector` | `/health/ready` 报 `db ok=false`；在途 SSE 被止损而非挂死 |
| 外部入口断 | 改 nginx 端口映射后 reload | `ExternalProbeFailing` 触发，而 `ChatAgentDown` **不**触发（这是两条规则分工的验证） |

演练完把结论记进当天的 `.workbuddy/memory/YYYY-MM-DD.md`。

### 4.4 知识库与源文件的一致性（下线文档后必做）

**语料删了/改了，向量库不会自己跟着变。** 灌库是幂等的（重复灌不产生副本），
但**没有反向路径** —— 这是"答案里有已下线内容"和"声明已删除却仍可被搜出来"的共同根因。

```bash
python scripts/tasks.py kb-prune -- --json | head -40   # 预演，什么都不删
python scripts/tasks.py kb-prune -- --apply             # 只删「源已消失」的
python scripts/tasks.py kb-prune -- --include-stale --apply   # 连「内容已变」的旧块一起清
```

判据来自块上的 `source_path`（相对语料根的路径）与 `content_hash`（文件内容指纹），
在 `load_chunks` 里逐文件打上去。

| 现象 | 含义 | 怎么办 |
|---|---|---|
| `源已消失 N 份` | 文件被删/移走/改名，但那批块还在库里 | 直接 `--apply` |
| `内容已变 N 份` | 文件还在但内容改了 ⇒ 新块入库、**旧块一条没删**，新旧并存 | **先确认新版本已灌进去**，再加 `--include-stale --apply`（顺序反了会出现"知识库短暂没内容"） |
| `无法判定 N 份` | 老数据没指纹（本次改造前灌的） | **不会被删**。重新灌一次库即可补上指纹 |
| 退出码 `2` | 语料目录**扫不到任何文件**，或用了内存后端 | **这不是通过**。几乎总是"语料没挂进容器 / `DOCS_DIR` 指错"，先 `ls` 确认再处理 |

⚠️ 退出码 `2` 必须与 `1`（执行失败）分开看：把它当成"清理失败"去修，会掩盖真正的
"语料没挂上"——而那意味着检索恒空，比孤儿块严重得多。

---

## 五、常见误判（踩过就记下来）

1. **"面板上 FIRING 了，说明通知过了"** —— Fire 与"送达"是两件事，见 1.1 的表。
2. **"容器在运行，所以备份在做"** —— 见 4.1。
3. **"staging 也是 production 语义，所以 staging 通过 = 生产通过"** ——
   前提是 staging 真的写了 `APP_ENV=production`。写成 `staging` 会**静默关掉鉴权、
   打开 /docs**，于是 staging 唯一不同的地方恰好是最该验证的那层（见 `deploy/CANARY.md`）。
4. **"灰度只影响 10% 用户"** —— 灰度真正的价值是**在影响 10% 的时候拿到证据**。
   不看证据的灰度只是"慢一点的发布"。
5. **"`/health/live` 绿了就是健康的"** —— liveness 刻意只回进程标识、不查依赖
   （失败才重启容器）。依赖健康与否要看 `/health/ready`。
