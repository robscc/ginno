# Code Mode（工具调用代码化）Ginno 切入方案

> 状态：设计稿（未实施）。对标：Claude Code 2.1.323 实验特性 Code Mode
> （`CLAUDE_CODE_EXPERIMENTAL_CODE_MODE=1`，官方文档 code.claude.com/docs/en/code-mode）。
> 关联：`docs/runtime-hot-swap-design.md`、mods 兼容层（broker）、workflow `python` 节点。

## 0. 背景：Code Mode 解决什么

传统 agent 的工具调用是「LLM 往返循环」：模型发一个 tool_call → 等结果 →
读结果 → 再决策下一个。N 步任务 = N 次完整 LLM 往返，每次都重读上下文、
重新生成。四个税：

1. **往返税**：延迟与 token 成本随步数线性涨。
2. **上下文污染**：`rg` 搜出 500 行全量回灌模型，模型只为挑 1 行。
3. **串行税**：独立命令只能排队往返，不能并行。
4. **机械组合税**：条件/循环/管道每步都要模型「想」一次。

Code Mode 的本质：**把编排循环从 LLM 搬进一个确定性执行器**。模型写一段
代码，执行器跑它；代码里的每次工具调用经宿主代理回权限/工具层；中间结果
留在执行器内，只有最终 `return` 值回模型。一步完成多调用、可并行、可过滤。

官方示例（改重试上限）一段代码完成 搜索→取文件→edit→跑测试→只返回路径
与测试输出；传统模式这是 4+ 次往返。

## 1. 原理拆解（三个部件）

```
LLM ──写代码──▶ [执行器 sandbox] ──tool(name,args)──▶ [宿主代理]
                     ▲                                    │
                     └────────── 结果（留在执行器）◀──────┘
                     │
                     └── 只有最终 return 值 ──▶ LLM 上下文
```

- **执行器**：跑 LLM 写的代码（Claude Code 用 JS；Ginno 贴合选 Python，见 §3）。
- **宿主代理**：代码里每个 `tool()` 调用 RPC 回宿主，走**与正常 tool_call
  完全相同**的权限+执行路径。执行器自身不直接持有任何工具能力。
- **结果过滤**：中间结果在执行器内流转（`files.split("\n")[0]`），不回模型；
  只有显式 return 进上下文。

关键安全语义（官方）：**每个内层工具调用仍过 deny/ask 规则 + PreToolUse
hooks**；但 auto mode 的分类器审的是**整段代码**而非每个调用，且看不到
运行时计算值（如搜索结果里的路径）——关键管控仍须落在 rules/hooks。

## 2. Ginno 架构映射（为什么贴合）

| Code Mode 部件 | Ginno 既有机制 | 贴合度 |
|---|---|---|
| 逐内层调用过权限 | `permission_node_factory`（graph.py:421）本就是 `for tc in pending_tool_calls` 逐个过 `tool_allowed` + deny/ask + PreToolUse hooks | ★★★ 直接复用，只需抽出可复用判定函数 |
| 工具代理回宿主 | mods broker（Unix socket + `GINNO_MOD_BROKER_TOKEN`）已是「工具调用代理」先例 | ★★★ 同构，新增一条 stdio JSON-RPC 通道即可 |
| 执行器跑任意代码 | `bash` 工具本就是任意代码执行（builtin.py，注释自认 "Not a sandbox"） | ★★★ **不引入新能力类别**，信任级别=bash |
| 中间结果不进上下文 | `tool_output_max_chars`（默认 20000）截断 + bg_run spool offset/limit 取回 | ★★★ 同思路，return-only 是更彻底的版本 |
| 长代码不触发看门狗 | `tools_node` 的 `TOOL_HEARTBEAT_S` 心跳（graph.py:979）已防 stall 误杀 | ★★★ repl 作为一个 tool 执行，心跳天然覆盖 |
| 内层调用透明可见 | `get_stream_writer` custom 事件（`ginno_tool_heartbeat` 先例） | ★★ 内层 tool 事件可流式渲染给 UI 而不进 LLM 上下文 |
| ask 中断/恢复 | ask_user/resume + `api/stream/resume.py` interrupt 公共入口 | ★★ 内层 ask 可复用 interrupt 机制浮出 UI |

**核心论点**：Code Mode 相对 bash 是**严格更优**——
- 权限粒度：bash 是一个粗粒度 `Bash(...)` 闸；Code Mode 内层每个结构化
  调用 individually 过权限，**更细**。
- 上下文经济：bash 全量 stdout 回灌（截断 20k）；Code Mode 只回 return 值。
- 所以这不是「放开任意代码」的新风险，而是「把已有的任意代码能力编排得
  更省、权限更细」。安全 posture 与 bash 对齐即可起步。

**反例对照（务必区分）**：workflow 的 `python` 节点是**白名单确定性入口**
（`workflows/scripts/__init__.py` `ENTRY_REGISTRY`，`fn(args)->dict`，无
LLM、无任意代码）——哲学与 Code Mode **相反**。Code Mode 是「LLM 写任意
编排代码」，不能塞进 ENTRY_REGISTRY；它是 chat turn 内的新工具，不是
workflow 节点。两者并存不冲突。

## 3. 切入方案：Shape A —— `repl` 工具（P0 推荐）

两种集成形态：

- **Shape A（REPL 工具）**：新增一个 `repl`（或 `code_mode`）工具。LLM 写
  代码作为该工具的参数；工具在子进程执行器里跑，内层 `tool()` RPC 回宿主。
  **从图视角这是一次 tool_call**——干净落在 agent→permission→tools 拓扑，
  不动图结构。
- **Shape B（图级编排节点）**：agent 发「代码计划」，新节点执行并把内层
  调用打回 permission/tools 循环。侵入图拓扑，P2 再议。

**P0 选 Shape A**。执行器设计：

```
sidecar (Python)
  └─ repl 工具被调用，args = {code: "...", timeout_s: 600}
       └─ spawn 子进程 executor（Python，stdio JSON-RPC）
            注入 builtins: tool(name, args) / parallel([...]) / log(...)
            执行 LLM 代码
            每个 tool() → JSON-RPC request → sidecar
                 sidecar: evaluate_tool_call() → 权限判定
                          allow → 执行真实工具 → result 回子进程
                          ask   → interrupt 浮出 UI，子进程阻塞等 resume
                          deny  → 回错误，代码可 catch
            代码 return 值 → JSON-RPC final → repl 工具返回给 LLM
```

- **语言选 Python**：sidecar 是 Python、工具是 Python StructuredTool
  （`build_builtin_tools` builtin.py:827），LLM 写 Python 编排最贴合。
  JS 执行器（对标 Claude Code）留 P2 可选。
- **通道选 stdio JSON-RPC**：子进程由 sidecar spawn，stdin/stdout 管道天然，
  无需 socket 布置（broker 的 socket 模式留给跨进程 mod 场景）。
- **执行器无直接工具能力**：只持有 RPC client。它仍能 `import subprocess`
  跑任意命令——**这与 bash 信任级别相同**，P0 接受并文档化；P1 可选沙箱
  （macOS `sandbox-exec`/seatbelt 或容器）收紧。

## 4. 权限模型（关键难点）

现状 `permission_node_factory` 的判定逻辑内联在图节点循环里。切入需要：

1. **抽出 `evaluate_tool_call(agent, name, args, hooks, policy) -> Decision`**
   纯函数（allow/deny/ask + 理由），图节点与 repl RPC handler 共用。这是
   本方案唯一的「重构既有代码」点，须保证图路径行为逐字节不变（回归测试）。
2. **内层 ask 的中断/恢复**：RPC handler 遇 ask → 走既有 interrupt 机制
   （`resume.py`）浮出 UI；子进程阻塞等待。resume 后把决定回给子进程继续。
   复用 ask_user 的 pending_question 基建。
3. **auto/特权模式语义**：与官方一致——特权模式内层调用仍过 PreToolUse
   hooks（用户规则权威）；分类器若引入，审整段代码而非每调用，且看不到
   运行时值。**关键管控必须落 rules/hooks**，文档明示。
4. **审计**：内层每个调用记 usage/日志（与正常 tool_call 同口径），repl
   外层记一条「code 执行」摘要。

## 5. 上下文经济

- repl 工具返回 = 代码最终 return 值，经 `tool_output_max_chars` 截断。
- 中间 tool 结果**默认不进 LLM 上下文**；可选 `log()` 显式回传少量摘要。
- 内层 tool 调用经 `get_stream_writer` custom 事件流式渲染给 UI（用户看到
  edit diff/命令，如同 Claude Code「见调用不见代码」），**但不进模型上下文**。
- 代码本身存 transcript（Ctrl+O 式展开），不进每轮 prompt。

## 6. 与既有机制的关系

| 机制 | 关系 |
|---|---|
| `bash` | Code Mode 是其「结构化+细权限+省上下文」升级；bash 保留（简单单命令仍用它） |
| workflow `python` 节点 | 哲学相反（白名单 vs 任意编排），并存不冲突，勿混用 |
| mods broker | 同构代理先例；repl 用 stdio 轻量版，broker socket 留给 mod |
| bg_run spool | 同「不灌上下文」思路；repl return-only 更彻底 |
| lastwords WAL | repl 是一个 tool 边界；内层调用可逐个 `note_tool_boundary` 细化快照 |
| tool 心跳 | repl 长执行天然被 `TOOL_HEARTBEAT_S` 覆盖，不触发 stall 误杀 |

## 7. 分期

**P0（最小可用，验证收益）**
- `evaluate_tool_call` 抽取 + 图路径回归。
- `repl` 工具 + Python 子进程执行器 + stdio JSON-RPC + `tool()` 代理。
- 内层 allow/deny 走通；ask 先简化为「内层 ask 一律转外层一次确认」。
- return-only 上下文 + 内层事件流式渲染。
- 开关：settings `code_mode.enabled`（默认关，opt-in，对标官方）。
- 测试：权限复用回归 + repl 端到端（fake LLM 写代码→多调用→return）。

**P1（生产可用）**
- 内层 ask 完整 interrupt/resume（复用 ask_user 基建）。
- `parallel()` 并行原语；超时/资源限制（子进程 kill）。
- 可选沙箱（seatbelt/容器）收紧任意 subprocess。
- 真实模型收益基准：同任务 repl vs 传统往返的 token/延迟对比（bench 链路）。

**P2（增强）**
- JS 执行器（对标 Claude Code）；Shape B 图级编排。
- 代码分类器（auto mode 审整段代码）。
- 与 Instincts 联动：高频 repl 编排模式沉淀为 skill。

## 8. 风险与开放问题

- **任意代码信任**：P0 与 bash 同级；若产品要「比 bash 更安全」必须上沙箱
  （P1），否则宣传口径只能是「更省、权限更细」，不能是「更安全」。
- **ask 嵌套复杂度**：内层 ask 浮出 UI 再 resume 回子进程，是本方案最重的
  工程点；P0 先简化规避。
- **checkpoint 语义**：repl 内层调用不在图 messages 里，恢复时如何重放？
  P0 约定「repl 不可中途恢复，失败整段重跑」；P1 再议内层 WAL。
- **模型能力门槛**：官方仅 Opus/Sonnet/Haiku 5.5+ 支持；Ginno 接多模型，
  弱模型写编排代码质量差，需按模型能力 gating（settings 或模型白名单）。
- **收益不确定**：往返税节省依赖任务「多调用且中间结果大」；单调用任务
  repl 反而多一层开销。需 bench 实测界定适用场景，避免过度设计。

## 9. 测试清单

- `evaluate_tool_call` 抽取后图路径行为逐字节回归（deny/ask/hooks/特权）。
- repl 端到端：fake LLM 写「搜索→过滤→edit→return」代码，断言只有 return
  进上下文、内层调用各过权限。
- 内层 deny：代码 catch 错误继续；内层 ask：P0 转外层确认。
- 超时：子进程超时被 kill，repl 返回错误不拖垮 turn。
- 心跳：长 repl 执行不触发 CHUNK_TIMEOUT 看门狗。
- 上下文：中间大输出不进 messages（断言 token 数）。
