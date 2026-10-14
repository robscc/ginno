"use client";

// NewTeamModal — 建团队（agent-team-design.md §5.1）。填团队名 + 选 Lead
// persona（主会话的 agent，待决问题 2 的当前取舍：默认 dev）。建团队时
// runtime 自动建主会话 session（api/teams.py create_team_endpoint）。

import { useState } from "react";
import { UserPlus } from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import { agentHex } from "@/lib/theme";
import type { Team } from "@/lib/types";
import { NewAgentModal } from "./NewAgentModal";

export function NewTeamModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (team: Team) => void;
}) {
  const g = useGinno();
  const tc = useTranslations("chat");
  const [name, setName] = useState("");
  const [agentId, setAgentId] = useState<string>(g.agents[0]?.id ?? "dev");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [newAgentOpen, setNewAgentOpen] = useState(false);

  const submit = async () => {
    if (!name.trim() || busy) return;
    setBusy(true);
    setErr(null);
    try {
      // 领队实体化（2026-10-14）：runtime 建队端点自动任命实体领队（服务端
      // 保证，任何客户端不落空），前端无需重复调用。
      const r = await api.createTeam({ name: name.trim(), agent_id: agentId });
      if (r.ok && r.team) onCreated(r.team);
      else setErr(r.error || tc("team.createFailed"));
    } catch {
      setErr(tc("team.createFailed"));
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
        <div className="text-sm font-semibold text-txt">{tc("team.newTeam")}</div>

        <label className="mt-3 block text-xs text-muted">{tc("team.nameLabel")}</label>
        <input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
          placeholder={tc("team.namePlaceholder")}
          className="mt-1 w-full rounded-lg border border-line2 bg-base px-3 py-1.5 text-sm text-txt outline-none focus:border-violet/50"
        />

        <label className="mt-3 block text-xs text-muted">{tc("team.leadLabel")}</label>
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
          {/* 现场招人：新建独立配置的 Agent（NewAgentModal），建完即选中 */}
          <button
            onClick={() => setNewAgentOpen(true)}
            className="flex items-center gap-1.5 rounded-full border border-dashed px-2.5 py-1 text-xs text-faint transition-colors hover:border-violet/50 hover:text-violet"
          >
            <UserPlus className="h-3 w-3" />
            {tc("team.agent.newAgent")}
          </button>
        </div>
        <div className="mt-1.5 text-[10px] leading-relaxed text-faint">{tc("team.leadHint")}</div>

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
            disabled={!name.trim() || busy}
            className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-40"
          >
            {busy ? tc("team.creating") : tc("team.create")}
          </button>
        </div>

        {newAgentOpen && (
          <NewAgentModal
            onClose={() => setNewAgentOpen(false)}
            onCreated={(a) => {
              setNewAgentOpen(false);
              setAgentId(a.id);
            }}
          />
        )}
      </div>
    </div>
  );
}
