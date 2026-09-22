# 文档解析工具对比（Docling 同类生态位）

> 整理日期：2026-09-21
> 数据来源：各项目官方文档 + 第三方评测（OmniDocBench v1.6 / olmOCR-Bench）+ 选型评测文章
> ⚠️ 版本、榜单分数、价格**变动很快**，正式选型前请回各自仓库确认。

---

## 0 先说分类框架

同类工具分**三类**，这不是程度差异，是**架构差异**：

| 类别 | 做法 | 代表 | 特点 |
|---|---|---|---|
| **A 流水线式** | OCR → 版面分析 → 表格/公式识别 → 结构化输出，各步独立可控 | **MinerU**、**Docling**、**PPStructure**、**Unstructured** | 模块化、可替换单步；配置复杂 |
| **B 端到端微调模型** | 视觉语言模型在文档数据上微调，图直接进、结构化出 | **Marker**、**Nougat**、**olmOCR**、**GOT-OCR 2.0**、**SmolDocling** | 精度高、开箱即用；算力需求高、可控性低 |
| **C 通用多模态大模型** | 直接 prompt 通用 VLM | GPT-4o、Gemini 2.x、Qwen2.5-VL、InternVL | 无需微调；按 token 计费、不可定制 |

**注意 Docling 属于 A 类**，它的"理解"来自多阶段专用模型（Layout Model + TableFormer + OCR + 可选 VLM），**不是把整份 PDF 扔给一个大模型**。

---

## 1 自托管工具对比

| 工具 | 许可（代码 / 权重） | OmniDocBench v1.6 | 硬件门槛 | 最适合 |
|---|---|---|---|---|
| **Docling**（IBM） | **MIT** / Apache-2.0 | 未列入榜单 | **纯 CPU 可跑，无 GPU 依赖** | 混合格式、内网/离线、许可要干净 |
| **MinerU**（上海 AI Lab） | MinerU 开源许可（Apache 系自定义）/ Apache-2.0 基础 | **86.47**（pipeline）<br>**95.26~95.75**（VLM/hybrid） | CPU 可跑（86.47 那档）<br>VLM 后端需 **8GB VRAM**；建议 16~32GB 内存 + ~20GB 磁盘 | **中文/复杂版面综合首选** |
| **PaddleOCR-VL**（百度） | **Apache-2.0** | **96.34（最高）** | 需 GPU | **精度优先** |
| **Marker 2** | Apache-2.0 / **RAIL-M（有门槛）** | 78.44 | 首次启动本地推理服务（vLLM / llama.cpp）<br>单 B200：23.7 页/秒（关 OCR）；balanced 模式 2.9 页/秒 | **吞吐量优先** |
| **olmOCR 2**（AI2） | **完全 Apache-2.0（含权重）** | 82.4（自测） | 12GB VRAM / 30GB 磁盘 | 学术文档、许可最宽松 |
| **Unstructured** | Apache-2.0 | 未列入；硬 PDF 上不如 2026 VLM 梯队 | 中等 | **20+ 格式统一元素模型**；策略可切 `auto` / `hi_res` / `fast` / `OCR-only` |
| **SmolDocling** | — | — | **256M 参数，可边缘部署** | 资源受限 / 边缘 |
| **Nougat**（Meta） | — | — | — | 公式密集的学术论文；**泛化差** |
| **GOT-OCR 2.0** | — | — | 高算力 | 复杂排版、开源 |
| **PyMuPDF / pymupdf4llm** | **AGPL-3.0 或商业授权** | — | **极轻** | 简单 PDF 快速转换；**但有 AGPL 传染风险** |
| **DeepSeek-OCR 2** | — | — | — | 文字压成视觉 token，约 **10× 压缩 / ~97% 精度**；本质是省 token 成本的方案 |
| **Tesseract** | Apache-2.0 | — | CPU | 干净扫描件够用，仅此而已 |

**关键差距**：
- **精度**：PaddleOCR-VL (96.34) > MinerU VLM (95.75) > MinerU pipeline (86.47) > olmOCR (82.4) > Marker (78.44)
- **CPU 可行性**：Docling ✅（设计上就支持）· MinerU ✅（pipeline 后端）· Marker ✅（v2 加了 CPU）· PaddleOCR-VL ❌
- **中文**：MinerU 与 PaddleOCR-VL 明显更强（都是中文团队）；Docling 是 IBM 出品，英文/欧洲语系更擅长

---

## 2 托管 API 对比

| 服务 | 价格 | 备注 |
|---|---|---|
| **LlamaParse** | **$1.25 / 1000 credits** → $0.00125/页（Fast）~ $0.05625/页（Agentic Plus） | 最便宜的可用托管；新账号 10,000 免费；**48 小时内重传同一文件免费** |
| **Unstructured（托管）** | **$0.015 / 页**，10,000 页免费 | 比云厂商便宜；开源库仍 Apache-2.0 |
| **Reducto** | $15 / 1000 标准页，$30 复杂页（2026-09-01 起），agentic 翻倍 | 适合"**表格读错有金钱代价**"的场景 |
| **Mistral Document AI** | $4 / 1000 页（Batch API $2），170 种语言 | ⚠️ 营销 vs 实测差很大：厂商自报 85.20，Ai2 独立测 **72.0** |
| **云厂商（AWS Textract / Azure / Google）** | 纯 OCR **$1.50** / 1000 页（量大 $0.60）；layout-aware **$10~15** / 1000 页 | 三家价格已趋同；Google 免费额度最实用（1000 页/账期） |

**托管 vs 自托管的分界**：差别不在精度（2026 年自托管已不输），而在**运维成本 vs 数据外发**。

---

## 3 ⚠️ 许可陷阱（最容易踩，单独列一节）

**核心问题：代码许可 ≠ 权重许可。** 仓库上写着 Apache-2.0，不代表你能用它的模型。

| 工具 | 陷阱 |
|---|---|
| **PyMuPDF / pymupdf4llm** | AGPL-3.0 或 Artifex 商业授权。**AGPL 第 13 条在网络服务场景会触发**——你把它包成服务对外提供，就要开源你的代码。这是**最危险的一个**，因为它常常通过依赖间接引入 |
| **Marker / Surya / Chandra**（Datalab 家族） | 代码 Apache-2.0，**权重是修改版 OpenRAIL-M**。Marker 与 Surya 标注 **5M USD 门槛**（营收或融资），**Chandra 是 2M USD**，且**禁止用于与其 API 竞争的产品** |
| **MinerU** | >1 亿 MAU 或 >2000 万美元月收入需商业许可；**且署名条款不设门槛**——若把 MinerU 支撑的解析作为服务提供给第三方，**必须在产品/公开文档中显著标注**，不合规许可自动终止 |
| **Docling** | MIT + Apache-2.0（granite-docling 权重），**无明显陷阱** |
| **olmOCR 2** | 完全 Apache-2.0（含权重），**最干净** |
| **PDF-Extract-Kit** | AGPL-3.0，通过 YOLO 和 PyMuPDF **继承 AGPL** |

**三条要内化的**：
1. Datalab 家族把**宽松代码与受限权重分开**——看仓库许可没用，要读你**锁定的那个版本**里的 license 文件；
2. MinerU 的门槛多数团队碰不到，但**署名条款与收入无关**；
3. **AGPL 通常从后门进来**（依赖引入），尤其 PyMuPDF。

---

## 4 一个通用原则：**Markdown 是展示格式，不是存储格式**

一旦把页面扁平化成 Markdown，你同时丢掉了：

- **页码** —— 引用溯源要用
- **bounding box** —— 定位回原文要用
- **块类型**（标题/正文/表格/图）—— 按类型重排要用
- **置信度** —— 调试坏 chunk 要用

而这些恰恰是后面都要用到的字段。

> **正确做法：持久化存结构化输出，渲染 Markdown 只在切块时做。**

**所有严肃工具都支持这一点**，其中 **Docling 的 `DoclingDocument` 是最完整的版本**——带 provenance 的类型化树，可导出 Markdown / HTML / JSON / DocTags。这也是为什么它常被当作大型框架里的**摄取层**，即使实际识别用的是别的模型。

对照：
- **MinerU** 除了 Markdown，还输出**按阅读顺序排序的 JSON 内容列表**
- **Marker 2** 支持 `--output_format json`（块树 + 元数据）和 `--output_format chunks`
- **Unstructured** 输出元素级结构化 JSON

---

## 5 针对本项目的建议

### 现状约束

| 约束 | 说明 |
|---|---|
| **有 GPU 但 torch 是 CPU 版**（RTX 5060 Laptop / **8GB**，`torch 2.11.0+cpu`、`cuda_available=False`） | PaddleOCR-VL 出局（需 GPU 且显存更大）；MinerU 现状只能用 pipeline 后端（86.47 那档）。**重装 cu128 版 torch 后可解锁 CUDA 加速**，但 8GB 显存限制了可跑的 VLM 规模（见 `PLAN_doc_parsing_v3.md` §9） |
| **中文语料** | MinerU / PaddleOCR-VL 有优势 |
| **倾向本地/内网** | 托管 API 的数据外发是顾虑（之前出现过"健康档案.pdf"这类语料） |
| **许可要干净** | 不要 AGPL，不要有营收门槛的权重 |
| **已有 torch + transformers** | Docling 安装代价很小（只需新增约 30 个小包） |

### 结论

**主选 Docling，把 MinerU 作为中文场景的备选/升级路径。**

理由：

| 维度 | Docling | MinerU |
|---|---|---|
| 许可 | **MIT，最干净** | 自定义许可，有署名条款 |
| CPU | ✅ 设计支持 | ✅ 但 pipeline 后端精度降一档（86.47） |
| 中文 | 一般（IBM 出品） | **强** |
| 结构化输出 | `DoclingDocument` **最完整** | 阅读顺序 JSON |
| 格式覆盖 | PDF/DOCX/PPTX/XLSX/HTML/图片/音频 | 主要 PDF，另有 EPUB/MOBI/DOCX |
| 生态 | 被当作摄取层广泛集成 | Dify 原生集成 |

**什么时候换 MinerU**：如果实测发现 Docling 在**你的中文语料**上表格/版面精度不够，且你能接受它的署名条款——
那时可以只替换 `src/docling_loader.py`（方案里已经把这个设为唯一耦合点）。

**明确避开**：
- ❌ **pymupdf4llm** —— AGPL 传染风险
- ❌ **Marker** —— 权重有 5M USD 营收门槛（除非你确定长期低于这个线）
- ⚠️ **olmOCR** —— 许可最干净但**2026-03-25 后无提交**，停更风险
- ⚠️ **Mistral Document AI** —— 自报分数与独立实测差 13 个点

---

## 6 怎么快速自测一个解析器（实用技巧）

不要只看榜单——**解析器在学术论文上优秀、在扫描发票上可能很差**。用你自己的文档测，三步：

```python
# ① 转换后先看三个数
print(f"pages: {len(doc.pages)}")
print(f"tables: {len(doc.tables)}")     # ★ 最该盯的数
print(f"chars: {len(markdown)}")
```

**`tables` 这个数最关键**：如果你知道某份文档里有表格，解析器却报 **0**，说明它**把表格压成了散文**——你的检索质量在还没切块之前就已经受损了。

```python
# ② 用 diff 工具并排看输出，丢表和阅读顺序错乱会立刻显形
# ③ 抽查 10 页：表格数值、双栏顺序、公式、页眉页脚是否被清掉
```

**验收清单（建议抄进你的评测集）**：
- [ ] 已知有 N 张表的文档，解析出 N 张
- [ ] 双栏文档的阅读顺序正确
- [ ] 表格数值逐格核对（抽 10 个单元格）
- [ ] 公式转 LaTeX 后可渲染
- [ ] 页眉页脚被归入修饰层、未混进正文
- [ ] 每个元素带页码与 bbox
- [ ] 同一文件跑两次结果一致（可重复性）

---

## 7 一句话选型（按场景）

| 你的情况 | 选 |
|---|---|
| 有 GPU，精度优先 | **PaddleOCR-VL** |
| 想要一个能从 GPU 优雅降级到 CPU 的工具 | **MinerU** |
| **纯 CPU / 内网 / 许可要干净** | **Docling** ← 本项目 |
| 营收或融资超过 $5M | ❌ Marker 权重出局 |
| 零基础设施、成本优先 | LlamaParse（$0.00375/页） |
| 读错一个表有金钱代价 | Reducto（$15~30/1000 页） |
| 语料是 40% 邮件 + PPT 的混合体 | Unstructured（统一元素模型比 PDF 榜单上多 3 个点更值钱） |

---

## 8 2026 年最重要的一个变化

**自托管不再是精度的妥协。** 一个 0.9B 的开源权重模型已经能超过前沿 VLM——这是今年真正的结构性变化。

**含义**：选型的首要判据从"哪个精度高"变成了"**哪个许可能用、哪个能在我的硬件上跑、哪个输出结构最完整**"。
