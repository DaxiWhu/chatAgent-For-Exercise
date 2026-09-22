"""Docling 环境自检：核对已装版本与 API 形态是否与适配层的假设一致。

为什么需要这个脚本：docling 迭代很快，类路径与参数名会变——例如 HybridChunker 的
chunk() 参数在不同版本里是 doc= 或 dl_doc=，docx 的 format option 类名也变过。
适配层 src/docling_loader.py 因此统一写了防御式取值；本脚本用来在**实际环境**里
逐条核对假设。升级 docling 之后应先跑它，再看适配层是否需要调整。

用法：
    D:/miniConda/envs/langchain1.2/python.exe scripts/check_docling_env.py
"""
import importlib
import importlib.metadata as md
import inspect
import sys

print("=" * 72)
print("Docling 环境自检")
print("=" * 72)
print(f"Python {sys.version.split()[0]}")
for pkg in ("docling", "docling-core", "docling-ibm-models", "docling-parse",
            "transformers", "tiktoken", "semchunk", "torch", "pdfminer.six"):
    try:
        print(f"  {pkg:20s} {md.version(pkg)}")
    except Exception:
        print(f"  {pkg:20s} 未安装")


def find(paths):
    """按候选路径依次尝试导入，返回第一个成功的对象。"""
    for p in paths:
        mod, _, attr = p.rpartition(".")
        try:
            return getattr(importlib.import_module(mod), attr), p
        except Exception:
            continue
    return None, "全部候选路径均失败"


def show(label, paths, want_sig=False):
    obj, used = find(paths)
    if obj is None:
        print(f"  [!!] {label}: {used}")
        print(f"       候选: {paths}")
        return None
    print(f"  [OK] {label}\n       路径: {used}")
    if want_sig:
        try:
            print(f"       签名: {inspect.signature(obj)}")
        except Exception as e:
            print(f"       签名: <取不到: {e}>")
    return obj


print("\n-- 1. 转换器与 pipeline options --")
show("DocumentConverter",
     ["docling.document_converter.DocumentConverter"], want_sig=True)
show("PdfFormatOption", ["docling.document_converter.PdfFormatOption"])
show("DocxFormatOption",
     ["docling.document_converter.DocxFormatOption",
      "docling.document_converter.WordFormatOption"])
show("InputFormat", ["docling.datamodel.base_models.InputFormat"])
show("ConversionStatus", ["docling.datamodel.base_models.ConversionStatus"])
PdfOpt = show("PdfPipelineOptions", ["docling.datamodel.pipeline_options.PdfPipelineOptions"])
show("TableFormerMode", ["docling.datamodel.pipeline_options.TableFormerMode"])

if PdfOpt is not None:
    print("\n  PdfPipelineOptions 关键字段（适配层要用到的）:")
    try:
        fields = set(PdfOpt.model_fields)
    except Exception:
        fields = set()
    for f in ("do_ocr", "do_table_structure", "do_cell_matching", "do_formula_enrichment",
              "do_code_enrichment", "do_picture_classification", "do_picture_description",
              "generate_picture_images", "generate_page_images", "images_scale",
              "table_structure_options", "ocr_options", "accelerator_options",
              "artifacts_path", "enable_parallel_processing"):
        print(f"       {'OK ' if f in fields else '缺失'} {f}")

print("\n-- 2. 切分器（HybridChunker）--")
HC = show("HybridChunker",
          ["docling.chunking.HybridChunker",
           "docling_core.transforms.chunker.hybrid_chunker.HybridChunker"],
          want_sig=True)
if HC is not None:
    print(f"       chunk 方法签名: {inspect.signature(HC.chunk)}")
    params = list(inspect.signature(HC.chunk).parameters)
    print(f"       -> 第一个数据参数是: {params[1] if len(params) > 1 else '?'}"
          "   （适配层按它判定传 doc= 还是 dl_doc=）")
    for meth in ("contextualize", "serialize"):
        print(f"       {meth}: {'有' if hasattr(HC, meth) else '无'}")

print("\n-- 3. Tokenizer（HybridChunker 必需）--")
show("HuggingFaceTokenizer",
     ["docling_core.transforms.chunker.tokenizer.huggingface.HuggingFaceTokenizer",
      "docling.chunking.tokenizer.huggingface.HuggingFaceTokenizer"], want_sig=True)
show("OpenAITokenizer",
     ["docling_core.transforms.chunker.tokenizer.openai.OpenAITokenizer"], want_sig=True)
BT = show("BaseTokenizer",
          ["docling_core.transforms.chunker.tokenizer.base.BaseTokenizer"])
if BT is not None:
    abstracts = sorted(getattr(BT, "__abstractmethods__", []) or [])
    print(f"       抽象方法（自定义 tokenizer 必须实现）: {abstracts}")

print("\n-- 4. 文档模型元素 --")
for name, path in [
    ("TableItem", "docling_core.types.doc.document.TableItem"),
    ("PictureItem", "docling_core.types.doc.document.PictureItem"),
    ("DoclingDocument", "docling_core.types.doc.document.DoclingDocument"),
]:
    obj, used = find([path])
    print(f"  {'[OK]' if obj else '[!!]'} {name}  {used if obj else ''}")
    if obj is not None:
        for meth in ("export_to_markdown", "export_to_dataframe", "caption_text", "iterate_items"):
            if hasattr(obj, meth):
                print(f"         有 {meth}{inspect.signature(getattr(obj, meth))}")

print("\n" + "=" * 72)
print("自检结束。若某项 [!!]，适配层会走对应的降级分支，不必手工修。")
print("=" * 72)
