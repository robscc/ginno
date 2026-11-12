"use client";

import { useEffect, useState } from "react";
import { Boxes, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/api/workspace";

/**
 * 上下文管理（Session Tool Policy P1，2026-11-12 拍板：入口在输入框）。
 * 黑名单制勾选 MCP servers / 非内置 skills；minimal 预设一键；保存走
 * PUT /api/sessions/{id}/tool_policy，下一 turn 生效（runtime 漂移重建）。
 * 内置 skill（browser）与 builtin 工具不参与，不在此列出。
 */
type McpStatus = { name: string; tools?: number | string; enabled?: boolean };
type SkillRow = { name: string; description?: string };

const BUILTIN_SKILLS = new Set(["browser"]);
// 粗估：单个 MCP 工具 schema ≈ 330 token（68 工具 ≈ 22k 的经验基线）。
const TOK_PER_TOOL = 330;

export function ToolPolicyPopover({
  sessionId,
  onClose,
}: {
  sessionId: string;
  onClose: () => void;
}) {
  const t = useTranslations("composer.toolPolicy");
  const [mode, setMode] = useState<"all" | "minimal" | "custom">("all");
  const [mcp, setMcp] = useState<McpStatus[]>([]);
  const [skills, setSkills] = useState<SkillRow[]>([]);
  const [mcpOff, setMcpOff] = useState<Set<string>>(new Set());
  const [skOff, setSkOff] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [mcpRes, skillsRes, sess] = await Promise.all([
          api.mcpStatus(),
          api.listSkills(),
          fetch(`/api/sessions/${sessionId}`).then((r) => r.json()),
        ]);
        if (!alive) return;
        setMcp(
          (mcpRes.status || []).map((s: Record<string, unknown>) => ({
            name: String(s.name),
            tools: (s.tools as number) ?? 0,
            enabled: !!s.enabled,
          })),
        );
        setSkills(
          (Array.isArray(skillsRes) ? skillsRes : []).filter(
            (s: SkillRow) => !BUILTIN_SKILLS.has(s.name),
          ),
        );
        const pol = sess?.tool_policy || {};
        setMode(pol.mode || "all");
        setMcpOff(new Set(pol.mcp_disabled || []));
        setSkOff(new Set(pol.skills_disabled || []));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  const mcpToolCount = (name: string) => {
    const s = mcp.find((x) => x.name === name);
    return typeof s?.tools === "number" ? s.tools : 0;
  };
  const savedTokens =
    mode === "minimal"
      ? mcp.reduce((a, s) => a + mcpToolCount(s.name), 0) * TOK_PER_TOOL
      : [...mcpOff].reduce((a, n) => a + mcpToolCount(n), 0) * TOK_PER_TOOL +
        skOff.size * 30;

  async function save() {
    setSaving(true);
    setSaved(false);
    try {
      const r = await api.setToolPolicy(sessionId, {
        mode,
        mcp_disabled: mode === "custom" ? [...mcpOff] : [],
        skills_disabled: mode === "custom" ? [...skOff] : [],
      });
      if (r.ok) {
        setSaved(true);
        setTimeout(onClose, 600);
      }
    } finally {
      setSaving(false);
    }
  }

  const chip = (on: boolean, label: string, onClick: () => void, key: string) => (
    <button
      key={key}
      onClick={onClick}
      className={
        "rounded-full px-2.5 py-1 text-xs transition-colors " +
        (on
          ? "bg-accent/15 text-accent ring-1 ring-accent"
          : "bg-line2/60 text-txt hover:bg-line2")
      }
    >
      {label}
    </button>
  );

  return (
    <div className="absolute bottom-full right-0 z-50 mb-1 w-80 rounded-lg border border-line bg-card p-2.5 shadow-xl">
      <div className="mb-2 flex items-center gap-1.5 text-xs font-medium text-txt">
        <Boxes className="h-3.5 w-3.5" />
        {t("title")}
        <span className="ml-auto text-[10px] text-faint">{t("hint")}</span>
      </div>
      {loading ? (
        <div className="flex items-center justify-center py-6 text-muted">
          <Loader2 className="h-4 w-4 animate-spin" />
        </div>
      ) : (
        <>
          <div className="mb-2 flex gap-1">
            {(["all", "minimal", "custom"] as const).map((m) =>
              chip(
                mode === m,
                m === "all" ? t("modeAll") : m === "minimal" ? t("modeMinimal") : t("modeCustom"),
                () => setMode(m),
                m,
              ),
            )}
          </div>
          {mode === "custom" && (
            <div className="max-h-72 overflow-y-auto text-xs">
              <div className="pb-1 text-[10px] uppercase tracking-wide text-faint">
                {t("mcpSection")}
              </div>
              {mcp.map((s) => (
                <label
                  key={s.name}
                  className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 hover:bg-card2"
                >
                  <input
                    type="checkbox"
                    checked={!mcpOff.has(s.name)}
                    onChange={(e) =>
                      setMcpOff((m) => {
                        const n = new Set(m);
                        e.target.checked ? n.delete(s.name) : n.add(s.name);
                        return n;
                      })
                    }
                  />
                  <span className="min-w-0 flex-1 truncate text-txt">{s.name}</span>
                  <span className="text-faint">
                    {mcpToolCount(s.name)} {t("toolsUnit")}
                  </span>
                </label>
              ))}
              <div className="pb-1 pt-2 text-[10px] uppercase tracking-wide text-faint">
                {t("skillsSection")}
              </div>
              {skills.map((s) => (
                <label
                  key={s.name}
                  className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 hover:bg-card2"
                >
                  <input
                    type="checkbox"
                    checked={!skOff.has(s.name)}
                    onChange={(e) =>
                      setSkOff((m) => {
                        const n = new Set(m);
                        e.target.checked ? n.delete(s.name) : n.add(s.name);
                        return n;
                      })
                    }
                  />
                  <span className="min-w-0 flex-1 truncate text-txt">{s.name}</span>
                </label>
              ))}
            </div>
          )}
          <div className="mt-2 flex items-center gap-2 border-t border-line2/50 pt-2 text-[11px]">
            <span className="text-emerald-600 dark:text-emerald-400">
              {t("saveTokens", { n: (savedTokens / 1000).toFixed(1) })}
            </span>
            <button
              onClick={() => void save()}
              disabled={saving}
              className="ml-auto rounded bg-accent px-2.5 py-1 text-xs text-white disabled:opacity-50"
            >
              {saved ? t("saved") : t("save")}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
