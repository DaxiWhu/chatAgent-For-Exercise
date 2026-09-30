# chatAgent SLO 与错误预算

> 本文是 SLO 的**定义**（唯一事实源）。`alerts.yml` 里 `chatagent-slo` 组的表达式是它的
> **实现**，两者由 `tests/test_monitoring_config.py::test_slo_doc_matches_alert_expressions`
> 逐行对齐 —— 只改一处会立刻测红。改 SLO 承诺时请同时改本文与表达式。

## 1. 为什么要有 SLO（而不是全靠阈值告警）

阈值告警只能回答"**此刻**是不是坏了"。它对两类真实故障几乎无感：

- **慢刀子**：错误率长期稳定在 6%，每个瞬时窗口都没超"红线"，但一个月下来已经违约；
- **尖峰后遗症**：一个 20 分钟的错误尖峰过去后，瞬时错误率早已回落，可它烧掉的预算已经回不来了。

SLO 把"坏不坏"换成"**按这个速度烧下去，月底还够不够**"，用**燃尽率**（burn rate）度量：

```
燃尽率 = 实际错误率 / (1 - SLO 目标)
```

- 燃尽率 = 1 ⇒ 不快不慢，刚好在窗口末把预算用完；
- 燃尽率 = 14.4 ⇒ 1 小时烧掉 30 天预算的 2%（约 8.6 小时烧光），必须立刻处置。

两种告警并存：`chatagent-availability` 组管"此刻坏了"，`chatagent-slo` 组管"预算在烧"。

## 2. SLI 口径

| SLI | 事件定义 | 好事件 | 坏事件 | 为什么这么定 |
| --- | --- | --- | --- | --- |
| `availability` | 一个 HTTP 请求 | 状态码非 5xx | 状态码 5xx | 用户能感知的"服务不可用"就是 5xx。4xx/429/413 是**我们主动拒绝**，不算可用性故障 |
| `latency` | 一个**非流式** HTTP 请求 | 耗时 ≤ 1 秒 | 耗时 > 1 秒 | 流式接口（`/v1/chat`、`/v1/chat/resume`）耗时等于整段生成的时长，算进来会让该 SLI 恒不达标 |

⚠️ **可用性 SLI 不要用 `http_errors_total` 当分子**。它只在**未捕获异常**时自增
（见 `app/middleware.py`），503 这类"正常返回的失败"（如就绪探针）不计入，
拿它当分子会系统性低估错误率 —— SLO 看着永远达标，这是最危险的一种错。
正确口径是 `http_requests_total{status=~"5.."}`。

⚠️ **标签值格式必须与 `app/metrics.py` 实际渲染一致**：桶边界渲染成 `le="1.0"`，
PromQL 是全串匹配，写成 `le="1"` 会让该规则**永不触发且没有任何报错**。

## 3. SLO 目标与错误预算

| SLI | 目标（窗口：30 天） | 错误预算 | 预算含义 |
| --- | --- | --- | --- |
| `availability` | 99.5% | 0.5% | 30 天内允许约 3.6 小时（按 1 rps 折算）的 5xx |
| `latency` | 99.0% 非流式请求 ≤ 1s | 1.0% | 30 天内允许 1% 的非流式请求超 1 秒 |

目标值是**起点**，不是圣旨：若确认某个低价值路由天然易错/易慢，正确做法是**正式**调整
SLO（改本文 + 改表达式），而不是让告警长期挂着 —— 一条长期 firing 的告警等于没有告警。

## 4. 燃尽率告警表（与 `alerts.yml` 一一对应）

| 告警 | slo | 目标 | 燃尽率 | 长窗口 | 短窗口 | 消耗预算 | 严重度 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SLOAvailabilityBurnFast | availability | 0.995 | 14.4 | 1h | 5m | 2% | critical |
| SLOAvailabilityBurnMedium | availability | 0.995 | 6 | 6h | 30m | 5% | critical |
| SLOAvailabilityBurnSlow | availability | 0.995 | 1 | 3d | 6h | 10% | warning |
| SLOLatencyBurnFast | latency | 0.99 | 14.4 | 1h | 5m | 2% | warning |
| SLOLatencyBurnSlow | latency | 0.99 | 1 | 3d | 6h | 10% | warning |

「消耗预算」= 燃尽率 × 长窗口 ÷ 30 天（例：14.4 × 1h ÷ 720h = 2%）。

## 5. 为什么必须"长窗口 + 短窗口"同时成立（多窗口多燃尽率）

| 只用 | 后果 |
| --- | --- |
| 只看长窗口 | 已经过去的尖峰让告警继续挂很久（恢复慢、误报长），值班的人很快开始忽略它 |
| 只看短窗口 | 30 秒抖动就能把人叫醒（噪音），同样会被忽略 |
| 两者用 `and` 同时要求 | 既证明"长期确实在烧"，也证明"此刻还在烧" —— 这条才值得叫人 |

实现要点：`expr` 里两个窗口各自独立计算比率，用 `and` 连接。两侧都是不带 `by` 的
`sum()`，标签集都为空，因此能正确配对。

## 6. 处置动作

| 告警 | 处置 |
| --- | --- |
| `SLOAvailabilityBurnFast`（critical） | 立即响应。先看 `HighErrorRate` / `StreamErrorsHigh` 是否同时 firing（是则同一根因）；按 `path` 标签定位路由；无法快速定位就回滚最近一次发布（`release` 标签可区分 stable/canary） |
| `SLOAvailabilityBurnMedium`（critical） | 通常是某个**长期存在**的错误源。看 6 小时内错误按 `path` 的分布是否集中在单一路由；结合 `llm_tokens_per_request` 判断是否模型侧退化 |
| `SLOAvailabilityBurnSlow`（warning） | 排期处理，不必立刻叫人。若确认是常态，应调整 SLI 口径或正式下调 SLO，而不是让它长期挂着 |
| `SLOLatencyBurnFast`（warning） | 按 `path` 看哪个路由变慢：`/v1/reviews` 慢 ⇒ 查审核队列表与 PG；`/health/*` 慢 ⇒ 查探针是否在等待外部依赖 |
| `SLOLatencyBurnSlow`（warning） | 排期优化（典型是 PG 查询缺索引或探针串行等待） |

## 7. 复核节奏

- **每周**：看错误预算剩余比例与本周燃尽最快的 SLI 是哪一个。
- **每月**：若某个 SLI 连续两个月都轻松达标（预算剩余 > 80%），说明目标定松了，应上调；
  若连续违约且已排查完技术原因，应正式下调并记录原因。
- **每次 SLO 变更**：同时改本文与 `alerts.yml`，并跑 `tests/test_monitoring_config.py`。

## 8. 已知局限（必须写清楚，否则会被误用）

1. **指标是单进程的**：多 worker / 多实例下每个进程各算各的。本文的表达式用 `sum(...)`
   跨实例聚合，因此**只对真正的全局错误率负责**；要按实例定位仍需 `instance` 标签下钻。
   要全局准数得换 `prometheus-client` + Pushgateway，或把计数汇总到 Redis。
2. **没有录音规则（recording rules）**：SLI 表达式在每条告警里内联重复。好处是不依赖
   额外的规则文件、口径就在眼前；代价是改口径要改多处，且面板上要重写同样的表达式。
   若将来要多处复用同一 SLI，应改为录音规则 `slo:availability_error_ratio` 等。
3. **短窗口受抓取间隔限制**：`scrape_interval` 为 30s，因此 5m 窗口只有约 10 个采样点。
   再短的窗口（如 1m）会因采样太少而抖动，不建议。
4. **燃尽率告警对"预算还剩多少"无感**：它是**速率**告警，窗口末尾的快照才看余量。
   若要"预算不足 10% 时提醒"，那是另一条规则（基于 `increase` 累计），本组未覆盖。
