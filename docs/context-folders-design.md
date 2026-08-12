# 上下文目录设计（把本地目录挂载为 Agent 上下文）

> 状态：**已定稿**（2026-08-12 拍板：绑定模型选 **C 文件夹库+会话挂载**；新挂载目录**默认 rw**；范围外访问 **M0 维持现状**不加收紧开关）。目标：给 Ginno 引入"上下文文件夹/项目"能力——除了 `~/.ginno` 这个自有数据目录，用户可以**主动把任意本地目录**（代码仓库、笔记库、文档堆）挂载进会话，让 Agent 直接读写、检索、执行，方便对代码/项目的操作。
>
> 依据：三路竞品深度调研（CLI Agent / IDE / 桌面 AI 助手，2026-08）+ Ginno 代码库现状勘察。

## 0. TL;DR

- **竞品收敛出的 2026 共识架构 = 混合式**：(1) **连接文件夹**（live 引用、按需读取、权限分级）用于工作文件；(2) **可选索引知识库**（embed→RAG）只用于大型静态语料；(3) **检索模式必须显式**，绝不静默切换（Claude Projects 因静默降级 RAG 被用户骂）。
- **Ginno 现状**：project 只是 slug（UI 硬编码 `default`），每会话工作目录 = `~/.ginno/projects/<slug>/sessions/<sid>/`；**无任何附加目录概念**；工具层非沙箱（绝对路径透传，仅 `_path_denied` 黑名单）；知识库 vault 是唯一"外部目录"先例。
- **推荐方案（选项 C：文件夹库 + 会话级挂载，已选定）**：全局 `folders.json` 注册目录库；会话按需挂载 N 个目录并可指定一个为**主工作目录**（bash cwd + 相对路径基准切过去）；每目录带 `ro/rw` 访问级（**默认 rw**）+ "是否加载其规则文件"开关；上下文注入走**按需读取优先**（扩展现有文件工具的可达范围 + EnvironmentSection 宣告），v1 **不做 RAG 索引**。
- **安全核心**：`access ≠ config`（挂载目录只给文件访问，绝不加载它的 settings/hooks/脚本）；白名单化可达路径；越界访问触发权限询问并可"总是允许"。
- **落点**：`folders.json` + `/api/folders*` + session meta 扩展 + `build_builtin_tools(workspace, context_dirs)` + `EnvironmentSection` 扩节 + TopBar 挂载 chip + Settings 管理页。M0~M2 三期。

---

## 1. 竞品分析（事实基础）

### 1.1 三条技术路线

#### 路线 A：CLI Agent（Claude Code / Codex / Gemini CLI / Aider / OpenCode / Cline）

| 产品 | 默认范围 | 附加目录机制 | 权限语义 | 规则文件发现 |
|---|---|---|---|---|
| **Claude Code** | cwd | 三通道：`--add-dir` 启动参数、`/add-dir` 会话中、`permissions.additionalDirectories` 持久化 | 附加目录 = 文件访问权；**不加载**其 settings/hooks（access ≠ config）；其 CLAUDE.md 需显式 opt-in | CLAUDE.md 层级：enterprise→user→project，向上遍历到根；子目录 CLAUDE.md **懒加载**（碰到该目录文件时才载入）；`@import` 越界需首次批准 |
| **Codex CLI** | 启动目录 | `--add-dir`、`writable_roots` 配置 | **沙箱边界 = 工作区成员资格**：加目录即扩展可写围栏；审批策略与沙箱模式是独立两轴 | AGENTS.md：全局→根→嵌套，全部拼接、**就近优先** |
| **Gemini CLI** | cwd | `--include-directories`、`/directory add`、`includeDirectories` 配置 | Trusted Folders 信任机制（不信任目录跑安全模式） | GEMINI.md 向上遍历合并；**有开关控制"是否从附加目录加载其 GEMINI.md"** |
| **Aider** | git repo | `/add`（可编辑）、`/read`（**只读引用，可来自文件系统任意处**）、`/drop` | 无 OS 沙箱，靠范围纪律 + git 自动提交回滚 | 无层级规则；惯例是 `--read CONVENTIONS.md` |
| **OpenCode** | 启动目录 | 无原生 add-dir；`external_directory` 权限类别 + 社区插件 | 把"工作区之外"当作**权限类别**而非成员资格变更 | AGENTS.md 全局+项目根 |

**Aider 的 repo-map 值得单列**：tree-sitter 提取符号 → 以"当前会话文件"为个性化种子做 PageRank 排序 → 压进 token 预算（默认 ~1024，自动伸缩）。这是"附加了 1 万文件的大仓库怎么不撑爆上下文"的标准答案。

#### 路线 B：IDE（Cursor / Windsurf / Zed / Copilot / JetBrains）

| 产品 | 默认范围 | 附加目录机制 | 检索 | 关键教训 |
|---|---|---|---|---|
| **Cursor** | workspace + 环境上下文 | `@folder` mention、拖拽、多根工作区（3.2 起 agent 原生支持多文件夹会话） | embed 索引优先（chunk hash 缓存、增量同步）+ agent 搜索工具混合 | `.cursorignore`（禁 AI 访问）vs `.cursorindexingignore`（仅不索引）**两级 ignore**；多根曾出现规则跨根重复 bug |
| **Windsurf** | workspace + memories | `@directory`、Add Folder to Workspace | 专有 embedding，**有"索引最大文件数"显式上限** | 多根下 @mention 曾看不到附加根目录 |
| **Zed** | **每个 agent 线程绑定一个工作目录**（worktree 隔离） | `@dir` mention、+ 菜单（仅项目内；加外部目录至今是 open request） | **纯按需**：无 embedding 索引，agent 用 grep/read 工具探索；`/context` 显示 token 占用、`/compact` 压缩 | "哪个目录属于哪个会话"的最干净模型；线程按项目分组 |
| **Copilot** | workspace 语义索引 | `#file`/`#folder`、回形针 Attach Context 选择器、拖拽；附件渲染为**可移除 chip** | 本地语义索引 + grep 混合 | 从"自动附带当前文件"**撤回**为显式 `#editor`——explicit beats ambient |
| **JetBrains** | project + Codebase 开关 | 附件类型化：file/folder/image/**symbol/commit** | 云端 embedding（只存 embedding 不存源码，作为隐私卖点） | 检索开关做成一键 toggle |

#### 路线 C：桌面 AI 助手（Cherry Studio / AnythingLLM / ChatBox / ChatWise / LobeChat / Open WebUI / Claude Projects+Cowork）

三种架构：

1. **索引知识库**（copy-in → chunk → embed → RAG）：Cherry Studio、AnythingLLM、ChatBox、LobeChat、Open WebUI。Cherry 支持**直接添加文件夹目录**入库，但文档明说这是"托管快照"不是实时视图——会过期。
2. **项目知识自动降级**：Claude Projects——文件全量注入直到逼近上下文上限，然后**自动切 RAG（宣称 10×容量）**。用户强烈抱怨静默切换导致"知识变成碎片"。→ 反面教材。
3. **live 文件夹引用**（无索引、授权目录内按需读写）：Claude Cowork（授予一个文件夹，agent 在其中读写）、ChatWise Agent 模式（**连接多个本地项目文件夹 + 指定一个主工作区**，跨文件夹搜索）、AnythingLLM File System Agent（按文件夹授权）、Obsidian（vault 本身即语料，索引就存放在 vault 内）。

关键机制对比：**Open WebUI 的 Focused RAG vs Full Context 显式开关**是找到的最干净模型（官方指南：<~20K token 的文档全量注入往往优于检索）。

### 1.2 跨产品共识（直接可抄）

1. **默认单锚点，其余必须显式挂载**——所有产品都这样；"全局可见一切"没人做。
2. **挂载通道三件套**：启动参数 / 会话内命令 / 持久化配置，语义一致（Claude Code 最完整）。
3. **access ≠ config**：挂载目录给的是文件访问权，不是配置加载权（Claude Code 的清晰边界）。
4. **两级内容**：只读引用 vs 可编辑（Aider `/read` vs `/add`、Codex read-only vs workspace-write）。只读是低风险默认。
5. **规则文件层级合并、就近优先**；AGENTS.md 已成五家 IDE + CLI 的通用交换格式；**嵌套子目录规则懒加载**是趋势。
6. **挂载是会话身份的一部分**：resume 时恢复挂载（Claude Code `--continue`）。
7. **explicit beats ambient**：Copilot 撤回自动附带、JetBrains Codebase toggle——默认让 agent 在挂载范围内自主发现，给用户显式 pin 的廉价入口。
8. **按需工具优先，索引是可选项**：Zed 证明纯 grep/read 工具 + 无索引完全可行且永不过期；索引只在大语料场景按需开。
9. **展示范围**："agent 现在能看到什么"必须可见（Zed `/context`、Cursor repo switcher、附件 chip 条）。
10. **两级 ignore**：隐私级（禁一切 AI 访问）vs 索引级（仅不入索引）；尊重 `.gitignore`。

### 1.3 已证实的坑（别踩）

| 坑 | 证据 | 对策 |
|---|---|---|
| 大文件夹拖垮索引 | AnythingLLM #928、Open WebUI #12077 | v1 不做索引；将来索引必须后台增量 + 文件数上限显式可见 |
| 索引过期 | Cherry 快照式入库被自家文档承认 | live 引用优先；将来索引要 mtime+hash 增量 + 新鲜度徽标 |
| 静默切 RAG 降级 | Claude Projects 用户反弹 | 检索模式**按目录显式选择**，UI 上永不静默变 |
| 多根/多目录实现全是 bug | Gemini #5512、Codex #18448/#24214、Roo #8041、Cline 只读第一个根 | 路径统一 resolve + symlink 规范化；挂载集合作为唯一事实源在 runtime 集中管理 |
| embedding 锁定/换库重嵌 | AnythingLLM 换向量库 = 全量重嵌 | 将来索引存 embedder 名+维度；本地优先 |
| 云端依赖反模式 | LobeChat KB 强制 S3；Rewind 停服 | Ginno 保持 local-first，索引落 `~/.ginno/` 可搬迁 |

---

## 2. Ginno 现状与差距（2026-08-12 勘察）

| 维度 | 现状 | 差距 |
|---|---|---|
| 项目模型 | slug 客户端自由字符串，UI 硬编码 `"default"`，无注册表/无 CRUD/无选择器 | 多项目本就是 🚧；本设计不强行一次做完，目录挂载先行 |
| 工作目录 | 服务端每会话创建 `sessions/<sid>/` 并绑定；客户端传的 `workspace` 被忽略 | 单一 workspace 字符串贯穿 `AgentState`→`SessionCtx`→`build_builtin_tools`；无附加目录字段 |
| 上下文注入 | WorldState `EnvironmentSection` 宣告一个 `<workspace>`；知识库 vault 走检索注入（`knowledge/injection.py`） | vault 是唯一"外部目录"先例，可复用其 probe/配置 UI 模式 |
| 工具层 | read/write/edit/glob/grep/bash，workspace 构造时绑定；**绝对路径透传无围栏**，仅 `_path_denied` 黑名单（~/.ssh、Keychains、~/.ginno） | 需要可达范围白名单化，支持多根 |
| 权限 | `settings.permissions` 对 `repr(args)` fnmatch；`bypass_permissions` 默认开 | 路径规则可用但无规范化；挂载目录应纳入权限叙事 |
| 选择器 | Tauri capabilities 仅 `core:default`，**webview 无任何 FS/dialog 能力**，一切文件操作走 sidecar | 目录选择需加 `tauri-plugin-dialog` 或走"手输路径 + sidecar probe"（vault 先例） |
| 会话恢复 | `_ensure_session` 从 paths 重新派生 workspace | 挂载集合需持久化进 session meta 并在恢复时重建 |

**结论**：接缝都在——`AgentState.workspace`、`build_builtin_tools(workspace)`、`EnvironmentSection`、session meta、vault 的 probe 先例。改动是扩展性的，不是推倒性的。

---

## 3. 产品方案

### 3.1 核心场景（user stories）

1. **代码操作**：我把 `~/workspace/ginno` 挂进会话 → 让 agent "跑一下测试，把失败的修了"→ agent 在该目录 bash/grep/edit，主工作目录就是它。
2. **只读参考**：我把公司规范库 `~/docs/standards` 挂为只读 → agent 写代码时参照它，但改不了它。
3. **跨目录任务**：同时挂前端 repo（rw）+ API 契约 repo（ro）→ "对照契约把接口对齐"。
4. **笔记/文档问答**：挂一个笔记目录 → 问"我上个月关于 X 的调研结论"→ agent grep/read 按需找（大语料场景将来可手动开索引）。
5. **规则随行**：目录里有 `AGENTS.md`（或 `GINNO.md`）→ 挂载且开关打开时，其中约定自动对该会话生效；换一个会话不挂它就不生效。

### 3.2 概念模型

- **上下文目录（Context Folder）**：注册进 Ginno 的一个本地目录，带访问级与规则加载开关。注册 ≠ 挂载：注册是进"目录库"，挂载是进某个会话。
- **挂载（Attach）**：会话级动作；挂载集合是**会话身份的一部分**（重开/resume 恢复）。
- **主工作目录（Primary）**：至多一个挂载目录可被标为主：bash 的 cwd、文件工具相对路径基准、EnvironmentSection 的 `<workspace>` 都切过去。未指定时保持现状（session files 目录）。
- **访问级**：`ro`（只读引用：read/grep/glob 可达，write/edit 拒绝，bash 写操作按权限策略）/ `rw`（完整工作区）。**新挂载默认 `rw`**（个人单机场景信任度高，挂进来就能干活；在 popover/管理页可随时降为 ro）。
- **规则加载开关**：`load_rules`——是否读取该目录内 `AGENTS.md`/`GINNO.md` 注入（默认开，可关；学 Gemini CLI 的信任拨盘）。**目录内其它配置/脚本永不加载**（access ≠ config）。

### 3.3 三个绑定模型选项

#### 选项 A：纯会话级挂载（Zed / Claude Code 模型）

每个会话独立维护自己的挂载集合；没有全局库，每次现加。

- ✅ 最简单，贴合"每会话一个工作目录身份"的业界最干净模型
- ❌ 常用目录反复添加；跨会话无一致性；"我的项目"无处安放

#### 选项 B：Project 一等实体（Cursor workspace 模型）

引入 Project：Project = 名称 + 一组目录 + 项目级规则；会话归属 Project；顺带把 🚧 的多项目做了。

- ✅ 一步到位；目录、规则、会话都有归属；对齐 README 里 `~/workspace/<proj>` 的愿景
- ❌ 动"会话↔项目"的基本关系，迁移成本大（现有 session meta、checkpointer、侧栏全要改）；对"随手挂个目录问个问题"的轻量场景太重

#### 选项 C：文件夹库 + 会话级挂载（✅ 已选定）

全局**目录库**（注册过的目录，记住访问级/开关）+ 会话从库里挂（也可临时加）。Project 实体留到后续独立演进，届时"项目"可由"一组目录 + 一组会话"自然组装。

- ✅ 覆盖全部五个场景；轻量路径（临时挂）与重度路径（库管理）都通
- ✅ 对现有架构是纯扩展（session meta 加字段、新增 `/api/folders`）
- ✅ 为将来的 Project 实体预留了"目录组"这块积木
- ❌ 比 A 多一点存储管理（一个 json 文件而已）

> 下文按选项 C 展开。

### 3.4 UX 设计

```
┌────────────────────────────────────────────────────────────────┐
│ ☰  Ginno        会话标题            [📁 2 ▾] [🎯] [模型] [⋮]  │   ← TopBar：挂载 chip
├──────────────┬─────────────────────────────────────────────────┤
│ 会话列表      │                                                 │
│  …           │                 ChatStream                      │
│              │                                                 │
│              ├─────────────────────────────────────────────────┤
│              │ [+] 输入…                          📎  @  /     │
└──────────────┴─────────────────────────────────────────────────┘

点击 [📁 2 ▾] 弹出 popover：
┌──────────────────────────────────────┐
│ 本会话上下文目录                      │
│ ★ ginno        rw   AGENTS.md ✓  ✕  │  ★ = 主工作目录
│   standards    ro   AGENTS.md ✗  ✕  │
│ ─────────────────────────────────── │
│ ＋ 挂载目录…（选择/输入路径）         │
│ ⚙ 管理目录库                          │
└──────────────────────────────────────┘
```

- **挂载入口三通道**（对齐 Claude Code 共识）：① TopBar chip popover；② 会话内命令 `/mount <path>`（`/mount`、`/umount`、`/mounts`）；③ Settings → 上下文目录页（库管理 + 每目录默认值）。
- **添加流程**：点"挂载目录" → 原生文件夹选择器（M1；M0 为手输路径）→ sidecar probe（存在?目录?文件数?有 .git?有 AGENTS.md?）→ 确认（访问级默认 `rw`，可勾选降为 `ro`）→ 入库并挂载。
- **可见性**：EnvironmentSection 让模型知道边界；TopBar chip 让用户知道边界；agent 越界访问时权限弹窗说明"该路径不在挂载范围"。
- **拖拽**：现有 Tauri 拖拽文件链路（`__ginnoFileDrop`）可扩展识别目录路径 → 询问"作为只读挂载?"（M1+）。

### 3.5 权限与信任模型

1. **默认可读范围 = 会话 workspace ∪ 挂载目录**；**默认可写范围 = 会话 workspace ∪ rw 挂载目录**。`~/.ssh`、Keychains、`~/.ginno`（除当前会话目录）黑名单不变。
2. **范围外访问**：触发权限询问（现有 interrupt 流），弹窗给"本次允许 / 总是允许该路径"——"总是允许"写入 `settings.permissions`（对齐 Roo/OpenCode 的 external_directory 模式）。`bypass_permissions` 开启时行为同现状（不拦），但**挂载宣告与只读级仍然生效**（只读级是工具层硬约束，不走权限策略）。
3. **access ≠ config**：挂载目录中的任何 `settings.json`、hooks、skills、mcp 配置一律不加载。只有 `AGENTS.md`/`GINNO.md` 文本（受 `load_rules` 开关控制）会注入。
4. **规则注入就近优先**：多个挂载目录的规则文件都注入时按目录分节标注 `path`，冲突时指示模型以"与当前操作文件最近的目录"为准（Codex/AGENTS.md spec 语义）。
5. **范围外访问维持现状（M0 决议）**：未挂载路径的行为与今天完全一致（`bypass_permissions` 开启时放行，仅黑名单拦截）。不新增"限制在挂载目录内"开关——个人助理需要灵活性；挂载的 ro 级仍是工具层硬约束。收紧开关留作未来按需项（见 §4.7）。

### 3.6 边界：v1 不做的事

- ❌ **不做 RAG/向量索引**：按需读取 + grep/glob 足够（Zed 验证过）；大语料索引是 M2+ 的可选项，且必须显式开启、显式模式（Full-Context vs 检索，学 Open WebUI）。
- ❌ **不做目录树浏览器/文件编辑 UI**：Ginno 不是 IDE；agent 是操作主体。M2 可加只读浏览。
- ❌ **不做目录 watch/同步**：live 引用天然新鲜，无需 watcher。
- ❌ **不引入 Project 实体**（选项 B 的内容），除非你选它。

---

## 4. 技术方案

### 4.1 数据模型

```jsonc
// ~/.ginno/folders.json（新增；原子写，同 settings.json 模式）
{
  "folders": [
    {
      "id": "f_a1b2c3",                    // 短随机 id，稳定引用（路径可被用户改名）
      "path": "/Users/x/workspace/ginno",  // 规范化后的绝对路径
      "name": "ginno",                     // 默认 basename，可改
      "access": "rw",                      // "ro" | "rw"（库级默认）
      "load_rules": true,                  // 是否注入该目录 AGENTS.md/GINNO.md
      "added": "2026-08-12T10:00:00+08:00",
      "last_used": "2026-08-12T12:30:00+08:00"
    }
  ]
}
```

```jsonc
// session meta（sessions/_index.json 条目）扩展
{
  "id": "…", "title": "…", "agent_id": "…",
  "context_folders": ["f_a1b2c3", "f_d4e5f6"],  // 挂载集合（库 id）
  "primary_folder": "f_a1b2c3"                  // 可空 = session files 目录为 cwd
}
```

- 挂载以 **id 引用**而非路径字符串：目录改路径时库单点更新，所有会话跟随。
- 快照容错：会话创建/恢复时把解析后的路径快照进内存态，库条目被删不炸会话（降级为"路径缺失"标记，UI 提示）。

### 4.2 Runtime 改动

**状态**（`state.py` / `world_state.py`）：

```python
@dataclass
class ContextDir:
    id: str
    path: Path        # resolved
    name: str
    access: Literal["ro", "rw"]
    load_rules: bool

# AgentState / SessionCtx 增加
context_dirs: list[ContextDir]   # 默认 []
primary: str | None              # 主目录 id
```

**工具层**（`tools/builtin.py`）——核心改动：

```python
def build_builtin_tools(workspace: str, context_dirs=None, primary_path=None):
    base = Path(primary_path or workspace)          # 相对路径基准 + bash cwd
    read_roots  = [workspace] + [d.path for d in context_dirs]
    write_roots = [workspace] + [d.path for d in context_dirs if d.access == "rw"]
```

- `_ws()` 相对路径解析基准 → `base`；`bash` 的 `cwd` → `base`。
- `_path_denied` 升级为 `_path_policy(p, *, writing)`：黑名单优先 → 挂载只读目录对 write 拒绝（硬约束）→ 其余维持现状语义（白名单外不新增拦截，与 M0"维持现状"决议一致；预留 writing/roots 参数以便未来加收紧开关而不再改签名）。
- `glob/grep` 增加可选 `root` 参数（限白名单内），默认 `base`——让模型能定向搜某个挂载目录；输出路径统一用绝对路径（多根下相对路径有歧义）。
- symlink 一律 `resolve()` 后判边界（防 symlink 越狱；竞品多目录 bug 的重灾区）。

**注入**（`graph.py` / `world_state.py`）：

- `EnvironmentSection.render` 增加：

```xml
<context_folders>
- /Users/x/workspace/ginno [rw, primary]  (AGENTS.md 已加载)
- /Users/x/docs/standards [ro]
工作规则：只读目录禁止写入；范围外路径先征求用户同意。
</context_folders>
```

- `build_stable_system` 收集 `load_rules=true` 挂载目录的 `AGENTS.md`/`GINNO.md`（每个截断 ~8k 字符，合计预算 ~24k），按目录分节注入 `<folder_rules path="…">`。向下懒加载（子目录规则文件碰到再读）M2 再做。
- `_ensure_session`：从 meta 恢复挂载集合 → 重建 `context_dirs` → 重建工具/图（走现有 `_maybe_refresh_session_graph` 模式）。

### 4.3 API 设计

| 方法 & 路径 | 作用 |
|---|---|
| `GET /api/folders` | 列出目录库 |
| `POST /api/folders` `{path, name?, access?, load_rules?}` | 注册（内部先 probe；重复路径幂等返回已有） |
| `POST /api/folders/probe` `{path}` | 探测：存在/是否目录/文件数(上限采样)/有无 .git /有无 AGENTS.md|GINNO.md → 给 UI 展示 |
| `PATCH /api/folders/{id}` | 改名/访问级/规则开关/改路径（改路径重 probe） |
| `DELETE /api/folders/{id}` | 从库移除（会话引用降级为缺失标记） |
| `PUT /api/sessions/{sid}/context` `{folder_ids, primary_id?}` | 设置会话挂载集合（幂等全量替换；WS 广播 `context.updated`） |
| `CreateSessionRequest` 扩展 `context_folders?: [id]` | 新会话直接带挂载 |

WS：复用现有 `context.updated` 事件推送 chip 刷新；权限询问沿用 `permission.request`。

### 4.4 Tauri 侧

- **M0**：手输路径 + sidecar probe（零 Tauri 改动，vault 先例已验证）。
- **M1**：加 `taurii-plugin-dialog`（`dialog:default` capability），webview 经 `window.__TAURI__.dialog.open({directory: true})` 选目录后把路径交给 sidecar。CSP/同源不受影响（dialog 是 core invoke）。
- 拖拽目录识别（M1+）：`lib.rs` 的 DragDrop 已回传路径列表，前端对目录路径弹出"挂载为 ro?"确认，调用同一 `/api/folders` 流程。

### 4.5 会话命令

`commands/resolver.py` 增加 `/mount <path|name>`（probe → 入库 → 挂载）、`/umount <name>`、`/mounts`（列出当前挂载）、`/primary <name>`（切主目录）。与现有 TurnPlan 机制一致。

### 4.6 安全清单

- [ ] 黑名单恒定优先（ssh/Keychains/ginno-home）
- [ ] 只读级在工具层硬拦，不受 bypass_permissions 影响
- [ ] 路径规范化（resolve + symlink）后再判界
- [ ] bash 的动态拼接路径仍可能绕过静态扫描——现状已知弱点，挂载不使其恶化；`settings.permissions` 路径 glob 规则在 M1 做规范化（展开 `~`、resolve 后匹配）
- [ ] 挂载目录内配置/钩子/技能一律不加载（代码层断言 + 文档声明）
- [ ] 规则文件注入有字符上限，防巨型 AGENTS.md 挤爆上下文

### 4.7 里程碑

**M0 — 可用性闭环（本次建议范围）**
1. `folders.json` + `/api/folders*`（含 probe）
2. session meta `context_folders/primary_folder` + `PUT /sessions/{sid}/context` + 创建时带入 + `_ensure_session` 恢复
3. `build_builtin_tools` 多根白名单 + 只读硬约束 + bash cwd 切主目录
4. `EnvironmentSection` 挂载宣告；AGENTS.md/GINNO.md 注入（带开关）
5. Settings → "上下文目录"管理页（手输路径）；TopBar chip + popover；`/mount` 命令

**M1 — 体验**
- 原生文件夹选择器（tauri-plugin-dialog）；拖拽目录挂载
- `settings.permissions` 路径 glob 规则规范化（展开 `~`、resolve 后匹配），让挂载目录的权限规则可写可靠

**M2 — 纵深（按需）**
- RightPanel 目录浏览 tab（只读树 + reveal）
- `@folder`/`@file` mention 直注（学 Cursor/Zed）
- 大目录可选索引：复用 LanceDB + knowledge 管道，显式开关 + 新鲜度徽标；repo-map 式摘要（tree-sitter + token 预算）作代码目录的轻量定向
- Project 实体：目录组 + 会话归属（消化 🚧 多项目）
- （未来按需）"限制文件访问在挂载目录内"全局开关 + 越界"总是允许"写回 settings（M0 决议维持现状，此项不在承诺范围）

### 4.8 验收（M0）

- [ ] 挂载 `~/workspace/ginno`（rw、primary）→ agent bash `pwd` 输出该路径；`pytest` 可跑；edit 生效
- [ ] 挂载 ro 目录 → agent 可 read/grep，write_file 返回 `[error]` 且 EnvironmentSection 有解释
- [ ] 重启 app 后会话挂载集合与主目录恢复
- [ ] 删除库条目后旧会话不崩，UI 显示"目录缺失"
- [ ] 未挂载时行为与现状完全一致（回归）

---

## 5. 决策记录（2026-08-12）

| # | 决策点 | 结论 |
|---|---|---|
| 1 | 绑定模型 | **选项 C：文件夹库 + 会话级挂载**。Project 实体留待 M2+ 独立演进 |
| 2 | 新挂载目录默认访问级 | **`rw`**（个人单机信任度高；可随时降为 ro） |
| 3 | 范围外访问策略 | **M0 维持现状**，不加收紧开关；留作未来按需项 |
| 4 | 规则文件名 | `AGENTS.md` 与 `GINNO.md` 都认（同目录两者并存时 AGENTS.md 优先）——对齐业界标准同时保留自有品牌入口 |
| 5 | v1 是否做 RAG 索引 | **不做**。按需读取优先；大语料索引为 M2+ 显式可选项 |

## 6. 下一步

按 §4.7 M0 清单实施；实施过程若偏离本文档，按惯例记录到 `implementation-notes.md`。
