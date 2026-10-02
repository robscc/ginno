"use client";

import { useCallback, useEffect, useState } from "react";
import * as api from "@/lib/runtime";
import type { FolderEntry, FolderProbe } from "@/lib/types";
import { FolderInput, Search, Plus, Trash2 } from "lucide-react";

function AccessToggle({
  access,
  onChange,
}: {
  access: "ro" | "rw";
  onChange: (a: "ro" | "rw") => void;
}) {
  return (
    <button
      onClick={() => onChange(access === "rw" ? "ro" : "rw")}
      title="Click to toggle access level: rw read-write / ro read-only (hard constraint at the tool layer)"
      className="rounded border border-line2 px-1.5 py-0.5 font-mono text-[11px] transition-colors"
      style={{
        color: access === "rw" ? "#4ade80" : "#fbbf24",
        background: access === "rw" ? "#22c55e14" : "#f59e0b14",
      }}
    >
      {access === "rw" ? "rw" : "ro"}
    </button>
  );
}

export function ContextFoldersSettings() {
  const [folders, setFolders] = useState<FolderEntry[]>([]);
  const [path, setPath] = useState("");
  const [access, setAccess] = useState<"ro" | "rw">("rw");
  const [loadRules, setLoadRules] = useState(true);
  const [probe, setProbe] = useState<FolderProbe | null>(null);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const reload = useCallback(() => {
    api
      .listFolders()
      .then((r) => setFolders(r.folders || []))
      .catch(() => {});
  }, []);
  useEffect(reload, [reload]);

  async function doProbe() {
    setProbe(null);
    setMsg("");
    if (!path.trim()) {
      setMsg("Enter a folder path first");
      return;
    }
    setProbe(await api.probeFolder(path.trim()));
  }

  async function add() {
    setBusy(true);
    setMsg("");
    try {
      const r = await api.createFolder({ path: path.trim(), access, load_rules: loadRules });
      if (!r.ok) {
        setMsg(r.error || "Failed to add");
        return;
      }
      setMsg(`Added to the folder library: ${r.folder?.name}`);
      setPath("");
      setProbe(null);
      reload();
    } finally {
      setBusy(false);
    }
  }

  async function patch(id: string, p: Partial<FolderEntry>) {
    await api.updateFolder(id, p);
    reload();
  }

  async function remove(f: FolderEntry) {
    if (!window.confirm(`Remove "${f.name}" from the folder library? Sessions that mounted it will show it as missing.`)) return;
    await api.deleteFolder(f.id);
    reload();
  }

  return (
    <div className="mx-auto max-w-3xl px-8 py-7">
      <h2 className="flex items-center gap-2 text-lg font-semibold text-txt">
        <FolderInput className="h-5 w-5 text-violet" /> Context Folders
      </h2>
      <p className="mt-1 text-sm text-muted">
        Register local folders (repos, notes, docs) into the folder library, then mount them in a
        session (the TopBar 📁 menu or the <code className="text-txt">/mount</code> command). Once
        mounted, the Agent can read and write the files directly;{" "}
        <code className="text-txt">AGENTS.md</code> / <code className="text-txt">GINNO.md</code>{" "}
        inside the folder are injected as that folder&apos;s rules.
      </p>

      {/* ---- add form ---- */}
      <div className="mt-6 rounded-xl border border-line bg-card p-4">
        <div className="text-sm font-medium text-txt">Add a folder</div>
        <div className="mt-3 flex gap-2">
          <input
            value={path}
            onChange={(e) => {
              setPath(e.target.value);
              setProbe(null);
            }}
            onKeyDown={(e) => e.key === "Enter" && doProbe()}
            placeholder="Absolute path, e.g. ~/workspace/my-repo"
            className="field flex-1"
          />
          <button
            onClick={doProbe}
            className="flex items-center gap-1.5 rounded-lg border border-line bg-card px-3 py-1.5 text-xs text-muted hover:text-txt"
          >
            <Search className="h-3.5 w-3.5" /> Probe
          </button>
        </div>

        {probe && (
          <div className="mt-3 rounded-lg border border-line2 bg-card2 px-3 py-2 text-xs">
            {probe.ok ? (
              <div className="space-y-1 text-muted">
                <div>
                  <span className="text-txt">{probe.path}</span> · {probe.file_count}
                  {probe.file_count_truncated ? "+" : ""} files
                  {probe.has_git ? " · git repository" : ""}
                </div>
                <div>
                  {probe.rule_file ? (
                    <span style={{ color: "#4ade80" }}>Found {probe.rule_file} (will be injected as rules)</span>
                  ) : (
                    <span className="text-faint">No AGENTS.md / CLAUDE.md / GINNO.md found</span>
                  )}
                  {probe.already_registered && <span style={{ color: "#fbbf24" }}> · already in the library (will be updated)</span>}
                </div>
              </div>
            ) : (
              <div style={{ color: "#f87171" }}>{probe.error}</div>
            )}
          </div>
        )}

        <div className="mt-3 flex items-center gap-4 text-sm text-muted">
          <label className="flex items-center gap-2">
            Access level
            <select
              value={access}
              onChange={(e) => setAccess(e.target.value as "ro" | "rw")}
              className="field w-auto py-1"
            >
              <option value="rw">Read-write (rw)</option>
              <option value="ro">Read-only (ro)</option>
            </select>
          </label>
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={loadRules}
              onChange={(e) => setLoadRules(e.target.checked)}
            />
            Load its rule files (AGENTS.md / CLAUDE.md / GINNO.md)
          </label>
          <button
            onClick={add}
            disabled={busy || !path.trim()}
            className="ml-auto flex items-center gap-1.5 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            <Plus className="h-3.5 w-3.5" /> Add to library
          </button>
        </div>
        {msg && <div className="mt-2 text-xs text-muted">{msg}</div>}
      </div>

      {/* ---- library list ---- */}
      <div className="mt-6">
        <div className="mb-2 text-sm font-medium text-txt">Folder library ({folders.length})</div>
        {folders.length === 0 ? (
          <div className="rounded-xl border border-dashed border-line2 px-4 py-8 text-center text-sm text-faint">
            No folders registered yet. Add one to mount it in sessions.
          </div>
        ) : (
          <div className="space-y-2">
            {folders.map((f) => (
              <div
                key={f.id}
                className="flex items-center gap-3 rounded-xl border border-line bg-card px-4 py-3"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-txt">{f.name}</span>
                    <AccessToggle access={f.access} onChange={(a) => patch(f.id, { access: a })} />
                  </div>
                  <div className="truncate font-mono text-xs text-faint" title={f.path}>
                    {f.path}
                  </div>
                </div>
                <label
                  className="flex shrink-0 items-center gap-1.5 text-xs text-muted"
                  title="Whether to inject this folder's AGENTS.md / CLAUDE.md / GINNO.md into sessions that mount it"
                >
                  <input
                    type="checkbox"
                    checked={f.load_rules}
                    onChange={(e) => patch(f.id, { load_rules: e.target.checked })}
                  />
                  Rules
                </label>
                <button
                  onClick={() => remove(f)}
                  className="shrink-0 rounded-lg p-1.5 text-faint hover:bg-card2 hover:text-red-400"
                  title="Remove from library"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            ))}
          </div>
        )}
        <p className="mt-3 text-xs text-faint">
          Security boundary: mounting only grants file access — a folder&apos;s settings / hooks /
          skills are never loaded (access ≠ config); the read-only level is a hard constraint at the
          tool layer, independent of Privileged Mode.
        </p>
      </div>
    </div>
  );
}
