"use client";
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import { Bot, Save } from "lucide-react";

// External coding-agent delegation (docs/external-agents-design.md):
// opt-in gate + backend detection. External CLIs bring their own model
// accounts — Ginno never forwards provider credentials to them.
interface ExtForm {
  enabled: boolean;
  maxActive: number;
  mode: string;
}

interface BackendRow {
  name: string;
  installed: boolean;
  path: string;
}

export function ExternalAgentsSettings() {
  const t = useTranslations("settings.externalAgents");
  const [form, setForm] = useState<ExtForm>({
    enabled: false,
    maxActive: 8,
    mode: "",
  });
  const [backends, setBackends] = useState<BackendRow[]>([]);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .getSettings()
      .then((s) => {
        const c = ((s as Record<string, any>).context || {}) as Record<string, any>;
        setForm({
          enabled: !!c.external_agents_enabled,
          maxActive:
            typeof c.max_active_delegations === "number"
              ? c.max_active_delegations
              : 8,
          mode: typeof c.delegation_mode === "string" ? c.delegation_mode : "",
        });
      })
      .catch(() => {});
    api.getExternalAgents().then(setBackends).catch(() => {});
  }, []);

  const save = async () => {
    setBusy(true);
    setMsg("");
    try {
      // PUT /settings is a FULL-FILE replace: read the current settings
      // first and abort on a failed read — falling back to {} would wipe
      // every other settings block (same guard as WebSearchSettings).
      const cur = (await api.getSettings()) as Record<string, any>;
      if (!cur || typeof cur !== "object" || Object.keys(cur).length === 0) {
        setMsg(t("readAborted"));
        return;
      }
      cur.context = {
        ...(cur.context || {}),
        external_agents_enabled: form.enabled,
        // 并发上限夹在 1..32,非法输入回落 8——runtime 侧还有同样兜底
        max_active_delegations: Math.max(
          1,
          Math.min(32, Math.round(Number(form.maxActive)) || 8)
        ),
        delegation_mode: ["edit", "read-only"].includes(form.mode)
          ? form.mode
          : "",
      };
      await api.putSettings(cur);
      setMsg(t("saved"));
    } catch {
      setMsg(t("saveFailed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-2xl space-y-6 p-6">
      <div className="flex items-center gap-2">
        <Bot className="h-5 w-5 text-violet" />
        <h2 className="text-lg font-semibold text-txt">{t("title")}</h2>
      </div>
      <p className="text-sm text-muted">{t("intro")}</p>

      <div className="rounded-lg border border-line bg-card/40 p-4">
        <label className="flex cursor-pointer items-center gap-3">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
            className="h-4 w-4 accent-[#a78bfa]"
          />
          <span className="text-sm text-txt">{t("enable")}</span>
        </label>
        <p className="mt-2 text-xs text-faint">{t("enableHint")}</p>
        <div className="mt-3 flex items-center gap-3 border-t border-line pt-3">
          <label htmlFor="ext-max-active" className="text-sm text-txt">
            {t("maxActive")}
          </label>
          <input
            id="ext-max-active"
            type="number"
            min={1}
            max={32}
            value={form.maxActive}
            onChange={(e) =>
              setForm({ ...form, maxActive: Number(e.target.value) })
            }
            className="w-20 rounded-md border border-line2 bg-card px-2 py-1 text-sm text-txt"
          />
          <span className="text-xs text-faint">{t("maxActiveHint")}</span>
        </div>
        <div className="mt-3 flex items-center gap-3 border-t border-line pt-3">
          <label htmlFor="ext-delegation-mode" className="text-sm text-txt">
            {t("delegationMode")}
          </label>
          <select
            id="ext-delegation-mode"
            value={form.mode}
            onChange={(e) => setForm({ ...form, mode: e.target.value })}
            className="rounded-md border border-line2 bg-card px-2 py-1 text-sm text-txt"
          >
            <option value="">{t("modeAuto")}</option>
            <option value="edit">{t("modeEdit")}</option>
            <option value="read-only">{t("modeReadOnly")}</option>
          </select>
          <span className="text-xs text-faint">{t("delegationModeHint")}</span>
        </div>
      </div>

      <div className="rounded-lg border border-line bg-card/40 p-4">
        <div className="mb-3 text-sm font-medium text-txt">{t("backends")}</div>
        <div className="space-y-1.5">
          {backends.length === 0 && (
            <div className="text-xs text-faint">{t("noBackends")}</div>
          )}
          {backends.map((b) => (
            <div key={b.name} className="flex items-center gap-2 text-sm">
              <span className={b.installed ? "text-green" : "text-faint"}>
                {b.installed ? "✓" : "✗"}
              </span>
              <span className="font-mono text-txt">{b.name}</span>
              <span className="truncate text-xs text-faint" title={b.path}>
                {b.installed ? b.path : t("notInstalled")}
              </span>
            </div>
          ))}
        </div>
        <p className="mt-3 text-xs text-faint">{t("ownAccount")}</p>
      </div>

      <div className="flex items-center gap-3">
        <button
          onClick={save}
          disabled={busy}
          className="flex items-center gap-1.5 rounded-md bg-violet px-3 py-1.5 text-sm text-white disabled:opacity-50"
        >
          <Save className="h-3.5 w-3.5" />
          {t("save")}
        </button>
        {msg && <span className="text-xs text-faint">{msg}</span>}
      </div>
    </div>
  );
}
