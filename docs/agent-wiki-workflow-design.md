# Agent Wiki × 固定工作流设计(nvk 模式落地)

> 2026-10-07。取代 `knowledge-and-wiki-design.md` §4.5 的「确定性编译器」路线:
> wiki 页不再由 runtime 正则提炼,而是由 **agent 在一个固定 workflow 里综合撰写**
> (nvk/llm-wiki 的「Claude Code 就是编译器」模式)。用户后续在 Workflows UI 里
> **以维护 workflow 的方式**维护整个编译管线。

## 1. 角色翻转

| 层 | 旧(编译器路线) | 新(agent-wiki 路线) |
|---|---|---|
| `Raw/` | 编译源,agent 可写 | **不可变源**(ingest 落点,禁止改写) |
| `Wiki/` | 正则编译产物,**禁止手写** | **agent 撰写的知识层**(综合、双链、置信度) |
| 编译者 | `WikiCompiler`(正则 + 可选 LLM) | workflow 里的 agent 步进节点 |
| 维护 | 重跑 build | 用户编辑 workflow DSL / 定时任务指向它 |

读路径**完全不动**:全 vault 索引、检索注入(`<injected_wiki>` + 引用契约)、
关联图、usage 台账照旧——它们消费的是"页面上有什么",不关心页面谁写的。

## 2. 固定 workflow:「Wiki 编译」(system seed)

```
inv(python) → loop compile(agent ×N, 并行) → fin(python) → fix(agent)
```

* **inv** — python 节点,entry `kb_wiki_inventory`:扫 `Raw/` 全部 md,
  sha256 对台账(`~/.ginno/knowledge/wiki-inventory.json`)求差,
  产出 `plan.files = [{path, rel, title, reason: new|changed|missing_page}]`。
  纯确定性、零 LLM。
* **compile** — loop(`over: {{plan.files}}`, `as: doc`,parallel):
  body 是一个 agent 步进,`goal` 内联编译规范(见 §3),
  对每个文档综合写/更新 `Wiki/<slug>.md`,经 WRITE_JSON 回写 `{compiled: [...]}`。
* **fin** — entry `kb_wiki_finalize`:吸收 `{{compiled}}`,落台账、确定性重建
  `Wiki/INDEX.md`、跑断链 lint,产出 `report = {indexed, broken: [...]}`。
* **fix** — agent 步进:`report.broken` 为空则直接回复"无断链";
  否则逐条修复(改链接或补页)。

配置单源:entries 内部走 `load_knowledge_config()`(vault/raw/wiki 目录),
DSL 不携带路径;夜间维护 = 定时任务 target=workflow 指向本 workflow
(scheduler 的 workflow 分流现成支持,零新代码)。

局限:parallel loop 是默认(fan-out 并行,write-back 组装成完整数组);
若设置里关掉 `workflow_parallel_loops`,引擎降级为顺序执行,此时逐条
last-write-wins 会丢中间项——finalize 少记的文档下轮 inventory 会重新列出
(多花一次编译,不产生错误数据)。

## 2.5 工作流 agent 步进的可回放会话

每次 workflow run 开始时(`_run_workflow_bg`)创建一个真实会话
(type `workflow`,标题 `⚙️ <workflow 名>`,meta 带 `workflow_run_id`,
`workflows/transcript.py`);每个 agent 步进结束后把本轮对话
(Human=步进 goal + AI/工具消息环)镜像追加到该会话的 checkpoint。
效果与定时 agent 任务一致:会话列表可见、聊天 UI 可回放完整对话。
约束:纯追加(checkpointer delta 的 pure-append 不变量),并行步进按
会话锁串行落账;镜像失败只记日志,绝不阻塞 run。

## 3. 编译规范(内联在 compile 步进 goal,摘录)

* 读 `Raw/` 原文,文章是**综合出来的,不是抄的**;原文不可变。
* 每文档一页 `Wiki/<slug>.md`;frontmatter 必含
  `title / date / tags / confidence(high|medium|low) / sources`(`sources`
  列本次 raw 相对路径——断链 lint 与追溯靠它)。
* 正文:结论先行;`[[双链]]` 关联相关页(只链真实存在/本轮会创建的页);
  置信度按来源质量与交叉印证。
* 完成后 WRITE_JSON:`{"compiled": [{"doc": <rel>, "page": <标题>, "action": "created"|"updated"}]}`。

## 4. runtime 增删

**删**:`knowledge/compiler.py` 整文件(正则提炼 / generate_concept_page /
build_all / 台账 / llm_compile——A+B 未合入,随之作废);API
`POST /api/kb/wiki/build`、`POST /api/kb/wiki/ingest`。

**留**(从旧 compiler 移入新 `knowledge/maintenance.py`,只读不生成):
`update_index`(INDEX.md 确定性重建)、`lint_links`(断链清单,[[链接]] 对
wiki 索引标题/路径求差)。

**增**:`workflows/scripts/kb_wiki.py` 三个确定性 entry
(`kb_wiki_inventory` / `kb_wiki_finalize` / `kb_wiki_lint`)注册进
`ENTRY_REGISTRY`;store.py `_SEED` 追加 system workflow(带 drift 跟踪,
后续调优推新版本即自动升级已装实例)。

**前端**:KB 页「Build wiki」改为**打开可见会话并运行本 workflow**
(`createSession` → `POST /api/workflow_runs {workflow_id,
present_in_session_id}` → 跳转会话,run 卡片实时可见);设置页无新开关。

## 5. 与三个参考项目的取舍(定案)

* nvk/llm-wiki:采纳「agent 即编译器 + 规范内联 + INDEX 先行」;research/
  thesis 多视角研究暂不做(需要时加 workflow 前置段即可,仍是维护 DSL)。
* lucasastorian/llmwiki:夜间 Routine = 定时任务×workflow,已覆盖。
* Pratiyush/llm-wiki:断链 lint 已收编;置信度生命周期(stale/archived)
  留作后续 frontmatter 约定,不进 runtime。
