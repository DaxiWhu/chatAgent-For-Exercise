#!/usr/bin/env bash
# =============================================================================
# chatAgent 回滚：把灰度流量归零，并**验证**流量真的回到了稳定版
# =============================================================================
#
# 设计取向：回滚必须是**一条命令、幂等、且自带验证**
# -------------------------------------------------
# 出问题时人处在压力下，任何需要现场推导的步骤都会被跳过或做错。所以：
#
#   - **幂等**：重复执行结果相同（权重再设一遍、reload 再发一次都无害）；
#   - **自带验证**：改完不只是打印"已回滚"，而是真的打几次入口、
#     确认 `release` 字段全部是 stable —— 因为"改了配置"与"流量真的回去了"
#     是两件事，而后者才是你要的；
#   - **先 nginx -t 再 reload**：语法错时 reload 会失败但旧配置继续生效，
#     看起来"没报错"，实则权重没变；
#   - **不删容器**：默认保留 app-canary（它就是证据）。
#
# 用法
# ----
#   deploy/rollback.sh
#   deploy/rollback.sh --reason "5xx 从 0.1% 涨到 4%"
#   deploy/rollback.sh --stop-canary          # 顺便停掉灰度实例（事后清理时用）
#   deploy/rollback.sh --to chatagent:0.1.0   # 顺手把灰度实例也换回旧镜像
#
# 退出码
#   0  回滚完成且验证通过
#   1  执行失败（**必须人工接手**：可能 nginx 起不来了）
#   2  权重已改，但验证没通过（流量可能仍有一部分进灰度）
#
# 通知
# ----
# 脚本结束时（**任何退出路径**，包括失败）会向 `alert-sink` 发一条"回滚已执行"，
# 把这次动作记进值班群。receiver 默认 oncall，可用 ROLLBACK_RECEIVER 覆盖。
# **通知失败不影响回滚结果** —— 它是记录，不是止血的一部分。
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

COMPOSE=(docker compose --env-file .env -f deploy/app/docker-compose.yml)
# 与 release.sh 同源：镜像 tag 的真源是仓库根 VERSION（compose 里是 `${APP_VERSION:-<版本>}`）
APP_VERSION="${APP_VERSION:-$(cat VERSION 2>/dev/null || echo 0.0.0-unknown)}"
export APP_VERSION
ENTRY="${ROLLBACK_ENTRY:-http://127.0.0.1:8080}"
REASON="（未填写原因）"
STOP_CANARY=0
TO_IMAGE=""
SAMPLES=10

RC_OK=0
RC_ERR=1
RC_VERIFY_FAILED=2

die() { echo "错误：$*" >&2; exit "$RC_ERR"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --reason)      REASON="${2:?--reason 需要一句话}"; shift 2 ;;
    --stop-canary) STOP_CANARY=1; shift ;;
    --to)          TO_IMAGE="${2:?--to 需要镜像引用}"; shift 2 ;;
    -*)            die "未知参数：$1" ;;
    *)             die "多余的位置参数：$1" ;;
  esac
done

[ -f .env ] || die "找不到 .env（必须在仓库根执行）"
command -v docker >/dev/null 2>&1 || die "找不到 docker"

# --- 回滚通知 ---------------------------------------------------------------
# 通知走 alert-sink，而不是把 webhook URL 从这里读出来直接用：通道选择
# （wecom/feishu/generic）与凭据只存在于网关进程的环境里，在这里再实现一遍
# 渲染 + 通道分支，等于让同一套约定有两个实现 —— 而它们早晚会漂。
# alert-sink 只挂在 monitor 网络上、没有宿主端口映射，所以从容器内部调它自己。
ROLLBACK_RECEIVER="${ROLLBACK_RECEIVER:-oncall}"

notify_rollback() {
  local rc="$1" verdict
  case "$rc" in
    0)  verdict="✅ 回滚完成且验证通过" ;;
    2)  verdict="⚠️ 权重已改，但**验证未通过**（可能仍有流量进灰度）" ;;
    *)  verdict="❌ 回滚**执行失败**，需要人工接手" ;;
  esac
  local summary="回滚已执行：${verdict}。原因：${REASON}。版本：${APP_VERSION}${TO_IMAGE:+；目标镜像：${TO_IMAGE}}"

  if "${COMPOSE[@]}" exec -T alert-sink python alert_sink.py \
       --notify "$ROLLBACK_RECEIVER" --summary "$summary" >/dev/null 2>&1; then
    echo "（已发出回滚通知 → receiver=$ROLLBACK_RECEIVER）"
  else
    echo "⚠️ 回滚通知**没能发出**（alert-sink 未运行 / 通道未配置）。" >&2
    echo "   这不影响回滚结果 —— 但「刚刚回过滚」这件事没进群，请手工同步一次。" >&2
  fi
}

# 用 trap 覆盖**所有**退出路径。只在成功分支里发通知，会刚好漏掉最需要通知的
# 两种情形：验证未通过（2）与执行失败（1）—— 而它们才是要叫醒人的那些。
on_exit() {
  local rc=$?
  notify_rollback "$rc"
  exit "$rc"
}
trap on_exit EXIT

echo "=========================================="
echo "回滚：把 canary 权重归零"
echo "  原因  $REASON"
echo "  时间  $(date -Iseconds)"
echo "=========================================="
echo "回滚前：$(python scripts/nginx_weight.py show)"

# --- 1. 权重归零 ------------------------------------------------------------

echo
echo "[1/3] 权重 → canary 0"
python scripts/nginx_weight.py set --canary 0

echo
echo "[2/3] 校验语法并重载 nginx"
# 顺序不能反：先 -t 后 -s reload。reload 失败时旧配置仍然生效，
# 而"旧配置生效"的表现和"忘了 reload"完全一样 —— 只有 -t 能提前拦住语法错。
"${COMPOSE[@]}" exec -T nginx nginx -t || die "nginx 配置语法有误（**未重载**，旧配置仍在跑）；修好后重跑本脚本"
"${COMPOSE[@]}" exec -T nginx nginx -s reload
echo "后：$(python scripts/nginx_weight.py show)"

# --- 2. 验证：打入口，确认 release 字段全是 stable --------------------------

echo
echo "[3/3] 验证流量真的回到稳定版（打入口 $SAMPLES 次，看 /health/live 的 release 字段）"
if ! command -v curl >/dev/null 2>&1; then
  echo "⚠️ 本机没有 curl，跳过端到端验证。请手工执行：" >&2
  echo "   for i in \$(seq 1 10); do curl -s --noproxy '*' $ENTRY/health/live; echo; done" >&2
  echo "   并确认每一条里的 \"release\" 都是 stable。" >&2
  # 刻意**不算失败**：没能验证 ≠ 回滚失败。但要说清楚"你没有得到确认"。
  echo
  echo "回滚动作已完成，但**未经验证**。别把它当成'已验证回滚成功'。" >&2
elif ! curl -s --noproxy '*' --max-time 5 "$ENTRY/health/live" >/dev/null 2>&1; then
  echo "❌ 入口 $ENTRY 打不通 —— 这本身可能就是故障的一部分。" >&2
  echo "   权重已归零，但请立刻确认 nginx 与 app-stable 是否正常：" >&2
  echo "   ${COMPOSE[*]} ps" >&2
  exit "$RC_ERR"
else
  seen=""
  hit_canary=0
  for _ in $(seq 1 "$SAMPLES"); do
    body="$(curl -s --noproxy '*' --max-time 5 "$ENTRY/health/live" || true)"
    if printf '%s' "$body" | grep -q '"release"[[:space:]]*:[[:space:]]*"canary"'; then
      hit_canary=$((hit_canary + 1))
    fi
    if printf '%s' "$body" | grep -q '"release"[[:space:]]*:[[:space:]]*"stable"'; then
      seen="stable"
    fi
  done

  if [ "$hit_canary" -gt 0 ]; then
    echo "❌ $SAMPLES 次里有 $hit_canary 次仍落到 canary。" >&2
    echo "   → 权重已改但**流量没全回来**。常见原因：" >&2
    echo "     ① reload 没生效（看 nginx 日志）；② 另有入口/第二个 upstream；" >&2
    echo "     ③ 客户端连接复用（keepalive 到旧实例）—— 隔 30 秒再验一次。" >&2
    echo "   在确认之前，**不要**重启任何实例（重启会丢掉现场证据）。" >&2
    exit "$RC_VERIFY_FAILED"
  fi
  if [ "$seen" != "stable" ]; then
    echo "⚠️ 打通了，但响应里没有可识别的 release 字段（$SAMPLES 次都没匹配到）。" >&2
    echo "   这不是失败，但也**没有得到确认**。请手工看一条原始响应：" >&2
    echo "   curl -s --noproxy '*' $ENTRY/health/live" >&2
  else
    echo "✅ 连续 $SAMPLES 次全部落到 stable，且没有一次落到 canary。"
  fi
fi

# --- 3. 可选：把灰度实例也换回旧镜像 / 停掉它 --------------------------------

if [ -n "$TO_IMAGE" ]; then
  echo
  echo "[附加] 把 app-canary 镜像换回 $TO_IMAGE"
  CANARY_IMAGE="$TO_IMAGE" "${COMPOSE[@]}" up -d --no-deps app-canary
  echo "   已换回（此时它 weight=0，不接流量，只作为下次发布的预置位）"
fi

if [ "$STOP_CANARY" -eq 1 ]; then
  echo
  echo "[附加] 停掉 app-canary"
  echo "      ⚠️ 停之前请确认已经保存现场（日志/指标时间窗）——"
  echo "         docker stop 之后容器的 stdout 就没法再用 docker logs 取了。"
  "${COMPOSE[@]}" stop app-canary
fi

echo
echo "=========================================="
echo "回滚完成。请立刻做两件事："
echo "  1) 把现场记下来（告警值、日志片段、时间点）——"
echo "     回滚前的那几分钟证据是**唯一**能定位根因的东西，重启/重建会抹掉它。"
echo "  2) 在 deploy/monitoring/ERROR_BUDGET_POLICY.md 的框架下判断这次要不要"
echo "     触发发布冻结，并约一次复盘。"
echo "  3) 确认下面那行回滚通知**已发出** —— 没发出就手工同步一次。"
echo "=========================================="
exit "$RC_OK"
