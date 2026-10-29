"use client";

import { RotateCw, Link2, FileText, Workflow } from "lucide-react";
import { Markdown } from "./Markdown";
import { cn } from "@/lib/utils";
import { AskUserCard } from "./AskUserCard";
import { useGinno } from "@/lib/store";
import { ensureFileRegistered } from "@/lib/api/workspace";
import { openLinkExternal } from "@/lib/api/knowledge";
import { useChatT } from "./blocks/i18n";
import type { Block, SteerBandFile, SteerBandImage } from "./blocks/types";
import { MentionText } from "./MentionText";
import {
  SubagentSpawnCard,
  SubagentResultCard,
  SubagentBriefCard,
  PlanUpdateCard,
  WorkerResultCard,
  WorkerAssignCard,
} from "./blocks/subagentCards";
import {
  parseSources,
  stripSources,
  webRefMap,
  resolveSourceRefs,
  maskPartialSources,
  SourcesBlock,
} from "./blocks/sources";
import { FileChips, SkillChips, TABLE_KINDS } from "./blocks/chips";
import { WidgetBlock, WorkflowBlock } from "./blocks/widgets";
import { ToolBlock, ThinkingBlock } from "./blocks/toolBlocks";
import { TaskEventBand } from "./TaskCard";
import { imageUrl, ImageGallery } from "./blocks/media";
import { ContextBlocks } from "./blocks/sources";

// ---- 原有导出整体保留：各内聚主题拆到 blocks/ 子模块，这里统一 re-export，
// 保证所有现有 import 路径（@/components/chat/blocks）不变。 ----
export { useEventI18nText } from "./blocks/i18n";
export {
  SUBAGENT_BUCKET_ORDER,
  SUBAGENT_STATUS_META,
  subagentSessionBucket,
  subagentStatusBucket,
  subagentStatusMeta,
  useStatusLabel,
} from "./blocks/types";
export { DuckStatusIcon } from "./blocks/duckStatus";
export type {
  SourceItem,
  Block,
  SubagentCardBlock,
  SteerBandImage,
  SteerBandFile,
  QuestionBlock,
  QuestionSub,
} from "./blocks/types";
export {
  parseSubagentResults,
  parseSubagentResult,
  parseSubagentBrief,
  foldSubagentResultBlocks,
  parseWorkerResults,
  parseWorkerAssign,
} from "./blocks/subagentParse";
export {
  SubagentKindBadges,
  SubagentSpawnCard,
  SubagentResultCard,
  SubagentBlocks,
  SubagentBriefCard,
  WorkerResultCard,
  WorkerAssignCard,
} from "./blocks/subagentCards";
export {
  parseSources,
  stripSources,
  webRefMap,
  resolveSourceRefs,
  maskPartialSources,
  ContextBlocks,
  normWikiRef,
  matchWikiPage,
  SourcesBlock,
} from "./blocks/sources";
export { FileChips, SkillChips } from "./blocks/chips";
export { WidgetBlock, WorkflowBlock } from "./blocks/widgets";
export { ToolBlock, ThinkingBlock } from "./blocks/toolBlocks";
export { imageUrl, Lightbox, ImageGallery } from "./blocks/media";

// Strip "[attached <kind>: <name>]" patterns that the LLM sometimes repeats in its text
// (violating the "don't repeat tool results" instruction). These are shown as ref chips instead.
const ATTACHED_REF_RE = /\[attached\s+\w+:\s*[^\]]+\]/g;

function cleanAgentText(text: string): string {
  return text.replace(ATTACHED_REF_RE, "").replace(/\n{3,}/g, "\n\n").trim();
}

/** 附件型 ref（file/doc）：点击时把 ref_id 登记进文件注册表（老会话的历史
 *  ref 从未注册过；live 转向在 ref.emit 时已注册，这里幂等命中）再走和
 *  FileChips 一致的预览。link 型 ref 直接交系统浏览器；无 ref_id 的维持纯展示。 */
function RefChip({ refKind, name, refId }: { refKind: string; name: string; refId?: string }) {
  const g = useGinno();
  const { tc } = useChatT();
  const Ic = refKind === "workflow" ? Workflow : refKind === "link" ? Link2 : FileText;
  const isUrl = /^https?:\/\//i.test(refId ?? "");
  const openable =
    !!refId && (refKind === "link" ? isUrl : refKind === "file" || refKind === "doc");
  const onOpen = async () => {
    if (!refId) return;
    if (refKind === "link") {
      openLinkExternal(refId);
      return;
    }
    const sid = g.activeSessionId;
    if (!sid) return;
    const r = await ensureFileRegistered(sid, refId, name);
    if (r.ok && r.file) {
      g.openPreview({
        id: r.file.id,
        name: r.file.name ?? name,
        path: r.file.path ?? refId,
        kind: r.file.kind,
      });
    }
  };
  return (
    <button
      type="button"
      disabled={!openable}
      onClick={() => void onOpen()}
      title={openable ? `${tc("refChip.clickOpen")}\n${refId}` : undefined}
      className={`inline-flex items-center gap-1.5 rounded-lg border border-line bg-card px-2.5 py-1 text-xs text-muted ${
        openable ? "cursor-pointer hover:border-violet/50 hover:text-txt" : "cursor-default"
      }`}
    >
      <Ic className="h-3.5 w-3.5 text-violet" />
      {refKind === "workflow" ? tc("refChip.workflowPrefix") : ""}
      {name}
    </button>
  );
}

/** Blocks rendered INSIDE the assistant card (everything except refs).
 * onAnswerQuestion/questionLive thread ChatStream's ask_user resume channel
 * down to a pending question card; without them the card renders read-only. */
export function InnerBlocks({
  blocks,
  streaming,
  onAnswerQuestion,
  questionLive,
}: {
  blocks: Block[];
  streaming?: boolean;
  onAnswerQuestion?: (id: string, answer: string, optionIndex: number | null, skip: boolean) => void;
  questionLive?: boolean;
}) {
  const out: React.ReactNode[] = [];
  let i = 0;
  let key = 0;
  // Resolve `web|sN` citation ids against this bubble's web_search results.
  const refMap = webRefMap(blocks);
  while (i < blocks.length) {
    const b = blocks[i];
    const last = i === blocks.length - 1;
    if (b.kind === "image") {
      // Group consecutive images into one gallery.
      const urls: string[] = [];
      while (i < blocks.length && blocks[i].kind === "image") {
        urls.push(imageUrl(blocks[i] as Extract<Block, { kind: "image" }>));
        i++;
      }
      out.push(<ImageGallery key={key++} urls={urls} />);
      continue;
    }
    if (b.kind === "text") {
      // Citation framework: fold a trailing <ginno_citations> block into a
      // SourcesBlock; while streaming, mask the in-flight (unclosed) block.
      const cited = parseSources(b.text);
      // Strip UNCONDITIONALLY: an empty block (or one whose entries all fail
      // validation) parses to zero items but is still machine metadata — with
      // a conditional strip the raw tags leak into the bubble
      // (2026-08-21: turn b1463216 emitted an empty block).
      let text = stripSources(b.text);
      if (streaming && last && !cited.length) text = maskPartialSources(text);
      out.push(
        <div key={key++}>
          <Markdown text={cleanAgentText(text)} streaming={!!(streaming && last)} />
          {streaming && last && (
            <span className="ml-0.5 inline-block h-3.5 w-1.5 translate-y-0.5 animate-pulse bg-violet" />
          )}
          {cited.length > 0 && <SourcesBlock items={resolveSourceRefs(cited, refMap)} />}
        </div>,
      );
    } else if (b.kind === "sources") {
      out.push(<SourcesBlock key={key++} items={resolveSourceRefs(b.items, refMap)} />);
    } else if (b.kind === "widget") {
      out.push(
        <WidgetBlock key={b.renderId || `w${key++}`} kind={b.widgetKind} data={b.data} />,
      );
    } else if (b.kind === "workflow") {
      out.push(<WorkflowBlock key={key++} run={b.run} />);
    } else if (b.kind === "tool") {
      out.push(<ToolBlock key={key++} name={b.name} content={b.content} pending={b.pending} argsPreview={b.argsPreview} />);
    } else if (b.kind === "question") {
      out.push(
        <AskUserCard
          key={b.id || `q${key++}`}
          block={b}
          live={questionLive}
          onAnswer={onAnswerQuestion}
        />,
      );
    } else if (b.kind === "thinking") {
      out.push(<ThinkingBlock key={key++} text={b.text} live={!!streaming && last} />);
    } else if (b.kind === "file") {
      out.push(<FileChips key={key++} files={[b]} />);
    } else if (b.kind === "steer") {
      out.push(<SteerBand key={b.steerId || `st${key++}`} block={b} />);
    } else if (b.kind === "task_event") {
      // 后台任务事件注入点（评审决策 4）：极薄标记行，完整状态在 bg_run
      // 的常驻 TaskCard；此处只为时间线留下「结果何时回来」的锚。
      out.push(<TaskEventBand key={`tev-${b.taskId}-${key++}`} block={b} />);
    } else if (b.kind === "subagent_spawn") {
      out.push(<SubagentSpawnCard key={`sa-spawn-${b.sessionId}-${key++}`} block={b} />);
    } else if (b.kind === "subagent_result") {
      out.push(<SubagentResultCard key={`sa-result-${b.sessionId}-${key++}`} block={b} />);
    } else if (b.kind === "plan_update") {
      out.push(<PlanUpdateCard key={`plan-upd-${b.planId}-${key++}`} block={b} />);
    } else if (b.kind === "subagent_brief") {
      out.push(<SubagentBriefCard key={`sa-brief-${key++}`} block={b} />);
    } else if (b.kind === "worker_result") {
      out.push(<WorkerResultCard key={`wr-${b.sessionId}-${key++}`} block={b} />);
    } else if (b.kind === "worker_assign") {
      out.push(<WorkerAssignCard key={`wa-${b.fromSessionId}-${key++}`} block={b} />);
    } else if (b.kind === "context") {
      // context 事件（如 microcompact 清理提示）live 时挂在**当前 assistant
      // 消息**上；这里不渲染就会让 hasInner 为真但卡片体为空——一条只有
      // context 的消息于是显示成一张空白卡片（2026-10-26 联同底部空白一起
      // 排查）。历史重放会把它折成 system 行，两条路径现在都看得见。
      out.push(<ContextBlocks key={`ctx-${key++}`} blocks={[b]} />);
    }
    // refs rendered outside
    i++;
  }
  return <>{out}</>;
}

/** User bubble content: attached images as a gallery, text kept verbatim. */
export function UserBlocks({ blocks }: { blocks: Block[] }) {
  const files = blocks.filter((b): b is Extract<Block, { kind: "file" }> => b.kind === "file");
  const skills = blocks.filter((b): b is Extract<Block, { kind: "skill" }> => b.kind === "skill");
  const imgs = blocks
    .filter((b): b is Extract<Block, { kind: "image" }> => b.kind === "image")
    .map(imageUrl);
  const texts = blocks
    .filter((b): b is Extract<Block, { kind: "text" }> => b.kind === "text")
    .map((b) => b.text);
  // The user's own request rides the skill block on slash-skill turns.
  const skillTexts = skills.map((s) => s.text ?? "").filter(Boolean);
  const allTexts = [...texts, ...skillTexts];
  return (
    <>
      <FileChips files={files} />
      <SkillChips skills={skills} />
      {imgs.length > 0 && <ImageGallery urls={imgs} />}
      {allTexts.map((t, i) => (
        // break-words: pasted terminal output carries long unbroken paths —
        // without it the transcript overflows the page into horizontal scroll
        <div key={i} className="selectable min-w-0 whitespace-pre-wrap break-words">
          <MentionText text={t} />
        </div>
      ))}
    </>
  );
}

/** Ref chips rendered BELOW the card, matching the mock layout. */
export function RefBlocks({ blocks }: { blocks: Block[] }) {
  const refs = blocks.filter((b): b is Extract<Block, { kind: "ref" }> => b.kind === "ref");
  if (!refs.length) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {refs.map((r, i) => (
        <RefChip key={i} refKind={r.refKind} name={r.name} refId={r.refId} />
      ))}
    </div>
  );
}

export function hasPendingTool(blocks: Block[]): boolean {
  // A pending question card counts too: it REPLACED the ask_user tool bubble,
  // so without this the working indicator would die the moment the card lands.
  return blocks.some(
    (b) => (b.kind === "tool" && b.pending) || (b.kind === "question" && b.status === "pending"),
  );
}

/** "09:41" from the server's epoch-seconds stamp ("" when absent). */
function steerClock(at?: number): string {
  if (!at) return "";
  const d = new Date(at * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** Resolve a band image to a displayable URL (direct `url`, or a data: URL
 *  rebuilt from a base64 summary). "" when the summary carries no payload. */
function steerImgUrl(img: SteerBandImage): string {
  if (img.url) return img.url;
  if (img.data) return `data:${img.mediaType || img.media_type || "image/png"};base64,${img.data}`;
  return "";
}

/** A mid-turn steered message (docs/steering-design.md §4.2).
 *
 * Full-width band, not a right-aligned bubble: the user typed this INTO a
 * running turn, so it reads as the injection point — sitting between the tool
 * step that was running and the model's continuation — rather than as a turn
 * of its own. Used both inside the assistant bubble (history replay and live
 * absorption) and, on its own, in the "queue bar" above the composer.
 */
export function SteerBand({ block }: { block: Extract<Block, { kind: "steer" }> }) {
  const clock = steerClock(block.injectedAt);
  const { tc } = useChatT();
  // Deliberately NOT the thinking block's shape or colour: that block is violet
  // with a header ROW of its own (blocks.tsx ThinkingBlock), and an early
  // version of this band copied both — it read as a second thinking panel and
  // as two lines of chrome for one line of text. Here the label is an inline
  // chip on the same row as the words, and the accent is orange: unused
  // elsewhere in the chat chrome, so it collides with nothing (violet =
  // thinking/agent, blue = web citations, green = success, yellow = needs your
  // attention, red = stop/error).
  // Attachment chips are capped at MAX_VISIBLE with a "+N" tail: the band is a
  // narrow single-line layout (design §4.2), so a long attachment list must
  // truncate gracefully rather than blow the row out.
  const imgs = block.images ?? [];
  const files = block.files ?? [];
  const total = imgs.length + files.length;
  const MAX_VISIBLE = 3;
  const shownImgs = imgs.slice(0, MAX_VISIBLE);
  const shownFiles = files.slice(0, Math.max(0, MAX_VISIBLE - shownImgs.length));
  const overflow = total - shownImgs.length - shownFiles.length;
  return (
    <div className="my-1 flex items-start gap-2 rounded-r-md border-l-2 border-orange/70 bg-orange/[0.07] py-1 pl-2 pr-2">
      <span className="mt-[3px] flex shrink-0 items-center gap-1 rounded bg-orange/15 px-1 py-[1px] text-[10px] font-medium leading-none text-orange">
        <RotateCw className="h-2.5 w-2.5" aria-hidden />
        {tc("steer.injected")}{clock ? ` · ${clock}` : ""}
      </span>
      <div className="selectable min-w-0 whitespace-pre-wrap break-words text-[13px] leading-snug text-txt">
        {block.text}
      </div>
      {total > 0 && (
        <span className="mt-[2px] flex shrink-0 items-center gap-1">
          {shownImgs.map((img, i) => {
            const url = steerImgUrl(img);
            return url ? (
              <img
                key={`i${i}`}
                src={url}
                alt={img.name ?? tc("steer.imageAlt")}
                title={img.name}
                className="h-4 w-4 shrink-0 rounded border border-orange/40 object-cover"
              />
            ) : (
              <span key={`i${i}`} title={img.name} className="text-[11px] leading-none">
                🖼️
              </span>
            );
          })}
          {shownFiles.map((f, i) => (
            <span
              key={`f${i}`}
              title={f.path ?? f.name}
              className={cn(
                "flex max-w-[96px] items-center gap-0.5 rounded bg-orange/10 px-1 py-px",
                "text-[10px] leading-none text-orange",
              )}
            >
              <span aria-hidden>{TABLE_KINDS.has(f.kind ?? "") ? "📊" : "📄"}</span>
              <span className="truncate">{f.name}</span>
            </span>
          ))}
          {overflow > 0 && (
            <span
              title={tc("steer.moreAttachments", { count: overflow })}
              className="shrink-0 rounded bg-orange/15 px-1 py-px text-[10px] leading-none text-orange"
            >
              +{overflow}
            </span>
          )}
        </span>
      )}
    </div>
  );
}
