# Parked（挂起方案）

暂时搁置、但方案已成型、随时可重启的设计文档目录。**这里的东西不是废弃，是"排队"**。

## 目录内容

| 方案 | 搁置日期 | 优先级/预估 | 一句话 |
|---|---|---|---|
| [tabbed-workspace](./tabbed-workspace-design.md)（含 [HTML 原型](./tabbed-workspace.html)） | 2026-11-07 | P2 · 24-40h | 会话内多标签工作台：artifact/文件/浏览器/任务日志 tab 化，与对话同屏 |
| [byom-remote-machines](./byom-remote-machines-design.md) | 2026-11-15 | **已吸收，不排队** | 自带机器作执行节点。**特性已合并进 [A2A 连接器设计](../design/a2a-connector-design.md)**（其 L3 `turn` 语义等于 A2A `SendMessage`）；本文不会原样重启，保留作设计依据 |

## 规则

- 进入条件：方案文档 + 原型齐备，但当下不做（优先级让位 / 等依赖 / 等排期）。
- 移出条件：进入正式排期时移回 `docs/`（深度设计可去 `docs/design/`），并在文内去掉 🅿️ 状态头。
- 重启评估要点随方案记录在各自文档头部（如 tabbed-workspace：用户高频会话常一次产出 ≥3 产物时价值最大；若先做 MCP Apps，其 iframe UI 可直接注册为工作台第五种 tab，两者同做省一半工作量）。
