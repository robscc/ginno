# 对话内 Mermaid 图形渲染 · 产品方案

> 状态：**P0 + P1 已实现**（2026-10-26），P2/P3 待排期。落地中的三处修正见 §12。
> 范围：Ginno 桌面端对话流（含历史重放、Wiki 页阅读视图）对 ` ```mermaid ` 围栏代码块的图形化渲染
> 一句话结论：**以「Markdown 围栏自动渲染」为唯一主路径，前端纯渲染、后端零协议改动**；`render_widget(kind='diagram')` 只作为后续可选增强，不做第一期。

---

## 1. 现状与问题

| 事实 | 代码位置 |
|---|---|
| 对话正文由单一 Markdown 渲染器承载，`react-markdown@10` + `remark-gfm` + `rehype-highlight` | `apps/web/src/components/chat/Markdown.tsx` |
| 围栏代码块统一被 `pre` override 接管 → `<CodeBlock lang>`（标签 + 复制 + `<pre>`） | 同上 `components.pre` / `CodeBlock()` |
| 文本块在**流式期间逐 token 重渲染**（`streaming && last` 时追加光标） | `components/chat/blocks.tsx:142-145` |
| `Markdown` 被多处复用（对话、Wiki 阅读视图、表格预览、ask_user 卡、mod 节点） | `PageViewer.tsx` / `SheetViewer.tsx` / `AskUserCard.tsx` / `mod/modElements.tsx` |
| 已有富输出通道：`render_widget` → WS `widget.emit` → `WidgetBlock`（`stat_list` / `chart`） | `runtime/tools/render_tools.py`、`chat/blocks/widgets.tsx` |
| CSP 严格：`default-src 'self' tauri: asset:`，允许 `'unsafe-inline'` 样式/脚本 | `apps/desktop/tauri.conf.json:26` |
| 主题：CSS 变量 token（`--base/--card/--line/--txt/--muted/--chart-N`），浅色靠 `html.light` 类切换 | `app/globals.css`、`settings/GeneralSettings.tsx:15` |
| 系统提示词已有「结构化输出 / 图表」两段，但**没有图（diagram）指引** | `runtime/graph_prompts.py:136-161` |

**问题**：模型（以及用户粘贴的内容）现在天然会输出 ` ```mermaid `，但界面把它当普通代码块——高亮标签写着 `mermaid`、内容是缩进不齐的 DSL 文本。架构图/时序图/状态机这类「一说就绕、一画就懂」的表达被降级成需要人脑在文本里重建拓扑。

**机会点**：这是一次纯前端、低风险、高感知的体验升级，且天然获得三件事——历史消息重放一致（源码在 checkpoint 里）、离线可用（本地桌面应用）、跨页面生效（Wiki 页同一渲染器）。

---

## 2. 目标与非目标

### 目标
1. 对话里出现 ` ```mermaid ` 围栏 → 直接渲染为图，**零点击、零额外工具调用**。
2. 深浅主题、字号、容器宽度都跟随现有 token，视觉上与 `chart` 卡同族。
3. 流式期间不抖动、不闪错、不重复布局；消息落定后一次成形。
4. 图形可复制/导出/放大，源码可查看，出错可自愈（降级为代码块 + 提示）。
5. 历史重放、会话导出、Wiki 阅读视图行为一致。

### 非目标（本期不做）
- 不做图内交互（点击节点跳转、内联编辑、拖拽布局）。
- 不做图编辑画布 / 双向编辑（mermaid 源码 ⇄ 画布）。
- 不引入第二套图形 DSL（PlantUML / Graphviz）。
- 不做服务端预渲染（PNG 快照）——本期全靠客户端。

---

## 3. 用户场景

| 场景 | 典型诉求 | 图种 |
|---|---|---|
| 架构/方案评审 | 「这个 sidecar 启动链路怎么走的」 | `flowchart` |
| 故障排查 | 「这三个进程的调用顺序和错误回退点」 | `sequenceDiagram` |
| 状态机/生命周期 | 「turn 的状态流转：pending→running→paused」 | `stateDiagram-v2` |
| 数据模型 | 「三张表怎么关联」 | `erDiagram` |
| 排期 | 「这三周的阶段性交付」 | `gantt` |
| 组织结构/分解 | 「任务怎么拆的」 | `mindmap` / `flowchart` |

规模预期：单条回答 1~2 张图；图规模 5~20 节点（超过就该拆分，靠提示词约束）。

---

## 4. 方案选型

| | A. Markdown 围栏自动渲染（**选中**） | B. `render_widget(kind='diagram')` 工具 | C. 独立图形产物 + 侧栏画布 |
|---|---|---|---|
| 模型心智 | 输出 ` ```mermaid `，最自然 | 需学会调工具 + 传大段字符串 | 同 B，还要落文件 |
| 历史重放 | 源码在消息文本里，天然一致 ✅ | 需在 `messages_ui.py` 重放路径补 widget 还原 | 需额外产物注册链路 |
| 用户粘贴/外部来源 | 直接生效 ✅ | 不生效 | 不生效 |
| 流式体验 | 天然可见「正在长出来」 | 参数拼完才 emit，无中间态 | 同 B |
| 后端改动 | **0** | 提示词 + 重放 + 测试 | 大 |
| 扩散面 | 对话 + Wiki + 表格预览都受益 ✅ | 仅对话 | 仅产物面板 |
| 失败模式 | 语法错 → 降级代码块，可读 | 工具调用失败，静默丢失 | 文件写坏 |

**结论**：A 为主路径。B 的价值只剩「让模型可显式指定主题/标题/高度」，收益远小于复杂度，放到 P3 再评估（届时复用同一个 `MermaidDiagram` 组件，仅是入口不同）。

---

## 5. 技术架构

### 5.1 数据流

```
runtime（LLM 输出 markdown）
  └─ WS text.delta ──▶ store blocks[].text
        └─ blocks.tsx: <Markdown text streaming={streaming && last} />
              └─ Markdown.tsx: components.pre override
                    ├─ lang !== 'mermaid'  → 现有 <CodeBlock>（不变）
                    └─ lang === 'mermaid'  → <MermaidBlock source streaming />
                          ├ streaming=true  → 代码块占位（带脉冲条 / "生成中"）
                          └ streaming=false → 动态 import('mermaid') → render() → <svg>
```

关键点：**渲染决策完全在前端、由 markdown 源码驱动**。WS 协议、checkpointer、历史重放、导出全部不动。

### 5.2 新增/改动文件

| 文件 | 改动 |
|---|---|
| `apps/web/src/components/chat/MermaidBlock.tsx` | **新增**。渲染组件 + 串行渲染队列 + 主题同步 + 工具栏 |
| `apps/web/src/components/chat/MermaidPanZoom.tsx` | **新增**（P1）。缩放/拖拽/全屏（可先用 CSS `overflow:auto` 兜底） |
| `apps/web/src/components/chat/Markdown.tsx` | `pre` override 增加 `lang === "mermaid"` 分支；`Markdown` 新增可选 `streaming?: boolean` 透传 |
| `apps/web/src/components/chat/blocks.tsx` | 文本块传 `streaming={streaming && last}` |
| `apps/web/messages/{zh-CN,en}/composer.json` | `markdown.*` 下增加 mermaid 相关文案 |
| `apps/web/package.json` | `mermaid@^11`（仅此处新增依赖） |
| `packages/runtime/src/ginno_runtime/graph_prompts.py` | P2：新增「图形输出」system prompt 段 |

### 5.3 `MermaidBlock` 核心实现要点

```tsx
// 1) 源码提取：复用已有的 nodeText()，把 rehype-highlight 包出的 span 还原成纯文本
const source = nodeText(children).replace(/\n$/, "");

// 2) 渲染是全局单例 + 非重入（mermaid 内部有模块级状态，并发 render 会串图）
let queue: Promise<unknown> = Promise.resolve();
function enqueue<T>(job: () => Promise<T>): Promise<T> {
  const run = queue.then(job, job);
  queue = run.catch(() => {});
  return run;
}

// 3) 初始化一次（幂等），主题变化时重新 initialize + 重渲染
mermaid.initialize({
  startOnLoad: false,
  securityLevel: "strict",        // 不执行 HTML/脚本、禁用 click 回调
  theme: light ? "default" : "dark",
  themeVariables: {                // 与 globals.css token 对齐，避免"外层深色、图里白底"
    background: "transparent",
    primaryColor: "rgb(--card2)",  // 实现时读 computedStyle 取真实 RGB 三元组
    primaryTextColor: "rgb(--txt)",
    primaryBorderColor: "rgb(--line2)",
    lineColor: "rgb(--muted)",
    fontSize: "13px",
    fontFamily: "inherit",
  },
  flowchart: { htmlLabels: false, curve: "basis" },  // 关 htmlLabels：避免 foreignObject 在 WKWebView 的字体/裁剪问题
  suppressErrorRendering: true,    // v11：错误不要往 DOM 里塞红色错误框
});

// 4) 稳定后才渲染：streaming 期间一律占位
const { svg } = await enqueue(() => mermaid.render(`m${useId()}`, source));
```

**流式的三级策略**（防抖动是本期最容易做坏的地方）：

| 阶段 | 呈现 | 理由 |
|---|---|---|
| `streaming=true`（本轮仍在生成） | 源码淡显代码块 + 「图形生成中…」骨架条 | 半截 mermaid 语法 100% 报错，边流边渲染 = 每 token 一次解析 + 一次重排 |
| 文本稳定 ~300ms 且 `streaming` 未翻转 | 仍占位 | 防「模型写完图后又继续写正文」的瞬时落定误判 |
| `streaming=false` | 渲染成图；失败则降级为代码块 + 错误行 | 只渲染一次，成本可预测 |

> 备选（不推荐）：流式期间做「渐进渲染」——每 800ms 试渲染一次吞掉错误。收益是"图逐渐长出来"的爽感，代价是解析开销 10×、错误抖动、代码复杂度高。P3 可做实验。

### 5.4 主题同步

- 监听 `document.documentElement` 的 `class` 变化（`MutationObserver`，`light` 类增删）或复用 settings store 的 theme 值。
- 变化时：`mermaid.initialize` 重跑 → 所有已挂载的 `MermaidBlock` 重新 `render`。
- 用模块级「主题版本号 + 订阅」实现，避免每个图各自挂 observer。
- **渲染失败时不要保留旧主题的 svg**（会出现白底黑字贴在深色气泡里），统一走降级代码块。

### 5.5 安全

- `securityLevel: 'strict'`：mermaid 侧禁用 HTML label 注入与 `click` 回调，输出经 DOMPurify 清洗。
- 关闭 `htmlLabels`：同时规避 `foreignObject` + WKWebView 的裁剪问题。
- **不放宽 CSP**：mermaid 是打包进 bundle 的 ESM，无外链请求、无 `eval` 必需（如需可临时评估 `script-src 'unsafe-eval'`，当前配置里已有 `'unsafe-eval'`，可不再动）。
- 图形渲染结果为不可信输入生成的 SVG：只读展示，不加 `dangerouslySetInnerHTML` 之外的任何事件绑定；`<svg>` 内不挂 onClick。
- 链接：mermaid 图内若含 URL，不接管点击（`securityLevel: strict` 下本就不可点）。

### 5.6 性能预算

| 项 | 目标 | 手段 |
|---|---|---|
| 主 bundle 增量 | **0 KB** | `await import("mermaid")` 动态引入，独立 chunk |
| 首次渲染耗时（含 chunk 解析） | < 300ms | 桌面端本地加载；接受一次性成本 |
| 单图 render | < 80ms（≤20 节点） | 串行队列 + 结果缓存 |
| 重复渲染 | 命中缓存 0ms | `Map<sourceHash+theme, svg>`，LRU 上限 50 |
| 离屏图 | 不渲染 | `IntersectionObserver`，进入视口 200px 再渲染（P1） |
| 超长历史 | 不劣化 | 上面的缓存 + 离屏策略即足够 |

### 5.7 交互（P1）

图卡右上角 hover 工具条：
- `源码` 折叠/展开（默认收起，展开为可复制代码块）
- `复制源码`
- `下载 .svg`
- `复制 PNG`（离屏 canvas 2× 倍率，供粘到钉钉/文档）—— P1
- `放大`（全屏遮罩 + 滚轮缩放 + 拖拽平移）—— P1
- 尺寸策略：默认 `width:100%` 自适应容器；超出高度 480px 折叠为「展开大图」；`max-width` 与正文列一致。

### 5.8 错误与降级

| 情况 | 表现 |
|---|---|
| 语法错误 | 代码块（可复制）+ 一行 `图形渲染失败：<mermaid 首条错误> · 显示源码`，不阻塞消息其余部分 |
| 图过大（> 60 节点或超宽 3 倍） | 正常渲染 + 「展开大图」，不报错 |
| 未知/不支持的图类型（v11 不支持的自定义图） | 降级代码块 |
| chunk 加载失败（离线包损坏） | 降级代码块，控制台打点 |
| 用户关掉开关 | 全量降级代码块 |

**降级必须是原地、静默、可复制的**——用户永远能拿到源码，这是保底体验。

---

## 6. 后端与提示词（P2）

不需要协议改动，但需要「教模型怎么画」才好看。在 `graph_prompts.py` 的 charts 段后追加一段（示例）：

```
Diagrams: when the answer describes flow, sequence, state transitions, data
relationships or a schedule, prefer ONE ```mermaid block over prose. Rules:
- pick the smallest fitting type: flowchart TD/LR, sequenceDiagram,
  stateDiagram-v2, erDiagram, gantt, mindmap.
- label nodes in the user's language; keep labels <= 12 chars.
- <= 20 nodes per diagram; split into 2 diagrams instead of one huge one.
- never wrap the fence in another fence; no styling directives (classDef) unless asked.
- after the diagram, one line of takeaway — do not restate every node.
```

配套：`graph_prompts.py` 属稳定前缀（cache-safe），加段落在**同等条件下**会整体换一次前缀，只发生一次，可接受；建议与其它 prompt 改动同批发布。

---

## 7. i18n 文案（`messages/*/composer.json` → `markdown.*`）

`mermaid.generating` / `mermaid.source` / `mermaid.copySource` / `mermaid.downloadSvg` / `mermaid.copyPng` / `mermaid.expand` / `mermaid.renderFailed` / `mermaid.showSource` / `mermaid.tooLarge`

---

## 8. 分阶段计划

| 阶段 | 内容 | 工时 | 交付判据 |
|---|---|---|---|
| **P0 可用** | 围栏识别 + 动态 import + strict 安全 + 深浅主题 + 错误降级 + 流式占位 | 1.5d | 对话里 ` ```mermaid ` 出图；浅色/深色都对；语法错不炸 |
| **P1 好用** | 工具栏（源码/复制/SVG/PNG/全屏）、render 缓存、离屏渲染、i18n 补齐、设置开关 | 2d | 20 图的长会话滚动不卡；图可导出粘进钉钉 |
| **P2 好看** | 系统提示词「图形输出」段、错误文案打磨、埋点（渲染成功/失败/耗时/图种） | 1d | 模型自发画图率↑；失败率可观测 |
| **P3 增强** | `render_widget(kind='diagram')` 入口、图形产物登记到 Artifacts、渐进渲染实验 | 3d | 按需评估 |

**验收流程要求**：`make app` 后必须**完全退出并重启 Ginno** 再验证（否则命中 PyInstaller zlib 已知故障，`CLAUDE.md` §已知故障模式 1）。

---

## 9. 验收清单（DoD）

- [ ] ` ```mermaid ` + `flowchart TD` 正常出图，节点文字中文不乱码、不裁切
- [ ] `sequenceDiagram` / `stateDiagram-v2` / `erDiagram` / `gantt` 各一张冒烟通过
- [ ] 流式生成过程中无抖动、无"先红框后正常"、无横向滚动条跳动
- [ ] 语法错误 → 代码块 + 一行错误提示，可复制原文
- [ ] 浅色/深色切换后所有已渲染图即时重绘，无残留白底
- [ ] 历史会话重进（重放）后图与首次一致
- [ ] Wiki 阅读视图（`PageViewer`）中的 mermaid 同样出图
- [ ] 单条消息含 2 张图互不串图（并发队列验证）
- [ ] 图导出 .svg / 复制 PNG 成功，PNG 在钉钉里可读
- [ ] `pnpm typecheck` 通过；主 chunk 体积增量 ≈ 0
- [ ] 关闭设置开关后全量降级为代码块

## 10. 指标

| 指标 | 目标 |
|---|---|
| 渲染成功率（成功 / 出现 mermaid 块） | ≥ 95% |
| 平均渲染耗时（不含首次 chunk 加载） | < 120ms |
| 单会话 mermaid 出现率 | 观测项（P2 起算基线） |
| 用户「展开大图 / 复制」点击率 | 观测项（衡量是否真有价值） |
| 因图导致的报错上报数 | 0 |

## 11. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| mermaid 包体大（~1MB min，动态 chunk） | 首次渲染慢 | 动态 import + 桌面本地加载 + 缓存；不加载不进内存 |
| 边流边渲染导致崩溃/闪烁 | 体验崩坏 | 硬性规定「稳定后才渲染」，流式只出占位 |
| WKWebView 对 `foreignObject` 支持差 | 文字丢失 | `htmlLabels: false`；验收必测 WebKit |
| 模型画超大图 | 横向滚动/不可读 | 提示词约束节点数 + UI 折叠「展开大图」 |
| 主题 token 为 RGB 三元组，mermaid 需实色 | 配色不贴 | 启动时读 `getComputedStyle` 拼 `rgb(r,g,b)` |
| 图内嵌恶意 HTML | XSS | `securityLevel: 'strict'` + 关 htmlLabels |
| 与已有 `chart` 卡视觉不统一 | 界面割裂 | 同族容器样式（`border-line` + `bg-base/50` + violet 图标） |

---

## 附录 A：支持的图种取舍

第一期全放开（零成本，mermaid 自己判断）：`flowchart` / `sequenceDiagram` / `classDiagram` / `stateDiagram-v2` / `erDiagram` / `journey` / `gantt` / `pie` / `gitGraph` / `mindmap` / `timeline` / `quadrantChart` / `xychart`。

需要特殊提示的：`pie` / `xychart` 与已有 `render_widget(kind='chart')`（d3 实现）功能重叠——提示词里明确「**数值趋势/占比用 chart 工具，关系与流程用 mermaid**」，避免两套图表互相抢活。

## 附录 B：为什么不做服务端渲染成图

- 服务端（Python PyInstaller bundle）引入 mermaid-cli/headless Chrome 会立刻把安装包和启动时间打穿。
- 服务端出 PNG 后，历史重放、主题切换、缩放全部失真。
- 客户端 svg 是矢量的，缩放/导出都不掉质量。
- 唯一劣势（不支持 mermaid 的客户端如纯文本导出）由「源码保留在 markdown 里」天然兜住。

---

## 12. 实现状态与实测修正

### 已交付（P0 + P1）

| 文件 | 内容 |
|---|---|
| `apps/web/src/components/chat/MermaidBlock.tsx` | 围栏识别后的图形卡：动态 import、串行队列、主题同步、尺寸钉定、骨架占位、错误降级、工具条（复制源码 / 复制图片 / 下载 SVG / 放大 / 看源码） |
| `apps/web/src/components/chat/MermaidPanZoom.tsx` | 全屏看图：滚轮缩放（原生 passive:false 监听）、拖拽平移、Esc 关闭 |
| `apps/web/src/components/chat/Markdown.tsx` | `pre` override 增加 `lang === "mermaid"` 分支；新增 `streaming` prop；`rehypeHighlight` 加 `plainText: ["mermaid"]` 跳过无用高亮 |
| `apps/web/src/components/chat/blocks.tsx` | 文本块透传 `streaming={streaming && last}` |
| `apps/web/src/lib/mermaidPrefs.ts` | `ginno-mermaid` 偏好（localStorage + 订阅，设置页开关实时生效） |
| `apps/web/src/components/settings/GeneralSettings.tsx` | 设置 → 通用 → 「在对话里渲染 Mermaid 图形」开关 |
| `messages/{zh-CN,en}/{composer,settings}.json` | 全部文案（含缩放/关闭等补充键） |
| `apps/web/package.json` | `mermaid@^11`（11.17.2，锁文件无镜像地址污染） |

### 实测验证（dev server + 真实浏览器 DOM 断言）

探针页临时渲染 4 种图 + 1 张坏语法 + 1 段流式，逐项断言后已删除：

- `flowchart` / `sequenceDiagram` / `stateDiagram-v2` / `erDiagram` 全部 1:1 渲染，`g.node` 标签包围盒均未溢出图形框
- 同一条回答里两张图互不串图（`ginno-mmd-3` / `ginno-mmd-4` 内容各自正确）
- 坏语法 → `status=error` + 错误首行 + 源码代码块，其余内容不受影响
- 流式块 → 骨架占位，未发起 mermaid chunk 请求
- 主题切换 → 重渲染且配色反转（`fill` 由 `rgb(233,233,240)` → `rgb(23,23,30)`；深色回切命中缓存不重渲染）
- SVG → PNG 2×：726×158 的图产出 38.7 KB PNG；`Esc` 关闭全屏、缩放 120% 生效
- 首页 First Load JS 仍为 **90.8 kB**（mermaid 全量在异步 chunk，未加载时 0 请求）

### 三处与原方案的偏差（实测发现，已修正）

1. **`htmlLabels` 必须写在根级。** v11 起 `flowchart.htmlLabels` 已废弃且被根级覆盖（`config.type.d.ts`）。按原方案写图级时 `foreignObject` 依然存在；改根级后 `fo=false`，标签走 SVG `<text>`。
2. **不能沿用 mermaid 的 `width:100%` + `max-width:<自然宽>`。** 容器比图窄时整图等比缩小，文字一起变小（735px 气泡里放 1500px 流程图，字号被压到 ~6px）。改为**渲染后钉死自然尺寸**（剥掉内联 `max-width`、写死 `width/height`），装不下就横向滚动，整体看清交给「放大查看」。
3. **IntersectionObserver 需要兜底。** 窗口不可见时浏览器会限流甚至不投递 IO 回调，实测表现为「图永远空白」。修法：可见时保持懒加载语义，`document.hidden` 时 800ms 后强制放行一次。

### 尚未验证 / 下一步

- **桌面壳（WKWebView）实机**：以上验证在 Chrome 内核完成。`make app` 后必须完全退出并重启 Ginno 再验（否则撞 PyInstaller zlib 已知故障，见 `CLAUDE.md`）。重点看 SVG `<text>` 中文字体与 canvas 导出。
- **`navigator.clipboard.write([ClipboardItem])`**：WKWebView 少数场景不可用，已内置降级为下载 PNG，需实机确认。
- P2：`graph_prompts.py` 增加「图形输出」段 + 渲染成功/失败/耗时埋点。
