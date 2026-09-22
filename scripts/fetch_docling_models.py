"""绕过 HuggingFace 缓存机制，直接用 HTTP 把 Docling 模型下到本地目录。

为什么需要这个脚本（而不是 docling 自带的 download_models）：

  1. **符号链接问题**：HF hub 会把 `snapshots/` 下的文件做成指向 `blobs/` 的符号链接。
     Windows 未开开发者模式、或执行环境禁止创建链接时，hub 会退化成留下 **0 字节的空文件**，
     模型加载时报 "Expecting value: line 1 column 1 (char 0)"。

  2. **删除操作被拦截**：hub 下载每个文件都要创建再删除自己的 `.lock`，一趟下来几十次删除。
     若执行环境（如受保护的沙箱）限制批量删除，下载会在中途被打断，且永远停在 `.incomplete`。

本脚本用普通 HTTP GET 直接把文件写到最终位置，全程不做任何删除，两个问题都规避掉。

下载目标是 docling 的 artifacts 目录结构：`<输出目录>/<org>--<model>/<文件>`，
与官方 download_models 产出的一致，因此可直接配 DOCLING_ARTIFACTS_PATH 使用。

两类模型，两条路径：
  - **HF 上的模型**（layout / tableformer / 图片分类器 / 公式识别）：走 HF 兼容 API，见 hf_repos()。
  - **RapidOCR**（OCR 引擎，扫描件才用）：权重不在 HF 上，地址来自 rapidocr 自己的注册表，
    直接复用 docling 的 RapidOcrModel.download_models()，见 fetch_rapidocr()。
    不需要 OCR 时可加 --skip-rapidocr 跳过。

用法：
    python scripts/fetch_docling_models.py [输出目录，默认 ./models/docling]

国内直连 huggingface.co 会超时，脚本默认走 hf-mirror 镜像。要改端点：
    python scripts/fetch_docling_models.py --endpoint https://huggingface.co
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_ENDPOINT = os.getenv("HF_ENDPOINT", "").strip() or "https://hf-mirror.com"
UA = {"User-Agent": "docling-model-fetcher/1.0"}
TIMEOUT = 60
CHUNK = 1 << 20


def hf_repos() -> list[tuple[str, str]]:
    """从 docling 自身读取需要哪些 repo，避免手工维护清单过期。

    返回 [(repo_id, 本地目录名)]，目录名与官方 download_models 保持一致。
    """
    from docling.datamodel.pipeline_options import (
        DocumentPictureClassifierOptions,
        LayoutObjectDetectionOptions,
    )

    out: list[tuple[str, str]] = []
    spec = LayoutObjectDetectionOptions().model_spec
    out.append((spec.repo_id, spec.repo_id.replace("/", "--")))
    for override in (getattr(spec, "engine_overrides", None) or {}).values():
        merged = override.merge_with(spec.repo_id, spec.revision)
        out.append((merged.repo_id, str(merged.repo_id).replace("/", "--")))

    pic = DocumentPictureClassifierOptions.from_preset("document_figure_classifier_v2")
    out.append((pic.repo_id, pic.repo_cache_folder))

    # 这两个类只暴露了本地文件夹名（_model_repo_folder），repo_id 与它同形
    for folder in ("docling-project--docling-models", "docling-project--CodeFormulaV2"):
        out.append((folder.replace("--", "/", 1), folder))

    # 去重，保持顺序
    seen, uniq = set(), []
    for repo, folder in out:
        if repo not in seen:
            seen.add(repo)
            uniq.append((repo, folder))
    return uniq


def list_files(repo: str, endpoint: str) -> list[str]:
    """用 HF 兼容 API 列出 repo 里的文件。"""
    url = f"{endpoint}/api/models/{repo}"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.load(resp)
    return [s["rfilename"] for s in data.get("siblings", [])]


def download_file(url: str, dest: Path) -> int:
    """流式下载到 dest.part，完成后改名为 dest（自己控制，不经过任何删除操作）。"""
    # 有些文件在 repo 里是多层路径，例如 docling-models 的
    # model_artifacts/tableformer/accurate/tableformer_accurate.safetensors。
    # 不先建父目录，open(part, "wb") 会直接 FileNotFoundError——
    # 这正是 tableformer 四个文件下载失败的根因。
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers=UA)
    written = 0
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp, open(part, "wb") as fh:
        while True:
            block = resp.read(CHUNK)
            if not block:
                break
            fh.write(block)
            written += len(block)
    part.replace(dest)
    return written


def fetch_repo(repo: str, folder: str, root: Path, endpoint: str) -> tuple[int, int, list[str]]:
    """下载单个 repo。返回 (成功文件数, 字节数, 失败清单)。"""
    try:
        files = list_files(repo, endpoint)
    except urllib.error.HTTPError as exc:
        print(f"  [跳过] {repo}：仓库不存在或不可访问（HTTP {exc.code}）")
        return 0, 0, [f"{repo}: HTTP {exc.code}"]
    except Exception as exc:
        print(f"  [失败] {repo}：列举文件出错 {type(exc).__name__}: {exc}")
        return 0, 0, [f"{repo}: {exc}"]

    dest_dir = root / folder
    dest_dir.mkdir(parents=True, exist_ok=True)
    print(f"  {repo} → {folder}/（{len(files)} 个文件）")

    ok, total, failed = 0, 0, []
    for name in files:
        out = dest_dir / name
        if out.exists() and out.stat().st_size > 0:
            print(f"     已存在  {name}")
            ok += 1
            continue
        url = f"{endpoint}/{repo}/resolve/main/{name}"
        try:
            size = download_file(url, out)
            total += size
            ok += 1
            print(f"     {size / 1e6:8.2f} MB  {name}")
        except Exception as exc:
            failed.append(f"{repo}/{name}: {exc}")
            print(f"     [失败] {name}: {type(exc).__name__}: {exc}")
    return ok, total, failed


def fetch_rapidocr(root: Path, backend: str = "onnxruntime", lang: str = "ch") -> list[str]:
    """预取 RapidOCR（OCR 引擎）权重。返回失败清单。

    为什么单独走一条路：RapidOCR 的权重**不在 HF hub 上**，地址来自 rapidocr 自己的
    注册表（`rapidocr.inference_engine.base.InferSession.get_model_url`）。docling 的
    RapidOcrModel.download_models 正好是"查注册表 + requests 普通 HTTP 下载"，
    天然避开上面注释里的符号链接与 lock 删除两个坑，所以直接复用它而不是自己拼 URL。

    backend/lang 必须与运行时的 OCR 选项一致，否则解析出的文件名对不上，
    加载时仍会报 "RapidOCR artifacts not found or incomplete"。
    docling 默认 ocr_options 是 OcrAutoOptions（kind=auto, lang=[]），
    在 Windows/CPU 上落到 onnxruntime:ch，故此处默认值与之对齐。
    """
    try:
        from docling.models.stages.ocr.rapid_ocr_model import RapidOcrModel
    except Exception as exc:
        return [f"rapidocr: 无法导入 RapidOcrModel（{type(exc).__name__}: {exc}）"]

    dest = root / RapidOcrModel._model_repo_folder
    print(f"  rapidocr（backend={backend}, lang={lang}）→ {RapidOcrModel._model_repo_folder}/")
    try:
        RapidOcrModel.download_models(
            backend=backend, lang=lang, local_dir=dest, progress=False
        )
    except Exception as exc:
        print(f"     [失败] {type(exc).__name__}: {exc}")
        return [f"rapidocr({backend}:{lang}): {exc}"]

    files = sorted(p for p in dest.rglob("*") if p.is_file())
    for p in files:
        print(f"     {p.stat().st_size / 1e6:8.2f} MB  {p.relative_to(dest)}")
    if not files:
        return [f"rapidocr({backend}:{lang}): 下载后目录仍为空"]
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description="用 HTTP 直接下载 Docling 模型权重")
    ap.add_argument("output_dir", nargs="?", default="models/docling",
                    help="输出目录（默认 models/docling）")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                    help=f"HF 兼容端点（默认 {DEFAULT_ENDPOINT}）")
    ap.add_argument("--rapidocr-backend", default="onnxruntime",
                    help="RapidOCR 推理后端（onnxruntime|openvino|paddle|torch，默认 onnxruntime）")
    ap.add_argument("--rapidocr-lang", default="ch",
                    help="RapidOCR 识别语言代码（默认 ch，与 docling 默认一致）")
    ap.add_argument("--skip-rapidocr", action="store_true",
                    help="只下 HF 上的模型，跳过 RapidOCR")
    args = ap.parse_args()

    endpoint = args.endpoint.rstrip("/")
    root = Path(args.output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    print(f"端点   : {endpoint}")
    print(f"输出目录: {root}")
    try:
        repos = hf_repos()
    except Exception as exc:
        print(f"读取 docling 模型清单失败：{type(exc).__name__}: {exc}")
        print("请确认已安装 docling。")
        return 1

    total_ok, total_bytes, all_failed = 0, 0, []
    for repo, folder in repos:
        ok, size, failed = fetch_repo(repo, folder, root, endpoint)
        total_ok += ok
        total_bytes += size
        all_failed += failed

    if not args.skip_rapidocr:
        all_failed += fetch_rapidocr(root, args.rapidocr_backend, args.rapidocr_lang)

    print(f"\n完成：{total_ok} 个文件，新下载 {total_bytes / 1e6:.1f} MB")
    if all_failed:
        print(f"有 {len(all_failed)} 项失败：")
        for f in all_failed:
            print(f"  - {f}")
        print("可重跑本脚本，已下载的文件会跳过。")
        return 1

    print("\n下一步：确认 .env 中 DOCLING_ARTIFACTS_PATH 指向该目录，然后跑")
    print("  python scripts/check_docling_env.py")
    print("  python scripts/smoke_docling.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
