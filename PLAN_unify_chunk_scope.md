# PLAN：统一分块口径（单块路径 → 拆分口径）

> 状态：**已实施**（2026-09-24）。拍板结论：**单口径**（不保留双路兼容），
> **公式单独拆出**成块。下文 §1–§3 保留当时的问题分析与论证，§7 是实施记录。
> 落地代价：含结构元素的块 id 全部带上元素前缀 → **需重建索引**。
> 触发问题：同一个 Docling 块，因"结构元素数量不同"走两条路，导致**同一个元素在不同路径下得到
> 不同的 kind / content 归属**。最典型的是公式：单独一块时 `kind=formula`，与 ≥2 张表同块时
> 变成正文串里的一截 LaTeX。

---

## 1. 口径分裂的完整清单

入口判据在 `src/knowledge_chunk.py:813`：`if len(tables) <= 1 and len(pictures) <= 1: return [from_docling_chunk(...)]`

| 维度 | 单块路径 `from_docling_chunk` | 拆分路径 `from_docling_chunk_all` |
|---|---|---|
| 谁成块 | 代表元素（第一个表 / 第一张图） | 每个表 / 每张图 **各一块** |
| kind 来源 | `_kind_for_items` 按优先级 table > picture > formula > text | 硬编码：`KIND_TABLE`(:600) / `picture_kind()` / `KIND_TEXT`(:866) |
| 公式 | 可单独成 `kind=formula`、`tags=["公式"]` | 落进正文块，`kind=text`、`tags=["混排块正文"]` |
| 其余文本元素 | 通过整块 `text` 兜底混进元素块；另由 sibling 收进 retrieval_text | 独立正文块（唯一一块） |
| `allow_broad_fallback` | `True`（整块 text 可作 content 兜底） | `False`（诚实留白 + `自身无内容` tag） |
| `with_sibling` | `True` | `False` |
| 原页截图 | 公式块命中 `CITE_IMAGE_KINDS` → 裁图 | 公式降成 text → **不裁图** |
| chunk_id anchor | `{pages}\|{boxes}\|{text[:64]}` | `t{i}\|…` / `p{i}\|…` / `body\|{section_path}\|{pages}\|…` |

**两处具体危害**

1. **公式的语义随块内邻居而变**：同一份文档里的同一个公式，是否被识别为"公式"取决于它跟几张表/图同块 —— 输入结构决定语义，而非元素自身性质。
2. **单块路径的 content 兜底把别人的内容装进元素块**：`allow_broad_fallback=True` + `chunk.text` 是整块所有元素序列化的拼接 → 表块的 content 可能含同块正文/公式。

---

## 2. 为什么现在没统一（三条硬约束）

**① 向下统一（拆分 → 混排）等于把已修的 bug 放回去。**
拆分路径修的是不可逆丢失：旧写法 `next(...)` 只取第一个元素，第二张表的 md 不生成，而 content 的 `... or text` 因 md 非空短路掉了兜底 → 内容 / 检索文本 / 磁盘产物**三处全无**。这条不能回退。

**② 单块路径不能"只关开关不拆结构"。**
看起来只要把 `allow_broad_fallback=False`、`with_sibling=False` 也搬到单块路径，口径就齐了。**做不到**：不拆的话，同块正文没有别的容身处，关掉兜底 = 正文直接丢。在单块路径里，"兜底"和"不拆"是一件事的两面。

**③ 改结构 → 块数与 anchor 变 → 全库重建。**
`from_docling_chunk` 的注释写明"与改造前逐字节一致（含 chunk_id 算法）"。现状是**用 id 兼容换口径分裂**：新逻辑只接管"多元素"这个新场景，老场景原样不动。这不是疏忽，是刻意的渐进迁移，代价是口径分叉且无告警（技术债）。

---

## 3. 改哪边：改单块路径，向拆分口径收敛

拆分口径是**有明确设计立场的**（见其 docstring）：一个 chunk 一个语义单元、内容归属诚实、宁留白不张冠李戴。单块路径的混排 + 兜底是有损的，只是因为 id 兼容被留着。
所以统一方向：**把"是否需要拆"的判据从数量改为语义**，让单块路径退化为"纯文本快通道"。

### 目标不变式

> 一个 DoclingChunk 里，**每个结构性元素（表 / 图 / 公式）各自成块，其余文本元素合成唯一一个正文块**；
> 元素块的 content 只含自己的内容，正文块承担全部非结构性文本。
> 只有当块内既无表、又无图、又无公式时，才走单块直通（此时它必然是纯文本块）。

统一后的 kind 映射（无例外）：

| 出口 | kind | 备注 |
|---|---|---|
| 表格块 ×N | `table` | 已有 |
| 图片块 ×M | `chart` / `diagram` | 由 `picture_kind` 判定，已有 |
| 公式块 ×K | `formula` | **新增出口** |
| 正文块 ×1 | `text` | 其余文本元素，已有 |
| 纯文本块 ×1 | `text` | 单块快通道，行为不变 |

`_kind_for_items` 不再参与"含结构元素"的块 —— 它的优先级陷阱（1 表 + 1 图 → 图被吞）随之消解。

---

## 4. 分阶段落地

### P0 —— 零 id 影响，先做可观测与可筛（不改结构、不改块数、不改 anchor）

1. **正文块 tags 记录来源 label 集合**：`tags=["混排块正文", "含公式"]`。
   `tags` 不在 anchor 里 → id 逐字节不变。作用：拆分路径下"按公式筛"的能力先保住。
2. **单块路径遇"该拆没拆"时打 warning**：
   判据 `len(formulas) > 0 or (tables and pictures)` → `logger.warning("块内含结构元素但走单块路径，口径与拆分路径不一致：…")`。
   作用：让隐含口径显形，先量出生产语料里到底有多少块落在分叉区。
3. **不改任何行为**，仅新增观测。

### P1 —— 统一口径（需全量重建）

1. 新增常量与分流函数：
   ```python
   _STRUCT_LABELS = ("table", "picture", "formula", "equation")

   def _struct_items(items):
       tables   = [i for i in items if _label_of(i) == "table"]
       pictures = [i for i in items if _label_of(i) == "picture"]
       formulas = [i for i in items if _label_of(i) in ("formula", "equation")]
       return tables, pictures, formulas
   ```
2. 入口判据改为语义判定：
   ```python
   tables, pictures, formulas = _struct_items(items)
   if not (tables or pictures or formulas):
       return [from_docling_chunk(chunk, doc, **kwargs)]   # 纯文本快通道
   ```
3. 新增公式出口（`from_docling_chunk_all` 内，与表/图循环并列）：
   ```python
   for i, f in enumerate(formulas):
       pages, boxes = _collect_prov([f])
       out.append(_build_formula_chunk(
           f, items=items, pages=pages, boxes=boxes, text=text,
           id_prefix=f"f{i}|", **common))
   ```
4. 抽出 `_build_formula_chunk`，让单块路径的 `KIND_FORMULA` 分支改为调用它 —— 避免出现第三份公式口径。取文本用 `.text`（`FormulaItem` 继承 `TextItem`，实测有 `.text`）；空则退回 `item_text()` 的容错链。
5. **exclude 同步**（关键配套，漏掉就会"同一 LaTeX 占两个召回位"）：
   ```python
   _OWN_TEXT_EXCLUDE = ("picture", "table", "caption", "formula", "equation")
   # 正文块
   body = _sibling_texts(items, exclude=("picture", "table", "formula", "equation"))
   ```
   注意：正文块**仍然不排 caption** —— 孤儿题注容器这条设计不变。
6. **anchor 换代**：拆分路径的 id 一律前缀化，且建议整体加版本盐（如 `v2|t0|…`），让新旧代次不交叉。是否给纯文本块也加盐见 §6 决策点。

---

## 5. 影响面与风险

| 项 | 变化 | 缓解 |
|---|---|---|
| chunk_id | 所有含表/图/公式的块**全部改变** | file_checksum 已有 → 可增量判定"哪些文档需要重跑"；建议显式换代而非局部变 |
| 块数 | 每多一个结构元素 +1 块；原混排块另 +1 正文块 | 预计 1 表 + 正文 的块从 1 → 2 |
| embedding 成本 | 与块数线性相关 | 重建前先统计语料里"含结构元素的块"占比 |
| **召回结构** | 元素块不再带同块正文（`with_sibling=False`）→ 元素块 retrieval_text 变薄 | 这是最大风险：短文档场景下单块混排本是"图表块也能被正文命中"。**必须做 A/B 对比**（同问题集看 top-k 命中变化），再决定是否需要补偿 |
| 公式还原 | 恢复 `kind=formula` → 重新进入 `CITE_IMAGE_KINDS` 裁图 + 可被将来 VLM 批次筛出 | 纯收益 |
| 测试 | 黄金 id 断言从"保持不变"改为"新基线" | 断言要连 anchor 表达式一起更新，否则测的是旧契约 |
| 文档 | `PDF_PIPELINE_DETAIL.md`、`PLAN_doc_parsing_v3.md` §6.4 的口径需重写 | 两处都曾与实现相反，别再留口头约定 |

**不建议的中间态**：只把公式单独提出来成块、其余不动。那会让"什么成块"的规则变成
"表/图按数量、公式按有无"，规则更难推理，且 id 变化范围变得不可预测。

---

## 6. 待拍板的决策点

1. **重建窗口**：接受一次性全量重建（推荐，一次到位）？还是先只做 P0 观测，量出影响面再定？
2. **anchor 换代方式**：只给拆分路径加版本盐（纯文本块 id 保持不变，省重建量）？还是全库统一加盐（新旧代次彻底隔离，排查更简单）？
3. **公式块的 retrieval_text 形态**：维持现状（LaTeX 原样 + `section_path` 前缀）？还是补一个自然语言锚词（如"公式："）以提升可召回性？注意公式块此前刻意不做 `contextualize`。
4. **召回补偿**：若 A/B 显示元素块召回下降，是否接受"元素块 retrieval_text 保留一句同块正文"作为补偿？（代价是重新引入轻微重复）

---

## 7. 实施记录（2026-09-24）

**拍板**：单口径（不留双路兼容）+ 公式单独成块。**未加 anchor 版本盐** —— 纯文本块的产出
逻辑与内容都没变，加盐反而把重建面从"含结构元素的块"扩大到全库。

**改动**

- `src/knowledge_chunk.py`
  - 新增 `_STRUCT_LABELS` / `_struct_items()`（分块判据的唯一来源）、`_BODY_EXCLUDE`；
    `_OWN_TEXT_EXCLUDE` 增 `formula` / `equation`。
  - 入口判据由数量改语义：`if not (tables or pictures or formulas): return [from_docling_chunk(...)]`。
  - 新增 `_build_formula_chunk()`（kind=formula、content=元素 LaTeX、tags=`["公式"]`、
    id 前缀 `f{i}|`），`from_docling_chunk` 的公式分支复用它（消除第二份公式口径）。
  - 兜底判据改为 `allow = sole and not body`：**整块 text 只含这一个元素**时才允许吃它。
    这修正了上一轮"拆分路径一律禁兜底"的粗糙处 —— 禁的是**含同块别人内容的 text**，
    而不是 text 本身；否则"元素内容取不到"时会不可逆丢内容。
  - `from_docling_chunk` 退化为「纯文本快通道 + 逐 item 兜底路径」的单块构造器。
- `tests/test_knowledge_chunk.py`：改 5 条旧口径断言（表+图各成块、单元素走统一路径、
  元素块题注、有正文时不兜底、裸元素兜底并告警），新增 5 条（公式与表各成块、
  公式不进正文块、`_struct_items` 分组、`test_table_and_picture_...`、
  `test_block_with_text_never_lets_element_eat_whole_text`）。
- 文档：本文档状态改「已实施」；`PDF_PIPELINE_DETAIL.md` §2.6 的口径段重写
  （判据、兜底条件、公式出口、`_BODY_EXCLUDE`）。

**验证**

- 全量 `pytest -q` → **368 passed**（改造前 364）。
- `test_split_path_chunk_ids_unchanged_after_fallback_ban` 仍通过：那两个场景块数未变，
  基线不受本次改动影响。
- 真实 PDF（`tmp_probe/picture_types.pdf`）：**8 块 → 12 块**。每页的「图 + 说明句」
  从"图块吞掉说明句"变为"图块留白（`自身无内容` + `needs_vlm=True`）+ 说明句独立成正文块"，
  **内容零丢失**；图块 `retrieval_text` 仍非空（`Chapter 4 Pricing scatter plot`）→ 仍可召回。
- 公式链路的真实 PDF 验证**未做**：本机未下载 CodeFormulaV2（611 MB）且
  `DOCLING_FORMULA` 默认关 —— 该出口目前只由单元测试覆盖。

**已知影响 / 待办**

- 含结构元素的块 id 全部变化 → **需重建索引**（`file_checksum` 可增量定位哪些文档要重跑）。
- 元素块 content 变薄（原先靠 sibling 装进图块的那句话移入正文块）→ 这是统一口径的
  预期结果，但**召回结构确实变了**，上线前建议跑一次 A/B 对比。
- 公式 `kind=formula` 现在会重新进入 `CITE_IMAGE_KINDS` → 公式块恢复"裁原页截图"能力。

---

## 8. 合流：删掉单块构造器（2026-09-24，同日第二刀）

§7 之后 `from_docling_chunk` 仍是个**公开的单块入口**，但它的四条 kind 分支里三条的
接手方 `_all` 早已在直接调用。于是"块里有什么元素"这个问题被**两套判据**回答
（`_kind_for_items` 多选一 / `_struct_items` 分组），且预处理字段
（`meta` / `headings` / `_resolve_items` / `section_path` / `text`）被**算两遍**。
更要紧的是 `_kind_for_items` 作为"多选一"判据的唯一载体，随时可能被新调用点重新激活
——那正是"1 表 + 1 图 → 图被表格吞掉"的陷阱。

**拍板**：彻底合流，不留兼容别名。

### 改动

- **新增 `_build_text_chunk()`**：搬走原 `from_docling_chunk` 的 text 装配。
  它是四个构建器里**唯一**吃整块 `text` 的（块内无别的元素，无归属风险）、
  也是**唯一**用 `chunker` 参数的那个（只有它会调 `chunker.contextualize`）。
  anchor 沿用 `f"{pages}|{boxes}|{text[:64]}"` → **纯文本块 id 逐字节不变**。
- **`_all` 重排**：预处理提前到入口判据**之前**，两个出口共用同一份；
  fast path 与"意外 0 块"回退都改调 `_build_text_chunk`。
  回退目标换了但结果等价：`out` 为空 ⟺ 三组全空 ⟺ 原 `_kind_for_items` 也只会返回
  `KIND_TEXT`。
- **删除 `from_docling_chunk`**（整个函数）与 **`_kind_for_items`**（唯一调用点在被删
  函数内，`tests/` 无直接调用，断言零改动）。后者删除是本次**目的之一**。
- **`from_docling_items`**：`out.append(from_docling_chunk(...))` →
  `out.extend(from_docling_chunk_all(...))`，仍不传 `chunker`（保持兜底路径
  `retrieval_text` 口径不变）。
- **测试**：21 处改动 —— 12 处单元素用例改 `_all(...)[0]`（断言原样不动）；
  5 处"混排块取第一个"的用例**重写**（那些语义已不存在：
  `table_wins_over_text_in_mixed_block` → `..._splits_instead_of_picking_one`；
  图文 / 表文混排改为断言两块各有其位）；`test_single_element_block_now_goes_through_
  unified_path` 里"与旧单块路径不同 id"改为直接算期望 anchor，把 `t0|` 前缀钉住；
  新增 2 条锁兜底路径的真实覆盖面（表格会产块 / 图片被跳过）。
- **文档**：本节；`PDF_PIPELINE_DETAIL.md` §2.6 标题与口径段；
  `PLAN_doc_parsing_v3.md` 里提到 `_kind_for_items` 的那句加删除注记。

### 验证

- `tests/test_knowledge_chunk.py` → **83 passed**（新增 2 条锁兜底路径覆盖面）；
  全量 `pytest -q` → **368 passed**（改造前同为 368，零回归）。

### 覆盖面更正：兜底路径真正会触发的结构分支只有 table / formula

合流时我原以为"表 / 图 / 公式三条分支在兜底路径都是活的"。**实测更正**：
兜底入口有 `if not text.strip(): continue`，而 `item_text(item, doc)` 的规则是
"有 `.text` 就用它，否则回退 `table_to_markdown`"。于是：

| item | `item_text` 结果 | 兜底路径行为 |
|---|---|---|
| 文本 / 公式 | `.text` 非空 | 产出块（公式 → `kind=formula`）|
| 表格 | 回退 `table_to_markdown` → 表格 md 非空 | **产出块（`kind=table`）** |
| 图片 | 回退 `table_to_markdown` → 空串 | **被 `continue` 跳过，不产块** |

所以 `KIND_DIAGRAM` 分支在兜底路径实际到不了（合流前也一样，不是本次引入）。
保留它无害，且 `table_to_markdown` 将来若对图片返回非空，它会立刻生效。
这两条覆盖面已由 `test_from_docling_items_still_builds_table_chunk` 与
`test_from_docling_items_skips_picture_items_without_text` 锁住。
- `test_split_path_chunk_ids_unchanged_after_fallback_ban` 仍通过 —— 拆分路径的块数与
  anchor 表达式未动。9 处 `chunk_id=make_chunk_id(...)` 与 1 处 anchor 组装**一行未改**，
  本次全是搬移与删除。

### id 影响面（比 §7 多一处）

| 块 | id |
|---|---|
| 纯文本块（正文 / 快通道） | **不变**（anchor 表达式相同） |
| 拆分路径的元素块 | 已在 §7 变过（`t0\|` / `p0\|` / `f0\|`） |
| **兜底路径的元素块** | **本次变**：无前缀 → `t0\|` / `p0\|` / `f0\|` |

兜底路径只在"两种 chunker 都产出 0 块"时触发（扫描件被 OCR 成 `section_header` 等），
影响文档少；`file_checksum` 可用于增量定位。

### 遗留

- 合并的收益是**防御性**的（消除判据分叉、误读源头、回归可能），成本是当下的
  （兜底路径 id 变化）。行为上与合流前逐字节等价，即"没有 bug 被修好"。
- 曾考虑"仅同类元素 >1 时才加 `id_prefix`"以保住兜底路径 id，**未采纳**：
  那会再动一次 §7 刚落地的拆分路径 id，而"1 表 + 1 图"这类混合元素的块比兜底文档常见，
  重建面反而更大。
