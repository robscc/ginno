# dev-swap 热切换进度可视化 — 产品设计（MVP）

日期：2026-11-04 ｜ 状态：已实现（MVP 全量；偏差见文末「实现偏差记录」）
背景：`make dev-swap` / 聊天内 `/dev-swap` 的构建切换链路已有（scripts/dev_swap.sh + apps/desktop/src/dev_swap.rs），
但进度只存在于终端 log（`[dev-swap] quiesce OK`…），聊天里发 `/dev-swap` 更是完全黑盒——
用户只能盯 TopBar 等琥珀 `dev·{指纹}` 黄条出现。本方案把全链路进度搬进应用 UI。

## 0. 目标

- 一条从「发起 → 构建 → 预检 → 切换 → 壳收尾 → 验证」的**可见进度线**，任何一步失败能看到失败在哪、为什么。
- 不引入新进程/端口：进度上报复用 sidecar 现有 HTTP + 事件通道。

## 1. 总体形态：进度卡片（DevSwapCard）

发起 dev-swap（聊天 `/dev-swap` 或 Debug 菜单）后，在 **TopBar dev 黄条**下方弹出一张固定宽度卡片，
六个阶段纵向时间线（对齐 dev_swap.sh 实际阶段序）：

```
┌─ dev-swap · ginno → a3f9c2e ──────────────── ①─┐
│ ✔ 静默检查     无运行中 turn              0.4s │
│ ✔ 单测         pytest -m unit · 214 passed  86s │
│ ◐ 构建-runtime  PyInstaller …           ▓▓▓░░  │  ← 当前步：耗时大头，带预计
│ ○ 预检         blue-green :8799 试起            │
│ ○ 切换         symlink → dev-override/a3f9c2e  │
│ ○ 壳收尾       重启 sidecar + /api/health 指纹  │
│                          [取消]（仅排队阶段可用） │
└─────────────────────────────────────────────────┘
```

- 每行三段：状态点（✔/◐/○/✖）+ 阶段名 + 明细（结果摘要或实时输出尾行）。
- 全部完成 → 卡片转绿 1.5s 后收起，TopBar 黄条即终态凭证；失败 → 红色停在失败步，展开能看到**该阶段尾 30 行日志** + 「回滚」按钮（回滚 = symlink 指回旧目录，dev_swap.rs 已有 prune 逻辑可复用）。

## 2. 数据通道（核心设计决策）

进度产生方是 **dev_swap.sh（终端）或 /dev-swap 命令（sidecar 内）**，展示方是前端。两条路径统一到一个事件：

### 2.1 进度上报：`POST /api/devswap/progress`（sidecar 新增，仅 127.0.0.1）

dev_swap.sh 每进入/完成一个阶段，`curl` 上报一行 JSON：

```json
{"run_id":"a3f9c2e","stage":"build_runtime","state":"running",
 "detail":"PyInstaller …","pct":55,"log_tail":"..."}
```

- run_id = 目标指纹（脚本已有 NEW_DIR 命名，天然幂等键）；stage 枚举固定六值。
- `pct` 仅 build 阶段上报（按 dist 产物体积增速估算，粗粒度即可）；其余阶段二元。
- shell 侧封装成 `report()` 函数（替代现有 log()，log 与上报双写），失败静默——**上报挂了不影响构建**。

### 2.2 下发：`runtime.devswap_progress` WS 事件

sidecar 收到上报后缓存当前 run 状态（server_shared 单例 dict），并向所有连接广播
`devswap_progress` 事件（低频：阶段切换 + build 每 2s 节流），前端 DevSwapCard 订阅。
`runtime.dev_swap_pending`（既有）继续作为「切换就绪、等壳收尾」的触发，不改动。

### 2.3 /dev-swap（聊天发起）路径

聊天命令在 sidecar 进程内直接 subprocess 跑 dev_swap.sh，天然能实时逐行读 stdout——
解析同一 `[stage]` 前缀回填同一事件，不走上报端点。两条来源以 run_id 去重。

## 3. 阶段与超时语义

| 阶段 | 预期时长 | 超时 | 失败处理 |
|---|---|---|---|
| quiesce 静默检查 | <1s | 10s | 有运行 turn → 等待态（黄点），可点「不等了，强制」 |
| unit 单测 | ~90s | 10min | 失败即停，卡片红 + pytest 尾行 |
| build runtime | 3-6min | 20min | 同上；pct 失准只影响进度条不影响结果 |
| preflight 预检 | ~20s | 2min | 失败即停（不切换，零风险） |
| switch symlink | <1s | 5s | 极小窗口失败 → 直接重跑 |
| 壳收尾+验证 | ~10s | 60s | 失败自动回滚旧版本（现有逻辑），卡片展示回滚结果 |

取消语义：仅前两阶段允许取消（kill 子进程树）；build 之后取消=浪费但安全（bundle 未挂载）。

## 4. session 内进度倒计时提示

聊天里发起 `/dev-swap` 时，用户视角就是一条消息——进度不应只活在弹窗卡片里，
还要**在当前 session 的流式区**给出可跟随的状态。复用现有「等待下一轮」状态行机制
（streamCards.tsx 的尾部状态行，wave dots + 小字）扩展为**进度态**：

1. **turn 内状态行**：`/dev-swap` 命令执行期间，尾部状态行显示
   `dev-swap · 构建-runtime ▓▓▓░░ 55% · 已用 2:41 / 预计 5:00`，
   阶段推进时原地刷新（同一 span key，不产生新气泡）。
2. **倒计时来源**：每阶段维护 EMA 期望时长（首次用 §3 表的经验值，之后按本机历史
   实测均值自适应，`~/.ginno/devswap-stats.json` 落盘累计）；显示「预计剩余」而非倒计时
   绝对秒——构建时长抖动大，假精确反而误导。
3. **超时告警**：某阶段超过历史 P90 时长，状态行变琥珀并提示「比往常慢（P90=xx）」，
   提前给用户「可能卡了」的信号，而不是干等超时。
4. **完成/失败落块**：turn 结束时产出一个 `devswap_result` 块（复用 turn_meta 的角落
   chrome 语言）：一行结果 + 指纹 + 耗时 + 失败时附带失败阶段与「查看日志」链接
   （点开=DevSwapCard 日志展开同源）。这样进度既实时可见、结束后又沉淀为可回看的记录。
5. 跨会话：若在 A 会话发起、切到 B 会话，B 不打扰；回到 A 时先拉 `GET /api/devswap/progress`
   快照续播（与 §5 前端 hook 同源）。

## 5. 前端改动清单

- `components/devswap/DevSwapCard.tsx`（新）：时间线卡片 + 取消/回滚/日志展开。
- `useDevSwapProgress`（新 hook）：订阅 `devswap_progress`，重启后（壳收尾会断 WS）凭
  `GET /api/devswap/progress`（返回当前 run 状态）恢复——**WS 断线重连后先拉一次快照再续订**。
- TopBar dev 黄条与卡片状态联动：pending→呼吸态，done→常亮。

## 7. 不做（二期）

- 多历史 run 列表（本次只显示当前/最近一次）。
- CI/远端机器的 dev-swap 进度。
- pct 精确化（PyInstaller 无原生进度，产物体积估算够用）。

## 8. 测试

- 单测：上报端点解析、六阶段状态机（running→ok/fail 转移、超时）、run_id 去重、WS 断连快照恢复。
- shell：`report()` 在 curl 不可用时静默降级（模拟 sidecar 离线，构建仍成功）。
- 手工验收：`make dev-swap` 与聊天 `/dev-swap` 双路径进度一致；中途 Ctrl-C 终端，卡片 30s 内标失败不悬挂。
- session 倒计时：EMA 统计持久化往返；P90 慢告警阈值；切换会话后回来快照续播不重复计时。

## 9. 实现偏差记录（2026-11-04 实现时定案）

1. **事件/快照契约为扁平 current-line**：§2.2 的 payload 落地为
   `{ok, run:{run_id, sid, stage, state, detail, pct, log_tail, ts, started_at,
   stages:{stage→state}}}`（广播与 GET 同构）。run 级 state 映射：任一阶段
   fail → fail；finish ok/rolled_back → 同名终态；其余 running。首版曾广播
   嵌套快照（stages 全量对象），前端 normalize 只认顶层字段 → 事件全被
   丢弃、整条链路死亡——扁平化即修复，前端测试有契约回归用例。
2. **finish 终态由新进程的 state.json watcher 补报**：成功/回滚的终态只有
   重启后的新 sidecar 有资格上报（旧进程连同内存快照一起被换掉）。
   `api/devswap.watch_swap_finish`（server lifespan 拉起）盯
   `dev-override/state.json` 的 pending → verified/rolled_back(_factory) 跳变，
   闸门「本次启动后见过 pending」防普通重启重复报。state.json 新增
   `session_id`（registry 经 env `DEV_SWAP_SESSION_ID` 注入）。
3. **session 状态行不依赖 streaming**：/dev-swap 是后台任务、发起 turn 秒结，
   §4.1 的「turn 内状态行」若挂在 streaming 判据下永不出现——改为
   run 进行中挂**发起会话最后一个 assistant 气泡**尾部（isTail）持续显示；
   发起会话的 streaming 等待位也优先显示进度。会话门控 §4.5：事件带 sid，
   仅发起会话显示；终端 make dev-swap（无 sid）只有弹窗卡片。
4. **devswap_result 落块是 live-only 客户端块**：§4.4 的「可回看记录」降级为
   live 会话内的 system 消息（subagent_spawn 同款先例，不持久化）——持久
   凭证是 TopBar dev 指纹黄条。落块由 engine 订阅总线 + 会话切换补读双触发，
   run_id 全局去重。
5. **EMA/P90 统计存前端 localStorage**（`ginno:devswap-stats`），不落
   `~/.ginno/devswap-stats.json`——不动后端，换机重学可接受。
6. **build pct = dist 体积估算**：dev_swap.sh 后台 5s 采样 du（基线 = 构建
   前旧 bundle 体积，无基线用 256MB 经验值），封顶 95%；上报走 report()
   第 4 参，广播侧维持 2s 节流。
7. **徽标 + 进度 pill 迁到侧栏品牌行（2026-11-04 二次反馈）**：dev·{指纹}
   黄条与 hover 进度 pill 从 TopBar 迁至 SidebarHeader 品牌行（GinnoWork
   右侧），hover 弹层改 `left-0` 向右覆盖聊天区（aside 无 overflow 不裁）。
   同批把 channel 修成**运行时事实**：壳 spawn ladder 走 dev-override 分支时
   注入 `GINNO_RUNTIME_CHANNEL=dev`，`_build.build_info()` 以 env 覆盖
   build-info.json 写死的构建目标——此前 channel 恒为 app，徽标只能靠
   `-dirty` 指纹启发式点亮。徽标同时显示 built_at 时刻（HH:mm），便于
   「跑的是哪次构建」一眼可辨。
