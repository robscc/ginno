# Mermaid 兼容层（Salvage）：方案与实现记录

- 日期：2026-10-27（方案评审通过当日实现）
- 实现：`apps/web/src/lib/mermaidSalvage.ts`（纯函数）+ `MermaidBlock.tsx`（按序重试 + 徽标）
- 起因：session `150bcced` 两张状态图「图形渲染失败 Parse error on line 4」——
  模型把 flowchart 专属的虚线箭头写进了 `stateDiagram-v2`

## 1. 实测失败集（mermaid 11.17.2，真 parser 逐项验证）

Playwright 加载官方 dist 后 `mermaid.parse()` 逐项跑「模型高频写法 × 图型」矩阵（31 项），
失败集只有 5 类；sequence/class/er/gantt/mindmap 与其余全部写法（含 stateDiagram 的
`<-->`、态名含空格、`[*]` 带标签、中文描述、标签含 `/`；flowchart 的链式箭头、
中文边标签、`v1.2` 节点、中文 subgraph、已引号文本）均通过。

| # | 图型 | 写法 | 修复 |
|---|---|---|---|
| 1 | stateDiagram | `-.->` / `-.-` / `---` / `..->`（虚线类，flowchart 专属） | L1 箭头降级实线 |
| 2 | flowchart | `A[发布(含灰度)]` 文本含圆括号未引号 | L1 加引号 |
| 3 | flowchart | `A(文本(注))` 圆括号形状内嵌套括号 | L1 加引号（一层嵌套感知） |
| 4 | flowchart | `A[a\|b]` 文本含竖线 | L1 加引号 |
| 5 | flowchart | `end` 作节点（保留字；含带形状/作源点） | L1 改名 `end_` |

## 2. 三层管线（MermaidBlock 内，仅首渲失败后触发）

- **L1 定向修补**（幂等纯文本变换，只动该动的字符）：
  - `fixStateArrows`：虚线/长横线箭头记号 → `-->`
  - `fixFlowchartText`：形状括号内含 `( ) |` 且未引号 → 整体加引号（`"`→`#quot;`）；
    箭头任一侧或带形状的 `end` → `end_`（裸的补 `["end"]` 显示文本，带形状的保留原形状）
- **L2 类型迁移**（仅 stateDiagram，L1 修不动时）：`stateToFlowchart` 整图迁成 flowchart
  ——`[*]`→START/DONE 双圆、`A --> B : label`→`A -->|label| B`、`state X {}`→`subgraph`、
  `state "d" as X`→`X["d"]`、`X : 描述`→`X["描述"]`、note 丢弃、方向保留。
  **虚线在 flowchart 合法，迁移后原样保留**——保真度高于 L1 的实线降级。
- **L3 源码展示**（原有兜底不变）。

守则：能 parse 的图一个字符不碰（候选只在失败后生成）；每个命中候选带修复种类，
徽标 tooltip 逐条说明；候选最多各试一次；最终失败时报「原始源码」的 parse 错误
（候选的错误对用户无意义）。

## 3. 验证记录（2026-10-27）

esbuild 把 `mermaidSalvage.ts` 编为 ESM，Playwright 页面同时加载真 mermaid dist，
跑 13 项构造矩阵 + 会话 4 个真实块：

- 9 个失败构造全修复（arrows ×3、quoted ×3、end ×3），修复产物再跑 salvage 幂等干净
- 会话真实块 0/1：FAIL → **OK(arrows)**；块 2/3（flowchart）不受影响
- 4 个「不该修」用例（标签文本含 end 词、已引号、多圆节点同行、sequence）零误伤

复测手法（repo 无 jsdom，这是唯一能跑真 parser 的路）：
`python -m http.server` 服务 `apps/web/node_modules` → 页面 `import` mermaid dist
与编译产物 → `mermaid.parse()` 断言。改 `mermaidSalvage.ts` 后照此回归。

## 4. P1 提示词侧（已做，2026-10-27）

`graph_prompts.build_stable_system` 在 Charts 块后追加双语约束块（`t(en, zh)`，随
`prompt_language` 选边；稳定层内容，不影响前缀缓存）：状态图不用虚线；flowchart 节点
文本含 `( ) |` 须双引号；节点不叫 end。与前端兼容层互补——提示词治新增，兼容层治存量。
（前端命中埋点仍可选，未做。）