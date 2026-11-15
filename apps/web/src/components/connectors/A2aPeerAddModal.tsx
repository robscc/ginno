"use client";

/* A2A 接入外部 Agent 向导（a2a-connector-design.md §6.1 四步）：
 * 1 粘贴地址 → 2 Card 预览（第三方信任提示必须出现）→ 3 凭据 → 4 确认。
 *
 * 管理 API 契约（后端集成轮落地；未就绪时优雅降级）：
 *   POST /api/connectors/a2a/action
 *     action="a2a_preview_card" {url}  → {ok, card, missing?} | {ok:false, error}
 *     action="a2a_add_peer"     {endpoint,name,peer_fingerprint,auth_scheme,
 *                                token,trust,caps,confirm_mode} → {ok,id,slug}
 * 后端未实现该 action 时返回 {error:"unknown action…"}——预览失败走
 * 「未发布 Agent Card」手动填表兜底（设计 §6.1 第 1 步失败态本就要求）。
 *
 * i18n：a2a 域已正式注册（messages/{en,zh-CN}/a2a.json + global.d.ts +
 * provider.tsx 三处同步），组件用 useTranslations("a2a")——namespace/key
 * 拼错会直接编译失败。key 全部扁平连字符（无点）——仓库曾有带点 key 炸
 * IntlError 的教训；动态 key（auth-*）走字面量映射表保持静态可检查。
 *
 * 纯逻辑（parsePeerAddress/cardMissingFields/…）全部导出，node 环境
 * vitest 直接测；渲染层验证留给 packaged UI e2e（集成轮补）。 */

import { useCallback, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslations } from "next-intl";
import { Check, ChevronLeft, Loader2, ShieldAlert, X } from "lucide-react";
import { connectorAction } from "@/lib/api/connectors";
import { useEscClose } from "@/lib/useEscClose";

export const A2A_HUB_ID = "a2a";

// ---- 地址解析（前端镜像 a2a/address.py parse_address，语义逐条对齐） ----

export type ParsedPeerAddress = {
  ok: true;
  /** 消息端点：永远不含 fragment（#fp 只在本地校验用）。 */
  endpoint: string;
  token: string;
  fingerprint: string;
  oneTime: boolean;
};
export type AddressParseError = {
  ok: false;
  error: "empty" | "scheme" | "host" | "token" | "fingerprint";
};

/** 26 字符 base32（A-Z2-7，大小写宽容、原样保留——token 是凭据）。 */
const B32_26 = /^[A-Za-z2-7]{26}$/;
const CAP_PATH = /^\/a2a\/([^/]+)\/?$/;

export function parsePeerAddress(input: string): ParsedPeerAddress | AddressParseError {
  let raw = (input || "").trim();
  if (!raw) return { ok: false, error: "empty" };
  if (!raw.includes("://")) {
    // 裸域名补 https；"javascript:xxx" 这类伪 scheme 不能当域名补——
    // 保留原样让下面的 scheme 检查拒绝（与后端 _split 同款）。
    if (!raw.split("/", 1)[0].includes(":")) raw = "https://" + raw;
  }
  let u: URL;
  try {
    u = new URL(raw);
  } catch {
    return { ok: false, error: "host" };
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") {
    return { ok: false, error: "scheme" };
  }
  if (!u.hostname) return { ok: false, error: "host" };
  const origin = `${u.protocol}//${u.host}`;
  let endpoint = origin;
  let token = "";
  const m = CAP_PATH.exec(u.pathname);
  if (m) {
    token = m[1];
    if (!B32_26.test(token)) return { ok: false, error: "token" };
    endpoint = `${origin}/a2a/${token}`;
  } else if (u.pathname && u.pathname !== "/") {
    endpoint = origin + u.pathname;
  }
  if (u.search && endpoint !== origin) endpoint += u.search;

  let fingerprint = "";
  let oneTime = false;
  if (u.hash) {
    const params = new URLSearchParams(u.hash.slice(1));
    const fp = params.get("fp");
    if (fp !== null) {
      if (!B32_26.test(fp)) return { ok: false, error: "fingerprint" };
      fingerprint = fp;
    }
    if (params.get("ot") === "1") oneTime = true;
  }
  return { ok: true, endpoint, token, fingerprint, oneTime };
}

// ---- Card 校验与鉴权映射（镜像 a2a/card.py validate_card / models.py） ----

export type AuthScheme = "none" | "bearer" | "api_key" | "oauth2";
export type TrustLevel = "readonly" | "normal" | "full";

/** 缺字段清单（与后端 validate_card 同口径）；空数组 = 完整。 */
export function cardMissingFields(card: Record<string, unknown> | null | undefined): string[] {
  const c = card ?? {};
  const missing: string[] = [];
  const nonEmptyStr = (k: string) => typeof c[k] === "string" && (c[k] as string).trim() !== "";
  if (!nonEmptyStr("name")) missing.push("name");
  if (!nonEmptyStr("url")) missing.push("url");
  if (!nonEmptyStr("description")) missing.push("description");
  if (!nonEmptyStr("version")) missing.push("version");
  const caps = c.capabilities;
  if (!caps || typeof caps !== "object" || Object.keys(caps as object).length === 0) {
    missing.push("capabilities");
  }
  if (!Array.isArray(c.skills) || c.skills.length === 0) missing.push("skills");
  return missing;
}

/** OpenAPI 风格 securitySchemes → 本连接器 auth_scheme 枚举。 */
export function authSchemesFromCard(card: Record<string, unknown> | null | undefined): AuthScheme[] {
  const found = new Set<AuthScheme>();
  const ss = card?.securitySchemes;
  if (ss && typeof ss === "object") {
    for (const v of Object.values(ss as Record<string, unknown>)) {
      if (!v || typeof v !== "object") continue;
      const s = v as Record<string, unknown>;
      const type = String(s.type ?? "").toLowerCase();
      if (type === "http" && String(s.scheme ?? "").toLowerCase() === "bearer") {
        found.add("bearer");
      } else if (type === "apikey") {
        found.add("api_key");
      } else if (type === "oauth2") {
        found.add("oauth2");
      }
    }
  }
  if (found.size === 0) found.add("none");
  return [...found];
}

/** 能力地址已含 token → 跳过凭据步（设计 §6.1 第 3 步）。 */
export function credentialsStepNeeded(parsed: ParsedPeerAddress): boolean {
  return !parsed.token;
}

/** 第 4 步「确认接入」提交的 payload（endpoint 永远不带 fragment）。 */
export function buildPeerPayload(opts: {
  parsed: ParsedPeerAddress;
  name: string;
  authScheme: AuthScheme;
  token: string;
  trust: TrustLevel;
}): Record<string, unknown> {
  return {
    endpoint: opts.parsed.endpoint,
    name: opts.name.trim(),
    peer_fingerprint: opts.parsed.fingerprint,
    auth_scheme: opts.authScheme,
    token: opts.token,
    trust: opts.trust,
    caps: ["L0"],
    confirm_mode: "every",
  };
}

// ---- 管理 API（走既有 connector action 通道；后端未就绪时返回 {error}） ----

export interface A2aCardPreview {
  ok?: boolean;
  card?: Record<string, unknown>;
  missing?: string[];
  error?: string;
}

export async function previewA2aCard(url: string): Promise<A2aCardPreview> {
  const r = await connectorAction(A2A_HUB_ID, "a2a_preview_card", { url });
  return r as A2aCardPreview;
}

export interface A2aAddPeerResult {
  ok?: boolean;
  id?: string;
  slug?: string;
  error?: string;
}

export async function addA2aPeer(payload: Record<string, unknown>): Promise<A2aAddPeerResult> {
  const r = await connectorAction(A2A_HUB_ID, "a2a_add_peer", payload);
  return r as A2aAddPeerResult;
}

// ---- 向导组件 --------------------------------------------------------------

const AUTH_OPTIONS: AuthScheme[] = ["none", "bearer", "api_key", "oauth2"];
const TRUST_OPTIONS: TrustLevel[] = ["readonly", "normal", "full"];

/** auth_scheme → i18n key 的字面量映射：replace 拼 key 会退化成 string，
 * 映射表让 useTranslations 的 key 类型检查保持静态可验证。 */
const AUTH_LABEL_KEY: Record<
  AuthScheme,
  "auth-none" | "auth-bearer" | "auth-api-key" | "auth-oauth2"
> = {
  none: "auth-none",
  bearer: "auth-bearer",
  api_key: "auth-api-key",
  oauth2: "auth-oauth2",
};

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

export function A2aPeerAddModal({
  onClose,
  onAdded,
}: {
  onClose: () => void;
  /** 接入成功：父组件刷新连接器列表并提示。 */
  onAdded: (name: string) => void;
}) {
  const t = useTranslations("a2a");
  const [step, setStep] = useState(1);
  // 向导防误触：仅第一步允许 Esc 关闭（照 McpAddWizard 惯例）。
  useEscClose(onClose, { guard: () => step > 1 });

  const [address, setAddress] = useState("");
  const [addrError, setAddrError] = useState<string | null>(null);
  const [parsed, setParsed] = useState<ParsedPeerAddress | null>(null);

  const [cardLoading, setCardLoading] = useState(false);
  const [card, setCard] = useState<Record<string, unknown> | null>(null);
  const [cardFailed, setCardFailed] = useState(false);
  const [manualName, setManualName] = useState("");
  const [nameError, setNameError] = useState(false);

  const [authScheme, setAuthScheme] = useState<AuthScheme>("none");
  const [token, setToken] = useState("");
  const [trust, setTrust] = useState<TrustLevel>("readonly");

  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  const cardName = str(card?.name);
  const displayName = cardName || manualName.trim();

  const fetchPreview = useCallback(async (raw: string) => {
    setCardLoading(true);
    setCardFailed(false);
    setCard(null);
    try {
      // 传原始输入：后端需要 #fp 做指纹校验，endpoint 已剥 fragment。
      const r = await previewA2aCard(raw);
      if (r && r.card && typeof r.card === "object") {
        setCard(r.card);
        const schemes = authSchemesFromCard(r.card);
        setAuthScheme(schemes.includes("bearer") ? "bearer" : schemes[0]);
      } else {
        setCardFailed(true);
      }
    } catch {
      setCardFailed(true);
    } finally {
      setCardLoading(false);
    }
  }, []);

  const submitAddress = () => {
    const p = parsePeerAddress(address);
    if (!p.ok) {
      setAddrError(t(`wizard-err-${p.error}`));
      return;
    }
    setAddrError(null);
    setParsed(p);
    setStep(2);
    void fetchPreview(address.trim());
  };

  const leavePreview = () => {
    if (!displayName) {
      setNameError(true);
      return;
    }
    setNameError(false);
    setStep(parsed && credentialsStepNeeded(parsed) ? 3 : 4);
  };

  const back = () => {
    setAddError(null);
    if (step === 4) setStep(parsed && credentialsStepNeeded(parsed) ? 3 : 2);
    else if (step > 1) setStep(step - 1);
  };

  const confirm = async () => {
    if (!parsed || !displayName) return;
    setAdding(true);
    setAddError(null);
    try {
      const r = await addA2aPeer(
        buildPeerPayload({ parsed, name: displayName, authScheme, token, trust }),
      );
      if (r && r.ok === false) {
        setAddError(r.error || t("wizard-add-failed"));
        return;
      }
      onAdded(displayName);
      onClose();
    } catch {
      // 探测/注册失败保留已填配置，可重试（设计 §6.1 第 4 步失败态）。
      setAddError(t("wizard-add-failed"));
    } finally {
      setAdding(false);
    }
  };

  if (typeof document === "undefined") return null;

  const skills = Array.isArray(card?.skills) ? (card!.skills as Array<Record<string, unknown>>) : [];
  const caps = (card?.capabilities ?? {}) as Record<string, unknown>;
  const provider = (card?.provider ?? {}) as Record<string, unknown>;
  const missing = card ? cardMissingFields(card) : [];
  const declaredAuth = authSchemesFromCard(card);

  const trustWarning = (
    <div className="flex gap-2 rounded-lg border border-amber/40 bg-amber/10 px-3 py-2 text-[11.5px] leading-relaxed text-amber">
      <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <span>{t("wizard-trust-warning")}</span>
    </div>
  );

  return createPortal(
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-black/45 p-4"
      onClick={onClose}
    >
      <div
        className="flex max-h-[86vh] w-full max-w-lg flex-col rounded-2xl border border-line bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={t("wizard-title")}
      >
        {/* header */}
        <div className="flex items-center justify-between border-b border-line px-5 py-3.5">
          <div>
            <h2 className="text-sm font-semibold text-txt">{t("wizard-title")}</h2>
            <p className="mt-0.5 text-[11px] text-faint">{t("wizard-step-of", { n: step })}</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            title={t("wizard-close")}
            className="rounded-md p-1.5 text-faint hover:bg-hover hover:text-txt"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* step dots */}
        <div className="flex items-center gap-1.5 border-b border-line px-5 py-2.5">
          {[1, 2, 3, 4].map((n) => (
            <span
              key={n}
              className={`h-1.5 flex-1 rounded-full transition-colors ${
                n <= step ? "bg-violet-600" : "bg-line2"
              }`}
            />
          ))}
        </div>

        {/* body */}
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-5 py-4">
          {step === 1 && (
            <>
              <h3 className="text-[12.5px] font-semibold text-txt">{t("wizard-step1-title")}</h3>
              <p className="text-[11.5px] leading-relaxed text-faint">{t("wizard-step1-hint")}</p>
              <label className="block text-[11px] font-medium text-muted">
                {t("wizard-address-label")}
                <textarea
                  value={address}
                  onChange={(e) => setAddress(e.target.value)}
                  placeholder={t("wizard-address-placeholder")}
                  rows={3}
                  spellCheck={false}
                  className="mt-1 w-full resize-none rounded-lg border border-line bg-base px-3 py-2 font-mono text-[12px] text-txt outline-none placeholder:text-faint focus:border-violet/50"
                />
              </label>
              {addrError && <p className="text-[11.5px] text-red-500">{addrError}</p>}
            </>
          )}

          {step === 2 && (
            <>
              <h3 className="text-[12.5px] font-semibold text-txt">{t("wizard-step2-title")}</h3>
              {cardLoading ? (
                <div className="flex items-center gap-2 py-6 text-[12px] text-faint">
                  <Loader2 className="h-4 w-4 animate-spin" /> {t("wizard-card-loading")}
                </div>
              ) : card ? (
                <div className="space-y-2.5 rounded-lg border border-line bg-card2/50 px-3 py-2.5">
                  <div className="flex flex-wrap items-baseline gap-2">
                    <span className="text-[13px] font-semibold text-txt">{cardName || "—"}</span>
                    {str(card.version) && (
                      <span className="text-[10.5px] text-faint">v{str(card.version)}</span>
                    )}
                    {str(provider.organization) && (
                      <span className="text-[10.5px] text-muted">
                        {t("wizard-provider")}: {str(provider.organization)}
                      </span>
                    )}
                  </div>
                  {str(card.description) && (
                    <p className="text-[11.5px] leading-relaxed text-muted">{str(card.description)}</p>
                  )}
                  <div className="flex flex-wrap gap-1.5">
                    {str(card.protocolVersion) && (
                      <span className="rounded-md border border-line2 bg-base px-1.5 py-0.5 font-mono text-[10px] text-muted">
                        {t("wizard-protocol")}: {str(card.protocolVersion)}
                      </span>
                    )}
                    {caps.streaming === true && (
                      <span className="rounded-md border border-line2 bg-base px-1.5 py-0.5 text-[10px] text-muted">
                        {t("wizard-cap-streaming")}
                      </span>
                    )}
                    {caps.pushNotifications === true && (
                      <span className="rounded-md border border-line2 bg-base px-1.5 py-0.5 text-[10px] text-muted">
                        {t("wizard-cap-push")}
                      </span>
                    )}
                    <span className="rounded-md border border-line2 bg-base px-1.5 py-0.5 text-[10px] text-muted">
                      {t("wizard-auth-declared")}: {declaredAuth.map((s) => t(AUTH_LABEL_KEY[s])).join(" / ")}
                    </span>
                  </div>
                  <div>
                    <p className="mb-1 text-[10.5px] font-semibold uppercase tracking-wide text-faint">
                      {t("wizard-skills")}
                    </p>
                    {skills.length ? (
                      <ul className="space-y-1">
                        {skills.slice(0, 8).map((s, i) => (
                          <li key={str(s.id) || i} className="text-[11.5px] leading-snug">
                            <span className="font-medium text-txt">{str(s.name) || str(s.id) || "—"}</span>
                            {str(s.description) && (
                              <span className="block text-[11px] text-faint">{str(s.description)}</span>
                            )}
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="text-[11px] text-faint">{t("wizard-no-skills")}</p>
                    )}
                  </div>
                  {missing.length > 0 && (
                    <p className="text-[11px] text-amber">
                      {t("wizard-card-incomplete", { fields: missing.join(", ") })}
                    </p>
                  )}
                </div>
              ) : (
                <p className="text-[11.5px] leading-relaxed text-faint">
                  {cardFailed ? t("wizard-card-failed") : t("wizard-card-loading")}
                </p>
              )}
              {/* Card 缺失/拉取失败/有 Card 但缺 name——都给出兜底填名入口 */}
              {!cardName && !cardLoading && (
                <div className="space-y-2">
                  <label className="block text-[11px] font-medium text-muted">
                    {t("wizard-manual-name")}
                    <input
                      value={manualName}
                      onChange={(e) => setManualName(e.target.value)}
                      placeholder={t("wizard-manual-name-placeholder")}
                      className="mt-1 w-full rounded-lg border border-line bg-base px-3 py-1.5 text-[12px] text-txt outline-none placeholder:text-faint focus:border-violet/50"
                    />
                  </label>
                  {nameError && <p className="text-[11.5px] text-red-500">{t("wizard-name-required")}</p>}
                </div>
              )}
              {/* 设计 §6.1：第 2 步必须出现第三方信任提示 */}
              {trustWarning}
            </>
          )}

          {step === 3 && parsed && (
            <>
              <h3 className="text-[12.5px] font-semibold text-txt">{t("wizard-step3-title")}</h3>
              {!credentialsStepNeeded(parsed) ? (
                <p className="flex items-center gap-2 text-[11.5px] text-muted">
                  <Check className="h-3.5 w-3.5 text-green-600 dark:text-green-400" />
                  {t("wizard-token-skipped")}
                </p>
              ) : (
                <>
                  <label className="block text-[11px] font-medium text-muted">
                    {t("wizard-auth-scheme")}
                    <select
                      value={authScheme}
                      onChange={(e) => setAuthScheme(e.target.value as AuthScheme)}
                      className="mt-1 w-full rounded-lg border border-line bg-base px-3 py-1.5 text-[12px] text-txt outline-none focus:border-violet/50"
                    >
                      {AUTH_OPTIONS.map((s) => (
                        <option key={s} value={s} disabled={s === "oauth2"}>
                          {t(AUTH_LABEL_KEY[s])}
                        </option>
                      ))}
                    </select>
                  </label>
                  {authScheme === "oauth2" && (
                    <p className="text-[11px] text-faint">{t("wizard-oauth2-note")}</p>
                  )}
                  {authScheme !== "none" && authScheme !== "oauth2" && (
                    <label className="block text-[11px] font-medium text-muted">
                      {t("wizard-token")}
                      {/* 敏感凭据：password input（设计 §12.C） */}
                      <input
                        type="password"
                        value={token}
                        onChange={(e) => setToken(e.target.value)}
                        placeholder={t("wizard-token-placeholder")}
                        autoComplete="off"
                        className="mt-1 w-full rounded-lg border border-line bg-base px-3 py-1.5 font-mono text-[12px] text-txt outline-none placeholder:text-faint focus:border-violet/50"
                      />
                    </label>
                  )}
                  <p className="text-[11px] leading-relaxed text-faint">{t("wizard-token-hint")}</p>
                </>
              )}
            </>
          )}

          {step === 4 && parsed && (
            <>
              <h3 className="text-[12.5px] font-semibold text-txt">{t("wizard-step4-title")}</h3>
              <dl className="selectable space-y-1.5 rounded-lg border border-line bg-card2/50 px-3 py-2.5 text-[11.5px]">
                <div className="flex gap-2">
                  <dt className="w-24 shrink-0 text-faint">{t("wizard-summary-name")}</dt>
                  <dd className="min-w-0 break-words font-medium text-txt">{displayName}</dd>
                </div>
                <div className="flex gap-2">
                  <dt className="w-24 shrink-0 text-faint">{t("wizard-summary-endpoint")}</dt>
                  <dd className="min-w-0 break-all font-mono text-[11px] text-muted">{parsed.endpoint}</dd>
                </div>
                {parsed.fingerprint && (
                  <div className="flex gap-2">
                    <dt className="w-24 shrink-0 text-faint">{t("wizard-summary-fingerprint")}</dt>
                    <dd className="min-w-0 break-all font-mono text-[11px] text-muted">{parsed.fingerprint}</dd>
                  </div>
                )}
                <div className="flex gap-2">
                  <dt className="w-24 shrink-0 text-faint">{t("wizard-summary-auth")}</dt>
                  <dd className="text-muted">
                    {parsed.token ? t("wizard-token-skipped") : t(AUTH_LABEL_KEY[authScheme])}
                  </dd>
                </div>
                {parsed.oneTime && (
                  <div className="flex gap-2">
                    <dt className="w-24 shrink-0 text-faint" />
                    <dd className="rounded-full border border-amber/40 bg-amber/10 px-2 py-px text-[10px] text-amber">
                      {t("wizard-one-time-badge")}
                    </dd>
                  </div>
                )}
              </dl>
              <label className="block text-[11px] font-medium text-muted">
                {t("wizard-trust-level")}
                <select
                  value={trust}
                  onChange={(e) => setTrust(e.target.value as TrustLevel)}
                  className="mt-1 w-full rounded-lg border border-line bg-base px-3 py-1.5 text-[12px] text-txt outline-none focus:border-violet/50"
                >
                  {TRUST_OPTIONS.map((x) => (
                    <option key={x} value={x}>
                      {t(`trust-${x}`)}
                    </option>
                  ))}
                </select>
              </label>
              <p className="text-[11px] leading-relaxed text-faint">{t("wizard-trust-hint")}</p>
              {trustWarning}
              {addError && <p className="text-[11.5px] text-red-500">{addError}</p>}
            </>
          )}
        </div>

        {/* footer */}
        <div className="flex items-center justify-between border-t border-line px-5 py-3">
          <button
            type="button"
            onClick={step === 1 ? onClose : back}
            className="flex items-center gap-1 rounded-lg border border-line px-2.5 py-1.5 text-[11.5px] text-muted hover:bg-hover hover:text-txt"
          >
            {step > 1 && <ChevronLeft className="h-3 w-3" />}
            {step === 1 ? t("wizard-cancel") : t("wizard-back")}
          </button>
          {step === 1 && (
            <button
              type="button"
              onClick={submitAddress}
              className="rounded-lg bg-violet-600 px-3 py-1.5 text-[11.5px] font-medium text-white hover:bg-violet-500"
            >
              {t("wizard-next")}
            </button>
          )}
          {step === 2 && (
            <button
              type="button"
              onClick={leavePreview}
              disabled={cardLoading}
              className="rounded-lg bg-violet-600 px-3 py-1.5 text-[11.5px] font-medium text-white hover:bg-violet-500 disabled:opacity-50"
            >
              {t("wizard-next")}
            </button>
          )}
          {step === 3 && (
            <button
              type="button"
              onClick={() => setStep(4)}
              className="rounded-lg bg-violet-600 px-3 py-1.5 text-[11.5px] font-medium text-white hover:bg-violet-500"
            >
              {t("wizard-next")}
            </button>
          )}
          {step === 4 && (
            <button
              type="button"
              onClick={() => void confirm()}
              disabled={adding || !displayName}
              className="flex items-center gap-1.5 rounded-lg bg-violet-600 px-3 py-1.5 text-[11.5px] font-medium text-white hover:bg-violet-500 disabled:opacity-50"
            >
              {adding && <Loader2 className="h-3 w-3 animate-spin" />}
              {adding ? t("wizard-adding") : t("wizard-confirm")}
            </button>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
