#!/usr/bin/env bash
# Dev hot-swap build 段（docs/runtime-hot-swap-design.md §3.4）：
#   检查 quiesce（无运行中 turn）→ [pytest] → make runtime → blue-green 预检
#   （新 bundle 在独立端口 + temp home 探活验指纹）→ ditto 到
#   dev-override/ginno-runtime.v{sha}-{ts}/ → symlink 原子切 → state.json=pending。
# 之后由壳的 finish_dev_swap 完成重启+验证+回滚（前端事件 / Debug 菜单 / agent）。
#
# 用法： make dev-swap [SKIP_TESTS=1] [FORCE=1]   （FORCE 跳过 quiesce 检查）
# 回出厂： make dev-swap-clean
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME="$ROOT/packages/runtime"
DIST="$RUNTIME/dist/ginno-runtime"
OVERRIDE_DIR="${GINNO_HOME:-$HOME/.ginno}/dev-override"
LINK="$OVERRIDE_DIR/ginno-runtime"
PORT="${GINNO_RUNTIME_PORT:-8787}"
PREFLIGHT_PORT=8793
# 进度上报端口（goal 约定：GINNO_PORT/PORT，默认 8787；回落 GINNO_RUNTIME_PORT）。
PROGRESS_PORT="${GINNO_PORT:-${PORT:-${GINNO_RUNTIME_PORT:-8787}}}"

log()   { printf '%s[dev-swap]%s %s\n' "$C_CYAN" "$C_OFF" "$*"; }
die()   { printf '%s[dev-swap]%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

RUN_ID=""      # = NEW_DIR（目标目录名），进度上报的幂等键
CURRENT_STAGE=""   # die() 时给当前阶段标 fail 用

# report <stage> <state> [detail] [pct]
#   1) 终端日志（保留原样）；2) [devswap-progress] 结构化行（sidecar 逐行
#   解析通道，registry.py _dev_swap_background）；3) curl POST 上报端点。
#   三个通道任何一个失败都静默——进度可视化绝不影响构建。
report() {
  local stage="$1" state="$2" detail="${3:-}" pct="${4:-}"
  CURRENT_STAGE="$stage"
  local json
  json=$(RUN_ID="$RUN_ID" STAGE="$stage" STATE="$state" DETAIL="$detail" PCT="$pct" \
    python3 -c '
import json, os
d = {"run_id": os.environ["RUN_ID"], "stage": os.environ["STAGE"],
     "state": os.environ["STATE"], "detail": os.environ["DETAIL"] or None}
pct = os.environ["PCT"]
if pct:
    d["pct"] = int(pct)
print(json.dumps(d, ensure_ascii=False))
') || return 0
  # [devswap-progress] 前缀行：stdout 解析通道（run_id 为空时解析端会跳过）。
  # 与 curl 双写，端点侧以签名去重。
  printf '[devswap-progress] %s\n' "$json"
  curl -s -m 2 -X POST "http://127.0.0.1:$PROGRESS_PORT/api/devswap/progress" \
    -H 'Content-Type: application/json' -d "$json" >/dev/null 2>&1 || true
}

die_stage() {
  # die 的包装：先给当前阶段标 fail，再按原样报错退出。
  [ -n "$RUN_ID" ] && [ -n "$CURRENT_STAGE" ] && report "$CURRENT_STAGE" fail "$*"
  die "$@"
}
# 颜色仅限 TTY：sidecar 是管道捕获，尾部输出会原样进聊天 notice
# （registry.py _dev_swap_background），转义码漏进气泡就是 [31m 这种乱码。
if [ -t 1 ] || [ -t 2 ]; then
  C_CYAN=$'\033[36m'; C_RED=$'\033[31m'; C_OFF=$'\033[0m'
else
  C_CYAN=''; C_RED=''; C_OFF=''
fi

# ---- 前置 ---------------------------------------------------------------
# GUI 最小 PATH 兜底：/dev-swap 由 sidecar 原样透传 os.environ 起 bash
# （registry.py _dev_swap_background），from-Finder 启动时 PATH 只有
# /usr/bin:/bin:...，而 uv 在 Homebrew、node 在 ~/.local/bin（pnpm 也依赖
# 二者，make runtime → web 链路同样要）。同款先例：rtk_bridge.detect_rtk、
# external_agents/base._resolve_cli 的 extra 目录清单。
for _d in /opt/homebrew/bin /usr/local/bin /opt/homebrew/sbin \
          "$HOME/.local/bin" "$HOME/bin"; do
  case ":$PATH:" in *":$_d:"*) continue ;; esac
  if [ -d "$_d" ]; then PATH="$_d:$PATH"; fi
done
unset _d
export PATH
command -v uv >/dev/null || die "uv 不在 PATH（已兜底 Homebrew 前缀与 ~/.local/bin）"
[ -x "$DIST/ginno-runtime" ] || die "dist bundle 不存在（先跑 make runtime）"

SHA="$(git -C "$ROOT" rev-parse --short HEAD)$(git -C "$ROOT" diff --quiet || printf -- -dirty)"
TS="$(date +%s)"
NEW_DIR="ginno-runtime.v${SHA%%-*}.$TS"   # v{sha8}.{epoch}
FINGERPRINT="$SHA"
RUN_ID="$NEW_DIR"

# ---- 1. quiesce：无运行中 turn（⌘Q gate 同款聚合）------------------------
if [ "${FORCE:-0}" != "1" ]; then
  report quiesce running "检查运行中 turn"
  RUNNING="$(curl -sf --max-time 5 "http://127.0.0.1:$PORT/api/running" 2>/dev/null || echo '{"offline":true}')"
  if [ "${RUNNING#*offline}" != "$RUNNING" ]; then
    report quiesce ok "runtime 不在线，跳过静默检查"
    log "runtime 不在线（$PORT 无响应）——跳过 quiesce/预检，直接进入构建切换"
  else
    BUSY="$(printf '%s' "$RUNNING" | python3 -c '
import json, sys
d = json.load(sys.stdin)
n = sum(1 for s in d.get("sessions", []) if s.get("running"))
n += sum(1 for t in d.get("tasks", []) if not t.get("detached"))
print(n)' 2>/dev/null || echo "?")"
    if [ "$BUSY" != "0" ] && [ "$BUSY" != "?" ]; then
      die_stage "有 $BUSY 个运行中 turn/任务（/api/running）。等它们结束，或 FORCE=1 强制"
    fi
    report quiesce ok "无运行中 turn"
    log "quiesce OK（无运行中 turn）"
  fi
fi

# ---- 2. 单测（默认跑；SKIP_TESTS=1 跳过）--------------------------------
if [ "${SKIP_TESTS:-0}" != "1" ]; then
  report unit running "pytest -m unit"
  log "pytest -m unit …（SKIP_TESTS=1 跳过）"
  UNIT_OUT="$(cd "$RUNTIME" && uv run --group test --extra docs pytest -q -m unit --no-header -x 2>&1 | tail -2)" \
    || die_stage "pytest -m unit 失败：
$UNIT_OUT"
  report unit ok "$UNIT_OUT"
fi

# ---- 3. 构建（内容 hash 增量；跑着 app 也安全）--------------------------
# pct 采样（design §2.1）：build 是耗时大头，PyInstaller 无原生进度——按 dist
# 产物体积增速粗估，5s 一采、封顶 95%（未完成前不谎报 100）。基线 = 构建前
# 旧 bundle 体积；无旧 bundle（首建）用 256MB 经验值。增量命中秒完时循环
# 自然只跑一两拍，无碍。
BASE_KB="$(du -sk "$DIST" 2>/dev/null | awk '{print $1}')"
case "$BASE_KB" in ''|*[!0-9]*) BASE_KB=0 ;; esac
[ "$BASE_KB" -lt 10240 ] && BASE_KB=262144   # <10MB 视为无基线 → 256MB 默认
report build_runtime running "make runtime（PyInstaller）" 1
log "make runtime …"
make -C "$ROOT" runtime &
MAKE_PID=$!
(
  while kill -0 "$MAKE_PID" 2>/dev/null; do
    sleep 5
    K="$(du -sk "$DIST" 2>/dev/null | awk '{print $1}' || echo 0)"
    case "$K" in ''|*[!0-9]*) K=0 ;; esac
    P=$(( K * 100 / BASE_KB ))
    [ "$P" -lt 1 ] && P=1
    [ "$P" -gt 95 ] && P=95
    report build_runtime running "make runtime（PyInstaller）" "$P"
  done
) &
PCT_PID=$!
wait "$MAKE_PID" || { kill "$PCT_PID" 2>/dev/null; wait "$PCT_PID" 2>/dev/null || true; die_stage "make runtime 失败"; }
kill "$PCT_PID" 2>/dev/null; wait "$PCT_PID" 2>/dev/null || true
report build_runtime ok "bundle 已重建" 100

# ---- 4. blue-green 预检：新 bundle 独立端口 + temp home 探活 -------------
# 起不来/指纹不对 → 直接判失败，绝不进入切换（当前运行不受任何影响）。
if curl -sf --max-time 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
  report preflight running "blue-green 预检（:$PREFLIGHT_PORT 试起）"
  log "blue-green 预检：在 :$PREFLIGHT_PORT 试起新 bundle …"
  TMPHOME="$(mktemp -d /tmp/ginno-swap-preflight.XXXXXX)"
  GINNO_HOME="$TMPHOME" GINNO_RUNTIME_PORT="$PREFLIGHT_PORT" \
    "$DIST/ginno-runtime" >/dev/null 2>&1 &
  BG_PID=$!
  cleanup_bg() { kill "$BG_PID" 2>/dev/null || true; wait "$BG_PID" 2>/dev/null || true; }
  trap cleanup_bg EXIT
  OK=0
  for _ in $(seq 1 60); do
    FP="$(curl -sf --max-time 2 "http://127.0.0.1:$PREFLIGHT_PORT/api/health" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("build",{}).get("fingerprint",""))' 2>/dev/null || true)"
    if [ "$FP" = "$FINGERPRINT" ]; then OK=1; break; fi
    sleep 1
  done
  cleanup_bg; trap - EXIT
  rm -rf "$TMPHOME"
  [ "$OK" = "1" ] || die_stage "预检失败：新 bundle 60s 内未在 :$PREFLIGHT_PORT 验证指纹 $FINGERPRINT —— 保持现状，未做任何切换"
  report preflight ok "新 bundle 指纹 $FINGERPRINT"
  log "预检 OK：新 bundle 指纹 $FINGERPRINT"
fi

# ---- 5. ditto 到轮换目录（绝不写运行中路径）-----------------------------
mkdir -p "$OVERRIDE_DIR"
log "ditto → $OVERRIDE_DIR/$NEW_DIR"
ditto "$DIST" "$OVERRIDE_DIR/$NEW_DIR"
# ditto 残缺兜底（2026-11-04 实锤：B 目录为空却继续走到了 switch）：产物
# 二进制不存在 = 拷贝失败，就地判死，绝不切一个空 bundle。
if [ ! -x "$OVERRIDE_DIR/$NEW_DIR/ginno-runtime" ]; then
  die_stage "ditto 产物不完整：$OVERRIDE_DIR/$NEW_DIR/ginno-runtime 缺失"
fi

# ---- 6. 记旧值 → symlink 原子切 → state.json=pending ---------------------
# state.sha 存完整 fingerprint（含 -dirty）：壳的 finish_dev_swap 与
# /api/health 的 build.fingerprint 严格相等比较。
report switch running "symlink → dev-override/$NEW_DIR"
OLD="$(basename "$(readlink "$LINK" 2>/dev/null || echo '')" 2>/dev/null || true)"
# symlink 原子切：mv(1) 在目标是「指向目录的符号链接」时会跟着链接把源
# 挪进目标目录（BSD/macOS 语义——2026-11-04 实锤：旧 bundle 目录里躺着
# 历次 run 的 .tmp-link 残骸，主链接从未被脚本更新过）。改用 rename(2)
# 语义的 os.replace 原子替换，与壳侧 flip_link（std::fs::rename）同款；
# tmp 名也用壳侧同名的点前缀，避免残骸再入目录列表。
TMP_LINK="$OVERRIDE_DIR/.ginno-runtime.tmp-link"
rm -f "$TMP_LINK"
ln -sfn "$NEW_DIR" "$TMP_LINK" 2>/dev/null || ln -sf "$NEW_DIR" "$TMP_LINK"
python3 -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' "$TMP_LINK" "$LINK"
# 切完即验：链接确实指到新目录，否则后面所有验证都在验错的 bundle。
[ "$(readlink "$LINK" 2>/dev/null)" = "$NEW_DIR" ] || die_stage "symlink 切换未生效（仍指向 $(readlink "$LINK" 2>/dev/null || echo none)）"

# 新 bundle 的 built_at（build-info.json）：壳的指纹验证除 fingerprint 外
# 加验它——同一棵脏工作树连续构建 fingerprint 相同，只比指纹会把「ladder
# 落到 factory/旧 bundle」验成假阳性（2026-11-04 实锤，shell.log 三条
# stale 后跟 verified）。
BUILT_AT="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("built_at") or "")' "$DIST/_internal/build-info.json" 2>/dev/null || true)"

python3 - "$OVERRIDE_DIR/state.json" "$NEW_DIR" "$OLD" "$FINGERPRINT" "$BUILT_AT" <<'PY'
import json, os, sys
path, new, old, sha, built_at = sys.argv[1:6]
state = {
    "state": "pending",
    "new": new,
    "old": old or "",
    "sha": sha,
    # 新 bundle 构建时刻（ISO-8601 Z）：finish_dev_swap 与 /api/health 的
    # build.built_at 严格比对，堵住同指纹假阳性。
    "built_at": built_at,
    "created": int(__import__("time").time()),
    "requested_by": os.environ.get("DEV_SWAP_REQUESTED_BY", "cli"),
    # 发起会话（/dev-swap 命令路径由 registry 注入 env；make dev-swap 为空）：
    # 重启后新进程的 finish 终态上报 + 前端结果落块都以此定位会话。
    "session_id": os.environ.get("DEV_SWAP_SESSION_ID", ""),
}
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(state, f, ensure_ascii=False, indent=2)
os.replace(tmp, path)
print(json.dumps(state, ensure_ascii=False))
PY

report switch ok "state=pending，等壳收尾"
report finish running "壳重启 sidecar + 指纹验证"
log "✅ symlink 已切到 $NEW_DIR，state=pending。"
log "下一步（自动）：前端收到 runtime.dev_swap_pending 事件后 invoke finish_dev_swap；"
log "手动等价：Debug 菜单 → 完成热切换（Finish Dev Swap）。"
