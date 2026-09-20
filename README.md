# chatAgent

基于 LangGraph 的内容生成 Agent：生成 → 自动评分 → 低分重生成 / 人工审核 → 结束，
并提供 FastAPI 流式服务（SSE）与基于 TF-IDF 的轻量 RAG。

## 目录结构

```
chatAgent/
├─ src/                  生产代码（唯一真源）
│  ├─ graph.py           图与节点：init → retrieve → generate → evaluate → route → human
│  └─ retrieval.py       RAG 检索库（原 rag.py）：加载/切分/TF-IDF 检索
├─ app/                  服务层
│  └─ main.py            FastAPI 入口（原 serve.py）：/health /chat /review
├─ tests/                单元 + 整图 + 接口测试
├─ data/                 知识库语料（RAG 输入）
├─ notebooks/            原型笔记本（探索用，逻辑以 src/ 为准）
├─ examples/             教程原型脚本（不参与生产）
├─ requirements.txt
└─ .env / .env.example
```

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

| 变量 | 必填 | 说明 |
|------|------|------|
| `AGNES_API_KEY` | 是 | 模型 API Key |
| `AGNES_API_BASE` | 否 | 默认 `https://apihub.agnes-ai.com/v1` |
| `AGNES_MODEL` | 否 | 默认 `agnes-2.0-flash` |
| `DB_URL` | 否 | PG 检查点连接串；留空则用内存（重启丢状态） |
| `DOCS_DIR` | 否 | RAG 语料目录，默认 `data` |
| `SERVE_API_KEY` | 否 | 非空则开启接口鉴权 |
| `RAG_EMBEDDING` | 否 | `tfidf`（默认）或 `openai` |

## 说明

- `examples/` 是教程原型（PDF 切分、Chroma 灌库），依赖 `pdfminer.six` / `nltk` / `chromadb` / `openai`，
  需要时按 `requirements.txt` 末尾的注释单独安装。
- 修改图逻辑请只改 `src/graph.py`；`notebooks/chatAgent.ipynb` 仅作探索，不参与生产。
