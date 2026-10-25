# Dream（记忆蒸馏）产品设计 — UI 管理 + 用量归因

> 状态：**设计稿（2026-10-25），待评审，未实施**。
> 关联：[`memory-kb-gates-implementation-notes.md`](memory-kb-gates-implementation-notes.md)（质量门控 G1–G3）、
> [`usage-stats-design.md`](usage-stats-design.md)（用量台账 §3.6 来源口径）、
> [`knowledge-and-wiki-design.md`](knowledge-and-wiki-design.md)（KB 提升路径）。

## 0. TL;DR

- **现状**：管线齐全（采集 → 池 → 自动草稿 → 人工审 → 写 MEMORY.md），但 UI 只有一个右栏面板 + 一个弹层，
  **没有任何状态可视化、历史、观测**；单草稿槽位 + 永不回收，已经造成一次 24 天停摆（§1.2）。
- **方案**：Settings → 知识库分组新增 **「记忆工坊（Dream）」** 页（`/settings/knowledge/dream`），
  四区块：**状态卡 / 草稿队列 / 池 / 运行历史**；右栏面板保留为轻量入口，只加快捷状态与滞留告警。
- **用量**：蒸馏调用按 `source="memory"` 记账（该来源在 usage 设计与配色里早已预留，但从未有发送方），
  概览「来源分布」自动独立成桶；运行历史展示每次 run 的 token。**不新增存储**——直接复用
  `~/.ginno/usage/requests-YYYY-MM-DD.jsonl`。
- **一并补的记账缺口**：审计出 9 处未统计的 LLM 调用（§8），其中蒸馏、联网探测、标题生成、子代理计划/门控、
  团队生成 6 处为真实 token 消耗。

---

## 1. 现状盘点（代码事实）

### 1.1 管线与 UI

| 环节 | 代码位置 | 现状 | 问题 |
|---|---|---|---|
| 采集 | `api/stream/engine.py:1002-1007` | turn 结束把清洗后的助手正文追加进池，带 `cited` 质量信号 | 无池浏览入口，用户不知道攒了什么 |
| 池 | `memory/pool.py:43`、`paths.py:492` | `~/.ginno/memory/pool/<ts>.jsonl`，一行一条 | 只有计数（`pool_count`），内容不可见 |
| 自动触发 | `api/stream/effects.py:208` `_maybe_auto_distill` | 池 ≥ `pool_flush_threshold`（默认 30）→ 后台起草稿 | **有草稿就 early-return**，无过期回收 |
| 蒸馏 | `memory/summarize.py:243` `create_draft` | 双语 prompt、KB 台账注入、预算、`_DISTILL_LOCK` 防并发 | **无 usage 记账**；超预算只标记不拦截 |
| 审阅 | `apps/web/src/components/right/MemoryDraftModal.tsx` | diff + 可编辑 + 应用/丢弃 + sha1 冲突校验 | 单槽、无历史（应用/丢弃后草稿即删除） |
| 写回 | `summarize.py:330` `apply_draft` | 写 `~/.ginno/MEMORY.md`，按 cutoff 清池 | 无 |
| 配置 | `components/settings/KnowledgeSettings.tsx:237-270` | 开关/阈值/预算/蒸馏模型挤在知识库页 | 与运行状态脱节，改完看不到效果 |
| 观测 | ❌ 无 | 无运行日志、无失败可见、无 token | 停摆 24 天无人发现 |

### 1.2 事故复盘（本设计的直接动因）

`~/.ginno/memory/` 实况：

- `draft.md` / `draft.json` 的时间戳停在 **2026-10-01 11:03:47**（`trigger: "auto"`，第 30 条池触发）
- 因为 `has_draft()` 在 `effects.py:215` 与 `summarize.py:246` 直接返回，**10-01 之后每一轮都被跳过**
- 池已堆到 **290 条**（最早 09-17，最新 10-24）；`~/.ginno/MEMORY.md` 最后修改 **09-22**
- 草稿 9975 字 vs 预算 3000 字（超 3 倍），即使当时点应用也会写爆预算

结论：**自动蒸馏不是坏了，是"等人工审阅"这一步没有过期策略、没有告警、没有恢复路径**。产品设计的核心不是
再加一个按钮，而是把「Dream 运行」变成用户能看见、能管、能自愈的东西。

---

## 2. 目标与非目标

**目标**

- G1 可见：任何时刻能回答「池里有多少、上次什么时候跑、跑了多少 token、为什么没再跑」。
- G2 可控：开关、阈值、预算、模型、夜间窗口、草稿保留策略集中在一页，改完即时反映到状态卡。
- G3 自愈：草稿滞留超过阈值 → 自动过期 + 提示；自动路径永不因一次未审而永久停摆。
- G4 可追溯：保留最近 N 次运行与最近 N 份草稿（含已应用/已丢弃），可查耗时、模型、token、错误。
- G5 可计费：蒸馏的 token 成本在「用量统计 → 来源分布」里独立成桶，可看趋势。

**非目标**

- 不记录 prompt / 池正文到用量日志（沿用 `usage-stats-design §4.4` 隐私约定）。
- 不做多机汇总、不做云端同步。
- 本期不改蒸馏算法本身（prompt 与合并策略维持现状）。

---

## 3. 信息架构

```
右栏「记忆」面板（保留，轻量）           Settings → 知识库 → 「记忆工坊」
├─ 状态条（池/待审/上次运行/预算）        ├─ ① 状态卡（开关 + 水位 + 上次结果 + 近 7 天用量）
├─ 滞留告警条（>N 天未审）               ├─ ② 草稿队列（列表 + diff 抽屉）
├─ 蒸馏按钮（运行态）                    ├─ ③ 池（计数/范围/采样浏览/清空）
├─ MEMORY.md 分区（含"提升为知识页"）      └─ ④ 运行历史（runs 表格 + 失败重试）
└─ 草稿审阅弹层（复用/改造）
```

**术语统一**：一次「Dream 运行」= Run（触发 → 蒸馏 → 产出草稿）。Run 与草稿 1:1。
用户可见文案统一用「记忆蒸馏」，配置项与日志里保留 `dream` 别名（`dream.md` / `memory_dream`）。

**入口位置决策**：管理页放 Settings 而不是右栏抽屉，理由是它需要表格、历史、批量操作（右栏宽度不够），
且要与「用量统计」同层（用户核对成本时的动线）。

---

## 4. 界面设计

### 4.1 右栏记忆面板改造（`MemoryPanel.tsx`）

顶部把现有的「`pool 290` 胶囊」升级为**状态条**：

```
🧠 记忆            池 290 条 · 待审 1（滞留 24 天）      [✨ 蒸馏]
```

- **滞留告警条**（新增，条件：最老草稿 > 过期阈值的一半）：橙色，文案
  「草稿已滞留 24 天，后续自动蒸馏已暂停 → [立即审阅] [丢弃并重跑]」
- 蒸馏按钮运行中：`Loader + 「蒸馏中… 读取 290 条」`，同步展示池快照计数（避免用户以为卡了）
- 草稿横幅沿用现有样式，增加 `创建于 x 天前` 与 `超预算 6975 字` 两个 chip
- 底部加一行 `↗ 打开记忆工坊`（跳 §4.2）

### 4.2 记忆工坊页（新增，`/settings/knowledge/dream`）

#### ① 状态卡

| 字段 | 值 / 交互 |
|---|---|
| 采集开关 `capture` | Switch。关闭后池不再增长（历史池仍可蒸馏） |
| 自动蒸馏 `auto_summarize` | Switch |
| 池水位 | 数字 + 进度条（`pool_count / pool_flush_threshold`）、超阈值高亮 |
| 待审草稿 | 数量 + 最老滞留天数（>阈值红色） |
| 上次运行 | 时间、触发方式（auto / manual / schedule）、模型、耗时、token、结果徽标（成功/失败/跳过） |
| 近 7 天蒸馏用量 | 调用次数 + input/output token（读 usage 日志 `source=memory` 聚合） |
| 预算占用 | 上一份已应用草稿的字符数 / `memory_budget_chars` |

空态文案：「还没有运行过。池攒到 {threshold} 条会自动蒸馏，也可以点『立即蒸馏』。」

#### ② 草稿队列

列表列：`创建时间 · 触发 · 池条目 · 字符/预算 · 状态 · 操作`

- 状态：`待审 / 已应用 / 已丢弃 / 已过期`
- 待审行操作：`查看 diff`、`应用`、`丢弃`；过期行：`删除`
- 顶部批量：`清理已过期`、`保留最近 N 份`（默认 20）
- **diff 抽屉**：复用 `MemoryDraftModal` 的 `DiffView`，追加
  - 超预算提示升级为**拦截**：`应用`默认禁用，需点「按预算截断后应用」或手动编辑到预算内
  - 「拆两次」建议按钮：把草稿正文按 `##` 主题分组，勾选若干主题生成一份新草稿留在队列里（不新增 LLM 调用）
- 并发上限：**最多 3 份待审**。超出时创建新 run 前自动把最老一份置为 `已过期`（记录 `expired_reason`）

#### ③ 池

- 头部：`290 条 · 09-17 → 10-24 · cited 42 条`
- 列表（分页 20）：`时间 · 会话标题（点击跳会话）· 智能体 · cited 徽标 · 内容前 200 字`
  - 后端只返回**截断摘要**，不返回全文（隐私 + 载荷）
- 操作：`立即蒸馏`、`清空池`（危险确认：需二次输入"清空"）
- 说明文案：「池保存的是每个 turn 结束后的助手正文（已做注入清洗），不含你的提问。」

#### ④ 运行历史

表列：`时间 · 触发 · 模型 · 状态 · 耗时 · 输入 token · 输出 token · 池条目 · 产出字符 · 错误`

- 状态：`success / failed / skipped`（skipped 记原因：`draft_pending` / `throttled` / `pool_empty` / `in_progress`）
- 失败行：`重试` 按钮（同参数再跑一次，手动触发不受 600s 节流限制）
- 顶部：`自动路径节流：正常 / 冷却至 10:32`（对应 `_AUTO_DISTILL_FAIL_AT`）

### 4.3 设置项（迁移收敛）

从知识库页迁入本页（知识库页保留只读提示与跳转，避免两处可改）：

| 配置 | 字段 | 默认 | 说明 |
|---|---|---|---|
| 自动蒸馏 | `auto_summarize` | true | |
| 触发阈值 | `pool_flush_threshold` | 30 | |
| 记忆预算 | `memory_budget_chars` | 3000 | |
| 蒸馏模型 | `summarize_model` | ""（= 默认 provider） | |
| **夜间窗口**（新增） | `dream_nightly` / `dream_at` | 关 / "03:00" | 见 §4.4 |
| **草稿过期**（新增） | `draft_ttl_days` | 7 | 0 = 不自动过期 |
| **草稿保留份数**（新增） | `draft_keep` | 20 | |

配置仍落在 `knowledge` 配置对象（`knowledge/types.py`），不新建配置文件。

### 4.4 夜间窗口（「Dream 之所以叫 Dream」）

- 复用现有 scheduler（`scheduler.py` + `~/.ginno/schedules.json` 的机制），注册一个**内置任务** `memory_dream`，
  不进用户的 schedules 列表，只在本页开关；默认 **03:00**、可改时间、可关。
- 触发前检查（任一不满足 → `skipped` 并记原因）：`auto_summarize` 开、池 ≥ 阈值、无 `待审` 草稿、不在冷却期。
- 与 turn 触发的关系：**并存**。turn 触发负责"攒够就跑"，夜间窗口负责"每天至少一次收口"；
  两者共享 `_DISTILL_LOCK` 与阈值判断（幂等，先到先跑，后到见 `draft_pending` 跳过）。

### 4.5 草稿状态机

```
          ┌─ apply ─────────→ applied（写 MEMORY.md，清 cutoff 前的池）
pending ──┼─ discard ───────→ discarded（池保留）
          └─ ttl 到期/挤出 ──→ expired（池保留；可重跑）
```

- 过期与挤出都**必须写 runs.jsonl 的一条 `skipped/expired` 记录**，避免"静默消失"重演 §1.2。
- 应用后立刻检查池是否仍超阈值，若是则在状态卡提示「池仍超阈值，可在 X 分钟后再次蒸馏」（不自动连锁跑，
  防止一次大池连烧多份预算）。

---

## 5. 数据模型

```
~/.ginno/memory/
├── pool/<ts>.jsonl            # 现状不变
├── drafts/
│   ├── <draft_id>.md          # 草稿正文（draft_id = 秒级时间戳 + 4 位随机）
│   └── <draft_id>.json        # meta：见下
├── runs.jsonl                 # 追加式运行日志（对齐 usage_store 的形态）
└── draft.md / draft.json      # 旧单槽：启动时若存在则迁成 drafts/ 一条后删除
```

`drafts/<id>.json`：

```jsonc
{
  "id": "1790823827-a1b2",
  "created_at": 1790823827.5,
  "trigger": "auto",              // auto | manual | schedule
  "provider": "prov_387fc06b",
  "model": "glm-5.3",
  "pool_entries": 30,
  "pool_cutoff": 1790823798.2,    // 应用时清理到此时间点为止的池
  "budget": 3000,
  "chars": 9975,
  "previous_sha1": "…",           // 冲突校验（沿用）
  "status": "pending",            // pending | applied | discarded | expired
  "resolved_at": null,
  "expired_reason": null
}
```

`runs.jsonl` 一条：

```jsonc
{
  "ts": 1790823827.5, "run_id": "r-1790823827",
  "trigger": "auto", "status": "success",   // success | failed | skipped
  "reason": null,                            // skipped/failed 的原因码
  "duration_ms": 18420,
  "provider": "prov_387fc06b", "model": "glm-5.3",
  "input_tokens": 12930, "output_tokens": 4102,
  "cache_read_tokens": 0,
  "pool_entries": 30, "draft_id": "1790823827-a1b2", "chars": 9975
}
```

保留：`runs.jsonl` 保留 90 天（与 usage 对齐，启动时裁切）；草稿按 `draft_keep` 保留。

---

## 6. API 变更

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/memory` | **扩展**：增加 `status`（水位/待审/上次 run/节流状态）、`drafts_summary` |
| GET | `/api/memory/drafts` | 列表（含已应用/已丢弃/已过期，分页） |
| GET | `/api/memory/drafts/{id}` | 详情（draft/previous/diff/meta） |
| POST | `/api/memory/drafts/{id}/apply` | 应用（body 可带 `content` 编辑版、`force`、`truncate`） |
| POST | `/api/memory/drafts/{id}/discard` | 丢弃 |
| DELETE | `/api/memory/drafts?expired=1` | 批量清理过期 |
| GET | `/api/memory/pool?limit=&offset=` | 采样浏览（返回摘要，不返回全文） |
| DELETE | `/api/memory/pool` | 清空（需 `confirm` 字段） |
| GET | `/api/memory/runs?limit=` | 运行历史 |
| POST | `/api/memory/runs/{run_id}/retry` | 手动重试（绕过节流） |
| GET | `/api/memory/usage?days=7` | 蒸馏用量聚合（读 usage 日志 `source=memory`；供状态卡） |
| POST | `/api/memory/summarize` | 保持（手动触发），返回值扩为 run 对象 |

**兼容**：`/api/memory/draft`（单槽）保留为「最新待审草稿」的别名一个版本周期，前端切换后删除。

---

## 7. 用量统计（本次明确要求）

### 7.1 记账点

`memory/summarize.py` 的 `create_draft` 在 `model.ainvoke` 之后：

```python
from .. import usage as usage_mod, usage_store
u = usage_mod.extract_usage(response) or {}
usage_store.record(
    input_tokens=u.get("input_tokens") or 0,
    output_tokens=u.get("output_tokens") or 0,
    cache_read_tokens=u.get("cache_read_tokens") or 0,
    cache_creation_tokens=u.get("cache_creation_tokens") or 0,
    provider=<cfg.provider>, model=<实际模型名>,
    source="memory",
    turn_id=run_id,          # 归因到 Run（usage_store 无 run 字段，复用 turn_id）
    ok=..., error=..., latency_ms=...,
)
```

- `session_id` 留空：蒸馏是全局操作，不属于任何会话；**会话页签不应出现"记忆"行**（符合预期）。
- 失败也要记一行（`ok=False`）：失败的 token 同样计费，且能解释"为什么今天用量比对话多"。
- `runs.jsonl` 与 usage 的关系：runs 是**产品视图**（含 skipped、产出字符），usage 是**计费事实**。
  两者用 `run_id` 关联，不做冗余对账。

### 7.2 前端呈现

- **概览 → 来源分布**：`memory` 已在正典配色（`lib/sourceColor.ts` `#38bdf8`）与文案
  （`messages/*/settings.json` → `settings.usage.overview.source.memory` = 「记忆」）里，**记账一接线即自动出现**，
  无需改 UI。
- **请求日志**：来源筛选下拉需补全选项（见 §8.2），可直接按「记忆」过滤。
- **记忆工坊状态卡**：近 7 天蒸馏次数 / token 走 `GET /api/memory/usage`（后端聚合，前端不算）。
- **运行历史**：每行展示该 run 的 input/output token（写入时从 `extract_usage` 取，与 record 同源）。

### 7.3 不为蒸馏做的事

- 不新建一张"记忆用量"存储表——避免两套事实。
- 不做成本金额估算（沿用 `usage-stats-design §1.3` 的非目标）。

---

## 8. 用量记账缺口审计（附加要求）

### 8.1 未记账的 LLM 调用

| # | 调用点 | 代码位置 | 真实 token | 建议 `source` | 优先级 |
|---|---|---|---|---|---|
| 1 | **记忆蒸馏** | `memory/summarize.py:300` | ✅ 大 | `memory` | **P0（本次）** |
| 2 | 会话标题生成 | `title_gen.py:162` | ✅ 小 | `title`（新增来源） | P1 |
| 3 | 子代理计划 | `subagent_plan.py:167` | ✅ 中 | `subagent_plan` 或并入 `subagent` | P1 |
| 4 | 子代理门控 | `subagent_scheduler/gate.py:324,406` | ✅ 中 | `subagent_gate` 或并入 `subagent` | P1 |
| 5 | 团队生成 | `api/team_generate.py:133` | ✅ 中 | `team`（新增来源） | P1 |
| 6 | 联网检索探测 | `providers.py:670` `search_probe` | ✅ 中 | `probe`（设计已约定，从未接线） | P0（顺手） |
| 7 | 配置验证 ping | `providers.py:494` `_verify_config` | ⚠️ 极小（`max_tokens=1`） | `probe` | P2 |
| 8 | Jev / Laya 分类路由 | `classifier/jev.py:181`、`classifier/laya.py:224` | ❌ 响应不含 token | 记 0 token + latency 的调用审计行 | P2 |
| 9 | 模型列表拉取 | `providers._list_models` | ❌ 无生成 | 不记 | — |

已正确记账（无需改动）：主对话 `api/stream/engine.py:723`（`chat` / `goal` / `subagent` / `channel_resume`）、
压缩 `compaction.py:286`（`compaction`）、定时任务 `scheduler.py:948`（`schedule`）、工作流节点
`workflows/nodes/agent_helpers.py:133`（`workflow`，agent/llm/extract/supervisor/synthesis 全覆盖）、
外部 CLI 代理 `tools/external_agent.py:654`（`external`）、mods `mods/ops.py:472`（`mods`）、
LLM 分类器 `classifier/llm.py:142`（`classifier`）。

### 8.2 已发射但 UI 未登记的来源

后端 `_norm_source` 是"自然统计"（不白名单），但前端 `OverviewPanel.tsx:213-227` 的 `SOURCE_META`
与 catalog `settings.usage.overview.source` 只有 10 个键。日志里**已经出现**、但没有登记的是
**`schedule`、`mods`**；**`classifier`、`channel_resume`** 是代码里会发射、日志中尚未出现同样未登记。
它们现在都以英文原名 + hash 配色显示（能看，但突兀）。

> 数据现状（2026-10-25 全量日志）：`chat 2437 / other 1234 / schedule 645 / subagent 487 /
> workflow 183 / mods 6 / external 3 / compaction 1`；`memory`、`kb`、`probe`、`title`、`team` 为 0。
> 「其他」的 1234 行绝大多数是 2026-10-13 前 `subagent` 被强转 `other` 的历史数据（见
> `usage_store.py` 头部注释），不是本次审计缺口，不必回填。

建议：
- `SOURCE_META` / catalog / `sourceColor.CANONICAL_FG` 三处一起补：`schedule`「定时任务」、`mods`「插件」、
  `classifier`「分类器」、`channel_resume`「渠道续跑」；顺带补本期新增的 `memory` 系列（已有）与
  `title`「标题生成」、`team`「团队生成」。
- 保留 hash 兜底，避免以后每加一个来源就要改前端（现有设计意图，不要回退成白名单）。

### 8.3 观测口径缺口

- **跳过/失败没有记录**：自动路径的 `skipped`（`draft_pending` / `throttled`）现在只存在于代码分支里，
  用户侧不可见——这正是 §1.2 停摆 24 天没被发现的直接原因。`runs.jsonl` 落地后补齐。
- **额度压力不可见**：池水位、预算占用没有趋势。状态卡先给当前值，趋势列为 P2。

---

## 9. 分期与验收

### P0（先止血，1–2 天）

1. 蒸馏记账（§7.1）+ `probe` 记账
2. 草稿过期策略（`draft_ttl_days`，默认 7）+ 挤出策略（上限 3）
3. 右栏面板：状态条 + 滞留告警 + 「丢弃并重跑」
4. 前端来源登记补全（§8.2）

**验收**：仓里造 30 条池 → 自动草稿 → 不审 → 7 天后自动 `expired` → 下一轮自动蒸馏恢复；
usage 日志出现 `source=memory` 行；概览来源分布出现「记忆」桶。

### P1（可管理，3–5 天）

5. `drafts/` + `runs.jsonl` 数据层与迁移
6. 记忆工坊页：状态卡 / 草稿队列 / 池 / 运行历史
7. API 表（§6）与旧接口兼容别名
8. 超预算拦截 + 按主题拆分草稿

**验收**：可以从 UI 完成"看池 → 手动蒸馏 → 编辑 → 应用 → 查历史 → 看该次 token"全链路；
apply 后池按 cutoff 正确清理；`MEMORY.md` 被外部改动时冲突提示仍生效。

### P2（自动化，2–3 天）

9. 夜间窗口（`memory_dream` 内置任务）
10. 运行历史里的失败重试 / 节流状态
11. 标题生成、子代理计划/门控、团队生成的记账（§8.1 #2–#5）
12. 池水位与预算趋势

---

## 10. 风险与兼容

| 风险 | 处理 |
|---|---|
| 旧单槽草稿丢失 | 启动迁移：`draft.md`/`draft.json` → `drafts/<id>`（status=pending），保留 `previous_sha1` |
| 单槽 → 队列语义变化 | `create_draft` 返回值保持向后兼容（额外带 `draft_id`）；旧别名接口继续可用一个版本 |
| 自动路径变"贪跑" | 应用后不自动连锁；`_DISTILL_LOCK` + 冷却期不变 |
| 隐私 | 池浏览只返回 200 字摘要；usage 只记计数（沿用 §4.4） |
| 队列被大量草稿占盘 | `draft_keep`（默认 20）+ 过期清理 |
| 夜间任务与用户手动冲突 | 共享 `_DISTILL_LOCK`，后到者返回 `skipped: in_progress` 并记 run |

---

## 11. 待裁定

1. **管理页位置**：`/settings/knowledge/dream`（本文推荐，与知识库同组）还是独立一级菜单「记忆」？
2. **来源粒度**：子代理计划/门控并入 `subagent`（行数少、口径简单）还是拆 `subagent_plan` / `subagent_gate`
   （可定位慢在哪一步）？
3. **夜间默认**：03:00 固定，还是"用户本地时间凌晨随机器空闲"？本期建议固定时刻（简单、可预期）。
4. **超预算策略**：硬拦截（必须编辑到预算内）还是提供一键截断？本文推荐"默认拦截 + 一键按主题截断"。

> 裁定结果（2026-10-25 实施时定稿）：① 入口落在 **智能体分组**（用户指定 `设置 › 智能体 › 记忆`），
> 比本文推荐的 knowledge 组更贴近"智能体共享记忆"的心智；② 来源粒度本期**未拆**，仍共用 `subagent`；
> ③ 夜间固定时刻、默认关闭；④ 默认拦截 + 「按预算截断后应用」（按行边界截断，不做按主题拆分）。

---

## 12. 实施状态（2026-10-25）

### 已落地

| 项 | 位置 |
|---|---|
| 草稿队列（`drafts/<id>.md` + `.json`，状态 pending/applied/discarded/expired） | `memory/summarize.py` |
| TTL 过期 + 上限挤出 + 已解决草稿裁剪（`draft_ttl_days=7` / `draft_max_pending=3` / `draft_keep=20`） | 同上，`expire_stale_drafts()` |
| 旧单槽 `memory/draft.md` 自动迁移（启动 + 首次访问） | `migrate_legacy_draft()` |
| 运行日志 `~/.ginno/memory/runs.jsonl`（success / failed / skipped+reason），启动裁切 90 天 | `record_run/run_history/trim_runs` |
| 蒸馏用量记账 `source="memory"`（成功与失败都记，`turn_id=run_id`） | `create_draft` |
| 联网探针记账 `source="probe"` | `providers.search_probe` |
| "已生效的记忆" 投影（分段、字符数、注入预算、角色私有记忆、注入原文预览） | `memory/status.py` |
| 记忆 API：status / drafts CRUD / pool 浏览与清空 / runs / usage / 编辑 MEMORY.md / 删段 | `api/memory.py` |
| 启动与每轮清扫（迁移 + 过期 + 运行日志裁切，10 分钟节流） | `server.py` lifespan、`api/stream/effects.py` |
| 夜间窗口（内置，不进 schedules.json） | `scheduler._maybe_nightly_dream` |
| 前端：设置 › 智能体 › **记忆** 页（告警 / KPI / 已生效的记忆 / 草稿队列 / 运行历史 / 池 / 设置） | `components/settings/MemorySettings.tsx` |
| 超预算拦截 + 「按预算截断后应用」 | `MemoryDraftModal` + `apply_draft(truncate=)` |
| 知识库页的蒸馏设置迁走，留跳转提示 | `KnowledgeSettings.tsx` |

### 与本文的差异（有意为之）

- **同一时刻只保留 1 份待审草稿**：队列里的多份来自历史（applied/discarded/expired），`create_draft`
  遇到 pending 仍然复用而不是再烧一次 LLM。`draft_max_pending` 只作为脏数据的抬升保护。
- 右栏记忆面板保持轻量，**没有**加状态条/滞留横幅——诊断信息集中在设置页（用户入口二选一，避免两处维护）。

### P2 余项（2026-10-25 已全部实现）

| 项 | 实现 |
|---|---|
| 运行历史「失败重试」 | `POST /api/memory/runs/{run_id}/retry`（绕过自动路径冷却，手动触发一次蒸馏）；失败行内联「重试」按钮 |
| 自动路径节流状态 | `GET /api/memory` 返回 `throttle{until,active}`，运行历史页脚显示「冷却至 HH:MM / 正常」 |
| 标题生成记账 | `title_gen` → `source="title"` |
| 子代理计划 / 收尾记账 | `subagent_plan` → `subagent_plan`；`subagent_scheduler/gate`（压缩摘要、子会话命名两处）→ `subagent_gate` |
| 团队生成记账 | `api/team_generate` → `source="team"` |
| 池水位与预算趋势 | 每日快照 `~/.ginno/memory/state.jsonl`（同日覆盖），`GET /api/memory/trend?days=`；页面「近 30 天」双折线（池 vs 阈值、记忆占用 vs 注入预算），等比例 + 虚线基准 |
| 按主题拆分草稿 | `split_draft()` + `POST /api/memory/drafts/{id}/split`；审阅弹层超预算时出现「按主题拆分」勾选，原草稿置 discarded，新草稿不额外调用模型 |
| 来源登记补全 | `sourceColor` 正典色 + `USAGE_SOURCES` 顺序表；概览「来源分布」与请求日志筛选项补齐 `schedule / mods / classifier / channel_resume / title / team / subagent_plan / subagent_gate` |

记账入口统一收敛到 `usage.record_message(message, source=…)`：新增一个后台调用来源只需一行。

### 仍然没有做的

- 蒸馏失败的**自动**重试（避免对同一错误连续烧 token；失败行提供手动重试）
- 跨设备/云端用量汇总（本设计始终是单机本地）
