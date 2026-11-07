# 会话内多标签工作台（Tabbed Workspace）产品设计

> **状态：🅿️ 已挂起（2026-11-07）** —— 方案完整但暂不实施；重启条件见 `docs/parked/README.md`。
>
> 来源：特性雷达 2026-11-07 候选 #7（参考 Open WebUI v0.13.0 Tabbed workspace）。
> 落层：`apps/web`（纯前端，不动 runtime API；仅新增少量事件桥）。
> 预估：24-40h，分期见 §8。

## 1. 问题定义

Ginno 的会话产物目前分散在四个互不相通的容器里：

| 产物 | 容器 | 入口 |
|---|---|---|
| artifact（表格/报告/图片/HTML） | 右侧 `ArtifactsPanel`（dock 内单面板） | 右栏 tab |
| 文件预览 | `SheetViewer`（聊天流覆盖层） | 消息卡片点开 |
| 内嵌浏览器 | `ModPaneHost`（mod 浮层） | 工具卡片 / 命令 |
| 后台任务输出 | tasks 页 / 浮窗 | 侧栏导航 |

用户在一次会话里产出 ≥3 个产物时（常见：调研报告 md + 数据 xlsx + 网页查证），需要在覆盖层、右栏、浮层之间反复开关切换；右侧面板一次只能显示一个 tab，长表格在 360px 侧栏里左右滚动看不全。**对话是主体验，但产出没有获得与对话同屏的、可多开的承载空间。**

## 2. 设计目标 / 非目标

**目标**
1. 聊天窗口内以 **Tab** 承载工作产物：artifact / 文件 / 浏览器 / 任务日志各占一个标签，可多开、可切换、可关闭。
2. 对话与产出**同屏**：分栏（split）、工作台最大化（max）、隐藏（hidden）三档布局，拖拽调宽。
3. **复用既有基建**：预览组件、browser mod、unread badge、事件桥范式（`ginno:*` CustomEvent）不重写，只换容器。
4. peek 与 open 双层交互：Sheet 保留为轻量预览，工作台承载深度浏览。

**非目标**
- 不做多会话共享工作台（tab 集合 per-session）。
- 不做工作台内再嵌套分屏（一个 tab 一个产物，v1 不做 tab 内 split）。
- 不替换 Pin（pin=常驻小部件，tab=页面级浏览，定位互补，两者共存）。
- 不动 runtime：无新 API，纯 web 层重构 + 事件桥。

## 3. 信息架构与布局

### 3.1 三档布局（WorkspaceLayout）

```
┌─────────┬──────────────────────┬───────────────────┐
│ sidebar │       chat           │  workspace        │
│         │                      │ ┌───────────────┐ │
│         │                      │ │ ◧tabs strip   │ │
│         │                      │ ├───────────────┤ │
│         │                      │ │  active pane  │ │
│         │                      │ │               │ │
│         │  (Cmd+\ 循环)        │ └───────────────┘ │
└─────────┴──────────────────────┴───┬───────────────┘
                          拖拽分隔条调宽
```

- **split**（默认）：chat + workspace 左右分栏，分隔条可拖（min 280px each，记忆宽度到 localStorage）。
- **max**：workspace 占满主区，chat 压缩成顶部迷你条（最近一条用户消息 + 输入框单行），随时切回。
- **hidden**：workspace 折叠为右侧 6px 停靠条——**完全复用现有 `RightDock` 折叠条交互**（hover 滑出 dock pill、未读紫点），点击展开即回到 split。

### 3.2 Tab strip

```
◧ [📊 trending-11-07.xlsx •] [📄 竞品调研.md •] [🌐 api.github.com] [▶ bg:pytest ×]  ＋
```

- 每个 tab：类型图标 + 标题（产物名/域名/任务名）+ 未读点 `•`（有新内容且非当前）+ 关闭 ×。
- tab 可拖拽排序；双击标题 pin（pinned tab 不可被"关闭其他"清掉）。
- strip 右端 `＋`：打开产物选择器（复用 ArtifactsPanel 列表数据源）。
- 溢出：>8 个 tab 横向滚动 + `≫` 溢出菜单（参照浏览器 tab 交互）。

### 3.3 Tab 类型（v1 四类，注册表可扩展）

| kind | 图标 | 内容来源 | 复用组件 |
|---|---|---|---|
| `artifact` | 📊/📄/🖼 | artifact id → API 预览 | `ArtifactsPanel` 预览部分抽出为 `ArtifactView` |
| `file` | 📄 | file id → `/api/files/preview` | `SheetViewer` 内容体抽出为 `FilePreviewView`（保留分页） |
| `browser` | 🌐 | browser mod 实例 | `ModPaneHost` 渲染目标改为 tab pane（旧浮层保留为设置开关） |
| `task` | ▶ | bg task id → 输出流 tail | tasks 页日志组件 + WS `task.output` 帧 |

预留（v2 注册即可，不动框架）：`workflow`（运行视图）、`diff`（code panel）、`subagent`。

### 3.4 状态模型

```ts
// store 新增（zustand，与 RightTab 同层）
workspace: {
  layout: 'hidden' | 'split' | 'max',
  width: number,                    // split 下 workspace 宽度，localStorage 记忆
  tabs: WorkspaceTab[],             // 有序
  activeId: string | null,
}
type WorkspaceTab = {
  id: string;                       // 稳定 id：`${kind}:${src}`
  kind: 'artifact' | 'file' | 'browser' | 'task';
  src: string;                      // artifact id / file id / url / task id
  title: string; icon: string;
  pinned?: boolean; unread?: boolean;
  status?: 'live' | 'done' | 'error';  // browser 加载中 / task 运行态
}
```

- **去重**：同 `kind:src` 的 open 请求不新开 tab，激活已有 tab 并清 unread（幂等）。
- **per-session**：tab 集合挂 session（切会话各自恢复），持久化到 localStorage `workspace:<session_id>`；会话删除时清理。
- **上限**：单会话 12 个 tab，超出时最老的非 pinned tab 被替换（提示 toast）。

## 4. 交互流程

### 4.1 打开（open 的四个入口）

1. **聊天流产物卡片** → 新增「⧉ 在工作台打开」按钮（与现有下载/预览并排）。
2. **SheetViewer** → Sheet 顶栏新增「⤢ 工作台」按钮：peek 与 open 双层——Sheet 看概览，工作台看全量。点开后 Sheet 自动收起。
3. **ArtifactsPanel** → 行右键 / hover 按钮「在工作台打开」；双击行 = open（单击仍为选中侧栏预览，不改既有习惯）。
4. **工具卡片**（browser 工具、bg 任务）→ 卡片角标「在工作台打开」，把 mod 浮层/任务日志转为 tab。

统一走事件桥（与 `FILE_MISSING_EVENT` 同范式，零耦合）：

```ts
window.dispatchEvent(new CustomEvent('ginno:workspace-open',
  { detail: { kind: 'artifact', src: id, title, icon } }));
```

`WorkspaceHost` 监听并写入 store。反向：tab 内容发现产物失效（404）→ 复用 `ginno:file-missing` 标记 tab 标题「已失效」。

### 4.2 未读与自动提议

- tab 非激活时产物有新内容（任务新输出、浏览器导航、artifact 更新）→ 打未读点；RightDock 折叠条紫点机制同步复用。
- **自动开 tab 仅限显式产物事件**（`artifact.created`、任务启动、browser 打开），且设置项 `workspace.autoOpen` 默认开；工具中间结果不自动开，避免 tab 爆炸。

### 4.3 键盘

| 键 | 行为 |
|---|---|
| `Cmd/Ctrl+1..9` | 切换第 n 个 tab |
| `Cmd/Ctrl+W` | 关当前 tab（全关时 layout→hidden） |
| `Cmd/Ctrl+\` | 循环 hidden→split→max |
| `Cmd/Ctrl+Shift+]` / `[` | 下/上一个 tab |

### 4.4 与聊天协同

- max 布局下迷你条仍可输入（回车发送，产出自动开/更新 tab + 未读点）——"边看报告边追问"。
- 工作台激活时聊天新回复不打断当前 tab，仅更新迷你条摘要 + tab strip 左端会话未读点。

## 5. 组件改造清单

| 文件 | 改动 |
|---|---|
| `components/right/workspace/WorkspaceHost.tsx` **新增** | 工作台容器：布局切换、分隔条、折叠停靠 |
| `components/right/workspace/TabStrip.tsx` **新增** | tab 条：拖拽排序、pin、关闭、溢出菜单、未读点 |
| `components/right/workspace/panes/*.tsx` **新增** | 四类 pane 容器（ArtifactPane/FilePane/BrowserPane/TaskPane），薄壳 |
| `components/right/ArtifactsPanel.tsx` | 预览部分抽成 `ArtifactView` 供 pane 复用；列表加 open 入口（-约 300 行逻辑移动） |
| `components/chat/SheetViewer.tsx` | 内容体抽 `FilePreviewView`；顶栏加「工作台」按钮 |
| `components/right/ModPaneHost.tsx` | 增加 `target: 'floating' \| 'workspace-tab'` 渲染目标 |
| `lib/store.ts` | `workspace` slice + persist per-session |
| `lib/workspace.ts` **新增** | `WorkspaceTab` 类型、`openTab()` 帮助函数、事件常量 |
| 聊天流卡片（blocks/toolBlocks 等） | 加 open 按钮（事件桥一行调用） |

## 6. 边界与容错

- 产物被删：pane 内展示既有「已失效」态（复用 `isMissingPreviewError`），tab 不自动关。
- 浏览器 mod 崩溃：pane 显示错误 + 重载按钮，不拖垮工作台。
- 拖拽宽度越界：clamp 280px~70% 主区。
- 移动端/narrow：workspace 自动退化 hidden（v1 不做移动布局）。
- 性能：非激活 pane 不卸载（保滚动位置/浏览器状态），但用 `content-visibility` 降渲染成本。

## 7. 验收口径

- 一次会话产出 ≥3 产物：从"第 3 个产物开始看"到"三产物任意切换"的操作步数 ≤1 次/切换（现状 2-3 次）。
- xlsx（≥8 列）在 split 半屏下可见列数 ≥ 侧栏 2 倍。
- SheetViewer 用户无行为回归（默认路径不变，open 是增量按钮）。
- 全程无 runtime 改动；vitest 覆盖 store slice 与 tab 幂等/上限逻辑。

## 8. 分期

| 期 | 内容 | 量 |
|---|---|---|
| P0 | WorkspaceHost + TabStrip + artifact/file 两类 pane + 事件桥 + split/hidden 布局 + Sheet「工作台」按钮 | ~10h |
| P1 | browser/task pane + 未读点 + 自动开 tab 设置 + max 布局迷你条 | ~10h |
| P2 | tab 拖拽排序/pin + 快捷键 + per-session 持久化 + 溢出菜单 | ~8h |

交互原型：`docs/parked/tabbed-workspace.html`（P0+P1 范围的可点击 demo，与本方案一同挂起）。
