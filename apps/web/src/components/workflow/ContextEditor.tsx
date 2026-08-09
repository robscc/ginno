"use client";

import { useEffect, useMemo, useState } from "react";
import { AlertCircle, Check } from "lucide-react";

type Schema = { type?: string; properties?: Record<string, { type?: string }> };

function isEmpty(v: unknown): boolean {
  return v === undefined || v === null || v === "";
}

function Field({
  propKey,
  schema,
  value,
  onChange,
  warn,
}: {
  propKey: string;
  schema: { type?: string };
  value: unknown;
  onChange: (v: unknown) => void;
  // P3: the DSL references {{context.<key>}} but the value is still empty —
  // yellow border so the user knows what to fill before running.
  warn?: boolean;
}) {
  const t = schema.type;
  // Hook must be called unconditionally (rules-of-hooks), even though the
  // textarea that uses it only renders for object/array types.
  const [txt, setTxt] = useState(JSON.stringify(value ?? (t === "array" ? [] : {}), null, 2));
  const warnCls = warn ? "border-yellow/60 focus:border-yellow" : "";
  if (t === "number" || t === "integer") {
    return (
      <input
        type="number"
        className={`field ${warnCls}`}
        value={value == null ? "" : String(value)}
        onChange={(e) => onChange(e.target.value === "" ? undefined : Number(e.target.value))}
      />
    );
  }
  if (t === "boolean") {
    return (
      <input
        type="checkbox"
        className="h-4 w-4 rounded accent-violet"
        checked={!!value}
        onChange={(e) => onChange(e.target.checked)}
      />
    );
  }
  if (t === "object" || t === "array") {
    return (
      <textarea
        className={`field font-mono text-[11px] ${warnCls}`}
        rows={3}
        value={txt}
        onChange={(e) => {
          setTxt(e.target.value);
          try {
            onChange(JSON.parse(e.target.value));
          } catch {
            /* ignore until valid */
          }
        }}
      />
    );
  }
  return (
    <input
      type="text"
      className={`field ${warnCls}`}
      value={value == null ? "" : String(value)}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

/** Edit the WorkflowContext initial values (design §6). Schema-driven form with a
 *  JSON fallback; the parent ships the result as `context_override` on run. */
export function ContextEditor({
  dsl,
  onChange,
}: {
  dsl?: { context?: { schema?: Schema; initial?: Record<string, unknown> } };
  onChange: (v: Record<string, unknown>) => void;
}) {
  const schema = dsl?.context?.schema;
  const initial = dsl?.context?.initial || {};
  const props = schema?.properties || {};
  const [form, setForm] = useState<Record<string, unknown>>(initial);
  // Separate raw-text state for the schema-less JSON editor so the user can type
  // an intermediate (temporarily invalid) value without the input snapping back
  // to the last valid object on every keystroke.
  const [rawJson, setRawJson] = useState(() => JSON.stringify(initial, null, 2));

  useEffect(() => {
    setForm(initial);
    setRawJson(JSON.stringify(initial, null, 2));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dsl]);

  const set = (k: string, v: unknown) => {
    const next = { ...form, [k]: v };
    setForm(next);
    onChange(next);
  };

  const keys = Object.keys(props);
  // P3: which context keys the DSL actually interpolates ({{context.<key>}}) —
  // drives the fill-status chips and the yellow "unfilled" field highlight.
  const templateKeys = useMemo(() => {
    const found = new Set<string>();
    try {
      for (const m of JSON.stringify(dsl ?? {}).matchAll(/\{\{\s*context\.([a-zA-Z0-9_]+)\s*\}\}/g)) {
        found.add(m[1]);
      }
    } catch {
      /* unstringifiable dsl — no hints */
    }
    return found;
  }, [dsl]);
  const hintKeys = [...templateKeys].filter((k) => keys.includes(k));
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-1.5">
        <span className="text-[11px] font-medium text-txt">上下文（运行前可编辑）</span>
        <span
          className="group relative flex h-3.5 w-3.5 cursor-help items-center justify-center rounded-full border border-line2 text-[9px] text-faint"
          aria-label="上下文说明"
        >
          i
          <span className="pointer-events-none absolute left-1/2 top-full z-20 mt-1 w-56 -translate-x-1/2 rounded-md border border-line2 bg-card p-2 text-[10px] font-normal leading-snug text-muted opacity-0 shadow-lg transition-opacity duration-150 group-hover:opacity-100">
            工作流的「运行变量」：每次执行可改的输入（如仓库名、阈值、目标列表），会以{" "}
            <span className="font-mono text-txt">{"{{context.字段}}"}</span>{" "}
            注入各步骤的 goal。改这里只影响本次运行，不改 DSL 本身。
          </span>
        </span>
        <button
          onClick={() => {
            setForm(initial);
            setRawJson(JSON.stringify(initial, null, 2));
            onChange(initial);
          }}
          className="ml-auto text-[10px] text-faint hover:text-muted"
        >
          重置
        </button>
      </div>
      {hintKeys.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] text-faint">模板变量</span>
          {hintKeys.map((k) => {
            const filled = !isEmpty(form[k]);
            return (
              <span
                key={k}
                className={`flex items-center gap-1 rounded-full border px-1.5 py-0.5 font-mono text-[10px] ${
                  filled ? "border-line text-green" : "border-yellow/50 bg-yellow/10 text-yellow"
                }`}
              >
                {filled ? <Check className="h-2.5 w-2.5" /> : <AlertCircle className="h-2.5 w-2.5" />}
                {k}
                {!filled && <span className="font-sans">(未填)</span>}
              </span>
            );
          })}
        </div>
      )}
      <p className="text-[10px] leading-snug text-faint">
        {keys.length === 0
          ? "该 DSL 未声明 context.schema，下方用 JSON 自由填写本次运行的初始上下文（可为空 {}）。"
          : "按 schema 生成的表单；这些值会在运行开始时作为 context 初始值传入。"}
      </p>
      {keys.length === 0 ? (
        <textarea
          className="field font-mono text-[11px]"
          rows={3}
          value={rawJson}
          onChange={(e) => {
            setRawJson(e.target.value);
            try {
              const v = JSON.parse(e.target.value);
              if (typeof v === "object" && v) {
                setForm(v);
                onChange(v);
              }
            } catch {
              /* invalid intermediate — keep the caret, update once it parses */
            }
          }}
        />
      ) : (
        keys.map((k) => (
          <div key={k} className="grid grid-cols-[110px_1fr] items-center gap-2">
            <label className="truncate text-[11px] text-muted" title={k}>
              {k}
            </label>
            <Field
              propKey={k}
              schema={props[k]}
              value={form[k]}
              onChange={(v) => set(k, v)}
              warn={templateKeys.has(k) && isEmpty(form[k])}
            />
          </div>
        ))
      )}
    </div>
  );
}
