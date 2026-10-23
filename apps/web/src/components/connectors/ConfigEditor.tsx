"use client";

/* Schema-driven connector config editor (mods-connector-migration-design.md §4).
 *
 * 契约：configSchema 是 JSON Schema 子集 + x-ginno-* 扩展。表单按
 * x-ginno-group 分组、x-ginno-order 排序；未知键保留在「高级 JSON」里。
 * 保存走 PATCH /api/connectors/{id}/config，并等待 applied 结果——
 * 「已应用」只会在后端 ack（重载成功）后出现。 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import type {
  ConnectorConfigSchema,
  ConnectorSchemaProperty,
} from "@/lib/runtime";

type Values = Record<string, unknown>;
type Props = Record<string, ConnectorSchemaProperty>;

type ApplyState = "idle" | "applying" | "applied" | "error";

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function hasOwn(o: Record<string, unknown> | undefined, k: string): boolean {
  return !!o && Object.prototype.hasOwnProperty.call(o, k);
}

/** 深比较（表单值只含 JSON 基元/数组/对象，稳定序列化足够）。 */
function equal(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((v, i) => equal(v, b[i]));
  }
  if (isPlainObject(a) && isPlainObject(b)) {
    const ka = Object.keys(a);
    const kb = Object.keys(b);
    return ka.length === kb.length && ka.every((k) => hasOwn(b, k) && equal(a[k], b[k]));
  }
  return false;
}

/** 默认值优先取 detail.defaults（运行时权威），其次 schema.default。 */
function defaultOf(
  spec: ConnectorSchemaProperty,
  defaults: Values | undefined,
  key: string,
): unknown {
  if (hasOwn(defaults, key)) return defaults?.[key];
  return spec.default;
}

function defaultHint(value: unknown, emptyLabel: string): string {
  if (value === undefined || value === null) return emptyLabel;
  if (Array.isArray(value)) return value.length ? value.map(String).join(", ") : emptyLabel;
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

/** 表单值（含默认值）→ PATCH 的完整 config 载荷。
 *
 * 后端对 config 做「合并写」：省略一个键不会删掉已存覆盖。所以必须发全量
 * 生效值——「恢复默认」= 发送默认值本身，后端（mod 适配器）再剔除与默认值
 * 相等的键。未知键原样保留。 */
function buildPayload(
  props: Props,
  values: Values,
  extra: Values,
  defaults: Values | undefined,
): Values {
  const out: Values = {};
  for (const [key, spec] of Object.entries(props)) {
    if (key === "enabled") continue;
    const v = values[key] !== undefined ? values[key] : defaultOf(spec, defaults, key);
    if (v !== undefined) out[key] = v;
  }
  // 未知键是用户既有存储，原样保留。
  for (const [k, v] of Object.entries(extra)) out[k] = v;
  return out;
}

/** 表单值 + 未知键 → 高级 JSON 展示用的 effective 对象。 */
function effectiveFrom(
  props: Props,
  values: Values,
  extra: Values,
  defaults: Values | undefined,
): Values {
  const out: Values = { ...(defaults || {}) };
  for (const key of Object.keys(props)) {
    if (key === "enabled") continue;
    if (values[key] !== undefined) out[key] = values[key];
  }
  for (const [k, v] of Object.entries(extra)) out[k] = v;
  return out;
}

function valuesFrom(
  props: Props,
  overrides: Values,
  defaults: Values | undefined,
): Values {
  const out: Values = {};
  for (const [key, spec] of Object.entries(props)) {
    if (key === "enabled") continue;
    out[key] = hasOwn(overrides, key) ? overrides[key] : defaultOf(spec, defaults, key);
  }
  return out;
}

function extraFrom(overrides: Values, props: Props): Values {
  const out: Values = {};
  for (const [k, v] of Object.entries(overrides)) {
    if (k !== "enabled" && !(k in props)) out[k] = v;
  }
  return out;
}

function changedKeyCount(a: Values, b: Values): number {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  let n = 0;
  for (const k of keys) if (!equal(a[k], b[k])) n += 1;
  return n;
}

export function ConfigEditor({
  connectorId,
  config,
  defaults,
  schema,
  onSaved,
  onDirtyChange,
}: {
  connectorId: string;
  config?: Values;
  defaults?: Values;
  schema?: ConnectorConfigSchema;
  onSaved?: () => void;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const t = useTranslations("conn");
  const props = useMemo<Props>(() => {
    const raw = (schema?.properties || {}) as Props;
    // enabled 由卡片/抽屉的标准启停开关负责，不在表单里重复出现。
    const out: Props = {};
    for (const [k, v] of Object.entries(raw)) if (k !== "enabled") out[k] = v;
    return out;
  }, [schema]);

  const savedConfig = config || {};
  // baseline 是保存时的「完整生效值」快照；服务端 config 对内置连接器是
  // defaults ∪ stored（全量），对 mod 是覆盖值——统一归一化后首帧才不会误判
  // dirty，保存后也能正确判断是否还有未保存更改。
  const [baseline, setBaseline] = useState<Values>(() =>
    buildPayload(
      props,
      valuesFrom(props, savedConfig, defaults),
      extraFrom(savedConfig, props),
      defaults,
    ),
  );
  const [values, setValues] = useState<Values>(() =>
    valuesFrom(props, savedConfig, defaults),
  );
  const [extra, setExtra] = useState<Values>(() => extraFrom(savedConfig, props));
  const [applyState, setApplyState] = useState<ApplyState>("idle");
  const [error, setError] = useState<string | null>(null);
  // 已保存但重载失败（applied.ok=true, reloaded=false）——黄色提示，不是红色失败。
  const [notice, setNotice] = useState<string | null>(null);

  // 高级 JSON：默认折叠；打开时才生成文本。
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [advancedText, setAdvancedText] = useState("");
  const [advancedTouched, setAdvancedTouched] = useState(false);
  const [advancedError, setAdvancedError] = useState<string | null>(null);
  const [advancedValid, setAdvancedValid] = useState(false);

  // 每次字段编辑都清掉「已应用」，避免旧的成功提示误导。
  const markEdited = () => {
    setApplyState("idle");
    setError(null);
    setNotice(null);
  };

  const setValue = (key: string, v: unknown) => {
    markEdited();
    setValues((cur) => ({ ...cur, [key]: v }));
  };

  const canonicalAdvanced = useMemo(
    () => JSON.stringify(effectiveFrom(props, values, extra, defaults), null, 2),
    [props, values, extra, defaults],
  );

  const advancedDirty =
    advancedTouched && advancedText.trim() !== canonicalAdvanced.trim();

  const currentPayload = useMemo(
    () => buildPayload(props, values, extra, defaults),
    [props, values, extra, defaults],
  );

  const dirty = !equal(currentPayload, baseline) || advancedDirty;
  const changedCount =
    changedKeyCount(currentPayload, baseline) + (advancedDirty ? 1 : 0);

  // 未保存保护：刷新/关闭页面时由浏览器兜底；抽屉内由 ConnectorDetail 确认。
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  useEffect(() => {
    onDirtyChange?.(dirty);
    return () => onDirtyChange?.(false);
  }, [dirty, onDirtyChange]);

  useEffect(() => {
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      if (!dirtyRef.current) return;
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, []);

  // 数值字段范围校验（x-ginno-min / x-ginno-max）。
  const fieldErrors = useMemo(() => {
    const out: Record<string, string> = {};
    for (const [key, spec] of Object.entries(props)) {
      if (spec.type !== "number" && spec.type !== "integer") continue;
      const v = values[key];
      if (typeof v !== "number" || !Number.isFinite(v)) continue;
      const min = typeof spec["x-ginno-min"] === "number" ? spec["x-ginno-min"] : undefined;
      const max = typeof spec["x-ginno-max"] === "number" ? spec["x-ginno-max"] : undefined;
      if (min !== undefined && v < min) {
        out[key] = t("config.invalidNumber", { min, max: max ?? "∞" });
      } else if (max !== undefined && v > max) {
        out[key] = t("config.invalidNumber", { min: min ?? "-∞", max });
      }
    }
    return out;
  }, [props, values, t]);

  if (Object.keys(props).length === 0) {
    return <div className="py-6 text-sm text-faint">{t("config.empty")}</div>;
  }

  // 按 x-ginno-group 分组；组内按 x-ginno-order 再按 key 排序。
  const groups: Array<{ title: string; entries: Array<[string, ConnectorSchemaProperty]> }> = [];
  const sorted = Object.entries(props).sort(([ka, a], [kb, b]) => {
    const oa = typeof a["x-ginno-order"] === "number" ? a["x-ginno-order"] : Number.POSITIVE_INFINITY;
    const ob = typeof b["x-ginno-order"] === "number" ? b["x-ginno-order"] : Number.POSITIVE_INFINITY;
    if (oa !== ob) return oa - ob;
    return ka.localeCompare(kb);
  });
  for (const entry of sorted) {
    const title =
      typeof entry[1]["x-ginno-group"] === "string" && entry[1]["x-ginno-group"]
        ? String(entry[1]["x-ginno-group"])
        : t("config.groupGeneral");
    const group = groups.find((g) => g.title === title);
    if (group) group.entries.push(entry);
    else groups.push({ title, entries: [entry] });
  }

  const unknownKeys = Object.keys(extra);

  const openAdvanced = () => {
    if (!advancedOpen) {
      setAdvancedText(canonicalAdvanced);
      setAdvancedTouched(false);
      setAdvancedError(null);
      setAdvancedValid(false);
    }
    setAdvancedOpen(!advancedOpen);
  };

  const syncFromForm = () => {
    setAdvancedText(canonicalAdvanced);
    setAdvancedTouched(false);
    setAdvancedError(null);
    setAdvancedValid(false);
    markEdited();
  };

  const parseAdvanced = (): Values => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(advancedText);
    } catch (e) {
      throw new Error(e instanceof Error ? e.message : String(e));
    }
    if (!isPlainObject(parsed)) throw new Error(t("config.invalidJsonObject"));
    return parsed;
  };

  const validateAdvanced = () => {
    try {
      parseAdvanced();
      setAdvancedError(null);
      setAdvancedValid(true);
    } catch (e) {
      setAdvancedValid(false);
      setAdvancedError(
        t("config.invalidJson", { error: e instanceof Error ? e.message : String(e) }),
      );
    }
  };

  const save = async () => {
    setApplyState("applying");
    setError(null);
    setNotice(null);

    // 表单路径发全量生效值；高级 JSON 路径以用户编辑的对象为准。
    let payload = currentPayload;
    if (advancedDirty) {
      try {
        payload = parseAdvanced();
      } catch (e) {
        setApplyState("error");
        setError(t("config.invalidJson", { error: e instanceof Error ? e.message : String(e) }));
        return;
      }
    }

    try {
      const r = await api.patchConnectorConfig(connectorId, payload);
      // 400/404 的 JSON 体只有 {error}，没有 ok/applied——也要判失败。
      if (r.ok === false || r.error || r.applied?.ok === false) {
        setApplyState("error");
        setError(r.error || r.applied?.error || t("config.applyFailed"));
        return;
      }
      const returned = r.config ?? payload;
      const nextValues = valuesFrom(props, returned, defaults);
      const nextExtra = extraFrom(returned, props);
      setBaseline(buildPayload(props, nextValues, nextExtra, defaults));
      setValues(nextValues);
      setExtra(nextExtra);
      setAdvancedTouched(false);
      setAdvancedText(
        JSON.stringify(effectiveFrom(props, nextValues, nextExtra, defaults), null, 2),
      );
      setAdvancedValid(false);
      setAdvancedError(null);
      setApplyState("applied");
      // 配置已保存但 mod 未重载：如实提示，不要伪装成失败。
      setNotice(r.applied?.error || null);
      onSaved?.();
    } catch (e) {
      setApplyState("error");
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const hasFieldErrors = Object.keys(fieldErrors).length > 0;

  return (
    <div className="text-sm">
      {groups.map((group) => (
        <section key={group.title} className="mb-5">
          <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted">
            {group.title}
          </h3>
          <div>
            {group.entries.map(([key, spec]) => {
              const def = defaultOf(spec, defaults, key);
              const value = values[key];
              const modified = !equal(value, def);
              const title = String(spec.title || key);
              const desc = spec.description ? String(spec.description) : "";
              const fieldError = fieldErrors[key];
              // 契约用 x-ginno-secret；内置连接器历史上用 format:"password"。
              const isSecret =
                spec["x-ginno-secret"] === true || spec.format === "password";
              return (
                <div
                  key={key}
                  className="grid grid-cols-1 gap-3 border-t border-line py-3 first:border-t-0 md:grid-cols-[1fr_250px] md:items-start md:gap-5"
                >
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-1.5 text-[12.5px] text-txt">
                      <span>{title}</span>
                      {isSecret && (
                        <span className="rounded-full border border-line2 bg-card2 px-1.5 py-px text-[10px] text-faint">
                          {t("config.secret")}
                        </span>
                      )}
                      {modified && (
                        <span className="rounded-full border border-line2 bg-card2 px-1.5 py-px text-[10px] text-faint">
                          {t("config.modified")}
                        </span>
                      )}
                    </div>
                    {desc && (
                      <p className="mt-1 text-[11.5px] leading-relaxed text-faint">{desc}</p>
                    )}
                    <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[10.5px] text-faint">
                      <span>
                        {t("config.default")}{" "}
                        <code className="rounded bg-card2 px-1 py-px font-mono text-[10px] text-muted">
                          {defaultHint(def, t("config.emptyDefault"))}
                        </code>
                      </span>
                      {modified && (
                        <button
                          type="button"
                          onClick={() => setValue(key, def)}
                          className="text-violet-500 hover:text-violet-400"
                        >
                          {t("config.reset")}
                        </button>
                      )}
                    </div>
                  </div>
                  <div className="min-w-0">
                    <FieldControl
                      spec={spec}
                      value={value}
                      onChange={(v) => setValue(key, v)}
                      error={fieldError}
                      t={t as unknown as TFn}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      ))}

      {/* 高级 JSON 逃生口：未知键只在这里可见/可改。 */}
      <div className="mt-4 overflow-hidden rounded-lg border border-line bg-panel">
        <button
          type="button"
          onClick={openAdvanced}
          className="flex w-full items-center gap-2 px-3 py-2 text-left text-[11.5px] text-muted hover:bg-card2"
        >
          <span>{advancedOpen ? "▾" : "▸"}</span>
          <span>{t("config.advanced")}</span>
          <span className="ml-auto rounded-full border border-line2 px-1.5 py-px text-[10px] text-faint">
            {t("config.advancedHint")}
          </span>
        </button>
        {advancedOpen && (
          <div className="border-t border-line px-3 py-2.5">
            <textarea
              value={advancedText}
              spellCheck={false}
              onChange={(e) => {
                setAdvancedText(e.target.value);
                setAdvancedTouched(true);
                setAdvancedValid(false);
                setAdvancedError(null);
                markEdited();
              }}
              rows={9}
              className="w-full resize-y rounded-lg border border-line2 bg-card2 px-2.5 py-2 font-mono text-[11px] leading-relaxed text-txt outline-none focus:border-violet"
            />
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={validateAdvanced}
                className="rounded-lg border border-line px-2.5 py-1 text-[11px] text-txt hover:bg-hover"
              >
                {t("config.validate")}
              </button>
              <button
                type="button"
                onClick={syncFromForm}
                className="rounded-lg border border-line px-2.5 py-1 text-[11px] text-txt hover:bg-hover"
              >
                {t("config.syncFromForm")}
              </button>
              {advancedValid && (
                <span className="text-[11px] text-green-600 dark:text-green-400">
                  {t("config.validJson")}
                </span>
              )}
              {advancedError && (
                <span className="text-[11px] text-red-600 dark:text-red-400">{advancedError}</span>
              )}
            </div>
            {unknownKeys.length > 0 && (
              <p className="mt-2 rounded-md border border-yellow/30 bg-yellow/10 px-2.5 py-1.5 text-[11px] leading-relaxed text-yellow-700 dark:text-yellow-200">
                {t("config.unknownNote")}
                <span className="ml-1 font-mono text-[10px] text-yellow-800 dark:text-yellow-300">
                  {unknownKeys.join(", ")}
                </span>
              </p>
            )}
          </div>
        )}
      </div>

      {/* 粘性保存条：N changed + Discard + Save。 */}
      <div className="sticky bottom-0 z-10 -mx-5 mt-5 flex flex-wrap items-center gap-2 border-t border-line bg-base/95 px-5 py-3 backdrop-blur">
        <span
          className={`text-[11.5px] ${dirty ? "text-yellow-600 dark:text-yellow-400" : "text-faint"}`}
        >
          {dirty ? t("config.changed", { n: changedCount }) : t("config.clean")}
        </span>
        {applyState === "applied" && (
          <span className="text-[11.5px] text-green-600 dark:text-green-400">
            {t("config.applied")}
          </span>
        )}
        {notice && (
          <span className="min-w-0 break-words text-[11.5px] text-yellow-600 dark:text-yellow-400">
            {notice}
          </span>
        )}
        {error && (
          <span className="min-w-0 break-words text-[11.5px] text-red-600 dark:text-red-400">
            {error}
          </span>
        )}
        <span className="flex-1" />
        <button
          type="button"
          disabled={!dirty || applyState === "applying"}
          onClick={() => {
            markEdited();
            const resetValues = valuesFrom(props, baseline, defaults);
            const resetExtra = extraFrom(baseline, props);
            setValues(resetValues);
            setExtra(resetExtra);
            setAdvancedTouched(false);
            setAdvancedText(
              JSON.stringify(effectiveFrom(props, resetValues, resetExtra, defaults), null, 2),
            );
            setAdvancedError(null);
            setAdvancedValid(false);
          }}
          className="rounded-lg px-3 py-1.5 text-xs text-faint hover:text-txt disabled:opacity-40"
        >
          {t("config.discard")}
        </button>
        <button
          type="button"
          disabled={!dirty || applyState === "applying" || hasFieldErrors}
          onClick={() => void save()}
          className="rounded-lg bg-violet-600 px-4 py-1.5 text-xs font-medium text-white hover:bg-violet-500 disabled:opacity-40"
        >
          {applyState === "applying" ? t("config.applying") : t("config.save")}
        </button>
      </div>
    </div>
  );
}

/* ---- 控件 ---------------------------------------------------------------- */

type TFn = (key: string, values?: Record<string, string | number>) => string;

function FieldControl({
  spec,
  value,
  onChange,
  error,
  t,
}: {
  spec: ConnectorSchemaProperty;
  value: unknown;
  onChange: (v: unknown) => void;
  error?: string;
  t: TFn;
}) {
  const placeholder =
    typeof spec["x-ginno-placeholder"] === "string"
      ? spec["x-ginno-placeholder"]
      : undefined;
  const inputCls =
    "w-full rounded-lg border border-line2 bg-card2 px-2.5 py-1.5 text-xs text-txt outline-none placeholder:text-faint focus:border-violet";

  if (spec.type === "boolean") {
    const on = !!value;
    return (
      <div className="flex items-center justify-end md:justify-start">
        <button
          type="button"
          role="switch"
          aria-checked={on}
          onClick={() => onChange(!on)}
          className={`relative h-5 w-9 shrink-0 rounded-full transition-colors ${
            on ? "bg-violet" : "bg-line2"
          }`}
        >
          <span
            className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${
              on ? "left-[18px]" : "left-0.5"
            }`}
          />
        </button>
      </div>
    );
  }

  if (Array.isArray(spec.enum) && spec.enum.length > 0) {
    const opts = spec.enum;
    return (
      <select
        value={value === undefined ? "" : String(value)}
        onChange={(e) => {
          const picked = opts.find((o) => String(o) === e.target.value);
          onChange(picked === undefined ? e.target.value : picked);
        }}
        className={inputCls}
      >
        {value === undefined && <option value="">—</option>}
        {opts.map((o) => (
          <option key={String(o)} value={String(o)}>
            {String(o)}
          </option>
        ))}
      </select>
    );
  }

  if (spec.type === "number" || spec.type === "integer") {
    const min = typeof spec["x-ginno-min"] === "number" ? spec["x-ginno-min"] : undefined;
    const max = typeof spec["x-ginno-max"] === "number" ? spec["x-ginno-max"] : undefined;
    return (
      <div>
        <input
          type="number"
          value={value === undefined || value === null ? "" : String(value)}
          min={min}
          max={max}
          step={spec.type === "integer" ? 1 : "any"}
          placeholder={placeholder}
          onChange={(e) => {
            const raw = e.target.value;
            onChange(raw === "" ? undefined : Number(raw));
          }}
          className={inputCls}
        />
        {(min !== undefined || max !== undefined) && (
          <div className="mt-1 text-[10.5px] text-faint">
            {t("config.range", { min: min ?? "-∞", max: max ?? "∞" })}
          </div>
        )}
        {error && (
          <div className="mt-1 text-[10.5px] text-red-600 dark:text-red-400">{error}</div>
        )}
      </div>
    );
  }

  if (spec.type === "array") {
    const itemType =
      isPlainObject(spec.items) && typeof spec.items.type === "string"
        ? spec.items.type
        : "string";
    const arr = Array.isArray(value) ? value : [];
    // 对象数组（如钉钉 menus）不能按行拆——退回 JSON 编辑器，避免写成
    // "[object Object]" 把结构毁掉。
    const hasObjects = arr.some((v) => isPlainObject(v) || Array.isArray(v));
    if (itemType === "object" || hasObjects) {
      return <JsonListInput value={arr} onChange={onChange} t={t} />;
    }
    return (
      <textarea
        value={arr.map(String).join("\n")}
        rows={3}
        placeholder={t("config.onePerLine")}
        onChange={(e) => {
          const lines = e.target.value
            .split("\n")
            .map((s) => s.trim())
            .filter(Boolean);
          onChange(
            itemType === "number" || itemType === "integer"
              ? lines.map(Number).filter((n) => Number.isFinite(n))
              : lines,
          );
        }}
        className={`${inputCls} resize-y font-mono`}
      />
    );
  }

  if (spec["x-ginno-secret"] === true || spec.format === "password") {
    return <SecretInput value={value} onChange={onChange} placeholder={placeholder} t={t} />;
  }

  return (
    <input
      type="text"
      value={value === undefined || value === null ? "" : String(value)}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      className={inputCls}
    />
  );
}

/** 对象数组的 JSON 编辑器（按行拆分会毁掉结构，见 DingTalk menus）。 */
function JsonListInput({
  value,
  onChange,
  t,
}: {
  value: unknown[];
  onChange: (v: unknown) => void;
  t: TFn;
}) {
  const [text, setText] = useState(() => JSON.stringify(value ?? [], null, 2));
  const [err, setErr] = useState<string | null>(null);
  const syncedRef = useRef<unknown[]>(value);
  useEffect(() => {
    if (!equal(value, syncedRef.current)) {
      syncedRef.current = value;
      setText(JSON.stringify(value ?? [], null, 2));
    }
  }, [value]);
  return (
    <div>
      <textarea
        value={text}
        rows={4}
        spellCheck={false}
        onChange={(e) => {
          const next = e.target.value;
          setText(next);
          try {
            const parsed: unknown = JSON.parse(next);
            if (!Array.isArray(parsed)) throw new Error(t("config.invalidJsonArray"));
            setErr(null);
            syncedRef.current = parsed;
            onChange(parsed);
          } catch (ex) {
            setErr(
              t("config.invalidJson", {
                error: ex instanceof Error ? ex.message : String(ex),
              }),
            );
          }
        }}
        className="w-full resize-y rounded-lg border border-line2 bg-card2 px-2.5 py-1.5 font-mono text-[11px] leading-relaxed text-txt outline-none focus:border-violet"
      />
      {err && <div className="mt-1 text-[10.5px] text-red-600 dark:text-red-400">{err}</div>}
    </div>
  );
}

function SecretInput({
  value,
  onChange,
  placeholder,
  t,
}: {
  value: unknown;
  onChange: (v: unknown) => void;
  placeholder?: string;
  t: TFn;
}) {
  const [shown, setShown] = useState(false);
  return (
    <div className="relative">
      <input
        type={shown ? "text" : "password"}
        value={value === undefined || value === null ? "" : String(value)}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-lg border border-line2 bg-card2 px-2.5 py-1.5 pr-14 text-xs text-txt outline-none placeholder:text-faint focus:border-violet"
      />
      <button
        type="button"
        onClick={() => setShown((s) => !s)}
        className="absolute right-1 top-1/2 -translate-y-1/2 rounded px-1.5 py-0.5 text-[10.5px] text-faint hover:bg-line hover:text-txt"
      >
        {shown ? t("config.hide") : t("config.show")}
      </button>
    </div>
  );
}