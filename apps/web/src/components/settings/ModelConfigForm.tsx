"use client";

import { useId, useMemo, useState, type ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";
import { ChevronDown, Eye, EyeOff, Loader2, X } from "lucide-react";
import * as api from "@/lib/runtime";
import { cn } from "@/lib/utils";
import type { ModelConfig, ModelProtocol } from "@/lib/types";
import {
  blankConfig,
  countNonDefaults,
  domainRecommendations,
  mergeModelIds,
  parseModelsText,
  PROTOCOLS,
  PROTOCOL_LABEL,
  reconcileDefaultModel,
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

// 折叠段标题的双语内联映射（本次重构只动本文件，未入 messages catalog；
// 后续把 tuningTitle/advancedTitle 收进 settings.model.form 后可整段删除）。
const SECTION_TITLES: Record<string, { tuning: string; advanced: string }> = {
  en: { tuning: "Tuning", advanced: "Advanced" },
  "zh-CN": { tuning: "调优", advanced: "进阶" },
};

// 折叠段容器（2026-11-04 三段折叠）：标题 +（进阶段）已改项徽标 + 旋转箭头。
// 收起时内容不渲染——字段值都活在 draft state 里，收起只是视觉收纳（纯布局），
// 不影响提交与校验。
function CollapseSection({
  title,
  badge,
  open,
  onToggle,
  children,
}: {
  title: string;
  badge?: number;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <div className="rounded-xl border border-line">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3.5 py-2.5 text-left text-xs font-medium text-muted transition-colors hover:text-txt"
      >
        <span>{title}</span>
        {!!badge && (
          <span className="rounded-full bg-violet/15 px-1.5 py-px text-[10px] font-medium leading-4 text-violet">
            {badge}
          </span>
        )}
        <ChevronDown
          className={`ml-auto h-3.5 w-3.5 shrink-0 transition-transform ${open ? "rotate-180" : ""}`}
        />
      </button>
      {open && (
        <div className="space-y-3 border-t border-line px-3.5 py-3">{children}</div>
      )}
    </div>
  );
}

export function ModelConfigForm({
  initial,
  isNew,
  embedded = false,
  onCancel,
  onSaved,
}: {
  initial: ModelConfig;
  isNew: boolean;
  // 卡片化（2026-10-17）：嵌入展开卡的编辑体不带自己的边框/内边距——外框由
  // ModelConfigCard 提供，避免卡中卡。
  embedded?: boolean;
  onCancel: () => void;
  onSaved: (cfg: ModelConfig, latencyMs: number) => void;
}) {
  const t = useTranslations("settings.model.form");
  // 与 form 平级的 key（推荐理由文案）走父命名空间 settings.model.*
  const tp = useTranslations("settings.model");
  const locale = useLocale();
  const titles = SECTION_TITLES[locale] ?? SECTION_TITLES.en;
  // 默认模型 radio 组名（同页可能同时展开多张卡的编辑体，useId 防串组）。
  const defaultRadioGroup = useId();
  // 默认模型恒在列表内（后端不变式）：存量脏值（legacy 单模型 / 手改过
  // settings.json）mount 即收敛——保留仍在列表内的值，否则落到首条。
  const [draft, setDraft] = useState<ModelConfig>(() => {
    const base = initial.protocol ? initial : blankConfig();
    return { ...base, default_model: reconcileDefaultModel(base.models, base.default_model) };
  });
  const [modelsText, setModelsText] = useState(initial.models?.join("\n") ?? "");
  const [showKey, setShowKey] = useState(false);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 验证成功的行内绿条（「连接成功」+延迟 ms）。父组件 onSaved 会收起本
  // 表单，所以绿条先展示一小段时间再交棒（否则用户根本看不到）。
  const [savedOk, setSavedOk] = useState<number | null>(null);
  // extra_body 原始文本：输入框直接绑定它（受控）。合法性在失焦时校验——
  // 输入中途的未闭合 JSON 留在框里不打断输入，draft.extra_body 只在解析
  // 成功时同步；非法时红框+行内提示（字段级，不进全局 error bar）。
  const [extraBodyText, setExtraBodyText] = useState(() =>
    initial.extra_body ? JSON.stringify(initial.extra_body) : "",
  );
  const [extraBodyInvalid, setExtraBodyInvalid] = useState(false);

  // ②调优 / ③进阶 两个折叠段的展开态（默认折叠；进阶段的已改项用徽标提示，
  // 无需记住展开状态）。
  const [tuningOpen, setTuningOpen] = useState(false);
  const [advOpen, setAdvOpen] = useState(false);
  // 进阶段「已改项」徽标：与默认值不同的进阶字段数，0 时不渲染。
  const advChanged = countNonDefaults(draft);

  // 「从 API 拉取」 panel: fetched catalogue + ticked ids + search filter.
  const [fetching, setFetching] = useState(false);
  const [fetched, setFetched] = useState<Array<{ id: string; owned_by: string | null }> | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [modelSearch, setModelSearch] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());

  // ③ 模型列表视图切换：chip 行（行内 radio 设默认 + ✕ 移除）为主视图，
  // 手动 textarea 是兜底入口。有存量列表 → 直接 chip 视图；空列表（新建 /
  // legacy 单模型配置）→ 手动起步。拉取成功 / 应用所选后切回 chip。
  const [listMode, setListMode] = useState<"chips" | "manual">(
    initial.models?.length ? "chips" : "manual",
  );

  const set = <K extends keyof ModelConfig>(key: K, value: ModelConfig[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));

  const isAsr = draft.kind === "asr";
  const isAnthropic = draft.protocol === "anthropic";
  const isCompat = draft.protocol === "openai-compatible";
  const isResponses = draft.protocol === "openai-responses";
  // ①思考强度联动：能力声明的生效档位（缺省 auto）。auto → 三个思考控件
  // 隐藏只留状态行；budget/effort → 只展开对应控件；none → 状态行。
  const thinkingMode = draft.thinking_mode ?? "auto";

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
        id: draft.id, // 反掩码靠 id 找回存量 key（2026-10-29：不带 id 时
        // 掩码 •••• 原样出网，header ASCII 编码处炸 UnicodeEncodeError）
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
        // 拉取成功 → 模型列表升级为 chip 视图（手动编辑随时可切回）。
        setListMode("chips");
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
    setDraft((d) => ({
      ...d,
      models,
      // Backend invariant: default_model ∈ models (reconcileDefaultModel).
      default_model: reconcileDefaultModel(models, d.default_model),
    }));
  };

  // chip 行移除：列表与默认模型一并收敛（默认被移除 → 落到剩余首条；清空
  // → ""，校验用 vModels 兜底）。同步 modelsText 让「手动编辑」视图同源。
  const removeModel = (id: string) => {
    const models = draft.models.filter((m) => m !== id);
    setModelsText(models.join("\n"));
    setDraft((d) => ({ ...d, models, default_model: reconcileDefaultModel(models, d.default_model) }));
  };

  // Blank input = omit the field so the backend applies its per-protocol
  // default; temperature 0 is a literal greedy decode, not "default".
  const num = (key: "max_tokens" | "timeout_s" | "temperature" | "context_window", v: string) => {
    const n = v === "" ? undefined : Number(v);
    setDraft((d) => ({ ...d, [key]: n != null && Number.isFinite(n) ? n : undefined }));
  };

  // extra_body 失焦/提交校验：空串或合法 JSON → 放过并同步 draft；非法 →
  // 字段级红框（返回 false）。submit 复用同一入口兜底，避免把框里的非法
  // 文本静默丢弃后存走旧值。
  const validateExtraBody = (): boolean => {
    const raw = extraBodyText.trim();
    if (!raw) {
      set("extra_body", undefined);
      setExtraBodyInvalid(false);
      return true;
    }
    try {
      set("extra_body", JSON.parse(raw));
      setExtraBodyInvalid(false);
      return true;
    } catch {
      setExtraBodyInvalid(true);
      return false;
    }
  };

  const validate = (): string | null => {
    if (!draft.name.trim()) return t("vName");
    if (isCompat && !draft.base_url.trim()) return t("vBaseUrl");
    if (!isCompat && !draft.api_key.trim()) return t("vApiKey");
    if (isAsr && !draft.api_key.trim()) return t("vApiKey");
    if (!draft.models.length) return t("vModels");
    if (!draft.default_model || !draft.models.includes(draft.default_model))
      return t("vDefaultModel");
    return null;
  };

  // Q4: verify against the DRAFT (no save-then-verify dance); a pass is
  // already persisted server-side and the response carries the saved record.
  const submit = async () => {
    if (checking || savedOk != null) return; // 防重复提交（按钮已禁用，双保险）
    setError(null);
    if (!validateExtraBody()) return; // 行内红框已提示，不进全局 bar
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
        // 行内绿条先亮 ~0.9s 再交给父组件收起表单（父级还有自己的顶部
        // notice）；期间按钮保持禁用，防止绿条窗口内二次提交。
        setSavedOk(r.latency_ms ?? null);
        window.setTimeout(() => onSaved(r.config, r.latency_ms), 900);
        return;
      }
      setError(r.error || t("verifyUnknownError"));
    } catch {
      setError(t("connError"));
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className={embedded ? "" : "rounded-2xl border border-indigo/40 bg-card p-5"}>
      <div className="mb-4 text-sm font-semibold text-txt">
        {isNew ? t("addTitle") : t("editTitle", { name: initial.name || initial.id })}
      </div>

      <div className="space-y-3">
        {/* 类型：对话模型 vs 语音识别。asr 固定走 OpenAI 兼容端点
            （chat/completions + input_audio），协议选择随之隐藏。 */}
        <div>
          <label className="field-label">{t("kindLabel")}</label>
          <div className="flex max-w-sm rounded-lg border border-line p-0.5">
            {(["chat", "asr"] as const).map((k) => (
              <button
                key={k}
                type="button"
                onClick={() => {
                  set("kind", k);
                  if (k === "asr") set("protocol", "openai-compatible");
                }}
                className={`flex-1 rounded-md px-2 py-1.5 text-xs transition-colors ${
                  (draft.kind ?? "chat") === k ? "bg-card2 text-txt" : "text-muted hover:text-txt"
                }`}
                style={(draft.kind ?? "chat") === k && k === "asr" ? { color: "#14b8a6" } : undefined}
              >
                {t(k === "chat" ? "kindChat" : "kindAsr")}
              </button>
            ))}
          </div>
          {isAsr && <p className="mt-1 text-[11px] text-faint">{t("asrHint")}</p>}
        </div>

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
          {!isAsr && (
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
          )}
          {isAsr && (
            <div className="sm:col-span-2 self-end text-[11px] text-faint">
              {t("asrProtocolNote")}
            </div>
          )}
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

        {/* bearer_auth 属连接凭据（Anthropic 网关是否走 Bearer 头），留在
            基础段紧邻密钥；server_web_search 面板移入「进阶」折叠段。 */}
        {isAnthropic && (
          <label className="flex items-start gap-2 text-xs text-muted">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={!!draft.bearer_auth}
              onChange={(e) => set("bearer_auth", e.target.checked)}
            />
            <span>{t("bearerAuth")}</span>
          </label>
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

        {/* ②模型列表（2026-11-04 重构）：主视图 = chip 行（行内 radio 设默认
            + ✕ 移除），「手动编辑」切回 textarea 兜底。独立「默认模型」输入
            框已删——默认模型只能从列表行点选，恒在列表内（reconcileDefaultModel）。 */}
        <Field
          label={t("modelsLabel")}
          hint={listMode === "chips" ? t("defaultModelHint") : t("modelsHint")}
        >
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
            <button
              type="button"
              onClick={() => setListMode((m) => (m === "chips" ? "manual" : "chips"))}
              className="pill shrink-0 border border-line text-muted transition-colors hover:text-txt"
            >
              {listMode === "chips" ? t("manualEdit") : t("listView")}
            </button>
            {!canFetchModels && (
              <span className="text-[11px] text-faint">{t("fetchNeedKeys")}</span>
            )}
          </div>
          {fetchError && (
            <div className="selectable mb-2 rounded-md border border-yellow/40 bg-yellow/10 px-3 py-2 text-xs text-yellow">
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
            {listMode === "chips" ? (
              // chip 视图：一行一模型——radio 行内点选默认 + ✕ 移除。默认模型
              // 只能出自这里（default_model ∈ models 恒成立，移除默认行自动
              // 落到剩余首条）。
              <div className="rounded-xl border border-line">
                {draft.models.map((m) => (
                  <div
                    key={m}
                    className="flex items-center gap-2 border-b border-line px-3 py-1.5 last:border-b-0"
                  >
                    <label className="flex min-w-0 flex-1 cursor-pointer items-center gap-2 text-xs">
                      <input
                        type="radio"
                        name={defaultRadioGroup}
                        aria-label={t("setDefaultAria", { model: m })}
                        checked={draft.default_model === m}
                        onChange={() => set("default_model", m)}
                      />
                      <span className="truncate font-mono">{m}</span>
                      {draft.default_model === m && (
                        <span className="pill shrink-0 border border-violet/50 text-violet">
                          {t("defaultBadge")}
                        </span>
                      )}
                    </label>
                    <button
                      type="button"
                      onClick={() => removeModel(m)}
                      aria-label={t("removeModelAria", { model: m })}
                      className="shrink-0 text-faint transition-colors hover:text-txt"
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                ))}
                {!draft.models.length && (
                  <div className="px-3 py-3 text-center text-xs text-faint">
                    {t("emptyModelsHint")}
                  </div>
                )}
              </div>
            ) : (
              <>
                <div className="mb-1 text-[11px] text-faint">{t("manualHint")}</div>
                <textarea
                  className="field font-mono text-xs"
                  rows={4}
                  placeholder={isAsr ? "qwen3-asr-flash" : "qwen-plus\ndeepseek-v3\nclaude-sonnet-4-5"}
                  value={modelsText}
                  onChange={(e) => onModelsText(e.target.value)}
                />
              </>
            )}
          </Field>

        {/* ② 模型行为 —— 数值调优（chat：最大 Token / 上下文窗口 / 温度 / 超时；
            asr：仅超时）。默认折叠；值都在 draft 里，收起不影响提交与校验。 */}
        <CollapseSection
          title={titles.tuning}
          open={tuningOpen}
          onToggle={() => setTuningOpen((v) => !v)}
        >
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {!isAsr && (
              <Field label={t("maxTokens")}>
                <input
                  type="number"
                  className="field"
                  value={draft.max_tokens ?? ""}
                  onChange={(e) => num("max_tokens", e.target.value)}
                />
              </Field>
            )}
            {!isAsr && (
              <Field label={t("contextWindowLabel")} hint={t("contextWindowHint")}>
                <input
                  type="number"
                  className="field"
                  placeholder="200000"
                  value={draft.context_window ?? ""}
                  onChange={(e) => num("context_window", e.target.value)}
                />
              </Field>
            )}
            {!isAsr && (
              <Field label={t("temperature")}>
                <input
                  type="number"
                  step="0.1"
                  className="field"
                  value={draft.temperature ?? ""}
                  onChange={(e) => num("temperature", e.target.value)}
                />
              </Field>
            )}
            <Field label={t("timeoutS")}>
              <input
                type="number"
                className="field"
                value={draft.timeout_s ?? ""}
                onChange={(e) => num("timeout_s", e.target.value)}
              />
            </Field>
          </div>
        </CollapseSection>

        {/* ③ 进阶 —— 协议私有开关 / 思考强度区 / extra_body；徽标 = 与默认
            值不同的项数（countNonDefaults），0 时不显示。 */}
        <CollapseSection
          title={titles.advanced}
          badge={advChanged}
          open={advOpen}
          onToggle={() => setAdvOpen((v) => !v)}
        >
          {isAnthropic && (
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
          )}

          {isCompat && !isAsr && (
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

          {!isAsr && (
            // ①思考强度联动(thinking-effort-design §7/§9):能力声明 auto(默认)
            // → 预算三档/参数风格/extra_body 三控件隐藏,只留一行状态说明;
            // 显式选具体档位才展开对应控件(budget→预算,effort→参数风格,
            // 两者都带 extra_body 逃生口);none = 菜单置灰 + 服务端把档位按
            // auto 处理。隐藏的字段值仍留在 draft 里(视觉收纳,payload 不变)。
            <div className="space-y-2 rounded-xl border border-line p-3">
              <div className="text-xs font-medium text-muted">{t("thinkingCapabilityTitle")}</div>
              <div className="max-w-sm">
                <label className="field-label">{t("thinkingModeLabel")}</label>
                <select
                  className="field"
                  value={thinkingMode}
                  onChange={(e) =>
                    set("thinking_mode", e.target.value as ModelConfig["thinking_mode"])
                  }
                >
                  <option value="auto">{t("thinkingModeAuto")}</option>
                  <option value="budget">{t("thinkingModeBudget")}</option>
                  <option value="effort">{t("thinkingModeEffort")}</option>
                  <option value="none">{t("thinkingModeNone")}</option>
                </select>
                {thinkingMode === "auto" ? (
                  <p className="mt-0.5 text-[11px] leading-relaxed text-faint">
                    {t("thinkingAutoStatus")}
                  </p>
                ) : thinkingMode === "none" ? (
                  <p className="mt-0.5 text-[11px] leading-relaxed text-faint">
                    {t("thinkingNoneStatus")}
                  </p>
                ) : (
                  <p className="mt-0.5 text-[11px] leading-relaxed text-faint">
                    {t("thinkingModeHint")}
                  </p>
                )}
              </div>
              {thinkingMode === "budget" && (
                <div>
                  <label className="field-label">{t("thinkingBudgetsLabel")}</label>
                  <div className="flex max-w-md items-center gap-1.5">
                    {(["low", "medium", "high"] as const).map((k) => (
                      <input
                        key={k}
                        type="number"
                        min={0}
                        step={1024}
                        className="field w-full"
                        placeholder={t(`thinkingBudget_${k}`)}
                        value={draft.thinking_budgets?.[k] ?? ""}
                        onChange={(e) =>
                          set("thinking_budgets", {
                            ...(draft.thinking_budgets ?? {}),
                            [k]: Number(e.target.value) || 0,
                          })
                        }
                      />
                    ))}
                  </div>
                  <p className="mt-0.5 text-[11px] leading-relaxed text-faint">
                    {t("thinkingBudgetsHint")}
                  </p>
                </div>
              )}
              {thinkingMode === "effort" && (
                <div className="max-w-sm">
                  {/* 参数风格覆写（2026-11-04 Magpie 实案）：Qwen 套 Anthropic 壳
                      的网关只吃 reasoning_effort，thinking 参数必 400。 */}
                  <label className="field-label">{t("thinkingParamStyleLabel")}</label>
                  <select
                    className="field"
                    value={draft.thinking_param_style ?? "auto"}
                    onChange={(e) =>
                      set("thinking_param_style", e.target.value as ModelConfig["thinking_param_style"])
                    }
                  >
                    <option value="auto">{t("thinkingParamStyleAuto")}</option>
                    <option value="reasoning_effort">{t("thinkingParamStyleEffort")}</option>
                  </select>
                  <p className="mt-0.5 text-[11px] leading-relaxed text-faint">
                    {t("thinkingParamStyleHint")}
                  </p>
                </div>
              )}
              {(thinkingMode === "budget" || thinkingMode === "effort") && (
                <div className="max-w-sm">
                  <label className="field-label">{t("extraBodyLabel")}</label>
                  <input
                    className={cn(
                      "field font-mono text-[11px]",
                      extraBodyInvalid && "border-red",
                    )}
                    placeholder={t("extraBodyPlaceholder")}
                    value={extraBodyText}
                    onChange={(e) => {
                      const raw = e.target.value;
                      setExtraBodyText(raw);
                      const trimmed = raw.trim();
                      if (!trimmed) {
                        set("extra_body", undefined);
                        setExtraBodyInvalid(false);
                        return;
                      }
                      try {
                        set("extra_body", JSON.parse(trimmed));
                        setExtraBodyInvalid(false);
                      } catch {
                        /* 未闭合 JSON：留在框里不打断输入，失焦/提交时由
                           validateExtraBody 兜底 */
                      }
                    }}
                    onBlur={validateExtraBody}
                  />
                  {extraBodyInvalid && (
                    <p className="mt-0.5 text-[11px] text-red">{t("extraBodyInvalid")}</p>
                  )}
                </div>
              )}
            </div>
          )}
        </CollapseSection>
      </div>

      {savedOk != null && (
        // Green bar for a passing verify (the record is already persisted —
        // onSaved fires ~0.9s later and collapses the form).
        <div className="selectable mt-4 rounded-md border border-green/40 bg-green/10 px-3 py-2 text-xs text-green">
          {t("verifyOk", { ms: savedOk })}
        </div>
      )}

      {error && (
        // Yellow bar for verify failures / validation problems.
        <div className="selectable mt-4 rounded-md border border-yellow/40 bg-yellow/10 px-3 py-2 text-xs text-yellow">
          {error}
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
          disabled={checking || savedOk != null}
          className="inline-flex items-center gap-1.5 rounded-lg bg-violet px-4 py-2 text-xs font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {checking && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          {checking ? t("verifying") : t("verifySave")}
        </button>
      </div>
    </div>
  );
}
