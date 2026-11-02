# FAQ（常见问题）

> 面向使用者的高频问题。操作类问题见 [`user-guide.md`](user-guide.md)，
> 故障排查见 [`troubleshooting.md`](troubleshooting.md)。

## 数据与隐私

**Q：我的数据存在哪里？会不会上传？**
所有状态都在本机 `~/.ginno/` 目录下（JSON / JSONL / Markdown 纯文件）：会话、
checkpoint、设置、知识库索引。Ginno 没有数据库、没有账号、没有云同步——
唯一的网络出站是：①你配置的模型 API 端点（带你的 key）；②你主动触发的
联网搜索 / 浏览器访问；③可选的连接器（如钉钉 Stream 长连接）。

**Q：API Key 存在哪，安全吗？**
明文存在 `~/.ginno/settings.json`（与 Claude Code 等 CLI 工具的本地配置同一
形态）。Ginno 只是客户端：key 只会发往你配置的模型端点，不会发往其他任何
地方。磁盘上防的是「别人拿到你的电脑」——建议开启 macOS FileVault。

**Q：怎么备份 / 换机器迁移？**
整个 `~/.ginno/` 拷走即可（可读、可手改、可进 git）。新机器装好 Ginno 后
放回原位，会话与设置完整保留。

**Q：怎么彻底卸载？**
删除应用 + 删除 `~/.ginno/` 目录即可，系统内无其他残留（浏览器扩展在
Chrome 扩展页自行移除）。

## 模型与联网

**Q：支持哪些模型？**
任何 OpenAI / Anthropic 协议兼容端点：OpenAI、Anthropic、DeepSeek，以及
自建 / 中转兼容端点；另有 Ollama 本地模型（`local` extra）。在设置里填
Base URL + Key 即可，模型配置支持按 provider 解析 `context_window`。

**Q：必须联网吗？**
用云端模型需要联网；用 Ollama 本地模型可以全程离线（联网搜索、连接器等
功能除外）。

**Q：Ginno 收费吗？**
软件 MIT 开源免费。成本只有你自己付给模型提供商的 API 费用。

## 平台与升级

**Q：支持 Windows / Linux 吗？**
当前仅 macOS（Apple Silicon）。Tauri 本身跨平台，瓶颈在 Python sidecar 的
分平台打包验证——在评估中（见 [`../ROADMAP.md`](../ROADMAP.md)），暂无承诺时间表。

**Q：如何升级？**
目前手动：从 Releases 下载新 dmg 覆盖安装，`~/.ginno/` 数据不受影响。
**升级前请完全退出旧版**（⌘Q）。详见 [`upgrade-guide.md`](upgrade-guide.md)；
应用内自更新在路线图中。

**Q：为什么首次打开提示不受信任 / 要右键打开？**
当前 dmg 未做 Apple 开发者签名 + 公证（个人项目阶段）。首次启动右键 →
打开，或在「系统设置 → 隐私与安全性」放行。签名公证在路线图上。

## 与其他产品的关系

**Q：和 Claude Code 是什么关系？**
无官方关系。Ginno 在产品形态上借鉴了 Claude Code 的能力面（工具 / 权限 /
技能 / MCP / hooks），但它是完全独立的桌面应用，不依赖也不封装 Claude Code。
详细对比见 [`comparison.md`](comparison.md)。

**Q：为什么叫 local-first？和「离线优先」是一回事吗？**
不是。local-first 强调**数据主权**：状态的本拷贝在你机器上、格式开放可迁移，
即使软件停更你的数据也完好可用。联网能力（云模型、搜索）照常可用。
