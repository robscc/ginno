"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import {
  AlertTriangle,
  Check,
  Code2,
  Copy,
  Download,
  ImageDown,
  Maximize2,
} from "lucide-react";
import { readMermaidPref, subscribeMermaidPref } from "@/lib/mermaidPrefs";
import { MermaidPanZoom } from "./MermaidPanZoom";

/**
 * `` ```mermaid `` fenced block → rendered diagram.
 *
 * 设计约束（docs/design/mermaid-render-design.md）：
 *
 * 1. **流式期间绝不着手渲染**。Markdown 在 text.delta 上每个 token 重渲染一次，
 *    半截 mermaid 语法 100% 解析失败；边流边渲染的结果是每 token 一次解析 +
 *    一次重排 + 一串错误框。所以 streaming=true 时只出占位骨架，落定后渲染一次。
 * 2. **渲染串行**。mermaid 内部有模块级状态（临时 element、配置），并发
 *    `render()` 会把两张图串成一串（同一条回答里两张图时必现）。所有渲染走
 *    模块级 promise 队列。
 * 3. **主题跟随 token**。图表配置从 CSS 变量读真实颜色，`html.light` 类变化时
 *    重新 initialize + 重渲染，避免深色气泡里贴一张白底图。
 * 4. **永远可降级**。语法错/开关关/加载失败 → 原生源码代码块，用户永远拿得到
 *    原文，这是保底体验。
 *
 * 渲染结果为 mermaid 在 `securityLevel: "strict"` 下的产物（内部已过 DOMPurify，
 * 且我们关掉了 htmlLabels 与 click 回调），因此可以用 dangerouslySetInnerHTML。
 */

type MermaidApi = typeof import("mermaid")["default"];

// ---------------------------------------------------------------- 模块级状态

/** 动态 chunk：主 bundle 零增量，只有真的出现图才加载 mermaid。 */
let apiPromise: Promise<MermaidApi> | null = null;
function loadMermaid(): Promise<MermaidApi> {
  apiPromise ??= import("mermaid").then((m) => m.default);
  return apiPromise;
}

/** 渲染队列：mermaid 不可重入（见文件头第 2 条）。 */
let queue: Promise<unknown> = Promise.resolve();
function enqueue<T>(job: () => Promise<T>): Promise<T> {
  const run = queue.then(job, job);
  // 队列本身不能被某个失败的任务毒化
  queue = run.catch(() => undefined);
  return run;
}

/** 已 initialize 过的主题；主题切换时置回 null 强制重新配置。 */
let initedTheme: "dark" | "light" | null = null;
/** 每次 render 需要一个唯一 id（mermaid 会用它建临时 element）。 */
let renderSeq = 0;

/** svg 文本缓存（LRU，简单实现）：滚动回看长历史不必重算。 */
const CACHE_MAX = 60;
const svgCache = new Map<string, string>();

function cacheGet(key: string): string | undefined {
  const hit = svgCache.get(key);
  if (hit !== undefined) {
    // touch：Map 的插入序即 LRU 序
    svgCache.delete(key);
    svgCache.set(key, hit);
  }
  return hit;
}

function cachePut(key: string, svg: string): void {
  svgCache.set(key, svg);
  while (svgCache.size > CACHE_MAX) {
    const oldest = svgCache.keys().next().value;
    if (oldest === undefined) break;
    svgCache.delete(oldest);
  }
}

// ---------------------------------------------------------------- 主题

/** 读 CSS 变量并转成 mermaid 能吃的颜色串（token 是 "r g b" 三元组）。 */
function cssColor(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  if (!raw) return fallback;
  const parts = raw.split(/[\s,]+/).filter(Boolean);
  if (parts.length === 3 && parts.every((p) => Number.isFinite(Number(p)))) {
    return `rgb(${parts.join(", ")})`;
  }
  return raw;
}

function isDarkTheme(): boolean {
  return !document.documentElement.classList.contains("light");
}

/** 主题变量：语义色全部取自 globals.css 的 token，视觉上与 chart 卡同族。 */
function themeVariables(dark: boolean): Record<string, unknown> {
  const base = cssColor("--base", dark ? "#0a0a0f" : "#ffffff");
  const panel = cssColor("--panel", dark ? "#0e0e15" : "#f6f6f8");
  const card = cssColor("--card", dark ? "#15151d" : "#ffffff");
  const card2 = cssColor("--card2", dark ? "#1b1b25" : "#f1f1f4");
  const line = cssColor("--line", dark ? "#262632" : "#e2e2e8");
  const line2 = cssColor("--line2", dark ? "#34343f" : "#cfcfd8");
  const txt = cssColor("--txt", dark ? "#e9e9f0" : "#1b1b22");
  const muted = cssColor("--muted", dark ? "#9a9aa6" : "#5c5c68");
  const violet = "#8b5cf6";
  return {
    darkMode: dark,
    background: "transparent",
    fontFamily: "inherit",
    fontSize: "13px",
    // flowchart / 通用
    primaryColor: card2,
    primaryTextColor: txt,
    primaryBorderColor: line2,
    secondaryColor: card,
    secondaryTextColor: txt,
    secondaryBorderColor: line,
    tertiaryColor: panel,
    tertiaryTextColor: txt,
    tertiaryBorderColor: line,
    mainBkg: card2,
    nodeBorder: line2,
    nodeTextColor: txt,
    lineColor: line2,
    textColor: txt,
    titleColor: txt,
    edgeLabelBackground: base,
    clusterBkg: "transparent",
    clusterBorder: line,
    // sequence
    actorBkg: card2,
    actorBorder: line2,
    actorTextColor: txt,
    actorLineColor: line2,
    signalColor: muted,
    signalTextColor: muted,
    labelBoxBkgColor: card2,
    labelBoxBorderColor: line2,
    labelTextColor: txt,
    loopTextColor: muted,
    noteBkgColor: card,
    noteTextColor: muted,
    noteBorderColor: line,
    activationBkgColor: card2,
    activationBorderColor: line2,
    sequenceNumberColor: base,
    // state / class
    labelColor: txt,
    altBackground: card,
    classText: txt,
    // er
    attributeBackgroundColorOdd: card2,
    attributeBackgroundColorEven: card,
    // gantt
    sectionBkgColor: card2,
    altSectionBkgColor: "transparent",
    sectionBkgColor2: card,
    taskBkgColor: card2,
    taskBorderColor: line2,
    taskTextColor: txt,
    taskTextOutsideColor: muted,
    taskTextClickableColor: txt,
    activeTaskBkgColor: card2,
    activeTaskBorderColor: violet,
    gridColor: line,
    doneTaskBkgColor: card,
    doneTaskBorderColor: line2,
    critBkgColor: "rgba(244, 63, 94, 0.25)",
    critBorderColor: "#f43f5e",
    todayLineColor: violet,
    // pie
    pieTitleTextColor: txt,
    pieSectionTextColor: txt,
    pieLegendTextColor: muted,
    pieStrokeColor: base,
  };
}

async function ensureInit(mermaid: MermaidApi, theme: "dark" | "light"): Promise<void> {
  if (initedTheme === theme) return;
  const dark = theme === "dark";
  const cfg = {
    startOnLoad: false,
    // strict：不出 HTML label、不执行 click 回调、结果过 DOMPurify
    securityLevel: "strict" as const,
    // 解析失败时不要 v11 默认往 DOM 里塞的红色错误图
    suppressErrorRendering: true,
    theme: dark ? "dark" : "default",
    themeVariables: themeVariables(dark),
    fontFamily: "inherit",
    // htmlLabels 必须放**根级**：v11 起图级 `flowchart.htmlLabels` 已废弃且被根级
    // 覆盖（config.type.d.ts）。关掉它 = 标签走 SVG <text> 而不是 foreignObject，
    // WKWebView 对 foreignObject 的裁剪/字体测量不可靠，纯 text 也不受容器缩放影响。
    htmlLabels: false,
    flowchart: { useMaxWidth: true, curve: "basis" },
    sequence: { useMaxWidth: true, wrap: true, diagramMarginX: 12, diagramMarginY: 12 },
    gantt: { useMaxWidth: true },
    er: { useMaxWidth: true },
    class: { useMaxWidth: true },
    state: { useMaxWidth: true },
    pie: { useMaxWidth: true },
    mindmap: { useMaxWidth: true },
    timeline: { useMaxWidth: true },
    logLevel: "fatal" as const,
  };
  mermaid.initialize(cfg as Parameters<MermaidApi["initialize"]>[0]);
  initedTheme = theme;
}

/**
 * mermaid 默认给 svg 加 `width:100%` + `max-width:<自然宽>px`：容器比图窄时整张图
 * 等比缩小，文字跟着变小——735px 气泡里放一张 1500px 的流程图，字号会被压到 6px。
 * 这里把尺寸钉成自然大小，容器装不下就让外层横向滚动（与代码块同一策略），
 * 想整体看全屏有「放大查看」。
 */
function pinNaturalSize(svgText: string): string {
  const holder = document.createElement("div");
  holder.innerHTML = svgText;
  const el = holder.querySelector("svg");
  if (!el) return svgText;
  const { w, h } = sizeOf(el);
  el.setAttribute("width", String(Math.round(w)));
  el.setAttribute("height", String(Math.round(h)));
  el.style.removeProperty("max-width");
  el.style.removeProperty("width");
  return holder.innerHTML;
}

async function renderSvg(source: string, theme: "dark" | "light"): Promise<string> {
  const key = `${theme}\u0000${source}`;
  const hit = cacheGet(key);
  if (hit !== undefined) return hit;
  const mermaid = await loadMermaid();
  return enqueue(async () => {
    await ensureInit(mermaid, theme);
    // id 只用于 mermaid 建临时容器，必须唯一且是合法 id 片段
    const { svg } = await mermaid.render(`ginno-mmd-${++renderSeq}`, source);
    const pinned = pinNaturalSize(svg);
    cachePut(key, pinned);
    return pinned;
  });
}

// ---------------------------------------------------------------- 主题订阅
// 一份 observer 服务所有已挂载的图（每个图自带 observer 会在长会话里堆几十个）。

const themeSubs = new Set<() => void>();
let themeObserver: MutationObserver | null = null;

function ensureThemeObserver(): void {
  if (themeObserver || typeof MutationObserver === "undefined") return;
  themeObserver = new MutationObserver(() => {
    for (const fn of Array.from(themeSubs)) fn();
  });
  themeObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["class"],
  });
}

function useThemeTick(): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    ensureThemeObserver();
    const fn = () => setTick((n) => n + 1);
    themeSubs.add(fn);
    return () => {
      themeSubs.delete(fn);
    };
  }, []);
  return tick;
}

// ---------------------------------------------------------------- 导出工具

/** 从 svg 元素解析尺寸：优先 viewBox，其次 width/height 属性。 */
function sizeOf(el: SVGSVGElement): { w: number; h: number } {
  const vb = (el.getAttribute("viewBox") || "")
    .split(/[\s,]+/)
    .filter(Boolean)
    .map(Number);
  const num = (v: string | null) => {
    const n = parseFloat(v || "0");
    return Number.isFinite(n) && n > 0 ? n : 0;
  };
  let w = vb.length === 4 && vb[2] > 0 ? vb[2] : num(el.getAttribute("width"));
  let h = vb.length === 4 && vb[3] > 0 ? vb[3] : num(el.getAttribute("height"));
  if (!w) w = 800;
  if (!h) h = 600;
  return { w, h };
}

function svgSize(svgText: string): { w: number; h: number } {
  const holder = document.createElement("div");
  holder.innerHTML = svgText;
  const el = holder.querySelector("svg");
  return el ? sizeOf(el) : { w: 800, h: 600 };
}

/** svg → png blob。背景取 --card，避免"透明底粘到白文档里全白看不见"。 */
async function svgToPng(svgText: string, scale = 2): Promise<Blob> {
  const { w, h } = svgSize(svgText);
  const holder = document.createElement("div");
  holder.innerHTML = svgText;
  const el = holder.querySelector("svg")!;
  el.setAttribute("width", String(w));
  el.setAttribute("height", String(h));
  el.removeAttribute("style"); // 去掉 max-width，避免导出被裁
  const data = new XMLSerializer().serializeToString(el);
  const url = URL.createObjectURL(new Blob([data], { type: "image/svg+xml;charset=utf-8" }));
  try {
    const img = new Image();
    img.src = url;
    // decode() 比 onload 更稳：确保像素已可绘制
    await img.decode();
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(w * scale);
    canvas.height = Math.round(h * scale);
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("canvas unavailable");
    ctx.scale(scale, scale);
    ctx.fillStyle = cssColor("--card", "#15151d");
    ctx.fillRect(0, 0, w, h);
    ctx.drawImage(img, 0, 0, w, h);
    return await new Promise<Blob>((resolve, reject) => {
      canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("toBlob failed"))), "image/png");
    });
  } finally {
    URL.revokeObjectURL(url);
  }
}

function downloadBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

function firstLine(err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err ?? "");
  const line = msg.split("\n").map((s) => s.trim()).filter(Boolean)[0] || "unknown error";
  return line.length > 160 ? `${line.slice(0, 157)}…` : line;
}

// ---------------------------------------------------------------- 组件

type Status = "idle" | "loading" | "done" | "error";

/** messages/<locale>/composer.json → composer.markdown.mermaid.<key> 的 key 集合。 */
type MermaidKey =
  | "generating"
  | "renderFailed"
  | "empty"
  | "showSource"
  | "hideSource"
  | "copySource"
  | "copyPng"
  | "downloadSvg"
  | "expand"
  | "diagramAlt"
  | "zoomIn"
  | "zoomOut"
  | "resetZoom"
  | "closeDialog";

/** 源码代码块：开关关闭 / 渲染失败 / 用户展开源码时共用。 */
function SourceBox({ code, dense }: { code: string; dense?: boolean }) {
  return (
    <pre
      className={`overflow-x-auto bg-[rgb(var(--code-bg))] px-3 leading-relaxed text-muted [overflow-wrap:normal] ${
        dense ? "max-h-72 py-2 text-[11px]" : "py-3 text-xs"
      }`}
    >
      <code>{code}</code>
    </pre>
  );
}

export function MermaidBlock({ source, streaming }: { source: string; streaming?: boolean }) {
  const t = useTranslations("composer");
  const rawId = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const code = source.replace(/\s+$/, "");

  const [enabled, setEnabled] = useState(true);
  const [inView, setInView] = useState(false);
  const [status, setStatus] = useState<Status>("idle");
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showSource, setShowSource] = useState(false);
  const [zoomOpen, setZoomOpen] = useState(false);
  const [flash, setFlash] = useState<"src" | "png" | null>(null);
  const boxRef = useRef<HTMLDivElement | null>(null);
  const themeTick = useThemeTick();

  // 开关：挂载即同步一次，之后跟设置页联动
  useEffect(() => {
    const sync = () => setEnabled(readMermaidPref());
    sync();
    return subscribeMermaidPref(sync);
  }, []);

  // 离屏不渲染：长会话里几十张图不该在挂载时一起算
  useEffect(() => {
    if (inView) return;
    const el = boxRef.current;
    if (!el) return;
    if (typeof IntersectionObserver === "undefined") {
      setInView(true);
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setInView(true);
          io.disconnect();
        }
      },
      { rootMargin: "400px 0px" },
    );
    io.observe(el);
    // 兜底：窗口不可见（回答在后台跑完）时浏览器会限流甚至不投递 IO 回调，
    // 不能因此让图永远空白。可见时保持懒加载语义，只在后台时放行一次。
    const t = document.hidden ? setTimeout(() => setInView(true), 800) : null;
    return () => {
      io.disconnect();
      if (t !== null) clearTimeout(t);
    };
  }, [inView]);

  // 渲染：仅在「落定 + 可见 + 开关开 + 有内容」时跑一次
  useEffect(() => {
    if (!enabled || streaming || !inView || !code) return;
    let alive = true;
    setStatus("loading");
    setError(null);
    renderSvg(code, isDarkTheme() ? "dark" : "light")
      .then((out) => {
        if (!alive) return;
        setSvg(out);
        setStatus("done");
      })
      .catch((err) => {
        if (!alive) return;
        // 失败不保留旧图：深色主题上贴一张浅色 svg 比直接露出源码更糟
        setSvg(null);
        setError(firstLine(err));
        setStatus("error");
      });
    return () => {
      alive = false;
    };
  }, [code, enabled, streaming, inView, themeTick]);

  const copyText = useCallback(
    async (text: string, kind: "src") => {
      try {
        await navigator.clipboard.writeText(text);
        setFlash(kind);
        setTimeout(() => setFlash(null), 1400);
      } catch {
        /* clipboard unavailable */
      }
    },
    [],
  );

  const copyPng = useCallback(async () => {
    if (!svg) return;
    try {
      const blob = await svgToPng(svg);
      await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
      setFlash("png");
      setTimeout(() => setFlash(null), 1400);
    } catch {
      // WKWebView 少数场景拿不到 ClipboardItem：退化成下载，用户仍能拿到图
      try {
        downloadBlob(await svgToPng(svg), "diagram.png");
      } catch {
        /* ignore */
      }
    }
  }, [svg]);

  const downloadSvg = useCallback(() => {
    if (!svg) return;
    downloadBlob(new Blob([svg], { type: "image/svg+xml;charset=utf-8" }), "diagram.svg");
  }, [svg]);

  // next-intl 的 key 是字面量联合类型，动态拼接必须显式断言（key 集合由
  // messages/*/composer.json 的 markdown.mermaid.* 保证，见下方 MermaidKey）
  const tm = (k: MermaidKey) => t(`markdown.mermaid.${k}` as Parameters<typeof t>[0]);

  // 开关关闭 / 空内容：直接给源码，不再显示 chrome 上的按钮（没有图可操作）
  if (!enabled || !code) {
    return (
      <div className="my-3 overflow-hidden rounded-lg border border-line">
        <div className="flex items-center justify-between border-b border-line bg-card2/60 px-3 py-1.5">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-faint">
            mermaid
          </span>
          <button
            onClick={() => void copyText(code, "src")}
            className="text-[11px] text-faint transition-colors hover:text-txt"
          >
            {flash === "src" ? t("markdown.copied") : t("markdown.copy")}
          </button>
        </div>
        <SourceBox code={code || tm("empty")} />
      </div>
    );
  }

  const busy = streaming || status === "loading";
  const failed = status === "error";

  return (
    <div
      ref={boxRef}
      className="group/mermaid my-3 overflow-hidden rounded-lg border border-line bg-base/50"
    >
      {/* chrome：左侧标签 + 右侧工具条（hover 才亮，避免正文里一堆图标） */}
      <div className="flex items-center justify-between gap-2 border-b border-line bg-card2/60 px-3 py-1.5">
        <span className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-faint">
          <Code2 className="h-3 w-3 text-violet" aria-hidden />
          mermaid
          {busy && <span className="normal-case tracking-normal">{tm("generating")}</span>}
          {failed && (
            <span className="normal-case tracking-normal text-[rgb(var(--st-error))]">
              {tm("renderFailed")}
            </span>
          )}
        </span>
        <div className="flex items-center gap-1 opacity-0 transition-opacity group-hover/mermaid:opacity-100 focus-within:opacity-100">
          {svg && (
            <>
              <ToolButton label={tm("copyPng")} onClick={() => void copyPng()}>
                {flash === "png" ? (
                  <Check className="h-3.5 w-3.5 text-[rgb(var(--st-ok))]" aria-hidden />
                ) : (
                  <ImageDown className="h-3.5 w-3.5" aria-hidden />
                )}
              </ToolButton>
              <ToolButton label={tm("downloadSvg")} onClick={downloadSvg}>
                <Download className="h-3.5 w-3.5" aria-hidden />
              </ToolButton>
              <ToolButton label={tm("expand")} onClick={() => setZoomOpen(true)}>
                <Maximize2 className="h-3.5 w-3.5" aria-hidden />
              </ToolButton>
            </>
          )}
          <ToolButton
            label={showSource ? tm("hideSource") : tm("showSource")}
            onClick={() => setShowSource((v) => !v)}
            active={showSource}
          >
            <Code2 className="h-3.5 w-3.5" aria-hidden />
          </ToolButton>
          <ToolButton label={tm("copySource")} onClick={() => void copyText(code, "src")}>
            {flash === "src" ? (
              <Check className="h-3.5 w-3.5 text-[rgb(var(--st-ok))]" aria-hidden />
            ) : (
              <Copy className="h-3.5 w-3.5" aria-hidden />
            )}
          </ToolButton>
        </div>
      </div>

      {/* 图 / 骨架 / 错误 */}
      {svg && !failed && (
        <div
          // securityLevel:"strict" + htmlLabels:false 的产物，见文件头
          dangerouslySetInnerHTML={{ __html: svg }}
          className="selectable cursor-zoom-in overflow-x-auto px-3 py-3 [&_svg]:mx-auto [&_svg]:h-auto [&_svg]:max-w-none [&_svg]:bg-transparent"
          onClick={() => setZoomOpen(true)}
          role="img"
          aria-label={tm("diagramAlt")}
        />
      )}

      {busy && !svg && (
        <div className="space-y-2 px-3 py-5" aria-busy="true" aria-label={tm("generating")}>
          <div className="h-2.5 w-1/3 animate-pulse rounded bg-card2" />
          <div className="h-2.5 w-2/3 animate-pulse rounded bg-card2" />
          <div className="h-2.5 w-1/2 animate-pulse rounded bg-card2" />
        </div>
      )}

      {failed && (
        <div className="flex items-start gap-2 px-3 py-2 text-xs text-muted">
          <AlertTriangle
            className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[rgb(var(--st-missed))]"
            aria-hidden
          />
          <span className="min-w-0">
            {tm("renderFailed")}
            <span className="ml-1 break-words text-faint">{error}</span>
          </span>
        </div>
      )}

      {(failed || showSource) && <SourceBox code={code} dense />}

      {zoomOpen && svg && (
        <MermaidPanZoom
          svg={svg}
          labels={{
            dialog: tm("expand"),
            zoomIn: tm("zoomIn"),
            zoomOut: tm("zoomOut"),
            resetZoom: tm("resetZoom"),
            close: tm("closeDialog"),
          }}
          onClose={() => setZoomOpen(false)}
        />
      )}
    </div>
  );
}

function ToolButton({
  label,
  onClick,
  children,
  active,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
  active?: boolean;
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      onClick={onClick}
      className={`rounded p-1 transition-colors hover:text-txt ${
        active ? "text-txt" : "text-faint"
      }`}
    >
      {children}
    </button>
  );
}
