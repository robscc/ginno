"use client";

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { ChevronLeft, Copy, Eye, EyeOff, Plus } from "lucide-react";
import { ConfirmModal } from "@/components/ConfirmModal";
import * as api from "@/lib/runtime";
import type { McpServerStatus, McpToolDetail } from "@/lib/runtime";
import { McpToggle } from "./McpToggle";
import {
  fmtClock,
  fullToolName,
  serverLine,
  shortToolName,
  statusMeta,
  transportBadgeClass,
  transportOf,
  type McpServerEntry,
} from "./mcpShared";

export type McpDetailTab = "config" | "tools" | "perms" | "activity";
type Tab = McpDetailTab;
type KvRow = { k: string; v: string };

// 详情面板（页内切换，无新路由；原型 v2 scr-detail）。四个 tab：
// Configuration（连接参数，Save & Reload 全量重建）/ Tools（每工具开关，写
// disabled_tools 只 putMcp、下一 turn 图重建生效）/ Permissions（settings.json
// permissions 按 server 前缀过滤的视图编辑）/ Activity（connectedAt/lastErrorAt
// 派生的精简时间线，完整日志在 sidecar.log）。
export function McpDetailView({
  name,
  entry,
  status,
  detail,
  allToolNames,
  restarting,
  switchOn,
  switchPending,
  initialTab,
  onBack,
  onSaveEntry,
  onRename,
  onToggleTool,
  onReconnect,
  onToggleServer,
  onToast,
}: {
  name: string;
  entry: McpServerEntry;
  /** 5s 轻轮询状态（无 toolDetails）。 */
  status?: McpServerStatus;
  /** GET /api/mcp?tools=1 的明细（toolDetails / disabledTools）。 */
  detail?: McpServerStatus;
  /** 该 server 名下的 graph 全名（来自 5s 轮询 getMcp().tools 的最长前缀归属）。 */
  allToolNames: string[];
  restarting: boolean;
  /** 头部启停开关的下发值（pending 态覆盖轮询态，见 McpSettings.toggleServer）。 */
  switchOn: boolean;
  switchPending: boolean;
  /** 初始 tab（卡片菜单「Manage tools / Permissions」直达）。 */
  initialTab?: Tab;
  onBack: () => void;
  /** 保存连接参数（put + reload 全量重建）；返回是否成功。 */
  onSaveEntry: (name: string, entry: McpServerEntry) => Promise<boolean>;
  /** 重命名 = 删旧键 + 写新键（put + reload）；返回是否成功。 */
  onRename: (oldName: string, newName: string, entry: McpServerEntry) => Promise<boolean>;
  /** 工具开关（只 putMcp，下一 turn 生效）。 */
  onToggleTool: (tool: string, on: boolean) => void;
  onReconnect: () => void;
  onToggleServer: (on: boolean) => void;
  onToast: (msg: string, tone?: "ok" | "err" | "warn") => void;
}) {
  const t = useTranslations("settings.mcp");
  const router = useRouter();
  const [tab, setTab] = useState<Tab>(initialTab ?? "config");

  // ---- Configuration 表单（key={name} 由父组件保证切换 server 时重置） ----
  const [form, setForm] = useState(() => entryToForm(name, entry));
  const [showSecrets, setShowSecrets] = useState(false);
  const [saving, setSaving] = useState(false);
  const [renameTo, setRenameTo] = useState<string | null>(null); // 非空 = 确认弹窗打开

  const transport = transportOf(entry);
  const enabled = status?.enabled ?? entry.enabled !== false;
  const meta = status ? statusMeta(status) : null;
  const displayLine = status?.url || serverLine(entry);

  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setForm((f) => ({ ...f, [k]: v }));

  const save = async () => {
    if (!form.name.trim()) {
      onToast(t("cfgNameRequired"), "err");
      return;
    }
    const next = formToEntry(entry, form);
    const newName = form.name.trim();
    if (newName !== name) {
      // 重命名级联后果多（工具全名/权限 pattern 换前缀且不自动迁移）——二次确认
      setRenameTo(newName);
      return;
    }
    setSaving(true);
    try {
      const ok = await onSaveEntry(name, next);
      if (ok) onToast(t("savedCfg", { name }));
    } finally {
      setSaving(false);
    }
  };

  // ---- Tools ----
  // 明细优先（?tools=1 的 toolDetails）；后端参数未就绪时降级为 5s 轮询拿到的
  // graph 全名纯名单（无描述/标记），再缺（断连）走空态提示。
  const toolRows: McpToolDetail[] = useMemo(() => {
    const d = detail?.toolDetails;
    if (Array.isArray(d) && d.length) return d;
    return allToolNames.map((full) => ({ name: shortToolName(name, full) }));
  }, [detail, allToolNames, name]);

  const disabledSet = useMemo(() => {
    const raw = detail?.disabledTools ?? (Array.isArray(entry.disabled_tools) ? (entry.disabled_tools as string[]) : []);
    return new Set(raw.map((x) => shortToolName(name, String(x))));
  }, [detail, entry, name]);

  const [toolSearch, setToolSearch] = useState("");
  const filteredTools = useMemo(() => {
    const q = toolSearch.trim().toLowerCase();
    if (!q) return toolRows;
    return toolRows.filter(
      (r) => r.name.toLowerCase().includes(q) || (r.description ?? "").toLowerCase().includes(q),
    );
  }, [toolRows, toolSearch]);

  // ---- Permissions（settings.json → permissions 的按 server 过滤视图） ----
  const [perms, setPerms] = useState<{ allow: string[]; ask: string[]; deny: string[] } | null>(null);
  useEffect(() => {
    let alive = true;
    api
      .getSettings()
      .then((s) => {
        if (!alive) return;
        const p = ((s as Record<string, unknown>).permissions ?? {}) as Record<string, unknown>;
        setPerms({
          allow: strArr(p.allow),
          ask: strArr(p.ask),
          deny: strArr(p.deny),
        });
      })
      .catch(() => setPerms(null));
    return () => {
      alive = false;
    };
  }, []);

  const prefix = `mcp_${name}_`;
  const editPerm = (which: keyof PermsState, nextMatching: string[]) => {
    if (!perms) return;
    const next = { ...perms, [which]: nextMatching };
    setPerms(next);
    // 立即持久化：整文件读改写（与 PermissionsSettings 同一契约，后写者胜），
    // 只替换本 server 前缀命中的 pattern，其余原样保留。
    void (async () => {
      try {
        const s = (await api.getSettings()) as Record<string, unknown>;
        const p = ((s.permissions ?? {}) as Record<string, unknown>) as PermsState;
        const merged: PermsState = {
          allow: strArr(p.allow),
          ask: strArr(p.ask),
          deny: strArr(p.deny),
        };
        merged[which] = [...merged[which].filter((x) => !x.startsWith(prefix)), ...nextMatching];
        s.permissions = merged;
        const r = await api.putSettings(s);
        onToast(r.ok ? t("permsSaved") : t("permsSaveFailed"), r.ok ? "ok" : "err");
      } catch {
        onToast(t("permsSaveFailed"), "err");
      }
    })();
  };

  return (
    <div className="px-8 py-7">
      {/* ---- 头部 ---- */}
      <div className="mb-3">
        <button
          type="button"
          onClick={onBack}
          className="inline-flex items-center gap-1 rounded-lg border border-line2 px-2.5 py-1 text-xs text-muted transition-colors hover:bg-card2 hover:text-txt"
        >
          <ChevronLeft className="h-3.5 w-3.5" /> {t("backAll")}
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-2.5">
        <span
          className={`h-1.5 w-1.5 shrink-0 rounded-full ${
            restarting ? "animate-pulse bg-yellow" : status?.connected ? "bg-green" : enabled ? "bg-red" : "bg-faint"
          }`}
        />
        <h2 className="text-lg font-semibold text-txt">{name}</h2>
        <span
          className={`rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase leading-none tracking-wider ${transportBadgeClass(transport)}`}
        >
          {transport}
        </span>
        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            onClick={onReconnect}
            disabled={restarting}
            className="rounded-lg border border-line2 px-2.5 py-1 text-xs text-txt transition-colors hover:bg-card2 disabled:opacity-45"
          >
            {restarting ? t("reconnecting") : t("reconnect")}
          </button>
          <McpToggle checked={switchOn} pending={switchPending} onChange={onToggleServer} label={name} />
        </div>
      </div>
      <div className="mt-1.5 truncate font-mono text-[12.5px] text-faint">
        {displayLine || "—"}
        {meta ? (meta.kind === "up" ? ` · ${t("upFor", { duration: meta.value })}` : ` · ${t("failedAt", { time: meta.value })}`) : ""}
      </div>
      {!status?.connected && enabled && status?.error && (
        <div className="mt-3 max-w-[640px] rounded-lg border border-red/30 bg-red/[0.08] px-3 py-2 text-[12.5px] text-red">
          {status.error}
        </div>
      )}

      {/* ---- Tabs ---- */}
      <div className="mt-3.5 flex gap-0.5 border-b border-line">
        {(
          [
            ["config", t("tabConfig")],
            ["tools", t("tabTools")],
            ["perms", t("tabPerms")],
            ["activity", t("tabActivity")],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            onClick={() => setTab(id)}
            className={`-mb-px border-b-2 px-3 py-2 text-[13px] transition-colors ${
              tab === id ? "border-indigo font-medium text-txt" : "border-transparent text-muted hover:text-txt"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* ---- Configuration ---- */}
      {tab === "config" && (
        <div className="max-w-[580px] pt-4">
          <label className="field-label">{t("cfgName")}</label>
          <input className="field" value={form.name} onChange={(e) => set("name", e.target.value)} />
          <p className="mt-1 text-[11.5px] leading-relaxed text-faint">{t("cfgNameNote")}</p>

          <label className="field-label mt-3.5">{t("cfgTransport")}</label>
          <div className="flex gap-3">
            <select
              className="field w-[220px] font-mono text-[13px]"
              value={form.transport}
              onChange={(e) => set("transport", e.target.value)}
            >
              <option value="streamable-http">streamable-http</option>
              <option value="sse">sse</option>
              <option value="stdio">stdio</option>
            </select>
            <button
              type="button"
              onClick={() => setShowSecrets((v) => !v)}
              title={t("cfgShowSecrets")}
              className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-line px-2.5 text-xs text-faint transition-colors hover:text-txt"
            >
              {showSecrets ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
            </button>
          </div>
          <p className="mt-1 text-[11.5px] leading-relaxed text-faint">{t("cfgTransportNote")}</p>

          {form.transport === "stdio" ? (
            <>
              <label className="field-label mt-3.5">{t("cfgCommand")}</label>
              <input
                className="field font-mono text-[13px]"
                value={form.command}
                onChange={(e) => set("command", e.target.value)}
                placeholder="npx"
              />
              <label className="field-label mt-3.5">{t("cfgArgs")}</label>
              <textarea
                className="field font-mono text-[13px]"
                rows={2}
                value={form.argsText}
                onChange={(e) => set("argsText", e.target.value)}
              />
              <EnvRows
                label={t("cfgEnv")}
                rows={form.env}
                masked={!showSecrets}
                onChange={(rows) => set("env", rows)}
              />
              <p className="mt-1 text-[11.5px] leading-relaxed text-faint">{t("cfgEnvNote")}</p>
            </>
          ) : (
            <>
              {/* §8.3：URL 本身即凭证 —— password 遮蔽 + 眼睛切真值，避免把
                  脱敏值写回文件（这里始终绑定真值） */}
              <label className="field-label mt-3.5">{t("cfgUrl")}</label>
              <input
                className="field font-mono text-[13px]"
                type={showSecrets ? "text" : "password"}
                value={form.url}
                onChange={(e) => set("url", e.target.value)}
              />
              <p className="mt-1 text-[11.5px] leading-relaxed text-faint">{t("cfgUrlNote")}</p>
              <EnvRows
                label={t("cfgHeaders")}
                hint={t("cfgHeadersNote")}
                keyPlaceholder="Authorization"
                rows={form.headers}
                masked={!showSecrets}
                onChange={(rows) => set("headers", rows)}
              />
            </>
          )}

          <label className="field-label mt-3.5">{t("cfgTimeout")}</label>
          <input
            className="field w-[160px] font-mono text-[13px]"
            value={form.timeout}
            onChange={(e) => set("timeout", e.target.value)}
          />

          <div className="mt-4 flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={() => setForm(entryToForm(name, entry))}
              className="rounded-lg px-3 py-1.5 text-[13px] text-muted transition-colors hover:text-txt"
            >
              {t("cfgCancel")}
            </button>
            <button
              type="button"
              onClick={() => void save()}
              disabled={saving}
              className="rounded-lg bg-indigo px-3 py-1.5 text-[13px] font-medium text-white transition-colors hover:bg-indigo2 disabled:opacity-45"
            >
              {t("saveReload")}
            </button>
          </div>
        </div>
      )}

      {/* ---- Tools ---- */}
      {tab === "tools" && (
        <div className="pt-4">
          <div className="mb-1.5 flex items-center gap-2.5">
            <input
              className="field max-w-[240px]"
              placeholder={t("filterTools")}
              value={toolSearch}
              onChange={(e) => setToolSearch(e.target.value)}
            />
            <span className="ml-auto text-xs text-muted">
              {t("toolsEnabledCount", { on: toolRows.filter((r) => !disabledSet.has(r.name)).length, total: toolRows.length })}
            </span>
          </div>
          {toolRows.length === 0 ? (
            <div className="rounded-lg border border-line bg-card px-3.5 py-6 text-center text-[13px] text-faint">
              {status?.connected ? t("toolsNoMatch") : t("toolsUnavailable")}
            </div>
          ) : (
            <div className="overflow-hidden rounded-lg border border-line bg-card">
              <table className="w-full text-[13px]">
                <thead>
                  <tr className="border-b border-line text-left text-[11px] uppercase tracking-wider text-faint">
                    <th className="w-10 px-3.5 py-2.5 font-semibold"></th>
                    <th className="px-3.5 py-2.5 font-semibold">{t("thTool")}</th>
                    <th className="px-3.5 py-2.5 font-semibold">{t("thGraph")}</th>
                    <th className="px-3.5 py-2.5 font-semibold">{t("thDesc")}</th>
                    <th className="w-24 px-3.5 py-2.5 font-semibold">{t("thHints")}</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredTools.map((r) => {
                    const full = fullToolName(name, r.name);
                    const on = !disabledSet.has(r.name);
                    return (
                      <tr key={r.name} className="border-b border-line last:border-0 hover:bg-card2">
                        <td className="px-3.5 py-2">
                          <McpToggle checked={on} onChange={(next) => onToggleTool(r.name, next)} label={r.name} />
                        </td>
                        <td className="px-3.5 py-2 font-mono text-[12.5px] text-txt">{r.name}</td>
                        <td className="px-3.5 py-2">
                          <button
                            type="button"
                            title={full}
                            onClick={() => {
                              void navigator.clipboard?.writeText(full);
                              onToast(t("copied", { name: full }));
                            }}
                            className="inline-flex max-w-[220px] items-center gap-1 truncate font-mono text-[11px] text-faint hover:text-txt"
                          >
                            <span className="truncate">{full}</span>
                            <Copy className="h-3 w-3 shrink-0" />
                          </button>
                        </td>
                        <td className="max-w-[260px] truncate px-3.5 py-2 text-muted" title={r.description ?? undefined}>
                          {r.description || "—"}
                        </td>
                        <td className="px-3.5 py-2">
                          {r.readOnly ? (
                            <span className="rounded border border-green/35 px-1.5 py-0.5 font-mono text-[10px] leading-none text-green">
                              {t("hintReadOnly")}
                            </span>
                          ) : r.destructive ? (
                            <span className="rounded border border-red/35 px-1.5 py-0.5 font-mono text-[10px] leading-none text-red">
                              {t("hintDestructive")}
                            </span>
                          ) : (
                            <span className="rounded border border-line2 px-1.5 py-0.5 font-mono text-[10px] leading-none text-faint">
                              {t("hintNone")}
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                  {filteredTools.length === 0 && (
                    <tr>
                      <td colSpan={5} className="px-3.5 py-3.5 text-center text-xs text-faint">
                        {t("toolsNoMatch")}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
          <p className="mt-2.5 max-w-[640px] text-[11.5px] leading-relaxed text-faint">{t("toolsNote")}</p>
        </div>
      )}

      {/* ---- Permissions ---- */}
      {tab === "perms" && (
        <div className="max-w-[640px] pt-4">
          {!perms ? (
            <div className="rounded-lg border border-line bg-card px-3.5 py-6 text-center text-[13px] text-faint">
              {t("permsNone")}
            </div>
          ) : (
            (["deny", "ask", "allow"] as const).map((which) => {
              const matching = perms[which].filter((x) => x.startsWith(prefix));
              const colorCls =
                which === "allow"
                  ? "text-green"
                  : which === "ask"
                    ? "text-yellow"
                    : "text-red";
              return (
                <div key={which} className="mb-4">
                  <div className={`mb-1.5 text-xs font-semibold ${colorCls}`}>
                    {which === "allow" ? t("permsAllow") : which === "ask" ? t("permsAsk") : t("permsDeny")}
                  </div>
                  {matching.length === 0 && <p className="text-xs text-faint">{t("permsNone")}</p>}
                  <div className="flex flex-wrap gap-1.5">
                    {matching.map((pat) => (
                      <span
                        key={pat}
                        className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 font-mono text-xs ${chipColorCls(which)}`}
                      >
                        {pat}
                        <button
                          type="button"
                          aria-label={t("permsDelete")}
                          onClick={() => editPerm(which, matching.filter((x) => x !== pat))}
                          className="opacity-60 hover:opacity-100"
                        >
                          ×
                        </button>
                      </span>
                    ))}
                  </div>
                  <PermAdd
                    placeholder={t("permsPlaceholder", { server: name })}
                    onAdd={(pat) => {
                      if (!matching.includes(pat)) editPerm(which, [...matching, pat]);
                    }}
                  />
                </div>
              );
            })
          )}
          <p className="mt-2 max-w-[640px] text-[11.5px] leading-relaxed text-faint">{t("permsNote")}</p>
          <button
            type="button"
            onClick={() => router.push("/settings/security/permissions")}
            className="mt-2 inline-flex items-center gap-1 text-xs text-blue-400 hover:underline"
          >
            {t("permsOpen")} →
          </button>
        </div>
      )}

      {/* ---- Activity ---- */}
      {tab === "activity" && (
        <div className="max-w-[680px] pt-4">
          {!status?.connected && enabled && status?.error && (
            <div className="mb-3 rounded-lg border border-red/30 bg-red/[0.07] px-3.5 py-2.5 text-[12.5px]">
              <b className="text-red">{status.error}</b>
              {typeof status.lastErrorAt === "number" && (
                <span className="ml-2 text-[11px] text-faint">{t("failedAt", { time: fmtClock(status.lastErrorAt) })}</span>
              )}
              <div className="mt-1 text-[11.5px] text-muted">{t("activityRetry")}</div>
            </div>
          )}
          <ul className="max-w-[620px]">
            {status?.connected && (
              <TimelineRow
                time={typeof status.connectedAt === "number" ? fmtClock(status.connectedAt * 1000) : "—"}
                dot="bg-green"
                text={t("activityConnected", { count: status.tools })}
              />
            )}
            {!status?.connected && enabled && status?.error && (
              <TimelineRow
                time={typeof status.lastErrorAt === "number" ? fmtClock(status.lastErrorAt) : "—"}
                dot="bg-red"
                text={t("activityDisconnected", { error: status.error })}
              />
            )}
            <TimelineRow time="—" dot="bg-faint" text={t("activityPresent")} />
          </ul>
          <div className="mt-3.5 max-w-[760px] rounded-lg border border-line bg-[rgb(var(--code-bg))] px-3.5 py-3 font-mono text-xs leading-[1.8] text-muted">
            {status?.connected && (
              <div>
                <span className="text-faint">{typeof status.connectedAt === "number" ? fmtClock(status.connectedAt * 1000) : "--:--"}</span>
                <span className="mr-2 inline-block min-w-11 font-bold text-blue-400">INFO</span>
                {t("logInitOk", { name, transport, timeout: String(entry.connect_timeout ?? 15) })}
              </div>
            )}
            {!status?.connected && enabled && status?.error && (
              <>
                <div>
                  <span className="text-faint">{typeof status.lastErrorAt === "number" ? fmtClock(status.lastErrorAt) : "--:--"}</span>
                  <span className="mr-2 inline-block min-w-11 font-bold text-red-400">ERROR</span>
                  {t("logInitFail", { name, error: status.error })}
                </div>
                <div>
                  <span className="text-faint">--:--</span>
                  <span className="mr-2 inline-block min-w-11 font-bold text-yellow-400">WARN</span>
                  {t("logRetry", { name })}
                </div>
              </>
            )}
            <div>
              <span className="text-faint">--:--</span>
              <span className="mr-2 inline-block min-w-11 font-bold text-blue-400">INFO</span>
              {t("logPoll")}
            </div>
          </div>
          <p className="mt-2 text-[11.5px] text-faint">{t("activityFullLog")}</p>
        </div>
      )}

      {/* 重命名二次确认（设计文档 §5.4/§8.2） */}
      {renameTo && (
        <ConfirmModal
          title={t("renameTitle")}
          message={t("renameConfirm")}
          confirmLabel={t("saveReload")}
          onCancel={() => setRenameTo(null)}
          onConfirm={() => {
            const target = renameTo;
            setRenameTo(null);
            void (async () => {
              setSaving(true);
              try {
                const ok = await onRename(name, target, formToEntry(entry, form));
                if (ok) onToast(t("savedCfg", { name: target }));
              } finally {
                setSaving(false);
              }
            })();
          }}
        />
      )}
    </div>
  );
}

type FormState = {
  name: string;
  transport: string;
  url: string;
  headers: KvRow[];
  command: string;
  argsText: string;
  env: KvRow[];
  timeout: string;
};
type PermsState = { allow: string[]; ask: string[]; deny: string[] };

function strArr(v: unknown): string[] {
  return Array.isArray(v) ? v.map(String) : [];
}

function entryToForm(name: string, entry: McpServerEntry): FormState {
  const obj = (v: unknown): Record<string, unknown> =>
    v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
  const rowsOf = (v: unknown): KvRow[] => {
    const o = obj(v);
    const rows = Object.entries(o).map(([k, val]) => ({ k, v: String(val ?? "") }));
    return rows.length ? rows : [{ k: "", v: "" }];
  };
  const tr = transportOf(entry);
  return {
    name,
    transport: tr,
    url: String(entry.url ?? ""),
    headers: rowsOf(entry.headers),
    command: String(entry.command ?? ""),
    argsText: Array.isArray(entry.args) ? (entry.args as unknown[]).map(String).join("\n") : "",
    env: rowsOf(entry.env),
    timeout: entry.connect_timeout != null ? String(entry.connect_timeout) : "15.0",
  };
}

// 未知自定义键原样保留（§4.2）：从 entry 浅拷贝起改，只覆盖结构化字段；
// enabled / disabled_tools 不在这里碰（各自的开关路径负责）。
function formToEntry(entry: McpServerEntry, f: FormState): McpServerEntry {
  const e: McpServerEntry = { ...entry };
  e.transport = f.transport;
  delete e.type;
  if (f.transport === "stdio") {
    e.command = f.command.trim() || "npx";
    e.args = f.argsText
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    delete e.url;
    delete e.headers;
  } else {
    e.url = f.url.trim();
    delete e.command;
    delete e.args;
    delete e.env;
  }
  const kv = (rows: KvRow[]): Record<string, string> | null => {
    const o: Record<string, string> = {};
    for (const r of rows) if (r.k.trim()) o[r.k.trim()] = r.v;
    return Object.keys(o).length ? o : null;
  };
  const headers = kv(f.headers);
  if (headers) e.headers = headers;
  else delete e.headers;
  if (f.transport === "stdio") {
    const env = kv(f.env);
    if (env) e.env = env;
    else delete e.env;
  }
  const to = Number(f.timeout);
  if (Number.isFinite(to) && to > 0) e.connect_timeout = to;
  else delete e.connect_timeout;
  return e;
}

function chipColorCls(which: "allow" | "ask" | "deny"): string {
  if (which === "allow") return "border-green/35 text-green-400";
  if (which === "ask") return "border-yellow/40 text-yellow-400";
  return "border-red/40 text-red-400";
}

function TimelineRow({ time, dot, text }: { time: string; dot: string; text: string }) {
  return (
    <li className="grid grid-cols-[64px_14px_1fr] items-center gap-2.5 py-1.5 text-[12.5px] text-muted">
      <span className="font-mono text-[11.5px] text-faint">{time}</span>
      <span className={`inline-block h-[7px] w-[7px] rounded-full ${dot}`} />
      <span>{text}</span>
    </li>
  );
}

// env / headers 共用的 kv 行编辑器（value 可遮蔽）。
function EnvRows({
  label,
  hint,
  keyPlaceholder,
  rows,
  masked,
  onChange,
}: {
  label: string;
  hint?: string;
  keyPlaceholder?: string;
  rows: KvRow[];
  masked: boolean;
  onChange: (rows: KvRow[]) => void;
}) {
  const t = useTranslations("settings.mcp");
  return (
    <div className="mt-3.5">
      <label className="field-label">{label}</label>
      {hint && <p className="mb-1.5 text-[11.5px] leading-relaxed text-faint">{hint}</p>}
      <div className="space-y-2">
        {rows.map((r, i) => (
          <div key={i} className="flex items-center gap-2">
            <input
              className="field w-[170px] font-mono text-[13px]"
              value={r.k}
              placeholder={keyPlaceholder ?? "KEY"}
              onChange={(e) => onChange(rows.map((x, j) => (j === i ? { ...x, k: e.target.value } : x)))}
            />
            <input
              className="field flex-1 font-mono text-[13px]"
              type={masked ? "password" : "text"}
              value={r.v}
              placeholder={t("wzValue")}
              onChange={(e) => onChange(rows.map((x, j) => (j === i ? { ...x, v: e.target.value } : x)))}
            />
            <button
              type="button"
              aria-label={t("permsDelete")}
              onClick={() => onChange(rows.filter((_, j) => j !== i))}
              className="rounded-lg border border-line px-2 py-1.5 text-xs text-muted hover:text-red"
            >
              ×
            </button>
          </div>
        ))}
      </div>
      <button
        type="button"
        onClick={() => onChange([...rows, { k: "", v: "" }])}
        className="mt-2 flex w-full items-center justify-center gap-1 rounded-lg border border-dashed border-line2 px-3 py-1.5 text-[12.5px] text-muted transition-colors hover:border-indigo hover:text-txt"
      >
        <Plus className="h-3.5 w-3.5" /> {t("cfgAddVar")}
      </button>
    </div>
  );
}

// Permissions tab 的添加行。
function PermAdd({ placeholder, onAdd }: { placeholder: string; onAdd: (pattern: string) => void }) {
  const t = useTranslations("settings.mcp");
  const [draft, setDraft] = useState("");
  const add = () => {
    const v = draft.trim();
    if (!v) return;
    onAdd(v);
    setDraft("");
  };
  return (
    <div className="mt-2 flex gap-2">
      <input
        className="field max-w-[300px] font-mono text-xs"
        placeholder={placeholder}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && add()}
      />
      <button
        type="button"
        onClick={add}
        className="rounded-lg border border-line2 px-3 text-xs text-muted transition-colors hover:text-txt"
      >
        {t("permsAdd")}
      </button>
    </div>
  );
}
