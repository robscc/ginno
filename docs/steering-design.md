# Steering 设计（运行中注入用户消息）

> 状态：**草案**（语义、UI、范围已全部确认，决议见 §8）。目标：让用户在 Agent 正在工作时输入的消息**在同一次 turn 内被吸收进上下文**，而不是被丢弃或只能等下一轮 —— 对齐 Claude Code 的 real-time steering。

## 0. TL;DR

- **机制**：前端持有队列 → WS `steer` → runtime 存 per-session stash → **`agent_node` 入口 drain**，把消息追加进本次模型请求。注入时机 = LangGraph 的**超步边界**，与 Claude Code 的「工具调用一结束就传给 Claude，同一 turn 内」逐字对应。
- **不做**真正的中途抢占。现有唯一的轮中改状态原语 `aupdate_state(as_node="agent")` 会**终止当前段**（`stream.py:1258-1261`），所以不需要它 —— 注入点在节点入口，不在状态改写。
- **范围（已确认）**：只做轮中吸收。turn 结束时仍未被吸收的消息，由**前端**作为**普通 `invoke`** 开新轮发出（= Claude Code 的「只把最老一条作为下一 turn」）。runtime 不排空队列、不自动续轮。
- **parked 状态当作答复/改主意（已确认）**：turn 停在 permission / ask_user / version_propose 卡上时，用户输入不走队列，走 resume 路径 —— 语义按卡种区分（§3.4）。
- **消息形状**：持久化的是**原始用户文本** + `additional_kwargs["ginno_steer"]` 标记；`<ginno_steer>` 包裹只加在**模型面副本**上（沿用 `strip_old_images` / `_mark_cache_tail` 既有的 copy-only 惯例）。**不复用** goal 的 `[goal context]` 折叠（与已确认 UI 冲突）。
- **必须新铸 id**：steered 消息一律用新 message id，绝不复用 turnId。checkpointer 的 delta 只按 id 比对内容（`checkpointer.py:214-232`），复用 id 改文本会静默留下旧内容。
- **落点**：`api/stream.py`（新消息类型 + stash + ack）、`graph.py`（`agent_node` drain + 模型面 wrap + compaction 计数豁免）、`api/stream.py::_heal_interrupted_turn`（未提交即 stop 的兜底）、`compaction.py` / `microcompact.py`（计 turn 时豁免 steered 消息）、`ChatStream.tsx`（队列条 + 三种渲染态）。

---

## 1. 参考实现（Claude Code 事实基础）

> 来源：code.claude.com/docs/en/interactive-mode.md、anthropics/claude-code CHANGELOG.md、本机 v2.1.278 二进制字符串、SDK typescript.md。**只有标注【推断】的为推断。**

### 1.1 送达时机（核心）

官方 §"When Claude Code sends what you queued" 原文规则：

| 排队内容 | 时机 |
|---|---|
| **消息** | Claude 跑工具调用时排的消息，**工具调用一结束就传给 Claude，在同一 turn 内**；turn 自然结束时若仍有剩余，**只把最老一条**作为下一 turn 的开场，其余继续按同规则 |
| 命令 / shell | 压到 turn 结束，再一条条执行 |
| `Esc` | 中断 turn；队列保留并立即作为**新 turn** 开场发出 |

内部术语 **fold / absorb**：`messageQueue.consume(..., {reason:"absorbed_mid_turn"})`，日志 `[query] abort/suspension during mid-turn absorption — leaving N command(s) queued`。`absorbed_mid_turn` 仅存在于二进制内部日志，changelog 从未使用。

### 1.2 注入形状

- 多条同批折叠时**拼接成一条 user 消息、换行分隔**（changelog 2.1.78 "Fixed queued prompts being concatenated without a newline separator"；SDK "can merge several into one turn"）。
- user role 文本消息，带前缀常量 `"The user sent a new message while you were working:\n"`，内部分类器按此前缀判为 `queued_user` origin。
- 落盘为 `attachment.type === "queued_command"`；**mid-turn 折叠的消息不建 checkpoint**，`/rewind` 不列出。
- 与 tool_result **同一条消息**吗：【推断】否 —— 独立 user 消息追加在 tool_result 之后。
- prompt cache：无官方直接说明。间接证据 changelog 2.1.269 修复「中断后续发改变前缀重发方式、**损害 prompt-cache reuse**」，说明官方刻意保前缀稳定。【推断】尾部追加不损前缀命中。

### 1.3 中断与 UI

- `Esc`：已完成的工作保留；部分输出留在历史并写 marker `[Request interrupted by user]`（打断 tool_use 时 `... for tool use`）；未完成的 tool call 得 **error tool_result** 以保证 tool_use/tool_result 配对。
- `Ctrl+Enter` / `Ctrl+X Ctrl+S`：中断当前 turn + 一次性发出全部队列（2.1.275）。**本次不采用（已确认）。**
- `Esc Esc` 清草稿或开 `/rewind`；`Ctrl+C` 中断/清输入/二击退出。
- 队列条目显示在输入框上方，提示 `Press up to edit queued messages`；`↑` 取回全部到输入框、一行一条，改完重新作为**一条**入队。
- mid-turn 注入的消息在 transcript 里全宽背景高亮（2.1.181），模型接收前显示灰色（2.1.275）；assistant 消息带 `user_message_uuid`，折叠时 echo 最后一条的 uuid。
- 历史：0.2.75 排队引入 → **0.2.108 real-time steering 引入** → 1.0.84 / 2.0.68 可靠性修复（收尾时被忽略、subagent 中丢失）→ 2.1.205 / 2.1.219 SDK interrupt 回执 → 2.1.275 send-now。

### 1.4 另一条参考：Codex（本项目已调研过）

`goal-design.md:51-52` 记录 Codex 的两个 goal 模板走 **`inject_if_running`** —— objective 被用户改动、或记账越过预算时，注入**正在运行**的 turn。与 Claude Code 同构，说明「轮中注入」是两家共同选择。

---

## 2. 术语与范围

| 术语 | 含义 |
|---|---|
| **steered message / 中途消息** | 用户在 turn 运行中提交、且被吸收进该 turn 的消息 |
| **吸收（absorb）** | `agent_node` 入口把 stash 里的消息追加进 state，使其出现在**下一次模型请求**里 |
| **stash** | runtime 侧 per-session 待吸收队列（内存态） |
| **未吸收（unabsorbed）** | turn 结束时仍留在 stash / 或已 drain 但未提交的消息 |

**范围内**：纯文本 steer；内置 `/` 命令的立即响应；轮中吸收；parked 答复/改主意；三态 UI；stop 与队列的关系。
**范围外**（**已确认**）：图片/文件附件；`@mention` 展开（见 §3.1 —— steered 消息里的 `@file` 按字面文本交给模型，不做文件引用解析）；`Ctrl+Enter` 发送即中断；subagent 内部的 steering；`PinStream.tsx` 速聊窗（v1 只做 ChatStream，§5 列为跟进）。

---

## 3. 机制设计

### 3.1 WS 协议（新增两类上行、两类下行）

**上行**

```
{ type: "steer", steer_id, turn_id, message }
```

- `steer_id`：客户端铸造的幂等键（新 UUID，**不复用** turnId）。
- `turn_id`：客户端认为在跑的那个 turn（用于归属与对账）。
- 文本先过 `_commands.resolve_turn`（与 `invoke` 同），但**只取两层**：
  - `builtin_reply` 非空 → 立即回 `notice` + `message.end`，**不入队**（与现状一致 —— `stream.py:316-323` 的内置命令分支本就排在 busy 检查**之前**，所以 `/status` 这类今天在运行中也已经能立即响应，正是 Claude Code「立即执行的命令不入队」的等价物）。
  - 否则 stash `plan.text`（skill 文本替换保留 —— 它是纯文本层改写）。
- **丢弃** `mention_ctx` / `files_extra` / `agent_override`（已确认 v1 不做 mention）：`resolve_turn` 本身**不改写**用户文本（`commands/resolver.py:230-249`），mention 是独立注入通道，所以丢弃它们就等于 `@file` 按字面文本进上下文 —— 这是与普通 `invoke` 的一处**有意差异**，需要让用户可预期。

**下行**

| 事件 | 时机 | 前端动作 |
|---|---|---|
| `steer.accepted` | stash 写入成功 | 条目进入队列条（若尚未显示） |
| `steer.absorbed` | **drain 时刻**（`agent_node` 入口，见 §3.2） | 条目从队列条移入 transcript，作为注入点波段 |

`steer.accepted` 与 `steer.absorbed` 都可以多次到达（重连重放），前端按 `steer_id` 幂等。

### 3.2 轮中吸收 —— `agent_node` 入口

注入点是 `agent_node` 入口，在重建 history **之前**（`graph.py:691` 起）：

```python
_drained = steer_drain(sid)                    # pop 出本会话 stash
steered = [HumanMessage(content=…, id=steer_id,      # 新 id，绝不复用 turnId
                        additional_kwargs={"ginno_steer": {…}}) for …]
if steered:
    await _ack(_drained)                       # config 注入的 emitter → 发 steer.absorbed
base = [*state["messages"], *steered]          # 追加在 tool 结果之后
history = strip_old_images(…); history = _wrap_steered_for_model(history)
return {"messages": [*steered, response], "pending_tool_calls": tool_calls}
```

为什么这里就是正确的时机：图结构 `START → agent → permission → tools → agent → … → END`（`graph.py:993-1001`），`agent_node` 每次进入都紧接在一次模型请求之前。工具批次跑完 → `tools → agent` 边 → 节点入口 drain → 消息进入**紧接着的那次**模型请求。**LangGraph 的超步边界 = Claude Code 的「工具调用结束」时机**，无需任何新调度器。

drain 出的 HumanMessage 放进**节点返回值**（与 `response` 同批），才会经 `add_messages` reducer 落到 state 与 checkpoint；顺序为 `[… tool 消息 …, 中途消息, response]`。

#### ack 必须在 drain 时刻发（实现期修正）

`steer.absorbed` 由 `stream.py` 注入到 `config["configurable"]["steer_absorbed"]` 的 async emitter 发出，**不是**等超步 commit。这一点是被真浏览器验证逼出来的：

- 早先的写法在 `updates` 流看到 `agent` 节点 commit 时才 ack。但节点 commit 发生在**整段回复流式输出完之后**，于是波段的插入点落在模型续写**之后**（实测：`tool 结果 → 续写 → 波段`），而回放路径按 state 顺序渲染出 `tool 结果 → 波段 → 续写` —— live 与历史不一致。
- 改成 drain 时刻 ack 后顺序正确（`tests/e2e/test_steering_ws.py` 的 ordering 断言就是这条回归测试）。

代价：ack 不再自动等于「已落盘」，因此需要 in-flight 兜底（§6 陷阱 1）。

**若模型这一轮没产生 tool_call**，`route_after_agent`（`graph.py:813-814`）直接到 END，注入机会自然消失 —— 剩下的消息落回 §3.3 的规则。这不是缺陷，而是与 Claude Code 那条「turn 结束仍有剩余 → 最老一条作为下一 turn」**结构同源**。

### 3.3 未吸收 → 前端作为普通 invoke 发出

runtime 在 turn 段结束时清空 stash（与 `_RUNNING_TURNS` 注销同一道 `not saw_interrupt` 闸门 —— parked 退出**不清**，留给 resume 吸收）。未吸收的条目由前端重发：

1. 收到 `message.end` / `turn.stopped` / `error` 后，凡**未收到 `steer.absorbed`** 的条目 → 取出**最老一条**，以普通 `invoke` 发出（新 turn，新 turnId）；其余留在队列条，等这一轮结束事件再按同规则处理。**⏹ 是例外**：条目退回输入框而非自动发出（§4.1）。
2. **对账兜底**：断线重连后的 `/history` 对账里，若某 `steer_id` 已在历史中（按 `additional_kwargs.ginno_steer.steer_id` 匹配）→ 从队列条移除，不重发。这条是防双投的关键：吸收已提交但 ack 丢包时不会重复发送。
3. **parked 前重发**：resume 之前把队列里的条目再以 `steer` 发一遍（`respond` / `respondPropose` / `answerQuestion` 都走这一步）。因为 parked 段结束时 stash 已被清，而用户可能正是那之前排的队。服务端 enqueue 按 `steer_id` 替换，所以重发不可能重复注入。

> 幂等性总账：`steer.accepted`/`absorbed` 可重放 → 前端按 id 幂等；未吸收 → 重发为 invoke；已提交但 ack 丢 → 由 `/history` 对账吸收。三者合起来保证 exactly-once。

### 3.4 parked 状态：答复 / 改主意（已确认）

turn 停在 interrupt 上时用户输入，**不入队**，走 resume 路径。按卡种分语义：

| 挂起类型 | 用户输入文本的含义 | 实现 |
|---|---|---|
| `permission_request`（`graph.py:783`） | **改主意**：不批这个工具，按新指令重来 | `_spawn_resume({"decision": "deny"})` + 同批 stash 该文本 |
| `version_propose`（`workflow_tools.py:185`） | 改主意：不采纳该 diff | 同上（共用 resume 通道，`stream.py:467-471`） |
| `user_question` 且 `allow_free_text`（`ask_tools.py:183`） | **答复** | `user_answer`，`answer = 文本`（`stream.py:476-497`） |
| `user_question` 且不允许自由文本 | 改主意/跳过 | `user_answer` with `skip: true` + 同批 stash 该文本 |

关键验证（本设计的省力点）：**permission 卡 deny 之后图会回到 `agent_node`** —— `graph.py:785-795` 是 `Command(goto="agent", update={messages:[AIMessage("… user denied")]})`。ask_user 同理：resume 返回值交给工具 → 工具产出 ToolMessage → `tools → agent`。所以「拒绝/跳过 + 改主意」**不需要任何新的注入机制**，喂给 §3.2 的同一个 stash 即可，模型看到的顺序是：

```
… AI: "[Bash] user denied"        ← deny 解释了工具为何没跑
   Human: "别改那个文件，先跑测试"  ← 中途消息（改主意）
   AI: …                          ← 按新指令重排
```

顺序由 stash 写入与 `_spawn_resume` 的**同步先后**保证（先写 stash，再 spawn）。

**parked 与轮中的 UI 差异**：parked 路径的输入**立即**渲染为全宽高亮气泡（用户刚答复了卡片，期望马上看到自己的话进入对话流），不经过队列条；absorbed ack 通常在一个超步内到达。

### 3.5 消息形状（持久化 vs 模型面）

**持久化的 HumanMessage**：

```python
HumanMessage(
    content=raw_user_text,                     # 用户原话，UI 直接显示，无需剥离前缀
    id=f"{steer_id}",                          # 新 id，绝不复用 turnId
    additional_kwargs={"ginno_steer": {
        "steer_id": steer_id, "turn_id": turn_id, "injected_at": epoch,
    }},
)
```

**模型面副本**（在 §3.2 重建 history 的 copy 链里加一步，与 `strip_old_images` / `strip_tool_image_markers` / `_mark_cache_tail` 同位置、同惯例 —— 只改副本，脏不得持久化状态）：

```
<ginno_steer>
The user sent a new message while you were working. Treat it as the newest
instruction and adjust the current work accordingly.
<message>
{原文}
</message>
</ginno_steer>
```

选择理由：Claude Code 把 `"The user sent a new message while you were working:"` 放进 **content** 里，于是它的 UI 必须解析/剥离前缀；Ginno 用 `additional_kwargs` 承载元数据（沿用 `agent_id`、`ginno_images` 的既有做法，`graph.py:668-681` 注释明确这些能 round-trip 过 checkpointer serde），持久化内容保持用户原话，**UI 不需要解析**。

**不复用** `goals/templates.py` 的 `GOAL_CONTEXT_PREFIX` / `<ginno_goal>` 折叠：那套会被 `_messages_to_ui` 折成居中 context 行，与已确认的「全宽高亮气泡」冲突（§4）。

### 3.6 与既有机制的交互

| 机制 | 交互 |
|---|---|
| **prompt cache** | `_mark_cache_tail`（`graph.py:595-622`）是滚动尾部标记；追加式增长下请求 N 的断点仍是 N+1 的前缀 → 命中，仅新增部分计费。与 Claude Code 的推断一致 |
| **compaction 计 turn** | `compaction.py:81` 与 `microcompact.py:119` 都按 `isinstance(m, HumanMessage)` 计 turn 边界、保留最近 `compact_keep_turns`（默认 3）轮原文。**中途消息不是轮首** → 两处计数需豁免带 `additional_kwargs.ginno_steer` 的 HumanMessage，否则频繁 steering 会让原文保留窗口提前滑动、压缩得更早更狠 |
| **`_TURN_STOP`** | 只在 turn 起点重新武装，`setdefault` 语义下半点复用会把新轮秒杀（`stream.py:335-343` 注释明确警告）。steering 不引入新的 turn 起点，因此**不触碰**；但要保证 steer 路径**绝不 set 该事件** |
| **stop（⏹）** | 见 §6 陷阱表 |
| **`_heal_interrupted_turn`** | 已 drain 但节点未返回时被 stop 的中途消息**不在 state 里**，需要 heal 兜底（§6） |
| **`begin_ask_budget` / `_INTERACTIVE`** | 中途消息在同一 asyncio task、同一 contextvar 上下文内，无需改动；goal 续轮把 `_INTERACTIVE` 置 False 时 steer 仍可用（用户在场） |
| **goal 续跑** | 用户消息永远优先（goal-design §0）。steer 吸收后若 goal 仍 active，续跑照旧；`render_objective_updated`（`templates.py:91`，定义未调用）与 steer 是**两条独立路径**，不要合并 —— 它走 `[goal context]` 折叠，steer 走全宽气泡 |
| **agent_override** | 中途消息的 `plan.agent_override` **v1 忽略**（运行中的 turn agent 已固定），记录为已知限制 |

---

## 4. UI（三项已确认）

### 4.1 排队态 —— 输入框上方队列条

```
⏺ Claude 正在运行
  正在读取 stream.py…

待发送 (2)
  ① 别改那个文件，先跑测试
  ② 顺便更新一下 README

┌─────────────────────────┐    ⏹
│ 输入…                    │
└─────────────────────────┘
```

- 位置：composer 内联在 `ChatStream.tsx:2640-2712`，队列条紧贴 textarea **上方**。
- 序号 + 单行截断；`↑`（textarea 首行时）取回全部到输入框、一行一条，置于已输入文字之前（对齐 Claude Code）。
- 单条删除：每条带 ✕。
- 停止按钮（`running && !parked` 时是 ⏹，`ChatStream.tsx:2876-2884`）**保持不变**。
- **⏹ 时队列条目退回输入框（已确认）**：未吸收条目按原顺序**多行拼接**（`\n` 连接）写回 textarea，供用户编辑后再发 —— 不静默丢弃；若 textarea 原本有内容，退回文本置于其**之前**（与 `↑` 取回一致），并清空队列条。

### 4.2 注入态 —— 全宽高亮 + 标签

```
⏺ Bash   pnpm test
✓ 3 passed

┃ ⟳ 运行中注入 · 09:41 │ 别改那个文件，先跑测试
好，先跑测试，不动 stream.py。
```

实现后是**紧凑单行**（~26px 高，与一行正文相当）：标签是行内小 chip，与用户原话同一行，左侧橙色竖线 + 7% 橙色底作为「插入点」标记。

标签时间取 `additional_kwargs.ginno_steer.injected_at`（本地时区格式化）；历史回放同样渲染，保证重开会话可辨。

**颜色规则（实现期定）**：用**橙色**，并刻意避开思考块的造型。初版波段照抄了 `ThinkingBlock` 的样式（violet + 独立标题行），结果既被当成第二个思考面板，又为一行字占掉两行高度。避让表：

| 色 | 已被占用为 |
|---|---|
| violet | 思考块 / agent 流式光标 / workflow / 发送按钮 |
| blue | web 引用链接 |
| green | 成功（工具的 ✓、已选择） |
| yellow | 需要你注意（human interrupt、AskUserCard 可交互、需求项） |
| red | 停止 / 错误 |
| **orange** | **（无占用）→ 中途注入** |

### 4.3 同 turn 续写 —— 同一气泡内分隔线

```
╭─ Claude ────────────────────────────╮
│ 我先看一下实现。                     │
│ ⏺ Read stream.py   ✓                │
│ ··································  │
│ ⟳ 已接收中途消息                     │
│ 好，先跑测试，不动 stream.py。        │
│ ⏺ Bash pnpm test   ✓                │
╰─────────────────────────────────────╯
```

一个 turn = 一个 assistant 气泡；分隔线位置由「本 turn 内第一次 drain 发生的时间点」决定。前端在 `steer.absorbed` 到达时打标记，气泡渲染时据此插入分隔线（与 `tool.start`/`tool.end` 的插入点同层）。

### 4.4 状态机

| 状态 | 展示位置 | 触发转入 | 触发转出 |
|---|---|---|---|
| `queued` | 队列条 | 用户提交，turn 在跑 | absorbed / turn 结束 / ✕ |
| `absorbed` | transcript 全宽高亮 | `steer.absorbed` | —（终态） |
| `resending` | 队列条（标记） | turn 结束未吸收 | 转普通 invoke 后移出队列 |
| `parked-immediate` | transcript 全宽高亮 | parked 时输入 | 同 `absorbed`（不排队） |

---

## 5. 落点清单

| 文件 | 改动 |
|---|---|
| `packages/runtime/src/ginno_runtime/server_shared.py` | `_STEER_STASH` + `steer_enqueue`（按 `steer_id` 替换）/ `steer_drain` / `steer_clear`；`_STEER_INFLIGHT` + `steer_mark/take/clear/restash_inflight` |
| `packages/runtime/src/ginno_runtime/api/stream.py` | `steer` 消息分支（内置命令短路 / 丢弃 mention / 无回合时回 error / `steer.accepted`）；`_steer_absorbed` emitter 注入 `config`；`updates/agent` 分支改为退休 in-flight；段结束按 `not saw_interrupt` 清 stash（parked 退出则 restash）；`_heal_interrupted_turn` 落 in-flight 的中途消息 |
| `packages/runtime/src/ginno_runtime/graph.py` | `agent_node` 入口 drain + 调 emitter + 模型面 wrap 副本 + 返回值带上中途消息；`_wrap_steered_for_model()` |
| `compaction.py` / `microcompact.py` | `find_split_index` 计 turn 边界时豁免带 `ginno_steer` 标记的 HumanMessage（`is_steered()` —— 一处改动同时覆盖两者） |
| `api/messages_ui.py` | 回放：中途消息**不 flush** assistant 累积器，改为在气泡内插入 `{kind:"steer"}` 波段；早于首个 assistant step 时先挂 pending 再前置 |
| `apps/web/src/components/chat/blocks.tsx` | `Block` 新增 `steer` 变体 + `SteerBand` 组件（全宽高亮 + `⟳ 运行中注入 · HH:MM`） |
| `apps/web/src/components/chat/ChatStream.tsx` | `SteerItem` / `steerQueueRef` + `steerQueue` 镜像、`enqueueSteer`/`sendSteer`/`requeueSteersOnResume`/`recallSteers`/`flushSteerQueue`/`dropAbsorbedSteers`、`handleParkedSend`（答复 vs 改主意）、队列条 UI（composer 上方，含 ✕ 与 ↑ 取回）、`steer.accepted`/`steer.absorbed` 处理、`message.end`/`error` 冲队列、`turn.stopped` 退回输入框、两处 `/history` 对账去重、standalone 波段的整行渲染 |
| `packages/runtime/tests/e2e/test_steering_ws.py`（9 例） + `tests/unit/test_steering.py`（12 例） | 轮中吸收 + **ack 早于续写 token**（顺序回归）、批量顺序、无 turn_id 回退、无回合时拒绝、stop 后不 ack 不留痕、parked deny/改主意、stash 生命周期、compaction 豁免、模型面包裹与转义、回放波段位置 |

**明确不动**：`checkpointer.py`（`editResend` 隐患已定单开任务，§8 决议 8）、`PinStream.tsx`（速聊窗跟进项）、goal 的 `[goal context]` 折叠通道与 `render_objective_updated`。

---

## 6. 风险与陷阱

| # | 陷阱 | 处理 |
|---|---|---|
| 1 | **drain 后、节点返回前被 stop** → 消息已出 stash、ack 已发（前端已把它移出队列条），却未落 state | **已实现**：drain 时记入 `_STEER_INFLIGHT`；超步 commit 时退休；被 stop 则由 `_heal_interrupted_turn` 随派生 turn_id `{turn_id}:stop` 一起落 state（沿用该方法既有的 aupdate_state 通道）；parked 退出则 restash 给 resume 重吸收 |
| 2 | **stop 与队列**：⏹ 之后 stash 若不清理，下一轮会吸收上一轮的残留 | turn 的 `finally` 清 stash；前端在 ⏹ 时把队列条条目**退回输入框**（不静默丢弃，已确认，见 §4.1）|
| 3 | **id 复用** | steered 消息一律新 id。`checkpointer.py:214-232` 只按 id 比对内容，复用 id 改文本 → delta 存空 items、重建历史留旧内容。`editResend` 复用 turnId（`ChatStream.tsx:2200-2206`）是**既有隐患** —— **已定：单开任务，本次 `checkpointer.py` 不动** |
| 4 | **双投** | 三层幂等（§3.3）。最危险的是「已提交但 ack 丢包」→ 必须靠 `/history` 对账的 `steer_id` 匹配 |
| 5 | **`_TURN_STOP` 误触** | steer 路径绝不 set 该事件；只有 `stop` 分支 set |
| 6 | **compaction 窗口滑动** | §3.6 的豁免。不做豁免也能跑（Claude Code 同样把它当 user 消息），但会更早压缩 |
| 7 | **`PinStream.tsx` 第二副本** | 速聊窗有独立 busy 门控与 invoke（`:509-560`）。v1 不动 → 行为不一致；跟进时抽出共享队列组件 |
| 8 | **UI 与 goal context 行冲突** | steer 消息**不带** `GOAL_CONTEXT_PREFIX`，否则被 `_messages_to_ui` 折成居中行，与 §4.2 冲突 |

---

## 7. 验收

1. **轮中吸收**：turn 跑工具时发 `steer` → 该消息出现在**同一 turn** 的下一次模型请求里（日志可按 `steer_id` grep），transcript 顺序为 `tool 结果 → 波段 → assistant 续写`。
2. **超步边界**：模型连续多轮工具调用时，steer 在**最近一个**工具批次结束后被吸收（不等到 turn 结束）。
3. **未吸收路径**：模型纯文本回答（无 tool_call）时发 steer → turn 结束、消息作为新 turn 发出，历史中只有一条。
4. **stop 一致性**：drain 后立即 ⏹ → 消息不丢（heal 落 state）且不重复。
5. **parked**：permission 卡上输入文本 → 工具被 deny、模型按新指令重排；ask_user 卡上输入文本 → 作为答案返回（`allow_free_text` 为真时）。
6. **cache 不劣化**：同一会话连续 steer 时，`cache_read` 覆盖前缀、只有新增部分计费（对照 `usage/requests-*.jsonl`）。
7. **重开会话**：中途消息在历史回放里仍是全宽高亮 + 标签（`ginno_steer` round-trip 通过 checkpointer）。
8. **compaction 计数**：一次 turn 内注入 3 条中途消息后，原文保留窗口不因这 3 条而滑动。

---

## 8. 决议记录

无待定项。全部决议：

| # | 决议 | 落点 |
|---|---|---|
| 1 | 只做**轮中吸收**；未吸收的由前端以普通 `invoke` 开新轮 | §0、§3.3 |
| 2 | parked 状态输入当作**答复/改主意**，走 resume 而非队列 | §3.4 |
| 3 | 同 turn 内**同一 assistant 气泡** + 分隔线 | §4.3 |
| 4 | **不做** `Ctrl+Enter` 发送即中断；失败重试**继续复用** turnId | §1.3、§2 |
| 5 | ⏹ 时队列条目**退回输入框**、多行拼接、可编辑 | §4.1 |
| 6 | 附件与 `@mention` **不进**轮中，按字面文本交给模型 | §2、§3.1 |
| 7 | UI：队列条（输入框上方）+ 全宽高亮注入态 + 同气泡分隔线 | §4.1–4.3 |
| 8 | `editResend` 复用 turnId 的既有隐患（§6 陷阱 3）**单开任务**，本次 `checkpointer.py` 不动 | §5、§6 |

---

## 附录：与 Claude Code 的有意差异

| 项 | Claude Code | Ginno（本设计） | 理由 |
|---|---|---|---|
| 队列归属 | CLI 进程内 | **前端**持有，runtime 只有吸收窗口 | 只做轮中吸收（已确认）；前端本就是多标签页/重连对账的主体 |
| 未吸收的剩余 | 服务端把最老一条作为下一 turn | 前端以普通 `invoke` 发出 | 同上，且复用现成的 turn 起点逻辑（`_TURN_STOP` 武装、`_RUNNING_TURNS` 注册） |
| 模型面提示词 | 写在 content 前缀里 | `additional_kwargs` + 模型面副本包裹 | 持久化内容保持用户原话，UI 免解析 |
| 发送即中断（Ctrl+Enter） | 有 | **不做**（已确认） | 与 ⏹ 语义重叠，先不引入第二套中断入口 |
| 多条折叠 | 换行拼接成一条 user 消息 | v1 **不折叠**，逐条吸收 | 逐条渲染更贴合「全宽高亮气泡」UI，且避免拼接后无法按条对账 |
| 中途消息建 checkpoint | 不建 | 建（随节点返回值落 state） | Ginno 的历史/续跑依赖 checkpoint 完整性 |

---

## 实现记录（2026-09-21）

改动文件见 §5 落点清单。验证：

| 项 | 方式 | 结果 |
|---|---|---|
| 轮中吸收 / 批量顺序 / 无 turn_id 回退 / 无回合拒绝 / stop 不留痕 / parked deny+改主意 / stash 生命周期 | `tests/e2e/test_steering_ws.py`（9 例） | 通过 |
| compaction 豁免 / 模型面包裹与转义 / 回放波段位置 / stash 幂等 | `tests/unit/test_steering.py`（12 例） | 通过 |
| 全量回归 | runtime 全套 1209 例 | 通过（`test_packaged_ui_ask_user.py` 有 2 例失败，改动前后的干净树**都**失败，与本功能无关） |
| **真浏览器**（dev web:3000 + dev runtime:8899，隔离 `GINNO_HOME`，脚本化慢工具 `sleep 22`） | 队列条出现 → 波段落在 `tool 结果` 与 `续写` **之间**、且在**同一个** assistant 气泡内 → 刷新后位置不变 → ⏹ 把排队文本退回输入框 | 全部通过 |

**实现期推翻的一处设计假设**：原方案让 `steer.absorbed` 在超步 commit 时发，理由是「ack 即等于已落盘」。真浏览器暴露了它的问题 —— commit 发生在整段回复**流式输出之后**，波段因此被插到续写**之后**，而回放路径按 state 顺序渲染，两者不一致。改为 drain 时刻 ack，并用 `_STEER_INFLIGHT` + heal 把「已落盘」这一保证补回来（§3.2、§6 陷阱 1）。e2e 中那条 ordering 断言就是这个 bug 的回归测试。

**已知限制**（不在本次范围）：

1. **队列是内存态**：刷新页面会丢未吸收的排队条目（已吸收的仍在历史里）。持久化需把 `steerQueueRef` 落到 localStorage 或后端，并处理跨标签页去重。
2. **轮中不支持附件 / `@mention`**（已确认）：带附件的输入在运行中不排队，composer 原样保留，等 turn 结束再发。
3. **`PinStream.tsx` 速聊窗未跟进**：行为与主窗口不一致；跟进时应抽出共享的队列组件。
4. **goal 续轮**：清理走 `_stream_graph` 的同一道闸门，因此无需单独处理；其可吸收性依赖 `_run_goal_turn` 提前注册 `_RUNNING_TURNS`（`api/sessions.py:244`）。
| `@mention` / 附件 | 排队消息照常解析 | v1 不解析，按字面文本进上下文（已确认） | 收窄轮中注入面，避开与图片裁剪 / 文件注入的时序耦合 |