"use client";

import {
  Children,
  isValidElement,
  useState,
  type ReactElement,
  type ReactNode,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";

/** Recursively extract plain text from rendered children (for the copy button). */
function nodeText(n: ReactNode): string {
  if (n == null || typeof n === "boolean") return "";
  if (typeof n === "string" || typeof n === "number") return String(n);
  if (Array.isArray(n)) return n.map(nodeText).join("");
  if (isValidElement(n)) return nodeText((n.props as { children?: ReactNode }).children);
  return "";
}

function CodeBlock({ lang, children }: { lang?: string; children: ReactNode }) {
  const [copied, setCopied] = useState(false);
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
          {lang || "code"}
        </span>
        <button
          onClick={copy}
          className="text-[11px] text-faint transition-colors hover:text-txt"
        >
          {copied ? "已复制 ✓" : "复制"}
        </button>
      </div>
      <pre className="overflow-x-auto bg-[rgb(var(--code-bg))] p-3 text-xs leading-relaxed">
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
export function Markdown({ text }: { text: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeHighlight]}
      components={{
        pre({ children }) {
          // Fenced code: consume the inner <code> element, keep its highlighted
          // children, and wrap in a labeled/copyable chrome.
          const el = Children.toArray(children).find(isValidElement) as
            | ReactElement<{ className?: string; children?: ReactNode }>
            | undefined;
          const lang = /language-([\w+-]+)/.exec(el?.props?.className || "")?.[1];
          return <CodeBlock lang={lang}>{el?.props?.children}</CodeBlock>;
        },
        code({ children }) {
          // Inline code only — block code is handled by the pre override above.
          return (
            <code className="rounded border border-line bg-card2/70 px-1 py-0.5 font-mono text-[0.85em] text-[rgb(var(--inline-code))]">
              {children}
            </code>
          );
        },
        p({ children }) {
          return <p className="my-2 leading-relaxed first:mt-0 last:mb-0">{children}</p>;
        },
        a({ href, children }) {
          return (
            <a
              href={href}
              target="_blank"
              rel="noreferrer"
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
          return <h2 className="mb-2 mt-4 text-base font-bold first:mt-0">{children}</h2>;
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
      {text}
    </ReactMarkdown>
  );
}
