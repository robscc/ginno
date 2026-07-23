"use client";

import { useEffect, useState } from "react";

type Schema = { type?: string; properties?: Record<string, { type?: string }> };

function Field({
  propKey,
  schema,
  value,
  onChange,
}: {
  propKey: string;
  schema: { type?: string };
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const t = schema.type;
  // Hook must be called unconditionally (rules-of-hooks), even though the
  // textarea that uses it only renders for object/array types.
  const [txt, setTxt] = useState(JSON.stringify(value ?? (t === "array" ? [] : {}), null, 2));
  if (t === "number" || t === "integer") {
    return (
      <input
        type="number"
        className="field"
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
        className="field font-mono text-[11px]"
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
      className="field"
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

  useEffect(() => {
    setForm(initial);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dsl]);

  const set = (k: string, v: unknown) => {
    const next = { ...form, [k]: v };
    setForm(next);
    onChange(next);
  };

  const keys = Object.keys(props);
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2">
        <span className="text-[11px] font-medium text-txt">上下文（运行前可编辑）</span>
        <button
          onClick={() => {
            setForm(initial);
            onChange(initial);
          }}
          className="ml-auto text-[10px] text-faint hover:text-muted"
        >
          重置
        </button>
      </div>
      {keys.length === 0 ? (
        <textarea
          className="field font-mono text-[11px]"
          rows={3}
          value={JSON.stringify(form, null, 2)}
          onChange={(e) => {
            try {
              const v = JSON.parse(e.target.value);
              if (typeof v === "object" && v) {
                setForm(v);
                onChange(v);
              }
            } catch {
              /* ignore */
            }
          }}
        />
      ) : (
        keys.map((k) => (
          <div key={k} className="grid grid-cols-[110px_1fr] items-center gap-2">
            <label className="truncate text-[11px] text-muted" title={k}>
              {k}
            </label>
            <Field propKey={k} schema={props[k]} value={form[k]} onChange={(v) => set(k, v)} />
          </div>
        ))
      )}
    </div>
  );
}
