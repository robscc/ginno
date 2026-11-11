# Changelog

All notable changes to Ginno are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[SemVer](https://semver.org/) — pre-1.0, expect breaking changes in minor
versions.

## [Unreleased]

（暂无）

## [0.2.1] - 2026-11-11

40 commits since 0.2.0。主题：Instincts 本能学习、会话内搜索、通知治理、
遗言确认续跑、热更新取消与自愈、drawio 多页预览。

### Instincts（本能）— 行为规则沉淀
- **P0 最小闭环**：L1 确定性捕获 + YAML 存储 + 模板规则生成 + 注入管道 +
  管理页（左下角 BETA 入口）
- **P1-lite**：retry_recovered 捕获 + 置信度衰减治理
- **L2 纠偏检测**：启发式预筛 + observer 小模型判定（detached 任务化）
- 修复：i18n 目录未注册致页面显示原始 key；收尾钩子线程化修 CI 落盘竞态

### 会话内搜索与提及
- **Cmd+F find bar**：数据层索引 + Highlight API 高亮，跨消息定位
- mention P2：拼音首字母 + fuzzy 匹配排序；chip 图标化（气泡+输入框）
- mention 投递 e2e 轮询补 messages=None 中间态防护

### 通知治理（OS 通知不再刷屏/误报）
- 跨会话 reply 节流：KindRateGate 60s 冷却 + 合并计数（+N）
- worker 假完成修正：linked.done 附 worker_status，working/waiting 不再
  显示「已完成」
- ask_user 挂起通知去重集落 sessionStorage——重载不再重复弹
- workflow/subagent 两类 OS 通知默认 OFF（应用内已有 notice/digest）
- **桌面壳 CPU 修复**：通知等待线程无上限轮询 getDeliveredNotifications
  致 ginno-desktop 58%/usernoted 84%——限 4 路 + fire-and-forget

### 遗言模式（重启恢复）产品化
- auto 模式人工会话改「待确认续跑」：注入带 + runtime.lastwords_pending
  广播 + 收件箱点击直达 + POST /api/lastwords/resume（TTL 1h，exactly-once）
- subagent 会话保持无感直跑；低工作量丢弃日志 debug→info

### 热更新（dev-swap）
- **取消功能补全**：pidfile + TERM 优雅取消 trap + 切换点安全门 +
  命令路径可达（企微/桌面同口径）
- public 资产完整性自检+自愈（fileicons 丢失自动恢复）
- 进度计时修复：elapsed 锚 stageStartedAt，不再每 2-5s 归零
- web 内容戳纳入 public/；补拷防线升级文件数比对

### 文件预览与产物
- **drawio 多页预览**：页签切换（分段药丸控件、横向滚动+N/M 计数、
  ←/→ 键盘翻页、主题自适应配色）
- 文件预览 Esc 关闭；Esc 统一 hook 补齐 7 个浮层（向导类带防误触 guard）
- 产物面板「在 Finder 中显示」（open -R / explorer /select,）
- 附件图标全类型化：vscode-icons 彩色图标替换 emoji 三态

### 安全与权限
- **保护区 own_ws 修复**：挂 primary 挂载后会话自己的 uploads/memory
  目录不再被误拒（_DenyPolicy 独立豁免参）
- G3 渠道命令权限闸：admin 级命令按 userid 判定 fail-closed
- bash 工具环境变量注入：settings.json "env" 键 + 设置页可视化编辑
  （PATH 追加语义，代理场景开箱即用）

### 其他
- 桌面壳开机自启动（SMAppService 注册态为唯一真源）
- 内置 AgentTeam 团队模板（含内阁模式）
- 断线补发窗口 6s→60s（冷启动/休眠恢复）
- 连接器安装向导 i18n 补齐（wecom/vibe/dingtalk 双语）
- 品牌行鸭子 logo 恢复 + 侧栏加宽 w-72
- MCP app 初始数据推送 content 形态规整（trending 空表修复）

## [0.2.0] - 2026-11-09

63 commits since 0.1.3。主题：MCP Apps 端到端打通、通知统一、遗言模式、
企微媒体、切换会话性能治理、rtk 深度集成。

### MCP Apps（聊天内可交互应用卡）
- **P0 全链路**：`_meta.ui.resourceUri` 透传 + `ui://` 资源读取 + 沙箱 iframe 卡
  （McpAppCard/postMessage 桥/tools-with-ui 接线）；wire 协议对齐官方 ext-apps
  （`ui/initialize` 应答含必填 `hostContext`；推送 `ui/notifications/tool-result`）
- 修复：MCP 工具参数类型端到端保真（number/integer/boolean 不再拍平成 string）；
  probe 试连 cancel scope 跨 task 崩溃；向导错误可复制；tools-with-ui 空清单
  15s 新鲜期 + lookup miss 自愈刷新（app 卡不再延迟/缺失）

### 通知统一（右下角唯一出口）
- notice 总线：右下角容器 + 未读/已读通道（mode/silent/countInBadge 防双算）+
  收件箱面板（RightDock 新 tab）+ TopBar 铃铛 badge；ModToastHost 等私有
  toast 全部退役合流；session 未读投影进收件箱

### 遗言模式（重启恢复）
- 被 dev-swap/重启杀掉的 turn 自动恢复：WAL 持续写 + SIGTERM 兜底 + 启动扫描
  （exactly-once/TTL：signal 1h·running 15min/恢复价值门槛）；人工会话
  notify 注入（前端折叠卡 + 通知中心条目 + 设置三选一），subagent 自动续跑
  （via:lastwords）；30 用例质量矩阵

### 企微媒体
- 被动回复 send_image/send_file（协议矩阵查证：image≤10MB 仅 png/jpg/gif、
  file≤20MB、media_id 3 天）；产物上行三段式（大小预检/降级回执/矩阵测试）；
  **正文媒体引用即发**——markdown/attach_ref 中发现的图片文件触发发送，
  push_only 场景降级可达提示

### 切换会话性能治理（988ms → 0.5ms）
- checkpointer 热路径**尾窗读**（尾部窗口 fold，缺锚翻倍回退全量）+
  物化缓存（stat 失效）——get_tuple/put/get_pending_interrupt 三热路径
  从 O(日志) 降为 O(窗口)；前端**转录窗口化**（首屏 ≤80 条，⌘K 锚点自动
  扩窗）；**存量切分器**（`ginno checkpoints segment`：主文件自包含 +
  只读归档段 + 首切备份 + fold 等价自检回滚）

### rtk 深度集成
- bash 超预算中间段流式落盘 + read_file 分页恢复；rtk 复合段感知重写 +
  git -C 形态 + 1.1.0 白名单；同目录探测（嵌入式/评测分发 ginno 与 rtk
  同卷不进 PATH）

### 修复
- ContextRing 混用守卫 + 压缩阈值与圆环同口径（会话级模型解析统一链；
  sideband record 不入分子，13%↔60% 抖动根治）
- 断线窗口发送不再秒标未送达（先等重连 6s 再补发，红叹号兜底）
- 定时任务实例 artifact 预览点不开（SheetViewer 移出 hidden 工作区行）
- 429 限流报错清洗（去重置时间重复/英文括注，附结构化 reset_at）
- scheduler UTC 午夜时间炸弹（reconcile 扫窗动态展开，CI 三用例红根因）
- dev-swap 三连：quiesce 不计后台任务（自锁）、进度锚点+时间插值、
  mktemp 模板 X 末尾
- ask_user 多问题卡选项后自由输入解锁 + /compact 假「未送达」
- 任务事件历史重放折叠（与 live 折卡对齐）；blocked/deny 改 ToolMessage
  配对响应（拦截信息独立渲染）；后台任务 hover 显示完整脚本

### 特性与工程
- 特权会话：运行时数据目录只读挂载（Ginno 承接 Ginno）+ workspace-in-
  mount-roots 打穿 home-deny 历史洞封堵（哨兵断言）
- worker_bus assign_worker 异步化 + 派完自动订阅结论
- BYOM（自带机器）设计文档；CLAUDE.md 增补特性分支 PR 自动化章节

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
