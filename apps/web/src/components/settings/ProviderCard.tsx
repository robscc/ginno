"use client";

import { useState, type ReactNode } from "react";
import { Eye, EyeOff } from "lucide-react";
import type { ProviderConfig } from "@/lib/types";

export type VerifyState = { state: "idle" | "checking" | "ok" | "fail"; msg?: string };

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <label className="field-label">{label}</label>
      {children}
    </div>
  );
}

function StatusPill({ cfg, status }: { cfg: ProviderConfig; status: VerifyState }) {
  let color = "#71717a";
  let text = "未配置";
  if (status.state === "checking") {
    color = "#eab308";
    text = "验证中";
  } else if (status.state === "ok") {
    color = "#22c55e";
    text = "已连接";
  } else if (status.state === "fail") {
    color = "#ef4444";
    text = "失败";
  } else if (cfg.enabled && (cfg.api_key || cfg.base_url)) {
    color = "#60a5fa";
    text = "已配置";
  }
  return (
    <span className="pill" style={{ background: color + "1f", color }} title={status.msg}>
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
      {text}
    </span>
  );
}

function Toggle({ on, onClick }: { on: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="relative h-5 w-9 rounded-full transition-colors"
      style={{ background: on ? "#8b5cf6" : "#34343f" }}
    >
      <span
        className="absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all"
        style={{ left: on ? "18px" : "2px" }}
      />
    </button>
  );
}

export function ProviderCard({
  cfg,
  icon,
  name,
  subtitle,
  status,
  setField,
  onBlurSave,
  onToggle,
  onVerify,
}: {
  cfg: ProviderConfig;
  icon: ReactNode;
  name: string;
  subtitle: string;
  status: VerifyState;
  setField: (key: keyof ProviderConfig, value: unknown) => void;
  onBlurSave: () => void;
  onToggle: () => void;
  onVerify: () => void;
}) {
  const [showKey, setShowKey] = useState(false);
  const isCompat = cfg.protocol === "openai-compatible";

  const KeyInput = ({ optional }: { optional?: boolean }) => (
    <div className="relative flex-1">
      <input
        type={showKey ? "text" : "password"}
        value={cfg.api_key}
        placeholder={optional ? "可选" : ""}
        onChange={(e) => setField("api_key", e.target.value)}
        onBlur={onBlurSave}
        className="field pr-9"
      />
      <button
        type="button"
        onClick={() => setShowKey((v) => !v)}
        className="absolute right-2 top-1/2 -translate-y-1/2 text-faint hover:text-muted"
      >
        {showKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
      </button>
    </div>
  );

  const num = (key: "max_tokens" | "timeout_s", v: unknown) =>
    setField(key, v === "" ? 0 : Number(v));

  return (
    <div className="rounded-2xl border border-line bg-card p-5">
      <div className="mb-4 flex items-center gap-3">
        {icon}
        <div className="min-w-0">
          <div className="text-sm font-semibold text-txt">{name}</div>
          <div className="truncate text-xs text-faint">{subtitle}</div>
        </div>
        <div className="ml-auto flex items-center gap-3">
          <StatusPill cfg={cfg} status={status} />
          <Toggle on={cfg.enabled} onClick={onToggle} />
        </div>
      </div>

      {!isCompat && (
        <div className="mb-4">
          <label className="field-label">API Key</label>
          <div className="flex items-center gap-2">
            <KeyInput />
            <button
              onClick={onVerify}
              disabled={status.state === "checking"}
              className="shrink-0 rounded-lg border border-line2 px-3 py-2 text-xs text-muted hover:text-txt disabled:opacity-50"
            >
              验证
            </button>
          </div>
        </div>
      )}

      {isCompat ? (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label="端点名称">
              <input
                className="field"
                placeholder="My Local LLM"
                value={cfg.name || ""}
                onChange={(e) => setField("name", e.target.value)}
                onBlur={onBlurSave}
              />
            </Field>
            <Field label="Base URL *">
              <input
                className="field"
                placeholder="http://localhost:11434/v1"
                value={cfg.base_url}
                onChange={(e) => setField("base_url", e.target.value)}
                onBlur={onBlurSave}
              />
            </Field>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Model Name">
              <input
                className="field"
                placeholder="qwen3.7-plus"
                value={cfg.model || ""}
                onChange={(e) => setField("model", e.target.value)}
                onBlur={onBlurSave}
              />
            </Field>
            <Field label="API Key (可选)">
              <div className="flex items-center gap-2">
                <KeyInput optional />
                <button
                  onClick={onVerify}
                  disabled={status.state === "checking"}
                  className="shrink-0 rounded-lg border border-line2 px-3 py-2 text-xs text-muted hover:text-txt disabled:opacity-50"
                >
                  验证
                </button>
              </div>
            </Field>
          </div>
        </div>
      ) : (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label="默认模型">
              <input
                className="field"
                value={cfg.default_model || ""}
                onChange={(e) => setField("default_model", e.target.value)}
                onBlur={onBlurSave}
              />
            </Field>
            <Field label={cfg.protocol === "openai" ? "Base URL (可选代理)" : "Base URL (可选)"}>
              <input
                className="field"
                placeholder={cfg.protocol === "openai" ? "https://api.openai.com/v1" : "https://api.anthropic.com"}
                value={cfg.base_url}
                onChange={(e) => setField("base_url", e.target.value)}
                onBlur={onBlurSave}
              />
            </Field>
          </div>

          {cfg.protocol === "anthropic" ? (
            <div className="grid grid-cols-3 gap-3">
              <Field label="Max Tokens">
                <input
                  type="number"
                  className="field"
                  value={cfg.max_tokens ?? ""}
                  onChange={(e) => num("max_tokens", e.target.value)}
                  onBlur={onBlurSave}
                />
              </Field>
              <Field label="Temperature">
                <input
                  type="number"
                  step="0.1"
                  className="field"
                  value={cfg.temperature ?? ""}
                  onChange={(e) => setField("temperature", e.target.value === "" ? 0 : Number(e.target.value))}
                  onBlur={onBlurSave}
                />
              </Field>
              <Field label="Timeout (s)">
                <input
                  type="number"
                  className="field"
                  value={cfg.timeout_s ?? ""}
                  onChange={(e) => num("timeout_s", e.target.value)}
                  onBlur={onBlurSave}
                />
              </Field>
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-3">
              <Field label="Organization ID">
                <input
                  className="field"
                  placeholder="org-xxxxxxxxxx (可选)"
                  value={cfg.org_id || ""}
                  onChange={(e) => setField("org_id", e.target.value)}
                  onBlur={onBlurSave}
                />
              </Field>
              <Field label="Max Tokens">
                <input
                  type="number"
                  className="field"
                  value={cfg.max_tokens ?? ""}
                  onChange={(e) => num("max_tokens", e.target.value)}
                  onBlur={onBlurSave}
                />
              </Field>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
