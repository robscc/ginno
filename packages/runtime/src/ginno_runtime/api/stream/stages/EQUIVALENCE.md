# _stream_graph → stages/ 四阶段迁移等价性核对清单（批次 5-2）

基线：main @ be1e4e8 的 `engine._stream_graph`（engine.py:119-1769，1650 行）。
铁律：复制式对齐，非改写。旧 `_stream_graph` 一行未动（`git diff engine.py`
为空）；分流入口是 turn.py 的 import 行（`from .stages import
dispatch_stream_graph as _stream_graph`），默认路径与 legacy 引用同一函数对象。

## 一、机械变换规则（仅此两类，全量适用）

| # | 变换 | 说明 |
|---|------|------|
| 1 | 局部变量 → `ctx.<同名属性>` | 字段名与 legacy 局部变量一一同名；闭包 nonlocal（`ws_closed`/`_steer_user_arrived`）变 ctx 属性写 |
| 2 | legacy 模块级名字 → `eng.<name>` 属性访问 | legacy 读 engine 模块全局；monkeypatch 经 `_PatchMirrorModule` 落在 engine 命名空间，属性访问保证同刻同对象（值快照 import 会漂移，**禁止**） |

## 二、逐段对照表（legacy 行号 → staged 位置）

| legacy engine.py | 语义段 | staged 位置 |
|---|---|---|
| 119-124 | 签名 + docstring | run.py `stream_graph_staged` |
| 125-131 | begin_ask_budget + 预初始化开头 | run.py 主函数 + ctx.py `__init__` |
| 131-166 | 预初始化（saw_interrupt…stop_evt 占位） | ctx.py `__init__`（stop_evt=占位 Event，legacy 165 同防线） |
| 168-186 | turn_id/ui_turn_id 解析 + emit 工厂 | run.py `_setup_identity` + streamio.py `make_emit` |
| 200-204 | slug/session_id/agent_id/usage_source | run.py `_setup_identity` |
| 205-212 | **注册表写入点①** `_RUNNING_TURNS`/`_RUNNING_TURNS_AT` | run.py `_setup_identity` |
| 213-221 | note_turn_start + `_TURN_STOP.setdefault` | run.py `_setup_identity` |
| 222-224 | ws_closed = False | run.py `_setup_identity` |
| 225-247 | safe_send 闭包 | streamio.py `make_safe_send` |
| 249-252 | DeltaCoalescer 装配 | run.py `_setup_io` |
| 254-269 | keepalive + `_ka` | streamio.py `keepalive` + run.py `_setup_io` |
| 271-274 | `_pre_turn_todos` 快照 | run.py `_setup_io` |
| 280-288 | `_steer_user_arrived` | run.py `_setup_stream` |
| 289-345 | `_steer_absorbed` 闭包 | streamio.py `make_steer_absorbed` |
| 346-354 | steer 注入 config + stream 建立 | run.py `_setup_stream` |
| 355-432 | 循环前状态/accumulators（21 个） | run.py `_setup_stream` |
| 433-484 | turn.start / turn_resume | run.py `_emit_turn_start` |
| 485-507 | stop_waiter | run.py 主函数 try 体内 |
| 508-536 | chunked_stream 闭包（看门狗+stop） | streamio.py `chunked_stream` |
| 537-548 | async for + chunk.type gate | run.py 主循环 |
| 549-651 | messages 文本/thinking/叙述/attach/搜索 | streamio.py `handle_messages_text` |
| 652-727 | messages tool_call_chunks（start/args 流） | toolloop.py `handle_messages_tool_chunks` |
| 728-759 | custom（模型退避 notice；其余吞） | streamio.py `handle_custom` + run.py 循环 `continue` |
| 760-765 | updates 分发 flush | toolloop.py `handle_updates` |
| 766-995 | updates/agent（预警/usage/tool_calls） | toolloop.py `_updates_agent` |
| 996-1058 | updates/tools（tool.end/workflow/effects） | toolloop.py `_updates_tools` |
| 1059-1118 | updates/permission（denial 收口） | toolloop.py `_updates_permission` |
| 1119-1152 | `__interrupt__` 三种停车 | permission.py `handle_interrupt` |
| 1153-1181 | attach 尾片 + TODO 关联 + `*.changed` | finalize.py `post_stream_flush` |
| 1182-1433 | completed 分支（memory/title/goal/team/mods/message.end/linked） | finalize.py `finalize_completed` |
| 1434-1440 | parked 分支（paused_at_interrupt log） | run.py 主函数 |
| 1441-1467 | except TurnStopped（heal + turn.stopped） | closeout.py `handle_turn_stopped` |
| 1468-1600 | except Exception 前半（steer-continue/auto-retry） | closeout.py `handle_turn_error` |
| 1600-1744 | except Exception 后半（error 卡 + mods） | closeout.py `handle_turn_error` |
| 1745-1769 | finally（coal/ka/stop_waiter/注册表②④/steer/team/ws_closed） | finalize.py `finalize` |

## 三、全局注册表写入点对照（脚本验证，代码点数一致）

| 写入点 | legacy | staged | 验证 |
|---|---|---|---|
| ① `_RUNNING_TURNS[sid]=` / `_RUNNING_TURNS_AT[sid]=` | 1+1 | 1+1（run.py `_setup_identity`） | grep 计数相等 |
| ② `_TURN_STOP.setdefault` + `.pop` | 1+1 | 1+1（run.py:118 / finalize.py） | grep 计数相等（docstring 文字除外） |
| ③ `_PENDING_RESUME.add`×3 + `_PENDING_KIND[...]=`×3 | 3+3 | 3+3（permission.py 三 interrupt 分支） | 同上 |
| ④ finally：`_RUNNING_TURNS.pop`/`_RUNNING_TURNS_AT.pop`/`_PENDING_RESUME.discard`/`_PENDING_KIND.pop` | 1+1+1+1 | 1+1+1+1（finalize.py） | 同上 |

时序注释：② 在 `_user_stopped` 捕获**之后**（major-B 顺序）；④ 与 steer
keep/clear、team settled、lastwords note_turn_end 的相对顺序逐行对应
（finalize.py 内行序 = legacy finally 行序）。

## 四、 knowingly 机械差异清单（无语义差异）

1. **continue → return**：messages 类型 gate 与 custom 分支尾的 `continue`
   变为函数 return / 编排器循环 continue（同一 async for 迭代语义）。
2. **闭包 → ctx 工厂**：emit/safe_send/_steer_absorbed/chunked_stream/
   _emit_attach_tags 改为接收 ctx 的模块级函数；每次调用读 ctx 属性
   （不快照），与闭包读局部变量的时序等价（`session_id`/`ui_turn_id`
   在 try 内重赋值后，emit 立即读到新值——golden 的 frame_session 证实）。
3. **递归调用**：except 内 steer-continue/auto-retry 的
   `await _stream_graph(...)` → `await stream_graph_staged(...)`（同一实现
   的递归，即 legacy 递归 legacy 的对应物）。
4. **except 内 return**：`handle_turn_error` 返回 bool，编排器据此 return；
   两条路径 finally 均照常执行（Python try/finally 语义不变）。
5. **相对导入层级 +1**：stages 比 engine 深一级包（`from ...` → `from ....`）。
6. **函数内延迟 import 的环规避**：closeout→run 的 `stream_graph_staged`
   在函数体内 import（legacy 无此环——单函数自递归）。
7. **异常传递**：legacy except 块内裸抛（如 `_session_meta_patch(slug,...)`
   在 slug 未绑定时 NameError）→ staged 为同位置 AttributeError，同样
   传播出编排器（finally 先跑）——失败模式等价。
8. **`_coal`/`_ka` 未初始化的 finally 防线**：legacy 依赖
   `except Exception: pass` 吞 NameError；staged 同结构吞 AttributeError
   （ctx 属性缺失时）——等价。`stop_evt` 占位 Event 两边同构
   （ctx.__init__，对应 legacy 165）。

## 五、验证证据

| 验证 | 结果 |
|---|---|
| golden 双路（3 场景 × legacy/staged 断言同一快照） | 6/6 passed，快照零字节漂移 |
| emit 事件名多重集（legacy vs staged，脚本统计） | 24 种/30 次 = 24 种/30 次 |
| safe_send 调用点 | 30 = 30 |
| `_coal.feed`/`_coal.flush` 调用点 | 4/8 = 4/8 |
| ws 系（keepalive/steering/llm_events/stall_repro） | staged 下 57 passed |
| ws 系 e2e（ask_user/permission/workflow_ws/chat/files） | staged 下 36 passed |
| ruff check stages/ + turn.py + golden 测试 | All checks passed |
| 性能 smoke（交替 6 轮 × 3 turn，两步工具轮墙钟） | legacy 中位 56.3ms / staged 58.5ms = 1.039x（同数量级） |
| 全量 pytest（GINNO_STREAM_ENGINE=staged 全局） | 见提交信息/批次报告 |
