# ginno exec — headless 合约（Multica runtime / benchmark harness 接入）

`ginno exec` 是 Ginno 的对外 headless 一次性入口：外部 harness（Multica
daemon、workbuddy-bench）以子进程拉起它，在指定工作目录完成一个任务
turn，从 stdout 解析 claude-code stream-json 兼容的 JSONL。

实现：`packages/runtime/src/ginno_runtime/cli.py`，与桌面版共享同一条
turn 执行链（`create_session → _run_stream`，ws=None），完全进程内。

## 独立安装

不需要桌面 app。仓库根目录：

```bash
# pipx
pipx install packages/runtime
# 或 uv 等价命令
uv tool install packages/runtime
```

装完 PATH 上有 `ginno` 命令：

```bash
ginno --version     # → ginno x.y.z
```

## 用法

```bash
ginno exec [--cwd DIR] [--home DIR] [--prompt-stdin] [-- <instruction>]
```

| flag | 说明 |
|---|---|
| `--cwd DIR` | 工作目录（默认 `$PWD`）；注册为 context folder 并设为 primary，工具的相对路径与 bash cwd 都解析到这里 |
| `--home DIR` | GINNO_HOME 覆盖（默认每次一个临时目录，见下文隔离性） |
| `--prompt-stdin` | 从 stdin 完整读取任务指令；**与 `-- <instruction>` 互斥**（同时给出 → usage 错误 exit 2）；两者都没有 → usage 错误 exit 2。长任务描述必用此模式——Windows 单条 argv 上限约 32K 字符 |
| `--max-turns N` | 递归步数上限（→ `GINNO_RECURSION_LIMIT`） |
| `--no-world-state` | 关掉规则/记忆等 world-state 注入层 |
| `--no-attach` | 绝不探活/连接本地 Ginno app，强制进程内隔离模式 |
| `--settle-timeout S` | 主 turn 结束后等 subagent 树/wake turn 静默的最长秒数（默认 1800） |

## 环境变量

| env | 必填 | 说明 |
|---|---|---|
| `GINNO_EXEC_BASE_URL` | 进程内模式是 | OpenAI 兼容端点（等价 claude 的 ANTHROPIC_BASE_URL） |
| `GINNO_EXEC_API_KEY` | 进程内模式是 | API key |
| `GINNO_EXEC_MODEL` | 进程内模式是 | 模型 id；同时钉死内部一切模型路由（agent 绑定/摘要/分类器都解析到它） |
| `GINNO_EXEC_MAX_OUTPUT_TOKENS` | 否 | 输出预算；默认 0 = 不传 max_tokens，由端点自决 |
| `GINNO_EXEC_TIMEOUT_S` | 否 | （provider 层超时；流级看门狗另有 `GINNO_CHUNK_TIMEOUT_S`，exec 已默认抬到 1200s） |
| `GINNO_EXEC_NO_ATTACH` | 否 | `=1` 时绝不探活 `127.0.0.1:8787`（测试断言覆盖：`tests/unit/test_cli_exec_contract.py::test_no_attach_never_probes_sidecar`） |
| `GINNO_EXEC_HOME` | 否 | 等价 `--home`；`--home` 优先 |

`GINNO_FAKE_LLM=1`（+ `GINNO_FAKE_LLM_SCRIPTS=<json>`）切换到脚本化假模型，
整条链路零网络——全部测试都跑在这条缝上。

## 两种运行模式（自动选择）

- **远端（attach）**：本地有 Ginno app 在跑（`127.0.0.1:8787` 探活成功）就连它，
  走 REST + WS——用 app 现成的 provider/会话状态（含个人记忆）。此模式下
  `GINNO_EXEC_*` 三件套可缺省。
- **进程内（隔离）**：探不到 sidecar（或 `--no-attach` / `GINNO_EXEC_NO_ATTACH=1`）
  时的默认。隔离 home、env 合成 provider、无个人记忆。benchmark / Multica
  runtime 走这条路，env 三件套必填（缺失 → exit 2）。

## stdout 帧目录（claude-code stream-json 兼容 JSONL）

一行一帧，立即 flush：

| type | subtype | 何时出现 |
|---|---|---|
| `system` | `init` | 会话就绪；带 `session_id` / `model` / `cwd`（远端模式另有 `attached` / `port`） |
| `assistant` | — | 每个 turn 一条：`message.content` = text 块 + tool_use 块（**input 是真实参数**）+ 本 turn usage |
| `user` | — | assistant 帧之后（仅有工具调用的 turn）：`message.content` = `tool_result` 块，按 `tool_use_id` 对应 |
| `system` | `subagent_spawned` | 内部 spawn 事件；带 `session_id` / `goal` |
| `system` | `subagent_status` | 子代理状态迁移；带 `session_id` / `status` / `title` |
| `system` | `error` | 引擎级 turn 错误（engine `error` 事件直译） |
| `system` | `settle_timeout` / `remote_timeout` / `remote_error` | 收尾阶段的部分状态，附诊断字段 |
| `result` | `success` / `error_deterministic` | **终帧，必有**。字段：`is_error` / `duration_ms` / `duration_api_ms` / `num_turns` / `session_id` / `text` / `preview`（200 字符）/ `error` / `usage`（全 subagent 树汇总） |

tool_result 的 `content` 是引擎 WS 帧里的工具输出文本，超过 4000 字符会被
引擎截断（`TOOL_OUTPUT_WS_LIMIT`，截断尾巴标注总长度）。未跑完/被 permission
拒绝的工具调用没有带 id 的 tool.end，转录层**不虚构**对应 tool_result。

崩溃安全：`_run` / `_run_remote` 抛出的任何未捕获异常也 emit 一条
`{"type":"result","subtype":"error_deterministic","is_error":true,"error":...}`
终帧再以非零码退出——harness 永远拿得到终态，不必靠超时兜底。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | turn 成功（`result.is_error=false`） |
| 1 | turn 失败：引擎报错（session `last_error`）、未捕获异常崩溃、Ctrl-C（130：KeyboardInterrupt） |
| 2 | usage/配置错误：缺 `GINNO_EXEC_*` 三件套（进程内模式）、`--prompt-stdin` 与 `--` 冲突、缺任务指令、argparse 用法错误 |

## 隔离性与 `--home` 持久化

默认每次 exec 取一个**临时 GINNO_HOME**——个人记忆/桌面状态天然不加载，
benchmark 的 diff 捕获不会把 home 当改动。显式 `--home DIR`（或
`GINNO_EXEC_HOME`）时全部状态落在该目录：

- `settings.json` — 合成 settings（唯一 exec provider）
- `projects/<slug>/sessions/<session_id>.jsonl` — LangGraph checkpointer（追加式日志；
  旧版 `.json` 仍可读）
- `usage/requests-YYYY-MM-DD.jsonl` — token 账本

同一 `--home` 二次 exec 后，上一会话仍可按 `session_id` 从 meta 索引找回
（v2 `--resume` 的地基）。测试：`tests/e2e/test_cli_exec.py::test_exec_home_persistence`。

`--cwd` 注册进 context folder 库按 resolved path 幂等：重复 exec 同一目录
不膨胀 folder 库（这一点与 home 隔离无关，保持既有行为）。

## 安全现状（headless 语义）

- **`bypass_permissions=True`**：headless 无人值守，permission park 没人应答，
  必须放行所有工具调用——即 exec 模式下**所有工具都不经确认直接执行**。
  只能把 `--cwd` 指向可信目录；不要在多租户/不受信工作区上跑。
- **`external_agents_enabled=False`**：不委托外部 CLI（claude-code/codex），
  只跑 Ginno 内置 subagent——headless 度量的是 Ginno 自己，也避免把任务
  二次转发给本机其它 agent。
- **`ask_user` 关闭**：set_interactive(False)，模型必须自决，不会挂起等输入。

## v2 预留（本期未实现）

- `--resume <session_id>`：从 `--home` 加载既有会话线程继续执行；帧格式不变，
  `result.session_id` 保持不变（Multica 对任务的追加评论传这个）。
- `ginno exec --list-models`：查配置端点 `/v1/models`，打印 JSON 数组。
- ACP（Agent Client Protocol）stdio adapter：仅在需要交互式多轮时再做。
