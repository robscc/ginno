"use client";

/**
 * Claude Code Mods band/pane 树渲染器(claude-code-mods-design.md §7.1/§7.4)。
 *
 * broker 的 SurfaceTable 把 mod 的 ui.render 结果序列化成 JSON 树
 * (SerializedNode),经 Python WS 以 `mod.bands {generation, tree}` /
 * `mod.pane.update {id, generation, tree}` 帧推到前端。这里递归把它落成 DOM:
 * Box→div、Text→span、Button/Input/Select→可交互元素(press 回发)、
 * Markdown/Code→只读文本。
 *
 * 序列化树是不可信数据(mod 产出):渲染全程白名单取 props,未知类型只画占位
 * `∅`,字符串子节点超长截断——绝不让 mod 的 props 直接决定任意样式。
 */

import { useState } from "react";
import { Markdown } from "@/components/chat/Markdown";

export interface SerializedNode {
  type: string;
  props?: Record<string, unknown>;
  children?: (SerializedNode | string)[];
  /** Button/Input/Select 专有:broker 分配的动作 id,press 帧回传。 */
  actionId?: string;
}

/** 一次 band 快照(WS `mod.bands` 帧的载荷)。 */
export interface ModBandSnapshot {
  generation: number;
  tree: (SerializedNode | string)[];
  /** 来源 mod 名(可选,帧里带了才显示标签)。 */
  mod?: string;
}

/** Text 单字符串子节点超过这个长度截断(防止 mod 刷屏)。 */
const MAX_TEXT_CHARS = 10000;

// 终端色名 → 主题色。暗色主题下 black/white 这类极端色映射到主题的中性灰,
// 避免 mod 用 "black" 把文字画进背景里。
const TERMINAL_COLORS: Record<string, string> = {
  black: "#9a9aa6",
  red: "#ef4444",
  green: "#22c55e",
  yellow: "#eab308",
  blue: "#3b82f6",
  magenta: "#d946ef",
  cyan: "#22d3ee",
  white: "#f4f4f5",
  gray: "#9a9aa6",
  grey: "#9a9aa6",
  graydim: "#6b7280",
  brightred: "#f87171",
  brightgreen: "#4ade80",
  brightyellow: "#facc15",
  brightblue: "#60a5fa",
  brightmagenta: "#e879f9",
  brightcyan: "#67e8f9",
  brightwhite: "#ffffff",
};

function terminalColor(v: unknown): string | undefined {
  if (typeof v !== "string") return undefined;
  return TERMINAL_COLORS[v.trim().toLowerCase()];
}

function num(v: unknown): number | undefined {
  const n = typeof v === "string" ? Number(v) : typeof v === "number" ? v : NaN;
  return Number.isFinite(n) ? n : undefined;
}

function bool(v: unknown): boolean {
  return v === true || v === "true";
}

function truncate(s: string): string {
  return s.length > MAX_TEXT_CHARS ? `${s.slice(0, MAX_TEXT_CHARS)}…` : s;
}

/** Box 的 props 白名单映射 → style。未知 key 一律忽略。 */
function boxStyle(props: Record<string, unknown>): React.CSSProperties {
  const style: React.CSSProperties = {};
  const dir = props.flexDirection;
  if (dir === "row" || dir === "column") style.flexDirection = dir;
  const pad = num(props.padding);
  const px = num(props.paddingX) ?? pad;
  const py = num(props.paddingY) ?? pad;
  if (px !== undefined) {
    style.paddingLeft = px;
    style.paddingRight = px;
  }
  if (py !== undefined) {
    style.paddingTop = py;
    style.paddingBottom = py;
  }
  const gap = num(props.gap);
  if (gap !== undefined) style.gap = gap;
  if (bool(props.border)) {
    style.border = "1px solid rgb(var(--line) / 1)";
    style.borderRadius = 6;
  }
  return style;
}

function ModNodeView({
  node,
  onPress,
}: {
  node: SerializedNode | string;
  /** value 仅 Input 提交 / Select 选中时携带(broker 侧转交 runner 的回调)。 */
  onPress: (actionId: string, value?: string) => void;
}) {
  if (typeof node === "string") return <span className="whitespace-pre-wrap">{truncate(node)}</span>;
  const props = (node.props ?? {}) as Record<string, unknown>;
  const children = node.children ?? [];

  switch (node.type) {
    case "Box":
      return (
        <div className="flex" style={boxStyle(props)}>
          {children.map((c, i) => (
            <ModNodeView key={i} node={c} onPress={onPress} />
          ))}
        </div>
      );
    case "Text": {
      const color = terminalColor(props.color);
      const cls = [
        "whitespace-pre-wrap",
        bool(props.bold) ? "font-semibold" : "",
        bool(props.italic) ? "italic" : "",
        bool(props.underline) ? "underline" : "",
        bool(props.dimColor) ? "opacity-60" : "",
      ]
        .filter(Boolean)
        .join(" ");
      return (
        <span className={cls} style={color ? { color } : undefined}>
          {children.map((c, i) =>
            typeof c === "string" ? (
              <span key={i}>{truncate(c)}</span>
            ) : (
              <ModNodeView key={i} node={c} onPress={onPress} />
            ),
          )}
        </span>
      );
    }
    case "Button": {
      const label =
        typeof props.label === "string"
          ? props.label
          : children.filter((c) => typeof c === "string").join("");
      const hotkey = typeof props.hotkey === "string" ? props.hotkey : "";
      // 禁用样式但保留点击:press 帧回发,后端/broker 未就绪时由调用方静默。
      // 真正的可用性由视觉传达(半透明),不靠 pointer-events 拦截。
      return (
        <button
          onClick={() => node.actionId && onPress(node.actionId)}
          className="flex items-center gap-1.5 rounded-md border border-line bg-card2/60 px-2 py-0.5 text-xs text-muted opacity-60 transition-colors hover:opacity-80"
        >
          <span>{truncate(label)}</span>
          {hotkey && <kbd className="rounded border border-line px-1 font-mono text-[10px] text-faint">{hotkey}</kbd>}
        </button>
      );
    }
    case "Input":
      return <ModInputNode node={node} props={props} onPress={onPress} />;
    case "Select":
      return <ModSelectNode node={node} props={props} onPress={onPress} />;
    case "Markdown": {
      // 子节点就是 markdown 源文本;复用 chat 的渲染器(白名单主题样式)。
      const src = children.filter((c) => typeof c === "string").join("");
      return (
        <div className="min-w-0 text-sm text-txt">
          <Markdown text={truncate(src)} />
        </div>
      );
    }
    case "Code": {
      // 轻量呈现:不做语法高亮(Monaco 实例对一个 pane 片段太重),等宽
      // pre + 横向滚动即可;超长截断同 Text。
      const src = children.filter((c) => typeof c === "string").join("");
      const lang = typeof props.language === "string" ? props.language : "";
      return (
        <pre className="max-h-64 overflow-auto rounded-md border border-line bg-base p-2 font-mono text-xs leading-relaxed text-txt">
          {lang && <div className="mb-1 text-[10px] uppercase tracking-wide text-faint">{lang}</div>}
          <code className="whitespace-pre">{truncate(src)}</code>
        </pre>
      );
    }
    default:
      // 未知元素类型:占位,不猜测渲染。
      return <span className="font-mono text-faint" title={node.type}>∅</span>;
  }
}

/** Input(pane 专有,§7.4):文本框,提交(Enter / 按钮)即 press,输入值随帧回发。 */
function ModInputNode({
  node,
  props,
  onPress,
}: {
  node: SerializedNode;
  props: Record<string, unknown>;
  onPress: (actionId: string, value?: string) => void;
}) {
  const [text, setText] = useState(typeof props.value === "string" ? props.value : "");
  const placeholder = typeof props.placeholder === "string" ? props.placeholder : "";
  const submitLabel = typeof props.submitLabel === "string" ? props.submitLabel : "↵";
  const multiline = bool(props.multiline);
  const disabled = !node.actionId;
  const submit = () => {
    if (node.actionId) onPress(node.actionId, text);
  };
  return (
    <div className="flex min-w-0 items-start gap-1.5">
      {multiline ? (
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={placeholder}
          rows={3}
          className="min-w-0 flex-1 rounded-md border border-line bg-card px-2 py-1 text-xs text-txt placeholder:text-faint"
        />
      ) : (
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) submit();
          }}
          placeholder={placeholder}
          className="min-w-0 flex-1 rounded-md border border-line bg-card px-2 py-1 text-xs text-txt placeholder:text-faint"
        />
      )}
      <button
        onClick={submit}
        disabled={disabled}
        className="shrink-0 rounded-md border border-line bg-card2/60 px-2 py-1 text-xs text-muted transition-colors hover:text-txt disabled:opacity-40"
      >
        {truncate(submitLabel)}
      </button>
    </div>
  );
}

/** Select(pane 专有):下拉,选中即 press,选项值随帧回发。 */
function ModSelectNode({
  node,
  props,
  onPress,
}: {
  node: SerializedNode;
  props: Record<string, unknown>;
  onPress: (actionId: string, value?: string) => void;
}) {
  // options 白名单:string 或 {label, value};其余丢弃。
  const options = (Array.isArray(props.options) ? props.options : [])
    .map((o: unknown) =>
      typeof o === "string"
        ? { label: o, value: o }
        : o && typeof o === "object"
          ? {
              label: String((o as { label?: unknown }).label ?? (o as { value?: unknown }).value ?? ""),
              value: String((o as { value?: unknown }).value ?? ""),
            }
          : null,
    )
    .filter((o): o is { label: string; value: string } => o !== null && o.label !== "");
  const disabled = !node.actionId || options.length === 0;
  return (
    <select
      value={typeof props.value === "string" ? props.value : ""}
      disabled={disabled}
      onChange={(e) => node.actionId && onPress(node.actionId, e.target.value)}
      className="min-w-0 rounded-md border border-line bg-card px-1.5 py-1 text-xs text-txt"
    >
      {(props.value === undefined || props.value === "") && <option value="">…</option>}
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {truncate(o.label)}
        </option>
      ))}
    </select>
  );
}

export { ModNodeView };
