# MCP Apps 支持设计：让 MCP server 的交互 UI 渲染进 Ginno

> 来源：2026-11-07 MCP 调研 + 官方规范核实（modelcontextprotocol.io/extensions/apps）。
> 现状：Ginno 接入 MCP server 完备，但 MCP Apps（工具附带交互 UI）**零支持**——接入后 UI 被静默丢弃。
> 预估：P0 ~8h · P1 ~4h · P2 挂起（与 workspace 联动）。

## 0. MCP Apps 是什么（官方机制，已核实）

server 在 tool description 的 `_meta` 里声明 UI 引用，host 渲染交互界面进对话：

```
① tool description: _meta.ui = { resourceUri: "ui://app/main", csp, permissions }
② host 预载该 resource（HTML，常内联 JS/CSS；外部源受 csp 限制）
③ tool 被调用 → host 在对话流内以 sandboxed iframe 渲染该 HTML
④ app ⇄ host 走 postMessage JSON-RPC（MCP 方言）：
     共享核心：tools/call（app 可回调 server 工具）
     类似：ui/initialize
     新增：ui/ 前缀方法（host 可向 app 推送数据/上下文更新）
⑤ 安全：iframe sandbox 强隔离（不可碰 parent DOM/cookies/storage），
     host 控制能力开关（可限制 app 能调哪些工具、禁用 openLink 等）
```

**价值**：数据可视化/表单/仪表盘直接长在对话里，双向数据流复用 MCP 既有模式，无需 app 自建 API。

## 1. Ginno 差距（调研实证）

| 缺什么 | 证据 |
|---|---|
| registry 只 `initialize + list_tools + call_tool` | `mcp/registry.py` |
| resources 不消费 | `read_resource`/`list_resources` 全仓库 0 匹配 |
| `_meta` 不透传 | `_wrap_tool` 只取 name/description/inputSchema |
| 前端无沙箱 iframe、无 postMessage 桥 | 无 `ui/*` 实现 |

## 2. 架构设计

```
┌─ runtime (Python) ─────────────────────────────────────────────┐
│ MCPRegistry                                                     │
│ ① _wrap_tool: 透传 tool._meta（挂 LangChain StructuredTool      │
│    metadata.ui = {resourceUri, csp, permissions}）              │
│ ② 新增 read_resource(server, uri) → HTML 字符串                 │
│ ③ 新 API：                                                      │
│    GET  /api/mcp/ui?server&tool → {html, csp, permissions}     │
│         （前端按需取 ui:// resource，runtime 代理，前端不直连 server）│
│    POST /api/mcp/call {server, tool, args, via: "mcp-app"}     │
│         （app 回调工具，绕过 LLM 直调 session.call_tool；        │
│          必须过 permission 层：deny→ask→allow，ask 弹权限卡）     │
└────────────────────────────────────────────────────────────────┘
                      │ HTTP（Ginno 前端不直连 MCP server）
┌─ web (React) ──────────────────────────────────────────────────┐
│ McpAppCard（对话流内，tool 结果带 ui 时渲染）                     │
│  <iframe sandbox="allow-scripts" srcdoc={html} />              │
│  sandbox 不给 allow-same-origin；permissions 按声明附加           │
│                                                                  │
│ McpAppBridge（postMessage JSON-RPC 方言实现）                    │
│  app→host: ui/initialize, tools/call→POST /api/mcp/call,        │
│            消息 schema 白名单校验（未知方法丢弃+日志）             │
│  host→app: ui/initialize 应答、tool 结果推送、上下文更新          │
│  targetOrigin 严格化 / 来源 event.origin 校验                     │
│                                                                  │
│ 呈现容器（分层）：P0 对话流卡片 → P1 Pin 浮窗常驻 → P2 workspace tab │
└────────────────────────────────────────────────────────────────┘
```

### 关键决策：自研桥，不用 `@mcp-ui/client`

`@mcp-ui/client` 假定 host（浏览器）**直连** MCP server（http 传输）。Ginno 的 server 连接全在 Python 侧（含 stdio，浏览器根本无法直连），前端只做代理。故选 **AppBridge 思路自研**：前端桥只实现 postMessage 方言 + 经 runtime API 代理 resource 拉取与工具调用。零新增 npm 依赖。

### 权限与安全

- app 发起 `tools/call` 与 LLM 发起同权：走 `permission/policy.py` 同一套 deny→ask→allow（`via: "mcp-app"` 标记来源，ask 卡片注明"由 MCP App xxx 发起"）
- iframe `sandbox="allow-scripts"` 起步（无 same-origin）；`_meta.ui.permissions`（麦克风/摄像头等）映射到 sandbox 附加 token，UI 显性提示
- `_meta.ui.csp` 透传为 iframe 文档 CSP；未声明 csp 的外部加载一律拒绝（默认最严）
- postMessage 双向校验：`event.origin`/`source` 比对 + JSON-RPC method 白名单

### 呈现位置（与已挂起 workspace 的关系）

- **P0：对话流内卡片**（官方形态）：McpAppCard **附加**于普通工具气泡上方（气泡本身保留，无 UI 时零影响），高度自适应（max 420px，可折叠）
- **P1：Pin 浮窗**：复用现有 PinApp——McpAppCard 可"钉住"变常驻小窗（仪表盘类 app 价值最大），Ginno 已有完整 pin 基建
- **P2（挂起联动）**：`mcp_app` 注册为 tabbed workspace 第五种 tab 类型——workspace 在 `docs/parked/` 排队中，两者重启时合并实施（README 已记联动点）

### P0 实现决策记录（2026-11-07，commit feat/mcp-apps-p0）

1. **工具块 UI 发现 = 方案 a（轻量清单端点）**：`GET /api/mcp/tools-with-ui`
   返回 `{graphName: {server, tool, resourceUri, csp, permissions}}`，前端 store
   （`lib/mcpApps.ts`）单飞拉取 + 5min stale 缓存，渲染时同步 `lookup(graphName)`。
   不动 WS 帧 schema（改帧会波及历史会话回放），工具集变化靠 stale 过期或下次挂载收口。
2. **权限 ask 收敛（P0）**：app 发起的 `POST /api/mcp/call` 走
   `permission/policy.decide(图内全名, repr(args))`——与 graph permission node 完全
   同构（含 `bypass_permissions` 特权跳过）。**deny 与 ask 都 403**，错误体带
   `decision` 与双语指引（"将 `mcp_<server>_<tool>(*)` 加入 permissions.allow"）；
   ask 弹卡回环（interrupt 复用）属 P1。
3. **安全形态**：iframe `sandbox="allow-scripts"`（无 allow-same-origin，绝不加）；
   入站 `event.source === iframe.contentWindow` 严格比对（沙箱 iframe 是不透明源，
   origin 恒 "null"，source 比对是唯一可靠校验）；出站 postMessage 用 `"*"`
   （不透明源无法收窄 targetOrigin，payload 仅 initialize 应答与 ui/data，零凭证）；
   入站 method 白名单 `ui/initialize` / `tools/call` / `logging/*`，未知丢弃+warn。
4. **permissions/csp P0 形态**：`_meta.ui.permissions` 原样透传到卡片提示条 +
   iframe `allow` 属性拼接（逐 token 精确映射与麦克风授权流留给 P1
   transcript-server 验证）；`csp` 已透传到前端但 P0 不注入 srcdoc 文档头
   （P1 做"未声明 csp 拒绝外部加载"的默认最严策略）。
5. **呈现细节**：app 卡**附加**在普通工具气泡上方（不替换——无 UI/拉取失败
   恒回退现状）；IntersectionObserver 懒建 iframe（rootMargin 400px，
   MermaidBlock 惯例）；iframe 固定 360px 高（P0 无 resize 协议）+ 可折叠，
   容器约束在 420px 内。
6. **runtime 侧**：`_meta.ui` 经 StructuredTool.metadata（`mcp_ui`/`mcp_server` 键）
   透传；`read_resource` Text/Blob(base64→utf-8) 拼接；`call_tool` 与 graph 工具
   包装器共用 `_content_texts` 提取（两条路径永不漂移），额外透传
   `structuredContent`；会话解析复用 `_resolve_live` 重连语义（reload 后旧实例
   自动路由到新连接）。

## 2.5 参考实现：Skybridge（alpic-ai/skybridge，MIT）

> 调研核实：2026-11-07（README + 官网 + 仓库结构）。npm 100K 月下载，OpenAI 官方博客推荐。

**定位澄清（避免误用）**：Skybridge 是 **app 侧**全栈 TS/React 框架——帮开发者**写** MCP App（`skybridge/server` 定义 tool+view、`skybridge/web` 提供 useToolInfo 等 hooks），**不是 host 渲染库**。Ginno 要做的是 host 侧。但它对 Ginno 有三个直接参考点：

| 参考点 | 位置 | 对 Ginno 的用途 |
|---|---|---|
| app 视图桥（协议 client 端实现） | `packages/core/src/web` | Ginno `McpAppBridge`（host 端）的消息 schema 与它对齐 ⇒ **Skybridge 生态的 app（号称 Claude/ChatGPT 里的 10%）在 Ginno 里直接可跑** |
| 本地 emulator（开源 host 参考） | `packages/devtools`（vite app） | 它模拟 Claude/ChatGPT 渲染 MCP Apps——iframe 加载 view、Preview/Inputs/Context/History 面板、HMR 注入，是 McpAppCard 最现成的同类实现，P0 编码前通读 |
| **官方 basic-host（最权威参考）** | `modelcontextprotocol/ext-apps` 仓库 `examples/basic-host` | 官方自带的最小 host：选工具 → 调用 → 取 ui:// resource → 沙箱 iframe 渲染 → 代理 app 的 tools/call。**Ginno P0 骨架直接对照它实现**（比 Skybridge emulator 更贴协议本体） |
| 端到端验收样本 | `examples/`（everything/flight-booking/productivity/capitals…） | P0 验收集从"官方 qr-server"升级为：官方 qr-server（协议最小闭环）+ Skybridge everything（生态兼容）双样本 |

**生态位价值（写 app 用，非 host 用）**：将来给 Ginno 生态自研 app（trending 浏览器、任务监控仪表盘）可直接用 Skybridge 写——一次编写同时跑 Claude / ChatGPT / VSCode / Ginno，不绑私有形态；其 agent skill（`npx skills add alpic-ai/skybridge`）与 devtools audit（对照 host 合规指南自动检查）也是 Ginno 开发流可复用的工具。

## 2.6 e2e 验收集（官方 ext-apps examples，2026-11-07 已核实 22 个在列）

仓主：`modelcontextprotocol/ext-apps`，`examples/` 目录；一键编排：`cd examples && bun run-all.ts start`（起 basic-host `:8080` + 全部 server，端口 3101+；`EXAMPLE=<folder>` 可单跑）。

| Demo | 验证点 | 分期归属 |
|---|---|---|
| **qr-server**（Python） | 最小闭环冒烟：工具→ui:// resource→iframe→交互回调；兼验非 TS 生态 | P0 双样本之一 |
| **map-server**（CesiumJS 3D 地球，支持 npx 零 clone） | 复杂 UI：外部 CDN 资源 + CSP、geocoding 交互回调 | P0 双样本之二 |
| basic-server-react / vanillajs 等 7 框架 starter | 框架无关性抽查（P0 不必全跑） | 备用 |
| **system-monitor-server** | host 持续推送 / 实时刷新 | P1 |
| **transcript-server**（按住说话 STT） | `_meta.ui.permissions` 麦克风授权流 | P1 |
| **lazy-auth-server** | 权限层联动 / 懒授权 | P1 |
| say-server（TTS）/ shadertoy（着色器）/ threejs / video / pdf / sheet-music | 富媒体、高帧率、语音 API | P1 抽查 |
| budget-allocator / scenario-modeler / customer-segmentation / wiki-explorer / cohort-heatmap | 多控件 → tools/call 密集回调（节流验证） | P1 抽查 |
| debug-server / integration-server | 开发调试 | 开发期 |
| Skybridge `examples/everything`、`flight-booking` | 生态兼容（Skybridge 框架写的 app 在 Ginno 可跑） | P0/P1 第三样本 |

**执行顺序**：① 先用 basic-host 把 P0 双样本跑通（排除 app 自身变量，不依赖 Ginno 改造）→ ② Ginno host P0 落地后同两样本复跑（mcp.json stdio 接入）→ ③ P1 加 system-monitor + transcript + Skybridge flight-booking。


## 3. 分期

| 期 | 内容 | 量 | 验证 |
|---|---|---|---|
| P0 | runtime：_meta 透传 + read_resource + `/api/mcp/ui` + `/api/mcp/call`（权限）；前端：McpAppCard + McpAppBridge 最小闭环（initialize/tools/call/结果推送） | ~8h | 按 §2.6 执行顺序 ①②：qr-server + map-server 双样本端到端 ✅ done 2026-11-07（CI 编译级验证；e2e 按 §2.6 待办） |
| P1 | Pin 浮窗承载 + csp/permissions 完整支持 + app 发起权限卡的 UI 回环 + 未声明 csp 的降级策略 | ~4h | 按 §2.6 顺序 ③：system-monitor + transcript + Skybridge flight-booking |
| P2 | workspace tab 集成（随 workspace 重启） | — | 挂起 |

P0 单测：registry `_meta` 透传、`/api/mcp/call` 权限拒绝路径（mock session）；前端桥的 schema 白名单（vitest，node 环境可跑）。

## 4. 风险

- mcp SDK 1.28.1 `ClientSession.read_resource` 为标配 API（无版本风险）
- srcdoc 大 HTML（内联 bundle 数百 KB）的渲染性能：懒渲染（进入视口才建 iframe）
- app 密集 `tools/call` 打爆 server：桥侧串行队列 + 最小间隔节流
- 本机无 node/pnpm：前端部分需 CI 或 `pnpm dev` 验证（同通知统一 P0 的验证深度声明）
