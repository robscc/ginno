# 外部 Agent 只读模式边界设计（issue #21）

日期：2026-11-05 ｜ 状态：已决策（方案 B：诚实命名 + prompt 契约，机制强制列为可选后续）

## 1. 现状盘点（per-backend 强制等级）

| backend | read-only 实现 | 强制等级 |
|---|---|---|
| codex | `-s read-only`（沙箱挡 shell+apply_patch，codex.py:53-56） | 机制强制 |
| pi | `--tools read,grep,find,ls`（pi.py:96-101） | 机制强制 |
| dsh | `DSH_PERMISSION_MODE=read-only`（dsh.py:161-166） | 机制强制 |
| claude-code | 无参数差异，两种 mode 都 `--dangerously-skip-permissions`（claude_code.py:69-80） | **非强制（名义）** |

claude-code 不能走 CLI 层禁工具：2026-10-04 真机矩阵 V1-V10 证明
`--allowedTools/--disallowedTools/--settings/--strict-mcp-config` 全部炸掉
2.1.x headless 延迟工具系统（claude_code.py:64-68）。OS 级 sandbox-exec
虽可行但 deprecated 且 profile 维护成本高（假成功风险），收益/成本比不成立。

## 2. 选定语义

`read-only` = **意图声明（intent declaration）**，不是安全边界：
- 对 codex/pi/dsh：恰好被机制满足（约束 = 声明 = 执行）。
- 对 claude-code：约束靠 ① prompt 显式契约前缀（模型遵守，注入型内容可绕）
  ② 调用方把委托输出当 UNTRUSTED DATA ③ 用户审查 diff。文档与 UI 必须如实。

## 3. 实施清单（文件:行号级）

### 3.1 runtime：prompt 契约 + 强制标记
- [ ] `tools/external_agents/base.py`：`ExternalBackend` 增加可选类属性
      `enforced_modes: tuple[str, ...] = ()`；codex/pi/dsh 填 `("read-only",)`，
      claude-code 留空。
- [ ] `tools/external_agent.py`（delegate_agent 构造处，`m` 解析之后）：
      mode 为 read-only 且 backend 不在 enforced_modes 时，prompt 前注入：
      `[CONSTRAINT] read-only delegation: you MUST NOT create, modify, or
      delete any file or run state-changing shell commands. Analysis and
      reporting only. State any violation explicitly in your answer.`
      （确定性字符串；注入位置在 prompt 最前，不破坏既有 brief。）
- [ ] 同文件 `_delegation_result_text`：机器头追加 `enforced=yes|no`
      （按 enforced_modes），父模型与回放 UI 可见。
- [ ] docstring 修正（external_agent.py mode 参数）：
      `"read-only" (default — analysis only; enforced for codex/pi/dsh,
      prompt-level constraint for claude-code)`。

### 3.2 测试
- [ ] `tests/unit/test_external_agents_adapters.py:103-108`：保持现有断言
      不变，新增用例——claude-code read-only prompt 前缀注入 + enforced=no、
      codex read-only prompt **不**注入 + enforced=yes。
- [ ] `tests/unit/test_external_agent_tools.py:173-185`：argv 断言不动；
      补 `_delegation_result_text` 含 enforced= 的断言。

### 3.3 UI 三处一致（明示「非强制」）
- [ ] `apps/web/messages/zh-CN/tools.json:1088-1091` 及 en 同步：
      `delegationModeHint` → "钉死 edit / read-only（意图声明；read-only 对
      claude-code 为提示级约束，codex/pi/dsh 为沙箱强制）· 保存后即时生效"；
      `modeReadOnly` → "始终 read-only（见提示）"。
- [ ] `ExternalAgentsSettings.tsx:142-144`：read-only 选项下方加一行
      text-faint 说明，文案取自上述 i18n key（新增 `readOnlyScopeNote`）。
- [ ] `docs/external-agents-design.md:9`：旧「只读是默认 + --restricted」
      段落改写指向本文档。

## 4. 不做的事（防止行为回退）
- 不改 claude-code argv（V1-V10 教训）；不引入 sandbox-exec/hook；
- 不改 codex/pi/dsh 现有强制路径；
- `delegation_mode` 配置键名不变（UI 选项文案变，schema 不变）。

## 5. 可选后续（默认不做）
若未来威胁模型升级（如无人值守委托）：给 claude-code 包 sandbox-exec
profile（写全禁 + ~/.claude 会话状态白名单 + 网络仅 api.anthropic.com），
并在 build_env 失败时 fail-closed 拒绝 read-only 委托——避免「想强制但
静默没强制」的假成功。
