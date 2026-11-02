<!-- 提交前：标题用 Conventional Commits（feat/fix/...，scope 可选，如 fix(runtime,web): ...） -->
<!-- 安全问题不要走 PR/公开 issue —— 见 SECURITY.md -->

## What & why

<!-- 改了什么，为什么改。有关联 issue 请链接（Closes #123）。 -->

## Change type

- [ ] feat（新功能）
- [ ] fix（缺陷修复）
- [ ] docs（文档）
- [ ] refactor / chore（不改行为）
- [ ] test（补测试）

## Affected areas

- [ ] runtime (Python, `packages/runtime`)
- [ ] web (Next.js, `apps/web`)
- [ ] desktop (Rust, `apps/desktop`)
- [ ] mods / extension / docs / CI

## How tested

<!-- 勾选你实际跑过的验证；CI 会复核 -->

- [ ] `cd packages/runtime && uv run --group test pytest -q`（或带 `-m unit/api/e2e` 圈定范围）
- [ ] `cd apps/web && pnpm lint && pnpm typecheck && pnpm test`
- [ ] `cargo fmt --check`（Rust 改动）
- [ ] `make smoke`（涉及装配/会话/工作流主链路时）
- [ ] `make check`（**改了任何 UI 文案 / i18n catalog 必勾**）

## Notes for reviewers

<!-- 风险点、取舍、需要重点看的位置。没有就删掉这一节。 -->
