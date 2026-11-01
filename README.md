# chatAgent

基于 **OpenAI Agents SDK** 的内容生成 Agent：入口 agent 自己决定「直接交付 / 叫帮手 /
转人工审核」，并提供 FastAPI 流式服务（SSE）与基于 TF-IDF 的轻量 RAG。

> 编排原语只有 `Agent`（instructions + tools + handoffs + guardrails）和 `Runner`
> 主循环 —— **没有图、没有节点、没有边**。一轮请求 = 一次 `Runner.run`（见
> `src/turn.py`）；需要人做决定时运行停在官方 `RunState` 上，由 `app/runs.py` 落库。

## 目录结构

```
chatAgent/
├─ src/                  生产代码（唯一真源）
│  ├─ turn.py            **一轮请求 = 一次 `Runner.run`**（入口在这里，没有编排图）
│  ├─ agents/            agent 装配：入口 / 专家 / 工具 / 护栏 / 审批 / 历史压缩
│  │  ├─ runner.py           `Runner` 的调用与结果整理（`TaskOutcome`）
│  │  ├─ frontline.py        入口 agent（判定 reply / transfer）+ 交付工具
│  │  ├─ specialists.py      专家 agent（检索、文学创作…）
│  │  ├─ tools.py            工具：检索 / 交付 / 落盘，以及时间 / 算术 / 资产回读
│  │  ├─ approval.py         工具审批：`deliver(needs_review=True)` 停在交付前
│  │  ├─ compaction.py       跨轮历史压缩
│  │  └─ session.py          官方 `Session` 的 PG 后端（跨轮历史唯一载体）
│  └─ rag/               RAG 数据链路（解析 → 切分 → 向量化 → 检索 → 引用）
│     ├─ retrieval.py        RAG 检索库：语料装载 + TF-IDF / embedding / pgvector + 引用回溯
│     ├─ loaders.py          多格式解析：txt/md/csv/tsv/json/jsonl/html/docx/xlsx/pptx
│     ├─ knowledge_chunk.py  切分核心：DoclingDocument / Block → KnowledgeChunk
│     ├─ docling_loader.py   docling 封装（PDF / docx 解析）
│     ├─ docx_media.py       docx 媒体：图片 / OLE / 原生图表
│     ├─ pdf_parser.py       PDF 解析（收编自教程）：pdfminer 提文本 + 段落重组 + 交叠切块
│     ├─ image_describer.py  VLM 图片描述
│     ├─ contextualizer.py   Contextual Retrieval（块级上下文前缀）
│     ├─ vector_store.py     pgvector 存储层
│     └─ chunk_writer.py     chunk 兼容写出
├─ app/                  服务层
│  ├─ main.py            FastAPI 入口：/v1/chat /v1/review /v1/reviews /health/* /metrics
│  ├─ serve.py           启动入口 + 事件循环工厂（`python -m app.serve`）
│  ├─ config.py          服务层配置集中处（其余模块不再各写 os.getenv）
│  ├─ migrations.py      DB 结构版本管理 + doctor（CLI：--status / --apply）
│  ├─ health.py          存活/就绪探针
│  ├─ ratelimit.py       限流（内存 / Redis）
│  ├─ quota.py           token 预算与日配额
│  ├─ review_queue.py    人工审核队列
│  ├─ sessions.py        会话归属登记（防猜 thread_id 串读）
│  ├─ metrics.py         Prometheus 指标
│  ├─ middleware.py      请求上下文（request_id / 访问日志）
│  ├─ logging_setup.py   结构化日志
│  └─ tracing.py         Langfuse / LangSmith 接入
├─ deploy/               部署产物（Dockerfile / compose / nginx / Prometheus 告警 + SLO.md）
├─ scripts/              运维与评测脚本（备份演练、压测、检索评测、OpenAPI 快照）
├─ tests/                单元 + 整图 + 接口 + 检索/解析测试
│  └─ snapshots/         OpenAPI 契约快照（对外接口的承诺，见「接口契约」）
├─ data/                 知识库语料（多格式）
├─ notebooks/            原型笔记本（探索用，逻辑以 src/ 为准）
├─ examples/             示例：`http_client.py` 是接入参考实现，其余是教程原型
├─ requirements.txt
├─ PRODUCTION_CONFIG.md  上线前需要你填的配置清单（对着它勾）
└─ .env / .env.example
```

## 支持的语料格式

把文件丢进 `DOCS_DIR`（默认 `data/`）即可，`load_chunks` 会按扩展名自动分派：

| 类别 | 扩展名 | 解析方式 |
|------|--------|----------|
| 纯文本 | `.txt` `.md` `.markdown` `.log` `.rst` `.text` | 直接读，**自动兜底 GBK/GB18030 编码** |
| 表格 | `.csv` `.tsv` | 表头 + 每行转成 `列=值 \| 列=值` |
| 数据 | `.json` `.jsonl` `.ndjson` | 嵌套结构扁平化成 `key: value` 行 |
| 网页 | `.html` `.htm` | 去脚本/样式，标题转成 Markdown `#` |
| Word | `.docx` | 标题→`#`、段落、表格保留行列 |
| Excel | `.xlsx` `.xlsm` | 每个工作表一个块 |
| PPT | `.pptx` | 每页一个块 |
| PDF | `.pdf` | pdfminer 提文本 + 段落重组 + 句子级交叠切块 |

配置文件类（`.yml` `.yaml` `.toml` `.ini` `.conf` `.sql` 等）按纯文本处理。
**不支持的扩展名会被跳过并打一条 WARNING**（不会把二进制文件当文本读进来）。

## 快速开始

```bash
# 1) 准备配置
cp .env.example .env      # 填入 AGNES_API_KEY 等

# 2) 安装依赖
pip install -r requirements.txt

# 3) 起服务
python -m app.serve                      # 推荐：自动适配事件循环（见下方说明）
# 等价的手写形式：
# uvicorn app.main:app --loop app.serve:loop_factory

# 4) 跑测试
pytest tests/ -q
```

> **为什么用 `python -m app.serve` 而不是裸 `uvicorn`**：它统一处理 `APP_HOST` /
> `APP_PORT` / `APP_WORKERS`（兼容平台注入的 `WEB_CONCURRENCY`）与优雅关闭排空上界
> `GRACEFUL_SHUTDOWN_TIMEOUT`，这几项裸命令都要自己敲对。
>
> `.env` 由**启动入口自己在导入期加载**，所以第 1 步写完就不用再 `export` 一遍；
> `APP_HOST` / `APP_PORT` 这些也可以直接写在 `.env` 里。这条以前不成立（那时
> `.env` 只在 `app/main.py` 导入时加载，而入口解析排空上界**早于**它 ⇒ 只配
> `.env` 必然报 `missing_api_key` 拒绝启动），2026-10-30 已修，由
> `tests/test_serve_env_bootstrap.py` 用子进程守着。
>
> ⚠️ **历史说明（2026-10-20 更新）**：这个入口原本还有一个理由 —— 检查点用的是
> `AsyncPostgresSaver`（psycopg 异步），而 psycopg 异步**不支持 Windows 的
> ProactorEventLoop**，所以要注入自适应的事件循环工厂。**异步检查点已经随图一起退役**：
> 现在所有 PG 访问都是同步 psycopg + 工作线程（见 `app/pg.py` 的「为什么是同步池」），
> 因此那条约束不再成立，`app/serve.py` 里的 `loop_factory` 只剩兼容作用。
>
> 入口是 `app.main:app`（旧命令 `uvicorn serve:app` 已随文件迁移失效）。
>
> 判断「运行现场有没有真的落库」的唯一可靠信号：`/health/ready` 的 `run_state` 项。
> 显示 `memory（进程内存，重启丢现场！）` 就是没接上。

## 常用命令

任务入口是 `scripts/tasks.py`（纯标准库、跨平台），`Makefile` 只是它的一层薄转发
（给 CI / Linux / WSL 用）。**为什么不是只用 Makefile**：`make` 在 Windows 上默认
不存在，而本项目主力开发环境就是 Windows —— 只写 Makefile 等于让人靠记忆敲长命令，
而"记不住就跳过"正是所有运维动作衰减的起点（RUNBOOK 里那句"每季度演练一次"
就是这么失效的）。

下面用 `tasks` 代指 `python scripts/tasks.py`：

| 目的 | 命令 |
|------|------|
| 看全部任务 | `tasks help` |
| 跑测试 / 只重跑上次失败的 | `tasks test` / `tasks test-fast` |
| **提交前门禁** | `tasks check` |
| **热路径微基准** | `tasks bench`（`tasks bench -- --check` 走 CI 门禁口径；`-- --baseline <文件>` 与本机留档对比） |
| 起本地栈 / 含监控 / 停 | `tasks up` / `tasks up-observability` / `tasks down` |
| **端到端自检**（真起栈） | `tasks e2e`（SSE 分帧 / 灰度权重 / 运行现场落库；`--keep` 保留现场） |
| 看服务状态 / 跟日志 | `tasks ps` / `tasks logs`（`tasks logs -- app-stable` 只看一个） |
| 备份 / **恢复演练** | `tasks backup-pg` / `tasks drill` |
| 灰度发布 / 回滚 | `tasks release -- <镜像tag> --dry-run` / `tasks rollback -- <镜像tag>` |
| 看灰度判据 | `tasks canary`（退出码即结论：0 推进 / 1 回滚 / 2 数据不足转人工） |
| 改版本号 | 改 `VERSION` → `tasks sync-version` → 在 `CHANGELOG.md` 补一条 |
| 刷新契约快照 / 依赖锁 | `tasks openapi` / `tasks lock` |
| 清构建缓存 | `tasks clean`（默认只列出，加 `--yes` 才删） |

参数透传用 `--`：`tasks test -- -k version`。有 `make` 的环境里等价写法是
`make check`、`make up`、`make drill`…，参数走 `ARGS`：`make test ARGS="-- -k version"`。

`tasks check` 与 CI 的 `test` job **等价**（版本号副本一致 → 依赖锁未漂移 → 全量测试）。
两边一旦漂移 —— 尤其是 CI 加了步骤而这里没跟上，导致"本地全绿、推上去才红" ——
`tests/test_tasks_entrypoint.py` 会红。

## 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/chat` | SSE 流式对话。事件：`token` 增量文本、`citations` 引用、`need_review` 需人工审核、`budget_exceeded` 超 token 预算、`timeout` 超请求总时限（终止性，其后无 `done`）、`stopped` 被用户停止（终止性，其后无 `done`）、`done` 结束、`error` 异常 |
| POST | `/v1/chat/resume` | **续跑停在审批上的运行**（SSE，事件同 `/v1/chat`）。body：`{thread_id}`。⚠️ 当前**恒回 409**：官方只在「产生 interruption」时给可恢复现场，而本项目唯一的中断点就是交付审批 ⇒ 那就该走 `/v1/review`；断流 / 超时那一类没有现场（官方明确写明留给应用自己），如实回 409 并指路「重发消息」 |
| GET | `/v1/sessions/{thread_id}` | **会话状态快照**。纯读、幂等，返回 `status` / `resumable` / `draft` / `messages` / `citations` / `pending_review` |
| POST | `/v1/sessions/{thread_id}/messages` | **任务执行中补一句话**（不触发运行、不占配额、无 SSE）。body：`{message}`。生效时机：本次运行的下一次模型调用 → 下一个 agent 节点 → 下一轮。支持 `Idempotency-Key`（重试不会写两遍）。会话后端不可用时 503（那份补充存不下来） |
| POST | `/v1/sessions/{thread_id}/stop` | **停掉正在跑的那一次运行**。停止后 SSE 收到终止性的 `stopped` 事件（其后无 `done`），已产出正文保留。⚠️ 停止是**取消**，官方不会为取消产出可恢复现场 ⇒ `stopped` 里的 `resumable` 恒为 false。当前没有在跑的任务返回 409；重复调用幂等 |
| POST | `/v1/review` | 人工审核 resume。body：`{thread_id, action: "accept"\|"revise", feedback}`。非审核态返回 409 |
| GET | `/v1/reviews` | 待人工审核队列（分页列举，含草稿摘要与评分） |
| GET | `/health` | 兼容旧路径，等价于 `/health/live`，恒返回 `{"status":"ok"}` |
| GET | `/health/live` | 存活探针：只反映本进程，不查外部依赖 |
| GET | `/health/ready` | 就绪探针：真探 LLM 配置 / 数据库 / 向量扩展 / 运行现场存储（`run_state`）；不健康回 503 |
| GET | `/metrics` | Prometheus 文本指标（`METRICS_ENABLED=0` 时返回 404） |

`/chat`、`/review` 保留为 `/v1/*` 的兼容别名（标记 deprecated，功能一致）。
`/v1/chat/resume` 与 `/v1/sessions/*` 是新增能力，**没有**旧路径别名。

`/chat` 请求体：`{"thread_id": "会话ID", "message": "用户输入"}`

### 接口契约（快照门禁）

对外接口的承诺固化在 `tests/snapshots/openapi.json`，`tests/test_openapi_contract.py`
每次跑测试都会重新生成一份并逐字节比对 —— **任何契约变化（删字段、改参数位置、
改枚举取值、给响应包一层）都会以 diff 形式出现在 CI 里**，而不是等调用方报错。

```bash
python scripts/snapshot_openapi.py            # 校验（CI 同款）
python scripts/snapshot_openapi.py --write    # 确认变更后重新生成
```

⚠️ 测红了**先看 diff 再决定要不要 `--write`**。而且重新生成快照**不会**让下面
这几份手写断言通过 —— 它们就是专门用来防"顺手洗白"的：

- 对外路由清单（新增/删除端点必须有人显式确认）；
- 旧路径 `/chat`、`/review` 必须带 `deprecated` 标记（否则客户端收不到迁移信号）；
- 需要鉴权的路由必须声明 `Authorization` / `X-Tenant-Id` 两个头，且**不得**声明
  `X-API-Key`（共享密钥已删除，留着会让接入方以为它还是凭证）；
- 模型里不得出现 `maxLength` 这类"从配置冻进来的"约束（字段闸门在**调用时**读
  config，冻进模型会让限制值变成对外契约的一部分且无法被测试改）。

### 提示词版本（可追溯）

提示词是模型行为的一部分，因此**有版本号**。三个模板集中在 `src/prompts.py`
（首轮作答 / 修订指令 / 评审指令），版本常量是 `PROMPT_VERSION`。

它出现在三处，回答的是同一个问题——「这条回答是哪版提示词产生的」：

| 位置 | 形态 | 用途 |
|---|---|---|
| 响应头 | `X-Prompt-Version: 1.0.0` | 一条回答 = 一个响应 = 一个版本号；客户端、网关访问日志可直接记录 |
| `/health/ready` | `prompt_version` 字段 | 线上核对（与代码版本 `version` 并列） |
| 启动日志 | `提示词版本：1.0.0` | 部署后第一眼确认 |

**改模板文本必须 bump `PROMPT_VERSION`**：`tests/test_prompts.py` 里存着
「版本号 → 三个模板渲染后的 sha256」映射，只改文本不改版本号会直接测红。
该测试同时断言提示词**没有回流**到 `src/` 的代码里（只允许出现在 `src/prompts.py`）。

### 断点续传

SSE 一断（弱网、切后台、代理超时），客户端手里往往只剩「已经打到屏幕上的那半句」。
但先把一件事说清楚：**可恢复的现场不是客户端断流时产生的**。

| 情形 | 有没有可恢复的现场 | 客户端该怎么办 |
|---|---|---|
| 模型在交付那一刻声明要人审（`needs_review`） | **有** —— 运行真的停在官方 `RunState` 上，由 `app/runs.py` 落库 | 调 `/v1/review`（要的是**人的决定**，不是 resume） |
| 客户端断流 / 请求超时 / 用户点「停止」 | **没有** —— 那是取消，官方只对 interruption 产现场 | 重发消息；带同一个 `Idempotency-Key` 会重放上次结果 |
| 正常跑完 | 没有（也不需要） | 直接渲染 `draft` / `messages` |

这一层的失效方式是**静默**的：假设"断流后还能接着跑"，接口会返回 200，客户端以为
续上了，实际上是从头又跑一遍 —— 已经发出去的 token 重发一遍、已经落盘的资产再做一次。

> 官方对「崩溃恢复与副作用对账」是**明确不管**的：`human_in_the_loop.md` 把这一层
> 指给 Dapr / Temporal / Restate / DBOS 这类耐久执行引擎。本项目不引第二个编排框架，
> 所以**不假装能续** —— 没有现场时 `/v1/chat/resume` 如实回 409 并指路重发消息。

```text
① SSE 断了
   └─> GET /v1/sessions/{thread_id}
         status=awaiting_review → POST /v1/review     （需要人的决定）
         status=completed       → 取 draft/messages 渲染即可
         status=unknown         → 从未开始，或已被清理
```

`status` 只有三种，判据是「有没有现场 / 有没有待审记录 / 有没有历史」：

| 判据 | status | 客户端该做什么 |
|---|---|---|
| `run_store` 里有现场，或审核队列里有 `pending` 记录 | `awaiting_review` | 弹审核界面，调 `/v1/review`；`pending_review` 里有草稿 |
| 该会话有历史条目（官方 `Session`） | `completed` | 直接渲染 `draft` / `messages` |
| 两者都没有 | `unknown` | 当新会话处理 |

> `resumable` **只在真有现场时为 true**。有历史 ≠ 能接着跑：历史只证明这一轮收过尾。
> 把这两件事混成一个字段，客户端就会对着一条已完成的会话反复调 resume。


> 快照端点是**只读**的：它只查会话属主、**不写**登记表。否则"先读一次"就能把别人
> 还没开始的 `thread_id` 占为己有。多租户下查不到属主一律 404（fail-closed），
> 避免它变成"某个会话是否存在"的枚举探针。

### 请求体积上限（两层，别只做一层）

| 层 | 位置 | 超限时 | 防的是什么 |
|---|---|---|---|
| 字节闸门 | ASGI 中间件 | **413** + 明确上限值 | 用几百 MB 的请求把进程内存打爆（不需要任何漏洞，一个 curl 就够） |
| 字段闸门 | Pydantic 校验器 | **422** + 点名哪个字段超了多少 | 合法体积内的失控输入，给客户端可直接照做的错误信息 |

默认 1 MiB / 10 万字符（`message`）、2 万字符（`feedback`）、128 字符（`thread_id`），
见 `MAX_REQUEST_BYTES` / `MAX_MESSAGE_CHARS` / `MAX_FEEDBACK_CHARS` / `MAX_THREAD_ID_CHARS`。

> `/v1/chat/resume` 等路径**不能只靠 nginx 的 `client_max_body_size`**：内部直连、
> 未前置网关的部署形态、压测都是绕过它的。而且仅校验 `Content-Length` 也能被
> `Transfer-Encoding: chunked` 绕过 —— 所以中间件是"声明超限直接拒 + 边读边计量"两层。

> `/v1/sessions/*` 同时受按 IP 与按会话限流约束（防枚举探测），但**不查配额、不认领会话** ——
> 看状态不花 token，也不该改变归属。

### 断连止损（别再自己轮询 `is_disconnected()`）

客户端拔线后必须**立刻**停掉生成，否则模型照跑到自然结束、token 照付。机制：

```
客户端断开
  → ASGI receive() 收到 http.disconnect
  → sse_starlette 的 _listen_for_disconnect（常驻任务）取走它
  → 回调 EventSourceResponse(client_close_handler_callable=...)
       ├─ 计数 client_disconnected_total{kind=chat|review|resume} + 记 WARNING 日志
       └─ 置位信号 → _watch_disconnect 任务 aclose 上游
  → 库 cancel 整个 task group，SSE 响应收尾
```

> ⚠️ **不要用 `request.is_disconnected()` 轮询**（本项目 2026-09-29 前的写法，已废弃）。
> `receive()` 是**单消费者**通道：sse_starlette 的监听任务在响应开始时就阻塞在那儿，
> 轮询永远晚一步；而且库一拿到 `http.disconnect` 就立刻取消整个 task group，
> 轮询连下一次 `sleep` 都等不到 ⇒ 表现为"生成确实停了，但指标恒为 0、日志一行没有"，
> 也就是**止损靠的是库的取消，我们自己的观测是死代码**。更糟的是万一轮询先抢到
> 那条消息，库的监听任务会永久阻塞，连接彻底挂死。
>
> 因此：回调**必须 async**（库是 `await handler(message)`）、**不得 await 耗时操作**
> （它跑在库的取消路径上）、**不得在里面 `aclose()` 上游生成器**（那个生成器正被
> 另一个任务驱动，并发调用会撞上 asyncio 的 "already running" 保护）。
> 关流交给生成器自己的 `finally` —— 取消会带着 `CancelledError` 打进它的挂起点。
> 接线统一收在 `app/main.py` 的 `_sse()` 里，三个端点共用，避免漏接。

**为什么不能用 `TestClient` 测这条路**：它把请求-响应当成一次性完整往返，
客户端"提前关掉"时**不会**下发 `http.disconnect`，这条路永远走不到。
端到端覆盖在 `tests/test_disconnect.py`（自己构造 ASGI `receive` 通道投递断连）。

### 鉴权与身份（人对服务 / 机器对服务）

**只有一种身份来源：本服务签发的会话令牌。** 人类用户与服务账号都走同一条路 ——
账号密码经 `POST /v1/auth/login` 换 `Bearer` 令牌，用户身份与租户身份都在令牌载荷里：
`sub`（用户名）、`tenant`（租户名）、`roles`。服务账号与人类用户共用 `users` 表
（命名习惯加 `svc-` 前缀），这样脚本身份可归因（留痕里有 `user`）、可单独撤权、
租户来自令牌载荷而**不可自报**。

⚠️ **2026-10-05：共享密钥 `SERVE_API_KEY`（`X-API-Key`）、"匿名放行"开关
（`REQUIRE_AUTH`），以及外部身份源（OIDC / JWKS）都已随代码路径一起删除。**
不是"废弃、留空即可"，而是**环境里存在它们（留空也算）会让服务拒绝启动**
（守卫 `app/main.py::_assert_no_legacy_auth_knobs`，OIDC 那一族按 `AUTH_OIDC_`
**前缀**匹配）。删而不是留空的理由见 `deploy/SECRETS.md` §4.2：共享密钥那三条缺陷
（分不出身份 / 撤不掉 / 租户只能自报）都是**结构性**的，只要留着一行配置，就总会有
人为了省事去配它；OIDC 则是**从未启用过**的第二条身份入口。

```
Authorization: Bearer <jwt>   ──► 用户主体(sub) + 租户 + 角色 + 会话，可撤销、可审计
                                （人类与服务账号**都走这条**，也是**唯一**一条）
（不带凭证）                   ──► 401（没有任何"匿名放行"或"凭共享密钥放行"的兜底）
```

身份只有一种：`Authorization: Bearer`（本服务签发的会话令牌），租户一律取**载荷里的
`tenant`**，没有租户就是空串。⚠️ 代码里**没有** `via` 这类"认证方式"标记：它的取值
曾经是 session / oidc / 共享密钥 / 匿名，如今只剩一种存在可能 —— 恒为定值的字段会被
误读成"还有别的认证方式"，所以随 OIDC 一起删掉了（见 `CHANGELOG.md` 的移除条目）。

`X-Tenant-Id` **只是"确认"**：带上它时与令牌里的租户不符即 403；不带也照常工作。
**它永远不是租户来源** —— 租户只能来自已验签的载荷。会话归属由 `app/sessions.py` 登记属主。

⚠️ **无租户 ≠ 能看全库。** 口径 2026-10-05 反转：无租户只能读到**公共租户**
（`RAG_TENANT`，本项目用 `public`）的文档；**没配公共租户则什么都检索不到**
（检索层落到 `NO_TENANT_SENTINEL` 哨兵值）。旧口径（空串 = 不加过滤 = 能召回
全部租户的文档）恰好相反，直接后果是"自助注册的账号能读全库"，作为生产默认站不住。

> **为什么共享密钥必须删掉而不是留着**：它的租户由**请求头自报且被采信** ——
> 任何持证者改个 header 就是"另一个租户"：日配额重新计算、审核队列归属失真、
> 配合可猜的 `thread_id` 还能串读别人的会话。用户鉴权把租户放进**已验签的 JWT**，
> 客户端只能"带着"、不能"声明"。

⚠️ **生产环境必须启用用户鉴权**：`APP_ENV=production` 且没有 `AUTH_SESSION_SECRET`
与可登录的账号时，服务**拒绝启动**。⚠️ **没有任何环境变量能关掉这条检查** ——
曾经能"绕过鉴权要求"的两个变量（`SERVE_API_KEY` / `REQUIRE_AUTH`）都已经不在代码里了。

`APP_ENV=production` 推导出的收紧默认是 `ENABLE_DOCS=0`（`/docs` `/redoc`
`/openapi.json` 全 404）。⚠️ 鉴权要求**不由它推导**：无论什么环境，"没有凭证 ⇒ 401"
都是结构性的。

### 用户鉴权（账号与会话）

只有 JWT 一种口径。**管理接口与审稿接口都只认 JWT**，而且现在**所有接口都是这样** ——
历史上审稿接口对"非 JWT 身份"有一处刻意豁免（为了让持共享密钥的旧调用方不被 403），
那条路径删除后豁免也随之消失。

| 场景 | 配置 | 效果 |
|------|------|------|
| 标准自部署 | `AUTH_SESSION_SECRET`（首启用 `AUTH_USERS` 种子建出第一个管理员） | 本服务账号表（PBKDF2，零额外依赖）+ 可撤销、可清场的会话 |

**多设备**：同一用户可以在多台设备同时登录，每一台是一个独立会话。
`POST /v1/auth/logout` 只登出当前那一台；加 `?all=true` 清空该用户**全部**设备
（作用域只到当前凭证里的 user，不是"任意踢人"接口）。

认证端点：`POST /v1/auth/login`（账号密码换会话令牌）、
`POST /v1/auth/refresh`、`POST /v1/auth/logout`、`GET /v1/auth/me`。
login / refresh **不走** `_preflight`（此时还没有会话，否则死锁），只过限流。

> **为什么必须引入服务端状态**
> 因为**撤销**。无状态 JWT 在任何地方都有效，服务端**无法单方面作废**它 —— 用户点了
> 登出、密码泄漏、员工离职，服务端都没有手段让已签发的 token 立刻失效。所以拆职责：
> **本服务既回答"你是谁"，也回答"你现在还能不能用"**。后者落在一张**会话白名单**上，
> 每个会话一个键 `auth:sess:{user}:{sid}`：登录新增一个键、登出删掉自己的那个键、
> 校验时判断"这个 sid 还在不在"。
> 为什么是白名单而不是"已作废 jti"的黑名单：黑名单要保存所有历史 token 直到它们过期，
> 是**无界增长**的；白名单只存当前有效的会话，条数 = 用户数 × 在线设备数，且有 TTL 回收。
> 两条配套约束：① 白名单键的 TTL 取 `max(access, refresh)`；② 刷新**必须沿用同一个 sid**
> —— 每次刷新都新建会话的话，条数就变成"刷新次数"而不是"登录次数"，又变成无界增长。

⚠️ **多 worker / 多容器部署必须配 `REDIS_URL`**。会话校验是**无条件**查存储的 ——
副本 A 签发的会话在副本 B 查不到，用户会按请求落在哪个副本**随机收到 401**
（表现为"刚登录成功、隔几次请求又要重登"）。检查启动日志里的
`auth.user_auth.session_backend` 应显示 `redis`；否则启动期会点名告警。

**本地用户表的哈希怎么来**（不要手写）：

```bash
python -m scripts.hash_password -u alice -t acme -r reviewer,admin   # 输出可直接粘贴的条目
python -m scripts.hash_password --check alice                        # 排障：拿当前配置验一次密码
```

⚠️ **`.env` 里 `AUTH_USERS` 的值必须加单引号**：PBKDF2 串含 `$`，而 compose 对**未加引号**的
值会做变量插值，`$<hex>` 段被当成变量名替换成**空串** ⇒ 哈希报废 ⇒
「密码明明是对的却登不进去」，且全程没有报错。脚本输出的就是带引号的成品，照抄即可。
反过来说：**本地直跑能登进去，不代表容器里能登进去**（python-dotenv 只插值 `${VAR}`，
不受影响）—— 加单引号两边都对，没有理由不加。

用户鉴权相关变量：

| 变量 | 必填 | 说明 |
|------|------|------|
| `AUTH_USERS` | 首启种子 | `user:hash:tenant[:role\|role][,...]`。**只在账号表为空时用来建出第一个管理员**（解鸡生蛋），之后不再被读 —— 用户的增删改查走 `app/accounts.py` 与下面的管理接口。⚠️ 值要加**单引号**（见上文） |
| `AUTH_ADMIN_ROLES` | 否 | 允许管理用户与租户的角色，默认 `admin`。⚠️ **fail-closed**：留空就等于**没人能管**（管理接口一律 403）。✅ 2026-10-05 起 `AUTH_REVIEWER_ROLES` 也统一为 fail-closed（默认 `reviewer`），两者不再刻意相反 —— 要放开必须**显式写空** |
| `AUTH_ALLOW_SELF_REGISTER` | 否 | 是否开放 `POST /v1/auth/register`，默认 `false`（**关闭**）。开启后任何人都能建出**无租户**账号 |
| `AUTH_SESSION_SECRET` | 建议必填 | 会话签名密钥，`openssl rand -hex 32`。**不配 ⇒ 无法签发/续期本服务会话 ⇒ 登出/撤销能力整体不可用** |
| `AUTH_ACCESS_TTL` / `AUTH_REFRESH_TTL` | 否 | 秒，默认 `900` / `604800`。会话键的 TTL 取两者**最大值**（只取 access TTL 会让"闲一会儿回来要重新登录"） |
| `REDIS_URL` | 多副本必填 | 会话白名单的存放位置。**不配 = 落在进程内存**：单进程可正常工作，但一旦多 worker / 多容器，副本 A 签发的会话在副本 B 查不到 ⇒ **随机 401**。启动自检会点名（见下） |
| ~~`AUTH_SINGLE_SESSION`~~ | — | **已于 2026-10-01 移除**：现在**允许同一用户在多台设备同时登录**。登出只影响发起请求的那一台，`POST /v1/auth/logout?all=true` 可清场。`.env` 里若残留该变量会被**静默忽略** |
| `AUTH_REVIEWER_ROLES` | 否 | 允许审稿的角色，默认 `reviewer`（⚠️ **2026-10-05 改**：此前默认空 = 不限制）。**不配 = 只有带 `reviewer` 角色的人能审**；要"不做角色校验"必须**显式写空** `AUTH_REVIEWER_ROLES=`。**只对 JWT 生效**：共享密钥路径没有角色概念，该豁免随 `SERVE_API_KEY` 一起退役 |

> ~~JWKS / OIDC 相关的变量已随外部身份源一起删除~~（2026-10-05）。
> 它们历史上解决的是"JWKS 拉不到就拒绝（fail-closed）、算法白名单在取公钥之前校验、
> kid 未命中先刷缓存"这几件事 —— 属于已删除功能的设计说明，保留在
> `deploy/SECRETS.md` 与 `CHANGELOG.md` 里备查。

### 账号体系（用户 / 租户 / 加入申请）

用户与租户存在库里（`app/accounts.py`；有 `DB_URL` 走 PG、否则退回内存 —— 内存会让
"在 A 副本建的用户，B 副本说密码错"，启动自检会点名）。关系是**多对一**：`users.tenant`
是单列，**一个用户只能属于一个租户**，无租户即空串。

**谁管谁**：管理员角色（`AUTH_ADMIN_ROLES`）能对用户与租户做增删改查；普通用户只能改
自己的密码、递交加入申请。管理接口**只认 JWT**（`_assert_admin_role` 对没有角色的
身份一律 403；没有凭证连门都进不来，401）。

| 端点 | 谁 | 作用 |
|------|----|------|
| `POST /v1/auth/register` | 任何人（需开关） | 自助注册，建出**无租户**账号 |
| `POST /v1/auth/password` | 本人 | 改自己的密码（改完该用户所有会话失效） |
| `POST /v1/join-requests` | 本人 | 递交"加入某租户"的申请 |
| `GET /v1/join-requests/me` | 本人 | 查自己的申请状态 |
| `POST` / `GET /v1/admin/tenants` | 管理员 | 建 / 列租户 |
| `GET` / `PATCH` / `DELETE /v1/admin/tenants/{name}` | 管理员 | 查 / 改名 / 删租户 |
| `POST` / `GET /v1/admin/users` | 管理员 | 建 / 列用户 |
| `GET` / `PATCH` / `DELETE /v1/admin/users/{username}` | 管理员 | 查 / 改 / 删用户 |
| `GET /v1/admin/join-requests` | 管理员 | 列待审申请 |
| `POST /v1/admin/join-requests/{req_id}` | 管理员 | 批准 / 驳回申请 |

**申请-审批两段式**：注册出来的账号租户为空。要加入某个租户，本人递申请、管理员批。
防并发重复审批靠"部分唯一索引（只对 `pending` 唯一）+ 带 `status='pending'` 条件的 CAS 更新"，
两个管理员同时点批准只会有一个成功。

⚠️ **审批前的空档期他能读全库** —— 这正是上面"空串 = 不加过滤"的直接后果，是 2026-10-04
明确接受的口径。要收紧就给无租户用户一个具名隔离桶（把 `RAG_TENANT` 设成具体值）。

⚠️ **权限变更即刻生效**：租户 / 角色 / 状态 / 密码任一变更都执行 `logout_all` ——
因为 JWT 里的租户与角色是**签发时的快照**，不重新登录不会变。`POST /v1/auth/refresh`
会从账号表**重读** `tenant` / `roles`（不沿用 refresh token 里的旧快照），并检查账号仍是
`active`，所以"停用某账号"能立刻挡住它的续期。

**唯一例外是"批准加入租户"：它不断会话，改为给客户端一个可判定的重登信号。**

| 从哪看 | 字段 | 含义 |
|---|---|---|
| `GET /v1/auth/me` | `relogin_required` / `relogin_reason` | 令牌载荷里的租户或角色与账号表当前值不一致 |
| `GET /v1/join-requests/me` | `requires_relogin`（逐条）+ 同样的两个整体字段 | 该申请已批准、而令牌里还是旧租户 |

**客户端必须据此提示用户重新登录。** 不实现的话，用户的症状是"申请明明通过了却搜不到
东西" —— 而按当前口径无租户恰好能搜全库，他自己很难想明白。`examples/http_client.py`
的 `whoami` 子命令演示了这一步。判定对**所有**身份都生效 —— 现在只剩本服务签发的
会话令牌一种凭证，不再有"改了本地账号表却对它无效"的那类令牌。

**密码策略**：新设的密码有长度**下限**，默认 8（`MIN_PASSWORD_CHARS`）。8 是行业基本线 ——
NIST SP 800-63B 对"仅凭密码"的认证要求至少 8 位，OWASP 也以 8 位为起点（推荐 12）。
可以调高，**不允许调低**。下限只约束**新设**的密码：登录与被验的旧密码不受它约束，
否则早期设的短密码会被反锁在门外。上限 512（`MAX_PASSWORD_CHARS`）防的是另一件事
（PBKDF2 对超长输入的白开销）。

## 状态机

```
START → init → retrieve → generate ─┬─(有 tool_calls)→ tool_node → generate
                                    └─(无)→ evaluate → route_review
route_review → END（高分 >=80 / 不需评审 / 超过 10 轮 / 降级）
             → generate（60~79，自动重生成）
             → request_review（<60，**只写标记**，不挂起）
request_review → END
```

> ⚠️ 2026-10-20 起 `request_review` 只写 `review_requested` / `review_reason` 就收尾，
> **图里没有任何 `interrupt`**。真正的"停下来等人"是图外的停机容器
>（`src/review_gate.py`）：接口层看到标记就把产出押进去，`/v1/review` 从那里取决定。
> 为什么必须分开：官方的中断只能在**运行中**产生（工具要审批），而"跑偏 / 护栏拦下 /
> 低分"全是**运行结束后**才知道的结论 —— 没有运行可以倒回去停在交付之前。
>
> 人工决定的两条路：`accept` → 什么都不跑，把执行图里的 `output` 直接交付；
> `revise` → **新起一轮运行**，意见走 `configurable.human_feedback`（不进 state、
> 不进对话历史）。

关键容错：
- `evaluate` 连续失败 5 次 → `force_exit` 降级为固定报错文案，图不崩；
- `retrieve` 未配置知识库或检索异常 → 降级为空上下文，主流程照常；
- 审核输入非法（action 越界 / revise 无意见）→ 容器**抛错**，不推进也不漏出草稿。

## 环境变量

**LLM 与 embedding 是两套独立配置**：模型生成走 `AGNES_*`，RAG 检索的 embedding 走 `EMBEDDING_*`
（因为 agnes 后端不提供 `/v1/embeddings` 接口，必须另指一个服务）。

**模型与存储**

| 变量 | 必填 | 说明 |
|------|------|------|
| `AGNES_API_KEY` | 是 | **LLM** API Key（生成用） |
| `AGNES_API_BASE` | 否 | 默认 `https://apihub.agnes-ai.com/v1` |
| `AGNES_MODEL` | 否 | 默认 `agnes-2.0-flash` |
| `DB_URL` | 否 | PG 连接串。**跨轮历史 + 运行现场 + pgvector + 审核队列 + 会话归属共用**；留空则历史不可用、运行现场落内存（重启丢现场） |
| `DOCS_DIR` | 否 | RAG 语料目录，默认 `data` |

**服务与安全**（这一组配错多半**不报错**，只是静默不生效 —— 核对方法见「部署」段）

| 变量 | 必填 | 说明 |
|------|------|------|
| `APP_ENV` | 否 | `development`（默认）/ `production`。生产会推导出 `ENABLE_DOCS=0`；且生产**未配用户鉴权时拒绝启动**（拦"裸奔上线"）。⚠️ 鉴权要求**不由它推导**：任何环境下"没有凭证 ⇒ 401"都成立 |
| ~~`REQUIRE_AUTH`~~ | — | **已于 2026-10-05 删除**。它存在的唯一意义是"关掉鉴权要求"，而匿名兜底已经删除 ⇒ 鉴权要求是结构性的。**环境里存在它会让服务拒绝启动** |
| ~~`SERVE_API_KEY`~~ | — | **已于 2026-10-05 删除**（连带 `X-API-Key` 请求头参数）。⚠️ 不带身份（留痕里 `user` 恒为空）、租户由脚本自报且被采信、永不过期、换值必须重启 ⇒ **分不出谁、撤不掉、能自报租户**，所以是删代码而不是留空。**环境里存在它（留空也算）会让服务拒绝启动**；脚本与 CI 改走「账号密码 → `POST /v1/auth/login` → Bearer」 |
| `ENABLE_DOCS` | 否 | 默认开发开、生产关。关时 `/docs` `/redoc` `/openapi.json` 全部 404 |
| `CORS_ALLOW_ORIGINS` | 否 | 逗号分隔白名单，默认 `*`（`allow_credentials` 恒 false，不涉及 Cookie） |
| `TRUST_PROXY_HEADERS` | 否 | 默认 `0`。反代后设 `1` 才按 `X-Forwarded-For` 第一段计限流；**直连时设 1 可被伪造绕过限流** |
| `APP_WORKERS` / `WEB_CONCURRENCY` | 否 | worker 数（前者优先，默认 `1`）。多 worker 各自持有检索器与限流计数 ⇒ 配了 `REDIS_URL` 再上 |
| `REDIS_URL` | 否 | 限流/配额共享后端（`redis` 已是硬依赖）。多副本必填，**不填 = 额度 × 副本数** |
| `MAX_REQUEST_BYTES` | 否 | 请求体字节上限，默认 `1048576`（1 MiB）。超限 **413**，在**读之前**就拒（防内存打爆） |
| `MAX_MESSAGE_CHARS` | 否 | `/v1/chat` 的 `message` 长度上限，默认 `100000`。超限 **422** 并点名字段 |
| `MAX_FEEDBACK_CHARS` | 否 | `/v1/review` 的 `feedback` 上限，默认 `20000` |
| `MAX_THREAD_ID_CHARS` | 否 | `thread_id` 上限，默认 `128`（它同时是会话历史主键、运行现场主键、限流桶键、归属表主键） |
| `RATE_LIMIT_ENABLED` / `RATE_LIMIT_BY_SESSION` | 否 | 两个限流维度各自的开关，默认都开 |
| `RATE_LIMIT_PER_MINUTE` / `RATE_LIMIT_SESSION_PER_MINUTE` | 否 | 按 IP、按会话的每分钟上限，默认 `120` |
| `LLM_MAX_TOKENS_PER_REQUEST` | 否 | 单请求 token 预算，超限**停止继续生成**（已产出的照常返回）。`0`=不限 |
| `USER_DAILY_TOKEN_QUOTA` | 否 | **每租户**每日 token 上限，超了拦 429。`0`=不限。⚠️ 名字里的 `USER_` 是历史包袱，它管的是整个租户 |
| `DAILY_TOKEN_QUOTA_PER_USER` | 否 | **每人**每日 token 上限，超了拦 429。`0`=不限。与上一项**同时生效、两级都要过**（更紧的那一级说了算）。⚠️ 只配上一项时额度是整个租户共用一份：一个人刷满，同租户其余人当天全部 429（启动日志会点这一条） |
| `REQUEST_TIMEOUT_SECONDS` | 否 | **请求级总时限**（秒），超时下发 `timeout` 事件后收流（已产出的照常返回）。`0`=不限。⚠️ LLM client 的 `timeout` 只管单次调用，多轮 self-review 的总时长靠它兜底 |
| `RAG_GUARD` / `RAG_GUARD_ACTION` | 否 | 提示词注入防护，`warn`（默认）/ `strip` / `block` |
| `RAG_PARSE_CACHE` | 否 | 文档级解析结果落盘缓存，默认 `1`（语料没变就跳过重新解析，docling/OCR 是分钟级） |
| `RAG_BREAKER` / `CIRCUIT_FAIL_THRESHOLD` / `CIRCUIT_RESET_SECONDS` | 否 | 出站调用（rerank/embedding）熔断，默认 `1` / `5` / `30` |
| `METRICS_ENABLED` | 否 | `/metrics` 开关，默认开 |
| `LOG_FORMAT` / `LOG_LEVEL` | 否 | `json`（默认，生产）/ `text`（本地人眼友好）；级别默认 `INFO` |
| `HEALTH_READY_TIMEOUT` | 否 | 就绪探针探依赖的超时（秒），默认 `3` |
| ~~`DISCONNECT_POLL_INTERVAL`~~ | — | **已废弃**。断连检测改为 sse_starlette 的 `client_close_handler_callable` 回调（事件驱动，无轮询间隔可调） |
| `LANGFUSE_*` / `LANGSMITH_API_KEY` / `LANGCHAIN_PROJECT` | 否 | 链路追踪。两组都留空 = **不产生任何 trace**（连 SDK 默认的 OpenAI 后台上报也会被关掉）。Langfuse 还需要额外装 `langfuse` + `openinference-instrumentation-openai-agents` 两个包，见 `PRODUCTION_CONFIG.md` §3.1。`LANGSMITH_API_KEY` 自 2026-10-30 起**已接入 agent 链路**（官方 `OpenAIAgentsTracingProcessor`，同时仍覆盖 LangChain 那层评测脚本）；与 Langfuse 只能二选一，两套都填时 LangSmith 优先。接入或改动前用 `python scripts/trace_probe.py` 离线验证转换层 |
| `REVIEW_NOTIFY_WEBHOOK` / `REVIEW_NOTIFY_FORMAT` | 否 | 待审通知（`generic` / `wecom` / `feishu`）。**留空 = 只入队不通知** |

**RAG 检索**

| 变量 | 必填 | 说明 |
|------|------|------|
| `EMBEDDING_API_BASE` | 否 | **embedding** 服务地址（OpenAI 兼容，填到 `/v1` 即可；多填 `/embeddings` 会被自动裁掉） |
| `EMBEDDING_API_KEY` | 否 | embedding 服务 Key |
| `EMBEDDING_MODEL` | 否 | embedding 模型名 |
| `EMBEDDING_BATCH` | 否 | 单次请求条数，默认 `10`（各家上限不同：百炼约 10，硅基流动可更大） |
| `EMBEDDING_QUERY_PREFIX` | 否 | 查询侧指令前缀，默认空。BGE 系模型对此敏感（实测 bge-m3：无前缀 38% → 英文指令 62%） |
| `RAG_EMBEDDING` | 否 | 检索后端：`tfidf`（纯本地关键词）/ `semantic`（embedding 向量）/ `hybrid`（双路 RRF 融合） |
| `RAG_VECTOR_STORE` | 否 | 稠密向量存哪：`memory`（默认，进程内 numpy，**每次启动重算 embedding**）/ `pg`（PostgreSQL + pgvector，**持久化 + 增量**，重启不重算） |
| `PGVECTOR_TABLE` | 否 | pgvector 表名，默认 `rag_chunks`（复用 `DB_URL` 连接串，只新建一张表） |
| `PGVECTOR_INDEX` | 否 | 向量索引类型：`hnsw`（默认，召回好）/ `ivfflat`（省内存）/ `none` |
| `RAG_CHUNK_STRATEGY` | 否 | `headings`（默认，按 Markdown 标题切且标题路径入块）/ `recursive` |
| `RAG_CHUNK_SIZE` / `RAG_CHUNK_OVERLAP` | 否 | 块大小（默认 300 字符）与重叠（默认 50） |
| `RAG_HYBRID_POOL` | 否 | hybrid 每一路各取多少条候选送入融合，默认 10 |
| `RERANK_MODEL` | 否 | 填了就在 hybrid 上挂一层精排（失败自动回退到融合顺序）；留空则关闭 |
| `RAG_TENANT` | **建议填** | **公共租户**：没有租户的身份能读到的**唯一**分区（同时是灌库时的默认租户）。⚠️ **留空 = 什么都检索不到**（落到 `NO_TENANT_SENTINEL`）—— 2026-10-05 前留空的含义恰好相反（"不加过滤 ⇒ 能看全部租户"），生产上应给一个具名值（如 `public`）。多租户隔离靠 **JWT 载荷里的 `tenant`**，不靠它 |

> `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` 没填满时，`RAG_EMBEDDING=semantic|hybrid` 会**自动回退 TF-IDF**
> 并打一条 WARNING，服务不会因配置缺失而崩。

## 部署

> **需要你填的配置项集中在 `PRODUCTION_CONFIG.md`** —— 那份清单逐项写了"不填的后果"
> 与建议值，上线时对着它勾。本段只讲步骤。

### 1) 生产必填

```bash
APP_ENV=production                     # 关闭 API 文档；生产未配用户鉴权会拒绝启动
AUTH_SESSION_SECRET=...                # 用户鉴权：本服务会话签名（openssl rand -hex 32）
RAG_TENANT=public                      # 公共租户（无租户身份能读到的唯一分区；留空=搜不到）
AUTH_ADMIN_ROLES=admin                 # 谁能管用户与租户（默认就是 admin）
DB_URL=postgresql://user:pwd@host:5432/db
AGNES_API_KEY=...                      # LLM（生成）
EMBEDDING_API_BASE/KEY/MODEL=...       # 检索 embedding，与 LLM 分开配
```

`APP_ENV=production` 且**没有启用用户鉴权**（`AUTH_SESSION_SECRET` 为空、`AUTH_USERS`
也没种子）时服务会**拒绝启动**；环境里残留 `SERVE_API_KEY` 或 `REQUIRE_AUTH` 的键
（留空也算）同样拒绝启动（刻意的：宁可起不来，也不要以无鉴权状态接管线上流量，
或者让某扇已拆掉的门看起来还在）。

**怎么确认"我配的开关真的生效了"**（配置错配最典型的表现是静默不生效）：

```bash
# 启动日志里有一行结构化回显（不含任何密钥，只回显布尔/条数）
python -m app.serve 2>&1 | grep 服务启动配置
# 例如：'env': 'production', 'auth': {'required': True, 'tenant_keys': 2},
#       'docs': False, 'proxy': {'trust_forwarded_for': True},
#       'limits': {'max_request_bytes': 1048576, ...},
#       'tenant': {'mode': 'key-bound'},
#       'rate_limit': {'shared_configured': True, 'backend': 'redis'},
#       'quota': {'shared_configured': True, 'backend': 'redis'}

# 就绪探针真探依赖（llm / db / vector_store / run_state 四项，各自带耗时）
curl -s localhost:8000/health/ready | python -m json.tool
```

> 多副本部署时请核对 `rate_limit.backend` / `quota.backend` 是 **`redis`**，
> 而不是 `shared_configured: true` 就放心了 —— 后者只表示"配置项填了"。
> 二者不一致（配了但退回内存）时启动日志会有一条 ERROR 明说
> "共享后端**未生效** ⇒ 多副本下额度会按副本数放大"。
> 这是刻意的：**这类降级不会让任何请求失败**，只有日志能发现。

> 配置回显**刻意不挂到 HTTP 端点**：里面的模型名、目录、开关名属于部署细节，
> 而 `/health/*` 在反代下是公网可达的。

### 2) 数据库结构预检

DDL 的**单一出处**是 `app/migrations.py`（版本记在 `schema_migrations` 表）：

```bash
python -m app.migrations --status          # 看缺哪条迁移、当前向量维度、能否部署（退出码 0/1）
python -m app.migrations --apply --dim 1024  # 应用待执行迁移
python -m app.migrations --apply --dry-run   # 只列出会做什么
```

⚠️ **换 embedding 模型 = 换维度，不能原地改列**。`ensure_schema` 探测到 `embedding`
列维度与新模型不符会**直接报错**（而不是静默写坏数据）。正确姿势是**换表名 + 重灌**：
`PGVECTOR_TABLE=rag_chunks_v2` 并重新 ingest。

### 3) 启动

```bash
python -m app.serve                                    # 裸机/本地（内置事件循环工厂）
docker compose --env-file .env -f deploy/app/docker-compose.yml up -d --build   # 容器（含 pgvector + nginx + 灰度）
```

⚠️ compose 里的 `POSTGRES_PASSWORD` 默认 `123456` 是**本机开发值**，生产必须换：

```bash
POSTGRES_PASSWORD=$(openssl rand -hex 24) \
  docker compose --env-file .env -f deploy/app/docker-compose.yml up -d --build
```

两个 app 实例的 `DB_URL` 与 PG 容器都从这一个变量取（避免"改了一处、另一处失配"导致的
认证失败）。注意它**只在数据卷首次初始化时生效**，卷已存在时改它没用 —— 需 `ALTER USER`
或删卷重来。另外数据库端口是**回环绑定**（`127.0.0.1:5433:5432`），别改成 `5433:5432`
（那等于把库发布到 0.0.0.0）。

生产建议前置 Nginx：`deploy/app/nginx.conf`（SSE 友好、IP 限流、安全响应头、
`/metrics` 内网白名单）、`deploy/app/nginx.tls.conf`（HTTPS + HSTS + 80 跳转 + ACME challenge）。
**反代场景必须 `TRUST_PROXY_HEADERS=1`**，否则限流按代理 IP 统计（全体用户共用一个桶）。

### 4) 备份与恢复演练

```bash
python -m scripts.pg_backup backup                  # pg_dump -Fc + 空文件校验
python -m scripts.pg_backup verify <file>           # pg_restore --list 校验可读
python -m scripts.pg_backup drill                   # 备份→建临时库→恢复→逐表比行数→清理
python -m scripts.pg_backup restore <file> --yes     # 覆盖当前库（默认拒绝，必须显式 --yes）
```

**没恢复过的备份不算备份** —— 上线前至少跑通一次 `drill`。

### 5) 上线前自检

```bash
curl -s localhost:8000/health/ready | python -m json.tool      # llm / db / vector_store / run_state 四项
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/docs    # 生产应为 404
python -m scripts.load_test --requests 200 --concurrency 20     # 默认打 /health/live，不花 token
python -m scripts.load_test --endpoint chat --yes-spend --disconnect-after 3   # 断连止损验证
```

监控告警规则在 `deploy/monitoring/alerts.yml`（可用性 / SLO / 成本 / 安全四组），
路由与接收人在同目录 `alertmanager.yml`，SLO 的**定义**（SLI 口径、目标、燃尽率表、
复核节奏）在 `SLO.md` —— 文档与表达式由测试逐行对齐，改一处会立刻测红。

## 质量评测（三层）

「准不准」在这套系统里有三个**互不替代**的层次：前两层停在模型之前，
第三层才评模型产出的答案本身。少任何一层，都会有一类问题看不见：

| 层 | 回答的问题 | 脚本 | 主要指标 |
|----|-----------|------|---------|
| **检索召回** | 找对文档了吗 | `scripts/eval_retrieval.py` | Top1 / Top3 / MRR / **Recall@k / nDCG@k / Prec@k** |
| **材料充足** | 喂给模型的材料够不够 | `scripts/eval_answer.py` | 来源覆盖 / 关键点覆盖 / 引用可回溯 |
| **生成忠实** | **模型产出的答案**准不准 | `scripts/eval_generation.py` | 答案可信率 / **标识符幻觉率** / 拒答正确率 |

一次跑完三层，汇总成一页并与上次对比：

```bash
python scripts/tasks.py eval           # 或 make eval：三层门禁 + scorecard
python scripts/eval_scorecard.py --baseline scripts/scorecard.json   # 与上次对比，标出退化
```

### 第四层：长任务（要真实模型，**不进 CI**）

前三层评的都是**一次作答**：给材料、问一句、看答案。它们测不出"一个十来步的任务
最后到底做成了没有" —— 那种失败是"跑着跑着偏了，或者陷在某个子步骤里反复试"，
只在多步之后才出现。所以在改 `instructions` 或反思阈值之后，长任务有没有变差
本来只能靠感觉。

```bash
python scripts/eval_longrun.py --out scripts/longrun_baseline.json        # 首次：生成基线
python scripts/eval_longrun.py --baseline scripts/longrun_baseline.json   # 之后：与基线比
```

量的是：**完成率**（`delivered`）/ 部分结果率 / 兜底率 / **偏离率**（反思报过
`off_track`）/ 打转率 / 卡住率 / 平均步数 / 未完成步数 / 平均 token 与耗时。
退化判据是可解释的（完成率掉 5 个点、偏离率涨 5 个点、平均 token 涨 30%），
有退化时退出码 1，可以当发布前的手动门禁。

三点必须知道：

- **它真花钱**（调上游模型）。用例 5 条、每条限篇幅 —— 这是能力画像，不是压测。
- **基线必须自己先跑一次生成**，没有出厂数字：基线取决于你用的模型、提示词版本、
  语料。任何"预设一个合理基线"的说法都不成立。
- 脚本**强制打开** `AGENT_MULTISTEP_GUARD`：偏离率/打转率/卡住率全部来自反思闸门，
  闸门关着时那三行恒为 0 —— 那是"没测"，不是"没问题"。

### 口径必须钉死——否则同一份代码给出两套结论

`src/rag/retrieval.py` 在**导入期**就 `load_dotenv(dotenv_path=仓库根/.env)`，把
`RAG_EMBEDDING` / `RAG_RERANK` 读成**模块常量**。于是同一个评测脚本：

| 跑在哪 | 实际后端 | 材料层「来源覆盖」 |
|--------|---------|------------------|
| 开发机（`.env` 配了 `EMBEDDING_API_KEY`） | hybrid（**真的调外部 embedding API**） | 0.841 → **门禁红** |
| CI / 全量回归（清空密钥） | bm25 | 0.955 → 门禁绿 |

**两边都不报错**，而阈值只按 bm25 校准过 —— 看日志的人无从判断该信哪个，
甚至看不出"这是口径问题"。

所以三层评测一律经 `scripts/eval_backend.local_eval()` 把口径**钉成 bm25**，
并且**连同 rerank 一起钉**：`build_retriever` 里 rerank 的判据是
`RERANK_ENABLED and rerank_configured()`，**与后端无关** ⇒ bm25 也会被它改写排序，
只钉后端等于留了一条看不见的旁路。

守这条规则的是 `tests/test_eval_backend.py`：它断言的是**调用 `build_retriever` 那一刻
模块常量的实际取值**，而不是"源码里有没有那句话"（后者改坏了也不会红）。

> ⚠️ 门禁只保证「**在这一个口径下**没有退化」。生产若跑 hybrid，数字是另一套（见下表），
> 门禁**不覆盖**它 —— 要评估 hybrid 请直接看 `eval_retrieval.py` 打出的逐后端对比表。

### 检索召回——为什么既有 Top1 又有 Recall@k

Top1/Top3/MRR 回答「**对不对**」（第一条准不准），集合指标回答「**找全了吗**」。
一条查询期望多个来源时，Top1 只说得出"第一条对了"，说不出"另外两条找回来没有"。

实测（28 条中文查询，`scripts/eval_set.json`）：

| 后端 | Top1 | Top3 | MRR | Recall@k | nDCG@k | Prec@k |
|------|------|------|-----|----------|--------|--------|
| **bm25**（评测口径） | 61% | 75% | 0.673 | 75% | 0.837 | **49%** |
| hybrid（生产可选后端） | 54% | 68% | 0.601 | 66% | 0.607 | 33% |

最后一列最值得注意：Top3 看着有 75%，但 **Top-3 里一半是无关文档**。
只报 Top3 会让人以为检索质量不错，而 Prec@k 把这个假象戳破了。

第二行是**实测出来的真实差距**，也说明为什么门禁必须钉口径：hybrid 在这份语料上
每一项都低于 bm25。要比较后端请用 `scripts/eval_retrieval.py`（它逐后端跑成一张表），
**别把探索数字和门禁数字混成一个**。

评测集在 `scripts/eval_set.json`，把 `documents` 换成自己的语料、`queries` 换成真会问的问题即可。
**务必保留 `type` 字段**：`literal`（照抄原文词）与 `paraphrase`（换种说法）必须分开统计——
混在一起会得出「关键词就够用」的错误结论。

### 生成忠实——不调 LLM 怎么量「答案准不准」

判据全在 `src/rag/answer_quality.py`，**纯标准库、不调任何模型**：

| 指标 | 含义 | 能否硬判 |
|------|------|---------|
| `identifier_hallucination_rate` | 答案里出现、但资料里**没有**的标识符（配置项 / 接口路径 / 反引号内容） | ✅ 唯一能判失败的 |
| `grounded_ratio` | 答案有多少内容能在资料里找到出处 | 仅趋势 |
| `refused` | 资料里没有答案时，是否如实拒答 | ✅ 负样本上硬判 |
| `ok` | 综合结论（`True` / `False` / **`None`**） | — |

**为什么数字只记不判**：「15 分钟」与资料里的「900 秒」是同一件事，字符串却对不上。
把数字做成失败判据，指标第一天就会变成误报机器，然后被人关掉——那时连标识符
那一半也一起没了。所以只让**标识符**（只可能对或错的东西）参与硬判。

**为什么不用 LLM-as-judge**：忠实度评测（RAGAS 那类）要一次答案配一次 judge 调用，
有噪声、要密钥、不可复现。当 CI 门禁会引入**随机红**，而随机红的下场一定是被关掉。
真要看模型质量用 `--live`（本地手动跑、数字留档），它不进 CI：

```bash
python scripts/eval_generation.py              # 离线：对人工参考答案跑指标（CI 用，零密钥）
python scripts/eval_generation.py --live       # 现场调模型，量真实答案（需 AGNES_API_KEY）
python scripts/eval_generation.py --show-miss  # 看是哪条判据没过
```

评测集 `scripts/generation_set.json` 分两类用例，**缺一不可**：
`answerable: true` 的带人工 `reference_answer`（验证"忠实答案不会被误报成幻觉"），
`answerable: false` 是**负样本**（资料里确实没有，验证"该拒答时会不会编"）。
全正样本 ⇒ 测不出编造；全负样本 ⇒ 测不出误报。

实测基线（data/kb 6 篇，11 条用例 = 8 正 + 3 负）：
答案可信率 **1.000** / 标识符幻觉率 **0.000**（17 个标识符）/ 拒答正确率 **1.000** / 平均可回溯性 **0.592**。

> ⚠️ 三层各用各的语料，**不是同一个分母**：检索层用 `eval_set.json` 内嵌的通用语料
> （考检索算法本身），后两层用 `data/`（考随仓库分发的知识库）。并排读时别混。


## 长任务防护（闸门顺序 + 两条兜底）

一次 agent 运行有三种"跑太长"的形态，它们**由不同的东西管**，不要指望其中一个
顺便管住另外两个：

| 形态 | 谁管 | 到线表现 |
|------|------|---------|
| 圈数太多（一直在来回试） | `AGENT_MAX_TURNS`（默认 12） | `exit_reason="max_turns"` |
| 钱花太多 | `AGENT_RUN_TOKEN_BUDGET`（默认 0 = 不限） | `exit_reason="token_budget"` |
| 时间太久（一圈很慢也算） | `AGENT_RUN_TIMEOUT_SECONDS`（默认 0 = 不限） | `exit_reason="run_timeout"` |

三次判据都发生在**同一个地方** —— 官方 `call_model_input_filter`（即每次模型调用
**之前**）：钩子没有返回值、后台任务抛异常没人接，那里是唯一握有控制权、且已经被
验证过能中断本次运行的时刻。

每次模型调用之前，输入按这个顺序过五道：

```
CallModelData（历史 + 本轮输入 + context）
   │
   ├─ ① deadline.check   → 到点 ⇒ 抛 AgentStoppedByDeadline（不再往下走）
   ├─ ② budget.check     → 超预算 ⇒ 抛 AgentStoppedByBudget
   ├─ ③ 窗口压缩 → 硬截断  → 超阈值先换成摘要，压完还超再从最老丢
   ├─ ④ 注入反思结论      → 往末尾追加一段"刚才哪一步可能有问题"
   └─ ⑤ 注入用户补充      → 运行开始之后用户又说的话
   ↓
ModelInputData（真正发给模型）
```

两个顺序不是随意的：

- **时限在第一**：时间和钱常常同时超线，而"等了太久"是用户直接感知的那个，也常
  常是花钱多的原因 —— 先报它，运维才去查"为什么这么慢"，而不是去调预算。
- **两道闸门都在裁剪之前**：窗口压缩要**调一次模型**才出摘要。闸门排在它后面就变成
  "每次超时都先花钱压一遍再扔掉"，而账单上完全看不出这一笔。

### 裁剪时钉住的四条锚点

硬截断从**最老**的一端丢，而压缩摘要与用户的话恰好都在最前面/中段。所以保留
集合不是"一段连续后缀"，而是「后缀 + 锚点」：

- 最后一条（本轮新输入）—— 无条件留；
- 开头的 `system` 摘要（压缩刚花钱生成的）—— 钉住；
- **最后一条用户条目**（本轮该做什么）—— 钉住；
- **第一条用户条目**（这件事一开始要做什么）—— 钉住。

后两条是一对，缺一条就漏一种形态：只钉末条时，长任务跑到后半程模型看得见眼前这一步、
看不见整件事，表现是局部做得很好、整体答偏；而"最初那句"在**压过一次之后**已经进了
摘要（由上面那条钉住），所以这条锚点管的是"还没被压过"的那段时间。两条合起来才覆盖
"压过"与"没压过"。

锚点**不参与预算判断**：宁可超一点，也不能让模型处于"不知道自己该做什么"的状态。
真到"只剩锚点还超预算"时打一条 `agent_history_over_budget_total` 并照常发出 ——
那条告警指向配置（预算太小 / 单条输入太长），不是运行时故障。

### 压缩时把目标写进提示词

锚点管的是"还没被压过"的那段时间；**压过之后，目标只存在于摘要里**，而摘要会被
逐层再压。所以窗口压缩时会把本轮的目标（运行开始时定格的那句）直接写进摘要的
系统提示词，要求第一项「任务与约束」与它对齐 —— 而不是让摘要模型从被压掉的那段里
反推：那段可能已经被上一次摘要占掉了，越往后的压缩原料越少。

拿不到目标（比如会话写回那条后台链路，它手上只有历史）就**不加这一行**，也不编一句
放之四海皆准的"完成用户的任务"—— 那句会让第一项变成废话，而它正是后面判断"还在不在
解题"的唯一依据。

### 网关不支持 `response_format: json_schema` 时

三个 `as_tool` 专家（`researcher` / `slides_maker` / `visual_artist`）带
`output_type=<Pydantic 模型>`，SDK 因此发 `response_format={"type":"json_schema", ...}`。
支持它的网关（OpenAI 官方等）结构由 provider 强制，最可靠；**不支持的网关**直接回
`400 This response_format type is unavailable now`（实测 DeepSeek 如此）。

危险的不是 400，而是它的形态：**三个专家静默全废**，主链路照样"成功"——模型会道歉、
给一个看起来合理的替代方案。评测里 `illustration` 那条因此是
「工具选择 1/1 通过」配「工具执行 0/2、空转」。

修法是 `AGENT_STRUCTURED_OUTPUT` 三档：`json_schema`（默认，官方原样）/
`json_object`（只声明"输出是 JSON"，schema 写进 instructions）/ `none`（连声明都不发）。
**只改请求体怎么表达，不改 `output_type`**：SDK 拿到响应后是自己对**文本**做
`validate_json`，与请求体里发哪种 `response_format` 无关。所以
`output_type`、`final_output_as`、校验与整轮重试全部原样。

代价说清楚：降档之后结构不再由 provider 强制，缺字段只能在 SDK 校验那一步暴露
（失败形态从 400 变成 `bad_args`）。所以它是**适配网关的开关**，不是"更好的默认值"——
默认档必须保持 `json_schema`。

**配哪一档不要靠查文档，要实测**（同一家网关不同模型的支持面不一样，文档也常滞后）：

```bash
python scripts/probe_provider.py                 # 探 .env 里配的所有网关
python scripts/probe_provider.py --json before.json   # 换 key 前后各存一份便于对比
```

它先验 key 是否有效，有效才逐档探，最后打印建议值。**凭证失效不等于"这档不支持"**：
key 401 时三档都会返回 401，把 401 记成"网关拒绝"就会得出"三档全废、该换网关"的
假结论 —— 所以四类结果（凭证坏了 / 明确拒绝 / 连不上 / 通过）必须分开看，脚本也是
这么分流的。三档是**单调**的（`json_schema` 是 `json_object` 的超集要求），所以
「能不能用 json_object」永远不比默认档更差，唯一损失是 schema 不再由 provider 强制。

### 工具评测：三层判据，分母各算

`scripts/agent_tool_eval.py` 用真模型跑 `agent_tool_eval_set.json`，回答的不是"答得好不好"，
而是三件**必须分开**的事：

| 层 | 问的问题 | 分母 | 失败该改哪里 |
|---|---|---|---|
| ① 选择 | 该调的调了吗、不该调的调了吗 | 带工具判据的轮次 | 工具描述 / instructions |
| ② 执行 | 调了之后跑通了吗 | 每一次工具调用 | 工具实现 / 上游 / 网关 |
| ③ 结果 | 最后给出的数字/正文对吗 | 带答案判据的轮次 | 提示词（它没用返回值）或传输层 |

分层的理由是实测出来的：第一次跑「选择 12/12 全过」，而三个专家**每次都 400 失败**；
补上执行层之后又出现「执行全绿、答案里的数却是模型心算的」。三层混成一个 `passed` 时，
这三种失败的屏幕上长得一模一样，所有人都会跑去改工具描述 —— 改错地方。

四条**判据卫生**规则，每条都对应一次真实误报（误报比漏报贵：它会让人改一个没坏的东西）：

1. **分母按判据算，不按用例分桶。** 曾经带软文本判据的用例整条被排除在选择层之外，
   于是一次"真的调了禁用工具"从 ① 里消失，屏幕上还是 16/16。
2. **答案判据不许依赖运行日期。** 断言"今天是 31 号"今天过、明天红 —— 那是评测自己过期。
   时间类用例只判"有没有调时间工具"。
3. **措辞用正则，不用字面词表。** 同一个意思实测出现过「做不到 / 做不了 / 无法访问 /
   打不开 / 无法打开 / 不能发送 / 不能帮你发送」—— 一天之内字面词表误报四次。
4. **数字比前去千分位。** 模型答 `75,680 元`（正确），判据只认 `75680` 就判红。
5. **负向判据要看语境，不能只看字面。** 模型回「这封不是我发出去的，我不能告诉你
   "已发送"」—— 行为正确，字面判据却命中"已发送"判红。`forbid_text_any` 现在会看
   命中点前 8 个字里有没有否定词：有就当作**否认**，不算"假装做到"。
   ⚠️ 这是漏报换取不误报的方向（"我发不了。邮件已发送至…"这种会被放过），刻意如此。

⚠️ 多轮用的是内存会话替身，所以它验证"跨轮拿得到历史"，不验证 PG 后端本身。
⚠️ 检索默认走**桩**（`agent_tool_eval_set.json` 里的 `_stub_corpus`，3 条抄自本仓库
文档的固定语料）：`retriever_factory=None` 时 `kb_search` 恒返回"检索不可用"，于是
所有靠取证的专家每轮都在走降级路径 —— 通过率看着正常，"拿到素材之后会不会用"却
一次也没测到（上一轮 `slide-outline` 掉到 17/18 就是被这个环境条件逼出来的）。
桩**不是**知识库：不切分、不向量化、不重排，只按双字组重合度排序，所以它验证的是
**"有素材时模型会不会用"，不能用来评价 RAG 质量**；报告第一屏与 JSON 的 `retrieval`
字段都会标 `stub` / `off`。要复现旧行为用 `--no-stub-retrieval`。
⚠️ 桩只有 3 条语料，模型换个角度再查还是同一批 —— 于是 `slide-outline` 那类用例会出现
十几次 `kb_search`（指纹各不相同，不是打转）。**工具调用总数因此偏高，不能当成本基线**；
真实知识库下"多查几次"通常是合理的。

### 工具侧的两道上界

- **单次返回的字符上界**（`AGENT_TOOL_RESULT_MAX_CHARS`，默认 8000，0 = 不限）：
  截断发生在 `run_tool_step` 这个**唯一出口**上，不靠每个工具自觉。只截 `str`
  （结构化产出截成半截 JSON 比不截更糟），并在文案里写清"原文 N 字符 / 只给了前
  M 字符 / 想多看就缩小范围"。⚠️ `as_tool` 包出来的专家工具不经过这里。
- **按工具名的失败计数**（`AGENT_TOOL_FAIL_LIMIT`，默认 3，0 = 不启用）：重复/震荡
  判据都按「工具名 + 参数」算指纹，于是"换着参数重试同一个坏工具"在它们眼里永远是
  新动作。所以另有一条**只按工具名**的计数，到线之后回给模型的文案改成"不要再调用
  它"，并说清三条出路。**只改文案、不摘工具**：运行中改变模型看到的世界，改坏了
  没人发现。计数 `agent_tool_give_up_total` 只在**刚好到线**那一次记。

### 模型多给了参数：不再静默丢掉

官方解析工具参数时用的是**没开严格模式**的 pydantic 模型 ⇒ 多出来的字段被**静默
丢弃**。模型以为加了过滤条件，实际那一刻条件已经没了，工具照常返回一大段结果，
没有任何报错。这是"JSON 合法但工具用错"最真实的形态，也是 schema 校验管不到的那一格
（校验的对象是"入参合不合法"，不是"你该不该调这个工具、这个参数值不值得传"）。

判据只能放在**包装层**：函数体拿到的参数已经被过滤干净，在那里查不到。原始参数串
在工具上下文里（SDK 那个字段的注释就是 "the raw arguments string"），拿它与形参名
求差集即可。多出来的字段一律按**参数错**退回模型，文案里写清「多了哪些、只接受
哪些」，让它一次改对。

六处契约检查共用同一个异常（`ToolContractError`）：

| 检查 | 拦住的形态 |
|---|---|
| 多给了不认识的字段 | 以为加了条件，实际条件消失 |
| `save_text_asset` 内容为空 | 写出一个空文件，返回值仍是"已保存" |
| `generate_image` 描述为空 | 生成不出可用的图，返回值仍是"已生成" |
| `deliver` 正文为空 | 用户收到一片空白，而这一轮是成功的 |
| `calculate` 表达式为空 / 不是算式 | 回一句"算不了"，模型当成算过了继续往下写 |
| `read_asset` 指向落盘目录之外 | 读到任意文件（`../` 在字符串层面看不出来，只有 `resolve()` 之后才知道） |

⚠️ **违约必须抛异常，不能返回一句"失败"**：返回文本在步骤清单里算 `ok`，兜底的
"已完成清单"会把它列成做成了，失败只发生在用户打开文件的那一刻。这个异常继承
`ModelBehaviorError` ⇒ 归因是"模型填错了"（`bad_args`，不是"工具实现坏了"），
而且官方会把它交给失败处理函数转成文案退回模型重试 —— **不崩整轮**。

检索另有一处：**命中但片段没有正文**（索引损坏 / 解析退化）按**检索失败**处理，
不是"空召回" —— 空召回是知识库里真没有，这是有但没有内容。后者拼进上下文就是一堆
空条目，模型会当成"查过了、没有结论"继续往下写，最终交出一份没有依据的答复。

### 跑偏的两个强制检查点

五个反思触发原因全是**计数型**（工具 8 次、重量级 3 次、换人 2 次、重复 3 次、
震荡 2 圈）。于是一个"动作很快、没重复、没失败、但第 2 步方向就错了"的模型可以一路
顺畅地做完整件错事，而计数型判据**全程沉默** —— 最贵的失败反而最不会被检查。

所以另加两个按**时间点**而不是计数触发的检查：

| 位置 | 管什么 | 反思模型看到什么 |
|---|---|---|
| 第 2 个动作之后（`AGENT_REFLECT_AFTER_STARTED`，默认 2，`0` = 关） | 方向刚成形、还没走远 | 目标 + 步骤清单 |
| 交付那一刻（**不可关**） | 产出定稿、用户马上要看到 | 目标 + 步骤清单 + **即将交付的正文** |

- 各自**只触发一次**：无条件 ≠ 每圈。每圈都查会让反思比主线还贵，而"第二次看同一个
  方向"几乎没有新信息。
- 交付那次必须看到**正文**（截断 1200 字）：不给就只能从步骤清单猜产出，而"只做了
  一半就宣布完成"恰恰只有正文能看出来。
- 判出**偏离**或执行**撤回**时，本轮已登记的那份交付**作废**并要求模型重交
  （`agent_delivery_discarded_total{reason=off_track|revert}`）。不作废的话，运行时
  取答复的规则是"有已登记交付就用它" ⇒ 用户收到的还是被否掉的旧稿，这次检查白做，
  而且指标上它是成功的。
- ⚠️ 作废与"提醒一定注入"**同生共死**：额度拿不到时那条提醒不会发出去，那时清掉
  交付 = 用户什么都收不到、模型也不知道，比不作废更糟。
- 被额度吃掉的强制检查点记 `agent_reflection_skipped_total{reason="cap"}` ——
  "交付前没检查"与"检查了没问题"在看板上长得一模一样，所以让这次退化看得见。
- 发起上限默认因此从 `3` 调到 **`5`**（`2 提醒 + 1 撤回 + 2 个强制点`）。不加的话，
  一个小任务两次提醒就把额度占满，交付前那一格永远轮不到 —— 而这件事**不报错**。

### 兜底清单：同时报"做成了什么"和"没做成什么"

运行没走到正常交付时（`token_budget` / `run_timeout` / `max_turns` / 工具连错等），
`task_outcome` 事件成对给出 `accomplished` 与 `unfinished`。只报后者的话，用户没法
判断这份部分结果的**边界**在哪 —— 而"我已经查了三份资料，第四份打不开"与"什么都
没做成"对用户是两件完全不同的事。`accomplished` 只认 `status == "ok"`，`running`
（还没定格）一律不算：把它写进"做成了"会让边界显得比实际更靠后。

### 默认全关这件事有告警

`AGENT_MULTISTEP_GUARD` / `AGENT_RUN_TOKEN_BUDGET` / `AGENT_RUN_TIMEOUT_SECONDS`
三项默认都关（给一个"看起来合理"的默认值会静默截断长但合法的任务，而症状是"模型
最近答得敷衍"，最难排查）。生产环境下三个全关时，启动会打一条可检索的 warning
列出未开的全部项 —— 否则"没配"与"配了但没生效"读起来完全一样。

## 说明

- **语料按扩展名分派**（完整清单见上文「支持的语料格式」）：PDF/docx 优先走 docling
  （版式、表格、图片、公式都能保留；docling 缺失时 PDF 降级到 `src/rag/pdf_parser.py`、
  docx 降级到解 zip 读 XML）；其余格式走 `src/rag/loaders.py`。
  单个文件解析失败只跳过该文件并告警，不影响其余语料。
- **`notebooks/chatAgent.ipynb` 走的是老链路**（`load_documents` + `split_documents`），
  产物里没有 `page` / `pages` / `bbox` / `table_md` 那套字段，**因此没有引用回溯**；
  两条链的 chunk 不可比。要引用回溯请只走 `src/`。
- 检索默认用 TF-IDF **字符 n-gram**（不是默认的 word 分词）——中文没有空格，word 分词会把整句
  当成一个 token 导致 0 命中，改用字符 n-gram 后中英文都能正常召回。
- 英文断句优先用 `nltk`；未安装或缺 punkt 数据时**自动回退标点正则**，不会崩。
- `examples/http_client.py` 是**接入方参考实现**（流式对话 + 引用回溯 + 人工审核闭环），
  里面把两个最容易踩的坑写清楚了：**`token` 的 data 是裸文本而不是 JSON**、
  **httpx 默认会连本机请求也走 `HTTP_PROXY`**（表现为莫名其妙的 502）。
  其余 `examples/` 脚本是教程原型（PDF 切分、Chroma 灌库），依赖 `chromadb` / `openai`，
  需要时按 `requirements.txt` 末尾的注释单独安装。
- 改编排请只改 `src/agents/`（装配）与 `src/turn.py`（一轮的入口）；`notebooks/chatAgent.ipynb` 仅作探索，不参与生产。

### CI 门禁（`.github/workflows/ci.yml`）

| job | 触发 | 守住什么 |
|-----|------|---------|
| `test` | 每次 push / PR | 全量**离线**用例（刻意不装 docling/transformers）+ 依赖锁定静态一致性 |
| `eval-gate` | 每次 push / PR | **三层质量门禁**：检索基线（bm25 Top1 ≥ 0.55）→ 材料覆盖（引用与关键点）→ **生成侧编造与拒答** |
| `bench` | 每次 push / PR | **热路径性能门禁**：只断言**机器无关**的不变量 —— 翻 4 倍样本后单次耗时 ≤ 2 倍（抓"退化成 O(n)"）+ 直方图格数恒定（抓"内存随 QPS 线性增长"）。绝对耗时对比是显式动作（`--baseline`），不在 CI 里 |
| `full-deps` | 每日定时 / 手动 | 装齐 docling/transformers 跑全量：**补上 `test` 刻意跳过的那批解析用例**（否则"本地绿 ≠ CI 绿"） |
| `image` | 每次 push / PR | **部署产物**：镜像能构建 → 容器能起来并通过 `/health/live` → 漏洞扫描无 HIGH/CRITICAL |

`image` job 存在的理由：前面三个 job 只验证 Python 代码，而真正上线的是**镜像**。
"只构建不启动"会给人"产物已验证"的错觉 —— 少 COPY 一个目录、缺 `libgl1`、
`CMD` 写错，全都**构建成功但起不来**。所以它三步递进，缺一步就有一类盲区。

