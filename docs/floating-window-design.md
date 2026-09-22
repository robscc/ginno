# Ginno 悬浮速聊窗（Floating Quick Chat）实施计划

> 状态：待评审 · 2026-09-19
> 产品设计稿：`design-mocks/floating-chat-design.html`（方案 B，已拍板）
> 已确认决策：Q1 完整 agent 能力 + 内联确认 · Q2 默认独立快速会话（可切跟随）·
> Q3 默认避让全屏 Space · Q4 菜单栏图标 + Dock 保留 · Q5 胶囊点击穿透做、默认关

## 0. 一句话架构

第二个 Tauri `WebviewWindow`（label `pin`）加载 `http://127.0.0.1:8787/pin`；
前端绕开 AppShell 渲染独立 `PinApp`（胶囊/迷你窗两视图）；聊天走**新建轻量
PinStream**（复用 `blocks.tsx` 渲染件 + 现有 WS 协议，不动 ChatStream 单体）；
runtime 仅加 `session.type="quick"` 字段与 settings `floating` 默认键。
**WS 协议零改动**——runtime 本来就是每会话多 socket 广播（`stream.py` `_SESSION_WS`），
权限应答去重（`_PENDING_RESUME`）也已存在，双窗口同订一个会话开箱即用。

## 1. 数据模型（先定这部分）

### 1.1 SessionMeta 增加 type 字段

- runtime：`api/sessions.py` `CreateSessionRequest` + `type: str | None = None`；
  创建时写入 meta（`sessions.py:521-535` 处）；`_index.json` 无 schema 迁移成本（旧会话缺字段 = 普通会话）。
- 前端：`apps/web/src/lib/types.ts` `SessionMeta` + `type?: "quick"`。
- quick 会话仍归属 `project_slug="default"`（与现状一致），主窗口会话列表加 ⚡ 角标，不过滤。
- checkpoint 零成本：`FileCheckpointer` 按 `thread_id=session_id` 懒创建，无需注册。

### 1.2 settings.json 新增 `floating` 键（前端经 GET/PUT /api/settings 读写）

```jsonc
"floating": {
  "hotkey": "CommandOrControl+Shift+Space", // ⇧⌘Space（非 ⌃⌥Space——后者撞 macOS 输入法切换）；global-shortcut 语法；设置页按键录入器；注册失败降级 tray 并提示
  "default_mode": "quick",                  // "quick" | "follow"（跟随主窗口当前会话）
  "inactive_opacity": 0.7,                  // 失焦透明度，1.0 = 关闭该行为
  "visible_on_all_spaces": true,
  "fullscreen_policy": "avoid",             // "avoid"（默认，macOS 原生行为，零成本）| "overlay"（NSWindowCollectionBehaviorFullScreenAuxiliary）
  "pill_click_through": false,              // Q5：胶囊态 set_ignore_cursor_events
  "show_on_launch": false
}
```

- 默认值写进 `paths.py::_DEFAULT_SETTINGS`（`ensure_layout` 自动 seed）。
- 前端设置组件照抄 `notifyPrefs.ts` 的读-改-写整文件模式。
- **Rust 不写 settings.json**（PUT 是整文件覆盖，会互相冲掉）。

### 1.3 ~/.ginno/floating.json（Rust 独占，窗口几何持久化）

```jsonc
{
  "mini": { "x": 0, "y": 0, "w": 360, "h": 520 },   // 迷你窗上次位置尺寸（按显示器 memory 由 monitor id 区分可后加）
  "pill": { "x": 0, "y": 0 }                          // 胶囊吸附角落后的位置
}
```

## 2. 接口（IPC / 事件 / 协议）

### 2.1 Rust 命令（首个 #[tauri::command] + invoke_handler，均为 app 自定义命令，capabilities 无需逐条放行）

| 命令 | 作用 |
|---|---|
| `pin_toggle()` | tray/设置页入口：隐藏↔显示（上次形态）；mini 形态显示时发 `pin:focus-input` |
| `pin_set_mode(mode: "mini"｜"pill")` | 前端收起/展开按钮调用；Rust 负责 resize/移动/几何保存/恢复 |
| `pin_hide()` | ✕：隐藏窗口（会话保留），非销毁 |
| `pin_open_main(session_id: Option<String>)` | ⌘↵ 接管：show+focus main，`eval __ginnoOpenSession('<sid>')`（复用现有全局钩子，lib.rs 已有该机制） |
| `pin_apply_prefs(prefs)` | 前端把 settings.floating 推给 Rust（透明度/all-spaces/穿透/热键重注册） |

### 2.2 Rust → pin webview 事件（状态源在 Rust）

- `pin:mode` `{mode, reason}`：快捷键/tray 触发形态变化时通知前端切换视图（PillView ⇄ MiniView）。
- `pin:focus-input`：窗口唤出并 set_focus 后发出，前端把光标放进输入框（webview 常驻，textarea 的 autoFocus 只在首次挂载生效；仅靠窗口焦点不够——WKWebView 的 DOM focus 停在原处）。PinApp 收到后经 DOM 事件转给 PinStream，pill→mini 展开时 PinApp 也会补发一次。
- 前端 → Rust 用命令；Rust → 前端仅此两个事件，避免双向状态漂移。

### 2.3 前端双窗口协调（同源 localStorage/BroadcastChannel 天然共享）

- 跟随模式：新建 `BroadcastChannel("ginno-active-session")`；主窗口在
  `setActiveSession` 处 broadcast `{session_id}`；pin 订阅。主窗口改动 ~5 行。
- pin 窗口**不写** `ginno-last-session`（避免污染主窗口的会话恢复），自己的状态用 `ginno-pin-*` 前缀 key。

### 2.4 WS 协议

零改动。PinStream 只实现事件子集：
`turn.start / token.delta / thinking.delta(仅指示) / tool.start / tool.args / tool.end /
permission.request / message.end / turn.stopped / turn.state / error / notice / session_title`；
发送 `invoke / stop / permission_response / turn_state / ping`。
其余事件（usage/goal/run.*/preview.* 等）忽略。

## 3. 用户可见行为（对照产品稿）

- 唤起：`⌥⌘Space`（可改）/ tray「显示悬浮窗」/ 主窗口标题栏图钉按钮（后续可加）。
- 全局快捷键 =「唤出输入框」：隐藏→显示迷你窗；胶囊→展开迷你窗（穿透胶囊唯一的唤出路径）；迷你窗→隐藏。唤出后光标自动落在输入框，可直接打字。tray 仍是纯显示/隐藏（上次形态）。
- 三态：隐藏 ⇄ 胶囊（状态点：绿=运行中/灰=空闲/橙=待确认；橙时弹跳一次）⇄ 迷你窗 360×520（可拖、可resize 280–480 宽、距边 16px 吸附）。
- Esc/⌄ 收胶囊；点胶囊展开；✕ 隐藏（会话保留，下次唤出恢复）；⌘↵ 移交主窗口并滚动到该会话。
- 失焦 70% 透明（可关）；`.floating` 层级；默认所有 Space 可见；默认避让全屏 Space。
- 完整 agent 能力：工具行折叠为一行（点击展开）、代码块限高 3 行、权限确认内联 [允许一次][拒绝]。
- 会话：默认独立快速会话（可「新对话」）；会话标识处下拉切「跟随 · <主窗口会话名>」。
- tray：显示/隐藏悬浮窗 · 打开主窗口 · 退出（退出走现有 quitting 流程杀 sidecar）。

## 4. 实施阶段（机械部分）

### Phase 1 · runtime（半天）
- `sessions.py`：CreateSessionRequest/meta + `type`。
- `paths.py`：`_DEFAULT_SETTINGS` + `floating`。
- 测试：`packages/runtime/tests/` 加 type 字段透传用例。

### Phase 2 · Tauri 壳（1–1.5 天）
- `Cargo.toml`：+`tauri-plugin-global-shortcut`、tauri features +`tray-icon`,`macos-private-api`；
  `tauri.conf.json` +`"app": {"macOSPrivateApi": true}`。
- `src/lib.rs`（注意：不是 src-tauri/，Tauri crate 直接在 apps/desktop/）：
  - `WebviewWindowBuilder` 懒创建 pin（`always_on_top/decorations(false)/transparent/shadow/resizable/skip_taskbar/visible(false)`，URL `/pin`）；
  - 命令表 + `invoke_handler(generate_handler![...])`（当前完全没有，净新增）；
  - tray（`TrayIconBuilder` + 菜单）；global-shortcut 注册（失败降级 + notice）；
  - `on_window_event` 按 label 分支：pin 的 CloseRequested=hide（现有逻辑硬编码 main）；
  - 几何持久化 `~/.ginno/floating.json`；show 时 clamp 到当前 monitor 工作区（避开刘海）；
  - `set_visible_on_all_workspaces(true)`（Tauri v2 API；不行则 objc2 兜底）；
  - `fullscreen_policy="overlay"` 时设 collectionBehavior（avoid 为默认不动）。
- `capabilities/default.json`：windows +`"pin"`，permissions +`core:window:allow-start-dragging`（前端 `data-tauri-drag-region`）。

### Phase 3 · 前端 /pin（1.5–2 天）
- `app/pin/page.tsx` + `AppShell.tsx` 顶部早退分支：`pathname === "/pin"` 时只渲染 children（不挂 workspace/ChatStream/sidebar）。Next 静态导出产出 `pin.html`，runtime `_serve_web` 的 `p+".html"` 匹配已覆盖，SPA fallback 兜底。
- `components/pin/PinApp.tsx`：视图切换（PillView/MiniView）、`pin:mode` 事件订阅、状态点聚合（由 PinStream 的 turn/permission 状态驱动）、快捷键（Esc/⌘↵）。
- `components/pin/PinStream.tsx`：轻量聊天（~700 行内）：socket 生命周期照抄 ChatStream 的重连/心跳/`turn_state` 语义（3s 重连、20s ping、45s 静默重连），消息 ref 自持；复用 `blocks.tsx` 的 `InnerBlocks/ToolBlock/Markdown`（+`compact` prop：代码块限高、工具行默认折叠）；权限块从协议直连（`permission.request` → 内联按钮 → `permission_response`）。
- `lib/pinPrefs.ts`：`floating` 键读-改-写（照 notifyPrefs 模式）+ `pin_apply_prefs` 推送。
- 快速会话生命周期：pin 首启创建/恢复 `type:"quick"` 会话（`ginno-pin-session` key）；「新对话」按钮；跟随模式走 BroadcastChannel。
- 主窗口：`setActiveSession` 处 broadcast（~5 行）；会话列表 ⚡ 角标（AppShell 一行条件渲染）。

### Phase 4 · 设置页 + 打磨（1 天）
- `settings/[tab]`：`generateStaticParams` +`"floating"`；`SettingsNav`/`SettingsView` +分支；`FloatingSettings.tsx`（热键/透明度/Spaces/全屏策略/穿透/启动显示）。
- 边缘吸附、橙色待确认弹跳动画、pill click-through 开关联调。
- 真机验证清单：Raycast 共存（热键冲突降级）、全屏 Space 避让、Stage Manager、双显示器位置记忆、截屏可见性提示、`make app` 后**完全退出重启**再验（CLAUDE.md 已知故障模式 #1）。

## 5. 风险与对策

| 风险 | 对策 |
|---|---|
| PinStream 与 ChatStream 协议处理漂移 | 只实现 13 个事件子集；类型复用 `blocks.tsx` 的 `Block`；不抽公共层（避免动 3397 行单体），接受受控重复 |
| `macos-private-api` + 自签名校验 | `make app` 已含签名校验流程；transparency 是 private-api 的常规门槛，dev 模式先验 |
| settings.json 双写冲突 | Rust 侧一律不碰 settings.json；几何独立 floating.json |
| 热键被占用 | 注册失败 → tray 兜底 + 前端 notice 提示改键 |
| pin 窗口误入 main 的 CloseRequested/`ginno:notify` 路径 | on_window_event / listen_any 全部按 label 分支 |
| bypass_permissions 默认 True 导致确认交互几乎不出现 | 属现有全局行为，非本 feature 范围；内联确认按协议实现即可 |
| 跟随模式下主窗口忙 turn，pin 再 invoke | runtime 已有 busy notice（"当前有回合正在进行"），PinStream 展示为灰条提示 |

## 6. 不做（v1 范围外）

LSUIElement 纯后台模式、截屏自动隐藏、按显示器分别记忆几何、主窗口标题栏图钉按钮、
quick 会话自动清理。均已记入产品稿 v2 候选。
