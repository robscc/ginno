# 通知系统设计（Notifications Design）

> **状态**：现行唯一权威 spec（2026-11-15）。
> **取代**：`docs/os-notifications-v2-design.md`——该文件在实现它的提交 `3ccf0af`
> 里被删除，此后 `handle.ts` / `notifyHub.ts` 的注释长期指向一个不存在的文件
> （已在 P0/P1 清理）。本文按**已合入 main 的实际代码**回写，不是提案。
> **配套**：`docs/notifications-audit-2026-11-14.md`（事故取证与根因，含实证数据）、
> `docs/notice-unification-design.md`（应用内 toast 总线，其 P2 已标 ARCHIVED）。
>
> 读这份文档前请先记住一句话：**发 OS 通知只有一条路——`lib/desktop.ts` 的
> `notifyNative()`，所有闸门装在它里面。** 任何新站点绕过它就等于绕过用户偏好。

---

## 1. 两个正交轴：点击目标 ≠ 通知分类

历史上只有一个 `kind` 字段，同时被当作「点了跳哪」和「这是哪类事件」用，导致
`NoticeHost` 的失焦兜底被迫借用 `kind:"test"`（点击只能 refocus）来发一条本该是
failure 类的通知。现已拆开：

| 轴 | 字段 | 取值 | 作用 | 谁消费 |
|---|---|---|---|---|
| **点击目标** | `kind` | `session` / `workflow-run` / `schedule-run` / `bg-task` / `test` | 用户点横幅后跳到哪 | Rust `focus_and_open()` |
| **通知分类** | `notifyKind` | `reply` / `askUser` / `failure` / `workflow` / `schedule` / `bgTask` / `test` | 过哪道闸门、放什么提示音 | web `osGateOpen()` + `soundFor()`；Rust 仅记日志 |

两轴**独立**：`kind:"session"` 的通知可以属于 `reply`、`askUser` 或 `failure` 任一分类。

`notifyKind` 在 `NativeNotification` 里是**必填**字段——这是刻意的：让 TypeScript
逼每个调用点声明自己属于哪一类，从而不可能「忘了过闸门」。P1 改造时正是它把所有
遗漏点暴露成编译错误（最终只剩 2 处，都在测试文件里）。

wire 上 `notifyKind` 序列化为 `notify_kind`（snake_case，与 Rust 侧一致），
`NotifyPayload` 用 `#[serde(default)] Option<String>` 接收，**旧 web bundle 不发这个
字段也能正常投递**（只是日志里显示 `notify_kind=?`）。

---

## 2. 六个通知分类及其生产者

| 分类 | 生产者（唯一） | 触发事件 | 点击目标 | 提示音 | 默认 |
|---|---|---|---|---|---|
| `reply` | `handle.ts` replyMerge flush | `message.end`（本会话 turn 完成）、`linked.done`（订阅会话 turn 完成） | `session` | 用户选的音色 | ON |
| `askUser` | `handle.ts` `maybeNotifyAskUser()` | `user.question`（ask_user 挂起） | `session` | 用户选的音色 | ON |
| `askUser` | `handle.ts` lastwords 分支 | `runtime.lastwords_pending`（重启后待确认的恢复） | `session` | 用户选的音色 | ON |
| `failure` | `handle.ts` error 分支 | turn 失败 | `session` | **Basso**（固定） | ON |
| `failure` | `NoticeHost` `useNoticeNativeFallback` | 失焦期间到达的 **error 级**应用内 notice | `test`（仅 refocus） | **Basso** | ON |
| `workflow` | `store/notifications.ts` `notifyRunTransitions` | 工作流 run 状态跳变到终态（5s 轮询 diff） | `workflow-run` | 用户选的音色 | ON |
| `schedule` | `store/notifications.ts` `notifyScheduleRunFinished` | `/api/ws/schedule` 推来 `run_finished` | `schedule-run` | 用户选的音色 | ON |
| `bgTask` | `store/notifications.ts` `notifyTaskTransitions` | 后台任务状态跳变到终态（5s 轮询 diff） | `bg-task` | 用户选的音色 | ON |
| `test` | `NotificationsSettings` 测试按钮 | 用户点击 | `test` | 用户选的音色 | —（无开关） |

### 2.1 为什么没有 `subagent` 分类

V2 曾列出 `subagent`（i18n 原文标注「预留」），**至今没有生产者**，P1 已删除：

- `linked.done` 在服务端就过滤掉了 subagent/schedule origin
  （`api/stream/engine.py:1303`：`if _origin in ("interactive", "deliver")`），
  所以它承载的是「订阅的**主**会话/worker」而非子代理；
- 子代理结果的通道是「注入父对话 + `worker.*` 应用内 notice」，从不发 OS 通知。

留一个拨了没反应的开关比没有更糟。**将来若真要做子代理 OS 通知，应作为新功能连同
它的事件源一起加回来**（届时需要决定：spawn/done 哪个时点、是否受 `reply` 的合并窗
约束、点击跳父会话还是子会话）。

### 2.2 `workflow` 分类为什么需要一次性迁移

该开关在 v1 里是死的（无代码消费），但 `saveNotifyPrefs` 会把整个 `kinds` 对象写盘，
所以用户 settings.json 里的 `workflow:false` **不是用户的选择，而是死开关的默认值被
持久化的副产物**。P1 接真之后若照抄这个 `false`，等于把用户一直在收的工作流通知
静默掉。

因此 `notifyPrefs.migrateKinds()` 在 `kinds_schema < 2` 时把 `workflow` 强制拉回
`true`，并由 `loadNotifyPrefs` 回写盖上 `kinds_schema: 2`。**盖过标记之后就是用户的
真选择，迁移不再覆盖。** `subagent` 的残留键由 `fromWire` 只读已知 `KINDS` 自然丢弃。

---

## 3. 闸门管线（唯一出口）

```
调用点
  │  ①「用户是否正盯着」——只有调用点知道（要比对 activeSid / 路由）
  │     isSessionAttentive(activeSid, sid)  会话级站点（handle.ts 四处）
  │     isAppAttentive()                    应用级站点（notifications.ts 三处 + NoticeHost）
  │  ② 节流/合并（仅 reply 类，见 §5）
  ▼
notifyNative(n)                            ← lib/desktop.ts，唯一出口
  │  ③ isDesktop()                非 Tauri 环境 → false（调用点可退 HTML5 Notification）
  │  ④ isNotifyOwnerWindow()      /pin 悬浮窗 → false（见 §6）
  │  ⑤ osGateOpen(n.notifyKind)   总开关 ∧ 分类开关 ∧ 不在勿扰窗；"test" 类绕过这三层
  │  ⑥ 隐私                       privacy === "titleOnly" → body 抹空（"test" 类不抹）
  │  ⑦ 提示音                     n.sound ?? soundFor(notifyKind)；failure → Basso
  ▼
emit("ginno:notify", { kind, notify_kind, id, title, body, sound })
  ▼
Rust lib.rs listen_any → std::thread::spawn → notify.rs show_notification_and_wait
```

③④⑤ 任一不通过都返回 `false`。**这意味着 `false` 不再只表示「桥不可用」**，也
表示「被偏好闸门拦下」——所以各调用点的 HTML5 浏览器兜底分支必须**自己再判一次**
`osGateOpen(kind)`，否则用户关掉了桌面提醒、dev 浏览器里反而照样弹。四个兜底点
（replyMerge flush、`notifyRunTransitions`、`notifyScheduleRunFinished`、
`NotificationsSettings` 测试按钮）都已如此处理。

### 3.1 为什么闸门装在出口而不是调用点

P0/P1 之前有 7 个站点各拼各的闸门，覆盖极不均匀（审计文档 P1-1 的矩阵）：
`handle.ts` 的 lastwords 与 `NoticeHost` 的失焦兜底**完全不过**总开关/分类/勿扰/隐私，
`store/notifications.ts` 的三个函数只过总开关。结果是「关掉桌面提醒」关不干净、
「开了勿扰」照样弹、「仅标题」隐私对一半站点无效。

**只要「发通知」这件事有 N 个入口，就必然有入口漏判**——而且新增站点时没有任何东西
强制它声明自己属于哪一类。收口到唯一出口 + `notifyKind` 必填，是把这条约束变成
编译期与运行时双重强制。

调用点仍保留 `shouldNotify(kind, watching)` 预判（= `!watching && osGateOpen(kind)`），
与出口判定**幂等重叠**，属有意的双保险：调用点先判可以省掉无谓的正文字符串构造，
出口再判则兜住任何漏网的站点。

---

## 4. 行为矩阵：事件 × 注意力 × 偏好

「注意力」= 用户是否正在看。**判据本体在 `lib/notifyHub.ts`，全站共用**：

- `isAppAttentive()` = `document.visibilityState === "visible"` **∧** `document.hasFocus()`
- `isSessionAttentive(activeSid, sid)` = `isAppAttentive()` ∧ `location.pathname === "/"` ∧ `activeSid === sid`

**为什么必须两个条件合取**——实测结论，探针与完整数据表见
`scripts/probe-wkwebview-attention.swift` + `.README.md`（macOS 15.6 / WKWebView，
2026-11-15）：

| 实测场景 | `visibilityState` | `hasFocus()` | 单看 visibilityState 会怎样 |
|---|---|---|---|
| 切到别的 App，Ginno 窗口仍在屏上 | **visible** | **false** | 误判成「在看」→ **该弹不弹** |
| 同 App 另一窗口为 key | visible | false | 同上 |
| **焦点在页面内 iframe**（MCP app 卡 / 代码面板 viewer） | visible | **true** | —— 这是纳入 hasFocus 的主要风险，**实测排除** |
| App 隐藏（`NSApp.hide`） | **hidden** | true（滞后） | 单看 hasFocus 会误判 → 故两者都不可省 |
| 最小化 | hidden | false | 一致 |

即：`visibilityState` 不追踪窗口焦点（漏「切走但没最小化」这个最常见场景），
`hasFocus()` 单独用又会在 App 隐藏时滞后。**合取**才同时覆盖两者。

`hasFocus` 方法不存在时（node/SSR、老环境）退回只看 `visibilityState`，即 P1 的
保守行为——不会因为拿不到信息就判成「没在看」而狂弹。

> 未覆盖的情形：被**别 App** 的窗口完全遮挡。实测中同 App 的不透明窗口完全遮挡时
> `visibilityState` 仍报 `visible`（WebKit 遮挡追踪对同 App 窗口不生效），别 App
> 遮挡未单独测；但「切走而未被完全盖住」已是主流场景，结论不受影响。

| 事件 | 用户正盯着该会话 | 用户在别的路由/别的会话/切到别的 App | 总开关 OFF | 该分类 OFF | 勿扰窗内 | privacy=titleOnly |
|---|---|---|---|---|---|---|
| turn 完成（reply） | 不弹（进 30s 合并窗，flush 时**复检**，仍在看则丢弃） | 弹，30s 内同会话合并、60s 全局冷却 | 不弹 | 不弹 | 不弹 | 弹，正文空 |
| 订阅会话完成（reply） | 同上（判的是**本**会话，见 §5.3） | 同上 | 不弹 | 不弹 | 不弹 | 弹，正文空 |
| ask_user 挂起 | 不弹 | **即时弹**，不合并、不冷却 | 不弹 | 不弹 | 不弹 | 弹，正文空 |
| lastwords 待确认 | **弹**（刻意不判注意力，见下） | 弹 | 不弹 | 不弹 | 不弹 | 弹，正文空 |
| turn 失败 | 不弹 | **即时弹** + Basso | 不弹 | 不弹 | 不弹 | 弹，正文空 |
| error 级应用内 notice（失焦兜底） | 不弹 | 弹 + Basso，点击仅 refocus | 不弹 | 不弹 | 不弹 | 弹，正文空 |
| 工作流 run 终态 | 不弹（徽标已覆盖） | 弹 | 不弹 | 不弹 | 不弹 | 弹，正文空 |
| 定时任务 run 终态 | 不弹 | 弹 | 不弹 | 不弹 | 不弹 | 弹，正文空 |
| 后台任务终态 | 不弹 | 弹 | 不弹 | 不弹 | 不弹 | 弹，正文空 |
| 设置页测试按钮 | 弹 | 弹 | **弹**（test 类绕过偏好） | **弹** | **弹** | **弹，正文不抹** |

### 4.1 两处刻意偏离，及其理由

**lastwords 不判注意力。** 它是重启后「有个 turn 被打断了，要不要恢复」的待确认提示，
漏掉的代价（工作白丢）远高于多看一条横幅的代价；且它本身罕见、应用内 notice 有
`dedupeKey: lastwords-pending:<sid>` 去重。它**不走** reply 的合并窗与 60s 冷却
（那是治「完成」类刷屏的），但仍完整受 `enabled` / `kinds.askUser` / DnD / privacy
约束——P1 之前它连总开关都不看。

**测试按钮绕过全部偏好闸门。** 它是用户的显式动作，必须在任何偏好下都给出可见反馈，
否则「按了没反应」会被误读成通知系统坏了。设置页的说明文案已写明它绕过总开关。
它**仍受窗口所有权约束**（`/pin` 窗口不发），因为那不是偏好而是架构约束。

---

## 5. 节流与合并语义（仅 reply 类）

`askUser` / `failure` **绝不节流**——等人和出事是「不看就永远卡着」的状态。
只有 `reply`（完成类）会刷屏，因此只有它有两级节流。

### 5.1 两级节流

| 级别 | 机制 | 窗口 | 作用域 | 位置 |
|---|---|---|---|---|
| 一级 | `ReplyMergeWindow` | 30s | **按会话**（key = 目标会话 id） | `handle.ts` 模块级单例 `replyMerge` |
| 二级 | `KindRateGate` | 60s | **全局**（跨会话） | `notifyHub.replyRateGate` |

一级把同一会话 30s 内的多次完成合并成一条「已完成 N 个任务」；二级把 30s 窗口之间、
跨会话的密集完成压到 60s 一条，被吞掉的条数由下一次放行时以标题后缀 `(+N)` 带出。

### 5.2 flush 时才判定，不在 push 时

`onFlush` 的顺序是：**复检注意力 → 过全局冷却 → 发**。

push 到 flush 隔着 30s，用户的注意力早变了；用 30 秒前的判定决定现在要不要打扰是错的。
同理，全局冷却也在 flush 时施加——这样「被合并掉的」和「被冷却吞掉的」两种抑制不会
互相重复计数。

### 5.3 `linked.done` 的注意力判据判的是**本**会话

订阅的 worker（`targetSid`）完成时，判定用的是 `isSessionAttentive(activeSid, 本地 sid)`
而非 `targetSid`。这是 V2 起的既有语义，P1 刻意保留：用户正盯着自己的会话时，
📡 chip 上的徽标已经可见，不需要再弹横幅。

代价是：如果你盯着会话 A、订阅的会话 B 完成，你会收到通知（因为判的是 A）。
这被认为是正确的——你订阅 B 就是为了被告知它完成了。

### 5.4 切进会话即取消

`cancelPendingReplyNotice(sid)` 挂在 `useChatStreamEngine` 的 `activeSessionId`
effect 上：用户切进某会话时，丢弃它待发的合并通知。

`ReplyMergeWindow.cancel()` 的注释从 V2 起就写着 "e.g. user switched to the session"，
但直到 P1 **全仓库零调用点**——API 造好了没接线。

### 5.5 历史教训：不要两套并行节流

P1 之前 `message.end` 走 30s 合并窗、`linked.done` 走 60s 全局冷却，**两套机制互不
相干**。2026-11-10 为治「多 worker 并行刷屏」加的 `KindRateGate` 只接到了
`linked.done` 一路，而多会话并发完成的主路径恰恰是 `message.end`——它完全没有全局
上限。现在两路 push 进同一个 `replyMerge`，冷却在 flush 时统一施加。

---

## 6. 窗口所有权：只有主窗发通知

`isNotifyOwnerWindow()` = `location.pathname !== "/pin"`。

悬浮速聊窗 `/pin` 为了拿 hooks 也挂了 AppShell，而 **AppShell 的所有 hook 都跑在
`if (pathname === "/pin") return <>{children}</>` 早退之前**：

| 通知源 | 位置 | 在 pin 窗口是否运行 |
|---|---|---|
| `useNoticeNativeFallback()` | `AppShell.tsx:56` | ✅ |
| schedule WS → `notifyScheduleRunFinished` | `AppShell.tsx:522-540`（无 pathname 门控） | ✅ |
| `useWorkflows` 轮询 → `notifyRunTransitions` | `store.tsx:309`（Provider 层） | ✅ |
| SidebarFooter 轮询 → `notifyTaskTransitions` | 组件内，pin 不渲染 | ❌ |

pin 窗口建出来即 hidden 且 hide 保活不销毁（`pin.rs` `ensure_pin_window`），所以它的
`document.visibilityState !== "visible"` **恒为真**——那三处「用户没在看才弹」的前提
在 pin 窗口**永远成立**。后果：用户正盯着主窗也照样弹；主窗同时隐藏时同一事件弹两条
（模块级状态按窗口独立，无跨窗去重）。

闸门装在 `notifyNative` 出口，一处覆盖全部站点，**包括将来新增的**。

> 若将来 pin 窗口需要发通知，正确做法是引入跨窗口的**通知所有者选举**（例如经 Tauri
> 命令竞争一个全局锁），而不是让每个窗口各自判定——后者必然重复。

---

## 7. 壳侧：投递、重试、点击回执、日志

### 7.1 投递重试（`notify.rs`）

`show()` 在 UN 后端要过 `mac-usernotifications` 的 `block_on_current`：不在主线程时，
它要求 `CFRunLoop::main().is_waiting()` 为真才肯发送，否则返回 `MainThreadNotRunning`。
而 `is_waiting()` 只在主 run loop **睡在等待相位**时为真——主线程正在执行回调（例如
正在派发触发本通知的那个 `ginno:notify` 事件）时它是 false。这是**假阴性**：run loop
明明在跑、响应迟早送达，库却拒发。

实测（审计文档 P0-2）：2357 次失败，占投递尝试的 40%；失败率随通知密度变化——
低负载区 0-5%，风暴区 30-53%。是负载相关的竞态，不是「线程用错了」。

处置：**有界重试**（5 次 × 25ms）。**不**把 `show()` 搬去 `run_on_main_thread`——
紧随其后的 `wait_for_action`（等用户点击，可能几分钟）会在主线程上阻塞，直接冻住 UI。
重试不会重复投递：`block_on_current` 在 poll future **之前**就做该判定，返回 Err 时
通知根本没发出去。

### 7.2 点击等待的并发上限

`MAX_ACTION_WAITERS = 4`：超过则 fire-and-forget（通知照发，只是点了不跳转）。
这是 2026-11-10 为治「几十路并发轮询把 ginno-desktop 打到 58% CPU、系统 usernoted
打到 84%」加的症状级补丁——**根因（P0-1 的 switch 穿透）修掉后它应退化为纯防御**，
`fire-and-forget` 日志归零即为验证。

### 7.3 日志格式（`common.rs` `shell_log`）

```
<本地时间戳含UTC偏移> <事件行>
2026-11-15T00:12:33.456+08:00 ginno:notify kind=session notify_kind=askUser id=1a0c… title="⏸ 测试会话"
2026-11-15T00:12:33.470+08:00 notification shown kind=session notify_kind=askUser id=1a0c… title="⏸ 测试会话"
```

- **时间戳**：毫秒精度（风暴期同一秒内有几十条，秒级看不出突发形态）；本地时间但
  带 UTC 偏移，故跨时区无歧义。用 `chrono`——它本来就在依赖图里（`trash` 引入），
  提为直接依赖的编译成本可忽略（实测整次 `cargo check` 7.3s）。
  没有消费方解析这个格式：`maintenance.py` 只按 mtime 轮转，`lib.rs` 只是在编辑器里
  打开它。
- **`notify_kind` + `title` 前 40 字符**：由 `NotifyPayload::log_ctx()` 统一输出，
  5 处日志行全部复用。此前只记点击目标 `kind`，3858 条 `kind=session` 无法区分是
  reply 还是误发的 askUser——P0 排查时只能靠排除法推断。标题前缀用于同分类内进一步
  定位（`⏸` = ask_user 挂起、`⚠️` = 失败、`📡` = 订阅会话完成、`⚡` = lastwords）。
  按 `chars()` 截断而非字节切片：标题是中文/emoji，按字节切会截出非法 UTF-8。
- **带锁 append**：进程内 `Mutex` 串行化。此前多线程无锁 append 在 19879 行日志里
  产生 **529 行粘连**（一条记录的 `id=` 直接接上下一条的前缀、没有换行），恰好在
  风暴期让日志不可解析。锁中毒被忽略（`into_inner`）：一个 panic 的写者不该让此后
  所有诊断静默。

---

## 8. 持久化

`settings.json` 的 `notifications` 键，wire 形状用 snake_case：

```jsonc
{
  "enabled": true,
  "sound": true,
  "sound_name": "Glass",
  "kinds": { "reply": true, "askUser": true, "failure": true,
             "workflow": true, "schedule": true, "bgTask": true },
  "kinds_schema": 2,          // 见 §2.2；缺失视作 1 → 触发迁移
  "privacy": "full",           // 或 "titleOnly"
  "dnd": { "enabled": false, "from": "22:00", "to": "08:00" }
}
```

- `fromWire` 只读已知键，未知键（如已删除的 `subagent`）自然丢弃，下次存盘清掉。
- **`fromWire` 必须对 `kinds` 和 `dnd` 都做一层展开拷贝**。`{...DEFAULT_PREFS}` 是
  浅拷贝，`d.dnd` 与 `DEFAULT_PREFS.dnd` 曾是同一引用，`d.dnd.enabled = …` 会写穿到
  模块级默认值——用户设过一次勿扰窗后默认值被永久污染，此后任何缺 `dnd` 键的回退
  都落到被污染的「默认」上，勿扰再也关不掉且重启不愈。P1 修复并加了三条回归。
- 旧版 `localStorage["ginno:notify"]`（V1 三键）在首次 `loadNotifyPrefs` 时迁移并删除。

---

## 9. 测试策略：哪一层测什么

| 层 | 文件 | 测什么 | 为什么必须在这一层 |
|---|---|---|---|
| reducer | `handleNotify.test.ts`（15 例） | 把**真实事件**喂进真实 `handle()`，数 `notifyNative` 调用次数 | switch 穿透这类**结构性**错误，纯函数测试原理上抓不到 |
| 出口闸门 | `desktopNotifyGate.test.ts`（18 例） | owner-window × enabled × kinds × DnD × privacy × sound × test 绕过 | 闸门装在出口，就必须对出口做端到端断言 |
| 纯函数 | `notifyHub.test.ts`（23 例） | 合并窗、冷却门、勿扰窗、`fromWire`/`migrateKinds`、注意力判据、`DEFAULT_PREFS` 不被污染 | 时间与偏好组合爆炸，适合表驱动 |
| 真实运行时 | 见下 | 真 sidecar + 真 Chromium + 真 WS + 真 reducer 的 emit 计数 | 单测全绿也可能整条链路不通 |
| 打包 UI e2e | `packages/runtime/tests/e2e/test_packaged_ui_notify.py` | dist sidecar + 真浏览器里的通知边界 | CLAUDE.md 硬性要求：前端交互改动必须配 packaged UI 用例 |

**reducer 层测试必须双向**：既断言「非提问事件零通知」（防复发），也断言「真挂起
问题仍恰好弹一条」（防修过头把合法通知杀了）。只测一个方向的回归测试会在下一次
「优化」时被反向破坏。

**真实运行时的两个保真度坑**（都实测踩过）：

1. fake LLM 一个 turn 只吐 **1 个** chunk，而真实 provider 是几百个 delta 帧
   （runtime 侧 33ms 合帧）。必须按 33ms 节奏**显式排期**放大帧；用
   `setTimeout(…, 0)` 会把大批回调挤进同一毫秒，旧实现的 `${sid}:${Date.now()}`
   去重键随之碰撞，201 帧只漏出 3 条通知——严重低估。
2. Node 25 暴露了 `localStorage` 全局对象但 `getItem` **不是函数**（未开
   `--experimental-webstorage`），而 `loadNotifyPrefs` 第一行就调它 → 抛 TypeError
   被自己的 catch 吞掉、偏好永远停在默认值、`getSettings` 一次都不会被调。浏览器里
   正常，vitest（node 环境）里必须打桩。

---

## 10. 已知残留与刻意不做的事

| 项 | 状态 | 说明 |
|---|---|---|
| `hasFocus` 纳入注意力判据 | **已核实并纳入**（2026-11-15） | WKWebView 探针实测：`visibilityState` 不追踪窗口焦点（切到别的 App 而窗口仍在屏上时仍报 `visible`），而焦点位于 iframe 内时顶层 `hasFocus()` 仍为 true——即纳入 hasFocus 能修好「该弹不弹」，且不会把 MCP app 卡 / 代码面板 viewer 的使用误判成「没在看」。判据取**合取** `visible && hasFocus()`（App 隐藏时 hasFocus 会滞后报 true，故两者都不可省）。探针、跑法与完整数据表：`scripts/probe-wkwebview-attention.swift` + `.README.md` |
| 非 error 级 notice 的失焦兜底 | 刻意不做 | 需要 Rust 侧加 `notice` 点击目标（约 10 行，`focus_and_open` 里与 `test` 同分支）。但 info/success 级 toast 多是短暂确认（「模型已切换」），失焦时逐条转 OS 通知是净噪声。若将来确有需求，**走这条廉价路径，不要建 Rust 通知窗** |
| Rust 通知窗（`notice-unification` P2） | **ARCHIVED** | 见该文档的归档说明 |
| 跨窗口通知去重 | 靠窗口所有权规避 | 只有主窗发通知，因此不需要。若将来放开，需引入所有者选举 |
| 勿扰窗内的「静默投递到通知中心」 | 刻意不做 | 需要 Rust 侧支持「不弹横幅但入中心」，UN 后端无此细粒度控制；当前语义是**完全抑制** |
| `shell.log` 轮转 | 依赖 `maintenance.py` | 壳侧自己不做轮转，按 mtime 清理 |
