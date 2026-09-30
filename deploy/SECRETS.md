# 密钥管理（现状、风险与迁移路径）

> 本文记录密钥的**现状**、**为什么它是个真问题**、以及**分几步能改好**。
> 刻意不写成"最佳实践清单"：那种清单落地率极低，因为它不告诉你**第一步改什么**。

---

## 一、现状

所有密钥都在项目根的 `.env` 里，以**明文**形式存在，通过三种方式进入系统：

| 用途 | 传递方式 | 谁在用 |
|---|---|---|
| 应用密钥（`AGNES_API_KEY`、`EMBEDDING_API_KEY`、`SERVE_API_KEY`、`TENANT_KEYS`） | compose 的 `env_file: ../../.env` → 容器环境变量 | `app/config.py` 读取 |
| 基础设施口令（`POSTGRES_PASSWORD`、`REDIS_PASSWORD`、`GRAFANA_PASSWORD`） | compose 的 `${VAR}` 插值（从项目根 `.env`） | compose 自己，用于拼 `DB_URL` / 容器参数 |
| 通知凭据（`ALERT_SINK_*_WEBHOOK`） | 同上 → `alert-sink` 容器环境变量 | `deploy/monitoring/alert_sink.py` |

`.env` 与 `.env.*` 已被 `.gitignore` 忽略（`.env.example` 是唯一的例外，里面只有
刻意的开发默认值）。CI 的 `security` job 用 gitleaks 扫当前工作树，
配置见仓库根 `.gitleaks.toml`。

### 已经做对的部分

- `.env` 不进版本库；`.env.example` 只放占位值；
- 密钥在启动日志里**只回显"有没有配"**，不回显内容（`app/config.py` 的 `as_dict()`：
  `tenant_keys` 只回显**条数**）；
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

### 2.2 没有轮换流程，也没有轮换能力

- 没有任何地方记录"这个密钥上次什么时候换的"；
- `SERVE_API_KEY` 是**单值**：换它意味着旧 key 立刻失效 ⇒ 所有调用方同时中断。
  也就是说**当前架构下无法做零停机轮换**（`TENANT_KEYS` 是 map，可以临时加新条目
  做重叠，但 `SERVE_API_KEY` 不行）。
- `POSTGRES_PASSWORD` 更麻烦：它**只在数据卷首次初始化时生效**
  （见 `deploy/app/docker-compose.yml` 顶部说明），换它需要 `ALTER USER` 或删卷。

### 2.3 泄漏的影响面是"全量"

一份 `.env` 同时包含数据库口令、模型密钥、租户密钥、通知 webhook。
任何一处泄漏（误提交、备份文件外流、离职交接遗漏、被入侵的开发机）都等于**全部**。

---

## 三、迁移路径（三步，按成本递增）

### 第 1 步（建议尽快）：用 SOPS + age 把 `.env` 变成加密文件

**改什么**：新增 `secrets/.env.enc`（SOPS 加密），部署时解密到 `.env`（仍被 gitignore）。

```bash
# 初始化（每台机器只需一次，age 私钥放 ~/.config/sops/age/keys.txt，绝不进仓库）
age-keygen -o ~/.config/sops/age/keys.txt

# secrets/.sops.yaml
# creation_rules:
#   - path_regex: secrets/.*\.env$
#     age: age1你团队的公钥

sops -e .env > secrets/.env.enc        # 加密入库
sops -d secrets/.env.enc > .env        # 部署时解密
```

**收益**：仓库里不再有明文密钥；谁能解密由 age 私钥决定（可审计、可分发）。
**成本**：低（一个 `sops` 二进制 + 一把 age 密钥），**不改任何应用代码**。
**这一步解决 2.3 的"误提交"与"仓库外流"**，不解决 2.1。

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

### 4.1 零停机轮换的正确顺序（先加新的，再删旧的）

以 `TENANT_KEYS` 为例（它天然支持重叠）：

```bash
# ① 保留旧 key，追加新 key（两个都能用）
TENANT_KEYS=旧key:acme,新key:acme

# ② 通知调用方切成新 key，观察旧 key 的用量是否降到 0
#    可用指标确认：http_requests_total 按 X-Tenant-Id 或日志里的 tenant 字段

# ③ 确认无流量后移除旧 key
TENANT_KEYS=新key:acme
```

**顺序反了（先删后加）的后果**：调用方全部 401，而且看起来像"服务坏了"，
排查方向会先跑到鉴权代码上。

### 4.2 `SERVE_API_KEY`：目前做不到零停机

它是单值，没有重叠窗口。两种选择，都请明确选一个而不是默认硬切：

- **接受短暂中断**：选低峰期，改 `SERVE_API_KEY` → 重启 → 通知调用方。
  适用于内部/少数调用方。
- **改成列表**（需要改代码）：让 `SERVE_API_KEY` 支持逗号分隔的多值
  （与 `TENANT_KEYS` 同一个思路），这样才能重叠轮换。
  这是后续值得做的一项加固。

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

### 4.4 轮换记录

每次轮换在当天的 `.workbuddy/memory/YYYY-MM-DD.md` 记一行：
**哪个密钥、为什么换、换完有没有确认旧值失效**。
没有记录 = 下次没人知道该不该再换。

---

## 五、怀疑泄漏时的处置顺序

**顺序很重要，先撤销再排查**（排查需要时间，而泄漏在持续）：

1. **立即撤销**（不是"先看看是不是真的"）。
   按影响面从大到小：数据库口令 → 模型密钥 → 租户密钥 → webhook。
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

- [ ] 第 1 步：SOPS + age，`.env` 加密入库（**当前唯一待办中的最高优先级**）
- [ ] 给所有密钥标注"上次轮换时间"，并定一个轮换周期（建议 90 天）
- [ ] `SERVE_API_KEY` 支持多值以支持零停机轮换（需改代码）
- [ ] 第 2 步：改走 Docker secrets，`config.py` 加"文件优先"垫片
- [ ] 部署机上限制 `.env` 的文件权限（`chmod 600`，属主为部署用户）
- [ ] 限制能执行 `docker` 命令的人员范围（这等价于"能读全部密钥"）
- [ ] 复盘一次"如果今天 `.env` 泄漏，我需要多久才能全部换完"——
      这个时间就是你的暴露窗口，它通常比你以为的长
