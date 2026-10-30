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
| 送到了但没转 | Alertmanager 里能看到 FIRING，机器人没消息 | `docker compose --env-file .env -f deploy/app/docker-compose.yml logs alert-sink` 看是否有转换失败 |
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
- **运行现场是否真的落了库** —— 退回内存时服务照常工作、回答照常出，**只在容器重启后
  丢现场**（`/health/ready` 的 `run_state` 明细是唯一能看出它的地方）。
- **跨轮历史是否可用** —— 没配 `DB_URL` 时模型只看得到当轮那一句，而所有指标都正常
  （启动日志里有一条专门的 warning）。

第 ⑤ 步只在启用了邮件时才有意义，但那时它**不能省**：邮件是唯一**不经 alert-sink** 的
通道（走 Alertmanager 原生 `email_configs`），两条链路在 Alertmanager 里就分叉了 ——
所以 `alert_sink.py --selftest` 全绿**证明不了邮件能送达**。

它要起一整套容器（几分钟），所以刻意不放进每次 CI 的 push 上，而是手动 / 每晚定时
（`ci.yml` 的 `e2e` job）。

#### 后端地址现在是**请求时**解析的（2026-10-06 改；在此之前要手工 reload 才更新）

`nginx.conf` 的 upstream 用了 `resolver` + `zone` + 每个 `server` 上的 `resolve`：
容器换 IP 后**最多 10 秒** nginx 就会自己发现（`valid=10s`），**不需要**再手工 reload。

以前不是这样，而那时的故障形态值得记下来 —— 它决定了那三行为什么必须写在配置里：

- `server app-stable:8000` 只在**加载配置那一刻**解析一次 ⇒ 谁重建了 app 容器
  （换镜像 tag / 手工 `up -d`）换了 IP，nginx 会继续往**旧地址**发；
- 后果一：走 nginx 那条路**全站 502 且不自愈**（不是灰度那 10%：目标地址是错的，
  权重再大也没用）；
- 后果二：那个旧 IP 还可能被**别的容器**拿走 ⇒ 用户请求被发到一个完全无关的服务上；
- 而**诊断极其容易误判**：`/health/ready` 正常、容器都在跑、Prometheus 的 `up` 全是 1、
  日志里没有错误 —— 只有经 nginx 的那条路是死的。

```bash
# 复核（任何时候想知道"nginx 现在把流量发到哪儿"）：
docker compose --env-file .env -f deploy/app/docker-compose.yml exec -T nginx \
  nginx -T 2>/dev/null | grep -A8 "upstream chatagent_backend"
```

⚠️ **配置与镜像版本是绑定的，不能只改一边**：`resolve` 参数在**开源版是 1.27.3 起**才有的
（官方原文："Prior to version 1.27.3, this parameter was available only as part of our
commercial subscription"），所以 compose 里钉的是 `nginx:1.27.5-alpine`。
**降这个版本不是"退回旧行为"，是 nginx 因为配置语法错直接起不来（入口整个没了）。**
守卫：`tests/test_deploy_topology.py::test_nginx_upstream_is_dynamically_resolved`
同时核对"镜像 ≥ 1.27.3"与"配置里确实用了 `zone`/`resolver`/`resolve` 三件套"。

**仍然存在的一个窗口（已知，刻意没处理）**：冷启动时 nginx 起得来、但应用进程还没开始
监听（init、导入、连库要几秒）⇒ 那几秒里请求会 502。它**自己会恢复**，
与上面那个"永久 502"是两件不同的事。要消掉它得给 app 加健康检查、并让 nginx 等
`condition: service_healthy` 才启动 —— 代价是健康检查一旦判错，nginx 就永远不启动
（全站不可用），属于要单独拍板的改动（`ASSESSMENT_production_readiness.md` §5）。

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
| **P2** | `SLO*BurnMedium` / `SLOBudgetExhausted` / `HighLatencyP95` / 成本组告警 / 平台组 warning（`BackupLoopFailing`、`BackupMetricsMissing`、`TLSCertProbeDown`） | 当天 | 值班（可异步） |
| **P3** | `SLO*BurnSlow` / `SLOBudgetLow` / `TLSCertExpiringSoon` | 排期（一周内） | 记录，不必叫人 |

⚠️ `SLOBudgetExhausted` 的标签是 `severity: critical`，但它**不是 P1**：
critical 在这里指"**必须当天有人做出冻结决定**"（错误预算已经花光，按
`ERROR_BUDGET_POLICY.md` 要全面冻结功能变更 + 48 小时内复盘），
而不是"服务正在坏" —— 服务很可能早就恢复了，只是账没了。
把它当成 P1 冲上去查故障，会查到一个"系统一切正常"的现场。

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
- 进程死了 ⇒ 看日志里的最后一屏。**特别注意启动期的两类拒绝启动**（都是特性不是 bug）：
  ① `APP_ENV=production` 且**未启用用户鉴权（`AUTH_*`）** ⇒ 没有任何人能登录，
     于是**故意**拒绝启动；
  ② 环境里残留了**已删除的鉴权开关**（`SERVE_API_KEY` / `REQUIRE_AUTH`，**留空也算**）
     ⇒ 同样拒绝启动，日志会逐条列出该删哪一行。
  ⚠️ 这两条**都不受任何环境变量影响**，只能去配 `AUTH_*` / 去删那两行 ——
  没有任何开关能把鉴权要求关掉（那正是这两个变量被删除的原因）。
- 进程活着但探针失败 ⇒ 看 `/health/ready` 的 `checks` 明细，定位是哪个依赖。
- 只是 `/metrics` 被拒 ⇒ nginx 的 `location = /metrics` 只允许内网段；
  Prometheus 若在宿主机上跑，源 IP 可能不在白名单里。

#### `ChatAgentReadinessDegraded`（warning，5m）
readiness 一直 503 ⇒ 实例被摘掉流量，等于"没在服务"。

```bash
curl -sS localhost:8000/health/ready | python -m json.tool
```
逐项看 `llm / db / vector_store / run_state` 哪个 `ok=false`。

> ⚠️ **`run_state` 这一项是最容易搞错的**：运行现场若降级成内存实现，
> 它的文案会明确写 `memory（进程内存，重启丢现场！）`。
> 这是判断"持久化到底有没有接上"的**唯一可靠信号** —— 服务看起来一切正常，
> 但所有会话状态在重启后会丢。
>
> 这一项的另外两种文案别读错：
> - `postgres（表尚未建立：首次写入时建）` —— 正常（还没运行被打断过）；
> - `⚠️ 配了 DB_URL 但数据库当前连不上…`（`ok=false`）—— 现场**实际写不进**，
>   重启即丢。它一定伴随 `checks.db.ok=false`。
>   ⚠️ 2026-10-30 之前这一档报的是 `ok:true` + "表尚未建立"，是**假绿灯**
>   （那时探针只读进程内属性、不参考 db 探测结果）；现在 `readiness()` 会把 db 的
>   结论传给这一项，改回去就会重新变成假绿灯。

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


#### `EventLoopBlocked`（warning，10m）

事件循环被**同步调用**占住 —— 这段时间里**所有**并发请求一起停着。它**不产生任何错误**
（错误率、超时率都正常），唯一表现是延迟上升；而延迟上升的原因有几十个，所以需要这条
专属信号把「卡住了」与「慢」分开。

```bash
# 1) 阻塞时间占比（秒/秒）—— 就是告警在判的那个量
curl -s 'localhost:9090/api/v1/query?query=sum(rate(event_loop_lag_seconds_sum[10m]))' | python -m json.tool
# 2) 单次阻塞有多长（P50）
curl -s 'localhost:9090/api/v1/query?query=histogram_quantile(0.5,sum(rate(event_loop_lag_seconds_bucket[10m]))by(le))' | python -m json.tool
```

判读：**形状不同，要去的地方完全不同。**

| `event_loop_lag_seconds` 的形状 | 含义 | 先去哪找 |
| --- | --- | --- |
| 偶发的数百毫秒尖峰 | 某个**一次性同步调用**（读大文件、同步 HTTP、`time.sleep`） | 最近新增的工具 / 压缩路径 |
| 持续的小值（几十毫秒）不断出现 | **高频小阻塞**（日志、JSON 序列化、正则回溯） | 请求路径上的每个同步操作 |
| 持续小值 **＋ RSS 同步上涨** | **GC 越来越重**（堆变大 ⇒ 每次回收更久），或真在泄漏 | `python_gc_collections_total` 的增速 + `process_resident_memory_bytes` |

第三步（区分「在算」还是「在等」）：拿 `process_cpu_seconds_total` 除以
`process_uptime_seconds` —— 接近核数说明真在烧 CPU；远低于核数说明时间主要花在
等 I/O 或等上游模型上，`EventLoopBlocked` 就不是主因。

```bash
# 内存是否在稳定上涨（泄漏的第一个信号）
curl -s 'localhost:9090/api/v1/query?query=process_resident_memory_bytes' | python -m json.tool
# 各代 GC 次数（第 2 代也在涨 ⇒ 有对象活过了两轮回收）
curl -s 'localhost:9090/api/v1/query?query=rate(python_gc_collections_total[30m])' | python -m json.tool
```

⚠️ 这三条是**下钻**用的，没有独立告警：内存问题的最终故障形态是 OOM / 容器重启
（由 k8s 侧兜住），GC 抖动由 `EventLoopBlocked` 兜住。它们负责回答「是内存涨了、
GC 在抖、还是 CPU 打满」，不负责发通知。RSS 的绝对值**跨部署不可比**
（取决于语料规模与并发），要设阈值必须先在目标环境回填基线（见 `CAPACITY.md` §五）。

处置顺序：

1. **先对照部署时间**：本告警从「零」变成「持续 >0」的时刻，通常就是引入同步调用的那次
   发布 —— 用 `CHANGELOG.md` 与 `release` 标签定位到具体提交，比逐个函数读代码快得多。
2. **找法**：`tests/test_event_loop_offload.py` 守的是**已知**的那些调用点（已经被移到线程里的），
   它保证不了「新加的路径也不阻塞」。所以新路径要自己查：搜 `async def` 里有没有
   `open()` / `requests.` / `time.sleep` / 大 `json.dumps` / 同步 DB 客户端。
3. **修法**：把阻塞调用移到线程里（`await asyncio.to_thread(...)`）。
   ⚠️ **不是**调大 `MAX_CONCURRENT_REQUESTS` —— 那只会让更多请求一起排队等同一个阻塞，
   把等待时间摊给更多人。
4. 若确认是**长期误报**（占比在阈值附近晃而延迟正常）⇒ 先看 `event_loop_lag_seconds`
   的分位再定阈值，**不要直接删掉这条规则**：它是这类问题唯一的直接证据。

> ⚠️ 改探针之前必读的两条实测约束（依据在 `app/loopwatch.py` 的模块说明）：
> ① 心跳间隔 **0.1s 必须小于要检测的阻塞时长** —— 阻塞若完全落在两次心跳之间，
> 心跳定时器仍会按时到期、`lag ≈ 0`，**什么都看不到**（实测：间隔 1s 时一次 200ms 的
> 同步阻塞**完全没被记录**）；
> ② 阈值 50ms 是 **Windows 定时器量化（约 15ms）**的 3 倍余量，Linux 上基线更低、更安全。


#### `StreamErrorsHigh`（critical，5m）
SSE 流中途抛异常。典型成因：运行现场落库故障、模型超时、配额被拒。

```bash
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=500 app-stable | grep '流异常'
```
重点看 `DB_URL` 连通性（落库失败会让流在输出中途断掉）与上游模型是否可用。

> 这类问题**不会**在 HTTP 状态码上体现（响应头早就发出去了），
> 所以只看 `HighErrorRate` 会完全漏掉它 —— 这就是这条规则独立存在的原因。

#### `IdempotencyConflictsHigh`（warning，10m）
**客户端在拿同一个 `Idempotency-Key` 反复提交。** 两种含义完全不同的成因，
先分档再动手：

```bash
# ① 分档：是在途重试（客户端等不及）还是不可重放（服务端没缓存住）
curl -s 'localhost:9090/api/v1/query?query=sum by (reason) (increase(idempotency_conflict_total[15m]))' | python -m json.tool
# ② 被正确吸收的重试有多少（与上面一起看才是全貌）
curl -s 'localhost:9090/api/v1/query?query=increase(idempotency_replay_total[15m])' | python -m json.tool
# ③ 有没有因为回答太长而没能缓存下来
curl -s 'localhost:9090/api/v1/query?query=increase(idempotency_truncated_total[15m])' | python -m json.tool
```

- `reason=in_flight` 占多数 ⇒ **请求比客户端的重试窗口更慢**。对照
  `HighLatencyP95` 与 `llm_first_token_seconds`（`mode="stream"`）确认上游是不是变慢了。
  ⚠️ 正确做法是让客户端把超时调长，**不是**放宽服务端的幂等 —— 放宽等于把
  "重复计费"重新放出来。
- `reason=not_replayable` 占多数 ⇒ 上一次已完成，但响应超过
  `IDEMPOTENCY_MAX_BODY_BYTES` 没被缓存。若 `idempotency_truncated_total` 同向在涨，
  按回答的实际大小调大它（这是**内存换重放能力**的取舍）。
- `idempotency_mismatch_total` 在涨 ⇒ **客户端 bug**（同键换了载荷）。
  服务端行为正确（草案 2.2 的 MUST NOT），要改的是客户端。
- `idempotency_rejected_total` 在涨 ⇒ 客户端没按 `IDEMPOTENCY_DOC_URL`
  公布的格式带键（缺头 = 开了 `IDEMPOTENCY_REQUIRE_KEY` 却没发；含非法字符 = 键里有
  控制字符/引号/空格）。把它当**接入事故**处理，不要放宽校验。

> 这条规则只在客户端真的带幂等键时才有数据。序列为空 ⇒ 规则不触发，
> 那说明**当前没有任何客户端在用这个能力**（不是"没问题"）。

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
- 顺手看 nginx 的错误日志：`docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=100 nginx`。

#### `TLSCertExpiringSoon`（warning，1h）
证书过期是唯一"必然发生且能提前几十天发现"的故障。

```bash
openssl s_client -connect <域名>:443 </dev/null 2>/dev/null | openssl x509 -noout -dates
```
自动续期（Let's Encrypt）时先查**续期任务本身**是否失败 —— 这比证书本身常见得多。

#### `TLSCertProbeDown`（warning，30m）
**证书探活自己挂了** —— 它与上一条的分工要说清楚：上一条回答"证书快到期了吗"，
这一条回答"**证书监控还在工作吗**"。只有这一条是健康的，上一条才是可信的：
`TLSCertExpiringSoon` 读的 `probe_ssl_earliest_cert_expiry` **只在探活成功时才产出序列**，
探活一挂，上一条就变成哑弹，而它不会以任何方式表现出来。

```bash
curl -s --noproxy '*' localhost:9090/api/v1/targets | python -m json.tool | grep -A6 blackbox-tls
```

按报错分成三种，处置不同：

| targets 里的报错 | 含义 | 怎么做 |
|---|---|---|
| DNS / 域名解析失败 | 域名还是 `REPLACE-WITH-YOUR-DOMAIN` 占位符（配置步骤没做完） | 替换它，然后 `docker compose --env-file .env -f deploy/app/docker-compose.yml restart prometheus` |
| TLS / HTTP 失败 | 域名填了，但 `https_2xx` 模块没拿到 200 | 按 `ExternalProbeFailing` 那套查入口链路（DNS、证书、安全组、端口） |
| 连不上 blackbox-exporter | 探活组件本身不可达 | 看 `MonitoringStackDown`（它也会同时响），先恢复组件 |

⚠️ 若你的 HTTPS 入口**不在本编排内**（TLS 由宿主机上已有的 LB / 云负载均衡终结），
那这个 job 在本编排里永远探测不到东西 —— 正确做法是**删掉 `blackbox-tls` 这个 job
和这条规则**，而不是留一条常年 firing 的告警。常年 firing 的告警等于没有告警：
所有人都会学会忽略它，那时真问题也一起被忽略。

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

#### `CitationUnsupportedFrequent`（warning，10m）

**答案里出现了本轮检索命中里不存在的引用编号** —— 例如本轮只命中 3 条，答案却标了
`[7]`。这是**可以用代码证明的编造引用**，不是主观判断：用户会照着这个编号去核对出处，
而它指向的东西不存在。它比"没有引用"更糟，因为**它让人以为有据可查**。

```bash
# 定位是哪几个编号：从响应 / 日志里找 citation_check
docker compose --env-file .env -f deploy/app/docker-compose.yml logs app-stable \
  | grep '引用校验未通过'
# 指标按状态拆分，先看是哪一类在涨
curl -s localhost:8000/metrics | grep '^citation_check'
```

1. 看日志里 `refs` 是哪几个编号、`citation_check` 事件里 `citations` 有几条。
   越界的那些就是编造出来的。
2. **常见成因是换了模型或改了提示词**后模型开始自己编号 —— 先确认 `PROMPT_VERSION`
   与 `EMBEDDING_MODEL` 最近有没有人动过（`git log` + 启动日志回显）。
3. ⚠️ **别急着把 `RAG_CITATION_CHECK` 关掉**。这条告警响本身就是有价值的信号；
   关掉它只是让"编造引用"重新变得不可见。若已配 `RAG_CITATION_CHECK_ACTION=block`，
   这类回答的引用会被吞掉（**答案照常返回**）—— 核对"消失的引用"是否都是这一类。
4. **模型只是编号习惯不同**（比如把整段引用写成 `[资料1]`）**不属于本告警** ——
   本告警只认数字编号（`[n]` / `【n】` / `［n］`，**刻意不认 `(n)`**：正文里
   "第(3)条"太常见，认了就是稳定误报，而稳定误报的判据最后一定会被人关掉）。
5. `status=weak` / `degraded` **不会**触发本告警（前者是"改写幅度大"的启发式、
   后者是"源文件被删"的数据状态，都不该拿拦用户答案）—— 它们只进
   `citation_check_total{status=...}`，用于巡检而非告警。

#### `SharedBackendDegraded`（warning，5m）

**Redis（限流 / 日配额 / 幂等的共享后端）不可达，实现退到了进程内计数。**
这是最需要告警的一类：**保护机制失效，而服务一切正常** —— 请求成功率、延迟、错误率
全都看不出异常，限流看起来也仍在工作。单副本下只是"计数更准一点"，
**多副本下等于把限流与日配额放宽到副本数倍**。

```bash
# 1) 定位是哪个组件在退回
curl -s localhost:8000/metrics | grep '^shared_backend_errors_total'
# 2) 直接验 Redis 可达性与连接数
redis-cli -h <host> -p 6379 ping
redis-cli -h <host> -p 6379 info clients | grep -E 'connected_clients|maxclients'
```

1. 按 `component` 标签定位：`quota` ⇒ 查这段时间的日额度是否被刷穿；
   `ratelimit` ⇒ 查是否有来源绕过了限流（`rate_limited_total` 会明显下降）。
2. 看启动自检里的 `shared_configured` 与 `backend` 两栏 —— 前者是"打算用共享后端"、
   后者是"**实际在用**"。配了却常年 `backend=memory`，说明地址/密码就没连上。
3. 恢复后**必须人工核对**这段时间的实际消耗（日志里的「配额拒绝」与限流触发次数），
   必要时追溯。**不要只看到告警消失就收工**。
4. ⚠️ 别为了让告警消失而去掉 `REDIS_URL` —— 那等于永久放弃多副本保护
   （限流 / 日配额 / 幂等三者共用它，去掉之后三者会一起退回进程内计数）。

#### `RequestTimeoutFrequent`（warning，5m）

**请求级总时限被顶掉：流被终止，用户拿到半个回答。** 它**不产生 5xx**（正文已经下发
一部分了），所以 `HighErrorRate` 与 SLO 燃尽**都看不见它** —— 不单独看这条，
就只剩"等用户投诉"这一条路径。

分诊顺序（这条告警的 runbook 只有一件事：判断"该改配置"还是"该修上游"）：

```bash
curl -s localhost:8000/metrics | grep -E '^llm_first_token_seconds|^llm_call_duration_seconds|^rag_stage_duration_seconds'
```

1. `llm_first_token_seconds` 慢 ⇒ 上游模型首字节慢（按 `FirstTokenTooSlow` 处置）。
2. `llm_call_duration_seconds` 整体变慢 ⇒ 上游抖动（按 `LlmCallErrorsHigh` 的分诊走）。
3. `rag_stage_duration_seconds` 某一段变慢 ⇒ 按 `RagRetrievalDegraded` 处置。
4. 三者都正常 ⇒ 是 `REQUEST_TIMEOUT_SECONDS` 配得偏紧。对照
   `llm_tokens_per_request` 的 P95 估算合理值。
   ⚠️ **改大它之前先确认前端 fetch 与 nginx 的 `proxy_read_timeout` 都比它长** ——
   否则用户侧会先在别处超时，问题只是从"服务端超时"搬到"入口超时"。

#### `RagRetrievalDegraded`（warning，5m）

**检索抛异常后降级为"无上下文继续作答"**：接口 200、答案照出，只是没有引用，
用户看到的是"依据不足"。此前这里只有一行 warning 日志，于是"检索一直失败"
在指标上**完全看不出来**。

1. **先看 `RagRetrievalDegraded` 的 `stage` 标签**：`kb_search` 是主问答链路、
   `agent_search` 是 agent 链路。只有一条在涨 ⇒ 问题在对应链路的调用姿势，不是检索器本身。
2. 看 `circuit_open` 是否同时为 1 —— 是则按 `RetrievalCircuitOpen` 处置
   （**熔断打开时降级是预期行为**，别把两条告警当成两个问题）。
3. 查 embedding 网关连通性与 `EMBEDDING_API_KEY`；再看 `rag_stage_duration_seconds`
   是 `embed` 段还是 `vector` 段。
4. **语料目录没挂进容器**也会走到这里：看启动日志有没有「RAG 语料为空」。
5. ⚠️ 这条**只在"整体检索失败"时涨**。空召回（检索成功但没命中）走的是
   `rag_hits_total{result="empty"}`，是另一个问题、另一套处置（见 `AgentRetrievalEmptyHigh`）。

#### `RetrievalCircuitOpen`（warning，5m）

**出站熔断器（rerank / embedding）未闭合 ⇒ 调用被快速失败 ⇒ 调用方降级。**
全程没有异常抛出、没有 5xx，**只有这个 gauge 能反映它**。用户侧的感知是
"排序变差""答案依据不足"，而面板上的错误率完全正常。

```bash
# gauge：0=闭合，1=打开**或半开**（半开也算未闭合）
curl -s localhost:8000/metrics | grep '^circuit_'
```

1. 看 `circuit_opened_total` 的增长速率与 `circuit_rejected_total`
   （后者 = **省下了多少次白等**，可用于估算熔断帮你挡掉的延迟）。
2. 查上游网关健康与配额：`rerank` ⇒ `RERANK_MODEL` / `RERANK_API_KEY`；
   `embedding` ⇒ `EMBEDDING_MODEL` / `EMBEDDING_API_KEY`。
3. ⚠️ **半开状态也会让本告警保持 firing** —— 那表示上游**还没真正恢复**
   （每次试探又失败一次）。**不要为了让告警消失而调大 `CIRCUIT_FAIL_THRESHOLD`**，
   那只是让"熔断"这件事也一起变得不可见。
4. 恢复后 gauge 会自行变 0；确认 `rag_stage_duration_seconds{stage="rerank"}`
   的量级回到基线，再收工。
5. **`breaker` 是标签名，不是 `name`**：`app/metrics.py` 的读写函数第一个形参就叫
   `name`，标签再叫 `name` 会让上报当场抛 `TypeError`（本项目实测踩过：
   三个熔断指标从未落地，而且异常穿透 `guarded_call` 让检索整体降级）。
   写 PromQL 过滤时用 `circuit_open{breaker="rerank"}`。

#### `FirstTokenTooSlow`（warning，10m）

**流式请求的首字节 P95 超过 8 秒。** 这是流式服务里第一个该看的用户体验指标：
端到端延迟（`http_request_duration_seconds`）覆盖整段生成、天然被"回答有多长"主导，
它快慢**说明不了**"用户等了多久才看见第一个字" —— 而这正是
`client_disconnected_total` 高企（用户没耐心）最常见的成因。

1. 先分诊：`rag_stage_duration_seconds`（检索慢？）与 `llm_call_duration_seconds`（模型慢？）。
2. 两者都正常 ⇒ 问题在**排队/并发**：看
   `http_concurrency_in_flight / http_concurrency_limit` 与 `rate_limited_total`。
3. 若 `client_disconnected_total` 同时在涨 ⇒ **用户已经在关页面了**，优先处理。
4. 对照 `llm_tokens_total` 排除"上游整体过载"。
5. ⚠️ 本告警**只看 `mode="stream"`**。非流式链路（agent 链路整段返回）的"首字节"其实是
   **整段生成耗时**，混进同一个分位数会让这个数失去含义 —— 那部分看
   `mode="buffered"` 的分布，不进本告警。

**它不覆盖的第二种慢：「第一个字很快，然后卡住」。** 用户描述通常是"答到一半不动了"，
而这条告警**不会响**（首 token 并不慢），端到端耗时又被回答长度主导、看不出来。
这时看 `llm_inter_chunk_seconds{kind="chat"}` 的 P95 / max：

| 形状 | 含义 | 下一步 |
| --- | --- | --- |
| 偶发一个数秒的尖峰，且**同一时刻** `event_loop_lag_seconds` 也有尖峰 | **我们自己**把事件循环堵住了（同步调用），不是上游 | 按 `EventLoopBlocked` 处置 |
| 尖峰只出现在这一条、`event_loop_lag_seconds` 干净 | 上游解码**中途**卡住（队列抖动 / 限流） | 看上游状态；对照 `llm_call_duration_seconds` 的同一时间窗 |
| 整体抬升（P95 一起涨） | 上游解码整体变慢，或模型被换成了更慢的一档 | 对照发布记录与模型配置 |
| 一直很正常 | 用户说的"卡"不在服务端（前端渲染、网络、网关缓冲） | 查 `client_disconnected_total` 与网关访问日志 |

⚠️ 两件事不要用它做：① **不要**用它推算"每秒吐几个 token"——上游可能一次下发多个
token（chunk ≠ token）；② **不要**把跨模型调用的等待算进去——中间那段是工具执行，
指标本身已经按"同一次模型调用"断开（判据见 `app/main.py` 的 `last_call`）。

#### `LlmCallErrorsHigh`（warning，10m）

**模型调用自身的失败率超过 10%。** 没有它时，上游抖动只能透过
`chat_stream_errors_total` 与 `agent_runs_total{exit_reason="error"}` 间接看到 ——
而这两者都会被重试与"兜底话术"吃掉一部分，**系统性低估真实失败率**。

1. **先按 `stage` 定位环节**：`evaluate` 高 ⇒ 评审环节（有重试兜底，主要推高返工与成本）；
   `answer` / `revise` 高 ⇒ **用户直接拿不到答案**（最急）；`step_get` / `step_stream`
   ⇒ agent 链路。
2. 看 `llm_call_duration_seconds` 的分布：失败集中在**超时附近**（≈ `REQUEST_TIMEOUT_SECONDS`）
   说明上游变慢；否则更可能是 4xx / 协议错（查 `AGNES_API_BASE` 与模型名是否被换掉）。
3. 同时看 `llm_budget_exceeded_total`，排除"预算被顶掉被误记成失败"。
4. agent 链路的话，对照 `agent_runs_total{exit_reason}` 的分档：
   `error` 会把限流(429)/超时/协议错/代码 bug 压成同一桶，**要看日志里的 `error` 字段**
   （`TaskOutcome.error` 形如 `"类型: 消息"`）才能分开处置 —— 限流该退避、bug 该改代码。

#### `RunStatePersistFailed`（warning，5m）

**运行现场没能落库。** 触发指标是 `review_gate_failed_total`
（`app.main._hold_for_review` 写 `agent_run_state` 失败时计数）。

它意味着什么、不意味着什么：

- **不**影响已经发出的正文 —— 用户那轮回答是完整的；
- **意味着**这一轮的"待人审"现场没了：`/v1/review` 会因为找不到现场而回 409，
  审核台上点「同意」没反应。`RunStore.save` 此时会**降级成内存**并打 warning
  （"这次能不能接着跑"比"存在哪个介质上"重要），所以进程不重启就还能救回来 ——
  重启就真丢了。

```bash
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=200 app-stable | grep -E '运行现场|agent_run_state'
```

1. 看是**连接类**失败（`DB_URL` / PG 容器 / 连接池耗尽 / `PoolNotConfigured`）
   还是**表结构**问题（`agent_run_state` 不存在 ⇒ 跑 `python -m app.migrations apply`）。
2. 检查 PG 磁盘空间（见 `DiskSpaceLow`）与连接数上限。
3. 恢复后**必须核对**这段时间的待审会话：日志里 grep `会话 <thread_id>` 的 warning，
   把它们捞出来通知审核人**重新触发一次**（客户端重发原消息即可，不会重复计费 ——
   幂等键会重放上次结果）。
4. ⚠️ 这一项**没有**耗时直方图：它已经套在请求总时限里（超时会先被 `timeout`
   事件收掉），所以"慢"不会再表现为这里的失败。看不到序列时先确认有没有待审流量
   （`review_needed_total`）。

#### `OnlineHallucinationDetected`（warning，15m）

**采样到的线上回答里出现了"可被代码证明的编造"**（引用了语料里不存在的标识符，
或标了不存在的引用编号）。判据是**纯规则的、零模型调用**，所以这不是主观判断。
它与 `CitationUnsupportedFrequent` 的分工：后者只看引用编号，这条还会看
`KEY=VALUE`、路径、常量这类标识符。

1. ⚠️ **先确认采样开着**：`RAG_ONLINE_QUALITY_SAMPLE` 默认 **0**。没开时这两个序列
   **不存在**（不是 0）⇒ 本规则 inactive。**先看 `online_answer_judged_total` 有没有序列**，
   没有就是"没在检查"，不是"没问题"。
2. 看 `online_answer_judged_total{verdict}` 的构成：`unknown` 占比过高说明多数回答
   没有可核的标识符，此时这个**比率本身参考价值有限**（先确认有效样本量再说）。
   ⚠️ `unknown` 是**刻意**单列的 —— 把"无从判断"并进 `pass` 会让线上 ok_rate 朝好看的方向偏。
3. 与最近一次变更对照：`PROMPT_VERSION` / `EMBEDDING_MODEL` / 上游模型名
   （换模型或改提示词后这类错误最常见）。
4. 用离线入口复现：`python scripts/eval_generation.py --live`。
5. 集中在某类问题时，考虑收紧 `answer_prompt`，或先打开
   `RAG_CITATION_CHECK_ACTION=block` 止损（会吞掉不可信引用，**答案照常返回**）。

#### `RunControlErrors`（warning，5m）

**停止任务这条链路自己的操作失败了**（`run_control_errors_total` 增长，`op` 标出是哪一步）。
它为什么值得单独一条：停止是**成本与安全的止损阀** —— 别继续烧 token、别继续改用户的文件。
这个阀静默失效时，没有别的信号能发现它：接口照样回 200、agent 照样跑完、
错误率与延迟全都正常，只有这条指标看得见。

`op` 的四个取值分别意味着什么（**先去按 `op` 分档，别一上来就看日志**）：

| `op` | 失败后果 | 用户侧表现 |
| --- | --- | --- |
| `begin` | 这次运行**没有登记**，之后任何 `/stop` 都找不到它 | 点停止"没反应"，任务跑到自然结束 |
| `request_stop` | 停止请求**没写进去** | 同上；多副本时尤其明显（请求落在 A，运行在 B，靠库传递，写不进就传不过去） |
| `stop_requested` | 查不到停止状态，服务端**按"没停"继续跑** | 停止延迟生效或完全不生效 |
| `finish` | 登记行**残留**成"幽灵运行" | 下一次 `/stop` 可能打到一条已经结束的运行上 |

1. 分档：`sum by (op) (increase(run_control_errors_total[15m]))`。
2. 日志里搜「运行登记」「停止请求」，先分清是哪一类：
   PG 不可达 / 连接池耗尽 / `agent_runs` 表不存在。
   表不存在基本等于**忘了跑迁移**：`python -m app.migrations apply`（本表是版本 **9**）。
3. 查残行：`SELECT count(*), max(started_at) FROM agent_runs;`
   行数远大于实际并发数 ⇒ `finish` 一直在失败。这些行要先手工清掉，
   否则它们会让后续 `/stop` 打到空处（表里"最新一次运行"指向早已结束的那条）。
4. 单副本部署可以**临时**接受降级（停止只对本进程生效，进程内旗标仍然管用），
   但仍要修：多副本是常态，而跨副本的停止完全依赖这张表。
5. 一条对照曲线：`run_stop_requested_total`（点了多少次）与 `run_stopped_total`
   （真的收流了多少次）。长期对不上 ⇒ 停止**没有真正生效**，而不只是"没人点"。
6. ⚠️ 数据库操作失败**不会**让请求报错（登记不上不该让 `/v1/chat` 直接失败），
   所以"接口一直是好的"完全不能作为"停止是好的"的证据。

#### `SessionMessageErrors`（warning，5m）

**"任务执行中补充"的那句话没能写进会话历史。** 这条与上一条是两件事，
不要合并：上一条是"停不下来"，这条是"**说了没听见**"，处置入口完全不同。

为什么写历史这一份是关键：进程内的暂存只在本副本、本进程存活期间有效；
**写进会话历史的那一份才是唯一持久的**。写失败意味着下一个节点、下一轮、
以及重启/换副本之后，都看不到用户补的这句话。

而且它丢得很安静：接口会回 503（客户端能看见 `type(e).__name__`），
但如果客户端把这个端点当成"可忽略的增强能力"而不提示用户，用户就会以为 agent 已经收到了。

1. 日志里搜「补充写入会话历史失败」，抛出的异常类型会直接写在 503 响应里。
2. 常见原因按顺序排查：`DB_URL` 没配（单机开发环境的正常状态，生产不该出现）→
   `agent_messages` 表没建（迁移版本 **7**）→ PG 连接池耗尽。
3. 看 `session_message_total` 的增速来区分性质：
   **只有高峰失败** ⇒ 容量问题（连接池 / PG 压力，对照 `PgTableGrowthFast` 与连接数上限）；
   **一直失败** ⇒ 配置问题（DSN、表、权限）。
4. `session_message_total` 同时为 0 ⇒ 这个端点**根本没被调用过**，
   问题在上游客户端而不是存储 —— 先确认客户端有没有真的发出请求，再查存储。
5. 恢复后要做一件人工的事：**告诉受影响用户那几句没进历史，需要重发**。
   服务端没有"补写队列"，不会自己补。

### 3.2 SLO 与错误预算（category: slo）

先读 `deploy/monitoring/SLO.md` 的处置表，这里只给补充：

| 告警 | 动作 |
|---|---|
| `SLOAvailabilityBurnFast`（critical） | 按 P1 处理。**25% 的 30 天预算在 1 小时内烧掉** —— 这是最紧急的一类 |
| `SLOAvailabilityBurnMedium`（critical） | 通常是长期错误源。看 6 小时错误按 `path` 是否集中在单一路由 |
| `SLOAvailabilityBurnSlow`（warning） | 排期。若确认是常态，**正式**下调 SLO（改 `SLO.md` + 表达式两处），别让它长期挂着 |
| `SLOLatencyBurnFast`（warning） | 按 `path` 定位：`/v1/reviews` 慢 ⇒ 审核队列表与 PG；`/health/*` 慢 ⇒ 探针在等外部依赖。⚠️ 这条**不含**流式接口（它们单独有一条） |
| `SLOLatencyBurnSlow`（warning） | 排期优化（典型是 PG 查询缺索引） |
| `SLOStreamLatencyBurnFast`（warning） | **首 token** 慢（不是整段生成慢）。按顺序分诊首 token 之前的每一段：`rag_stage_duration_seconds`（检索）→ `agent_history_compaction_seconds{result="error"}`（压缩白等一次超时）→ `llm_call_duration_seconds`（模型调用本身）→ 排队（`http_concurrency_in_flight / http_concurrency_limit`）。通常紧跟 `FirstTokenTooSlow`（P95 > 8s），是同一根因的两个粒度 |
| `SLOStreamLatencyBurnSlow`（warning） | 排期。先看是不是集中在长历史会话（压缩与检索都随历史增长）；确认是上游模型整体变慢的话，要么换模型/加缓存，要么按 `SLO.md` §7 正式调整阈值 |
| `SLOBudgetLow`（warning） | 该 SLI 的**功能变更冻结**（按 `ERROR_BUDGET_POLICY.md`），解冻条件是预算回到 50% |
| `SLOBudgetExhausted`（critical） | **全面冻结**功能变更 + 48 小时内复盘。⚠️ 按 **P2** 处理，不是 P1（见 §1.3 的说明） |

> **错误预算烧到什么程度要冻结发布**：见 `deploy/monitoring/ERROR_BUDGET_POLICY.md`。
> 一句话版：`Fast` 触发即冻结发布并优先回滚；`Slow` 触发则本周期只允许修复类变更；
> **`SLOBudget*` 触发则按预算余量的两档动作执行**（20% ⇒ 冻结该 SLI 的变更，
> 耗尽 ⇒ 全面冻结 + 复盘）。

#### 预算余量的两条（与上面 7 条不是同一族）

上面那几条问的是"**按这个速度还要多久烧光**"（燃尽率），它们对
"**现在还剩多少**"完全无感；`SLOBudgetLow` / `SLOBudgetExhausted` 问的是后者。
余量由 `recording.yml` 的**录音规则**按 30 天窗口算出（SLO 定义见
`deploy/monitoring/SLO.md` §4b）。

```bash
# 每个 SLI 各自还剩多少（availability / latency / stream_latency，与告警读的是同一个数）
curl -s --noproxy '*' 'localhost:9090/api/v1/query?query=slo:budget_remaining_ratio' \
  | python -m json.tool
# 先确认录音规则本身在跑（它哑掉时告警只会"永远不触发"，看起来像预算很健康）
curl -s --noproxy '*' localhost:9090/api/v1/rules \
  | python -m json.tool | grep -A3 'slo:budget_remaining_ratio'
```

- `health` 不是 `ok` 或 `lastEvaluation` 不动 ⇒ 问题在录音规则侧（见 `SLO.md` §8 第 6 条），
  而不是"预算健康"；
- 数值是**近似值**（平均的是每 5 分钟桶的速率），用来判断"过没过 20% / 耗尽"足够，
  不要拿它算精确额度；
- 这两条走 `kind = "budget"` 的**专属路由**，`repeat_interval` 是 **24h**：
  余量是月级慢变量，按小时重复只会让它在被处置前就被静音。**别在确认前静音它**。

### 3.3 成本与配额（category: cost）

#### `RateLimitSurging`（warning，10m）
看 `scope`：`ip` ⇒ 单来源刷量，保持限流并考虑在 nginx 层收紧；
`session` ⇒ 某客户端多开或循环重试。**是误伤就调阈值**，不要长期容忍正常用户被拒。

#### `RateLimitSharedEgressWithoutTrustedProxy`（warning，0m）
限流的 IP 维度发现**同一个对端地址背后有多个不同用户**，且当前没有信任转发头。
含义：这批人的按 IP 额度是**共用一份**的 —— 一个人刷满，同出口的其他人（含
`POST /v1/auth/login`）全部 429。最常见的成因是应用部署在反向代理后面却漏配
`TRUST_PROXY_HEADERS`（配错的表现只有"额度算错了"，不报错、不崩溃）。

第一句话：**先确认部署形态，别急着调额度。**
```bash
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=200 app-stable | grep '共享出口'
```
- 前面有反代/网关 ⇒ 设 `TRUST_PROXY_HEADERS=1` 后重启，应用改从 `X-Forwarded-For`
  取真实客户端地址；**确认代理是覆盖式写 XFF**（`proxy_set_header X-Forwarded-For
  $remote_addr;`），追加式写法下最左段是客户端可控的 —— 那等于限流可被伪造绕过
  （守卫见 `tests/test_deploy_topology.py`）；
- 确认是 NAT / 公司出口（已开 `TRUST_PROXY_HEADERS` 仍报）⇒ 那条走 warning 日志而不是
  本告警，处置是提 `RATE_LIMIT_PER_MINUTE` 或按出口人数评估；
- 验证：告警消失且 `rate_limited_total{scope="ip"}` 回落。认证端点另有一道按真实 IP 的
  nginx 硬帽子（`deploy/app/nginx.conf` 的 `chatagent_auth` zone）兜底，它保证**单个**
  客户端占不满应用侧那个桶 —— 但它不解决"全站共用一个桶"这件事。

#### `QuotaExhausted`（warning，1h）
```bash
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=200 app-stable | grep '配额拒绝'
```
- 少数几个租户集中 ⇒ 业务增长，该加额度；
- 大量不同租户零散出现 ⇒ **用户令牌被共享/盗用**（查
  `/v1/auth/logout?all=true` 清场、核对 `auth_login_total` 的来源）。
  ⚠️ 曾经的第二个怀疑对象"脚本通行证泄漏（它不带租户、可自报任意租户）"已不存在：
  共享密钥路径整体删除后，就只剩"令牌被共享/盗用"这一种解释，而令牌**是可撤销的**
  —— 处置动作因此从"轮换一把全局密钥"变成"停用那个账号并清掉它的全部会话"。

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
# 按服务账号登录换令牌再查（共享密钥 X-API-Key 已**删除**，不是"废弃"，见 deploy/SECRETS.md §4.2.1）
TOKEN=$(curl -sS -X POST localhost:8080/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$AGENT_SCRIPT_USER\",\"password\":\"$AGENT_SCRIPT_PASSWORD\"}" \
  | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -sS -H "Authorization: Bearer $TOKEN" localhost:8080/v1/reviews | python -m json.tool
```
**先查通知通道**（`REVIEW_NOTIFY_WEBHOOK`）——积压最常见的成因是通知没送到人手里，
而不是没人愿意审。

#### `ReviewWaitTooLong`（warning，15m）

**被处理掉的那些待审记录里，P90 的等待时长超过 8 小时。**
⚠️ 它与 `ReviewBacklog` 是**两个不同的问题，不可互替**：
"积压 20 条各等 10 分钟"与"积压 2 条各等 3 天"，在 `review_pending` 上**完全一样**。
而 human-in-the-loop 服务里，用户实际承受的延迟主体就是这段等待
（模型生成通常只占其中几秒）。

1. 对照 `review_pending` 判断是**「处理得慢」**还是**「根本没人处理」** ——
   后者最常见，先查 `REVIEW_NOTIFY_WEBHOOK` 是否失效。
2. 看 `action=revised` 与 `accepted` 的分布差异：被打回的那些若明显更久，
   说明来回沟通成本高，应在 `need_review` 载荷里**一次给全判据**。
3. 业务上确实不需要人工把关的话，**调低触发评审的阈值**，而不是让人一直等。
4. ⚠️ 本指标的观测点只在 `review_queue.resolve()`（唯一知道 `created_at` 的地方），
   所以**只有被处理过的记录才有样本** —— 长期没人处理的那些恰好看不到，
   由 `ReviewBacklog` 兜住。两条必须都在。
   同理它的判定窗口写成 **6 小时**：小时级窗口会被"上批刚处理完"的样本填满，
   P90 一直偏低 ⇒ 长期无人处理的积压永远不触发。

#### `FrequentClientDisconnects`（warning，10m）
服务侧已及时止损（不再计费），但用户没拿到回答。通常是首字节太慢或前端超时太短。
配合 `HighLatencyP95` 一起看。

### 3.4 安全（category: security）—— 任何一次都是 P0

#### `TenantMismatchDetected`（critical，单条即报）
```bash
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=500 app-stable | grep '租户自报与用户凭证不符'
```
- **含义已变（2026-10-05）**：这个指标**只有一个来源** —— 请求头
  `X-Tenant-Id` 与 JWT 载荷里的租户不一致时 403 并计数。
  ⚠️ 曾经的"脚本通行证那条路不受它约束（它的租户本来就来自请求头自报）"已经不存在：
  共享密钥路径整体删除后，**没有任何路径**能从请求头自报租户，所以这条告警
  不再有"误报来源"，出现即可疑。
- 来自内部服务 ⇒ 客户端把另一个租户的 id 写进了请求头（改配置/改调用方即可）；
- 来自外部 ⇒ 有人在**用自己租户的令牌试探别的租户**。按越权处置：定位 IP 与
  `sub`（日志里有 `token_tenant=... claimed=...`），必要时
  `POST /v1/admin/users/{username}` 把该账号 `status=disabled`（会同时清掉它的全部会话）。
  ⚠️ 这里**没有"轮换租户密钥"这个动作了** —— 租户与密钥的绑定表（TENANT_KEYS）已删除。

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
| `alertmanager` | **告警从此发不出去**（Watchdog 也会停） | `docker compose --env-file .env -f deploy/app/docker-compose.yml --env-file .env -f deploy/app/docker-compose.yml logs --tail=200 alertmanager`；先恢复，再补发消息 |
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
| 认证失败 | 密码文件没挂进容器，或 `smtp_from` 与 `smtp_auth_username` 不一致（多数邮件服务要求两者相同） | `docker compose --env-file .env -f deploy/app/docker-compose.yml --env-file .env -f deploy/app/docker-compose.yml exec alertmanager cat /etc/alertmanager/smtp_password`（**只在排障时**，别把输出粘进工单） |
| 对方拒收 | 收件人被机器人/网关列入拒收名单、或进了垃圾箱 | 看 `failed notifications` 里 SMTP 的原始错误码 |

> 邮件**默认是关闭的**（启用步骤与四个坑写在 `alertmanager.yml` 末尾）。如果你从没启用过
> 却看到 `integration=email` 在失败，说明有人放开了一半：检查 `片段①/②/③` 是否只取消注释了
> 一部分，以及 compose 里那行 `smtp_password` 的 volume 是否也放开了。

#### `BackupLoopFailing`（warning，15h 未成功）
```bash
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=100 backup-pg backup-qdrant
ls -lt backups/ | head
python scripts/pg_backup.py backup                # ① 手工存一份 dump（**不刷新指标**，见下）
docker compose --env-file .env -f deploy/app/docker-compose.yml restart backup-pg   # ② 让指标推进
```

⚠️ **① 和 ② 解决的是两件不同的事，别只做①就以为修好了。**
`backup_last_success_timestamp_seconds` 是**备份循环**（compose 里的 `backup-pg` /
`backup-qdrant` 服务）每轮写进 `backups/backup-pg.prom` 的；`scripts/pg_backup.py backup`
只产出一个 dump 文件，**完全不碰**那个 `.prom`。所以只跑 ① 的话：磁盘上多了一份备份，
但指标仍是旧时间戳、`BackupLoopFailing` 照旧 firing —— 运维会误以为"备份已修好"，
而真正的原因（循环没在成功）还挂着。要让指标推进，必须让**循环自己成功跑完一轮**。
`restart backup-pg` 之所以有效，是因为循环的结构是"**启动即跑一轮**，然后才对齐到
槽位睡眠"（见 compose 里 `backup-pg` 的 `command`），不是先睡再跑。

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

#### `PgTableGrowthFast`（warning，15m）—— 磁盘告警的提前量
某张表 6 小时增长超过 1.5GB（`delta(pg_table_size_bytes[6h]) > 1.5e9`）。
数据来自 `pg-stats` 容器每 15 分钟写的 `backups/pg-tables.prom`。

> ⚠️ 这是**永久保留**（`RETENTION_DAYS=0`）配套的可见性。历史数据只增不减是
> **刻意的设计**，所以"在涨"本身不是故障 —— 要判断的是**跑得最快的那张**
> 是不是在异常膨胀。`DiskSpaceLow` 只在低于 15% 时响，那时余量往往只剩几小时；
> 这条的价值是**提前几天**看见，并且直接指出是哪张表。

```bash
# ① 采集链路还活着吗（为空 ⇒ 先修 pg-stats，否则这条告警本身就是哑的）
curl -s localhost:9100/metrics | grep pg_table_size_bytes
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=100 pg-stats
# ② 谁在吃空间
docker compose --env-file .env -f deploy/app/docker-compose.yml exec pgvector psql -U langgraph_app -d langgraph_checkpoints \
  -c "SELECT relname, pg_size_pretty(pg_total_relation_size(oid)) FROM pg_class WHERE relkind='r' ORDER BY pg_total_relation_size(oid) DESC LIMIT 10"
```
- `agent_messages` 涨得快 ⇒ 对照 `http_requests_total` 看是不是被刷（真被刷时
  `agent_history_dropped_items` 也会同步放大）；
- `agent_run_state` 涨得快 ⇒ 待审会话积压（对照 `review_needed_total` 与审核队列长度）；
  它按会话**覆盖**写，正常不会无限涨 ⇒ 持续线性增长说明有人在反复触发待审。
- 确认是预期增长就**调大阈值**（按盘容量与业务量定），**不要**关掉这条规则 ——
  它是磁盘告警唯一的提前量。

### 3.6 agent 能力链路（category: agent）—— "请求有没有被正确地跑完 agent 链路"

前面几组管的是"服务活没活 / 花了多少 token"，这一组管的是
"**这个能力有没有跑起来、跑得好不好**"。

> ⚠️ **拓扑（2026-10-20 起）**：链路是两个 **agent** —— **前台**（自己作答，或把
> 创作交出去）与**文学**。交接走官方 `handoff`，它发生在**同一次运行的内部**，
> 不是一个新节点、也不新起一次运行（`tests/test_agents_pipeline.py` 钉着这一点）。
> 检索 / PPT / 插图仍然只是前台手里的**工具**。
>
> 所以现在**只有一种口径**：一次用户请求 = 一次运行
> （`agent_runs_total` / `agent_model_calls` / `agent_tool_calls`，带 `agent` 标签）。
> 想知道"这一轮换过人没有"看 `agent_participants_total{agent}` 与
> `agent_handoff_total{src,dst}` —— 它们才是"多了一个人"的证据；运行数不会变多。
>
> ⚠️ 哪天一次请求又变成多次运行（比如把某个帮手挪成独立的运行），分母必须重新
> 拆成"请求口径 / 运行口径"两套 —— 混用会让比率系统性偏差，而且没有任何告警能发现。

> ⚠️ 先分清两种"看起来正常"：
> - **指标序列不存在** ⇒ 从没走过 agent 链路（没有流量，最常见）；
> - **进程压根没起来** ⇒ 没装 SDK / 没配 Key，`app.main._assert_agents_ready` 会
>   让启动**直接失败**（`missing_sdk` / `missing_api_key` / `build_failed`）。
>   ⚠️ 这等于是 `up == 0` 与容器重启计数的事，**不要**再去指标里找"降级计数"——
>   那条降级路径已经在 2026-10-20 随图删除，找不到是正常的。
>
> 前者不是故障，后者也不是"能力失灵"而是"服务没起来"。分不清就会把"没有这个能力"
> 读成"能力正常"。

**这一组里没有 `AgentLinkDegraded` 这条了（2026-10-20 删）** —— 不要找它。

它盯的是"想走 agent 但走不了 ⇒ 静默退回问答链路"，而**降级路径已经不存在**：
agent 链路是唯一链路，SDK 缺失 / 模型没配时进程**直接起不来**（
`app.main._assert_agents_ready`，报 `missing_sdk` / `missing_api_key` / `build_failed`）。

那么"起不来"靠什么发现？靠 **`up == 0`** 与容器重启计数 —— 这是部署层的事实，
不是应用指标。当初那条告警唯一的触发序列 `agent_unavailable_total` 也已一起删除，
所以你在 `/metrics` 里 grep 不到它，**那不是漏埋点**。

> 判断这段语境是否还成立，只看一句：**还有没有"不走 agent 也能答"的第二条链路。**
> 只要没有，就不该再有"降级面"的指标与告警。

#### `AgentHistoryDegraded`（warning，5m）—— 链路活着，但模型"失忆"了
15 分钟内有会话后端故障（`increase(agent_session_errors_total[15m]) > 0`）。
此时链路**照常跑、答案照常返回 200**，但送进模型的**只有用户刚说的这一句**，
上文全丢 —— 也就是"带完整上下文"这个能力悄悄退回了单轮。

> **和"服务起不来"的区别**：那种是整条链路没起来（`up == 0`，客户端连 200 都拿不到），
> 这条是"走得动，但没有记忆"—— 服务零报错、探针全绿。用户侧最典型的症状：
> 用户说「**接着上一轮那个方案，把篇幅压一半**」，模型回一句「不知道您指的是什么」，
> 或者干脆当作一个全新话题从头写 —— 因为它真的没看到上文。

```bash
# ① 看是哪个 kind 在涨（ModuleNotFoundError / OperationalError / TimeoutError 等）
curl -s --noproxy '*' localhost:8000/metrics | grep agent_session_errors_total
# ② 启动自检的结论行（就绪 / 不可用，各一条，直接 grep 得到）
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=300 app-stable | grep -E '跨轮对话历史(就绪|不可用)'
# ③ 首条故障的完整原因（后续同类故障只计数、不再重复打日志）
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=300 app-stable | grep '跨轮对话历史不可用'
```
- `kind=ModuleNotFoundError` ⇒ 没装 `sqlalchemy`：装包（`requirements.txt` 里已声明，
  但 `pip install -r` 没跑过就会漏）。
- `kind=NoSuchModuleError` / `ArgumentError` ⇒ `DB_URL` 的写法不对：本项目只装了 psycopg 3
  一套驱动，异步前缀必须是 `postgresql+psycopg://`（`src.agents.session.to_async_dsn`
  会自动补；如果你手写了 `+asyncpg`，报的就是"驱动没装"）。
- `kind=OperationalError` / `TimeoutError` ⇒ **PG 不可达 / 探不通**：核 `DB_URL` 指向的
  主机端口、PG 容器状态与网络。⚠️ 特别注意：`create_async_engine` 是**懒连接** ——
  地址写错在构造期完全不报错，要到"第一次取历史"才炸，而那一步在 `Runner.run` 内部、
  会被 `run_agent` 的宽 `except` 接住 ⇒ 节点抛 `AgentRunFailed`，接口返回 `incomplete`
  （⚠️ 这里**没有**可续跑的现场：进程没崩、是一轮运行失败了，客户端重发消息即可）。
  **别从"agent 链路答不出来"开始排查，先看这条指标**（启动自检那条 ERROR 也直接给结论）。

> ⚠️ **"表不存在"（`relation "agent_sessions" does not exist`）不在这条指标里**：
> 它发生在 `Runner.run` 内部的第一次 `get_items`，同样被 `run_agent` 的宽 `except` 接住
> ⇒ 同样抛 `AgentRunFailed`（由 `AgentRunFailureRateHigh` 发现），但**降级行为完全一样**：
> 本轮没有历史。先跑 `python -m app.migrations status` 确认版本到了 `7`
> （`agent_sessions` / `agent_messages` 两张表由 v7 迁移建）。

> ⚠️ **`kind` 标签是故障类型的闭集**（异常类名），**绝不含 `thread_id` 或用户输入** ——
> 这是刻意的：否则基数会随会话数爆炸。

恢复判据：`agent_session_errors_total` **停止增长**，且启动自检重新打出 `跨轮对话历史就绪：后端=...`。

> **降级行为是设计好的，不是 bug**：后端挂了就当"本轮没有历史"，**不退化成 5xx**。
> 所以它必须靠这条告警被发现，而不是靠错误率。

#### `AgentHistoryBudgetExhausted`（warning，5m）—— 历史取到了，但塞不进去
裁剪已经保到"最后一条必须留"的底线，输入总量**仍然**超预算
（`increase(agent_history_over_budget_total[15m]) > 0`）。请求照常返回 200，
但这一轮的模型**没看到上文**。

> **和 `AgentHistoryDegraded` 的区别**：那条是"历史**取不到**"，这条是"**取到了但装不下**"。
> 用户侧症状一样（「接着上一轮改」被当成新话题），而排查方向完全不同 ——
> 这条指向**配置**，不是故障。

> ⚠️ 最常见的一种成因：`AGENT_HISTORY_TOKEN_LIMIT` 配得**比模型的真实窗口还大**
> —— 那样裁剪永远不触发，每轮都超窗，而服务侧看不出任何异常。

```bash
# ① 实际生效的预算（启动自检那条结论行）
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=300 app-stable | grep '跨轮对话历史就绪'
# ② 当轮到底多大（那条 warning 里带实际 token 数与预算）
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=300 app-stable | grep '跨轮历史预算不足'
```
- 先核对"预算是多少 / 模型窗口多大"：这是**输入**预算，工具清单与输出长度
  **不在**它的视野里，必须留余量 —— 配成窗口大小就等于每轮必超；
- 预算确实偏小 ⇒ 调大 `AGENT_HISTORY_TOKEN_LIMIT`；
- 单条输入过长 ⇒ 看该次请求的输入与入口体积闸门（`http_request_body_too_large_total`）。

恢复判据：该序列**停止增长**。

#### `AgentHistoryBudgetErrors`（warning，5m）—— 预算压根没算出来
（`increase(agent_history_budget_errors_total[15m]) > 0`）

算不出来时**整段不裁剪**（刻意不因为算预算失败就把整轮请求打挂），于是输入可能超窗
被模型侧拒 —— 表现是"**这次答不出来**"。日志**只打首条**，其余只计数（防日志放大）。

```bash
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=300 app-stable | grep '跨轮历史的 token 预算算不出来'
curl -s --noproxy '*' localhost:8000/metrics | grep agent_history_budget_errors_total
```
- `kind=ModuleNotFoundError` ⇒ 没装 `tiktoken`（`requirements.txt` 里已声明）；
- 拉不到词表（离线环境）⇒ 自动退化成**字符数近似**并打一条 warning，功能不中断；
- `kind=TypeError` / `AttributeError` ⇒ SDK 升版后形状变了，看 `src/agents/history_budget.py`。

恢复判据：该序列**停止增长**。

#### `AgentToolStepTimeoutFrequent`（warning，5m）—— 这一步没等到结果
（`increase(agent_tool_step_timeout_total[15m]) > 3`）

工具在一次调用内超过 `AGENT_TOOL_TIMEOUT_SECONDS`（默认 20s）仍未返回，于是**放弃
等待**、把这一圈交还给模型。⚠️ 它**不一定**表现为失败：回给模型的话是"换做法 / 缩小
范围 / 先做完其余部分"，模型往往能绕过去 —— 于是请求 200、错误率正常、可用性正常，
真实代价是**答案少了一段依据 + 这一轮变慢**。这类"不报错的坏"只能靠这条告警发现。

```bash
# 哪个工具在超时
curl -s --noproxy '*' localhost:8000/metrics | grep agent_tool_step_timeout_total
# 实际等待时长与工具名（warning 里都有）
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=300 app-stable | grep '工具单步超时'
# 同工具的正常耗时分布：p95 是否本来就贴着超时线
curl -s --noproxy '*' localhost:8000/metrics | grep agent_tool_result_total
```
- `tool="kb_search"` ⇒ 查检索侧：向量库/rerank 地址、索引大小、**是否冷启动正在加载**；
- `tool` 是落盘类 ⇒ 查磁盘与 `AGENT_ASSET_DIR` 权限（写不进去会一直挂着等 IO）；
- **先判"工具真慢"还是"预算配小了"**：`agent_tool_result_total` 里该工具的耗时分
  布若 p95 紧贴 `AGENT_TOOL_TIMEOUT_SECONDS`，调大预算；若耗时本身在涨，先查依赖。
  ⚠️ 不要只调大超时把告警压下去 —— 那等于把"工具坏了"变成"用户多等 20 秒"。
- 同时出现 `AgentRunFailureRateHigh`，或答案里反复写"这一步没做成" ⇒ 模型没能绕过，
  先按上面修工具。

> ⚠️ 工具跑在**独立线程池**（`AGENT_TOOL_WORKERS`）。这条涨起来时，限流、落库、结算
> 仍然是健康的 —— 那正是隔离的目的，但也会让人误判成"服务没事"。别看整体错误率。

恢复判据：该序列**停止增长**（注意是"不再出现"，不是"降到 0"——它是一个增量指标）。

#### `AgentRevertFrequent`（warning，5m）—— 机制开始替模型改上下文了
（`increase(agent_recovery_level_total{level="2"}[30m]) > 2`）

第 2 级：模型在同一类问题上被提醒过一次之后仍然重复，系统**把出问题那一步及其之后的
动作从模型输入里撤掉**，让它回到那一步之前重新决定。

> ⚠️ 这**不是**失败。被撤回的运行大多数仍能正常交付 —— 所以
> `agent_task_outcome_total{outcome="delivered"}` 上完全看不到它。这条告警是"回滚频率"
> 的唯一线上口径，也是判断"这套防护值不值得开"的依据。
>
> ⚠️ **前提**：`AGENT_MULTISTEP_GUARD` 默认**关**。关着时这条序列**不存在**，
> 告警永不触发 —— 那不是"没发生"，是"闸门没开"。别把静默读成正常。

```bash
# 按问题类型拆开看（loop / stuck / off_track 三条的改法完全不同）
curl -s --noproxy '*' localhost:8000/metrics | grep 'agent_recovery_level_total'
# 同一动作被做了几次（loop 的主证据）
curl -s --noproxy '*' localhost:8000/metrics | grep agent_action_repeats_total
# 两个动作来回（repeat 判据看不见的那一半）
curl -s --noproxy '*' localhost:8000/metrics | grep agent_action_oscillation_total
```
- `problem="loop"` ⇒ 看是哪个工具被反复调，再看它的返回**有没有让模型看出"没结果"**；
  同时看 `agent_action_oscillation_total`：那个在涨而 `agent_action_repeats_total` 不涨，
  就是"两个动作来回试"的形态。
- `problem="stuck"` ⇒ 查 `agent_tool_result_total{status="failed"}` 的同工具 `reason`：
  `bad_args` 改工具描述、`error` 查实现/依赖、`timeout` 查预算与数据量。
- `problem="off_track"` ⇒ 内容层问题，查该 agent 的 instructions 里任务边界。

恢复判据：该序列停止增长。若同时出现 `AgentGuardStopFrequent`，说明撤回也没纠正 ⇒
先按那一条处置（那边是"机制已经放弃"）。

#### `AgentGuardStopFrequent`（warning，1m）—— 刹车了，这次运行被中断
（`increase(agent_recovery_level_total{level="3"}[30m]) > 0`）

第 3 级：提醒与撤回都没能让模型跳出问题，机制主动中断本次运行
（`exit_reason="guard_stop"` ⇒ 判定表收成 `failed`）。用户看到的是"这次答不出来"或半段内容。

**判据是"有发生"而不是速率**：这是终止性事件，一次就代表一次运行没跑完。

```bash
# 停因只在日志里（capped / not_found / not_fixed / side_effect）
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=500 app-stable \
  | grep '多步防护第 3 级'
# 用户侧影响面
curl -s --noproxy '*' localhost:8000/metrics | grep 'agent_task_outcome_total'
```
- `capped` ⇒ 撤回额度用完（`AGENT_REFLECT_MAX_LEVEL2`）。先确认是不是**同一个问题**
  反复触发，再决定调额度 —— 直接调大会让它在同一个坑里撤更多步。
- `not_found` ⇒ 那一步已被 token 裁剪或历史压缩吃掉，撤回无处下手。查
  `AGENT_HISTORY_TOKEN_LIMIT` 是否偏小、或压缩是否把步骤结构压没了（摘要里应有
  "第 N 步 动作 → 关键结果"）。
- `not_fixed` ⇒ 撤了同类问题又出现。这是**内容层**问题（提示词/约束），不是配置。
- `side_effect` ⇒ 出问题的动作是落盘类（不可撤）。看那个工具是否该拆成
  "准备 + 提交"两步，让"准备"可撤。
- 先看 `problem` 标签再动手：它回答"退的是什么问题"，停因回答"为什么退不了"，
  两个维度缺一个就会改错地方。

#### `AgentRunTimeoutFrequent`（warning，30m 内 > 0 次）

一次运行撞到了**墙钟上限**（`AGENT_RUN_TIMEOUT_SECONDS`）。它是终止性事件：这一轮
在注入口被停掉 ⇒ **没有答复正文**。非流式下客户端什么都没收到；流式下只有已经推出去
的那半段（不进会话历史）。

为什么单独一条，不和 `AgentGuardStopFrequent` 合并：那个是"机制放弃让模型自己改"
（提示词/策略问题），这个是"跑太久了"（配置太紧，或某一步真的慢）。改的地方完全不同。

```bash
# 停了几次
curl -s --noproxy '*' localhost:8000/metrics | grep 'agent_run_deadline_stop_total'
# 用户侧影响面（这些运行最终落成 failed + run_timeout）
curl -s --noproxy '*' localhost:8000/metrics | grep 'agent_fallback_total'
# 日志里那条带「已跑 X 秒 / 上限 Y 秒」
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --since 40m app-stable | grep '超过上限'
```

按这个顺序看，别一上来就调大上限：

1. **单个工具慢**：`agent_tool_duration_seconds` 的 p95 按 `tool` 拆。某一个贴着
   `AGENT_TOOL_TIMEOUT_SECONDS` ⇒ 是它的问题（检索数据量、落盘 IO）。
2. **全都慢**：上游/网关的问题，看 `llm_first_token_seconds`。
3. **圈数多**：`agent_model_calls_per_run` 接近 `AGENT_MAX_TURNS` ⇒ 总时长是被
   圈数堆出来的，先治打转（`agent_action_repeats_total` /
   `agent_action_oscillation_total`），别用上限掩盖它。
4. **确认是正常长任务**之后才调 `AGENT_RUN_TIMEOUT_SECONDS`。它默认 `0`（不限），
   所以这条告警只在**配了**上限的部署里可能触发 —— 触发本身就说明配的这个值
   与真实任务不匹配。

> ⚠️ 这两条（`token_budget` / `run_timeout`）被停下时，**已落盘的资产还在**
> `AGENT_ASSET_DIR` 里。用户说"刚才生成到一半的那份"时，先去那儿找，不要以为
> 全丢了。

#### `AgentRunFailureRateHigh`（warning，10m）
异常收尾比例 > 20%（`exit_reason` ∈ `error` / `evaluator_error` / `max_turns`）。

> ⚠️ **口径在 2026-10-03 变更过，别照旧笔记理解**：此前异常会被节点兜成一句得体的
> 话术、接口照常 200；现在**异常直接抛出**（节点抛 `AgentRunFailed`），接口返回
> `incomplete` / 失败事件，**客户端会看到一次失败**。这是刻意换的 —— 把"没跑成"
> 伪装成"跑成了"，会让观测面与重试逻辑一起失真（表现成"接口说完成了、其实什么都没产出、
> 连『继续生成』的按钮都不弹"）。
>
> 指标这一侧**不变**：`agent_runs_total{exit_reason="error"}` 在抛之前就已播报。
> 所以这条告警仍然是发现"DB_URL 配错 / 上游限流 / 协议不兼容"的**第一条线索**，
> 而不是靠错误率兜底。

```bash
# 按 exit_reason 看是哪一类在涨（agent 标签能进一步定位是哪一段在空转）
curl -s --noproxy '*' localhost:8000/metrics | grep 'agent_runs_total'
# 真实异常原因在日志里（TaskOutcome.error 会把「类型: 消息」带出来）
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=300 app-stable | grep -E 'agent 链路执行异常|评审链路失败'
```
- `error` 且消息含 `RateLimitError` ⇒ **上游限流，不是链路缺陷**：等额度或退避重试；
- `error` 且为超时 / 协议错 ⇒ 查 `AGNES_API_BASE` 连通性与 `AGENT_TIMEOUT_SECONDS`；
- `evaluator_error` ⇒ 评审的结构化输出不稳定，可提高 `AGENT_EVALUATOR_TRIES`；
- `max_turns` ⇒ 单任务步数耗尽，见下下条的处置。

#### `AgentReworkRateHigh`（warning，15m）
返工率 > 50%（未一次通过的任务占比）。这是**能力**最直接的代理指标，
而且它直接对应成本 —— 每次返工 = 再跑一整轮模型调用。

> ⚠️ 分母是 `agent_runs_total`（**运行**口径）。2026-10-20 起这两者**重合**：
> 一次请求 = 一次运行（换人发生在运行内部）。哪天一次请求又变成多次运行，
> 分母必须跟着换，否则返工率会被系统性低估 —— 而"腰斩的返工率"看起来像是
> 能力提升了。

```bash
curl -s --noproxy '*' localhost:8000/metrics | grep -E 'agent_runs_total|agent_rework_total|agent_handoff_total'
```
- `score=needs_improvement` 居多 ⇒ 评审判据与产出不匹配：
  文学 agent 查篇幅/基调约束是否够明确；PPT 那串工具调用查 `SlideDeck` 的结构化字段是否过严；
- 同时看 `agent_participants_total` —— 若某个专家**从不出现**，说明它从没被调用
  （多是对它的工具描述写得不够明确，模型不知道什么时候该用）。

#### `AgentModelCallsPerRunHigh`（warning，15m）
平均每次 **agent 运行**的模型调用 > 12（`AGENT_MAX_TURNS` 的默认值）。
均值贴着上限，说明相当一部分任务在**贴着上限跑**，成本与延迟都会显著抬升。

> ⚠️ 直方图是**运行口径**（带 `agent` 标签）。一次请求 = 一次运行 ⇒ 它现在**也等于**
> 每个用户请求的成本，不需要再乘任何东西。

```bash
curl -s --noproxy '*' localhost:8000/metrics | grep -E 'agent_model_calls_(sum|count)'
# 哪一段在涨（前台 / 文学）—— 看 agent 标签；handoff 之后收尾的是文学，别混着看
curl -s --noproxy '*' localhost:8000/metrics | grep 'agent_runs_total'
```
- 先按 `agent` 标签定位，再看是"模型在想"（`agent_model_calls` 涨）还是"工具在往返"
  （`agent_tool_calls` 同向涨）；
- `agent_handoff_total` 现在只可能是 **前台 → 文学** 这一种组合。
  ⚠️ 别再去查"A→B→A 的往返"：只配了 `handoffs=[文学]` 一条交接，文学 agent
  没有配回程 handoff，所以那个模式结构上不可能出现 —— 拿一个不可能发生的模式
  去排查只会浪费时间；
- 检查专家 instructions 的 Routine 是否过长 —— **每多一条规则就多一次工具调用**。

#### `AgentGuardrailTripsFrequent`（warning，5m）
30 分钟内护栏拦截 > 5 次。

> ⚠️ **护栏拦截是正确行为**，要修的是产出质量，不是把护栏关掉。

```bash
curl -s --noproxy '*' localhost:8000/metrics | grep -E 'agent_guardrail_trips_total|agent_output_check_total'
```
- `guardrail=citation_traceability_guardrail` ⇒ 模型在**编造引用编号**
  （最典型的幻觉形态）。检查 `research` 是否稳定返回带编号的结论；
- `guardrail=length_guardrail` ⇒ 核对 `AGENT_MAX_ANSWER_CHARS` 与业务期望的篇幅。

#### `AgentRetrievalEmptyHigh`（warning，15m）
agent 链路的检索空召回率 > 50%。答案会退化成"依据不足"，而**检索器本身
不会记任何错误日志** —— 这正是它需要单独一条告警的原因。

```bash
curl -s --noproxy '*' localhost:8000/metrics | grep -E 'rag_hits_total|agent_retrieved_chunks'
# ① 先排除"语料根本没挂"（容器部署最常见的原因）
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=200 app-stable | grep 'RAG 语料为空'
```
- 全部打空 ⇒ `DOCS_DIR` 指向空目录 / 语料没挂进容器；
- 部分打空 ⇒ 模型改写出的检索式与语料用词偏离较大，
  在 `researcher` 的 instructions 里要求**优先复用用户原词**。

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

`promdata` 的保留期是**显式设成 40 天**的（`docker-compose.yml` 里 prometheus 的
`--storage.tsdb.retention.time=40d`），不是"不设就无限长"—— Prometheus 的默认值是
**15 天**。这个 40 天是 **SLO 的前提**而不是调优项：预算余量规则读 30 天窗口，
调小它会让预算静默只算半个月（"上周烧掉的那一段"滑出保留期、预算自己涨回来），
所以它有守卫钉着（`test_prometheus_retention_covers_the_longest_budget_window`）。
要按容量反推能留多久，看 `deploy/CAPACITY.md`。

### 4.3 故障演练（每季度挑一条做）

代码里的降级路径（熔断、配额、限流、断连止损）**不演练就等于不存在** ——
它们平时不生效，出事时才发现写错了，那时最贵。可演练的清单：

| 演练 | 怎么造 | 期望结果 |
|---|---|---|
| embedding 熔断 | 把 `EMBEDDING_API_BASE` 指到一个黑洞地址 | 检索走 BM25 回退，服务不崩，日志有熔断告警 |
| rerank 熔断 | 把 `RERANK_API_BASE` 指到黑洞 | `HybridRetriever.last_rerank_ok is False`，结果退回 RRF 融合顺序 |
| Redis 不可用 | `docker compose --env-file .env -f deploy/app/docker-compose.yml stop redis` | 退到进程内计数（**不是放行**），启动日志有明确提示 |
| PG 不可用 | `docker compose --env-file .env -f deploy/app/docker-compose.yml stop pgvector` | `/health/ready` 报 `db ok=false`；在途 SSE 被止损而非挂死 |
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
   前提是 staging 真的显式写了 `APP_ENV=production`。判据 2026-10-05 已改为
   「认不出来的取值一律当生产」（写 `staging`/`prd` 不再静默降级），但靠插值取值
   的地方仍会被部署机上残留的 `APP_ENV` 带偏 ⇒ **显式写死才是可靠前提**
   （见 `deploy/CANARY.md`）。
4. **"灰度只影响 10% 用户"** —— 灰度真正的价值是**在影响 10% 的时候拿到证据**。
   不看证据的灰度只是"慢一点的发布"。
5. **"`/health/live` 绿了就是健康的"** —— liveness 刻意只回进程标识、不查依赖
   （失败才重启容器）。依赖健康与否要看 `/health/ready`。
6. **"发布时个别用户的回答断在半句，是他们的网络问题"** —— 先查这里的三个值：
   `REQUEST_TIMEOUT_SECONDS`（请求总时限，180）≤ `GRACEFUL_SHUTDOWN_TIMEOUT`
   （排空上界，**默认跟随总时限** = 190）< `stop_grace_period`（容器宽限期，210s）。
   只要排空上界小于总时限，发布会**主动取消**那些本来还被允许继续跑的请求；
   只要宽限期小于排空上界，容器会在排空完成前 SIGKILL（日志里连一条都留不下）。
   启动日志里若有「断在半句」那条警告，就是第一个关系被配反了。
   `tests/test_graceful_shutdown.py` 钉住了这三个值的联动，改一个不改另一个会红。
