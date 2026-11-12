# 企微指挥热更新：无头 finish 闭环（G1）+ 命令权限闸（G3）技术方案

> 状态：**已实施**（G1 main commit 9923962；G3 main 见 channel_policy
> 相关 PR #59）。关联：`docs/runtime-hot-swap-design.md`、
> `docs/dev-swap-progress-design.md`。
> 实施与设计的差异点（2026-11-12 回填）：
> - L2 直通落点在 `engine/handle.ts` 的 `runtime.dev_swap_pending`
>   case（设计写 bus.ts）——语义一致：`auto===true` 跳过确认框直接
>   invoke `finish_dev_swap`，invoke 失败静默（壳 watcher 兜底）。
> - 确认回执经 `api/devswap._notify_unattended_confirm` →
>   `_wecom_adapter.register_devswap_confirm`（旧进程侧，与设计一致）。
> - 钉钉侧答案路由未做对称适配（设计 §1.4 提及）——企微先行，钉钉
>   复用时补 `register_devswap_confirm` 同构方法即可。
> - 端到端真机演示（企微 y/n 全链）待补；runtime 状态机 7 用例绿
>   （tests/unit/test_devswap_unattended.py）。
> 范围裁定（2026-11-09）：G2（git pull 语义）与 G4（终态推企微）**明确不做**；
> 本文只覆盖 G1 + G3。

## 0. 背景与问题

企微发 `/dev-swap` 今天已可触发（`channels/base.py::run_channel_turn` 的
builtin 命令短路与桌面 WS 同口径）。两个断点：

- **G1**：构建成功后 `runtime.dev_swap_pending` 广播 → 前端
  `lib/devswap/bus.ts` **弹确认框** → 人工点击 → invoke 壳
  `finish_dev_swap`（重启+指纹验证+失败回滚）。企微场景下桌面没人点框 →
  切换停在「待切换」；桌面完全退出 → 广播无人收，新 bundle 挂着不生效。
  finish 逻辑在壳 `apps/desktop/src/dev_swap.rs::run_finish`（pub(crate)），
  但唯一入口是 `#[tauri::command]`（webview invoke）——壳活着也无法脱离
  webview 执行。
- **G3**：channel 侧命令短路直接执行，无身份门槛。`per_conversation` 模式下
  群聊任何人发 `/dev-swap` 都会触发（`_resolve_session` 按 chat_key 绑定，
  userid 不参与命令鉴权）。

## 1. G1：无头 finish 闭环

### 1.1 设计原则

1. **壳是唯一有资格杀/拉 sidecar 的一方**（dev_swap.rs 头注释既有约定），
   本方案不改变这一点；无头路径 = 给壳增加「非 webview 的 finish 入口」。
2. **state.json 是壳 ↔ sidecar 的可靠文件介质**（不依赖进程存活）：
   `dev-override/state.json` 已有 `state: pending → verified/rolled_back`
   状态机，扩展一个字段即可承载无头授权。
3. **企微确认 = finish 授权**：把「桌面点确认框」翻译成「企微回复 y」，
   安全语义等价（都是发起侧的人工确认），桌面弹窗保留给桌面发起的场景。

### 1.2 状态流（目标）

```
企微: /dev-swap
  └─ registry._dev_swap_async_handler 识别 channel 发起
       env + DEV_SWAP_UNATTENDED=confirm        （确认模式，默认）
  └─ dev_swap.sh: build 段全过 → symlink 已切
       state.json: {state:"pending", unattended:"confirm",
                    requested_by:"agent", session_id, chat_key, ts}
  └─ 旧 sidecar 仍活着：向企微回执「切换就绪，回复 y 生效 / n 丢弃」
       （复用 channel pending_question 挂起，TTL 10 分钟）
用户: y
  └─ pending_question 答案路由 → api/devswap.mark_unattended_ok(chat_key)
       state.json: unattended → "go"      （原子改写，文件介质）
       同时广播 runtime.dev_swap_pending {auto:true}（前端活则直通）
  └─ 壳 watcher 轮询 state.json（5s）：unattended=="go" && state=="pending"
       && age<10min → try_lock(RestartLock) → run_finish()
  └─ run_finish: 杀旧 sidecar → 拉新 bundle → /api/health 指纹验证
       → verified / rolled_back*；emit dev-swap:result
  └─ 新进程 watch_swap_finish 补报 finish 终态（既有机制，不动）
```

用户回复 `n`（或 TTL 到期）：`unattended → "abort"`，state 保持 pending，
回执「已搁置：桌面端进度徽标仍可点按补执行（现状 postponed 语义）」。
**n 不触发回滚**——rollback 只属于「验证失败」，人工丢弃=保留现场可补。

### 1.3 分层兜底（三层，层层递进）

| 层 | 执行者 | 存活前提 | 机制 |
|---|---|---|---|
| L2 | 前端 bus.ts | webview 活（App 开着即可，含后台） | 收到 `{auto:true}` 不弹框直接 invoke `finish_dev_swap` |
| L3 | 壳 Rust watcher | 壳进程活（含托盘/窗口全关） | dev_swap.rs 内 spawn 轮询 state.json，条件满足即调 `run_finish` |
| L4 | 下次壳启动 | 壳完全退出后的冷启动 | 启动序列里同一判定跑一次（`age<10min` 防误吞陈旧 pending） |

L2 是低延迟路径（<1s），L3 兜 webview 崩溃/挂起（≤5s），L4 兜壳重启窗口。
`RestartLock.try_lock` 既有纪律保证三路竞争安全（后到者拿到 "another
restart/swap is already in flight" 直接退出）。

### 1.4 改动点（文件级）

**runtime（Python）**

- `commands/registry.py`
  - `_dev_swap_async_handler`：`session["type"].startswith("channel")` 时
    env 增加 `DEV_SWAP_UNATTENDED=confirm`，并把 `chat_key` 写进
    `DEV_SWAP_CHAT_KEY`（回执路由用；chat_key 从 session 的 channel
    元数据取，adapter 绑定时已写入）。
- `scripts/dev_swap.sh`
  - switch 段写 state.json 时透传 `unattended`/`chat_key` 字段
    （env 缺省不写——手动 `make dev-swap` 的交互式语义完全不变）。
- `api/devswap.py`
  - 新增 `mark_unattended(chat_key, verdict)`（"go"|"abort"）：校验
    state==pending && unattended=="confirm" && chat_key 匹配后原子改写
    state.json；"go" 时同时 `_push_global_event("runtime.dev_swap_pending",
    {auto:true, run_id})`。
  - 供 channel 答案路由调用（挂 internal 函数即可，不新增 HTTP 面）。
- `channels/wecom/adapter.py`（+ 钉钉同构适配器）
  - `_dev_swap_async_handler` 构建启动回执改为「构建中，完成后等你确认」；
  - 构建成功通知：`api/devswap` 在 shell 报 switch ok 时回调 channel——
    具体落点：`watch_swap_finish` 之外新增「旧进程侧 switch-ok 钩子」
    （`update_progress` 收到 stage=switch state=ok 时，若
    unattended=="confirm" 且有 chat_key → 向该 chat 发确认回执 +
    `register_pending_question`（options: ["y","n"]，TTL 600s））。
  - `pending_question` 既有答案路由处加一个 devswap 分支：答案 y/n →
    `api/devswap.mark_unattended`。
- `apps/web/src/lib/devswap/bus.ts`
  - `dev_swap_pending` 载荷含 `auto:true` → 不入 pendingConfirm，直接
    invoke `finish_dev_swap`（结果仍走既有 `dev-swap:result` 渲染）。

**壳（Rust）**

- `apps/desktop/src/dev_swap.rs`
  - `run_finish` 保持不动；新增 `spawn_unattended_watcher(app)`：
    每 5s `read_state()`，判定
    `state=="pending" && unattended=="go" && ts 距今<600s` →
    `try_lock(RestartLock)` → `run_finish`。进程内全局只 spawn 一次。
  - 壳启动序列（`lib.rs` setup）调用一次冷启动判定（L4）。
  - `unattended` 字段缺省/非 "go" 一律不动——**手动 dev-swap 的桌面确认
    弹窗路径零变化**（回归红线）。

### 1.5 竞态与边界

| 场景 | 行为 |
|---|---|
| 前端 invoke 与壳 watcher 同时触发 | RestartLock try_lock，后到者退出 |
| 用户桌面同时在跑手动 make dev-swap | watcher 只认 `unattended=="go"`；手动路径无此标记，互不干扰 |
| y 之后构建产物本身坏了 | run_finish 健康检查失败 → 既有回滚（rolled_back），不变 |
| TTL 到期未回复 | state 保持 pending + unattended=="confirm"（不再有效），watcher 不认；桌面 postponed 补执行路径保留 |
| 壳完全退出期间用户回了 y | sidecar 写 state.json 成功（文件介质）；壳下次启动 L4 判定 age<600s 则补执行，超龄回执「已搁置」 |
| 陈旧 pending（几天前） | age 守卫直接跳过 |

### 1.6 结果可见性

按裁定 G4 不做：finish 终态不推企微。用户回 y 后的结局可见性 =
桌面 `dev-swap:result` / TopBar 指纹黄条（既有）；企微侧最多在
pending_question 被答复时回执一句「已授权切换，结果见桌面」。此边界
写死在本文档，防后续误判为缺陷。

## 2. G3：命令权限闸

### 2.1 模型

- `BuiltinCommand` 新增字段 `channel_policy: "all" | "admin" | "none"`
  （缺省 `"all"`，兼容现状）：
  - `all`：channel 可触发（/clear /fork /help /todos 等既有可用集）；
  - `admin`：仅 `admin_users` 内的 userid 可经 channel 触发（/dev-swap）；
  - `none`：channel 一律拒绝（桌面专属，预留给未来高危命令）。
- 配置：`channels.wecom.admin_users: ["userid", ...]`（settings.json，
  adapter `start(config)` 已接收该 dict；钉钉同名字段）。**fail-closed**：
  `admin_users` 为空/缺省 → admin 级命令对 channel 全拒，回执附配置路径。

### 2.2 判定点

`channels/base.py::run_channel_turn` 命令短路处（resolve_turn 之后、
async_handler 分发之前）：

```python
if plan.builtin or plan.builtin_async:
    cmd = BUILTINS.get(plan.builtin or plan.builtin_async)
    if channel_ctx is not None:            # channel 上下文才检
        policy = cmd.channel_policy if cmd else "all"
        if policy == "none" or (policy == "admin" and userid not in admin_users):
            → tap 回执双语拒绝文案（含命令名/是否可配/配置位置）
            return
```

- `run_channel_turn` 签名增加 `channel_ctx: ChannelCtx | None`
  （`ChannelCtx = {adapter_kind, chat_key, userid, admin_users}`），
  wecom/钉钉两个 `_run_text_turn` 调用处透传（userid 两处都已持有）。
- 桌面 WS 路径**不传 channel_ctx，判定自动跳过**——桌面行为零变化。

### 2.3 命令标注（初版）

| 命令 | channel_policy | 备注 |
|---|---|---|
| dev-swap | **admin** | 本方案主对象 |
| clear / fork / help / goal / todos / compact … | all | 维持现状 |
| （无新增 none 项） | — | 字段先落，语义预留 |

### 2.4 判定粒度说明

- 按**发消息的 userid** 判定，不按会话 owner（per_conversation 群聊里
  会话共享、人不同）；
- 群聊 vs 单聊不区分（admin 名单全局于该 channel）；
- 不引入「命令黑名单转义」：LLM 生成文本恰好以 / 开头会被同一闸门
  拦截（admin 命令非 admin 用户连解释性回执都拿不到，只有拒绝文案）。

## 3. 测试清单

**runtime（pytest）**
1. `test_channel_commands.py`：policy 矩阵——all 命令任意 userid 可用；
   admin 命令 admin 放行 / 非 admin 拒绝 / admin_users 空 fail-closed；
   拒绝回执双语。
2. `test_devswap_unattended.py`：state.json unattended 状态机
   （confirm→go / confirm→abort / 非法 chat_key 拒改 / 陈旧 ts 拒改）；
   pending_question 答案路由（y/n）到 mark_unattended 的接线。
3. 既有 `test_channel_turn_*` 回归：命令短路顺序（arm 之前）不变、
   桌面路径不传 channel_ctx 零影响。

**壳（cargo test）**
4. `dev_swap.rs` 单测：watcher 判定矩阵（pending+go+新鲜=执行；
   confirm/abort/无字段/超龄=跳过）；try_lock 冲突让位。

**前端（vitest，bus 纯逻辑）**
5. `auto:true` 直通 invoke；无 auto 走弹窗（回归）。

**端到端（手动，make app 后）**
6. 六场景验收：桌面弹窗路径回归 / 企微非 admin 拒 / admin y 闭环
   （App 前台）/ admin y 闭环（App 后台仅壳）/ y 后壳退出再启动补执行 /
   TTL 过期搁置。

## 4. 实施顺序建议

1. G3 先行（独立、小、纯 runtime）：registry 字段 + base.py 闸门 + 测试；
2. G1-L3/L4（壳 watcher）与 G1 确认协议（runtime 侧）可并行；
3. G1-L2 前端直通最后合（依赖 auto 载荷字段定型）；
4. 全链路手动验收（§3.6）后，在 CLAUDE.md「构建」节补一行
   「企微 /dev-swap 无人值守语义」说明。
