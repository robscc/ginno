# BYOM — Bring Your Own Machine（自带机器）设计

> 状态：**设计稿 v1（待评审）** 日期：2026-11-08
> 范围：把用户自己的机器（家里 PC / 公司服务器 / NAS / 云 VPS / 局域网台式机）注册为 Ginno 的**执行节点**，agent 可把 bash / 文件 / headless turn 派到那台机器上跑。

## 0. TL;DR

- **一句话**：机器上跑 `ginno node`（复用 runtime wheel，零新包），主动反向 WS 连到桌面端（或 node 监听、桌面端正向连）；桌面端多一张「我的机器」花名册，agent 多一组 `machine_*` 工具。
- **组网自带**：延续本地优先哲学——局域网 / Tailscale / SSH 隧道 / 公网 IP 由用户自备，Ginno **不建云中继**、不加任何云端依赖。
- **认证**：一次性配对码换长期 token；token 可随时重置吊销；per-machine 信任级别（readonly / normal / full）在**派发前**静态钉死（照 delegate_agent 的 seam 原则）。
- **能力分级**：L0 状态探测 → L1 bash 执行 → L2 文件 get/put → L3 headless turn（真·把 agent 派到那台机器干活）。v1 落 L0/L1，P1 补 L2/L3。
- **测试缝**：`GINNO_FAKE_NODE` 脚本化假节点，桌面端全链路测试零真机（照 `GINNO_FAKE_LLM` 先例）。

## 1. 背景与目标

### 1.1 现状盘点（代码事实）

| 现有基建 | 位置 | 对 BYOM 的复用点 |
|---|---|---|
| worker 会话花名册（list/assign/watch/status） | `worker_bus/registry.py`、`worker_bus/deliver.py`、`worker_bus/tools.py` | 机器注册表照此范式：resolve_target 按 id/标题命中、状态投影不新造存储、assignable 判定 |
| 浏览器扩展 WS 反连（hub + 请求配对 + 队列） | `browser/relay.py`（`/extension` 端点、RelayState、invoke_tool） | 机器 → 桌面端反向 WS 协议层直接照抄（hello/心跳/req-res 配对） |
| headless 一次性入口，可独立安装（pipx/uv tool） | `cli.py` 的 `ginno exec`、`docs/headless.md` | L3 turn 派发 = 在目标机器上调 `ginno exec`；node 子命令加进同一 CLI |
| 外部代理委托（安全 seam 六原则） | `docs/external-agents-design.md`、`delegation_sessions.py` | 任务简报自足、静态权限、结果字符串唯一回流、进程树超时清理、用量双写、输出不可信——全部平移 |
| runtime wheel 构建 | `make runtime-wheel`（workbuddy-bench 已在用） | 机器侧安装 = `uv tool install packages/runtime`，无需桌面 app |
| 后台任务卡 + 输出 spool | `api/tasks.py`、bg task 范式 | 远程 exec 的输出回传沿用「spool + 截断 + tail 取回提示」形态 |
| 定时执行 | `api/schedule.py`（30s tick） | P2：定时任务可指定目标机器 |
| 设置 API 白名单合并先例 | `world_state.py`（`_CONTEXT_DEFAULTS`）、opt-in 门控 | `machines_enabled` 门控模板（默认 false，env `GINNO_MACHINES` 覆盖） |

### 1.2 目标

1. 用户在 Settings 里加一台自己的机器（生成配对码 → 机器上一条命令完成接入），此后 agent 可查机器列表、在那台机器上跑命令、传文件、派完整 turn。
2. 长任务出沙盒：爬虫、训练、构建、批量处理跑在用户的常开机器上，本机合盖不影响。
3. 资源/数据就近：目标机器上的数据（代码仓库、数据集）不用先拖回本机再处理。
4. 全程零云：桌面端与机器直接通信，密钥与任务内容不出用户自己的网。

### 1.3 非目标（本期不做）

- 云中继 / 消息服务器 / 账号体系——违背本地优先，绝不引入；
- 交互式 shell 转发 / 端口转发 / 远程桌面——用户已有 SSH/tailscale，不重造；
- 一台 node 注册到多个桌面端（一 node 一 desktop，v1 从简）；
- 自动组网（wireguard 编排等）——组网自带，文档给建议即可；
- GPU 调度 / 算力市场——只做状态展示（L0 报 caps），不做调度。

## 2. 拓扑与连接模型

### 2.1 两种模式（按可达性选，同一套协议）

| 模式 | 方向 | 适用 | 实现 |
|---|---|---|---|
| **pull（默认）** | node → 桌面端反向 WS | 机器可达桌面端：同局域网、Tailscale 网内、SSH -R 隧道 | 桌面端 FastAPI 挂 `/node` WS 端点（照 `relay.py` 的 `/extension`）；桌面端零暴露 |
| **push** | 桌面端 → node 正向连 | 机器有公网 IP / 端口可达（云 VPS） | node 以 `ginno node --listen 0.0.0.0:9700` 起 FastAPI，同一套 req/res 帧换成 HTTP-WS 服务端 |

- 心跳：15s ping/pong；断线自动重连（指数退避，上限 60s）。
- 机器条目记 `mode`（pull/push）与 `url`；pull 模式下 url 是 node 配置里的桌面端地址，push 模式下是 node 的监听地址。

### 2.2 配对与认证

1. 桌面端「添加机器」→ 生成**一次性配对码**（8 位、10 分钟有效、单次消费），展示给用户。
2. 机器上执行：`ginno node --join <desktop-url> --code <配对码>` → node 换回长期 token（32 字节随机），存 `~/.ginno-node/token`；桌面端只存 token 的 SHA-256（不落明文，与 api_key 的 mask 纪律一致）。
3. 之后每条 WS 连接 hello 帧携带 token；桌面端 hash 比对。
4. **吊销**：Settings 里「重置配对」→ 删 token hash + 生成新码；该机器旧 token 立即失效。

### 2.3 传输安全

- LAN / Tailscale：`ws://` + token 每连接验证，够用。
- 公网 push 模式：建议用户自备 TLS（node `--tls-cert/--tls-key` 或 caddy 反代）；文档明确「**最省心的做法是两台机器装 Tailscale**」。

## 3. 节点能力模型与信任级别

### 3.1 能力分级（node hello 帧声明 caps）

| 级别 | 能力 | v1 落点 |
|---|---|---|
| L0 | `status`：os / cpu / mem / disk / gpu / python 版本 / 标签 | P0 |
| L1 | `exec`：bash 命令（cwd 限 root 内、timeout、exit code、输出 spool 回传） | P0 |
| L2 | `file_list` / `file_get` / `file_put`：root 内文件传取（分块帧，单文件 cap 512MB） | P1 |
| L3 | `turn`：机器上调 `ginno exec` 跑完整 headless turn（用机器的文件系统与本地模型配置） | P1 |

不做 L4（常驻服务/端口转发）。

### 3.2 信任级别（桌面端 per-machine 设置，派发前钉死）

| 级别 | exec | file | turn |
|---|---|---|---|
| `readonly`（默认） | 只允许白名单只读命令前缀（`ls/cat/head/tail/wc/du/df/ps/git status/git log/git diff/pip list`…），任何写/网络命令拒绝 | 只允许 get/list | 只允许 `--no-attach` 进程内只读 |
| `normal` | 限 root 内 cwd 的常规命令 | get/put/list | 开放 |
| `full` | 不限（仍限 root 边界） | 全开 | 开放 |

- 判定在**桌面端派发前**做一次，node 侧再按同一张表复核一次（双侧一致，accept-then-ignore 不存在——照 delegate_agent「fail-loud」）。
- readonly 白名单是**前缀集合**，不试图做语义安全分析；危险操作靠用户主动调 trust，工具描述里写明。

### 3.3 root 边界

- node 启动必须给 `--root DIR`（默认 `~/ginno-node-workspace`），所有 exec cwd 与文件操作限定 root 内，越界 **fail-loud 拒绝**（不做静默路径修正）。
- L3 turn 等价 `ginno exec --cwd <root 内目标>`，world-state 注入用机器侧自己的 `GINNO_HOME`。

## 4. 协议（WS 帧，JSON 单行）

```
→ hello  {type:"hello", id, name, caps:["exec","file","turn"], token, ver}
← hello_ack {type:"hello_ack", ok, trust, reason?}
→ ping / ← pong（15s；双向皆可发）
（桌面端 → node）
→ req {type:"req", id, op:"status"|"exec"|"file_list"|"file_get"|"file_put"|"turn", args:{...}}
← res {type:"res", id, ok, payload:{...} | error:{code,message}}
   exec: payload={exit_code, stdout_tail, stderr_tail, spool_ref?, duration_ms}
   file_get/put: 分块帧 {type:"chunk", id, seq, data_b64, final}
（node → 桌面端）
→ event {type:"event", kind:"exec_progress", id, delta_tail}（长命令的 live tail，限频 2s）
```

- 请求配对与超时：照 `relay.py` 的 invoke 队列——req 带单调 id，pending dict 配对 res；桌面端侧统一 30s（status）/ 用户指定（exec）/ turn 专属超时。
- 断线语义：pending req 全部以 `code=machine_offline` 失败回工具层；v1 **不做离线排队**（P1 加「10 分钟过期队列」，照 assign_worker queued-不持久语义）。

## 5. 工具面与派活集成

agent 可见新工具（进 `build_all_tools`，默认**不注册**，`settings.context.machines_enabled` 门控，env `GINNO_MACHINES` 覆盖——照 external_agents 先例）：

| 工具 | 签名（要点） | 对应 |
|---|---|---|
| `machines()` | 无参 → 花名册行（id/名称/模式/状态 online-offline/信任/能力/最近活动） | 照 `list_workers` 行格式 |
| `machine_run(machine, command, cwd?, timeout_s?)` | 返回 exit code + 限长输出；离线 fail 不重试 | L1 |
| `machine_file(machine, action, path, ...)` | get/put/list | L2（P1） |
| `machine_turn(machine, goal, constraints?, acceptance?, timeout_s?)` | 自足简报（同 delegate_agent 三段式），返回结果摘要 | L3（P1） |

- 结果一律文本、错误 `[error]` 前缀、绝不 raise；每条结果头部带 `[machine:<id>]` 溯源标记（输出是**不可信数据**）。
- 截断走既有 `_tools_node_factory` 中段截断，不自带。
- 用量双写：L3 turn 的 token 记 JSONL 账本 `source="machine:<id>"` + 同步 `_USAGE_BY_SESSION` 累加器（照 external 双写，否则 TopBar 回跳）。
- P2 集成：`create_subagent_plan` 的 item 增加可选 `machine` 字段（依赖调度自动挑在线机器）；scheduler 定时任务增加目标机器。

## 6. 安全模型（delegate_agent seam 六原则平移）

| 原则 | BYOM 落点 |
|---|---|
| 1. 任务简报自足 | `machine_turn` 的 goal/constraints/acceptance 组装后在**桌面端**冻结下发，机器侧看不到会话上下文 |
| 2. 静态权限、启动前钉死 | per-machine trust 在派发前判定 + node 复核；readonly 白名单不靠 agent 自律 |
| 3. 结果字符串唯一回流 | req/res 文本帧；不透传事件流；`[machine:<id>]` 溯源头 |
| 4. 进程树超时清理 | node 侧 exec 用 `start_new_session` + `killpg`（SIGTERM→2s→SIGKILL）；孤儿命令不留存 |
| 5. 用量双写 | §5，`source="machine:<id>"` |
| 6. 输出不可信 | 工具 docstring + 溯源头声明；中央截断不动 |

补充边界：
- token 明文只存机器侧；桌面端只存 hash；重置配对即吊销。
- node 的 root 之外**没有**任何可触达面（无配置读写 op、无任意路径文件 op）。
- 桌面端 `/node` 端点只认 hello 帧 token；无 token 的连接 3 秒内踢（不泄露存在性信息差异）。

## 7. 存储与设置 UI

- 桌面端：`~/.ginno/machines.json`（新文件，格式对齐 settings.json 风格）：
  `{machines:[{id, name, mode, url, token_hash, trust, caps, tags, created_at}], pairing:{code, expires_at} }`
  读写走既有 settings 写路径纪律（写前校验、display 值不回填真 secret）。
- 设置页「我的机器」（`apps/web` Settings 新 tab）：
  - 列表：在线状态点（绿/灰）、模式与地址、信任级别徽章、能力 tags、最近活动时间；
  - 添加向导：选 pull/push → 生成配对码 + 复制「机器上要跑的一条命令」；
  - 行操作：重命名 / 改 trust / 重置配对（吊销）/ 删除（`_config_references` 式守卫：有 pending 任务时 409 提示，照 model_configs 删除守卫先例）。
- 顶栏：可选机器徽章（照 badge API 先例），显示在线机器数。

## 8. node 形态与安装

- `packages/runtime` 新增 CLI 子命令 `ginno node`（argparse 已在 `cli.py:660`，加 subcommand）：
  ```
  ginno node --join <desktop-url> --code <code> [--name NAME] [--root DIR] [--listen HOST:PORT] [--tls-cert P --tls-key P]
  ```
  首次 join 换 token 后写 `~/.ginno-node/config.json`，之后裸 `ginno node` 即可重连。
- 依赖零新增：fastapi/uvicorn/websockets/httpx 均已在 runtime 依赖树里。
- 常驻方式：P1 提供 systemd unit 与 launchd plist 模板（`docs/` 附模板片段），文档给「nohup 最小版」。

## 9. 测试策略

| 层 | 方案 |
|---|---|
| 协议/注册表单测 | 帧编解码、配对码过期/单次消费、token hash、resolve_target 命中/歧义、trust 判定表驱动 |
| 桌面端全链路 | `GINNO_FAKE_NODE=1` + `GINNO_FAKE_NODE_SCRIPTS=<json>` 脚本化假 node（照 fake_model 范式）：工具→dispatch→res 回流零真机 |
| node 侧单测 | root 边界越界拒绝、readonly 前缀白名单、killpg 超时清理、重连退避 |
| e2e（本地 loopback） | 真起一个 pull node（本机 127.0.0.1 对连）走 exec 全链路；CI 干净环境可跑，无外部依赖 |
| 红线 | 不在 `tests/e2e/` 建 conftest；共享 fixture 进根 conftest；UTC 边界照旧意识 |

## 10. 分期

| 期 | 内容 | 验收 |
|---|---|---|
| **P0（最小可用）** | machines.json + `/node` WS 端点（pull）+ 配对码/token + `ginno node`（L0/L1）+ `machines()` / `machine_run()` + Settings 机器页（列表/添加/删除/trust）+ fake node 测试缝 + 门控默认关 | 局域网机器配对成功；agent 能 `machines()` 看到在线机器并在其上跑 `uname -a`；trust=readonly 时 `touch x` 被双侧拒绝；单测+loopback e2e 绿 |
| **P1** | L2 文件 get/put + push 模式 + L3 `machine_turn`（headless 委派）+ 用量双写 + 离线 10 分钟队列 + systemd/launchd 模板 + live tail event | 机器间传文件成功；`machine_turn` 在远程机器跑完一个真实任务并回流摘要；usage 页出现 `machine:<id>` 来源 |
| **P2** | plan item 的 `machine` 字段、scheduler 目标机器、按 caps/标签路由、机器分组 | plan 里一条 item 指定家里机器跑构建，全程无人值守 |

## 11. 风险与开放问题

- **NAT 场景覆盖**：桌面端在家、机器在公网 → push 模式或 tailscale；文档不替用户解决组网（非目标已声明）。
- **readonly 白名单的假安全感**：前缀匹配拦不住管道/子命令注入（如 `ls; rm`）→ 文档明确 readonly 仅防误写不防恶意；真敏感机器建议只在可信网内挂。
- **token 泄漏爆炸半径**：= 该机器 root 目录内任意命令执行 → 缓解：默认 readonly、吊销即时、root 目录最小化。
- **并发写冲突**：远程机器上可能同时有用户手动在干活 → 工具描述提醒（对齐多 agent 协作纪律「commit 前核对文件归属」）。
- **开放问题**：① L3 turn 在机器侧是否复用桌面端的模型配置（建议：机器侧自管 `GINNO_EXEC_*`，v1 不同步 provider 配置）；② Windows 机器支持度（runtime 本身跨平台，killpg 在 Windows 退化 taskkill——P1 实测再定）；③ machines.json 是否进 settings context 白名单（建议是，便于 agent 感知机器存在）。
