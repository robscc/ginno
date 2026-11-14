# Code Mode 原型收益实测报告（选项 B 决策依据）

> 日期：2026-11-14 · 全程 fake-LLM，**零真实 LLM 调用**（GLM 周窗仅 ~1.4k credit，不烧配额）。
> 关联：`docs/design/code-mode-design.md` · 原型 `packages/runtime/src/ginno_runtime/code_mode/`
> 复跑：`uv run --directory packages/runtime python scripts/code_mode_bench.py`

## 1. 实测数据（cl100k_base 计数）

同一 5 步工具链「搜索→读文件→改 maxAttempts 3→5→跑测试→复核」，fixture 为
1 主文件 + 12 干扰文件（search 返回大量行，贴近真实搜索量级）。

| 指标 | traditional 逐调用 | code-mode return-only | 变化 |
|---|---:|---:|---:|
| **上下文 tokens** | 1445 | 271 | **−81.2%** |
| **LLM 往返次数** | 6 | 2 | **−66.7%** |
| 消息条数 | 13 | 5 | −61.5% |
| 内层工具调用（留执行器） | 5（全回灌） | 5（不回灌） | — |
| 端到端墙钟 (s)* | 0.0009 | 0.0240 | +0.023 |

code-mode return 值（进上下文的全部内容）：
`{"edited":["src/retry.py"],"ok":true,"tests_passed":true,"attempts_now":5,"verify_hits":13}`

\* **墙钟口径诚实声明**：fake-LLM 无真实推理等待，两模式墙钟≈工具执行耗时；
code-mode 额外含 RPC/子进程开销，故 fake 模式下**反而略慢**。真实延迟收益
∝ 往返节省 × 单次 LLM 延迟（TTFT+生成），本脚本**不虚构该常数**——往返次数
（6→2）才是延迟与成本的决定性代理指标。

## 2. 收益来源拆解

- **上下文 −81.2%**：traditional 把 5 个工具的全量输出（search 13+ 行 ×2、
  read 全文、pytest 40 行）全部回灌；code-mode 中间结果留执行器，仅 1 个小
  JSON return 进上下文。这是 return-only 的直接效果，与 [[mksglu/context-mode]]
  的输出压缩同向但更彻底（它压单次体积，Code Mode 干脆不回灌中间态）。
- **往返 −66.7%**：6 次（5 tool_call + 1 汇总）压成 2 次（写代码 + 读 return）。
  真实场景每次往返含完整上下文重读 + 生成，N 步任务的延迟/成本随步数线性涨，
  Code Mode 把它压成常数。

## 3. 原型验证了什么 / 没验证什么

**已验证（机制正确）**：
- stdio JSON-RPC 执行器骨架可跑：`tool()` 代理回宿主、return-only、内层调用
  计数、permission deny 在代码内可 catch、未知工具报错、多调用链（5 单测绿）。
- 收益量级在确定性场景下显著且可复跑。

**未验证（属选项 A/P0 范围，本任务不超范围）**：
- 真实工具 + `evaluate_tool_call()` 权限复用（原型用确定性工具集）。
- 内层 ask 的 interrupt/resume（原型简化为 deny）。
- 真实 LLM 写编排代码的质量与收益（fake-LLM 的代码是手写的理想代码）。
- 沙箱、并行原语、checkpoint 恢复语义。

## 4. 决策建议：**A（进入 P0 施工）**

阈值（上下文节省 ≥50% 且 往返节省 ≥60%）**双双达标**（81.2% / 66.7%），
且原型机制验证通过。建议进入选项 A，按设计文档 §7 P0 清单施工：

1. `evaluate_tool_call()` 抽取 + 图路径逐字节回归（唯一重构点）。
2. `repl` 工具注册 + 接真实工具 + 权限复用。
3. 内层 ask 先简化「转外层一次确认」，完整 interrupt 留 P1。
4. return-only 上下文 + 内层事件流式渲染（复用 get_stream_writer）。
5. settings `code_mode.enabled` 默认关（opt-in）。

**风险提示（决策时须知）**：
- 收益依赖「多调用 + 中间结果大」的任务；单调用任务 repl 反增一层开销。
  P0 应在 bench 链路用**真实 LLM** 复测界定适用场景（明早配额重置后可跑，
  脚本骨架已留 fake_llm_* 替换点）。
- 宣传口径只能「更省、权限更细」，不能「更安全」（任意代码信任=bash，
  除非 P1 上沙箱）。
- 弱模型写编排代码质量差，需按模型能力 gating。

## 5. 交付物

- 原型：`code_mode/{__init__,executor,host}.py`（执行器 + RPC 宿主 + repl 骨架）
- 基准：`scripts/code_mode_bench.py`（可复跑，零真实调用）
- 测试：`tests/unit/test_code_mode_prototype.py`（5 绿）
- 本报告 + 设计文档 `code-mode-design.md`
