# 钉钉机器人连接器设计（产品设计 + Adapter 技术方案 + 特性流程）

> 状态：设计稿 v2（未实现） · 日期：2026-10-21
> 依据：钉钉开放平台调研（Obsidian Raw《2026-10-20-钉钉机器人对接调研.md》）+ Ginno 代码核验（文中所有 `文件:行号` 均已逐条验证）
> 关联文档：[connector-module-design.md](./connector-module-design.md)
> v2 变更：§4 新增——**每个特性的端到端流程定义**（触发→步骤→数据→异常），§5 附 WS 帧×钉钉动作总映射表

---

## 0. 结论速览

| 决策点 | 结论 |
|---|---|
| 机器人形态 | 钉钉**企业内部应用机器人**（非群 Webhook，后者不可双向） |
| 接入模式 | **Stream 模式**（WebSocket 出站长连接，免公网/免加解密），官方 `dingtalk-stream` Python SDK |
| 架构落点 | 新增 `channels/` 模块承载 **ChannelAdapter 抽象层**；`DingTalkConnector`（Connector 子类）负责配置/状态/安装，`DingTalkChannelAdapter` 负责协议桥接 |
| 会话驱动 | 复用 headless turn 协议：`_run_stream(None, …)`（`api/sessions.py:314` / `cli.py:538` 两个先例），但**保持 interactive**（钉钉有人应答 ask_user） |
| 输出订阅 | 假 WS tap 挂 `server_shared._SESSION_WS[sid]`（`cli.py:510` 先例），按帧事件驱动钉钉侧渲染 |
| ask_user | `user.question` 帧 → 互动卡片 → STREAM 回调 → 提炼公共 `resume_pending_question()` 恢复（复刻 `ws.py:908-925` 协议） |
| 文件双向 | 收：downloadCode→临时URL→会话文件目录→`attached_files`；发：媒体上传→mediaId→富媒体消息 |
| 分期 | P0 单聊 MVP → P1 菜单+群聊+权限确认 → P2 多人协作增强 |

---

## 1. 背景与目标

让用户在钉钉客户端内与 Ginno Agent 双向对话：Ginno 桌面端保持运行，钉钉作为远程触达渠道。产品范围：

1. 图片/文件上传（用户→Agent）与发送（Agent→用户），钉钉客户端内可直接预览；
2. 先单人对话（单聊），架构预留多人对话（群聊）；
3. ask_user 在钉钉侧可用；
4. 菜单/命令配置放在连接器配置里，连接器需展示连接状态；
5. 现有特性无法在钉钉侧支持的，输出兼容性报告（§6）。

本期**只做设计与方案，不写实现**。

---

## 2. 产品设计

### 2.1 用户旅程

**配置（桌面端）**：设置 → 连接器 → 「钉钉机器人」卡片 → 填 appKey/appSecret → 按安装引导在钉钉开放平台创建企业内部应用、开启机器人、事件订阅选 Stream 模式 → 回到 Ginno 状态变「已连接」。

**对话（钉钉端）**：用户在钉钉找到机器人发起单聊 → 发文本/图片/文件 → Agent 以**流式 AI 卡片**（打字机）回复 → 需要决策时卡片出现**按钮选项 +「其他」输入** → 点选后 Agent 继续 → Agent 产出的图片/文件以富媒体消息发出，点开即预览。

**菜单**：发 `/菜单名` 触发预配置动作（如「今日 TODO」「跑工作流」）。菜单项在桌面端连接器配置维护。

### 2.2 连接器配置页（含菜单与状态）

沿用 Connector 体系，前端**零新增代码**（`ConfigFold.tsx` 按 `config_schema` 动态渲染）。完整 config_schema：

```jsonc
{
  "app_key":        {"type": "string",  "title": "AppKey",   "secret": false},
  "app_secret":     {"type": "string",  "title": "AppSecret","secret": true},
  "robot_code":     {"type": "string",  "title": "robotCode（默认=AppKey）", "secret": false},
  "allowed_staff_ids": {"type": "list", "title": "允许使用的钉钉用户 staffId 白名单（空=应用可见范围全部）"},
  "default_agent_id":  {"type": "string", "title": "渠道默认 Agent（空=全局默认）"},
  "session_policy":    {"type": "enum", "options": ["per_user", "per_conversation"],
                        "default": "per_user", "title": "会话策略（P0 固定 per_user）"},
  "menus": {"type": "list", "title": "菜单/命令", "item_schema": {
      "trigger": {"type": "string", "title": "触发词（/开头或全等）"},
      "title":   {"type": "string", "title": "展示名"},
      "prompt":  {"type": "string", "title": "注入的指令模板（支持 {date} 等变量）"},
      "new_session": {"type": "bool", "title": "每次新开会话", "default": false}
  }},
  "ask_timeout_s":  {"type": "int", "default": 600, "title": "ask_user 超时秒数"},
  "tool_policy":    {"type": "enum", "options": ["readonly", "confirm_highrisk", "all"],
                     "default": "confirm_highrisk", "title": "工具策略（见 F11）"}
}
```

**连接状态**（连接器卡片实时展示）：`connected / connecting / disconnected(重连中) / error / disabled` + 最近错误 + 最近事件时间。来源 = Stream WS 健康度，经 `ConnectorRegistry.report()` 上报（§4 F1）。

### 2.3 消息体验

- **流式回复**：AI 卡片占位「思考中…」→ 打字机增量 → 终态定格；失败标错误态。
- **ask_user 卡片**：问题 + 2~5 按钮 +「其他」输入；答题后按钮置灰防重；超时自动 skip。
- **文件**：图片内嵌消息（点开大图）；pdf/office 文件消息（客户端内建在线预览）；HTML 等不可预览类型发文件并附说明。
- **长任务**：回复不依赖 `sessionWebhook`（>30s 不可靠），统一服务端 API 定向发送。

### 2.4 单人 → 多人路线

| 阶段 | 形态 | 会话策略 | 多人处理 |
|---|---|---|---|
| P0 | 单聊 | `staffId` → 独立 Ginno session | 无 |
| P1 | 群聊 @机器人 | 群 `conversationId` → 共享 session | 消息注入带说话人前缀；白名单控制谁能驱动 |
| P2 | 群协作 | 同上 | 抢答消解、@ 回复定向、群内权限分级 |

---

## 3. 技术架构

### 3.1 模块布局

```
packages/runtime/src/ginno_runtime/
├── connectors/                      # 已有体系（不动核心）
│   └── builtin.py                   # + DingTalkConnector 注册进 ensure_builtin_connectors()
└── channels/                        # ★ 新增模块
    ├── __init__.py
    ├── base.py                      # ChannelAdapter 抽象 + StreamTap 基类 + 会话路由器
    └── dingtalk/
        ├── adapter.py               # DingTalkChannelAdapter（Stream 客户端/路由/生命周期）
        ├── tap.py                   # DingTalkTap：WS 帧翻译 → 卡片/消息动作
        ├── cards.py                 # AI 卡片创建/流式更新/终态；ask_user 互动卡片
        ├── media.py                 # downloadCode 下载 / 媒体上传 / mediaId 缓存
        ├── menus.py                 # 菜单匹配与模板渲染
        └── sessions.py              # conversationId/staffId ↔ ginno session_id 映射表
```

### 3.2 分层与依赖

```
钉钉开放平台 ──WebSocket(Stream SDK)──┐
                                      ▼
DingTalkConnector(connectors/)  ──apply_config──▶ DingTalkChannelAdapter(channels/)
  · config_schema / install_steps                · 收消息 → 路由 → headless turn
  · 状态上报 registry.report()                   · StreamTap 挂 _SESSION_WS 收帧
  · 启停生命周期                                  · 帧 → 卡片/消息/文件 动作
                                      │
                                      ▼
                    Ginno 核心（sessions / stream / graph）——零改动*
                    * 唯一例外：resume_pending_question() 提炼（F4）
```

**职责边界**：Connector = 配置+状态+安装（连接器体系契约）；Adapter = 协议桥接+消息翻译。未来飞书/企微/Slack 只新增 `channels/<name>/`，`base.py` 抽象层与 Ginno 核心挂点全部复用。

### 3.3 ChannelAdapter 抽象（base.py）

```python
class ChannelAdapter(abc.ABC):
    async def start(self, config: dict) -> None: ...   # 建连、注册回调
    async def stop(self) -> None: ...                  # 断连、清理 tap
    def on_channel_message(self, raw: dict) -> None: ...   # 渠道消息 → 路由
    def report_status(self, status: str, detail: str = "") -> None: ...  # → registry.report

class StreamTap:                    # duck-typed 假 WS（cli.py:69 _StreamTap 同款协议）
    session_id: str
    async def send_text(self, frame_json: str) -> None: ...  # 引擎把 turn 帧推给它
    # 子类按 frame["type"] 分发到卡片/消息动作（§5 映射表）
```

---

## 4. 特性流程定义（每个特性端到端）

> 统一格式：**触发 → 步骤 → 数据契约 → 异常/边界**。所有 Ginno 侧符号均已核验。

### F1 连接器生命周期与状态上报

**触发**：用户保存配置 / Ginno 启动 / 断网恢复 / 用户停用连接器。

**步骤**：
1. `DingTalkConnector.apply_config(cfg)`（`connectors/registry.py:76`）校验必填（app_key/app_secret），落盘加密 secret，状态 `installing`；
2. `server.py` lifespan 启动序列（`ensure_builtin_connectors()` 已在 `server.py:135` 调用）后追加 `adapter.start(cfg)`——进 asyncio task，不阻塞 lifespan；
3. `adapter.start`：dingtalk-stream 客户端注册两个 topic——机器人消息 `/v1.0/im/bot/messages/get` + 卡片回调（callbackType=STREAM）；WS 建立后 `report_status("connected", "Stream 已连接")`；
4. 首次启动写映射表（F2）、预取 accessToken（7200s 缓存，过期懒刷新）；
5. 停用：`adapter.stop()` → SDK 关闭 WS → 摘除所有活动 tap → `report_status("disabled")`。

**状态机**（直接复用 `ConnectorRegistry` 状态机，`registry.py:22`）：
`not_installed → installing → connected ⇄ disconnected → error / disabled`
- WS 断开 → `report("disconnected", 原因)`；registry 自带防抖窗口（`registry.py:123-130`，connected→disconnected 有 debounce，闪断不掉状态点）；
- 鉴权失败（secret 错）→ `report("error", "appKey/appSecret 校验失败")`，不重试，等用户改配置。

**异常**：lifespan 重启（dev reload）→ stop 旧 task 再启新 task；flock 租约（`scheduler.py:115` 同款模式）防 dev + 打包双进程重复建连。

---

### F2 消息接收 → 路由 → turn 驱动

**触发**：钉钉 STREAM 推送机器人消息（单聊直发或群聊 @机器人）。

**步骤**：
1. SDK 回调 `on_channel_message(raw)`：提取 `conversationId / senderStaffId / senderNick / msgtype / content / sessionWebhook / downloadCode(附件类)`；
2. **安全闸**（F11）：`senderStaffId` 不在白名单 → 忽略（不回复，防探测）；
3. **菜单匹配**（F8）：命中菜单则替换 user_text；
4. **会话映射**：`sessions.py` 查映射表 `staffId → ginno session_id`；无映射或菜单 `new_session=true` → 调用会话创建（`POST /api/sessions` 的内部等价路径 `api/sessions.py:552`），写入映射，记录 `staffId/conversationId`；
5. **附件落地**（若 msgtype ∈ picture/file/richText）：走 F6 下行流程，得到 `attached_files`；
6. **排队闸**（F9）：该 session 有在途 turn → 入队（或回「正在处理上一条…」）；
7. **驱动 turn**——headless 协议（复刻 `api/sessions.py:297-317`，**差异点：不调用 `_set_interactive_turn(False)`**，钉钉有人应答）：

```python
turn_id = uuid4().hex
config = {"configurable": {
    "thread_id": session_id, "project_slug": slug, "agent_id": agent_id,
    "turn_id": turn_id, "user_text": text, "usage_source": "dingtalk",
}}
server_shared._RUNNING_TURNS[session_id] = turn_id          # 早置（stop 可见）
tap = DingTalkTap(...); server_shared._SESSION_WS.setdefault(session_id, []).append(tap)
try:
    await stream_api._run_stream(None, session["graph"], config, text, session, agent_id)
finally:
    if _RUNNING_TURNS.get(session_id) == turn_id and session_id not in _PENDING_RESUME:
        _RUNNING_TURNS.pop(session_id, None)                 # interrupt 停车时保留
    _SESSION_WS[session_id] = [s for s in ... if s is not tap]   # 摘 tap
```

**数据契约（入）**：钉钉消息体（关键字段如上）。**路由表**（`channels/dingtalk/sessions.py`，JSON 落盘于连接器数据目录）：

```jsonc
{"mappings": [{
  "staff_id": "01234", "conversation_id": "cid0...",
  "ginno_session": "a1b2c3...", "bound_at": "...", "last_active": "..."
}]}
```

**异常**：session 图不存在（被删）→ 重建 session 并映射；`_run_stream` 抛错 → tap 收 error 帧 → 卡片错误态（F3 步骤 8）；同一用户消息风暴 → F9。

---

### F3 流式输出（AI 卡片打字机）

**触发**：F2 的 turn 启动后，引擎向 tap 推帧（帧类型清单见 §5）。

**步骤**：
1. tap 收 `turn.start` → `cards.create_placeholder(session_id, turn_id)`：创建 AI 卡片（模板：状态行 + markdown 正文区），初始内容「⏳ 思考中…」；记录 `open_conversation_id + card_instance_id`（卡片后续更新的锚点）。钉钉侧**不使用 sessionWebhook**（长任务不可靠）；
2. tap 收 `token.delta`（正文增量）→ 写入缓冲；**节流批量**：每 500ms 或累计 ≥ 400 字符调一次 `cards.stream_update(card_id, merged_markdown)`（AI 卡片流式更新接口，客户端呈现打字机）；节流参数可配（接口频率上限待验证，见 §7）；
3. `thinking.delta` → 默认丢弃（可选配置映射为卡片折叠区，P2）；
4. `tool.start / tool.end` → 维护卡片状态行（如「🔧 正在执行 bash…」，同一时间只显示最新一条，避免正文区污染）；
5. `widget.emit` → **降级渲染**：`stat_list` 转 markdown 列表、`chart` 转 markdown 表格 + 一行结论（数据取自 data 字段，不发明数字）；P2 可服务端渲染 PNG 后走 F7 发图；
6. `message.end` → flush 缓冲，卡片定格正文；
7. `ref.emit / artifacts.changed / image.emit` → 触发 F7 产物上行；
8. `turn.stopped`（含 stop/error）→ 卡片终态：正常=正文定格；被打断=追加「⏹ 已停止」；错误=追加「⚠️ 出错：<msg>」；同时走 F4 步骤 8 清理停车状态。

**数据契约**：卡片正文 = 累积 markdown（Ginno 正文本来就是 markdown，钉钉 AI 卡片原生渲染 markdown；表格/代码块可用，复杂 HTML 不支持→兼容性 §6）。

**异常**：流式更新接口失败 → 退避重试 3 次 → 仍失败则静默丢弃该批增量，turn 结束时一次性全量更新（终态保证可见）；卡片创建失败 → 降级为普通 markdown 消息（无打字机），终态再全量发一条。

---

### F4 ask_user 全链路（核心流程）

**触发**：turn 中 Agent 调用 ask_user 工具（`tools/ask_tools.py:113`）→ LangGraph `interrupt({"kind":"user_question", …})`（`ask_tools.py:190`）。

**步骤**：
1. 引擎捕获 interrupt，停车并广播：`_PENDING_RESUME.add(sid)`、`_PENDING_KIND[sid]="user_question"`（`api/stream/engine.py:1535-1537`），推帧给 tap：

```jsonc
{"type": "user.question",
 "id": "<tool_call_id>", "question": "...", "header": "...",
 "options": ["选项A", "选项B"], "allow_free_text": true}
```

2. tap 拦截该帧 → `cards.send_question_card()`：创建互动卡片 = header + question 正文 + 每个选项一枚按钮（回调值 `{"q":"<id>","act":"opt","i":<index>}`）+ `allow_free_text=true` 时一枚「✏️ 其他」按钮（触发钉钉输入框回调）；旧问题卡片（若有）置灰；
3. 卡片按钮点击 → 钉钉 STREAM 卡片回调（同一条 WS）→ adapter 校验 `q` 对应当前停车问题 → 组装 resume payload 并恢复：

```python
# 与 ws.py:919-925 桌面端协议逐字段一致
resume = {"kind": "user_answer", "answer": "选项A", "option_index": 0, "skip": False}
channels.base.resume_pending_question(session_id, resume)   # ★ 新提炼公共入口
# free_text 分支: {"answer": "<用户输入>", "option_index": None, "skip": False}
# 超时分支:      {"answer": "", "option_index": None, "skip": True}   → Agent 走自主默认
```

4. **`resume_pending_question(session_id, resume_value)`**（唯一动核心的改造，从 `api/stream/ws.py:238 _spawn_resume` + `ws.py:908-925 user_answer` 分支提炼为模块级）：
   a. 校验 `session_id in _PENDING_RESUME` 且 `_PENDING_KIND == "user_question"`（peek 不 pop，不匹配则拒绝，保持停车可答）；
   b. `_PENDING_RESUME.discard(sid)`、`_PENDING_KIND.pop(sid)`；
   c. 执行 `_spawn_resume` 原逻辑：resume_config（沿用 interrupt 时 agent）、`_TURN_STOP` 事件捕获、`_turn_lock(sid)` 串行（暂停 turn 的 drain 未完成前不得 resume，否则重跑中断工具——`ws.py:271-279` 注释原话）、`_run_resume(None, graph, cfg, val)`、成功后 `_start_goal_driver`、finally 清理 `_TURN_TASKS/_TURN_STOP` + `on_turn_settled`；
   d. WS handler 的 user_answer 分支改为调用同一函数（桌面端回归面 = 该分支等价重构）。
5. resume 后 turn 继续，输出仍走 F3 流式进**同一张卡片**（tap 不变）；
6. **防重**：卡片回调到达即调卡片更新把全部按钮 disabled；`resume_pending_question` 的 peek 校验天然防双击/双端（桌面+钉钉同时答：先到者生效，后者被 peek 拒绝）；
7. **超时**：adapter 为每个停车问题启动 `ask_timeout_s`（默认 600s）定时器：到点仍 parked → 发 `skip=True` resume + 卡片标注「已超时跳过」；
8. turn 终止路径统一清理：`turn.stopped`/断连时取消未决定时器。

**边界**：断网期间用户已点按钮但回调丢失 → 重连后钉钉会重推未 ack 的 STREAM 回调（SDK 语义，待验证 §7）；Ginno 重启 → checkpoint 持久化，`_PENDING_RESUME` 状态丢失但 interrupt 仍在文件里（`ws.py:209-221` 已有重连重播机制）→ adapter 启动时对映射会话做一次停车检测并重发问题卡片。

---

### F5 权限确认渠道化（P1，协议同 F4）

**触发**：引擎广播 `permission.request` 帧（`engine.py` emit 清单已有）。

**步骤**：tap 收帧 → 互动卡片「⚠️ Agent 请求权限：<工具+参数摘要>」+ [允许][拒绝] → 回调组装 `{"decision": "allow"|"deny"}` → 复用提炼后的公共 resume 入口（对齐 `ws.py:884-905 permission_response` 分支：peek 校验 `_PENDING_KIND == "permission_request"`）。`version.propose`（工作流 DSL 确认）钉钉侧**不承载**：tap 只发文本通知「有工作流变更待确认，请到桌面端处理」（兼容性 §6）。

### F6 文件下行（用户 → Agent）

**触发**：F2 收到 msgtype ∈ {picture, file, richText} 的消息。

**步骤**：
1. 从消息体取 `downloadCode`（各附件类型字段名按官方协议）；
2. `media.download(downloadCode)`：带 accessToken 调「下载机器人接收消息的文件内容」→ 302 临时 URL（短有效期，立即使用）；
3. 流式下载到 `paths.session_files_dir(slug, session_id)/<uuid>_<filename>`（与桌面端上传同目录同构，`api/sessions.py:579-582`）；
4. 构造 `attached_files`（结构对齐 `graph.py:423` 消费的字段）：

```jsonc
[{"name": "截图.png", "kind": "image", "path": "<会话文件目录绝对路径>"}]
// kind 推断: image/video/audio/pdf/office/…（决定 graph 注入的读取指引）
```

5. F2 步骤 7 驱动 turn 时把 `attached_files` 传入（对齐 WS 消息 `files` 字段 → `_resolve_attached_files` 路径，`server.py:506`）→ graph 注入「用户本轮附加了以下文件…用 parse_document/bash 读取」上下文（`graph.py:415-448`）→ Agent 正常用工具消费；
6. 回执：turn 正文外，卡片附注「📎 已接收 2 个文件」。

**异常**：downloadCode 过期/下载失败 → turn 照跑但正文提示「文件 X 下载失败，请重发」；>20MB → 直接提示超限并建议桌面端上传（媒体上限待验证 §7）。

### F7 产物上行（Agent → 用户）

**触发**：tap 观测到产物帧——`image.emit`（bash 生成的图片，引擎自动侦测注册）或 `ref.emit`（write_file/attach_ref 产物）。

**步骤**：
1. `image.emit`（路径为本地图片）→ `media.upload(path, robotCode)`：multipart 上传 → `mediaId`（LRU 缓存 path→mediaId，同文件不重传）→ 发送 picture 富媒体消息（用户点开即大图预览）；
2. `ref.emit`（kind=file/doc，本地文件路径）→ 扩展名白名单 `{png,jpg,jpeg,gif,webp,pdf,docx,xlsx,pptx,txt,csv,md,html,zip}`：
   - 可预览类（图片/pdf/office）→ upload → file/picture 消息（钉钉客户端内建在线预览）；
   - 不可预览类（html/zip…）→ 照发文件消息 + 正文附一句「该类型钉钉内无法直接预览，请下载后查看」；
   - 超过媒体大小上限 → 不上传，正文给出文件在桌面端的路径（`write_file` 结果已含绝对路径）；
3. `artifacts.changed` → 仅用于触发一次目录 diff 兜底（防止帧遗漏，扫描会话文件目录 mtime > turn 开始时间的文件），去重后同步骤 2。

**异常**：上传失败重试 2 次后降级为「路径回执」；secret/robotCode 无效 → `report_status("error")` 提示富媒体不可用，文本通道不受影响。

### F8 菜单/命令

**触发**：F2 步骤 3；或用户发 `/help`、`菜单`。

**步骤**：
1. `menus.match(text)`：遍历配置项，`trigger` 精确匹配或 `/trigger` 前缀；未命中 → 普通对话；命中 → 继续；
2. 模板渲染：`prompt` 中 `{date}/{time}/{staff_name}` 等变量替换；
3. `new_session=true` → F2 步骤 4 强制新开会话（并更新映射）；否则进当前会话；
4. 渲染结果作为 user_text 进入 F2 步骤 7（usage_source 标 `dingtalk_menu`）；
5. `/help`/`菜单`（内置）→ 回复 markdown 菜单列表（从配置动态生成，含每个菜单触发词与说明）；机器人首次会话欢迎语同源生成。

**数据**：菜单配置即 §2.2 `menus` 列表，仅存于连接器配置（单一权威），钉钉侧无硬编码命令。

### F9 并发与排队

**触发**：F2 步骤 6 检查到 `server_shared._RUNNING_TURNS.get(sid)` 非空 或 `sid in _PENDING_RESUME`。

**步骤**：
1. **停车中（ask_user 等答案）**：新文本视为「追加补充」→ 追加到问题上下文？**否**——安全起见：回卡片提示「⏳ 有问题等你作答」（附跳转提示），新消息丢弃并计数；连续 3 条丢弃则再提示一次「可发送『跳过』跳过当前提问」（文本 `跳过/skip` → F4 步骤 3 的 skip 分支，等价于卡片按钮）；
2. **turn 运行中**：新消息入 per-session 队列（上限 5），turn 终态后按序取出一条作为下一 turn 输入（其余丢弃并提示合并）；也可配置为「排队仅一条+拒绝」模式；
3. **多用户并发**：不同 staffId → 不同 session → 各自独立 turn 并行（Ginno 天然支持多 session 并发）；共享全局资源（钉钉 API QPS ~20）由 adapter 内统一令牌桶限流（上传/发卡/流式更新共用）。

### F10 断线重连

**步骤**：
1. WS 断开 → `report("disconnected", 原因)`（registry 防抖兜住闪断）；
2. 重连采用指数退避（复刻 `mods/channel.py:264-287`：start→max 翻倍、成功即复位）；
3. 重连成功 → `report("connected")` + 补偿动作：重发未决问题卡片（F4 步骤 8）、对在途 turn 的卡片做一次全量刷新（若 turn 还活着，流式更新继续）；
4. 连续失败超过阈值（如 30 分钟）→ `report("error", "长时间无法连接")`，退避改为小时级，等用户介入。

### F11 白名单与工具策略（安全）

**步骤**：
1. **用户白名单**：F2 步骤 2；空白名单=应用可见范围（开放）但**强烈建议配置**；未授权用户消息静默忽略（可配置为回复一句「未授权」）；
2. **工具策略**（config `tool_policy`，通过 turn config 注入渠道标记，graph 侧按渠道过滤工具集——实现细节随 P0 落地核）：
   - `readonly`：仅只读工具（read/grep/glob/parse_document/web_search…），bash/write/edit 全禁；
   - `confirm_highrisk`（默认）：全工具可用，但 bash 写操作/删除类/浏览器控制触发 `permission.request` → F5 卡片确认；
   - `all`：不设限（不推荐，仅信任环境）；
3. app_secret 加密落盘；accessToken 仅内存缓存；日志脱敏（消息正文不落 sidecar.log，只落 staff_id/turn_id 级元数据）。

### F12 群聊多人对话（P1 预案）

**步骤**：
1. 群消息要求 @机器人（`atUsers` 含本机器人）才触发；@文本从 content 中剥离；
2. `session_policy=per_conversation`：群 `conversationId` → 一个共享 session；
3. **说话人注入**：user_text 前缀 `[来自钉钉群·<senderNick>(staffId 后4位)] <正文>`，多轮后 Agent 能区分发言人（与 Ginno 现有 mention_context 注入风格一致）；
4. ask_user 卡片题干自动加「（@<发起人> 请作答）」；答案映射回发起 turn 的用户上下文；
5. **抢答消解**：同群在途 turn 时新 @ 消息入队（F9 策略 2）；
6. 群白名单沿用 `allowed_staff_ids`（未授权成员 @ 机器人 → 群内回复「该成员未授权」或静默，配置决定）。

---

## 5. WS 帧事件 × 钉钉动作总映射表（tap 分发表）

| 帧事件（engine emit） | 载荷要点 | 钉钉动作 | 流程 |
|---|---|---|---|
| `turn.start` | turn_id | 创建 AI 卡片占位「思考中」 | F3.1 |
| `token.delta` | 文本增量 | 节流批量 → AI 卡片流式更新（打字机） | F3.2 |
| `thinking.delta` | 思考文本 | 丢弃（P2 可选折叠区） | F3.3 |
| `tool.start` / `tool.end` | 工具名/状态 | 卡片状态行「🔧 正在执行 X」 | F3.4 |
| `tool.input` / `tool.args` | 参数 | 丢弃（隐私+噪音） | — |
| `widget.emit` | kind/data | 降级 markdown（stat_list→列表 / chart→表格+结论） | F3.5 |
| `message.end` | — | flush + 卡片正文定格 | F3.6 |
| `image.emit` | 本地图片路径 | 媒体上传→picture 消息（大图预览） | F7.1 |
| `ref.emit` | kind/ref 文件 | 白名单分类：预览类文件消息 / 不可预览类+说明 | F7.2 |
| `artifacts.changed` | — | 目录 diff 兜底侦测产物 | F7.3 |
| `user.question` | id/question/header/options/allow_free_text | 互动卡片按钮 +「其他」输入 | F4 |
| `permission.request` | 工具+参数 | P1：确认卡片 allow/deny；P0：自动 deny+提示 | F5 |
| `version.propose` | workflow diff | 文本通知「到桌面端确认」 | F5 |
| `turn.stopped` | reason | 卡片终态（停止/错误标注）+ 清理定时器 | F3.8 |
| `run.bind` / `workflow.emit` / `workflows.changed` / `todos.changed` / `skills.changed` / `preview.invalidate` / `keepalive` | — | 丢弃（keepalive 计入连接健康度） | — |

---

## 6. 特性兼容性报告

### ✅ 完整支持
文本/Markdown 对话（AI 卡片流式）· ask_user（选项/自由文本/超时 skip，F4）· 图片收发与预览（F6/F7）· 常见格式文件收发与在线预览（F6/F7）· 菜单/命令（F8）· 连接状态展示（F1）· 长任务回复（服务端 API 定向，不受 sessionWebhook 30s 限制）· 多用户并发（F9）。

### ⚠️ 降级支持（有损但可用）

| 特性 | 限制 | 降级方案 |
|---|---|---|
| `render_widget`（stat_list/chart 富组件） | 钉钉无等价动态组件 | markdown 列表/表格+结论；P2 服务端渲染 PNG 走 F7 |
| `attach_ref` / Artifacts 面板引用 | 本地文件芯片不可点击 | 直接作为文件消息发送（F7.2） |
| HTML 深度报告 | 钉钉无 HTML 预览 | 发 .html 文件+说明；或提示转 PDF |
| 工具执行可视化（code panel 等） | 桌面 UI 专属 | 卡片状态行轻量呈现（F3.4） |
| 多会话/会话切换 UI | 钉钉单窗口 | per_user 固定绑定 + 菜单「新开会话」（F8） |
| Obsidian Wiki 注入/引用块 | 面向桌面会话机制 | 渠道会话默认不开（按项目配置），引用块不外发 |

### ❌ 无法支持（渠道本质差异）

| 特性 | 原因 | 处置 |
|---|---|---|
| 桌面端特权确认弹窗 UI | 钉钉无原生确认通道 | P0 自动 deny 高危；P1 用 permission 卡片承接（F5） |
| 浏览器实时操控所见即所得 | 用户不在 Ginno UI 内 | 截图可回传；操控仍由 Agent 执行 |
| 浮窗/多窗口/桌面 shell 特性 | 平台差异 | 不在渠道暴露 |
| Workflow `version.propose` 的 diff 确认交互 | unified diff 需桌面 UI | 钉钉仅文本通知（F5） |
| Workflow `human` 节点的 UI resume | 现挂 Ginno 前端 | P2 评估用 F4 同款卡片机制承接 |

---

## 7. 风险与待验证清单

1. **`resume_pending_question()` 重构**是唯一动核心的改造：提炼 `ws.py:238 _spawn_resume` + `ws.py:908-925` 分支，桌面端 WS 路径等价改写，需回归「user_answer / permission_response / 双端竞答」用例。
2. dingtalk-stream SDK 的 asyncio task 与 sidecar lifespan 融合；卡片 STREAM 回调在流式更新期间的可达性（社区有异常 case，P0 用「问题卡片与流式卡片分离」规避）。
3. AI 卡片流式更新接口的频率上限 → F3 节流参数（500ms/400 字符）需实测校准。
4. 媒体上传 robotCode 要求与大小上限（图/视频 20MB 待确认）；免费档配额（API 10000 次/月、事件 5000 次/月）对高频流式更新的消耗建模。
5. 群聊 openConversationId 获取（P1 前补调研）；STREAM 回调重推语义（断线期间按钮点击是否补投）。
6. 打包：dingtalk-stream 及依赖纳入 PyInstaller bundle；重建后必须完全重启 Ginno（CLAUDE.md zlib 已知故障）。
