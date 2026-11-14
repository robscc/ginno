# AgentTeam 剩余可做项 Backlog（三路调研合并·终版）

> ## ✅ 完成状态（2026-11-15 更新）
>
> **全部 56 项已交付**：P0 7/7 ｜ P1 21/21 ｜ P2 28/28。
> 分支 `worktree-agent-team-next`，24 个提交，**未合并 main**。
>
> | 交付形态 | 项 |
> |---|---|
> | 代码修复 + 回归测试 | P0 全部、P1 全部、P2 除下列两项外全部 |
> | 决策文档（backlog 本就定性为评估/暂缓） | P2-6（TEAM.md 维持与 KB 隔离）、P2-7（goals 暂缓，阻塞已解除）→ `docs/agent-team-p2-6-p2-7-decisions.md` |
>
> **验证深度（如实标注）**：runtime 全量套件 3146+ passed exit 0；
> 前端 tsc --noEmit 零错误 / vitest 191 passed / next build exit 0（53/53 页）；
> ruff 全过。**新增的 packaged e2e（`test_packaged_ui_team.py`）已写+可收集但未执行**
> ——本地 dist 未构建且 Ginno 正在运行（按 CLAUDE.md 的 zlib 故障模式此时不能重建
> bundle），由 CI 的 ui-e2e job 真跑。真机双窗口交互未验。
>
> **修复过程中额外挖出 2 个不在原清单的真 bug**：
> ① checkpoint 并发写丢失（用户消息落盘放在投递之后，被成员回帖覆盖；
> 连跑 4 次复现 1 次，轮询 10s 仍为 0 条 → 证明是真丢失不是延迟）；
> ② `member_remove_session` 空 ids 分支注释谎称「已写盘」但实际没调 `_write`。
>
> **口径更正**：原文「本机无 node」是错的——node v25.8.2 + pnpm 9.15.1 实际可用。
> 改用真实编译后 tsc 抓出 4 个静态自查看不出的错（narrowing 不进闭包、
> 重复导入、非法枚举值 ×2）。下方原始条目保留调研时口径，不改写。

---

> 日期：2026-10-14
> 基线：main `fdedcd2`（设计文档 P0/P1/P2/P3/P3+ 账面全部上线）
> 工作树：`.claude/worktrees/agent-team-next`（分支 `worktree-agent-team-next`）
> 方法：3 路只读调研（runtime / 前端UX / 质量性能，共 46 项原始发现）→ 去重合并 → **本人对全部 P0 逐条代码核验**
> 口径：静态代码分析，未运行构建/测试（本机无 node）。标注「推断」处为代码路径可推出但未真机复现。

---

## 0. 结论先行

分期账面全部落地，存储层并发纪律（RLock + 原子落盘）与 blocked 原子拒绝也确实扎实。
但三路调研合并后发现 **7 个 P0 级断裂**，其中 **3 个是「测试绿、生产死」**——
单测用 monkeypatch 或把缺陷写成预期断言，CI 全绿也发现不了：

| # | 断裂 | 后果 | 核验 |
|---|---|---|---|
| **P0-1** | 前端不消费 `team.delivered` + 用户消息不落 checkpoint | 广场发送后 composer **永久锁死**；重载误判「未送达」→ 重试致**重复投递** | ✅ 6 环全确证 |
| **P0-2** | **Lead 工具绑错会话** | **P3「Lead 自主分派」整条能力空转**——工具绑在永不思考的会话上 | ✅ 3 环全确证 |
| **P0-3** | team steer 条目无 `origin` → 段末清场丢弃 | 回执说「已投递」，消息**静默丢失** | ✅ 全确证 |
| **P0-4** | `unknown`/`error` 语义态是**死代码** | 头号纪律「重启后标 unknown，绝不假装 idle」**未生效** | ✅ 确证 |
| **P0-5** | `recover_teams` 删「坏 roster」团队全部会话 | 启动即**不可逆数据丢失** | ✅ 确证 |
| **P0-6** | 模板/一句话建队**三处断裂** | first_task 必失败 + 领队重复任命报错 + 成员职责说明全丢 | ✅ 确证 |
| **P0-7** | blocked→resume 回帖断链 | 撞权限卡时回半截话；用户代答后的**真正结果永不回主会话** | ✅ 确证 |

**三路独立收敛**：P0-1 被前端路与质量路各自从不同入口发现；P0-4/P0-6 被质量路与 runtime 路各自发现。
**P0-2/P0-3 只有 runtime 路发现**——前两份都漏了，说明单维度调研不够。

---

## 1. P0 — 必须先修

### P0-1 广场发送生命周期悬空 + 用户消息不持久化

**6 环链路（逐环核验）**：

1. `api/stream/ws.py:566-588`：@ 投递分支 push `team.delivered` 后 **`continue`**——无 `message.end`、无 HumanMessage 落盘。无 @ 路由分支（`ws.py:590-640`）同样。
2. `engine/handle.ts`：只有 `case "team.changed"`(1134)、`case "team.reply"`(1715)，**全 web src grep `team.delivered` 零命中**。
3. `ChatStream.tsx:560-563`：`attemptSend` 置 `busyBySessionRef[sid]=true` + 建 live 气泡。
4. `ChatStream.tsx:348-349`：`running = liveId !== null || …` → **恒真**。
5. `useComposerSend.ts:211-229`：busy 时后续发送全走 `enqueueSteer` → **死队列**（等不到 message.end）。
6. `engine/connect.ts:176-183`：重连对账找不到该用户气泡 → 标 `failed/connLost`（红❗可重试）→ 点重试 = **重复投递**，正是设计 §5.3.3 明文要防的事故。

**测试固化了缺陷**：`tests/api/test_team_p0.py:262-289` 把「无 message.end」写成预期断言。

**修复**：runtime 落带标记的 HumanMessage 到主会话 checkpoint（**不起 turn、不占 LLM context**）；
前端增 `case "team.delivered"` → markDelivered + 清 live 气泡 + 按 turn_id 对齐；对账不得误标 failed。
**量 M**｜与 P1-1 同源，建议合并开发｜UI 表现为推断，落地前先写 packaged 复现用例。

---

### P0-2 Lead 工具绑错会话 → P3 整条能力空转 ⭐ 只有 runtime 路发现

**3 环链路（逐环核验）**：

1. `tools/team.py:85-99`：`build_team_tools` **仅当 meta `type=="team"`** 才绑定，否则返回 `[]`。
2. `api/teams.py:107`：领队实例会话建为 **`type="team-member"`** → 拿到空工具表。
   且 `tools/team.py:100-107` 的 `_team()` helper **显式拒绝成员命中**（`located[1] is not None` → `("", None)`，注释「成员命中也算不合法——一切经主会话」）。
3. `ws.py:636`：主会话（`type="team"`）**永不自跑 turn**——所有文本消息都路由给成员后 `continue`。

→ **工具绑在一个永不思考的会话上；真正思考的领队实例没有工具。**
`list_members` / `deliver_to_member` / `assign_task` 三个 Lead 工具**实际不可达**，
P3「Lead 自主分派」整条能力空转。领队也无替代手段：worker_bus 不接 team-member，成员间 @ 是旧 persona 语义。

**讽刺**：`test_team_p0.py:419-434` 还**断言了成员会话拿空工具表**——把缺陷钉成了预期。

**修复（S）**：`build_team_tools` 增「该 session 是某团队 `role==leader` 的成员实例」分支，
`_team()` 放行 leader 成员命中；补绑定测试。
**待产品确认**：绑到**领队实例会话**（符合实体领队模型，改动小、与现有路由一致，**推荐**），
还是让广场主会话恢复 Lead turn？

---

### P0-3 team steer 投递静默丢弃 ⭐ 只有 runtime 路发现

**链路（核验）**：
- `server_shared.py:415-426` `steer_keep_channel`：段末清场**只保留 `origin in ("channel","task","worker")`**。
- `teams/deliver.py:371-381`：team steer 条目字段为 `steer_id/turn_id/text/injected_at/extra_kwargs`——**无 `origin`**。
- → 未被吸收的 team steer 条目在 turn 段末被丢弃，而回执已报 `delivered`。

**关键洞察**：仓库**已为同类问题专门修过三次**（channel/task/worker），
docstring 明写「『已追加』的回执不能是空头支票」「eventual delivery，不静默丢」。
**team 只是没加进白名单**——这是现成范式的遗漏，不是新设计。

**修复（S）**：team steer 条目加 `origin:"team"` + 白名单加 `"team"`；
仿 `reconcile_stash_injections` 把未吸收条目补投为新 turn（补投须过并发闸与 blocked 检查，复用 `deliver()` 全路径）。

---

### P0-4 `unknown` / `error` 语义态是死代码

`teams/status.py:96-105` 读 `_SESSIONS[sid]["stop_reason"]` 判 error/unknown。
但**全 runtime 写该字段的只有 delegation 相关代码**（`delegation_sessions.py:205/285/396`、`tools/external_agent*`），
**chat / team-member 会话从无写入方**；`api/sessions.py:1900-1921` 的 `_ensure_session` 也不加载它。

→ 恒为 `""`，两分支永不触发。重启后正在干活的成员显示 idle/done，
**设计 §3.3 头号纪律「宁可显示我不确定，不要显示错误的它空闲」实际未生效**。
`_run_member_turn` 异常路径也置 done 而非 error；`recover_teams` 不做状态刷新/unknown 标记。

单测 `test_team_store_status.py:186-194` 直接 monkeypatch `_SESSIONS` 造数据源 → **假信心**。

**修复（M，需定数据源）**：(a) turn 终态写 `stop_reason` 进 meta 并让 `_ensure_session` 恢复；
或 (b) 改判据读现成的 `meta.last_finished`/`last_error`（`engine.py:428` 已在写）。
`recover_teams` 启动时对「持久化 status=working 但无活 turn」标 unknown。
**验证**：真实重启模拟（清 `_SESSIONS`、meta 留中断 turn），**不许 monkeypatch 造数据源**。

---

### P0-5 `recover_teams` 启动即删「坏 roster」团队的全部会话

`api/teams.py:131-186`：`referenced` 集合**只来自 `list_teams()`**；
`teams/store.py:58-78` 的 `list_teams` 对坏文件 **`except (JSONDecodeError, OSError): continue`** 静默跳过。

→ roster JSON 一损坏，该团队进不了 `referenced`，其 team/team-member 会话启动时被
`delete_session(..., force=True)` 当孤儿删除（`server.py:163-164`）。**不可逆**。

讽刺：「坏文件按缺失处理」正是存储模块自己声明要兜底的场景，却被上层清理当成「不存在」。

**修复（S）**：清理前对「读不出的 roster」跳过而非判孤儿（`list_teams` 增 `include_unreadable`，
或遇坏文件即放弃本轮清理）。**验证**：写坏 roster → 跑 `recover_teams` → 断言会话仍在。

---

### P0-6 模板 / 一句话建队三处断裂

`api/team_generate.py` 的 `from_spec`（`from_template` 与 Prompt→Team 共用，**新手主入口**）：

1. **first_task 必失败**：`team_generate.py:214-218` 用 `DeliverRequest(text=spec["first_task"])`
   **不带 instance_id/agent_id** → `api/teams.py:606-609` 必返回 `no_target` failed。
   内阁等模板开场任务静默丢弃，`first_task_delivered` 恒 False。
2. **领队重复任命**：建队已 `appoint_leader`（`api/teams.py:256-261`），循环再 `add_member(role=leader)`
   被 `api/teams.py:355-362` 拒绝 → 每次模板建队 `member_errors` 必含领队；
   且领队显示名变成主会话标题「👥 队名」而非 spec 名。
3. **成员 instructions 全丢**：`AddMemberRequest`（`api/teams.py:44-47`）**无 instructions 字段**，
   也不写 notes.md。生产方有（`team_generate.py:109`、`team_templates.py:239`）、**消费方无**。
   内阁模板的「反糊弄三件套」回报契约到不了成员。

现有 `test_team_templates.py` **只测 spec 解析，无建队断言** → 三处全漏。

**修复（M）**：first_task 投 leader 实例；建队跳过已由 appoint_leader 落地的 leader（或让其接受 display_name）；
instructions 写实例 notes.md（复用 `memory.set_member_notes`，上限 4000 字 vs instructions 限 1000 字，安全）。

---

### P0-7 blocked→resume 回帖断链

`teams/deliver.py:255-290`：`_run_member_turn` 把 `_run_managed_turn` **返回**当作 turn 结束，
立即 `post_reply` + 置 done。但 `subagent_scheduler/runner.py:88-93`：**parked（interrupt）turn 的返回只是挂起**，
真正 settle 在 resume 路径。

而 `api/stream/` 全目录的 team 引用**只有两处**：`turn.py:650`（记忆注入）与 `ws.py`（投递分支）；
`resume.py:141-144` 只调 subagent 的 `on_turn_settled`，**无 team 钩子**。

→ 成员撞权限/ask_user 卡（团队场景高频）时：主会话收到**卡住前的半截话**并被置 done；
用户代答后跑出的**真正结果永远不回主会话**，done 未读也不再触发。

**修复（M）**：`_run_member_turn` 在 `_PENDING_RESUME` 命中时跳过回帖；
在 engine 显式终态分支 / resume 完成点加 team 回帖钩子。**与 P1-2 共用同一钩子实现**。

---

## 2. P1 — 生命周期 / 竞态 / 可靠性

> **共同根因：turn 生命周期未接 team 钩子**（`api/stream/` 只有记忆注入 + 投递分支两处 team 引用）。
> P0-7 与下面 P1-2/3/4 都源于此——**补一次钩子能一起修好四项，建议一个 PR 统筹**。
> 钩子必须绝不带崩 turn（照 `memory.turn_context` 的兜异常范式），按 `team_of_session` 判型避免影响 subagent/普通会话。

| # | 项 | 证据 | 量 |
|---|---|---|---|
| P1-1 | **投递回执可视化**：sysline 回执行 + 原子拒绝卡 + stalled 警示（stalled **禁一键重发**，只给「进入工作台」） | 原型有实现无；runtime 回执已含 reason/channel/queued 但前端零渲染；i18n 无 receipt 键 | M |
| P1-2 | **状态回流缺钩子**：blocked——「设计里最有价值的一态」——用户不打开团队页就不落盘/不推 `team.changed`/不进 Dock；徽标零刷新直读持久化态 | refresh/broadcast 调用点仅 deliver 路径 + API 读端点 + list_members；`api/badge.py:25-40` | M |
| P1-3 | **排队永久搁浅 + 静默丢弃**：drain 只挂 deliver 自有路径；用户在工作台自起的 turn 结束不 drain（但回执已报 `delivered/channel=queue`）；drain 时成员已 blocked 的排队消息被丢弃且主会话无通知 | `deliver.py:79-104`（91-92 注释「无人可通知」）；drain 调用点仅 `291/468` | M |
| P1-4 | **steer 重复回帖 + 30 分钟超时静默丢失**：每条 steer 各 spawn 一个 waiter → N 次 steer = N 条重复回帖 + N 个 0.4s×1800s 轮询；超时仅 log | `deliver.py:388-395,453-461`；`DELIVER_WAIT_S=1800` | S |
| P1-5 | **并发双开 turn / 闸击穿**：`_is_live` 与闸检查在 spawn 之前，登记在 task 内 → 两个并发 deliver 可同时通过 | `deliver.py:431-441`；`runner.py:66` task 内才写 `_RUNNING_TURNS` | M |
| P1-6 | **回帖静默丢失**：重启后未打开主会话即经 HTTP/Lead 工具投递 → `_SESSIONS` 无主会话 → 回帖只留日志（成员会话有 `_ensure_session` 兜底，主会话没有） | `deliver.py:193-196` vs `424-428` | S |
| P1-7 | **seen/徽标漂移**：`refresh_team(client)` 把按该客户端算的 done/idle 落盘，badge 读盘 → pin 端 seen 后 main 端未读在 Dock 上消失 | `status.py:159-178`；`api/teams.py:194-217`；`badge.py:25-35` | M |
| P1-8 | **领队降级/升级不同步 `leader_instance_id`**：PATCH 把领队改回 member 后，路由仍把全部无 @ 消息投给已降级成员，且无法任命新领队 | `api/teams.py:411-435`；`route.py:33-40` 优先 leader_instance_id；`MemberPanel.tsx:92` | S |
| P1-9 | **@ 多人并行投递未实现**（§9.4 待决问题）：`@dev @writer 各自做 X` 只有 dev 收到，writer 静默忽略 | `resolver.py:323` 只取第一个 @agent；`ws.py:566-589` 单目标；`route.py:21-44` 数组契约早已预留；无 @ 广播分支已证明多目标循环可行 | M |
| P1-10 | **wait 死等**：等待中团队被解散/成员被移除 → 指定 ids 时 rows 恒空、循环无出口（默认 timeout=0）→ Lead turn 永久挂起；ids 部分缺失静默只等子集；until 无枚举校验 | `wait.py:75-92,29-30`；`tools/team.py` 默认 timeout_s=0 | S |
| P1-11 | **状态订阅 socket 无重连**：TeamShell 专用 WS 只设 onmessage，sidecar 重启后面板/总览永久 stale | `TeamShell.tsx:210-243`；`lib/api/chat.ts:15-22` 裸 WebSocket | S |
| P1-12 | **@ 菜单键盘绕过 blocked 禁选**：鼠标有守卫，↑↓/Enter/Tab 与 pickItem 均不检查 disabled；active 初始 0，blocked 排首位时一击即中 | `Composer.tsx:272-286`；`ChatStream.tsx:751-779`；`ComposerMenu.tsx:80-95` | S |
| P1-13 | **steerHint 永不触发**：判据 `input.includes("@agent:"+agent_id)`，实际 token 用 display_name 且输入框是显示形态 `🤖 名字` → 恒假（死代码） | `Composer.tsx:172-181`；`commandMenu.ts:288,300-315` | S |
| P1-14 | **成员级生命周期 UI 全无**：`patchTeamMember`/`removeTeamMember` 全仓库零调用，runtime 端点齐备 | `lib/api/sessions.ts:512-528`；`api/teams.py:410-476` | M |
| P1-15 | **记忆编辑器可清空文件**：加载失败 → `setLoaded(true)` 空文本 → save 直写 → sidecar 抖动时把 TEAM.md/notes.md 覆盖成空 | `TeamMemoryModal.tsx:28-56` | S |
| P1-16 | **`alert()` 违反仓库自身 Tauri 纪律**：ConfirmModal docstring 明言原生对话框在 webview 不可靠 | `TeamShell.tsx:195`；`ConfirmModal.tsx:4-6` | S |
| P1-17 | **侧栏 /team 导航项缺 needs_action 徽标**：三级上卷断在导航层（Dock 已实现，应用内没有） | `SidebarFooter.tsx:76-83` 只有 Beta 标；设计 §5.3.1 明文「导航项/Dock」 | S |
| P1-18 | **回帖/blocked 无 OS 通知**：`team.reply` case 无 notifyNative/pushNotice；NotifyKind 无 team 类 | `handle.ts:1715-1738`；`notifyPrefs.ts:38` | M |
| P1-19 | **窄窗口不可用**：minWidth 900 − 侧栏 288 − 团队左栏 208 − 成员面板 300 ≈ **104px** 对话列；无响应式断点 | `tauri.conf.json:17-20`；`AppShell.tsx:878`；`TeamShell.tsx:459`；`MemberPanel.tsx:107` | M |
| P1-20 | **广场 AgentChips 语义错位**：渲染全量 agent + 「Ask X」persona 文案，但广场语义是投递给成员；选非成员静默失败 | `AgentChips.tsx:30-46`；`ChatStream.tsx:853-869`；`deliver.py:486-497` | M |
| P1-21 | **团队链路零 packaged e2e**（违反 CLAUDE.md 2026-11-12 红线）：58 个 e2e 文件无 team 用例；`commandMenu.test.ts` 无 teamMembers 覆盖 | `tests/e2e/` glob；参照 `test_packaged_ui_mention.py` 范式 | M |

---

## 3. P2 — 跨子系统集成（新增维度，runtime 路独有）

> 这些是**能力扩展**而非缺陷修复。共同价值：让团队成为 Ginno 的一等公民，而不是只在 /team 页里能用。

| # | 项 | 证据 | 量 | 备注 |
|---|---|---|---|---|
| P2-1 | **schedule 驱动团队**：新增 `target=team`（投 leader 或指定成员），让「每天 9 点团队站会/巡检」成立 | `schedule_store.py:36` `TARGET_TYPES=("prompt","workflow")`；prompt 走影子会话 headless **绕过团队路由** | M | 依赖 P1-2 状态落盘 |
| P2-2 | **worker_bus 放开 team-member** ⭐ | `worker_bus/registry.py:26` `ASSIGNABLE_TYPES` 不含 team/team-member；`__init__.py` 明示「独立于 AgentTeam」 | M | **改变了 agent→agent 的成本等式**——见 §5 |
| P2-3 | **渠道消息投递团队**：钉钉/企微收到消息可路由给团队，回帖回渠道 | `channels/` grep team 零命中；`base.py:193-200` headless 直跑不经 ws 路由 | M–L | 注意 runner 已有的「channel 注入不外发」纪律 |
| P2-4 | **workflow 节点调用团队成员**：agent 节点可选「投给成员并等回帖」，把团队编进工作流 | `workflows/` grep team 仅 `transcript.py:37`（隐藏父类型，非集成） | M | 可复用 wait 原语收敛；依赖 P0-2/P1-2 |
| P2-5 | **团队成本可见**：成员 turn usage source 标 `"team"` + per-instance/团队聚合 API + 面板渲染 | `runner.py:52-53` usage_source 仅 subagent/chat 二分，team-member 记为 `"chat"`；`status.py:214-249` member_row 无 usage 字段 | M | 设计 §5.6 硬要求；`MemberPanel.tsx:5` 注释写了「累计 token」但无实现 |
| P2-6 | **TEAM.md ↔ 知识库关系评估** | `knowledge/` grep team 零命中；`memory.py:77-118` 走独立 `wrap_context_section`，不进 KB 索引 | S(评估)/M(实现) | 先决策：TEAM.md 是「约定」不是「知识」，**刻意隔离可能是对的** |
| P2-7 | **goals ↔ team** | `goals/` grep team 零命中 | — | **建议暂缓**：goal 驱动单会话持续目标，与团队「广场+成员」模型正交；若做形态是「goal 挂 leader 会话、由 Lead 分派」，依赖 P0-2 |

## 4. P2 — 可见性 / 一致性 / 打磨

| # | 项 | 证据 | 量 |
|---|---|---|---|
| P2-8 | **徽标过滤 archived 团队**：归档团队仍点亮 Dock，与前端行徽标行为不一致 | `api/badge.py:30-34`、`api/teams.py:196-204` 不过滤；`TeamShell.tsx:288` 前端不亮 | S |
| P2-9 | **并发闸 settings 化**：设计明文「settings 可调」，实际硬编码 | `store.py:36`；照 `subagent_scheduler/meta.py:13-17` 的 settings+env 范式 | S |
| P2-10 | **working 展示文本落地**：`last_tool` **只读不写** → working 永远显示「正在运行」 | `status.py:123-131` 读；全仓 grep 无写入点 | S |
| P2-11 | **「投递≠批准」提示词 + 投递溯源信封** | 全 runtime grep 相关文案零命中（§5.7 要求未实现）；`deliver.py:431` 新 turn 路径 `extra_kwargs=None` 无溯源键（steer 路径有） | S |
| P2-12 | **成员工作台顶栏**补三层记忆 chip + ctx/tok | 现仅 instance_id/model/notes；原型 mbar 有三层 chip | S |
| P2-13 | **投递 brief 结构化卡**（目标/共享 workspace/团队上下文/完成后） | `deliver.py:255-289` 原文透传；原型 .brief 卡 | M |
| P2-14 | **TeamReplyCard** 补状态/耗时 meta；修 `span role=button` 嵌套在 `button` 内（HTML 语义非法） | `TeamReplyCard.tsx:56-110` | S |
| P2-15 | **未读汇总条语义**：awayMin 取自 last_active（成员在跑时恒≈1，误导）；收起不持久；缺持久性脚注 | `TeamShell.tsx:639-652,79` | S |
| P2-16 | **elapsed 不走字**：1s tick 只触发重渲染，值来自快照 → blocked 等待时长冻结 | `MemberPanel.tsx:98-104,265` | S |
| P2-17 | **键盘可达性**：团队行 div onClick 无 role/tabIndex；行内菜单 opacity-0 仅 hover 可见；模态无 Esc | `TeamShell.tsx:308-341,338` | M |
| P2-18 | **错误处理扫尾**：rename/patch/删会话无 catch；refresh 静默吞错无提示；/team 无离线横幅 | `TeamShell.tsx:129-145,172-185`；`MemberPanel.tsx:318-330` | S |
| P2-19 | **教育性文案**：上卷规则说明条 + @ 菜单团队脚注（「@=投递不是换 persona」） | 原型 .rollup/.mf 有，实现无 | S |
| P2-20 | **wait(until=["done"]) 永不收敛**：用户 seen 后 done→idle；client 硬编码 main | `wait.py:56-71,26` | S |
| P2-21 | **多会话成员回帖读错 session**：turn 起始捕获 sid，post_reply 重读 `member.session_id`；切换活跃会话无「旧会话在跑」守卫（**竞态未验证**） | `deliver.py:255-264` vs `162-190`；`api/teams.py:419-447` | S/M |
| P2-22 | **性能随规模恶化**：`team_of_session` 每条消息全盘扫团队文件；member_row 每成员读 notes；面板 1s 全量重渲染；badge 5s 轮询；记忆注入 TEAM.md 8000 + notes 4000 字符/成员/turn | `store.py:348-366`；`ws.py:600`；`memory.py:29-30,77-110`；`badge.rs:16` | M |
| P2-23 | **记忆注入缺 turn 级 e2e**：只测 `turn_context` 函数，未断言成员真实 turn 收到 TEAM.md | `test_team_p3.py:70-88`；`turn.py:660` 接线未测 | S |
| P2-24 | **pin 浮窗联动**（待核实）：PinStream 无 team 事件处理；follow 是否广播 /team 的 active session 未验证 | `components/pin/` grep team 零命中；`useSessions.ts:44-56` | M |
| P2-25 | **「进入工作台」事件在 /team 之外丢失**：纯 window 事件仅 TeamShell 监听 | `TeamReplyCard.tsx:24-30`；`TeamShell.tsx:258-268` | S |
| P2-26 | **编制写入边角**：leader 唯一性用起始快照检查（竞态）；patch 允许任意 leader_instance_id 无校验；多会话端点无 API 测试；`member_remove_session` 空 ids 分支注释称「已写盘」但实际未 `_write` | `api/teams.py:336-417,475-533`；`store.py:292-316` | S |
| P2-27 | **recover_teams 补全**：孤儿扫描 slug 集=现存团队 slug∪{default}，漏「团队已全删的 slug」 | `api/teams.py:172` | S |

### P2-28 文档与实现不一致（7 条，逐条决定「改文档」还是「补实现」）

| 文档处 | 文档说 | 代码实际 |
|---|---|---|
| `docs:328` | 上卷 blocked>unknown>working>done>waiting>idle | `status.py:41` 含 error，且 **waiting 在 done 前** |
| `docs:354` | wait 默认安定集 idle\|done\|blocked | `wait.py:26` 还含 unknown/error |
| `docs:402` | 并发上限「settings 可调」 | `store.py:36` 硬常量 |
| `docs:404` | 每实例 token 常显 | member_row 无 usage 字段 |
| `docs:424` | 未读汇总「重连时服务端推」 | `TeamShell.tsx:633-693` 前端本地横幅 |
| `docs:320` | 状态「turn 生命周期钩子触发、不新增轮询」 | 无钩子 + badge 5s 轮询 |
| `docs:411` | 「投递≠批准」写进成员系统提示 | 全 src grep 无该提示 |

---

## 5. V2 项重新评估（成本等式已变）

| 项 | 设计依据 | 评估 |
|---|---|---|
| **agent→agent 直连邮箱** | §5.2 要点 3 有意不做（可解释性优先） | ⭐ **成本等式已变**：`worker_bus` 已提供现成的跨 session 派活/订阅/回执管道，**不需要新建邮箱**。建议以 **P2-2**（放开白名单 + 沿用 ask 权限）落地，而非原 V2 设想。这是本次调研最有价值的战略结论 |
| git worktree 隔离 / PR 自动化 | §1、§7 风险表 | **V2 可评估**：多成员并发改同一仓库的语义冲突是真实痛点，代价 M–L。建议先观察实际冲突频率再立项 |
| 外部 agent 当团队成员 | §1 非目标（进程管理） | `delegate_agent` 已覆盖托管；但「delegation 会话入编制」是 M–L 可选演进，与 P2-2 同属「成员异构化」议题 |
| 多机/云端网格 | §1、§5.8 | **维持不做**：本地桌面定位，`machine` 字段已预留（`store.py:200`），代价 L+ |
| 总览页 live 多列预览 | §9.5 | **按设计等用户反馈**：状态卡网格已实现 |
| 回帖 LLM 压缩 | §11 偏差 4 | **不算缺口**：1200 字截断是实施时定稿的有意替代 |

### §9 六条待决问题落实对账

| # | 问题 | 状态 |
|---|---|---|
| 1 | 团队绑 project | ✅ 已落实（`store.py:85-131`） |
| 2 | Lead 是谁 | ✅ 双落点已落实，但引出 **P0-2 / P1-8** |
| 3 | 成员离线保留 | ⚠️ 部分：解散=级联删（三重防呆），保留路径是 archived——但 archived 读路径半实现（**P2-8**） |
| 4 | @ 多人并行 | ❌ **未实现** → P1-9 |
| 5 | 总览 live 预览 | ✅ 有意不做（状态卡已落地） |
| 6 | seen 粒度 | ✅ 按客户端已实现；pin 徽标留尾 → P2-24 |

---

## 6. 明确「有意不做」（勿重复立项）

- **team 会话不进主侧栏/⌘K**：`sessionVisibility.ts:16-20` 有意隔离，非混乱。
- **i18n**：`chat.json` team.* 与 `composer.json` teamStatus 双语齐备。
- **回帖 1200 字截断**：实施时定稿的有意替代（非缺口）。

## 7. 做得对的（已核实，避免误报）

- 存储并发纪律合规：`store.py:27-31,39-45` RLock + tmp/`os.replace`，seen 合并在锁内，照 `session_meta._IDX_LOCK` 教训。
- blocked 原子拒绝优先于 steer/排队：`deliver.py:310-340`，`test_team_p3.py:171-184` 钉住。
- 解散防呆：活 turn 拒绝 + force + 级联删 + 清队列（`api/teams.py:287-321`）。
- 徽标无重复计数：`session_unread.py:22-30` 排除 team/team-member。

---

## 8. 建议执行顺序

1. **S 级快赢先清**（半天内）：**P0-3**（steer origin，现成范式遗漏）、**P0-5**（坏 roster 数据丢失）、**P0-2**（Lead 工具绑定，需产品确认语义）。
2. **补 turn 生命周期 team 钩子**——一次修好 **P0-7 + P1-2 + P1-3 + P1-4** 四项，建议一个 PR 统筹。
3. **P0-1**（发送生命周期 + 持久化）与 **P1-1**（回执可视化）合并开发。
4. **P0-4**（定数据源）、**P0-6**（模板三处）。
5. **P1-21 packaged e2e**——CLAUDE.md 红线，且 P0-1/P0-2 的验收都依赖它。
6. P1 剩余 S 级快赢：P1-6/8/10/11/12/13/15/16/17。
7. P2 按需；**P2-2（worker_bus）建议优先评估**——它让 agent→agent 协作从 L 降到 M。
8. P2-28 文档一致性随上述修复同步更新。

## 9. 待决问题（需用户拍板）

1. **P0-2 修复语义**：团队工具绑到**领队实例会话**（推荐，符合实体领队模型、改动小），还是让广场主会话恢复 Lead turn？
2. **P0-4 数据源**：补 turn 终态写 `stop_reason`，还是改读现成的 `meta.last_finished`/`last_error`？
3. **P0-6 成员 instructions 落点**：写实例 notes.md（走现有记忆注入，推荐）还是并入 agent 系统提示？
4. **P2-2/P2-3 权限边界**：worker_bus/渠道消息投给团队成员时，是否沿用工具层 ask 权限（推荐沿用，与 §5.7 一致）？
5. **P2-5 成本可见性是否纳入本期**：决定 P2-5/12/14 是「纯前端 S」还是「跨栈 M」。
6. **P0-1 是否已有在途修复**：主工作树当前在 `fix/notification-storm-p0` 分支，需确认是否已含相关改动，避免重复立项。
