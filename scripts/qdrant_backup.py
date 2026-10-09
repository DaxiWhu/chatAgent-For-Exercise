#!/usr/bin/env python
"""Qdrant 快照：备份 / 列出 / 校验 / 清理。

为什么 Qdrant 也要备份（它和 PG 不一样）
--------------------------------------
PG 里存的是**不可再生**的东西（会话历史、审核记录、归属登记）—— 丢了就是丢了。
Qdrant 里存的是**可以从语料重建**的向量，所以直觉上"不用备份"。

这个直觉在生产上是错的，两个原因：

  1. **重建要花钱**。重建 = 把全部语料重新走一遍 embedding（外部 API，按 token 计费）。
     语料上万块时这是一笔真实账单，而且是"为一个本可避免的运维事故"付的。
  2. **重建要花时间**。解析（docling/OCR 是分钟级）+ 切分 + embedding 全跑一遍，
     期间检索能力是**残缺的**（只能退回进程内 BM25，召回质量下降）。
     RTO 从"几分钟恢复快照"变成"几小时重灌"。

所以快照的定位是**降低 RTO、避免重算成本**，而不是"唯一真相来源"。
真相来源始终是语料本身 —— 这也是本脚本**不自动执行恢复**的原因：
Qdrant 的恢复会覆盖当前集合，属破坏性动作，必须人工确认（步骤见 `drill` 的输出）。

⚠️ 与 `scripts/pg_backup.py` 的接口刻意保持一致（backup / list / verify / prune），
   让运维只学一套命令。

用法
----
    python scripts/qdrant_backup.py backup            # 建快照并下载到本地
    python scripts/qdrant_backup.py list              # 本地文件 + 服务端快照
    python scripts/qdrant_backup.py verify <文件>     # 校验快照文件可读且结构完整
    python scripts/qdrant_backup.py prune --keep 7    # 清理服务端旧快照
    python scripts/qdrant_backup.py drill             # 备份 + 校验 + 打印恢复步骤

环境变量
--------
    QDRANT_URL         默认 http://localhost:6333
    QDRANT_API_KEY     托管/公网必填；本地无鉴权可留空
    QDRANT_COLLECTION  默认 rag_chunks
    BACKUP_DIR         默认 ./backups/qdrant

退出码
------
    0 = 成功；1 = 失败（校验不过、HTTP 报错等）；2 = 无法连接或未配置
    （2 是给定时任务用的：`backup-qdrant` 容器据此区分"未启用该后端"与"真失败"）
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# 让"直接跑脚本"（python scripts/qdrant_backup.py）也能 import 到项目模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts import drill_metrics  # noqa: E402

DEFAULT_BACKUP_DIR = "backups/qdrant"
RC_OK, RC_FAIL, RC_UNREACHABLE = 0, 1, 2

# 视为"本机"的主机名。对这些地址**必须绕过代理**，理由见 QdrantAdmin.__init__。
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


class QdrantAdmin:
    """只封装备份/校验需要的几个端点（不引入 qdrant-client 依赖）。"""

    def __init__(self, url: str, api_key: str = "",
                 collection: str = "rag_chunks", timeout: int = 30):
        self.url = (url or "").rstrip("/")
        self.collection = collection
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            # 用 api-key 头而不是 query 参数：query 会被写进访问日志与进程列表
            self.headers["api-key"] = api_key

        # ⚠️ 本机地址必须**绕过代理**。
        # http_proxy/https_proxy 在开发机与企业网里极为常见，而 urllib 只有在
        # 配了 NO_PROXY 时才豁免本机地址 —— 没配时的表现**极具误导性**：
        # 请求被送进代理 → 代理连不上本机端口 → 回一个 502 → 报错指向
        # "HTTP 502"，看起来像"Qdrant 服务返回了错误"，而真正的原因是
        # **根本没连到 Qdrant**，于是你会去查 Qdrant 的日志（那里什么都没有）。
        # （本项目在压测脚本上踩过同类问题：数字变成了"经代理的吞吐"。）
        host = urllib.parse.urlsplit(self.url).hostname or ""
        if host.lower() in _LOCAL_HOSTS:
            self._opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}))       # 空 dict = 不用任何代理
        else:
            self._opener = urllib.request.build_opener()

    def _req(self, method: str, path: str, body=None, raw: bool = False):
        if not self.url:
            raise ConnectionError("未配置 QDRANT_URL（该后端可能未启用）")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(f"{self.url}{path}", data=data,
                                     method=method, headers=self.headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read(300).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            # 502/503/504 是**上游没在服务**的信号（代理或反向代理给出的），
            # 语义上与"连不上"同类。对**可选后端**必须报"跳过"而不是"备份失败"——
            # 否则一个压根没启用的后端会每天定时告警一次，很快所有人都把这条
            # 通知屏蔽掉，真失败也一起被忽略。
            if e.code in (502, 503, 504):
                raise ConnectionError(
                    f"{method} {path} -> HTTP {e.code}（上游不可达，通常说明 "
                    f"{self.url} 后面没有在跑的服务）{detail.strip()}"
                ) from None
            raise RuntimeError(f"{method} {path} -> HTTP {e.code} {detail.strip()}") from None
        except urllib.error.URLError as e:
            raise ConnectionError(f"无法连接 {self.url}：{e.reason}") from None
        except ConnectionError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ConnectionError(f"{type(e).__name__}: {e}") from None
        if raw:
            return payload
        return json.loads(payload.decode("utf-8")) if payload else {}

    def collection_info(self) -> dict:
        return self._req("GET", f"/collections/{self.collection}").get("result") or {}

    def create_snapshot(self) -> dict:
        return self._req("POST", f"/collections/{self.collection}/snapshots").get("result") or {}

    def list_snapshots(self) -> list:
        return self._req("GET", f"/collections/{self.collection}/snapshots").get("result") or []

    def delete_snapshot(self, name: str):
        return self._req("DELETE", f"/collections/{self.collection}/snapshots/{name}")

    def download_snapshot(self, name: str) -> bytes:
        # 响应对这个端点是二进制（tar），不能按 JSON 解析
        return self._req("GET", f"/collections/{self.collection}/snapshots/{name}", raw=True)


def _admin(args) -> QdrantAdmin:
    return QdrantAdmin(
        url=args.url or _env("QDRANT_URL"),
        api_key=args.api_key or _env("QDRANT_API_KEY"),
        collection=args.collection or _env("QDRANT_COLLECTION", "rag_chunks"),
    )


def _backup_dir(args) -> Path:
    d = Path(args.backup_dir or _env("BACKUP_DIR", DEFAULT_BACKUP_DIR))
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
def cmd_backup(args) -> int:
    admin = _admin(args)
    print(f"Qdrant : {admin.url or '（未配置）'}")
    print(f"集合   : {admin.collection}")

    # 先看集合有多少点。**空备份是"成功但无用"** —— 不检查的话，
    # 灌库还没跑完（或集合名配错）时会安静地产出一堆 0 点快照，
    # 而"备份成功了"这句日志会让人以为有保障。
    try:
        info = admin.collection_info()
        points = info.get("points_count")
        if points is None:
            points = info.get("vectors_count")
        print(f"点数   : {points if points is not None else '未知'}")
        if points == 0:
            print("⚠️ 集合为空。备份会成功但没有任何内容 —— "
                  "通常说明语料还没灌进来，或 QDRANT_COLLECTION 配错了。")
    except ConnectionError as e:
        print(f"无法连接：{e}", file=sys.stderr)
        return RC_UNREACHABLE
    except RuntimeError as e:
        print(f"读取集合信息失败：{e}", file=sys.stderr)
        return RC_FAIL

    snap = admin.create_snapshot()
    name = snap.get("name") or ""
    if not name:
        print(f"服务端未返回快照名：{snap}", file=sys.stderr)
        return RC_FAIL
    print(f"服务端快照已创建：{name}（{snap.get('size', '?')} 字节）")

    try:
        data = admin.download_snapshot(name)
    except Exception as e:  # noqa: BLE001
        print(f"下载快照失败：{e}", file=sys.stderr)
        return RC_FAIL
    if not data:
        print("下载到 0 字节 —— 快照不可用", file=sys.stderr)
        return RC_FAIL

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = _backup_dir(args) / f"{admin.collection}-{ts}.snapshot"
    # 与 `pg_backup.py` 同一口径：先写 `.part`，再原子改名（2026-10-06 修）。
    # `data` 虽然已经全在内存里，但 `write_bytes` 仍可能被中途打断（磁盘满、
    # 容器被 kill、断电）—— 而截断的 `.snapshot` 从文件名上完全看不出来，
    # 等到真要恢复的那一天才发现它是坏的，那时已经没有别的备份了。
    part = out.with_name(out.name + ".part")
    part.write_bytes(data)
    os.replace(part, out)
    print(f"已保存：{out}（{len(data) / 1024:.1f} KB）")

    # 服务端快照不清的话会一直堆积在容器卷里 —— 卷写满的后果是
    # **Qdrant 直接不可写**（无法再灌库），也就是说"备份"把库搞挂了。
    if args.keep and args.keep > 0:
        rc = _prune(admin, args.keep, dry_run=False)
        if rc != RC_OK:
            return rc
    return RC_OK


def _prune(admin: QdrantAdmin, keep: int, dry_run: bool) -> int:
    snaps = admin.list_snapshots()
    # 按创建时间倒序：保留最新的 keep 份
    snaps.sort(key=lambda s: s.get("creation_time") or "", reverse=True)
    victims = snaps[keep:]
    if not victims:
        print(f"服务端快照 {len(snaps)} 份，无需清理（保留 {keep} 份）")
        return RC_OK
    for s in victims:
        n = s.get("name") or ""
        if dry_run:
            print(f"  [dry-run] 将删除 {n}")
            continue
        try:
            admin.delete_snapshot(n)
            print(f"  已删除服务端快照 {n}")
        except Exception as e:  # noqa: BLE001
            # 删不掉不是致命错误（本地已有副本），但要说清楚 ——
            # 否则卷会被慢慢写满，而那时已经很难联想到是这个原因
            print(f"  ⚠️ 删除 {n} 失败：{e}", file=sys.stderr)
    return RC_OK


def cmd_prune(args) -> int:
    try:
        return _prune(_admin(args), args.keep, dry_run=not args.apply)
    except ConnectionError as e:
        print(f"无法连接：{e}", file=sys.stderr)
        return RC_UNREACHABLE
    except RuntimeError as e:
        print(f"列出快照失败：{e}", file=sys.stderr)
        return RC_FAIL


def cmd_list(args) -> int:
    d = _backup_dir(args)
    files = sorted(d.glob("*.snapshot"))
    print(f"本地备份目录：{d}")
    if not files:
        print("  （空）")
    for f in files:
        st = f.stat()
        mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {f.name:44s} {st.st_size / 1024:10.1f} KB  {mtime}")

    try:
        snaps = _admin(args).list_snapshots()
        print(f"\n服务端快照：{len(snaps)} 份")
        for s in sorted(snaps, key=lambda x: x.get("creation_time") or "", reverse=True):
            print(f"  {s.get('name', '?'):44s} {(s.get('size') or 0) / 1024:10.1f} KB")
    except ConnectionError as e:
        print(f"\n（读不到服务端快照：{e}）")
    except RuntimeError as e:
        print(f"\n（读服务端快照失败：{e}）")
    return RC_OK


def cmd_verify(args) -> int:
    """校验一个快照文件是否「结构完整且可读」。

    为什么值得单独一条命令：**备份文件存在 ≠ 备份可用**。
    下载被截断、磁盘写满写了一半、被 gzip 之外的格式弄坏 —— 这些都不影响
    `ls` 看到文件，只在真正要恢复的那天暴露。快照本质是 tar 归档，
    这里就按 tar 读一遍（能列目录即结构完整）。
    """
    p = Path(args.file)
    if not p.exists():
        print(f"文件不存在：{p}", file=sys.stderr)
        return RC_FAIL
    size = p.stat().st_size
    if size == 0:
        print(f"{p} 是 0 字节 —— 这不是一个有效备份", file=sys.stderr)
        return RC_FAIL
    try:
        with tarfile.open(p, "r:*") as tf:
            names = tf.getnames()
    except Exception as e:  # noqa: BLE001
        print(f"{p} 无法作为 tar 归档读取（文件损坏或不是快照）：{e}", file=sys.stderr)
        return RC_FAIL
    print(f"✅ {p} 可读：{size / 1024:.1f} KB，含 {len(names)} 个条目")
    for n in names[:6]:
        print(f"   - {n}")
    if len(names) > 6:
        print(f"   …还有 {len(names) - 6} 个")
    if not names:
        print("⚠️ 归档是空的 —— 恢复出来会是一个空集合", file=sys.stderr)
        return RC_FAIL
    return RC_OK


def cmd_drill(args) -> int:
    """备份 → 校验 → 打印恢复步骤。**刻意不自动恢复**。

    没被恢复过的备份不算备份 —— 但"自动恢复"同样危险：它会覆盖当前集合，
    若在生产上误触就是把线上数据换成一份可能过期的快照。
    所以这里把"能不能恢复"验证到**文件层**（快照确实可读、结构完整），
    最后一步（真的灌回去）留给人工，并把命令原样打出来。
    """
    rc = cmd_backup(args)
    if rc != RC_OK:
        return rc

    files = sorted(_backup_dir(args).glob("*.snapshot"), key=lambda f: f.stat().st_mtime)
    if not files:
        print("没有找到刚生成的快照文件", file=sys.stderr)
        return RC_FAIL
    newest = files[-1]
    print()
    print("=" * 68)
    print(f"校验最新备份：{newest.name}")
    print("=" * 68)
    rc = cmd_verify(argparse.Namespace(file=str(newest)))
    if rc != RC_OK:
        return rc

    admin = _admin(args)
    print()
    print("=" * 68)
    print("恢复步骤（**人工执行**，会覆盖当前集合）")
    print("=" * 68)
    print(f"""
1) 先确认你真的要回滚 —— 恢复会用快照替换整个集合 `{admin.collection}`，
   期间产生的向量会全部丢失。

2) 上传快照（把 <集合名> 换成目标集合）：
     curl -X POST "{admin.url}/collections/{admin.collection}/snapshots/upload?priority=snapshot" \\
       -H "api-key: $QDRANT_API_KEY" \\
       -F "snapshot=@{newest}"

3) 恢复后核对点数与检索：
     curl -s "{admin.url}/collections/{admin.collection}" | python -m json.tool | grep -i points
     python scripts/verify_pgvector.py        # 或跑一次 eval_retrieval.py 看召回

⚠️ 若 Qdrant 用的是托管服务（Qdrant Cloud），恢复入口在控制台里，
   上面第 2 步的命令仅适用于自建实例。
""")
    return RC_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Qdrant 快照备份 / 校验 / 清理")
    ap.add_argument("--url", default="", help="覆盖 QDRANT_URL")
    ap.add_argument("--api-key", default="", help="覆盖 QDRANT_API_KEY")
    ap.add_argument("--collection", default="", help="覆盖 QDRANT_COLLECTION")
    ap.add_argument("--backup-dir", default="", help="覆盖 BACKUP_DIR")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("backup", help="建快照并下载到本地")
    p.add_argument("--keep", type=int, default=7,
                   help="服务端保留最近 N 份（0 = 不清理）。默认 7")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("list", help="列出本地备份与服务端快照")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("verify", help="校验快照文件结构完整")
    p.add_argument("file")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("prune", help="清理服务端旧快照")
    p.add_argument("--keep", type=int, default=7)
    p.add_argument("--apply", action="store_true", help="默认 dry-run，加它才真删")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("drill", help="备份 + 校验 + 打印恢复步骤")
    p.add_argument("--keep", type=int, default=7)
    p.set_defaults(func=cmd_drill)

    args = ap.parse_args(argv)
    try:
        rc = args.func(args)
    except KeyboardInterrupt:
        print("\n中断", file=sys.stderr)
        return RC_FAIL
    if args.cmd == "drill":
        # 演练结果落 textfile 指标（见 scripts/drill_metrics.py 的说明）
        drill_metrics.publish("qdrant", rc == RC_OK)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
