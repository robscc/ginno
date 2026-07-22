"use client";

import { useEffect, useState } from "react";
import * as api from "@/lib/runtime";

type Perms = { allow: string[]; deny: string[]; ask: string[] };
const EMPTY: Perms = { allow: [], deny: [], ask: [] };

const HELP: Record<keyof Perms, string> = {
  allow: "匹配即放行、不弹框。例：Read(*)、Grep(*)",
  ask: "匹配即弹框询问。例：Bash(*)、Write(*)",
  deny: "匹配即拒绝（优先级最高）。例：Bash(rm -rf *)",
};

function RuleList({
  title,
  which,
  rules,
  onChange,
}: {
  title: string;
  which: keyof Perms;
  rules: string[];
  onChange: (next: string[]) => void;
}) {
  const [draft, setDraft] = useState("");
  const add = () => {
    const v = draft.trim();
    if (!v) return;
    onChange([...rules, v]);
    setDraft("");
  };
  return (
    <div>
      <div className="mb-1 flex items-center gap-2">
        <span className="text-sm font-medium text-txt">{title}</span>
        <span className="rounded-full bg-card2 px-2 py-0.5 text-[11px] text-muted">{rules.length}</span>
      </div>
      <p className="mb-2 text-xs text-faint">{HELP[which]}</p>
      <div className="space-y-1.5">
        {rules.map((r, i) => (
          <div key={i} className="flex gap-2">
            <input
              className="field flex-1 font-mono text-xs"
              value={r}
              onChange={(e) => onChange(rules.map((x, j) => (j === i ? e.target.value : x)))}
            />
            <button
              onClick={() => onChange(rules.filter((_, j) => j !== i))}
              aria-label="删除规则"
              className="rounded-lg border border-line px-2 text-muted hover:text-red"
            >
              ×
            </button>
          </div>
        ))}
        <div className="flex gap-2">
          <input
            className="field flex-1 font-mono text-xs"
            placeholder="新规则，如 Bash(git *)"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && add()}
          />
          <button
            onClick={add}
            className="rounded-lg border border-line2 px-3 text-xs text-muted hover:text-txt"
          >
            添加
          </button>
        </div>
      </div>
    </div>
  );
}

export function PermissionsSettings() {
  const [perms, setPerms] = useState<Perms>(EMPTY);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = () => {
    setMsg("");
    api
      .getSettings()
      .then((s) => {
        const p = ((s as Record<string, unknown>).permissions || {}) as Record<string, unknown>;
        setPerms({
          allow: Array.isArray(p.allow) ? (p.allow as string[]) : [],
          deny: Array.isArray(p.deny) ? (p.deny as string[]) : [],
          ask: Array.isArray(p.ask) ? (p.ask as string[]) : [],
        });
      })
      .catch(() => setMsg("加载失败：运行时未连接"));
  };
  useEffect(load, []);

  const set = (which: keyof Perms) => (next: string[]) => setPerms((p) => ({ ...p, [which]: next }));

  async function save() {
    setBusy(true);
    setMsg("");
    try {
      // PUT /settings replaces the whole file → read full settings, merge, write back
      // so providers / hooks / knowledge are not clobbered.
      const s = (await api.getSettings()) as Record<string, unknown>;
      s.permissions = { allow: perms.allow, deny: perms.deny, ask: perms.ask };
      const r = await api.putSettings(s);
      setMsg(r.ok ? "已保存（下一次工具调用即生效，无需重启）" : "保存失败");
    } catch {
      setMsg("保存失败：运行时未连接");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">权限策略</h2>
      <p className="mt-1 max-w-2xl text-sm text-muted">
        规则形如 <code className="font-mono text-txt">Tool(arg-glob)</code>，按{" "}
        <b>deny → ask → allow</b> 顺序首个匹配生效，缺省 ask。特权模式开启时本策略被跳过（见 通用设置）。
      </p>
      <div className="mt-5 max-w-2xl space-y-6">
        <RuleList title="Allow（放行）" which="allow" rules={perms.allow} onChange={set("allow")} />
        <RuleList title="Ask（询问）" which="ask" rules={perms.ask} onChange={set("ask")} />
        <RuleList title="Deny（拒绝）" which="deny" rules={perms.deny} onChange={set("deny")} />
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
