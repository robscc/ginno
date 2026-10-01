# Browser Companion Extension Design(伴随式浏览器扩展:第三条路)

> 状态:**已实施**(2026-10-02,实现说明见文末 §9)。目标:不内嵌浏览器引擎,而是通过一个
> companion Chrome 扩展 + localhost WS relay,让 Ginno 操控**用户真实浏览器**——
> 登录态、扩展、使用习惯全部保留,浏览器引擎零维护成本。
>
> 参考对象(逆向分析,2026-10-01):
> - QwenWork(千问办公)Chrome 扩展 — `~/Library/Application Support/QwenWork/chrome-extension/`
>   (manifest v3 + background.js ~135KB 压缩 + 3 个 content script)。操作流畅度
>   是目前同类产品里做得最好的,本文大量工程决策直接对齐它。
> - 结论存档:auto-memory `qwenwork-browser-extension-analysis.md`。
>
> 与既有设计的关系:
> - 取代 `browser-embed-design.md`(CEF 内嵌路线)——该路线 2026-08-29 随内嵌浏览器
>   整体移除而退役,`packages/runtime/src/ginno_runtime/browser/` 只剩 `__pycache__`。
>   ego-lite 的产品契约(Space / handoff / 所有权)仍然有效,但引擎底座从「内嵌 CEF」
>   换成「用户自己的 Chrome」。
> - 与 dsh 端点策略互补(`dsh-plugin-ecosystem-integration`):relay 协议按 MCP 形状
>   设计,以后既能服务 Ginno 自己,也能作为 MCP server 暴露给第三方 agent。

---

## 0. TL;DR

- **形态**:Ginno sidecar(FastAPI)新增 `/extension` WS 端点作为 relay;配套一个
  Ginno Chrome 扩展(`chrome.debugger` CDP + content scripts)控制用户真实浏览器。
  端口发现走 Tauri 注册的 native messaging host(M1 可先手动输端口)。
- **协议**:MCP 风格 JSON-RPC(`tools/discover` / `tools/invoke` / `tools/progress`),
  capabilities 数组协商,FIFO 命令队列。照抄 QwenWork 的形状,字段名可兼容。
- **工具面**:16 个工具,`browser_` 前缀,坐标轨(`browser_computer`)+ 语义轨
  (`browser_read_page` / `browser_find` / `browser_form_input`)双轨互备
  (§4 有完整 schema)。
- **权限**:全部工具过 `permission/policy.py`;`browser_js`(任意代码执行)、
  `browser_file_upload`(路径必须在会话挂载目录内,对齐 mounts 体系)、高危域名
  (支付/邮箱)强制 ask。
- **M1**:relay 端点 + 扩展骨架 + `browser_computer`/`browser_read_page`/截图跑通。

---

## 1. 为什么是这条路

| 维度 | 内嵌 CEF(旧路线) | Playwright/无头 | companion 扩展(本设计) |
|---|---|---|---|
| 浏览器维护 | 全量(C EF build、签名、白屏…) | 驱动版本对齐 | **零**(用户自己的 Chrome) |
| 用户登录态 | 需导入/共享 profile | 无 | **天然共享**(用户真实 profile) |
| 事件可信度 | CDP trusted | trusted | trusted(`chrome.debugger`) |
| 存在感 | 常驻窗口 | 不可见 | **按需 attach、idle 即隐** |
| 用户可接管 | 需 handoff 机制 | 不适用 | 天然(就是用户的浏览器) |
| 分发成本 | 打进 app | 打进 sidecar | 装一次扩展(引导流程) |

代价:需要用户装扩展 + Chrome 才能工作;多浏览器(Quark/Edge)靠 UA 探测兼容,
QwenWork 已验证可行(其 background 里有 quark/edge/chrome 三分支)。

## 2. 总体架构

```
Ginno 桌面(Tauri)
 ├─ sidecar(FastAPI,packages/runtime)
 │    └─ /extension WS relay 端点(新增,127.0.0.1 随机端口)
 ├─ native messaging host(新增,Rust 侧注册 manifest "app.ginno.connector")
 │    └─ 职责仅一个:把 relay 端口号告诉扩展(替代手动输入)
 ▼
Ginno Chrome 扩展(manifest v3)
 ├─ background.js(service worker)
 │    ├─ 主通道:ws://127.0.0.1:<port>/extension
 │    ├─ 备用通道:native messaging 直接当 transport(base64 分块,突破消息大小限制)
 │    ├─ 执行层:每工具调用按需 chrome.debugger.attach → CDP 命令 → idle 自动 detach
 │    └─ FIFO 命令队列 + tools/progress 进度流
 └─ content scripts ×3
      ├─ accessibility-tree.js — ref_N 元素引用系统
      ├─ page-bridge.js       — 语义填表/点击/正文抽取
      └─ visual-indicator.js  — Shadow DOM 指示器(脉冲边框/高亮/Stop 按钮)
```

**QwenWork 已验证的传输层细节,直接沿用:**

- **端口发现**:扩展起来后先 `connectNative("…connector")`,host 进程(sidecar 或
  Tauri 拉起的小工具)回 `{port, pid, name}`;WS 连不上时 native messaging 整体降级
  为备用 transport(同样 JSON-RPC 帧,base64 分块,单块 ≤64KB)。
- **重连**:指数退避 1s→10s 封顶,连续失败进 30s 冷却;ping/pong keepalive。
- **service worker 复活**:tab 状态写 `chrome.storage.session`,被杀后 rehydrate,
  并用 `Runtime.evaluate("1")` 逐 tab 探活清僵尸。
- **multi-relay**:relayClient 表支持多个 Ginno 实例(或未来多个 Ginno 系产品)
  同时连一个扩展,命令按 clientId 回路由。M1 可不做,协议字段保留。

## 3. Relay 协议

JSON-RPC 形状(MCP 风格,不分帧、每 WS 消息一个 JSON):

```jsonc
// 扩展 → Ginno,连接后即报
{"method": "extensionInfo", "params": {
  "version": "1.0.0", "browserType": "chrome",
  "capabilities": ["mcp-tools", "fifo-command-queue", "tool-progress"],
  "browserClientId": "<uuid,chrome.storage 持久化>"}}

// Ginno → 扩展
{"id": 1, "method": "tools/discover"}
{"id": 2, "method": "tools/invoke", "params": {"tool": "browser_computer",
  "arguments": {"action": "left_click", "tabId": 5, "ref": "ref_12"}}}

// 扩展 → Ginno
{"id": 1, "result": {"tools": [/* §4 全部 schema */]}}
{"id": 2, "result": {"content": [{"type": "text", "text": "Clicked at (320, 411)"},
  {"type": "image", "data": "<b64>", "mimeType": "image/jpeg"}]}}
{"id": 3, "error": {"code": "QUEUE_TIMEOUT", "message": "..."}}

// 扩展 → Ginno,仅慢工具(screenshot/file_upload),能力协商后才发
{"method": "tools/progress", "params": {"requestId": 2, "tool": "browser_computer",
  "stage": "capture_attempt_1", "elapsedMs": 812, "queueLength": 0}}
```

要点(全部来自 QwenWork 实测有效的决策):

- **capabilities 数组协商**而非版本号判断。每个新能力(fifo 队列、progress、
  multi-relay)是独立开关,老扩展连上新 sidecar 不炸。Ginno 自己的 WS 协议
  (`api/stream.py`)以后加能力也应采用这个模式。
- **FIFO 队列**:扩展端单线程顺序执行工具调用;队列满 → `QUEUE_FULL`,排队超时 →
  `QUEUE_TIMEOUT`(screenshot 类有独立更长超时)。防止 sidecar 并发轰炸。
- **错误码全大写机器码**:`QUEUE_FULL` / `CLIENT_NOT_CONNECTED` / `EXTENSION_RESTRICTED`,
  message 面向模型,附带下一步动作(见 §5「教学式错误」)。

## 4. Ginno 工具面(16 个)

命名统一 `browser_` 前缀(与 `web_search`/`web_fetch` 区分;Ginno 工具名是平铺的,
不引点号)。schema 的 **description 内嵌使用指引**(不靠 system prompt 堆规则)——
这是 QwenWork 工具面最值得抄的一点,下面的描述文本已按 Ginno 语境改写。

### 4.1 视觉轨(坐标)

#### `browser_computer`
```jsonc
{"action": {
  "enum": ["left_click","right_click","double_click","triple_click","type",
           "key","screenshot","zoom","scroll","scroll_to","hover",
           "left_click_drag","wait"],
  // 描述同 QwenWork computer 工具,逐 action 一行说明;
  // "zoom": 对 region 截高清图, inspect 小 UI 元素用
},
"coordinate":   "[x, y] 视口像素坐标。left_click/double_click/triple_click/right_click/scroll/hover 必填;left_click_drag 里是终点",
"start_coordinate": "[x, y] left_click_drag 起点",
"region":       "[x0,y0,x1,y1] zoom 用",
"ref":          "read_page/find 返回的 ref_N。scroll_to 必填;点击类可与 coordinate 二选一(ref 自动滚动到位并取元素中心)",
"text":         "type 的文本 / key 的键名(空格分隔多键,如 \"Backspace Backspace\";快捷键 cmd+a / ctrl+a)",
"scroll_direction": "up|down|left|right",
"scroll_amount": "1-10 滚轮格数,默认 3",
"duration":     "wait 秒数 0-10,默认 1",
"repeat":       "key 重复次数 1-100(如按 5 次 ArrowDown)",
"modifiers":    "ctrl|shift|alt|cmd 可 + 组合",
"quality":      "low|medium|high 截图质量,默认 low。low 足够导航和读 UI;medium 读小字;high 只在细节关键时用(更大更慢)",
"tabId":        "必填。没有合法 tabId 就先调 browser_tabs_context"}
```

### 4.2 语义轨(ref)

#### `browser_read_page`
```jsonc
{"filter": "interactive|all(默认 all)。interactive 只返回可交互元素,任意深度;all 受 depth 限制",
 "depth":  "1-50,默认 15,仅 filter=all",
 "max_chars": "1000-200000,默认 50000",
 "ref_id": "只读某个 ref 子树 —— 输出超限时错误信息会引导你带 ref_id 重读",
 "tabId":  "必填"}
// 返回:缩进文本树,每行 `[ref_N] role "label" (disabled, checked, …)`
// 截断时末尾附 [TRUNCATED] 与「用 ref_id 聚焦子树」提示
```

#### `browser_find`
```jsonc
{"query": "空格分词、字面匹配元素文本/aria-label/title/role(打分:label 3 分/role 2 分/文本 1 分)。
          不懂同义词——页面上写「立即购买」时搜「购买按钮」不会命中。用页面上实际出现的词",
 "max_results": "1-100,默认 20", "tabId": "必填"}
```

#### `browser_form_input`
```jsonc
{"ref": "read_page/find 的 ref_N", "value": "string|number|boolean。checkbox 用 boolean,select 用 option 值或文本", "tabId": "必填"}
// 实现:原生 value setter + InputEvent(change)(React 受控组件兼容),
// contentEditable 用 execCommand;文件上传除外(见 browser_file_upload)
```

### 4.3 导航 / 标签

#### `browser_navigate`
```jsonc
{"url": "http(s) URL 或裸域名(自动补 https://),或 \"back\"/\"forward\"",
 "force": "页面有 beforeunload(未保存更改)时是否强行离开。默认 false:导航被拦,
          返回错误让你(和用户)先决定", "tabId": "必填"}
// 返回含最终 URL(标注 redirect)、标题、耗时
```

#### `browser_tabs_context` / `browser_tabs_create` / `browser_tabs_close`
```jsonc
// context: 列出 Ginno tab group 内全部标签(id/url/title/loading);
//   描述里明确写:已有标签可能被其他会话占用,新会话优先 tabs_create 自己的
// create: 组内开新空标签;close: {tabId} 关组内标签
```

### 4.4 读取 / 调试

#### `browser_page_text`
```jsonc
{"max_chars": "默认 50000", "tabId": "必填"}
// 正文优先:article/main/[class*=content] 等候选里取文本最长者,压缩空白后截断
```

#### `browser_js`(门控:默认 ask)
```jsonc
{"text": "在页面上下文求值的 JS。不要写 return,写表达式;
         语句/async 函数体自动兼容。任意代码执行 → 权限策略强制确认", "tabId": "必填"}
```

#### `browser_console` / `browser_network`
```jsonc
// console: {tabId, pattern(正则,描述里要求必须给), onlyErrors?, clear?, limit?}
// network: {tabId, urlPattern?, clear?, limit?}
// 实现:CDP 事件被动缓冲进 ring buffer(console 1000 / network 500 条),
// attach 期间一直收,读时才取;跨域导航自动清空
```

### 4.5 文件 / 窗口

#### `browser_file_upload`(门控:路径必须在会话 mounts 内)
```jsonc
{"paths": "绝对路径数组,必须在会话挂载目录内(与 read_file/write_file 同一套 mounts 校验)",
 "ref": "input[type=file] 的 ref(输入框已暴露时)",
 "triggerRef": "只露出「上传」按钮时传按钮的 ref:扩展拦截文件选择器
               (Page.setInterceptFileChooserDialog)直接设文件,不弹系统对话框;
               按钮先弹菜单的场景自动找「上传文件」菜单项",
 "tabId": "必填"}
// 成功后验证 input.files(数量/文件名/非空)并返回诊断:
// 页面事件、文本匹配、file inputs、最近网络请求 —— 让模型能判断业务侧是否真消费了文件
```

#### `browser_resize_window`
```jsonc
{"width": "400-7680", "height": "300-4320", "tabId": "必填"}
```

### 4.6 工具落地形态

LangChain `@tool`,挂在 `tools/browser_tools.py`,由 `build_all_tools` 在
`browser.enabled` 时注入(对齐 `web_tools.py` 的 cfg.enabled 模式):

```python
@tool
def browser_computer(action: str, tabId: int, coordinate: Optional[list[int]] = None,
                     ref: Optional[str] = None, text: Optional[str] = None, ...) -> str:
    """Use a mouse and keyboard to interact with a web browser tab…(§4.1 的
    description 全文,内嵌使用指引)。Returns text, or text+image for
    screenshot/zoom — images stream back through the turn's content blocks."""
```

builtin 契约同其他工具:**never raise**,失败降级为 `[error] …` 文本;截图图片走
现有的 turn 内容块图片通道(inline-images-design.md 的机制)。

## 5. 工程细节清单(QwenWork 验证过,按性价比排序)

1. **jpeg 质量阶梯 + 截图分 stage 超时/恢复 + progress 流**。
   quality 5/40/60;`fromSurface:true` 失败 → detach+reattach → `fromSurface:false`;
   attach/capture/recovery 各自超时预算;全程 `tools/progress` 推 stage。慢操作
   永远有反馈,这是「体感流畅」的一半。
2. **坐标映射**:screenshotContext 记 viewport↔screenshot 尺寸,模型给的坐标一律
   换算回视口(SPA 内导航 `Page.navigatedWithinDocument` 时失效即删)。
3. **ref 系统**:Map<ref, WeakRef> + WeakMap<元素, ref> 双向,防泄漏;ref 失效的
   错误必须教恢复路径("Use browser_read_page or browser_find to get a fresh ref")。
4. **idle detach**:每次工具调用重置 idle timer(比如 10s),空闲即 detach 全部
   debugger 并隐指示器——Chrome「正在被调试」横幅只在干活瞬间存在。
5. **beforeunload 策略**:默认 dismiss(不丢用户数据),`force:true` 才 accept,
   结果回传模型决策;alert/confirm 对话框自动处理(`Page.handleJavaScriptDialog`)。
6. **可中断打字**:type 循环逐字符检查 stop 标志;页内 Stop 按钮发
   `STOP_TOOL_EXECUTION`,sidecar 把它接进 turn 的中断流。
7. **教学式错误**:所有错误信息带「下一步动作」;输出截断时附「用 ref_id 聚焦」提示。
8. **CDP→JS 双向 fallback**:CDP 滚动失败降级 JS;JS 执行 scripting API → CDP
   表达式 → async 包裹三级;`tabs.goBack` 失败降级 `history.back()`。
9. **导航等待**:`tabs.update` 后 100ms 轮询 `tab.status === "complete"`
   (导航 10s / back-forward 5s);back/forward 先等 `loading` 出现再等 complete。
10. **tab group 归置**:agent 标签自动进「Ginno」组,组标题 emoji 表状态(⏳/✅)。
11. **反面参考(要避的坑)**:QwenWork 的 visual-indicator 截图前隐藏函数当前是
    **空函数**——截图内容与页面 overlay 的时序竞争没解决,索性砍掉。Ginno 若做
    指示器,应在截图管线内原生处理(截图帧内临时隐藏,而非独立 hide/restore 对)。

## 6. 安全与权限(对齐 Ginno 既有体系)

- **全部浏览器工具过 `permission/policy.py`**。默认档:读类(read_page/find/
  page_text/screenshot/console/network)`allow`;动作类(click/type/navigate/
  form_input)`allow`(浏览器操作本身可见、可 Stop);`browser_js` 与
  `browser_file_upload` **强制 ask**(任意代码执行 / 文件出站)。
- **高危域名单**:支付、邮箱、云控制台等域上的动作类工具强制 ask,名单进 settings。
- **file_upload 路径校验**:复用 `builtin.py` 的 mounts/`_path_denied` 体系,
  路径不在会话挂载目录内直接拒——浏览器不能变成绕过文件权限的旁门。
- **URL 黑名单**:`chrome://` / `chrome-extension://` / `devtools://` 拒绝导航与
  attach(QwenWork 同款限制)。
- **登录态即用户本人**:浏览器里就是用户的真实身份,产品上必须默认可见(指示器
  + tab group + Stop 按钮),高风险动作过权限门——同 ego-lite 契约的「Agent 即
  用户、默认可见、可接管」。

## 7. 双轨与里程碑

### 7.1 扩展是否必须:已裁定(2026-10-01)

**只要产品目标是「操控用户真实浏览器」,就必须装独立扩展,且无法静默安装。**
Chrome 2025 年两轮安全收紧封死了全部旁路:

| 旁路 | 状态 |
|---|---|
| `--remote-debugging-port` 直连用户默认 profile | Chrome 136 起静默失效(必须配非默认 `--user-data-dir`,[官方公告](https://developer.chrome.com/blog/remote-debugging-port)) |
| 命令行 `--load-extension` 静默装扩展 | Chrome 137 起从正式版移除,只剩手动 Load unpacked([Chromium 公告](https://groups.google.com/a/chromium.org/g/chromium-extensions/c/aEHdhDZ-V0E)) |
| Web Store 分发 | 可行但 `debugger` 权限审核严、更新受商店节奏控制 |
| 企业策略静默安装 | 仅托管设备适用 |

`chrome.debugger` 扩展 API 是唯一在用户真实 profile 上工作且受官方支持的通道——
这不是 QwenWork 的选型偏好,是没有选择(其 manifest 带 `key` 字段、上报
`installType: "development"`,走的开发者模式手动加载)。

**因此双轨**:
- **产品主线 = 扩展轨**(用户真实浏览器、登录态共享、可接管)。安装引导是
  P0 产品流程:首启检测未装 → 深链 `chrome://extensions` 分步引导 → 连接状态
  常驻可视化 → 断连自愈。对齐 QwenWork popup 的连接/错误文案体系。
- **B 轨(免安装降级模式)= 专用 profile 实例**:sidecar 以
  `--user-data-dir=~/.ginno/browser-profile --remote-debugging-port=<随机>` 拉起
  真 Chrome,直连 CDP。零安装零扩展,但登录态隔离(每站点登一次或做 cookie
  导入),「接管」价值弱化——是「Ginno 的浏览器」而非「我的浏览器」。
  与已移除的内嵌 CEF 不同:引擎是用户自装的 Chrome,无打包/签名/白屏维护成本。

**工程含义:执行器抽象。** sidecar 侧把执行器抽成 `BrowserExecutor` 接口
(扩展 WS relay / CDP 直连两个实现),工具面、ref 系统、截图管线、权限、
progress 流全部与执行器无关。M1 用 CDP 执行器打通全部资产(零分发摩擦,
内部即刻 dogfood),M2 换扩展执行器时工具面一行不改。

### 7.2 里程碑

- **M1(跑通闭环,B 轨)**:sidecar 浏览器模块 + CDP `BrowserExecutor` +
  `browser_computer` / `browser_read_page` / `browser_tabs_*` 5 个工具
  (专用 profile 实例,无扩展依赖);截图管线(质量阶梯/坐标映射)+ tab group。
- **M2(扩展轨上线)**:Ginno 扩展骨架(WS relay、native messaging 端口发现、
  attach/idle-detach、视觉指示器)+ 扩展执行器接入;§4 全部 16 工具、权限集成、
  fifo 队列 + progress、console/network ring buffer、file_upload 双模式;
  安装引导流程(P0);popup「发送此页面」反向入口。
- **M3(生态)**:浏览器 handoff 卡(接管,对齐旧设计里
  `interrupt({kind:"browser_handoff"})` 的图协议);workflow `browser` 节点直接
  复用同一工具面;relay 端点按 MCP 形状对外暴露(占端点策略,让 dsh 类 harness
  也能用 Ginno 的浏览器通道)。

### 7.3 用户引导与安装流程(扩展轨 P0)

> 宿主:本节的向导/状态卡/排查文案由 **Connector 模块**承载
> (`connector-module-design.md`,Chrome 扩展是第一个连接器,侧栏左下角
> Knowledge Base 上方);此处保留内容本体,实现挂到该模块。

静默安装不可能(§7.1),因此引导流程本身是产品能力,不是设置页的一行小字。

#### 包的分发:物化到用户目录(对齐 QwenWork 实证模式)

扩展源文件打包在 `Ginno.app/Contents/Resources/browser-extension/`,Ginno 首启
(以及每次版本比对不一致)时复制到 **`~/.ginno/browser-extension/`**:

- 用户可写、路径稳定,避开 app 包只读路径与签名校验问题
- **manifest 内置 `key` 字段固定扩展 ID**(QwenWork 同款)——unpacked 扩展的 ID
  由 key 派生;ID 不固定,后面 native messaging 的 `allowed_origins` 就对不上,
  这是 M3 端口发现的前置条件,**M2 打包时就必须生成并写死 key**
- 更新流程:Ginno 比对版本 → 写入新文件 → Chrome 对 unpacked 目录有文件监听,
  自动热重载 → 扩展按既有重连逻辑回连 relay,无需用户再进 chrome://extensions。
  物化目录里放一份 `VERSION.json`(版本、构建时间),前端「浏览器连接」卡片显示
  当前扩展版本,便于排障时对齐

#### 首启引导向导(5 步,前端实现)

触发条件:浏览器功能开启 且 relay 未连接 且 未完成过引导(状态存 settings)。
侧栏「浏览器」入口带红点引导;向导可随时从设置重新进入。

每步一个卡片,**文案即产品**(macOS / Chrome 正式版):

> **Step 0 · 这是什么**
> 「Ginno 可以在你自己的 Chrome 里替你干活——用你的登录态,像你一样点按输入。
> 你随时能看到它在动,也可以随时叫停。需要装一个 Ginno 的浏览器扩展(一次性,
> 约 1 分钟)。」
> [继续] [以后再说]
>
> **Step 1 · 扩展已就位**
> 「Ginno 已将扩展文件放到你电脑上的 `~/.ginno/browser-extension` 文件夹。」
> [📝 在 Finder 中显示](保持此文件夹打开,第 4 步要用)
>
> **Step 2 · 打开 Chrome 扩展页**
> 「在 Chrome 地址栏输入下面的地址并回车(浏览器不允许网页直接打开这个页面,
> 需要你手动输入):」
> `chrome://extensions` [📋 复制地址] [打开 Chrome]
>
> **Step 3 · 开启开发者模式**
> 「扩展页**右上角**有「开发者模式」开关,打开它。」(配截图高亮红框)
>
> **Step 4 · 加载扩展**
> 「点击左上角「加载已解压的扩展程序」,在弹出的文件夹选择器里选中刚才打开的
> **browser-extension 文件夹**(注意:选中文件夹本身,不是进去选里面的文件),
> 点「选择」。列表里出现「Ginno」即成功。」(配截图)
>
> **Step 5 · 连接确认(自动)**
> 「正在等待扩展连回 Ginno…」——relay 连上(收到 `extensionInfo`)即自动打勾:
> 「✅ 已连接:Ginno 扩展 v1.0.0 · Chrome」[开始使用]
> 超时 30s 未连上 → 展开下方「没连上?」排查清单,并提供 B 轨降级按钮
> 「先用 Ginno 自带的浏览器实例(无需扩展)」。

#### 连接状态常驻与自愈

- 设置页 + 侧栏浏览器入口常驻连接徽标:已连接(版本)/ 未安装 / 已禁用 / 重连中
- 断连(Chrome 重启、扩展被禁、Chrome 升级偶发禁用开发者模式扩展)→ 徽标变黄,
  点开即向导的 Step 2-4 精简版(「打开 chrome://extensions,确认 Ginno 扩展已启用」)
- relay 侧永远先探测再提示:状态判定顺序 = WS 已连 > native messaging 已连 >
  都没有(才显示「未安装/未启用」),避免把网络抖动误报成安装问题

#### 排查文案表(每个失败态一句人话 + 一个动作)

| 状态 | 文案 | 动作 |
|---|---|---|
| 未检测到扩展 | 「Chrome 里还没有 Ginno 扩展」 | 重进向导 / 复制 chrome://extensions |
| 扩展已禁用 | 「Ginno 扩展被禁用了(Chrome 更新后偶尔会这样)」 | 深链说明 + 「打开 chrome://extensions」 |
| 连接失败但已装 | 「扩展在,但没连上 Ginno」 | 「重启 Ginno」/ 自动重试中(显示次数) |
| 加载了错误目录 | 「检测到扩展版本为 v?(期望 v1.0.0)。请删除后重新加载 ~/.ginno/browser-extension」 | 复制路径 |
| Chrome 不在运行 | 「先打开 Chrome,再回来点重试」 | 重试 |

#### native messaging 端口发现(M3,静默部分)

- manifest 写入 `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/`,
  由 Tauri(Rust)完成——**这一步 Chrome 允许应用静默写**,不涉及扩展安装,
  前置条件即上述固定扩展 ID(`allowed_origins: chrome-extension://<固定ID>/`)
- 装好后用户连「端口」概念都不需要知道;M2 之前的过渡:扩展 popup 里显示
  「 Ginno 已连接(端口 54321)」,向导 Step 5 的连接即靠 WS 扫描 + native 发现

## 8. 待裁定

1. ~~扩展分发~~ **已裁定为双轨**(§7.1):扩展走开发者模式手动加载 + 首启引导
   (对齐 QwenWork);Web Store 作为以后规模化时的可选通道再评估。原残留子项
   (未装扩展时 B 轨默认开启还是显式开启)已收编为连接器配置项
   `fallback_profile_mode`(见 connector-module-design.md §3,建议默认 `ask`)。
2. 多浏览器范围:M1 只 Chrome;Quark/Edge 的 UA 探测分支是否跟进看用户分布。
3. `browser_js` 与既有 `browser_eval` 概念(旧设计)的关系:合并还是保留两个名字。
4. 指示器视觉:是否复用 Ginno 品牌色做脉冲边框/Stop 按钮(QwenWork 用的绿色系)。

---

## 9. 实现说明(2026-10-02)

全部功能已落地,主要模块:

| 设计项 | 实现位置 |
|---|---|
| B 轨 CDP 执行器 | `packages/runtime/src/ginno_runtime/browser/cdp.py` + `executor.py`(Chrome 发现/启动/attach/ring buffers/对话框处理) |
| 双轨注入脚本(单一来源) | `browser/scripts.py`(`__ginnoAT`/`__ginnoBridge`;扩展构建时生成 content scripts) |
| relay 端点 + FIFO + progress | `browser/relay.py`(`ws://127.0.0.1:<port>/extension`,挂在 api/connectors.py) |
| 16 个 browser_* 工具 | `tools/browser_tools.py`(`_dispatch` 网关:扩展轨优先,按 fallback_profile_mode 降 B 轨) |
| Chrome 扩展本体 | `packages/extension/`(manifest 带固定 key,ID `jlmheiiglpdikeihgjefjhmoakfllhkm`;background.js 全 16 工具 + idle detach + tab group + 视觉指示) |
| 扩展物化 + native host | `browser/native_host.py`(物化到 `~/.ginno/browser-extension/`;host 清单写 Chrome NativeMessagingHosts,端口经 `~/.ginno/browser-relay-port` 发现) |
| 权限 | `permission/policy.py::ensure_browser_permissions`(读/动作类 allow;`browser_js`/`browser_file_upload` 走默认 ask);上传路径过 mounts 校验;受保护域名经 connector 配置 `confirmed_domains` 放行 |
| workflow `browser` 节点 | `workflows/nodes/builtin.py::BrowserNode`(AgentNode 子类,浏览器风味 goal 前缀,工具面复用 build_all_tools) |
| handoff | `browser_handoff` 工具阻塞等待 + `GET /api/connectors/browser/handoff` + Connectors 页「已接管,继续」横幅 |
| MCP 暴露(占端点) | `POST /mcp`(initialize/tools/list/tools/call,走同一 `_dispatch`) |
| 截图回传 | ToolMessage content `[text, image_url]` 块;`graph.strip_old_images` 已修(非 Human 消息保留原类) |

验证:B 轨 headless Chrome E2E(建页/read_page/find/填表/截图/键入/console)✓;
relay 协议闭环(模拟扩展连入→extensionInfo→工具路由)✓;MCP 端点(16 工具 +
真实调用)✓;runtime 测试全过(1743 + 20 新增单测);web `tsc` + `next build` 绿。

### 9.1 补齐清单(2026-10-02 第二轮)

初版实现说明中简化/未落的项已全部补齐:连接器 WS 事件通道
(`/api/ws/connectors`,轮询降级为兜底)、tools/progress 前端呈现(连接器页
进度条)、popup「发送此页面」反向入口(openInChat → page_pushed → 连接器页
卡片 → ginno:prefill-input 预填聊天)、聊天流 handoff 卡(ToolBlock 特判 +
release-all 语义)、受保护页面的动作类拦截(computer/form_input/js/
file_upload 先查 tab URL,confirmed_domains 在连接器配置可编辑)、B 轨 ask
模式的「提示一次」toast、file_upload 两跳菜单场景(上传菜单项候选竞速)、
native messaging 备用传输(relayData/分块双向桥,dev 用 venv python shebang,
frozen 用 .sh wrapper 调 `ginno-runtime --native-host`)、向导步数 localStorage
重入、SPA 软导航清 screenshotCtx。multi-relaw 多实例共存按设计 §3 的
「协议字段保留」状态维持单连接。

已知边界(与设计一致):`--load-extension` 在 Chrome 137+ 正式版已移除,扩展必须
开发者模式手动加载(§7.3 向导);`browser_handoff` 在扩展轨由 sidecar 工具层承载
(扩展侧不等待);敏感域在 navigate 处拦截,computer 类按 URL 命中受保护域名时需
用户在连接器页确认后重试。
