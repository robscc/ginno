"use client";

import { useMemo, useState, type ReactNode } from "react";
import { Eye, EyeOff } from "lucide-react";
import * as api from "@/lib/runtime";
import type { ModelConfig, ModelConfigRefs, ModelProtocol } from "@/lib/types";
import {
  blankConfig,
  describeRefs,
  domainRecommendations,
  parseModelsText,
  PROTOCOLS,
  PROTOCOL_LABEL,
} from "./modelConfigShared";

// Add/edit form for one model config (multi-provider-model-config.md §2.2–2.4).
// "验证并保存" (Q4): the verify endpoint persists on success, so there is no
// separate save button — onSaved() hands the returned record back to the list.

function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <div>
      <label className="field-label">{label}</label>
      {children}
      {hint && <p className="mt-1 text-[11px] text-faint">{hint}</p>}
    </div>
  );
}

export function ModelConfigForm({
  initial,
  isNew,
  onCancel,
  onSaved,
}: {
  initial: ModelConfig;
  isNew: boolean;
  onCancel: () => void;
  onSaved: (cfg: ModelConfig, latencyMs: number) => void;
}) {
  const [draft, setDraft] = useState<ModelConfig>(initial.protocol ? initial : blankConfig());
  const [modelsText, setModelsText] = useState(initial.models?.join("\n") ?? "");
  const [showKey, setShowKey] = useState(false);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refs, setRefs] = useState<ModelConfigRefs | undefined>(undefined);

  const set = <K extends keyof ModelConfig>(key: K, value: ModelConfig[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));

  const isAnthropic = draft.protocol === "anthropic";
  const isCompat = draft.protocol === "openai-compatible";
  const isResponses = draft.protocol === "openai-responses";

  const recommendations = useMemo(
    () => (isCompat ? domainRecommendations(draft.base_url) : []),
    [isCompat, draft.base_url],
  );

  const onModelsText = (text: string) => {
    const models = parseModelsText(text);
    setModelsText(text);
    setDraft((d) => {
      // Keep default_model only while it survives in the list; otherwise fall
      // to the first entry (backend invariant: default_model ∈ models).
      const dm = d.default_model && models.includes(d.default_model) ? d.default_model : (models[0] ?? "");
      return { ...d, models, default_model: dm };
    });
  };

  // Blank input = omit the field so the backend applies its per-protocol
  // default; temperature 0 is a literal greedy decode, not "default".
  const num = (key: "max_tokens" | "timeout_s" | "temperature", v: string) => {
    const n = v === "" ? undefined : Number(v);
    setDraft((d) => ({ ...d, [key]: n != null && Number.isFinite(n) ? n : undefined }));
  };

  const validate = (): string | null => {
    if (!draft.name.trim()) return "请填写配置名称。";
    if (isCompat && !draft.base_url.trim()) return "OpenAI Compatible 端点必须填写 Base URL。";
    if (!isCompat && !draft.api_key.trim()) return "该协议需要 API Key（Compatible 端点才可留空）。";
    if (!draft.models.length) return "请至少添加一个模型 id。";
    if (!draft.default_model || !draft.models.includes(draft.default_model))
      return "默认模型必须是模型列表中的一员。";
    return null;
  };

  // Q4: verify against the DRAFT (no save-then-verify dance); a pass is
  // already persisted server-side and the response carries the saved record.
  const submit = async () => {
    setError(null);
    setRefs(undefined);
    const invalid = validate();
    if (invalid) {
      setError(invalid);
      return;
    }
    setChecking(true);
    try {
      const body = { ...draft, name: draft.name.trim(), id: isNew ? undefined : draft.id };
      const r = await api.verifyModelConfig(body);
      if (r.ok) {
        onSaved(r.config, r.latency_ms);
        return;
      }
      setError(r.error || "验证失败：未知错误");
      setRefs(r.refs);
    } catch {
      setError("无法连接运行时");
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="rounded-2xl border border-indigo/40 bg-card p-5">
      <div className="mb-4 text-sm font-semibold text-txt">
        {isNew ? "新增模型配置" : `编辑「${initial.name || initial.id}」`}
      </div>

      <div className="space-y-3">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label="名称 *">
            <input
              className="field"
              placeholder="如：中转站 A / DeepSeek / 本地 Ollama"
              value={draft.name}
              onChange={(e) => set("name", e.target.value)}
              autoFocus={isNew}
            />
          </Field>
          <div className="sm:col-span-2">
            <label className="field-label">协议</label>
            <div className="flex rounded-lg border border-line p-0.5">
              {PROTOCOLS.map((p) => (
                <button
                  key={p}
                  type="button"
                  onClick={() => set("protocol", p)}
                  className={`flex-1 rounded-md px-2 py-1.5 text-xs transition-colors ${
                    draft.protocol === p ? "bg-card2 text-txt" : "text-muted hover:text-txt"
                  }`}
                >
                  {PROTOCOL_LABEL[p]}
                </button>
              ))}
            </div>
            {isCompat && (
              <p className="mt-1 text-[11px] text-faint">
                自定义 base_url 的 OpenAI 兼容端点：DeepSeek、通义千问、Kimi、中转站、Ollama 等都归这类。
              </p>
            )}
          </div>
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field
            label={isCompat ? "Base URL *（必填）" : "Base URL（可选，默认官方）"}
            hint={isResponses ? "默认 https://api.openai.com/v1" : isAnthropic ? "默认 https://api.anthropic.com" : undefined}
          >
            <input
              className="field"
              placeholder={isAnthropic ? "https://api.anthropic.com" : isResponses ? "https://api.openai.com/v1" : "https://api.deepseek.com/v1"}
              value={draft.base_url}
              onChange={(e) => set("base_url", e.target.value)}
            />
          </Field>
          <Field label={isCompat ? "API Key（可选，本地端点留空）" : "API Key *"}>
            <div className="relative">
              <input
                type={showKey ? "text" : "password"}
                className="field pr-9"
                placeholder="sk-..."
                value={draft.api_key}
                onChange={(e) => set("api_key", e.target.value)}
              />
              <button
                type="button"
                onClick={() => setShowKey((v) => !v)}
                aria-label={showKey ? "隐藏 API Key" : "显示 API Key"}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-faint hover:text-muted"
              >
                {showKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
          </Field>
        </div>

        {isAnthropic && (
          <label className="flex items-start gap-2 text-xs text-muted">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={!!draft.bearer_auth}
              onChange={(e) => set("bearer_auth", e.target.checked)}
            />
            <span>Bearer 认证 — 第三方 Anthropic 兼容网关用 Authorization: Bearer 代替 x-api-key</span>
          </label>
        )}

        {isResponses && (
          <Field label="Org ID（可选）" hint="官方 OpenAI 组织 id，仅 Responses API 生效">
            <input
              className="field"
              placeholder="org-..."
              value={draft.org_id ?? ""}
              onChange={(e) => set("org_id", e.target.value)}
            />
          </Field>
        )}

        {isCompat && (
          <div className="space-y-2 rounded-xl border border-line p-3">
            <div className="text-xs font-medium text-muted">兼容端点私有开关（高级）</div>
            <div className="flex flex-col gap-2">
              {(
                [
                  ["enable_search", "联网搜索 — 需要端点支持，如通义千问 compatible-mode 的 enable_search"],
                  ["enable_thinking", "思考模式 — 混合思考模型（如 Qwen3）先输出推理过程再作答，仅流式生效"],
                ] as const
              ).map(([flag, text]) => {
                const rec = recommendations.find((r) => r.flag === flag);
                return (
                  <div key={flag} className="flex items-start gap-2">
                    <label className="flex flex-1 items-start gap-2 text-xs text-muted">
                      <input
                        type="checkbox"
                        className="mt-0.5"
                        checked={!!draft[flag]}
                        onChange={(e) => set(flag, e.target.checked)}
                      />
                      <span>{text}</span>
                    </label>
                    {rec && !draft[flag] && (
                      // Q5: recommend by base_url domain, never auto-check;
                      // the pill applies the suggestion in one click.
                      <button
                        type="button"
                        onClick={() => set(flag, true)}
                        title={`${rec.why}（点击应用推荐）`}
                        className="pill shrink-0 border border-blue/50 text-blue transition-colors hover:bg-blue/10"
                      >
                        推荐
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="模型列表 *" hint="每行一个模型 id，也可用逗号分隔">
            <textarea
              className="field font-mono text-xs"
              rows={4}
              placeholder={"qwen-plus\ndeepseek-v3\nclaude-sonnet-4-5"}
              value={modelsText}
              onChange={(e) => onModelsText(e.target.value)}
            />
          </Field>
          <div className="space-y-3">
            <Field label="默认模型 *">
              {draft.models.length ? (
                <select
                  className="field"
                  value={draft.default_model}
                  onChange={(e) => set("default_model", e.target.value)}
                >
                  {!draft.models.includes(draft.default_model) && draft.default_model && (
                    <option value={draft.default_model}>{draft.default_model}（不在列表中）</option>
                  )}
                  {draft.models.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              ) : (
                <input className="field" disabled placeholder="先在左侧添加模型" />
              )}
            </Field>
            <div className="grid grid-cols-3 gap-3">
              <Field label="Max Tokens">
                <input
                  type="number"
                  className="field"
                  value={draft.max_tokens ?? ""}
                  onChange={(e) => num("max_tokens", e.target.value)}
                />
              </Field>
              <Field label="Temperature">
                <input
                  type="number"
                  step="0.1"
                  className="field"
                  value={draft.temperature ?? ""}
                  onChange={(e) => num("temperature", e.target.value)}
                />
              </Field>
              <Field label="Timeout (s)">
                <input
                  type="number"
                  className="field"
                  value={draft.timeout_s ?? ""}
                  onChange={(e) => num("timeout_s", e.target.value)}
                />
              </Field>
            </div>
          </div>
        </div>
      </div>

      {(error || refs) && (
        // Yellow bar for verify refusal / validation problems (Q8 refs list).
        <div className="mt-4 rounded-md border border-yellow/40 bg-yellow/10 px-3 py-2 text-xs text-yellow">
          {error}
          {describeRefs(refs) && <div className="mt-1">仍被引用：{describeRefs(refs)}（请先改绑后再试）</div>}
        </div>
      )}

      <div className="mt-4 flex items-center justify-end gap-2">
        <button
          onClick={onCancel}
          className="rounded-lg px-3 py-2 text-xs text-muted transition-colors hover:text-txt"
        >
          取消
        </button>
        <button
          onClick={() => void submit()}
          disabled={checking}
          className="rounded-lg bg-violet px-4 py-2 text-xs font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {checking ? "验证中…" : "验证并保存"}
        </button>
      </div>
    </div>
  );
}
