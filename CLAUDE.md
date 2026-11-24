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

**规则：如果构建时 Ginno 仍在运行，出包后须用 ⌘Q 完全退出，再打开新包验证。**

### 2. 时间敏感测试在 CI（UTC runner）翻车

- CI runner 是 **UTC 时区 + 2 核（慢）**。两类坑已实际发生：
  - `test_schedule_api` 的 timeline 断言在 UTC 23:30 后当天网格不足点 → 已按「不足 2 点切明天」修复（2026-11-03）。**写任何依赖「今天/当天剩余时间」的测试前先想 UTC 边界。**
  - e2e 里「查 history 断言 checkpoint 产物」存在落盘竞态，CI 慢机器放大 → 用轮询等待（见 `test_ask_user_flow.py` 的 `_wait_question_blocks`），不要裸查一次就断言。

## 构建

- **本地桌面打包**：Ginno 已退出时跑 `make app`。如果构建时需要保持 Ginno 运行，
  用 `make app-force`（等价于 `make app FORCE=1`）。它会跳过进程守卫，出包后须用
  ⌘Q 完全退出旧进程、再打开新包。用 `/api/health` 确认构建指纹已更新且
  `runtime_stale` 为 `false`，再做桌面验证。
- 两条命令都构建 web、PyInstaller sidecar 和 Tauri 桌面应用并校验签名。
  普通 `make app` 在 Ginno 运行或其 sidecar 占用 8787 时会退出，避免运行中换包。
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
- **开发热切换（`docs/runtime-hot-swap-design.md`）**：改完 runtime 代码后
  `make dev-swap`（或会话里 `/dev-swap`）一条命令完成「单测 → PyInstaller 重建 →
  blue-green 预检 → symlink 原子切换」，随后壳自动重启 sidecar 到新 bundle 并
  验证 `/api/health` 的 build 指纹，失败自动回滚旧版本——**无需退出重开应用**。
  回出厂：`make dev-swap-clean`。侧栏品牌行（GinnoWork 右侧）出现琥珀 `dev·{指纹}` 黄条即在跑开发 bundle。
- 生产 bundle 位于 `apps/desktop/target/release/bundle/macos/Ginno.app`，
  runtime bundle 位于 `packages/runtime/dist/ginno-runtime/`。

## 发版（v* tag → 自动出 dmg）

流程见 `CONTRIBUTING.md` 的 Release runbook。要点：

1. bump **四处**版本（`apps/desktop/tauri.conf.json` / `apps/web/package.json` /
   `packages/runtime/pyproject.toml` / **`packages/runtime/src/ginno_runtime/_version.py`**）
   后**必须 `uv lock`**——Makefile 的 runtime 构建走 `uv run --locked`，lock 里记录的
   workspace 版本不同步会直接挂。
   ⚠️ 第四处是 2026-11-15 补的：`_version.py` 曾长期停在 0.1.0 而其余三处已 0.3.3，
   导致 `ginno --version`、`/api/health`、**A2A Agent Card**、以及
   **`ginno_user_agent()`（网关按 UA 识别 Ginno 流量）** 全部自报 0.1.0。
   现 runtime 内所有版本展示统一 import `_version.__version__`，前端由
   `next.config.mjs` 从 package.json 构建期注入；
   `tests/unit/test_version_single_source.py` 在 CI 钉住一致性，漏 bump 会红。
2. CHANGELOG.md：Unreleased → 新版本；commit `release: vX.Y.Z`；`git tag` + push。
3. `.github/workflows/release.yml` 自动：macOS runner `make app`（ad-hoc 签名）→
   `make smoke` 闸门 → dmg 附到 GitHub Release。首跑约 12 分钟。
4. Release notes 手工补双语版 + SHA-256（下载 dmg 后 `shasum -a 256`）。

## 测试与 CI

**CI 三栈**（`.github/workflows/ci.yml`，每次 push main / PR 全跑）：

| Job | 内容 |
|---|---|
| `runtime-tests` | `uv run --group test --extra docs pytest -q --cov`（coverage 上传 Codecov） |
| `ui-e2e` | macos：`make runtime`（web+PyInstaller bundle）→ playwright 跑 `tests/e2e/test_packaged_ui_*.py` 浏览器级用例（@ mention 菜单/ask_user/MCP trending 等）。**前端交互改动必须在 `tests/e2e/` 加 packaged UI 用例**（2026-11-12 起，@ 菜单全灭三天才被发现的教训——vitest/tsc 抓不到渲染层断裂） |
| `web` | pnpm lint + typecheck + **vitest** + next build |
| `rust` | cargo fmt --check + cargo check（desktop + mod-broker；runner macos-14） |

**本地跑测试**：

```bash
# runtime ——⚠️ 必须带 --extra docs：extractors/preview/document-tools 等 50+ 用例
# 依赖 openpyxl/pandas/docx/pptx/pypdf；bare --group test 环境会缺（CI 曾因此 48 红）
cd packages/runtime && uv run --group test --extra docs pytest -q          # 全量（xdist 后 ~3.5 分钟；每测试 120s 兜底超时）
# 本地已有 dist bundle 时 packaged UI e2e 会真跑（每个起 sidecar+chromium，
# 全量会拖到 20min+）——只想要 CI 同款集合时排除之：
#   ... --ignore-glob='tests/e2e/test_packaged_ui_*.py'
# 提速并行：加 `-n 4 --dist loadgroup`（CI 即此配置）
uv run --group test --extra docs pytest -m unit -q                        # 纯单测
# 前端
cd apps/web && pnpm test          # vitest（src/lib/*.test.ts，node 环境，无 DOM）
```

### 提交前门禁（pre-commit）

```bash
./scripts/install-hooks.sh   # clone 后跑一次；hook 指向仓库内 scripts/pre-commit.sh
```

按 staged 文件类型分发：rust 变更 → `cargo fmt --check`（desktop + mod-broker 两个
manifest）；python 变更 → `ruff check`；前端变更 → `next lint`。未涉及的栈零开销。
fmt/ruff 已在 CI 连红过五次（都是提交前没跑）——**装了它就不会再犯**。应急跳过
单次提交：`git commit --no-verify`（慎用，CI 仍会拦）。

**重构契约测试五套**（2026-11 巨型文件绞杀者重构留下的防退化网，改相关区域前先知道它们在）：

- `tests/unit/test_event_contract.py` — turn 事件 golden 逐字节快照（`api/stream/stages/` + `api/stream/ws/` 全模块扫描，路径按 `_REPO_ROOT` 绝对解析）；改 stages/ws 任何事件发射必红，预期内更新快照要走等价性论证。
- `apps/web/src/components/chat/engine/handleDispatch.test.ts` — 前端事件分发表 94 用例快照：`handlers/index.ts` 注册表增删事件类型必须同步钉住清单（93→94 就是 worker.pending_questions 那次），否则 vitest 红。
- `packages/runtime/tests/unit/test_startup_tasks.py` — 启动任务表守卫（36+ 任务，name/阶段/depends_on/timeout_s 全登记）。
- 等价性/地图文档：`api/stream/stages/EQUIVALENCE.md`（四阶段引擎与 legacy 等价性论证）+ `apps/web/…/handlers/CODEMAP.md`（前端 handlers 注册表地图）。
- OpenAPI 快照 — REST 面变更即红，提醒显式更新。

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

## 特性分支 PR 自动化（CI 绿 → 自动合并 main）

标准交付流（2026-11 实战沉淀，agent 协作纪律）：

1. **分支**：`git checkout -b <type>/<slug>`（fix/feat/refactor），一分支一主题；
   commit message 中文规范：现象 → 根因 → 修复 → 验证（跑了哪些测试）。
2. **提交前最小验证集**（按改动栈，不跑全量）：runtime → 相关单测文件 +
   `ruff check`；web → `pnpm exec tsc --noEmit` + `pnpm exec vitest run`；
   只有都绿才推。长中文 commit message 直接 `-m` 可能被 agent shell 参数层
   吞——写进文件用 `git commit -F <file>`。
3. **开 PR**：`gh pr create -t "<type>: <一句话>" --body-file <file> -b main -H <branch>`，
   body 四段：现象 / 根因 / 修复 / 验证。
4. **挂 watch shell 自动合并**（后台跑，不占会话；CI rust job ~9 分钟，
   轮询窗口必须 ≥ 25 轮×60s——10 轮等不到绿的教训见 PR #41）：

   ```bash
   sleep 300
   for i in $(seq 1 25); do
     OUT=$(gh pr checks <N> 2>&1)
     if ! echo "$OUT" | grep -qE "pending|in progress|queued"; then
       if echo "$OUT" | grep -q fail; then echo CI_RED; break; fi
       S=$(gh pr view <N> --json state -q .state)
       if [ "$S" = "OPEN" ]; then gh pr merge <N> --rebase --delete-branch && break; fi
       [ "$S" = "MERGED" ] && break
     fi
     sleep 60
   done
   git checkout -q main && git pull -q --ff-only
   ```

   **判绿一律用 `gh pr checks` 原生命令的三态**（pending / fail / green），
   不要手搓 grep 计数——`grep -oc` 数的是行数不是匹配数、大小写敏感，
   PR #39/#40 两次 watch 空转翻车均源于此。

   **但 `gh pr checks` / `gh pr create` 走 GraphQL，本机该端点经常连不上**
   （TLS handshake timeout / connection reset，2026-11-15 连续 5 次全败）。
   退路是 REST，且**pending 与 fail 计数必须在 jq 里算，绝不要交给 grep**：
   PR #143 用 `grep -c '|queued||\||in_progress||'` 判 pending，BSD grep 不认
   `\|` 交替，整个模式退化成永不匹配的字面量 → pending 恒为 0 →
   `runtime-tests` 还在 in_progress 就执行了合并（侥幸该 job 随后 success，
   main 未受损，但这是第三次同一类翻车）。

   ```bash
   SHA=$(git rev-parse HEAD)
   API="repos/robscc/ginno/commits/$SHA/check-runs"
   # ① 先确认 check 已注册：**列表为空时 pending 也是 0**，不校验就会假绿
   gh api "$API" --jq '.total_count'          # 本仓库应为 5，0 就是还没注册，继续等
   # ② pending 数（在 jq 里算）
   gh api "$API" --jq '[.check_runs[]|select(.status!="completed")]|length'
   # ③ fail 数：completed 且 conclusion 不在白名单（skipped/neutral 不算失败）
   gh api "$API" --jq '[.check_runs[]|select(.status=="completed"
        and .conclusion!="success" and .conclusion!="skipped"
        and .conclusion!="neutral")]|length'
   # ④ 建 PR / 合并都走 REST，绕开 GraphQL
   gh api repos/robscc/ginno/pulls -X POST -f title=… -F body=@body.md \
        -f head=<branch> -f base=main --jq .html_url
   gh api repos/robscc/ginno/pulls/<N>/merge -X PUT -f merge_method=rebase
   ```

   **判绿 = `total_count == 5` ∧ pending == 0 ∧ fail == 0**，三个条件缺一不可。
   合并后仍要回查一次 check 全清单确认（#143 就是提前合并、事后才补验）。

   网络抖动是常态而非异常：每个 `gh` 调用都要包重试（3-6 次、递增退避），
   单次失败**不等于** CI 红，也**不等于**可以跳过判绿直接合并。
5. **合并策略 rebase-merge + 删分支**；构建生效统一走 `make app`（改 runtime
   后想不重启验证走 `make dev-swap`），不逐 PR 构建。

## 文档归档规范

- 设计/方案文档一律放进本仓库 `docs/`（如 `docs/code-panel-diff-design.md`），深度设计放 `docs/design/`；不要只留在会话或 Obsidian 里。
- 交互式 HTML 原型/mockup 参照仓库根 `mcp-server-ui-prototype.html` 先例放仓库根，或放 `design-mocks/`。
- 调研报告类产物可同时存 Obsidian `/Users/songchenshuan/Documents/Obsidian Vault/Ginno/Research/`。

## 代码结构速查

- `packages/runtime/src/ginno_runtime/` — Python 运行时全部源码
  - `api/` — 按域拆分的 router（sessions/stream/workflows/files/knowledge/config/
    todos/usage/memory/teams/tasks/plans/schedule/connectors/…，31 文件 ~275 端点）；
    `api/stream/` 是 turn 执行/WS 流的**包**（`resume.py` 含 interrupt 公共恢复入口
    `resume_question`——WS 直答/steer 即答/relay_answer 三路同源）；`stages/` 是
    staged 四阶段引擎（permission/toolloop/streamio/finalize，唯一引擎，等价性
    论证见 stages/EQUIVALENCE.md）；`ws/` 是 WS 通道模块包（deliver/registry/
    relay/tools/watch）
  - `worker_bus/` — 子代理/worker 跨会话派活协议：`registry.py` 花名册、
    `watch.py` 订阅/digest、`relay.py` worker ask_user 中断透传（qid 一次性凭证 +
    已答墓碑 + `relay_answer` 工具 + 订阅方条件注入，默认权限 allow——权限迁移在
    `permission/policy.py` ensure_relay_permissions）
  - `api/tasks_overview.py` — 六源运行中任务聚合（turns/scheduled/subagents/
    runs/goals/delegations → GET /api/tasks/overview + stop 门面）
  - `startup_tasks.py` — 启动任务声明表（name/阶段/depends_on/timeout_s/
    fail_policy，36+ 任务）
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

<!-- rtk-instructions v2 -->
# Command output

Command output here is condensed to save tokens, keeping every signal and
dropping costly noise. Treat it as the complete result: run commands
normally, and batch related commands into one call to avoid extra turns.
Truncated results state their recovery path in their own output. Re-run a
command as `rtk bypass <cmd>` only when its result is unusable: empty when
output was clearly expected, contradicting its exit code, or garbled.
<!-- /rtk-instructions -->