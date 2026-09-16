# CLAUDE.md — Ginno 项目排障指南

Ginno 是本地 AI Agent 桌面应用：Tauri(Rust) 壳 + Next.js 前端 + Python(LangGraph/FastAPI) sidecar。

## ⚠️ 排障第一步：别看错目录

**Ginno 的数据和日志在 `~/.ginno/`，不在 `~/.claude/`。**
`~/.claude/` 是 Claude Code 自己的目录，和 Ginno 无关。

| 内容 | 位置 |
|---|---|
| **运行时日志（排障首选）** | `~/.ginno/logs/sidecar.log` |
| 会话/checkpoint 数据 | `~/.ginno/projects/<slug>/sessions/<session_id>.json` |
| 设置 | `~/.ginno/settings.json`、`~/.ginno/config.json` |
| token 用量 | `~/.ginno/usage/requests-YYYY-MM-DD.jsonl` |
| 项目规则 / 记忆 | `~/.ginno/projects/<slug>/GINNO.md`、`~/.ginno/MEMORY.md` |

日志里的报错格式：`<时间> ERROR <事件> session=<id> turn=<id>`，traceback 紧跟其后。
用 `rtk proxy grep/sed` 查日志可避免 rtk 压缩输出导致的行号/内容截断。

## 已知故障模式

### 1. `zlib.error: Error -3 while decompressing data: incorrect header check`
（也可能报 `unknown compression method`）

**症状**：traceback 末尾是 `pyimod01_archive.py extract`；进程启动正常、请求正常，
但某个 turn 开始所有**懒加载 import** 失败（microcompact / compaction / graph 等）。

**根因**：桌面版运行时跑在 PyInstaller bundle 里。PyInstaller 每次 extract 都**按路径重新打开**
归档文件。`make app` / `make sidecar` 在 sidecar 运行期间替换了 bundle 后，进程内存里的旧
TOC 偏移量读到新归档的垃圾数据 → zlib 解压失败。**只有重启才能恢复。**
（2026-08-08 与 2026-08-10 各发生过一次，均为重建后未重启应用。）

**处理**：完全退出并重启 Ginno 应用即可，会话数据不受影响。

**规则：`make app` 之后必须完全退出并重新打开 Ginno 再验证，否则必然复现此错误。**

## 构建

- 改完代码后跑 `make app`（见全局 memory）。
- `make app` = web + PyInstaller sidecar bundle + Tauri 桌面应用（含签名校验，
  仅 linker-signed 会导致 webview 白屏）。
- 开发调试可用 `pnpm dev`（web:3000 + runtime:8787 + Tauri），dev 模式下 runtime
  直接跑源码（无 PyInstaller），不存在上述 bundle 问题。
- 生产 bundle 位于 `apps/desktop/target/release/bundle/macos/Ginno.app`，
  runtime bundle 位于 `packages/runtime/dist/ginno-runtime/`。

## 代码结构速查

- `packages/runtime/src/ginno_runtime/` — Python 运行时全部源码
  - `api/stream.py` — turn 执行/WS 流（microcompact、compaction 的懒加载入口）
  - `checkpointer.py` — 文件式 LangGraph checkpointer（full/delta 两种模式）
  - `compaction.py` / `microcompact.py` — 上下文压缩（语义压缩，非 zlib）
  - `server.py` — FastAPI 装配
- `apps/web/src/` — Next.js 前端；`apps/desktop/src-tauri/` — Tauri 壳
