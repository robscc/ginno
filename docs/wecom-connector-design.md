# 企业微信智能机器人连接器设计（P0+P1）

> 2026-10-28 实施。以 `docs/dingtalk-connector-design.md` 为蓝本（channels/ 分层
> 完全复用），差异点集中在协议层：企微智能机器人走 **WebSocket 长连接**
> （无公网回调 URL、无加解密配置），凭证只有 BotID + Secret 两个。
> 官方协议文档：developer.work.weixin.qq.com/document/path/101463。

## 1. 概述

企业微信「智能机器人」（API 模式 + 长连接）作为 Ginno 的入站渠道：

- 企微用户在单聊/群聊/频道（话题）中发消息（群聊/频道需 @机器人）；
- sidecar 与 `wss://openws.work.weixin.qq.com` 保持一条长连接收推送；
- 消息驱动映射的 Ginno session 跑 headless turn，回复以 **stream 流式消息**
  全量替换式推回企微（类打字机）；
- ask_user 以文本作答（编号选项），文件/图片双向互通。

## 2. 配置（Connector 层）

| 键 | 类型 | 说明 |
|---|---|---|
| `bot_id` | string | 智能机器人 BotID（企微后台 API 配置页） |
| `secret` | string (password) | 智能机器人 Secret |
| `allowed_userids` | string[] | 白名单（空=不限；未授权静默忽略） |
| `default_agent_id` | string | 渠道入口绑定的 agent |
| `session_policy` | enum | `per_user`（默认，按 user+chat 映射）/ `per_conversation`（整个会话共用） |
| `ask_timeout_s` | int | ask_user 停车超时自动 skip |
| `welcome_text` | string | enter_chat 欢迎语（空=关闭） |

## 3. 模块布局（channels/wecom/）

| 文件 | 职责 |
|---|---|
| `protocol.py` | 纯协议层：帧构造/解析、时限常量、消息 body 提取、@剥除、作答解析（可单测） |
| `client.py` | WS 传输层：建连/订阅/心跳(30s, 3 次丢 pong 断)/req_id 回执关联/回调分发 |
| `adapter.py` | 生命周期监督（退避重连、凭证错不重试）+ 消息路由（白名单→待答短路→turn） |
| `tap.py` | 引擎帧 → 企微动作：stream 续流、问题文本化、产物上传、超时降级 |
| `media.py` | 下行：url+aeskey 下载 + AES-256-CBC 解密；上行：init/chunk/finish 分片上传 |
| `sessions.py` | userid/chatid ↔ ginno session 映射（同钉钉，原子写+损坏重建） |

复用不改：`channels/base.py`（`ChannelAdapter`/`ChannelTap`/`run_channel_turn`/
`ensure_channel_session`）、registry 状态机、`api/connectors.py`、前端 schema
驱动表单。**顺带修复 main 既有 bug**：`base.py` 的 `report_status` 引用了不
存在的 `get_registry`（钉钉渠道状态上报同样受影响）。

## 4. 协议要点（与钉钉的关键差异）

帧统一 `{"cmd", "headers": {"req_id"}, "body"}`；**ack 的 errcode 在顶层**
（不在 body）。

- 订阅：`aibot_subscribe` `{bot_id, secret}` → ack errcode!=0 即凭证错误
  （`WecomAuthError`，adapter 报 error 不重试）。
- 回调：`aibot_msg_callback`（msgtype text/image/file/mixed/voice/video，
  `from.userid`、群聊带 `chatid`、频道 `compat_info.source=="channel"`）、
  `aibot_event_callback`（enter_chat / template_card_event / feedback_event）。
- 回复：`aibot_respond_msg` 透传回调 req_id；stream 消息 `{id, content,
  finish}`，content **全量替换**；时限：**收到回调 5s 内首帧**（adapter 受理
  即发占位帧兜底）、**流式全程 ≤10min**（9.5min 安全线强制 finish，残余走
  `aibot_send_msg` 主动推送收尾）、回复窗口 24h。
- 群聊只推被 @ 的消息；频道全量推送 → tap 侧按正文是否 @ 过滤
  （`strip_mention_prefix` 剥掉 @ 前缀再喂 agent）。
- 上传：三段式分片（512KB/片 ≤100 片）得 media_id（3 天有效），被动回复
  file/image 消息；下载 url 5 分钟有效 + AES 解密（key=base64 32B, iv=key[:16]）。

## 5. 关键流程

### F-路由 收到消息（adapter.handle_msg）
白名单 → text?(频道需@/剥@) → **待答短路**（`_pending_q` 命中且 session 在
`_PENDING_RESUME` → parse_answer → `resume_pending_interrupt`，不开新 turn）
→ `_begin_stream`（5s 占位帧）→ 会话映射 → 在途锁（locked → 占位帧定格提示）
→ `run_channel_turn`。

### F-流式（tap）
占位帧（adapter）→ token.delta 攒增量、3s 节流发全量 → message.end flush →
finish=true 定格（幂等）；user.question → 问题+编号选项写进流 + 登记
`_pending_q` + 超时 skip；permission.request → 自动 deny + 提示（P0 策略）；
image/ref.emit → 分片上传 + file/image 被动回复；turn.stopped → 定格+备注。

### F-媒体
下行（image/file/mixed）：占位帧 → 下载解密 → `session_files_dir` →
attached_files → turn（usage_source=wecom_file）。上行：见上。

### F-生命周期
`apply_config`（PATCH 时）→ enabled+凭证 → stop 旧 → start 新；**启动恢复**
（`_wecom_maybe_autostart`，钉钉 P0 缺口的补课）：seed 时读 merged config，
enabled 则 create_task 拉起。监督循环：断线 → 指数退避(1→60s+抖动)，稳定
60s 重置退避；凭证错误 → STATUS_ERROR 停止重试。

### F-测试连接
`POST /api/connectors/wecom-bot/test`：一次性建连+订阅+断开（不占长连接位），
恒 200，`ok:false` 带原始错误。注意单 bot 仅允许一条活跃长连接，新连接会把
旧连接踢掉（服务端会有 disconnected 通知/直接断开，监督循环自动重连）。

## 6. 降级与限制

- 语音/视频消息：P1 不消费（静默忽略）；模板卡片互动：P2。
- 流式帧计入单会话限频（~30 msg/min）→ tap 3s 节流留余量。
- 流式超 9.5min 强制定格 + 残余 markdown 主动推送（`aibot_send_msg` 仅支持
  markdown/template_card）。
- secret 明文存 `settings.json`（与钉钉一致的既有取舍）。

## 7. 验证记录（2026-10-28，真实凭证调试）

1. 单测 `tests/unit/test_wecom_channel.py` 20 用例全绿（含订阅死锁回归：
   recv 循环必须先于订阅 ack 等待启动）。
2. 真实探活：订阅 ack `errcode:0`，心跳 ack 顶层 errcode 格式确认。
3. `POST /api/connectors/wecom-bot/test` → `{"ok":true}`。
4. dev runtime（源码模式）PATCH 启用 → `connected / Long connection subscribed`；
   重启 runtime 验证启动恢复自动拉起 → 同样 connected。
5. 端到端消息回路通过：企微单聊发消息 → 白名单/路由 → 占位流帧 →
   headless turn（session `58de1a21…`，9 事件）→ stream 全量替换式流式
   回复 + finish 定格；映射表落盘 `~/.ginno/channels/wecom/mappings.json`。

调试中修复的 4 个 bug：
- **订阅死锁**（wecom client）：recv 循环晚于订阅 ack 等待 → ack 无人读，
  超时；改为先起 recv 再等 ack（已加回归测试）。
- **群聊 @ 分隔符吞正文**（wecom protocol）：企微 @ 后的分隔符是 U+2005
  窄空格而非 ASCII 空格，原 `partition(" ")` 把正文整体吞掉、消息静默丢弃；
  改为 Unicode 空白感知的正则（已加回归测试）。
- **`report_status` 引用不存在的 `get_registry`**（channels/base.py，main
  既有 bug，钉钉渠道状态上报同受影响）→ 改为 `registry()`。
- **`ensure_channel_session` 只认 `session_id`**（channels/base.py，main 既有
  bug——`create_session` 返回 meta 形状主键是 `id`，钉钉新会话同样建不起
  来）→ 两个键都认。

渠道会话标题：新建会话带「企业微信 · <userid>」标题（`ensure_channel_session`
新增 `title` 参数），侧栏可直接辨认来源；群聊亦验证通过（2026-10-28）。
