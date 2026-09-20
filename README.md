# chatAgent

基于 LangGraph 的内容生成 Agent：生成 → 自动评分 → 低分重生成 / 人工审核 → 结束，
并提供 FastAPI 流式服务（SSE）与基于 TF-IDF 的轻量 RAG。

## 目录结构

```
chatAgent/
├─ src/                  生产代码（唯一真源）
│  ├─ graph.py           图与节点：init → retrieve → generate → evaluate → route → human
│  ├─ retrieval.py       RAG 检索库（原 rag.py）：语料装载 + TF-IDF / embedding 检索
│  ├─ loaders.py         多格式解析：txt/md/csv/tsv/json/jsonl/html/docx/xlsx/pptx
│  └─ pdf_parser.py      PDF 解析（收编自教程）：pdfminer 提文本 + 段落重组 + 交叠切块
├─ app/                  服务层
│  └─ main.py            FastAPI 入口（原 serve.py）：/health /chat /review
├─ tests/                单元 + 整图 + 接口 + 检索/解析测试
├─ data/                 知识库语料（多格式）
├─ notebooks/            原型笔记本（探索用，逻辑以 src/ 为准）
├─ examples/             教程原型脚本（不参与生产）
├─ requirements.txt
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
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 4) 跑测试
pytest tests/ -q
```

> 注意：入口是 `app.main:app`（旧命令 `uvicorn serve:app` 已随文件迁移失效）。

## 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查，返回 `{"status":"ok"}` |
| POST | `/chat` | SSE 流式对话。事件：`token` 增量文本、`need_review` 需人工审核、`done` 结束、`error` 异常 |
| POST | `/review` | 人工审核 resume。body：`{thread_id, action: "accept"\|"revise", feedback}`。非审核态返回 409 |

`/chat` 请求体：`{"thread_id": "会话ID", "message": "用户输入"}`

若设置了 `SERVE_API_KEY`，所有请求需带请求头 `X-API-Key`。

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

| 变量 | 必填 | 说明 |
|------|------|------|
| `AGNES_API_KEY` | 是 | **LLM** API Key（生成用） |
| `AGNES_API_BASE` | 否 | 默认 `https://apihub.agnes-ai.com/v1` |
| `AGNES_MODEL` | 否 | 默认 `agnes-2.0-flash` |
| `DB_URL` | 否 | PG 检查点连接串；留空则用内存（重启丢状态） |
| `DOCS_DIR` | 否 | RAG 语料目录，默认 `data` |
| `SERVE_API_KEY` | 否 | 非空则开启接口鉴权 |
| `EMBEDDING_API_BASE` | 否 | **embedding** 服务地址（OpenAI 兼容，填到 `/v1` 即可；多填 `/embeddings` 会被自动裁掉） |
| `EMBEDDING_API_KEY` | 否 | embedding 服务 Key |
| `EMBEDDING_MODEL` | 否 | embedding 模型名 |
| `EMBEDDING_BATCH` | 否 | 单次请求条数，默认 `10`（各家上限不同：百炼约 10，硅基流动可更大） |
| `EMBEDDING_QUERY_PREFIX` | 否 | 查询侧指令前缀，默认空。BGE 系模型对此敏感（实测 bge-m3：无前缀 38% → 英文指令 62%） |
| `RAG_EMBEDDING` | 否 | 检索后端：`tfidf`（纯本地关键词）/ `semantic`（embedding 向量）/ `hybrid`（双路 RRF 融合） |
| `RAG_CHUNK_STRATEGY` | 否 | `headings`（默认，按 Markdown 标题切且标题路径入块）/ `recursive` |
| `RAG_CHUNK_SIZE` / `RAG_CHUNK_OVERLAP` | 否 | 块大小（默认 300 字符）与重叠（默认 50） |
| `RAG_HYBRID_POOL` | 否 | hybrid 每一路各取多少条候选送入融合，默认 10 |
| `RERANK_MODEL` | 否 | 填了就在 hybrid 上挂一层精排（失败自动回退到融合顺序）；留空则关闭 |

> `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` 没填满时，`RAG_EMBEDDING=semantic|hybrid` 会**自动回退 TF-IDF**
> 并打一条 WARNING，服务不会因配置缺失而崩。

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

- **语料支持 `.txt` / `.md` / `.pdf`**：纯文本走 `RecursiveCharacterTextSplitter`；
  PDF 走 `src/pdf_parser.py`（pdfminer 提文本 → 短行断段 → 句子级交叠切块，收编自教程并清理）。
  单个 PDF 解析失败只跳过该文件并告警，不影响其余语料。
- 检索默认用 TF-IDF **字符 n-gram**（不是默认的 word 分词）——中文没有空格，word 分词会把整句
  当成一个 token 导致 0 命中，改用字符 n-gram 后中英文都能正常召回。
- 英文断句优先用 `nltk`；未安装或缺 punkt 数据时**自动回退标点正则**，不会崩。
- `examples/` 是教程原型（PDF 切分、Chroma 灌库），依赖 `chromadb` / `openai`，
  需要时按 `requirements.txt` 末尾的注释单独安装。
- 修改图逻辑请只改 `src/graph.py`；`notebooks/chatAgent.ipynb` 仅作探索，不参与生产。
