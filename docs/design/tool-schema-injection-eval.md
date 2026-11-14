# 工具 schema 按需注入评估报告

> 日期：2026-11-14 · 离线实测（零真实 LLM 调用）。复跑：
> `uv run --directory packages/runtime python scripts/schema_overhead_measure.py`
> 明细：`/tmp/schema_overhead.json`。关联：Code Mode 真实对比
> `code-mode-real-bench-report.md`（本报告是其「更确定优化方向」的后续评估）。

## 1. 固定开销实测（每轮全量重放）

`build_all_tools(workspace)` → 52 工具 → `convert_to_openai_tool` 真实下发
schema → tiktoken cl100k_base：

| 功能类 | 工具数 | tokens | 占比 |
|---|---:|---:|---:|
| browser | 16 | 2888 | 29.4% |
| ungrouped（bg_run/goal/mcp/ask_user 等） | 11 | 1696 | 17.3% |
| file_shell（read/write/edit/glob/grep/bash） | 6 | 1525 | 15.5% |
| workflow | 8 | 1094 | 11.1% |
| skill | 4 | 920 | 9.4% |
| artifact_render | 4 | 882 | 9.0% |
| subagent_worker | 1 | 505 | 5.1% |
| web | 2 | 319 | 3.2% |
| **合计** | **52** | **9829** | 100% |

单工具大头：browser_computer 745、bg_run 583、delegate_agent 505、
install_skills 410、render_widget 363、grep_files 356、bash 352。

**口径说明**：此为 workspace-only 工具集（52 个）。真实会话再叠加
subagent 全家（spawn/plan/worker 等，需 project_slug/session_id）、MCP 工具、
team 工具——生产会话 tools schema 实测约 **12~15k tokens/轮**（与 Code Mode
基准首 turn input≈15k 吻合）。**每轮全量重放**：10 轮会话 ≈ 100~150k 累计
schema tokens。

## 2. 决定性变量：Ginno 已对 Anthropic 做 prompt caching

`graph_models.py:87-113`：Anthropic 路径已给 **system + tools 块打
`cache_control: ephemeral` 断点**（注释原话「gateway cached just tools+system
~30k」），并在最后两条 content 消息加第二断点延长缓存前缀。受
`settings.context.cache_control`（默认 true）控制。

**含义**：
- **Anthropic 模型**：tools schema 走 **cache-read（≈10% 输入价）**，不是全价。
  按需注入省不了多少成本，反而**每轮变动工具集会打断缓存前缀 → cache miss →
  更贵**。对 Anthropic，按需注入是**负收益**。
- **OpenAI 兼容（magpie/qwen、zhipu 等）**：代码未显式打 cache_control，依赖
  网关自动前缀缓存（provider 相关，**magpie 是否缓存未验证**）。若未缓存，
  ~9.8k/轮为全价 → 按需注入最多省 64%（见 §3）。

## 3. 按需注入方案草图（若要做）

- **触发条件**（三选一，复杂度递增）：
  1. 静态 per-agent 配置：agent 声明所需工具组（确定性、无瞎调风险、缓存稳定）。
  2. 意图分类：用户消息关键词/小模型判定注入哪些组（如「浏览/打开网页」→browser）。
  3. 懒注入：先给核心组，模型试图调未注入工具时拦截→注入该组→重试。
- **缓存策略**：工具集必须**按 agent 稳定**（同一 agent 每轮同一工具集），否则
  打断 Anthropic 缓存前缀，得不偿失。
- **瞎调风险（模型看不到未注入 schema）**：
  - 风险 a：不知道工具存在 → 能力缺失；b：凭训练记忆幻觉调用不存在的工具。
  - 缓解：常驻一份**廉价工具索引**（仅 name+一行 desc，~1-2k）让模型知道有
    什么；全 schema 按需展开；拦截瞎调→注入→重试（优雅降级）。

## 4. 结论：**不做（通用动态按需注入）**，改做窄口径静态裁剪

**不做通用按需注入的理由**：
1. Anthropic 已缓存 → 无成本收益 + 打断缓存反增成本（§2）。
2. 瞎调风险 + 工具索引 + 拦截重试的复杂度，对冲掉 OpenAI 兼容侧的收益。
3. Code Mode 教训（真实数据打脸 fake 基准）在前——动态编排类优化 ROI 易高估。

**建议的窄口径替代（确定性、低风险、缓存友好）**：
- **静态 per-agent 工具组裁剪**：按 agent 类型固定工具集（如纯聊天 agent 不
  注入 browser/workflow/subagent 组）。工具集 per-agent 稳定 → 不打断缓存。
  最大头 browser 组（2888 tokens, 29.4%）对多数非浏览器会话是纯浪费。
- **情境门控**：无浏览器 tab 连接时不注入 browser 组；非 workflow 上下文不
  注入 workflow 组。确定性条件，无瞎调风险。
- **先验证 magpie 缓存**：查 usage 的 `cached_input_tokens`——若 magpie 已自动
  缓存前缀，则连窄口径收益也大幅缩水，应进一步降级为「仅裁剪明显无用组」。

## 5. 工作量预估

| 方案 | 工作量 | 收益 | 风险 |
|---|---|---|---|
| 通用动态按需注入 | 大（3-5 天：索引+拦截重试+意图分类+缓存调优） | Anthropic 负、OpenAI 兼容最多省 64% 但被复杂度对冲 | 高（瞎调、缓存打断） |
| **静态 per-agent 裁剪**（建议） | 小-中（1-2 天：工具组注册表+agent 配置+门控接线） | 省 browser/workflow 等低频组（最多 ~6.3k/轮，64%） | 低（确定性、缓存稳定） |
| 仅情境门控 browser 组 | 小（0.5 天：无 tab 时不注入 browser） | 省 ~2.9k/轮（29.4%） | 极低 |
| 什么都不做 | 0 | 0（Anthropic 已缓存，成本可控） | 0 |

**最终建议**：优先「仅情境门控 browser 组」（0.5 天、省 29.4%、零风险）作为
第一步验证收益；再评估是否扩到静态 per-agent 裁剪。**通用动态按需注入不做**。
若 magpie 验证为已缓存，则连 browser 门控的收益也主要是上下文窗口占用（非
成本），优先级进一步下调。

## 6. 诚实边界
- 单配置（workspace-only 52 工具）实测；生产会话工具更多，绝对值更高但比例近似。
- magpie/qwen 是否自动缓存前缀**未验证**（需查 usage cached_input_tokens，
  控制配额未实测）——这是 OpenAI 兼容侧收益的最大不确定项。
- token 计数用 cl100k_base 近似各 provider 真实分词；不同模型分词器有偏差。
