"use client";

// TeamFromPromptModal — V2 P2-2：一句话 → 小队（先预览，后创建）。
//
// 两步是无副作用的：`api.generateTeam` 只让模型起草一份 TeamSpec，确认前
// 不写任何东西；用户在预览里改完（名称 / 章程 / 成员 / 首个任务）点
// 「确认创建」才调 `api.createTeamFromSpec`。半成品团队无法重命名，比没有
// 团队更糟，所以生成与创建必须分开。
//
// 领队数量在客户端先归一（恰好一位）：生成结果可能带 0 或多个 leader，切换
// 角色时也顺手把其他成员降级，服务端仍会再校验一次。

import { useState } from "react";
import { Loader2, Plus, Sparkles, Trash2, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { Team } from "@/lib/types";

export const MAX_PROMPT_MEMBERS = 6;

type EditableMember = {
  agent_id: string;
  display_name: string;
  role: "leader" | "member";
  instructions: string;
};

type Draft = {
  name: string;
  charter_md: string;
  members: EditableMember[];
  first_task: string;
};

/** 创建成功后的回执（TeamShell 用它决定选中哪个团队、显示什么提示）。 */
export interface TeamCreateMeta {
  name: string;
  memberErrors: string[];
  firstTaskDelivered: boolean;
  firstTaskRequested: boolean;
}

/** 把模型给的 spec 收成可编辑草稿：agent id 回落到现有 agent，领队唯一。 */
function toDraft(spec: api.TeamSpec, agentIds: string[], fallback: string): Draft {
  const pick = (id: string) => (agentIds.includes(id) ? id : fallback);
  let members: EditableMember[] = (spec.members ?? [])
    .slice(0, MAX_PROMPT_MEMBERS)
    .map((m) => ({
      agent_id: pick(m.agent_id),
      display_name: m.display_name ?? "",
      role: m.role === "leader" ? "leader" : "member",
      instructions: m.instructions ?? "",
    }));
  if (members.length === 0) {
    members = [{ agent_id: fallback, display_name: "", role: "leader", instructions: "" }];
  }
  let leaderSeen = false;
  members = members.map((m) => {
    if (m.role === "leader" && !leaderSeen) {
      leaderSeen = true;
      return m;
    }
    return { ...m, role: "member" as const };
  });
  if (!leaderSeen) members[0] = { ...members[0], role: "leader" };
  return {
    name: spec.name ?? "",
    charter_md: spec.charter_md ?? "",
    members,
    first_task: spec.first_task ?? "",
  };
}

export function TeamFromPromptModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (team: Team | null, meta: TeamCreateMeta) => void;
}) {
  const g = useGinno();
  const tc = useTranslations("chat");
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [creating, setCreating] = useState(false);

  const agentIds = g.agents.map((a) => a.id);
  const fallbackAgent = g.agents[0]?.id ?? "dev";

  const generate = async () => {
    const p = prompt.trim();
    if (!p || busy) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await api.generateTeam(p);
      if (r.ok && r.spec) {
        setDraft(toDraft(r.spec, agentIds, fallbackAgent));
      } else {
        // 后端在 422/400 时把 {ok, error, raw} 放在 detail 里。
        setErr(r.detail?.error || tc("team.promptFailed"));
      }
    } catch {
      setErr(tc("team.promptFailed"));
    } finally {
      setBusy(false);
    }
  };

  const updateMember = (index: number, patch: Partial<EditableMember>) => {
    setDraft((d) =>
      d
        ? { ...d, members: d.members.map((m, i) => (i === index ? { ...m, ...patch } : m)) }
        : d,
    );
  };

  // 恰好一位领队：选 leader 时把其余成员统一降级。
  const setRole = (index: number, role: "leader" | "member") => {
    setDraft((d) =>
      d
        ? {
            ...d,
            members: d.members.map((m, i) => {
              if (i === index) return { ...m, role };
              return role === "leader" && m.role === "leader"
                ? { ...m, role: "member" as const }
                : m;
            }),
          }
        : d,
    );
  };

  const addMember = () => {
    setDraft((d) => {
      if (!d || d.members.length >= MAX_PROMPT_MEMBERS) return d;
      const used = new Set(d.members.map((m) => m.agent_id));
      const agent = g.agents.find((a) => !used.has(a.id)) ?? g.agents[0];
      return {
        ...d,
        members: [
          ...d.members,
          {
            agent_id: agent?.id ?? fallbackAgent,
            display_name: "",
            role: "member" as const,
            instructions: "",
          },
        ],
      };
    });
  };

  const removeMember = (index: number) => {
    setDraft((d) => {
      if (!d) return d;
      const members = d.members.filter((_, i) => i !== index);
      // 删掉领队后把第一位补成领队，避免提交时零领队。
      if (members.length > 0 && !members.some((m) => m.role === "leader")) {
        members[0] = { ...members[0], role: "leader" };
      }
      return { ...d, members };
    });
  };

  const create = async () => {
    if (!draft || creating) return;
    const name = draft.name.trim();
    if (!name) {
      setErr(tc("team.nameRequired"));
      return;
    }
    if (draft.members.length === 0) {
      setErr(tc("team.membersRequired"));
      return;
    }
    if (draft.members.filter((m) => m.role === "leader").length !== 1) {
      setErr(tc("team.leaderRequired"));
      return;
    }
    setCreating(true);
    setErr(null);
    const firstTask = draft.first_task.trim();
    try {
      const r = await api.createTeamFromSpec({
        name,
        charter_md: draft.charter_md,
        members: draft.members.map((m) => ({
          agent_id: m.agent_id,
          display_name:
            m.display_name.trim() || g.agents.find((a) => a.id === m.agent_id)?.name || "",
          role: m.role,
          instructions: m.instructions,
        })),
        first_task: firstTask,
      });
      if (!r.ok) {
        setErr(r.detail?.error || tc("team.createFailed"));
        return;
      }
      onCreated((r.team as Team | undefined) ?? null, {
        name,
        memberErrors: r.member_errors ?? [],
        firstTaskDelivered: !!r.first_task_delivered,
        firstTaskRequested: firstTask.length > 0,
      });
    } catch {
      setErr(tc("team.createFailed"));
    } finally {
      setCreating(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onMouseDown={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={tc("team.promptTitle")}
    >
      <div
        className="flex max-h-[88vh] w-full max-w-2xl flex-col rounded-xl border border-line bg-card p-4 shadow-2xl"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 shrink-0 text-violet" />
          <span className="min-w-0 flex-1 text-sm font-semibold text-txt">
            {draft ? tc("team.promptPreviewTitle") : tc("team.promptTitle")}
          </span>
          <button
            onClick={onClose}
            aria-label={tc("team.cancel")}
            className="shrink-0 rounded-md p-1 text-muted hover:bg-card2 hover:text-txt"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="mt-3 min-h-0 flex-1 overflow-y-auto pr-0.5">
          {!draft ? (
            <>
              <div className="text-[11px] leading-relaxed text-faint">{tc("team.promptHint")}</div>
              <textarea
                autoFocus
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                rows={3}
                placeholder={tc("team.promptPlaceholder")}
                className="mt-2 w-full resize-y rounded-lg border border-line2 bg-base px-3 py-2 text-sm leading-relaxed text-txt outline-none focus:border-violet/50"
              />
            </>
          ) : (
            <div className="space-y-3">
              <div>
                <label className="block text-xs text-muted">{tc("team.nameLabel")}</label>
                <input
                  autoFocus
                  value={draft.name}
                  onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                  placeholder={tc("team.namePlaceholder")}
                  className="mt-1 w-full rounded-lg border border-line2 bg-base px-3 py-1.5 text-sm text-txt outline-none focus:border-violet/50"
                />
              </div>

              <div>
                <label className="block text-xs text-muted">{tc("team.charterLabel")}</label>
                <textarea
                  value={draft.charter_md}
                  onChange={(e) => setDraft({ ...draft, charter_md: e.target.value })}
                  rows={4}
                  placeholder={tc("team.charterPlaceholder")}
                  className="mt-1 w-full resize-y rounded-lg border border-line2 bg-base px-3 py-2 font-mono text-[11px] leading-relaxed text-txt outline-none focus:border-violet/50"
                />
              </div>

              <div>
                <div className="flex items-center gap-2">
                  <label className="block text-xs text-muted">
                    {tc("team.membersLabel", {
                      count: draft.members.length,
                      max: MAX_PROMPT_MEMBERS,
                    })}
                  </label>
                  <button
                    onClick={addMember}
                    disabled={draft.members.length >= MAX_PROMPT_MEMBERS}
                    title={
                      draft.members.length >= MAX_PROMPT_MEMBERS
                        ? tc("team.capReached", { max: MAX_PROMPT_MEMBERS })
                        : undefined
                    }
                    className="ml-auto flex items-center gap-1 rounded-md border border-line2 px-2 py-1 text-[11px] text-muted transition-colors hover:border-violet/50 hover:text-violet disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    <Plus className="h-3 w-3" />
                    {tc("team.addMember")}
                  </button>
                </div>

                <div className="mt-1.5 space-y-1.5">
                  {draft.members.map((m, i) => (
                    <div key={i} className="rounded-lg border border-line bg-base/40 p-2.5">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <select
                          value={m.agent_id}
                          aria-label={tc("team.agentLabel")}
                          onChange={(e) => updateMember(i, { agent_id: e.target.value })}
                          disabled={g.agents.length === 0}
                          className="rounded-lg border border-line2 bg-base px-2 py-1 text-xs text-txt outline-none focus:border-violet/50"
                        >
                          {g.agents.length === 0 && <option value={m.agent_id}>{tc("team.agentNeeded")}</option>}
                          {g.agents.map((a) => (
                            <option key={a.id} value={a.id} className="bg-panel text-txt">
                              {a.name}
                            </option>
                          ))}
                        </select>
                        <input
                          value={m.display_name}
                          onChange={(e) => updateMember(i, { display_name: e.target.value })}
                          placeholder={tc("team.displayNamePlaceholder")}
                          className="min-w-0 flex-1 rounded-lg border border-line2 bg-base px-2 py-1 text-xs text-txt outline-none focus:border-violet/50"
                        />
                        <select
                          value={m.role}
                          aria-label={tc("team.roleLabel")}
                          onChange={(e) => setRole(i, e.target.value as "leader" | "member")}
                          className="rounded-lg border border-line2 bg-base px-2 py-1 text-xs text-txt outline-none focus:border-violet/50"
                        >
                          <option value="leader" className="bg-panel text-txt">
                            {tc("team.roleLeader")}
                          </option>
                          <option value="member" className="bg-panel text-txt">
                            {tc("team.roleMember")}
                          </option>
                        </select>
                        <button
                          onClick={() => removeMember(i)}
                          title={tc("team.removeMember")}
                          aria-label={tc("team.removeMember")}
                          className="rounded-md p-1 text-muted transition-colors hover:bg-card2 hover:text-red"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                      <textarea
                        value={m.instructions}
                        onChange={(e) => updateMember(i, { instructions: e.target.value })}
                        rows={2}
                        placeholder={tc("team.instructionsPlaceholder")}
                        aria-label={tc("team.instructionsLabel")}
                        className="mt-1.5 w-full resize-y rounded-lg border border-line2 bg-base px-2 py-1.5 text-[11px] leading-relaxed text-txt outline-none focus:border-violet/50"
                      />
                    </div>
                  ))}
                </div>
              </div>

              <div>
                <label className="block text-xs text-muted">{tc("team.firstTaskLabel")}</label>
                <input
                  value={draft.first_task}
                  onChange={(e) => setDraft({ ...draft, first_task: e.target.value })}
                  placeholder={tc("team.firstTaskPlaceholder")}
                  className="mt-1 w-full rounded-lg border border-line2 bg-base px-3 py-1.5 text-sm text-txt outline-none focus:border-violet/50"
                />
              </div>
            </div>
          )}

          {err && <div className="mt-3 text-xs text-red">{err}</div>}
        </div>

        <div className="mt-4 flex justify-end gap-2 border-t border-line pt-3">
          {draft ? (
            <button
              onClick={() => {
                setDraft(null);
                setErr(null);
              }}
              className="mr-auto rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:text-txt"
            >
              {tc("team.promptRegenerate")}
            </button>
          ) : null}
          <button
            onClick={onClose}
            className="rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:text-txt"
          >
            {tc("team.cancel")}
          </button>
          {!draft ? (
            <button
              onClick={() => void generate()}
              disabled={!prompt.trim() || busy}
              className="flex items-center gap-1 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-40"
            >
              {busy && <Loader2 className="h-3 w-3 animate-spin" />}
              {busy ? tc("team.promptGenerating") : tc("team.promptGenerate")}
            </button>
          ) : (
            <button
              onClick={() => void create()}
              disabled={creating}
              className="flex items-center gap-1 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-40"
            >
              {creating && <Loader2 className="h-3 w-3 animate-spin" />}
              {creating ? tc("team.creating") : tc("team.confirmCreate")}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
