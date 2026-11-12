"use client";

import { useEffect, useRef, useState } from "react";
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
  const [err, setErr] = useState("");
  // 勾选即自动保存（2026-11-13 修复：手动保存按钮摩擦——uncheck 后直接
  // 点外部关闭=未保存，重开回读旧 policy 全 check，被用户报 bug）。
  const autoTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingRef = useRef<[Set<string>, Set<string>] | null>(null);

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
      if (autoTimer.current) {
        // 防抖窗内关闭：立即 flush 待存（否则最后一下勾选丢失）
        clearTimeout(autoTimer.current);
        const pending = pendingRef.current;
        if (pending) void save(pending[0], pending[1]);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  const toolCount = (name: string) => {
    const s = mcp.find((x) => x.name === name);
    return typeof s?.tools === "number" ? s.tools : 0;
  };
  const offTools = [...mcpOff].reduce((a, n) => a + toolCount(n), 0);
  const savedK = (offTools * TOK_PER_TOOL) / 1000;

  async function save(nextMcp: Set<string>, nextSk: Set<string>, closeAfter = false) {
    setSaving(true);
    setSaved(false);
    setErr("");
    try {
      const r = await api.setToolPolicy(sessionId, {
        mode: nextMcp.size || nextSk.size ? "custom" : "all",
        mcp_disabled: [...nextMcp],
        skills_disabled: [...nextSk],
      });
      if (r.ok) {
        setSaved(true);
        if (closeAfter) setTimeout(onClose, 400);
      } else {
        // 版本错配/网络失败不再静默（PUT 405 时用户以为存住了）
        setErr(t("saveFail"));
      }
    } catch {
      setErr(t("saveFail"));
    } finally {
      setSaving(false);
    }
  }

  /** 勾选变化 → 400ms 防抖自动落库（勾选即生效，无按钮摩擦）。 */
  function scheduleAutoSave(nextMcp: Set<string>, nextSk: Set<string>) {
    if (autoTimer.current) clearTimeout(autoTimer.current);
    pendingRef.current = [nextMcp, nextSk];
    autoTimer.current = setTimeout(() => {
      pendingRef.current = null;
      void save(nextMcp, nextSk);
    }, 400);
  }

  const section = (label: string) => (
    <div className="sticky top-0 z-10 bg-card/95 pb-1 pt-1.5 text-[10px] font-medium uppercase tracking-wider text-faint backdrop-blur">
      {label}
    </div>
  );

  /** iOS 风 switch（2026-11-13：checkbox 换开关——勾选态一眼可辨）。 */
  const switchEl = (on: boolean, toggle: () => void, key: string) => (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      onClick={(e) => {
        e.preventDefault(); // 整行 label 点击统一入口，避免双触发
        toggle();
      }}
      className={
        "relative h-[18px] w-[32px] shrink-0 rounded-full transition-colors duration-150 " +
        (on ? "bg-accent" : "bg-line2")
      }
      tabIndex={-1}
      key={key}
    >
      <span
        className={
          "absolute top-[2px] h-[14px] w-[14px] rounded-full bg-white shadow-sm transition-transform duration-150 " +
          (on ? "translate-x-[16px]" : "translate-x-[2px]")
        }
      />
    </button>
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
      {switchEl(on, toggle, key)}
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
                    scheduleAutoSave(n, skOff);
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
                    scheduleAutoSave(mcpOff, n);
                    return n;
                  }),
              ),
            )}
          </div>
          <div className="mt-2 flex items-center gap-2 border-t border-line2/50 pt-2 text-[11px]">
            <span
              className={
                err ? "text-red" : savedK > 0
                  ? "text-emerald-600 dark:text-emerald-400"
                  : "text-faint"
              }
            >
              {err || t("saveTokens", { n: savedK.toFixed(1) })}
            </span>
            <button
              onClick={() => void save(mcpOff, skOff, true)}
              disabled={saving}
              className="ml-auto rounded-md bg-accent px-3 py-1 text-xs text-white transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              {saved ? `✓ ${t("saved")}` : t("done")}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
