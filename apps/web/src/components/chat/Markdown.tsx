"use client";

import {
  Children,
  isValidElement,
  memo,
  useCallback,
  useEffect,
  useState,
  type ReactElement,
  type ReactNode,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useTranslations } from "next-intl";
import { FileText } from "lucide-react";
import { openLinkExternal } from "@/lib/runtime";
import { splitFrontmatter, type FrontmatterEntry } from "@/lib/markdownFrontmatter";
import { parseMarkdownFileTarget, type MarkdownFileTarget } from "@/lib/codePaths";
import { MermaidBlock } from "./MermaidBlock";

// Obsidian-style [[target]] / [[target|alias]] / [[target#heading]]. Turned into
// in-page fragment links (#wl:<encoded>) so any URL transform keeps them; the
// `a` renderer below intercepts the prefix when `onWikilink` is provided.
const WIKILINK_RE = /\[\[([^\]|#]+?)(?:#[^\]|]*)?(?:\|([^\]]+?))?\]\]/g;

const PLAIN_LANGUAGES = ["mermaid", "plaintext", "text", "txt"];
type Highlight = ReturnType<typeof import("rehype-highlight").default>;

// All Markdown instances share one request and grammar registry per document.
// Cache a failed load too, so a broken chunk cannot trigger a request per message.
let highlightPromise: Promise<Highlight | undefined> | undefined;
function loadHighlight() {
  return (highlightPromise ??= import("rehype-highlight")
    .then(({ default: rehypeHighlight }) => {
      const highlight = rehypeHighlight({ plainText: PLAIN_LANGUAGES });
      const safelyHighlight: Highlight = (tree, file) => {
        try {
          highlight(tree, file);
        } catch {
          // A grammar error must leave the code and prose readable.
        }
      };
      return safelyHighlight;
    })
    .catch(() => undefined));
}

/** Recursively extract plain text from rendered children (for the copy button). */
function nodeText(n: ReactNode): string {
  if (n == null || typeof n === "boolean") return "";
  if (typeof n === "string" || typeof n === "number") return String(n);
  if (Array.isArray(n)) return n.map(nodeText).join("");
  if (isValidElement(n)) return nodeText((n.props as { children?: ReactNode }).children);
  return "";
}

/** Frontmatter 属性条（Obsidian properties 风格）：标量 `key: value`，
 *  tags 等行内数组渲染成 chip，多行列表只显示条数。 */
function FrontmatterBar({ entries }: { entries: FrontmatterEntry[] }) {
  const t = useTranslations("composer");
  return (
    <div className="my-3 rounded-lg border border-line bg-card2/40 px-3 py-2 text-[11px] leading-relaxed">
      {entries.map((e) =>
        e.items && e.items.length > 0 ? (
          <div key={e.key} className="flex flex-wrap items-center gap-1.5 py-0.5">
            <span className="text-faint">{e.key}:</span>
            {e.items.map((tag) => (
              <span
                key={tag}
                className="rounded-full bg-violet/10 px-2 py-px text-[10px] text-violet"
              >
                {tag}
              </span>
            ))}
          </div>
        ) : (
          <div key={e.key} className="py-0.5">
            <span className="text-faint">{e.key}: </span>
            <span className="text-muted">
              {e.list ? t("markdown.frontmatterItems", { count: e.value }) : e.value}
            </span>
          </div>
        ),
      )}
    </div>
  );
}

function CodeBlock({
  lang,
  children,
  streaming,
  onHighlightReady,
}: {
  lang?: string;
  children: ReactNode;
  streaming?: boolean;
  onHighlightReady?: (highlight: Highlight) => void;
}) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!lang || PLAIN_LANGUAGES.includes(lang) || !onHighlightReady) return;

    let cancelled = false;
    let cancelScheduled: (() => void) | undefined;
    void loadHighlight().then((highlight) => {
      if (!highlight || cancelled) return;
      const apply = () => {
        if (!cancelled) onHighlightReady(highlight);
      };
      if (streaming) {
        apply();
      } else if (typeof window.requestIdleCallback === "function") {
        const id = window.requestIdleCallback(apply, { timeout: 1000 });
        cancelScheduled = () => window.cancelIdleCallback(id);
      } else {
        const id = window.setTimeout(apply, 32);
        cancelScheduled = () => window.clearTimeout(id);
      }
    });
    return () => {
      cancelled = true;
      cancelScheduled?.();
    };
  }, [lang, onHighlightReady, streaming]);
  // composer 域 catalog（本组件也被其他域文件复用，文案统一收在 composer.markdown）
  const t = useTranslations("composer");
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(nodeText(children).replace(/\n$/, ""));
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable */
    }
  };
  return (
    <div className="my-3 overflow-hidden rounded-lg border border-line">
      <div className="flex items-center justify-between border-b border-line bg-card2/60 px-3 py-1.5">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-faint">
          {lang || t("markdown.codeLabel")}
        </span>
        <button
          onClick={copy}
          className="text-[11px] text-faint transition-colors hover:text-txt"
        >
          {copied ? t("markdown.copied") : t("markdown.copy")}
        </button>
      </div>
      {/* overflow-wrap:normal cancels the break-words inherited from the
          Markdown wrapper (see below): fenced code must scroll horizontally,
          never wrap mid-token. */}
      <pre className="overflow-x-auto bg-[rgb(var(--code-bg))] p-3 text-xs leading-relaxed [overflow-wrap:normal]">
        <code className={`hljs${lang ? ` language-${lang}` : ""}`}>{children}</code>
      </pre>
    </div>
  );
}

/**
 * Full-featured markdown renderer for assistant output.
 *
 * remark-gfm brings tables, task lists, strikethrough and autolinks;
 * rehype-highlight adds syntax highlighting (token palette lives in
 * globals.css so it follows the light/dark theme). Task lists render as
 * checkbox rows, visually distinct from plain bullet/numbered lists.
 */
// memo: markdown 解析 + highlight.js 是 transcript 里最贵的渲染（14KB 报告单条
// 数 ms）。composer 每敲一个键 ChatStream 都会整体重渲染，没有 memo 时全部历史
// 消息的 Markdown 每个键重跑一遍（实测 27ms/键）。text 不变即跳过；chat 路径
// onWikilink 恒为 undefined，浅比较成立。
export const Markdown = memo(function Markdown({
  text,
  onWikilink,
  streaming,
  onFileLink,
}: {
  text: string;
  onWikilink?: (target: string) => void;
  onFileLink?: (target: MarkdownFileTarget) => void;
  /** 本轮回答仍在生成。高亮就绪后立即用于流式文本，Mermaid 等文本落定再出图。 */
  streaming?: boolean;
}) {
  const t = useTranslations("composer");
  const [highlight, setHighlight] = useState<Highlight>();
  const onHighlightReady = useCallback((ready: Highlight) => {
    setHighlight(() => ready);
  }, []);
  // Let parsed code blocks request the plugin. Markdown syntax inside a literal
  // block, inline code, or prose never starts a load. Keep the renderer stable
  // as streaming text changes so pending subscriptions survive those updates.
  const renderPre = useCallback(({ children }: { children?: ReactNode }) => {
    const el = Children.toArray(children).find(isValidElement) as
      | ReactElement<{ className?: string; children?: ReactNode }>
      | undefined;
    const lang = /language-([\w+-]+)/.exec(el?.props?.className || "")?.[1];
    if (lang === "mermaid") {
      const source = nodeText(el?.props?.children).replace(/\n$/, "");
      return <MermaidBlock source={source} streaming={streaming} />;
    }
    return (
      <CodeBlock
        lang={lang}
        streaming={streaming}
        onHighlightReady={highlight ? undefined : onHighlightReady}
      >
        {el?.props?.children}
      </CodeBlock>
    );
  }, [highlight, onHighlightReady, streaming]);
  // Frontmatter（Obsidian 属性块）：剥离正文交给属性条渲染——预览窗/知识库
  // 页阅读视图里的 .md 原文以 `---\ntitle: …` 开头时，YAML 不再被当正文裸奔。
  const { body: fmBody, entries: fmEntries } = splitFrontmatter(text);
  const src = onWikilink
    ? fmBody.replace(WIKILINK_RE, (_, target: string, alias?: string) => {
        const label = (alias || target).trim();
        return `[${label}](#wl:${encodeURIComponent(target.trim())})`;
      })
    : fmBody;
  return (
    /* break-words (overflow-wrap: break-word) is inherited, so one class on
       this wrapper covers every prose node (p/li/td/blockquote/a/headings).
       Without it a long unbreakable token — slash-joined identifiers like
       （JobA/JobB/JobC）, bare paths, URLs — cannot wrap, overflows the chat
       bubble, and drags a horizontal scrollbar across the whole transcript
       (2026-10-14: turn 435558959885220985: one token laid out 1025px wide
       against a 735px column -> transcript scrollWidth 1066 vs 783).
       The CodeBlock <pre> re-asserts overflow-wrap:normal so fenced code
       keeps scrolling horizontally instead of wrapping mid-token. */
    <div className="selectable break-words">
      {fmEntries.length > 0 && <FrontmatterBar entries={fmEntries} />}
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={highlight ? [() => highlight] : []}
        components={{
          pre: renderPre,
          code({ children }) {
            // Inline code only — block code is handled by the pre override above.
            return (
              <code className="break-all rounded border border-line bg-card2/70 px-1 py-0.5 font-mono text-[0.85em] text-[rgb(var(--inline-code))]">
                {children}
              </code>
            );
          },
          p({ children }) {
            return <p className="my-2 leading-relaxed first:mt-0 last:mb-0">{children}</p>;
          },
          a({ href, children }) {
            if (onWikilink && href && href.startsWith("#wl:")) {
              const target = decodeURIComponent(href.slice(4));
              return (
                <button
                  type="button"
                  onClick={() => onWikilink(target)}
                  title={t("markdown.openLink", { target })}
                  className="rounded bg-violet/15 px-1 py-0.5 text-violet underline decoration-violet/40 underline-offset-2 transition-colors hover:bg-violet/25 hover:decoration-violet"
                >
                  {children}
                </button>
              );
            }
            const file = onFileLink ? parseMarkdownFileTarget(href) : null;
            if (file && onFileLink) {
              const location = file.line ? `${file.path}:${file.line}` : file.path;
              return (
                <button
                  type="button"
                  onClick={() => onFileLink(file)}
                  title={t("markdown.openFileLink", { target: location })}
                  className="inline rounded bg-card2/80 px-1 py-px text-left text-txt underline decoration-line2 underline-offset-2 transition-colors hover:text-violet hover:decoration-violet"
                >
                  <FileText aria-hidden className="mr-1 inline h-3 w-3 align-[-2px] text-muted" />
                  {children}
                </button>
              );
            }
            return (
              <a
                href={href}
                target="_blank"
                rel="noreferrer"
                onClick={(e) => {
                  // WKWebView ignores target=_blank — hand http(s) links to the
                  // sidecar, which opens the OS default browser (same route the
                  // SourcesBlock citations use). Non-http schemes (mailto: …)
                  // keep the default anchor behavior.
                  if (href && /^https?:\/\//i.test(href)) {
                    e.preventDefault();
                    openLinkExternal(href);
                  }
                }}
                className="text-violet underline decoration-violet/40 underline-offset-2 transition-colors hover:decoration-violet"
              >
                {children}
              </a>
            );
          },
          ul({ className, children }) {
            if (className?.includes("contains-task-list")) {
              return <ul className="my-2 space-y-1.5">{children}</ul>;
            }
            return <ul className="my-2 list-disc space-y-1 pl-5 marker:text-faint">{children}</ul>;
          },
          ol({ children }) {
            return <ol className="my-2 list-decimal space-y-1 pl-5 marker:text-faint">{children}</ol>;
          },
          li({ className, children }) {
            if (className?.includes("task-list-item")) {
              // GFM task item: first child is a disabled checkbox input.
              const kids = Children.toArray(children);
              const box = kids.find(
                (k) => isValidElement(k) && (k.props as { type?: string }).type === "checkbox",
              ) as ReactElement<{ checked?: boolean }> | undefined;
              const checked = !!box?.props?.checked;
              const rest = kids.filter((k) => k !== box);
              return (
                <li className="flex items-start gap-2">
                  <input
                    type="checkbox"
                    checked={checked}
                    readOnly
                    className="mt-[3px] h-3.5 w-3.5 shrink-0 cursor-default rounded accent-violet"
                  />
                  <span className={checked ? "leading-relaxed text-muted line-through" : "leading-relaxed"}>
                    {rest}
                  </span>
                </li>
              );
            }
            return <li className="leading-relaxed">{children}</li>;
          },
          table({ children }) {
            return (
              <div className="my-3 overflow-x-auto rounded-lg border border-line">
                <table className="w-full border-collapse text-[13px]">{children}</table>
              </div>
            );
          },
          thead({ children }) {
            return <thead className="bg-card2/60">{children}</thead>;
          },
          th({ children }) {
            return (
              <th className="border-b border-line px-3 py-1.5 text-left font-semibold text-txt">
                {children}
              </th>
            );
          },
          td({ children }) {
            return <td className="border-b border-line/60 px-3 py-1.5 text-muted">{children}</td>;
          },
          blockquote({ children }) {
            return (
              <blockquote className="my-2 border-l-2 border-violet/50 pl-3 text-muted">
                {children}
              </blockquote>
            );
          },
          h1({ children }) {
            return <h1 className="mb-2 mt-4 text-lg font-bold first:mt-0">{children}</h1>;
          },
          h2({ children }) {
            // NOTE: text-[1rem], NOT text-base — the custom `base` color shadows
            // Tailwind's `base` font-size keyword, so `text-base` also emits
            // color:var(--base), painting the heading the background color
            // (invisible in dark & light). Arbitrary size dodges the collision.
            return <h2 className="mb-2 mt-4 text-[1rem] font-bold first:mt-0">{children}</h2>;
          },
          h3({ children }) {
            return <h3 className="mb-1.5 mt-3 text-sm font-semibold first:mt-0">{children}</h3>;
          },
          h4({ children }) {
            return <h4 className="mb-1 mt-2 text-sm font-semibold first:mt-0">{children}</h4>;
          },
          hr() {
            return <hr className="my-3 border-line" />;
          },
          img({ src, alt }) {
            // 裸相对文件名（AI 手写产物引用，如 ![帧②](v3-l-f2.png)）：history
            // 侧由后端 messages_ui 改写成 fileId 图块；live 流式半截帧里没有
            // 可加载 URL——按页面 URL 解析必 404（811b… 会话四占位块报障，
            // 2026-11-21）。渲染成 alt 占位文本，不渲染 broken <img>。
            if (src && !/^(https?:|data:|blob:|\/)/.test(src)) {
              return (
                <span className="my-1 inline-flex items-center gap-1 rounded-md border border-line2 bg-card px-2 py-1 text-xs text-muted">
                  🖼 {alt || src}
                </span>
              );
            }
            return <img src={src} alt={alt || ""} className="my-2 max-h-72 rounded-lg border border-line" />;
          },
          strong({ children }) {
            return <strong className="font-semibold text-txt">{children}</strong>;
          },
          del({ children }) {
            return <del className="text-muted">{children}</del>;
          },
        }}
      >
        {src}
      </ReactMarkdown>
    </div>
  );
});
