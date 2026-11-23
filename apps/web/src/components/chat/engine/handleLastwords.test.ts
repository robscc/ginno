// engine/handle 的 lastwords 恢复提示回归（2026-11-16 交互补齐）。
//
// 事故背景：「遗言好像没如我所愿触发过」。后端修好（任意 reason 的人工会话
// 都广播 runtime.lastwords_pending + head 不再恒空）后，前端旧实现仍只投一条
// 6s 即逝的通知、点击仅跳会话——用户没有「恢复」按钮可点，/api/lastwords/resume
// 从未被前端调用。本文件把真实事件喂进真实 handle() reducer，断言：
//   ① 通知是常驻决策卡（duration:0）+ 主按钮「恢复」+ 次按钮「忽略」；
//   ② 文案带真实请求摘要（head）与被中断后台任务数（bg_interrupted）；
//   ③ 点「恢复」真调 resumeLastwords（成功/no_pending/失败三路反馈）；
//   ④ 点「忽略」真调 dismissLastwords（dismiss:true 语义在 API 层）；
//   ⑤ OS 通知与 dedupeKey 行为保留。
//
// 范式同 handleNotify.test.ts：mock 掉 API/OS 出口，notice 总线用真的
// （node 可跑，localStorage/rAF 均有守卫），subscribeNotices 捕获卡片。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { resumeLastwords, dismissLastwords, notifyNative } = vi.hoisted(() => ({
  resumeLastwords: vi.fn(
    async (_sid: string): Promise<{ ok: boolean; error?: string }> => ({ ok: true }),
  ),
  dismissLastwords: vi.fn(
    async (_sid: string): Promise<{ ok: boolean; had?: boolean }> => ({ ok: true, had: true }),
  ),
  notifyNative: vi.fn(async (_n: unknown) => true),
}));

vi.mock("@/lib/runtime", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return { ...actual, resumeLastwords, dismissLastwords };
});
vi.mock("@/lib/desktop", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return { ...actual, notifyNative };
});

import { handle } from "@/components/chat/engine/handle";
import type { EngineCtx } from "@/components/chat/engine/ctx";
import { clearNotices, subscribeNotices, type Notice } from "@/lib/notice";

// 最小 EngineCtx（同 handleNotify.test.ts）：lastwords 路径只读 i18nRef，
// 其余字段是 handle 顶部公共段需要的最小集。
function makeCtx(overrides: Record<string, unknown> = {}): EngineCtx {
  const ref = <T,>(v: T) => ({ current: v });
  const base = {
    i18nRef: ref({
      // 带插值的 tr 桩：键名 + JSON 参数——文案断言能看见 head/bgInterrupted
      // 的真实取值，而不是只锁键名。
      tr: (k: string, vars?: Record<string, unknown>) =>
        vars ? `${k} ${JSON.stringify(vars)}` : k,
      evText: (_e: unknown, f: string) => f,
    }),
    activeSidRef: ref<string | null>(null),
    storeRef: ref<Record<string, unknown[]>>({}),
    liveBySessionRef: ref<Record<string, string | null>>({}),
    socketsRef: ref<Record<string, unknown>>({}),
    busyBySessionRef: ref<Record<string, boolean>>({}),
    compactStageRef: ref<Record<string, unknown>>({}),
    streamAgentRef: ref<Record<string, string | null>>({}),
    orphanStreamRef: ref<Record<string, boolean>>({}),
    adoptedTurnRef: ref<Record<string, boolean>>({}),
    retryNoticeRef: ref<Record<string, string | null>>({}),
    seenTurnStartRef: ref<Record<string, boolean>>({}),
    session: { agent_id: "dev-agent" },
    g: { sessions: [{ id: "s1", title: "测试会话" }] },
    syncDisplay: () => {},
  };
  return { ...base, ...overrides } as unknown as EngineCtx;
}

const PENDING_EV = {
  event: "runtime.lastwords_pending",
  session_id: "s1",
  head: "帮我把季度报告汇总成 PPT",
  user_request_head: "帮我把季度报告汇总成 PPT",
  reason: "signal",
  bg_running: 1,
  bg_interrupted: 2,
  bg_interrupted_ids: ["t-a", "t-b"],
  tool_calls: 7,
  elapsed_s: 185,
};

let notices: Notice[] = [];
let unsub: (() => void) | null = null;

beforeEach(() => {
  clearNotices();
  notices = [];
  unsub = subscribeNotices((l) => {
    notices = l;
  });
  resumeLastwords.mockClear();
  resumeLastwords.mockImplementation(async () => ({ ok: true }));
  dismissLastwords.mockClear();
  dismissLastwords.mockImplementation(async () => ({ ok: true, had: true }));
  notifyNative.mockClear();
});
afterEach(() => {
  unsub?.();
  unsub = null;
  clearNotices();
});

/** 等 fire-and-forget 的 async onClick 落地（真实 Promise 微任务）。 */
async function flush() {
  await vi.waitFor(() => {
    expect(notices.length).toBeGreaterThan(0);
  });
  await new Promise((r) => setTimeout(r, 0));
}

describe("runtime.lastwords_pending → 常驻恢复决策卡", () => {
  it("通知常驻（duration:0）+ 恢复主按钮 + 忽略次按钮 + 跳会话 actionMeta", () => {
    handle(makeCtx(), "s1", { ...PENDING_EV });
    expect(notices).toHaveLength(1);
    const n = notices[0];
    expect(n.level).toBe("info");
    expect(n.duration).toBe(0); // 等用户决策，绝不 6s 自动消失
    expect(n.dedupeKey).toBe("lastwords-pending:s1");
    expect(n.actionMeta).toEqual({ kind: "session", id: "s1" });
    expect(n.action?.label).toBe("notice.lastwords-resume-btn");
    expect(n.secondaryAction?.label).toBe("notice.lastwords-dismiss-btn");
    expect(n.countInBadge).toBe(true);
    expect(n.kind).toBe("lastwords");
  });

  it("文案含真实请求摘要（head）与被中断后台任务数（bg_interrupted）", () => {
    handle(makeCtx(), "s1", { ...PENDING_EV });
    const text = notices[0].text;
    expect(text).toContain("帮我把季度报告汇总成 PPT");
    expect(text).toContain('"bgInterrupted":2');
    expect(text).toContain('"tools":7');
    expect(text).toContain('"elapsed":"3:05"'); // 185s → 3:05
    // 旧文案「输入「继续」即可续跑」的键已退役
    expect(text).not.toContain("lastwordsPending ");
  });

  it("head 缺失时回落占位键；只有 user_request_head 也能取到摘要", () => {
    handle(makeCtx(), "s1", { ...PENDING_EV, head: "", user_request_head: "" });
    expect(notices[0].text).toContain("notice.lastwords-no-head");
    clearNotices();
    handle(makeCtx(), "s1", {
      ...PENDING_EV,
      head: undefined,
      user_request_head: "仅备用键的摘要",
    });
    expect(notices[0].text).toContain("仅备用键的摘要");
    clearNotices();
    // head 为空串（非 null）也要回落备用键，不能吞成占位符
    handle(makeCtx(), "s1", {
      ...PENDING_EV,
      head: "",
      user_request_head: "空串 head 的备用摘要",
    });
    expect(notices[0].text).toContain("空串 head 的备用摘要");
  });

  it("同一会话重复广播 → dedupe 只留一张卡（不堆叠）", () => {
    const ctx = makeCtx();
    handle(ctx, "s1", { ...PENDING_EV });
    handle(ctx, "s1", { ...PENDING_EV });
    expect(notices).toHaveLength(1);
  });

  it("缺 session_id → 不投通知也不发 OS 通知", () => {
    handle(makeCtx(), "s1", { ...PENDING_EV, session_id: "" });
    expect(notices).toHaveLength(0);
    expect(notifyNative).not.toHaveBeenCalled();
  });

  it("OS 通知保留：kind=session / notifyKind=askUser / 标题与正文同源", () => {
    handle(makeCtx(), "s1", { ...PENDING_EV });
    expect(notifyNative).toHaveBeenCalledTimes(1);
    const payload = notifyNative.mock.calls[0][0] as {
      kind: string;
      notifyKind: string;
      id: string;
      title: string;
      body: string;
    };
    expect(payload).toMatchObject({ kind: "session", notifyKind: "askUser", id: "s1" });
    expect(payload.title).toContain("notice.lastwords-pending-title");
    expect(payload.body).toBe(notices[0].text);
  });
});

describe("「恢复」主按钮 → 真调 /api/lastwords/resume", () => {
  it("成功：resumeLastwords(sid) 被调 + success 反馈原位刷新", async () => {
    handle(makeCtx(), "s1", { ...PENDING_EV });
    notices[0].action!.onClick();
    await flush();
    expect(resumeLastwords).toHaveBeenCalledWith("s1");
    expect(dismissLastwords).not.toHaveBeenCalled();
    await vi.waitFor(() => {
      expect(notices[0].level).toBe("success");
    });
    expect(notices[0].text).toContain("notice.lastwords-resume-ok");
    expect(notices[0].dedupeKey).toBe("lastwords-pending:s1");
    expect(notices).toHaveLength(1); // 原位刷新，不堆第二条
  });

  it("no_pending（过期/已处置）：warn 明确提示，不重试", async () => {
    resumeLastwords.mockImplementation(async () => ({ ok: false, error: "no_pending" }));
    handle(makeCtx(), "s1", { ...PENDING_EV });
    notices[0].action!.onClick();
    await vi.waitFor(() => {
      expect(notices[0].level).toBe("warn");
    });
    expect(notices[0].text).toContain("notice.lastwords-resume-expired");
    expect(resumeLastwords).toHaveBeenCalledTimes(1);
  });

  it("ok:false 其它错误：error 常驻卡 + 重试按钮可再点", async () => {
    resumeLastwords.mockImplementation(async () => ({ ok: false, error: "boom" }));
    handle(makeCtx(), "s1", { ...PENDING_EV });
    notices[0].action!.onClick();
    await vi.waitFor(() => {
      expect(notices[0].level).toBe("error");
    });
    expect(notices[0].text).toContain("notice.lastwords-resume-failed");
    expect(notices[0].text).toContain("boom");
    expect(notices[0].duration).toBe(0);
    // 重试：再点一次错误卡上的恢复按钮
    resumeLastwords.mockImplementation(async () => ({ ok: true }));
    notices[0].action!.onClick();
    await vi.waitFor(() => {
      expect(notices[0].level).toBe("success");
    });
    expect(resumeLastwords).toHaveBeenCalledTimes(2);
  });

  it("网络异常：error 卡带异常信息（不静默吞错）", async () => {
    resumeLastwords.mockImplementation(async () => {
      throw new Error("ECONNREFUSED");
    });
    handle(makeCtx(), "s1", { ...PENDING_EV });
    notices[0].action!.onClick();
    await vi.waitFor(() => {
      expect(notices[0].level).toBe("error");
    });
    expect(notices[0].text).toContain("ECONNREFUSED");
  });
});

describe("「忽略」次按钮 → 真调 dismiss:true", () => {
  it("dismissLastwords(sid) 被调 + 已忽略反馈原位刷新", async () => {
    handle(makeCtx(), "s1", { ...PENDING_EV });
    notices[0].secondaryAction!.onClick();
    await vi.waitFor(() => {
      expect(dismissLastwords).toHaveBeenCalledWith("s1");
    });
    await vi.waitFor(() => {
      expect(notices[0].text).toContain("notice.lastwords-dismissed");
    });
    expect(resumeLastwords).not.toHaveBeenCalled();
    expect(notices[0].dedupeKey).toBe("lastwords-pending:s1");
    expect(notices).toHaveLength(1);
  });

  it("dismiss 网络异常：error 卡 + 重试按钮", async () => {
    dismissLastwords.mockImplementation(async () => {
      throw new Error("offline");
    });
    handle(makeCtx(), "s1", { ...PENDING_EV });
    notices[0].secondaryAction!.onClick();
    await vi.waitFor(() => {
      expect(notices[0].level).toBe("error");
    });
    expect(notices[0].text).toContain("notice.lastwords-dismiss-failed");
    expect(notices[0].text).toContain("offline");
    expect(notices[0].action?.label).toBe("notice.lastwords-dismiss-btn");
  });
});
