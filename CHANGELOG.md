# Changelog

All notable changes to Ginno are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[SemVer](https://semver.org/) — pre-1.0, expect breaking changes in minor
versions.

## [Unreleased]

（暂无）

## [0.1.3] - 2026-11-06

- **dev-swap 热更新全链路可视化**：六阶段进度（侧栏品牌行徽标 + hover 六阶段卡 + session 内倒计时/P90 慢告警）、切换确认门（右下角对话框，暂不后 ⏳ 待切换可补完成）、构建取消（左上按钮二击）、`POST /api/devswap/start|cancel`、WS 重连快照兜底
- **issue 清仓（#4-#12/#13-#22，社区审计 by vector4wang）**：
  - 修复：turn 变量遮蔽 UnboundLocalError、prompt hook 改写未入最终消息、workflow resume/continue 预算跨段累计、Code Panel 手动刷新确认、mod-runner 数组 props 丢弃、prefill 事件 sessionId、Extract 预算计数
  - 加固：broker 帧身份以连接为权威 + middleware 改写后最终参数复检；checkpoint delta 同 ID 改写丢失（降级 full）、channel 绕锁（共享 _turn_lock）、多实例 flock+fsync；Pin history 时序合并；manifest 补 nativeMessaging
  - 设计：外部 Agent 只读边界（docs/external-agent-readonly-design.md）；broker store 路径单一来源（Python 计算，Store 单锁快照，PR #23）
- **产品功能**：session 手动标签/图标（8 icon 枚举 + ≤3 label + # 前缀过滤）、侧栏 ask_user 等待图标、修正型注入合并、@result 全库候选、热更新侧栏按钮、AgentTeam Beta 标、usage 压缩次数列
- **修复**：rtk GUI PATH 兜底、diff 行号重复/版本串可读化、toolLabels 旧种子迁移误伤、压缩观测日志、zsh EQUALS 诊断、worker_bus digest 生命周期可观测、bubble_turn_mismatch 洪水
- **工程**：pre-commit 门禁（fmt/ruff/lint 按类型分发）、CI mod-runner job / cargo test --lib / ruff correctness 门禁、architecture.md 修正

## [0.1.2] - 2026-11-05

### Added

- **Turn 级耗时 + token 显示**（对话每个 turn 右下角）：运行中每秒跳动
  （token 随每次 LLM 调用累加实时更新），turn 结束由 message.end 的权威值
  定格，reload 后从 session 的 turns.jsonl 投影恢复（按 bubble 内最后一条
  AIMessage id 精确对齐；老会话静默降级不显示）。
- **工具输出分色**：bash 类输出的 `[exit N]` 行 0=绿/非 0（含 timeout）=红，
  stdout 默认色、stderr 琥珀色文字（不分色块）；标题行 ✓/✗ 跟随退出状态。
- **Runtime 热切换（开发自闭环）**：`make dev-swap`（或会话内 `/dev-swap`）一条命令
  完成「单测 → PyInstaller 重建 → blue-green 预检 → symlink 原子切换 → 壳热重启
  → 构建指纹验证 → 失败自动回滚」——改 runtime 代码不再需要退出重开应用
  （`docs/runtime-hot-swap-design.md`，P0+P1）。TopBar 琥珀 `dev·{指纹}` 黄条标识
  开发 bundle；`make dev-swap-clean` 一键回出厂。
- `/api/health` 现返回 `build`（git 指纹 + channel + repo），壳据此验证热切换。
- 壳新增 sidecar 崩溃自动拉起（watcher：曾在线后掉线且无 restart 在飞 → respawn）。

### Fixed

- goal continuation 两个 flaky：`status` 与 `turns_used` 记账存在写序窗口，
  慢机（CI）上断言读到中间态——等待条件改为两者齐备。

## [0.1.1] - 2026-11-03

### Added

- Full-stack CI: web (lint + typecheck + vitest + build), Rust (fmt + check),
  runtime (pytest with coverage → Codecov) — and green on all three.
- Release automation: `v*` tag → dmg built on a GitHub macOS runner (ad-hoc
  signed), attached to the GitHub Release.
- Frontend test suite bootstrapped (vitest): token formatting, session
  visibility, classname merging, chat waiting-state.
- Community files: CONTRIBUTING, SECURITY policy, PR template, ROADMAP;
  user-facing FAQ / troubleshooting / upgrade / comparison docs.

### Fixed

- Permission denial no longer forks a concurrent agent lane (LangGraph static
  edge + `Command.goto` double-execution; endless-spinner symptom).
- Usage stats expose provider display names instead of raw hash ids.
- Test-suite collection broken since 8d6cb13 (conftest name shadowing) —
  main CI is green again for the first time since then.
- `fmtTokens` doc/impl mismatch (1.25M rendered as 1.3M).

### Changed

- `cargo fmt` baseline for desktop shell + mod-broker (no behavior change).

## [Unreleased]

[0.1.1]: https://github.com/robscc/ginno/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/robscc/ginno/releases/tag/v0.1.0

## [0.1.0] - 2026-10-08

First public release 🎉 (~2.5 months, 220+ commits, daily dogfooding).

### Added

- **Multi-agent chat** — switch agents mid-conversation, shared history,
  per-agent memory.
- **Tools · permissions · skills · MCP · hooks** — Claude Code's shape, built in.
- **Visual Workflow Studio** — versioned DSL compiled to a LangGraph state
  graph, with a live run observer.
- **Local knowledge base** — index your Obsidian vault; answers with citations.
- **Long-horizon goals + TODO sync**; scheduled tasks (30s tick scheduler).
- **Embedded browser + web search** — retrieval with source attribution.
- **Local-first storage** — no DB / no account / no cloud; all state is
  JSON / JSONL / Markdown under `~/.ginno/`.
- **Channels**: DingTalk connector (Stream mode), WeCom.
- Platforms: macOS (Apple Silicon).

[Unreleased]: https://github.com/robscc/ginno/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/robscc/ginno/releases/tag/v0.1.0
