"""补齐的四个确定性工具（时间 / 算术 / 资产回读）的单测。

为什么单独一个文件而不是塞进 ``test_agent_tool_steps.py``
--------------------------------------------------------
那个文件测的是**编排**（步骤清单、重复计数、失败档位），它要用替身模型跑整条链；
这里测的是**纯函数行为**（这个表达式算不算得了、这个路径读不读得到）。
两者的失败方式不同：编排坏了是"清单少一条"，工具坏了是"给模型一句错话"。
混在一起时，一条红的用例要先判断它属于哪一类。

⚠️ 每个"预期失败"的用例都必须用**新的** ``TaskContext``
------------------------------------------------------
工具失败会累进步骤清单，而 ``AGENT_TOOL_FAIL_LIMIT``（默认 3）一到线，回给模型的
文案就被换成"不要再调用它" —— 于是第 3 条以后的断言看到的不再是那条具体错误。
共用一个上下文会让"文案变了"伪装成"这个工具坏了"。
"""

from __future__ import annotations

import asyncio
import json

from agents.tool_context import ToolContext
from agents.usage import Usage

from src.agents.context import TaskContext
from src.agents.tools import (
    calculate,
    get_current_time,
    list_assets,
    read_asset,
    save_text_asset,
)
from tests._agents_testkit import make_ctx


def _invoke(tool, ctx: TaskContext, **kwargs) -> str:
    """按**真实链路**调一次工具：走 ``on_invoke_tool``，参数是 JSON 串。

    直接调底层函数是不行的（``@function_tool`` 之后它已经不是函数了），
    而且只有这条口子会经过参数解析与 ``failure_error_function`` ——
    "错误文案长什么样"正是这些用例要断言的东西。
    """
    payload = json.dumps(kwargs, ensure_ascii=False)
    tool_ctx = ToolContext(
        context=ctx,
        usage=Usage(),
        tool_name=tool.name,
        tool_call_id="c1",
        tool_arguments=payload,
    )
    return asyncio.run(tool.on_invoke_tool(tool_ctx, payload))


# --------------------------------------------------------------------------- #
# 一、get_current_time
# --------------------------------------------------------------------------- #
def test_current_time_carries_date_weekday_and_zone(tmp_path):
    """日期、星期、时区三样都要有。

    只给日期的话，模型算"下周三"仍然要做一次心算（而它算错也没人发现）；
    只给时间戳的话，它得自己换时区 —— 这两件事都不该留给模型。
    """
    text = _invoke(get_current_time, make_ctx(tmp_path))
    assert "Asia/Shanghai" in text
    assert "星期" in text
    # 年份必须是"现在"这个量级，而不是被格式化成别的什么东西
    assert text[:4].isdigit() and 2024 <= int(text[:4]) <= 2100


def test_current_time_honours_the_timezone_argument(tmp_path):
    """时区参数必须真的生效 —— 传了 UTC 还回东八区，等于这个参数不存在。"""
    text = _invoke(get_current_time, make_ctx(tmp_path), timezone="UTC")
    assert "UTC" in text
    assert "Asia/Shanghai" not in text


def test_current_time_rejects_a_human_language_zone(tmp_path):
    """「北京时间」这类说法要拒，并告诉模型该写什么。

    静默回落到默认时区更糟：模型以为自己的时区参数生效了，之后的换算全按错的基准做。
    """
    text = _invoke(get_current_time, make_ctx(tmp_path), timezone="北京时间")
    assert "IANA" in text


# --------------------------------------------------------------------------- #
# 二、calculate
# --------------------------------------------------------------------------- #
def test_calculate_follows_operator_precedence(tmp_path):
    """先乘除后加减 —— 这条看着像废话，但它是"模型心算会错"的那一类。

    提示模型"运算顺序"不是靠嘱咐，而是靠一个**它无法违反**的求值器。
    """
    assert _invoke(calculate, make_ctx(tmp_path), expression="2+3*4") == "2+3*4 = 14"


def test_calculate_avoids_binary_float_noise(tmp_path):
    """``0.1+0.2`` 不能回给模型 ``0.30000000000000004``。

    那个数字本身没错，但它进到正文里就是一段没人能用的垃圾；
    而模型下一个动作很可能是"把它四舍五入再算一遍"。
    """
    assert _invoke(calculate, make_ctx(tmp_path), expression="0.1+0.2").endswith("= 0.3")


def test_calculate_supports_whitelisted_functions(tmp_path):
    assert _invoke(calculate, make_ctx(tmp_path), expression="sqrt(16)+abs(-2)") == (
        "sqrt(16)+abs(-2) = 6"
    )


def test_calculate_refuses_division_by_zero(tmp_path):
    """除零要把话说清楚，而不是给一句 ``ZeroDivisionError``。"""
    text = _invoke(calculate, make_ctx(tmp_path), expression="1/0")
    assert "除数是零" in text


def test_calculate_refuses_code_execution(tmp_path):
    """★ ``eval`` 的替代品必须是**白名单**：模型生成的字符串不能被执行。

    这条用例的值不在"防住 ``__import__``"（那是显然的），而在**将来有人图省事
    改回 ``eval(expression)`` 时立刻红**。合法算式走的是同一段代码，所以
    "改回 eval" 不会让任何别的用例失败。
    """
    text = _invoke(calculate, make_ctx(tmp_path), expression="__import__('os').system('echo hi')")
    assert "不支持这个函数" in text
    assert "__import__" in text


def test_calculate_refuses_object_method_calls(tmp_path):
    """属性调用（``(1).bit_length()``）同样不许 —— 白名单只认裸函数名。"""
    text = _invoke(calculate, make_ctx(tmp_path), expression="(1).bit_length()")
    assert "不支持这个函数" in text


def test_calculate_refuses_a_huge_exponent(tmp_path):
    """指数超过上界要拒 —— 它会把工具线程池的一个线程占死。

    ⚠️ 用例刻意用 ``2**65``（结果 20 位）而不是 ``9**999``（954 位）：
    后者的结果会被**位数闸门**先拦下，于是"指数上界"这条判据有没有都不影响结论 ——
    一条永远为绿的用例证明不了任何事。实战里真正危险的是嵌套幂 ``9**9**9``：
    内层算出四亿，外层拿它当指数，求值会直接吃满内存。

    线程池是共享的（见 ``toolpool``），一个表达式就能让其它工具调用排队到超时。
    """
    text = _invoke(calculate, make_ctx(tmp_path), expression="2**65")
    assert "超过上界" in text


def test_calculate_refuses_an_empty_expression(tmp_path):
    """空表达式必须当场拒，而不是回一句 ``= 0`` 之类的东西。"""
    text = _invoke(calculate, make_ctx(tmp_path), expression="   ")
    assert "空的" in text


def test_calculate_rejects_arguments_it_does_not_know(tmp_path):
    """★ 多给的参数必须**当场**被拒。

    官方解析参数用的是 ``params_pydantic_model(**json_data)``，那个模型没有
    ``extra="forbid"`` ⇒ 多给的字段会被静默丢弃。于是模型以为"我加了精度限制"，
    实际那一刻条件已经没了，而它拿到的结果看起来完全正常。这一层是唯一能看见
    原始参数的地方（``tools._extra_args``）。
    """
    text = _invoke(calculate, make_ctx(tmp_path), expression="1+1", precision=2)
    assert "不认识这些参数" in text
    assert "precision" in text


# --------------------------------------------------------------------------- #
# 三、list_assets / read_asset
# --------------------------------------------------------------------------- #
def test_list_assets_says_there_is_nothing_yet(tmp_path):
    """空目录要给一句**明确**的话，而不是空串。

    空串会被模型当成"工具没返回"，于是它继续猜文件名。
    """
    assert "还没有落盘任何文件" in _invoke(list_assets, make_ctx(tmp_path))


def test_list_assets_shows_name_size_and_time(tmp_path):
    ctx = make_ctx(tmp_path)
    _invoke(save_text_asset, ctx, name="周报", content="本周进展：3 项。")
    text = _invoke(list_assets, ctx)
    assert "周报.txt" in text
    assert "字节" in text


def test_saved_asset_can_be_read_back(tmp_path):
    """★ 写入与读取必须成对 —— 这是本次补工具的核心动机。

    ``save_text_asset`` 一直是单向的：写得出去、读不回来。于是多轮对话里
    "把上一步的稿子改一下"只能靠模型记住全文，而历史一旦被 ``history_budget``
    裁过，它就真的没有了。
    """
    ctx = make_ctx(tmp_path)
    _invoke(save_text_asset, ctx, name="草稿", content="第一版：开场白。")
    assert _invoke(read_asset, ctx, name="草稿.txt") == "第一版：开场白。"


def test_read_asset_lists_what_is_available_when_the_name_is_wrong(tmp_path):
    """文件名猜错时要给出可选项 —— 否则模型会一直换着名字猜。"""
    ctx = make_ctx(tmp_path)
    _invoke(save_text_asset, ctx, name="真实文件", content="x")
    text = _invoke(read_asset, ctx, name="不存在.txt")
    assert "没有这个文件" in text
    assert "真实文件.txt" in text


def test_read_asset_refuses_to_escape_the_asset_dir(tmp_path):
    """★★ 路径穿越必须被拒，且**内容一个字都不能返回**。

    只断言"返回里有拒绝字样"是不够的：实现先读文件再报错，也会拿到这句话。
    所以这里在外层放一个真实存在的文件，并断言它的内容没有出现在返回里。
    """
    outside = tmp_path / "secret.txt"
    outside.write_text("这是目录外面的内容", encoding="utf-8")
    ctx = make_ctx(tmp_path)

    for name in ("../secret.txt", "..\\secret.txt"):
        text = _invoke(read_asset, ctx, name=name)
        assert "目录外面" in text, (name, text)
        assert "这是目录外面的内容" not in text, name


def test_read_asset_refuses_a_binary_file_without_crashing(tmp_path):
    """非 UTF-8 文件要降级成一句话，不能把 ``UnicodeDecodeError`` 抛给模型。"""
    ctx = make_ctx(tmp_path)
    base = ctx.asset_dir
    base.mkdir(parents=True, exist_ok=True)
    (base / "figure.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    text = _invoke(read_asset, ctx, name="figure.png")
    assert "不是 UTF-8" in text


def test_read_asset_refuses_an_empty_name(tmp_path):
    text = _invoke(read_asset, make_ctx(tmp_path), name="  ")
    assert "空的" in text


# --------------------------------------------------------------------------- #
# 四、接线
# --------------------------------------------------------------------------- #
def test_new_tools_are_exposed_to_the_model():
    """四个工具都必须挂在前台上。

    漏挂的症状极隐蔽：工具写好了、单测全绿，但模型永远看不到它 ——
    表现是"模型就是不用这个工具"，而看板上的工具曝光里根本没有这个名字。
    """
    from src.agents.frontline import frontline_agent

    names = {t.name for t in frontline_agent.tools}
    assert {"get_current_time", "calculate", "list_assets", "read_asset"} <= names


def test_new_tools_are_read_only_so_they_need_no_approval(tmp_path):
    """★ 只读工具**不挂审批**，而且行为上确实不落任何东西。

    审批的门槛必须留给"撤不回来"的动作（落盘 / 生成）。给只读工具也加一道确认，
    代价不是多一次点击，而是训练用户**盲目点同意** —— 等真正的危险动作来时，
    那道确认已经不值钱了。

    这里同时用**行为**断言只读性（跑完目录里不多出文件），而不是只读一遍代码：
    "这个函数没写盘"这种判断，读代码时最容易看漏一个 ``mkdir``。
    """
    ctx = make_ctx(tmp_path)
    before = set(tmp_path.iterdir())
    _invoke(list_assets, ctx)
    _invoke(get_current_time, ctx)
    _invoke(calculate, ctx, expression="1+2")
    assert set(tmp_path.iterdir()) == before
