"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { getVoiceSettings, saveVoiceSettings, type VoiceSettings as VoiceConfig } from "@/lib/api/voice";

export function VoiceSettings() {
  const t = useTranslations("settings.voice");
  const [config, setConfig] = useState<VoiceConfig>();
  const [error, setError] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const load = useCallback(() => {
    setError(false);
    void getVoiceSettings().then(setConfig).catch(() => setError(true));
  }, []);
  useEffect(load, [load]);
  const model = config?.models.find((item) => item.id === config.selected);
  return <section className="space-y-4 p-6">
    <h2 className="text-lg font-semibold">{t("title")}</h2>
    {error && <p role="alert" className="text-red">{t("error")} <button onClick={load} className="underline">{t("retry")}</button></p>}
    {config ? <>
      <label className="block max-w-xl space-y-1">
        <span>{t("model")}</span>
        <select className="field w-full" value={model?.id ?? ""} disabled={saving}
          onChange={(e) => { setSaved(false); setConfig({ ...config, selected: e.target.value }); }}>
          <option value="">{t("none")}</option>
          {config.models.map((item) => <option key={item.id} value={item.id}>{[item.name, item.region, item.model].filter(Boolean).join(" · ")}</option>)}
        </select>
      </label>
      <p className="text-sm text-muted">{model ? model.region ? t("destination", { region: model.region }) : t("gatewayDestination") : t("setup")}</p>
      {!!config.unavailable?.length && <div className="space-y-2 text-sm">
        <h3 className="font-medium">{t("unavailableTitle")}</h3>
        <ul className="space-y-2 text-muted">
          {config.unavailable.map((item) => <li key={item.id}>
            <span className="break-words font-medium text-txt">{item.name}{item.models.length ? ` · ${item.models.join(", ")}` : ""}</span>
            <p>{t(`unavailable.${item.reason}`)}</p>
          </li>)}
        </ul>
      </div>}
      <Link className="text-violet underline" href="/settings/connections/model-api">{t("modelApi")}</Link>
      <label className="block max-w-xl space-y-1">
        <span>{t("shortcut")}</span>
        <select className="field w-full" value={config.shortcut} disabled={saving}
          onChange={(e) => { setSaved(false); setConfig({ ...config, shortcut: e.target.value as VoiceConfig["shortcut"] }); }}>
          <option value="fn">Fn / 🌐</option><option value="ctrl-alt-v">Ctrl + Option + V</option><option value="off">{t("off")}</option>
        </select>
      </label>
      <p className="text-sm text-muted">{t("shortcutHint")}</p>
      <button className="rounded bg-violet px-3 py-1.5 text-white disabled:opacity-50" disabled={saving}
        onClick={async () => {
          setSaving(true); setError(false); setSaved(false);
          try { await saveVoiceSettings(model?.id ?? "", config.shortcut); setSaved(true); }
          catch { setError(true); }
          finally { setSaving(false); }
        }}>{t("save")}</button> {saved && <span role="status">{t("saved")}</span>}
    </> : !error && <p>{t("loading")}</p>}
  </section>;
}
