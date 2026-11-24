"use client";

import { useCallback, useEffect, useRef } from "react";
import { useTranslations } from "next-intl";
import { listCodeRoots } from "@/lib/api/code";
import { splitRootPathIn, type MarkdownFileTarget } from "@/lib/codePaths";
import { pushNotice } from "@/lib/notice";
import { useGinno } from "@/lib/store";
import type { SessionMeta } from "@/lib/types";
import { useChatT } from "../blocks/i18n";

export function useMarkdownFileLinks(session: SessionMeta | null) {
  const { activeSessionId, codeRootId, openInCode } = useGinno();
  const { tc } = useChatT();
  const code = useTranslations("code.notice");
  const sessionId = session?.id;
  const remotePeer = session?.remote_peer;
  const peerName = session?.remote_peer_name || remotePeer;
  // Keep the request alive when streaming Markdown replaces a link node.
  // A response from a previous session must never open in the current panel.
  const active = useRef(activeSessionId);
  useEffect(() => {
    active.current = activeSessionId;
    return () => { active.current = null; };
  }, [activeSessionId]);

  const open = useCallback(async ({ path, line }: MarkdownFileTarget) => {
    if (!sessionId || active.current !== sessionId) return;
    if (remotePeer) {
      const name = path.split("/").pop() || path;
      pushNotice("info", tc("refChip.remoteNotice", {
        name, path: line ? `${path}:${line}` : path, peer: peerName || remotePeer,
      }), { inbox: false });
      return;
    }
    try {
      const roots = await listCodeRoots("default", sessionId);
      if (active.current !== sessionId) return;
      const target = splitRootPathIn(roots, path);
      if (target?.rel) {
        openInCode({ rootId: target.rootId, path: target.rel, line });
      } else {
        // Outside every root: hand the absolute path to the panel, which shows
        // the §3.2 guidance (notice + "add its folder") instead of a dead end.
        openInCode({ rootId: codeRootId ?? roots[0]?.id ?? "session", path, line });
      }
    } catch {
      if (active.current === sessionId) pushNotice("error", code("readFailed"), { inbox: false });
    }
  }, [sessionId, remotePeer, peerName, codeRootId, openInCode, code, tc]);

  return sessionId ? open : undefined;
}
