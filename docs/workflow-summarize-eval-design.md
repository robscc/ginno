# Workflow 总结质量优化设计（Summarize-from-Session 可观测与评测）

> 目标场景：聊天会话「总结成流程」(`POST /api/workflows/summarize-from-session`)。
> 本设计解决两个核心指标的持续优化闭环：**成功率**与**准确率**，并提供问题定位手段。

---

## 1. 指标定义

两个指标都需要分层定义，否则"失败"无法归因。

### 1.1 成功率（能总结出完整可运行工作流的比率）

按漏斗分层，每层都可独立统计：

| 层级 | 定义 | 判定方式 |
|---|---|---|
| L1 解析成功 | 模型输出能解析为 JSON 对象 | 自动（`_extract_json_obj`） |
| L2 校验成功 | 通过 `normalize_dsl` + `validate_dsl` | 自动（现有逻辑） |
| L3 采纳成功 | 用户把草稿保存为工作流（而非放弃） | 自动（需埋点，见 §3.2） |
| L4 可运行 | 保存后的首次运行完整跑完（status=done） | 自动（需溯源链，见 §3.2） |

- **语法成功率** = L2 / 总请求数（当前唯一能自动算的）
- **完整成功率（主指标）** = L4 / 总请求数

### 1.2 准确率（成功的基础上完整复现对话流程的比率）

"复现"不是逐字复现，而是**流程语义复现**：工作流以相同输入运行时，节点序列、
分支/循环行为、关键工具动作与原会话的实际流程一致。

判定分两档：

- **结构准确率（自动，粗粒度）**：运行 trace 与原会话 trace 的结构对齐分 ——
  节点数/顺序相似度、原会话出现的工具名在 DSL goal/步骤中的覆盖率、
  branch/loop 拓扑是否对应原会话中的条件与重复。
- **语义准确率（judge/人工，细粒度）**：LLM-as-judge 按 rubric 打分
  （步骤完整性 / 顺序正确性 / 变量提取正确性 / 是否引入原会话没有的步骤），
  人工标注作为校准基准。

**准确率（主指标）** = judge 判定"完整复现"的运行数 / L4 成功数。

---

## 2. 现状与缺口

当前管线（`api/workflows.py`）：

```
session messages → _trace_text(截断) → [_SYNTHESIZE_PROMPT, trace] → model
→ _extract_json_obj → normalize/validate → 最多 3 次带错误反馈重试 → 返回草稿
```

缺口：

1. **零记录**：输入（trace、provider、last_n）与输出（每次 attempt 的 raw、
   最终 dsl、错误）都不落盘，失败无法事后 review。
2. **溯源链断裂**：UI `createFromSummarize` 调 `createWorkflow` 时不带
   `source_session_id`，草稿→工作流→运行无法 join，L3/L4 与准确率都算不了。
3. **无失败分类**：error 是自由文本，无法按根因聚合（解析失败 vs 悬空边 vs
   覆盖不全 vs 截断丢信息）。
4. **无评测集**：改 prompt / 换模型只能手测，无回归保证，无法量化"优化后提升了"。
5. **用户信号丢失**：用户在 SummarizeModal 里删节点、改 goal、加变量、放弃草稿，
   这些是免费的质量标注，目前全部丢弃。
6. **prompt 无版本**：`_SYNTHESIZE_PROMPT` 改动后历史记录无法区分新旧版本产出。

---

## 3. 记录层设计（输入输出落盘，方便 review）

### 3.1 存储布局

沿用 `usage_dir()`（按天 JSONL）的先例，新增
`paths.synthesis_dir()` → `~/.ginno/synthesis/`：

```
~/.ginno/synthesis/
├── records-YYYY-MM-DD.jsonl     # 索引：每次 summarize 调用一行（小，供统计）
└── cases/
    └── <record_id>.json         # 全量 case：输入输出原文（大，供人肉 review）
```

索引行保持小（统计、grep 友好）；全量内容放独立文件（trace 可能几十 KB），
避免 JSONL 膨胀到无法阅读。

### 3.2 记录 schema（`cases/<record_id>.json`）

```jsonc
{
  "record_id": "syn-<uuid10>",
  "ts": 1760000000.0,
  "prompt_version": "v1",               // _SYNTHESIZE_PROMPT 版本号
  "input": {
    "session_id": "…",
    "project_slug": "default",
    "provider": "…", "model": "…",
    "last_n": null,
    "message_count": 42,                // 截断前消息总数
    "trace_chars": 18342,
    "trace": "USER: …\nAGENT: …",       // 实际发给模型的 trace 全文
    "tool_names_seen": ["list_prs", "write_file"]  // 原会话出现的工具，覆盖率评分用
  },
  "attempts": [                          // 每次 attempt 都留底
    {"i": 1, "raw": "…模型原始输出…", "parse_ok": true, "errors": ["edge to unknown node x"]},
    {"i": 2, "raw": "…", "parse_ok": true, "errors": []}
  ],
  "output": {
    "ok": true,
    "dsl": { "…最终 DSL…" },
    "attempts_used": 2,
    "latency_ms": 8400
  },
  "failure_class": null,                 // 见 §4，自动能填的先填
  "outcome": {                           // 后续事件异步回填（见下）
    "user_action": "saved|abandoned|edited_saved|dev_refine|null",
    "edited_dsl": { "…用户编辑后实际保存的 DSL…", "（diff 即免费标注）": "" },
    "workflow_id": "…",
    "first_run": {"run_id": "…", "status": "done|failed|cancelled", "error": null}
  }
}
```

索引行（`records-*.jsonl`）只含：
`record_id, ts, session_id, provider, prompt_version, ok, attempts_used,
failure_class, latency_ms` + outcome 回填字段。

### 3.3 溯源链打通（outcome 回填）

三处小改动把断链接上：

1. **总结时**：endpoint 生成 `record_id`，写 case 文件，并在响应里返回
   `record_id`（现有响应加一个字段）。
2. **创建时**：`POST /api/workflows` 接受可选 `provenance`
   `{source_session_id, synthesis_record_id}`，存进 `meta.json`；
   UI `createFromSummarize` / `openDevFromSummarize` 透传。
   创建成功即回填 record 的 `outcome.user_action / edited_dsl / workflow_id`。
   用户在弹层里关窗放弃 → UI 调一个轻量
   `POST /api/workflows/summarize-outcome`（`{record_id, action:"abandoned"}`）。
3. **运行时**：run 终态钩子（`_set_run_status` 的 terminal 分支）检查该
   workflow 的 `provenance.synthesis_record_id`，若该 workflow 尚无
   first_run 记录，则回填 `outcome.first_run`。

至此 **record → workflow → run** 全链路可 join，L3/L4/准确率全部可自动统计。

### 3.4 隐私边界

trace 含用户对话原文。默认**仅本地**（与现有 `usage/`、checkpoints 一致），
不随任何请求外发；review 报告工具也纯本地生成。是否需要脱敏导出，见 §8 开放问题。

---

## 4. 失败分类体系（定位问题的核心）

每条失败记录打一个 `failure_class`，能自动判定的自动判定，判定不了的留给
judge/人工。分类直接对应修复手段，聚合后即可回答"该优先改哪里"：

| class | 含义 | 自动判定 | 对应修复杠杆 |
|---|---|---|---|
| `E1_PARSE_FAIL` | 输出不是 JSON 对象（3 次后仍失败） | ✅ | 结构化输出 / prompt |
| `E2_VALIDATION_FAIL` | DSL 校验不过；带子类 | ✅ | 错误反馈模板 / prompt |
| ├ `unknown-entry` / `dangling-edge` / `branch-edge-conflict` / `placeholder-not-in-schema` / `unknown-agent` / `loop-body-edge` | 校验错误子类（从 validate_dsl 错误串归一） | ✅ | 同上 |
| `E3_INCOMPLETE` | 语法过了但漏掉原会话关键步骤 | judge/人工 | trace 截断策略 / prompt |
| `E4_SEMANTIC_DRIFT` | 步骤在但顺序/意图错位，或臆造步骤 | judge/人工 | prompt / few-shot |
| `E5_RUNTIME_FAIL` | L4 前运行失败（表达式、缺工具、agent 不可用） | ✅（run 回填） | 引擎健壮性 / 预检 |
| `E6_TRUNCATION_LOSS` | 关键信息被 trace 截断丢失 | judge（对比全文与 trace） | `_trace_text` 策略 |
| `E7_USER_ABANDON` | 用户看了草稿直接放弃 | ✅（outcome 埋点） | 草稿质量/产品交互 |

`E2` 子类让"校验失败"不再是黑盒：如果发现 80% 是
`placeholder-not-in-schema`，就该改错误反馈措辞或在 normalize 阶段自动补全，
而不是重写整个 prompt。

---

## 5. 指标与报表（产品视角）

### 5.1 漏斗与分布

`scripts/synthesis_report.py`（P0 交付物）读 records 输出：

```
summarize 漏斗（近 30 天 / 按 prompt_version 分组）
  请求 120 → L1 解析 116 (96.7%) → L2 校验 104 (86.7%)
  → L3 采纳 71 (59.2%) → L4 首跑成功 58 (48.3%)
  准确率(judge): 44/58 = 75.9%

失败分布: E2:12 (placeholder-not-in-schema×7, dangling-edge×3, …)
         E3:18  E5:9  E7:22
维度切片: provider×成功率、last_n×成功率、message_count 分桶×成功率
重试收益: attempt2/3 挽回 9 次 (占 L2 的 8.7%)
编辑距离: 保存草稿 vs 原始草稿平均改动 2.3 个节点（越小说明越准）
```

**编辑距离**（合成 DSL 与用户最终保存 DSL 的节点级 diff）是不需要人工标注的
准确率代理指标，产品上可以直接看趋势。

### 5.2 产品侧补充

- **SummarizeModal 反馈按钮**：草稿卡片上加 👍/👎，👎 展开结构化选项
  （漏了步骤 / 顺序不对 / 变量识别不对 / 步骤太粗），写入
  `outcome.user_feedback`。这是最便宜的人工标注来源。
- **覆盖提示**：总结完成后弹层头部显示「覆盖 5 个关键步骤 · 3 个工具调用」，
  让用户在保存前就能发现漏步骤（同时降低 E3 的隐性占比）。
- **usage-stats 面板加一张"工作流总结"卡片**：漏斗 + 失败分布，复用现有
  usage 页面模式。

---

## 6. 离线评测（Eval Harness）—— 迭代与回归的基础

没有 eval 集，任何 prompt 优化都是盲改。

### 6.1 Golden set

- 来源：从 §3 记录的真实 case 中挑选（覆盖短/长会话、有无分支循环、
  多工具调用），人工确认后拷入 `evals/summarize/cases/`。
- 每个 case 含：`input.trace`（+ 可选完整消息备份，供 E6 判定）、
  `expect` 标注：期望关键步骤列表、是否应有 branch/loop、期望变量。
- 起步 15–25 个即可，随 review 持续扩充（每个真实失败 case 修好后沉淀进去）。

### 6.2 `scripts/eval_summarize.py`

对 golden set 重放合成（复用线上同一 `_trace_text` + prompt + 任意指定
provider/model），输出每条 case 的得分与总体报告：

- **硬指标**：L1/L2 通过率；`tool_names_seen` 在 DSL 中的覆盖率；
  期望关键步骤被 goal 覆盖的比例（关键词/embedding 匹配）。
- **Judge 指标**：LLM-as-judge 按 rubric 对
  coverage / faithfulness / reproducibility 各打 1–5 分并给理由。
- 结果写 `evals/summarize/runs/<时间戳>-<prompt_version>-<model>.json`。

### 6.3 A/B 对比

同一 golden set 分别跑 prompt v1 / v2（或不同模型），report 脚本并排输出
差量。**任何 prompt 改动必须先过 eval 再上线**，且旧 case 分数不得回退
（作为 CI 可选门禁）。

---

## 7. 候选优化杠杆（由数据驱动，按定位结果选择）

记录与 eval 就位后，按失败分布排序逐项实施，预期优先级：

1. **结构化输出**（治 E1/E2 大头）：用 provider 的 JSON mode /
   tool-call 强约束输出，替代自由文本解析；`_extract_json_obj` 的兜底保留。
2. **错误反馈模板增强**（治 E2 子类）：现在只回传错误串；改为回传
   "错误 + 出错节点片段 + 修正示例"，并对高频子类（如 placeholder）
   在 `normalize_dsl` 里做确定性自动修复（自动把 DSL 里出现的
   `{{var}}` 补进 schema）——能用代码修的不用 token 修。
3. **trace 截断策略**（治 E3/E6）：当前消息 500 字符硬截断会丢目标语句。
   改为"用户消息保全优先 + agent 消息摘要 + 工具调用序列完整保留"，
   并把 todo 清单、文件写入路径等结构化信号单独提取进 trace 头部。
4. **Few-shot**（治 E4）：prompt 内嵌 1–2 个高质量 trace→DSL 示例。
5. **运行前预检**（治 E5）：创建后首跑前做静态检查（branch 表达式可求值、
   agent 可用、工具存在），把运行期失败左移到可提示的校验期。
6. **失败兜底**（产品）：3 次失败后不再只给错误串，而是生成一个
   "单步骨架草稿"让用户有东西可编辑，把 E1/E2 失败转化为 E7 观察项。

---

## 8. 实施分期

| 期 | 内容 | 产出 |
|---|---|---|
| **P0 记录闭环** | `paths.synthesis_dir()` + recorder 模块；summarize endpoint 写记录（含 prompt_version）；`provenance` 字段打通创建与 run 回填；outcome 端点；`scripts/synthesis_report.py` | 输入输出可 review，漏斗可统计 |
| **P1 定位工具** | failure_class 自动归类；case 级 HTML review 页（trace ∥ attempts ∥ DSL 图 ∥ outcome）；SummarizeModal 👍/👎 反馈埋点 | 失败可归因，用户信号入库 |
| **P2 Eval** | golden set 目录与挑选流程；`eval_summarize.py` + judge rubric；A/B 报告 | 优化可回归、可量化 |
| **P3 优化实施** | 按 §7 失败分布逐项落地（结构化输出、normalize 自动修复、trace 策略…） | 指标提升 |

P0 改动集中在 runtime（`paths.py`、新 `synthesis.py`、`api/workflows.py`、
`workflows/store.py`）与 web 两处透传，均可加单测（沿用
`tests/api/test_workflow_summarize.py` 的 seeded session 模式）。

---

## 9. 开放问题（实施前需确认）

1. **review 场景**：记录只在本机 `~/.ginno/synthesis/` 够吗？是否需要
   导出命令（打包某时间段 records + cases 成 zip）以便多机汇总？
2. **准确率判定口径**：LLM judge 为主、人工抽检校准是否可接受？judge 用
   哪个 provider（成本 vs 质量）？
3. **隐私**：trace 原文本地留档默认开启；是否需要开关或 N 天自动清理
   （如 cases 保留 90 天，索引永久）？
4. **golden set 来源**：初期全部来自自己的真实会话吗？（涉及第三方数据时
   需人工审查后入库）
