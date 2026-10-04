# Implementation Notes — pi backend + delegation 子会话（2026-10-04）

## 偏差记录（Deviations）

1. **claude `--restricted` 已死**：真机验证发现 claude CLI 2.1.229 移除了该
   标志（原后端在这台机器上本来就是坏的）。改为 `--disallowedTools Bash`
   + `--permission-mode plan|acceptEdits`，两模式均禁 shell——比原设计更严。
2. **pi live 运行受用户侧配置阻塞**：pi 默认 provider openrouter 无 API key。
   解析器/argv 已 fixture + 错误路径验证；等 `pi /login` 后可实测。
3. **codex 本机未装**：`--json` 解析只做了 fixture 验证 + 降级路径；
   未真机冒烟（设计已声明）。
4. **adapter 拆分为用户中途要求**：从单文件改为
   `external_agents/{base,claude_code,pi,codex}.py` + 装配层，接口
   （ExternalBackend Protocol）与实现分离；导入面保持不变。
5. **砍掉"默认 timeout 可配置"设置项**：范围控制，工具参数已有 timeout；
   如需要再加 settings 键。
6. **i18n 走 json round-trip**（messages/{en,zh-CN}/{tools,chat}.json）：
   若 git diff 显示无关行重排，为格式化差异，非内容变化。

## 已验证

* 单测 9 项（adapters + delegation sessions）
* 冒烟：record_delegation → checkpoint 回读 → `_messages_to_ui` 渲染出
  原生气泡 + tool 卡片
* 真机：claude-code stream-json success（'ok'，usage/session_id/num_turns 齐）
* 注册表：claude-code/pi 已装，codex 未装（available_backends 正确）

## 加固轮追加（2026-10-04 晚）

1. **ws.py 守卫初版相对导入差一层**（`..session_meta` → `...session_meta`），把所有
   WS 连接炸了——深度接线测试（WSConversation 驱动真实 turn）当场抓获。
2. **claude 权限标志三连坑**（见设计文档 §9.6）：plan/allowlist/strict-mcp/deny-Bash
   全都会饿死 2.1.x headless 的延迟工具系统；variadic --disallowedTools 还会吞 prompt。
   最终 `--settings` permissions.deny 方案经真机 V1-V10 矩阵验证 + 完整工具链复测
   （真实文件内容回流 + 6 轮工具循环回放）。
3. **前端渲染两处修复**：侧栏嵌套只认 subagent（delegation 会平铺顶层）→ childrenOf/
   hasVisibleParent 双处放行；delegation 会话 composer 换只读条 + ws 层拒绝 turn。
4. **pi 空输出 diagnostic 修正**（"rc=0; stderr: " 无意义 → "no assistant output"）。

## 展示修复轮（2026-10-05）

1. **委托结果注入缺信封**：`_delegation_bg` 裸注 `[delegate …]` 机器头文本，
   前端当 steering 带原样渲染（用户反馈）。改为 `format_subagent_result`
   信封包裹（正文含机器头不变，§7 溯源头），前端折 🧭 结果卡；旧版裸注入
   重放时按 `delegation=` 溯源行兜底折卡。
2. **委托卡判定**：`subagent.status` 的 result_summary 也以机器头开头
   （与注入通道竞速 dedup 后，先到的卡也能识别）；卡片按 session
   type=delegation 或机器头判定，徽标行 backend/mode/stop/turns/duration/
   tokens，确认/纠偏/升级按钮不渲染（外部 CLI 终态 + 回放只读，无意义——
   用户红框反馈）。
3. **ToolBlock started 回执**：无 stop 键时徽标显示 `started`（原显示 `?`）。
