# Runtime 热切换设计（开发自闭环：改代码 → 重建 → 换 bundle → 重启 → 验证 → 回滚）

> 状态：**P0+P1 已实施**（2026-11-04，见 CHANGELOG Unreleased）；P2（与 self-update 合流）未做。本文设计「只换 runtime sidecar」的开发热更新；整 app（壳+web+runtime 一起换）的自更新见 `self-update-design.md`（两方案共享回滚骨架，§9 讨论合流）。
> 相关：`CLAUDE.md`（zlib 已知故障 = 本方案要根治的头号约束）、`apps/desktop/src/sidecar.rs`、`docs/p3-packaging-notes.md`。

---

## 0. TL;DR

- **一句话**：新 bundle 构建到 `~/.ginno/dev-override/ginno-runtime.v{sha}-{ts}/`，**symlink 原子切换** + 壳的发现梯子（override 优先 → app 内置兜底）；重启复用现成的 `restart_sidecar`（它每次调用都重新解析路径）；健康验证带**构建指纹**（git sha），失败自动把 symlink 切回旧目录再重启——最坏情况回退 app 内置 bundle，**app 永远打得开**。
- **为什么是壳做 supervisor**：`restart_sidecar`（sidecar.rs:316）已验证「壳活着换后端」（kill child → wait → 重新 spawn → splash → reload），且 broker 故意活过 sidecar 重启；外部 launchd 看门狗对这个「进程内亲子关系 + webview 导航」的协调毫无增益。
- **为什么不动 app bundle 内的 resources/runtime**：改 .app Contents 会破坏 code signature 的 sealed resources → 下次启动白屏；且用户装新 dmg 会冲掉改动。override 放 `~/.ginno/`（用户数据目录）零签名约束。
- **为什么必须「换完立即重启」**：PyInstaller 懒加载按**路径**重开归档——旧进程 + 新 bundle 混跑 = TOC 偏移读垃圾 = zlib error（CLAUDE.md 已知故障 1）。所以顺序是「停旧 → 切 symlink → 起新」，绝不留混搭窗口。

---

## 1. 目标与非目标

**目标**：开发 agent 在会话里完成一次 runtime 改动后，一条命令（`make dev-swap` 或 agent 工具 `/dev-swap`）完成：测试 → PyInstaller 重建 → 安全替换 → sidecar 重启加载新码 → 健康检查 → 失败自动回滚。**壳与前端不退出不重装**。

**非目标**：整 app 自更新（壳/web 一起换，归 `self-update-design.md`）；远端版本 feed；Windows/Linux。

## 2. 现状盘点（证据）

| 环节 | 现状 | 对本方案的意义 |
|---|---|---|
| 壳 spawn sidecar | `spawn_sidecar`（sidecar.rs:249）：release 硬编码 `resource_dir()/resources/runtime/ginno-runtime`，**每次调用重新解析**（无缓存）；dev 走 `uv run uvicorn` | 加发现梯子的唯一改动点；无路径缓存 → 切 symlink 后重启即生效 |
| 壳重启 sidecar | `restart_sidecar`（sidecar.rs:316）：kill child → wait → 再 spawn → splash → webview 重载；`RestartLock` 防并发（sidecar.rs:30） | **supervisor 语义现成**，本方案只是给它加「换路径 + 验证 + 回滚」的编排 |
| 壳自动拉起 | **没有**（child 崩溃不 respawn；restart 是菜单驱动） | P1 补 child-exit 监听 |
| 壳↔前端事件 | `app.listen_any("ginno:notify")`（lib.rs）等 Tauri 事件 + `#[tauri::command]` | 「agent 发起 → 前端中转 → 壳执行」的通道现成 |
| 健康检查 | `/api/health`（server.py:566）返回 `{ok, version, …}`；**version 硬编码 "0.1.0"** | 已可用；P0 把 version 换成构建指纹（git sha）才能验证「新 bundle 真的起来了」 |
| 打包 | `make runtime`（Makefile:211）：web → PyInstaller onedir → `packages/runtime/dist/ginno-runtime/`，内容 hash 增量 | 产物落点固定；`make runtime` 跑着 app 也能执行（不碰运行中 bundle） |
| zlib 教训 | 运行中替换 bundle → 旧进程懒加载读新归档 → 解压炸，**只能重启恢复** | 决定了「先停旧再切路径」的顺序；也决定了绝不能原地 ditto 到运行中目录 |
| 数据一致性 | checkpoint jsonl 追加 + 原子写；被杀 turn 留僵尸 checkpoint，heal 已有（触发滞后） | 重启安全性已被 `restart_sidecar` 与 e2e 反复验证 |

## 3. 设计

### 3.1 轮换布局（`~/.ginno/dev-override/`）

```
~/.ginno/dev-override/
├── ginno-runtime.v{gitsha8}-{epoch}/   # 每次 make dev-swap 产出一个目录（gen-suffix）
├── ginno-runtime -> ginno-runtime.v…/  # symlink：唯一切换点，rename(2) 原子替换
└── state.json                          # swap 状态机（见 3.3），原子写（tmp+rename）
```

- **目录不可原子换名**（rename 到非空目录会失败），**symlink 可以**：`ln -s <new> <tmp> && mv -f <tmp> ginno-runtime`（同目录 rename 原子）。
- 旧目录保留到新版本 verified 后，清到**最近 2 份**（每份 ~500MB，防磁盘膨胀）；回滚目标永远存在。

### 3.2 壳侧发现梯子（spawn_sidecar 唯一改动）

release 分支改为：

```
1. ~/.ginno/dev-override/ginno-runtime 存在且可执行 → 用它（symlink 解引用）
2. 否则 → app 内置 resource_dir()/resources/runtime/ginno-runtime（现状不变）
```

第 2 级是「绝不打不开」的最终兜底：symlink 坏了/被清理工具移走/override 整个删掉，壳都回落到出厂 bundle。

### 3.3 状态机（state.json，CLI 与壳的唯一通信媒介）

`building → pending → restarting → verified | rolled_back`

```json
{"state": "pending", "new": "ginno-runtime.vabc12345-1770000000",
 "old": "ginno-runtime.vdef67890-1769000000", "sha": "abc12345",
 "created": 1770000000, "requested_by": "agent|cli"}
```

单写者：CLI 用 mkstemp+rename 原子写；壳只读 + 写终态（verified/rolled_back）。

### 3.4 流程（三方分工）

**CLI（`make dev-swap`，新增 Makefile target + scripts/dev_swap.sh）**——build 段：

1. 前置：git 干净度检查（记录 sha）、工具链检查（uv/node）；可选 `pytest -q -m unit`（`SKIP_TESTS=1` 跳过）。
2. `make runtime`（增量，~4 分钟；跑着 app 也安全）。
3. `ditto dist/ginno-runtime → dev-override/ginno-runtime.v{sha}-{ts}/`（跨卷安全、保可执行位；绝不动运行中路径）。
4. 原子切 symlink（3.1）→ 写 state=pending → 输出「请触发 finish_dev_swap」。

**壳（新增 `#[tauri::command] finish_dev_swap`）**——swap+restart 段（唯一有资格杀/拉 sidecar 的一方）：

1. 读 state.json：非 pending → no-op 返回。
2. `restart_sidecar()`（复用：kill → wait → spawn【此时梯子已解析到新 symlink】→ splash → reload）。
3. 轮询 `http://127.0.0.1:8787/api/health` 最多 90s，**验证 version 指纹 == pending.sha**（只 ok 不够——旧 bundle 也能 ok）。
4. 成功 → state=verified，清旧目录（保留 2 份），emit `dev-swap:result {ok:true}` 给前端。
5. 失败 → symlink 原子切回 old 目录 → 再 `restart_sidecar()` → 验证旧指纹 → state=rolled_back，emit `dev-swap:result {ok:false, rolledBack:true}`。**双重失败兜底**：连旧目录都起不来 → 删 symlink → restart（梯子落回 app 内置）→ 报错明示。

**runtime（agent 触发面）**——P0 用 builtin command `/dev-swap`（commands/registry.py 注册）：调 `make dev-swap`（subprocess）→ 成功后向 WS 广播 `runtime.dev_swap_pending` 事件 → **前端收到事件即 `invoke('finish_dev_swap')`**（前端 ~20 行）。发起 turn 会在重启中被杀（发起者即旧进程）——zombie checkpoint 由现有 heal 兜底，P1 做优雅 quiesce。

### 3.5 构建指纹（P0 必做，否则验证无意义）

- `Makefile` runtime target 构建时写 `build-info.json`（git sha + 时间）→ PyInstaller `--add-data` 打进 bundle。
- `server.py` `/api/health` 的 `version` 改读 build-info（缺文件回退 `ginno_runtime.__version__`）。
- 壳验证、UI 的 dev 版标识（TopBar 黄条「dev-runtime {sha8}」，防「忘了自己在跑 dev 码」）都吃这个指纹。

## 4. 回答委派的三个设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| a) 重启发起方 | **壳 supervisor + IPC 命令**（复用 restart_sidecar + 新 command） | 壳独占 child handle 与 webview 导航协调；sidecar 自杀不可靠（壳无 respawn，自杀后没人拉起）；launchd 看门狗对进程内亲子关系零增益且重 |
| b) 替换原子性 | **v{sha}-{ts} 目录 + symlink 原子切** | 目录 rename 非原子（非空目标）；symlink 的 rename(2) 原子；gen-suffix 保留旧版天然支撑回滚 |
| c) 回滚保护 | verified 前不删旧目录；health 失败→切回旧→再验；双失败→删 symlink 落回 app 内置 | 「app 永远打得开」由三层梯子保证（override → 旧版 → 出厂） |

## 5. 分期

### P0 最小闭环（1-2 天，文件级）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `apps/desktop/src/sidecar.rs` | spawn_sidecar release 分支加 dev-override 梯子（~15 行） |
| 2 | `apps/desktop/src/lib.rs` | 新 `finish_dev_swap` command（读 state → restart → 验指纹 → 回滚），注册 invoke_handler |
| 3 | `Makefile` + `scripts/dev_swap.sh` | `dev-swap` target：检查→make runtime→ditto→symlink 切→写 state |
| 4 | `packages/runtime/…/server.py` + Makefile | build-info.json 注入 + health.version 读指纹 |
| 5 | `apps/web/src/…`（事件监听处） | 收 `runtime.dev_swap_pending` → invoke finish_dev_swap → toast 结果 |
| 6 | `packages/runtime/…/commands/registry.py` | `/dev-swap` builtin（跑脚本+发事件；CLI 路径 `make dev-swap` 同样可用） |
| 7 | `CLAUDE.md` / `CONTRIBUTING.md` | 新流程与排障（zlib 语义变化：热切换后无需手工重启） |

### P1（体验与稳健）

- 壳 child-exit 监听（崩溃自动拉起，重生周期内读最新 symlink）。
- **blue-green 预检**：新 bundle 先在独立端口 + temp home 探活（复用 `scripts/screenshot_ui.py` 的 spawn/wait 模式），起不来不进入切换。
- 优雅 quiesce：发起方 turn 先停在 interrupt / 空闲点再换，消灭僵尸 checkpoint。
- Debug 菜单手动「热切换 runtime」入口 + TopBar dev 版黄条。

### P2（合流）

- 与 `self-update-design.md` 合流：整 app 更新时 runtime override 自动失效清理（app 内置版本 > override 版本则提示）；relauncher 复用本文 state.json/回滚骨架。

## 6. 关键风险

| # | 风险 | 缓解 |
|---|---|---|
| 1 | 发起 turn 被重启腰斩 → 僵尸 checkpoint | heal 已有；P1 quiesce；state.json 记录发起 session，重启后 notice 告知 |
| 2 | dev-override 忘删 → 生产心智混淆 | TopBar dev 指纹黄条；`make dev-swap-clean` 一键回出厂；state.json `requested_by` 留痕 |
| 3 | 磁盘膨胀（每份 ~500MB） | verified 后清到保留 2 份 |
| 4 | symlink 被清理工具/TimeMachine 移走 | 梯子第 2 级回退 app 内置；壳 spawn 失败时自愈（探测→删坏 symlink→重试） |
| 5 | 并发 swap（agent 连发两次） | RestartLock 已有；state.json 原子写 + 壳侧 state 非 pending 即 no-op |
| 6 | 构建机 ≠ 运行机（无工具链） | dev-swap 前置检查 uv/node/pyinstaller，缺则明错 |
| 7 | 混搭窗口（旧进程+新 bundle） | 顺序强制「停旧→切→起新」；symlink 在 restart 内部解析，无窗口 |
| 8 | web 前端与 runtime 版本错配（前端还是旧的） | 本设计明确只换 runtime；前端不兼容新 API 属正常开发迭代风险，e2e 拦 |
