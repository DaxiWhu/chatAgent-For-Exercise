"""Qdrant 备份脚本（`scripts/qdrant_backup.py`）的行为测试。

为什么值得测
------------
备份脚本的失败模式**全是"看起来成功了"**：

  - 集合为空时备份照常成功 → 产出一堆 0 点快照，而日志写着"备份成功"；
  - 下载被截断（磁盘写满 / 连接中断）→ 文件存在，但恢复时才知道坏了；
  - 服务端快照不清 → 卷被写满，**Qdrant 直接不可写**（"备份"把库搞挂了）；
  - 连不上 Qdrant 时若不区分退出码 → 定时任务把"未启用后端"当成"备份失败"
    反复告警，很快被忽略，真失败也一起被忽略。

这些都不会让脚本崩，只会让**需要恢复的那天发现没有可用备份**。
所以这里测的是判定逻辑，不是"脚本能跑"。
"""

from __future__ import annotations

import importlib.util
import io
import json
import tarfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "qdrant_backup.py"


def _load():
    spec = importlib.util.spec_from_file_location("qdrant_backup", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


QB = _load()


def _make_snapshot_bytes(entries=("config.json", "collection/segments/0")) -> bytes:
    """造一个结构上合法的快照文件（Qdrant 快照本质是 tar 归档）。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in entries:
            payload = json.dumps({"name": name}).encode()
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _args(**kw):
    base = {"url": "", "api_key": "", "collection": "", "backup_dir": "",
            "file": "", "keep": 7, "apply": False}
    base.update(kw)
    return type("A", (), base)()


# ---------------------------------------------------------------------------
# verify：备份文件存在 ≠ 备份可用
# ---------------------------------------------------------------------------
def test_verify_accepts_valid_snapshot(tmp_path, capsys):
    p = tmp_path / "good.snapshot"
    p.write_bytes(_make_snapshot_bytes())
    assert QB.cmd_verify(_args(file=str(p))) == QB.RC_OK
    assert "可读" in capsys.readouterr().out


def test_verify_rejects_empty_file(tmp_path):
    """0 字节文件不能算备份。它只是"那个位置有个东西"。"""
    p = tmp_path / "empty.snapshot"
    p.write_bytes(b"")
    assert QB.cmd_verify(_args(file=str(p))) == QB.RC_FAIL


def test_verify_rejects_corrupted_file(tmp_path):
    """截断 / 写坏的文件必须被拒 —— 这正是"恢复那天才发现"的那类问题。"""
    p = tmp_path / "broken.snapshot"
    p.write_bytes(_make_snapshot_bytes()[: 40])
    assert QB.cmd_verify(_args(file=str(p))) == QB.RC_FAIL


def test_verify_rejects_non_tar(tmp_path):
    p = tmp_path / "notatar.snapshot"
    p.write_bytes(b"this is just text, not a snapshot")
    assert QB.cmd_verify(_args(file=str(p))) == QB.RC_FAIL


def test_verify_rejects_missing_file(tmp_path):
    assert QB.cmd_verify(_args(file=str(tmp_path / "nope.snapshot"))) == QB.RC_FAIL


def test_verify_rejects_empty_archive(tmp_path):
    """归档结构合法但**没有任何条目** ⇒ 恢复出来是个空集合，同样算失败。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz"):
        pass
    p = tmp_path / "noentries.snapshot"
    p.write_bytes(buf.getvalue())
    assert QB.cmd_verify(_args(file=str(p))) == QB.RC_FAIL


# ---------------------------------------------------------------------------
# prune：不清服务端快照会把库写挂
# ---------------------------------------------------------------------------
class _FakeAdmin:
    def __init__(self, snaps):
        self._snaps = snaps
        self.deleted = []

    def list_snapshots(self):
        return list(self._snaps)

    def delete_snapshot(self, name):
        self.deleted.append(name)


def test_prune_keeps_newest_and_deletes_rest(capsys):
    snaps = [{"name": f"s{i}", "creation_time": f"2026-09-{i:02d}T00:00:00Z"}
             for i in range(1, 11)]
    admin = _FakeAdmin(snaps)
    assert QB._prune(admin, keep=3, dry_run=False) == QB.RC_OK
    # 保留 09、10、08（时间最新三个），删掉其余
    assert sorted(admin.deleted) == [f"s{i}" for i in range(1, 8)]


def test_prune_noop_when_under_limit(capsys):
    admin = _FakeAdmin([{"name": "a", "creation_time": "2026-09-01T00:00:00Z"}])
    assert QB._prune(admin, keep=7, dry_run=False) == QB.RC_OK
    assert admin.deleted == []


def test_prune_dry_run_does_not_delete(capsys):
    snaps = [{"name": f"s{i}", "creation_time": f"2026-09-{i:02d}T00:00:00Z"}
             for i in range(1, 6)]
    admin = _FakeAdmin(snaps)
    QB._prune(admin, keep=1, dry_run=True)
    assert admin.deleted == []
    assert "[dry-run]" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 未配置 / 不可达：必须与"真失败"区分开
# ---------------------------------------------------------------------------
def test_missing_url_returns_unreachable_code(monkeypatch, capsys):
    """未配置 QDRANT_URL 时返回 2（不是 1）。

    定时任务据此把"未启用该后端"与"备份真失败"分开播报 ——
    混在一起会让"未启用"每天报一次失败，很快所有人都不看了。
    """
    monkeypatch.delenv("QDRANT_URL", raising=False)
    rc = QB.cmd_backup(_args())
    assert rc == QB.RC_UNREACHABLE


def test_unreachable_host_returns_unreachable_code(tmp_path, monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    # 一个必然连不上的本地端口
    rc = QB.cmd_backup(_args(url="http://127.0.0.1:1"))
    assert rc == QB.RC_UNREACHABLE


def test_loopback_is_not_routed_through_http_proxy(monkeypatch):
    """配了 `http_proxy` 而未配 `NO_PROXY` 时，访问本机 Qdrant **不能**走代理。

    这是一个极常见、且**报错方向完全错误**的坑：开发机/企业网里 http_proxy 通常
    是开着的，而 urllib 只有在 NO_PROXY 里列了本机地址时才豁免。没豁免时请求被
    送进代理，代理连不上本机端口后回一个 502 —— 于是报错看起来是
    "Qdrant 返回了 HTTP 502"（像服务端的问题），实际是**根本没连到 Qdrant**，
    查 Qdrant 的日志什么都不会有。

    ⚠️ 为什么这里断言**构造结果**，而不是"发一个请求看有没有走代理"
    ------------------------------------------------------------
    后者在 Windows 上是**假绿**：urllib 除了环境变量还会读注册表里的代理绕过表
    （默认含 `<local>`），于是"本机被豁免"在开发机上恒真 —— 把实现改回
    `urlopen` 也照样绿。而在 Linux/CI 上是否豁免**只**由 NO_PROXY 决定，
    那才是真正会出事的环境。所以直接断言 opener 的构成：

      - 本机地址 ⇒ ProxyHandler 的 proxies 为空（我们显式传了 `ProxyHandler({})`）；
      - 远端地址 ⇒ 保留默认代理支持（配了 http_proxy 就该用上，否则企业网里连不出去）。
    """
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    # 刻意**清掉** NO_PROXY：真实环境里它经常就是没配
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)

    def proxies_of(admin):
        """取出该 opener 实际会用的代理表。

        返回 None 表示**根本没有 ProxyHandler** —— 这也是"不走代理"，
        而且正是 CPython 的实际行为：`build_opener(ProxyHandler({}))` 会跳过
        默认的 ProxyHandler，再把我们传进去的这个也一并略过，最终列表里
        一个 ProxyHandler 都没有。所以"不走代理"有两种合法形态（None / {}）。
        """
        for h in admin._opener.handlers:
            if isinstance(h, urllib.request.ProxyHandler):
                return dict(h.proxies)
        return None

    local = QB.QdrantAdmin("http://127.0.0.1:6333")
    remote = QB.QdrantAdmin("http://qdrant.example.com:6333")

    assert proxies_of(local) in (None, {}), (
        "本机地址的 opener 带了代理 —— 配了 http_proxy 的机器上，"
        "报错会指向 Qdrant 而不是代理配置"
    )
    assert proxies_of(remote), (
        "远端地址丢掉了代理支持 —— 企业网里（只能经代理出网）会连不出去"
    )


def test_gateway_error_counts_as_unreachable_not_failure(tmp_path, monkeypatch):
    """上游返回 502/503/504 时按"不可达"（退出码 2）处理，不是"备份失败"（1）。

    502 是**代理或反向代理**给出的"上游没在服务"信号，语义上与"连不上"同类。
    对**可选后端**必须报"跳过"：否则一个压根没启用的后端会每天定时告警一次，
    很快所有人都把这条通知屏蔽掉，那时真失败也一起被忽略 —— 这正是本脚本
    区分退出码 2 的全部理由。
    """
    monkeypatch.delenv("QDRANT_URL", raising=False)

    class _Gateway(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            body = b"upstream connect failed"
            self.send_response(502)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        assert QB.cmd_backup(_args(url=url, backup_dir=str(tmp_path), keep=0)) == QB.RC_UNREACHABLE
    finally:
        srv.shutdown()
        srv.server_close()


def test_auth_error_still_counts_as_failure(tmp_path, monkeypatch):
    """401/403 是**配置错**（密钥不对），必须报"失败"而不是"不可达"。

    与上面那条形成对照：如果把"任何非 2xx"都归成"不可达"，那么"API Key 填错了"
    会被永久静默成"该后端未启用"，而实际上备份**一份都没产出** —— 这才是最危险的
    那种失败（看起来一切正常）。
    """
    monkeypatch.delenv("QDRANT_URL", raising=False)

    class _Unauthorized(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            body = b'{"status":{"error":"Unauthorized"}}'
            self.send_response(401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Unauthorized)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        assert QB.cmd_backup(_args(url=url, backup_dir=str(tmp_path), keep=0)) == QB.RC_FAIL
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# 端到端：起一个假 Qdrant，验证"快照真的被下载并落盘"
# ---------------------------------------------------------------------------
class _FakeQdrant(BaseHTTPRequestHandler):
    snapshot_bytes = b""
    requests: list = []

    def log_message(self, *a):  # 静音
        pass

    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        type(self).requests.append(("GET", self.path))
        if self.path.endswith("/snapshots"):
            return self._json(200, {"result": []})
        if "/snapshots/" in self.path:          # 下载
            raw = type(self).snapshot_bytes
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        # 集合信息
        return self._json(200, {"result": {"points_count": 1234}})

    def do_POST(self):  # noqa: N802
        type(self).requests.append(("POST", self.path))
        return self._json(200, {"result": {"name": "snap-2026-09-29.snapshot",
                                          "size": len(type(self).snapshot_bytes)}})


@pytest.fixture()
def fake_qdrant():
    _FakeQdrant.snapshot_bytes = _make_snapshot_bytes()
    _FakeQdrant.requests = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeQdrant)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_backup_downloads_and_persists_snapshot(fake_qdrant, tmp_path, monkeypatch):
    """端到端：真的把快照下载下来、落盘，且落盘的文件能通过 verify。

    这条是"备份真的能用"的最低保障 —— 只测辅助函数（tar 解析、prune 排序）
    无法覆盖"下载返回的字节是不是被正确写入文件"。
    """
    monkeypatch.delenv("QDRANT_URL", raising=False)
    args = _args(url=fake_qdrant, backup_dir=str(tmp_path), keep=0)
    assert QB.cmd_backup(args) == QB.RC_OK

    files = list(tmp_path.glob("*.snapshot"))
    assert len(files) == 1, f"应落盘 1 个快照文件，实际 {[f.name for f in files]}"
    assert files[0].read_bytes() == _FakeQdrant.snapshot_bytes
    # 下载下来的东西必须是**可恢复的**（结构完整），不能只是"字节数对"
    assert QB.cmd_verify(_args(file=str(files[0]))) == QB.RC_OK


def test_backup_request_sequence_is_info_create_download(fake_qdrant, tmp_path, monkeypatch):
    """请求顺序必须是：读集合信息 → 建快照 → 下载快照。

    顺序颠倒的后果很隐蔽：跳过"建快照"直接下载会拿到 404（看着像 bug），
    而不先读点数就无从发现"集合是空的"（那会安静地产出无用备份）。
    """
    monkeypatch.delenv("QDRANT_URL", raising=False)
    QB.cmd_backup(_args(url=fake_qdrant, backup_dir=str(tmp_path), keep=0))
    assert [m for m, _ in _FakeQdrant.requests] == ["GET", "POST", "GET"]


def test_backup_warns_on_empty_collection(fake_qdrant, tmp_path, monkeypatch, capsys):
    """集合点数为 0 时必须**显式告警**（这是"成功但无用"的备份）。"""
    monkeypatch.delenv("QDRANT_URL", raising=False)

    class _Empty(_FakeQdrant):
        def do_GET(self):  # noqa: N802
            if self.path.endswith("/snapshots"):
                return self._json(200, {"result": []})
            if "/snapshots/" in self.path:
                raw = type(self).snapshot_bytes
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            return self._json(200, {"result": {"points_count": 0}})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Empty)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        QB.cmd_backup(_args(url=url, backup_dir=str(tmp_path), keep=0))
        assert "集合为空" in capsys.readouterr().out
    finally:
        srv.shutdown()
        srv.server_close()
