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
- auto 仅对 `reason=dev-swap` 生效（崩溃/信号恢复默认 notify——自动续跑烧 token 且可能有副作用动作，按 reason 分级）；
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
