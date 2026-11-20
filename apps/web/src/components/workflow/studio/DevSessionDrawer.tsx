"use client";

/**
 * DevSessionDrawer — 方案B 阶段5 屏4: edit a workflow's DSL through
 * conversation WITHOUT leaving the Studio. A right-side drawer hosting the
 * workflow-dev session's live stream (ONE session per mount, own WebSocket,
 * own message loop on top of the blocks.tsx renderers — ChatStream is a
 * singleton in AppShell and cannot be mounted twice).
 *
 * The point of the drawer is the version.propose card: the dev agent proposes
 * a DSL edit, the user applies/rejects inline, the server writes v(N+1) and
 * the Studio canvas refreshes via reloadWorkflows.
 *
 * 批次 3-2（绞杀者）：WS 生命周期 / 消息环 / 事件 reducer 已抽到
 * `components/session/`（useSessionSocket + useSessionTurn + sessionBlocks，
 * 与 PinStream 同骨架）；本组件只保留抽屉的差异层——version.propose 卡、
 * run 活动条、抽屉 chrome 与 composer。
 */

import { useEffect, useRef, useState } from "react";
import { AlertCircle, ArrowUp, FileEdit, Loader2, MessagesSquare, Square, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import type { WorkflowDef } from "@/lib/types";
import { loadToolLabels, toolLabel } from "@/lib/toolLabels";
import { useRunStatusLabel } from "@/components/chat/RunBlocks";
import { DiffView } from "@/components/workflow/DiffView";
import { ContextBlocks, InnerBlocks, UserBlocks, type Block } from "@/components/chat/blocks";
import { mapHistoryToMsgs, type SessionMsg } from "@/components/session/sessionBlocks";
import { useSessionTurn } from "@/components/session/useSessionTurn";

interface VersionPropose {
  workflow_id: string;
  from_version: number;
  diff: string;
  rationale: string;
}

/** 抽屉消息 = 共享基座（无 pin 的 teamReply/retryText 扩展）。 */
type DrawerMsg = SessionMsg;

/**
 * Outer shell: session resolution (reuse the bound workflow-dev session or
 * create one — same title convention as the old router.push flow) + the
 * overlay chrome. The inner stream is keyed by session id so a session switch
 * is a clean remount (PinStream's contract).
 */
export function DevSessionDrawer({ wf, onClose }: { wf: WorkflowDef; onClose: () => void }) {
  const g = useGinno();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const t = useTranslations("wf.dev");

  useEffect(() => {
    let alive = true;
    const existing = g.sessions.find((s) => s.workflow_id === wf.id);
    if (existing) {
      setSessionId(existing.id);
      return;
    }
    setErr(null);
    g.newSession("workflow-dev", { title: t("sessionTitle", { name: wf.name }), workflow_id: wf.id }).then(
      (s) => {
        if (!alive) return;
        if (s?.id) setSessionId(s.id);
        else setErr(t("sessionCreateFailed"));
      },
    );
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wf.id]);

  // Esc closes the drawer (the composer never sees it — no trap needed).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50" onClick={onClose}>
      <div
        className="absolute right-0 top-0 flex h-full w-[420px] max-w-[92vw] flex-col border-l border-line bg-panel shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-1.5 border-b border-line px-3 py-2">
          <MessagesSquare className="h-3.5 w-3.5 shrink-0 text-violet" />
          <span className="min-w-0 flex-1 truncate text-[12.5px] font-semibold text-txt">
            {t("title", { name: wf.name })}
          </span>
          <span className="rounded border border-line2 px-1 font-mono text-[10px] text-faint">
            v{wf.version ?? 1}
          </span>
          <button
            onClick={onClose}
            title={t("closeTitle")}
            className="btn-press rounded border border-line2 px-1.5 py-0.5 text-[11px] text-muted hover:text-txt"
          >
            <X className="h-3 w-3" />
          </button>
        </div>

        {sessionId ? (
          <DrawerStream
            key={sessionId}
            sessionId={sessionId}
            onApplied={() => {
              // 应用后服务端写 v(N+1) 并广播 workflows.changed — 主动刷新让
              // Studio 画布立即跟上（不依赖全局 ChatStream 的转发）。
              void g.reloadWorkflows();
              void g.reloadWorkflowRuns();
            }}
          />
        ) : (
          <div className="flex flex-1 items-center justify-center px-6 text-center text-xs text-faint">
            {err ? <span className="text-red">{err}</span> : <Loader2 className="h-4 w-4 animate-spin" />}
          </div>
        )}
      </div>
    </div>
  );
}

/** The session stream: shared socket/message loop (useSessionTurn) plus the
 *  two drawer-specific renders: the version.propose diff card and the subtle
 *  run-activity strip. */
function DrawerStream({
  sessionId,
  onApplied,
}: {
  sessionId: string;
  onApplied: () => void;
}) {
  const [propose, setPropose] = useState<VersionPropose | null>(null);
  const [proposeResult, setProposeResult] = useState<{ decision: "allow" | "deny"; fromVersion: number } | null>(null);
  const [runLines, setRunLines] = useState<Record<string, string>>({});
  const [input, setInput] = useState("");
  const t = useTranslations("wf.dev");
  const statusLabel = useRunStatusLabel();

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  // 语言加载由根部 I18nProvider 负责（原 loadPromptLang 已移除）。
  useEffect(() => {
    loadToolLabels();
  }, []);

  const s = useSessionTurn<DrawerMsg>({
    sessionId,
    midPrefix: "dm",
    mapHistory: (res, mid) => mapHistoryToMsgs<DrawerMsg>(res, mid),
    texts: {
      interruptedTool: () => t("interruptedTool"),
      unknownError: () => t("unknownError"),
      compacted: (count) => t("compaction", { count }),
    },
    // 抽屉专属事件：提案卡 / run 活动条 / 远端 DSL 变更联动。
    onExtraEvent: (ev) => {
      switch (ev.event) {
        case "version.propose":
          // The whole point of the drawer: the dev agent's DSL edit proposal.
          setPropose({
            workflow_id: ev.workflow_id as string,
            from_version: (ev.from_version as number) ?? 0,
            diff: (ev.diff as string) ?? "",
            rationale: (ev.rationale as string) ?? "",
          });
          break;
        case "run.bind":
        case "run.status": {
          // MINIMAL: the Studio observer shows full detail — the drawer only
          // surfaces a subtle "run #xxxx · 状态" strip for runs the dev agent
          // triggered (or that were presented into this session).
          const rid = ev.run_id as string;
          if (rid) {
            const st = (ev.status as string) || "bound";
            setRunLines((prev) => ({ ...prev, [rid]: st }));
          }
          break;
        }
        case "workflows.changed":
          // A propose was applied (here or elsewhere) — the shell's explicit
          // onApplied covers the local decision; this catches remote edits.
          onApplied();
          break;
        default:
          break;
      }
    },
  });
  const { messages, liveId, busy, permission, sendError, wsStatus, postTurn, stopTurn, respond } = s;

  useEffect(() => {
    if (stickRef.current) bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages, propose]);

  function onScroll() {
    const el = scrollRef.current;
    if (!el) return;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  }

  function sendText(raw: string) {
    const text = raw.trim();
    if (!text) return;
    postTurn({
      text,
      userBlocks: [{ kind: "text", text }],
      notReadyLabel: () => t("notReady"),
      failedLabel: () => t("sendFailed"),
      onSent: () => {
        setInput("");
        requestAnimationFrame(() => {
          if (textareaRef.current) textareaRef.current.style.height = "auto";
        });
      },
      onFailed: (failed) => setInput(failed),
    });
  }

  /** Exact ChatStream wire format (ChatStream.tsx respondPropose): the plain
   *  permission_response channel with no propose_id — the server tracks the
   *  pending proposal interrupt per session and resumes it with {decision}. */
  function respondPropose(decision: "allow" | "deny") {
    respond(decision);
    const p = propose;
    setPropose(null);
    if (p) {
      setProposeResult({ decision, fromVersion: p.from_version });
      if (decision === "allow") onApplied();
      window.setTimeout(() => setProposeResult(null), 4000);
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !e.nativeEvent.isComposing) {
      e.preventDefault();
      sendText(input);
    }
  }

  function onInput(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setInput(e.target.value);
    const el = e.target;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 112)}px`;
  }

  const connecting = wsStatus !== "live";

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* run activity strip (MINIMAL — full detail lives in the observer) */}
      {Object.keys(runLines).length > 0 && (
        <div className="flex flex-wrap gap-x-3 gap-y-0.5 border-b border-line px-3 py-1 text-[10px] text-faint">
          {Object.entries(runLines).map(([rid, st]) => (
            <span key={rid} className="flex items-center gap-1">
              <span
                className={`h-1 w-1 rounded-full ${
                  st === "running" ? "animate-pulse bg-green" : "bg-faint"
                }`}
              />
              {t("runStrip", { id: rid.slice(0, 8), status: statusLabel(st) })}
            </span>
          ))}
        </div>
      )}

      {/* messages */}
      <div ref={scrollRef} onScroll={onScroll} className="min-h-0 flex-1 space-y-2 overflow-y-auto px-2.5 py-2.5">
        {messages.map((m) => {
          if (m.role === "system") {
            return (
              <ContextBlocks
                key={m.id}
                blocks={m.blocks.filter((b): b is Extract<Block, { kind: "context" }> => b.kind === "context")}
              />
            );
          }
          if (m.role === "user") {
            return (
              <div key={m.id} className="flex justify-end">
                <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md border border-violet/25 bg-violet/[0.12] px-3 py-1.5 text-[13px] leading-relaxed text-txt">
                  <UserBlocks blocks={m.blocks} />
                </div>
              </div>
            );
          }
          if (m.error) {
            const text = m.blocks.find((b) => b.kind === "text");
            return (
              <div key={m.id} className="rounded-xl border border-red/40 bg-red/[0.07] px-3 py-2">
                <div className="mb-1 flex items-center gap-1.5 text-xs font-medium text-red">
                  <AlertCircle className="h-3.5 w-3.5 shrink-0" />
                  {t("replyFailed")}
                </div>
                <div className="selectable whitespace-pre-wrap break-words text-xs leading-relaxed text-muted">
                  {"text" in (text ?? {}) ? String((text as { text?: string })?.text ?? "") : ""}
                </div>
              </div>
            );
          }
          const streaming = m.id === liveId;
          return (
            <div key={m.id} className="rounded-2xl rounded-bl-md border border-line bg-card px-3 py-2 text-[13px] leading-relaxed text-txt">
              <InnerBlocks blocks={m.blocks} streaming={streaming} />
              {streaming && m.blocks.length === 0 && (
                <Loader2 className="h-3.5 w-3.5 animate-spin text-faint" />
              )}
            </div>
          );
        })}

        {/* the drawer's reason to exist: the DSL 变更提案 diff card */}
        {propose && <DrawerProposeCard propose={propose} onDecide={respondPropose} />}
        {proposeResult && (
          <div className="rounded-lg border border-line2 bg-card2/60 px-3 py-1.5 text-[11px] text-muted">
            {proposeResult.decision === "allow" ? t("applied") : t("rejected")} · v
            {proposeResult.fromVersion} →{" "}
            {proposeResult.decision === "allow" ? t("newVersion") : t("unchanged")}
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* inline permission confirmation (non-workflow tool the agent needs) */}
      {permission && (
        <div className="mx-2.5 mb-2 rounded-xl border border-yellow/40 bg-yellow/10 p-2.5">
          <div className="mb-1 flex items-center gap-1.5 text-xs font-medium text-yellow">
            <AlertCircle className="h-3.5 w-3.5 shrink-0" />
            {t("permissionRequired")} <code className="font-mono text-txt">{toolLabel(permission.tool)}</code>
          </div>
          <pre className="mb-2 max-h-20 overflow-auto rounded-lg bg-base/60 p-2 font-mono text-[10px] leading-snug text-muted">
            {JSON.stringify(permission.args, null, 2)}
          </pre>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => respond("allow")}
              className="rounded-lg bg-violet px-2.5 py-1 text-xs font-medium text-white transition-opacity hover:opacity-90"
            >
              {t("allowOnce")}
            </button>
            <button
              type="button"
              onClick={() => respond("deny")}
              className="rounded-lg border border-line2 px-2.5 py-1 text-xs text-muted transition-colors hover:text-txt"
            >
              {t("deny")}
            </button>
          </div>
        </div>
      )}

      {(connecting || sendError) && (
        <div className="flex items-center gap-1.5 px-3 pb-1 text-[11px] text-faint">
          {connecting && <Loader2 className="h-3 w-3 animate-spin" />}
          {sendError ?? (wsStatus === "reconnecting" ? t("reconnecting") : t("connecting"))}
        </div>
      )}

      {/* composer */}
      <div className="border-t border-line px-2.5 py-2">
        <div className="flex items-end gap-1.5 rounded-xl border border-line2 bg-card px-2.5 py-1.5 transition-colors focus-within:border-violet/60">
          <textarea
            ref={textareaRef}
            rows={1}
            value={input}
            onChange={onInput}
            onKeyDown={onKeyDown}
            placeholder={t("placeholder")}
            className="max-h-28 min-h-[24px] flex-1 resize-none bg-transparent text-[13px] leading-6 text-txt outline-none placeholder:text-faint"
          />
          {busy ? (
            <button
              type="button"
              onClick={stopTurn}
              title={t("stopTurn")}
              className="mb-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-red/15 text-red transition-colors hover:bg-red/25"
            >
              <Square className="h-3 w-3" />
            </button>
          ) : (
            <button
              type="button"
              onClick={() => sendText(input)}
              disabled={!input.trim()}
              title={t("sendTitle")}
              className="mb-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-violet text-white transition-opacity hover:opacity-90 disabled:opacity-30"
            >
              <ArrowUp className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/** workflow_propose_edit diff confirmation card, drawer-width variant of
 *  ChatStream's ProposeCard (collapsed-by-default diff with hunk count; the
 *  session graph is paused at the tool's interrupt until the user decides). */
function DrawerProposeCard({
  propose,
  onDecide,
}: {
  propose: VersionPropose;
  onDecide: (decision: "allow" | "deny") => void;
}) {
  const [deciding, setDeciding] = useState<null | "allow" | "deny">(null);
  const [diffOpen, setDiffOpen] = useState(false);
  const hunks = (propose.diff.match(/^@@/gm) || []).length;
  const t = useTranslations("wf.dev");
  const decide = (d: "allow" | "deny") => {
    if (deciding) return;
    setDeciding(d);
    onDecide(d);
  };
  return (
    <div className="rounded-xl border border-yellow/30 bg-yellow/[0.04] p-2.5">
      <div className="mb-1 flex flex-wrap items-center gap-1.5 text-[12.5px] font-medium text-yellow">
        <FileEdit className="h-3.5 w-3.5 shrink-0" />
        {t("proposalTitle")}
        <span className="rounded border border-yellow/40 px-1.5 py-0.5 text-[10px] font-normal text-muted">
          {t("proposalVersion", { id: propose.workflow_id, from: propose.from_version })}
        </span>
      </div>
      {propose.rationale && (
        <div className="selectable mb-2 text-xs text-muted">{t("rationale", { reason: propose.rationale })}</div>
      )}
      <button
        onClick={() => setDiffOpen((v) => !v)}
        className="mb-2 flex items-center gap-1 text-[11px] text-faint hover:text-muted"
      >
        {diffOpen ? t("collapseDiff") : t("viewDiff", { count: hunks })}
      </button>
      {diffOpen && <DiffView diff={propose.diff} />}
      <div className="mt-2.5 flex gap-2">
        <button
          onClick={() => decide("allow")}
          disabled={!!deciding}
          className="btn-press flex items-center gap-1 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
        >
          {deciding === "allow" && <Loader2 className="h-3 w-3 animate-spin" />}
          {deciding === "allow" ? t("applying") : t("apply")}
        </button>
        <button
          onClick={() => decide("deny")}
          disabled={!!deciding}
          className="btn-press flex items-center gap-1 rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:bg-red/10 hover:text-red disabled:opacity-50"
        >
          {deciding === "deny" && <Loader2 className="h-3 w-3 animate-spin" />}
          {t("deny")}
        </button>
      </div>
    </div>
  );
}
