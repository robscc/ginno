# Session Tool Policy — 会话级 Skill/MCP 按需禁用

> 状态：**已拍板（2026-11-12：黑名单制 / 仅裁 MCP+非内置 skill，builtin 不参与 / 入口在输入框）**
> 动机：MCP 工具 schema 是上下文最大固定项（68 工具 ≈12-18k token/turn
> 全量注入），纯编码会话用不到钉钉文档/日历却每轮付费。

## 1. 数据模型

`session.meta.tool_policy`（随 checkpoint 持久化，遗言恢复自动带回）：

```jsonc
{
  "mode": "custom",            // "all"(默认) | "minimal" | "custom"
  "mcp_disabled": ["srv_x"],   // server id 黑名单
  "skills_disabled": ["todo"]
}
```

- `all`：现状全量（默认，存量会话零迁移零行为变化）
- `minimal` 预设：仅内置核心（read/write/edit/bash/glob/grep/ask_user…），
  MCP 全关、skills 全关
- `custom`：默认全开 + 勾掉不要的

## 2. 生效点（runtime）

- **MCP**：`build_tools(session)` 按 policy 过滤 server→tools 映射；工具集
  每轮随 config 构建 → **下一 turn 生效**；运行中 turn 不热插拔（不中断、
  不出现工具中途消失）
- **Skills**：系统提示 skills 目录段过滤行；`use_skill` 调禁用项返回明确
  错误（「本会话已禁用，会话设置可开启」），不静默失败
- **继承**：subagent / worker / delegation 继承主会话 policy（子会话能力
  不宽于父）
- mod（Mod Store）工具门是独立机制，不并入（避免两套开关打架）

## 3. UI（会话设置 → 上下文管理）

```
┌ 上下文管理 ────────────────────────┐
│ 预设: [全部] [最小] [自定义]        │
│ ▼ MCP 服务器               省 ~14k │
│   ☑ 钉钉文档 (44 工具)             │
│   ☑ 钉钉日历 (21 工具)             │
│   ☐ 网页搜索 (3 工具)              │
│ ▼ Skills (40)                省 ~1k │
│ 预计每轮节省: ~15.2k tokens        │
│ [保存 · 下一轮生效]                 │
└────────────────────────────────────┘
```

- 每项显示工具数 + 预估 token（schema 长度统计一次缓存）
- 保存 → notice「已更新工具策略，下一轮生效」
- 会话列表行加 `⊘N` 角标提示裁剪过

## 4. P2 进阶（后置）

- @ 面板快捷恢复：@ 被禁 server 的工具时浮出「开启？」一键恢复
- agent 模板预置默认 policy
- turn 统计建议：「20 轮未用 MCP，建议最小化（省 ~14k/轮）」

## 5. 分期

| 期 | 内容 | 量 |
|---|---|---|
| P0 | 数据模型 + runtime 过滤（MCP/skills/继承）+ 单测 | ~1 天 |
| P1 | 会话设置 UI（预设三档 + 勾选 + token 预估 + 生效提示） | ~1 天 |
| P2 | @ 快捷恢复 / 模板预置 / 自动建议 | ~半天 |

## 6. 拍板结论（2026-11-12）

1. **黑名单制** ✅
2. **裁剪对象仅 MCP servers + 非内置 skills**——builtin 工具与内置 skill
   一律不参与 minimal/custom 裁剪（minimal = MCP 全关 + 非内置 skill 全关）
3. **UI 入口在输入框（Composer）**：工具箱按钮弹「上下文管理」面板
