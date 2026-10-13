"use client";

// NewAgentModal — 团队流程内联新建 Agent（2026-10-13 用户需求：New Team
// 界面允许新增一个独立配置的新 Agent）。
//
// 与 Settings → Agents 的完整编辑器分工：这里是「招人」的轻量路径——只收
// 编制必需项（id/显示名/persona/模型/颜色），tools_allow 等进阶项走
// POST /api/agents 默认值（["*"]），后续在 Settings 里精调。建完回调
// onCreated，调用方（NewTeamModal / AddMemberModal）刷新 agent 列表并
// 选中它——新建即可用，不用离开当前流程。

import { useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import { AGENT_HEX, agentHex } from "@/lib/theme";
import type { AgentConfig } from "@/lib/types";

export function NewAgentModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (agent: AgentConfig) => void;
}) {
  const g = useGinno();
  const tc = useTranslations("chat");
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [binding, setBinding] = useState(""); // "provider|model"，空 = 跟随全局默认
  const [color, setColor] = useState("violet");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // 与 AgentsSettings 同一套模型枚举：enabled provider × models[]。
  const modelGroups = useMemo(
    () =>
      Object.entries(g.providers)
        .filter(([, p]) => p.enabled)
        .map(([pid, p]) => ({
          pid,
          name: p.name || pid,
          models: (
            Array.isArray(p.models) && p.models.length
              ? p.models
              : [p.default_model || p.model || ""]
          ).filter((m): m is string => !!m),
        })),
    [g.providers],
  );

  const slugOk = /^[a-z0-9][a-z0-9_-]*$/.test(id.trim());

  const submit = async () => {
    if (busy || !slugOk) return;
    setBusy(true);
    setErr(null);
    const [provider, model] = binding ? binding.split("|") : ["custom", ""];
    try {
      const r = await api.createAgent({
        id: id.trim(),
        name: name.trim() || id.trim(),
        system_prompt: systemPrompt.trim(),
        // provider_explicit：这里选了具体供应商就是刻意绑定；没选（custom）
        // 跟随全局默认。
        provider,
        provider_explicit: !!binding,
        model,
        color,
      });
      if (r.ok && r.agent) {
        await g.reloadAgents();
        onCreated(r.agent);
      } else {
        setErr(r.error || tc("team.agent.createFailed"));
      }
    } catch {
      setErr(tc("team.agent.createFailed"));
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
        className="flex max-h-[86vh] w-full max-w-md flex-col rounded-xl border border-line bg-card p-4 shadow-2xl"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="text-sm font-semibold text-txt">{tc("team.agent.title")}</div>
        <div className="mt-0.5 text-[10px] leading-relaxed text-faint">
          {tc("team.agent.hint")}
        </div>

        <div className="mt-3 grid grid-cols-[1fr_1fr] gap-2">
          <div>
            <label className="block text-xs text-muted">{tc("team.agent.idLabel")}</label>
            <input
              autoFocus
              value={id}
              onChange={(e) => setId(e.target.value.toLowerCase())}
              placeholder="reviewer"
              className="mt-1 w-full rounded-lg border border-line2 bg-base px-2.5 py-1.5 text-sm text-txt outline-none focus:border-violet/50"
            />
            {id && !slugOk && (
              <div className="mt-1 text-[10px] text-red">{tc("team.agent.idInvalid")}</div>
            )}
          </div>
          <div>
            <label className="block text-xs text-muted">{tc("team.agent.nameLabel")}</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={tc("team.agent.namePlaceholder")}
              className="mt-1 w-full rounded-lg border border-line2 bg-base px-2.5 py-1.5 text-sm text-txt outline-none focus:border-violet/50"
            />
          </div>
        </div>

        <label className="mt-3 block text-xs text-muted">{tc("team.agent.personaLabel")}</label>
        <textarea
          value={systemPrompt}
          onChange={(e) => setSystemPrompt(e.target.value)}
          rows={5}
          placeholder={tc("team.agent.personaPlaceholder")}
          className="mt-1 w-full resize-y rounded-lg border border-line2 bg-base px-2.5 py-1.5 text-xs leading-relaxed text-txt outline-none focus:border-violet/50"
        />

        <label className="mt-3 block text-xs text-muted">{tc("team.agent.modelLabel")}</label>
        <select
          value={binding}
          onChange={(e) => setBinding(e.target.value)}
          className="mt-1 w-full rounded-lg border border-line2 bg-base px-2.5 py-1.5 text-sm text-txt outline-none focus:border-violet/50"
        >
          <option value="">{tc("team.agent.modelDefault")}</option>
          {modelGroups.map((grp) => (
            <optgroup key={grp.pid} label={grp.name}>
              {grp.models.map((m) => (
                <option key={`${grp.pid}|${m}`} value={`${grp.pid}|${m}`}>
                  {m}
                </option>
              ))}
            </optgroup>
          ))}
        </select>

        <label className="mt-3 block text-xs text-muted">{tc("team.agent.colorLabel")}</label>
        <div className="mt-1 flex flex-wrap gap-1.5">
          {Object.keys(AGENT_HEX).map((c) => (
            <button
              key={c}
              onClick={() => setColor(c)}
              className="rounded-md border p-0.5"
              style={{ borderColor: color === c ? agentHex(c) : "transparent" }}
              title={c}
            >
              <span
                className="block h-4 w-4 rounded"
                style={{ background: agentHex(c) }}
              />
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
            disabled={!slugOk || busy}
            className="flex items-center gap-1.5 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-40"
          >
            {busy && <Loader2 className="h-3 w-3 animate-spin" />}
            {tc("team.agent.create")}
          </button>
        </div>
      </div>
    </div>
  );
}
