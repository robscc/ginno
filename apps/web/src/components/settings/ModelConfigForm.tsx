"use client";

import { useMemo, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { Eye, EyeOff, Loader2 } from "lucide-react";
import * as api from "@/lib/runtime";
import type { ModelConfig, ModelConfigRefs, ModelProtocol } from "@/lib/types";
import {
  blankConfig,
  describeRefParts,
  domainRecommendations,
  formatRefParts,
  mergeModelIds,
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
  const t = useTranslations("settings.model.form");
  // 引用计数等与 form 平级的 key 走父命名空间（settings.model.ref.*）
  const tp = useTranslations("settings.model");
  const [draft, setDraft] = useState<ModelConfig>(initial.protocol ? initial : blankConfig());
  const [modelsText, setModelsText] = useState(initial.models?.join("\n") ?? "");
  const [showKey, setShowKey] = useState(false);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refs, setRefs] = useState<ModelConfigRefs | undefined>(undefined);

  // 「从 API 拉取」 panel: fetched catalogue + ticked ids + search filter.
  const [fetching, setFetching] = useState(false);
  const [fetched, setFetched] = useState<Array<{ id: string; owned_by: string | null }> | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [modelSearch, setModelSearch] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());

  const set = <K extends keyof ModelConfig>(key: K, value: ModelConfig[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));

  const isAnthropic = draft.protocol === "anthropic";
  const isCompat = draft.protocol === "openai-compatible";
  const isResponses = draft.protocol === "openai-responses";

  const recommendations = useMemo(
    () => (isCompat ? domainRecommendations(draft.base_url) : []),
    [isCompat, draft.base_url],
  );

  const canFetchModels = !!(draft.protocol && draft.base_url.trim() && draft.api_key.trim());

  const filteredFetched = useMemo(() => {
    const q = modelSearch.trim().toLowerCase();
    return (fetched ?? []).filter((m) => m.id.toLowerCase().includes(q));
  }, [fetched, modelSearch]);

  const fetchModels = async () => {
    setFetchError(null);
    setFetching(true);
    try {
      const r = await api.listModelsForConfig({
        protocol: draft.protocol,
        base_url: draft.base_url,
        api_key: draft.api_key,
        org_id: draft.org_id,
        bearer_auth: draft.bearer_auth,
      });
      if (r.ok) {
        setFetched(r.models);
        // models already in the list start checked (merging them is a no-op)
        setPicked(new Set(draft.models));
        setModelSearch("");
      } else {
        setFetchError(r.error || t("fetchUnknownError"));
      }
    } catch {
      setFetchError(t("connError"));
    } finally {
      setFetching(false);
    }
  };

  const applyPicked = () => {
    if (!picked.size) return;
    // route through onModelsText so the default_model fallback rule applies
    onModelsText(mergeModelIds(draft.models, [...picked]).join("\n"));
    setFetched(null);
    setPicked(new Set());
  };

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
  const num = (key: "max_tokens" | "timeout_s" | "temperature" | "context_window", v: string) => {
    const n = v === "" ? undefined : Number(v);
    setDraft((d) => ({ ...d, [key]: n != null && Number.isFinite(n) ? n : undefined }));
  };

  const validate = (): string | null => {
    if (!draft.name.trim()) return t("vName");
    if (isCompat && !draft.base_url.trim()) return t("vBaseUrl");
    if (!isCompat && !draft.api_key.trim()) return t("vApiKey");
    if (!draft.models.length) return t("vModels");
    if (!draft.default_model || !draft.models.includes(draft.default_model))
      return t("vDefaultModel");
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
      setError(r.error || t("verifyUnknownError"));
      setRefs(r.refs);
    } catch {
      setError(t("connError"));
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="rounded-2xl border border-indigo/40 bg-card p-5">
      <div className="mb-4 text-sm font-semibold text-txt">
        {isNew ? t("addTitle") : t("editTitle", { name: initial.name || initial.id })}
      </div>

      <div className="space-y-3">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label={t("nameLabel")}>
            <input
              className="field"
              placeholder={t("namePlaceholder")}
              value={draft.name}
              onChange={(e) => set("name", e.target.value)}
              autoFocus={isNew}
            />
          </Field>
          <div className="sm:col-span-2">
            <label className="field-label">{t("protocolLabel")}</label>
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
            {isCompat && <p className="mt-1 text-[11px] text-faint">{t("compatHint")}</p>}
          </div>
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field
            label={isCompat ? t("baseUrlLabelRequired") : t("baseUrlLabelOptional")}
            hint={isResponses ? t("baseUrlHintResponses") : isAnthropic ? t("baseUrlHintAnthropic") : undefined}
          >
            <input
              className="field"
              placeholder={isAnthropic ? "https://api.anthropic.com" : isResponses ? "https://api.openai.com/v1" : "https://api.deepseek.com/v1"}
              value={draft.base_url}
              onChange={(e) => set("base_url", e.target.value)}
            />
          </Field>
          <Field label={isCompat ? t("apiKeyLabelOptional") : t("apiKeyLabelRequired")}>
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
                aria-label={showKey ? t("hideKeyAria") : t("showKeyAria")}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-faint hover:text-muted"
              >
                {showKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
          </Field>
        </div>

        {isAnthropic && (
          <>
            <label className="flex items-start gap-2 text-xs text-muted">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={!!draft.bearer_auth}
                onChange={(e) => set("bearer_auth", e.target.checked)}
              />
              <span>{t("bearerAuth")}</span>
            </label>

            <div className="space-y-2 rounded-xl border border-line p-3">
              <label className="flex items-start gap-2 text-xs text-muted">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={!!draft.server_web_search}
                  onChange={(e) => set("server_web_search", e.target.checked)}
                />
                <span>{t("serverWebSearch")}</span>
              </label>
              {draft.server_web_search && (
                <div className="flex items-center gap-2 pl-5 text-xs text-muted">
                  <span className="whitespace-nowrap">{t("serverWebSearchMaxUses")}</span>
                  <input
                    type="number"
                    className="field w-20"
                    min={1}
                    max={20}
                    value={draft.server_web_search_max_uses ?? 5}
                    onChange={(e) =>
                      set("server_web_search_max_uses", Number(e.target.value) || 5)
                    }
                  />
                </div>
              )}
              <p className="text-[11px] leading-relaxed text-faint">
                {t("serverWebSearchHint")}
              </p>
            </div>
          </>
        )}

        {isResponses && (
          <Field label={t("orgIdLabel")} hint={t("orgIdHint")}>
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
            <div className="text-xs font-medium text-muted">{t("compatSwitchesTitle")}</div>
            <div className="flex flex-col gap-2">
              {(
                [
                  ["enable_search", t("enableSearchHint")],
                  ["enable_thinking", t("enableThinkingHint")],
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
                        title={t("recommendedTitle", { why: tp(`recommendWhy.${rec.whyKey}`) })}
                        className="pill shrink-0 border border-blue/50 text-blue transition-colors hover:bg-blue/10"
                      >
                        {t("recommended")}
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label={t("modelsLabel")} hint={t("modelsHint")}>
            <div className="mb-2 flex items-center gap-2">
              <button
                type="button"
                onClick={() => void fetchModels()}
                disabled={!canFetchModels || fetching}
                className="pill inline-flex shrink-0 items-center gap-1.5 border border-blue/50 text-blue transition-colors hover:bg-blue/10 disabled:opacity-50"
              >
                {fetching && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                {fetching ? t("fetching") : t("fetchModels")}
              </button>
              {!canFetchModels && (
                <span className="text-[11px] text-faint">{t("fetchNeedKeys")}</span>
              )}
            </div>
            {fetchError && (
              <div className="mb-2 rounded-md border border-yellow/40 bg-yellow/10 px-3 py-2 text-xs text-yellow">
                {t("fetchFailed", { error: fetchError })}
              </div>
            )}
            {fetched && (
              // Ticker-style picker over the provider's catalogue.
              <div className="mb-2 space-y-2 rounded-xl border border-line p-3">
                <div className="flex items-center gap-2">
                  <input
                    className="field flex-1"
                    placeholder={t("searchModelsPlaceholder")}
                    value={modelSearch}
                    onChange={(e) => setModelSearch(e.target.value)}
                  />
                  <button
                    type="button"
                    onClick={() => {
                      setFetched(null);
                      setPicked(new Set());
                    }}
                    className="shrink-0 rounded-lg px-3 py-2 text-xs text-muted transition-colors hover:text-txt"
                  >
                    {t("cancel")}
                  </button>
                </div>
                <div className="max-h-56 overflow-y-auto rounded-lg border border-line">
                  {filteredFetched.map((m) => (
                    <label
                      key={m.id}
                      className="flex cursor-pointer items-center gap-2 px-3 py-1.5 text-xs hover:bg-card2"
                    >
                      <input
                        type="checkbox"
                        checked={picked.has(m.id)}
                        onChange={(e) =>
                          setPicked((p) => {
                            const n = new Set(p);
                            if (e.target.checked) n.add(m.id);
                            else n.delete(m.id);
                            return n;
                          })
                        }
                      />
                      <span className="truncate font-mono">{m.id}</span>
                      {m.owned_by && (
                        <span className="ml-auto shrink-0 text-[11px] text-faint">{m.owned_by}</span>
                      )}
                    </label>
                  ))}
                  {!filteredFetched.length && (
                    <div className="px-3 py-3 text-center text-xs text-faint">{t("noMatching")}</div>
                  )}
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-[11px] text-faint">
                    {t("fetchedStats", { total: filteredFetched.length, selected: picked.size })}
                  </span>
                  <button
                    type="button"
                    onClick={applyPicked}
                    disabled={!picked.size}
                    className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
                  >
                    {t("addSelected", { count: picked.size })}
                  </button>
                </div>
              </div>
            )}
            <div className="mb-1 text-[11px] text-faint">{t("manualHint")}</div>
            <textarea
              className="field font-mono text-xs"
              rows={4}
              placeholder={"qwen-plus\ndeepseek-v3\nclaude-sonnet-4-5"}
              value={modelsText}
              onChange={(e) => onModelsText(e.target.value)}
            />
          </Field>
          <div className="space-y-3">
            <Field label={t("defaultModelLabel")}>
              {draft.models.length > 0 && draft.models.length <= 12 ? (
                // Short list → a select. The current value is added as an option
                // when it is not in the list, so a mismatch is visible and
                // fixable instead of being unrepresentable.
                <select
                  className="field"
                  value={draft.default_model}
                  onChange={(e) => set("default_model", e.target.value)}
                >
                  {!draft.models.includes(draft.default_model) && draft.default_model && (
                    <option value={draft.default_model}>{draft.default_model} {t("notInListSuffix")}</option>
                  )}
                  {draft.models.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              ) : (
                // Free text, for a LONG list and for NO list at all. The empty
                // case is a legacy config (it stores only `model`, no `models[]`)
                // — such a provider still HAS a model (the chat works, and the
                // server synthesizes the list from `model`), so this input must
                // stay editable. It used to be `disabled` here, which left the
                // default model of every legacy-config provider permanently
                // unsettable ("默认模型不能改"). Native datalist keeps it
                // dependency-free.
                <div>
                  <input
                    className="field font-mono text-xs"
                    list="mc-default-model-options"
                    value={draft.default_model}
                    onChange={(e) => set("default_model", e.target.value)}
                    placeholder={t("modelIdPlaceholder")}
                  />
                  <datalist id="mc-default-model-options">
                    {draft.models.map((m) => (
                      <option key={m} value={m} />
                    ))}
                  </datalist>
                  {draft.models.length === 0 ? (
                    <p className="mt-1 text-[11px] text-faint">{t("noModelListNote")}</p>
                  ) : (
                    !draft.models.includes(draft.default_model) &&
                    draft.default_model && (
                      <p className="mt-1 text-[11px] text-yellow">
                        {t("notInListModel", { model: draft.default_model })}
                      </p>
                    )
                  )}
                </div>
              )}
            </Field>
            <div className="grid grid-cols-3 gap-3">
              <Field label={t("maxTokens")}>
                <input
                  type="number"
                  className="field"
                  value={draft.max_tokens ?? ""}
                  onChange={(e) => num("max_tokens", e.target.value)}
                />
              </Field>
              <Field label={t("contextWindowLabel")} hint={t("contextWindowHint")}>
                <input
                  type="number"
                  className="field"
                  placeholder="200000"
                  value={draft.context_window ?? ""}
                  onChange={(e) => num("context_window", e.target.value)}
                />
              </Field>
              <Field label={t("temperature")}>
                <input
                  type="number"
                  step="0.1"
                  className="field"
                  value={draft.temperature ?? ""}
                  onChange={(e) => num("temperature", e.target.value)}
                />
              </Field>
              <Field label={t("timeoutS")}>
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
          {describeRefParts(refs).length > 0 && (
            <div className="mt-1">
              {t("stillReferenced", {
                refs: formatRefParts(describeRefParts(refs), (key, n) =>
                  tp(`ref.${key}`, { count: n }),
                ),
              })}
            </div>
          )}
        </div>
      )}

      <div className="mt-4 flex items-center justify-end gap-2">
        <button
          onClick={onCancel}
          className="rounded-lg px-3 py-2 text-xs text-muted transition-colors hover:text-txt"
        >
          {t("cancel")}
        </button>
        <button
          onClick={() => void submit()}
          disabled={checking}
          className="rounded-lg bg-violet px-4 py-2 text-xs font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {checking ? t("verifying") : t("verifySave")}
        </button>
      </div>
    </div>
  );
}
