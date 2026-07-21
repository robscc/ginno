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
        aria-label={showKey ? "隐藏 API Key" : "显示 API Key"}
        className="absolute right-2 top-1/2 -translate-y-1/2 text-faint hover:text-muted"
      >
        {showKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
      </button>
    </div>
  );
}

function VerifyFeedback({ status }: { status: VerifyState }) {
  if (status.state === "fail") {
    return <div className="mt-1.5 text-xs text-red">验证失败：{status.msg || "未知错误"}</div>;
  }
  if (status.state === "ok") {
    return (
      <div className="mt-1.5 text-xs text-green">
        已连接{status.latency != null ? ` · ${status.latency} ms` : ""}
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
}) {
  const [showKey, setShowKey] = useState(false);
  const isCompat = cfg.protocol === "openai-compatible";

  // The backend only fails later, at chat time, on missing values
  // (models.py) — warn here instead when the provider is enabled but
  // unconfigured.
  const missing = !cfg.enabled
    ? ""
    : cfg.protocol === "anthropic" && !cfg.api_key
      ? "需要 API Key 才能调用 Anthropic。"
      : cfg.protocol === "openai" && !cfg.api_key
        ? "未填写 API Key，调用 OpenAI 会失败。"
        : cfg.protocol === "openai-compatible" && !cfg.base_url
          ? "缺少 Base URL，无法连接到自定义端点。"
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
      {status.state === "checking" ? "验证中…" : "验证"}
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
              title="新会话默认使用此提供商（可在通用设置修改）"
            >
              默认
            </span>
          ) : (
            cfg.enabled &&
            onSetDefault && (
              <button
                onClick={onSetDefault}
                className="pill border border-line2 text-faint transition-colors hover:text-muted"
                title="设为默认提供商"
              >
                设为默认
              </button>
            )
          )}
          <StatusPill cfg={cfg} status={status} />
          <Toggle on={cfg.enabled} onClick={onToggle} label={`启用 ${name}`} />
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
                placeholder="qwen-plus"
                value={cfg.model || ""}
                onChange={(e) => setField("model", e.target.value)}
                onBlur={onBlurSave}
              />
            </Field>
            <Field label="API Key (可选)">
              <div className="flex items-center gap-2">
                <KeyInput
                  value={cfg.api_key}
                  showKey={showKey}
                  onToggleShow={() => setShowKey((v) => !v)}
                  onChange={(e) => setField("api_key", e.target.value)}
                  onBlur={onBlurSave}
                  placeholder="本地端点可留空"
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
                <Field label="Timeout (s) · 仅用于验证">
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
                <span>Bearer 认证 — 第三方 Anthropic 兼容网关用 Authorization: Bearer 代替 x-api-key</span>
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

      {missing && (
        <div className="mt-3 rounded-md border border-yellow/40 bg-yellow/10 px-2 py-1 text-[11px] text-yellow">
          {missing}
        </div>
      )}
    </div>
  );
}
