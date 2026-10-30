# Worker Protocol — 跨 Session 派活与订阅协议

> 状态:**P0+P1+P2(除员工池)已实现**(2026-10-29,`worker_bus/` + 前端卡片,
> 18 项单测 `tests/unit/test_worker_bus.py`)。独立于 AgentTeam(不 import
> `teams/*`、不依赖 TEAM.md/成员实例);只复用**引擎级**原语(steer stash、
> wake turn、session meta、注入信封)。本文档为协议契约 + 实现索引;
> §11 为已落定的决策记录。

## 1. 目标 / 非目标

**目标**
- 任意 session 能以统一方式**派活(assign)**给另一个 worker session
  ——忙则排队、闲则唤醒、终态则拒绝。
- 任意 session 能**订阅(watch)**另一个 session 的终态,settle 时结果
  摘要自动回传订阅者——不轮询。
- 派活方全程可知:回执(投递结果)、目标状态(查询/阻塞等待)、最终输出。

**非目标(本轮明确不做)**
- ~~不做常驻员工池 / 员工复用(方案 B 层面)~~ → 仍推迟:会改 subagent
  生命周期契约(完成门/finalize 语义与常驻 worker 冲突),需独立一轮设计
- ~~不做进度流式订阅 / digest~~ → P2 已实现:`watch(want="digest")`,
  节流 `DIGEST_MIN_INTERVAL_S=300`(watch.py),内容为最后一条 AIMessage
  前 300 字节节选
- ~~不做 UI(前端零改动)~~ → P1 已实现:worker_result / worker_assign
  双卡(blocks/subagentCards.tsx),live 吸收(handle.ts)与历史重放
  (subagentParse.ts fold + TranscriptList)双路径折卡
- 不与团队机制互通(team 有自己的投递通道,互不感知)

## 2. 角色与术语

| 术语 | 定义 |
|---|---|
| **Worker** | 被派活/被订阅的 session。P0 限定 **task-typed**:`subagent` / `delegation` / `scheduled`(有明确终态的会话) |
| **Sender** | 派活方 session(任意类型,含 main) |
| **Subscriber** | 订阅方 session(任意类型,含 main) |
| **Main session** | 用户直接对话的会话。**可被派活**(2026-10-29 放开,实测
  「AMS 迁移」场景:用法就是「会话即员工」,wake/steer 注入 main 的机制
  本就存在);但**无终态**——watch 只收 digest。type 创建即主动标注
  `"main"`(启动 backfill 迁移历史行,空 type 仅作 legacy 兜底);用户可在
  侧栏扇形菜单开关「接活」(meta `worker` 字段,缺省接活,关掉即退出
  花名册/派活白名单)。channel 会话(用户 IM 实时对话面)不可被派活 |
| **Settle** | task-typed session 进入终态:done / failed / stopped / error |

## 3. 寻址与发现(Registry)

- **地址 = session_id**(UUID,稳定,进程重启不变)。人/模型友好寻址靠
  `find` 解析(标题前缀匹配 → 唯一命中才接受),不做别名系统。
- **事实源 = `_index.json`**,不新建存储。meta 已有
  `type/title/subagent.status/updated/parent_session_id` 全部所需字段。
- **发现工具** `workers.find(query)`:`(id, title, type, status, age)` 行,
  按 `updated` 倒序,过滤条件 `type ∈ task-typed`。这是 LLM 侧唯一的
  花名册入口(现状:模型完全看不见兄弟会话)。

## 4. 协议消息(Envelope)

所有跨 session 消息统一信封,落 checkpoint 的 `additional_kwargs`:

```jsonc
// HumanMessage.additional_kwargs
{
  "ginno_worker_msg": {
    "msg_id": "uuid",          // 幂等键(重投/去重,同 steer_id 语义)
    "from": "<sender_id>",     // 溯源(渲染卡片"来自 X")
    "kind": "assign | notice | result",
    "ref":  "<msg_id>",        // result 回指它应答的那条 assign(可空)
    "sent_at": 1730000000
  }
}
```

- `assign`:正文 = 结构化简报(Goal/Constraints/Acceptance,同 subagent
  brief 的自足性要求:接收方按"有能力但零上下文的同事"读)。
- `notice`:自由文本通报(无任务语义)。
- `result`:终态回传正文 = 机器头(状态+溯源)+ result_summary。

**投递语义**:at-least-once + `msg_id` 去重(steer 通道已有同款幂等);
**回执三态**(同步返回给派活工具):
- `absorbed` — 已提交进目标 checkpoint(持久,目标重启也在)
- `queued` — 已入目标 steer 队列但**未持久**(进程崩溃会丢;派活方收到
  queued 回执后若目标长时间无响应,应 `status` 查证后决定重发)
- `refused` — 确定未投(target 非task-typed / 已终态 / 不存在)

## 5. 状态模型(对外统一口径)

由引擎现有信号**投影**计算,不新增状态存储:

| 协议状态 | 来源 |
|---|---|
| `working` | `session_id ∈ _RUNNING_TURNS ∪ _MANAGED_TURNS` |
| `waiting` | `session_id ∈ _PENDING_RESUME`(停在 ask_user/权限卡) |
| `settling` | 终态翻转中(wake/级联停的锁窗口内) |
| `done / failed / stopped / error` | task-typed 的 meta 终态 |
| `unknown` | 磁盘有 meta、进程内无会话且非终态(重启后未唤醒) |

`worker.status(target, wait=true, until=[...], timeout_s)` = 查询 +
阻塞等待(等价语义已存在于 `wait_for_subagents`,泛化到任意 worker)。

## 6. 传输机制(Delivery)

### 6.1 assign —— 按目标状态双路(机制全部现成,只是收口)

```
assign(target, brief):
  ├─ target working/waiting → steer_enqueue(target, entry)
  │     entry = {steer_id: msg_id, text: brief, origin: "worker",
  │              extra_kwargs: {ginno_worker_msg: {...}}}
  │     → 目标下一个 agent superstep 入口排水吸收(同 turn 生效)
  │     → 回执 queued(absorbed 待 checkpoint 提交后成立)
  ├─ target idle → wake turn(锁 + 生存检查 + headless turn,brief 即
  │     turn 的 user_text → 天生持久)→ 回执 absorbed
  └─ target terminal/unknown-id → refused
```

**必须处理的机制细节**(均为踩过的坑,直接继承解法):
- steer 条目生命周期 = 单 turn 段:段末 `steer_keep_channel` 目前只保留
  `origin ∈ {channel, task}` 的未吸收条目(有补投 watcher 捞回重投)——
  **需加 `worker` 同待遇**,否则 turn 收尾时未吸收的 assign 会被静默丢。
- `_STEER_INFLIGHT` 承诺链:drain 时 ack、commit 时清——absorbed 回执的
  真实性锚点,跨 session 投递必须走同一条链,不能绕过。
- wake 的竞态防护(under-lock 重读 meta / 预置 busy 标记 / stop-event
  前置检查)照抄 `_wake_parent_turn` 的三道闸,防幽灵 turn。

### 6.2 watch —— 订阅注册 + settle 钩子

```
watch(target):
  meta(target).subscribers += {subscriber_id, want: "terminal", since}
  (幂等:重复 watch 刷新 since,不重复加)
```

- **注册存哪**:目标 meta 的 `subscribers[]` 字段(`session_meta` 串行锁
  已保证跨 session 写安全;heal 路径天然带上)。
- **触发点**:task-typed 终态翻转处(与今日 `_inject_result` 注入父级
  同一钩子位)。除 parent 链外,遍历 `subscribers[]` 逐一回传:
  订阅者忙 → steer;闲 → wake turn。**一次性**:触发即自动移除该订阅
  (want=terminal 的语义就是" notify once")。
- **防回声**:subscribers 里若含目标自己/其祖先已有的注入对象,去重
  (parent 已有结构性回传,不重复注入)。
- **渠道订阅者**(2026-10-30 keep_edc84fe53d 事故):订阅者是 IM 渠道会话
  (type=channel)时,digest/result 的 wake turn 不经过适配器的
  run_channel_turn——没有 ChannelTap 挂流,回复只落会话,IM 用户永远收
  不到订阅的消息。修法在引擎收口分支:`channels.base.relay_channel_reply`
  ——channel 会话、正文非空、流上无 tap(有 tap = 适配器驱动的正常轮,
  回复已由 tap 翻译)→ 经适配器 `notify_turn_reply` 主动推送(企微/钉钉
  markdown,📬 前缀,4000 字截断)。这同时覆盖子代理回传与任务完成注入
  等一切无 tap 的调度 turn。
- **重启对账**:订阅关系在 meta 里持久;settle 钩子在重启后对账
  (reconcile)路径里同样执行——错过的终态在下次启动时补发。

### 6.3 隔离边界(与 AgentTeam 不参合的落实方式)

- 新模块 `worker_bus/`(协议实现),**不 import `teams/*`**;
- 复用的只有引擎原语:`server_shared.steer_*`、wake-turn 模式
  (从 `subagent_scheduler/runner.py` 提炼通用函数或最小复制,见开放问题 5)、
  `session_meta` 读写、`_push_session_event`。
- subagent/delegation 自己的父子回传**不受影响、不走本协议**(结构性
  血缘回传优先;本协议只服务"树外"的订阅关系)。

## 7. 回传渲染(已实现:P1 双卡)

信封是**双轨**的,职责分离:
- **XML 文本标签**(前端折卡依据):`<ginno_worker_assign from session>
  <goal/><constraints/><acceptance/>` 与 `<ginno_worker_result session from
  status goal kind?>`(kind="digest" 标识进度节选)。前端
  `subagentParse.ts` 用 DOMParser 解析,同 subagent brief/result 的既有
  契约——历史重放(HumanMessage 原文折行)与 live 吸收(handle.ts 折
  system 行)都吃文本,不依赖 kwargs。
- **`ginno_worker_msg` kwargs 键**(机器溯源):随持久化 HumanMessage 落
  checkpoint(msg_id 幂等/ from 溯源),供后续程序化消费;前端不读它。

卡片:`WorkerResultCard`(终态回传/进度 digest,带状态 glyph、来源标题、
「查看员工对话」跳转)与 `WorkerAssignCard`(任务简报,goal/constraints/
acceptance 三段 + 派发方),渲染入口 `SubagentBlocks`(与子代理卡同一
系统行通道,TranscriptList 双分支分派)。

## 8. 顺序、并发与上限

- **每目标 FIFO**:steer 队列天然有序;多条 assign 在同一吸收窗被
  拼接(同今日 steer 批量排水语义)。
- **订阅者侧合并**:多个 worker 同窗 settle → 复用注入合并窗
  (COALESCE + merged 文案)成一回合注入,防订阅者被连环唤醒。
- **订阅上限**:每 worker `subscribers ≤ 8`(超出拒绝,防风暴);
- **队列上限**:每目标未吸收 steer 条目 ≤ 16(超出回执 refused
  `busy-overflow`,防派活方无限堆积)。

## 9. 失败模式矩阵

| 场景 | 行为 |
|---|---|
| 目标在投递中死亡/停止 | steer 路径:条目随段末清场消失 → 派活方靠 status 查证;wake 路径:under-lock 生存检查拒绝 |
| 派活方在回传前死亡 | 回传仍执行(steer/wake 目标是订阅者会话,与派活方存亡无关);丢失的只是回执 |
| 运行时崩溃重启 | absorbed 级消息在 checkpoint 里;queued 级丢失(回执语义已声明);订阅在 meta 里,settle 对账补发 |
| 两个 sender 同时 assign 同一空闲目标 | turn 锁串行化:先醒者开 turn,后者 steer 排队进同 turn |
| 订阅者=目标祖先 | 去重(结构性回传已覆盖) |
| 目标是 main session | **可派活**(2026-10-29 放开);watch want=terminal 被拒并引导 digest(main 无终态) |
| 目标是 channel 会话 | refused(实时对话面,协议层白名单不含) |
| 用户说「在 X session 上做」但模型本地代跑 | 系统提示层路由判据(graph_prompts 跨会话派活规则,2026-10-29 c4d6f83f 实测补) |

## 10. Spike 验证场景(P0 出口判据)

dev 模式(`pnpm dev`,无 PyInstaller)跑通以下 6 幕,机制即算验证:

1. assign 忙目标:正在跑工具调用的 worker 收到 brief,同 turn 吸收
2. assign 闲目标:headless wake turn 启动,brief 成为首条消息
3. assign 终态/非 task-typed:refused 三态回执正确
4. watch → 目标 done:订阅者(忙/闲两版)收到带信封的 result
5. 订阅者收到注入后,卡片正常折叠渲染(复用 ginno_subagent_result 键)
6. 重启 sidecar:订阅关系仍在,已终态未回传的目标补发

## 11. 决策记录(原开放决策点,均已按实现落定)

1. **steer 保留名单** → 已并入保留集:`steer_keep_channel` 保留
   `origin ∈ {channel, task, worker}`(server_shared.py),未吸收 assign
   随段末清场保留,由既有补投 watcher 捞回——不自建 watcher。
2. **absorbed 回执时机** → 一段式(设计稿倾向项):wake 成功即
   `absorbed`(brief 即 turn 首条消息,天生持久);steer 路径回
   `queued`(checkpoint 提交前不升格);锁内竞态降级 steer 也回
   `queued`。不做二段式追踪,派活方拿 queued 后靠 `worker_status`
   查证(deliver.py 出口码 `ran/steered/refused:*` 映射回执)。
3. **跨项目** → **默认全局**(2026-10-29 用户确认方向后翻转):Ginno
   项目概念已弱化(侧栏/会话端点事实上单项目 "default"),寻址
   `resolve_target` 本就跨 slug——花名册与其同口径,默认列全部项目,
   行内 `project` 字段标注归属;`all_projects=false` 收窄回单项目。
4. **scheduled 会话** → 订阅实例(只能订阅"这一次执行");scheduled
   每次执行产生新 session,模板级订阅属员工池议题,随其推迟。
5. **wake 逻辑的取用方式** → 提炼(不复制):`_wake_parent_turn` 扩参
   `extra_kwargs` / `steer_origin`,返回出口码字符串供协议层映射;
   worker_bus 直接 import,零复制。

## 12. 分期与实施状态

- **P0 ✅**:worker_bus(registry / deliver / watch / tools)+
  `list_workers / assign_worker / watch_worker / worker_status` 四工具
  (graph.py 注册),18 项单测;§10 六幕中 1/3/4/6 幕已有单测等价覆盖
  (assign 三态、watch 终态短路/一次性、重启 reconcile 补发),2/5 幕
  待 dev 真机验证。
- **P1 ✅**:前端双卡 + i18n(chat.worker.*,en/zh-CN)+ live/重放双路径。
- **P2 ✅(部分)**:digest 订阅(300s 节流)+ 跨项目花名册/寻址;
  ❌ 常驻员工池推迟(生命周期契约冲突,见 §1)。

**待真机验证**(dev 或 make app 后):§10 第 2 幕(闲目标 wake 成首条
消息)、第 5 幕(订阅者收到注入后卡片折叠);digest 节流的实际观感;
跨 slug assign 的 wake 路径。
