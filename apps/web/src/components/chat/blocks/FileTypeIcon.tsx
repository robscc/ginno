"use client";

import { FileText } from "lucide-react";

/**
 * Per-type file icons rendered from the vendored vscode-icons set
 * (public/fileicons/*.svg, MIT — downloaded once, no CDN dependency).
 * Resolution order: exact extension → file kind → lucide FileText fallback.
 * Replaces the old emoji triage (📊/🖼️/📄) everywhere attachments render:
 * ref chips, file chips, composer attachment rows, user-bubble rows.
 */

/** ext → icon basename under public/fileicons/ */
const EXT_ICON: Record<string, string> = {
  md: "markdown",
  markdown: "markdown",
  txt: "text",
  log: "text",
  json: "json",
  jsonl: "json",
  xml: "xml",
  csv: "text",
  tsv: "text",
  xlsx: "excel",
  xlsm: "excel",
  xls: "excel",
  docx: "word",
  doc: "word",
  pptx: "powerpoint",
  ppt: "powerpoint",
  pdf: "pdf",
  html: "html",
  htm: "html",
  css: "css",
  yaml: "yaml",
  yml: "yaml",
  toml: "toml",
  py: "python",
  ts: "typescript",
  tsx: "typescript",
  mts: "typescript",
  js: "js",
  mjs: "js",
  cjs: "js",
  rs: "rust",
  go: "go",
  sh: "shell",
  zsh: "shell",
  bash: "shell",
  svg: "svg",
  zip: "zip",
  gz: "zip",
  tgz: "zip",
  drawio: "drawio",
  dio: "drawio",
};

/** file kind (registry classify) → icon basename; consulted when the
 *  extension has no exact icon (e.g. exotic image formats → image.svg). */
const KIND_ICON: Record<string, string> = {
  spreadsheet: "excel",
  table: "text",
  document: "word",
  presentation: "powerpoint",
  pdf: "pdf",
  data: "json",
  text: "text",
  image: "image",
  html: "html",
  drawio: "drawio",
};

export function fileIconSrc(
  name: string | undefined,
  kind: string | undefined,
): string | null {
  const ext = (name ?? "").includes(".")
    ? (name ?? "").split(".").pop()!.toLowerCase()
    : "";
  const base = EXT_ICON[ext] ?? KIND_ICON[kind ?? ""] ?? "";
  return base ? `/fileicons/${base}.svg` : null;
}

export function FileTypeIcon({
  name,
  kind,
  className = "h-3.5 w-3.5 shrink-0",
  fallbackClassName,
}: {
  name?: string;
  kind?: string;
  className?: string;
  fallbackClassName?: string;
}) {
  const src = fileIconSrc(name, kind);
  if (src) {
    // eslint-disable-next-line @next/next/no-img-element
    return <img src={src} alt="" aria-hidden draggable={false} className={className} />;
  }
  return <FileText className={fallbackClassName ?? className} aria-hidden />;
}
