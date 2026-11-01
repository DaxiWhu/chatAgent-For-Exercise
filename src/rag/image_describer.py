"""图片 → VLM 结构化描述（批次 6）。

设计立场：**VLM 只补"确定性来源确实拿不到"的那部分**，能省的调用一次都不花。
  - 有 caption / 作者替代文字 → 已经是人写的确定性描述，跳过（`needs_vlm` 本来就已为 False）
  - docx 原生图表 → 数据已从 chart XML 精确读出，跳过
  - 已经在缓存里的（按图片内容 sha1 + 模型名）→ 跳过，不重复付费
  - EMF/WMF 矢量图 → **VLM 读不了**，显式标记而不是静默跳过
  - 没落盘的图（source_image 为空）→ 显式记为"无图可取"，绝不假装描述过

⚠️ 为什么默认关闭（`RAG_VLM_DESC=off`）：按图计费。方案 §9.3 明确要求
"先算量再决定"——所以先用 `estimate()` 算出总量与预估 token，再显式打开。
一张图约 800~1500 输入 token + 300~500 输出，一万张图就是千万级 token。

配置（全部走环境变量）
  RAG_VLM_DESC      off（默认）| auto | on
                    off  = 完全不调用，`needs_vlm` 原样保留
                    auto = 三项 VLM_* 配齐才调，否则告警并保留 `needs_vlm`
                    on   = 强制调；缺任一项直接报错，避免"以为跑了其实没跑"
  VLM_MODEL         默认空。主网关（DeepSeek）**没有视觉模型**，必须另指一家有视觉
                    能力的网关并填它的模型名
  VLM_API_BASE      默认空。同上，**不跟随** LLM_API_BASE（跟随会把图发给一个不支持
                    图片输入的模型，报错信息和"网关不对"完全不像）
  VLM_API_KEY       默认空。同上，**不跟随** LLM_API_KEY
  VLM_MAX_IMAGES    单次运行最多描述几张（默认 50，防误开时刷爆账单）
  VLM_TIMEOUT       单次请求超时秒数（默认 120）
  RAG_VLM_CACHE_DIR 缓存目录（默认 data/.vlm_cache）
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# 自己加载 .env，不让"配置是否生效"取决于 import 顺序（本项目踩过两次）
try:
    from dotenv import load_dotenv

    # 项目根 = 上溯 3 层（src/rag/x.py → src/rag → src → 根）。少一层会指向
    # src/.env（并不存在）而让加载静默失效；下面用 `_ROOT` 解析相对缓存目录，
    # 若算错会把 data/.vlm_cache 落到 src/data/.vlm_cache 里（2026-09-28 迁入 src/rag/ 时踩过）。
    _ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    load_dotenv(dotenv_path=os.path.join(_ROOT, ".env"))
except Exception:  # noqa: BLE001  （dotenv 是可选依赖）
    pass


def _env_int(name: str, default: int) -> int:
    """读环境变量并转 int，转不动就用默认值。

    name: 环境变量名。
    default: 未设置或值非法时的回退值。
    """
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


VLM_DESC = os.getenv("RAG_VLM_DESC", "off").strip().lower()
# ⚠️ VLM 是**独立的一家网关**，不跟随主网关。2026-11-01 起主网关是 DeepSeek，它没有
#    视觉模型；以前这三个值复用 `LLM_*`，现在必须各自显式配置。
#
#    为什么不给回落默认值：base 为空时 SDK 会拼出畸形 URL，报出来的错误与"这个模型
#    不支持图片输入"完全不是一个样子，排查会被带偏；更糟的是有些客户端在 base 为空
#    时回落到默认的 api.openai.com —— 那等于把图片发给一个你没打算用的服务。
#    宁可让 `enabled()` 直接说"没配 VLM_API_BASE"，也不要打一个注定失败的请求。
VLM_MODEL = os.getenv("VLM_MODEL", "").strip()
VLM_API_BASE = os.getenv("VLM_API_BASE", "").strip()
VLM_API_KEY = os.getenv("VLM_API_KEY", "").strip()
VLM_MAX_IMAGES = max(1, _env_int("VLM_MAX_IMAGES", 50))
VLM_TIMEOUT = max(10, _env_int("VLM_TIMEOUT", 120))
VLM_CACHE_DIR = os.getenv("RAG_VLM_CACHE_DIR", "data/.vlm_cache").strip() or "data/.vlm_cache"

# 送模型前的长边上限。方案 L1 的约束是"按 DPI 合理渲染，长边 1600~2000px"，
# 超了要分块渲染而不是降分辨率；这里对**已经裁剪好的图**取同一上限，
# 小字号/细线在上限内不会丢，同时把 token 控制在可预期范围。
MAX_SIDE = _env_int("VLM_MAX_SIDE", 1600)

# 位图格式：VLM 能读的。矢量格式（EMF/WMF）**不在其中** —— 必须显式区分，
# 否则一个永远失败的调用会被当成"调用失败"，而真实原因是"格式根本读不了"。
RASTER_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")
VECTOR_EXTS = (".emf", ".wmf", ".svg")


# ---------------------------------------------------------------------------
# 开关与算量（先算量再决定，见 §9.3）
# ---------------------------------------------------------------------------
def _vlm_missing() -> list[str]:
    """启用 VLM 前必须齐的三项；返回缺的那些（空列表 = 齐了）。

    为什么要逐项点名：`enabled()` 返回的原因会直接进日志，用来回答"为什么一张都
    没描述"。只说"未配置"，人还得自己去翻是哪一项 —— 三项分属不同服务（地址、
    凭证、模型），猜错的代价是多跑一轮批处理。
    """
    missing: list[str] = []
    if not VLM_API_KEY:
        missing.append("VLM_API_KEY")
    if not VLM_API_BASE:
        missing.append("VLM_API_BASE")
    if not VLM_MODEL:
        missing.append("VLM_MODEL")
    return missing


def enabled() -> tuple[bool, str]:
    """是否要真的调用 VLM。返回 (是否启用, 原因)。

    无参数。读环境变量 RAG_VLM_DESC 的三个取值：
        off（默认）→ 恒 False；
        on        → 三项 VLM_* 没配齐时 False（点名缺哪个）；
        auto      → 同上，配齐才 True。
    原因文案会直接进日志，用来回答"为什么一张都没描述"。
    """
    if VLM_DESC in ("off", "false", "0", "no", ""):
        return False, "RAG_VLM_DESC=off（默认关闭：按图计费，请先 estimate 再开）"
    missing = _vlm_missing()
    if missing:
        return False, f"RAG_VLM_DESC={VLM_DESC} 但未配置 {'/'.join(missing)}"
    if VLM_DESC in ("on", "true", "1", "yes"):
        return True, "RAG_VLM_DESC=on"
    # auto
    return True, "RAG_VLM_DESC=auto 且已配置 API Key"


def estimate(chunks: list) -> dict:
    """统计"有多少图需要 VLM"，并给出粗略 token 预估。

    chunks: 块列表（KnowledgeChunk 对象，用 getattr 读属性，所以传 dict 也只会
        得到空结果而不会报错）。只有 needs_vlm=True 的块进入统计。

    返回 dict：chunks_total / needs_vlm / sendable / by_reason /
    est_input_tokens / est_output_tokens / model / enabled。
    注意 `sendable` 是"不设限时能送多少张"，不含 limit、failed 两维 ——
    那两维只有真跑过 enrich 才有意义。

    存在的理由：这是**先算量再决定**的执行体。语料规模一上来，
    "按图计费"会从个位数变成千万级 token，这个数字应该在打开开关之前拿到。
    """
    pics = [c for c in chunks if getattr(c, "needs_vlm", False)]
    by_reason: dict[str, int] = {}
    with_file = 0
    for c in pics:
        img = (getattr(c, "source_image", "") or "")
        if not img:
            why = "无图可取（source_image 为空）"
        elif img.lower().endswith(VECTOR_EXTS):
            why = "矢量图（VLM 读不了，需先转位图）"
        elif not os.path.isfile(img):
            why = "图片文件不存在"
        else:
            why = "可送 VLM"
            with_file += 1
        by_reason[why] = by_reason.get(why, 0) + 1

    # 粗略估算：一张 1600 长边的图约 (w*h)/750 token，取方案给的 800~1500 区间中值兜底
    est_in = with_file * 1200 + sum(len(getattr(c, "retrieval_text", "") or "") for c in pics) // 2
    est_out = with_file * 400
    return {
        "chunks_total": len(chunks),
        "needs_vlm": len(pics),
        "sendable": with_file,
        "by_reason": by_reason,
        "est_input_tokens": est_in,
        "est_output_tokens": est_out,
        "model": VLM_MODEL,
        "enabled": enabled()[0],
    }


def format_estimate(est: dict) -> str:
    """把 estimate 的结果排成一行日志。

    est: estimate() 的返回值。needs_vlm / sendable / est_input_tokens /
        est_output_tokens / model / enabled 六键硬取（缺了会 KeyError），
        by_reason 用 .get 读，缺失时只少一段明细。
    """
    parts = [f"待描述 {est['needs_vlm']} 张／其中可送 {est['sendable']} 张",
             f"预估 ~{est['est_input_tokens']} 输入 + ~{est['est_output_tokens']} 输出 token",
             f"模型 {est['model']}", f"开关 {'开' if est['enabled'] else '关'}"]
    line = "；".join(parts)
    if est.get("by_reason"):
        line += "；明细：" + "，".join(f"{k}={v}" for k, v in est["by_reason"].items())
    return line


# ---------------------------------------------------------------------------
# prompt 与请求构造
# ---------------------------------------------------------------------------
_PROMPT_TMPL = """你是文档插图分析助手。请**只根据图中可见内容**作答。

硬性要求：
1. 看不出来的项一律留空（"" 或 []），**绝不编造**。宁可少写，不可猜。
2. `visible_text` 必须逐行照抄图中出现的文字（含数字、单位、图例、轴标签、图注）。
3. `description` 里出现的每个数字，都必须能在 `visible_text` 里找到。
4. 不要输出 markdown 代码块，只输出一个 JSON 对象。

上下文（仅供理解图片用途，**不要把这些当成图里的内容**）：
- 所在章节：{section_path}
- 图前正文：{preceding_text}
- 图后正文：{following_text}
- 图注/作者替代文字：{caption}
- 类型提示：{predicted_class}

输出 JSON：
{{"chart_type": "折线图/柱状图/饼图/流程图/示意图/照片 等",
  "title": "图内的标题，没有就留空",
  "visible_text": "图中所有可见文字，逐行照抄",
  "axes": {{"x": "横轴含义与范围", "y": "纵轴含义与范围"}},
  "legend": ["图例项"],
  "key_data": ["2023年机电产品 1800 亿元"],
  "trend": "趋势或结论，没有就留空",
  "description": "一段话描述：图号/类型/标题/轴/关键数值与结论"}}"""

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def build_messages(image_bytes: bytes, mimetype: str, context: dict) -> list:
    """构造 OpenAI 兼容的多模态 messages（图片 + 前后文）。

    image_bytes: 已规整的图片字节（PNG，见 prepare_image），会被 base64 内联进 url。
    mimetype: 图片 MIME（如 "image/png"），拼进 data URI。
    context: 上下文 dict，由 context_for 产出，读四个键：section_path /
        preceding_text / following_text / caption / predicted_class；
        任一缺失都会用"（无）/（未知）"占位，不会 KeyError。

    返回只含单条 user 消息的列表 —— 图片与 prompt 同在一条消息的 content 数组里，
    这是 OpenAI 兼容多模态接口的要求。
    """
    prompt = _PROMPT_TMPL.format(
        section_path=context.get("section_path") or "（无）",
        preceding_text=_clip(context.get("preceding_text")),
        following_text=_clip(context.get("following_text")),
        caption=context.get("caption") or "（无）",
        predicted_class=context.get("predicted_class") or "（未知）",
    )
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:{mimetype};base64,{b64}"}},
        ],
    }]


def _clip(text: Any, limit: int = 200) -> str:
    """截断一段上下文文本，空值统一替换成占位符。

    text: 原始文本，None / 空串 / 纯空白都返回 "（无）"。
    limit: 截断长度（字符），超出补 "…"。默认 200。

    前后文各截 200 字：给模型定主题用，多了只是白花 token。
    """
    s = (text or "").strip().replace("\n", " ")
    return s[:limit] + ("…" if len(s) > limit else "") if s else "（无）"


# ---------------------------------------------------------------------------
# 图片准备
# ---------------------------------------------------------------------------
def prepare_image(path: str) -> tuple[bytes, str]:
    """读图并规整成模型可读的位图。返回 (字节, mimetype)。

    path: 图片文件路径。文件不存在 / 打开失败 / 解码失败都会**抛异常**
        —— 由调用方 describe 捕获并转成带 error 的 dict。

    用 PIL 重新编码（而不是原样透传）有三个好处：
      1. 统一格式，避免某些后端不认 tiff/bmp；
      2. 长边超过上限时等比缩放，token 可预期；
      3. 顺手把 CMYK/带 alpha 的图转成 RGB，减少后端报错面。

    返回值恒为 ("image/png" 的字节, "image/png") —— 无论输入是什么格式。
    """
    from PIL import Image

    with Image.open(path) as img:
        img = img.convert("RGB") if img.mode not in ("RGB", "L") else img
        w, h = img.size
        if max(w, h) > MAX_SIDE:
            scale = MAX_SIDE / float(max(w, h))
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "PNG")
    return buf.getvalue(), "image/png"


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
def _cache_path(image_bytes: bytes) -> str:
    """缓存文件的绝对路径。

    image_bytes: 图片字节（**规整后**的 PNG，不是原文件字节）—— 用内容做键，
        所以同一张图换个文件名仍命中缓存；反之改了尺寸/格式会算作新图。

    缓存键 = 图片内容 + 模型名。图片内容相同就一定得到同一份描述，
    换模型则需要重算（不同模型的输出不能互相当成事实）。
    """
    sha = hashlib.sha1(image_bytes + VLM_MODEL.encode("utf-8")).hexdigest()
    root = VLM_CACHE_DIR
    if not os.path.isabs(root):
        root = os.path.join(_ROOT, root)
    return os.path.join(root, f"{sha}.json")


def _read_cache(image_bytes: bytes) -> dict | None:
    """读缓存的描述结果，未命中或解析失败返回 None。

    image_bytes: 图片字节（透传 _cache_path 算键）。

    命中时会把 `cached` 置为 True 再返回 —— 注意这个字段只在读缓存时被写上，
    磁盘上那份 JSON 恒为 false，不要用它判断缓存产物。
    读盘/JSON 解析异常只记 warning 并按未命中处理。
    """
    p = _cache_path(image_bytes)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
        data["cached"] = True
        return data
    except Exception as exc:  # noqa: BLE001  缓存坏了不该让流程挂掉
        logger.warning("VLM 缓存读取失败，忽略：%s（%s）", p, exc)
        return None


def _write_cache(image_bytes: bytes, result: dict) -> None:
    """把描述结果写进缓存（自动建目录）。

    image_bytes: 图片字节（透传 _cache_path 算键）。
    result: 要缓存的完整结果 dict（含 usage / issues 等附加字段，原样落盘）。

    写盘失败只记 warning —— 本次结果照样会被 _apply 用上，只是下次要重新付费。
    注意落盘内容里的 `cached` 恒为 False（True 只在 _read_cache 里临时写上）。
    """
    try:
        p = _cache_path(image_bytes)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        logger.warning("VLM 缓存写入失败（不影响结果）：%s", exc)


# ---------------------------------------------------------------------------
# 校验：VLM 是生成式的，必须能发现它"编了"
# ---------------------------------------------------------------------------
def validate(desc: dict) -> list:
    """返回问题列表（空 = 通过）。

    desc: 描述 dict，**会被就地修改** —— 全空的 `axes` 会被 pop 掉（避免空壳
        被当成"结构完整"）。读 visible_text / key_data / description / trend /
        title / axes 六个键，全部用 .get 兜底。

    刻意只做**能确定性做**的校验，不假装能做更多：
      - **数值回查**：description / key_data 里的数字必须能在 visible_text 里找到。
        模型的 `visible_text` 是"逐行照抄"的转录，与"生成式总结"的可信度不同级别；
        总结里冒出转录中没有的数字，就是幻觉的最典型形态（把 1,280,000 写成 1,280,00）。
      - **空转录**：visible_text 为空却给出了 title/key_data，同样可疑。
    数值**对不上时以确定性来源为准**（正文表格 / chart XML），这条原则贯穿整个方案。
    """
    issues: list = []
    visible = (desc.get("visible_text") or "")
    visible_nums = {_norm_num(n) for n in _NUMBER.findall(visible)}

    claimed = list(desc.get("key_data") or [])
    for field in ("description", "trend", "title"):
        v = desc.get(field)
        if isinstance(v, str) and v:
            claimed.append(v)

    unverified = []
    for text in claimed:
        for n in _NUMBER.findall(str(text)):
            if _norm_num(n) not in visible_nums:
                unverified.append(n)
    if unverified:
        issues.append("数值未在 visible_text 中找到：" + "、".join(sorted(set(unverified))[:8]))

    if not visible.strip() and (desc.get("title") or desc.get("key_data")):
        issues.append("visible_text 为空却给出了标题/数值，疑似凭上下文编造")

    axes = desc.get("axes")
    if isinstance(axes, dict) and not any(str(v).strip() for v in axes.values()):
        desc.pop("axes", None)      # 全空就删掉，别把空壳留下当"结构完整"
    return issues


def _norm_num(n: str) -> str:
    """归一化数字，让排版差异不造成假阳性。

    n: 单个数字字符串（正则捕获出来的）。

    归一化数字：1,800 / 1800 / 1800.0 应视为同一个数。
    转不成 float 时（如 "."）原样返回，不抛异常。
    """
    s = n.replace(",", "").rstrip(".")
    try:
        f = float(s)
        return str(int(f)) if f.is_integer() else str(f)
    except ValueError:
        return s


def parse_response(text: str) -> dict:
    """模型输出 → dict。容忍 ```json 包裹与前后废话，解析不出就返回带 error 的空壳。

    text: 模型输出的原始文本。

    返回要么是"模型自己给的任意 dict"（**字段集合不定**，消费侧必须用 .get 兜底），
    要么是 {"error": ..., "raw": ...} 的失败壳。只保证两件事：是合法 JSON、是对象。
    """
    if not text:
        return {"error": "模型返回空内容"}
    m = _JSON_BLOCK.search(text)
    raw = m.group(1) if m else text
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"输出不是合法 JSON：{exc}", "raw": text[:400]}
    if not isinstance(data, dict):
        return {"error": "输出 JSON 不是对象", "raw": text[:400]}
    return data


# ---------------------------------------------------------------------------
# 调用
# ---------------------------------------------------------------------------
def _call_api(messages: list) -> tuple[str, dict]:
    """真正发请求。返回 (模型输出文本, usage)。单独抽出便于测试时替换。

    messages: OpenAI 兼容的多模态 messages（见 build_messages）。

    返回值第一项是模型输出原文，**未做任何解析**（可能是代码块、可能带前后废话、
    可能是空串）；第二项是 usage dict，后端不给时返回 {}。两处都用 try 兜住，
    绝不因为取字段失败而抛异常。
    """
    from openai import OpenAI

    client = OpenAI(api_key=VLM_API_KEY, base_url=VLM_API_BASE, timeout=VLM_TIMEOUT)
    resp = client.chat.completions.create(model=VLM_MODEL, messages=messages,
                                         temperature=0.1)
    text = ""
    try:
        text = resp.choices[0].message.content or ""
    except Exception:  # noqa: BLE001
        text = ""
    usage = {}
    try:
        u = getattr(resp, "usage", None)
        usage = {"prompt_tokens": getattr(u, "prompt_tokens", 0),
                 "completion_tokens": getattr(u, "completion_tokens", 0),
                 "total_tokens": getattr(u, "total_tokens", 0)}
    except Exception:  # noqa: BLE001
        pass
    return text, usage


def describe(path: str, context: dict | None = None, *,
             call: Callable | None = None, use_cache: bool = True) -> dict:
    """描述一张图。返回结构化结果（含 issues / usage / cached）。

    path: 图片文件路径。扩展名落在 VECTOR_EXTS 时直接返回 error（不读盘）。
    context: 上下文 dict（见 context_for）；None 视作空 dict，prompt 里全用占位符。
    call: 可注入的调用函数，签名 (messages) -> (text, usage)。None 时用 _call_api。
        测试与离线场景传假实现即可，不必真连 API。
    use_cache: 是否读写缓存。默认 True；关掉用于"必须重新生成"的场合
        （注意关掉后即使磁盘有缓存也会重新付费）。

    绝不抛异常：出错时返回带 `error` 的 dict，由调用方决定怎么记 ——
    一张图描述失败不该让整份文档的解析结果作废。

    ⚠️ 解析失败（JSON 不合法）时**早返回且不写缓存** —— 这类输出每次都付费、
    每次都失败、永远不收敛；只有成功的结果才落盘。
    """
    context = context or {}
    ext = os.path.splitext(path)[1].lower()
    if ext in VECTOR_EXTS:
        return {"error": f"矢量格式 {ext} 无法直接送 VLM，需先转成位图（缺 LibreOffice 时无解）"}

    try:
        image_bytes, mimetype = prepare_image(path)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"图片读取/转换失败：{exc}"}

    if use_cache:
        hit = _read_cache(image_bytes)
        if hit:
            return hit
        # 兼容早期用原始文件字节做键写下的缓存
        with open(path, "rb") as fh:
            hit = _read_cache(fh.read())
        if hit:
            return hit

    messages = build_messages(image_bytes, mimetype, context)
    t0 = time.perf_counter()
    try:
        text, usage = (call or _call_api)(messages)
        #text	str	模型输出原文，未做任何解析——可能是代码块、可能带前后废话、可能是空串	resp.choices[0].message.content 为 None → ""
        # usage	dict	{"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}	后端不给 usage → {}
    except Exception as exc:  # noqa: BLE001  网络/鉴权/限流都可能，全按"这次没成功"处理
        return {"error": f"VLM 调用失败：{type(exc).__name__}: {exc}"}

    out = parse_response(text)      #模型给的字段是不定的
    out["usage"] = usage
    out["model"] = VLM_MODEL
    out["elapsed"] = round(time.perf_counter() - t0, 2)
    out["cached"] = False
    if out.get("error"):
        logger.warning("VLM 输出解析失败（%s）：%s", os.path.basename(path), out["error"])
        return out

    out["issues"] = validate(out)
    if out["issues"]:
        logger.warning("VLM 描述有可疑项（%s）：%s",
                       os.path.basename(path), "；".join(out["issues"]))
    if use_cache:
        _write_cache(image_bytes, out)
    return out


# ---------------------------------------------------------------------------
# 接入产物：把描述写回 KnowledgeChunk
# ---------------------------------------------------------------------------
def context_for(chunks: list, index: int) -> dict:
    """为第 index 个块装配上下文（同源文档的前后文 + 章节路径 + 图表 + 分类）。

    chunks: 完整块列表（KnowledgeChunk 对象列表，用 getattr 读属性）。
        前后文一定在**这个列表内**找，所以列表范围就是"同一份文档"的边界。
    index: 目标块在 chunks 中的下标。

    为什么前后文都要：图注常缺，"这张图在讲什么"往往写在正文里（"如图 3 所示…"）。
    前文给主题，后文给结论。**只在同一 source 内取**，跨文档取会把别人的正文
    当成这张图的语境，比不给还糟。
    """
    c = chunks[index]
    source = getattr(c, "source", "")
    section = getattr(c, "section_path", "") or ""
    caption = (getattr(c, "alt_text", "") or getattr(c, "content", "") or "").strip()
    # 分类名判定用**正向白名单**，不用"排除已知非分类词"的黑名单。
    # 为什么：`tags` 是混装的 list —— 分类名（bar_chart）、类别名（图表）、
    # 处理状态标记（待VLM / 自身无内容 / 混排块正文）都往里放。黑名单要求
    # "每加一个 tag 都记得同步排除名单"，漏一次就把标记当成图片类别喂给 VLM
    # （实际发生过：`文本兜底` 曾被当作 predicted_class）。只认 26 个真实类别名，
    # 之后往 tags 里放什么都不会被误读。
    from .knowledge_chunk import _V25_CLASSES
    pred = ""
    #这里为什么取第一个tag
    #cls 是唯一可能命中白名单的值，它是被刻意放在最前面的。构造的时候专门放的

    for t in getattr(c, "tags", None) or []:
        if t in _V25_CLASSES:
            pred = t
            break

    def _neighbor(step: int) -> str:
        """沿指定方向找最近的一块"有正文且不需要 VLM"的相邻块。

        step: 方向与步长，-1 往前、+1 往后。

        遇到不同 source 立即停止（返回 ""）—— 跨文档不算邻居。
        跳过正文为空、以及本身也 needs_vlm 的块（拿图去描述图没有意义）。
        末尾找不到时返回 ""。
        """
        i = index + step
        while 0 <= i < len(chunks):
            n = chunks[i]
            if getattr(n, "source", "") != source:
                return ""
            text = (getattr(n, "retrieval_text", "") or "").strip()
            if text and not getattr(n, "needs_vlm", False):
                return text
            i += step
        return ""

    return {"section_path": section,
            "preceding_text": _neighbor(-1),
            "following_text": _neighbor(1),
            "caption": caption,
            "predicted_class": pred}


def enrich(chunks: list, *, describer: Callable | None = None,
           max_images: int | None = None) -> dict:
    """就地为需要 VLM 的块补描述。返回统计（供日志与脚本使用）。

    chunks: 块列表（KnowledgeChunk 对象），**就地改写** —— 写 vlm_desc、
        追加 retrieval_text、清 needs_vlm、打 tags。空列表直接返回零统计。
    describer: 可注入的描述函数，签名 (image_path, context) -> desc dict。
        None 时用 describe。**注意门闸是模块级的 enabled()**，与这里注入什么无关。
    max_images: 本次最多成功描述几张。None 时取 VLM_MAX_IMAGES（默认 50）。
        计数依据是"已成功数"（含缓存命中），达到后就只标记不再调用。

    统计里刻意区分几种"没做"的原因：开关关着、超上限、无图可取、格式不支持、调用失败。
    只报一个"成功 N 张"是看不出问题的 —— 而"以为跑了其实没跑"是这类批处理最常见的坑。
    """
    stats = {"total": len(chunks), "candidates": 0, "described": 0, "cached": 0,
             "skipped_disabled": 0, "skipped_limit": 0, "skipped_vector": 0,
             "skipped_no_image": 0, "failed": 0}
    if not chunks:
        return stats

    ok, why = enabled()
    if not ok:
        stats["skipped_disabled"] = sum(1 for c in chunks if getattr(c, "needs_vlm", False))
        if stats["skipped_disabled"]:
            logger.info("跳过图片 VLM 描述：%s（%d 张待描述）", why, stats["skipped_disabled"])
        return stats

    limit = VLM_MAX_IMAGES if max_images is None else max_images
    for i, c in enumerate(chunks):
        if not getattr(c, "needs_vlm", False):
            continue
        stats["candidates"] += 1

        img = (getattr(c, "source_image", "") or "")
        if not img:
            stats["skipped_no_image"] += 1
            _mark_skipped(c, "待VLM（无图可取）")
            continue
        if img.lower().endswith(VECTOR_EXTS):
            stats["skipped_vector"] += 1
            _mark_skipped(c, "待VLM（矢量图，需先转位图）")
            continue
        if not os.path.isfile(img):
            stats["skipped_no_image"] += 1
            _mark_skipped(c, "待VLM（图片文件不存在）")
            continue
        if stats["described"] >= limit:
            stats["skipped_limit"] += 1
            _mark_skipped(c, "待VLM（超出本次上限）")
            continue

        desc = (describer or describe)(img, context_for(chunks, i))
        if desc.get("error") or not desc.get("description"):
            stats["failed"] += 1
            _mark_skipped(c, f"待VLM（{(desc.get('error') or '未产出 description')[:60]}）")
            continue

        _apply(c, desc)
        stats["described"] += 1
        if desc.get("cached"):
            stats["cached"] += 1

    logger.info("图片 VLM 描述：成功 %d 张（缓存命中 %d）；未做 %d（超限 %d / 无图 %d / 矢量 %d / 失败 %d）",
                stats["described"], stats["cached"],
                stats["skipped_no_image"] + stats["skipped_vector"]
                + stats["skipped_limit"] + stats["failed"],
                stats["skipped_limit"], stats["skipped_no_image"],
                stats["skipped_vector"], stats["failed"])
    return stats


def _apply(chunk: Any, desc: dict) -> None:
    """把描述写回块：structured_data 进 vlm_desc，description 追加进检索文本。

    chunk: 目标块（KnowledgeChunk 对象），**就地改写** —— vlm_desc /
        retrieval_text / needs_vlm / tags 四处。chunk.vlm_desc 存的是**描述原文**，
        不做字段筛选（理由是审计：token 账要对得上）。
    desc: describe 产出的描述 dict。**会被就地改写** —— 合并后的 issues 写回
        desc["issues"]。缺 description 时只写 vlm_desc 不追加文本（追加空串无意义）。

    校验放在**消费侧**而不是只放在 describe() 里：换本地模型、从缓存/人工结果读入时
    都不该绕过"数值必须在 visible_text 里找得到"这条底线（测试逼出来的修正——
    原来只在网络路径校验，注入任何别的 describer 就静默失效了）。
    """
    issues = list(desc.get("issues") or [])
    for extra in validate(desc):
        if extra not in issues:
            issues.append(extra)
    if issues:
        desc["issues"] = issues

    chunk.vlm_desc = desc
    desc_text = (desc.get("description") or "").strip()
    # 模型产出**追加**在确定性描述之后，绝不覆盖：caption / 替代文字是作者写的，
    # 一旦让生成式内容顶掉它，就再也分不清哪句是人写的、哪句是模型说的。
    parts = [p for p in (chunk.retrieval_text, desc_text) if p]
    chunk.retrieval_text = " ".join(parts)
    chunk.needs_vlm = False
    tags = list(chunk.tags or [])
    if "VLM描述" not in tags:
        tags.append("VLM描述")
    if issues:
        tags.append("VLM待核对")
    chunk.tags = tags


def _mark_skipped(chunk: Any, tag: str) -> None:
    """给"没做成"的块打上可检索的标记，方便事后按标签筛出来补跑。

    chunk: 目标块，**就地改写 tags**（不会碰 vlm_desc / needs_vlm ——
        needs_vlm 仍为 True，所以下一轮还会被重试）。
    tag: 要打上的标记，形如 "待VLM（原因）"。

    写入前会先清掉所有旧的 `待VLM*` 标记再追加：否则反复跑会累积一堆
    "待VLM（无图可取）待VLM（图片文件不存在）"——原因已经变了，旧标记只会误导。
    """
    tags = [t for t in (chunk.tags or []) if not str(t).startswith("待VLM")]
    tags.append(tag)
    chunk.tags = tags
