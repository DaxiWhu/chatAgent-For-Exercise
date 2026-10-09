# PLAN：任务流分流（task routing）

> 状态：**待实施**（2026-10-01）。
> 拍板口径：**按「能力集合 + 预算」分流，不按「意图标签」分流**；判据**便宜在前**且**允许事后升级**；
> 只有 **direct / rag 两条支路进 P0**，write / code 先留 `needs` 位不实现。
> 触发问题：全链路是一条固定直链 —— 寒暄也要付一次 embedding、也要过一次评审模型；
> 而唯一的"要不要评审"信号（`ReviewResult.is_need_evaluate`）产生在**付完检索与生成之后**，位置反了。
> 实施代价：**不重建索引**、不动 `src/rag/` 解析链路；只加 1 个节点 + 2 条条件边，现有节点函数不改。
> 前置阅读：无。本方案不触碰 `PIPELINE_CONSTRAINTS.md` 覆盖的任何口径。

---

## 0. 结论摘要

四句话说完：

1. **入口产出的是一份 `plan`（能力位 + 预算 + 风险 + 兜底），不是 `intent` 字符串。** 标签排他、能力可叠加，
   用标签做分流在第二条需求进来时就会退化成"枚举所有组合"。
2. **判定分三层、便宜在前**（规则 → 缓存/近邻 → LLM），且**必须保留一条事后升级通道**——
   入口误判无法根除，但有了升级通道，一次误判的代价从"答错"降级成"慢一点"。
3. **四条支路的真正区别不在节点多少，而在"纠错信号从哪来"**：直答无信号、检索问答看检索相关性、
   写作看 critic（内部信号，天然自洽）、代码看执行（唯一客观真值）。信号可靠性决定循环能放多宽。
4. **本项目 P0 只需要跳过 `retrieve` 这一个动作** —— `graph.generate` 在 `iteration == 0` 且 `context == ""`
   时**已经**走"直传 messages"的分支，所以"直答支路"就是它已有的那条无上下文路径。不需要新写生成节点。

---

## 1. 行业标杆怎么做（2026 年口径）

### 1.1 三层路由：分类器 → 车道 → 选型

成熟产品的路由器是**三段串联**，每段回答不同的问题：

| 层 | 回答什么 | 档位 |
|---|---|---|
| ① 任务分类器 | "这是哪一类任务"（代码生成 / 解释 / 评审 / 调试 / 抽取…） | 关键词正则 < 1ms $0 ＞ 小型本地分类器 ＞ 托管分类器 ＞ 前沿 LLM 分类器 |
| ② 车道路由 | "这一例多复杂、多急" | 本地自托管 / 异步批处理 / 实时云 |
| ③ 模型选型 | "这一档里哪个模型性价比最好" | 读实时延迟与成本信号 |

两条关键的量化结论：

- **判官式路由（用模型做分类）在每个请求上多付 50–300ms。** 在一个 30 步的 agent 里，
  这是 **4.5 秒纯路由开销**。对交互式产品，这个开销会变成用户可感知的等待。
- **分类时改写 prompt 会破坏 KV-cache。** 前缀必须逐字节一致才能复用缓存；路由器一旦改写或注入
  分类前言，**每请求的前缀都不同** ⇒ 直接丢掉稳定前缀带来的 **60–90% 输入成本削减**。
  ⇒ 硬规则：**路由器只读不写，绝不修改要发给生成模型的 prompt。**

**对本项目的意义**：我们没有多模型可切，所以第 ③ 层现在无意义；但第 ② 层的"车道"概念直接可用——
**"要不要检索 / 要不要反思"就是车道**。

### 1.2 按「任务类」路由，不预测「难度」

业界最被推荐的第一版路由不是"判断这题难不难"，而是**按任务类分流**：

> 分类、抽取、格式化通常在小模型能力范围内；规划、多步推理、综合通常不在。
> **按你自己代码里的步骤位置去路由（这是工作流的哪一步），而不是去"审视内容"。**

四个好处：**确定性**（同类任务永远走同一条路，可复现可排查）、**可解释**（规则能念给人听）、
**零成本**（没有分类调用、没有额外延迟）、**可度量**（每个任务类能单独跑基线与门禁）。

### 1.3 级联（cascade）的盈亏平衡点

"先用便宜档、不够再升级"听起来严格更优，但**有盈亏平衡**：设便宜档成功率为 `p`、两档价格比 10×，
则 **`p > 0.7` 时级联省钱，`p < 0.7` 时反而更贵**（两个模型都付了，还多付延迟）。

两个极易忽略的代价：

- **评估不能收费**。如果用模型调用来判"够不够好"，这次判断必须比它避免的升级更便宜 ⇒
  现实中只能是**确定性检查**（schema 校验、必填字段、数值范围、工具调用是否格式正确）。
- **升级是叠加延迟的**，平均成本降了但 **p95 变差**。

⇒ **"需要判断力"的场景用任务类路由，不要用级联。** 这条直接决定了本项目 P0 不做 LLM 判官。

### 1.4 成本口径：每个成功任务的成本，不是每次调用

> 一个便宜模型失败一次、触发重试或升级，很容易比直接调用一次强模型更贵。
> Agent 循环会放大这一点：弱模型走 7 步而不是 4 步，因为**每一步都会累积上下文**，
> token 消耗随轨迹长度**超线性**增长。

同时必须跟踪**每档的拒答率与过度拒答率**——"便宜模型更爱拒答合法请求"产生的重试与用户挫败，
任何成本模型都反映不出来。

### 1.5 编排者–工作者：前沿模型规划，小模型执行

```
用户请求 → 分类器(50–100ms)
             ├─ 简单/受限任务 → 小模型工作者池
             └─ 复杂/推理任务 → 前沿编排者 → 综合 → 输出
```

结论性判断：**小模型做执行，不做规划**（可校验的窄任务：工具调用生成、抽取、格式转换、分类）。
反过来，`规划 / 跨步约束追踪 / 最终综合` 恰恰是前沿模型不可替代的地方。

**路由同时是治理面**（这一条常被忽略）：

- **不可信入口的能力上限**：公网入口不需要前沿模型 + 200k 上下文，
  把"从不可信输入可达的模型档位与上下文长度"钉住，是防 denial-of-wallet 性价比最高的单一控制。
- **数据驻留变成可执行的规则**，而不是一份政策文档——路由决定哪个供应商、哪个法域处理这条请求。

### 1.6 编码类标杆的做法：Explore → Plan → Execute

以 Claude Code 的形态为代表（一手文档 + 使用惯例）：

| 阶段 | 用什么 | 为什么 |
|---|---|---|
| Explore | 便宜模型 + **只读**工具（Read/Grep/Glob） | 探索是"产出大量你不会复用的输出"的典型：高输出/结论比，正好用上下文边界隔掉 |
| Plan | 主会话自己规划 | 规划是分析性的，且**方案要给人看** |
| 审批 | **人类闸门放在"改文件之前"** | 这是唯一真正重要的闸门位置 |
| Execute | 强模型 + 写权限 | 改文件是紧耦合、需要判断的动作 |

**委派判据**（三条该委派 / 四条不该委派）：

- 该委派：① 输出/结论比高（日志、全树搜索、只要一个事实的文档）；② 独立、可并行；
  ③ 只读、自包含。
- 不该委派：① 与正在进行的编辑紧耦合；② 已经定好的小改动（冷启动开销大于收益）；
  ③ **中途需要人类审批** —— 子代理不能发起用户提问，这条是决定性的；④ 各阶段共享大量上下文。

**技能 = 渐进披露**：启动时只加载 name + description（约 100 token），命中后才读完整 `SKILL.md`，
最后才读附带资源。这条对 token 预算是决定性的，也是"知识层"和"路由层"分离的实现方式。

**子代理的 description 就是路由表**：描述含糊（"帮助处理代码"）永远路由不准；
必须写清**确切的触发条件**。生产路径上**显式调用比自动委派更可取**——因为它是确定性的。

### 1.7 WorkBuddy 的形态（一手）

WorkBuddy 把"分流"拆到了三个不同的层，而不是塞进一个分类器：

| 层 | 形态 | 作用 |
|---|---|---|
| 任务闸门 | **工作模式**：Agent（直接执行）/ Plan（先出方案等确认）/ Ask（只读不动） | 由用户显式选择 ⇒ **确定性**，不猜。等价于"路由结论决定权限上限" |
| 能力层 | **专家**（方法论）/ **技能**（可复用流程）/ **连接器**（外部系统）/ **资料库**（知识） | 把"要不要查资料、要不要连外部系统"外化成**独立的资源层**，而不是塞进 prompt |
| 执行层 | 自主规划与执行、多任务并行、产物交付 | 交付物（文档/表格/PPT）而非文本 |

对本项目**最可移植的两点**：

1. **把不确定性交给用户显式表达**（工作模式），而不是让模型猜。低成本、零误判。
2. **知识层独立于路由层**：WorkBuddy 的资料库是用户显式挂载的，不在 prompt 里判断"要不要 RAG"。
   你项目的 `DOCS_DIR` + 租户隔离已经是这个形态了，**别退化**。

### 1.8 反向结论：哪些标杆做法**不该抄**

| 不抄 | 理由 |
|---|---|
| 每次请求都问模型"该走哪条路" | 50–300ms/请求，且是本项目**现在就有的病**（`evaluate` 里的 `is_need_evaluate` 就是它） |
| 多智能体协商 / 互相评审 | 本项目是"单轮问答 + 人工审核"，协商带来的复杂度远大于收益 |
| 模型选型（第 ③ 层） | 只有 1 个对话模型 + 1 个 embedding 模型，没有可选项。先做车道，收益大一个数量级 |
| 异步批处理车道 | 交互式 SSE 场景不适用（除离线评测与 `kb_prune` 这类任务，已有独立脚本） |
| 路由器改写 prompt | 破坏 KV-cache（§1.1），且让"模型实际看到了什么"不可复现 |

---

## 2. 设计口径（本项目拍板）

### 2.1 `plan` 而不是 `label`

```python
plan = {
    "needs":      {"retrieval": bool, "reflection": bool, "code_exec": bool, "human": bool},
    "budget":     {"max_iterations": int, "max_tokens": int, "deadline_ms": int},
    "risk":       {"factual_claim": bool, "injection_surface": bool, "side_effect": bool},
    "confidence": float,        # 路由器自己的置信度（不是答案的置信度）
    "fallback":   str,          # 路由器也不确定时走哪条（默认 "rag"）
}
```

⚠️ **`plan` 必须是普通 dict，不能是 pydantic 实例**：state 会被 checkpointer 序列化，
放不可序列化对象在续跑时炸在半路。

### 2.2 三层判定 + 事后升级

```
L0 规则（μs / $0）      长度、续问标记、显式 URL/文件名、纯寒暄
L1 缓存 + 近邻（ms）    query 归一化指纹命中历史判定 / 向量近邻
L2 LLM 结构化输出       **默认关闭**
```

⚠️ **两条硬规则**：

- **L2 默认关**。开着它等于把"路由开销"重新引入，而本项目收益主要来自"跳过检索"这一件事。
- **L0/L1 未命中时默认走 `rag`，不是 `direct`**（fail-safe 偏多做）。这与限流/鉴权的 fail-closed 方向
  **相反**，因为两类错误的代价不对称（见 §2.6）。这条必须有测试钉住。

### 2.3 反馈信号决定拓扑

| 支路 | 信号来源 | 拓扑 | 预算 |
|---|---|---|---|
| direct | 无 | `LLM × 1`，无循环 | `max_iterations = 0` |
| rag | **检索相关性** | `retrieve → generate → evaluate`；回边=`不足则改 query 重检索`（P2） | 3 |
| write | **critic**（内部） | `draft → critique → revise`；停止=`连续两轮无新意见` | 6 |
| code | **执行** | `写 → 跑 → 读错误 → 修`；停止=`测试绿` | 由超时兜底 |

写作用"连续两轮无新意见"而不是"分数到 90"：**LLM critic 会为了显得有用来编改进点**，
分数会一直小幅波动，用阈值当停止条件等于永远停不下来。

### 2.4 出口统一、判据分治

出口闸门共享（观测/审核/审计/配额只实现一次），但**判据必须按支路分别审**：

> direct 支路没有 citations ⇒ `citation_check{status="no_citations"}` 在 direct 上是**正常状态**。
> 不区分支路，direct 流量会把 `no_citations` 灌满，真正的"该有引用却没有"被淹没。

⇒ **所有出口指标必须带 `route` 标签**（`route` 是有限取值，安全；**绝不能带 query**）。

### 2.5 预算显式化

`plan.budget` 必须**替换** `src/graph.py` 的模块常量 `MAX_ITERATION`，不能与之并存（两处真相）。
同理 `LOW`/`HIGH` 若按支路分化，也要走同一份 plan。

### 2.6 错误代价不对称（决定所有默认值）

| 误判方向 | 后果 | 严重度 |
|---|---|---|
| 该检索 → 判成 direct | 无依据作答、可能编造，**用户看不出来** | **高（事故）** |
| 不该检索 → 判成 rag | 多一次 embedding + 向量查询，+100~300ms | 低（只是慢） |
| 该反思 → 判成 direct | 质量略低，但不出错 | 低 |
| 不该反思 → 判成 write | 多 2–3 倍 token 与延迟 | 中（成本） |

⇒ 默认值一律**偏多做**；评测用**代价加权的混淆矩阵**，不用准确率
（准确率 92% 会完全掩盖"错的那 8% 全在高代价方向"）。

---

## 3. 目标架构

### 3.1 图结构

```
START → init → route_task ─┬─ "direct" ─────────────────────→ generate ─┬─ (tool_calls) → tool_node ┐
                           │                                            │                        │
                           │                                            └─ route == direct → END │
                           │                                                                     │
                           └─ "rag" ──→ retrieve ──→ generate ──→ evaluate ──→ route_review ──────┘
                                                                                  ├─ end
                                                                                  ├─ regenerate → generate
                                                                                  └─ human → human_review
```

**关键发现（决定了 P0 有多小）**：`graph.generate` 在 `iteration == 0` 且 `context == ""` 时
**已经**走 `MODEL.invoke(messages)` 这条分支。也就是说：

> **direct 支路 = 现有的"无上下文 generate"，只是跳过 `retrieve` 这一步。**

所以 P0 **不需要新写生成节点**，只需要：

- 新增 1 个节点 `route_task`；
- 新增 1 条条件边 `route_task → {direct: generate, rag: retrieve}`；
- 把 `route_tool` 换成 `route_after_generate`（多一个分支：`route == "direct"` → `END`）。

### 3.2 状态字段（`graph.OverAllState` 新增）

| 字段 | 类型 | 谁写 | 说明 |
|---|---|---|---|
| `route` | `str` | `route_task` | `"direct"` / `"rag"`，出口指标与闸门的标签源 |
| `route_plan` | `dict` | `route_task` | §2.1 的 plan（**普通 dict**） |
| `route_source` | `str` | `route_task` | `"rule"` / `"cache"` / `"llm"` / `"escalated"` —— **观测"路由是怎么定的"** |
| `retrieval_stats` | `dict` | `retrieve` | `{hits, empty, degraded, elapsed_ms, context_chars}` |
| `exit_reason` | `str` | 各出口 | `"converged"` / `"budget_exhausted"` / `"not_needed"` / `"forced"` |

⚠️ **`init` 不要清 `route_plan`。** `init` 现在的职责是复位 `iteration` / `force_exit`
（其 docstring 明确"只读入参、不读任何字段"）；而 `route_plan` 需要**跨轮继承**（见 §3.4）。
LangGraph 的返回是合并语义，所以不在 `init` 里清就自然保留了。

### 3.3 四条支路的拓扑（P0 只实现前两条）

| 支路 | P0 | 拓扑 | 回边触发条件 | 停止条件 |
|---|---|---|---|---|
| direct | ✅ | `generate → END` | 无 | 单次即出 |
| rag | ✅ | `retrieve → generate → evaluate → route_review` | 分数落 `[LOW, HIGH)` | 分数 ≥ HIGH / `iteration` 用尽 / 转人工 |
| write | ⏳ 只留 `needs` 位 | 同 rag + 强制评审 + critic 独立输入 | 有新意见 | **连续两轮无新意见** |
| code | ⏳ 只留 `needs` 位 | + 沙箱执行节点 | 测试失败 | 测试全绿 / 超时 |

⚠️ **write 的 critic 输入必须不同于 generator**（不给检索上下文，或换模型/温度），否则是"自己给自己打分"。
⚠️ **code 的执行是独立安全域**：`guard` 只防 prompt 注入，防不了生成代码的破坏性操作 ——
沙箱、超时、文件作用域、网络开关缺一不可，**不要指望 guard**。

### 3.4 多轮继承

"帮我查 X" → 回答 → "再详细点"：**单看最后一条是 direct，实际必须继承上一轮的 `needs.retrieval`。**

规则：`route_task` 读 checkpoint 里上一轮的 `route_plan`，**继承 + 覆盖**——
本轮判据若明确给出结论则以本轮为准，否则继承。这就要求 §3.2 的"不清 `route_plan`"。

---

## 4. 现状评估：四个具体错配

| # | 位置 | 现状 | 后果 |
|---|---|---|---|
| 1 | `graph.retrieve` | 无条件执行 | 寒暄也付一次 embedding（外部 API，有网络+费用）+ 向量查询；且**首次请求就会加载整份语料** |
| 2 | `graph.evaluate` | 无条件执行 | 多一次 LLM 调用 + 结构化输出，只为判"要不要评审" |
| 3 | `graph.evaluate` 的产出时机 | 唯一的路由信号 `ReviewResult.is_need_evaluate` 产生在**付完检索与生成之后** | 顺序反了：该在入口判的事，拖到了出口 |
| 4 | `graph.route_review` | 升级人工只看 `score < LOW`，**缺风险维度** | "写首诗 55 分"也挤进 `review_queue`，把人工资源从真正需要人的场景上挤走 |

第 3 条是抓手：**把 `is_need_evaluate` 的判断提前到检索之前，它就从"事后跳过评审"升级成"事前选择支路"。**

---

## 5. 落地路线（每阶段一个 commit）

### P0 —— 最小可上线

**改动面**

| 文件 | 改什么 |
|---|---|
| `src/router.py`（新） | `decide(query, *, prev_plan=None, cached=None) -> dict`；纯标准库，**不 import `app`**（有 AST 守卫扫整个 `src/`） |
| `src/graph.py` | `OverAllState` 加 §3.2 字段；新增 `route_task` 节点；`route_tool` → `route_after_generate`；`build_graph` 加 1 节点 + 2 边 |
| `src/prompts.py` | **不动**。若日后要 L2，需 bump `PROMPT_VERSION`（`tests/test_prompts.py` 存指纹映射） |
| `app/config.py` | 新增 `ROUTER_*` 段（§6）+ `as_dict()` |
| `app/main.py` | SSE 收尾读 `values["route"]` / `route_source` / `retrieval_stats`，上报指标；`_thread_view` 加 `route` |
| `tests/test_router.py`（新） | 单元：L0 规则、fail-safe 默认、指纹归一化、继承覆盖 |
| `tests/test_graph.py` | 端到端：direct 不调 retrieve；rag 支路行为与改造前逐字段一致 |
| `tests/test_grafana_dashboards.py` | `_APP_PREFIXES` 加 `"route_"` / `"chat_"` —— **见下方⚠️** |
| `chatagent-overview.json` | 加一块路由分布面板（否则新指标没有可视化出口） |
| `deploy/monitoring/alerts.yml` | 4 条新告警（§7） |
| `PRODUCTION_CONFIG.md` / `CHANGELOG.md` / `deploy/RUNBOOK.md` | 计数与处置条目 |

⚠️ **`_APP_PREFIXES` 的守卫方向是单向的，这点很容易踩**：它只做
"面板里以已知前缀开头的标识符**必须**在代码里存在"，**不**做"代码产出的指标必须进前缀表"。
后果是：若加了 `route_total` 的面板却没把 `"route_"` 加进 `_APP_PREFIXES`，
那块面板**不受任何守卫保护** —— 写错一个字母（`rout_total`）Prometheus 不报错、
面板永远空白，**看起来和"最近没流量"一模一样**。
⇒ **只加指标不加面板**时不需改前缀表；**一旦加面板，必须同步加前缀**。

**守卫（必须能"删掉实现就变红"）**

1. `test_direct_route_never_touches_retriever` —— 用**计数替身**检索器，断言 `calls == 0`。
   ⚠️ 不是断言"context 为空"（那在检索失败时也为真 ⇒ 假绿）。
2. `test_rag_route_calls_retriever_exactly_once` —— 断言 `calls == 1`。
3. `test_unmatched_rule_falls_back_to_rag_not_direct` —— 钉住 §2.2 的 fail-safe 方向。
4. `test_state_fields_are_json_serializable` —— `route_plan` 只能是 dict（§2.1）。
5. `test_generate_prompt_is_not_modified_by_router` —— §1.1 的硬规则（路由器只读不写）。
6. `test_rag_branch_output_identical_to_pre_refactor` —— 逐字段回归，防"顺手改了 rag 支路"。

**变异组**（逐组超时、还原后字节相等）

| # | 变异 | 期望 |
|---|---|---|
| M1 | 删掉 L0 规则的返回分支 | 规则用例红 |
| M2 | 未命中时返回 `"direct"` | 守卫 3 红 |
| M3 | 把 direct 的条件边接回 `retrieve` | 守卫 1 红 |
| M4 | `route_plan` 用 pydantic 实例 | 守卫 4 红 |
| M5 | 路由器顺手 `strip()` 了 query 再传下游 | 守卫 5 红 |

**验收判据**：全量回归绿；direct 支路 **0 次 embedding 调用**；rag 支路与改造前**逐字段一致**。

### P1 —— 预算与退出原因

- `plan.budget` 替换 `MAX_ITERATION`（单一来源）；
- `exit_reason` 进 state + 指标 `chat_exit_reason_total{reason}`；
- 告警 `BudgetExhaustedFrequent`；
- 守卫：`test_exit_reason_distinguishes_converged_from_budget_exhausted`
  —— **这两个混在一起报，调优方向会完全错**（以为是 prompt 问题，其实是预算不够）。

### P2 —— 事后升级通道

- `retrieve` 空命中 → **不再"按无上下文继续"**，改为升级到澄清/明说"资料里没有"的出口；
- 事实断言检测（含具体数字/实体）→ 升级到检索支路；
- `route_source = "escalated"` + 指标 `route_escalated_total{from, to, reason}`。

⚠️ **升级必须一次性**：state 里放 `escalated` 标记，禁止 A→B→A。
⚠️ 空命中改语义会**改变现有行为**，需要单独评估（当前 `graph.retrieve` 的日志是
"RAG 未召回任何片段，按无上下文继续"）—— 这条是 P2 而不是 P0，正是因为它改的是**答案内容**。

### P3 —— 路由评测进 CI（与 #36 bench 门禁合流）

- `scripts/eval_set.json` 的 `queries` 加 `expect_route` 与 `route_cost`（错误代价权重）；
- `scripts/eval_routing.py`：输出**代价加权错误分 + 混淆矩阵**；
- 退码语义与 `eval_answer.py` 一致（**超阈值退 1 / 无基线退 2**）；
- 现有 `eval_set.json` 是 `{_comment, documents, queries}`，`queries` 28 条
  （`literal` 16 / `paraphrase` 12），每条形如 `{'q': ..., 'expect': [...], 'type': ...}`。

---

## 6. 配置项（走 `app/config.py`，进启动日志回显）

```ini
# 任务分流。默认开 —— 但"开着且判据不足"时行为必须与关一致（退回固定直链）。
RAG_ROUTER=1
# rule（L0 规则）| rule+cache（+ 指纹缓存）| rule+cache+llm（+ LLM 结构化，默认不选）
RAG_ROUTER_MODE=rule+cache
# 仅 rule+cache+llm 生效：置信度低于它才下沉到 L2
RAG_ROUTER_LLM_CONFIDENCE=0.7
# 指纹缓存条数上限。**必须有界** —— key 来自请求
RAG_ROUTER_CACHE_SIZE=512
# L0 规则：超过这个长度不可能是寒暄/续问
RAG_ROUTER_DIRECT_MAX_CHARS=40
# 各支路预算（替换 MAX_ITERATION）
RAG_BUDGET_DIRECT=0
RAG_BUDGET_RAG=3
RAG_BUDGET_WRITE=6
```

⚠️ **`RAG_ROUTER=0` 时，图必须与改造前逐字段一致**（含走 `retrieve`）——
这是"能力开关默认开、关掉即回到旧行为"的既有约定。开启但 `MODE` 判据未命中 ⇒ 走 `rag`（fail-safe）。

---

## 7. 指标与告警

**指标**（`app/metrics.py`，`observe` 复用既有 `_BUCKETS`）

| 指标 | 类型 | 回答什么 |
|---|---|---|
| `route_total{route, source}` | counter | 各支路占比，以及**路由是怎么定的**（`rule`/`cache`/`llm`/`escalated`） |
| `route_decision_duration_seconds` | histogram | 路由本身的开销（必须远小于它省下的检索开销） |
| `route_escalated_total{from, to}` | counter | 入口误判率 |
| `chat_exit_reason_total{reason}` | counter | 达标退出 vs 预算耗尽 |
| `rag_hits_total{result}` | counter | `hit` / `empty` —— **空命中率**（与 #37 一并做） |
| `rag_retrieval_degraded_total` | counter | 检索抛异常降级的次数 |

⚠️ `route` / `source` / `reason` 都是**有限取值**，适合做标签；**绝不要把 query 或来源放进标签**。

**告警**（35 → 39 条）

| 告警 | 表达式（示意） | 守什么 |
|---|---|---|
| `RouteDirectRateAbnormal` | direct 占比相对基线突增 | **路由退化成"全直答"= 检索被静默绕过** |
| `RouterFallbackHigh` | L2 占比过高 | L0/L1 判据失效（或 `MODE` 被误配） |
| `RouteEscalationLoop` | `increase(route_escalated_total[10m])` 异常 | 升级死循环（P2） |
| `BudgetExhaustedFrequent` | `increase(chat_exit_reason_total{reason="budget_exhausted"}[1h])` | 预算配得不合理（P1） |

⚠️ `RouteDirectRateAbnormal` 是这套体系里**最重要的一条**：它对应"检索整个挂掉但服务外观全绿"
这个本项目最贵的坑（5xx 不涨、延迟反而**变低**、答案照常返回）。

---

## 8. 风险与"不做的事"

**不做**

- 多智能体协商/互相评审（复杂度 >> 收益）；
- 模型选型层（只有 1 个对话模型 + 1 个 embedding 模型）；
- 异步批处理车道（交互式 SSE 不适用）；
- 让路由器改写 prompt（破坏 KV-cache，且让"模型实际看到什么"不可复现）；
- 多引擎同现：加支路时**不要**并行走两套（旧直链 + 新分流），那会让"线上跑的是哪条路"变成不可知。

**风险**

| 风险 | 缓解 |
|---|---|
| 多轮场景路由判错（"再详细点"） | 继承 + 覆盖（§3.4），并有专门用例 |
| 首次请求才加载语料变成"首次 rag 请求才加载" | 启动自检里显式预热（或接受，但要可见） |
| 缓存 key 被构造撑爆 | `RAG_ROUTER_CACHE_SIZE` 有界（同 `RAG_TENANT_CACHE_MAX` 的理由） |
| 加支路后出口闸门失配 | 加支路 = **出口闸门与指标标签必须重审**（§2.4），列为 checklist 项 |
| 路由指标基数膨胀 | 标签只允许有限取值；有测试钉住 |

---

## 9. 验收清单（→ `PRODUCTION_CONFIG.md` 自检清单第 36 项）

1. `/metrics` 里**有** `route_total`，且 `route` 标签只出现 `direct` / `rag`；
2. direct 支路确实**没有**触发检索（用 `rag_hits_total` 的缺失来佐证，或在日志里看无检索行）；
3. `RAG_ROUTER=0` 时行为与改造前一致（逐字段回归绿）；
4. `chat_exit_reason_total` 里 `converged` 与 `budget_exhausted` **分开可读**；
5. 四条新告警全部加载（`promtool check rules` / `yaml.safe_load` 通过），且计数文档同步；
6. `route_plan` 可 JSON 序列化；
7. 变异验证 5/5 按预期变红、实现字节还原；
8. 全量回归绿。
