# Code Mode 真实 Ginno 对比报告（magpie qwen3.8-flash）

> 日期：2026-11-14 · **真实 Ginno agent + 真实 LLM**（magpie 网关 qwen-token-plan/qwen3.8-flash）。
> 前置：fake-LLM 原型报告 `code-mode-bench-report.md`（本报告**修正其乐观结论**）。
> 复跑：`uv run --directory packages/runtime python scripts/code_mode_real_bench.py`
> （大输出变体：`CM_NOISE=50 CM_CHECKLINES=800 CM_OUT=/tmp/code_mode_hard_result.json`）

## 0. 怎么跑的（满足「一定要用 ginno 跑」）

- 把原型 repl 最小接入 Ginno 工具集（`tools/code_mode_tools.py`，env `GINNO_CODE_MODE=1`
  门控，默认关、不影响生产；`build_all_tools` 挂载）。
- `ginno exec` 隔离模式（`--no-attach`，源码 in-process 真实 agent，bypass_permissions）
  跑同一任务两 case，magpie qwen3.8-flash 真实推理。
- 从 exec stream-json 解析真实 usage：往返=assistant 事件数、累计输入/输出 tokens=各 turn 求和。
- 内层 repl 调用直接 invoke 真实内置工具（grep/read/edit/bash），**尚未逐个过权限**（P0 项）。

## 1. 真实数据（两任务 × 两模式）

| 任务 | 指标 | A traditional | B code-mode | 变化 |
|---|---|---:|---:|---:|
| **简单**（小输出） | LLM 往返 | 1 | 1 | 0 |
| | 工具调用 | 4 | 2 | — |
| | 累计输入 tokens | 15110 | 14945 | **−1.1%** |
| **大输出**（800 行 check + 50 干扰文件） | LLM 往返 | 1 | 1 | 0 |
| | 工具调用 | 4 | 3 | — |
| | 累计输入 tokens | 20133 | 16423 | **−18.4%** |
| | 累计输出 tokens | 233 | 197 | −15.5% |

A 工具序列：grep_files→read_file→edit_file→bash（**一个 turn 内批量发出**）。
B 工具序列：repl×2~3（模型自行拆分，仍一个 turn）。

## 2. 关键发现（修正 fake 基准）

**① 往返优势在真实强模型上消失。** fake 基准假设「传统=6 次串行往返」，但
qwen3.8-flash **本身就会在一个 turn 里批量并行发工具调用**——传统模式也只用了
**1 次往返**。Code Mode 承诺的「N 次往返压成 1 次」**已被现代模型的并行 tool_call
能力大部分实现**，Code Mode 在此之上没有额外往返收益。

**② return-only 上下文收益真实但温和、且依赖任务。** 小输出任务仅 −1.1%，大输出
（800 行）任务 −18.4%。收益来自「中间大输出留执行器、只回小摘要」，**随中间输出
体积增大而增大**，但本任务量级下是个位数到十几百分比。

**③ 上下文大头是工具定义（~15k 固定），Code Mode 不动它。** 53 个工具的 schema
注入是两模式相同的固定开销，占了累计输入的绝大部分——这是 Code Mode 无法压缩的。

**④ fake 基准为何高估**：它手写了「理想编排代码 + 串行往返 + 大输出逐 turn 回灌」，
三个假设在真实强模型 + 简单任务下都不成立。真实数据把收益从「81%/67%」打回「1~18%/0%」。

## 3. 修正后的决策建议：**C（归档）为主，保留窄口径 opt-in 可选**

fake 报告基于 81%/67% 推荐 A（P0 施工）；**真实数据不支持该力度**：
- 往返收益 ≈ 0（强模型已批量调用）；
- 上下文收益 1~18%，温和且任务依赖；
- 而 P0 成本不小：`evaluate_tool_call` 权限复用、内层 ask 的 interrupt/resume、
  沙箱、checkpoint 恢复语义——**ROI 偏低**。

**建议**：
- **主选 C**：原型 + 接入代码归档（已 env 门控默认关，零生产风险），暂不投入完整 P0。
- **可选窄口径**：若 Ginno 高频出现「超大工具输出」任务（如全仓搜索/长测试日志），
  可做一个**仅 return-only 过滤**的轻量版（不做并行/沙箱/复杂 ask），作为 opt-in
  实验特性观察真实收益，再决定是否扩大。
- **真正该优先的上下文优化**：压缩工具定义注入（~15k 固定大头）比 Code Mode 收益更确定
  ——这与 [[mksglu/context-mode]] 的工具输出沙箱化、以及 Ginno 已有的 tool_output_max_chars
  截断同向，建议优先评估「按需注入工具 schema」。

## 4. 交付物

- 最小接入：`tools/code_mode_tools.py`（env 门控）+ `graph.py` 挂载（默认关）
- 真实基准：`scripts/code_mode_real_bench.py`（可复跑，简单/大输出两变体）
- 本报告 + fake 原型报告 `code-mode-bench-report.md`
- 明细：`/tmp/code_mode_real_result.json`、`/tmp/code_mode_hard_result.json`

## 5. 诚实边界

- 单模型（qwen3.8-flash）单任务族；弱模型（不批量调用）可能展现往返收益，未测。
- 内层 repl 未过权限（bypass 隔离测试）；生产启用前必须补（P0 项）。
- 累计输入 tokens 含固定工具定义开销，纯任务上下文收益被稀释——看绝对差值更直观。
