"use client";

// Composer — 输入框及其周边 UI（自 ChatStream.tsx 机械拆出，渲染逐字一致）。
// 所有状态仍归 ChatStream（composer 是单实例、home/session 两槽按 key 重挂，
// 状态必须留在父级），这里只接收状态 + 回调并渲染。

import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import { Paperclip, Keyboard, ArrowUp, AlertCircle, Square, RotateCcw } from "lucide-react";
import { useTranslations } from "next-intl";
import { t } from "@/i18n/provider";
import type { useGinno } from "@/lib/store";
import { ContextFoldersChip } from "@/components/shell/ContextFoldersChip";
import { ComposerMenu } from "@/components/chat/ComposerMenu";
import { pruneMentions } from "@/components/chat/commandMenu";
import type { MenuItem, ResolvedMention, Trigger } from "@/components/chat/commandMenu";
import type { SessionMeta } from "@/lib/types";
import {
  caretOnFirstLine,
  caretOnLastLine,
  navDown,
  navUp,
  type HistoryState,
} from "../inputHistory";
import type { Attachment, FileAttachment } from "../streamCore";
import type { SteerItem } from "@/lib/steerQueue";
import { QueueBar } from "./QueueBar";
import { ModelChipMenu } from "./ModelChipMenu";
import { ConnStatusChip } from "./ConnStatusChip";
import { ComposerAttachmentRows, ImagesReadingRow } from "./ComposerAttachmentRows";
import { ModBand, type ModBandGroup } from "../mod/ModBand";
import type { useModelPick } from "../useModelPick";

type MenuState = { items: MenuItem[]; active: number; trigger: Trigger } | null;

export function Composer({
  g,
  session,
  isHome,
  running,
  parked,
  wsStatus,
  onReconnect,
  composerHint,
  imagesReading,
  steerItems,
  dropSteer,
  recallSteers,
  composerBoxRef,
  textareaRef,
  fileRef,
  composerH,
  setComposerH,
  onResizeStart,
  dragOver,
  setDragOver,
  addFiles,
  input,
  setInput,
  attachments,
  setAttachments,
  fileAttachments,
  setFileAttachments,
  menu,
  setMenu,
  pickItem,
  recomputeMenu,
  mentionsRef,
  historyFor,
  inputHistoryRef,
  send,
  stopTurn,
  goalActive,
  modBands,
  onModUiPress,
  model,
}: {
  g: ReturnType<typeof useGinno>;
  session: SessionMeta | null;
  isHome: boolean;
  running: boolean;
  parked: boolean;
  wsStatus: "connecting" | "live" | "reconnecting" | "offline";
  onReconnect: () => void;
  composerHint: string | null;
  imagesReading: number;
  steerItems: SteerItem[];
  dropSteer: (sid: string, steerId: string) => void;
  recallSteers: (sid: string) => void;
  composerBoxRef: MutableRefObject<HTMLDivElement | null>;
  textareaRef: MutableRefObject<HTMLTextAreaElement | null>;
  fileRef: MutableRefObject<HTMLInputElement | null>;
  composerH: number | undefined;
  setComposerH: Dispatch<SetStateAction<number | undefined>>;
  onResizeStart: (e: React.PointerEvent) => void;
  dragOver: boolean;
  setDragOver: Dispatch<SetStateAction<boolean>>;
  addFiles: (files: FileList | File[] | null) => Promise<void>;
  input: string;
  setInput: Dispatch<SetStateAction<string>>;
  attachments: Attachment[];
  setAttachments: Dispatch<SetStateAction<Attachment[]>>;
  fileAttachments: FileAttachment[];
  setFileAttachments: Dispatch<SetStateAction<FileAttachment[]>>;
  menu: MenuState;
  setMenu: Dispatch<SetStateAction<MenuState>>;
  pickItem: (item: MenuItem) => void;
  recomputeMenu: (text: string) => void;
  mentionsRef: { current: Record<string, ResolvedMention[]> };
  historyFor: (sid: string) => HistoryState;
  inputHistoryRef: { current: Record<string, HistoryState> };
  send: () => void;
  stopTurn: () => void;
  goalActive: boolean;
  modBands: ModBandGroup[] | undefined;
  onModUiPress: (sessionId: string, generation: number, actionId: string) => void;
  model: ReturnType<typeof useModelPick>;
}) {
  const tc = useTranslations("chat");
  const {
    pendingMounts, setPendingMounts, modelOpen, setModelOpen, pickModel,
    enabledProviders, targetAgent, homeEffective, homeModelSource, modelChipLabel,
  } = model;

  return (
    <div
      ref={composerBoxRef}
      /* 树拖拽（pointer events）用 elementFromPoint 识别这里为放置目标 */
      data-composer="1"
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        void addFiles(e.dataTransfer.files);
      }}
      className={`relative rounded-2xl border bg-card p-2.5 transition-colors focus-within:border-line2 ${
        dragOver || g.composerDragOver ? "border-violet/70 ring-2 ring-violet/30" : "border-line"
      }`}
    >
      <div
        onPointerDown={onResizeStart}
        title={tc("composer.resizeTitle")}
        onDoubleClick={() => setComposerH(undefined)}
        className="absolute -top-1 left-1/2 z-10 flex h-2 w-12 -translate-x-1/2 cursor-ns-resize items-center justify-center rounded-full hover:bg-line2/60"
        aria-label={tc("composer.resizeAria")}
      >
        <span className="h-0.5 w-6 rounded-full bg-line2" />
      </div>
      {menu && (
        <ComposerMenu
          items={menu.items}
          active={menu.active}
          onPick={pickItem}
          onHover={(i) => setMenu((m) => (m ? { ...m, active: i } : m))}
        />
      )}
      {/* @ 投递 steering 提示（设计稿视图 1）：收件人 working 时，
          投递会在下个工具边界以 steering 注入，不打断当前 turn。 */}
      {(() => {
        if (session?.type !== "team") return null;
        const team = g.teams.find((t) => t.main_session_id === session.id);
        const working = (team?.member_rows ?? []).filter(
          (r) => r.status === "working" && input.includes(`@agent:${r.agent_id}`),
        );
        if (!working.length) return null;
        return (
          <div className="mb-2 rounded-md border border-line bg-card/60 px-2.5 py-1.5 text-[11px] leading-relaxed text-faint">
            {tc("team.steerHint", { names: working.map((r) => r.display_name).join(t(", ", "、")) })}
          </div>
        );
      })()}
      {session && (modBands?.some((g) => g.tree.length > 0) ?? false) && (
        // Claude Code Mods band 插槽(claude-code-mods-design.md §7.1):
        // composerHint 之上、与 hint/steer 条同级;每 mod 一段。空树零渲染。
        <div className="mb-2">
          <ModBand
            groups={modBands!}
            onPress={(actionId, generation) => onModUiPress(session.id, generation, actionId)}
          />
        </div>
      )}
      {composerHint && (
        // Blocked send, made visible. A silent return here read as a
        // dead send button (the original bug report).
        <div className="mb-2 flex items-center gap-1.5 rounded-lg border border-yellow/40 bg-yellow/10 px-2.5 py-1.5 text-xs text-yellow">
          <AlertCircle className="h-3.5 w-3.5 shrink-0" />
          <span>{composerHint}</span>
        </div>
      )}
      {imagesReading > 0 && <ImagesReadingRow />}
      <ComposerAttachmentRows
        attachments={attachments}
        fileAttachments={fileAttachments}
        uploadingLabel={session ? tc("composer.uploading") : tc("composer.uploadsOnSend")}
        onRemoveImage={(i) => setAttachments((l) => l.filter((_, j) => j !== i))}
        onRemoveFile={(i) => setFileAttachments((l) => l.filter((_, j) => j !== i))}
      />
      {session?.type === "subagent" && running && (
        // 子任务视图的输入语义（subagent-design.md §6.2）：running 时的输入
        // 走既有 steering 通道（useSteerQueue，同主对话），这里只把语义标出来。
        <div className="mb-1 flex items-center gap-1.5 px-0.5 text-[10px] text-faint">
          <RotateCcw className="h-3 w-3 shrink-0 animate-pulse" aria-hidden />
          <span>{tc("composer.subtaskSteerHint")}</span>
        </div>
      )}
      {session && steerItems.length > 0 && (
        <QueueBar items={steerItems} sessionId={session.id} onDrop={dropSteer} />
      )}
      <textarea
        ref={textareaRef}
        value={input}
        onChange={(e) => {
          const v = e.target.value;
          setInput(v);
          recomputeMenu(v);
          // Keep the resolved mentions in sync with the visible tokens.
          if (session) {
            mentionsRef.current[session.id] = pruneMentions(
              mentionsRef.current[session.id] ?? [],
              v,
            );
          }
        }}
        onPaste={(e) => {
          if (e.clipboardData?.files?.length) {
            e.preventDefault();
            void addFiles(e.clipboardData.files);
          }
        }}
        onKeyDown={(e) => {
          // Guard IME composition: without this, pressing Enter to commit a
          // CJK candidate (e.g. Chinese) fires send() mid-composition and
          // posts partial/garbled text. isComposing + keyCode 229 cover it.
          const composing = e.nativeEvent.isComposing || e.keyCode === 229;
          // While the autocomplete menu is open, navigate/confirm it
          // instead of sending. (IME guard first — same as send.)
          if (menu && !composing) {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setMenu((m) => m && { ...m, active: (m.active + 1) % m.items.length });
              return;
            }
            if (e.key === "ArrowUp") {
              e.preventDefault();
              setMenu((m) => m && { ...m, active: (m.active - 1 + m.items.length) % m.items.length });
              return;
            }
            if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
              e.preventDefault();
              pickItem(menu.items[menu.active]);
              return;
            }
            if (e.key === "Escape") {
              e.preventDefault();
              setMenu(null);
              return;
            }
          }
          // Esc while a turn runs = stop it (same as the ⏹ button);
          // only when the composer is focused and nothing is parked —
          // a parked prompt's own card (Deny / 跳过) is the deliberate
          // exit, a stray Escape must not cancel the whole turn.
          // ↑ on an EMPTY composer takes the queued steer entries back for editing —
          // one per line, in order (Claude Code's "take back what you
          // queued"). Guarded on empty input so it never steals the caret
          // when there is text to navigate.
          if (e.key === "ArrowUp" && !composing && !input && session && steerItems.length > 0) {
            e.preventDefault();
            recallSteers(session.id);
            return;
          }
          // ↑/↓ input history recall (shell-style): only when the caret
          // sits on the first (↑) / last (↓) line — multi-line editing
          // keeps native caret moves. Priority: IME > command menu >
          // steer recall (above) > this. Nothing to navigate → fall
          // through to native behavior (no preventDefault).
          if (!composing && session && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
            const el = textareaRef.current;
            const caret = el?.selectionStart ?? input.length;
            const base = historyFor(session.id);
            const r =
              e.key === "ArrowUp"
                ? caretOnFirstLine(input, caret)
                  ? navUp(base, input)
                  : null
                : caretOnLastLine(input, caret)
                  ? navDown(base, input)
                  : null;
            if (r) {
              e.preventDefault();
              inputHistoryRef.current[session.id] = r.state;
              setInput(r.text);
              setMenu(null); // 程序化回填后菜单必须关(与 pickItem 一致)
              requestAnimationFrame(() => {
                const el2 = textareaRef.current;
                if (el2) {
                  el2.focus();
                  el2.setSelectionRange(r.text.length, r.text.length);
                }
              });
              return;
            }
          }
          if (e.key === "Escape" && !composing && running && !parked) {
            e.preventDefault();
            stopTurn();
            return;
          }
          if (e.key === "Enter" && !e.shiftKey && !composing) {
            e.preventDefault();
            send();
          }
        }}
        rows={2}
        placeholder={tc("composer.placeholder")}
        style={
          composerH != null
            ? { height: composerH - 56, minHeight: 44, overflowY: "auto" }
            : { maxHeight: 240 }
        }
        className="w-full resize-none bg-transparent px-1.5 py-1 text-sm text-txt outline-none placeholder:text-faint"
      />
      <div className="flex flex-wrap items-center justify-between gap-1 px-1 pt-1">
        <div className="flex min-w-0 items-center gap-1 text-faint">
          <input
            ref={fileRef}
            type="file"
            accept="image/*,.xlsx,.xls,.xlsm,.csv,.tsv,.docx,.pptx,.pdf,.json,.xml,.txt,.md"
            multiple
            className="hidden"
            onChange={(e) => {
              void addFiles(e.target.files);
              e.target.value = "";
            }}
          />
          <button
            onClick={() => fileRef.current?.click()}
            className="rounded-md p-1.5 transition-colors hover:bg-card2 hover:text-muted"
            title={tc("composer.addAttachmentsTitle")}
          >
            <Paperclip className="h-4 w-4" />
          </button>
          <button
            onClick={() => {
              // Start a slash command — only valid as the FIRST token, so
              // this only makes sense on an empty composer. With existing
              // text we just focus the textarea instead of mangling it.
              if (!input.trim()) {
                setInput("/");
                requestAnimationFrame(() => {
                  const el = textareaRef.current;
                  if (el) {
                    el.focus();
                    el.setSelectionRange(1, 1);
                  }
                  recomputeMenu("/");
                });
              } else {
                textareaRef.current?.focus();
              }
            }}
            className="rounded-md p-1.5 hover:bg-card2 hover:text-muted"
            title={tc("composer.slashTitle")}
            aria-label={tc("composer.slashAria")}
          >
            <Keyboard className="h-4 w-4" />
          </button>
          {!isHome && (
            <ConnStatusChip
              wsStatus={wsStatus}
              onReconnect={onReconnect}
            />
          )}
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-1.5">
        {/* 首页挂载 chip(home-mount-picker-design.md §2.1)：操作的是「首条
            消息将生效」的 pending 列表；会话内由 TopBar 的 live chip 负责。 */}
        {!session && (
          <ContextFoldersChip
            ids={pendingMounts.ids}
            primary={pendingMounts.primary}
            onChange={(ids, primary) => setPendingMounts({ ids, primary })}
          />
        )}
        {running && goalActive && session?.type !== "delegation" && (
          <button
            onClick={() => void g.setGoalStatus(session!.id, "paused")}
            title={tc("goal.pauseTitle")}
            aria-label={tc("goal.pauseAria")}
            className="flex h-8 w-8 items-center justify-center rounded-lg border border-line2 text-muted hover:text-txt"
          >
            <Square className="h-3.5 w-3.5" />
          </button>
        )}
        {/* model chip: per-session provider/model switch (home: pick for
            the session that first send will create). Enabled while a
            turn runs — the switch is in-place server-side and takes
            effect at the next LLM call (model-assignment-design.md
            §3.2). */}
        <ModelChipMenu
          session={session}
          label={modelChipLabel}
          homeModelSource={homeModelSource}
          targetAgent={targetAgent}
          homeEffective={homeEffective}
          enabledProviders={enabledProviders}
          open={modelOpen}
          onToggle={() => setModelOpen((v) => !v)}
          onClose={() => setModelOpen(false)}
          onPick={(pid, m) => void pickModel(pid, m)}
        />
        {running && session && !parked ? (
          <button
            onClick={stopTurn}
            title={tc("composer.stopTitle")}
            aria-label={tc("composer.stopAria")}
            className="flex h-8 w-8 items-center justify-center rounded-lg bg-red text-white transition-opacity hover:opacity-90"
          >
            <Square className="h-3.5 w-3.5" fill="currentColor" />
          </button>
        ) : (
        <button
          onClick={send}
          disabled={
            parked ||
            running ||
            // In-session uploads run immediately and gate the send; on
            // home they're deferred until lazy creation, so they must
            // not disable the button (same deadlock as send()'s guard).
            (!!session && fileAttachments.some((f) => f.uploading)) ||
            (!input.trim() && attachments.length === 0 && fileAttachments.length === 0)
          }
          className="flex h-8 w-8 items-center justify-center rounded-lg bg-violet text-white transition-opacity hover:opacity-90 disabled:opacity-40"
        >
          <ArrowUp className="h-4 w-4" />
        </button>
        )}
        </div>
      </div>
    </div>
  );
}
