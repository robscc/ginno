# implementation-notes-i18n-events.md — W8 runtime 事件契约全切对照表

> 任务 #6（P2）：runtime → web 用户可见事件文案一次性全切 `i18n_key + params`。
> 方法：附录 A 枚举（`_ev` / `_push_session_event` / notice payload 全量扫描）。
> 设计依据：i18n-design.md §3（契约）、§5（双轨制）、§6（prompt 层）、附录 A。

## 0. 分流规则落地（§5 双轨制到本任务的映射）

| 轨道 | 判据 | 处理 |
|---|---|---|
| **事件契约 key** | 结构性小文案、固定发射点、payload 是事件模板 | `message/text` 保留英文兜底 + `i18n_key` + `params`；key 落 web catalog |
| **内联 `t(en,zh)`** | 注入模型上下文的文本；**以及**命令回复/计划确认等**ephemeral notice**（notice 从不进 checkpoint、永不回放，渲染时 locale 即发射时 locale） | 发射点当场渲染成品双语字符串 |
| **runtime `_()`** | 无请求上下文的后台任务（scheduler 落库行），settings locale 兜底 | runtime `i18n/{en,zh_CN}.json` |

为什么 ephemeral notice 走 t() 而不是 key：`/help`、`/compact` 报告、拆分方案列表是
**动态复合文本**（内嵌技能清单/统计数字/子任务列表），单条 ICU 模板无法表达；
§5 明确大段文本保留「文案即代码」轨道。历史回放不涉及 notice → 无回放漂移风险。

## 1. 事件契约发射点对照表（stream 域，web catalog `messages/{en,zh-CN}/stream.json`）

| # | 发射点（文件:行为） | 事件 | i18n_key | params | 前端渲染点 | en / zh 文案（catalog） |
|---|---|---|---|---|---|---|
| 1 | api/stream/ws.py `session_ws` 未知会话 | `error` | `stream.error_unknown_session` | `{id}` | 错误卡（chat） | Unknown session: {id} / 未知会话：{id} |
| 2 | ws.py 非 JSON 帧 | `error` | `stream.error_invalid_json` | — | 错误卡 | Invalid JSON / 无效的 JSON |
| 3 | ws.py 未知消息类型 | `error` | `stream.error_unknown_type` | `{type}` | 错误卡 | Unknown message type: {type} / 未知的消息类型：{type} |
| 4 | ws.py invoke busy（async 命令） | `notice` | `stream.busy_wait_command` | `{command}` | notice 气泡 | A turn is already running; wait…/{command} / 已有回合正在运行；请等它结束后再运行 /{command} |
| 5 | ws.py async 命令执行异常 | `notice` | `stream.command_failed` | `{command,error}` | notice 气泡 | /{command} failed: {error} / /{command} 失败：{error} |
| 6 | ws.py invoke busy（普通回合） | `notice` | `stream.busy_wait` | — | notice 气泡 | A turn is already running; please wait… / 已有回合正在运行，请等待其完成 |
| 7 | ws.py turn_job 异常 | `error` | `stream.turn_failed` | `{error}` | 错误卡（含重试按钮） | Turn failed: {error} / 回合失败：{error} |
| 8 | ws.py resume_job 异常 | `error` | `stream.turn_failed` | `{error}` | 同上 | 同上 |
| 9 | ws.py invoke 内联阶段异常 | `error` | `stream.turn_failed` | `{error}` | 同上 | 同上 |
| 10 | ws.py checkpoint retry 异常 | `error` | `stream.turn_failed` | `{error}` | 同上 | 同上 |
| 11 | ws.py steer 分流 async 命令 | `notice` | `stream.command_only_after_turn` | `{command}` | notice 气泡 | /{command} can only run after the current turn finishes / /{command} 只能在当前回合结束后运行 |
| 12 | ws.py steer 无回合可吸收 | `error` | `stream.no_turn_running` | — | 错误卡 | No turn is currently running; please send again / 当前没有正在运行的回合，请重新发送 |
| 13 | ws.py subagent.plan.confirm 失败 | `notice` | `stream.plan_confirm_failed` | — | notice 气泡 | Failed to confirm the split plan; retry or split again / 确认拆分方案失败；请重试或重新拆分 |
| 14 | ws.py retry_from_checkpoint busy | `notice` | `stream.busy_wait` | — | notice 气泡 | 同 #6 |
| 15 | engine.py 瞬断自动重试预告 | `notice` | `stream.auto_retry` | `{error,seconds,attempt,max}` | notice 气泡 | Model connection error ({error}); auto-retrying in {seconds}s ({attempt}/{max})… / 模型连接异常（{error}），{seconds} 秒后自动重试（{attempt}/{max}）… |
| 16 | engine.py turn 兜底异常 | `error` | `stream.turn_failed` | `{error}` | 错误卡 | 同 #7（last_error 持久化同享该 key 的语义） |

## 2. summary 域（web catalog `messages/{en,zh-CN}/summary.json`，历史回放行）

| # | 发射点 | 载体 | i18n_key | params | 前端渲染点 | en / zh |
|---|---|---|---|---|---|---|
| 17 | world_state.summary_row_parts ← messages_ui（GET /history） | context block | `summary.compacted_row` | `{preview}` | 转录居中 context 行 | 🗂 Earlier conversation compacted into a summary{preview} / 🗂 此前对话已压缩为摘要{preview} |
| 18 | world_state.reinject_row_parts ← messages_ui | context block | `summary.reinjected_row` | — | 同上 | 🌍 Current world state re-injected / 🌍 当前世界状态已重新注入 |

契约字段名（历史块）：`{"kind":"context","text":<英文兜底>,"i18n_key":<dot path>,"params":{...}}`。
WS 事件：`{"message":<英文兜底>,"i18n_key":…,"params":…}`（params 可选，无占位符时省略）。

## 3. core.json notify 子树（OS 通知文案；接线归 W1——.ts/.tsx 在本任务禁区）

| key | params | 现英文出处（接线点） | en / zh |
|---|---|---|---|
| `notify.turnDoneBody` | — | useChatStreamEngine.ts:1322 "Reply completed" | Reply completed / 回复已完成 |
| `notify.workflowDoneBody` | `{duration}` | store.tsx "Completed · took …" | Completed · took {duration} / 已完成 · 用时 {duration} |
| `notify.workflowFailedBody` | — | store.tsx "Run failed" | Run failed / 运行失败 |
| `notify.workflowFailedAt` | `{step}` | store.tsx "Failed at \"…\"" | Failed at "{step}" / 在「{step}」失败 |
| `notify.workflowDefaultName` | — | store.tsx "Workflow" | Workflow / 工作流 |

说明：现实现中 OS 通知文案由 web 生成（design §7 的「runtime 传成品字符串」是目标态），
本任务不动 .tsx，故 key 先行入库，接线由 W1/后续任务完成。

## 4. runtime `_()` catalog（`ginno_runtime/i18n/{en,zh_CN}.json`——后台任务渲染）

| key | params | 发射点 | en / zh |
|---|---|---|---|
| `schedule.run_succeeded` | — | scheduler.py `_execute_run` 状态行（run 行 summary 兜底） | Succeeded / 已成功 |
| `schedule.run_failed` | `{error}` | 同上 | Failed: {error} / 失败：{error} |
| `schedule.run_interrupted` | — | scheduler.py 停机取消落行 | Run interrupted (app quit) / 运行被中断（应用退出） |
| `schedule.missed` | — | scheduler.py `_record_missed` | Missed (machine asleep or app not running) / 已错过（机器睡眠或应用未运行） |
| `schedule.default_task_title` | — | scheduler.py 影子会话标题兜底 | Scheduled task / 定时任务 |
| `errors.session_not_found` | `{id}` | P0 既有占位（保留） | Session {id} not found / 会话 {id} 不存在 |

## 5. 内联 t() 轨道（本任务新增/修正的双语点；不改 key）

| 位置 | 性质 | 处理 |
|---|---|---|
| engine.py 递归预算预警 steer | 模型上下文 | 原硬编码中文 → `t(en, zh)` 双语 |
| turn.py `_attach_only_text()`（原 `_ATTACH_ONLY_TEXT` 常量） | 用户气泡 + 模型输入 | 硬编码中文 → `t()`，随请求 locale |
| world_state.render_reinjection / summary_lead_in | 模型上下文 | 已是 t()（P0 就绪，核验通过） |
| goals/templates.py context_row_text 兜底行「🎯 目标推进」 | 转录行 | → `t("🎯 Goal progress", …)` |
| commands/registry.py 全部命令回复 + BUILTINS.description（改双语元组，t() 在回复时选边） | ephemeral notice | 英文硬编码 → `t(en, zh)` |
| subagent_plan.py 拆分/确认/校验文案 | ephemeral notice / ValueError → notice | 英文硬编码 + 一处硬编码中文（`拆分失败`）→ 统一 `t()` |
| subagent_scheduler.py 并发 80% 预警 | ephemeral notice | 英文硬编码 → `t()` |
| graph.py `build_stable_system` | system prompt | **新增注入 `response_lang_directive()`**（persona 之后、world 之前；locale 稳定 → 前缀缓存不失效，切换语言合法失效） |

## 6. 不翻译清单（核验通过，保持原样）

- fork 标记（`ginno_steer`/`ginno_subagent_fork` 等 additional_kwargs 键）
- split 关键字（`split` / `拆分`，registry._split_form_task 双关键字协议）
- 协议保留字：`[conversation summary]`、`[world state re-injection]`、`[turn context]`、
  `[goal context]`、`[world state update]`（ALL_CONTEXT_PREFIXES，UI 靠它们折叠行）
- goal status 协议值（active/paused/…，仅 *_LABELS 展示层翻译）
- subagent.* / run.* 事件中的 status 字段
- 日志（sidecar.log 全英文，未动）
- `…(truncated, N chars total)`（messages_ui._truncate_for_ws）— 工具输出体**内部**的
  截断装饰，无法以独立字段携带 key；保持英文（协议装饰），如需翻译需改 payload 结构（遗留项）

## 7. 显式不做 / 遗留（偏离决定）

1. **REST 错误体**（sessions.py `{"error": "unknown session"}`、goal REST 错误等）：
   HTTP JSON 协议细节，面板侧自行映射；未纳入本轮 key 契约。
2. **synthesis.event 的 error 文案**（`_synth_error_text`）：渲染在右坞总结面板
   （right.* 域，归并行任务），留英文兜底，key 化待 right 域任务。
3. **历史会话成品字符串回放**：契约兼容路径 `i18n_key ? t(key,params) : text 直显`
   由 W1 渲染端实现；runtime 侧保证 text 字段永不变语义（英文兜底）。
4. OS 通知接线（见 §3）：key 已入库，useChatStreamEngine/store.tsx 的取数换 t() 归 W1。
5. `message.end` 的 `text`（模型正文预览）、`turn.start` 的 `name`（Agent 名兜底）：模型
   生成/专名，不翻译。

## 8. 验证

- 契约断言新增：`packages/runtime/tests/unit/test_event_contract.py`（15 条：key 双向
  parity、payload 三元组、summary 行三元组、runtime catalog 双语、directive 注入、
  attach-only intent 双语）。
- 受影响断言修正：`tests/unit/test_steering.py`（attach-only 意图改用 `_attach_only_text()`）。
- 结果：`pytest -m unit` **1131 passed**（基线 1116 + 新增 15）；`pytest -m api`
  **297 passed**；e2e 285 passed / 3 failed——其中 2 个单跑复测通过（flaky），
  `test_packaged_ui_context_chip_and_usage` 等待的 UI 文案属 W1 并行迁移中的 web
  渲染（本任务运行时路径未触碰该链路），归 W1 验证。
