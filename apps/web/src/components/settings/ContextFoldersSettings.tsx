"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
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
  const t = useTranslations("settings.folders");
  return (
    <button
      onClick={() => onChange(access === "rw" ? "ro" : "rw")}
      title={t("accessToggleTitle")}
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
  const t = useTranslations("settings.folders");
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
      setMsg(t("enterPath"));
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
        setMsg(r.error || t("addFailed"));
        return;
      }
      setMsg(t("added", { name: r.folder?.name ?? "" }));
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
    if (!window.confirm(t("confirmRemove", { name: f.name }))) return;
    await api.deleteFolder(f.id);
    reload();
  }

  return (
    <div className="mx-auto max-w-3xl px-8 py-7">
      <h2 className="flex items-center gap-2 text-lg font-semibold text-txt">
        <FolderInput className="h-5 w-5 text-violet" /> {t("title")}
      </h2>
      <p className="mt-1 text-sm text-muted">
        {t.rich("description", {
          code: (chunks) => <code className="text-txt">{chunks}</code>,
        })}
      </p>

      {/* ---- add form ---- */}
      <div className="mt-6 rounded-xl border border-line bg-card p-4">
        <div className="text-sm font-medium text-txt">{t("addTitle")}</div>
        <div className="mt-3 flex gap-2">
          <input
            value={path}
            onChange={(e) => {
              setPath(e.target.value);
              setProbe(null);
            }}
            onKeyDown={(e) => e.key === "Enter" && doProbe()}
            placeholder={t("pathPlaceholder")}
            className="field flex-1"
          />
          <button
            onClick={doProbe}
            className="flex items-center gap-1.5 rounded-lg border border-line bg-card px-3 py-1.5 text-xs text-muted hover:text-txt"
          >
            <Search className="h-3.5 w-3.5" /> {t("probeButton")}
          </button>
        </div>

        {probe && (
          <div className="mt-3 rounded-lg border border-line2 bg-card2 px-3 py-2 text-xs">
            {probe.ok ? (
              <div className="space-y-1 text-muted">
                <div>
                  <span className="text-txt">{probe.path}</span> ·{" "}
                  {t("filesCount", { count: `${probe.file_count}${probe.file_count_truncated ? "+" : ""}` })}
                  {probe.has_git ? ` ${t("gitRepo")}` : ""}
                </div>
                <div>
                  {probe.rule_file ? (
                    <span style={{ color: "#4ade80" }}>{t("ruleFound", { file: probe.rule_file })}</span>
                  ) : (
                    <span className="text-faint">{t("ruleNotFound")}</span>
                  )}
                  {probe.already_registered && <span style={{ color: "#fbbf24" }}>{t("alreadyRegistered")}</span>}
                </div>
              </div>
            ) : (
              <div style={{ color: "#f87171" }}>{probe.error}</div>
            )}
          </div>
        )}

        <div className="mt-3 flex items-center gap-4 text-sm text-muted">
          <label className="flex items-center gap-2">
            {t("accessLabel")}
            <select
              value={access}
              onChange={(e) => setAccess(e.target.value as "ro" | "rw")}
              className="field w-auto py-1"
            >
              <option value="rw">{t("accessRw")}</option>
              <option value="ro">{t("accessRo")}</option>
            </select>
          </label>
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={loadRules}
              onChange={(e) => setLoadRules(e.target.checked)}
            />
            {t("loadRulesLabel")}
          </label>
          <button
            onClick={add}
            disabled={busy || !path.trim()}
            className="ml-auto flex items-center gap-1.5 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            <Plus className="h-3.5 w-3.5" /> {t("addButton")}
          </button>
        </div>
        {msg && <div className="mt-2 text-xs text-muted">{msg}</div>}
      </div>

      {/* ---- library list ---- */}
      <div className="mt-6">
        <div className="mb-2 text-sm font-medium text-txt">{t("libraryTitle", { count: folders.length })}</div>
        {folders.length === 0 ? (
          <div className="rounded-xl border border-dashed border-line2 px-4 py-8 text-center text-sm text-faint">
            {t("empty")}
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
                  title={t("rulesTooltip")}
                >
                  <input
                    type="checkbox"
                    checked={f.load_rules}
                    onChange={(e) => patch(f.id, { load_rules: e.target.checked })}
                  />
                  {t("rulesLabel")}
                </label>
                <label
                  className="flex shrink-0 items-center gap-1.5 text-xs text-muted"
                  title={t("autoTooltip")}
                >
                  <input
                    type="checkbox"
                    checked={!!f.auto_mount}
                    onChange={(e) => patch(f.id, { auto_mount: e.target.checked })}
                  />
                  {t("autoLabel")}
                </label>
                <button
                  onClick={() => remove(f)}
                  className="shrink-0 rounded-lg p-1.5 text-faint hover:bg-card2 hover:text-red-400"
                  title={t("removeTitle")}
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            ))}
          </div>
        )}
        <p className="mt-3 text-xs text-faint">{t("securityNote")}</p>
      </div>
    </div>
  );
}
