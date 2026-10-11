"use client";

import { Code2, Sparkles } from "lucide-react";
import type { CodeRoot } from "@/lib/codeTypes";
import { listCodeRoots } from "@/lib/runtime";
import { useGinno } from "@/lib/store";
import { useChatT } from "./i18n";
import type { Block } from "./types";

type FileBlock = Extract<Block, { kind: "file" }>;

export const TABLE_KINDS = new Set(["spreadsheet", "table"]);

// ---- chip → code-panel routing (docs/code-panel-design.md §3.4, D5) --------
// The DEFAULT click on a chip is unchanged: it still opens the SheetViewer
// preview (tables/PDF/images/document extracts), so chat behaviour is
// byte-for-byte the same. Text/code/data files additionally get an EXPLICIT
// secondary entry ("在代码面板打开"). Should the default ever be re-routed by
// file kind, flip the one switch below — the call sites don't move.
const CODE_CHIP_ROUTE: "preview" | "code" = "preview";
const CODE_FILE_KINDS = new Set(["text", "code", "data"]);

/** Which path a chip's (default) click should take. */
function chipRoute(fileKind: string | undefined): "preview" | "code" {
  if (CODE_CHIP_ROUTE === "code" && CODE_FILE_KINDS.has(fileKind ?? "")) return "code";
  return "preview";
}

/**
 * Secondary entry: open a file chip in the code panel. The chip carries an
 * absolute registry path (uploads land in the session workspace); the panel is
 * addressed by `root id` + root-relative path, so resolve the longest matching
 * root first. A path under no root is handed to the panel, which shows the
 * "该文件不在当前工作区根内" guidance (design §3.2).
 */
async function openChipInCode(
  g: ReturnType<typeof useGinno>,
  file: FileBlock,
): Promise<void> {
  const sessionId = g.activeSessionId;
  const path = file.path ?? "";
  if (!sessionId || !path) return;
  if (!path.startsWith("/")) {
    // Already root-relative — trust the currently selected root.
    g.openInCode({ rootId: g.codeRootId ?? "session", path });
    return;
  }
  let roots: CodeRoot[];
  try {
    roots = await listCodeRoots("default", sessionId);
  } catch {
    return;
  }
  let best: { rootId: string; rel: string } | null = null;
  for (const r of roots) {
    if (!r.path) continue;
    const prefix = r.path.endsWith("/") ? r.path : `${r.path}/`;
    if (path === r.path) {
      best = { rootId: r.id, rel: "" };
      continue;
    }
    if (path.startsWith(prefix)) {
      const rel = path.slice(prefix.length);
      // Most specific root wins = the shortest remaining relative path.
      if (!best || rel.length < best.rel.length) best = { rootId: r.id, rel };
    }
  }
  if (best) {
    g.openInCode({ rootId: best.rootId, path: best.rel });
  } else {
    // Outside every root: let the panel surface the guidance.
    g.openInCode({ rootId: g.codeRootId ?? roots[0]?.id ?? "session", path });
  }
}

/** Clickable file chips (user bubble + replayed history). Default click opens
 *  the preview; text/code/data chips also carry a secondary "open in code
 *  panel" entry. */
export function FileChips({ files }: { files: FileBlock[] }) {
  const g = useGinno();
  const { tc } = useChatT();
  if (!files.length) return null;
  return (
    <div className="mb-1 flex flex-wrap gap-1.5">
      {files.map((f, i) => {
        const clickable = !!f.fileId;
        const showCodeEntry = CODE_FILE_KINDS.has(f.fileKind ?? "") && !!f.path;
        return (
          <div
            key={f.fileId ?? `${f.name}-${i}`}
            className={`flex items-center gap-1.5 rounded-lg border border-line bg-card2 px-2 py-1 text-xs text-txt ${
              clickable ? "hover:border-violet/50" : ""
            }`}
          >
            <button
              type="button"
              disabled={!clickable}
              onClick={() => {
                if (!clickable) return;
                if (chipRoute(f.fileKind) === "code") void openChipInCode(g, f);
                else g.openPreview({ id: f.fileId!, name: f.name, path: f.path ?? "", kind: f.fileKind });
              }}
              title={clickable ? tc("fileChips.clickPreview") : f.path}
              className={`flex min-w-0 items-center gap-1.5 ${clickable ? "cursor-pointer" : "cursor-default"}`}
            >
              <span>
                {TABLE_KINDS.has(f.fileKind ?? "") ? "📊" : f.fileKind === "image" ? "🖼️" : "📄"}
              </span>
              <span className="max-w-[220px] truncate">{f.name}</span>
            </button>
            {showCodeEntry && (
              <button
                type="button"
                title={tc("fileChips.openCode")}
                aria-label={tc("fileChips.openCode")}
                onClick={() => void openChipInCode(g, f)}
                className="flex h-4 w-4 shrink-0 items-center justify-center rounded text-faint transition-colors hover:bg-card hover:text-txt"
              >
                <Code2 size={12} />
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}

type SkillBlock = Extract<Block, { kind: "skill" }>;

/** Slash-skill invocation chips (user bubble, replayed history). The SKILL.md
    body is model scaffolding; the user sees "/name" + their request only. */
export function SkillChips({ skills }: { skills: SkillBlock[] }) {
  const { tc } = useChatT();
  if (!skills.length) return null;
  return (
    <div className="mb-1 flex flex-wrap gap-1.5">
      {skills.map((s, i) => (
        <span
          key={`${s.name}-${i}`}
          title={tc("skillChips.invoked", { name: s.name })}
          className="flex items-center gap-1.5 rounded-lg border border-violet/40 bg-violet/10 px-2 py-1 text-xs text-violet"
        >
          <Sparkles className="h-3 w-3 shrink-0" />
          <span className="max-w-[220px] truncate font-medium">/{s.name}</span>
        </span>
      ))}
    </div>
  );
}
