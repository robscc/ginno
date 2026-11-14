# 通知机制审计报告（2026-11-14）

> 触发：用户反馈「通知基本上是乱通知」。
> 方法：源码静态取证 + git 考古 + 运行时日志实证（`$HOME/.ginno/logs/shell.log`）。
> 状态：**仅诊断，未改动任何代码**；修复方案分三批，等定夺后施工。
> 关联：`docs/notice-unification-design.md`（应用内 toast 总线，P0/P1 已落地、P2 未做）；
> `docs/os-notifications-v2-design.md`（OS 通知 V2 spec，**已被实现它的提交 3ccf0af 删除**，
> 而 `handle.ts:31`、`lib/notifyHub.ts` 的注释至今引用它）。

## 0. 结论先行

「乱通知」不是策略调参问题，是**三个 P0 缺陷叠加**：

1. 一个 switch 穿透让**每个流式 token 都发一条 OS 通知**（不该响的狂响）；
2. 一个线程模型错误让 **40% 的通知静默失败**（该响的经常不响）；
3. 一个常驻隐藏窗口成为**无闸门的第二通知源**（正盯着也弹、且重复弹）。

三者叠加的主观感受就是「完全随机」。

## 1. 实证数据

来源 `$HOME/.ginno/logs/shell.log`（19879 行，审计时仍在写入）。

| 指标 | 数值 | 含义 |
|---|---|---|
| 通知相关行占比 | **77%**（15459 行） | 诊断日志被通知流量淹没 |
| web→壳 `ginno:notify` 事件 | 6183 | 前端发出的通知请求总数 |
| 真实投递 macOS（`notification shown`） | 3379 | — |
| **`show FAILED`** | **2254（占 40%）** | 错误原文全是 `Mainthread not running` |
| `fire-and-forget`（超 4 路并发等待） | 182 | 通知风暴 footprint |
| 单会话最高通知数 | **1583 条**（`1a0c1d77…`） | 一个会话被弹了 1583 次 |
| 次高会话 | 927 条（`7a680f0a…`）、364 条（`c79b49cd…`） | 非孤例 |
| 并发写粘连行 | 529 | 多线程无锁 append，风暴旁证 |
| 密度分布 | 全日志均匀 ~190 条/千行 | **稳态，不是一次性事故** |

按 kind 分布：`session` 3858 / `schedule-run` 14 / `test` 11 / `bg-task` 4 —— `session`
类占 99%，即风暴全部来自会话事件通道（reply / askUser / failure / lastwords 共用该点击目标）。

最密的 200 行窗口（行 19350-19550）含 66 条通知，呈严格三连循环、同一 session id、
中间**零其它事件**：

```
ginno:notify kind=session id=1a0c1d77a4364728bf3573a83d6d05ac
notification shown kind=session id=1a0c1d77a4364728bf3573a83d6d05ac
notification fire-and-forget (5 waiters in flight) kind=session id=…
```

## 2. 根因

### P0-1 switch 穿透：每个 token delta 发一条「等你回答」

`apps/web/src/components/chat/engine/handle.ts:323-362` 的 case 结构
（`git show 3ccf0af` 确认由 V2 提交引入）：

```ts
case "token.delta":
case "thinking.delta":
  …stripFromLive…
// fall through                      ← 显式穿透
case "tool.start":
case "tool.args":
case "tool.end":
case "user.question": {               ← V2 把这里改成块，塞进了通知代码
  const qid = typeof ev.id === "string" ? ev.id : `${sid}:${Date.now()}`;
  if (!notifiedQuestionIds.has(qid)) { …shouldNotify("askUser")→notifyNative… }
  adoptOrphanStream(…); mutateLive(…); break;
}
```

作者以为该块只服务 `user.question`，实际被 **5 个事件共享**。致命点在 `qid` 兜底：

- `token.delta` / `thinking.delta` 载荷是 `{event, content}`，**没有 `id` 字段**
  （`packages/runtime/src/ginno_runtime/api/stream/engine.py:547/583/623`）
  → 走 `${sid}:${Date.now()}` → **每帧都是新 key，去重集永不命中**；
- delta 合帧窗口 33ms（`api/stream/delta_coalesce.py`）→ **约 30 条通知/秒/会话**；
- `kinds.askUser` 默认 `true`（`lib/notifyPrefs.ts:69`），`!watching` 在「切到别的
  App / 别的路由 / 别的会话」时全部成立 → 闸门全开；
- 会话 socket **切会话不关闭**（`useChatStreamEngine.ts:464-466` 注释明写
  "Sockets stay open when the user switches sessions"）→ 所有访问过的会话都在持续供帧。

**排除法确认是 askUser 而非 reply**：reply 走 `replyMerge` 30s 合并窗，单会话上限约
2 条/分钟，凑不出 1583 条；failure 按错误事件计。只有 delta 穿透能解释该量级与
「三连循环、零间隔」形态。

**两个连带损伤**：

1. `_saveNotifiedQuestionIds()` 在块内 → **每个 delta 写一次 sessionStorage**
   （最多 1000 条 JSON ≈ 40KB），主线程抖动；
2. `size > 1000 → clear()` → delta 洪水灌满后整体清空，**真实挂起问题的去重记录被
   一起抹掉**，`b088cc0`（2026-11-10「ask_user 挂起通知跨重载重复弹」）修的 bug 被
   这个洪水重新打开。

> 旁证：`apps/desktop/src/notify.rs:52-58` 记着 2026-11-10 实测「通知风暴把
> ginno-desktop 打到 58% CPU、系统 usernoted 打到 84%」，当时处置是加
> `MAX_ACTION_WAITERS = 4` 上限——**治了轮询线程这个症状，没找到风暴源**。

### P0-2 40% 通知静默失败：`show()` 跑在裸 spawn 线程上

`apps/desktop/src/lib.rs:568` → `std::thread::spawn(move || show_notification_and_wait(h, payload))`，
`notify.rs:83` 在该线程里直接 `n.show()`。notify-rust 的 UNUserNotificationCenter
后端要求主线程 run loop，于是 2254 次返回 `Err("Mainthread not running")`，被
`shell_log` 记一行后 `return` 掉——**用户完全无感**。

### P0-3 隐藏悬浮窗 `/pin` 是第二个无闸门通知源

`/pin` 渲染 `PinApp`，但 **AppShell 的 hooks 在 `if (pathname === "/pin") return`
之前全部执行**（`AppShell.tsx:873` 早退，hooks 在 56 / 522 行）：

| 通知源 | 位置 | 在 pin 窗口是否运行 |
|---|---|---|
| `useNoticeNativeFallback()` | AppShell:56 | ✅ 运行 |
| schedule WS → `notifyScheduleRunFinished` | AppShell:522-540（无 pathname 门控） | ✅ 运行 |
| `useWorkflows` 轮询 → `notifyRunTransitions` | store.tsx:309（Provider 层） | ✅ 运行 |
| SidebarFooter 轮询 → `notifyTaskTransitions` | 组件内，pin 不渲染 | ❌ 不运行 |

pin 窗口**常驻 hidden**（`pin.rs:348` 懒建、hide 保活不销毁）→ 它的
`document.visibilityState !== "visible"` **恒为真** → 这三个函数的「用户没在看才弹」
前提**永远成立**。后果：

- 正盯着主窗，定时任务/工作流完成照样弹（pin 窗口发的）；
- 主窗若同时隐藏，**同一事件弹两条**（模块级状态按窗口独立，无跨窗去重）。

### P1-1 四套闸门各说各话

`notifyPrefs` 提供 `enabled / kinds / dnd / privacy` 四层偏好，覆盖极不均匀：

| 发通知站点 | enabled | kinds | DnD | privacy | 注意力判据 |
|---|:-:|:-:|:-:|:-:|---|
| `handle.ts:350` askUser | ✅ | ✅ | ✅ | ✅ | visibility+route+activeSid |
| `handle.ts:762` linked.done | ✅ | ✅ | ✅ | ✅ | 判的是**本**会话非目标会话 |
| `handle.ts:1544` message.end | ✅ | ✅ | ✅ | ✅ | visibility+route+activeSid |
| `handle.ts:1633` failure | ✅ | ✅ | ✅ | ✅ | 同上 |
| **`handle.ts:1013` lastwords** | ❌ | ❌ | ❌ | ❌ | **无任何判据** |
| **`NoticeHost.tsx:147` 失焦兜底** | ❌ | ❌ | ❌ | ❌ | visibility+**hasFocus** |
| **`notifications.ts:36/108/170`** workflow/schedule/bg-task | ✅ | ❌ | ❌ | ❌ | **仅 visibility** |

→ 关掉「启用桌面提醒」，lastwords 和 error 级 toast 照样弹；开了勿扰，工作流/定时
任务/后台任务照样弹；设了「仅标题」隐私，这三类照样把全文塞进横幅。

### P1-2 设置页两个开关是死的

`shouldNotify` 全仓库只被 `askUser / reply / failure` 三种调用过。设置页按 `KINDS`
渲染 5 个开关（`NotificationsSettings.tsx:124`），其中 **`workflow` 和 `subagent`
无任何代码消费**——用户拨了没反应。而真正发工作流通知的 `notifyRunTransitions`
只看 `enabled`，不看 `kinds.workflow`。

### P1-3 reply 的两套节流互不相干

| | 触发路径 | 节流机制 | 全局冷却 |
|---|---|---|---|
| A | `message.end`（本会话 turn 完成） | `replyMerge` 30s **按会话**合并 | ❌ 无 |
| B | `linked.done`（订阅的 worker 完成） | `replyRateGate` 60s **全局**冷却 | ✅ 有 |

2026-11-10 为治「多 worker 并行刷屏」加的 `KindRateGate` **只接到了 B，没接到 A**——
而 A 才是多会话并发完成的主路径。另外：

- `ReplyMergeWindow.cancel(key)` 注释写着「e.g. user switched to the session」，但
  **全仓库零调用点**（只有 `push`）→ 30s 内点进那个会话，通知照弹；
- 闸门在 **push 时**求值，flush 时（30s 后）不复检 → 用 30 秒前的注意力状态决定
  现在要不要打扰。

### P1-4 注意力判据不统一

只有 `NoticeHost.tsx:144` 用了 `visibilityState && hasFocus` 双判，其余 5 处只看
`visibilityState`。macOS 上「窗口在屏幕上但失焦」（切到别的 App 而没最小化）
`visibilityState` 是否降级为 `hidden` 取决于 WebKit 遮挡追踪，**不可靠**（待实机核实）
——若为 `visible`，则 `isWatching` 误判为「用户在看」→ **该弹的不弹**。

### P2 制度性成因

1. **设计文档被实现它的那个提交删掉了**：`3ccf0af` 的 stat 里有
   `docs/os-notifications-v2-design.md | 89 ----------`，而代码注释至今指向它——
   **没有 spec 可对照**；
2. **零端到端覆盖**：`notifyHub.test.ts` 只单测 `ReplyMergeWindow / KindRateGate /
   inDndWindow / fromWire` 这些纯函数，**没有一个测试把事件喂进 `handle()`**。
   switch 穿透这种结构性错误，纯函数测试原理上抓不到。CLAUDE.md 明写「前端交互改动
   必须在 `tests/e2e/` 加 packaged UI 用例」，V2 这次没加；
3. **可观测性不足**：`shell_log` 只记 `kind`（点击目标）和 `id`，**不记通知类
   （reply/askUser/failure）和标题**——只能靠排除法断定是 askUser。且 `kind` 一词被
   两个正交概念共用（`NativeNotification.kind` 是点击目标，`NotifyKind` 是事件类），
   `NoticeHost` 兜底因此被迫用 `kind:"test"` 这种 hack；日志还是多线程无锁 append
   （529 行粘连）；
4. `notice-unification-design.md` 的 **P2（Rust 通知窗）从未落地**，双渲染层只完成一半。

## 3. 修复方案

### 第一批 P0（止血，约 2-3h，改 3 个文件 + 1 个测试）

**① 拆掉 switch 穿透**（`handle.ts`）——把 `user.question` 独立成 case，通知逻辑抽成
具名函数，让结构本身不可能再穿透：

```ts
case "token.delta":
case "thinking.delta":
  …stripFromLive…
// fall through
case "tool.start":
case "tool.args":
case "tool.end":
  adoptOrphanStream(ctx, sid, ev); mutateLive(ctx, sid, ev); break;

case "user.question": {
  maybeNotifyAskUser(ctx, sid, ev);   // ← 只有这里会发 askUser 通知
  adoptOrphanStream(ctx, sid, ev); mutateLive(ctx, sid, ev); break;
}
```

`maybeNotifyAskUser` 内部：`ev.id` 缺失就直接 return（真问题必带 tool_call id，缺失即
异常，不该发通知）；去重集改 LRU 淘汰而非整体 `clear()`；sessionStorage 写盘节流
（同 tick 合并）。

**② `show()` 派到主线程**（`lib.rs` / `notify.rs`）——用 `app.run_on_main_thread(...)`
投递 `show()`，点击等待仍留后台线程。目标：`show FAILED: Mainthread not running` 归零。

**③ 单一通知所有者**（`lib/desktop.ts`）——把闸门下沉进 `notifyNative()` 这个唯一出口，
一处收口覆盖全部 6 个站点（含未来新增）：

```ts
export async function notifyNative(n: NativeNotification & { notifyKind: NotifyKind }): Promise<boolean> {
  if (!isDesktop()) return false;
  if (window.location.pathname === "/pin") return false;   // pin 窗口永不发通知
  const np = notifyPrefs();
  if (!np.enabled || !np.kinds[n.notifyKind]) return false;
  if (inDndWindow(np.dnd, minutesOfDay())) return false;
  const body = np.privacy === "titleOnly" ? "" : n.body;
  …emit…
}
```

同时把 `kind`（点击目标）与 `notifyKind`（事件类）在类型上分开，`NoticeHost` 兜底不再
借用 `kind:"test"`。

**④ 补回归测试**（欠账）——表驱动：把 `token.delta / thinking.delta / tool.start /
tool.args / tool.end` 逐个喂进 `handle()`，断言 `notifyNative` **零调用**；喂
`user.question` 断言**恰好一次**、重放同 id 断言不重复。这条测试是防止穿透复发的唯一
可靠手段。

### 第二批 P1（一致性，约半天）

| # | 改动 | 效果 |
|---|---|---|
| 5 | `NotifyKind` 补 `schedule / bgTask`；`notifications.ts` 三个函数改走 `notifyNative` 统一闸门 | kinds/DnD/privacy 对全部通知生效 |
| 6 | 设置页 5 个开关全部接真（`workflow`→`notifyRunTransitions`，`subagent`→子代理/linked.done）；接不上的就**删掉开关**，不留假控件 | 设置项全部可信 |
| 7 | reply 节流合一：`message.end` 与 `linked.done` 共用同一 gate + merge 实例；**flush 时复检注意力**；在 activeSid 变更的 effect 里调 `replyMerge.cancel(prevSid)` | 消除双轨与过期闸门 |
| 8 | 抽 `isAttentive()` 单一判据（`visible ∧ hasFocus ∧ pathname==="/" ∧ activeSid===sid`），6 个站点共用 | 消除「该弹不弹」 |
| 9 | `handle.ts:1013` lastwords 与 `NoticeHost` 兜底纳入统一闸门（保留「高优先级不受冷却」语义，但必须受 `enabled`/DnD 约束） | 关得掉 |
| 10 | `shell_log` 增记 `notifyKind` 与标题前 40 字符；改带锁 append 或单线程日志队列 | 下次可直接定位；消除 529 行粘连 |

### 第三批 P2（收口，按需）

11. 重写 OS 通知 spec（建议 `docs/notifications-design.md`），把「事件 × 注意力状态 ×
    偏好 → 是否弹 / 弹什么 / 点了去哪」写成矩阵表作为唯一 spec；修掉 `handle.ts:31`、
    `notifyHub.ts` 两处指向不存在文件的注释；
12. 决策 `notice-unification` 的 P2（Rust 通知窗）做或不做——不做就标 ARCHIVED
    （仓库已有先例：Code Mode 设计如此处理），别留半成品状态；
13. 复核 `MAX_ACTION_WAITERS = 4`：根因修掉后它应退化为纯防御，`fire-and-forget`
    日志归零即为验证（建议保留）。

## 4. 验收对照表

| 验收项 | 判据 | 取证方式 |
|---|---|---|
| delta 不再触发通知 | 后台会话跑长任务，`ginno:notify` 增量 ≈ 0 | 日志计数 + 新增单测 |
| askUser 仍正常 | 真挂起问题时恰好 1 条，重连不重弹 | 单测 + 实机 |
| 投递失败归零 | `show FAILED` 计数 0 | `grep -c 'show FAILED' shell.log` |
| pin 窗口不发通知 | 主窗可见时定时任务完成 → 0 或 1 条（不重复） | 实机 + 日志 |
| 总开关真能关 | 关掉后**任何**通知都不弹（含 lastwords/error 兜底） | 实机逐项触发 |
| DnD 真能拦 | 勿扰窗内 workflow/schedule/bg-task 也静默 | 改系统时间或临时窗口 |
| 5 个分类开关全部有效 | 逐个关闭后对应事件不弹 | 实机矩阵 |
| reply 不刷屏 | N 个后台会话同时完成 → 1 条带 `(+N)` | 实机 |
| 切进会话即取消 | 30s 内点进该会话 → 不再弹 | 实机 |
| 日志可读 | 通知流量占比从 77% 降到个位数；无粘连行 | 重跑本次统计脚本 |

## 5. 验证深度与待核实

**已实证**（读代码 + git 考古 + 运行时日志统计 + 真实运行时前后对比）：P0-1 的 switch
结构与 delta 无 `id` 字段、P0-2 的 2357 次 `Mainthread not running`、P0-3 的 hooks 早于
`/pin` 早退执行、P1-1/2/3 的闸门覆盖与零调用点、P2 的文档删除与测试缺口、以及
1583 条/单会话的风暴量级。修复效果见 §6。

**未做**：`shell.log` 的前后百分比对比需要重建并重启桌面应用（web bundle 与 Rust 壳
都变了），而审计与施工都发生在运行中的 Ginno 会话内，重启会终止会话本身——故 §6 改为
量测 shell.log 通知流量的**唯一上游**（web 侧 `emit("ginno:notify")`）。合并后按 §4 的
步骤重建重启，即可用附录脚本补上 shell.log 侧的数字。

**待核实**（两项，实机 5 分钟可定）：

1. macOS 上「窗口可见但失焦」时 WKWebView 的 `visibilityState` 究竟是 `visible` 还是
   `hidden`——决定 P1-4 的严重度是「该弹不弹」还是仅口径不统一；
2. `Mainthread not running` 是否**全部**由并发风暴诱发（线程数暴涨时 notify-rust 内部
   状态失效），还是低频下也会稳定复现——决定 P0-2 的有界重试在低负载下是否只是纯保险。

## 6. 修复后真实运行时复测（前后同口径）

**为什么不直接读 shell.log**：`shell.log` 由 Tauri 壳写，拿到修复后的数字必须重建并重启
桌面应用。改为直接量**壳侧通知流量的唯一上游**——web 侧 `notifyNative()` →
`emit("ginno:notify")`；`lib.rs:566` 每收到一个 emit 就记一行 `ginno:notify kind=… id=…`，
两者一一对应，且不受壳侧投递失败（P0-2）干扰。

**真实度**：真 sidecar（源码起，`GINNO_FAKE_LLM` 脚本化模型，非 mock 前端）+ 真
Chromium（playwright）+ 真 WebSocket + 真 `handle()` reducer + 真 `notifyPrefs` 闸门。
仅注入两处，且都不改动被测代码：

1. **Tauri 桥打桩**——浏览器里没有壳，桩接 `__TAURI_INTERNALS__.invoke` 才能捕获
   `plugin:event|emit` 里的 `ginno:notify` 载荷；
2. **WS 帧放大**——fake LLM 一个 turn 只吐 1 个 chunk，而真实 provider 是几百个 delta
   帧。按 runtime 侧 `delta_coalesce` 的**真实 33ms 合帧节奏**把 `token.delta` 复制 200
   份排期派发。
   > 保真度坑（实测踩过）：用 `setTimeout(…, 0)` 同步错峰会把大批回调挤进同一毫秒，
   > 旧实现的 `${sid}:${Date.now()}` 去重键随之碰撞，201 帧只漏出 3 条通知——严重
   > 低估修复前的量级。必须按 33ms 显式排期，让 `Date.now()` 真的往前走。

**注意力状态**：`document.visibilityState` 覆写为 `hidden`，模拟「用户切到别的 App、
窗口未最小化」——这正是生产里风暴的触发条件（`isWatching` 转 false，闸门打开）。

**结果（`--amp 200`，两次运行同一脚本同一参数）**：

| 场景 | 修复前 `fdedcd2` | 修复后 |
|---|---|---|
| turn1：201 个 `token.delta` 帧（33ms 节奏，用户未在看） | **201 条** `⏸ Ginno`「等你回答」 | **0 条** |
| turn2：1 个真 `ask_user`（`user.question` 带 tool_call id） | 1 条 | **1 条**（body「等你回答——任务已挂起」） |

修复前**严格 1:1**（一帧一条），与 §1 的单会话 1583 条、与单测里「50 帧 = 50 条」互相
印证；修复后归零，且**合法 ask_user 通知未被误杀**（正向用例通过）。

> 口径说明：turn1 的 reply 类完成通知走 `replyMerge` 的 30s 合并窗，15s 测量窗内不会
> flush，故不计入上表——这是既有设计（P1-3 待改），非本次改动引入。

## 附：复现统计的脚本口径

对 `$HOME/.ginno/logs/shell.log` 逐行分类计数（`ginno:notify` / `notification shown` /
`show FAILED` / `fire-and-forget` / `wait_for_action`），按 `id=(\S+)` 提取 session id
做 Counter，再以 50 行步长滑窗（窗宽 200 行）求 `notification shown` 密度峰值。
注意日志无时间戳，故用**文件行号位置**代替时间轴做趋势判断。
