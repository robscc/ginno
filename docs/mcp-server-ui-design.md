# MCP Server Settings UI Design（MCP 服务器设置页 UI 化）

> 状态：**设计定稿**（2026-10-09）。目标：把 Settings → Connections → MCP 从
> 「raw JSON textarea + 状态行」升级为结构化的服务器管理页——卡片列表、启停开关、
> 工具级开关、添加向导、权限联动，后端 `mcpServers` 格式保持兼容、既有 API 只做加法。
>
> 关联文档：
> - `mcp-server-ui-prototype.html`（仓库根目录）— v2 交互原型，本文档的 UI 规格来源；
>   原型中的 mock 数据（模板 chips、up 3d 时间线等）落地时以本文档裁定为准。
> - `packages/runtime/src/ginno_runtime/mcp/registry.py` — 后端行为权威（本文 §1.1 盘点）。
> - `connector-module-design.md` — 连接器模块管「连没连、怎么装」；MCP server 属工具层
>   （该文档 §1 明确不收编），本页挂在 Settings → Connections 组内，信息架构不变。

---

## 0. TL;DR

- **现状**：MCP 设置页只有一段 raw JSON textarea（Save & Reload 全量重建）+ 一列
  状态行（绿/红点 + 单行根因）。用户想停用一个服务器、关掉某个危险工具，只能手改
  JSON——改错一个逗号就保存失败。
- **方案**：前端重写为「卡片列表 + 详情四 tab + 添加向导」；后端 `mcp.json` 每个
  server 条目新增 `enabled` / `disabled_tools`（`connect_timeout` 已存在，一并露出），
  旧格式完全兼容；`GET /api/mcp` 的 `status[]` 扩展 `enabled` / `disabledTools` /
  `toolDetails`，`POST /api/mcp/reconnect` 支持可选 `?server=`。
- **核心语义裁定**：工具开关 = 纯配置写入（putMcp），下一 turn 经既有 drift 机制
  自然生效，**不需要** reload/reconnect；只有 transport/url/command 等连接参数变更
  才走 Save & Reload 全量重建。手动 Reconnect 走 `reconnect?server=` 惰性补连，
  绕过冷却，不动已连接服务器。
- **权限联动**：工具图内名字是 `mcp_<server>_<tool>`（server 名可为中文），设置页
  Permissions tab 是 `settings.json → permissions` 三个列表（allow/ask/deny）的
  按 server 过滤视图；Security → Permissions 仍是中央管理入口，两边读写同一份
  数据，判定优先级 deny > ask > allow > 默认 ask。
- **分期**：P0 结构化列表 + 服务器/工具启停；P1 添加向导 + Import + 权限联动 +
  Activity；P2 OAuth 授权态 + elicitation。

## 1. 背景与现状

### 1.1 后端现状（registry.py 盘点，全部已落地）

| 能力 | 事实 |
|---|---|
| 配置文件 | `~/.ginno/mcp/mcp.json`，顶层键 `mcpServers`（`load()` 也兜底认 `servers`） |
| 传输类型 | `MCPServerConfig.from_dict` 同时接受 `transport`（自有 schema）与 `type`（Claude 风格）；拼写归一化 `strip().lower().replace("_","-")`——`streamable_http` / `Streamable_HTTP` 都折成 `streamable-http`；缺省 stdio |
| connect_timeout | 已是 per-server 配置项，float，默认 15.0；`connect_all` 按 `asyncio.wait_for(live.connect(), timeout=cfg.connect_timeout)` 逐服务器限时，并发连接（N 台 ≈ max 延迟而非求和） |
| 连接失败 | 记入 `_failed[name]`（monotonic 时间）+ `_last_error[name]`（`_compact_connect_error` 剥 ExceptionGroup 到叶子，`TypeName: msg` 单行，截 300 字符） |
| 惰性自愈 | `retry_failed(cooldown_s=120)`：只补 `_failed` 里未 live 的，冷却 120s（以最新一次失败计），`_retry_busy` 防并发；turn 路径与 `GET /api/mcp` 都会机会式触发 |
| 状态查询 | `status()` → 逐配置服务器 `{name, connected, tools:int, error}` |
| 工具命名 | `_full_tool_name` = `mcp_{server_name}_{tool_name}`，单一事实源（包装器与名字比对共用，2026-08-10 事故的修复产物） |
| 图内刷新 | 每turn drift 检查（`turn.py`）：只在 **gains（出现新工具）或服务器被删** 时重建图；纯 loss（配置在但连不上）不剥工具——旧包装器经 `_resolve_live` 每次调用时重解析，重连后自动路由到新连接 |
| 工具排序 | `all_langchain_tools()` 按名字排序——tools 数组位于 provider cache prefix 最前端，重连乱序会打穿整段前缀缓存 |

### 1.2 前端现状与痛点（McpSettings.tsx）

当前页（`apps/web/src/components/settings/McpSettings.tsx`，119 行）只有三块：

1. **状态行列表**：每行 `点 + 名字 + 工具数/单行 error`，只读，无任何操作入口；
2. **raw JSON textarea**（14 行高）：整个 `mcp.json` 原文，`JSON.parse` 失败只在
   旁边报一句 `invalidJson`；
3. **两个按钮**：`Save & Reload`（putMcp → reloadMcp 全量重建）、`Reconnect`
   （有任一断连时出现，`connect_all` 只补缺口）；5s 轮询 `setInterval(load, 5000)`。

痛点：

- **改配置门槛高**：停用一个服务器 = 手删 JSON 块再整段重连；关一个危险工具
  （如 `delete_document`）根本做不到——只要服务器在线，它的全部工具每 turn 注入。
- **token 无感**：钉钉网关一族动辄 24～97 个工具，全部默认开启，用户没有任何
  地方能看到「这些工具每 turn 花多少 token」、更没有手段裁剪。
- **错误排查断层**：单行根因有了（`_compact_connect_error`），但没有重试入口
  （要点 Reconnect 按钮且它对所有失败服务器一起重试）、没有历史、没有工具清单。
- **添加服务器无引导**：钉钉网关的 `?key=` 鉴权、stdio 的 env 继承这些常见形态
  全靠用户自己懂 JSON。

## 2. 目标 / 非目标

**目标**

1. 服务器级启停、工具级开关全部结构化 UI 化，`mcp.json` 手改不再是必经之路；
2. 状态可见性升级：统计条（connected/failed/tools active/~tokens per turn）、
   卡片级单行根因、失败服务器一键重试；
3. 添加向导覆盖两大主流形态：远程网关（streamable-http / sse，`?key=` 鉴权）与
   本地进程（stdio，npx / uvx）；兼容粘贴 Claude Desktop / Cursor / `.mcp.json` 片段；
4. 与既有权限体系（`settings.json → permissions`）联动，per-server 视图编辑；
5. `mcpServers` 格式向后/向前兼容：新键被旧 runtime 忽略，旧文件被新 runtime
   以默认值补齐。

**非目标（本期不做）**

- 不改 `mcpServers` 顶层格式、不做 schema 校验器（PUT 仍是「写了就认」，坏配置
  的兜底是连接时的单行根因 + Advanced JSON 回退）；
- 不做连接器模块式的安装向导/状态机（MCP 不是 connector，边界见
  `connector-module-design.md` §1）；
- 不做 per-server 系统提示词、工具描述改写（影响 prompt 层，另行立项）；
- 不做 MCP resources / prompts 面（当前只用 tools 面）；
- 不做远程同步/多配置源，`~/.ginno/mcp/mcp.json` 仍是唯一事实源。

## 3. 配置 schema 扩展

### 3.1 新增键

每个 server 条目在既有键（`type`/`transport`、`url`/`command`/`args`/`env`）之上
新增三个键：

```jsonc
// ~/.ginno/mcp/mcp.json
{
  "mcpServers": {
    "钉钉文档": {
      "type": "streamable-http",            // 既有；type/transport 等价，拼写归一化
      "url": "https://mcp-gw.dingtalk.com/server/xxx?key=yyy",
      "enabled": true,                       // 新增：false 时完全不连接、工具不注册
      "disabled_tools": ["delete_document", "transfer_owner"],  // 新增：图内屏蔽的原始工具名
      "connect_timeout": 15.0                // 既有（此前只能手改 JSON），UI 露出
    },
    "github": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_TOKEN": "ghp_..." }
      // enabled 缺省 = true；disabled_tools 缺省 = []
    }
  }
}
```

语义裁定：

- **`enabled: false`**：`connect_all` 跳过（不进 pending）；`status()` 里该条
  `connected: false, enabled: false`，不算 `failed`（不是故障，是意图）；已 live
  的服务器被改成 disabled 后由 reconnect 端点逐出（§4.3）。等价于「注释掉这台
  服务器」。
- **`disabled_tools`**：成员是**服务器侧原始工具名**（不是 `mcp_` 前缀全名），
  精确匹配不过通配。被屏蔽的工具不进 `all_langchain_tools()` / 
  `list_wrapped_tools()`——模型看不见、权限层也碰不到（区别于 permissions deny，
  见 §6.2）。
- **两个键都只影响「注册进图的工具集」**，不触碰连接本身：一台 enabled 且在线的
  服务器，`disabled_tools` 再长也保持连接（Activity/状态仍绿）。

### 3.2 兼容性

- **向前兼容**：`MCPServerConfig.from_dict` 只读白名单键，`enabled` /
  `disabled_tools` 对旧 runtime 是无害未知键，直接忽略——新版 UI 写出的文件在旧
  版应用里照常工作（含 `enabled: false` 的服务器在旧版会被连接，属可接受的降级，
  UI 保存时 toast 提示「旧版本应用会忽略启停状态」即可，不阻塞）。
- **向后兼容**：新 `from_dict` 读 `cfg.get("enabled", True)`、
  `cfg.get("disabled_tools", [])`，旧文件零迁移。
- **PUT 仍是全文写回**：结构化开关的实现方式是 GET config → 改一个条目 →
  PUT 全文（§4.2），文件里用户手写的注释性自定义键原样保留。

### 3.3 生效语义：开关 = 配置写入，下一 turn 自然生效

这是本设计最关键的裁定，依赖既有 drift 机制（§1.1 末行），**不需要新增任何
刷新端点**：

- **工具开关**：`putMcp` 写文件后，registry 的读侧（`list_wrapped_tools` /
  `all_langchain_tools`）按 `disabled_tools` 过滤——开启→关闭是图内工具集收缩，
  关闭→开启是 gains。drift 规则（gains 或删服才重建）需要**一处精化**：
  「被 `disabled_tools` 屏蔽」属于用户意图的移除，必须触发重建，不能套用
  「纯 loss 容忍」（容忍是给连接抖动的，不是给用户开关的）。落点：
  `unconfigured_wrapped_names(frozen)` 的判定从「不在任何配置服务器前缀下」
  扩展为「不在任何 **enabled** 服务器前缀下，**或**原始名命中所属服务器的
  `disabled_tools`」——重建条件仍是现成的 orphaned 检查，`turn.py` 无需改动。
- **服务器启停**：disable → 该前缀整体视为 unconfigured → 同路径重建；enable →
  重连成功后工具集出现 → gains 重建。
- **时序承诺**：所有开关 UI 文案统一为「takes effect on the next turn」，与
  原型 toast 一致；正在进行的 turn 不受影响。
- **代价提示**：tools 数组在 provider cache prefix 最前端（§1.1），任何开关都会
  一次性打穿前缀缓存。UI 在 Tools tab 底部注明；不做防抖（用户操作频率远低于
  每 turn 一次）。

## 4. API 契约（全部为既有端点的加法）

### 4.1 GET /api/mcp — status 扩展

```jsonc
{
  "servers": ["钉钉文档", "github"],          // 既有：配置里的全部名字
  "tools": ["get_document_info", ...],       // 既有：原始名（raw）
  "failed": ["钉钉待办"],                     // 既有：连接失败名单
  "status": [
    {
      "name": "钉钉文档",
      "enabled": true,                       // 新增
      "connected": true,                     // 既有
      "transport": "streamable-http",        // 新增（归一化后的值）
      "url": "https://mcp-gw.dingtalk.com/server/xxx?key=***",  // 新增，脱敏（§8.3）
      "tools": 24,                           // 既有：enabled 且连接后的可见工具数
      "disabledTools": ["delete_document"],  // 新增
      "error": null,                         // 既有：单行根因
      "toolDetails": [                       // 新增：仅 ?tools=1 时返回（见下）
        {
          "name": "get_document_info",       // 原始名
          "description": "Fetch document metadata",
          "readOnlyHint": true,              // 来自 MCP tool annotations
          "destructiveHint": false
        }
      ]
    }
  ]
}
```

- `toolDetails` **默认不返回**：5s 轮询只吃计数与开关状态，97 工具 × 描述的
  payload 只在详情页 Tools tab / 向导预览需要，用 `GET /api/mcp?tools=1` 显式取
  （响应体不变，仅 status[].toolDetails 补齐）。
- annotations 缺失时 `readOnlyHint` / `destructiveHint` 取 `false`（MCP 规范默认
  即非只读； destructive 的展示章 §5.4）。UI badge 规则：`readOnlyHint=true` →
  绿 `read-only`；否则 `destructiveHint=true` → 红 `destructive`；都无 → 灰 `-`。
- 顺带保留既有行为：本端点仍在 `has_pending_failures()` 时 fire-and-forget
  `retry_failed()`——5s 轮询本身就是自愈时钟，UI 语义见 §5.5。

### 4.2 GET /api/mcp/config 与 PUT /api/mcp

- `GET /api/mcp/config`：**不变**，返回文件原文（含真实 key/env——Advanced JSON
  的逃生舱依赖这一点；UI 结构化字段一律展示脱敏值，§8.3）。
- `PUT /api/mcp`：**契约不变**（整文件写回），仅新增约定：前端结构化开关的写入
  都是「GET config → 单点修改 → PUT 全文」的读改写，禁止前端拼整个文件；后端
  不做 schema 校验（与现状一致）。结构化开关流程**不追加 reload**（§3.3）。

### 4.3 POST /api/mcp/reload 与 POST /api/mcp/reconnect?server=

| 端点 | 语义 | 何时用 |
|---|---|---|
| `POST /api/mcp/reload` | **全量重建**：`close_all` → 新 registry → `load` → `connect_all`；会断开在用连接（会话内旧包装器靠 `_resolve_live` 自动重路由） | Save & Reload：transport / url / command / args / env 等连接参数变更、Advanced JSON 保存、向导新增服务器 |
| `POST /api/mcp/reconnect[?server=NAME]` | **惰性补连**：`connect_all` 只碰缺失的，不动已连接的；新增两处语义：(1) 可选 `?server=` 只重试指定服务器，**绕过 120s 冷却**（手动意图优先于自愈节奏）；(2) 逐出 `_live` 中 `enabled` 已翻 false 的服务器（配合启停开关，避免为了「断开一台」做全量 reload） | Reconnect failed 按钮（无参）、卡片/详情的 Reconnect（`?server=`）、启停开关的 enable 应用 |

响应沿用现状：reconnect 返回 `{ok, status}`（status 始终全量返回，前端直接吃，
省一次 GET）。

### 4.4 P1 新增：POST /api/mcp/test（向导 Test Connection）

向导第 3 步需要「不落盘试连」：请求体即一个 server 条目（`{name, type, url,
headers?, ...}` 或 stdio 形态），后端临时构造 `_LiveServer` 走一遍 connect +
`list_tools`，返回 `{ok, ms, tools:[toolDetails], error}`，**不注册、不写文件**。
实现上直接复用 `_LiveServer` + `_compact_connect_error`，connect 超时取条目的
`connect_timeout`。

## 5. UI 规格

### 5.1 信息架构与组件拆分

- 路由不变：`/settings/connections/mcp`（`settingsGroups.ts` connections 组第 2
  个 tab，LEGACY_REDIRECT 已指向此处）；Security → Permissions 不动。
- `McpSettings.tsx` 拆为：`McpSettings`（列表页）+ `McpServerDetail`（详情页，
  同路由内 state 切换，与原型一致）+ `McpWizardModal` + `McpImportModal`（P1）；
  共享一个 `useMcp` hook 承载 5s 轮询与乐观更新。
- 文案全部进 i18n catalog（`apps/web/messages/{en,zh-CN}/settings.json` 的
  `settings.mcp.*`），UI 文案英文（2026-10-04 起 UI 全英文规则），注释中文。
  原型里的英文串即 en 文案底稿。

### 5.2 列表页

```
┌ MCP Servers ──────────────────────────── [↓ Import JSON] [+ Add Server] ┐
│ N servers — tools are injected into every turn; disable unused ones     │
│ to save tokens.                                                         │
├─────────────────────────────────────────────────────────────────────────┤
│ ● N connected   ● M failed            K tools active · ~13.0k tokens/turn│  ← 统计条
│ [↻ Reconnect failed]                    stored in ~/.ginno/mcp/mcp.json │
│ ┌─────────────────────────────────────────────────────────────────────┐ │
│ │ ● 钉钉文档  [STREAMABLE-HTTP]              10/24 tools   [开关] [⋮] │ │
│ │   https://mcp-gw.dingtalk.com/server/3501…?key=*** · up 3d          │ │
│ └─────────────────────────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────────────────────────┐ │
│ │ ● 钉钉待办  [STREAMABLE-HTTP]               0/11 tools   [开关] [⋮] │ │
│ │   https://mcp-gw.dingtalk.com/server/206c…?key=*** · last ok 09:58  │ │
│ │   HTTPError: 401 Unauthorized - key expired · 11 tools cached       │ │ ← 红色单行根因
│ └─────────────────────────────────────────────────────────────────────┘ │
│ ▸ Advanced — edit raw config (mcp.json)                                 │  ← 折叠
└─────────────────────────────────────────────────────────────────────────┘
```

- **统计条**：connected（绿点）/ failed（红点）计数；右侧
  `K tools active · ~X tokens / turn`，K = enabled 且在线服务器的启用工具数之和，
  X = K × 130（启发式常数，集中在 `useMcp` 一处定义并注释来源，§8.1）。disabled
  服务器的工具不计入。
- **卡片**：状态点（绿 running / 红 error / 黄脉冲 restarting——restarting 是前端
  乐观态，后端只有 connected 布尔）+ 名字 + transport badge（蓝 http / 紫 stdio /
  黄 sse）+ 第二行 mono 显示脱敏 URL · meta（`up 3d` / `last ok 09:58`，由
  status 新增的 `connectedAt` / `lastErrorAt` 时间戳派生，后端在 `_LiveServer`
  connect 成功/失败时记 wall-clock，随 status 返回）+ 右侧 `N/M tools` + 启停
  Toggle + ⋮ 菜单（Edit configuration / Manage tools / Permissions / Reconnect /
  Remove server 红色）。
- **启停 Toggle**：写路径 = §3.3（putMcp；disable 附加 `reconnect` 逐出，enable
  附加 `reconnect?server=` 补连）。toast：`Enabled — tools available on the next
  turn` / `Disabled — tools removed on the next turn`。disabled 卡片整体降不透明度，
  状态点灰。
- **Reconnect failed**：仅当 `failed` 非空时出现，对全部失败服务器并发
  `reconnect`（无参），按钮转 `Reconnecting…`；成功翻绿靠下个 5s 轮询（§5.5）。
- **Advanced JSON 折叠区**：保留现状 textarea + `Save & Reload`（此处唯一触发
  reload 的入口之一），旁注一行说明：`putMcp → reloadMcp — registry is fully
  rebuilt. Accepts type or transport; streamable_http spellings are normalized.`
  定位是逃生舱：结构化 UI 覆盖不到的键（自定义键、批量迁移）从这里进。

### 5.3 添加向导（+ Add Server，P1）与 Import JSON（P1）

三步向导（Transport → Configure → Test），远程优先：

- **Step 1 Transport**：两张大选项卡——`Remote server · streamable-http`（默认
  选中；文案点明「DingTalk MCP gateway URLs carry `?key=` auth in the query
  string — no headers needed. SSE also supported.」）与 `Local process · stdio`
  （npx / uvx；inherits the parent env so bundled node/npx resolve——对应
  registry stdio 分支的 env 继承行为）。下方模板 chips（钉钉文档 / 火山联网搜索 /
  Sentry / GitHub (stdio) / Filesystem (stdio)，硬编码在前端常量即可）+ 折叠的
  「Or paste config JSON (Claude Desktop · Cursor · .mcp.json)」：Parse 按钮取
  `mcpServers` 第一个条目预填，`type/transport` 拼装归一化交给后端。
- **Step 2 Configure**：Display name；http 形态 = Server URL + Headers（kv 行，
  value 用 password input）；stdio 形态 = Command / Arguments（一行一个）/
  Environment（kv，password）；共用 `connect_timeout (seconds)` 默认 15.0。
- **Step 3 Test**：调 §4.4 `POST /api/mcp/test`，日志行流式（initialize
  handshake → tools/list → validating schemas）。成功：绿色框（耗时、transport、
  timeout）+ **Tools discovered — graph-facing names** 预览（`mcp_<name>_<tool>`
  chips）+ 警示行 `Tools are injected into every turn — disable unused ones in
  the Tools tab to save tokens.`；失败：红色框 + `_compact_connect_error` 单行
  根因 + tip（check the URL and key）。`Save & Reload` 仅在成功后可点（putMcp
  增量合并进现有 config → reload）。
- **Import JSON**：粘贴整段 `mcpServers`，Detect & Parse 后逐服务器勾选行（名字
  + transport badge + mono 摘要 + env 计数），Import 走 putMcp 合并写回，toast
  `Imported N server(s) — connect on next reload`（不自动 reload，列表上出现
  「imported — not yet connected」提示与 Reconnect failed 入口）。

### 5.4 详情页（四 tab）

头部：`← All servers` 返回；状态点 + 名字 + transport badge；动作区
`Test connection`（同 §4.4 端点）/ `Reconnect`（`?server=`）/ 启停 Toggle；第二行
mono 脱敏 URL · meta；error 时追加红色错误行（`_compact_connect_error` 产物）。

- **Configuration**：Display name / Transport 下拉（streamable-http / sse /
  stdio，旁注 type/transport 等价与拼写归一化）/ 按 transport 切换的 url+headers
  或 command+args+env（与向导 Step 2 同构，env 继承说明同）/ `connect_timeout`。
  底部 `Cancel` / `Save & Reload`——**连接参数变更走全量 reload**（§4.3 表）。
  ⚠️ Rename（Display name）即改 `mcpServers` 键名 = 旧服务器删除 + 新服务器新增，
  工具全名、会话 `mcp_tool_names`、permissions 模式全部换前缀——保存前二次确认
  弹窗写明后果（§8.2）。
- **Tools**：过滤输入 + `N of M enabled`；表格列 = Toggle / Tool（原始名，mono）/
  Graph name（`mcp_<server>_<tool>` 全名，弱化色 mono，复制按钮）/ Description /
  Hints（§4.1 badge 规则）。写路径 = putMcp，toast 「takes effect on the next
  turn」；底部注 `Estimate ~130 tokens per enabled tool per turn.` + 前缀缓存
  提示。连不上的服务器：表格用 `failed` 之外的缓存数据渲染（若该服务器曾有
  toolDetails）或显示 `tools unavailable while disconnected` 空态。
- **Permissions**（P1）：三段 Allow / Ask / Deny，chip 形态
  `mcp_<server>_<verb>_*`，可删；添加框 placeholder `mcp_<server>_verb_*`。
  读写目标是 `settings.json → permissions` 的同名列表（经 GET/PUT /settings，
  复用 PermissionsSettings 的整文件读改写模式），**只展示/增删本 server 前缀
  命中的模式**。旁注：`Synced to settings.json → permissions. Managed centrally
  in Security → Permissions. First match wins: deny > ask > allow, default ask.`
- **Activity**（P1 精简版）：当前状态 + 最近一次 error（时间戳 + 单行根因）+
  connect/断连历史几条（来自 §4.1 新增时间戳，非全量日志）；完整日志视图 P2
  （需后端按 `mcp[<name>]` 前缀过滤 `~/.ginno/logs/sidecar.log` 的只读端点，
  registry 日志已带该前缀，可行性已验证）。底部注 `Full history in
  ~/.ginno/logs/sidecar.log`。

### 5.5 轮询与状态语义（Reconnect failed / 5s poll / 两条修复路径）

- **5s 轮询保留现状**：`useMcp` 每 5s `GET /api/mcp`；后端借这次轮询机会式触发
  `retry_failed()`（120s 冷却 + busy guard 在 registry 内部）。**用户什么都不按，
  失败服务器也会在冷却窗口后自动翻绿**——原型的 9s 模拟正是在演示这一点。UI 上
  不展示「冷却还剩几秒」（噪声），只在 Activity 里写 `lazy retry scheduled`。
- **手动 Reconnect ≠ 轮询自愈**：手动走 `reconnect[?server=]`，**绕过冷却**立即
  补连——这是它存在的唯一理由（急用某台服务器时不等 120s）。它不重建 registry、
  不打断任何已连接服务器，与 Save & Reload 的对比见 §4.3 表，按钮文案与禁用态
  （reconnecting）沿用现状 i18n key。
- **Save & Reload 的代价必须在 UI 写明**：rebuild 期间该服务器的在用调用会中断
  （会话侧可恢复，`_resolve_live` 重路由），所以 Advanced JSON 保存、向导新增、
  Configuration 编辑三处按钮都用同一文案 `Save & Reload` 并在 toast 里带
  `registry rebuilt` 字样，让用户能区分这两条路径。

## 6. 工具命名空间与权限联动

### 6.1 `mcp_<server>_<tool>`（server 名可为中文）

- 图内工具名 = `mcp_{server_name}_{tool_name}`（`_full_tool_name`，单一事实源）。
  server 名就是 `mcpServers` 的键，**允许中文**（现网即「钉钉文档」等），因此
  图名形如 `mcp_钉钉文档_get_document_info`。这不是新设计，是既有事实；本设计
  的义务是：所有新 UI（Tools tab 的 Graph name 列、向导预览 chips、Permissions
  chips）都渲染这个全名，并保证 CJK 在 mono 字体 + 窄列下的截断/复制体验（§8.2）。
- **前缀歧义（既有限制，非本期引入）**：`unconfigured_wrapped_names` 用
  `mcp_{s}_` 前缀归属性，server `a` 与 `a_b` 并存时 `mcp_a_b_tool` 会被误判为
  `a` 的工具。`disabled_tools` 的归属判定（§3.3）必须用「最长匹配的配置服务器
  前缀」而非首个前缀，并在 registry 实现时用一组中文/下划线交叠名的单测锁死。
  不引入「配置键 slug 化」——那会破坏所有现网 permissions 模式与
  `mcp_tool_names` 指纹。

### 6.2 `disabled_tools` vs permissions deny：两种「关」，语义不同

| | `disabled_tools`（mcp.json） | `permissions deny`（settings.json） |
|---|---|---|
| 模型可见性 | 不可见（不进 tools 数组） | 可见（调用时被判 deny，得到可恢复错误） |
| token 成本 | 省（不占 tools 注入） | 不省 |
| 语义 | 「这台服务器没这个能力」 | 「有能力但禁止用」 |
| 优先级 | —（根本不存在） | deny > ask > allow > 默认 ask（`PermissionPolicy.decide` 顺序，先 deny） |

UI 引导：Tools tab 里关闭工具的行内提示写 `removed from the model entirely`；
Permissions tab 里 deny 的说明写 `visible to the model, calls are blocked`。
想省 token 用前者，想留证据/审计用后者。

### 6.3 Security → Permissions 单一事实源

- permissions 数据只存在 `settings.json`，MCP 详情页 Permissions tab 是
  **按 server 过滤的视图编辑器**，Security → Permissions（`PermissionsSettings.tsx`
  三个 RuleList）是**中央全量编辑器**——两边都是 GET /settings → 改 → PUT
  /settings 的整文件读改写，后写者胜（与现状两个设置页之间的并发语义一致，
  不引入额外锁）。
- 模式写法沿用既有规则语法 `<ToolName>(<arg-glob>)`：MCP 工具不带括号的模式
  `mcp_钉钉文档_get_*` 等价于 `mcp_钉钉文档_get_*(*)`（arg 全匹配）；fnmatch
  大小写不敏感，对 CJK 无副作用。MCP 页添加模式时默认生成带 `_*` 尾缀的 verb 级
  模式（如原型），用户可编辑成精确工具名。
- 禁止在 MCP 页写「整服务器 deny」按钮之类的糖——它等价于 enabled 开关，两套
  入口两种语义只会制造混乱。

## 7. 分期

### P0 — 结构化列表 + 启停 + 工具开关（先落地，独立可发布）

- 后端：`MCPServerConfig` 增 `enabled` / `disabled_tools` 解析；`connect_all`
  跳过 disabled；`status()` 扩展 §4.1 字段（`toolDetails` 挂 `?tools=1`）；
  `list_wrapped_tools` / `all_langchain_tools` 按 `disabled_tools` 过滤；
  `unconfigured_wrapped_names` 按 §3.3 精化（含 CJK/下划线交叠名单测）；
  `reconnect` 支持 `?server=`（绕冷却）与 disabled 逐出；`_LiveServer` 记
  connectedAt / lastErrorAt。
- 前端：列表页重写（卡片 + 统计条 + 启停 Toggle + ⋮ 菜单的 Edit/Manage tools/
  Reconnect/Remove）+ 详情页头部与 Configuration / Tools 两 tab + Advanced JSON
  折叠区保留 + `useMcp` 5s 轮询 hook；i18n key 全量入 catalog。
- Remove server = GET config → 删条目 → putMcp → reload，确认弹窗列出
  「N tools will disappear from the next turn」。

### P1 — 向导 + 权限联动 + Activity

- `POST /api/mcp/test`；三步添加向导（远程优先、`?key=` 说明、模板 chips、粘贴
  解析）+ Import JSON 批量导入；详情页 Permissions tab（§6.3 读写契约）；
  Activity tab 精简版（状态 + 最近 error + connectedAt/lastErrorAt 派生条目）。

### P2 — OAuth / elicitation / 完整日志

- streamable-http 的 OAuth 授权态：status 增 `auth: "needs-auth"` 态 + 卡片
  `Sign in` 按钮 + token 存储方案（另行细化，MCP 授权规范 2.1 版）；
- elicitation：server 请求用户输入时的 UI 通道（依赖 MCP elicitation 支持）；
- Activity 完整日志视图（sidecar.log 按 `mcp[<name>]` 过滤的只读端点 + 前端
  log 组件）。

## 8. 风险与开放问题

### 8.1 token 预算（~130 tok/tool/turn）

- 130 是启发式均值（工具名 + description + inputSchema 折算），真实分布很宽：
  钉钉文档 24 工具 ≈ 3.1k/turn，2026-08 曾出现单 registry 97 工具 ≈ 12.6k/turn
  的极端案例。统计条必须标注 `~`（估算），**不做**精确 tokenizer 计数（每 5s
  轮询做一次全量 schema 计数不可接受）。
- 开放问题：是否给「预算护栏」——enabled 工具总数超过阈值（如 150）时统计条
  变黄提示。倾向做，等 P0 上线拿到真实分布再定阈值。
- 关联成本：开关变更打穿 provider cache prefix（§3.3），一次重建 ≈ 一次性重付
  整段未缓存输入。Tools tab 注释需写清，避免用户当成「免费即时」操作来回拨。

### 8.2 中文 server 名的工具名展示

- 图名 `mcp_钉钉文档_get_document_info` 在 mono 列中 CJK 宽度是拉丁的两倍，
  原型用 `max-width + ellipsis` 截断；需补**悬停 title 全名 + 点击复制**（Tools
  tab 与 Permissions 添加框都要），否则用户没法把全名粘进 Security → Permissions。
- 重命名 server（= 改配置键）会级联失效：会话 `mcp_tool_names` 指纹（旧前缀
  整体 orphaned → 重建）、permissions 模式（旧前缀成死模式，UI 不自动迁移——
  明确不自动改写 settings.json 的 permissions，二次确认弹窗里提示用户手改或
  到 Security 页清理）。
- 开放问题：provider 侧对非 ASCII 工具名的兼容边界。现网 GLM/qwen 系 API 已在用
  且正常（钉钉一族即中文名）；若未来接严格 `^[a-zA-Z0-9_-]+$` 的 provider，需要
  「配置键 vs 显示名 vs 工具名 slug」三层拆分，牵动 `_full_tool_name` 单一事实源，
  届时另立设计，本期明确不做。

### 8.3 URL 中 key 的脱敏展示

- 钉钉网关形态 `?key=yyy` 决定了 URL 本身就是凭证。规则：
  - **status 的 `url` 字段**：后端脱敏——query 参数名命中
    `/key|token|secret|password|api_?key|access_?token/i` 的值替换为 `***`，path
    保留（卡片、详情副行、向导预览全部只吃这个字段）；
  - **Configuration 的 URL 输入框**：password 型遮蔽 + 眼睛切换，显示真值——
    若显示脱敏值，用户一保存就会把 `***` 写回文件（这是原型未覆盖的坑，以
    「真值 + 遮蔽」解决）；
  - **Advanced JSON**：真值明文（逃生舱语义，本地文件本就可读），折叠区副注
    提醒；
  - **Activity / toast / 错误行**：一律用后端已脱敏的 `error` / `url`，前端不再
    自行拼接原始 URL。
- `env` 值同理：GET config 返回真值，UI password 遮蔽 + 眼睛切换；不新增
  「写入即打码」的存储层方案（保持 mcp.json 是唯一事实源、明文可手改）。

### 8.4 其他开放问题

- **`up 3d` 的时钟基准**：connectedAt 是进程内 wall-clock，应用重启后「up 时长」
  归零而非累计。可接受（展示「本次连接时长」），文案写 `connected for 3d` 防
  歧义。
- **两个设置页并发写 settings.json**（MCP Permissions tab 与 Security 页）：
  沿用现状整文件读改写、后写者胜；若实际出现互相覆盖的反馈，再考虑 settings
  写入加版本号乐观锁（独立小改动，不阻塞本期）。
- **`mcpServers`/`servers` 双顶层键**：`load()` 兜底认 `servers`，但 UI 写回时
  永远归一成 `mcpServers`（PUT 前检测转换），避免同一文件两种键并存漂移。
- **原型与实现的已知偏差清单**（实现时以此文档为准）：冷却 120s（原型 Activity
  文案写 60s，改文案）；`up 3d` 需后端新增时间戳（P0）；Test Connection 需新
  端点（§4.4，P1）；模板 chips 数据是硬编码前端常量（P1，不进 mcp.json）。

## 9. 文件清单（实现时触达）

| 层 | 文件 | 变更 |
|---|---|---|
| 后端 | `packages/runtime/src/ginno_runtime/mcp/registry.py` | schema 解析（enabled/disabled_tools）、过滤、status 扩展、时间戳、`?server=` 逐出/补连 |
| 后端 | `packages/runtime/src/ginno_runtime/api/config.py` | `?tools=1`、reconnect `?server=`、P1 `POST /api/mcp/test` |
| 前端 | `apps/web/src/components/settings/McpSettings.tsx` | 重写为列表页（+ 详情/向导/导入新组件文件） |
| 前端 | `apps/web/src/lib/runtime.ts` | `McpServerStatus` 类型扩展、`reconnectMcp(server?)`、`testMcp()` |
| 前端 | `apps/web/messages/{en,zh-CN}/settings.json` | `settings.mcp.*` 新 key（英文文案以原型为底稿） |
| 前端 | `apps/web/src/components/settings/settingsGroups.ts` | **不动**（IA 不变，MCP 仍在 connections 组） |
| 测试 | `packages/runtime` 单测 | CJK/下划线交叠前缀归属、enabled/disabled drift、`?server=` 绕冷却、脱敏函数 |
