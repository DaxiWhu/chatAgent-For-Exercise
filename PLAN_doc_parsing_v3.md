# 文档解析总方案 v3（全格式分流定稿）

> 日期：2026-09-22
> 范围：**全格式** —— Word / 电子版 PDF / 清晰扫描件 / 模糊 PDF / 图片转存 + 切分与入库
> 关系：本档把 v2（`PLAN_pdf_image_text.md`，仅覆盖电子版 PDF）**扩展为全格式**。v2 中未变的部分继续有效、本文不重复：
> KnowledgeChunk 双轨产物（`content` / `retrieval_text`）、`src/docling_loader.py` 作为唯一耦合点、批次 3-4 的转换层设计。
> 关联：`ARCHITECTURE.md`（终态蓝图）、`COMPARE_doc_parsers.md`（同类工具对比）

---

## 0 结论先行

你给出的方案，**主线判断正确**：Docling 统一做解析层、MinerU 作中文备选、结构感知切分、图片经 VLM 转文字描述后与原文合并入库、按格式分流而非一套打天下。

有 **5 处需要校准**，其中 2 处会直接导致实施失败：

| # | 原写法 | 问题 | 修正 |
|---|---|---|---|
| **1** | Word 用 Docling 提取「原生图表、OLE 对象、截图**全部**导出成图片文件」 | **Docling 做不到**。<br>· OLE 嵌入对象：**被完全忽略**，`embedded_files` 至今是未实现的 feature request（issue #2051）<br>· docx 图片提取**不可靠**：多人反馈老版本 Word 生成的 docx 提不出图（issue #1347），需 pandoc 转一遍才能出<br>· **docx 后端没有页码与 bbox**——Docling 论文明确：MS Word/HTML 的元素位置 provenance 在渲染前是未知的 | Word 走**混合路线**：结构信息用 Docling，图表/图片/OLE 另走一条**确定性**来源（§2） |
| **2** | 表格 `mode="ACCURATE"` | 参数名不对，照抄会报错 | `pipeline_options.table_structure_options.mode = TableFormerMode.ACCURATE`（`from docling.datamodel.pipeline_options import TableFormerMode`） |
| **3** | 图片送入**本地** VLM（Qwen2-VL / Llava） | 本机确有 GPU（RTX 5060 Laptop，**8GB**），但 **torch 是 CPU 版**：`torch 2.11.0+cpu`、`cuda_available=False`。现状下"本地 VLM"跑不起来 | §9 给三条路，**建议先用 API**（项目已配 agnes-2.5-pro，实测支持视觉输入） |
| **4** | 「PDF 转图片并保存：条件触发 **ColPali**」 | **返回图片 ≠ ColPali**，两件事被捆在一起。条件触发的是"要不要附页面截图"，这与 ColPali（视觉检索）无关 | §7 拆成两个独立决策：附截图**默认开**（零模型）；ColPali **暂不引入** |
| **5** | 切分用 Docling HybridChunker，「有更厉害的可以介绍」 | HybridChunker 已是当前最优默认，换掉它无收益 | §8：真正值得做的是**父子块**与 **Contextual Retrieval**，收益可量化 |

**其余判断成立**：MinerU 用于扫描件更强、MinerU 无内置结构感知切分所以需自己做、模糊件用 TextIn 合合、原文+表格+图片描述合并入库。

---

## 1 分流总图

```
文件进来
  │
  ├─ ① 判断容器格式
  │     ├─ .docx / .doc ──────────────────────────→ §2 Word 混合路线
  │     ├─ .pdf ──────────────────────────────────→ ②
  │     └─ 图片 (.png/.jpg/…) ────────────────────→ 直接进 Docling（包一层 PDF 走图像管线）
  │
  ├─ ② PDF 判断有无可用文字层
  │     ├─ 有，且抽取字符数/页 > 阈值 ────────────→ §3 电子版：Docling 标准管线
  │     └─ 无 / 极少 ─────────────────────────────→ ③ 扫描件
  │
  ├─ ③ 扫描件：先本地 OCR 试跑
  │     ├─ 抽样页识别置信度可接受 ────────────────→ §4 Docling + OCR（中文用 MinerU 备选，实测决定）
  │     └─ 置信度低（模糊/底色/粘连） ────────────→ §5 转 TextIn 合合（云端）或标 unprocessable 拒绝
  │
  └─ ④ 统一进入：元素路由 → 双轨产物 → 切分 → KnowledgeChunk → 向量库
        （此层与格式无关，是 v2 §6/§7 的设计）
```

**要点**：**分流在解析层，收敛在产物层**。无论上游用 Docling / MinerU / TextIn，出口必须是同一个 `KnowledgeChunk` 结构——这是 `docling_loader.py` 作为唯一耦合点的意义。

---

## 2 Word（docx）：Docling 只能给你一半，另一半必须另找来源

### 2.1 Docling 在 docx 上到底能给什么

| 元素 | Docling 表现 | 结论 |
|---|---|---|
| 正文段落 / 标题层级 / 列表 | ✅ 基于 python-docx，直接产 `DoclingDocument` 树 | 可用 |
| 普通表格 | ✅ 相对可靠（docx 表格本身是结构化的，不需要 TableFormer） | 可用，**且比转 PDF 后识别更准** |
| 截图 / 位图 | ⚠️ **不可靠**：新版本 Word 创建的可能出，老版本/pandoc 生成的常常一封都不出 | 不可依赖 |
| **Word 原生图表**（插入的折线/柱状图） | ❌ 拿不到。本质是 `chart XML + 内嵌 xlsx`，python-docx 层面无渲染图 | 必须另找来源 |
| **OLE 对象**（嵌入的 Excel / PPT / 公式编辑器对象） | ❌ **完全忽略**，输出 JSON 里连引用都没有 | 必须另找来源 |
| 页码 / bbox | ❌ **不存在**。docx 是流式格式，无固定分页，Docling 不渲染 | 位置只能用"文档流顺序"表达 |

> 参考：docling issue #2051（OLE 提取，未实现）、issue #1347（DOCX 图片提取失败，多版本反馈）、Docling 论文 §2（"for MS Word or HTML, element locations and page provenance are unknown unless rendered"）。

### 2.2 三条路线

| 路线 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **A** Docling docx 后端 | 直接 `converter.convert("x.docx")` | 文本与表格结构最好；无模型开销、极快 | 图表/OLE 拿不到；图片不可靠；无位置 |
| **B** 转 PDF 再解析 | LibreOffice `soffice --headless --convert-to pdf` 或 Word COM → 再走 Docling PDF 管线 | **所有视觉元素都被渲染成页面像素**，能被 PDF 管线当 `PictureItem` 抠出；有页码 + bbox | 表格精度**下降**（结构化表格变成图形、需重新识别）；文本可能重新断行；多一步转换 |
| **C** 直接解 docx 包（自研 ~100 行） | docx 就是 zip：`word/media/` 是图片原件、`word/embeddings/` 是 OLE 原件、`word/charts/chart*.xml` 是**图表的原始数据**；用 `word/_rels/document.xml.rels` + `document.xml` 里的 `r:embed` / `o:OLEObject/@r:id` 反向定位，即可得出「第 N 段之后有一张图」 | **100% 确定性，零模型**；拿到的是**原始数据而非图像**；位置可精确定到段落序号 | 需自己写解析器；不产出渲染图（若下游要"图长什么样"仍需渲染） |

### 2.3 推荐：A + C 组合（B 作兜底）

```
┌─ A（Docling docx） ──→ 正文文本 + 标题层级 + 普通表格     ← 结构最好的来源
│
└─ C（解 docx 包） ────→ 图片原件 + OLE 原件 + 图表原始数据   ← 确定性的补充来源
                          + 每个元素"在第几段之后"的位置
   （A 的 body 树顺序 + C 的段落序号，两边可对齐 → 上下文可还原）
```

**为什么 C 比 VLM 更该优先**：

Word 原生图表的数据**本来就在 XML 里**，格式是：

```xml
<word/charts/chart1.xml>
  <c:lineChart>                        ← 图表类型（不用猜）
    <c:ser>
      <c:cat> 2019 2020 2021 2022 </c:cat>   ← X 轴类目
      <c:val> 120  145  138  180   </c:val>   ← 数值序列
```

也就是说，对 Word 图表而言，**"坐标轴、数值、趋势、图例"这些你要 VLM 输出的东西，本来就有确定性答案**。VLM 看图反而会引入误差（生成式模型会"编辑"而非仅转写）。**先取 XML，取不到才退到 VLM** —— 这条原则同样适用于 PDF 里的矢量图。

OLE 对象同理：拿到嵌入的 `.xlsx` 原件后，直接用 `openpyxl` 或 Docling 的 xlsx 后端解析，比识别一张截图准得多。

**什么时候才需要 B**：docx 里含**复杂 SmartArt、文本框叠图、艺术字**等 C 无法复原视觉效果的形态，且下游确实要看渲染图。此时用 B 兜底。

**windows 上转 PDF 的两个选项**：
- LibreOffice headless：`soffice --headless --convert-to pdf --outdir <dir> <file.docx>`（无 Office 依赖，需装 LibreOffice）
- Word COM：`win32com.client.Dispatch("Word.Application")` → `SaveAs(wdFormatPDF)`（需装 Office，但保真度最高）

---

## 3 电子版 PDF：Docling 配置清单

你的描述基本正确，这里把「参数 → 默认值 → 我们设什么 → 代价」列成可直接抄的清单。

| 配置项 | 默认 | 建议 | 说明 / 代价 |
|---|---|---|---|
| `table_structure_options.mode` | `FAST` | **`ACCURATE`** | TableFormer 高精度模式，合并单元格 / 无边框表更好。**约慢 2×**（只在含表的页有感知） |
| `do_table_structure` | `True` | `True` | 关掉的话表格会被压成散文，检索质量在切块前就受损 |
| `do_cell_matching` | `True` | `True` | 单元格与文本对齐，影响 `export_to_dataframe()` 的准确度 |
| `do_formula_enrichment` | **`False`** | 按需 | 转 LaTeX。**每页跑公式模型，显著变慢**。只在确实有数学内容时开 |
| `do_code_enrichment` | `False` | 按需 | 代码块专用 OCR |
| `do_picture_classification` | **`False`** | **`True`** | 产出 `predicted_class`（`line_chart` / `bar_chart` / `picture` …）——**我们的图片价值判定直接依赖它** |
| `do_picture_description` | **`False`** | 见 §6 | Docling **内置** VLM 描述能力，可配 `PictureDescriptionVlmOptions(repo_id=...)`。默认模型很小（SmolVLM-256M 级），中文/复杂图表能力有限 |
| `generate_picture_images` | **`False`** | **`True`** | 不显式打开就**导不出裁剪图**。这是"图片导出成文件"的开关 |
| `generate_page_images` | **`False`** | 按需 | 整页位图。**内存开销大**（每页一张图），只在需要"附页面截图"时开 |
| `images_scale` | `1.0` | `1.0~2.0` | 2.0 对小字/细线更清楚，内存与耗时同比上升。**不要设 0** |
| `do_ocr` | `True` | 电子版可关 | 有文字层的 PDF 关掉可省大量时间（OCR 对每页跑识别器） |
| `accelerator_options.device` | `auto` | `cpu`（现状） | 装了 CUDA 版 torch 后可设 `cuda`，大幅提速 |
| `artifacts_path` | `None` | 生产环境**必须设** | 预下载模型到本地目录，避免运行时依赖 HuggingFace 可达性（`docling-tools models download`） |

**公式的正确表示**（呼应 v2 §5）：Docling 输出 LaTeX 只是**保真**，它离自然语言问题很远。`retrieval_text` 仍需一句自然语言描述，否则"这个公式什么意思"这类提问永远召回不到。

**关于"图片自动裁剪导出 + 记录在第几段之后"**：

- 裁剪：`generate_picture_images=True`，然后遍历 `doc.pictures`，`picture.image.pil_image.save(...)`
- 位置：每个 item 带 `prov`（`page_no` + `bbox`）——**PDF 里有页码和坐标，这正是 docx 缺的东西**
- "在第几段之后"：遍历 `doc.iterate_items()` 时维护一个"上一个文本项的序号"即可；但更稳的是直接用 `section_path`（基于 `HybridChunker` 的 `meta.headings`）——**章节路径比段落序号稳定**，插入删除段落不会失效

---

## 4 清晰扫描件：Docling + OCR，MinerU 作实测备选

### 4.1 你已得出的结论是对的

> "MinerU 好像不能像 Docling 一样内置结构感知切分，需要自己做切分……建议全部都使用 Docling"

**同意，并且理由比"省事"更硬**：

**混用两套解析器会让 chunk 元信息模型分裂。** Docling 的 `DoclingDocument` 里每个元素天然带 `prov`（页码+bbox）与类型标签，`HybridChunker` 直接消费它产出 `headings` / `doc_items` / `page_no`。
换成 MinerU，你拿到的是"阅读顺序 JSON"，要自己再建一套等价的 provenance 模型——**同一份语料里出现两种 chunk 结构，检索层与引用层都要写两套分支**。

所以分层原则确立为：**解析后端可换，产物模型不换。** `src/docling_loader.py` 是唯一允许 import 解析器的地方。

### 4.2 什么时候真的该换 MinerU

只在一种情况：**实测发现 Docling 在你的中文扫描件上表格/版面精度不够**。

判断方法（`COMPARE_doc_parsers.md` §6 的自测清单）：

```python
print(f"tables: {len(doc.tables)}")   # ★ 最该盯的数
```

已知文档里有 N 张表，解析出 0 张 → 表格被压成散文，**你的检索质量在切块之前就已经坏了**，这时换后端才有意义。

**注意 MinerU 的两个成本**（`COMPARE_doc_parsers.md` §3）：
1. 自定义许可，**署名条款不设营收门槛**——把它支撑的解析作为服务提供给第三方时必须在产品/文档中显著标注；
2. 无 GPU 时只能用 pipeline 后端（OmniDocBench 86.47），**换它未必比 Docling 强**。

---

## 5 模糊 PDF：TextIn 合合，但有前置条件

你的判断成立（抗底色/阴影/粘连、复杂表格还原强、支持公式），但要注意它是**云端上传**：

| 约束 | 处理 |
|---|---|
| 数据外发 | 本项目语料含"健康档案"类敏感内容 → **默认不走云端** |
| 触发条件 | 只在**本地 OCR 抽样置信度低于阈值**时才考虑，而不是默认路由 |
| 若必须用 | 脱敏后上传，或改用自建 GPU |
| 费用 | 按量计费，模糊件通常占少数，可接受 |

**无论走哪条路，"处理失败"必须是显式状态**：
识别不出内容时，产出 `kind: "unprocessable"` + 原因，**不要入库一个空 chunk**。
这是 v2 §2 第 7 条"拒绝不能变成静默"的延续——**静默失败是 RAG 最贵的 bug**，因为它表现为"检索不到"，而不是报错。

---

## 6 图片 → VLM 描述：注入什么，用什么 prompt

### 6.1 输入构造（把你的"图片 + 前后文"具体化）

```python
vlm_input = {
    "image": <裁剪出的图>,
    "context": {
        "section_path": "第三章 > 出口情况 > 主要贸易伙伴",   # 来自 HybridChunker 的 headings
        "preceding_text": "<图前最近一段文本，截尾 200 字>",
        "following_text": "<图后最近一段文本，截首 200 字>",
        "caption": "<Docling 抽到的图注，若有>",
        "predicted_class": "line_chart",                    # Docling 图片分类
    },
}
```

**为什么前后文都要**：图注常缺（尤其电子版 PDF 的图），而"这张图在讲什么"往往在**正文里被引用**（"如图 3 所示，出口量持续增长"）。前文给主题，后文给结论。

### 6.2 要求结构化输出

让 VLM 按固定 schema 输出，而不是自由文本——**结构化输出才能被校验**：

```json
{
  "chart_type": "折线图",
  "title": "2019-2023年出口额变化",
  "axes": {"x": "年份（2019~2023）", "y": "出口额（亿元，0~2000）"},
  "legend": ["机电产品", "纺织品"],
  "key_data": ["2023年机电产品约1800亿元", "纺织品类持续下降"],
  "trend": "整体上升，2022年回落",
  "description": "图3：折线图，横轴为年份2019~2023，纵轴为出口额0~2000亿元；机电产品持续上升至2023年约1800亿元，纺织品类自2021年起下降。"
}
```

`description` 字段**就是** `retrieval_text`；其余字段进 `structured_data` 供 Agent 精确读取。

### 6.3 必须做的校验

**VLM 是生成式的，会"编辑"而不只是"转写"。** 强制一条确定性校验：

- **行数守恒**：如果 `key_data` 声称"N 个数据点"，而原图数据点数量可数（柱状图/饼图易数），做交叉检查；
- **数值回查**：若同一数据在正文表格里出现过，比对 VLM 读出的数值与表格值是否一致；
- **不一致时以确定性来源为准** —— 这也是 §2.3 优先取 XML 的原因。

### 6.4 什么图才值得送 VLM（沿用 v2 §4，规则极简）

| 判据（按序） | 结论 |
|---|---|
| 属于 `furniture`（页眉/页脚/页码） | ❌ 丢弃 |
| 有 caption | ✅ 送 VLM |
| `predicted_class` ∈ {`line_chart`, `bar_chart`, `pie_chart`, …} | ✅ 送 VLM |
| 正文出现"见图 N / 如表 N"引用 | ✅ 送 VLM |
| 面积占比 < 3% 且无 caption 且无分类 | ❌ 装饰，丢弃 |
| 其余（`predicted_class: "picture"` 的位图） | ⚠️ 标 `needs_vlm`，先记元信息 |

---

## 7 澄清：「附页面截图」与「ColPali」是两件事

你的原话把这两者写在了一起，这里拆开——**它们成本差三个数量级**。

| | A. 回答里附页面截图 | B. ColPali 视觉检索 |
|---|---|---|
| **解决什么** | 用户想"看到原文那一块" | 文字召回**召不回**时，用页面图像做召回 |
| **怎么做** | 用 chunk 的 `page_no + bbox`，把页面渲染成图后裁剪（PyMuPDF / Docling 的 `generate_page_images`） | 页面图像 → 多向量 patch 索引 → 用视觉相似度检索 |
| **成本** | **零模型、零 GPU**；仅在需要时渲染单页 | 每页产生**上千个 patch 向量**，索引体积与普通文本向量不是一个量级；**必须 GPU** |
| **触发条件** | chunk 含表格/公式/插图，或来源是扫描件 | 仅在"纯文本检索对某类文档持续失败"且该文档**只有图上有信息**时 |
| **本项目建议** | ✅ **默认开**，成本可忽略，引用可溯源 | ⏸ **暂不引入**（语料规模未到，`ARCHITECTURE.md` D15：< 1 万片段不引入向量库，更遑论多向量索引） |

**所以**："检索命中的 chunk 带页码与 bbox → 需要时渲染该页并裁剪 → 随回答返回图片"，这条链**完全不需要 ColPali**，也不该被 ColPali 的复杂度拦住。

**ColPali 的正确提问方式是**：*"有没有一整类文档，靠文本检索怎么都召不回，而人眼一看图就懂？"* 若答案是否，就不要引入。

---

## 8 切分：HybridChunker 已是最优默认，要升级就升级这两个

你问"有没有更厉害的可以介绍让我选"。结论：**换分块器没有收益，值得上的是另外两件事。**

| 方案 | 是什么 | 成本 | 本项目判断 |
|---|---|---|---|
| **Docling HybridChunker** | 结构优先 + token 预算兜底；保留标题层级（`meta.headings`）、跨块表格重复表头、`doc_items` 回溯 | **零 LLM 调用、免费、token 精确** | ✅ **默认就用它** |
| HierarchicalChunker | 纯按标题层级切，不看 token | 免费 | 标题层级深、段落短的文档可考虑；边界不如 hybrid |
| **父子块（Parent-Child / small-to-big）** | 小块（~250 token）入索引做精确匹配；命中后返回其父块（~1500 token）给模型 | 需索引层支持父子 ID 映射；**不需要模型** | ⭐ **最值得做的下一步**。动机：v2 的双轨产物解决了"表格读不懂"，父子块解决"chunk 太小没上下文" |
| **Contextual Retrieval**（Anthropic） | 每块前面加一句"它在全文中的位置/主题"再嵌入 | 每块一次 LLM 调用 | ⭐ **次值得**。官方实验：top-20 失败率 5.7% → 3.7%，配 BM25 后 → 2.9%。**本项目已有 HybridRetriever，加上它成本可控** |
| 语义切分（逐句 embedding 找主题边界） | 依据相似度跌落切 | 索引期每句一次 embedding；阈值要调 | ❌ 已有实测结论支撑不做（见 v2 §3）：**有段落结构的文档递归/Hybrid 已零截断**，语义切分解决的是不存在的问题 |
| Agentic / LLM 引导切分 | LLM 读完全文决定切点 | LLM 调用 + **非确定性** | ❌ 有 2026 年复现研究显示：**语料内检索上，简单结构法优于 LLM 引导法**；且非确定性会污染可重复性 |
| Unstructured.io | 多格式统一元素模型，切分是它的副产物 | Apache-2.0；硬 PDF 精度不如 2026 VLM 梯队 | ⚠️ 它的价值在**格式覆盖**（40% 邮件 + PPT 的混合语料），不在切分。本项目主力是 PDF/Word，收益不显 |
| Firecrawl | 云端网页抓取 + 解析 + 分块 | 云端 | ❌ 面向网页，不是文档解析方案 |
| LlamaParse | 托管解析，长文档强 | $1.25/1000 credits；**数据外发** | ⚠️ 隐私约束冲突；作为对照基准可以，不作主线 |

**一句话**：`HybridChunker`（结构）→ 叠加 **父子块**（上下文）→ 叠加 **Contextual Retrieval**（位置提示）。这三层是递进的，每层都有明确收益，且都不需要换解析器。

---

## 9 VLM 选型：先看清硬件现实

### 9.1 现状

```
GPU:   NVIDIA GeForce RTX 5060 Laptop GPU, 8151 MiB   ← 有，但只有 8GB
torch: 2.11.0+cpu                                    ← CPU 版！cuda_available=False
docling: 未安装
```

**这解释了为什么"本地 VLM"目前跑不起来**：不是没有卡，是 torch 没编译进 CUDA。

### 9.2 三条路

| 路线 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **① API（推荐起点）** | 用项目已有的 `agnes-2.5-pro`（**实测支持视觉输入**，`agnes-image-*` 是生图模型不是看图） | 零部署、零显存占用、中文可用 | **数据外发**；按 token 计费；需确认敏感语料政策 |
| **② 本地小模型** | 重装 CUDA 版 torch（RTX 5060 是 Blackwell / sm_120，需 **cu128** 轮子）→ 跑 Qwen2.5-VL **3B**（bf16 约 6.5~7GB，勉强）或 7B **int4**（约 6GB，更稳） | 数据不出机器；离线可用 | 需下载 ~2.5GB torch + 模型权重；8GB 显存下 **Docling 模型与 VLM 不能同时驻留**，必须串行（Docling 跑完释放显存再跑 VLM） |
| **③ Docling 内置描述** | `do_picture_description=True` + `PictureDescriptionVlmOptions(repo_id=...)` | 零代码，与解析同流程 | 默认模型极小（SmolVLM-256M 级），**中文与复杂图表能力弱** |

**建议顺序**：① 起步验证效果 → 若因隐私必须本地，再走 ②。

**③ 的巧用**：可以把 ③ 当**筛选器**（"这张图有没有信息"），把 ① / ② 当**精描器**。粗筛用便宜模型，只对通过的图跑好模型。

**不推荐 Llava**：中文能力弱，而本项目语料是中文。

### 9.3 先算量再决定

送 VLM 是**按图计费**的。决定前先统计一次：

```
语料集合中共多少份文档
  → Docling 跑一遍，统计 predicted_class 为图表的 picture 数
  → 其中多少有 caption（可省一次 VLM 调用）
  → 得出"需要 VLM 的图"总量
```

一张图约 800~1500 输入 token（含图片）+ 300~500 输出。**一万张图就是千万级 token** —— 这个数字应该在做决定前拿到，而不是在账单上看到。

---

## 10 统一产物：KnowledgeChunk

v2 §6 的结构继续有效，v3 增加 4 个字段以承载本次扩展：

```json
{
  "chunk_id":       "sha1(source + page + kind + bbox)",
  "source":         "report.docx",
  "page":           7,
  "kind":           "text | table | chart | diagram | formula | unprocessable",
  "section_path":   "第三章 > 出口情况",
  "bbox":           [l, t, r, b],
  "content":        "事实内容（表格 Markdown / LaTeX / 原文）",
  "retrieval_text": "面向检索的自然语言（用于向量化）",
  "tags":           ["折线图", "出口量", "增长趋势"],

  "page_total":     12,
  "parser":         "docling | mineru | textin",
  "needs_vlm":      false,
  "file_checksum":  "...",

  "table_md":       "| 年龄 | 给付比例 |\n|---|---|\n| 18-40岁 | 160% |",
  "vlm_desc":       {"chart_type": "折线图", "trend": "整体上升", "description": "图3：…"},
  "source_image":   "extracted/report_p7_img2.png",
  "origin":         "docling_docx | docling_pdf | docx_zip | rendered_pdf",
  "parent_id":      "sha1(...)",           ← 父子块用
  "unprocessable_reason": null             ← 拒绝要显式，不静默
}
```

**四个新字段各自的用途**：

- `table_md` / `vlm_desc`：把"事实"与"模型产出"分开存。**模型产出绝不能覆盖事实**——一旦 VLM 读错，你还能拿 `content`/`table_md` 纠正它；
- `origin`：告诉下游这个 chunk 是"直读结构化所得"还是"渲染后识别所得"。**同一份文档里两者精度不同**，排查坏 chunk 时这是第一手线索；
- `parent_id`：为 §8 的父子块预留，现在留空不影响上线；
- `unprocessable_reason`：显式失败状态。

---

## 11 分阶段落地

在 v2 的 4 批基础上扩展，**批次顺序按"能否独立交付价值"排列**：

| 批次 | 内容 | 验收标准 | 依赖 |
|---|---|---|---|
| **1** | **切分统一**：`_make_splitter`（`src/retrieval.py:144`）补中文分隔符 | 无换行长段落**零句中截断**；现有 101 测试全过 | 无 |
| **2** | **接入 Docling（PDF）**：`pip install docling` + `docling-tools models download`（设 `DOCLING_ARTIFACTS_PATH`）；写 `src/docling_loader.py` | 一份含表格+图表的 PDF → 打印每页 text/table/picture/formula 数量与 bbox；`len(doc.tables)` 与人工清点一致 | 无（与 1 并行） |
| **3** | **转换层**：`src/knowledge_chunk.py` —— 双轨产物、表格行列文字化、`section_path`、图片价值判定 | 表格数值能被自然语言查询命中；未处理项标 `needs_vlm` 而非丢弃 | 2 |
| **4** | **接入主流程**：改 `load_chunks` 的 `.pdf` 分支；`chunk_writer.to_legacy()` 保旧字段兼容 | 端到端：检索命中表格数值与图表 caption | 3 |
| **5** | **docx 支持**：Docling docx 后端（路线 A）+ `src/docx_media.py`（路线 C，解 zip 取图片/OLE/chart XML） | 一份含原生图表 + 截图 + OLE 的 docx → 图表数据从 XML 精确读出；图片全部导出；位置（第几段之后）可还原 | 4 |
| **6** | **图片 VLM 描述**：`src/image_describer.py`，先接 API（agnes-2.5-pro） | 图表的 `vlm_desc.description` 可被人读懂；数值与表格交叉校验通过 | 5 |
| **7** | **扫描件**：Docling + OCR 中文配置；用实测决定是否需要 MinerU | 一份扫描件能抽出表格且数值人工核对通过 | 4 |
| **8**（可选） | **父子块 / Contextual Retrieval** | 检索评测集上 Recall@k 提升可测 | 7 |

**为什么 1 排最前**：零新依赖、零风险、改动只有一处配置，且是后续所有批次的地基。
**2 与 1 相互独立**，可并行。

**进度（截至 2026-09-22）**

| 批次 | 状态 | 验证方式 |
|---|---|---|
| 1 切分补中文分隔符 | ✅ 已落地 | 单元测试：遍历多个 chunk_size，断言无句中硬切 |
| 2 接入 Docling（PDF） | ✅ 已落地 | `scripts/smoke_docling.py`：含表格 PDF → `len(doc.tables)=1`，Markdown 还原数值 |
| 3 KnowledgeChunk 转换层 | ✅ 已落地 | 单元测试 46 项（含引用解析、表格行列文字化） |
| 4 接线 load_chunks + 兼容层 | ✅ 已落地 | 旧契约测试全过；`to_legacy` 契约单测 |
| 5 docx 双路线（A+C） | ✅ 已落地 | `scripts/check_docx.py` 七项确定性核对全通过；集成测试在 `tests/test_docling_loader.py` |
| 6 图片 VLM 描述 | ⏸ 未做 | 前置已就绪：图片已落盘、`needs_vlm` 已标记 |
| 7 扫描件深化（中文 OCR 参数 / MinerU 判定） | ⏸ 未做 | 通路已验证（`scripts/check_ocr.py`），缺真实语料 |
| 8 父子块 / Contextual Retrieval | ⏸ 可选 | — |

**验证方式的分层**（这是本项目刻意维持的）：零依赖的纯函数单测默认跑；需要 docling + 模型权重的集成用例用 `pytest.importorskip` 隔离，且**只在"权重没下齐"时 skip，其他失败照常变红** —— 否则真实回归会被一句 skip 掩盖。

---

## 12 明确不做 / 待确认

| 项 | 决定 | 理由 |
|---|---|---|
| 自研布局检测 / 表格识别 | ❌ 不做 | Docling 已覆盖且更好 |
| 自研 PDF 文字抽取（保留 pdfminer 链路） | ⚠️ 过渡期保留 | Docling 接入后 PDF 走新链路；旧链路留作对照，稳定后再删 |
| ColPali | ⏸ 不做 | 成本与语料规模不匹配（§7） |
| 语义切分 / Agentic 切分 | ❌ 不做 | 已有实测结论 + 非确定性代价（§8） |
| 跨页表格合并 | ⏸ 暂不做 | 需 VLM + 确定性校验，风险高 |
| 引入独立向量库 | ⏸ 不做 | 语料规模未到（`ARCHITECTURE.md` D15） |
| Firecrawl / LlamaParse 作主线 | ❌ 不做 | 前者面向网页，后者数据外发 |

**待你确认的 4 件事**（其中 1/2/3 已定，见括号）：

1. **VLM 走 API 还是本地**？→ 取决于语料敏感度与是否有隐私要求（§9.2）（**已定：走 API，agnes-2.5-pro**，见批次 6）
2. **docx 走哪条路线**？→ 建议 A + C；是否需要 B（转 PDF）取决于 docx 里有没有 SmartArt 这类 C 复原不了的形态（**已定：A + C**；B 暂不引入，遇到 SmartArt 再说）
3. **Word 图表数据**：确认用"直接读 chart XML"而非"VLM 看图"（**已定：读 XML**，实测见附录 H）
4. **扫描件是否需要 MinerU**：先用你的真实扫描件跑 Docling 一遍（看 `len(doc.tables)`），用数据而非榜单决定（**仍未定，缺真实语料**）

---

## 13 相对你原方案的净变化（一页速览）

| 你的方案 | v3 保留 | v3 修改 |
|---|---|---|
| Word 用 Docling 提文本/表格/图片/图表/OLE | 文本 + 普通表格 ✅ | 图片/OLE/图表**改走解 zip**；Docling 在 docx 上拿不到这些，且无位置信息 |
| PDF 用 Docling，表格 ACCURATE，导图，公式可选 | ✅ 全部保留 | 参数写法修正；补齐 5 个必须显式打开的开关 |
| 图片 + 前后文 → VLM → 结构化描述 | ✅ 保留 | 给出 schema、校验规则、筛选判据 |
| 扫描件 MinerU > Docling | ✅ 作为实测备选 | **默认仍 Docling**——混用会让 chunk 元信息模型分裂；换后端前先用 `len(doc.tables)` 验证 |
| 模糊件用 TextIn 合合 | ✅ 保留 | 设为条件触发而非默认路由；数据外发需脱敏 |
| 条件触发 ColPali + 返回图片 | 返回图片 ✅ | **拆开**：附截图零成本默认开；ColPali 暂不引入 |
| 切分用 HybridChunker | ✅ 保留 | 升级方向改为父子块 + Contextual Retrieval |
| 原文 + 表格 + 图片描述合并入库 | ✅ 保留 | 事实与模型产出**分开存字段**；失败显式标记 |

---

## 附录：实测环境记录（2026-09-22，本机实跑）

实装后发现网上多数示例与本机实际情况不符，记录如下，避免重复踩坑。

### A. 版本与 API 形态

实测：`docling 2.129.0` / `docling-core 2.98.0` / `docling-ibm-models 4.0.3` / `docling-parse 7.21.0` / `transformers 5.3.0` / `tiktoken 0.12.0` / `torch 2.11.0+cpu`。

| 项 | 常见写法 | 本机实测 |
|---|---|---|
| docx 的 format option | `DocxFormatOption` | **`WordFormatOption`**（`docling.document_converter`） |
| `do_cell_matching` | 文档里常提到 | **字段不存在**，设了会报错 |
| `enable_parallel_processing` | 常被推荐用于提速 | **字段不存在** |
| `HybridChunker.chunk()` | `chunk(doc)` / `chunk(dl_doc)` | 参数名是 **`dl_doc`** |
| `HybridChunker.__init__` | `HybridChunker(max_tokens=...)` | **不收 `max_tokens`**，token 预算在 tokenizer 里设 |
| `HuggingFaceTokenizer` | 位置参数传 tokenizer | 必须用关键字：`HuggingFaceTokenizer(tokenizer=..., max_tokens=...)` |
| `download_models(output_dir=...)` | 传字符串路径 | **必须传 `Path`**，传 str 会 `AttributeError: 'str' object has no attribute 'mkdir'` |

因此 `src/docling_loader.py` 统一采用"先探测再使用"的写法（`_set()` 只在字段存在时赋值），升级 docling 不必改代码。核对脚本：`scripts/check_docling_env.py`。

### B. 网络：HuggingFace 直连不可用

```
https://huggingface.co    → 超时
https://hf-mirror.com     → 200 ✅
https://api.siliconflow.cn → 可达（embedding 与 VLM 不受影响）
```

**结论**：模型下载必须设 `HF_ENDPOINT=https://hf-mirror.com`（已写入 `.env`）。这是社区镜像，生产环境需自行评估可用性与合规性。

### C. ⚠️ HuggingFace 缓存机制在本机会失败（两个叠加的问题）

**问题 1：符号链接退化成 0 字节文件。**
HF hub 会在 `snapshots/` 下创建指向 `blobs/` 的符号链接。当前环境不允许创建链接时，hub 留下了**真实的 0 字节文件**，而 `blobs/` 里的内容完好（16MB 级）。表现是模型加载报：

```
RuntimeError: Failed to load image processor from ...: Expecting value: line 1 column 1 (char 0)
```

那个 JSON 解析错误就是读空文件造成的。

**问题 2：下载过程的删除操作被拦截。**
hub 每下一个文件都要创建再删除自己的 `.lock`，一趟 `download_models` 累计 50+ 次删除。受保护环境会在此处中断，下载永远停在 `.incomplete` 状态（实测卡在 `docling-layout-heron` 的 171MB 主体）。

**解法：`scripts/fetch_docling_models.py`** —— 用普通 HTTP GET 把文件直接写到最终位置，全程零删除操作，同时规避以上两点。产出目录结构与官方 `download_models` 一致，可直接配 `DOCLING_ARTIFACTS_PATH`。

```bash
python scripts/fetch_docling_models.py models/docling
```

已下载的文件会跳过，可反复重跑。实测总量约 1.4 GB（36+4 个文件）。

**踩坑记录（都是实际跑出来的，不是推测）：**

| 现象 | 根因 | 修法 |
|---|---|---|
| tableformer 四个文件报 `FileNotFoundError: ...safetensors.part` | `download_file()` 直接在嵌套路径上 `open(part,"wb")`，没先建父目录。`model_artifacts/tableformer/accurate/` 这种多层路径必然失败 | 写文件前 `dest.parent.mkdir(parents=True, exist_ok=True)` |
| RapidOCR 报 `artifacts not found or incomplete` | **RapidOCR 权重不在 HF 上**，地址来自 `rapidocr` 自己的注册表，原清单里根本没有它 | 单独走一条路：复用 docling 的 `RapidOcrModel.download_models()`（查注册表 + `requests` 普通 HTTP，天然无 HF 缓存问题） |

RapidOCR 的 `backend`/`lang` 必须与运行时一致，否则文件名对不上仍会报同一个错。docling 默认 `ocr_options` 是 `OcrAutoOptions`（`kind=auto, lang=[]`），在 Windows/CPU 上落到 `onnxruntime:ch`，预取默认值已与之对齐。不需要 OCR 时用 `--skip-rapidocr` 省一份权重。

### C2. ⚠️ 配置读取依赖 import 顺序（隐蔽，已修）

`src/docling_loader.py` 的配置是在 **import 时**读成模块常量的，而 `.env` 原先只在 `src/retrieval.py` 里加载。于是同一个模块出现两种行为：

```
pytest tests/            → conftest 经 src.graph 走到 retrieval，.env 已加载 → 用本地权重，正常
scripts/smoke_docling.py → 只 import docling_loader，.env 未加载 → 转去联网下模型 → ProxyError 502
```

排查成本极高：同一个模块、同一份代码，换个入口就变行为。**修法**：在 `docling_loader` 里也加载一次 `.env`（`load_dotenv` 默认不覆盖已有变量，重复调用无副作用），不再依赖调用方先 import 谁。

> 更彻底的做法是把这些常量改成惰性读取（用到时才 `os.getenv`），但那会牵动整个模块的读写方式，留待后续重构。

### D. 本机资源

| 项 | 值 | 影响 |
|---|---|---|
| GPU | RTX 5060 Laptop, 8151 MiB | 存在，可跑小 VLM（3B bf16 / 7B int4） |
| torch | `2.11.0+cpu` | **Docling 走 CPU**，每页秒级开销；要用 GPU 需重装 cu128 轮子（Blackwell / sm_120） |
| 模型权重 | 需约 400~600 MB 磁盘 | 已 gitignore（`models/`） |

### E. 相关脚本

| 脚本 | 用途 |
|---|---|
| `scripts/check_docling_env.py` | 核对版本与 API 形态是否与适配层假设一致（升级 docling 后先跑） |
| `scripts/fetch_docling_models.py` | 绕过 HF 缓存机制下载模型（受限环境必备）；含 RapidOCR 预取 |
| `scripts/smoke_docling.py` | 端到端烟测：现场造含表格的 PDF，打印元素统计与耗时 |
| `scripts/check_ocr.py` | OCR 链路自检：造"纯图片 PDF"，确认 OCR 真能捞回文字 |
| `scripts/check_docx.py` | docx 双路线自检：造含原生图表+截图+OLE 的 docx，逐项核对图表数值/替代文字/OLE 内层正文/位置锚点 |

### F. ⚠️ 切块器会静默丢内容（实测缺陷，已修）

**现象**：扫描件 OCR 成功后，`parse_document` 仍返回 `unprocessable`，理由是"疑似扫描件且未启用 OCR"——但 OCR 明明是开着的、日志里三个 onnx 权重都加载了。

**逐步定位**（`scripts/check_ocr.py` + 一次性诊断脚本）：

| 层 | 观察 | 结论 |
|---|---|---|
| docling 转换 | `status=success`，`len(doc.texts)=1`，文本 = `'Hello Docling World Invoice Number 2024'` | OCR 完全正常 |
| HybridChunker | `raw chunks = 0`，**且不抛异常** | 内容在这一层被丢掉 |
| parse_document | 拿到空 chunks → 判 unprocessable | 已有内容被替换成 `[未能解析]` |

**根因**：HybridChunker 对"通篇只有标题、没有正文 item"的文档返回 **0 块**（标题负责开章节，章节里没有 body 就不产出任何块）。OCR 把整页文字识别成 `section_header` 时正好命中。而我们拿到 0 块后据此判定"解析不出内容"，把已经抽到的文字丢了——**静默数据丢失**，且报错原因把排查方向带偏到"OCR 没开"。

**修法：四级切分阶梯**（`src/docling_loader.py::to_knowledge_chunks`）

| 级 | 切分器 | 定位 |
|---|---|---|
| 1 | `HybridChunker` | token 预算 + 结构感知，正常路径 |
| 2 | `HierarchicalChunker` | 纯结构切，不做 token 合并 |
| 3 | 按 item 逐条（`knowledge_chunk.from_docling_items`） | 粒度最好，跳过标题 |
| 4 | 整文档 Markdown 单块（`from_docling_markdown`） | **保证不变式：文档有内容 → 一定有块** |

配套把 `parse_document` 的判定拆开：只有文档**真的**没抽出东西（`texts/tables/pictures` 全空）才判 `unprocessable`；"抽到内容却切不出块"单独报，reason 明确写成实现问题，不再误报成扫描件。

**顺带的两个实测坑**：

1. **docling 的 item 上没有 `export_to_text`**。只有 `export_to_doctags` / `export_to_document_tokens`（混着坐标的结构标记流，不能当正文）。正文在各 item 的 `.text` 上，表格/图片才走 `export_to_markdown`。
2. **`OcrMode` 默认不是全页 OCR**。`OcrMode.DEFAULT` 实为 `PDF_AWARE_LAYOUT_REGIONS`：先取版面检测的簇，再剔除已被 PDF 文字覆盖的。本次实测三种模式（DEFAULT / FULL_PAGE / LAYOUT_REGIONS）在本样本上都能捞回文字，故暂不改默认；但**版面模型漏检的扫描件需要 `mode=OcrMode.FULL_PAGE`**，这是后续遇到真实坏件时的第一个旋钮。

### G. ⚠️ `chunk.meta.doc_items` 装的是「引用」而非元素（影响批次 3/4，已修）

**现象**：`kc.table_md` 一直是空串，`linearize_table()`（本项目"表格数值可被自然语言问句命中"的核心设计）**从未真正执行过**；图片分类恒为空、全部落到"diagram + 需 VLM"。

**根因**：`HybridChunker` 产出的 `chunk.meta.doc_items` 里，元素是 `DocItem` **轻量引用**——只有 `self_ref`（如 `#/tables/0`）、`label`、`prov`，**没有 `.text`、没有 `export_to_markdown`、没有 `annotations`**。而引用上恰好还有个 `prov`，所以"有坐标"这条线索看起来正常，掩盖了其余能力全在走回退分支。

| 我们依赖的能力 | 只拿引用时 | 后果 |
|---|---|---|
| `item.export_to_markdown(doc)` | `AttributeError` → 被 `try/except` 吞掉 | `table_md` 空，`linearize_table` 拿到空串 |
| `item.text` | 不存在（`None`） | 混排块的正文丢失 |
| `item.annotations`（图片分类） | 不存在 | 图片分类永远失效，VLM 预算被浪费 |

**为什么一直没暴露**：docling 自己的 `chunk.text` 会把表格序列化成 `East, Revenue = 1200. West, Revenue = 980` 这种准自然语言，检索效果"看着还行"，于是"内容不为空"掩盖了整条链路在走回退分支。

**修法**：新增 `knowledge_chunk._resolve_items(items, doc)` —— 按 `self_ref` 在建好的索引里把引用解析回完整元素，**并保留引用带来的位置信息**（引用带 `prov`，完整元素不一定有；直接替换会导致 `page` 变 `None`，把位置信息丢掉 —— 所以缺 prov 时 `copy.copy` 后补上，不改库对象）。

```python
# 关键点：引用提供位置（prov），元素提供内容（text / export_to_markdown）
resolved = _resolve(items, doc)   # 按 self_ref 换回完整元素，换不到就用引用自己
pages, boxes = _collect_prov(items + resolved)   # 两边都收，避免丢位置
```

**教训（可复用到任何"框架对象字段"的假设上）**：拿到第三方的结构化对象时，先 `print(type(obj), obj.model_dump() if hasattr(...) else dir(obj))` 看清它到底是什么，再写消费代码。本项目为此踩了两次坑：`WordFormatOption`（猜 API 名字）与本次（猜字段存在）。

### H. docx 双路线实测（批次 5，已落地）

**实现**：`src/docx_media.py`（路线 C，纯标准库）+ `docling_loader.parse_docx()`（A+C 组合）+ `knowledge_chunk.from_docx_media()`（产物层）+ `retrieval._parse_docx()`（接线，含 python-docx 降级）。

实测一份含"原生折线图 + 截图 + 嵌入 Excel"的 docx（手拼包，XML 按真实 Word 形态写）：

| 产物 | 结果 |
|---|---|
| 图表 | `kind=chart`，类目/数值来自 `chart XML` 的 `strCache`/`numCache`，**精确**：`2019=120、2020=145、2021=138、2022=180`；`needs_vlm=False`（数据是确定性的，不该花钱） |
| 图片 | `kind=diagram`，替代文字（`wp:docPr@descr`）**免 VLM**；原件落盘到 `data/.media/<词干>_<源路径指纹>/` |
| OLE | 内层 `.xlsx` 被解包并用 `loaders` 解析出正文 → 升级为 `kind=table`，标签 `已解析原件` |
| 位置 | 每个媒体块带 `para_index`（文档流第几段）+ `anchor_text`（本段文字，空则回退上文最近非空段落），按锚点插回正文序列 |

**三个实测坑（都已修）**：

1. **Docling 把整篇正文和图表塞进同一个块，然后按"含图片"判成 diagram** → `retrieval_text` 只剩一句"图表"，**整篇正文失去检索入口**（正文只在 `content` 里，而检索层只认 `text`）。
   修法：混排块按内容降级——有正文就保留正文进 `retrieval_text`，同时清掉 `needs_vlm`（图片已由 C 路线索引，块里也没有图像字节）。
   顺带修好同类缺陷：`_kind_for_items` 原来"含表格就只输出表格、含图片就只输出一句图表"，同样会把同块正文挤出检索文本。
2. **OLE 对象的 VML 预览位图会重复入库**。Word 插入 OLE 时同时写 `<v:imagedata>`（预览位图，常为 EMF）与 `<o:OLEObject>`（原件），**在同一个段落里**。
   修法：`docx_media._mark_ole_previews()` 在"引用形式是 VML `imagedata`"且"同段落 OLE 的内层正文确实解析出来了"两个条件同时成立时才标记去重；原件没解出来时保留预览（那份图可能是唯一的视觉线索）。
3. **`[Content_Types].xml` 里缺主文档 `Override`** 时 python-docx 会直接判 `Input document is not valid`，Docling docx 后端同样拒收。手拼夹具必须带这条，否则测的是夹具不是解析器。

**速度参考**（本机 CPU）：docx 链路单份样件约 1 分钟级（含 docling 模型加载），因此 `tests/test_docling_loader.py` 里的 docx 用例与 `scripts/check_docx.py` 一样，属于"能 skip 的集成层"，不放进零依赖单测。
