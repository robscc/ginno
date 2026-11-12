"use client";

import { FileText } from "lucide-react";
import icon_audio from "@/assets/fileicons/audio.svg";
import icon_css from "@/assets/fileicons/css.svg";
import icon_drawio from "@/assets/fileicons/drawio.svg";
import icon_excel from "@/assets/fileicons/excel.svg";
import icon_go from "@/assets/fileicons/go.svg";
import icon_html from "@/assets/fileicons/html.svg";
import icon_image from "@/assets/fileicons/image.svg";
import icon_js from "@/assets/fileicons/js.svg";
import icon_json from "@/assets/fileicons/json.svg";
import icon_markdown from "@/assets/fileicons/markdown.svg";
import icon_pdf from "@/assets/fileicons/pdf.svg";
import icon_powerpoint from "@/assets/fileicons/powerpoint.svg";
import icon_python from "@/assets/fileicons/python.svg";
import icon_rust from "@/assets/fileicons/rust.svg";
import icon_shell from "@/assets/fileicons/shell.svg";
import icon_svg from "@/assets/fileicons/svg.svg";
import icon_text from "@/assets/fileicons/text.svg";
import icon_toml from "@/assets/fileicons/toml.svg";
import icon_typescript from "@/assets/fileicons/typescript.svg";
import icon_video from "@/assets/fileicons/video.svg";
import icon_word from "@/assets/fileicons/word.svg";
import icon_xml from "@/assets/fileicons/xml.svg";
import icon_yaml from "@/assets/fileicons/yaml.svg";
import icon_zip from "@/assets/fileicons/zip.svg";

/**
 * Per-type file icons rendered from the vendored vscode-icons set
 * (src/assets/fileicons/*.svg, MIT — vendored, no CDN dependency).
 *
 * 2026-11-12 迁出 public/：fileicons 在 public/ 下被构建/热切换窗口
 * 神秘清空 ×3（元凶毫秒级未目击）——现走 Next 标准资产管理（ES import
 * 进 webpack 依赖图，构建期哈希打包进 _next/static/media/），源目录
 * 不在任何构建清理路径上，从依赖图层面根除「构建窗口丢资产」。
 *
 * Resolution order: exact extension → file kind → lucide FileText fallback.
 * Replaces the old emoji triage everywhere attachments render: ref chips,
 * file chips, composer attachment rows, user-bubble rows.
 */

/** basename → 经 import 打包的资源 URL（next 静态资产管线）。 */
const ICON_SRC: Record<string, string> = {
  audio: icon_audio,
  css: icon_css,
  drawio: icon_drawio,
  excel: icon_excel,
  go: icon_go,
  html: icon_html,
  image: icon_image,
  js: icon_js,
  json: icon_json,
  markdown: icon_markdown,
  pdf: icon_pdf,
  powerpoint: icon_powerpoint,
  python: icon_python,
  rust: icon_rust,
  shell: icon_shell,
  svg: icon_svg,
  text: icon_text,
  toml: icon_toml,
  typescript: icon_typescript,
  video: icon_video,
  word: icon_word,
  xml: icon_xml,
  yaml: icon_yaml,
  zip: icon_zip,
};

/** ext → icon basename（vscode-icons 映射） */
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
  return base ? (ICON_SRC[base] ?? null) : null;
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
