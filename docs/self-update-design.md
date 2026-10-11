# Ginno 自更新(self-update)方案设计

> 状态:**方案待评审**。目标:让 Ginno 具备「杀掉自己 → 换新 → 拉起新进程 → 恢复原会话继续对话」的完整能力,首期服务于 agent 驱动的本地源码自更新(agent 改代码 → `make app` → 自己更新自己)。
> 相关:`CLAUDE.md`(zlib 已知故障)、`docs/p3-packaging-notes.md`、`docs/notice-center-design.md`(更新完成通知的落点)。

---

## 0. TL;DR

- **一句话**:新增一个**外部接力进程 relauncher**(detached 小脚本/二进制),把更新拆成互不重叠的三段——**build(跑着也能做)→ swap+relaunch(relauncher 独占)→ restore(新进程自动完成)**;会话恢复不新造机制,直接复用已有的 `ginno-last-session` + checkpoint `/history` + `turn_state` 探针链路。
- **为什么是外部进程**:macOS 上进程不能可靠地"换掉自己再重启自己"——壳退出后没人执行 `open`;而且换 bundle、等旧进程死透、验签、回滚都必须在两个进程生命周期的空档里由第三方完成。这是 Sparkle 等所有 updater 的共同形态。
- **关键现状红利**:①会话恢复链路**已经存在**(前端断线重连 + checkpoint 全量重建,重启 app 对前端等价于一次长断线);②`restart_sidecar` 已证明「壳活着换后端」可行,broker 故意设计为活过 sidecar 重启;③2026-10-05 后 checkpoint/index 全部原子写,8fd0759 已有僵尸 checkpoint heal 路径。
- **成本**:新增 relauncher 脚本 + 壳 `self_update` 命令 + runtime `self_update` 工具 + update-state 状态机,约 4 个新文件 + 3 处改动;无新 Rust 依赖(不引 tauri-plugin-updater)。

---

## 1. 需求与场景

### 1.1 驱动场景

| # | 场景 | 触发者 | 说明 |
|---|---|---|---|
| S1 | **agent 自我更新(主场景)** | agent 在会话里调 `self_update` 工具 | agent 改完代码跑 `make app`,然后说"我要重启自己了",杀自己→换新→回来继续汇报 |
| S2 | 用户手动"重启应用" | Debug 菜单 / 设置页 | 排障用(现在只能 ⌘Q 再手动开);也是 P0 验证整个 loop 的最小闭环 |
| S3 | 远端版本 feed 更新 | 设置页"检查更新" | **非目标**(本方案不建发布服务器;但 relauncher/回滚骨架为它预留,见 §9) |

### 1.2 两个硬需求拆开看

1. **杀自己 + 启动新进程**:需要外部接力(壳退出后必须有活着的进程执行 `open`)+ 优雅退出(别把正在跑的 turn 直接腰斩成不可恢复状态)。
2. **恢复会话继续对话**:需要 checkpoint 一致(被杀时刻的 turn 留下可 heal 的状态)+ 前端自动回到原会话 + 更新前后的"上下文连续性"(agent 知道自己更新完了)。

---

## 2. 现状盘点(证据)

### 2.1 壳(Tauri,`apps/desktop/src/lib.rs`)

| 环节 | 现状 | 对自更新的意义 |
|---|---|---|
| sidecar spawn | `spawn_sidecar` lib.rs:401-446,release 跑 bundle 内 `resources/runtime/ginno-runtime`,stdio → `~/.ginno/logs/sidecar.log` | 前端+后端**同在 app bundle 里**,生产 web 就是 sidecar 伺服的(tauri.conf.json:8 `frontendDist http://127.0.0.1:8787`)→ **web/runtime/shell 三者只能一起换** |
| sidecar 重启 | Debug 菜单 `restart_sidecar` lib.rs:461-548,RestartLock 防并发 | 「壳活着换后端」已验证可行;broker 故意活过重启(lib.rs:239-241) |
| app 重启/relaunch | **没有**。无 tauri-plugin-updater、无 relaunch 代码 | 本方案核心缺口 |
| 退出杀 sidecar | `RunEvent::Exit` lib.rs:2030-2049 `child.kill()`=**SIGKILL 硬杀**;runtime 的 uvicorn graceful shutdown(lifespan finally,server.py:196-229)**实际走不到** | 一致性目前靠"下次启动对账 + 惰性 heal"兜底(8fd0759 的背景);自更新要更体面的退出,P1 改 SIGTERM→宽限→SIGKILL |
| single-instance | **没有**。双开防线只是新实例 `kill_stale_sidecar` 抢 8787 | relauncher `open` 新 app 时旧 app 必须已死透,否则双壳并存;P1 补插件 |
| 签名 | 自签身份 "Ginno Local Code Signing",Makefile:61-66 防 linker-signed | 换 bundle 前必须 `codesign --verify` 同一 designated requirement,否则白屏 |
| 版本号 | tauri.conf.json 固定 "0.1.0",无 bump | 需要 build 版本指纹(git sha),否则"更新成功了吗"无法回答 |

### 2.2 runtime(Python,`packages/runtime/src/ginno_runtime/`)

| 环节 | 现状 | 对自更新的意义 |
|---|---|---|
| 会话持久化 | `~/.ginno/projects/<slug>/sessions/`:`_index.json`(meta,原子写)+ `<sid>.json`(checkpoint,full/delta,tmp+rename 原子,RLock 串行) | **进程被杀不丢数据**;这正是"杀了也能恢复"的底座 |
| 重启后继续会话 | `_ensure_session`(sessions.py:1201-1286)WS 连接时按 meta 重建 graph,历史由 checkpointer 按 thread_id 恢复;无启动预载 | 新 sidecar 起来后,老会话自动可用,零改动 |
| 中断 turn 的 heal | `_heal_interrupted_turn`(engine.py:669)补 `(interrupted)` ToolMessage、落半截回答;僵尸 checkpoint 判定在 `/compact` 路径(commands/registry.py:482-515)惰性触发 | 杀进程会制造僵尸;heal 已有但触发点太靠后(用户 /compact 才修),P1 提前到 WS 连接/启动对账 |
| interrupt 持久化 | `get_pending_interrupt`(checkpointer.py:346)直接读文件,重启后 WS 重连即重发并可 resume | **停在 interrupt 的 turn 是唯一真正跨重启存活的"进行中"状态**——更新前把 turn 停在 interrupt 是理想的 quiesce 形态 |
| turn 活性 | 全内存(`_RUNNING_TURNS/_TURN_TASKS` 等 server_shared.py:76-139);连接断开不杀 turn(engine.py:887-897 广播语义) | 进程死=turn 死,留僵尸 checkpoint;无跨进程"恢复进行中 turn"API(本方案不补,靠 heal+retry 语义) |
| 启动对账 | server.py lifespan:委托改判、孤儿 subagent/workflow/schedule 清理 | 新进程启动时的"清账"已有;把更新完成通知挂在这里 |

### 2.3 前端(`apps/web/src/`)

| 环节 | 现状 | 对自更新的意义 |
|---|---|---|
| 活跃会话恢复 | `localStorage["ginno-last-session"]`(store.tsx:74),AppShell.tsx:135-150 启动恢复;localStorage 按 bundle identity 存在 WKWebView 数据目录,**换 bundle 内容(不改 bundle id)不丢** | 新 app 打开即回到原会话,零改动 |
| 消息重建 | 首次进会话 `getSessionHistory` 全量拉 checkpoint 重建气泡(engine :680-725),`last_error` 回放错误卡 | 对前端,**app 重启 ≡ 一次长断线 + 页面刷新**,现有链路天然覆盖 |
| 断线重连 | onclose 3s 重试(engine :587-599),重连后 `turn_state` 探针 + `reconcileTurnFromHistory` 全量对账 | 更新窗口内 webview 会经历 close→(新 origin 同址)→重连;无需新代码,但 P1 加一条"检测到更新中"的 UI 态(免得显示吓人的连接失败) |
| 通知 | notice center(在建,docs/notice-center-design.md) | "已从 vA 更新到 vB,会话已恢复"的落点 |

### 2.4 缺口清单(方案要补的东西)

1. 外部 relauncher(等旧进程死 → 换 bundle → 验签 → `open` → 健康检查 → 失败回滚)。
2. 壳的 `self_update` / `restart_app` 能力 + 优雅退出(SIGTERM 宽限)。
3. runtime 侧 agent 工具 `self_update`(含 quiesce:更新前处理在跑的 turn)。
4. 跨重启的"更新状态 + 版本指纹"(`~/.ginno/update-state.json` + bundle 内 VERSION)。
5. 僵尸 checkpoint heal 提前触发 + 更新完成通知/续聊衔接。

---

## 3. 总体架构:三段式,互不重叠

```
┌─ 段1 BUILD(旧 app 还活着,允许)──────────────────────────┐
│  agent: 改代码 → make app(FORCE=1,见 §7.1)               │
│  产物: apps/desktop/target/release/bundle/macos/Ginno.app   │
│  + bundle 内 resources/VERSION(git sha + 构建时间)         │
└──────────────────────────────────────────────────────┬────┘
                                                        ▼
┌─ 段2 SWAP+RELAUNCH(外部 relauncher 独占窗口)──────────┐
│  ① 旧壳调用 self_update → 写 update-state.json(armed)     │
│  ② spawn detached relauncher(/bin/zsh 脚本,日志独立)      │
│  ③ relauncher 等待 quiesce 完成(轮询 sidecar turn 状态)    │
│  ④ 旧壳 app.exit()(Exit 钩子 SIGTERM→5s→SIGKILL 杀 sidecar)│
│  ⑤ relauncher 轮询旧壳 PID 死透                             │
│  ⑥ 验签新 bundle(codesign --verify 同 DR)→ 不符即中止      │
│  ⑦ 换 bundle:running.app → .old-<ts>;新 → running 路径     │
│     (若 running 路径 == target 路径,跳过拷贝)              │
│  ⑧ open 新 app;轮询 /api/health(最多 90s)                │
│  ⑨ 成功 → update-state=verified,删 .old;失败 → 回滚重开旧  │
└──────────────────────────────────────────────────────┬────┘
                                                        ▼
┌─ 段3 RESTORE(新 app 自动完成)────────────────────────┐
│  壳启动(已有逻辑)→ sidecar 起来 → 前端回同源 origin        │
│  → localStorage 恢复活跃会话 → /history 重建 → WS 重连      │
│  → 启动对账时读 update-state:verified → 通知"更新完成"      │
│  → (可选)auto_continue:该会话自动注入续聊 turn            │
└──────────────────────────────────────────────────────┘
```

**分工原则**:build 归 agent/Makefile(现状不动);swap+relaunch 归 relauncher(唯一在空档期活着的进程);restore 归现有启动链路(几乎零新代码)。三段的唯一通信媒介是 `update-state.json` + 文件系统。

---

## 4. 组件设计

### 4.1 relauncher(新文件,`apps/desktop/resources/ginno-relaunch.sh` 或独立小工具)

一个 zsh 脚本就够了(不需要 Rust 二进制;它不依赖 app 任何代码,这正是它存在的意义):

```
ginno-relaunch.sh <shell_pid> <new_bundle> <current_bundle> <state_file>
```

行为(每步写一行到 `~/.ginno/logs/updater.log`,状态推进写 state_file):

| 步 | 动作 | 超时/失败处理 |
|---|---|---|
| 1 | 等 quiesce:轮询 `POST http://127.0.0.1:8787/api/sessions/{sid}/turn_state`(或 §4.3 的 `/api/system/update-quiesce`)直到无 running turn | 60s 超时 → 调既有 `POST /api/sessions/{id}/stop` 四形态停掉,再等 10s |
| 2 | 等旧壳死:`kill -0 <shell_pid>` 轮询(壳自己 `app.exit()`) | 15s 超时 → `kill -TERM`,再 10s → `kill -9`(兜底,正常不走这) |
| 3 | 验签新 bundle:`codesign --verify --deep` + `codesign -d -r` 比对 designated requirement 与旧 bundle 一致 | 失败 → state=aborted,旧 app 原样,**拉起旧 app** 并通知 |
| 4 | 换 bundle(仅当 `current != new`):`mv current current.old-<ts>`;`ditto new current`(跨卷安全拷贝保签名) | ditto 失败 → `mv` 回滚,拉起旧 app |
| 5 | `open <current_bundle>`(不带 -n;single-instance 插件兜底) | — |
| 6 | 轮询 `http://127.0.0.1:8787/api/health` 最多 90s(新版含 version 字段,§4.5) | 成功 → state=verified,`rm -rf *.old-<ts>`;失败 → state=rollback:换回 .old,重开旧 app |
| 7 | 退出(无论成败,进程生命周期 < 3 分钟) | — |

要点:
- **detached spawn**:壳用 `Command::new("/bin/zsh").arg(script).args(...)` + `process_group(0)`(新进程组,不随壳死)+ stdio 指向 updater.log。spawn 之后壳与它**不再通信**,一切靠 state_file——壳死了它也能跑完。
- **为什么轮询 PID 而不是 exec**:Tauri 壳不能 exec 替换自身(webview 状态);`app.exit()` 走正常退出路径(CloseRequested/Exit 钩子、localStorage 落盘)最干净。
- `.old-<ts>` 保留到 health 验证通过才删;回滚就是反向 `mv`。

### 4.2 壳侧:`self_update` / `restart_app` 命令 + 优雅退出

新增 Tauri command(`lib.rs`,与 restart_sidecar 并列):

```
self_update(opts { new_bundle?: string })   // 有 new_bundle=换版本;无=restart_app(S2 场景)
restart_app()
```

`self_update` 流程:
1. 取 `RestartLock` 同款的 `UpdateLock` 防重入(state_file 已 armed 则拒绝)。
2. 确定 `current_bundle` = 当前可执行文件向上找 `.app`(macOS `NSBundle`/`current_exe` 皆可);`new_bundle` 缺省 = `<repo>/apps/desktop/target/release/bundle/macos/Ginno.app`(agent 自更新主场景的固定产物路径,写进 env `GINNO_BUILD_BUNDLE` 由构建注入亦可)。
3. 写 `~/.ginno/update-state.json`(§4.5)→ spawn relauncher(detached)→ **延迟 2s** `app.exit(0)`(给 webview 时间把 localStorage/notice 状态落盘,给 runtime 时间收到 quiesce 通知)。

优雅退出改造(P1,独立有价值):`RunEvent::Exit` 钩子里 `child.kill()` 改为 **SIGTERM → 5s 轮询 → SIGKILL**,让 uvicorn lifespan 的清理(MCP 断连、workflow 标 interrupted、scheduler 停)真正执行——自更新把这条从"理论路径"变成"每次更新都走的路径"。

配套小项:
- **single-instance 插件**(P1):防 relauncher `open` 与残存实例双开;回调里聚焦已有窗口。
- **broker stale socket**:`start_mod_broker` 前 unlink 已存在的 `.sock`(现在旧实例留下的 socket 文件是否清理需验证,P0 顺带确认)。
- env 注入增量:`GINNO_APP_PATH`、`GINNO_SHELL_PID` 注入 sidecar,让 runtime 的 `self_update` 工具不需要反向找壳。

### 4.3 runtime 侧:agent 工具 `self_update` + quiesce

新增 agent 工具(工具定义与既有工具同框架):

**`self_update(wait_for_turns: bool = true)`**:
1. 读 `GINNO_APP_PATH`/`GINNO_SHELL_PID`(env,§4.2);缺省 → 报错"非桌面环境,不支持"(dev/远端跑不了这个)。
2. **quiesce 自己**:本会话当前 turn 就是调这个工具的 turn → 工具直接返回文本"更新已安排,大约 30 秒后重启,我会回来继续",让 turn 正常走完(agent 的最后一条消息落 checkpoint)。
3. 其它会话有 running turn:`wait_for_turns=true` 时注册回调等它们结束(上限 60s);超时或 `false` → 逐个调既有 stop 逻辑(`_stop_parked_turn` / `_TURN_STOP`),产生 turn.stopped 事件。
4. spawn relauncher(runtime 直接 spawn 即可,它和壳是同级进程;脚本路径从 env `GINNO_RELAUNCH_PATH` 读,由壳注入——**工具不依赖壳的 IPC,只依赖 env+文件**,保持两侧解耦)。
5. 返回给 agent:"relauncher 已启动,本 turn 结束后应用将重启;重启后你会在同一会话里继续"。

新增小端点 `GET /api/system/update-state`(读 state_file)+ 启动对账扩展(server.py lifespan):发现 `verified` 且 `session_id` 非空 → 清掉状态前向该会话广播一条 `system.update_completed {from,to}` 事件(前端 notice 显示"已从 vA 更新到 vB";agent 会话里渲染成系统消息,完成上下文闭环——agent 醒来能"看见"更新成功了)。

**僵尸 heal 提前**(P1,与 8fd0759 同机制):把 `/compact` 路径的僵尸判定(registry.py:482-515)提为 `_ensure_session` WS 连接时执行——被更新杀掉的 mid-turn 留下 `next` 非空 checkpoint,新会话连上来先 heal,用户看到的是"上次回合已中断"错误卡(可 retry),而不是等下次 /compact 才修。

### 4.4 前端:基本零改动,三个增强

1. **更新中 UI 态**(P1):WS 断线重连时若 `/api/system/update-state` 返回 armed → 显示"正在更新 Ginno,即将回来"的过渡页/横幅,替代吓人的"连接失败";`openSocket` 的 3s 重试循环天然会在新 sidecar 起来后自动接上。
2. **更新完成 notice**:消费 `system.update_completed` 事件 → notice center 右上角提示(与 docs/notice-center-design.md 落点一致)。
3. **auto_continue**(P2,默认关):update-state 里带 `auto_continue: true` 时,新进程启动对账后由 runtime 向该会话注入一条用户不可见 system turn(如 `[system] 应用已从 vA 更新到 vB,请继续之前的任务`)——这才真正实现"agent 自己更新自己后接着干活"。风险:若更新本身失败回滚,注入会造成循环;必须 verified-only + 单次守卫(state_file 里 `continued: true` 幂等位)。

### 4.5 `~/.ginno/update-state.json` 状态机

跨重启的唯一事实源,relauncher/壳/runtime 三方读写,写法沿用 tmp+rename 原子写:

```json
{
  "phase": "armed | swapped | verified | aborted | rolled_back",
  "from_version": "0.1.0+8fd0759",      // 旧 bundle VERSION
  "to_version": "0.1.0+73db076",        // 新 bundle VERSION
  "session_id": "…",                     // 发起更新的会话(恢复+通知目标)
  "auto_continue": false,
  "old_bundle": "/…/Ginno.app.old-<ts>",// 回滚地址
  "new_bundle": "/…/target/…/Ginno.app",
  "error": null, "ts": "…", "continued": false
}
```

状态迁移:`(无)→armed(relauncher 起来)→swapped(open 前)→verified(health 过)`;任一步失败 → `aborted`(没动过 bundle)或 `rolled_back`(动过,已换回)。新 app 启动对账读它决定通知/续聊/清账。

### 4.6 版本指纹(VERSION)

- `make app` 构建时写 `<bundle>/Contents/Resources/VERSION`:`{version}+{git short sha}+{build ts}`(Makefile 一行;tauri.conf.json 的 0.1.0 保持不动,不引入 bump 流程)。
- `/api/health` 响应加 `version` 字段(sidecar 启动时读自己 bundle 的 VERSION,dev 读 repo git)。前端"关于"/设置页、relauncher 验证、update-state 的 from/to 全部以此为准。

---

## 5. 关键时序(S1:agent 自更新)

```
agent 会话(turn N)
  ├─ agent: bash(make app FORCE=1)          # 段1;~2-5 分钟
  ├─ agent: self_update()                    # 工具返回"已安排",agent 输出告别语,
  │                                          # turn N 正常完成并落 checkpoint ★
  │   (runtime: quiesce 其它会话 → 写 state=armed
  │    → spawn relauncher → return)
  ├─ relauncher: 等 turn_state idle(通常 0s,★已完成)→ 等 shell_pid 死
  ├─ 壳: self_update 后 2s → app.exit() → Exit 钩子 SIGTERM sidecar(5s→KILL)
  ├─ relauncher: 验签 → (若需要)换 bundle → state=swapped → open 新 app
  ├─ 新壳: spawn 新 sidecar → splash → 前端回来
  │   新 sidecar lifespan: 读 state → 僵尸对账 → 广播 system.update_completed
  ├─ 前端: localStorage 回到 session → /history 重建(turn N 的告别语可见)
  │   → WS 重连 → notice"已从 vA 更新到 vB"
  └─ relauncher: health 90s 内 OK → state=verified → 清 .old → 退出
      (失败 → rolled_back + 换回旧 bundle + 重开旧 app)
用户/agent: 在同一会话输入框继续对话 —— turn N+1 在新版本上执行
```

---

## 6. 安全与回滚

| 风险 | 对策 |
|---|---|
| 新 bundle 未签名/签名不符(linker-signed → 白屏) | relauncher 步 3 强制验签,失败不换、拉起旧 app;与 Makefile:61-66 同一 DR 口径 |
| 新版本起来但坏(health 不过/crash loop) | health 90s 轮询;`rolled_back` 换回 `.old-<ts>` 重开;`.old` 删除只发生在 verified 之后 |
| 新版本能起但运行中崩(health 过了才坏) | 兜底手动:用户用 `.old` bundle 或 `make app` 重建;P2 可加"启动后 10 分钟内 N 次退出"crash-loop 检测自动回滚(记录在 §9 开放问题) |
| 更新中杀 turn 造成 checkpoint 损坏 | checkpoint/index 已是原子写;mid-turn 死留僵尸,由提前到连接时的 heal(§4.3)修复;interrupt 态天然跨重启存活 |
| 双壳并存 | relauncher 等 PID 死透才 open + single-instance 插件双层防线 |
| relauncher 自身 bug(外部脚本无 app 代码) | 脚本 <100 行、只依赖 /bin/zsh + codesign + open + curl;任何一步失败都走"拉起旧 app"分支(最坏情况 = 没更新成功) |
| state_file 与实际状态漂移 | 所有 phase 迁移单写者:armed/swapped/verified 只由 relauncher 写;runtime 只读 + 写 continued 位;armed 由发起方(runtime/壳)写并 flock |

---

## 7. 与已知约束对齐

### 7.1 zlib 故障(CLAUDE.md 已知故障 1)与 `make app` 防呆

zlib 故障根因是**运行中的 sidecar 的 bundle 被替换**。分两种情况:

- **运行 app 在 `/Applications`(或任何 ≠ target 路径)**:`make app` 写 target/,不碰运行中 bundle → 理论安全;但防呆(Makefile:107-131)不区分路径一律拒绝 → 给 make 增加 `GINNO_APP_PATH` 感知:防呆脚本读运行中 app 的真实路径,≠ target 产物路径就放行(仍拒绝同路径)。**本方案默认走这条**。
- **运行 app 就在 target/release/bundle/macos/**(开发常态):build 会原地替换运行中 bundle → 这正是防呆要拦的。自更新此场景需 `FORCE=1`,且规则升级为:**FORCE=1 build 后必须立即 self_update**(把暴露窗口从"人工想起来再重启"缩到分钟级),quiesce 已保证期间无 lazy-import 密集操作(compaction/microcompact 只在有 turn 时跑)。文档写明,不做更强保证。

### 7.2 会话/数据不受影响的明示

`~/.ginno/`(settings、sessions、usage、MEMORY、browser-extension)全部在 app bundle 外,换 bundle 零影响;broker socket/token 在 app_local_data_dir(同样 bundle 外),新壳重建,注意 §4.2 的 stale socket 清理。

### 7.3 mods 兼容层

broker 是壳进程内 path-dep,随壳一起更新;mod-runner.mjs 在 resources 里随 bundle 换。sidecar 重启 fallback 梯子不变,无新增耦合。

---

## 8. 分阶段落地

### P0 — 重启闭环(不含换 bundle):"杀自己+回来"先跑通
1. 壳:`restart_app()` 命令(Debug 菜单"重启应用")+ Exit 钩子 SIGTERM 化。
2. relauncher 脚本(只做:等死→open→health)。
3. 端到端验收:发一条消息 → 菜单重启 → 回到同会话,历史完整,能继续对话;mid-turn 重启 → 错误卡 + retry 可用。
> P0 单独有价值(排障"重启应用"菜单),且把最大不确定性(退出/恢复链路)先行验证。

### P1 — 自更新完整链(S1 场景)
1. VERSION 指纹 + /api/health.version。
2. relauncher 完整版(验签/换 bundle/回滚)+ updater.log。
3. 壳 `self_update` 命令 + env 注入(APP_PATH/SHELL_PID/RELAUNCH_PATH)。
4. runtime `self_update` 工具 + quiesce + `/api/system/update-state` + 启动对账广播 `system.update_completed`。
5. 僵尸 heal 提前到 WS 连接;前端"更新中"过渡态 + 完成通知。
6. `make app` 防呆的路径感知(§7.1)+ single-instance 插件。
> 验收:agent 在会话里完成 改码→build→self_update→回来汇报 全程;人为坏掉新 bundle(改二进制)验证回滚。

### P2 — 打磨
1. `auto_continue`(verified-only + 幂等守卫)。
2. crash-loop 自动回滚(启动后短窗多次退出 → 回滚)。
3. `make app` 期间进度上报给 agent(build 日志 tail 到工具结果)。
4. (可选)远端 feed:建 latest.json + 下载到 staging,复用同一 relauncher——发布服务器存在时才做。

---

## 9. 风险与开放问题(待评审拍板)

| # | 问题 | 倾向 |
|---|---|---|
| Q1 | relauncher 用 zsh 脚本还是编译进壳的小 Rust 二进制(target/bin)? | 脚本(零构建耦合、可读可改);若担心 `/bin/zsh` 策略限制再升级 |
| Q2 | `make app` 防呆改造走"路径感知放行"还是统一 FORCE=1 + 立即重启纪律? | 路径感知(机器可靠 > 人守纪律);FORCE 纪律作为 target 路径场景的补充说明 |
| Q3 | 前端"更新中"过渡态做整页 splash 还是顶部横幅+禁用输入? | 横幅(与 notice center 形态一致,实现小) |
| Q4 | `system.update_completed` 在 agent 会话里渲染为系统消息还是普通 notice? | 两者都要:notice 给用户,会话内系统消息给 agent(它是 agent 上下文闭环的载体) |
| Q5 | quiesce 等其它会话 turn 的上限 60s 是否合适(长 workflow run 怎么办)? | workflow run 不等(stop 会标 interrupted,可 resume,语义已有);只等 chat turn |
| Q6 | dev 模式(`pnpm dev`)是否支持 self_update? | 否,工具直接报错指引用户手动重启;dev 本来就没有 bundle 替换问题 |

---

## 附:改动面清单(预估)

| 层 | 文件 | 类型 |
|---|---|---|
| 壳 | `apps/desktop/src/lib.rs`(self_update/restart_app 命令、Exit SIGTERM、env 注入、single-instance) | 改 |
| 壳 | `apps/desktop/resources/ginno-relaunch.sh` | 新 |
| 构建 | `Makefile`(VERSION 写入、防呆路径感知、relauncher 拷进 resources) | 改 |
| runtime | `ginno_runtime/api/system.py`(update-state 端点+启动对账广播) | 新/改 |
| runtime | `self_update` 工具注册(tools 框架内)+ quiesce | 新 |
| runtime | 僵尸 heal 提前(sessions/_ensure_session 挂钩) | 改 |
| runtime | `server.py` health.version | 改 |
| web | 更新中横幅 + update_completed 消费(engine/notice) | 改 |
