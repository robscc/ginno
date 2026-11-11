"use client";

import React, { useEffect, useState } from "react";

import { RotateCw, Link2, FileText, Workflow } from "lucide-react";
import { FileTypeIcon } from "./blocks/FileTypeIcon";
import { useTranslations } from "next-intl";
import { Markdown } from "./Markdown";
import { cn } from "@/lib/utils";
import { fmtTokens } from "@/lib/format";
import { AskUserCard } from "./AskUserCard";
import { useGinno } from "@/lib/store";
import { ensureFileRegistered } from "@/lib/api/workspace";
import { toolsWithUi, type McpToolUiInfo } from "@/lib/runtime";
import { openLinkExternal } from "@/lib/api/knowledge";
import { useChatT } from "./blocks/i18n";
import { McpAppCard } from "./McpAppCard";
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
import { FileChips, SkillChips } from "./blocks/chips";
import { WidgetBlock, WorkflowBlock } from "./blocks/widgets";
import { ToolBlock, ThinkingBlock } from "./blocks/toolBlocks";
import { TaskEventBand } from "./TaskCard";
import { imageUrl, ImageGallery } from "./blocks/media";
import { ContextBlocks } from "./blocks/sources";
import { fmtClock } from "@/lib/devswap/stats";

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
export { foldTaskEventBlocks } from "./blocks/taskEventParse";
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
export { imageUrl, ImageGallery } from "./blocks/media";
import { Lightbox } from "./blocks/media";

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
      {refKind === "workflow" ? (
        <Workflow className="h-3.5 w-3.5 text-violet" />
      ) : refKind === "link" ? (
        <Link2 className="h-3.5 w-3.5 text-violet" />
      ) : (
        <FileTypeIcon name={name} className="h-3.5 w-3.5 shrink-0" />
      )}
      {refKind === "workflow" ? tc("refChip.workflowPrefix") : ""}
      {name}
    </button>
  );
}

/** turn 级耗时+token 小字（turn 右下角）。live 版带 t0Ms → 每秒自跳；
 * 定格版（message.end 权威值 / history 投影）只有 elapsedS。 */
export function TurnMetaLine({ block }: { block: Extract<Block, { kind: "turn_meta" }> }) {
  const tm = useTranslations("chat.turnMeta");
  const [, tick] = React.useState(0);
  const live = typeof block.t0Ms === "number";
  React.useEffect(() => {
    if (!live) return;
    const h = setInterval(() => tick((n: number) => n + 1), 1000);
    return () => clearInterval(h);
  }, [live]);
  const elapsed = live
    ? Math.max(0, Math.round((Date.now() - (block.t0Ms ?? Date.now())) / 1000))
    : block.elapsedS;
  const hasTok = typeof block.inputTokens === "number" || typeof block.outputTokens === "number";
  // 2026-11-11 方案 A 分色胶囊：⏱ 中性 / ↑ 输入蓝 / ↓ 输出绿（绿与工具
  // 块成功色呼应）；胶囊分组不占额外高度；hover tooltip 展开精确值。
  const chip =
    "rounded-md bg-card2/60 px-1.5 py-px transition-colors hover:bg-card2 cursor-pointer";
  const tips: string[] = [];
  if (typeof block.inputTokens === "number")
    tips.push(`输入 ${block.inputTokens.toLocaleString()}`);
  if (typeof block.outputTokens === "number")
    tips.push(`输出 ${block.outputTokens.toLocaleString()}`);
  if (typeof elapsed === "number") tips.push(`耗时 ${elapsed}s`);
  const [open, setOpen] = React.useState(false);
  const tps =
    typeof block.outputTokens === "number" && elapsed
      ? (block.outputTokens / Math.max(1, elapsed)).toFixed(1)
      : null;
  return (
    <div
      className="select-none pt-0.5 font-mono text-[11px] leading-4"
      title={open ? undefined : tips.join(" · ")}
    >
    <div className="flex justify-end gap-1">
      {typeof elapsed === "number" && (
        <span
          className={`${chip} text-muted/90`}
          onClick={() => setOpen((v) => !v)}
        >
          {elapsed}s
        </span>
      )}
      {typeof block.inputTokens === "number" && (
        <span
          className={`${chip} text-sky-600 dark:text-sky-400`}
          onClick={() => setOpen((v) => !v)}
        >
          <span className="font-bold">↑</span>
          {fmtTokens(block.inputTokens)}
        </span>
      )}
      {typeof block.outputTokens === "number" && (
        <span
          className={`${chip} text-emerald-600 dark:text-emerald-400`}
          onClick={() => setOpen((v) => !v)}
        >
          <span className="font-bold">↓</span>
          {fmtTokens(block.outputTokens)}
        </span>
      )}
    </div>
      {open && (
        // 点击胶囊展开明细（2026-11-11）：精确千分位 + 吞吐率；再次点击收起。
        <div className="mt-1 flex justify-end gap-2 rounded-md border border-line bg-card2/40 px-2 py-1 text-[10px] text-muted">
          <span>{tm("detailIn")} {block.inputTokens?.toLocaleString() ?? "—"}</span>
          <span className="text-sky-600 dark:text-sky-400">
            {`${tm("detailOut")} ${block.outputTokens?.toLocaleString() ?? "—"}`}
          </span>
          {typeof elapsed === "number" && <span>{tm("detailElapsed")} {elapsed}s</span>}
          {tps && <span>{tps} tok/s</span>}
        </div>
      )}
    </div>
  );
}

/** dev-swap run 终态结果行（§4.4）：与 turn_meta 同款角落 chrome 语言。
 * 失败/回滚带失败阶段与可展开的尾日志（与 DevSwapCard 日志同源）。 */
export function DevSwapResultLine({ block }: { block: Extract<Block, { kind: "devswap_result" }> }) {
  const [open, setOpen] = React.useState(false);
  // devswap 域动态 key（state 枚举）收敛转型，理由同 streamCards 的 tds。
  const t = useTranslations("devswap") as unknown as (
    key: string,
    values?: Record<string, string | number>,
  ) => string;
  const ok = block.state === "ok";
  const elapsed =
    typeof block.elapsedS === "number" ? fmtClock(block.elapsedS) : null;
  // runId 是完整目录名 ginno-runtime.v{sha}.{ts}——展示收敛到指纹段。
  const fp = block.runId.startsWith("ginno-runtime.")
    ? (block.runId.split(".")[1] ?? block.runId)
    : block.runId;
  const stageLabel = block.failedStage
    ? t(`card.stage.${block.failedStage}`)
    : "";
  const label = ok
    ? t("result.ok", { fp, elapsed: elapsed ?? "" })
    : t(block.state === "rolled_back" ? "result.rolledBack" : "result.fail", {
        stage: stageLabel,
        elapsed: elapsed ?? "",
      });
  const logLines = (block.logTail ?? "").split("\n").filter(Boolean);
  return (
    <div className="mt-1 flex select-none flex-col items-end gap-1">
      <div
        className={`flex items-center gap-1.5 font-mono text-[11px] leading-4 ${
          ok
            ? "text-emerald-600 dark:text-emerald-400"
            : "text-red-600 dark:text-red-400"
        }`}
        data-testid="devswap-result"
      >
        <span>{ok ? "✔" : "✖"}</span>
        <span>{label}</span>
        {!ok && !!logLines.length && (
          <button
            onClick={() => setOpen((o) => !o)}
            className="text-muted underline decoration-line2 underline-offset-2 hover:text-txt"
          >
            {open ? t("result.hideLogs") : t("result.logs", { n: Math.min(logLines.length, 30) })}
          </button>
        )}
      </div>
      {open && !!logLines.length && (
        <pre className="max-h-40 w-full overflow-auto rounded-md border border-line bg-base/60 p-2 text-left font-mono text-[10px] leading-snug text-muted">
          {logLines.slice(-30).join("\n")}
        </pre>
      )}
    </div>
  );
}

/** MCP Apps 清单（docs/mcp-apps-design.md §2）：挂载拉一次，store 单飞去重
 * （N 张卡的会话也只发一次请求）；resolved 后 setState 补渲染。MCP 连接
 * 变化靠 staleMs 过期后的下一次挂载/刷新收口，P0 不做推送。 */
function useMcpToolsWithUi(): Record<string, McpToolUiInfo> {
  const [map, setMap] = useState<Record<string, McpToolUiInfo>>(() => toolsWithUi.peek() ?? {});
  useEffect(() => {
    let alive = true;
    toolsWithUi
      .get()
      .then((m) => {
        if (alive) setMap(m);
      })
      .catch(() => {
        /* 拉不到清单就不渲染 app 卡（普通气泡照常） */
      });
    return () => {
      alive = false;
    };
  }, []);
  return map;
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
  const mcpUiMap = useMcpToolsWithUi();
  // Resolve `web|sN` citation ids against this bubble's web_search results.
  const refMap = webRefMap(blocks);
  // turn_meta（耗时+token 小字）是「卡片角落 chrome」而非内容：live 期插入
  // 位置不定（ensureLive 建%、turn.start 补插在首、usage 追加在尾、history
  // 投影在尾），若按数组位置内联渲染会漂到内容上方/中间——统一提取到所有
  // 内容之后渲染，「turn 右下角」语义恒成立。
  const tmBlock = blocks.find(
    (b): b is Extract<Block, { kind: "turn_meta" }> => b.kind === "turn_meta",
  );
  // devswap_result 同为角落 chrome：循环里不占位，统一提到内容之后渲染。
  const dsResult = blocks.find(
    (b): b is Extract<Block, { kind: "devswap_result" }> => b.kind === "devswap_result",
  );
  // 流式光标/尾部判定的「最后一个内容块」：turn_meta 不占位（它在数组尾时，
  // 不能让真正的末尾 text 块失去光标与 partial-citation 掩码）。
  let contentEnd = blocks.length - 1;
  while (contentEnd >= 0 && blocks[contentEnd].kind === "turn_meta") contentEnd--;
  while (i < blocks.length) {
    const b = blocks[i];
    if (b.kind === "turn_meta") {
      i++;
      continue;
    }
    const last = i === contentEnd;
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
      // MCP Apps：工具命中 ui 清单时，普通气泡上方附加交互 app 卡（保守
      // 附加而非替换——无 UI/清单未达/拉取失败都回退现有渲染，见
      // docs/mcp-apps-design.md §2 呈现容器 P0）。
      const mcpUi = mcpUiMap[b.name];
      if (mcpUi && !b.pending) {
        out.push(
          <McpAppCard key={`mcpapp-${key++}`} server={mcpUi.server} tool={mcpUi.tool} data={b.content} />,
        );
      }
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
    } else if (b.kind === "devswap_result") {
      // 角落 chrome：不占流推进位（同 turn_meta），由 DevSwapResultLine 统一渲染。
      i++;
      continue;
    }
    // refs rendered outside
    i++;
  }
  return (
    <>
      {out}
      {tmBlock && <TurnMetaLine key="tm" block={tmBlock} />}
      {dsResult && <DevSwapResultLine key="dsr" block={dsResult} />}
    </>
  );
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
  // 注入的图片/附件可点击（2026-11-04 用户反馈）：图片走 Lightbox（与普通
  // 消息图集同一查看器），文件 chip 走 openInCode 打开代码面板（同 chips.tsx
  // 既有惯例——session 根相对路径）。
  const [lb, setLb] = React.useState<number | null>(null);
  const g = useGinno();
  const allImgUrls = (block.images ?? []).map(steerImgUrl).filter(Boolean);
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
        <MentionText text={block.text} />
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
                onClick={(e) => {
                  e.stopPropagation();
                  setLb(allImgUrls.indexOf(url));
                }}
                className="h-4 w-4 shrink-0 cursor-zoom-in rounded border border-orange/40 object-cover transition-transform hover:scale-125"
              />
            ) : (
              <span key={`i${i}`} title={img.name} className="text-[11px] leading-none">
                🖼️
              </span>
            );
          })}
          {shownFiles.map((f, i) => (
            <button
              type="button"
              key={`f${i}`}
              title={f.path ?? f.name}
              onClick={(e) => {
                e.stopPropagation();
                const p = f.path;
                if (p) g.openInCode({ rootId: g.codeRootId ?? "session", path: p });
              }}
              className={cn(
                "flex max-w-[96px] cursor-pointer items-center gap-0.5 rounded bg-orange/10 px-1 py-px",
                "text-[10px] leading-none text-orange hover:bg-orange/25",
              )}
            >
              <span aria-hidden>
                <FileTypeIcon name={f.name} kind={f.kind} className="h-3 w-3" />
              </span>
              <span className="truncate">{f.name}</span>
            </button>
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
      {lb !== null && allImgUrls.length > 0 && (
        <Lightbox
          urls={allImgUrls}
          index={lb}
          onClose={() => setLb(null)}
          onNav={(i: number) => setLb(i)}
        />
      )}
    </div>
  );
}
