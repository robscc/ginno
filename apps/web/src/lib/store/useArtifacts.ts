"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "../runtime";
import type { Artifact, ArtifactPatch } from "../types";
import { uiText } from "../../i18n/uiText";
import type { RightTab } from "./useRightPanel";

export function useArtifacts({
  activeSessionId,
  activeSessionRef,
  rightPanelOpenRef,
  artifactsFollowRef,
  rightTabsHiddenRef,
  pendingFlashRef,
  setPanelBadge,
  setRightTabState,
  setFlashArtifactIds,
}: {
  activeSessionId: string | null;
  activeSessionRef: { current: string | null };
  rightPanelOpenRef: { current: boolean };
  artifactsFollowRef: { current: boolean };
  rightTabsHiddenRef: { current: RightTab[] };
  pendingFlashRef: { current: string[] };
  setPanelBadge: (
    updater: (
      prev: Partial<Record<RightTab, number>>,
    ) => Partial<Record<RightTab, number>>,
  ) => void;
  setRightTabState: (tab: RightTab) => void;
  setFlashArtifactIds: (ids: string[]) => void;
}) {
  // Stable setters/refs come from the right-panel and sessions slices; the
  // refs keep the reloadArtifacts dependency array identical to the pre-split
  // store (where they were same-scope constants).
  const panelHandlesRef = useRef({ setPanelBadge, setRightTabState, setFlashArtifactIds });
  const handlesRef = useRef({
    activeSessionRef,
    rightPanelOpenRef,
    artifactsFollowRef,
    rightTabsHiddenRef,
    pendingFlashRef,
  });
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  // 项目级 artifact 全量（@ 菜单跨会话选择用）：面板仍用会话级的
  // artifacts，两者数据源同端点不同 scope。仅喂菜单，不做 diff/跟随。
  const [projectArtifacts, setProjectArtifacts] = useState<Artifact[]>([]);
  // Ref mirror of the committed artifacts list: reloadArtifacts diffs against
  // it OUTSIDE the setState updater (side effects inside updaters double-fire
  // under dev StrictMode).
  const artifactsListRef = useRef<Artifact[]>([]);
  useEffect(() => {
    artifactsListRef.current = artifacts;
  }, [artifacts]);

  // The session scope artifacts were last loaded for. When the scope changes
  // (session switch/create/delete) the whole list swaps, which must NOT trigger
  // the fresh-artifact auto-follow/pulse (that's only for genuinely new rows
  // arriving within the same session).
  const artifactScopeRef = useRef<string | null | undefined>(undefined);

  const reloadArtifacts = useCallback(async () => {
    try {
      // Artifacts belong to the session: scope the fetch to the active session
      // (null → unscoped, which only happens in the boot gap before a session
      // is selected).
      const scope = handlesRef.current.activeSessionRef.current;
      const next = await api.listArtifacts("default", scope ?? undefined);
      const scopeChanged = artifactScopeRef.current !== scope;
      artifactScopeRef.current = scope;

      // §7.6 auto-follow: new artifact for the ACTIVE session → switch to the
      // Artifacts tab (if follow is on) and pulse-highlight the rows; when the
      // panel is collapsed, badge the edge dock instead and queue the pulse
      // for the next reopen (right-panel-redesign.md §3.6). Skip on a scope
      // change (session switch) — that swaps the whole list, it isn't a fresh
      // arrival. Diff + side effects stay OUTSIDE the setState updater:
      // StrictMode double-invokes updaters in dev and would double-count
      // badges/flashes.
      const prevIds = new Set(artifactsListRef.current.map((a) => a.id));
      const fresh = next.filter((a) => !prevIds.has(a.id));
      const mine = fresh.filter(
        (a) => !a.session_id || a.session_id === handlesRef.current.activeSessionRef.current,
      );
      setArtifacts(next);
      if (!scopeChanged && mine.length) {
        if (!handlesRef.current.rightPanelOpenRef.current) {
          panelHandlesRef.current.setPanelBadge((prev) => ({
            ...prev,
            artifacts: (prev.artifacts ?? 0) + mine.length,
          }));
          handlesRef.current.pendingFlashRef.current.push(...mine.map((a) => a.id));
        }
        if (
          handlesRef.current.artifactsFollowRef.current &&
          !handlesRef.current.rightTabsHiddenRef.current.includes("artifacts")
        ) {
          // Silent when collapsed: pre-select the tab so expanding lands on
          // Artifacts, but never yank the panel open.
          //
          // A HIDDEN Artifacts tab stays hidden: this is a background arrival,
          // not user intent, so un-hiding here would silently undo a
          // deliberate preference. The badge/flash path above still carries
          // the signal.
          panelHandlesRef.current.setRightTabState("artifacts");
          if (handlesRef.current.rightPanelOpenRef.current) {
            panelHandlesRef.current.setFlashArtifactIds(mine.map((a) => a.id));
            window.setTimeout(() => panelHandlesRef.current.setFlashArtifactIds([]), 2500);
          }
        }
      }
    } catch {
      /* ignore */
    }
  }, []);

  // Rescope the artifacts panel whenever the active session changes (switch /
  // create / delete), so it always shows the current session's artifacts.
  useEffect(() => {
    reloadArtifacts();
  }, [activeSessionId, reloadArtifacts]);

  // Reference-only delete: the file on disk is untouched, so a mistaken
  // delete is recoverable. Optimistic remove with rollback if the sidecar
  // rejects or is unreachable, so the panel never diverges from disk.
  const removeArtifact = useCallback(async (id: string) => {
    let snapshot: Artifact[] | null = null;
    setArtifacts((prev) => {
      snapshot = prev;
      return prev.filter((a) => a.id !== id);
    });
    try {
      const r = await api.deleteArtifact(id);
      if (!r.ok) throw new Error("delete failed");
    } catch {
      if (snapshot) setArtifacts(snapshot);
    }
  }, []);

  // Inspector edits: optimistic update, server round-trip, rollback on
  // rejection (e.g. blank name) — and reconcile with the canonical record
  // the server returns (whitelisted + trimmed).
  const patchArtifact = useCallback(async (id: string, patch: ArtifactPatch) => {
    const artPatch: Partial<Artifact> = {};
    if (patch.name !== undefined) artPatch.name = patch.name;
    if (patch.kind !== undefined) artPatch.kind = patch.kind;
    if (patch.schema !== undefined) artPatch.schema = patch.schema;
    let snapshot: Artifact[] | null = null;
    setArtifacts((prev) => {
      snapshot = prev;
      return prev.map((a) => (a.id === id ? { ...a, ...artPatch } : a));
    });
    try {
      const r = await api.updateArtifact(id, patch);
      if (!r.ok) throw new Error(r.error || uiText("net.updateFailed"));
      if (r.artifact) {
        const canonical = r.artifact;
        setArtifacts((prev) => prev.map((a) => (a.id === id ? { ...a, ...canonical } : a)));
      }
      return { ok: true };
    } catch (e) {
      if (snapshot) setArtifacts(snapshot);
      return { ok: false, error: e instanceof Error ? e.message : uiText("net.updateFailed") };
    }
  }, []);

  // @ 菜单的项目级列表：不带 session_id（端点语义 = 全量）。加载失败静默——
  // 菜单退回仅本会话条目，不影响面板。
  const reloadProjectArtifacts = useCallback(async () => {
    try {
      setProjectArtifacts(await api.listArtifacts("default"));
    } catch {
      /* sidecar 未起：菜单退回仅本会话 */
    }
  }, []);

  return {
    artifacts,
    projectArtifacts,
    reloadArtifacts,
    reloadProjectArtifacts,
    removeArtifact,
    patchArtifact,
  };
}
