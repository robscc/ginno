# CLAUDE.md — Ginno 项目排障指南

Ginno 是本地 AI Agent 桌面应用：Tauri(Rust) 壳 + Next.js 前端 + Python(LangGraph/FastAPI) sidecar。
版本节奏：语义化版本 + tag 发版（见下「发版」），CHANGELOG.md 记账。

## ⚠️ 排障第一步：别看错目录

**Ginno 的数据和日志在 `~/.ginno/`，不在 `~/.claude/`。**
`~/.claude/` 是 Claude Code 自己的目录，和 Ginno 无关。

| 内容 | 位置 |
|---|---|
| **运行时日志（排障首选）** | `~/.ginno/logs/sidecar.log`（Python 独占轮转 5MB×3；含 uvicorn 访问日志，badge/health/tasks 等 GET 轮询已过滤，4xx/5xx 保留） |
| sidecar 原始 stdio（崩溃兜底） | `~/.ginno/logs/sidecar-stdio.log`（shell 追加、超 5MB 重启时截断；勿再写 sidecar.log，避免双 writer 轮转分裂） |
| 会话/checkpoint 数据 | `~/.ginno/projects/<slug>/sessions/<session_id>.jsonl`（追加式日志；旧版 `.json` 仍可读并自动迁移） |
| 设置 | `~/.ginno/settings.json`、`~/.ginno/config.json` |
| token 用量 | `~/.ginno/usage/requests-YYYY-MM-DD.jsonl` |
| 项目规则 / 记忆 | `~/.ginno/projects/<slug>/GINNO.md`、`~/.ginno/MEMORY.md` |

日志里的报错格式：`<时间> ERROR <事件> session=<id> turn=<id>`，traceback 紧跟其后。
用 `rtk proxy grep/sed` 查日志可避免 rtk 压缩输出导致的行号/内容截断。
CI 排障：`git credential fill` 取 token 后 curl `api.github.com/repos/robscc/ginno/actions/jobs/<id>/logs`（job id 从 run 的 jobs API 取）；GitHub 网页被 DNS 污染但 api.github.com 可直连。

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

### 2. 时间敏感测试在 CI（UTC runner）翻车

- CI runner 是 **UTC 时区 + 2 核（慢）**。两类坑已实际发生：
  - `test_schedule_api` 的 timeline 断言在 UTC 23:30 后当天网格不足点 → 已按「不足 2 点切明天」修复（2026-11-03）。**写任何依赖「今天/当天剩余时间」的测试前先想 UTC 边界。**
  - e2e 里「查 history 断言 checkpoint 产物」存在落盘竞态，CI 慢机器放大 → 用轮询等待（见 `test_ask_user_flow.py` 的 `_wait_question_blocks`），不要裸查一次就断言。

## 构建

- 改完代码后跑 `make app`（见全局 memory）。
- `make app` = web + PyInstaller sidecar bundle + Tauri 桌面应用（含签名校验，
  仅 linker-signed 会导致 webview 白屏）；构建前有防呆：检测到 Ginno 在运行或
  8787 被 Ginno sidecar 占用即报错退出（对应上面 zlib 已知故障），`FORCE=1` 跳过。
- `make app TAURI_ARGS="--config <json>"` 可透传 tauri build 参数——CI 的 release.yml
  用它把 `signingIdentity` 覆盖为 ad-hoc（`"-"`，runner 无本地自签 identity）。
- `make mod-broker` / `make mod-runner`：mods 兼容层两个组件——broker CLI
  （→ `<repo>/target/release/ginno-mod-broker`，即 Python 的发现梯子；桌面版不
  用它，broker 以 path dependency 编译进 Tauri 进程内）与 mod-runner.mjs
  （esbuild 单文件，`make sidecar` 会一并拷进 Tauri resources）。桌面启动时
  shell 进程内起 broker 监听 app 数据目录的 Unix socket，并向 sidecar 注入
  env `GINNO_MOD_BROKER_SOCK` / `GINNO_MOD_BROKER_TOKEN` / `GINNO_MOD_RUNNER_PATH`；
  broker 没起来时不注入 env，Python 走 dev-spawn / `~/.ginno/bin` 发现梯子。
- 开发调试可用 `pnpm dev`（web:3000 + runtime:8787 + Tauri），dev 模式下 runtime
  直接跑源码（无 PyInstaller），不存在上述 bundle 问题。
- 生产 bundle 位于 `apps/desktop/target/release/bundle/macos/Ginno.app`，
  runtime bundle 位于 `packages/runtime/dist/ginno-runtime/`。

## 发版（v* tag → 自动出 dmg）

流程见 `CONTRIBUTING.md` 的 Release runbook。要点：

1. bump 三处版本（`apps/desktop/tauri.conf.json` / `apps/web/package.json` /
   `packages/runtime/pyproject.toml`）后**必须 `uv lock`**——Makefile 的 runtime
   构建走 `uv run --locked`，lock 里记录的 workspace 版本不同步会直接挂。
2. CHANGELOG.md：Unreleased → 新版本；commit `release: vX.Y.Z`；`git tag` + push。
3. `.github/workflows/release.yml` 自动：macOS runner `make app`（ad-hoc 签名）→
   `make smoke` 闸门 → dmg 附到 GitHub Release。首跑约 12 分钟。
4. Release notes 手工补双语版 + SHA-256（下载 dmg 后 `shasum -a 256`）。

## 测试与 CI

**CI 三栈**（`.github/workflows/ci.yml`，每次 push main / PR 全跑）：

| Job | 内容 |
|---|---|
| `runtime-tests` | `uv run --group test --extra docs pytest -q --cov`（coverage 上传 Codecov） |
| `web` | pnpm lint + typecheck + **vitest** + next build |
| `rust` | cargo fmt --check + cargo check（desktop + mod-broker；runner macos-14） |

**本地跑测试**：

```bash
# runtime ——⚠️ 必须带 --extra docs：extractors/preview/document-tools 等 50+ 用例
# 依赖 openpyxl/pandas/docx/pptx/pypdf；bare --group test 环境会缺（CI 曾因此 48 红）
cd packages/runtime && uv run --group test --extra docs pytest -q          # 全量（~6 分钟）
uv run --group test --extra docs pytest -m unit -q                        # 纯单测
# 前端
cd apps/web && pnpm test          # vitest（src/lib/*.test.ts，node 环境，无 DOM）
```

**测试基建红线**（都踩过坑，别再踩）：

- **禁止在 `tests/e2e/`（或任何子目录）建 `conftest.py`**——它会遮蔽模块名 `conftest`，
  让全仓库的 `from conftest import event_names/script/...` 解析到空壳（26 文件收集
  错误，main CI 红过一个多星期才被修复，2026-11-03）。共享 fixture 一律放根
  `tests/conftest.py`。
- `server_shared._RUNNING_TURNS` 由根 conftest 的 autouse fixture 每测试前 `clear()`
  （e2e 段残留会污染后跑的 unit/api 断言）。用 `clear()` 保对象同一性——
  `tasks/inject.py`、`running_work.py` 持有同一 dict 的引用，setattr 替换会分裂。
- graph 的 429/限流错误**不再立即失败**：走 `rate_limited_backoff`（30s 起指数退避 ×5，
  共 ~15 分钟）。测试 429→usage_limited 之类语义时 monkeypatch `graph._RATE_LIMIT_WAIT_S`
  （见 `test_goal_continuation.py` 的先例）。
- 打包 UI 的 playwright e2e（`test_packaged_ui_*`）需要运行中的 dist sidecar——
  本地 8787 被 Ginno/dev 占着会挂，CI 干净环境自动 skip。跑它们前先退出 Ginno。

## 代码结构速查

- `packages/runtime/src/ginno_runtime/` — Python 运行时全部源码
  - `api/` — 按域拆分的 router（sessions/stream/workflows/files/knowledge/config/
    todos/usage/memory/teams/tasks/plans/schedule/connectors/…，25+ 文件）；
    `api/stream/` 是 turn 执行/WS 流的**包**（`resume.py` 含 interrupt 公共恢复入口）
  - `graph.py` — 聊天主图（agent/permission/tools 三节点，纯 `Command(goto=…)` 路由，
    无 permission→tools 静态边；含限流退避）
  - `browser/` — 浏览器双轨（browser-companion-extension-design.md）：`executor.py`
    B 轨专用 profile Chrome + CDP;`relay.py` 扩展轨 WS 端点;`scripts.py` 是
    双轨共用的页面注入脚本（扩展 content scripts 由此生成,单一来源）;
    `native_host.py` 物化扩展到 `~/.ginno/browser-extension/` + 端口发现
  - `channels/` — IM 渠道接入：`dingtalk/`（Stream 模式 wss）、`wecom/`，共用 `base.py`
  - `connectors/` — 连接器模块（connector-module-design.md）：registry + 内置
    chrome-extension / browser-profile 连接器;API 在 `api/connectors.py`
  - `checkpointer.py` — 文件式 LangGraph checkpointer（full/delta 两种模式）
  - `compaction.py` / `microcompact.py` — 上下文压缩（语义压缩，非 zlib）
  - `server.py` — FastAPI 装配壳；进程级状态在 `server_shared.py`
- `packages/extension/` — Ginno Chrome 扩展源码（manifest 带固定 key,
  ID `jlmheiiglpdikeihgjefjhmoakfllhkm`）;`build.py` 构建/物化;扩展排障看
  popup 连接状态 + `chrome://extensions` 错误按钮
- `packages/mod-runner/` — mods 兼容层的 node 侧 runner（esbuild 单文件产物）
- `crates/mod-broker` — broker CLI（Rust，桌面版编译进 Tauri 进程内）
- `apps/web/src/` — Next.js 前端（`src/lib/*.test.ts` 为 vitest 单测）；
  `apps/desktop/` — Tauri 壳（Rust 源码在 `apps/desktop/src/`，Cargo.toml /
  tauri.conf.json 直接在此目录）
