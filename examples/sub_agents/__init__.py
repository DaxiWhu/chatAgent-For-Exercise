"""按 OpenAI Agents SDK 官方原语装配的多专家 sub-agent 参考实现（离线可跑）。

本包**不属于生产链路**（与 `examples/chroma_save_test.py` 同一定位）。
它存在的唯一目的：把「创作类 / 回答类 / 评估 / 校验」这四类诉求，
按官方判据分别落到**不同的原语**上，而不是一律做成子 agent。

跑法::

    python -m examples.sub_agents.demo       # 三条典型请求，离线演示
    python -m examples.sub_agents.selftest   # 结构断言（拓扑 / 边界 / 权限）
    python -m examples.sub_agents.mutate     # 变异验证：把实现改坏，确认 selftest 会红

依赖：``pip install openai-agents``（示例本身不需要 API key）。
"""

from __future__ import annotations
