# 任务分流：最终形态参考实现

> 这是「一个成熟 agent 收到问题后，怎么把不同任务送到不同流里」的**最终形态** ——
> 完整、可跑、可复制。不含分阶段路线（那在 `PLAN_task_routing.md`）。
>
> ```
> python -m examples.task_routing.demo       # 四条支路各跑一遍
> python -m examples.task_routing.selftest   # 18 条断言，退出码即结论
> python -m examples.task_routing.mutate     # 变异验证：证明守卫真的会红（5/5）
> ```

---

## 1. 一句话结论

**分流不是"在一条链上加 if-else"，而是把入口的判定编译成一份「能力集合 + 预算」的策略对象，
各支路按策略跑自己的循环，最后收敛到统一的出口闸门。**

三处最容易被做错的地方，本样例都按"错法"的另一边实现了：

| 常见错法 | 本样例的做法 | 为什么 |
|---|---|---|
| 模型输出 `intent: "chat"/"rag"/…` | 输出 `plan = {task, needs, budget, risk}` | 标签**排他**、能力**可叠加**；用标签走到第二条需求就退化成枚举组合 |
| 每个请求都问模型该走哪条路 | L0 规则（μs/$0）→ L1 缓存 → L2 模型（**默认关**） | 判官式路由每请求多付 50–300ms，30 步 agent 累积 4.5s；且分类时改写 prompt 会破坏 KV-cache（丢掉 60–90% 输入成本削减） |
| 四条支路各写一套生成逻辑 | **direct 复用 `generate`** | `generate` 在 `context == ""` 时本来就直传 messages ⇒ 直答 = 无上下文的 generate，只是跳过 `retrieve`。两份实现必然漂移 |

---

## 2. 四条支路：差别不在节点多少，而在「纠错信号从哪来」

这是全篇最核心的一条认知。决定拓扑的只有一件事：**这条支路纠错时看什么信号**。

| 支路 | 信号来源 | 可靠性 | 循环能放开到多宽 | 停止条件 | 预算 |
|---|---|---|---|---|---|
| `direct` | **无** | — | **不给循环** | 单次即出 | 0 |
| `rag` | **检索相关性** | 中 | 中 | 分数 ≥ HIGH / 预算尽 | 3 |
| `write` | **critic（内部）** | 自洽但会漂 | 宽 | **连续两轮无新意见** | 6 |
| `code` | **执行（客观真值）** | 最高 | 最宽 | 测试绿 / 超时兜底 | 8 |

三条由信号可靠性直接推出的实现细节：

1. **直答不给循环。** 没有可纠错的信号，循环只会换措辞、白烧 token。`max_iterations = 0`
   不是"还没配"，是设计。
2. **写作的停止条件是"连续两轮无新意见"，不是"分数到 90"。** LLM critic 会为了显得有用
   去**编**改进点，分数一直小幅波动 ⇒ 拿阈值当停止条件等于永远停不下来。
   实现方式是把每轮 issues 累积在 `state["critiques"]` 里与上一轮比集合包含。
3. **代码支路必须有独立安全域。** `guard` 只防 prompt 注入，**防不了生成代码里的 `rm -rf`**。
   所以 `execute` **刻意不内置任何执行实现**，只接受 `bind(sandbox=...)` 注入；未注入时
   明确不执行并标记 `executed=False` —— 示例代码会被复制到生产，一个"方便的内置执行器"
   就是定时炸弹。

---

## 3. 入口：三层路由 + 事后升级

```
L0 规则（μs / $0）   续问标记 → 编码指令 → 写作指令 → 检索指令/文档标识 → 纯寒暄 → 极短文本
L1 缓存（ms）        query 归一化指纹命中历史判定（去空白/去标点/小写）
L2 LLM（默认关）     结构化输出；失败降级到 fallback，绝不成为失败点
未命中               → fallback（默认 rag）
```

两条硬规则：

- **L0 的"续问"必须最先判。** 用户先问"帮我查 X"，拿到回答后说"再详细点" —— 单看这句是
  短句，实际必须**继承上一轮的 `needs`**。所以 `init` 刻意**不清** `route_plan`。
- **未命中时走 `rag` 而不是 `direct`。** 两类错误代价**不对称**：该检索却直答 = 事故
  （无依据作答、用户看不出来）；不该检索却检索 = 只是慢 100–300ms。方向与限流/鉴权的
  fail-closed **相反**。（`selftest` 第 3 条钉住这个方向。）

### 规则的词要锚在"动作 + 对象"上

`demo` 的 ⑤ 就是这个坑的实测：一句「了解这个系统在长时间**运行**之后内存会不会增长」
最初被判成写代码支路 —— 因为规则表里有光杆动词「运行」。它会把误判从"慢一点"
变成"跑一段用户没要求的代码"。修法是锚在 `运行这段` / `跑个测试` / ` ``` ` / `def x(` 上。

> **宽泛的词命中率高、看起来效果好，代价全在误判上。**

### 路由器只读不写

`decide()` 绝不修改 query，也不改写要发给生成模型的 prompt —— 理由见 §1 的第二行。
这条有守卫（`selftest` 第 5 条），而**守卫本身踩过一次坑**：最初的断言是
"`state["messages"][0]` 还是原文"，但 messages 是 **append-reducer**，改写表现为**追加**，
第一条永远是原文 ⇒ 变异后照样绿（假绿）。改成断言"模型**实际收到**的输入"才生效。
这个坑由 `mutate.py` 的 M3 抓出。

---

## 4. 出口：统一，但判据分治

所有支路都汇到 `finish` —— 观测/审核/审计/配额只实现一次。但**判据必须按支路分别审**：

> `direct` 支路没有 citations ⇒ `citation_check{status="no_citations"}` 在 direct 上是
> **正常状态**。不区分支路，direct 流量会把 `no_citations` 灌满，真正的"该有引用却没有"
> 就被淹没在噪声里。

⇒ 所有出口指标必须带 `route` 标签（`route` 是**有限取值**，安全；**绝不能带 query**）。
**每加一条支路，`EXIT_JUDGES` 与指标标签都要重审一遍** —— 这是加支路最常被漏掉的一次性成本。

### 退出归因：达标 ≠ 预算耗尽

`exit_reason` 的五种取值：`converged` / `budget_exhausted` / `not_needed` / `forced` / `rejected`。

⚠️ **这两种混着报，调优方向会完全错** —— 你会以为该去调 prompt，其实只是预算不够。
命名原则：只描述事实，不描述判断（别把"好/坏"塞进归因值，否则口径不同的调用方会对
同一批数据得出相反结论）。

### 升级人工：低分 **AND** 高风险

`after_review` 里转人工的条件是 `score < LOW` **且** `risk.factual_claim|side_effect`。
只看分数会让"写首诗 55 分"挤占人工队列，把审核资源从真正需要人的场景上挤走。

---

## 5. 目录

| 文件 | 作用 |
|---|---|
| `plan.py` | 策略对象、各支路默认预算、fail-safe 方向、`plan_from_state` 容错恢复 |
| `router.py` | 三层路由、L0 规则表、有界指纹缓存 |
| `state.py` | `OverAllState` 字段定义与两条 state 硬约束 |
| `nodes.py` | 四条支路的节点与条件边（含 `EXIT_JUDGES`、`_classify_exit`） |
| `graph.py` | 图组装（唯一走向决定点 = `route_task`） |
| `demo.py` | 可跑的四支路演示（替身模型 + 计数替身检索器） |
| `selftest.py` | 18 条断言，纯 assert、退出码即结论 |
| `mutate.py` | 变异验证（5 组，预检 + 自证还原） |

---

## 6. 接入生产

样例是自包含的；并进生产时按这张清单来（顺序有依赖）：

1. **`src/router.py`（新建）** —— 直接搬 `router.py` + `plan.py`。
   ⚠️ `src/` 有 AST 守卫「不得 import `app`」（`tests/test_index_identity.py`），
   本样例已满足：只用标准库。
2. **`src/graph.py`** —— 给 `OverAllState` 加 `state.py` 的字段（是**超集**，直接合并）；
   加 `route_task` 节点与 `after_route_task` 条件边；把 `route_tool` 改成 `after_generate`。
   `generate` / `evaluate` 等现有节点函数**一行不用改**。
3. **`src/prompts.py`** —— 若要做 L2 路由，`_critique_prompt` / 路由 prompt 必须集中到这里
   并 bump `PROMPT_VERSION`（`tests/test_prompts.py` 存着版本→指纹映射）。P0 不动 prompts。
4. **`app/config.py`** —— `RAG_ROUTER` / `RAG_ROUTER_MODE` / `RAG_ROUTER_CACHE_SIZE` /
   `RAG_BUDGET_*`，进 `as_dict()` 以便出现在启动日志回显。
   ⚠️ `RAG_ROUTER=0` 时行为必须**逐字段**回到改造前（含走 `retrieve`）—— 这是"能力开关
   默认开、关掉即回到旧行为"的既有约定。
5. **`app/main.py`** —— SSE 收尾读 `values["route"]` / `route_source` / `retrieval_stats` /
   `exit_reason` / `exit_verdict` 上报指标；`_thread_view` 加同名字段。
   ⚠️ `route_plan` 是普通 dict，可以直接放进视图；但**不要**把 `critiques` 全量下发。
6. **指标**（`app/metrics.py` 的 API）：
   `route_total{route, source}` / `route_decision_duration_seconds` /
   `chat_exit_reason_total{reason}` / `rag_hits_total{result}` / `rag_retrieval_degraded_total`。
   标签只能取**有限值**，绝不能放 query。
7. **告警**（`deploy/monitoring/alerts.yml`）：
   ⚠️ 其中最重要的一条是 `RouteDirectRateAbnormal`（direct 占比相对基线突增）——
   它对应本项目最贵的坑：**检索整个挂掉但服务外观全绿**（5xx 不涨、延迟反而**变低**、
   答案照常返回）。
8. **面板**：加面板就必须把 `"route_"` / `"chat_"` 加进
   `tests/test_grafana_dashboards.py` 的 `_APP_PREFIXES`。
   ⚠️ 那个守卫是**单向**的：只验"面板里的标识符必须在代码里存在"，**不**验"代码产出的
   指标必须进前缀表"。漏加前缀 ⇒ 那块面板不受任何保护，写错一个字母 Prometheus 不报错、
   面板永远空白，**看起来和"最近没流量"一模一样**。
9. **启动自检** —— 把"检索器是否可用 / 语料是否非空"显式预热。改造后语料加载从
   "首次请求"推迟到"首次 rag 请求"，失败信号出现得更晚。

### 守卫与变异

并进生产后应保留的守卫（`selftest.py` 里都有对应实现，5 组变异全部能钉住）：

1. `direct` 不触碰检索器 —— **必须用计数替身断言 `calls == 0`**，不能断言"context 为空"
   （检索失败时也为真 ⇒ 假绿）。
2. `rag` 恰好检索一次（`calls == 1`）。
3. 判据未命中 → `rag` 而非 `direct`（fail-safe 方向）。
4. `plan` 可 JSON 序列化。
5. 路由器不改写模型实际收到的输入。
6. 续问继承上一轮支路，且确实又检索了一次。
7. `budget_exhausted` 与 `converged` 可区分。
8. 不可恢复的失败（沙箱未注入）**不重试** —— 否则一个配置错误变成 8 轮模型调用空转。
9. 路由缓存有界。

---

## 7. 诚实的边界

- **不做模型选型层。** 本项目只有 1 个对话模型 + 1 个 embedding 模型，没有可选项；
  先做车道（要不要检索/反思），收益大一个数量级。
- **不做多智能体协商。** 单轮问答 + 人工审核的形态下，协商复杂度远大于收益。
- **不做级联。** 10× 价格比下要便宜档成功率 `p > 0.7` 才划算，且"够不够好"必须
  **免费且确定性**地判定 —— 本项目的判据（编号可追溯/来源存在/重叠率）不是这个性质。
- **`write` 与 `code` 的预算值（6 / 8）是拍的**，需要用真实语料跑一轮评测再定；
  它们现在唯一的依据是"比 rag 宽"这条定性判断。
- **路由准确率没有离线评测。** 要能度量，得给 `scripts/eval_set.json` 加 `expect_route`
  与 `route_cost`（错误代价权重），统计**代价加权错误分**而不是准确率 ——
  准确率 92% 会完全掩盖"错的那 8% 全在高代价方向"。
