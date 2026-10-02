<div align="center">

# Ginno

**把你自己电脑变成 AI Agent 工作台的本地优先桌面应用。**

*Claude Code 的形态 × 全文件存档（无数据库 · 无账号 · 无云同步）× 可视化 Workflow Studio。*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-macOS%20(Apple%20Silicon)-black.svg)
![No cloud](https://img.shields.io/badge/cloud-none-brightgreen.svg)

[English](README.md) · [简体中文](README.zh-CN.md)

![Ginno 聊天工作台](docs/smoke/ui-workspace.png)

</div>

---

## 它是什么

Ginno 把"AI Agent 该有的能力"做成一个**完整的桌面应用**，而不是散落在终端和十几个浏览器标签里。

- 🧠 **多 Agent 对话** —— 一个会话内可切换 Agent、历史共享，每个 Agent 有独立记忆
- 🛠️ **工具 · 权限 · 技能 · MCP · Hooks** —— 对齐 Claude Code 的形态，全套内置
- 🔀 **可视化 Workflow Studio** —— 版本化 DSL 编译成 LangGraph 状态图，带实时运行观察台
- 📚 **本地知识库** —— 直接索引你的 Obsidian Vault，回答带引用溯源
- 🎯 **Goal 长程推进 + TODO 同步** —— 让 Agent 盯着一个长期目标持续跑
- 🌐 **内置浏览器 + 网页搜索** —— 带来源标注的检索，不瞎编

> 完整功能见 [`docs/user-guide.md`](docs/user-guide.md)。

| Workflow Studio | 本地知识库 |
|---|---|
| ![Workflow Studio](docs/smoke/ui-workflow-tab.png) | ![知识库](docs/smoke/kb-overview.png) |

---

## 为什么是"本地优先"

一条设计原则：**你的数据不该离开你的电脑。**

| | Ginno |
|---|---|
| 数据库 | ❌ 无 —— 所有状态就是 `~/.ginno/` 下的 JSON / JSONL / Markdown |
| 账号体系 | ❌ 无 —— 打开就能用 |
| 云同步 | ❌ 无 —— 全部本机 |
| 可迁移 | ✅ 拷走目录即可搬家；可读、可手改、可 git 管理 |

模型调用由你自己配置（OpenAI / Anthropic / DeepSeek，或任意兼容端点）。
**Ginno 只做客户端，不接管你的 Key，也不接触你的数据。**

---

## 安装

**方式 A —— 下载安装包（macOS，Apple Silicon）：** 到 [Releases](https://github.com/robscc/ginno/releases)
下载最新 `.dmg`。目前**未签名**，首次打开请用 **右键 → 打开** 绕过 Gatekeeper。

**方式 B —— 从源码构建：**

```bash
git clone https://github.com/robscc/ginno
cd ginno

pnpm install                       # 前端 + 桌面壳
cd packages/runtime && uv sync     # Python 运行时
cd ../..

pnpm dev                           # 一键起 web + runtime + 桌面壳
```

需要：Node ≥ 20 · pnpm ≥ 9 · Python ≥ 3.11（用 `uv`）· Rust（Tauri）。

**打包成 `.app`：** `make app` → `apps/desktop/target/release/bundle/dmg/Ginno_*.dmg`
（详见 [`docs/p3-packaging-notes.md`](docs/p3-packaging-notes.md)）。

---

## 技术栈

| 层 | 选型 |
|---|---|
| 壳 | Tauri 2（Rust）—— 仅进程管理 + webview |
| UI | Next.js 14（静态导出）+ React 18 + Tailwind |
| 运行时 | Python + FastAPI + LangGraph（sidecar，PyInstaller 打包） |
| 存储 | 纯文件（无 DB）；可选 LanceDB 做语义检索向量缓存 |

> 架构与子系统设计见 [`docs/architecture.md`](docs/architecture.md)。

---

## 状态

- 一个**个人项目**，日常使用中（dogfooding），高频迭代 —— 约 2.5 个月、220+ commits。
- 尚早、不成熟，API 与界面仍在变动。
- 欢迎 Issue / PR / 想法，中英文都可以。

## License

[MIT](LICENSE) © 2026 ChuanchuanSong