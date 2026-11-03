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

log() { printf '\033[36m[dev-swap]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[dev-swap]\033[0m %s\n' "$*" >&2; exit 1; }

# ---- 前置 ---------------------------------------------------------------
command -v uv >/dev/null || die "uv 不在 PATH"
[ -x "$DIST/ginno-runtime" ] || die "dist bundle 不存在（先跑 make runtime）"

SHA="$(git -C "$ROOT" rev-parse --short HEAD)$(git -C "$ROOT" diff --quiet || printf -- -dirty)"
TS="$(date +%s)"
NEW_DIR="ginno-runtime.v${SHA%%-*}.$TS"   # v{sha8}.{epoch}
FINGERPRINT="$SHA"

# ---- 1. quiesce：无运行中 turn（⌘Q gate 同款聚合）------------------------
if [ "${FORCE:-0}" != "1" ]; then
  RUNNING="$(curl -sf --max-time 5 "http://127.0.0.1:$PORT/api/running" 2>/dev/null || echo '{"offline":true}')"
  if [ "${RUNNING#*offline}" != "$RUNNING" ]; then
    log "runtime 不在线（$PORT 无响应）——跳过 quiesce/预检，直接进入构建切换"
  else
    BUSY="$(printf '%s' "$RUNNING" | python3 -c '
import json, sys
d = json.load(sys.stdin)
n = sum(1 for s in d.get("sessions", []) if s.get("running"))
n += sum(1 for t in d.get("tasks", []) if not t.get("detached"))
print(n)' 2>/dev/null || echo "?")"
    if [ "$BUSY" != "0" ] && [ "$BUSY" != "?" ]; then
      die "有 $BUSY 个运行中 turn/任务（/api/running）。等它们结束，或 FORCE=1 强制"
    fi
    log "quiesce OK（无运行中 turn）"
  fi
fi

# ---- 2. 单测（默认跑；SKIP_TESTS=1 跳过）--------------------------------
if [ "${SKIP_TESTS:-0}" != "1" ]; then
  log "pytest -m unit …（SKIP_TESTS=1 跳过）"
  (cd "$RUNTIME" && uv run --group test --extra docs pytest -q -m unit --no-header -x 2>&1 | tail -2)
fi

# ---- 3. 构建（内容 hash 增量；跑着 app 也安全）--------------------------
log "make runtime …"
make -C "$ROOT" runtime

# ---- 4. blue-green 预检：新 bundle 独立端口 + temp home 探活 -------------
# 起不来/指纹不对 → 直接判失败，绝不进入切换（当前运行不受任何影响）。
if curl -sf --max-time 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
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
  [ "$OK" = "1" ] || die "预检失败：新 bundle 60s 内未在 :$PREFLIGHT_PORT 验证指纹 $FINGERPRINT —— 保持现状，未做任何切换"
  log "预检 OK：新 bundle 指纹 $FINGERPRINT"
fi

# ---- 5. ditto 到轮换目录（绝不写运行中路径）-----------------------------
mkdir -p "$OVERRIDE_DIR"
log "ditto → $OVERRIDE_DIR/$NEW_DIR"
ditto "$DIST" "$OVERRIDE_DIR/$NEW_DIR"

# ---- 6. 记旧值 → symlink 原子切 → state.json=pending ---------------------
# state.sha 存完整 fingerprint（含 -dirty）：壳的 finish_dev_swap 与
# /api/health 的 build.fingerprint 严格相等比较。
OLD="$(basename "$(readlink "$LINK" 2>/dev/null || echo '')" 2>/dev/null || true)"
ln -sfn "$NEW_DIR" "$LINK.tmp-link" 2>/dev/null || ln -sf "$NEW_DIR" "$LINK.tmp-link"
mv -f "$LINK.tmp-link" "$LINK"

python3 - "$OVERRIDE_DIR/state.json" "$NEW_DIR" "$OLD" "$FINGERPRINT" <<'PY'
import json, os, sys
path, new, old, sha = sys.argv[1:5]
state = {
    "state": "pending",
    "new": new,
    "old": old or "",
    "sha": sha,
    "created": int(__import__("time").time()),
    "requested_by": os.environ.get("DEV_SWAP_REQUESTED_BY", "cli"),
}
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(state, f, ensure_ascii=False, indent=2)
os.replace(tmp, path)
print(json.dumps(state, ensure_ascii=False))
PY

log "✅ symlink 已切到 $NEW_DIR，state=pending。"
log "下一步（自动）：前端收到 runtime.dev_swap_pending 事件后 invoke finish_dev_swap；"
log "手动等价：Debug 菜单 → 完成热切换（Finish Dev Swap）。"
