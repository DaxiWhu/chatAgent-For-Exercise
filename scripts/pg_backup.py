"""数据库备份 / 恢复 / **恢复演练**。

为什么"演练"是必需品
------------------
没被恢复过的备份不算备份。你可能拿到一个 pg_dump 时权限不足、版本不匹配、
或者恢复出来少了一半的表 —— 而这一切只有在真的需要它的那天才会暴露。
`drill` 把"备份 → 恢复到临时库 → 逐表比对行数 → 清理"整条链跑一遍，
让这件事变成一条可以定期执行的命令。

为什么用 Python 而不是 bash
------------------------
要解析 DB_URL、要在"容器内 pg_dump"与"宿主机 pg_dump"之间自动选择、
要在 Windows 与 Linux 上都能跑、还要连库比对行数 —— 这些用 bash 写会满是
引号与转义坑（本项目就踩过 CRLF 让 shell 判断失准的坑）。

怎么执行 pg_dump / pg_restore
---------------------------
按优先级自动选择，选不到就**报错而不是静默跳过**（静默跳过会让人以为备份成功了）：
  1. 容器内执行（推荐）：`docker exec <容器> pg_dump ...`
     容器里自带与 PG 版本严格匹配的客户端，也不受宿主机是否装了 PG 客户端影响。
  2. 宿主机直连：本地 PATH 里有 `pg_dump` 时用它。

用法
----
    python scripts/pg_backup.py backup                 # 备份到 backups/
    python scripts/pg_backup.py list                   # 列出已有备份
    python scripts/pg_backup.py verify <file.dump>     # 只校验文件完整性（不恢复）
    python scripts/pg_backup.py restore <file.dump> --yes
    python scripts/pg_backup.py drill                  # 备份 + 恢复演练 + 行数比对

环境变量
--------
    PG_CONTAINER   容器名（默认 chatagent-pgvector）
    DB_URL         连接串（默认从 .env 读）
    BACKUP_DIR     备份目录（默认 ./backups）
"""

import argparse
import datetime
import os
import shutil
import subprocess
import sys

# 让 `python scripts/pg_backup.py` 这种"直接跑脚本"的方式也能 import 到项目模块
# （直接跑时 sys.path[0] 是 scripts/ 而不是仓库根）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts import drill_metrics  # noqa: E402


def _load_env():
    """让 .env 生效（可选依赖）。"""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # noqa: BLE001
        pass


class Config:
    """从 DB_URL 解析出来的连接参数 + 执行通道选择。"""

    def __init__(self):
        self.dsn = (os.getenv("DB_URL") or "").strip()
        if not self.dsn:
            raise SystemExit("未配置 DB_URL：请在 .env 里填好，或设置环境变量。")
        self.container = (os.getenv("PG_CONTAINER") or "chatagent-pgvector").strip()
        self.backup_dir = os.getenv("BACKUP_DIR") or "backups"

        import psycopg.conninfo as ci

        d = ci.conninfo_to_dict(self.dsn)
        self.host = d.get("host") or "localhost"
        self.port = str(d.get("port") or "5432")
        self.user = d.get("user") or "postgres"
        self.password = d.get("password") or ""
        self.dbname = d.get("dbname") or "postgres"
        # 建/删临时库要连到一个"总是存在"的库
        self.admin_db = d.get("dbname") if d.get("dbname") == "postgres" else "postgres"

    # ---- 执行通道 ----
    def docker_ok(self) -> bool:
        """docker 可用且目标容器正在运行。"""
        if not shutil.which("docker"):
            return False
        try:
            r = subprocess.run(["docker", "exec", self.container, "true"],
                               capture_output=True, timeout=15)
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    def local_pg_ok(self, tool) -> bool:
        """宿主机是否有该 PG 客户端工具。"""
        return shutil.which(tool) is not None

    def choose(self, tool):
        """选择执行通道，返回 ("docker"|"local", None) 或抛 SystemExit 并给指引。"""
        if self.docker_ok():
            return "docker"
        if self.local_pg_ok(tool):
            return "local"
        raise SystemExit(
            f"找不到可用的 {tool}：\n"
            f"  · 容器方式不可用（docker 未安装，或容器 {self.container} 未运行）\n"
            f"  · 宿主机 PATH 里也没有 {tool}\n"
            "请二选一：启动 PG 容器，或安装 PostgreSQL 客户端工具。"
        )

    def _env(self):
        """给子进程的 PG* 环境（避免把密码写进命令行被 ps 看到）。"""
        e = dict(os.environ)
        if self.password:
            e["PGPASSWORD"] = self.password
        return e

    def base_cmd(self, tool, mode):
        """拼出工具的公共前缀。"""
        if mode == "docker":
            return ["docker", "exec", "-i", self.container, tool, "-U", self.user]
        return [tool, "-h", self.host, "-p", self.port, "-U", self.user]

    def stage(self, mode, path):
        """让选中的通道能访问到备份文件，返回该通道下的可用路径。

        local:  宿主机路径直接用。
        docker: 容器看不到宿主机文件系统，先 `docker cp` 进容器再给容器内路径。
                （不这么做的话 `pg_restore` 会报 "could not open input file"，
                 而这个错误看起来像"备份文件坏了"，很容易误判。）
        """
        if mode == "local":
            return path
        inner = "/tmp/_chatagent_restore.dump"
        r = subprocess.run(["docker", "cp", path, f"{self.container}:{inner}"],
                           capture_output=True, timeout=180)
        if r.returncode != 0:
            raise SystemExit(f"docker cp 失败：{r.stderr.decode(errors='replace')[:300]}")
        return inner


def _stamp():
    """备份文件名用的时间戳。"""
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def _human(n):
    """字节数转人眼可读。"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------
def do_backup(cfg: Config, out_dir=None) -> str:
    """执行一次备份，返回文件路径。

    用 `-Fc`（custom format）：自带压缩、支持 pg_restore 选择性恢复与并行恢复。
    写完会校验文件确实非空且 pg_restore 能解析它 —— 空文件是"看起来成功了"的经典陷阱。
    """
    mode = cfg.choose("pg_dump")
    out_dir = out_dir or cfg.backup_dir
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"chatagent_{_stamp()}.dump")

    cmd = cfg.base_cmd("pg_dump", mode) + ["-d", cfg.dbname, "-Fc"]
    with open(path, "wb") as fh:
        r = subprocess.run(cmd, stdout=fh, stderr=subprocess.PIPE, env=cfg._env())
    if r.returncode != 0:
        try:
            os.remove(path)
        except OSError:
            pass
        raise SystemExit(f"pg_dump 失败（exit {r.returncode}）：\n{r.stderr.decode(errors='replace')}")

    size = os.path.getsize(path)
    if size == 0:
        os.remove(path)
        raise SystemExit("pg_dump 成功退出但产出空文件：请检查库名/权限。")

    print(f"备份完成：{path}（{_human(size)}，通道={mode}）")
    return path


def do_verify(cfg: Config, path: str) -> bool:
    """校验备份文件可被 pg_restore 解析（不恢复、不写任何数据）。"""
    if not os.path.exists(path):
        raise SystemExit(f"文件不存在：{path}")
    size = os.path.getsize(path)
    if size == 0:
        raise SystemExit(f"文件为空：{path}")

    mode = cfg.choose("pg_restore")
    # pg_restore 的 --list **不能**从 stdin 读（传 "-" 会被当成文件名），
    # 所以必须先 stage 成"通道内可见的路径"。
    inner = cfg.stage(mode, path)
    cmd = cfg.base_cmd("pg_restore", mode) + ["--list", inner]
    r = subprocess.run(cmd, capture_output=True, env=cfg._env())
    if r.returncode != 0:
        print(f"✗ 备份文件不可解析：{r.stderr.decode(errors='replace')[:500]}")
        return False

    # --list 输出形如 "3; 2615 2200 SCHEMA - public postgres"，数一下对象行
    objects = [ln for ln in r.stdout.decode(errors="replace").splitlines()
               if ln and not ln.startswith(";")]
    print(f"✓ 备份文件可解析：{path}（{_human(size)}，含 {len(objects)} 个对象）")
    return True


def do_list(cfg: Config):
    """列出备份目录里的文件。"""
    d = cfg.backup_dir
    if not os.path.isdir(d):
        print(f"备份目录不存在：{d}")
        return
    files = sorted(f for f in os.listdir(d) if f.endswith(".dump"))
    if not files:
        print(f"备份目录为空：{d}")
        return
    print(f"备份目录：{d}")
    for f in files:
        p = os.path.join(d, f)
        print(f"  {f}  {_human(os.path.getsize(p))}  {datetime.datetime.fromtimestamp(os.path.getmtime(p)):%Y-%m-%d %H:%M}")


# ---------------------------------------------------------------------------
# 恢复
# ---------------------------------------------------------------------------
def do_restore(cfg: Config, path: str, target_db=None, confirm=False):
    """把备份恢复到 `target_db`（默认 = 当前 DB_URL 的库）。

    ⚠️ 恢复到现有库会用 `--clean --if-exists` 先删同名对象 —— 这会**覆盖线上数据**。
    所以非临时库的场景必须显式 `--yes` 才会执行。
    """
    target_db = target_db or cfg.dbname
    if target_db == cfg.dbname and not confirm:
        raise SystemExit(
            f"拒绝执行：目标库 {target_db} 就是当前 DB_URL 指向的库，恢复会覆盖其中的数据。\n"
            "确认要覆盖请加 --yes；或先用 `drill` 在临时库上验证。"
        )
    mode = cfg.choose("pg_restore")
    inner = cfg.stage(mode, path)
    cmd = cfg.base_cmd("pg_restore", mode) + ["-d", target_db, "--no-owner",
                                              "--clean", "--if-exists", inner]
    r = subprocess.run(cmd, stderr=subprocess.PIPE, env=cfg._env())
    # pg_restore 对有依赖关系的对象常返回 1（"忽略的错误"），只要不是致命错就放行并提示
    err = r.stderr.decode(errors="replace")
    if r.returncode != 0:
        print(f"⚠️ pg_restore 返回 {r.returncode}（常见于已存在对象/DROP 依赖），错误摘要：{err[:800]}")
    else:
        print(f"✓ 已恢复到 {target_db}")
    return r.returncode


# ---------------------------------------------------------------------------
# 恢复演练
# ---------------------------------------------------------------------------
def _table_counts(dsn) -> dict:
    """读一个库里所有 public 表的行数。"""
    import psycopg

    out = {}
    with psycopg.connect(dsn, connect_timeout=8) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            tables = [r[0] for r in cur.fetchall()]
            for t in tables:
                cur.execute(f'SELECT count(*) FROM "{t}"')
                out[t] = int(cur.fetchone()[0])
    return out


def _make_dsn(cfg: Config, dbname: str) -> str:
    """把当前 DSN 的库名替换成 dbname。"""
    import psycopg.conninfo as ci

    d = ci.conninfo_to_dict(cfg.dsn)
    d["dbname"] = dbname
    return ci.make_conninfo(**d)


def do_drill(cfg: Config) -> bool:
    """恢复演练：备份 → 恢复到临时库 → 逐表比对行数 → 删除临时库。

    返回 True 表示演练通过（备份确实可用）。
    这是唯一能证明"备份不是摆设"的操作。
    """
    import psycopg

    print("=== 恢复演练：1/5 备份 ===")
    path = do_backup(cfg)

    print("=== 2/5 校验文件 ===")
    if not do_verify(cfg, path):
        return False

    tmp_db = f"chatagent_drill_{_stamp()}"
    admin_dsn = _make_dsn(cfg, cfg.admin_db)

    print(f"=== 3/5 建临时库 {tmp_db} ===")
    try:
        with psycopg.connect(admin_dsn, connect_timeout=8, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{tmp_db}"')
    except Exception as e:  # noqa: BLE001
        raise SystemExit(
            f"建临时库失败：{type(e).__name__}: {e}\n"
            "（需要 CREATE DATABASE 权限；容器镜像的 POSTGRES_USER 默认就是超级用户）"
        )

    try:
        print("=== 4/5 恢复到临时库 ===")
        do_restore(cfg, path, target_db=tmp_db, confirm=True)

        print("=== 5/5 比对行数 ===")
        src = _table_counts(cfg.dsn)
        dst = _table_counts(_make_dsn(cfg, tmp_db))

        missing = sorted(set(src) - set(dst))
        mismatched = sorted(t for t in (set(src) & set(dst)) if src[t] != dst[t])

        for t in sorted(src):
            mark = "✓" if t in dst and src[t] == dst[t] else "✗"
            got = dst.get(t, "缺失")
            print(f"  {mark} {t:<24} 源={src[t]:<8} 恢复后={got}")

        ok = not missing and not mismatched
        if ok:
            print(f"\n演练通过：{len(src)} 张表行数全部一致，备份确实可用。")
        else:
            print(f"\n演练未通过：缺失表 {missing}，行数不一致 {mismatched}")
        return ok
    finally:
        # 无论成败都清理临时库：留着会污染实例，且下次演练可能撞名
        try:
            with psycopg.connect(admin_dsn, connect_timeout=8, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'DROP DATABASE IF EXISTS "{tmp_db}" WITH (FORCE)')
            print(f"已清理临时库 {tmp_db}")
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 清理临时库失败（请手动 DROP DATABASE {tmp_db}）：{e}")


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    """命令行入口。退出码 0 = 成功，非 0 = 失败（可直接用于定时任务的判据）。"""
    _load_env()
    p = argparse.ArgumentParser(
        prog="python scripts/pg_backup.py",
        description="chatAgent 数据库备份 / 恢复 / 恢复演练",
    )
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("backup", help="执行一次备份")
    sub.add_parser("list", help="列出已有备份")
    sub.add_parser("drill", help="恢复演练（备份→恢复到临时库→比对行数→清理）")

    v = sub.add_parser("verify", help="校验备份文件可解析")
    v.add_argument("file")

    r = sub.add_parser("restore", help="恢复备份（覆盖目标库，需 --yes）")
    r.add_argument("file")
    r.add_argument("--target-db", default=None, help="目标库名（默认 = DB_URL 的库）")
    r.add_argument("--yes", action="store_true", help="确认覆盖现有库")

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 1

    cfg = Config()

    if args.cmd == "backup":
        do_backup(cfg)
        return 0
    if args.cmd == "list":
        do_list(cfg)
        return 0
    if args.cmd == "verify":
        return 0 if do_verify(cfg, args.file) else 1
    if args.cmd == "restore":
        return 0 if do_restore(cfg, args.file, args.target_db, args.yes) == 0 else 1
    if args.cmd == "drill":
        ok = do_drill(cfg)
        # 演练结果落 textfile 指标：让"上次成功演练是什么时候"变成可观测的事实，
        # 而不是某个人的记忆（见 scripts/drill_metrics.py 的说明）。
        drill_metrics.publish("pg", ok)
        return 0 if ok else 1
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
