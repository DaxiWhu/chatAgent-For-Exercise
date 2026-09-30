# 灰度发布、判据与回滚

> 这份文档回答一个问题：**改完镜像 tag 之后，我怎么知道该不该放量 / 该不该回滚？**
>
> 在此之前，答案是"打开 Prometheus 面板看一眼"。那不是一个流程，因为它
> 在**灰度没有流量**时给出的结论是"看起来没问题"——而那条路径的终点是
> 把一个从未被验证过的版本放量到 100%。

---

## 一、三个环境的配置分工（别混着改）

| 层 | 文件 | 管什么 | 典型内容 |
|---|---|---|---|
| 共享 | `.env`（项目根） | 各环境**共同**的配置与凭据 | 模型密钥、embedding 地址、限流阈值 |
| 环境差异 | `deploy/app/docker-compose.staging.yml` | **环境之间**的差异 | 端口、副本数、`APP_RELEASE`、关掉监控与备份 |
| 版本差异 | `deploy/app/canary.env` | **同一环境内 stable / canary** 的差异 | 试新模型、开实验开关 |

把"环境差异"写进 `.env` 是最常见的错误：那样本地直跑 `python -m app.serve`
会连带拿到 staging 的配置，而你以为自己在跑本机默认值。

起 staging：

```bash
docker compose --env-file .env -f deploy/app/docker-compose.yml \
               -f deploy/app/docker-compose.staging.yml up -d --build
```

> ⚠️ staging overlay 里有两个**必须**遵守的点（文件头有详细说明，这里只列结论）：
> 1. `name:` 必须改 —— 否则与生产**共用同一套命名卷**，staging 会直接写到生产数据上；
> 2. `APP_ENV` 必须写 `production`，**不能**写 `staging` ——
>    `app/config.py` 只把 `production`/`prod` 当生产，写 `staging` 会**静默关掉鉴权、
>    打开 /docs**，于是 staging 唯一与生产不同的地方恰好是最该验证的那一层。
>    环境标识请用 `APP_RELEASE`。

---

## 二、发布流程（一条命令，但每一步在做什么必须清楚）

> **整条流程已经脚本化**：`deploy/release.sh <镜像>` 会依次做
> 「拉起灰度实例 → 改权重 → reload → 等观察窗 → 跑判据 → 按退出码决定」，
> 判据不达标时**自动调用 `deploy/rollback.sh`**。
> 下面把它拆开讲一遍 —— 因为自动化不等于"不用懂"，出事时你需要知道它停在哪一步。

### ① 拉起灰度实例（只动灰度）

```bash
# 一条命令跑完整个流程（档位 1% → 10% → 50% → 100%，每档观察 5 分钟）
deploy/release.sh ghcr.io/<org>/chatagent:sha-1a2b3c4d5e6f

# 先看清楚它要做什么，不真动
deploy/release.sh <镜像> --dry-run

# 自定义档位与观察窗
deploy/release.sh <镜像> --stages 1,5,25,100 --observe 600
```

**用 `sha-<短哈希>` 而不是 `:main`。** `:main` 会被下一次推送覆盖，而发布出问题时
你要回答的第一个问题是"线上跑的到底是哪个 commit"——只有 sha 标签能回答它
（CI 的 publish job 同时打这两种标签，见 `.github/workflows/ci.yml`）。

脚本做的事就是原来那条命令，只是**不会再忘记 `--env-file .env`**
（漏了它整套 `${VAR}` 插值都会退回默认值，见 compose 文件顶部）：
`CANARY_IMAGE=<镜像> docker compose --env-file .env -f deploy/app/docker-compose.yml up -d --no-deps app-canary`。

**做完是什么样：** `curl -s localhost:8080/health/live` 的 `release` 字段
变成 `canary`。若没变，说明流量根本没到灰度，此时**不要继续看指标**，
先解决"流量到不了"（下一步的判据会直接告诉你这件事）。

### ② 观察一个完整窗口

窗口至少 10 分钟，且要覆盖一次真实的流量形态（有人提问、有长回答）。
窗口太短的后果不是"判不准"，而是**样本量判据会直接报"无法判定"**——
这是刻意设计，见第三节。

### ③ 跑判据

```bash
python scripts/canary_check.py --prometheus-url http://localhost:9090 --window 10m
```

输出示例（每条判据都会同时打印灰度与稳定版的值，用来区分"是灰度引入的"
还是"整体在坏"）：

```
灰度判据（窗口 10m）
--------------------------------------------------------------------------
✅ 灰度样本量      canary=      1043  stable=       9288
     窗口 10m 内灰度 1043 个请求
✅ 5xx 比例        canary=    0.0041  stable=     0.0038
     在容差内
❌ P95 延迟        canary=    0.9200s stable=     0.4100s
     相对劣化：0.9200s vs 稳定版 0.4100s（容差 1.3x + 地板 0.05s）
--------------------------------------------------------------------------
结论：FAIL —— 应回滚灰度（见 deploy/CANARY.md 的回滚步骤）
```

**退出码就是判据本身**（与 `pg_backup.py` / `qdrant_backup.py` 同一套约定）：

| 码 | 含义 | 该做什么 |
|---|---|---|
| `0` | PASS | 可以逐步放量，**每次调整后重新跑一遍** |
| `1` | FAIL | **立刻回滚**（下一步） |
| `2` | 无法判定 | **不要当成通过，也不要回滚** —— 先解决"看不到数据"：Prometheus 是否可达、两个抓取目标是否 UP、灰度是否真的在收流量 |

> ⚠️ `2` 被单独拎出来，是因为它和 `1` 的处置完全相反。把"没采到数据"
> 归成"失败"会让你回滚一个其实没问题的版本；归成"通过"则是在零数据的情况下放量。

### ④ 放量 / 回滚（脚本化，但仍要懂它在改什么）

**放量**（渐进，不要一次到底）：

```bash
# 手工方式（脚本做的就是这件事）
python scripts/nginx_weight.py set --stable 99 --canary 1
docker compose --env-file .env -f deploy/app/docker-compose.yml exec nginx nginx -t
docker compose --env-file .env -f deploy/app/docker-compose.yml exec nginx nginx -s reload

# 看当前档位（不用去翻配置文件）
python scripts/nginx_weight.py show
```

> 用 `scripts/nginx_weight.py` 而不是手工编辑 `nginx.conf`，理由是**改错的失败方式**：
> nginx 权重写错的典型表现是"改了但没生效"（匹配不到那一行 / 忘了 reload），
> 而它**不会报错** —— 于是你会以为压了 10% 流量，实际还是 1%，
> 判据的样本量永远不够。该脚本"改不到就报错退出"，写完还会读回来比对，
> 并且拒绝把 stable/canary **同时**设为 0（那会让 nginx 返回
> `no live upstreams` 502，等于把一次改配置变成一次全站故障）。

每次调完权重，**重新回到第 ② 步**观察一个窗口。权重越大，判据的
置信度越高（样本量够了），而一旦有问题，影响面也越大。

**回滚（一条命令，自带验证）**：

```bash
deploy/rollback.sh --reason "P95 从 0.41s 涨到 0.92s"
```

它做四件事，顺序是刻意的：

1. `python scripts/nginx_weight.py set --canary 0`；
2. `nginx -t`（**先校验再 reload**：reload 失败时旧配置继续生效，
   表现和"忘了 reload"一模一样）；
3. `nginx -s reload`；
4. **验证**：连打入口 10 次，确认 `/health/live` 的 `release` 字段
   一次都不是 `canary` —— 因为"改了配置"与"流量真的回去了"是两件事。

保存现场后才停容器（默认**不停**，它就是你唯一的证据）：

```bash
docker compose --env-file .env -f deploy/app/docker-compose.yml logs --tail=200 app-canary
deploy/rollback.sh --reason "..." --stop-canary        # 事后清理时再停
```

**回滚后必须做的事**：把失败的判据值连同当时的日志一起记下来。
灰度最大的价值不是"少影响 10% 用户"，而是**在影响 10% 的时候就能拿到证据**。
不回看证据就等于白灰一场。

`nginx.conf` 里 `max_fails=3 fail_timeout=15s` 已经保证灰度实例**崩掉**时
nginx 会自动摘掉它（那 10% 流量转到稳定版），所以"进程起不来"这一类问题
不会持续伤用户。但请注意它挡不住**语义错误**（回答变差、变慢、变贵），
那正是第三节判据存在的理由。

---

## 三、判据为什么是这四条

`scripts/canary_check.py` 跑四条判据，每一条都对应一类**只看面板看不出来**的问题：

| 判据 | 抓什么 | 为什么不能去掉 |
|---|---|---|
| 灰度样本量 | 灰度**有没有流量** | nginx 权重写错、灰度容器没起来、抓取目标选错都表现为"一条请求都没有"。此时所有指标都是干净的，任何只看指标的判据都会给出通过 |
| 5xx 比例 | 报错 | 用 `http_requests_total{status=~"5.."}` 而非 `http_errors_total`（后者不含 503，会系统性低估） |
| P95 延迟 | 变慢 | 灰度最典型的问题是"变慢"而不是"报错"，只看错误率会放行一个慢一倍的版本 |
| 流式错误率 | SSE 中途挂掉 | **主接口是 SSE，流中途失败时响应头早就发出去了，HTTP 状态码看着完全正常**。只看 5xx 会 100% 漏掉这类问题 |
| 每请求 token | 成本 / 上下文膨胀 | 新版 prompt 变长、检索多召回了几块，在错误率与延迟上都看不出来，但账单会翻倍 |

两个关键实现选择，改动前请先读 `scripts/canary_check.py` 里的注释：

1. **全部用 `rate` 比较，不用绝对值。** 灰度只承载 10% 流量，绝对错误数天然是
   稳定版的十分之一 —— 按绝对值比较，"灰度永远看起来更好"，等于永远判通过。
2. **判据是"相对劣化 **或** 绝对超标"。** 只看相对会被坏基线带偏（稳定版自己
   已经 5% 时，灰度 5.2% 会被当成"没劣化"）；只看绝对会放过低基数下的成倍劣化。

### 阈值怎么调

默认值是保守的起始点，都在 `python scripts/canary_check.py --help` 里：

| 参数 | 默认 | 什么时候动它 |
|---|---|---|
| `--window` | `10m` | 流量很小 ⇒ 拉长到 `30m`（否则样本量判据会一直报"无法判定"） |
| `--min-requests` | `30` | 同上；门槛低于 30 时统计噪声会盖过真实差异 |
| `--max-error-ratio` / `--max-p95-ratio` | `1.5` / `1.3` | 有明确的性能预算时收紧 |
| `--max-error-abs` | `0.02` | 与 `deploy/monitoring/SLO.md` 的可用性目标对齐后再改 |
| `--max-token-ratio` | `1.5` | 换模型/改 prompt 导致成本结构性变化时，先改基线再改这里 |

### 已知局限（诚实列出）

- **每请求 token 是粗粒度代理**：它用全部请求作分母，能发现"整体成本异常"，
  但不会告诉你具体哪个接口。要细查请按 `path` 标签下钻。
- **只覆盖"当前窗口"**：它回答"现在好不好"，不回答"过去 24 小时有没有漂移"。
  后者属于 SLO 燃尽率告警的职责（`deploy/monitoring/alerts.yml`）。
- **依赖 Prometheus 单实例口径**：多副本部署时指标是各进程各算各的，
  脚本用 `sum by (release)` 聚合，因此**取到的是各副本之和**——
  这对比例型判据是对的，对绝对计数类判据（样本量门槛）要按副本数放宽。
- **回滚已自动化，但"回滚后通知人"还没有**：`deploy/release.sh` 在判据返回 1 时
  会自动调 `deploy/rollback.sh`（幂等，重复执行无害），止损不依赖人是否在场。
  **缺口在下半句**：回滚完成后没有任何东西主动告诉你"刚刚发生过一次回滚"。
  它不会触发 `alerts.yml` 里的规则（回滚本身不是异常指标）。
  可用的替代信号是部署时盯 `scripts/canary_check.py` 的输出与 `release` 标签的
  分布变化；要做到"回滚即通知"，需要给发布流程加一条 webhook —— 尚未做。

---

## 四、判据是怎么接进发布流程的（已脚本化）

这一节原来是"给一段示例脚本，但刻意不进仓库"。**这个决定在 2026-09-30 被推翻**，
理由是原来那条顾虑（"自动改配置文件很容易把状态改到一个谁都不清楚的位置"）
现在有更便宜的对策，而不自动化的代价反而更高：

- **代价**：手工流程在真正需要回滚的那一刻最不可靠 —— 人已经慌了，
  而"改 weight + reload + 确认生效"里最容易漏的恰好是最后一步。
- **对策**：把"改配置"收进一个**幂等 + 自带校验 + 自带验证**的脚本里
  （`scripts/nginx_weight.py` + `deploy/rollback.sh`），并且让脚本每一步都
  打印它改了什么、当前是什么状态。

现在的入口是 `deploy/release.sh`，它与判据的接线方式是**退出码**：

| `canary_check.py` | `release.sh` 的动作 | 为什么 |
|---|---|---|
| `0` PASS | 推进到下一档 | 判据达标 |
| `1` FAIL | **自动执行 `deploy/rollback.sh`**，脚本退出码 2 | 快速止血；回滚是幂等的，重复执行无害 |
| `2` 无法判定 | **停在当前档位**，脚本退出码 3 | 回滚一个其实没问题的版本同样是错；这一步必须由人决定 |

> ⚠️ 第三条是整条流程里最容易被写错的地方：**"非 0 就回滚"**。退出码 2 表示
> "看不到数据"（样本不足 / Prometheus 不可达），而不是"灰度有问题"。
> 把它归为失败会让你回滚一个健康的版本，还顺手把"看不到数据"这个真问题掩盖掉。

手工只跑判据（不发布）：

```bash
python scripts/canary_check.py --window 10m --json | python -m json.tool
```
