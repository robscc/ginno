# 新建会话挂载目录 — 产品设计

> 状态：已实现（2026-10-09，未提交）。上游设计：`context-folders-design.md`（挂载机制本体）。
> 本文只覆盖「建会话时的挂载入口」，不动挂载机制本身。
> 实现备注：桌面壳权限收紧为 `dialog:allow-open`（比 §2.2 初稿的
> `dialog:default` 更窄，只开目录选择）；首页 chip 在 `ChatStream` 挂载点
> 每次发送后按库内 auto_mount 条目重新预填。

## 0. 结论先行

**挂载机制在 Ginno 里已经端到端存在**（目录库、ro/rw、primary=bash cwd、
规则文件注入、会话中改挂载、重启恢复），后端建会话请求**已接受**
`context_folders` + `primary_folder`（`sessions.py:77-78`，`:578-583` 走
`resolve_session_dirs`，与会话中改挂载同一条已验证路径）。

唯一缺口在前端：挂载入口 `ContextFoldersChip` 只在有活跃会话的 TopBar 出现
（`ContextFoldersChip.tsx:38`，无 session 直接 `return null`），而会话是
**首次发送时懒创建**的（`ChatStream.tsx:682` `createAndSend`），创建链路
（`newSession` → `createSession`）不携带任何挂载字段。

评审决议（2026-10-08）：**v1 一次带上全部三个可选项**——目录库快选、
自动挂载标记、原生目录选择器。因此后端不再零改动（库 schema 加
`auto_mount` 字段），桌面壳加 dialog 插件。改动面见 §3。

## 1. 用户故事

- 「我要分析 `~/workspace/foo` 里的代码」——现在必须先随便发一句话建出会话，
  再去 TopBar 挂目录，agent 的第一句话已经在错误的上下文里跑完了。
- 想要的是：首页输入框旁直接选好目录，第一条消息就带着正确的挂载、
  正确的 bash cwd、正确的项目规则（GINNO.md/CLAUDE.md）发出。

## 2. 交互设计

### 2.1 入口位置

首页 composer 的 chips 行（agent chip、model chip 同族），一个 folder chip：

```
┌──────────────────────────────────────────────┐
│  输入框…                                       │
├──────────────────────────────────────────────┤
│ 📁 2   ▾  │ 🤖 Dev Agent ▾ │ qwen3.8-max ▾ │  ← chips 行
└──────────────────────────────────────────────┘
```

- 未选时 chip 显示 `📁 挂载`（faint），已选时显示数量并着绿（沿用
  `ContextFoldersChip.tsx:91-94` 的样式约定）。
- 与 agent/model chip 一致的「将要用什么」哲学（ChatStream.tsx:634）：
  chip 命名的是**发送时将生效**的挂载，不是某个已存在会话的状态。

### 2.2 面板（复用 + 三处增强）

点开面板与现有 TopBar chip 面板同构，但操作对象是**本地待生效列表**
（pending），不调 `putSessionContext`：

```
┌ 挂载目录 ────────────────────────────────┐
│ ☆ ~/workspace/foo        rw        ✕     │  ← 待挂载列表
│ ☆ ~/data/reports         ro        ✕     │
├──────────────────────────────────────────┤
│ 目录库                                    │
│   ~/lab/ginno        [自动]      [+]      │  ← 快选 + 自动挂载标记
│   ~/docs/specs                    [+]     │
├──────────────────────────────────────────┤
│ [输入路径___________] [浏览…] [挂载+]      │  ← 浏览 = 原生选择器
│ ⚙ 管理目录库…                             │
└──────────────────────────────────────────┘
```

- **目录库快选**：已入库目录一键 [+]，补现有 live chip 只能手输路径重复
  挂载的洞。同一个组件，live/pending 两种模式的面板都受益。
- **自动挂载标记**：库条目可标「自动」（`auto_mount`），新建会话的 pending
  预填为全部自动条目（有 primary 标记的条目优先做 primary；否则第一个自动
  条目）。面板里点「自动」直接切换，等价于设置页操作。
- **原生选择器**：桌面壳加 `tauri-plugin-dialog`（Rust 依赖 + capability +
  `@tauri-apps/plugin-dialog` JS），「浏览…」弹系统目录选择框。web 侧已有
  `@tauri-apps/api/core` invoke 的成熟桥（CodePanel/PinApp 同款），照
  opener 插件的三步走即可。**纯浏览器（pnpm dev 直开 :3000）无 Tauri 时
  按钮自动隐藏**，手输路径不受影响。
- 星标 = primary（bash cwd），规则与 live 一致：**首个挂载自动成为
  primary**，之后可改星。后端不会自动兜底（实测 `resolve_session_dirs`
  primary 为空时 cwd 保持会话文件目录）。
- ro/rw 切换走库级 `updateFolder`（挂载时继承条目权限，改权限改的是库）。
- 「管理目录库」跳 `settings/knowledge/folders`（沿用 `:189`）。

### 2.3 发送瞬间

`createAndSend` 建会话时把 pending 一并提交；成功后清空 pending（与
`setTarget(null)` / `homeModel` 重置同批，`ChatStream.tsx:724-725`）。
会话打开后 TopBar 的 chip 显示**同一批 id**——视觉无缝接管，用户感知不到
「先选后建」和「建后挂载」的差别。

## 3. 数据流与改动清单

```
首页 chip(本地 pending；初始值 = auto_mount 条目)
  → createAndSend (ChatStream.tsx:693)
      g.newSession(agentId, { ...homeModel,
        context_folders: pending.ids, primary_folder: pending.primary })
  → store.newSession (store.tsx:1105)  opts 增加两个透传字段
  → api.createSession (runtime.ts:90)  req 类型增加两个透传字段
  → POST /api/sessions  ← 建会话路径零改动
```

### 前端

| 文件 | 改动 |
|---|---|
| `ContextFoldersChip.tsx` | 泛化双模式：live（现有）+ pending（受控 `value/onChange`）；面板加目录库快选区（含自动挂载切换）；「浏览…」按钮（有 Tauri 才渲染） |
| `ChatStream.tsx` | chips 行挂 pending chip；`createAndSend` 透传 + 成功后清 pending；pending 初始化拉一次库（auto_mount 条目） |
| `store.tsx` / `runtime.ts` | `newSession` opts、`createSession` req 类型加 `context_folders`/`primary_folder` 透传 |
| `settings/knowledge/folders` 页 | 条目行加「自动挂载」开关（`updateFolder` 现成端点） |
| `types.ts` | `FolderEntry` 加 `auto_mount?: boolean` |
| `messages/{en,zh-CN}` | 复用 `shell.folders.*`，新增约 6 个 key（库快选、自动挂载、浏览） |

### 后端（仅 auto_mount 一个字段）

| 文件 | 改动 |
|---|---|
| `context_folders.py` | 库条目支持 `auto_mount: bool`；`update_folder` 白名单（`:145-152`）加一行；`create_folder` 接受可选初值 |
| API 层 | 透传即可（`update`/`create` 请求体是 dict，无需改 schema） |

### 桌面壳

| 文件 | 改动 |
|---|---|
| `apps/desktop/Cargo.toml` | 加 `tauri-plugin-dialog = "2"` |
| `apps/desktop/src/lib.rs` | 注册插件（`tauri_plugin_dialog::init()`） |
| capabilities | 授 `dialog:default`（允许目录选择；不给 open/save 完整权） |
| `apps/web/package.json` | 加 `@tauri-apps/plugin-dialog` |

三步与现有 `tauri-plugin-opener` 的接入完全同构（Cargo.toml:25 的注释即模板）。

## 4. 边界情况

| 情况 | 处理 |
|---|---|
| 路径已在库中 | `createFolder` 幂等（同路径返回已有条目，`context_folders.py:106`），不会重复入库 |
| 输入的路径磁盘上不存在 | 与 live 一致：入库成功但解析为 `missing`，面板显示灰行，工具层跳过 |
| 挂载后被移除 primary | primary 不在列表中时前端清空 primary |
| 用户选了挂载但没发送就走了 | 库里多了条目（可管理页删），无会话残留——与 live 挂载即入库一致 |
| 首页拖拽文件夹进来 | 现有行为是**复制进 uploads**（attachOne），不是挂载——保持不变，避免语义突变 |
| 自动条目过多 / 用户手动清空 | pending 就是普通本地状态，用户可增删；「手动改过」不记忆（下次仍按 auto 条目预填） |
| 纯浏览器无 Tauri | 「浏览…」隐藏；快选/手输照常 |
| 自动条目指向已删除目录 | 预填后显示灰行（missing），不影响发送 |

## 5. 决策记录

2026-10-08 评审拍板，三个可选项**全部进 v1**：

- **目录库快选**：做。补 live chip 手输路径的洞，两模式同组件同受益。
- **自动挂载标记**：做。库条目 `auto_mount` + 设置页开关 + 首页预填。
- **原生目录选择器**：做。桌面壳加 `tauri-plugin-dialog`（照 opener 模板），
  纯浏览器环境降级隐藏。

## 6. 验收

- 首页选 2 个目录（其一标星）→ 发第一条消息：会话 TopBar chip 显示同 2 个
  id；agent 第一轮的工具调用可直接读写挂载目录；bash cwd = 星标目录。
- 挂载目录含 `GINNO.md`/`CLAUDE.md`：首条消息的系统提示词已含规则文本
  （WorldState `folder_rules`），不是第二轮才注入。
- ro 目录：文件写工具拒绝写入并给出明确错误；rw 目录可写。
- 目录库快选：库里有 N 条时，pending/live 面板都能一键挂载，不重复入库。
- 自动挂载：标记 2 条后回首页，pending 预填 2 条；发消息即生效；取消某条
  的自动标记不影响已建会话。
- 「浏览…」在桌面应用里弹系统目录选择框，选中即入库并加入 pending；
  纯浏览器打开 :3000 时按钮不出现。
- 未发送离开首页：pending 不残留到下一次（会话列表/其他入口不受影响）。
- 后端重启后恢复会话：挂载列表与 primary 不变（`_ensure_session` 既有行为）。
- 无 sidecar / 库为空：chip 优雅降级（数量模式），不阻塞输入。
