# chatAgent 项目结构与文件归类分析

> ## ⚠️ 本文是 2026-09-20 的**历史快照**，不是现状
>
> 全文多处把 `src/graph.py`（以及它的 `init/retrieve/generate/evaluate/route/human`
> 节点、`PostgresSaver` 检查点）当作真源描述。**那些东西已经在 2026-10-20 全部删除**：
>
> | 本文说的 | 现在是什么 |
> |---|---|
> | `src/graph.py`（图 + 节点） | `src/turn.py`（一轮 = 一次 `Runner.run`）+ `src/agents/` 装配 |
> | `src/review_gate.py`（停机容器，单节点图） | 官方 `RunState` + `app/runs.py`（`agent_run_state` 表） |
> | `PostgresSaver` / 检查点续跑 | 官方 `Session` 存跨轮历史；运行现场存 `agent_run_state` |
> | `tests/test_graph.py` / `test_nodes.py` / `test_review_gate.py` / `test_agents_route.py` | 均已删除，覆盖迁到 `test_agents_pipeline.py` / `test_agents_entry.py` / `test_run_state.py` |
>
> 想读现状请看 `README.md`（接口与启动）、`PIPELINE.md`（RAG 链路）与
> `DESIGN_agent_architecture.md`（拓扑取舍）。本文保留只为存档当时的审计结论。

> 生成时间：2026-09-20
> 分析范围：项目内全部文件（不含 `.git/`、`__pycache__/`、`.idea/`、`.pytest_cache/` 等缓存）

---

## 执行记录（2026-09-20 已落地）

下面「结论速览」中的 P0/P1 问题**已全部修复**，目录也已按第三节方案重排。实际变更：

| 原状态 | 现状 |
|--------|------|
| `serve.py` 第 18 行 `import logger`（无模块）→ 启动即崩 | 已删除 `import logger` + 197–296 行未挂载的 `lifespan` 死代码；文件迁为 `app/main.py`，入口改为 `uvicorn app.main:app` |
| notebook 有 `retrieve`/`context`，`src/graph.py` 没有 → 服务无 RAG | 已把 `retrieve` 节点、`context` 字段、更细粒度 `evaluate` 降级并入 `src/graph.py`；检索失败可降级为空上下文 |
| `DB_URL` vs `PG_CHECKPOINT_URI` 命名不一致 → PG 持久化静默失效 | 统一为 `DB_URL`；验证时日志已显示 `PostgresSaver 初始化成功`，PG 持久化真正生效 |
| `rag.py` 与 `rag/` 同名 | `rag.py` 已迁为 `src/retrieval.py`，同名冲突消除 |
| `vectorSaveTest.py` 明文硬编码 API Key | 迁为 `examples/chroma_save_test.py`，密钥改读环境变量；错误导入 `from tools import` 修正为 `from examples import` |
| `docs/intro.txt` 是语料却放在 docs/ | 迁为 `data/intro.txt`；`.env` 的 `DOCS_DIR` 已同步改为 `./data` |
| `verify_client.py` / `.serve.log` / `.verify.out` 临时产物 | 已删除；新增离线接口测试 `tests/test_api.py` |

**测试结果：`pytest tests/ -q` → 26 passed**（原 14 + 新增 8 个 RAG/容错 + 4 个接口用例）。
**新入口验证：`uvicorn app.main:app` 启动后 `/health` 返回 `{"status":"ok"}`。**

> ⚠️ 遗留提示：分析时 8000 端口上仍有一个**旧实例**（PID 3744）在跑迁移前的代码，需要用新命令重启才会生效。

### 增量更新（2026-09-20 之后）

| 变更 | 说明 |
|------|------|
| 新增 `src/pdf_parser.py` | 收编教程 `examples/pdf_split_*.py`，修掉硬编码路径、中文插空格、`punkt_tab` 缺失必崩三个问题 |
| 新增 `tests/test_retrieval.py`、`tests/test_pdf_parser.py` | 检索层与 PDF 解析的离线回归测试（PDF 由测试现场生成） |
| `src/retrieval.py` 新增 `load_chunks()` | 统一语料入口，按扩展名分派：`.txt/.md` 递归切分、`.pdf` 走教程链路 |
| `src/graph.py` 的 `get_retriever()` | 改调 `load_chunks()`，PDF 因此进入 RAG |
| embedding 与 LLM 解耦 | 新增 `EMBEDDING_API_BASE/KEY/MODEL` 三个环境变量，LLM 仍走 `AGNES_*` |
| 修 bug | TF-IDF 中文检索失效（改字符 n-gram）、`build_prompt` 解包错误、`retrieval.py` 缺 `load_dotenv` |

---

## 一、结论速览（问题清单 —— 状态见上方执行记录）

| # | 问题 | 影响 | 优先级 |
|---|------|------|--------|
| 1 | **双真源分叉**：`chatAgent.ipynb` 有 `retrieve` 节点 + `context` 字段，`src/graph.py` 没有 | `serve.py`/`tests` 依赖 `src`，**线上服务实际没有 RAG 能力** | P0 |
| 2 | `serve.py` 第 18 行 `import logger`（项目内无 `logger.py`） | 磁盘上当前 `serve.py` **启动即 ImportError**；8000 端口跑的是旧实例 | P0 |
| 3 | 环境变量命名不一致：notebook 用 `DB_URL`，`serve.py`/`.env.example` 用 `PG_CHECKPOINT_URI` | `.env` 里只有 `DB_URL` → **服务的 PG 持久化静默失效，回退内存**，重启丢状态 | P0 |
| 4 | `rag.py`（模块）与 `rag/`（目录）**同名** | 实测 `rag` 解析到 `rag.py`，暂不报错；但一旦给 `rag/` 加 `__init__.py` 就会静默切换，属隐患 | P1 |
| 5 | `rag/` 是教程原型：**明文硬编码 API Key**、`from tools import` 路径错误、硬编码不存在的 PDF 路径 | 密钥泄露风险；脚本跑不起来；与 `rag.py`/`serve.py` 功能重复 | P1 |
| 6 | `docs/` 名不副实：里面 `intro.txt` 是 RAG **语料**不是文档 | 目录语义混乱，后续放 Markdown 文档没地方 | P2 |

---

## 二、全部文件逐个分析

### A. 生产代码链路（核心）

| 文件 | 行数 | 作用 | 状态 / 问题 |
|------|------|------|-------------|
| `src/graph.py` | 215 | 图逻辑**真源**：`build_graph(model, checkpointer, tools)`。节点 init/generate/evaluate/tool_node/request_review；`route_review` 阈值 LOW=60/HIGH=80、iteration>=10 封顶；`request_review` **只写 `review_requested` 标记**（2026-10-20 起不再 `interrupt`，停机在 `src/review_gate.py`） | ✅ 质量最好、可注入、测试友好。❌ **缺 `retrieve` 节点与 `context` 字段**，落后于 notebook |
| `src/__init__.py` | 0 | 使 `src` 成为包 | ✅ 正常 |
| `serve.py` | 296 | FastAPI 服务：`/health`、`/chat`(SSE 流式)、`/review`(人工审核 resume) | ⚠️ **前半段(1–196)已验证可用**；🔴 **后半段(197–296)断裂**：`import logger` 无模块、`lifespan` 未挂载到 `app`、`API_TYPE`/`CHROMADB_DIRECTORY` 等全局从未赋值、与 `rag.py` 重复实现 → 属死代码 |
| `chatAgent.ipynb` | 388 | 原型笔记本。**含 `retrieve` 节点**（`get_retriever()` 懒加载单例 + `DOCS_DIR`）、`OverAllState.context`、`except (ValidationError, OutputParserException)` 更细粒度降级、`PostgresSaver(psycopg.connect(DB_URL))` | ⚠️ **逻辑最全，已与 `src/graph.py` 分叉**。另有两处小瑕疵：`generate` 首轮把整个 `messages` 列表拼进 prompt（应为最后一条的 content）；f-string 内嵌同种引号依赖 Python≥3.12 |

### B. RAG 相关（两套并存，未统一）

| 文件 | 行数 | 作用 | 状态 / 问题 |
|------|------|------|-------------|
| `rag.py` | 187 | **独立 TF-IDF RAG**：`load_documents`/`split_documents`/`build_retriever`/`answer` + CLI。被 notebook 以 `from rag import ...` 引用 | ✅ 自洽可用、无向量库依赖。❌ 未被 `serve.py`/`src/graph.py` 调用；与 `rag/` 同名 |
| `rag/pdfSplitTest_Ch.py` | 115 | 中文 PDF 切分（pdfminer.six + 正则断句 + 交叠切块） | ⚠️ 教程原型：硬编码 `../input/健康档案.pdf`（不存在） |
| `rag/pdfSplitTest_En.py` | 118 | 英文 PDF 切分（nltk sent_tokenize） | ⚠️ 教程原型：硬编码 `../input/llama2.pdf`、`nltk.data.path.append('../other/punkt_tab')` |
| `rag/vectorSaveTest.py` | 181 | Chroma 灌库原型（`MyVectorDBConnector` add/search） | 🔴 **明文硬编码 API Key（第 27、31 行）** ＋ `from tools import ...` 路径错误（文件在 `rag/` 下）＋ 引用不存在的 `input/健康档案.pdf` |
| `rag/prompt_template.txt` | 13 | 健康档案问答模板（`{context}`/`{query}`），给 `serve.py` 后半段用 | ⚠️ 仅服务于已断裂的 RAG 原型；与 `rag.py` 的 `build_prompt` 重复 |

### C. 测试

| 文件 | 行数 | 作用 | 状态 |
|------|------|------|------|
| `tests/__init__.py` | 0 | 使 tests 成为包 | ✅ |
| `tests/conftest.py` | 30 | 注入项目根到 `sys.path`；`mock_model`（MagicMock 假模型）+ `graph` fixture | ✅ 不依赖真实 API |
| `tests/test_nodes.py` | 81 | 节点/路由单测：`route_review` 6 分支、`after_human` 2 分支、`evaluate` 成功/降级、`generate` 首轮 | ✅ 14 用例全通过。⚠️ `make_state` 无 `context` 字段，补 `retrieve` 后需同步更新 |
| `tests/test_graph.py` | 54 | 整图集成：人工回路 + resume accept、高分结束、降级不崩 | ✅ |
| `verify_client.py` | 82 | 本次用于验证 `/chat`、`/review` 的**临时** SSE 客户端（纯标准库，避开 git-bash 中文编码） | ⚠️ 临时产物，建议删或转成 `tests/test_api.py` |

### D. 语料 / 数据

| 文件 | 行数 | 作用 | 状态 |
|------|------|------|------|
| `docs/intro.txt` | 5 | 梧州介绍，RAG 的示例语料 | ⚠️ 是**数据**不是文档，放 `docs/` 语义不符 |

### E. 配置 / 依赖

| 文件 | 作用 | 状态 / 问题 |
|------|------|-------------|
| `requirements.txt` | 13 行依赖：langgraph / checkpoint-postgres / langchain-openai / psycopg / pydantic / fastapi / uvicorn / sse-starlette / scikit-learn / numpy | ⚠️ 后半段 RAG 原型用的 `langchain-chroma`、`chromadb`、`pdfminer.six`、`nltk`、`langchain-tavily` **未列入**；若不删原型则缺依赖 |
| `.env` | 真实配置。键：`AGNES_API_KEY`/`AGNES_API_BASE`/`AGNES_MODEL`/`DB_URL`/`DOCS_DIR`/`DEEPSEEK_API_KEY`/`DEEPSEEK_API_BASE`/`TAVILY_API_KEY` | ⚠️ `DB_URL` 与 `serve.py` 的 `PG_CHECKPOINT_URI` 不一致；`DEEPSEEK_*`/`TAVILY_API_KEY` 当前代码未使用 |
| `.env.example` | 模板：`AGNES_*`/`PG_CHECKPOINT_URI`/`SERVE_API_KEY`/`RAG_EMBEDDING` | ⚠️ **缺 `DOCS_DIR`**（notebook 需要） |
| `.gitignore` | Python/venv/Jupyter/IDE/`*.db`/`.env`/日志/测试缓存/`.workbuddy/` | ✅ 完善；⚠️ 未忽略 `.verify.out` |

### F. 临时产物与缓存（可清理）

| 路径 | 说明 |
|------|------|
| `.serve.log` | 本次启动 uvicorn 的日志（已被 `*.log` 忽略） |
| `.verify.out` | 本次验证输出（**未被忽略**） |
| `__pycache__/`、`.pytest_cache/`、`.idea/` | 缓存与 IDE，已被忽略 |
| `.workbuddy/` | 项目记忆（`memory/2026-08-19.md`、`2026-08-25.md`、`2026-09-20.md`）——**保留，勿删** |

---

## 三、推荐目录结构

```
chatAgent/
├─ README.md                     # 【新增】项目说明 + 启动方式
├─ requirements.txt
├─ .gitignore  .env  .env.example
│
├─ src/                          # 生产代码（唯一真源）
│  ├─ __init__.py
│  ├─ config.py                  # 【新增】集中读环境变量，消除 DB_URL/PG_* 歧义
│  ├─ graph.py                   # 图与节点（补 retrieve 节点 + context 字段）
│  ├─ retrieval.py               # 由根目录 rag.py 迁入（RAG 检索库）
│  └─ llm.py                     # 【可选】ChatOpenAI 工厂，notebook/serve 共用
│
├─ app/                          # 服务层
│  ├─ __init__.py
│  └─ main.py                    # 由 serve.py 迁入；只保留 /chat /review /health
│
├─ tests/
│  ├─ __init__.py
│  ├─ conftest.py
│  ├─ test_nodes.py
│  ├─ test_graph.py
│  └─ test_api.py                # 【新增】接口级测试（原 verify_client.py 进化版）
│
├─ data/                         # 知识库语料（RAG 输入）
│  └─ intro.txt                  # 由 docs/intro.txt 迁入
│
├─ notebooks/                    # 探索/原型
│  └─ chatAgent.ipynb            # 由根目录迁入，标注「不参与生产」
│
├─ examples/                     # 教程/一次性脚本（不参与生产、不部署）
│  ├─ pdf_split_zh.py            # 原 rag/pdfSplitTest_Ch.py
│  ├─ pdf_split_en.py            # 原 rag/pdfSplitTest_En.py
│  ├─ chroma_save_test.py        # 原 rag/vectorSaveTest.py（先删明文 Key）
│  └─ prompts/qa_template.txt    # 原 rag/prompt_template.txt
│
├─ deploy/                       # 【可选】部署
│  ├─ Dockerfile
│  ├─ docker-compose.yml
│  └─ init_pg.sql                # 建库建用户 + GRANT CREATE
│
└─ .workbuddy/                   # 工具数据（保留）
```

### 归类理由

| 目标目录 | 装什么 | 为什么 |
|----------|--------|--------|
| `src/` | 被部署、被测试导入的生产逻辑 | 只放「上线要跑的代码」；`config.py` 把散落的 `os.getenv` 收口，顺手解决 `DB_URL`/`PG_CHECKPOINT_URI` 歧义 |
| `app/` | Web 服务入口 | 与「业务逻辑(src)」分离：换 Web 框架只动 `app/`，换逻辑只动 `src/` |
| `tests/` | 所有 `test_*.py` + `conftest.py` | pytest 默认发现规则即 `test_*.py`；`verify_client.py` 升格为 `test_api.py` 后就能进 CI |
| `data/` | RAG 语料（txt/md/pdf）、未来向量库目录 | 数据与代码分离；`docs/` 留给 Markdown 文档，名实相符 |
| `notebooks/` | `.ipynb` 探索稿 | 笔记本是原型，混在根目录会让人误以为它是入口 |
| `examples/` | 教程代码、一次性脚本 | 明确「不部署、不测试、可随时删」，避免教程残留污染生产目录 |
| `deploy/` | Dockerfile、compose、初始化 SQL | 部署产物集中，CI/CD 好找 |
| 根目录 | README、requirements、`.env*`、`.gitignore` | 通用约定，工具默认按根目录找 |

### 迁移映射表（当前 → 目标）

| 当前路径 | 目标路径 | 备注 |
|----------|----------|------|
| `rag.py` | `src/retrieval.py` | 改名即消除与 `rag/` 的同名隐患 |
| `serve.py` | `app/main.py` | 迁入时**删掉 197–296 行**；`from src.graph import` 保持 |
| `chatAgent.ipynb` | `notebooks/chatAgent.ipynb` | 导入语句 `from rag import ...` → `from src.retrieval import ...` |
| `docs/intro.txt` | `data/intro.txt` | `.env` 的 `DOCS_DIR` 指向 `data` |
| `rag/pdfSplitTest_Ch.py` | `examples/pdf_split_zh.py` | 修硬编码 PDF 路径（或保留为纯示例） |
| `rag/pdfSplitTest_En.py` | `examples/pdf_split_en.py` | 同上 |
| `rag/vectorSaveTest.py` | `examples/chroma_save_test.py` | **先删除第 27、31 行明文 Key**，改读环境变量 |
| `rag/prompt_template.txt` | `examples/prompts/qa_template.txt` | 与 `rag.py:build_prompt` 去重后二选一 |
| `verify_client.py` | `tests/test_api.py` | 或直接删除 |
| `.serve.log` / `.verify.out` | 删除 | 临时产物 |
| — | `src/config.py` | 新增，统一配置 |
| — | `README.md` | 新增 |

---

## 四、动手顺序建议

1. **先止血（P0）**：删 `serve.py` 第 18 行 `import logger` 与 197–296 行死代码 → `serve.py` 恢复可启动；
2. **统一配置（P0）**：`.env` 补 `PG_CHECKPOINT_URI`（或把 `serve.py` 改读 `DB_URL`），`.env.example` 补 `DOCS_DIR`；
3. **定真源（P0）**：把 notebook 的 `retrieve` 节点 + `context` 字段搬进 `src/graph.py`，同步更新 `tests/test_nodes.py` 的 `make_state`；
4. **消隐患（P1）**：删 `rag/vectorSaveTest.py` 明文 Key；`rag.py` → `src/retrieval.py`；
5. **搬家（P2）**：按上面映射表落到目标目录，补 `README.md`，跑一遍 `pytest` 验收。

> 搬家阶段建议**先动结构、后改逻辑**，每步跑一次 `pytest`，避免重构与改错混在一起难定位。
