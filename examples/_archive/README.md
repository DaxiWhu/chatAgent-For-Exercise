# _archive —— 被取代的实施方案

这里的文件**不是待办，是历史**。保留是为了对照，不要在此基础上继续改。

| 归档内容 | 被谁取代 | 为什么被取代 |
|---|---|---|
| `task_routing/`（9 文件）+ `PLAN_task_routing.md` | `examples/sub_agents/` + `DESIGN_agent_architecture.md` | 那套是"四条预定义支路 + 条件边"的 **workflow 编排**写法：`task` 单值决定走向、四条支路维度不统一、且共享约 80% 节点。OpenAI 官方明确批评这类 declarative 做法"随 workflow 变复杂会迅速变得笨重"。新方案改成**一条 loop + 官方五原语**。 |

归档内容仍然可跑（`python -m examples._archive.task_routing.demo`），
但 `examples/_archive/` 不在任何测试与 CI 的覆盖范围内。

确认不需要后可以直接删除整个目录。
