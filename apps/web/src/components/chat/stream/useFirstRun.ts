"use client";

// useFirstRun — V2 P2-3 首启引导卡的状态与动作（自 ChatStream.tsx 机械拆出，
// 行为逐字一致）。

import { useEffect, useState } from "react";
import type { MutableRefObject } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import { pushNotice } from "@/lib/notice";
import { isDesktop } from "@/lib/desktop";
import type { useGinno } from "@/lib/store";
import type { SessionMeta } from "@/lib/types";
import type { useModelPick } from "../useModelPick";

// V2 P2-3: the first-run starter is shown until the user acts on / dismisses it.
const FIRST_RUN_LS_KEY = "ginno-first-run-done";

export function useFirstRun({
  g,
  session,
  textareaRef,
  homeModel,
  setPendingMounts,
}: {
  g: ReturnType<typeof useGinno>;
  session: SessionMeta | null;
  textareaRef: MutableRefObject<HTMLTextAreaElement | null>;
  homeModel: ReturnType<typeof useModelPick>["homeModel"];
  setPendingMounts: ReturnType<typeof useModelPick>["setPendingMounts"];
}) {
  const tc = useTranslations("chat");
  const router = useRouter();
  // ── V2 P2-3 first-run starter (home view only) ─────────────────────────
  const [firstRunDone, setFirstRunDone] = useState(false);
  // localStorage may only be read after mount (SSR pre-render has no window);
  // gate the cards on this so a dismissed starter never flashes back.
  const [firstRunChecked, setFirstRunChecked] = useState(false);
  const [firstRunBusy, setFirstRunBusy] = useState(false);

  // ── V2 P2-1 / P2-3 localStorage hydration (client-only) ─────────────────
  // try/catch every read/write: localStorage throws in private windows and can
  // be blocked entirely, and none of this is load-bearing for the chat itself.
  useEffect(() => {
    try {
      setFirstRunDone(localStorage.getItem(FIRST_RUN_LS_KEY) === "1");
    } catch {
      /* see above */
    }
    setFirstRunChecked(true);
  }, []);

  // ── V2 P2-3 first-run starter ──────────────────────────────────────────
  function dismissFirstRun() {
    setFirstRunDone(true);
    try {
      localStorage.setItem(FIRST_RUN_LS_KEY, "1");
    } catch {
      /* dismissal still applies for this mount */
    }
  }

  /** Card 2: hand the user the composer — the starter has done its job. */
  function firstRunAsk() {
    dismissFirstRun();
    requestAnimationFrame(() => textareaRef.current?.focus());
  }

  /** Card 3: example workflows live in the workflow library. */
  function firstRunWorkflow() {
    dismissFirstRun();
    router.push("/workflows");
  }

  /** Card 1: pick a directory with the same native dialog + createFolder +
   *  pending-mount pipeline the home mount chip uses, then open a session with
   *  the folder mounted from birth (`newSession`'s context_folders contract).
   *  Plain-browser dev has no native bridge; point at the mount chip instead of
   *  failing silently. */
  async function firstRunMountFolder() {
    if (firstRunBusy) return;
    if (!isDesktop()) {
      pushNotice("info", tc("firstRun.browserHint"), { source: "general" });
      requestAnimationFrame(() => textareaRef.current?.focus());
      return;
    }
    setFirstRunBusy(true);
    try {
      let picked: string | null = null;
      try {
        const { open } = await import("@tauri-apps/plugin-dialog");
        const p = await open({ directory: true, multiple: false });
        if (typeof p === "string") picked = p;
      } catch {
        pushNotice("error", tc("firstRun.pickFailed"), { source: "general" });
        return;
      }
      if (!picked) return; // user cancelled — keep the starter on screen
      const c = await api.createFolder({ path: picked, access: "rw", load_rules: true });
      if (!c.ok || !c.folder) {
        pushNotice("error", c.error || tc("firstRun.mountFailed"), { source: "general" });
        return;
      }
      const ids = [c.folder.id];
      // Same pending state the chip edits; also the fallback if creation fails.
      setPendingMounts({ ids, primary: c.folder.id });
      const s = await g.newSession(g.agents[0]?.id ?? null, {
        ...(homeModel ?? {}),
        context_folders: ids,
        primary_folder: c.folder.id,
      });
      if (!s) return; // store banner (sessionError) carries the reason
      dismissFirstRun();
      requestAnimationFrame(() => textareaRef.current?.focus());
    } catch {
      pushNotice("error", tc("firstRun.mountFailed"), { source: "general" });
    } finally {
      setFirstRunBusy(false);
    }
  }

  // V2 P2-3: starter shows only on the home view, with no sessions at all.
  const showFirstRun =
    firstRunChecked && g.ready && !session && g.sessions.length === 0 && !firstRunDone;

  return {
    showFirstRun,
    firstRunBusy,
    dismissFirstRun,
    firstRunAsk,
    firstRunWorkflow,
    firstRunMountFolder,
  };
}
