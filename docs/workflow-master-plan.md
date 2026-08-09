# Ginno Workflow 引擎可靠性 + 质量提升总体方案

> 合并自：workflow-ux-redesign（已落地）、workflow-reliability-discussion、
> workflow-synthesis-quality-plan、workflow-tooling-plan。
> 本文是单一事实来源；所有讨论结论与待决事项均以此为准。

---

## 一、核心目标与指标体系

### 北极星指标（总结成 Workflow 场景）

- **成功率** = 产出完整可运行工作流次数 / 总结尝试次数（最终口径到 L3）
- **准确率** = 成功样本中完整复现原对话流程的比例（L4，依赖用户反馈 + LLM 评审）

### 漏斗分层（度量必须按层归因）

```
L0 触发总结
 └─ L1 生成成功：parse + validate 通过
     └─ L2 被采用：用户创建（不关闭/不大改）
         └─ L3 可运行：首次 run 跑完 done，无失败步骤
             └─ L4 保真：运行结果复现原对话意图  ← 准确率
```

---

## 二、可靠性改造

### 2.1 议题① Loop 空序列处理（on_empty）

#### 已定设计

- `loop` 节点增加 `on_empty: "skip" | "fail"`，**默认 `"skip"`**
- 无论取值，以下修复无条件落地：
  - 空序列发 `loop_skip` 事件（含 over 表达式 + 求值结果摘要）
  - `fail` 取值则发 error 事件归因到 loop 节点
  - loop 正常结束/达 max_iters 补发 `node_exit`（状态收尾）；达上限加发 `loop_cap` 事件
  - body 未执行时置新状态 `"skipped"`（UI 配套：STATUS_COLOR/LABEL/时间线）

#### 改动文件

| 文件 | 改动 |
|---|---|
| `workflows/nodes/builtin.py` | LoopNode：空序列分支发 loop_skip + on_empty 路由；结束时补 node_exit |
| `workflows/dsl.py` | validate_dsl：`on_empty` 可选，枚举校验 |
| `lib/types.ts` | WorkflowStep status 增 `"skipped"` |
| `components/chat/RunBlocks.tsx` | STATUS_COLOR/LABEL/Glyph 增 skipped 样式 |
| `components/workflow/WorkflowLogTimeline.tsx` | KIND_STYLE 增 loop_skip/loop_cap |

---

### 2.2 议题② 隐式抽取节点（核心，最详细）

#### 2.2.1 问题根因

AgentNode 的 context 写回依赖 WRITE_JSON 文本约定（步骤系统提示要求模型在
回复结尾输出 `WRITE_JSON {...}`），而：
- 模型可能不输出（本次事故根因）
- summarize 生成的 DSL 完全不知道这个约定
- array 类型在回复结尾最容易碎（10 个对象的列表）

#### 2.2.2 设计原则

**将结构化产出的责任从步骤模型移到编译器**：
- 步骤模型只管完成任务（goal），不需要知道任何 context 约定
- writes 声明放在 DSL，compiler 自动注入隐式抽取节点
- 抽取节点读步骤最终回复文本、专职产出严格 JSON

#### 2.2.3 DSL 扩展

**新增 `writes` 字段**（step/agent 节点可选）：

```json
{
  "id": "search_and_select",
  "type": "step",
  "agent": "research",
  "goal": "搜索全球市场新闻，选出 10 只明日 A 股关注股票",
  "writes": {
    "stocks": {
      "type": "array",
      "items": {"type": "object"},
      "description": "每只股票含 code/name/reason 字段"
    },
    "market_summary": {"type": "string"}
  }
}
```

**新增 `extract_model` 字段**（step/agent 节点可选，覆盖抽取模型）：

```json
"extract_model": "claude-haiku-4-5-20251001"
```

**validate_dsl 新增校验**：
- `writes` 若存在，必须是非空 dict
- 每个 value 必须是含 `type` 的 dict（JSON Schema 最小子集）
- key 只能含字母数字下划线（作为 context 键安全）
- `extract_model` 若存在，必须是字符串

**LLMNode** 已有 `output: string` 字段（写单个 key），与 `writes` 互不干扰
（`writes` 仅针对 step/agent 节点）。

#### 2.2.4 编译期注入（compiler.py 变更）

**注入时机**：validate_dsl（用户原始 DSL）之后、StateGraph 建图之前，作为独立
预处理 pass。注入产物不持久化——每次 compile_workflow 重新合成，存储 DSL 保持
干净（只含 writes 声明）。

```
compile_workflow(dsl, model, tools, run_ctx, checkpointer)
  1. normalize_dsl(dsl)
  2. validate_dsl(d)          ← 校验用户 DSL（不含 extract 节点）
  3. d, syn = _inject_extract_nodes(d, model)   ← 新增：合成 __extract 节点
  4. for n in d["nodes"]: g.add_node(...)       ← 包含合成节点
  5. for n in d["nodes"]: cls.add_edges(...)
  6. g.compile(checkpointer)
```

**`_inject_extract_nodes(dsl, model)` 算法**：

```python
def _inject_extract_nodes(dsl: dict, model) -> tuple[dict, list[str]]:
    """为带 writes 声明的节点注入隐式 __extract 节点，返回 (增强后 dsl, 合成节点 id 列表)。"""
    d = deepcopy(dsl)
    loop_bodies = {n["body"] for n in d["nodes"] if n.get("type") == "loop" and n.get("body")}
    loop_head_of_body = {n["body"]: n["id"]
                         for n in d["nodes"] if n.get("type") == "loop" and n.get("body")}
    synthetic = []
    for n in list(d["nodes"]):           # 对原始 nodes 迭代，不含合成节点
        if not n.get("writes"):
            continue
        src_id = n["id"]
        ext_id = f"{src_id}__extract"
        # 合成节点描述
        ext_node = {
            "id": ext_id,
            "type": "extract",           # 注册为内部节点类型
            "source_node": src_id,
            "writes": n["writes"],
            "extract_model": n.get("extract_model"),
        }
        # 如果 src 是 loop body：back-edge 需从 ext_id → loop_head，而非 src → loop_head
        if src_id in loop_bodies:
            ext_node["back_to"] = loop_head_of_body[src_id]
        d["nodes"].append(ext_node)
        synthetic.append(ext_id)
        # 重定向出边：src_id → X 改为 ext_id → X（loop body 没有显式出边，跳过）
        for e in d["edges"]:
            if e.get("from") == src_id:
                e["from"] = ext_id
        # 新增 src → ext 边
        d["edges"].append({"from": src_id, "to": ext_id})
    return d, synthetic
```

**Loop body 特殊处理（边连接）**：
- Loop body 的 back-edge（body → loop head）是结构边，**不在 dsl.edges**，由
  `LoopNode.add_edges` 用 `g.add_edge(body, nid)` 添加。
- 注入后：back-edge 应变为 `body__extract → loop_head`。
- 解决方案：`LoopNode.add_edges` 改为：
  ```python
  # 判断 body 是否有对应的 __extract 节点
  ext_body = f"{body}__extract"
  back_src = ext_body if any(n["id"] == ext_body for n in d.get("nodes", [])) else body
  g.add_edge(back_src, nid)
  ```
- `ExtractNode.add_edges`：若 node dict 含 `back_to`，**不添加**标准出边（由
  LoopNode 负责），否则走正常的 `g.add_edge(ext_id, next_or_END)` 逻辑。

**分支节点目标有 writes**：branch cases/default 指向原始 step id，branch 路由到
step，step 执行后自然流到 step__extract → 无需特殊处理。✓

**Entry 节点有 writes**：entry 依然指向原始 step id，step__extract 是其后继。✓

#### 2.2.5 ExtractNode 实现

```python
@register_node
class ExtractNode(BaseNode):
    """编译器内部节点（用户 DSL 不直接使用）：读步骤回复文本，产出严格 JSON 写入 context。"""
    type = "extract"
    # 内部节点标记，validate_dsl 不对其直接校验
    _internal = True

    params_schema = {
        "type": "object",
        "required": ["source_node", "writes"],
        "properties": {
            "source_node": {"type": "string"},
            "writes": {"type": "object"},
            "extract_model": {"type": "string"},
        },
    }

    @staticmethod
    async def execute(node, cctx, state, config, eff) -> dict:
        from ...models import build_model
        from ...graph import text_of_content
        import json

        run_ctx = cctx["run_ctx"]
        node_id = node["id"]
        source_id = node["source_node"]
        writes_schema = node["writes"]          # {key: json_schema}
        context = dict(state.get("context") or {})
        events: list = []

        def emit(ev):
            events.append(ev)
            run_ctx["events"].append(ev)

        emit({"run_id": run_ctx["run_id"], "node_id": node_id,
              "kind": "node_enter", "node_type": "extract"})

        # ── 输入来源：AgentNode 已将 result_text 写入 state.results[source_id] ──
        source_text = (state.get("results") or {}).get(source_id, "")

        # ── WRITE_JSON 兼容快速路径 ──────────────────────────────────────────────
        # 若步骤回复已含合法 WRITE_JSON，且覆盖所有声明键，直接采用（省一次 LLM）
        from ..nodes.agent_helpers import parse_writes
        wj = parse_writes(source_text)
        if wj and all(k in wj for k in writes_schema):
            validated, errs = _validate_writes(wj, writes_schema)
            if not errs:
                emit({"run_id": run_ctx["run_id"], "node_id": node_id,
                      "kind": "context_write", "keys": list(validated.keys()),
                      "method": "write_json"})
                emit({"run_id": run_ctx["run_id"], "node_id": node_id,
                      "kind": "node_exit", "status": "done"})
                return {"context": {**context, **validated}, "events": events,
                        "__output__": validated}

        # ── 主路径：抽取 LLM ────────────────────────────────────────────────────
        schema_str = json.dumps(writes_schema, ensure_ascii=False)
        prompt = (
            "以下是一个步骤执行后的输出文本。请严格按给定 JSON Schema 提取字段，"
            "只输出 JSON 对象，不要任何解释。若某字段内容不存在，该字段值输出 null。\n\n"
            f"JSON Schema:\n{schema_str}\n\n"
            f"步骤输出：\n{source_text[:8000]}"
        )

        extract_model_name = node.get("extract_model")
        m = build_model(extract_model_name) if extract_model_name else cctx["model"]

        from langchain_core.messages import HumanMessage
        last_err = ""
        for attempt in range(2):
            if attempt > 0:
                prompt += f"\n\n[上一次输出有误：{last_err}。请只输出符合 Schema 的 JSON 对象。]"
            resp = await m.ainvoke([HumanMessage(content=prompt)])
            raw = text_of_content(resp.content)
            try:
                parsed = _extract_json_from_text(raw)
            except Exception as e:
                last_err = f"JSON 解析失败：{e}"
                continue
            validated, errs = _validate_writes(parsed, writes_schema)
            if not errs:
                break
            last_err = "; ".join(errs)
        else:
            # 两次均失败 → 步骤 fail，归因到 __extract 节点
            emit({"run_id": run_ctx["run_id"], "node_id": node_id,
                  "kind": "error",
                  "error": f"抽取失败（来源步骤 {source_id}）：{last_err}",
                  "traceback": None})
            raise RuntimeError(f"ExtractNode {node_id} failed: {last_err}")

        emit({"run_id": run_ctx["run_id"], "node_id": node_id,
              "kind": "context_write", "keys": list(validated.keys())})
        emit({"run_id": run_ctx["run_id"], "node_id": node_id,
              "kind": "node_exit", "status": "done"})
        return {"context": {**context, **validated}, "events": events,
                "__output__": validated}

    @classmethod
    def add_edges(cls, g, node: dict, d: dict) -> None:
        from langgraph.graph import END
        nid = node["id"]
        # loop body 情形：ExtractNode 承接 back-edge（back_to 已由注入器设置）
        if node.get("back_to"):
            g.add_edge(nid, node["back_to"])
            return
        # 普通情形：接原步骤的出边（注入时已重定向到 __extract）
        nxt = cls._outgoing(d, nid)
        g.add_edge(nid, nxt or END)
```

**辅助函数**（放 `extract.py` 同文件）：

```python
def _validate_writes(data: dict, schema: dict) -> tuple[dict, list[str]]:
    """对声明的每个 key 做类型校验，返回 (validated_dict, errors)。"""
    from .base import validate_against
    errs = []
    out = {}
    for key, key_schema in schema.items():
        val = data.get(key)
        if val is None:
            errs.append(f"key '{key}' missing or null")
            continue
        key_errs = validate_against(key_schema, val)
        if key_errs:
            errs.extend([f"{key}: {e}" for e in key_errs])
        else:
            out[key] = val
    return out, errs

def _extract_json_from_text(text: str) -> dict:
    """从文本中提取第一个完整 JSON 对象（兼容 markdown fence）。"""
    import json, re
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    try:
        v = json.loads(text)
        if isinstance(v, dict):
            return v
    except json.JSONDecodeError:
        pass
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        v = json.loads(text[i:j+1])
        if isinstance(v, dict):
            return v
    raise ValueError("no JSON object found in extraction output")
```

#### 2.2.6 错误归因与 Retry 语义

**归因链路**：
- 抽取失败时，error 事件的 `node_id = "step_id__extract"`
- `_drive_run_events` 中 `node_to_step = {s["id"]: s["id"] for s in wf["steps"]}` 不含 `__extract`（见 §2.2.8）
- 需在 error 事件处理中把失败映射回源步骤：
  ```python
  # api/workflows.py _drive_run_events 中 kind == "error" 分支
  actual_node = nid
  if nid and nid.endswith("__extract"):
      actual_node = nid[:-len("__extract")]  # 剥离后缀，得到源步骤 id
  _set_run_status(run_id, "failed",
                  error=str(ev.get("error") or ""),
                  error_detail={"node_id": actual_node,   # 前端 RunErrorBox 归因
                                "extract_node_id": nid,   # 保留完整归因
                                "traceback": ev.get("traceback")})
  if actual_node in node_to_step:
      wf_store.update_step(run_id, node_to_step[actual_node], "failed")
  ```
- RunErrorBox 显示「失败于：{源步骤标题}」，点展开可看 `extract_node_id` 细节。

**Retry 语义（从 checkpoint 重试）**：
- `retry_from_checkpoint` 时，抽取节点的 LangGraph checkpoint 状态在源步骤完成后、
  抽取失败前（graph 未提交抽取节点的 superstep）→ 恢复后重跑抽取节点，源步骤**不
  重新执行**（checkpoint 已保存 state.results[source_id]）✓
- 普通 retry（从头）：源步骤 + 抽取节点均重新执行 ✓

#### 2.2.7 事件与状态模型

| 节点 | 产生的事件 kind | 写入 state 字段 |
|---|---|---|
| source step（AgentNode）| node_enter / tool_call / tool_result / context_write（WRITE_JSON） / node_exit | results[src_id] = result_text; context（WRITE_JSON 写回）|
| `__extract`（WRITE_JSON 快速路径）| node_enter / context_write(method=write_json) / node_exit | context（抽取结果）|
| `__extract`（正常路径） | node_enter / context_write / node_exit | context（抽取结果）|
| `__extract`（失败） | node_enter / error | — |

**state.results 现状确认**：
- `AgentNode.execute` 第 161 行已有：`"results": {**state.get("results", {}), node_id: result_text}`
- `ExtractNode` 读 `state.results[source_id]` ✓ 无需改 AgentNode
- `LLMNode` **不写 results**（只写 context[output_key]）→ `writes` 仅对 step/agent 有效，
  LLMNode 用原有 `output` 字段，两者互不干扰

#### 2.2.8 UI 展现与 steps_from_dsl 过滤

**`steps_from_dsl` 过滤**（`dsl.py`）：
```python
def steps_from_dsl(dsl: dict) -> list[dict]:
    out = []
    for n in _as_list(dsl.get("nodes")):
        if not isinstance(n, dict): continue
        # 过滤编译器内部节点（不暴露给 UI 步骤列表）
        if n.get("type") == "extract" or n.get("id", "").endswith("__extract"):
            continue
        out.append({"id": n.get("id") or "",
                    "title": n.get("title") or n.get("goal") or n.get("id") or "",
                    "agent_id": n.get("agent") or n.get("agent_id")})
    return out
```
→ run.steps 不含 `__extract`，进度条/状态列表干净 ✓

**DAG 可视化**（v1）：
- `WorkflowDag` 基于 `wf.dsl`（存储 DSL），不含 `__extract` → DAG 干净
- extract 节点只出现在事件时间线（node_enter/exit 可见）
- v2 扩展：前端可从 `writes` 声明推断出抽取步骤，渲染为源步骤的附属小节点

**事件时间线**（`WorkflowLogTimeline`）：
- `kind = "node_enter"` + `node_type = "extract"` → 样式：小号 + `Extract` 标签
- `kind = "context_write"` + `method = "write_json"` → 标注「via WRITE_JSON（快速路径）」
- 不进 EXPANDABLE（不需要展开详情）；失败走普通 error 样式

#### 2.2.9 summarize 提示词更新

`_SYNTHESIZE_PROMPT` 末尾追加（替换现有变量提取指令）：

```
For steps that produce data consumed by later steps (especially loops),
declare a `writes` field with JSON Schema:
- Lists of items: {"type": "array", "items": {"type": "object"}}
- Single string:  {"type": "string"}
- Number/boolean: {"type": "number"} / {"type": "boolean"}
Key names must match the {{context.key}} references used downstream.
Example:
  {"id": "search", "type": "step", "agent": "research",
   "goal": "Search and select top 10 stocks. Return list.",
   "writes": {"stocks": {"type": "array", "items": {"type": "object"}}}}

Do NOT put WRITE_JSON instructions in goals — the engine handles extraction.
Context {{template}} variables still go in context.schema/initial as before.
```

`prompt_version` bump：`"synth-3"`。

#### 2.2.10 未考虑到的问题 Checklist

以下是深度审查后发现的潜在遗漏，**每项都需要在实施前确认处理方式**：

| # | 问题 | 影响 | 建议处理 |
|---|---|---|---|
| A | **`validate_dsl` 会拒绝 `extract` 节点**（unknown type）— 但注入在 validate 之后，所以 validate 看不到 extract 节点 | 无害（设计已规避）| 确保 inject 在 validate 之后 |
| B | **同一个 step 的 WRITE_JSON 写回与 extract 节点重复写 context**：若步骤已 WRITE_JSON，extract 走快速路径，context 被覆盖一次（值相同）→ context_write 事件发两次 | 轻微噪声 | 快速路径跳过 context 合并；或 AgentNode 的 WRITE_JSON 路径在 writes 声明存在时静默（抽取节点统一处理） |
| C | **extract 节点的 `_post` 会把 validated 写入 state.outputs[ext_id]**（BaseNode._post）。这不影响功能，但 outputs 里有额外键 | 可接受 | 不处理；或 ExtractNode 覆写 _post 跳过 outputs 记录 |
| D | **`extract_model` 建立新连接**：每次抽取都 `build_model(extract_model_name)` 会重建 client，无缓存 | 轻微性能损耗 | build_model 本身轻量（不建 HTTP session，httpx client 由 langchain 管）；run 级缓存如需要可后续加 |
| E | **loop body 里 source step 有 writes + LoopNode.add_edges 改动**：LoopNode 检查 body 是否有对应 `__extract` 节点——需要访问 dsl，而 `add_edges(g, node, d)` 已有 `d` 参数，可直接查 ✓ | 设计正确，需实现 | 按 §2.2.4 实现；测试用例必须覆盖 loop body with writes |
| F | **多层嵌套循环**（loop body 本身又是 loop）：body__extract 的 back_to 指向外层 loop head，里层 loop head 不受影响。但 body 步骤是里层 loop 的 entry，则 body 自身有 back-to-inner-loop-head——与 extract 冲突？→ 当前 DSL v1 不支持嵌套 loop（validate 没有明确禁止，但设计上不预期）| 理论路径，v1 不触发 | 文档标注：writes + 嵌套 loop body 暂未验证 |
| G | **结构边（loop back-edge）与合成边共存**：注入器在 dsl.edges 里加了 `src → ext` 边，再由 LoopNode 加 `ext → loop_head` 的 graph 边（非 dsl 边）。validate_dsl 会看到 ext → X 的 dsl 边并检查 X 是否在 idset——但注入发生在 validate 之后，dsl.edges 里的合成边不会被 validate 看到 ✓ | 无害 | 确认时序正确 |
| H | **WorkflowDef.steps 在 run 创建时已快照**：`create_run` 调用 `steps_from_dsl`，结果存入 run JSON。如果后续改 `steps_from_dsl` 过滤逻辑，已创建的 run 不受影响（因为 writes 声明本来就不改旧 DSL）| 可接受 | 无需处理 |
| I | **`__extract` 后缀冲突**：用户若手写 `id: "foo__extract"` 的节点，注入时会当作内部节点过滤。| 极罕见 | validate_dsl 增一条规则：用户节点 id 不得以 `__extract` 结尾 |
| J | **extract 节点的输入 `eff`（edge transform）**：BaseNode.make_node 从 `state.inputs[nid]` 取 eff，但 extract 节点的前驱边没有 transform → `state.inputs[ext_id]` 为空 → `eff = ctx`（全量 context）。这可接受，ExtractNode 不用 eff，用 `state.results[source_id]` | 无影响 | 不处理；或文档注明 |
| K | **抽取成功但结果为空 array `[]`**：`_validate_writes` 不报错（空 array 是合法值）→ context.stocks = [] → loop `on_empty` skip 触发（有 loop_skip 事件）✓ | 设计正确 | 无需处理 |
| L | **大 source_text 截断**：目前 source_text 截至 8000 字符送给抽取模型。若回复超长（详细研报 + 股票列表），关键列表可能在截断范围内。| 潜在质量问题 | v1 保留 8000 截断（通常够用）；后续可提高或让 AgentNode 直接把结构化结果另存 |
| M | **extract_model 字符串格式**：只有 model 名（如 "claude-haiku-4-5-20251001"）还是 "provider:model"？`build_model` 当前签名是 `build_model(provider, model=None)`，传单一字符串不 match。| 实现阻断 | 选项1：`extract_model` 格式为 `"provider:model"` 拆分；选项2：新增 `build_model_by_name(name)` 按模型名 lookup provider；建议选项2 |

#### 2.2.11 改动文件清单（完整）

| 文件 | 改动内容 |
|---|---|
| `workflows/dsl.py` | validate_dsl 增 writes/extract_model 校验 + 禁止 `__extract` 后缀的用户节点 id；steps_from_dsl 过滤 extract 类型 |
| `workflows/nodes/extract.py` | 新建：ExtractNode + _validate_writes + _extract_json_from_text |
| `workflows/nodes/registry.py` | 注册 ExtractNode（标记 `_internal = True`） |
| `workflows/nodes/__init__.py` | import extract 模块 |
| `workflows/compiler.py` | _inject_extract_nodes() 函数；compile_workflow 调用它 |
| `workflows/nodes/builtin.py` | LoopNode.add_edges：检查 body 是否有对应 __extract 节点，调整 back-edge 起点 |
| `models.py` | 新增 `build_model_by_name(name) → model`（按模型名自动匹配 provider）|
| `api/workflows.py` | _drive_run_events：error 事件归因解析 __extract 后缀；_SYNTHESIZE_PROMPT 更新；prompt_version bump |
| `workflows/store.py` | create_def / update_def：写入时调用 normalize_dsl（不含 extract，不变）|
| `lib/types.ts` | WorkflowRunEvent 增 `method?: string`（context_write 快速路径标记）|
| `components/workflow/WorkflowLogTimeline.tsx` | node_type="extract" 的 node_enter/exit 特殊样式；context_write method=write_json 标注 |

---

### 2.3 议题③ 文件工具边界（⏳ 三项待定）

#### 当前问题（本次事故实证）

summarize_and_attach 步骤找不到报告路径 → 以 home 目录为根调用 glob_files/read_file，
扫遍 Applications/Library/System/Volumes/.ginno 等全局目录——这是**读权限无边界的直接结果**。

#### 无论选什么都先加的底线（P0 止血，无需讨论）

```python
# tools/files.py 或 graph.py 文件工具包装层
_HARD_DENY = [
    str(paths.home()),          # ~/.ginno 原始数据
    os.path.expanduser("~/.ssh"),
    os.path.expanduser("~/Library/Keychains"),
]

def _check_path_deny(p: str) -> str | None:
    """若路径在硬拒绝名单下，返回错误消息；否则 None。"""
    abs_p = os.path.realpath(p)
    for denied in _HARD_DENY:
        if abs_p == denied or abs_p.startswith(denied + "/"):
            return f"[error] 路径 {p!r} 在拒绝访问区域内"
    return None
```

#### 三项待定决策

**决策 4：allowlist 声明位置**

| 方案 | 描述 | 优缺点 |
|---|---|---|
| A | DSL 顶层 `paths: ["/path/to/obsidian"]` 显式声明 | 明确、可审计；需手写 |
| B | context.schema 中 `format: "path"` 标记字段自动纳入 | 零额外配置（obsidian_raw_path 已在 context）；判定规则需定义 |
| A+B 并存 | 自动识别 + 可额外声明 | 推荐 |

倾向：B 自动识别（匹配 `/` 开头字符串类型字段或 format:path）+ A 兜底，summarize
提示词教「路径变量用 context.initial 存，引擎自动允许访问」。

**决策 5：越界行为**

倾向：**tool error**（`[error] 路径在限制区域外`），不 fail run——让步骤 agent
有机会用其他方式完成任务或自行报告。配合 loop_skip 事件足够。

**决策 6：workflow 步骤的 bash 工具**

- bash 工具允许 `cat ~/.ssh/...` 等任意命令，文件边界管不住
- 选项：默认禁用 / 弱前缀检查（bash 命令以允许路径前缀为前提）/ 接受风险
- **倾向**：workflow 步骤的 agent fork 的 tools_allow **默认不包含 bash**（保守）；
  DSL 中可显式 `skills: ["bash_allowed"]` 或类似机制解锁
- 需和现有 tools_allow 机制配合验证（bash 当前是否默认开？）

---

### 2.4 议题④ 事件时间线保真（顺手修）

**问题**：所有事件在 superstep 边界批量落盘，ts = 落盘时刻 → 同一步骤的
node_enter/tool_call/node_exit 可能时间戳相同（本次事故：14 条事件同一秒）。

**修法**：`emit()` 时在事件 dict 里打 ts，`append_event` 保留已有 ts：

```python
# nodes/builtin.py emit() 函数
import time
def emit(ev):
    if "ts" not in ev:
        ev["ts"] = time.time()
    events.append(ev)
    run_ctx["events"].append(ev)
```

```python
# api/workflows.py append_event（events 模块）
# 如果 ev 已含 ts，尊重它；否则此处打 ts
```

一行级改动，随议题① 一起完成。

---

## 三、总结成 Workflow 质量体系

### 3.1 记录方案（P1，建 case 档案）

每次调用 summarize-from-session 建一个 case 目录：

```
~/.ginno/synthesis/
  20260809-221400-7700a4a8/
    input.json      # trace 全文 + 元信息（完整重放快照）
    attempts.jsonl  # 重试循环每轮原始输出 + parse/validate 结果
    output.json     # 最终 DSL + fail_stage 标签 + 耗时
    outcome.json    # 异步回填：adopted/edited/first_run/user_feedback
```

**input.json schema**：
```json
{
  "synthesis_id": "20260809-221400-7700a4a8",
  "session_id": "7700a4a8...",
  "ts": 1786284880,
  "prompt_version": "synth-3",
  "provider": "anthropic", "model": "claude-fable-5",
  "last_n": null,
  "session_stats": {"messages": 131, "tool_calls": 42},
  "trace": "<_trace_text 全文>"
}
```

**outcome.json 异步回填链路**：
- 弹窗创建成功 → `synthesis_id` 随 createWorkflow 传入 → workflow meta 存 `synthesized_from`
- 弹窗关闭未创建 → 前端发轻量事件 `synthesis_discarded`
- 该 workflow 首次 run 终态 → 后端按 `synthesized_from` 回填 first_run
- 用户 👍/👎 → 回填 user_feedback

### 3.2 问题定位：失败分类学（taxonomy）

自动打标签，review 时按标签过滤：

| 阶段 | 标签 | 判定依据 |
|---|---|---|
| L1 格式 | `format.not_json` | parse 失败 |
| L1 结构 | `schema.<错误签名>` | validate_dsl 错误归一化后 |
| L2 丢弃 | `adoption.discarded` | outcome.adopted = false |
| L2 大改 | `adoption.heavy_edit` | edit_distance > 阈值 |
| L3 运行 | `exec.<node_type>.<error签名>` | first_run.failed_node |
| L4 保真 | `fidelity.user_down` | user_feedback.verdict = "down" |

`prompt_version` 进每条记录 → 按版本对比失败标签分布 = 低成本 A/B。

### 3.3 产品侧提升（各层转化）

| 层 | 措施 |
|---|---|
| L1 | ② 的 writes 声明消除模型遗漏写回；两阶段提示词（先骨架后 goal）可按数据决定是否实施 |
| L2 | 弹窗内一句话精修（带 trace + 当前 DSL + 指令重生成）；会话↔步骤映射预览（`source_turns` 字段）；纯聊天 session 的预期管理提示 |
| L4 | 首跑结束后一次性 👍/👎 回收（RunBlocks/WorkflowPanel 注入，summarize 来源才显示）|

### 3.4 离线评测（P4）

- **Golden set**：从 case 库人工挑 20-30 个，标注期望节点结构，版本化入 `tests/synthesis_golden/`
- **回放**：`ginno-cli synth replay <case> [--prompt synth-4]`（读 input.json，不需要活会话）
- **打分**：validate 通过率（L1 代理）+ 节点序列相似度（L4 代理）+ LLM-judge（覆盖度/顺序/幻觉）
- **门禁**：提示词改版前跑 golden set，L1 代理不回退

---

---

## 四、配套工具（全 UI 化，无 CLI）

> CLI 降级为「P4 可选，CI 批处理专用」；P0–P2 全走现有 UI 扩展 + 两个新页面。

---

### 4.1 WorkflowInspector 扩展

**涉及文件**：`apps/web/src/components/workflow/WorkflowInspector.tsx`

#### 4.1.1 Doctor 面板（内联折叠）

**位置**：版本号 `v4▾` 按钮右侧，紧邻开发会话按钮，运行按钮左侧。

```
PR Triage            v4▾  ⚠️ 1  [开发会话]           [▶ 运行]
                     ↑ 点击展开 doctor 面板
```

无问题时不渲染徽标，有 warning → 黄色 `⚠️ N`，有 error → 红色 `✕ N`。

**展开后（内联，在 DAG 上方）**：

```
┌──────────────────────────────────────────────────────────┐
│  🛡 DSL 检查                                    收起 ▴   │
├──────────────────────────────────────────────────────────┤
│  ✕ loop 'create_todos_loop' 的 over 表达式               │
│      context.stocks 无上游 writes/initial 来源            │
│      （stocks 从未被任何节点声明产出）                     │
│                          [一键升级 writes]                │
├──────────────────────────────────────────────────────────┤
│  ⚠ write_report 的 goal 含路径字面量                      │
│      {{obsidian_raw_path}} 未在 context.initial 声明      │
└──────────────────────────────────────────────────────────┘
```

**视觉规范**：
- 容器：`rounded-lg border border-line bg-base/30 p-2.5 space-y-1.5`
- error 行：左边 `X h-3.5 w-3.5 text-red`，正文 `text-[11px] text-red`，详情 `text-faint`
- warning 行：`AlertTriangle h-3.5 w-3.5 text-yellow`，正文 `text-yellow`
- [一键升级 writes] 按钮：`btn-press border border-violet/40 text-violet text-[11px]`，
  点击后 → 走**现有 workflow-dev agent 会话**（pre-prompt: 「为这个 workflow 补充 writes 声明」）

**数据来源**：`GET /api/workflows/{wf_id}/doctor`，挂载时请求一次，结果缓存到
组件 state；workflow dev session 落地 propose_edit 后自动重新请求（watch `wf.version`）。

**新 API**：
```
GET /api/workflows/{wf_id}/doctor
→ {"ok": true, "errors": [{"rule":"loop.over.no_source","node_id":"...","message":"..."}],
   "warnings": [...]}
```
后端实现：直接在 Python 里运行 §4.2 的规则集，无 LLM 调用，毫秒级响应。

#### 4.1.2 步骤列表：节点耗时 + token

**现有步骤列表**（WorkflowInspector.tsx 步骤清单区域）在右侧追加：

```
● done  拉取 PR 列表                        12.4s  1.2K ↑
● done  分析 PR 内容                         34.1s  8.7K ↑
⚡ run   生成报告                              5.2s  …
```

- 耗时 = `node_exit.ts - node_enter.ts`（事件已有 ts，§2.4 保真后精确）
- token = `node_exit.usage.input_tokens + output_tokens`（§4.5 run遥测落盘后可读）
- 无数据时两列不渲染（向后兼容）
- 样式：`ml-auto text-[10px] text-faint tabular-nums`，箭头 `↑` 表示 token（上传方向 = 模型输入）

#### 4.1.3 事件时间线：文件访问快速过滤

**在 WorkflowLogTimeline 顶部加过滤 tabs**：

```
执行日志 · 节点 write_report
[全部] [工具调用] [文件访问] [上下文写入]
```

「文件访问」tab 只显示 `tool_call` 中 name 含 `glob_files/read_file/write_file/patch_file`
以及对应 `tool_result`——直观看到步骤都动了哪些文件，排查越界问题。

**视觉规范**：tabs 样式复用 /workflows 页面的 `全部/系统/用户` tabs（`border-b border-violet`）。

---

### 4.2 Settings 新 tab「总结质量」

**加入 SettingsNav.tsx MAIN 列表**（紧跟 Workflows 之后）：
```tsx
{ id: "synthesis-quality", label: "总结质量", icon: TrendingUp, color: "#a78bfa" }
```
同时更新：`generateStaticParams`（app/settings/[tab]/page.tsx）+ SettingsView 的 tab 路由。

**新文件**：`apps/web/src/components/settings/SynthesisQualitySettings.tsx`

#### 页面整体布局

```
┌──────────────────────────────────────────────────────────────────────┐
│  总结质量                                                             │
│  过去 [30 天 ▾]  · 基于 synth-3 版本                                 │
├──────────────────────────────────────────────────────────────────────┤
│  漏斗指标                                                             │
│  ┌──────────┬──────────┬──────────┬─────────────────────────────┐    │
│  │ L1 生成  │ L2 采用  │ L3 首跑  │ 平均草稿改动量              │    │
│  │   82%    │   71%    │   64%    │   1.2 个节点                │    │
│  │ 24/29 次 │ 17/24 次 │ 11/17 次 │ edit_distance 均值          │    │
│  └──────────┴──────────┴──────────┴─────────────────────────────┘    │
│  top 失败标签  schema.edge_unknown(4) · format.not_json(3) · …       │
├──────────────────────────────────────────────────────────────────────┤
│  案例列表                                                             │
│  [全部 ▾]  [最近 7 天 ▾]  [仅失败]                共 29 条           │
│  ┌────────────────────────────────────────────────────────────────┐   │
│  │ ✕ schema.edge_unknown · 今天 22:14 · 131 消息 · synth-3  [查看]│   │
│  │ ✓ 已采用 · 首跑成功   · 今天 14:32 · 48 消息  · synth-3  [查看]│   │
│  │ ⚠ adoption.discarded  · 昨天 18:32 · 67 消息  · synth-3  [查看]│   │
│  └────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

**视觉规范**：
- 漏斗指标卡片：`grid grid-cols-4 gap-3`，每格 `rounded-lg border border-line bg-card p-3 text-center`
- 百分比大字：`text-2xl font-bold`，颜色：`text-violet`（L1）/ `text-blue`（L2）/ `text-green`（L3）
- 案例行：`flex items-center gap-3 rounded-md px-3 py-2 hover:bg-card2`
- 状态图标：✓ `text-green` / ✕ `text-red` / ⚠ `text-yellow`
- 失败标签：`rounded-full border border-line px-2 py-0.5 text-[10px] font-mono text-faint`
- [查看] 按钮：`text-[11px] text-violet hover:opacity-80`，点击展开侧抽屉

#### 案例详情侧抽屉（SynthesisCaseDrawer.tsx）

从右侧 slide in，宽 360px，与 VersionHistoryDrawer 相似结构：

```
┌──────────────────────────────────────────────┐
│  案例详情                                  ✕ │
│  20260809-221400-7700a4a8                     │
│  synth-3 · anthropic · 131 消息 · 22:14      │
├──────────────────────────────────────────────┤
│  状态  ✕ schema.edge_unknown                 │
│  耗时  21.3s · 尝试 3 次                     │
├──────────────────────────────────────────────┤
│  各轮尝试                                    │
│  ▸ 第 1 轮  ✕ validate_dsl 失败              │
│      edge_unknown: stocks → report           │
│  ▸ 第 2 轮  ✕ validate_dsl 失败              │
│      entry 'start' 不是 node id              │
│  ▸ 第 3 轮  ✕ JSON 解析失败                  │
├──────────────────────────────────────────────┤
│  后续结果                                    │
│  创建  —— 未被采用                            │
│  首跑  ——                                    │
│  反馈  ——                                    │
├──────────────────────────────────────────────┤
│  Trace（原始会话摘要）               [展开 ▾] │
│  USER: 帮我整理一下...                       │
│  AGENT: 好的，我来...                         │
│  → tool search_web({"query":"..."})          │
├──────────────────────────────────────────────┤
│  [用当前提示词重新总结]                        │
│  [↺ 对比：用 synth-4 重新总结]               │
└──────────────────────────────────────────────┘
```

**「重新总结」按钮**：调用 `POST /api/synthesis/replay/{id}` → 后端读
`input.json` 中的 trace，重新跑 synthesize 流程 → 返回 `{ok, dsl}` → 前端打开
SummarizeModal，同时 case 的 attempts 追加新一轮记录。

**「对比：用 synth-4 重新总结」**：同上，传 `prompt_version` 参数 → 两次结果
并排展示（左右 DAG + 结构差异）。 这个交互是评测引擎的最小可用版本，无需离线脚本。

---

### 4.3 新增后端 API 一览

| 端点 | 用途 | 实现要点 |
|---|---|---|
| `GET /api/workflows/{id}/doctor` | DSL 静态 lint | 纯 Python 规则，无 LLM，毫秒级；rules 独立模块 `workflows/doctor.py` |
| `GET /api/synthesis/cases` | case 列表（含元数据）| 扫 `~/.ginno/synthesis/` 目录，读 output.json |
| `GET /api/synthesis/cases/{id}` | case 详情 | 读 input.json + attempts.jsonl + outcome.json |
| `GET /api/synthesis/stats?days=N` | 漏斗指标 | 聚合计算，可加简单缓存（文件 mtime 版本） |
| `POST /api/synthesis/replay/{id}` | 重新总结（trace 重放）| 读 input.json，直接调 model.ainvoke，追加 attempt |

---

### 4.4 Doctor 规则模块（`workflows/doctor.py`）

独立文件，供：Inspector API / propose_edit 校验 / summarize 草稿重试 hint 三处复用。

```python
# workflows/doctor.py
from .dsl import normalize_dsl

def run_doctor(dsl: dict) -> dict:
    """返回 {"errors": [...], "warnings": [...]}，每项含 rule/node_id/message。"""
    d = normalize_dsl(dsl)
    errors, warnings = [], []
    nodes = d.get("nodes") or []
    context_initial = (d.get("context") or {}).get("initial") or {}
    # 预计算：每个 key 的产出来源
    writes_sources = {}                  # key → node_id
    for n in nodes:
        for k in (n.get("writes") or {}).keys():
            writes_sources[k] = n["id"]
    for k in context_initial:
        writes_sources.setdefault(k, "__initial__")
    loop_vars_in_scope = set()           # loop body 内可访问的 as 变量
    loop_bodies = {n.get("body"): n.get("as") for n in nodes
                   if n.get("type") == "loop" and n.get("body")}
    for n in nodes:
        nid, nt, goal = n.get("id"), n.get("type"), n.get("goal") or ""
        loop_as = loop_bodies.get(nid)   # 该节点是某个 loop 的 body
        # 规则 1：loop.over 引用 context.X 但无来源
        if nt == "loop":
            over = n.get("over") or ""
            if over.startswith("context."):
                key = over[len("context."):]
                if key not in writes_sources:
                    errors.append({"rule":"loop.over.no_source","node_id":nid,
                        "message":f"loop '{nid}' over=context.{key} 无上游 writes/initial 来源"})
        # 规则 2：goal 引用 {{context.X}} 但 X 无来源（loop as 变量除外）
        import re
        for m in re.finditer(r"\{\{context\.([a-zA-Z0-9_]+)\}\}", goal):
            key = m.group(1)
            if key not in writes_sources and key != (loop_as or ""):
                errors.append({"rule":"goal.context_ref.no_source","node_id":nid,
                    "message":f"节点 '{nid}' 的 goal 引用 {{{{context.{key}}}}} 但该 key 无来源"})
        # 规则 3：goal 含路径字面量但 context.initial 无对应路径声明
        if re.search(r"/Users/|/home/|/tmp/|\.md|\.json", goal):
            has_path_ctx = any(
                isinstance(v, str) and ("/" in v or v.endswith(".md"))
                for v in context_initial.values()
            )
            if not has_path_ctx:
                warnings.append({"rule":"goal.path_literal","node_id":nid,
                    "message":f"节点 '{nid}' 的 goal 含路径字面量，建议放入 context.initial"})
        # 规则 4：用户节点 id 以 __extract 结尾
        if nt != "extract" and (nid or "").endswith("__extract"):
            errors.append({"rule":"node_id.reserved_suffix","node_id":nid,
                "message":f"节点 id '{nid}' 不得以 __extract 结尾（引擎保留后缀）"})
    # 规则 5：writes 声明的键无下游消费（warn）
    consumed = set()
    for n in nodes:
        for m in re.finditer(r"\{\{context\.([a-zA-Z0-9_]+)\}\}", n.get("goal") or ""):
            consumed.add(m.group(1))
        if n.get("type") == "loop":
            over = n.get("over") or ""
            if over.startswith("context."):
                consumed.add(over[len("context."):])
    for src_nid, key in [(v, k) for k, v in writes_sources.items() if v != "__initial__"]:
        if key not in consumed:
            warnings.append({"rule":"writes.unused","node_id":src_nid,
                "message":f"节点 '{src_nid}' 声明写入 '{key}' 但下游未消费"})
    return {"errors": errors, "warnings": warnings}
```

---

### 4.5 Run 遥测补充（节点级 token/耗时，P0）

`emit()` 改动（在 nodes/builtin.py 各 node 类中统一修改）：
- `node_enter` 追加 `ts: time.time()`（§2.4 已含）
- `node_exit` 追加：
  - `ts: time.time()`（§2.4 已含）
  - `usage: {input_tokens, output_tokens}`（从 `resp.response_metadata` 提取，
    字段名随 provider 不同：openai = `token_usage`，anthropic = `usage`，做容错 fallback）

Inspector 步骤列表读法：`events.filter(e => e.node_id === step.id)` 取 node_enter.ts 和
node_exit.ts 差值 + node_exit.usage（所有 tool_call 轮求和）。

---

### 4.6 改动文件汇总（工具部分）

| 文件 | 改动内容 |
|---|---|
| `workflows/doctor.py` | 新建：doctor 规则引擎（独立，无 LLM）|
| `api/workflows.py` | 新增 `GET /api/workflows/{id}/doctor` 端点 |
| `api/synthesis.py` | 新建：cases/stats/replay 端点 |
| `server.py` | include synthesis router |
| `SettingsNav.tsx` | MAIN 列表加「总结质量」entry，icon=TrendingUp |
| `SettingsView.tsx` | 增 synthesis-quality tab 路由 |
| `settings/[tab]/page.tsx` | generateStaticParams 增 synthesis-quality |
| `settings/SynthesisQualitySettings.tsx` | 新建：漏斗指标 + case 列表 |
| `settings/SynthesisCaseDrawer.tsx` | 新建：案例详情侧抽屉 + replay 按钮 |
| `workflow/WorkflowInspector.tsx` | doctor badge/面板 + 步骤耗时/token 列 + upgrade 按钮 |
| `workflow/WorkflowLogTimeline.tsx` | 过滤 tabs（全部/工具调用/文件访问/上下文写入）|
| `lib/runtime.ts` | 新增 doctor/synthesis API 函数 |
| `nodes/builtin.py` | node_exit 追加 usage 字段（各 node emit）|

---

## 五、实施路线图

```
第一轮（约 3-4 天，可靠性改造）
  ① loop 空序列 + on_empty + loop_cap 事件 + skipped 状态（引擎 + UI）
  ④ emit() 打 ts 保真
  ③ 硬 deny 底线（.ginno/.ssh 等，止血）
  P0工具：ginno-cli 骨架 + workflow show + run 遥测

第二轮（约 3-4 天，抽取节点核心）
  ② 完整实施：
    - ExtractNode（extract.py + 注册）
    - _inject_extract_nodes（compiler.py）
    - LoopNode.add_edges back-edge 调整
    - validate_dsl writes/extract_model/id后缀校验
    - steps_from_dsl 过滤
    - build_model_by_name（models.py）
    - _drive_run_events 归因解析
    - _SYNTHESIZE_PROMPT + prompt_version bump
  P1工具：synth 记录三件套 + outcome 回填链路 + doctor 规则库 + upgrade 工具

第三轮（约 2-3 天，配套完善）
  ③ allowlist 决策落地（待 §2.3 三项拍板后）
  P2工具：replay + golden set + LLM-judge + dry-run
  👍/👎 反馈回收（P3 quality plan）

持续优化（按数据驱动）
  两阶段提示词（L1 数据证明必要后）
  弹窗精修 + 映射预览（L2 数据证明必要后）
  golden set 扩充 + 门禁 CI 化
```

**关键依赖链**：doctor 数据流规则依赖 writes 声明先落地 →
replay/评测依赖记录格式先定稿 → allowlist 复用 writes 声明机制 →
实施序不可乱。

---

## 六、三项仍待定的决策（议题③）

| # | 决策 | 我的倾向 |
|---|---|---|
| 4 | allowlist 声明位置 | context 路径字段自动识别（format:path）+ 显式 `paths` 并存 |
| 5 | 越界行为 | tool error（不 fail run） |
| 6 | workflow 步骤 bash 工具 | 默认禁，skill 解锁 |




