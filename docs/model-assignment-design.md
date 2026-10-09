# 模型分配与执行中切换 — 产品/技术方案

> 状态：**P1+P2 已实现（2026-10-10），P3 砍掉（用户决策：只干到 P2）**
> 日期：2026-10-09（设计）/ 2026-10-10（实现落定）
> 需求来源：
> 1. 允许在执行过程中给 session 换模型（turn 运行中可切，下一步生效）
> 2. 每个 agent 可以给子 agent 设置模型，设置入口在 Agents 页面

## 0. TL;DR

- **P1**：AgentConfig 新增 `subagent_models`（per-type 模型矩阵），Agents 页每个 agent
  卡片增加 "Sub-agent models" 区块；spawn 时解析链插入一层。顺带把 Agents 页主模型
  自由文本框换成下拉。
- **P2**：会话模型 chip 在 turn 运行中解禁，切换语义为 **下一步生效**（正在流式输出的
  那次调用继续用旧模型完成）。运行时把 `agent_node` 对 model 的闭包捕获改为
  per-superstep 动态解析，PATCH 改为原地更新（不再 pop + 重建 + 断线重连）。
- **P3（可选）**：外部委托（pi/claude/codex）透传 model 参数。

## 1. 现状（file:line 以当前 main 为准）

### 模型绑定与切换
- session 的 `provider/model` 存 meta（磁盘 index）+ 内存 `_SESSIONS` 条目
  （`api/sessions.py:593-595, 652-690`）。
- `PATCH /api/sessions/{id}` 已支持换模型：校验 `build_model` → 改 meta →
  **pop `_SESSIONS`** → 下次 WS 连接 `_ensure_session` 重建 graph
  （`api/sessions.py:726-786`）。语义 = 下轮生效；turn 运行中 UI chip 被禁用
  （`ChatStream.tsx:1436` `disabled={running || parked}`），前端还要
  `cycleSessionSocket` 断线重连（`ChatStream.tsx:652`）。
- 根因限制：model 实例在 `build_graph(model)` 时被闭包捕获
  （`graph.py:1553-1583` → `agent_node_factory` `graph.py:922-1010`），运行中的
  turn 不受绑定变更影响。
- steer 机制证明 mid-turn 通道存在：WS 消息 → `_STEER_STASH` → `agent_node`
  入口吸收（`graph.py:983`）——恰在每次 LLM 调用之前，是模型热切的天然挂点。
- compaction 用内存 `session["model"]`（`compaction.py:227`）；title 生成与
  subagent 派发从 meta 重建 fresh client；checkpoint/state **不含** model，无迁移
  问题。

### 子 agent
- 内部子 agent：`spawn_subagent` 按类型派出真实子会话（`type="subagent"`，
  depth ≤ 2）。模型继承 = 父会话当前模型，可被**类型 frontmatter `model`** 覆盖
  （`subagent_scheduler.py:591-597` → `resolve_type_model`
  `subagent_types.py:179-204`；`provider/model` 前缀仅当 provider 已知才生效）。
- 类型注册表 `~/.ginno/agents/subagents/*.md`（explore/researcher/reviewer/
  implementer + 用户自定义），per-type 模型覆盖**已存在但只能手改文件，无 UI**。
- 外部委托（`delegate_agent` → pi/claude/codex 子进程，`type="delegation"`）：
  adapter argv **不带任何 model 参数**，外部 CLI 自带模型账号（既定产品决策，
  `pi.py:63`）。

### Agents 页
- 即 `/settings/agents/agents` → `AgentsSettings.tsx`，管理 agent profile
  （`~/.ginno/agents/<id>.json`，`AgentConfig` `agents/registry.py:36-57`）。
- 已有 per-agent：system_prompt、tools_allow、connectors_deny、provider 下拉、
  **model 自由文本框**（`:435-443`，无校验无下拉）、图标/颜色。
- `validate_model_binding`（`agents/registry.py:208-225`）已存在：绑定必须在
  provider 的 `models[]` 内，create/update 时强制。

## 2. P1 — 每个 agent 给子 agent 设模型

### 2.1 数据契约

`AgentConfig` 新增可选字段：

```json
{
  "subagent_models": {
    "explore":   {"provider": "custom",  "model": "glm-4.7"},
    "researcher": null
  }
}
```

- key = 子 agent 类型名（须在类型注册表内；保存时校验，未知 key 拒绝）。
- value = 显式绑定，或 `null`/缺省 = 继承（现行为）。
- 写入校验复用 `validate_model_binding`。
- 旧 agent 文件无此字段 → 全部继承，零迁移。

### 2.2 解析链（spawn 时，`create_subagent` 拼装处插入一层）

```
1. agent.subagent_models[type]     ← 新增，UI 显式设置，最高优先
2. 类型 frontmatter model           ← 现有，文件级类型默认
3. 父会话当前模型                    ← 现有默认
4. （进 create_session 后走既有链：全局默认等）
```

理由：UI 设置是用户对该 agent 的显式、近期决策，比手改 frontmatter 更"刻意"，
故置于类型默认之上。绑定失效时的回退沿用 `_build_model_with_fallback`。

### 2.3 UI（Agents 页，文案英文）

每个 agent 编辑卡新增 **Sub-agent models** 区块：

```
Sub-agent models
├─ explore      [Inherit (type default)]  ▾   → 展开为 provider 分组下拉
├─ researcher   [Inherit (parent model)]  ▾
├─ reviewer     [Inherit (parent model)]  ▾
└─ implementer  [Inherit (parent model)]  ▾
    Help: Overrides apply when this agent spawns sub-agents.
    Sessions already running keep their model at spawn time.
```

- 行 = 类型注册表全量（`describe_types()` 已有）。
- 下拉复用 chat 模型 chip 的分组数据源（`GET /api/providers` 的 enabled
  configs × `models[]`），首项 "Inherit"，旁边小字显示**实际解析结果**
  （如 `→ glm-4.7 (type default)`），让用户看到继承链落点。
- **顺带改进**：主模型自由文本框换成同款下拉（含 "Follow provider default"），
  统一交互 + 落实 `validate_model_binding`（现在自由文本可存非法值）。

### 2.4 生效语义

- 改动只影响**之后**的 spawn：spawn 时快照解析结果写进子会话自身的
  provider/model（与现状一致，子会话始终持有自己的绑定）。
- 已 spawn 且在跑的子 agent **不**跟随（见开放问题 1）。

## 3. P2 — 执行中换模型（父/子会话通用）

### 3.1 产品语义

- turn 运行中模型 chip **解禁**；选中即 `PATCH`，语义 = **下一步生效**：
  正在流式输出的那次调用用旧模型跑完，下一个 superstep（下一次 LLM 调用，
  含工具轮转后）用新模型。
- 切换后 UI 轻提示 `Model switched to <X> — effective next step`，chip 立即
  反映新值；不弹确认、不打断输入。
- 子 agent 会话是真实会话，同一机制天然适用（子会话内也可热切）。

### 3.2 运行时机制

1. `agent_node_factory(model, …)` 改为闭包捕获 **resolver**（如
   `session_id → _SESSIONS[sid]["model"]`），每个 superstep 入口取一次，
   `bind_tools` + `ainvoke` 用当次解析结果。steer drain 位置不变。
2. `PATCH /api/sessions/{id}` 的 model-only 变更改为 **原地更新**：
   `build_model` 校验 → 更新 `_SESSIONS[sid]["model"/"model_provider"/
   "model_name"]` + patch meta。graph 不重建、不 pop、不断线——resolver 下一次
   解析自然拿到新模型。空闲时同样走原地更新（下一 turn 首个 superstep 生效）。
   agent_id / workflow 等其他字段的 patch 仍走 pop + 重建。
3. 前端去掉模型切换后的 `cycleSessionSocket`（不再需要重连）；后端广播
   WS 事件 `session.model_changed {session_id, provider, model,
   effective:"next_step"}`，多视图（chat、子 agent 卡片）据此刷新。
4. compaction 跟随 `session["model"]`，原地更新后自动用新模型（进行中的
   compaction 持旧对象跑完，可接受）。title/subagent 派发从 meta 取值，自动跟随。

### 3.3 边界情况

| 情况 | 行为 |
|---|---|
| 新模型不支持 tool call（`bind_tools` 失败） | 该 superstep 走现有 turn 错误通道报错卡；PATCH 时的 `build_model` 校验挡住配置错，运行期能力缺失提示用户切回 |
| provider 配置被删/改导致绑定失效 | 现有 `_refresh_session_metas` preserve-if-resolvable 机制接管（`config.py:420-480`）；在跑 turn 持旧对象直至失败，与现状一致 |
| 切换发生在 steer 恢复段（`--s{n}` 派生 turn） | 无特殊处理：resolver 每 superstep 解析，天然跟随 |
| usage 记账 | 每次请求按当次真实模型记录（实现时核对 requests jsonl 是否已含 model 字段） |
| running 中 PATCH 非 model 字段 | 维持现状（pop + 重建，重连后生效），UI 相应字段保持 running 禁用 |

## 4. P3（可选）— 外部委托透传模型

**已砍掉（2026-10-10 用户决策：只干到 P2）。** 以下保留为未来参考：

- `delegate_agent` 工具加可选 `model` 参数 → adapter argv：claude `--model`、
  codex `-m`、pi 走自身配置（不支持则忽略并记录）。
- UI 放 ExternalAgentsSettings 每后端一个默认 model，不在 Agents 页
  （外部 CLI 不是 agent profile）。
- 与"外部代理自带账号"决策的关系：透传只指定模型名，不代填账号，不冲突。

## 5. 分期与验收

| 期 | 内容 | 验收要点 | 状态 |
|---|---|---|---|
| P1 | `subagent_models` 契约 + 解析链 + Agents 页区块 + 主模型下拉化 | 用 dev agent 配 explore→X，spawn explore 子 agent 验证 meta 里是 X；Inherit 行为与现状一致；非法绑定被拒 | ✅ 单测覆盖（registry 校验/解析链三档优先级/降级） |
| P2 | agent_node 动态解析 + PATCH 原地更新 + chip 解禁 + WS 事件 | turn 运行中切模型：当前流式段不中断、下一 superstep 换模型；无需断线重连；compaction 用新模型 | ✅ 单测覆盖（PATCH 原地/结构回退/getter memo）；真机待验 |
| P3 | 外部委托 model 透传 | claude/codex argv 带上；pi 忽略有日志 | ❌ 不做 |

每期完成后 `make app` + 完全重启验证（zlib 已知故障模式）。

## 6. 决策记录（原开放问题，2026-10-10 落定）

1. **在跑子 agent 是否跟随父级设置热切？** → **否**：子会话持有自己的绑定，
   父级改动只影响后续 spawn（spawn 时快照）；需要热切时在该子会话自己的
   chip 上操作（P2 覆盖）。不做级联广播。
2. **外部委托纳入与否** → 不做（P3 砍掉）。
3. **Agents 页主模型下拉化** → 随 P1 完成（幽灵项保留遗留非法值可见可改）。
4. **running 中切换是否需要确认** → 不确认，轻量 toast
   （"Model switched to X — takes effect next step"，复用 mod toast 总线）。

## 7. 实现偏差记录（相对本设计的 §2/§3）

- **§2.1**：null 条目在保存/读取时一律规范化为**键缺席**（不保留 null 键）；
  null 语义上等于继承，也无需校验类型存在性。
- **§2.2**：spawn 侧成员检查只对**显式 model**做（同 `validate_model_binding`
  硬约束）；空 model 解析出的 provider 默认值来自配置本身，不反向查成员。
- **§3.2**：**ModelHandle 代理方案被否决**——`graph._is_anthropic_model` 走
  `type().__module__` 类型检查，`__getattr__` 代理会被击穿。落地为
  `server_shared.make_session_model_getter(session_id, initial)`：agent 节点
  每 superstep 入口解析真实实例，memo 兜底清表窗口。
- **§3.1**：chip 解禁范围扩大到 **parked** 态（中断挂起恢复后下一 superstep
  生效，机制相同）；agent_id-only 的 PATCH 维持既有原地行为（旧代码本就只在
  模型切换时 pop，语义不变）。
- 前端 toast 文案为硬编码英文（符合 2026-10-04 起 UI 全英文政策），未走
  i18n 键。
