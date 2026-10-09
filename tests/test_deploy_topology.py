"""部署拓扑的分层约束（staging overlay + 灰度文档）。

为什么需要这个测试
------------------
`docker-compose.staging.yml` 里的三个错误**全都不会报错**，只会在最坏的时刻
以最坏的方式体现出来：

  1. **忘了改 `name:`** ⇒ 与生产**共用同一套命名卷**（compose 的项目名默认取自
     目录名，两个文件在同一目录就是同一个项目）。表现是"staging 起来了、一切正常"，
     而你在 staging 里跑的灌库/清洗**作用在生产数据上**。
  2. **`APP_ENV` 没显式写（靠插值）或写成开发口径的值** ⇒ 会得到非生产语义
     （`REQUIRE_AUTH` 默认为假、`ENABLE_DOCS` 默认为真）。
     ⚠️ 判据 2026-10-05 已从"白名单认生产"改为"**黑名单认开发**"（只有
     `development`/`dev`/`local`/`test`/`testing` 算非生产），所以 `staging`/`prd`
     这类写法现在**会落到生产、不再静默降级**；但**插值**仍会被部署机上残留的
     `APP_ENV` 带偏，所以这个文件继续显式写死 `production`。
     也就是说：staging 唯一与生产不同的地方，恰好是最该被验证的那一层。
  3. **`ports:` 没加 `!override`** ⇒ compose 对 `ports`/`expose`/`dns`/`tmpfs`
     的叠加规则是**拼接**而不是替换。表现是 staging 仍然去抢生产的端口，
     报 `address already in use` —— 看起来像"服务起不来"，很难想到是端口。

`deploy/CANARY.md` 同样要守两条：它引用的脚本必须真实存在，
且必须写明**三种退出码分别该做什么**（因为 `1` 与 `2` 的处置完全相反：
前者立刻回滚，后者坚决不要动流量）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy" / "app"
BASE = DEPLOY / "docker-compose.yml"
STAGING = DEPLOY / "docker-compose.staging.yml"
CANARY_DOC = ROOT / "deploy" / "CANARY.md"
CANARY_SCRIPT = ROOT / "scripts" / "canary_check.py"


def _load_with_compose_tags(path: Path) -> dict:
    """解析 compose 文件。

    compose 支持 `!override` / `!reset` 这两个自定义标签（用于覆盖那些
    "叠加时会被拼接"的列表字段）。它们不是标准 YAML，所以要先注册构造器，
    否则 `safe_load` 会直接抛错 —— 而"测试读不了这个文件"本身也是一种失效模式。
    """
    class _Loader(yaml.SafeLoader):
        pass

    for tag in ("!override", "!reset"):
        _Loader.add_constructor(
            tag,
            lambda loader, node, _t=tag: (
                None if _t == "!reset" else loader.construct_sequence(node, deep=True)
            ),
        )
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)


@pytest.fixture(scope="module")
def base_cfg():
    assert BASE.exists(), f"缺少主编排文件：{BASE}"
    return _load_with_compose_tags(BASE)


@pytest.fixture(scope="module")
def staging_cfg():
    assert STAGING.exists(), (
        f"缺少 staging overlay：{STAGING}\n"
        "没有它就没有环境分层，验证只能在生产上做"
    )
    return _load_with_compose_tags(STAGING)


# ---------------------------------------------------------------------------
# ① 项目名：决定卷与网络的隔离
# ---------------------------------------------------------------------------
def test_staging_uses_a_different_project_name(base_cfg, staging_cfg):
    """staging 必须有自己的项目名。

    compose 的项目名决定命名卷与网络的前缀。同名 ⇒ 共用同一套卷 ⇒
    staging 直连**生产的数据库与向量库**。这不是"环境串扰"，是数据事故：
    staging 里的迁移/灌库/清理会真实地作用在生产数据上，而且没有任何报错。
    """
    base_name = base_cfg.get("name")
    staging_name = staging_cfg.get("name")
    assert staging_name, (
        "staging overlay 没有设置 name —— compose 会沿用同一个项目名，"
        "两套环境共用命名卷（staging 会写到生产数据上）"
    )
    assert staging_name != base_name, (
        f"staging 与主编排的项目名相同（{staging_name!r}）——共用命名卷与网络"
    )


def test_staging_data_services_exist_in_base(staging_cfg, base_cfg):
    """overlay 里覆盖的服务必须在主文件里存在。

    打错服务名时 compose 会**新建**一个服务，而不是报错 —— 于是你会得到一个
    "配置看着没问题、但多了一个空服务、真正想改的那个没改"的编排。
    """
    base_services = set(base_cfg.get("services") or {})
    for name in staging_cfg.get("services") or {}:
        assert name in base_services, (
            f"staging overlay 覆盖了不存在的服务 {name!r}；"
            f"主文件里只有 {sorted(base_services)}"
        )


# ---------------------------------------------------------------------------
# ② APP_ENV：决定鉴权与 /docs
# ---------------------------------------------------------------------------
def test_staging_forces_production_semantics(staging_cfg):
    """staging 的 `APP_ENV` 必须是字面量 `production`。

    两点都要：**值必须是 production**，且**不能是 `${APP_ENV:-...}` 这种插值**
    —— 插值意味着"部署机上 export 过 APP_ENV 就会被带偏"，那时你会得到一个
    鉴权被关掉的 staging，而它看起来和生产一模一样。

    ⚠️ 2026-10-05 起保留这条守卫的**理由变了**（判据已改，见下）：
      · 原判据是白名单认生产（`APP_ENV in ("production","prod")`），写 `staging`
        会被当成开发环境、**静默**关掉鉴权并打开 /docs —— 那时这条守卫是唯一的拦截点。
      · 现判据是黑名单认开发（只有 development/dev/local/test/testing 算非生产），
        所以写 `staging` **也会**落到生产语义，不再静默降级。
      · 仍然保留的理由：**显式声明优于依赖判据的宽容度**。staging 能不能安全运行，
        不该取决于"判据在别的取值上恰好偏保守"。所以这里继续要求写死 `production`，
        且不许用插值（插值会被部署机上的环境变量带偏成开发口径）。

    环境标识请用 `APP_RELEASE`：它只影响日志与指标标签，不改变任何开关。
    """
    env = (staging_cfg["services"]["app-stable"].get("environment") or {})
    assert "APP_ENV" in env, (
        "staging 没有显式设置 APP_ENV —— 会沿用主文件的 ${APP_ENV:-production}，"
        "该插值在部署机 export 过 APP_ENV 时会被带偏"
    )
    value = str(env["APP_ENV"])
    assert value == "production", (
        f"staging 的 APP_ENV={value!r}。判据已改为「认不出来的取值一律当生产」，"
        "所以写 staging 不会再静默降级 —— 但 staging 的安全性不该依赖判据的宽容度，"
        "请显式写 production；环境标识请用 APP_RELEASE"
    )
    assert "APP_RELEASE" in env, (
        "staging 应该用 APP_RELEASE 标识环境（它不影响任何开关，只影响回显与指标标签）"
    )


# ---------------------------------------------------------------------------
# ③ ports：叠加时是"拼接"，必须显式 override
# ---------------------------------------------------------------------------
def test_staging_port_bindings_replace_instead_of_appending():
    """端口字段必须用 `!override` 顶掉，不能只写一个新列表。

    compose 的叠加规则里 `ports` / `expose` / `dns` / `tmpfs` 是**拼接**：
    只写 `ports: ["127.0.0.1:5434:5432"]` 的结果是同时绑 5433 与 5434 ——
    staging 依然会去抢生产的端口，报 `address already in use`。
    这个报错看起来像"服务起不来"，排查方向很容易错。

    这里用文本匹配而不是解析后的结构：`!override` 这个标签本身就是要断言的东西，
    解析完就看不到了。
    """
    text = STAGING.read_text(encoding="utf-8")
    assert "ports: !override" in text, (
        "staging 里没有使用 `ports: !override` —— compose 会**拼接**端口，"
        "staging 仍会抢生产的端口"
    )

    # 反向检查：**任何** `ports:` 行都必须带 `!override`。
    # 用负向断言而不是"匹配裸列表 + 换行"，因为后者会被一个行尾注释绕过
    # （`ports:  # 说明` 后面跟列表），而那种写法照样是拼接语义。
    bare = [m.group(0) for m in
            re.finditer(r"^[ \t]+ports:(?!\s*!override).*$", text, flags=re.M)]
    assert not bare, (
        f"staging 里有 {len(bare)} 处 `ports:` 没带 `!override`：{bare}\n"
        "compose 对 ports 的叠加是**拼接**而不是替换 ⇒ 会同时绑生产与 staging 的端口"
    )


# ---------------------------------------------------------------------------
# ④ 告警只能来自生产
# ---------------------------------------------------------------------------
def test_staging_disables_monitoring_and_backups(staging_cfg):
    """staging 必须关掉监控与备份。

    两件事都会出问题：
      - **告警**：staging 的错误会半夜叫醒值班的人，而它本就该允许出错。
        结果是真告警被"这条是 staging 的"习惯性忽略。
      - **备份**：staging 用临时数据，备份只是白占磁盘并让"备份有没有在今天跑过"
        这个判断（`ls -lt backups/`）变得含糊 —— 你不知道那个文件是生产还是 staging 的。

    用 `replicas: 0` 而不是删掉服务：配置保留，需要时改回 1 即可。
    """
    services = staging_cfg.get("services") or {}
    for name in ("alertmanager", "prometheus", "backup-pg", "backup-qdrant"):
        assert name in services, (
            f"staging 没有关掉 {name}；告警/备份必须只属于生产环境"
        )
        replicas = ((services[name].get("deploy") or {}).get("replicas"))
        assert replicas == 0, (
            f"staging 的 {name} 仍然会启动（deploy.replicas={replicas!r}）"
        )


def test_staging_keeps_the_app_running(staging_cfg):
    """反向检查：staging 真的保留了应用本体。

    "关掉一切" 也能让上面那条测试通过，所以必须有一条断言防止把 staging
    改成一个什么都不跑的空编排。
    """
    services = staging_cfg.get("services") or {}
    assert "app-stable" in services
    replicas = ((services["app-stable"].get("deploy") or {}).get("replicas"))
    assert replicas != 0, "staging 把自己的应用也关掉了"


# ---------------------------------------------------------------------------
# 灰度文档：引用的东西必须存在，退出码的处置必须写明
# ---------------------------------------------------------------------------
# 口令类变量不许有"默认值兜底"
# ---------------------------------------------------------------------------
# 口径：口令的默认值等于**把口令公开写在仓库里**；`${VAR:?…}` 才是"没设就报错"。
# 两种写法展开后是同一个值，所以这条约束**只存在于文本里** —— 没有任何运行时
# 差异能反映它，只能断言原文。
#
# ⚠️ 这张表是"登记制 + 一致性检查"，不是"想起来了就加一条"（2026-10-06 修）。
# 原来的实现只登记了 `deploy/app/` 下那两份文件，于是
# `deploy/pgvector/docker-compose.yml` 一直用着 `${POSTGRES_PASSWORD:-123456}`
# 而守卫全绿 —— 它是个**独立可执行**的 PG 编排，且建出来的 `langgraph_app` 是
# 超级用户，所以"仅供本机开发"的默认值等于"一个随时会被当成生产库起来的弱口令库"。
# 名单式判据的通病就在这里：漏登记没有任何症状。下面那条
# `test_every_compose_file_is_registered` 把它变成一个会红的错误。
_REQUIRED_PASSWORDS = {
    "deploy/app/docker-compose.yml": ("POSTGRES_PASSWORD", "REDIS_PASSWORD", "GRAFANA_PASSWORD"),
    "deploy/app/docker-compose.staging.yml": ("STAGING_REDIS_PASSWORD",),
    "deploy/pgvector/docker-compose.yml": ("POSTGRES_PASSWORD",),
    # TLS overlay 只加端口与证书挂载，不碰口令 —— 显式登记"零个"，
    # 这样它将来若引入口令变量，一致性检查仍会提醒回来改这里。
    "deploy/app/docker-compose.tls.yml": (),
}


def _compose_files() -> list[Path]:
    """仓库里所有编排文件（含 override）。"""
    return sorted(ROOT.glob("deploy/**/docker-compose*.yml"))


def test_every_compose_file_is_registered():
    """发现到的编排文件与登记表必须**完全一致**。

    两个方向都有意义：
      · 发现多出一份 ⇒ 新编排文件没登记，它的口令口径没人管（就是这次的问题）；
      · 登记多出一项 ⇒ 文件被删/改名了，表里那一项是死的（守卫看起来还在，其实没在查）。
    """
    found = {p.relative_to(ROOT).as_posix() for p in _compose_files()}
    registered = set(_REQUIRED_PASSWORDS)
    assert found == registered, (
        f"编排文件与登记表不一致。\n"
        f"  只在磁盘上、没登记：{sorted(found - registered)}\n"
        f"  只在表里、不在磁盘：{sorted(registered - found)}\n"
        "新增编排文件时请把它登记进 _REQUIRED_PASSWORDS 并写明该文件里哪些口令必须必填；"
        "若该文件不含口令变量，登记成空元组。"
    )


@pytest.mark.parametrize("path", _compose_files(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_password_vars_are_required_not_defaulted(path):
    """口令变量必须写成 `${VAR:?…}`（必填），不许有 `:-某默认值`。

    为什么值得一条守卫：漏写 `--env-file .env` 是**静默**失败（见编排文件头部
    那段说明），叠上"口令有默认值"就从"配置没生效"升级成"**上线了，而且口令是
    公开在仓库里的**"。更糟的是数据卷初始化后口令**固化** —— 你以为改环境变量
    就换了，实际换不掉，只能删卷重来。

    两个方向都要断言：
      · 不许有 `:-` —— 默认值兜底本身就是那个坑；
      · 必须有 `:?` —— 光删掉默认值是不够的。`${VAR}` 这种写法同样没有默认值，
        但它会安静地展开成**空串**，把"口令为空"当成配置成功。
    """
    text = path.read_text(encoding="utf-8")
    for var in _REQUIRED_PASSWORDS[path.relative_to(ROOT).as_posix()]:
        assert not re.search(r"\$\{" + var + r":-", text), (
            f"{var} 又有了默认值兜底（`:-`）：漏写 --env-file 时会静默用弱口令起库"
        )
        assert re.search(r"\$\{" + var + r":\?", text), (
            f"{var} 没有写成 ${{{var}:?…}} —— 少了默认值不等于必填，"
            "`${VAR}` 会展开成空串并把「口令为空」当成配置成功"
        )


def test_env_example_does_not_ship_weak_passwords():
    """模板文件里也不许留弱口令 —— 它是被复制出去的，等于弱口令的传播源。

    那三行必须是**空**的：留空 ⇒ compose 因 `${VAR:?}` 报错退出（响亮、立刻可修），
    留 `123456` ⇒ 一路装完、没有任何提示（安静、且数据卷一旦初始化就改不掉）。
    """
    path = ROOT / ".env.example"
    text = path.read_text(encoding="utf-8")
    offenders = []
    for var in ("POSTGRES_PASSWORD", "REDIS_PASSWORD", "GRAFANA_PASSWORD"):
        m = re.search(r"^" + var + r"=(.*)$", text, re.M)
        assert m, f".env.example 里没有声明 {var}（模板与编排文件必须同步）"
        if m.group(1).strip():
            offenders.append(f"{var}={m.group(1).strip()}")
    assert not offenders, (
        "模板里又出现了弱口令 —— 复制它的人会直接带着这个口令上线：\n  "
        + "\n  ".join(offenders)
    )


def test_canary_doc_references_a_real_script():
    """文档里提到的判据脚本必须真实存在。

    否则文档会指向一个不存在的命令，而这只有在你真正要回滚时才会发现。
    """
    assert CANARY_DOC.exists(), f"缺少灰度流程文档：{CANARY_DOC}"
    text = CANARY_DOC.read_text(encoding="utf-8")
    assert "scripts/canary_check.py" in text, "文档没有引用判据脚本"
    assert CANARY_SCRIPT.exists(), (
        f"文档引用的 {CANARY_SCRIPT} 不存在"
    )


def test_canary_doc_documents_all_three_exit_codes():
    """文档必须写明三种退出码分别该做什么。

    只写"退出码非 0 就回滚"是本项目里最典型的错误做法：
    退出码 2 表示"看不到数据"，此时回滚是错的（回滚一个其实没问题的版本），
    而放量更错。这两种处置必须被明确区分，否则值班的人只能猜。
    """
    text = CANARY_DOC.read_text(encoding="utf-8")
    for code in ("0", "1", "2"):
        assert f"`{code}`" in text, f"文档没有说明退出码 {code} 的含义与处置"
    assert "不要当成通过" in text or "不要当成通过，" in text, (
        "文档没有强调「无法判定」不等于通过 —— 这是灰度流程里最贵的假绿"
    )


def test_canary_doc_has_an_executable_rollback_procedure():
    """回滚必须是可执行的命令，而不是"把流量切回去"这种描述。

    出问题时人处在压力下，任何需要现场推导的步骤都会被跳过或做错。
    2026-09-30 起回滚被**脚本化**（`deploy/rollback.sh`），所以这里同时接受
    两种写法：既可以是手工命令（`weight=0`），也可以是脚本化的
    （`nginx_weight.py set --canary 0`）—— 但"零权重 + reload"这两件
    缺一不可的事必须都能在文档里看到。
    """
    text = CANARY_DOC.read_text(encoding="utf-8")
    assert "nginx -s reload" in text, "回滚步骤里没有 reload 命令，权重改了不会生效"
    assert ("weight=0" in text or "set --canary 0" in text), (
        "回滚步骤没有说明如何把流量收回稳定版（零权重）"
    )


def test_canary_doc_warns_about_app_env_staging():
    """文档必须写明 `APP_ENV=staging` 这个坑。

    这是本项目里"看起来对但其实关掉了鉴权"的典型：写 `staging` 是最自然的
    命名选择，而它恰好触发非生产语义。文档不写，几乎一定会有人这么写。
    """
    text = CANARY_DOC.read_text(encoding="utf-8")
    assert "APP_ENV" in text and "staging" in text
    assert "production" in text
    assert "鉴权" in text or "REQUIRE_AUTH" in text, (
        "文档没有说明写 APP_ENV=staging 的后果（静默关掉鉴权 / 打开 /docs）"
    )


# ---------------------------------------------------------------------------
# 治理文档：值班手册必须覆盖每一条告警
# ---------------------------------------------------------------------------
ALERTS = ROOT / "deploy" / "monitoring" / "alerts.yml"
RUNBOOK = ROOT / "deploy" / "RUNBOOK.md"
BUDGET_POLICY = ROOT / "deploy" / "monitoring" / "ERROR_BUDGET_POLICY.md"
SECRETS_DOC = ROOT / "deploy" / "SECRETS.md"


def _alert_names() -> list[str]:
    cfg = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
    names = []
    for group in cfg.get("groups") or []:
        for rule in group.get("rules") or []:
            if rule.get("alert"):
                names.append(rule["alert"])
    return names


def test_runbook_covers_every_alert():
    """每一条告警都必须在值班手册里有处置条目。

    这条防的是最典型的运维退化：**加了新告警，但没人知道响了该做什么**。
    后果不是"少一条文档"，而是那条告警在两次"响了没人动"之后被静默删掉 ——
    等于白写。

    这里只断言"名字出现了"，因为处置内容的对错无法自动判定；
    但"名字没出现"是能自动判定的，而那正好对应"完全没写"。
    """
    assert ALERTS.exists(), f"缺少告警规则文件：{ALERTS}"
    assert RUNBOOK.exists(), f"缺少值班手册：{RUNBOOK}"

    names = _alert_names()
    assert len(names) >= 10, f"告警规则数量异常（{len(names)} 条），本测试需同步"

    text = RUNBOOK.read_text(encoding="utf-8")
    missing = [n for n in names if n not in text]
    assert not missing, (
        f"值班手册缺少这些告警的处置条目：{missing}\n"
        "新增告警时请同步在 deploy/RUNBOOK.md 里写「谁会被告警、第一句话做什么」"
    )


def test_runbook_links_the_policy_documents():
    """值班手册要能一步点到另两份治理文档。

    出问题时人处在压力下，需要的是"点一下就到"，而不是"翻目录去找"。
    """
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "ERROR_BUDGET_POLICY.md" in text, "手册没有链到错误预算政策"
    assert "SECRETS.md" in text, "手册没有链到密钥管理文档（怀疑泄漏时要用）"
    assert "CANARY.md" in text, "手册没有链到灰度/回滚文档（止血时要用）"


def test_error_budget_policy_defines_actions_not_just_goals():
    """错误预算政策必须给出**动作**，而不只是重复 SLO 目标。

    没有动作的错误预算就只是仪表盘上的一个数字 —— 这正是 SLO.md 里
    "那才是错误预算存在的意义"所指的那件事。
    """
    assert BUDGET_POLICY.exists(), f"缺少错误预算政策：{BUDGET_POLICY}"
    text = BUDGET_POLICY.read_text(encoding="utf-8")

    assert "冻结" in text, "政策里没有说明预算烧掉后对发布的影响（冻结）"
    assert "回滚" in text, "政策里没有提到回滚"
    assert "复盘" in text, "政策里没有要求复盘 —— 没有复盘的冻结只是拖延"


def test_error_budget_policy_only_cites_real_alerts():
    """政策引用的告警名必须真实存在。

    引错名字的后果是：告警响的是 A，政策写的是 B，值班的人按 B 的说明去操作 ——
    而 B 根本不存在。这类错误只有真的出事时才会发现。
    """
    text = BUDGET_POLICY.read_text(encoding="utf-8")
    cited = re.findall(r"`(SLO[A-Za-z]+)`", text)
    assert cited, "政策里没有引用任何 SLO 告警名，本测试需同步"

    names = set(_alert_names())
    unknown = [c for c in cited if c not in names]
    assert not unknown, (
        f"错误预算政策引用了不存在的告警名：{unknown}\n"
        f"alerts.yml 里实际有：{sorted(n for n in names if n.startswith('SLO'))}"
    )


def test_secrets_doc_states_the_leak_response_order():
    """密钥泄漏的处置必须写明**顺序**：先撤销，再排查。

    顺序反了（先花时间确认"是不是真的泄漏"）会让暴露窗口无限延长 ——
    排查要几个小时，而泄漏在持续。文档必须把人推向"先撤"。
    """
    assert SECRETS_DOC.exists(), f"缺少密钥管理文档：{SECRETS_DOC}"
    text = SECRETS_DOC.read_text(encoding="utf-8")

    assert "撤销" in text, "泄漏处置里没有『先撤销』这一步"
    assert "轮换" in text, "文档没有讲轮换流程"
    # "清历史不能替代轮换" 是这类事故里最贵的误解
    assert "不能替代轮换" in text, (
        "文档没有强调『清理 git 历史不能替代轮换密钥』——"
        "密钥一旦离开控制范围就必须假定已泄露"
    )


def test_secrets_doc_flags_the_single_value_key_rotation_gap():
    """必须点明 `SERVE_API_KEY` 是单值、**做不到**零停机轮换，且旧方案已作废。

    把它写成"按流程轮换即可"会让运维在生产上硬切，然后所有调用方同时 401。

    这条断言改过两次，值得记下来，因为它演示了"断言与它声称要守的东西脱钩"是怎么发生的：
      - 最初断言的是"文档要提醒给 `SERVE_API_KEY` 加多值"（当时的修法建议）；
      - 2026-10-04 第一次订正：那个修法多余（`TENANT_KEYS` 本就是 `{密钥: 租户}` 的 map），
        于是改成钉"单值 / `TENANT_KEYS` / 退役"三处；
      - 同一天稍后：**`TENANT_KEYS` 被整体删除**，那条重叠轮换的路一起消失。此时
        "文档里出现 `TENANT_KEYS`"仍然能被满足 —— 因为删除后的订正句里就写着这个词。
        断言再次与意图脱钩。所以现在钉的是**订正本身**：文档必须说明那条路已作废。
    """
    text = SECRETS_DOC.read_text(encoding="utf-8")
    assert "SERVE_API_KEY" in text
    assert "单值" in text, "文档没有指出 SERVE_API_KEY 是单值 —— 这是它换不动的原因"
    assert "TENANT_KEYS" in text and "删除" in text, (
        "文档没有写明「同一租户登记两把钥匙」那条重叠轮换的路**已随 TENANT_KEYS 删除而作废**。"
        "不提这件事的话，下一个人会照着那份照做清单去切换，在切换那一刻把全体调用方打成 401"
    )
    assert "AUTH_SESSION_SECRET" in text, (
        "会话签名密钥同样是单值：换它等于让全体用户重新登录，这属于产品决策，文档得写出来"
    )


# ---------------------------------------------------------------------------
# 日志轮转 / 网络分段 / 备份窗口 / 保留清理 / 优雅关闭（2026-09-30 新增）
# ---------------------------------------------------------------------------
# 这一节守的都是**不会报错、只会在最坏时刻暴露**的配置：
#   - 日志无上限 ⇒ 几周后撑满盘 ⇒ PG 随机 500（而不是"磁盘满"）；
#   - 数据服务与面板同网段 ⇒ 一个容器被拿下 = 横向直达数据库；
#   - 备份"间隔 × 份数"被无意改小 ⇒ 需要"回到上周"时发现只留有 3 天；
#   - 保留清理只活在文档里 ⇒ 三张只增不减的表无限膨胀，且没有任何告警指向它；
#   - stop_grace_period 小于排空上界 ⇒ 发布时在途 SSE 被 SIGKILL（且不留日志）。

#: 这几个服务是"数据面"，只允许接 data 网络（见 compose 文件末尾的分段说明）。
DATA_ONLY_SERVICES = {
    "pgvector", "qdrant", "redis", "backup-pg", "backup-qdrant", "retention-sweep",
}


def _compose_env_default(text: str, key: str) -> str | None:
    """取 `${KEY:-默认值}` 里的默认值。

    优先"锚定到 `KEY:` 行首"那种写法 —— 它会**跳过** shell 脚本里的
    `$${KEY:-默认值}` 兜底（那是另一处默认值，不是 compose 的插值默认值），
    也不会误伤别的 `${KEY:-...}` 出现位置。只在找不到锚定形式时才退回全文第一次出现，
    因为像 `QDRANT_BACKUP_KEEP` 这种"变量名与 compose 键名不同"的情况
    （左值是 `BACKUP_KEEP:`）锚定不到。
    """
    m = re.search(rf"^\s*{re.escape(key)}:\s*\$\{{{re.escape(key)}:-([^}}]+)\}}", text, re.M)
    if m:
        return m.group(1)
    m = re.search(rf"\$\{{{re.escape(key)}:-([^}}]+)\}}", text)
    return m.group(1) if m else None


def test_every_service_limits_its_container_logs(base_cfg):
    """每个服务都必须挂日志轮转上限。

    docker 的 json-file 驱动**默认没有任何上限**：容器往 stdout 写多少就落多少。
    本栈里几个组件天生话多（PG 的 WAL/checkpoint 提示、Prometheus 的规则加载、
    nginx 的每个请求……），跑几个月把 `/var/lib/docker` 吃掉几十 GB 是常态。

    它和"没有磁盘告警"合起来是一条完整的宕机路径：
    日志撑满盘 → PG 写不进去 → 表现成**随机 500**（而不是"磁盘满"）→ 你甚至不知道去 `df -h`。
    """
    bad = []
    for name, svc in (base_cfg.get("services") or {}).items():
        opts = ((svc.get("logging") or {}).get("options")) or {}
        if not opts.get("max-size") or not opts.get("max-file"):
            bad.append(name)
    assert not bad, (
        f"以下服务没有日志轮转上限：{bad}\n"
        "docker 默认不限制容器日志大小；请在服务上挂 `logging: *default-logging`"
    )


def test_data_services_are_on_the_data_network_only(base_cfg):
    """数据库 / 向量库 / Redis 只能接 data 网络。

    全部丢在一个默认桥里时，任何一个容器被拿下 = 横向可达全部 ——
    尤其 Promtail（挂着 docker.sock，等价 root）能直连 PG / Qdrant / Redis。
    data 段存在的**全部意义**就是挡住这一类，所以数据服务不得出现在别的网络上。
    """
    services = base_cfg.get("services") or {}
    bad = {}
    for name in DATA_ONLY_SERVICES:
        svc = services.get(name)
        assert svc is not None, f"{name} 服务不见了（本测试的清单需要同步）"
        extra = set(svc.get("networks") or []) - {"data"}
        if extra:
            bad[name] = sorted(extra)
    assert not bad, (
        f"以下数据服务出现在了非 data 网络上：{bad}\n"
        "PG / Qdrant / Redis 不该与面板、exporter 同段 —— 那是横向移动的通道"
    )


def test_networks_are_declared_and_referenced_consistently(base_cfg):
    """服务引用的网络必须都在顶层 `networks:` 里声明过。

    引用了未声明的网络时 compose 会**自动创建**它（而不是报错），
    于是你以为自己在分段，实际上多了一个匿名桥 —— 分段被静默架空。
    """
    declared = set(base_cfg.get("networks") or {})
    assert {"edge", "data", "monitor"} <= declared, (
        f"顶层 networks 缺少分段定义（现有：{sorted(declared)}）"
    )
    for name, svc in (base_cfg.get("services") or {}).items():
        for net in svc.get("networks") or []:
            assert net in declared, f"{name} 引用了未声明的网络 {net!r}"


def test_app_services_stop_grace_period_exceeds_the_drain_timeout(base_cfg, monkeypatch):
    """容器 `stop_grace_period` 必须**大于** app 侧的排空上界。

    本服务主接口是 SSE，一次回答可能持续几十秒。uvicorn 对"响应未结束"的连接
    不会掐断，但容器有自己的宽限期 —— `docker stop` 默认只给 **10s** 然后 SIGKILL。
    **被 SIGKILL 才是真正掐断在途流的原因**（不是应用代码），而且它不给进程任何
    记账/清理的机会，日志里查不到。两个值写在两个文件里，改一个不改另一个就是事故。
    """
    monkeypatch.delenv("GRACEFUL_SHUTDOWN_TIMEOUT", raising=False)
    from app import serve

    drain = serve.resolve_graceful_timeout()
    assert drain > 0

    for svc_name in ("app-stable", "app-canary"):
        period = str((base_cfg["services"][svc_name]).get("stop_grace_period") or "")
        assert period, (
            f"{svc_name} 没有设置 stop_grace_period —— docker 默认 10s，"
            "滚动更新时会 SIGKILL 掉在途 SSE"
        )
        m = re.fullmatch(r"(\d+)([sm])", period)
        assert m, f"{svc_name} 的 stop_grace_period={period!r} 无法解析（期望 45s 这种形式）"
        secs = int(m.group(1)) * (60 if m.group(2) == "m" else 1)
        assert secs > drain, (
            f"{svc_name} 的 stop_grace_period={secs}s 不大于 serve.py 的排空上界 {drain}s —— "
            "容器会在排空完成前被 SIGKILL，那才是真正掐断在途流的原因（且不留日志）"
        )


def test_backup_retention_window_is_at_least_a_week():
    """备份的"间隔 × 保留份数"必须 >= 7 天。

    这个乘积才是**真正可回溯的时间窗**（RPO 由间隔决定，可回溯长度由它决定）。
    只调间隔不调份数时窗口会静默变化：把 24h 压到 6h 而份数不动 ⇒ 窗口从
    14 天缩到 **3.5 天** —— 而那正是最需要"回到上周"的时候。所以两个值必须联动。
    """
    text = BASE.read_text(encoding="utf-8")
    interval = _compose_env_default(text, "BACKUP_INTERVAL_HOURS")
    assert interval, "compose 里没找到 BACKUP_INTERVAL_HOURS 的默认值"

    for label, keep_key in (("pg 备份（不可再生）", "BACKUP_KEEP"),
                            ("qdrant 备份（可从语料重建）", "QDRANT_BACKUP_KEEP")):
        keep = _compose_env_default(text, keep_key)
        assert keep, f"compose 里没找到 {keep_key} 的默认值"
        window_h = float(interval) * float(keep)
        assert window_h >= 7 * 24, (
            f"{label} 的可回溯窗口只有 {window_h:.0f}h（< 168h = 7 天）："
            f"interval={interval}h × keep={keep}。改间隔时**必须**同步改保留份数。"
        )


def test_backup_loops_publish_prometheus_textfiles():
    """两个备份循环都必须把"上次成功时间"写成 Prometheus textfile（原子替换）。

    备份容器本身不提供任何可抓取的端点，而 `BackupLoopFailing` 要判定
    "上次成功是多少秒之前" —— 唯一途径就是让它写一个 `.prom` 给
    node-exporter 的 textfile collector 读走。

    且必须是**原子替换**（写临时文件再 mv）： collector 可能正好在写的中途抓取，
    半截文件会让它报错并**丢掉整个文件里所有指标**。
    """
    text = BASE.read_text(encoding="utf-8")
    for target in ("pg", "qdrant"):
        assert f"backup-{target}.prom" in text, (
            f"backup-{target} 没有写出 backup-{target}.prom —— 它的成败将不可观测"
        )
    assert "backup_last_success_timestamp_seconds" in text, (
        "备份循环没有暴露 backup_last_success_timestamp_seconds"
    )
    assert "backup_backend_skipped" in text, (
        "备份循环没有暴露 backup_backend_skipped —— Qdrant 未启用时会产生永不恢复的假告警"
    )
    assert "mktemp" in text, (
        "备份指标不是原子发布的 —— 请用 `mktemp` 写临时文件再 mv，"
        "否则 textfile collector 抓到半截文件会丢掉整批指标"
    )


def test_node_exporter_collects_the_textfile_directory(base_cfg):
    """node-exporter 必须挂上 textfile 目录**并**启用对应 collector。

    少任何一个，备份指标链路都会**静默断掉**：备份在跑、`.prom` 在写，
    但 Prometheus 里查不到任何 `backup_*` 序列（这正是 `BackupMetricsMissing`
    存在的原因 —— 它专门抓这种"指标本身不见了"的状态）。
    """
    svc = (base_cfg.get("services") or {}).get("node-exporter")
    assert svc, "没有 node-exporter —— 磁盘、内存与备份指标全部缺失"

    cmd = svc.get("command") or []
    cmd = cmd if isinstance(cmd, list) else [cmd]
    assert any("--collector.textfile.directory" in str(c) for c in cmd), (
        "node-exporter 没有启用 textfile collector（--collector.textfile.directory）"
    )

    mounts = [str(v) for v in (svc.get("volumes") or [])]
    assert any("backups:/textfile:ro" in m for m in mounts), (
        "node-exporter 没有把 backups/ 以**只读**方式挂到 textfile 目录"
        "（写职责在备份容器，exporter 只需要读）"
    )


def test_retention_sweep_is_scheduled_and_safe_by_default():
    """表数据保留清理必须真的有东西在跑，且默认是"拒绝删"的安全态。

    `scripts/retention_sweep.py` 之前**只活在 docstring 的 cron 示例里** ——
    等于没有任何东西在清理，而检查点表、`review_queue`、`session_owners`
    三张表都只增不减。这类问题"半年后才疼、疼起来最难查"：等症状出现时，
    VACUUM 跟不上、索引膨胀、备份窗口从分钟级变成小时级，而**没有任何告警**
    会指向"表太大了"。

    同时必须确认默认值是 0（不清理）：删数据不可逆，一个没配好的默认值
    等于在部署当天就开始删历史。`retention_sweep.py` 在 `RETENTION_DAYS<=0`
    时的行为是"拒绝执行并退出 2"，而不是"静默不清理"。
    """
    text = BASE.read_text(encoding="utf-8")
    cfg = yaml.safe_load(text)
    assert "retention-sweep" in (cfg.get("services") or {}), (
        "没有 retention-sweep 服务 —— 保留清理仍然只活在文档里"
    )
    assert "retention_sweep.py" in text, "retention-sweep 服务没有调用 retention_sweep.py"

    default_days = _compose_env_default(text, "RETENTION_DAYS")
    assert default_days is not None, (
        "retention-sweep 没有从 RETENTION_DAYS 取保留期（口径必须与 .env 同源）"
    )
    assert default_days == "0", (
        f"RETENTION_DAYS 的 compose 插值默认值是 {default_days!r} —— 删数据不可逆，"
        "默认必须是 0（不清理）。\n"
        "⚠️ 别退化成『全文里有没有 ${RETENTION_DAYS:-0} 这个子串』：shell 那段 "
        '`if [ "$${RETENTION_DAYS:-0}" -le 0 ]` 里也有它，全文匹配会让'
        "『把 compose 默认值改成 90』测不出来（变异验证抓到过这个假绿）。"
    )


# ---------------------------------------------------------------------------
# 依赖自动更新：让安全补丁从"CI 某天变红"变成"提前收到 PR"
# ---------------------------------------------------------------------------
DEPENDABOT = ROOT / ".github" / "dependabot.yml"

# 官方文档给的 `schedule.interval` 取值。写错时 Dependabot **不报错**，
# 只是这条 schedule 安静地不生效 —— 与这一层其它配置的失效方式一模一样。
DEPENDABOT_INTERVALS = {
    "daily", "weekly", "monthly", "quarterly", "semiannually", "yearly", "cron",
}


def test_dependabot_config_exists_and_is_valid_yaml():
    assert DEPENDABOT.exists(), (
        "没有 .github/dependabot.yml —— 依赖只能靠人盯，"
        "安全补丁要等到 CI 某天变红才发现"
    )
    data = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    assert data.get("version") == 2, "dependabot 配置的 version 必须是 2"
    assert data.get("updates"), "updates 为空 = 配了等于没配"


def test_dependabot_covers_python_and_ci_actions():
    """Python 依赖与 CI 自身用的 action 是两块**必须**覆盖的面。

    - `pip`：生产镜像实际安装的就是 `requirements.txt`；
    - `github-actions`：不覆盖的话 `actions/checkout@v3` 这类会长期留在仓库里 ——
      action 有自己的安全问题，而且它是**跑在你的 CI 里**的代码。
    """
    data = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    kinds = {u.get("package-ecosystem") for u in data["updates"]}
    assert {"pip", "github-actions"} <= kinds, f"缺少生态：{sorted(kinds)}"


def test_dependabot_entries_are_well_formed():
    """每条都要有合法的 directory(ies) 与 interval。

    这两项都属于"写错了不报错、只是不生效"：`directory` 指向一个不存在的路径
    ⇒ Dependabot 找不到清单、安静地什么都不做；`interval` 拼错 ⇒ 同样安静。
    """
    data = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    for u in data["updates"]:
        eco = u.get("package-ecosystem")
        dirs = u.get("directories") or ([u["directory"]] if u.get("directory") else [])
        assert dirs, f"{eco} 既没有 directory 也没有 directories —— Dependabot 找不到清单"
        for d in dirs:
            assert d.startswith("/"), f"{eco} 的目录 {d!r} 必须以 / 开头（相对仓库根）"
        interval = (u.get("schedule") or {}).get("interval")
        assert interval in DEPENDABOT_INTERVALS, (
            f"{eco} 的 interval={interval!r} 不是合法取值（{sorted(DEPENDABOT_INTERVALS)}）"
        )


def test_dependabot_documents_the_lockfile_step():
    """pip 的 PR 合并前必须重跑 lock 脚本 —— 这一条**必须写在配置里**。

    它是"配上自动更新之后新长出来的一步"：`requirements.lock` 钉精确版本、CI 有一致性
    门禁，而 Dependabot 不认识那个文件。忘了这一步的后果是**镜像构建失败**，而不是门禁
    变红 —— 两种失败隔得很远，人很容易在 CI 全绿的情况下合进去。
    """
    text = DEPENDABOT.read_text(encoding="utf-8")
    assert "lock_requirements.py" in text, (
        "配置里没写『合并前跑 scripts/lock_requirements.py --write』——"
        "照常合并会让镜像构建失败，而 CI 门禁照样全绿"
    )


# ---------------------------------------------------------------------------
# 客户端 IP 从哪来（2026-10-04）
# ---------------------------------------------------------------------------
# 应用侧按 IP 的限流（含 `POST /v1/auth/login` 的防爆破闸门）读的是转发头里的
# 客户端地址，而 `app/main.py` 的 `_client_ip` 取的是该头的**第一段**。
#
# `$proxy_add_x_forwarded_for` 的官方语义是「把客户端自带的 X-Forwarded-For
# **原样保留在左**，再把 `$remote_addr` **追加在右**」（nginx 文档 Embedded Variables）。
# 于是最左段 = **客户端随便写的值** —— 攻击者每换一个值就换一个限流桶，限流整体失效。
# 这件事值得钉住的原因是它**不会报错**：限流器照常工作、日志照常打，
# 只有"攻击者能换桶"这一件事静默成立。
#
# 正确写法是**覆盖**：`proxy_set_header X-Forwarded-For $remote_addr;`
# ⇒ 转发头里只有一段，就是 nginx 认定的客户端地址。
NGINX_CONFS = [DEPLOY / "nginx.conf", DEPLOY / "nginx.tls.conf"]


def test_nginx_overwrites_forwarded_for_instead_of_appending():
    """两份 nginx 都必须**覆盖**写 `X-Forwarded-For`，不得用追加式写法。

    追加式（`$proxy_add_x_forwarded_for`）会让应用取到的第一段是客户端伪造的值
    ⇒ 按 IP 的限流可被绕过（换一个伪造值换一个桶），且**没有任何报错**。
    """
    for conf in NGINX_CONFS:
        text = conf.read_text(encoding="utf-8")
        # 只看**未注释**的指令行：注释里出现这个变量名是说明性的，不是配置。
        active = [ln for ln in text.splitlines()
                  if ln.strip() and not ln.lstrip().startswith("#")]
        offenders = [ln.strip() for ln in active if "proxy_add_x_forwarded_for" in ln]
        assert not offenders, (
            f"{conf.name} 用了追加式写法 —— 客户端伪造的 X-Forwarded-For 会被原样透传，"
            f"而应用取最左段当限流键 ⇒ 按 IP 的限流（含登录防爆破）可被绕过：{offenders}"
        )


def test_nginx_forwarded_for_is_set_from_remote_addr():
    """两份 nginx 都必须**确实**把 `$remote_addr` 写进 `X-Forwarded-For`。

    上一条只禁止了错误写法；若有人把那行直接**删掉**（而不是改对），上一条照样通过，
    但应用会读不到这个头 —— `TRUST_PROXY_HEADERS=1` 时落到"头缺失"的回退分支，
    拿到的是代理自己的地址 ⇒ 全体用户共用一个限流桶。两条一起才把
    "要么写对、要么别动"这个约束钉住。
    """
    pattern = re.compile(
        r"^\s*proxy_set_header\s+X-Forwarded-For\s+\$remote_addr\s*;", re.M)
    for conf in NGINX_CONFS:
        text = conf.read_text(encoding="utf-8")
        assert pattern.search(text), (
            f"{conf.name} 里找不到 `proxy_set_header X-Forwarded-For $remote_addr;` —— "
            "客户端 IP 的来源必须显式写清（覆盖式），既不能换成追加式，也不能删掉"
        )


# ---------------------------------------------------------------------------
# ⑩ 明文入口只绑回环 / TLS 是显式动作 / 内部组件不得对外发布
# ---------------------------------------------------------------------------
# 这三条的失效症状都是**"什么都没有变"**：
#   - 绑了全网卡 ⇒ 服务照常工作，只是同一网段的人能抓到
#     `POST /v1/auth/login` 的口令与 `Authorization: Bearer <token>`；
#   - 内部组件发布了端口 ⇒ Grafana/Prometheus 照常工作，只是任何能访问该端口的人
#     都能看到全量指标（含租户用量）；
#   - Loki 在没有鉴权的情况下发布了端口 ⇒ 日志（可能含用户原文）对全内网可读。
# 没有任何一条会报错或留下日志，所以只能靠静态守卫钉住。
TLS_OVERRIDE = DEPLOY / "docker-compose.tls.yml"
_LOOPBACK_PREFIXES = ("127.0.0.1:", "[::1]:", "localhost:")


def _port_strings(entry) -> str:
    """把 compose 的 `ports` 条目归一成可判定的字符串。

    支持两种写法：短语法 `"127.0.0.1:8080:80"` 与长语法
    `{target: 80, published: "8080", host_ip: "127.0.0.1"}`。
    长语法不认出来就会**静默漏判**（读到的不是字符串，断言直接跳过）。
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return (f"{entry.get('host_ip', '')}:{entry.get('published', '')}"
                f":{entry.get('target', '')}")
    return repr(entry)


@pytest.fixture(scope="module")
def tls_cfg():
    assert TLS_OVERRIDE.exists(), (
        f"缺少 TLS 覆盖文件：{TLS_OVERRIDE}\n"
        "没有它，'对公网提供服务'就只能手改主编排 —— 而手改正是"
        "'不小心把明文入口暴露到公网'的来源"
    )
    return _load_with_compose_tags(TLS_OVERRIDE)


def test_every_published_port_in_the_base_file_is_loopback(base_cfg):
    """主编排里**每一个**发布的端口都必须绑回环。

    主编排面向本机/内网联调，它承载的是**真实凭据**（登录口令、Bearer 令牌）。
    绑定写宽一次（`"8080:80"` 而不是 `"127.0.0.1:8080:80"`），凭据就明文过网 ——
    而服务本身照常工作，没有任何症状。对外暴露必须是**显式叠加 TLS 覆盖文件**。
    """
    offenders: list[str] = []
    for name, svc in (base_cfg.get("services") or {}).items():
        for entry in (svc or {}).get("ports") or []:
            text = _port_strings(entry)
            if not text.startswith(_LOOPBACK_PREFIXES):
                offenders.append(f"{name}: {text}")
    assert not offenders, (
        "主编排里有端口绑到了非回环地址（明文且任何同网段主机可访问）：\n  "
        + "\n  ".join(offenders) + "\n"
        "对外提供服务请叠加 deploy/app/docker-compose.tls.yml，而不是把绑定写宽"
    )


def test_staging_entry_is_loopback_only(staging_cfg):
    """staging 入口同样只绑回环 —— 它跑的是真实账号与真实令牌。"""
    ports = ((staging_cfg.get("services") or {}).get("nginx") or {}).get("ports") or []
    assert ports, "staging overlay 没有覆盖 nginx 的端口（会沿用主编排的）"
    for entry in ports:
        text = _port_strings(entry)
        assert text.startswith(_LOOPBACK_PREFIXES), (
            f"staging 的明文入口绑到了非回环地址：{text} —— "
            "staging 使用真实凭据，明文过网等于把令牌摊在同一网段上"
        )


def test_tls_override_publishes_https_and_replaces_the_plain_config(tls_cfg):
    """TLS 覆盖文件必须：换掉 conf + 发布 80/443 + 只读挂证书。"""
    text = TLS_OVERRIDE.read_text(encoding="utf-8")
    nginx = (tls_cfg.get("services") or {}).get("nginx") or {}

    # 1) conf 必须被换掉（同 target 覆盖），否则 nginx 仍跑明文 conf
    mounts = list(nginx.get("volumes") or [])
    assert any("nginx.tls.conf:/etc/nginx/conf.d/default.conf" in m for m in mounts), (
        f"TLS 覆盖文件没有把 default.conf 换成 nginx.tls.conf，实际挂载：{mounts}"
    )
    assert not any(m.split(":")[0].endswith("nginx.conf") for m in mounts), (
        f"明文 nginx.conf 仍被挂载（会与 tls conf 抢同一个 target）：{mounts}"
    )

    # 2) 证书只读
    assert any(m.endswith(":ro") and "certs" in m for m in mounts), (
        f"证书目录没有以只读方式挂载：{mounts}"
    )

    # 3) 端口：必须用 `!override`，否则 compose 会**拼接**上主编排的 8080
    assert "ports: !override" in text, (
        "TLS 覆盖文件里 `ports` 没有用 `!override` —— compose 对 ports 是"
        "**拼接**语义，结果是同时绑 8080 与 80"
    )
    published = {_port_strings(e) for e in (nginx.get("ports") or [])}
    assert {"80:80", "443:443"} <= published, (
        f"TLS 覆盖文件必须发布 80（ACME/跳转）与 443，实际：{published}\n"
        "少了 80 ⇒ 证书无法用 http-01 续期，而续期失败通常到过期当天才发现"
    )


def test_internal_observability_services_never_publish_ports(base_cfg):
    """无鉴权的内部组件（Loki / Promtail）不得发布端口。

    `deploy/monitoring/loki.yml` 里写着 `auth_enabled: false` —— 这是**单机 + 仅内网**
    前提下才成立的取舍：Loki 里存的是应用日志，可能含用户原文与
    `request_id` 关联的完整链路。一旦给它加了 `ports:`，它就在编排网络之外也可读了，
    而"日志泄漏"没有任何运行期信号。
    （Grafana / Prometheus / Alertmanager 有各自的鉴权，但它们也**必须**绑回环 ——
    由上一条「所有发布端口必须回环」统一兜住。）
    """
    for name in ("loki", "promtail"):
        svc = (base_cfg.get("services") or {}).get(name)
        assert svc is not None, f"主编排里找不到 {name} 服务（守卫引用的名字已过期）"
        assert not svc.get("ports"), (
            f"{name} 发布了端口：{svc.get('ports')} —— 它没有鉴权，"
            "发布出去等于把应用日志（可能含用户原文）对更大范围放开"
        )


def test_loki_config_records_the_unpublished_precondition():
    """Loki 关掉鉴权的**前提**必须写在配置里（注释即契约）。

    为什么断言注释而不是只看 `auth_enabled: false`：
    `auth_enabled: false` 本身在单机内网下是合理取舍，问题在于**前提会丢**。
    下一个人打开这个文件，看到的只是一个 `false` —— 他没有任何线索知道
    "它之所以安全，是因为没人给它开端口"。前提一旦脱离配置，就没人会维护它。
    所以把前提写成注释，并让守卫钉住它；删注释 = 变红，被迫重新面对这个取舍。
    """
    text = (ROOT / "deploy" / "monitoring" / "loki.yml").read_text(encoding="utf-8")
    assert re.search(r"^\s*auth_enabled:\s*false\s*$", text, flags=re.M), (
        "Loki 的 auth_enabled 已不是 false —— 那就请同步复核本文件里"
        "「内部组件不得发布端口」这条守卫的必要性与措辞"
    )
    assert "绝不发布端口" in text, (
        "loki.yml 里没有写明「关鉴权的前提是不发布端口」—— "
        "前提脱离配置之后，下一个人只会看到一个孤零零的 false，"
        "而它看起来像「忘了配鉴权」"
    )
    assert "X-Scope-OrgID" in text, (
        "loki.yml 里没有写明多租户时该怎么改（auth_enabled: true + X-Scope-OrgID）—— "
        "踩到这个场景的人会以为只能加个用户名密码"
    )


def test_services_never_point_at_localhost(base_cfg):
    """任何服务的 `environment` 里都不许出现 `localhost` / `127.0.0.1`。

    容器内的 `localhost` 指**容器自身**，不是宿主机。`.env` 里那些
    `http://localhost:6333`、`...@localhost:5433` 是给**本机直跑**用的，
    一旦透传进容器就必然连不上 —— 而失败方式各不相同、**没有一个会说
    "地址配错了"**：备份循环播报"跳过"、应用静默回退内存后端（或内存向量检索）。

    2026-10-06 的事故正是这一类：`backup-qdrant` 写成
    `QDRANT_URL: ${QDRANT_URL:-http://qdrant:6333}`，而 `.env` 里那份是
    `http://localhost:6333` ⇒ 容器内连自己 ⇒ 每轮 rc=2；偏偏 `BackupLoopFailing`
    的判据 `(backup_backend_skipped != 1)` 又主动排除这种情形（那条是为
    "Qdrant 未启用"设计的）⇒ **向量库从未被备份且全程零告警**。
    """
    bad = {}
    for name, svc in (base_cfg.get("services") or {}).items():
        env = svc.get("environment") or {}
        if not isinstance(env, dict):
            continue                      # 列表形式的 environment 在 base_cfg 里不该出现
        for key, val in env.items():
            if isinstance(val, str) and ("localhost" in val or "127.0.0.1" in val):
                bad.setdefault(name, []).append(f"{key}={val}")
    assert not bad, (
        f"以下服务的 environment 指向了 localhost：{bad}\n"
        "容器内的 localhost 是它自己；请改成集群内服务名（如 http://qdrant:6333）"
    )


def test_cluster_addresses_are_not_piped_from_host_env(base_cfg):
    """容器内地址必须是**字面量**，不能整体引用 `.env` 里的同名宿主机变量。

    与上一条是两件事：上一条查"文本里写没写 localhost"，这一条查"是不是把
    `.env` 那份宿主机地址**引用**了进来"。引用形式在 compose 文本里看不见
    localhost，所以上一条**挡不住它** —— 而这正是本次事故的实际形态。

    `DB_URL` 允许插值，但只能插**口令**那一段（`${POSTGRES_PASSWORD:?...}`），
    主机名必须是 `pgvector` 字面量。
    """
    services = base_cfg.get("services") or {}
    for svc_name in ("app-stable", "app-canary", "backup-qdrant"):
        svc = services.get(svc_name)
        assert svc is not None, f"{svc_name} 不见了（本测试的清单需要同步）"
        env = svc.get("environment") or {}

        if "QDRANT_URL" in env:
            assert env["QDRANT_URL"] == "http://qdrant:6333", (
                f"{svc_name} 的 QDRANT_URL 不是字面量服务名：{env['QDRANT_URL']!r}\n"
                "它必须硬编码 `http://qdrant:6333` —— 引用 ${QDRANT_URL} 会把 .env 里"
                "给本机直跑用的 localhost 带进容器"
            )
        if "DB_URL" in env:
            assert "@pgvector:5432" in env["DB_URL"], (
                f"{svc_name} 的 DB_URL 没指向集群内服务名：{env['DB_URL']!r}"
            )
            assert "localhost" not in env["DB_URL"], (
                f"{svc_name} 的 DB_URL 里出现了 localhost"
            )


# ---------------------------------------------------------------------------
# nginx：后端地址必须"请求时解析"，且**与镜像版本绑定**
# ---------------------------------------------------------------------------
#: `resolve` 参数进入**开源版**的最小版本。
#: 官方原文："Prior to version 1.27.3, this parameter was available only as part of our
#: commercial subscription"（nginx.org 的 ngx_http_upstream_module 文档，`server` 指令段）。
_NGINX_RESOLVE_MIN = (1, 27, 3)


def _upstream_active_lines(path: Path) -> list:
    """抽出 upstream 块里**有语义**的行（去掉注释与空行）。"""
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^upstream\s+\w+\s*\{([\s\S]*?)^\}", text, re.M)
    assert m, f"{path.name} 里找不到 upstream 块 —— 本测试的解析需要同步"
    out = []
    for raw in m.group(1).splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def test_nginx_upstream_is_dynamically_resolved(base_cfg):
    """nginx 必须**在请求时**解析后端地址，而且镜像版本必须支撑这一点。

    **为什么（2026-10-06 改）**：`server app-stable:8000` 这种写法下，nginx 只在
    **加载配置那一刻**把域名解析成 IP 并缓存。官方对 `resolve` 参数的说明是决定性判据 ——
    加了它才会 "monitor changes of the IP addresses ... **without the need of restarting
    nginx**"。不加的时候，谁重建了 app 容器（换镜像 tag / 手工 `up -d`）换了 IP，
    nginx 就继续往**旧地址**发请求：走 nginx 那条路**全站 502 且不自愈**，
    而容器状态、`/health/ready`、Prometheus 的 `up` **全都正常**，只有经 nginx 的路是死的；
    旧 IP 还可能被别的容器拿走，请求被发到无关服务上。

    **这条守卫为什么要把三件事一起查**：它们必须**同时**成立才有意义 ——
      · 有 `resolve` 但没有 `zone` ⇒ nginx 拒绝启动（官方：server group must reside in
        the shared memory）；
      · 有 `resolve` 但没有 `resolver` ⇒ 同上（`resolve` 需要一个解析器）；
      · 三件套齐全但镜像 < 1.27.3 ⇒ **`resolve` 这个参数根本不存在** ⇒ nginx 起不来。
    只查其中一件，就等于把"入口整个起不来"留到上线那一刻才发现。
    而这些**都不是静默失败** —— 它们让 nginx 直接拒绝启动，所以必须提前挡住。
    """
    image = (base_cfg["services"]["nginx"] or {}).get("image", "")
    m = re.search(r"nginx:(\d+)\.(\d+)\.(\d+)", image or "")
    assert m, (
        f"nginx 镜像没有钉到具体补丁版（当前 {image!r}）—— `resolve` 参数有 1.27.3 的硬下限，"
        "浮动的 tag 在不同 registry / 旧缓存上可能解析到更低的版本，而那样 nginx 会起不来"
    )
    got = tuple(int(x) for x in m.groups())
    assert got >= _NGINX_RESOLVE_MIN, (
        f"nginx 镜像 {image} 低于 {'.'.join(map(str, _NGINX_RESOLVE_MIN))}，"
        "而配置里用了 `resolve` 参数（该参数此前仅商业订阅可用）⇒ nginx 直接起不来"
    )

    for conf in NGINX_CONFS:
        lines = _upstream_active_lines(conf)
        joined = "\n".join(lines)
        assert re.search(r"^resolver\s+\S+", joined, re.M), (
            f"{conf.name} 的 upstream 里没有 `resolver` —— `resolve` 无法工作"
        )
        assert re.search(r"^zone\s+\w+\s+\S+", joined, re.M), (
            f"{conf.name} 的 upstream 没有声明 `zone` —— 官方要求 server group 位于共享内存，"
            "缺了 nginx 起不来"
        )
        servers = [ln for ln in lines if ln.startswith("server ")]
        assert len(servers) >= 2, f"{conf.name} 的 upstream 里 server 行少于 2 条：{servers}"
        for ln in servers:
            assert re.search(r"\bresolve\s*;", ln), (
                f"{conf.name} 的这条 server 没有 `resolve` "
                f"（会被当成「只在加载时解析一次」，容器换 IP 后全站 502）：{ln}"
            )


def test_the_two_nginx_configs_resolve_the_same_way():
    """两份 nginx 配置的**解析指令**必须完全一致（zone / resolver / server 三行）。

    它们是**互斥**挂载的（都挂到 `/etc/nginx/conf.d/default.conf`：主文件是明文入口，
    `docker-compose.tls.yml` 换成 TLS 版），平时不同时生效 ⇒ **改一份忘另一份不报错**，
    只会在"切到 TLS 之后"悄悄退回旧行为 —— 而那时多半正是对公网提供服务的时候。

    只比对**解析相关**的行（`zone` / `resolver` / `server`），不比对整个块：
    权重已经由 `tests/test_release_tooling.py::test_repo_entry_confs_agree_on_weights`
    负责；而将来若有意让 TLS 版用不同的超时参数，也不该被这条挡住。
    （"两份各改一半"在这个仓库里已经栽过一次：环境变量读取的正则被两个文件各维护一份。）
    """
    def resolve_lines(path: Path) -> list:
        return [ln for ln in _upstream_active_lines(path)
                if ln.startswith(("zone ", "resolver ", "server "))]

    a, b = (resolve_lines(c) for c in NGINX_CONFS)
    assert a == b, (
        "两份 nginx 配置的后端解析指令不一致（改一份忘另一份不会报错，"
        "只会在切到 TLS 后悄悄退回旧行为）：\n"
        f"  {NGINX_CONFS[0].name}: {a}\n"
        f"  {NGINX_CONFS[1].name}: {b}"
    )

