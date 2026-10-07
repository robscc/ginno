"use client";

/**
 * Claude Code Mods band 树渲染器(claude-code-mods-design.md §7.1)。
 *
 * broker 的 SurfaceTable 把 mod 的 ui.render 结果序列化成 JSON 树
 * (SerializedNode),经 Python WS 以 `mod.bands {generation, tree}` 帧推到前端。
 * 这里递归把它落成 DOM:Box→div、Text→span、Button→禁用样式的按钮(press 回发)。
 *
 * 序列化树是不可信数据(mod 产出):渲染全程白名单取 props,未知类型只画占位
 * `∅`,字符串子节点超长截断——绝不让 mod 的 props 直接决定任意样式。
 */

export interface SerializedNode {
  type: string;
  props?: Record<string, unknown>;
  children?: (SerializedNode | string)[];
  /** Button 专有:broker 分配的动作 id,press 帧回传。 */
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
  onPress: (actionId: string) => void;
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
    default:
      // 未知元素类型:占位,不猜测渲染。
      return <span className="font-mono text-faint" title={node.type}>∅</span>;
  }
}

export { ModNodeView };
