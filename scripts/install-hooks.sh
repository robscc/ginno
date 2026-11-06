#!/usr/bin/env bash
# 一键安装 Git pre-commit 门禁（clone 后跑一次）：
#   ./scripts/install-hooks.sh
# 门禁内容见 scripts/pre-commit.sh（fmt/ruff/lint 按需分发）。
# 应急跳过单次提交：git commit --no-verify
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
mkdir -p .git/hooks
cat > .git/hooks/pre-commit <<'EOF'
#!/usr/bin/env bash
exec "$(git rev-parse --show-toplevel)/scripts/pre-commit.sh"
EOF
chmod +x .git/hooks/pre-commit scripts/pre-commit.sh
echo "✓ pre-commit 已安装（.git/hooks/pre-commit → scripts/pre-commit.sh）"
echo "  跳过单次：git commit --no-verify"
