# 通知统一设计：单总线 · 双渲染层 · 未读/已读同通道

> 来源：2026-11-07 会话决策（toast 右下角统一 + Tauri 双版本 + 未读体系统一）。
> 落层：P0/P1 纯 `apps/web`；P2 加 `apps/desktop`（Rust）。
> 预估：P0 2-3h · P1 4-6h · P2 8-12h（合计 ~16-22h）。

## 0. 现状问题（调研结论，2026-11-07）

| 问题 | 证据 |
|---|---|
| 5 套悬浮 toast 并存：NoticeHost（右上）/ ModToastHost（右下）/ fallbackToast（右下）/ DevSwapCard（右下持久卡）/ McpSettings 私有（底部居中） | 全手写无第三方库 |
| 同坐标互相重叠 | ModToastHost 与 fallbackToast 同为 `bottom-5 right-5` |
| settings 等路由通知不可见 | NoticeHost 挂工作区列内，非工作区路由父容器 `hidden` |
| 未读无通道 | 会话未读圆点（transcript 域）与通知各自为政；OS 原生通知（notifyNative）点了无回执 |

## 1. 目标架构

```
通知事件源（WS handle / UI 操作 / Rust 事件）
        │
        ▼
  pushNotice(level, text, opts)          ← 唯一总线（lib/notice.ts 扩展）
        │
        ├─① 渲染分发（render.ts，按环境二选一）
        │    ├─ 无壳（isDesktop()=false）→ Web 版：React NoticeHost，fixed 右下角
        │    └─ 有壳（isDesktop()=true）→ Rust 版：复用/新建通知窗（pin.rs 范式）
        │         Tauri event 推卡片数据 → 通知窗渲染同一份 <NoticeCard/>
        │         action 点击经 Rust event 转回主窗执行
        │
        ├─② 未读/已读通道（notice store，同一次 push 写入）
        │    ├─ badge 未读数（顶栏 bell）
        │    ├─ 收件箱面板（列表 / 单条已读 / 全部已读）
        │    └─ 已读状态机：unread → read（四种触发，见 §3）
        │
        └─③ 兜底：主窗失焦时 OS 原生通知（复用 notifyNative，kind 泛化 notice）
```

**核心原则**：一个事件源 → 一次 `pushNotice` → 三条出口（渲染/未读/兜底）自动联动；调用点永远只面对总线。

## 2. 双渲染层（"做两个版本"）

### 2.1 环境探测
复用 `lib/desktop.ts` 的 `isDesktop()`（`__TAURI_INTERNALS__` + WKWebView UA 双保险），分发在 `noticeRender` 内部，调用点无感。

### 2.2 Web 版（无壳）
现 NoticeHost 改造：
- 容器 `absolute right-3 top-2 z-30` → `fixed bottom-6 right-6 z-50`
- 挂载点从工作区列提到 AppShell 顶层（顺带修复 settings 路由不可见）
- 进场动画从上滑入 → 从下滑入

### 2.3 Rust 版（有壳）——UI 与 Web 版一致
**方案：独立通知窗（抄 pin.rs 作业，先例已验证）**，不是 OS 通知（OS 样式无法定制，做不到"UI 跟 web 一致"）：

| 关注点 | 做法（复用 pin.rs 手法） |
|---|---|
| 窗口 | `WebviewWindowBuilder` transparent + undecorated + always_on_top + skip_taskbar + focus:false（不抢焦点） |
| 定位 | `monitor_logical()` 锚主窗右下角（与 Web 版视觉位置一致），主窗移动时跟随 |
| 内容 | 加载轻量通知入口路由，渲染**同一个** `NoticeCard` 组件（UI 100% 一致） |
| 数据 | Rust `emit_to(notice-win, "notice", payload)`；卡片倒计时结束/手关 → `invoke` 收卡片，全部消失 → hide 窗口保活复用（不销毁，避免反复建窗开销） |
| action 跨窗 | 通知窗点击按钮 → `emit_to(main, "notice-action", id)` → 主窗执行原 `action.onClick`（如跳设置页）并标已读 |
| 堆叠 | 卡片在窗口内 flex-col 堆叠，`NOTICE_MAX` 同 Web 版（3，必要时 4） |

**与现有 OS 原生通知的关系**：保留为兜底——仅主窗**失焦/最小化**时同时发 `notifyNative`（点击回焦点）；聚焦时只走通知窗，不再打扰系统通知中心。

## 3. 未读/已读管理（同通道）

### 3.1 notice 生命周期扩展
```ts
interface Notice {
  …现有字段…;
  mode: "toast" | "silent";   // silent：不弹卡片，仅进收件箱+badge（回合完成等不打扰型）
  readAt: number | null;      // null = 未读
}
```

### 3.2 已读状态机（四种触发，全部走总线 API）
| 触发 | 调用 |
|---|---|
| 用户点 toast 的 action 按钮 | `markNoticeRead(id)`（action 执行附带） |
| 用户点 × 关闭 toast | `markNoticeRead(id)` |
| 收件箱点开条目 | `markNoticeRead(id)` |
| 收件箱「全部已读」 | `markAllNoticesRead()` |

badge 未读数 = `readAt===null` 计数；toast 被自动超时消失**不算已读**（保留在收件箱，与手机通知中心语义一致）。

### 3.3 与既有未读体系的关系（边界）
- **会话未读圆点**（transcript 域）：维持现有 session store 为**单一事实源**，不迁移。通知域为每个有新回复的会话投一条 silent notice（`kind:"session"`, `countInBadge:false`）——只作为收件箱里可点的记录，**不计入 badge**（防双算）。
- **badge = 通知未读数（notice store）+ 会话未读数（session store）**，badge 组件订阅两 store 合并 selector。
- **session 条目点击已读的原子双写**：
  ```
  用户点收件箱里 session 条目（或直接点开会话）
    ├─ markNoticeRead(noticeId)          → 通知域投影条目 readAt=now
    ├─ sessionStore.markSessionRead(id)  → 会话 unread=0（现有函数）
    ▼
  badge selector 重算：unreadSessions-1 → badge N → N-1
  ```
  两个 store 独立 emit、badge 各重算一次，最终值幂等一致（无竞态）。
- **worker/团队未读**：同上聚合展示，不迁移。

### 3.4 持久化
P1：localStorage（`ginno:noticebox`，上限 100 条滚动窗口）。
P2 可选：升级 runtime `api/notifications`（跨设备/多窗一致）——本地优先原则下非必须。

## 4. 存量 5 套体系的收编

| 体系 | 处置 | 改动点 |
|---|---|---|
| NoticeHost | 升级为唯一总线渲染层（Web 版） | 容器定位 + 挂载点 + 动画方向 |
| ModToastHost | 退役：`pushModToast` 函数体转发 `pushNotice(...,{source:"mod"})`，调用方零改动 | handle.ts:1603 + 删组件 |
| AppShell fallbackToast | 合流：私有 state → `pushNotice("info",…)` | AppShell 2 处 |
| McpSettings 私有 toast | 合流：`showToast(msg,tone)` 内部映射转发；3 子组件 `onToast` 接口不动 | McpSettings 2 处 |
| DevSwapCard | 不进总线（持久交互卡非 toast），移入右下统一容器堆叠避让 | 容器归属 |

不动：CodePanel 等 7 处面板内嵌条（贴上下文的就地提示）；会话未读圆点。

## 5. 分期与验收

| 期 | 内容 | 量 | 验收 |
|---|---|---|---|
| P0 | 总线统一 + Web 版右下角 + settings 可见性修复 + mod/MCP/fallback 收编 + DevSwapCard 同容器 | 2-3h | 全部悬浮 toast 出自单容器右下角；无第三方库；现有测试零破坏 ✅ done 2026-11-07 |
| P1 | 未读/已读通道：mode 扩展 + badge + 收件箱面板 + 四种已读触发 + 失焦时 OS 兜底联动 | 4-6h | push 一次即 badge+1；四种触发均清；silent 不弹卡片 |
| P2 | Rust 通知窗（pin.rs 范式）+ 双版本分发 + action 跨窗转发 + 主窗跟随定位 | 8-12h | 桌面版通知为独立置顶窗 UI 与 Web 版像素级同源；失焦时 OS 兜底 |

## 6. 风险与声明

- 本机无 node/pnpm：P0/P1 组件渲染效果**未编译验证**，需 `pnpm dev` 或 CI `web` job 验证；`cargo check` 可本地跑（P2）。
- pin 窗口先例是 mini 模式改造而非纯新建，通知窗建窗细节（首次冷启动时机、多显示器 DPI）实现时以 pin.rs 为准逐项核对。
- 通知窗 hide 保活复用的内存驻留 < 若干 MB 级 webview，可接受（pin 同款取舍）。
