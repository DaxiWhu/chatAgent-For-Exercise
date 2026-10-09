"""`.env.example` 的结构守卫。

为什么值得单独钉
----------------
`.env.example` 是**部署时唯一被照抄的模板**，而它的失效方式**全是静默的**：

  - **同一个键写两处** ⇒ dotenv 是"后者覆盖前者"。真实发生过：`SERVE_API_KEY`
    在「脚本过渡通行证」段与「⚠️ 鉴权（P0）」段各写了一次，而**生效的是旧文案那一处**
    （值是空、说明还停在"留空 = 接口完全裸奔……填了就要求 X-API-Key"）。
    于是照模板部署的人会以为"鉴权就是这一个开关"，完全不知道已经有 JWT 体系。
  - **给废弃项预置了非空值** ⇒ 照抄即开启。`SERVE_API_KEY` 曾经就是
    `change_me_in_prod`，而它恰恰是最不该默认开启的那条路径。

这两条都不会报错、也不会被任何功能测试发现（没有任何代码读 `.env.example`），
所以只能靠"读文件 + 断言结构"来钉。

⚠️ 2026-10-05 起多了一条**反向**的失效方式，`test_deleted_shared_key_is_not_declared_at_all`
钉的就是它：**已删除的配置项却还留在模板里**。模板的作用是"照抄就能跑"，
留一个"配了会让服务拒绝启动"的键，比缺一个键更坏 —— 缺了会报错，多了会静默
把人引向一个不存在的门。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"

# 只统计**没被注释掉**的声明：被注释的行不参与 dotenv 生效，重复是无害的
# （同一个键在不同段落各留一条示例是常见写法，例如 `# VLM_MODEL=...`）。
_DECL = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _declarations() -> list[tuple[int, str, str]]:
    """返回 [(行号, 键, 值)]，只取生效（未注释）的声明。"""
    out: list[tuple[int, str, str]] = []
    for lineno, raw in enumerate(ENV_EXAMPLE.read_text(encoding="utf-8").splitlines(), 1):
        m = _DECL.match(raw.rstrip())
        if m:
            out.append((lineno, m.group(1), m.group(2).strip()))
    return out


def test_env_example_exists():
    """模板必须存在 —— 它是"照什么部署"这个问题的唯一答案。"""
    assert ENV_EXAMPLE.is_file(), f"找不到部署模板：{ENV_EXAMPLE}"


def test_no_key_is_declared_twice():
    """同一个键**最多声明一次**。

    dotenv 取最后一次出现的值，所以"写两处"等于在前一处埋一个静默分叉：
    它看起来还在生效（因为文字还在），实际已经被后面那句覆盖了。
    """
    seen: dict[str, int] = {}
    dupes: list[str] = []
    for lineno, key, _value in _declarations():
        if key in seen:
            dupes.append(f"{key}（第 {seen[key]} 行 与 第 {lineno} 行）")
        else:
            seen[key] = lineno
    assert not dupes, (
        "`.env.example` 里有键被声明了多次：" + "；".join(dupes) +
        "\n后果：dotenv 用最后一条，前面那条的说明全部失效；"
        "两处文案一旦不同步，读的人就会照着过时的那条做。请合并到一处。"
    )


def test_deleted_shared_key_is_not_declared_at_all():
    """`SERVE_API_KEY` 在模板里**不许出现** —— 连空声明都不许。

    语义在 2026-10-05 反转了。此前这条要求"保留一行**空**声明"，因为那个键虽然
    已废弃、却仍有意义（填了打告警，留空是正确配置）。现在代码里已经**没有**
    对应路径，而且启动期守卫把"环境里存在这个键"当作**拒绝启动**的理由
    （留空也算 —— 见 `app/main.py::_assert_no_legacy_auth_knobs`）。

    ⇒ 模板里留一行，等于教人配一个"会让服务起不来"的变量。所以判据从"必须为空"
      改成"必须不存在"；两个方向都点名，是为了防止它被改回成占位值的形态。
    """
    hits = [(ln, v) for ln, k, v in _declarations() if k == "SERVE_API_KEY"]
    assert not hits, (
        f"模板里又声明了 `SERVE_API_KEY`（第 {[ln for ln, _ in hits]} 行，值 {hits[0][1]!r}）。"
        "该键对应的代码路径已整体删除，且环境里存在它（留空也算）会让服务**拒绝启动**"
        "。模板里只应留下说明性注释，不能有可生效的声明。"
    )


def test_deleted_oidc_keys_are_not_declared_at_all():
    """`AUTH_OIDC_` 一族在模板里**不许出现声明** —— 连空声明都不许。

    理由同上面那条（`SERVE_API_KEY`）：代码路径已整体删除，且环境里存在**任何**
    `AUTH_OIDC_` 开头的键（**留空也算**）会让服务**拒绝启动**。模板里留一行，
    等于教人配一个"会让服务起不来"的变量。

    ⚠️ 判据取 `_declarations()`（只统计**未注释**的行），所以注释里提到
    `AUTH_OIDC_JWKS_URI` 这类说明性文本不会被误报 —— 那段"已删除"的说明是**必须留**的。
    """
    hits = [(ln, k, v) for ln, k, v in _declarations() if k.startswith("AUTH_OIDC_")]
    assert not hits, (
        f"模板里又声明了 OIDC 配置项：{[(ln, k) for ln, k, _ in hits]}。"
        "外部身份源（OIDC / JWKS）已整体删除，环境里存在任何 `AUTH_OIDC_` 开头的键"
        "（留空也算）都会让服务**拒绝启动**。模板里只应留下说明性注释。"
    )
