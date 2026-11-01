# 解析 → 切分 → 向量化：全流程与技术栈

> 本文是**总览文档**：每条链路的"用了什么、为什么这么选、代价是什么、在哪验证"。
> 细节与实测数据在 `PLAN_doc_parsing_v3.md`（各格式分流与实测缺陷）与 `ARCHITECTURE.md`（终态蓝图）。
> 所有版本号为本机实测（2026-09-22，`D:\miniConda\envs\langchain1.2`）。

---

## 0 一页总览

```
文件
 │
 ├─ ① 分流（按容器格式）───────────────────────────── src/rag/loaders.py · src/rag/retrieval.py
 │    .pdf / .docx → Docling 双后端        csv/tsv/xlsx/pptx/json/jsonl/html/md/txt → load_blocks()
 │    图片 → 有图无文的块 → VLM/OCR        .doc/.xls/.ppt → 不支持（需 LibreOffice）
 │
 ├─ ② 解析（每格式的确定性抽取）────────────────────── src/rag/docling_loader.py · src/rag/docx_media.py · src/rag/loaders.py
 │    PDF：版面/表格/OCR/公式 + 图片裁剪 + 原页位图
 │    Word：Docling 正文（A）+ 解 zip 取图片/OLE/图表 XML（C）
 │    其它：各自的结构（表头/工作表/幻灯片/记录/标题/代码块）
 │
 ├─ ③ 统一产物 KnowledgeChunk（26 字段，双轨：content 保真 / retrieval_text 面向检索）── src/rag/knowledge_chunk.py
 │
 ├─ ④ 切分 ──────────────────────────────────────── src/rag/retrieval.py
 │    docling 侧：HybridChunker + 四级兜底阶梯（保证"有内容 → 一定有块"）
 │    其它格式：结构块 + 超大块二次切分（section/page 跟着走）
 │    再叠加：父窗（small-to-big）
 │
 ├─ ⑤ 切分 → 向量化之间的处理（9 步，见 §5）────────── src/rag/chunk_writer.py · src/rag/contextualizer.py
 │
 ├─ ⑥ 向量化与索引 ──────────────────────────────── src/rag/retrieval.py
 │    TfidfRetriever（字符 n-gram）| EmbeddingRetriever（bge-m3）| HybridRetriever（RRF 融合）
 │
 └─ ⑦ 检索 → 生成 → 引用（含原页返图）────────────── src/agents/tools.py · src/turn.py · app/main.py
      citations：来源 / 页码 / 章节 / 坐标 / chunk_id / 保真节选 / 表格网格 / 原页截图
```

**一句话**：**分流在解析层，收敛在产物层。** 无论上游是 PDF、Word、扫描件还是 Excel，
出口都是同一个 `KnowledgeChunk`；下游（切分、检索、生成、引用）只认这一个结构。

---

## 1 技术栈总表

### 1.1 解析层

| 工具 | 版本 | 作用 | 为什么选它 / 代价 |
|---|---|---|---|
| **docling** | 2.129.0 | PDF/docx 结构化解析主引擎 | MIT；自带版面模型、TableFormer、OCR 编排；**代价**：CPU 上每页秒级 |
| docling-core | 2.98.0 | `DoclingDocument` 数据模型 + HybridChunker | 结构信息（页码/坐标/层级）天然带着，不必自建 |
| docling-parse | 7.21.0 | C++ PDF 解析后端（37MB） | 快；随 docling 安装 |
| docling-ibm-models | 4.0.3 | 版面/表格/图分类模型的推理封装 | — |
| **RapidOCR** | 3.9.2 | 扫描件 OCR（中文） | 纯 onnxruntime、无需 torch、中文好；**权重 31MB** |
| onnxruntime | 1.25.1 | OCR/布局的 onnx 推理运行时 | 已在环境里（7 月装） |
| torch | 2.11.0+**cpu** | 版面/表格模型推理 | ⚠️ CPU 版，GPU 闲置；换 cu128 可提速 |
| python-docx | 1.2.0 | docx 降级链路 + 取表格/标题 | 轻量、成熟 |
| openpyxl | 3.1.5 | xlsx 单元格/合并单元格/公式缓存值 | 唯一能读合并单元格与缓存值的库 |
| python-pptx | 1.0.2 | pptx 文本/表格/**演讲者备注** | 备注字段只有它给 |
| BeautifulSoup + lxml | 4.14.3 / 6.1.0 | HTML 结构解析 | lxml 快且容错 |
| olefile | 0.47 | 解 docx/pptx 里嵌入的 OLE 对象 | 拿到内层真 xlsx，比识别截图准 |
| Pillow | 12.2.0 | 位图处理（缩放/裁剪/编码） | 引用返图的裁剪靠它 |
| pdfminer.six | 20260107 | PDF 文字层探测 + 降级链路 | 毫秒级、不加载模型；用于"要不要开 OCR"的判据 |
| zipfile + ElementTree | 标准库 | 解 OOXML 包（媒体/图表 XML/关系） | **零依赖**：`docx_media.py` 刻意不 import docling |

### 1.2 切分与检索层

| 工具 | 版本 | 作用 | 备注 |
|---|---|---|---|
| HybridChunker（docling-core） | 2.98.0 | token 预算 + 结构感知切分 | 用 BGE tokenizer 对齐 embedding 上限 |
| RecursiveCharacterTextSplitter | langchain-text-splitters | 非 docling 格式的兜底切分 | 已补中文分隔符（避免句中硬切） |
| scikit-learn TfidfVectorizer | 1.8.0 | 稀疏检索（`analyzer="char_wb"`, 2–4gram） | 中文必须用字符 n-gram（word 分词实测 0 命中） |
| **BAAI/bge-m3** | 1024 维 | 稠密检索 embedding | 走独立 `EMBEDDING_*` 配置（与 LLM 分离） |
| RRF（自实现） | — | 稀疏 + 稠密的融合排序 | 无需训练、无需调参 |

### 1.3 模型调用（全部走 API，**本地无 LLM/VLM**）

| 用途 | 模型 | 备注 |
|---|---|---|
| 生成 / 评审 | `deepseek-chat`（`LLM_MODEL`） | 走 OpenAI 兼容接口 |
| 图片结构化描述 | 无默认（`VLM_MODEL`） | ⚠️ 主网关**没有**视觉模型，必须另配一家（`VLM_API_BASE` / `VLM_API_KEY` / `VLM_MODEL`）；默认**关闭**（按图计费） |
| 上下文前缀（Contextual Retrieval） | `deepseek-chat`（`CONTEXTUAL_MODEL`） | 有自己的默认值，换网关时要一起改；默认关 |
| embedding | `BAAI/bge-m3`（硅基流动） | 与上面完全分离的凭证 |

### 1.4 服务与编排

| 工具 | 版本 | 作用 |
|---|---|---|
| OpenAI Agents SDK | 0.20.0 | 唯一编排原语：`Agent`（instructions + tools + handoffs + guardrails）+ `Runner` 主循环。**没有图/节点**；停机靠官方 `RunState` |
| LangChain Core / OpenAI | 1.2.18 / 1.1.11 | 消息与模型封装（`ChatOpenAI`） |
| FastAPI + sse-starlette | 0.135.1 / 3.3.4 | `/chat`·`/review` SSE 流式（含 `citations` 事件） |
| psycopg（同步池） | 3.3.3 | 跨轮历史（官方 `Session`）+ 运行现场（`app/runs.py`）落 PG；缺 `DB_URL` 时回退内存 |
| pydantic | 2.12.5 | 结构化输出校验（评审结果 / 人工决策） |

### 1.5 本地模型权重（1.5GB → 清理后 430MB）

| 权重 | 体积 | 用途 | 在用 |
|---|---|---|---|
| `docling-layout-heron` | 164MB | 版面检测（PyTorch） | ✅ |
| TableFormer `accurate` | 203MB | 表格结构识别 | ✅ |
| `DocumentFigureClassifier-v2.5` | 33MB | 图片类型分类（line_chart/…） | ✅ |
| `RapidOcr`（det/cls/rec） | 31MB | 中文 OCR | ✅ |
| 公式模型 / ONNX 布局 / TableFormer fast | ~~914MB~~ | — | ❌ 已清理（验证后删除，释放 1.1GB） |

预取：`python scripts/fetch_docling_models.py models/docling`（HTTP 直写，绕开 HF 缓存机制在 Windows 上的符号链接问题）。

---

## 2 解析层：按格式分流

**分流原则**：**能确定性拿到的不猜，拿不到才请模型。**

| 格式 | 用什么 | 拿到什么 | 比 Word/PDF "多出来"的难点 |
|---|---|---|---|
| **PDF（电子版）** | Docling（`TableFormerMode.ACCURATE`） | 阅读顺序文本 + 表格网格 + 页码 + bbox + 图片裁剪 + 原页位图 | —（基准） |
| **PDF（扫描件）** | Docling + RapidOCR（`lang=ch` 显式配置） | OCR 文字 + **从位图重建的表格结构** + 整页原图 | 无文字层；版面漏检需 `DOCLING_OCR_MODE=full` 兜 |
| **PDF（降级）** | pdfminer.six | 纯文本 + 段落重建 | 无表格结构、无坐标；仅当 Docling 不可用时 |
| **docx** | Docling（正文）+ **解 zip**（媒体） | 正文层级 + 普通表格 + 图片原件 + **原生图表 XML 数据** + **OLE 内层原件** + 段落位置 | 图表/OLE/图片 Docling 完全拿不到；无页码（用段落序号 + 上下文锚点） |
| **xlsx** | openpyxl + 解 zip | 每行 `列=值` + 合并单元格填充 + 图表 XML（**缓存缺失时按 `c:f` 回表取值**） | ① 值的位置即语义 ② 合并单元格只在左上角存值 ③ **公式缓存值可能不存在** ④ 一个文件多张独立表 |
| **pptx** | python-pptx + 解 zip | 一页一块 + 标题做章节 + **演讲者备注** + 表头感知表格 + 图表/媒体 | ① 形状顺序 ≠ 视觉顺序 ② 备注藏着真正的讲解 ③ `slide10` 会排到 `slide2` 前（按数字排序） |
| **csv/tsv** | 标准库 csv + 分隔符 sniff | 表头 + 每行 `列=值`，**每块重复表头** | 表头只是约定（不用"文本 vs 数字"启发式猜：中文表格里两者都是文本） |
| **json/jsonl** | 标准库 json | 扁平化 `key: value`，**一条记录一块** | 结构是语义的（键即列名）；现实文件常有非法行（按原文保留） |
| **html** | BeautifulSoup + lxml | 标题路径 + 段落 + **表格按行**（表头写进每行） | ① 表格易被拍平成单元格 ② 导航/页脚会污染索引 |
| **md/txt** | 标准库 + 正则 | 标题路径 + **代码块整块保留** + front-matter 单独成块 | 代码被切开后检索回半截代码，断点看着很自然 |
| **图片** | — | 有图无文的块（`needs_vlm` + `source_image`） | 根本没有文字层，只能 OCR/VLM；没开 VLM 时也留在索引里可被筛出 |

**两个跨格式发现**：

1. **图表 XML 在 Word / Excel / PowerPoint 里是同一种格式**（DrawingML chart，`c:chartSpace`）→ 一套解析器服务三个格式。
2. **表格类格式的通用解只有一条：把列名写进每一行**（csv / xlsx / pptx 表格 / html 表格 / docx 表格同解）——二维网格里"值"的含义由列位置承担，而列名通常只在首行。

---

## 3 统一产物：KnowledgeChunk（26 字段）

**双轨设计**（本项目的地基）：

| 字段 | 含义 | 谁用 |
|---|---|---|
| `content` | **保真事实**：原文/原网格，没被加工过 | 回显给用户核对、喂模型 |
| `retrieval_text` | **面向检索的加工文本**：表格行列文字化、章节路径、可选上下文前缀、VLM 描述 | 向量化（出边界时成为旧字段 `text`） |
| `page` / `bbox` / `para_index` / `anchor_text` | 位置：PDF 用页码+坐标；docx 用段落序号+上下文 | 引用回溯、原图裁剪 |
| `kind` | `text` / `table` / `chart` / `diagram` / `formula` / `unprocessable` | 分流处理与统计 |
| `chunk_id` | 可复算的幂等 id | 溯源、去重、增量更新 |
| `parser` / `origin` | 谁解析的、走哪条链路（`docling` / `pdfminer` / `docx_zip` / `csv` / `xlsx`…） | **降级必须可见** |
| `table_md` / `source_image` / `page_image` | 表格网格 / 媒体原件 / **原页截图** | 引用返图 |
| `needs_vlm` / `vlm_desc` | 是否需要/已有模型描述 | 控制成本 |
| `parent_id` / `parent_text` | 父子块（small-to-big） | 生成侧上下文 |
| `unprocessable_reason` | 拒绝要显式，不静默 | 运维可见性 |

---

## 4 切分

### 4.1 docling 侧：四级兜底阶梯

| 级 | 切分器 | 定位 |
|---|---|---|
| 1 | `HybridChunker` | token 预算 + 结构感知，正常路径 |
| 2 | `HierarchicalChunker` | 纯结构切，不做 token 合并 |
| 3 | 按 item 逐条 | 粒度最好，跳过标题 |
| 4 | 整文档 Markdown 单块 | **保证不变式：文档有内容 → 一定有块** |

**为什么需要 4 级**：实测出 HybridChunker 对"通篇只有标题、没有正文 item"的文档会返回 **0 块且不抛异常**（OCR 把整页识别成 `section_header` 时正好命中）→ 我们据此判"解析不出内容"，把已经抽到的文字丢掉了，属**静默数据丢失**。

### 4.2 其它格式：结构块 + 超大块二次切分

- 每种格式先产出**它自己说得清的结构**（表头/工作表/幻灯片/记录/标题/代码块）；
- 超过 `RAG_CHUNK_SIZE` 的块二次切分，**section/page 跟着走**（切开的只是长度，归属不变）。

### 4.3 父窗（small-to-big，默认开，零成本）

检索命中**小块**（精度高），送模型时换成命中块所在的**连续窗口**（约 1200 字，同一文档内扩张）。

用**窗口**而不是"按章节分组"：PDF 的 `section_path` 常为空，按节分组等于把整篇文档当父块再截断 → 结果**永远是文档开头**，命中块根本不在里面。

---

## 5 切分 → 向量化之间的 9 步处理

| # | 处理 | 作用 | 默认 |
|---|---|---|---|
| 1 | `chunk_writer.to_legacy` | 出边界契约：`text := retrieval_text`、`section := section_path`，旧消费者不受影响 | 开 |
| 2 | 表格行列文字化 `linearize_table` | `\| 华东 \| 1200 \|` → `地区=华东；销售额=1200`（让"华东卖了多少"能召回） | 开 |
| 3 | 章节路径并入检索文本 | 标题是召回线索（"第三章的出口数据"） | 开 |
| 4 | 图片落盘 `source_image` | 供 VLM 描述与引用返图 | 开 |
| 5 | 原页截图 `page_image` | 扫描件整页 / 表格图区域（引用返图） | 开（`DOCLING_PAGE_IMAGES=auto`） |
| 6 | VLM 结构化描述 | 确定性来源（图注/替代文字/chart XML）都拿不到时才调模型 | **关**（按图计费） |
| 7 | 上下文前缀（Contextual Retrieval） | 每块一句"它在全文的位置与主题"再嵌入 | **关**（每块一次 LLM 调用） |
| 8 | 父窗 `parent_text` | 生成侧上下文 | 开 |
| 9 | 嵌入 | 只对 `text` 做；`content` **不参与向量化** | — |

---

## 6 向量化与索引

| 检索器 | 输入 | 特点 | 实测（6 文档 / 28 查询） |
|---|---|---|---|
| `TfidfRetriever` | `c["text"]` | 字符 2–4gram TF-IDF，纯本地零依赖 | Top1 71%，**照抄型 100%** |
| `EmbeddingRetriever` | `c["text"]` | bge-m3 1024 维；批 10（`EMBEDDING_BATCH`）；**查询前缀只加查询侧** | Top1 18%；加英文指令 39% |
| `HybridRetriever` | 两者 | RRF 融合（+可选 rerank，失败自动回退） | Top1 64%，**改写型 42%（最高）** |

**两个刻意的设计**：

- **embedding 与 LLM 完全分离**（`EMBEDDING_*` vs `LLM_*`）：对话网关不提供 embeddings 接口。
- **不引向量数据库**：语料规模（< 1 万块）用 numpy 暴力算余弦足够；引入向量库是等到规模证明它值得再说。

---

## 7 检索 → 生成 → 引用（可回溯）

```
query → retriever.search(top_k=3) → 完整块记录（26 字段）
                                      ├─ 送模型：context_block()  优先级 父窗 > 保真内容 > 检索文本
                                      └─ 给用户：build_citations()
                                           { n, source, score, chunk_id, kind, parser, origin,
                                             section, page, bbox, para_index, anchor_text,
                                             snippet(保真节选), truncated,
                                             table_md?, source_image?, page_image?, alt_text?, tags?,
                                             where: "年报.pdf · 第 1 页 · 第三章 > 出口情况" }
```

- **`snippet` 取 `content`（保真）而不是 `text`（加工品）**：拿文字化结果当"原文"回显，用户核对时会对不上。
- **原图返图规则**：扫描件 → 整页原图；表格/图表/图片/公式 → 按 bbox 并集裁区域；电子版纯文字 → **不返图**（没必要）。
- `/chat` 与 `/review` 在正文之后、`done` 之前下发 `event: citations`。

---

## 8 成本与开关一览

| 开关 | 默认 | 打开后花钱/花资源 | 收益 |
|---|---|---|---|
| `RAG_DOC_PARSER` | `docling` | — | 主解析链路 |
| `DOCLING_OCR` | `auto` | 扫描件才开；21s/页（CPU） | 扫描件可读 |
| `DOCLING_FORMULA` | `0` | 每页跑公式模型 | 公式转 LaTeX |
| `DOCLING_PAGE_IMAGES` | `auto` | 每页一张位图常驻内存（耗时 +0.4s/5 页，可忽略） | 引用返原图 |
| `RAG_VLM_DESC` | `off` | **按图计费**（一张约 950 输入 token） | 图片可被文字检索 |
| `RAG_CONTEXTUAL` | `off` | 每块一次 LLM 调用（⚠️ 实测本语料上让指标变差） | 理论提升检索 |
| `RAG_PARENT_CHILD` | `on` | 零成本 | 生成侧上下文 |
| `RAG_EMBEDDING` | `hybrid` | embedding API 调用 | 混合召回 |

---

## 9 验证入口（不靠"看起来对"）

| 命令 | 验证什么 |
|---|---|
| `pytest tests/ -q` | 335 项单测（全部离线；需模型权重的用例只在权重缺失时 skip） |
| `python scripts/smoke_docling.py` | 含表格 PDF：`len(doc.tables)` 与 Markdown 还原 |
| `python scripts/check_docx.py` | docx 双路线：图表数值 / 替代文字 / OLE 内层正文 / 位置锚点（七项核对） |
| `python scripts/check_ocr.py --sample cn` | 中文扫描件：OCR 文字 + 表格重建 + **原图返回** |
| `python scripts/check_vlm.py [--dry-run]` | 图片 VLM：算量与真调用（校验防幻觉规则） |
| `python scripts/eval_retrieval.py [--contextual]` | 检索质量（按 literal / paraphrase 分组） |

---

## 10 明确不做 / 未做

| 项 | 状态 | 理由 |
|---|---|---|
| 本地 LLM / VLM 权重 | ❌ 不做 | 走 API；本地只有 Docling 的解析模型（430MB） |
| MinerU | ⏸ 暂不引入 | 实测清晰扫描件的表格能被 Docling 重建；换后端要自建第二套 provenance 模型 |
| PyMuPDF | ❌ 不引 | **AGPL**；页面渲染用 Docling 自带能力（MIT） |
| 旧二进制 `.doc/.xls/.ppt` | ❌ 不支持 | 需先装 LibreOffice 转换 |
| 向量数据库 | ⏸ 不做 | 语料规模未到 |
| ColPali 视觉检索 | ⏸ 不做 | 成本与语料规模不匹配 |
| 页面渲染截图（bbox 高亮标注） | ⚠️ 部分 | 已返原图，但图上**没有画框标出命中区域**；前端可按 `bbox` 自行叠加 |
