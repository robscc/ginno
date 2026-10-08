# 代码面板设计（在会话工作区内当轻量 IDE 用）

状态：待评审 · 勘察日期 2026-09-29 · 体例沿用 `docs/*-design.md`

---

## 0. TL;DR

**要做什么**：给会话加一个「代码」右栏 tab——多根文件树 + Monaco 编辑器。能读、能改、能看 git 改动、能搜、能新建/重命名/删除；并且和 agent 的改动合流（agent 动过的文件在树里高亮，从聊天直接跳到具体行）。

**不是什么**：不是预览器。**不复用 `SheetViewer` 那条"文档预览"通路**——那是给表格/PDF/图片的，按 `fileId` 寻址；代码面板按**路径**寻址，是第二条独立通路，两者在 §3.4 收口。

**四个已定决策**（详见 §5）：

| # | 决策 | 选择 |
|---|---|---|
| D1 | 工作区树的根模型 | **多根** = `primary_folder`（若有）+ 其余挂载夹 + 会话工作区 |
| D2 | 布局形态 | **右栏宽版**，宽度上限从 560 放到 ~50vw |
| D3 | 编辑器 | **Monaco，可编辑**（非只读） |
| D4 | v1 范围 | 四项全要：agent 改动高亮+跳转 · 项目内搜索 · git 标记+diff · 文件操作 |

**最大代价（已明说并接受）**：D2 是"改动最小"那档，代价是"树 + 编辑器 + diff"三样挤在一个侧栏里。解法是**面板内双态**——默认「文件优先」把树收成一条面包屑条（⌘B 展开），面板 ≥600px 时自动切「并排」。见 §3.4。

**最高风险是写路径**。你我会和 agent 同时改同一批文件。设计用**一个版本令牌 + 一个变更事件**同时解决三件事：agent 改动高亮、外部修改检测、保存冲突（§4.6）。这是整个设计里最值得先做对的部分。

**两个未知必须先验证**（§7）：Monaco 在 Next 14 静态导出 + Tauri 下的 worker 打包（1–2h spike，失败可降级）；`grep_files` 是否 ripgrep 支撑（纯 Python 则大仓库内容搜索要明说预算）。

---

## 1. 参考实现分析（事实基础：deepseek-harness 的 workspace files）

dsh 把这做成了一个一等子系统，不是某个面板的附属功能。这部分是**事实**，不是提议。

### 1.1 它的结构

| 关注点 | 位置 |
|---|---|
| Host 服务 + 线上类型 | `packages/api/workspace-files/`（`WorkspaceFiles` 类，5 个 `@Remote` 方法） |
| 文件树 UI（tab kind `files`） | `packages/client/ui-sidebar-files/`（`DirectoryNode`、`createFilesStore`、`filesFace`） |
| 文档预览 UI（tab kind `text`） | `packages/client/ui-sidebar-documentpreview/`（按扩展名注册的渲染器表） |
| FS 能力缝 | `packages/fs/`（`dsh-fs` / `dsh-fs-local` / `dsh-fs-sandbox`） |
| `@file` 提及补全 | `packages/context/file-reference-local/`（`WorkspaceFileSearch`） |
| 轮末改动卡片 + diff 审查 | `packages/deliverables/workspace-changes/`、`packages/client/ui-deliverables/` |

五个远程方法构成全部后端：`read` / `readBytes` / `stat` / `list` / `changes`（stream）。**没有写**。

### 1.2 跨产品共识（直接可抄）

1. **元数据流与内容读取分开。** `stat` 只回 `{absolutePath, version, bytes}`，内容是按需分页拉的。任意大小的文件开在常量内存里，代价是消费方自己拼页（没有 seek）。
2. **不透明 `version` 令牌。** UI 只做相等比较，从不解析时间戳。外部改动会改变它，所以"内容相同但重写了一遍"也能检测到。
3. **懒加载、一层一次。** 展开才拉，收起就释放。**watcher 是 per-node、按需、从不递归**。
4. **变更先公告，由人决定是否应用。** 不把 agent 正在写的文件从读者眼皮底下换掉。
5. **按地址解耦，不按渲染器名。** 树只产出 `dsh-resource://file/...` 地址；tab 注册表里有谁认领就谁渲染（`fallback` 兜底，更长的后缀优先）。加一个渲染器不需要动树。
6. **排序在读者侧**：目录优先，然后 `Intl.Collator(numeric: true, sensitivity: 'base')`（`file2` < `file10`）。
7. **二进制不走 base64**，用 multipart 分片传输。
8. **读取无审批。** 它明确没有 allow/deny 列表、没有审批提示。

### 1.3 已证实的坑与它的取舍（我们有意偏离的地方）

| dsh 的做法 | 问题 | 我们的决定 |
|---|---|---|
| `read`/`stat`/`readBytes` **不受根约束**（跟随 session 后端读权限，绝对路径和 `..` 外都能读） | 有多个根时"绝对路径"语义不清；围栏难以向用户解释 | **收紧**：客户端只发 `root_id` + 相对路径，服务端解析根并强制包含性。见 §4.3 |
| symlink 一律拒（即使指回根内） | 太保守，真实仓库里 symlink 很常见 | **放宽**：解引用后做包含性检查，根内放行、根外拒绝 |
| 树不隐藏任何东西（`node_modules` 也照列） | 大仓库里树直接不可用 | **折中**：默认折叠为**占位行**（`node_modules · 已隐藏 12,483 项`，可点开）。既不骗人也不用滚 1 万行。见 §3.4 |
| 树无虚拟化、无搜索，2000 项/层截断 | 单层 2000 行 DOM 有真实成本 | 渲染行上限 300 + 增量加载（§4.1） |
| 自动刷新开关**被刻意隐藏**（state 和逻辑活着但 UI 是 `<span hidden>`） | 交付了能力却没有入口 | 刷新策略明确暴露（§3.4） |
| HTML 在 `sandbox="allow-scripts"` iframe 里跑，保留网络访问 | 有被接受的攻击面；我们不需要这个能力 | **不做** HTML 沙箱渲染 |
| 它的 diff 是独立 surface（`changes-review` tab），不在文档查看器里 | 合理 | 我们也在代码面板内做 diff，但用 Monaco DiffEditor（§4.5） |

---

## 2. Ginno 现状与差距（2026-09-29 勘察）

### 2.1 关键事实：Ginno 有三个"目录"概念，dsh 只有一个

这是移植的最大分歧点，也是 D1 非定不可的原因。

| # | 概念 | 落点 | 性质 |
|---|---|---|---|
| A | **会话工作区** | `paths.session_files_dir(slug, sid)` = `~/.ginno/projects/<slug>/sessions/<sid>/`（含 `uploads/`、`results/`） | agent 产物的落点。会话 meta 字段名就叫 `workspace`；`api/sessions.py` 的 `create_session` 会**用 `session_dir` 覆盖客户端传入的 `workspace`**；`_ensure_session`（同文件 ~908 行）从 `paths.session_files_dir` 重新推导，不读 meta（为兼容旧数据） |
| B | **上下文挂载夹** | 库文件 `~/.ginno/folders.json`；`context_folders.py` | 条目形如 `{id: "f_xxxx", path, name, access: "ro"\|"rw", load_rules, added, last_used}`；`ACCESS_TIERS = ("ro","rw")`。会话通过 `context_folders: list[str]` + `primary_folder` 绑定；`resolve_session_dirs(folder_ids, primary_id) -> (dirs, primary_path)`，每个 dir 含 `{id, path, name, access, load_rules, missing}`。`primary_path` 同时是相对路径基址和 bash cwd |
| C | **自动发现的 git 项目根** | `projects.py`，`<sid>.projects.json` | `MARKERS = (".git",".claude","CLAUDE.md","AGENTS.md","GINNO.md","package.json","pyproject.toml","Cargo.toml","go.mod","pnpm-workspace.yaml")`，`MAX_DEPTH=6`，`MAX_ROOTS=5`。**只是可见性，不构成访问权**。由每次文件/命令工具触碰路径时的 `observe_path` 钩子记录 |

**结论**：树根 = B 的全部 + A +（`primary_folder` 优先）。C **不作为根**——它记录的是"agent 碰过哪些项目"，语义上是"最近活动"而非"用户授权的工作区"。可作为后续的快捷入口提示，但不进 v1。

### 2.2 现有预览是 `fileId` 键控，不是路径键控

聊天里的文件走 `files/registry.py` 的 `FileRegistry`（`get_registry(slug)`、`get_by_id`）→ `GET /api/files/{file_id}/preview` → `files/preview.build_preview` → `files/extractors.py`（xlsx/pdf/docx 提取、表格分页）。

新功能是**第二条按路径寻址的通路**。要点：两条通路必须在"打开一个 xlsx"这件事上收口到同一个 extractor，否则同一个文件在聊天里能看、在树里看不了。

### 2.3 可复用资产（比预期多）

| 资产 | 位置 | 怎么用 |
|---|---|---|
| 现成递归树 | `apps/web/src/components/settings/SessionFilesSettings.tsx`（`renderLevel(...)`） | 抽出通用组件，别从零写树 |
| 拖拽件（新版） | `apps/web/src/components/workflow/studio/PanelResizer.tsx`（`usePanelWidth(storageKey, default, min, max)` + `<PanelResizer>`） | 新面板的拖拽件。pointer capture、键盘 ±16px、双击复位、`onDrag(delta)` |
| 右栏接入点（旧版） | `apps/web/src/components/right/RightPanel.tsx` | 第 6 个 tab。**注意**：它自带一套 `mousemove` 自研拖拽，比上面那套差，建议顺手迁移 |
| 统一 diff 着色 | `apps/web/src/components/workflow/DiffView.tsx`、`studio/diffLines.ts` | Studio 继续用；代码面板用 Monaco DiffEditor，**两者不合并** |
| 文档预览 | `apps/web/src/components/chat/SheetViewer.tsx` | 表格/PDF/图片继续走它 |
| Markdown + hljs | `apps/web/src/components/chat/Markdown.tsx`（`react-markdown` + `remark-gfm` + `rehype-highlight`），token 调色板在 `apps/web/src/app/globals.css` | markdown 文件在 Monaco 里用内置渲染即可，不必复用 |
| 聊天文件 chip / 工具块 | `apps/web/src/components/chat/blocks.tsx`（`kind:"file"` 带 `fileId, name, path, fileKind`；`FileChips`；`ToolBlock`） | 跳转入口 |
| store | `apps/web/src/lib/store.tsx`（`RightTab`、`PANEL_WIDTH_MIN/MAX/DEFAULT` = 280/560/380、`PANEL_PREFS_KEY`、`openPreview`） | 新 store 动作的落点 |
| API 客户端约定 | `apps/web/src/lib/runtime.ts` | 全部 camelCase 导出，`json<T>()` 包装 |
| 目录列举范本 | `packages/runtime/src/ginno_runtime/api/files.py` 的 `_session_file_guard(slug, sid, sub)` + `list_session_files_endpoint` | 返回 `{ok, path, entries:[{name,type:"dir"\|"file",size,mtime}]}`，目录优先排序，`target.resolve().is_relative_to(base)` |
| 目录勘察范本 | `context_folders.py` 的 `cf.probe`（`_PROBE_FILE_CAP=2000`，跳过 `{.git,node_modules,__pycache__,.venv,venv}`，探测 `has_git`/`rule_file`） | 跳过清单直接复用 |
| 围栏原语 | `tools/builtin.py` 的 `_path_denied(p, base_dir, extra_roots)`、`_mount_access(p)`、`_fs_root_guard`；`GLOB_MAX_HITS=500`、`GLOB_TIME_BUDGET_S=10.0` | 见 §4.6 |
| 内容搜索 | `tools/builtin.py` 的 `grep_files` | **直接复用**，别新写一套 |
| 挂载增删 | `api/folders.py`：`GET/POST /api/folders`、`POST /api/folders/probe`、`PATCH/DELETE /api/folders/{id}`；`PUT /api/sessions/{id}/context` | 面板里"添加文件夹"直接复用 |
| Tauri invoke | `apps/desktop/src/lib.rs` 的 `invoke_handler`（~1330 行），**目前只有 7 个 pin 相关命令** | 新增 opener/trash 命令的落点。注意：webview↔shell 主要走 `webview.eval` 全局（`__ginnoOpenSession` 等），`invoke` 不是主数据通路 |

### 2.4 差距

| 需要的能力 | 现在有吗 |
|---|---|
| 多根文件树 | ❌ 只有设置页里绑死会话工作区的树 |
| 按路径读文件内容 | ❌ 只有按 `fileId` 读注册产物 |
| 代码编辑器（行号/高亮/折叠/查找） | ❌ 只有 markdown 渲染 + 文档提取 |
| 写文件（用户直写） | ❌ 只有 agent 工具能写 |
| git 状态 | ❌ 完全没有 |
| 名称/内容搜索 | ⚠️ 内容搜索有，但只在 agent 工具层（`grep_files`），未暴露 |
| agent 改动 → 前端信号 | ❌ `observe_path` 只记项目根，不记"本轮动过哪些文件" |
| 在 Finder/默认应用打开、废纸篓 | ❌ 不是 pin 那 7 个命令之一 |

---

## 3. 产品方案

### 3.1 核心场景（user stories）

| # | 场景 | 现在的痛苦 |
|---|---|---|
| U1 | agent 说"我改了 `api/stream.py`"，我想**直接看到它改了什么** | 只能信它的描述，或者自己去终端翻 |
| U2 | 我想**自己读一遍项目代码**再决定下一步让它做什么 | Ginno 里根本没有看代码的地方 |
| U3 | agent 写完一个 `.py`，我想**顺手改两行** | 得切到编辑器，改完再切回来 |
| U4 | 我想看**这个仓库现在脏在哪**（哪些文件被改了） | 完全没有入口 |
| U5 | 我想在项目里**搜一个符号/字符串** | 只能让 agent 去搜，或者自己开终端 |
| U6 | agent 产出的东西在 `~/.ginno/.../sessions/<sid>/`，我想**看一眼再决定要不要留下** | 只能走设置页那个"会话文件"树，或者 Finder |

U1 是 Ginno 相比普通 IDE 最有价值的一个——**它是唯一一个"agent 刚改了什么"这件事天然可得的编辑器**。其余场景是"既然做了就做全"。

### 3.2 概念模型

```
root           一个工作区根。id 取 "session"（会话工作区）或挂载夹的 f_xxxx
               客户端只发 root_id，从不发绝对路径
path           相对 root 的路径。"" 表示根本身
version        不透明的新鲜度令牌，仅用于相等比较（size:mtime_ns）
               不做内容哈希——同尺寸同 mtime 的外部改写理论上探测不到，
               这与 VS Code 同级别，接受
entry          {name, type: "dir"|"file"|"symlink"|"other", size?, mtime?, hidden?, ignored?}
tab            面板内打开的一个文件。携带 root_id + path + model + savedVersionId + dirty + conflict
```

**根内 vs 根外**是一等状态：agent 完全可能碰一个不在任何根里的路径（比如它 `cat` 了 `~/Downloads/x.log`）。这时不报错，而是提示 **「这个文件不在当前工作区根内」+ 一个「把其所在目录加为工作区」按钮**——直接接 `POST /api/folders`。这把"根外"从一个错误变成一个可操作的引导。

### 3.3 布局形态：三个选项（决议）

| 选项 | 内容 | 代价 | 结论 |
|---|---|---|---|
| A 独立模式路由 | 新增 `/code`（与 `/workflows` → `StudioShell` 平级），中间区域变「树 \| 编辑器」，聊天收为右栏 | 要改 AppShell 的路由/布局分支；聊天离开视线 | ✗ |
| B 全高左栏 | 会话列表和聊天之间插一列全高文件树 | 会话列表被挤到第二列或需折叠；AppShell 多处布局改动 | ✗ |
| **C 右栏宽版** | 树加在右栏作为第 6 个 tab；宽度上限从 560 放到 ~50vw | **编辑器仍然偏窄**（见下） | **✓ 已选** |

**C 的代价是真的，写明**：一屏同时要有会话列表（256px）+ 聊天（弹性）+ 代码面板（≤50vw）。在 1440px 屏上右栏最多 720px，"树 240 + 编辑器 480"的编辑器偏窄；在 1920px 上是"树 260 + 编辑器 700"，可用。

**解法是面板内双态**（不是把问题藏起来）：

```
【文件优先】默认 — 树收起为一条面包屑条，编辑器吃满整个面板宽
┌────┬─────────────┬───────────────────────┐
│会话│  Chat       │ 🗂 main.py ▸ src/api  │  ← 面包屑 + 树开关(⌘B)
│列表│             │  1  def stream(...):  │
│    │             │  2      ...           │
└────┴─────────────┴───────────────────────┘

【并排】面板 ≥600px 自动进入（或 ⌘B 手动）— 树 220–300px + 编辑器
┌────┬─────────────┬───────────────────────┐
│会话│  Chat       │▾ src/  │ 1 def stream  │
│列表│             │  ●a.py │ 2   ...       │
│    │             │▸ api/  │               │
└────┴─────────────┴───────────────────────┘
```

切换规则：面板 <600px 强制「文件优先」（此时树以**覆盖式抽屉**从左侧滑出，不挤压编辑器）；≥600px 默认「并排」；用户手动切过之后以用户选择为准。状态按 tab 持久化。

**为什么默认文件优先而不是并排**：屏幕上是"三栏"而不是"两栏"，聊天才是这个应用的主体。而且实际上"一边看树一边写代码"的频率远低于"盯着一个文件写"。

### 3.4 UX 设计

#### 根区（面板顶部）

```
┌─ 代码 ───────────────────────────────┐
│ [我的项目 ▾]                    ⟳ ⚙ │   ← 根切换 / 刷新 / 面板选项
│  ● 我的项目          rw   ⎇ main     │
│  ○ 文档库            ro              │
│  ○ 会话工作区        rw              │
│  ＋ 添加文件夹…                       │   ← 接 POST /api/folders
└──────────────────────────────────────┘
```

- 每个根显示 `rw`/`ro` 徽标；`missing` 的根标灰 + 「文件夹不存在（已移动或删除）」+ 重设/移除入口，**不报错**。
- git 根显示分支名；非 git 根显示「非 Git 仓库」，静默无徽标。
- 根内嵌套挂载（一个根在另一个根里面）：允许，最长前缀匹配。树里不重复展示——外层根里遇到内层根的路径时，该目录行显示一个"独立根"角标。

#### 树

- 懒加载，一层一次。目录优先，然后 `Intl.Collator(undefined, {numeric:true, sensitivity:'base'})`。
- **重目录占位行**（不是隐藏、不是照列）：
  ```
  ▸ node_modules · 已隐藏 12,483 项        ← 单行，可点开
  ▸ .git · 已隐藏
  ```
  默认折叠集 = `cf.probe` 跳过集 ∪ dsh 搜索排除集：
  `.git, node_modules, .next, dist, build, out, target, coverage, __pycache__, .venv, venv, .turbo, .pytest_cache, .mypy_cache, .gradle`
  面板选项里有「显示全部」开关。**理由**：照列（dsh 的做法）在大仓库里等于不可用；直接隐藏则是对文件系统撒谎，用户会以为文件不存在。占位行两者都避开。
- **git 装饰**：行首单字母徽标 + 文件名着色（M 琥珀 / A·U 绿 / D 红 / R 蓝 / 冲突红）。**不与 agent 改动标记共用编码**——后者是行左侧一条 accent 竖线 + 一个圆点。
- **agent 改动标记**：本轮/本会话动过的文件。两个筛选（本轮 / 本会话）。悬停显示「agent 在第 3 轮修改」。
- **文件操作**：右键菜单 = 新建文件 / 新建文件夹 / 重命名 / 删除（→ 废纸篓）/ 复制路径（绝对·相对）/ 在 Finder 中显示 / 在默认应用打开 / 加入聊天引用（后置）。
- 键盘：`↑↓` 移动、`→←` 展开收起、`Enter` 打开、`F2` 重命名、`Delete` 删除、`⌘P` 快速打开。`role="tree"`/`treeitem`、`aria-expanded`、可见焦点环。
- 状态：加载中（骨架行）、空目录、截断（`仅显示前 2000 项`）、失败（按错误码给文案）。

#### 编辑器

- 打开的文件以**标签栏**呈现：脏点、冲突徽标、关闭按钮；中键关闭；溢出横向滚动；最多 20 个（LRU 关闭）。
- 文件优先态下，面包屑 = `根名 ▸ src ▸ api ▸ main.py`，点任意段可跳该目录。
- diff 态：标签栏出现一个 `main.py (diff)` 伪标签，Monaco DiffEditor 并排/内联切换，对比 `HEAD` blob 与工作区内容。
- 搜索态：`⇧⌘F` 结果列表占满面板，每条 = 文件 + 行号 + 命中行；点击 → 打开并定位高亮。
- **markdown 文件**用 Monaco 的 markdown 语法高亮原样显示源码（不做渲染预览）——要看渲染结果走聊天 chip 那条 `SheetViewer` 通路。这是一个刻意的分工，写进面板选项里的提示。

#### 从聊天跳转（统一入口）

所有触发点都调同一个 store 动作 `openInCode({root_id, path, line?})`：

| 触发点 | 行为 |
|---|---|
| `kind:"file"` chip 的次要入口（小图标 / ⌘点击） | 定位到该文件 |
| `ToolBlock` 里 `read_file` / `write_file` / `edit_file` 的路径参数 | 定位到该文件；`read_file` 带 `offset` 时定位到该行 |
| 「本轮改动」小节里的文件名 | 同上 |
| `bash` 工具输出里识别出的路径 | 同上（尽力而为，识别不出就不做链接） |

**行为**：切到 `code` tab → 若面板关着就打开 → **不动宽度**（尊重用户已设的宽度；因为树会自动收起，窄面板下依然可用——自动放宽是侵入性的，不做）→ 展开树到该文件 → 若文件已打开则切到该标签 → `revealLineInCenter(line)` + 装饰高亮 1.5s。
若路径不在任何根内 → §3.2 的引导提示。

**按类型分流的收口——默认不变**（D5 决议）：chip 的**默认**点击行为**保持现状**，一律继续走 `SheetViewer`（表格/PDF/图片/文档尤其），聊天区零行为变更。文本/代码文件只新增一个**显式的次要入口**「在代码面板打开」。

分流逻辑仍按 `fileKind` 写好放在路由函数里（`text`/`code`/`data` → 代码面板），但默认走哪个分支由**一个开关常量**决定，当前值为「全走 SheetViewer」。哪天想开成分流只改这一个值，不用碰调用点。

#### 快捷键

`⌘B` 树开关 · `⌘P` 快速打开 · `⇧⌘F` 项目内搜索 · `⌘F` 查找 · `⌘⌥F` 替换 · `⌃G` 跳行 · `⌘S` 保存 · **`⌘⌥W` 关闭标签** · `F2` 重命名 · `Delete` 删除

**`⌘W` 不能用（勘察结论）**：它被 Tauri 原装 macOS 菜单的 Close Window 占着（`lib.rs:454` 的 `Menu::default`），而那是**原生菜单加速键，不是 JS 事件**——`preventDefault()` 打不过它，按 ⌘W 会直接关窗口。改用 `⌘⌥W`（已确认空闲）。若坚持要 `⌘W`，得改 Rust 侧菜单移除 Close Window，代价不值。

其余已验证空闲：`⌘B` `⌘P` `⌘S` `⇧⌘F` `⌘F` `⌘⌥F` `⌃G` `F2` `Delete`。已占：`⌘\`（右栏开关）、`⌘N` `⌘K` `⌘R` `⌘⌥R` `⇧⌘Space`。

### 3.5 权限与信任模型

**核心原则：围栏按"可达路径"划，审批按"行动者身份"划。**

- `permission/policy.py` 的 `decide()` / `graph.py` 的 `permission_node_factory` 是给 **agent 工具调用**用的 interrupt 审批流。它在 `is_bypass_permissions()`（settings 默认 `True`）时直接跳过。
- **代码面板的读写不走这套。** 用户在面板里点保存，是**人直接操作**，等价于在编辑器里按 ⌘S；agent 需要审批是因为它是自主行动者。把浏览/保存变成弹窗审批会是灾难性体验，而且会给用户一个错误的心理模型（"这些操作有风险"）。
- 反过来，这意味着面板是一个**特权 surface**：用户能写的地方 agent 未必能写。这是对的，但要写进文档以免日后被当成缺陷。
- 面板**自己的**硬约束（与 `bypass_permissions` 无关，不可被设置绕过）：
  1. `ro` 挂载上不可写（`_mount_access` == ro → 编辑区只读 + 「此文件夹以只读方式挂载」+ 一键切为读写）
  2. `.git` 内部任何路径不可写（即使「显示全部」打开了）
  3. `_path_denied` 的永久拒绝区（`~/.ssh`、`~/Library/Keychains`）
  4. 根外路径不可达

### 3.6 边界：v1 不做的事

终端 · LSP 全家桶（跳转定义/查找引用/符号重命名/诊断） · 调试器 · 扩展与插件 · 内置 git 提交/推送/分支切换 UI（那是 agent 的活） · 补全源（除 Monaco 自带的词法级补全） · 跨根拖拽移动 · `@` 提及补全 · HTML 沙箱渲染 · `.gitignore` 灰显（P2 之后） · 多用户协作。

---

## 4. 技术方案

### 4.1 数据模型与上限

```
GET /api/code/roots      → { ok, roots: [Root] }
GET /api/code/list       → { ok, root, path, entries: [Entry], truncated }
GET /api/code/read       → { ok, root, path, version, encoding, language,
                             editable, readonly_reason?, text?|pages?, eof }
PUT /api/code/write      → { ok, version } | 409 { ok:false, code:"conflict", version }
POST /api/code/mkdir     → { ok, entry }
POST /api/code/rename    → { ok, entry }
POST /api/code/delete    → { ok, trashed: true }
POST /api/code/move      → { ok, entry }
GET  /api/code/git       → { ok, is_repo, toplevel?, branch?, entries: {path: "M"|"A"|...} }
GET  /api/code/search    → { ok, mode, hits: [...], scanned, truncated, elapsed_ms }

Root  = { id, name, path, access: "ro"|"rw", missing: bool, is_repo: bool, branch?: string }
Entry = { name, type: "dir"|"file"|"symlink"|"other", size?, mtime?,
          hidden?: true, ignored?: true, git?: "M"|"A"|"U"|"D"|"R"|"C" }
```

`roots` 由 `resolve_session_dirs(session.context_folders, session.primary_folder)` 产出，加一个固定的 `{id:"session"}` 条目指向 `session_files_dir(slug, sid)`，`primary_folder` 排最前。

| 上限 | 值 | 理由 |
|---|---|---|
| 单层列目录 | 2000 | 与 `cf.probe` 的 `_PROBE_FILE_CAP=2000` 一致 |
| 单层渲染行 | 300 + 增量加载 | 无虚拟化时的 DOM 成本 |
| 可编辑文件 | 5 MiB | Monaco 实际可用区；超出降级只读分页 |
| 只读分页 | 2 MiB / 5000 行 | 单页内存有界 |
| 打开标签 | 20（LRU） | Monaco model 内存 |
| 文件名 | ≤255 字节 | 文件系统限制前置校验 |
| 名称搜索 | 扫 20000 项 / 返 50 | 沿用 dsh 量级 |
| 内容搜索 | `GLOB_TIME_BUDGET_S=10.0` / `GLOB_MAX_HITS=500` | 直接复用 `grep_files` 既有预算 |
| git status 缓存 | 2s，写操作 / 轮末失效 | 避免每次展开都打 git |

### 4.2 Runtime 改动

1. **新增 `api/code.py`**（`APIRouter`，注册进 `server.py`），实现 §4.1 全部端点。`files.py`／`folders.py` 不动。
2. **抽出一个共用围栏 helper**：`resolve_code_target(session, root_id, relpath, *, write: bool) -> (abs_path, root)`，内部按 §4.6 的闸门顺序执行。

   勘察结果：`_mount_access` **不是模块级函数**，它是 `build_builtin_tools` 内部的闭包（`builtin.py:251`），依赖闭包里的 `mounts` 列表（`builtin.py:224-235` 构造）。所以"直接复用"需要一次**小重构**：把 `resolve_mounts(context_dirs) -> list[tuple[Path, str]]` 和 `mount_access(p, mounts) -> str | None`（最具体匹配胜出）提到模块级，然后让 `build_builtin_tools` 调它们。`_path_denied` 已经是模块级，可直接用。**这样只有一份最具体匹配的实现**，不会两边漂移。

3. **`code.changed` 的传输：走既有的「工具结果标记」通路，不新造机制。**

   既有的房内模式（inline-images 设计）已经跑通同一条路：`bash` 在结果尾部追加 `<!--ginno-images:[...]-->`（`files/images.py:73` `encode_images_marker`），**agent 节点把它从模型视野里剥掉**（`graph.py:469-485` `strip_images_marker`），`messages_ui.py` 渲染持久化消息时也剥（`162-182`），而 `stream.py:857-866` 用 `parse_images_marker` 解析后 `emit("image.emit", ...)`。照抄这条路即可：新增一个 `<!--ginno-code:[{path,op}]-->` 标记 + `encode/parse/strip` 三件套，`write_file`/`edit_file` 追加，agent 节点剥掉，`stream.py` 解析并 `emit("code.changed", ...)`。

   **两个候选方案已被勘察排除**：
   - **从聊天块反推——不可行（订正：原文对 `kind:"file"` 的描述有误）。** 实情是：`kind:"file"` 块是**用户附件 chip**，不是 agent 产物——它们在发送时取自 composer 的 `payload.files`（`ChatStream.tsx:2540-2547`），历史回放时取自 `state["attached_files"]`（`messages_ui.py:546-559`）。而 agent 侧走的是**另一个块类型 `kind:"ref"`**（由 `attach_ref` 产生，`stream.py:1847-1852`、`messages_ui.py:594-600`），产物本身则通过 `preview.emit` / `image.emit` / artifacts 面板露出。
     结论不变且更硬：**一次对既有源码的 `edit_file` 在聊天流里不产生任何指向该文件的块**（`kind:"file"` 只覆盖用户上传，`kind:"ref"` 只覆盖模型主动挂了标签的产物）。所以「agent 本轮改了哪些文件」无法从聊天反推——D7 成立。
   - **用 Hook 系统——不可行。** `hooks/dispatcher.py:36-37` 虽然声明了 `PostToolUse`，但**全仓库只有声明、没有任何地方 dispatch 它**（`graph.py` 只 dispatch `PreToolUse`，见 `graph.py:860`）。而且 hooks 的语义是「用户自撰的外部命令」（会 spawn 进程），拿它做内部事件是误用。

4. **内容搜索：复用 `grep_files` 的实现，但必须补上重目录剪枝。**

   勘察结果：`grep_files`（`builtin.py:400-457`）是**纯 Python，不是 ripgrep**——`re.compile` + `os.walk` + 逐文件 `read_text(utf-8, errors="ignore")` + 逐行 `rx.search`。预算 `GLOB_MAX_DIRS=20000` / `GLOB_TIME_BUDGET_S=10.0` / `max_hits` 默认 50。

   **关键**：它的目录剪枝只走 `_path_denied`，而 `_path_denied` **不排除 `node_modules`**（它只管 `~/.ssh`、钥匙串、`~/.ginno`）。所以对 ginno 这种带 `node_modules` 的仓库，agent 的 grep 会真的走进去，靠 20000 目录预算兜底。代码面板的内容搜索**必须叠加 §3.4 的重目录跳过集**，否则 10s 预算会被 `node_modules` 吃光。

   （附注：agent 那个 `grep_files` 本身也有这个低效，但那是改 agent 行为，不在本功能范围，未动。）

   名称搜索新写（BFS + 同一份排除集 + 上限 + per-(session,root) 缓存，写操作/轮末失效）。
5. **git 状态**：子系统里调 `git -C <root> status --porcelain=v2 -z --untracked-files=all --ignored=matching`；根不是仓库时先 `git rev-parse --show-toplevel` 找外层仓库并重定位路径前缀。`git` 不存在 / 非仓库 → 静默降级，不报错。**根内嵌套仓库 v1 不装饰**（在日志里说明）。
6. **编码探测**：先 `utf-8`，失败试 `gbk`（中文用户高频命中），再 `latin-1`，回写实际编码给前端显示。这是相比 dsh（只做 UTF-8 + NUL 检测）的必要增强。

### 4.3 API 设计要点

- **客户端只发 `root_id` + 相对路径，从不发绝对路径。** 这是与 dsh 的核心安全偏离：dsh 的 `read` 让绝对路径和 `..` 外都能读（跟随后端读权限），在单根模型下勉强说得通，在多根模型下"绝对路径属于哪个根"没有答案。
- 错误码（append-only，前端按码出文案）：
  `unknown-root` · `root-missing` · `outside-root` · `denied-path` · `not-directory` · `read-only-mount` · `binary` · `not-text` · `too-large` · `conflict` · `absent` · `truncated` · `search-timeout`
- 响应统一 `{ ok: bool, ... }`，与 `files.py` / `folders.py` 一致。
- `read` 支持 `rev=worktree|HEAD`：`HEAD` 走 `git show HEAD:<relpath>`，用于 diff 的基线。**这样只需一个端点就能喂 Monaco DiffEditor**，不必让服务端算 diff。

### 4.4 Tauri 侧

目前 `invoke_handler` 只有 7 个 pin 命令。需要新增（**这是本功能第一块非 pin 的 invoke 工作**）：

| 命令 | 实现 | 说明 |
|---|---|---|
| `code_reveal(root, path)` | `tauri-plugin-opener` 的 reveal | 在 Finder 中显示 |
| `code_open_external(root, path)` | `tauri-plugin-opener` | 在默认应用打开 |
| `code_trash(root, path)` | `trash` crate | **删除 = 移到废纸篓，不是永久删除** |

**Rust 侧必须独立复核路径**：收到 `root` + `path` 后 `canonicalize(path).starts_with(canonicalize(root))`，不满足即拒。webview 传来的一切都不可信——这就是纵深防御的第二条独立路径（sidecar 校验一次，Rust 再校验一次）。需要同步更新 `Cargo.toml`、`tauri.conf.json` 的 capability（opener 的 scope）。

### 4.5 前端

**store**（`lib/store.tsx`）：

- `RightTab` 增加 `"code"`。
- 宽度改为**按 tab 存**：`rightPanelWidthByTab: Record<RightTab, number>`。旧格式 `{open, width}` 做向后兼容读取（把旧 `width` 应用到迁移时的当前 tab）。
- 每 tab 有自己的 `max`：code tab 为 `min(0.5 * window.innerWidth, 1100)`，其余维持 560。这需要 `usePanelWidth` 支持动态 max（**小 API 改动**：`max` 接受 `number | (() => number)`）。
- 新增动作：`openInCode({root_id, path, line?})`、`codeRootId`、`codePanelMode: "file"|"side"`、`codeTreeOpen`、`touched: Map<absPath, {turnId, op, at}>`。

**面板组件**：`components/right/CodePanel.tsx`，内含 `RootBar` / `FileTree` / `EditorTabs` / `MonacoEditor` / `GitDiffView` / `SearchView` / `ConflictBar`。树的渲染逻辑从 `SessionFilesSettings.tsx` 的 `renderLevel` 抽出复用。

**顺手迁移**：把 `RightPanel` 的自研 `mousemove` 拖拽换成 `PanelResizer` + `usePanelWidth`（Studio 那套更好：pointer capture、键盘可达、双击复位）。不同步做的话就是在旧实现上继续加功能。

**Monaco 接线要点**：

- 依赖 `monaco-editor`。建议 `@monaco-editor/react` 的 `loader.config({ monaco })` 指向本地包，**不要让它走 CDN**（Tauri 离线环境会静默失败）。
- **model 的 URI 必须带 root_id**：`monaco.Uri.parse("inmemory://ginno/<rootId>/<relPath>")`。否则同名文件在不同根下会共用 model、互相污染——这是个很容易踩且很难查的坑。
- **脏标记用 `model.getAlternativeVersionId()`** 与保存时的值比较，不要做文本 diff——后者会把 undo 回原样误判成"仍然脏"。
- 关闭标签 `model.dispose()`；组件卸载 `editor.dispose()`。不做会稳定泄漏。
- minimap **默认关闭**（面板窄）；`wordWrap` 默认开；`renderWhitespace: "boundary"`；bracket pair colorization 开；`stickyScroll` 关。
- 主题用 `monaco.editor.defineTheme` 从 `globals.css` 的 token 派生一套，随应用主题 `setTheme`——不要直接用裸 `vs`/`vs-dark`，会和 GInno 的配色打架。
- 语言按扩展名映射到 Monaco 内置 basic-languages，未知扩展名退 `plaintext` + 自动换行。
- **worker**：Next 14 静态导出下 Monaco 的 worker 打包需要额外配置（`monaco-editor-webpack-plugin` 或手工拷 worker）。**v1 允许无 worker 上线**——无 worker 时语法高亮、折叠、括号匹配、多光标、查找替换全部可用，只少了 TS/JSON 的语言服务（类型诊断/验证）。有 worker 是 P3 的增强。

**降级链**（顺序判定）：路径在 `.git` 下且要写 → 拒绝 · `ro` 根且要写 → 只读 · 检出二进制（前 8 KiB 有 NUL）→ 图片走 `<img>`、其余「二进制文件，无法以文本打开」+ 在默认应用打开 · > 5 MiB → 只读分页预览 + 「文件过大（8.2 MB），已切换为只读预览」 · 非文本可解码 → 「无法识别文本编码」+ 在默认应用打开 · 单行 > 50k 字符（压缩过的产物）→ 只读。

### 4.6 安全清单

**闸门顺序**（`resolve_code_target`，前一个不过就不进下一个）：

| # | 闸门 | 失败码 |
|---|---|---|
| 1 | `root_id` → 绝对路径（走 `_ensure_session`，让**冷会话**也能工作） | `unknown-root` / `root-missing` |
| 2 | 拼相对路径 → `Path.resolve()` → `is_relative_to(root)` | `outside-root` |
| 3 | **symlink 解引用后复核**包含性（步骤 2 是对未解引用的路径做的） | `outside-root` |
| 4 | `_path_denied()` 与挂载/skill/会话工作区豁免**精确镜像** | `denied-path` |
| 5 | 写操作：`_mount_access()` == `rw`；路径不在 `.git` 下 | `read-only-mount` / `denied-path` |
| 6 | 大小 / 类型 / 文本性 | `too-large` / `binary` / `not-text` |

**闸门 4 是为什么"只做包含性检查不够"**：如果只做第 2、3 步，用户把一个 `~/.ssh` 目录加为挂载夹之后它就是可读的了——因为它在根内。永久拒绝区必须**叠加**在包含性之上，而不是被包含性替代。

**闸门 4 的精确规则（二轮勘察回填，原文"精确镜像"的说法太含糊，已订正）**：

调用必须是 `_path_denied(p, session_workspace_dir, mount_roots)`——**第二个参数传会话工作区目录 `paths.session_files_dir(slug, sid)`，不是 `primary_path`**。

原因在 `tools/builtin.py:161-172`：`_path_denied` 只豁免「`base_dir` 是 `paths.home()`（即 `~/.ginno`）的**真子目录**」这一种情况。会话工作区满足（它在 `~/.ginno` 下）；而 `primary_path` 是用户挂载夹的路径，**不在 `~/.ginno` 下**，所以一旦把一个含 `primary_folder` 的会话的 `primary_path` 当 `base_dir` 传进去，那句豁免判断直接不成立，**会话工作区会被判为拒绝区**——表现就是「会话工作区根是空的」。

`mount_roots` 用 `resolve_session_dirs` 解析出的挂载路径列表（与 `builtin.py:247` 同构：**不要把会话工作区塞进 extra_roots**，那会在 `workspace == home` 时打穿 home 拒绝）。

**顺带发现的一个既有隐患（不在本功能范围内，但同源）**：`builtin.py` 传给 `_path_denied` 的 `base_dir` 就是 `primary_path`（有挂载时）。也就是说**只要会话设了主文件夹，agent 就再也读不到自己会话工作区的绝对路径**（相对路径正常，因为它基于 `primary_path` 解析）。这看起来是 `builtin.py` 的疏漏而非有意设计。**本次不动它**（改的是 agent 能力，需要单独决策），但代码面板必须按上面的正确规则实现，并为此写单测。

**其他**：
- 客户端不发绝对路径（§4.3）
- Rust 侧独立复核（§4.4）
- 文件名校验：不含 `/`、不是 `.`/`..`、无控制字符、≤255 字节
- 删除默认进废纸篓，且删除含内容的目录要二次确认并报出条目数
- 根本身不可重命名/删除/移动
- 内容搜索有 10s 预算 + 显式报出"扫描了 N 个文件后停止，结果可能不完整"——**静默截断会被读成"搜完了"**

### 4.7 里程碑

按**风险退火**排序，不是按功能多少。D4 的四项全在范围内，这里只是顺序。

| 阶段 | 内容 | 为什么在这 |
|---|---|---|
| **S1 骨架与只读** | `code` tab + per-tab 宽度 + `PanelResizer` 迁移 · `roots`/`list`/`read` · 多根 + 重目录占位 · Monaco 只读（高亮/行号/折叠/查找/多光标）· 二进制/超大/编码降级 · 手动 + 轮末刷新 | 先把"能看见"做出来，尽快拿到真实反馈 |
| **S2 写与并发**（最高风险） | `read` 带 version · `PUT write` 带 `base_version` · 409 冲突条 · `ro` 硬约束 · `.git` 禁写 · 标签栏（脏标记、⌘S、⌘W、LRU 20） | 和 agent 抢文件是唯一的不可逆风险，紧跟 S1 |
| **S3 与 agent 合流 + git** | WS `code.changed` · 树里的 agent 改动标记 · 打开中文件的"公告而非覆盖" · git 装饰 · 点开看 diff（Monaco DiffEditor） | 依赖 S2 的版本机制，也是本功能最有价值的部分 |
| **S4 检索与文件操作** | 名称搜索（⌘P）· 内容搜索（复用 `grep_files`，⇧⌘F）· 新建/重命名/删除（→废纸篓）/ 同根拖拽 · Tauri opener/trash 命令 | 相对独立，风险低，可并行 |
| 之后 | Monaco worker 语言服务 · 跨根移动 · `.gitignore` 灰显 · 树虚拟化 · 并排布局默认值调整 · `@` 引用 | |

### 4.8 验收（M0）

1. 打开一个有 `node_modules` 的仓库，展开根：单层不卡，`node_modules` 是占位行且可点开。
2. 打开一个 40 MB 的 `.log`：变只读分页，明确提示，不卡死。
3. 打开一个 GBK 编码的 `.txt`：能显示，且标出实际编码。
4. 打开 `.png`：显示图片；打开 `.wasm`：提示二进制 + 在默认应用打开可用。
5. 挂一个 `ro` 根：能读、编辑器只读、新建/重命名/删除不可用且给出"切为读写"入口。
6. 把 `~/.ssh` 加为挂载夹：目录出现在根列表里，但打开任意文件报 `denied-path`。
7. 会话工作区根能正常列出并读到 `results/` 下的产物（验证闸门 4 的豁免镜像正确）。
8. **用一个当前未加载的会话**（冷会话）打开代码面板：根列表和树正常（验证走的是 `_ensure_session`）。
9. 用户打开 A 文件并编辑不保存 → 让 agent 用 `edit_file` 改同一个文件 → 面板出现冲突条且**不覆盖**已有编辑 → 保存走 409 → 「看差异」能看到两边内容。
10. agent 连续改 3 个文件 → 树里这 3 个文件在"本轮"筛选下高亮；从聊天对应 chip 点进去能定位到行。
11. `⇧⌘F` 搜一个跨 20+ 文件的字符串：有结果、点击能定位；人为制造超时能出现"结果可能不完整"的提示。
12. 删除一个目录 → 出现在废纸篓里，可恢复。
13. 在 `~/.ginno` 之外的挂载根里新建/重命名/删除后，`git status` 装饰在 2s 内更新。

---

## 5. 决策记录（2026-09-29）

| # | 议题 | 选项 | 决议 | 理由与已知代价 |
|---|---|---|---|---|
| D1 | 树根模型 | 多根 / 单根 / 自由浏览 | **多根** | 贴合 Ginno 已有的挂载模型；根集合与 agent `read_file` 的可达范围一致，围栏好解释。代价：绝对路径语义必须由 `root_id` 承担（§4.3 因此收紧） |
| D2 | 布局形态 | 独立路由 / 全高左栏 / 右栏宽版 | **右栏宽版** | 改动最小，不挤压会话列表。**代价已接受：编辑器偏窄**，靠面板内双态（默认文件优先）缓解 |
| D3 | 编辑器 | CodeMirror 6 / Monaco / 只读高亮 / 外部编辑器 | **Monaco，可编辑** | Tauri 内置无体积与离线顾虑，体验最接近真 IDE。代价：Next 静态导出的 worker 打包摩擦（§4.5，可降级） |
| D4 | v1 范围 | 四项多选 | **全选** | — （实现顺序见 §4.7，按风险而非按功能排） |
| D5 | chip 默认行为 | 保持 SheetViewer / 按类型分流 | **保持 SheetViewer**（默认行为不变） | 2026-09-29 决议：默认点击一律继续走 `SheetViewer`，**聊天区零行为变更**。文本/代码只新增显式次要入口「在代码面板打开」。分流逻辑仍写在路由函数里但由开关常量控制（当前=全走 SheetViewer），想开时改一个值 |
| D6 | 删除语义 | 永久删除 / 废纸篓 | **废纸篓** | 文件树里的 `rm` 不留后悔药。代价：多一个 Rust 依赖 |
| D7 | agent 改动信号来源 | 从聊天块反推 / 工具层发事件 | **工具层发事件** | 服务端权威，且在 S3 与冲突检测合流；从聊天块反推依赖 `kind:"file"` 是否总带路径，不稳 |

---

## 6. 与既有系统的关系

- **`SheetViewer` / `files/registry.py`**：不动。文档类预览继续按 `fileId` 走。两条通路在"打开一个 xlsx"上收口到同一个 `build_preview`/extractors（§2.2）。
- **`permission/policy.py`**：不动，且刻意不接（§3.5）。
- **`context_folders.py`**：只读复用 `resolve_session_dirs` / `probe` 的跳过集；"添加文件夹"复用 `api/folders.py` 的现有端点，面板不新增挂载管理 API。
- **`tools/builtin.py`**：`_path_denied` / `_mount_access` 被复用（不是复制）；`observe_path` 调用点扩展出一个事件；`grep_files` 被复用做内容搜索。
- **Studio / `DiffView`**：Studio 的 diff 走 `DiffView.tsx` 不变；代码面板用 Monaco DiffEditor。两套并存是有意的——Studio 的 diff 是工作流产物，代码面板的是 git 工作区。
- **`projects.py`（C 概念）**：v1 不作为树根。后续可作为"最近活动"快捷入口。

---

## 7. 下一步与待验证

按"先花小钱消掉不确定性"排序。前三条会实质改变实现，建议**在写第一行代码前**做掉。

| # | 未知 | 怎么验 | 影响 |
|---|---|---|---|
| 1 | **Monaco worker 在 Next 14 静态导出 + Tauri 下的打包路径** | 1–2h spike：最小页面挂 Monaco，只跑 web 静态导出构建（不要 `make app`），确认打包产物里 worker 能加载 | 决定 S1 是"有 worker"还是"无 worker 降级"。降级可接受，但要早知道 |
| 2 | ~~`grep_files` 是否 ripgrep 支撑~~ | ✅ **已验证（2026-09-29）** | **纯 Python**（`builtin.py:400-457`）。且它**不剪枝 `node_modules`**，靠 20000 目录预算兜底；代码面板必须自己叠加重目录跳过集 |
| 3 | ~~`observe_path` 调用点 + WS 事件约定~~ | ✅ **已验证** | 注入点是 `graph.py:982`（`_project_observer`）。`code.changed` **走既有「工具结果标记」通路**（照抄 `<!--ginno-images:-->` 那条），不用 Hook（`PostToolUse` 只声明未 dispatch），详见 §4.2-3 |
| 4 | ~~`kind:"file"` 块的 `path` 是否总在某个根内~~ | ✅ **已验证（订正）** | `kind:"file"` 是**用户附件** chip（`ChatStream.tsx:2540`、`messages_ui.py:546`）；agent 侧是另一个块类型 `kind:"ref"`。一次 `edit_file` 在聊天流里不产生任何指向该文件的块 → **D7 得实证** |
| 4d | 注册文件的 path 是否总在某个根内 | ✅ **已验证：不保证** | `FileRegistry.register`（`files/registry.py:95-148`）**没有任何包含性校验**，只做 `_norm`；绝对路径原样通过（`api/files.py:94-110`）；`attach_ref` / `@artifact` / bash 产图按任意绝对路径注册。所以「根外文件」不是边角情况，§3.2 的「加为工作区」引导**必须做** |
| 4e | ~~Monaco worker 与 CSP~~ | ✅ **已验证：不是问题** | store 与主进程均确认 worker 是**同源 HTTP 分块**（`server.py::_serve_web` 服 `127.0.0.1:8787/apps/web/out`），不是 `blob:`，`default-src 'self'` 覆盖得到。**结论：带 worker 可用**，`pnpm --filter @ginno/web build` 实测通过。但反向依然成立：本仓 CSP 无 `worker-src`/`child-src`，**`blob:` worker 会被拦**——详见 §8.2 |
| 4f | 快捷键占用 | ✅ **已验证** | 空闲：`⌘B` `⌘P` `⌘S` `⇧⌘F` `⌘F` `⌘⌥F` `⌃G` `F2` `Delete`。**已占：`⌘W`**（Tauri 原装 macOS 菜单的 Close Window，`lib.rs:454`——JS 的 `preventDefault` 打不过它）、`⌘\`（右栏开关 `AppShell.tsx:321`）、`⌘N` `⌘K` `⌘R` `⌘⌥R` `⇧⌘Space` |
| 4b | 能否用 Hook 系统做 `code.changed` | ✅ **已验证** | **不能**。`PostToolUse` 只声明未 dispatch（`graph.py` 仅 dispatch `PreToolUse`），且 hooks 是用户自撰外部命令的语义 |
| 4c | `_mount_access` 能否直接复用 | ✅ **已验证** | **不能直接 import**——它是 `build_builtin_tools` 内的闭包（`builtin.py:251`）。需要一次小重构提取到模块级，详见 §4.2-2 |
| 5 | `⌘B`/`⌘P`/`⌘S` 是否与既有全局绑定冲突 | 查 `AppShell.tsx` 现有绑定（已知占用 `⌘\`） | 快捷键表可能要改 |
| 6 | `_path_denied` 的 `~/.ginno` 豁免逻辑能否被精确镜像 | 读源码 + 写单测（验收第 7 条） | 镜像错 = 会话工作区根列不出东西 |
| 7 | `usePanelWidth` 支持动态 max 的改动面 | 读 `PanelResizer.tsx` | 小改动，但会碰到 Studio 的三处调用 |
| 8 | `tauri-plugin-opener` / `trash` 的 capability 配置 | 查 `tauri.conf.json` + 跑一次完整 `make app` | 记得 `make app` 后**完全退出并重开 Ginno**（见 CLAUDE.md 的 zlib 故障） |

---

## 8. S1 交付回填（2026-09-29 实施后）

S1 由 7 个并行 agent 实现（编排：**文件所有权互斥** + 阶段间栅栏），随后一轮独立验证 + 主进程收尾。本节只记**与上面设计不同的地方**和**以后必须知道的事实**。

### 8.1 Monaco 的版本硬约束（重要，以后别再踩）

**`monaco-editor` 必须钉在 0.52.x–0.55.x。** 0.56.0 给 `editorWorkerService.js` 加了一句内部的 `new URL('../../common/services/editorWebWorkerMain.js', import.meta.url)`；Next 14 的 webpack 会把它当 asset/resource 处理，把**原始 ESM** 复制进 `static/media/`，然后 Terser 解析不了、构建直接失败。

`apps/web/package.json` 用 `^0.55.0`——`0.x` 的 caret 本来就只允许同 minor 的 patch，所以已经足够紧；实测解析到 **0.55.1**，`pnpm --filter @ginno/web build` 通过（23/23 静态页）。

⚠️ **升级 Monaco 前必须先跑 web 构建**：`typecheck` 通过**完全不代表**构建能通过。这一点在本次实施里被真实踩到过——验证 agent 发现 typecheck 绿而 `build` 红。

### 8.2 worker 接线（已验证可用，照抄）

```ts
new Worker(new URL("monaco-editor/esm/vs/editor/editor.worker.js", import.meta.url))
```

- 必须是 **classic worker**，不能传 `{ type: "module" }`。
- 必须是**字面量的静态 `new URL(...)`**（webpack 要能静态看见），且子路径必须是 `esm/vs/...` 形式。
- 零 webpack 配置：不需要 `monaco-editor-webpack-plugin`、不需要改 `next.config.mjs`、不需要手工拷 worker 文件。
- worker 创建失败会自动退到主线程（Monaco 自己兜底），不用额外守卫。

**CSP 不是问题，但原因值得记**：worker 是**同源 HTTP 分块**（`server.py::_serve_web` 在 `127.0.0.1:8787` 上服 `apps/web/out`），不是 `blob:`，所以 `tauri.conf.json` 的 `default-src 'self'` 覆盖得到。

⚠️ **反向结论**：这套 CSP 没有 `worker-src` / `child-src`，所以**从 `blob:` URL 建 worker 会被拦掉**——以后别走那条路。

### 8.3 实施中改掉的设计决定（都是改好了）

| 原设计 | 实际做法 | 为什么更好 |
|---|---|---|
| §2.3「顺手迁移：`RightPanel` 换 `PanelResizer` + `usePanelWidth`」 | 换了 `PanelResizer` 的**手柄**，但宽度持久化仍归 store（`rightPanelWidthByTab`，单键） | `usePanelWidth` 会再写一个自己的裸数字键 → **两个宽度真源**。F 拒绝这条迁移是对的，**§2.3 的这条建议作废** |
| §4.1 客户端按 `{ok}` 判别联合处理错误 | 客户端在 `ok:false` 时**抛 `CodeApiError`（带 `code`）**；服务端仍是 HTTP 200 + `{ok:false, code, message}` | 简报把返回类型钉成了 `Promise<CodeRoot[]>` 等（无 `ok` 字段），抛错是唯一不丢错误码的形态。`CodeErrorCode` 联合已导出 |
| 客户端只发 `root_id` + 相对路径 | 一致。但**聊天 chip 只有绝对路径**，所以点击瞬间要先 `listCodeRoots` 做最长前缀匹配；匹配不上就把绝对路径透传给 `openInCode`，由面板显示 §3.2 的「不在工作区根内」引导 | §4d 的发现（注册表路径可能在任何地方）在这里落地了 |
| 主题用 `defineTheme` 从 `globals.css` 派生 | 一致（深/浅两套） | ✅ |
| 树的重目录占位行只有「已隐藏」 | 补了 `Entry.hidden_count`，现在可显示「已隐藏 12,483 项」（上限 9999 显示为 `9,999+`） | 兑现 §3.4 的诚实占位行设计 |

### 8.4 已延后、非遗漏的项

- `RootBar` 的「＋ 添加文件夹…」行（接 `POST /api/folders`）——S1 简报只列了徽标 / 缺失态 / 分支 / 刷新。
- `ro` 挂载提示条上的「一键切为读写」按钮——需要挂载夹 API，归 S2。
- 单行 > 50k 字符（压缩产物）降级只读——未进 S1 规则清单。
- 从 `kind:"ref"` 块（`attach_ref` 产物）跳转——S1 只接了聊天 chip 的次要入口。

### 8.5 仍未处理的既有隐患（重申 §4.6）

`builtin.py` 传给 `_path_denied` 的 `base_dir` 仍是 `primary_path`（有主文件夹时），即 **agent 读不到自己会话工作区的绝对路径**。本次**没有动它**——代码面板走的是正确规则，但 agent 侧这个疏漏需要单独决策。

另有一个既有告警与本次无关：`store.tsx` 的 `reloadWorkflowRuns` 在 `[]` 依赖下调 `setRightPanelOpen`，**HEAD 上就存在**（依赖的是 ref 和稳定 setter，属良性），未动。

### 8.6 验证过的事实（供后续参考）

- **围栏第 4 闸门经独立反事实验证**：把 `primary_path` 当 `base_dir` 传进去时 `_path_denied` 返回 **True**（拒绝）——证明"传会话工作区目录"这个选择是**承载语义的**，不是风格偏好。已固化为单测。
- `builtin.py` 的 `resolve_mounts` / `mount_access` 提炼重构**行为等价**，861 个既有单测全绿。
- **教训：TS 编译不会报"源码里混进真 NUL 字节"**。本次前端源码里出现过两个真 `0x00`（`EditorTabs.tsx` / `FileTree.tsx` 的模板字符串里，把转义写成了实际字节），后果是 `git diff` 把这些文件当**二进制**、diff 直接不显示。已在收尾时全仓扫描并修掉。**建议以后收尾都扫一遍**（`python3 -c` 扫 `\x00` 与非 UTF-8 很快）。

---

## 9. 图片预览回填（2026-09-29 追加）

§4.5 的降级链写了「图片走 `<img>`」，但没交代**字节从哪来**——`read` 只回文本，二进制时 `text: null`，所以前端拿不到图片内容，这个能力当时等于没有。本节记实际落地的形态。

### 9.1 新增 `GET /api/code/raw`

```
GET /api/code/raw?project_slug=&session_id=&root=&path=&v=<version>
    → 200 原始字节（Content-Type 按白名单）| 非 2xx + X-Ginno-Code
```

- **走和 `read` 完全同一道围栏**（第 5 节那六道闸门）。第二个端点正是最容易漏掉围栏的地方，所以有专门的测试对两个端点做一致性断言。
- **失败必须是真 HTTP 状态，不能是 `{ok:false}`**：这个端点只被 `<img src>` 消费，而 `<img>` 读不了 JSON body——200 带 JSON 错误会渲染成"图坏了"而不是"失败了"。所以错误码改走 `X-Ginno-Code` 响应头。这是本仓唯一一个不遵守 `{ok}` 信封的端点，理由就在这里。
- 体积上限 `RAW_MAX_BYTES = 32 MiB`，**先 `stat()` 判断再读**，绝不为了拒绝一个大文件而把它读进内存。
- `X-Content-Type-Options: nosniff` + 白名单外的类型给 `Content-Disposition: attachment`：工作区里的文件不能把 webview 骗去当标记语言解析。
- `Cache-Control: immutable`，配合前端拼的 `v=<version>`（`size:mtime_ns`）——版本变了 URL 就变，所以不可变缓存是安全的，也避免了每次切标签都重新拉一遍图。

### 9.2 图片的识别：`readonly_reason: "image"`

`read` 在二进制分支里额外判断扩展名是否属于 `files.extractors.IMAGE_EXTS`（**仓库对"什么是图片"的唯一定义**），是则回 `"image"` 而不是 `"binary"`。前端据此分派到 `ImagePreview`，非图片二进制仍走原来的「二进制文件，无法以文本打开」提示。

`CodeRead.readonly_reason` 的联合因此多了一个成员；`"image"` 是其中**唯一不算"降级"的值**——面板能显示它，只是不能当文本显示。

### 9.3 SVG 也渲染成图（2026-09-29 决议，翻转了初版）

初版实现让 SVG 走文本（在编辑器里看 XML 源码），理由是"代码面板就该给源码"。**用户推翻了这条：SVG 要渲染成图。** 记录如下，因为翻转它暴露了一个实现顺序问题。

**关键点：图片判定必须在二进制嗅探之前。** SVG 的字节**是文本**（前 8 KiB 无 NUL），所以只靠嗅探它会走文本路径、被当成源码。因此 `code_read` 里改成：

```python
if target.suffix.lower() in IMAGE_EXTS:   # ← 在嗅探之前
    return {... "readonly_reason": "image", "text": None ...}
# ... 然后才是 open + NUL 嗅探 → "binary"
```

这个顺序带来两个附带好处，不是纯风格问题：

1. **"文本形态的图片"能被正确分类**——否则任何文本格式的图片类型都会漏。
2. **不再依赖嗅探**——一个恰好开头 8 KiB 没有 NUL 的小位图，以前会掉到文本路径显示乱码；现在按扩展名分类，不会再出现。

安全上仍然是安全的：`raw` 以 `image/svg+xml` 返回，`<img>` 里的 SVG **脚本是惰性的**（拿不到本源的 DOM、不能执行），所以我们**不需要**沙箱——真实前提是我们**刻意不提供 inline/iframe 的渲染面**（§1.3 已排除 HTML 沙箱）。

固化为两个测试：`test_read_flags_svg_as_image`（分类，且锁住"判定在嗅探之前"这个顺序——把两块对调它就会挂）与 `test_raw_serves_svg_with_a_renderable_type`（传输）。

`language.ts` 里的 `svg: "xml"` 映射**保留**：SVG 现在不会进 Monaco，但万一以后有别的通路把它当文本渲染，那个映射是正确的兜底。

### 9.4 顺带修掉的一个「看起来没实现」的 bug

`SettingsNav` 里的「工具标签」入口点进去 404。**功能是好的**（`SettingsView` 正确渲染 `ToolLabelsSettings`），问题在 `apps/web/src/app/settings/[tab]/page.tsx` 的 `generateStaticParams()` 漏了 `tool-labels`——静态导出下没列进 params 的路由**根本不会生成**。

**这类 bug 在 dev 里看不见**（`next dev` 按需渲染任意路由），只有打包版才 404，所以用户的"好像没实现"是完全合理的观察。已补上并加注说明该列表必须与导航保持同步。验收方式：构建后 `apps/web/out/settings/*.html` 从 16 个变成 17 个。

### 9.5 右栏标签的顺序与显隐

（不在本设计的范围，但落在同一个界面区域，记一笔。）标签的顺序与显隐可在**设置 → 通用设置 → 右栏标签**里用拖拽 + 眼睛图标调整，标签栏与折叠态 dock 同时生效。

要点：

- 标签注册表（顺序/标签名/图标）提到 `apps/web/src/lib/rightTabs.ts`，成为唯一来源；它用 **type-only** import 引 `RightTab`，所以 store → rightTabs 的**值**依赖与 rightTabs → store 的**类型**依赖不构成运行时循环。
- 偏好持久化进既有的 `ginno-right-panel` blob（新增 `order` / `hidden` 字段，向后兼容）。
- **对陈旧偏好加固**：代码里删掉的标签被丢弃；代码里新加的标签被**追加**——否则以后新发布的标签会一直隐形。
- **两条守卫**：不允许隐藏最后一个可见标签（空标签栏之后没东西可点，面板再也回不来）；活动标签被隐藏时自动落到第一个可见的。
- **一条语义界线**（值得记住）：程序化切标签分两类——**明确的用户意图**（聊天跳转、点通知）可以**取消隐藏**并切过去；**后台事件**（artifacts 自动跟随）必须**尊重隐藏偏好**，否则等于在后台偷偷撤销用户的选择。未读徽标那条路已经承担了后台事件的提示职责。
- 拖拽**不能用 HTML5 DnD**：`tauri.conf.json` 的 `dragDropEnabled: true` 会让 Tauri 拦截原生拖拽事件，关掉又会破坏应用现有的文件拖入功能。所以走指针事件，照抄已在线上工作的 `PanelResizer` 模式。拖拽之外**必须**保留键盘可达（行可聚焦、`↑↓` 移动、`空格/回车` 切换显隐）——桌面应用里只能鼠标操作的设置项是真实缺陷。

---

## 10. S2–S4 交付回填（2026-09-29 实施后）

S1–S4 由多轮并行 agent 实现（每轮：**文件所有权互斥** + 阶段间栅栏 + 独立验证），主进程负责契约与集成。本节只记**与设计不同的地方**、**为什么**，以及**哪些地方取证过、哪些没有**。

### 10.1 S2 写路径与并发

**服务端 `PUT /api/code/write`**（`api/code.py`）：`base_version` 比较交换 · **原子写**（同目录 `mkstemp` → 写 → `fsync` → **在 `replace` 之前 `os.chmod(tmp, st.st_mode)`** → `os.replace`）· **编码原样写回**（GBK 不会被悄悄变 UTF-8；写不了就 `not-text`，不降级）· 按编码后字节数判 `too-large` · 写闸门直接复用 `resolve_code_target(write=True)`（**没有第二套 `ro`/`.git` 判断**）。

**前端**：脏状态由 Monaco 的 `getAlternativeVersionId()` 与保存基线比较（**不做文本 diff**——那会把「undo 回原样」误判成仍然脏）· ⌘S 走 Monaco 命令注册表 · 冲突条三选一 · `ConflictDiff`（Monaco DiffEditor，并排/内联）。

**评审期抓到并修掉的两个 bug**（都不是 agent 的错，是实现里自然会长出来的）：

1. **`MonacoEditor` 只在 model URI（`rootId + path`）变化时重建 model**，而 `CodePanel` 没给 `key` → **「刷新」对已打开的文件无效**：重新读到了，编辑器还显示旧内容。修法是给 `key` 带上每标签的 `rev`，且 **`rev` 只在显式替换缓冲区时递增**——保存成功只 bump `savedNonce`，否则白丢光标与撤销历史。S3 的「agent 改了文件自动跟随」也依赖这个 `key`。
2. **冲突后继续编辑再点「强制覆盖」会写回旧文本**，静默丢掉新击键（`dirty` 在冲突前后都是 true，分辨不出来）。修法：`MonacoEditor` 增加 `onChangeText`，`CodePanel` 用 per-tab 的 `latestTextRef`（**ref 不是 state**，零重渲染）作为「强制覆盖」的数据源。

**两条 UX 决定**：LRU 淘汰**只针对干净的标签**，20 个都脏时宁可超出上限也不丢编辑 · 关闭有未保存修改的标签**按两次**（不弹系统模态，标签 tooltip 提前说明）。

**一个已接受的固有竞态**：`stat` 判 version 与 `os.replace` 之间是 check-then-write 窗口，两个写者同一瞬间仍可能后写覆盖先写。原子 replace 保证不会出现半个文件，但真正的 CAS 需要文件锁——**未做，需另开**。

**（2026-10-08 更新）这不是"整个系统都没锁"**：agent 侧的内置 `write_file`/`edit_file` 已在
`tools/builtin.py` 加了**进程级写锁 + 原子写**（锁覆盖 `edit_file` 的 read→write 全程），修掉的是
"同一消息里多个 tool call 并发改同一文件互相踩踏"。本节说的 check-then-write 窗口是**另一条路径**
——`PUT /api/code/write`（用户在面板里保存）对上磁盘/agent，且锁是进程内的、覆盖不到 `bash` 或外部进程，
所以窗口依旧存在，两者不冲突。

### 10.2 S3 与 agent 合流 + git

**传输通路照抄既有的 `<!--ginno-images:-->` 那一套**（新增 `files/code_changes.py` 的 encode/parse/strip + `match_root_id`），标记加在 `write_file`/`edit_file` 的返回里，**三处剥离**：agent 节点、`messages_ui` 渲染、`stream.py` 送出前。**不用 Hook 系统**（`PostToolUse` 只声明未 dispatch——§7 已列为已验证不可行）。

**一处设计收口（值得记住）**：`code.changed` 事件**携带写后的 `version`**。就靠这一个字段，「agent 改了文件」这条**推送**路径直接复用了 S2 的冲突条——干净标签自动重载、脏标签弹同一个「重新加载 / 看差异 / 强制覆盖」，不必再造一套 UI。代价是推送路径只有 version 没有内容，所以 `conflict.diskText` 允许为 `null`，「看差异」按需去读（读不到就**不开差异**并说明，而不是显示成「磁盘是空的」）。

**git 装饰**：`GET /api/code/git` 在 **toplevel** 跑 `git status --porcelain=v2 -z --ignored=matching`，`_relocate` 统一把路径重定位成 root 相对；非仓库 / 没 git / 超时**一律静默降级为无装饰**。顺带修掉一个 S1 缺口：`rev=HEAD` 的 `git show HEAD:<rel>` 原先是相对 root 解析的，**只有 root 正好是仓库顶层时才正确**，现在补上了 toplevel 前缀。

**服务端不做 git 缓存**（设计 §4.1 提过 2s 缓存）：前端按「每个根一次」拉取、在手动刷新与收到 agent 改动时重取，天然避免了每次展开都打 git。**这是个取舍，不是遗漏。**

**两种装饰的编码必须不同**：git 是行首**字母**着色，agent 改动用**左侧 accent 竖条 + 圆点**。

### 10.3 S4 检索与文件操作

**搜索**（`GET /api/code/search`）：内容搜索**复用 `grep_files` 的实现思路并补上重目录剪枝**——勘察已证明 `grep_files` 只走 `_path_denied`、**不剪枝 `node_modules`**，直接复用会让目录预算被吃光。**截断/超时显式上报**（静默截断会被读成「搜完了」）。名称匹配用子序列模糊，排序确定性（深度→路径长→名）。

**文件操作**（新模块 `api/code_fsops.py`）：`mkdir`/`rename`/`move`/`delete`，全部经 `resolve_code_target(write=True)`。

**删除路径上的一处安全设计（重要）**：打包版的删除最终交给 Rust 的 `code_trash`，而它**只做路径包含性复核、看不到挂载层级**。这不是「纵深防御可以省」的问题，而是**正确性问题**——`mount_access` 是最具体匹配胜出，所以「一个只读挂载嵌在可写根里面」时，前端按根算 `writable` 在结构上就是错的。因此给 `POST /api/code/delete` 加了 **`check_only`** 模式：前端先过服务端这道门（围栏 + 条目数），**通过了再** `invoke("code_trash")`。已有测试锁住「只读层级仍被强制」与「不能拿它探测根外路径」。

**另两类被 agent 自己发现并修掉的问题**（都是实现里真实长出来的）：

- **路径绕过**：`code._norm_rel` 会 `strip("/")`，所以**先归一化再送进 `resolve_code_target` 会把绝对路径变成「看起来相对」的路径**，绕过该函数的绝对路径闸门（只剩包含性检查）。现在所有端点都把**客户端原始字符串**喂给 resolver，归一化值只用于回显/判断。
- **Tauri 能力配置**：**故意不加 `opener:*` 权限**。三个命令是 app 自有的 invoke command（Tauri v2 的 ACL 不管它们——现有 7 个 pin 命令同样没有权限条目），而加上 `opener:default` 会把 `plugin:opener|reveal_item_in_dir` 暴露给 webview，**成为绕过 `code_resolve_in_root` 的第二条通道**。同时用 `open_js_links_on_click(false)` 构建插件，否则默认的 JS 注入会劫持现有的 `window.open` 行为。

**前端的两处刻意选择**：内容搜索的 `q` 在**客户端转义**后再加 `(?i)`——服务端把 `q` 当正则编译，而结果高亮是按字面量做的，不转义会让 `foo.bar` 的 `.` 变通配符、命中与高亮不一致。正则能力仍在 API 层，将来加开关即可 · `onAddFolder` **不改 `primary_id`**：加一个文件夹不应该改变 agent 的相对路径基准。

**服务端降级的「回收站」是 `会话工作区/.trash-<epoch_ns>/`，不是系统废纸篓**（浏览器/dev 无 trash API）。打包版走真废纸篓。响应里带 `trash_path` 与中文 `message`，UI 要转达。

**不需要 `pnpm install`**：新增的只有 Rust crate（`trash`、`tauri-plugin-opener`），没有 JS 依赖。首次构建的机器需能访问 crates.io。

### 10.4 ⚠️ 取证的边界：UI 从未在真实应用里跑过

到 S4 为止，**所有前端交互一次都没有在活的 webview 里被驱动过**。已经取到的证：

- 服务端：**1020 个单测**（S1 起累计），含围栏、写路径、git、搜索、文件操作；多轮**变异测试**（每个变异都被捕获，含 sha256 双向还原证明）
- **打包产物上的真调用**：`/api/code/{roots,list,read,raw,write}` 在 PyInstaller bundle 里真调通过（含「陈旧 base_version → 409 且文件未被改动」这条）
- 类型检查、web 静态导出构建、Tailwind 类名是否真实存在（实测 `text-amber`/`text-cyan` 不存在且会**静默失效**）、全仓 NUL/编码扫描（**两次**抓到 agent 把 ` ` 写成实际字节，会让 `git diff` 把文件当二进制）

**没取到的证**：树/编辑器/双态布局/跳转/保存/冲突条/差异视图/git 装饰/agent 跟随/搜索/右键菜单/行内输入/拖拽/⌘P/⇧⌘F/OS 集成（Finder、默认应用、废纸篓）——**全部未验证**。这是当前最大的一笔欠账，且随功能增加而增大。

**教训（值得固化）**：`git diff` 对含 NUL 的文件静默转为二进制，而 **TS 编译不会报**；`text-amber` 这类不存在的 Tailwind 类也**静默失效**。两者都属于「编译过 ≠ 生效」，收尾时应该各扫一遍。

### 10.5 首次实机使用后的修正（2026-09-30）

用户跑起来后报：**进入「代码」标签默认是空的，要按一下文件树才显示文件。**

根因不是渲染 bug，而是**布局自动定尺寸与默认宽度撞在一起**：`wide` 阈值是 600px，而右栏默认宽 380px，于是自动定尺寸把模式设成 `"file"` 且 `codeTreeOpen = false`——「树列」与「抽屉」两个分支都不渲染，面板里只剩一句「从文件树打开一个文件开始阅读 / 按 ⌘B 展开文件树」。**窄面板的默认状态就是「空面板」**，对一个以浏览为主的入口来说不可接受。

修正三处：

1. **新增 `browsing` 状态**（没有打开任何标签）：此时树占满面板、编辑器列 `hidden`、两个切换按钮隐藏（没有东西可切）。打开文件后回到原来的两态规则（宽面板并排 / 窄面板文件优先 + ⌘B 抽屉）。
2. **默认根改为会话工作区**（`id === "session"`）而不是 `roots[0]`：服务端把挂载夹排在前面，所以有挂载时 `roots[0]` 是项目文件夹；而会话工作区总是存在、又是 agent 产物的落点，做默认**永不空**。项目文件夹在根切换器里一次点击可达。
3. 抽屉的渲染条件从「`file` 模式且树开着」改成「**树未被固定**且树开着」——表达的是意图（抽屉是树未 dock 时的兜底），并且天然排除了浏览态。

**顺带的教训**：`browsing` 一开始被我写在 `tabs` 声明之前，`tsc` 立刻报 TDZ（`Block-scoped variable 'tabs' used before its declaration`）。长组件里的这种声明顺序依赖很容易漏，**类型检查是唯一能抓住它的手段**——这就是「改完就跑 typecheck」值钱的地方。