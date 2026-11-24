// engine/handle 的 dispatch 契约测试（重构批次 0——handle() 后续注册表化的
// 验收基准）。三层结构：
//
//   ① 事件类型全集枚举快照：从 handle.ts 源码静态提取 switch 的 case 清单，
//      与人工钉住的清单逐项比对——将来把巨型 switch 拆成注册表时，任何
//      丢 case / 改名 / 漏迁移都会在这里先红，而不是在用户桌面上丢事件。
//   ② 全事件 dispatch 触达表：79 个事件类型逐个构造最小事件喂进真实
//      handle()，断言每个 case 至少触达它该有的核心观察点（g.* 回调 /
//      store 落块 / refs 状态 / 通知出口）。dispatch 表错键会静默掉进
//      switch 之外（无 default），这层就是防「静默丢事件」的网。
//   ③ 高风险 case 深度断言：token.delta 共享体边界（不清 sending）、
//      message.end 收口契约（turn_meta 定格 / reply 进合并窗不直发 /
//      origin 门）、permission.request 与 user.question 的弹层边界、
//      error 即时通知、turn.stopped 全量复位、notice auto_retry 原位替换、
//      压缩镜像收尾与 turn.state 探针保镜像。
//
// mock 范式继承 handleNotify.test.ts / handleUsage.test.ts：node 环境无 DOM，
// 副作用出口（OS 通知 / notice 总线 / runtime fetch / steer·connect 兄弟模块）
// 全部 vi.mock 成 spy；notifyHub 的 ReplyMergeWindow 保留真实实现（prototype
// spy 观察 reply 合并窗的 push），仅 shouldNotify 固定为 true 消除对本地
// settings.json 的依赖。通知去重的行为级深测在 handleNotify.test.ts，不重复。

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// vi.mock 提升到 import 之前，工厂不能引用顶层 const —— 全走 vi.hoisted。
const { notifyNative } = vi.hoisted(() => ({
  notifyNative: vi.fn(async (_n: unknown) => true),
}));
const { pushNotice } = vi.hoisted(() => ({ pushNotice: vi.fn() }));
const { getWorkflowRun } = vi.hoisted(() => ({ getWorkflowRun: vi.fn() }));
const { flushSteerQueue, recallSteers, settleRecalled } = vi.hoisted(() => ({
  flushSteerQueue: vi.fn(),
  recallSteers: vi.fn(),
  settleRecalled: vi.fn(),
}));
const { reconcileTurnFromHistory } = vi.hoisted(() => ({
  reconcileTurnFromHistory: vi.fn(async () => ({})),
}));
const { pushModPane } = vi.hoisted(() => ({ pushModPane: vi.fn() }));
const { pushDevSwapRun, setDevSwapPendingConfirm } = vi.hoisted(() => ({
  pushDevSwapRun: vi.fn(),
  setDevSwapPendingConfirm: vi.fn(),
}));

vi.mock("@/lib/desktop", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  notifyNative,
}));
vi.mock("@/lib/notice", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  pushNotice,
}));
vi.mock("@/lib/runtime", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getWorkflowRun,
}));
vi.mock("@/components/chat/engine/steer", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  flushSteerQueue,
  recallSteers,
  settleRecalled,
}));
vi.mock("@/components/chat/engine/connect", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  reconcileTurnFromHistory,
}));
vi.mock("@/components/chat/mod/paneBus", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  pushModPane,
}));
vi.mock("@/lib/devswap/bus", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  pushDevSwapRun,
  setDevSwapPendingConfirm,
}));
// shouldNotify 真实实现读偏好缓存（可被本地 settings.json 关掉）——契约测试
// 要的是确定性：闸门恒开。isSessionAttentive 保留真实（node 无 document，
// 恒 false = 「没在看」），ReplyMergeWindow 保留真实类供 prototype spy。
vi.mock("@/lib/notifyHub", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  shouldNotify: () => true,
}));

import { ReplyMergeWindow } from "@/lib/notifyHub";
import { createDisplayScheduler, handle } from "@/components/chat/engine/handle";
import type { EngineCtx } from "@/components/chat/engine/ctx";

const SID = "s1";
type AnyRec = Record<string, unknown>;

// ── 最小可跑通 EngineCtx（超集桩：覆盖全部 case 真正读到的字段）──────────
interface Bag {
  ctx: EngineCtx;
  g: Record<string, ReturnType<typeof vi.fn> | unknown>;
  steerQ: { handleEvent: ReturnType<typeof vi.fn>; itemsFor: ReturnType<typeof vi.fn> };
  setPermission: ReturnType<typeof vi.fn>;
  setPropose: ReturnType<typeof vi.fn>;
  setModBands: ReturnType<typeof vi.fn>;
  setModAsks: ReturnType<typeof vi.fn>;
  onUsageChange: ReturnType<typeof vi.fn>;
  onContextChange: ReturnType<typeof vi.fn>;
  finishSynthesisWait: ReturnType<typeof vi.fn>;
  syncDisplay: ReturnType<typeof vi.fn>;
}

function makeBag(overrides: AnyRec = {}): Bag {
  const ref = <T,>(v: T) => ({ current: v });
  const g = {
    sessions: [{ id: SID, title: "契约会话" }],
    applySessionPatch: vi.fn(),
    reloadTodos: vi.fn(),
    reloadSkills: vi.fn(),
    reloadMemoryBadge: vi.fn(),
    reloadAgents: vi.fn(),
    reloadProviders: vi.fn(),
    reloadWorkflows: vi.fn(),
    reloadWorkflowRuns: vi.fn(),
    reloadSynthesisCases: vi.fn(),
    reloadArtifacts: vi.fn(),
    reloadProjectArtifacts: vi.fn(),
    notifyRunToolActivity: vi.fn(),
    notifyCodeChange: vi.fn(),
    openPreview: vi.fn(),
    notifyPreviewInvalidate: vi.fn(),
    notifyGoal: vi.fn(),
    notifyTeamUpdated: vi.fn(),
    notifyPlanUpdated: vi.fn(),
    notifySubagentSpawned: vi.fn(),
    notifySubagentStatus: vi.fn(),
    markSessionUnread: vi.fn(),
    clearSessionUnread: vi.fn(),
    watchingSession: vi.fn(() => false),
  };
  const steerQ = {
    handleEvent: vi.fn(),
    itemsFor: vi.fn(() => [] as Array<{ steerId: string; text: string; images: []; files: []; turnId?: string }>),
    clear: vi.fn(),
  };
  const setModBands = vi.fn((u: (p: unknown) => unknown) => u({}));
  const setModAsks = vi.fn((u: (p: unknown) => unknown) => u({}));
  const setPermission = vi.fn();
  const setPropose = vi.fn();
  const onUsageChange = vi.fn();
  const onContextChange = vi.fn();
  const finishSynthesisWait = vi.fn();
  const syncDisplay = vi.fn();
  const base = {
    i18nRef: ref({
      tr: (k: string, v?: Record<string, unknown>) => (v ? `${k} ${JSON.stringify(v)}` : k),
      evText: (_e: unknown, f: string) => f,
    }),
    activeSidRef: ref<string | null>(SID),
    storeRef: ref<AnyRec>({}),
    liveBySessionRef: ref<AnyRec>({}),
    socketsRef: ref<AnyRec>({}),
    permsRef: ref<AnyRec>({}),
    proposeRef: ref<AnyRec>({}),
    busyBySessionRef: ref<AnyRec>({}),
    serverRunningRef: ref<AnyRec>({}),
    compactStageRef: ref<AnyRec>({}),
    streamAgentRef: ref<AnyRec>({}),
    orphanStreamRef: ref<AnyRec>({}),
    adoptedTurnRef: ref<AnyRec>({}),
    retryNoticeRef: ref<AnyRec>({}),
    seenTurnStartRef: ref<AnyRec>({}),
    reconcileTimerRef: ref<AnyRec>({}),
    probeSeqRef: ref<AnyRec>({}),
    sendSeqRef: ref<AnyRec>({}),
    runsBySessionRef: ref<AnyRec>({}),
    sumPendingRef: ref<string | null>(null),
    steerQ,
    setPermission,
    setPropose,
    setModBands,
    setModAsks,
    onUsageChange,
    onContextChange,
    finishSynthesisWait,
    session: { agent_id: "ag1" },
    g,
    syncDisplay,
  };
  const ctx = { ...base, ...overrides } as unknown as EngineCtx;
  return { ctx, g, steerQ, setPermission, setPropose, setModBands, setModAsks, onUsageChange, onContextChange, finishSynthesisWait, syncDisplay };
}

// ── store 读取 helper（宽类型断言，范式同既有测试）────────────────────────
type Row = {
  id?: string; role?: string; status?: string; failed?: boolean; error?: boolean;
  turnId?: string; ts?: number; teamDelivery?: AnyRec; blocks: Array<AnyRec & { kind: string }>;
};
const rows = (bag: Bag, sid = SID): Row[] =>
  ((bag.ctx.storeRef.current as AnyRec)[sid] as Row[] | undefined) ?? [];
const kinds = (bag: Bag, sid = SID): string[] =>
  rows(bag, sid).flatMap((m) => m.blocks.map((b) => b.kind));
const blockOf = (bag: Bag, kind: string, sid = SID) =>
  rows(bag, sid).flatMap((m) => m.blocks).find((b) => b.kind === kind);
const refOf = (bag: Bag, name: keyof EngineCtx, sid = SID): unknown =>
  ((bag.ctx[name] as { current: AnyRec }).current as AnyRec)[sid];
const setRef = (bag: Bag, name: keyof EngineCtx, sid: string, v: unknown) => {
  ((bag.ctx[name] as { current: AnyRec }).current as AnyRec)[sid] = v;
};

/** 预置一条 sending 状态的用户气泡（markDelivered 断言用）。 */
function seedSendingUser(bag: Bag) {
  (bag.ctx.storeRef.current as AnyRec)[SID] = [
    { id: "u1", role: "user", status: "sending", blocks: [] },
  ];
}

// window 全局桩（模块级 spy，mod.* 的 dispatchEvent 观察点直接引用）：
// isSessionAttentive 在 isAppAttentive()（document 不存在）即早退，不会碰
// location —— stub 无副作用。
const winDispatch = vi.fn();
beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("window", { dispatchEvent: winDispatch });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ── ① 事件类型全集枚举快照 ────────────────────────────────────────────────
// 批次 6-2 注册表化后，case 清单的权威源是 handlers/index.ts 的注册表
// （["event", handler] 条目顺序 = 原 switch case 顺序）。
const SRC = readFileSync(fileURLToPath(new URL("./handlers/index.ts", import.meta.url)), "utf8");
const STREAM_SRC = readFileSync(
  fileURLToPath(new URL("./handlers/01-stream.ts", import.meta.url)),
  "utf8",
);
const EXTRACTED_CASES = [...SRC.matchAll(/^\s*\["([^"]+)",/gm)].map((m) => m[1]);

/** 人工钉住的 handle() switch case 全集（源码顺序）。重构迁移注册表时此表
 *  就是验收清单：迁移后必须每个键都还能路由到等价处理。 */
const EXPECTED_CASES = [
  "token.delta", "thinking.delta", "reasoning.summary", "tool.start", "tool.args", "tool.args_delta", "tool.end",
  "user.question",
  "widget.emit", "ref.emit", "image.emit",
  "llm.request", "llm.response",
  "workflow.emit",
  "turn.start",
  "permission.request", "version.propose",
  "todos.changed", "skills.changed", "memory.changed", "agents.changed",
  "providers.changed", "session.model_changed",
  "workflows.changed",
  "synthesis.event",
  "run.bind", "run.event", "run.status",
  "artifacts.changed", "code.changed",
  "preview.emit", "preview.invalidate",
  "worker.waiting", "worker.progress", "worker.resumed", "worker.pending_questions",
  "linked.done",
  "steer.recalled", "steer.accepted", "steer.answered", "steer.absorbed",
  "delegate_update",
  "session.state_changed",
  "runtime.devswap_progress", "runtime.lastwords_pending",
  "runtime.lastwords_restored", "runtime.dev_swap_pending",
  "deliver.report",
  "notice",
  "goal.updated", "team.changed", "plan.updated", "goal.archived", "goal.cleared",
  "session_title", "session.context",
  "subagent.spawned", "subagent.status", "subagent.plan", "subagent.plan.cancelled",
  "context.updated", "context.microcompacted", "context.compacting", "context.compacted",
  "history.cleared",
  "usage",
  "turn.state",
  "message.end",
  "turn.stopped",
  "error",
  "team.delivered", "team.reply",
  "mod.bands", "mod.toast", "mod.state.changed",
  "mod.prompt.fill", "mod.prompt.suggest", "mod.ask",
  "mod.pane.open", "mod.pane.update", "mod.pane.close",
];

describe("① handle() 事件类型全集枚举快照（防拆分丢 case）", () => {
  it("源码提取的 case 清单与钉住清单完全一致（顺序 + 内容）", () => {
    expect(EXTRACTED_CASES).toEqual(EXPECTED_CASES);
  });

  it("case 标签无重复（共享体是相邻 case，不是同名标签）", () => {
    expect(new Set(EXTRACTED_CASES).size).toBe(EXTRACTED_CASES.length);
  });

  it("流帧共享体结构：7 事件相邻共享，user.question 独立 handler（P0-1 结构防线）", () => {
    // 2026-11-14 通知风暴的结构载体：delta/tool 共享体顺序 + ask 通知必须在
    // user.question 自己的 handler 里。拆注册 / 换共享函数都会红。
    expect(SRC).toMatch(
      /\["token\.delta", stream\.onDelta\],\s*\n\s*\["thinking\.delta", stream\.onDelta\],\s*\n\s*\["reasoning\.summary", stream\.onDelta\],\s*\n\s*\["tool\.start", stream\.onStreamFrame\],\s*\n\s*\["tool\.args", stream\.onStreamFrame\],\s*\n\s*\["tool\.args_delta", stream\.onStreamFrame\],\s*\n\s*\["tool\.end", stream\.onStreamFrame\],\s*\n\s*\["user\.question", stream\.onQuestion\],/,
    );
    // ask 通知仍是 user.question handler 的专属首副作用（不进共享函数）。
    expect(STREAM_SRC).toMatch(
      /export function onQuestion\(ctx: EngineCtx, sid: string, ev: ServerEvent\) \{\s*\n\s*maybeNotifyAskUser\(ctx, sid, ev\);/,
    );
  });
});

// ── ② 全事件 dispatch 触达表 ──────────────────────────────────────────────
interface CaseSpec {
  event: string;
  desc: string;
  /** 前置事件序列（同一 ctx 依序先喂，构造流中间态） */
  pre?: Array<{ event: string } & AnyRec>;
  /** 事件载荷（event 字段自动带上） */
  ev?: AnyRec;
  /** ctx 种子（pre 之前应用） */
  seed?: (bag: Bag) => void;
  probe: (bag: Bag) => void | Promise<void>;
}

const DISPATCH_TABLE: CaseSpec[] = [
  // —— 流帧（adoptOrphanStream + mutateLive 家族）——
  {
    event: "token.delta", desc: "追加 text 块 + 孤儿认领置 busy", ev: { content: "甲", turn_id: "t1" },
    probe: (b) => {
      expect(kinds(b)).toContain("text");
      expect(refOf(b, "busyBySessionRef")).toBe(true);
    },
  },
  {
    event: "thinking.delta", desc: "追加 thinking 块", ev: { content: "思", turn_id: "t1" },
    probe: (b) => expect(kinds(b)).toContain("thinking"),
  },
  {
    event: "reasoning.summary", desc: "摘要快照", ev: { summary: { revision: 1, parts: [{ id: "a", text: "Review" }] } },
    probe: (b) => expect(kinds(b)).toContain("reasoning_summary"),
  },
  {
    event: "tool.start", desc: "追加 pending tool 块", ev: { id: "c1", name: "bash", turn_id: "t1" },
    probe: (b) => expect(blockOf(b, "tool")).toMatchObject({ name: "bash", pending: true }),
  },
  {
    event: "tool.args", desc: "给 pending tool 块挂 argsPreview",
    pre: [{ event: "tool.start", id: "c1", name: "bash", turn_id: "t1" }],
    ev: { id: "c1", preview: "ls -la" },
    probe: (b) => expect(blockOf(b, "tool")).toMatchObject({ argsPreview: "ls -la" }),
  },
  {
    event: "tool.args_delta", desc: "给 pending tool 块挂累计参数快照",
    pre: [{ event: "tool.start", id: "c1", name: "bash", turn_id: "t1" }],
    ev: { id: "c1", tail: '{"command":', total: 11 },
    probe: (b) => expect(blockOf(b, "tool")).toMatchObject({ argsStream: '{"command":' }),
  },
  {
    event: "tool.end", desc: "tool 块关闭（pending=false）",
    pre: [{ event: "tool.start", id: "c1", name: "bash", turn_id: "t1" }],
    ev: { id: "c1", name: "bash", content: "ok" },
    probe: (b) => expect(blockOf(b, "tool")).toMatchObject({ pending: false, content: "ok" }),
  },
  {
    event: "user.question", desc: "渲染 question 卡 + 弹一条 ask 通知，不碰权限弹层",
    ev: { id: "q-dispatch-1", question: "选哪个？" },
    probe: (b) => {
      expect(kinds(b)).toContain("question");
      expect(notifyNative).toHaveBeenCalledTimes(1);
      expect((notifyNative.mock.calls[0] as unknown[])[0]).toMatchObject({ notifyKind: "askUser", id: SID });
      expect(refOf(b, "permsRef")).toBeUndefined();
    },
  },
  {
    event: "widget.emit", desc: "追加 widget 块", ev: { kind: "kpi", data: { v: 1 } },
    probe: (b) => expect(kinds(b)).toContain("widget"),
  },
  {
    event: "ref.emit", desc: "追加 ref 块", ev: { kind: "doc", name: "报告" },
    probe: (b) => expect(kinds(b)).toContain("ref"),
  },
  {
    event: "image.emit", desc: "追加 image 块", ev: { file_id: "f1" },
    probe: (b) => expect(kinds(b)).toContain("image"),
  },
  {
    event: "llm.request", desc: "追加 pending llm_call 行", ev: { req_id: "r1", attempt: 1, model: "m" },
    probe: (b) => expect(blockOf(b, "llm_call")).toMatchObject({ reqId: "r1", attempt: 1, pending: true }),
  },
  {
    event: "llm.response", desc: "原地补全 llm_call 终态（不新增行）",
    pre: [{ event: "llm.request", req_id: "r1", attempt: 1, model: "m" }],
    ev: { req_id: "r1", attempt: 1, ok: true },
    probe: (b) => {
      const calls = kinds(b).filter((k) => k === "llm_call");
      expect(calls).toHaveLength(1);
      expect(blockOf(b, "llm_call")).toMatchObject({ pending: false });
    },
  },
  {
    event: "workflow.emit", desc: "追加 workflow 块", ev: { run: { id: "wf1", status: "running", steps: [] } },
    probe: (b) => expect(kinds(b)).toContain("workflow"),
  },
  {
    event: "turn.start", desc: "busy 置位 + 气泡贴 agent/turnId + 插 turn_meta + 侧栏排序刷新",
    seed: seedSendingUser,
    ev: { turn_id: "t1", agent_id: "a1", name: "甲" },
    probe: (b) => {
      expect(refOf(b, "busyBySessionRef")).toBe(true);
      expect(refOf(b, "seenTurnStartRef")).toBe(true);
      const live = rows(b).find((m) => m.blocks.some((x) => x.kind === "turn_meta"));
      expect(live).toMatchObject({ agentId: "a1", turnId: "t1" });
      expect(b.g.applySessionPatch).toHaveBeenCalledWith(SID, { updated: expect.any(Number) });
      expect(rows(b).find((m) => m.id === "u1")).toMatchObject({ status: undefined });
    },
  },

  // —— 弹层 / 轻刷新 ——
  {
    event: "permission.request", desc: "写 permsRef（权限卡数据源）",
    ev: { tool: "bash", args: { cmd: "ls" } },
    probe: (b) => expect(refOf(b, "permsRef")).toEqual({ tool: "bash", args: { cmd: "ls" } }),
  },
  {
    event: "version.propose", desc: "写 proposeRef（工作流版本提案）",
    ev: { workflow_id: "w1", from_version: 2, diff: "d", rationale: "r" },
    probe: (b) => expect(refOf(b, "proposeRef")).toEqual({ workflow_id: "w1", from_version: 2, diff: "d", rationale: "r" }),
  },
  { event: "todos.changed", desc: "刷 TODO", ev: {}, probe: (b) => expect(b.g.reloadTodos).toHaveBeenCalled() },
  { event: "skills.changed", desc: "刷 skill 清单", ev: {}, probe: (b) => expect(b.g.reloadSkills).toHaveBeenCalled() },
  { event: "memory.changed", desc: "刷记忆角标", ev: {}, probe: (b) => expect(b.g.reloadMemoryBadge).toHaveBeenCalled() },
  { event: "agents.changed", desc: "刷 agent 列表", ev: {}, probe: (b) => expect(b.g.reloadAgents).toHaveBeenCalled() },
  { event: "providers.changed", desc: "刷新模型档位列表", ev: {}, probe: (b) => expect(b.g.reloadProviders).toHaveBeenCalled() },
  {
    event: "session.model_changed", desc: "patch 三元组 + 轻提示（不进收件箱）",
    ev: { provider: "p1", model: "m1", thinking_effort: "high" },
    probe: (b) => {
      expect(b.g.applySessionPatch).toHaveBeenCalledWith(SID, { provider: "p1", model: "m1", thinking_effort: "high" });
      expect(pushNotice).toHaveBeenCalledWith("success", expect.any(String), expect.objectContaining({ dedupeKey: `model:${SID}`, inbox: false }));
    },
  },
  {
    event: "workflows.changed", desc: "刷工作流双列表", ev: {},
    probe: (b) => { expect(b.g.reloadWorkflows).toHaveBeenCalled(); expect(b.g.reloadWorkflowRuns).toHaveBeenCalled(); },
  },
  {
    event: "synthesis.event", desc: "刷总结面板", ev: { kind: "started", synthesis_id: "sy1" },
    probe: (b) => expect(b.g.reloadSynthesisCases).toHaveBeenCalled(),
  },

  // —— workflow run 镜像 ——
  {
    event: "run.bind", desc: "拉 run 详情入会话运行表 + 刷全量列表",
    seed: () => {
      // mock 需在 handle 之前给出 promise（run.bind 同步调 .then）。
      getWorkflowRun.mockResolvedValue({ run: { id: "rb1", status: "running", steps: [], updated: 0 } });
    },
    ev: { run_id: "rb1" },
    probe: async (b) => {
      // run.bind 的 fetch 回调是异步的——handle 先行返回，waitFor 等落表。
      await vi.waitFor(() => {
        expect(((b.ctx.runsBySessionRef.current as AnyRec)[SID] as Array<{ id: string }>).some((r) => r.id === "rb1")).toBe(true);
      });
    },
  },
  {
    event: "run.event", desc: "in-flight 工具调用可见化（tool_call → notifyRunToolActivity）",
    ev: { run_id: "re1", payload: { kind: "tool_call", node_id: "n1", calls: [{ name: "bash", args: { cmd: "ls" } }] } },
    probe: (b) => expect(b.g.notifyRunToolActivity).toHaveBeenCalledWith("re1", expect.objectContaining({ nodeId: "n1", toolName: "bash" })),
  },
  {
    event: "run.status", desc: "镜像终态 + 清 pending_interrupt",
    seed: (b) => { (b.ctx.runsBySessionRef.current as AnyRec)[SID] = [{ id: "rs1", status: "running", steps: [], pending_interrupt: { x: 1 } }]; },
    ev: { run_id: "rs1", status: "done" },
    probe: (b) => {
      const run = ((b.ctx.runsBySessionRef.current as AnyRec)[SID] as Array<AnyRec>)[0];
      expect(run).toMatchObject({ status: "done", pending_interrupt: null });
      expect(b.g.reloadWorkflowRuns).toHaveBeenCalled();
    },
  },

  // —— 产物 / 代码 / 预览 ——
  {
    event: "artifacts.changed", desc: "刷双产物列表", ev: {},
    probe: (b) => { expect(b.g.reloadArtifacts).toHaveBeenCalled(); expect(b.g.reloadProjectArtifacts).toHaveBeenCalled(); },
  },
  {
    event: "code.changed", desc: "带 POST-WRITE 版本通知代码面板",
    ev: { path: "/tmp/a.py", op: "write", version: "v3" },
    probe: (b) => expect(b.g.notifyCodeChange).toHaveBeenCalledWith(expect.objectContaining({ path: "/tmp/a.py", op: "write", version: "v3", baseVersion: null })),
  },
  {
    event: "preview.emit", desc: "打开预览 + 刷产物", ev: { open: true, file_id: "pf1", name: "n", path: "p" },
    probe: (b) => { expect(b.g.openPreview).toHaveBeenCalledWith(expect.objectContaining({ id: "pf1" })); expect(b.g.reloadArtifacts).toHaveBeenCalled(); },
  },
  {
    event: "preview.invalidate", desc: "通知 FileViewer 失效", ev: { file_id: "pi1" },
    probe: (b) => expect(b.g.notifyPreviewInvalidate).toHaveBeenCalledWith("pi1"),
  },

  // —— 通知类 ——
  {
    event: "worker.waiting", desc: "等待可见化 notice（按事件+目标去重）",
    ev: { target: "s2", target_title: "T" },
    probe: (b) => expect(pushNotice).toHaveBeenCalledWith("info", expect.any(String), expect.objectContaining({ dedupeKey: "worker.waiting-s2" })),
  },
  {
    event: "worker.progress", desc: "进度节选 notice", ev: { target: "s2", target_title: "T" },
    probe: () => expect(pushNotice).toHaveBeenCalledWith("info", expect.any(String), expect.objectContaining({ dedupeKey: "worker.progress-s2" })),
  },
  {
    event: "worker.resumed", desc: "结果注入 notice", ev: { target: "s2", target_title: "T" },
    probe: () => expect(pushNotice).toHaveBeenCalledWith("info", expect.any(String), expect.objectContaining({ dedupeKey: "worker.resumed-s2" })),
  },
  {
    event: "worker.pending_questions", desc: "代答请求 notice（按 qid 集去重，观看中静默）",
    ev: { target: "s2", questions: [{ qid: "q1", worker_title: "T" }] },
    probe: (b) => expect(pushNotice).toHaveBeenCalledWith("info", expect.any(String), expect.objectContaining({ dedupeKey: "worker.pending_questions-q1" })),
  },
  {
    event: "linked.done", desc: "进 reply 合并窗（与 message.end 同窗，不直发）",
    ev: { session_id: "s2", text: "完成" },
    probe: () => {
      expect(notifyNative).not.toHaveBeenCalled(); // 30s 窗内绝不直发
      expect(pushNotice).not.toHaveBeenCalled();
    },
  },

  // —— steering ——
  {
    event: "steer.recalled", desc: "对账移交 settleRecalled",
    ev: { steer_ids: ["sa", "sb"] },
    probe: () => expect(settleRecalled).toHaveBeenCalledWith(expect.anything(), SID, ["sa", "sb"]),
  },
  {
    event: "steer.accepted", desc: "移交 steerQ 队列", ev: { steer_id: "s1a" },
    probe: (b) => expect(b.steerQ.handleEvent).toHaveBeenCalled(),
  },
  {
    event: "steer.answered", desc: "同走 steerQ（parked 答案收走条目）", ev: { steer_id: "s1n" },
    probe: (b) => expect(b.steerQ.handleEvent).toHaveBeenCalled(),
  },
  {
    event: "steer.absorbed", desc: "注入带落转录（无本地条目走事件原文）",
    ev: { steer_id: "ab1", text: "追加内容", injected_at: 100 },
    probe: (b) => {
      const band = blockOf(b, "steer");
      expect(band).toMatchObject({ steerId: "ab1", text: "追加内容", injectedAt: 100 });
    },
  },

  // —— 委托 / 状态机 ——
  {
    event: "delegate_update", desc: "running 委托行 upsert（同 id 刷新）",
    ev: { delegation_id: "d1", running: true, text: "干活", backend: "subagent" },
    probe: (b) => expect(rows(b).find((m) => m.id === "delegate-live-d1")).toBeTruthy(),
  },
  {
    event: "session.state_changed", desc: "compacting 镜像置位",
    ev: { session_id: SID, state: "compacting" },
    probe: (b) => expect(refOf(b, "compactStageRef")).toBe("summarizing"),
  },

  // —— runtime 生命周期 ——
  {
    event: "runtime.devswap_progress", desc: "规范化后入 devswap 总线",
    ev: { run_id: "ds1", stage: "unit", state: "running" },
    probe: () => expect(pushDevSwapRun).toHaveBeenCalledWith(expect.objectContaining({ run_id: "ds1", stage: "unit", state: "running" })),
  },
  {
    event: "runtime.lastwords_pending", desc: "收件箱常驻卡 + 即时 OS 通知（askUser 类）",
    ev: { session_id: SID, head: "修 bug", tool_calls: 2, elapsed_s: 90 },
    probe: () => {
      expect(pushNotice).toHaveBeenCalledWith("info", expect.any(String), expect.objectContaining({ dedupeKey: `lastwords-pending:${SID}`, duration: 0 }));
      expect(notifyNative).toHaveBeenCalledTimes(1);
      expect((notifyNative.mock.calls[0] as unknown[])[0]).toMatchObject({ notifyKind: "askUser", id: SID });
    },
  },
  {
    event: "runtime.lastwords_restored", desc: "收件箱恢复条目（跳会话）",
    ev: { session_id: SID },
    probe: () => expect(pushNotice).toHaveBeenCalledWith("info", expect.any(String), expect.objectContaining({ dedupeKey: `lastwords:${SID}`, actionMeta: { kind: "session", id: SID } })),
  },
  {
    event: "runtime.dev_swap_pending", desc: "点亮确认条（非 auto 不自动重启）",
    ev: {},
    probe: async () => {
      await vi.waitFor(() => expect(setDevSwapPendingConfirm).toHaveBeenCalledWith("pending"));
    },
  },

  // —— 渠道 / 命令回执 ——
  {
    event: "deliver.report", desc: "投递回执文本落气泡 + 清 sending",
    seed: seedSendingUser,
    ev: { targets: [{ label: "甲", state: "started" }] },
    probe: (b) => {
      const text = kinds(b).includes("text") ? blockOf(b, "text") : undefined;
      expect(String(text?.text ?? "")).toContain("deliver.started");
      expect(rows(b).find((m) => m.id === "u1")).toMatchObject({ status: undefined });
    },
  },
  {
    event: "notice", desc: "内置命令回执直落气泡 + 清 sending",
    seed: seedSendingUser,
    ev: { message: "帮助文本" },
    probe: (b) => {
      expect(String(blockOf(b, "text")?.text ?? "")).toContain("帮助文本");
      expect(rows(b).find((m) => m.id === "u1")).toMatchObject({ status: undefined });
    },
  },

  // —— goal / team / plan ——
  {
    event: "goal.updated", desc: "goal 快照进 TopBar chip",
    ev: { goal: { id: "g1", status: "active" } },
    probe: (b) => expect(b.g.notifyGoal).toHaveBeenCalledWith(SID, { id: "g1", status: "active" }),
  },
  {
    event: "team.changed", desc: "团队聚合回流 + blocked 跃迁通知",
    ev: { team: { id: "tm1", member_rows: [{ instance_id: "i-dispatch-1", display_name: "甲", status: "blocked" }] } },
    probe: (b) => {
      expect(b.g.notifyTeamUpdated).toHaveBeenCalled();
      expect(pushNotice).toHaveBeenCalledWith("warn", expect.any(String), expect.objectContaining({ dedupeKey: "team-blocked-i-dispatch-1" }));
      expect(notifyNative).toHaveBeenCalledTimes(1);
    },
  },
  {
    event: "plan.updated", desc: "任务清单快照 upsert",
    ev: { plan: { plan_id: "p1", tasks: [] } },
    probe: (b) => expect(b.g.notifyPlanUpdated).toHaveBeenCalledWith({ plan_id: "p1", tasks: [] }),
  },
  { event: "goal.archived", desc: "fall-through 清 chip", ev: {}, probe: (b) => expect(b.g.notifyGoal).toHaveBeenCalledWith(SID, null) },
  { event: "goal.cleared", desc: "清 chip", ev: {}, probe: (b) => expect(b.g.notifyGoal).toHaveBeenCalledWith(SID, null) },
  {
    event: "session_title", desc: "侧栏即时改名",
    ev: { title: "新标题" },
    probe: (b) => expect(b.g.applySessionPatch).toHaveBeenCalledWith(SID, { title: "新标题", title_auto: false }),
  },
  {
    event: "session.context", desc: "挂载集 patch",
    ev: { context_folders: ["/a"], primary_folder: "/a" },
    probe: (b) => expect(b.g.applySessionPatch).toHaveBeenCalledWith(SID, { context_folders: ["/a"], primary_folder: "/a" }),
  },

  // —— 子代理生命周期 ——
  {
    event: "subagent.spawned", desc: "父会话转录加发起卡 + 侧栏建行",
    ev: { parent_session_id: SID, session_id: "sub1", goal: "g", type: "subagent", origin: "agent", depth: 1 },
    probe: (b) => {
      expect(b.g.notifySubagentSpawned).toHaveBeenCalled();
      expect(blockOf(b, "subagent_spawn")).toMatchObject({ sessionId: "sub1", goal: "g" });
    },
  },
  {
    event: "subagent.status", desc: "终态（done）落结果卡",
    ev: { parent_session_id: SID, session_id: "sub1", status: "done", result_summary: "ok" },
    probe: (b) => {
      expect(b.g.notifySubagentStatus).toHaveBeenCalled();
      expect(blockOf(b, "subagent_result")).toMatchObject({ sessionId: "sub1", summary: "ok" });
    },
  },
  {
    event: "subagent.plan", desc: "拆分方案折卡",
    ev: { plan_id: "pl1", task: "拆", subtasks: [{ t: 1 }] },
    probe: (b) => expect(blockOf(b, "subagent_plan")).toMatchObject({ planId: "pl1", task: "拆", status: "pending" }),
  },
  {
    event: "subagent.plan.cancelled", desc: "pending 卡翻 cancelled（幂等只动 pending）",
    pre: [{ event: "subagent.plan", plan_id: "pl1", task: "拆", subtasks: [] }],
    ev: { plan_id: "pl1" },
    probe: (b) => expect(blockOf(b, "subagent_plan")).toMatchObject({ status: "cancelled" }),
  },

  // —— 上下文管理 ——
  {
    event: "context.updated", desc: "非 environment 变更落居中行",
    ev: { changes: [{ section: "files", summary: "挂载变更" }] },
    probe: (b) => expect(blockOf(b, "context")).toMatchObject({ text: "挂载变更" }),
  },
  {
    event: "context.microcompacted", desc: "旧工具输出清理行（带 i18n 参数）",
    ev: { cleared_tool_outputs: 3 },
    probe: (b) => expect(blockOf(b, "context")).toMatchObject({ i18n_key: "chat.context.microcompacted", params: { n: 3 } }),
  },
  {
    event: "context.compacting", desc: "置压缩镜像 + 阶段",
    ev: { stage: "summarizing", pct: 10 },
    probe: (b) => expect(refOf(b, "compactStageRef")).toBe("summarizing"),
  },
  {
    event: "context.compacted", desc: "压缩完成居中行",
    ev: { compacted_messages: 5 },
    probe: (b) => expect(blockOf(b, "context")).toMatchObject({ i18n_key: "chat.context.compacted", params: { n: 5 } }),
  },
  {
    event: "history.cleared", desc: "转录整体丢弃 + 流式状态复位",
    seed: (b) => {
      (b.ctx.storeRef.current as AnyRec)[SID] = [{ id: "x", role: "user", blocks: [] }];
      setRef(b, "busyBySessionRef", SID, true);
      setRef(b, "orphanStreamRef", SID, true);
      setRef(b, "retryNoticeRef", SID, "chunk");
    },
    ev: {},
    probe: (b) => {
      expect(rows(b)).toHaveLength(0);
      expect(refOf(b, "liveBySessionRef")).toBeNull();
      expect(refOf(b, "busyBySessionRef")).toBe(false);
      expect(refOf(b, "orphanStreamRef")).toBe(false);
      expect(refOf(b, "retryNoticeRef")).toBeNull();
    },
  },

  // —— usage / 探针 ——
  {
    event: "usage", desc: "活跃会话：turn 增量回调 TopBar + 圆环替换",
    ev: { turn: { input_tokens: 5, output_tokens: 2 }, context: { percent: 42 } },
    probe: (b) => {
      expect(b.onUsageChange).toHaveBeenCalledWith(expect.objectContaining({ input_tokens: 5, output_tokens: 2, calls: 1 }));
      expect(b.onContextChange).toHaveBeenCalledWith({ percent: 42 });
    },
  },
  {
    event: "turn.state", desc: "running 探针回声：镜像 server 运行态",
    ev: { running: true },
    probe: (b) => expect(refOf(b, "serverRunningRef")).toBe(true),
  },

  // —— turn 收口三态 ——
  {
    event: "message.end", desc: "live 收口（liveId/busy 复位）",
    pre: [
      { event: "turn.start", turn_id: "t1", agent_id: "a1" },
      { event: "token.delta", content: "答", turn_id: "t1" },
    ],
    ev: { turn_id: "t1", meta: { elapsed_s: 3, input_tokens: 9, output_tokens: 4 } },
    probe: (b) => {
      expect(refOf(b, "liveBySessionRef")).toBeNull();
      expect(refOf(b, "busyBySessionRef")).toBe(false);
      expect(b.g.markSessionUnread).toHaveBeenCalledWith(SID);
    },
  },
  {
    event: "turn.stopped", desc: "用户中止：弹层清空 + 全量复位",
    pre: [{ event: "turn.start", turn_id: "t1", agent_id: "a1" }],
    seed: (b) => {
      setRef(b, "permsRef", SID, { tool: "bash", args: {} });
      setRef(b, "proposeRef", SID, { workflow_id: "w" });
    },
    ev: {},
    probe: (b) => {
      expect(refOf(b, "permsRef")).toBeNull();
      expect(refOf(b, "proposeRef")).toBeNull();
      expect(b.setPermission).toHaveBeenCalledWith(null);
      expect(b.setPropose).toHaveBeenCalledWith(null);
      expect(refOf(b, "busyBySessionRef")).toBe(false);
      expect(refOf(b, "liveBySessionRef")).toBeNull();
      expect(recallSteers).toHaveBeenCalledWith(expect.anything(), SID);
    },
  },
  {
    event: "error", desc: "失败卡 + 即时 failure 通知（不合并）",
    pre: [
      { event: "turn.start", turn_id: "t1", agent_id: "a1" },
      { event: "token.delta", content: "半截", turn_id: "t1" },
    ],
    ev: { message: "炸了" },
    probe: (b) => {
      expect(notifyNative).toHaveBeenCalledTimes(1);
      expect((notifyNative.mock.calls[0] as unknown[])[0]).toMatchObject({ notifyKind: "failure", id: SID });
      expect(rows(b).some((m) => m.error === true)).toBe(true);
      expect(refOf(b, "busyBySessionRef")).toBe(false);
      expect(refOf(b, "liveBySessionRef")).toBeNull();
    },
  },

  // —— AgentTeam 回执 ——
  {
    event: "team.delivered", desc: "delivered：清 sending + 防御性收口（不标 failed）",
    seed: (b) => {
      (b.ctx.storeRef.current as AnyRec)[SID] = [
        { id: "u1", role: "user", status: "sending", blocks: [] },
        { id: "live9", role: "assistant", blocks: [], turnId: "t9" },
      ];
      setRef(b, "liveBySessionRef", SID, "live9");
      setRef(b, "busyBySessionRef", SID, true);
    },
    ev: { receipt: "delivered", turn_id: "t9", session_id: "sub9", display_name: "甲" },
    probe: (b) => {
      const u = rows(b).find((m) => m.id === "u1");
      expect(u).toMatchObject({ status: undefined }); // 清 sending，但绝不标 failed
      expect(u?.failed).toBeFalsy();
      expect(u?.teamDelivery).toMatchObject({ receipt: "delivered", memberSessionId: "sub9" });
      expect(refOf(b, "busyBySessionRef")).toBe(false);
      expect(refOf(b, "liveBySessionRef")).toBeNull();
      expect(rows(b).some((m) => m.id === "live9")).toBe(false); // 空占位撤除
    },
  },
  {
    event: "team.reply", desc: "成员回帖按 message_id 幂等追加",
    ev: { message_id: "mr1", text: "回帖内容" },
    probe: (b) => {
      const m = rows(b).find((x) => x.id === "mr1");
      expect(m).toBeTruthy();
      expect(String(blockOf(b, "text", SID) === undefined ? "" : "")).toBe(""); // no-op（形状断言在上面）
    },
  },

  // —— mods ——
  {
    event: "mod.bands", desc: "整树替换进 per-mod 段",
    ev: { mods: [{ mod: "m1", generation: 1, tree: [{ id: "n" }] }] },
    probe: (b) => {
      const updater = b.setModBands.mock.calls[0] as unknown as [(p: unknown) => unknown];
      const next = updater[0]({});
      expect(next).toEqual({ [SID]: [{ mod: "m1", generation: 1, tree: [{ id: "n" }] }] });
    },
  },
  {
    event: "mod.toast", desc: "收编进全局 notice 总线",
    ev: { level: "warn", text: "警告" },
    probe: () => expect(pushNotice).toHaveBeenCalledWith("warn", "警告", expect.objectContaining({ source: "mod" })),
  },
  {
    event: "mod.state.changed", desc: "设置页重拉事件",
    ev: {},
    probe: () => {
      const evName = (winDispatch.mock.calls[0] as unknown as [{ type?: string }])[0]?.type;
      expect(evName).toBe("ginno:mods-state-changed");
    },
  },
  {
    event: "mod.prompt.fill", desc: "接管 composer 草稿（覆盖语义）",
    ev: { text: "填入" },
    probe: () => {
      const evName = (winDispatch.mock.calls[0] as unknown as [{ type?: string }])[0]?.type;
      expect(evName).toBe("ginno:mod-fill-input");
    },
  },
  {
    event: "mod.prompt.suggest", desc: "幽灵建议事件",
    ev: { text: "建议" },
    probe: () => {
      const evName = (winDispatch.mock.calls[0] as unknown as [{ type?: string }])[0]?.type;
      expect(evName).toBe("ginno:suggest-input");
    },
  },
  {
    event: "mod.ask", desc: "问题浮层按 sid 存最新",
    ev: { id: "ask1", message: "问", choices: ["是", "否"] },
    probe: (b) => {
      const updater = b.setModAsks.mock.calls[0] as unknown as [(p: unknown) => unknown];
      const next = updater[0]({});
      expect(next).toEqual({ [SID]: { id: "ask1", message: "问", choices: ["是", "否"], mod: undefined } });
    },
  },
  {
    event: "mod.pane.open", desc: "pane 开（转发 paneBus）",
    ev: { id: "pn1", title: "面板" },
    probe: () => expect(pushModPane).toHaveBeenCalledWith(SID, "open", expect.objectContaining({ id: "pn1", title: "面板" })),
  },
  {
    event: "mod.pane.update", desc: "pane 更新",
    ev: { id: "pn1", title: "面板" },
    probe: () => expect(pushModPane).toHaveBeenCalledWith(SID, "update", expect.objectContaining({ id: "pn1" })),
  },
  {
    event: "mod.pane.close", desc: "pane 关",
    ev: { id: "pn1" },
    probe: () => expect(pushModPane).toHaveBeenCalledWith(SID, "close", expect.objectContaining({ id: "pn1" })),
  },
];

describe("② 全事件 dispatch 触达表（注册表化重构的验收网）", () => {
  it.each(DISPATCH_TABLE)("$event — $desc", async (c) => {
    const bag = makeBag();
    c.seed?.(bag);
    for (const p of c.pre ?? []) handle(bag.ctx, SID, p);
    handle(bag.ctx, SID, { event: c.event, ...(c.ev ?? {}) });
    await c.probe(bag);
  });

  it("②表覆盖①钉住的全部事件类型（两张清单不脱节）", () => {
    expect(DISPATCH_TABLE.map((c) => c.event).sort()).toEqual([...EXPECTED_CASES].sort());
  });
});

describe("session.model_changed effort reset", () => {
  it("uses the authoritative Auto and replaces the ordinary model toast", () => {
    const bag = makeBag();
    vi.mocked(pushNotice).mockClear();
    handle(bag.ctx, SID, {
      event: "session.model_changed", provider: "p1", model: "model-b",
      thinking_effort: "auto", effort_reset: true,
    });
    expect(bag.g.applySessionPatch).toHaveBeenCalledWith(SID, {
      provider: "p1", model: "model-b", thinking_effort: "auto",
    });
    expect(pushNotice).toHaveBeenCalledTimes(1);
    expect(pushNotice).toHaveBeenCalledWith("success", "model.thinkingReset", {
      source: "model", dedupeKey: `model:${SID}`, inbox: false,
    });
  });
});

// ── ③ 高风险 case 深度断言 ────────────────────────────────────────────────
describe("③ token.delta 共享体边界", () => {
  it("流帧不清 sending（markDelivered 只属于 turn.start / 收口事件）", () => {
    const bag = makeBag();
    seedSendingUser(bag);
    handle(bag.ctx, SID, { event: "token.delta", content: "甲", turn_id: "t1" });
    handle(bag.ctx, SID, { event: "tool.start", id: "c1", name: "bash", turn_id: "t1" });
    handle(bag.ctx, SID, { event: "user.question", id: "q-3-1", question: "？" });
    expect(rows(bag).find((m) => m.id === "u1")).toMatchObject({ status: "sending" });
    // 对照：turn.start 立刻清。
    handle(bag.ctx, SID, { event: "turn.start", turn_id: "t1", agent_id: "a1" });
    expect(rows(bag).find((m) => m.id === "u1")).toMatchObject({ status: undefined });
  });

  it("notice(auto_retry) 提示在恢复出流时原位撤行，新一轮替换不堆叠", () => {
    const bag = makeBag();
    handle(bag.ctx, SID, { event: "notice", i18n_key: "stream.auto_retry", message: "第1次重试" });
    expect(String(blockOf(bag, "text")?.text)).toContain("⚠️ 第1次重试");
    expect(refOf(bag, "retryNoticeRef")).toContain("第1次重试");

    handle(bag.ctx, SID, { event: "token.delta", content: "恢复出流", turn_id: "t1" });
    expect(refOf(bag, "retryNoticeRef")).toBeNull();
    const texts = rows(bag).flatMap((m) => m.blocks.filter((b) => b.kind === "text"));
    expect(texts.some((b) => String(b.text).includes("第1次重试"))).toBe(false); // 撤行
    expect(texts.some((b) => String(b.text).includes("恢复出流"))).toBe(true);

    handle(bag.ctx, SID, { event: "notice", i18n_key: "stream.auto_retry", message: "第2次重试" });
    const warns = rows(bag)
      .flatMap((m) => m.blocks.filter((b) => b.kind === "text"))
      .filter((b) => String(b.text).includes("⚠️"));
    expect(warns).toHaveLength(1); // 原位替换，不堆 N 行
    expect(String(warns[0]?.text)).toContain("第2次重试");
  });
});

describe("③ message.end 收口契约", () => {
  function seedTurn(bag: Bag, turn = "t1") {
    handle(bag.ctx, SID, { event: "turn.start", turn_id: turn, agent_id: "a1" });
    handle(bag.ctx, SID, { event: "token.delta", content: "答案", turn_id: turn });
  }

  it("meta.elapsed_s 权威值定格 turn_meta（删 t0Ms 停跳），token 一并写入", () => {
    const bag = makeBag();
    seedTurn(bag);
    handle(bag.ctx, SID, { event: "message.end", turn_id: "t1", meta: { elapsed_s: 5, input_tokens: 10, output_tokens: 3 } });
    const tm = blockOf(bag, "turn_meta");
    expect(tm).toMatchObject({ elapsedS: 5, inputTokens: 10, outputTokens: 3 });
    expect(tm).not.toHaveProperty("t0Ms");
    expect(rows(bag).find((m) => m.blocks.some((b) => b.kind === "text"))?.ts).toBeGreaterThan(0); // stampTurnTs
  });

  it("reply 通知进 30s 合并窗：handle 返回时不直发 OS 通知", () => {
    const pushSpy = vi.spyOn(ReplyMergeWindow.prototype, "push");
    const bag = makeBag();
    seedTurn(bag);
    handle(bag.ctx, SID, { event: "message.end", turn_id: "t1", meta: { elapsed_s: 1 } });
    expect(pushSpy).toHaveBeenCalledWith(SID, expect.objectContaining({ title: "契约会话" }));
    expect(notifyNative).not.toHaveBeenCalled(); // flush 在 30s 后，且 flush 前绝不弹
  });

  it("origin=schedule 的 headless 完成不进 reply 合并窗（防双弹），未读点保留", () => {
    const pushSpy = vi.spyOn(ReplyMergeWindow.prototype, "push");
    const bag = makeBag();
    seedTurn(bag);
    handle(bag.ctx, SID, { event: "message.end", turn_id: "t1", origin: "schedule", meta: { elapsed_s: 1 } });
    expect(pushSpy).not.toHaveBeenCalled();
    expect(bag.g.markSessionUnread).toHaveBeenCalledWith(SID);
  });

  it("正盯着该会话：不标未读、上报已读压 last_finished", () => {
    const bag = makeBag({ activeSidRef: { current: SID } });
    vi.stubGlobal("document", { visibilityState: "visible" });
    // isSessionAttentive 还要求 window.location.pathname === "/"
    vi.stubGlobal("window", { location: { pathname: "/" }, dispatchEvent: vi.fn() });
    seedTurn(bag);
    handle(bag.ctx, SID, { event: "message.end", turn_id: "t1", meta: { elapsed_s: 1 } });
    expect(bag.g.clearSessionUnread).toHaveBeenCalledWith(SID);
    expect(bag.g.markSessionUnread).not.toHaveBeenCalled();
  });
});

describe("③ 弹层边界与收口复位", () => {
  it("permission.request 只写 permsRef（React 同步在 engine 层，不在 reducer）", () => {
    const bag = makeBag();
    handle(bag.ctx, SID, { event: "permission.request", tool: "bash", args: { cmd: "ls" } });
    expect(refOf(bag, "permsRef")).toEqual({ tool: "bash", args: { cmd: "ls" } });
    expect(bag.setPermission).not.toHaveBeenCalled();
  });

  it("turn.stopped 关闭在飞工具块（pending 收口，running 不卡死）", () => {
    const bag = makeBag();
    handle(bag.ctx, SID, { event: "turn.start", turn_id: "t1", agent_id: "a1" });
    handle(bag.ctx, SID, { event: "tool.start", id: "c1", name: "bash", turn_id: "t1" });
    handle(bag.ctx, SID, { event: "turn.stopped" });
    const tool = blockOf(bag, "tool");
    expect(tool).toBeTruthy();
    expect(tool).not.toMatchObject({ pending: true });
  });

  it("error：live 气泡标 failed（红边卡）+ 失败通知即时直发", () => {
    const bag = makeBag();
    handle(bag.ctx, SID, { event: "turn.start", turn_id: "t1", agent_id: "a1" });
    handle(bag.ctx, SID, { event: "token.delta", content: "半截", turn_id: "t1" });
    handle(bag.ctx, SID, { event: "error", message: "网关 502" });
    const live = rows(bag).find((m) => m.failed === true);
    expect(live).toBeTruthy();
    expect((notifyNative.mock.calls[0] as unknown[])[0]).toMatchObject({ notifyKind: "failure", body: "网关 502" });
  });
});

describe("③ 压缩阶段", () => {
  it("旧运行时的估算值不影响阶段，通知和保活也不会清除状态", () => {
    const bag = makeBag();
    handle(bag.ctx, SID, { event: "context.compacting", stage: "summarizing", pct: 8, predicted_s: 30 });
    handle(bag.ctx, SID, { event: "notice", message: "x" });
    handle(bag.ctx, SID, { event: "keepalive" });
    expect(refOf(bag, "compactStageRef")).toBe("summarizing");
    handle(bag.ctx, SID, { event: "context.compacting", stage: "writing" });
    expect(refOf(bag, "compactStageRef")).toBe("writing");
  });

  it("重连恢复保存阶段，后续探针可以补上丢失的完成帧", () => {
    const bag = makeBag();
    handle(bag.ctx, SID, { event: "turn.state", running: false, compacting: true, compaction_stage: "writing" });
    expect(refOf(bag, "compactStageRef")).toBe("writing");
    handle(bag.ctx, SID, { event: "turn.state", running: false, compacting: false });
    expect(refOf(bag, "compactStageRef")).toBeNull();
  });

  it("压缩完成即清除阶段", () => {
    const bag = makeBag();
    handle(bag.ctx, SID, { event: "context.compacting", stage: "summarizing" });
    handle(bag.ctx, SID, { event: "context.compacting", stage: "done" });
    expect(refOf(bag, "compactStageRef")).toBeNull();
  });
});

// Run real reducers and the production scheduler with a manually advanced clock.
function displayHarness() {
  const callbacks: FrameRequestCallback[] = [];
  const cancelFrame = vi.fn();
  const document = { hidden: false };
  vi.stubGlobal("document", document);
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => callbacks.push(cb));
  vi.stubGlobal("cancelAnimationFrame", cancelFrame);
  let active = SID;
  const bag = makeBag();
  const commit = vi.fn((sid: string) => ({
    text: blockOf(bag, "text", sid)?.text,
    permission: bag.ctx.permsRef.current[sid]?.tool,
    live: !!bag.ctx.liveBySessionRef.current[sid],
  }));
  const display = createDisplayScheduler(() => active, commit);
  bag.ctx.syncDisplay = display.sync;
  bag.ctx.seenTurnStartRef.current[SID] = true;
  const dispatch = (event: string, payload = {}, sid = SID) => {
    handle(bag.ctx, sid, { event, ...payload }, display.defer);
  };
  return {
    dispatch, display, commit, document, cancelFrame, callbacks,
    switchTo(sid: string) { display.cancel(); active = sid; display.sync(sid); },
  };
}

describe("stream display commits", () => {
  it("commits 50 same-frame deltas once, with every token in order", () => {
    const { dispatch, commit, callbacks } = displayHarness();
    const chunks = Array.from({ length: 50 }, (_, index) => `${index},`);
    for (const content of chunks) dispatch("token.delta", { content });
    expect(commit).toHaveBeenCalledTimes(0);
    expect(callbacks).toHaveLength(1);
    callbacks[0](0);
    expect(commit).toHaveBeenCalledTimes(1);
    expect(commit).toHaveReturnedWith({ text: chunks.join(""), permission: undefined, live: true });
  });

  it("shows prior deltas with permission and the last delta with the finished turn", () => {
    const { dispatch, commit, callbacks, cancelFrame } = displayHarness();
    dispatch("token.delta", { content: "before permission" });
    dispatch("permission.request", { tool: "bash" });
    dispatch("token.delta", { content: " and before finish" });
    dispatch("message.end", { origin: "schedule" });
    expect(commit.mock.results[0]?.value).toEqual({ text: "before permission", permission: "bash", live: true });
    expect(commit.mock.results[1]?.value).toEqual({ text: "before permission and before finish", permission: "bash", live: false });
    expect(cancelFrame.mock.calls).toEqual([[1], [2]]);
    callbacks.forEach((callback) => callback(0));
    expect(commit).toHaveBeenCalledTimes(2);
  });

  it("flushes on hide and ignores canceled callbacks across disconnect and session switches", () => {
    const { dispatch, display, commit, document, callbacks, switchTo } = displayHarness();
    dispatch("token.delta", { content: "visible" });
    document.hidden = true;
    display.flush();
    dispatch("token.delta", { content: " hidden" });
    expect(commit.mock.results[1]?.value).toEqual({ text: "visible hidden", permission: undefined, live: true });
    expect(callbacks).toHaveLength(1);
    document.hidden = false;
    dispatch("token.delta", { content: " disconnect" });
    display.sync(SID);
    expect(commit.mock.results[2]?.value).toEqual({ text: "visible hidden disconnect", permission: undefined, live: true });
    dispatch("token.delta", { content: " old" });
    switchTo("s2");
    dispatch("token.delta", { content: " background" });
    dispatch("token.delta", { content: "new session" }, "s2");
    expect(callbacks).toHaveLength(4);
    callbacks.slice(0, 3).forEach((callback) => callback(0));
    expect(commit).toHaveBeenCalledTimes(4);
    callbacks[3](0);
    expect(commit.mock.results[4]?.value).toEqual({ text: "new session", permission: undefined, live: true });
    switchTo(SID);
    callbacks[2](0);
    expect(commit).toHaveBeenCalledTimes(6);
    expect(commit.mock.results[5]?.value).toEqual({ text: "visible hidden disconnect old background", permission: undefined, live: true });
  });
});
