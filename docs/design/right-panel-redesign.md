# 右栏改版方案（right-panel-redesign）

> 状态：待评审
> 日期：2026-08-08
> 关联代码：`apps/web/src/components/right/`、`components/shell/AppShell.tsx`、`lib/store.tsx`

## 1. 背景与目标

右栏目前有 4 个面板（TODO / Workflow / Artifacts / Memory），固定 380px、常驻显示、无法收起。
随着聊天区承载越来越多内容（表格预览、长输出），右栏常驻挤占主区宽度；同时 Artifacts
是使用频率最高的面板，却排在 tab 第三位。

本次目标：

1. **Tab 重排**：Artifacts / TODO / Workflow / Memory，默认 tab 改为 Artifacts。
2. **右栏可收起/展开**：收起后聊天区全宽，右缘提供悬停唤出的 Dock；支持快捷键。
3. **宽度可拖拽**：280–560px，持久化。
4. 收起期间的新 Artifact 通知不打断用户，用角标提示。

非目标（本期不做）：

- 左栏收起/展开。
- 面板内容本身的功能改动（各 Panel 内部不动）。
- tab 持久化（`rightTab` 维持内存态，同现状）。

## 2. 现状

| 项 | 现状 |
|---|---|
| 宽度 | 固定 `w-[380px]`，不可调 |
| Tab 顺序 | TODO / Workflow / Artifacts / Memory，默认 `todo` |
| 可见性 | 工作区路由（`/`）常驻显示，无收起入口 |
| 自动跟随 | 会话产生新 Artifact 时自动切到 Artifacts tab 并高亮 2.5s（docs §7.6）；手动点过其他 tab 后 `artifactsFollow` 关闭，不再抢焦点 |
| 状态归属 | `rightTab` / `artifactsFollow` 在 store（`GinnoProvider`），无 localStorage 持久化 |
| 快捷键 | 全局快捷键目前不存在（仅面板内部局部 keydown） |

## 3. 方案设计

### 3.1 Tab 顺序与默认值

- 顺序：**Artifacts → TODO → Workflow → Memory**。
- 首次打开的默认 tab：`artifacts`（与新顺序首位一致）。
- tab 文案旁增加图标，与 Dock 图标一致：

  | tab | 图标（lucide） |
  |---|---|
  | Artifacts | `FileBox` |
  | TODO | `ListTodo` |
  | Workflow | `Zap` |
  | Memory | `Brain` |

- 既有自动跟随逻辑不变：新 Artifact 到达 → 后台 `rightTab` 切为 `artifacts`
  （这样无论面板开合，下次展开都落在 Artifacts 上并触发高亮）。

### 3.2 收起 / 展开交互（核心）

只有工作区路由（`/`）存在右栏；settings / kb / workflows 路由不渲染右栏与 Dock。

#### 展开态（默认，`open = true`）

- 布局同现状，宽度改为 store 中的 `rightPanelWidth`（默认 380）。
- tab 栏右侧新增**收起按钮**（`PanelRightClose` 图标，aria-label「收起面板」）。
- 面板左缘有 4px 拖拽热区（见 3.4）。

#### 收起态（`open = false`）

右栏整体卸载（`display:none` 级别的隐藏，不保留占位），聊天区获得全宽。右缘提供两级提示：

1. **静息提示条**：右缘一条低对比度竖线（约 2px、高 32px、垂直居中），
   有未读 Artifact 时竖线上方显示 violet 小圆点。**点击可直接展开**（触屏兜底）。
2. **悬停 Dock**：鼠标进入右缘 6px 热区后，滑出 48px 宽的竖排 Dock
   （150ms ease-out），内容自上而下：4 个面板图标（顺序同 tab），图标可带角标。
   - 点击任一图标：展开面板并直达该 tab，清除该 tab 角标。
   - 鼠标离开 Dock 250ms 后收回（防抖，避免横向扫过时闪烁）。
   - Dock 的 z-index 低于 SheetViewer / Modal，避免覆盖全屏预览。

> 选择「隐藏 + 边缘悬停」而非「常驻图标栏」的原因（评审已确认）：
> 收起时聊天空间最大化；图标栏只在需要时出现，角标仍保留通知能力。
> 代价是发现性，用「静息提示条可点击 + 快捷键 ⌘\ + 首次使用提示」补偿。

#### 首次引导

首次进入收起能力时（`ginno-right-panel` key 不存在），维持**展开**默认态，不做引导弹窗；
收起按钮与静息提示条即教学。若后续数据显示用户不知道能收起，再补一次性 tooltip。

### 3.3 快捷键

- `⌘\`（macOS）/ `Ctrl+\`（Win/Linux）：切换右栏开合。注册在 AppShell，
  仅工作区路由生效。与现有局部 keydown（ArtifactsPanel 预览、代码块）无冲突。
- 快捷键展开时恢复**上一次选中的 tab**（`rightTab` 本就在 store 中保持）。

### 3.4 宽度拖拽

- 面板左缘 4px 热区，`cursor: col-resize`，hover 时显示 1px violet 指示线。
- 拖拽实时改宽，clamp 到 [280, 560]；拖拽中给 body 加 `select-none`
  防止误选中文本。
- **双击热区重置为 380**。
- 宽度持久化（见 3.5）。窗口变窄不做自动收起（留作后续观察项）。

### 3.5 状态与持久化

store 新增：

```ts
rightPanelOpen: boolean;                    // 默认 true（升级用户不突变）
setRightPanelOpen(open: boolean): void;
rightPanelWidth: number;                    // 默认 380，clamp [280, 560]
setRightPanelWidth(w: number): void;
panelBadge: Partial<Record<RightTab, number>>;  // 收起期间的未读角标
clearPanelBadge(tab?: RightTab): void;
```

持久化：单个 key `ginno-right-panel`，JSON `{"open":true,"width":380}`，
读写 try/catch（与 `ginno-theme` 同一模式）。tab 与角标不持久化。

### 3.6 收起时的新 Artifact 通知（角标）

复用 `reloadArtifacts` 里既有的「新行检测」（§7.6 同一段逻辑）：

- **面板展开**：行为不变 —— 切 tab + 行高亮 2.5s。
- **面板收起**：不展开面板，`panelBadge.artifacts += 新到达数量`（仅统计当前
  active session 的行），静息提示条出现 violet 圆点，Dock 的 Artifacts 图标显示数字角标。
- 清除时机：面板展开（任意方式）即清空全部角标；单独点 Dock 某图标只清该 tab。
- v1 仅 Artifacts 有角标；TODO / Workflow / Memory 预留字段，暂不产生角标。
  - 实现偏差（2026-08-08）：Dock 图标额外镜像了既有的 Workflow tab 角标
    （运行中/新失败，work item E），与展开态 tab 栏保持一致；该角标不进
    `panelBadge`（它是常驻派生态，不是"收起期间的未读"）。

### 3.7 视觉细节

- 收起按钮、Dock 图标沿用现有 token：`text-muted hover:text-txt`、`bg-card / bg-card2`、
  圆角 `rounded-lg`，边框 `border-line`。
- 角标：violet 底白字，最小 16px 圆形，数字 >99 显示「99+」。
- Dock 出现/收回用 `transform: translateX` + 150ms 过渡，不动画宽度以免推动布局
  （Dock 是绝对定位覆盖在聊天区右缘之上，收起态不占布局空间）。

## 4. 边界情况

| 场景 | 处理 |
|---|---|
| 拖拽中鼠标移出窗口 | `pointerup` 监听挂 window，正常收尾 |
| SheetViewer 全屏预览打开 | Dock z-index 低于预览层；快捷键仍可用 |
| 聊天区右缘文本选择 | 热区仅 6px 且需 hover 触发，误触面极小；250ms 收回防抖 |
| 触屏设备（无 hover） | 静息提示条可直接点击展开；快捷键可用 |
| sidecar 未连接 | 角标依赖 `reloadArtifacts`，sidecar 挂了不产生角标，无副作用 |
| 设置/KB/Workflows 路由 | 不渲染右栏与 Dock；回到 `/` 恢复 `rightPanelOpen` 状态 |

## 5. 实现拆分（自底向上）

1. `lib/store.tsx`：新增开合/宽度/角标状态 + localStorage；默认 tab 改 `artifacts`；
   `reloadArtifacts` 的新行分支按开合状态分流（展开→高亮，收起→角标）。
2. `components/right/RightPanel.tsx`：TABS 重排 + 图标；tab 栏加收起按钮；
   左缘拖拽改宽（含双击重置）。
3. `components/right/RightDock.tsx`（新增）：静息提示条 + 悬停 Dock + 角标。
4. `components/shell/AppShell.tsx`：按 `rightPanelOpen` 渲染 RightPanel / RightDock；
   注册 `⌘\` / `Ctrl+\`。
5. 文档：本文档落地；`docs/architecture.md` 第 471 行附近组件清单顺带更新。

## 6. 验收清单

- [ ] tab 顺序 Artifacts / TODO / Workflow / Memory，首开落在 Artifacts
- [ ] 收起按钮、静息提示条点击、Dock 图标点击、`⌘\` 四种方式均可开合
- [ ] 收起后聊天区全宽；Dock 悬停出现、250ms 延迟收回
- [ ] 宽度拖拽 280–560 clamp、双击重置 380、刷新后保持
- [ ] 收起时新 Artifact 到达：不弹面板，静息条圆点 + Dock 数字角标；展开后角标清空且行高亮仍在
- [ ] 展开时手动切换 tab 后，自动跟随不再抢焦点（既有行为不回退）
- [ ] settings / kb / workflows 路由无右栏、无 Dock、无热区
- [ ] `make app` 构建通过
