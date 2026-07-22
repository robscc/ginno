# Ginno 产品使用说明

> 适用版本：当前主干（0.1.0，骨架→功能迭代阶段）。
> 本文是**面向使用者**的完整说明，覆盖界面操作、配置与文件机制。
>
> **关于“完成度”的诚实声明**：Ginno 仍在快速迭代，界面上有少量**占位/未接通**的元素。由于目前**没有账号体系**，左下角曾出现的 “头像 / 名字 / Pro Plan / Sign out” 占位**已移除**（见第 11 节）。全文用图例标注每一项的真实状态，避免误导：
>
> | 标记 | 含义 |
> |---|---|
> | ✅ | 已实装、可用 |
> | 🧩 | 后端已实装，但**无 UI**，需改配置文件 / 调 API |
> | 🚧 | 占位 / 仅有界面壳，**行为未接通**（见第 11、13 节） |
> | 🔮 | 设计/路线中，尚未实现 |

---

## 目录
1. [Ginno 是什么](#1-ginno-是什么)
2. [安装与启动](#2-安装与启动)
3. [界面总览](#3-界面总览)
4. [会话 Sessions](#4-会话-sessions)
5. [对话 Chat](#5-对话-chat)
6. [Agent](#6-agent)
7. [右栏面板（TODO / Workflow / Artifacts）](#7-右栏面板)
8. [知识库 Knowledge Base](#8-知识库-knowledge-base)
9. [设置 Settings（逐标签）](#9-设置-settings)
10. [高级：文件与配置（Power User）](#10-高级文件与配置)
11. [账号与个人资料（现状说明）](#11-账号与个人资料)
12. [常见问题 FAQ](#12-常见问题-faq)
13. [已知限制与路线](#13-已知限制与路线)

---

## 1. Ginno 是什么

Ginno 是一个**个人 AI Agent 桌面应用**，形态借鉴 Claude Code：多 Agent、工具调用、权限确认、技能（Skills）、MCP 工具、Hooks、记忆、知识库。

- **本地优先**：所有状态都以**文件**形式存在本机的 `~/.ginno/`，**没有数据库、没有云同步、没有账号体系**（见第 11 节）。
- **三层结构**：Tauri 原生壳（Rust）+ Next.js 界面 + Python 运行时（FastAPI + LangGraph，作为 sidecar 进程）。界面与运行时通过 `http://127.0.0.1:8787` 的 HTTP + WebSocket 通信。
- **一个对话 = 一个会话（Session）**；每个会话可临时指定由哪个 Agent 回答；同一会话内可中途切换 Agent，历史共享。

---

## 2. 安装与启动

### 2.1 开发模式（推荐先跑通这个）
前置：Node ≥ 20、pnpm ≥ 9、Python（建议用 `uv`）。
```bash
# 安装依赖
pnpm install
cd packages/runtime && uv sync && cd ../..

# 一键起三个进程（runtime :8787 + web :3000 + tauri 壳）
pnpm dev

# 或单独起
pnpm dev:runtime   # Python 运行时 :8787
pnpm dev:web       # 界面 :3000（开发时界面连 :8787 的 API）
pnpm dev:desktop   # Tauri 壳
```
> 只想看界面 + API（不要 Tauri 壳）：先 `pnpm --filter @ginno/web build` 生成静态产物，再 `pnpm dev:runtime`，然后浏览器打开 **http://127.0.0.1:8787** —— 运行时会**同源**托管界面与 API（这也是打包后的工作方式）。

### 2.2 打包成桌面应用
```bash
pnpm build:web       # 界面静态导出
pnpm build:runtime   # PyInstaller 把运行时打成 sidecar 二进制
pnpm build:desktop   # Tauri 产出 .dmg / .msi / .AppImage
```

### 2.3 首次启动发生了什么
运行时启动时会在 `~/.ginno/` 自动初始化（`ensure_layout`）：
- 创建目录树（`memory/ projects/ skills/ mcp/ hooks/ agents/ workflows/ knowledge/ vectorstore/ logs/` 等）；
- 写默认 `settings.json`（含模型 providers、permissions、hooks、knowledge 等空/默认块）；
- 种子 **3 个 Agent**：`dev` / `research` / `writer`；
- 种子 **7 条示例 TODO**、**1 个示例 Workflow**（`pr-triage`）；
- 写 `config.json`（主题）、`MEMORY.md`（记忆索引）、`mcp/mcp.json`（空 MCP 注册表）。

> 旧版 `settings.json`（单一 `model`+`env` 形态）会被**就地迁移**为新的 `providers` 形态，**不会覆盖**你已填的 key/base_url。

---

## 3. 界面总览

```
┌────────────────────────────────────────────────────────────────────────────┐
│ 左导航 (w-64)            │ 顶栏 TopBar：会话标题 · 当前 Agent · Running/Idle · [model] · ⋮🚧 │
│ ┌──────────────────────┐ │ ┌────────────────────────────────────────────────────────────────┐ │
│ │ GinnoWork            │ │ │                                                                │ │
│ │ ▾ Sessions（会话列表）│ │ │                     聊天区 ChatStream                           │ │
│ │ ▾ Agents（在场指示）  │ │ │   用户气泡（右：文本·图片） / Agent 气泡（左：Markdown文本·图片·思考·工具·卡片·引用·流程）│
│ │                      │ │ │   权限确认条（出现时，置于输入框上方）                            │ │
│ │                      │ │ │   输入区：[Ask Dev][Ask Research][Ask Writer][+ New Session]    │ │
│ │                      │ │ │            文本框 · 📎🚧 🚧 “/” · 发送➤                        │ │
│ ├──────────────────────┤ │ └────────────────────────────────────────────────────────────────┘ │
│ │ 📖 Knowledge Base    │ │                                              │ 右栏 (w-380)         │
│ │ ⚙ Settings           │ │                                              │ [TODO][Workflow][Artifacts]│
│ └──────────────────────┘ │                                              └────────────────────┘
└────────────────────────────────────────────────────────────────────────────┘
```
- **左导航**：`Sessions`（点击切换会话、当前会话高亮；标题右侧的 **＋** 用于新建会话）、`Agents`（只读在场指示：当前会话的 Agent 或正在运行的显示绿色 Active，否则 Idle）、底部 `Knowledge Base` / `Settings` 入口；**无账号区**（当前无账号体系，见第 11 节）。
- 页面路由：`/`（工作区/聊天）、`/kb`（知识库）、`/settings/<tab>`（设置）。

---

## 4. 会话 Sessions

- **自动创建**：首次进入工作区若无会话，会用第一个 Agent 自动新建一个。
- **切换**：点左导航的会话项；若不在工作区页会跳回 `/`。**切回某会话时会自动加载它的历史消息**（你的提问 + Agent 回复，含思考 / 工具 / 卡片 / 引用等块），不再像以前那样切走再切回就空白。
- **标题规则**：
  - 未自定义时，标题**自动跟随当前 Agent**（如 `Dev Agent session`），切换 Agent 会改名；
  - 一旦你**手动改名**（或显式设置标题），标题即“固定”，后续切换 Agent 不再自动改名。
  - （目前 UI 暂未提供重命名入口；标题随 Agent 自动生成，或通过 API `PATCH /sessions/{id}` 设置。）
- **新建**：两处入口，都用第一个 Agent 新建并切换到新会话——① 左导航 **Sessions** 标题右侧的 **＋**（在任意页面都可用，最方便）；② 聊天输入区上方的 **+ New Session**。
  > 若**没有启用/配好任何模型提供商**，新建会失败；此时左导航 Sessions 列表下方会出现一条**琥珀色提示**（显示后端原因，如 `provider … disabled` / `API Key 为空`），**点击它直达 设置 → 模型 API**。配好后再点 ＋ 即可。
- 会话历史按 `project slug`（界面固定为 `default`）存于 `~/.ginno/projects/default/sessions/`，**重启运行时不丢失**（文件 checkpointer 按 `thread_id` 恢复）；界面通过 `GET /sessions/{id}/history` 读取，并以与实时流**完全一致的“块”结构**渲染（文本/思考/工具/卡片/引用/流程）。
  > 说明：历史里每条 Agent 气泡显示的是该会话**当前** Agent 的 persona（实时流中“中途 Ask 其它 Agent”的逐轮身份未单独持久化，属已知小限制）。

---

## 5. 对话 Chat

### 5.1 发送与“一次一轮”
- 文本框输入，**Enter 发送**、**Shift+Enter 换行**，或点右下发送➤。
- 有**发送锁**：一轮未结束（仍在流式 / 有未决工具 / 弹着权限确认）时不能发下一条，发送按钮置灰。
- 未连上 sidecar 时发送按钮置灰（左下/顶栏状态会变）。

### 5.2 回复里会出现的“块”
| 块 | 说明 | 状态 |
|---|---|---|
| 文本 | Agent 的自然语言，**按完整 Markdown 渲染**（见 5.3），流式追加，末尾有光标动画 | ✅ |
| 图片 image | 模型/工具产出的图片，缩略图网格，点击放大预览（见 5.5） | ✅ |
| 思考 thinking | 模型的思考内容，**独立可折叠面板**（左侧紫色强调边 + 思考图标，流式时展开并显示“思考中…”，结束后自动折叠、可点开看全文），来自 `thinking`/`reasoning_content` | ✅ |
| 工具调用 tool | `tool · <name> ✓ · N 行 · M 字符` 的**可折叠条**：输出较长时默认收起，展开后放在带内滚动的限高容器里；执行中显示转圈 `name…` | ✅ |
| 卡片 widget | `render_widget(kind="stat_list", data={title, items:[{label,value,status}]})` 渲染成状态列表卡 | ✅ |
| 引用 ref | `attach_ref(kind, name)` 在气泡**下方**生成可点击 chip（file/doc/workflow/link） | ✅ |
| 流程 workflow | `workflow_run` 在气泡内嵌入实时步骤进度块 | ✅ |

> 结构化输出（卡片/引用/流程）是“静默”的：不会作为普通工具气泡出现，模型只回一句人类摘要即可。

### 5.3 文本的 Markdown 渲染 ✅
Agent 的文本块用完整 Markdown 渲染，覆盖以下特性：标题（h1–h4）、**加粗** / *斜体* / ~~删除线~~、行内 `代码`、引用块、无序 / 有序列表、链接（新标签打开）、表格、分隔线，以及围栏代码块。
- **代码块**：顶部带语言标签（如 `PYTHON`）与**复制**按钮，内容带语法高亮（高亮配色随深/浅主题自适应）。
- **任务清单（TODO list）与普通列表区分**：`- [ ]` / `- [x]` 渲染成带勾选框的待办样式，已勾选项带删除线；普通 `-` / `1.` 列表则用常规的项目符号 / 编号，二者视觉明显不同。

### 5.4 思考面板 ✅
支持“扩展思考”的模型（如带 `reasoning_content` 的 OpenAI 兼容网关）会把推理过程放进**独立面板**，与正文强力区分：
- 流式生成时面板展开、图标脉动并显示“思考中…”；
- 本轮思考结束后**自动折叠**为一行（显示字数），点击可随时展开/收起，展开后内容在限高框内滚动。

### 5.5 图片与多图预览 ✅
- **输入**：在输入框**粘贴截图**、**拖拽图片**进来，或点左下 **📎** 选择图片，可一次多张；上传前会在本地自动压缩（大图缩到长边 1600px、JPEG），发送前以缩略图显示在输入框上方，可逐张 ✕ 移除。
- **输出**：你发的图、以及模型/工具返回的图，都以缩略图显示（单图大图、多图网格），**点击任意一张打开全屏预览**：`Esc` 或点遮罩关闭，多张时可用左右箭头 / 按钮翻页，右上角显示 `当前/总数`。

### 5.6 权限确认（Allow / Deny）✅
当某次工具调用命中“需询问”的策略时，输入框**上方**弹出黄色确认条，显示 `tool` 名与参数 JSON，提供 **Allow / Deny**：
- **Allow**：执行该工具，继续本轮；
- **Deny**：拒绝，工具返回“被拒”，Agent 据此改口/换工具。
- 何时会弹：见第 10.2 节权限策略（默认：`Bash`/`Write`/`Edit` 及“写 vault”的 MCP 工具会询问；`read/glob/grep` 及只读 vault 工具放行；`rm -rf *`/`sudo *`/写 `~/.ssh`、`~/.gnupg` 直接拒绝）。
- 注：`todo_*` / `workflow_*` / `artifact_*` / `render_*` 这些“产品内”工具**从不弹权限**。
- ⚠️ **特权模式默认开启**（见 9.6）：开启时**不会弹任何确认**，所有工具直接执行。要看到 Allow/Deny 弹窗，需先在 设置 → 通用 关闭特权模式。

### 5.7 路由到指定 Agent ✅
输入区上方的 **Ask Dev / Ask Research / Ask Writer** 按钮：点选后本轮由该 Agent 回答（再次点取消选择，回退到会话默认 Agent）。切换会同步更新会话的 Agent 与（自动标题时的）标题。

### 5.8 斜杠命令 / Skills ✅
在输入框以 `/<技能名>` 开头发送，会把该技能的 `SKILL.md` 正文注入为本轮指令（后接你写的诉求）。技能在 **Settings → Skills** 管理（见 9.2）。输入框里的 “`/`” 只是个提示标记，本身不可点。

### 5.9 顶栏 ✅ / 🚧
- 标题、当前 Agent 胶囊、`Running/Idle` 状态点；
- 右侧 **model 按钮**：显示当前模型标签，点击跳到 **Settings → 模型 API**；
- 右侧 **⋮** 按钮：✅ 打开会话菜单——**重命名会话**（写回标题，之后不再随 Agent 自动改名）与**复制会话 ID**。

### 5.10 输入区按钮 ✅ / 🚧
- **📎（附件）**：选择图片附加到本轮（等同粘贴 / 拖拽，详见 5.5）。
- **⌨（斜杠）**：✅ 在输入框插入 `/` 以触发斜杠命令（见 5.8）。

---

## 6. Agent ✅

### 6.1 三个内置 Agent 的差异
| Agent | 定位 | 工具范围（`tools_allow`） |
|---|---|---|
| **Dev** | 代码 / PR / 调试 / 仓库操作 | `*`（全部） |
| **Research** | 搜集与综合信息、读文档/笔记、给来源；**不改文件** | `read_file, glob_files, grep_files, mcp_*, todo_list`（只读 TODO） |
| **Writer** | 起草/润色文档与沟通 | `read_file, write_file, edit_file, glob_files, mcp_*, todo_*` |

> `tools_allow` 支持 fnmatch 通配（如 `mcp_*`、`todo_*`、`*`）。**越权工具会被直接拦截**（不弹权限，直接告诉模型“该工具不可用”）。

### 6.2 每个 Agent 的独立记忆 ✅
每个 Agent 有自己的 `~/.ginno/agents/<id>/MEMORY.md` + `memory/`，会在该 Agent 回答时注入其 system prompt（“private to this agent”）。

### 6.3 管理 ✅
**Settings → Agent 管理**：可编辑每个 Agent 的 `system_prompt` / `tools_allow` / `provider` / `model`，可 `delete`、可新建（填 `id` + `name`）。

---

## 7. 右栏面板

切换标签：`TODO` / `Workflow` / `Artifacts`。

### 7.1 TODO ✅
“Daily TODO”——全局每日清单：
- **筛选**：All / High / Medium / Low；
- **勾选**：点方框切换完成（乐观更新 + 写回后端）；
- **+ New**：行内新增（默认 priority=medium、category=Dev）；
- 每条显示优先级点、分类 chip、`due`；底部 **Today's progress** 进度条。
- **Agent 也能改**：对拥有 `todo_*` 的 Agent 说“加个待办/把 X 标记完成”，它会调用工具修改，右栏与聊天都会刷新。
- 首次启动种子了 7 条示例。

### 7.2 Workflow ✅（查看） / 运行靠 Agent
- 显示**实时运行**的进度树（每个 run 的步骤状态：pending/running/done/failed + 进度条）；
- 空态提示：“让 Agent 跑一个 workflow（例如 ‘run the PR Triage workflow’）”；
- 底部列出已有**配方（definition）**名称。
- **运行**：在对话里让 Agent 用 `workflow_run` / `workflow_step` 驱动；**配方的增删**在 **Settings → Workflows**（见 9.5）。

### 7.3 Artifacts ✅（只读）
自动登记的产物列表：你在对话里 `attach_ref` / `artifact_register`、或 Agent 写/引用文件时，会出现在这里（按 file/doc/workflow/link 显示图标）。当前为**只读展示**。

### 7.4 Memory ✅（全局记忆）
显示 `~/.ginno/MEMORY.md` 的内容（由自动总结提炼而来）+ 当前 pool 计数（待总结的对话轮数）+ **总结**按钮。
- **自动捕获**：每轮 Agent 回复结束后，其文本（经 sanitize 去除注入标记）自动追加到 `memory/pool/*.jsonl`；
- **手动总结**：点「总结」按钮（或调 `POST /memory/summarize`），用 LLM 把 pool 摘录与现有 MEMORY.md 合并、提炼可复用知识，写回 MEMORY.md 并清空 pool；
- **自动注入**：MEMORY.md 内容在每轮注入所有 Agent 的 system prompt（`<injected_memory>` 包裹），与 Agent 私有记忆并存。

---

## 8. 知识库 Knowledge Base ✅（页面） / 🧩（配置）

### 8.1 它是什么
把**你的 Obsidian vault** 当作知识库（LLMWiki 方案）：运行时扫描 vault、建立内存索引；**每轮对话按你提问的相关性**检索 top-K 条目，注入到 Agent 的 system prompt，让回答“带着你的笔记知识”。检索**不依赖 embedding/向量库**（多信号词法打分 + 链接图加成，支持中英文）。

此外提供**编译 + 关联发现**：`Build wiki` 把 `raw_dir/` 的原始文档编译成 `wiki_dir/` 的概念页/汇总页/索引（确定性正则，零 LLM），并跑**关联引擎**（TF-IDF 余弦 0.35 + 标签 Jaccard 0.25 + 共被引 0.20 + 时间 0.10 + 层级 0.10）自动发现相关页、聚类、可合并候选。
> **索引范围**：检索/关联索引**只索引 `wiki_dir` 子树**——编译后的 Wiki 页才是“知识”，`raw_dir`、`research`、`memory`、vault 根的零散笔记都不进索引。所以**导入一个已编译好的 LLM Wiki（如 `Molly/Wiki`）无需重新编译**，直接索引即可用。编译器的自动关联同样只看 `wiki_dir`，**绝不改写你的原始文档**；同源文档产出的概念互为“兄弟”会被刻意跳过（不互相关联）。

### 8.2 页面功能（`/kb`）
- **统计条**：pages / links / tags / 上次索引时间 / vault 路径；
- **搜索框**：中英文皆可，结果卡显示**相关度%**、标签、来源路径、**命中信号**、摘要片段；
- **标签云**：点标签按 tag 过滤；
- **全部页面** tab：列出所有索引页（标题/标签/路径）；
- **Build wiki** ✅：把 `raw_dir/` 编译成 Wiki（概念页 + 汇总页 + INDEX），完成后顶部显示“扫描/新建/更新/自动关联/用时”；
- **Rebuild index**：仅重建内存索引（不编译）；
- **发现** tab ✅：显示关联边总数，并提供「查看某页的相关」查询（相关页 + 分数 + 主导信号），以及**强关联 / 聚类 / 可合并候选 / 孤立页**四个分区。
  > 说明：自动编译出的概念页，其“强关联/聚类”通常为空（编译产物很难达到 ≥0.8 / 聚类密度阈值）；这两个分区在**手写 Wiki 页**（标签与链接精心编排）时才有内容。最常用的是「查看某页的相关」。
- **导入面板（未启用时显示）** ✅：填入 vault 路径 → **检测**（自动识别 `Molly/Wiki` 等 `<命名空间>/Wiki` 布局，回显 Wiki 页/Raw 篇数）→ **导入并索引**（写配置 + 索引，**不编译**，已编译的 Wiki 立即可用）；面板底部链接到 **设置 → 知识库** 做细调。
- **未启用时**：即显示上面的导入面板（不再需要手改 `settings.json`）。

### 8.3 如何启用与配置 ✅
两种入口，都会写到 `settings.json` 的 `knowledge` 块：
- **KB 页导入面板**（最快）：填 vault 路径 → **检测** → **导入并索引**；
- **设置 → 知识库**：vault 路径 + 检测 + 启用 + 自动注入 + top-K + 最小相关度 + `wiki_dir`/`raw_dir` + 「保存并索引」。

也可直接调 API：`GET /kb/wiki/probe?path=`（只读检测布局与页数）、`PUT /kb/wiki/config`（写配置）、`POST /kb/wiki/index`（索引）。`knowledge` 块字段：
```jsonc
"knowledge": {
  "enabled": true,
  "vault_path": "/你的/Obsidian/vault",   // 必填，指向 vault 根
  "raw_dir": "Ginno/Raw",                 // 新文档写这里
  "wiki_dir": "Ginno/Wiki",               // 自动编译产物，勿手写
  "research_dir": "Ginno/Research",
  "auto_inject": true,                    // 每轮自动检索注入
  "inject_top_k": 5,
  "inject_min_score": 0.3,
  "rescan_interval_s": 60,
  "use_semantic": false,                  // 语义检索开关，默认关；开启需 uv sync --extra rag
  "embedding_model": "",                  // sentence-transformers 模型（空=多语默认）
  "semantic_weight": 0.5,                 // 余弦相似度叠加到词法分的权重
  "capture": true,                        // 每轮把 assistant 文本捕获入 pool
  "auto_summarize": true,                 // pool 达阈值自动总结
  "pool_flush_threshold": 30,             // 触发自动总结的 pool 条数
  "summarize_model": "",                  // 总结用 provider（空=默认 provider）
  "memory_budget_chars": 3000             // 注入 MEMORY.md 的字符预算
}
```
导入**已编译**的 Wiki（如 Molly）时，`wiki_dir` 填检测到的命名空间目录（如 `Molly/Wiki`），`raw_dir` 填 `Molly/Raw`（没有就留空）；「保存并索引」后即可在 `/kb` 搜索、在对话中自动注入，**无需 Build**。只有当你新增原始文档、想把它编译进 Wiki 时才点 **Build wiki**。

**语义检索（可选）**：在 设置 → 知识库 勾选「语义检索」并「保存并索引」/ Build wiki 后，运行时会用本地 `sentence-transformers` 对 Wiki 页编码、把向量缓存到 `~/.ginno/vectorstore`（LanceDB），检索时按 **词法 + 余弦相似度** 融合排序（`semantic_weight` 控制语义占比）。该能力依赖 `uv sync --extra rag`；未装依赖、模型下载或编码失败时**自动退回纯词法检索**，不报错，也不影响 `use_semantic=false` 的默认路径。

### 8.4 自动注入行为
- **索引范围 = 整个 vault（除 `raw_dir` 与 `.obsidian` 等系统目录）**：所以你在 vault 任意目录写的成品笔记（如 `股市/`、根目录散记）都能被 `/kb` 搜到、列入“全部页面”、并在对话中注入；`raw_dir` 仅作为编译源，经 **Build wiki** 编成 wiki 页后才被检索。`wiki_dir` 只用于编译产物归类与 INDEX/关联图，**不再限制检索范围**。
- 启用后，每轮把**最近一条用户消息**作为 query 检索；
- 命中条目以 `## 相关知识` 注入（含相关度%、命中信号、摘要），并用 `<injected_wiki>` 包裹，**提示模型把它当“数据/参考”，而非指令**；
- **无关内容不会被注入**（低于 `inject_min_score` 的不出现）；
- 同时注入**目录规范**：新文档写 `raw_dir/`，`wiki_dir/` 由编译自动生成、不要手写。

### 8.5 检索/索引/编译/关联 API（🧩，供脚本/调试）
- 检索/索引：`GET /kb/wiki/search?q=&tag=` · `GET /kb/wiki/list` · `GET /kb/wiki/stats` · `POST /kb/wiki/index` · `PUT /kb/wiki/config`；
- 编译：`POST /kb/wiki/build`（全 vault）· `POST /kb/wiki/ingest {path}`（单文件，path 可为相对 vault 或绝对路径；越界报错）；
- 关联：`GET /kb/wiki/related?title=&top_k=` · `GET /kb/wiki/discover` · `GET /kb/wiki/orphans` · `GET /kb/wiki/backlinks?title=`；
- 检测/导入：`GET /kb/wiki/probe?path=`（只读：识别 `<ns>/Wiki` 布局并返回 `wiki_pages/raw_pages/has_index/total_md`，不写 vault）。
- （另：`GET /kb/servers`、`GET /kb/search`、`GET /kb/list` 走的是 **MCP vault 实时查询**，与上面的内存索引是两条路径。）

---

## 9. 设置 Settings

左导航 ⚙ → 进入。左侧标签栏分两组：

### 9.1 模型 API ✅
驱动 Agent 推理的提供商配置，三张卡：
- **Anthropic**：API Key（可显隐）、`默认模型`、`Base URL(可选)`、`Max Tokens`、`Temperature`、`Timeout`；
- **OpenAI**：API Key、`默认模型`、`Base URL(可选代理)`、`Organization ID`、`Max Tokens`；
- **自定义端点 (OpenAI Compatible)**：`端点名称`、`Base URL*`、`Model Name`、`API Key(可选)` —— 适配 Ollama / DeepSeek / Qwen / Groq / LM Studio 等。
每张卡有 **启用开关** 与 **验证** 按钮（真实发起一次最廉价调用，返回 未配置/验证中/已连接/失败）。编辑**失焦自动保存**。
> 至少启用并验证通过一个提供商，聊天才能跑通真实模型。
> **中转 / 自建网关用 Bearer 鉴权**：有些 Anthropic 协议网关（企业模型中转等）要求把 token 放在 `Authorization: Bearer …` 而不是 `x-api-key`（否则会 `401 缺少Authorization头`）。在 Anthropic 卡勾选 **Bearer 认证** 即可（旧配置仍兼容 `settings.json` 的 `bearer_auth`）。
> **Agent 绑到“未启用”的提供商会自动回退**：内置 Agent 默认 `provider` 为 `custom`；若它未启用，Ginno 会自动改用它**已启用的默认提供商**，避免“明明启用了模型却建不出会话”。想让某 Agent 固定用某提供商，在 **Agent 管理** 把它的 `provider` 改成对应 id 即可。顶栏的模型标签显示的是**本轮实际解析到的模型**，可据此核对。
> **联网搜索（模型自带）**：OpenAI / 自定义端点卡有 **联网搜索** 开关 —— 开启后 Ginno 会在请求体带 `enable_search: true`，让模型在需要时自动联网（典型如通义千问 compatible-mode）。点 **测试联网** 会发一个时效性问题并回显模型回答，便于确认你的端点是否真的支持；不支持的端点该字段会被忽略，不影响普通对话。Anthropic 协议无此参数，故不显示该开关。

### 9.2 Skills ✅
“一次性指令模板”，存于 `~/.ginno/skills/<name>/SKILL.md`：
- 列表显示 `/<name>`、`trigger`（user-invocable / model-invocable / both）、描述、`tools`；可 `delete`；
- **New skill**：填 `name`(kebab-case) + 带 frontmatter 的正文，`Create`。
- 触发：在聊天输入 `/<name>`（见 5.8）。

### 9.3 MCP 工具 ✅
- 顶部显示“已连接 N server(s)，M tool(s)”；
- 直接编辑 `mcp.json` 的 JSON（`mcpServers`，支持 stdio / sse / streamable-http），**Save & Reload** 保存并重连。
- 用途：接入外部工具；Obsidian MCP 也是知识库访问的实时路径之一。

### 9.4 Agent 管理 ✅
见第 6.3 节。

### 9.5 Workflows ✅（配方管理）
- 列表显示配方（name / id / 描述 / 步骤）；可 `delete`；
- **New workflow**：`name` + `description` + `steps`（JSON 数组，如 `[{"title":"Step 1"}]`），`Create`。
- 运行进度看右栏 Workflow（由 Agent 触发，见 7.2）。

### 9.6 通用设置 ✅ / 部分说明
- **默认模型提供商**：下拉选择（disabled 的会标注），实时写回；
- **主题**：`dark` / `light` 切换，立即生效并记忆在本地（`localStorage`）；
- **工作目录**：只读说明行。
  > 实际工具读写的工作目录由启动时的环境变量 `NEXT_PUBLIC_WORKSPACE` 决定（开发默认 `/tmp/gw`）；**设计约定**是 `~/workspace/<project>`，Agent 元数据在 `~/.ginno/projects/<slug>/`。界面此行展示的是约定，非当前生效值的回显。
- **特权模式** ✅（`bypass_permissions`，**默认开启**）：开启后 Agent 调用任何工具都**不再询问、不被权限策略拦截**（含 Bash/Write 等危险操作），即“允许执行一切命令”。关闭后恢复按权限策略询问/拦截（见 10.2）。
  > 注意：你配置的 `PreToolUse` Hook **仍会执行**——Hook 是自定义规则，始终生效；特权模式只跳过 `tools_allow` 越权检查与权限策略。默认无 Hook，故默认即“全放行”。该开关**实时生效**（下一次工具调用即按新值判定，无需重启/新建会话）。

### 9.7 通知 🚧（仅本地偏好）
一个“启用桌面提醒”复选框，**只写本地 `localStorage`**（`ginno-notify`）。
> ⚠️ **目前没有真实的桌面通知推送实现**——该开关暂不影响任何行为，属占位偏好。

---

## 10. 高级：文件与配置

### 10.1 `~/.ginno/` 目录结构
```
~/.ginno/
├── settings.json          # providers / permissions / hooks / knowledge（部分无 UI，见下）
├── config.json            # 主题等
├── MEMORY.md              # 全局记忆索引（✅ 自动总结 POST /memory/summarize 写入；每轮注入）
├── memory/                # 全局记忆条目（含 pool/ 待总结摘录）
├── agents/<id>.json       # 每个 Agent 的 persona/tools/model
│   └── <id>/MEMORY.md     # 该 Agent 的私有记忆（自动注入其 prompt）
├── projects/<slug>/sessions/   # 会话历史（文件 checkpointer，按 thread_id）
├── projects/<slug>/workflow_runs/  # workflow 运行实例（按项目）
├── skills/<name>/SKILL.md # 全局技能
├── mcp/mcp.json           # MCP 注册表
├── hooks/                 # hook 脚本（在 settings.json 引用）
├── workflows/<id>.json    # workflow 配方
├── knowledge/             # 知识库配置（索引/关联图在内存，不落盘）
├── cache/                 # 通用缓存
├── vectorstore/           # 语义向量缓存（LanceDB；use_semantic 开启后写入）
└── logs/
```

### 10.2 权限策略（🧩，编辑 `settings.json` 的 `permissions`）
格式为 `<工具名>(<参数 glob>)`，匹配顺序 **deny → ask → allow**，都不命中默认 `ask`：
```jsonc
"permissions": {
  "allow": ["read_file", "glob_files", "grep_files", "mcp_vault_read_*", "mcp_vault_search_*", "mcp_vault_list_*"],
  "deny":  ["Bash(rm -rf *)", "bash(sudo *)", "Write(~/.ssh/**)", "Write(~/.gnupg/**)"],
  "ask":   ["Bash(*)", "write_file", "edit_file", "mcp_vault_write_*", "mcp_vault_create_*"]
}
```
运行时实际判定顺序：**① 该 Agent 的 `tools_allow`（越权直接拦，不弹框）→ ② `PreToolUse` Hooks（可 block）→ ③ 上面的 permissions 策略**（ask 时弹 UI 确认）。**特权模式开启时（默认）跳过 ① 和 ③，仅 ② 仍执行**——即除你自定义 Hook 外，一切工具直接放行。

### 10.3 Hooks（🧩，编辑 `settings.json` 的 `hooks`）
事件：`SessionStart` / `UserPromptSubmit` / `PreToolUse` / `PostToolUse` / `Stop`。每个事件可配 `[{matcher, command}]`，运行时把上下文 JSON 喂给 `command` 的 stdin，读 stdout 的 `{"block":true,"reason":"..."} / {"inject":"..."} / {"rewrite":"..."}` 来拦截/注入/改写。
```jsonc
"hooks": { "PreToolUse": [ { "matcher": "bash", "command": "python3 ~/.ginno/hooks/guard_bash.py" } ] }
```

### 10.4 记忆机制 ✅
- **每 Agent 私有记忆**：`agents/<id>/MEMORY.md`，回答时自动注入（✅）；
- **全局记忆 / 自动总结**：每轮 Agent 回复自动捕获到 `memory/pool/`；点右栏 Memory 标签的「总结」按钮（或 `POST /memory/summarize`）用 LLM 提炼合并到 `MEMORY.md`，并清空 pool；MEMORY.md 在每轮注入所有 Agent（`<injected_memory>`）。

### 10.5 Skills / MCP / Hooks 的关系
- **Skills** = 可复用的“指令模板”（注入 prompt / 由 `/<name>` 触发）；
- **MCP** = 给 Agent 接**外部工具**（运行时把 MCP 工具包装成普通工具）；
- **Hooks** = 在关键事件**外挂脚本**做拦截/审计/改写。三者正交、可叠加。

---

## 11. 账号与个人资料

> 你之前看到的“有账号逻辑、但没完整实现”的部分——**现已处理**：既然没有账号体系，界面上的账号占位已**移除**。

**现状**：
- **界面上已无账号元素**：左导航底部不再显示头像 / 名字 / `Pro Plan` / `Sign out`（它们曾是写死的占位，现已删除，改动见 `apps/web/src/components/shell/AppShell.tsx`）；
- **没有登录页 / 没有鉴权 / 没有多用户 / 没有订阅**：运行时不校验任何用户，启动即可用；所有数据都在本机 `~/.ginno/`，**单用户、本地**；
- 因此也**没有任何“退出登录”行为**可言。

**为什么移除而非补全**：Ginno 定位是**单机个人** Agent，第一阶段不需要账号即可完整使用；保留一个假的 “David Chen / Pro Plan / Sign out” 只会误导，故直接删除。

**若将来真要账号，需要补**（路线，非现状）：
1. 登录/注册与凭证存储（或对接 SSO）；
2. 运行时的鉴权中间件 + 会话/令牌；
3. 用户模型与“数据归属”（把 `~/.ginno/` 的本地数据与用户绑定，或改云端存储）；
4. 在左导航重新加入**真实**的用户信息入口与可用的“退出登录”；
5. 多用户下的权限/隔离（尤其知识库的文档级 ACL，目前默认全员可读）。

> 简言之：**当前没有账号功能，界面也不再假装它有。**

---

## 12. 常见问题 FAQ

- **界面打不开 / 聊天转圈连不上**：确认运行时在跑（`pnpm dev:runtime` 或 sidecar 已起），浏览器/壳访问 `http://127.0.0.1:8787/health` 应返回 `{"ok":true}`。界面对 sidecar 有 60 次×500ms 的启动等待，刚启动稍等即可。
- **发消息没反应 / 报错**：多半是**没有启用并验证通过任何模型提供商**（Settings → 模型 API）。先用“验证”确认“已连接”。
- **点“新建会话”没反应**：现在不会静默失败了——若因未配置模型而失败，左导航 Sessions 下会出现**琥珀色提示**，点击直达 设置 → 模型 API（见第 4 节）。若提示是 `401`/“缺少 Authorization 头”，按 9.1 给 provider 加 `bearer_auth: true`。
- **工具一直弹权限 / 被拒绝**：检查 `settings.json` 的 `permissions`（10.2）与该 Agent 的 `tools_allow`（越权工具会被直接拦，不弹框）。
- **Knowledge Base 显示“未启用”**：用 **设置 → 知识库** 或 KB 页导入面板保存配置会**立即生效**（`PUT /kb/wiki/config` 会刷新索引缓存，无需重启）；若直接手改 `settings.json` 文件，则需点 **Rebuild index** 或重启运行时以刷新缓存。
- **主题/通知设置“没存住”**：主题存 `localStorage`（换浏览器/清缓存会丢）；通知开关目前只是本地偏好、无实际效果（9.7）。
- **重启后历史还在吗**：在。会话历史走文件 checkpointer；但**内存里的会话对象**重启后会在你下次打开该会话时从磁盘重建。
- **我改了 MCP / Skills 没生效**：MCP 需 **Save & Reload**；Skills 新建/删除后列表会刷新，运行时会重新加载。

---

## 13. 已知限制与路线

| 项 | 状态 | 说明 |
|---|---|---|
| 账号 / 登录 / Sign out / 订阅 | — | **界面无账号元素**（占位已移除）；登录/鉴权本身未实现，见第 11 节 |
| 桌面通知真实推送 | 🚧 | 仅有本地偏好开关 |
| 附件📎 / 快捷键⌨ / 顶栏⋮ | ✅ | 📎 附加图片；⌨ 插入 `/` 触发斜杠命令；⋮ 会话菜单（重命名 / 复制 ID） |
| 会话重命名 UI | ✅ | 顶栏 ⋮ → 重命名会话（写回后标题不再自动跟随 Agent） |
| 多项目（project slug） | 🚧 | 界面固定 `default` |
| 工作目录回显 | 🚧 | 通用设置该行展示约定值，非生效值回显 |
| 知识库 Settings 标签 | ✅ | 设置 → 知识库 已实现（检测 / 保存 / 重建索引） |
| 权限 / Hooks 编辑 UI | ✅ | 设置 → 权限策略 / Hooks 可视化编辑（读写 settings.json） |
| 记忆自动总结 / 全局注入 / 记忆工具 | ✅ | 每轮自动捕获；右栏 Memory 标签「总结」按钮 / `POST /memory/summarize` 提炼；MEMORY.md 每轮注入 |
| 知识库编译器 raw→wiki / 关联图 / Build wiki | ✅ | KB 页 **Build wiki** / `POST /kb/wiki/build`（无 `/kb build` 命令）；关联图 + 发现页已实现 |
| 经验循环（co-copilot 式抽取→晋升） | 🔮 | P3 路线 |
| 语义检索（LanceDB / embedding） | ✅ | 已接通：`use_semantic` + 本地 sentence-transformers + LanceDB 缓存，词法+余弦融合；需 `uv sync --extra rag`，否则自动退回词法 |
| Artifacts / Workflow 的 UI 操作（增删改/手动运行） | 🚧/✅ | Artifacts 只读；Workflow 运行靠 Agent、配方在设置增删 |

---

### 一句话总结
Ginno 现已可用：**多 Agent 对话 + 工具/权限/技能/MCP + TODO/Workflow/Artifacts 右栏 + 知识库检索注入 + 知识库编译/关联 + 全套设置（含知识库标签）**，全部本地、文件化、无账号（账号占位已移除，见第 11 节）。仍为占位/待接通的：**桌面通知真实推送、多项目（project slug）**——以本文图例为准。需要我把任意 🔮/🧩 项（例如语义召回 `memory.recall`、多项目、或账号登录骨架）落到代码里，告诉我即可。
