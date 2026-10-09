"""密钥入库链路的守卫（SOPS + age）。

为什么需要这一份
--------------
这一步的每一步都**要么生效、要么静默失效**，而且失效时看起来完全正常：

  1. `path_regex` 匹配的是**相对于 `.sops.yaml` 所在目录**的路径，用的是 Go 的
     `regexp.MatchString`（未锚定、区分大小写）。写成绝对路径或漏了 `^`，
     规则会在别处悄悄命中。
  2. **Windows 上参与匹配的字符串带的是反斜杠**（`--filename-override` 先过
     `filepath.Abs`）。实测：正则只写 `^secrets/` 时，加密**当场失败**并报
     "no matching creation rules found" —— 配置看起来完全正确，只是在 Windows 上
     一次都没生效过。
  3. `--filename-override` 忘传时同样是那句报错：`creation_rules` 是按**输入路径**
     （`.env`）匹配的，而输入根本不在 `secrets/` 下。
  4. 密文名字的后缀决定格式（`.env` ⇒ 逐键信封；其它 ⇒ 整块二进制），
     两种都"能用"，但后者看不到有哪些键、没法按单键增删改。
  5. 密文一旦被 `.gitignore` 吃掉，`git add` 会**静默跳过** —— 加密了半天，
     仓库里什么都没有。

前四条只能靠断言钉住（跑不了 sops 的地方也得能验），第五条用 `git check-ignore`
做**行为级**校验（与 `test_monitoring_config.py` 里 SMTP 密码那条同一手法：
读 `.gitignore` 的文本是弱断言，反选、上级目录、`info/exclude` 都判断不了）。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import tasks

ROOT = Path(__file__).resolve().parents[1]
SOPS_CFG = ROOT / ".sops.yaml"
GITLEAKS_CFG = ROOT / ".gitleaks.toml"
SECRETS_DOC = ROOT / "deploy" / "SECRETS.md"

CIPHER = "secrets/app.sops.env"          # 入库的密文
PLAIN = ".env"                            # 只在本机的明文
PLACEHOLDER = "AGE_PUBLIC_KEY_PLACEHOLDER"

# ⚠️ 上面两条是**独立写死的约定**，刻意不复用 `tasks.SECRETS_CIPHER`。
#    复用的话，谁改了那个常量，这些断言就跟着改 —— "两边一起漂"就没人发现了。
#    `.sops.yaml`、`.gitignore`、`deploy/SECRETS.md` 都是按这个路径写的，
#    所以由下面 `test_task_constant_matches_the_documented_path` 把两者钉在一起。
SECRET_KEY_RE_TEMPLATE = r"(?<![\w-]){}(?![\w-])"


def _mentions(text: str, token: str) -> bool:
    """带词边界地找一个标识符 —— `in` 是**子串**判断，`secrets-decryptt` 也算命中。

    这个坑本仓库已经踩过两次（见 `test_monitoring_config.py` 的 `_series_in`）：
    用 `in` 判断"文档里提了某个名字"，等于没有判断。
    """
    return re.search(SECRET_KEY_RE_TEMPLATE.format(re.escape(token)), text) is not None

# 实测确认过的两件事，写死成常量：
#   - 密文里每个值都被包成 ENC[AES256_GCM,...]（逐键信封形态的标志）
#   - 参与 path_regex 匹配的字符串在 Windows 上是反斜杠
ENVELOPE = "ENC[AES256_GCM,"
WIN_CIPHER = CIPHER.replace("/", "\\")


# --------------------------------------------------------------------------
# 解析辅助
# --------------------------------------------------------------------------
def _rules() -> list[dict]:
    cfg = yaml.safe_load(SOPS_CFG.read_text(encoding="utf-8"))
    assert isinstance(cfg, dict) and cfg.get("creation_rules"), (
        ".sops.yaml 里没有 creation_rules —— sops 会报 no matching creation rules found"
    )
    return cfg["creation_rules"]


def _matches(pattern: str, rel_path: str) -> bool:
    """复刻 sops 的匹配语义：`regexp.MatchString`（未锚定）、区分大小写。

    刻意用 `re.search` 而不是 `fullmatch` —— 源码里没有加锚点，`^`/`$` 由配置自己写。
    本用例复刻这个语义，正是为了能验出"配置漏写了锚点"。
    """
    try:
        return re.search(pattern, rel_path) is not None
    except re.error as exc:  # 正则写坏了：报错要指到配置上，而不是抛在测试里
        pytest.fail(f".sops.yaml 的 path_regex 不是合法正则：{exc}")


def _git_ignores(rel_path: str) -> bool:
    """问 git 本身：这个路径会不会被 `git add` 跳过。"""
    if not (ROOT / ".git").exists():
        pytest.skip("不在 git 仓库里（源码打包场景），跳过行为级校验")
    proc = subprocess.run(["git", "check-ignore", "-q", rel_path], cwd=ROOT, capture_output=True)
    assert proc.returncode in (0, 1), (
        f"`git check-ignore {rel_path}` 返回了 {proc.returncode}（既不是 0 也不是 1）—— "
        "守卫本身失效了，先确认 git 可用"
    )
    return proc.returncode == 0


# --------------------------------------------------------------------------
# ① 配置本身
# --------------------------------------------------------------------------
def test_sops_config_has_a_rule_that_actually_covers_the_ciphertext():
    """密文路径必须命中**某一条** creation rule，且那条规则带 age 收件人。"""
    hits = [r for r in _rules()
            if r.get("path_regex") and _matches(r["path_regex"], CIPHER)]
    assert hits, (
        f".sops.yaml 里没有任何 creation rule 命中 {CIPHER} —— "
        f"`sops --encrypt` 会直接报 'no matching creation rules found'。"
        f"现有规则：{[r.get('path_regex') for r in _rules()]}"
    )
    assert any(r.get("age") for r in hits), (
        f"命中 {CIPHER} 的那条规则没有 `age:` 收件人 —— 没有收件人就没人能解密"
    )


def test_sops_rule_is_anchored_to_the_secrets_directory():
    """开头的 `^` 是**承重**的：sops 用的是未锚定的子串匹配。

    少了它，`vendor/secrets/x` 这类无关路径也会命中 —— 规则会跑去加密别人的密钥文件。

    刻意**不**断言结尾有 `$`：本规则的尾巴是 `.+`，加上与不加匹配的是同一批路径
    （`MatchString` 就是 `search`，而路径里不会有换行）。一条不影响结果的断言，
    在有人把尾巴改写成更严格的形式时会变成误报，而误报的守卫最终会被删掉。
    """
    for rule in _rules():
        pat = rule.get("path_regex")
        if not pat or not _matches(pat, CIPHER):
            continue
        assert pat.startswith("^"), (
            f"path_regex 缺少开头的 `^`：{pat!r} —— 未锚定的子串匹配会让它命中 "
            f"`vendor/{CIPHER}` 这类无关路径"
        )
        for decoy in (f"vendor/{CIPHER}", f"backup/{CIPHER}", f"x{CIPHER}"):
            assert not _matches(pat, decoy), (
                f"path_regex {pat!r} 把无关路径 {decoy!r} 也命中了"
            )
        return
    pytest.fail(f"没有规则命中 {CIPHER}")


def test_sops_rule_accepts_both_path_separators():
    """正则必须同时接受 `/` 与 `\\` —— Windows 上 sops 传进来的就是反斜杠。

    实测（`tmp_probe/sops_probe.py`）：把分隔符写成只认 `/` 时，Windows 上加密
    **一次都不会成功**，报的还是那句和"配置写错了"无从区分的
    "no matching creation rules found"。这条断言就是那次实测的固化。
    """
    for rule in _rules():
        pat = rule.get("path_regex")
        if not pat or not _matches(pat, CIPHER):
            continue
        assert _matches(pat, WIN_CIPHER), (
            f"path_regex {pat!r} 只认 `/`，不认 Windows 的反斜杠 {WIN_CIPHER!r} —— "
            f"分隔符要写成 [\\\\/]，否则这条配置在 Windows 上永远不生效"
        )
        return
    pytest.fail(f"没有规则命中 {CIPHER}")


# --------------------------------------------------------------------------
# ② 密文的名字与 git 行为
# --------------------------------------------------------------------------
def test_task_constant_matches_the_documented_path():
    """把 `scripts/tasks.py` 里的常量和上面那份"写死的约定"钉在一起。

    两边都改名而没人发现，是最典型的分叉方式：任务把密文写到 A、文档让人去 B 找。
    """
    assert tasks.SECRETS_CIPHER == CIPHER, (
        f"tasks.SECRETS_CIPHER = {tasks.SECRETS_CIPHER!r}，"
        f"而 .sops.yaml / .gitignore / SECRETS.md 都按 {CIPHER!r} 写 —— 两边已经分叉"
    )
    assert tasks.SECRETS_PLAIN == PLAIN, (
        f"tasks.SECRETS_PLAIN = {tasks.SECRETS_PLAIN!r}，约定是 {PLAIN!r}"
    )


def test_ciphertext_name_ends_with_dot_env():
    """后缀决定 sops 的格式推断：`.env` ⇒ Dotenv（逐键信封），其余 ⇒ Binary（整块）。

    实测过 Binary 形态**也能解回来**，差别在粒度：看不到有哪些键、没法按单键增删改、
    diff 也看不出改了哪条密钥 —— 而"换某一条密钥"是这一步每天都在做的事。
    """
    assert tasks.SECRETS_CIPHER.endswith(".env"), (
        f"密文叫 {tasks.SECRETS_CIPHER!r}，后缀不是 `.env` —— "
        f"sops 的 FormatForPath 会退化成 Binary store（整块加密），"
        f"虽然能解回来，但看不到有哪些键、不能按单键增删改"
    )
    assert not Path(tasks.SECRETS_CIPHER).name.startswith(".env."), (
        "密文名以 `.env.` 打头 —— 会被 .gitignore 的 `.env.*` 命中（那条模式不带斜杠，"
        "匹配任意层级），`git add` 会静默跳过"
    )


def test_ciphertext_is_committable_but_plaintext_is_not():
    """这两条必须**同时**成立，缺一个就意味着"密钥只在本地"或"明文进了仓库"。"""
    assert not _git_ignores(CIPHER), (
        f"{CIPHER} 被 git 忽略了 —— 加密出来的东西**提交不上去**，"
        f"而 `git add` 是静默跳过的，没有任何提示"
    )
    assert _git_ignores(PLAIN), (
        f"{PLAIN} 没有被 git 忽略 —— 一次 `git add -A` 就会把全部明文密钥提交进版本库"
    )


def test_ciphertext_temp_file_is_ignored():
    """加解密是"临时文件 + 原子替换"，中断时可能留下半成品密文。

    内容是密文（不是泄露），但半成品比没有更误导人：它看起来像"已经加密好了"。
    """
    assert _git_ignores(f"{CIPHER}.tmp"), (
        f"{CIPHER}.tmp 没有没忽略 —— scripts/tasks.py 的 _run_to_file 会在被中断时"
        f"留下它，而它不该被提交"
    )


# --------------------------------------------------------------------------
# ③ 任务入口的行为（拦住真实 argv，不读源码文本）
# --------------------------------------------------------------------------
@pytest.fixture
def staged(monkeypatch, tmp_path):
    """把 tasks 的 ROOT 指到临时目录，并假装 sops 已安装、记录将要执行的 argv。"""
    calls: list[tuple[list[str], Path]] = []

    monkeypatch.setattr(tasks, "require", lambda tool, hint: f"/fake/{tool}")
    monkeypatch.setattr(tasks, "ROOT", tmp_path)

    def fake_run_to_file(argv, dest, *, mode=0o600):
        calls.append((list(argv), dest))
        return 0

    monkeypatch.setattr(tasks, "_run_to_file", fake_run_to_file)
    (tmp_path / "secrets").mkdir(parents=True, exist_ok=True)
    return tmp_path, calls


def test_encrypt_passes_filename_override(staged):
    """`--filename-override` 不是可选项。

    `creation_rules` 是按**输入路径**匹配的，输入是 `.env`（不在 `secrets/` 下），
    少这个参数就报 "no matching creation rules found" —— 实测过。
    这条守卫防的是"有人把它简化成 `sops -e .env > secrets/app.sops.env`"。
    """
    root, calls = staged
    (root / PLAIN).write_text("A=1\n", encoding="utf-8")

    assert tasks.task_secrets_encrypt([]) == 0
    (argv, dest) = calls[0]

    assert "--filename-override" in argv, f"加密命令没有 --filename-override：{argv}"
    assert argv[argv.index("--filename-override") + 1] == tasks.SECRETS_CIPHER, (
        f"--filename-override 给的不是密文路径：{argv}"
    )
    assert argv[-1] == PLAIN, f"输入不是仓库根的 {PLAIN}：{argv}"
    assert dest == root / tasks.SECRETS_CIPHER


def test_encrypt_refuses_when_plaintext_is_missing(staged):
    """没有明文还硬加密，只会写出一个空密文 —— 而那比没有更糟。"""
    root, calls = staged
    assert tasks.task_secrets_encrypt([]) != 0
    assert not calls, "明文不存在时不该执行任何 sops 命令"


def test_decrypt_refuses_to_clobber_without_force(staged):
    """本机那份明文里可能有还没加密进去的改动，覆盖是不可逆的数据丢失。"""
    root, calls = staged
    (root / "secrets" / "app.sops.env").write_text(ENVELOPE + "x]]\n", encoding="utf-8")
    (root / PLAIN).write_text("本机独有的改动=1\n", encoding="utf-8")

    assert tasks.task_secrets_decrypt([]) != 0, (
        "`.env` 已存在时解密仍然执行了 —— 会把本机还没加密的改动整份覆盖掉"
    )
    assert not calls, "拒绝时不该执行任何 sops 命令"
    assert (root / PLAIN).read_text(encoding="utf-8") == "本机独有的改动=1\n"


def test_decrypt_runs_when_forced(staged):
    root, calls = staged
    (root / "secrets" / "app.sops.env").write_text(ENVELOPE + "x]]\n", encoding="utf-8")
    (root / PLAIN).write_text("旧值=1\n", encoding="utf-8")

    assert tasks.task_secrets_decrypt(["--force"]) == 0
    argv, dest = calls[0]
    assert argv == ["sops", "--decrypt", tasks.SECRETS_CIPHER], argv
    assert dest == root / PLAIN


def test_decrypt_refuses_when_ciphertext_is_missing(staged):
    root, calls = staged
    assert tasks.task_secrets_decrypt([]) != 0
    assert not calls


# --------------------------------------------------------------------------
# ④ 文档与实测结论
# --------------------------------------------------------------------------
def test_secrets_doc_documents_the_actual_commands():
    """文档要写**真实存在**的命令名与路径，否则照着做的人会以为是自己敲错了。

    ⚠️ 用**词边界**匹配而不是 `token in text`：后者在文本被写成
    `secrets-decryptt` 时照样为真 —— 而那种"只差一个字母"的错，正是它要防的。
    """
    text = SECRETS_DOC.read_text(encoding="utf-8")
    for token in (CIPHER, "secrets-encrypt", "secrets-decrypt", ".sops.yaml"):
        assert _mentions(text, token), (
            f"deploy/SECRETS.md 里没有提到 {token!r} —— 文档与实现已经分叉"
        )


def test_gitleaks_config_does_not_allowlist_the_ciphertext():
    """**刻意**没有为密文加放行，而且是实测结论。

    实测（gitleaks 8.30.1）：扫密文 0 命中；加上放行再扫也是 0 命中（放行是多余的）；
    对照组（明文复制成密文名）4 处命中。⇒ 一旦按路径放行那个文件，
    真正的兜底（"那个路径下不该出现明文"）就失效了，换来的是一条永远不生效的规则。
    """
    text = GITLEAKS_CFG.read_text(encoding="utf-8")
    # 去掉注释行，只看**生效的配置** —— 本文件里那段解释性注释本身就含 sops/secrets 字样
    body = re.sub(r"(?m)^[ \t]*#.*$", "", text)
    for word in ("sops", "secrets"):
        assert word not in body.lower(), (
            f"`.gitleaks.toml` 的生效配置里出现了 {word!r} —— "
            "实测表明密文本来就不会命中（见 tmp_probe/gitleaks_probe.py），"
            "按路径放行反而会放走'明文出现在密文路径'这种真事故。"
            "如果确实需要新增放行，请先跑那个对照实验，并同步改本用例。"
        )


def test_ciphertext_on_disk_is_really_ciphertext():
    """等第一份密文真的提交上来之后，这条才开始生效。

    没有它的话，最危险的形态是"有人把明文 `.env` 复制成 `secrets/app.sops.env` 提交"：
    `.gitignore` 不会拦（路径本来就该入库），唯一的兜底是 CI 的 gitleaks（实测能抓住），
    但那要等到 CI 跑完。这里再加一道本地的、更早的闸门。
    """
    path = ROOT / CIPHER
    if not path.exists():
        pytest.skip(f"{CIPHER} 还没生成（等首次 secrets-encrypt）")

    text = path.read_text(encoding="utf-8", errors="replace")
    assert ENVELOPE in text, (
        f"{CIPHER} 里找不到 {ENVELOPE!r} —— 它看起来不是 SOPS 密文"
        f"（很可能是直接把明文复制过去了）"
    )

    cfg_text = SOPS_CFG.read_text(encoding="utf-8")
    assert PLACEHOLDER not in cfg_text, (
        f"{CIPHER} 已经存在，但 .sops.yaml 里还是占位符 {PLACEHOLDER!r} —— "
        f"这份密文是用别的钥匙加的，下次谁也解不开"
    )


# ---------------------------------------------------------------------------
# "共享密钥怎么轮换"这个问题已随路径一起消失
# ---------------------------------------------------------------------------
# 本节此前钉的是共享密钥的形态与轮换窗口：`TENANT_KEYS` 让同一租户并存两把 key
# （于是 `deploy/SECRETS.md` §4.2.1 那份迁移清单能零停机），而 `SERVE_API_KEY`
# 是**一个**字符串、"整串相等"判定 ⇒ 换值的生效时刻旧钥匙同时作废。
#
# 两条前提现在都没了：`TENANT_KEYS` 早先随"租户改由 **JWT 载荷**承载"删除，
# `SERVE_API_KEY` 本身随后被**整体删除**（2026-10-05，见 `app/main.py` 顶部）。
#
# 所以"重叠轮换窗口"这个难题不再存在 —— 服务里已经没有"一把字符串决定谁能进"
# 的凭证形态。保留下面的用例，是把它当成**删除登记**：若有人重新引入第二种共享
# 凭证，这里的断言会立刻失败，逼他先回答"它怎么轮换、怎么归因到人"。
def test_no_shared_secret_knob_can_change_authz_outcome():
    """服务端**没有**任何"共享密钥 / 开关"形态的配置能改变鉴权结果。

    为什么逐个点名而不是按后缀扫：`config` 里合法地存在着**别的** `*_API_KEY`
    （模型与链路追踪的上游凭证，如 `AGNES_API_KEY` / `LANGSMITH_API_KEY`），
    它们不参与本服务鉴权。所以这里点名的是**已删除的那几个**：
      · `SERVE_API_KEY`  —— 共享密钥通行证（`X-API-Key`）；
      · `TENANT_KEYS`     —— "租户 ↔ API Key"对照表；
      · `REQUIRE_AUTH`    —— "关掉鉴权要求"的开关。
    三个都在 `config` 与 `main` 两处都不许复活：只删一处的实现会留下"配了不生效"
    或"没配也能用"的错位。
    """
    from app import config as conf
    from app import main

    for name in ("SERVE_API_KEY", "TENANT_KEYS", "REQUIRE_AUTH"):
        assert not hasattr(conf, name), f"config 里又出现了已删除的鉴权开关 {name}"
        assert not hasattr(main, name), f"main 里又出现了已删除的鉴权开关 {name}"


def test_a_bare_key_string_is_not_a_credential():
    """把"看起来像密钥"的字符串当凭证传进去 ⇒ 401，且提示要**去登录换令牌**。

    覆盖的是"忘了加 `Bearer ` 前缀"这种真实故障：调用方手里有密钥、服务端也
    配好了账号，但仍然 401。此时错误文案必须把人指向"登录换令牌"，否则排障会
    绕到"是不是密钥配错了 / 服务没起来"上去 —— 而共享密钥时代它就是对的。
    """
    from fastapi import HTTPException

    from app import main

    with pytest.raises(HTTPException) as exc:
        main._authenticate("script-key")
    assert exc.value.status_code == 401
    assert "auth/login" in exc.value.detail, (
        "401 的文案没有指向登录接口 —— 会让排障以为密钥配错了"
    )

