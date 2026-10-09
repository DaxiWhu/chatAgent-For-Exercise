"""任务分流参考实现（**最终形态样例**）。

定位
----
这是「一个成熟 agent 的任务分流最终应该长什么样」的完整可运行样例：
策略对象、三层路由、四条支路、统一出口、预算与退出归因，全部到位。

⚠️ 它**不是**生产接线。生产落地时把这个包并进 `src/`（`router.py` / `graph.py`），
并按 `README.md` §「接入生产」的清单同步测试、指标与告警。放在 `examples/` 下
的另一个原因：`examples/` 在本项目里就是"参考实现、不属于生产链路"的定位
（见 `examples/chroma_save_test.py` 的头部说明）。

跑一遍
------
    python -m examples.task_routing.demo
"""
