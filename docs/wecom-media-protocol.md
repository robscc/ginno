# 企微智能机器人媒体发送协议查证（wecom 渠道发媒体）

> 查证日期：2026-11-07。来源：企微官方文档「智能机器人 长连接模式」
> <https://developer.work.weixin.qq.com/document/path/101463>（页面标注最后更新 2026/10/10）。
> 本文档服务于 `feat/wecom-send-media`：把 agent 产物（图片/文件）通过企微
> 智能机器人渠道发给用户。**注意与群机器人 webhook（`qyapi.weixin.qq.com`
> 那套）不是同一协议**——Ginno wecom 渠道是智能机器人长连接（WSS）形态。

## 1. msgtype 支持矩阵（发送侧）

| 通道 | cmd | 支持的 msgtype | 媒体可发？ |
|---|---|---|---|
| 被动回复（透传消息回调 req_id） | `aibot_respond_msg` | stream / template_card / markdown / **file** / voice / **image** / video | ✅ image、file 原生支持 |
| 主动推送（chatid 寻址，无回调） | `aibot_send_msg` | **仅 template_card、markdown** | ❌ 不支持 image/file |

**结论**：媒体只能走**被动回复**（`aibot_respond_msg` + 回调 req_id）。
主动推送场景（`WeComTap.push_only`，如 steer 补投/任务收尾）**无法发媒体**，
只能降级 markdown 路径回执——这是协议决定的，不是实现偷懒。
主动推送另有前置条件：用户须先在会话中给机器人发过消息（Ginno 的
chat_key 映射天然满足）。

## 2. 消息体格式（respond 侧）

```jsonc
// 文件消息
{"msgtype": "file", "file": {"media_id": "MEDIA_ID"}}
// 图片消息
{"msgtype": "image", "image": {"media_id": "MEDIA_ID"}}
```

media_id 由「上传临时素材」三段式接口获得（见 §4）。与
`channels/wecom/protocol.py::media_body()` 现有构造一致。

## 3. 时限与频率

| 项 | 值 |
|---|---|
| 回复窗口（req_id 有效期） | 收到消息回调后 **24 小时** |
| 频率限制（回复+推送合计，每会话） | **30 条/分钟、1000 条/小时** |
| 流式消息全程上限 | 10 分钟（`STREAM_SAFE_S` 9.5 分钟安全线已实现） |
| 回调首条回复时限 | 5 秒（占位流已实现） |

## 4. 上传临时素材（`aibot_upload_media_init` / `_chunk` / `_finish`）

三段式：init 取 `upload_id` → 逐片 base64 上传 → finish 合并返回 `media_id`。

| 项 | 值 | 代码落点 |
|---|---|---|
| 上传类型 `type` | file / image / voice / video | `media.upload_media(media_type=…)` |
| **大小上限** | **图片 ≤10MB、语音 ≤2MB、视频 ≤10MB、普通文件 ≤20MB**；最少 5 字节 | 本 PR 在 `upload_media` 增加按类型预检 |
| **图片格式白名单** | **仅 png / jpg(jpeg) / gif**——**webp 不在列** | 本 PR：wecom 侧 webp 按 file 类型发 |
| 单分片 | ≤512KB（base64 编码前） | `MEDIA_CHUNK_BYTES` 已实现 |
| 分片数 | ≤100（理论 ~50MB，实际受 §类型上限约束更紧） | `MEDIA_MAX_CHUNKS` 已实现 |
| 上传会话（upload_id）有效期 | **30 分钟**，超时自动清理；分片幂等、可乱序、断线可续传 | 单连接内串行上传，无续传需求 |
| **media_id 有效期** | **3 天** | 立发立用，无缓存价值 |
| 上传频率（每 bot） | 30 次/分钟、1000 次/小时 | 产物逐个上传，正常用量远低 |

## 5. Ginno 侧设计决策（依据上述协议）

1. **发送封装**：`WecomClient.send_image(req_id, media_id)` /
   `send_file(req_id, media_id)`——都是 `aibot_respond_msg` 被动回复的
   薄封装（主动推送协议不支持媒体，故无 chatid 版本）。
2. **分流**（tap `_send_artifact`，仿钉钉先例但按企微协议调整）：
   扩展名白名单 `_SENDABLE`（未知类型不外发）→ `png/jpg/jpeg/gif` 走
   image、**webp 降为 file**（官方图片格式不含 webp）、其余走 file →
   `upload_media` → `send_image/send_file`。
3. **失败降级**：上传或发送任一环节异常 → markdown 路径回执
   （"📎 产物文件（企业微信上传失败，见桌面端）: path"）+ **warn 日志**
   （仓库规矩：渠道发送失败的静默丢弃必须带 warn）。
4. **push_only tap（req_id 为空）**：跳过上传（`aibot_send_msg` 发不了
   媒体，上传了也必败），直接 markdown 路径回执走主动推送出口。
5. **大小预检**：`upload_media` 按类型上限（image 10MB / file 20MB）在
   init 前本地拒绝，超限直接走降级路径，省一次注定失败的三段式。

## 6. 不确定项（如实标注）

- **未做真机验证**：以上全部来自官方文档静态查证 + 官方/社区 SDK 对照
  （`protocol.py` 头注）。真实企微客户端收图/收文件的端到端表现由父会话
  安排用户配合验收。
- voice / video 上传类型虽在协议内，Ginno 产物上行暂不涉及（白名单里
  本来就没有音频/视频扩展名），未实现对应分支。
- 文档未明说 file 类型上传是否有格式限制（仅图片明列格式）；按「无限制」
  处理，若真机发现特定格式被拒再补白名单。
- `aibot_send_msg` 的 `chat_type` 字段（1 单聊/2 群聊/0 兼容）当前
  `markdown_push_body` 未设置（走 0/缺省兼容模式），与媒体发送无关，未动。
