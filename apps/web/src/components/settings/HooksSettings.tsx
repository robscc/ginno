"use client";

import { useEffect, useState } from "react";
import * as api from "@/lib/runtime";

const EVENTS = ["PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SessionStart"] as const;
type Ev = (typeof EVENTS)[number];
type Hook = { matcher: string; command: string };

const HELP: Record<Ev, string> = {
  PreToolUse: "Before a tool call; matcher = tool name (e.g. Bash).",
  PostToolUse: "After a tool call; matcher = tool name.",
  UserPromptSubmit: "When the user submits a message.",
  Stop: "At the end of each turn.",
  SessionStart: "When a session starts.",
};

const norm = (h: unknown): Hook => {
  const o = (h || {}) as Record<string, unknown>;
  return { matcher: String(o.matcher || ""), command: String(o.command || "") };
};

export function HooksSettings() {
  const [hooks, setHooks] = useState<Record<string, Hook[]>>({});
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = () => {
    setMsg("");
    api
      .getSettings()
      .then((s) => {
        const raw = ((s as Record<string, unknown>).hooks || {}) as Record<string, unknown[]>;
        const out: Record<string, Hook[]> = {};
        for (const e of EVENTS) out[e] = Array.isArray(raw[e]) ? raw[e].map(norm) : [];
        setHooks(out);
      })
      .catch(() => setMsg("Failed to load: runtime not connected"));
  };
  useEffect(load, []);

  const update = (e: Ev, next: Hook[]) => setHooks((h) => ({ ...h, [e]: next }));

  async function save() {
    setBusy(true);
    setMsg("");
    try {
      const s = (await api.getSettings()) as Record<string, unknown>;
      const cleaned: Record<string, Hook[]> = {};
      for (const e of EVENTS) {
        const list = (hooks[e] || [])
          .map((h) => ({
            command: h.command.trim(),
            ...(h.matcher.trim() ? { matcher: h.matcher.trim() } : {}),
          }))
          .filter((h) => h.command);
        if (list.length) cleaned[e] = list as Hook[];
      }
      s.hooks = cleaned;
      const r = await api.putSettings(s);
      setMsg(r.ok ? "Saved (takes effect on the next matching event; hooks are unaffected by Privileged Mode)" : "Failed to save");
    } catch {
      setMsg("Failed to save: runtime not connected");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">Hooks</h2>
      <p className="mt-1 max-w-2xl text-sm text-muted">
        Run custom commands on lifecycle events; commands receive a JSON payload on stdin (including
        event / tool_name, etc.). Hooks always run and are unaffected by Privileged Mode.
      </p>
      <div className="mt-5 max-w-2xl space-y-6">
        {EVENTS.map((e) => (
          <div key={e}>
            <div className="mb-1 flex items-center gap-2">
              <span className="font-mono text-sm text-txt">{e}</span>
              <span className="rounded-full bg-card2 px-2 py-0.5 text-[11px] text-muted">
                {(hooks[e] || []).length}
              </span>
            </div>
            <p className="mb-2 text-xs text-faint">{HELP[e]}</p>
            <div className="space-y-1.5">
              {(hooks[e] || []).map((h, i) => (
                <div key={i} className="flex gap-2">
                  <input
                    className="field w-40 font-mono text-xs"
                    placeholder="matcher (optional)"
                    value={h.matcher}
                    onChange={(ev) =>
                      update(
                        e,
                        (hooks[e] || []).map((x, j) => (j === i ? { ...x, matcher: ev.target.value } : x)),
                      )
                    }
                  />
                  <input
                    className="field flex-1 font-mono text-xs"
                    placeholder="command, e.g. python ~/.ginno/hooks/x.py"
                    value={h.command}
                    onChange={(ev) =>
                      update(
                        e,
                        (hooks[e] || []).map((x, j) => (j === i ? { ...x, command: ev.target.value } : x)),
                      )
                    }
                  />
                  <button
                    onClick={() => update(e, (hooks[e] || []).filter((_, j) => j !== i))}
                    aria-label="Delete hook"
                    className="rounded-lg border border-line px-2 text-muted hover:text-red"
                  >
                    ×
                  </button>
                </div>
              ))}
              <button
                onClick={() => update(e, [...(hooks[e] || []), { matcher: "", command: "" }])}
                className="rounded-lg border border-line2 px-3 py-1 text-xs text-muted hover:text-txt"
              >
                + Add hook
              </button>
            </div>
          </div>
        ))}
        <div className="flex items-center gap-3">
          <button
            onClick={save}
            disabled={busy}
            className="rounded-lg bg-violet px-4 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            Save
          </button>
          <button
            onClick={load}
            className="rounded-lg border border-line px-3 py-1.5 text-sm text-muted hover:text-txt"
          >
            Reload
          </button>
          {msg && <span className="text-xs text-muted">{msg}</span>}
        </div>
      </div>
    </div>
  );
}
