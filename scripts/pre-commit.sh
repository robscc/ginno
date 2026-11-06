#!/usr/bin/env bash
# Ginno pre-commit 门禁（scripts/install-hooks.sh 一键安装）。
#
# 只挡「提交前没跑必过检查」的低级红（CI 已连红四次 rust fmt + 一次
# ruff 重复 import）：按 staged 文件类型分发，未涉及的栈零开销——
# 全栈大改也应在 ~15s 内完成。重的测试留给 CI。
#
# 应急跳过（慎用）：git commit --no-verify
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

# hook 环境的 PATH 极简（node/pnpm/cargo 都可能不在）——补齐家 brew 与 cargo
export PATH="/opt/homebrew/bin:$HOME/.cargo/bin:$PATH"

staged() { git diff --cached --name-only --diff-filter=ACMR | grep -E "$1" || true; }

CARGO="${CARGO:-cargo}"
UV="${UV:-/opt/homebrew/bin/uv}"
PNPM="${PNPM:-pnpm}"

fail=0

# ── Rust：cargo fmt --check（desktop 与 mod-broker 两个 manifest）──
if staged '\.(rs|toml)$' | grep -qE 'apps/desktop|crates/'; then
  for M in apps/desktop/Cargo.toml crates/mod-broker/Cargo.toml; do
    echo "› cargo fmt --check ($M)"
    if ! "$CARGO" fmt --check --manifest-path "$M" 2>&1 | sed 's/^/  /'; then
      echo "✗ rust fmt 未过（$M）——跑 cargo fmt 后重试，或 --no-verify 应急" >&2
      fail=1
    fi
  done
fi

# ── Python：ruff（lint + import 顺序，packages/runtime）──
if staged '\.py$' | grep -q 'packages/runtime'; then
  echo "› ruff check (packages/runtime)"
  if ! "$UV" run --directory packages/runtime --locked ruff check src tests 2>&1 | sed 's/^/  /'; then
    echo "✗ ruff 未过——跑 uv run --directory packages/runtime ruff check --fix 后重试" >&2
    fail=1
  fi
fi

# ── 前端：next lint（仅 apps/web 有 staged 时）──
if staged '\.(ts|tsx|js|jsx)$' | grep -q 'apps/web'; then
  echo "› next lint (apps/web)"
  if ! (cd apps/web && "$PNPM" lint 2>&1 | sed 's/^/  /'); then
    echo "✗ 前端 lint 未过——cd apps/web && pnpm lint 修后重试" >&2
    fail=1
  fi
fi

exit "$fail"
