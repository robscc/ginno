type InlineWidget = { kind: "stat_list" | "chart"; data: Record<string, unknown>; summary?: string };
type Part = { text: string } | { widget: InlineWidget };
const CLOSE = "</ginno-block>";
const OPEN = "<ginno-block";
const MAX_BYTES = 64 * 1024;
const ATTACHED_REF_RE = /\[attached\s+\w+:\s*[^\]]+\]/g;

/** Attachment receipts have their own chips in the transcript. */
export function cleanAgentText(text: string): string {
  return text.replace(ATTACHED_REF_RE, "").replace(/\n{3,}/g, "\n\n").trim();
}

// The gateway sometimes prints raw JSON in a quoted attribute. Scan the JSON
// object itself so apostrophes in labels and escaped double quotes are safe.
function objectEnd(text: string, start: number): number {
  if (text[start] !== "{") return -1;
  let depth = 0;
  let quoted = false;
  for (let i = start; i < text.length; i++) {
    const ch = text[i];
    if (quoted && ch === "\\") i++;
    else if (ch === '"') quoted = !quoted;
    else if (!quoted && ch === "{") depth++;
    else if (!quoted && ch === "}" && --depth === 0) return i + 1;
  }
  return -1;
}

function readWidget(markup: string): InlineWidget | null {
  const attributes = new Map<string, string>();
  const attr = /\s+(type|kind|data|summary)\s*=\s*(['"])/y;
  let cursor = OPEN.length;
  while (!/^\s*>\s*$/.test(markup.slice(cursor))) {
    attr.lastIndex = cursor;
    const match = attr.exec(markup);
    if (!match || attributes.has(match[1])) return null;
    const [, key, quote] = match;
    const start = attr.lastIndex;
    const end = key === "data" ? objectEnd(markup, start) : markup.indexOf(quote, start);
    if (end < 0 || markup[end] !== quote) return null;
    attributes.set(key, markup.slice(start, end));
    cursor = end + 1;
  }
  if (attributes.get("type") !== "render_widget") return null;
  try {
    const data = JSON.parse(attributes.get("data") ?? "") as Record<string, unknown>;
    const kind = attributes.get("kind") ?? (Array.isArray(data.items) ? "stat_list" : "chart");
    const summary = attributes.get("summary");
    if (kind === "stat_list" && Array.isArray(data.items) && data.items.length > 0 && data.items.length <= 100
      && data.items.every((row) => row && typeof row.label === "string"
        && (row.value === undefined || typeof row.value === "string")
        && (row.status === undefined || typeof row.status === "string"))) {
      return { kind, data: { title: typeof data.title === "string" ? data.title : undefined, items: data.items }, summary };
    }
    if (kind === "chart" && ["bar", "line", "area", "pie"].includes(String(data.type))
      && typeof data.x === "string" && typeof data.y === "string"
      && (data.title === undefined || typeof data.title === "string")
      && (data.format === undefined || ["number", "percent", "currency"].includes(String(data.format)))
      && Array.isArray(data.data) && data.data.length > 0 && data.data.length <= 30) {
      const { x, y } = data;
      if (data.data.every((row) => row && typeof row[x] === "string" && Number.isFinite(row[y]))) {
        return { kind, data, summary };
      }
    }
  } catch {
    // Malformed markers remain readable Markdown instead of throwing.
  }
  return null;
}

/** Compatibility for assistant prose only. Recognize standalone markers, and
 * leave fenced examples, malformed payloads, and other tag types untouched. */
export function splitInlineWidgets(text: string, streaming: boolean): Part[] {
  if (!text.includes("<ginno-")) return [{ text }];
  const parts: Part[] = [];
  const boundary = /^ {0,3}(`{3,}|~{3,})([^\n]*)|^ {0,3}<ginno-block(?=\s|$)/gm;
  let fence = "";
  let cursor = 0;
  for (let match = boundary.exec(text); match; match = boundary.exec(text)) {
    if (match[1]) {
      if (!fence) fence = match[1];
      else if (fence[0] === match[1][0] && match[1].length >= fence.length && !match[2].trim()) fence = "";
      continue;
    }
    if (fence) continue;
    const start = match.index + match[0].indexOf(OPEN);
    const close = text.indexOf(CLOSE, boundary.lastIndex);
    if (close < 0) {
      if (streaming) return [...parts, { text: text.slice(cursor, start) }];
      break;
    }
    if (close - start > MAX_BYTES) continue;
    const widget = readWidget(text.slice(start, close));
    if (!widget) continue;
    if (start > cursor) parts.push({ text: text.slice(cursor, start) });
    parts.push({ widget });
    cursor = close + CLOSE.length;
    boundary.lastIndex = cursor;
  }
  if (cursor < text.length) parts.push({ text: text.slice(cursor) });
  return parts;
}
