"""出站调用的韧性：熔断器 + 带退避的重试。

为什么需要熔断（而重试不够）
--------------------------
只加重试会**放大故障**：上游 embedding / rerank 网关挂了，每个请求都重试 3 次，
故障期间打到上游的流量变成 3 倍，对方更难恢复，本服务的线程也被拖住。
熔断器在"连续失败达阈值"后**直接快速失败**（不发出请求），隔一段时间放一个
试探请求过去；成功了再恢复。这样既保护上游，也把本服务的失败延迟从"超时"
降到"毫秒级"。

为什么是**实例级**而不是全局单例
------------------------------
本项目已有大量离线测试，全局熔断状态会**跨用例串味**（上个用例把熔断打开了，
下个用例以为服务挂了）。所以每个客户端（RerankClient / 检索器）各自持有一个
breaker，生命周期跟着实例走，测试天然隔离。

重试与熔断的分工
--------------
- **重试**：处理"偶发失败"（网络抖动、上游瞬时 5xx）。
- **熔断**：处理"持续故障"（上游整体不可用）。
两者组合的正确姿势是：**先重试、重试仍失败才记为一次熔断失败**
（否则一次抖动就被算成 N 次失败，阈值瞬间打满）。`guarded_call` 在
`retries > 1` 时就是这个语义。

⚠️ **但它在生产上从未生效**（2026-10-06 记录，不是 bug 修复，是口径澄清）
--------------------------------------------------------------------
`guarded_call(..., retries=1)` 是**默认值**，而全项目的调用点
（`retrieval.py` 的 5 处 embed / rerank）**一个都没有传 `retries`** ⇒ 走的是
`else: result = fn(...)` 那条分支，`retry_call` 一次都没被调用过。
也就是说线上真实跑的是：

    一次失败 = 记一次熔断失败（阈值是按"每次失败"计的，不是按"一轮重试"）

后果需要说清楚，免得有人按错的口径去调参：
- `CIRCUIT_FAIL_THRESHOLD=5` 在**默认配置**下表示"连续 5 次请求各失败一次就打开"，
  而不是"连续 5 轮重试都失败"——两者在抖动场景下能差出一个数量级；
- 上游偶发失败**不会被自动重试**，会直接降级（embedding 失败 ⇒ 灌库失败；
  rerank 失败 ⇒ 静默退回融合顺序，见 `retrieval.py` 的 `rerank_fallback_total`）。

为什么**不**顺手把默认改成 2：那会改变生产行为（上游抖动时每个请求多打一次，
首轮失败后的额外等待变成 0.5s + 抖动），这是配置决策而不是修 bug ——
真要开，就在调用点显式写 `retries=N` 并同时调 `CIRCUIT_FAIL_THRESHOLD`
（重试次数与阈值必须一起想，否则"5 次"的含义会跟着变）。
本模块保留 `retry_call` 与 `retries` 参数，是为了让那个选择**随时可做**，
而不是为了假装它已经做了。
"""

import logging
import random
import time

from .. import observability

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """熔断打开时抛出：表示"没有真的发请求，直接失败"。

    调用方可以据此区分"熔断快速失败"与"真实调用失败"（本项目的检索链路两者
    都走同一个降级分支，但日志里区分开更利于排障）。
    """


class CircuitBreaker:
    """三态熔断器：closed（正常）→ open（直接失败）→ half_open（试探）。"""

    def __init__(self, name, fail_threshold=5, reset_seconds=30, enabled=True):
        """构造。

        name: 熔断器名字（日志用，如 "rerank" / "embedding"）。
        fail_threshold: 连续失败多少次后打开。
        reset_seconds: 打开后隔多久放一个试探请求（进入 half_open）。
        enabled: False 时 allow 恒 True、不计数（等价于关掉熔断）。
        """
        self.name = name
        self.fail_threshold = max(1, int(fail_threshold))
        self.reset_seconds = max(1, int(reset_seconds))
        self.enabled = bool(enabled)
        self.state = CLOSED
        self.failures = 0
        self.opened_at = 0.0
        self.open_count = 0            # 累计打开次数（观测用）

    def _publish_state(self) -> None:
        """把"是否已闭合"播报成指标（0/1）。

        为什么必须有这条指标
        ------------------
        熔断打开的行为是**快速失败**：调用方捕获后走降级分支，接口照常返回
        200、答案照常生成（只是变成"依据不足"）。所以整条链路上**没有任何
        报错**，面板全绿 —— 唯一能反映"检索正在被熔断"的就是熔断器自己的状态。

        为什么是 0/1 而不是三态枚举
        ------------------------
        告警表达式要一眼看懂：`circuit_open == 1` 比 `circuit_state > 0` 少一层
        心智负担。**half_open 同样算作"未闭合"** —— 只报 open 会让"长期卡在
        half_open 反复试探"（上游始终没真正恢复）这段完全不告警。

        为什么不带 component / backend 之类维度：breaker 已经是有限取值
        （"rerank" / "embedding"），再加维度只会让标签基数无谓地翻倍。

        ⚠️ 标签名为什么叫 `breaker` 而不叫 `name`（2026-10-02 实测踩过）
        ------------------------------------------------------------
        `app/metrics.py` 的读写函数第一个形参就叫 `name`
        （`gauge(name, **labels)` / `inc(name, amount=1, **labels)`），
        标签里再出现 `name=` 会在**调用点**当场抛出
        `TypeError: gauge() got multiple values for argument 'name'`。

        ⚠️ 这里**不是**"被兜底吞掉、只是指标没了"——2026-10-02 用探针确认过：
        异常抛在 `observability.gauge(...)` 这一行本身，`_dispatch` 的兜底
        `except` **根本轮不到**。后果比"没指标"严重得多：`record_success()` 在
        **每一次成功调用**后都会走到这里 ⇒ 异常穿透 `guarded_call` ⇒ 被检索
        调用方的宽 `except` 接住 ⇒ **整条检索永久降级为"无上下文"**
        （用户看到的是"依据不足"，而当时这条降级路径连指标都没有）。
        所以保留名 `name` 一律不能当标签键。`value` / `amount` 对同族函数同理
        （`histogram(name, value, …)` / `inc(name, amount=1, …)`）。
        `tests/test_runtime_metrics.py::test_metric_labels_never_shadow_metrics_parameter_names`
        是一条扫源码的 AST 守卫，钉住这条规则。
        """
        observability.gauge("circuit_open", 0 if self.state == CLOSED else 1,
                            breaker=self.name)

    def allow(self) -> bool:
        """判断本次调用是否可以真的发出去。

        返回 True（放行）或 False（熔断中，调用方应直接失败）。
        open 状态到点后会转为 half_open 并**只放行这一次**试探。
        """
        if not self.enabled:
            return True
        if self.state == CLOSED:
            return True
        if self.state == OPEN:
            if time.time() - self.opened_at >= self.reset_seconds:
                self.state = HALF_OPEN
                self._publish_state()
                logging.info("熔断器 %s 进入半开，放行一个试探请求", self.name)
                return True
            # 被挡下的这一次：计数。它与 open 状态是两件事 ——
            # 状态告诉你"现在开着"，计数告诉你"已经白白失败了多少次调用"。
            observability.counter("circuit_rejected_total", breaker=self.name)
            return False
        # HALF_OPEN：放行试探（失败会立刻重新打开）
        return True

    def record_success(self):
        """一次成功：清空失败计数并闭合。"""
        if not self.enabled:
            return
        if self.state != CLOSED:
            logging.info("熔断器 %s 试探成功，恢复闭合", self.name)
        self.state = CLOSED
        self.failures = 0
        self._publish_state()

    def record_failure(self):
        """一次失败：累计；达阈值则打开。half_open 下失败立即重新打开。"""
        if not self.enabled:
            return
        self.failures += 1
        if self.state == HALF_OPEN or self.failures >= self.fail_threshold:
            self.state = OPEN
            self.opened_at = time.time()
            self.open_count += 1
            observability.counter("circuit_opened_total", breaker=self.name)
            self._publish_state()
            logging.warning("熔断器 %s 打开（连续失败 %d 次，%ds 后试探）",
                            self.name, self.failures, self.reset_seconds)

    def snapshot(self) -> dict:
        """导出当前状态（给健康检查/日志用）。"""
        return {"name": self.name, "state": self.state, "failures": self.failures,
                "open_count": self.open_count}


def retry_call(fn, attempts=3, base_delay=0.5, max_delay=8.0, jitter=0.2,
               exceptions=(Exception,), on_error=None):
    """带指数退避 + 抖动的重试。

    fn: 无参可调用（用 lambda/partial 包好参数再传）。
    attempts: 最多尝试几次（含首次）。
    base_delay: 首次退避基数（秒），之后按 2 的幂增长。
    max_delay: 单次退避上限。
    jitter: 抖动比例（0~1），避免多个客户端同时重试形成尖峰。
    exceptions: 需要重试的异常类型元组。
    on_error: 可选回调 `on_error(attempt, exc)`，用于日志。

    全部尝试都失败时**抛出最后一次异常**（不吞），由调用方决定降级方式。
    """
    last = None
    for i in range(max(1, int(attempts))):
        try:
            return fn()
        except exceptions as e:  # noqa: PERF203
            last = e
            if on_error:
                on_error(i + 1, e)
            if i == max(1, int(attempts)) - 1:
                break
            delay = min(max_delay, base_delay * (2 ** i))
            delay *= 1 + random.uniform(-jitter, jitter)
            time.sleep(max(0.0, delay))
    raise last


def guarded_call(breaker, fn, *args, retries=1, **kwargs):
    """在熔断器保护下调用 `fn(*args, **kwargs)`。

    breaker: CircuitBreaker。
    fn: 目标可调用。
    retries: 允许的重试次数（默认 1 = 不重试，保持既有行为）。
        传 >1 时**先重试、重试全失败才记一次熔断失败**（见模块 docstring）。

    熔断打开时抛 `CircuitOpenError`；否则原样返回 fn 的结果或抛出其异常。
    """
    if not breaker.allow():
        raise CircuitOpenError(f"{breaker.name} 熔断中，快速失败")
    try:
        if retries and int(retries) > 1:
            result = retry_call(lambda: fn(*args, **kwargs), attempts=int(retries))
        else:
            result = fn(*args, **kwargs)
    except Exception:
        breaker.record_failure()
        raise
    else:
        breaker.record_success()
        return result
