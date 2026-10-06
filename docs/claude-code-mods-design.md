# Ginno × Claude Code Mods 兼容层设计方案

- 日期:2026-10-06
- 状态:设计稿 v2(架构定案:Rust broker 分布式编排,待评审)
- 前置调研:`~/.ginno/projects/default/sessions/.../uploads/2026-10-06-Claude Code Mods 规范与 Ginno 适配方案.md`(下称「调研笔记」)
- 规范基线:Claude Code 2.1.289 Mods reference
- 相关既有文档:`connector-module-design.md`、`external-agents-design.md`、`subagent-design.md`、`docs/design/right-panel-redesign.md`、`p3-packaging-notes.md`

---

## 0. TL;DR

**架构:Rust 中心化 mod 总线(分布式编排)。** 新增 Rust crate **`ginno-mod-broker`**:桌面端编译进 Tauri app,dev/web 模式由 Python spawn 同一 crate 的 CLI 二进制——它是星型拓扑的中心,负责链编排(`next()` 跨进程回环、预算记账、matcher)、`$` op 的本地实现与安全管控(grants 在 IPC 边界强制)、SurfaceTable(band 渲染)、以及 **每 mod 一个 Node runner 进程**的挂载/监督。Python runtime 通过 Unix socket 连接 broker,职责收窄为两件事:**引擎事件源**(`tool.call`/`turn.*` 等挂载点)与 **runtime-backed `$ op 后端**(`$.session`/`$.fs` 工作区… 配置经 Python 推送,broker 仍不碰 settings.json)。前端 band/toast/ask 仍走 Python WS 单通道。

链语义(next 单次状态机、budget 暂停记账、catch 共享下方状态等)按 DSH 源码核实的规格(§15)在 **Rust 中实现**,DSH(MIT)的 JS 侧(`$` shim、元素构造器、createOn 校验、单测)移植进 runner。分三阶段,验收用官方示例 **Token Weather(P0)→ Blast Radius(P1)→ Replay Theater(P2)**。

设计演化:纯「Rust 转发 RPC」被否(转发无收益、dev 断);「单 mod-host」被分布式编排取代,但其帧协议与 runner 侧代码被本方案继承。

---

## 1. 目标与非目标

### 目标

1. **直接运行生态里已有的 Claude Code Mods**:用户把一个 mod 目录放进 `~/.ginno/mods/`,Ginno 能加载、触发其 hooks、渲染其 UI。
2. **兼容度可度量**:注册了但 Ginno 不触发的事件、调用了但未实现的 `$` 方法,都要显式暴露(warning / reject / validate 报告),不静默失败。
3. **per-mod 进程隔离 + 中心化安全管控**:一个 mod 崩溃不连坐;每个 `$` 调用过 broker,grants 白名单在 IPC 边界强制执行(拒绝发生在能力到达 mod 之前)。
4. **mod 生命周期独立于 Python runtime**:broker 与 runner 不随 runtime 重启而重载(dev 重建/崩溃后 band 与状态不丢)。
5. **复用 Ginno 既有资产**:事件挂载点对齐 LangGraph 图的真实插桩位置;UI 复用 composer band / AskUserCard / widget 卡模式;进程发现复用 `_resolve_cli` 模式;DSH(MIT)源码移植。

### 非目标(明确不做)

- 不做 OS 级沙箱(quickjs/isolate 嵌入):进程隔离 + broker 能力管控已远优于 DSH 的同进程,真沙箱留待后续。
- 不适配官方 `diff` mod(依赖 `settings.read` + `telemetry` + pane + `ui.focus/scroll`,DSH 已放弃)。
- 不做 marketplace / mod 商店分发(P2 之后再说)。
- 不做 tier 完整语义(`prepend/append/builtin/core`):全部按 `user` tier 处理,`next.to` 拒绝(与 DSH 同,后续按需放开)。
- 不做 `$.audio`、`$.telemetry`、`$.settings`(Ginno 无对应策略层,做了就是假兼容)。

---

## 2. 总体架构

```
┌─────────────────────────── apps/web (Next.js, 桌面=Tauri webview) ──────────────────────────┐
│  ChatStream composer band 插槽   ModToastHost   ModAskCard   Settings「Mods」tab   右栏(P2) │
└───────────────▲─────────────────────────────────────────────────────────────────────────────┘
                │ WS: mod.bands / mod.toast / mod.ask / mod.answer / mod.ui.press(单一前端通道)
┌───────────────┴─────────────────────────────────────────────────────────────────────────────┐
│  Python runtime(职责收窄:事件源 + op 后端 + 配置所有者)                                      │
│   ├─ ModChannel(新, ginno_runtime/mods/channel.py):连 broker socket,收发帧                  │
│   ├─ 事件抽头: session/turn/prompt/tool 挂载点(§5.3)                                         │
│   ├─ runtime-backed op: session.* / prompt.submit / tool.* / command.* / mcp.call(§5.4)      │
│   └─ 配置推送: 启动连接后把 mods 配置/grants/node 路径推给 broker(settings.json 所有权不动)    │
└───────────────▲─────────────────────────────────────────────────────────────────────────────┘
                │ Unix domain socket + token(换行 JSON 帧)
┌───────────────┴─────────────────────────────────────────────────────────────────────────────┐
│  ginno-mod-broker(Rust crate;桌面=编译进 Tauri,dev/web=独立二进制由 Python spawn)           │
│   ├─ 链编排器: dispatch / next() 回环(单次语义) / BudgetClock / .catch / matcher(§3.2)     │
│   ├─ op 表: broker 本地实现 fs/http/process/state/store/clock/ui.*(安全管控在此)(§3.3)      │
│   ├─ SurfaceTable: generation/actionId/press/订阅重绘(§3.4)                                  │
│   ├─ runner 监督: 每 mod spawn/重启(退避)/资源上限/node 发现(§3.5)                           │
│   └─ grants 强制: 每个 $ 调用过此边界,白名单拒在能力到达 mod 之前(§3.6)                       │
└──────┬──────────────────┬──────────────────┬────────────────────────────────────────────────┘
       │ stdio 换行 JSON  │                  │        (每 mod 一个进程,崩溃隔离)
┌──────▼──────┐   ┌──────▼──────┐   ┌──────▼──────┐
│ runner(mod A)│   │ runner(mod B)│   │ runner(mod C)│   Node ≥22.18,thin hooks runner
│ 装载清单+ESM  │   │             │   │             │   $ shim/元素构造器(移植自 DSH)
└─────────────┘   └─────────────┘   └─────────────┘
```

**关键架构决策与理由:**

| 决策 | 选择 | 理由 |
|---|---|---|
| 拓扑 | **Rust broker 星型中心,链编排者而非转发器** | 「模块间交互全过 Rust」是星型拓扑的自然结果;grants 在 IPC 边界强制;per-mod 崩溃隔离 |
| broker 宿主形态 | **一个 Rust crate 两种宿主**:桌面编译进 Tauri;dev/web 由 Python spawn 同 crate 的 CLI 二进制 | 一套逻辑两种环境,`pnpm dev`(无 Tauri)与 web 模式不断 |
| 数据面传输 | **Unix domain socket + token**(Python↔broker)、**stdio**(broker↔runner) | 走 Rust 核心/socket,绝不经 webview 的 Tauri event IPC(UI 专用,做数据面又慢又绑死 web 模式) |
| mod 进程模型 | **每 mod 一个 Node runner** | 崩溃隔离 + 独立内存上限;~30-50MB/个,接受 |
| 配置所有权 | **settings.json 仍归 Python**,连接后推送给 broker | 不拆两个事实来源;broker 无盘上配置,重启即收新配置 |
| 前端通道 | **bands/toast/ask 全部经 Python WS 转发**,broker 不直连前端 | 单一前端通道,web 模式可用;桌面专属直连留作优化项 |
| Node 二进制 | **运行时发现系统 Node(≥22.18,LTS),不打包进 bundle** | 签名/dlopen Team ID 问题(p3-packaging-notes.md);`find_chrome()`/`_resolve_cli()` 均为发现+降级先例 |
| 链语义实现 | **Rust 重写**(行为规格 = §15),DSH JS 侧移植进 runner | 分布式编排无法复用 DSH 进程内 chain;其单测移植为 broker 协议级集成测试 |

### 2.1 启动时序

**桌面(Tauri)**:
1. Tauri 启动 → broker 开始监听 Unix socket(`$TMPDIR/ginno-mod-broker.sock`,旁写 token 文件防其他进程抢连);
2. Tauri 按现有方式 spawn Python runtime(`apps/desktop/src/lib.rs`),注入 env `GINNO_MOD_BROKER_SOCK` + token 路径;
3. Python ModChannel 连接 → **推送配置**(启用 mod 清单、grants、node 路径、限制值 hookTimeoutMs 等);
4. broker 逐 mod spawn runner → runner 解析清单、import ESM、跑 `register(on)` → hooks 批量上报 → ready;
5. 事件流开始。runtime 重启 = Python 重连,broker 与 runner **不动**(目标 4)。

**dev / web(无 Tauri)**:Python 用 `_resolve_cli` 式发现发现 `ginno-mod-broker` 二进制(make 构建产物或 `~/.ginno/bin/`),自己 spawn(`--socket <path>`),其余同。发现不到 broker 或 node → 功能禁用 + 设置页引导(external-agents 同款体验)。

### 2.2 设计演化记录

1. 「RPC 经 Rust 纯转发」:否——转发无功能收益、`tool.call` 在每次工具调用路径上、Python→Rust 无现成通道、dev 断。
2. 「单 mod-host(Python 子进程,链在 host 内闭环)」:曾为 v1 主案;分布式编排定案后被取代,其**帧格式、事件挂载点、band 协议、DSH 移植的 runner 侧代码**全部继承。
3. 「Rust 中心化 mod 总线」:**定案(v2)**。代价已评估:链语义 Rust 重写、Python↔Rust 通道新建、每 mod 一进程;收益:per-mod 隔离、IPC 边界安全管控、mod 独立于 runtime 生命周期、为常驻服务型 mod 留路。

---

## 3. ginno-mod-broker 设计(Rust crate)

### 3.1 crate 形态

```
crates/mod-broker/            # 或 packages/mod-broker/,workspace 成员
  src/lib.rs                  # BrokerCore:协议、编排、op 表、SurfaceTable(不依赖 tauri)
  src/bin/ginno-mod-broker.rs # CLI: --socket <path> 独立跑(dev/web 用)
  apps/desktop                # Tauri app 依赖 lib.rs,启动时构造 BrokerCore 监听
```

- Rust 侧依赖:tokio(异步运行时)、serde_json(帧)、regex(matcher)、reqwest(`$.http`)、tokio::process(`$.process`)。
- **无盘上状态依赖**:配置全由 Python 推送;`$.store`/`$.state` 落盘位置 `~/.ginno/mods/`(broker 直接写,不经 Python)。

### 3.2 链编排器(分布式 chain 语义)

行为规格 = §15(逐条对齐 DSH 已核实的源码语义),Rust 实现要点:

- **dispatch**:broker 持全局注册表(mod 装载序 + hooks);事件到来按序逐 hook 派发;**matcher broker 侧预筛**(§3.2.1);命中的 hook 以 `{kind:'event', invocation, event, session, payload, deadlineMs}` 帧发给对应 runner。
- **next() 单次语义**:runner 发 `{kind:'next', invocation, e}`;broker 按 invocation 记账 `beneath`——**首调创建下方执行,二调直接回缓存结果**(§15.2 全套规则:hook 已作答则下方结果保留、null 是合法答案、catch 共享下方状态、失败按 hook×kind 去重上报)。
- **BudgetClock(busy-time 记账)**:broker 天然知道 hook 的等待区间——所有 `next()`/`$` 调用都经 broker round-trip;**busy = 派发→settle 总时长 − 等待 broker round-trip 的时长**;`clock.sleep` 由 broker 直接计时(计入 busy,符合规范)。预算值:hook 10s、`prompt.edit` 50ms、catch 1s、`session.end` 合计 1.5s。本地 socket 往返 ~50-200µs,50ms 预算下 IPC 占比可忽略。
- **runner 崩溃 = 该 hook 失败**:skip + report,链继续——per-mod 隔离的核心红利;broker 同时启动该 runner 的退避重启(1s/5s/30s,3 次后停用并通知 Python → 设置页)。
- **事件输入不可变**:跨进程本就是值拷贝(JSON),每侧各自深冻结即可;改写必须传副本。
- **`$.x.y` 也是事件**:mod 的每个 `$` 调用先在 broker 过一遍事件链(只派给**先于它加载**的 mod,`order < raisedBy.order`),放行后才进 op 表——「先装的 mod 拦截后装的调用」的中间件语义保留;`tool.call` op 不二次入链(broker 用 callOrigins 标记 raisedBy)。

#### 3.2.1 matcher 的跨进程表示

matcher 在 JS 侧可含 RegExp 对象,跨进程序列化约定:

- string → 相等;array → 成员之一;`{"regexp": "<source>", "flags": "..."}` → 正则。
- **string/array 由 broker(Rust)直接评估**(省掉无谓派发);**regexp 由 broker 用 Rust regex 评估**,并在文档与 validate 报告中注明 JS/Rust 正则方言差异风险(如 backreference、lookbehind 支持差异);runner 侧不复核(避免双实现漂移,方言差异归入已知限制清单)。

### 3.3 op 表:broker 本地实现 vs Python 后端

**原则:凡安全敏感的(fs/http/process/env)与跨 mod 共享的(state/store/clock)在 broker 实现——管控发生在能力到达 mod 之前;凡需要 runtime 事实的转发 Python。**

| 归属 | op | 实现 |
|---|---|---|
| **broker 本地** | `state.get/set`(订阅记账在此)、`store.get/set/delete/keys`(全机 4MiB,per-plugin 写串行化落盘)、`clock.now/sleep/after/every`(timers 归 broker,会话结束随会话销毁,close 语义=§15.4)、`fs.*`(**根限定 workspace**,4MiB/文件,`resolve:true` 跟随 symlink)、`http.fetch`(域名白名单=grants,4MiB 有界流式读)、`process.run`(argv 声明制+grants,30s 默认/10min 上限,stdout/stderr 各 1MiB,kill tree)、`env.get`(只读白名单 PATH/HOME/GINNO_*)、`ui.toast/status/log/notice/copy`、`ui.open`(P0/P1 恒 `{isPlaced:false, reason}`,P2 接右栏) | Rust 直接实现,grants 检查先行 |
| **Python 后端** | `session.id/cwd/root/model/turns/messages/usage/version`、`prompt.submit`、`tool.list/call`(走权限策略,不豁免)、`tool.register`、`command.register/run/list`、`mcp.call`、`model.complete`(P2,用量记账)、`agent.*`(P2)、`turn.abort` | broker 转发帧给 ModChannel,Python 实现(§5.4) |
| **不实现** | `$.audio`、`$.telemetry`、`$.settings`、`$.env.set`、`$.fs.ancestors`(P2 评估) | broker 回 `{ok:false, code:'no-implementation', message:'no implementation for <ns>.<method>'}` |

### 3.4 SurfaceTable(band 渲染,移植 DSH surfaces.ts 语义)

- 每会话一条 band:`generation` 递增;树未变(JSON 等价)则**保持 generation 不重发但刷新回调持有**;Button 的 `onPress` 闭包留在 runner,序列化树带 `<modId>:<actionId>`,broker 持 `actions: Map<(generation, actionId) → (runner, 发回路径)>`。
- **press**:前端回发 `{generation, actionId}`;过期代际忽略 + warning;当前代际则把 press 帧发给对应 runner 执行 `onPress`(无时限),完成后重绘回新快照。
- **订阅重绘**:`ui.render` 期间该 mod 的 `state.get` 全过 broker → broker 以 collecting set 记录本 pass 读过的 slot,结束后替换 subscribed;`state.set` 命中 subscribed 的 band 重绘;重绘延迟一个 macrotask(§15.4)。
- 重绘触发源:session.start 后、订阅 state 写入、`ui.invalidate/open/close`、每次 `tool.call` 链与 `turn.complete` 落定后、press 后。
- band 快照经 Python 转发:`bands.update` 通知 → ModChannel → WS `mod.bands`。
- 限流 10 次/秒(broker 内节流)。

### 3.5 runner 监督与 Node 发现

- spawn:`node <runner.mjs> --mod <dir>`(runner.mjs 为 esbuild 单文件零依赖产物,rsync 进 Tauri resources;broker 二进制同路径规则);stdio PIPE,stderr 落 `~/.ginno/logs/mod-<name>.log`。
- 生命周期:ready 握手 → 心跳(runner 每 5s `ping`,broker 15s 无响应判死);崩溃退避重启 1s/5s/30s,3 次停用 + 通知。
- 资源上限:每 runner `--max-old-space-size=128` + 超时强杀;进程组隔离。
- **node 发现**:配置里 `mods.nodePath` 优先(Python 用 `_resolve_cli` 三段式发现后写入推送),broker 不自带发现逻辑——单一实现,坑只踩一次(GUI PATH 盲区是修过的坑)。

### 3.6 grants 安全执行

- Python 推送 `mods.<name>.grants`(首次运行确认见 §8);broker 对本地实现的 op **逐调用检查**:fs.write 未授权 → `{ok:false, code:'denied'}`;http 域名不在白名单 → 拒;process argv 未声明 → 拒。
- Python 后端的 op(tools/commands)在 Python 侧再过一层权限策略(双层)。
- `mods.allowOverrideDenyRules=false`(默认):已被 policy deny 的 `tool.call` 改写,broker 侧拒绝转发改写结果。

### 3.7 CLI 模式

`ginno-mod-broker validate <dir>`:spawn 临时 runner 装载 mod(mock `$`),汇总 hooks(事件 × matcher)、`$` 调用清单、state key、**Ginno 不支持的事件/未实现的方法单列**——输出格式照抄 `claude plugin validate`,同时作为 §8 首次运行的权限清单数据源。`test`(复刻 claude-code/testing,P2)。

---

## 4. mod runner 设计(Node,每 mod 一进程)

### 4.1 形态与装载

- `packages/mod-runner/`(Node ESM TS 源码,esbuild 单文件 `dist/mod-runner.mjs`,零 npm 运行时依赖);**mod 本体若有 npm 依赖必须 vendor**(P0 政策,装载时检测并 warning)。
- Node ≥22.18(原生 type stripping 直接加载 `.ts` mod;不满足时仅 `.js`)。
- **读清单(与 DSH 的关键差异)**:`.claude-plugin/plugin.json`(name/version/types)+ `hooks/hooks.json`(`modules[0]`)→ `import(file://…)` → `defineMod` 包装跑 `register(on, options)`;`options` = userConfig 默认值 ∪ 推送来的 `mods.<name>.config`。用户 `git clone` 任意 mod 仓库进 `~/.ginno/mods/` 即用。

### 4.2 runner 侧职责(移植自 DSH)

- **`$` shim**:mod 看到的 `$` 与规范同形——每个方法把参数整形为帧发给 broker(await 结果;`ui.log/toast/status` fire-and-forget);**未实现成员返回命名 reject 的函数**(Proxy 方案照抄 DSH api.ts);`$` 只读。
- **元素构造器**:`$.ui.resolve(e)` 返回 `Box/Text/Button` 构造器(冻结、品牌符号),返回值在 runner 序列化为 JSON 树(Button 带闭包占位,由 broker 编 actionId);`treeProblem` 校验照抄(§15.8 序列化规则:仅标量 props、children 展开)。
- **createOn 校验**:KNOWN_EVENTS/`ns.*` glob/裸注册仅一次/matcher 形状(§15.3),注册表批量上报 broker(`{event, matcher}` 序列化格式见 §3.2.1);`classic.*` register 时拒绝。
- **hook 执行**:收到 event 帧后构造冻结的 `e`、`next`(发帧,单次性由 broker 保证)、`$`(本 invocation 绑定);`.catch` handler 本地保存,失败时 broker 发 failure 帧来调用。
- **runner 不做任何策略决策**:预算、链序、matcher 预筛、grants 全在 broker——runner 越薄越可信。

### 4.3 DSH 移植清单(MIT,保留原注释与文件头,仓库 NOTICE 注明 Copyright (c) 2026 DeepSeek)

| DSH 文件 | 去向 |
|---|---|
| `api.ts` / `elements.ts` / `module.ts` / `values.ts` | **移植进 runner**(改为发帧) |
| `chain.ts` / `engine.ts` / `matcher.ts`(评估部分) / `surfaces.ts` / `host-ops.ts` / `index.ts` | **不移植**——语义由 broker(Rust)按 §15 规格重写 |
| `tool-names.ts` 别名表 | **broker 侧**(Rust 双向 Map:Read→read_file 等,§15.8) |
| `tests/`(chain/matcher/module/elements 等 spec) | **移植为 broker 协议级集成测试**(起真 runner 验证分布式链语义) |

---

## 5. Python 侧:ModChannel + 事件抽点 + op 后端

### 5.1 模块划分(`ginno_runtime/mods/`)

```
channel.py     # ModChannel:连 broker socket,帧编解码,per-session 事件串行(跨 session 并行)
events.py      # 事件抽头注册(下表挂载点),把引擎事实整形成规范 payload
ops.py         # Python 后端 op 实现(session.*/prompt.submit/tool.*/command.*/mcp.call)
api.py         # FastAPI:GET /api/mods、PUT /api/mods/<name>(启停/配置/grants)、POST install/validate
bridge_utils.py# node/broker 二进制发现(_resolve_cli 移植复用)、dev 模式 spawn broker
```

### 5.2 连接生命周期

- 桌面:读 env `GINNO_MOD_BROKER_SOCK` + token → 连接;dev/web:发现并 spawn broker 二进制(`--socket`)。
- 连接后**推送配置**(mods.items/grants/node 路径/预算值),之后 broker 才 spawn runner;断线重连(指数退避),重连后重新推配置(broker 侧幂等:配置变化 → 逐 mod 重载差异项)。
- 广播:broker 的 `mod.status` 通知(loaded/error/disabled/restarting)→ WS `mod.state.changed` → 设置页。

### 5.3 事件映射表(挂载点均为已核实的代码位置)

| Claude 事件 | Ginno 挂载点 | 优先级 | 改写/接管语义 |
|---|---|---|---|
| `session.start` | `api/sessions.py:545 create_session()` 成功后 | P0 | 仅观察 |
| `session.end` | `delete_session()`(sessions.py:810)+ 应用退出 | P0 | 仅观察(1.5s 总预算;hooks 落定后 broker 才 forgetSession) |
| `turn.start` | `api/stream/engine.py:822`(现有 `turn.start` WS 事件同点) | P0 | 仅观察 |
| `turn.complete` | engine.py:1266-1310 完成分支(`message.end` 之前) | P0 | `{text}` 改写(P1);P0 观察;payload 含 TurnRecord 折算(§15.7) |
| `prompt.submit` | `api/stream/turn.py:286 _run_stream()` 入口、`user_text` 落地之前 | P0 | `{drop}`(P1);改写 text+context(P1,算法 §15.6);P0 观察 |
| `tool.call` | `graph.py` permission_node 内,**与现有 PreToolUse hooks 同位**(graph.py:1086-1104),即 **policy.decide 之前、tools_allow 之后** | P0 | `{deny}`(P0)/ `{result}` 接管与改写 result/`isError`(P1,**须过该工具输出 schema 校验**,§15.5)/ 改写 args(P1;P0 照抄 DSH 拒绝文案) |
| `ui.render`(AbovePrompt) | broker 渲染循环(§3.4) | P0 | 返回元素树 = 画 band |
| `command.run` | `commands/registry.py` 查内置命令 **未命中** 时派发 | P1 | mod 注册命令 P1,拦截 P2 |
| `tool.check` | permission_node 内 policy.decide 之前、`tool.call` 之后 | P1 | `{decision}` 三值 |
| `agent.spawn` | `agents/` 子代理 spawn 路径 | P1 | `{deny}`(P1)/ 改 `model`(P2) |
| `session.compact` | `compaction.py:129 maybe_compact_history()` 入口 | P2 | `{skip}` |
| `ui.press` / `ui.input` | 前端 `mod.ui.press` 帧 → Python → broker → runner | P1 | — |
| `classic.PreToolUse` / … | 桥接现有 `hooks/dispatcher.py` 的 HookEvent | P1 | 复用 `{block, reason, inject, rewrite}` |
| 其余规范事件 | — | 不做 | 注册时 warning:「registered, but this host never raises that event」 |

规则:`tool.call` 选 **policy 之前**(与 Claude 一致、与 DSH 相反)——permission_node 已确立「hook 先于 policy、bypass 下仍运行」(graph.py:1086 注释)。`turn.stopped`/`error` 两个非正常收尾(engine.py:1316-1424)P0 一并派发 `turn.complete`(payload 带 stopReason)。**无 mod 注册某事件时零 IPC 开销**(broker 查注册表短路)。

### 5.4 runtime-backed op 实现

| 优先级 | op | 实现 |
|---|---|---|
| **P0** | `session.id/cwd/root/model/version/usage/turns` | `_SESSIONS` / session meta;usage 从 usage 记账 |
| | `session.messages()` | checkpointer 重建 history,最近 4096 条,tool 名过 broker 别名表(由 broker 在转发时已映射) |
| **P1** | `prompt.submit` | 等价前端 `invoke` 帧;正文框架 `Message from the "<mod>" mod:`(`asUser` 可免);origin 标注(§15.6) |
| | `tool.list/call` | `build_all_tools()` 注册表;call 走权限策略不豁免;callOrigins 语义在 broker |
| | `tool.register` | 注册名 `mcp__<plugin>__<tool>`(照抄 DSH,§15.8) |
| | `command.register/run/list` | 注册进 commands 体系,前缀保留 `mod/<name>/` |
| | `mcp.call` | 转发 `mcp/registry.py` 已连接 server |
| | `turn.abort` | 等价 `stop` |
| **P2** | `model.complete/classify` | 走现有 model_configs,`maxTokens` ≤ 64000,用量记账 |
| | `agent.register/spawn/list` | 对接 `agents/registry.py` + subagent 体系 |

---

## 6. 通信协议(三段同构,换行 JSON 帧)

### 6.1 帧类型

```jsonc
// 请求-响应($ op;Python↔broker 与 broker↔runner 同构,方向可反转)
{"v":1,"id":42,"kind":"call","ns":"fs","method":"read","args":{"path":"./x.py"},"session":"s1","mod":"token-weather"}
{"v":1,"id":42,"kind":"result","ok":true,"value":"<content>"}
{"v":1,"id":42,"kind":"result","ok":false,"code":"denied|no-implementation|not-found","message":"..."}

// 引擎事件(Python→broker)与 hook 派发(broker→runner)
{"v":1,"kind":"event","event":"tool.call","session":"s1","invocation":"i-17","payload":{...},"deadlineMs":10000}

// next()/catch(runner→broker;invocation 关联)
{"v":1,"kind":"next","invocation":"i-17","e":{...改写后的事件输入或原样}}
{"v":1,"kind":"catch-call","invocation":"i-17"}   // broker 失败信息随 event 帧重入 runner

// 通知(bands/toast/status,单向)
{"v":1,"kind":"notify","method":"bands.update","session":"s1","args":{"generation":3,"tree":[...]}}
```

- `v` 协议版本(P0=1);语义按 Claude Code 2.1.289 固化,升级是显式决策。
- **invocation id** 贯穿一次 hook 派发:next 单次性、budget 记账、catch 关联都挂它。
- 并发:请求按 `id` 关联(两侧各持 pending map);同一 session 的事件串行(await 完成才继续),跨 session 并行;runner 对同一 invocation 的 hook 串行。
- 事件超时:Python 计算 `deadlineMs` 传入,broker 强制;超时按事件默认策略继续(`tool.call` 超时 = 放行)。
- WebSocket 侧(前端↔Python)新增:`mod.bands` / `mod.toast` / `mod.ask{askId,text,options}` / `mod.answer{askId,answer}` / `mod.ui.press{generation,actionId}` / `mod.state.changed`;`ask` 独立于 LangGraph interrupt(mod 的提问多在 turn 之外),session 切走即取消。

### 6.2 三段差异

| 链路 | 传输 | 握手 |
|---|---|---|
| Python ↔ broker | Unix socket + token | `hello{role:'runtime',config:{...}}` → `ready{mods:[...]}` |
| broker ↔ runner | 子进程 stdio | spawn 参数 `--mod <dir>`;runner 上报 `hooks-registered{hooks:[{event,matcher}]}` → broker 回 `start` |

---

## 7. 前端方案(apps/web)

### 7.1 AbovePrompt band 插槽(P0)

- **位置**:`ChatStream.tsx` composer box(约 1032-1183 行)内、`composerHint` band(1066-1073)同级、其上方;做成**列表插槽**(现状已有 composerHint、steer 队列条两个单行先例)。
- **数据协议(broker SurfaceTable 的 generation/actionId 语义,经 Python WS 转发)**:`mod.bands {generation, tree}`(树未变时 broker 保持 generation 不重发);按钮点击回发 `mod.ui.press {generation, actionId}`——过期代际的 press broker 直接忽略并记 warning,当前代际执行回调并重绘后回新快照。

### 7.2 Toast 宿主(P0)

- 全局无 toast 系统 → `AppShell` 顶层新增 `ModToastHost`,消费 `mod.toast`(参照 `fallbackToast`:8s 自动消失,堆叠);info/warn/error 三色。

### 7.3 `$.ui.ask`(P1)

- 新帧 `mod.ask` → 前端复用 `AskUserCard` UI 形态渲染浮层 → `mod.answer` 回传 → Python → broker resolve 对应 Promise;孤儿清理:session 切走即 reject `aborted`。

### 7.4 Pane(P2,对应规范 `ui.render(Pane)` + `$.ui.open`)

- Ginno 的 **RightDock/RightPanel 就是天然 pane**(`docs/design/right-panel-redesign.md`)。P2:`$.ui.open` 在右栏开 tab,`Input/Select/ui.input`、`Markdown/Code` 补齐。P0/P1 阶段恒 `{isPlaced:false, reason:'pane not available'}` 让 mod 优雅降级到 band。

### 7.5 设置页(P0)

- `SettingsNav` TABS 加 `mods`:mod 列表(名称/版本/状态 loaded|error|disabled|restarting/来源全局|项目)、启停、**首次运行权限确认**(§8)、打开目录、卸载、配置编辑;「验证」按钮 → `POST /api/mods/validate` → broker validate 报告(注册事件 × 触发情况、`$` 调用 × 实现情况);broker/node 运行时状态(版本/未发现引导,external-agents 的 `available()/configured()` 两级探测模式)。

---

## 8. 安全模型

1. **进程隔离**:每个 mod 独立 Node runner;mod 代码摸不到 Python runtime、broker 内部状态、其他 mod 进程。
2. **IPC 边界管控(核心升级)**:所有能力经 broker;grants(fs.write / http 域名 / process argv / env / tool.call 拦截)在**能力到达 mod 之前**由 Rust 强制——未授权调用得到 `denied`,不是事后审计。
3. **能力声明 + 首次运行确认**:装载前 broker `validate` 产出权限清单,用户逐项批准,存 `settings.json mods.<name>.grants`(Python 持有,推送 broker 执行)。
4. **路径与网络守卫**:`$.fs` 限定 workspace 根;`$.http` 域名白名单;`$.process` argv 声明制;`$.env` 只读白名单。
5. **资源上限**:hook 预算(§3.2)+ store/state 4MiB + band 限流 10/s + 每 runner `--max-old-space-size=128` + 心跳判死 + 超时强杀。
6. **socket 防抢连**:token 文件(0600)随 socket 创建;runner stdio 无对外面。
7. **文档明示**:mod「拥有 runner 进程权限、能力受 grants 约束」;安装责任在用户。
8. **治理开关**(settings.json `mods.*`):`enabled`、`allowManagedModsOnly`、`allowOverrideDenyRules`(默认 false)。

---

## 9. 配置与状态

```jsonc
// ~/.ginno/settings.json 增量(走现有 _DEFAULT_SETTINGS + ensure_layout 迁移模式)
"mods": {
  "enabled": true,
  "allowOverrideDenyRules": false,
  "nodePath": "/opt/homebrew/bin/node",   // Python _resolve_cli 发现结果,可覆盖
  "brokerPath": null,                      // dev/web 模式独立 broker 二进制路径(默认自动发现)
  "items": {
    "token-weather": {
      "enabled": true,
      "source": "global",                  // global | project
      "grants": { "fs.read": true, "process.run": false },
      "config": { "units": "metric" }      // userConfig,defaults.update(stored) 合并(connector 同款)
    }
  }
}
```

- **所有权切分**:settings.json 归 Python(唯一事实来源,连接后推送);运行态(`$.state` 会话级内存 + 可选落盘、`$.store` 全机 4MiB 落盘、timers、SurfaceTable)归 broker,存 `~/.ginno/mods/`。
- mod 目录:`~/.ginno/mods/`(全局,P0)+ `<workspace>/.ginno/mods/`(项目级,P1,覆盖同全局名)。
- WS 广播:`mod.state.changed`。

---

## 10. 与现有 hooks / connectors 的关系

| 既有系统 | 关系 |
|---|---|
| `hooks/dispatcher.py`(settings hooks,仅 PreToolUse 生效) | **保留不动**;P1 把其事件以 `classic.<Event>` 暴露(顺序:settings hooks 先,mods 后,两者都先于 policy.decide)。长期 converge 到 mods,另行决策 |
| connectors registry | 借鉴生命周期模式(状态机、事件广播、设置页形态);mods **不注册为 connector**,独立 `mods/` 模块 |
| delegate_agent | 借鉴 `_resolve_cli`(node/broker 发现)、进程监督、kill tree |
| MCP | `$.mcp.call` 复用;runner spawn 借鉴 `_LiveServer` 专属 task 模式 |

---

## 11. 构建与分发

- 新增 Rust workspace 成员 `crates/mod-broker`(lib + CLI 二进制):`make mod-broker` 产出;桌面端 Cargo 依赖 lib;dev/web 用 CLI 二进制(路径发现:仓库 target → `~/.ginno/bin/`)。**broker 不进 PyInstaller bundle**(它不是 runtime 的资源),进 Tauri resources / 独立安装。
- `packages/mod-runner/`:esbuild 单文件 `dist/mod-runner.mjs`,rsync 进 Tauri resources(纯文本,无签名问题);broker 按配置路径引用。
- Node 不打包进 bundle(签名问题);运行时发现 + 设置页引导。
- dev 模式(`pnpm dev`):runtime 源码直跑 + spawn CLI broker + 源码 runner(系统 node type stripping),无 PyInstaller/签名环节。

---

## 12. 分阶段实施计划

### P0 —— 「总线跑通、能加载、能画 band」(预估 2~2.5 周)

| 交付 | 内容 |
|---|---|
| crates/mod-broker | 链编排器(dispatch/next 单次/BudgetClock/catch/matcher 预筛)、op 表(state/store/clock/fs/http/process/env/ui.*/toast)、SurfaceTable、runner 监督 + 心跳、grants 检查、socket 服务 + CLI 二进制、validate 基础版 |
| packages/mod-runner | 清单装载(plugin.json/hooks.json)、`$` shim、元素构造器、createOn 校验、hook 执行(移植 DSH api/elements/module/values) |
| runtime mods/ | ModChannel + 连接生命周期 + 配置推送、事件抽头(session.start/end、turn.start/complete、prompt.submit、tool.call 观察+deny)、Python 后端 op(session 读族)、FastAPI 路由、settings 迁移 |
| 前端 | band 插槽 + Box/Text 渲染器、ModToastHost、mods 设置页(列表/启停/首次确认) |
| 测试 | DSH chain/matcher spec 移植为 broker 集成测试 |
| **验收** | **官方 Token Weather 跑通**:band 显示、随 turn 重绘;杀掉其 runner 不影响其他 mod;runtime 重启 mod 不重载;validate 报告与实际行为一致 |

### P1 —— 「能交互、能拦截」(预估 1~2 周)

- `Button` + `ui.press`、`$.ui.ask`、`tool.call` 改写/`{result}` 接管(含输出 schema 校验)/args 改写、`tool.check`、`command.*`(mod 注册命令)、`prompt.submit` drop/改写/`context`、`classic.*` 桥接、项目级 mods 目录、`mcp.call`、`$.process.run`/`$.http.fetch` + grants 全量。
- **验收**:官方 **Blast Radius** 跑通(拦截 + deny + band 按钮交互)。

### P2 —— 「pane 与生态」(预估 2~3 周)

- RightDock pane + `Input/Select` + `Markdown/Code` 元素、`model.complete`、`agent.*`、`session.compact`、`prompt.compose/section`、`--test` 测试套件、band 排序、`engine.create`/`plugin.register`、`$.fs.ancestors`。
- **验收**:官方 **Replay Theater** 跑通。

### 持续

- 兼容度报告进设置页(每 mod「注册事件 × 实际触发」「$ 调用 × 实现」命中率);规范版本升级评估流程。

---

## 13. 风险与开放问题

| # | 风险/问题 | 应对 |
|---|---|---|
| 1 | **链语义 Rust 重写的正确性**(最大风险) | §15 规格逐条对齐 + DSH 单测移植为协议级集成测试;Token Weather/Blast Radius 官方用例验收 |
| 2 | **官方规范漂移**(Mods 发布 5 天,2.1.287→289 已有变化) | 协议 `v` 字段 + 语义按 2.1.289 固化;升级是显式决策 |
| 3 | **JS↔Rust 正则方言差异**(matcher) | 常用 matcher(字符串/数组)broker 直评;regexp 方言差异列入 validate 报告与已知限制 |
| 4 | 每 mod 一 Node 进程的内存(~30-50MB/个) | 接受;`--max-old-space-size=128`;mod 数量大时评估 runner 复用(退回 §2.2 记录的单 host 形态) |
| 5 | Python↔broker 双向依赖的启动顺序(桌面先 Tauri 后 Python) | env 注入 socket 路径;连不上重连退避;dev 模式 Python 自己 spawn 消除顺序问题 |
| 6 | mod 携带 npm 依赖 | P0 政策:只支持零依赖或已 vendor;检测 package.json 且无 node_modules → warning |
| 7 | Node 未安装 / 版本过低(<22.18 无 type stripping) | available()/configured() 两级探测 + 设置页引导;低版本仅支持 .js mod |
| 8 | 双实现漂移(matcher 两侧、语义两侧) | 原则:runner 零策略(§4.2);matcher 只在 broker 评 |
| 9 | 渲染性能(band 全组重绘) | broker 限流 10/s;P2 tree diff |
| 10 | 开放:常驻服务型 mod(独立于会话生命周期)的 API 形态 | 本架构已支持进程常驻;API 扩展另行设计 |
| 11 | 开放:`dispatcher.py` 是否 converge 进 mods 后删除 | P2 后另行决策 |

---

## 14. 附录:验收用例(官方 mods)

| mod | 规模 | 依赖面 | 对应阶段 |
|---|---|---|---|
| Token Weather | ≈80 行 | 观察事件 + `$.state` + AbovePrompt band | P0 |
| Blast Radius | 中 | `tool.call` + `{deny}` + band 按钮(`ui.press`) | P1 |
| Replay Theater | 大 | pane + 输入 + `$.session.messages` + 命令 | P2 |
| 官方 `diff` | — | settings.read + telemetry + pane + ui.focus/scroll | 不做 |

---

## 15. 附录 A:DSH 源码级借鉴清单(逐项,均已在源码核实;v2 起同时是 broker Rust 实现的行为规格)

> 源码:`packages/experimental/claude-code-mods/src/`(v0.2.1-alpha.1,MIT)。`chain/engine/matcher/surfaces/host-ops` 的语义由 broker 按下列条目重写;`api/elements/module/values` 移植进 runner;DSH 单测移植为 broker 集成测试。

### 15.1 时间预算:BudgetClock(busy-time 记账,不是墙钟)

- 时钟只在 **hook 自身忙时**走:`await next()` 与 `$` 调用期间 **暂停**,唯一例外 `$.clock.sleep` 不暂停(计入 hook 自身时间)。
- DSH 实现:`spent + busySince` 两段记账,`Promise.race([running, clock.expired])`;超时 `HookTimeoutError`(`ran past its ${ms} ms limit`)。
- **broker 实现**:busy = 派发→settle 总时长 − 等待 broker round-trip(next/$ 帧)的时长;`clock.sleep` 由 broker 计时并计入 busy。
- `next.budget.remainingMs` 是 getter,读时现算。

### 15.2 next() 单次语义与失败规则(dispatch 状态机)

- **单次**:首调创建下方执行并缓存;第二次 `next()` 直接返回缓存;hook abandoned 后调 `next()` 返回 undefined。
- **`null` 是合法答案**(「画了个空的」),只有 settle 成**非对象**才算 `returned no result` → 跳过该 hook。
- **skip 规则**:hook throw / 超时 / 无结果对象 → 跳过,链从它收到的 input 继续;**但它已调过 `next` 则下方结果保留**——「跑过就不重跑」。
- **hook 已作答、下方还在跑**:等下方 settle,失败只记 report(`the chain beneath failed after the hook answered`),事件仍以 hook 答案落定。
- **下方失败 vs hook 失败**:失败来自 beneath 时向上原样抛(是事件自身的失败)。
- **`.catch`**:与 hook 共享下方状态(不重复跑);catch 返回对象可代替答案;catch 自身失败/超时(1s)→ 报告并丢弃;`next.error={kind:'throw'|'timeout',message}`、`next.called`。
- **report 去重**:每 hook 每种失败 kind 只报一次,直到 mod 重载。
- **v2 增补**:runner 崩溃/超时未响应 = 该 hook 失败(kind='throw',message 点名 runner dead),链继续——per-mod 隔离红利。

### 15.3 注册与选择(HookRegistry / createOn)

- `on()` 校验:事件名必须是 **KNOWN_EVENTS 精确名、`*`、或 `<ns>.*`**(ns 存在于已知事件),否则 `"<name>" is not an event`;`classic.*` register 时拒绝。
- **同一事件不带 matcher 只能注册一次**(带 matcher 可多次)。
- matcher 字段语义:标量全等;数组成员之一;RegExp test(`lastIndex=0` 每次重置);**dispatch 时逐 hook 求值**(broker 侧预筛,string/array 直评,regexp 见 §3.2.1 方言注记)。
- `*` 匹配除 `telemetry.*` 外一切。
- **顺序**:`order < raisedBy.order`——**mod 的 `$` 调用只被先于它加载的 mod 拦截**;`$.tool.call` 用 callOrigins 标记 raisedBy,不二次入链。
- mod 名 `/^[A-Za-z0-9_-]{1,64}$/`;重名报 `another plugin of that name loads first`;register 抛错包装 `hooks module did not load: register threw ...`(照抄 Claude Code 措辞)。
- 装载成功日志照抄 validate 格式:`hooks module <name> loaded (tier user); events: turn.start{...}, tool.call{tool=/^Write/}`;不触发事件清单单独 warning。

### 15.4 状态/定时器/会话清理

- `$.state`:**每会话**(slot = `<plugin>\u0000<key>`);DSH 内存态,Ginno broker 持有(可选落盘)。
- **订阅重绘**:渲染期间收集本次 render 读过的 slot,结束后替换 subscribed;`state.set` 命中 subscribed 才重绘;重绘**延迟一个 macrotask**——v2 中 broker 直见所有 state.get,收集天然成立。
- **TimerSet**:`after/every` 句柄不阻止进程退出;close = 取消全部 + 拒绝新建 + **await 正在跑的回调**;timers 按「发起会话 + mod」归属,**会话结束随会话销毁**;timer 回调经 press 帧同通道发回 runner 执行。
- `session.end`:hooks 落定前 state 仍可读、timers 仍活;全部 settle 后才 forgetSession。
- **DetachedRuns**:`turn.complete`/`session.end` 不阻塞主链路 detached 派发;rejection 一律 report;dispose 统一 abort + allSettled 排干。

### 15.5 tool.call 管线接线(DSH 版行为规格,Ginno 差异见注)

- **P0 参数改写拒绝**(DSH 现状):`validateNext` 校验 `{tool,args}` 与入参一致——改工具名报 `rerouted the call from X to Y`,改参数报 `rewrote the arguments of X`,hook 被 skip。「logged call runs as logged」。
- **Ginno P1 差异**:真支持改写(改写后重新走 permission policy);P0 先照抄拒绝文案。
- **deny 形状**:`{deny}` → `isError:true` + `Error: <reason>` + 错误码 `MOD_DENIED`;`{result}` 接管 → 成功值必须过**该工具输出 schema 校验**,否则 error-shaped(`MOD_ANSWERED`)——防 mod 用假 JSON 骗过下游。
- **改写 result/isError**:经 replacements 机制在 post-execute 替换 content;成功被标 isError → 变失败。
- v2 注:`validateNext`/callOrigins/replacements 由 broker + Python 合作实现(改写判定在 broker,工具结果替换在 Python tools_node)。

### 15.6 prompt.submit 改写算法(DSH `index.ts`,可直接复用于 `_run_stream`)

- **改写文本落位**:首个 text block 承载改写后全文,其余 text block 删除,**非 text block(图片等)原位保留**;全无 text block 的 prompt 在末尾追加。
- **`context` 追加**:作为额外 text block 附在**最后一条人类消息**后。
- **结果宽容**:`drop` 必须 string;`text` 非 string 保留原文;`context` 只保留 string 行。
- `{drop}` → batch reject,记 info。
- **origin**:mod 经 `$.prompt.submit` 排队的消息记 messageId→mod 名,引发的 `prompt.submit` 以 `{kind:'plugin',name}` 为 origin(否则 `{kind:'composer'}`);正文框架 `Message from the "<mod>" mod:`(`asUser:true` 可免)。

### 15.7 turn 折算(TurnRecord)

- 每会话 `{turn, startedAt, answer, usage}`:answer = 最后一次提交的 assistant 文本;usage 累加 input/output/cache_read/cache_creation 并记 model。
- `turn.complete` payload:`{turnId, answer, durationMs, isAborted, reason:'answer'|'aborted'|'error', usage}`;detached 派发;`{text}` 改写 P0 进日志。

### 15.8 其余值得照抄的小语义

- **`$` 只读**:set/defineProperty/deleteProperty 拦截;未实现成员是**返回命名 reject 的函数**(`no implementation for <ns>.<method>`),不是 undefined。
- **fire-and-forget**:`ui.log/toast/status` 不 await、失败仅 report。
- **store 写串行化**:per-plugin promise 链防 read-modify-write 交错;写入前校验 4MiB(官方口径全机 4MiB;DSH 按每插件,不采用)。
- **http.fetch 有界读**:流式读 body,超 4MiB 立即 cancel 并报错。
- **process.run**:argv 数组**不经 shell**;stdout/stderr 各限 1MiB;grace 1s;`AbortSignal.any([事件 signal, timeout])`。
- **fs.stat**:symlink 跟随目标(`resolve:true`),悬空链接 kind=`other`;`mtimeMs` 可先报 0。
- **工具名别名表**(mod 看到的 Claude Code 名 → 宿主名),Ginno 版初始映射(broker 侧双向 Map):

  | Claude Code | Ginno builtin |
  |---|---|
  | `Read` | `read_file` |
  | `Write` | `write_file` |
  | `Edit` | `edit_file` |
  | `Bash` | `bash` |
  | `Glob` | `glob` |
  | `Grep` | `grep` |
  | `AskUserQuestion` | `ask`(P1) |
  | `Skill` | `skill`(P1) |
  | `Task` | `subagent`(P1) |
  | `TodoWrite` | `todo`(P1) |

  未映射名原样透传。
- **mod 注册工具名**:`mcp__<plugin>__<tool>`(照抄)。
- **SERVED_EVENTS 与 unserved 报告**:broker 维护「真正派发的事件」集合;注册了集合外引擎事件 → 装载时逐个点名 warning。P0 集合即 §5.3 表中 P0/P1 行。
- **`$.env.set` 不实现**(DSH 允许写共享 env 是其无沙箱的表现;Ginno 拒绝)。

### 15.9 Ginno 与 DSH 行为差异总表(本设计 v2 的立场)

| 维度 | DSH | Ginno v2 | 理由 |
|---|---|---|---|
| 运行位置 | 同进程,Cordis 插件 | **每 mod 一 runner 进程 + Rust broker 编排** | 隔离 + IPC 边界管控 + 独立生命周期 |
| 链语义实现 | 进程内 JS(chain.ts) | Rust 重写(规格=本附录) | 分布式编排无法复用进程内实现 |
| 清单文件 | 不读 plugin.json/hooks.json | runner 读 | drop-in 兼容生态 |
| `tool.call` 时机 | 权限判定后 | 权限判定前 | 贴 Claude 原语义;permission_node 已有 hook 先于 policy 的先例 |
| 参数改写/改道 | 拒绝(skip + report) | P0 同 DSH;P1 真支持 | Claude Code 支持 |
| `$.env.set` | 允许 | 不实现 | 写 env 是全局副作用 |
| `$.store` 限额 | 每插件 4 MiB | 全机 4 MiB(官方口径) | 规范一致优先 |
| `$.fs` 根 | 会话 cwd | workspace 白名单 + grants(broker 强制) | 安全 |
| pane | 放弃 | P0/P1 同降级;P2 接 RightDock | Ginno 有现成右栏 |
| state 持久化 | 内存 | broker 持有,可选落盘 | 桌面常驻、跨重启 |
| `session.end` state 生命 | hooks 落定后 forgetSession | 同 | 照抄 |
