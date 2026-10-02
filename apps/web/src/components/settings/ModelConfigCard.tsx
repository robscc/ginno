"use client";

import { useState } from "react";
import { Pencil, Trash2, Star } from "lucide-react";
import type { ModelConfig } from "@/lib/types";
import { hostOf, PROTOCOL_LABEL, protocolBadge, relTime } from "./modelConfigShared";

// One saved model config in the list (multi-provider-model-config.md §2.1).
// Presentational + local delete-confirm state; all writes go through the
// parent's callbacks (PUT is a full replacement, so the parent owns the list).

function Toggle({ on, onClick, label }: { on: boolean; onClick: () => void; label: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      onClick={onClick}
      className="relative h-5 w-9 shrink-0 rounded-full transition-colors"
      style={{ background: on ? "#8b5cf6" : "#34343f" }}
    >
      <span
        className="absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all"
        style={{ left: on ? "18px" : "2px" }}
      />
    </button>
  );
}

// Persisted verify state: green with a relative time, yellow past 7 days,
// red when the last verify failed, gray when never verified.
function VerifyDot({ cfg }: { cfg: ModelConfig }) {
  if (cfg.last_error) {
    return (
      <span className="flex items-center gap-1.5 text-faint" title={cfg.last_error}>
        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-red" />
        Last verification failed
      </span>
    );
  }
  if (cfg.verified_at) {
    const stale = Date.now() / 1000 - cfg.verified_at > 7 * 86400;
    return (
      <span
        className={`flex items-center gap-1.5 ${stale ? "text-yellow" : "text-faint"}`}
        title={stale ? `Last verified ${relTime(cfg.verified_at)}` : undefined}
      >
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${stale ? "bg-yellow" : "bg-green"}`} />
        {stale ? "Re-verification recommended" : `Verified ${relTime(cfg.verified_at)}`}
      </span>
    );
  }
  return (
    <span className="flex items-center gap-1.5 text-faint">
      <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: "#71717a" }} />
      Not verified
    </span>
  );
}

export function ModelConfigCard({
  cfg,
  isDefault,
  busy,
  onToggle,
  onSetDefault,
  onEdit,
  onDelete,
}: {
  cfg: ModelConfig;
  isDefault: boolean;
  busy?: boolean;
  onToggle: () => void;
  onSetDefault: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const badge = protocolBadge(cfg);
  const host = hostOf(cfg.base_url) || "official endpoint";
  const models = cfg.models ?? [];
  const chips = models.slice(0, 6);

  return (
    <div className={`rounded-2xl border border-line bg-card p-5 ${cfg.enabled ? "" : "opacity-70"}`}>
      <div className="flex items-center gap-2">
        <span
          className="pill"
          style={{ background: `${badge.color}1f`, color: badge.color }}
          title={`Protocol: ${PROTOCOL_LABEL[cfg.protocol]}`}
        >
          <span className="h-1.5 w-1.5 rounded-full" style={{ background: badge.color }} />
          {badge.label}
        </span>
        <span className="truncate text-sm font-semibold text-txt" title={cfg.name}>
          {cfg.name || cfg.id}
        </span>
        {isDefault && (
          <span className="pill border border-violet/50 text-violet" title="New sessions use this config by default">
            Default
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          {!isDefault &&
            cfg.enabled && (
              <button
                onClick={onSetDefault}
                disabled={busy}
                className="pill border border-line2 text-faint transition-colors hover:text-muted disabled:opacity-50"
                title="Set as the default config"
              >
                Set default
              </button>
            )}
          <button
            onClick={onEdit}
            aria-label={`Edit ${cfg.name}`}
            className="rounded-md p-1.5 text-faint transition-colors hover:bg-card2 hover:text-muted"
          >
            <Pencil className="h-3.5 w-3.5" />
          </button>
          <button
            onClick={() => setConfirming(true)}
            aria-label={`Delete ${cfg.name}`}
            className="rounded-md p-1.5 text-faint transition-colors hover:bg-card2 hover:text-red"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
          <Toggle on={cfg.enabled} onClick={onToggle} label={`Enable ${cfg.name}`} />
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-faint">
        <span>{badge.label === "OpenAI" ? "official API" : host}</span>
        <span>·</span>
        <span>
          {models.length} models{cfg.default_model ? ` · default ${cfg.default_model}` : ""}
        </span>
        <span>·</span>
        <VerifyDot cfg={cfg} />
      </div>

      {models.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {chips.map((m) => (
            <span
              key={m}
              className={`pill border font-mono ${
                m === cfg.default_model ? "border-violet/50 text-violet" : "border-line2 text-muted"
              }`}
              title={m === cfg.default_model ? "Default model of this config" : undefined}
            >
              {m === cfg.default_model && <Star className="h-2.5 w-2.5 fill-violet" />}
              {m}
            </span>
          ))}
          {models.length > chips.length && (
            <span className="pill border border-line2 text-faint">+{models.length - chips.length}</span>
          )}
        </div>
      )}

      {confirming && (
        // Q8: single yellow bar inline, no second dialog. If the backend still
        // refuses (default / referenced), the parent shows the refs list.
        <div className="mt-3 rounded-md border border-yellow/40 bg-yellow/10 px-3 py-2 text-xs text-yellow">
          {isDefault ? "This config is the global default; after deletion the default switches to the first enabled config. " : ""}
          Delete &quot;{cfg.name || cfg.id}&quot;? Agents referencing it will show as deleted, and
          historical sessions fall back to the default config.
          <div className="mt-2 flex gap-2">
            <button
              onClick={() => {
                setConfirming(false);
                onDelete();
              }}
              className="rounded-md border border-yellow/50 px-2.5 py-1 font-medium transition-colors hover:bg-yellow/20"
            >
              Delete
            </button>
            <button
              onClick={() => setConfirming(false)}
              className="rounded-md px-2.5 py-1 text-muted transition-colors hover:text-txt"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
