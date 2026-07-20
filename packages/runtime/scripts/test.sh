#!/usr/bin/env bash
# Run the Ginno runtime test suite (unit + api + e2e) via uv.
#
# Usage:
#   ./scripts/test.sh                 # all tests
#   ./scripts/test.sh -m unit         # only unit tests
#   ./scripts/test.sh -m api          # only API integration tests
#   ./scripts/test.sh -m e2e          # only WebSocket end-to-end tests
#   ./scripts/test.sh -k permission   # passthrough any pytest args
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if command -v uv >/dev/null 2>&1; then
  uv run --group test pytest "$@"
else
  echo "uv not found; install from https://docs.astral.sh/uv/" >&2
  exit 1
fi
