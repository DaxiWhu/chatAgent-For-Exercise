# OpenAI Agents SDK 详解（Python）

> **性质**：参考资料（非计划、非规范）。内容取自 2026-10 官方文档逐页取证，用于给
> chatAgent 的后续架构决策提供参照基线。
> **取证范围**：Intro / Agents / Tools / Guardrails / Running agents / Streaming /
> Agent orchestration / Handoffs / Results / Human-in-the-loop / Sessions / Context /
> Usage / MCP / Tracing / Models / Configuration / Sandbox agents。
> **引用纪律**：`「」` 内为官方原文；标注「分析」的是本文判断，不是官方立场。

---

## 0. 一句话定位

> 「The OpenAI Agents SDK enables you to build agentic AI apps in a **lightweight, easy-to-use
> package with very few abstractions**. It's a **production-ready upgrade** of our previous
> experimentation for agents, **Swarm**.」

两条自述设计原则：

1. 「Enough features to be worth using, but **few enough primitives** to make it quick to learn.」
2. 「Works great out of the box, but you can **customize exactly what happens**.」

**分析**：它的竞争力不在"功能多"，而在**原语少且正交**。整个 SDK 的一等公民只有
`Agent` / `Runner` / `Tool` / `Handoff` / `Guardrail`，其余（Sessions、Tracing、Sandbox、
Realtime、Voice、MCP）都是围绕这五个的**配套层**。

---

## 1. 最小骨架

```bash
pip install openai-agents
export OPENAI_API_KEY=sk-...
```

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")
result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)
```

四个对象构成完整心智模型：

| 对象 | 角色 |
|---|---|
| `Agent` | 一个"LLM + 指令 + 工具 + 可移交对象"的配置体（**纯配置，无状态**） |
| `Runner` | 唯一的执行器，内含 agent loop |
| `RunResult` | 本轮运行的结果契约（输出、items、用量、中断） |
| `RunConfig` | 单次运行的覆盖项（模型、护栏、追踪、工具执行） |

**关键设计**：`Agent` 是无状态配置对象。**所有运行状态都在 `Runner` 侧**（result /
RunState / Session）。这与"把状态写进 agent 对象"的框架有本质区别 —— 意味着同一个
`Agent` 实例可被并发复用于多个 run。

---

## 2. 核心抽象

### 2.1 Agent 的完整参数

| 参数 | 必填 | 作用 |
|---|---|---|
| `name` | **是** | 人类可读名 |
| `instructions` | 否 | 系统提示词，**可为静态字符串或动态函数**（接收 agent + context，可 async） |
| `prompt` | 否 | 引用 OpenAI 平台上的 prompt 模板（静态对象或函数） |
| `handoff_description` | 否 | 被当作 handoff 目标时暴露的描述 |
| `handoffs` | 否 | 可移交的专家 agent 列表 |
| `model` / `model_settings` | 否 | 模型与调参 |
| `tools` | 否 | 可调用工具（含 `agent.as_tool()`） |
| `mcp_servers` / `mcp_config` | 否 | MCP 工具 |
| `input_guardrails` / `output_guardrails` | 否 | 输入/输出护栏 |
| `output_type` | 否 | 结构化输出类型（**传了即触发 structured outputs**） |
| `hooks` | 否 | agent 作用域生命周期回调 |
| `tool_use_behavior` | 否 | 工具结果的处置方式（见 §5.4） |
| `reset_tool_choice` | 否 | 工具调用后是否重置 `tool_choice`（默认 `True`，**防工具死循环**） |

### 2.2 Context：依赖注入，不是提示词

> 「The **most important** thing to be aware of: **every agent, tool function, lifecycle etc
> for a given agent run must use the same _type_ of context**.」
>
> 「The context object is **not** sent to the LLM. It is purely a **local object** that you can
> read from, write to and call methods on.」

```python
@dataclass
class UserContext:
    name: str
    uid: str
    async def fetch_purchases(self) -> list[Purchase]: ...

agent = Agent[UserContext](name="Assistant", ...)
result = await Runner.run(agent, "…", context=UserContext(...))
```

`RunContextWrapper[T]` 暴露：`context`（你的对象）、`usage`（用量）、`tool_input`
（嵌套 `as_tool` 的结构化入参）、`approve_tool()` / `reject_tool()`。

子类 `ToolContext` 额外给：`tool_name` / `tool_call_id` / `tool_arguments` / `tool_namespace`。

⚠️ **安全提醒（官方原文）**：「Avoid putting secrets in `RunContextWrapper.context` if you
intend to persist or transmit serialized state.」——HITL 场景下 `RunState` 会把运行元数据一起序列化。

---

## 3. 执行模型：只有一条 loop

> 「The SDK uses the Responses API by default for OpenAI models, but it **wraps model calls in
> a higher-level runtime**.」

### 3.1 agent loop 的确切步骤

对 `Runner.run` / `run_sync` / `run_streamed` 中任意一个，runner 执行同一套循环：

1. 用「当前 agent + 当前 input」调用 LLM。
2. LLM 产出输出，进入三分支：
   - **2.1** 归为 **final output** ⇒ 循环结束并返回。
   - **2.2** 请求一次 **handoff** ⇒ **更新当前 agent 与 input**，重新运行循环。
   - **2.3** 产生 **tool calls** ⇒ 执行并追加结果，重新运行循环。
3. 超出 `max_turns` ⇒ 抛 `MaxTurnsExceeded`（传 `max_turns=None` 禁用上限）。

**「final output」的判定规则（原文）**：
> 「it produces text output with the desired type, and there are **no tool calls**.」

### 3.2 三个容易记错的语义

| 项 | 真相 |
|---|---|
| `max_turns` 的单位 | **agent-loop turns = LLM 调用次数**，不是"对话轮次" |
| 一次 run 的含义 | 「a single **logical** turn」内部可能有**多次 Agent 运行与多次 LLM 调用** |
| 超限行为 | 抛 `MaxTurnsExceeded`，**不是**返回部分结果 |

**分析（本文最重要的判断之一）**：`handoff` 与 `tool calls` 是**同一层的两个出口**，
整个 SDK **没有"支路"这种结构**。分流是模型在运行时**选择调用哪个函数**的结果 ——
即**涌现的、数据驱动的**，而非结构预设的。

### 3.3 并发与取消

- `RunConfig.tool_execution = ToolExecutionConfig(max_function_tool_concurrency=N)`
  限制本地工具并发；`None`（默认）= 全部同时启动。
  ⚠️ 它与 `ModelSettings.parallel_tool_calls` **不同**：后者控制模型"能否一次发多个工具调用"，
  前者控制 SDK"模型发出后怎么执行"。
- `result.cancel()` 立即停止；`result.cancel(mode="after_turn")` 让当前轮干净跑完再停，
  之后可用 `RunState` 恢复。

---

## 4. 编排：两个维度，四个格子

官方把编排拆成**两个正交维度**，混起来讲是理解它最大的障碍。

### 4.1 维度一：谁做决策

| 方式 | 官方定义 | 代价 |
|---|---|---|
| **LLM 决策式** | 「An agent is an LLM equipped with instructions, tools and handoffs… the LLM can **autonomously plan** how it will tackle the task」 | 路径不可预知，难压测 |
| **代码式** | 「orchestrating via code makes tasks **more deterministic and predictable**, in terms of speed, cost and performance」 | 分支写死，复杂度上升即僵化 |

代码式的官方四种形态：**structured outputs 分类后路由** / **链式串联** / **while + evaluator
循环** / **`asyncio.gather` 并行**。

⚠️ **官方对 declarative 框架的批评（原文）**：
> 「Some frameworks are **declarative**, requiring developers to explicitly define
> **every branch, loop, and conditional in the workflow upfront** through graphs consisting of
> nodes and edges… While beneficial for visual clarity, this approach can **quickly become
> cumbersome and challenging as workflows grow more dynamic and complex**. In contrast, the
> Agents SDK adopts a more flexible, **code-first** approach.」

**分析**：这段是官方对"把所有分支预先画成图"的明确保留意见。凡是打算把
「每条支路 + 每个回边」都写进图结构的方案，都在它的批评范围内。

### 4.2 维度二：控制权归谁（只在多 agent 时出现）

| 模式 | 机制 | 官方判据（原文） |
|---|---|---|
| **Agents as tools** | manager 通过 `Agent.as_tool()` 调用专家 | 「You want **one agent to own the final answer**, combine outputs from multiple specialists, or **enforce shared guardrails in one place**.」 |
| **Handoffs** | triage 把会话**移交**给专家，专家成为活跃 agent | 「You want the specialist to **respond directly**, keep prompts focused, or have the handoff switch the active instructions **without requiring the manager to narrate the result**.」 |

**机制要点**：

- **Handoff 底层就是一个工具** —— 「Handoffs are **represented as tools** to the LLM… the tool
  would be named `transfer_to_refund_agent`」。
- Handoff 默认传递**完整历史**；`input_filter` 可改写，`input_type` 可携带元数据，
  `on_handoff` 可挂回调，`is_enabled` 可动态开关。
- **`as_tool()` 的嵌套 run 默认不继承父 run 的会话状态** ——
  「the parent run's conversation state is **not inherited automatically**」，需要显式传同一个 `session`。
- `as_tool()` 的返回值可用 `custom_output_extractor` **完全改写**（默认用子 agent 的 final output）。

### 4.3 Handoff 的四条硬约束

1. **一个目标注册一个 handoff**：「The `handoff()` helper **always transfers control to the
   specific `agent` you passed in**.」`input_type` 只携带元数据，**不做分发**。
2. **`is_enabled` 不能做授权**：「`is_enabled` is evaluated… **before the model returns handoff
   arguments**, so it **cannot authorize values inside an argument-bearing handoff**.」→ 需要在
   `on_handoff` 开头校验并 `raise`。
3. **护栏不随 handoff 复制**：「Input guardrails still apply **only to the first agent** in the
   chain, and output guardrails **only to the agent that produces the final output**.」
4. **handoff 只能前进不能回退**（除非你显式给对方配一个回程 handoff）。

### 4.4 子 agent 的粒度：按什么切、怎么接线（官方判据 + 源码证据）

**先说什么不是判据**：官方**没有**按「一个功能 vs 一个业务」划分。触发拆分的是一个**症状**：

- `A practical guide to building agents`：「Our general recommendation is to **maximize a single
  agent's capabilities first**… often **a single agent with tools is sufficient**.」
  两条触发条件：**Complex logic**（prompt 里 if-then-else 太多、模板难扩展）、
  **Tool overload**（「The issue isn't solely the number of tools, but their **similarity or
  overlap**」——有实现管好 15+ 个清晰工具，另一些不到 10 个重叠工具就崩）。
- `developers.openai.com/tracks/building-agents`：「**This shouldn't be your go-to solution**,
  but … if you have **separate tasks that do not overlap**」＋ **Very complex or long
  instructions** / **A lot of tools (or similar tools across tasks)**。
- 不拆时的官方替代方案是 **prompt 模板变量**（「a single flexible base prompt that accepts
  policy variables」），而不是再拆一个 agent。

**拆分的三条收益（官方原文）**：Separation of concerns（「Research vs. drafting vs. **QA**」）/
Parallelism / **Focused evals**（「Score agents differently, depending on their scoped goals」）。
⇒ 「创作 vs 评审分开」是被官方点名列出的正当理由；**Focused evals** 意味着拆开后每段可**单独评测**。

**接线三选一，判据只有一条：子 agent 的话给谁看**（`multi_agent` 页原文）：
「Use **agents as tools** when a specialist should help with a **bounded subtask but should not
take over the user-facing conversation**. Use **handoffs** when **routing itself is part of the
workflow**…」

| 接线 | 谁按下选择键 | 面向谁 | 控制权 | 官方形态出处 |
|---|---|---|---|---|
| `handoff()` | 模型 | **用户** | 转移 | `examples/agent_patterns/routing.py` |
| `Agent.as_tool()` | 模型 | manager | 保留 | `examples/agent_patterns/agents_as_tools_conditional.py` |
| 代码编排 chain / while | **你的代码** | 都不面向 | 你的代码 | `examples/agent_patterns/llm_as_a_judge.py`、`parallelization.py` |

**「创作 + 评审」属于第三种（代码编排）**，源码证据 `examples/agent_patterns/llm_as_a_judge.py`：
两个独立 `Agent`（`story_outline_generator` / `evaluator`），**既非 handoff 也非 `as_tool()`**；
`while True` 里交替 `Runner.run`；评审用 `output_type=EvaluationFeedback`
（`feedback: str` ＋ `score: Literal["pass","needs_improvement","fail"]`）保证**代码可判**；
反馈靠 `input_items.append({…, "role": "user", "content": f"Feedback: {…}"})` **伪装成用户消息回灌**
（创作 agent 因此不需要额外状态）；停止条件写在 evaluator 的 instructions 里：
「Never give it a pass on the first try. After 5 attempts, you can give it a pass if the story
outline is good enough - **do not go for perfection**」
⇒ ⚠️ **预算写在 prompt 里，而不是用分数阈值当停止条件**（LLM 评委会持续小幅批评，阈值永远停不下来）。

⚠️ 官方**为什么**用代码 `while` 而不是接成 handoff / `as_tool()` —— 以及**本项目为什么仍选了 `as_tool`**：

| 官方不用它的理由 | 本项目（2026-10-02 用户拍板）怎么处理 |
|---|---|
| 接 handoff ⇒ 评审 agent 成为新的 active agent，**它会直接对用户说话**（评审语不是给用户看的）；且 handoff 单向，想回来得另注册一个反向 handoff | **同样不接 handoff**，这一点两边一致 |
| 接 `as_tool()` ⇒ 「要不要评审」交给创作 agent 自己判断 ⇒ 它可能跳过 | 用户明确要求**由模型决定**，接受这个代价；代码侧的补偿是 `exit_reason="unreviewed"` —— **跳过评审 ≠ 评审通过**，指标能看出跳过比例 |
| 代码 `while` 把循环放在**外部**，模型无法跳过 ⇒ 唯一能保证"评审一定会跑" | 承认保证不到了。**这是选择"模型自主"必须付的透明度成本，靠可观测性补，不靠强制** |

⇒ 本页这一节记录的是**官方做法与其理由**，不是本项目的结论。判据以
`DESIGN_agent_architecture.md` 文件头的「2026-10-02 修订」为准。

**另两种粒度的证据**：`agents_as_tools_conditional.py` 的三个语言 agent 各自 `instructions`
**只有一句话**、无任何工具 ⇒ 「一个功能 ＝ 一次独立模型调用能做完的事」，且 `is_enabled=` 收 callable
⇒ **工具清单随上下文动态变化**；`parallelization.py` 用 `asyncio.gather` **把同一个 agent 跑 3 次**
⇒ agent 是无状态配置对象、可复用（「一个功能一个 agent」≠「一次任务一个 agent」）。

---

## 5. 工具层

### 5.1 五类工具

| 类别 | 成员 | 执行位置 |
|---|---|---|
| **Hosted（OpenAI 托管）** | `WebSearchTool` / `FileSearchTool` / `CodeInterpreterTool` / `HostedMCPTool` / `ImageGenerationTool` / `ToolSearchTool` / `ProgrammaticToolCallingTool` | OpenAI 侧 |
| **Local runtime** | `ComputerTool` / `ShellTool` / `LocalShellTool` / `ApplyPatchTool` | 你的进程 |
| **FunctionTool** | 任意 Python 函数 | 你的进程 |
| **Agents as tools** | `agent.as_tool()` | 你的进程（嵌套 run） |
| **Experimental** | `codex_tool()` | 你的进程 |

⚠️ 一批能力**仅 Responses API 支持**，在 Chat Completions 上会被拒绝：
`ToolSearchTool`、`tool_namespace()`、`@function_tool(defer_loading=True)`、
`ProgrammaticToolCallingTool`、`tool_choice="programmatic_tool_calling"`。

### 5.2 `@function_tool` 从类型注解生成 schema

> 「Under the hood, we use Python's `inspect` module to extract the function signature, `griffe`
> for docstring parsing, and `pydantic` for creating the schema.」

- 工具名 = 函数名（`name_override=` 可覆盖），描述 = docstring；
- docstring 支持 `google` / `sphinx` / `numpy` 三种风格并自动探测；
- 参数描述可用 `Field(description=...)` 或 `Annotated[int, "…"]`；
- **可选把 run context 作为第一个参数**（类型注解 `RunContextWrapper[T]`）。

### 5.3 超时与失败

```python
@tool(timeout=2.0, timeout_behavior="error_as_result")   # 默认：超时作为结果回给模型
async def slow_lookup(query: str) -> str: ...

@tool(failure_error_function=my_custom_error_function)   # 默认：default_tool_error_function
def get_user_profile(user_id: str) -> str: ...
```

三种 `failure_error_function` 语义：**不传** ⇒ 默认函数把错误告知 LLM；**传自定义** ⇒ 用你的；
**显式传 `None`** ⇒ **重新抛出**（run 失败）。⚠️ 超时配置**仅支持异步** handler。

### 5.4 `tool_use_behavior`：工具结果去哪

| 取值 | 行为 |
|---|---|
| `"run_llm_again"`（默认） | 工具执行后交回 LLM 产出最终响应 |
| `"stop_on_first_tool"` | 第一个工具的输出**直接作为最终响应**，不再过 LLM |
| `StopAtTools(stop_at_tool_names=[...])` | 指定工具触发即停止 |
| `ToolsToFinalOutputFunction` | 自定义函数决定"结束"还是"继续" |

⚠️ 配套的 `reset_tool_choice` 默认 `True`：防止"工具结果→LLM 又发同一工具"的死循环。

### 5.5 结构化的延迟加载（Tool search）

大工具面不必一次性塞进上下文：`@tool(defer_loading=True)` + `tool_namespace(...)` +
`ToolSearchTool()`，模型**按需加载命名空间**。官方建议**每个命名空间少于 10 个函数**。

---

## 6. 记忆与状态：四种策略，选一种

> 「There are **four common ways** to carry state into the next turn.」

| 策略 | 状态存放处 | 适合 | 下一轮传什么 |
|---|---|---|---|
| `result.to_input_list()` | **你的内存** | 小聊天循环、要完全手动控制、任意 provider | 列表 + 新用户消息 |
| `session` | 你的存储 + SDK | 持久聊天状态、可恢复运行、自定义存储 | 同一个 session 实例（或指向同一存储的新实例） |
| `conversation_id` | OpenAI Conversations API | 服务端命名会话、跨 worker/服务共享 | **同一个 ID + 只传新用户轮** |
| `previous_response_id` | OpenAI Responses API | 轻量服务端续接，不建会话资源 | `result.last_response_id` + 只传新用户轮 |

**两条互斥规则（官方原文）**：

- 「**Session persistence cannot be combined** with server-managed conversation settings
  (`conversation_id`, `previous_response_id`, or `auto_previous_response_id`) in the same run.」
- 「`conversation_id` and `previous_response_id` are **mutually exclusive**.」

**内置 session 后端**：`SQLiteSession`（默认，文件或内存）、`AsyncSQLiteSession`、
`RedisSession`、`SQLAlchemySession`、`MongoDBSession`、`DaprSession`、
`OpenAIConversationsSession`、`OpenAIResponsesCompactionSession`、`AdvancedSQLiteSession`、
`EncryptedSession`（包装器）。

**默认存的是原始 items，不是摘要**。要压缩有三条路：`SessionSettings(limit=N)` 只取最近 N 条；
`OpenAIResponsesCompactionSession` 真做 clear-and-rewrite 压缩；
`RunConfig.session_input_callback` 自定义合并（**重排/过滤旧历史不会导致其被重新保存**）。

**自定义 Session 协议**（不必继承基类）：实现 `get_items` / `add_items` / `pop_item` /
`clear_session`。若四个方法都显式声明 `wrapper: RunContextWrapper | None = None` 关键字参数，
SDK 才会把 run context 传进来（**泛型 `**kwargs` 不满足签名检查**）。

⚠️ `SQLiteSession` **不做行级认证或篡改检测**；共享/不可信存储要用 `EncryptedSession`
（⚠️ 它**只加密，不校验完整性与顺序**）或自定义实现。

---

## 7. 护栏与人工介入

### 7.1 Guardrails：位置决定语义

| 位置 | 何时运行 | 关键约束 |
|---|---|---|
| **Input guardrail** | **默认与 agent 并行**（`run_in_parallel=True`） | ⚠️ **只对链上第一个 agent 生效** |
| **Output guardrail** | agent 完成后**串行** | ⚠️ **只对产出最终输出的 agent 生效**；**不支持并行** |
| **Tool guardrail** | 每次被守护的 function-tool 调用前后 | 不适用于 `HostedMCPTool` |

**乐观执行的代价（原文）**：
> 「with parallel execution, **the expensive model may already have started** before the guardrail
> completes… the agent may have **already consumed tokens and executed tools** before being cancelled.」

要省钱就 `run_in_parallel=False`（阻塞式，「the agent **never executes**, preventing token
consumption and tool execution」）。

**Tripwire 触发后的行为**：抛 `InputGuardrailTripwireTriggered` / `OutputGuardrailTripwireTriggered`，
**拒绝候选最终输出**（不写入 session），但**保留已完成的 tool call/output 与重放所需的元数据**。
⚠️ 原文警告：保留的重放元数据**可能仍含敏感数据**。

### 7.2 Human-in-the-loop：审批是 run 级，不是 agent 级

> 「That approval surface is **run-wide**，not limited to the current top-level agent.」

三种来源都走同一套：当前 agent 的工具 / handoff 之后到达的 agent 的工具 /
嵌套 `as_tool()` 内的工具。**恢复时必须用原始顶层 agent**。

五步流程：① 模型发出工具调用 → ② runner 评估审批规则 → ③ 需审批则**暂停**，
待批项进 `result.interruptions` → ④ `result.to_state()` + `approve/reject` → ⑤
`Runner.run(agent, state)` 从断点续跑。

⚠️ **fail-closed（原文）**：
> 「Callable approval rules **fail closed** when the SDK cannot safely inspect the arguments.」

参数缺失/空白/JSON 畸形/非对象/含 `NaN` 时，callable **不被调用**，直接要求人工审批。

⚠️ **`RunState` 的安全边界（原文）**：
> 「`RunState.from_json()` and `RunState.from_string()` **do not authenticate** the snapshot or the
> person submitting it. Only deserialize snapshots from **trusted storage**.」

**Sticky 决定**：`approve(interruption, always_approve=True)` / `reject(..., always_reject=True)`
会把决定持久化到 run state，**能挺过序列化往返**。Hosted MCP 的 sticky 身份 = `server_label` + 工具名。

⚠️ `ShellTool` / `ApplyPatchTool` 的 `needs_approval` **默认 `False`**，且**仅设 `on_approval`
不会启用审批** —— 必须同时设 `needs_approval`。⚠️ 官方明说：**SDK 的审批不等于沙箱**。

---

## 8. 上下文

**Context 是重载术语**，官方分两类：

| 类型 | 内容 | 是否会发给 LLM |
|---|---|---|
| **Local context** | `RunContextWrapper.context` / `ToolContext` | **不会**（纯本地依赖注入） |
| **Agent/LLM context** | instructions / input / tools / retrieval | 会（必须进入对话历史） |

> 「When an LLM is called, the **only** data it can see is from the **conversation history**.」

**让数据对 LLM 可见的四条路**：① 写进 `instructions`；② 加进 `input`；③ 通过
`FunctionTool` **按需**暴露；④ 用检索或 web search。

**编辑"即将发给模型的输入"的钩子**：

- `RunConfig.call_model_input_filter` —— 在**模型调用前**拿到的**完整输入**（含已合并的 session 历史），
  可裁剪历史 / 注入 system 提示 / 脱敏。必须返回 `ModelInputData`，**否则抛 `UserError`**。
- `RunConfig.session_input_callback` —— 在**更早**的"历史与本轮输入的合并"步骤上做定制。

**窗口管理的真实能力边界**（分析 + 官方事实）：

- 客户端侧**没有**自动裁剪算法；压缩靠 `OpenAIResponsesCompactionSession` 或
  `ModelSettings(truncation="auto")`（让 **Responses API 丢弃最旧项**）或自己写
  `call_model_input_filter`。
- 服务端侧有 `ModelSettings.context_management=[{"type": "compaction", "compact_threshold": ...}]`。
- 沙箱侧有独立的 `Compaction` capability。

---

## 9. 模型层

**默认模型**：未指定时用 `gpt-5.6-luna` + `reasoning.effort="none"` + `verbosity="low"`
（官方定位：**成本敏感、高并发**）。需要前沿能力时显式设 `gpt-5.6-sol`。

**三层指定优先级**：`RunConfig(model=...)`（运行级）> `Agent(model=...)`（agent 级）>
`OPENAI_DEFAULT_MODEL`（全局）。

**非 OpenAI 提供商**三条路：`set_default_openai_client`（全局）/ `ModelProvider`（运行级）/
`Agent.model`（agent 级）；适配器 `openai-agents[litellm]` 与 `openai-agents[any-llm]`（beta）。

⚠️ `OpenAIResponsesModel` 与 `OpenAIChatCompletionsModel` **能力集不同**，官方建议**单个工作流
统一用同一种形态**。Chat Completions 会**静默丢弃** Responses-only 字段；用
`strict_feature_validation=True` 可把这类警告升级为**错误**。

**`ModelSettings` 关键项**：`temperature` / `top_p` / `max_tokens` / `tool_choice`
（`auto` / `required` / `none` / 指定名）/ `parallel_tool_calls` / `truncation` / `store` /
`context_management` / `include_usage` / `timeout`（正数秒）/ `extra_args` / `extra_body` /
`reasoning` / `verbosity` / `retry`（`ModelRetrySettings`：`max_retries` / `backoff` / `policy`）。

**重试**支持策略组合：`retry_policies.any(provider_suggested(), retry_after(),
network_error(), http_status([408, 409, 429, 500, 502, 503, 504]))`。

---

## 10. 可观测性

### 10.1 Tracing：默认开启并上传

> 「the default… `BatchTraceProcessor`… export them **to the OpenAI backend**」
> 「Tracing is **enabled by default**.」

- 关闭：`OPENAI_AGENTS_DISABLE_TRACING=1` / `set_tracing_disabled(True)` /
  `RunConfig.tracing_disabled=True`。
- ⚠️ **ZDR（Zero Data Retention）组织无法使用 tracing**。
- **默认 trace 粒度**：整个 `Runner.run()` 是一个 trace；每次 runner 调用 = `task_span`；
  每个模型 turn = `turn_span`；另有 `agent_span` / `generation_span` / `function_span` /
  `guardrail_span` / `handoff_span`。可用
  `RunConfig(tracing={"include_task_and_turn_spans": False})` 收敛层级。
- **敏感数据默认上传**：`RunConfig.trace_include_sensitive_data` **默认 `True`**；
  `generation_span` 存 LLM 输入输出，`function_span` 存工具输入输出；音频 span 默认含 base64 PCM。
- ⚠️ **脱敏的顺序陷阱（原文）**：trace processor 是**独立观察者**，
  「**先注册 redaction processor 并不能阻止后续 exporter 收到数据**」。正确做法是用
  `set_trace_processors()` **替换**默认处理器，并在**自己的 exporter 内**完成"复制 → 脱敏 → 投递"，
  失败即**丢弃整批**且**只记固定失败文案**（不带 payload / 异常文本 / traceback）。
- 长时 worker（Celery/RQ/FastAPI 后台任务）里要显式 `flush_traces()`。

### 10.2 Usage

`result.context_wrapper.usage` 提供 `requests` / `input_tokens` / `output_tokens` /
`total_tokens` / `request_usage_entries`（逐请求明细），以及细分子项：
`input_tokens_details.cached_tokens`、`input_tokens_details.cache_write_tokens`、
`output_tokens_details.reasoning_tokens`。

> 「Usage is **aggregated across all model calls** during the run, including model calls that
> produce tool calls or **handoffs**.」

⚠️ **session 不累计 usage**：每次 `Runner.run()` 的 usage 只代表该次执行（虽然历史仍在）。
⚠️ 从 `RunState` 恢复时 usage 是**独立快照**，不会回写原始 `RunResult`；
**例外**：嵌套 `as_tool()` 恢复后的用量会被**刻意聚合到外层 run**。

---

## 11. Sandbox agents（新特性）

### 11.1 定位

> 「Modern agents work best when they can operate on **real files in a filesystem**.」

`SandboxAgent` **仍然是一个 `Agent`**，保留 `instructions` / `tools` / `handoffs` /
`mcp_servers` / `model_settings` / `output_type` / guardrails / hooks，也仍走 `Runner`。
**变的是执行边界**：多了一个**持久、可变的文件系统工作区**。

**选择判据（原文）**：
> 「If you do **not** need access to files or a stateful, mutable filesystem, **keep using `Agent`**.
> If shell access is just one **occasional** capability, add **hosted shell**; if the **workspace
> boundary itself is part of the feature**, use sandbox agents.」

### 11.2 Manifest 与 Capabilities

`Manifest` 声明**全新会话**的工作区契约（`root`、文件/目录、`LocalFile`/`LocalDir`、
`GitRepo`、云挂载、环境变量、用户组、`extra_path_grants`）。

⚠️ **路径安全（原文）**：「Manifest entry paths are **workspace-relative**. They **cannot be
absolute paths or escape the workspace with `..`**.」⚠️ 含 `extra_path_grants` 的 manifest 应视为
**可信配置**，**不得**从模型输出加载。

| Capability | 作用 | 依赖 |
|---|---|---|
| `Shell` | 加 `exec_command`（支持 PTY 时加 `write_stdin`） | — |
| `Filesystem` | 加 `apply_patch` / `view_image` | — |
| `Skills` | 索引并物化 skills 到沙箱（**替代手动挂载 `.agents`**） | — |
| `Memory` | 跨 run 读取/生成 memory 产物 | 需要 `Shell`；运行时更新还需 `Filesystem` |
| `Compaction` | 长流程在 compaction items 后裁剪上下文 | — |

默认 `Capabilities.default()` = `Filesystem() + Shell() + Compaction()`；
⚠️ **一旦显式传 `capabilities=[...]` 就整体替换默认值**。

### 11.3 隔离模型（必须读）

> 「The isolation provided by a session **depends on its backend**… a session does **not itself
> guarantee OS-level confinement**.」

| 后端 | 隔离强度 |
|---|---|
| `UnixLocalSandboxClient` | **Linux：命令就是主机进程，无 OS 级限制**；macOS：`sandbox-exec` 限文件系统但**无网络隔离** |
| `DockerSandboxClient` | 用于**不可信命令**；**Windows 上必须用 Docker 或托管 provider** |

⚠️ 关键提醒：**本地 `extra_path_grants` 在 Linux 上不约束任意 shell 命令**。
⚠️ **snapshot 只含 workspace root**，额外授权路径是**运行时访问，不是持久状态**。

---

## 12. 工程坑清单（按踩中概率排序）

| # | 坑 | 事实 |
|---|---|---|
| 1 | 以为 input guardrail 每跳都跑 | **只对链上第一个 agent 生效**；output 只对最后一个 |
| 2 | 用 `is_enabled` 做授权 | 它在模型返回参数**之前**求值，**拿不到参数**；授权放 `on_handoff` 并 raise |
| 3 | 以为 `as_tool` 的嵌套 run 共享会话 | **默认不继承**父 run 状态，要显式传同一个 `session` |
| 4 | session 与 `conversation_id`/`previous_response_id` 混用 | **同一次 run 互斥**，会重复上下文 |
| 5 | 以为 tracing 是本地日志 | **默认上传 OpenAI 后端**；且 ZDR 组织**不可用** |
| 6 | 先注册脱敏 processor 就以为安全 | 处理器是**独立观察者**，必须 `set_trace_processors()` **替换**并在自己的 exporter 内脱敏 |
| 7 | 从不可信来源反序列化 `RunState` | 官方明确：**不认证快照与提交者**，只从可信存储反序列化 |
| 8 | 以为 SDK 审批等于沙箱 | 官方原话区隔；`UnixLocal` 在 Linux 上**无 OS 级限制** |
| 9 | `max_turns` 当成"对话轮数" | 单位是 **LLM 调用次数** |
| 10 | 用 `Agent.clone()` 后就改 tools | **浅拷贝**：未覆盖的 list 属性仍与原件**共享容器** |
| 11 | 流式里不等迭代器结束 | 「A streaming run is **not complete until the iterator ends**」——session 持久化等后处理在最后一个 token **之后** |
| 12 | 非 Responses provider 用托管工具 | `ToolSearchTool` / `tool_namespace` / `programmatic_tool_calling` 等**仅 Responses 支持** |

---

## 13. 两条对照线

### 13.1 Agents SDK vs Responses API

| 选它 | 选 Responses API |
|---|---|
| 想让 runtime 管 turn / 工具执行 / 护栏 / handoff / session | 想**自己拥有** loop、工具分发与状态处理 |
| agent 要产出 artifacts 或跨多个协同步骤 | 工作流**短命**，主要就是返回模型响应 |
| 需要真实工作区或可恢复执行（Sandbox） | — |

官方明确：**不必全局二选一**，「Many applications use the SDK for managed workflows and call
the Responses API directly for lower-level paths.」

### 13.2 与 LangGraph 的机制差异（分析，非官方）

| 维度 | OpenAI Agents SDK | LangGraph |
|---|---|---|
| 图结构 | **不存在**。只有一条 loop + 工具集 | 显式 `StateGraph`：节点 + 边 + 条件边 |
| 分流机制 | 模型在运行时**选出**要调用的工具（含 handoff） | 开发者**预先定义**条件边与路由函数 |
| 状态 | SDK 侧 `Session` / `RunState`；agent 本身无状态 | 图 state + checkpointer（可换后端） |
| 可恢复性 | `RunState` 序列化 + HITL 审批 | `interrupt` + checkpointer |
| 中断语义 | 审批为 **run 级**，恢复用**顶层 agent** | `interrupt` 在节点内，按 `thread_id` 恢复 |
| 确定性 | LLM 决策式偏弱；代码式可恢复 | **结构上强**（边是代码） |
| 学习成本 | 低（原语少） | 中（需理解 state/reducer/checkpoint 语义） |
| 适合 | 开放式任务、要快速起步、需要托管能力 | 需要精确控制流程、自定义状态流转、长期演进 |

**分析**：两者不是"新旧"关系，而是**两个正交的取舍点**：

- Agents SDK 把不确定性交给**模型**（原语少、涌现式）；
- LangGraph 把不确定性交给**开发者**（结构显式、可复现）。

真正需要警惕的不是"选错框架"，而是**同时用两套心智模型**：用 LangGraph 的显式图
去实现一件本该由模型自己选的事 —— 那就是 OpenAI 官方批评的 declarative 反模式。

---

## 附：环境变量速查

| 变量 | 作用 |
|---|---|
| `OPENAI_API_KEY` | LLM 请求与 tracing 的默认 key（**懒初始化**，须在首次模型调用前设好） |
| `OPENAI_DEFAULT_MODEL` | 全局默认模型 |
| `OPENAI_BASE_URL` / `OPENAI_WEBSOCKET_BASE_URL` | 端点覆盖 |
| `OPENAI_AGENTS_DISABLE_TRACING=1` | 全局关 tracing |
| `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA=0` | 排除敏感数据 |
| `OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1` / `OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1` | 默认不记录模型/工具输入输出 |
| `OPENAI_AGENTS_ENABLE_LITELLM_SERIALIZER_PATCH=true` | LiteLLM Pydantic 序列化兼容补丁 |

## 附：全局配置 API

`set_default_openai_key()` · `set_default_openai_client(client, use_for_tracing=...)` ·
`set_default_openai_api("chat_completions")` · `set_tracing_export_api_key()` ·
`set_tracing_disabled(True)` · `set_trace_processors([...])` · `add_trace_processor(...)` ·
`enable_verbose_stdout_logging()` · `set_default_openai_responses_transport("websocket")` ·
`flush_traces()`

**配置对象与字典互换**：SDK 定义的配置参数一般同时接受 settings 对象或**同字段字典**，
字典会被 normalize；**未知字段抛 `TypeError`**（可早期发现拼写错误）。
