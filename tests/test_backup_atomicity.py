"""备份落盘的**原子性**守卫（`scripts/pg_backup.py` / `scripts/qdrant_backup.py`）。

为什么这是一类独立的缺陷（2026-10-06）
------------------------------------
"备份写了一半"和"备份写完了"在文件系统上只差一个**长度**，从文件名上完全看不出来。
而备份的消费方全是按**文件名**找最新一份的：

    ls -lt backups/ | head          # RUNBOOK 排查备份告警的第一步
    files = sorted(... .dump)       # 脚本自己的 list

于是中断留下的半截文件会被当成"最近一次成功备份"。更糟的是它**会**通过"文件存在"
这种最朴素的检查 —— 真正的发现时刻是恢复那天，而那时已经没有别的备份了。

修法是先写 `<name>.part`、校验通过后再 `os.replace` 原子改名：
半截文件永远不可能出现在最终名字上，而 `.part` 这个后缀本身就说明"这是没写完的东西"。

⚠️ 本文件的判据是"**最终文件名下没有文件**"，不是"没有残留文件"。
   残留是允许的（也是可见的、可清理的）；被误认成备份才是不允许的。
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PB = _load("pg_backup")
QB = _load("qdrant_backup")


# ---------------------------------------------------------------------------
# pg_backup：被硬中断（Ctrl-C / SIGKILL / 断电）时
# ---------------------------------------------------------------------------
class _FakePgConfig:
    """`do_backup` 真正用到的那几样东西 —— 刻意不构造真实 Config（它要连库）。"""

    dbname = "langgraph_checkpoints"
    user = "langgraph_app"
    backup_dir = "."

    def choose(self, tool):
        return "local"

    def base_cmd(self, tool, mode):
        return [tool, "-U", self.user]

    def _env(self):
        return {}


def test_pg_backup_interrupted_write_leaves_no_file_under_the_final_name(tmp_path, monkeypatch):
    """写入过程被打断 ⇒ 最终文件名下**必须什么都没有**。

    变异验证：把实现改回"直接写最终文件名"（`open(path, "wb")`），本条立刻变红 ——
    因为那时 `chatagent_<ts>.dump` 会带着半截内容出现在备份目录里。
    """
    def _interrupted(cmd, stdout=None, stderr=None, env=None):
        stdout.write(b"PGDMP partial")      # 真的写进去了一点
        stdout.flush()
        raise KeyboardInterrupt             # 模拟 Ctrl-C / 进程被杀

    monkeypatch.setattr(subprocess, "run", _interrupted)

    with pytest.raises(KeyboardInterrupt):
        PB.do_backup(_FakePgConfig(), out_dir=str(tmp_path))

    finals = list(tmp_path.glob("*.dump"))
    assert not finals, (
        f"被中断的备份出现在了最终文件名下：{[f.name for f in finals]} —— "
        "`ls -lt backups/ | head` 会把它当成最近一次成功备份"
    )
    parts = list(tmp_path.glob("*.part"))
    assert len(parts) == 1, "中断的半成品应当以 `.part` 的形式留下（可见、可清理）"
    assert parts[0].read_bytes() == b"PGDMP partial"


def test_pg_backup_success_renames_into_place(tmp_path, monkeypatch, capsys):
    """成功路径：先落 `.part`，最后**只剩**最终文件名（`.part` 不应残留）。"""
    def _ok(cmd, stdout=None, stderr=None, env=None):
        stdout.write(b"PGDMP real content")
        stdout.flush()
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", _ok)

    path = PB.do_backup(_FakePgConfig(), out_dir=str(tmp_path))

    assert Path(path).exists()
    assert Path(path).read_bytes() == b"PGDMP real content"
    assert not list(tmp_path.glob("*.part")), "成功路径不该留下 .part"
    assert "备份完成" in capsys.readouterr().out


def test_pg_backup_list_surfaces_interrupted_leftovers(tmp_path, monkeypatch, capsys):
    """`list` 必须把 `.part` 残留报出来 —— 否则这个机制是隐形的。

    残留文件不会自己消失也不会被 retention 清理（循环只删 `*.dump`），
    不报出来就会一直堆着，而"上次备份曾经中断"这条信息永远不会送到人眼前。
    """
    (tmp_path / "chatagent_20260101_000000.dump").write_bytes(b"ok")
    (tmp_path / "chatagent_20260102_000000.dump.part").write_bytes(b"half")

    cfg = _FakePgConfig()
    cfg.backup_dir = str(tmp_path)
    PB.do_list(cfg)

    out = capsys.readouterr().out
    assert "中断残留" in out
    assert "chatagent_20260102_000000.dump.part" in out


# ---------------------------------------------------------------------------
# qdrant_backup：同一条口径
# ---------------------------------------------------------------------------
def _snapshot_bytes() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        payload = json.dumps({"name": "config.json"}).encode()
        info = tarfile.TarInfo("config.json")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


class _FakeQdrantAdmin:
    """只实现 `cmd_backup` 真正用到的四样东西（不走网络）。"""

    url = "http://127.0.0.1:1"
    collection = "rag_chunks"

    def collection_info(self):
        return {"points_count": 12}

    def create_snapshot(self):
        return {"name": "snap.snapshot", "size": len(_snapshot_bytes())}

    def download_snapshot(self, name):
        return _snapshot_bytes()


def _qargs(tmp_path):
    return type("A", (), {
        "url": "http://127.0.0.1:1", "api_key": "", "collection": "",
        "backup_dir": str(tmp_path), "file": "", "keep": 0, "apply": False,
    })()


def _patch_admin(monkeypatch):
    monkeypatch.setattr(QB, "QdrantAdmin", lambda *a, **kw: _FakeQdrantAdmin())


def test_qdrant_backup_interrupted_write_leaves_no_file_under_the_final_name(tmp_path, monkeypatch):
    """同 pg：写到一半失败时，`.snapshot` 这个名字下不许有文件。

    为什么这条对 qdrant 尤其重要：快照数据的**唯一**消费方就是"恢复"，
    而恢复时脚本只看文件名与结构。半截归档会被 `verify` 判为损坏 ——
    但前提是有人先跑过 `verify`；直接按文件名挑的那条路（人工恢复）
    会拿着一个坏文件去覆盖正在用的集合。
    """
    monkeypatch.delenv("QDRANT_URL", raising=False)
    _patch_admin(monkeypatch)
    real = Path.write_bytes

    def _boom(self, data):
        real(self, data[:8])                # 写进去 8 个字节
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", _boom)

    with pytest.raises(OSError):
        QB.cmd_backup(_qargs(tmp_path))

    finals = list(tmp_path.glob("*.snapshot"))
    assert not finals, (
        f"被中断的快照出现在了最终文件名下：{[f.name for f in finals]}"
    )
    assert list(tmp_path.glob("*.part")), "半成品应当以 `.part` 的形式留下"


def test_qdrant_backup_success_leaves_exactly_one_snapshot_and_no_part(tmp_path, monkeypatch, capsys):
    """成功路径：`.snapshot` 一份、`.part` 零份。"""
    monkeypatch.delenv("QDRANT_URL", raising=False)
    _patch_admin(monkeypatch)

    assert QB.cmd_backup(_qargs(tmp_path)) == QB.RC_OK
    assert len(list(tmp_path.glob("*.snapshot"))) == 1
    assert not list(tmp_path.glob("*.part"))
    assert "已保存" in capsys.readouterr().out
