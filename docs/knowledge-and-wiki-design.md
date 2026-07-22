# Ginno 知识库与自动总结方案设计

> 参考 Molly 的两套子系统，为 Ginno 增加：
> 1. **LLMWiki 知识库**——把 Obsidian vault 编译成可检索的 wiki，并在每次对话时按相关性注入到 system prompt。
> 2. **自动总结知识（Memory Refinery）**——对话内容自动捕获 → LLM 提炼 → 沉淀为长期记忆并回注。
>
> 本文档覆盖从 **UI → HTTP API → LangGraph 运行时 → 磁盘存储** 的完整链路，并给出可直接落地的分阶段路线图。

---

## 1. 背景与目标

### 1.1 参考对象（Molly）

Molly 有三个互相衔接的知识子系统，最终都汇聚到「注入 system prompt」这一个出口：

| 子系统 | 原始存储 | 蒸馏存储 | 注入方式 |
|---|---|---|---|
| **WikiLLM** | Obsidian vault（markdown + frontmatter） | 内存索引（无向量库） | `<injected_wiki>`，按 query 检索 top-K |
| **Memory（轮级）** | `memory/pool/*.jsonl`（每轮 assistant 文本） | `memory/public.md`（LLM 合并） | `<injected_memory>` |
| **Co-Copilot（经验级）** | `co-copilot/experiences.jsonl`（LLM 抽取的结构化经验） | 复用 public.md 管线 | 同 Memory |

**Molly 的关键事实（移植时要带着走）**：
- Wiki 检索**不靠 embedding**，而是多信号词法打分（tag 0.4 / title 0.3 / summary 0.15 + wikilink 加成 + 时间衰减），「语义」相似度用 **TF-IDF 余弦**近似。中文用 unigram/bigram/trigram 切词。
- Wiki 编译器是**确定性正则**（抽取 `**加粗**` 与 `` `行内代码` `` 作为概念），LLM 只「消费」wiki，不参与编译。
- 记忆总结由 LLM 完成（`SUMMARIZE_PROMPT`），触发点为：手动 CLI、HTTP、达到最大轮次、经验晋升。**没有定时器、不是每轮都总结。**
- 注入内容用 XML 标签包裹（`<injected_wiki>` / `<injected_memory>`）并声明「权威层级」，提示模型把注入内容当「数据」而非「指令」（防注入）。

### 1.2 Ginno 现状（差距分析）

| 能力 | 现状 | 缺口 |
|---|---|---|
| KB 浏览 | `apps/web/src/app/kb/page.tsx` 仅通过 MCP 列文件/搜文件 | 无 wiki 编译、无相关性检索、无注入 |
| 记忆 | `agents/memory.py` 每个 agent 有独立 `MEMORY.md`，注入到该 agent 的 prompt | 无自动捕获、无 LLM 总结、无跨 agent 的全局记忆 |
| 语义检索 | `architecture.md` P4 规划了 LanceDB，`vectorstore/` 目录已预留 | 完全未实现；`memory.recall` / `obsidian.recall` 工具未实现 |
| Hooks | `hooks/dispatcher.py` 已实现，但运行时只在 permission 节点触发 `PreToolUse` | 无 `Stop`/轮结束钩子，无捕获落点 |
| 记忆工具 | 无 | 计划中的 `memory.save/read/forget/recall` 未实现 |

**结论**：Ginno 已具备承载本方案的骨架（`~/.ginno/` 文件布局、LangGraph 运行时、hooks、每 agent 记忆、MCP KB），缺的是「编译/检索/注入」与「捕获/总结/回注」两条管线。

### 1.3 目标

- **G1（LLMWiki）**：配置一个 Obsidian vault → 自动扫描建立内存索引 → 用户提问时检索 top-K 相关条目并注入 system prompt；提供编译（raw→wiki）、搜索、统计、关系发现等能力与对应 UI。
- **G2（自动总结）**：每轮对话自动捕获 assistant 产出 → 达到阈值或用户触发时，用 LLM 提炼为长期记忆 → 回注所有 agent 的 prompt。
- **G3（可选·经验循环）**：用 LLM 从会话中抽取结构化经验（knowledge/error/topic/todo），人工审核后晋升为长期记忆。

**非目标**：不引入数据库（沿用 Ginno「无 DB、全文件」原则）；默认不引入 embedding 依赖（语义检索作为可选增强）。

---

## 2. 总体架构

### 2.1 在 Ginno 进程拓扑中的位置

沿用现有「Tauri 壳 + Next.js UI + Python sidecar」三段式，新增能力全部落在 Python runtime 与 Next.js UI：

```
┌────────────────────────────────────────────────────────────────────┐
│  Tauri Shell                                                         │
│  ┌──────────────────────────┐   ┌────────────────────────────────┐  │
│  │  Next.js webview          │◄─►│  Python sidecar (FastAPI+LG)    │  │
│  │  - KB 页(检索/统计/编译)   │   │  ┌──────────────────────────┐  │  │
│  │  - Memory 面板(总结/审核)  │   │  │ knowledge/  (新增)         │  │  │
│  │  - Settings→知识 配置      │   │  │  indexer / retriever /     │  │  │
│  └──────────────────────────┘   │  │  compiler / association /  │  │  │
│                                   │  │  memory(summarize/pool) /  │  │  │
│                                   │  │  injection                 │  │  │
│                                   │  └──────────────────────────┘  │  │
│                                   │  graph.py 在 build prompt 时    │  │
│                                   │  调用 injection 注入 wiki+memory│  │
│                                   └──────────────┬─────────────────┘  │
└──────────────────────────────────────────────────┼─────────────────────┘
                         ┌──────────────────────────┴───────────────┐
                         │  ~/.ginno/  +  Obsidian vault (直读文件系统) │
                         └───────────────────────────────────────────┘
```

### 2.2 两条数据流

**流 A · LLMWiki（读路径，每轮）**
```
用户提问
   ↓ (agent_node 取最后一条 HumanMessage 作为 query)
build_agent_system_prompt(query=...)
   ↓
knowledge.retriever.retrieve(query, top_k=5, min_score=0.3)   ← 内存索引
   ↓
注入 <injected_wiki>（相关条目 + 自动发现的相关页）
   ↓
LLM 回答（带着相关知识）
```

**流 B · 自动总结（写路径，异步）**
```
每轮对话结束（message.end 且非 interrupt）
   ↓
server 把本轮 assistant 全文 sanitize 后 append 到 memory/pool/*.jsonl
   ↓ (pool 达到阈值 / 用户点「总结」/ /summarize 技能)
knowledge.memory.summarize_pool()
   ↓  LLM(build_model 默认 provider) + SUMMARIZE_PROMPT
合并「现有 MEMORY.md + pool 摘录」→ 覆写 ~/.ginno/MEMORY.md → 清空 pool
   ↓
下一轮 build prompt 时，全局 MEMORY.md 注入 <injected_memory>（所有 agent 共享）
```

---

## 3. 数据模型与存储

### 3.1 `~/.ginno/` 目录扩展（在 `paths.py` 中新增路径助手）

```
~/.ginno/
├── MEMORY.md                  # 全局蒸馏记忆（已有，改为总结输出目标 + 注入源）
├── memory/
│   ├── pool/                  # 新增：原始捕获缓冲（每轮一个 jsonl，总结后清空）
│   │   └── <ts>.jsonl
│   └── entries/               # （可选）结构化记忆条目
├── knowledge/                 # 新增：wiki 运行时数据
│   ├── index.json             # 索引快照缓存（可选，重启加速；权威源仍是 vault）
│   └── assoc_cache.json       # 关联图缓存（TTL 5min）
├── experiences.jsonl          # 新增（G3）：结构化经验
├── watermarks.json            # 新增（G3）：增量分析水位
├── settings.json              # 扩展 knowledge 配置块
└── projects/<slug>/sessions/  # 既有：文件 checkpointer（总结经验时可读会话历史）
```

**Obsidian vault（用户自有目录，直读文件系统）**——沿用 Molly 的目录约定，可配置：
```
<vault_path>/
├── Ginno/
│   ├── Raw/        # 原始文档（用户/agent 写入这里）
│   ├── Wiki/       # 编译产物（/kb build 生成，禁止手写）
│   │   ├── INDEX.md
│   │   ├── concepts/  projects/  decisions/
│   ├── Research/   # 深度研究报告
│   └── Memory/     # （可选）记忆副本
```

### 3.2 Wiki 条目（`knowledge/types.py`）

一个 wiki 条目就是一个带 YAML frontmatter 的 markdown 文件。Python 数据类：

```python
@dataclass
class WikiEntry:
    path: str              # 绝对路径
    relative_path: str     # 相对 vault
    title: str             # frontmatter.title → 首个 H1 → 文件名
    summary: str           # frontmatter.abstract/summary → 正文首段(≤200 字)
    tags: list[str]
    links: list[str]       # 正文中的 [[wikilinks]]
    modified: float        # mtime
    checksum: str          # sha256(原文)
    type: str | None = None
    confidence: str | None = None   # high|medium|low
    sources: list[str] = field(default_factory=list)
```

**Frontmatter 识别键**（`parse_frontmatter`，移植 Molly 的轻量 YAML 解析，支持标量/行内数组/块数组/一层嵌套）：
`title` / `abstract`|`summary` / `tags` / `type` / `confidence` / `sources`。

> **关于权限**：Molly 用 frontmatter 的 `permission` 做文档级 ACL。Ginno 是**单机个人**应用，默认**不引入 ACL**（所有条目可读），保留 `permission` 字段解析但不强制，作为未来多端/共享场景的扩展点。

### 3.3 捕获池条目（`memory/pool/*.jsonl`）

```json
{"session_id": "…", "agent_id": "dev", "timestamp": "2026-07-20T01:29:59Z", "content": "…本轮 assistant 全文(已清洗)…"}
```

### 3.4 经验条目（G3，`experiences.jsonl`）

```python
@dataclass
class Experience:
    id: str
    kind: str                 # error | knowledge | topic | todo
    title: str                # 一句话摘要（去重/检索键）
    body: str
    tags: list[str]
    confidence: float         # 0..1
    status: str               # pending | promoted | dismissed | done
    source_session_id: str
    created: float; updated: float
    # kind 特有字段：
    root_cause: str | None; fix: str | None      # error
    question: str | None; key: str | None; value: str | None  # knowledge
    rationale: str | None                          # topic
```

---

## 4. 后端设计（Python runtime）

新增包 `packages/runtime/src/ginno_runtime/knowledge/`：

```
knowledge/
├── __init__.py
├── types.py          # WikiEntry / RetrievalResult / Experience / 配置 dataclass
├── config.py         # 读 settings.json 的 knowledge 块（vault_path/各目录/开关）
├── frontmatter.py    # parse_frontmatter / extract_summary / extract_wikilinks
├── tokenize.py       # 中文 n-gram + 拉丁切词（检索与关联共用）
├── indexer.py        # WikiIndexer：扫描/增量/缓存
├── retriever.py      # WikiRetriever：多信号打分 + wikilink 加成
├── association.py    # AssociationEngine：TF-IDF 余弦 + tag/co-occur/temporal/hierarchy
├── compiler.py       # WikiCompiler：raw→concept 页（确定性基线 + 可选 LLM 增强）
├── injection.py      # build_wiki_context(query) → <injected_wiki> 文本
├── memory_pool.py    # append_to_pool / read_pool / clear_pool
├── summarize.py      # summarize_pool()：LLM 提炼 MEMORY.md（含 SUMMARIZE_PROMPT）
└── (G3) analyzer.py / experience_store.py / promote.py
```

### 4.1 配置（`knowledge/config.py` + `settings.json`）

在 `settings.json` 增加 `knowledge` 块（沿用「默认值合并」模式，参考 `providers.py`）：

```jsonc
{
  "knowledge": {
    "enabled": false,              // 总开关；false 时整个子系统不加载
    "vault_path": "",              // Obsidian vault 根目录（必填才启用）
    "raw_dir": "Ginno/Raw",
    "wiki_dir": "Ginno/Wiki",
    "research_dir": "Ginno/Research",
    "auto_inject": true,           // 是否每轮检索并注入 wiki
    "inject_top_k": 5,
    "inject_min_score": 0.3,
    "rescan_interval_s": 60,       // 内存索引重建间隔
    "use_semantic": false,         // 可选：LanceDB 语义检索（需 rag extra）
    "memory": {
      "capture": true,             // 是否每轮捕获 assistant 文本到 pool
      "auto_summarize": true,      // pool 达阈值时自动总结
      "pool_flush_threshold": 30,  // pool 累计轮数阈值
      "summarize_model": "",       // 空 → 用 default_provider
      "memory_budget_chars": 3000
    }
  }
}
```

> **直读文件系统 vs MCP**：索引需要批量读全部文件，走 MCP 逐次调用太慢。因此 wiki 索引/编译**直读 vault 文件系统**（与 Molly 一致）；现有 `/kb/*`（MCP）端点保留为「实时单点查询」的补充。`vault_path` 与 MCP filesystem server 的目录通常指向同一处。

### 4.2 索引器（`indexer.py`）

```python
class WikiIndexer:
    def scan(vault_path) -> None            # 全量：递归 parse_file，建 entries + backlinks
    def incremental_scan() -> Diff          # mtime 快筛 + sha256 确认，返回 added/updated/removed
    def get_entries() / get_all_tags() / find_by_title() / get_backlinks(title) / get_orphans()
```
- 跳过目录：`.obsidian .trash node_modules .git .vscode` + 所有点目录；扩展名 `.md/.markdown`。
- 共享单例 + `rescan_interval_s` 定时（首次或超时全量，否则增量）。**索引在内存，不落库**；可选把快照写 `knowledge/index.json` 作重启加速缓存。

### 4.3 检索器（`retriever.py`）——移植 Molly 多信号打分

**切词** `tokenize_query(q)`：中文段（含 `[一-龥]`）输出 unigram+bigram+trigram；非中文按 `[\w-]` 切、长度≥2；去重。

**打分** `score_entry(entry, tokens)`（每个字段对每个 token 至多计一次）：

| 信号 | 权重 | 命中条件 |
|---|---|---|
| tag | +0.4 | `tag⊇token` 或 `token⊇tag`（双向子串） |
| title | +0.3 | `title.includes(token)` |
| summary | +0.15 | `summary.includes(token)` |
| 时间衰减 | +0.05×(1−days/7) | 仅当 score>0 且 7 天内修改 |

封顶 1.0，记录 `matched_terms`（如 `tag:x`/`title:y`）。

**Wikilink 图加成**：把 score≥0.3 结果的 `links` 收集起来；对「title 是链接目标但自身 score<0.3」的结果 +0.1（封顶），标记 `wikilink`。

**`retrieve(query, top_k=5, min_score=0.3)`**：切词 → 全量打分 → 取 `score≥min_score` → wikilink 加成 → 降序取 top_k → 返回 `RetrievalResult{entry, score, matched_terms, snippet(summary[:300])}`。

### 4.4 关联引擎（`association.py`）——自动发现相关页（TF-IDF，无 embedding）

成对计算（跳过自身/近重名/同源 sibling/已显式链接），五信号加权：

| 信号 | 权重 | 说明 |
|---|---|---|
| semantic | 0.35 | **TF-IDF 余弦**（正文代理 = summary+tags+title；IDF=log(N/df)+1） |
| tag_overlap | 0.25 | tag 集合 Jaccard |
| co_occur | 0.20 | 共被引：都链接到它们的页面集合 Jaccard |
| temporal | 0.10 | exp(−|Δmtime|/7d)，>0.3 计 |
| hierarchy | 0.10 | A 提到 B 标题 + 覆盖≥50% B 的 tag + A 体量≥1.5×B |

合成 `score=Σ(val·w)` 封顶 1.0，`dominant_type` 取最高单信号；`score≥0.3` 保留为边。
对外：`find_related(title, top_k=10)`、`discover()`（强关联≥0.8 / 聚类 / 孤儿桥接 / 合并候选≥0.75）。聚类：score≥0.5 建邻接、BFS 连通分量、密度≥0.4 保留。

### 4.5 编译器（`compiler.py`）——raw → wiki 页

**基线（确定性，零 LLM，移植 Molly）**：
1. `extract_concepts(text)`：抽取 `**加粗**` 与 `` `行内代码` `` 作为概念（带 ±50 字上下文），按小写去重。
2. `summary` = 首个 >20 字非标题段落，否则前 200 字。
3. 对前 10 个概念，在 `Wiki/concepts/` 生成/更新概念页（`generate_concept_page`，含 frontmatter + `## Related`）。
4. 生成汇总页 `Wiki/<name>.md`，含 `## Key Concepts` 链接。
5. 自动关联：对新建页 `find_related(top_k=5)`，`score≥0.7` 自动写入 `## Related`（`[[title]]`），否则记为建议。
6. `update_index()` 重新生成 `Wiki/INDEX.md`（按目录分组）。

**增强（可选·LLM，让它真正「LLMWiki」）**：`compile_with_llm(raw_path)` 用 `build_model(默认 provider)` 生成更准的 `summary`、概念列表与标签（提示词约束「只输出 JSON」），替换第 1–2 步的正则抽取。用 `knowledge.compiler_llm: bool` 开关，默认关闭以保证离线可用。

- **ingest**（单文件）vs **build**（全 vault，过滤掉 `wiki/`、`.obsidian/` 等）。
- **索引范围（实现细化）**：检索/关联索引**只索引 `wiki_dir` 子树**（`WikiIndexer(include_dirs=[wiki_dir])`；编译后的 Wiki 页才是“知识”，`raw_dir`/`research`/`memory`/根零散笔记都不进索引）。因此**导入已编译好的 LLM Wiki（如 `Molly/Wiki`）无需重新编译**，直接索引即可。编译器内部的自动关联同样只看 `wiki_dir`，**绝不改写原始文档**；`_raw_files` 也只编译 `raw_dir`（避免对 Molly 这类 vault 误编译 Research/Todo/Memory）。同源文档产出的概念互为“兄弟”，由 skip 规则刻意跳过、不互相关联。
- **导入已存在的 Wiki**：新增只读 `GET /kb/wiki/probe?path=`，自动识别 `<命名空间>/Wiki`（或根 `Wiki`）布局并返回 `wiki_pages/raw_pages/has_index/total_md`；UI（KB 页导入面板 + 设置→知识库）据此预填 `wiki_dir/raw_dir` 并「保存并索引」（`PUT /kb/wiki/config` + `POST /kb/wiki/index`），不触发 build。

### 4.6 注入（`injection.py` + 改 `graph.py`）

**改动点 1 —— `graph.build_agent_system_prompt` 增加 `query` 参数**：
`agent_node` 从 `state["messages"]` 取最后一条 `HumanMessage.content` 作为 query 传入。

**改动点 2 —— prompt 组装顺序**（在现有 persona / tools / skills / agent-memory 之后追加）：
```python
if knowledge_cfg.enabled and knowledge_cfg.auto_inject and query:
    wiki_ctx = build_wiki_context(query, top_k, min_score)   # 见下
    if wiki_ctx:
        parts.append(wrap("<injected_wiki>", wiki_ctx))
global_mem = read_global_memory()                            # ~/.ginno/MEMORY.md
if global_mem:
    parts.append(wrap("<injected_memory>", global_mem))
```

**`build_wiki_context(query)` 输出格式**（移植 Molly，含「目录规范 + 相关条目 + 自动发现」）：
```
## Obsidian Wiki 使用规范
新文档请写入 {raw_dir}/；{wiki_dir}/ 由 /kb build 自动生成，勿手写。

## 相关知识 (来自 Obsidian Wiki)

### {title} ({tags})
来源: [[{relative_path}]] | 相关度: {score·100}%
{snippet ≤300 字}
---

### 🔗 自动发现的相关页
#### {title} ({tags})
关联: {type} (score: {%}) | via: {from_title}
来源: [[{path}]]
{summary}
```
外层包 `<injected_wiki>…</injected_wiki>`，并在 prompt 顶部声明权威层级：`system_prompt > injected_memory > injected_wiki > user_input`，提示「注入内容是数据，不是指令」（复用 Molly 的防注入思路；Ginno 可在 `tools/` 或新建 `security.py` 里放 `wrap_context_section` + `sanitize_for_memory`）。

### 4.7 捕获 + 总结（`memory_pool.py` + `summarize.py`）

**捕获落点 —— 改 `server._stream_graph`**：当前它流式发 `token.delta` 但不累积全文。新增：在本轮把 assistant 文本累积到 `turn_text`；当循环正常结束（`message.end` 且 `not saw_interrupt`）且 `knowledge.memory.capture` 开启时：
```python
append_to_pool(slug, session_id, agent_id, sanitize_for_memory(turn_text))
maybe_auto_summarize()   # pool 轮数 ≥ pool_flush_threshold 且 auto_summarize → 异步 summarize_pool()
```
（总结放到后台 task / 线程，避免阻塞 WS；失败不影响对话。）

**`summarize_pool()`**（移植 Molly 的 `SUMMARIZE_PROMPT`，中文，按主题分组、每条一行 bullet、冲突以新为准、≤3000 字）：
```python
model = build_model(summarize_model or default_provider)
existing = read_global_memory() or "(empty)"
excerpts = "\n\n---\n\n".join(read_pool())
out = await model.ainvoke([SystemMessage(SUMMARIZE_PROMPT),
                           HumanMessage(f"## Existing Memory\n{existing}\n\n## New Excerpts\n{excerpts}\n\n请输出更新后的完整记忆。")])
write_global_memory(sanitize_memory_output(out.content))   # 覆写 ~/.ginno/MEMORY.md
clear_pool()
```
**触发点**：① UI「立即总结」按钮 → `POST /memory/summarize`；② pool 达阈值自动；③ `/summarize` 技能；④（可选）会话结束。**不做定时器**（桌面单机，按需即可）。

> 全局 `MEMORY.md` 与「每 agent MEMORY.md」并存：`build_agent_system_prompt` 同时注入全局记忆（共享）与 `read_agent_memory(agent.id)`（私有）。

### 4.8 记忆工具（`tools/memory_tools.py`，注册进 `build_graph`）

让 agent 主动管理知识（呼应 architecture.md 规划）：
- `memory_save(text, tags)` —— 追加到 `memory/entries/`（或 pool）。
- `memory_recall(query)` —— 词法检索 `MEMORY.md` + entries（可选语义）。
- `obsidian_recall(query)` —— 走 wiki `retriever.retrieve`，返回 top-K 条目。
- `memory_summarize()` —— 手动触发 `summarize_pool()`。

工具命名进入 `build_graph.all_tools`；权限默认 `memory_*` allow、写 vault 的工具走 ask。

### 4.9 （G3，可选）经验循环

- `analyzer.py`：取目标会话历史，按 watermark 取增量（`< min_new_turns=4` 跳过），截断 24000 字，用 `EXTRACT_PROMPT`（中文，严格输出 JSON 数组）抽取 `Experience[]`，去重键 `${kind}::normalize(title)`，写 `experiences.jsonl`（status=pending）。LLM 失败不推进 watermark。
- `promote.py`：`promote(id)` → 把经验格式化为文本 append 到 pool →（可选）立即 `summarize_pool()` → 标记 promoted。
- 触发：UI「分析」按钮 / `POST /knowledge/experiences/analyze`；可选后台间隔（默认 30min，桌面端默认关闭）。

---

## 5. HTTP API 设计（扩展 `server.py`）

所有端点在 `knowledge.enabled=false` 或 `vault_path` 未配置时返回 `{"ok": false, "error": "knowledge not configured"}`。

### 5.1 Wiki
| 方法 | 路径 | 说明 | 响应要点 |
|---|---|---|---|
| GET | `/kb/wiki/search?q=&tag=` | 检索（top_k=10,min=0.2） | `results[{title,path,tags,summary,score,matched_terms}]` |
| GET | `/kb/wiki/list` | 条目列表 | `pages[{title,path,tags,modified}]` |
| GET | `/kb/wiki/stats` | 统计 | `{total_pages,pages_by_dir,total_links,unique_tags,last_indexed}` |
| POST | `/kb/wiki/index` | 重建索引 | `{ok,indexed,tags}` |
| POST | `/kb/wiki/ingest` `{path}` | 编译单文件 | `{created[],updated[],new_links,associations[]}` |
| POST | `/kb/wiki/build` | 全 vault 编译 | `{scanned,created[],updated[],duration_ms}` |
| GET | `/kb/wiki/related?title=&top_k=` | 相关页 | `RelatedPagesResult` |
| GET | `/kb/wiki/discover` | 发现报告 | `{strong[],clusters[],merge_candidates[],isolated[],orphan_bridges[]}` |
| GET | `/kb/wiki/orphans` / `/kb/wiki/backlinks?title=` | 孤儿/反链 | … |

> 现有 `/kb/servers`、`/kb/search`、`/kb/list`（MCP）保留不动，作为实时单点查询。

### 5.2 Memory
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/memory` | 读全局 `MEMORY.md` + pool 计数 |
| POST | `/memory/summarize` | 立即总结（返回 `{ok,summarized_chars,pool_entries,message}`） |

### 5.3 Experiences（G3）
`GET /knowledge/experiences?status=`、`POST /knowledge/experiences/analyze`、`POST /knowledge/experiences/{id}/promote|dismiss|done`。

---

## 6. UI 设计（Next.js）

复用现有视觉语言（`bg-panel`/`text-txt`/`pill`/`field`、lucide 图标、`agentHex`）。三处改动：**KB 页升级**、**右侧面板新增 Knowledge/Memory tab**、**Settings 新增「知识」tab**。

### 6.1 升级 KB 页（`apps/web/src/app/kb/page.tsx`）

从「MCP 文件列表」升级为「检索 + 统计 + 编译 + 发现」工作台：

```
┌ Knowledge Base                                   [Rebuild index] [Build wiki] ┐
│  vault: ~/ObsidianVault   ·  128 pages · 340 links · 56 tags · indexed 2m ago │
│ ┌──────────────────────────────────────────────────────────────────────────┐ │
│ │ 🔍 搜索知识…                                                  [Search]    │ │
│ └──────────────────────────────────────────────────────────────────────────┘ │
│  RESULTS (relevance)                                                          │
│  📄 LangGraph 权限节点设计            tag:arch  92%                            │
│     来源: Ginno/Wiki/concepts/permission.md                                   │
│     命中: title:permission · tag:arch                                          │
│     └ 权限节点按 deny→ask→allow 顺序匹配…                                      │
│  📄 文件 Checkpointer                 85%   …                                  │
│  ────────────────────────────────────────────────────────────────            │
│  🔗 自动发现: Checkpointer原子写 (semantic 78% via 权限节点)                    │
│                                                                                │
│  [Tabs: 搜索结果 | 全部页面 | 统计 | 发现/孤儿]                                 │
└────────────────────────────────────────────────────────────────────────────────┘
```
- 搜索框 → `GET /kb/wiki/search`，卡片展示 title/tags/相关度%/来源/命中信号/snippet。
- 「Build wiki」→ `POST /kb/wiki/build`，显示 created/updated/耗时。
- 「统计」tab：pages_by_dir、top tags（pill 云）。
- 「发现」tab：强关联 / 聚类 / 合并候选 / 孤儿（来自 `/kb/wiki/discover`）。

### 6.2 右侧面板新增 tab（`components/right/RightPanel.tsx`）

在 `TODO | Workflow | Artifacts` 后增加 **`Knowledge`** 与 **`Memory`**：

```
[TODO] [Workflow] [Artifacts] [Knowledge] [Memory]
─── Knowledge（当前会话注入的知识）──────────────
  本轮注入 3 条 · auto_inject: on
  📄 LangGraph 权限节点  92%
  📄 文件 Checkpointer   85%
  [在 KB 页打开] [刷新]
─── Memory（长期记忆）──────────────────────────
  全局记忆 1,842 字 · 待总结 pool: 7 轮
  ┌ 最近提炼 ┐
  ## 架构与技术栈
   • 运行时用 LangGraph 动态图…
   • 无数据库，全文件存储…
  [ 立即总结 (7) ]  [ 查看 pool ]  [ 清空 ]
```
- Knowledge tab：展示**当前会话最近一轮**注入的条目（需要 server 在 WS 事件里附带 `wiki.injected` 事件，或前端按最近 query 调 `/kb/wiki/search`）。
- Memory tab：展示 `MEMORY.md` 摘要 + pool 待总结轮数 + 「立即总结」按钮（`POST /memory/summarize`）。

### 6.3 Settings 新增「知识」tab

`SettingsNav.tsx` 的 `MAIN` 增加 `{ id:"knowledge", label:"知识库", icon:BookOpen }`；`settings/[tab]/page.tsx` 的 `generateStaticParams` 加 `knowledge`；新建 `KnowledgeSettings.tsx`：

```
知识库
  [x] 启用知识库
  Vault 路径:  [ /Users/…/ObsidianVault        ] [选择目录]
  Raw 目录:    [ Ginno/Raw   ]   Wiki 目录: [ Ginno/Wiki ]
  [x] 每轮自动注入相关知识   Top-K [5]  最小相关度 [0.3]
  [ ] 使用语义检索 (需安装 rag 依赖)
  自动总结
  [x] 捕获每轮对话     [x] 达到阈值自动总结   阈值 [30] 轮
  总结模型: [ 跟随默认 ▾ ]     记忆预算 [3000] 字
  [ 保存 ]   [ 测试检索 ]
```
- 「选择目录」在 Tauri 下用原生 dialog（`__TAURI__`），dev 下退化为文本输入。
- 保存到 `settings.json` 的 `knowledge` 块（走现有 `PUT /settings` 或新增 `PUT /knowledge/config`）。

---

## 7. 分阶段路线图

| 阶段 | 范围 | 完成标准 |
|---|---|---|
| **P0 · Wiki 只读检索 + 注入** | `config/frontmatter/tokenize/indexer/retriever/injection` + `graph.py` 注入改造 + `GET /kb/wiki/{search,list,stats}` + KB 页搜索/统计 | 配好 vault 后，提问能在 prompt 注入 top-K 相关条目，KB 页可搜索 |
| **P1 · 编译 + 关联 + 发现** | `compiler`（确定性基线）+ `association` + `POST /kb/wiki/{index,ingest,build}` + `GET /kb/wiki/{related,discover,orphans}` + KB 页「Build/发现」 | KB 页 **Build wiki** / `POST /kb/wiki/build` 生成 concept 页与 INDEX（无 `/kb build` 命令）；发现页展示关联/聚类 |
| **P2 · 自动总结记忆** | `memory_pool/summarize` + `server` 捕获改造 + `tools/memory_tools` + `GET/POST /knowledge/memory*` + 右侧 Memory tab + Settings 知识 tab | 对话自动入池；达阈值/点按钮总结成 MEMORY.md 并回注所有 agent |
| **P3 · 经验循环（可选）** | `analyzer/experience_store/promote` + `/knowledge/experiences*` + Memory tab 审核区 | LLM 抽取经验→人工 promote→并入记忆 |
| **P4 · 语义增强（可选）** | LanceDB 向量库（用既有 `rag` extra）+ `use_semantic` 开关（**KB 检索语义已实现**：本地 sentence-transformers + LanceDB 缓存 + 词法余弦融合，缺依赖自动退回词法）+ `memory.recall/obsidian.recall` 语义版（待做） | 检索/召回支持语义相似度，与词法融合排序 |

> 建议从 **P0 → P2** 作为 MVP（读路径 + 写路径闭环），P1/P3/P4 增量迭代。

---

## 8. 测试方案（复用已建成的测试框架）

完全沿用 `packages/runtime/tests/` 的三层结构与 `isolated_home` 隔离：

- **unit**（`-m unit`）：`test_tokenize`（中文 n-gram）、`test_frontmatter`（YAML 解析/summary/wikilinks）、`test_indexer`（扫描/增量/checksum）、`test_retriever`（打分权重、wikilink 加成、min_score/top_k）、`test_association`（TF-IDF 余弦、聚类、discover）、`test_compiler`（concept 抽取/页生成/INDEX）、`test_memory_pool`（append/read/clear）、`test_summarize`（用 fake LLM 断言 prompt 拼装与写盘/清池）。
- **api**（`-m api`）：`test_kb_wiki_api`（search/list/stats/build/ingest/discover，vault 指向 tmp 目录）、`test_memory_api`（summarize 用 monkeypatch 的 fake model）、未配置时返回 `ok:false`。
- **e2e**（`-m e2e`）：用 `ScriptedChatModel` 驱动真实 graph，断言：① 配好 vault 后，提问的 system prompt 含 `<injected_wiki>`（可通过一个「回显 system prompt」的 fake 或检查注入函数输出）；② 一轮对话结束后 pool 出现捕获条目；③ `memory_summarize` 工具/端点把 pool 炼成 MEMORY.md 并在下一轮注入 `<injected_memory>`。

**关键复用**：`ScriptedChatModel`（已支持脚本化工具调用）+ `GINNO_HOME` 隔离 + `GINNO_FAKE_LLM` seam。vault 用 tmp 目录种入若干带 frontmatter 的 markdown 即可，**无需真实 Obsidian、无网络、无 embedding 依赖**。

---

## 9. 与 Molly 的取舍 / 差异

| 维度 | Molly | Ginno 方案 | 理由 |
|---|---|---|---|
| 运行形态 | 服务端多端 | 单机桌面 | 无需多用户 ACL、无需后台常驻调度 |
| 权限 ACL | frontmatter `permission` 强制 | 解析但不强制（可选） | 个人单机，过度设计 |
| 编译 | 纯正则 | 正则基线 + 可选 LLM 增强 | 离线可用，又名副其实「LLMWiki」 |
| 检索 | 词法多信号 + TF-IDF | 同 + 可选 LanceDB 语义 | 默认零依赖，语义作增强 |
| 总结触发 | 手动/HTTP/max-turns/晋升 | 手动/阈值/技能/会话结束 | 去掉 max-turns（Ginno 无此机制），加阈值自动 |
| 经验循环 | 完整 co-copilot（定时） | 简化版、默认手动 | 桌面端按需，避免后台 LLM 消耗 |
| 注入防注入 | XML 包裹 + 权威层级 + sanitize | 同（移植 `wrap_context_section`/`sanitize`） | 必要，保留 |

---

## 10. 关键改动文件清单（落地索引）

**新增（runtime）**：`knowledge/` 包（§4，约 12 个模块）、`tools/memory_tools.py`。
**修改（runtime）**：
- `paths.py`：新增 `memory_pool_dir / knowledge_dir / experiences_path / watermarks_path` 等。
- `graph.py`：`build_agent_system_prompt(..., query)` + 注入 wiki/memory；`agent_node` 传 query；`build_graph` 注册 memory 工具。
- `server.py`：`_stream_graph` 累积 turn 文本并捕获；新增 `/kb/wiki/*`、`/knowledge/*` 路由；`lifespan` 初始化 wiki 索引（若启用）。
- `settings`：默认 `knowledge` 块（`paths._DEFAULT_SETTINGS`）。

**新增/修改（web）**：
- 改 `app/kb/page.tsx`（升级工作台）。
- 新增 `components/right/KnowledgePanel.tsx`、`MemoryPanel.tsx`；改 `RightPanel.tsx`（加 tab）。
- 新增 `components/settings/KnowledgeSettings.tsx`；改 `SettingsNav.tsx`、`settings/[tab]/page.tsx`。
- `lib/runtime.ts` 增加对应 API client；`lib/types.ts` 增加 `WikiEntry/RetrievalResult/Memory/Experience` 类型。

**文档/配置**：本文件；`pyproject.toml` 如需 `rag` extra 已在（lancedb/sentence-transformers）。
