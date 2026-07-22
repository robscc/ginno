"use client";

import { useEffect, useState } from "react";
import * as api from "@/lib/runtime";

const EVENTS = ["PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SessionStart"] as const;
type Ev = (typeof EVENTS)[number];
type Hook = { matcher: string; command: string };

const HELP: Record<Ev, string> = {
  PreToolUse: "工具调用前；matcher = 工具名（如 Bash）。",
  PostToolUse: "工具调用后；matcher = 工具名。",
  UserPromptSubmit: "用户提交消息时。",
  Stop: "一轮结束时。",
  SessionStart: "会话开始时。",
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
      .catch(() => setMsg("加载失败：运行时未连接"));
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
      setMsg(r.ok ? "已保存（下一次匹配的事件即生效，hook 不受特权模式影响）" : "保存失败");
    } catch {
      setMsg("保存失败：运行时未连接");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">Hooks</h2>
      <p className="mt-1 max-w-2xl text-sm text-muted">
        在生命周期事件触发自定义命令；命令通过 stdin 收到 JSON 载荷（含 event / tool_name 等）。hook
        始终执行，不受特权模式影响。
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
                    placeholder="matcher (可选)"
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
                    placeholder="command，如 python ~/.ginno/hooks/x.py"
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
                    aria-label="删除 hook"
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
                + 添加 hook
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
            保存
          </button>
          <button
            onClick={load}
            className="rounded-lg border border-line px-3 py-1.5 text-sm text-muted hover:text-txt"
          >
            重新加载
          </button>
          {msg && <span className="text-xs text-muted">{msg}</span>}
        </div>
      </div>
    </div>
  );
}
