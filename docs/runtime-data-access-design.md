# Runtime Data Access — 特权会话方案（Privileged Session）

> 状态：v3（2026-11-08 审计收敛。v1 工具路线（read_log/投影/分区挂载）
> 被否决；v2 的独立 `privileged` meta 标志亦简化掉——特权 = 指向 `~/.ginno`
> 的 ro 挂载本身，纯挂载形态，无新概念。触发背景：Ginno 承接 Ginno
> （自举开发/排障/跨会话取证）需要 agent 读运行时数据，一刀切守卫迫使
> agent 用字符串拼接绕 bash 弱守卫）。

## 1. 威胁模型（承 v1，不变）

守卫是**防误不防恶**的：L3 bash token 扫描挡不住动态构造路径。硬 deny 的
真实价值 = ①防误（agent 顺手读写运行时状态弄坏自身）+ ②隐私（跨会话
数据/凭据不进无关上下文）。放开形态是"用户显式授权的高信任挂载"，
**提权必须来自用户动作（挂载面板/确认弹层），agent 永远不能自己开**。

## 2. 机制：特权 = 一条指向 `~/.ginno` 的 ro 挂载

无独立标志、无新状态。用户在挂载面板把 `~/.ginno` 以只读挂进会话（预设
按钮或直接输路径，见 §5），该会话即"特权会话"。生命周期（挂载/卸载/
auto_mount 持久/live rebuild）全部是 context_folders 既有语义。

效果（全部来自既有机制，守卫判序零改动）：

| 层 | 行为 | 来源 |
|---|---|---|
| L2 `_DenyPolicy` | mounts 豁免在 home-deny 之前 → 整树可读；realpath 归一化挡 `logs/../settings.json` 类逃逸 | builtin.py 判序 |
| 写保护 | ro 是硬写约束：write_file/edit_file 拒，**独立于 bypass_permissions** | context-folders M0 |
| L1 入口 | glob/grep 的 root 白名单自动接受 | 现状 |
| L3 bash | token 扫描带 mount_roots → 命令中的 home 路径放行；写类命令（`>` 重定向）无法判别，随弱守卫现状（§8） | 现状 |
| 上下文保护 | read_file 分页闸 / grep 5MB 跳过 / glob 10s·20k 目录预算——**特权不豁免**（上下文保护≠访问控制；巨型 session jsonl 走 read_file 分页） | 现状 |

## 3. 唯一的守卫改动：凭据文件并入 always-deny（~6 行）

`_always_deny_roots()` 追加：`paths.settings_path()`、`config.json`、
`paths.mcp_config_path()`。`_inside` 支持文件级 root 精确匹配
（builtin.py:206），已核实可行。效果：

* 普通 session 无变化（这些文件本就被 home deny 覆盖）；
* 特权 session：always-deny **优先于 mounts 豁免** → 即使整树 ro 挂载，
  明文 api_key 所在文件仍然拒——"防误"防线在特权下依然成立；
* 顺带封掉现存口子：挂载面板今天就能手输 `~/.ginno` 以 **rw** 挂上
  （add_folder 无校验）——钳 ro + always-deny 双层封死。

看配置的正道保持 masked 通道（api/config.py GET 已脱敏 / 设置 UI）。

## 4. 护栏（轻量，不动守卫）

1. **手动挂载钳 ro**（~20 行）：`api/folders.py` 的 POST / probe / PATCH：
   library 内 home 树路径一律 `access="ro"`（无视入参，响应标 `forced_ro`）。
   挂载面板的 `createFolder` 调用同样经过（前端预设按钮本身传 ro）。
2. **确认弹层在 path 判定上**（前端 ~15 行）：`mountPath()` 前判定路径
   落在 home 树内（预设按钮或手输均命中）→ 先弹 ConfirmModal（§5）。
   弹层挂在判定上而不是按钮上——任何路径进入 home 都必须过用户确认。
3. **subagent 不继承特权**：spawn 构建子代理 context_dirs 时过滤
   `path == home()` 的条目（~5 行）。授权不随委派扩散。
4. **提权结构性禁止**：agent 工具面无挂载能力（现状如此，保持）；
   审计点：后续新增任何能改 context_folders 的工具都必须拒绝 home 路径。
5. **prompt 警示行**（~10 行）：world_state 渲染 context_dirs 时，对
   path==home() 的条目追加警示（§5 文案）。
6. **审计**：挂载/卸载流水现成；folders API 钳 ro 时打
   `runtime_mount_forced_ro`（INFO）。

## 5. 产品交互：怎么打开

### 主入口 — TopBar 挂载 chip 面板（ContextFoldersChip）

```
面板（点 chip 展开）
├── 预设行：🛡 Ginno 运行时数据 · 只读      [挂载]   ← 新增
├── （现有）路径输入框 …                    [＋]    ← 手输 ~/.ginno 同效
└── （现有）挂载列表：各条目 [unmount] / auto_mount 开关
```

* 点预设「挂载」或在输入框输入 `~/.ginno` → **ConfirmModal 确认弹层**
  （复用 ConfirmModal.tsx）：

  > **开启特权访问（只读）**
  > 本会话的 AI 将能读取 Ginno 运行时数据：运行日志、全部项目的会话
  > 历史（含你与 AI 的完整对话）、用量明细、记忆。数据只读不可写；
  > 设置中的密钥文件（settings/config/mcp）仍受保护不可读。
  > [取消] [仅本会话挂载] [挂载并默认用于新会话]

  两个确认按钮分别对应 `auto_mount=false/true`（既有字段，语义精确）。
* **状态可见**：挂载列表中该条目 🛡 徽标 + ro 样式（现有绿色 auto 指示
  同款位）；chip 计数含它。深链：dev 指纹/排障场景点击侧栏问号的
  "排障"入口直达该面板（可选）。
* **关闭**：条目 unmount（现有交互，即时 rebuild，特权立即失效）；或关
  auto_mount（新会话不再自动带上）。

### 引导入口（可选，P1）— 排障场景的 agent 提示

普通会话里用户说"看下日志为什么报错"，agent 读不到 → 回复中给出
「需要只读挂载运行时数据」提示 + 前端高亮挂载 chip（或深链按钮）。
agent 只引导，不代开。graph_prompts 排障提示段 ~3 行。

## 6. 测试计划（~120 行）

`tests/unit/test_privileged_mount.py`（isolated_home）：

* 挂载 home(ro) 后：read_file 读 `<home>/logs/sidecar.log` 成功；
  glob root=home 可列出；
* write_file / edit_file 目标在 home 内 → ro 拒；
* read `<home>/settings.json` / `config.json` → denied（always-deny 优先）；
* bash `cat <home>/logs/x.log` 放行；`cat <home>/config.json` 拒；
* bash `cat <home>/logs/../config.json` → 拒（realpath 归一化）；
* 卸载 → 上述读全部恢复 denied；
* spawn 子代理 → 子代理工具集内 home 路径 denied（不继承）。

`tests/api/test_folders_guard.py`（~40 行）：

* POST 挂 home（`access:"rw"` 入参，复刻挂载面板调用）→ 落库 ro、
  `forced_ro: true`；
* PATCH 改 path 到 home 子树 / 改 access → 同样钳制；
* 挂 home 外目录 → 行为不变（回归）。

前端 vitest：mountPath 前 home 路径触发确认弹层（预设与手输两路）。

## 7. 验收判据

| 判据 | 操作 |
|---|---|
| 打开顺畅 | chip 面板 → 预设行/手输 `~/.ginno` → 确认弹层 → 挂载成功，🛡 徽标可见 |
| 自举排障通 | 特权会话说"看下日志报错"→ agent 直接 read_file/grep sidecar.log，无拼接 hack |
| 跨会话取证通 | 特权会话 read_file 分页读任意 sessions/<id>.jsonl |
| 凭据永不进上下文 | 特权下读 settings.json 拒（含手输 rw 挂载场景） |
| 误写结构性挡 | write/edit 拒 + 子代理不继承 |
| 可逆 | unmount 即时生效；auto_mount 控制新会话默认 |

## 8. 已知限制（文档化，不修）

* bash 写类命令（`echo > settings.json`）在特权 session 放行（token 扫描
  不判读写语义）——弱守卫既有边界，特权是用户显式确认的高信任上下文；
* grep 跳过 >5MB jsonl → read_file 分页替代；
* 特权使 home 全树对 agent 可见（含个人会话历史）——确认弹层已明示，
  这是用户自己的授权决定。

## 9. 决策记录

* **D7 特权=纯挂载形态**：无独立标志/无新状态；挂载面板预设 + path 判定
  承载全部产品语义（确认弹层/徽标/auto_mount 复用既有交互）。
* **D8 v1 工具路线废弃**：read_log/inspect_session/分区投影不做。
* **D9（承 D3）凭据文件 always-deny**：特权下也拦；看配置走 masked API。
* **D10 子代理不继承特权**：spawn 过滤 path==home() 条目。
* **D11（承 D5）写永不放开**：ro 硬约束独立于权限模式（含 bypass）。
* **D12 确认弹层挂 path 判定而非按钮**：任何路径形式进入 home 都必须过
  用户确认，预设按钮只是快捷方式。
