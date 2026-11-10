# Instincts（本能规则沉淀）技术设计 — 错误 → 带置信度的行为规则 → 注入 → 演化

> 状态：**设计稿**（2026-11-10，源自特性雷达 11-10 候选 3；原型见 [`design-mocks/instincts-prototype.html`](../design-mocks/instincts-prototype.html)，
> 深研报告另存 Obsidian `Research/2026-11-10-ginno-instincts-技术方案.md`，本文档为其仓库版并含后续增补）。
> 关联：[`dream-mode-design.md`](dream-mode-design.md)（记忆蒸馏管线与草稿审阅范式，§14.5 协同）、
> [`distill-redesign-design.md`](distill-redesign-design.md)（会话蒸馏 kind=skill，即 evolve 落点）、
> [`memory-kb-gates-implementation-notes.md`](memory-kb-gates-implementation-notes.md)（质量门控 G1–G3）、
> [`knowledge-and-wiki-design.md`](knowledge-and-wiki-design.md)（注入管道同层）。
> 参照实现：ECC continuous-learning-v2（<https://github.com/affaan-m/ECC>）。

## 1. 背景与目标

**问题**：agent 在同一个坑里摔第二次——上次被用户拒绝的工具用法、被否掉的方案、踩过的配置坑，
新会话一概不记得。Ginno 已有 wiki 注入（用户策展知识）与 memory pool / Dream 蒸馏（事实性结论），
但没有「行为规则」这一层：短小、带置信度、可随使用自动升降级的操作性约束。

**目标**：turn 中出现纠偏信号（权限被拒 / 用户纠偏 / 重试自救）时，自动沉淀为原子「本能」
（instinct）条目；新会话按 置信度×相关性 注入 top-N；反复验证可演化为正式 skill；
错误条目被纠正后快速降级、失效自动清理。全流程本地文件、用户可审计可编辑可删除。

**一句话边界**：

| 层 | 沉淀什么 | 既有机制 |
|---|---|---|
| wiki 注入 | 用户策展的知识/事实 | `knowledge/injection.py`（已上线） |
| memory pool / Dream | 会话的事实性结论/偏好 | `memory/pool.py` + distiller（已上线） |
| **instincts（本设计）** | **操作性行为规则「别再这么做/该这么做」** | 新增 |
| skills | 完整工作流能力 | `skills/loader.py`（instinct 的演化终点） |

## 2. 与 ECC 参照实现的差异（Ginno 的架构优势）

ECC continuous-learning-v2 的核心教训：v1 用 skill 观察 session 触发率仅 50-80%；v2 改用
PreToolUse/PostToolUse shell hooks 外挂才做到 100% 确定性捕获。

Ginno 是完整 runtime（LangGraph 进程内），**观察层先天 100% 可靠且语义更丰富**：
权限决策、重试、用户消息都是进程内结构化事件，无需 shell hooks 外挂。映射：

- ECC hooks 观察层 → Ginno 的 graph permission 节点 + engine 收尾钩子（§5）
- ECC Haiku observer 进程 → Ginno detached 后台任务（复用 `mods/events.py` spawn 模式）
- ECC SessionStart hook 注入 → Ginno `build_turn_context` parts 注入（与 wiki 同层）
- ECC git-remote 哈希项目隔离 → Ginno 直接用现有 project slug（单机本地优先）

沿用 ECC 的：置信度四档语义、升降级规则、scope 决策表、evolve 聚类、
「导出只导 instincts 不导原始 observations」的隐私边界。

## 3. 核心概念与生命周期

instinct = 一条原子行为规则：**trigger（何时适用）+ behavior（该做什么）+ 置信度**。

```
            捕获(观察事件)              observer 分析
  turn ──────────────────> observations.jsonl ──────────> pending (0.3)
                                                          │ 用户确认 / 再次观察强化
                                                          v
                              active (0.3 → 0.9 随验证升降)
                                │            │            │
                        聚类起草 v      被纠正 v      长期未观察 v
                            evolved       pruned(冻结/降档)  pruned
                        (=SKILL.md 落地，条目保留溯源)
```

状态：`pending → active → evolved | pruned`；`pruned` 不物理删除（移入 `pruned/`，可恢复），
`evolved` 条目保留并链接到生成的 skill。

## 4. 数据模型与存储布局

### 4.1 instinct 条目（YAML，一文件一条，id 即文件名）

```yaml
id: ask-before-rm-cache
trigger: "用户要求清理缓存目录或执行删除类命令时"
behavior: "先列出将删除的文件并请用户确认，再执行；不要直接 rm"
confidence: 0.7
domain: tool-usage        # tool-usage | code-style | workflow | security | user-preference
scope: project            # project | global
project_slug: workbuddy-bench
source: permission-deny   # permission-deny | user-correction | retry-recover | manual
status: active            # pending | active | evolved | pruned
evidence:
  observed: 3             # 独立重复观察次数
  applied: 2              # 注入后被实际采纳次数
  uncorrected: 2          # 采纳后未被纠正
  corrected: 0            # 采纳后被用户纠正
  last_seen: 2026-11-10T08:30:00+08:00
created: 2026-11-08T...
updated: 2026-11-10T...
evolved_to: null          # evolve 后填 skill name
```

选 YAML：与 SKILL.md frontmatter 同族（runtime 已有 YAML 解析依赖），用户可手改，git 可 diff。

### 4.2 目录布局（`paths.py::ensure_layout()` 新增一段；下文 `$GINNO_HOME` 即 Ginno
数据目录，`paths.py::home()` 解析，默认用户主目录下 `.ginno`，可环境变量覆盖）

```
$GINNO_HOME/instincts/
├── global/{active,pending,pruned,evolved}/<id>.yaml
├── projects/<slug>/          # 结构同 global/，按现有 project slug 隔离
├── observations.jsonl        # 观察事件流（含 project 字段，全局一份）
└── observer-state.json       # 上次分析位点 + 时间，防重复分析
```

### 4.3 观察事件 schema（observations.jsonl 每行）

```json
{"ts": "...", "type": "tool_denied", "tool": "bash", "args_repr": "rm -rf build/", "static": false,
 "session": "...", "project": "ginno", "turn": "..."}
{"ts": "...", "type": "correction_candidate", "before": "...", "after": "...",
 "signal": "negation|redirect|deny-same-turn", "session": "...", "project": "ginno"}
{"ts": "...", "type": "retry_recovered", "error_class": "429|timeout|zlib", "action": "..."}
```

`args_repr` 必须脱敏后落盘（复用 `memory/pool.py` 的 sanitize 思路：去除 token/密钥模式）。

## 5. 捕获管道（三层事件源）

### L1 确定性事件（hooks 级，100% 可靠，P0）

| 事件 | 捕获点（现有代码） | 动作 |
|---|---|---|
| 用户拒绝权限 | `graph.py` permission 节点 resume 后 `decision=="deny"` 分支（约 :655）与静态 deny 分支；`api/stream/ws.py:1052-1078` `permission_response` 入口 | append `tool_denied` 观察 |
| 重试自救成功 | `graph.py` in-turn 重试 / `rate_limited_backoff` 成功路径 | append `retry_recovered` |
| 用户撤销 | 撤销协议入口（undo 已落地） | append `user_reverted` |

实现形态：`instincts/capture.py` 暴露 `observe(event: dict) -> None`（幂等追加 JSONL），上述分支各插
一行调用。异常全吞（观察失败绝不影响主 turn，对齐 `maybe_compact_history` 容错风格）。

### L2 纠偏检测（启发式预筛 + 小模型判定，P1）

用户纠偏**无现成钩子**（classifier 层是模型路由/工具选择，不承担此职）。两段式：

1. **廉价预筛**（确定性，`instincts/prefilter.py`）：用户消息含否定/纠偏词（"不对/别/不要/撤销/
   我说的不是"）、紧跟工具失败后的指令改向、同 turn 出现 permission deny——命中才进入 2；
2. **小模型判定**（`instincts/observer.py`）：(before 摘要, after 指令, 工具上下文) 喂给配置的
   observer 模型，输出：是否构成可沉淀规则 + trigger/behavior 草稿 + domain/scope 建议。

### L3 会话级回顾（P2，可选）

`session.compact` 事件触发时对被压缩段做回顾性提取，兜底 L2 预筛漏掉的纠偏。低优先级。

## 6. Observer 分析器

- **触发**：turn 收尾钩子（`api/stream/engine.py` 收尾分支，与 `_maybe_auto_distill` 同位置）检查：
  新观察 ≥ `min_observations`（默认 20）且距上次分析 ≥ 5 分钟 → spawn detached 分析任务
  （复用 `mods/events.py::spawn_turn_complete` detached spawn 模式）。
- **职责**：读 `observer-state.json` 位点之后的观察 → 逐条判定：
  - 模板化确定性规则（「工具 X 参数模式 Y 被拒 N 次」）**直接规则生成，不调 LLM**；
  - 纠偏类观察调 LLM 起草 trigger/behavior；
  - 与既有条目匹配（trigger 语义同）→ 强化（observed+1）而非新建；
  - 与既有条目矛盾（trigger 同、behavior 反）→ 标记 `conflict`，冻结待人工。
- **scope 决策**（照 ECC 表）：语言/框架/目录约定 → project；安全/通用实践/工具习惯 → global。
- **usage 记账**：observer 的 LLM 调用按 `source="instincts"` 记入 usage 台账
  （照 Dream 蒸馏 `source="memory"` 桶的先例，概览「来源分布」自动独立成桶）。
- **成本门控**：无新纠偏观察时零 LLM 调用；活跃日常预估 <10 次/天小模型调用。
- **防停摆**（Dream 事故教训，见 dream-mode-design.md §1.2：单草稿 early-return 曾致 24 天停摆）：
  pending 队列天然多槽不互斥；observer-state 每次成功后前进、失败保留位点可重试；
  pending 滞留 >14 天自动过期转 pruned 并在管理页提示，不阻塞后续分析。

## 7. 置信度引擎（确定性规则，LLM 不参与打分）

四档语义（照 ECC）：

| 分值 | 含义 | 注入行为 |
|---|---|---|
| 0.3 | 试探 | 注入但标注「低置信，用户当前指令优先」 |
| 0.5 | 适度 | 正常注入 |
| 0.7 | 强 | 注入 + 排序优先（默认注入门槛） |
| 0.9 | 近确定 | 核心行为，排序最高 |

升降级（全部确定性增量，落盘可审计）：

- 升：独立重复观察 +0.1/次（封顶 0.9）；注入后被采纳且未被纠正 +0.05；多源一致（不同 session、
  不同 source 类型命中同一规则）+0.1；
- 降：注入后被用户明确纠正 −0.2（纠偏信号反查：该 turn 触发了 L1/L2 捕获且对象与条目相关）；
  `decay_days`（默认 90）未观察 −0.1（observer 顺带执行衰减扫描）；
- <0.3 → 自动转 `pruned`；conflict 状态不参与升降级。

「注入后被采纳」的 P1 简化口径：注入了该条目的 turn 中，agent 后续工具调用/回复未再触发同类捕获
事件，即计一次 uncorrected——不追求精确归因，统计意义足够。

## 8. 注入管道（复用 wiki 管道范式）

- **挂点**：`graph_prompts.py::build_turn_context` parts 列表新增
  `wrap_context_section("instincts", build_instinct_context(query, project))`——与
  `injected_wiki`/`team_context` 完全同范式（显式设计的最小扩展点）。
- **层级**：volatile turn-context 层（每轮重建），**不进稳定 system 层**——保 prefix cache。
- **排序**：`confidence × relevance`。relevance = trigger 与 query 词汇重叠 + domain 与当前
  工具集/项目栈匹配（ECC relevance ranking 思路，纯本地计算）。
- **格式**（紧凑，硬上限 ~600 tokens，零命中不注入）：

```
<instincts>
历史会话沉淀的行为规则（按置信度排序；与用户当前指令冲突时以用户为准）：
- [0.8] 用户要求清理缓存目录时：先列出将删文件并确认，再执行
- [0.7] 本项目测试必须带 --extra docs，否则依赖缺失 50+ 用例红
</instincts>
```

- **注入防御**：instinct 正文（机器生成内容）过 `knowledge/injection.py` 的
  `_INJECTION_PATTERNS`（wrapper 伪造防护），命中即拒注该条并记 `injection_blocked`。
- **用量记录**：对齐 wiki 注入的 `usage.record_injected` 口径。

## 9. 演化与治理

| 操作 | 触发 | 实现 |
|---|---|---|
| evolve | 管理页按钮 / `/instincts evolve` | **复用 `api/distill.py` 现成落点**：distill 的 `kind=skill` 已实现「会话→SKILL.md」模板化落地（`_skill_md` 收集 steps/tools、防重名、mark 溯源），evolve 只是把输入换成「同 domain 且 confidence≥0.8 的 active 条目集合」→ LLM 起草 SKILL.md（frontmatter 带 `trigger: model-invocable`）→ 复用其落地/防重名/溯源逻辑写到 `$GINNO_HOME/skills/<name>/`；原条目 `status: evolved` + `evolved_to` 溯源 |
| promote | 管理页手动；同 id 出现在 2+ project slug 时给建议 badge | 文件从 `projects/<slug>/active/` 移 `global/active/`，scope 改写 |
| prune | 衰减 <0.3 自动 / 手动删除 | 移 `pruned/`（软删）或物理删除（手动） |
| export/import | 管理页 | **只导出 instinct YAML，绝不导 observations.jsonl**（隐私边界照 ECC） |
| 冲突处理 | observer 检测 | 管理页冲突区展示双方条目，用户二选一或合并 |

## 10. 配置与 API

### 10.1 settings.json 新段（照 `KnowledgeConfig` 范式：types.py dataclass + load/save + `paths._DEFAULT_SETTINGS` 种子）

```json
"instincts": {
  "enabled": true,
  "capture": true,
  "detect_corrections": true,
  "observer_model": "",
  "min_observations": 20,
  "inject_enabled": true,
  "inject_min_confidence": 0.7,
  "inject_max_n": 6,
  "inject_token_budget": 600,
  "decay_days": 90
}
```

`observer_model` 空串 = 跟随主模型；推荐配置 flash/Haiku 级。`GET/PUT /api/settings` 全文档透传
（`api/config.py:224-262`），新段自动可见，无需改 settings 端点；设置页在
`components/settings/` 新增 instincts 组（`settings/[group]` 路由现成）。

### 10.2 REST（新 `api/instincts.py`，`server.py` include_router 注册）

```
GET    /api/instincts?scope&domain&status&min_confidence   列表
GET    /api/instincts/{id}
PUT    /api/instincts/{id}                                 编辑（含手动调 confidence）
DELETE /api/instincts/{id}
POST   /api/instincts                                      手动创建
POST   /api/instincts/{id}/promote
POST   /api/instincts/evolve                               聚类起草（同步返回草稿，确认后落盘）
POST   /api/instincts/prune                                触发衰减扫描
GET    /api/instincts/observations?limit                   观察 feed（只读）
GET/POST /api/instincts/export | /import
```

## 11. Web 管理页（照 kb / scheduled 页四层范式）

| 层 | 文件 |
|---|---|
| 路由页 | `apps/web/src/app/instincts/page.tsx`（薄壳） |
| 组件 | `apps/web/src/components/instincts/{InstinctsPage, InstinctCard, ObservationFeed, EvolveDialog, shared}.tsx` |
| API client | `apps/web/src/lib/api/instincts.ts`（`lib/runtime.ts` re-export）+ `lib/types.ts` 类型 |
| 后端 | `api/instincts.py`（见 §10.2） |

页面布局照 `app/kb/page.tsx`：`px-8 py-7` 主区、图标+标题行（lucide `Zap`，`text-violet`）、
副标题、pill 统计横排、页内 tabs（`border-b-2 border-violet` + count：规则/待确认/冲突/观察/演化/注入预览）。
入口加在 `components/shell/SidebarFooter.tsx` 页脚导航（团队/连接器/定时任务/后台任务/KB/工作流/
**本能**/设置），pending 计数角标照后台任务页范式。视觉细节以
[`design-mocks/instincts-prototype.html`](../design-mocks/instincts-prototype.html)（v2，已对齐真实 UI
token 与类名）为基准。

## 12. 落层总览

| 层 | 变更 | 文件 |
|---|---|---|
| runtime 新增 | instincts 模块族 | `instincts/{__init__,types,capture,prefilter,observer,confidence,inject,evolve,store}.py` |
| runtime 修改 | 拒绝/重试捕获插桩 | `graph.py`（permission deny 分支、重试成功路径各 +1 行） |
| runtime 修改 | turn 收尾 observer 门控 | `api/stream/engine.py` 收尾分支（与 `_maybe_auto_distill` 同位置 +1 调用） |
| runtime 修改 | 注入挂点 | `graph_prompts.py::build_turn_context` parts +1 |
| runtime 修改 | 目录种子 | `paths.py::ensure_layout` + `_DEFAULT_SETTINGS` |
| runtime 新增 | API + usage 源 | `api/instincts.py`、`server.py` 注册、usage `source="instincts"` |
| web 新增 | 管理页四层 + 侧栏入口 | 见 §11 |

## 13. 里程碑（总估 24-40h，拆三期）

### P0 最小闭环（~10-14h）：确定性规则先跑起来
- L1 捕获（tool_denied / retry_recovered）+ observations.jsonl 落盘（脱敏）；
- 存储 YAML + **模板规则生成**（不调 LLM）：同类 `tool_denied` 观察达 3 次 → 模板化生成 pending 条目；
- 注入管道（`build_instinct_context` 挂进 `build_turn_context`，置信度排序 top-N + 防御 + 预算）；
- 配置段 + 只读管理页（列表 + 手动增删改）。
- **验收**：同一项目拒绝 3 次同类命令后，新会话注入该规则且可关闭。

### P1 完整学习闭环（~10-16h）
- L2 纠偏检测（预筛 + observer 小模型判定）；
- observer detached 任务化 + 位点管理 + 冲突检测 + pending 滞留过期；
- 置信度引擎完整（applied/uncorrected 归因、衰减扫描、corrected 降档）；
- 管理页完整（pending 确认流、冲突区、观察 feed）+ relevance 排序。

### P2 演化与生态（~8-12h）
- evolve 聚类起草 SKILL.md → distill 落点落地；
- promote 建议-badge、L3 compact 回顾提取、export/import、usage 注入口径并轨。

## 14. 风险与开放问题

1. **误学习闭环**（最大风险）：错误 instinct 注入 → agent 行为跑偏 → 反被统计为 uncorrected 强化。
   缓解：0.3-0.5 档注入必带「用户当前指令优先」声明；corrected 降档要快（−0.2）；conflict 冻结；
   P0 只注入 ≥0.7 条目（即至少 4 次独立观察验证）。
2. **注入预算协调**：instincts / wiki / memory 三块同在 turn-context 层，各自有预算但没有全局
   分配器。开放问题：是否引入注入块总预算 + 优先级仲裁（建议观察 P1 数据后再定，先各自硬上限）。
3. **观察噪声**：`args_repr` 模板化生成 trigger 的泛化度差。P0 接受（宁缺毋滥），P1 由 observer
   LLM 负责泛化改写 trigger。
4. **observer 成本**：小模型调用已门控；L2 预筛误报率高时纠偏判定调用会涨，上线后看 usage 数据校准。
5. **与记忆抽取（Dream/distill）机制的关系**：Ginno 记忆抽取有三套可协同——① memory pool 自动蒸馏
   （`memory/pool.py` + `effects.py` `_maybe_auto_distill`）：与 instincts 同构骨架
   （capture→pool→distill→inject），挂同一个收尾分支、共享 sanitize/注入防御（pool 的
   `_INJECTION_PATTERNS` 即 knowledge/injection 的 canonical 列表，instincts 同列表）；分工 =
   MEMORY.md 蒸馏**事实性结论**、instinct 沉淀**行为规则**，两池分开、注入同层。② Dream 草稿审阅
   （`/api/memory/drafts` + MemoryDraftModal 的 diff/应用/丢弃/过期回收）：instincts 的 pending
   确认流与其同构，实现时对齐其队列 API 形态（多槽、滞留过期、失败不停摆——见 dream-mode-design.md
   §1.2 事故教训，已吸收进 §6）。③ 会话手动蒸馏（`api/distill.py`，kind=skill/kb）：kind=skill 的
   「会话→SKILL.md」落地即 instincts evolve 的现成实现（见 §9）。usage 归因：Dream 用
   `source="memory"`，instincts 用 `source="instincts"`，用量概览各自独立成桶。
6. **多 agent 场景**：子代理/worker turn 是否捕获观察（量大噪声高）？P0 仅主会话，子代理观察开关留 P2。

## 15. 参考来源

- ECC continuous-learning-v2 完整机制：
  <https://raw.githubusercontent.com/affaan-m/ECC/main/skills/continuous-learning-v2/SKILL.md>
- ECC 仓库：<https://github.com/affaan-m/ECC>
- Ginno 代码挂点：2026-11-10 只读调研（graph.py permission 节点 :622-680、engine.py 收尾
  :1150-1200、graph_prompts.build_turn_context :343-373、knowledge/injection.py、skills/、
  memory/pool.py、dream-mode-design.md、distill.py kind=skill、paths.ensure_layout、
  kb/scheduled 页四层范式、SidebarFooter 页脚导航）。
- 需求侧证据：特性雷达 2026-11-10 候选 3（Obsidian Raw/特性雷达-2026-11-10.md）。
