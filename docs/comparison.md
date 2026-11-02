# Ginno 与同类产品的定位对比

> 目的：帮你快速判断 Ginno 适不适合你。对比基于各项目**公开资料与自我定位**，
> 不是跑分评测；各家迭代都很快，细节以官方文档为准（口径时间：2026-11）。

## 一句话定位

**Ginno = 把「本地优先」做到底的个人 AI Agent 桌面应用**：Claude Code 的
能力形态（工具 / 权限 / 技能 / MCP / hooks），加上可视化 Workflow Studio
和本地知识库；无数据库、无账号、无云，全部状态是你机器上 `~/.ginno/` 里
可读、可改、可 git 的纯文件。

## 对比表

| | Ginno | Claude Code | Cline / Continue | Open WebUI / Cherry Studio | LangGraph Studio |
|---|---|---|---|---|---|
| 形态 | 独立桌面 app | 终端 CLI | IDE 插件 | 网页 / 桌面聊天前端 | 伴随 LangGraph 的工作流调试 UI |
| 数据归属 | `~/.ginno/` 纯文件，完整可迁移 | `~/.claude/` 等 | IDE 工作区 | 多为本地 + 可选云 | 依赖 LangGraph 项目 |
| 账号 / 云 | 无 / 无 | Anthropic 账号（或 API key） | API key | 自托管需服务 | — |
| 模型 | 任意 OpenAI/Anthropic 兼容端点 + Ollama | Anthropic（含兼容端点） | 多家 | 多家 | 取决于宿主 |
| 可视化工作流 | ✅ 版本化 DSL → LangGraph 编译 + 运行观察 | ❌ | ❌ | ❌ / 有限 | ✅（调试向） |
| 本地知识库（带引用） | ✅（索引 Obsidian Vault） | ❌（需自配） | 代码库索引向 | 部分有 RAG | ❌ |
| 工具 / MCP / skills / hooks | ✅ 内置 | ✅ | MCP 为主 | 插件机制 | — |
| 连接器（IM 入口） | ✅ 钉钉 / 企业微信 | ❌ | ❌ | 部分 | ❌ |
| 平台 | macOS (Apple Silicon) | macOS / Linux / Windows | 跟随 IDE | 跨平台 | — |
| 阶段 | **v0.1+，个人项目高速迭代** | 成熟商用 | 成熟 | 成熟 | 成熟 |

## 什么时候选 Ginno

- 你想要 **Claude Code 式的 agent 能力**，但要在**图形界面**里用，且会话、
  记忆、工作流、知识库、IM 入口在一个应用里闭环。
- 「数据不出我的机器」是硬需求：无 DB、无账号、无云同步，`~/.ginno/` 可
  备份可审计可进 git。
- 你用 Obsidian，想让 agent 直接引用你的知识库作答（带出处）。
- 你想把重复劳动固化成**可视化工作流**并挂定时任务。

## 什么时候暂时别选

- 你需要 Windows / Linux——还在评估（[ROADMAP](../ROADMAP.md)）。
- 你需要开箱即用的官方签名 / 自动更新——目前手动升级 + 未公证 dmg。
- 你要的是团队级多人协作 / 云端部署——Ginno 明确不做账号与云
  （[ROADMAP](../ROADMAP.md)「Not planned」）。

## 诚实声明

Ginno 是个人项目，成熟度与上述成熟产品不在一个量级；列表中的 ✅ 以
[`user-guide.md`](user-guide.md) 的实测标注为准。发现对比失实欢迎 Issue 指正。
