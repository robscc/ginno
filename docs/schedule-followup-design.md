# 定时任务继续沟通（Schedule Follow-up）设计

> 2026-10-14 立项（同日定稿：**fork 优先**）。现状：定时任务每次触发新建
> 影子会话跑一轮 headless，执行记录页只读回放（无 composer、无 WS）。
> 用户看完结果想追问时，只能另开新会话从头交代背景。定稿方案：**一键把
> 影子会话 fork 成普通会话**，在副本里继续沟通——影子会话本体保持只读
> 快照，定时任务「每轮独立、可对比、无状态漂移」的属性零损伤。

## 0. 核心洞察

影子会话是**真实会话**（checkpoint、graph、meta、用量一应俱全，`type:
"scheduled"` 只是隐藏标记）。fork = 复制它的历史到新普通会话——这套机制
**已经存在**：subagent fork 模式的 `_seed_fork_history`（父 checkpoint
消息全量复制、id 全部重新铸造、防 checkpoint 冲突）正是干这个的。本设计
是把它接到定时场景：一个端点 + 一个按钮，无新协议、无回放页改造。

对比落选方案（回放页内置 composer）：影子会话一旦可写，「某轮执行记录」
就变成「一份被用户聊过的话术」——回放失去快照语义，且要给回放页背上
engine/WS 的全部复杂度。fork 让两者各归其位：**回放永远干净，沟通发生
在正式会话里**。

## 1. 产品方案

### 1.1 入口与流程

```
ScheduleRunView（只读回放，保持现状）
┌──────────────────────────────────────────────────────┐
│ ⏰ PR 状态巡检  ✓成功 · 14:02 · 1m12s · ↑12.3k ↓890   │
│                          [ 继续沟通 ]  [ 返回 ]       │  ← 新增按钮
│──────────────────────────────────────────────────────│
│  …transcript（只读，原样保留）…                       │
└──────────────────────────────────────────────────────┘
        │ 点击「继续沟通」
        ▼
POST /api/schedule/runs/{run_id}/fork
        │  新建普通会话 + 复制影子会话全量历史（id 重铸）
        ▼
前端切到新会话（侧栏立即可见，标题 = 任务名）——用户直接输入追问
```

- 按钮**只在终态（ok/error）且影子会话存在时**出现；运行中不出现。
- 新会话出现在侧栏（普通会话，无任何 scheduled 标记），标题沿用任务名
  （剥掉 ⏰ 前缀；重名无碍）。
- 影子会话不动：回放照旧、下一轮定时照常新建、执行记录不变。

### 1.2 fork 语义（复用 _seed_fork_history）

- 新会话 = `create_session`（普通类型；agent / project_slug 沿任务
  target；不继承任何挂载——与影子会话创建时的参数面一致）。
- 历史种子 = 影子会话 checkpoint 的全部消息（user prompt + agent 各轮、
  thinking/tool 全量），**id 全部重铸**——两条会话的 checkpoint 从此无
  关联，互不干扰。
- 种子顶部加一条轻量溯源 context 行：「⏰ 来自定时任务〈名称〉的执行
  （10-13 14:02）」——新会话里一眼知道来路（渲染走现有 context chips，
  零新组件）。
- meta 记 `forked_from_run`（run_id）供追溯；run 行回写
  `forked_session_id`（最近一次 fork 的落点）。
- **允许多次 fork**：每次生成独立副本（用户可能想分头追问不同方向）。
  已 fork 过的 run，按钮旁出现「打开上次会话」快捷入口（P1）。

### 1.3 各场景行为

| 场景 | 行为 |
|---|---|
| 下一次定时触发 | 不受影响，照常新建影子会话 |
| fork 后继续聊 | 全部发生在新会话（普通会话的一切：权限卡、@、steer、子代理…） |
| fork 时原 run 还在跑 | 按钮不出现（运行中无完整终态 transcript） |
| 影子会话被删 | 按钮消失（无源可 fork）；已 fork 出的会话不受影响 |
| workflow 目标 | 无会话可 fork——按钮不渲染（未来可议 fork 为 workflow 输入） |

## 2. 实现拆解

### 2.1 runtime（一个端点）

`POST /api/schedule/runs/{run_id}/fork`：
1. 读 run 行 → 校验终态 + session_id 存在；
2. `create_session`（普通类型，agent/slug 沿 task.target）；
3. 复用 `subagent_scheduler` 的 `_seed_fork_history(shadow_id, new_id,
   slug, brief=溯源行)`——id 重铸 + 顶部 brief 全部现成；
4. meta：新会话记 `forked_from_run`；run 行回写 `forked_session_id`；
5. 返回 `{ok, session_id}`。

### 2.2 前端

- `ScheduleRunView` 顶栏加「继续沟通」按钮（终态且 session_id 在时渲染）：
  调 fork → `g.reloadSessions()` → `g.setActiveSession(new_id)` → 返回
  工作区路由 → 聚焦输入框。
- 「打开上次会话」快捷入口（P1）：run 行有 `forked_session_id` 且会话
  存活时显示。
- 回放页其余零改动（保持只读轮询渲染）。

## 3. 边界与安全

- **隐藏语义不变**：影子会话永远不进列表/搜索；fork 出的副本是全新普通
  会话，进列表是它作为正式会话的本分。
- **权限**：fork 动作本身 = 创建会话 + 复制历史，均为用户已授权能力；
  fork 后的沟通走普通会话的全套门禁，无任何放宽。
- **数据隔离**：id 重铸保证两条会话 checkpoint 无共享写目标（fork 模式
  已在 subagent 场景验证）。
- **成本**：fork 是纯本地复制（无模型调用）；新会话首条追问按普通会话
  计费（input 含 fork 进来的历史，如实记账）。

## 4. 分期

| 期 | 内容 | 判据 |
|---|---|---|
| P0 | fork 端点 + 回放页「继续沟通」按钮 | 一键 fork → 新会话出现在侧栏 → 追问一轮 → 上下文连贯 |
| P1 | 「打开上次会话」入口 + 多副本管理（run 详情列出全部 fork） | 已 fork 的 run 可一键回到任一副本 |
