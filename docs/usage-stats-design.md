# Usage 设计（全局 Token 用量统计）

> 状态：**P0 已实现**（2026-08-08）。评审决议已合入：① 成本估算不纳入（移出 P2）；② 明细保留 90 天为默认；③ provider verify ping 计入（`source=probe`，P1 采集点接线时生效）；④ 时间控制不跨页签——统计窗口移入概览页签内（见 §3.1）。可交互原型：`docs/design/prototypes/usage-stats-prototype.html`。目标：给 Ginno 引入全局 Token 用量统计能力——Settings 新增「用量统计」菜单项，提供**每日/每小时用量趋势、单会话与全局的输入/输出/缓存命中明细、请求日志、Provider 与模型维度统计**。所有数据纯本地存储（延续 Ginno「无数据库、文件即状态」的约定）。

## 0. TL;DR

- **入口**：Settings → 主分组新增「用量统计」（`/settings/usage`），页面内三个子页签：**概览 / 会话 / 请求日志**。
- **数据底座**：新增 `~/.ginno/usage/requests-YYYY-MM-DD.jsonl` **追加式请求日志**——每次 LLM 调用落一行（时间、会话、来源、provider、model、输入/输出/缓存读/缓存写 tokens、耗时、状态）。这是全部统计的唯一事实来源（single source of truth）。
- **采集点**：复用现有 `extract_usage`（LangChain `usage_metadata`）埋点，从「只累加到内存」升级为「累加 + 落盘」；后台 LLM 调用（压缩/workflow/memory/KB）作为 P1 补齐，用 `source` 字段区分来源。
- **现状升级**：现有 `_USAGE_BY_SESSION` 是纯内存的，runtime 重启即清零——改为日志聚合后，**会话 token 量跨重启可追溯**；TopBar 实时计数器行为不变。
- **指标口径统一**：归一化为「整段提示词」口径（input = 非缓存 + 缓存读 + 缓存写），缓存命中率 = `cache_read / input`，跨 provider 可加和、可比较（修正现有 Anthropic 口径下可能 >100% 的 bug）。
- **保留策略**：明细默认保留 **90 天**（启动时清理更老的日文件），无上限聚合需求由前端按窗口实时聚合。
- **分期**：P0 = 日志落盘 + 概览/会话/请求日志三个页面 + Provider/模型统计；P1 = 后台调用采集、轮次级下钻、保留期配置；P2 = 成本估算、CSV 导出、预算提醒。

---

## 1. 背景与目标

### 1.1 现状盘点（代码事实）

| 现状 | 位置 | 局限 |
|---|---|---|
| 每次 LLM 调用可从 `AIMessage.usage_metadata` 提取 `input/output/cache_read/cache_creation` | `usage.py::extract_usage` | 只在主对话流里采了一次，**不落盘** |
| 每会话累计器 `_USAGE_BY_SESSION`（含 calls、cache_hit_ratio） | `server.py:217` | **纯内存**，runtime 重启清零；只有当前进程生命周期 |
| WS `usage` 事件（turn + session 累计 + 命中率）→ TopBar 显示 `↑in ↓out ⚡cache%` | `server.py:3963`、`TopBar.tsx:96` | 只服务「本次运行」，无历史 |
| `GET /api/sessions/{id}/usage` 供切换会话时回显 | `server.py:633` | 数据源同上，重启后为空 |
| 会话 meta 持久化了 `provider/model/created/updated` | `projects/<slug>/sessions/_index.json` | 没有用量字段 |
| 后台 LLM 调用（历史压缩、workflow 合成/运行、memory 提炼、KB 构建、provider 探测）各自直连 `build_model` | `server.py:1698/1736/3151`、`compaction.py`、`memory/summarize.py`、`providers.py` | **完全不计入任何统计** |

结论：**数据在每次调用时都拿得到，只是没有被记录**。这是本设计最重要的事实基础——不需要新增拦截层，只需在既有提取点「顺手落盘」，再为后台调用点补同一个记录函数。

### 1.2 目标

对应三条原始诉求：

1. **单会话看得清**：任一会话的累计输入/输出、缓存命中（读/写）、调用次数；重启不丢；可下钻到轮次（P1）。
2. **全局看得清**：跨会话的每日 token 用量；按**小时**看使用趋势；输入/输出/缓存命中同屏。
3. **可审计、可归因**：请求级日志（每次 LLM 调用一行）；按 **Provider**、按**模型**的分布统计。

### 1.3 非目标（本期不做）

- 成本/金额估算（价格表、自定义单价）→ 列为 P2 开放问题，见 §10。
- 预算与超额提醒（Goal 评审已决议去掉 token 预算，统计侧也不引入）。
- 多机汇总/云端上报——纯本地单机。
- 请求/响应**内容**的记录——只记元数据与计数（见 §4.4 隐私）。

---

## 2. 用户与场景

Ginno 是单机个人 Agent，用户即机主。核心场景：

| # | 场景 | 用户问题 | 落在哪个页面 |
|---|---|---|---|
| S1 | 每日盘点 | 「今天/这周烧了多少 token？比昨天多吗？」 | 概览 KPI + 每日趋势 |
| S2 | 峰值定位 | 「凌晨怎么还在跑？哪个小时用得最凶？」（Goal 自主续跑、长任务） | 概览 小时分布 |
| S3 | 会话归因 | 「哪个会话吃得最多？这个重构会话到底花了多少？」 | 会话页签（排序 + 单会话明细） |
| S4 | 缓存健康 | 「prompt cache 到底起没起作用？命中率多少？」（缓存读按 ~10% 计价，直接影响成本） | KPI 卡 + 每会话/每请求命中列 |
| S5 | 多供应商对比 | 「anthropic 和自定义端点各占多少？哪个模型用得最多？」 | 概览 Provider/模型分布 |
| S6 | 排障取证 | 「刚才那次报错的请求花了多少 token？某会话最近一次调用是什么时候、什么模型？」 | 请求日志（过滤 + 分页） |

---

## 3. 产品方案

### 3.1 入口与信息架构

- Settings 左侧导航 **MAIN 分组**新增一项（「会话文件」之后）：`用量统计`（icon: `BarChart3`，建议色 `#2dd4bf`）。路由 `/settings/usage`，与现有 tab 机制一致（`generateStaticParams` + `SettingsView` 映射）。
- 页面内顶部为**页签切换**：

```
用量统计        [ 概览 | 会话 | 请求日志 ]
```

三个页签职责单一、互不重叠：**概览回答「多少/何时」，会话回答「谁花的」，请求日志回答「每一次的细节」**。页签间可跳转（概览点某天 → 请求日志按该日过滤；会话行 → 请求日志按该会话过滤）。

> **时间控制不跨页签**（评审决议）：不设页面级全局时间范围。概览用自己的「统计窗口」（近 7/30/90 天，默认 30），会话用自己的范围过滤，请求日志用自己的日期过滤——每个页签的时间语义不同（窗口 vs 窗口 vs 单日），一个全局控件只会造成误解。

### 3.2 概览（默认页签）

自上而下四层：KPI 卡 → 每日趋势 → 小时分布 → Provider/模型分布。

```
┌────────────────────────────────────────────────────────────────────────┐
│  统计窗口只影响本页…                        [ 近7天 | 近30天 | 近90天 ]  │
│                                                                        │
│  ┌───────────────┐ ┌───────────────┐ ┌───────────────┐ ┌────────────┐ │
│  │ 今日 Tokens    │ │ 今日请求       │ │ 今日缓存命中率  │ │ 近7天 Tokens│ │
│  │ 1.24M         │ │ 312 次        │ │ 78.2% ⚡      │ │ 8.9M       │ │
│  │ ↑1.10M ↓140K  │ │ 较昨日 +41     │ │ 较昨日 +2.1pt  │ │ 12 个会话   │ │
│  └───────────────┘ └───────────────┘ └───────────────┘ └────────────┘ │
│                                                                        │
│  每日趋势（堆叠柱；点击某天 → 下方小时分布切到该日；悬停显示当日明细）      │
│   tokens ▲      ▅▇                                                    │
│          │  ▂▃▅ ▇█▆   ▆▃    ▂            ■输入(非缓存) ■缓存读 ■输出   │
│          └──────────────────────────────▶ 日期                         │
│                                                                        │
│  小时分布 · 2026-08-07 ▾（默认今天；悬停显示该小时 in/out/cache/请求数）  │
│   ▁▁▂▅▇█▆▃▁▁▁▂▃▅▇▆▂▁▁▁▂▃                                              │
│   0    4    8    12   16   20   23 时                                  │
│                                                                        │
│  ┌─────────────────────────────┐  ┌──────────────────────────────────┐ │
│  │ Provider 分布（窗口内）       │  │ 模型排行（按总 tokens）           │ │
│  │ anthropic ▓▓▓▓▓▓▓▓ 72% 6.4M │  │ claude-x   6.1M  78%  命中 81%  │ │
│  │ custom    ▓▓▓     28% 2.5M  │  │ gpt-4o     1.7M  22%  命中 —    │ │
│  │ （悬停: in/out/请求数）       │  │ （行尾 ⚡=缓存命中率；点击→日志）  │ │
│  └─────────────────────────────┘  └──────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────────┘
```

要点：
- **统计窗口（页签内，非全局）**：近 7/30/90 天（默认 30），影响概览内的每日趋势、Provider/模型分布与第 4 张 KPI 卡（近 N 天总量）；前 3 张 KPI 卡固定为「今日」口径，不受窗口影响。会话、请求日志页签不受此控件影响（各有自己的时间过滤）。
- **KPI 卡 4 张**：今日 tokens（副文案拆输入/输出）、今日请求数、今日缓存命中率、近 N 天总量（随窗口）。每张卡带「较昨日」环比小字。
- **每日趋势**：堆叠柱三段 = 非缓存输入 / 缓存读 / 输出。选「输入总量」与「缓存读」分开着色，是为了直观回答 S4——缓存读占比越高柱子越「绿」。
- **小时分布**：24 根柱，默认今天，日期选择器可回看任意有数据的日子（回答 S2：一眼看出凌晨的 Goal 续跑）。
- **Provider/模型分布**：横向比例条 + 排行榜，模型行带命中率。点击任一分布项 → 跳转请求日志并预置过滤。

### 3.3 会话（Sessions 页签）

回答「谁花的」：全部会话按用量排序的总表，支持搜索与排序；展开/点击进入单会话明细。

```
┌────────────────────────────────────────────────────────────────────────┐
│  [搜索会话…]                      排序: 总Tokens ▾   范围: 近30天 ▾     │
│                                                                        │
│  会话 / Agent        模型        输入    输出   缓存读  命中  请求  最近活跃│
│  ▾ 重构支付模块  🤖   claude-x   812K   96K   2.4M   74%   214   2小时前 │
│     └ 轮次明细(P1): 每轮 in/out/cache 时间轴 + 压缩/workflow 来源标记    │
│  ▸ 排查登录异常       claude-x   301K   40K   900K   69%    88   昨天    │
│  ▸ 周报生成          gpt-4o      52K    8K    —      —     12   3天前   │
│  …                                                                     │
└────────────────────────────────────────────────────────────────────────┘
```

要点：
- 每行 = 一个会话在所选时间窗内的聚合：**输入、输出、缓存读、命中率、请求数、最近活跃**。会话标题/图标/Agent 来自 `_index.json`（已持久化 provider/model，天然可展示）。
- 会话被删除后，其历史用量**仍然保留在日志中**（行显示「(已删除) + 原 ID 短码」）——用量是账单性质的数据，不随会话删除而销毁（对齐「会话文件删除会话但保留文件」的既有产品决策）。
- 单会话视图（行内展开或右侧抽屉）：累计卡 + 该会话的小时/轮次分布。轮次级下钻为 P1。
- 与现状的关系：`GET /api/sessions/{id}/usage`（TopBar 数据源）切换为从日志聚合（§5.6），TopBar 的「本次运行」实时计数行为不变。

### 3.4 请求日志（Requests 页签）

一行 = 一次 LLM 调用。这是排障与审计的底表。

```
┌────────────────────────────────────────────────────────────────────────┐
│  日期: 2026-08-07 ▾  Provider ▾  模型 ▾  来源 ▾  会话 ▾   [搜索…]      │
│                                                                        │
│  时间      会话          来源    Provider   模型      输入  输出 缓存读 耗时 状态│
│  23:14:02 重构支付模块    chat   anthropic claude-x 12.4K 1.2K 11.0K 3.2s ✓ │
│  23:13:10 重构支付模块    chat   anthropic claude-x 11.8K 0.9K 10.9K 2.8s ✓ │
│  23:02:44 (后台)         compact anthropic claude-x 88.0K 2.1K    —  12.4s ✓ │
│  …                                          第 1/9 页  ‹ 1 2 3 … 9 ›  │
└────────────────────────────────────────────────────────────────────────┘
```

要点：
- **过滤器**：日期、provider、模型、来源（chat/goal/compaction/workflow/memory/kb/probe）、会话；组合过滤。
- **分页** 50 行/页；按时间倒序。
- 失败请求（P1）：状态列 ✗ + 错误类别，tokens 为空——用于回答「哪些请求在烧钱重试/报错」。
- 行尾操作：「定位会话」（跳转会话页签）。
- 不展示请求内容（prompt/响应），只展示计数与元数据（§4.4）。

### 3.5 指标口径（重要：跨 provider 归一化）

各家 provider 对 usage 字段的语义不同，**记录时统一归一化**，页面只消费归一化后的口径：

| 字段 | 归一化口径 | Anthropic 原始 | OpenAI(-兼容) 原始 |
|---|---|---|---|
| `input_tokens` | **整段提示词** = 非缓存输入 + 缓存读 + 缓存写 | `input + cache_read + cache_creation` | `prompt_tokens`（本就含 cached） |
| `output_tokens` | 输出 | 原样 | 原样 |
| `cache_read_tokens` | 缓存命中读 | `cache_read` | `prompt_tokens_details.cached_tokens` |
| `cache_creation_tokens` | 缓存写入 | `cache_creation` | 0（OpenAI 无此概念） |

由此得到唯一定义的比率：

```
缓存命中率 = cache_read_tokens / input_tokens      （分母=整段提示词，∈[0,1]）
净输入(非缓存) = input_tokens − cache_read_tokens − cache_creation_tokens
```

> ⚠️ 修正现状：`usage.py::cache_hit_ratio` 现用 `cache_read / 原始input`，在 Anthropic 口径下分母不含缓存部分，命中率可能算出 >100%（TopBar 的 ⚡% 同源）。P0 一并统一为上述口径；`usage.py` 保留函数签名，仅修正分母。

### 3.6 请求来源（source 分类）

| source | 含义 | 采集期 |
|---|---|---|
| `chat` | 用户对话轮次（含工具循环中的每次模型调用） | P0 |
| `goal` | Goal 自主续轮（与 chat 同一流式路径，由续轮注入方打标） | P0 |
| `compaction` | 历史压缩摘要调用 | P1 |
| `workflow` | workflow 合成 / 运行中的模型节点 | P1 |
| `memory` | memory 提炼/摘要 | P1 |
| `kb` | 知识库 Wiki 构建/摘要 | P1 |
| `probe` | provider verify / search_probe 探测 | P1 |
| `other` | 兜底 | — |

概览/会话页签默认统计**全部来源**；请求日志可按来源过滤。这样「今天烧了多少」永远是真实的全口径，而「对话本身花了多少」可用来源过滤得到。

---

## 4. 数据设计

### 4.1 记录时机与 Schema

每次拿到 `usage_metadata` 即追加一行 JSON（一次 LLM 调用 = 一行）：

```jsonc
{
  "ts": 1786378800.123,          // epoch 秒（本地时区落桶）
  "session_id": "9f2c…",         // 后台调用可为 null
  "project_slug": "ginno",       // 可为 null
  "agent_id": "dev",             // 可为 null
  "turn_id": "t-8a1b",           // 轮次聚合用（P1 下钻）；可为 null
  "source": "chat",              // §3.6 枚举
  "provider": "anthropic",
  "model": "claude-x",
  "input_tokens": 12400,         // 归一化后（§3.5）
  "output_tokens": 1200,
  "cache_read_tokens": 11000,
  "cache_creation_tokens": 0,
  "latency_ms": 3200,            // P0 可为 null（流式路径暂不测单次耗时）
  "ok": true,
  "error": null                  // 失败请求（P1）记错误类别，不记报文
}
```

### 4.2 存储布局

延续「无数据库、文件即状态」：

```
~/.ginno/usage/
├── requests-2026-08-07.jsonl    # 当日请求日志，append-only，一行一条
├── requests-2026-08-06.jsonl
└── …
```

- **按天分文件**：天然支撑「每日统计」、清理与聚合的边界；单日上限就是调用次数，文件体积可控。
- 写入方：runtime 内新增 `usage_store.py`（记录 + 聚合 + 清理），append 用 `O_APPEND` 行写，无锁单机场景足够；写失败只告警不阻塞对话（统计永远不能拖累主流程）。
- **容量估算**：单行 ~250–300B；重度日 2000 次调用 ≈ 600KB/天 ≈ **54MB/90 天**，无压力。
- 聚合不落预计算表：窗口最大 90 天、单日 ≤ 数百 KB，API 层实时流式聚合足够快（完成的往日文件可做进程内 mtime 缓存）。

### 4.3 保留与清理

- 默认保留 **90 天**明细；runtime 启动时（及每日首次写入时）删除更老的 `requests-*.jsonl`。
- 保留期 P1 可在设置调整（`settings.json.usage.retention_days`）；P0 用常量默认值。
- 不提供「清空统计」UI（P0）；用户可直接删目录。

### 4.4 隐私

- 只记**计数与元数据**：不记 prompt、响应、工具参数、文件路径。
- `error` 字段只记错误类别（如 `rate_limit` / `timeout`），不落原始报文。
- 数据不出本机（与 Ginno 整体一致）。

---

## 5. API 设计（runtime FastAPI）

所有接口前缀 `/api/usage`，时间参数为本地日期 `YYYY-MM-DD`；聚合在 server 端完成。

| 接口 | 说明 |
|---|---|
| `GET /api/usage/overview?days=30` | KPI + 每日序列 + Provider/模型分布。返回：`{window, today, prev_today, totals, daily:[{date, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, calls, cache_hit_ratio}], providers:[{provider, …, share}], models:[{provider, model, …, cache_hit_ratio}]}` |
| `GET /api/usage/hourly?date=2026-08-07` | 单日 24 小时分布：`{date, hours:[{hour, input_tokens, output_tokens, cache_read_tokens, calls}]}` |
| `GET /api/usage/sessions?from&to&sort=total&limit=50` | 会话聚合表（join 会话 meta 得到标题/图标/agent/模型；已删除会话给占位标题） |
| `GET /api/usage/sessions/{session_id}?from&to` | 单会话聚合 + （P1）轮次序列 `turns:[{turn_id, ts, input_tokens, …}]` |
| `GET /api/usage/requests?date=&provider=&model=&source=&session_id=&page=&page_size=` | 请求日志分页：`{rows:[…], total, page, page_size}` |
| `GET /api/sessions/{id}/usage`（改造既有） | TopBar 数据源：优先日志聚合（含历史），无日志回退内存累计；响应形状不变 |

实现要点：
- 新增 `usage_store.py`：`record(entry)`（归一化 + append）、`iter_range(from,to)`（逐日流式读）、`aggregate_*` 系列、`cleanup(retention_days)`；往日文件按 `(path, mtime, size)` 做进程内缓存。
- 采集接线（P0）：`_stream_graph` 中现有 `extract_usage` 成功分支处调用 `record()`（provider/model 取自会话 meta，`source` 由续轮标记决定 chat/goal）；`usage.py::extract_usage` 增加 OpenAI `cached_tokens` → `cache_read` 的归一化。
- 采集接线（P1）：compaction / workflow / memory / kb / probe 各调用点包一层统一 helper。

## 6. 前端设计（实现层面）

- **导航**：`SettingsNav.tsx` MAIN 组加 `{ id:"usage", label:"用量统计", icon: BarChart3, color:"#2dd4bf" }`；`/settings/[tab]` 的 `generateStaticParams` 与 `SettingsView` 各加一项。
- **组件**（`components/settings/`）：`UsageSettings.tsx`（页签壳 + 时间范围）；`usage/OverviewPanel.tsx`、`usage/SessionsPanel.tsx`、`usage/RequestsPanel.tsx`；`usage/charts.tsx`（基于已依赖的 **d3-scale** 手绘 SVG：堆叠柱、小时柱、横向比例条——不引入新图表库，风格对齐现有深色 UI）。
- **数字格式**：复用 TopBar 的 `fmtTokens`（抽出到 lib 共享）；命中率展示 `⚡78%`。
- **刷新策略**：进入页签/切换范围时拉取；页面可见时 60s 轮询概览；不做 WS 推送（统计页非实时场景）。
- **空态**：无任何日志时给引导文案（「开始对话后这里会出现用量数据」），图表不渲染假数据。

## 7. 兼容与边界

| 情形 | 处理 |
|---|---|
| 功能启用前的历史 | 无日志可追溯——空态；会话聚合回退内存累计（现状行为） |
| OpenAI-兼容端点不返回 usage | 该调用不落行；会话/全局统计天然不含（与 TopBar 现状一致） |
| 会话已删除 | 日志保留；会话页签显示占位标题 + ID 短码 |
| 时区 | 全部按**本地时区**落桶（日文件名为本地日期）；跨时区旅行不做特殊处理 |
| 磁盘写入失败 | 仅 `_log.warning`，绝不影响对话流 |
| 并发写 | 单 runtime 单进程 append；测试用 `$GINNO_HOME` 隔离（既有约定） |

## 8. 分期计划

- **P0（MVP，本需求三条全部落地）**
  1. `usage_store.py` + 归一化口径修正（§3.5）+ 主对话/Goal 采集落盘
  2. `/api/usage/*` 五个接口 + `/api/sessions/{id}/usage` 改造
  3. Settings「用量统计」页：概览（KPI/每日/小时/Provider/模型）、会话表、请求日志表
  4. 90 天保留清理
- **P1**：后台 LLM 调用采集（compaction/workflow/memory/kb/probe）、轮次级下钻、失败请求记录、保留期设置项、`latency_ms` 实测
- **P2**：成本估算、CSV 导出、预算/提醒、请求日志全文检索

## 9. 验收标准（P0）

1. 真实对话一轮后：当日 `requests-*.jsonl` 新增一行；请求日志页可见；概览 KPI/图表同步变化。
2. 重启 runtime：概览/会话统计不丢；`GET /api/sessions/{id}/usage` 返回含历史的累计值。
3. 命中率按 §3.5 口径计算，永不 >100%；TopBar ⚡% 与日志口径一致。
4. 概览各图 ↔ 请求日志按同窗口过滤后的手工加和一致（provider/模型分布 likewise）。
5. 删除会话后其用量仍出现在会话页签与概览中。
6. 构造 91 天前的假日文件，启动后被清理。
7. 单测/API 测试：归一化（Anthropic/OpenAI 两种 usage 形状）、聚合、清理、分页与过滤（走 `GINNO_FAKE_LLM` 缝隙产生 usage）。

## 10. 开放问题（评审定夺）

1. **成本估算**（tokens × 单价）是否纳入路线图？难点：自定义 OpenAI-兼容端点无公开价目，需要「每 provider 手填单价」；建议 P2，且默认关闭。
2. **保留期默认 90 天**是否合适？（备选 30/180；容量不是约束，主要是「翻旧账」的价值衰减）
3. 是否需要**导出 CSV**（P2 已列，确认优先级）。
4. provider **verify 的 ping 请求**（1 token 级）是否计入统计？建议计入并标 `source=probe`，保证「请求日志 = 全部出站调用」的完整性。
