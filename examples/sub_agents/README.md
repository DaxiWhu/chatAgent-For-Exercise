# examples/sub_agents —— 按官方原语装配的多专家参考实现

> ## ⚠️⚠️ 本目录是**修订前**的历史参考，不是当前生产形态
>
> 它的设计早于两次修订（2026-10-02 与 2026-10-03），**与 `src/agents/` + `src/graph.py`
> 已经不一致**。读它之前先知道这四处差异（细节见
> [`DESIGN_agent_architecture.md`](../../DESIGN_agent_architecture.md) §8 缺口 1）：
>
> | 本目录（旧） | 生产实现（现在） |
> |---|---|
> | 文学专家走 `handoff()`（`frontline.py` 里还有回程边） | 文学是**图里的一个独立节点**，由前台的结构化判定经条件边路由过去；**没有回程边**，链路里**一处 handoff 都不剩** |
> | 评审是代码 `while`（每轮必跑、`MAX_ATTEMPTS` 兜底） | 评审是挂在产出方身上的 `as_tool`，**调不调由模型决定** ⇒ 多一档 `exit_reason="unreviewed"` |
> | 跨轮历史靠局部变量 `result.to_input_list()` | 官方 `Session`（`Runner.run(session=...)`）+ PostgreSQL 后端 |
> | 入口跟随上一轮结束点（`current_agent = result.last_agent`） | 每轮入口**固定为前台** |
>
> `selftest` / `mutate` 全绿只说明**这个目录自己自洽**，**不能**当作"当前设计正确"的证据。
> 判据一律以 `DESIGN_agent_architecture.md` 与 `src/agents/` 为准。
>
> 设计判据与取舍见根目录 **[`DESIGN_agent_architecture.md`](../../DESIGN_agent_architecture.md)**；
> 本文件只讲"怎么跑、里面有什么"。
>
> ⚠️ 本目录**取代**了旧的 `examples/task_routing/`（那套是"四条预定义支路"的 workflow 写法，
> 已被归档到 `examples/_archive/task_routing/`）。

## 跑法

```bash
pip install openai-agents          # 示例本身不需要 API key

python -m examples.sub_agents.demo       # 四条典型请求，离线演示
python -m examples.sub_agents.selftest   # 53 条断言：拓扑 + 行为
python -m examples.sub_agents.mutate     # 6 组变异：把实现改坏，确认断言会红
```

演示与断言都跑在官方 `agents.testing.ScriptedModel` 上（**确定性、零网络、零密钥**）。
要接真模型，把 `model=ScriptedModel(...)` 去掉即可 —— 默认走 `OPENAI_DEFAULT_MODEL`。

## 四类诉求 → 四种不同原语

| 用户诉求 | 落到什么原语 | 接线 | 为什么不是"一律做成子 agent" |
|---|---|---|---|
| 创作 · 文学 | `Agent` + **`handoff()`** | 专家接管对话 | 作品就是交付物，用户会就它继续追问 |
| 创作 · PPT / 插图 | `Agent` + **`Agent.as_tool()`** | 前台保留控制权 | 产出是结构化大纲 / 资产路径，要由前台汇总 |
| 回答 · 资料问答 | 前台自己的 `tools` + **`as_tool` 的取证专家** | 前台保留控制权 | "回答问题"本来就是前台的职责，单独立一个 agent 只会重复 |
| 评估 | **第二个 `Agent` + 代码 `while`** | 代码调用 | 交给模型决定 ⇒ 它会在对自己不利时跳过评审 |
| 校验 | **`@input_guardrail` / `@output_guardrail`** | 官方独立原语 | 做成 agent 就变成"被校验者决定要不要校验" |
| 解析 / 渲染 / 落盘 | **普通函数（连工具都不是）** | 代码 | 没有模型需要"决定要不要渲染" |

## 文件清单

| 文件 | 职责 | 关键点 |
|---|---|---|
| `frontline.py` | 唯一对用户说话的 agent | `tools=[as_tool 专家]`、`handoffs=[文学专家]`、回程边 |
| `specialists.py` | 四个专家 agent | 三个走 `as_tool`（产出结构化），一个走 `handoff`（产出自由文本） |
| `evaluator.py` | 评审 agent | 结构化 `EvaluationFeedback{feedback, score}`；停止条件写在 instructions 里 |
| `guards.py` | 入口 / 出口护栏 | 全部确定性判据，不花模型调用 |
| `runner.py` | 代码编排 | `current_agent = result.last_agent`、反馈伪装成 user 消息回灌 |
| `context.py` | 共享上下文 `TaskContext` | 对照官方 `AirlineAgentContext`；handoff 只传上下文变更 |
| `types.py` | 结构化输出类型 | 给代码消费的环节必须结构化；给用户看的环节不该结构化 |
| `tools.py` | 普通 function tools | 检索 / 落盘 / 出图占位；**渲染不做成工具** |
| `scripting.py` | `ScriptedModel` 步骤辅助 | 只是把官方 `assistant_message`/`function_call` 包成步骤 |
| `selftest.py` | 53 条断言 | A 拓扑 + B~F 行为；关键断言落在"模型实际收到的输入"上 |
| `mutate.py` | 变异验证 | 预检唯一匹配 + SHA-256 自证还原 + `newline=""` |

## 这套实现自己抓出来的一个设计缺陷

`selftest` 的 B8 第一次跑就红了：文学专家的 instructions 里写着
「用户想换题目以外的任务时，转回 frontline_agent」，但它**没有注册回程 handoff** ——
handoff 天然是单向的，那句话是一句**做不到的承诺**。

官方 `customer_service`（airline）里，两个专家都做了
`faq_agent.handoffs.append(handoff(agent=triage_agent, ...))`，正是为此。
`frontline.py` 里现在补上了同一条反向边，并由 B8 / M3 两条钉死。

> 这类"提示词里承诺了、拓扑上没接"的错配，靠读代码几乎发现不了 —— 只有断言抓得到。
