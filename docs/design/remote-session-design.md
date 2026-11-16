# 远端会话设计：A2A peer 作为子 agent 与会话执行方

> 状态：设计定案中（2026-11-16）
> 关联：`a2a-connector-design.md`（协议与入站/出站）、`ginno-node-headless-design.md`（节点形态）
> 触发：用户要求「新建对话时能选远端 ginno」，且「A 当成子 agent 来对待也要，B 也要」

## 1. 问题与现状

### 1.1 用户要什么

| 代号 | 需求 | 语义 |
|---|---|---|
| **A** | 远端 peer **当子 agent 对待** | 用户可显式指派活给远端，有 worker 卡、进度、结果回注，与本地 subagent 同体验 |
| **B** | **新建对话时选远端 ginno** | 整个会话归属远端，每轮都在那台机器上跑，本地只是显示与交互层 |

### 1.2 现状（已核实，非推测）

- 出站 peer 可在 UI 添加（`A2aPeerAddModal` 四步向导，后端 `a2a_add_peer` 已实现）
- 加完后 peer 变成**两个工具** `a2a_<slug>_delegate` / `a2a_<slug>_task`（设计 §7.4），
  **模型驱动**，用户无法显式指派
- composer 的执行方选择器 `target` 只从 `g.agents`（**本地 agent**）取值
- `MentionKind = "artifact" | "agent" | "session" | "result"`——**无 peer**
- 设计文档**无「会话级执行方」概念**

**结论**：A 与 B 都不存在。当前形态是「本地 agent 当编排者，远端当被委托方」，
用户要的是「远端可以是一等公民的执行方」。

## 2. 核心抽象：远端会话（remote session）

A 与 B **共用一个核心**，不各做一套：

> **一个会话可以挂 `remote_peer`。挂了之后，它的 turn 在远端 peer 执行，
> 而不是本地跑图。**

- **A** = `type="subagent"` 且挂 `remote_peer` 的会话
  → 现有 worker 卡 / digest / 结果回注 / `watch_worker` **全部复用，零改动**
- **B** = 普通会话挂 `remote_peer`
  → 新建对话时选 peer，整个会话归属远端

这样 A 不是「另做一套委托」，而是「subagent 的一种执行后端」；
B 不是「另做一套会话」，而是「会话的一种执行后端」。

### 2.1 定案

| # | 议题 | 定案 | 理由 |
|---|---|---|---|
| 1 | 存储位置 | session meta 增 `remote_peer: str`（peer slug），空/缺省=本地 | 与 `agent_id`/`provider` 同层，落盘即持久，重启不丢 |
| 2 | 路由接缝 | **`_run_stream` 调用前拦截**（`api/stream/ws.py` 两处 + deliver 路径） | 单点收口，避免每个调用方各判一次 |
| 3 | 远端上下文映射 | 复用出站既有 `a2a/contexts.json`（设计定案 #12），本地 session_id ↔ 远端 contextId 1:1 | 不另造映射；多轮延续靠它 |
| 4 | 进度回传 | 优先 `message/stream`（SSE，P1-3 已实现）；对端不支持则降级 `message/send` + 轮询 `tasks/get` | Card 的 `capabilities.streaming` 是判据，不猜 |
| 5 | 产物落地 | 远端 Artifact 的文本内容（P1-1 已实现）落进本地会话 transcript + Artifacts 面板 | 「能看到成果」是本轮已验证的核心价值 |
| 6 | 权限批准 | 远端 `input-required` → 本地弹权限卡 → 用户批准 → 带 `referenceTaskIds` 回信续跑（P1-2 已实现） | **绝不本地自批**；远端 trust 边界仍由对端 guard 强制 |
| 7 | 失败语义 | 对端离线/不可达 → **fail-fast 明确报错**，不静默回退本地执行 | 静默回退会让用户以为在远端跑，实际在本机——最危险的错觉 |
| 8 | 本地工具面 | 远端会话里**本地工具不注入**（文件/bash 等都在远端）；只保留交互类（ask_user/render_widget/attach_ref） | 否则模型会以为能操作本机文件，产生错误行为 |
| 9 | A 的指派入口 | `spawn_subagent` 增 `peer` 参数；@ 菜单与 subagent 选择器列出 peer | 用户显式指派，不靠模型自觉调工具 |
| 10 | B 的选择入口 | 新建对话的执行方选择器把 peer 与本地 agent **并列**，peer 项带机器图标与地址提示 | 一眼看出「这轮在哪台机器跑」 |
| 11 | 混合会话 | **不支持**同一会话中途切换本地/远端 | 上下文与文件系统不连续，切了等于换会话；要换就新建 |
| 12 | 远端会话的 fork | **禁止** fork 远端会话 | fork 依赖本地 checkpoint 重放，远端图状态不在本机 |

## 3. 分期

| 期 | 内容 | 验收 |
|---|---|---|
| **R1** | 远端会话核心：meta 字段 + 路由接缝 + 远端 turn 执行器（send/stream 双模）+ 上下文映射复用 | 挂 `remote_peer` 的会话真在远端跑完一轮，transcript 有远端回复；对端离线 fail-fast |
| **R2** | A：`spawn_subagent(peer=...)` + worker 卡/digest 复用 + @ 菜单 peer 项 | 显式指派远端 subagent，worker 卡显示进度，结果自动回注父会话 |
| **R3** | B：`createSession(remote_peer=...)` + 新建对话选择器 UI + 会话头显示远端标识 | UI 新建对话选远端，整个会话在远端跑；会话头一眼看出归属 |
| **R4** | 权限批准转发 + 产物落 Artifacts 面板 + 断线重连 | 远端 ask → 本地批准 → 续跑成功；产物可在面板打开 |

R1 是地基，R2/R3 并列依赖它，R4 收尾。**R2 与 R3 可并行**（文件归属不冲突）。

## 4. 安全边界（不因便利放宽）

- 远端的 trust/caps/exec_root **仍由对端 guard 强制**，本地只是发起方
- 本地**绝不代批**远端权限请求（定案 #6）
- 远端会话不注入本地文件/shell 工具（定案 #8），避免「以为在远端其实动了本机」
- 对端离线**不静默回退本地**（定案 #7）
- peer 凭据仍走既有 settings 约定，不进 transcript / 日志

## 5. 不做

- 同一会话中途切换执行方（定案 #11）
- fork 远端会话（定案 #12）
- 远端会话的本地 checkpoint 重放（图状态在对端）
- 把远端 skill 拆成独立工具（沿用设计 §7.4：skill 是描述性的）

## 6. 待验证的留白

| 项 | 现状 | 处理 |
|---|---|---|
| 远端 `input-required` 的事件形态 | P1-2 已实现协议侧，但**本地 UI 如何呈现**未定 | R4 施工前先看既有权限卡组件能否复用 |
| SSE 断线后的续订 | `tasks/resubscribe` 已实现 | R4 验真机断线重连 |
| 远端会话的 token 用量计费 | 用量在对端产生 | 先如实标注「远端用量不计入本地统计」，不假装能合并 |
