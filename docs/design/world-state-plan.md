# WorldState · 上下文工程方案

> 状态:**已实施**(维度 A/B/C/D/E 全部落地,含 chip 显示级别表与 token 用量展示;按产品决策暂缓 workspace(A1)与记忆预算(A5)/自动蒸馏(A5b),GINNO.md 由 Agent prompt 承载) ｜ 参考:OpenAI Codex harness 源码分析(2026-08 快照) ｜ 涉及:`packages/runtime`(主)、`apps/web`(chip 与用量展示)
> 一句话定位:**把"模型该知道的世界"结构化、可对比、变更显式化,让 Ginno 的每一次模型调用既正确又便宜。**

---

## 0. 实施状态(2026-08-04)

| 计划项 | 状态 | 落点 |
|---|---|---|
| C1 引擎(section/快照/diff/基线持久化) | ✅ | `world_state.py`;基线存 `sessions/<id>.world.json` |
| C2 增量更新消息 | ✅ | `[world state update]` 置于本轮用户消息前(`server._run_stream`) |
| C3 `context.updated` WS 事件 + chip 显示级别表 | ✅ | server 发事件;前端 `ChatStream` 按表过滤(纯 environment=静默),渲染为居中 system 行 |
| A1 environment(无 workspace) | ✅ | `EnvironmentSection`(日期/星期/时区/OS/ginno_home/project) |
| A3 permissions 可见 | ✅ | `PermissionsSection` |
| A4 Agent 切换/prompt 编辑感知 | ✅ | `AgentSection`(GINNO.md 的角色由 Agent prompt 承载,不再新增文件) |
| A6 skills 索引预算 | ✅ | `SkillsSection._budget_index`(`skills_index_max_chars`) |
| A7 MCP 变更感知 | ✅ | `McpSection` |
| A5 记忆预算 / A5b 自动蒸馏 | ⏸ 暂缓 | 产品决策先不做;记忆仍全文注入 + 变更感知已具备 |
| B1 易变内容迁出 system | ✅ | `graph.build_turn_context` → `[turn context]` 尾部消息 |
| B2 system 字节稳定化 | ✅ | `graph.build_stable_system` |
| B3 Anthropic cache_control | ✅ | `graph._system_message`(可经 `context.cache_control` 关) |
| D1/D2 usage 采集 + WS 事件 | ✅ | `usage.py`;`server._stream_graph` 发 `usage` |
| D3/D4 前端用量展示 + 缓存命中率 | ✅ | TopBar 用量 pill(↑/↓ + ⚡缓存%) |
| E1 token 计数 | ✅ | `tokens.py` |
| E2 工具输出截断 | ✅ | `truncation.py` + `graph._tools_node_factory` |
| E3 历史压缩 + E4 压缩后重注入 | ✅ | `compaction.py` + `context.compacted` 事件 |
| E5 checkpoint 增量快照 | ✅ | `checkpointer.py`(delta 模式 + 写锁;`put_writes` 持久化但暂不暴露) |
| workspace 注入 / F1 / F2 | ⏸ 冻结 | 与 artifacts 方向冲突,待其定案 |
| 测试 | ✅ | 单元 + API + WS e2e + 打包 UI Playwright(真浏览器 chip/usage),`552 passed` |

**新增配置(`settings.context`)**:`world_state`、`cache_control`、`tool_output_max_chars`、`compaction_enabled`、`compact_threshold_tokens`、`compact_keep_turns`、`checkpoint_mode`、`skills_index_max_chars`。默认值见 `world_state._CONTEXT_DEFAULTS`。

**已知限制**:`checkpoint_mode=delta` 下 `put_writes` 已持久化但 `get_tuple` 暂不回传 `pending_writes`(保持改造前的恢复语义);工具输出截断对非字符串 content 不生效。

---

## 1. 背景:从 Codex 学到的机制,与 Ginno 的现状差距

Codex(openai/codex,约 127 万行 Rust)的 harness 用一套 **WorldState 快照 + 增量 diff** 机制管理模型可见的状态:状态切成命名 section,每个 section 维护结构化快照;每轮只对比、只渲染变化部分;变化以增量消息追加进历史末尾,绝不改写前缀——配合 prefix cache,长会话的输入成本降一个数量级,且模型始终被显式告知"世界发生了什么变化"。

对照 Ginno 现状(基于 runtime 全量代码探查):

| 维度 | Codex | Ginno 现状 | 差距 |
|---|---|---|---|
| 状态注入 | section 化 + 快照 diff | `build_agent_system_prompt()` 每次 LLM 调用全量重拼(graph.py:71) | 无结构化,但注入点唯一、改造面小 |
| 环境状态 | 日期/时区/cwd/权限全注入 | **零注入**:模型不知道日期、OS;workspace 在 state 里却从不进 prompt | 纯空白 |
| 变更感知 | diff 增量通知,模型显式知情 | 静默重建:切 Agent、总结记忆、改 prompt 后模型无声看到不同 system | 正确性隐患 |
| 每轮易变内容 | 追加在历史末尾,前缀稳定 | wiki 检索/附加文件/@mention 混在 system prompt 里,每轮必变 | 缓存杀手 |
| 缓存 | 全链路为 prefix cache 服务 | **零 caching 配置**,每轮全量历史重发 | 最大的钱 |
| 历史治理 | compaction 四路纵深 | 无限增长、无 token 计数;checkpoint 单文件已见 60MB | 独立的更大隐患(维度 E) |

两个决定方案形态的事实:
1. Ginno 的 system prompt **每次调用重建、从不进 checkpoint**——状态基线无需持久化重放,每次现算;
2. Ginno 每轮把 `[sys_msg] + 全部历史` 整体重发——**prefix cache 经济学完全成立**,且目前一分红利没吃到。

**取 Codex 三条纪律,不搬其重型机器**(rollout patch 链、fragment 自愈、tool_search 均不适用当前 Ginno):
- 状态 = 命名 section + 结构化快照,变更靠对比检测;
- 变更 = 显式通知(小增量消息),而非静默改写模型的世界;
- 请求前缀只增不改,为 prefix cache 服务。

---

## 2. 总体架构

```
┌─────────────────────────────────────────────────────────────────┐
│ 每次 LLM 调用的请求组装(agent_node)                                │
│                                                                 │
│  [system prompt ── 稳定层,从最新快照渲染,无时间戳/无查询依赖]        │
│  ├─ persona(agent prompt)+ 工具清单 + 使用指引   (静态)           │
│  ├─ <environment>  日期/星期/时区/OS/ginno_home   (当日不变则稳定)  │
│  ├─ <permissions>  特权/审批模式摘要               (切换才变)       │
│  ├─ <skills>       一行式索引(预算化)             (增删才变)       │
│  └─ <memory>       MEMORY.md(预算截断)            (总结后才变)     │
│                     ▲ Anthropic: cache_control 断点打在此处        │
│  [历史消息 ── checkpoint 全部对话]               ▲ 前缀稳定 → 缓存   │
│  │  …其中夹着若干 [world state update] 增量消息(只增不改)           │
│  [本轮尾部消息 ── 每轮新追加,天然不可缓存]                          │
│  ├─ <attached_files>  本轮附加文件摘要       ← 从 system 迁出      │
│  ├─ <mentioned_*>     本轮 @mention          ← 从 system 迁出     │
│  ├─ <injected_wiki>   本轮检索结果            ← 从 system 迁出     │
│  └─ HumanMessage(用户输入)                                        │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ 变更检测(每个 turn 入口 _run_stream + 事件触发点)                   │
│                                                                 │
│  current = WorldState.build(session)    # 读磁盘/设置/注册表        │
│  changes = diff(current, baseline)      # section 级快照对比       │
│  if changes:                                                    │
│      ① 合并为一条 [world state update] 消息,置于本轮用户消息前        │
│      ② WS 推 context.updated 事件 → 前端渲染 chip(§7)             │
│      ③ baseline = current → sessions/<id>.world.json            │
└─────────────────────────────────────────────────────────────────┘
```

与 Codex 的对应:稳定层 system ≈ 初始 render_full;`[world state update]` ≈ render_diff 片段;本轮尾部消息 ≈ contextual user bundle。**区别:基线不进历史、每次调用现算**——LangGraph "system 不入 checkpoint" 现状的自然延伸,resume/time-travel 免费兼容。

---

## 3. 维度 A:状态注入(WorldState Sections)

| # | 条目 | 内容 | 价值 | 量 |
|---|---|---|---|---|
| **A1** | 环境状态 section | 日期/星期、时区、OS、ginno_home、project_slug(**不含 workspace,见 §9 冻结项**) | 修复"模型不知道今天几号"的空白;TODO/周报/时间类场景从此可用 | M |
| **A2** | ~~GINNO.md~~ → **复用 Agent prompt** | 不新增文件机制;项目规则/行为约定写在现有 Agent 管理的 `system_prompt` 字段 | 零新 UI;变更感知并入 A4 | — |
| **A3** | 权限模式可见 | 特权 vs 审批模式 + ask 规则摘要告知模型 | 模型不再靠被拒消息事后学习权限边界 | S |
| **A4** | Agent 状态感知 | `agent` section 快照 `{agent_id, name, prompt_hash, tool_count}`;**切换 Agent** 或**当前 Agent prompt 被编辑**时发增量消息 | 消除多 Agent 共会话的身份混乱;覆盖原 A2 的变更感知诉求 | S |
| **A5** | 记忆注入治理 | MEMORY.md(全局+agent)注入加预算截断(建议合计 ≤4K tokens)+ hash 变更检测 | 现在全文无预算注入,记忆膨胀直接吃 token | M |
| **A5b** | 自动记忆蒸馏 | 实装已定义未读取的 `auto_summarize` / `pool_flush_threshold`(pool 达阈值自动 summarize_pool) | 死配置复活,记忆闭环自动化 | M |
| **A6** | Skills 索引预算化 | skill 索引按上下文比例限长,超预算截断描述 | skill 增多后索引不失控 | S |
| **A7** | MCP 变更感知 | mcp.json 修改/reload 后增量通知工具集变化 | 消除"工具凭空出现/消失" | S |

**A1 环境状态详细设计**:

字段与来源:

| 字段 | 来源 | 变更频率 |
|---|---|---|
| `date` + `weekday` | `datetime.now().astimezone()` 本地日期 | 每天一次 |
| `timezone` | tzname + UTC 偏移 | 几乎不变 |
| `os` | `platform.system()/release()/machine()` | 会话内静态 |
| `ginno_home` | `paths.home()` | 静态 |
| `project_slug` | session | 会话级(为多项目 roadmap 占位) |

渲染示例(进稳定层 system):

```xml
<environment>
<date>2026-08-04 (星期二)</date>
<timezone>Asia/Shanghai (UTC+8)</timezone>
<os>macOS 15.5 (arm64)</os>
<ginno_home>/Users/xx/.ginno — 记忆、skills、settings 所在目录</ginno_home>
<project>default</project>
</environment>
```

刻意**不注入时分秒**:每分钟变化会持续打破前缀缓存(Codex 同样只注入 date/timezone);需要精确时间用 bash `date`。跨天由 turn 入口对比 `date` 检测,发增量消息"日期已更新为 …"(UI 侧静默,见 §7)。

---

## 4. 维度 B:注入纪律(缓存工程)

| # | 条目 | 内容 | 价值 | 量 |
|---|---|---|---|---|
| **B1** | 易变内容迁出 system | wiki/附加文件/@mention 移到本轮尾部消息(HumanMessage 前) | 现在 system 每轮必变,任何缓存都无法命中 | M |
| **B2** | system 字节稳定化 | 稳定层无时间戳、无查询依赖、段落顺序确定 | prefix cache 前提 | S |
| **B3** | Anthropic cache_control | system 稳定层 ephemeral 断点;历史尾部可选滚动断点(ChatAnthropic 支持 block 级 cache_control) | 长会话输入成本降 ~90%(Anthropic 路径) | M |
| **B4** | OpenAI 系验证 | 实测 openai/DashScope/Qwen 网关 prefix caching 行为,遥测确认 | 用数据决定该路径收益 | S |

省 token 的账(为什么值得做):每轮请求重发全部历史,历史越长每轮越贵;prefix cache 让重叠前缀按 ~1/10 价计费。做法 A(每轮全量重注状态)为历史累积重复内容;做法 B(原地改 system 开头)令前缀从第 0 字节失效、全历史按全价重付;WorldState 纪律 = 少注入 + 前缀永不动,两头的钱都省。

---

## 5. 维度 C:WorldState 引擎(变更显式化)

| # | 条目 | 内容 | 量 |
|---|---|---|---|
| **C1** | `world_state.py` 核心 | Section 协议、快照构建、section 级 diff、基线持久化 | M |
| **C2** | 增量更新消息 | 变更合并为一条 `[world state update]`,置于本轮用户消息前 | S |
| **C3** | `context.updated` WS 事件 | 携带 `changes: [{section, summary}]`,前端渲染 chip(§7) | S |

接口骨架:

```python
class WorldStateSection(Protocol):
    id: str                                     # 稳定键:"environment"/"agent"/...
    def snapshot(self, ctx: SessionCtx) -> dict  # 只含比较所需的最小数据
    def render(self, snap: dict) -> str          # 渲染进稳定层 system 的文本
    def render_update(self, old: dict, new: dict) -> str  # 增量消息片段

class WorldState:
    sections: dict[str, WorldStateSection]       # 固定顺序 = 渲染顺序
    def build(session) -> Snapshot               # dict[section_id -> snap]
    def render_system(snapshot) -> str
    def diff(old, new) -> list[SectionChange]
    def render_update(changes) -> str            # 合并渲染一条增量消息
```

更新消息模板:

```
[world state update]
- Now operating as **writer**(5 个可用工具,此前 12 个)
- 你的角色设定已更新
- 长期记忆已刷新(来自 31 条对话的蒸馏)
```

持久化:`~/.ginno/projects/<slug>/sessions/<id>.world.json`(每会话一个小文件,**不进 checkpoint**——FileCheckpointer 每步全量快照,塞进去会在已经 60MB 的文件里每步复制一份)。`_ensure_session` 重启恢复时读它继续 diff,避免误报"一切都变了"。更新消息本身在 messages 里 → checkpoint/resume 自动携带。

变更检测触发点:每 turn 入口 `_run_stream` 调 `_sync_world_state(session)`(覆盖日期翻页、文件 mtime、设置变更);事件驱动补充(`POST /api/memory/summarize` 完成、`PUT /api/mcp` + reload、skill 增删、settings PUT、invoke 携带的 agent_id 变化)。

---

## 6. 维度 D:遥测与可见性

| # | 条目 | 内容 | 量 |
|---|---|---|---|
| **D1** | usage 采集 | 从 `AIMessage.usage_metadata` 提取 input/output/cache tokens | S |
| **D2** | WS usage 事件 | 每轮下发;协议位现成(`_ev` + turn_id 贯穿) | S |
| **D3** | 前端用量展示 | 轮级 + 会话累计;设置页统计面板 | M |
| **D4** | 缓存命中率指标 | `cache_read / total input` 比率,验证 B3/B4 | S |

---

## 7. 上下文更新 chip(产品规格)

**定义**:当"模型知道的事情"发生变化,在聊天流里给用户一条轻量系统提示——让用户知道"模型已经知道了"。它是变更事件的 UI 镜像:同一事件,(a) 进历史给模型看(C2),(b) 推 `context.updated` 给用户看(chip)。chip 本身不是对话消息,不占 token,不进 checkpoint。

**形态**:聊天流中的居中小字系统行,非气泡,不打断对话。复用现有 `notice` 事件的渲染范式(不写 checkpoint 的旁路提示已有先例),新增一个 block 类型:

```
┌─────────────────────────────────────────────┐
│  用户: 帮我起草那篇周报                          │
│  Agent(writer): 好的,基于这周的记录……           │
│                                             │
│          ⟳ 已切换为 dev · 12 个可用工具         │  ← chip
│                                             │
│  用户: 那先把测试跑一下                          │
│  Agent(dev): ……                             │
└─────────────────────────────────────────────┘
```

**文案与显示级别**:

| 触发事件 | chip 文案 | 级别 |
|---|---|---|
| 切换 Agent | ⟳ 已切换为 **dev** · 12 个可用工具 | 显示 |
| Agent prompt 被编辑 | ⟳ **writer** 的角色设定已更新,模型已感知 | 显示 |
| 记忆总结完成 | ⟳ 长期记忆已更新(31 条对话 distilled) | 显示 |
| MCP 配置重载 | ⟳ MCP 工具已更新:12 → 14 | 显示 |
| 特权模式开关 | ⟳ 已切换为特权模式(工具不再询问) | 显示(安全语义,必须显式) |
| 日期翻页 | — | 静默(太平凡,显示是打扰) |

**交互**:
- MVP:纯文本一行,无交互(ChatStream 加一个 block 类型 + 消费 `context.updated`)。
- V2(看反馈再定):点击展开变更详情——prompt 编辑显示改了什么、MCP 显示增删工具、记忆显示新 MEMORY.md 预览;可复用右栏现有预览组件。
- 连续变更合并("上下文已更新:3 项变更")MVP 不做——变更本来就低频。

---

## 8. 维度 E:上下文治理(伴生工程,比 WorldState 更 urgent)

| # | 条目 | 内容 | 量 |
|---|---|---|---|
| **E1** | token 计数 | 本地估算(bytes/4 起步)+ 服务端 usage 校正 | S |
| **E2** | 工具输出截断 | 头+尾保留的中间截断、字节/token 双预算、截断标记;大文件 read/bash 输出不再无限进历史 | M |
| **E3** | 历史压缩(local 摘要) | 超阈值用当前模型跑摘要轮替换旧历史,保留最近用户消息 | XL |
| **E4** | 压缩后 WorldState 重注入 | 压缩完成时重新 render 当前快照(依赖 C1) | S |
| **E5** | checkpoint 瘦身 | FileCheckpointer 增量化(实装 put_writes/去除每步全量快照) | L |

E3 是全项目最大单项,建议阶段 3 一开始立项,不等 WorldState 全部完成;E4 是其正确性前提(压缩后模型仍知日期/规则/角色)。

---

## 9. 冻结与预留

**冻结(与 artifacts 方向有冲突,待产品定案)**:

| 条目 | 说明 |
|---|---|
| workspace 注入 | A1 不含 workspace/uploads_dir 字段;artifacts 体系演进可能重定义 workspace 语义,现在注入会制造返工 |
| F1 工具 workspace 默认值(不传时落会话 workspace 而非 sidecar cwd) | 与上同源,一并冻结;冻结期间文件类工具继续依赖模型显式传参 |
| F2 占位值回退(/tmp/gw → ~/workspace) | 同上 |

**预留(按触发条件启动,不预做)**:

| # | 条目 | 触发条件 |
|---|---|---|
| G1 | Deferred 工具 + tool_search(BM25 检索、命中经增量消息注入) | MCP 工具 > 20~30 个 |
| G2 | hooks 接线(SessionStart/UserPromptSubmit 派发 + `inject` 消费) | 出现"用户自定义上下文注入"需求 |
| G3 | "模型当前所知"检查器(右栏展示各 section 快照) | D3 落地后 |
| G4 | 多项目支持(slug 实化 + workspace 选择器) | 产品排期;section 已按 slug keyed,零返工 |
| G5 | load_skill 工具(模型自主按需读 skill 正文) | skill 数量增长、纯 /skill 调用不够时 |

**仍生效的防护项**:F3——GINNO.md 取消后,新注入面只剩用户自己的 agent prompt/记忆,注入防护沿用现有 `_INJECTION_PATTERNS` sanitize + "视为数据"包裹纪律,无新增工作。

---

## 10. 集成点清单(代码级)

1. `graph.py:build_agent_system_prompt` 拆为 `build_stable_system(world_snapshot)`(persona/工具/指引 + sections,**禁止每轮变化值**)+ `build_turn_context(attached, mentions, wiki)`(本轮尾部消息);`agent_node` 改为 `[sys_msg] + history + [turn_context?]`。
2. `server.py:_run_stream` 入口加 `_sync_world_state(session)`;有变更时 `input_state["messages"] = [update_msg, HumanMessage(...)]`(add_messages 按序追加,增量消息恰在用户消息前);同时推 `context.updated`。
3. `workflows/nodes/agent_helpers.py:build_system` 同步改造(复用 `build_stable_system`;顺手修正其 project_slug 硬编码 "default")。
4. 快照持久化:每会话 `sessions/<id>.world.json`;`_ensure_session` 恢复基线。
5. Anthropic 缓存:`ChatAnthropic` system 传 block 列表,稳定层挂 `cache_control: {"type":"ephemeral"}`。
6. 前端:`ChatStream` 新增 `context` block 类型 + 消费 `context.updated`/`usage` 事件。

---

## 11. 分期实施与验收

```
阶段0  快速赢(无框架依赖)                          ~1天
  D1 + D2
  验收:每轮 WS 有 usage 事件(input/output/cache tokens)

阶段1  WorldState 引擎 + 首批 sections             ~3-4天
  C1 + C2 + C3 → A1(无 workspace 版)+ A3 + A4
  验收:system 出现 <environment>/<permissions>;跨天仅发一次日期更新;
       切 Agent / 编辑 Agent prompt 有增量消息 + chip;e2e 快照断言

阶段2  注入纪律 + 缓存兑现                          ~3天
  B1 + B2 → B3 → B4 + D4
  验收:未变更轮次 system 跨轮 diff 为空;
       Anthropic 长会话 cache_read 占比 >60%(遥测实测)

阶段3  上下文治理(E1/E2 可提前并行)                 ~7天
  E1 + E2(先行)→ E3 + E4 → E5
  验收:100 轮会话 token 占用有界;单次大文件 read 不撑爆上下文;
       checkpoint 文件 <5MB;压缩后模型仍知日期/规则/角色

阶段4  记忆与 skills 深化                           ~3天
  A5 + A5b + A6 + A7
  验收:记忆注入有预算且超限有截断标记;pool 满阈值自动蒸馏;
       MCP reload 有增量通知

G 维度  按触发条件排入后续迭代
```

依赖链:C1 是 A3/A4/E4 的地基;B1/B2 是 B3 的前提;D1 是 B4/D4/E3 阈值的前提。

---

## 12. 风险与对策

| 风险 | 对策 |
|---|---|
| prompt 结构变化引起模型行为漂移 | 现有 Playwright e2e + runtime tests 回归;settings 开关灰度(`context.world_state: on/off`) |
| 多 section 同时变化的消息轰炸 | diff 合并为一条更新消息 |
| openai-compatible 网关无缓存 | 遥测识别后降级:至少保住注入量减少 + 变更显式化的收益 |
| 增量消息历史累积 | 单条 ~百 token 量级且是真实对话记录;阶段 3 压缩时随 turn 摘要 |
| workflow 引擎行为变化 | agent_helpers 同步改造后跑 workflow e2e 验证 |

---

## 13. 产品影响与已决事项

**已决**(2026-08-04):
- workspace 注入及相关加固(F1/F2)**暂缓**,与 artifacts 方向有冲突,待其定案(§9);
- 项目规则**不新建 GINNO.md**,复用 Agent 管理的 prompt 字段;其变更感知并入 A4;
- chip 显示级别表见 §7(待产品最终确认);日期类变更静默。

**待产品决策**:

| 决策点 | 选项 | 建议 |
|---|---|---|
| chip 显示级别表 | §7 草案 | 照草案;特权模式变更必须显式 |
| token 用量展示 | 设置页统计 / 每轮气泡 / 都展示 | 先设置页统计,气泡可选开关 |
| 自动蒸馏默认值 | 默认开 / 关 | 默认开(阈值 30),设置页可关 |
| 记忆注入预算 | 字符/token 上限 | 全局+agent 合计 ≤4K tokens,超限截断并标注 |

**正向影响**:模型知道日期/OS 后,TODO/周报类场景真正可用(建议进 changelog);变更显式化提升信任感;usage 遥测让"Supervisor token 预算"(session-workflow-overview 设计稿)第一次有数据基础;所有 section 按 slug keyed,与多项目 roadmap 天然对齐。

---

## 附:Codex 机制要点速查(本方案的参考来源)

- **WorldState**:section 化快照 + RFC 7386 merge patch 持久化 + 三态(Absent/Unknown/Known)历史对齐;变更只发增量、追加历史末尾,前缀稳定保 prefix cache。
- **压缩四路**:token-budget(跳摘要开新窗口)/ remote v1/v2(服务端、加密 Compaction)/ local(当前模型摘要),comp_hash 换模型时先用旧模型压缩。
- **工具**:Deferred 暴露 + tool_search(BM25 over 全 schema 文本,命中经 `tool_search_output` item 由服务端注入下一轮)——Ginno 预留为 G1。
- **纪律**:"不重写历史、避免 cache miss、片段有硬上限"写进 Codex 自己的 AGENTS.md——本方案 B2/E2/A5 预算即此纪律的移植。
