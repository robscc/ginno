# Workflow 可靠性方案讨论

> 起因：run `0d864e3243`（generate_stock_analysis_and_todos）的退化链——
> search 步未写 context.stocks → loop 静默跳过 0 迭代 → 最后一步拿不到报告路径，
> 用 glob_files 从 home 目录盲扫 9 分钟（爬进 Library/、System/Volumes/、.ginno/）。
> 暴露 4 个层面的问题，逐一讨论。

---

## 事实基础（已核准的实现现状）

| 机制 | 现状 |
|---|---|
| context 写回 | **纯提示词约定**：step 系统提示要求模型在结尾输出 `WRITE_JSON {...}`，AgentNode 用 `parse_writes` 解析。模型不输出就什么都没有，无任何校验/重试 |
| loop 空序列 | `over` 求值为空/非 list → `items=[]` → 直接路由到 next，**无事件、无错误**；loop 节点只发 node_enter 不发 node_exit → 步骤状态永远 "running" |
| 步骤产物 | 仅 LLMNode 支持 `output: key`（整段文本入 context）；AgentNode 无确定性产物捕获 |
| 文件工具 | fork agent 继承源 agent 全部工具与 workspace；glob/read 接受绝对路径 → **全盘可读**（本次事故实证） |
| 事件时间线 | 事件在 superstep 边界批量落盘，ts=落盘时刻 → UI 时间线失真（本次 run 14 条事件同一秒时间戳） |
| summarize 提示词 | **完全不知道 WRITE_JSON 约定的存在**——生成的 DSL 里 "Store X in context.Y" 这类 goal 全靠模型自觉 |

---

## 议题 ① loop 空序列与步骤状态收尾（✅ 已定：on_empty 默认 skip）

**决定**：DSL loop 节点增加 `on_empty`，**默认 `"skip"`**（可配 `"fail"`）。
无论取值，以下修复无条件落地：

- 空序列发 `loop_skip` 事件（带 over 表达式与求值结果摘要），fail 取值则发 error 归因到 loop 节点；
- loop 正常结束 / 达 max_iters 补发 node_exit（状态收尾），达上限加发 `loop_cap` 事件；
- body 未执行时置新状态 `skipped`，UI 配套（STATUS_COLOR/LABEL/时间线各一处）。

合法性依据：空序列语义有真实歧义（"处理每个失败项"恰好没有失败项 = 正常空；
"选出 10 只股票"为空 = 异常）。上游契约违反由 ② 的 writes 校验在产出方拦住，
loop 侧只需消费方语义。

---

## 议题 ② context 传递可靠性（✅ 已收敛：隐式抽取节点 + writes 声明）

**决定**：结构化产出的责任从步骤模型移到编译器——声明 `writes` 的步骤，
compile 时自动注入隐式抽取节点，由它产出严格 JSON（array 一等公民）。
步骤模型不需要知道任何 context 约定。

### 设计

**1. DSL：step 声明产出**（只声明 schema，不教模型任何约定）：

```json
{"id": "search_and_select", "type": "step", "agent": "research",
 "goal": "搜索全球市场新闻，选出 10 只明日 A 股关注股票",
 "writes": {"stocks": {"type": "array", "items": {"type": "object"}}}}
```

**2. Compiler：自动注入**（validate 之后、建图之前做 DSL 预处理）：
对每个带 `writes` 的节点 `N`：
- 合成节点 `N__extract`（`type: "extract"`，内部类型，不进用户 DSL 文档）；
- 边改写：`N → X` 变为 `N → N__extract → X`（loop body→loop 的回边同样适用）；
- `N__extract` 携带：`source: N`、`writes` schema、模型配置。

**3. 抽取节点行为**：
- 输入 = `state.results[N]`（步骤最终回复文本；前置条件：AgentNode 需把
  result_text 存入 results——实现时核对，缺则补）；
- 提示词 = 「从下面的步骤输出中提取字段，严格按此 JSON Schema 输出。只输出
  JSON。字段内容确实不存在时输出 null，不要编造。」+ schema + 步骤输出；
- 解析 + schema 校验（顶层类型、array 元素类型；更深结构尽力校验）；
- 校验失败 → 带错误信息重试一次 → 仍失败 → **步骤 fail，error_detail.node_id
  归因到 `N__extract`**（既然声明了 writes 就是硬契约）；
- 合法空数组 `[]` 是合法产出（配合 ① 的可见 skip），`null`/缺失才算失败。

**4. 模型**：独立配置项 `extract_model`，**默认继承步骤模型**——先用对的，
以后可换廉价模型。

**5. WRITE_JSON 兼容**：步骤回复中已有**合法** WRITE_JSON 时直接采用、跳过
抽取调用（省一次 LLM）；手写 DSL 旧行为完全不受影响。WRITE_JSON 保留为
兼容通道，不再对外推广。

**6. 边界**：未声明 `writes` 的步骤**不注入**抽取节点——纯任务步骤零开销。

### 事件 / UI 模型

- `N__extract` 是真实图节点：DAG、时间线、步骤清单天然可见（渲染为步骤的
  附属小节点，type 标签 `extract`）；
- 抽取失败 = 普通 error 事件（node_id = `N__extract`），RunErrorBox 归因直接可用；
- 抽取成功发 `context_write` 事件（现有 kind，keys = 声明字段）。

### summarize 提示词更新

- 教唯一新概念：「为下游需要数据的步骤声明 `writes`（JSON Schema 类型；列表用
  array + items）」；
- 不再提 WRITE_JSON；goal 回归纯任务描述（"Store X in context.Y" 这类措辞淘汰）。

### 结构性收益（对照事故链）

| 事故环节 | 收敛后 |
|---|---|
| 模型忘了 WRITE_JSON → stocks 丢失 | 抽取节点专职产出，声明校验兜底 |
| summarize DSL 不懂引擎约定 | 只需教 writes 声明一个概念 |
| loop 拿到空静默跳过 | writes 硬契约在上游拦截；真为空走可见 skip |
| array 在自由文本结尾最易碎 | 抽取器唯一职责就是产出干净 JSON |

---

## 议题 ③ 文件工具边界（最大议题，安全设计）

现状：fork agent 全盘可读（本次实证扫了 home、Library、System/Volumes、.ginno 原始数据）。
写操作同样无边界——这次只是碰巧在"找文件"。

### 选项 A：workspace 根限制
- 文件工具限定在 run 的 workspace 内；相对路径解析到 workspace；绝对路径必须
  位于允许根列表内。
- ✅ 简单、符合直觉
- ❌ 本 workflow 的合法目标（Obsidian 库）就在 workspace 外——一刀切会打死合法场景

### 选项 B：运行级 allowlist（DSL/context 声明）⭐ 建议
- DSL 顶层或 context.schema 声明 `paths: [...]`（如 obsidian_raw_path）；run 启动时
  计算允许根集合 = workspace ∪ 声明路径；文件工具（glob/read/write/bash 的文件动作）
  强制校验。
- ✅ 显式、可审计（UI 可在运行前展示"此流程将访问这些路径"）
- ✅ 与 ② 的声明式方向一致；summarize 提示词可教它从 goal 里识别路径
- ❌ 需要枚举哪些工具受管（glob_files/read_file/write/patch/bash？bash 无法完全管住——
  见待决定 3）

### 选项 C：越界走权限审批（对齐聊天的 permission.request）
- ❌ workflow 常为 headless（无人可批）；交互审批打断自动化。可作 B 的补充：
  有 UI 的 run 越界时暂停询问，headless 直接拒绝。

### 选项 D：读宽写严
- 读允许较宽、写严格限制。❌ 本次事故就是读扫描；且读敏感文件同样危险。不单独采用。

### 无论选什么都要做的底线
- **硬性 deny 列表**：`.ginno/`（运行时自己的数据！本次被爬）、`.ssh`、凭据目录——
  任何声明都不能解锁。

**待决定**：
1. allowlist 声明在哪：DSL 顶层 `paths` vs context.schema 里标 `format: "path"` 的字段
   自动纳入？（倾向后者+显式 paths 并存——context 里已有 obsidian_raw_path 这类值，
   自动识别零配置，但需要"哪些值算路径"的判定规则）
2. 越界行为：报错给步骤（tool error，让 agent 调整）还是直接 fail run？
   （倾向 tool error + 事件，配合 ① 的失败归因足够）
3. **bash 工具**：文件边界管不住 bash（`cat ~/.ssh/...` 防不了）。选项：workflow 步骤
   默认禁 bash / bash 也走 allowlist 前缀检查（弱）/ 维持现状但记录风险。怎么定？
4. 存量 workflow（内置 todo-sync 等）是否需要迁移？（todo-sync 走 MCP 不走文件工具，
   预计不受影响，需验证）

---

## 议题 ④ 事件时间线保真（顺手修）

事件在 superstep 边界批量落盘、ts=落盘时刻 → UI 时间线失真、stuck 检测只能靠
run.updated 心跳。**修法**：`emit()` 时即打 ts（事件自带时间），`append_event`
保留已有 ts。一行级改动，随 ① 一起修。

---

## 建议的实施顺序

1. **①+④**（半天）：loop_skip 事件、状态收尾、on_empty、事件 ts 保真——
   纯引擎修复，先落地。
2. **③ 底线**（半天）：.ginno/.ssh 等硬 deny——不依赖任何方案选型，先止血。
3. **②**（1-2 天）：writes 声明 + 编译器注入抽取节点 + 校验/重试 +
   summarize 提示词更新（方案已收敛，见议题 ②）。
4. **③ allowlist**（1-2 天）：依赖 ② 的声明机制成型后做，避免两次改 schema。

---

## 决策点状态

| # | 决策 | 状态 |
|---|---|---|
| 1 | loop `on_empty` | ✅ 已定：**默认 skip**（可配 fail），skip/fail 均发显式事件 |
| 2 | context 写入机制 | ✅ 已定：**隐式抽取节点**（writes 声明 + 编译期注入）；合法 WRITE_JSON 直接采用跳过抽取 |
| 3 | 抽取/writes 失败策略 | ✅ 已定：**步骤 fail，归因 `N__extract`**（硬契约） |
| 3b | 抽取模型 | ✅ 已定：独立配置项 `extract_model`，默认继承步骤模型 |
| 3c | 注入边界 | ✅ 已定：未声明 writes 的步骤不注入，零开销 |
| 4 | 文件 allowlist 声明位置 | ⏳ 待定（倾向 context 路径字段自动识别 + 显式 paths） |
| 5 | 越界时 tool error 还是 fail run | ⏳ 待定（倾向 tool error + 事件） |
| 6 | workflow 步骤的 bash 怎么管 | ⏳ 待定（默认禁 / 弱检查 / 接受风险） |
