"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ChevronRight } from "lucide-react";
import type { SessionMeta } from "@/lib/types";
import { useGinno } from "@/lib/store";
import { isSubagentConfirmed, markSubagentConfirmed } from "@/lib/subagentConfirm";
import { t } from "@/i18n/provider";
import { useChatT } from "./i18n";
import { parseDelegation } from "./delegation";
import {
  SUBAGENT_BUCKET_ORDER,
  subagentStatusBucket,
  subagentStatusMeta,
  useStatusLabel,
  type Block,
  type SubagentCardBlock,
} from "./types";
import { DuckStatusIcon } from "./duckStatus";

/** 子代理的 fork / 类型小徽标（P3 范围 3）：meta.subagent.mode === "fork" 时
 *  显示 fork 徽标；agent_type 仅在 runtime 侧确实把类型名记进了 meta 时渲染
 *  （防御性读取，不造字段——没记就什么都不显示）。发起卡片、结果卡片与侧栏
 *  子会话行共用同一份渲染。 */
export function SubagentKindBadges({
  sub,
}: { sub?: import("@/lib/types").SubagentMeta | null }) {
  const { tc } = useChatT();
  if (!sub) return null;
  const agentType = (sub as { agent_type?: unknown }).agent_type;
  return (
    <>
      {sub.mode === "fork" && (
        <span
          className="shrink-0 rounded-md border border-violet/40 bg-violet/10 px-1 py-px text-[10px] leading-4 text-violet"
          title={tc("subagent.forkTitle")}
        >
          fork
        </span>
      )}
      {typeof agentType === "string" && agentType && (
        <span
          className="shrink-0 rounded-md border border-line2 bg-card2/60 px-1 py-px text-[10px] leading-4 text-muted"
          title={tc("subagent.typeTitle", { type: agentType })}
        >
          {agentType}
        </span>
      )}
    </>
  );
}

// ---- subagent 卡片（subagent-design.md §6.3）--------------------------------
// 主对话里的发起卡片 / 结果卡片。状态永远从 store 的会话元数据实时读——
// subagent.status 事件刷新 store，卡片随之换挡，无需自己的事件订阅。

function subagentElapsed(from?: number): string {
  if (!from) return "";
  const s = Math.max(0, Math.round(Date.now() / 1000 - from));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** 发起卡片：goal / 约束 / 验收 / 状态脉搏 / 「查看对话」入口。 */
export function SubagentSpawnCard({
  block,
}: {
  block: Extract<Block, { kind: "subagent_spawn" }>;
}) {
  const g = useGinno();
  const router = useRouter();
  const { tc } = useChatT();
  const statusLabel = useStatusLabel();
  const live = g.sessions.find((s) => s.id === block.sessionId);
  const sub = live?.subagent;
  const status = sub?.status ?? "running";
  const meta = subagentStatusMeta(status);
  const active = status === "running" || status === "waiting";
  const openChild = () => {
    // 与通知点击同一条路径：切会话 + 落到最新消息。
    g.setActiveSession(block.sessionId);
    if (window.location.pathname !== "/") router.push("/");
    window.dispatchEvent(new CustomEvent("ginno:focus-latest", { detail: block.sessionId }));
  };
  return (
    <div className="rounded-lg border border-line bg-base/50 px-3 py-2.5 text-xs">
      <div className="flex items-center gap-1.5">
        <span className="shrink-0">🤖</span>
        <span className="min-w-0 flex-1 truncate font-medium text-txt" title={sub?.goal || block.goal}>
          {block.title || sub?.goal || block.goal || tc("subagent.fallbackTitle")}
        </span>
        <SubagentKindBadges sub={sub} />
        {active && <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full" style={{ background: meta.color }} />}
        <span className="shrink-0" title={tc("status.tooltip", { status: statusLabel(status) })}>
          {meta.glyph} {statusLabel(status)}
        </span>
        <button
          onClick={openChild}
          className="shrink-0 rounded-md border border-line2 px-1.5 py-0.5 text-[10px] text-muted transition-colors hover:border-violet/50 hover:text-violet"
        >
          {tc("subagent.viewConversation")}
        </button>
      </div>
      {(sub?.goal || block.goal) && (
        <div className="mt-1.5 whitespace-pre-wrap break-words leading-relaxed text-muted">
          {tc("subagent.goalLabel")}{sub?.goal || block.goal}
        </div>
      )}
      {(sub?.constraints || block.constraints) && (
        <div className="mt-1 whitespace-pre-wrap break-words leading-relaxed text-faint">
          {tc("subagent.constraintsLabel")}{sub?.constraints || block.constraints}
        </div>
      )}
      {(sub?.acceptance || block.acceptance) && (
        <div className="mt-1 whitespace-pre-wrap break-words leading-relaxed text-faint">
          {tc("subagent.acceptanceLabel")}{sub?.acceptance || block.acceptance}
        </div>
      )}
      <div className="mt-1.5 flex items-center gap-2 text-[10px] text-faint">
        <span>{block.origin === "user" ? tc("subagent.startedByUser") : tc("subagent.startedByAgent")}</span>
        {typeof block.depth === "number" && <span>{tc("subagent.depth", { n: block.depth + 1 })}</span>}
        {block.spawnedAt && <span>{tc("subagent.runningFor", { time: subagentElapsed(block.spawnedAt) })}</span>}
      </div>
    </div>
  );
}

/** 结果卡片：摘要 + 状态 + 验收区 + 确认/纠偏操作（P2）。
 *
 * 确认 = 卡片定稿归档（本地态，localStorage 记忆，见 lib/subagentConfirm）；
 * 有问题 = 两个出口：去子会话纠偏（steering，设计 §5.4）或让主对话处理
 * （invoke/steer 一条引用该结果的消息，主 agent 决定重拆/追问/接手）。
 * 验收判定不在这里解析——runtime 会在注入消息里引导主 agent 逐条对照
 * acceptance 给出一行判定（P2 共享契约 5），卡片只展示 acceptance 原文。 */
export function SubagentResultCard({
  block,
}: {
  block: Extract<Block, { kind: "subagent_result" }>;
}) {
  const g = useGinno();
  const router = useRouter();
  const { tc } = useChatT();
  const statusLabel = useStatusLabel();
  const live = g.sessions.find((s) => s.id === block.sessionId);
  const sub = live?.subagent;
  // delegation 回执（external_agent 注入）复用本卡。判定双保险：summary 带
  // [delegate …] 机器头（注入/status 两通道的 summary 都以它开头），或子会话
  // meta type=delegation（老数据/极端时序兜底）。
  const summaryText = block.summary || sub?.result_summary || "";
  const del = parseDelegation(summaryText);
  const isDelegation = live?.type === "delegation" || !!del;
  // 终态优先级：机器头（本次运行真实 verdict）> meta.stop_reason。
  const stop = del?.stop ?? live?.stop_reason ?? "";
  const status = isDelegation
    ? stop === "error" || stop === "timeout"
      ? "failed"
      : "done"
    : sub?.status ?? "done";
  const meta = subagentStatusMeta(status);
  const goal = sub?.goal || block.goal || "Subtask";
  const [confirmed, setConfirmed] = useState(() => isSubagentConfirmed(block.sessionId));
  const [problemOpen, setProblemOpen] = useState(false);
  const openChild = () => {
    g.setActiveSession(block.sessionId);
    if (window.location.pathname !== "/") router.push("/");
    window.dispatchEvent(new CustomEvent("ginno:focus-latest", { detail: block.sessionId }));
  };
  const confirm = () => {
    markSubagentConfirmed(block.sessionId);
    setConfirmed(true);
  };
  // 出口一：跳进子会话，输入框预填纠偏 steering 提示（运行中走注入，idle 时
  // 是普通消息——预填的只是文本，发送语义由 ChatStream 的既有管线决定）。
  const goToChild = () => {
    openChild();
    window.dispatchEvent(
      new CustomEvent("ginno:prefill-input", {
        detail: {
          sessionId: block.sessionId,
          text: t(
            `[Course-correct] Regarding this subtask's goal "${goal}", its results have the following problems:`,
            `【纠偏】关于本子任务的目标「${goal}」，结果存在以下问题：`,
          ),
        },
      }),
    );
  };
  // 出口二：以一条引用该结果的消息回到当前（主）会话——ChatStream 侧按
  // 会话忙闲决定 invoke 还是 steer（ginno:subagent-escalate 监听）。
  const escalate = () => {
    window.dispatchEvent(
      new CustomEvent("ginno:subagent-escalate", {
        detail: {
          sessionId: block.sessionId,
          goal,
          summary: block.summary || sub?.result_summary || "",
        },
      }),
    );
    setProblemOpen(false);
  };
  return (
    <div className="rounded-lg border border-line bg-card/60 px-3 py-2.5 text-xs">
      <div className="flex items-center gap-1.5">
        <span className="shrink-0">{isDelegation ? "🧭" : "🤖"}</span>
        <span
          className="min-w-0 flex-1 truncate font-medium text-txt"
          title={isDelegation ? block.goal || live?.backend || "" : goal}
        >
          {isDelegation
            ? tc("subagent.delegationResultTitle", {
                backend: live?.backend ?? del?.backend ?? "?",
              })
            : tc("subagent.resultTitle", { goal })}
        </span>
        <SubagentKindBadges sub={sub} />
        <span className="shrink-0" title={tc("status.tooltip", { status: statusLabel(status) })}>
          {meta.glyph} {statusLabel(status)}
        </span>
      </div>
      {isDelegation && block.goal && (
        <div className="mt-0.5 truncate text-faint" title={block.goal}>
          {block.goal}
        </div>
      )}
      {/* delegation 徽标行：机器头的 kv 折成等宽小胶囊（与 ToolBlock 委托徽标
          同款配色）；turns/duration/tokens 只有终态回执携带。 */}
      {isDelegation && (
        <div className="mt-1.5 flex flex-wrap items-center gap-1 font-mono text-[10px] text-faint">
          <span className="rounded-full border border-violet/40 bg-violet/10 px-1.5 leading-4 text-violet">
            {live?.backend ?? del?.backend ?? "?"}
          </span>
          <span className="rounded-full border border-line px-1.5 leading-4 text-muted">
            {live?.mode ?? del?.mode ?? "?"}
          </span>
          <span
            className={`rounded-full border px-1.5 leading-4 ${
              stop === "success"
                ? "border-green/40 bg-green/10 text-green"
                : stop === "error" || stop === "timeout"
                  ? "border-red/40 bg-red/10 text-red"
                  : "border-line text-faint"
            }`}
          >
            {stop || "?"}
          </span>
          {del?.turns && <span>turns={del.turns}</span>}
          {del?.duration && <span>duration={del.duration}</span>}
          {del?.tokens && <span>tokens={del.tokens}</span>}
        </div>
      )}
      {/* delegation 正文：机器头与 delegation= 溯源行剥掉（信息已进徽标行与
          卡片跳转，ToolBlock 同款过滤）。 */}
      {summaryText && (
        <div className="mt-1.5 max-h-60 overflow-y-auto whitespace-pre-wrap break-words leading-relaxed text-muted">
          {isDelegation && del
            ? summaryText
                .split("\n")
                .filter((l, idx) => idx > 0 && !/^delegation=/.test(l))
                .join("\n")
                .trim()
            : summaryText}
        </div>
      )}
      {block.error && (
        <div className="mt-1.5 whitespace-pre-wrap break-words text-red/90">{tc("subagent.errorPrefix")}{block.error}</div>
      )}
      {/* 验收区（P2 共享契约 5）：acceptance 非空时展示原文；逐条判定由主 agent
          在汇报里给出（runtime 注入消息已引导），卡片不重复解析。 */}
      {sub?.acceptance && (
        <div className="mt-2 rounded-md border border-violet/25 bg-violet/[0.05] px-2 py-1.5">
          <div className="text-[10px] font-medium text-violet">{tc("subagent.acceptanceCriteria")}</div>
          <div className="mt-0.5 whitespace-pre-wrap break-words leading-relaxed text-muted">
            {sub.acceptance}
          </div>
          <div className="mt-1 text-[10px] text-faint">
            {tc("subagent.acceptanceHint")}
          </div>
        </div>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-1.5 border-t border-line/60 pt-1.5">
        <button
          onClick={openChild}
          className="rounded-md border border-line2 px-1.5 py-0.5 text-[10px] text-muted transition-colors hover:border-violet/50 hover:text-violet"
        >
          {tc("subagent.viewFullConversation")}
        </button>
        {/* delegation：外部 CLI 跑完即终态，回放会话只读——确认/纠偏/升级
            这套子代理结果评审流程对它没有意义，整个不渲染（2026-10-05
            用户反馈），只留「查看完整对话」。 */}
        {!isDelegation && confirmed && (
          <span
            className="rounded-md border border-green/40 bg-green/10 px-1.5 py-0.5 text-[10px] text-green"
            title={tc("subagent.confirmedTitle")}
          >
            ✅ {tc("subagent.confirmed")}
          </span>
        )}
        {!isDelegation && !confirmed && (
          <>
            <button
              onClick={confirm}
              title={tc("subagent.confirmTitle")}
              className="rounded-md border border-line2 px-1.5 py-0.5 text-[10px] text-muted transition-colors hover:border-green/50 hover:text-green"
            >
              ✅ {tc("subagent.confirm")}
            </button>
            <button
              onClick={() => setProblemOpen((v) => !v)}
              className={`rounded-md border px-1.5 py-0.5 text-[10px] transition-colors ${
                problemOpen
                  ? "border-yellow/50 bg-yellow/10 text-yellow"
                  : "border-line2 text-muted hover:border-yellow/50 hover:text-yellow"
              }`}
            >
              {tc("subagent.problem")}
            </button>
          </>
        )}
        {!isDelegation && problemOpen && !confirmed && (
          <>
            <button
              onClick={goToChild}
              title={tc("subagent.correctInChildTitle")}
              className="rounded-md border border-yellow/40 bg-yellow/10 px-1.5 py-0.5 text-[10px] text-yellow transition-colors hover:bg-yellow/20"
            >
              {tc("subagent.correctInChild")}
            </button>
            <button
              onClick={escalate}
              title={tc("subagent.escalateTitle")}
              className="rounded-md border border-yellow/40 bg-yellow/10 px-1.5 py-0.5 text-[10px] text-yellow transition-colors hover:bg-yellow/20"
            >
              {tc("subagent.escalate")}
            </button>
          </>
        )}
        {sub?.acceptance && (
          <span className="ml-auto text-[10px] text-faint">{tc("subagent.verdictsInReport")}</span>
        )}
      </div>
    </div>
  );
}

/** 子代卡片行集（系统行渲染入口）。 */
export function SubagentBlocks({
  blocks,
  session,
}: {
  blocks: SubagentCardBlock[];
  /** 子会话视图传入：简报卡据此读 spawn_subagent 的实际参数（以它为准） */
  session?: SessionMeta;
}) {
  if (!blocks.length) return null;
  return (
    <div className="flex flex-col gap-2">
      {blocks.map((b, i) =>
        b.kind === "subagent_spawn" ? (
          <SubagentSpawnCard key={`${b.sessionId}-${i}`} block={b} />
        ) : b.kind === "subagent_result" ? (
          <SubagentResultCard key={`${b.sessionId}-${i}`} block={b} />
        ) : (
          <SubagentBriefCard key={`sa-brief-row-${i}`} block={b} session={session} />
        ),
      )}
    </div>
  );
}

/** 子会话首条 <ginno_subagent_brief> 消息折出的任务简报卡：goal/约束/验收
 *  一屏可读，报告格式与输出纪律两段附录（notes）收进折叠区。 */
export function SubagentBriefCard({
  block,
  session,
}: {
  block: Extract<Block, { kind: "subagent_brief" }>;
  /** 子会话的会话元数据：goal/constraints/acceptance/agent_type/mode 是
   *  spawn_subagent 创建时写入的**实际参数**——以它为准（文本只补「其他」），
   *  这样子会话里的简报与主对话的发起卡不会出现两套说法。 */
  session?: SessionMeta;
}) {
  const [notesOpen, setNotesOpen] = useState(false);
  const { tc } = useChatT();
  const meta = session?.subagent;
  const type = String(
    (meta as { agent_type?: string } | undefined)?.agent_type ?? "",
  ).trim();
  const isFork = meta ? meta.mode === "fork" : !!block.fork;
  // 参数为准：meta 有值就用 meta（与发起卡同源），没有才退回简报文本。
  const goal = meta?.goal || block.goal;
  const constraints = meta?.constraints || block.constraints;
  const acceptance = meta?.acceptance || block.acceptance;
  const sections: Array<[string, string | undefined, string]> = [
    [tc("subagent.goalSection"), goal, "text-txt"],
    [tc("subagent.constraintsSection"), constraints, "text-muted"],
    [tc("subagent.acceptanceSection"), acceptance, "text-muted"],
    [tc("subagent.otherSection"), block.extra, "text-muted"],
  ];
  return (
    <div className="rounded-lg border border-violet/30 bg-violet/[0.04] px-3 py-2.5 text-xs">
      <div className="flex items-center gap-1.5">
        <span className="shrink-0">🧭</span>
        <span className="shrink-0 font-medium text-violet">{tc("subagent.briefTitle")}</span>
        {type && (
          <span
            className="rounded-full border border-violet/40 bg-violet/10 px-1.5 text-[10px] leading-4 text-violet"
            title={tc("subagent.typeTitle", { type })}
          >
            {type}
          </span>
        )}
        {isFork && (
          <span
            className="rounded-md border border-violet/40 bg-violet/10 px-1.5 py-0.5 text-[10px] text-violet"
            title={tc("subagent.forkInheritedTitle")}
          >
            fork
          </span>
        )}
      </div>
      {sections.map(([label, value, cls]) =>
        value ? (
          <div key={label} className="mt-1.5">
            <span className="text-[10px] font-medium text-muted">{label}</span>
            <div className={`whitespace-pre-wrap break-words leading-relaxed ${cls}`}>
              {value}
            </div>
          </div>
        ) : null,
      )}
      {(block.persona || block.notes) && (
        <div className="mt-2 border-t border-line/60 pt-1.5">
          <button
            onClick={() => setNotesOpen((v) => !v)}
            className="text-[10px] text-faint transition-colors hover:text-muted"
          >
            {notesOpen ? "▾" : "▸"} {tc("subagent.notesToggle")}
          </button>
          {notesOpen && (
            <div className="mt-1 max-h-48 overflow-y-auto whitespace-pre-wrap break-words leading-relaxed text-faint">
              {block.persona && (
                <>
                  <div className="mb-1 text-[10px] text-violet">{tc("subagent.personaLabel")}</div>
                  {block.persona}
                </>
              )}
              {block.persona && block.notes && <div className="my-1.5 border-t border-line/60" />}
              {block.notes}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** 委派卡：同一轮并行发起的多个子代理合并成一张（设计 §6.3，2026-10-01 空间
 *  优化）。每行一个子任务——状态点 + 类型徽标（组内类型一致时收进组头）+
 *  标题 + 已运行时长；点行展开 goal/约束/验收与「查看完整对话」。
 *
 *  2026-10-12 收纳优化：多行时按状态三档分节（运行中 / 失败 / 成功），每节
 *  独立折叠——运行中、失败默认展开，成功默认收起（并行委派一多，全部平铺
 *  会把主对话挤没；空节不渲染）。单行时不分节，维持原平铺。
 *
 *  旧的单张发起卡（SubagentSpawnCard）仍保留：历史重放、单独一条等场景沿用。 */
export function SubagentGroupCard({
  rows,
}: {
  rows: Array<Extract<Block, { kind: "subagent_spawn" }>>;
}) {
  const g = useGinno();
  const router = useRouter();
  const { tc } = useChatT();
  const statusLabel = useStatusLabel();
  const [openId, setOpenId] = useState<string | null>(null);
  // 三档分节的折叠态。成功默认收起是收纳主收益；标题上的计数保证它
  // 「收起但可见」，不会像 2026-10-01 那次自动折叠一样像消失。
  const [bucketOpen, setBucketOpen] = useState<Record<string, boolean>>({});
  const bucketOpenOf = (b: string) => bucketOpen[b] ?? b !== "done";
  if (!rows.length) return null;

  // 类型徽标：组内一致 → 收到组头；混合 → 每行各自标注（用户指出的场景）。
  const types = rows.map((r) => {
    const live = g.sessions.find((s) => s.id === r.sessionId);
    return String((live?.subagent as { agent_type?: string } | undefined)?.agent_type ?? "").trim();
  });
  const nonEmpty = [...new Set(types.filter(Boolean))];
  const uniformType = nonEmpty.length === 1 ? nonEmpty[0] : "";
  const mixed = nonEmpty.length > 1;

  const openChild = (sid: string) => {
    g.setActiveSession(sid);
    if (window.location.pathname !== "/") router.push("/");
    window.dispatchEvent(new CustomEvent("ginno:focus-latest", { detail: sid }));
  };

  const renderRow = (r: Extract<Block, { kind: "subagent_spawn" }>, i: number) => {
    const live = g.sessions.find((s) => s.id === r.sessionId);
    const sub = live?.subagent;
    const status = sub?.status ?? "running";
    const meta = subagentStatusMeta(status);
    const active = status === "running" || status === "waiting";
    const title = r.title || sub?.goal || r.goal || tc("subagent.fallbackTitle");
    const expanded = openId === r.sessionId;
    const type = types[i];
    return (
      <div key={r.sessionId} className={`border-t border-line/40 first:border-t-0 ${!active ? "opacity-60" : ""}`}>
        <button
          onClick={() => setOpenId(expanded ? null : r.sessionId)}
          className="flex w-full items-center gap-2 py-1 text-left transition-colors hover:bg-card2/40"
          title={sub?.goal || r.goal}
        >
          <span className="shrink-0" title={tc("status.tooltip", { status: statusLabel(status) })}>{meta.glyph}</span>
          {mixed && type && (
            <span className="shrink-0 rounded-full border border-violet/40 bg-violet/10 px-1.5 text-[10px] leading-4 text-violet">
              {type}
            </span>
          )}
          <span className="min-w-0 flex-1 truncate text-muted">{title}</span>
          {r.spawnedAt && active && (
            <span className="shrink-0 text-[10px] text-faint">
              {tc("subagent.runningFor", { time: subagentElapsed(r.spawnedAt) })}
            </span>
          )}
          <ChevronRight
            className={`h-3 w-3 shrink-0 text-faint transition-transform ${expanded ? "rotate-90" : ""}`}
          />
        </button>
        {expanded && (
          <div className="pb-2 pl-5 pr-1">
            {(sub?.goal || r.goal) && (
              <div className="whitespace-pre-wrap break-words leading-relaxed text-muted">
                {tc("subagent.goalLabel")}{sub?.goal || r.goal}
              </div>
            )}
            {(sub?.constraints || r.constraints) && (
              <div className="mt-1 whitespace-pre-wrap break-words leading-relaxed text-faint">
                {tc("subagent.constraintsLabel")}{sub?.constraints || r.constraints}
              </div>
            )}
            {(sub?.acceptance || r.acceptance) && (
              <div className="mt-1 whitespace-pre-wrap break-words leading-relaxed text-faint">
                {tc("subagent.acceptanceLabel")}{sub?.acceptance || r.acceptance}
              </div>
            )}
            <div className="mt-1.5 flex items-center gap-2 text-[10px] text-faint">
              <button
                onClick={() => openChild(r.sessionId)}
                className="rounded-md border border-line2 px-1.5 py-0.5 text-muted transition-colors hover:border-violet/50 hover:text-violet"
              >
                {tc("subagent.viewFullConversation")}
              </button>
              <span>{r.origin === "user" ? tc("subagent.startedByUser") : tc("subagent.startedByAgent")}</span>
              {typeof r.depth === "number" && <span>{tc("subagent.depth", { n: r.depth + 1 })}</span>}
            </div>
          </div>
        )}
      </div>
    );
  };

  // 单行不分节：一条子任务配节头是纯噪音。
  if (rows.length === 1) {
    return (
      <div className="rounded-lg border border-line bg-card/60 px-3 py-2 text-xs">
        <div className="flex items-center gap-1.5">
          <span className="shrink-0">🧭</span>
          <span className="min-w-0 truncate font-medium text-txt">
            {tc("subagent.delegated", { count: rows.length })}
          </span>
          {uniformType && (
            <span
              className="shrink-0 rounded-full border border-violet/40 bg-violet/10 px-1.5 text-[10px] leading-4 text-violet"
              title={tc("subagent.typeTitle", { type: uniformType })}
            >
              {uniformType}
            </span>
          )}
        </div>
        <div className="mt-1.5 flex flex-col">{rows.map((r, i) => renderRow(r, i))}</div>
      </div>
    );
  }

  // 三档分节：桶序 = 运行中 → 失败 → 成功；行在桶内保持原顺序，
  // 类型徽标的原始下标（types[i]）随之携带。
  const bucketed = SUBAGENT_BUCKET_ORDER.map((b) => ({
    bucket: b,
    entries: rows
      .map((r, i) => ({ r, i }))
      .filter(({ r }) => {
        const sub = g.sessions.find((s) => s.id === r.sessionId);
        return subagentStatusBucket(sub?.subagent?.status) === b;
      }),
  })).filter((b) => b.entries.length > 0);

  return (
    <div className="rounded-lg border border-line bg-card/60 px-3 py-2 text-xs">
      <div className="flex items-center gap-1.5">
        <span className="shrink-0">🧭</span>
        <span className="min-w-0 truncate font-medium text-txt">
          {tc("subagent.delegated", { count: rows.length })}
        </span>
        {uniformType && (
          <span
            className="shrink-0 rounded-full border border-violet/40 bg-violet/10 px-1.5 text-[10px] leading-4 text-violet"
            title={tc("subagent.typeTitle", { type: uniformType })}
          >
            {uniformType}
          </span>
        )}
      </div>
      <div className="mt-1.5 flex flex-col">
        {bucketed.map(({ bucket, entries }) => {
          const open = bucketOpenOf(bucket);
          return (
            <div key={bucket} className="border-t border-line/40 first:border-t-0">
              <button
                onClick={() => setBucketOpen((m) => ({ ...m, [bucket]: !open }))}
                className="flex w-full items-center gap-1.5 py-1 text-[10px] text-faint transition-colors hover:text-muted"
                title={statusLabel(bucket)}
              >
                <ChevronRight className={`h-3 w-3 shrink-0 transition-transform ${open ? "rotate-90" : ""}`} />
                <DuckStatusIcon status={bucket} />
                <span>{statusLabel(bucket)}</span>
                <span>· {entries.length}</span>
              </button>
              {open && <div className="flex flex-col">{entries.map(({ r, i }) => renderRow(r, i))}</div>}
            </div>
          );
        })}
      </div>
    </div>
  );
}
