# 方案：多步任务的检测 / 处理 / 兜底（2026-10-09）

> 状态：**第 1~4 步已落地**（2026-10-09：步骤记录 / 重复计数 / 反思闸门 / 三级升级的第 2 级），
> 第 5~7 步待做（兜底出口与第 3 级 · 结构化摘要 · 指标告警收口）。
> ✅ §8 的两处"必须先验证"**都已验完**（输入侧 `call_id` 在、响应侧形态确认）。
> 命名沿用项目惯例：新能力默认**可关**，关掉时行为与今天逐字节一致。

## 0. 要解决什么

"多步执行"要**分两个层次**看，不能笼统说成"整段是黑盒"：

* **节点之间（图这一层）**：**可见**。每个节点跑完整份落一份检查点，路由函数读状态字段
  决定下一条边；跨 agent 的"换人"就发生在这里（前台给出结构化判定 → 条件边跳到文学）。
* **一个 agent 节点内部**：**不可见**。它等于**一次完整的 agent 运行**，这次运行内部模型
  可来回最多 `MAX_TURNS`（默认 12）次模型调用，每次可能配一次工具执行。这些来回
  **图不介入、不落检查点** ⇒ 这一段内部**没有步骤边界**，退不回去。

⚠️ 两条链路的落点**不一样**，讲的时候必须分开：**兜底链路（rag）把工具调用画在图上了**
（`generate` ↔ `tool_node` 这个循环，退出上限 `MAX_ITERATION`）⇒ 那条链路的"步"在
**节点之间**、可见、有检查点；只有 **agent 链路**才是"步在节点内部"的那一条。
**本方案针对后者** —— 它要补的正是"节点内部看不到"这一段。

由此产生三个具体毛病：

1. **工具报错不可见**：官方会把异常转成一句话回给模型（模型能自愈），
   但本项目**没有记录任何成败** ⇒ 某个工具 30% 调用失败时，看板上呈现的是
   "工具次数正常、模型调用偏高"，指标会把人引向提示词，而真凶是工具实现。
2. **原地打转不可见**：在两个参数之间来回试，会安安静静烧完 `AGENT_MAX_TURNS`（默认 12）。
   现象是"这轮特别慢、特别贵"，从指标上看不出是打转。
3. **跑偏没有判据**：现有评审看的是"产出**好不好**"，不是"是不是在做**原来那件事**"。
   一份漂亮但答错问题的大纲会拿到高分。

本方案补上这三样，并给出纠正失败后的逐级处置。

## 1. 已拍板的决策

| # | 决策 | 含义 |
|---|---|---|
| 1 | 中间步骤可见性 = **钩子旁路记录** | 不动图拓扑；只在官方钩子上多接记录动作 |
| 2 | 反思 = **异步反思闸门** | 计数到阈值 ⇒ 发起异步反思 ⇒ 结论在下一次模型调用前注入 |
| 3 | 触发 = **多计数各设阈值** | 四个计数（见 §2.4），谁先到谁触发 |
| 4 | 纠正 = **三级升级** | ①注入反馈继续 ②退回出错前那一步 ③停止重试 |
| 5 | 兜底 = **按严重程度分两级** | 轻 ⇒ 已完成部分 + 未完成清单；重 ⇒ 交人工 |
| 6 | 反思迟到 = **收尾前等几秒** | 实现为"每次模型调用前最多等 X 秒"（见 §2.4 末） |

## 2. 三条硬约束（方案的形状由它们决定）

已逐条拉官方源码核实：

**约束一：唯一的「注入口」是模型调用之前那一步。**
`RunConfig.call_model_input_filter` 收到 `CallModelData`（有 `model_data` / `agent` /
`context` 三个字段，改写时前两个**必须原样传回**），出参是 `ModelInputData`。
钩子（`lifecycle.py` 里的 `on_llm_start` / `on_tool_start` / `on_tool_end` …）**没有返回值**，
只能观察，**不能改写**送进模型的内容。

**约束二：钩子与过滤器共享同一个上下文对象。**
钩子收到 `RunContextWrapper[TContext]`，它有个 `.context` 字段
（`run_context.py` 原文：*"The context object (or None), passed by you to `Runner.run()`"*）；
过滤器收到 `CallModelData.context`。**两者是同一个 `TaskContext`。**
⇒ 不需要另造全局容器，步骤记录与反思状态直接挂在 `TaskContext` 上。

**约束三：钩子拿不到参数、也拿不到异常。**
- `on_tool_start(ctx, agent, tool)` / `on_tool_end(ctx, agent, tool, result)`
  **都没有参数形参** ⇒ 工具入参只能从 `on_llm_end(ctx, agent, response)` 的
  `ModelResponse.output` 里取（里面含 `ResponseFunctionToolCall` 项，字段为
  `name` / `arguments` / `call_id`，见 `agents/items.py` 的 `ToolCallItemTypes`）。
- 工具的**异常**在钩子之前就已被 `failure_error_function` 转成了字符串
  ⇒ 只靠 `on_tool_end` 分不出成败，必须换掉默认实现（见 §2.2）。

**一个好消息**：`ResponseFunctionToolCall` 带 `call_id`，而工具结果项也带同一个 `call_id`
⇒ **"退回第 k 步"有稳定标识可定位，不必靠顺序假设。** 这是 §2.5 第 2 级能成立的前提。

## 3. 六个部件

### 3.1 步骤记录（挂 `TaskContext`）

在 `TaskContext` 上新增一个步骤清单，每个"动作"一条：

| 字段 | 来源 | 说明 |
|---|---|---|
| 序号 | 自增 | 从 1 开始，本次运行内单调 |
| 工具名 | `on_llm_end` 的函数调用项 | |
| 参数指纹 | 同一处，`arguments` | 规范化后（键排序）再做短哈希（见 §3.3） |
| `call_id` | 同一处 | **退回时的定位键** |
| 成败 | §3.2 的自定义错误处理函数 | `ok` / `failed` |
| 结果摘要 | `on_tool_end` 的 `result` | 截断保存（**不进日志正文**，与"日志不落原文"口径一致） |
| 耗时 | `on_tool_end` 前后取时钟 | 顺带补上"哪个工具慢" |

⚠️ **这不进 LangGraph 的 state**：`TaskContext` 本身不是 state（与 `AgentStats` 同一条）。
跨轮要保留的部分走 `CARRY_ACROSS_NODES`（见 §3.4 的跨节点计数）。

### 3.2 成败的分辨（换掉默认的错误处理函数）

官方默认（`agents/tool.py::default_tool_error_function`）只返回一句给模型看的话：
`"An error occurred while running the tool. Please try again. Error: ..."`
（参数 JSON 解析失败另有一句专门措辞）。**既不记录、也分不出是哪种失败。**

做法：写一个工厂，给每个工具统一装上自定义版本——
**既返回给模型的文本（保住自我修复），又记一条 `failed` 到步骤清单**。

两处可挂，都要覆盖：
- `function_tool(..., failure_error_function=...)`（`tool.py`）—— 检索、落盘这类普通工具；
- `Agent.as_tool(..., failure_error_function=...)`（`agent.py`）—— 专家子任务。

⚠️ 该回调的签名为 `(ctx: RunContextWrapper, error)`，**没有工具名形参**
⇒ 工具名要在**工厂里用闭包捕获**（构造工具时就知道名字），不要试图从回调参数里取。

⚠️ **为什么"只接钩子、不换错误处理函数"不行**：实测失败路径上 `on_tool_end`
**照样触发**，而它拿到的 `result` 就是那句错误文本 ⇒ 钩子侧分不出成败。
这是第一步实测出来的（见 §9 第 1 条），它决定了本部件**没有**更省事的做法。

### 3.3 动作指纹

`指纹 = 短哈希(工具名 + 规范化后的参数)`。规范化 = JSON 键排序 + 压掉分隔符空白
（`sort_keys=True` / `separators=(",", ":")`），确保 `{"a":1,"b":2}` 与
`{"b":2,"a":1}` 落到同一个值。用途只有一个：**发现"同一动作重复"**
（§3.4 四个计数里的 `repeat`）。不做跨请求持久化（只在本次运行内存活）。

✅ **已落地（2026-10-09，第二步）**。落点与口径：

- `steps.py::_fingerprint`；清单侧 `StepLog.repeat_counts()`（只列**做过两次以上**的
  `{指纹: 次数}`）与 `StepLog.max_repeat`（最多次数，反思闸门直接读它）。
- `_settle` 里**在设 `settled` 之前**判"这个指纹之前出现过没有"：设之后再判会把
  **自己**算进去 ⇒ 每一条都被判成重复，"打转"指标直接等于总调用数。
- 播报：同一动作**出现第二次起、每多一次播一次** `agent_action_repeats_total{tool}`。
  口径是整次运行内累计（不设窗口），且与 `repeat_counts()` **同源**（都只数已定格的
  步骤）⇒ 播报次数恒等于 `Σ(每个动作次数 − 1)`，两处不会各算各的。
- **跟随 `AGENT_TOOL_STEPS`**，没有独立开关 —— 指纹本来就在步骤清单里，重复次数只是
  它的派生量，两处分别维护一定会漂。
- ⚠️ 空指纹（没配到参数的步骤）**不参与**：它是"这一条没拿到参数"的记号，
  按同一个值归并会让几条互不相干的孤立步骤凭空变成"重复了三次"。

### 3.4 反思闸门

**触发（四个计数，各设阈值，谁先到谁触发）：**

| 计数 | 数什么 | 为什么需要 |
|---|---|---|
| `tools` | 全部工具调用次数（含检索这类轻量） | 最粗的"跑了多久"，兜底 |
| `heavy` | 重量级动作次数（专家调用、落盘） | 检索十次不算跑偏，专家五次很可能算 |
| `hops` | 跨节点转移次数（前台 → 文学） | 真正的"换角色"信号；**跨运行**，挂 `CARRY_ACROSS_NODES` |
| `repeat` | 同一指纹的重复次数 | **唯一能发现"打转"的判据**——打转时总次数不高、重量级动作也不多 |

⚠️ 专家是**以工具形式**（`as_tool`）挂上去的，不是真 handoff ⇒
`heavy` 数的是"调了几次专家工具"；真正跨节点的移动只有「前台 → 文学」一次，
且发生在**两次 run 之间**，所以要挂在跨节点字段上才能累计。

**发起（在钩子里，不阻塞）：**
计数到阈值 ⇒ `asyncio.create_task(...)` 发起反思，**不 await**。
⚠️ **必须持有 task 引用**（存进 `TaskContext`），否则可能被 GC ——
与 `app/main.py::_spawn_history_compaction` 里那条坑同源。

**反思本身**：一次旁路模型调用，复用 `compaction.py::_client_or_none` 那套
（同 `AGNES_MODEL` / `AGNES_API_KEY` / `AGNES_API_BASE`）。
⚠️ 走旁路 ⇒ **不计入 `AGENT_MAX_TURNS`，也不进 SDK tracing** ⇒ **必须自己设超时**
（`compaction.py::_summarize_timeout` 是现成先例）。

**反思的输入必须是结构化的步骤清单**（序号 / 动作 / 参数 / 结果 / 成败），
输出也必须是结构化的：**有没有问题、问题类型、涉及第几步 / 哪个 `call_id`、建议**。
⚠️ 输出里的"第几步"是第 2 级退回的定位依据 ⇒ **这一步不能是自由文本**，
要按固定字段解析（与 `evaluator.py` 里评审的结构化输出同一手法）。
解析失败按"没问题"处理（**不因为反思本身出错就把请求拦下来**）。

**注入（在 `call_model_input_filter` 里）：**
1. 看 `TaskContext` 有没有"待注入的反思结论"——有就追加进 `model_data.input`；
2. 看有没有"待执行的退回"——有就按 `call_id` 删条目（§3.5 第 2 级）；
3. ⚠️ **每个结论只注入一次**（注入后清标记），否则会每一轮都重复塞同一段，污染上下文；
4. 改写时 `CallModelData.agent` / `.context` **原样传回**。

**"收尾前等几秒"的实际表现**（决策 6）：因为唯一注入口是"模型调用之前"，
所以"等"只能表现为 **每次模型调用前最多等 X 秒**；判断"这次是不是最后一次调用"
在那个位置**看不到**。阈值定得早时绝大多数情况等 0 秒，真正会等到的只有收官那一次。
⚠️ 这条要写进配置注释，否则运维会以为它是"只在最后等"。

**✅ 已落地（2026-10-09，第三步）**

落点：`src/agents/reflection.py`（`maybe_reflect` / `_run` / `inject_reflections` /
`evidence_of` / `ReflectiveFinding` / `counts_of` / `_due`）、
`hooks.py::on_tool_end` 末尾接一次判定（它是"判决口"）、
`context.TaskContext.reflection`、`runner.py::build_run_config` 的
`call_model_input_filter` 由"一处"变"两步"（`trim_model_input` → `inject_reflections`）。

方案没写到、落地时必须自己定的三处（已拍板）：

| 没写到的点 | 定下来的口径 | 为什么 |
|---|---|---|
| 阈值过了之后**多久再看一眼** | 阈值整数倍（8 ⇒ 第 8、16、24 次） | "每次都触发" ⇒ 长任务里反思比主线还贵；"只触发一次" ⇒ 第四步判不出"同类问题又出现了" |
| **重量级**具体指哪些 | 叫专家（三个 `as_tool`）+ 落盘产出（`save_text_asset` / `generate_image`）；`kb_search` / `review_draft` 不算 | 检索本来就该多查几轮；评审是只读流程步骤，"产出→评审→改"本来要调几次 |
| 决策 6 的"等"**等到什么程度** | 每次模型调用前最多 2 秒（`AGENT_REFLECT_WAIT_SECONDS`），0 = 不等 | 位置决定了它只能这么实现（见上一段）；没赶上的计 `agent_reflection_late_total` |

⚠️ **两条容易写成"看起来能跑"的地方**（各有一条专属守卫）：

1. **注入排在裁剪之前**：症状是"提醒过、模型没照做"，而日志与指标里一切正常
   （提醒确实生成了、也确实进了输入，只是下一轮被当成老历史裁掉了）。
   用 `AGENT_HISTORY_TOKEN_LIMIT=1` 把顺序差异逼成**行为差异**：
   顺序反了 ⇒ 裁剪把提醒当成"最后一条无条件保留"，**用户那句被挤掉**。
2. **在飞任务没有强引用**：`asyncio.create_task` 的返回值不持有就会被 GC 掉，
   任务可能停在半个动作上（与 `app/main.py::_spawn_history_compaction` 同源）；
   而同一份集合还是 `_wait_for_pending` 的"该等谁"名单 ⇒ 少了它两件事一起坏。

守卫：`tests/test_agent_reflection.py`（本步 44 条；第 4 步之后全文件 **64** 条）；
变异验证 **本步那 18 组全部按预测抓到**（脚本 `tmp_probe/mutate_reflection.py`；
第 4 步在同一脚本里追加了 9 组，见 §3.5 末）
（阈值改成每次都发起 / 优先级反序 / 证据不筛选 / 证据无上限 / JSON 严格解析 /
未知取值当问题 / 解析失败当问题 / 注入排在裁剪前 / 注入不清队列 / 不持有任务引用 /
`hops` 从步骤清单取 / 重量级名单混进检索 / 总开关默认开 / 用 `>` 代替 `>=` /
提醒插在最前 / `instructions` 不原样传回 / 等待无上限 / **装配处摘掉裁剪**）。

⚠️ **守卫缺口是从变异验证里长出来的，不是想出来的**：

* **证据不筛选**（把 `flagged` 恒为真）与**装配处摘掉裁剪**（`trim_model_input` 从链上
  摘掉、只留注入）两种改法，在补守卫之前**一条测试都不会红** —— 原来的用例只断言
  "该在的在"，没断言"不该在的不在"，而"不裁了"与"不用裁"在用例里长得一模一样；
* **发起后不持有任务引用**同样全绿通过（只影响 GC 与"该等谁"，没有任何断言盯着它）。
* ⇒ 三条各补一条守卫：`test_evidence_leaves_out_ordinary_steps`、
  `test_in_flight_tasks_are_held_and_released`，以及把
  `tests/test_agents_pipeline.py::test_run_config_trims_by_tokens_and_omits_reasoning_ids`
  从"断言挂的是哪个函数"扩成**实跑一次证明裁剪仍在链上**。
* ⚠️ 另一处教训（M18 第一版）：变异要**只改想验的那一处**。第一版写成
  `return await reflection.inject_reflections(data, ...)`，把 `CallModelData` 原样返回 ⇒
  整组 pipeline 用例（32 条）一起红，但红的原因是"形状写错了"、**把想验的那条不变量
  盖住了**。改成只替换"裁剪那一次调用"后才是一次干净的验证。

### 3.5 三级升级

**第 1 级 · 注入反馈，继续跑。**
把反思结论塞进下一次模型调用，让模型自己改。
上限 `AGENT_REFLECT_MAX_LEVEL1` 次；**同一个问题只注入一次**（避免刷屏）。

**第 2 级 · 退回出错前那一步。**
把该动作（按 `call_id` 定位）**及其之后的所有条目**从送进模型的内容里删掉，
让模型在那一步之前重新决定。
⚠️ 语义要说清：**这是"上下文回滚"，不是"状态回滚"**——
- **拿得回**：一个干净的模型上下文；
- **拿不回**：已花的 token、**已落盘的副作用**（文件还在、库写入还在）；
- **且重来会重复副作用** ⇒ 必须配幂等（同一产出重复生成时只算一次）。
  落盘类工具（`tools.py` 里的资产产出）要按内容/路径去重，否则会得到两份一样的文件。
`history_budget` 的裁剪已经在做同类操作（删条目），所以这个口子是通的。
上限 `AGENT_REFLECT_MAX_LEVEL2` 次。

**第 3 级 · 停止重试。** 见 §3.6。

**"没纠正"怎么判**：步数继续累加 ⇒ 反思会再次触发 ⇒
若再次给出**同类问题**即判定未纠正，升级。判据是"问题类型相同"，不是"文本相似"。

**✅ 第 1 + 2 级已落地（2026-10-09，第 3、4 步）**

落地前先验掉的两个事实（方法见 `tmp_probe/probe_input_call_id.py`）：

| 事实 | 结论 | 决定了什么 |
|---|---|---|
| `call_id` 在**输入侧**（历史转换之后）还在不在 | ✅ 在 —— `function_call` 与 `function_call_output` 两条都带（方案 §8 第 1 条那个缺口，至此补齐） | 按 `call_id` 定位那一步是可行的 |
| 运行**中间**的条目形态 | ✅ 只有「用户消息 + 工具调用对」—— 助手文本条目 **0 条** | 撤回按 `call_id` 过滤就够，不会留下"一段没有来由的助手话" |

⚠️ 并由此发现一处**方案没写到、但决定实现形状**的事实：**注入口的改写是"本次调用专用"的**。
官方 `maybe_filter_model_input` 返回的 `ModelInputData` 只喂给这一次模型调用，而运行状态里的
条目还在、并且每轮之后又被回写（`run_internal/run_loop.py`：
`run_state._generated_items = list(streamed_result._model_input_items)`）
⇒ **撤回必须"持久"**：把撤掉的 `call_id` 记在本次运行里，之后每一轮都按它过滤。
只删那一次的表现是"下一轮那条动作又回来了"，而模型会以为自己没做过、很可能再调一次。

方案没写到、落地时必须自己定的四处（已拍板）：

| 没写到的点 | 定下来的口径 | 为什么 |
|---|---|---|
| 第 2 级**由第几次反思触发** | 同类问题出现到**第 3 次**（= 第 1 级上限 + 1） | 方案只写了"再次给出同类问题即升级"，没写它跟"最多注入 2 次"怎么共存；这样语义是"先提醒 N 次、还不行才撤"，两个数是推导关系 |
| 因此**发起上限**与注入上限的关系 | 拆开：`AGENT_REFLECT_MAX_FIRES`（默认 3 = 2 + 1） | 触发线本来就发生在注入额度**用光之后**，拿注入上限卡发起次数 ⇒ 第 2 级永远轮不到 |
| 拿不回已落盘的副作用怎么办 | **只撤没有副作用的动作**（白名单 `kb_search` / `research` / `review_draft`） | 撤回 = "让模型重做那一步"，撤一次落盘动作 = 两份产物。⚠️ 用白名单而非黑名单：黑名单下"新加一个工具"默认**能**被撤，而"它有没有副作用"只有当它真被撤一次、留下两份文件时才暴露 |
| 那一步已经不在输入里（被裁剪 / 压缩吃掉） | **当次不退回**，降级成一条提醒 + 计 `agent_recovery_skipped_total{reason="not_found"}` | 不放弃是对的，但"能力退化"本身必须看得见 —— 否则"退回老是不生效"与"一直没出问题"在看板上长得一模一样 |

⚠️ **两处会"安静地坏"的地方**（各配守卫）：

1. **撤回不持久**（只删这一次调用）—— 见上表那条事实，症状是"下一轮那条动作又回来了"。
2. **第 2 级被注入上限卡住** —— 触发线是"第 1 级上限 + 1"，它本来就在注入额度用光之后发生。

守卫：`tests/test_agent_reflection.py` 第八组（20 条，全文件 64 条）；
变异验证见 §3.4 末（第 4 步新增 9 组：撤回不持久 / 同类判据 / 白名单失效 / 定位失败也删 /
升级线写死 / 第 2 级吃注入额度 / 撤回说明丢关键句 / 发起上限用回注入上限 / 只删一条）。

### 3.6 兜底出口（两级，按严重程度）

> ⚠️ **本节口径已被 `PLAN_step5_fallback.md` §4 取代（2026-10-10）。** 原文留在这里只为
> 对照"为什么改"，**实现请照 §4 那张表**。最要紧的一处修正是：这里的"轻 / 重"把
> **问题性质**与**处置**揉成了一根轴 —— 实际上它们是两条互相垂直的轴：
> **问题性质**（要人来审 = 业务类 / 要改代码 = 系统类）决定"该让谁来看"，
> **处置**（停止 / 转人工 / 交什么）是另一条，**可以叠加**。
> ⇒ 所以 **"停止" ≠ "不交东西"**：系统类的问题要停，但那次运行**已经产出的内容照交**，
> 只标明"这是部分结果"；确实什么都没产出才是 `failed`。
> 最终是**四档**：`delivered` / `partial` / `manual` / `failed`（与指标标签同名同值）。

| 程度 | `outcome` | 交付什么 |
|---|---|---|
| 轻（有产出、只是没全做完） | `partial` | **已完成部分 + 未完成步骤清单**，并标明这是部分结果 |
| 重（跑偏 / 打转 / 反复失败） | `manual` | 不交半成品，**移交人工审核** |

⚠️ 移交人工要**复用现有通道**（`review_queue` + `human_review` 节点那套），
不要另造一个队列；触发条件从"评审分数低"**扩成**"分数低 **或** 收到兜底标记"。
⚠️ "未完成步骤清单"直接取自 §3.1 的步骤记录（把 `ok` 之外的列出来），
不要另写一份描述——两处表述会漂。

## 4. 与历史压缩的衔接

你在最早那条需求里说"压缩时保留**结构性摘要**（步骤编号、动作、关键结果、是否成功）"。
现状是**明确冲突**的：`compaction.py::_SUMMARY_PROMPT` 原文要求
*"直接输出摘要正文，**不要标题、不要前后缀**"*、控制在 400 字以内——**刻意是自由文本**。

但更要紧的是下一层：**这些信息根本不在对话里**（工具成败与参数在运行内部就没了）。
⇒ **"结构化摘要"与"检测"是同一个前置条件**：
必须先有 §3.1 的步骤记录，压缩才谈得上"保留结构"。

落地时的做法：压缩段的摘要**分两段拼**——
① 自由文本摘要（现有能力，管"聊了什么"）；
② 结构化步骤表（新增，管"做了什么、成没成"），从步骤记录直接序列化。
两者都放在那条 `system` 摘要里（图状态侧是 `SystemMessage`）。
⚠️ 结构化表要有条数上限（避免它自己变成新的膨胀源）。

## 5. 指标

**新增**（补上 2026-10-09 盘出的两个缺口）：

| 指标 | 类型 | 标签 | 用途 |
|---|---|---|---|
| `agent_tool_result_total` | counter | `tool` / `status` | **工具正确率**（缺口一）✅ 第一步已落地 |
| `agent_tool_duration_seconds` | histogram | `tool` | 哪个工具慢 ✅ 第一步已落地 |
| `agent_action_repeats_total` | counter | `tool` | 打转的直接信号 ✅ 第二步已落地 |
| `agent_reflections_total` | counter | `trigger` / `verdict` | `trigger` ∈ tools/heavy/hops/repeat；`verdict` ∈ ok/problem ✅ 第三步已落地（调用**失败**时记在 `trigger="error"` 上，与"反思跑完了但结论是 ok"区分开） |
| `agent_reflection_duration_seconds` | histogram | | 反思自己多慢 ✅ 第三步已落地 |
| `agent_reflection_timeout_total` | counter | | 反思没跑完 ✅ 第三步已落地（超时单独计数：它指向**配置**，与"网关抖动"处置不同） |
| `agent_reflection_late_total` | counter | | 结论没赶上（白跑）✅ 第三步已落地（它在涨 ⇒ `AGENT_REFLECT_TIMEOUT` 或阈值配得不合适） |
| `agent_recovery_level_total` | counter | `level` | 1 / 2 / 3 各发生几次 ✅ 第 3、4、5 步已全部落地（`1` = 注入提醒、`2` = 撤回那一步、`3` = 踩刹车中断本次运行）。⚠️ `3` 是在**抛出那一下**计的、不是在"判定出来"那一下 —— 判定 ≠ 真的停成了（判定发生时运行可能已经没有下一次模型调用了） |
| `agent_recovery_skipped_total` | counter | `reason` | **到线了却没撤成**：`not_found`（那一步已被裁剪/压缩吃掉）/ `side_effect`（动作有副作用、白名单外）/ `capped`（额度用完）。⚠️ 它在涨说明"问题还在、但已经没得退了" ⇒ 该调额度，或该上第 3 级 ✅ 第 4 步已落地（这三个值现在**各对应一个第 3 级停因**，见 `PLAN_step5_fallback.md` §5） |
| `agent_task_outcome_total` | counter | `outcome` / `agent` | **任务完成率**（缺口二）：delivered / partial / manual / failed ✅ 第 5 步已落地。⚠️ 标签值就是代码里那四个常量（同名同值 ⇒ 看板上"完成率掉了"能直接对到判定，中间不过一次翻译） |
| `agent_fallback_total` | counter | `reason` / `agent` | 兜底各类原因各几次（`reason` 是**有限枚举**：none / error / off_track / review_failed / guardrail / no_output / loop / stuck / max_turns / guard_stop）✅ 第 5 步已落地 |

⚠️ **两个 `agent` 标签是硬约定、不是风格**：一次请求 = N 次运行（转交那一轮是
前台 + 文学 2 次），少了它按 `outcome` 聚合的看板会把"运行数"当"请求数"，阈值随之失真
—— 而没有任何东西会变红。`error_kind` **绝不进标签**（异常原文带 request id、上游返回体、
可能含用户数据），它只进日志。

⚠️ **除 `agent_task_outcome_total` / `agent_fallback_total` 外，其余只看板不告警**：告警
（`AgentToolFailureRateHigh` / `AgentLoopDetected` / `AgentRecoveryLevel3Frequent`）排在
§7 第 7 步，与其余指标、配置、文档一起收口 —— 在那之前它们只是"有数据、没人盯"。

⚠️ 沿用现有口径约束：`agent` 组序列**只在真走 agent 链路时才有**（没流量时序列不存在，
而不是报 0）；按 `exit_reason` 聚合的看板**必须带 `agent` 标签**。

**告警**（新增，与 `alerts.yml` 的 `chatagent-agent` 组并列）：
`AgentToolFailureRateHigh`（某工具失败率过高）、`AgentLoopDetected`（指纹重复频繁）、
`AgentRecoveryLevel3Frequent`（兜底频繁发生 = 反思机制没能救回来）。

## 6. 配置项

除 `AGENT_TOOL_STEPS` 外全部默认**关闭 / 保守**，关掉时行为与今天一致。

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_TOOL_STEPS` | `1` | 第一 + 二步的开关：步骤记录 / 成败分辨 / **重复计数**。**默认开**，理由见下 |
| `AGENT_MULTISTEP_GUARD` | `0` | 总开关 |
| `AGENT_REFLECT_AFTER_TOOLS` | `8` | 工具调用数阈值 |
| `AGENT_REFLECT_AFTER_HEAVY` | `3` | 重量级动作阈值 |
| `AGENT_REFLECT_AFTER_HOPS` | `2` | 跨节点转移阈值 |
| `AGENT_REFLECT_ON_REPEAT` | `3` | 同一指纹重复几次触发 |
| `AGENT_REFLECT_MAX_LEVEL1` | `2` | 第 1 级最多注入几次 |
| `AGENT_REFLECT_MAX_FIRES` | `3` | 一次运行里最多**发起**几次反思。⚠️ 必须 ≥ 两级额度之和（默认 3 = 2 + 1），否则第 2 级永远轮不到 |
| `AGENT_REFLECT_MAX_LEVEL2` | `1` | 第 2 级最多退回几次（`0` = 只提醒不撤） |
| `AGENT_REFLECT_WAIT_SECONDS` | `2` | 每次模型调用前最多等几秒（见 §3.4 末） |
| `AGENT_REFLECT_TIMEOUT` | `20` | 反思自己的超时（**必设**，见 §3.4） |

⚠️ `AGENT_TOOL_STEPS` 是"全部默认关闭"的**例外**，且这个例外是有意的：
它是**纯观察** —— 不改任何对外可见行为（回给模型的错误文本仍由官方实现产出、
指标走旁路、记录失败只降级为日志），所以"关掉时与今天一致"它天然满足；
而**默认关掉等于这个指标永远没有数据**，那正是要修的缺口一。
第二步的**重复计数跟随它、没有独立开关**（理由见 §3.3 末）：同一个"纯观察"里再切一刀，
只会多出一种"记录开着但重复数没开"的组合，而那种组合下看板会显示"没有打转"——
比不显示更糟。

`src/` 侧**直读 `os.getenv`**（与 `session.py` / `compaction.py` 一致，避免 `src/ → app/` 反向依赖）；
数字用 `src/envutil.py` 的 `env_int/env_float`（带上下界夹取）。
`.env.example` 与 `PRODUCTION_CONFIG.md` 同步。

✅ **已落地**：`AGENT_TOOL_STEPS`（第 1~2 步）+ `AGENT_MULTISTEP_GUARD` /
`AGENT_REFLECT_AFTER_TOOLS` / `AGENT_REFLECT_AFTER_HEAVY` / `AGENT_REFLECT_AFTER_HOPS` /
`AGENT_REFLECT_ON_REPEAT` / `AGENT_REFLECT_MAX_LEVEL1` / `AGENT_REFLECT_TIMEOUT` /
`AGENT_REFLECT_WAIT_SECONDS`（第 3 步）+ `AGENT_REFLECT_MAX_FIRES` /
`AGENT_REFLECT_MAX_LEVEL2`（第 4 步）—— 共 10 个键已进 `.env.example` 与运维文档。
⚠️ **第 5 步没有新增任何键**（有意如此）：判定阈值、"未完成清单"条数上限都是代码里的常量。
⭐ 可调的是**两个既有键**：`AGENT_REFLECT_MAX_LEVEL2=0` 会让每一个到线的同类问题都落进
`capped` ⇒ 第 3 级**每次都会停**（组合错了就是"一发现问题就中断"，而它看起来只是"额度配小了"）；
反过来把 `AGENT_REFLECT_MAX_FIRES` 配到 ≥ 两级额度之和，才看得到 `not_fixed` 这个停因。
⏳ 未落地：无（本表已全部落地）。

## 7. 落地顺序

1. **步骤记录 + 成败分辨**（§3.1 / §3.2）—— ✅ **已完成**（2026-10-09）。
   落点：`src/agents/steps.py`（`StepLog` / `ToolStep` / `calls_from_response` /
   `tool_error_handler` / `steps_enabled`）、`hooks.py` 新增 `on_llm_end` 与
   `on_tool_end`、`context.TaskContext.steps`、**七处接线**（3 个 `function_tool` +
   3 个专家 `as_tool` + 评审 `as_tool`；`render_slide_deck` 刻意不装，它不是工具）。
   先只记录、只播报 `agent_tool_result_total`，**不开反思闸门**。
   这一步就能补上"工具正确率"那个缺口，且单独可用。
   守卫：`tests/test_agent_tool_steps.py`（35 条）；变异验证 **8 组全部按预测抓到**。
   ⚠️ **§8 的两处"必须先验证"在这一步顺带完成**：要建步骤记录，本来就要读
   `on_llm_end` 的函数调用项，把它的原始形态打一行日志即可 —— **不必等到第 4 步**。
   若发现 `call_id` 在拼好的输入里丢了，第 4 步的形态要改，而那时前面三步已经能用。
   **实测结论见 §8 开头（第 1 条只验到一半，第 2 条已验证）。**
2. **动作指纹 + 重复计数**（§3.3）—— ✅ **已完成**（2026-10-09）。
   落点：`steps.py::StepLog.repeat_counts()` / `max_repeat`；`_settle` 里在设 `settled`
   **之前**判重复并播 `agent_action_repeats_total{tool}`。**跟随 `AGENT_TOOL_STEPS`**，
   不引入独立开关。口径：整次运行内累计、同一动作出现第二次起**每多一次播一次**、
   空指纹不参与。
   守卫：`tests/test_agent_tool_steps.py` 第九组（11 条）；变异验证 **8 组全部按预测抓到**
   （每一条都判重复 / 判定放在 `settled` 之后 / 丢指纹判据 / 丢空指纹判据 / 阈值放宽 /
   指纹不含工具名 / 指纹不排序 / `max_repeat` 数成"有几个动作重复"）。
   下一步（反思闸门）直接读 `max_repeat` 与 `repeat_counts()` 判"该不该介入"。
3. **反思闸门**（§3.4）—— 触发、异步、结构化输出、注入。
   先只做"注入"，不做退回。
   ✅ **已完成**（2026-10-09）。落点：新建 `src/agents/reflection.py`、
   `context.TaskContext.reflection`、`hooks.py::on_tool_end` 接判决、
   `runner.py` 新增 `model_input_filter`（裁剪 → 注入**两步**）。
   `.env.example` 与 `PRODUCTION_CONFIG.md` 已同步 8 个键。
   守卫：`tests/test_agent_reflection.py`（44 条）；变异验证 **18 组全部按预测抓到**
   （明细与三组"变异验证才发现"的守卫缺口见 §3.4 末）。
   ⚠️ 这一步**刻意不碰**第 2 级退回：它的前提（`call_id` 在**输入侧**是否还在）
   仍未验证，见 §8 第 1 条 —— 动手前必须先补。
4. **三级升级**（§3.5）—— 第 2 级退回需要 `call_id` 定位，见下一条。
   ✅ **第 2 级已完成**（2026-10-09）。落点：`reflection.py` 的 `_REVERTABLE_TOOLS` /
   `max_level2` / `_max_fires` / `_level2_line` / `_attach_step` / `_revert_decision` /
   `_apply_revert` / `_locate` / `_drop_revoked` / `ReflectionState.revoked`，
   以及注入口里"滤掉已撤回的 → 执行撤回 → 追加说明"三段。两处口径见 §3.5 的表。
   守卫：`tests/test_agent_reflection.py` 第八组（20 条，全文件 64 条）；
   变异验证 9 组全部按预测抓到。
   ✅ **第 3 级随第 5 步落地**（2026-10-10，见下条）：到线却没撤成时不再"降级成提醒"，
   而是**停止本次运行**；`agent_recovery_skipped_total` 在涨 = 该上第 3 级了。
5. **兜底出口**（§3.6）—— 接现有审核通道。
   ✅ **已完成**（2026-10-10）。落点：新建 `src/agents/fallback.py`（`decide` 七行判定表 /
   `report` 唯一上报点 / `_unfinished` 未完成清单）、`reflection.py` 的第 3 级
   （`AgentStoppedByGuard` / `_level3_reason` / `_raise_stopped`）、`runner.py` 的
   **专有捕获**（必须排在 `except Exception` 之前）与判定调用、`graph.py` 的两处收尾
   （`_verdict_state` / `_verdict_fields` / `_with_partial_notice`）、`app/main.py`
   的转人工判定**提前**（否则待审草稿会先流给用户）与 `task_outcome` 事件。
   方案与全部口径：`PLAN_step5_fallback.md`（§1 两条垂直的轴 / §4 判定表 / §5 第 3 级）。
   守卫：`tests/test_agent_fallback.py`（33 条）；`tests/test_agent_reflection.py` 补 3 条
   第 3 级判定；变异验证 46 组（新增 19 组）。
   ⚠️ 这一步**推翻了 §3.6 的"轻 / 重两级"**（那是把两条轴揉成了一根），最终是**四档**。
6. **结构化摘要**（§4）—— 与压缩合并。
7. 指标 / 告警 / 配置 / 文档 / 守卫 —— **收口**。
   ⚠️ 含 `CHANGELOG.md` 的 `[Unreleased]` 条目：前几步落地时**刻意都没单独补**，
   避免同一个功能在变更日志里记成半截（"记了指标没记开关"那种）。
   第 5 步落地时同样**刻意没补** `CHANGELOG.md` —— 与前面一致，留到这一步一起写。

## 8. 风险与落地时必须先验证的点

**必须先验证（不验证就动手可能白做）：**

1. **`call_id` 在拼好的模型输入里是否还在。** §3.5 第 2 级的定位全靠它。
   拼输入的形状是 `prepared = converted_history + new_input_list`，
   要确认工具结果条目在转换后仍带 `call_id`（对话补全接口那边可能映射成
   `tool_call_id`）。**这一步要在真实运行里打一次日志确认**，不能靠推断。
   ✅ **2026-10-09 已全部验完**（`tmp_probe/probe_input_call_id.py`）：
   **响应侧**带 `call_id`（见下条）；**输入侧也带** —— 第 2 次模型调用收到的条目是
   `{"id":..,"type":"function_call","call_id":..,"name":..,"arguments":..}` 与
   `{"type":"function_call_output","call_id":..,"output":..}`，两条都有。
   ⚠️ 顺带验出的两条形态事实（都影响实现形状，方案原文没写）：
   ① 顺序是**时间序**（用户消息 → 调用 → 结果）⇒ "从那一步删到末尾"是可做的；
   ② 运行**中间**的条目只有「用户消息 + 工具调用对」，**助手文本条目 0 条**
   ⇒ 按 `call_id` 过滤就够，不会留下"一段没有来由的助手话"。
   ⚠️ **仍未验证**的是**真实网关那一层**（对话补全接口自己的转换函数）——
   探针用的是按官方 item 类型构造的假模型。所以实现里不假设"输入侧一定有 `call_id`"：
   定位不到 ⇒ 计 `agent_recovery_skipped_total{reason="not_found"}` + **第 3 级停止**
   （口径见 §8 第 6 条，2026-10-10 已从"降级成提醒"改成"停止"）。
2. **`on_llm_end` 的 `response.output` 里，函数调用项的形态。**
   已确认类型存在（`ResponseFunctionToolCall`，带 `name` / `arguments` / `call_id`），
   但**在当前网关（对话补全接口）下是否原样保留**需要实测。
   ✅ **2026-10-09 已验证（`Runner.run` 这条非流式链路）**：函数调用项**同时带**
   `name` / `arguments` / `call_id` 三个属性，`call_id` 原样落到清单里。
   验证方式：用按官方 item 类型构造的**假模型**走完整 `run_agent` 链路
   （`tmp_probe/probe_steps_e2e.py` 与 `tests/test_agent_tool_steps.py`）。
   ⚠️ **仍未验证**的是**真实网关那一层**（对话补全接口的转换函数）—— 所以实现里刻意
   用**属性存在性**判据（有 `name` 且 `arguments` 不是 None）而**不是** `isinstance`：
   按类型判一旦形态有差异，表现是"一步都没记上"，且不报错、无指标。

**已知风险：**

3. **重来会重复副作用**（§3.5）—— 落盘类工具必须有幂等，否则退回一次就多一份文件。
4. **反思是额外成本** —— 每次反思 = 一次模型调用，且不计入步数上限
   ⇒ 极端情况下"反思比主线还贵"。要有开关与上限。
5. **迟到的结论白跑**（§3.4）—— 属可接受损失，但要可计数（`agent_reflection_late_total`）。
6. **与历史压缩的相互作用** —— 压缩会把老历史换成摘要，而退回要删的是**具体条目**；
   若那一步已被压缩吃掉，退回就**无从定位**。
   ✅ **2026-10-09 定口径**（第 4 步）：定位不到 ⇒ 计
   `agent_recovery_skipped_total{reason="not_found"}`。
   刻意没选方案里建议的"直接放弃、不计数"：那样"退回老是不生效"只能靠翻日志，
   与"一直没出问题"在看板上完全一样；而选"删到最近一条还在的为止"会让撤掉的
   不是真正出问题那一步，语义变模糊。同理，token 裁剪（`history_budget`）也会吃掉那一步，
   两条路径都落在同一个判据上。
   ⚠️ **2026-10-10 改口径**（第 5 步）：定位不到**不再"当次不退回、降级成一条提醒"**，
   而是**第 3 级：停止本次运行**。理由是分类 ——「该退却退不了」（找不到 / 那一步不可撤 /
   额度用完）归**系统类**，系统类的处置就是"停止 + 给开发信号"；降级成提醒等于**让它
   悄悄过去**（模型照样在同一个地方打转，只是我们不再管它了）。
7. **跨副本**：步骤记录在进程内，多副本下各算各的（与现有指标同一局限，`SLO.md` 已写明）。

## 9. 第一步实测得到的两条形态事实（原方案没写到，但决定了实现形状）

1. **失败路径上 `on_tool_end` 照样触发。** 官方**先**把异常转成"给模型看的那句话"
   （`run_internal/tool_execution.py`），钩子拿到的 `result` 就是那句话
   ⇒ **从钩子侧分不出"真的成功了"与"收到一句错误文本"**。
   所以 §3.2 换掉默认错误处理函数不是"顺手多记一份"，而是**唯一**能分辨成败的位置：
   "只接钩子、不换错误处理函数"这个看起来更省事的做法**不成立**。

2. **参数不是合法 JSON 时，`on_tool_start` 也已经触发**（实测 `stats.tool_calls` 照样 +1）。
   ⇒ 失败路径在清单上是 `begin → fail → finish` **三连**，因此 `finish` 只能
   "收尾**尚未定格**的那一条"：改成"找不到就补记一条"就会给每次失败**补出一条假的
   成功**，工具失败率被自己抹平，而症状是"指标正常"，没有任何东西会报错。
   守卫：`test_fail_then_finish_stays_failed`、
   `test_a_failed_step_is_not_finished_into_a_new_ok_step`。
