# 「总结成 Workflow」质量提升方案（成功率 × 准确率）

> 长期目标：把 session → workflow 做成一个可度量、可归因、可持续优化的闭环。
> 两个北极星指标：
> - **成功率** = 产出完整可运行工作流的次数 / 总结尝试次数
> - **准确率** = 成功样本中完整复现原对话流程的比例

---

## 1. 核心观点：这是一个漏斗，不是一个二值结果

「总结成功」至少分四级，每级的失败模式和所需数据完全不同——度量和问题定位
都必须按漏斗分层，否则「成功率低」永远是黑盒：

```
L0 触发总结
 └─ L1 生成成功：返回合法 DSL（parse + validate 通过）
     └─ L2 被采用：用户在弹窗中创建（而不是关闭/大改后丢弃）
         └─ L3 可运行：创建后的首次运行跑完（done，无失败步骤）
             └─ L4 保真：运行结果复现了原对话的意图与产物 ← 准确率
```

- **成功率**（用户口径）= L3 / L0。工程上拆成 L1/L2/L3 三段分别归因。
- **准确率** = L4 / L3。L4 无法全自动判定，靠「用户反馈 + LLM 评审」双通道（见 §6）。

关键推论：**记录系统必须在 L0 就建档**，逐级回填结果，而不是只在失败时记日志。

---

## 2. 记录方案（定位问题的地基）

### 2.1 存储布局

每次总结合成一个 case 目录（本地，随 ~/.ginno）：

```
~/.ginno/synthesis/
  20260809-221400-7700a4a8/          # 时间戳 + session 前 8 位
    input.json                        # 输入快照（可离线重放）
    attempts.jsonl                    # 每一轮 LLM 尝试（含失败轮）
    output.json                       # 最终产物 + 元信息
    outcome.json                      # 后续回填：采用/运行/反馈
```

### 2.2 各文件 schema

**input.json**（重放所需的全部输入）：
```json
{
  "synthesis_id": "20260809-221400-7700a4a8",
  "session_id": "7700a4a8...",
  "ts": 1786284880,
  "prompt_version": "synth-3",        // 提示词版本号，改动即 bump
  "engine_version": "0.1.0",
  "provider": "anthropic", "model": "...",
  "last_n": null,
  "session_stats": {"messages": 131, "tool_calls": 42, "turns": 18},
  "trace": "<实际送入 LLM 的 trace 全文>"
}
```

**attempts.jsonl**（重试循环每轮一行）：
```json
{"attempt": 1, "latency_ms": 8200, "raw": "<模型原始输出>",
 "parse": "ok|not_json|json_not_object", "validate_errors": ["..."],
 "hint_fed_back": "<给下一轮的纠错提示，末轮为 null>"}
```

**output.json**：
```json
{"status": "ok|failed", "fail_stage": "format|schema",
 "dsl": { ... },                       // normalize 后的最终 DSL（失败时为最后一次产出）
 "total_latency_ms": 21000, "attempts_used": 2}
```

**outcome.json**（异步回填，缺字段 = 尚未发生）：
```json
{"created": true, "workflow_id": "97eeb92617", "created_at": ...,
 "edited": true,                        // 用户是否改动过草稿
 "edit_distance": 3,                    // 草稿 vs 实际创建的 DSL 差异数（节点增删改计数）
 "first_run": {"run_id": "0d864e3243", "status": "done", "failed_node": null},
 "user_feedback": {"verdict": "up|down", "note": "..."}}
```

### 2.3 回填链路（改动点）

| 时机 | 动作 |
|---|---|
| summarize 端点 | 建 case 目录，写 input/attempts/output |
| 弹窗创建成功 | 前端把 `synthesis_id` 传给 createWorkflow → 写入 workflow meta（`synthesized_from`）→ 回填 outcome.created |
| 弹窗关闭未创建 | 前端发轻量事件 `synthesis_discarded`（带 edit 过的草稿）→ 回填 L2 失败 |
| 该 workflow 首次 run 终态 | 后端按 `synthesized_from` 回填 first_run |
| 用户反馈（见 §4.3） | 回填 user_feedback |

所有回填 best-effort（try/except），绝不阻塞主流程。

### 2.4 隐私与治理

- 全部本地文件，不出机器；trace 本身已是会话内容的截断版。
- 保留策略：默认保留最近 200 个 case 或 90 天，超出的按时间清理（随启动做，惰性）。
- 设置项可关闭记录（默认开——这是质量闭环的数据源）。

---

## 3. 问题定位方案：失败分类学（taxonomy）

每个 case 自动打 `fail_stage` 标签，review 时按标签过滤。标签体系：

| 阶段 | 标签 | 判定依据（自动） |
|---|---|---|
| L1 格式 | `format.not_json` / `format.not_object` | parse 结果 |
| L1 结构 | `schema.<错误签名>` | validate_dsl 错误归一化（去节点名变量，聚合统计） |
| L2 采用 | `adoption.discarded` / `adoption.heavy_edit`（edit_distance > 阈值） | outcome 回填 |
| L3 运行 | `exec.<failed_node_type>.<error签名>` | 首次 run 的 error_detail |
| L4 保真 | `fidelity.user_down` / `fidelity.judge_low` | 反馈 + LLM 评审 |

**Review 工作流**（命令行起步，不急着做 UI）：

```
ginno-cli synth list --stage schema --last 7d     # 过滤视图
ginno-cli synth show <case>                        # input trace + 各轮 attempt + DSL
ginno-cli synth replay <case> [--prompt synth-4]   # 离线重放（见 §5）
ginno-cli synth stats                              # 漏斗各层计数 + 失败标签分布
```

`stats` 输出示例（这就是周会看的东西）：
```
L0→L1 生成成功 82%   top失败: schema.edge_unknown(9) format.not_json(4)
L1→L2 采用率   71%   heavy_edit 占比 18%（草稿质量代理指标）
L2→L3 首跑成功 64%   top失败: exec.step.model_error(5) exec.loop.empty(3)
L3→L4 保真     待反馈数据（当前 👍 4 / 👎 2）
```

**关键设计**：`prompt_version` 进每条记录——任何提示词/引擎改动 bump 版本，
stats 按版本对比，这就是低成本的 A/B：同一个失败标签在新版本下是否收敛。

---

## 4. 产品方案（提升各层转化）

### 4.1 L1 生成：结构先行的提示词（下一步迭代方向）
- 现提示词一次性要完整 DSL。改为两阶段：先输出「结构骨架」（节点列表+边+变量，
  无 goal 细节）→ 校验骨架 → 再补全 goal。骨架小、易校验，格式失败率预期显著下降。
  （先不做，等记录数据证明 L1 失败的主要构成再投入。）

### 4.2 L2 采用：让用户低成本校验与修正，而不是丢弃
- **对话↔步骤映射预览**：弹窗里每个节点标注「对应对话第 X–Y 轮」，准确率肉眼
  可查，错了当场改。（映射信息在生成时顺手让模型产出：`"source_turns": [3,7]`）
- **弹窗内指令精修**：加一个输入框「用一句话描述要怎么改」→ 带着原 trace +
  当前 DSL + 指令再调一次模型重生成。比打开 dev session 轻得多，直接挽救
  heavy_edit 与 discard。
- **资格预期管理**：trace 里没有工具调用的纯聊天 session，总结按钮给提示
  「该会话以讨论为主，可能总结不出可执行流程」——降低无效尝试、保护指标分母。

### 4.3 L4 保真：反馈回收（准确率的唯一可靠来源）
- summarize 创建的 workflow，首次 run 结束后（聊天 run 卡 / Workflow 面板）显示
  一次性 👍/👎 + 可选一句话，写入 outcome.user_feedback。
- 不做强制弹窗打断；👎 时附一个「复制 case 路径」方便用户报给我们。

### 4.4 模板沉淀（远期）
review 中反复出现的成功结构（如「检索→撰写→循环建 TODO」）固化为 few-shot
示例进提示词，或直接成为内置模板——golden case 反哺生成质量。

---

## 5. 离线评测（eval harness）

记录方案的红利：**input.json 可重放**，评测不再依赖真实会话现场。

1. **Golden set**：从 case 库人工挑 20-30 个代表性输入，人工标注期望结构
   （节点数/顺序/分支/变量），版本化管理。
2. **回放**：`synth replay` 对 golden set 跑当前提示词，产出两个分数：
   - 成功率代理 = validate 通过率；
   - 准确率代理 = 与标注结构的匹配分（节点序列相似度 + 边相似度 + 变量集合）。
3. **LLM-as-judge 补充**：对通过 case，用评审模型对照 trace 打分
   （覆盖度：每个对话阶段是否成节点；顺序/分支是否正确；是否有幻觉步骤），
   分数入 case，作为人工 review 的优先级排序。
4. **门禁**：提示词改版前跑一遍 golden set，成功率代理不得回退（CI 或手动）。

---

## 6. 实施分期

| 期 | 内容 | 产出 |
|---|---|---|
| P1（1 天） | 记录三件套（input/attempts/output）+ prompt_version + CLI list/show/stats | 所有总结开始积累数据 |
| P2（1 天） | outcome 回填链路（创建/丢弃/首跑结果）+ workflow meta `synthesized_from` | 漏斗 L0→L3 可度量 |
| P3（1 天） | 👍/👎 反馈回收 + LLM-judge 离线评审脚本 | L4 开始积累 |
| P4（按需） | replay + golden set + 提示词两阶段化 + 弹窗精修/映射预览 | 优化手段逐个上线验证 |

P1/P2 先行的理由：**没有数据就没有优化方向**——先让漏斗可见，再决定把力气
花在 L1（提示词/两阶段生成）还是 L2（精修交互）还是 L3（引擎可靠性，与
workflow-reliability-discussion.md 的议题联动）。

---

## 7. 与其他方案的关系

- **议题 ②（writes 硬契约 + 抽取节点）落地后**，L3 的 `exec.*` 失败应显著下降——
  stats 按 prompt/engine 版本对比即可量化验证该改造的价值。
- **议题 ③（文件 allowlist）**：L3 的「越界 tool error」类失败同理可量化。
- 记录系统本身与它们正交，可以先上。

## 8. 待定决策

| # | 决策 | 倾向 |
|---|---|---|
| 1 | CLI 还是 Settings 页做 review 入口 | CLI 先行（成本低），数据证明价值后再做 UI |
| 2 | 保留策略：200 case / 90 天 | 可配，先硬编码默认值 |
| 3 | 弹窗精修（§4.2）是否进 P2 | 建议 P4——先看 heavy_edit/discard 数据占比再投入 |
| 4 | LLM-judge 用什么模型 | extract_model 同款策略：独立配置，默认继承默认 provider |
