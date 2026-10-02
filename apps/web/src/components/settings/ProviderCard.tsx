"use client";

import { useState, type ChangeEvent, type ReactNode } from "react";
import { Eye, EyeOff } from "lucide-react";
import type { ProviderConfig } from "@/lib/types";

export type VerifyState = {
  state: "idle" | "checking" | "ok" | "fail";
  msg?: string;
  latency?: number;
};

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
  let text = "Not configured";
  if (status.state === "checking") {
    color = "#eab308";
    text = "Verifying";
  } else if (status.state === "ok") {
    color = "#22c55e";
    text = "Connected";
  } else if (status.state === "fail") {
    color = "#ef4444";
    text = "Failed";
  } else if (cfg.enabled && (cfg.api_key || cfg.base_url)) {
    color = "#60a5fa";
    text = "Configured";
  }
  return (
    <span className="pill" style={{ background: color + "1f", color }} title={status.msg}>
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
      {text}
    </span>
  );
}

function Toggle({ on, onClick, label }: { on: boolean; onClick: () => void; label: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
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

// Deliberately defined at module level: if this lived inside ProviderCard's
// render function it would get a fresh component identity on every render,
// and React would remount the input on each keystroke — dropping focus while
// typing the API key.
function KeyInput({
  value,
  showKey,
  onToggleShow,
  onChange,
  onBlur,
  placeholder,
}: {
  value: string;
  showKey: boolean;
  onToggleShow: () => void;
  onChange: (e: ChangeEvent<HTMLInputElement>) => void;
  onBlur: () => void;
  placeholder?: string;
}) {
  return (
    <div className="relative flex-1">
      <input
        type={showKey ? "text" : "password"}
        value={value}
        placeholder={placeholder}
        onChange={onChange}
        onBlur={onBlur}
        className="field pr-9"
      />
      <button
        type="button"
        onClick={onToggleShow}
        aria-label={showKey ? "Hide API key" : "Show API key"}
        className="absolute right-2 top-1/2 -translate-y-1/2 text-faint hover:text-muted"
      >
        {showKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
      </button>
    </div>
  );
}

function VerifyFeedback({ status }: { status: VerifyState }) {
  if (status.state === "fail") {
    return <div className="mt-1.5 text-xs text-red">Verification failed: {status.msg || "unknown error"}</div>;
  }
  if (status.state === "ok") {
    return (
      <div className="mt-1.5 text-xs text-green">
        Connected{status.latency != null ? ` · ${status.latency} ms` : ""}
      </div>
    );
  }
  return null;
}

export function ProviderCard({
  cfg,
  icon,
  name,
  subtitle,
  status,
  isDefault,
  setField,
  onBlurSave,
  onToggle,
  onVerify,
  onSetDefault,
  onToggleSearch,
  onToggleThinking,
  onTestSearch,
  searchStatus,
}: {
  cfg: ProviderConfig;
  icon: ReactNode;
  name: string;
  subtitle: string;
  status: VerifyState;
  isDefault?: boolean;
  setField: (key: keyof ProviderConfig, value: unknown) => void;
  onBlurSave: () => void;
  onToggle: () => void;
  onVerify: () => void;
  onSetDefault?: () => void;
  onToggleSearch?: () => void;
  onToggleThinking?: () => void;
  onTestSearch?: () => void;
  searchStatus?: { state: "idle" | "checking" | "ok" | "fail"; text?: string };
}) {
  const [showKey, setShowKey] = useState(false);
  const isCompat = cfg.protocol === "openai-compatible";

  // The backend only fails later, at chat time, on missing values
  // (models.py) — warn here instead when the provider is enabled but
  // unconfigured.
  const missing = !cfg.enabled
    ? ""
    : cfg.protocol === "anthropic" && !cfg.api_key
      ? "An API key is required to call Anthropic."
      : cfg.protocol === "openai" && !cfg.api_key
        ? "Calls to OpenAI will fail without an API key."
        : cfg.protocol === "openai-compatible" && !cfg.base_url
          ? "Base URL is missing; cannot connect to the custom endpoint."
          : "";

  // Empty input = omit the field entirely, so the backend falls back to its
  // per-protocol default. Sending 0 would NOT mean "default": temperature 0
  // is a literal 0.0 (greedy decoding) in models.py `_sampling`.
  const num = (key: "max_tokens" | "timeout_s" | "temperature", v: string) => {
    if (v === "") return setField(key, undefined);
    const n = Number(v);
    setField(key, Number.isFinite(n) ? n : undefined);
  };

  const verifyBtn = (
    <button
      onClick={onVerify}
      disabled={status.state === "checking"}
      className="shrink-0 rounded-lg border border-line2 px-3 py-2 text-xs text-muted hover:text-txt disabled:opacity-50"
    >
      {status.state === "checking" ? "Verifying…" : "Verify"}
    </button>
  );

  return (
    <div className="rounded-2xl border border-line bg-card p-5">
      <div className="mb-4 flex items-center gap-3">
        {icon}
        <div className="min-w-0">
          <div className="text-sm font-semibold text-txt">{name}</div>
          <div className="truncate text-xs text-faint">{subtitle}</div>
        </div>
        <div className="ml-auto flex items-center gap-3">
          {isDefault ? (
            <span
              className="pill border border-violet/50 text-violet"
              title="New sessions use this provider by default (changeable in General)"
            >
              Default
            </span>
          ) : (
            cfg.enabled &&
            onSetDefault && (
              <button
                onClick={onSetDefault}
                className="pill border border-line2 text-faint transition-colors hover:text-muted"
                title="Set as the default provider"
              >
                Set default
              </button>
            )
          )}
          <StatusPill cfg={cfg} status={status} />
          <Toggle on={cfg.enabled} onClick={onToggle} label={`Enable ${name}`} />
        </div>
      </div>

      {!isCompat && (
        <div className="mb-4">
          <label className="field-label">API Key</label>
          <div className="flex items-center gap-2">
            <KeyInput
              value={cfg.api_key}
              showKey={showKey}
              onToggleShow={() => setShowKey((v) => !v)}
              onChange={(e) => setField("api_key", e.target.value)}
              onBlur={onBlurSave}
            />
            {verifyBtn}
          </div>
          <VerifyFeedback status={status} />
        </div>
      )}

      {isCompat ? (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Endpoint name">
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
                placeholder="qwen-plus"
                value={cfg.model || ""}
                onChange={(e) => setField("model", e.target.value)}
                onBlur={onBlurSave}
              />
            </Field>
            <Field label="API Key (optional)">
              <div className="flex items-center gap-2">
                <KeyInput
                  value={cfg.api_key}
                  showKey={showKey}
                  onToggleShow={() => setShowKey((v) => !v)}
                  onChange={(e) => setField("api_key", e.target.value)}
                  onBlur={onBlurSave}
                  placeholder="Leave empty for local endpoints"
                />
                {verifyBtn}
              </div>
            </Field>
          </div>
          <VerifyFeedback status={status} />
        </div>
      ) : (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Default model">
              <input
                className="field"
                value={cfg.default_model || ""}
                onChange={(e) => setField("default_model", e.target.value)}
                onBlur={onBlurSave}
              />
            </Field>
            <Field label={cfg.protocol === "openai" ? "Base URL (optional proxy)" : "Base URL (optional)"}>
              <input
                className="field"
                placeholder={
                  cfg.protocol === "openai" ? "https://api.openai.com/v1" : "https://api.anthropic.com"
                }
                value={cfg.base_url}
                onChange={(e) => setField("base_url", e.target.value)}
                onBlur={onBlurSave}
              />
            </Field>
          </div>

          {cfg.protocol === "anthropic" ? (
            <>
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
                    onChange={(e) => num("temperature", e.target.value)}
                    onBlur={onBlurSave}
                  />
                </Field>
                <Field label="Timeout (s) · verification only">
                  <input
                    type="number"
                    className="field"
                    value={cfg.timeout_s ?? ""}
                    onChange={(e) => num("timeout_s", e.target.value)}
                    onBlur={onBlurSave}
                  />
                </Field>
              </div>
              <label className="flex items-start gap-2 text-xs text-muted">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={!!cfg.bearer_auth}
                  onChange={(e) => setField("bearer_auth", e.target.checked)}
                  onBlur={onBlurSave}
                />
                <span>Bearer auth — third-party Anthropic-compatible gateways use Authorization: Bearer instead of x-api-key</span>
              </label>
            </>
          ) : (
            <div className="grid grid-cols-2 gap-3">
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

      {cfg.protocol !== "anthropic" && (
        <div className="mt-4 border-t border-line pt-3">
          <label className="flex items-start gap-2 text-sm text-txt">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={!!cfg.enable_search}
              onChange={() => onToggleSearch?.()}
            />
            <span>
              Web search — when enabled, the model goes online automatically when needed (requires
              endpoint support, e.g. Qwen compatible-mode&apos;s{" "}
              <code className="font-mono text-xs">enable_search</code>).
            </span>
          </label>
          <div className="mt-2 flex items-center gap-2">
            <button
              onClick={onTestSearch}
              disabled={!cfg.enabled || searchStatus?.state === "checking"}
              className="rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:text-txt disabled:opacity-50"
            >
              {searchStatus?.state === "checking" ? "Testing…" : "Test web search"}
            </button>
            {searchStatus?.state === "fail" && (
              <span className="text-xs text-red">Failed: {searchStatus.text}</span>
            )}
          </div>
          {searchStatus?.state === "ok" && searchStatus?.text && (
            <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded-lg bg-base/60 p-2 text-[11px] text-muted">
              {searchStatus.text}
            </pre>
          )}
          <label className="mt-3 flex items-start gap-2 text-sm text-txt">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={!!cfg.enable_thinking}
              onChange={() => onToggleThinking?.()}
            />
            <span>
              Thinking mode — hybrid thinking models (e.g. the Qwen3 series) output their reasoning
              process before the answer (requires endpoint support for{" "}
              <code className="font-mono text-xs">enable_thinking</code>; streaming responses only).
            </span>
          </label>
        </div>
      )}

      {missing && (
        <div className="mt-3 rounded-md border border-yellow/40 bg-yellow/10 px-2 py-1 text-[11px] text-yellow">
          {missing}
        </div>
      )}
    </div>
  );
}
