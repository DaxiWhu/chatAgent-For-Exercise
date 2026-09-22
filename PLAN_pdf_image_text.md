# 电子版 PDF 图文处理方案（v2）

> ⚠️ **范围已被扩展**：本档只覆盖「电子版 PDF」。全格式（Word / 电子版 PDF / 扫描件 / 模糊件）的分流定稿见 **`PLAN_doc_parsing_v3.md`**。
> 本档**继续有效**的部分：KnowledgeChunk 双轨产物（§6）、流水线设计（§7）、模块分层与 `docling_loader.py` 唯一耦合点（§8）、切分实测结论（§3）。
> v3 已修订本档的：批次划分（第 9 节 → v3 §11）、图片与 ColPali 的界定（v3 §7）。

> 制定日期：2026-09-21（v2，**改用 Docling 做解析层**）
> 范围：**只处理电子版 PDF**（有文字层的）。扫描件 / 模糊图片 / 纯图像文件不在本次范围。
> 关联：`ARCHITECTURE.md`（终态蓝图）、`PROJECT_STRUCTURE.md`（现有结构）

---

## 0 v2 修订说明（为什么要改）

v1 里我打算**自研**布局探测、表格提取、图片分类（批次 2/3/4）。**这个判断是错的**——用户指出应该直接用 Docling 这类工具。
核实后确认：**Docling 已经覆盖了 v1 里大部分自研内容，且安装代价很小**（`torch` / `transformers` 本机已装，只需新增约 30 个小包）。

v2 的分层改为：

```
DoclingDocument（文档事实模型）   ← Docling 产出，我们不自研
      ↓  我们只做这一层转换
KnowledgeChunk（知识检索单元）    ← 本项目需要的结构
```

---

## 1 Docling 覆盖了什么（我们**不再自研**）

| 能力 | Docling 产出 |
|---|---|
| **布局检测** | Layout Model 判断区域类型：标题 / 正文 / 表格 / 图片 / 页眉页脚 |
| **阅读顺序重建** | 双栏、复杂排版按正确顺序输出（`body` 树的子项顺序即阅读顺序）——**这是 pdfminer 做不到的** |
| **表格结构** | `TableItem.data.grid`：N×M 单元格数组，**含行列跨度（row/col span）与表头标记** |
| **公式** | 行内与独立公式**自动转 LaTeX** |
| **图片** | `PictureItem`：图片数据 + **caption** + **内容分类标注**（如 `predicted_class: "line_chart"`） |
| **溯源** | 每个元素带 `page_no` + `bbox`（l/t/r/b）+ `charspan` |
| **正文 / 修饰分离** | `body` vs `furniture`——**页眉、页脚、页码自动归入 furniture**，不用自己判"是不是装饰" |
| **结构感知切分** | `HybridChunker`：切块附带 `meta.headings`（章节路径）与 `meta.doc_items`（回溯引用） |
| **导出** | Markdown / HTML / JSON / DocTags / 纯文本 |

**安装代价（实测 `pip install --dry-run docling`）**：

- `torch`、`transformers`、`tokenizers`、`sympy`、`numpy` 等**均已满足**；
- 需新增约 30 个小包：`docling-core`、**`docling-ibm-models`**（模型定义）、**`docling-parse`**（PDF 后端）、
  `rapidocr`（轻量 OCR）、`rtree`、`shapely`、`semchunk`、`tabulate`、`latex2mathml`、`tree-sitter-*` 等；
- **一次性成本**：首次运行时下载模型权重（Layout + TableFormer + OCR，数百 MB 量级）；
- 默认**全本地运行**，不外发数据；模型可预下载，支持离线/内网部署。

---

## 2 Docling **不**做的（这才是我们项目要写的那一层）

| # | 我们要做的 | 为什么 Docling 不管 |
|---|---|---|
| **1** | **双轨产物**（`content` 保真 + `retrieval_text` 面向检索） | Docling 给的是文档事实；"用什么文本去做向量召回"是检索设计 |
| **2** | **表格的行列文字化** | Docling 给结构化 grid；但 `18-40岁 \| 160%` 这种二维结构在向量空间里离自然语言问题很远，需改写成接近句子的形式 |
| **3** | **图片"业务上有无价值"的判定** | Docling 给技术分类（`line_chart` / `picture`）与 furniture 分离；**但对我们的知识库有没有价值是业务判断**（不过有了 classification，规则可以极简） |
| **4** | **趋势方向的推理** | Docling 分类为 `line_chart`，但不判断"上升/下降" |
| **5** | **切分策略统一**（中文分隔符） | Docling 的 `HybridChunker` 按结构+token 切；中文字符分隔符配置仍需我们给的 chunker 处理 |
| **6** | **接入现有图**：`chunk` 结构、`retrieve` 节点、元信息扩展 | 项目内部事务 |
| **7** | **未处理项的显式标记**（不再静默丢弃） | 呼应"拒绝不能变成静默" |

---

## 3 切分：递归切分是主力，只需补一处配置

> **修正说明**：本节初版写成"递归切分只是兜底、句子级打包更优"——**表述过强，已按补充实测修正**。

### 实测一：有段落结构（`\n\n`）的文本 —— 默认配置就没有问题

303 字、3 个段落、每段都超 `chunk_size=120`：

| 方式 | 块数 | 块长 | 真·句中断开 |
|---|---|---|---|
| 递归 · 默认英文分隔符 | 3 | 94~105 | **0** |
| 递归 · 中文分隔符 | 3 | 94~105 | **0** |
| 句子级打包 | 3 | 94~119 | **0** |

**三者完全相同。** 原因：递归切分第一层分隔符就是 `\n\n`，**在到达"空格 / 逐字符"层级之前，文档已被段落切开**。

### 实测二：唯一的失败场景 —— **没有换行的长段落**

105 字、单段无换行、带 `。` `，`、`chunk_size=100`：

| 方式 | 块 1 | 句中断开 |
|---|---|---|
| 递归 · 默认英文分隔符 | **正好 100 字**，尾 `…常住人口约二` | ❌ **1 处** |
| 递归 · 中文分隔符 | 68 字 | ✅ 0 |
| 句子级打包 | 69 字 | ✅ 0 |

**根因**：无换行 → `\n\n` / `\n` 都用不上；中文无空格 → 从 `" "` 掉到 `""` 逐字符 → 凑满处切断。

### 结论

1. **递归切分是生产主力**（用户判断正确）；
2. **唯一要修的是配置**：`_make_splitter`（`src/retrieval.py:144`）没传 `separators`，用的是英文默认值。补上中文标点后，
   **递归切分与句子级打包效果等价**（68 vs 69 字，都是 0 处句中截断）；
3. **语义切分确实稀有**——生产上更常见的是父子块与 Contextual Retrieval。

### 要改什么

```python
RecursiveCharacterTextSplitter(
    chunk_size=..., chunk_overlap=...,
    separators=['\n\n', '\n', '。', '！', '？', '；', '，', ' ', ''],
    keep_separator=True,
)
```

---

## 4 图片"有无价值"的判定（基于 Docling 输出，规则极简）

**有了 Docling 的 `picture.label` + `annotations.predicted_class` + `captions` + `furniture` 分离，"是否有意义"不再需要 7 个自研信号**，规则可以压缩成：

| 判据（按顺序） | 结论 |
|---|---|
| 元素属于 `furniture`（页眉/页脚/页码） | ❌ 无价值，丢弃 |
| 有 `captions` | ✅ 有价值 |
| `predicted_class` ∈ {`line_chart`, `bar_chart`, `pie_chart`, ...} | ✅ 有价值（是图表） |
| 正文中出现"见图 N / 如表 N / 如图"引用 | ✅ 有价值 |
| 面积占比 < 3% 且无 caption 且无分类 | ❌ 大概率装饰，丢弃 |
| 其余（如 `predicted_class: "picture"` 的位图） | ⚠️ 标 `needs_vlm`，本次只记元信息 |

> **一次实测的副产品**：v1 里我发现"电子版 PDF 的图分矢量图和位图，矢量图的文字坐标可读、可以零模型处理"。
> 用 Docling 之后**这个区分不再必要**——Docling 把图统一变成 `PictureItem` 并给出内容分类。
> 记录在此仅作背景：它解释了"为什么有些图本来能零成本读出来"，但**不作为实现路径**。

---

## 5 表格、图表、公式分别怎么处理

| 类型 | 来源 | 我们的处理 |
|---|---|---|
| **表格** | `TableItem.data.grid`（含行列跨度、表头标记） | ① `structured_data`：转 Markdown（保住网格，作事实源）<br>② `retrieval_text`：**行列文字化**（`18-40岁对应给付比例160%`）<br>③ 生成一句表摘要<br>④ 元信息：`page` / `bbox` / `n_rows` / `n_cols` / `caption` |
| **图表** | `PictureItem` + `predicted_class` + `captions` | ① `retrieval_text` = caption + 分类标签（"这是一张折线图，标题为…"）<br>② **趋势方向**：可选接 VLM（`agnes-2.5-pro` 已实测可用）生成"整体上升"这类描述<br>③ 保留原图引用与 bbox |
| **示意图 / 照片** | `PictureItem` | 有 caption 用 caption；无 caption 标 `needs_vlm`，本次不处理 |
| **公式** | `TextItem(label="equation")`，**Docling 已转 LaTeX** | ① `structured_data` = LaTeX（保真）<br>② `retrieval_text` = 一句自然语言描述（可用 VLM 生成，或先留空并标 `needs_desc`） |

**为什么表格必须有两个表示**（这是本项目自己的设计，Docling 不管）：

```
structured_data:  年龄     | 给付比例
                  18-40岁  | 160%
                  41-60岁  | 140%

retrieval_text:   XX终身寿险 > 保险责任 > 身故保险金：
                  18-40岁对应给付比例160%；41-60岁对应140%。
```

前者喂 LLM 保真，后者用于召回——**用户不会用二维表格语法检索**。

---

## 6 统一产物结构（KnowledgeChunk）

```json
{
  "chunk_id":     "sha1(source + page + kind + bbox)",
  "source":       "report.pdf",
  "page":         7,
  "kind":         "text | table | chart | diagram | formula",
  "section_path": "第三章 > 出口情况",
  "bbox":         [l, t, r, b],

  "content":      "事实内容（表格 Markdown / LaTeX / 原文）",
  "retrieval_text": "面向检索的自然语言（用于向量化）",
  "tags":         ["折线图", "出口量", "增长趋势"],

  "page_total":   12,
  "needs_vlm":    false,
  "parser":       "docling",
  "parser_version": "...",
  "file_checksum": "..."
}
```

**关键字段**：`content`（保真）与 `retrieval_text`（召回）**必须分开**——这是本层设计的核心。

---

## 7 流水线全景

```
电子版 PDF
   │
   ├─ ① Docling 解析（含模型，本地运行）  ← 不再自研
   │     ├─ Layout 检测 → 标题/正文/表格/图片/页眉页脚
   │     ├─ Reading Order 重建（双栏也能对）
   │     ├─ TableFormer → TableItem.data.grid
   │     ├─ 公式 → LaTeX
   │     ├─ PictureItem → caption + 内容分类
   │     └─ 每元素带 page_no + bbox
   │
   ├─ ② 遍历 body 树（按结构，不按数组顺序）
   │     ├─ 收集 section_path（章节层级 + 标题文本）
   │     └─ 跳过 furniture（页眉页脚）
   │
   ├─ ③ 元素路由 → 各自生成双轨产物
   │     ├─ text    → content=原文，retrieval_text=原文+section_path 前缀
   │     ├─ table   → content=Markdown，retrieval_text=行列文字化
   │     ├─ chart   → content=caption，retrieval_text=caption+分类+（可选）趋势
   │     ├─ formula → content=LaTeX，retrieval_text=自然语言描述
   │     └─ picture → 判定是否值得入索引；否则标 needs_vlm
   │
   ├─ ④ 切分（递归 + 中文分隔符；或复用 Docling HybridChunker）
   │
   └─ ⑤ 产出 KnowledgeChunk 列表 → 现有 EmbeddingRetriever / HybridRetriever
          （检索层接口不变，src/graph.py 的 retrieve 节点不用改）
```

---

## 8 模块与接口

```
src/docling_loader.py   新增 · Docling 封装（唯一与 Docling 耦合的地方）
    parse_pdf(path) -> DoclingDocument
    iter_body(document) -> 按阅读顺序产出 (item, section_path)

src/knowledge_chunk.py  新增 · DoclingDocument → KnowledgeChunk 转换层（本项目核心）
    to_chunks(document, source) -> list[KnowledgeChunk]
    from_text(item, path)    -> KnowledgeChunk
    from_table(item, path)   -> KnowledgeChunk      # 含行列文字化
    from_picture(item, path) -> KnowledgeChunk|None # 含有无价值判定
    from_formula(item, path) -> KnowledgeChunk
    linearize_table(grid)    -> str                 # 行列文字化
    table_to_markdown(grid)  -> str

src/chunk_writer.py     新增 · 接入现有 load_chunks（保持旧字段兼容）
    to_legacy(chunk) -> dict   # 保证 source/text 字段仍在，旧测试不破

src/docx_media.py       新增 · docx 路线 C：解 zip 取 Docling 拿不到的那一半（纯标准库）
    extract(path) -> list[DocxAsset]      # 图片原件 / OLE 原件 / chart XML 数据 / 段落位置
    parse_chart_xml(data) -> dict         # 图表数据是确定性的，不送 VLM

src/retrieval.py        改动 · 切分补中文分隔符；load_chunks 的 .pdf / .docx 分支改走 docling_loader
```

**分层原则**：`docling_loader.py` 是**唯一** import docling 的文件。
将来换 MinerU / PaddleOCR-VL，只需改这一个文件 + `knowledge_chunk.py` 的字段映射。
`docx_media.py` 刻意**不** import docling（只用 zipfile + ElementTree）——它在"没装 docling"
的降级环境里也必须能为 docx 取媒体，否则那条降级支路会一起废掉。

> 实现现状（含实测缺陷与修法）见 `PLAN_doc_parsing_v3.md` 附录 F/G/H；批次进度见其 §11。

---

## 9 分阶段落地（从 7 批缩减为 4 批）

| 批次 | 内容 | 验收标准 | 依赖 |
|---|---|---|---|
| **1** | **切分统一**：`_make_splitter` 补中文分隔符；PDF 链路实测等价后并入统一入口 | 无换行长段落**零句中截断**；现有 101 测试全过 | 无 |
| **2** | **接入 Docling**：`pip install docling`；`src/docling_loader.py`；跑通一份 PDF 得到 DoclingDocument | 能打印出每页的 text/table/picture/formula 数量与 bbox | 1 |
| **3** | **转换层**：`src/knowledge_chunk.py` —— 双轨产物、表格行列文字化、section_path、图片价值判定 | 一份含表格+图表的 PDF → 产出 KnowledgeChunk；表格数值能被自然语言检索命中 | 2 |
| **4** | **接入主流程**：改 `load_chunks`、兼容旧字段、端到端验证、`needs_vlm` 显式标记 | 端到端：检索能命中表格数值与图表 caption；未处理项被标记而非静默丢弃 | 3 |

**批次 1 与批次 2 相互独立，可以并行或先做任一个。**

**为什么批次 1 仍排前面**：它零新依赖、零风险、改动小（只改 `src/retrieval.py`），且是批次 2-4 的地基。

---

## 10 本次明确不做

| 不做 | 原因 |
|---|---|
| 自研布局探测 / 表格结构识别 | **Docling 已覆盖且更好**（v1 的错误判断，已修正） |
| 自研图片"7 信号"判定 | 有 Docling 的 classification + furniture 分离后，规则可极简 |
| 扫描件专项优化 | Docling 自带 OCR（rapidocr），先用默认效果；专项后续再说 |
| 跨页表格合并 | 需要 VLM + 确定性校验，风险高（见 `ARCHITECTURE.md`） |
| 引入向量库 | 语料规模未到，`ARCHITECTURE.md` D15：< 1 万片段不引入 |
| 语义切分 | 需 embedding、成本高、结果不确定 |
| 矢量图坐标法（v1 的思路） | 走 Docling 后不再需要；记录在 §4 作背景 |

---

## 11 风险与待确认

| 风险 | 说明 | 缓解 |
|---|---|---|
| **首次模型下载** | Docling 首次运行要下模型权重（数百 MB） | 提前在目标机器预热一次；确认内网可达 |
| **速度** | Docling 跑视觉模型，比 pdfminer 慢（每页有推理开销） | 灌库是离线的一次性成本；若要提速可只对"疑似有表/图"的页启用模型 |
| **输出稳定性** | 模型推理结果需确认可重复（同输入同输出） | 批次 2 里实测验证；固定模型版本 |
| **依赖锁定** | 新增约 30 个包 | 记入 `requirements.txt` 并锁定版本 |
| **与现有 101 测试的关系** | 改 `load_chunks` 会影响 PDF 相关测试 | 用 `chunk_writer.to_legacy()` 保证旧字段不变；必要时同步调整测试并说明改动 |
| **中文字符切分** | Docling 的 `HybridChunker` 按 token 切，中文字符级切分要额外配置 | 批次 1 先修好本项目切分器；批次 3 决定文本用哪个 chunker |
