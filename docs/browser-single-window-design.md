# Browser Single-Window Design（单 CEF 窗口 + Session 绑定 + Tab 上移）

> 状态：**设计稿（待实施）**。取代 `browser-embed-design.md` 的「内嵌子视图 / 多窗口 dock」路线。
>
> 背景：CEF 151（Chrome runtime）的模型是 **一个 browser 实例 = 一个 OS 窗口**，且 macOS 上
> `parent_view` 内嵌被上游禁用（`chrome_child_window.cc` `GetParentWidget()` 在 mac 返回空，
> 见 cef issue #3294）。继续追求「内嵌 / 多窗口 dock」成本高、不稳定。
>
> 产品决策（用户确认）：
> 1. **可见浏览器窗口恒为 1**；但**每个 session 持有一个自己的（隐藏）CEF browser**，
>    切换 session = 显示它的 browser、隐藏其它，**不重载、不丢页面状态**（方案 B）；
> 2. **Tab 仍是逻辑对象**（session 内切 tab = 导航该 session 的 browser，可重载）；
> 3. **Tab / Space 的逻辑管理上移到 runtime + Web UI，Tauri/C 不再做 tab/dock/内嵌**。

---

## 0. TL;DR

- **一个可见窗口 + 每 session 一个隐藏 browser**。活跃 session 的 browser 显示在唯一可见窗口；
  其余 session 的 browser 隐藏但**保活**（DOM/SPA/滚动状态都在），切回瞬时、不重载。
- **Session 持有浏览器上下文**：`session.browser = { tabs, active_tab_id, owner, browser_id }`。
- **Tab = 逻辑页面记录**（session 内切 tab = 导航，可重载）；**Session = 活 browser**（切换不重载）。
- **handoff / 锁定 / 中断** 三态状态机原样保留，key 从 space 换成 `session_id`。
- **Tauri/C 职责**：启动 CEF 宿主、写 `cef-cdp.json`、按 runtime 指令 **show/hide/摆位/标题** 各 browser。
  不管 tab、不管 session 逻辑、不管 CDP。
- **Runtime owns 一切逻辑**：browser 生命周期（LRU 保活）、tab、导航、handoff、CDP。
- **Web UI**：tab 条绑当前 session；会话列表切 session（= 切换可见 browser）。
- **资源有界**：隐藏 browser 按 LRU 保留上限（如 3），超出回收（该 session 下次切回重载）。

---

## 1. 概念模型

| 概念 | 语义 | 数量 | 存储 | 谁管 |
|---|---|---|---|---|
| **Session** | 一个对话/任务，持有一个**活 browser** | N | sessions + `session.browser` | runtime |
| **Tab** | session 内一个页面（逻辑记录） | 1..N / session | `session.browser.tabs` | runtime + Web UI |
| **CEF browser** | 每 session 一个；**仅活跃者可见**，其余隐藏保活 | ≤ LRU 上限 | CEF 宿主 | runtime 生命周期 + Tauri show/hide |
| **可见窗口** | 承载活跃 session 的 browser | **恒为 1** | CEF 宿主 | Tauri/C |
| **owner** | `agent` / `agentDelegatedToUser` / `user` | 1 / session | `session.browser.owner` | runtime |

**关系**：Session 1:1 活 browser；browser 1:N tab（tab 逻辑导航）；可见窗口恒 1（显示活跃 browser）。
**Space 概念退役**：原「Space」= 现「session 的浏览器上下文」；`spaces.json` 注册表删除。

---

## 2. 分层职责

| 职责 | Tauri/C | Runtime(Python) | Web UI | CEF |
|---|---|---|---|---|
| 启动 CEF 宿主 / 写 cef-cdp.json | ✅ | 读 | — | 承载 |
| 创建/回收每 session browser + show/hide/标题/摆位 | ✅ | 触发（LRU 生命周期） | — | 承载 |
| tab 列表 / 开 / 切 / 关 | ❌ | ✅ | ✅ tab 条 | ❌ |
| 导航 active tab | ❌ | ✅ CDP | 地址栏 | 执行 |
| session 切换 → 切窗口内容 | ❌ | ✅ | 会话列表 | 执行 |
| owner / handoff / 交还 | ❌ | ✅ | ✅ 提示+按钮 | ❌ |
| 登录态 / cookies | ❌ | profile | 导入向导 | 存储 |

> 「tab 管理不在 Tauri」成立：Tauri 只有窗口生命周期；tab/session 全在 runtime+Web UI。

---

## 3. Tab 管理（runtime + Web UI）

### 3.1 数据结构
```python
# 持久化在 session 记录里（或 ~/.ginno/browser/sessions.json）
session.browser = {
  "owner": "agent",                 # 三态之一
  "tabs": [ {"id": "t1", "url": "...", "title": "...", "updated": 1786...}, ... ],
  "active_tab_id": "t1",
}
```

### 3.2 操作（全部 = 改 runtime 状态 + 导航单窗口）
- **开 tab**：`tabs.push({id,url})`；`active_tab_id=id`；CDP `Page.navigate(url)`。
- **切 tab**：`active_tab_id=id`；CDP 导航到该 tab url。（非 active tabs 不保活 DOM，切回重载。）
- **关 tab**：移除；若关的是 active，切相邻并导航；最后一个 tab 关闭 → 保留一个 about:blank。
- **导航（地址栏）**：更新 active tab 的 url/title；CDP 导航。

### 3.3 Session 切换（方案 B：show/hide，不重载）
1. 记当前 session 为隐藏（其 browser 保持存活，DOM/状态不丢）。
2. 取目标 session 的 browser：
   - 已保活 → **直接 show**（orderFront），隐藏其它；**无导航、无重载**。
   - 未保活（新 session 或被 LRU 回收）→ 创建/重建 browser，导航到其 `active_tab` url。
3. `browser_focus = target`；可见窗口标题改 `Ginno · <session title>`。
4. Web UI tab 条重绘为 target 的 tabs（从持久化读）。

### 3.4 Session/Tab 与 CDP target 的对应
- **Session = 一个活 CDP browser/target**（隐藏或可见）。这是「切 session 不重载」的载体。
- **Tab = 逻辑记录**，session 内切 tab = 对该 session 的 browser `Page.navigate`（**可重载**）。
  （不为每 tab 开 browser，避免 target 数量爆炸；只有 session 维度保活。）
- 因此：**切 session 不重载；切 tab 重载**。两者代价不同，符合「session 是工作上下文、tab 是页签」的直觉。

### 3.5 多 Session 切换：窗口表现（逐帧）
全局唯一窗口，位置/大小恒定，切换只换内容，不跳窗。

| 时刻 | Web UI | 可见窗口 |
|---|---|---|
| t0 点 session B | 会话列表高亮 B；tab 条立即重绘为 B 的 tabs；地址栏=B active url | 标题立即改 `Ginno · <B title>` |
| t0+ε | — | **show B 的活 browser、hide A**；B 页面瞬时出现（已保活，无加载） |
| 若 B 未保活 | tab 条 active 项 spinner | 新建/重建 browser + 导航（此时有加载） |

- **切回 A**：A 的 browser 一直保活 → show 即恢复原页面（滚动/SPA 状态都在），**不重载**。
- **切到无上下文的 session**：`tabs=[about:blank]`，新建 browser 显示空白页。
- **切到 delegated session**：show 该 browser + Web UI 交还 bar；agent 仍 paused。
- **快速连切**：show/hide 为原生操作，无需 debounce；仅「新建/导航」路径 debounce ~150ms。
- **窗口被最小化/遮挡**：切换不强制前置（除非用户开启「切换时聚焦」）。

### 3.6 浏览器焦点（browser_focus）与抢占策略
单窗口 ⇒ 同一刻只有一个 session 能用浏览器。引入全局 `browser_focus: session_id`。

- **用户切 session** → `focus=B`。若 A 的 agent 正 running 且持有浏览器：
  - A 的后续 browser 调用 → **interrupt 暂停**（`kind:"browser_preempted"`），UI 提示
    「浏览器已切到 B；切回 A 或点恢复继续」。复用 handoff 的 interrupt/resume 机制。
- **agent 发 browser op 且其 session≠focus** → **steal focus**：`focus=agent session`，
  窗口切过去 + toast「Agent 正在使用浏览器（<session>）」。
- **防抖**：steal 后 2s 内用户切回不触发 agent 再次 steal（否则 agent 暂停），避免窗口抖动。

> 该策略保证「agent 不会在用户看不见的页面上操作」，且用户随时可抢回窗口。

### 3.7 窗口生命周期边界
- 关闭伴随窗口（点 X）= **hide**，不销毁；Web UI「浏览器」按钮可再 show；profile 保留。
- 启动时不自动开窗；首次 browser 操作或点「浏览器」时创建/显示。
- 崩溃/退出重启：CEF 重置；按 `browser_focus` session 的 active tab 恢复导航。

---

## 4. Handoff / 锁定 / 中断（沿用，key 换 session）

状态机不变（`ownership.py` 三态 + `AGENT_LOCKED`），仅把 `name`(space) 换成 `session_id`：

- **锁定**：agent 侧操作（eval/cdp/click/navigate）先过 `_guard(session_id)`：
  `owner in AGENT_LOCKED → raise BrowserLocked`。delegated 时 runtime 不转发 agent 命令。
- **中断**：`hand_off(session_id)` 设 `owner=delegated` → `raise BrowserHandoff`
  → 工具层转 LangGraph `interrupt({kind:"browser_handoff", session_id,url,reason})` → turn 暂停。
- **恢复**：人点「交还」→ WS `permission_response/browser_resume` → `take_over(session_id)`（owner=agent）→ `Command(resume=…)`。
- **隐式抢占**：`dispatch_input`：owner==agent 且真实 click/key/wheel → 翻 delegated，返回 `handoff:true`；hover 不抢；take_over 后 grace 窗口防残留输入抢回。
- **浏览器被切走（browser_preempted）**：单窗口下，agent 正用浏览器时用户切走（§3.6），该 agent 的后续 browser 调用以
  `interrupt({kind:"browser_preempted", session_id})` 暂停；用户切回或点「恢复」→ resume。与 handoff 共用 resume 通道。
- **waiting_human(session_id)**、`browser.handoff`/`browser.space`/`browser.focus` 广播保留。

---

## 5. 窗口管理（Tauri/C，最小）

- 启动时**不**自动开 browser；首次 browser 操作 / 点「浏览器」时为活跃 session `create_browser`，写 `cef-cdp.json`。
- **每 session 一个 browser**（runtime 按需创建/回收）；同一刻仅活跃者可见，其余 `hide` 保活。
  `Target.createTarget` 不用于开 tab（tab 是逻辑导航）。
- Rust 暴露（供 runtime 触发）：`create_browser(id,url)`、`show(id)/hide(id)`、`set_title(s)`、`set_frame(rect)`、`focus()`。
- 可见窗口的 X = hide 活跃 browser（§3.7），不销毁。
- **151 注意**：chrome runtime 每个 browser 是一个 OS 窗口，隐藏用 orderOut/hide；
  创建瞬间可能闪一下，创建后立即 hide 非活跃者（可接受）。
- 删除：`ginno_cef.m` dock_*（抬窗口内嵌）、`cef-cmd.json` show 通道、NSView 池、`browser_tile.rs` hole/`emitBrowserTile`/`ginno-hole`、screencast `<img>`。

---

### 5.5 可见性联动（最终形态：无右侧 pane，有手动开关）
- **右侧内嵌 pane 已整体移除**：BrowserPane 不再挂载；无 tile/screencast/输入转发/分栏；左菜单恒显。
  浏览器就是独立伴随窗口（自带 Chrome tab 条/地址栏），主窗口不再画页面。
- **可见性 = `onWorkspace && activeSession && browserOpen`**，由一个 effect 驱动：
  - 满足 → `activate_session`（C `show`：只显示该 session 窗口、隐藏其它 ⇒ 切 session 隐藏不相关 tab/窗口）；
  - 非 workspace 或 `browserOpen=false` → `hide`（C `hide_all`）。
- **手动开关**：TopBar「浏览器」按钮 / `⌘.` 翻转 `browserOpen`（默认开）；非 workspace 置灰。
  `hide_all` 依赖 C 侧 `is_cef_window` 识别伴随窗口（非主窗口、内容尺寸即视为伴随窗口）。
- **activate 为 show-only**：不建 tab、不导航，聚焦/开关不会把页面打回 about:blank。
- **不强制尺寸**：伴随窗口由用户自由缩放；移除 `--window-size` / `Emulation.setDeviceMetricsOverride` / `set_viewport/set_bounds` 强制逻辑。
- handoff 提示走聊天 HandoffCard / 工作流卡片。
- C 侧 `rep_read_cmd` 支持 `{"op":"show","slot"}` 与 `{"op":"hide_all"}`；`dock_layout` 只控显隐、`hide_all` 优先，不挖洞不 pin。

---

## 6. API（runtime REST/WS）

按 session 的 tab 操作（替代原 spaces/* 与全局 tabs/*）：
- `GET  /api/browser/state` → `{ active_session_id, owner, tabs, active_tab_id, engine }`
- `POST /api/browser/tabs` `{url}` → 开 tab（当前 session）
- `POST /api/browser/tabs/{id}/activate`
- `POST /api/browser/tabs/{id}/close`
- `POST /api/browser/navigate` `{url}` → 导航 active tab
- `POST /api/browser/session/{id}/activate` → 切 session：设 `browser_focus`、保存/恢复 tabs、导航窗口
- `GET  /api/browser/focus` → 当前 `browser_focus` + 是否有 agent 持有
- handoff：`browser.handoff` / `browser_resume`（带 session_id）
- preempt：`browser_preempted` interrupt 的 resume 复用 `browser_resume`
- WS 广播：`browser.focus`（focus 变化）、`browser.tabs`（tabs 变化）、`browser.handoff`
- 阻塞 CDP 的端点保持 **sync `def`**（线程池），`spawn_bg` 线程安全广播。

---

## 7. 前端

- **tab 条**绑当前 session 的 `tabs`（开/切/关 + 地址栏），不再绑 space。
- **会话列表**（左侧已有）切 session → 调 `session/{id}/activate` → CEF 窗口内容切换。
- 删除：Space chips、内嵌 pane/hole、screencast 显示、`BrowserPane` 内嵌部分。
- handoff 提示 / 交还按钮保留（session 级）。

---

## 8. 移除 / 保留清单

**移除**：`spaces.py` 注册表、dock（C）、show 通道、NSView 池、hole/`ginno-hole`/`emitBrowserTile`、screencast 内嵌、BrowserPane Space chips、`use_or_create`/`complete`/`claim_user` 的 space 语义。
**保留**：`ownership.py` 三态、`BrowserHandoff`/`BrowserLocked`、graph interrupt、`dispatch_input`+grace、CDP engine（单 target 导航）、profile/登录导入、`waiting_human`。

---

## 9. 边界与限制

- **切 tab 重载**（tab 是逻辑导航）；**切 session 不重载**（session browser 保活）。
- **隐藏 browser 按 LRU 保活，上限默认 3**：超出回收最久未用者（其 session 下次切回重建+重载）。
  内存占用 ≈ 活跃页面数，有界。
- 登录态单 profile 共享。
- 可见窗口恒 1，人不能并排看两个 session 的页面（产品接受）。
- 151 隐藏 browser 仍是 OS 窗口（orderOut），任务栏/Dock 不显示额外图标（helper 无独立图标）。

---

## 10. 验证计划

1. 单测：session tabs 开/切/关；session 切换恢复 tabs；handoff 三态 + interrupt/resume；`waiting_human`。
2. 手动：启动→唯一 CEF 窗口；开 2 tab 切换；切 session 窗口内容切换；handoff（login-wall）→ 交还恢复；关闭多余窗口不存在。
3. 回归：`pytest -m "unit or api" -k browser`。
