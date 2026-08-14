# Browser Embedding Design（内嵌浏览器：聊天 + 工作流 + Goal）

> 状态：**M2 协议层 + atrium 挖洞 + Frameworks + Helper.app + C 宿主已进包**。目标：在 Ginno 里内嵌一台带 Space 的真 Chromium 浏览器——
> 复制 ego-lite 的体验（真页面、真标签、真接管、登录共享），并作为**一等能力**同时服务
> 聊天、工作流节点、Goal 自主推进。
>
> 参考对象：
> - [citrolabs/ego-lite](https://github.com/citrolabs/ego-lite) — 产品契约（Space / handoff / 所有权 / helpers）
> - [atrium — From WKWebView to CEF](https://getatrium.dev/blog/embedding-real-browser-tauri) — Tauri 内嵌 Chromium 的工程路径
> - OpenHuman（tinyhumansai/openhuman）— CEF fork + child webview + CDP
>
> 引擎：生产优先 **打包 CEF 原生子视图**（Helper.app + `libginno_cef.dylib` + 宿主
> `cef_initialize` 写出 `~/.ginno/browser/cef-cdp.json`）。`try_cef()` 只在 helpers
> **并且** 宿主 CDP 真的在听时才返回实例；否则回退无头系统 Chrome screencast，
> **不会假装 native tile 活着**。Rust 壳按 atrium 挖洞，并把 CEF 挂成 hole NSView
> 的子视图。节点 / 协议 / 数据模型在引擎切换时不动。不弹出系统 Chrome 窗口。

---

## 0. TL;DR

- **产品目标**：Ginno 工作区变成 **Chat | Browser** 分栏。右边不是截图回放，是一台
  带地址栏、多标签、下载、Space 的真 Chromium。人日常可在 Human Space 里逛，Agent 在
  自己的 Space 里干活，登录共用，互不抢焦点。要登录 / 过验证码时，点「接管」，手点在
  **右侧分栏的画面上**（M1：CDP screencast + Input.dispatch；M2：CEF 原生子视图）。
  不弹出系统 Chrome。
- **不做**：不把 `ego lite.app` 嵌进 Ginno；不把 WKWebView 子 webview 当 ego 替身
  （atrium 已证伪）。
- **产品契约 = ego-lite 的契约**。helpers / Space / 三态所有权 / handoff 纪律照抄，
  让模型从 Claude Code 过来几乎不用改写法。
- **一等能力**：浏览器**不只是聊天的工具**。聊天、`step`（AgentNode）可选调用、
  **工作流节点 `type: "browser"`**（M1 就能跑通带 handoff 的 workflow）、Goal 自主推进
  共用同一套 supervisor 与 Space。
- **技术主线**：`BrowserSupervisor`（sidecar，权威）+ 浏览器引擎（M1 dock、M2 CEF）
  + 前端 pane（`<canvas>` 或 CEF tile）+ Ginno Tauri 壳（**唯一破例**：视图托管与几何同步，业务仍不进 Rust）。
- **协议层唯一硬改动**：`handOff()` → 图 `interrupt({kind:"browser_handoff"})`；
  `takeOver()` → `/decide` / `/resume`。绕过现有 `bash` 30s 超时与 Goal 无头续跑。
- **登录态 Agent = 用户本人**。产品级默认可见、默认可接管、高风险域强制 ask。
- **Snapshot**：CDP accessibility + 自研 refMap（`@N` / `loc=`），**诚实 85%**（跨源
  iframe / 封闭 shadow 做不到 ego 的内核级，不写进 M1 承诺）。
- **M1 里程碑**：内嵌可用（Chat|Browser 分栏 + Space 列表 + chip + 内置 `/browse` +
  `browser_eval` 工具 + Chrome 导入向导 + workflow `browser` 节点 + handoff 卡）。

---

## 1. 为什么不是「接 ego-lite」

ego-lite 本身是独立 Chromium 应用，Space 只活在它自己的窗口里，没有可嵌入的 WebView /
iframe API。ego-lite 的集成分三层，由浅到深：

| 层 | 内容 | 是否进入默认产品 |
|---|---|---|
| **契约层** | helpers / Space / 所有权 / SKILL 语气按 ego 对齐 | ✅ 立即 |
| **实现层（主线）** | Ginno 自己的 Chromium 宿主 + sidecar supervisor | ✅ 主线 |
| **后端层（M3 可选）** | 本机已有 ego-lite 时，`browser_eval` 可切到 `ego-browser` 进程 | 可选 |

**不要做的事**：

1. 不要把 `ego lite.app` 嵌进 Ginno。ego 没给嵌入 API，Space 也不打算跨进程。
2. 不要把 WKWebView 当 ego 替身。atrium 的实测：OAuth / CDP / 点击合成全挂，后来换成 CEF。
3. 不要拿 `web_fetch` 当集成路径——公网只读、禁 localhost，做不了登录态浏览器。

---

## 2. ego-lite 的完整体验契约（要复制的部分）

| # | 契约 | Ginno 必须达到 |
|---|---|---|
| 1 | 真 Chromium，不是 headless / 不是截图 | Pane 里是活的浏览器 |
| 2 | 人的标签页和 Agent Space 隔离，同进程共享登录 | Space 一等公民，不绑死「每会话一个」 |
| 3 | 首次从 Chrome 迁 profile（cookie / 扩展 / 书签） | M1 入门向导 |
| 4 | 点进 Space 就能看、能接管，原生指针键盘 | 接管时必须原生输入，不能点在假画面上 |
| 5 | 所有权 `agent \| agentDelegatedToUser \| user` | 三态原样搬 |
| 6 | 验证码 / 扫码 / 支付 → handoff → 人做完 → takeOver | 独立 HITL 卡，不是套权限框 |
| 7 | Code-base：一次 JS 写完整段，helpers 预注入 | API 兼容 ego-browser helpers |
| 8 | `snapshot` + `@ref` + `loc=`，跨 iframe / shadow | 目标对齐；内核级 snapshot 做不到就明示 85% |
| 9 | `complete({ keep })` 单独收尾，标签留下给人审计 | 原样：工作脚本和关闭脚本分开 |
| 10 | 不抢人正在看的页 | Agent 动 Space 时，Human Space 不动 |
| 11 | 地址栏 / 多标签 / 下载 / 扩展 | Space 里要有真 chrome（至少地址栏+标签+下载） |
| 12 | `learnings` 越用越快 | M4（ego 自己也标着 coming soon） |

---

## 3. 工作区与 UI

### 3.1 工作区布局

```
┌──────────────┬──────────────────────────────────────────┐
│ Chat         │  Spaces: [我的] [列 GitHub issues ●]  ＋  │
│              │  ← → ↻  github.com/issues     [接管]     │
│ 工具气泡：   │ ┌──────────────────────────────────────┐ │
│   browser_   │ │  真 Chromium（标签 / 地址栏 / 页面）  │ │
│   eval 跑完  │ │                                      │ │
│   一段 JS    │ └──────────────────────────────────────┘ │
│              │  ownership: agentDelegatedToUser         │
│ 「需要你扫码」│                                          │
└──────────────┴──────────────────────────────────────────┘
```

- 右栏现有 Artifacts / TODO / Workflow / Memory **不动**，浏览器是工作区分栏，不是第五个 tab
- TopBar **Browser chip**：当前 Space 名、URL、三态所有权
- **handoff 卡**：独立样式（不是权限 ask），带「去浏览器」按钮、超时倒计时
- 任务结束 `keep: true`：Space 留着，人可以点进去审计

### 3.2 Space 列表与接管

| 行为 | 触发 |
|---|---|
| 新建 Agent Space | 工具调用 `useOrCreate(name)` |
| 切换当前展示的 Space | 点 Space 条 |
| 接管 | 点画面或点「接管」→ ownership=`agentDelegatedToUser`，工具全部硬停 |
| 交还 | 聊天「交还」/ 在 Space 内点浮标 → `takeOverTaskSpace` |
| 保持 / 关掉 | 对应 `completeTaskSpace({ keep })`，必须**单独一轮** |

---

## 4. 协议：所有权三态与 handoff

### 4.1 状态机

```
useOrCreate(name)     → ownership=agent
        │
   干活（一次 JS）
        │
   要人？handOff()    → agentDelegatedToUser
        │                 Agent 工具全部硬停
        │                 人在 Pane 里原生操作
   人点「交还」或聊天说继续
        │
   takeOver()         → ownership=agent
        │
   下一轮模型被要求 takeOver(同一 name)，禁止新开 Space
        │
   确认干完后，单独一段 complete({ keep })
```

### 4.2 与 ego 完全一致的硬规矩

- 同一目标跟进、纠错、复验，**先复用原 Space**
- `complete({ keep })` **单独一轮**，禁止和工作脚本写在一起（ego [PR #29](https://github.com/citrolabs/ego-lite/pull/29)）
- `handOff` / `complete(keep)` 打到 user-owned Space → `{ done: false, skipped: 'user-owned' }`
- `agentDelegatedToUser` 仍算 Agent 的 Space，只是控制权临时在人

### 4.3 Ginno 唯一硬改动

ego 的 `waitForAgentControl` 会堵住 Node 进程。Ginno 的 `bash` 默认 30s，Goal 还会在空闲时续跑。所以：

```text
browser_eval 里调用 handOff()
    → JS 运行时返回 { interrupt: "handoff", space }
    → 图 interrupt({ kind: "browser_handoff", space_id, url, reason })
    → WS browser.handoff（卡片 + chip 变「需要你」）
    → 人在 Pane 原生操作，点「交还」
    → permission_response 同款 resume
    → 下一轮模型被要求 takeOver(同一 name)，禁止新 Space
```

这是相对 ego CLI 的**唯一协议级偏差**。`browser_eval` 超时默认 **180s**，不走 bash。JS 运行时常驻，Space / refMap 跨轮存活。

---

## 5. Agent 契约：兼容 ego-browser

模型侧尽量长得像 [ego-browser SKILL](https://github.com/citrolabs/ego-lite/blob/main/skills/ego-browser/SKILL.md)：

```js
await useOrCreateTaskSpace('list github issues')
await openOrReuseTab('https://github.com/issues', { wait: true, timeout: 20 })
cliLog(await snapshotText())
await click('@12')
```

### 5.1 Helpers（照抄 ego-browser）

| 分组 | helpers |
|---|---|
| Task space | `listTaskSpaces` / `useOrCreateTaskSpace` / `claimTaskSpace` / `handOffTaskSpace` / `takeOverTaskSpace` / `waitForAgentControl` / `completeTaskSpace` / `closeTaskSpace` |
| Navigation | `listTabs` / `openOrReuseTab` / `closeTab` / `gotoAndWait` / `currentTab` / `switchTab` / `gotoUrl` / `pageInfo` / `ensureRealTab` |
| Observation | `snapshotText` / `captureScreenshot` / `drainEvents` |
| Interaction | `click` / `fillInput` / `hover` / `select` / `scroll` / `dispatchKey` / `uploadFile` |
| JS / CDP | `js` / `elementEval` / `cdp` / `fetch.server` / `fetch.browser` |
| Logging | `cliLog` |

### 5.2 Ginno 的入口

不强迫模型去 `bash` 调 `ego-browser`。Ginno 给的是：

- 一等工具 `browser_eval(code, space?)`：helpers 预注入（M0 把 ego 方言转成 Python 再 `exec`）
- 内置 skill `/browse`：正文替换 + **本轮授予 frontmatter `tools:`（`browser_*`）**，不挑当前 Agent
- 显式工具：`browser_snapshot` / `browser_handoff` / `browser_screenshot`
- `openOrReuseTab` 会把 `ai.sf-express.com` 补成 `https://…`，等到非 `about:blank`；登录墙会在返回值里标 `login_wall`
- Chrome 新标签必须 `PUT /json/new`（GET = 405，Space 会停在空白页）

### 5.3 选择规则（写进 WorldState / skill）

- 公网静态页 → 继续 `web_fetch`
- 要登录 / 点击 / SPA / 「打开网站」→ 必须 `browser_eval`
- 有活跃 Space 时，公开页也可以用浏览器（审计同一画面）
- **禁止**用 `mcp_playwright_*` 当浏览器——那是匿名无头的替代，不是产品

### 5.4 `browser_eval` 工具契约

| 参数 | 类型 | 含义 |
|---|---|---|
| `space` | string | Space 名（3–6 词，自然语言）；缺省 = `session-{{sid}}` |
| `code` | string | JS 代码，helpers 预注入 |
| `headed` | bool | 是否抢焦点（默认 true；handoff 时强制 true） |
| `timeout_s` | int | 180s 上限，默认 60 |

返回值：

- `{ ok: true, log: [...], return: any }` — 正常完成
- `{ interrupt: "handoff", space, url, reason }` — 要人
- `{ error: "[error] ...", recoverable: bool }` — 失败，agent 可重试

---

## 6. Snapshot 与 ref

### 6.1 语义

仿 ego 的「快照 + ref」：

```text
[ref=3, loc='#main .item:nth-of-type(2) a', url='https://example.com/p/1']
  [heading] 产品 X
  [link] 查看详情
  [ref=4, loc='...', url='...']
    [button] 加入购物车
```

- `@N`：基于 CDP `backendNodeId`，跨快照通常稳定，但只在最近一次 snapshot 的 refMap 内有效
- `loc=`：跨轮稳定选择器（CSS / xpath / loc=），是 `@N` 失效时的 fallback
- 目标：主文档 + 同源 iframe 全覆盖；跨源 + 封闭 shadow = **85%**，不写进 M1 承诺

### 6.2 学习（learnings）

M4：成功操作序列蒸馏进 `~/.ginno/browser/learnings/{domain}.md`，每次 helper 调用自动注入该域经验。ego 的 learnings 是 coming soon，Ginno 可以晚一步。

---

## 7. 技术架构

### 7.1 进程拓扑

```
┌─ Tauri 主窗口 ─────────────────────────────────────┐
│  WKWebView（现有 UI，同源 8787）                    │
│   Chat | [占位矩形 = Browser tile 的布局盒]         │
│                         ▲                           │
│                         │ 同步 bounds                 │
│  ┌──────────────────────┴────────────────────────┐  │
│  │  真 Chromium 视图（M1 dock / M2 CEF child）   │  │
│  └───────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────┘
                         │ CDP / 内部 RPC
                         ▼
Python sidecar
  BrowserSupervisor
    • Spaces（= BrowserContext）
    • ownership 状态机
    • JS 运行时（预注入 helpers）
    • 与 Goal / permission / WS 对接
                         │
                  ~/.ginno/browser/
                    profile/     持久登录
                    spaces.json  Space 元数据
                    learnings/   M4
```

### 7.2 职责切分

| 层 | 职责 | 守「Tauri 零业务」？ |
|---|---|---|
| Tauri | 托管 Chromium 视图、同步 tile 矩形、把焦点交给/收回 Pane | **破例**，只做视图托管和几何同步 |
| Sidecar | Space、所有权、`browser_eval`、HITL、Goal 停手 | 业务权威 |
| Web UI | 分栏、chip、handoff 卡、Space 列表 | 不画网页本身 |

### 7.3 引擎两阶段

**M1（screencast）**：系统 Chrome / Chromium，独立 `user-data-dir=~/.ginno/browser/profile`，**无头**。Ginno 用 `Page.startScreencast` 把 JPEG 画进右侧分栏，鼠标/键盘经 `Input.dispatch*` 打回页面。接管 = 手点在 App 里的这块画面上，不弹系统窗。

**M2（CEF）**：Chromium Embedded Framework 子视图画进主窗口。才能做到无缝分栏、resize、和聊天同一窗口循环。工作量是「换引擎」级：打包体积、签名、`Contents/Frameworks`、崩溃隔离。M1 的 Space / helpers / HITL **全部复用**，只换渲染宿主。

### 7.4 登录

```text
首次打开 Browser Pane
  → 探测本机 Chrome（默认 Profile）
  → 「导入登录态？」
  → Chrome 必须退出（user-data-dir 锁）
  → 拷 cookie / local storage / 可选扩展
  → 写入 ~/.ginno/browser/profile
  → 之后 Agent 和 Human Space 都从这套 profile 长 BrowserContext
```

- 禁止去抢正在跑的 Chrome 的 `user-data-dir`
- Cookie 不进 checkpoint、不进 MEMORY、不进 usage 日志

---

## 8. 与现有能力的关系

| 现有 | 调整后 |
|---|---|
| Playwright MCP（headless、新鲜 profile） | **保留**给无登录、无 UI 的脚本 / e2e。设置里标明「匿名无头」 |
| `web_search` / `web_fetch` | 保留。`web_fetch` 继续禁 localhost，**绝不**拿它打 CDP |
| `bash` + 外挂 `ego-browser` | 可选后端。默认 Agent **不要**走这条（30s 超时、无 HITL、画面在窗外） |
| MCP 截图包装 | 仍然是文本。真截图走 `captureScreenshot` → 会话文件 / Artifacts |
| Goal driver | `agentDelegatedToUser` 时**禁止**无头续跑 |
| Research / Writer | 给 `browser_*`，不要指望他们靠 `bash` 调 CLI |

---

## 9. 工作流：浏览器是一等节点

### 9.1 为什么 step 不够

| 只让 step 调浏览器 | 会炸的点 |
|---|---|
| `max_tool_iters=8` | 又变回 Playwright 那种一步一工具 |
| 暂停后**整步重跑**（现成语义） | Space 会被重开，登录/标签丢 |
| 无头 run、人没在看 | 验证码来了没地方接管 |
| `complete({ keep })` | 模型随手放在工作脚本里，ego 已踩过坑 |
| 确定性「打开 URL → 抽表」 | 不该烧一轮 LLM |

**结论：工作流要两种用法，`browser` 必须是节点类型。**

### 9.2 DSL：新增 `type: "browser"`

节点系统本来就是为这个留的口。`NODE_TYPES_V1` 加 `browser`，`@register_node class BrowserNode`。

```jsonc
{
  "id": "open_approvals",
  "type": "browser",
  "space": "dingtalk-approvals",          // 3–6 词；同 run 内同名复用
  "action": "eval",                       // eval | snapshot | handoff | complete
  "code": "await openOrReuseTab('{{url}}', { wait: true })\nconst t = await snapshotText()\nreturn { title: (await pageInfo()).title, snapshot: t }",
  "keep": true,                           // complete 时才看
  "timeout_s": 180,
  "headed": true,                         // false = 不抢焦点，仍用同一 profile
  "writes": ["page"]                      // return 值写入 context
}
```

| action | 行为 | 失败 / HITL |
|---|---|---|
| `eval` | 跑 `code`（helpers 与聊天同一套） | 脚本里 `handOff()` → 本节点 interrupt |
| `snapshot` | 只观察，写 `snapshot` / `url` / `title` | 无 Space 则先 `useOrCreate` |
| `handoff` | 等价 `handOffTaskSpace` + 等交还 | 就是一次 interrupt |
| `complete` | **单独节点**，对应 ego 的收尾 heredoc | `keep:false` 关 Space；`keep:true` 留给审计 |

### 9.3 校验纪律

1. 同一 `space` 名在同一 run 内必须复用，禁止同名重建
2. `complete` 不能和 `eval` 写在同一个节点（对齐 ego [PR #29](https://github.com/citrolabs/ego-lite/pull/29)）
3. `code` 走现有表达式沙箱的**另一层**：只允许预注入 helpers，禁止 `import` / `fs` / `child_process`
4. `{{context.x}}` 只做字符串替换，再交给 JS，避免表达式引擎和 JS 混求值

`loop` 的 `body` 可以是 browser 节点——「对 `context.urls` 每一项打开并抽取」是第一等配方，不需要先让 LLM 发明循环。

### 9.4 执行：Space 跟 run，不跟 step 的消息历史

```text
run 启动
  BrowserSupervisor.attach(run_id)
      default space name = "wf-{{workflow.name}}"
      或节点上的 space

browser 节点
  useOrCreate(space)           // ownership=agent
  跑 action
  若 helpers 调了 handOff()
      interrupt({
        kind: "browser_handoff",
        run_id, node, space, url, reason
      })
      // 图暂停；checkpoint 钉在这个节点
      // 与 HumanNode / 手动 pause 同一条 resume 路

人在 Pane 交还
  POST /workflow_runs/{id}/decide { decision: "browser_resume" }
  takeOver(同一 space)         // 禁止新 Space
  节点从 interrupt 之后继续

run 结束
  未显式 complete 的 Space：
    成功 → keep（默认，给人审计）
    失败 / cancel → keep（排障）
    只有 DSL 里的 complete(keep:false) 才关
```

- 聊天共用 supervisor，所以：
  - 聊天里开过的 `dingtalk-approvals`，工作流同名可以 `claim`（要用户点头，默认不抢人的 Human Space）
  - 工作流开的 Space，聊天气泡 / chip 也能看见
  - **禁止**两个 run 默默共用一个 Space（并行会互点）。要共用必须 DSL 写 `space: "shared:foo"` + 运行前确认

- `headed: false`：不抢窗口焦点，截图仍可进事件流。handoff 时**强制升为 headed** 并打开 Pane——没人看的验证码等于卡死

- Goal driver 那条同样适用：run 停在 `browser_handoff` 时，**不准**当普通空闲去续下一个节点

### 9.5 为什么还要保留「step 调浏览器」

`BrowserNode` 解决可重跑脚本。有些 step 目标是「看完再判断」，脚本写不死。

- 给 `browser_*` 做产品内工具（和 todo / goal 一样，**不弹权限**）
- `ensure_browser_tools`：dev / research / writer 的 `tools_allow` 加上 `browser_*`（workflow-dev 不加，它只编 DSL）
- AgentNode 过滤时放行 `browser_*`（今天 MCP 有 `mcp_` 前缀特例，浏览器走同一模式）
- step 的系统提示补一句：有浏览器任务用 `browser_eval`，不要 `web_fetch`，不要 `mcp_playwright_*`

step 里的 handoff：工具返回 `{interrupt:"handoff"}` → **提升成图级 interrupt**（不要让模型在 8 次迭代里空转）。resume 后 step 会整步重跑——这是现成语义。所以 step 路径必须：

- 重跑时 `useOrCreate` 回到**同一个** Space（名字 = `wf-{run8}-{node}` 或模型传入的 name）
- 禁止在 step 里 `complete({keep:false})`（校验 + 运行时拒）

**一句话：探索用 step，流水线用 browser 节点。**

### 9.6 和现有节点 / UI 怎么接

| 现成的 | 怎么用 |
|---|---|
| `human` | 继续问话、改 context。要碰页面用 `browser/handoff`，不要用 human 冒充 |
| 手动 pause | 边界仍在节点之间；`eval` 跑到一半被 pause → 整节点重跑，所以长脚本要自己按阶段拆节点 |
| `run.bind` / 黄徽章 | `browser_handoff` 比 `human` 更抢眼（黄 +「去浏览器」） |
| DAG | globe 图标；边上标 space 名 |
| 事件流 | `browser.space` / `browser.nav` / `browser.handoff` / `browser.extract`（snapshot 截断进 jsonl，原图进会话文件） |
| `summarize-from-session` | 会话里出现过 `/browse` 或 `browser_eval` → 草稿里生成 `browser` 节点，不要压成普通 step |
| `workflow-dev` | 系统提示加 browser 节点 schema；diff 卡里能预览 `code` |

设置页「运行」且没有 `present_in_session_id`：handoff 时自动绑到最近会话，或弹出独立 Browser 窗。**不允许** interrupt 之后没有任何画面。

---

## 10. Goal 与浏览器的交叉

### 10.1 handoff 时 goal 停

- `agentDelegatedToUser` 时 **禁止** Goal driver 自动续跑
- WS 事件 `goal.updated` 的 `status` 不变，但 `browser_state` 字段标 `waiting_human`
- 人交还后，continuation 才恢复
- Goal 无头续跑碰到验证码会空转。`waiting_human` 必须卡住 driver

### 10.2 Goal 长跑场景

- 「每天早上汇总未读邮件」：浏览器会话要能挂着
- 跨天 run：Space / profile 都持久化在 `~/.ginno/browser/`
- 若某天出现新登录墙 → 自动 handoff，等人在 Pane 里解决

### 10.3 Goal driver 与 workflow browser 节点

Goal driver 调 workflow run 时，run 里的 browser 节点照走 interrupt 路。Goal 的无头续跑和 run 的 interrupt 互不冲突。

---

## 11. 权限与安全

### 11.1 默认策略

- `bypass_permissions` 默认 ON（现有 Ginno 默认）仍有效，但浏览器工具走产品内工具路径，**永不弹权限**
- `bypass_permissions` OFF 时：`browser_eval` = `allow`（和 todo/goal/workflow 同级）；高风险域名见 11.2

### 11.2 高风险域

| 域模式 | 行为 |
|---|---|
| `*://*.bank*` / `*://*.alipay*` / `*://*.weixin*` | 强制 `ask`（即便 bypass） |
| `file://` / `about://` | 默认 allow（本地） |
| `localhost:*` / `127.0.0.1:*` | allow（开发） |
| 其他 | allow |

规则写在 `settings.browser.risky_domains`，用户可改。

### 11.3 默认行为

- **默认可见**：Agent 浏览器任务默认 `headed: true`，除非用户显式开「静默模式」
- **默认可接管**：handoff 卡永不超时自动取消（Goal 场景除外）
- **截图归档**：`captureScreenshot` 自动进 Artifacts；会话文件同步
- **Cookie 隔离**：不进 checkpoint / MEMORY / usage

### 11.4 沙箱

JS 运行时只暴露预注入 helpers：

- 禁 `import` / `require` / `fs` / `child_process` / `eval(String)`
- `cdp` 白名单化命令（`Page.*` / `Runtime.evaluate` / `Input.*`），黑名单命令禁（如 `Browser.close` 之外的进程级命令）
- 网络请求走 `fetch.server` / `fetch.browser`，禁止 `net` 直连

---

## 12. 数据模型

### 12.1 磁盘

```text
~/.ginno/browser/
  profile/                    # 持久 user-data-dir
  spaces.json                 # Space 元数据索引
    {
      "spaces": [
        {
          "name": "dingtalk-approvals",
          "owner": "agent",            // agent | agentDelegatedToUser | user
          "bound_run_id": null,        // workflow run 绑定的 run_id
          "bound_session_id": "sid-abc",
          "created_at": 1713000000,
          "tabs": ["tab-1", "tab-2"]
        }
      ]
    }
  learnings/{domain}.md       # M4
  browser_state.json          # 当前 Pane 绑定的 Space / URL / 焦点
```

### 12.2 内存

`server_shared.py` 扩展：

```python
_BROWSER_SUPERVISOR = None     # BrowserSupervisor 单例
```

`BrowserSupervisor`：

```python
class BrowserSupervisor:
    spaces: dict[str, Space]           # name → Space
    bindings: dict[str, str]           # run_id / session_id → space_name
    engine: BrowserEngine              # M1 Chrome / M2 CEF
    runtime: JSRuntime                 # Node 子进程，helpers 预注入
```

### 12.3 WS 事件

| 事件 | 方向 | 内容 |
|---|---|---|
| `browser.space` | server→client | `{ space, owner, url, title, tabs }` |
| `browser.handoff` | server→client | `{ space, url, reason, chip_text }` |
| `browser.frame` | server→client | M1 截图帧 / M2 CEF 原生绘制 |
| `browser.nav` | server→client | `{ space, url, title, refMap }` |
| `browser.extract` | server→client | `{ space, snapshot }`（截断版） |
| `browser.complete` | server→client | `{ space, keep }` |

### 12.4 REST

```text
GET    /api/browser/spaces
GET    /api/browser/spaces/{name}
POST   /api/browser/spaces/{name}/handoff
POST   /api/browser/spaces/{name}/takeover
POST   /api/browser/spaces/{name}/complete    { keep }
GET    /api/browser/spaces/{name}/tabs
POST   /api/browser/spaces/{name}/tabs                 { url, human }
POST   /api/browser/spaces/{name}/tabs/{id}/activate   { human }
POST   /api/browser/spaces/{name}/tabs/{id}/close      { human }
GET    /api/browser/spaces/{name}/downloads
GET    /api/browser/downloads
GET    /api/browser/state
POST   /api/browser/import-chrome
```

---

## 13. 里程碑

```text
M0  对照实现，不进主干（3–5 天）
    无头系统 Chrome + 独立 profile + 分栏 screencast
    helpers 最小集：useOrCreate / snapshotText / click(@N) / handOff
    一张本地带登录墙的 HTML，走完三态所有权
    验收：人能在 App 分栏里输入，Agent 在 handoff 期间点不动

M1  可日常用的 ego 骨架
    Chat|Browser 分栏、Space 列表、chip
    内置 /browse + browser_eval（SKILL 基本抄 ego）
    Chrome 导入向导
    complete({ keep }) 纪律 + 跨轮复用 Space
    Goal 遇 handoff 停
    工作流 type: browser 节点 + handoff 卡 + DAG 图标
    Playwright MCP 仍在，只标成「匿名无头」

M2  真嵌入 + 真 chrome（协议层 + 挖洞 + Frameworks + Helper.app + C 宿主）
    CEF tile：`choose_engine` / `CefEngine` 只在宿主 CDP 活着时返回
    atrium 式 WKWebView 挖洞 + `Contents/Frameworks/{Chromium Embedded Framework, Ginno Helper*.app, libginno_cef.dylib}`
    （宿主没起来 → 仍走 Chrome screencast，不假装 native tile）
    地址栏 / 每 Space 多标签 / 下载进 ~/.ginno/browser/downloads + Artifacts
    高风险域名（及改密 path）强制 ask，并 flip owner → Goal 停
    snapshot 补同源 iframe / 开放 shadow 提示；跨源 / 封闭 shadow 仍省略

M3  体验打磨
    Human Space、多 Space 并行
    站点 learnings（~/.ginno/browser/learnings）
    可选：已安装的 ego-lite 当后端（同一套 helpers）
    workflow-dev 能产出 browser 节点；会话总结能生成 browser 节点

M4  工作流节点 + 无人值守
    workflow 里的 browser step
    定时 Goal（早报未读）——仍遵守 handoff 停手
    learnings 实际跑起来
```

**M0 的通过标准不是「能截到帧」，是「handoff 时手指点在真页面上」。** 过不了就不要做 UI。

---

## 14. 实现落点（拆任务）

| 文件 / 包 | 改什么 |
|---|---|
| 新：`packages/runtime/src/ginno_runtime/browser/` | supervisor / spaces / helpers / profile / engine |
| `workflows/nodes/builtin.py` | `BrowserNode`（eval / snapshot / handoff / complete） |
| `workflows/dsl.py` | `NODE_TYPES_V1` + browser 校验（complete 独立、space 复用） |
| `api/workflows.py` | `_wf_build_deps` 注入 BrowserSupervisor；`decide` 增加 `browser_resume` |
| `graph.py` / agents | `browser_*` 产品内工具 + `ensure_browser_tools` |
| `api/stream.py` / WS | `browser.handoff` / `browser.space` 推到 `present_in` |
| 前端 `workflow/` + `RunBlocks` | 节点图标、handoff 卡、「去浏览器」 |
| 前端新增 `components/browser/` | Pane / Space 条 / chip / handoff 卡 |
| `workflows/synthesis.py` | 从带浏览的会话总结出 browser 节点 |
| `paths.py` | `~/.ginno/browser/` 布局 + settings 新段 |

---

## 15. 风险

| 风险 | 应对 |
|---|---|
| **打包**：M2 上 CEF = 体积、签名、`Frameworks`、和 PyInstaller sidecar 是两套运行时 | M1 靠系统 Chrome；M2 单独流水线；`make app` 扩 target |
| **打破「Tauri 零业务」** | 只允许视图托管。Space 状态若漏进 `lib.rs`，半年后桌面壳会变成第二套运行时 |
| **登录态 Agent = 用户本人** | 比 headless Playwright 危险一个数量级。默认画面可见、默认可接管、支付域强制 ask |
| **ego 内核 snapshot 复制不了** | 定制 Chromium 是他们的护城河。对外话术用「兼容 ego-browser 契约」，不要写「snapshot 质量一致」 |
| **孤儿进程** | ego 自己都有 [Space 关了 renderer 还在](https://github.com/citrolabs/ego-lite/issues/88)。Ginno 退出必须杀浏览器子进程，崩溃要能 reap |
| **平台** | ego 只做 macOS。我们 M1 先 Mac；CEF 路线反而更利于以后 Windows。不要把主线绑死在 `ego lite.app` 上 |
| **Chrome profile 锁** | 不能和正在跑的 Chrome 抢同一个 `user-data-dir`。Ginno 必须用自己的 profile，或只读导入 cookie |
| **Goal driver × 浏览器 HITL** | 无头续跑碰到验证码会空转。`waiting_human` 必须卡住 driver |

---

## 16. 不做的事（明确边界）

1. 不把 ego-lite 当默认引擎。M3 才可选接入。
2. 不用 `web_fetch` 当集成路径。
3. 不强制用户搬家（从 Chrome 迁 profile 是**导入**，不是替代）。
4. 不让浏览器能力进 `workflow-dev` 的工具集（它只编 DSL）。
5. 不在 `research` / `writer` 里**只**用 bash 调 CLI。
6. 不在 M1 承诺跨源 iframe / 封闭 shadow 的 snapshot。
7. 不在 step 里允许 `complete({keep:false})`。
8. 不让 `mcp_playwright_*` 当「已登录浏览器」。

---

## 17. 与现有设计的兼容性

- `architecture.md` 第 6 节：`browser` 作为新小节加入，与 chat graph / workflows / goals 并列
- `workflow-dsl-design.md` §3 节点类型：v1 扩展为 `step / branch / loop / human / browser`
- `goal-design.md`：`browser_state: waiting_human` 字段，goal driver 在续跑前检查
- `commands-and-mentions-design.md`：`/browse` 作为新增 user-invocable skill
- `citations-design.md`：`browser.extract` 的 snapshot 可作为 wiki 来源（M3）

---

## 附录 A：ego-lite 链接

- [citrolabs/ego-lite](https://github.com/citrolabs/ego-lite)
- [Space](https://lite.ego.app/document/en/docs/space)
- [ego-browser](https://lite.ego.app/document/en/docs/ego-browser)
- [Skills](https://lite.ego.app/document/en/docs/skills)
- [SKILL.md](https://github.com/citrolabs/ego-lite/blob/main/skills/ego-browser/SKILL.md)
- [PR #41 — agentDelegatedToUser](https://github.com/citrolabs/ego-lite/pull/41)
- [PR #29 — complete 单独 heredoc](https://github.com/citrolabs/ego-lite/pull/29)
- [Issue #88 — 孤儿 renderer](https://github.com/citrolabs/ego-lite/issues/88)
- [atrium — From WKWebView to CEF](https://getatrium.dev/blog/embedding-real-browser-tauri)
- [OpenHuman](https://github.com/tinyhumansai/openhuman)
