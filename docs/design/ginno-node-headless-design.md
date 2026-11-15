# ginno node — 无桌面机 headless 形态设计补充

> **本文是 `a2a-connector-design.md` 的补充小节**，填补该文档对 `ginno node`
> 只给了「吸收自 BYOM、复用 wheel 零新包、`--join <地址>`」三句话、
> 未定进程模型/凭据落点/执行链/产物回报/离线判定的留白。
> 定案以主设计文档为准，本文只补它没说的部分，**不推翻任何已定案**。
>
> 状态：设计补充（施工前落盘）｜日期：2026-11-15｜关联验收：主文档 #38（`ginno node --join`）、#31/#32/#33（trust 强制）、#36（peer_offline）

## 0. 一句话

`ginno node` = **无 GUI 的 runtime 进程 + A2A 入站 server**，
让一台无桌面机（服务器/NAS/Mac mini）作为**标准 A2A 对端**被主 Ginno 派活与回报。

## 1. 关键架构裁定：push 模式，不是反向 WS

主设计 §11.3.2 **明确不吸收** BYOM 的 pull 模式反向 WS：

> 「会破坏 A2A 标准互操作（规范是 HTTP+JSON-RPC+SSE）。NAT 场景改用
> Tailscale / SSH -R 隧道。**不为省一层组网而偏离协议**」

**故 ginno node 必须是 push 模式**：node 监听 A2A 端口，主 Ginno 正向连。
node **不主动向桌面端建立任何连接**——它就是一个标准 A2A server，
与「别家 agent」这类对端在协议上完全同构。

**推论（重要）**：`ginno node` 的能力上限 = **A2A 入站执行层的能力上限**。
主设计把入站真跑 agent 列为 P1（`server.py:31` 当前只返回 `submitted` 占位），
所以 **node 要能真干活，必须先把入站执行层做出来**。
这不是 node 的私有需求——桌面端入站同样需要它，**一次实现两处受益**。

## 2. 留白填补（六项定案）

### 2.1 进程模型：argparse 子命令 + 进程内 uvicorn

主设计只说「runtime 新增子命令，复用 wheel 零新包」，未定进程形态。

**定案**：照 `cli.py` 既有 `checkpoints` 子命令范式（`cli.py:653`）加分流：

```
ginno node [--join <能力地址>] [--listen HOST:PORT] [--name NAME]
           [--root DIR] [--home DIR] [--tls-cert P --tls-key P]
```

- **无 GUI 依赖**：不 import 任何 Tauri/前端模块；只起 runtime + A2A listener
- 起服复用 `a2a/inbound.py` 的 `ensure_inbound_listener`（已幂等、已预留 TLS 参数）
- 进程内跑 uvicorn，阻塞至 SIGTERM/SIGINT；**不 fork、不 daemonize**
  （交给 systemd/launchd 管，照 BYOM 原稿 P1 的模板思路，但模板本身不在本期）
- 长跑隔离：测试与文档里一律 `setsid` + `nohup`（仓库既有纪律：bg 任务 2h 硬上限、
  sidecar 重启看门狗杀进程组）

**`--join` 与 `--listen` 的关系**（主设计未说清）：
- `--listen` 是**服务端形态**（node 被连），本期主路径
- `--join <地址>` 在主设计里是「node 接入桌面端」，但 push 模式下 node 不需要
  主动连桌面端。**故 `--join` 在本期的语义收窄为「导入对端能力地址以建立互信」**
  （可选，用于 node 也要反向派活给别人的场景），**不是建立反向连接**。
  这是对主设计措辞的**澄清而非偏离**——主设计的 `--join` 本就写「两种模式都支持」
  指的是长期/一次性地址两种，不是 pull/push 两种。

### 2.2 凭据与配置落点：复用现有 settings 约定，不新造格式

BYOM 原稿写 `~/.ginno-node/token` + `~/.ginno-node/config.json`（独立格式）。
主设计写 `settings.connectors.<id>`。任务约束要求「遵守现有 settings 约定」。

**定案**：
- node 用 **`GINNO_HOME` 隔离**，默认 `~/.ginno-node`（避免与桌面端 `~/.ginno` 互踩）
- 落盘一律走现有 `paths.py` + `settings` 机制，**不新造凭据文件格式**
- token **只存 SHA-256 hash**（复用 `a2a/auth.py` 的 `TokenStore`，主设计 #41）
- 能力地址的明文 token **只在签发时打印一次**到 stdout（供用户复制到桌面端），
  之后不可回读——与 `api/config.py` SEC-01 掩码纪律一致
- 私钥/证书走 `a2a/identity.py`（已实现 0600 权限、mkstemp、rename 前 chmod）

### 2.3 任务执行：复用 `ginno exec` 的 turn 链，会话映射照渠道范式

主设计说「会话映射照 `channels/base.py ensure_channel_session` 范式」，
但未定执行链本身。

**定案**：
- 执行链复用 `ginno exec` 的 **`create_session → _run_stream`（ws=None）**
  ——`docs/headless.md` 已明确这是「与桌面版共享同一条 turn 执行链，完全进程内」
- 会话映射照 `ensure_channel_session`：`type="a2a"`、独立 slug、不挂用户目录
- **执行层做成共享模块**（`a2a/inbound_exec.py`），桌面端入站与 node 共用，
  避免两套实现漂移（仓库既有教训：同抽象双实现行为不一致是最阴的坑）
- 任务状态机走主设计 §3 的 TaskState 9 值；**终态不可重启**
- 超时：`start_new_session` + `killpg`（SIGTERM→2s→SIGKILL），主设计 #40

### 2.4 trust / caps 必须在执行侧强制（不能只存储）

**现状缺口**：`peers.py` 有 trust/caps 字段，但**派发与执行侧都不复核**
（主设计 #31/#32/#33 属 P1）。P0 无执行层故无从强制，**node 有执行层就必须强制**。

**定案**：
- 执行前按 trust 过滤工具面：`readonly` → 拒一切写工具；`normal` → 允许 exec_root 内写；
  `full` → 仍**拒改配置**（主设计 #33：full ≠ 可改配置）
- `exec_root` 边界 **fail-loud**：越界直接拒绝，**不做静默修正**（主设计 #32）
- caps 阶梯校验：L0 观测 / L1 只读执行 / L2 文件 / L3 turn，**越级拒绝**
- **readonly 前缀匹配的假安全感**必须在工具描述里写明（主设计 §11.3.3 原文：
  前缀匹配拦不住 `ls; rm`，readonly 仅防误写不防恶意）

### 2.5 产物回报：返回路径与摘要，不传文件内容

主设计未定 node 的产物如何回流。

**定案**：
- 产物留在 node 的 `exec_root` 下，Task 结果里以 A2A `Artifact` 返回
  **路径 + 摘要 + 元信息**，**不传文件内容**
- 文件内容传取属 **L2 能力**（主设计 §11.3.1），是独立 P1 项，不在本期
- 理由：避免把大文件塞进 JSON-RPC 响应；也避免 node 的文件系统被远程读穿

### 2.6 心跳与离线判定：push 模式下 node 不发心跳

BYOM 原稿的「15s ping/pong + 指数退避重连」是**反向 WS 的产物**，
push 模式下**不适用**（node 不持有到桌面端的连接）。

**定案**：
- **node 侧无心跳、无重连逻辑**——它只是监听
- **桌面侧离线判定**：连接失败/超时 → `peer_offline` **fail-fast**，
  **不排队不重试**（主设计 #36；BYOM 原稿的「10 分钟离线队列」属 P1，不在本期）
- 桌面侧可用 Card fetch 或 `tasks/get` 作探活，但**不引入专用心跳协议**
  （保持标准 A2A 互操作）

## 3. 分期与验收对照

| 期 | 范围 | 对照主设计验收 # | 测试 |
|---|---|---|---|
| **N1** | `ginno node` CLI 子命令 + 进程内起 A2A listener + 能力地址签发/打印 + 鉴权复用 | **#38**（CLI 形态）、#41（token 只存 hash）、#2/#6（401 统一） | pytest：CLI 解析、起停幂等、地址签发、无 GUI 依赖断言 |
| **N2** | **入站执行层**（共享模块）+ trust/caps/exec_root 强制 + 自环互通 | **#31/#32/#33**（trust 强制）、#7/#8（权限 fail-closed）、#17/#18（会话隔离）、#40（killpg） | pytest：执行层单测 + **自环 e2e（主 Ginno 派活给本机 node）** |
| **N3** | 产物 Artifact 回报 + `peer_offline` fail-fast + fake 断网/重连 | **#36**（离线）、产物回报（本文 §2.5） | pytest：断网/重连/超时/越界用例 |

**N2 是关键路径**：它同时解锁桌面端入站（主设计 P1-1）与 node，
是 A2A 剩余清单里 ROI 最高的一项。

## 4. 与主设计的偏离点（显式声明）

| 项 | 主设计原文 | 本文处置 | 性质 |
|---|---|---|---|
| `--join` 语义 | 「`ginno node --join <地址>` 接入」 | 收窄为「导入对端地址建立互信」，**不建立反向连接** | **澄清**（push 模式的必然推论，非偏离） |
| 凭据落点 | BYOM 原稿 `~/.ginno-node/token` 独立格式 | 走现有 settings + TokenStore，`GINNO_HOME` 隔离 | **收敛到主设计**（主设计本就写 settings.connectors） |
| 心跳 | BYOM 原稿 15s ping/pong | node 侧无心跳 | **随 pull 模式一并否决**（主设计已否决 pull） |
| 入站执行层归属 | 主设计列为桌面端 P1 | 做成**共享模块**，node 与桌面端共用 | **实现策略补充**，范围未扩大 |

**未扩大范围**：8787 主端口安全闸不在本任务内（任务约束明确）；
L2 文件传取、R0 远程观测、SSE 流式、TTL 两态均**不在本期**。

## 5. 不做的事

- ❌ 反向 WS / pull 模式（主设计已否决）
- ❌ 离线排队（P1）
- ❌ systemd/launchd 模板（P1，本期只保证 `setsid` 可长跑）
- ❌ L2 文件内容传取（独立 P1）
- ❌ 任何 GUI/Tauri 依赖
- ❌ 新造凭据文件格式
