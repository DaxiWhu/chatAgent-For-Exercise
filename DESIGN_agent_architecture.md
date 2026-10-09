# 智能体架构设计（按 OpenAI Agents SDK 官方原语）

> 状态：**设计已两次修订**（2026-10-02 / 2026-10-03）。本页描述的是**最新**的设计。
> 参考实现：`examples/sub_agents/`（53 条断言 + 6 组变异全绿，但=**两次修订之前**的决策）
> 生产实现：`src/agents/` + `src/graph.py`（**已按最新决策落地**）
> 取代：`PLAN_task_routing.md`、`examples/task_routing/`（已归档到 `examples/_archive/`）
> 取证基线：`openai-agents`（0.22.3 与 0.20.0 两代都读过源码）；官方示例源码逐文件读过

### ⚠️ 2026-10-03 修订（只改了一件事，但它改变了拓扑的形状）

| # | 修订前 | 修订后（现在） | 落点 |
|---|---|---|---|
| 4 | agent 链路是**图里的一个节点**（`synthesize`）：前台与专家的切换全在 SDK 内部完成（`handoffs` 工具 + `continue`），检查点看不见 | agent 链路拆成**图里的两个节点**：`frontline` 与 `literary`。"转交"从**SDK 的 handoff 工具**换成**结构化判定 + 图的一条条件边** | `src/graph.py`、`src/agents/` |

三条配套结论（每一条都有守卫，见 §6.3）：

1. **agent 节点内不许有 handoff。** SDK 的 `handoff` 在同一个节点内部换人后 `continue`
   （`run.py` 原话 "we run the loop again, with the new agent"）⇒ 节点里一带，
   节点边界就消失了，"拆成两个节点"等于没拆。所以 `handoffs` 被整条去掉，
   跨节点转移**只能**由图的一条边表达。
2. **"要转交"这个判定必须落进 state。** 前台用 `output_type=FrontlineDecision`
   （`action: "reply" | "transfer"` + `content`），条件边读 state 里的
   `agent_decision` —— 这样检查点里看得见"为什么跳到了下一个节点"。
3. **一次请求 = N 次运行**（转交那一轮是前台 + 文学 2 次）。由此带来一批必须跟着改的
   口径：指标分**请求口径 / 运行口径**（见 `deploy/RUNBOOK.md` §3.6）、
   每个节点各建一份 `TaskContext` 所以必须从 state 续引用与统计、
   异常**必须抛出**而不是在节点里兜住（否则检查点写着"没有待执行节点"、接口报
   `completed`、客户端连"继续生成"都不弹）。

**这条回程边被明确丢掉**："文学专家在本轮内转回前台"。理由：入口已经固定为前台，
下一轮用户说话自然回到前台；加回程边会让图出现环，还要额外配一个打转上限。
⇒ 文学节点**没有**出边回到 `frontline`（它的条件边只通向收尾 / 人工复核）。

**变异验证：11 组破坏，全部按预测变红**（脚本 `tmp_probe/mutate_split_v2.py`，带
预检 + `newline=""` 写回 + SHA-256 自证还原）。其中抓出一处**假绿**，值得记下来：

> `test_agent_run_metrics_report_tool_calls` 里那条"标签必须真的传到位"的断言，
> **直接调** `_report_run_metrics(...)` 并自己传 `agent="frontline_agent"` ——
> 它只证明"函数会把参数原样播报"。把调用处
> `agent=getattr(agent, "name", "")` 改成 `agent=""`，**它照样绿**。
> 补上 `test_each_run_reports_its_own_agent_label`（跑一次真实运行、看指标出口
> 实际收到什么；并且同一个函数换 agent 时标签必须跟着换）之后，那组变异才真的被抓。
> ⇒ 教训：**断言落在"函数的参数"上，等于没断言调用方**。

### ⚠️ 2026-10-02 修订（本页有两份实现，别串了）

用户指出两处**偏离官方做法**，本页已按修订后的决策改写；同一天内第 1 条又回收了一次
（原修订本身也是偏离，见下）：

| # | 修订前（本页旧结论） | 修订后（现在） | 落点 |
|---|---|---|---|
| 1 | 分流用**自写正则/词表**判"是不是创作类" | **入口不做任何内容分诊**：官方 `examples/customer_service` 的入口 agent **自己就是分诊者**（同一个 agent 既带 tools 又带 handoffs）⇒ 代码里不该存在"先分类、再分派"的第二层 | 仅 `src/` |
| 2 | 评审=**代码 `while` 每轮必跑**（`MAX_ATTEMPTS` 兜底） | 评审=**`as_tool`，模型自己决定调不调** | 仅 `src/` |
| 3 | 入口**跟随上一轮结束点**（`entry_agent_for`，照抄官方 `current_agent = result.last_agent`） | 入口**每轮固定为前台**；跨轮历史交给官方 `Session`（`Runner.run(session=...)`） | 仅 `src/` |

⚠️ 第 1 条**走过一个来回**，两版都别再走：

1. 最早用三条正则判"是不是创作类" —— 把路由降级成自定义词表；
2. 中途换成 `triage_agent` 出 `output_type` 结构化判定（`RouteDecision`）—— 判据确实
   交给了模型，但**在入口 agent 之外多插了一层"先分类、再分派"**。官方不需要这一层：
   入口 agent 自己就能兼任分诊（它同时带 tools 与 handoffs，"直接答 / 叫谁"由它当场决定）。

第 2 版的落点已全部删除：`src/agents/router.py`（留档 `examples/_archive/router_triage/`）、
`RouteDecision` / `CREATIVE_KINDS` / `classify` / `triage_agent` 的公开导出、
`classify_failed` 这一档 reason、`agent_classify_errors_total` 指标。
`graph.dispatch` 因此**回到同步**（一次模型调用都不花），RAG 链路降级为**兜底**。

⚠️ **第 3 条是"有意不照抄官方"的一处**（唯一一处，理由见 §6.2）：官方两个多轮示例都把
"谁接下一轮"交给上一轮的结束点，本项目改成每轮固定回前台。同时补上了官方示例里
**另一半**跨轮载体 —— 历史（`examples/*/main.py` 的 `input_items = result.to_input_list()`）。
`entry_agent_for` / `_ENTRY_AGENTS` / `DEFAULT_ENTRY_AGENT` 已删，换成常量 `ENTRY_AGENT`；
state 的 `current_agent` 仍在写，但降级为**纯观测**。

⚠️ **两份实现当前不一致**：

* `src/agents/` —— 已落地修订后的决策（本页 §4/§5/§6.5/§7/§9 描述的就是它）。
* `examples/sub_agents/` —— **仍是修订前**：`runner.py` 还在 `for attempt in range(MAX_ATTEMPTS)`，
  `evaluator.py` 的注释还在论证「评审**不能**接成 `as_tool`」（那正是被推翻的结论）；
  文学仍走 `handoff`（2026-10-03 已改成图的一条边）。
  **尚未同步**（已记入 §8 已知缺口）。
  ⇒ 读 `examples/` 时请**只当历史参考**，判据以本页与 `src/agents/` 为准。

---

## 1. 为什么把之前的方案全部换掉

之前的方案（预定义四条支路 + 条件边）不是写错了，是**范式选错了**。四点：

| # | 问题 | 结论 |
|---|---|---|
| 1 | 它是 **workflow 编排**（路径预先定义、模型只填内容），不是 agent | 对同质任务没错，但把它当"通用 agent"讲是错的 |
| 2 | `task` 单值决定走向，与"能力可叠加"自相矛盾 | 等于把标签偷偷加回来了 |
| 3 | 四条支路的维度不统一（"要不要外部知识" vs "要不要迭代"） | 两个正交维度硬凑成四个并列槽位 |
| 4 | 四条支路共享约 80% 的节点（`generate` 被三条共用） | 「支路」这个抽象本身多余 |

而 OpenAI 官方在文档里**直接批评了这类做法**：declarative 框架要求开发者"预先用节点+边显式定义
每个分支、循环与条件……**随 workflow 变复杂会迅速变得笨重**"，因此 SDK 选择 code-first。

⇒ 替换方向：**一条 loop + 少量强原语 + 少量 agent**，把不确定性交回模型（handoff / 工具选择 /
**结构化判定**），把**必须确定性**的部分交给代码（护栏、渲染、落盘）。

⚠️ 「评估循环」这四个字**已在 2026-10-02 修订中被划掉**（见文件头的修订表）：
评审从"代码确定性的一环"改成"模型可选的工具"。判断某个环节该由谁拿主意的判据见 §4。

---

## 2. 官方原语：只有五个，且只有一条执行链路

| 原语 | 作用 | 备注 |
|---|---|---|
| `Agent` | 无状态配置对象：`instructions + tools + handoffs + output_type + guardrails` | 同一实例可并发复用于多个 run |
| `Runner` | 唯一执行入口，`Runner.run(agent, input, ...)` 跑 **一条 loop** | 循环出口只有三个：final output / tool call / handoff |
| `Tool` | 普通函数工具 / hosted 工具 / **`Agent.as_tool()`** | `as_tool()` 的嵌套 run 是一次完整的 run |
| `Handoff` | 控制权转移 | **本质是"换 active agent"，不是"执行一个任务"** |
| `Guardrail` | `@input_guardrail` / `@output_guardrail` | **校验的官方位置**，独立于 agent |

关键事实（源码核实，`agents/models/interface.py`）：

```python
async def get_response(
    system_instructions, input, model_settings,
    tools, output_schema, handoffs, tracing,
    *, previous_response_id, conversation_id, prompt,
) -> ModelResponse
```

⇒ **`handoffs` 是独立形参，不混进 `tools`**。这一点与"handoff 底层就是一个工具"的说法需要分层理解：
在模型实现层（如 `OpenAIResponsesModel`）它会被翻译成一份可调用清单发给模型；
但在 `Model` 抽象边界上，`tools` 与 `handoffs` 是**两个不同的形参**，
且 `handoff()` 选中后**不执行任何东西** —— 只换 active agent 与 instructions。

实测（本仓库 `selftest.py` B5/B7）：

```
第一次调用 tools   = ['build_slide_deck', 'make_illustration', 'research', 'save_text_asset']
第一次调用 handoffs = [('transfer_to_literary_writer', 'literary_writer')]
```

---

## 3. 三个判据（设计时只问这三个）

### 判据一：要不要拆成多个 agent？

不是"功能 vs 业务"，而是**注意力边界有没有被撑破**。触发条件是症状：

* **Complex logic**：instructions 里 if-then-else 太多、模板难扩展；
* **Tool overload**：⚠️ 官方原文「问题不在数量，而在**相似性或重叠**」——有实现管好 15+ 个清晰工具，
  另一些不到 10 个重叠工具就崩。

官方两条原话：「maximize a single agent's capabilities first」「**This shouldn't be your go-to solution**」。
**没有症状就先别拆**；不拆时的正规做法是**把差异做成 prompt 变量**（一份基础 prompt + 政策变量）。

### 判据二：拆完之后怎么接？

官方 `multi_agent` 页那条判据：专家「helps with a **bounded subtask but should not take over the
user-facing conversation**」⇒ `as_tool`；「**routing itself is part of the workflow**」⇒ `handoff`。

翻译成一句话：**这段产出最终要出现在用户可见的回复里吗？**

* 给用户看、且**要接管对话** ⇒ 官方原语是 `handoff`（专家接管，用户接下来直接跟它说话）；
  ⚠️ **本项目不用 `handoff`** —— 它换成了"**图的一条边**"（专家是独立节点）。
  原因是本项目要给每条边配检查点与续跑，而 `handoff` 在**同一个节点内部**换人后继续跑，
  检查点看不见（见 §6.3）。判据不变，落点变了。
* 给主 agent 看 ⇒ `as_tool`（专家只回一段文本，用户永远看不到它）
* 谁都不需要看，只是流程规定的一步 ⇒ **代码编排**（它本来就不该是 agent）

### 判据三：这个环节配不配是 agent？

**需要模型"拿主意"的才配 agent。**
能写成确定性函数的（解析、切分、渲染、落盘、编号提取、格式校验）**连工具都不该是工具**。

⚠️ 这一条最常被违反，表现是"把 `render_slides` 也做成 tool" ——
那只是给模型多一个可以忘掉的选项。

---

## 4. 四类诉求 → 落到四种**不同**的原语

这是本次设计最重要的结论。用户列的"创作 / 回答 / 评估 / 校验"四类，
在官方范式里**不是四个并列的 agent**：

```
诉求              原语                              接线          谁按选择键
------------------------------------------------------------------------------
创作 · 文学       Agent                              **图的一条边**  模型
                                                    （2026-10-03 前是 handoff）
创作 · PPT/插图   Agent(as_tool)                     tools         模型
回答 · 资料问答   前台自己的 tools + 取证 as_tool     tools         模型
评估              Agent(as_tool)                     tools         **模型**
校验              @input_guardrail/@output_guardrail  官方原语      ——
解析/渲染/落盘     普通函数                           代码          ——
```

> **"走哪条路"没有单独一行** —— 官方形态里这件事**由入口 agent 自己顺手做了**
> （它同时带 tools 与 handoffs）。此前这里有一行 `入口分流 Agent(output_type=结构化判定)`，
> 2026-10-02 已删除，见页首「修订」。

三条不能含糊的理由：

1. **评估走 `as_tool`，由模型决定调不调。**（2026-10-02 按用户要求**从代码 `while` 改过来**。）
   官方两套形态都有：`llm_as_a_judge.py` 用 `while True` 交替 `Runner.run`（evaluator
   不在 tools/handoffs 里，每轮必跑、`pass` 才 break）；这里选的是模型自主调用。
   **选择"模型自主"必须付的透明度成本**：模型可能一次都不调 ⇒ 代码不能假设评审
   一定发生过 ⇒ `exit_reason` 多一档 `unreviewed`，且**跳过评审不算通过**。
   评审反馈靠**工具返回值**回到创作方（SDK 的 tool 结果进对话历史），
   不再由 runner 拼 user 消息回灌 —— 回灌那套是代码 `while` 形态的产物。
2. **校验必须是 guardrail。** 官方给了独立原语，且语义不同：
   `input_guardrail` 只在 `current_turn == 0` 跑、**默认与模型并行**（乐观执行，tripwire 触发时 token 已烧）；
   来源是 `starting_agent.input_guardrails + run_config.input_guardrails` —— **两个池子相加**
   ⇒ 同一条护栏只能出现在一个池子里，否则第一轮跑两遍。
   本项目**入口**护栏挂 `frontline_agent.input_guardrails`（2026-10-03 从 `RunConfig` 移过来）：
   链路拆成两个节点后，每轮第一个节点**固定**是前台（图里写死的边）——
   "第一份输入必被扫"从"与拓扑无关的不变式"变成了**结构保证**；
   而挂在 `RunConfig` 上反而会让文学那次运行（载荷 = 整段历史）把同一句话再扫一遍。
   另见 §6.2.1：接会话之后它扫的**范围**还要收敛到"本轮消息"；
   `output_guardrail` 只对产出最终输出的 agent 生效、**必定串行**，被拒的候选输出**不落 session**，
   它仍在 `RunConfig` 上 —— 因为产出最终输出的可能是前台、也可能是文学。
3. **"回答问题"不该另立 agent。** RAG 问答就是前台的职责，单独立一个只会让路由多一跳；
   真正需要独立的是"多步检索取证"这个**有界子任务**。

---

## 5. 本项目拓扑

```
用户提问
   │ 原文（取 state 里**最后一条用户消息**，不是"最后一条消息"）
   ▼
┌──────────────────────────────────────────────────────────┐
│ graph.dispatch（**同步**）：只探一次"链路能不能用"          │
│   ⚠️ 不做内容分诊 —— "直接答还是交给专家"由下面前台自己定   │
│   能用 ⇒ route=agent（**所有请求都走这条**）                │
│   用不了 ⇒ route=rag + reason（disabled / module_          │
│            unavailable / runtime_not_ready）→ 问答链路兜底  │
└───┬───────────────────────────────────────┬──────────────┘
    │ route=agent（默认路径）                │ route=rag（**兜底**：
    │                                        ▼  agent 链路不可用）
    │                                    原问答链路
    │                                    retrieve → generate
┌───┴──────────────────────────────────────────────────────┐
│ 【节点 1】frontline —— 前台 agent（唯一直接面向用户的 agent）│
│   ★ **每轮入口固定是它**，与上轮结束在谁无关                │
│   output_type = FrontlineDecision                          │
│        action="reply"    ⇒ content 是**给用户的答复**      │
│        action="transfer" ⇒ content 是**给下一位的交接说明** │
│   tools: research / build_slide_deck / make_illustration    │
│          / review_draft（评审）/ save_text_asset            │
│   input_guardrails: [prompt_injection_guardrail]  ← 见下方注 │
│   append_prompt=True（本轮原话**只在这里**进会话）           │
│   ⊕ 跨轮历史由官方 Session 按 thread_id 托管（见 §6.2）      │
└───┬────────────────────────────────────┬──────────────────┘
    │ 条件边：state["agent_decision"]["action"]               │
    │ == "transfer"                        │ != "transfer"    │
    │ （这一步**没有交付物**）              │                  │
    ▼                                      ▼                  │
┌───────────────────────────────┐   ┌───────────────────────┐│
│ 【节点 2】literary            │   │ 出口护栏（RunConfig）  ││
│   文学作者 agent              │   │  引用可追溯 / 长度上限 ││
│   无 output_type ⇒ 产出就是   │   └──────────┬────────────┘│
│   **作品正文**                │              │             │
│   tools: [review_draft]       │              │             │
│   append_prompt=False         │              │             │
│   （原话上一次运行已写进会话）  │              │             │
│   ⚠️ **没有回程边**（见上方修订）│             │             │
└───────┬───────────────────────┘              │             │
        │ 作品正文                             │             │
        ▼                                      ▼             │
┌──────────────────────────────────────────────────────────┐ │
│ 出口护栏（RunConfig）→ 记录 → route_agent_review          │ │
│   达标 ⇒ END（用户看到答复）                              │ │
│   不达标 ⇒ human_review（人工复核；agent 链路**不**产生   │ │
│            "regenerate" —— 不能拿 RAG 的生成器改 agent 产出）│ │
└──────────────────────────────────────────────────────────┘ │
```

> **两个 agent 节点各是一次独立的 SDK 运行**（`runner.run_agent`），所以：
>
> * 每个节点**各建一份 `TaskContext`** —— SDK 里没有"跨运行共享上下文"这回事。
>   跨节点要延续的东西（`citations` / `assets` / `agent_stats`）在 state 里有对应字段，
>   进节点时**从 state 读回来**。漏了不会报错，但后果全是静默的：文学那次运行的
>   引用校验会把"本轮真的检索过"的编号判成编造，或把前台落的资产从视图里抹掉。
> * 指标出现**两种口径**：`agent_route_total` / `agent_handoff_total` / `agent_rework_total`
>   是**请求口径**；`agent_runs_total` / `agent_model_calls` / `agent_tool_calls`
>   是**运行口径**（带 `agent` 标签）。混用分母会算出一个没有告警能发现的错误比率。
> * 异常**不再被节点兜住**（节点抛 `AgentRunFailed`）—— 只有抛出去，检查点才会
>   留下待执行节点、"继续生成"才真的可用。代价是这类请求不再表现为一句友好话术，
>   而是一次**失败**（刻意换的，理由见 `src/graph.py::AgentRunFailed`）。
>
> **护栏现在是"一挂一"，不再都挂 `RunConfig`：**
> * `input_guardrails=[prompt_injection_guardrail]` 挂在 **`frontline_agent` 上**。
>   每轮第一个 agent 节点**固定**是前台（图里写死的边），所以"第一份输入必被扫"
>   仍是结构保证；挂在 `RunConfig` 上反而会多扫一次 —— 文学那次运行的载荷是整段历史，
>   而两个池子是**相加**的（官方 `run.py`：`starting_agent.input_guardrails +
>   run_config.input_guardrails`），同一句话会被扫两遍。
> * `output_guardrails=[citation_traceability, length]` 仍在 `RunConfig` 上 ——
>   出口护栏只对"产出最终输出"的 agent 生效，而产出最终输出的可能是前台、也可能是文学。
> * 出口护栏要**读得懂结构化产出**：`action == "transfer"` 时这一步**没有交付物**，
>   护栏直接放行、且不计数（否则会拿判定对象的 repr 去判，或把交接说明里形如 `[9]`
>   的字样当成编造引用，把整条产出拦下）。

三条数据形态（对照第 3 节判据）：

| 边 | 传出去 | 收回来 |
|---|---|---|
| `as_tool` | **一组参数**（`{"input": "..."}`） | **一段文本**（结构化结果的 JSON 序列化） |
| 图的节点边（`frontline` → `literary`） | **一个结构化判定**（`action="transfer"` + 交接说明；由 state 承载） | **一份作品正文**（文学那次运行的 `final_output`） |
| 代码循环 | **state 里的输入项 + 一条 Feedback user 消息** | 下一个候选产出 |

---

## 6. 官方源码级证据（标注【原文】/【实测】/【分析】）

### 6.1 handoff 传的是"上下文变更"，不是"任务结果"

【原文】官方 `examples/customer_service/main.py`：

```python
async def on_seat_booking_handoff(context: RunContextWrapper[AirlineAgentContext]) -> None:
    flight_number = f"FLT-{random.randint(100, 999)}"
    context.context.flight_number = flight_number
```

而下游工具里写着：

```python
assert context.context.flight_number is not None, "Flight number is required"
```

【分析】`on_handoff` 唯一做的事是往共享上下文写一个字段，下游靠它工作 ⇒
**handoff 的产出不是"结果"，而是"接下来该按什么前提办事"**。结果走 messages。

### 6.2 跨轮对话：入口固定 + 官方 Session（**本项目有意不照抄官方的一处**）

【原文】官方两个多轮示例的主循环：

```python
current_agent: Agent[AirlineAgentContext] = triage_agent   # 第一轮的入口，写死
input_items: list[TResponseInputItem] = []
while True:
    input_items.append({"content": user_input, "role": "user"})
    result = await Runner.run(current_agent, input_items, context=context)
    input_items = result.to_input_list()      # ← 跨轮载体之一：历史
    current_agent = result.last_agent         # ← 跨轮载体之二：谁在接话
```

（`examples/customer_service/main.py`；`routing.py` 里分别写作
`inputs = result.to_input_list()` 与 `agent = result.current_agent`。）

【分析】这两个赋值是**一套、两半**：一个记"说到哪儿了"，一个记"谁在接话"。少了任何一半，
"接着上一轮继续说"都不成立。而官方的语义是：控制权一旦交给专家就**留在专家手里**，
直到专家自己判断"这不归我管了"再转回分诊台。

⚠️ **本项目只采用这一套的后半部分（历史），前半部分（入口跟随）刻意改掉了：**

| | 官方 | 本项目 | 理由 |
|---|---|---|---|
| 入口 | 第一轮写死，之后 `= result.last_agent` | **每轮固定为前台**（常量 `ENTRY_AGENT`） | 跟着上轮走时，一旦换了话题，要由**专家**先判断"这不是我的活"再转回前台 —— 那是一次"专才勉强判断不属于自己领域的事"的模型调用，且失败方式是**硬撑不转**（用户会看到一个文学专家在解释限流怎么算）。固定回前台，判断权就在唯一有全局视野的那个 agent 手上；代价只是"接着聊"时多一次模型调用 |
| 历史 | `to_input_list()` 存**局部变量** | 官方 `Session`（`Runner.run(session=...)`）+ 落 PostgreSQL（官方 `SQLAlchemySession`，与检查点同库） | 局部变量活不过一个请求；`Session` 是官方为"服务"准备的机制，附带三件容易做错的事：取历史时按身份/频次**去重**、写回时按"本轮已存到第几条"防重、截断后**清孤儿工具调用**（含 reasoning 配对） |

⇒ 由此得到两条硬约束：

1. **`runner` 里不许出现"读上一轮结束在哪个 agent"的入口判定。** `entry_agent_for` /
   `_ENTRY_AGENTS` / `DEFAULT_ENTRY_AGENT` 已删；谁把它们加回来，就等于静默恢复了
   入口跟随。守卫：`test_entry_agent_is_pinned_to_the_frontline`（单元）+
   `test_state_agent_does_not_decide_the_entry`（图级 —— 后者才拦得住"graph 又把状态传回去"）。
2. **本轮只送增量。** 传了会话之后所有输入都会被**追加**进会话（`save_result_to_session`
   的去重只覆盖"同一轮内的重试"），所以人工打回那一轮若重传用户原话，会话里就会出现
   两条一模一样的用户消息（实测：条目 2 → 5）。守卫：
   `test_revise_turn_appends_only_the_feedback`。

   ⚠️ **2026-10-03 拆分之后这条判据变成两种情形，别再当成一句话**（判据是
   `runner._build_input_items`）：

   | 情形 | 原话在会话里了吗 | 判据 |
   |---|---|---|
   | 本轮**第一个** agent 节点（前台） | 还没有 | `append_prompt=True` ⇒ 带上 |
   | 本轮的**后续**节点（文学） | 上一个节点刚写进去 | `append_prompt=False` ⇒ 不带 |
   | **人工打回**那一轮（前台） | **已经在了** —— 第一遍（人工评审之前那次运行）就写进去了 | 判据**不是** `append_prompt`（它在打回轮仍是 `True`，因为前台确实是本轮第一个节点），而是"**会话在不在**" |

   两者在打回轮上**结论相反** —— 用同一个变量表达就会静默重复。会话不可用时一律带上
   （没地方存历史，不带等于模型什么都看不到，而那是一种没有任何报错的坏）。

⚠️ **`current_agent` 仍在写进 state，但它已降级为纯观测**（回答"这轮谁收的尾"），
**不参与路由**。它的值仍必须是**结局**（每次运行的 `result.last_agent.name`），不是起点。
两个节点各写一次，最后一次写下的就是"这轮谁收的尾"；某次运行没跑到底时**不覆盖**，
保持原值（官方异常路径也不会更新它）。守卫
`test_graph_writes_back_the_agent_the_run_ended_on`、`test_failed_run_does_not_move_the_conversation`。

**`on_handoff` 回调已整条作废**（2026-10-03）：链路里**一个 handoff 都没有了**，
所以没有任何触发点（`hooks.on_handoff` 的 docstring 已标注"当前不会被触发"）。
跨节点转移的"谁交给了谁"由 `graph.frontline` 手工补记到同一个 `stats.handoffs` 字段上，
`agent_handoff_total` 的语义因此不变（它回答的一直是"多少请求被交给了专家"）。

⚠️ 历史坑（别再走回去）：本仓库曾用 `on_handoff` 回调去写"谁在接话"，结果是回调只覆盖
**去程**、回程没有对等写入 ⇒ 写回的是陈旧值，两条边都"注册了"却等于白接，读代码完全
看不出来。2026-10-02 由只读探针实测复现，随后改为**结果驱动** —— 取
`result.last_agent` 这个唯一的事实来源。

#### 6.2.1 接会话之后，一处判据必须跟着改：入口护栏扫的范围

【原文】官方 `run.py`（非 server-managed 分支）：

```python
prepared_input, _ = await prepare_input_with_session(raw_input, session, ...)
original_input_for_state = prepared_input          # ← 历史 + 本轮，**合并后**的
...
await run_input_guardrails(..., copy_input_items(original_input), ...)
```

【分析】护栏拿到的是**合并后的输入**。于是照整份扫有两个后果，且都不会报错：
`warn` 档下历史里那句命中被**每轮重复计数**（`agent_input_scan_total` 从"命中请求数"
变成"命中次数 × 剩余轮数"）；`block` 档下是**功能级**的 —— 用户第一轮说了句被判为注入
的话（被拦、且该输入会被写进会话），之后**每一轮都被历史里那句话拦住**，用户被永久锁在
拒绝里。⇒ 本仓库把判据收敛回"**最后一条用户消息**"（`guards._latest_user_text`），
这也正是接会话**之前**的实际语义。

⚠️ **挂载点在 2026-10-03 从 `RunConfig` 移到了 `frontline_agent.input_guardrails`。**
理由不是"换个地方"，而是两个池子是**相加**的（上面的官方原文）：链路拆成两个节点后，
每轮第一个节点**固定**是前台（图里写死的边）⇒ "第一份输入必被扫"成了**结构保证**；
而挂在 `RunConfig` 上会让文学那次运行再扫一遍 —— 那次的载荷是**整段历史**，
等于把同一句话扫两次、并重新踩上"历史命中被重复计数"的老问题。
守卫：`test_input_guardrail_scans_the_new_message_not_the_whole_history`（两个方向都钉）、
`test_input_guardrail_is_wired_in_exactly_one_pool`（只许出现在一个池子里）。

### 6.3 跨节点转移：**不再用 handoff**，改用「结构化判定 + 图的一条边」

> 本节在 2026-10-03 被**整节改写**。上一版标题是"handoff 单向 ⇒ 回程边必须显式注册" ——
> 那个结论随着"拆成两个图节点"一起作废了：链路里**已经没有 handoff**。

【原文】官方 `run.py`，handoff 之后是**同一个 run 内部**继续跑：

```python
# we run the loop again, with the new agent
```

【分析】这句话是本节的全部理由：**SDK 的 handoff 不离开当前节点**。所以

* 只要节点里还带着 `handoffs`，控制权就在这个节点内部换来换去，
  检查点只能看见"这个节点跑完了"，看不见"中途换过谁" —— 拆节点等于没拆；
* 反过来，要让检查点记录到"从 A 走到 B"，A 与 B **必须是两个图节点**。

⇒ 本项目把跨节点转移换成：**前台用 `output_type=FrontlineDecision` 输出判定**，
节点把它写进 state（`agent_decision`），**条件边**读这个字段决定跳 `literary` 还是收尾。
于是"为什么跳了"进了检查点，而且**节点边界 = 续跑边界**（崩在文学那次运行上，
续跑从文学开始，前台不会重跑）。

三条守卫：

- `test_no_handoffs_anywhere_and_no_unkeepable_promise` —— 一个 `handoffs` 都不许有，
  且文学专家的 instructions 里**不许**再出现"转回前台"（那是一个拓扑上做不到的承诺，
  留着模型会尝试调用一个不存在的工具、或者硬撑着不转）；
- `test_frontline_topology_is_exactly_what_the_design_claims` —— 前台的
  `tools` / `handoffs` 逐项对照本页；
- `test_graph_routes_a_transfer_to_the_literary_node` —— 图级：判定为 `transfer` 时
  `next == ("literary",)`，落盘可查。

⚠️ **"转交"这一步没有交付物。** 它不算通过、也不算"漏审"（`exit_reason="transfer"`），
出口护栏对它**直接放行且不计数**（守卫 `test_transfer_note_is_not_judged_as_a_deliverable`、
`test_transfer_is_not_filed_under_unreviewed`）。否则会拿判定对象的 repr 去判长度，
或把交接说明里形如 `[9]` 的字样当成"编造引用"而拦下整条产出。

⚠️ **没有回程边**（2026-10-03 拍板丢掉）：入口已固定为前台，下一轮用户说话自然回到前台。
加环会让图需要额外配一个打转上限，而那正是被丢掉的能力。所以排查时**不要**去
`agent_handoff_total` 里找 "A→B→A 的往返" —— 它在结构上不可能发生。

### 6.4 as_tool 的嵌套 run 继承父 run 的 RunConfig

【原文】`agents/agent.py` 的 `as_tool()` 内部：

```python
resolved_run_config = run_config
if resolved_run_config is None and isinstance(context, ToolContext):
    resolved_run_config = context.run_config
```

【分析】⇒ 在顶层 `Runner.run(..., run_config=...)` 挂一次，前台、专家、嵌套 run 全部生效。
这也是本示例能完全离线跑起来的前提（`RunConfig(model=ScriptedModel(...))`）。

### 6.5 反馈回灌与停止条件

【原文】`llm_as_a_judge.py`：评审 `output_type=EvaluationFeedback{feedback, score: Literal[...]}`；
反馈靠 `input_items.append({..., "role": "user", "content": f"Feedback: {...}"})` 回灌；
停止条件写在 evaluator 的 instructions 里 ——
「Never give it a pass on the first try. After 5 attempts, you can give it a pass … **do not go for
perfection**」。

【分析】不用分数阈值当停止条件是关键：LLM 评委会持续给出小幅改进建议，阈值永远停不下来。

⚠️ **本项目刻意不照抄这一段**（2026-10-02 返工）。上面整套（`while` 交替 `Runner.run` +
`input_items.append(Feedback)` 回灌 + `MAX_ATTEMPTS` 代码上限）是**代码 `while` 形态**的
配套：它假设"每轮必评、评不过就再来一轮"。本项目改成 **`as_tool` + 模型自主调用**之后：

* **没有 `MAX_ATTEMPTS` 了**（`runner.py` 里已删）。不存在代码驱动的重试轮次，
  自然也没有"代码上限"要兜。要改就由模型在同一次 run 里自己改。
* **评审反馈不走 user 消息回灌**，走**工具返回值**（SDK 的 tool 结果进对话历史）。
* 官方那句「do not go for perfection」**仍然照搬**，但落点变了：它不再用来兜"循环 5 轮
  还不 pass"，而是直接写进 `evaluator` 的 instructions（"措辞与风格上的偏好不构成
  `needs_improvement` —— 反复要求重写只会让交付无限延迟"）。

### 6.6 官方"刻意不让某些环节成为 agent"

| 官方例子 | 事实 |
|---|---|
| `research_bot/manager.py` | `ResearchManager` 是**普通 Python 类**，不是 `Agent`；全程代码串（plan → 并行 search → write） |
| `agent_patterns/parallelization.py` | 同一个 `spanish_agent` 对象被 `asyncio.gather` 跑 3 次 ⇒ agent 是无状态配置对象；"跑几次"由**代码**定 |
| `agent_patterns/agents_as_tools.py` | 三个翻译 agent 的 instructions **各一行、零工具**；最后的 `synthesizer_agent` 是**代码串上去的第二步**，不是工具 |

### 6.7 官方例子的 agent 数落在 2~5

切法三种都有（按目标语言切 / 按领域切 / 按阶段切）⇒ **"按功能还是按业务"这个问法不成立**。
共同点只有一条：**两个 agent 的工具集与上下文不重叠**。

---

## 7. 本仓库实现的关键决策

| 决策 | 理由 |
|---|---|
| `frontline_agent` 是唯一对外的 agent | 用户只需要一个对话对象；多入口 = 多一套"谁该接话"的判定 |
| **只拆到"会接管对话"这一级**：前台 / 文学 = 图里的两个节点 | 节点边界 = **续跑边界**。工具调用**不**拆成节点 —— 工具级的唯一收益是断点更细（秒级问答消费不到），代价是把工具循环 / 历史裁剪 / 三级护栏 / `as_tool` 全部重写 |
| 三个专家走 `as_tool` 且 `output_type` 结构化 | 产出要被**代码**消费（汇总 / 渲染 / 落库），结构化了才可判 |
| 文学专家走**图的一条边**（2026-10-03 前是 `handoff`）且**不结构化** | 它交付的是作品本身；结构化会把作品压成字段。改走图边是因为节点里带 handoff 会让节点边界消失（见 §6.3） |
| 前台的 `output_type = FrontlineDecision`（`reply` / `transfer`） | "要不要转交"由**模型**定，但**判据落在 state 里** ⇒ 检查点看得见"为什么跳到了下一个节点" |
| 评估 agent 挂在**创作者自己的 `tools` 里**（`as_tool`） | 2026-10-02 按用户要求改：**调不调由模型定**。代码侧的代价是必须多一档 `exit_reason="unreviewed"`（跳过评审 ≠ 通过），且返工判据从 `attempts>1` 换成 `review_rounds>1` |
| **入口**护栏挂 `frontline_agent`；**出口**护栏挂 `RunConfig` | 入口每轮的"第一个节点"固定是前台（结构保证），挂 RunConfig 会让文学那次运行拿整段历史再扫一遍；出口护栏只对产出最终输出的 agent 生效，那个 agent 可能是前台也可能是文学 |
| 渲染 / 落盘 / 文件写入放在代码里 | 没有模型需要"决定要不要渲染" |
| 本示例的护栏全是确定性的 | 能用正则/编号判定的就不要花一次模型调用；需要判断力的才按官方 `output_guardrails.py` 形态另建一条 |
| 演示与测试**自建** `ScriptedModel`（`tests/_agents_testkit.py`） | 官方那个只在较新 SDK 里有（0.20 没有 `agents.testing` 子模块），而依赖刻意停在 `openai-agents==0.20.0`（更高版本要求 `openai>=3`，与 `langchain-openai` 的 `openai<3` 冲突）。自建用的仍是官方扩展点 `Model.get_response` |

---

## 8. 已知缺口（都还没做，别当成已完成）

1. **`examples/sub_agents/` 落后两次修订**（2026-10-02 与 2026-10-03）：入口判定仍缺、
   评审仍是代码 `while`、跨轮历史仍靠局部变量、文学仍走 `handoff`（新版已改成图的一条边），
   且 `evaluator.py` 的注释还在论证被推翻的结论。
   生产实现 `src/agents/` + `src/graph.py` 已按最新决策落地。
   要么把示例一起改，要么在示例里显式标注"这是修订前的历史参考"—— **别放着两边互相矛盾**。
2. **未接真实模型。** 需要 `OPENAI_API_KEY`（示例刻意不需要）。
3. **历史压缩：已做（2026-10-08 落地，默认关闭）。** 落在 `src/agents/compaction.py`
   —— **两个固定入口 + 一个共用内核**，四个调用点（两条链路各两个）：
   - **入口一（历史压缩）**：老历史超过"保留轮数"就压。会话侧在 `app/main.py::_stream_graph`
     开头起一个后台任务（**不挡本轮**，下一轮读到压过的）；图状态侧是 `src/graph.py` 新增的
     `compact_history` async 节点，插在 `init` 与 `dispatch` 之间（**本轮生效**）。
   - **入口二（窗口压缩）**：将要送进模型的输入超过**预算的 85%** 就压。agent 链路走
     `RunConfig.call_model_input_filter`（`history_budget.trim_model_input` 现在是两层：
     **先摘要、再硬截断**）；问答兜底链路在 `generate` 调模型之前，用同步版本
     （langgraph 把同步节点丢线程池 ⇒ 那里没有事件循环）。
   - 会话侧的"清空 + 写回"与"写会话"走**同一把锁**（`CompactingSession` 是会话装饰器，
     在 `session_for` 的缓存里包一次）；异常与取消**都回滚**；摘要为空**绝不替换**。
   - ⚠️ **官方 `OpenAIResponsesCompactionSession` 仍然用不了，但真正的闸门是 `response_id`、
     不是模型名。** SDK 的调用点判据是
     `if response_id and is_openai_responses_compaction_aware_session(session)`
     （`agents/run_internal/session_persistence.py`），而 `response_id` 只有 **Responses 接口**
     才给（`models/openai_responses.py`），本项目走 Chat Completions
     （`models/openai_chatcompletions.py` 里写死 `response_id=None`）⇒ **恒为空**，
     那个钩子**安静地什么都不做**。（此前这里记的是"构造时校验模型名，只认 `gpt-*`"——
     那是**更表面**的一层，即便骗过它也还是不会被调用。形状照抄官方，触发点自己找。）
   - 摘要写成 `role="system"`（图里是 `SystemMessage`），切分**必须按"整轮"**
     （工具调用与结果靠 `call_id` 配对，按条目截会白丢信息）。
     ⚠️ **"对话中间夹一条 system 消息"这一条未经真实模型验证**（本机不接真模型），
     落地后要用一次真实调用冒烟：确认网关接受，且模型把它当背景而非新指令。
   - tracing 目前仍是"未配 key 则跳过导出"。
4. **"需要判断力的出口护栏"未实现。** 现在只能证明"引用编号在本轮检索结果里出现过"，
   证明不了"那句话真被该片段支撑" —— 后者需要 LLM，应按官方 `output_guardrails.py`
   另建一条，**不要混进现有的确定性护栏假装都覆盖了**。
5. **`conversation_id` 与 handoff input filter 不兼容**（官方限制）：
   若将来要用服务端托管会话，需要"接收方不继承历史"时，必须另起一次 run。
6. **未做真实模型下的评测。** 现有 53 条断言全是结构与行为断言，**不含答案质量**；
  质量侧仍走 `scripts/eval_*.py` 那三条链路。

---

## 9. 验收清单（改这个设计时逐条过）

- [ ] 新加的专家是否**真的**需要模型拿主意？（能写成函数的一律不建 agent）
- [ ] 新专家的产出给谁看？选择与产出形态一致吗？三种落点别混：
      **给主 agent 消费** ⇒ `as_tool` + `output_type` 结构化；
      **接管对话、要跨轮续跑** ⇒ **图的一个节点** + 结构化判定 + 条件边；
      **确定性步骤** ⇒ 普通函数 / 代码。
- [ ] 如果新加的是**图节点**：节点内部有没有偷偷带 `handoffs`？
      （带了节点边界就消失，"拆开了"是假的 —— `run.py` 里 handoff 之后是 `continue`）
      守卫：`test_no_handoffs_anywhere_and_no_unkeepable_promise`。
- [ ] 会接管对话的专家，有没有被同时放进 `tools`？两个位置语义完全不同
      （`tools` = 有界子任务、产出用户看不到；节点 = 接管对话）。
- [ ] 专家 instructions 里承诺的"可以转给谁"，拓扑上**有没有那条边**？
      本项目现在**没有回程边**（文学不会在本轮内转回前台）—— 提示词里若还这么说，
      就是一个做不到的承诺。
- [ ] 新加的校验是确定性判据吗？是的话**不要**做成 agent。
- [ ] 新加的"要不要做"判据，是**模型**该拿主意的（⇒ 工具 / 结构化判定），
      还是**代码**该定的（⇒ 普通函数 / 护栏）？两者别互串。
      ⚠️ 反例：在入口外另加"内容分诊"（正则词表、或让另一个 agent 先分类），
      或用代码 `while` 强制每轮评审 —— 两处都是 2026-10-02
      返工掉的东西，**别再改回去**。
- [ ] 评审（`review_draft`）在 `tools` 里吗？**两处**都挂了吗？
      （前台 + 文学 —— 产出最终输出的那个 agent 必须有它；只挂前台会整条漏掉。）
      `exit_reason="unreviewed"` 这一档还在不在？转交（`transfer`）有没有被误算成
      `unreviewed`？守卫：`test_transfer_is_not_filed_under_unreviewed`。
- [ ] 护栏挂对池子了吗？**入口**挂 `frontline_agent`（每轮第一个节点固定是它），
      **出口**挂 `RunConfig`；出口护栏**读得懂结构化产出**吗（`action="transfer"` 时
      没有交付物，要放行且不计数）？守卫：`test_input_guardrail_is_wired_in_exactly_one_pool`、
      `test_transfer_note_is_not_judged_as_a_deliverable`。
- [ ] `AgentStats` 新增字段时，有没有归类到 `CARRY_ACROSS_NODES` 或 `RUN_LOCAL`？
      跨节点要延续的东西**必须**能从 state 读回来，否则文学那次运行看不见前台留下的
      引用 / 资产 / 统计。守卫：`test_agent_stats_fields_are_all_classified`。
- [ ] 新增的指标是**请求口径**还是**运行口径**？两者混用分母会算出一个
      没有告警能发现的错误比率（见 §5 的注）。
- [ ] 节点里的异常是**抛出去**还是兜住？兜住 ⇒ 检查点写着"没有待执行节点" ⇒
      接口报 `completed`、客户端连"继续生成"都不弹。守卫：
      `test_agent_failure_bubbles_and_leaves_the_turn_resumable`。
- [ ] 入口判定里**有没有**出现"读上一轮结束在哪个 agent"？（那是不该有的，见 §6.2）
- [ ] 送进 `Runner.run` 的是**本轮新增的那一句**吗？（送整份历史会与会话里的历史叠加，
      重复量随轮次平方增长。）两种情形判据不同，详见 §6.2 的表格 ——
      本轮第一个节点、后续节点、人工打回轮，三者不是同一句话。
- [ ] 新加的入口护栏判据扫的是**本轮消息**还是整份载荷？（见 §6.2.1）
- [ ] 断言的落点是不是"模型实际收到的输入"？（只断言"结果对"会出现假绿）
- [ ] 用"脏数据 / 污点"构造的用例，**有没有先断言"污点本身确实会被识别"**？
      否则用例可能整条空转 —— 2026-10-02 变异验证抓到一个真实例子：所谓"注入话术"
      那句**根本不命中**护栏规则，于是"历史脏"与"本轮干净"在结果上毫无区别，
      把判据退回扫描整份载荷**一个测试都不红**。
- [ ] 本轮新增/修改的守卫，**跑过变异验证了吗**（故意破坏实现 → 必须变红 →
      SHA-256 逐文件自证还原）？"从没红过的绿断言不是证据"。
      ⚠️ 变异本身也要辨真伪：置成 `None` 而官方 `resolve()` 只叠加非 None 字段 ⇒
      那是**空操作**，绿是对的；换成"把常量冻进去"才知道守卫管不管用。
- [ ] `python -m examples.sub_agents.selftest` 与 `.mutate` 是否都全绿？
      ⚠️ 它们验的是**修订前**的形态（见 §8 缺口 1）—— 全绿只说明示例自身自洽，
      **不能**拿它当"当前设计正确"的证据。
