"use client";

import { useEffect, useState } from "react";
import { Boxes, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/api/workspace";

/**
 * 上下文管理（Tool Policy P1，2026-11-12 二轮改版：去掉预设档，直接勾选）。
 * 勾掉 = 禁用（黑名单）；全勾 = all。MCP 服务器/非内置 skills 两节，
 * 内置（browser、builtin 工具）不参与不列出。保存走 PUT tool_policy，
 * 下一 turn 生效。
 */
type McpStatus = { name: string; tools?: number | string };
type SkillRow = { name: string; description?: string };

const BUILTIN_SKILLS = new Set(["browser"]);
const TOK_PER_TOOL = 330;

export function ToolPolicyPopover({
  sessionId,
  onClose,
}: {
  sessionId: string;
  onClose: () => void;
}) {
  const t = useTranslations("composer.toolPolicy");
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
          api.getSessionDetail(sessionId),
        ]);
        if (!alive) return;
        setMcp(
          (mcpRes.status || []).map((s: Record<string, unknown>) => ({
            name: String(s.name),
            tools: (s.tools as number) ?? 0,
          })),
        );
        setSkills(
          (Array.isArray(skillsRes) ? skillsRes : []).filter(
            (s: SkillRow) => !BUILTIN_SKILLS.has(s.name),
          ),
        );
        const pol =
          (sess?.tool_policy as Record<string, unknown> | undefined) || {};
        setMcpOff(new Set((pol.mcp_disabled as string[]) || []));
        setSkOff(new Set((pol.skills_disabled as string[]) || []));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  const toolCount = (name: string) => {
    const s = mcp.find((x) => x.name === name);
    return typeof s?.tools === "number" ? s.tools : 0;
  };
  const offTools = [...mcpOff].reduce((a, n) => a + toolCount(n), 0);
  const savedK = (offTools * TOK_PER_TOOL) / 1000;

  async function save() {
    setSaving(true);
    setSaved(false);
    try {
      const r = await api.setToolPolicy(sessionId, {
        mode: mcpOff.size || skOff.size ? "custom" : "all",
        mcp_disabled: [...mcpOff],
        skills_disabled: [...skOff],
      });
      if (r.ok) {
        setSaved(true);
        setTimeout(onClose, 900);
      }
    } finally {
      setSaving(false);
    }
  }

  const section = (label: string) => (
    <div className="sticky top-0 z-10 bg-card/95 pb-1 pt-1.5 text-[10px] font-medium uppercase tracking-wider text-faint backdrop-blur">
      {label}
    </div>
  );

  const row = (
    key: string,
    name: string,
    on: boolean,
    toggle: () => void,
    right?: string,
    dim = false,
  ) => (
    <label
      key={key}
      className={
        "flex cursor-pointer items-center gap-2.5 rounded-md px-2 py-1.5 transition-colors hover:bg-card2/60 " +
        (dim ? "opacity-45" : "")
      }
    >
      <input type="checkbox" checked={on} onChange={toggle} className="h-3.5 w-3.5" />
      <span className="min-w-0 flex-1 truncate text-xs text-txt">{name}</span>
      {right && (
        <span className="shrink-0 rounded bg-line2/50 px-1.5 py-px text-[10px] tabular-nums text-faint">
          {right}
        </span>
      )}
    </label>
  );

  return (
    <div className="absolute bottom-full right-0 z-50 mb-1 w-80 rounded-xl border border-line bg-card p-3 shadow-2xl">
      <div className="mb-1 flex items-center gap-1.5 text-xs font-medium text-txt">
        <Boxes className="h-3.5 w-3.5 text-accent" />
        {t("title")}
        <span className="ml-auto text-[10px] font-normal text-faint">
          {t("hint")}
        </span>
      </div>
      <div className="mb-1 text-[10px] text-faint">{t("hintOff")}</div>
      {loading ? (
        <div className="flex items-center justify-center py-8 text-muted">
          <Loader2 className="h-4 w-4 animate-spin" />
        </div>
      ) : (
        <>
          <div className="max-h-64 space-y-0.5 overflow-y-auto pr-1">
            {section(t("mcpSection"))}
            {mcp.map((s) =>
              row(
                s.name,
                s.name,
                !mcpOff.has(s.name),
                () =>
                  setMcpOff((m) => {
                    const n = new Set(m);
                    n.has(s.name) ? n.delete(s.name) : n.add(s.name);
                    return n;
                  }),
                toolCount(s.name) > 0
                  ? t("toolsUnit", { n: toolCount(s.name) })
                  : undefined,
                toolCount(s.name) === 0,
              ),
            )}
            {section(t("skillsSection"))}
            {skills.map((s) =>
              row(
                s.name,
                s.name,
                !skOff.has(s.name),
                () =>
                  setSkOff((m) => {
                    const n = new Set(m);
                    n.has(s.name) ? n.delete(s.name) : n.add(s.name);
                    return n;
                  }),
              ),
            )}
          </div>
          <div className="mt-2 flex items-center gap-2 border-t border-line2/50 pt-2 text-[11px]">
            <span
              className={
                savedK > 0
                  ? "text-emerald-600 dark:text-emerald-400"
                  : "text-faint"
              }
            >
              {t("saveTokens", { n: savedK.toFixed(1) })}
            </span>
            <button
              onClick={() => void save()}
              disabled={saving}
              className="ml-auto rounded-md bg-accent px-3 py-1 text-xs text-white transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              {saved ? `✓ ${t("saved")}` : t("save")}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
