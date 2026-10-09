"""三层路由：规则 → 缓存 → LLM（默认关）。

判定顺序的两条硬规则
--------------------
1. **便宜在前。** 简单问题占 60~80% 的流量；如果每个请求都先付一次模型调用去分类，
   快路径省下的检索开销会被路由本身吃回去。
2. **L2（LLM 分类）默认关。** 判官式路由在每个请求上多付 **50~300ms**；在一个 30 步的
   agent 里这是 **4.5 秒纯路由开销**。本项目收益主要来自"跳过检索"这一件事，
   不值得为它引入一次模型调用。（开关留给 `mode`，但默认值不含 `llm`。）

三层各自的判据
--------------
| 层 | 成本 | 判据 |
|---|---|---|
| L0 规则 | μs / $0 | 续问标记、写作/编码/检索指令、纯寒暄、长度 |
| L1 缓存 | ms | query **归一化指纹**命中历史判定 |
| L2 LLM | 50~300ms | 结构化输出（默认关，`mode` 里显式加 `+llm` 才启用） |

未命中任何一层 ⇒ 走 **fallback（默认 `rag`）**，不是 `direct`：见 `plan.DEFAULT_FALLBACK`
的代价不对称说明。

路由器只读不写（硬规则）
------------------------
`decide()` **绝不修改 query**，也不改写要发给生成模型的 prompt。原因是可量化的：

> 分类时改写或注入前言的 prompt 会让**每个请求的前缀都不同**，破坏 KV-cache 的
> 前缀复用，直接丢掉稳定前缀带来的 **60~90% 输入成本削减**。

附带的好处是"模型实际看到了什么"保持可复现 —— 而排查答案质量时，第一件要看到的就是它。
"""
from __future__ import annotations

import re
import time
from collections import OrderedDict

from .plan import DEFAULT_FALLBACK, ROUTES, make_plan

# ---------------------------------------------------------------------------
# L0 规则表
# ---------------------------------------------------------------------------
# ⚠️ **顺序即优先级**，下面的 decide_l0 严格按此顺序短路。
# "续问"必须排在"短文本判直答"之前 —— 否则「再详细点」（4 个字）会被判成 direct，
# 而它恰恰是**最需要继承上一轮 needs** 的一类。
_GREETING = re.compile(
    r"^(你好|您好|hi|hello|hey|在吗|在么|早上好|中午好|下午好|晚上好|"
    r"谢谢|多谢|感谢|thx|thanks|ok|好的|收到|明白|辛苦了)\s*[!！。.~,，]*$",
    re.I,
)
_CONTINUE = re.compile(r"^(再|继续|接着|展开|详细|多说|说下|往下|然后呢|还有呢|再来)")
_URL_OR_FILE = re.compile(
    r"https?://|www\.|[\w\-]+\.(pdf|docx?|txt|md|markdown|xlsx?|csv|pptx?|png|jpe?g)\b", re.I
)
_RETRIEVAL_CUE = re.compile(
    r"(根据|依据|参考|查阅|查一下|查查|找一下|搜一下|资料|文档|知识库|手册|规范|"
    r"说明书|报告里|文件里|出处|引用)"
)
_WRITE_CUE = re.compile(
    r"(写一篇|写一份|写个|帮我写|起草|撰写|拟定|拟一[份个篇]|润色|改写|扩写|"
    r"缩写成|翻译成|生成一篇|出一[份个]文案)"
)
# ⚠️ 「运行」这类**光杆动词**不能单独进规则表。实测：一句
# 「了解这个系统在长时间**运行**之后内存会不会增长」会被判成写代码支路，
# 然后被送进沙箱 —— 这就是把误判从"慢一点"变成"跑一段用户没要求的代码"。
# 规则必须锚在**动作 + 代码对象**上（运行*这段*、跑*个测试*、含 ``` 或函数定义）。
# 这是 L0 最容易踩的一类坑：宽泛的词命中率高、看起来效果好，代价全在误判上。
_CODE_CUE = re.compile(
    r"(运行一下|运行这段|运行这个|运行看|执行一下|执行这段|执行这个|跑一下|跑跑看|"
    r"这段代码|这个函数|这个报错|报什么错|调试|debug|traceback|异常栈|"
    r"单元测试|单测|跑测试|编译错误|类型错误|NullPointer|"
    r"```|def\s+\w+\(|class\s+\w+[:(])"
)


class DecisionCache:
    """L1 判定缓存：query 指纹 → plan 摘要。

    ⚠️ **必须有界**。key 直接来自请求，无上限意味着"构造一批陌生 query"就能吃内存 ——
    那是 DoS 面，不是功能。同 `src/graph.py::get_retriever` 的 `RAG_TENANT_CACHE_MAX`
    是同一个理由。

    淘汰用 FIFO 而不是 LRU：这里的访问分布是"一批热点 query 长期活跃"，
    FIFO 足够，而 LRU 要额外维护访问顺序（并发下还要加锁），复杂度换不来实际收益。
    """

    #: 归一化：去掉空白与全部标点，只留字母/数字/汉字。
    _BLANK = re.compile(r"[\s\u3000]+")
    _PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+")

    def __init__(self, size=512):
        self.size = max(1, int(size))
        self._d = OrderedDict()

    @classmethod
    def fingerprint(cls, query) -> str:
        """把 query 归一成缓存键。

        归一化的三个动作（去空白 / 去标点 / 小写）都**只作用于缓存键**，
        不作用于传给下游的 query —— 见模块头"路由器只读不写"。
        """
        s = cls._BLANK.sub("", (query or "").strip().lower())
        return cls._PUNCT.sub("", s)

    def get(self, query):
        key = self.fingerprint(query)
        if not key:
            return None
        hit = self._d.get(key)
        if hit is None:
            return None
        return dict(hit)          # 返回副本：调用方改它不该污染缓存

    def put(self, query, value):
        key = self.fingerprint(query)
        if not key:
            return
        self._d[key] = dict(value)
        self._d.move_to_end(key)
        while len(self._d) > self.size:
            self._d.popitem(last=False)

    def clear(self):
        self._d.clear()

    def __len__(self):
        return len(self._d)


def decide_l0(query, prev_plan=None, direct_max_chars=40):
    """L0 规则层。返回 `(task, why, confidence)`；**未命中返回 `(None, "", 0.0)`**。

    query: 用户原文（原样，不 strip 后回写 —— 只是本地判）。
    prev_plan: 上一轮的 plan（从 checkpoint 恢复）。续问要用它做**继承**。
    direct_max_chars: 超过这个长度不可能是寒暄/续问（长度判据的上限）。
    """
    q = (query or "").strip()
    if not q:
        return "direct", "空输入", 1.0

    # ① 续问：**必须最先判**。它决定的是"继承上一轮的 needs"，而不是"这一句本身像什么"。
    #    用户先说"帮我查 X"，拿到回答后说"再详细点" —— 单看这句是简单续问，
    #    实际必须继承上一轮的 retrieval。这正是 plan 要跨轮保留的原因。
    if _CONTINUE.search(q):
        if isinstance(prev_plan, dict) and prev_plan.get("task") in ROUTES:
            return prev_plan["task"], f"续问，继承上一轮 {prev_plan['task']}", 0.9
        return "rag", "续问但无上一轮，按 fail-safe 走检索", 0.5

    # ② 指令类：这三条都是**显式**的能力声明，比长度/礼貌判据硬得多。
    if _CODE_CUE.search(q):
        return "code", "命中编码/执行指令", 0.85
    if _WRITE_CUE.search(q):
        return "write", "命中写作指令", 0.85
    if _RETRIEVAL_CUE.search(q) or _URL_OR_FILE.search(q):
        return "rag", "命中检索/引用指令或含文档标识", 0.85

    # ③ 纯寒暄：短、无信息量、单次即出。
    if len(q) <= direct_max_chars and _GREETING.match(q):
        return "direct", "纯寒暄", 0.95

    # ④ 极短输入：没有实体、没有问句特征，检索也命中不了什么。
    if len(q) <= direct_max_chars and not _has_fact_signal(q):
        return "direct", f"短文本({len(q)}≤{direct_max_chars})且无事实信号", 0.6

    return None, "", 0.0


def _has_fact_signal(q) -> bool:
    """文本里是否含**具体事实**的痕迹：数字、问号、专有名词倾向。

    这是 L0 的第 ④ 条判据，也是 P2 事后升级通道的**同一个信号**（命中就该去检索）。
    刻意做成一个独立函数：升级判据与入口判据用同一个实现，才不会出现
    "入口判它 simple、升级判它 factual"这种自相矛盾。
    """
    if re.search(r"\d", q):
        return True
    if any(ch in q for ch in "?？"):
        return True
    # 连续 2+ 个汉字里出现明显专名后缀
    return bool(re.search(r"[\u4e00-\u9fff]{2,}(是|有|多少|怎么|为什么|哪里|哪个)", q))


def decide(query, *, prev_plan=None, cache=None, mode="rule+cache",
           fallback=DEFAULT_FALLBACK, direct_max_chars=40, llm_classifier=None):
    """三层路由入口。返回一份 plan（普通 dict）。

    mode:            `rule` / `rule+cache` / `rule+cache+llm`。默认含 cache、**不含 llm**。
    cache:           可选 `DecisionCache` 实例；为 None 时不走缓存。
    llm_classifier:  可选 `callable(query) -> (task, why, confidence)`；仅 `+llm` 时调用。
                     生产里这个函数应该是结构化输出调用，且**必须**有超时——
                     路由自己不能成为延迟的瓶颈。

    返回的 plan 里额外带两个**观测字段**（不进任何闸门）：
    - `why`        人话理由（进日志）
    - `elapsed_ms` 路由自身耗时（它必须远小于它省下的检索开销，否则路由就是负收益）
    """
    t0 = time.perf_counter()
    use_cache = "cache" in mode
    use_llm = "llm" in mode

    task, why, conf = decide_l0(query, prev_plan=prev_plan, direct_max_chars=direct_max_chars)
    source = "rule"

    if task is None and use_cache and cache is not None:
        hit = cache.get(query)
        if hit is not None:
            task, why, conf, source = hit["task"], hit.get("why", "缓存命中"), hit.get("confidence", 1.0), "cache"

    if task is None and use_llm and llm_classifier is not None:
        try:
            task, why, conf = llm_classifier(query)
            source = "llm"
        except Exception as e:  # noqa: BLE001
            # L2 失败**不能**成为失败点：降级到 fallback，而不是把请求打挂。
            task, why, conf, source = None, f"L2 失败：{e}", 0.0, "rule"

    if task is None:
        task, why, conf, source = fallback, "判据未命中，fail-safe 走兜底支路", 0.0, "rule"

    plan = make_plan(task, source=source, confidence=conf, fallback=fallback, why=why)
    plan["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 3)

    # 只缓存 L0/L1 的**确定**结论；L2 的结论本来就不便宜，缓存它反而让"模型判过什么"
    # 变成不可见的隐式状态。失败/兜底的结论**不缓存** —— 那会把一次抖动固化成长尾。
    if use_cache and cache is not None and source in ("rule", "cache") and conf >= 0.5:
        cache.put(query, {"task": task, "why": why, "confidence": conf})
    return plan
