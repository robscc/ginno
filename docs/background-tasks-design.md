# Ginno 后台任务设计

> 状态：已评审通过（2026-10-28，§11 五个开放问题全部落定）
> 对标：Claude Code 的 `run_in_background` bash + BashOutput + KillShell + 完成通知注入。
> 关系：与 subagent 互补 —— subagent 是「有 LLM loop 的子 session」（阻塞式嵌套等待），
> 后台任务是「无 LLM 的纯进程」（非阻塞，完成时注入回发起会话）。

## 0. 一句话

让 agent 把 shell 命令丢到后台跑、自己继续干别的活；命令结束后结果自动回到发起
session；人和 agent 随时可以查看日志、停止、删除。

## 1. 需求

Agent 侧：

- 「跑 `make app` / 大测试 / dev server，跑完告诉我结果」——不占住当前 turn。
- 跑的过程中可以：查看目前输出（tail）、停止、列举所有在跑的任务。
- 结束后（正常退出 / 非零退出 / 超时 / 被杀）**主动**把结果送回主 session，
  而不是等模型想起来轮询。

人侧：

- 任意时刻看到所有后台任务（跨 session），含运行中 / 最近完成。
- 点开看日志（tail + 全量），一键停止，完成后删除清理。
- 任务完成有可感知的通知（复用现有 session-done 通知 + 未读角标）。

典型场景：构建、长时间测试、`pnpm dev` 观察启动、训练/爬虫脚本、watch 脚本。
**V1 的「watch」语义 = 等进程退出**；逐行模式匹配监控（Monitor 式）见 §9 明确不做。

## 2. 核心决策

### D1. 后台任务是 runtime 的进程注册表对象，不是子 session

subagent 解决「另一个 LLM 帮我干活」，后台任务解决「一个进程帮我干活」。不建
session、无 checkpointer、无 token 消耗；只有进程 + 日志 spool + 元数据。

### D2. 结果回传 = 外部事件注入（复用 channels 双路），非阻塞

这是与 subagent 嵌套等待最大的区别。完成时把一条合成消息注入发起 session：

- session 空闲 → 走 `run_channel_turn` 同款路径发起新 turn；
- session 运行中 → 走 `steer_channel_turn` 同款路径入 steer 队列（模型本 turn 内
  就能看到，语义与 IM steer 一致）；
- session 已被删除 → 只做系统通知 + 任务中心展示，不注入。

模型侧同时提供查询工具（bg_list/bg_logs），覆盖「我现在就想看」的场景；注入
解决「模型忘了看」的场景。两者缺一不可，但注入是默认主通道。

### D3. 持久化到 `~/.ginno/tasks/`，日志先落盘再谈一切

```
~/.ginno/tasks/
  index.json            # 任务元数据索引（原子写）
  <task_id>/
    meta.json           # 单任务元数据（含最终状态/exit_code）
    output.log          # stdout+stderr 合并追加（含 \x00 分隔的启动头）
```

重启后 `index.json` 里 status=running 的任务标记为 `interrupted`（app 退出杀了
sidecar 的所有子进程）。日志与结果不丢——这是用户最在意的部分。

### D4. V1 进程是 sidecar 子进程，app 退出即死（诚实标注）

不做 detached/nohup 存活（P2 可选）。理由：主场景（构建/测试/观察）时长分钟级，
跨 app 重启存活的需求与「make app 后必须重启」的现实相互纠缠，V1 先把「死了但
日志在、状态标清楚了」做扎实。`interrupted` 任务在重启后的任务中心醒目展示
「Interrupted by app quit」。

### D5. 工具面 4+1 个，UI 面纯 REST

模型：`bg_run` / `bg_list` / `bg_logs` / `bg_stop`（+`bg_remove`）。
前端：`/api/tasks*` REST + 打开任务中心时轮询（WS 是 per-session 的，任务天然
跨 session，轮询 1–2s 足够；完成注入本身会推起新 turn，聊天流自动可见）。

### D6. 完成注入默认开，短窗合并

同一 session 5s 内多个任务完成 → 合并成一条注入消息，防止 turn 轰炸。
`bg_run(notify=false)` 可显式关掉（如「起 dev server 后不用汇报」场景——
尽管 dev server 不退出，用超时+bg_stop 兜底）。

## 3. 数据模型

```python
@dataclass
class TaskRecord:
    id: str              # "t_" + 8 位随机，全局唯一
    command: str         # 原始命令
    title: str           # 展示名：模型给 name 参数或命令前 40 字符
    cwd: str             # 发起时的工作目录（快照，不跟随 session 切换）
    status: str          # starting|running|finished|failed_to_start|
                         # stopped|timeout|interrupted
    exit_code: int | None
    pid: int | None
    origin_session: str | None   # 发起 session；手动创建时为 None
    origin_turn: str | None      # 排障用
    notify: bool                 # 完成时注入 origin_session，默认 True
    mode: str                    # "run"（默认）| "server"（等 healthy 行，不盼退出）
    healthy_pattern: str | None  # server 模式：日志行子串/glob，命中即注入「已就绪」
    healthy_at: float | None     # 命中时刻（一次即止，任务继续跑）
    timeout_s: int | None        # 默认 7200；None = 无限（需 bg_stop 收尾）
    created_at: float
    started_at: float | None
    ended_at: float | None
    log_path: str
    log_bytes: int               # 追踪 spool 大小，超限截头
```

状态机：

```
starting ──Popen ok──> running ──exit 0──> finished
   │                     │      ──exit≠0─> finished（exit_code 区分，不叫 failed：
   │                     │                  bash 语义里非零是正常信息）
   │                     ├──bg_stop/超时──> stopped / timeout
   └──Popen 抛错──> failed_to_start
（任何持久化态）──sidecar 重启──> interrupted（仅 running 会转）
```

任务删除 = 从 index 移除 + 删目录。运行中的任务删除 = 先 stop 再删（UI 二次确认）。

## 4. 模型工具

| 工具 | 参数 | 返回 |
|---|---|---|
| `bg_run` | `command, name="", cwd="", timeout=7200, notify=true, mode="run", healthy=""` | 立即返回 `t_xxx started (pid 1234, log ~/.ginno/tasks/t_xxx/output.log)`；提示模型「完成时会自动注入结果，无需轮询」 |
| `bg_list` | — | 全部任务一行摘要（id/状态/命令/时长/exit） |
| `bg_logs` | `task_id, tail=40, full=false` | tail 行数或全量（全量受 head+tail 预算截断）；未结束也返回当前已有输出 |
| `bg_stop` | `task_id` | 发 SIGTERM→2s→SIGKILL，状态 stopped |
| `bg_remove` | `task_id` | 停止并删除记录与日志 |

工具描述里写清与 `bash` 的分工：默认用 `bash`（阻塞、30s）；预期超过 ~1 分钟或
明确「跑完再叫我」的用 `bg_run`。两者共用 `_path_denied` deny-root 扫描与
workspace cwd 解析（同一套 guard，不另起炉灶）。

**server 模式**（评审决策 5）：用于永不退出的长驻进程（dev server / watcher）。
`healthy` 给出就绪标志（子串或 glob），reader 线程逐行匹配，命中即注入一次
「已就绪」事件（§5），任务继续跑；退出 / 停止 / 超时仍照常注入。`mode="server"`
不带 `healthy` 报参数错误。典型：`bg_run("pnpm dev", mode="server", healthy="Ready in")`。

## 5. 完成注入：消息格式

注入走 channel-input 同款管线，对 checkpointer 而言是一条带 origin 标记的
user-side 消息（UI 上渲染为 task 卡片消息，非用户气泡）：

```
[task finished] "make app" (t_ab12cd34) exit 0 · ran 12m30s
--- tail ---
...最后 40 行（stdout+stderr 合并）...
--- end ---
Full log: /Users/x/.ginno/tasks/t_ab12cd34/output.log
Other running tasks: t_ef56 (pnpm dev, 3m)
```

- tail 超预算走 head+tail 截断（同 bash 工具的 _compose_capped 预算思想）。
- 停止/超时/interrupted 重启补报 用同样模板，首行换为
  `[task stopped by user]` / `[task timeout after 2h]` / `[task interrupted by app quit]`。
- server 模式 healthy 命中（一次即止，任务继续跑）：
  `[task healthy] "pnpm dev" (t_xxx) ready after 0:04 — matched "Ready in"`，无 tail。
- 这些合成消息是**模型侧输入**；UI 不渲染成第二条消息——评审决策 4：UI 上一个
  task_id 只有一张常驻卡片，注入表现为卡片状态翻转（§6.1）。
- interrupted 补报在重启后第一次加载该 session 时注入一次（避免重启风暴）。
- session 运行中 → steer 入队；`steer_id` 去重复用现有机制。

## 6. UI 设计

### 6.1 聊天内 task 块（blocks.tsx 新 kind: `"task"`）

- `bg_run` 的 tool block 直接升级为常驻 task 卡片（不随 tool_result 结束而折叠）：
  标题行 = 图标 + title + 状态徽标（Running spinner / ● Healthy / ✓ exit 0 /
  ✗ exit 1 / Stopped / Interrupted）+ 计时。
- 展开区：日志尾部 20 行（点 "View full log" 跳任务中心抽屉）。
- 操作：Stop（运行中）/ Remove（已结束）。卡片状态由任务中心数据驱动（轮询）。
- 评审决策 4：一个 task_id 在整条时间线上**只有这一张卡**。完成 / healthy / 停止
  注入不产生独立的消息卡，全部表现为这张卡的状态翻转（RUNNING→EXIT 0 /
  RUNNING→● HEALTHY / →■ STOPPED）+ meta 行「result injected ✓」；新 turn 的
  agent 回复直接跟在卡后。历史回放按 task_id 同样合并渲染。

### 6.2 任务中心（全局，P1）

- 入口：侧栏底部 "Tasks" 行（运行中数量角标），点开右侧抽屉。
- 列表跨 session：Running 置顶（命令、来源 session 名、时长、进度条无——只有
  已写日志字节数），下面 Recent（今天完成的）。
- 行操作：查看日志（全量滚动 + 自动跟随到底部）、Stop、Remove。
- 空态文案："No background tasks. Ask the agent to run a command in the background."
- 保留策略：finished/stopped 记录保留 7 天或最近 50 条，启动时惰性清理。

### 6.3 通知

复用 session-done 通知（notify-rust 后端，settings `notifications`）：任务完成
即发系统通知（标题 = task title，正文 = exit 状态），点击聚焦发起 session。
未读角标复用 session_unread。

## 7. Runtime 实现要点（文件级）

- 新模块 `packages/runtime/src/ginno_runtime/tasks/`
  - `registry.py`：`TaskRegistry`——spawn（Popen + login shell，同 bash 的
    `$SHELL -lc`）、每任务 reader 线程追加写 spool（复用 `_drain_pipe` 思想，
    但写文件而非攒内存）、asyncio reaper（进程退出 → 写终态 → 触发注入/通知）、
    `stop_all()`（server lifespan shutdown 时 SIGTERM 全体）。
  - `store.py`：index/meta 原子读写（`_atomic_write_text` 同款）、启动时
    running→interrupted 迁移。
- `tools/tasks.py`（注册进 `build_builtin_tools` 所在 registry 流程）：bg_* 工具。
- 完成注入：把 `channels/base.py` 的 `steer_channel_turn` / `run_channel_turn`
  的「外部输入进 session」内核抽成 `session_inject.py`（channels 与 tasks 共用；
  channels 行为不变，纯抽取重构，channel 相关的媒体/会话确保逻辑留下）。
- REST `api/tasks.py`：`GET /api/tasks`、`GET /api/tasks/{id}`、
  `GET /api/tasks/{id}/logs?tail=&offset=`、`POST /api/tasks/{id}/stop`、
  `DELETE /api/tasks/{id}`。挂在 `server.py` 装配，lifespan 里起/停 registry。
- 前端：`apps/web/src/lib/api/tasks.ts` + blocks.tsx `"task"` kind +
  TaskCenter 组件 + i18n en/zh（术语表、tool_labels 迁移照旧清单走）。

## 8. 权限与安全

- deny-root 扫描：与 `bash` 完全同一套（`_path_denied` + user-rc 豁免）。
- 权限提示：`bg_run` 走与 bash 同级的确认（若现有 permission policy 拦 bash，
  同样拦 bg_run；后台化不降权限）。
- 数值全部走 settings.json 的 `background_tasks` 键（评审决策 2：可配置但
  **不做设置页面**），默认值：
  ```json
  "background_tasks": {
    "max_concurrent": 5,        // 超出 bg_run 报错并附 bg_list 摘要
    "default_timeout_s": 7200,  // timeout=null 仍需模型显式声明（须自行 bg_stop）
    "log_cap_mb": 10,           // 单任务 spool 上限，截头保尾记 truncated
    "inject_tail_lines": 40,    // 注入消息携带的 tail 行数
    "retention_days": 7,
    "retention_keep": 50
  }
  ```
- 注入消息永不出现在用户输入框/草稿；来源标记 `origin=task`，不可被 prompt 注入
  伪造（命令输出本身就是不可信内容——模型侧已按 tool_result 同级对待）。

## 9. 明确不做（V1）

- **进程跨 app 重启存活**（detached/setsid/重启认领）——P2 再议。
- **逐行 pattern 监控**（Monitor 式「每次出现 ERROR 告诉我」）——V1 只有退出事件。
- 后台任务里跑交互式 TUI / 向进程 stdin 写入。
- 手动建任务（评审决策 3）：任务中心无 New Task 入口，REST 不开
  `POST /api/tasks`——任务只能由 agent 的 `bg_run` 发起。
- 跨机器/远程任务、任务定时/cron（已有 scheduler 管定时）。
- Windows（当前平台只有 darwin）。

## 10. 分期

- **P0**（一个可评审的实现单元）：tasks 模块（registry+store+注入抽取）、bg_* 4+1
  工具（含 server 模式 healthy 行注入）、REST、聊天内 task 卡片（状态+tail+Stop）、
  重启 interrupted 迁移、单测（含 zombie 孙进程：管道被孙进程握住时 reader 线程
  要能收尾——bash 已有同款经验）。
- **P1**：任务中心抽屉、系统通知、完成注入合并窗、保留策略清理、i18n 打磨。
- **P2**：detached 模式（进程跨重启存活 + 重启认领）、pattern watch（server 模式
  泛化：多 pattern / 逐行事件）。

## 11. 开放问题（已决，2026-10-28 评审）

1. **完成注入默认开** ✅ —— `notify` 默认 `true`，模型可显式关。
2. **数值可配置** ✅ —— 并发 5 / 默认超时 2h / 日志 10 MB / 注入 tail 40 行 /
   保留 7 天（或最近 50 条）为默认值，进 settings.json `background_tasks`
   键（§8），不做设置页面。
3. **手动建任务** ❌ —— 任务中心不做 New Task 入口，REST 不开 POST /api/tasks。
4. **聊天卡片与注入消息合并** ✅ —— 一个 task_id 整条时间线只渲染一张卡，
   注入的合成消息是模型侧输入，UI 表现为卡片状态翻转（§6.1）。
5. **server 模式** ✅ —— `bg_run(mode="server", healthy="Ready in")`：日志行
   命中即注入一次「已就绪」，退出/停止仍照报，P0 实现（§4）。

## 12. 实现偏差记录（2026-10-28 实现后回填）

设计评审通过后按 §1–§11 实现，四处与原文的偏差（均已验证）：

1. **完成注入没有从 channels 抽公共模块**（§7 原计划）。channels 的双路注入
   与渠道回显耦合过深，抽取风险大；实际照抄 `subagent_scheduler/runner.py`
   的 `_inject_result`/`_queue_injection` 成熟模式，落在
   `tasks/inject.py`（busy → `steer_enqueue(origin="task",
   extra_kwargs.ginno_task_event)`；idle → 5s 合并窗 → headless 唤醒 turn，
   镜像 `_run_goal_turn` 的 `_RUNNING_TURNS` 早臂 + `set_interactive(False)`）。
   唯一共享改动是 `server_shared.steer_keep_channel` 扩展保留
   `origin in ("channel", "task")`，未吸收的 task steer 跨段存活，由
   600s rescue watcher 以唤醒 turn 补投。
2. **任务中心是 `/tasks` 路由页，不是抽屉**（§6.2）。仓库全局视图惯例
   （/scheduled、/kb、/connectors 都是路由页 + 侧栏入口），抽屉反而破例。
   通知点击落点也走它：Rust `focus_and_open("bg-task")` → web 侧
   `window.__ginnoOpenTasks()`（AppShell 注册）→ `/tasks`。
3. **OS 通知由前端轮询驱动，不走 runtime 推送**（§6.3）。SidebarFooter
   5s 全局轮询 `notifyTaskTransitions` 做状态 diff（仅后台标签页 +
   prefs 总闸触发，与 workflow-run 通知同一套语义）；runtime 的
   `tasks_registry_started` 只负责持久化迁移与 reaper，不产生 UI 事件。
4. **`kind: "bg-task"`** 补进 `lib/desktop.ts` NativeNotification 联合类型
   与 `notify.rs focus_and_open` 的分发（此前未知 kind 会误走
   `__ginnoOpenSession`，task id 不是 session id）。
