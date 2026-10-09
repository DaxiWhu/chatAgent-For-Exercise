"""生成 / 校验 `AUTH_USERS` 的口令哈希（第二档用户鉴权的配套工具）。

为什么需要它
------------
`AUTH_USERS` 每条要写成一个 PBKDF2 编码串：

    alice:pbkdf2_sha256$600000$<salt_hex>$<hash_hex>:acme:reviewer

这个东西**不可能手写**：salt 要 16 字节密码学随机、哈希要 60 万次 PBKDF2 迭代。
于是本脚本把「生成」这一步收敛成一个命令，避免出现"凑一个看起来像的串进去"
（那样服务不会报错、只是所有人永远登不进去）。

用法
----
    python -m scripts.hash_password                     # 交互输入密码，只打印哈希串
    python -m scripts.hash_password -u alice -t acme     # 直接给出可粘贴的 AUTH_USERS 条目
    python -m scripts.hash_password -u alice -t acme -r reviewer,admin
    python -m scripts.hash_password --check alice        # 排障：拿当前配置验一次密码

⚠️ 不要用 `--password`（明文会进 shell history 与 `ps` 输出）。密码默认交互输入，
   只在自动化场景才从 stdin 管道读取（`echo '...' | python -m scripts.hash_password`）。

⚠️⚠️ 与 docker compose 的冲突（本项目部署形态下必读）
----------------------------------------------------
PBKDF2 编码串里含 `$`，而 **Compose 会对未加引号的值做变量插值**（官方文档原话：
「Unquoted and double-quoted values have interpolation applied」）。
hex 串以字母开头时（例如 `$a1b2c3...`）恰好匹配**未加花括号**的变量名规则
⇒ 被当成变量引用 ⇒ 查不到同名环境变量 ⇒ 替换成**空串**。

后果（这是最要命的地方：全程无报错）：
    写入前  pbkdf2_sha256$600000$a1b2...$c3d4...
    容器内  pbkdf2_sha256$600000$$
    ⇒ `verify_password` 切出空 salt / 空哈希 ⇒ 永远返回 False
    ⇒ 现象是「密码明明是对的，就是登不进去」，而你会在密码上排查半天。

所以**整条值必须加单引号**（单引号值按字面处理，不插值）：

    AUTH_USERS='alice:pbkdf2_sha256$600000$abc...$def...:acme:reviewer'

本脚本在 `-u/-t` 模式下会直接打印**带引号的成品**，照抄即可。

    ⚠️ 反向提醒：本地直跑（`app/main.py` 用 python-dotenv）时它只插值 `${VAR}`
       花括号形式，未加花括号的 `$hex` 不受影响 ⇒ **本地能登进去，不代表容器里能登进去**。
       加单引号两边都对，没有理由不加。

排障
----
`--check <user>` 读**服务实际看到的配置**（先加载 `.env`，再读 `app.config.AUTH_USERS`），
提示输入密码并当场校验，同时检查哈希串结构是否完好 —— 若结构已损坏，
会直接指出"疑似被 compose 插值吃掉"，省掉一轮猜测。
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 会破坏 AUTH_USERS 的行格式（解析靠 `:` 切分、靠 `,` 分用户、靠 `|` 分角色）
_FORBIDDEN = {
    "用户名": (":", ","),
    "租户": (":", ","),
    "角色": (":", ",", "|"),
}


def _load_app_modules():
    """延迟导入 `app` 模块，返回 `(auth, config)`。

    ⚠️ 两个原因，都**必须**是"调用时"而不是"模块导入时"：

    1. **顺序**：要先加载 `.env` 再导入 `app.config` —— `config` 的所有常量都在
       导入期从环境里取好，顺序反了就永远读到"没有 `.env` 时"的值。
    2. **零副作用**：模块顶层若调 `load_dotenv()`，那么任何 `import` 本脚本的代码
       （例如测试）都会顺带把**开发机的 `.env`** 灌进 `os.environ`。这会破坏
       `tests/conftest.py` 刻意建立的前提 ——「测试进程等价于裸环境」，而破坏它的
       表现是**与顺序相关的、看起来毫不相干的失败**（真实事故：改一行 `.env`
       导致全量 35 条全 401）。
    """
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:  # pragma: no cover - 缺 dotenv 时退化为只读进程环境
        pass

    from app import auth as userauth
    from app import config

    return userauth, config


def _read_password(from_stdin: bool) -> str:
    """取得明文密码：交互提示（默认）或 stdin 管道。"""
    if from_stdin:
        line = sys.stdin.readline()
        if not line:
            raise SystemExit("错误：stdin 里没有读到密码")
        return line.rstrip("\n\r")

    if not sys.stdin.isatty():
        # 非交互且没加 --stdin：直接静默读 stdin 会让"忘了管道"变成一段奇怪的等待，
        # 明确报错更好。
        raise SystemExit(
            "错误：当前不在交互终端。请用管道传入密码（--stdin 或 `echo ... | ...`），"
            "或改用交互终端运行。"
        )

    pw = getpass.getpass("密码: ")
    again = getpass.getpass("再输入一次: ")
    if pw != again:
        raise SystemExit("错误：两次输入不一致")
    return pw


def _resolve_password(args) -> str:
    """按优先级取得明文密码：`--password` > `--stdin`/管道 > 交互提示。"""
    if args.password is not None:
        print(
            "⚠️ 已使用 --password：明文密码会留在 shell history 与 ps 输出里，"
            "生产环境请改用交互输入。",
            file=sys.stderr,
        )
        return args.password
    if args.stdin and sys.stdin.isatty():
        # 加了 --stdin 却连着终端：读下去会静默挂起等人输入，明确报错更好。
        raise SystemExit("错误：--stdin 需要密码来自管道或重定向，当前是交互终端。")
    return _read_password(from_stdin=args.stdin or not sys.stdin.isatty())


def _validate_component(label: str, value: str) -> None:
    for ch in _FORBIDDEN[label]:
        if ch in value:
            raise SystemExit(f"错误：{label} 不能包含 {ch!r}（会破坏 AUTH_USERS 的行格式）：{value!r}")


def build_entry(user: str, tenant: str, roles, pwhash: str) -> str:
    """拼出 `AUTH_USERS` 的单条记录（`user:hash:tenant[:role|role]`）。"""
    entry = f"{user}:{pwhash}:{tenant}"
    if roles:
        entry += ":" + "|".join(roles)
    return entry


def check(user: str, args, userauth, config) -> int:
    """排障模式：以服务实际看到的配置校验一次密码。"""
    users = getattr(config, "AUTH_USERS", {}) or {}
    entry = users.get(user)
    if entry is None:
        known = ", ".join(sorted(users)) or "（无）"
        print(f"未找到用户 {user!r}。当前 AUTH_USERS 中的用户：{known}", file=sys.stderr)
        return 3

    stored = entry.get("hash") or ""
    parts = stored.split("$")
    # 结构完好性先于密码正确性检查：结构坏了的话"密码错"是必然结果，
    # 不先排除掉就会让人一直怀疑密码。
    if len(parts) != 4 or not parts[2] or not parts[3]:
        print(
            "哈希串结构已损坏（salt/hash 为空）：\n"
            f"  实际读到: {stored!r}\n"
            "最可能的原因：`.env` 里 AUTH_USERS 的值**没有加单引号**，\n"
            "被 docker compose 的变量插值把 `$<hex>` 段替换成了空串。\n"
            "解决：整条值加单引号 → AUTH_USERS='user:pbkdf2_sha256$...$...:tenant'",
            file=sys.stderr,
        )
        return 4

    pw = _resolve_password(args)
    if userauth.verify_password(pw, stored):
        print(
            f"通过：{user} @ {entry.get('tenant')} "
            f"roles={list(entry.get('roles') or ()) or '（无）'}"
        )
        return 0
    print("不通过：密码与该哈希不匹配（哈希串结构正常，问题在密码本身）", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="生成 AUTH_USERS 的口令哈希（PBKDF2-HMAC-SHA256）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m scripts.hash_password -u alice -t acme -r reviewer,admin\n"
            "  python -m scripts.hash_password --check alice\n"
        ),
    )
    ap.add_argument("-u", "--user", help="用户名（给了它才会输出完整 AUTH_USERS 条目）")
    ap.add_argument("-t", "--tenant", help="该用户所属租户（与 -u 搭配）")
    ap.add_argument("-r", "--roles", default="", help="角色，逗号分隔，例如 reviewer,admin")
    ap.add_argument("-i", "--iterations", type=int, default=None, help="PBKDF2 迭代数（默认取配置值）")
    ap.add_argument(
        "--password",
        default=None,
        help="直接提供密码（⚠️ 会留在 shell history 与 ps 输出里，不推荐）",
    )
    ap.add_argument("--stdin", action="store_true", help="从 stdin 读一行作为密码（自动化用）")
    ap.add_argument("--check", metavar="USER", help="排障：校验当前配置里某用户的密码")
    args = ap.parse_args(argv)

    userauth, config = _load_app_modules()

    if args.check:
        return check(args.check, args, userauth, config)

    if bool(args.user) != bool(args.tenant):
        raise SystemExit("错误：-u/--user 与 -t/--tenant 必须同时提供")

    pw = _resolve_password(args)
    if not pw:
        raise SystemExit("错误：空密码等于一条「任何人都能登录」的记录，已拒绝")

    pwhash = userauth.hash_password(pw, iterations=args.iterations)

    if args.iterations is not None:
        # 不去复制 app.auth 里的迭代数上下限（复制会漂移），而是直接看**生成结果的真实值**：
        # 两者不一致就说明被收敛了，必须说出来 —— 否则"我指定了 2000000"与
        # "实际是边界值"之间的差异是完全静默的。
        actual = int(pwhash.split("$")[1])
        if actual != args.iterations:
            print(
                f"⚠️ 请求的迭代数 {args.iterations} 超出允许区间，实际使用 {actual}。",
                file=sys.stderr,
            )

    # 自检：生成完立刻回验一次。哈希串是"生成完几乎不会再看"的东西，
    # 一旦生成过程出错（或哈希编码格式与 verify 不同步），错误会一直潜伏到用户登录才暴露。
    if not userauth.verify_password(pw, pwhash):
        raise SystemExit("内部错误：生成的哈希自检未通过，已中止（请勿使用该串）")

    if not args.user:
        print(pwhash)
        return 0

    _validate_component("用户名", args.user)
    _validate_component("租户", args.tenant)
    roles = [r.strip() for r in args.roles.split(",") if r.strip()]
    for r in roles:
        _validate_component("角色", r)

    entry = build_entry(args.user, args.tenant, roles, pwhash)
    print("# 粘贴到 .env（必须保留单引号，否则 docker compose 会插值吃掉 $hex 段）：")
    print(f"AUTH_USERS='{entry}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
