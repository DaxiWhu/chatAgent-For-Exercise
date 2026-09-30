# chatAgent

基于 LangGraph 的内容生成 Agent：生成 → 自动评分 → 低分重生成 / 人工审核 → 结束，
并提供 FastAPI 流式服务（SSE）与基于 TF-IDF 的轻量 RAG。

## 目录结构

```
chatAgent/
├─ src/                  生产代码（唯一真源）
│  ├─ graph.py           Agent 编排层：init → retrieve → generate → evaluate → route → human
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

> **为什么用 `python -m app.serve` 而不是裸 `uvicorn`**：检查点持久化用的是
> `AsyncPostgresSaver`，它底层是 psycopg 的**异步**连接，而 psycopg 异步**不支持
> Windows 的 ProactorEventLoop** —— uvicorn 在 win32 上恰好默认用它（且 0.36+ 直接
> 指定 loop_factory，`set_event_loop_policy` 无效）。`app/serve.py` 注入了一个
> 平台自适应的事件循环工厂（win32→Selector，Linux→uvloop/Selector），
> 让持久化在两种平台都能生效。**不这么做时服务仍能起，但会静默降级为内存检查点
> （重启丢状态）**，`/health/ready` 的 `checkpointer` 项会显示 `<...>Saver（内存…）`。
>
> 入口是 `app.main:app`（旧命令 `uvicorn serve:app` 已随文件迁移失效）。

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
| 起本地栈 / 含监控 / 停 | `tasks up` / `tasks up-observability` / `tasks down` |
| **端到端自检**（真起栈） | `tasks e2e`（SSE 分帧 / 灰度权重 / 持久化检查点；`--keep` 保留现场） |
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
| POST | `/v1/chat` | SSE 流式对话。事件：`token` 增量文本、`citations` 引用、`need_review` 需人工审核、`budget_exceeded` 超 token 预算、`timeout` 超请求总时限（终止性，其后无 `done`）、`done` 结束、`error` 异常 |
| POST | `/v1/chat/resume` | **续跑中途断掉的会话**（SSE，事件同 `/v1/chat`）。body：`{thread_id}`。待人工审核时返回 409 并指路 `/v1/review` |
| GET | `/v1/sessions/{thread_id}` | **会话状态快照**（断点续传的基础）。纯读、幂等，返回 `status` / `resumable` / `draft` / `messages` / `citations` / `pending_review` |
| POST | `/v1/review` | 人工审核 resume。body：`{thread_id, action: "accept"\|"revise", feedback}`。非审核态返回 409 |
| GET | `/v1/reviews` | 待人工审核队列（分页列举，含草稿摘要与评分） |
| GET | `/health` | 兼容旧路径，等价于 `/health/live`，恒返回 `{"status":"ok"}` |
| GET | `/health/live` | 存活探针：只反映本进程，不查外部依赖 |
| GET | `/health/ready` | 就绪探针：真探 LLM 配置 / 数据库 / 向量扩展 / 检查点类型；不健康回 503 |
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
- 需要鉴权的路由必须声明 `X-API-Key` / `X-Tenant-Id` 头；
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
该测试同时用 AST 断言提示词**没有回流**到 `src/graph.py` 的代码里。

### 断点续传

SSE 一断（弱网、切后台、代理超时），客户端手里往往只剩"已经打到屏幕上的那半句"。
但**状态其实一直在检查点里** —— 每个节点边界都会落一次，`aget_state()` 随时能取回
完整对话、最新草稿、评分与引用。这两个端点就是把已有事实暴露出来：

```text
① SSE 断了
   └─> GET /v1/sessions/{thread_id}
         status=incomplete  → resumable=true  → POST /v1/chat/resume   （机器能自己跑完）
         status=awaiting_review            → POST /v1/review           （需要人的决定）
         status=completed                  → 结束了，取 draft/messages 渲染即可
```

`status` 由 `state.next`（未执行完的节点）推导：

| `next` | status | 客户端该做什么 |
|---|---|---|
| 含 `human_review` | `awaiting_review` | 弹审核界面，调 `/v1/review`；`pending_review` 里有草稿与评分 |
| 非空、不含 `human_review` | `incomplete` | 提示"继续生成"，调 `/v1/chat/resume` |
| 空 | `completed` | 直接渲染 `draft` / `messages` / `citations` |

> **续跑的语义是"重新执行未完成的那个节点"**，不是"接上上次的半个 token"。
> 实测确认：流被取消后检查点停在**该节点之前**（`next` 里留着它），所以续跑会
> 从头把这次回答重新流一遍，客户端拿到的是完整自洽的正文。代价是**再花一次该节点的
> token**（前面已完成的检索与多轮迭代不会重跑）。

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

### 鉴权与租户（三档，按配置自动选择）

请求头：`X-API-Key` 带密钥，`X-Tenant-Id`（可选）声明租户。

| 配置 | 身份模式 | 租户怎么定 | 适用 |
|------|----------|-----------|------|
| `TENANT_KEYS=acme:k1,globex:k2` | `tenant-key` | 由 Key **反查**，请求头只用于核对（不一致 **403**） | 生产多租户（推荐） |
| 只配 `SERVE_API_KEY` | `global-key` | 仍由请求头**自报** ⇒ 启动时告警 | 单租户 / 兼容 |
| 都不配 | `anonymous` | 请求头或 `RAG_TENANT` | 仅本地开发 |

> **为什么多租户必须用 `TENANT_KEYS`**：只有"一个全局 Key + 请求头"时，租户是客户端
> **自报**的 —— 任何持 Key 者改个 header 就是"另一个租户"：日配额重新计算、
> 审核队列归属失真、配合可猜的 `thread_id` 还能串读别人的会话。
> 配了 `TENANT_KEYS` 后租户由 Key 反查得出，请求头只能"确认"不能"声明"。
> 会话归属由 `app/sessions.py` 登记属主（仅多租户模式启用）。

`APP_ENV=production` 会推导出两个收紧默认：`REQUIRE_AUTH=1`（**未配任何密钥就拒绝启动**）、
`ENABLE_DOCS=0`（`/docs` `/redoc` `/openapi.json` 全 404）。

## 状态机

```
START → init → retrieve → generate ─┬─(有 tool_calls)→ tool_node → generate
                                    └─(无)→ evaluate → route_review
route_review → END（高分 >=80 / 不需评审 / 超过 10 轮 / 降级）
             → generate（60~79，自动重生成）
             → human_review（<60，interrupt 等人工 accept / revise）
human_review → END（accept）| generate（revise，带人工意见重写）
```

关键容错：
- `evaluate` 连续失败 5 次 → `force_exit` 降级为固定报错文案，图不崩；
- `retrieve` 未配置知识库或检索异常 → 降级为空上下文，主流程照常；
- 人工输入非法（action 越界 / revise 无意见）→ 重新 `interrupt` 让人补填。

## 环境变量

**LLM 与 embedding 是两套独立配置**：模型生成走 `AGNES_*`，RAG 检索的 embedding 走 `EMBEDDING_*`
（因为 agnes 后端不提供 `/v1/embeddings` 接口，必须另指一个服务）。

**模型与存储**

| 变量 | 必填 | 说明 |
|------|------|------|
| `AGNES_API_KEY` | 是 | **LLM** API Key（生成用） |
| `AGNES_API_BASE` | 否 | 默认 `https://apihub.agnes-ai.com/v1` |
| `AGNES_MODEL` | 否 | 默认 `agnes-2.0-flash` |
| `DB_URL` | 否 | PG 连接串。**检查点 + pgvector + 审核队列 + 会话归属共用**；留空则检查点用内存（重启丢状态） |
| `DOCS_DIR` | 否 | RAG 语料目录，默认 `data` |

**服务与安全**（这一组配错多半**不报错**，只是静默不生效 —— 核对方法见「部署」段）

| 变量 | 必填 | 说明 |
|------|------|------|
| `APP_ENV` | 否 | `development`（默认）/ `production`。生产会推导出 `REQUIRE_AUTH=1`、`ENABLE_DOCS=0` |
| `REQUIRE_AUTH` | 否 | 默认跟随 `APP_ENV`。开启时**未配任何密钥就拒绝启动**（拦"裸奔上线"） |
| `SERVE_API_KEY` | 否 | 单一全局 Key，非空则开启鉴权。⚠️ **留空 = 接口完全裸奔** |
| `TENANT_KEYS` | 否 | `租户:密钥[,...]`。把租户**绑定到 Key**（生产多租户必配，见上文） |
| `ENABLE_DOCS` | 否 | 默认开发开、生产关。关时 `/docs` `/redoc` `/openapi.json` 全部 404 |
| `CORS_ALLOW_ORIGINS` | 否 | 逗号分隔白名单，默认 `*`（`allow_credentials` 恒 false，不涉及 Cookie） |
| `TRUST_PROXY_HEADERS` | 否 | 默认 `0`。反代后设 `1` 才按 `X-Forwarded-For` 第一段计限流；**直连时设 1 可被伪造绕过限流** |
| `APP_WORKERS` / `WEB_CONCURRENCY` | 否 | worker 数（前者优先，默认 `1`）。多 worker 各自持有检索器与限流计数 ⇒ 配了 `REDIS_URL` 再上 |
| `REDIS_URL` | 否 | 限流/配额共享后端（`redis` 已是硬依赖）。多副本必填，**不填 = 额度 × 副本数** |
| `MAX_REQUEST_BYTES` | 否 | 请求体字节上限，默认 `1048576`（1 MiB）。超限 **413**，在**读之前**就拒（防内存打爆） |
| `MAX_MESSAGE_CHARS` | 否 | `/v1/chat` 的 `message` 长度上限，默认 `100000`。超限 **422** 并点名字段 |
| `MAX_FEEDBACK_CHARS` | 否 | `/v1/review` 的 `feedback` 上限，默认 `20000` |
| `MAX_THREAD_ID_CHARS` | 否 | `thread_id` 上限，默认 `128`（它同时是检查点主键、限流桶键、归属表主键） |
| `RATE_LIMIT_ENABLED` / `RATE_LIMIT_BY_SESSION` | 否 | 两个限流维度各自的开关，默认都开 |
| `RATE_LIMIT_PER_MINUTE` / `RATE_LIMIT_SESSION_PER_MINUTE` | 否 | 按 IP、按会话的每分钟上限，默认 `120` |
| `LLM_MAX_TOKENS_PER_REQUEST` | 否 | 单请求 token 预算，超限**停止继续生成**（已产出的照常返回）。`0`=不限 |
| `USER_DAILY_TOKEN_QUOTA` | 否 | 每租户每日 token 上限，超了先拦 429。`0`=不限 |
| `REQUEST_TIMEOUT_SECONDS` | 否 | **请求级总时限**（秒），超时下发 `timeout` 事件后收流（已产出的照常返回）。`0`=不限。⚠️ LLM client 的 `timeout` 只管单次调用，多轮 self-review 的总时长靠它兜底 |
| `RAG_GUARD` / `RAG_GUARD_ACTION` | 否 | 提示词注入防护，`warn`（默认）/ `strip` / `block` |
| `RAG_PARSE_CACHE` | 否 | 文档级解析结果落盘缓存，默认 `1`（语料没变就跳过重新解析，docling/OCR 是分钟级） |
| `RAG_BREAKER` / `CIRCUIT_FAIL_THRESHOLD` / `CIRCUIT_RESET_SECONDS` | 否 | 出站调用（rerank/embedding）熔断，默认 `1` / `5` / `30` |
| `METRICS_ENABLED` | 否 | `/metrics` 开关，默认开 |
| `LOG_FORMAT` / `LOG_LEVEL` | 否 | `json`（默认，生产）/ `text`（本地人眼友好）；级别默认 `INFO` |
| `HEALTH_READY_TIMEOUT` | 否 | 就绪探针探依赖的超时（秒），默认 `3` |
| ~~`DISCONNECT_POLL_INTERVAL`~~ | — | **已废弃**。断连检测改为 sse_starlette 的 `client_close_handler_callable` 回调（事件驱动，无轮询间隔可调） |
| `LANGFUSE_*` / `LANGSMITH_API_KEY` / `LANGCHAIN_PROJECT` | 否 | 链路追踪，两组都留空 = 不接（行为与没有这层代码一致） |
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
| `RAG_TENANT` | 否 | 兜底租户，**仅在单租户模式生效**。多租户请用 `TENANT_KEYS`，别靠它 |

> `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` 没填满时，`RAG_EMBEDDING=semantic|hybrid` 会**自动回退 TF-IDF**
> 并打一条 WARNING，服务不会因配置缺失而崩。

## 部署

> **需要你填的配置项集中在 `PRODUCTION_CONFIG.md`** —— 那份清单逐项写了"不填的后果"
> 与建议值，上线时对着它勾。本段只讲步骤。

### 1) 生产必填

```bash
APP_ENV=production                     # 推导出 REQUIRE_AUTH=1 + 关闭 API 文档
TENANT_KEYS=acme:<key>,globex:<key>    # 多租户：租户**绑定到 Key**（单租户可改用 SERVE_API_KEY）
DB_URL=postgresql://user:pwd@host:5432/db
AGNES_API_KEY=...                      # LLM（生成）
EMBEDDING_API_BASE/KEY/MODEL=...       # 检索 embedding，与 LLM 分开配
```

`APP_ENV=production` 且**没有任何 Key** 时服务会**拒绝启动**（刻意的：宁可起不来，
也不要以无鉴权状态接管线上流量）。

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

# 就绪探针真探依赖（llm / db / vector_store / checkpointer 四项，各自带耗时）
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
curl -s localhost:8000/health/ready | python -m json.tool      # llm / db / vector_store / checkpointer 四项
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/docs    # 生产应为 404
python -m scripts.load_test --requests 200 --concurrency 20     # 默认打 /health/live，不花 token
python -m scripts.load_test --endpoint chat --yes-spend --disconnect-after 3   # 断连止损验证
```

监控告警规则在 `deploy/monitoring/alerts.yml`（可用性 / SLO / 成本 / 安全四组），
路由与接收人在同目录 `alertmanager.yml`，SLO 的**定义**（SLI 口径、目标、燃尽率表、
复核节奏）在 `SLO.md` —— 文档与表达式由测试逐行对齐，改一处会立刻测红。

## 检索质量评测

```bash
python -m scripts.eval_retrieval              # 完整对比（每个组合会逐条发请求，较慢）
python -m scripts.eval_quick                  # 快速版：批量算查询向量，秒级返回
python -m scripts.eval_retrieval --show-miss  # 打印未命中明细，便于定位问题
```

评测集在 `scripts/eval_set.json`，把 `documents` 换成你自己的语料、`queries` 换成你真会问的问题即可。
**务必保留 `type` 字段**：`literal`（照抄原文词）与 `paraphrase`（换种说法）必须分开统计——
混在一起会得出「关键词就够用」的错误结论。

实测参考（28 条中文查询 / 6 篇共 17 个片段，bge-m3）：

| 后端 | 整体 Top1 | 照抄 Top1 | 改写 Top1 |
|------|-----------|-----------|-----------|
| tfidf | **75%** | **100%** | 42% |
| hybrid + 前缀 | 61% | 75% | 42% |
| semantic + 前缀 | 39% | 50% | 25% |

结论：**小语料、查询多照抄原文词时关键词最强**；把切分从「递归 500」改成「结构感知 300」后，
tfidf 照抄 Top1 由 88% → 100%。语义检索的价值要在文档变长、说法差异大时才体现出来。

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
- 修改图逻辑请只改 `src/graph.py`；`notebooks/chatAgent.ipynb` 仅作探索，不参与生产。

### CI 门禁（`.github/workflows/ci.yml`）

| job | 触发 | 守住什么 |
|-----|------|---------|
| `test` | 每次 push / PR | 全量**离线**用例（刻意不装 docling/transformers）+ 依赖锁定静态一致性 |
| `eval-gate` | 每次 push / PR | 检索基线（tfidf Top1 ≥ 0.55）—— 改 prompt / 换模型 / 调切分后召回率不许悄悄掉 |
| `full-deps` | 每日定时 / 手动 | 装齐 docling/transformers 跑全量：**补上 `test` 刻意跳过的那批解析用例**（否则"本地绿 ≠ CI 绿"） |
| `image` | 每次 push / PR | **部署产物**：镜像能构建 → 容器能起来并通过 `/health/live` → 漏洞扫描无 HIGH/CRITICAL |

`image` job 存在的理由：前面三个 job 只验证 Python 代码，而真正上线的是**镜像**。
"只构建不启动"会给人"产物已验证"的错觉 —— 少 COPY 一个目录、缺 `libgl1`、
`CMD` 写错，全都**构建成功但起不来**。所以它三步递进，缺一步就有一类盲区。

