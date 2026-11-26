import remarkGfm from "remark-gfm";
import remarkParse from "remark-parse";
import { unified } from "unified";
import { splitFrontmatter } from "./markdownFrontmatter";

const parser = unified().use(remarkParse).use(remarkGfm);
const containers = new Set(["root", "blockquote", "list", "listItem", "table", "tableRow"]);

interface MarkdownNode {
  type: string;
  value?: string;
  children?: readonly MarkdownNode[];
  alt?: string | null;
  url?: string;
}

function visibleText(node: MarkdownNode): string {
  if (node.type === "break") return "\n";
  if (node.type === "image") {
    // Chat renders unresolved relative images as a text placeholder.
    return node.url && !/^(https?:|data:|blob:|\/)/.test(node.url) ? `🖼 ${node.alt || node.url}` : "";
  }
  if (node.children) {
    return node.children.map(visibleText).join(containers.has(node.type) ? "\n" : "");
  }
  return node.value ?? "";
}

/** Use the same Markdown grammar as the chat. Link labels are children of
 * the link node. Destinations and reference definitions are not text nodes. */
export function markdownSearchText(source: string): string {
  const { body, entries } = splitFrontmatter(source);
  const properties = entries.map(({ key, value, items }) => `${key}: ${items?.join(" ") ?? value}`);
  return [...properties, visibleText(parser.parse(body))].join("\n");
}
