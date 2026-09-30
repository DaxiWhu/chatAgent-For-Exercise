#!/usr/bin/env bash
# =============================================================================
# chatAgent 灰度发布：改权重 → 观察窗 → 按判据退出码决定「推进 / 回滚 / 转人工」
# =============================================================================
#
# 为什么要有这个脚本
# ----------------
# 在此之前，"发布"是一串手工动作（改 compose 的镜像 tag、改 nginx 权重、
# reload、盯面板、再改权重……）。手工流程的问题不是"麻烦"，而是它**在
# 压力最大的时候最不可靠**：真正需要回滚时，人已经慌了，而回滚恰好是
# 那一串动作里最容易漏掉 reload 的一步。
#
# 本脚本把整条流程固定下来，并且**用退出码驱动决策**（而不是靠人看面板）：
#
#     scripts/canary_check.py 退出 0  → 判据达标      → 推进到下一档
#     scripts/canary_check.py 退出 1  → 判据明确变差  → **立刻回滚**
#     scripts/canary_check.py 退出 2  → 看不到数据    → **什么都别做**，转人工
#
# ⚠️ 第三条是最容易被写成"非 0 就回滚"的地方，而那是错的：退出码 2 表示
#    "数据不足/无法判定"（样本太少、Prometheus 不可达），此时回滚等于
#    回滚一个**其实没问题**的版本，还会把"看不到数据"这个真问题掩盖掉。
#
# 用法
# ----
#   deploy/release.sh ghcr.io/<org>/chatagent:abc1234
#   deploy/release.sh 0.3.0-rc1 --stages 1,10,50,100 --observe 300
#   deploy/release.sh <image> --dry-run          # 只打印将做什么
#   deploy/release.sh <image> --resume           # 从当前权重继续（中断后续跑）
#
# 前置条件
#   - **必须在仓库根目录执行**（`--env-file .env` 是相对当前目录的，
#     少写它整套 `${VAR}` 插值都会退回默认值，见 compose 文件顶部说明）
#   - nginx / app-stable / app-canary 已经在跑
#   - Prometheus 可达（默认 http://127.0.0.1:9090，可用 PROMETHEUS_URL 覆盖）
#
# 退出码
#   0  已放量到最后一档（或 dry-run 正常结束）
#   1  执行失败（环境/命令问题，**不是**判据不达标）
#   2  判据不达标 → 已自动回滚
#   3  判据无法判定 → **已停在当前档位**，需要人工介入
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

COMPOSE=(docker compose --env-file .env -f deploy/app/docker-compose.yml)
PROM_URL="${PROMETHEUS_URL:-http://127.0.0.1:9090}"
STAGES="1,10,50,100"
OBSERVE=300
DRY_RUN=0
RESUME=0
IMAGE=""

RC_OK=0
RC_ERR=1
RC_ROLLED_BACK=2
RC_MANUAL=3

die() { echo "错误：$*" >&2; exit "$RC_ERR"; }

usage() {
  sed -n '2,50p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "$RC_OK"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --stages)   STAGES="${2:?--stages 需要一个逗号分隔的列表}"; shift 2 ;;
    --observe)  OBSERVE="${2:?--observe 需要秒数}"; shift 2 ;;
    --dry-run)  DRY_RUN=1; shift ;;
    --resume)   RESUME=1; shift ;;
    -h|--help)  usage ;;
    -*)         die "未知参数：$1（用 --help 看用法）" ;;
    *)          IMAGE="$1"; shift ;;
  esac
done

[ -n "$IMAGE" ] || die "缺少镜像参数。例：deploy/release.sh ghcr.io/<org>/chatagent:abc1234"
[ -f .env ] || die "找不到 .env（必须在仓库根执行，且 .env 必须存在：整套 \${VAR} 插值都依赖它）"
command -v docker >/dev/null 2>&1 || die "找不到 docker"
[ -f deploy/app/docker-compose.yml ] || die "找不到 deploy/app/docker-compose.yml（当前目录不是仓库根？）"

# 纯本地短标签（没有仓库前缀）时补上本地镜像名前缀，避免误从 Docker Hub 拉
case "$IMAGE" in
  */*) : ;;
  *)   IMAGE="chatagent:$IMAGE" ;;
esac

echo "=========================================="
echo "灰度发布"
echo "  镜像      $IMAGE"
echo "  档位      $STAGES（canary 流量百分比）"
echo "  观察窗    ${OBSERVE}s / 档"
echo "  Prometheus $PROM_URL"
echo "=========================================="

# --- 工具函数 ---------------------------------------------------------------

weights() { python scripts/nginx_weight.py show; }

wait_healthy() {
  local svc="$1" tries="${2:-60}" cid status
  for _ in $(seq 1 "$tries"); do
    cid="$("${COMPOSE[@]}" ps -q "$svc" 2>/dev/null || true)"
    if [ -n "$cid" ]; then
      status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo unknown)"
      if [ "$status" = "healthy" ] || [ "$status" = "running" ]; then
        return 0
      fi
    fi
    sleep 2
  done
  echo "⚠️ app-canary 在等待期间没有进入 healthy/running（最后状态：$status）" >&2
  return 1
}

reload_nginx() {
  # 先 -t 校验语法：**不要**用 `nginx -s reload` 去发现语法错误，
  # reload 失败时旧配置继续生效，很容易被误判成"已经切过去了"。
  "${COMPOSE[@]}" exec -T nginx nginx -t
  "${COMPOSE[@]}" exec -T nginx nginx -s reload
}

check_canary() {
  local out rc
  set +e
  out="$(python scripts/canary_check.py --prometheus-url "$PROM_URL" --json 2>&1)"
  rc=$?
  set -e
  echo "$out" | tail -40
  return "$rc"
}

# --- 1. 拉起灰度实例 --------------------------------------------------------

if [ "$RESUME" -eq 0 ]; then
  echo
  echo "[1/4] 拉取并启动 app-canary（镜像 $IMAGE）"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry-run] docker pull $IMAGE"
    echo "  [dry-run] CANARY_IMAGE=$IMAGE ${COMPOSE[*]} up -d --no-deps app-canary"
  else
    docker pull "$IMAGE" || {
      echo "  拉取失败 —— 检查镜像是否存在、是否已登录 registry。" >&2
      echo "  （本地构建的镜像请先 docker build -t $IMAGE ...）" >&2
      exit "$RC_ERR"
    }
    CANARY_IMAGE="$IMAGE" "${COMPOSE[@]}" up -d --no-deps app-canary
    wait_healthy app-canary || die "app-canary 起不来，**不要继续放量**"
  fi
else
  echo
  echo "[1/4] --resume：跳过拉起，沿用当前运行的 app-canary"
fi

# --- 2. 逐档推进 ------------------------------------------------------------

IFS=',' read -r -a STAGE_LIST <<< "$STAGES"
echo
echo "[2/4] 逐档放量（当前：$(weights)）"

i=0
for pct in "${STAGE_LIST[@]}"; do
  i=$((i + 1))
  pct="${pct// /}"
  [ -n "$pct" ] || continue
  if ! [[ "$pct" =~ ^[0-9]+$ ]] || [ "$pct" -gt 100 ]; then
    die "--stages 里的 '$pct' 不是 0~100 的整数"
  fi
  stable=$((100 - pct))

  echo
  echo "------------------------------------------"
  echo "[档 $i/${#STAGE_LIST[@]}] canary ${pct}%  (stable=$stable canary=$pct)"
  echo "------------------------------------------"

  if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry-run] python scripts/nginx_weight.py set --stable $stable --canary $pct"
    echo "  [dry-run] nginx -t && nginx -s reload"
    echo "  [dry-run] 等待 ${OBSERVE}s 后跑 scripts/canary_check.py"
    continue
  fi

  python scripts/nginx_weight.py set --stable "$stable" --canary "$pct"
  reload_nginx
  echo "  已切换；等待 ${OBSERVE}s 观察窗（期间请勿手工改权重）…"
  sleep "$OBSERVE"

  echo "  运行判据（scripts/canary_check.py）…"
  if check_canary; then
    echo "  ✅ 判据达标 → 继续下一档"
  else
    rc=$?
    if [ "$rc" -eq 1 ]; then
      echo
      echo "  ❌ 判据不达标（退出码 1）→ 自动回滚"
      "$ROOT/deploy/rollback.sh" --reason "canary_check 判定不达标（档 $i，canary ${pct}%）"
      exit "$RC_ROLLED_BACK"
    elif [ "$rc" -eq 2 ]; then
      echo
      echo "  ⚠️ 判据**无法判定**（退出码 2）—— 这既不是通过也不是失败。"
      echo "     已停在 canary ${pct}%，**没有回滚、也没有继续放量**。"
      echo "     请人工确认："
      echo "       - Prometheus 是否可达：curl -s --noproxy '*' $PROM_URL/-/healthy"
      echo "       - 这段时间是否真的没有流量（样本不足）"
      echo "     确认后再决定：重跑本脚本，或执行 deploy/rollback.sh 止血"
      exit "$RC_MANUAL"
    else
      echo "  ⚠️ canary_check 异常退出（码 $rc）——按'无法判定'处理，原地不动。" >&2
      exit "$RC_MANUAL"
    fi
  fi

  if [ "$pct" -eq 100 ]; then
    echo "  已到最后一档（canary 承载全部流量）。"
  fi
done

# --- 3. 收尾提示 ------------------------------------------------------------

echo
echo "[3/4] 完成。当前权重：$(weights)"
# 用带引号的 heredoc（<<'EOF'）：内容里的反引号在**不带引号**的 heredoc 里
# 会被当成命令替换去执行 —— 这类坑不会报错，只会让输出变成一堆
# "command not found" 或者更糟，静默执行了不该执行的命令。
cat <<'EOF'

[4/4] 接下来（本节**刻意不自动化**，因为它是不可逆的改名动作）
  现在 app-canary 跑的是新版本，`app-stable` 跑的是旧版本。真正"换稳"需要三步：
    ① 确认观察期没有回退（看 SLO 面板与错误率，至少跨过一个业务高峰）；
    ② 把 app-stable 的镜像提升到同一个 tag，再按上一步的档位反向切换；
    ③ 稳定后再停掉 app-canary。
  在此之前**别停 app-canary**：它是唯一承载新版本的实例。

  发布记录请写进当天的 .workbuddy/memory/YYYY-MM-DD.md（镜像、档位、每档的判据输出）。
EOF

exit "$RC_OK"
