# 记忆 × 知识库质量门控打通 — 实施对账

分支：main · 2026-08-30 ｜ 依据：`docs/knowledge-and-wiki-design.md`（G3/§4.7/§4.8）、`docs/design/world-state-plan.md`（A5/A5b）

## 背景

全局记忆与 KB 原是两条零数据流的平行管线：记忆不被索引/检索，KB 洞见不沉淀回记忆；蒸馏静默覆写 MEMORY.md；`capture/auto_summarize/pool_flush_threshold/summarize_model/memory_budget_chars` 五个配置只写不读。本次按「agent 起草、人批准」给两条管线装上质量门并打通。

## 已实现

### 闸 1 · 捕获质量信号
- `append_to_pool(..., cited=bool)`：pool JSONL 行新增 `cited`（stream.py 在 `_process_turn_citations` 返回 verified 计数后置位）。
- `capture` 配置复活：为假时跳过捕获（api/stream.py 捕获点）。

### 闸 2 · 蒸馏门控（草稿模式，复活 A5b）
- `memory/summarize.py` 重写：`create_draft`（LLM 只产草稿，写 `~/.ginno/memory/draft.md` + `draft.json`，不碰 MEMORY.md/pool）/ `apply_draft(content?, force?)`（写盘 + 按 `pool_cutoff` 清池——审核期间新轮次不丢 + 陈旧守卫：MEMORY.md 变化时拒绝，除非 force）/ `discard_draft`（保留 pool）/ `remove_memory_lines`。
- 单草稿槽 + `_DISTILL_LOCK`：并发触发返回既有草稿（不重跑 LLM）；manual 撞锁报 `distill_in_progress`，auto 静默跳过。
- 阈值自动起草（A5b）：stream.py 捕获后 `pool_count() ≥ pool_flush_threshold` 且无待审草稿 → `spawn_bg` 起草 + 广播；失败 10 分钟节流。
- `summarize_model` 复活：provider 解析顺序 请求参数 > 配置 > 默认。
- 新端点：`GET /api/memory/draft`、`POST /api/memory/draft/apply|discard`；`POST /api/memory/summarize` 语义变更为产草稿；`GET /api/memory` 增 `draft_pending/kb_usable`。

### B1 · KB 感知蒸馏
- `SUMMARIZE_PROMPT` 增「证据权重」（`[有引用验证]` 前缀条目优先）与「与知识库的关系」段；预算参数化（`memory_budget_chars`）。
- 蒸馏输入附「近期知识库使用台账」（`usage.top(sort="cited", limit=10)`，仅 `cfg.usable`，try/except 降级——KB 坏了不阻塞蒸馏）。

### 闸 3 · 提升门控（记忆 → KB）
- `POST /api/kb/wiki/promote/preview {text,title?}`：去重闸（`WikiRetriever.retrieve(text)`，≥0.5 判 merge——检索分每字段每查询只计一次，title+summary 双命中即封顶 0.5）+ 确定性页面草稿（无 LLM；frontmatter `type: memory / confidence: medium / tags [memory] / sources [ginno://memory]`，复用 `WikiCompiler._dump_frontmatter`）+ 落点 `~<namespace>/Memory/`（设计 §3.1 既定记忆副本区：被检索索引、被编译器排除）。
- `POST /api/kb/wiki/promote/apply {path, raw, remove_from_memory?}`：复用 `_vault_resolve` 越界守卫 + 已存在拒绝 + `_kb_refresh`/`_maybe_build_semantic`；可选按行精确移除 MEMORY.md 对应段；广播 `kb.changed` + `memory.changed`。
- `/api/kb/wiki/list` 响应补 `type/confidence`。

### 前端
- MemoryPanel 重构：按 `## ` 分段渲染 + 悬停「沉淀到知识库」；「提炼草稿」→ 直开审核弹窗；待审草稿横幅。
- 新 `MemoryDraftModal`（DiffView 差异 + 字数/预算 + 可编辑 + 采纳/丢弃 + memory_changed 二段强采纳）与 `PromoteModal`（去重结果/合并建议 + 可编辑 raw + 「同时从记忆移除」默认开）。
- 事件与徽标：新全局事件 `memory.changed`（`_push_global_event`）→ ChatStream case → store `reloadMemoryBadge` → `panelBadge.memory`；RightPanel/RightDock Memory tab 紫点。
- KnowledgeSettings 新增「记忆提炼」fieldset（五个复活配置项）。
- KB 页：`type === "memory"` 页显示「记忆」pill + 「仅记忆来源」过滤。

## 决策与偏差

- **结构化晋升候选（LLM 尾部块解析）未做**：批准范围明确列为 follow-up；B1 用「（已收录于 KB: 页名）」行内提示替代。
- **merge 阈值 0.5（非 0.75）**：0.75 是 association 页间相似度先例；检索 query 打分饱和于 0.5（无 tag 命中时），实测后调整并注释。
- **A1（记忆作为检索源注入）未做**：提升路径已让记忆成为可检索知识；合成索引条目需对抗 `refresh()` 全量重建，成本高，列 follow-up。
- 自动起草沿用世界状态规划的产品决策：默认开、阈值 30、设置页可关；草稿模式保证自动路径也永不静默覆写。

## 验证记录（2026-08-30）

- 全量回归：1066 passed / 1 skipped（新增 26：unit 草稿生命周期/KB 摘要/引用标记、api 草稿四端点 + promote、e2e capture 门控 + 阈值自动起草）。
- 真实应用走查（独立 GINNO_HOME + GINNO_FAKE_LLM + 8899 端口，未触碰用户运行中的 app）：设置「记忆提炼」块渲染与保存 → 对话轮捕获（pool: 1）→ 提炼草稿弹窗（diff/预算/可编辑）→ 采纳（MEMORY.md 更新、pool 清零、草稿槽删除、tab 徽标点熄灭）→ 段落沉淀（去重闸命中 50% 相似页并建议合并）→ 确认（页面落 `Ginno/Memory/`、记忆段移除）→ KB 页可检索、带「记忆」pill、「仅记忆来源」过滤生效。

## Follow-ups（未做）

1. A1：MEMORY.md 作为合成索引条目参与每轮检索注入（需索引器扫描后重注入钩子）。
2. 使用率驱动的遗忘/衰减（需条目级注入跟踪，当前仅整页注入）。
3. 设计 §4.8 的 `memory_save/memory_recall/memory_forget` agent 工具。
4. 结构化晋升候选（蒸馏输出尾部块解析 → 候选清单直连沉淀弹窗）。
