"""部署拓扑的分层约束（staging overlay + 灰度文档）。

为什么需要这个测试
------------------
`docker-compose.staging.yml` 里的三个错误**全都不会报错**，只会在最坏的时刻
以最坏的方式体现出来：

  1. **忘了改 `name:`** ⇒ 与生产**共用同一套命名卷**（compose 的项目名默认取自
     目录名，两个文件在同一目录就是同一个项目）。表现是"staging 起来了、一切正常"，
     而你在 staging 里跑的灌库/清洗**作用在生产数据上**。
  2. **`APP_ENV` 写成了 `staging`** ⇒ `app/config.py` 只把 `production`/`prod`
     当生产，于是 `REQUIRE_AUTH` 默认为假、`ENABLE_DOCS` 默认为真。
     表现是"staging 上验证通过"，然后在生产上第一次遇到 401 或"拒绝启动"。
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
    鉴权被静默关掉的 staging，而它看起来和生产一模一样。

    环境标识请用 `APP_RELEASE`：它只影响日志与指标标签，不改变任何开关。
    """
    env = (staging_cfg["services"]["app-stable"].get("environment") or {})
    assert "APP_ENV" in env, (
        "staging 没有显式设置 APP_ENV —— 会沿用主文件的 ${APP_ENV:-production}，"
        "该插值在部署机 export 过 APP_ENV 时会被带偏"
    )
    value = str(env["APP_ENV"])
    assert value == "production", (
        f"staging 的 APP_ENV={value!r}。写成 staging/prod-xxx 都会因为 "
        "app/config.py 的 IS_PRODUCTION 只认 production/prod 而**静默关掉鉴权、"
        "打开 /docs**；环境标识请用 APP_RELEASE"
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
    """必须点明 `SERVE_API_KEY` 是单值、**做不到**零停机轮换。

    把它写成"按流程轮换即可"会让运维在生产上硬切，然后所有调用方同时 401。
    诚实地写出"当前做不到、需要改代码"，才能被排进计划而不是现场发现。
    """
    text = SECRETS_DOC.read_text(encoding="utf-8")
    assert "SERVE_API_KEY" in text
    assert "多值" in text or "列表" in text, (
        "文档没有指出 SERVE_API_KEY 需要支持多值才能零停机轮换"
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
