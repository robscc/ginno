"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";

type Perms = { allow: string[]; deny: string[]; ask: string[] };
const EMPTY: Perms = { allow: [], deny: [], ask: [] };

function RuleList({
  title,
  which,
  rules,
  onChange,
}: {
  title: string;
  which: keyof Perms;
  rules: string[];
  onChange: (next: string[]) => void;
}) {
  // 分组说明文案按 which 取 settings.permissions.help.*
  const t = useTranslations("settings.permissions");
  const [draft, setDraft] = useState("");
  const add = () => {
    const v = draft.trim();
    if (!v) return;
    onChange([...rules, v]);
    setDraft("");
  };
  return (
    <div>
      <div className="mb-1 flex items-center gap-2">
        <span className="text-sm font-medium text-txt">{title}</span>
        <span className="rounded-full bg-card2 px-2 py-0.5 text-[11px] text-muted">{rules.length}</span>
      </div>
      <p className="mb-2 text-xs text-faint">{t(`help.${which}`)}</p>
      <div className="space-y-1.5">
        {rules.map((r, i) => (
          <div key={i} className="flex gap-2">
            <input
              className="field flex-1 font-mono text-xs"
              value={r}
              onChange={(e) => onChange(rules.map((x, j) => (j === i ? e.target.value : x)))}
            />
            <button
              onClick={() => onChange(rules.filter((_, j) => j !== i))}
              aria-label={t("deleteRule")}
              className="rounded-lg border border-line px-2 text-muted hover:text-red"
            >
              ×
            </button>
          </div>
        ))}
        <div className="flex gap-2">
          <input
            className="field flex-1 font-mono text-xs"
            placeholder={t("newRulePlaceholder")}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && add()}
          />
          <button
            onClick={add}
            className="rounded-lg border border-line2 px-3 text-xs text-muted hover:text-txt"
          >
            {t("add")}
          </button>
        </div>
      </div>
    </div>
  );
}

export function PermissionsSettings() {
  const t = useTranslations("settings.permissions");
  const [perms, setPerms] = useState<Perms>(EMPTY);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = () => {
    setMsg("");
    api
      .getSettings()
      .then((s) => {
        const p = ((s as Record<string, unknown>).permissions || {}) as Record<string, unknown>;
        setPerms({
          allow: Array.isArray(p.allow) ? (p.allow as string[]) : [],
          deny: Array.isArray(p.deny) ? (p.deny as string[]) : [],
          ask: Array.isArray(p.ask) ? (p.ask as string[]) : [],
        });
      })
      .catch(() => setMsg(t("loadFailed")));
  };
  useEffect(load, []);

  const set = (which: keyof Perms) => (next: string[]) => setPerms((p) => ({ ...p, [which]: next }));

  async function save() {
    setBusy(true);
    setMsg("");
    try {
      // PUT /settings replaces the whole file → read full settings, merge, write back
      // so providers / hooks / knowledge are not clobbered.
      const s = (await api.getSettings()) as Record<string, unknown>;
      s.permissions = { allow: perms.allow, deny: perms.deny, ask: perms.ask };
      const r = await api.putSettings(s);
      setMsg(r.ok ? t("saved") : t("saveFailed"));
    } catch {
      setMsg(t("saveFailedConn"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">{t("title")}</h2>
      <p className="mt-1 max-w-2xl text-sm text-muted">
        {t.rich("description", {
          code: (chunks) => <code className="font-mono text-txt">{chunks}</code>,
          b: (chunks) => <b>{chunks}</b>,
        })}
      </p>
      <div className="mt-5 max-w-2xl space-y-6">
        <RuleList title={t("allow")} which="allow" rules={perms.allow} onChange={set("allow")} />
        <RuleList title={t("ask")} which="ask" rules={perms.ask} onChange={set("ask")} />
        <RuleList title={t("deny")} which="deny" rules={perms.deny} onChange={set("deny")} />
        <div className="flex items-center gap-3">
          <button
            onClick={save}
            disabled={busy}
            className="rounded-lg bg-violet px-4 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            {t("save")}
          </button>
          <button
            onClick={load}
            className="rounded-lg border border-line px-3 py-1.5 text-sm text-muted hover:text-txt"
          >
            {t("reload")}
          </button>
          {msg && <span className="text-xs text-muted">{msg}</span>}
        </div>
      </div>
    </div>
  );
}
