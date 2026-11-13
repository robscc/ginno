"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Boxes, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/api/workspace";

/**
 * 上下文管理（Tool Policy P1，2026-11-12 二轮改版：去掉预设档，直接勾选）。
 *
 * 2026-11-13 体验修复（用户反馈“界面有点错乱”）：
 * - iOS 开关的 track 用了 `bg-accent`，但该 token 在 90c431b UI redesign
 *   时被删掉 → 所有“开”的开关 track 透明，只剩一个白点；底栏“完成”按钮
 *   同样是白字透明底。这里补回 tailwind accent token（见 globals.css）。
 * - 底部不再显示“每轮约省 ↑0.0k”：改成“当前注入 ≈x k/次”，关掉后显示
 *   “已关 N 项 · ≈省 x k/次”，数字始终有基准。
 * - 区分四种状态：设置中停用 / 未连接 / 连接失败 / 已连接（0 工具）。
 *   不再把所有 tools=0 的行统一置灰（那会把“停用”和“故障”混成一类）。
 * - “✓ 已保存”不再永久占用“完成”按钮：单独做 1.5s 状态提示，按钮只负责关闭。
 * - 补 Esc 关闭、开关可键盘聚焦、MCP/技能分区显示数量。
 *
 * 勾掉 = 禁用（黑名单）；全勾 = all。MCP 服务器/非内置 skills 两节，
 * 内置（browser、builtin 工具）不参与不列出。保存走 PUT tool_policy，
 * 下一 turn 生效。
 */
type McpStatus = {
  name: string;
  tools: number;
  connected: boolean;
  enabled: boolean;
  error?: string;
};
type SkillRow = { name: string; description?: string };

const BUILTIN_SKILLS = new Set(["browser"]);
const TOK_PER_TOOL = 330;

type RowState = "ok" | "disabled" | "offline" | "failed" | "empty";

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
  const [retrying, setRetrying] = useState("");
  // 勾选即自动保存（2026-11-13 修复：手动保存按钮摩擦——uncheck 后直接
  // 点外部关闭=未保存，重开回读旧 policy 全 check，被用户报 bug）。
  const autoTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingRef = useRef<[Set<string>, Set<string>] | null>(null);
  const savedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const mapMcp = useCallback(
    (list: Array<Record<string, unknown>> | undefined): McpStatus[] =>
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
        const [mcpRes, skillsRes, sess] = await Promise.all([
          api.mcpStatus(),
          api.listSkills(),
          api.getSessionDetail(sessionId),
        ]);
        if (!alive) return;
        setMcp(mapMcp(mcpRes.status));
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
      if (savedTimer.current) clearTimeout(savedTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  // Esc 关闭：与全局浮层关闭语义一致（2026-11-13）。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const toolCount = (name: string) => {
    const s = mcp.find((x) => x.name === name);
    return s ? s.tools : 0;
  };

  const stateOf = (s: McpStatus): RowState => {
    if (!s.enabled) return "disabled";
    if (!s.connected) return s.error ? "failed" : "offline";
    if (s.tools === 0) return "empty";
    return "ok";
  };

  // 只统计“真正会注入”的工具：全局停用/未连接的 server 本来就不占上下文。
  const injectable = mcp.filter((s) => s.enabled && s.connected);
  const injectableTools = injectable.reduce((a, s) => a + s.tools, 0);
  const onTools = injectable.reduce(
    (a, s) => a + (mcpOff.has(s.name) ? 0 : s.tools),
    0,
  );
  const offTools = injectableTools - onTools;
  const offCount =
    injectable.filter((s) => mcpOff.has(s.name)).length + skOff.size;
  const onK = (onTools * TOK_PER_TOOL) / 1000;
  const savedK = (offTools * TOK_PER_TOOL) / 1000;

  async function save(nextMcp: Set<string>, nextSk: Set<string>) {
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
        if (savedTimer.current) clearTimeout(savedTimer.current);
        savedTimer.current = setTimeout(() => setSaved(false), 1500);
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

  const section = (label: string, count: number) => (
    <div className="sticky top-0 z-10 bg-card/95 pb-1 pt-1.5 text-[10px] font-medium uppercase tracking-wider text-faint backdrop-blur">
      {label} · {count}
    </div>
  );

  /** iOS 风 switch（2026-11-13：checkbox 换开关——勾选态一眼可辨）。 */
  const switchEl = (
    on: boolean,
    toggle: () => void,
    key: string,
    label: string,
    disabled = false,
  ) => (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      onClick={(e) => {
        e.preventDefault(); // 整行 label 点击统一入口，避免双触发
        toggle();
      }}
      className={
        "relative h-[18px] w-[32px] shrink-0 rounded-full transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 " +
        (on ? "bg-accent" : "bg-line2") +
        (disabled ? " cursor-not-allowed opacity-40" : "")
      }
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
    state: RowState,
    right?: string | null,
    error?: string,
  ) => {
    const disabled = state === "disabled";
    return (
      <label
        key={key}
        title={error || undefined}
        className={
          "flex cursor-pointer items-center gap-2.5 rounded-md px-2 py-1.5 transition-colors hover:bg-card2/60 " +
          (disabled ? "opacity-45" : "")
        }
      >
        {switchEl(on, toggle, key, name, disabled)}
        <span className="min-w-0 flex-1 truncate text-xs text-txt">{name}</span>
        {state === "failed" && (
          <button
            type="button"
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              void retry(name);
            }}
            className="shrink-0 rounded bg-red/15 px-1.5 py-px text-[10px] text-red hover:bg-red/25"
          >
            {retrying === name ? t("retrying") : t("retry")}
          </button>
        )}
        {right && (
          <span
            className={
              "shrink-0 text-[10px] tabular-nums " +
              (state === "failed"
                ? "text-red"
                : state === "offline"
                  ? "text-amber-500"
                  : "rounded bg-line2/50 px-1.5 py-px text-faint")
            }
          >
            {right}
          </span>
        )}
      </label>
    );
  };

  const statusRight = (s: McpStatus): string => {
    const st = stateOf(s);
    if (st === "disabled") return t("statusDisabled");
    if (st === "failed") return t("statusFailed");
    if (st === "offline") return t("statusOffline");
    if (st === "empty") return t("zeroTools");
    return t("toolsUnit", { n: s.tools });
  };

  const hint = saving
    ? t("saving")
    : saved
      ? t("hintSaved")
      : t("hintAuto");

  const footerText = err
    ? err
    : saved
      ? `✓ ${t("hintSaved")}`
      : offCount > 0
        ? t("savedPerCall", { n: offCount, k: savedK.toFixed(1) })
        : t("injected", { n: onK.toFixed(1) });

  return (
    <div className="absolute bottom-full right-0 z-50 mb-1 w-[360px] rounded-xl border border-line bg-card p-3 shadow-2xl">
      <div className="mb-1 flex items-center gap-1.5 text-xs font-medium text-txt">
        <Boxes className="h-3.5 w-3.5 text-accent" />
        {t("title")}
        <span className="ml-auto text-[10px] font-normal text-faint">
          {hint}
        </span>
      </div>
      <div className="mb-1 text-[10px] text-faint">{t("hintOff")}</div>
      {loading ? (
        <div className="flex items-center justify-center py-8 text-muted">
          <Loader2 className="h-4 w-4 animate-spin" />
        </div>
      ) : (
        <>
          <div className="max-h-[300px] space-y-0.5 overflow-y-auto pr-1">
            {section(t("mcpSection"), mcp.length)}
            {mcp.map((s) => {
              const st = stateOf(s);
              return row(
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
                st,
                statusRight(s),
                s.error,
              );
            })}
            {section(t("skillsSection"), skills.length)}
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
                "ok",
              ),
            )}
          </div>
          <div className="mt-2 flex items-center gap-2 border-t border-line2/50 pt-2 text-[11px]">
            <span
              className={
                err
                  ? "text-red"
                  : saved || offCount > 0
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
        </>
      )}
    </div>
  );
}