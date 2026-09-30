/**
 * Extension → Monaco language id for the code panel viewer (design §4.5:
 * "语言按扩展名映射到 Monaco 内置 basic-languages，未知扩展名退 plaintext +
 * 自动换行").
 *
 * Ids are the built-in `monaco-editor/esm/vs/basic-languages` ids, so we never
 * need to register a language ourselves. The runtime's `/api/code/read` response
 * already carries a `language` field; this module is the client-side fallback
 * (and the authoritative list for what the viewer *can* highlight).
 */

/** Extensions (lower-case, no dot) → Monaco basic-language id. */
const LANGUAGE_BY_EXT: Record<string, string> = {
  // JavaScript / TypeScript
  js: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  jsx: "javascript",
  ts: "typescript",
  mts: "typescript",
  cts: "typescript",
  tsx: "typescript",
  // Data / config
  json: "json",
  jsonc: "json",
  json5: "json",
  geojson: "json",
  yml: "yaml",
  yaml: "yaml",
  toml: "ini",
  ini: "ini",
  cfg: "ini",
  conf: "ini",
  properties: "ini",
  env: "ini",
  csv: "plaintext",
  tsv: "plaintext",
  // Markup / styles
  html: "html",
  htm: "html",
  vue: "html",
  svelte: "html",
  xml: "xml",
  svg: "xml",
  xsl: "xml",
  plist: "xml",
  css: "css",
  scss: "scss",
  sass: "scss",
  less: "less",
  md: "markdown",
  markdown: "markdown",
  mdx: "markdown",
  // Systems / compiled
  c: "c",
  h: "c",
  cpp: "cpp",
  cxx: "cpp",
  cc: "cpp",
  hpp: "cpp",
  hh: "cpp",
  hxx: "cpp",
  cs: "csharp",
  java: "java",
  go: "go",
  rs: "rust",
  swift: "swift",
  kt: "kotlin",
  kts: "kotlin",
  scala: "scala",
  dart: "dart",
  lua: "lua",
  r: "r",
  // Scripting / shells
  py: "python",
  pyi: "python",
  rb: "ruby",
  php: "php",
  pl: "perl",
  pm: "perl",
  sh: "shell",
  bash: "shell",
  zsh: "shell",
  fish: "shell",
  ps1: "powershell",
  psm1: "powershell",
  bat: "bat",
  cmd: "bat",
  // Query / schema / infra
  sql: "sql",
  psql: "pgsql",
  mysql: "mysql",
  graphql: "graphql",
  gql: "graphql",
  proto: "protobuf",
  tf: "hcl",
  hcl: "hcl",
  // Misc
  ex: "elixir",
  exs: "elixir",
  clj: "clojure",
  cljs: "clojure",
  fs: "fsharp",
  vb: "vb",
  sol: "solidity",
  diff: "plaintext",
  patch: "plaintext",
  log: "plaintext",
  txt: "plaintext",
};

/** Whole-filename matches (no extension, or an extension we must not trust). */
const LANGUAGE_BY_NAME: Record<string, string> = {
  dockerfile: "dockerfile",
  ".dockerignore": "plaintext",
  ".gitignore": "plaintext",
  ".gitattributes": "plaintext",
  ".npmrc": "ini",
  ".editorconfig": "ini",
  ".env": "ini",
  ".bashrc": "shell",
  ".zshrc": "shell",
  ".profile": "shell",
  gemfile: "ruby",
  rakefile: "ruby",
};

export const PLAINTEXT = "plaintext";

/**
 * Resolve a Monaco language id from a path.
 *
 * Unknown extensions and extension-less files (Makefile, LICENSE, …) fall back
 * to `plaintext`, which the viewer opens with soft wrap on — see
 * `MonacoEditor`'s `wordWrap: "on"` default (design §4.5).
 */
export function languageForPath(path: string): string {
  const name = path.split("/").pop() ?? path;
  const lower = name.toLowerCase();

  const byName = LANGUAGE_BY_NAME[lower];
  if (byName) return byName;

  const dot = lower.lastIndexOf(".");
  if (dot <= 0) return PLAINTEXT; // no extension, or a dotfile (".env" handled above)
  const ext = lower.slice(dot + 1);
  return LANGUAGE_BY_EXT[ext] ?? PLAINTEXT;
}