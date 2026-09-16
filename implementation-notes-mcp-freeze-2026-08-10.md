# Implementation Notes — MCP 工具冻结修复（2026-08-10）

## 背景（排障结论）

session `7af7e21d…`（research agent）说"找不到钉钉文档 MCP"，但 `~/.ginno/mcp/mcp.json`
四个 server 实际都连着（97 个工具）。根因链：

1. **graph 工具集在 session 创建时冻结**（`sessions.py create_session` → `session["graph"]`，
   `stream.py` 直接取缓存）。创建时 `all_tools` 里只有火山搜索一个 MCP 工具，
   research 的 `tools_allow: ["mcp_*", ...]` 只能匹配到它。钉钉三件套后来连上，
   冻结闭包不会长出新工具 → agent 一直只有 17 个工具。
2. **MCP 连接失败不重试**：08-06 09:41 全部 server 因 DNS `[Errno 8]` 失败后一直是死的，
   只有手动 `/api/mcp/reload` 或重启才重连。
3. world state 公告的工具数（111→113→127）是全局 live 值，与 agent 实际可调用的
   17 个严重脱节，误导模型去翻 `~/.ginno`。

## 改动计划

1. `api/stream.py`：`_maybe_refresh_session_graph(session)` —— 每 turn 开头用
   `registry.list_tools()`（不构造 langchain 工具，便宜）与 `session["mcp_tool_names"]`
   比对，不一致则重建 graph 并更新 session 三个键。WS permission resume 改读
   `session["graph"]` 当下值。
2. `mcp/registry.py`：`_connect_one` 记录失败（`_failed: name → monotonic`）；
   新增 `failed_servers` / `has_pending_failures()` / `retry_failed(cooldown_s=120)`。
3. `api/config.py` GET `/api/mcp`（UI 轮询点）：有失败且冷却已过 → fire-and-forget
   `retry_failed()`；响应增加 `failed` 字段。`stream.py` turn 入口同样触发。
4. `server_shared.py`：`spawn_bg(coro)` —— asyncio.create_task 只被弱引用，
   需要强引用集合防 GC。
5. `world_state.py`：`_agent_allowed_count` 拆出 `_agent_allowed_names`；
   `McpSection.snapshot` 按当前 agent 的 tools_allow 过滤后再计数/公告。

## 测试

- registry：fake `_LiveServer` 先失败后成功，验证 retry_failed 恢复 + 冷却生效。
- world_state：McpSection 在显式 allow 列表排除 MCP 时不公告。
- stream：stub registry + build_graph，验证变化才重建、键被更新。

## Deviations

1. **指纹一开始写错了**：初版用 `registry.list_tools()`（MCP 原始工具名，如
   `get_document_content`）对比 session 的 `mcp_tool_names`（包装名
   `mcp_钉钉文档_get_document_content`）——两者永不相等，会导致每 turn 都重建
   graph。修正：registry 新增 `list_wrapped_tools()`，命名格式与 `_wrap_tool`
   共用 `_full_tool_name()` 单一来源，并加测试钉住两者一致。
2. 第一次 `make app` 与指纹修正发生时间重叠（构建启动后才改的源码），
   bundle 内容不可信 → 测试全过后重新跑了一次 `make app`。
3. 构建命令带了 `| tail -40`，管道退出码掩盖了 make 的真实状态、输出也被截断，
   一度以为构建未完成。以产物为准：`dist/ginno-runtime`（11:39:38）→
   `Ginno.app`（11:40:15）顺序产出，`codesign` 为 "Ginno Local Code Signing"
   正式签名（非 linker-signed），构建实际完整成功。

# Implementation Notes — LLM 会话标题（2026-08-12）

## 背景

侧栏/顶栏标题原先是第一条用户消息的 40 字符截断（「进入 这个会话 ~/workspace/lab/inc-aiplat-core-」）。
现改为：首轮 turn 开始时照旧写截断占位标题（立即有侧栏标签），同时 arm
`title_llm_pending` + `title_seed`；首个未 interrupt 完成的 turn 把助手回复存
`title_assistant` 并 `spawn_bg` 后台调用，用一条 subject 总结替换占位标题，
复用既有 `session_title` WS 事件（前端零改动）。失败保留截断标题、下轮重试；
手动 rename 清 pending 永远优先（写前再读、无 await 间隔 = 无锁原子认领）。

## Deviations

1. **模型来源偏离计划**：原计划复用 session 内存里的 model 对象（compaction 模式），
   但 e2e 的 ScriptedChatModel 会被标题调用吃掉一个脚本条目，连标题 prompt 都漏进
   `model._captured`，一次弄坏 5 个既有 e2e。改为 title_gen 自己
   `build_model(meta provider/model)`：生产等价（meta 存的就是会话解析后的
   provider/model），测试里 build_model 未 patch → disabled provider 直接 raise → 干净跳过。
2. **GINNO_FAKE_LLM seam 守卫**：该 seam 下 build_model 返回占位模型，标题会变成
   占位文案首行；检测到 seam 直接跳过，packaged 演示保持确定性。
3. **真实模型返回 list content**（qwen thinking 块：`['', {'thinking': ...}, {...text...}]`），
   初版 `str(content)` 把 Python list 字面量当标题写进 meta（手动验证当场抓到）。
   修：`_content_text` 提取 text 块（镜像 compaction._msg_line），sanitize 增加
   `[{` 开头即 abort 的保险。回归测试钉住。
4. **顺手修 main 上已存在的 e2e 失败**：test_chat_happy_path 断言 names[0]=="turn.start"，
   但首轮 session_title 事件先于 turn.start（_touch_session_title 所致），改为
   断言 session_title→turn.start 顺序。
5. test_packaged_ui_playwright 失败为**既有**问题（跑的是 make app 之前的旧 bundle，
   且失败在 agent 列表渲染、与标题无关），未动。
