"use client";

/* A2A hub 详情「对端节点」页签（a2a-connector-design.md §5 + 2026-11-18
 * 连接器页改造）：peer 从顶层独立卡收进 hub 的子配置。
 *
 * 数据面（详见 lib/api/a2a.ts 头注释）：
 *   - 列表：GET /api/a2a/peers（脱敏投影 + registry 上报的 status/statusDetail）；
 *   - 子配置编辑：内嵌 ConfigEditor，数据来自 GET /api/connectors/<peer.id>
 *     （config/configSchema/defaults），保存走 PATCH 同一 id——peer 实例虽然
 *     hide_in_list（顶层列表不出现），详情/配置 API 仍按 id 正常解析；
 *   - 删除/重连：hub 的 connector action（a2a_delete_peer / a2a_reconnect_peer）。
 *
 * 行内编辑同时只开一个（editingSlug 单值）：多个编辑器会争抢外层的
 * onDirtyChange，关闭/切 tab 守卫（ConnectorDetail 的 dirtyRef）只认一份
 * dirty 状态。收起按钮在 dirty 时禁用——先在编辑器里保存或放弃，避免静默
 * 丢改动；本组件卸载时无条件清 dirty（tab 切走由外层守卫兜底）。
 *
 * i18n：a2a 域（messages/{en,zh-CN}/a2a.json），key 全部扁平连字符。
 * 渲染层验证走 packaged UI e2e（test_packaged_ui_a2a_peers.py）；纯逻辑
 * （peerStatusKey/trustLabel）导出给 node 环境 vitest。 */

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import {
  CheckCircle2,
  CircleOff,
  CircleDashed,
  Loader2,
  Plus,
  RefreshCw,
  TriangleAlert,
  XCircle,
} from "lucide-react";
import * as api from "@/lib/runtime";
import type { ConnectorInfo } from "@/lib/runtime";
import {
  deleteA2aPeer,
  listA2aPeers,
  reconnectA2aPeer,
  type A2aPeerPublic,
} from "@/lib/api/a2a";
import { A2aPeerAddModal } from "./A2aPeerAddModal";
import { ConfigEditor } from "./ConfigEditor";

// ---- 纯逻辑（node 环境 vitest 直接测） ------------------------------------

/** 行内状态徽标的 i18n key（a2a 域）——字面量联合保住 useTranslations
 * 的静态 key 类型检查（next-intl 严格模式不接受 string）。 */
export type PeerStatusI18nKey =
  | "peers-status-connected"
  | "peers-status-disconnected"
  | "peers-status-error"
  | "peers-status-disabled"
  | "peers-status-unknown";

/** 停用优先于连接状态：停用的 peer 探针无意义。 */
export function peerStatusKey(
  peer: Pick<A2aPeerPublic, "enabled" | "status">,
): PeerStatusI18nKey {
  if (!peer.enabled) return "peers-status-disabled";
  switch (peer.status) {
    case "connected":
      return "peers-status-connected";
    case "disconnected":
      return "peers-status-disconnected";
    case "error":
      return "peers-status-error";
    default:
      return "peers-status-unknown";
  }
}

/** trust 值 → i18n key 的字面量映射（后端枚举外的值由调用方回落原值）。 */
const TRUST_KEYS = {
  readonly: "trust-readonly",
  normal: "trust-normal",
  full: "trust-full",
} as const;

export type TrustI18nKey = (typeof TRUST_KEYS)[keyof typeof TRUST_KEYS];

export function trustLabelKey(trust: string): TrustI18nKey | null {
  return (TRUST_KEYS as Record<string, TrustI18nKey>)[trust] ?? null;
}

const STATUS_META: Record<string, { cls: string; Icon: typeof CheckCircle2 }> = {
  "peers-status-connected": {
    cls: "text-green-600 dark:text-green-400",
    Icon: CheckCircle2,
  },
  "peers-status-disconnected": {
    cls: "text-yellow-600 dark:text-yellow-400",
    Icon: TriangleAlert,
  },
  "peers-status-error": { cls: "text-red-600 dark:text-red-400", Icon: XCircle },
  "peers-status-disabled": { cls: "text-faint", Icon: CircleOff },
  "peers-status-unknown": { cls: "text-faint", Icon: CircleDashed },
};

// ---- 组件 ------------------------------------------------------------------

export function A2aPeersPane({
  onDirtyChange,
  onSaved,
}: {
  /** 行内 ConfigEditor 的 dirty 透传（关闭/切 tab 守卫共用外层 dirtyRef）。 */
  onDirtyChange: (dirty: boolean) => void;
  /** 子配置保存成功：外层刷新 hub 详情 + 列表。 */
  onSaved: () => void;
}) {
  const t = useTranslations("a2a");

  const [peers, setPeers] = useState<A2aPeerPublic[] | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);

  const [editingSlug, setEditingSlug] = useState<string | null>(null);
  const [editDetail, setEditDetail] = useState<ConnectorInfo | null>(null);
  const [editLoading, setEditLoading] = useState(false);

  const [editorDirty, setEditorDirty] = useState(false);
  const [busy, setBusy] = useState<{ slug: string; kind: "delete" | "reconnect" } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const load = useCallback(async () => {
    // listA2aPeers 失败回落空表（lib 契约），这里不区分——空表与「读失败」
    // 都渲染空态；真正的错误提示由 refresh 的 catch 兜底。
    const rows = await listA2aPeers();
    setPeers(rows);
    return rows;
  }, []);

  useEffect(() => {
    void load().catch(() => setLoadFailed(true));
  }, [load]);

  // 卸载时无条件清 dirty：外层守卫只认一份状态，残留 true 会把用户困住。
  useEffect(() => () => onDirtyChange(false), [onDirtyChange]);

  // 刷新只动数据不动消息——doReconnect/doDelete 设置的结果提示要在刷新后
  // 存活，清提示由调用方（刷新按钮）显式做。
  const refresh = useCallback(async () => {
    try {
      await load();
    } catch {
      setLoadFailed(true);
    }
  }, [load]);

  const openEditor = async (p: A2aPeerPublic) => {
    if (editingSlug === p.slug) return; // 收起走独立按钮（dirty 守卫）
    if (editorDirty) return; // 同上：不许在 dirty 时切换/另开
    setEditingSlug(p.slug);
    setEditDetail(null);
    setEditLoading(true);
    try {
      setEditDetail(await api.getConnectorDetail(p.id));
    } catch {
      setEditingSlug(null);
      setMsg({ kind: "error", text: t("peers-config-load-failed") });
    } finally {
      setEditLoading(false);
    }
  };

  const collapseEditor = () => {
    if (editorDirty) return;
    setEditingSlug(null);
    setEditDetail(null);
    setEditorDirty(false);
    onDirtyChange(false);
  };

  const handleDirtyChange = useCallback(
    (dirty: boolean) => {
      setEditorDirty(dirty);
      onDirtyChange(dirty);
    },
    [onDirtyChange],
  );

  const doReconnect = async (p: A2aPeerPublic) => {
    setBusy({ slug: p.slug, kind: "reconnect" });
    setMsg(null);
    try {
      const r = await reconnectA2aPeer(p.slug);
      if (r && r.ok === false) {
        setMsg({ kind: "error", text: `${t("peers-reconnect-failed")}：${r.error || ""}`.replace(/：$/, "") });
      } else {
        setMsg({ kind: "ok", text: t("peers-reconnect-ok") });
      }
      await refresh();
    } catch {
      setMsg({ kind: "error", text: t("peers-reconnect-failed") });
    } finally {
      setBusy(null);
    }
  };

  const doDelete = async (p: A2aPeerPublic) => {
    setBusy({ slug: p.slug, kind: "delete" });
    setMsg(null);
    try {
      const r = await deleteA2aPeer(p.slug);
      if (r && r.ok === false) {
        setMsg({ kind: "error", text: `${t("peers-delete-failed")}：${r.error || ""}`.replace(/：$/, "") });
        return;
      }
      if (editingSlug === p.slug) {
        setEditingSlug(null);
        setEditDetail(null);
        setEditorDirty(false);
        onDirtyChange(false);
      }
      setConfirmDelete(null);
      setMsg({ kind: "ok", text: t("peers-delete-done") });
      await refresh();
    } catch {
      setMsg({ kind: "error", text: t("peers-delete-failed") });
    } finally {
      setBusy(null);
    }
  };

  const rows = peers ?? [];

  return (
    <div data-testid="a2a-peers-pane" className="space-y-4">
      {/* 头部：说明 + 计数 + 添加 */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-0 flex-1">
          <p className="text-[11.5px] leading-relaxed text-faint">{t("peers-subtitle")}</p>
        </div>
        {peers !== null && peers.length > 0 && (
          <span className="text-[11px] text-faint">{t("peers-count", { n: peers.length })}</span>
        )}
        <button
          type="button"
          onClick={() => {
            setMsg(null);
            void refresh();
          }}
          title={t("peers-refresh")}
          className="rounded-md p-1 text-faint hover:bg-hover hover:text-txt"
        >
          <RefreshCw className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          data-testid="a2a-peers-add"
          onClick={() => setWizardOpen(true)}
          className="flex items-center gap-1.5 rounded-lg border border-violet/40 bg-violet/10 px-3 py-1.5 text-[11.5px] font-medium text-violet hover:bg-violet/20"
        >
          <Plus className="h-3.5 w-3.5" /> {t("peer-add")}
        </button>
      </div>

      {msg && (
        <p
          className={`text-[11px] ${
            msg.kind === "error"
              ? "text-red-600 dark:text-red-400"
              : "text-green-700 dark:text-green-400"
          }`}
        >
          {msg.text}
        </p>
      )}

      {/* 清单 */}
      {peers === null ? (
        loadFailed ? (
          <div className="flex items-center gap-2 py-4 text-[12px] text-faint">
            {t("peers-load-failed")}
            <button
              type="button"
              onClick={() => void refresh()}
              className="rounded-lg border border-line px-2.5 py-1 text-[11px] text-txt hover:bg-hover"
            >
              <RefreshCw className="mr-1 inline h-3 w-3" />
              {t("peers-retry")}
            </button>
          </div>
        ) : (
          <div className="flex items-center gap-2 py-4 text-[12px] text-faint">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          </div>
        )
      ) : rows.length === 0 ? (
        <p className="rounded-lg border border-dashed border-line2 px-3 py-4 text-center text-[11.5px] text-faint">
          {t("peers-empty")}
        </p>
      ) : (
        <div className="space-y-2.5">
          {rows.map((p) => {
            const statusKey = peerStatusKey(p);
            const meta = STATUS_META[statusKey];
            const StatusIcon = meta.Icon;
            const trustKey = trustLabelKey(p.trust);
            const editing = editingSlug === p.slug;
            return (
              <div
                key={p.slug}
                data-testid={`a2a-peer-row-${p.slug}`}
                className={`rounded-xl border bg-card transition-colors ${
                  editing ? "border-violet/40" : "border-line"
                } ${!p.enabled ? "opacity-70" : ""}`}
              >
                <div className="flex flex-wrap items-center gap-2 px-3 py-2.5">
                  <span className={`inline-flex items-center gap-1 text-[11px] ${meta.cls}`}>
                    <StatusIcon className="h-3 w-3" />
                    {t(statusKey)}
                  </span>
                  <span className="min-w-0 truncate text-[12.5px] font-medium text-txt">
                    {p.name}
                  </span>
                  <span className="rounded-full border border-line2 bg-card2 px-1.5 py-px text-[10px] text-faint">
                    {trustKey ? t(trustKey) : p.trust}
                  </span>
                  <span className="min-w-0 flex-1 truncate font-mono text-[10.5px] text-faint" title={p.endpoint}>
                    {p.endpoint || "—"}
                  </span>
                  <span className="flex items-center gap-1.5">
                    <button
                      type="button"
                      data-testid={`a2a-peer-config-${p.slug}`}
                      onClick={() => void openEditor(p)}
                      disabled={editorDirty && !editing}
                      className="rounded-lg border border-line px-2.5 py-1 text-[11px] text-txt hover:bg-hover disabled:opacity-40"
                    >
                      {editing ? t("peers-config-opened") : t("peers-config")}
                    </button>
                    <button
                      type="button"
                      data-testid={`a2a-peer-reconnect-${p.slug}`}
                      onClick={() => void doReconnect(p)}
                      disabled={busy !== null || !p.enabled}
                      title={p.statusDetail || undefined}
                      className="flex items-center gap-1 rounded-lg border border-line px-2.5 py-1 text-[11px] text-muted hover:bg-hover hover:text-txt disabled:opacity-40"
                    >
                      {busy && busy.slug === p.slug && busy.kind === "reconnect" ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : (
                        <RefreshCw className="h-3 w-3" />
                      )}
                      {busy && busy.slug === p.slug && busy.kind === "reconnect"
                        ? t("peers-reconnecting")
                        : t("peers-reconnect")}
                    </button>
                    {confirmDelete === p.slug ? (
                      <>
                        <span className="text-[10px] text-faint">{t("peers-delete-confirm-title")}</span>
                        <button
                          type="button"
                          data-testid={`a2a-peer-delete-confirm-${p.slug}`}
                          onClick={() => void doDelete(p)}
                          disabled={busy !== null}
                          className="text-[11px] text-red-600 hover:opacity-80 disabled:opacity-50 dark:text-red-400"
                        >
                          {busy && busy.slug === p.slug && busy.kind === "delete" ? (
                            <Loader2 className="mr-1 inline h-3 w-3 animate-spin" />
                          ) : null}
                          {t("peers-delete-confirm-ok")}
                        </button>
                        <button
                          type="button"
                          onClick={() => setConfirmDelete(null)}
                          className="text-[11px] text-muted hover:text-txt"
                        >
                          {t("inbound-confirm-cancel")}
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        data-testid={`a2a-peer-delete-${p.slug}`}
                        onClick={() => setConfirmDelete(p.slug)}
                        disabled={busy !== null}
                        className="text-[11px] text-muted hover:text-red-600 disabled:opacity-50 dark:hover:text-red-400"
                      >
                        {t("peers-delete")}
                      </button>
                    )}
                  </span>
                </div>

                {/* 子配置：内嵌 ConfigEditor（同一 schema 驱动表单，PATCH 到 peer id） */}
                {editing && (
                  <div className="border-t border-line px-3 py-3">
                    <div className="mb-2 flex items-center gap-2">
                      <span className="text-[11px] font-semibold uppercase tracking-wide text-muted">
                        {t("peers-config-title", { name: p.name })}
                      </span>
                      <span className="flex-1" />
                      <button
                        type="button"
                        data-testid={`a2a-peer-collapse-${p.slug}`}
                        onClick={collapseEditor}
                        disabled={editorDirty}
                        title={editorDirty ? t("peers-collapse-dirty-hint") : undefined}
                        className="rounded-md px-2 py-0.5 text-[11px] text-faint hover:bg-hover hover:text-txt disabled:opacity-40"
                      >
                        {t("peers-collapse")}
                      </button>
                    </div>
                    {editLoading ? (
                      <div className="flex items-center gap-2 py-3 text-[12px] text-faint">
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      </div>
                    ) : editDetail ? (
                      <ConfigEditor
                        key={p.id}
                        connectorId={p.id}
                        config={editDetail.config}
                        defaults={editDetail.defaults}
                        schema={editDetail.configSchema}
                        onSaved={() => {
                          void refresh();
                          onSaved();
                        }}
                        onDirtyChange={handleDirtyChange}
                      />
                    ) : (
                      <p className="py-2 text-[11.5px] text-red-600 dark:text-red-400">
                        {t("peers-config-load-failed")}
                      </p>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {wizardOpen && (
        <A2aPeerAddModal
          onClose={() => setWizardOpen(false)}
          onAdded={(name) => {
            setMsg({ kind: "ok", text: t("wizard-added", { name }) });
            void refresh();
          }}
        />
      )}
    </div>
  );
}
