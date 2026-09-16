# Implementation Notes — 缓存命中率修复 (P0–P2)

日期: 2026-08-31。依据 2026-08-28 的深度分析(真实命中率 34.9%,历史从未被缓存)。

## 计划

1. **P0 usage.py 遥测修复**:
   - langchain 1.x(anthropic 1.4.8 / openai 1.3.5)的 `usage_metadata.input_tokens`
     已是 WHOLE-prompt(含缓存部分)→ 不再重复加 cache_read/creation。
   - cache_creation 真实值在 `input_token_details.ephemeral_5m_input_tokens` /
     `ephemeral_1h_input_tokens`(网关返回 `cache_creation` 明细时 langchain 会把
     通用 `cache_creation` 置 0)→ 三者相加。
2. **P0 滚动历史断点** (graph.py):
   - 新 helper `_mark_cache_tail(history)`:从尾部选最近 2 条有内容的消息,
     在最后一个内容块挂 `cache_control: ephemeral`;拷贝消息,不改持久化状态。
   - 门槛同 `_system_message`:`_is_anthropic_model(model)` + settings.context.cache_control。
   - ToolMessage 需转成列表内容块,langchain 会把块级 cache_control 提升到
     tool_result 级(langchain_anthropic `_merge_messages` 已验证)。
   - 断点预算:system 1 + tail 2 = 3 ≤ Anthropic 上限 4。
3. **P1 MCP 排序** (mcp/registry.py): `all_langchain_tools()` 按 name 排序。
4. **P2 记忆预算** (world_state.py): `memory_max_chars` 默认 8000,
   MemorySection.render 超限截断 + 指引读文件。

## 已验证的前提(实验/源码)

- 网关 127.0.0.1:15721 支持消息区断点(写 2101 → 读 2101),TTL 5 分钟。
- langchain_anthropic 1.4.8:`_create_usage_metadata` 把缓存部分加进 input_tokens;
  `cache_creation` 明细存在时置零通用键,明细挪入 `ephemeral_5m_input_tokens`。
- langchain_anthropic `_merge_messages`:tool 内容块的 cache_control 提升到
  tool_result 级(子块不支持);相邻 Human/Tool 消息合并为一个 human 消息。
- stream.py:702 的刷新指纹已对 `list_wrapped_tools()` 排序。

## Deviations

- 2026-08-31:本地网关 127.0.0.1:15721 当前未运行(08-28 实验时在线),
  "滚动断点端到端真机验证"顺延到网关恢复后执行。已本地验证的部分:
  langchain `_merge_messages` 把 ToolMessage 块级标记正确提升到
  tool_result 级、human/AI 文本块标记保留、合并顺序不变。
- 文档中一处中英混排笔误已修正。

## 完成情况

- usage.py:input 透传 + 三键相加恢复写入量;4 个测试文件同步新语义。
- graph.py:`CACHE_TAIL_MARKS=2`,`_with_cache_mark`/`_mark_cache_tail`,
  agent_node 在两个 strip 之后、ainvoke 之前挂尾部断点(同 _system_message 门槛)。
- mcp/registry.py:`all_langchain_tools()` 按名排序。
- world_state.py:`memory_max_chars` 默认 8000,render 截断+文件指引;
  snapshot 仍存全文(变更检测语义不变)。
- 文档:usage-stats-design.md §3.5 注、architecture.md agent 节点说明。
- 测试:863 unit+api 通过,120 e2e 通过(1 skip)。
- `make app` 成功(2026-08-31),新 bundle 启动验证:sidecar 正常、WS 正常、
  无 zlib/traceback。注意:本次是先构建、后首次启动,不存在"运行中替换
  bundle"场景。
- 待办(需网关 127.0.0.1:15721 在线):跑一个含多次工具调用的 turn,确认
  `~/.ginno/usage/requests-*.jsonl` 出现 ① `cache_creation_tokens > 0`;
  ② turn 内 `cache_read_tokens` 随请求递增(不再冻结在 ~33k)。
