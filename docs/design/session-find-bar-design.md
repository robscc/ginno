# Session 内搜索（⌘F Find Bar）设计

> 状态：设计稿（未实现） · 2026-11-10
> 范围：`apps/web` 前端。runtime/Tauri 侧零改动。
> 关联：窗口化渲染（TranscriptList 2026-11-08）、⌘K 会话搜索（V2 P0-1）、消息锚点（V2 P0-1/P0-2）。

## 0. 一句话方案

**搜索索引建在消息数据层**（ChatStream 的全量 `messages`，与 DOM 窗口化无关），**命中高亮用 CSS Custom Highlight API**（零 DOM 改动、不碰 React 渲染层），**跳转复用 ⌘K 已有的「扩窗 → `data-msg-id` 定位 → flash」管线**（`revealUpTo` + `ginno:scroll-to-message` 事件），浮层组件 `FindBar` 挂在 ChatStream 转录容器右上角。

## 1. 现状事实（代码锚点，本设计的全部依据）

| # | 事实 | 位置 |
|---|------|------|
| F1 | 消息数据是 ChatStream **本地全量 state**，窗口化只发生在渲染层：`messages` 全量存在，`TranscriptList` 只 render `visible = messages.slice(-headKeep)` | `ChatStream.tsx:83`；`TranscriptList.tsx:135-137` |
| F2 | 窗口化常量：`WINDOW_INITIAL=80`、`WINDOW_STEP=60`；`headKeep` 随 session 切换重置 | `TranscriptList.tsx:128-134` |
| F3 | 扩窗函数 `revealUpTo(id)`：目标在窗口外时 `setHeadKeep(messages.length - i + 8)`（+8 条上下文） | `TranscriptList.tsx:138-142` |
| F4 | 同 session 消息跳转事件 `ginno:scroll-to-message`（detail=messageId）：TranscriptList 已有监听，**内部就是 revealUpTo 扩窗 + 60 帧重试 + `scrollToMessageAnchor` + `flashMessageAnchor`** | `TranscriptList.tsx:195-218`；发送侧 `AppShell.tsx:793-805`（`openFromSearch`） |
| F5 | `scrollToMessageAnchor`：`querySelector('[data-msg-id=…]')` + `scrollIntoView({block:"center"})`；`flashMessageAnchor`：violet ring 2s；`highlightSnippet`：大小写不敏感 `<mark>` 包裹（⌘K 结果行在用） | `searchShared.tsx:53/65/85` |
| F6 | `data-msg-id` 只存在于三类行：user（HumanMessage id）、assistant 合并气泡（turn **首条** AIMessage id）、错误卡。中间 AI step / ToolMessage 命中没有对应行——现状是优雅 no-op | `TranscriptList.tsx:146-151, 323, 391, 406` |
| F7 | ⌘K 全局监听在 AppShell：`(metaKey\|\|ctrlKey) && !shift && !alt` + `key.toLowerCase()==="k"`；同 effect 还有 ⌘N；`/pin` 路由豁免 | `AppShell.tsx:826-842` |
| F8 | `Markdown` 是 **memo 组件**，注释明言「markdown 解析 + highlight.js 是 transcript 里最贵的渲染（无 memo 实测 27ms/键）」——给它传搜索词会击穿 memo，每敲一键全量历史重渲染 | `Markdown.tsx:94-98` |
| F9 | `blocksToPlainText` 是既有的「blocks→纯文本」先例，但只取 text+skill（复制语义，不含工具输出） | `CopyBubbleButton.tsx:11-21` |
| F10 | ToolBlock 长输出默认折叠（`expanded = open ?? !isLong`，`>LONG_OUTPUT_LINES` 折叠），折叠时正文不在 DOM | `toolBlocks.tsx:45, 102-141` |
| F11 | 流式文本增长走 `appendStreamDelta`（同 kind 块原地合并），live 气泡 `liveId` 每个 token 重渲染 | `streamCore.ts`（appendStreamDelta/applyBlock） |
| F12 | IME 处理先例：`isComposing || keyCode===229` 跳过快捷键 | `Composer.tsx:237-238` |
| F13 | ChatStream **常驻挂载**（AppShell 用 `hidden` 切换工作区可见性防 state 丢失），session 是 prop、内部 `messages` 随切换重建 | `AppShell.tsx:97-103` |
| F14 | 转录滚动容器：`scrollRef`（`overflow-y-auto`）在 `<div className="relative flex min-h-0 flex-1 flex-col">` 内，容器内已有 absolute 定位的 jump-latest 按钮（`absolute bottom-3`） | `ChatStream.tsx:897-907, 949-955` |
| F15 | 代码库目前没有 CSS Custom Highlight API 的使用；全局样式在 `apps/web/src/app/globals.css`，主题色 violet=`#8b5cf6`（rgb(139 92 246)）、yellow=`rgb(234 179 8)` | grep 无命中；`globals.css` |

## 2. 交互规格

### 2.1 规格表

| 项 | 规格 |
|---|---|
| 打开 | `⌘F`（macOS）/ `Ctrl+F`（Win/Linux）。当前 session 转录视图右上角弹出浮层；若已打开则聚焦输入框并全选（再次 ⌘F 同义） |
| 浮层内容 | 搜索输入框 · 命中计数 `N/M` · 上一个（↑/⇧Enter）· 下一个（↓/Enter）· 关闭 ✕；宽度 ~320px，`absolute right-4 top-2 z-40`（转录容器内，与 jump-latest 同层语言） |
| 输入即时搜索 | 防抖 150ms 后更新计数与高亮；空串=无高亮、计数隐藏 |
| 命中展示 | 已渲染区域内所有命中处背景高亮（yellow/25）；当前项更强样式（violet 背景 + 圆角）并自动滚动到视口中部 |
| Enter | 下一个（越过末项回卷到首项） |
| ⇧Enter / ⌘G | 上一个（回卷同理） |
| Esc | 关闭浮层、清除全部高亮、焦点还原到打开前的焦点元素（通常 Composer） |
| ✕ | 同 Esc |
| 会话切换 | 浮层自动关闭（query 不跨会话保留——不同会话继续搜同一词的需求低，且避免「隐藏的高亮」误导） |
| 无命中 | 计数显示 `0/0`，输入框描红（`border-red/40`）；无当前项、不滚动 |
| 大小写 | 默认不敏感（与 ⌘K `highlightSnippet` 一致）；「Aa」开关为 P2 |

### 2.2 视觉

- 浮层：`rounded-lg border border-line bg-card shadow-lg px-2 py-1.5 flex items-center gap-1.5`，与 jump-latest pill、⌘K modal 同一设计语言。
- 高亮色直接复用现有 token：普通命中 `bg-yellow/25`、当前命中 `bg-violet text-white rounded-sm`（CSS Highlight API 下用 `::highlight()` 伪元素规则实现，见 §5）。
- 行级定位反馈：跳转落点沿用 `flashMessageAnchor` 的 violet ring（不额外发明样式）。

## 3. 组件与状态设计

```
apps/web/src/lib/sessionFind.ts                    ← 纯函数：索引 + 搜索（可单测）
apps/web/src/components/chat/stream/FindBar.tsx     ← 浮层 UI（输入/计数/按钮/键盘）
apps/web/src/components/chat/stream/useSessionFind.ts ← hook：open/query/hits/active + 生命周期
apps/web/src/components/chat/stream/findHighlight.ts ← 高亮 painter（Highlight API + 降级检测）
```

### 3.1 数据流

```
ChatStream
 ├─ messages: ChatMsg[]（全量，F1）──────────┐
 ├─ liveId（流式气泡 id，F11）──────────────┤
 │                                          ▼
 │  useSessionFind(messages, liveId, sessionId)
 │    ├─ buildFindIndex(messages)   纯函数，防抖 150ms 重建
 │    │    → FindIndexEntry[]: { msgId, blockIdx, kind, text, streaming }
 │    ├─ searchFindIndex(index, query) → FindHit[]: { msgId, blockIdx, start, len }
 │    └─ state: query / hits / activeIdx / open
 │                                          │
 ├─ <FindBar …/>（open 时挂载，absolute 于转录容器）◀┘
 └─ findHighlight.paint(container, hits, activeHit)  ← 命令式，effect 里调用
```

- `useSessionFind` 在 `session?.id` 变化时整体 reset（对齐 F2 的 headKeep 重置模式）。
- 索引按消息粒度 memo：`Map<msgId, {text, version}>`，version 用 `blocks` 数组引用标识（不可变更新，引用变才重提取）——流式期只有 liveId 一条消息引用在变，其余全部命中缓存，重建成本 O(1) + O(liveMsg)。
- **命中单位 = (msgId, blockIdx, occurrence)**。`start` 是该 block 提取文本内的字符偏移，仅用于计数与排序；DOM 定位不依赖它（见 §4.2 的 painter 二次匹配），因此「提取文本」与「渲染文本」的差异（markdown 语法符号、`cleanAgentText` 清理）**不会造成定位错乱**，只可能造成极少量的命中数与肉眼差异——可接受，见 §7.3。

### 3.2 搜索文本提取（block 范围界定）

新增 `extractSearchableText(blocks: Block[]): string[]`（每 block 一段，段与索引条目一一对应）：

| block kind | 是否入索引 | 提取内容 | 理由 |
|---|---|---|---|
| `text`（user/assistant） | ✅ | `b.text` 原文 | 核心正文。assistant 侧不剥 `<ginno_citations>`/`cleanAgentText`——索引与渲染轻微不一致无害（§3.1），剥了反而要把那套 pipeline 搬进 lib |
| `thinking` | ✅ | `b.text` | 用户常搜自己看过的推理段 |
| `tool` | ✅ | `${b.name}\n${b.argsPreview ?? ""}\n${b.content}` | 工具输出是「那个报错在哪条消息」的主要搜索目标；`pending`（content="…"）不搜 |
| `question` | ✅ | question + options + answer | 停车问答可搜 |
| `steer` | ✅ | `b.text` | 中途插话是正文 |
| `skill` | ✅ | `/${b.name} ${b.text ?? ""}` | 与 F9 复制语义一致 |
| `context` | ✅ | `b.text`（成品文案） | 系统行短、可搜；`i18n_key` 不搜（key 非人读） |
| `subagent_result` / `worker_result` / `plan_update` / `subagent_brief` / `worker_assign` | ✅ | goal/summary/text 字段 | 卡片摘要可搜（P1 可再收窄） |
| `file` / `ref` | ✅ | 仅 `name` | 文件名是有效搜索目标；二进制内容不可搜 |
| `image` / `widget` / `workflow` / `sources` / `turn_meta` / `devswap_result` / `task_event` / `subagent_spawn` / `subagent_plan` | ❌ | — | 图片无文本；widget.data 结构不定；元数据非搜索目标 |

P0 落地可先只做前 7 行（text/thinking/tool/question/steer/skill/context——覆盖 95% 场景），卡片类进 P1：`extractSearchableText` 是纯函数，扩表零风险。

### 3.3 FindBar 组件（骨架）

```tsx
// apps/web/src/components/chat/stream/FindBar.tsx（设计示意，非最终代码）
export function FindBar({
  query, hits, activeIdx, onChange, onPrev, onNext, onClose,
}: FindBarProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  // 挂载即聚焦全选；再次 ⌘F 的「聚焦全选」由 useSessionFind 收到事件后
  // dispatch 一个内部 focus 请求（ref 重置 input.selectionRange）
  useEffect(() => { inputRef.current?.select(); }, []);
  return (
    <div className="absolute right-4 top-2 z-40 flex items-center gap-1.5 rounded-lg
                    border border-line bg-card px-2 py-1.5 shadow-lg">
      <input ref={inputRef}
        value={query}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.nativeEvent.isComposing || e.keyCode === 229) return; // F12
          if (e.key === "Enter") { e.preventDefault(); e.shiftKey ? onPrev() : onNext(); }
          else if (e.key === "Escape") { e.preventDefault(); onClose(); }
        }}
        className="w-44 bg-transparent text-sm outline-none …" />
      <span className={`text-[11px] tabular-nums ${hits.length ? "text-faint" : "text-red"}`}>
        {hits.length ? `${activeIdx + 1}/${hits.length}` : "0/0"}
      </span>
      <button onClick={onPrev} aria-label="上一个"><ChevronUp className="h-3.5 w-3.5" /></button>
      <button onClick={onNext} aria-label="下一个"><ChevronDown className="h-3.5 w-3.5" /></button>
      <button onClick={onClose} aria-label="关闭"><X className="h-3.5 w-3.5" /></button>
    </div>
  );
}
```

挂载点：`ChatStream.tsx:897` 的 `<div className="relative flex min-h-0 flex-1 flex-col">` 内（jump-latest 的兄弟，F14）。该容器 `relative`，天然承载 absolute 浮层；不与顶部 PlanBoard 冲突（PlanBoard 是 `z-30` 且 `pointer-events-none` 外层，FindBar `z-40` 在其上可交互；两者都在顶部时 FindBar 靠右贴边，视觉重叠区极小）。

## 4. 窗口化协同（核心深度问题 1）

### 4.1 原则：索引看数据，高亮看 DOM

- **索引/计数/上一个下一个遍历**：基于 `messages` 全量（F1）。窗口外（未渲染）消息**天然可搜**——它们一直完整躺在 state 里。
- **高亮与精确定位**：只对**已渲染**的 DOM 生效。命中当前项时才要求该消息进 DOM。

### 4.2 跳转管线（复用，零新机制）

当前项切换（Enter/⇧Enter/按钮）时：

```
1. activeHit = hits[activeIdx]
2. dispatch `ginno:scroll-to-message` 事件（detail = activeHit.msgId）
   → TranscriptList 既有监听（F4）自动完成：
     目标在窗口外 → revealUpTo(id) 扩窗（+8 上下文）→ 下一帧 [data-msg-id] 出现
     → scrollToMessageAnchor(id)（居中）+ flashMessageAnchor（violet ring）
   → FindBar 自己不碰 headKeep（状态归 TranscriptList，避免双 owner）
3. painter 轮询重试（≤60 帧，照抄 F4 的 60 帧模式）：
   在 [data-msg-id=msgId] 子树内对当前 block（按 blockIdx 匹配第 N 个可搜
   block 的 DOM 容器——见 §5.4）做文本二次匹配，建立 Range：
     命中 → CSS.highlights 注册 + 用 range.getBoundingClientRect() 把
            当前项滚到视口 1/3 处（比消息级居中更准）
     目标 block 尚未渲染（如工具卡折叠 F10）→ 停在消息级定位（ring 已闪，
            计数正确），不强行展开卡片（尊重用户折叠意愿）
```

要点：

- **事件复用不改契约**：`ginno:scroll-to-message` 的 detail 保持 string。不新增事件类型（曾有想法扩 detail 为 `{id, blockIdx}`，会破坏 `openFromSearch` 的既有发送方——不做）。
- **扩窗成本与 ⌘K 同源**：向上跳到极早消息时 `revealUpTo` 可能一次挂载数百条（F3），这正是 ⌘K 锚点已经走的路径，已被 2026-11-08 窗口化治理验证可接受（一次性 mount，非持续成本）。
- **F6 的固有盲区**：命中若落在没有独立行的中间 AI step，`msgId`（合并气泡 id）仍然有效——因为索引以**消息（气泡）**为单位提取文本，气泡 id 一定有行。真正无行的是「⌘K 服务端搜索返回 ToolMessage id」的场景，与本设计的客户端索引无关。✅ 本方案不受 F6 no-op 问题影响。

### 4.3 关闭时清理

关闭/卸载/会话切换 → `findHighlight.clear()`（注销 `CSS.highlights` 两个注册名）。高亮不落任何持久状态，重开浮层才重建。

## 5. 高亮实现选型（核心深度问题 2）

### 5.1 对比

| 维度 | (a) 渲染层注入 `<mark>` | (b) DOM textNode 包裹（post-render） | (c) overlay 定位 | (d) CSS Custom Highlight API ⭐推荐 |
|---|---|---|---|---|
| 做法 | 把 needle 穿 props 传进 `Markdown`/`ToolBlock`/… 所有 block 渲染组件，渲染时 split 文本包 `<mark>` | 渲染后用 TreeWalker 找 textNode，`Range.surroundContents` 或 splitText 插 `<mark>` | 每个命中算 `getBoundingClientRect`，绝对定位彩块盖在上面 | `Range` 对象数组注册进 `CSS.highlights.set("find-hit", …)`，`::highlight(find-hit)` 上色 |
| 对 React 的侵入 | **高**：Markdown 是 memo（F8），加 needle prop 后每敲一键全量历史 markdown 重解析（27ms/键 起步）；且要改 10+ 个 block 组件签名 | **冲突真实存在**：React 持有 textNode 引用，splitText/包裹后 React 后续 diff（尤其流式期 textContent 更新）可能操作错节点或清掉 mark；需要每帧重刷 | 零 DOM 改动，但**几何同步地狱**：流式/换行/窗口扩容/字号变化都要重算 rect；滚动要用 transform 跟随，浮层裁剪（代码块横向滚动 `overflow-x-auto`）下 overlay 会画到容器外 | **零 DOM 改动**：Range 由浏览器解析布局，滚动/换行/裁剪全部自动正确；React 重渲染替换 textNode 时对应 Range 自动失效（安全静默），只需 effect 重扫 |
| 窗口化适配 | 全窗口组件都要接 prop | 每次扩窗/流式后要重包裹 | 同左，且要重算几何 | 重扫只对 visible 窗口（见 §5.2），成本毫秒级 |
| 文本选择/复制 | mark 是 DOM 一部分，选择正常 | 正常 | overlay 盖住会挡点击/选择（要 pointer-events:none 补丁） | 完全正常（高亮只是绘制层） |
| 兼容性 | 无风险 | 无风险 | 无风险 | Chrome 105+/Safari 17.2+（对应 macOS 14.2+ 的 WKWebView）；macOS 13.x 不可用 → 降级路径 §5.3 |

### 5.2 推荐：(d) CSS Custom Highlight API，配 §5.3 降级

理由浓缩为三条：

1. **唯一不碰 React DOM 所有权的方案**——(a) 击穿 memo 性能（F8 是明文教训），(b) 与 React 更新机制正面冲突（流式期尤其危险，F11），(c) 几何同步在「流式增长 + 窗口扩容 + 代码块横向滚动」三座大山下维护成本最高。
2. **与窗口化天然契合**：painter 只扫 visible 窗口（`container.querySelectorAll('[data-msg-id]')` 限定范围），窗口外消息没有 Range 也无所谓——索引/计数本来就是数据层的。
3. **正是浏览器原生 find 的实现方式**（Chrome 105+ 的 Ctrl+F 即此 API），语义对齐用户预期。

painter（`findHighlight.ts`）核心流程：

```ts
// 能力检测（模块加载时一次）
const SUPPORTED = typeof CSS !== "undefined" && "highlights" in CSS && typeof Highlight !== "undefined";

export function paint(scrollEl: HTMLElement, hits: FindHit[], activeIdx: number): void {
  if (!SUPPORTED) return;                       // 降级：只剩滚动+ring+计数（§5.3）
  const rangeHits: Range[] = [], rangeActive: Range[] = [];
  for (const [i, h] of hits.entries()) {
    const row = scrollEl.querySelector(`[data-msg-id="${cssEscape(h.msgId)}"]`);
    if (!row) continue;                          // 窗口外：跳过（数据层计数仍含它）
    const ranges = locateRanges(row, h);         // §5.4：block 内二次匹配
    (i === activeIdx ? rangeActive : rangeHits).push(...ranges);
  }
  CSS.highlights.set("find-hit", new Highlight(...rangeHits));
  CSS.highlights.set("find-hit-active", new Highlight(...rangeActive));
}
```

`globals.css` 追加：

```css
::highlight(find-hit) { background-color: rgb(234 179 8 / 0.25); }
::highlight(find-hit-active) { background-color: rgb(139 92 246); color: #fff; border-radius: 2px; }
```

### 5.3 降级（macOS 13 / 老 Safari：无 Highlight API）

painter 静默 no-op。功能退化为：**计数 + Enter 遍历 + 消息级滚动 + violet ring flash**（全部来自 §4.2 管线，与高亮无关）。不做 textNode `<mark>` 的第二套降级实现——两套高亮路径的维护成本高于降级体验的收益；且 Ginno 的 Tauri 2 最低系统要求事实上随 WKWebView 演进，老系统占比趋零。UI 上不区分提示（行为自解释）。

### 5.4 block 内二次匹配（定位不依赖索引偏移）

`locateRanges(row, hit)`：在 `row` 子树内找到第 `hit.blockIdx` 个「可搜 block 容器」——实现为给 TranscriptList 的 block 渲染加 **一个可选 data 属性**（`data-block-idx={i}`，仅在 findBar open 时由 context 开关传入，默认不渲染该属性以避免常态 DOM 膨胀；这是对 blocks 渲染的唯一改动，纯增量、零行为变化）。容器内 TreeWalker 找 textNode，对拼接文本做 `indexOf(query)`（大小写不敏感），每处命中建 `Range(textNode, offset, textNode, offset+len)`。**与索引的 start 偏移完全独立**，markdown 渲染差异（`**bold**` 的星号消失等）只会造成 Range 匹配数与索引计数在个别 block 上不一致——以 DOM 实际匹配为准显示高亮，计数以索引为准（差 1-2 处不影响可用性，§7.3 列为已知瑕疵）。

> P0 简化选项：`data-block-idx` 也可以不做——`locateRanges` 直接对整行（`[data-msg-id]` 子树）做文本匹配。代价是同一气泡内多个 block 都含关键词时「当前项」无法区分到 block 级（active 高亮会把整行所有命中都点亮为 active）。建议 P0 就带上该属性（改动量极小：`InnerBlocks`/`UserBlocks` 循环处 `key` 旁加一个 attr），换取准确的 active 粒度。

## 6. 键盘路由（核心深度问题 3）

### 6.1 ⌘F 的注册位置：AppShell 全局 effect（与 ⌘N/⌘K 并列），经 window 事件转发

```
AppShell 既有 effect（F7）追加分支：
  else if (k === "f") {
    e.preventDefault();                          // 接管：WKWebView 无原生 find UI，
                                                 // preventDefault 兜住 dev 模式 Chrome
                                                 // 的浏览器原生 Ctrl+F
    window.dispatchEvent(new CustomEvent("ginno:find-open"));
  }
```

- **为什么不直接在 ChatStream 监听**：AppShell 的修饰键判定/`/pin` 豁免/与 ⌘K 同一入口，行为一致性最好；ChatStream 常驻挂载（F13），监听 window 事件无时序问题。
- **ChatStream 侧**：`useSessionFind` 监听 `ginno:find-open` → `session` 非空时 `setOpen(true)` + 聚焦全选请求。已打开时该事件即「聚焦全选」（规格 2.1）。
- **`/pin` 豁免**：沿用 F7 的 `pathname === "/pin"` return——pin 窗口无 ChatStream，⌘F 不响应（P2 再考虑 PinStream）。
- **⌘K modal 打开时**：modal 是全屏浮层，transcript 不可见，开 find 无意义。`searchOpen` state 在 AppShell，在追加分支里加 `if (searchOpen) return` 门控即可（一行）。
- **IME**：AppShell 现有判定不含 isComposing——⌘F 带修饰键不会是 IME 输入，无需处理；FindBar 输入框内 Enter 处理带 F12 的 isComposing 守卫（Composer 先例）。

### 6.2 与浏览器原生 ⌘F（WKWebView）

Tauri 2 macOS 壳 = WKWebView，**没有内置 find-in-page UI**，⌘F 默认无行为——接管无原生冲突；`preventDefault` 只为兜住 `pnpm dev` 浏览器调试模式的原生 find。Rust 侧（`apps/desktop/src/*.rs`）grep 无任何快捷键处理，无需改动。

### 6.3 与 ⌘K 的关系（不冲突，分工明确）

| | ⌘F（本设计） | ⌘K（既有） |
|---|---|---|
| 范围 | 当前 session 内 | 跨全部 session（服务端 `/api/search`） |
| 数据源 | 前端 `messages` state（内存，含未保存 live 内容） | sidecar search_index（持久层） |
| 形态 | 转录内嵌浮层，正文原位高亮 | 全屏 modal，结果列表 + snippet |
| 键位 | F | K——物理键不同，无抢键；两者可先后打开，⌘K open 时 ⌘F 被门控（§6.1） |

### 6.4 Esc 的归属

- FindBar 输入框聚焦（常态）：输入框 `onKeyDown` 直接消费。
- 焦点被点走（用户点了消息/工具卡）：`useSessionFind` 挂 window keydown 兜底 `if (open && e.key === "Escape") close()`——注册在捕获阶段且仅 open 时生效，不与 modal 的 Esc（SessionSearchModal 等自行处理，此时它们未开）竞争。

## 7. 边界场景

### 7.1 流式进行中的 turn（核心深度问题 4）

分层处理，避免每 token 全量重扫：

1. **索引层**：`useSessionFind` 的重建对非 live 消息全走 memo 缓存（§3.1），live 气泡条目每次重建（它引用在变）。重建本身防抖 150ms——与用户打字的防抖共享同一条路径，流式高频 delta 被 150ms 窗口合并。
2. **计数/遍历**：live 消息正常参与（用户搜刚吐出的关键词是真实诉求）；occurrence 随文本增长可能漂移（第 3 个命中变第 5 个）——**当前项锚定策略**：activeIdx 存「从尾部数第 k 个」当 activeHit 在 live 气泡内（简单实现：重建后若原 activeHit 的 msgId+blockIdx+start 仍存在则保持指向它，否则退化为同 msgId 的最近命中）。P0 从简：重建后 activeIdx 不变（序号指向的内容可能移动），计数正确性优先。
3. **高亮层**：painter **跳过 liveId 气泡**（F11 每 token 重渲染，Range 持续失效，重扫是持续成本）——live 气泡内命中不高亮、不精确定位（跳转仍滚到该气泡并 flash ring）。turn 收口（liveId 置空，messages 稳定）后下一次 paint 自动纳入。
4. **重复触发**：paint 由 `[hits, activeIdx]` 变化驱动，输入防抖（150ms）+ 跳转节流（每次 activeIdx 变化一次）天然限频。

### 7.2 代码块、工具卡等非纯文本 block（核心深度问题 5）

- **fenced code（assistant text 内）**：随 `text` block 入索引，`locateRanges` 的 TreeWalker 对 `<code>` 内 textNode 一视同仁——代码内容可搜可高亮（横向滚动容器内 Highlight API 自动跟随裁剪，这正是选型 (d) 的优势）。
- **工具卡（tool block）**：标题行（name+argsPreview）永远在 DOM；长输出默认折叠（F10）时正文不在 DOM → **索引仍含全文（能搜到、计数含它），跳转定位到卡片但不自动展开**（尊重折叠；ring flash 落在卡片上，用户点开即见）。若想更贴心：P1 给折叠态命中加一个小徽标「内含 N 处命中」。
- **widget/workflow/mermaid**：不入索引（§3.2 表）；mermaid 源码在 text block 的 fence 里，已天然可搜。
- **thinking**：`ThinkingBlock` 默认展开（`useState(true)`），可搜可高亮；用户手动折叠后同工具卡逻辑（定位到卡片不展开）。

### 7.3 大会话性能预算（核心深度问题 6）

| 操作 | 成本估算 | 依据/预算 |
|---|---|---|
| 索引构建（首开+query 变更） | 数千 block ≈ 1-3ms（纯字符串拼接 + join），防抖 150ms 合并击键 | 每条消息提取 = 遍历 blocks 拼串；无正则无 DOM。对比：一次 markdown 渲染 27ms（F8） |
| 搜索（每次防抖后） | `indexOf` 全索引一遍 ≈ 1ms 级 | 线性扫，`String.prototype.indexOf` |
| painter 重扫（跳转/输入后） | 只扫 visible 窗口（≤ headKeep 条消息的 DOM），80-150 条消息 ≈ 5-15ms | TreeWalker 遍历 textNode；比 (b) 方案便宜因为没有 DOM 写 |
| 向上跳转扩窗 | 一次性 mount 数百条（revealUpTo 语义），单次百 ms 级 | 与 ⌘K 锚点同路径（F3/F4），已被窗口化治理接受 |
| 常驻成本（打开但不打字） | 0（无定时器、无 observer；paint 仅在 hits/activeIdx 变化时跑） | — |

上限保护：命中数 > 2000 时 painter 只对「当前项 ±20 条命中所在消息」建 Range（计数仍全量），输入防抖保持 150ms。5000 条消息 × 平均 8 block 的会话（≈ Ginno 长会话实测规模，见窗口化注释的 60 消息/834 blocks 比例外推）全流程 < 50ms。

### 7.4 其它

- **空会话/HomeView**：`session === null` 时 ⌘F no-op（`useSessionFind` 的 open 监听里门控）。
- **只读视图（subagent/delegation/channel 会话）**：搜索纯读操作，照常可用（这些视图也走 TranscriptList，F4 管线自动生效）。
- **转录为空但有 runs（LiveRunBlock）**：runs 不在 `messages` 里（`ChatStream.tsx:934-945`，独立渲染）——P0 不搜 runs 内容，归 P2。
- **卸载清理**：浮层关闭、ChatStream 隐藏（路由切换，F13 组件不卸载）——`hidden` 期间 painter 结果不可见无害；组件真卸载（应用退出）随 DOM 消亡。
- **双开边界**：仅桌面单窗口 + pin 窗口；pin 无 ChatStream 不响应（§6.1），无跨窗口状态。

## 8. 实施拆分与改动文件清单

### M1（P0）：索引 + 浮层 + 跳转 + 高亮 —— 端到端可用

| 文件 | 动作 | 内容 |
|---|---|---|
| `apps/web/src/lib/sessionFind.ts` | 新增 | `extractSearchableText`、`buildFindIndex`、`searchFindIndex`（纯函数，node 环境可测） |
| `apps/web/src/lib/sessionFind.test.ts` | 新增 | vitest：提取表（各 kind）、大小写不敏感、多命中排序、空串/纯空白、流式标记 |
| `apps/web/src/components/chat/stream/useSessionFind.ts` | 新增 | hook：open/query/hits/activeIdx、`ginno:find-open` 监听、session reset、防抖、跳转 dispatch |
| `apps/web/src/components/chat/stream/FindBar.tsx` | 新增 | 浮层 UI（§3.3） |
| `apps/web/src/components/chat/stream/findHighlight.ts` | 新增 | SUPPORTED 检测、`paint`/`clear`、`locateRanges` |
| `apps/web/src/components/chat/ChatStream.tsx` | 修改 | 挂 `useSessionFind` + `<FindBar/>`（转录容器内，~15 行）；transcript ref 传给 painter |
| `apps/web/src/components/shell/AppShell.tsx` | 修改 | ⌘F 分支 + searchOpen 门控（~6 行，F7 effect 内） |
| `apps/web/src/app/globals.css` | 修改 | 两条 `::highlight()` 规则 |
| `apps/web/messages/zh-CN/chat.json`、`apps/web/messages/en/chat.json` | 修改 | `chat.find.*`：placeholder/无命中/上一个/下一个/关闭 aria |

### M2（P1）：精度与范围补全

| 文件 | 动作 | 内容 |
|---|---|---|
| `apps/web/src/components/chat/blocks.tsx` | 修改 | `InnerBlocks`/`UserBlocks` 循环处加 `data-block-idx`（findBar context 开关门控，§5.4） |
| `apps/web/src/lib/sessionFind.ts` | 修改 | 扩提取表：卡片类 block（subagent_result 等） |
| `apps/web/src/components/chat/blocks/toolBlocks.tsx` | 修改 | 折叠态工具卡「内含 N 处命中」徽标（可选） |

### M3（P2）：增强（见 §9）

### 验证（实现阶段的验收口径，非本文档任务）

- `pnpm exec vitest run src/lib/sessionFind.test.ts`（node 环境纯函数）
- `pnpm exec tsc --noEmit` + `pnpm exec next lint`
- 手动 checklist：长会话（>80 条）搜窗口外早期词 → 计数含未渲染命中 → Enter 跳转自动扩窗定位；流式中搜正在生成的词；工具卡折叠命中；Esc 后无残留高亮；⌘K 与 ⌘F 互不打架；`pnpm dev` Chrome 下 Ctrl+F 被接管。

## 9. P2 可选增强

1. **大小写敏感开关 / 全字匹配**（Aa、`"` 包裹词组——照 Chrome find 栏惯例）。
2. **命中列表下拉**：浮层下方列出命中消息摘要（复用 `MessageHitRow`/`highlightSnippet`，F5），点击直接跳。
3. **PinStream 支持**：pin 窗口同款 FindBar（PinStream 有自己的 transcript 渲染，需独立接线）。
4. **LiveRunBlock 内容纳入索引**（runs 独立于 messages，§7.4）。
5. **搜索词联动 ⌘K**：find bar 无命中时提示「跨会话搜索 "xxx"」一键转 ⌘K modal 并预填 query。
6. **高亮持久到滚动**：painter 目前只扫 visible——滚动进入新区域时重扫（`scrollRef` 的 onScroll 节流 200ms 触发增量 paint）。P0 不做的原因：索引计数已覆盖窗口外，「跳过去才亮」符合 find 语义；滚动常驻重扫是锦上添花。
