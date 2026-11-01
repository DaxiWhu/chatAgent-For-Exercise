# 密钥管理（现状、风险与迁移路径）

> 本文记录密钥的**现状**、**为什么它是个真问题**、以及**分几步能改好**。
> 刻意不写成"最佳实践清单"：那种清单落地率极低，因为它不告诉你**第一步改什么**。

---

## 一、现状

所有密钥都在项目根的 `.env` 里，以**明文**形式存在，通过三种方式进入系统：

| 用途 | 传递方式 | 谁在用 |
|---|---|---|
| 应用密钥（`LLM_API_KEY`、`EMBEDDING_API_KEY`、`LANGSMITH_API_KEY`） | compose 的 `env_file: ../../.env` → 容器环境变量 | `app/config.py` 读取 |
| ~~共享密钥 `SERVE_API_KEY`~~ | **已于 2026-10-05 整体删除** | 不再是"应用密钥"：它不是本服务的任何凭证。⚠️ 环境里**存在**该键（留空也算）会让服务拒绝启动，见 4.2 |
| 用户鉴权（`AUTH_SESSION_SECRET`、`AUTH_USERS`） | 同上 | `app/config.py` 读取 → `app/auth.py` 消费。⚠️ `AUTH_USERS` 已降级为"只在账号表为空时建出第一个管理员"的**种子**；用户与口令哈希真正住在 PG 的 `users` 表里 |
| 基础设施口令（`POSTGRES_PASSWORD`、`REDIS_PASSWORD`、`GRAFANA_PASSWORD`） | compose 的 `${VAR}` 插值（从项目根 `.env`） | compose 自己，用于拼 `DB_URL` / 容器参数 |
| 通知凭据（`ALERT_SINK_*_WEBHOOK`） | 同上 → `alert-sink` 容器环境变量 | `deploy/monitoring/alert_sink.py` |

`.env` 与 `.env.*` 已被 `.gitignore` 忽略（`.env.example` 是唯一的例外，里面只有
刻意的开发默认值）。CI 的 `security` job 用 gitleaks 扫当前工作树，
配置见仓库根 `.gitleaks.toml`。

> **加密入库的脚手架已就位（2026-10-03）**：`secrets/app.sops.env` 是密文、随仓库分发；
> 明文 `.env` 仍然只在本机。配置在 `.sops.yaml`，命令是 `make secrets-encrypt` /
> `make secrets-decrypt`（见 §三 第 1 步）。
> **还差的一步只能由人来做**：跑一次 `age-keygen` 并把公钥填进 `.sops.yaml`。
> 在那之前加密会**直接报错**（这是刻意的：没有收件人就不该悄悄用一把谁也没有的钥匙）。

### 已经做对的部分

- `.env` 不进版本库；`.env.example` 只放占位值；
- 密钥在启动日志里**只回显"有没有配"**，不回显内容（`app/config.py` 的 `as_dict()`：
  `auth.script_key` 是布尔、`user_auth.seed_users` 只回显**条数**）；
- 对外错误走 `_client_error()`，生产环境只给通用文案 + `request_id`，
  不会把上游报错里的地址/凭据漏给客户端（`tests/test_identity.py` 有断言钉住）；
- CI 有密钥扫描（gitleaks，**卡门禁**）。

---

## 二、为什么这仍然是个待办项

三个具体问题，都不是"理论风险"：

### 2.1 环境变量对同机进程与 `docker inspect` 可见

```
docker inspect chatagent-app-stable-1 --format '{{json .Config.Env}}'
```
任何能执行 docker 命令的人（或任何以 root 运行的过程）都能直接读出全部密钥。
`/proc/<pid>/environ` 同理。

**这不是"配置不好看"，而是"权限边界消失了"**：能 `docker ps` 的人本该只是
"能看容器状态"，实际上却拿到了数据库口令与模型密钥。

### 2.2 没有轮换流程；换得动的那条没启用，在用着的那条换不动

- 没有任何地方记录"这个密钥上次什么时候换的"；
- ~~**`SERVE_API_KEY` 是单值**：换它意味着旧 key 在同一刻失效 ⇒ 调用方同时中断。~~
  **2026-10-05 已解决，方式是删除而不是修补**：这条共享密钥（以及 `TENANT_KEYS`）
  已整体从代码里删除。它的三个问题（分不出身份 / 撤不掉 / 租户只能自报）都是
  **结构性**的，只要留着一行配置就总有人去配它，所以修法不是"给它加重叠窗口"
  而是让它不存在 —— 现在机器调用方走的是与用户完全相同的 JWT 路径，
  于是**轮换问题变成"给服务账号换口令"**，不再有"换密钥就全体中断"这一回事。
  详见 4.2（含已执行的删除清单）。
- `AUTH_SESSION_SECRET` 换起来很容易（`openssl rand` 一行），代价是所有会话立即失效、
  用户重新登录一次 —— 对多数部署这是**可接受**的，见 4.4。
- `POSTGRES_PASSWORD` 更麻烦：它**只在数据卷首次初始化时生效**
  （见 `deploy/app/docker-compose.yml` 顶部说明），换它需要 `ALTER USER` 或删卷。
  2026-10-04 核实：`.env` 里 `DB_URL` 用的**仍是默认口令**，从未换过。

### 2.3 泄漏的影响面是"全量"

一份 `.env` 同时包含数据库口令、模型密钥、租户密钥、通知 webhook。
任何一处泄漏（误提交、备份文件外流、离职交接遗漏、被入侵的开发机）都等于**全部**。

---

## 三、迁移路径（三步，按成本递增）

### 第 1 步（脚手架已落地，待本机生成密钥）：用 SOPS + age 把 `.env` 加密入库

**产物**：`secrets/app.sops.env`（密文，**进仓库**）+ 仓库根的 `.env`（明文，仍被 gitignore）。
**配置**：`.sops.yaml` —— 里面只有**公钥**；私钥在 `~/.config/sops/age/keys.txt`，
**绝不进仓库**。"谁能解密"由私钥发给谁决定，不由配置文件决定。

一次性准备（每台需要解密/加密的机器都要有私钥）：

```bash
age-keygen -o ~/.config/sops/age/keys.txt   # 会打印公钥（age1 开头）
# 把公钥粘进 .sops.yaml 的 `age:` 一行，替换掉 AGE_PUBLIC_KEY_PLACEHOLDER
```

之后只有两条命令：

```bash
make secrets-encrypt   # 明文 .env  →  密文 secrets/app.sops.env（改完密钥后跑一次）
make secrets-decrypt   # 密文       →  明文 .env（换机器/部署；已有 .env 时要加 --force）
```

Windows 上没有 `make`，用 `python scripts/tasks.py secrets-encrypt`（同一条逻辑）。
CI / Linux 上也可以直接跑 `make`。

**四条不写下来一定会踩的**（都实测过，脚本在 `tmp_probe/sops_probe.py`）：

1. **密文名必须以 `.env` 结尾。** sops 按**后缀**推断格式（`cmd/sops/formats.FormatForPath`
   只认 `.yaml/.yml/.json/.env/.ini`，**其余一律 Binary**）。后缀写成 `.enc` 时它把整份文件
   当成一块二进制加密 —— 虽然**也能解回来**，但看不到里面有哪些键、没法用 `sops set`
   按单键增删改、diff 也看不出改的是哪一条。而"换某一条密钥"正是每天都在做的事。
2. **`--filename-override` 是必需的**（命令里已带上）。`creation_rules` 是按**输入路径**
   匹配的，输入是 `.env`（不在 `secrets/` 下），少这个参数直接报
   `no matching creation rules found`。
3. **Windows 上 `path_regex` 匹配到的分隔符是反斜杠**（`--filename-override` 会先过
   `filepath.Abs`）。所以 `.sops.yaml` 里写成 `[/\\]`。实测：只写 `/` 时，Windows 上
   加密**一次都不会成功**，报的还是那句与"配置写错了"无法区分的错。
4. **密文名不能以 `.env.` 打头。** `.gitignore` 的 `.env.*` 不带斜杠、匹配**任意层级**的
   文件名 —— 叫 `.env.enc` 的话 `git add` 会**静默跳过**，加密了半天仓库里什么都没有。

**收益**：仓库里不再有明文密钥；谁能解密由 age 私钥发给谁决定（可分发、可收回）。
**成本**：低（两个单文件二进制 + 一把 age 密钥），**不改任何应用代码**。
**这一步解决 §2.3 的"误提交 / 打包外流"**：`git add -f .env`、`tar` 打包整个目录、
`.gitignore` 写错，这三条路都被"仓库里那份本来就是密文"兜住了。**不解决 §2.1**
（环境变量仍然对同机进程与 `docker inspect` 可见）。

⚠️ **那把私钥的备份就是全部。** `~/.config/sops/age/keys.txt` 丢了，
`secrets/app.sops.env` 就再也解不开 —— 没有"找回密码"这条路。
首次生成之后请立刻单独备份（密码管理器 / 离线介质），这份备份**不能**放进仓库。

### 第 2 步：把密钥从环境变量挪到文件挂载（Docker secrets）

**改什么**：compose 用 `secrets:` 挂载（落在 `/run/secrets/<name>`），
应用侧加一层"先读文件、再读环境变量"的兼容垫片：

```python
# app/config.py 里 _env_str / _env_bool 的统一入口加这一层（示意）
def _secret(name: str, default: str = "") -> str:
    """优先读 /run/secrets/<name>（Docker secrets 挂载点），退回环境变量。

    迁移期两种都要能读：容器还没切到 secrets 时环境变量仍在，
    切过去之后文件优先。等全部切换完成再删掉环境变量分支。
    """
    path = Path("/run/secrets") / name.lower()
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return (os.environ.get(name) or default).strip()
```

**收益**：`docker inspect` / `/proc/*/environ` 里不再有密钥（2.1 解决）。
**成本**：中（改 `config.py` 的取值入口 + compose + 一条测试钉住"文件优先"）。
**注意**：这不是"额外加密"，是"换了一条只有目标容器能读的传递路径"，
所以仍然要配合第 1 步（文件本身不能进仓库）。

### 第 3 步（团队规模上来之后）：Vault / 云 KMS

**什么时候值得**：需要**审计日志**（谁在什么时候读了哪个密钥）、
**自动轮换**（租约到期自动换）、**动态凭据**（每次连接生成短期 DB 账号）。
**成本**：高（多一个必须高可用的服务，以及它自己的密钥怎么存的问题）。

先做第 1、2 步。**在没有 Vault 之前，第 1 步就能挡掉最危险的场景（明文进仓库）。**

---

## 四、轮换流程

### 4.1 零停机轮换的前提：**验证端能同时认两把钥匙**

"先加新的、再删旧的"这条顺序，只有在"验证端能同时接受新旧两把钥匙"时才是零停机。
验证端只认一把时，加与删之间不存在重叠窗口 —— 顺序本身救不了你，先删后加更是
**全体 401**，而且看起来像"服务坏了"，排查方向会先跑到鉴权代码上。

按这个前提把本项目的密钥分一下：

| 密钥 | 验证端能否同时认两把 | 结论 |
|---|---|---|
| ~~外部 IdP 的签名密钥~~ | **该能力已删除**（外部身份源 OIDC / JWKS 已整体删除，见 4.2.2） | 不再是可选项；删掉后本表已无"天然能重叠"的密钥 |
| `AUTH_SESSION_SECRET` | 不需要 —— 换它等于**所有会话立即失效**，用户重新登录一次 | 见 4.4 |
| ~~`SERVE_API_KEY`（共享密钥）~~ | **该凭证已删除**（不是"不能重叠"，是"不存在"） | 见 4.2 |
| `POSTGRES_PASSWORD` | 不适用 —— 换它受"数据卷首次初始化"约束 | 见 4.3 |

### 4.2 `SERVE_API_KEY`（共享密钥）：**已整体删除**（2026-10-05）

⚠️ **本节留在这里是为了记住"为什么删"，不是操作指南。** 这条路径已经不存在：
`app/config.py` 里没有 `SERVE_API_KEY`，`app/main.py::_authenticate` 只剩
`Authorization: Bearer` 一条入口，25 个端点签名上的 `x_api_key` 参数也已删除。

删除前的状态（供核对"是否还有遗漏"）：

- 它**是单值**：验证端只有 `hmac.compare_digest(请求头, 那一个常量)`，没有"新旧并存"
  的窗口 ⇒ 换值 = 改配置 + 重启 = 踢掉所有在线调用方；
- 早先的修法建议是"迁到 `TENANT_KEYS`（那张 `{密钥: 租户}` 的表天然支持同一租户
  登记两把钥匙）"，**该表也已删除**（租户改由 JWT 载荷承载）；
- ⇒ 结论是让整条路径消失，而不是给它补一个重叠窗口。

它的三条缺陷都是**结构性**的（不是"密钥不够长"），这也是"删"而不是"留空"的理由：

1. **分不出是谁** —— 所有调用方共用同一个值 ⇒ 审核留痕归不到人；
2. **撤不掉** —— 单值、无过期、无重叠窗口；
3. **带不了租户** —— 它没有载荷，租户只能靠 `X-Tenant-Id` **自报且被采信** ⇒
   持它者换个请求头就能切检索分区、并绕过按租户分桶的配额（这就是"生产环境
   只配它时拒绝启动"的原因）。

**机器调用方现在用什么**：`POST /v1/auth/login` 换 Bearer 令牌（服务账号）。
共用实现 `scripts/script_auth.py`，用法 `examples/http_client.py login`。
于是"凭证轮换"变成了"给服务账号换口令"—— 与人类用户同一套流程，不再有
"换一把密钥就把全体调用方打断"这种东西。

**守卫（防止它被重新引入）**：

- 启动期：环境里**存在** `SERVE_API_KEY` 或 `REQUIRE_AUTH`（**留空也算**）⇒
  `app/main.py::_assert_no_legacy_auth_knobs` **拒绝启动**；
- 测试期：`tests/test_user_auth.py::test_api_key_header_is_not_a_credential_anymore`
  （带 `X-API-Key` 必得 401）、`test_authenticate_takes_no_shared_key_parameter`
  （`_authenticate` 签名里不许再出现密钥参数）、
  `tests/test_secrets_config.py::test_no_shared_secret_knob_can_change_authz_outcome`
  （`config` / `main` 两处都不许再有这三个名字）；
- 模板：`.env.example` **不再声明**该键
  （`tests/test_env_example.py::test_deleted_shared_key_is_not_declared_at_all`）。

#### 4.2.1 退役过程与删除清单（**2026-10-05 已执行完毕**）

方向已拍板：**给脚本建服务账号，走 `POST /v1/auth/login` 换令牌**（不是把一份长期
refresh token 塞进 `.env`）。已落地的部分：

- **共用实现** `scripts/script_auth.py`：读 `AGENT_SCRIPT_USER` / `AGENT_SCRIPT_PASSWORD`
  → 登录 → 产出 `Authorization: Bearer …` 请求头；
- **三个调用方已切换**：`scripts/load_test.py`、`tests/test_e2e_stack.py`
  （用自己的 `E2E_USER` / `E2E_PASSWORD`，回退到 `AGENT_SCRIPT_*`）、
  `examples/http_client.py`（新增 `login` 子命令）；
- `scripts/tasks.py e2e` 不再把 `SERVE_API_KEY` 当主路径 —— 只在 `.env` 里确实还配着它时
  回退使用，并打印一条废弃提醒。

当年列出的三个问题，现在两个有答案、一个仍卡着：

1. **凭证怎么落盘** —— ✅ 用户名 + 口令走登录。口令**只从环境变量读、不提供命令行参数**
   （明文进 shell history 与 `ps` 输出是这类泄漏最常见的形态，与 `hash_password` 的口径一致）。
2. **access token 的有效期怎么处理** —— ✅ 每次运行开始时登录一次。
   压测这种"一次跑几十分钟"的场景，令牌过期会表现为请求开始 401、重跑一次即可。
   **刻意不做自动续期**：refresh token 是长期凭证，把它引入脚本等于换一种形态的长期密钥，
   正好又把刚拆掉的问题装回去。
3. **一个调用方报多个租户的旧用法怎么拆** —— ⚠️ 曾经卡在这里（"必须先确认没有
   人用一把钥匙切多个租户"）。
   **2026-10-05 决策：不再等确认，直接删。** 理由是"确认无人这么做"这件事**没有
   可靠手段**：这把密钥本就不归因到人，谁也说不清有哪些调用方在用；而只要路径还在，
   就总会有人为了省事去配它 —— 于是上面三条缺陷会被原样重新引入。
   需要多租户的脚本改成**多个服务账号**（JWT 里租户是单值）。
   ⇒ `app/main.py::_authenticate` 的第 ① 分支与 `Identity.via` 的 `script-key` 档
   已整体删除（该字段随后在 OIDC 一并清理时**整体删除**，见 4.2.2）。

**配置侧**：本机 `.env` 里的 `SERVE_API_KEY` 与 `REQUIRE_AUTH` 两行**已删除**
（原值备份在 `.env.bak`，被 `.gitignore` 的 `.env.*` 忽略、不会进仓库）。
⚠️ 只是"留空"不够 —— 启动期守卫按**键是否存在**判定，所以必须整行去掉。

##### 删除清单（2026-10-05 整理并**已全部执行**）

⚠️ 必须**一次动完**，因为这些层彼此耦合 —— 只删认证分档而不删请求头参数，
会在 25 个端点上留下一个没人读的必填参数。

| 层 | 位置 | 规模 |
|----|------|------|
| ① 认证分档 | `app/main.py::_authenticate` 的第 ① 档（`hmac.compare_digest`），以及 `Identity.via` 的取值分档 | 1 处判定 + 1 组取值 |
| ② 请求头参数 | 各端点签名上的 `x_api_key: str = Header(default="")`，及传给 `_authenticate` / `_admin_ident` 的实参 | **25 个端点、59 处引用** |
| ③ 租户来源 | `_retrieval_tenant` 里 `script-key` 那一支（自报租户被采信） | 1 处 |
| ④ 运行时豁免 | `_assert_review_role` 对"非 JWT 身份"的直接放行 | 1 处 |
| ⑤ 脚本入口 | `scripts/load_test.py` / `examples/http_client.py` 的 `--api-key`；`scripts/tasks.py::_e2e_api_key` | 3 处 |
| ⑥ 配置与模板 | `app/config.py` 的 `SERVE_API_KEY`、`app/auth.py` 的注释、`.env.example` 的说明段 | 3 处 |
| ⑦ 测试 | monkeypatch `SERVE_API_KEY` 的用例（`test_identity` / `test_user_auth` / `test_secrets_config` / `test_tenant_isolation` / `test_api_v1` / `test_disconnect` / `test_idempotency`）；`test_env_example.py` 那条"该键必须为空"要改成"该键已不存在" | 约 10 条 |
| ⑧ 守卫与文档 | `tests/test_doc_drift.py` 的白名单条目、8 份 md（含 RAG 语料 `data/kb/01`，后者要配套 `kb-prune` + 重建索引） | — |

⚠️ 两处**必须与删除一起决定**的连带项（**2026-10-05 均已按"删干净"落地**）：

- `_authenticate` 会从三档变两档，`Identity.via` 的合法取值随之收窄 —— `app/auth.py`
  文件头与 `app/review_queue.py` 的 via 说明已同步（否则文档在描述一个不再存在的取值）。
  ⚠️ 该字段随后在 OIDC 一并清理时**整体删除**：取值收窄到最后只剩 `session`
  一种 ⇒ 恒为定值的字段没有信息量，见 4.2.2；
- 第 ④ 项**已删**（不再对"非 JWT 身份"放行审核）。当时留的这个问题——
  "**匿名档要不要保留审核入口**"——随匿名档一起消失了：现在进不来就没有身份，
  进得来的都是 JWT ⇒ 角色校验对**每一个**身份都必须执行，
  守卫 `test_user_auth.py::test_review_role_check_is_enforced_for_every_identity_kind` 钉住它。

### 4.2.2 外部身份源（OIDC / JWKS）：**已整体删除**（2026-10-05）

⚠️ **本节同样是为了记住"为什么删"。** 这条路径已经不存在：环境里不再有
`AUTH_OIDC_*` 任何一个键，`app/auth.py` 里也没有 JWKS 拉取、`kid` 轮换、
算法白名单、`id_token` 登录这些东西了。

删除前的状态（供核对"是否还有遗漏"）：

- 认证入口按 JWT header 的 `alg` 分流：`HS256` 走自签会话、其余走外部 IdP 验签；
- 验签需要维护一份**外部公钥**（JWKS，`jwks_uri` 拉取 + 缓存 + `kid` 轮换）；
- ⇒ 它带来的是**一份本服务无法单方面作废的凭证**：token 由 IdP 签发，本地改账号表
  对它无效，撤销只能去 IdP 那边做（这正是 4.1 里"零停机轮换唯一天然成立者"的由来，
  也是它**唯一**的好处）。

删掉它的理由不是"它不安全"，而是**它一天也没被启用过**：本项目实际的身份来源是
"自助注册 + 申请加入租户 + 管理员批准"，全是本服务自己的账号体系。留着的代价是
多一条要长期维护与测试的身份入口，以及一份"看起来能接 SSO"的误导。

**连带删除项**（与 4.2 的那次删除同类，必须一次动完）：

| 层 | 位置 |
|----|------|
| ① 验签实现 | `app/auth.py` 的 `JwtVerifier`（JWKS 拉取与缓存、`kid` 轮换、算法白名单、时钟偏移容忍）、`AuthService.verify_oidc_token` / `authenticate_bearer` |
| ② 配置与派生 | `app/config.py` 的 10 个 `AUTH_OIDC_*` 常量与 `AUTH_OIDC_CONFIGURED` |
| ③ 接口 | `LoginReq.id_token` 字段、登录端点的 `id_token` 分支 |
| ④ 启动期 | JWKS 预热 lifespan、"半配置"告警 |
| ⑤ 身份字段 | `Identity.via`（取值曾 `session`/`oidc`/`script-key`/`anonymous`，如今只剩一个 ⇒ 恒为定值无信息量，整字段删除；审核留痕 `resolved_via` **列保留**，新记录固定写 `session`） |
| ⑥ 守卫 | `AUTH_OIDC_` 前缀族纳入死开关守卫（见下） |
| ⑦ 测试与文档 | 4 个测试文件的验签用例；8 份 md 含 RAG 语料 `data/kb/01` |

**守卫（防止它被重新引入）**：

- 启动期：环境里**存在**任何 `AUTH_OIDC_` 开头的键（**留空也算**）⇒
  `app/main.py::_assert_no_legacy_auth_knobs` **拒绝启动**并打印删除原因。
  ⚠️ 用**前缀族匹配**而不是逐条列举：这一族有 10 个键，漏掉一个就等于留了一个口子，
  而"漏项"最不容易被发现（没人会去数）。
- 模板：`.env.example` 与 `.env` 都**不再声明**该族任何键，只留一段"已删除"说明。

### 4.3 `POSTGRES_PASSWORD`：特别注意"只在初始化生效"

数据卷已存在时改 `.env` 里的 `POSTGRES_PASSWORD` **没有任何效果**
（PG 只认初始化时写进 `pg_authid` 的值）。正确做法：

```bash
docker compose --env-file .env -f deploy/app/docker-compose.yml exec pgvector \
  psql -U langgraph_app -d langgraph_checkpoints \
  -c "ALTER USER langgraph_app PASSWORD '新口令'"
# 然后把 .env 里的 POSTGRES_PASSWORD 同步改掉，再重启 app 实例
```
**两处必须同时改**：只改一处时表现为"服务连不上库"，而排查的人通常会去怀疑网络。

### 4.4 `AUTH_SESSION_SECRET`：轮换 = 全体重新登录（这既是代价，也是能力）

它是本服务自签会话 token 的签名密钥（生成：`openssl rand -hex 32`）。

- **换它的效果是"所有会话立刻失效"** —— 全部用户下次请求 401、需要重新登录。
  这既是代价（低峰期做、提前通知），也是**能力**：怀疑会话被劫持时，
  "换密钥"是唯一能一次性清空所有已签发 token 的手段。
  （`SERVE_API_KEY` 没有这种"一键清场"语义，换它只是打断调用方。）
- 想做到**不打断**目前不支持：自签会话只用**一把**密钥签发与验签，token 里也不带
  "这条是用哪把密钥签的"这个信息。（原先那套 `kid` 多密钥选钥只服务于外部身份源的
  JWKS 验签，**已随 OIDC 一起删除**，自签这边本来就没有。）要重叠，得先让 token
  自报 `kid`、验签侧再按 `kid` 选密钥 —— 这是对**自签会话**这套机制的改造，
  与"给机器调用方发什么凭证"是两件独立的事，见 4.1。
- ⚠️ 它**同时也是"登出/撤销"生效的前提**：不配它 ⇒ 本服务**签不出也验不了**会话
  token ⇒ **没有任何人能登录**（`app/auth.py` 的 `can_issue` 为假时登录直接 503
  `auth_misconfigured`；启动日志会告警）。生产**必配**。
- 注意清场的**粒度**：换这个密钥是**全局**清场（所有用户、所有设备）；
  `POST /v1/auth/logout?all=true` 是**单用户**清场（清该用户的全部设备，只作用于
  当前凭证里的 user）；`POST /v1/auth/logout` 是**单设备**。日常处置优先用后者，
  不必动密钥、也不会打搅其他用户。

`AUTH_USERS` 里的口令哈希按"凭据"对待：

- 哈希串**不是明文口令**（PBKDF2-HMAC-SHA256，60 万次迭代），但仍是敏感值 ——
  拿到它可以离线爆破弱口令，所以它不该出现在日志、工单、聊天记录里；
- **增删用户 = 改这一条值**：`python -m scripts.hash_password -u alice -t acme -r reviewer`
  生成后粘贴，改完**必须重启实例**（`config.py` 在导入期读它）；
- 批量换口令逐个替换即可，不需要停机；
- ⚠️ 粘贴时**整条值必须加单引号**，否则 compose 的变量插值会把 `$<hex>` 段吃成空串，
  哈希静默报废 —— 表现为「密码明明是对的却登不进去」且无任何报错。
  本地直跑（python-dotenv 只插值 `${VAR}`）不受影响 ⇒ **本地能登进去 ≠ 容器里能登进去**。

### 4.5 轮换记录

每次轮换在当天的 `.workbuddy/memory/YYYY-MM-DD.md` 记一行：
**哪个密钥、为什么换、换完有没有确认旧值失效**。
没有记录 = 下次没人知道该不该再换。

---

## 五、怀疑泄漏时的处置顺序

**顺序很重要，先撤销再排查**（排查需要时间，而泄漏在持续）：

1. **立即撤销**（不是"先看看是不是真的"）。
   按影响面从大到小：数据库口令 → 模型密钥 → 租户密钥 → 会话密钥 → webhook。
   其中 **`AUTH_SESSION_SECRET` 优先于其它"通知类"凭据**：换掉它等于把所有在线会话
   立刻清场，是唯一能止损"会话已被劫持"的手段（见 4.4）。
   注意 webhook URL 泄漏的影响是"攻击者可以往你的群里发假告警"，
   它会**掩盖真告警**，所以也要换。
2. **检查用量是否异常**：模型密钥看 `llm_tokens_total` 的速率拐点；
   租户密钥看 `quota_exceeded_total` 与各租户的调用分布；
   数据库口令看 PG 的连接来源（`pg_stat_activity`）。
3. **确认泄漏渠道并封堵**：误提交（清历史 + 轮换，**清历史不能替代轮换**）、
   备份外流、开发机被入侵、离职交接。
4. **加固**：把这次的渠道加进 gitleaks 的检测范围或 `.gitignore`；
   如果是流程问题（比如"有人把 .env 贴进聊天记录"），改流程并写进本文档。

> ⚠️ **"把 commit 从历史里删掉"不能替代轮换。** 密钥一旦离开你的控制范围，
> 就必须假定它已经泄露 —— 历史清理只影响"别人还能不能看到"，不影响"是否已被复制"。

---

## 六、落地清单

- [x] 第 1 步的**脚手架**（2026-10-03）：`.sops.yaml`、`.gitignore` 的例外说明、
      `make secrets-encrypt` / `secrets-decrypt`、`tests/test_secrets_config.py` 守卫
- [ ] 第 1 步的**收尾（只有你能做）**：`age-keygen` 生成私钥 → 公钥填进 `.sops.yaml`
      → `make secrets-encrypt` → 提交 `secrets/app.sops.env` → **把私钥备份好**
- [ ] 给所有密钥标注"上次轮换时间"，并定一个轮换周期（建议 90 天）
- [x] 脚本侧改用 JWT 服务账号（2026-10-04 完成，见 4.2.1）；`.env` 里的共享密钥已于
      2026-10-05 整行**删除** ⇒ 本机不再有这条可用路径
- [x] **真删代码**：按 4.2.1 的"删除清单"已一次动完（2026-10-05）
- [x] 外部身份源（OIDC / JWKS）整体删除并纳入死开关守卫（2026-10-05，见 4.2.2）
- [ ] 换掉数据库口令：现在是**默认值**，且改 `.env` 不生效，必须 `ALTER USER`（见 4.3）
- [ ] `AUTH_SESSION_SECRET` 纳入轮换周期；确认"换密钥 = 全体重新登录"已被产品侧接受
- [ ] `AUTH_USERS`（种子管理员）的哈希只在 `.env` 里、从不进日志/工单
      （生成用 `python -m scripts.hash_password`）。⚠️ 注意它现在只是**种子**：
      改它不会改动已建出来的用户，那些用户的口令哈希在 PG 的 `users` 表里，
      轮换要走 `PATCH /v1/admin/users/{username}`
- [ ] 第 2 步：改走 Docker secrets，`config.py` 加"文件优先"垫片
- [ ] 部署机上限制 `.env` 的文件权限（`chmod 600`，属主为部署用户）
- [ ] 限制能执行 `docker` 命令的人员范围（这等价于"能读全部密钥"）
- [ ] 复盘一次"如果今天 `.env` 泄漏，我需要多久才能全部换完"——
      这个时间就是你的暴露窗口，它通常比你以为的长
