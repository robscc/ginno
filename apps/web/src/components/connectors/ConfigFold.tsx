"use client";

/* Connector config fold (connector-module-design.md §2.2): the few knobs each
 * connector declares in its config_schema — rendered generically so new
 * connectors get their settings UI for free. */

import { useState } from "react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import type { ConnectorInfo } from "@/lib/runtime";

export function ConfigFold({
  connector,
  onSaved,
}: {
  connector: ConnectorInfo;
  onSaved: () => void;
}) {
  // conn 域 catalog（messages/{en,zh-CN}/conn.json）；schema 里的 title/description
  // 是 runtime 下发的数据文案，不在前端翻译范围。
  const tConn = useTranslations("conn");
  const [cfg, setCfg] = useState<Record<string, unknown>>(connector.config || {});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const schema = (connector.configSchema || {}) as {
    properties?: Record<string, Record<string, unknown>>;
  };
  const props = Object.entries(schema.properties || {});

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const r = await api.patchConnectorConfig(connector.id, cfg);
      if (r.ok === false && r.error) setError(r.error);
      else {
        setSaved(true);
        setTimeout(() => setSaved(false), 1500);
        onSaved();
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="mt-3 space-y-3 rounded-lg border border-line bg-hover/40 p-3">
      {props.length === 0 && (
        <div className="text-xs text-faint">{tConn("config.empty")}</div>
      )}
      {props.map(([key, spec]) => {
        const title = String(spec.title || key);
        const desc = spec.description ? String(spec.description) : "";
        const value = cfg[key];
        if (spec.type === "boolean") {
          return (
            <label key={key} className="flex cursor-pointer items-start gap-2">
              <input
                type="checkbox"
                checked={value === undefined ? spec.default === true : !!value}
                onChange={(e) => setCfg({ ...cfg, [key]: e.target.checked })}
                className="mt-0.5 h-4 w-4 accent-violet-600"
              />
              <span className="text-xs text-txt">
                {title}
                {desc && <span className="block text-faint">{desc}</span>}
              </span>
            </label>
          );
        }
        if (Array.isArray(spec.enum)) {
          return (
            <div key={key}>
              <div className="mb-1 text-xs text-txt">
                {title}
                {desc && <span className="block text-faint">{desc}</span>}
              </div>
              <select
                value={String(value ?? spec.default ?? "")}
                onChange={(e) => setCfg({ ...cfg, [key]: e.target.value })}
                className="w-full rounded-lg border border-line bg-card px-2 py-1.5 text-xs text-txt"
              >
                {(spec.enum as string[]).map((v) => (
                  <option key={v} value={v}>{v}</option>
                ))}
              </select>
            </div>
          );
        }
        if (spec.type === "array") {
          return (
            <div key={key}>
              <div className="mb-1 text-xs text-txt">
                {title}
                {desc && <span className="block text-faint">{desc}</span>}
              </div>
              <textarea
                rows={2}
                value={Array.isArray(value) ? (value as string[]).join("\n") : ""}
                onChange={(e) =>
                  setCfg({
                    ...cfg,
                    [key]: e.target.value.split("\n").map((s) => s.trim()).filter(Boolean),
                  })
                }
                placeholder={tConn("config.onePerLine")}
                className="w-full rounded-lg border border-line bg-card px-2 py-1.5 font-mono text-xs text-txt"
              />
            </div>
          );
        }
        return (
          <div key={key}>
            <div className="mb-1 text-xs text-txt">{title}</div>
            <input
              value={String(value ?? "")}
              onChange={(e) => setCfg({ ...cfg, [key]: e.target.value })}
              className="w-full rounded-lg border border-line bg-card px-2 py-1.5 text-xs text-txt"
            />
          </div>
        );
      })}
      <div className="flex items-center gap-2">
        <button
          onClick={save}
          disabled={saving}
          className="rounded-lg bg-violet-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-violet-500 disabled:opacity-50"
        >
          {saving ? tConn("config.saving") : tConn("config.save")}
        </button>
        {saved && <span className="text-xs text-green-600">{tConn("config.saved")}</span>}
        {error && <span className="text-xs text-red-600">{error}</span>}
      </div>
    </div>
  );
}
