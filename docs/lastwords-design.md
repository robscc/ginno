# 遗言模式（Last Words）：热切换重启后的自动会话恢复

> 来源：2026-11-07 会话决策。痛点实测：dev-swap 热切换重启 sidecar 后，被打断的
> turn 只能靠 checkpoint 被动恢复——agent「失忆」于自己正在干什么，需要用户手动
> 说"继续"。遗言模式让重启变成**主动续跑**。
> 落层：runtime 为主（快照+扫描+注入）+ `dev_swap.sh` 一处调用 + 设置项。
> 预估：P0 ~4h · P1 ~4h。

## 0. 一句话

sidecar 在被热切换重启**前**，把进行中 turn 的关键上下文写成「遗言」落盘；
重启**后**扫描遗言，自动注入回原 session——agent 被唤醒接着干，用户无感或收到恢复通知。

## 1. 生命周期

```
dev_swap.sh（switch 前）
  → POST /api/devswap/lastwords/prepare     ← 显式预告（首选，sidecar 有充分时间落盘）
  → SIGTERM 兜底 handler（进程级，防漏）
       │  sidecar：遍历 _RUNNING_TURNS
       │  每个活跃 turn 写 ~/.ginno/lastwords/<session_id>.json
       ▼ 进程被替换、壳重启 sidecar
新 sidecar 启动（server.py 装配段）
  → 扫描 lastwords/*.json（TTL 1h 内）
  → 按 mode 注入原 session → 删除文件（exactly-once）
       ├─ notify（默认）：注入一条 [lastwords] 系统消息 + 通知中心未读
       └─ auto（setting）：headless turn 自动续跑「请继续完成：<遗言摘要>」
```

## 2. 遗言内容（结构化快照，不额外调 LLM）

```json
{
  "session_id": "1a0c…", "project": "default",
  "reason": "dev-swap",            // dev-swap | signal | crash-partial
  "fingerprint_old": "a9b145b",
  "user_request_head": "…原始请求前 500 字",
  "last_agent_message": "…最后一条回复前 500 字（含进行中工具卡上下文）",
  "running_tools": ["dev-swap-force"],   // bg 任务 id：重启后 bg_logs 可收割
  "pending_intent_hint": null,      // P1：plan/todo 提炼
  "ts": 1794023094
}
```

MVP 不调 LLM 组装遗言（重启窗口宝贵）；P1 用 turn 内已有的 plan/TODO 状态增强摘要质量。

## 3. 注入机制（复用既有基建）

- **notify 模式**：走 `tasks/inject.py` 同一通道注入 HumanMessage（`[lastwords] 重启恢复：…` 前缀）——首行形态同时喂给 #29 的 `foldTaskEventBlocks` 家族（新增 `[lastwords]` 识别），重放也不露原文带；
- **auto 模式**：headless turn（scheduler 同款）注入 `继续完成 <user_request_head 摘要>；上次进行到 <last_agent_message 摘要>；后台任务 <running_tools> 可收割`；
- exactly-once：注入成功（或 TTL 过期）即删文件；注入失败（session 已删）静默丢弃 + 日志。

## 4. 设置与安全

- `settings.json → devswap.lastwords.mode: "notify" | "auto" | "off"`（默认 notify）；
- auto 仅对 `reason=dev-swap` 生效（崩溃/信号恢复默认 notify——自动续跑烧 token 且可能有副作用动作，按 reason 分级）；**subagent 型会话放宽**：auto 对 reason∈{dev-swap, signal, running} 都自动续跑（首跑追加——子会话本就 headless，无人点「继续」，自动续跑零打扰；人工会话维持仅 dev-swap）；
- 遗言注入消息显式标注来源与旧指纹，agent 与用户都可辨识；auto 起的 turn 带 `via:"lastwords"` 标记，用量面板可归因。

## 5. 分期

| 期 | 内容 | 量 |
|---|---|---|
| P0 | prepare API + SIGTERM 兜底 + 快照落盘 + 启动扫描 + notify 注入 + exactly-once/TTL + dev_swap.sh 接线 + 单测（fastapi TestClient 优雅关闭模拟） | ~4h ✅ |
| P1 | auto 模式 headless 续跑 + setting 开关（设置页）+ plan/todo 摘要增强 + `[lastwords]` 折叠族 | ~4h ✅ |
| P2 | 崩溃半遗言（SIGKILL 前 flush 尽力而为）、多 session 待恢复列表进通知中心 | 挂起 ✅ |

## 6. 边界（不做的）

- 不替代 checkpoint（checkpoint 管消息历史不丢；遗言只管「唤醒信号」）；
- 不跨设备/不进云（本地文件，`~/.ginno` 内）；
- 不做 Windows 服务式托管重启（桌面壳已负责拉起 sidecar）。

## 7. 实现决策补记（2026-11-07 落地版）

- **注入通道实选**：notify 不复用 tasks/inject 的 wake-turn 路径（那是
  「事件回来必须让 agent 反应」的语义，会跑 turn 烧 token）——改为
  `graph.aupdate_state(config, {"messages": [HumanMessage(...)]}, as_node="agent")`
  直接追加折叠带（compaction/heal 同款先例）：不跑 turn、agent 下次被唤醒
  时可见、消息带 `ginno_task_event` 戳走 messages_ui 既有折叠投影。
- **折叠实现**：runtime 注入消息带 `ginno_task_event`（kind:"lastwords"），
  messages_ui 零改动折出 task_event block；前端 text 兜底路径（taskEventParse）
  新增 `[lastwords]` 首行识别折 `ev:"lastwords"`（taskId 合成 "lastwords"、
  title 取首行余下），双保险。
- **P2 WAL 落点**：turn 启动（engine._stream_graph 注册 _RUNNING_TURNS 后）
  写初始遗言 + 工具边界（graph.tools_node 入口）增量覆盖，节流
  **1 次/5s/turn**（首写不受限）；turn 正常收尾（非 interrupt 停车）
  删遗言——绝不误恢复已完成 turn。未用 checkpointer put 钩子：那里每
  superstep 都触发，粒度不合适且侵入 checkpointer。
- **通知中心**：注入成功后 `_push_global_event("runtime.lastwords_restored")`，
  前端 handle.ts 投 pushNotice（kind:"lastwords"、countInBadge:true、
  dedupeKey:`lastwords:<sid>`、actionMeta kind:"session" 跳回原会话）。
  「继续」按钮未做（actionMeta 仅支持 session 跳转；手动续跑需新增
  actionMeta 类型 + runtime resume API，收益不抵复杂度——恢复带本身已让
  agent 下轮可见上下文，auto 模式可在设置打开）。**决策：纯展示+跳转。**
- **auto 归因**：turn config 带 `usage_source:"lastwords"` + `origin:"lastwords"`
  （随 message.end 下发），用量面板可归因；起点消息带 ginno_task_event 戳
  折叠，重放不露续跑模板长文。
- **SIGTERM 兜底**：挂在 lifespan finally 首段（uvicorn 优雅关闭
  timeout_graceful_shutdown=3s 窗口内），同步快速 IO（checkpoint JSONL 读
  + 原子写）；prepare API 的写走 `asyncio.to_thread` 不阻塞事件循环。
- **P1 偏差**：plan/todo 摘要增强（§5 P1 行）未做——快照已含
  user_request_head/last_agent_message/running_tools/bg_tasks 四元组，
  续跑模板据此已足够定位中断点；plan 提炼需 turn 内状态快照通道（侵入
  graph state），收益边际，挂起待真实使用反馈。
- **验证深度**：单测 30 例 + 全量回归 3216 绿 + vitest 158 绿；**真实
  dev-swap 端到端未做**（需跑一次 make dev-swap 观察遗言写入与重启后
  注入——留给父会话人工验证）。

## 8. 首跑问题与质量修（2026-11-07 用户重启实测复盘）

首跑实况：重启后收到注入 `[lastwords] running 重启恢复：，额偶皮老妈 ·
后台任务 1 个可收割(bg_logs)`——三个缺陷：①重启前最后一句闲聊感叹被当
"待恢复工作"注入（噪声）；②reason=running（SIGTERM 兜底未升级）；③"1 个
可收割"的 bg 任务实际早已完成（快照过时）。

### 8.1 SIGTERM 兜底为什么没升级 reason（诊断结论）

桌面壳两条杀 sidecar 路径（apps/desktop/src/sidecar.rs）：

1. **优雅退出**（`stop_child_gracefully`，Cmd+Q / Debug 重启）：SIGTERM
   + 6s 窗口。uvicorn `timeout_graceful_shutdown=3` 到期后继续走 lifespan
   shutdown（`write_lastwords(signal)` 在此执行）。但 asyncio loop teardown
   会 cancel 残余 turn 协程 → engine finally 的 `note_turn_end` **把刚写好
   的 signal 遗言删掉**。修复：`mark_shutdown()`（server.py lifespan
   finally 首段置位）后 `note_turn_end` 不再删遗言。
2. **退出后立即重启**（`kill_stale_sidecar`，首跑实况走的这条路）：SIGTERM
   后仅 ~100ms 就对端口未释放的进程升级 SIGKILL，而 uvicorn 优雅关闭全程
   ≥3s——`write_lastwords(signal)` **结构性来不及执行** → WAL 的
   reason=running 残留。这是壳的有意设计（防端口被僵死进程占死），Python
   侧无法补救；若要堵死需放宽 escalate 窗口到 ≥3.5s（Rust 改动，影响启动
   时序，另行评估）。

**结论：reason=running 的 WAL 遗言是重启场景的唯一防线，consume 侧的
门槛过滤与 15min TTL 是对 WAL「无差别快照」的补偿闸。**

### 8.2 恢复价值门槛（consume 时判定，write 不动）

- **信号**：WAL 写点维护进程内 effort 计数（turn 累计工具调用数 +
  turn 起始 wall-clock 时间戳），随快照落盘 `effort` 字段。计数在节流
  **之外**每次更新——节流只丢写盘频率绝不丢计数；`write_lastwords`
  （dev-swap/signal）也合入当前计数。输出量级不单列字段：`last_agent_message`
  已可近似，门槛只用工具数 + 时长两个可精确维护的信号。
- **规则**（保守，宁缺勿滥）：工具调用 ≥3 **或** turn 时长 ≥120s；
  显式优雅关闭（dev-swap/signal）门槛减半（2 工具 / 60s——明确重启场景
  更值得恢复）。低于门槛 → consume 时静默丢弃（删文件 exactly-once +
  debug 日志）。
- **settings 覆盖**：`devswap.lastwords.min_effort = {"tool_calls": int,
  "turn_seconds": float}`（`tool_calls=0` 关闭工具维度门槛）。
- **保守放行边界**：快照缺 `effort` 字段（旧版本文件）不判门槛直接
  放行——门槛只过滤「明确低价值」，不误杀不可判定的。
- 阈值依据：闲聊 turn 实测量级 0 工具 / <60s；最小有意义工作单元是
  「读文件→改→验证」三步工具循环（≥3）；纯生成长回复（无工具）以 120s
  时长兜底。

### 8.3 bg_tasks 注入前实时核对

快照是某个工具边界时刻的，bg 任务状态在其后继续演化。注入前对照
`/api/tasks` 同款数据源（`tasks/store.read_index`）：已结束的移除、全部
结束则省略该段；核对源不可达保留原列表 + render 加「状态未知」后缀
（宁可多提示一次收割，不静默吞掉可能在跑的任务）。

### 8.4 reason=running TTL 收紧

1h → **15min**：SIGKILL 残留的 running 态过期更快（用户大概率已自行
重新投入）；dev-swap/signal 保持 1h（显式优雅关闭，恢复价值窗口长）。

### 8.5 subagent 会话的 auto 放宽（首跑追加）

app 重启（退出重开）杀掉的 subagent turn 只有 notify（注入 + 通知等
人工点「继续」）——但子会话本就 headless，无人盯着通知，恢复链路断在
最后一步。放宽：mode=auto 时 subagent 型会话（判据 = 会话元数据
`type=="subagent"`，与 parent_session_id/depth 同源，勿凭会话名猜测）
对 reason∈{dev-swap, signal, running} 都起 headless 续跑 turn
（origin/usage_source=lastwords 归因，§3 续跑模板不变）；人工会话维持
仅 dev-swap（有真人在看，自动续跑反而打扰）。mode=notify 下 subagent
同样只注入——放宽只作用于 auto。

### 8.6 验证

门槛过滤矩阵 / bg 核对（mock 任务表三态）/ running TTL / WAL 计数不丢 /
优雅关闭保护 / subagent auto 放宽（signal/running 续跑、notify 模式不
放宽、低价值正交丢弃、meta 判据回落）共 30 个新用例，累计 60 例全绿；
全量回归见 PR CI。
