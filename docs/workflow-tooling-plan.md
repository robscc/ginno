# Workflow 配套工具方案

> 横跨三个方案（ux-redesign 已落地 / reliability-discussion / synthesis-quality-plan）
> 的工具地基。原则：先补「今天手工干过的事」，再补「度量驱动优化工具」，UI 最后。

---

## 0. 现状缺口（为什么要这些工具）

| 今天发生的事 | 暴露的缺口 |
|---|---|
| 排查 run 0d864e3243 靠手写 python one-liner 翻 events.jsonl | 无 run 取证工具 |
| 股票 workflow「loop 依赖未声明的 context.stocks」运行前无人发现 | 无 DSL 静态检查（数据流 lint） |
| 议题② 会给每个 writes 步骤增加抽取 LLM 调用 | 无 token/成本遥测，无法管控与优化 |
| 质量方案要 replay/评审 | 无 CLI、无评测 runner |
| 议题② 落地后存量 DSL（含内置 seed）要补 writes | 无迁移工具 |

---

## 1. ginno-cli：运行时管理 CLI（地基，P0）

`uv run python -m ginno_runtime.cli <cmd>`（开发期源码跑；打包进 sidecar 二进制后
`ginno-runtime cli ...` 同入口复用）。

| 子命令 | 用途 | 服务谁 |
|---|---|---|
| `workflow show <run_id>` | run 取证：状态/步骤/事件时间线/工具调用汇总/文件访问足迹/耗时 | 排障（替代手工翻 jsonl）|
| `workflow doctor <wf_id>` | 静态检查（见 §3） | 可靠性①②③、总结质量 L1 |
| `workflow runs [--status x]` | run 列表/清理 | 运维 |
| `synth list/show/stats/replay` | 总结 case 评审（quality-plan §3） | 质量体系 P1-P4 |
| `workflow upgrade <wf_id>` | LLM 辅助补 writes 声明（见 §5） | 议题② 迁移 |

## 2. Run 遥测：节点级 token/耗时（P0，便宜且急需）

事件补两个字段（节点退出时从 AIMessage.response_metadata 提取）：
- `node_exit` 追加 `usage: {input_tokens, output_tokens}`、`latency_ms`
- events.jsonl 结构不变，前端时间线暂不渲染（先入数据）

**为什么 P0**：议题② 的抽取节点会让带 writes 的步骤 LLM 调用翻倍——没有成本
数据就无法决定 extract_model 何时该换廉价模型；L3（首跑成功率）诊断也需要知道
慢在哪。CLI `workflow show` 直接聚合展示。

## 3. workflow doctor：静态数据流 lint（P1，议题② 落地后规则最全）

编译前对 DSL 做无 LLM 的静态检查，规则集：

| 规则 | 等级 | 备注 |
|---|---|---|
| `loop.over` 引用的 context 键无上游 writes/initial 来源 | error | **本次事故的运行前拦截点** |
| goal 引用 `{{context.X}}` 但 X 无来源（initial/writes/loop as 变量） | error | |
| `{{var}}` 未定义 | error | 现有 validate 可扩展 |
| writes 声明的键从未被下游消费 | warn | 死代码提示 |
| goal 含路径字面量但未声明 paths（议题③ 后） | warn | |
| 步骤数=1 且无工具意图（纯聊天总结常见） | warn | 总结质量预期管理 |

**挂载点**（三处都要，成本一份规则库）：
1. summarize 草稿创建前——不满足 error 规则 → 生成阶段重试时作为 hint 喂回；
2. dev session `workflow_propose_edit` 校验时——doctor error 直接拒绝提案并回喂；
3. CLI 手动跑。

## 4. Dry-run：零 token 结构验证（P2）

`workflow dryrun <wf_id>`：step/llm 节点替换为回显 goal 的 stub，真实编译执行，
验证边连通/loop 路由/变量流动，产出「每步收到什么 context」的报告。
用途：doctor 的动态补充、golden set 冒烟、议题① loop 语义的回归验证。

## 5. DSL 迁移工具（P1，随议题②）

- `workflow upgrade <wf_id>`：LLM 一次性读 DSL + 各步 goal → 产出 writes 声明
  补丁 → doctor 校验 → 走正常版本机制（新版本，可 rollback）。
- 内置 seed（todo-sync 等）升级走代码内 seed 更新，不走 CLI。
- 顺手引入 `dsl.schema_version` 字段 + normalize 内迁移钩子（为 v2 subflow 铺路）。

## 6. 评测引擎（P2，质量方案 P4 的执行体）

- `synth replay <case|--golden>`：离线重放（读 input.json 的 trace，不需要活会话），
  支持 `--prompt <version>` 对比；
- 打分器：validate 通过率（成功率代理）+ 结构相似度（节点序列/边/变量集合，
  准确率代理）+ LLM-judge 脚本（覆盖度/顺序/幻觉步骤三项打分）；
- golden set 约定：`tests/synthesis_golden/<name>.json`（input trace + 人工标注
  期望结构），纳入 nightly；
- 门禁：提示词 bump 前跑 golden set，成功率代理回退即报警（先人工跑，CI 后补）。

## 7. 版本与治理约定（轻量，P1）

- `prompt_version` 注册表：`ginno_runtime/versioning.py` 一个 dict 常量
  （synth/extract/judge 各自版本号 + 变更记录注释），bump 即留痕；
- synthesis case 保留策略（200 个 / 90 天）随启动惰性清理；
- run events 无保留策略问题（跟随 run 删除）。

## 8. UI 配套（最后，数据证明价值后）

- Settings 增「总结评审」tab：case 浏览/过滤（CLI 的图形版）；
- Inspector：节点耗时/token 展示、doctor 警告条；
- 👍/👎 反馈按钮（质量方案 P3 已含）。

---

## 优先级与依赖总览

```
P0（与可靠性①②并行，约 1 天）
  CLI 骨架 + workflow show        ← 今天排障的手工活工具化
  run 遥测（usage/latency 入事件） ← 议题② 成本管控的前提
P1（随质量方案 P1 + 议题②，约 2 天）
  synth 记录三件套 + synth list/show/stats
  doctor 规则库（随②落地补全数据流规则）+ 三处挂载
  workflow upgrade + schema_version
  prompt_version 注册表 + 保留策略
P2（约 2-3 天）
  dry-run、replay、打分器、golden set、LLM-judge
Later
  UI 评审页、Inspector 遥测展示
```

**关键依赖**：doctor 的数据流规则依赖议题② 的 writes 声明先落地（否则「来源」
无从定义）；replay/评测依赖质量方案 P1 的记录格式先定稿——所以实施序就是：
①② 引擎改造 → P0 工具 → 记录 P1 → doctor/迁移 → 评测 P2。
