#!/usr/bin/env bash
# Run the Ginno dev stack: Python runtime + Next.js web + Tauri shell.
#
# Usage:
#   ./scripts/dev.sh          # all three
#   ./scripts/dev.sh web      # just web
#   ./scripts/dev.sh runtime  # just runtime
#   ./scripts/dev.sh desktop  # just tauri (assumes the other two are up)

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

target="${1:-all}"

run_runtime() {
  echo "[dev] starting python runtime on :8787"
  cd "$ROOT/packages/runtime"
  if command -v uv >/dev/null 2>&1; then
    uv run uvicorn ginno_runtime.server:app --reload --port 8787 --host 127.0.0.1
  else
    python -m uvicorn ginno_runtime.server:app --reload --port 8787 --host 127.0.0.1
  fi
}

run_web() {
  echo "[dev] starting next.js on :3000"
  cd "$ROOT"
  pnpm --filter @ginno/web dev
}

run_desktop() {
  echo "[dev] starting tauri (will pick up :3000)"
  cd "$ROOT"
  pnpm --filter @ginno/desktop tauri dev
}

case "$target" in
  runtime) run_runtime ;;
  web)     run_web ;;
  desktop) run_desktop ;;
  all)
    # Run all three in parallel. SIGINT kills everything.
    trap 'kill 0' INT
    run_runtime &
    run_web &
    run_desktop &
    wait
    ;;
  *)
    echo "unknown target: $target" >&2
    exit 1
    ;;
esac
