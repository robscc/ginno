"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Boxes,
  Check,
  ChevronDown,
  ChevronRight,
  Loader2,
  Search,
} from "lucide-react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/api/workspace";
import { Toggle as McpToggle } from "@/components/ui/Toggle";

/**
 * 上下文管理（2026-11-13 P0 重做）。
 *
 * 账本优先：顶部先回答“这一轮 prompt 被什么占满了”，再给可裁剪的开关。
 * - 预算条：used/window + 压缩阈值刻度；分项 = 历史/工具/技能/系统。
 * - 工具：server 级开关 + 展开到工具级（三态：全开/部分/全关）；token
 *   数字来自 /context_breakdown（tiktoken o200k_base，固定项本地实测，
 *   历史 = 上次 prompt_tokens − 固定项）。
 * - 四态：设置中停用 / 未连接 / 连接失败（行内重试）/ 已连接。
 * - 黑名单制：勾掉 = 禁用；内置 skill / builtin 工具不参与、不列出。
 * - 保存 400ms 防抖自动落库；关闭只负责关面板。
 */
type McpServer = {
  name: string;
  tools: number;
  connected: boolean;
  enabled: boolean;
  error: string;
};
type ToolDetail = { name: string; fullName: string; description: string };
type SkillRow = { name: string; description?: string };
type ToolOff = Record<string, Set<string>>;

const BUILTIN_SKILLS = new Set(["browser"]);
const TOK_PER_TOOL_FALLBACK = 330;

type RowState = "on" | "partial" | "off" | "disabled" | "offline" | "failed";

function fmt(n: number): string {
  if (!n) return "0";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}m`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(Math.round(n));
}

export function ToolPolicyPopover({
  sessionId,
  onClose,
}: {
  sessionId: string;
  onClose: () => void;
}) {
  const t = useTranslations("composer.toolPolicy");
  const [mcp, setMcp] = useState<McpServer[]>([]);
  const [skills, setSkills] = useState<SkillRow[]>([]);
  const [bd, setBd] = useState<api.ContextBreakdown | null>(null);
  const [mcpOff, setMcpOff] = useState<Set<string>>(new Set());
  const [toolOff, setToolOff] = useState<ToolOff>({});
  const [skOff, setSkOff] = useState<Set<string>>(new Set());
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [details, setDetails] = useState<Record<string, ToolDetail[]>>({});
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [skillsOpen, setSkillsOpen] = useState(false);
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState("");
  const [retrying, setRetrying] = useState("");
  const autoTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingRef = useRef<[Set<string>, ToolOff, Set<string>] | null>(null);
  const savedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const mapMcp = useCallback(
    (list: Array<Record<string, unknown>> | undefined): McpServer[] =>
      (list || []).map((s) => ({
        name: String(s.name),
        tools: typeof s.tools === "number" ? s.tools : 0,
        connected: Boolean(s.connected),
        enabled: s.enabled !== false,
        error: s.error ? String(s.error) : "",
      })),
    [],
  );

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [mcpRes, skillsRes, sess, breakdown] = await Promise.all([
          api.mcpStatus(),
          api.listSkills(),
          api.getSessionDetail(sessionId),
          api.contextBreakdown(sessionId).catch(() => null),
        ]);
        if (!alive) return;
        setMcp(mapMcp(mcpRes.status));
        setSkills(
          (Array.isArray(skillsRes) ? skillsRes : []).filter(
            (s: SkillRow) => !BUILTIN_SKILLS.has(s.name),
          ),
        );
        const pol =
          (breakdown?.ok ? breakdown.policy : undefined) ||
          ((sess?.tool_policy as Record<string, unknown> | undefined) || {});
        setMcpOff(new Set((pol.mcp_disabled as string[]) || []));
        const rawTools = (pol.mcp_tools_disabled as Record<string, string[]> | undefined) || {};
        setToolOff(
          Object.fromEntries(
            Object.entries(rawTools)
              .filter(([, names]) => Array.isArray(names) && names.length)
              .map(([server, names]) => [server, new Set(names)]),
          ),
        );
        setSkOff(new Set((pol.skills_disabled as string[]) || []));
        setBd(breakdown?.ok ? breakdown : null);
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
      if (autoTimer.current) {
        clearTimeout(autoTimer.current);
        const pending = pendingRef.current;
        if (pending) void save(pending[0], pending[1], pending[2]);
      }
      if (savedTimer.current) clearTimeout(savedTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const tokenByName = useMemo(() => {
    const map = new Map<string, number>();
    for (const part of bd?.tools || []) {
      for (const tool of part.byTool || []) map.set(tool.name, tool.tokens);
    }
    return map;
  }, [bd]);

  const injectable = mcp.filter((s) => s.enabled && s.connected);
  const injectableTools = injectable.reduce((a, s) => a + s.tools, 0);

  const preview = useMemo(() => {
    if (!bd) {
      const onTools = injectable.reduce(
        (a, s) => a + (mcpOff.has(s.name) ? 0 : s.tools),
        0,
      );
      const total = injectableTools * TOK_PER_TOOL_FALLBACK;
      const on = onTools * TOK_PER_TOOL_FALLBACK;
      return { total, on, delta: 0, used: on, window: 0, threshold: 0 };
    }
    let on = 0;
    let total = 0;
    for (const part of bd.tools) {
      for (const tool of part.byTool) {
        total += tool.tokens;
        const off =
          mcpOff.has(part.server) || toolOff[part.server]?.has(tool.name);
        if (!off) on += tool.tokens;
      }
    }
    return {
      total,
      on,
      delta: on - bd.toolsTotal,
      used: Math.max(0, bd.used + (on - bd.toolsTotal)),
      window: bd.window,
      threshold: bd.threshold,
    };
  }, [bd, mcpOff, toolOff, injectable, injectableTools]);

  const offToolCount = Object.values(toolOff).reduce((a, s) => a + s.size, 0);
  const offCount = mcpOff.size + offToolCount + skOff.size;
  const savedK = Math.abs(preview.delta) / 1000;

  function stateOf(s: McpServer): RowState {
    if (!s.enabled) return "disabled";
    if (!s.connected) return s.error ? "failed" : "offline";
    if (mcpOff.has(s.name)) return "off";
    if ((toolOff[s.name]?.size || 0) > 0) return "partial";
    return "on";
  }

  /** Local (unsaved-inclusive) stats for one server's row chip. */
  function serverStats(s: McpServer): { enabled: number; tokens: number } {
    const part = bd?.tools.find((p) => p.server === s.name);
    if (!part) {
      return {
        enabled: Math.max(0, s.tools - (toolOff[s.name]?.size || 0)),
        tokens: 0,
      };
    }
    let enabled = 0;
    let tokens = 0;
    for (const tool of part.byTool) {
      if (!tool.enabled) continue;
      if (mcpOff.has(s.name) || toolOff[s.name]?.has(tool.name)) continue;
      enabled += 1;
      tokens += tool.tokens;
    }
    return { enabled, tokens };
  }

  async function ensureTools() {
    if (Object.keys(details).length || detailsLoading) return;
    setDetailsLoading(true);
    try {
      const r = await api.mcpStatus(true);
      const map: Record<string, ToolDetail[]> = {};
      for (const s of r.status || []) {
        map[String(s.name)] = (
          (s.toolDetails as Array<Record<string, unknown>> | undefined) || []
        )
          .map((d) => ({
            name: String(d.name || ""),
            fullName: String(d.fullName || ""),
            description: String(d.description || ""),
          }))
          .filter((d) => d.fullName);
      }
      setDetails(map);
    } catch {
      /* 展开失败不阻塞面板；再点一次重试 */
    } finally {
      setDetailsLoading(false);
    }
  }

  async function save(nextMcp: Set<string>, nextTools: ToolOff, nextSk: Set<string>) {
    setSaving(true);
    setSaved(false);
    setErr("");
    try {
      const toolsDisabled: Record<string, string[]> = {};
      for (const [server, names] of Object.entries(nextTools)) {
        if (names.size) toolsDisabled[server] = [...names];
      }
      const r = await api.setToolPolicy(sessionId, {
        mode:
          nextMcp.size || nextSk.size || Object.keys(toolsDisabled).length
            ? "custom"
            : "all",
        mcp_disabled: [...nextMcp],
        mcp_tools_disabled: toolsDisabled,
        skills_disabled: [...nextSk],
      });
      if (r.ok) {
        setSaved(true);
        if (savedTimer.current) clearTimeout(savedTimer.current);
        savedTimer.current = setTimeout(() => setSaved(false), 1500);
      } else {
        setErr(t("saveFail"));
      }
    } catch {
      setErr(t("saveFail"));
    } finally {
      setSaving(false);
    }
  }

  function schedule(nextMcp: Set<string>, nextTools: ToolOff, nextSk: Set<string>) {
    if (autoTimer.current) clearTimeout(autoTimer.current);
    pendingRef.current = [nextMcp, nextTools, nextSk];
    autoTimer.current = setTimeout(() => {
      pendingRef.current = null;
      void save(nextMcp, nextTools, nextSk);
    }, 400);
  }

  async function retry(name: string) {
    setRetrying(name);
    setErr("");
    try {
      await api.reconnectMcp(name);
      const r = await api.mcpStatus();
      setMcp(mapMcp(r.status));
    } catch {
      setErr(t("saveFail"));
    } finally {
      setRetrying("");
    }
  }

  /** 原生 checkbox 直接给目标态，不读 stateOf——两次快速点击时 state
   *  快照可能是旧的（batched updates），旧逻辑会把两次点击算成同一方向。 */
  function toggleServer(s: McpServer, targetOn: boolean) {
    let nextMcp = new Set(mcpOff);
    let nextTools: ToolOff = { ...toolOff };
    if (!targetOn) {
      nextMcp.add(s.name);
    } else {
      nextMcp.delete(s.name);
      if (nextTools[s.name]?.size) {
        nextTools = { ...nextTools };
        delete nextTools[s.name];
      }
    }
    setMcpOff(nextMcp);
    setToolOff(nextTools);
    schedule(nextMcp, nextTools, skOff);
  }

  function toggleTool(server: string, fullName: string, next: boolean) {
    const set = new Set(toolOff[server] || []);
    if (next) set.delete(fullName);
    else set.add(fullName);
    const nextTools = { ...toolOff, [server]: set };
    const nextMcp = new Set(mcpOff);
    nextMcp.delete(server);
    setToolOff(nextTools);
    setMcpOff(nextMcp);
    schedule(nextMcp, nextTools, skOff);
  }

  function toggleSkill(name: string, targetOn: boolean) {
    const next = new Set(skOff);
    if (!targetOn) next.add(name);
    else next.delete(name);
    setSkOff(next);
    schedule(mcpOff, toolOff, next);
  }

  const query = q.trim().toLowerCase();
  const skillsShown = skills.filter(
    (s) => !query || s.name.toLowerCase().includes(query) || (s.description || "").toLowerCase().includes(query),
  );
  const showSkills = skillsOpen || Boolean(query);
  const toolMatches = (server: string) =>
    (details[server] || []).filter(
      (tool) =>
        tool.name.toLowerCase().includes(query) ||
        tool.description.toLowerCase().includes(query),
    );
  const mcpShown = mcp.filter(
    (s) => !query || s.name.toLowerCase().includes(query) || toolMatches(s.name).length > 0,
  );

  /** 公共 ui/Toggle（原 settings/mcp/McpToggle，2026-11-13 提升）——
   *  全站同一视觉/交互/键盘语义，不再自绘第三种开关。 */
  const switchEl = (
    on: boolean,
    toggle: (next: boolean) => void,
    label: string,
    disabled = false,
    _small?: boolean,
  ) => (
    <span className={disabled ? "pointer-events-none opacity-40" : ""}>
      <McpToggle checked={on} onChange={toggle} label={label} />
    </span>
  );

  /** 工具级 16px 方框 checkbox（原生 input + 对勾），与 switchEl 同一套
   *  原生语义。 */
  const checkEl = (on: boolean, toggle: (next: boolean) => void, label: string) => (
    <span className="relative inline-flex h-4 w-4 shrink-0">
      <input
        type="checkbox"
        className="peer absolute inset-0 z-10 cursor-pointer opacity-0"
        checked={on}
        onChange={(e) => toggle(e.target.checked)}
        aria-label={label}
      />
      <span className="absolute inset-0 flex items-center justify-center rounded-[4px] border border-line2 text-transparent transition-colors peer-checked:border-accent peer-checked:bg-accent peer-checked:text-white peer-focus-visible:ring-2 peer-focus-visible:ring-accent/60">
        <Check className="h-3 w-3" strokeWidth={3} />
      </span>
    </span>
  );

  const chevron = (open: boolean) =>
    open ? (
      <ChevronDown className="h-3.5 w-3.5 shrink-0 text-faint" />
    ) : (
      <ChevronRight className="h-3.5 w-3.5 shrink-0 text-faint" />
    );

  const statusChip = (s: McpServer) => {
    const st = stateOf(s);
    if (st === "disabled") return t("statusDisabled");
    if (st === "failed") return t("statusFailed");
    if (st === "offline") return t("statusOffline");
    if (st === "off") return `${s.tools} · ${t("offShort")}`;
    const { enabled, tokens } = serverStats(s);
    return `${enabled === s.tools ? s.tools : `${enabled}/${s.tools}`} · ${
      tokens ? `≈${fmt(tokens)}` : "—"
    }`;
  };

  useEffect(() => {
    if (q.trim()) void ensureTools();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);

  const hint = saving ? t("saving") : saved ? t("hintSaved") : t("hintAuto");
  const pct = preview.window
    ? Math.min(100, Math.max(0, (preview.used / preview.window) * 100))
    : 0;
  const thresholdPct = preview.window
    ? Math.min(100, Math.max(0, (preview.threshold / preview.window) * 100))
    : 0;
  const near = preview.window > 0 && preview.used >= preview.threshold;
  const footerText = err
    ? err
    : saved
      ? `✓ ${t("hintSaved")}`
      : preview.delta < -10
        ? t("savedPerCall", { n: offCount, k: savedK.toFixed(1) })
        : t("injected", { n: fmt(preview.used) });

  return (
    <div className="absolute bottom-full right-0 z-50 mb-1 flex max-h-[min(72vh,560px)] w-[400px] flex-col rounded-xl border border-line bg-card p-3 shadow-2xl">
      <div className="mb-1 flex items-center gap-1.5 text-xs font-medium text-txt">
        <Boxes className="h-3.5 w-3.5 text-accent" />
        {t("title")}
        <span className="ml-auto text-[10px] font-normal text-faint">{hint}</span>
      </div>

      {/* 账本：先给总量与组成，再给开关 */}
      {bd ? (
        <div className="mb-2">
          <div className="flex items-baseline justify-between text-[10px] text-faint">
            <span>{t("perRequest")}</span>
            <span className="tabular-nums">
              ≈{fmt(preview.used)} / {fmt(preview.window)}
            </span>
          </div>
          <div className="relative mt-1 h-1.5 overflow-hidden rounded-full bg-line2">
            <div
              className={
                "h-full rounded-full transition-all " +
                (near ? "bg-red" : "bg-accent")
              }
              style={{ width: `${pct}%` }}
            />
            {thresholdPct > 0 && (
              <span
                className="absolute -top-0.5 h-2.5 w-px bg-amber-500/80"
                style={{ left: `${thresholdPct}%` }}
                title={t("threshold", { n: fmt(preview.threshold) })}
              />
            )}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] text-faint">
            <span>{t("history")} {fmt(bd.history)}</span>
            <span>{t("toolsLabel")} ≈{fmt(preview.on)}</span>
            <span>{t("skillsLabel")} {fmt(bd.skills.tokens)}</span>
            <span>{t("systemLabel")} {fmt(bd.system)}</span>
            {bd.counter === "estimate" && <span>· {t("estimated")}</span>}
          </div>
        </div>
      ) : (
        <div className="mb-2 flex items-center justify-between text-[10px] text-faint">
          <span>{t("perRequest")}</span>
          <span className="tabular-nums">≈{fmt(preview.used)}</span>
        </div>
      )}

      <div className="mb-1.5 flex items-center gap-1.5 rounded-md bg-card2/60 px-2 py-1">
        <Search className="h-3 w-3 shrink-0 text-faint" />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t("search")}
          className="w-full bg-transparent text-[11px] text-txt placeholder:text-faint focus:outline-none"
        />
      </div>

      {loading ? (
        <div className="flex flex-1 items-center justify-center py-8 text-muted">
          <Loader2 className="h-4 w-4 animate-spin" />
        </div>
      ) : (
        <div className="min-h-0 flex-1 space-y-0.5 overflow-y-auto pr-1">
          <div className="sticky top-0 z-10 bg-card/95 pb-1 pt-0.5 text-[10px] font-medium uppercase tracking-wider text-faint backdrop-blur">
            {t("mcpSection")} · {mcpShown.length}
          </div>
          {mcpShown.length === 0 && (
            <div className="px-2 py-3 text-[11px] text-faint">{t("emptyMcp")}</div>
          )}
          {mcpShown.map((s) => {
            const st = stateOf(s);
            const rowTools = details[s.name] || [];
            const visibleTools = toolMatches(s.name);
            const open = expanded.has(s.name) || (Boolean(query) && visibleTools.length > 0);
            const disabled = st === "disabled";
            return (
              <div key={s.name} className="rounded-md">
                <div
                  onClick={(e) => {
                    if (e.target instanceof HTMLElement && e.target.closest("button")) return;
                    if (disabled || st === "failed" || st === "offline") return;
                    toggleServer(s, st === "off");
                  }}
                  className={
                    "flex items-center gap-1.5 rounded-md px-1.5 py-1.5 transition-colors hover:bg-card2/60 " +
                    (disabled ? "opacity-45" : "cursor-pointer")
                  }
                >
                  {/* 故意用 span[role=button]：button 是 labelable，会成为
                      整行 <label> 的隐式激活目标，点行会先展开而不是切换开关。
                      preventDefault 阻断 label 的合成 click 转发。 */}
                  <span
                    role="button"
                    tabIndex={0}
                    onClick={(e) => {
                      e.preventDefault();
                      if (disabled || st === "failed" || st === "offline") return;
                      const next = new Set(expanded);
                      if (next.has(s.name)) next.delete(s.name);
                      else next.add(s.name);
                      setExpanded(next);
                      void ensureTools();
                    }}
                    onKeyDown={(e) => {
                      if (e.key !== "Enter" && e.key !== " ") return;
                      e.preventDefault();
                      if (disabled || st === "failed" || st === "offline") return;
                      const next = new Set(expanded);
                      if (next.has(s.name)) next.delete(s.name);
                      else next.add(s.name);
                      setExpanded(next);
                      void ensureTools();
                    }}
                    className="shrink-0 cursor-pointer text-faint hover:text-txt focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 rounded"
                    aria-label={t("expandTools")}
                  >
                    {chevron(open)}
                  </span>
                  {switchEl(
                    st !== "off",
                    (next) => toggleServer(s, next),
                    s.name,
                    disabled,
                  )}
                  <span className="min-w-0 flex-1 truncate text-xs text-txt" title={s.error || s.name}>
                    {s.name}
                  </span>
                  {st === "failed" && (
                    <button
                      type="button"
                      onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        void retry(s.name);
                      }}
                      className="shrink-0 rounded bg-red/15 px-1.5 py-px text-[10px] text-red hover:bg-red/25"
                    >
                      {retrying === s.name ? t("retrying") : t("retry")}
                    </button>
                  )}
                  <span
                    className={
                      "shrink-0 text-[10px] tabular-nums " +
                      (st === "failed"
                        ? "text-red"
                        : st === "offline"
                          ? "text-amber-500"
                          : st === "on" || st === "partial"
                            ? "text-faint"
                            : "text-faint")
                    }
                  >
                    {statusChip(s)}
                  </span>
                </div>
                {open && !disabled && st !== "failed" && st !== "offline" && (
                  <div className="ml-8 border-l border-line2/50 pl-1.5">
                    {detailsLoading && !rowTools.length ? (
                      <div className="flex items-center gap-1 px-2 py-1 text-[10px] text-faint">
                        <Loader2 className="h-3 w-3 animate-spin" /> {t("loadingTools")}
                      </div>
                    ) : visibleTools.length === 0 ? (
                      <div className="px-2 py-1 text-[10px] text-faint">{t("noTools")}</div>
                    ) : (
                      visibleTools.map((tool) => {
                        const on = !toolOff[s.name]?.has(tool.fullName);
                        const tokens = tokenByName.get(tool.fullName) || 0;
                        return (
                          <label
                            key={tool.fullName}
                            title={tool.description}
                            className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-0.5 hover:bg-card2/50"
                          >
                            {checkEl(on, (next) => toggleTool(s.name, tool.fullName, next), tool.name)}
                            <span className="min-w-0 flex-1 truncate text-[11px] text-muted">
                              {tool.name}
                            </span>
                            <span className="shrink-0 text-[9px] tabular-nums text-faint">
                              {tokens ? `≈${fmt(tokens)}` : ""}
                            </span>
                          </label>
                        );
                      })
                    )}
                  </div>
                )}
              </div>
            );
          })}

          <button
            type="button"
            onClick={() => setSkillsOpen((v) => !v)}
            className="sticky top-0 z-10 flex w-full items-center gap-1 bg-card/95 pb-1 pt-1.5 text-[10px] font-medium uppercase tracking-wider text-faint backdrop-blur"
          >
            {chevron(showSkills)}
            <span className="flex-1 text-left">
              {t("skillsSection")} · {skills.length}
              {skOff.size > 0 ? ` · ${t("offN", { n: skOff.size })}` : ""}
            </span>
          </button>
          {showSkills &&
            skillsShown.map((s) => (
              <label
                key={s.name}
                title={s.description}
                className="flex cursor-pointer items-center gap-2 rounded-md px-1.5 py-1 hover:bg-card2/60"
              >
                {switchEl(!skOff.has(s.name), (next) => toggleSkill(s.name, next), s.name)}
                <span className="min-w-0 flex-1 truncate text-xs text-txt">{s.name}</span>
                {s.description && (
                  <span className="hidden max-w-[45%] truncate text-[10px] text-faint sm:inline">
                    {s.description}
                  </span>
                )}
              </label>
            ))}
          {showSkills && skillsShown.length === 0 && (
            <div className="px-2 py-2 text-[10px] text-faint">{t("noSkills")}</div>
          )}
        </div>
      )}

      <div className="mt-2 flex items-center gap-2 border-t border-line2/50 pt-2 text-[11px]">
        <span
          className={
            err
              ? "text-red"
              : saved || preview.delta < -10
                ? "text-emerald-600 dark:text-emerald-400"
                : "text-faint"
          }
        >
          {footerText}
        </span>
        <button
          onClick={onClose}
          disabled={saving}
          className="ml-auto rounded-md bg-accent px-3 py-1 text-xs text-white transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {t("done")}
        </button>
      </div>
    </div>
  );
}