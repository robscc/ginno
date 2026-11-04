/**
 * Markdown frontmatter（Obsidian 属性块）剥离与解析。
 *
 * 预览窗 / 知识库页阅读视图把 .md 原文交给 Markdown 组件渲染时，
 * 开头的 YAML frontmatter（title/date/tags/…）会被当正文裸奔（键值对
 * 逐行显示、`---` 变分隔线）。这里在渲染前把它剥出来，交给属性条
 * 组件紧凑展示；正文从 `# 标题` 开始照常渲染。
 *
 * 纯函数、零依赖——单测在 markdownFrontmatter.test.ts。
 */

/** 只匹配文档开头、闭合的 frontmatter 块（`---\n…\n---`，尾换行连吞后续
 *  空行——正文从第一个非空字符起，避免 body 以空行开头）。 */
export const FRONTMATTER_RE = /^---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n+|$)/;

export interface FrontmatterEntry {
  key: string;
  /** 标量值（已剥引号）；列表型为条数（见 items）。 */
  value: string;
  /** tags: [a, b] 行内数组解析出的条目（渲染成 chip）。 */
  items?: string[];
  /** 列表型（行内数组或多行 `- ` 列表计数）。 */
  list?: boolean;
}

const SCALAR_RE = /^([A-Za-z_][\w .-]*):[ \t]*(.*)$/;

function unquote(s: string): string {
  return s.trim().replace(/^["']|["']$/g, "").trim();
}

/** 解析 frontmatter 正文（简易 YAML 子集：顶层标量 + 行内数组 + 多行列表计数）。 */
export function parseFrontmatter(raw: string): FrontmatterEntry[] {
  const out: FrontmatterEntry[] = [];
  let pendingKey: string | null = null;
  let pendingCount = 0;
  const flushPending = () => {
    if (pendingKey !== null && pendingCount > 0) {
      out.push({ key: pendingKey, value: String(pendingCount), list: true });
    }
    pendingKey = null;
    pendingCount = 0;
  };
  for (const line of raw.split(/\r?\n/)) {
    if (/^\s+-\s/.test(line)) {
      if (pendingKey !== null) pendingCount += 1;
      continue;
    }
    const m = SCALAR_RE.exec(line);
    if (!m) {
      flushPending();
      continue;
    }
    flushPending();
    const key = m[1].trim();
    const rest = m[2].trim();
    if (!rest) {
      // 空值：可能是多行列表（sources: 之后跟缩进 `- `）的开头，也可能是
      // 真空值——先挂起，行末 flush。
      pendingKey = key;
      continue;
    }
    const arr = /^\[(.*)\]$/.exec(rest);
    if (arr) {
      const items = arr[1]
        .split(",")
        .map((s) => unquote(s))
        .filter(Boolean);
      out.push({ key, value: String(items.length), items, list: true });
    } else {
      out.push({ key, value: unquote(rest) });
    }
  }
  flushPending();
  return out;
}

export interface SplitFrontmatter {
  /** 剥离后的正文（原样，未匹配/解析为空时等于原文）。 */
  body: string;
  /** 解析出的顶层属性；空数组 = 没有（可展示的）frontmatter。 */
  entries: FrontmatterEntry[];
}

/**
 * 剥离文档开头的 frontmatter。保守策略：正则命中但一个键都解析不出
 * （说明开头那个 `---` 只是普通分隔线）时不剥，原文照常渲染。
 */
export function splitFrontmatter(text: string): SplitFrontmatter {
  const m = FRONTMATTER_RE.exec(text);
  if (!m) return { body: text, entries: [] };
  const entries = parseFrontmatter(m[1]);
  if (entries.length === 0) return { body: text, entries: [] };
  return { body: text.slice(m[0].length), entries };
}
