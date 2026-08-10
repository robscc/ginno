# 引用与来源设计（Citations & Sources）：Wiki + WebSearch

> 状态：**P0 + P1 已实现**（2026-08-08，单测 + e2e 全绿）。本文由 `wiki-citation-design.md`
> 扩展而来，统一两类来源。实现与设计的偏差点见 §10 各期注记。
> 目标：给 Ginno 的回答建立**统一出处体系**——本地知识（LLMWiki 注入）与外部知识（WebSearch）
> 都可引用、可点击、可验证、有遥测；并补齐 Ginno 目前缺失的**内置网络搜索工具**。
> 概念参考：Codex memories 的 `<oai-mem-citation>` + read usage telemetry（2026-08 源码分析）；
> Reasonix 的多引擎 web search（`/search-engine`）与引擎抽象。

---

## 0. TL;DR

- **统一来源模型**：每轮一个 `SourceRegistry`，条目带稳定编号（`s1..sn`）与 `kind: wiki | web`；
  引用走**同一个**末尾块 `<ginno_citations>`（类型化条目），行内引用 wiki 用 `[[wikilink]]`、
  web 用数字角标 `[n]`。
- **双信号遥测**（沿用 wiki 稿）：`injected`（弱）/ `cited`（强）；web 侧再加
  `snippet（仅搜索摘要）/ fetched（已读原文）` 的**接地深度**维度——UI 上直接可见"这个来源模型读到哪一层"。
- **WebSearch 补齐**：新增内置工具 `web_search / web_fetch`（引擎可插拔：DuckDuckGo 默认免 key，
  可配 SearXNG/Tavily/Brave/Exa/Bing/Baidu），搜索结果**自带编号**，模型据此引用；
  provider 原生联网（Qwen `enable_search` 等）作为 P2 适配层映射进同一模型。
- **产品显示**：答案气泡下的**来源条**（图标 + 标题 + 域名 + 深度徽章 + 用途 note，点击跳转），
  P1 加行内角标/悬停卡；历史回放零额外持久化（从已落盘消息文本重解析）。
- **闭环**：web 遥测按域名聚合 → KB Discover「高频引用的外部域名」→ **一键存档进 vault Raw/**
  （web → wiki 沉淀，P2）。
- **缓存无害**：契约文本随每轮易变上下文注入；稳定 system 层不动。

---

## 1. 背景与目标

### 1.1 现状盲区

1. **回答无溯源**：结论是否基于自己的笔记/网页，用户看不到；
2. **注入有效性不可观测**：wiki 注入了有没有被用，无信号（见 §3 遥测）；
3. **Ginno 没有内置网络搜索**：现状只有两条不透明路径——
   - provider 自带联网（`enable_search`，Qwen/DashScope 等）：**搜索过程与来源对运行时完全不可见**，无法引用、无法遥测；
   - MCP 浏览工具（默认 Playwright）：是"操作浏览器"不是"检索"，来源捕获靠启发式。
   即：**外部知识进入对话的链路没有结构化来源记录**——这是 WebSearch 引用要先解决的基建问题。

### 1.2 参考

| 系统 | 做法 | 本方案取舍 |
|---|---|---|
| Codex memories | 末尾 `<oai-mem-citation>` 块（文件:行号 + rollout_ids）+ shell 命令遥测 → usage 决定记忆存废 | 采用"末尾块 + 用量反馈"骨架；遥测信号源换成注入计数/引用解析（更适合注入式检索） |
| Reasonix | `web_search/web_fetch` 内置 + `/search-engine` 多引擎（Bing/Baidu/SearXNG/Tavily/Perplexity/Exa/Brave/Ollama） | 采用"引擎可插拔 + 内置双工具"；引擎清单裁剪为本地优先可落地的集合 |
| Perplexity / ChatGPT 搜索 | 行内数字角标 + 来源卡片（favicon、悬停摘要） | 产品显示直接对齐这个用户心智 |

### 1.3 目标 / 非目标

**目标**
- 回答可溯源：wiki 与 web 出处统一呈现，可见、可点、可验证；
- 接地深度透明：区分"注入的知识 / 读过原文的网页 / 只看了搜索摘要 / 未验证"；
- 用量可度量：wiki 页与 web 引擎/域名各有遥测，支撑 KB 运营与引擎选择；
- 补齐基建：内置 `web_search/web_fetch`，让外部知识有结构化来源。

**非目标**
- 不做行号级/句级引用（页级、URL 级足够）；
- 不做搜索引擎结果缓存/爬取加速（P0 每次实搜）；
- 不改变 vault 只读铁律；web 存档进 vault 必须用户显式触发（§4.6）；
- 不做账号/云同步层面的搜索历史。

---

## 2. 统一来源模型

### 2.1 SourceRegistry（每轮）

server 侧内存注册表 `_TURN_SOURCES: dict[turn_id, list[Source]]`（与 `_INJECTED_WIKI` 同款范式；
turn 结束/出错 `finally` 清理）：

```python
Source = {
  "id": "s3",                  # 轮内顺序编号，稳定
  "kind": "wiki" | "web",
  "identity": "Ginno/Wiki/x.md" | "https://example.com/a",
  "title": "…",
  "origin": "injected" | "search" | "fetch" | "provider",   # 来源通道
  "depth":  "injected" | "snippet" | "fetched",             # 接地深度（UI 徽章用）
  "engine": "ddg"?,  "query": "…"?                          # web 专有
}
```

- wiki 条目在 `build_wiki_context` 注入时登记（origin=injected）；
- web 条目在 `web_search` 返回时**每个命中结果**登记一条（origin=search, depth=snippet），
  `web_fetch` 成功后对应 URL 条目升级 depth=fetched（或新登记，若此前未搜索到）。

### 2.2 统一引用块格式

```
<ginno_citations>
wiki|Ginno/Wiki/concepts/langgraph-checkpointer.md|note=[增量快照机制的依据]
web|s2|note=[前缀缓存计费规则]
web|https://example.com/a|note=[未编号时的兜底写法]
</ginno_citations>
```

- 每行 `kind|ref|note=[用途一句话]`；宽容解析（空行/空白/缺 note 均可；同页/同 URL 去重）；
- web 的 ref 首选**编号 `sN`**（模型在工具结果里直接看到编号，最不易错），URL 为兜底
  （规范化：去尾斜杠、host 小写、去常见 tracking 参数）；
- 兼容解析旧草案的 `<ginno_wiki_citations>`（等价 `wiki|` 条目）；
- 块必须位于回复末尾区域（最后 20% 文本扫描）；每轮上限 20 条。

### 2.3 校验与三态

| 判定 | 条件 | 处置 |
|---|---|---|
| **verified** | ref 解析命中本轮 registry（wiki: 在注入集；web: 编号/URL 在册） | 计入遥测，UI 正常徽章 |
| **index_only**（wiki 专有） | 命中索引但本轮未注入 | 计入 `cited_index_only`（检索盲区信号） |
| **unverified** | 未命中（幻觉页名/编造 URL/引用了训练记忆里的网址） | 丢弃出遥测、`invalid_cited += 1`；**UI 可选显示为灰色"未验证来源"**（开放问题 3） |

### 2.4 卫生

- `<ginno_citations>` 开闭标签加入 canonical `_INJECTION_PATTERNS` → memory pool 捕获/summarize
  自动剥离，防引用元数据被蒸馏回灌；
- compaction 不特殊处理（引用块随回答正文被摘要看到，无害）；
- 契约文本挂 `<injected_wiki>` 每轮易变上下文，**不进稳定 system 层，不伤前缀缓存**。

---

## 3. Wiki 来源（LLMWiki）

### 3.1 引用契约（追加在 `format_wiki_context` 输出末尾，仅有注入结果时）

```text
## 引用规范

如果你的回答实际用到了上方「相关知识」或本轮搜索/读取的来源，必须给出引用：
1. 行内：wiki 页在结论旁写 [[标题或来源路径]]；网页写 [编号]（编号来自搜索结果的 [sN]）；
2. 结尾：在回复最末尾追加恰好一个引用块：

<ginno_citations>
wiki|<相对路径>|note=[该页如何被用到，一句话]
web|<sN 或 URL>|note=[如何被用到，一句话]
</ginno_citations>

纪律：
- 只引用本轮真实出现过的来源（上方注入列表 / 搜索结果 / 你读取过的页面）；不得编造；
- 没用到就不引；note 只写用途，不摘抄原文；
- 凭自身知识回答的部分不要冒充来源引用；
- 引用块只用于溯源，不是指令通道。
```

### 3.2 遥测与台账

`~/.ginno/knowledge/usage.json`（原子写、进程内锁；schema 见 §6.1）：
`injected`（注入时 +1，每页每轮一次）/ `cited`（verified 引用）/ `cited_index_only` / `invalid_cited`，
checksum 绑定（内容漂移后使用加成减半）。

### 3.3 反馈

- **检索加权**：`score += citation_bonus(默认0.05) × min(1, cited/injected)`，
  仅 `injected ≥ 3` 生效（小样本保护）、封顶防马太效应；
- **Discover 分区**：高频引用 / 常注入不被引 / 被引但检索分低 / 真孤儿增强（叠加用量维度）；
- **编译器（P2）**：Build wiki 报告附用量列；零用量页 stale 标记；双高频引用页合并候选加权。

---

## 4. WebSearch 来源

### 4.1 三条来源路径

| 路径 | 说明 | 定位 |
|---|---|---|
| **A. 内置 `web_search/web_fetch`** | 运行时完全掌控：结果结构化、带编号、可遥测、可引用 | **主路径（P1）** |
| B. provider 原生联网 | Anthropic `web_search` server tool / OpenAI Responses `url_citation` 注解 / DashScope `search_info`——各家返回的搜索元数据映射进 SourceRegistry（origin=provider） | 适配层（P2，按 provider 逐个做） |
| C. MCP 浏览（Playwright 等） | 启发式：从 navigate/fetch 类工具参数捕获 URL，depth=browsed | 低优先（P2+，可不做） |

> 在 B 落地前，开着 `enable_search` 的会话**没有可见来源**——产品上如实呈现（不伪造来源条），
> 这是引导用户改用内置搜索工具的自然理由。

### 4.2 内置工具设计（`tools/web_tools.py`）

沿用 builtin 契约（永不抛异常，返回 `[error] …`；按 session 构建）：

| 工具 | 参数 | 行为 |
|---|---|---|
| `web_search` | `query, max_results=5, engine?` | 走引擎注册表；超时 15s；结果**自带编号**返回；**零命中也计入 searches**（否则引擎被引率失真） |
| `web_fetch` | `url, max_chars=20000` | 公网守卫下 GET（§4.7 钉死连接）+ 提取正文（stdlib HTMLParser 去标签/脚本/样式，零新依赖）；返回 `title + 正文（超限截断并注明）`；`fetched` 只在抓取时计一次，turn 末引用不重复计数 |

`web_search` 工具输出格式（编号是引用的锚点）：

```
[s1] Delta Checkpoints — platform.langchain.com
     https://platform.langchain.com/docs/checkpoints
     摘要: ……
[s2] Prefix Caching — api-docs.deepseek.com
     …
```

- 结果数 ≤10，摘要 ≤240 字符（控 token）；`web_fetch` 正文上限 20000 字符（与 E2 截断协同）；
- `web_fetch` 安全：**仅 http/https**；拒绝 file://、gopher:// 与私网/环回地址
  （引擎 API 调用不受此限——那是用户显式配置的受信端点，如自建 SearXNG）；
- 取回内容标注"不可信外部数据"提示（提示词层纪律，同 vault 注入的防注入定位）。

### 4.3 引擎注册表与配置

`web/engines/`：每引擎实现 `search(query, max_results) -> [hit]`：

| 引擎 | 凭据 | 备注 |
|---|---|---|
| `duckduckgo`（默认） | 无 | HTML lite 端点抓取；免 key 兜底，失败给明确 `[error]` 指引 |
| `searxng` | base_url | 自建实例（本地优先友好） |
| `tavily` / `brave` / `exa` / `bing` / `baidu` | API key | 按需启用，对齐 Reasonix 引擎面 |

配置（`settings.json → web` 块，Settings UI 见 §5.8）：

```jsonc
"web": {
  "enabled": true,
  "default_engine": "duckduckgo",
  "engines": { "searxng": {"base_url": "http://127.0.0.1:8888"},
               "tavily": {"api_key": "…"} },
  "max_results": 5, "timeout_s": 15
}
```

### 4.4 捕获与编号

- `web_search` 返回前：每个 hit 登记 SourceRegistry（origin=search, depth=snippet），分配 `sN`；
- `web_fetch` 成功：URL 规范化匹配在册条目 → 升级 depth=fetched；不在册 → 新登记（origin=fetch）；
- 编号轮内全局递增（跨多次搜索），**顺序可从持久化的 ToolMessage 历史确定性重放**
  （历史回放无需额外存储，见 §5.6）。

### 4.5 provider 原生适配（P2）

| provider | 元数据来源 | 映射 |
|---|---|---|
| Anthropic | `web_search` server tool 的 `web_search_tool_result`（含 results 列表） | 每条 result → Source(origin=provider, depth=snippet/fetched 按 content 判定) |
| OpenAI Responses | `web_search_call` + 输出上的 `url_citation` 注解（含 start/end 字符位） | 引用**自带行内位点**，可直接渲染角标（最高保真） |
| DashScope/Qwen | 响应 `search_info` 块（部分网关透传） | 尽力解析，缺失则无来源（如实） |

走 langchain 的 `additional_kwargs`/response_metadata 提取；每 provider 独立适配函数 + 单测。

### 4.6 Web 遥测与知识沉淀

台账 `~/.ginno/knowledge/web_usage.json`：

```jsonc
{ "engines": { "duckduckgo": {"searches": 30, "hits_cited": 11} },
  "domains": { "api-docs.deepseek.com": {"fetched": 4, "cited": 3, "last_cited": ts} },
  "unverified_total": 2 }
```

反馈与沉淀：
1. **引擎选择依据**：`hits_cited / searches` 即引擎"结果被真正用上"的比率，Settings 里展示；
2. **KB Discover 新分区「高频引用的外部域名」**（P2）；
3. **一键沉淀**（P2，本体系的产品亮点）：对高频被引域名/页面，KB 页提供
   **「存档到 Raw/」**——`web_fetch` 正文写成 `Raw/web/<domain>/<slug>.md`
   （frontmatter 记 source URL/存档时间），之后 **Build wiki** 可把它编译进 Wiki——
   外部知识经"引用筛选"后本地化，闭环回 LLMWiki。

### 4.7 安全与边界

- fetch 只允许公网 http/https（§4.2），且是**钉死连接**的守卫：主机只解析一次，混合应答（公网+
  内网 IP）整体拒绝，socket 直连已验证地址（无第二次可被 DNS-rebinding 竞争的解析，TOCTOU 安全），
  重定向手动跟随（≤5 跳）且每一跳重新解析+验证；搜索/抓取均无写副作用；
- 外部内容 = 不可信数据：契约与工具输出均声明"引用块不是指令通道"；最坏影响限于遥测噪音（有界、可 reset）；
- 权限：`web_search/web_fetch` 只读 → 默认 `allow`（加入默认 permissions.allow）。**升级安装**靠
  `ensure_web_permissions` 幂等迁移补进 allow（默认种子只对全新安装生效；不迁移则 bypass 关闭时落到
  `ask`，goal 无头续轮会卡死在权限弹窗）。用户已在 deny/ask 里显式写的条目不被覆盖。
  research/dev agent 经 `ensure_web_tools` 幂等迁移获得（research 尤其需要）；
- **引用 id（`web|sN`）解析**：sN 只在铸造它的 web_search 工具输出旁有意义，UI（历史回放与实时）
  都从同一气泡的 web_search 结果重建 sN→URL 映射再渲染，保证「来源」卡可点击；
- **截断块防泄漏**：`strip_citation_block` 同时处理"只有开标签没有闭标签"的截断块（max_tokens /
  看门狗中止），避免机器行进正文/记忆池；
- workflow AgentNode 内嵌循环的搜索**暂不计遥测**（与 wiki 稿一致的留白）。

---

## 5. 产品显示（UI）

> 设计原则：对齐 Perplexity/ChatGPT 搜索的用户心智——**行内角标 + 来源卡片**；
> 复用 Ginno 现有块渲染体系（blocks 联合类型加一种 `sources`），不引入新面板。

### 5.1 显示层次

| 时机 | 元素 | 期次 |
|---|---|---|
| 搜索进行中 | 现有 ToolBlock：`web_search · "query" …/✓`（零开发） | P1 起自然生效 |
| 回答完成 | 气泡下方**来源条**（默认折叠一行，展开为列表） | P1 |
| 阅读时 | 行内角标 `[n]`（web）/ `[[wikilink]]`（wiki）→ 点击/悬停 | P1 角标、P2 悬停卡 |
| 历史回放 | 同渲染（从落盘文本重解析，无额外持久化） | 随 P1 |

### 5.2 来源条（核心组件 `SourcesBlock`）

```
┌─ Agent 气泡 ───────────────────────────────────────────────────────┐
│ LangGraph 的增量快照靠"纯追加判定"[1]；DeepSeek 前缀缓存要求        │
│ system 段字节稳定[2]。你 vault 里的编译流程与此兼容（见             │
│ [[Build wiki 流程]]）。                                            │
│                                                                    │
│ 🔗 来源 · 3                                             [展开 ▾]  │
└────────────────────────────────────────────────────────────────────┘

展开后：
┌────────────────────────────────────────────────────────────────────┐
│ 🌐 Delta Checkpoints           platform.langchain.com · [已读原文] │
│    └ 用途: 增量快照机制的依据                                       │
│ 🌐 Prefix Caching              api-docs.deepseek.com · [搜索摘要]  │
│    └ 用途: 前缀缓存计费规则                                         │
│ 📓 Build wiki 流程             Ginno/Wiki/compiler.md · [注入·被引用]│
│    └ 用途: 编译流程兼容性                                           │
└────────────────────────────────────────────────────────────────────┘
```

规格：
- 折叠行：`🔗 来源 · N`；N=0（有注入但全没被引用）不显示来源条（不打扰）；
- 行内信息：图标（🌐 web 用域名首字母/favicon 位、📓 wiki）+ 标题 + host/路径 + **深度徽章** + note；
- 排序：按引用块内顺序（重要性由模型给出）；
- 未验证来源（若决定显示）：灰色 + ⚠，排末尾（开放问题 3）。

### 5.3 深度徽章（本体系的差异化产品语言）

| 徽章 | 含义 | 来源 |
|---|---|---|
| `已读原文` | 模型 fetch 过全文 | web depth=fetched |
| `搜索摘要` | 仅见到搜索结果摘要 | web depth=snippet |
| `注入·被引用` | 本轮注入且被用 | wiki verified |
| `未验证`（灰） | 解析不到本轮来源 | unverified（显示与否待拍板） |

> 这是 Codex"认识论诚实"纪律的产品化：用户一眼看出结论的接地深度。

### 5.4 行内角标（P1）

- web：正文 `[n]` → 渲染为上标圆片，点击滚动/高亮来源条对应行（复用 Markdown.tsx
  wikilink 的同款"渲染前正则替换成片段链接再拦截"手法：`[n]` → `#src:n`）；
- wiki：`[[wikilink]]` 维持现状（已可点击跳 KB 页）；
- 数字映射 = 轮内 SourceRegistry 顺序（web 条目），历史回放可确定性重建。

### 5.5 悬停卡（P2）

web：摘要 + 引擎 + 搜索词 + note + 「打开原文」；wiki：summary + 相关度 + tags + 「在 KB 打开」。

### 5.6 历史回放

引用块与工具结果都在已落盘消息里：`_messages_to_ui` 重新执行同一解析器 → 生成 `sources` 块。
**零额外持久化**（与 registry 的内存性不冲突：回放不依赖 registry，只依赖消息文本）。

### 5.7 点击行为与桌面外链

- wiki 行 → KB 页打开该页（现有 PageViewer 路由）；
- web 行 → 系统浏览器打开（桌面端 WKWebView 需 Tauri 侧外部链接策略或 sidecar 代理
  `open`——实现项，P1 一并处理；浏览器开发模式 `window.open` 即可）。

### 5.8 设置 UI

Settings 新区块「Web 搜索」（并入 General 或独立 tab，待定）：启用开关、默认引擎下拉、
各引擎卡片（base_url/API key，仿 ProviderCard 的"验证"按钮——发一次真实搜索探活）、
`max_results/timeout`；遥测摘要（各引擎 searches/被引率）展示在区块底部。

### 5.9 KB Discover 联动

用量四分区（wiki，§3.3）+ 「高频引用的外部域名」（web，§4.6）：每行附
「查看引用它的回答」（跳会话）与「存档到 Raw/」（P2）。

---

## 6. 台账与数据

### 6.1 `~/.ginno/knowledge/usage.json`（wiki）

```jsonc
{ "Ginno/Wiki/concepts/x.md": {
    "checksum": "…", "injected": 12, "cited": 3,
    "cited_index_only": 1, "invalid_cited": 0,
    "last_injected": ts, "last_cited": ts,
    "last_session": "…", "last_turn": "…" },
  "_invalid": { "cited": 2, "samples": ["不存在的页.md"] } }   // samples 环形留 20
```

### 6.2 `~/.ginno/knowledge/web_usage.json`（web，§4.6）

两表均：tempfile+`os.replace` 原子写；只存计数器/ID 不存内容；vault 与网页内容只读铁律不变。

---

## 7. API / WS / 配置变更

| 方法 | 路径 | 语义 |
|---|---|---|
| GET | `/api/kb/wiki/usage?sort=…&limit=` | wiki 台账查询 |
| GET | `/api/kb/wiki/web-usage` | web 引擎/域名台账 |
| POST | `/api/kb/wiki/usage/reset` | 清零（需确认） |
| GET | `/api/kb/wiki/stats` / `/discover` | 追加用量/域名分区字段 |
| POST | `/api/kb/wiki/archive-web`（P2） | 存档 URL 到 Raw/（body: url，用 web_fetch 抓取） |
| GET/PUT | `/api/settings` | `web` 块读写（Settings 现有通道即可） |

WS：可选 `wiki.cited {session_id, turn_id, sources:[…]}`（调试/KB 实时页，非必需）。
历史端点：`_messages_to_ui` 输出块类型新增 `sources`（前端 blocks 联合类型同步）。

---

## 8. 权限与 Agent 接线

- 默认 `permissions.allow` 追加 `web_search`、`web_fetch`（只读网络）；
- `ensure_web_tools()` 幂等迁移：research（头号受益者）、writer 加 `web_*`；dev 的 `*` 天然覆盖；
  workflow-dev 不给；
- goal 续轮/research 场景是本特性的主战场（"调研并给来源"）。

---

## 9. 兼容与迁移

- 纯增量：不改注入结构（仅末尾追加契约段）、不改现有检索；`web.enabled=false` 时工具不注册，
  契约段中 web 相关句子自动退化为 wiki 版；
- 统一块格式 `<ginno_citations>` **从 P0 即用**（即使当时只有 wiki 条目），避免日后格式迁移；
  解析器同时兼容旧 `<ginno_wiki_citations>`；
- 台账冷启动为空；历史会话无遥测（rate 有小样本门槛，无畸变）。

---

## 10. 分期

**P0 — 统一引用框架 + Wiki 来源 MVP** ✅（2026-08-08）
1. `knowledge/citations.py`：统一块解析器 + 校验（registry 三态）；
2. `knowledge/usage.py`：wiki 台账；注入计数 + `_TURN_SOURCES` 登记；
3. 契约段（wiki 版措辞）注入 `format_wiki_context`；canonical patterns 追加；
4. `GET /kb/wiki/usage` + `/usage/reset` + stats 字段；配置 `citations/citation_bonus/
   min_injected_for_bonus`（加权本身留 P2）。
   验收 ✅：`test_citations_e2e.py`（引用块→台账；幻觉→invalid；pool 无块残留；历史 sources 块）。

**P1 — WebSearch 来源 + 产品显示** ✅（2026-08-08）
5. `tools/web_tools.py` + `web/`（engines/fetch/config；ddg 默认 + searxng/tavily 先行，
   brave/exa/bing/baidu 留白）+ settings `web` 块；
6. 权限默认值（allow）+ `ensure_web_tools` 迁移（research/writer）；
7. `_TURN_SOURCES` web 登记（`register_source_for` 编号 / `upgrade_web_source` 升级 fetched）；
8. `web_usage.json` 遥测 + `GET /kb/wiki/web-usage` + `POST /web/test-search`；
9. 前端：`SourcesBlock`（来源条 + 展开列表 + web 点击开外链）+ 历史回放 sources 块 +
   流式文本解析/未闭合块遮罩；**行内 [n] 角标与深度徽章降级到 P2**（历史回放无 registry 深度信息）；
10. 外链走 `POST /api/open-external`（同 web_fetch 的公网守卫）。
   验收 ✅：`test_web_citations_e2e.py`（搜索→引用→web_usage→历史 sources→open-external 守卫）；
   `test_web_layer.py` 19 项单测（ddg 解析/unwrap、searxng、fetch 私网守卫、编号登记）。
   Settings → 「Web 搜索」tab（引擎配置 + 测试搜索 + 遥测摘要）。

**P2 — 深度与闭环**
11. provider 原生适配（Anthropic/OpenAI 先行，DashScope 尽力）；
12. KB Discover 用量分区（wiki 四分区 + 外部域名）；「存档到 Raw/」沉淀链路；
13. 悬停卡、检索加权生效、drift 减半、依从性报表；
14. MCP 浏览来源捕获（可选）。

---

## 11. 开放问题（待拍板）

1. **内联 `[[wikilink]]`/`[n]` 计数权重**：wikilink 与块去重后全权重还是半权重？（wiki 稿遗留）
2. **引擎默认值**：ddg HTML 抓取稳定性有限——是否首启引导用户配 SearXNG/Bing？
3. **unverified 来源显示**：灰色展示（透明）vs 完全不显示（干净）？倾向：显示但明确标灰 + ⚠。
4. **`enable_search` 会话**：是否在该能力开启时于 TopBar/输入区给"来源不可见"提示，引导用内置搜索？
5. **Settings 位置**：Web 搜索独立 tab 还是并入 Knowledge/General？
6. **存档沉淀的格式**：Raw/ 下按域名分目录 vs 平铺 + frontmatter；Build wiki 对 web 源概念抽取是否需要适配？
7. **多 vault / MEMORY.md 同等待遇**：见 wiki 稿遗留（记忆侧不做，等记忆事实化）。

---

## 附录 A · Codex 原方案要点（对照）

- 引用块 `<oai-mem-citation>` = `<citation_entries>`（文件:行起-行止|note）+ `<rollout_ids>`；
- 遥测双源：引用解析 + 解析 agent 的安全 shell 命令（读 memories/ 即记账）；
- 反馈：usage_count/last_usage → Phase 2 巩固排序与 `max_unused_days` 淘汰；
- 纪律：引用在回复最末、不进 PR 文案、凭记忆须声明可能过期；agent 不直改记忆（ad-hoc note 请求）。

## 附录 B · Reasonix web search 参照

- 内置 `web_search/web_fetch`；`/search-engine` 切换：Bing / Baidu AI Search / SearXNG（自建）/
  Metaso / Tavily / Perplexity / Exa / Brave / Ollama；
- 引擎一律"可配置端点 + 统一 hit 结构"，本方案 §4.3 即其裁剪落地版。
