"use client";

// AddMemberModal — 给团队加成员实例（agent-team-design.md §3.1 / §5.1）。
// 选一个 agent（可重复选同一个 → 各自独立实例）+ 可选显示名 + 角色。
// runtime 会为成员建独立 session（api/teams.py add_member_endpoint）。

import { useState } from "react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import { agentHex } from "@/lib/theme";
import type { Team } from "@/lib/types";

export function AddMemberModal({
  team,
  onClose,
  onAdded,
}: {
  team: Team;
  onClose: () => void;
  onAdded: () => void;
}) {
  const g = useGinno();
  const tc = useTranslations("chat");
  const [agentId, setAgentId] = useState<string>(g.agents[0]?.id ?? "dev");
  const [displayName, setDisplayName] = useState("");
  const [role, setRole] = useState<"leader" | "member">("member");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async () => {
    if (busy) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await api.addTeamMember(team.id, {
        agent_id: agentId,
        display_name: displayName.trim(),
        role,
      });
      if (r.ok) onAdded();
      else setErr(r.error || tc("team.addFailed"));
    } catch {
      setErr(tc("team.addFailed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onMouseDown={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div
        className="w-full max-w-md rounded-xl border border-line bg-card p-4 shadow-2xl"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="text-sm font-semibold text-txt">{tc("team.addMember")}</div>

        <label className="mt-3 block text-xs text-muted">{tc("team.agentLabel")}</label>
        <div className="mt-1 flex flex-wrap gap-1.5">
          {g.agents.map((a) => {
            const sel = a.id === agentId;
            const hex = agentHex(a.color);
            return (
              <button
                key={a.id}
                onClick={() => setAgentId(a.id)}
                className="flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition-colors"
                style={
                  sel
                    ? { borderColor: hex, background: hex + "1a", color: hex }
                    : { borderColor: "var(--line,#262b34)", color: "var(--muted,#8b93a1)" }
                }
              >
                <span className="h-1.5 w-1.5 rounded-full" style={{ background: hex }} />
                {a.name}
              </button>
            );
          })}
        </div>
        <div className="mt-1.5 text-[10px] leading-relaxed text-faint">{tc("team.multiInstanceHint")}</div>

        <label className="mt-3 block text-xs text-muted">{tc("team.displayNameLabel")}</label>
        <input
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
          placeholder={tc("team.displayNamePlaceholder")}
          className="mt-1 w-full rounded-lg border border-line2 bg-base px-3 py-1.5 text-sm text-txt outline-none focus:border-violet/50"
        />

        <label className="mt-3 block text-xs text-muted">{tc("team.roleLabel")}</label>
        <div className="mt-1 flex gap-1.5">
          {(["member", "leader"] as const).map((r) => (
            <button
              key={r}
              onClick={() => setRole(r)}
              className={`rounded-full border px-2.5 py-1 text-xs transition-colors ${
                role === r
                  ? "border-violet/50 bg-violet/10 text-violet"
                  : "border-line text-muted hover:text-txt"
              }`}
            >
              {tc(r === "leader" ? "team.roleLeader" : "team.roleMember")}
            </button>
          ))}
        </div>

        {err && <div className="mt-3 text-xs text-red">{err}</div>}

        <div className="mt-4 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:text-txt"
          >
            {tc("team.cancel")}
          </button>
          <button
            onClick={submit}
            disabled={busy}
            className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-40"
          >
            {busy ? tc("team.adding") : tc("team.add")}
          </button>
        </div>
      </div>
    </div>
  );
}
